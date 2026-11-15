"""A failed stop replacement must not adopt the old, cancelled stop as the new protection."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.base import OrderRequest
from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.risk.manager import RiskManager, TradeRequest
from app.db.models.trade import TradeStatus
from app.db.repositories.trade_repo import TradeRepo

ENTRY, STOP, QTY, BAL = Decimal("10.00"), Decimal("9.50"), Decimal("10"), Decimal("100000")


class FakeCache:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, json.loads(message)))

    def alerts(self, alert_type: str) -> list[dict]:
        out = [m["payload"] for c, m in self.published if m["event"] == "system_alert"]
        return [a for a in out if a["alert_type"] == alert_type]


class FlakyStopSubmitBroker(MockBroker):
    """While raise_stops is set, stop submissions raise; the old stop stays visible by ref."""

    def __init__(self) -> None:
        super().__init__()
        self.raise_stops = False

    async def place_order(self, order: OrderRequest):
        if self.raise_stops and order.order_type == "STP":
            self.placed_orders.append(order)
            raise ConnectionError("simulated submit failure")
        return await super().place_order(order)


@pytest_asyncio.fixture
async def db_engine():
    eng = create_async_engine(os.environ["DATABASE_URL"])
    async with eng.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE trades RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    factory = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _factory():
        async with factory() as sess:
            yield sess

    return _factory


@pytest_asyncio.fixture
async def broker():
    b = FlakyStopSubmitBroker()
    await b.connect()
    b.last_prices["AAPL"] = ENTRY
    yield b
    await b.disconnect()


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()


def _manager(broker, session_factory, cache) -> OrderManager:
    return OrderManager(
        broker,
        RiskManager(),
        session_factory,
        cache,
        order_retry_attempts=1,
        order_retry_backoff_seconds=0,
        is_market_open=lambda: True,
        protective_stops=True,
        stop_cancel_wait_seconds=0.5,
    )


async def _open_buy_ten(broker, session_factory, cache):
    """Buy 10 at 10.00 with a protective stop for 10 resting at the broker."""
    manager = _manager(broker, session_factory, cache)
    req = TradeRequest(
        trade_id=uuid.uuid4(), symbol="AAPL", direction="BUY", quantity=QTY,
        entry_price=ENTRY, stop_loss_price=STOP, account_balance=BAL,
    )
    await manager.submit_order(req)
    return manager, req.trade_id


async def _get(session_factory, trade_id):
    async with session_factory() as s:
        return await TradeRepo(s).get_by_id(trade_id)


def _stop_requests(broker, trade_id):
    return [o for o in broker.placed_orders if o.order_ref == f"{trade_id}:stop"]


async def test_replacement_submit_error_does_not_adopt_old_stop(
    broker, session_factory, cache
):
    manager, tid = await _open_buy_ten(broker, session_factory, cache)
    broker.set_order_state(f"{tid}:stop", "PARTIAL", Decimal("4"), Decimal("9.50"))
    await manager.sync_fills(tid)
    broker.set_position("AAPL", Decimal("6"))
    old_stop_id = (await _get(session_factory, tid)).stop_order_id
    assert old_stop_id is not None
    before = len(_stop_requests(broker, tid))
    broker.raise_stops = True

    assert await manager.ensure_protective_stop(tid, list(broker.executions)) is False

    assert cache.alerts("PROTECTIVE_STOP_REPLACED") == []
    assert len(cache.alerts("PROTECTIVE_STOP_FAILED")) == 1
    trade = await _get(session_factory, tid)
    assert trade.status == TradeStatus.OPEN
    assert trade.stop_order_id == old_stop_id
    assert len(_stop_requests(broker, tid)) == before + 1

    broker.raise_stops = False
    before = len(_stop_requests(broker, tid))

    assert await manager.ensure_protective_stop(tid, list(broker.executions)) is True

    stops = _stop_requests(broker, tid)
    assert len(stops) == before + 1
    assert stops[-1].quantity == 6
    new_stop = broker.order_results[f"{tid}:stop"]
    assert new_stop.status == "WORKING"
    assert new_stop.broker_order_id != old_stop_id
    trade = await _get(session_factory, tid)
    assert trade.stop_order_id == new_stop.broker_order_id
    assert len(cache.alerts("PROTECTIVE_STOP_REPLACED")) == 1
