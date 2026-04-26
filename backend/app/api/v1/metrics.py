"""
Performance metrics endpoints.

GET /api/v1/metrics/performance  — KPI summary from closed trades
                                   (win rate, total P&L, trade count, avg duration)
                                   Query params: range = 7d | 30d | 90d | all

GET /api/v1/metrics/analytics    — Advanced analytics: drawdown time series,
                                   rolling Sharpe ratio, trade day-of-week heatmap
                                   Query params: range = 30d | 90d | all
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import ok
from app.db.models.portfolio import PortfolioSnapshot
from app.db.models.trade import Trade, TradeStatus
from app.dependencies import get_db

router = APIRouter(prefix="/metrics", tags=["metrics"])

_RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}
_ANALYTICS_RANGE_DAYS = {"30d": 30, "90d": 90}


@router.get("/performance")
async def get_performance(
    range: str = Query("30d", description="Time range: 7d | 30d | 90d | all"),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Return trade performance KPIs for the requested time window.

    Computes over CLOSED trades within the range. Returns zeroed values when
    no trades exist (never 404).
    """
    stmt = select(Trade).where(Trade.status == TradeStatus.CLOSED)

    if range in _RANGE_DAYS:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_RANGE_DAYS[range])
        stmt = stmt.where(Trade.closed_at >= cutoff)

    result = await db.execute(stmt.order_by(Trade.closed_at))
    trades = list(result.scalars().all())

    if not trades:
        return JSONResponse(content=ok({
            "range": range,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate_pct": 0.0,
            "total_pnl": "0",
            "avg_pnl": "0",
            "avg_duration_hours": 0.0,
            "largest_winner": "0",
            "largest_loser": "0",
        }))

    pnls = [t.pnl or Decimal("0") for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls, Decimal("0"))

    durations: list[float] = []
    for t in trades:
        if t.closed_at and t.executed_at:
            dur = (t.closed_at - t.executed_at).total_seconds() / 3600
            durations.append(dur)

    avg_duration = sum(durations) / len(durations) if durations else 0.0

    return JSONResponse(content=ok({
        "range": range,
        "trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2),
        "total_pnl": str(total_pnl),
        "avg_pnl": str(total_pnl / len(trades)),
        "avg_duration_hours": round(avg_duration, 2),
        "largest_winner": str(max(wins, default=Decimal("0"))),
        "largest_loser": str(min(losses, default=Decimal("0"))),
    }))


# ---------------------------------------------------------------------------
# Analytics endpoint (18.2) — rolling Sharpe, drawdown series, trade heatmap
# ---------------------------------------------------------------------------

_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@router.get("/analytics")
async def get_analytics(
    range: str = Query("30d", description="Time range: 30d | 90d | all"),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Advanced analytics for the requested time window.

    Returns:
      - drawdown_series: equity curve with running drawdown % from portfolio_snapshots
      - rolling_sharpe_series: 20-snapshot rolling Sharpe from daily equity changes
      - trade_heatmap: P&L aggregated by day-of-week from closed trades
    """
    cutoff: datetime | None = None
    if range in _ANALYTICS_RANGE_DAYS:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_ANALYTICS_RANGE_DAYS[range])

    # --- Portfolio snapshots for drawdown + rolling Sharpe ---
    snap_stmt = select(PortfolioSnapshot).order_by(PortfolioSnapshot.time)
    if cutoff:
        snap_stmt = snap_stmt.where(PortfolioSnapshot.time >= cutoff)
    snap_result = await db.execute(snap_stmt)
    snapshots = list(snap_result.scalars().all())

    drawdown_series = _compute_drawdown_series(snapshots)
    rolling_sharpe_series = _compute_rolling_sharpe(snapshots, window=20)

    # --- Closed trades for day-of-week heatmap ---
    trade_stmt = select(Trade).where(Trade.status == TradeStatus.CLOSED)
    if cutoff:
        trade_stmt = trade_stmt.where(Trade.closed_at >= cutoff)
    trade_result = await db.execute(trade_stmt)
    trades = list(trade_result.scalars().all())

    trade_heatmap = _compute_trade_heatmap(trades)

    return JSONResponse(content=ok({
        "range": range,
        "drawdown_series": drawdown_series,
        "rolling_sharpe_series": rolling_sharpe_series,
        "trade_heatmap": trade_heatmap,
    }))


def _compute_drawdown_series(snapshots: list[PortfolioSnapshot]) -> list[dict]:
    """
    Compute peak-to-trough drawdown % at each snapshot point.

    drawdown_pct is always ≤ 0 (negative means below peak).
    Returns empty list when no snapshots exist.
    """
    if not snapshots:
        return []

    series = []
    peak = float(snapshots[0].total_equity)

    for snap in snapshots:
        equity = float(snap.total_equity)
        if equity > peak:
            peak = equity
        drawdown_pct = ((equity - peak) / peak * 100) if peak > 0 else 0.0

        series.append({
            "time": snap.time.isoformat(),
            "equity": str(snap.total_equity),
            "drawdown_pct": round(drawdown_pct, 4),
        })

    return series


def _compute_rolling_sharpe(
    snapshots: list[PortfolioSnapshot],
    window: int = 20,
) -> list[dict]:
    """
    Compute rolling annualised Sharpe ratio using equity returns across snapshots.

    Uses a sliding window of `window` consecutive snapshots. Annualises assuming
    252 trading days per year. Returns an entry for each snapshot where a full
    window of data is available.
    """
    if len(snapshots) < window + 1:
        return []

    equities = [float(s.total_equity) for s in snapshots]
    returns = [
        (equities[i] - equities[i - 1]) / equities[i - 1]
        for i in range(1, len(equities))
    ]

    series = []
    for i in range(window - 1, len(returns)):
        window_returns = returns[i - window + 1 : i + 1]
        mean_r = sum(window_returns) / window
        variance = sum((r - mean_r) ** 2 for r in window_returns) / window
        std_r = math.sqrt(variance)

        sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 1e-10 else 0.0
        # snapshot index is i+1 because returns are offset by 1
        series.append({
            "time": snapshots[i + 1].time.isoformat(),
            "sharpe": round(sharpe, 4),
        })

    return series


def _compute_trade_heatmap(trades: list[Trade]) -> list[dict]:
    """
    Aggregate closed trade P&L by day of week (0=Monday … 6=Sunday).

    Returns a 7-element list even when no trades exist for a given day,
    so the frontend can always render all 7 columns.
    """
    day_pnl: dict[int, list[Decimal]] = {d: [] for d in range(7)}

    for trade in trades:
        if trade.closed_at and trade.pnl is not None:
            dow = trade.closed_at.weekday()  # 0=Monday
            day_pnl[dow].append(trade.pnl)

    heatmap = []
    for dow in range(7):
        pnls = day_pnl[dow]
        total = sum(pnls, Decimal("0"))
        avg = total / len(pnls) if pnls else Decimal("0")
        heatmap.append({
            "day": dow,
            "day_name": _DAY_NAMES[dow],
            "trade_count": len(pnls),
            "total_pnl": str(total),
            "avg_pnl": str(avg),
        })

    return heatmap
