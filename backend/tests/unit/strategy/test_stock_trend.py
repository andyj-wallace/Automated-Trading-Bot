"""
Unit tests for StockTrendStrategy.

Uses small ma_period values (4) to keep bar sequences short and crossover
conditions precisely controllable. All numeric expectations are pre-computed
analytically — see comments for the derivation.

Test data conventions:
    All sequences use ma_period=4, stop_loss_pct="0.03" unless noted.

BUY crossover case — bars = [9, 9, 9, 9, 9, 9, 10.5]  (7 bars, ma_period=4):
    Previous window closes[-5:-1] = [9, 9, 9, 9]:
        ma_prev = SMA([9, 9, 9, 9]) = 9.0
        prev_close = 9 ≤ 9.0 ✓
    Current window closes[-4:] = [9, 9, 9, 10.5]:
        ma_now = SMA([9, 9, 9, 10.5]) = 37.5/4 = 9.375
        current_close = 10.5 > 9.375 ✓
    → BUY at entry=10.5, stop=10.5×0.97=10.185→10.19,
      stop_dist=0.31, take_profit=10.5+0.62=11.12

Already-above case — bars = [9, 9, 9, 9, 9, 10.5, 10.8]:
    Previous window [9, 9, 9, 10.5]: ma_prev = 9.375
    prev_close = 10.5 > 9.375 → NOT (prev_close ≤ ma_prev) → HOLD

Price-below case — bars = [10, 10, 10, 10, 10, 10, 9.0]:
    Current window [10, 10, 10, 9.0]: ma_now = 9.75
    current_close = 9.0 ≤ 9.75 → NOT (current_close > ma_now) → HOLD
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.strategy_engine.base import MarketData, RiskParams
from app.core.strategy_engine.stock_trend import StockTrendStrategy, _sma

from .conftest import make_bars_from_closes, make_market_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strategy(**kwargs) -> StockTrendStrategy:
    """Return a strategy with small ma_period for test controllability."""
    defaults = {
        "ma_period": 4,
        "stop_loss_pct": "0.03",
    }
    defaults.update(kwargs)
    return StockTrendStrategy(config=defaults)


def _buy_cross_closes() -> list[float]:
    """7 bars: price crosses above MA on the last bar."""
    return [9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 10.5]


def _already_above_closes() -> list[float]:
    """7 bars: price was already above MA on prev bar — no crossover."""
    return [9.0, 9.0, 9.0, 9.0, 9.0, 10.5, 10.8]


def _price_below_closes() -> list[float]:
    """7 bars: price stays below the MA throughout — HOLD."""
    return [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 9.0]


def _market(
    closes: list[float],
    symbol: str = "TEST",
    current_price: float | None = None,
) -> MarketData:
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
        s = StockTrendStrategy(config={})
        assert s.ma_period == 200
        assert s.stop_loss_pct == Decimal("0.03")

    def test_custom_config_overrides_defaults(self):
        s = StockTrendStrategy(
            config={"ma_period": 50, "stop_loss_pct": "0.05"}
        )
        assert s.ma_period == 50
        assert s.stop_loss_pct == Decimal("0.05")

    def test_ma_period_zero_raises(self):
        with pytest.raises(ValueError, match="ma_period"):
            StockTrendStrategy(config={"ma_period": 0})

    def test_ma_period_negative_raises(self):
        with pytest.raises(ValueError, match="ma_period"):
            StockTrendStrategy(config={"ma_period": -1})

    def test_stop_loss_pct_zero_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pct"):
            StockTrendStrategy(config={"stop_loss_pct": "0"})

    def test_stop_loss_pct_one_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pct"):
            StockTrendStrategy(config={"stop_loss_pct": "1"})

    def test_stop_loss_pct_above_one_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pct"):
            StockTrendStrategy(config={"stop_loss_pct": "1.5"})


# ---------------------------------------------------------------------------
# HOLD conditions
# ---------------------------------------------------------------------------


class TestHoldConditions:
    @pytest.mark.asyncio
    async def test_hold_when_insufficient_bars(self):
        """Fewer than ma_period+1 bars → HOLD."""
        s = _strategy(ma_period=4)
        data = _market([10.0, 10.0, 10.0, 10.0])  # need 5, have 4
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_exactly_at_minimum_bar_count_minus_one(self):
        """One bar short of the minimum required → HOLD."""
        s = _strategy(ma_period=10)
        data = _market([100.0] * 10)  # need 11, have 10
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_price_below_ma(self):
        """Price stays below the MA → HOLD (downtrend, no long entry)."""
        s = _strategy()
        signal = await s.generate_signal(_market(_price_below_closes()))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_already_above_ma_on_previous_bar(self):
        """
        Price was already above the MA on the previous bar.
        Crossover condition fails → HOLD (prevents re-entry in established uptrend).
        """
        s = _strategy()
        signal = await s.generate_signal(_market(_already_above_closes()))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_price_exactly_at_ma(self):
        """Price equals the MA (not strictly above) → HOLD."""
        # All-flat: SMA([10,10,10,10]) = 10, current_close = 10
        # 10 > 10 is False → HOLD
        s = _strategy()
        data = _market([10.0] * 7)
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_on_bar_immediately_after_crossover(self):
        """
        On the bar after the crossover, price is already above the MA on prev bar.
        bars = [9,9,9,9,9,10.5,10.8] → prev_close=10.5 > ma_prev=9.375 → HOLD.
        """
        s = _strategy()
        signal = await s.generate_signal(_market([9.0, 9.0, 9.0, 9.0, 9.0, 10.5, 10.8]))
        assert signal.action == "HOLD"


# ---------------------------------------------------------------------------
# BUY signal
# ---------------------------------------------------------------------------


class TestBuySignal:
    @pytest.mark.asyncio
    async def test_buy_on_first_crossover_above_ma(self):
        """Price just crosses above MA → BUY."""
        s = _strategy()
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_buy_signal_entry_price_is_current_price(self):
        """entry_price matches current_price in MarketData."""
        s = _strategy()
        data = _market(_buy_cross_closes(), current_price=10.5)
        signal = await s.generate_signal(data)
        assert signal.entry_price == Decimal("10.5")

    @pytest.mark.asyncio
    async def test_buy_signal_stop_loss_is_below_entry(self):
        """stop_loss_price < entry_price (long position, stop below entry)."""
        s = _strategy()
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.stop_loss_price is not None
        assert signal.stop_loss_price < signal.entry_price

    @pytest.mark.asyncio
    async def test_buy_signal_stop_loss_placed_correctly(self):
        """
        stop = entry × (1 − stop_loss_pct), rounded to 2 dp.

        entry=10.5, stop_loss_pct=0.03:
          stop = 10.5 × 0.97 = 10.185 → rounds to 10.19
        """
        s = _strategy(stop_loss_pct="0.03")
        data = _market(_buy_cross_closes(), current_price=10.5)
        signal = await s.generate_signal(data)
        assert signal.stop_loss_price == Decimal("10.19")

    @pytest.mark.asyncio
    async def test_buy_signal_take_profit_above_entry(self):
        """take_profit_price > entry_price."""
        s = _strategy()
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.take_profit_price is not None
        assert signal.take_profit_price > signal.entry_price

    @pytest.mark.asyncio
    async def test_buy_signal_take_profit_satisfies_min_rr(self):
        """
        take_profit must satisfy at least 2:1 R:R.

        entry=10.5, stop=10.19, stop_dist=0.31:
          min_take_profit = 10.5 + 0.31×2 = 11.12
        """
        s = _strategy(stop_loss_pct="0.03")
        data = _market(_buy_cross_closes(), current_price=10.5)
        signal = await s.generate_signal(data)
        stop_dist = signal.entry_price - signal.stop_loss_price
        min_tp = signal.entry_price + stop_dist * 2
        assert signal.take_profit_price >= min_tp

    @pytest.mark.asyncio
    async def test_buy_signal_take_profit_exact_value(self):
        """
        take_profit = entry + 2 × stop_distance, rounded to 2 dp.

        entry=10.5, stop=10.19, stop_dist=0.31, take_profit=10.5+0.62=11.12
        """
        s = _strategy(stop_loss_pct="0.03")
        data = _market(_buy_cross_closes(), current_price=10.5)
        signal = await s.generate_signal(data)
        assert signal.take_profit_price == Decimal("11.12")

    @pytest.mark.asyncio
    async def test_buy_signal_symbol_matches_market_data(self):
        """Signal symbol matches the symbol in MarketData."""
        s = _strategy()
        data = _market(_buy_cross_closes(), symbol="AAPL")
        signal = await s.generate_signal(data)
        assert signal.symbol == "AAPL"

    @pytest.mark.asyncio
    async def test_buy_signal_timestamp_is_set(self):
        """Signal timestamp is a timezone-aware datetime."""
        s = _strategy()
        signal = await s.generate_signal(_market(_buy_cross_closes()))
        assert signal.timestamp is not None
        assert signal.timestamp.tzinfo is not None

    @pytest.mark.asyncio
    async def test_buy_signal_with_prev_close_exactly_at_ma(self):
        """
        prev_close == ma_prev satisfies the ≤ condition — BUY triggers.

        bars = [9,9,9,9,9,9,10.5]: prev_close=9 == ma_prev=9 → crossover ✓
        """
        s = _strategy()
        signal = await s.generate_signal(_market([9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 10.5]))
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_custom_ma_period(self):
        """Smaller ma_period works correctly — BUY detected."""
        s = _strategy(ma_period=2)
        # Need 3 bars: prev_close=9<=ma_prev=9, current=11>ma_now=(9+11)/2=10
        data = _market([9.0, 9.0, 11.0])
        signal = await s.generate_signal(data)
        assert signal.action == "BUY"


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
        assert "properties" in schema
        assert "ma_period" in schema["properties"]
        assert "stop_loss_pct" in schema["properties"]
        assert "symbols" in schema["properties"]

    def test_schema_defaults_match_instance_defaults(self):
        s = StockTrendStrategy(config={})
        schema = s.get_config_schema()
        assert schema["properties"]["ma_period"]["default"] == 200
        assert schema["properties"]["stop_loss_pct"]["default"] == "0.03"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_stock_trend_registered_in_registry(self):
        from app.core.strategy_engine.registry import registry
        assert "stock_trend" in registry.registered_types()

    def test_registry_builds_correct_class(self):
        from app.core.strategy_engine.registry import registry
        instance = registry.build("stock_trend", {"ma_period": 50})
        assert isinstance(instance, StockTrendStrategy)

    def test_registered_class_is_stock_trend_strategy(self):
        from app.core.strategy_engine.registry import registry
        cls = registry._classes["stock_trend"]
        assert cls is StockTrendStrategy


# ---------------------------------------------------------------------------
# Utility function
# ---------------------------------------------------------------------------


class TestUtilityFunction:
    def test_sma_of_uniform_values(self):
        values = [Decimal("10")] * 5
        assert _sma(values, 5) == Decimal("10")

    def test_sma_of_mixed_values(self):
        # [9, 9, 9, 10.5] → 37.5 / 4 = 9.375
        values = [Decimal("9"), Decimal("9"), Decimal("9"), Decimal("10.5")]
        result = _sma(values, 4)
        assert result == Decimal("9.375")

    def test_sma_uses_only_last_period_values(self):
        # 6 values but period=4: only last 4 should count
        values = [Decimal("1"), Decimal("2"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10")]
        result = _sma(values, 4)
        assert result == Decimal("10")

    def test_sma_period_one_returns_last_value(self):
        values = [Decimal("5"), Decimal("10"), Decimal("15")]
        assert _sma(values, 1) == Decimal("15")
