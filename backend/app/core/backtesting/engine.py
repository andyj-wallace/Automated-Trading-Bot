"""
BacktestingEngine — replays a strategy against historical OHLCV data.

Each bar is presented to the strategy in chronological order. When a BUY or
SELL signal fires, a simulated trade is opened with risk validation applied
(same 1% and R:R gates as live trading). Simulated fills use the next bar's
open price (avoids look-ahead bias). The trade is tracked until:
  - price hits the stop-loss  → closed at stop
  - price hits the take-profit → closed at take-profit
  - backtest ends with no exit → closed at final bar's close

Results are returned as a BacktestResult containing the full trade log and
computed metrics (15.2).

Usage:
    engine = BacktestingEngine(strategy, risk_manager)
    result = await engine.run(bars, account_balance=Decimal("100000"))

Friction modelling:
  Fills apply `slippage_pct` against the raw bar price — unfavorably, in
  both directions (entry fills slightly worse, exits fill slightly worse)
  — plus a flat `commission_per_trade` charged on both the entry and exit
  leg. Defaults (5bps slippage, $1/leg commission) are a deliberately
  conservative approximation, not a real broker's fee schedule; the point
  is that backtest results are no longer a frictionless best case. Set both
  to 0 to restore the original frictionless behavior.

Limitations (intentional scope):
  - One open trade at a time (no pyramiding)
  - Long-only (SELL signals are skipped — RiskManager rejects shorts)
  - No partial fills
  - No walk-forward / train-test split — see backtesting/walk_forward.py for
    an explicit out-of-sample harness built on top of this engine; calling
    .run() directly on the same data used to tune strategy parameters can
    still overfit if you don't use it.
"""

from __future__ import annotations

import math as math
from bisect import bisect_right as bisect_right
from dataclasses import dataclass as dataclass
from dataclasses import field as field
from datetime import datetime as datetime
from decimal import ROUND_HALF_UP as ROUND_HALF_UP
from decimal import Decimal
from uuid import UUID as UUID
from uuid import uuid4

from app.brokers.base import PriceBar
from app.core.backtesting.fills import apply_slippage, exit_fill, leg_commission, visible_extra_bars
from app.core.backtesting.metrics import _compute_metrics
from app.core.backtesting.results import BacktestMetrics as BacktestMetrics
from app.core.backtesting.results import BacktestResult, SimulatedTrade
from app.core.risk.costs import CostModel
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class BacktestingEngine:
    """
    Drives a strategy through historical bars and collects simulated trades.

    Args:
        strategy:             A BaseStrategy instance configured for the backtest.
        risk_manager:         RiskManager instance (uses default 2:1 R:R, 1% rule).
        slippage_pct:         Unfavorable price adjustment applied to every fill
                              (entry and exit), as a fraction of price. Default
                              "0.0005" (5bps). Set to "0" to disable.
        commission_per_trade: Flat fee charged on the entry leg and again on the
                              exit leg. Default "1.00". Set to "0" to disable.
        cost_model:           When set, each leg pays the broker's real fee schedule
                              (see core/risk/costs.py) instead of the flat fee.
        compound:             Size each entry from the initial balance plus closed
                              P&L instead of holding the balance fixed.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        risk_manager: RiskManager,
        slippage_pct: Decimal = Decimal("0.0005"),
        commission_per_trade: Decimal = Decimal("1.00"),
        *,
        cost_model: CostModel | None = None,
        compound: bool = False,
    ) -> None:
        self._strategy = strategy
        self._risk_manager = risk_manager
        self._slippage_pct = slippage_pct
        self._commission_per_trade = commission_per_trade
        self._cost_model = cost_model
        self._compound = compound

    async def run(
        self,
        bars: list[PriceBar],
        symbol: str,
        account_balance: Decimal,
        strategy_type: str = "unknown",
        strategy_config: dict | None = None,
        extra_bars: dict[str, list[PriceBar]] | None = None,
    ) -> BacktestResult:
        """
        Replay the strategy over `bars` and return a full BacktestResult.

        Args:
            bars:             Chronological OHLCV bars (oldest first).
            symbol:           Ticker symbol used for logging/results.
            account_balance:  Starting equity; held fixed throughout the run.
            strategy_type:    Name tag for the result (e.g. "moving_average").
            strategy_config:  Config dict tag for the result.
            extra_bars:       Bars for other symbols a strategy needs (e.g. SPY for a
                              regime filter); each step sees only bars dated <= the
                              current bar.
        """
        if len(bars) < 2:
            raise ValueError("Need at least 2 bars to run a backtest.")

        result = BacktestResult(
            symbol=symbol,
            strategy_type=strategy_type,
            strategy_config=strategy_config or {},
            account_balance=account_balance,
            start_time=bars[0].timestamp,
            end_time=bars[-1].timestamp,
        )

        open_trade: SimulatedTrade | None = None
        signals_generated = 0
        signals_rejected = 0
        extra_bars = extra_bars or {}
        extra_times = {sym: [b.timestamp for b in series] for sym, series in extra_bars.items()}

        # We need at least 1 previous bar before we can call generate_signal
        # (the strategy itself may need more, but we start iteration at bar 1).
        for i in range(1, len(bars)):
            current_bar = bars[i]
            history = bars[: i + 1]  # bars up to and including current

            # 1. Check if open trade exits on this bar
            if open_trade is not None:
                open_trade = self._check_exit(open_trade, current_bar, i)
                if open_trade.exit_price is not None:
                    result.trades.append(open_trade)
                    open_trade = None
                    continue  # don't enter a new trade on the same bar we just exited

            # 2. No open trade — generate signal from strategy
            if open_trade is None:
                open_trade, generated, rejected = await self._try_enter(
                    symbol, bars, i, current_bar, history,
                    extra_bars, extra_times, account_balance, result,
                )
                signals_generated += generated
                signals_rejected += rejected

        # Close any trade still open at end of data
        if open_trade is not None:
            self._close_at_end(open_trade, bars)
            result.trades.append(open_trade)

        result.metrics = _compute_metrics(
            trades=result.trades,
            initial_balance=account_balance,
            bars_tested=len(bars),
            signals_generated=signals_generated,
            signals_rejected=signals_rejected,
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _try_enter(
        self,
        symbol: str,
        bars: list[PriceBar],
        i: int,
        current_bar: PriceBar,
        history: list[PriceBar],
        extra_bars: dict[str, list[PriceBar]],
        extra_times: dict[str, list[datetime]],
        account_balance: Decimal,
        result: BacktestResult,
    ) -> tuple[SimulatedTrade | None, int, int]:
        """Evaluate the strategy on this bar and try to open a trade on a BUY."""
        signals_generated = 0
        market_data = MarketData(
            symbol=symbol,
            current_price=current_bar.close,
            bars=history,
            timestamp=current_bar.timestamp,
            extra_bars=visible_extra_bars(extra_bars, extra_times, current_bar.timestamp),
        )
        signal = await self._strategy.generate_signal(market_data)

        if signal.action == "HOLD":
            return None, 0, 0

        if signal.action == "SELL":
            # Skip shorts — RiskManager long-only guard would reject anyway
            return None, 0, 0

        signals_generated += 1

        open_trade, signals_rejected = await self._try_open(
            symbol, bars, i, signal, account_balance, result
        )
        return open_trade, signals_generated, signals_rejected

    async def _try_open(
        self,
        symbol: str,
        bars: list[PriceBar],
        i: int,
        signal: Signal,
        account_balance: Decimal,
        result: BacktestResult,
    ) -> tuple[SimulatedTrade | None, int]:
        """Run the entry gates for `signal`; return the new trade (or None) and rejections."""
        signals_rejected = 0
        # Fill at next bar's open (look-ahead safe only if i+1 < len)
        if i + 1 >= len(bars):
            return None, signals_rejected
        fill_bar = bars[i + 1]
        raw_fill_price = fill_bar.open

        # Risk gates run on the intended (pre-slippage) price, matching
        # live OrderManager: validation happens before the broker call,
        # using the requested price — slippage is only realized after,
        # in the actual fill. This keeps gate accept/reject decisions
        # identical to the frictionless case; only PnL is affected.
        balance = account_balance
        if self._compound:
            balance += sum((t.pnl or Decimal("0") for t in result.trades), Decimal("0"))
            if balance <= 0:
                return None, signals_rejected
        risk_params = RiskParams(
            account_balance=balance,
            entry_price=raw_fill_price,
            stop_loss_price=signal.stop_loss_price or (raw_fill_price * Decimal("0.97")),
        )
        quantity = await self._strategy.calculate_position_size(risk_params)
        if quantity <= 0:
            signals_rejected += 1
            return None, signals_rejected

        # Run through risk gates
        stop = signal.stop_loss_price or (raw_fill_price * Decimal("0.97")).quantize(Decimal("0.01"))
        tp_hint = signal.take_profit_price

        request = TradeRequest(
            trade_id=uuid4(),
            symbol=symbol,
            direction="BUY",
            quantity=Decimal(str(quantity)),
            entry_price=raw_fill_price,
            stop_loss_price=stop,
            account_balance=balance,
            take_profit_price=tp_hint,
        )
        try:
            validation = self._risk_manager.validate(request)
        except RiskRejectionError:
            signals_rejected += 1
            return None, signals_rejected

        # Long entry: slippage works against us — we pay slightly more.
        fill_price = self._apply_slippage(raw_fill_price, worse_direction=1)
        risk_amt = Decimal(str(quantity)) * (fill_price - request.stop_loss_price)
        open_trade = SimulatedTrade(
            trade_id=request.trade_id,
            symbol=symbol,
            direction="BUY",
            entry_price=fill_price,
            stop_loss_price=request.stop_loss_price,
            take_profit_price=validation.take_profit_price,
            quantity=quantity,
            risk_amount=risk_amt,
            entry_bar_index=i + 1,
            entry_time=fill_bar.timestamp,
            commission_paid=leg_commission(
                self._cost_model, self._commission_per_trade, quantity, fill_price
            ),  # entry leg
        )
        return open_trade, signals_rejected

    def _close_at_end(self, open_trade: SimulatedTrade, bars: list[PriceBar]) -> None:
        """Close a trade still open at the end of the data at the last bar's close."""
        last_bar = bars[-1]
        # Long exit: slippage works against us — we receive slightly less.
        open_trade.exit_price = self._apply_slippage(last_bar.close, worse_direction=-1)
        open_trade.exit_time = last_bar.timestamp
        open_trade.exit_reason = "END_OF_DATA"
        open_trade.commission_paid += leg_commission(
            self._cost_model, self._commission_per_trade, open_trade.quantity, open_trade.exit_price
        )  # exit leg
        open_trade.pnl = (
            (open_trade.exit_price - open_trade.entry_price) * open_trade.quantity
            - open_trade.commission_paid
        )

    def _check_exit(
        self, trade: SimulatedTrade, bar: PriceBar, bar_index: int
    ) -> SimulatedTrade:
        """
        Check whether stop or take-profit is hit on `bar`.

        See exit_fill(): a bar that gaps past a level fills at its open; if both
        levels are hit on the same bar, the stop wins (conservative assumption).

        Exit fills apply slippage (unfavorable) and the exit-leg commission,
        same as a real close order would incur.
        """
        filled = exit_fill(trade, bar, self._slippage_pct)
        if filled is not None:
            trade.exit_price, trade.exit_reason = filled
            trade.exit_time = bar.timestamp
            trade.commission_paid += leg_commission(
                self._cost_model, self._commission_per_trade, trade.quantity, trade.exit_price
            )  # exit leg
            trade.pnl = (
                (trade.exit_price - trade.entry_price) * trade.quantity - trade.commission_paid
            )

        return trade

    def _apply_slippage(self, price: Decimal, *, worse_direction: int) -> Decimal:
        """Adjust `price` unfavorably by this engine's `_slippage_pct`."""
        return apply_slippage(price, worse_direction, self._slippage_pct)
