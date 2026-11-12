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
from datetime import UTC as UTC
from datetime import datetime as datetime
from datetime import timedelta as timedelta
from decimal import Decimal
from enum import Enum as Enum

from app.brokers.base import BaseBroker, OrderResult
from app.brokers.base import OrderRequest as OrderRequest
from app.core import market_calendar
from app.core.events import envelope as envelope
from app.core.events import publish_system_alert as publish_system_alert
from app.core.execution.order_close import (
    TRADE_EVENTS_CHANNEL as TRADE_EVENTS_CHANNEL,
)
from app.core.execution.order_close import (
    CloseOutcome as CloseOutcome,
)
from app.core.execution.order_close import (
    OrderCloseMixin,
)
from app.core.execution.order_entry import OrderEntryMixin
from app.core.execution.order_records import OrderRecordsMixin
from app.core.execution.order_stops import ProtectiveStopMixin
from app.core.risk.calculator import RiskCalculator
from app.core.risk.circuit_breaker import CircuitBreaker
from app.core.risk.costs import CostModel
from app.core.risk.growth import GrowthPolicy
from app.core.risk.manager import (
    RiskManager,
    TradeRequest,
    ValidationResult,
)
from app.core.risk.manager import RiskRejectionError as RiskRejectionError
from app.monitoring.logger import audit_logger, error_logger, trading_logger
from app.monitoring.logger import risk_logger as risk_logger

_calculator = RiskCalculator()


class OrderManager(OrderEntryMixin, OrderCloseMixin, ProtectiveStopMixin, OrderRecordsMixin):
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
        # Counts submit_order() calls from entering the lock until they return or raise.
        self._entries_in_flight = 0
        self._entries_idle = asyncio.Event()
        self._entries_idle.set()

    def _entry_done(self) -> None:
        """Release one in-flight submit_order() count; wake waiters at zero."""
        self._entries_in_flight -= 1
        if self._entries_in_flight == 0:
            self._entries_idle.set()

    async def wait_for_entries_idle(self, timeout: float) -> bool:
        """Wait until no submit_order call is in flight; False if timeout passes first."""
        if self._entries_in_flight == 0:
            return True
        try:
            await asyncio.wait_for(self._entries_idle.wait(), timeout)
        except TimeoutError:
            return False
        return True

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
            self._entries_in_flight += 1
            self._entries_idle.clear()
            try:
                validation = await self._validate_entry(request)
                if self._session_factory:
                    await self._create_pending(request, validation)
            except BaseException:
                self._entry_done()
                raise

        try:
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
            broker_request, order_result, broker_error, state_unknown = (
                await self._submit_entry(request)
            )

            # ------------------------------------------------------------------
            # Step 6 — Post-confirmation audit entry
            # If this write fails it is a system fault: escalate to error.log
            # ------------------------------------------------------------------
            self._audit_post_confirmation(request, validation, order_result)

            # ------------------------------------------------------------------
            # Step 7 — Record the outcome. Nothing becomes OPEN without a real fill.
            # ------------------------------------------------------------------
            filled = await self._record_entry_outcome(
                request, validation, order_result, broker_request, state_unknown
            )
            if broker_error:
                raise broker_error

            # ------------------------------------------------------------------
            # Step 8 — Record KPI snapshot (non-fatal if it fails)
            # ------------------------------------------------------------------
            await self._snapshot_after_entry(request, filled)

            return validation, order_result
        finally:
            self._entry_done()

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

    def _audit_post_confirmation(
        self,
        request: TradeRequest,
        validation: ValidationResult,
        order_result: OrderResult,
    ) -> None:
        """Write the post-confirmation audit entry; on failure escalate to error.log and re-raise."""
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
