"""
Symbol management endpoints.

GET    /api/v1/symbols           — List all watchlist symbols
POST   /api/v1/symbols           — Add a symbol (validates ticker with broker first)
DELETE /api/v1/symbols/{ticker}  — Remove a symbol (requires confirm=true if open position)
"""

from fastapi import APIRouter, Body, Depends, Path, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import SymbolCreateIn, SymbolDeleteConfirmIn, SymbolOut, err, ok
from app.brokers.base import BaseBroker
from app.data.historical import HistoricalDataFetcher
from app.db.repositories.symbol_repo import SymbolRepo
from app.dependencies import get_broker, get_db
from app.monitoring.logger import system_logger

router = APIRouter(prefix="/symbols", tags=["symbols"])


@router.get("")
async def list_symbols(
    active_only: bool = Query(False, description="Return only active symbols"),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    repo = SymbolRepo(db)
    symbols = await repo.get_all(active_only=active_only)
    return JSONResponse(
        content=ok(jsonable_encoder([SymbolOut.model_validate(s) for s in symbols]))
    )


@router.post("", status_code=201)
async def add_symbol(
    body: SymbolCreateIn,
    db: AsyncSession = Depends(get_db),
    broker: BaseBroker = Depends(get_broker),
) -> JSONResponse:
    ticker = body.ticker.upper().strip()

    if not ticker or len(ticker) > 10:
        return JSONResponse(
            status_code=422,
            content=err("INVALID_TICKER", f"Ticker must be 1–10 characters, got {ticker!r}"),
        )

    # Validate ticker against the broker before persisting
    try:
        valid = await broker.validate_ticker(ticker)
    except Exception as exc:
        system_logger.warning(
            "Broker ticker validation failed — proceeding with add",
            extra={"ticker": ticker, "error": str(exc)},
        )
        valid = True  # broker unavailable; allow the add

    if not valid:
        return JSONResponse(
            status_code=422,
            content=err("INVALID_TICKER", f"{ticker!r} is not a valid tradeable symbol"),
        )

    repo = SymbolRepo(db)
    try:
        symbol = await repo.create(ticker=ticker, display_name=body.display_name)
        await db.commit()
    except ValueError as exc:
        return JSONResponse(status_code=409, content=err("SYMBOL_EXISTS", str(exc)))

    return JSONResponse(
        status_code=201,
        content=ok(jsonable_encoder(SymbolOut.model_validate(symbol))),
    )


@router.delete("/{ticker}")
async def remove_symbol(
    ticker: str = Path(..., description="Ticker symbol to remove, e.g. AAPL"),
    body: SymbolDeleteConfirmIn = Body(default_factory=SymbolDeleteConfirmIn),
    db: AsyncSession = Depends(get_db),
    broker: BaseBroker = Depends(get_broker),
) -> JSONResponse:
    ticker = ticker.upper()

    # Check for an open position before deleting
    has_position = False
    if broker.is_connected():
        try:
            positions = await broker.get_positions()
            has_position = any(p.symbol == ticker for p in positions)
        except Exception as exc:
            system_logger.warning(
                "Could not fetch positions for delete-symbol check",
                extra={"ticker": ticker, "error": str(exc)},
            )

    if has_position and not body.confirm:
        return JSONResponse(
            status_code=409,
            content=err(
                "OPEN_POSITION",
                f"{ticker!r} has an open position. "
                "Resend with confirm=true to delete anyway.",
            ),
        )

    repo = SymbolRepo(db)
    deleted = await repo.delete(ticker)
    if not deleted:
        return JSONResponse(
            status_code=404,
            content=err("NOT_FOUND", f"Symbol {ticker!r} not found"),
        )

    await db.commit()
    return JSONResponse(content=ok({"ticker": ticker, "deleted": True}))


@router.post("/{ticker}/fetch-history")
async def fetch_symbol_history(
    ticker: str = Path(..., description="Ticker symbol to fetch history for"),
    db: AsyncSession = Depends(get_db),
    broker: BaseBroker = Depends(get_broker),
) -> JSONResponse:
    ticker = ticker.upper()

    fetcher = HistoricalDataFetcher(broker)
    count = await fetcher.fetch_and_store(ticker, db)
    await db.commit()

    if count == 0:
        return JSONResponse(
            status_code=422,
            content=err(
                "NO_DATA_RETURNED",
                f"Broker returned no bars for {ticker}. Ensure IB Gateway is connected and the symbol is valid.",
            ),
        )

    return JSONResponse(content=ok({"ticker": ticker, "bars_stored": count}))
