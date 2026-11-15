"""Protective stop sized to the open quantity; replacement only when the broker holds it."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

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
        import json

        self.published.append((channel, json.loads(message)))

    def alerts(self, alert_type: str) -> list[dict]:
        out = [m["payload"] for c, m in self.published if m["event"] == "system_alert"]
        return [a for a in out if a["alert_type"] == alert_type]


class CountingBroker(MockBroker):
    """Counts get_executions() calls so tests can prove no broker fetch happened."""

    def __init__(self) -> None:
        super().__init__()
        self.execution_fetches = 0

    async def get_executions(self):
        self.execution_fetches += 1
        return await super().get_executions()


class ErrorAfterCancelBroker(CountingBroker):
    """After a stop cancel the stop keeps reporting ERROR."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    async def cancel_order(self, broker_order_id: str) -> bool:
        self.cancelled = True
        return await super().cancel_order(broker_order_id)

    async def find_order_by_ref(self, order_ref: str):
        result = await super().find_order_by_ref(order_ref)
        if self.cancelled and order_ref.endswith(":stop") and result is not None:
            return result.model_copy(update={"status": "ERROR"})
        return result


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
    b = CountingBroker()
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


async def _clear_stop_order_id(session_factory, trade_id) -> None:
    async with session_factory() as s:
        await TradeRepo(s).set_stop_order(trade_id, None)
        await s.commit()


async def _set_status(session_factory, trade_id, status: TradeStatus) -> None:
    async with session_factory() as s:
        trade = await TradeRepo(s).get_by_id(trade_id)
        trade.status = status
        await s.commit()


def _stop_requests(broker, trade_id):
    return [o for o in broker.placed_orders if o.order_ref == f"{trade_id}:stop"]


async def _stop_partly_filled(broker, manager, trade_id) -> None:
    """The stop filled 4 at 9.50 and is no longer working; the journal shows the 4."""
    broker.set_order_state(f"{trade_id}:stop", "PARTIAL", Decimal("4"), Decimal("9.50"))
    await manager.sync_fills(trade_id)


async def test_place_protective_stop_sizes_new_stop_to_open_quantity(
    broker, session_factory, cache
):
    manager, tid = await _open_buy_ten(broker, session_factory, cache)
    await _stop_partly_filled(broker, manager, tid)
    await _clear_stop_order_id(session_factory, tid)
    assert len(_stop_requests(broker, tid)) == 1

    assert await manager.place_protective_stop(tid) is True

    stops = _stop_requests(broker, tid)
    assert len(stops) == 2
    assert stops[-1].quantity == 6
    trade = await _get(session_factory, tid)
    assert trade.stop_order_id == broker.order_results[f"{tid}:stop"].broker_order_id


async def test_place_protective_stop_places_nothing_when_stop_filled_the_whole_position(
    broker, session_factory, cache
):
    manager, tid = await _open_buy_ten(broker, session_factory, cache)
    broker.set_order_state(f"{tid}:stop", "FILLED", Decimal("10"), Decimal("9.50"))
    await manager.sync_fills(tid)
    await _clear_stop_order_id(session_factory, tid)

    assert await manager.place_protective_stop(tid) is False

    assert len(_stop_requests(broker, tid)) == 1
    assert (await _get(session_factory, tid)).stop_order_id is None


async def test_ensure_with_executions_skips_broker_fetch_when_stop_covers_open(
    broker, session_factory, cache
):
    manager, tid = await _open_buy_ten(broker, session_factory, cache)
    fetches_before = broker.execution_fetches

    assert await manager.ensure_protective_stop(tid, list(broker.executions)) is False

    assert broker.execution_fetches == fetches_before
    assert len(_stop_requests(broker, tid)) == 1


@pytest.mark.parametrize("held", [Decimal("6"), Decimal("8")])
async def test_partial_stop_is_replaced_for_open_quantity(
    broker, session_factory, cache, held
):
    manager, tid = await _open_buy_ten(broker, session_factory, cache)
    await _stop_partly_filled(broker, manager, tid)
    broker.set_position("AAPL", held)
    before = len(_stop_requests(broker, tid))

    assert await manager.ensure_protective_stop(tid, list(broker.executions)) is True

    stops = _stop_requests(broker, tid)
    assert len(stops) == before + 1
    assert stops[-1].quantity == 6
    trade = await _get(session_factory, tid)
    assert trade.stop_order_id == broker.order_results[f"{tid}:stop"].broker_order_id
    replaced = cache.alerts("PROTECTIVE_STOP_REPLACED")
    assert len(replaced) == 1
    assert replaced[0]["new_quantity"] == "6"
    assert replaced[0]["trade_id"] == str(tid)


async def test_rejected_replacement_returns_false_without_replaced_alert(
    broker, session_factory, cache
):
    manager, tid = await _open_buy_ten(broker, session_factory, cache)
    await _stop_partly_filled(broker, manager, tid)
    broker.set_position("AAPL", Decimal("6"))
    broker.next_status = "REJECTED"

    assert await manager.ensure_protective_stop(tid, list(broker.executions)) is False

    assert cache.alerts("PROTECTIVE_STOP_REPLACED") == []
    assert len(cache.alerts("PROTECTIVE_STOP_FAILED")) == 1


@pytest.mark.parametrize("held", [Decimal("0"), Decimal("-6"), Decimal("3")])
async def test_broker_holding_less_than_open_quantity_places_nothing(
    broker, session_factory, cache, held
):
    manager, tid = await _open_buy_ten(broker, session_factory, cache)
    await _stop_partly_filled(broker, manager, tid)
    broker.set_position("AAPL", held)
    before = len(_stop_requests(broker, tid))

    assert await manager.ensure_protective_stop(tid, list(broker.executions)) is False

    assert len(_stop_requests(broker, tid)) == before
    assert broker.order_results[f"{tid}:stop"].status == "PARTIAL"
    assert cache.alerts("PROTECTIVE_STOP_REPLACED") == []


async def test_wrong_size_working_stop_still_erroring_after_cancel_places_no_stop(
    session_factory, cache
):
    broker = ErrorAfterCancelBroker()
    await broker.connect()
    broker.last_prices["AAPL"] = ENTRY
    try:
        manager, tid = await _open_buy_ten(broker, session_factory, cache)
        await _stop_partly_filled(broker, manager, tid)
        # Still WORKING for the original 10 while only 6 remain open.
        broker.set_order_state(f"{tid}:stop", "WORKING", Decimal("0"), Decimal("9.50"))
        broker.set_position("AAPL", Decimal("6"))
        before = len(_stop_requests(broker, tid))

        assert await manager.ensure_protective_stop(tid, list(broker.executions)) is False

        assert len(_stop_requests(broker, tid)) == before
        assert cache.alerts("PROTECTIVE_STOP_REPLACED") == []
    finally:
        await broker.disconnect()


async def test_trade_that_is_not_open_is_left_alone(broker, session_factory, cache):
    manager, tid = await _open_buy_ten(broker, session_factory, cache)
    await _stop_partly_filled(broker, manager, tid)
    broker.set_position("AAPL", Decimal("6"))
    await _set_status(session_factory, tid, TradeStatus.CLOSING)
    before = len(_stop_requests(broker, tid))

    assert await manager.ensure_protective_stop(tid, list(broker.executions)) is False
    assert await manager.place_protective_stop(tid, force=True) is False

    assert len(_stop_requests(broker, tid)) == before
    assert broker.order_results[f"{tid}:stop"].status == "PARTIAL"
