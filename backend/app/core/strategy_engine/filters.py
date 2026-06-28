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
