"""
OrderManager — the only permitted code path for order submission.

submit_order() orchestrates the full execution pipeline:
  1. Risk validation (RiskManager.validate)
  2. Pre-submission audit entry → audit.log (before any broker call)
  3. Broker order submission
  4. Post-confirmation audit entry → audit.log (after broker responds)
     └─ If this write fails: escalate to error.log — treated as system fault
  5. Returns (ValidationResult, OrderResult) for the caller to persist

Audit entries are append-only. No path in this codebase may modify or delete
them after writing. See design.md § Audit Trail.

Depends on: RiskManager (Layer 6.3), BaseBroker (Layer 4), audit_logger (Layer 2.2)
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.brokers.base import BaseBroker, OrderRequest, OrderResult
from app.core.risk.calculator import RiskCalculator
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest, ValidationResult
from app.monitoring.logger import audit_logger, error_logger, trading_logger

_calculator = RiskCalculator()


class OrderManager:
    """
    Orchestrates trade submission through validation, audit, and broker layers.

    Instantiate with injected broker and risk manager:
        order_manager = OrderManager(broker, risk_manager)
        validation, result = await order_manager.submit_order(request)

    The caller is responsible for persisting the trade (TradeHandler.on_fill).
    """

    def __init__(self, broker: BaseBroker, risk_manager: RiskManager) -> None:
        self._broker = broker
        self._risk_manager = risk_manager

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
            RiskRejectionError: If risk validation fails. No audit entries are
                written for rejected trades (rejection is logged to risk.log
                by RiskManager itself).
            Exception: Re-raised if the post-confirmation audit write fails
                (system fault — escalated to error.log before re-raising).
        """
        # ------------------------------------------------------------------
        # Step 1 — Risk validation (raises RiskRejectionError on failure)
        # ------------------------------------------------------------------
        validation = self._risk_manager.validate(request)

        # ------------------------------------------------------------------
        # Step 2 — Pre-submission audit entry (must be written before broker)
        # ------------------------------------------------------------------
        self._write_pre_submission(request, validation)

        # ------------------------------------------------------------------
        # Step 3 — Submit to broker, capturing any broker-level errors
        # ------------------------------------------------------------------
        broker_request = OrderRequest(
            trade_id=request.trade_id,
            symbol=request.symbol,
            direction=request.direction,
            quantity=request.quantity,
            order_type="MKT",
            stop_loss_price=request.stop_loss_price,  # type: ignore[arg-type]
            strategy_id=request.strategy_id,
        )

        broker_error: BaseException | None = None
        order_result: OrderResult

        try:
            order_result = await self._broker.place_order(broker_request)
        except Exception as exc:
            broker_error = exc
            order_result = OrderResult(
                trade_id=request.trade_id,
                broker_order_id="",
                status="ERROR",
                filled_quantity=Decimal("0"),
                avg_fill_price=Decimal("0"),
                error_message=str(exc),
                timestamp=datetime.now(timezone.utc),
            )

        # ------------------------------------------------------------------
        # Step 4 — Post-confirmation audit entry
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

        if broker_error:
            raise broker_error

        return validation, order_result

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
