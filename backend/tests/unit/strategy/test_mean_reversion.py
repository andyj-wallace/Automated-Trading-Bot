"""
Unit tests for MeanReversionStrategy.

Uses small lookback values (4) to keep bar sequences short and crossover
conditions precisely controllable. All numeric expectations are pre-computed
analytically — see comments for the derivation.

Test data conventions:
    All sequences use lookback=4, z_score_threshold="1.0" unless noted.

BUY crossover case — bars = [10, 10, 10, 10, 10, 10, 4]  (7 bars, lookback=4):
    Previous window closes[-5:-1] = [10, 10, 10, 10]:
        mean=10, std=0, lower_band=10
        prev_close=10 ≥ 10 ✓
    Current window closes[-4:] = [10, 10, 10, 4]:
        mean=8.5, std=pstdev≈2.598, lower_band≈5.902
        current_close=4 < 5.902 ✓
    → BUY at entry=4.0, stop=3.92 (2% below), mean_target=8.50

Already-below case — bars = [10, 10, 10, 10, 10, 4, 4]:
    Previous window [10, 10, 10, 4]: mean=8.5, std≈2.598, band≈5.902
        prev_close=4 < 5.902 → NOT (prev_close ≥ band) → HOLD

Min-R:R take_profit case — bars = [5, 5, 5, 5, 5, 5, 3.8], stop_pct="0.15":
    Current window [5, 5, 5, 3.8]: mean=4.70, std≈0.520, band≈3.68  (< 3.8 ✓)
    entry=3.8, stop=3.8×0.85=3.23, stop_dist=0.57
    min_take_profit = 3.8 + 2×0.57 = 4.94
    mean_target = 4.70
    take_profit = max(4.70, 4.94) = 4.94  (min_rr floor wins)
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.strategy_engine.base import MarketData, RiskParams
from app.core.strategy_engine.mean_reversion import MeanReversionStrategy, _mean, _pstdev

from .conftest import make_bars_from_closes, make_market_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strategy(**kwargs) -> MeanReversionStrategy:
    """Return a strategy with small lookback for test controllability."""
    defaults = {
        "lookback": 4,
        "z_score_threshold": "1.0",
        "stop_loss_pct": "0.02",
    }
    defaults.update(kwargs)
    return MeanReversionStrategy(config=defaults)


def _buy_cross_closes() -> list[float]:
    """7 bars: price crosses below lower band on the last bar."""
    return [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 4.0]


def _already_below_closes() -> list[float]:
    """7 bars: price was already below band on prev bar — no crossover."""
    return [10.0, 10.0, 10.0, 10.0, 10.0, 4.0, 4.0]


def _within_bands_closes() -> list[float]:
    """7 bars with natural variation: last bar sits near the mean, within the ±1σ band.

    Window analysis for closes[-4:] = [11, 9, 11, 10]:
        mean=10.25, pstdev≈0.829, lower_band≈9.421
        current_close=10 ≥ 9.421 → NOT (current < band) → HOLD
    """
    return [9.0, 11.0, 9.0, 11.0, 9.0, 11.0, 10.0]


def _min_rr_closes() -> list[float]:
    """7 bars with stop_pct=0.15: take_profit uses min_rr floor, not mean."""
    return [5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 3.8]


def _market(closes: list[float], symbol: str = "TEST", current_price: float | None = None) -> MarketData:
    bars = make_bars_from_closes(closes)
    price = Decimal(str(current_price)) if current_price is not None else bars[-1].close
    return MarketData(
        symbol=symbol,
        current_price=price,
        bars=bars,
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInit:
    def test_default_config_parses_correctly(self):
        s = MeanReversionStrategy(config={})
        assert s.lookback == 20
        assert s.z_score_threshold == Decimal("2.0")
        assert s.stop_loss_pct == Decimal("0.02")

    def test_custom_config_overrides_defaults(self):
        s = MeanReversionStrategy(
            config={"lookback": 10, "z_score_threshold": "1.5", "stop_loss_pct": "0.03"}
        )
        assert s.lookback == 10
        assert s.z_score_threshold == Decimal("1.5")
        assert s.stop_loss_pct == Decimal("0.03")

    def test_lookback_less_than_2_raises(self):
        with pytest.raises(ValueError, match="lookback"):
            MeanReversionStrategy(config={"lookback": 1})

    def test_lookback_zero_raises(self):
        with pytest.raises(ValueError, match="lookback"):
            MeanReversionStrategy(config={"lookback": 0})

    def test_z_score_zero_raises(self):
        with pytest.raises(ValueError, match="z_score_threshold"):
            MeanReversionStrategy(config={"z_score_threshold": "0"})

    def test_z_score_negative_raises(self):
        with pytest.raises(ValueError, match="z_score_threshold"):
            MeanReversionStrategy(config={"z_score_threshold": "-1.0"})

    def test_stop_loss_pct_zero_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pct"):
            MeanReversionStrategy(config={"stop_loss_pct": "0"})

    def test_stop_loss_pct_one_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pct"):
            MeanReversionStrategy(config={"stop_loss_pct": "1"})

    def test_stop_loss_pct_above_one_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pct"):
            MeanReversionStrategy(config={"stop_loss_pct": "1.5"})


# ---------------------------------------------------------------------------
# HOLD conditions
# ---------------------------------------------------------------------------


class TestHoldConditions:
    @pytest.mark.asyncio
    async def test_hold_when_insufficient_bars(self):
        """Fewer than lookback+1 bars → HOLD."""
        s = _strategy(lookback=4)
        # Need 5 bars; supply only 4
        data = _market([10.0, 10.0, 10.0, 10.0])
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_price_within_bands(self):
        """Price stays within ±1σ of mean → HOLD."""
        s = _strategy()
        signal = await s.generate_signal(_market(_within_bands_closes()))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_already_below_band_on_previous_bar(self):
        """
        Price was already below the lower band on the previous bar.
        Crossover condition fails → HOLD (prevents re-entry into open deviation).
        """
        s = _strategy()
        signal = await s.generate_signal(_market(_already_below_closes()))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_exactly_at_minimum_bar_count_minus_one(self):
        """One bar short of the minimum required → HOLD."""
        s = _strategy(lookback=10)
        data = _market([100.0] * 10)  # need 11, have 10
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_on_flat_market_no_deviation(self):
        """Flat market with zero std → lower_band = mean = price → no deviation."""
        s = _strategy()
        data = _market([100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"


# ---------------------------------------------------------------------------
# BUY signal generation
# ---------------------------------------------------------------------------


class TestBuySignal:
    @pytest.mark.asyncio
    async def test_buy_on_first_deviation_crossover(self):
        """Price crosses below lower band for first time → BUY."""
        s = _strategy()
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_buy_signal_entry_price_is_current_price(self):
        """entry_price equals market_data.current_price, not last bar close."""
        s = _strategy()
        # current_price slightly different from last close to confirm the right field is used
        data = _market(_buy_cross_closes(), current_price=4.05)
        signal = await s.generate_signal(data)
        assert signal.action == "BUY"
        assert signal.entry_price == Decimal("4.05")

    @pytest.mark.asyncio
    async def test_buy_signal_stop_loss_placed_below_entry(self):
        """stop_loss = entry × (1 − stop_loss_pct), rounded to 2dp."""
        s = _strategy(stop_loss_pct="0.02")
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.action == "BUY"
        # entry=4.0, stop = 4.0 × 0.98 = 3.92
        assert signal.stop_loss_price == Decimal("3.92")

    @pytest.mark.asyncio
    async def test_buy_signal_stop_loss_is_below_entry(self):
        """stop_loss_price must always be strictly less than entry_price."""
        s = _strategy()
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.action == "BUY"
        assert signal.stop_loss_price < signal.entry_price

    @pytest.mark.asyncio
    async def test_buy_signal_take_profit_is_rolling_mean_when_mean_is_higher(self):
        """
        take_profit = rolling mean when mean > entry + 2 × stop_distance.

        bars = [10, 10, 10, 10, 10, 10, 4], entry=4.0, stop=3.92
        stop_dist = 0.08, min_take_profit = 4.16
        mean of current window [10, 10, 10, 4] = 8.50 > 4.16
        → take_profit = 8.50
        """
        s = _strategy(stop_loss_pct="0.02")
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.action == "BUY"
        assert signal.take_profit_price == Decimal("8.50")

    @pytest.mark.asyncio
    async def test_buy_signal_take_profit_uses_min_rr_floor_when_mean_is_close(self):
        """
        take_profit = entry + 2 × stop_dist when the rolling mean is below that floor.

        bars = [5, 5, 5, 5, 5, 5, 3.8], stop_pct=0.15
        entry=3.8, stop=3.23, stop_dist=0.57
        min_take_profit = 3.8 + 1.14 = 4.94
        mean of current window [5, 5, 5, 3.8] = 4.70 < 4.94
        → take_profit = 4.94
        """
        s = _strategy(stop_loss_pct="0.15")
        signal = await s.generate_signal(_market(_min_rr_closes()))
        assert signal.action == "BUY"
        assert signal.take_profit_price == Decimal("4.94")

    @pytest.mark.asyncio
    async def test_buy_signal_take_profit_above_entry(self):
        """take_profit must always be strictly above entry_price."""
        s = _strategy()
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.action == "BUY"
        assert signal.take_profit_price > signal.entry_price

    @pytest.mark.asyncio
    async def test_buy_signal_take_profit_satisfies_min_rr(self):
        """
        take_profit − entry ≥ 2 × (entry − stop_loss) in all cases.
        This mirrors the RiskManager's minimum 2:1 R:R gate.
        """
        s = _strategy()
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.action == "BUY"
        stop_dist = signal.entry_price - signal.stop_loss_price
        reward = signal.take_profit_price - signal.entry_price
        assert reward >= stop_dist * 2

    @pytest.mark.asyncio
    async def test_buy_signal_symbol_matches_market_data(self):
        s = _strategy()
        data = _market(_buy_cross_closes(), symbol="AAPL")
        signal = await s.generate_signal(data)
        assert signal.symbol == "AAPL"

    @pytest.mark.asyncio
    async def test_buy_signal_timestamp_is_set(self):
        s = _strategy()
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.action == "BUY"
        assert signal.timestamp is not None

    @pytest.mark.asyncio
    async def test_no_signal_on_bar_immediately_after_crossover(self):
        """
        Once price is below band on two consecutive bars, HOLD (no repeat entry).

        bars = [10, 10, 10, 10, 10, 4, 4] — prev and current both below band.
        """
        s = _strategy()
        signal = await s.generate_signal(_market(_already_below_closes()))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_custom_z_score_narrows_band(self):
        """A larger z_score_threshold means a larger deviation is needed for a signal."""
        # With z=3.0 the lower band is further below — same data should return HOLD
        s_tight = _strategy(z_score_threshold="0.1")   # fires very easily
        s_wide = _strategy(z_score_threshold="3.0")    # fires only on extreme deviations

        data = _market(_buy_cross_closes())

        signal_tight = await s_tight.generate_signal(data)
        signal_wide = await s_wide.generate_signal(data)

        # A tight band catches the deviation; a very wide band may miss it
        assert signal_tight.action == "BUY"
        # The wide-band strategy should HOLD on this modest deviation
        assert signal_wide.action == "HOLD"


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


class TestPositionSizing:
    @pytest.mark.asyncio
    async def test_calculate_position_size_uses_1pct_rule(self):
        """
        max_quantity = floor((balance × 0.01) / (entry − stop))

        balance=100_000, entry=4.0, stop=3.92 → risk_per_share=0.08
        max_qty = floor(1_000 / 0.08) = 12_500
        """
        s = _strategy()
        qty = await s.calculate_position_size(
            RiskParams(
                account_balance=Decimal("100000"),
                entry_price=Decimal("4.0"),
                stop_loss_price=Decimal("3.92"),
            )
        )
        assert qty == 12_500

    @pytest.mark.asyncio
    async def test_calculate_position_size_returns_zero_on_invalid_stop(self):
        """stop_loss_price ≥ entry_price → RiskCalculator raises → returns 0."""
        s = _strategy()
        qty = await s.calculate_position_size(
            RiskParams(
                account_balance=Decimal("100000"),
                entry_price=Decimal("100"),
                stop_loss_price=Decimal("105"),  # stop above entry — invalid
            )
        )
        assert qty == 0


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class TestConfigSchema:
    def test_schema_returns_dict_with_required_keys(self):
        s = _strategy()
        schema = s.get_config_schema()
        assert isinstance(schema, dict)
        assert "properties" in schema
        props = schema["properties"]
        assert "lookback" in props
        assert "z_score_threshold" in props
        assert "stop_loss_pct" in props
        assert "symbols" in props

    def test_schema_defaults_match_instance_defaults(self):
        s = MeanReversionStrategy(config={})
        schema = s.get_config_schema()
        props = schema["properties"]
        assert props["lookback"]["default"] == 20
        assert props["z_score_threshold"]["default"] == "2.0"
        assert props["stop_loss_pct"]["default"] == "0.02"


# ---------------------------------------------------------------------------
# Registry self-registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_mean_reversion_registered_in_registry(self):
        from app.core.strategy_engine.registry import registry
        assert registry.is_registered("mean_reversion")

    def test_registry_builds_correct_class(self):
        from app.core.strategy_engine.registry import registry
        s = registry.build("mean_reversion", {"lookback": 10})
        assert isinstance(s, MeanReversionStrategy)
        assert s.lookback == 10

    def test_registered_class_is_mean_reversion_strategy(self):
        from app.core.strategy_engine.registry import registry
        cls = registry.get_class("mean_reversion")
        assert cls is MeanReversionStrategy


# ---------------------------------------------------------------------------
# Utility function tests
# ---------------------------------------------------------------------------


class TestUtilityFunctions:
    def test_mean_of_uniform_values(self):
        vals = [Decimal("10")] * 5
        assert _mean(vals) == Decimal("10")

    def test_mean_of_mixed_values(self):
        vals = [Decimal("10"), Decimal("10"), Decimal("10"), Decimal("4")]
        assert _mean(vals) == Decimal("8.5")

    def test_pstdev_of_uniform_values_is_zero(self):
        vals = [Decimal("10")] * 4
        assert _pstdev(vals) == Decimal("0")

    def test_pstdev_of_known_values(self):
        # [10, 10, 10, 4]: mean=8.5, variance=(2.25+2.25+2.25+20.25)/4=6.75, std=√6.75≈2.598
        vals = [Decimal("10"), Decimal("10"), Decimal("10"), Decimal("4")]
        result = _pstdev(vals)
        # Compare to float computation for sanity
        import math
        expected = Decimal(str(math.sqrt(6.75)))
        # Allow ±0.0001 tolerance
        assert abs(result - expected) < Decimal("0.0001")
