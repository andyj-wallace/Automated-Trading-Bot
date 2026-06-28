"""Unit tests for the shared chop/whipsaw separation filter."""

from decimal import Decimal

from app.core.strategy_engine.filters import min_separation_ok


def test_zero_threshold_passes_any_positive_crossover():
    assert min_separation_ok(Decimal("100.0001"), Decimal("100"), Decimal("0")) is True


def test_separation_below_threshold_fails():
    # 100.05 is 0.05% above 100 — below a 0.1% threshold
    assert min_separation_ok(Decimal("100.05"), Decimal("100"), Decimal("0.001")) is False


def test_separation_at_threshold_passes():
    # exactly 0.1% above
    assert min_separation_ok(Decimal("100.1"), Decimal("100"), Decimal("0.001")) is True


def test_separation_above_threshold_passes():
    assert min_separation_ok(Decimal("112"), Decimal("100"), Decimal("0.001")) is True


def test_faster_below_slower_fails():
    assert min_separation_ok(Decimal("99"), Decimal("100"), Decimal("0.001")) is False


def test_zero_or_negative_slower_short_circuits_true():
    """Guards against division by zero; degenerate input passes rather than crashes."""
    assert min_separation_ok(Decimal("10"), Decimal("0"), Decimal("0.001")) is True
    assert min_separation_ok(Decimal("10"), Decimal("-5"), Decimal("0.001")) is True
