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


def _closes_to_bars(closes):
    from datetime import UTC, datetime, timedelta

    from app.brokers.base import PriceBar

    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        PriceBar(timestamp=start + timedelta(days=i), open=c, high=c + 1, low=c - 1, close=c, volume=1)
        for i, c in enumerate(closes)
    ]


def test_regime_ok():
    from app.core.strategy_engine.filters import regime_ok

    up = _closes_to_bars([Decimal(100 + i) for i in range(20)])
    down = _closes_to_bars([Decimal(200 - i) for i in range(20)])
    assert regime_ok(up[:5], 10) is False  # too few bars
    assert regime_ok(up, 10) is True
    assert regime_ok(down, 10) is False


def test_volatility_ok_boundaries():
    from app.core.strategy_engine.filters import volatility_ok

    bars = _closes_to_bars([Decimal(100)] * 20)  # ATR 2 on close 100 = 2%
    assert volatility_ok(bars, 14, Decimal("0.02"), Decimal("0.04")) is True
    assert volatility_ok(bars, 14, Decimal("0.01"), Decimal("0.02")) is True
    assert volatility_ok(bars, 14, Decimal("0.021"), Decimal("0.04")) is False
    assert volatility_ok(bars, 14, Decimal("0.0"), Decimal("0.019")) is False
