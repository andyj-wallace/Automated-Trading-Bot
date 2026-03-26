"""
Strategy management endpoints.

GET   /api/v1/strategies           — List all registered strategies
PATCH /api/v1/strategies/{id}      — Toggle is_enabled and/or update JSONB config
                                     (including config.symbols assignment)
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import StrategyOut, StrategyPatchIn, err, ok
from app.db.repositories.strategy_repo import StrategyRepo
from app.dependencies import get_db

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("")
async def list_strategies(
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    repo = StrategyRepo(db)
    strategies = await repo.get_all()
    return JSONResponse(
        content=ok(jsonable_encoder([StrategyOut.model_validate(s) for s in strategies]))
    )


@router.patch("/{strategy_id}")
async def patch_strategy(
    strategy_id: uuid.UUID,
    body: StrategyPatchIn,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    if body.is_enabled is None and body.config is None:
        return JSONResponse(
            status_code=422,
            content=err("NO_UPDATE", "Provide at least one of: is_enabled, config"),
        )

    repo = StrategyRepo(db)
    strategy = await repo.patch(
        strategy_id,
        is_enabled=body.is_enabled,
        config=body.config,
    )
    if strategy is None:
        return JSONResponse(
            status_code=404,
            content=err("NOT_FOUND", f"Strategy {strategy_id} not found"),
        )

    await db.commit()
    return JSONResponse(content=ok(jsonable_encoder(StrategyOut.model_validate(strategy))))
