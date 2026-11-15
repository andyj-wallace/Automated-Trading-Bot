"""
IBKRClient — wraps ib_async to implement BaseBroker for Interactive Brokers.

Connects via TCP socket to a locally running IB Gateway process. The Gateway
holds the authenticated IBKR session; this client never handles credentials.

See DEPLOYMENT.md for running the Gateway (Docker, auto-login).
"""

import asyncio
import math as math
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC as UTC
from datetime import datetime
from datetime import timedelta as timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder

from app.brokers.base import (
    AccountSummary,
    BaseBroker,
    Execution,
    OrderRequest,
    OrderResult,
    Position,
    PriceFeedCallback,
)
from app.brokers.base import (
    BrokerConnectionInfo as BrokerConnectionInfo,
)
from app.brokers.base import (
    PriceBar as PriceBar,
)
from app.brokers.base import (
    PriceUpdate as PriceUpdate,
)
from app.brokers.ibkr import mapper
from app.brokers.ibkr.connection_status import IBKRConnectionStatusMixin
from app.brokers.ibkr.hints import login_hint as login_hint
from app.brokers.ibkr.market_data import _REEMIT_SECONDS as _REEMIT_SECONDS
from app.brokers.ibkr.market_data import IBKRMarketDataMixin
from app.brokers.ibkr.market_data import _positive_decimal as _positive_decimal
from app.config import Settings
from app.core import market_calendar
from app.core.events import publish_system_alert
from app.core.tasks import spawn as spawn
from app.monitoring.logger import system_logger

if TYPE_CHECKING:
    from app.data.cache import RedisCache

# How long (seconds) to wait for an order fill before returning with
# whatever status we have.  Market orders on paper typically fill in <1s;
# this guard prevents an infinite wait on edge cases.
_CANCEL_WAIT_SECONDS = 10
_COMMISSION_WAIT_SECONDS = 2
_STOP_ACK_SECONDS = 10

_DEFAULT_BACKOFF = (5, 10, 20, 40, 60)


class LiveTradingGuardError(RuntimeError):
    """Raised when the configured or actual account mode conflicts with the environment."""


class IBKRClient(IBKRMarketDataMixin, IBKRConnectionStatusMixin, BaseBroker):
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

    async def get_executions(self) -> list[Execution]:
        """Today's executions (fills) for this session's account, one Execution per fill."""
        self._require_connected()
        fills = await self._ib.reqExecutionsAsync()
        return mapper.map_executions(list(fills))

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
