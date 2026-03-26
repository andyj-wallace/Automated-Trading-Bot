"""
Unit tests for RiskCalculator (Layer 6.2).

Covers the required cases per tasks.md:
  - Normal case
  - Tiny stop distance
  - Large account
  - Fractional result (floor behaviour)
  - Invalid stop price (≥ entry_price) — raises RiskValidationError
Plus additional edge cases for robustness.
"""

from decimal import Decimal

import pytest

from app.core.risk.calculator import RiskCalculator, RiskValidationError
from app.core.strategy_engine.base import RiskParams


@pytest.fixture
def calc() -> RiskCalculator:
    return RiskCalculator()


# ---------------------------------------------------------------------------
# Normal case
# ---------------------------------------------------------------------------


def test_normal_case(calc: RiskCalculator) -> None:
    """
    $100k account, entry $200, stop $195 → risk/share $5, max risk $1000 → qty 200.
    """
    qty = calc.max_quantity(
        account_balance=Decimal("100000"),
        entry_price=Decimal("200"),
        stop_loss_price=Decimal("195"),
    )
    assert qty == 200


def test_normal_case_small_account(calc: RiskCalculator) -> None:
    """$10k account, entry $50, stop $48 → risk/share $2, max risk $100 → qty 50."""
    qty = calc.max_quantity(
        account_balance=Decimal("10000"),
        entry_price=Decimal("50"),
        stop_loss_price=Decimal("48"),
    )
    assert qty == 50


# ---------------------------------------------------------------------------
# Tiny stop distance
# ---------------------------------------------------------------------------


def test_tiny_stop_distance(calc: RiskCalculator) -> None:
    """
    $100k account, entry $100.00, stop $99.99 → risk/share $0.01
    max risk = $1000, qty = floor(1000 / 0.01) = 100,000
    """
    qty = calc.max_quantity(
        account_balance=Decimal("100000"),
        entry_price=Decimal("100.00"),
        stop_loss_price=Decimal("99.99"),
    )
    assert qty == 100_000


def test_small_stop_distance(calc: RiskCalculator) -> None:
    """$50k account, entry $500, stop $499 → risk/share $1, max risk $500 → qty 500."""
    qty = calc.max_quantity(
        account_balance=Decimal("50000"),
        entry_price=Decimal("500"),
        stop_loss_price=Decimal("499"),
    )
    assert qty == 500


# ---------------------------------------------------------------------------
# Large account
# ---------------------------------------------------------------------------


def test_large_account(calc: RiskCalculator) -> None:
    """$10M account, entry $100, stop $95 → max risk $100k, qty 20,000."""
    qty = calc.max_quantity(
        account_balance=Decimal("10_000_000"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("95"),
    )
    assert qty == 20_000


# ---------------------------------------------------------------------------
# Fractional result — floor behaviour
# ---------------------------------------------------------------------------


def test_fractional_result_is_floored(calc: RiskCalculator) -> None:
    """
    $100k account, entry $150, stop $143 → risk/share $7
    max risk = $1000, qty = floor(1000 / 7) = floor(142.857...) = 142
    """
    qty = calc.max_quantity(
        account_balance=Decimal("100000"),
        entry_price=Decimal("150"),
        stop_loss_price=Decimal("143"),
    )
    assert qty == 142


def test_fractional_result_is_never_rounded_up(calc: RiskCalculator) -> None:
    """Result must always be floored — never rounded up even when fraction is 0.99."""
    qty = calc.max_quantity(
        account_balance=Decimal("10000"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("90.01"),  # risk/share = 9.99, max_risk = 100
    )
    # floor(100 / 9.99) = floor(10.010...) = 10
    assert qty == 10


# ---------------------------------------------------------------------------
# Invalid stop price — raises RiskValidationError
# ---------------------------------------------------------------------------


def test_stop_equal_to_entry_raises(calc: RiskCalculator) -> None:
    with pytest.raises(RiskValidationError, match="stop_loss_price"):
        calc.max_quantity(
            account_balance=Decimal("100000"),
            entry_price=Decimal("150"),
            stop_loss_price=Decimal("150"),
        )


def test_stop_above_entry_raises(calc: RiskCalculator) -> None:
    with pytest.raises(RiskValidationError, match="stop_loss_price"):
        calc.max_quantity(
            account_balance=Decimal("100000"),
            entry_price=Decimal("150"),
            stop_loss_price=Decimal("160"),
        )


def test_zero_account_balance_raises(calc: RiskCalculator) -> None:
    with pytest.raises(RiskValidationError, match="account_balance"):
        calc.max_quantity(
            account_balance=Decimal("0"),
            entry_price=Decimal("150"),
            stop_loss_price=Decimal("145"),
        )


def test_negative_entry_price_raises(calc: RiskCalculator) -> None:
    with pytest.raises(RiskValidationError, match="entry_price"):
        calc.max_quantity(
            account_balance=Decimal("100000"),
            entry_price=Decimal("-10"),
            stop_loss_price=Decimal("-15"),
        )


# ---------------------------------------------------------------------------
# risk_amount helper
# ---------------------------------------------------------------------------


def test_risk_amount_calculation(calc: RiskCalculator) -> None:
    """200 shares × ($200 - $195) = $1000."""
    amount = calc.risk_amount(
        quantity=200,
        entry_price=Decimal("200"),
        stop_loss_price=Decimal("195"),
    )
    assert amount == Decimal("1000")


def test_risk_amount_matches_1pct_for_max_quantity(calc: RiskCalculator) -> None:
    """The risk amount for max_quantity must be ≤ 1% of account balance."""
    balance = Decimal("100000")
    entry = Decimal("200")
    stop = Decimal("195")

    qty = calc.max_quantity(balance, entry, stop)
    amount = calc.risk_amount(qty, entry, stop)

    assert amount <= balance * Decimal("0.01")


# ---------------------------------------------------------------------------
# RiskParams convenience wrapper
# ---------------------------------------------------------------------------


def test_max_quantity_from_params(calc: RiskCalculator) -> None:
    params = RiskParams(
        account_balance=Decimal("100000"),
        entry_price=Decimal("200"),
        stop_loss_price=Decimal("195"),
    )
    assert calc.max_quantity_from_params(params) == 200
