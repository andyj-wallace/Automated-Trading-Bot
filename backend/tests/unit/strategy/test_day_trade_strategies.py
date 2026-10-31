"""Unit tests for the day-trade strategies vwap_reclaim and opening_range_breakout (L3, L4)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.brokers.base import PriceBar
from app.core import market_calendar
from app.core.strategy_engine.base import MarketData
from app.core.strategy_engine.indicators import session_vwap_series
from app.core.strategy_engine.opening_range_breakout import OpeningRangeBreakoutStrategy
from app.core.strategy_engine.vwap_reclaim import VwapReclaimStrategy
from tests.unit.strategy.test_trend_pullback import (
    BARS,  # steady uptrend: above SMA50, SPY above SMA200
)

ET = market_calendar.ET
OPEN = datetime(2026, 10, 19, 9, 30, tzinfo=ET)  # a Monday session; closes 16:00 ET


def _bar(i, h, lo, c, v):
    return PriceBar(timestamp=(OPEN + timedelta(minutes=5 * i)).astimezone(UTC), open=D(c), high=D(h),
                    low=D(lo), close=D(c), volume=v, bar_size="5 mins")


def _data(intraday, price, at=(11, 0), extra=None) -> MarketData:
    return MarketData(
        symbol="SCHB", current_price=D(price), bars=BARS, intraday_bars=intraday,
        timestamp=datetime(2026, 10, 19, *at, tzinfo=ET),
        extra_bars={"SPY": BARS[:220]} if extra is None else extra,
    )


# --- vwap_reclaim -----------------------------------------------------------


def _reclaim_session(last_volume=2000):
    return [
        _bar(0, "101", "99.5", "100.5", 1000),
        _bar(1, "100.4", "99.4", "99.6", 800),
        _bar(2, "99.8", "99.2", "99.4", 800),
        _bar(3, "99.6", "99.0", "99.3", 800),
        _bar(4, "101.2", "99.3", "101.0", last_volume),  # the reclaim
    ]


def test_reclaim_fixture_dips_below_then_closes_above_vwap():
    b = _reclaim_session()
    v = session_vwap_series(b)
    assert all(b[j].close < v[j] for j in (1, 2, 3)) and b[4].close > v[4]


async def test_vwap_reclaim_buys_with_day_deadline():
    signal = await VwapReclaimStrategy({}).generate_signal(_data(_reclaim_session(), "101.1"))
    assert signal.action == "BUY"
    assert signal.stop_loss_price == D("98.90")  # 99.0 x (1 - 0.001)
    assert signal.take_profit_price == D("101.1") + 2 * (D("101.1") - D("98.90"))
    assert signal.exit_deadline == datetime(2026, 10, 19, 15, 55, tzinfo=ET)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"at": (9, 50)},  # before the entry window
        {"at": (14, 45)},  # after it
        {"price": "101.5"},  # chasing: above last close + 0.3%
        {"price": "100"},  # not above VWAP
        {"extra": {}},  # market filter has no SPY bars
    ],
)
async def test_vwap_reclaim_holds(kwargs):
    kwargs = {"price": "101.1", **kwargs}
    signal = await VwapReclaimStrategy({}).generate_signal(_data(_reclaim_session(), **kwargs))
    assert signal.action == "HOLD"


async def test_vwap_reclaim_holds_on_weak_volume_and_short_session():
    strategy = VwapReclaimStrategy({})
    assert (await strategy.generate_signal(_data(_reclaim_session(last_volume=900), "101.1"))).action == "HOLD"
    assert (await strategy.generate_signal(_data(_reclaim_session()[1:], "101.1"))).action == "HOLD"


# --- opening_range_breakout -------------------------------------------------


def _breakout_session(second_close="100.2"):
    return [
        _bar(0, "100.3", "100.0", "100.2", 1000),
        _bar(1, "100.2", "100.0", "100.1", 1000),
        _bar(2, "100.3", "100.0", "100.2", 1000),  # opening range 100.0 - 100.3
        _bar(3, "100.5", "100.1", second_close, 1000),
        _bar(4, "100.7", "100.2", "100.5", 2000),  # breakout
    ]


async def test_orb_buys_first_close_above_range():
    signal = await OpeningRangeBreakoutStrategy({}).generate_signal(_data(_breakout_session(), "100.6", (10, 30)))
    assert signal.action == "BUY"
    assert signal.stop_loss_price == D("99.99")  # range low - 0.01
    assert signal.exit_deadline == datetime(2026, 10, 19, 15, 55, tzinfo=ET)


async def test_orb_holds_when_an_earlier_bar_already_broke_out():
    signal = await OpeningRangeBreakoutStrategy({}).generate_signal(
        _data(_breakout_session(second_close="100.4"), "100.6", (10, 30))
    )
    assert signal.action == "HOLD"


@pytest.mark.parametrize(
    ("high", "low"),
    [("100.1", "100.05"), ("101.5", "100.0")],  # range 0.05 (too narrow) and 1.5 (too wide)
)
async def test_orb_holds_on_bad_range_size(high, low):
    session = _breakout_session()
    session[1] = _bar(1, high, low, "100.1", 1000)
    session[0] = _bar(0, "100.1", "100.05", "100.1", 1000)
    session[2] = _bar(2, "100.1", "100.05", "100.1", 1000)
    session[3] = _bar(3, "100.1", "100.05", "100.1", 1000)
    session[4] = _bar(4, "101.7", "100.2", str(D(high) + D("0.1")), 2000)
    signal = await OpeningRangeBreakoutStrategy({}).generate_signal(_data(session, str(D(high) + D("0.15")), (10, 30)))
    assert signal.action == "HOLD"


async def test_orb_holds_after_entry_cutoff_and_when_chasing():
    strategy = OpeningRangeBreakoutStrategy({})
    assert (await strategy.generate_signal(_data(_breakout_session(), "100.6", (11, 31)))).action == "HOLD"
    assert (await strategy.generate_signal(_data(_breakout_session(), "101.0", (10, 30)))).action == "HOLD"


def test_day_trade_configs_are_validated():
    with pytest.raises(ValueError, match="multiple of 5"):
        OpeningRangeBreakoutStrategy({"range_minutes": 12})
    with pytest.raises(ValueError, match="HH:MM"):
        VwapReclaimStrategy({"entry_start_et": "ten"})
    with pytest.raises(ValueError, match="before"):
        VwapReclaimStrategy({"entry_start_et": "15:00", "entry_end_et": "14:00"})
