"""
VwapReclaimStrategy — buy when price reclaims the day's VWAP after a stretch below it.

Day trade: the signal carries an exit_deadline shortly before the close. Rules (all must hold,
otherwise HOLD; `b` = today's completed 5-minute bars, `v` = their cumulative VWAP):
  1. Time within [entry_start_et, entry_end_et]; daily close above SMA(trend_period); regime ok.
  2. The previous min_bars_below bars closed below VWAP and the last bar closed above it.
  3. The last bar's volume >= volume_mult x the average volume of the earlier bars.
  4. VWAP < price <= last close x (1 + max_chase_pct).
Stop: lowest low of the below-VWAP bars and the last bar, less stop_buffer_pct.
Target: price + rr_ratio x risk. Self-registers as "vwap_reclaim".
"""

from __future__ import annotations

from decimal import Decimal

from app.core.strategy_engine.base import MarketData, Signal
from app.core.strategy_engine.indicators import session_vwap_series
from app.core.strategy_engine.intraday_common import DayTradeStrategy, et_time, parse_et


class VwapReclaimStrategy(DayTradeStrategy):
    def __init__(self, config: dict) -> None:
        super().__init__({"volume_mult": "1.2", **config})
        self.min_bars_below = int(config.get("min_bars_below", 3))
        self.stop_buffer_pct = Decimal(str(config.get("stop_buffer_pct", "0.001")))
        self.entry_start = parse_et(config.get("entry_start_et", "10:00"), "entry_start_et")
        self.entry_end = parse_et(config.get("entry_end_et", "14:30"), "entry_end_et")
        if self.min_bars_below < 1:
            raise ValueError("min_bars_below must be >= 1")
        if self.stop_buffer_pct < 0:
            raise ValueError("stop_buffer_pct must be >= 0")
        if self.entry_start >= self.entry_end:
            raise ValueError("entry_start_et must be before entry_end_et")

    async def generate_signal(self, market_data: MarketData) -> Signal:
        symbol = market_data.symbol
        b = market_data.intraday_bars
        n = self.min_bars_below
        if not (self.entry_start <= et_time(market_data.timestamp) <= self.entry_end):
            return self._hold(symbol)
        if len(b) < n + 2 or not self.daily_context_ok(market_data):
            return self._hold(symbol)
        v = session_vwap_series(b)
        below = range(len(b) - 1 - n, len(b) - 1)
        if not (all(b[j].close < v[j] for j in below) and b[-1].close > v[-1]):
            return self._hold(symbol)
        if not self.volume_ok(b):
            return self._hold(symbol)
        price = market_data.current_price
        if not (v[-1] < price <= b[-1].close * (1 + self.max_chase_pct)):
            return self._hold(symbol)
        low = min([b[j].low for j in below] + [b[-1].low])
        stop = low * (1 - self.stop_buffer_pct)
        if stop <= 0 or stop >= price:
            return self._hold(symbol)
        return self.buy(market_data, stop)

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "type": "object",
            "properties": {
                "min_bars_below": {"type": "integer", "minimum": 1, "default": 3,
                                   "description": "5-minute bars that must close below VWAP before the reclaim"},
                "volume_mult": {"type": "string", "default": "1.2",
                                "description": "Reclaim bar volume vs the session average so far"},
                "max_chase_pct": {"type": "string", "default": "0.003",
                                  "description": "Skip if price already ran this fraction above the reclaim close"},
                "stop_buffer_pct": {"type": "string", "default": "0.001",
                                    "description": "Stop sits this fraction below the lowest low"},
                "rr_ratio": {"type": "string", "default": "2.0", "description": "Target multiple of the risk"},
                "entry_start_et": {"type": "string", "default": "10:00", "description": "Earliest entry (ET, HH:MM)"},
                "entry_end_et": {"type": "string", "default": "14:30", "description": "Latest entry (ET, HH:MM)"},
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

registry.register("vwap_reclaim", VwapReclaimStrategy)
