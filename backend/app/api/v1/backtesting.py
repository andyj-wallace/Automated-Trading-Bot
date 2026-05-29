"""
Backtesting endpoints.

POST /api/v1/backtesting/run            — queue a single-strategy backtest job
POST /api/v1/backtesting/run-portfolio  — queue a multi-strategy portfolio backtest job
GET  /api/v1/backtesting/{id}           — poll job status / retrieve result

Jobs run as background asyncio tasks. Results are held in process memory
(sufficient for a single-user bot; no persistence needed). The job store
is bounded to MAX_JOBS entries — oldest are evicted when full.

Status values: "pending" | "running" | "done" | "error"
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import err, ok
from app.brokers.base import PriceBar
from app.core.backtesting.engine import BacktestMetrics, BacktestResult, BacktestingEngine
from app.core.backtesting.portfolio_engine import (
    PortfolioBacktestResult,
    PortfolioBacktestEngine,
    PortfolioSlot,
)
from app.core.risk.manager import RiskManager
from app.core.strategy_engine.registry import registry
from app.db.repositories.ohlcv_repo import OHLCVRepo
from app.dependencies import get_db

router = APIRouter(prefix="/backtesting", tags=["backtesting"])

MAX_JOBS = 20


# ---------------------------------------------------------------------------
# In-memory job store
# ---------------------------------------------------------------------------


class _Job:
    def __init__(self, job_id: uuid.UUID, request: Any) -> None:
        self.job_id = job_id
        self.request = request
        self.status: str = "pending"
        self.error: str | None = None
        self.result: BacktestResult | None = None
        self.portfolio_result: PortfolioBacktestResult | None = None
        self.created_at: datetime = datetime.now(timezone.utc)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None


_jobs: dict[uuid.UUID, _Job] = {}


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class BacktestRunRequest(BaseModel):
    symbol: str
    strategy_type: str
    strategy_config: dict[str, Any] = {}
    account_balance: str = "100000"


class PortfolioSlotRequest(BaseModel):
    symbol: str
    strategy_type: str
    config: dict[str, Any] = {}


_RANGE_DAYS: dict[str, int] = {
    "1y": 365,
    "2y": 730,
    "5y": 365 * 5,
}


class PortfolioBacktestRunRequest(BaseModel):
    slots: list[PortfolioSlotRequest]
    account_balance: str = "100000"
    range: str = "5y"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/run", status_code=202)
async def run_backtest(
    body: BacktestRunRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Queue a backtest and return immediately with a job_id.

    Validates that the strategy type is registered and that historical data
    exists for the symbol before accepting the job.
    """
    ticker = body.symbol.upper()

    # Validate strategy type
    if not registry.is_registered(body.strategy_type):
        return JSONResponse(
            status_code=422,
            content=err(
                "UNKNOWN_STRATEGY",
                f"Strategy type {body.strategy_type!r} is not registered. "
                f"Registered: {registry.registered_types()}",
            ),
        )

    # Check historical data exists
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * 5)
    ohlcv_repo = OHLCVRepo(db)
    db_bars = await ohlcv_repo.get_bars(ticker, start=start, end=end)
    if not db_bars:
        return JSONResponse(
            status_code=422,
            content=err(
                "NO_HISTORICAL_DATA",
                f"No historical OHLCV bars found for {ticker}. "
                "Fetch historical data first via HistoricalDataFetcher.",
            ),
        )

    bars = [
        PriceBar(
            timestamp=bar.time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            bar_size=bar.bar_size,
        )
        for bar in db_bars
    ]

    # Evict oldest job if at capacity
    if len(_jobs) >= MAX_JOBS:
        oldest_id = min(_jobs, key=lambda k: _jobs[k].created_at)
        del _jobs[oldest_id]

    job_id = uuid.uuid4()
    job = _Job(job_id=job_id, request=body)
    _jobs[job_id] = job

    asyncio.create_task(_run_job(job, bars), name=f"backtest-{job_id}")

    return JSONResponse(
        status_code=202,
        content=ok({
            "job_id": str(job_id),
            "status": "pending",
            "symbol": ticker,
            "strategy_type": body.strategy_type,
        }),
    )


@router.post("/run-portfolio", status_code=202)
async def run_portfolio_backtest(
    body: PortfolioBacktestRunRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Queue a portfolio backtest across multiple strategy/symbol pairs.

    Validates that every strategy type is registered and every symbol has
    historical OHLCV data before accepting the job. Returns 202 immediately
    with a job_id; poll GET /api/v1/backtesting/{id} for status and results.
    """
    if not body.slots:
        return JSONResponse(
            status_code=422,
            content=err("EMPTY_SLOTS", "Portfolio backtest requires at least one slot."),
        )

    lookback_days = _RANGE_DAYS.get(body.range, _RANGE_DAYS["5y"])
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    ohlcv_repo = OHLCVRepo(db)

    # Validate all strategy types up-front
    unknown = [
        s.strategy_type
        for s in body.slots
        if not registry.is_registered(s.strategy_type)
    ]
    if unknown:
        return JSONResponse(
            status_code=422,
            content=err(
                "UNKNOWN_STRATEGY",
                f"Unknown strategy type(s): {unknown}. "
                f"Registered: {registry.registered_types()}",
            ),
        )

    # Fetch and validate OHLCV data for all symbols
    bars_by_slot: list[list[PriceBar]] = []
    missing: list[str] = []

    for slot_req in body.slots:
        ticker = slot_req.symbol.upper()
        db_bars = await ohlcv_repo.get_bars(ticker, start=start, end=end)
        if not db_bars:
            missing.append(ticker)
            continue
        bars_by_slot.append([
            PriceBar(
                timestamp=b.time,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                bar_size=b.bar_size,
            )
            for b in db_bars
        ])

    if missing:
        return JSONResponse(
            status_code=422,
            content=err(
                "NO_HISTORICAL_DATA",
                f"No historical OHLCV bars found for: {missing}. "
                "Fetch historical data first via HistoricalDataFetcher.",
            ),
        )

    # Evict oldest job if at capacity
    if len(_jobs) >= MAX_JOBS:
        oldest_id = min(_jobs, key=lambda k: _jobs[k].created_at)
        del _jobs[oldest_id]

    job_id = uuid.uuid4()
    job = _Job(job_id=job_id, request=body)
    _jobs[job_id] = job

    asyncio.create_task(
        _run_portfolio_job(job, body.slots, bars_by_slot, body.account_balance),
        name=f"portfolio-backtest-{job_id}",
    )

    return JSONResponse(
        status_code=202,
        content=ok({
            "job_id": str(job_id),
            "status": "pending",
            "slot_count": len(body.slots),
        }),
    )


@router.get("/{job_id}")
async def get_backtest_result(job_id: uuid.UUID) -> JSONResponse:
    """
    Poll the status of a backtest job.

    Returns the full result once status == "done".
    """
    job = _jobs.get(job_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content=err("NOT_FOUND", f"Backtest job {job_id} not found"),
        )

    is_portfolio = isinstance(job.request, PortfolioBacktestRunRequest)

    payload: dict = {
        "job_id": str(job.job_id),
        "status": job.status,
        "job_type": "portfolio" if is_portfolio else "single",
        "created_at": job.created_at.isoformat(),
    }
    if not is_portfolio:
        payload["symbol"] = job.request.symbol.upper()
        payload["strategy_type"] = job.request.strategy_type
    else:
        payload["slot_count"] = len(job.request.slots)

    if job.started_at:
        payload["started_at"] = job.started_at.isoformat()
    if job.finished_at:
        payload["finished_at"] = job.finished_at.isoformat()
    if job.error:
        payload["error"] = job.error
    if job.result:
        payload["result"] = _serialise_result(job.result)
    if job.portfolio_result:
        payload["result"] = _serialise_portfolio_result(job.portfolio_result)

    return JSONResponse(content=ok(payload))


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------


async def _run_job(job: _Job, bars) -> None:
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)

    try:
        strategy = registry.build(
            job.request.strategy_type, job.request.strategy_config
        )
        risk_manager = RiskManager()
        engine = BacktestingEngine(strategy, risk_manager)

        result = await engine.run(
            bars=bars,
            symbol=job.request.symbol.upper(),
            account_balance=Decimal(job.request.account_balance),
            strategy_type=job.request.strategy_type,
            strategy_config=job.request.strategy_config,
        )
        job.result = result
        job.status = "done"
    except Exception as exc:
        job.error = str(exc)
        job.status = "error"
    finally:
        job.finished_at = datetime.now(timezone.utc)


async def _run_portfolio_job(
    job: _Job,
    slot_requests: list[PortfolioSlotRequest],
    bars_by_slot: list[list[PriceBar]],
    account_balance_str: str,
) -> None:
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)

    try:
        slots = [
            PortfolioSlot(
                strategy=registry.build(sr.strategy_type, sr.config),
                symbol=sr.symbol.upper(),
                bars=bars,
                strategy_type=sr.strategy_type,
                strategy_config=sr.config,
            )
            for sr, bars in zip(slot_requests, bars_by_slot)
        ]
        engine = PortfolioBacktestEngine(RiskManager())
        result = await engine.run(slots, account_balance=Decimal(account_balance_str))
        job.portfolio_result = result
        job.status = "done"
    except Exception as exc:
        job.error = str(exc)
        job.status = "error"
    finally:
        job.finished_at = datetime.now(timezone.utc)


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


def _serialise_metrics(m: "BacktestMetrics") -> dict:
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
