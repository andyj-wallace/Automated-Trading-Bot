"""
Unit tests for IntraWeekMeanReversionStrategy.

Uses small period values (ma_period=5) and explicit weekday-only dates to keep
scenarios short and numerically precise. All expected values are pre-computed
analytically — see comments.

Fixed test dates (all UTC 16:00):
  Week A (prior) = ISO week 2 of 2024: Mon 2024-01-08 → Fri 2024-01-12
  Week B (current) = ISO week 3 of 2024: Mon 2024-01-15 → Fri 2024-01-19

Default BUY scenario (ma_period=5, pullback_pct="0.02", rr_ratio="2.0"):
  Week A closes: [99, 99, 99, 99, 99]  lows: [93, 93, 93, 93, 93]
  Week B closes: [105, 106, 107]        volumes: 100_000 each (Mon–Wed)

  Total 8 bars, closes = [99,99,99,99,99, 105,106,107]
  MA(5) = mean(closes[-5:]) = mean([99,105,106,107]) — wait, last 5:
    closes[-5:] = [99, 105, 106, 107] — len=8, [-5:] = indices 3..7
    = [closes[3]=99, closes[4]=99, closes[5]=105, closes[6]=106, closes[7]=107]
    = mean([99,99,105,106,107]) = 516/5 = 103.2
  bars[-1].close = 107 > 103.2  → uptrend ✓

  VWAP (Week B Mon–Wed, equal volume):
    = (105×100k + 106×100k + 107×100k) / 300k = 318/3 = 106.0
  threshold = 106.0 × 0.98 = 103.88
  current_price = 103.5 ≤ 103.88  → pullback triggered ✓

  stop = min prior-week lows = 93.00
  entry = 103.50, stop = 93.00  → stop < entry ✓

  take_profit = 106.0 + (106.0 − 93.0) × 2.0 = 106.0 + 26.0 = 132.00
  take_profit > entry ✓
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.brokers.base import PriceBar
from app.core.strategy_engine.base import MarketData, RiskParams
from app.core.strategy_engine.intra_week_reversion import (
    IntraWeekMeanReversionStrategy,
    _current_week_bars,
    _prior_week_bars,
    _sma,
    _vwap,
)


# ---------------------------------------------------------------------------
# Fixed dates — explicit weekday-only timestamps for reproducible week grouping
# ---------------------------------------------------------------------------

# ISO week 2 of 2024: Jan 8 (Mon) – Jan 12 (Fri)
_WEEK_A = [
    datetime(2024, 1, 8,  16, 0, tzinfo=timezone.utc),
    datetime(2024, 1, 9,  16, 0, tzinfo=timezone.utc),
    datetime(2024, 1, 10, 16, 0, tzinfo=timezone.utc),
    datetime(2024, 1, 11, 16, 0, tzinfo=timezone.utc),
    datetime(2024, 1, 12, 16, 0, tzinfo=timezone.utc),
]

# ISO week 3 of 2024: Jan 15 (Mon) – Jan 19 (Fri)
_WEEK_B = [
    datetime(2024, 1, 15, 16, 0, tzinfo=timezone.utc),
    datetime(2024, 1, 16, 16, 0, tzinfo=timezone.utc),
    datetime(2024, 1, 17, 16, 0, tzinfo=timezone.utc),
    datetime(2024, 1, 18, 16, 0, tzinfo=timezone.utc),
    datetime(2024, 1, 19, 16, 0, tzinfo=timezone.utc),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strategy(**kwargs) -> IntraWeekMeanReversionStrategy:
    defaults = {"ma_period": 5, "pullback_pct": "0.02", "rr_ratio": "2.0"}
    defaults.update(kwargs)
    return IntraWeekMeanReversionStrategy(config=defaults)


def _make_bar(
    close: float,
    ts: datetime,
    low: float | None = None,
    high: float | None = None,
    volume: int = 100_000,
) -> PriceBar:
    """Build a PriceBar with explicit field values."""
    if low is None:
        low = close - 0.5
    if high is None:
        high = close + 0.5
    return PriceBar(
        timestamp=ts,
        open=Decimal(str(close)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
        bar_size="1 day",
    )


def _buy_scenario_bars(
    prior_closes: list[float] | None = None,
    current_closes: list[float] | None = None,
    prior_low: float = 93.0,
    current_volumes: list[int] | None = None,
) -> list[PriceBar]:
    """
    Build an 8-bar list: 5 prior-week bars + 3 current-week bars (Mon–Wed).

    Defaults produce the canonical BUY scenario described in the module docstring.
    """
    if prior_closes is None:
        prior_closes = [99.0] * 5
    if current_closes is None:
        current_closes = [105.0, 106.0, 107.0]
    if current_volumes is None:
        current_volumes = [100_000] * len(current_closes)

    prior_bars = [
        _make_bar(c, _WEEK_A[i], low=prior_low) for i, c in enumerate(prior_closes)
    ]
    current_bars = [
        _make_bar(c, _WEEK_B[i], volume=current_volumes[i])
        for i, c in enumerate(current_closes)
    ]
    return prior_bars + current_bars


def _market(
    bars: list[PriceBar],
    current_price: float | None = None,
    symbol: str = "AAPL",
    open_position: bool = False,
) -> MarketData:
    price = Decimal(str(current_price)) if current_price is not None else bars[-1].close
    return MarketData(
        symbol=symbol,
        current_price=price,
        bars=bars,
        timestamp=datetime.now(timezone.utc),
        open_position=open_position,
    )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInit:
    def test_defaults_parse_correctly(self):
        s = IntraWeekMeanReversionStrategy(config={})
        assert s.ma_period == 50
        assert s.pullback_pct == Decimal("0.02")
        assert s.rr_ratio == Decimal("2.0")

    def test_custom_config_overrides_defaults(self):
        s = IntraWeekMeanReversionStrategy(
            config={"ma_period": 20, "pullback_pct": "0.03", "rr_ratio": "3.0"}
        )
        assert s.ma_period == 20
        assert s.pullback_pct == Decimal("0.03")
        assert s.rr_ratio == Decimal("3.0")

    def test_ma_period_zero_raises(self):
        with pytest.raises(ValueError, match="ma_period"):
            IntraWeekMeanReversionStrategy(config={"ma_period": 0})

    def test_ma_period_negative_raises(self):
        with pytest.raises(ValueError, match="ma_period"):
            IntraWeekMeanReversionStrategy(config={"ma_period": -1})

    def test_pullback_pct_zero_raises(self):
        with pytest.raises(ValueError, match="pullback_pct"):
            IntraWeekMeanReversionStrategy(config={"pullback_pct": "0"})

    def test_pullback_pct_one_raises(self):
        with pytest.raises(ValueError, match="pullback_pct"):
            IntraWeekMeanReversionStrategy(config={"pullback_pct": "1"})

    def test_pullback_pct_above_one_raises(self):
        with pytest.raises(ValueError, match="pullback_pct"):
            IntraWeekMeanReversionStrategy(config={"pullback_pct": "1.5"})

    def test_rr_ratio_zero_raises(self):
        with pytest.raises(ValueError, match="rr_ratio"):
            IntraWeekMeanReversionStrategy(config={"rr_ratio": "0"})

    def test_rr_ratio_negative_raises(self):
        with pytest.raises(ValueError, match="rr_ratio"):
            IntraWeekMeanReversionStrategy(config={"rr_ratio": "-1"})


# ---------------------------------------------------------------------------
# HOLD — trend filter
# ---------------------------------------------------------------------------


class TestTrendFilter:
    @pytest.mark.asyncio
    async def test_hold_when_insufficient_bars(self):
        """Fewer than ma_period bars → HOLD."""
        s = _strategy(ma_period=5)
        bars = [_make_bar(100.0, _WEEK_B[i]) for i in range(4)]  # need 5, have 4
        signal = await s.generate_signal(_market(bars))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_close_below_ma(self):
        """Last close below MA (downtrend) → HOLD."""
        s = _strategy(ma_period=5)
        # Downtrend: MA > current close
        closes = [110.0, 108.0, 106.0, 104.0, 102.0]
        bars = [_make_bar(c, _WEEK_A[i]) for i, c in enumerate(closes)]
        signal = await s.generate_signal(_market(bars))
        # MA(5) = mean([110,108,106,104,102]) = 106, close=102 ≤ 106 → HOLD
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_close_exactly_at_ma(self):
        """Last close == MA (not strictly above) → HOLD."""
        s = _strategy(ma_period=5)
        # Uniform closes: MA = close = 100
        bars = [_make_bar(100.0, _WEEK_A[i]) for i in range(5)]
        signal = await s.generate_signal(_market(bars))
        assert signal.action == "HOLD"


# ---------------------------------------------------------------------------
# HOLD — position guard
# ---------------------------------------------------------------------------


class TestPositionGuard:
    @pytest.mark.asyncio
    async def test_hold_when_open_position_is_true(self):
        """open_position=True on a valid BUY setup → HOLD (no duplicate entry)."""
        s = _strategy()
        bars = _buy_scenario_bars()
        data = _market(bars, current_price=103.5, open_position=True)
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_buy_when_open_position_is_false(self):
        """Same setup with open_position=False → BUY fires."""
        s = _strategy()
        bars = _buy_scenario_bars()
        data = _market(bars, current_price=103.5, open_position=False)
        signal = await s.generate_signal(data)
        assert signal.action == "BUY"


# ---------------------------------------------------------------------------
# HOLD — weekly data guards
# ---------------------------------------------------------------------------


class TestWeeklyDataGuards:
    @pytest.mark.asyncio
    async def test_hold_when_all_bars_in_same_week(self):
        """No prior-week bars (all in current week) → can't compute stop → HOLD."""
        s = _strategy(ma_period=3)
        # All bars in Week B; no prior-week data
        bars = [_make_bar(110.0 + i, _WEEK_B[i]) for i in range(5)]
        signal = await s.generate_signal(_market(bars))
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_no_pullback(self):
        """current_price above VWAP pullback threshold → HOLD."""
        s = _strategy()
        bars = _buy_scenario_bars()
        # VWAP = 106.0, threshold = 103.88 — price at 110 (well above)
        data = _market(bars, current_price=110.0)
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_price_exactly_at_vwap(self):
        """current_price == VWAP → not below threshold → HOLD."""
        s = _strategy()
        bars = _buy_scenario_bars()
        # VWAP = 106.0; threshold = 103.88; price at VWAP (106) > 103.88 → HOLD
        data = _market(bars, current_price=106.0)
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"

    @pytest.mark.asyncio
    async def test_hold_when_price_at_threshold_boundary(self):
        """current_price exactly at threshold (not ≤ strictly) — boundary test."""
        s = _strategy(pullback_pct="0.02")
        bars = _buy_scenario_bars()
        # VWAP=106, threshold=106×0.98=103.88; price exactly at 103.88 → ≤ threshold → BUY
        data = _market(bars, current_price=103.88)
        signal = await s.generate_signal(data)
        # 103.88 ≤ 103.88 is True → BUY
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_hold_when_stop_at_or_above_entry(self):
        """Prior week low >= current_price (degenerate stop) → HOLD."""
        s = _strategy()
        # Prior week low = 110 (high lows), current_price = 103.5 → stop=110 > entry → HOLD
        bars = _buy_scenario_bars(prior_low=110.0)
        data = _market(bars, current_price=103.5)
        signal = await s.generate_signal(data)
        assert signal.action == "HOLD"


# ---------------------------------------------------------------------------
# BUY signal — fields
# ---------------------------------------------------------------------------


class TestBuySignal:
    @pytest.mark.asyncio
    async def test_buy_fires_on_valid_pullback(self):
        """Full valid scenario produces a BUY signal."""
        s = _strategy()
        bars = _buy_scenario_bars()
        data = _market(bars, current_price=103.5)
        signal = await s.generate_signal(data)
        assert signal.action == "BUY"

    @pytest.mark.asyncio
    async def test_buy_entry_price_is_current_price(self):
        """entry_price matches market_data.current_price."""
        s = _strategy()
        bars = _buy_scenario_bars()
        data = _market(bars, current_price=103.5)
        signal = await s.generate_signal(data)
        assert signal.entry_price == Decimal("103.5")

    @pytest.mark.asyncio
    async def test_buy_stop_is_prior_week_low(self):
        """
        stop_loss_price == minimum low of all prior-week bars.

        Prior week lows all = 93.0 → stop = 93.00.
        """
        s = _strategy()
        bars = _buy_scenario_bars(prior_low=93.0)
        data = _market(bars, current_price=103.5)
        signal = await s.generate_signal(data)
        assert signal.stop_loss_price == Decimal("93.00")

    @pytest.mark.asyncio
    async def test_buy_stop_is_minimum_across_prior_week(self):
        """
        When prior-week bars have different lows, the minimum is used.

        Lows: [95, 90, 94, 92, 93] → min = 90.
        """
        s = _strategy()
        prior_lows = [95.0, 90.0, 94.0, 92.0, 93.0]
        prior_bars = [
            _make_bar(99.0, _WEEK_A[i], low=prior_lows[i]) for i in range(5)
        ]
        current_bars = [_make_bar(c, _WEEK_B[i]) for i, c in enumerate([105.0, 106.0, 107.0])]
        bars = prior_bars + current_bars
        data = _market(bars, current_price=103.5)
        signal = await s.generate_signal(data)
        assert signal.stop_loss_price == Decimal("90.00")

    @pytest.mark.asyncio
    async def test_buy_take_profit_exact_value(self):
        """take_profit = 132.00 for canonical scenario."""
        s = _strategy()
        bars = _buy_scenario_bars()
        data = _market(bars, current_price=103.5)
        signal = await s.generate_signal(data)
        assert signal.take_profit_price == Decimal("132.00")

    @pytest.mark.asyncio
    async def test_buy_take_profit_above_entry(self):
        """take_profit > entry_price."""
        s = _strategy()
        bars = _buy_scenario_bars()
        data = _market(bars, current_price=103.5)
        signal = await s.generate_signal(data)
        assert signal.take_profit_price > signal.entry_price

    @pytest.mark.asyncio
    async def test_buy_stop_below_entry(self):
        """stop_loss_price < entry_price (long position)."""
        s = _strategy()
        bars = _buy_scenario_bars()
        data = _market(bars, current_price=103.5)
        signal = await s.generate_signal(data)
        assert signal.stop_loss_price < signal.entry_price

    @pytest.mark.asyncio
    async def test_buy_signal_symbol_matches(self):
        """Signal symbol matches the symbol in MarketData."""
        s = _strategy()
        bars = _buy_scenario_bars()
        data = _market(bars, current_price=103.5, symbol="MSFT")
        signal = await s.generate_signal(data)
        assert signal.symbol == "MSFT"

    @pytest.mark.asyncio
    async def test_buy_signal_timestamp_is_timezone_aware(self):
        s = _strategy()
        bars = _buy_scenario_bars()
        data = _market(bars, current_price=103.5)
        signal = await s.generate_signal(data)
        assert signal.timestamp is not None
        assert signal.timestamp.tzinfo is not None

    @pytest.mark.asyncio
    async def test_custom_rr_ratio_changes_take_profit(self):
        """
        Larger rr_ratio → higher take_profit.

        rr=3.0: tp = 106 + 13×3 = 106 + 39 = 145.0
        rr=2.0: tp = 106 + 13×2 = 106 + 26 = 132.0
        """
        s_2x = _strategy(rr_ratio="2.0")
        s_3x = _strategy(rr_ratio="3.0")
        bars = _buy_scenario_bars()
        sig_2x = await s_2x.generate_signal(_market(bars, current_price=103.5))
        sig_3x = await s_3x.generate_signal(_market(bars, current_price=103.5))
        assert sig_3x.take_profit_price > sig_2x.take_profit_price
        assert sig_3x.take_profit_price == Decimal("145.00")


# ---------------------------------------------------------------------------
# Weekly VWAP calculation
# ---------------------------------------------------------------------------


class TestVwapCalculation:
    @pytest.mark.asyncio
    async def test_vwap_equal_volumes_is_arithmetic_mean(self):
        """
        With identical volumes VWAP = arithmetic mean of closes.

        closes=[100,110,120], equal volumes:
          vwap = 330/3 = 110.0, threshold = 107.8
          price=101 ≤ 107.8 → BUY
          stop=93, take_profit = 110 + (110-93)×2 = 110+34 = 144.00
        """
        s = _strategy()
        bars = _buy_scenario_bars(current_closes=[100.0, 110.0, 120.0])
        data = _market(bars, current_price=101.0)
        signal = await s.generate_signal(data)
        assert signal.action == "BUY"
        assert signal.take_profit_price == Decimal("144.00")

    @pytest.mark.asyncio
    async def test_vwap_weighted_by_volume(self):
        """
        VWAP is volume-weighted: heavier bars pull the average toward their close.

        closes=[100, 200], volumes=[1, 3]:
          vwap = (100×1 + 200×3) / (1+3) = 700/4 = 175.0
        """
        bars = [_make_bar(100.0, _WEEK_B[0], volume=1), _make_bar(200.0, _WEEK_B[1], volume=3)]
        result = _vwap(bars)
        assert result == Decimal("175")

    def test_vwap_single_bar_equals_close(self):
        """Single bar: VWAP = that bar's close price."""
        bar = _make_bar(123.45, _WEEK_B[0], volume=50_000)
        assert _vwap([bar]) == Decimal("123.45")

    def test_vwap_zero_volume_raises(self):
        """Zero total volume raises ValueError."""
        bar = _make_bar(100.0, _WEEK_B[0], volume=0)
        with pytest.raises(ValueError, match="zero"):
            _vwap([bar])


# ---------------------------------------------------------------------------
# Week helper functions
# ---------------------------------------------------------------------------


class TestWeekHelpers:
    def test_current_week_bars_returns_only_last_weeks_bars(self):
        """_current_week_bars returns only bars in the same ISO week as the last bar."""
        prior = [_make_bar(99.0, _WEEK_A[i]) for i in range(5)]
        current = [_make_bar(105.0, _WEEK_B[i]) for i in range(3)]
        all_bars = prior + current
        result = _current_week_bars(all_bars)
        assert len(result) == 3
        assert all(b.timestamp.isocalendar()[1] == 3 for b in result)  # week 3

    def test_prior_week_bars_returns_only_previous_week(self):
        """_prior_week_bars returns only bars from the ISO week before the last bar."""
        prior = [_make_bar(99.0, _WEEK_A[i]) for i in range(5)]
        current = [_make_bar(105.0, _WEEK_B[i]) for i in range(3)]
        all_bars = prior + current
        result = _prior_week_bars(all_bars)
        assert len(result) == 5
        assert all(b.timestamp.isocalendar()[1] == 2 for b in result)  # week 2

    def test_prior_week_excludes_older_bars(self):
        """Bars older than the prior week are excluded."""
        # Add a third (oldest) week before Week A
        week_c_date = datetime(2024, 1, 1, 16, 0, tzinfo=timezone.utc)  # ISO week 1
        old_bar = _make_bar(80.0, week_c_date)
        prior = [_make_bar(99.0, _WEEK_A[i]) for i in range(5)]
        current = [_make_bar(105.0, _WEEK_B[i]) for i in range(3)]
        all_bars = [old_bar] + prior + current
        result = _prior_week_bars(all_bars)
        # Should be Week A only (5 bars), not the older week-1 bar
        assert len(result) == 5

    def test_current_week_returns_empty_for_empty_bars(self):
        assert _current_week_bars([]) == []

    def test_prior_week_returns_empty_for_empty_bars(self):
        assert _prior_week_bars([]) == []


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
        props = schema["properties"]
        assert "ma_period" in props
        assert "pullback_pct" in props
        assert "rr_ratio" in props
        assert "symbols" in props

    def test_schema_defaults_match_instance_defaults(self):
        s = IntraWeekMeanReversionStrategy(config={})
        schema = s.get_config_schema()
        props = schema["properties"]
        assert props["ma_period"]["default"] == 50
        assert props["pullback_pct"]["default"] == "0.02"
        assert props["rr_ratio"]["default"] == "2.0"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_intra_week_reversion_registered_in_registry(self):
        from app.core.strategy_engine.registry import registry
        assert "intra_week_reversion" in registry.registered_types()

    def test_registry_builds_correct_class(self):
        from app.core.strategy_engine.registry import registry
        instance = registry.build("intra_week_reversion", {"ma_period": 10})
        assert isinstance(instance, IntraWeekMeanReversionStrategy)

    def test_registered_class_is_intra_week_reversion(self):
        from app.core.strategy_engine.registry import registry
        cls = registry._classes["intra_week_reversion"]
        assert cls is IntraWeekMeanReversionStrategy


# ---------------------------------------------------------------------------
# SMA utility
# ---------------------------------------------------------------------------


class TestSmaUtility:
    def test_sma_uniform_values(self):
        values = [Decimal("10")] * 5
        assert _sma(values, 5) == Decimal("10")

    def test_sma_uses_last_period_values_only(self):
        # 7 values, period=5: only last 5 matter
        values = [Decimal(str(v)) for v in [1, 2, 10, 10, 10, 10, 10]]
        assert _sma(values, 5) == Decimal("10")
