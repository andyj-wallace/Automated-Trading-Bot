"""
Shared chop/whipsaw filter for crossover-style trend-following strategies
(MovingAverageStrategy, StockTrendStrategy, BullBearStrategy's entry leg).

Trend-following crossover systems have a well-known failure mode: in a
sideways, directionless market, the two lines being compared (fast MA vs
slow MA, or price vs its MA) cross back and forth repeatedly, and a
strategy with no confirmation logic fires a fresh trade on every crossing
— each one likely to get stopped out for a small loss ("death by a
thousand cuts" / whipsaw). This filter adds a configurable minimum margin
a crossover must clear before it's treated as a real signal rather than
noise.
"""

from decimal import Decimal

from app.brokers.base import PriceBar
from app.core.strategy_engine.indicators import atr, sma


def min_separation_ok(faster: Decimal, slower: Decimal, min_separation_pct: Decimal) -> bool:
    """
    True if `faster` clears `slower` by at least `min_separation_pct` of `slower`.

    A bare `faster > slower` crossover check fires on any margin at all,
    including a fraction of a cent in a choppy market. Requiring a minimum
    separation (e.g. "0.001" = 0.1% of price) filters out marginal,
    noise-level crosses while still catching real trend changes, which
    typically clear by a much wider margin.

    `min_separation_pct` of "0" preserves the original bare-crossover
    behavior exactly (always returns True whenever faster > slower).
    """
    if slower <= 0:
        return True
    return (faster - slower) / slower >= min_separation_pct


def regime_ok(regime_bars: list[PriceBar], period: int) -> bool:
    """True when the market barometer's last close is above its `period`-day SMA."""
    if len(regime_bars) < period:
        return False
    closes = [b.close for b in regime_bars]
    return closes[-1] > sma(closes, period)


def volatility_ok(bars: list[PriceBar], period: int, min_pct: Decimal, max_pct: Decimal) -> bool:
    """True when ATR as a fraction of the last close lies within [min_pct, max_pct]."""
    if len(bars) < period + 1 or bars[-1].close <= 0:
        return False
    return min_pct <= atr(bars, period) / bars[-1].close <= max_pct
