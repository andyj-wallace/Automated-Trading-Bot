"""
OrderManager — the only permitted code path for order submission and position closing.

submit_order() orchestrates the full execution pipeline:
  1. Compute aggregate portfolio risk + risk validation (RiskManager.validate)
  2. Create trade row at PENDING in DB (if session_factory provided)
     └─ Steps 1-2 run under an asyncio.Lock (see _submission_lock) so that
        "read aggregate risk → validate → reserve via PENDING row" is
        atomic across concurrent submit_order() calls — otherwise two
        trades submitted close together could each pass the portfolio risk
        cap independently, since neither would see the other's risk until
        far later (previously: only once it reached OPEN, after the full
        broker round-trip). Aggregate risk now also counts PENDING/SUBMITTED
        trades, not just OPEN — see TradeRepo.get_active_trades.
  3. Pre-submission audit entry → audit.log (before any broker call)
  4. Transition trade to SUBMITTED
  5. Broker order submission (market entry with order_ref "<trade_id>:entry")
  6. Post-confirmation audit entry → audit.log (after broker responds)
     └─ If this write fails: escalate to error.log — treated as system fault
  7. Record fill and go OPEN; WORKING stays SUBMITTED; REJECTED/ERROR goes CANCELLED
  8. Returns (ValidationResult, OrderResult) for the caller

close_position() handles the full exit flow (market hours only):
  1. Atomic OPEN -> CLOSING (stores intended exit_reason)
  2. Cancel protective stop; reuse any existing "<id>:exit" order
  3. Submit one market close order for the filled quantity
  4. finalize_close(): pnl from fill prices, atomic CLOSING -> CLOSED, circuit
     breaker, trade_closed event (only if this call won the CLOSED transition)

Audit entries are append-only. No path in this codebase may modify or delete
them after writing.

Depends on: RiskManager (6.3), BaseBroker (Layer 4), TradeRepo (3.7),
            RedisCache (5.1), audit_logger (Layer 2.2)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

from app.brokers.base import BaseBroker, OrderRequest, OrderResult
from app.core import market_calendar
from app.core.events import envelope, publish_system_alert
from app.core.risk.calculator import RiskCalculator
from app.core.risk.circuit_breaker import CircuitBreaker
from app.core.risk.costs import CostModel
from app.core.risk.growth import GrowthPolicy
from app.core.risk.manager import (
    RiskManager,
    RiskRejectionError,
    TradeRequest,
    ValidationResult,
)
from app.monitoring.logger import audit_logger, error_logger, risk_logger, trading_logger

_calculator = RiskCalculator()

# Redis channel for trade events — consumed by WebSocket handler and PositionMonitor
TRADE_EVENTS_CHANNEL = "trade_events"


class CloseOutcome(str, Enum):
    CLOSED = "CLOSED"
    PENDING_EXIT = "PENDING_EXIT"  # exit order is working at the broker
    FAILED = "FAILED"  # trade stays CLOSING; the reconciler retries
    NOT_OPEN = "NOT_OPEN"
    MARKET_CLOSED = "MARKET_CLOSED"


class OrderManager:
    """
    Orchestrates trade submission and position closing through validation,
    audit, DB lifecycle, and broker layers.

    Args:
        broker:          Broker implementation (IBKRClient or MockBroker).
        risk_manager:    Configured RiskManager instance.
        session_factory: Optional callable returning an async context-manager
                         that yields an AsyncSession. Required for DB status
                         tracking (PENDING → SUBMITTED → OPEN/CANCELLED).
                         If None, DB operations are skipped (e.g. unit tests).
        cache:           Optional RedisCache for publishing trade events from
                         close_position(). If None, event publishing is skipped.
        order_retry_attempts:        Max attempts for a broker.place_order() call
                                     (entry or close) before giving up. Only
                                     retries on exceptions raised by the broker
                                     call (e.g. transient network/timeout) — a
                                     returned OrderResult, even REJECTED, is a
                                     deliberate broker decision and is not retried.
        order_retry_backoff_seconds: Base delay between retries; doubles each
                                     attempt (0.5s, 1s, 2s, ...).
        circuit_breaker:             Optional shared CircuitBreaker. When provided,
                                     close_position() reports realized pnl to it
                                     so RiskManager's Gate 0 (daily halt / per-
                                     strategy kill switch) stays current. Must be
                                     the SAME instance passed into the RiskManager
                                     used by this OrderManager — see app/main.py.

    Usage:
        order_manager = OrderManager(broker, risk_manager, session_factory, cache)
        validation, result = await order_manager.submit_order(request)
        # trade_executed is published automatically on every fill
    """

    def __init__(
        self,
        broker: BaseBroker,
        risk_manager: RiskManager,
        session_factory=None,
        cache=None,
        order_retry_attempts: int = 3,
        order_retry_backoff_seconds: float = 0.5,
        circuit_breaker: CircuitBreaker | None = None,
        is_market_open: Callable[[], bool] = market_calendar.is_market_open,
        protective_stops: bool = False,
        stop_cancel_wait_seconds: float = 5.0,
        *,
        growth: GrowthPolicy | None = None,
        account_type: str | None = None,
        cost_model: CostModel | None = None,
    ) -> None:
        self._growth = growth
        self._account_type = account_type  # None skips the settled-cash / day-trade checks
        self._cost_model = cost_model
        self._broker = broker
        self._risk_manager = risk_manager
        self._session_factory = session_factory
        self._cache = cache
        self._order_retry_attempts = max(1, order_retry_attempts)
        self._order_retry_backoff_seconds = order_retry_backoff_seconds
        self._circuit_breaker = circuit_breaker
        self._is_market_open = is_market_open
        self._protective_stops = protective_stops
        self._stop_cancel_wait_seconds = stop_cancel_wait_seconds
        # Serializes "compute aggregate risk → validate → insert PENDING row"
        # across concurrent submit_order() calls in this process — see the
        # portfolio-risk-cap race note in the module docstring.
        self._submission_lock = asyncio.Lock()

    async def submit_order(
        self,
        request: TradeRequest,
    ) -> tuple[ValidationResult, OrderResult]:
        """
        Submit a trade through the full validation → audit → broker pipeline.

        Args:
            request: Trade parameters including current account balance.

        Returns:
            (ValidationResult, OrderResult). A filled order has already had its
            trade_executed event published (best effort).

        Raises:
            RiskRejectionError: If risk validation fails. No audit entries or DB rows
                are written for rejected trades.
            Exception: Re-raised if the post-confirmation audit write fails
                (system fault — escalated to error.log before re-raising).
        """
        # ------------------------------------------------------------------
        # Steps 1-2 — Aggregate risk → validate → create PENDING row, all
        # under one lock. This is the only section that must be atomic: once
        # the PENDING row is committed, its risk_amount is immediately visible
        # to the next call's aggregate-risk read (see TradeRepo.get_active_trades),
        # so the broker round-trip below does NOT need to hold the lock.
        # ------------------------------------------------------------------
        async with self._submission_lock:
            active_trades = await self._load_active_trades()
            symbol = request.symbol.upper()
            if any(t.symbol.upper() == symbol for t in active_trades):
                reason = f"SYMBOL_ALREADY_ACTIVE: {symbol} already has an active trade"
                risk_logger.warning(
                    "OrderManager: trade rejected, symbol already active",
                    extra={"trade_id": str(request.trade_id), "symbol": symbol},
                )
                raise RiskRejectionError(reason=reason, request=request)
            current_aggregate_risk = sum((t.risk_amount for t in active_trades), Decimal("0"))
            validation = self._risk_manager.validate(request, current_aggregate_risk)
            position_value = request.quantity * request.entry_price + sum(
                (
                    (t.filled_quantity or t.quantity) * (t.entry_fill_price or t.entry_price)
                    for t in active_trades
                ),
                Decimal("0"),
            )
            if position_value > request.account_balance:
                reason = (
                    f"NOTIONAL_LIMIT_EXCEEDED: open positions plus this trade would be worth "
                    f"{position_value}, more than the account ({request.account_balance}); "
                    "the bot never trades on margin"
                )
                risk_logger.warning(
                    "OrderManager: trade rejected, total position value above account value",
                    extra={"trade_id": str(request.trade_id), "symbol": symbol,
                           "position_value": str(position_value)},
                )
                raise RiskRejectionError(reason=reason, request=request)

            if (
                self._growth is not None
                and self._growth.is_active(request.account_balance)
                and len(active_trades) >= self._growth.max_open_positions
            ):
                self._reject_entry(
                    request, "GROWTH_MAX_POSITIONS",
                    f"growth mode allows {self._growth.max_open_positions} active trades; "
                    f"{len(active_trades)} already active",
                )
            if self._account_type is not None:
                await self._check_account_rules(request, active_trades)

            if self._session_factory:
                await self._create_pending(request, validation)

        # ------------------------------------------------------------------
        # Step 3 — Pre-submission audit entry (must be written before broker)
        # ------------------------------------------------------------------
        self._write_pre_submission(request, validation)

        # ------------------------------------------------------------------
        # Step 4 — Transition to SUBMITTED immediately before broker call
        # ------------------------------------------------------------------
        if self._session_factory:
            await self._transition(request.trade_id, "SUBMITTED")

        # ------------------------------------------------------------------
        # Step 5 — Submit to broker (entry is always a plain market order;
        # the protective stop is placed after the fill, see place_protective_stop)
        # ------------------------------------------------------------------
        broker_request = OrderRequest(
            trade_id=request.trade_id,
            symbol=request.symbol,
            direction=request.direction,
            quantity=request.quantity,
            order_type="MKT",
            order_ref=f"{request.trade_id}:entry",
            strategy_id=request.strategy_id,
        )

        order_result, broker_error = await self._place_order_with_retry(
            broker_request, context="entry order"
        )
        state_unknown = False
        if order_result is None:
            try:
                order_result = await self._find_existing_order(broker_request)
            except Exception:
                state_unknown = True
            if order_result is not None:
                broker_error = None
        if order_result is None:
            order_result = OrderResult(
                trade_id=request.trade_id,
                broker_order_id="",
                status="ERROR",
                filled_quantity=Decimal("0"),
                avg_fill_price=Decimal("0"),
                order_ref=broker_request.order_ref,
                error_message=str(broker_error),
                timestamp=datetime.now(UTC),
            )

        # ------------------------------------------------------------------
        # Step 6 — Post-confirmation audit entry
        # If this write fails it is a system fault: escalate to error.log
        # ------------------------------------------------------------------
        try:
            self._write_post_confirmation(request, validation, order_result)
        except Exception as audit_exc:
            error_logger.critical(
                "AUDIT FAILURE: post-confirmation entry could not be written",
                extra={
                    "trade_id": str(request.trade_id),
                    "symbol": request.symbol,
                    "broker_order_id": order_result.broker_order_id,
                    "order_status": order_result.status,
                    "audit_error": str(audit_exc),
                },
            )
            raise  # missing post-confirmation entry is a system fault

        # ------------------------------------------------------------------
        # Step 7 — Record the outcome. Nothing becomes OPEN without a real fill.
        # ------------------------------------------------------------------
        filled = order_result.status in ("FILLED", "PARTIAL") and order_result.filled_quantity > 0
        if self._session_factory:
            if filled:
                await self._record_entry_fill(request.trade_id, order_result)
                await self._transition(request.trade_id, "OPEN")
                await self._publish_trade_executed(request, validation, order_result)
                await self.place_protective_stop(
                    request.trade_id, force=request.submit_stop_to_broker
                )
            elif order_result.status == "WORKING":
                await self._record_entry_order(request.trade_id, order_result.broker_order_id)
                await publish_system_alert(
                    self._cache,
                    "ORDER_STUCK_WORKING",
                    f"Entry order for {request.symbol} is still working at the broker",
                    trade_id=str(request.trade_id),
                    symbol=request.symbol,
                    order_ref=broker_request.order_ref,
                )
            elif state_unknown:
                # The order may be live at the broker: stay SUBMITTED so the reconciler adopts or cancels it.
                await publish_system_alert(
                    self._cache,
                    "ORDER_STATE_UNKNOWN",
                    f"Entry order for {request.symbol} failed and the broker could not say whether "
                    "it arrived; the reconciler will resolve it",
                    trade_id=str(request.trade_id),
                    symbol=request.symbol,
                    order_ref=broker_request.order_ref,
                )
            else:
                await self._transition(request.trade_id, "CANCELLED")

        if broker_error:
            raise broker_error

        # ------------------------------------------------------------------
        # Step 8 — Record KPI snapshot (non-fatal if it fails)
        # ------------------------------------------------------------------
        if self._session_factory and self._broker and filled:
            try:
                from app.core.metrics.collector import MetricsCollector
                async with self._session_factory() as session:
                    await MetricsCollector().record_snapshot(session, self._broker)
            except Exception as exc:
                trading_logger.warning(
                    "OrderManager: MetricsCollector snapshot failed",
                    extra={"trade_id": str(request.trade_id), "error": str(exc)},
                )

        return validation, order_result

    async def _publish_trade_executed(
        self,
        request: TradeRequest,
        validation: ValidationResult,
        order_result: OrderResult,
    ) -> None:
        """Publish trade_executed for a fill; a failure here never fails the order."""
        if self._cache is None or self._session_factory is None:
            return
        try:
            from app.core.execution.trade_handler import TradeHandler

            async with self._session_factory() as session:
                await TradeHandler(self._cache).on_fill(request, validation, order_result, session)
        except Exception as exc:
            trading_logger.warning(
                "OrderManager: trade_executed publish failed",
                extra={"trade_id": str(request.trade_id), "error": str(exc)},
            )

    async def _load_active_trades(self) -> list:
        """
        Non-terminal trades (PENDING, SUBMITTED, OPEN, CLOSING), used for the
        one-active-trade-per-symbol check and the aggregate risk sum. Always
        called from inside self._submission_lock so the read and the subsequent
        PENDING-row insert are atomic with respect to any other concurrent
        submit_order() call in this process.

        Returns [] if no session_factory is configured (e.g. unit tests).
        """
        if not self._session_factory:
            return []

        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            return await TradeRepo(session).get_active_trades()

    async def _place_order_with_retry(
        self,
        broker_request: OrderRequest,
        *,
        context: str,
    ) -> tuple[OrderResult | None, BaseException | None]:
        """
        Call broker.place_order() with retry on transient failures.

        Retries only when the broker call *raises* (network blip, timeout,
        transient disconnect) — up to `_order_retry_attempts` times with
        doubling backoff. A clean returned OrderResult (including a broker
        REJECTED status) is not a transient failure and is returned as-is
        on the first attempt.

        Returns (order_result, None) on success, or (None, last_exception)
        if every attempt raised.
        """
        last_exc: BaseException | None = None
        for attempt in range(1, self._order_retry_attempts + 1):
            try:
                result = await self._broker.place_order(broker_request)
                return result, None
            except Exception as exc:
                last_exc = exc
                try:
                    existing = await self._find_existing_order(broker_request)
                except Exception as lookup_exc:
                    trading_logger.warning(
                        f"OrderManager: {context} failed and the broker lookup failed too; not resubmitting",
                        extra={
                            "trade_id": str(broker_request.trade_id),
                            "error": str(exc),
                            "lookup_error": str(lookup_exc),
                        },
                    )
                    return None, exc
                if existing is not None:
                    return existing, None
                if attempt < self._order_retry_attempts:
                    delay = self._order_retry_backoff_seconds * (2 ** (attempt - 1))
                    trading_logger.warning(
                        f"OrderManager: {context} place_order failed — retrying",
                        extra={
                            "trade_id": str(broker_request.trade_id),
                            "attempt": attempt,
                            "max_attempts": self._order_retry_attempts,
                            "retry_in_seconds": delay,
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(delay)
        return None, last_exc

    async def _find_existing_order(self, broker_request: OrderRequest) -> OrderResult | None:
        """The broker's record of this order_ref, None if it has none; raises if the lookup fails."""
        if not broker_request.order_ref:
            return None
        return await self._broker.find_order_by_ref(broker_request.order_ref)

    async def close_position(
        self,
        trade_id: uuid.UUID,
        reason: str,  # an ExitReason value: "STOP_LOSS" | "TAKE_PROFIT" | "MANUAL" | "STALE_TIMEOUT" | "EXTERNAL" | "TIME_EXIT"
        *,
        allow_from_closing: bool = False,
    ) -> CloseOutcome:
        """
        Close a position with one market order, at most once per trade.

        OPEN -> CLOSING (atomic, stores the intended exit_reason) -> order ref
        "<id>:exit" -> CLOSED only after a real fill. Raises ValueError for an
        unknown reason string. Never touches state while the market is closed.
        """
        if not self._session_factory:
            raise RuntimeError("close_position requires a session_factory")

        from app.db.models.trade import ExitReason, TradeDirection
        from app.db.repositories.trade_repo import TradeRepo

        exit_reason = ExitReason(reason)

        if not self._is_market_open():
            return CloseOutcome.MARKET_CLOSED

        async with self._session_factory() as session:
            trade = await TradeRepo(session).begin_close(
                trade_id, exit_reason, allow_from_closing=allow_from_closing
            )
            await session.commit()
        if trade is None:
            trading_logger.warning(
                "close_position: trade not found or not eligible to close",
                extra={"trade_id": str(trade_id), "exit_reason": reason},
            )
            return CloseOutcome.NOT_OPEN

        symbol = trade.symbol
        close_direction = "SELL" if trade.direction == TradeDirection.BUY else "BUY"
        total_quantity = trade.filled_quantity if trade.filled_quantity else trade.quantity
        stop_order_id = trade.stop_order_id
        exit_ref = f"{trade_id}:exit"

        async def fail(alert_type: str, message: str, **details) -> CloseOutcome:
            await publish_system_alert(
                self._cache,
                alert_type,
                message,
                trade_id=str(trade_id),
                symbol=symbol,
                exit_reason=reason,
                **details,
            )
            return CloseOutcome.FAILED

        # A protective stop must be gone (or found filled) before we sell.
        if stop_order_id:
            try:
                stop_fill = await self._cancel_protective_stop(trade_id, stop_order_id)
            except Exception as exc:
                return await fail(
                    "CLOSE_FAILED",
                    f"Could not cancel protective stop for {symbol}; exit not sent",
                    error=str(exc),
                )
            if stop_fill is not None:
                done = await self.finalize_close(
                    trade_id, ExitReason.STOP_LOSS, stop_fill, stop_fill.avg_fill_price
                )
                return CloseOutcome.CLOSED if done else CloseOutcome.FAILED

        try:
            existing = await self._broker.find_order_by_ref(exit_ref)
            sold_summary = await self._broker.get_execution_summary(exit_ref)
        except Exception as exc:
            return await fail(
                "CLOSE_FAILED",
                f"Could not query broker for existing exit of {symbol}; exit not sent",
                error=str(exc),
            )

        if existing is not None and existing.status == "WORKING":
            await self._record_exit_order(trade_id, existing.broker_order_id)
            return CloseOutcome.PENDING_EXIT

        sold = sold_summary.filled_quantity if sold_summary else Decimal("0")
        remaining = total_quantity - sold
        if remaining <= 0 and sold_summary is not None:
            done = await self.finalize_close(
                trade_id, exit_reason, sold_summary, sold_summary.avg_fill_price
            )
            return CloseOutcome.CLOSED if done else CloseOutcome.FAILED

        try:
            held = sum(
                (p.quantity for p in await self._broker.get_positions() if p.symbol.upper() == symbol.upper()),
                Decimal("0"),
            )
        except Exception as exc:
            return await fail(
                "CLOSE_FAILED",
                f"Could not read the broker position for {symbol}; exit not sent",
                error=str(exc),
            )
        if trade.direction != TradeDirection.BUY:
            held = -held
        if held <= 0:
            # IBKR reports today's fills only, so a stop that filled on an earlier day looks unfilled
            # here; selling would open a short. The reconciler books the exit from the broker instead.
            return await fail(
                "CLOSE_FAILED",
                f"Broker holds no {symbol}; nothing sold. The reconciler will book the exit.",
            )
        remaining = min(remaining, held)

        close_request = OrderRequest(
            trade_id=trade_id,
            symbol=symbol,
            direction=close_direction,
            quantity=remaining,
            order_type="MKT",
            order_ref=exit_ref,
        )
        order_result, broker_error = await self._place_order_with_retry(
            close_request, context="close order"
        )
        if order_result is None:
            return await fail(
                "CLOSE_FAILED",
                f"Exit order for {symbol} failed after all retries; trade left CLOSING",
                attempts=self._order_retry_attempts,
                error=str(broker_error),
            )

        if order_result.status == "FILLED":
            try:
                summary = await self._broker.get_execution_summary(exit_ref)
            except Exception:
                summary = None
            fill = summary or order_result
            done = await self.finalize_close(trade_id, exit_reason, fill, fill.avg_fill_price)
            return CloseOutcome.CLOSED if done else CloseOutcome.FAILED
        if order_result.status == "WORKING":
            await self._record_exit_order(trade_id, order_result.broker_order_id)
            return CloseOutcome.PENDING_EXIT
        if order_result.status == "PARTIAL":
            return await fail(
                "CLOSE_PARTIAL",
                f"Exit for {symbol} only partly filled; trade left CLOSING",
                filled_quantity=str(order_result.filled_quantity),
            )
        return await fail(
            "CLOSE_FAILED",
            f"Exit order for {symbol} was {order_result.status}; trade left CLOSING",
            error=order_result.error_message,
        )

    async def place_protective_stop(self, trade_id: uuid.UUID, *, force: bool = False) -> bool:
        """
        Rest a GTC stop order at the broker at the risk-approved stop price.

        Idempotent: does nothing unless the trade is OPEN with no stop recorded.
        The internal monitor stays the primary exit; this is the outage backstop.
        A failure alerts PROTECTIVE_STOP_FAILED and never raises.
        """
        if not (self._protective_stops or force) or not self._session_factory:
            return False

        from app.db.models.trade import TradeDirection, TradeStatus
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            trade = await TradeRepo(session).get_by_id(trade_id)
        if trade is None or trade.status != TradeStatus.OPEN or trade.stop_order_id:
            return False

        symbol = trade.symbol
        quantity = trade.filled_quantity if trade.filled_quantity else trade.quantity
        stop_ref = f"{trade_id}:stop"
        try:
            existing = await self._broker.find_order_by_ref(stop_ref)
            if existing is not None and existing.status == "WORKING":
                await self._record_stop_order(trade_id, existing.broker_order_id, trade.stop_loss_price)
                return True
            request = OrderRequest(
                trade_id=trade_id,
                symbol=symbol,
                direction="SELL" if trade.direction == TradeDirection.BUY else "BUY",
                quantity=quantity,
                order_type="STP",
                stop_price=trade.stop_loss_price,
                tif="GTC",
                order_ref=stop_ref,
            )
            result, error = await self._place_order_with_retry(request, context="protective stop")
        except Exception as exc:
            result, error = None, exc

        if result is None or result.status in ("REJECTED", "ERROR"):
            await publish_system_alert(
                self._cache,
                "PROTECTIVE_STOP_FAILED",
                f"Broker stop for {symbol} could not be placed; internal monitor still protects it",
                trade_id=str(trade_id),
                symbol=symbol,
                error=str(error) if error else (result.error_message if result else None),
            )
            return False
        await self._record_stop_order(trade_id, result.broker_order_id, trade.stop_loss_price)
        return True

    async def _cancel_protective_stop(
        self, trade_id: uuid.UUID, stop_order_id: str
    ) -> OrderResult | None:
        """
        Cancel the resting stop and wait for it to settle.

        Returns the stop's fill summary if it executed (so the caller must not sell
        again), None once it is gone. Raises if it is still working after the wait.
        """
        stop_ref = f"{trade_id}:stop"
        await self._broker.cancel_order(stop_order_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stop_cancel_wait_seconds
        while True:
            state = await self._broker.find_order_by_ref(stop_ref)
            if state is None or state.status != "WORKING":
                break
            if loop.time() >= deadline:
                raise RuntimeError("protective stop still working after cancel")
            await asyncio.sleep(0.25)
        fill = await self._broker.get_execution_summary(stop_ref)
        if fill is not None and fill.filled_quantity > 0:
            return fill
        return None

    async def adopt_entry_fill(self, trade_id: uuid.UUID, fill: OrderResult) -> bool:
        """
        Move a PENDING/SUBMITTED trade to OPEN from a fill found at the broker
        (reconciler path): atomic, publishes trade_executed, places the protective stop.
        Returns False if the trade had already moved on.
        """
        from app.core.execution.trade_handler import TradeHandler
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            trade = await TradeRepo(session).open_from_fill(
                trade_id, fill.broker_order_id, fill.avg_fill_price, fill.filled_quantity,
                commission=fill.commission,
            )
            await session.commit()
        if trade is None:
            return False
        trading_logger.info(
            "Entry fill adopted from broker",
            extra={
                "trade_id": str(trade_id),
                "symbol": trade.symbol,
                "filled_quantity": str(fill.filled_quantity),
            },
        )
        if self._cache is not None:
            try:
                await TradeHandler(self._cache).publish_for_trade(trade, fill)
            except Exception as exc:
                trading_logger.warning(
                    "OrderManager: trade_executed publish failed",
                    extra={"trade_id": str(trade_id), "error": str(exc)},
                )
        await self.place_protective_stop(trade_id)
        return True

    async def finalize_close(
        self,
        trade_id: uuid.UUID,
        exit_reason,
        fill: OrderResult | None,
        fallback_price: Decimal,
    ) -> bool:
        """
        Record a confirmed exit: PnL, CLOSED row, circuit breaker, event, snapshot.

        Never places an order and ignores market hours. Returns True only if this
        call moved the trade CLOSING -> CLOSED, so PnL is recorded exactly once.
        """
        if not self._session_factory:
            raise RuntimeError("finalize_close requires a session_factory")

        from app.db.models.trade import TradeDirection, TradeStatus
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            repo = TradeRepo(session)
            trade = await repo.get_by_id(trade_id)
            if trade is None:
                error_logger.error(
                    "finalize_close: trade not found", extra={"trade_id": str(trade_id)}
                )
                return False
            if trade.status == TradeStatus.OPEN:
                trade = await repo.begin_close(trade_id, exit_reason)
                if trade is None:
                    await session.commit()
                    return False
            elif trade.status != TradeStatus.CLOSING:
                return False

            if fill is not None:
                shares, exit_price = fill.filled_quantity, fill.avg_fill_price
            else:
                shares = trade.filled_quantity if trade.filled_quantity else trade.quantity
                exit_price = fallback_price
            entry = trade.entry_fill_price if trade.entry_fill_price else trade.entry_price
            exit_commission = fill.commission if fill is not None else Decimal("0")
            fees = (trade.entry_commission or Decimal("0")) + exit_commission
            if trade.direction == TradeDirection.BUY:
                pnl = (exit_price - entry) * shares - fees
            else:
                pnl = (entry - exit_price) * shares - fees

            closed = await repo.finish_close(
                trade_id,
                exit_price,
                pnl,
                fill.broker_order_id if fill else None,
                exit_reason=exit_reason,
                exit_commission=exit_commission,
            )
            await session.commit()
        if closed is None:
            return False

        if self._circuit_breaker is not None:
            events = self._circuit_breaker.record_closed_trade(
                pnl=pnl,
                account_balance=closed.account_balance_at_entry,
                strategy_id=closed.strategy_id,
            )
            await self._handle_breaker_events(events, closed.strategy_id)

        trading_logger.info(
            "Position closed",
            extra={
                "trade_id": str(trade_id),
                "symbol": closed.symbol,
                "reason": exit_reason.value,
                "exit_price": str(exit_price),
                "pnl": str(pnl),
            },
        )
        if self._cache:
            try:
                await self._cache.publish(
                    TRADE_EVENTS_CHANNEL,
                    envelope(
                        "trade_closed",
                        {
                            "trade_id": str(trade_id),
                            "symbol": closed.symbol,
                            "reason": exit_reason.value,
                            "exit_price": str(exit_price),
                            "pnl": str(pnl),
                        },
                    ),
                )
            except Exception as exc:
                trading_logger.warning(
                    "OrderManager: trade_closed publish failed",
                    extra={"trade_id": str(trade_id), "error": str(exc)},
                )

        try:
            from app.core.metrics.collector import MetricsCollector

            async with self._session_factory() as session:
                await MetricsCollector().record_snapshot(session, self._broker)
        except Exception as exc:
            trading_logger.warning(
                "OrderManager: MetricsCollector snapshot failed on close",
                extra={"trade_id": str(trade_id), "error": str(exc)},
            )
        return True

    async def _handle_breaker_events(self, events: list[str], strategy_id: uuid.UUID | None) -> None:
        """Alert on a new daily halt; durably disable a strategy the breaker just killed."""
        for event in events:
            if event == "DAILY_HALT":
                await publish_system_alert(
                    self._cache,
                    "CIRCUIT_BREAKER_HALT",
                    "Daily loss limit reached: new trades are halted for the rest of the trading day",
                )
            elif event == "STRATEGY_KILLED" and strategy_id is not None:
                try:
                    from app.db.repositories.strategy_repo import StrategyRepo

                    async with self._session_factory() as session:
                        await StrategyRepo(session).set_enabled(strategy_id, False)
                        await session.commit()
                except Exception as exc:
                    error_logger.error(
                        "OrderManager: could not disable killed strategy",
                        extra={"strategy_id": str(strategy_id), "error": str(exc)},
                    )
                await publish_system_alert(
                    self._cache,
                    "STRATEGY_AUTO_DISABLED",
                    "Strategy disabled after too many consecutive losses",
                    strategy_id=str(strategy_id),
                )

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _create_pending(
        self,
        request: TradeRequest,
        validation: ValidationResult,
    ) -> None:
        """Create a PENDING trade row in the DB before the broker call."""
        from app.db.models.trade import TradeDirection
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            repo = TradeRepo(session)
            await repo.create(
                trade_id=request.trade_id,
                symbol=request.symbol,
                direction=TradeDirection(request.direction),
                quantity=request.quantity,
                entry_price=request.entry_price,
                stop_loss_price=request.stop_loss_price,  # type: ignore[arg-type]
                take_profit_price=validation.take_profit_price,
                reward_to_risk_ratio=validation.reward_to_risk_ratio,
                risk_amount=validation.risk_amount,
                account_balance_at_entry=validation.account_balance_at_entry,
                strategy_id=request.strategy_id,
                exit_deadline=request.exit_deadline,
            )
            await session.commit()

    async def _check_account_rules(self, request: TradeRequest, active_trades: list) -> None:
        """Cash accounts pay only with settled cash; margin accounts keep a day trade in reserve."""
        from app.db.models.trade import TradeStatus

        try:
            summary = await self._broker.get_account_summary()
        except Exception as exc:
            self._reject_entry(request, "ACCOUNT_UNAVAILABLE", f"could not read the account: {exc}")
        if self._account_type == "margin":
            if summary.day_trades_remaining == 0:  # -1 means unlimited
                self._reject_entry(
                    request, "NO_DAY_TRADES_LEFT",
                    "IBKR reports 0 day trades left; a same-day stop-out would flag the account",
                )
            return
        settled = summary.settled_cash if summary.settled_cash is not None else summary.cash_balance
        # IBKR refreshes account values about every 3 minutes, so reserve orders it may not have counted yet.
        cutoff = datetime.now(UTC) - timedelta(minutes=5)
        reserved = sum(
            (
                (t.filled_quantity or t.quantity) * (t.entry_fill_price or t.entry_price)
                for t in active_trades
                if t.status in (TradeStatus.PENDING, TradeStatus.SUBMITTED) or t.executed_at >= cutoff
            ),
            Decimal("0"),
        )
        fee = self._cost_model.commission(request.quantity, request.entry_price) if self._cost_model else Decimal("0")
        needed = request.quantity * request.entry_price + fee
        if needed > settled - reserved:
            self._reject_entry(
                request, "INSUFFICIENT_SETTLED_CASH",
                f"needs {needed:.2f}; settled cash {settled:.2f} minus {reserved:.2f} reserved",
            )

    @staticmethod
    def _reject_entry(request: TradeRequest, code: str, detail: str) -> None:
        risk_logger.warning(
            "OrderManager: entry rejected",
            extra={"trade_id": str(request.trade_id), "symbol": request.symbol, "reason": code, "detail": detail},
        )
        raise RiskRejectionError(reason=f"{code}: {detail}", request=request)

    async def _record_entry_fill(self, trade_id: uuid.UUID, result: OrderResult) -> None:
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            await TradeRepo(session).record_entry_fill(
                trade_id, result.broker_order_id, result.avg_fill_price, result.filled_quantity,
                commission=result.commission,
            )
            await session.commit()

    async def _record_exit_order(self, trade_id: uuid.UUID, order_id: str) -> None:
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            await TradeRepo(session).set_exit_order(trade_id, order_id)
            await session.commit()

    async def _record_stop_order(
        self, trade_id: uuid.UUID, order_id: str, stop_price: Decimal
    ) -> None:
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            await TradeRepo(session).set_stop_order(trade_id, order_id, stop_price)
            await session.commit()

    async def _record_entry_order(self, trade_id: uuid.UUID, order_id: str) -> None:
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            await TradeRepo(session).set_entry_order(trade_id, order_id)
            await session.commit()

    async def _transition(self, trade_id: uuid.UUID, new_status: str) -> None:
        """Transition trade status, logging CRITICAL if trade is missing."""
        from app.db.models.trade import TradeStatus
        from app.db.repositories.trade_repo import TradeRepo

        status = TradeStatus(new_status)
        async with self._session_factory() as session:
            repo = TradeRepo(session)
            trade = await repo.update_status(trade_id, status)
            if trade is None:
                error_logger.critical(
                    "OrderManager: status transition failed — trade not found",
                    extra={"trade_id": str(trade_id), "requested_status": new_status},
                )
            await session.commit()

    # ------------------------------------------------------------------
    # Audit helpers — private, only called within submit_order()
    # ------------------------------------------------------------------

    @staticmethod
    def _write_pre_submission(
        request: TradeRequest,
        validation: ValidationResult,
    ) -> None:
        """Write the mandatory pre-submission audit entry to audit.log."""
        audit_logger.info(
            "PRE_SUBMISSION",
            extra={
                "event": "PRE_SUBMISSION",
                "trade_id": str(request.trade_id),
                "symbol": request.symbol,
                "direction": request.direction,
                "quantity": str(request.quantity),
                "entry_price": str(request.entry_price),
                "stop_loss_price": str(request.stop_loss_price),
                "take_profit_price": str(validation.take_profit_price),
                "reward_to_risk_ratio": str(validation.reward_to_risk_ratio),
                "submit_stop_to_broker": request.submit_stop_to_broker,
                "risk_amount": str(validation.risk_amount),
                "account_balance_at_entry": str(validation.account_balance_at_entry),
                "strategy_id": str(request.strategy_id) if request.strategy_id else None,
                "risk_validation": "APPROVED",
            },
        )
        trading_logger.info(
            "Order pre-submission",
            extra={
                "trade_id": str(request.trade_id),
                "symbol": request.symbol,
                "direction": request.direction,
                "quantity": str(request.quantity),
                "entry_price": str(request.entry_price),
            },
        )

    @staticmethod
    def _write_post_confirmation(
        request: TradeRequest,
        validation: ValidationResult,
        order_result: OrderResult,
    ) -> None:
        """Write the mandatory post-confirmation audit entry to audit.log."""
        # Recalculate risk based on actual fill price
        final_risk_amount = Decimal("0")
        if (
            order_result.avg_fill_price > 0
            and order_result.filled_quantity > 0
            and request.stop_loss_price is not None
        ):
            try:
                final_risk_amount = _calculator.risk_amount(
                    quantity=order_result.filled_quantity,
                    entry_price=order_result.avg_fill_price,
                    stop_loss_price=request.stop_loss_price,
                )
            except Exception:
                final_risk_amount = validation.risk_amount

        audit_logger.info(
            "POST_CONFIRMATION",
            extra={
                "event": "POST_CONFIRMATION",
                "trade_id": str(request.trade_id),
                "broker_order_id": order_result.broker_order_id,
                "status": order_result.status,
                "filled_quantity": str(order_result.filled_quantity),
                "avg_fill_price": str(order_result.avg_fill_price),
                "final_risk_amount": str(final_risk_amount),
                "error_message": order_result.error_message,
            },
        )
        trading_logger.info(
            "Order post-confirmation",
            extra={
                "trade_id": str(request.trade_id),
                "broker_order_id": order_result.broker_order_id,
                "status": order_result.status,
                "filled_quantity": str(order_result.filled_quantity),
                "avg_fill_price": str(order_result.avg_fill_price),
            },
        )
