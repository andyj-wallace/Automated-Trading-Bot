"""
RiskCalculator — derives the maximum safe position size from the 1% rule.

Formula:
    max_quantity = floor((account_balance × 0.01) / (entry_price − stop_loss_price))

The 1% rule is a hard system constraint (design.md § Risk Management):
  - Maximum loss per trade = 1% of account balance
  - Loss is defined as: quantity × (entry_price − stop_loss_price)
  - A stop-loss price is required; orders without one are rejected upstream

This module is pure computation — no I/O, no DB, no logging.
"""

from decimal import ROUND_DOWN, Decimal

from app.core.strategy_engine.base import RiskParams

# The hard-coded maximum risk fraction — not configurable per design.
MAX_RISK_PCT = Decimal("0.01")


class RiskValidationError(ValueError):
    """
    Raised when the trade parameters make a valid 1%-rule calculation impossible.

    Current cases:
    - stop_loss_price >= entry_price  (no positive risk distance)
    - entry_price <= 0
    - account_balance <= 0
    """

    pass


class RiskCalculator:
    """
    Stateless position-size calculator.

    All methods are pure functions of their arguments. Instantiate once and
    reuse — there is no mutable state.
    """

    def max_quantity(
        self,
        account_balance: Decimal,
        entry_price: Decimal,
        stop_loss_price: Decimal,
    ) -> int:
        """
        Return the maximum number of shares that keeps loss within 1% of balance.

        Args:
            account_balance:  Current account equity (must be > 0).
            entry_price:      Planned entry price per share (must be > 0).
            stop_loss_price:  Hard stop-loss price (must be < entry_price).

        Returns:
            Maximum safe quantity as a non-negative integer. May be 0 if the
            1% budget is smaller than the risk per share.

        Raises:
            RiskValidationError: If stop_loss_price >= entry_price, or if either
                                 entry_price or account_balance is non-positive.
        """
        self._validate_inputs(account_balance, entry_price, stop_loss_price)

        max_risk = account_balance * MAX_RISK_PCT
        risk_per_share = entry_price - stop_loss_price  # guaranteed > 0 after validation

        # floor division in Decimal space, then convert to int
        raw = (max_risk / risk_per_share).to_integral_value(rounding=ROUND_DOWN)
        return int(raw)

    def risk_amount(
        self,
        quantity: int | Decimal,
        entry_price: Decimal,
        stop_loss_price: Decimal,
    ) -> Decimal:
        """
        Calculate the actual risk amount for a given quantity.

        Returns: quantity × (entry_price − stop_loss_price)
        """
        return Decimal(str(quantity)) * (entry_price - stop_loss_price)

    def max_quantity_from_params(self, params: RiskParams) -> int:
        """Convenience wrapper that accepts a RiskParams model."""
        return self.max_quantity(
            params.account_balance,
            params.entry_price,
            params.stop_loss_price,
        )

    # ------------------------------------------------------------------
    # Internal validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_inputs(
        account_balance: Decimal,
        entry_price: Decimal,
        stop_loss_price: Decimal,
    ) -> None:
        if account_balance <= 0:
            raise RiskValidationError(
                f"account_balance must be positive, got {account_balance}"
            )
        if entry_price <= 0:
            raise RiskValidationError(
                f"entry_price must be positive, got {entry_price}"
            )
        if stop_loss_price >= entry_price:
            raise RiskValidationError(
                f"stop_loss_price ({stop_loss_price}) must be strictly less than "
                f"entry_price ({entry_price})"
            )
