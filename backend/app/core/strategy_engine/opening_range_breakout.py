"""
OpeningRangeBreakoutStrategy — buy the first 5-minute close above the opening range.

Day trade: the signal carries an exit_deadline shortly before the close. Rules (all must hold,
otherwise HOLD; `b` = today's completed 5-minute bars, n = range_minutes / 5):
  1. More than n bars exist and the time is at or before entry_end_et.
  2. The opening range (high/low of b[:n]) is between min_range_atr and max_range_atr daily ATRs.
  3. The last bar is the FIRST close above the range high.
  4. The last bar's volume >= volume_mult x the average volume of the earlier bars.
  5. Daily close above SMA(trend_period); regime ok.
  6. range high < price <= last close x (1 + max_chase_pct).
Stop: range low - 0.01. Target: price + rr_ratio x risk. Self-registers as "opening_range_breakout".
"""

from __future__ import annotations

from decimal import Decimal

from app.core.strategy_engine.base import MarketData, Signal
from app.core.strategy_engine.indicators import atr
from app.core.strategy_engine.intraday_common import DayTradeStrategy, et_time, parse_et

_ATR_PERIOD = 14


class OpeningRangeBreakoutStrategy(DayTradeStrategy):
    def __init__(self, config: dict) -> None:
        super().__init__({"volume_mult": "1.5", **config})
        self.range_minutes = int(config.get("range_minutes", 15))
        self.min_range_atr = Decimal(str(config.get("min_range_atr", "0.15")))
        self.max_range_atr = Decimal(str(config.get("max_range_atr", "0.6")))
        self.entry_end = parse_et(config.get("entry_end_et", "11:30"), "entry_end_et")
        if self.range_minutes < 5 or self.range_minutes % 5:
            raise ValueError("range_minutes must be a positive multiple of 5")
        if not 0 <= self.min_range_atr <= self.max_range_atr:
            raise ValueError("min_range_atr must be >= 0 and <= max_range_atr")

    async def generate_signal(self, market_data: MarketData) -> Signal:
        symbol = market_data.symbol
        b = market_data.intraday_bars
        n = self.range_minutes // 5
        if len(b) <= n or et_time(market_data.timestamp) > self.entry_end:
            return self._hold(symbol)
        if len(market_data.bars) < _ATR_PERIOD + 1 or not self.daily_context_ok(market_data):
            return self._hold(symbol)
        or_high, or_low = max(x.high for x in b[:n]), min(x.low for x in b[:n])
        daily_atr = atr(market_data.bars, _ATR_PERIOD)
        if not (self.min_range_atr * daily_atr <= or_high - or_low <= self.max_range_atr * daily_atr):
            return self._hold(symbol)
        if not (b[-1].close > or_high and all(x.close <= or_high for x in b[n:-1])):
            return self._hold(symbol)
        if not self.volume_ok(b):
            return self._hold(symbol)
        price = market_data.current_price
        if not (or_high < price <= b[-1].close * (1 + self.max_chase_pct)):
            return self._hold(symbol)
        stop = or_low - Decimal("0.01")
        if stop <= 0 or stop >= price:
            return self._hold(symbol)
        return self.buy(market_data, stop)

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "type": "object",
            "properties": {
                "range_minutes": {"type": "integer", "minimum": 5, "default": 15,
                                  "description": "Length of the opening range; a multiple of 5"},
                "volume_mult": {"type": "string", "default": "1.5",
                                "description": "Breakout bar volume vs the session average so far"},
                "max_chase_pct": {"type": "string", "default": "0.003",
                                  "description": "Skip if price already ran this fraction above the breakout close"},
                "entry_end_et": {"type": "string", "default": "11:30", "description": "Latest entry (ET, HH:MM)"},
                "min_range_atr": {"type": "string", "default": "0.15",
                                  "description": "Skip ranges narrower than this many daily ATRs"},
                "max_range_atr": {"type": "string", "default": "0.6",
                                  "description": "Skip ranges wider than this many daily ATRs"},
                "rr_ratio": {"type": "string", "default": "2.0", "description": "Target multiple of the risk"},
                "trend_period": {"type": "integer", "minimum": 1, "default": 50,
                                 "description": "Daily average the close must sit above"},
                "regime_symbol": {"type": "string", "default": "SPY",
                                  "description": "Market barometer; empty string disables the market filter"},
                "regime_period": {"type": "integer", "minimum": 1, "default": 200, "description": "Regime average (days)"},
                "symbols": {"type": "array", "items": {"type": "string"}, "default": [],
                            "description": "Ticker symbols this strategy trades"},
            },
            "required": [],
        }


# Self-registration — runs when this module is imported.
from app.core.strategy_engine.registry import registry  # noqa: E402

registry.register("opening_range_breakout", OpeningRangeBreakoutStrategy)
