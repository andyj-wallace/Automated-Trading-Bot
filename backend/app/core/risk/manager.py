"""
RiskManager — hard gate that every trade must pass before reaching the broker.

validate() is the sole entry point. It:
  1. Rejects immediately if stop_loss_price is missing
  2. Computes the actual risk amount (quantity × stop distance)
  3. Rejects if risk_amount > 1% of account_balance
  4. Logs all rejections to risk.log with full context
  5. Returns a ValidationResult on approval

This module performs no I/O and no DB calls. The caller (OrderManager, Layer 7)
is responsible for supplying the current account_balance.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.core.risk.calculator import RiskCalculator, RiskValidationError
from app.monitoring.logger import risk_logger

MAX_RISK_PCT = Decimal("0.01")  # 1% rule — never configurable


# ---------------------------------------------------------------------------
# Request / result models
# ---------------------------------------------------------------------------


class TradeRequest(BaseModel):
    """
    All parameters needed for risk validation of a proposed trade.

    Constructed by OrderManager (Layer 7) before calling validate().
    The account_balance field is a real-time snapshot from the broker.
    """

    trade_id: UUID
    symbol: str
    direction: Literal["BUY", "SELL"]
    quantity: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal | None  # None triggers immediate rejection
    strategy_id: UUID | None = None
    account_balance: Decimal


class ValidationResult(BaseModel):
    """
    Returned by RiskManager.validate() when a trade is approved.

    All fields are forwarded to OrderManager for audit logging and
    trade persistence.
    """

    trade_id: UUID
    risk_amount: Decimal
    max_quantity: int
    account_balance_at_entry: Decimal  # snapshot at time of validation


# ---------------------------------------------------------------------------
# Rejection exception
# ---------------------------------------------------------------------------


class RiskRejectionError(Exception):
    """
    Raised by RiskManager.validate() when a trade fails risk checks.

    Carries enough context for the caller to log the rejection and
    surface it to the operator.
    """

    def __init__(self, reason: str, request: TradeRequest) -> None:
        super().__init__(reason)
        self.reason = reason
        self.request = request


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------


class RiskManager:
    """
    Validates a proposed trade against the 1% risk rule.

    Stateless: instantiate once and reuse.
    """

    def __init__(self) -> None:
        self._calculator = RiskCalculator()

    def validate(self, request: TradeRequest) -> ValidationResult:
        """
        Validate a trade request against the 1% risk rule.

        Args:
            request: The proposed trade with all financial parameters and the
                     current account balance.

        Returns:
            ValidationResult with the computed risk_amount,
            max_quantity, and account_balance_at_entry snapshot.

        Raises:
            RiskRejectionError: If the trade fails any risk check. The error
                message and .reason attribute describe the failure. All
                rejections are logged to risk.log before raising.
        """
        # --- Gate 1: stop-loss is required ---
        if request.stop_loss_price is None:
            self._reject(
                request,
                reason="MISSING_STOP_LOSS",
                detail="No stop_loss_price provided — all trades require a stop-loss",
            )

        # --- Gate 2: stop-loss must be below entry (validated by calculator) ---
        try:
            max_qty = self._calculator.max_quantity(
                account_balance=request.account_balance,
                entry_price=request.entry_price,
                stop_loss_price=request.stop_loss_price,  # type: ignore[arg-type]
            )
        except RiskValidationError as exc:
            self._reject(request, reason="INVALID_STOP_LOSS", detail=str(exc))

        # --- Gate 3: actual risk amount must not exceed 1% of balance ---
        risk_amount = self._calculator.risk_amount(
            quantity=request.quantity,
            entry_price=request.entry_price,
            stop_loss_price=request.stop_loss_price,  # type: ignore[arg-type]
        )
        max_allowed = request.account_balance * MAX_RISK_PCT
        if risk_amount > max_allowed:
            self._reject(
                request,
                reason="RISK_LIMIT_EXCEEDED",
                detail=(
                    f"risk_amount {risk_amount:.2f} exceeds 1% limit "
                    f"({max_allowed:.2f}) for balance {request.account_balance:.2f}"
                ),
            )

        return ValidationResult(
            trade_id=request.trade_id,
            risk_amount=risk_amount,
            max_quantity=max_qty,
            account_balance_at_entry=request.account_balance,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _reject(request: TradeRequest, reason: str, detail: str) -> None:
        """Log the rejection to risk.log and raise RiskRejectionError."""
        risk_logger.warning(
            "Trade rejected by RiskManager",
            extra={
                "trade_id": str(request.trade_id),
                "reason": reason,
                "detail": detail,
                "symbol": request.symbol,
                "direction": request.direction,
                "quantity": str(request.quantity),
                "entry_price": str(request.entry_price),
                "stop_loss_price": str(request.stop_loss_price),
                "account_balance": str(request.account_balance),
                "strategy_id": str(request.strategy_id) if request.strategy_id else None,
            },
        )
        raise RiskRejectionError(reason=f"{reason}: {detail}", request=request)
