"""Fill price, slippage and commission helpers shared by backtest engines."""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.brokers.base import PriceBar
from app.core.backtesting.results import SimulatedTrade
from app.core.risk.costs import CostModel


def exit_fill(
    trade: SimulatedTrade, bar: PriceBar, slippage_pct: Decimal
) -> tuple[Decimal, str] | None:
    """Exit price and reason if `bar` reaches the stop or target; a gap past a level fills at the open."""
    if bar.open <= trade.stop_loss_price:
        return apply_slippage(bar.open, -1, slippage_pct), "STOP_LOSS"
    if bar.low <= trade.stop_loss_price:
        return apply_slippage(trade.stop_loss_price, -1, slippage_pct), "STOP_LOSS"
    if bar.open >= trade.take_profit_price:
        return apply_slippage(bar.open, -1, slippage_pct), "TAKE_PROFIT"
    if bar.high >= trade.take_profit_price:
        return apply_slippage(trade.take_profit_price, -1, slippage_pct), "TAKE_PROFIT"
    return None


def leg_commission(
    cost_model: CostModel | None, flat_fee: Decimal, quantity: int, price: Decimal
) -> Decimal:
    """One order's fee: the broker schedule when a cost model is set, else the flat per-leg fee."""
    return cost_model.commission(Decimal(quantity), price) if cost_model else flat_fee


def visible_extra_bars(
    extra_bars: dict[str, list[PriceBar]], extra_times: dict[str, list[datetime]], now: datetime
) -> dict[str, list[PriceBar]]:
    """Each extra symbol's bars dated <= `now` (no look-ahead)."""
    return {
        sym: series[: bisect_right(extra_times[sym], now)] for sym, series in extra_bars.items()
    }


def apply_slippage(price: Decimal, worse_direction: int, slippage_pct: Decimal) -> Decimal:
    """
    Adjust `price` unfavorably by `slippage_pct`.

    worse_direction=+1 for a buy (we pay more), -1 for a sell (we receive
    less). Rounded to 2dp like a real stock price. Shared between
    BacktestingEngine and PortfolioBacktestEngine so both apply identical
    friction.
    """
    adjusted = price * (Decimal("1") + worse_direction * slippage_pct)
    return adjusted.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
