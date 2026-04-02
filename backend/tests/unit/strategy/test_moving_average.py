"""
Unit tests for MovingAverageStrategy (Layer 12.3).

Uses the conftest helpers (make_bars, make_bars_from_closes, make_market_data)
and small fast/slow periods (3/5 or 5/10) to keep bar sequences short and
crossover conditions precisely controllable.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.strategy_engine.base import RiskParams
from app.core.strategy_engine.moving_average import MovingAverageStrategy

from .conftest import make_bars_from_closes, make_market_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strategy(fast: int = 3, slow: int = 5, stop_pct: str = "0.02") -> MovingAverageStrategy:
    """Return a strategy with small periods for test controllability."""
    return MovingAverageStrategy(
        config={"fast_period": fast, "slow_period": slow, "stop_loss_pct": stop_pct}
    )


def _closes_golden_cross() -> list[float]:
    """
    6 bars engineered for a golden cross at the last bar.

    fast_period=3, slow_period=5.
    Previous position: fast_prev=avg([90,90,90])=90, slow_prev=avg([90..90])=90 (equal → ≤)
    Current position:  fast_cur=avg([90,90,200])=126.7, slow_cur=avg([90,90,90,90,200])=112
    Condition: fast_prev(90) <= slow_prev(90) AND fast_cur(126.7) > slow_cur(112) ✓
    """
    return [90.0, 90.0, 90.0, 90.0, 90.0, 200.0]


def _closes_death_cross() -> list[float]:
    """
    6 bars engineered for a death cross at the last bar.

    Previous: fast_prev=200, slow_prev=200 (equal → ≥)
    Current:  fast_cur=avg([200,200,50])=150, slow_cur=avg([200,200,200,200,50])=170
    Condition: fast_prev(200) >= slow_prev(200) AND fast_cur(150) < slow_cur(170) ✓
    """
    return [200.0, 200.0, 200.0, 200.0, 200.0, 50.0]


def _closes_established_uptrend() -> list[float]:
    """
    7 bars in steady uptrend — fast already above slow several bars ago.
    No crossover on the last bar → HOLD.
    """
    return [80.0, 90.0, 100.0, 110.0, 120.0, 130.0, 140.0]


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def test_init_custom_config() -> None:
    s = MovingAverageStrategy(
        config={"fast_period": 10, "slow_period": 50, "stop_loss_pct": "0.05"}
    )
    assert s.fast_period == 10
    assert s.slow_period == 50
    assert s.stop_loss_pct == Decimal("0.05")


def test_init_defaults() -> None:
    s = MovingAverageStrategy(config={})
    assert s.fast_period == 50
    assert s.slow_period == 200
    assert s.stop_loss_pct == Decimal("0.03")


def test_init_fast_ge_slow_raises() -> None:
    with pytest.raises(ValueError, match="fast_period"):
        MovingAverageStrategy(config={"fast_period": 50, "slow_period": 50})


def test_init_fast_gt_slow_raises() -> None:
    with pytest.raises(ValueError, match="fast_period"):
        MovingAverageStrategy(config={"fast_period": 100, "slow_period": 50})


def test_init_invalid_stop_pct_raises() -> None:
    with pytest.raises(ValueError, match="stop_loss_pct"):
        MovingAverageStrategy(config={"stop_loss_pct": "1.5"})


# ---------------------------------------------------------------------------
# HOLD conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hold_when_insufficient_bars(insufficient_bars_market) -> None:
    """Fewer bars than slow_period+1 → HOLD."""
    s = _strategy()  # slow=5; insufficient_bars_market has 10 bars, but strategy needs slow+1=6 bars
    # insufficient_bars_market has 10 bars, strategy needs 6 — this one passes the bar check.
    # Instead, test with 5 bars (exactly at slow_period).
    bars = make_bars_from_closes([100.0] * 5)
    data = make_market_data("X", bars)
    signal = await s.generate_signal(data)
    assert signal.action == "HOLD"


@pytest.mark.asyncio
async def test_hold_on_exactly_slow_period_bars() -> None:
    """Exactly slow_period bars (need slow+1) → HOLD."""
    s = _strategy(fast=3, slow=5)
    bars = make_bars_from_closes([100.0] * 5)
    data = make_market_data("X", bars)
    signal = await s.generate_signal(data)
    assert signal.action == "HOLD"


@pytest.mark.asyncio
async def test_hold_on_established_uptrend() -> None:
    """Ongoing uptrend with no crossover on the last bar → HOLD."""
    s = _strategy(fast=3, slow=5)
    bars = make_bars_from_closes(_closes_established_uptrend())
    data = make_market_data("BULL", bars)
    signal = await s.generate_signal(data)
    assert signal.action == "HOLD"


@pytest.mark.asyncio
async def test_hold_on_flat_market(flat_market) -> None:
    """200 bars with no trend, default 50/200 periods → no crossover → HOLD."""
    s = MovingAverageStrategy(config={})
    signal = await s.generate_signal(flat_market)
    assert signal.action == "HOLD"


# ---------------------------------------------------------------------------
# BUY signal — golden cross
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_on_golden_cross() -> None:
    s = _strategy(fast=3, slow=5)
    bars = make_bars_from_closes(_closes_golden_cross())
    data = make_market_data("GOLD", bars, current_price=Decimal("200"))
    signal = await s.generate_signal(data)
    assert signal.action == "BUY"


@pytest.mark.asyncio
async def test_buy_signal_has_correct_symbol() -> None:
    s = _strategy(fast=3, slow=5)
    bars = make_bars_from_closes(_closes_golden_cross())
    data = make_market_data("AAPL", bars, current_price=Decimal("200"))
    signal = await s.generate_signal(data)
    assert signal.symbol == "AAPL"


@pytest.mark.asyncio
async def test_buy_stop_loss_below_entry() -> None:
    """Stop-loss must be strictly below entry price."""
    s = _strategy(fast=3, slow=5, stop_pct="0.03")
    bars = make_bars_from_closes(_closes_golden_cross())
    data = make_market_data("X", bars, current_price=Decimal("100"))
    signal = await s.generate_signal(data)
    assert signal.stop_loss_price is not None
    assert signal.stop_loss_price < signal.entry_price


@pytest.mark.asyncio
async def test_buy_take_profit_above_entry() -> None:
    """Take-profit must be above entry for a BUY."""
    s = _strategy(fast=3, slow=5)
    bars = make_bars_from_closes(_closes_golden_cross())
    data = make_market_data("X", bars, current_price=Decimal("100"))
    signal = await s.generate_signal(data)
    assert signal.take_profit_price is not None
    assert signal.take_profit_price > signal.entry_price


@pytest.mark.asyncio
async def test_buy_take_profit_at_2_to_1_rr() -> None:
    """Take profit is entry + 2 × stop_distance → 2:1 R:R."""
    s = _strategy(fast=3, slow=5, stop_pct="0.10")  # 10% stop for easy maths
    entry = Decimal("100")
    bars = make_bars_from_closes(_closes_golden_cross())
    data = make_market_data("X", bars, current_price=entry)
    signal = await s.generate_signal(data)
    stop_dist = signal.entry_price - signal.stop_loss_price
    expected_tp = signal.entry_price + stop_dist * 2
    assert abs(signal.take_profit_price - expected_tp) < Decimal("0.02")


@pytest.mark.asyncio
async def test_buy_entry_price_is_current_price() -> None:
    s = _strategy(fast=3, slow=5)
    bars = make_bars_from_closes(_closes_golden_cross())
    current = Decimal("155.50")
    data = make_market_data("X", bars, current_price=current)
    signal = await s.generate_signal(data)
    assert signal.entry_price == current


# ---------------------------------------------------------------------------
# SELL signal — death cross
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_on_death_cross() -> None:
    s = _strategy(fast=3, slow=5)
    bars = make_bars_from_closes(_closes_death_cross())
    data = make_market_data("DEAD", bars, current_price=Decimal("50"))
    signal = await s.generate_signal(data)
    assert signal.action == "SELL"


@pytest.mark.asyncio
async def test_sell_stop_loss_above_entry() -> None:
    """For a short, stop-loss must be above entry price."""
    s = _strategy(fast=3, slow=5)
    bars = make_bars_from_closes(_closes_death_cross())
    data = make_market_data("X", bars, current_price=Decimal("100"))
    signal = await s.generate_signal(data)
    assert signal.stop_loss_price is not None
    assert signal.stop_loss_price > signal.entry_price


@pytest.mark.asyncio
async def test_sell_take_profit_below_entry() -> None:
    """For a short, take-profit must be below entry."""
    s = _strategy(fast=3, slow=5)
    bars = make_bars_from_closes(_closes_death_cross())
    data = make_market_data("X", bars, current_price=Decimal("100"))
    signal = await s.generate_signal(data)
    assert signal.take_profit_price is not None
    assert signal.take_profit_price < signal.entry_price


# ---------------------------------------------------------------------------
# Custom periods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_periods_5_10_golden_cross() -> None:
    """fast=5, slow=10 crossover detected correctly."""
    s = _strategy(fast=5, slow=10)
    # 11 bars: first 10 at 90, last at 200 — forces golden cross at last bar
    closes = [90.0] * 10 + [200.0]
    bars = make_bars_from_closes(closes)
    data = make_market_data("X", bars, current_price=Decimal("200"))
    signal = await s.generate_signal(data)
    assert signal.action == "BUY"


@pytest.mark.asyncio
async def test_custom_periods_5_10_death_cross() -> None:
    s = _strategy(fast=5, slow=10)
    closes = [200.0] * 10 + [50.0]
    bars = make_bars_from_closes(closes)
    data = make_market_data("X", bars, current_price=Decimal("50"))
    signal = await s.generate_signal(data)
    assert signal.action == "SELL"


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calculate_position_size_positive() -> None:
    s = _strategy()
    qty = await s.calculate_position_size(
        RiskParams(
            account_balance=Decimal("100000"),
            entry_price=Decimal("200"),
            stop_loss_price=Decimal("194"),
        )
    )
    assert qty > 0


@pytest.mark.asyncio
async def test_calculate_position_size_stop_above_entry_returns_zero() -> None:
    """SELL signal has stop above entry — calculate_position_size returns 0 (no crash)."""
    s = _strategy()
    qty = await s.calculate_position_size(
        RiskParams(
            account_balance=Decimal("100000"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("103"),  # stop above entry (short scenario)
        )
    )
    assert qty == 0


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_get_config_schema_has_required_fields() -> None:
    s = MovingAverageStrategy(config={})
    schema = s.get_config_schema()
    props = schema["properties"]
    assert "fast_period" in props
    assert "slow_period" in props
    assert "stop_loss_pct" in props
    assert "symbols" in props


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_strategy_is_registered_in_global_registry() -> None:
    """Self-registration at module import time must have fired."""
    from app.core.strategy_engine.registry import registry

    assert registry.is_registered("moving_average")
    assert registry.get_class("moving_average") is MovingAverageStrategy
