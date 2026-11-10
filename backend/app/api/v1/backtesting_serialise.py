"""Serialisers that turn backtest results into JSON-ready dicts."""

from __future__ import annotations

from app.core.backtesting.engine import BacktestMetrics, BacktestResult
from app.core.backtesting.portfolio_engine import PortfolioBacktestResult

# ---------------------------------------------------------------------------
# Result serialisation
# ---------------------------------------------------------------------------


def _serialise_result(result: BacktestResult) -> dict:
    m = result.metrics
    trades_out = [
        {
            "trade_id": str(t.trade_id),
            "entry_price": str(t.entry_price),
            "exit_price": str(t.exit_price) if t.exit_price else None,
            "stop_loss_price": str(t.stop_loss_price),
            "take_profit_price": str(t.take_profit_price),
            "quantity": t.quantity,
            "pnl": str(t.pnl) if t.pnl is not None else None,
            "exit_reason": t.exit_reason,
            "entry_time": t.entry_time.isoformat(),
            "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        }
        for t in result.trades
    ]

    return {
        "symbol": result.symbol,
        "strategy_type": result.strategy_type,
        "account_balance": str(result.account_balance),
        "start_time": result.start_time.isoformat(),
        "end_time": result.end_time.isoformat(),
        "trades": trades_out,
        "metrics": _serialise_metrics(m) if m else None,
    }


def _serialise_metrics(m: BacktestMetrics) -> dict:
    return {
        "trade_count": m.trade_count,
        "win_count": m.win_count,
        "loss_count": m.loss_count,
        "win_rate_pct": round(m.win_rate_pct, 2),
        "total_return": str(m.total_return),
        "total_return_pct": round(m.total_return_pct, 4),
        "avg_trade_pnl": str(m.avg_trade_pnl),
        "avg_winner": str(m.avg_winner),
        "avg_loser": str(m.avg_loser),
        "largest_winner": str(m.largest_winner),
        "largest_loser": str(m.largest_loser),
        "max_drawdown_pct": round(m.max_drawdown_pct, 4),
        "sharpe_ratio": round(m.sharpe_ratio, 4),
        "bars_tested": m.bars_tested,
        "signals_generated": m.signals_generated,
        "signals_rejected": m.signals_rejected,
        "ending_balance": str(m.ending_balance),
        "total_commission_paid": str(m.total_commission_paid),
    }


def _serialise_portfolio_result(result: PortfolioBacktestResult) -> dict:
    return {
        "account_balance": str(result.account_balance),
        "start_time": result.start_time.isoformat(),
        "end_time": result.end_time.isoformat(),
        "per_strategy": [_serialise_result(ps) for ps in result.per_strategy],
        "combined_equity_curve": [
            [ts.isoformat(), str(pnl)]
            for ts, pnl in result.combined_equity_curve
        ],
        "combined_metrics": _serialise_metrics(result.combined_metrics),
    }
