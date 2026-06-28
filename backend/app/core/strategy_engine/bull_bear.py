"""
BullBearStrategy — regime-filtered long entries using SPY as a market barometer.

Logic:
  Regime detection — SPY close vs its `regime_period`-day SMA:
    · SPY close > SMA  → bull regime  (long entries permitted)
    · SPY close ≤ SMA  → bear regime  → HOLD for all symbols
                                         (or buy the inverse-ETF hedge leg —
                                         see "Bear-market hedge" below)

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

  Bear-market hedge (interim stand-in for short selling):
    True short selling is not implemented — RiskManager is long-only by
    design today (see ENABLE_SHORT_SELLING in app/config.py, and the
    NotImplementedError stubs in risk/calculator.py and risk/manager.py
    gated behind it). As an interim way to still respond to a confirmed
    downtrend instead of just holding cash, this strategy can be configured
    with `inverse_etf_symbol` (e.g. "SH", an S&P 500 inverse ETF). Add that
    same ticker to `symbols` so the scheduler evaluates it each cycle: when
    evaluating that symbol and the regime is bearish, it buys the inverse
    ETF outright (ATR stop, 2:1 take-profit) — no separate technical trigger
    is required, the regime call itself is the signal. This stays entirely
    within the existing long-only order/PnL model.

Whipsaw/chop filter:
  A bare price-vs-MA crossover fires on any margin, including a fraction of
  a cent in a sideways market. `min_separation_pct` (default "0.001" = 0.1%)
  requires price to clear the entry SMA by at least that fraction of the
  SMA's value before a cross counts as tradeable. Set to "0" to restore the
  original bare crossover behavior. Does not apply to the inverse-ETF hedge
  leg, which is gated purely on the regime call.

Config keys:
    regime_symbol       str        Ticker used for regime detection (default "SPY")
    regime_period       int        SMA period for regime detection (default 200)
    entry_period        int        SMA period for target entry crossover (default 50)
    atr_period          int        ATR lookback for stop placement (default 14)
    atr_multiplier      str        ATR multiple for the stop distance (default "1.5")
    min_separation_pct  str        Minimum price/SMA separation to count a target
                                    entry cross as tradeable (default "0.001" = 0.1%)
    inverse_etf_symbol  str | None Ticker to buy as a bear-market hedge when the
                                    regime is bearish (default None = disabled).
                                    Must also be included in `symbols`.
    symbols             list       Ticker symbols this strategy trades (default [])

Regime data:
    SPY bars must be supplied in `market_data.extra_bars[regime_symbol]`.
    The scheduler populates this automatically via required_extra_symbols().
    When absent, the strategy returns HOLD (safe default).

Registration:
    Self-registers as "bull_bear" when this module is imported.
    Import once on app startup (see main.py lifespan).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from app.brokers.base import PriceBar
from app.core.risk.calculator import RiskCalculator
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal
from app.core.strategy_engine.filters import min_separation_ok

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
        inverse_etf_symbol = config.get("inverse_etf_symbol")
        self.inverse_etf_symbol: str | None = (
            str(inverse_etf_symbol).upper() if inverse_etf_symbol else None
        )
        self.min_separation_pct: Decimal = Decimal(
            str(config.get("min_separation_pct", "0.001"))
        )
        self._calculator = RiskCalculator()

        if self.regime_period < 1:
            raise ValueError(f"regime_period must be >= 1, got {self.regime_period}")
        if self.entry_period < 1:
            raise ValueError(f"entry_period must be >= 1, got {self.entry_period}")
        if self.atr_period < 1:
            raise ValueError(f"atr_period must be >= 1, got {self.atr_period}")
        if self.atr_multiplier <= Decimal("0"):
            raise ValueError(f"atr_multiplier must be > 0, got {self.atr_multiplier}")
        if self.min_separation_pct < Decimal("0"):
            raise ValueError(
                f"min_separation_pct must be >= 0, got {self.min_separation_pct}"
            )

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    async def generate_signal(self, market_data: MarketData) -> Signal:
        """
        Analyse market data and return BUY or HOLD.

        Returns HOLD when:
          - Regime bars are missing or insufficient (< regime_period + 1).
          - Target bars are insufficient (< max(entry_period, atr_period) + 1).
          - Bear regime and no inverse-ETF hedge configured for this symbol.
          - No crossover detected on the target symbol (bull regime).
          - ATR stop calculation yields a stop ≥ entry (degenerate case).
        """
        regime_bars = market_data.extra_bars.get(self.regime_symbol, [])

        # --- Guard: regime bars ---
        if len(regime_bars) < self.regime_period + 1:
            return self._hold(market_data.symbol)

        # --- Regime check ---
        regime_closes = [b.close for b in regime_bars]
        regime_ma = _sma(regime_closes, self.regime_period)
        is_bear = regime_closes[-1] <= regime_ma

        # --- Bear-market hedge leg: this symbol is the configured inverse ETF ---
        if self.inverse_etf_symbol and market_data.symbol.upper() == self.inverse_etf_symbol:
            return self._inverse_etf_signal(market_data, is_bear)

        if is_bear:
            return self._hold(market_data.symbol)

        # --- Guard: target bars ---
        target_required = max(self.entry_period, self.atr_period) + 1
        bars = market_data.bars
        if len(bars) < target_required:
            return self._hold(market_data.symbol)

        # --- Entry: crossover of target symbol above its entry_period SMA ---
        closes = [b.close for b in bars]
        ma_now = _sma(closes, self.entry_period)
        ma_prev = _sma(closes[:-1], self.entry_period)
        current_close = closes[-1]
        prev_close = closes[-2]

        if not (
            prev_close <= ma_prev
            and current_close > ma_now
            and min_separation_ok(current_close, ma_now, self.min_separation_pct)
        ):
            return self._hold(market_data.symbol)

        stop, take_profit = self._atr_stop_and_target(bars, market_data.current_price)
        if stop is None:
            return self._hold(market_data.symbol)

        return Signal(
            symbol=market_data.symbol,
            action="BUY",
            entry_price=market_data.current_price,
            stop_loss_price=stop,
            take_profit_price=take_profit,
            timestamp=market_data.timestamp,
        )

    def _inverse_etf_signal(self, market_data: MarketData, is_bear: bool) -> Signal:
        """
        Bear-market hedge leg: buy the configured inverse ETF outright while
        the regime is bearish — the regime call itself is the signal, no
        separate technical trigger on the ETF's own price is required.

        Interim stand-in for true short selling: buying an inverse ETF
        achieves bearish exposure while staying inside the existing
        long-only order/PnL model. Once the regime flips back to bullish,
        no explicit exit signal is emitted — the open hedge position is
        managed the same way every other trade is, via PositionMonitor's
        stop/take-profit (and trailing-stop / stale-trade timeout).
        """
        if not is_bear:
            return self._hold(market_data.symbol)

        bars = market_data.bars
        if len(bars) < self.atr_period + 1:
            return self._hold(market_data.symbol)

        stop, take_profit = self._atr_stop_and_target(bars, market_data.current_price)
        if stop is None:
            return self._hold(market_data.symbol)

        return Signal(
            symbol=market_data.symbol,
            action="BUY",
            entry_price=market_data.current_price,
            stop_loss_price=stop,
            take_profit_price=take_profit,
            timestamp=market_data.timestamp,
        )

    def _atr_stop_and_target(
        self, bars: list[PriceBar], entry: Decimal
    ) -> tuple[Decimal, Decimal] | tuple[None, None]:
        """ATR-based stop and 2:1 take-profit; (None, None) if degenerate."""
        atr = _atr(bars, self.atr_period)
        stop = (entry - atr * self.atr_multiplier).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
        if stop >= entry:
            return None, None
        stop_distance = entry - stop
        take_profit = (entry + stop_distance * 2).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
        return stop, take_profit

    def required_extra_symbols(self) -> list[str]:
        return [self.regime_symbol]

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
                "min_separation_pct": {
                    "type": "string",
                    "default": "0.001",
                    "description": (
                        "Minimum price/SMA separation (as a fraction of the SMA) required "
                        "for the target entry crossover to count as tradeable — filters "
                        "out noise-level crosses in choppy markets. '0' disables the filter."
                    ),
                },
                "inverse_etf_symbol": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": (
                        "Ticker to buy as a bear-market hedge when the regime is "
                        "bearish (e.g. 'SH'). Must also be listed in 'symbols'. "
                        "Interim stand-in for short selling — see ENABLE_SHORT_SELLING."
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
