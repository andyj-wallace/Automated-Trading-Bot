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
  5. Broker order submission (stop sent only when submit_stop_to_broker=True)
  6. Post-confirmation audit entry → audit.log (after broker responds)
     └─ If this write fails: escalate to error.log — treated as system fault
  7. Transition trade to OPEN (fill) or CANCELLED (rejection/error)
  8. Returns (ValidationResult, OrderResult) for the caller

close_position() handles the full exit flow:
  1. Load trade from DB; validate it is OPEN
  2. Transition to CLOSING
  3. Submit market close order to broker
  4. Report realized pnl to the circuit breaker (if configured)
  5. Transition to CLOSED; write exit_price, exit_reason, pnl
  6. Publish trade_closed event to Redis

Audit entries are append-only. No path in this codebase may modify or delete
them after writing. See design.md § Audit Trail.

Depends on: RiskManager (6.3), BaseBroker (Layer 4), TradeRepo (3.7),
            RedisCache (5.1), audit_logger (Layer 2.2)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.base import BaseBroker, OrderRequest, OrderResult
from app.core.risk.calculator import RiskCalculator
from app.core.risk.circuit_breaker import CircuitBreaker
from app.core.risk.manager import RiskManager, TradeRequest, ValidationResult
from app.monitoring.logger import audit_logger, error_logger, trading_logger

_calculator = RiskCalculator()

# Redis channel for trade events — consumed by WebSocket handler and PositionMonitor
TRADE_EVENTS_CHANNEL = "trade_events"


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
        # Then call TradeHandler.on_fill() for event publishing
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
    ) -> None:
        self._broker = broker
        self._risk_manager = risk_manager
        self._session_factory = session_factory
        self._cache = cache
        self._order_retry_attempts = max(1, order_retry_attempts)
        self._order_retry_backoff_seconds = order_retry_backoff_seconds
        self._circuit_breaker = circuit_breaker
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
            (ValidationResult, OrderResult) — pass both to TradeHandler.on_fill()
            if the order filled successfully.

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
            current_aggregate_risk = await self._compute_aggregate_risk()
            validation = self._risk_manager.validate(request, current_aggregate_risk)

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
        # Step 5 — Submit to broker
        # stop_loss_price is only forwarded when the strategy opted in
        # ------------------------------------------------------------------
        broker_stop = (
            request.stop_loss_price if request.submit_stop_to_broker else None
        )
        broker_request = OrderRequest(
            trade_id=request.trade_id,
            symbol=request.symbol,
            direction=request.direction,
            quantity=request.quantity,
            order_type="MKT",
            stop_loss_price=broker_stop,
            strategy_id=request.strategy_id,
        )

        order_result, broker_error = await self._place_order_with_retry(
            broker_request, context="entry order"
        )
        if order_result is None:
            order_result = OrderResult(
                trade_id=request.trade_id,
                broker_order_id="",
                status="ERROR",
                filled_quantity=Decimal("0"),
                avg_fill_price=Decimal("0"),
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
        # Step 7 — Transition to final status (OPEN or CANCELLED)
        # ------------------------------------------------------------------
        if self._session_factory:
            if order_result.status in ("FILLED", "PARTIAL"):
                await self._transition(request.trade_id, "OPEN")
            else:
                await self._transition(request.trade_id, "CANCELLED")

        if broker_error:
            raise broker_error

        # ------------------------------------------------------------------
        # Step 8 — Record KPI snapshot (non-fatal if it fails)
        # ------------------------------------------------------------------
        if self._session_factory and self._broker and order_result.status in ("FILLED", "PARTIAL"):
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

    async def _compute_aggregate_risk(self) -> Decimal:
        """
        Sum risk_amount across all non-terminal trades (PENDING, SUBMITTED,
        OPEN). Always called from inside self._submission_lock so the read
        and the subsequent PENDING-row insert are atomic with respect to any
        other concurrent submit_order() call in this process.

        Returns 0 if no session_factory is configured (e.g. unit tests).
        """
        if not self._session_factory:
            return Decimal("0")

        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            repo = TradeRepo(session)
            active = await repo.get_active_trades()
            return sum((t.risk_amount for t in active), Decimal("0"))

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

    async def close_position(
        self,
        trade_id: uuid.UUID,
        reason: str,  # "STOP_LOSS" | "TAKE_PROFIT" | "MANUAL"
    ) -> None:
        """
        Close an open position by submitting a market close order to the broker.

        Transitions the trade: OPEN → CLOSING → CLOSED (or logs error and
        stays at CLOSING if the broker rejects the close order).

        Args:
            trade_id: UUID of the trade to close.
            reason:   Exit reason — "STOP_LOSS", "TAKE_PROFIT", or "MANUAL".
        """
        if not self._session_factory:
            raise RuntimeError("close_position requires a session_factory")

        from app.db.models.trade import ExitReason, TradeDirection, TradeStatus
        from app.db.repositories.trade_repo import TradeRepo

        # ------------------------------------------------------------------
        # Load trade and validate current status
        # ------------------------------------------------------------------
        async with self._session_factory() as session:
            repo = TradeRepo(session)
            trade = await repo.get_by_id(trade_id)

            if trade is None:
                error_logger.error(
                    "close_position: trade not found",
                    extra={"trade_id": str(trade_id), "reason": reason},
                )
                return

            if trade.status != TradeStatus.OPEN:
                error_logger.critical(
                    "close_position: unexpected status — expected OPEN",
                    extra={
                        "trade_id": str(trade_id),
                        "current_status": trade.status.value,
                        "reason": reason,
                    },
                )
                return

            # Snapshot fields needed for the close order and PnL calculation
            symbol = trade.symbol
            close_direction = "SELL" if trade.direction == TradeDirection.BUY else "BUY"
            quantity = trade.quantity
            entry_price = trade.entry_price
            strategy_id = trade.strategy_id
            account_balance_at_entry = trade.account_balance_at_entry

            # Transition to CLOSING
            await repo.update_status(trade_id, TradeStatus.CLOSING)
            await session.commit()

        # ------------------------------------------------------------------
        # Submit close order to broker (market order, no stop)
        # ------------------------------------------------------------------
        close_request = OrderRequest(
            trade_id=trade_id,
            symbol=symbol,
            direction=close_direction,
            quantity=quantity,
            order_type="MKT",
            stop_loss_price=None,
        )

        order_result, broker_error = await self._place_order_with_retry(
            close_request, context="close order"
        )
        if order_result is None:
            error_logger.critical(
                "close_position: broker close order failed after all retries — "
                "position remains OPEN at the broker, stop/target is no longer "
                "being enforced for this trade",
                extra={
                    "trade_id": str(trade_id),
                    "reason": reason,
                    "attempts": self._order_retry_attempts,
                    "error": str(broker_error),
                },
            )
            return

        # ------------------------------------------------------------------
        # Record exit and transition to CLOSED
        # ------------------------------------------------------------------
        if order_result.status in ("FILLED", "PARTIAL"):
            fill_price = order_result.avg_fill_price

            # PnL: positive when trade moved in our favour
            if close_direction == "SELL":  # was a BUY trade
                pnl = (fill_price - entry_price) * order_result.filled_quantity
            else:  # was a SELL (short) trade
                pnl = (entry_price - fill_price) * order_result.filled_quantity

            try:
                exit_reason = ExitReason(reason)
            except ValueError:
                exit_reason = ExitReason.MANUAL

            # Feed realized pnl to the circuit breaker before anything else
            # can short-circuit this method (DB write, event publish, etc.)
            # so a halt/kill triggered by this trade applies to the very
            # next validate() call, not just the one after that.
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_closed_trade(
                    pnl=pnl,
                    account_balance=account_balance_at_entry,
                    strategy_id=strategy_id,
                )

            async with self._session_factory() as session:
                repo = TradeRepo(session)
                await repo.close_trade(
                    trade_id=trade_id,
                    exit_price=fill_price,
                    pnl=pnl,
                    exit_reason=exit_reason,
                )
                await session.commit()

            trading_logger.info(
                "Position closed",
                extra={
                    "trade_id": str(trade_id),
                    "symbol": symbol,
                    "reason": reason,
                    "exit_price": str(fill_price),
                    "pnl": str(pnl),
                },
            )

            # Publish trade_closed event for PositionMonitor and WebSocket consumers
            if self._cache:
                event = {
                    "event": "trade_closed",
                    "payload": {
                        "trade_id": str(trade_id),
                        "symbol": symbol,
                        "reason": reason,
                        "exit_price": str(fill_price),
                        "pnl": str(pnl),
                    },
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                await self._cache.publish(TRADE_EVENTS_CHANNEL, json.dumps(event))

            # Record KPI snapshot after close (non-fatal)
            try:
                from app.core.metrics.collector import MetricsCollector
                async with self._session_factory() as session:
                    await MetricsCollector().record_snapshot(session, self._broker)
            except Exception as exc:
                trading_logger.warning(
                    "OrderManager: MetricsCollector snapshot failed on close",
                    extra={"trade_id": str(trade_id), "error": str(exc)},
                )

        else:
            error_logger.error(
                "close_position: broker rejected close order — trade left at CLOSING",
                extra={
                    "trade_id": str(trade_id),
                    "broker_status": order_result.status,
                    "error": order_result.error_message,
                },
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
            )
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
        # Recalculate risk based on actual fill price per design.md spec
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
