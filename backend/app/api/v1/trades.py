"""
Trade read endpoints.

GET /api/v1/trades           — List trades with optional filters
GET /api/v1/trades/{trade_id} — Get a single trade by UUID
"""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import TradeOut, err, ok
from app.db.models.trade import TradeStatus
from app.db.repositories.trade_repo import TradeRepo
from app.dependencies import get_db

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("")
async def list_trades(
    symbol: str | None = Query(None, description="Filter by ticker symbol"),
    status: TradeStatus | None = Query(None, description="Filter by trade status"),
    strategy_id: uuid.UUID | None = Query(None, description="Filter by strategy UUID"),
    limit: int = Query(100, ge=1, le=500, description="Max results to return"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    repo = TradeRepo(db)
    trades = await repo.list(
        symbol=symbol,
        strategy_id=strategy_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(
        content=ok(jsonable_encoder([TradeOut.model_validate(t) for t in trades]))
    )


@router.get("/{trade_id}")
async def get_trade(
    trade_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    repo = TradeRepo(db)
    trade = await repo.get_by_id(trade_id)
    if trade is None:
        return JSONResponse(
            status_code=404,
            content=err("NOT_FOUND", f"Trade {trade_id} not found"),
        )
    return JSONResponse(content=ok(jsonable_encoder(TradeOut.model_validate(trade))))
