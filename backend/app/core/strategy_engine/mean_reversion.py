"""
MeanReversionStrategy — buys when price deviates below its rolling mean.

Logic:
  BUY signal  — price crosses below (mean − z_score_threshold × std_dev)
                for the first time (crossover, not continuation).
                Take-profit target = the rolling mean (reversion target).
                Stop-loss placed stop_loss_pct% below entry.

  HOLD        — all other conditions, including:
                  · insufficient bars (< lookback + 1)
                  · price already below the band on the previous bar
                    (avoids re-entering an open deviation zone)
                  · price within normal range

NOTE on SELL/short: extending this strategy to short when price crosses
above the upper band (mean + z × std) is a natural complement, but short
selling requires risk-engine support for stop_loss_price > entry_price.
That gate is not yet open; the SELL direction is omitted here and noted
for a future extension (see STR-04 in requirements.md).

Config keys:
    lookback           int   Rolling window in bars for mean/std (default 20)
    z_score_threshold  str   Band width in standard deviations (default "2.0")
    stop_loss_pct      str   Stop-loss distance below entry as a fraction (default "0.02")
    symbols            list  Ticker symbols this strategy trades (default [])

Registration:
    Self-registers as "mean_reversion" when this module is imported.
    Import once on app startup (see main.py lifespan) to make the type
    available to the StrategyScheduler.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from app.core.risk.calculator import RiskCalculator
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal

_TWO_DP = Decimal("0.01")


class MeanReversionStrategy(BaseStrategy):
    """
    Mean reversion strategy: enters a long when price deviates below
    its rolling mean and exits when price reverts to that mean.

    The strategy fires once per deviation event: it detects the first bar
    where price crosses below the lower band, then holds off on re-signalling
    until price has reverted (price is no longer below the band on the
    previous bar).

    Take-profit is set to whichever is larger:
      - the rolling mean (the natural reversion target), or
      - entry + 2 × stop_distance (minimum R:R the risk engine requires).
    The RiskManager enforces the 2:1 floor anyway; setting it here ensures
    the strategy's suggested take-profit is accurate rather than just
    triggering the mechanical floor every time.
    """

    def __init__(self, config: dict) -> None:
        self.lookback: int = int(config.get("lookback", 20))
        self.z_score_threshold: Decimal = Decimal(
            str(config.get("z_score_threshold", "2.0"))
        )
        self.stop_loss_pct: Decimal = Decimal(str(config.get("stop_loss_pct", "0.02")))
        self._calculator = RiskCalculator()

        if self.lookback < 2:
            raise ValueError(
                f"lookback must be at least 2, got {self.lookback}"
            )
        if self.z_score_threshold <= Decimal("0"):
            raise ValueError(
                f"z_score_threshold must be > 0, got {self.z_score_threshold}"
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
          - Fewer than lookback + 1 bars are available.
          - Price was already below the lower band on the previous bar
            (deviation already in progress — avoids repeat entry).
          - No deviation event detected on the current bar.
        """
        bars = market_data.bars
        required = self.lookback + 1  # +1 so we can compare current vs previous bar

        if len(bars) < required:
            return self._hold(market_data.symbol)

        closes = [bar.close for bar in bars]

        # Current bar window
        window_now = closes[-self.lookback:]
        mean_now = _mean(window_now)
        std_now = _pstdev(window_now)

        # Previous bar window (one bar earlier)
        window_prev = closes[-self.lookback - 1:-1]
        mean_prev = _mean(window_prev)
        std_prev = _pstdev(window_prev)

        current_close = closes[-1]
        prev_close = closes[-2]

        lower_band_now = mean_now - self.z_score_threshold * std_now
        lower_band_prev = mean_prev - self.z_score_threshold * std_prev

        # BUY: price JUST crossed below the lower band (first bar of deviation)
        # prev_close was at or above the band; current_close is below it.
        if prev_close >= lower_band_prev and current_close < lower_band_now:
            entry = market_data.current_price
            stop = (entry * (1 - self.stop_loss_pct)).quantize(
                _TWO_DP, rounding=ROUND_HALF_UP
            )
            stop_distance = entry - stop

            # Take-profit: rolling mean, but never below the 2:1 R:R floor.
            mean_target = mean_now.quantize(_TWO_DP, rounding=ROUND_HALF_UP)
            min_take_profit = (entry + stop_distance * 2).quantize(
                _TWO_DP, rounding=ROUND_HALF_UP
            )
            take_profit = max(mean_target, min_take_profit)

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
        """JSON Schema for the mean reversion strategy configuration."""
        return {
            "type": "object",
            "properties": {
                "lookback": {
                    "type": "integer",
                    "minimum": 2,
                    "default": 20,
                    "description": "Rolling window in bars for mean/std calculation",
                },
                "z_score_threshold": {
                    "type": "string",
                    "default": "2.0",
                    "description": (
                        "Band width in standard deviations — "
                        "price must cross below mean − (z × std) to trigger entry"
                    ),
                },
                "stop_loss_pct": {
                    "type": "string",
                    "default": "0.02",
                    "description": (
                        "Stop-loss distance below entry as a decimal fraction "
                        "(e.g. '0.02' = 2%)"
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

registry.register("mean_reversion", MeanReversionStrategy)


# ---------------------------------------------------------------------------
# Utility — pure Decimal arithmetic avoids float precision drift.
# ---------------------------------------------------------------------------


def _mean(values: list[Decimal]) -> Decimal:
    """Arithmetic mean of a non-empty Decimal sequence."""
    return sum(values) / Decimal(len(values))


def _pstdev(values: list[Decimal]) -> Decimal:
    """
    Population standard deviation of a non-empty Decimal sequence.

    Uses Decimal.sqrt() for exact arithmetic throughout.
    Returns Decimal("0") when all values are identical (zero variance).
    """
    n = Decimal(len(values))
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return variance.sqrt()
