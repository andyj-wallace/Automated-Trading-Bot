"""
Strategy signal read endpoint.

GET /api/v1/signals?symbol=&strategy_id=&limit= — Most recent signals first
"""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import SignalOut, ok
from app.db.repositories.signal_repo import SignalRepo
from app.dependencies import get_db

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("")
async def list_signals(
    symbol: str | None = Query(None, description="Filter by ticker symbol"),
    strategy_id: uuid.UUID | None = Query(None, description="Filter by strategy UUID"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    signals = await SignalRepo(db).list(symbol=symbol, strategy_id=strategy_id, limit=limit)
    return JSONResponse(
        content=ok(jsonable_encoder([SignalOut.model_validate(s) for s in signals]))
    )
