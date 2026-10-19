"""
Strategy management endpoints.

GET   /api/v1/strategies           — List all registered strategies
GET   /api/v1/strategies/types     — Registered strategy types with config schemas
POST  /api/v1/strategies           — Create a strategy (config validated)
PATCH /api/v1/strategies/{id}      — Toggle is_enabled and/or update JSONB config
                                     (including config.symbols assignment)
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import StrategyCreateIn, StrategyOut, StrategyPatchIn, err, ok
from app.core.strategy_engine.builtin import load_builtin_strategies
from app.core.strategy_engine.registry import registry
from app.core.strategy_engine.seed import config_error, normalize_config
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


@router.get("/types")
async def list_strategy_types() -> JSONResponse:
    load_builtin_strategies()
    return JSONResponse(
        content=ok(
            [
                {"type": name, "config_schema": registry.get_class(name).get_config_schema()}
                for name in registry.registered_types()
            ]
        )
    )


@router.post("", status_code=201)
async def create_strategy(
    body: StrategyCreateIn,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    load_builtin_strategies()
    config = normalize_config(body.config)
    error = config_error(registry, body.type, config)
    if error:
        return JSONResponse(status_code=422, content=err("INVALID_STRATEGY_CONFIG", error))
    strategy = await StrategyRepo(db).create(
        name=body.name, type=body.type, config=config, is_enabled=body.is_enabled
    )
    await db.commit()
    return JSONResponse(
        status_code=201, content=ok(jsonable_encoder(StrategyOut.model_validate(strategy)))
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
    config = None
    if body.config is not None:
        existing = await repo.get_by_id(strategy_id)
        if existing is None:
            return JSONResponse(
                status_code=404,
                content=err("NOT_FOUND", f"Strategy {strategy_id} not found"),
            )
        load_builtin_strategies()
        config = normalize_config(body.config)
        error = config_error(registry, existing.type, config)
        if error:
            return JSONResponse(status_code=422, content=err("INVALID_STRATEGY_CONFIG", error))
    strategy = await repo.patch(
        strategy_id,
        is_enabled=body.is_enabled,
        config=config,
    )
    if strategy is None:
        return JSONResponse(
            status_code=404,
            content=err("NOT_FOUND", f"Strategy {strategy_id} not found"),
        )

    await db.commit()
    return JSONResponse(content=ok(jsonable_encoder(StrategyOut.model_validate(strategy))))
