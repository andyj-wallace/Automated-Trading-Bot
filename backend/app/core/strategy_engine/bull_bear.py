"""
BullBearStrategy — regime-filtered long entries using SPY as a market barometer.

Logic:
  Regime detection — SPY close vs its `regime_period`-day SMA:
    · SPY close > SMA  → bull regime  (long entries permitted)
    · SPY close ≤ SMA  → bear regime  → HOLD for all symbols

  Entry (bull regime only) — target symbol crosses ABOVE its `entry_period`-day SMA:
    · First bar where prev_close ≤ ma_prev AND current_close > ma_now → BUY
    · All other conditions → HOLD

  Stop-loss: ATR-based — stop = entry − (ATR(atr_period) × atr_multiplier)
    Widens the stop in volatile regimes; tightens it in calm ones.

  Take-profit: entry + 2 × stop_distance (minimum 2:1 R:R for the risk engine).

  HOLD conditions:
    · Insufficient regime bars (< regime_period + 1)
    · Insufficient target bars (< max(entry_period, atr_period) + 1)
    · Bear regime (SPY below its SMA)
    · No crossover on the target symbol
    · Degenerate ATR (stop ≥ entry)

Config keys:
    regime_symbol   str   Ticker used for regime detection (default "SPY")
    regime_period   int   SMA period for regime detection (default 200)
    entry_period    int   SMA period for target entry crossover (default 50)
    atr_period      int   ATR lookback for stop placement (default 14)
    atr_multiplier  str   ATR multiple for the stop distance (default "1.5")
    symbols         list  Ticker symbols this strategy trades (default [])

Regime data:
    SPY bars must be supplied in `market_data.extra_bars[regime_symbol]`.
    When absent, the strategy returns HOLD (safe default).

Registration:
    Self-registers as "bull_bear" when this module is imported.
    Import once on app startup (see main.py lifespan).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.brokers.base import PriceBar
from app.core.risk.calculator import RiskCalculator
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal

_TWO_DP = Decimal("0.01")


class BullBearStrategy(BaseStrategy):
    """
    Regime-filtered trend strategy: only enters longs when the broad market
    (default: SPY) is in a bull regime (above its 200-day SMA). Within that
    regime, fires a BUY when the target symbol crosses above its 50-day SMA.
    Stop-loss is ATR-based so it adapts to current volatility.
    """

    def __init__(self, config: dict) -> None:
        self.regime_symbol: str = str(config.get("regime_symbol", "SPY")).upper()
        self.regime_period: int = int(config.get("regime_period", 200))
        self.entry_period: int = int(config.get("entry_period", 50))
        self.atr_period: int = int(config.get("atr_period", 14))
        self.atr_multiplier: Decimal = Decimal(str(config.get("atr_multiplier", "1.5")))
        self._calculator = RiskCalculator()

        if self.regime_period < 1:
            raise ValueError(f"regime_period must be >= 1, got {self.regime_period}")
        if self.entry_period < 1:
            raise ValueError(f"entry_period must be >= 1, got {self.entry_period}")
        if self.atr_period < 1:
            raise ValueError(f"atr_period must be >= 1, got {self.atr_period}")
        if self.atr_multiplier <= Decimal("0"):
            raise ValueError(f"atr_multiplier must be > 0, got {self.atr_multiplier}")

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    async def generate_signal(self, market_data: MarketData) -> Signal:
        """
        Analyse market data and return BUY or HOLD.

        Returns HOLD when:
          - Regime bars are missing or insufficient (< regime_period + 1).
          - Target bars are insufficient (< max(entry_period, atr_period) + 1).
          - Bear regime: SPY close ≤ its regime_period SMA.
          - No crossover detected on the target symbol.
          - ATR stop calculation yields a stop ≥ entry (degenerate case).
        """
        regime_bars = market_data.extra_bars.get(self.regime_symbol, [])

        # --- Guard: regime bars ---
        if len(regime_bars) < self.regime_period + 1:
            return self._hold(market_data.symbol)

        # --- Guard: target bars ---
        target_required = max(self.entry_period, self.atr_period) + 1
        bars = market_data.bars
        if len(bars) < target_required:
            return self._hold(market_data.symbol)

        # --- Regime check ---
        regime_closes = [b.close for b in regime_bars]
        regime_ma = _sma(regime_closes, self.regime_period)
        if regime_closes[-1] <= regime_ma:
            return self._hold(market_data.symbol)  # bear regime

        # --- Entry: crossover of target symbol above its entry_period SMA ---
        closes = [b.close for b in bars]
        ma_now = _sma(closes, self.entry_period)
        ma_prev = _sma(closes[:-1], self.entry_period)
        current_close = closes[-1]
        prev_close = closes[-2]

        if not (prev_close <= ma_prev and current_close > ma_now):
            return self._hold(market_data.symbol)

        # --- ATR-based stop ---
        entry = market_data.current_price
        atr = _atr(bars, self.atr_period)
        stop = (entry - atr * self.atr_multiplier).quantize(_TWO_DP, rounding=ROUND_HALF_UP)

        if stop >= entry:
            return self._hold(market_data.symbol)

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
        """JSON Schema for the bull/bear regime strategy configuration."""
        return {
            "type": "object",
            "properties": {
                "regime_symbol": {
                    "type": "string",
                    "default": "SPY",
                    "description": "Ticker used to determine market regime (default SPY)",
                },
                "regime_period": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 200,
                    "description": "SMA period for regime detection (default 200-day)",
                },
                "entry_period": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 50,
                    "description": "SMA period for target symbol entry crossover (default 50-day)",
                },
                "atr_period": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 14,
                    "description": "ATR lookback period for stop-loss placement (default 14)",
                },
                "atr_multiplier": {
                    "type": "string",
                    "default": "1.5",
                    "description": (
                        "ATR multiple for stop distance "
                        "(e.g. '1.5' means stop = entry − 1.5 × ATR)"
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

registry.register("bull_bear", BullBearStrategy)


# ---------------------------------------------------------------------------
# Utility — pure Decimal arithmetic avoids float precision drift.
# ---------------------------------------------------------------------------


def _sma(closes: list[Decimal], period: int) -> Decimal:
    """Simple moving average of the last `period` values in a Decimal sequence."""
    return sum(closes[-period:]) / Decimal(period)


def _atr(bars: list[PriceBar], period: int) -> Decimal:
    """
    Average True Range over the last `period` bars.

    True Range for bar i = max(high−low, |high−prev_close|, |low−prev_close|).
    Requires at least period+1 bars so `period` TRs can be computed.
    """
    trs: list[Decimal] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - prev_close),
            abs(bars[i].low - prev_close),
        )
        trs.append(tr)
    recent = trs[-period:]
    return sum(recent) / Decimal(len(recent))
