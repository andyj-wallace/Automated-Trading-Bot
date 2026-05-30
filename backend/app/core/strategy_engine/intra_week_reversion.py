"""
IntraWeekMeanReversionStrategy — buys short-term pullbacks to the weekly VWAP
within longer-term uptrends.

Logic:
  Trend filter:   Only BUY-eligible when bars[-1].close > `ma_period`-day SMA.
                  Ensures entries are taken with the dominant trend, not against it.

  Entry trigger:  current_price ≤ weekly_vwap × (1 − pullback_pct)
                  Weekly VWAP is computed from the current calendar week's bars
                  (Mon through the current bar): sum(close×volume) / sum(volume).
                  A pullback_pct% intra-week drop below VWAP signals a mean-
                  reversion entry.

  Stop-loss:      Lowest low from the prior calendar week.
                  The prior week's structural low; a break below it invalidates
                  the uptrend thesis.

  Take-profit:    weekly_vwap + (weekly_vwap − stop) × rr_ratio
                  Targets a return to above-VWAP levels at the configured R:R
                  multiple.

  Position guard: Returns HOLD when market_data.open_position is True, preventing
                  duplicate entries on the same symbol in the same cycle.

  HOLD conditions:
    · Fewer than ma_period bars available
    · bars[-1].close ≤ ma_period SMA (no uptrend)
    · open_position is True (already in a trade)
    · No prior-week bars found (can't compute stop-loss)
    · No current-week bars found (can't compute VWAP)
    · Zero volume in current week (can't compute VWAP)
    · current_price > weekly_vwap × (1 − pullback_pct) (no pullback yet)
    · prior week low ≥ current_price (degenerate stop)
    · take_profit ≤ current_price (degenerate take-profit)

Config keys:
    ma_period    int   Trend filter SMA period in bars (default 50)
    pullback_pct str   Required pullback below VWAP as a fraction (default "0.02" = 2%)
    rr_ratio     str   Take-profit distance as a multiple of VWAP-to-stop distance
                       (default "2.0")
    symbols      list  Ticker symbols this strategy trades (default [])

Week identification:
    Uses ISO calendar week numbers (Monday = start of week). Weeks that span a
    year boundary are handled correctly since isocalendar() returns the ISO year.

Registration:
    Self-registers as "intra_week_reversion" when this module is imported.
    Import once on app startup (see main.py lifespan).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.brokers.base import PriceBar
from app.core.risk.calculator import RiskCalculator
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal

_TWO_DP = Decimal("0.01")


class IntraWeekMeanReversionStrategy(BaseStrategy):
    """
    Intra-week mean reversion strategy: buys pullbacks to the weekly VWAP
    when the stock is in a confirmed uptrend and has pulled back ≥ pullback_pct
    below the weekly VWAP. Stop is the prior week's structural low; take-profit
    is a configured multiple of the VWAP-to-stop distance.
    """

    def __init__(self, config: dict) -> None:
        self.ma_period: int = int(config.get("ma_period", 50))
        self.pullback_pct: Decimal = Decimal(str(config.get("pullback_pct", "0.02")))
        self.rr_ratio: Decimal = Decimal(str(config.get("rr_ratio", "2.0")))
        self._calculator = RiskCalculator()

        if self.ma_period < 1:
            raise ValueError(f"ma_period must be >= 1, got {self.ma_period}")
        if self.pullback_pct <= Decimal("0") or self.pullback_pct >= Decimal("1"):
            raise ValueError(
                f"pullback_pct must be between 0 and 1, got {self.pullback_pct}"
            )
        if self.rr_ratio <= Decimal("0"):
            raise ValueError(f"rr_ratio must be > 0, got {self.rr_ratio}")

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    async def generate_signal(self, market_data: MarketData) -> Signal:
        """
        Analyse bars and return BUY or HOLD.

        Returns HOLD when any of the guard conditions in the module docstring
        are triggered.
        """
        bars = market_data.bars

        # --- Guard: sufficient bars for trend MA ---
        if len(bars) < self.ma_period:
            return self._hold(market_data.symbol)

        # --- Trend filter: last bar close must be above the SMA ---
        closes = [b.close for b in bars]
        ma = _sma(closes, self.ma_period)
        if closes[-1] <= ma:
            return self._hold(market_data.symbol)

        # --- Guard: no open position already ---
        if market_data.open_position:
            return self._hold(market_data.symbol)

        # --- Split bars by ISO calendar week ---
        current_week = _current_week_bars(bars)
        prior_week = _prior_week_bars(bars)

        if not current_week or not prior_week:
            return self._hold(market_data.symbol)

        # --- Weekly VWAP ---
        total_vol = sum(b.volume for b in current_week)
        if total_vol == 0:
            return self._hold(market_data.symbol)

        weekly_vwap = (
            sum(b.close * Decimal(str(b.volume)) for b in current_week)
            / Decimal(str(total_vol))
        )

        # --- Entry trigger: pullback below threshold ---
        threshold = weekly_vwap * (Decimal("1") - self.pullback_pct)
        if market_data.current_price > threshold:
            return self._hold(market_data.symbol)

        # --- Stop-loss: prior week's low ---
        entry = market_data.current_price
        stop = min(b.low for b in prior_week).quantize(_TWO_DP, rounding=ROUND_HALF_UP)

        if stop >= entry:
            return self._hold(market_data.symbol)

        # --- Take-profit: VWAP + (VWAP − stop) × rr_ratio ---
        take_profit = (
            weekly_vwap + (weekly_vwap - stop) * self.rr_ratio
        ).quantize(_TWO_DP, rounding=ROUND_HALF_UP)

        if take_profit <= entry:
            return self._hold(market_data.symbol)

        return Signal(
            symbol=market_data.symbol,
            action="BUY",
            entry_price=entry,
            stop_loss_price=stop,
            take_profit_price=take_profit,
            timestamp=market_data.timestamp,
        )

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
        """JSON Schema for the intra-week mean reversion strategy configuration."""
        return {
            "type": "object",
            "properties": {
                "ma_period": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 50,
                    "description": "Trend filter SMA period in bars (default 50-day)",
                },
                "pullback_pct": {
                    "type": "string",
                    "default": "0.02",
                    "description": (
                        "Required pullback below weekly VWAP as a decimal fraction "
                        "(e.g. '0.02' = 2% below VWAP triggers entry)"
                    ),
                },
                "rr_ratio": {
                    "type": "string",
                    "default": "2.0",
                    "description": (
                        "Take-profit distance as a multiple of the VWAP-to-stop distance "
                        "(e.g. '2.0' means take_profit = VWAP + 2 × (VWAP − stop))"
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
            timestamp=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# Self-registration — runs when this module is imported.
# ---------------------------------------------------------------------------

from app.core.strategy_engine.registry import registry  # noqa: E402

registry.register("intra_week_reversion", IntraWeekMeanReversionStrategy)


# ---------------------------------------------------------------------------
# Utility — pure Decimal arithmetic avoids float precision drift.
# ---------------------------------------------------------------------------


def _sma(closes: list[Decimal], period: int) -> Decimal:
    """Simple moving average of the last `period` values in a Decimal sequence."""
    return sum(closes[-period:]) / Decimal(period)


def _current_week_bars(bars: list[PriceBar]) -> list[PriceBar]:
    """Return bars belonging to the same ISO calendar week as the last bar."""
    if not bars:
        return []
    last_yw = bars[-1].timestamp.isocalendar()[:2]  # (ISO year, ISO week)
    return [b for b in bars if b.timestamp.isocalendar()[:2] == last_yw]


def _prior_week_bars(bars: list[PriceBar]) -> list[PriceBar]:
    """Return bars belonging to the ISO calendar week immediately before the last bar."""
    if not bars:
        return []
    prev_ts = bars[-1].timestamp - timedelta(weeks=1)
    prev_yw = prev_ts.isocalendar()[:2]
    return [b for b in bars if b.timestamp.isocalendar()[:2] == prev_yw]


def _vwap(bars: list[PriceBar]) -> Decimal:
    """
    Volume-weighted average price for the given bars.

    Raises ValueError when total volume is zero (cannot produce a meaningful VWAP).
    """
    total_vol = sum(b.volume for b in bars)
    if total_vol == 0:
        raise ValueError("Cannot compute VWAP: total volume is zero")
    return (
        sum(b.close * Decimal(str(b.volume)) for b in bars) / Decimal(str(total_vol))
    )
