"""
Circuit breaker admin endpoints.

GET  /api/v1/risk/circuit-breaker                          — Current daily/per-strategy status
POST /api/v1/risk/circuit-breaker/reset                     — Lift today's daily halt early
POST /api/v1/risk/circuit-breaker/strategies/{id}/kill      — Manually disable a strategy
POST /api/v1/risk/circuit-breaker/strategies/{id}/reset     — Re-enable a strategy

CircuitBreaker holds in-memory state. The engine re-computes the daily halt and loss
streaks from the trades table at startup, and a killed strategy is also disabled in
the database, so a restart never lifts a halt or a kill. An admin reset therefore
does not survive a restart (the safe direction).
"""

import uuid

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import ok
from app.core.risk.circuit_breaker import CircuitBreaker
from app.db.repositories.strategy_repo import StrategyRepo
from app.dependencies import get_circuit_breaker, get_db
from app.monitoring.logger import system_logger

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/circuit-breaker")
async def get_circuit_breaker_status(
    cb: CircuitBreaker = Depends(get_circuit_breaker),
) -> JSONResponse:
    return JSONResponse(
        content=ok(
            {
                "daily_halted": cb.is_halted(),
                "daily_realized_pnl": str(cb.daily_realized_pnl),
                "day_starting_balance": str(cb.day_starting_balance),
                "killed_strategy_ids": [str(sid) for sid in cb.killed_strategy_ids()],
                "trading_date": cb.trading_date.isoformat() if cb.trading_date else None,
                "daily_loss_limit_pct": str(cb.daily_loss_limit_pct),
                "halt_threshold_amount": str(cb.halt_threshold_amount),
                "consecutive_losses": {
                    str(sid): n for sid, n in cb.strategy_streaks().items()
                },
            }
        )
    )


@router.post("/circuit-breaker/reset")
async def reset_daily_halt(
    cb: CircuitBreaker = Depends(get_circuit_breaker),
) -> JSONResponse:
    """
    Admin override: lift today's daily halt without waiting for day rollover.

    In memory only: a restart re-computes the halt from today's closed trades, so
    this reset does not survive a restart. That is the safe direction.
    """
    cb.reset_daily_halt()
    system_logger.warning("CircuitBreaker: daily halt manually reset by admin")
    return JSONResponse(content=ok({"daily_halted": cb.is_halted()}))


@router.post("/circuit-breaker/strategies/{strategy_id}/kill")
async def kill_strategy(
    strategy_id: uuid.UUID = Path(..., description="Strategy UUID to disable"),
    cb: CircuitBreaker = Depends(get_circuit_breaker),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Kill a strategy: blocks it in the breaker and disables it in the database."""
    cb.kill_strategy(strategy_id)
    await StrategyRepo(db).set_enabled(strategy_id, False)
    await db.commit()
    system_logger.warning(
        "CircuitBreaker: strategy manually killed by admin",
        extra={"strategy_id": str(strategy_id)},
    )
    return JSONResponse(content=ok({"strategy_id": str(strategy_id), "killed": True}))


@router.post("/circuit-breaker/strategies/{strategy_id}/reset")
async def reset_strategy(
    strategy_id: uuid.UUID = Path(..., description="Strategy UUID to re-enable"),
    cb: CircuitBreaker = Depends(get_circuit_breaker),
) -> JSONResponse:
    """Clear the strategy's loss streak and kill flag. Re-enable it via PATCH /strategies."""
    cb.reset_strategy(strategy_id)
    system_logger.warning(
        "CircuitBreaker: strategy manually re-enabled by admin",
        extra={"strategy_id": str(strategy_id)},
    )
    return JSONResponse(content=ok({"strategy_id": str(strategy_id), "killed": False}))
