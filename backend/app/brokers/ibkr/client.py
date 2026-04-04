"""
IBKRClient — wraps ib_async to implement BaseBroker for Interactive Brokers.

Connects via TCP socket to a locally running IB Gateway process. The Gateway
holds the authenticated IBKR session; this client never handles credentials.

See .claude/specs/ibkr-gateway.md for setup and daily startup procedure.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from ib_async import IB, LimitOrder, MarketOrder, Stock

from app.brokers.base import (
    AccountSummary,
    BaseBroker,
    OrderRequest,
    OrderResult,
    Position,
    PriceBar,
    PriceFeedCallback,
    PriceUpdate,
)
from app.brokers.ibkr import mapper
from app.config import Settings
from app.monitoring.logger import system_logger

# How long (seconds) to wait for an order fill before returning with
# whatever status we have.  Market orders on paper typically fill in <1s;
# this guard prevents an infinite wait on edge cases.
_ORDER_FILL_TIMEOUT = 30


class IBKRClient(BaseBroker):
    """
    Production broker implementation backed by Interactive Brokers via ib_async.

    Instantiation does not connect — call connect() explicitly (or rely on
    the FastAPI lifespan handler to do so).

    Live trading guard: connect() raises RuntimeError if ENVIRONMENT=development
    and IBKR_TRADING_MODE=live are both set, preventing accidental live order
    submission during development.
    """

    _RECONNECT_DELAYS = [5, 15, 30, 60]  # seconds between attempts

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ib = IB()
        # symbol → Stock Contract (needed to cancel subscriptions)
        self._subscribed_contracts: dict[str, Stock] = {}
        self._price_callbacks: list[PriceFeedCallback] = []
        self._pending_tickers_handler_registered = False
        self._reconnecting = False
        self._ib.disconnectedEvent += self._on_disconnected
        self._ib.errorEvent += self._on_error

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Connect to IB Gateway.

        Raises RuntimeError if the live trading guard is triggered or if
        Gateway is not reachable.
        """
        self._enforce_live_trading_guard()

        await self._ib.connectAsync(
            self._settings.ibkr_host,
            self._settings.ibkr_port,
            clientId=self._settings.ibkr_client_id,
        )
        system_logger.info(
            "IBKRClient connected",
            extra={
                "host": self._settings.ibkr_host,
                "port": self._settings.ibkr_port,
                "client_id": self._settings.ibkr_client_id,
                "trading_mode": self._settings.ibkr_trading_mode,
            },
        )

    async def disconnect(self) -> None:
        self._reconnecting = False  # prevent reconnect loop from firing after intentional disconnect
        if self._ib.isConnected():
            self._ib.disconnect()
            system_logger.info("IBKRClient disconnected")
        self._subscribed_contracts.clear()
        self._price_callbacks.clear()
        self._pending_tickers_handler_registered = False

    def _on_disconnected(self) -> None:
        """
        Fired by ib_async when IB Gateway drops the connection unexpectedly.
        Schedules an async reconnect loop on the running event loop.
        """
        system_logger.warning(
            "IBKRClient lost connection to IB Gateway — scheduling reconnect",
            extra={
                "host": self._settings.ibkr_host,
                "port": self._settings.ibkr_port,
            },
        )
        if not self._reconnecting:
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._reconnect_loop())
            except RuntimeError:
                pass  # no running loop (e.g. during shutdown) — ignore

    def _on_error(self, req_id: int, error_code: int, error_string: str, contract) -> None:
        """
        Fired by ib_async for every error or informational message from IB Gateway.

        Error codes 1100-1300 relate to connectivity. Key ones:
          1100 — connection lost
          1101 — reconnected, data lost
          1102 — reconnected, data maintained
          2110 — connectivity between TWS and server is broken
        See: https://ibkrcampus.com/ibkr-api-page/trader-workstation-api/#error-codes
        """
        # IB uses low codes (1-9xx) for normal info messages; only log as warning/error for real problems
        if error_code < 2000:
            system_logger.warning(
                "IBKRClient: IB Gateway error",
                extra={
                    "req_id": req_id,
                    "error_code": error_code,
                    "error": error_string,
                    "symbol": contract.symbol if contract else None,
                },
            )
        else:
            system_logger.info(
                "IBKRClient: IB Gateway message",
                extra={
                    "req_id": req_id,
                    "error_code": error_code,
                    "message": error_string,
                },
            )

    async def _reconnect_loop(self) -> None:
        """
        Attempt to reconnect to IB Gateway with escalating delays.
        Stops if disconnect() is called intentionally (self._reconnecting=False).
        """
        self._reconnecting = True
        for delay in self._RECONNECT_DELAYS:
            if not self._reconnecting:
                return
            system_logger.info(f"IBKRClient reconnect attempt in {delay}s")
            await asyncio.sleep(delay)
            if not self._reconnecting:
                return
            try:
                await self._ib.connectAsync(
                    self._settings.ibkr_host,
                    self._settings.ibkr_port,
                    clientId=self._settings.ibkr_client_id,
                )
                system_logger.info("IBKRClient reconnected successfully")
                self._reconnecting = False
                return
            except Exception as exc:
                system_logger.warning(
                    "IBKRClient reconnect failed",
                    extra={"error": str(exc)},
                )
        self._reconnecting = False
        system_logger.error(
            "IBKRClient gave up reconnecting after all attempts failed"
        )

    def is_connected(self) -> bool:
        return self._ib.isConnected()

    # ------------------------------------------------------------------
    # Account & positions
    # ------------------------------------------------------------------

    async def get_account_summary(self) -> AccountSummary:
        self._require_connected()
        account_values = await self._ib.accountSummaryAsync()
        return mapper.map_account_summary(account_values)

    async def get_positions(self) -> list[Position]:
        self._require_connected()
        ib_positions = await self._ib.positionsAsync()
        return [mapper.map_position(p) for p in ib_positions]

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def subscribe_price_feed(
        self,
        tickers: list[str],
        callback: PriceFeedCallback,
    ) -> None:
        """
        Subscribe to real-time price ticks for the given symbols.

        Registers new contracts with IBKR and attaches the callback to
        the pendingTickersEvent. Subsequent calls with the same symbols
        are idempotent (already-subscribed tickers are skipped).
        """
        self._require_connected()

        self._price_callbacks.append(callback)

        for symbol in tickers:
            if symbol not in self._subscribed_contracts:
                contract = Stock(symbol, "SMART", "USD")
                self._ib.reqMktData(contract)
                self._subscribed_contracts[symbol] = contract

        if not self._pending_tickers_handler_registered:
            self._ib.pendingTickersEvent += self._on_pending_tickers
            self._pending_tickers_handler_registered = True

    def _on_pending_tickers(self, tickers: set) -> None:
        """
        Synchronous callback fired by ib_async when ticker data updates.

        Wraps each tick in a PriceUpdate and schedules async callbacks on
        the running event loop.
        """
        for ticker in tickers:
            if ticker.last is None and ticker.close is None:
                continue

            price = ticker.last if ticker.last is not None else ticker.close
            symbol = ticker.contract.symbol

            update = PriceUpdate(
                ticker=symbol,
                price=Decimal(str(price)),
                bid=Decimal(str(ticker.bid)) if ticker.bid is not None else None,
                ask=Decimal(str(ticker.ask)) if ticker.ask is not None else None,
                timestamp=datetime.now(timezone.utc),
            )
            loop = asyncio.get_event_loop()
            for cb in self._price_callbacks:
                loop.create_task(cb(update))

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResult:
        """
        Submit an order to IBKR and wait for fill or timeout.

        Uses SMART routing for US equities. Returns the result with
        whatever status was reached within _ORDER_FILL_TIMEOUT seconds.
        """
        self._require_connected()

        contract = Stock(order.symbol, "SMART", "USD")

        if order.order_type == "LMT" and order.limit_price is not None:
            ib_order = LimitOrder(order.direction, float(order.quantity), float(order.limit_price))
        else:
            ib_order = MarketOrder(order.direction, float(order.quantity))

        trade = self._ib.placeOrder(contract, ib_order)

        # Wait for the order to reach a terminal state within the timeout.
        deadline = asyncio.get_event_loop().time() + _ORDER_FILL_TIMEOUT
        while not trade.isDone() and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.1)

        return mapper.map_order_result(order.trade_id, trade)

    async def unsubscribe_price_feed(self, ticker: str) -> None:
        """Cancel a real-time market data subscription for one ticker."""
        self._require_connected()
        if ticker in self._subscribed_contracts:
            self._ib.cancelMktData(self._subscribed_contracts[ticker])
            del self._subscribed_contracts[ticker]

    async def get_historical_data(
        self,
        symbol: str,
        duration: str = "1 Y",
        bar_size: str = "1 day",
    ) -> list[PriceBar]:
        """Fetch OHLCV history from IB Gateway via reqHistoricalDataAsync."""
        self._require_connected()
        contract = Stock(symbol, "SMART", "USD")
        bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",       # empty string = now
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,          # regular trading hours only
        )
        return [mapper.map_price_bar(bar, bar_size) for bar in bars]

    async def validate_ticker(self, symbol: str) -> bool:
        """
        Check whether the symbol resolves to a valid IBKR-tradeable stock.

        Qualifies the contract against IBKR's contract database. Returns True
        if at least one matching contract is found, False otherwise. Falls back
        to True if not connected so callers are never blocked by connectivity.
        """
        if not self._ib.isConnected():
            return True  # cannot validate without connection — allow the add
        contract = Stock(symbol.upper(), "SMART", "USD")
        try:
            details = await self._ib.reqContractDetailsAsync(contract)
            return len(details) > 0
        except Exception:
            return False

    async def cancel_order(self, broker_order_id: str) -> bool:
        """
        Cancel a pending order by its IBKR order ID.

        Returns True if a matching order was found and the cancellation
        was submitted. Returns False if the order ID is unknown.
        """
        self._require_connected()

        for trade in self._ib.trades():
            if str(trade.order.orderId) == broker_order_id:
                self._ib.cancelOrder(trade.order)
                return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._ib.isConnected():
            raise RuntimeError(
                "IBKRClient is not connected. Call connect() first and ensure "
                "IB Gateway is running on "
                f"{self._settings.ibkr_host}:{self._settings.ibkr_port}."
            )

    def _enforce_live_trading_guard(self) -> None:
        """
        Raise if development environment is combined with live trading mode.

        This is a hard guard against accidentally placing real orders during
        development. See design.md § Security Considerations.
        """
        if (
            self._settings.environment == "development"
            and self._settings.ibkr_trading_mode == "live"
        ):
            raise RuntimeError(
                "Live trading guard triggered: ENVIRONMENT=development and "
                "IBKR_TRADING_MODE=live cannot be set simultaneously. "
                "Set IBKR_TRADING_MODE=paper or change ENVIRONMENT to proceed."
            )
