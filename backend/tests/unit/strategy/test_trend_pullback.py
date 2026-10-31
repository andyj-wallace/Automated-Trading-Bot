"""Unit tests for TrendPullbackStrategy (J2)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.brokers.base import PriceBar
from app.core.strategy_engine.base import MarketData, RiskParams
from app.core.strategy_engine.trend_pullback import TrendPullbackStrategy
from tests.unit.strategy.conftest import make_bars


def _setup_bars() -> list[PriceBar]:
    start = datetime(2025, 1, 1, tzinfo=UTC)

    def bar(i, o, h, lo, c, v=1_000_000):
        return PriceBar(timestamp=start + timedelta(days=i), open=D(o), high=D(h), low=D(lo), close=D(c), volume=v)

    bars = []
    for i in range(220):  # steady uptrend: close 50.0 -> 93.8
        c = D("50") + D("0.2") * i
        bars.append(bar(i, c - D("0.1"), c + D("0.3"), c - D("0.3"), c))
    for i, c in enumerate(["93.2", "92.4", "91.8", "91.6"], start=220):  # 4-day dip
        c = D(c)
        bars.append(bar(i, c + D("0.2"), c + D("0.3"), c - D("0.3"), c))
    bars.append(bar(224, "91.7", "92.6", "91.5", "92.5", 1_500_000))  # bounce bar
    return bars


BARS = _setup_bars()


def _data(price="92.60", bars=None, extra=None, symbol="SCHB") -> MarketData:
    return MarketData(
        symbol=symbol, current_price=D(price), bars=bars if bars is not None else BARS,
        timestamp=datetime.now(UTC), extra_bars={"SPY": BARS[:220]} if extra is None else extra,
    )


async def _signal(data=None, **config):
    return await TrendPullbackStrategy(config).generate_signal(data or _data())


async def test_buy_on_bounce():
    s = await _signal()
    assert (s.action, s.stop_loss_price, s.take_profit_price) == ("BUY", D("91.12"), D("95.56"))


async def test_regime_symbol_empty_needs_no_spy_bars():
    assert (await _signal(_data(extra={}), regime_symbol="")).action == "BUY"


@pytest.mark.parametrize(
    ("data", "config"),
    [
        (_data(extra={}), {}),  # no SPY bars
        (_data(price="93.50"), {}),  # chasing
        (_data(price="91.70", bars=BARS[:-1]), {}),  # no bounce yet
        (None, {"max_atr_pct": "0.006"}),
        (None, {"max_stop_atr": "1.5"}),
        (_data(bars=make_bars(250, trend=-0.2)), {}),  # downtrend
    ],
)
async def test_hold_cases(data, config):
    assert (await _signal(data, **config)).action == "HOLD"


async def test_sizing_follows_one_percent_rule():
    strategy = TrendPullbackStrategy({})
    params = {"entry_price": D("92.60"), "stop_loss_price": D("91.12")}
    assert await strategy.calculate_position_size(RiskParams(account_balance=D(100), **params)) == 0
    assert await strategy.calculate_position_size(RiskParams(account_balance=D(1000), **params)) == 6


def test_extra_symbols_and_validation():
    assert TrendPullbackStrategy({}).required_extra_symbols() == ["SPY"]
    with pytest.raises(ValueError):
        TrendPullbackStrategy({"fast_period": 60})
