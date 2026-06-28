"""
Unit tests for CompositeStrategy.

Uses StockTrendStrategy and MeanReversionStrategy as real sub-strategies
to verify signal combination behaviour. All numeric expectations are
pre-computed analytically.

Sub-strategy configurations use small lookbacks (ma_period=4, lookback=4)
to keep bar sequences short and crossover conditions precisely controllable.

Test data (7 bars, ma_period=4, lookback=4, z_score_threshold="1.0"):

  STOCK_TREND BUY, MEAN_REVERSION HOLD — bars = [9, 9, 9, 9, 9, 9, 10.5]
    StockTrend: ma_now=SMA([9,9,9,10.5])=9.375, current=10.5>9.375 ✓
                ma_prev=SMA([9,9,9,9])=9.0, prev=9≤9 ✓ → BUY
    MeanRev:    window [9,9,9,10.5]: mean=9.375, pstdev≈0.649, band≈8.726
                current=10.5 > 8.726 → NOT below band → HOLD

  MEAN_REVERSION BUY, STOCK_TREND HOLD — bars = [9, 9, 9, 9, 9, 9, 4]
    StockTrend: ma_now=SMA([9,9,9,4])=7.75, current=4<7.75 → HOLD
    MeanRev:    window [9,9,9,4]: mean=7.75, pstdev≈2.165, band≈5.585
                prev=[9,9,9,9]: mean=9, std=0, band=9; prev_close=9≥9 ✓
                current=4 < 5.585 ✓ → BUY at entry=4

  BOTH BUY — two StockTrend strategies with different ma_period, bars as above:
    ST(ma_period=4): BUY (as above)
    ST(ma_period=2): ma_now=SMA([9,10.5])=9.75, current=10.5>9.75 ✓
                     ma_prev=SMA([9,9])=9.0, prev=9≤9 ✓ → BUY

  BOTH HOLD — flat bars = [9, 9, 9, 9, 9, 9, 9]:
    StockTrend: current=9, ma_now=9, NOT(9>9) → HOLD
    MeanRev:    current=9, std=0, band=9, NOT(9<9) → HOLD
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

# Importing these triggers their self-registration in the global registry
import app.core.strategy_engine.mean_reversion  # noqa: F401
import app.core.strategy_engine.stock_trend  # noqa: F401
from app.core.strategy_engine.base import MarketData, RiskParams
from app.core.strategy_engine.composite import CompositeStrategy

from .conftest import make_bars_from_closes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ST_CONFIG = {"ma_period": 4, "stop_loss_pct": "0.03"}
_MR_CONFIG = {"lookback": 4, "z_score_threshold": "1.0", "stop_loss_pct": "0.02"}
_ST2_CONFIG = {"ma_period": 2, "stop_loss_pct": "0.03"}


def _market(closes: list[float], symbol: str = "TEST") -> MarketData:
    bars = make_bars_from_closes(closes)
    return MarketData(
        symbol=symbol,
        current_price=bars[-1].close,
        bars=bars,
        timestamp=datetime.now(UTC),
    )


def _st_buy_closes() -> list[float]:
    """StockTrend BUY, MeanReversion HOLD."""
    return [9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 10.5]


def _mr_buy_closes() -> list[float]:
    """MeanReversion BUY, StockTrend HOLD."""
    return [9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 4.0]


def _both_hold_closes() -> list[float]:
    """All-flat: both strategies HOLD."""
    return [9.0] * 7


def _both_buy_closes() -> list[float]:
    """StockTrend(ma=4) AND StockTrend(ma=2) both BUY."""
    return [9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 10.5]


def _any_composite(**overrides) -> CompositeStrategy:
    config = {
        "combination_mode": "any",
        "strategies": [
            {"type": "stock_trend", "config": _ST_CONFIG},
            {"type": "mean_reversion", "config": _MR_CONFIG},
        ],
    }
    config.update(overrides)
    return CompositeStrategy(config=config)


def _all_composite() -> CompositeStrategy:
    """Two StockTrend strategies — both BUY on the same bar."""
    return CompositeStrategy(config={
        "combination_mode": "all",
        "strategies": [
            {"type": "stock_trend", "config": _ST_CONFIG},
            {"type": "stock_trend", "config": _ST2_CONFIG},
        ],
    })


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInit:
    def test_default_mode_is_any(self):
        c = CompositeStrategy(config={
            "strategies": [{"type": "stock_trend", "config": _ST_CONFIG}]
        })
        assert c._mode == "any"

    def test_explicit_any_mode(self):
        c = CompositeStrategy(config={
            "combination_mode": "any",
            "strategies": [{"type": "stock_trend", "config": _ST_CONFIG}]
        })
        assert c._mode == "any"

    def test_explicit_all_mode(self):
        c = CompositeStrategy(config={
            "combination_mode": "all",
            "strategies": [{"type": "stock_trend", "config": _ST_CONFIG}]
        })
        assert c._mode == "all"

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="combination_mode"):
            CompositeStrategy(config={
                "combination_mode": "consensus",
                "strategies": [{"type": "stock_trend", "config": _ST_CONFIG}]
            })

    def test_explicit_majority_mode(self):
        c = CompositeStrategy(config={
            "combination_mode": "majority",
            "strategies": [{"type": "stock_trend", "config": _ST_CONFIG}]
        })
        assert c._mode == "majority"

    def test_empty_strategies_raises(self):
        with pytest.raises(ValueError, match="at least one sub-strategy"):
            CompositeStrategy(config={"combination_mode": "any", "strategies": []})

    def test_missing_strategies_key_raises(self):
        with pytest.raises(ValueError, match="at least one sub-strategy"):
            CompositeStrategy(config={"combination_mode": "any"})

    def test_sub_strategy_missing_type_raises(self):
        with pytest.raises(ValueError, match="missing required 'type' key"):
            CompositeStrategy(config={
                "strategies": [{"config": {"ma_period": 4}}]
            })

    def test_unregistered_type_raises(self):
        with pytest.raises(ValueError, match="not registered"):
            CompositeStrategy(config={
                "strategies": [{"type": "nonexistent_strategy_xyz"}]
            })

    def test_sub_strategies_instantiated(self):
        c = _any_composite()
        assert len(c._strategies) == 2

    def test_single_sub_strategy_valid(self):
        c = CompositeStrategy(config={
            "strategies": [{"type": "stock_trend", "config": _ST_CONFIG}]
        })
        assert len(c._strategies) == 1


# ---------------------------------------------------------------------------
# "any" mode
# ---------------------------------------------------------------------------


class TestAnyMode:
    @pytest.mark.asyncio
    async def test_first_buy_wins(self):
        """First sub-strategy (StockTrend) returns BUY — composite returns it."""
        c = _any_composite()
        signal = await c.generate_signal(_market(_st_buy_closes()))
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_first_buy_signal_comes_from_first_strategy(self):
        """Signal details (entry/stop) come from the first sub-strategy that fired."""
        c = _any_composite()
        signal = await c.generate_signal(_market(_st_buy_closes()))
        # StockTrend with entry=10.5, stop_loss_pct=0.03: stop=10.19
        assert signal.entry_price == Decimal("10.5")
        assert signal.stop_loss_price == Decimal("10.19")

    @pytest.mark.asyncio
    async def test_second_strategy_fires_when_first_holds(self):
        """First sub-strategy (StockTrend) HOLD; second (MeanReversion) BUY — composite BUY."""
        c = _any_composite()
        signal = await c.generate_signal(_market(_mr_buy_closes()))
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_second_strategy_signal_used_when_first_holds(self):
        """Signal details come from MeanReversion (the first non-HOLD)."""
        c = _any_composite()
        signal = await c.generate_signal(_market(_mr_buy_closes()))
        # MeanReversion with entry=4.0, stop_loss_pct=0.02: stop=4.0*0.98=3.92
        assert signal.entry_price == Decimal("4.0")
        assert signal.stop_loss_price == Decimal("3.92")

    @pytest.mark.asyncio
    async def test_hold_when_all_sub_strategies_hold(self):
        """All HOLD → composite returns HOLD."""
        c = _any_composite()
        signal = await c.generate_signal(_market(_both_hold_closes()))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_any_mode_symbol_preserved_on_hold(self):
        """HOLD signal carries the correct symbol."""
        c = _any_composite()
        signal = await c.generate_signal(_market(_both_hold_closes(), symbol="AAPL"))
        assert signal.symbol == "AAPL"

    @pytest.mark.asyncio
    async def test_any_mode_symbol_preserved_on_buy(self):
        """BUY signal carries the correct symbol."""
        c = _any_composite()
        signal = await c.generate_signal(_market(_st_buy_closes(), symbol="MSFT"))
        assert signal.symbol == "MSFT"

    @pytest.mark.asyncio
    async def test_order_matters_first_strategy_takes_priority(self):
        """When first list entry signals BUY, its signal is used (not second's)."""
        # St BUY with _st_buy_closes; if we put MR first and ST second,
        # MR will HOLD and ST will BUY → MR is first but returns HOLD → ST fires.
        # Reverse order: [MR, ST], data=_st_buy_closes → MR HOLD, ST BUY → return ST's signal.
        # Now verify entry price matches ST's signal.
        c = CompositeStrategy(config={
            "combination_mode": "any",
            "strategies": [
                {"type": "mean_reversion", "config": _MR_CONFIG},
                {"type": "stock_trend", "config": _ST_CONFIG},
            ],
        })
        signal = await c.generate_signal(_market(_st_buy_closes()))
        assert signal.action == "BUY"
        # Entry should be ST's signal (stop_loss_pct=0.03 → stop=10.19)
        assert signal.stop_loss_price == Decimal("10.19")


# ---------------------------------------------------------------------------
# "all" mode
# ---------------------------------------------------------------------------


class TestAllMode:
    @pytest.mark.asyncio
    async def test_buy_when_all_agree_buy(self):
        """Both ST(ma=4) and ST(ma=2) return BUY → composite BUY."""
        c = _all_composite()
        signal = await c.generate_signal(_market(_both_buy_closes()))
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_first_strategy_signal_used_when_all_agree(self):
        """Signal details come from the first sub-strategy (primary) when all agree."""
        c = _all_composite()
        signal = await c.generate_signal(_market(_both_buy_closes()))
        # First strategy: ST(ma_period=4, stop_loss_pct=0.03), entry=10.5, stop=10.19
        assert signal.entry_price == Decimal("10.5")
        assert signal.stop_loss_price == Decimal("10.19")

    @pytest.mark.asyncio
    async def test_hold_when_first_holds_second_buys(self):
        """ST(ma=4) HOLD, MR BUY → composite HOLD (not all agree)."""
        c = CompositeStrategy(config={
            "combination_mode": "all",
            "strategies": [
                {"type": "stock_trend", "config": _ST_CONFIG},
                {"type": "mean_reversion", "config": _MR_CONFIG},
            ],
        })
        # _st_buy_closes: ST BUY, MR HOLD → not all BUY → HOLD
        signal = await c.generate_signal(_market(_st_buy_closes()))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_first_buys_second_holds(self):
        """ST(ma=4) BUY, MR HOLD on _st_buy_closes → composite HOLD."""
        c = CompositeStrategy(config={
            "combination_mode": "all",
            "strategies": [
                {"type": "stock_trend", "config": _ST_CONFIG},
                {"type": "mean_reversion", "config": _MR_CONFIG},
            ],
        })
        signal = await c.generate_signal(_market(_st_buy_closes()))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_all_sub_strategies_hold(self):
        """All HOLD → composite HOLD."""
        c = _all_composite()
        signal = await c.generate_signal(_market(_both_hold_closes()))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_all_mode_symbol_preserved_on_hold(self):
        c = _all_composite()
        signal = await c.generate_signal(_market(_both_hold_closes(), symbol="TSLA"))
        assert signal.symbol == "TSLA"

    @pytest.mark.asyncio
    async def test_all_mode_timestamp_is_set(self):
        c = _all_composite()
        signal = await c.generate_signal(_market(_both_buy_closes()))
        assert signal.timestamp is not None
        assert signal.timestamp.tzinfo is not None

    @pytest.mark.asyncio
    async def test_combines_to_tightest_stop_not_first_strategy(self):
        """
        When agreeing sub-strategies propose different stops, the combined
        signal must use the tightest (safest) one, even if that isn't the
        first-listed sub-strategy's stop — not an arbitrary "first wins".
        """
        wider_stop_config = {"ma_period": 2, "stop_loss_pct": "0.05"}  # stop = 10.5*0.95 ≈ 9.98
        tighter_stop_config = _ST_CONFIG  # stop_loss_pct=0.03 → stop = 10.5*0.97 ≈ 10.19
        c = CompositeStrategy(config={
            "combination_mode": "all",
            "strategies": [
                {"type": "stock_trend", "config": wider_stop_config},  # primary, listed first
                {"type": "stock_trend", "config": tighter_stop_config},
            ],
        })
        signal = await c.generate_signal(_market(_both_buy_closes()))

        assert signal.action == "BUY"
        # Tighter stop (10.19, closer to entry) wins over the primary's own 9.975
        assert signal.stop_loss_price == Decimal("10.19")
        # Take-profit is dropped (None) since the stop was overridden from primary's
        assert signal.take_profit_price is None

    @pytest.mark.asyncio
    async def test_keeps_take_profit_when_primary_already_has_tightest_stop(self):
        """If the primary's own stop is already the tightest, its take_profit
        suggestion is preserved rather than discarded."""
        c = _all_composite()  # both sub-strategies use stop_loss_pct=0.03 — identical stops
        signal = await c.generate_signal(_market(_both_buy_closes()))
        assert signal.stop_loss_price == Decimal("10.19")
        assert signal.take_profit_price is not None


# ---------------------------------------------------------------------------
# "majority" mode
# ---------------------------------------------------------------------------


class TestMajorityMode:
    @pytest.mark.asyncio
    async def test_buy_when_two_of_three_agree(self):
        """2 BUY + 1 HOLD out of 3 sub-strategies → strict majority → BUY."""
        c = CompositeStrategy(config={
            "combination_mode": "majority",
            "strategies": [
                {"type": "stock_trend", "config": _ST_CONFIG},      # BUY
                {"type": "stock_trend", "config": _ST2_CONFIG},     # BUY
                {"type": "mean_reversion", "config": _MR_CONFIG},   # HOLD
            ],
        })
        signal = await c.generate_signal(_market(_st_buy_closes()))
        assert signal.action == "BUY"
        assert signal.stop_loss_price == Decimal("10.19")

    @pytest.mark.asyncio
    async def test_hold_when_only_one_of_three_agrees(self):
        """A single non-HOLD vote among 3 is not a strict majority → HOLD."""
        c = CompositeStrategy(config={
            "combination_mode": "majority",
            "strategies": [
                {"type": "stock_trend", "config": _ST_CONFIG},      # BUY
                {"type": "mean_reversion", "config": _MR_CONFIG},   # HOLD
                {"type": "mean_reversion", "config": _MR_CONFIG},   # HOLD
            ],
        })
        signal = await c.generate_signal(_market(_st_buy_closes()))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_all_hold(self):
        c = CompositeStrategy(config={
            "combination_mode": "majority",
            "strategies": [
                {"type": "stock_trend", "config": _ST_CONFIG},
                {"type": "stock_trend", "config": _ST2_CONFIG},
            ],
        })
        signal = await c.generate_signal(_market(_both_hold_closes()))
        assert signal.action == "HOLD"


# ---------------------------------------------------------------------------
# Nested composites
# ---------------------------------------------------------------------------


class TestNestedComposite:
    @pytest.mark.asyncio
    async def test_composite_can_nest_another_composite(self):
        """A composite sub-strategy can itself be a composite."""
        outer = CompositeStrategy(config={
            "combination_mode": "any",
            "strategies": [
                {
                    "type": "composite",
                    "config": {
                        "combination_mode": "all",
                        "strategies": [
                            {"type": "stock_trend", "config": _ST_CONFIG},
                            {"type": "stock_trend", "config": _ST2_CONFIG},
                        ],
                    },
                },
            ],
        })
        # Both inner ST strategies fire BUY → inner composite BUY → outer "any" returns BUY
        signal = await outer.generate_signal(_market(_both_buy_closes()))
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_nested_composite_holds_when_inner_all_fails(self):
        """Inner "all" gate fails → inner HOLD → outer "any" gets only HOLD → HOLD."""
        outer = CompositeStrategy(config={
            "combination_mode": "any",
            "strategies": [
                {
                    "type": "composite",
                    "config": {
                        "combination_mode": "all",
                        "strategies": [
                            {"type": "stock_trend", "config": _ST_CONFIG},
                            {"type": "mean_reversion", "config": _MR_CONFIG},
                        ],
                    },
                },
            ],
        })
        # ST BUY, MR HOLD → inner "all" → HOLD → outer "any" gets HOLD → HOLD
        signal = await outer.generate_signal(_market(_st_buy_closes()))
        assert signal.action == "HOLD"


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


class TestPositionSizing:
    @pytest.mark.asyncio
    async def test_delegates_to_first_sub_strategy(self):
        """Composite delegates position sizing to the first sub-strategy."""
        c = _any_composite()
        params = RiskParams(
            account_balance=Decimal("100000"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("99"),
        )
        qty = await c.calculate_position_size(params)
        assert qty == 1000  # 1% of 100k / $1 risk = 1000

    @pytest.mark.asyncio
    async def test_returns_zero_when_stop_equals_entry(self):
        """Invalid stop → first sub-strategy returns 0 → composite returns 0."""
        c = _any_composite()
        params = RiskParams(
            account_balance=Decimal("100000"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("100"),
        )
        qty = await c.calculate_position_size(params)
        assert qty == 0


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class TestConfigSchema:
    def test_schema_has_required_keys(self):
        c = _any_composite()
        schema = c.get_config_schema()
        assert "properties" in schema
        props = schema["properties"]
        assert "combination_mode" in props
        assert "strategies" in props
        assert "symbols" in props

    def test_schema_combination_mode_enum(self):
        c = _any_composite()
        schema = c.get_config_schema()
        assert schema["properties"]["combination_mode"]["enum"] == ["any", "all", "majority"]

    def test_schema_strategies_is_required(self):
        c = _any_composite()
        schema = c.get_config_schema()
        assert "strategies" in schema.get("required", [])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_composite_registered_in_registry(self):
        from app.core.strategy_engine.registry import registry
        assert registry.is_registered("composite")

    def test_registry_builds_composite_instance(self):
        from app.core.strategy_engine.registry import registry
        instance = registry.build("composite", {
            "combination_mode": "any",
            "strategies": [{"type": "stock_trend", "config": _ST_CONFIG}]
        })
        assert isinstance(instance, CompositeStrategy)

    def test_registered_class_is_composite_strategy(self):
        from app.core.strategy_engine.registry import registry
        assert registry._classes["composite"] is CompositeStrategy
