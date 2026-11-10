"""Helpers for the backtesting API: fee schedule, bar conversion, validation and job runner."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.responses import JSONResponse

from app.api.v1.backtesting_models import (
    BacktestRunRequest,
    PortfolioBacktestRunRequest,
    PortfolioSlotRequest,
    _Job,
)
from app.api.v1.schemas import err
from app.brokers.base import PriceBar
from app.config import get_settings
from app.core.backtesting.portfolio_engine import PortfolioBacktestEngine, PortfolioSlot
from app.core.risk.costs import PLANS, CostModel
from app.core.risk.manager import RiskManager
from app.core.strategy_engine.registry import registry
from app.db.repositories.ohlcv_repo import OHLCVRepo


def _build_costs(body: BacktestRunRequest | PortfolioBacktestRunRequest) -> tuple[RiskManager, dict]:
    """
    Risk manager and engine kwargs for the requested fee schedule.

    "legacy" keeps today's engines exactly (flat $1 per leg, default RiskManager). Anything else
    uses the broker's real schedule, and the fee gate rejects in backtests as it would live.
    Growth-mode universe and position limits are not applied in backtests.
    """
    if body.commission_plan == "legacy":
        return RiskManager(), {"compound": body.compound}
    settings = get_settings()
    cost_model = CostModel(PLANS[body.commission_plan], Decimal(body.slippage_bps))
    risk_manager = RiskManager(
        min_reward_to_risk=settings.min_reward_to_risk,
        cost_model=cost_model,
        max_breakeven_win_rate=settings.max_breakeven_win_rate,
    )
    return risk_manager, {
        "slippage_pct": cost_model.slippage_bps / Decimal("10000"),
        "cost_model": cost_model,
        "compound": body.compound,
    }


def _to_price_bars(db_bars) -> list[PriceBar]:
    return [
        PriceBar(
            timestamp=b.time, open=b.open, high=b.high, low=b.low, close=b.close,
            volume=b.volume, bar_size=b.bar_size,
        )
        for b in db_bars
    ]


async def _load_extra_bars(
    repo: OHLCVRepo, strategy, start: datetime, end: datetime
) -> tuple[dict[str, list[PriceBar]], str | None]:
    """Bars for the strategy's extra symbols (e.g. SPY); the second item names a symbol with no data."""
    extra: dict[str, list[PriceBar]] = {}
    for symbol in strategy.required_extra_symbols():
        symbol = symbol.upper()
        rows = await repo.get_bars(symbol, start=start, end=end)
        if not rows:
            return extra, symbol
        extra[symbol] = _to_price_bars(rows)
    return extra, None


async def _load_slot_bars(
    ohlcv_repo: OHLCVRepo,
    slots: list[PortfolioSlotRequest],
    strategies: list,
    start: datetime,
    end: datetime,
) -> tuple[list[list[PriceBar]], list[dict[str, list[PriceBar]]], list[str]]:
    """Load daily bars and extra-symbol bars for each portfolio slot; collect symbols with no data."""
    bars_by_slot: list[list[PriceBar]] = []
    extras_by_slot: list[dict[str, list[PriceBar]]] = []
    missing: list[str] = []

    for slot_req, strategy in zip(slots, strategies, strict=True):
        ticker = slot_req.symbol.upper()
        db_bars = await ohlcv_repo.get_bars(ticker, start=start, end=end)
        if not db_bars:
            missing.append(ticker)
            continue
        extra_bars, missing_extra = await _load_extra_bars(ohlcv_repo, strategy, start, end)
        if missing_extra:
            missing.append(missing_extra)
            continue
        bars_by_slot.append(_to_price_bars(db_bars))
        extras_by_slot.append(extra_bars)

    return bars_by_slot, extras_by_slot, missing


def _require_daily_only(strategies) -> None:
    if any(s.needs_intraday for s in strategies):
        raise ValueError("day-trade strategies need 5-minute bars; backtests only have daily bars")


def _bad_request(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=422, content=err(code, message))


def _no_historical_data(missing: list[str]) -> JSONResponse:
    """422 response naming the symbols that have no stored OHLCV bars."""
    return JSONResponse(
        status_code=422,
        content=err(
            "NO_HISTORICAL_DATA",
            f"No historical OHLCV bars found for: {missing}. "
            "Fetch historical data first via HistoricalDataFetcher.",
        ),
    )


async def _run_portfolio_job_locked(
    job: _Job,
    slot_requests: list[PortfolioSlotRequest],
    bars_by_slot: list[list[PriceBar]],
    account_balance_str: str,
) -> None:
    job.status = "running"
    job.started_at = datetime.now(UTC)

    try:
        slots = [
            PortfolioSlot(
                strategy=registry.build(sr.strategy_type, sr.config),
                symbol=sr.symbol.upper(),
                bars=bars,
                strategy_type=sr.strategy_type,
                strategy_config=sr.config,
                extra_bars=extra,
            )
            for sr, bars, extra in zip(slot_requests, bars_by_slot, job.extra_bars, strict=True)
        ]
        risk_manager, engine_kwargs = job.costs or (RiskManager(), {})
        engine = PortfolioBacktestEngine(risk_manager, **engine_kwargs)
        result = await asyncio.to_thread(
            asyncio.run,
            engine.run(slots, account_balance=Decimal(account_balance_str)),
        )
        job.portfolio_result = result
        job.status = "done"
    except Exception as exc:
        job.error = str(exc)
        job.status = "error"
    finally:
        job.finished_at = datetime.now(UTC)
