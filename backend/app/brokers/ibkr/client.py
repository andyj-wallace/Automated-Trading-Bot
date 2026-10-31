"""
IBKRClient — wraps ib_async to implement BaseBroker for Interactive Brokers.

Connects via TCP socket to a locally running IB Gateway process. The Gateway
holds the authenticated IBKR session; this client never handles credentials.

See DEPLOYMENT.md for running the Gateway (Docker, auto-login).
"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder

from app.brokers.base import (
    AccountSummary,
    BaseBroker,
    BrokerConnectionInfo,
    OrderRequest,
    OrderResult,
    Position,
    PriceBar,
    PriceFeedCallback,
    PriceUpdate,
)
from app.brokers.ibkr import mapper
from app.brokers.ibkr.hints import login_hint
from app.config import Settings
from app.core import market_calendar
from app.core.events import publish_system_alert
from app.core.tasks import spawn
from app.monitoring.logger import system_logger

if TYPE_CHECKING:
    from app.data.cache import RedisCache

# How long (seconds) to wait for an order fill before returning with
# whatever status we have.  Market orders on paper typically fill in <1s;
# this guard prevents an infinite wait on edge cases.
_CANCEL_WAIT_SECONDS = 10
_COMMISSION_WAIT_SECONDS = 2
_STOP_ACK_SECONDS = 10

# Re-emit an unchanged price after this many seconds so the Redis price key stays fresh.
_REEMIT_SECONDS = 5

_DEFAULT_BACKOFF = (5, 10, 20, 40, 60)


class LiveTradingGuardError(RuntimeError):
    """Raised when the configured or actual account mode conflicts with the environment."""


def _positive_decimal(value: float | None) -> Decimal | None:
    """Convert a tick value to Decimal; None for missing, NaN, infinite or non-positive."""
    if value is None or math.isnan(value) or math.isinf(value) or value <= 0:
        return None
    return Decimal(str(value))


class IBKRClient(BaseBroker):
    """
    Production broker implementation backed by Interactive Brokers via ib_async.

    Instantiation does not connect — call connect() explicitly (or rely on
    the FastAPI lifespan handler to do so).

    Live trading guard: connect() raises RuntimeError if ENVIRONMENT=development
    and IBKR_TRADING_MODE=live are both set, preventing accidental live order
    submission during development.
    """

    def __init__(
        self,
        settings: Settings,
        cache: "RedisCache | None" = None,
        *,
        backoff_seconds: tuple[float, ...] = _DEFAULT_BACKOFF,
        alert_after_seconds: float = 300,
        check_seconds: float = 5,
        clock: Callable[[], datetime] = market_calendar.now_et,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._cache = cache
        self._backoff = backoff_seconds
        self._alert_after = alert_after_seconds
        self._check_seconds = check_seconds
        self._ib = IB()
        # symbol -> Stock Contract (needed to cancel subscriptions and re-request after reconnect)
        self._subscribed_contracts: dict[str, Stock] = {}
        self._price_callbacks: list[PriceFeedCallback] = []
        self._pending_tickers_handler_registered = False
        self._last_emitted: dict[str, tuple[Decimal, datetime]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False
        self._supervisor_task: asyncio.Task | None = None
        self._reconnect_listeners: list[Callable[[], Awaitable[None]]] = []
        # Operator-facing connection state (see connection_info)
        self._down_since: datetime | None = None
        self._last_connected_at: datetime | None = None
        self._last_error: str | None = None
        self._account_mismatch = False
        self._ib.disconnectedEvent += self._on_disconnected
        self._ib.errorEvent += self._on_error

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Connect to IB Gateway and start the reconnect supervisor.

        A connectivity failure is logged and left to the supervisor to retry.
        Raises LiveTradingGuardError if the env guard or account-mode check fails.
        """
        self._enforce_live_trading_guard()
        self._stopping = False
        try:
            await self._connect_once()
        except LiveTradingGuardError:
            raise
        except Exception as exc:
            self._mark_down(exc)
            system_logger.warning(
                "IBKRClient: initial connect failed, supervisor will keep retrying",
                extra={"error": str(exc), "exception_type": type(exc).__name__},
            )
        if self._supervisor_task is None or self._supervisor_task.done():
            self._supervisor_task = asyncio.get_running_loop().create_task(
                self._supervise(), name="ibkr_supervisor"
            )

    async def _connect_once(self) -> None:
        await self._ib.connectAsync(
            self._settings.ibkr_host,
            self._settings.ibkr_port,
            clientId=self._settings.ibkr_client_id,
            timeout=10,
        )
        self._verify_account_mode()
        self._ib.reqMarketDataType(self._settings.ibkr_market_data_type)
        for contract in self._subscribed_contracts.values():
            self._ib.reqMktData(contract)
        self._last_connected_at = self._now()
        self._down_since = None
        self._last_error = None
        system_logger.info(
            "IBKRClient connected",
            extra={
                "host": self._settings.ibkr_host,
                "port": self._settings.ibkr_port,
                "client_id": self._settings.ibkr_client_id,
                "trading_mode": self._settings.ibkr_trading_mode,
                "resubscribed": len(self._subscribed_contracts),
            },
        )

    def _verify_account_mode(self) -> None:
        """Paper accounts have IDs starting with 'D'; refuse anything else in paper mode."""
        if self._settings.ibkr_trading_mode != "paper":
            return
        accounts = list(self._ib.managedAccounts() or [])
        bad = [a for a in accounts if not str(a).startswith("D")]
        if bad:
            self._account_mismatch = True
            self._ib.disconnect()
            raise LiveTradingGuardError(
                "IBKR_TRADING_MODE=paper but the Gateway is logged into a non-paper account "
                f"(account prefix {str(bad[0])[:2]}). Refusing to trade."
            )

    def add_reconnect_listener(self, fn: Callable[[], Awaitable[None]]) -> None:
        """Register a coroutine function awaited after every successful reconnect."""
        self._reconnect_listeners.append(fn)

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def _mark_down(self, exc: BaseException | None = None) -> None:
        if self._down_since is None:
            self._down_since = self._now()
        if exc is not None:
            self._last_error = str(exc) or type(exc).__name__

    def _hint(self) -> str:
        return login_hint(self._clock(), self._settings.ib_auto_restart_time)

    def connection_info(self) -> BrokerConnectionInfo:
        """Connection state plus the most likely fix, for the dashboard and health check."""
        if self._ib.isConnected():
            return BrokerConnectionInfo(state="connected", last_connected_at=self._last_connected_at)
        if self._account_mismatch:
            state = "account_mismatch"
            hint = "The Gateway is logged into a non-paper account; log it into the paper account."
        else:
            down_for = (self._now() - self._down_since).total_seconds() if self._down_since else 0
            state = "down" if down_for >= self._alert_after else "reconnecting"
            hint = self._hint()
        return BrokerConnectionInfo(
            state=state,
            down_since=self._down_since,
            last_connected_at=self._last_connected_at,
            last_error=self._last_error,
            hint=hint,
        )

    async def _supervise(self) -> None:
        loop = asyncio.get_running_loop()
        down_since: float | None = None
        attempt = 0
        alerted = False
        while not self._stopping:
            if self._ib.isConnected():
                if alerted:
                    await publish_system_alert(
                        self._cache,
                        "BROKER_RECONNECTED",
                        "IB Gateway connection restored",
                        severity="INFO",
                    )
                down_since, attempt, alerted = None, 0, False
                await asyncio.sleep(self._check_seconds)
                continue
            down_since = down_since or loop.time()
            self._mark_down()
            await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
            if self._stopping:
                return
            attempt += 1
            try:
                await self._connect_once()
            except LiveTradingGuardError as exc:
                await publish_system_alert(self._cache, "BROKER_ACCOUNT_MODE_MISMATCH", str(exc))
                self._stopping = True
                return
            except Exception as exc:
                self._mark_down(exc)
                system_logger.warning(
                    "IBKRClient: reconnect attempt failed",
                    extra={"attempt": attempt, "error": str(exc)},
                )
                if not alerted and loop.time() - down_since >= self._alert_after:
                    hint = self._hint()
                    minutes = max(1, int((loop.time() - down_since) // 60))
                    await publish_system_alert(
                        self._cache,
                        "BROKER_DISCONNECTED",
                        f"IB Gateway down {minutes} min. {hint}",
                        host=self._settings.ibkr_host,
                        port=self._settings.ibkr_port,
                        hint=hint,
                    )
                    alerted = True
                continue
            for listener in self._reconnect_listeners:
                try:
                    await listener()
                except Exception:
                    system_logger.exception("IBKRClient: reconnect listener failed")

    async def disconnect(self) -> None:
        self._stopping = True
        task, self._supervisor_task = self._supervisor_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._ib.isConnected():
            self._ib.disconnect()
            system_logger.info("IBKRClient disconnected")
        if self._pending_tickers_handler_registered:
            self._ib.pendingTickersEvent -= self._on_pending_tickers
        self._subscribed_contracts.clear()
        self._price_callbacks.clear()
        self._last_emitted.clear()
        self._pending_tickers_handler_registered = False

    def _on_disconnected(self) -> None:
        """Fired by ib_async when the Gateway drops the connection; the supervisor reconnects."""
        system_logger.warning(
            "IBKRClient lost connection to IB Gateway",
            extra={"host": self._settings.ibkr_host, "port": self._settings.ibkr_port},
        )

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
                    "ib_message": error_string,
                },
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
        ib_positions = await self._ib.reqPositionsAsync()
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

        if callback not in self._price_callbacks:
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

        Only valid last-trade prices are forwarded; callbacks run as referenced tasks.
        """
        now = datetime.now(UTC)
        for ticker in tickers:
            price = _positive_decimal(ticker.last)
            if price is None:
                continue
            symbol = ticker.contract.symbol
            last = self._last_emitted.get(symbol)
            if last and last[0] == price and (now - last[1]).total_seconds() < _REEMIT_SECONDS:
                continue
            self._last_emitted[symbol] = (price, now)
            prev_close = _positive_decimal(ticker.close)
            update = PriceUpdate(
                ticker=symbol,
                price=price,
                bid=_positive_decimal(ticker.bid),
                ask=_positive_decimal(ticker.ask),
                day_change=(price - prev_close) if prev_close else None,
                day_change_pct=((price - prev_close) / prev_close) if prev_close else None,
                timestamp=now,
            )
            for cb in list(self._price_callbacks):
                spawn(cb(update), name=f"tick-{symbol}", registry=self._tasks)

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResult:
        """
        Submit an order to IBKR (SMART routing) and wait for it to settle.

        MKT/LMT: wait up to order_fill_timeout_seconds, then cancel a still-working
        order and wait briefly for the cancel. STP: wait only for acknowledgement.
        """
        self._require_connected()

        contract = Stock(order.symbol, "SMART", "USD")
        qty = float(order.quantity)
        if order.order_type == "STP":
            if order.stop_price is None:
                raise ValueError("STP order requires stop_price")
            ib_order = StopOrder(order.direction, qty, float(order.stop_price))
        elif order.order_type == "LMT" and order.limit_price is not None:
            ib_order = LimitOrder(order.direction, qty, float(order.limit_price))
        else:
            ib_order = MarketOrder(order.direction, qty)
        ib_order.orderRef = order.order_ref
        ib_order.tif = order.tif

        trade = self._ib.placeOrder(contract, ib_order)
        loop = asyncio.get_running_loop()

        if order.order_type == "STP":
            deadline = loop.time() + _STOP_ACK_SECONDS
            while (
                not trade.isDone()
                and trade.orderStatus.status not in ("PreSubmitted", "Submitted")
                and loop.time() < deadline
            ):
                await asyncio.sleep(0.1)
        else:
            deadline = loop.time() + self._settings.order_fill_timeout_seconds
            while not trade.isDone() and loop.time() < deadline:
                await asyncio.sleep(0.1)
            if not trade.isDone():
                self._ib.cancelOrder(ib_order)
                deadline = loop.time() + _CANCEL_WAIT_SECONDS
                while not trade.isDone() and loop.time() < deadline:
                    await asyncio.sleep(0.1)
            # Commission reports arrive just after the execution.
            deadline = loop.time() + _COMMISSION_WAIT_SECONDS
            while trade.fills and loop.time() < deadline and any(
                not f.commissionReport.commission for f in trade.fills
            ):
                await asyncio.sleep(0.1)

        return mapper.map_order_result(order.trade_id, trade)

    async def find_order_by_ref(self, order_ref: str) -> OrderResult | None:
        """State of the latest order with this orderRef (session trades, then open orders)."""
        self._require_connected()
        matches = [t for t in self._ib.trades() if t.order.orderRef == order_ref]
        if not matches:
            await self._ib.reqAllOpenOrdersAsync()
            matches = [t for t in self._ib.trades() if t.order.orderRef == order_ref]
        if matches:
            return mapper.map_order_result(mapper.trade_id_from_ref(order_ref), matches[-1])
        return await self.get_execution_summary(order_ref)

    async def get_execution_summary(self, order_ref: str) -> OrderResult | None:
        """Aggregate today's executions for this orderRef (IBKR returns the current day only)."""
        self._require_connected()
        fills = await self._ib.reqExecutionsAsync()
        return mapper.summarize_fills(order_ref, list(fills))

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

    async def get_intraday_bars(self, symbol: str) -> list[PriceBar]:
        """Today's completed 5-minute regular-hours bars; the still-forming bar is dropped."""
        self._require_connected()
        bars = await self._ib.reqHistoricalDataAsync(
            Stock(symbol, "SMART", "USD"),
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="5 mins",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,  # UTC datetimes; the default is exchange-local time that would be mislabelled as UTC
        )
        now = datetime.now(UTC)
        mapped = [mapper.map_price_bar(bar, "5 mins") for bar in bars]
        return [b for b in mapped if b.timestamp + timedelta(minutes=5) <= now]

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

        for attempt in range(2):
            for trade in self._ib.trades():
                if str(trade.order.orderId) == broker_order_id:
                    self._ib.cancelOrder(trade.order)
                    return True
            if attempt == 0:
                await self._ib.reqAllOpenOrdersAsync()
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
        development.
        """
        if (
            self._settings.environment == "development"
            and self._settings.ibkr_trading_mode == "live"
        ):
            raise LiveTradingGuardError(
                "Live trading guard triggered: ENVIRONMENT=development and "
                "IBKR_TRADING_MODE=live cannot be set simultaneously. "
                "Set IBKR_TRADING_MODE=paper or change ENVIRONMENT to proceed."
            )
