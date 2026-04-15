"""
StockTrendStrategy — buys when price crosses above its N-day moving average.

Logic:
  BUY signal  — price crosses ABOVE the N-day simple moving average for the
                first time (crossover, not continuation).
                Stop-loss placed stop_loss_pct% below entry.
                Take-profit at entry + 2 × stop_distance (minimum 2:1 R:R).

  HOLD        — all other conditions, including:
                  · insufficient bars (< ma_period + 1)
                  · price was already above the MA on the previous bar
                    (avoids re-entering an established uptrend)
                  · price at or below the MA (downtrend — no long entry)

Rationale:
  The 200-day MA is a widely watched trend dividing line. A stock reclaiming
  its 200-day MA after a period below it signals a potential trend reversal
  that trend-followers want to capture. The crossover filter (first-bar-only)
  prevents spamming signals while a stock continues to trade above the MA.

NOTE on SELL/short: shorting below the MA is the natural complement, but
short-selling requires stop_loss_price > entry_price, which the risk engine
does not yet validate. The SELL direction is omitted pending that gate opening.

Config keys:
    ma_period      int   Rolling window in bars for the moving average (default 200)
    stop_loss_pct  str   Stop-loss distance below entry as a fraction (default "0.03")
    symbols        list  Ticker symbols this strategy trades (default [])

Registration:
    Self-registers as "stock_trend" when this module is imported.
    Import once on app startup (see main.py lifespan) to make the type
    available to the StrategyScheduler.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.core.risk.calculator import RiskCalculator
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal

_TWO_DP = Decimal("0.01")


class StockTrendStrategy(BaseStrategy):
    """
    Stock trend strategy: enters a long when price crosses above its N-day
    simple moving average, signalling a potential uptrend.

    The strategy fires once per crossover event: it detects the first bar
    where price moves from at-or-below the MA to above it, then holds off
    until price has dipped back below the MA and re-crossed (avoids
    continuous signals in an established uptrend).

    Take-profit is set at entry + 2 × stop_distance, satisfying the
    RiskManager's minimum 2:1 R:R requirement.
    """

    def __init__(self, config: dict) -> None:
        self.ma_period: int = int(config.get("ma_period", 200))
        self.stop_loss_pct: Decimal = Decimal(str(config.get("stop_loss_pct", "0.03")))
        self._calculator = RiskCalculator()

        if self.ma_period < 1:
            raise ValueError(
                f"ma_period must be at least 1, got {self.ma_period}"
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
        Analyse bars and return BUY or HOLD.

        Returns HOLD when:
          - Fewer than ma_period + 1 bars are available.
          - Price was already above the MA on the previous bar
            (crossover already in effect — avoids repeat entry).
          - Price is at or below the MA (downtrend, no signal).
        """
        bars = market_data.bars
        required = self.ma_period + 1  # +1 so we can compare current vs previous bar

        if len(bars) < required:
            return self._hold(market_data.symbol)

        closes = [bar.close for bar in bars]

        # Current bar: MA computed over the last ma_period closes
        ma_now = _sma(closes, self.ma_period)
        current_close = closes[-1]

        # Previous bar: MA computed over the ma_period closes ending one bar earlier
        ma_prev = _sma(closes[:-1], self.ma_period)
        prev_close = closes[-2]

        # BUY: price JUST crossed above the MA (first bar of confirmed uptrend)
        # prev_close was at or below the MA; current_close is strictly above it.
        if prev_close <= ma_prev and current_close > ma_now:
            entry = market_data.current_price
            stop = (entry * (1 - self.stop_loss_pct)).quantize(
                _TWO_DP, rounding=ROUND_HALF_UP
            )
            stop_distance = entry - stop
            take_profit = (entry + stop_distance * 2).quantize(
                _TWO_DP, rounding=ROUND_HALF_UP
            )
            return Signal(
                symbol=market_data.symbol,
                action="BUY",
                entry_price=entry,
                stop_loss_price=stop,
                take_profit_price=take_profit,
                timestamp=market_data.timestamp,
            )

        return self._hold(market_data.symbol)

    async def calculate_position_size(self, risk_params: RiskParams) -> int:
        """Return the maximum safe quantity using the 1% risk rule."""
        try:
            return self._calculator.max_quantity(
                risk_params.account_balance,
                risk_params.entry_price,
                risk_params.stop_loss_price,
            )
        except Exception:
            return 0

    def get_config_schema(self) -> dict:
        """JSON Schema for the stock trend strategy configuration."""
        return {
            "type": "object",
            "properties": {
                "ma_period": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 200,
                    "description": "Moving average window in bars (default 200-day)",
                },
                "stop_loss_pct": {
                    "type": "string",
                    "default": "0.03",
                    "description": (
                        "Stop-loss distance below entry as a decimal fraction "
                        "(e.g. '0.03' = 3%)"
                    ),
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

registry.register("stock_trend", StockTrendStrategy)


# ---------------------------------------------------------------------------
# Utility — pure Decimal arithmetic avoids float precision drift.
# ---------------------------------------------------------------------------


def _sma(closes: list[Decimal], period: int) -> Decimal:
    """Simple moving average of the last `period` values in a Decimal sequence."""
    return sum(closes[-period:]) / Decimal(period)
