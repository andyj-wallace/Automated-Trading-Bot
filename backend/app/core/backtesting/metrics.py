"""Summary metrics computed from a backtest's simulated trades."""

from __future__ import annotations

import math
from decimal import Decimal

from app.core.backtesting.results import BacktestMetrics, SimulatedTrade

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
            ending_balance=initial_balance,
        )

    pnls = [t.pnl or Decimal("0") for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    total_return = sum(pnls, Decimal("0"))
    total_commission = sum((t.commission_paid for t in trades), Decimal("0"))

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
        total_commission_paid=total_commission,
        ending_balance=initial_balance + total_return,
    )
