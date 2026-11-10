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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any as Any
from typing import Literal as Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel as BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.backtesting_helpers import (
    _bad_request,
    _build_costs,
    _load_extra_bars,
    _load_slot_bars,
    _no_historical_data,
    _require_daily_only,
    _run_portfolio_job_locked,
    _to_price_bars,
)
from app.api.v1.backtesting_models import (
    _RANGE_DAYS,
    BacktestRunRequest,
    PortfolioBacktestRunRequest,
    PortfolioSlotRequest,
    _Job,
)
from app.api.v1.backtesting_serialise import (
    _serialise_metrics as _serialise_metrics,
)
from app.api.v1.backtesting_serialise import (
    _serialise_portfolio_result,
    _serialise_result,
)
from app.api.v1.schemas import err, ok
from app.brokers.base import PriceBar
from app.config import get_settings as get_settings
from app.core.backtesting.engine import BacktestingEngine
from app.core.backtesting.engine import BacktestMetrics as BacktestMetrics
from app.core.backtesting.engine import BacktestResult as BacktestResult
from app.core.backtesting.portfolio_engine import (
    PortfolioBacktestEngine as PortfolioBacktestEngine,
)
from app.core.backtesting.portfolio_engine import (
    PortfolioBacktestResult as PortfolioBacktestResult,
)
from app.core.backtesting.portfolio_engine import PortfolioSlot as PortfolioSlot
from app.core.risk.costs import PLANS as PLANS
from app.core.risk.costs import CostModel as CostModel
from app.core.risk.manager import RiskManager
from app.core.strategy_engine.registry import registry
from app.db.repositories.ohlcv_repo import OHLCVRepo
from app.dependencies import get_db

router = APIRouter(prefix="/backtesting", tags=["backtesting"])

MAX_JOBS = 20


# ---------------------------------------------------------------------------
# In-memory job store
# ---------------------------------------------------------------------------


_jobs: dict[uuid.UUID, _Job] = {}


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

    try:
        strategy = registry.build(body.strategy_type, body.strategy_config)
        costs = _build_costs(body)
        _require_daily_only([strategy])
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return _bad_request("INVALID_CONFIG", str(exc))

    # Check historical data exists
    end = datetime.now(UTC)
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

    bars = _to_price_bars(db_bars)
    extra_bars, missing_extra = await _load_extra_bars(ohlcv_repo, strategy, start, end)
    if missing_extra:
        return _bad_request(
            "NO_HISTORICAL_DATA",
            f"No historical OHLCV bars found for {missing_extra}, which {body.strategy_type} needs. "
            "Fetch historical data first via HistoricalDataFetcher.",
        )

    # Evict oldest job if at capacity
    if len(_jobs) >= MAX_JOBS:
        oldest_id = min(_jobs, key=lambda k: _jobs[k].created_at)
        del _jobs[oldest_id]

    job_id = uuid.uuid4()
    job = _Job(job_id=job_id, request=body)
    job.costs, job.extra_bars = costs, extra_bars
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
    end = datetime.now(UTC)
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

    try:
        strategies = [registry.build(s.strategy_type, s.config) for s in body.slots]
        costs = _build_costs(body)
        _require_daily_only(strategies)
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return _bad_request("INVALID_CONFIG", str(exc))

    # Fetch and validate OHLCV data for all symbols
    bars_by_slot, extras_by_slot, missing = await _load_slot_bars(
        ohlcv_repo, body.slots, strategies, start, end
    )
    if missing:
        return _no_historical_data(missing)

    # Evict oldest job if at capacity
    if len(_jobs) >= MAX_JOBS:
        oldest_id = min(_jobs, key=lambda k: _jobs[k].created_at)
        del _jobs[oldest_id]

    job_id = uuid.uuid4()
    job = _Job(job_id=job_id, request=body)
    job.costs, job.extra_bars = costs, extras_by_slot
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


_backtest_slot = asyncio.Semaphore(1)


async def _run_job(job: _Job, bars) -> None:
    async with _backtest_slot:
        await _run_job_locked(job, bars)


async def _run_job_locked(job: _Job, bars) -> None:
    job.status = "running"
    job.started_at = datetime.now(UTC)

    try:
        strategy = registry.build(
            job.request.strategy_type, job.request.strategy_config
        )
        risk_manager, engine_kwargs = job.costs or (RiskManager(), {})
        engine = BacktestingEngine(strategy, risk_manager, **engine_kwargs)

        result = await asyncio.to_thread(
            asyncio.run,
            engine.run(
                bars=bars,
                symbol=job.request.symbol.upper(),
                account_balance=Decimal(job.request.account_balance),
                strategy_type=job.request.strategy_type,
                strategy_config=job.request.strategy_config,
                extra_bars=job.extra_bars,
            ),
        )
        job.result = result
        job.status = "done"
    except Exception as exc:
        job.error = str(exc)
        job.status = "error"
    finally:
        job.finished_at = datetime.now(UTC)


async def _run_portfolio_job(
    job: _Job,
    slot_requests: list[PortfolioSlotRequest],
    bars_by_slot: list[list[PriceBar]],
    account_balance_str: str,
) -> None:
    async with _backtest_slot:
        await _run_portfolio_job_locked(job, slot_requests, bars_by_slot, account_balance_str)
