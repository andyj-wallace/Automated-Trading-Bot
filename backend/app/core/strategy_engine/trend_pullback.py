"""
TrendPullbackStrategy — buy the first bounce after a dip to the fast average inside an uptrend.

Decides from completed daily bars (bars[-1] is the last completed session) and enters the
day after the bounce bar, so live trading and backtests behave the same.

Entry rules (all must hold, otherwise HOLD):
  1. Uptrend: close > SMA(slow) and SMA(mid) > SMA(slow).
  2. Regime (skipped when regime_symbol is ""): the regime symbol closes above its regime_period SMA.
  3. Volatility: ATR(atr_period) / close within [min_atr_pct, max_atr_pct].
  4. Pullback: the lowest low of the last pullback_lookback bars sits above SMA(slow) and at most
     touch_tolerance_pct above SMA(fast).
  5. Continuation: close[-1] > high[-2] and close[-2] <= high[-3] (the first strong bar of the
     bounce, so one pullback gives one signal).
  6. No chasing: current price <= close[-1] * (1 + max_chase_pct).

Stop: pullback low - stop_atr_buffer * ATR (HOLD if wider than max_stop_atr * ATR).
Target: entry + rr_ratio * (entry - stop).

Regime bars come from market_data.extra_bars[regime_symbol] (or from `bars` when the traded
symbol is the regime symbol). Self-registers as "trend_pullback".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from app.core.risk.calculator import RiskCalculator
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal
from app.core.strategy_engine.filters import regime_ok, volatility_ok
from app.core.strategy_engine.indicators import atr, sma

_TWO_DP = Decimal("0.01")


class TrendPullbackStrategy(BaseStrategy):
    def __init__(self, config: dict) -> None:
        self.fast_period = int(config.get("fast_period", 20))
        self.mid_period = int(config.get("mid_period", 50))
        self.slow_period = int(config.get("slow_period", 200))
        self.pullback_lookback = int(config.get("pullback_lookback", 5))
        self.touch_tolerance_pct = Decimal(str(config.get("touch_tolerance_pct", "0.005")))
        self.atr_period = int(config.get("atr_period", 14))
        self.stop_atr_buffer = Decimal(str(config.get("stop_atr_buffer", "0.25")))
        self.max_stop_atr = Decimal(str(config.get("max_stop_atr", "3.0")))
        self.max_chase_pct = Decimal(str(config.get("max_chase_pct", "0.01")))
        self.rr_ratio = Decimal(str(config.get("rr_ratio", "2.0")))
        self.regime_symbol = str(config.get("regime_symbol", "SPY") or "").upper()
        self.regime_period = int(config.get("regime_period", 200))
        self.min_atr_pct = Decimal(str(config.get("min_atr_pct", "0.005")))
        self.max_atr_pct = Decimal(str(config.get("max_atr_pct", "0.04")))
        self._calculator = RiskCalculator()

        if not 1 <= self.fast_period < self.mid_period < self.slow_period:
            raise ValueError("periods must satisfy 1 <= fast_period < mid_period < slow_period")
        if self.pullback_lookback < 2:
            raise ValueError(f"pullback_lookback must be >= 2, got {self.pullback_lookback}")
        if self.atr_period < 1 or self.regime_period < 1:
            raise ValueError("atr_period and regime_period must be >= 1")
        if min(self.touch_tolerance_pct, self.stop_atr_buffer, self.max_chase_pct, self.min_atr_pct) < 0:
            raise ValueError("percentages and the stop buffer must be >= 0")
        if self.max_stop_atr <= 0 or self.rr_ratio <= 0:
            raise ValueError("max_stop_atr and rr_ratio must be > 0")
        if self.min_atr_pct > self.max_atr_pct:
            raise ValueError("min_atr_pct must be <= max_atr_pct")

    async def generate_signal(self, market_data: MarketData) -> Signal:
        bars = market_data.bars
        symbol = market_data.symbol
        need = max(self.slow_period, self.atr_period + 1, self.pullback_lookback + 2) + 1
        if len(bars) < need:
            return self._hold(symbol)
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        price = market_data.current_price
        sma_fast, sma_mid, sma_slow = (
            sma(closes, self.fast_period), sma(closes, self.mid_period), sma(closes, self.slow_period)
        )
        if not (closes[-1] > sma_slow and sma_mid > sma_slow):
            return self._hold(symbol)
        if self.regime_symbol:
            regime_bars = (
                bars if symbol.upper() == self.regime_symbol
                else market_data.extra_bars.get(self.regime_symbol, [])
            )
            if not regime_ok(regime_bars, self.regime_period):
                return self._hold(symbol)
        if not volatility_ok(bars, self.atr_period, self.min_atr_pct, self.max_atr_pct):
            return self._hold(symbol)
        pullback_low = min(b.low for b in bars[-self.pullback_lookback:])
        if not (sma_slow < pullback_low <= sma_fast * (1 + self.touch_tolerance_pct)):
            return self._hold(symbol)
        if not (closes[-1] > highs[-2] and closes[-2] <= highs[-3]):
            return self._hold(symbol)
        if price > closes[-1] * (1 + self.max_chase_pct):
            return self._hold(symbol)
        atr_now = atr(bars, self.atr_period)
        stop = (pullback_low - atr_now * self.stop_atr_buffer).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
        risk = price - stop
        if stop <= 0 or risk <= 0 or risk > atr_now * self.max_stop_atr:
            return self._hold(symbol)
        take_profit = (price + risk * self.rr_ratio).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
        return Signal(
            symbol=symbol, action="BUY", entry_price=price, stop_loss_price=stop,
            take_profit_price=take_profit, timestamp=market_data.timestamp,
        )

    def required_extra_symbols(self) -> list[str]:
        return [self.regime_symbol] if self.regime_symbol else []

    async def calculate_position_size(self, risk_params: RiskParams) -> int:
        """Return the maximum safe quantity using the 1% risk rule."""
        try:
            return self._calculator.max_quantity(
                risk_params.account_balance, risk_params.entry_price, risk_params.stop_loss_price
            )
        except Exception:
            return 0

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "type": "object",
            "properties": {
                "fast_period": {"type": "integer", "minimum": 1, "default": 20,
                                "description": "Fast average (days); must be below mid_period"},
                "mid_period": {"type": "integer", "minimum": 1, "default": 50,
                               "description": "Mid average (days); must be below slow_period"},
                "slow_period": {"type": "integer", "minimum": 1, "default": 200,
                                "description": "Slow average (days) that defines the uptrend"},
                "pullback_lookback": {"type": "integer", "minimum": 2, "default": 5,
                                      "description": "Bars searched for the dip"},
                "touch_tolerance_pct": {"type": "string", "default": "0.005",
                                        "description": "A dip low within this fraction above the fast average counts as a touch"},
                "atr_period": {"type": "integer", "minimum": 1, "default": 14, "description": "ATR window"},
                "stop_atr_buffer": {"type": "string", "default": "0.25",
                                    "description": "Stop sits this many ATRs below the dip low"},
                "max_stop_atr": {"type": "string", "default": "3.0",
                                 "description": "Skip setups whose stop is wider than this many ATRs"},
                "max_chase_pct": {"type": "string", "default": "0.01",
                                  "description": "Skip if price already ran this fraction above the last close"},
                "rr_ratio": {"type": "string", "default": "2.0",
                             "description": "Target multiple of the risk; below MIN_REWARD_TO_RISK the risk manager rejects"},
                "regime_symbol": {"type": "string", "default": "SPY",
                                  "description": "Market barometer; empty string disables the market filter"},
                "regime_period": {"type": "integer", "minimum": 1, "default": 200,
                                  "description": "Regime average (days)"},
                "min_atr_pct": {"type": "string", "default": "0.005",
                                "description": "Minimum ATR as a fraction of price"},
                "max_atr_pct": {"type": "string", "default": "0.04",
                                "description": "Maximum ATR as a fraction of price"},
                "symbols": {"type": "array", "items": {"type": "string"}, "default": [],
                            "description": "Ticker symbols this strategy trades"},
            },
            "required": [],
        }

    def _hold(self, symbol: str) -> Signal:
        return Signal(symbol=symbol, action="HOLD", timestamp=datetime.now(UTC))


# Self-registration — runs when this module is imported.
from app.core.strategy_engine.registry import registry  # noqa: E402

registry.register("trend_pullback", TrendPullbackStrategy)
