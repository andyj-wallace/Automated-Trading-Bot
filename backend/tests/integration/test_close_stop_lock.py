"""AC7: stop replacement and close_position for one trade never interleave on the trade lock."""

from __future__ import annotations

import asyncio
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
from app.core.risk.manager import RiskManager
from app.db.models.trade import TradeDirection, TradeStatus
from app.db.repositories.trade_repo import TradeRepo

ENTRY, STOP, TARGET, QTY, BAL = (
    Decimal("200"),
    Decimal("195"),
    Decimal("210"),
    Decimal("10"),
    Decimal("100000"),
)
CLOSED_OR_CLOSING = {TradeStatus.CLOSING, TradeStatus.CLOSED}


class FakeCache:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel: str, message: str) -> None:
        import json

        self.published.append((channel, json.loads(message)))

    async def get(self, key: str):
        return None


class GatedBroker(MockBroker):
    """Records stop and exit activity; the first cancel of a stop waits on `release`."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple] = []
        self.release = asyncio.Event()
        self.armed = False
        self._gate_used = False

    async def place_order(self, order: OrderRequest):
        if self.armed:
            self.events.append(("place", order.order_ref, order.order_type, order.quantity))
        return await super().place_order(order)

    async def cancel_order(self, broker_order_id: str) -> bool:
        if not self.armed:
            return await super().cancel_order(broker_order_id)
        self.events.append(("cancel_start", broker_order_id))
        if not self._gate_used:
            self._gate_used = True
            await self.release.wait()
        ok = await super().cancel_order(broker_order_id)
        self.events.append(("cancel_end", broker_order_id))
        return ok


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
    b = GatedBroker()
    await b.connect()
    b.last_prices["AAPL"] = ENTRY
    yield b
    await b.disconnect()


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()


async def _setup(broker, session_factory, cache):
    """OPEN BUY of 10 with a WORKING stop for 4 (too small), so replacement must cancel it."""
    tid = uuid.uuid4()
    async with session_factory() as s:
        repo = TradeRepo(s)
        await repo.create(
            trade_id=tid, symbol="AAPL", direction=TradeDirection.BUY, quantity=QTY,
            entry_price=ENTRY, stop_loss_price=STOP, take_profit_price=TARGET,
            reward_to_risk_ratio=Decimal("2"), risk_amount=Decimal("50"),
            account_balance_at_entry=BAL,
        )
        await repo.update_status(tid, TradeStatus.OPEN)
        await repo.record_entry_fill(tid, "1", ENTRY, QTY)
        await s.commit()

    broker.set_position("AAPL", QTY, ENTRY)
    stop = await broker.place_order(
        OrderRequest(
            trade_id=tid, symbol="AAPL", direction="SELL", quantity=Decimal("4"),
            order_type="STP", stop_price=STOP, tif="GTC", order_ref=f"{tid}:stop",
        )
    )
    assert stop.status == "WORKING" and stop.filled_quantity == 0
    async with session_factory() as s:
        await TradeRepo(s).set_stop_order(tid, stop.broker_order_id)
        await s.commit()

    manager = OrderManager(
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
    return manager, tid, stop.broker_order_id


async def _get_status(session_factory, trade_id) -> TradeStatus:
    async with session_factory() as s:
        return (await TradeRepo(s).get_by_id(trade_id)).status


async def _wait_for_cancel_start(broker: GatedBroker) -> None:
    async def poll() -> None:
        while not any(e[0] == "cancel_start" for e in broker.events):
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=2)


async def test_replacement_and_close_do_not_interleave(broker, session_factory, cache):
    manager, tid, old_stop_id = await _setup(broker, session_factory, cache)
    stop_ref = f"{tid}:stop"
    broker.armed = True

    t1 = asyncio.create_task(manager.ensure_protective_stop(tid))
    await _wait_for_cancel_start(broker)

    t2 = asyncio.create_task(manager.close_position(tid, "MANUAL"))
    seen = len(broker.events)
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(broker.events) == seen, "close_position ran while replacement held the lock"
    assert not t2.done()

    broker.release.set()
    replaced, _outcome = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5)
    assert replaced is True

    new_stop_id = broker.order_results[stop_ref].broker_order_id
    assert new_stop_id != old_stop_id
    assert broker.events == [
        ("cancel_start", old_stop_id),
        ("cancel_end", old_stop_id),
        ("place", stop_ref, "STP", Decimal("10")),
        ("cancel_start", new_stop_id),
        ("cancel_end", new_stop_id),
        ("place", f"{tid}:exit", "MKT", Decimal("10")),
    ]
    assert await _get_status(session_factory, tid) in CLOSED_OR_CLOSING
    stop = await broker.find_order_by_ref(stop_ref)
    assert stop is None or stop.is_terminal


async def test_close_first_then_replacement_places_nothing(broker, session_factory, cache):
    manager, tid, old_stop_id = await _setup(broker, session_factory, cache)
    stop_ref = f"{tid}:stop"
    broker.armed = True

    t1 = asyncio.create_task(manager.close_position(tid, "MANUAL"))
    await _wait_for_cancel_start(broker)

    t2 = asyncio.create_task(manager.ensure_protective_stop(tid))
    seen = len(broker.events)
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(broker.events) == seen, "replacement ran while close_position held the lock"
    assert not t2.done()

    broker.release.set()
    _outcome, replaced = await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5)
    assert replaced is False

    assert broker.events == [
        ("cancel_start", old_stop_id),
        ("cancel_end", old_stop_id),
        ("place", f"{tid}:exit", "MKT", Decimal("10")),
    ]
    assert await _get_status(session_factory, tid) in CLOSED_OR_CLOSING
    stop = await broker.find_order_by_ref(stop_ref)
    assert stop is None or stop.is_terminal
