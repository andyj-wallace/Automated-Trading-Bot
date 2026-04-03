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

Limitations (intentional scope):
  - One open trade at a time (no pyramiding)
  - Long-only (SELL signals are skipped — RiskManager rejects shorts)
  - Market-order fills at next open, no slippage or commission modelling
  - No partial fills
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from uuid import UUID, uuid4

from app.brokers.base import PriceBar
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass
class SimulatedTrade:
    trade_id: UUID
    symbol: str
    direction: str          # "BUY" (shorts skipped for now)
    entry_price: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal
    quantity: int
    risk_amount: Decimal
    entry_bar_index: int
    entry_time: datetime
    exit_price: Decimal | None = None
    exit_time: datetime | None = None
    exit_reason: str | None = None   # "STOP_LOSS" | "TAKE_PROFIT" | "END_OF_DATA"
    pnl: Decimal | None = None


@dataclass
class BacktestMetrics:
    """Computed summary metrics for a completed backtest (15.2)."""

    trade_count: int
    win_count: int
    loss_count: int
    win_rate_pct: float          # 0–100

    total_return: Decimal        # sum of all P&L
    total_return_pct: float      # total_return / initial_balance × 100
    avg_trade_pnl: Decimal
    avg_winner: Decimal
    avg_loser: Decimal
    largest_winner: Decimal
    largest_loser: Decimal

    max_drawdown_pct: float      # peak-to-trough on cumulative equity, 0–100
    sharpe_ratio: float          # annualised Sharpe (daily returns, rf=0)

    bars_tested: int
    signals_generated: int
    signals_rejected: int        # by RiskManager


@dataclass
class BacktestResult:
    symbol: str
    strategy_type: str
    strategy_config: dict
    account_balance: Decimal
    start_time: datetime
    end_time: datetime
    trades: list[SimulatedTrade] = field(default_factory=list)
    metrics: BacktestMetrics | None = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class BacktestingEngine:
    """
    Drives a strategy through historical bars and collects simulated trades.

    Args:
        strategy:     A BaseStrategy instance configured for the backtest.
        risk_manager: RiskManager instance (uses default 2:1 R:R, 1% rule).
    """

    def __init__(self, strategy: BaseStrategy, risk_manager: RiskManager) -> None:
        self._strategy = strategy
        self._risk_manager = risk_manager

    async def run(
        self,
        bars: list[PriceBar],
        symbol: str,
        account_balance: Decimal,
        strategy_type: str = "unknown",
        strategy_config: dict | None = None,
    ) -> BacktestResult:
        """
        Replay the strategy over `bars` and return a full BacktestResult.

        Args:
            bars:             Chronological OHLCV bars (oldest first).
            symbol:           Ticker symbol used for logging/results.
            account_balance:  Starting equity; held fixed throughout the run.
            strategy_type:    Name tag for the result (e.g. "moving_average").
            strategy_config:  Config dict tag for the result.
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
                market_data = MarketData(
                    symbol=symbol,
                    current_price=current_bar.close,
                    bars=history,
                    timestamp=current_bar.timestamp,
                )
                signal = await self._strategy.generate_signal(market_data)

                if signal.action == "HOLD":
                    continue

                if signal.action == "SELL":
                    # Skip shorts — RiskManager long-only guard would reject anyway
                    continue

                signals_generated += 1

                # Fill at next bar's open (look-ahead safe only if i+1 < len)
                if i + 1 >= len(bars):
                    continue
                fill_bar = bars[i + 1]
                fill_price = fill_bar.open

                # Size the position
                risk_params = RiskParams(
                    account_balance=account_balance,
                    entry_price=fill_price,
                    stop_loss_price=signal.stop_loss_price or (fill_price * Decimal("0.97")),
                )
                quantity = await self._strategy.calculate_position_size(risk_params)
                if quantity <= 0:
                    signals_rejected += 1
                    continue

                # Run through risk gates
                stop = signal.stop_loss_price or (fill_price * Decimal("0.97")).quantize(Decimal("0.01"))
                tp_hint = signal.take_profit_price

                request = TradeRequest(
                    trade_id=uuid4(),
                    symbol=symbol,
                    direction="BUY",
                    quantity=Decimal(str(quantity)),
                    entry_price=fill_price,
                    stop_loss_price=stop,
                    account_balance=account_balance,
                    take_profit_price=tp_hint,
                )
                try:
                    validation = self._risk_manager.validate(request)
                except RiskRejectionError:
                    signals_rejected += 1
                    continue

                risk_amt = Decimal(str(quantity)) * (fill_price - validation.stop_loss_price)
                open_trade = SimulatedTrade(
                    trade_id=request.trade_id,
                    symbol=symbol,
                    direction="BUY",
                    entry_price=fill_price,
                    stop_loss_price=validation.stop_loss_price,
                    take_profit_price=validation.take_profit_price,
                    quantity=quantity,
                    risk_amount=risk_amt,
                    entry_bar_index=i + 1,
                    entry_time=fill_bar.timestamp,
                )

        # Close any trade still open at end of data
        if open_trade is not None:
            last_bar = bars[-1]
            open_trade.exit_price = last_bar.close
            open_trade.exit_time = last_bar.timestamp
            open_trade.exit_reason = "END_OF_DATA"
            open_trade.pnl = (last_bar.close - open_trade.entry_price) * open_trade.quantity
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

    def _check_exit(
        self, trade: SimulatedTrade, bar: PriceBar, bar_index: int
    ) -> SimulatedTrade:
        """
        Check whether stop or take-profit is hit on `bar`.

        Bar's low ≤ stop → stopped out at stop price.
        Bar's high ≥ take-profit → target hit at take-profit price.
        If both hit on the same bar, stop wins (conservative assumption).
        """
        if bar.low <= trade.stop_loss_price:
            trade.exit_price = trade.stop_loss_price
            trade.exit_reason = "STOP_LOSS"
        elif bar.high >= trade.take_profit_price:
            trade.exit_price = trade.take_profit_price
            trade.exit_reason = "TAKE_PROFIT"

        if trade.exit_price is not None:
            trade.exit_time = bar.timestamp
            trade.pnl = (trade.exit_price - trade.entry_price) * trade.quantity

        return trade


# ---------------------------------------------------------------------------
# Metrics computation (15.2)
# ---------------------------------------------------------------------------


def _compute_metrics(
    trades: list[SimulatedTrade],
    initial_balance: Decimal,
    bars_tested: int,
    signals_generated: int,
    signals_rejected: int,
) -> BacktestMetrics:
    if not trades:
        return BacktestMetrics(
            trade_count=0, win_count=0, loss_count=0, win_rate_pct=0.0,
            total_return=Decimal("0"), total_return_pct=0.0,
            avg_trade_pnl=Decimal("0"), avg_winner=Decimal("0"),
            avg_loser=Decimal("0"), largest_winner=Decimal("0"),
            largest_loser=Decimal("0"), max_drawdown_pct=0.0,
            sharpe_ratio=0.0, bars_tested=bars_tested,
            signals_generated=signals_generated, signals_rejected=signals_rejected,
        )

    pnls = [t.pnl or Decimal("0") for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    total_return = sum(pnls, Decimal("0"))

    # Equity curve for drawdown and Sharpe
    equity = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    daily_returns: list[float] = []

    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        daily_returns.append(float(p) / float(initial_balance))

    max_dd_pct = float(max_dd / initial_balance) * 100 if initial_balance else 0.0

    # Annualised Sharpe (assumes ~252 trading days; backtest bars are daily)
    sharpe = 0.0
    if len(daily_returns) > 1:
        mean_r = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std_r = math.sqrt(variance)
        if std_r > 0:
            sharpe = (mean_r / std_r) * math.sqrt(252)

    return BacktestMetrics(
        trade_count=len(trades),
        win_count=len(winners),
        loss_count=len(losers),
        win_rate_pct=len(winners) / len(trades) * 100,
        total_return=total_return,
        total_return_pct=float(total_return / initial_balance) * 100,
        avg_trade_pnl=total_return / len(trades),
        avg_winner=sum(winners, Decimal("0")) / len(winners) if winners else Decimal("0"),
        avg_loser=sum(losers, Decimal("0")) / len(losers) if losers else Decimal("0"),
        largest_winner=max(winners, default=Decimal("0")),
        largest_loser=min(losers, default=Decimal("0")),
        max_drawdown_pct=max_dd_pct,
        sharpe_ratio=sharpe,
        bars_tested=bars_tested,
        signals_generated=signals_generated,
        signals_rejected=signals_rejected,
    )
