"""D1/D2/D3/D6 through the real engine: manual order, manual close, flatten, status, health."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest_asyncio
from fastapi import FastAPI

from app.api.v1 import system, trades, trading
from app.db.models.trade import ExitReason, TradeStatus
from app.db.repositories.symbol_repo import SymbolRepo
from app.db.repositories.trade_repo import TradeRepo
from tests.integration.test_engine_wiring import _poll, trading_engine  # noqa: F401


@pytest_asyncio.fixture
async def api(trading_engine):  # noqa: F811
    eng, session_factory = trading_engine
    app = FastAPI()
    for module in (trading, trades, system):
        app.include_router(module.router, prefix="/api/v1")
    app.state.engine = eng
    async with session_factory() as s:
        repo = SymbolRepo(s)
        if await repo.get_by_ticker("TSLA") is None:
            await repo.create("TSLA")
        await s.commit()
    await eng.feed.sync()
    await eng.broker.simulate_price_tick("TSLA", Decimal("200"))
    assert await _poll(lambda: _price_ready(eng))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, eng, session_factory


async def _price_ready(eng):
    return await eng.fresh_price("TSLA")


async def test_manual_order_close_and_status(api):
    client, eng, session_factory = api
    resp = await client.post(
        "/api/v1/trades", json={"symbol": "TSLA", "stop_loss_price": "195", "quantity": 10}
    )
    assert resp.status_code == 201, resp.text
    trade_id = resp.json()["data"]["id"]

    dup = await client.post("/api/v1/trades", json={"symbol": "TSLA", "stop_loss_price": "195"})
    assert dup.status_code == 422 and dup.json()["error"]["code"] == "SYMBOL_ALREADY_ACTIVE"

    active = (await client.get("/api/v1/trades/active")).json()["data"]
    assert [t["id"] for t in active] == [trade_id]

    status = (await client.get("/api/v1/trading/status")).json()["data"]
    assert status["open_trade_count"] == 1 and status["engine_lock_held"] is True
    assert status["paused"] is False

    health = await client.get("/api/v1/system/health")
    assert health.json()["data"]["engine"]["status"] == "ok"
    assert health.json()["data"]["engine"]["info"]["tracked_trades"] >= 0

    closed = await client.post(f"/api/v1/trades/{trade_id}/close")
    assert closed.status_code == 200, closed.text
    async with session_factory() as s:
        trade = await TradeRepo(s).get_by_id(trade_id)
    assert trade.status == TradeStatus.CLOSED and trade.exit_reason == ExitReason.MANUAL

    again = await client.post(f"/api/v1/trades/{trade_id}/close")
    assert again.status_code == 409


async def test_flatten_closes_everything_and_pauses(api):
    client, eng, session_factory = api
    resp = await client.post(
        "/api/v1/trades", json={"symbol": "TSLA", "stop_loss_price": "195", "quantity": 10}
    )
    trade_id = resp.json()["data"]["id"]

    assert (await client.post("/api/v1/trading/flatten", json={"confirm": "no"})).status_code == 422
    out = await client.post("/api/v1/trading/flatten", json={"confirm": "FLATTEN"})
    assert out.json()["data"]["results"][0]["outcome"] == "CLOSED"
    async with session_factory() as s:
        trade = await TradeRepo(s).get_by_id(trade_id)
    assert trade.status == TradeStatus.CLOSED
    assert await eng.trading_state.is_paused() is True
    await client.post("/api/v1/trading/resume")
    assert await eng.trading_state.is_paused() is False


async def test_daily_summary_writes_snapshot_and_counts_opened(api):
    from datetime import UTC, datetime, timedelta

    from app.core.market_calendar import trading_date

    client, eng, session_factory = api
    await client.post(
        "/api/v1/trades", json={"symbol": "TSLA", "stop_loss_price": "195", "quantity": 10}
    )
    sent = {}

    async def capture(subject, body):
        sent["body"] = body

    eng.dispatcher.notify_daily_summary = capture
    body = await eng.daily_summary.run(trading_date())
    assert "Trades opened today: 1" in sent["body"] == body
    from sqlalchemy import text

    async with session_factory() as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM portfolio_snapshots WHERE time > :t"),
                {"t": datetime.now(UTC) - timedelta(minutes=1)},
            )
        ).scalar_one()
    assert count >= 1
