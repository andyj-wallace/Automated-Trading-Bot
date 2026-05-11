"""
Unit tests for BullBearStrategy.

Uses small period values (regime_period=4, entry_period=4, atr_period=3) to
keep bar sequences short while keeping conditions precisely controllable.
All numeric expectations are pre-computed analytically — see comments.

Default test config: regime_period=4, entry_period=4, atr_period=3, atr_multiplier="1.5"

--- Bull regime SPY bars (regime_period=4, 5 bars) ---
closes = [10, 10, 10, 10, 15]
  SMA([10,10,10,15]) = 45/4 = 11.25
  SPY close = 15 > 11.25 → bull regime

--- Bear regime SPY bars (regime_period=4, 5 bars) ---
closes = [15, 15, 15, 15, 10]
  SMA([15,15,15,10]) = 55/4 = 13.75
  SPY close = 10 < 13.75 → bear regime

--- Target BUY crossover (entry_period=4, 6 bars) ---
closes = [9, 9, 9, 9, 9, 10.5]
  ma_prev = SMA([9,9,9,9]) = 9.0,   prev_close = 9 ≤ 9.0 ✓
  ma_now  = SMA([9,9,9,10.5]) = 9.375, current = 10.5 > 9.375 ✓
  → crossover

ATR for atr_period=3 on the buy-cross bars (make_bars_from_closes):
  bar[5] (close=10.5): open=9, high=11.0, low=8.5
  TRs for bars[1..5] using make_bars_from_closes pattern:
    bars[1..4]: high=9.5, low=8.5, prev_close=9  → TR = max(1.0, 0.5, 0.5) = 1.0
    bars[5]:    high=11.0, low=8.5, prev_close=9 → TR = max(2.5, 2.0, 0.5) = 2.5
  last 3 TRs = [1.0, 1.0, 2.5]
  ATR = (1+1+2.5)/3 = 1.5
  stop = 10.5 − 1.5×1.5 = 10.5 − 2.25 = 8.25
  stop_dist = 2.25
  take_profit = 10.5 + 2×2.25 = 15.0
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.strategy_engine.base import MarketData, RiskParams
from app.core.strategy_engine.bull_bear import BullBearStrategy, _atr, _sma

from .conftest import make_bars_from_closes, make_market_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strategy(**kwargs) -> BullBearStrategy:
    """Return a BullBearStrategy with small periods for test controllability."""
    defaults = {
        "regime_period": 4,
        "entry_period": 4,
        "atr_period": 3,
        "atr_multiplier": "1.5",
    }
    defaults.update(kwargs)
    return BullBearStrategy(config=defaults)


def _bull_spy_closes(period: int = 4) -> list[float]:
    """SPY closes where the last bar is above the regime SMA."""
    return [10.0] * period + [15.0]  # last close 15 > SMA([10]*3+[15])=11.25


def _bear_spy_closes(period: int = 4) -> list[float]:
    """SPY closes where the last bar is below the regime SMA."""
    return [15.0] * period + [10.0]  # last close 10 < SMA([15]*3+[10])=13.75


def _buy_cross_target_closes(period: int = 4) -> list[float]:
    """Target closes where price crosses above entry SMA on the last bar."""
    return [9.0] * (period + 1) + [10.5]


def _already_above_target_closes(period: int = 4) -> list[float]:
    """Target closes where price was already above SMA — no crossover."""
    return [9.0] * period + [10.5, 10.8]


def _price_below_target_closes(period: int = 4) -> list[float]:
    """Target closes where current price is below SMA."""
    return [10.0] * (period + 1) + [9.0]


def _market(
    target_closes: list[float],
    spy_closes: list[float],
    symbol: str = "AAPL",
    regime_symbol: str = "SPY",
    current_price: float | None = None,
) -> MarketData:
    """Assemble a MarketData with target bars and SPY regime bars."""
    target_bars = make_bars_from_closes(target_closes)
    spy_bars = make_bars_from_closes(spy_closes)
    price = (
        Decimal(str(current_price))
        if current_price is not None
        else target_bars[-1].close
    )
    return MarketData(
        symbol=symbol,
        current_price=price,
        bars=target_bars,
        extra_bars={regime_symbol: spy_bars},
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInit:
    def test_defaults_parse_correctly(self):
        s = BullBearStrategy(config={})
        assert s.regime_symbol == "SPY"
        assert s.regime_period == 200
        assert s.entry_period == 50
        assert s.atr_period == 14
        assert s.atr_multiplier == Decimal("1.5")

    def test_custom_config_overrides_defaults(self):
        s = BullBearStrategy(config={
            "regime_symbol": "QQQ",
            "regime_period": 100,
            "entry_period": 20,
            "atr_period": 7,
            "atr_multiplier": "2.0",
        })
        assert s.regime_symbol == "QQQ"
        assert s.regime_period == 100
        assert s.entry_period == 20
        assert s.atr_period == 7
        assert s.atr_multiplier == Decimal("2.0")

    def test_regime_symbol_uppercased(self):
        s = BullBearStrategy(config={"regime_symbol": "spy"})
        assert s.regime_symbol == "SPY"

    def test_regime_period_zero_raises(self):
        with pytest.raises(ValueError, match="regime_period"):
            BullBearStrategy(config={"regime_period": 0})

    def test_entry_period_zero_raises(self):
        with pytest.raises(ValueError, match="entry_period"):
            BullBearStrategy(config={"entry_period": 0})

    def test_atr_period_zero_raises(self):
        with pytest.raises(ValueError, match="atr_period"):
            BullBearStrategy(config={"atr_period": 0})

    def test_atr_multiplier_zero_raises(self):
        with pytest.raises(ValueError, match="atr_multiplier"):
            BullBearStrategy(config={"atr_multiplier": "0"})

    def test_atr_multiplier_negative_raises(self):
        with pytest.raises(ValueError, match="atr_multiplier"):
            BullBearStrategy(config={"atr_multiplier": "-1"})


# ---------------------------------------------------------------------------
# Regime detection
# ---------------------------------------------------------------------------


class TestRegimeDetection:
    @pytest.mark.asyncio
    async def test_bull_regime_allows_entry(self):
        """SPY close above SMA → bull regime → BUY when crossover present."""
        s = _strategy()
        data = _market(_buy_cross_target_closes(), _bull_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_bear_regime_returns_hold(self):
        """SPY close below SMA → bear regime → HOLD regardless of target."""
        s = _strategy()
        data = _market(_buy_cross_target_closes(), _bear_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_spy_exactly_at_sma_is_bear(self):
        """SPY close == SMA is not strictly above → HOLD (bear regime boundary)."""
        s = _strategy()
        # All-uniform SPY closes: SMA = close → NOT strictly above → HOLD
        spy_closes = [10.0] * 5  # 5 bars, SMA=10, last=10 → 10 ≤ 10 → bear
        data = _market(_buy_cross_target_closes(), spy_closes)
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_bear_regime_overrides_strong_target_crossover(self):
        """
        Even with a clear target crossover, bear regime suppresses the signal.
        The bear gate is evaluated before the entry condition.
        """
        s = _strategy()
        data = _market(_buy_cross_target_closes(), _bear_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_missing_regime_bars_returns_hold(self):
        """extra_bars does not contain the regime symbol → HOLD."""
        s = _strategy()
        target_bars = make_bars_from_closes(_buy_cross_target_closes())
        data = MarketData(
            symbol="AAPL",
            current_price=target_bars[-1].close,
            bars=target_bars,
            extra_bars={},  # SPY absent
            timestamp=datetime.now(timezone.utc),
        )
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_insufficient_regime_bars_returns_hold(self):
        """Fewer than regime_period+1 SPY bars → HOLD."""
        s = _strategy(regime_period=4)
        # Only 4 bars; need 5 (regime_period + 1)
        spy_closes = [10.0] * 4
        data = _market(_buy_cross_target_closes(), spy_closes)
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"


# ---------------------------------------------------------------------------
# Entry conditions
# ---------------------------------------------------------------------------


class TestEntryConditions:
    @pytest.mark.asyncio
    async def test_hold_when_target_already_above_sma(self):
        """Price was above SMA on previous bar → no fresh crossover → HOLD."""
        s = _strategy()
        data = _market(_already_above_target_closes(), _bull_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_target_below_sma(self):
        """Current price below entry SMA → no crossover → HOLD."""
        s = _strategy()
        data = _market(_price_below_target_closes(), _bull_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_insufficient_target_bars(self):
        """Fewer than max(entry_period, atr_period)+1 target bars → HOLD."""
        s = _strategy(entry_period=4, atr_period=3)
        # Need max(4,3)+1=5 bars; supply only 4
        target_closes = [10.0] * 4
        data = _market(target_closes, _bull_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_target_price_exactly_at_sma(self):
        """current_close == ma_now (not strictly above) → HOLD."""
        s = _strategy()
        # Uniform closes: SMA = 10 = close → NOT strictly above → HOLD
        target_closes = [10.0] * 6
        data = _market(target_closes, _bull_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_on_bar_immediately_after_crossover(self):
        """Bar after crossover: prev was already above SMA → no re-entry → HOLD."""
        s = _strategy()
        # prev_close=10.5 > ma_prev=(9+9+9+10.5)/4=9.375 → already above → HOLD
        target_closes = [9.0, 9.0, 9.0, 9.0, 9.0, 10.5, 10.8]
        data = _market(target_closes, _bull_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"


# ---------------------------------------------------------------------------
# BUY signal fields
# ---------------------------------------------------------------------------


class TestBuySignal:
    @pytest.mark.asyncio
    async def test_buy_action_on_crossover_in_bull_regime(self):
        s = _strategy()
        data = _market(_buy_cross_target_closes(), _bull_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_buy_entry_price_matches_current_price(self):
        s = _strategy()
        data = _market(_buy_cross_target_closes(), _bull_spy_closes(), current_price=10.5)
        signal = await s.generate_signal(data)
        assert signal.entry_price == Decimal("10.5")

    @pytest.mark.asyncio
    async def test_buy_stop_loss_is_below_entry(self):
        s = _strategy()
        data = _market(_buy_cross_target_closes(), _bull_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.stop_loss_price is not None
        assert signal.stop_loss_price < signal.entry_price

    @pytest.mark.asyncio
    async def test_buy_stop_loss_atr_based(self):
        """
        stop = entry − ATR × atr_multiplier, rounded to 2 dp.

        entry=10.5, ATR=1.5 (see module docstring), multiplier=1.5:
          stop = 10.5 − 1.5×1.5 = 10.5 − 2.25 = 8.25
        """
        s = _strategy(atr_multiplier="1.5")
        data = _market(_buy_cross_target_closes(), _bull_spy_closes(), current_price=10.5)
        signal = await s.generate_signal(data)
        assert signal.stop_loss_price == Decimal("8.25")

    @pytest.mark.asyncio
    async def test_buy_take_profit_above_entry(self):
        s = _strategy()
        data = _market(_buy_cross_target_closes(), _bull_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.take_profit_price is not None
        assert signal.take_profit_price > signal.entry_price

    @pytest.mark.asyncio
    async def test_buy_take_profit_exact_value(self):
        """
        take_profit = entry + 2 × stop_distance, rounded to 2 dp.

        entry=10.5, stop=8.25, stop_dist=2.25:
          take_profit = 10.5 + 2×2.25 = 15.0
        """
        s = _strategy(atr_multiplier="1.5")
        data = _market(_buy_cross_target_closes(), _bull_spy_closes(), current_price=10.5)
        signal = await s.generate_signal(data)
        assert signal.take_profit_price == Decimal("15.0")

    @pytest.mark.asyncio
    async def test_buy_take_profit_satisfies_min_rr(self):
        """take_profit ≥ entry + 2 × stop_distance (at least 2:1 R:R)."""
        s = _strategy()
        data = _market(_buy_cross_target_closes(), _bull_spy_closes())
        signal = await s.generate_signal(data)
        stop_dist = signal.entry_price - signal.stop_loss_price
        assert signal.take_profit_price >= signal.entry_price + stop_dist * 2

    @pytest.mark.asyncio
    async def test_buy_signal_symbol_matches_market_data(self):
        s = _strategy()
        data = _market(_buy_cross_target_closes(), _bull_spy_closes(), symbol="MSFT")
        signal = await s.generate_signal(data)
        assert signal.symbol == "MSFT"

    @pytest.mark.asyncio
    async def test_buy_signal_timestamp_is_timezone_aware(self):
        s = _strategy()
        data = _market(_buy_cross_target_closes(), _bull_spy_closes())
        signal = await s.generate_signal(data)
        assert signal.timestamp is not None
        assert signal.timestamp.tzinfo is not None

    @pytest.mark.asyncio
    async def test_custom_atr_multiplier_changes_stop(self):
        """Larger atr_multiplier → wider stop below entry."""
        s_tight = _strategy(atr_multiplier="1.0")
        s_wide = _strategy(atr_multiplier="2.0")
        data = _market(_buy_cross_target_closes(), _bull_spy_closes(), current_price=10.5)
        sig_tight = await s_tight.generate_signal(data)
        sig_wide = await s_wide.generate_signal(data)
        assert sig_wide.stop_loss_price < sig_tight.stop_loss_price


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


class TestPositionSizing:
    @pytest.mark.asyncio
    async def test_calculate_position_size_uses_1pct_rule(self):
        """1% of $100k / $1 risk per share = 1000 shares."""
        s = _strategy()
        params = RiskParams(
            account_balance=Decimal("100000"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("99"),
        )
        qty = await s.calculate_position_size(params)
        assert qty == 1000

    @pytest.mark.asyncio
    async def test_calculate_position_size_returns_zero_on_invalid_stop(self):
        """stop == entry → RiskCalculator raises → returns 0."""
        s = _strategy()
        params = RiskParams(
            account_balance=Decimal("100000"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("100"),
        )
        qty = await s.calculate_position_size(params)
        assert qty == 0


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class TestConfigSchema:
    def test_schema_returns_dict_with_required_keys(self):
        s = _strategy()
        schema = s.get_config_schema()
        assert isinstance(schema, dict)
        props = schema["properties"]
        assert "regime_symbol" in props
        assert "regime_period" in props
        assert "entry_period" in props
        assert "atr_period" in props
        assert "atr_multiplier" in props
        assert "symbols" in props

    def test_schema_defaults_match_instance_defaults(self):
        s = BullBearStrategy(config={})
        schema = s.get_config_schema()
        props = schema["properties"]
        assert props["regime_symbol"]["default"] == "SPY"
        assert props["regime_period"]["default"] == 200
        assert props["entry_period"]["default"] == 50
        assert props["atr_period"]["default"] == 14
        assert props["atr_multiplier"]["default"] == "1.5"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_bull_bear_registered_in_registry(self):
        from app.core.strategy_engine.registry import registry
        assert "bull_bear" in registry.registered_types()

    def test_registry_builds_correct_class(self):
        from app.core.strategy_engine.registry import registry
        instance = registry.build("bull_bear", {"regime_period": 50})
        assert isinstance(instance, BullBearStrategy)

    def test_registered_class_is_bull_bear_strategy(self):
        from app.core.strategy_engine.registry import registry
        cls = registry._classes["bull_bear"]
        assert cls is BullBearStrategy


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


class TestUtilityFunctions:
    def test_sma_uniform_values(self):
        values = [Decimal("10")] * 4
        assert _sma(values, 4) == Decimal("10")

    def test_sma_last_period_only(self):
        # 6 values, period=4: only last 4 should count
        values = [Decimal(str(v)) for v in [1, 2, 10, 10, 10, 10]]
        assert _sma(values, 4) == Decimal("10")

    def test_atr_uniform_bars_constant_tr(self):
        """
        Bars with constant high=close+0.5, low=close-0.5, prev_close=close.
        TR = max(1.0, 0.5, 0.5) = 1.0 → ATR = 1.0.
        """
        closes = [10.0] * 5
        bars = make_bars_from_closes(closes)
        result = _atr(bars, 3)
        assert result == Decimal("1")

    def test_atr_uses_only_last_period_trs(self):
        """
        With 6 bars, atr_period=2: only the last 2 TRs should be averaged.
        Using buy-cross bars: TRs = [1,1,1,1,2.5], last 2 = [1, 2.5], ATR = 1.75.
        """
        bars = make_bars_from_closes([9.0, 9.0, 9.0, 9.0, 9.0, 10.5])
        result = _atr(bars, 2)
        assert result == Decimal("1.75")
