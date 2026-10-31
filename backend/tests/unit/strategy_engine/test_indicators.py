"""Unit tests for indicator helpers (J1)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from app.brokers.base import PriceBar
from app.core.strategy_engine.bull_bear import _atr
from app.core.strategy_engine.indicators import atr, avg_dollar_volume, sma


def _bars(closes: list[str], spread="1", volume=1000) -> list[PriceBar]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        PriceBar(
            timestamp=start + timedelta(days=i), open=D(c), high=D(c) + D(spread),
            low=D(c) - D(spread), close=D(c), volume=volume,
        )
        for i, c in enumerate(closes)
    ]


def test_sma_uses_last_period_values():
    assert sma([D(1), D(2), D(3), D(4)], 2) == D("3.5")


def test_atr_matches_bull_bear():
    bars = _bars([str(100 + (i * 7) % 5) for i in range(30)])
    assert atr(bars, 14) == _atr(bars, 14)


def test_avg_dollar_volume():
    assert avg_dollar_volume([]) == 0
    assert avg_dollar_volume(_bars(["10", "20"], volume=100)) == D("1500")


def test_session_vwap_series_uses_wap_then_typical_price_and_skips_zero_volume():
    from decimal import Decimal

    from app.brokers.base import PriceBar
    from app.core.strategy_engine.indicators import session_vwap_series

    def bar(h, lo, c, volume, wap=None):
        return PriceBar(timestamp=datetime(2026, 10, 19, tzinfo=UTC), open=Decimal(c), high=Decimal(h),
                        low=Decimal(lo), close=Decimal(c), volume=volume,
                        wap=Decimal(wap) if wap else None)

    bars = [
        bar("0", "0", "0", 0),                  # no volume yet -> 0
        bar("12", "9", "9", 100),               # typical (12+9+9)/3 = 10
        bar("30", "30", "30", 100, wap="20"),   # wap 20 -> (1000 + 2000) / 200 = 15
        bar("99", "99", "99", 0),               # zero volume repeats 15
    ]
    assert session_vwap_series(bars) == [Decimal("0"), Decimal("10"), Decimal("15"), Decimal("15")]
