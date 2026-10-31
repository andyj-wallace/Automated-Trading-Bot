"""
Operator controls for automated trading.

GET  /api/v1/trading/status   — Pause state, market/window state, broker, lock, scheduler
POST /api/v1/trading/pause    — Block automated entries (exits keep working)
POST /api/v1/trading/resume   — Allow automated entries again
POST /api/v1/trading/flatten  — Pause, then close every OPEN trade ({"confirm": "FLATTEN"})
"""

from decimal import Decimal

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import FlattenIn, PauseIn, err, ok
from app.core.engine import TradingEngine
from app.db.repositories.trade_repo import TradeRepo
from app.dependencies import get_db, get_engine
from app.monitoring.logger import system_logger

router = APIRouter(prefix="/trading", tags=["trading"])


@router.get("/status")
async def trading_status(
    engine: TradingEngine = Depends(get_engine),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    open_trades = await TradeRepo(db).get_open_trades()
    last_cycle = engine.scheduler.last_cycle_at
    equity = await engine.get_equity()
    growth = engine.growth
    limit = engine.settings.trading_capital_limit
    growth_mode = {
        "enabled": growth.enabled,
        "active": growth.is_active(equity) if equity > 0 else None,
        "equity": str(equity) if equity > 0 else None,
        "exit_equity": str(growth.exit_equity),
        "progress_pct": str((equity / growth.exit_equity * 100).quantize(Decimal("0.1"))) if equity > 0 else None,
        "max_open_positions": growth.max_open_positions,
        "universe": sorted(growth.universe),
        "commission_plan": engine.settings.commission_plan,
        "account_type": engine.settings.account_type,
        "capital_limit": str(limit) if limit is not None else None,
    }
    return JSONResponse(
        content=ok(
            {
                "paused": await engine.trading_state.is_paused(),
                "market_open": engine.market_open(),
                "in_eval_window": engine.in_eval_window(),
                "broker_connected": engine.broker.is_connected(),
                "broker": engine.broker.connection_info().model_dump(mode="json"),
                "engine_lock_held": engine.is_leader,
                "scheduler_last_cycle_at": last_cycle.isoformat() if last_cycle else None,
                "open_trade_count": len(open_trades),
                "circuit_breaker_halted": engine.circuit_breaker.is_halted(),
                "growth_mode": growth_mode,
            }
        )
    )


@router.post("/pause")
async def pause_trading(
    body: PauseIn = Body(default_factory=PauseIn),
    engine: TradingEngine = Depends(get_engine),
) -> JSONResponse:
    await engine.trading_state.pause(body.reason)
    return JSONResponse(content=ok({"paused": True}))


@router.post("/resume")
async def resume_trading(engine: TradingEngine = Depends(get_engine)) -> JSONResponse:
    await engine.trading_state.resume()
    return JSONResponse(content=ok({"paused": False}))


@router.post("/flatten")
async def flatten(
    body: FlattenIn,
    engine: TradingEngine = Depends(get_engine),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    if body.confirm != "FLATTEN":
        return JSONResponse(
            status_code=422,
            content=err("CONFIRMATION_REQUIRED", 'Body must be {"confirm": "FLATTEN"}'),
        )
    await engine.trading_state.pause("flatten")
    trades = await TradeRepo(db).get_open_trades()
    results = []
    for trade in trades:
        try:
            outcome = (await engine.order_manager.close_position(trade.id, "MANUAL")).value
        except Exception as exc:
            system_logger.error(
                "Flatten: close failed", extra={"trade_id": str(trade.id), "error": str(exc)}
            )
            outcome = "FAILED"
        results.append({"trade_id": str(trade.id), "symbol": trade.symbol, "outcome": outcome})
    return JSONResponse(content=ok({"paused": True, "results": results}))
