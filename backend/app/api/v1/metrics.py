"""
Performance metrics endpoints.

GET /api/v1/metrics/performance  — KPI summary from closed trades
                                   (win rate, total P&L, trade count, avg duration)
                                   Query params: range = 7d | 30d | 90d | all
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import ok
from app.db.models.trade import Trade, TradeStatus
from app.dependencies import get_db

router = APIRouter(prefix="/metrics", tags=["metrics"])

_RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90}


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
