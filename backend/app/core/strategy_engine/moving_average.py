"""
MovingAverageStrategy — 50/200-day simple moving average crossover.

BUY signal:  fast MA crosses ABOVE slow MA (golden cross).
SELL signal: fast MA crosses BELOW slow MA (death cross).

Both fast_period and slow_period are configurable via JSONB config.
Stop-loss is placed stop_loss_pct% from entry price; take-profit is set at
2× the stop distance to satisfy the RiskManager's minimum R:R gate.

NOTE on SELL direction: stop_loss_price is placed above entry_price for a
short. The current RiskManager validates stop_loss < entry_price (long-only
assumption). SELL signals will therefore be rejected until short-selling
support is added to the risk layer. The strategy is correct; the gate will
open once the risk layer is extended.

Config keys:
    fast_period      int   Moving average period for the fast line (default 50)
    slow_period      int   Moving average period for the slow line (default 200)
    stop_loss_pct    str   Stop-loss distance as a decimal fraction (default "0.03" = 3%)

Registration:
    This module self-registers with the process-level StrategyRegistry when
    imported. Import it once on app startup (e.g. in main.py) to make the
    type "moving_average" available to the scheduler.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.core.risk.calculator import RiskCalculator
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal

_TWO_DP = Decimal("0.01")


class MovingAverageStrategy(BaseStrategy):
    """
    Simple moving average crossover strategy.

    Generates a BUY signal the first bar a golden cross is detected and a SELL
    signal the first bar a death cross is detected. Returns HOLD on every other
    bar, including bars where the same cross is already in effect (avoids
    re-entering the same trade on every subsequent bar).
    """

    def __init__(self, config: dict) -> None:
        self.fast_period: int = int(config.get("fast_period", 50))
        self.slow_period: int = int(config.get("slow_period", 200))
        self.stop_loss_pct: Decimal = Decimal(str(config.get("stop_loss_pct", "0.03")))
        self._calculator = RiskCalculator()

        if self.fast_period >= self.slow_period:
            raise ValueError(
                f"fast_period ({self.fast_period}) must be less than "
                f"slow_period ({self.slow_period})"
            )
        if self.stop_loss_pct <= Decimal("0") or self.stop_loss_pct >= Decimal("1"):
            raise ValueError(
                f"stop_loss_pct must be between 0 and 1, got {self.stop_loss_pct}"
            )

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    async def generate_signal(self, market_data: MarketData) -> Signal:
        """
        Analyse bars and return BUY, SELL, or HOLD.

        Returns HOLD when:
        - Not enough bars for the slow MA (need slow_period + 1 for crossover detection)
        - No crossover detected on the current bar
        """
        bars = market_data.bars
        required = self.slow_period + 1  # need one extra bar for "previous" MAs

        if len(bars) < required:
            return self._hold(market_data.symbol)

        closes = [bar.close for bar in bars]

        fast_current = _sma(closes, self.fast_period)
        slow_current = _sma(closes, self.slow_period)

        # Previous bar's MAs — used to detect the moment of crossover
        prev_closes = closes[:-1]
        fast_prev = _sma(prev_closes, self.fast_period)
        slow_prev = _sma(prev_closes, self.slow_period)

        entry = market_data.current_price

        if fast_prev <= slow_prev and fast_current > slow_current:
            # Golden cross — BUY
            stop = (entry * (1 - self.stop_loss_pct)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
            stop_distance = entry - stop
            take_profit = (entry + stop_distance * 2).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
            return Signal(
                symbol=market_data.symbol,
                action="BUY",
                entry_price=entry,
                stop_loss_price=stop,
                take_profit_price=take_profit,
                timestamp=market_data.timestamp,
            )

        if fast_prev >= slow_prev and fast_current < slow_current:
            # Death cross — SELL (short)
            # stop_loss is ABOVE entry for a short position
            stop = (entry * (1 + self.stop_loss_pct)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
            stop_distance = stop - entry
            take_profit = (entry - stop_distance * 2).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
            return Signal(
                symbol=market_data.symbol,
                action="SELL",
                entry_price=entry,
                stop_loss_price=stop,
                take_profit_price=take_profit,
                timestamp=market_data.timestamp,
            )

        return self._hold(market_data.symbol)

    async def calculate_position_size(self, risk_params: RiskParams) -> int:
        """
        Return the maximum safe quantity using the 1% rule.

        For a SELL (short) signal the stop_loss is above entry, which makes
        risk_per_share negative — RiskCalculator will raise. In that case
        this method returns 0 and the scheduler skips the order.
        """
        try:
            return self._calculator.max_quantity(
                risk_params.account_balance,
                risk_params.entry_price,
                risk_params.stop_loss_price,
            )
        except Exception:
            return 0

    def get_config_schema(self) -> dict:
        """JSON Schema for the moving average strategy configuration."""
        return {
            "type": "object",
            "properties": {
                "fast_period": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 50,
                    "description": "Fast moving average period (days)",
                },
                "slow_period": {
                    "type": "integer",
                    "minimum": 2,
                    "default": 200,
                    "description": "Slow moving average period (days)",
                },
                "stop_loss_pct": {
                    "type": "string",
                    "default": "0.03",
                    "description": "Stop-loss distance as a decimal fraction (e.g. 0.03 = 3%)",
                },
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Ticker symbols this strategy trades",
                },
            },
            "required": [],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _hold(self, symbol: str) -> Signal:
        return Signal(
            symbol=symbol,
            action="HOLD",
            timestamp=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Self-registration — runs when this module is imported.
# ---------------------------------------------------------------------------

from app.core.strategy_engine.registry import registry  # noqa: E402

registry.register("moving_average", MovingAverageStrategy)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _sma(closes: list[Decimal], period: int) -> Decimal:
    """Simple moving average of the last `period` values."""
    return sum(closes[-period:]) / Decimal(period)
