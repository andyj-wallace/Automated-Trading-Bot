"""
Circuit breaker admin endpoints.

GET  /api/v1/risk/circuit-breaker                          — Current daily/per-strategy status
POST /api/v1/risk/circuit-breaker/reset                     — Lift today's daily halt early
POST /api/v1/risk/circuit-breaker/strategies/{id}/kill      — Manually disable a strategy
POST /api/v1/risk/circuit-breaker/strategies/{id}/reset     — Re-enable a strategy

This is the only way to lift a tripped halt or re-enable a killed strategy
short of restarting the process — CircuitBreaker holds in-memory state only.
"""

import uuid

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse

from app.api.v1.schemas import ok
from app.core.risk.circuit_breaker import CircuitBreaker
from app.dependencies import get_circuit_breaker
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
            }
        )
    )


@router.post("/circuit-breaker/reset")
async def reset_daily_halt(
    cb: CircuitBreaker = Depends(get_circuit_breaker),
) -> JSONResponse:
    """Admin override: lift today's daily halt without waiting for day rollover."""
    cb.reset_daily_halt()
    system_logger.warning("CircuitBreaker: daily halt manually reset by admin")
    return JSONResponse(content=ok({"daily_halted": cb.is_halted()}))


@router.post("/circuit-breaker/strategies/{strategy_id}/kill")
async def kill_strategy(
    strategy_id: uuid.UUID = Path(..., description="Strategy UUID to disable"),
    cb: CircuitBreaker = Depends(get_circuit_breaker),
) -> JSONResponse:
    cb.kill_strategy(strategy_id)
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
    cb.reset_strategy(strategy_id)
    system_logger.warning(
        "CircuitBreaker: strategy manually re-enabled by admin",
        extra={"strategy_id": str(strategy_id)},
    )
    return JSONResponse(content=ok({"strategy_id": str(strategy_id), "killed": False}))
