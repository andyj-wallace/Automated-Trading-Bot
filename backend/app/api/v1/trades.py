"""
Trade read endpoints.

GET  /api/v1/trades              — List trades with optional filters
GET  /api/v1/trades/active       — Trades in PENDING, SUBMITTED, OPEN or CLOSING
GET  /api/v1/trades/{trade_id}   — Get a single trade by UUID
POST /api/v1/trades              — Manual order through the full risk pipeline
POST /api/v1/trades/{id}/close   — Manual close (exit_reason MANUAL)
"""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import ManualTradeIn, TradeOut, err, ok
from app.core.engine import TradingEngine
from app.core.execution.order_manager import CloseOutcome
from app.core.risk.calculator import RiskCalculator
from app.core.risk.growth import effective_equity
from app.core.risk.manager import RiskRejectionError, TradeRequest
from app.db.models.trade import TradeStatus
from app.db.repositories.trade_repo import TradeRepo
from app.dependencies import get_db, get_engine

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


@router.get("/active")
async def active_trades(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    trades = await TradeRepo(db).get_active_trades()
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


_CLOSE_STATUS = {
    CloseOutcome.CLOSED: 200,
    CloseOutcome.PENDING_EXIT: 202,
    CloseOutcome.NOT_OPEN: 409,
    CloseOutcome.MARKET_CLOSED: 409,
    CloseOutcome.FAILED: 502,
}


@router.post("/{trade_id}/close")
async def close_trade(
    trade_id: uuid.UUID,
    engine: TradingEngine = Depends(get_engine),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    outcome = await engine.order_manager.close_position(trade_id, "MANUAL")
    status_code = _CLOSE_STATUS[outcome]
    if status_code >= 400:
        return JSONResponse(
            status_code=status_code,
            content=err(outcome.value, f"Close request for trade {trade_id}: {outcome.value}"),
        )
    trade = await TradeRepo(db).get_by_id(trade_id)
    return JSONResponse(
        status_code=status_code,
        content=ok(jsonable_encoder(TradeOut.model_validate(trade))),
    )


@router.post("", status_code=201)
async def create_manual_trade(
    body: ManualTradeIn,
    engine: TradingEngine = Depends(get_engine),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    symbol = body.symbol.upper().strip()
    if not engine.market_open():
        return JSONResponse(
            status_code=409, content=err("MARKET_CLOSED", "Market is closed")
        )
    price = await engine.fresh_price(symbol)
    if price is None:
        return JSONResponse(
            status_code=409,
            content=err("NO_FRESH_PRICE", f"No fresh price for {symbol}"),
        )
    if not engine.broker.is_connected():
        return JSONResponse(
            status_code=409, content=err("BROKER_DISCONNECTED", "Broker is not connected")
        )
    try:
        balance = effective_equity(
            await engine.broker.get_account_summary(), engine.settings.trading_capital_limit
        )
    except Exception as exc:
        return JSONResponse(status_code=409, content=err("BROKER_UNAVAILABLE", str(exc)))

    quantity = body.quantity
    if quantity is None:
        try:
            quantity = RiskCalculator().max_quantity(balance, price, body.stop_loss_price)
        except ValueError as exc:
            return JSONResponse(
                status_code=422, content=err("INVALID_STOP_LOSS", str(exc))
            )
        if quantity < 1:
            return JSONResponse(
                status_code=422,
                content=err("RISK_LIMIT_EXCEEDED", "Stop is too wide for a 1-share position"),
            )

    request = TradeRequest(
        trade_id=uuid.uuid4(),
        symbol=symbol,
        direction="BUY",
        quantity=Decimal(quantity),
        entry_price=price,
        stop_loss_price=body.stop_loss_price,
        account_balance=balance,
        strategy_id=None,
        take_profit_price=body.take_profit_price,
    )
    try:
        await engine.order_manager.submit_order(request)
    except RiskRejectionError as exc:
        code = exc.reason.split(":", 1)[0]
        return JSONResponse(status_code=422, content=err(code, exc.reason))
    except Exception as exc:
        return JSONResponse(status_code=502, content=err("ORDER_FAILED", str(exc)))

    trade = await TradeRepo(db).get_by_id(request.trade_id)
    return JSONResponse(
        status_code=201, content=ok(jsonable_encoder(TradeOut.model_validate(trade)))
    )
