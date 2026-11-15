"""Flatten settles a partly filled entry before adopting it, then closes only what is held."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.mock.client import MockBroker
from app.core.execution.flatten import flatten_positions
from app.core.risk.manager import TradeRequest
from app.db.models.trade import TradeStatus
from app.db.repositories.trade_repo import TradeRepo
from tests.integration.test_entry_partial_fill import (
    BAL,
    ENTRY,
    QTY,
    STOP,
    FakeCache,
    _get,
    _manager,
)


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


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()


@pytest_asyncio.fixture
async def broker():
    b = MockBroker()
    await b.connect()
    b.last_prices["AAPL"] = ENTRY
    yield b
    await b.disconnect()


class CancelFillsRemainderBroker(MockBroker):
    """Cancel fills the remaining 6 shares first, so the entry ends FILLED with 10."""

    async def cancel_order(self, broker_order_id: str) -> bool:
        ref = self._ref_by_order_id[broker_order_id]
        if not ref.endswith(":entry"):
            return await super().cancel_order(broker_order_id)
        self.set_order_state(ref, "FILLED", Decimal("10"), ENTRY)
        return True


class CallOrderBroker(MockBroker):
    """Records cancel and place calls in the order they arrive."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    async def cancel_order(self, broker_order_id: str) -> bool:
        self.calls.append(("cancel", broker_order_id))
        return await super().cancel_order(broker_order_id)

    async def place_order(self, order):
        self.calls.append(("place", order.direction))
        return await super().place_order(order)


async def _submit_entry_working_with_no_fills(broker, session_factory, cache) -> uuid.UUID:
    """Submit a 10-share buy whose entry is WORKING at the broker with nothing filled yet."""
    manager = _manager(broker, session_factory, cache)
    req = TradeRequest(
        trade_id=uuid.uuid4(), symbol="AAPL", direction="BUY", quantity=QTY,
        entry_price=ENTRY, stop_loss_price=STOP, account_balance=BAL,
    )
    broker.next_status, broker.next_filled_quantity = "WORKING", Decimal("0")
    await manager.submit_order(req)
    return req.trade_id


async def _flatten(broker, session_factory, cache) -> list[dict]:
    manager = _manager(broker, session_factory, cache)
    trading_state = SimpleNamespace(pause=AsyncMock())
    async with session_factory() as session:
        return await flatten_positions(
            trading_state, manager, broker, TradeRepo(session), idle_timeout=1.0
        )


def _exit_sells(broker) -> list:
    """Exit sells only; the protective stop is also a SELL but has a different ref."""
    return [
        o for o in broker.placed_orders
        if o.direction == "SELL" and o.order_ref.endswith(":exit")
    ]


async def test_submitted_partial_entry_is_cancelled_adopted_and_sold_for_fills(
    broker, session_factory, cache
):
    tid = await _submit_entry_working_with_no_fills(broker, session_factory, cache)
    assert (await _get(session_factory, tid)).status == TradeStatus.SUBMITTED
    broker.set_order_state(f"{tid}:entry", "WORKING", Decimal("4"), ENTRY)
    broker.set_position("AAPL", Decimal("4"), ENTRY)

    results = await _flatten(broker, session_factory, cache)

    assert results == [
        {"trade_id": str(tid), "symbol": "AAPL", "outcome": "ENTRY_ADOPTED"},
        {"trade_id": str(tid), "symbol": "AAPL", "outcome": "CLOSED"},
    ]
    assert broker.order_results[f"{tid}:entry"].status == "PARTIAL"
    assert [s.quantity for s in _exit_sells(broker)] == [4]
    assert (await _get(session_factory, tid)).status == TradeStatus.CLOSED


async def test_remainder_filling_during_cancel_is_sold_in_full(session_factory, cache):
    broker = CancelFillsRemainderBroker()
    await broker.connect()
    try:
        broker.last_prices["AAPL"] = ENTRY
        tid = await _submit_entry_working_with_no_fills(broker, session_factory, cache)
        broker.set_order_state(f"{tid}:entry", "WORKING", Decimal("4"), ENTRY)
        broker.set_position("AAPL", Decimal("10"), ENTRY)

        results = await _flatten(broker, session_factory, cache)

        assert results[0] == {"trade_id": str(tid), "symbol": "AAPL", "outcome": "ENTRY_ADOPTED"}
        assert results[-1]["outcome"] == "CLOSED"
        assert broker.order_results[f"{tid}:entry"].status == "FILLED"
        assert [s.quantity for s in _exit_sells(broker)] == [10]
        assert (await _get(session_factory, tid)).filled_quantity == Decimal("10")
    finally:
        await broker.disconnect()


async def test_open_trade_cancels_working_entry_remainder_before_selling(
    session_factory, cache
):
    broker = CallOrderBroker()
    await broker.connect()
    try:
        broker.last_prices["AAPL"] = ENTRY
        # The real cancel turns this WORKING entry into PARTIAL, so the trade opens at 4.
        broker.next_status, broker.next_filled_quantity = "WORKING", Decimal("4")
        manager = _manager(broker, session_factory, cache)
        req = TradeRequest(
            trade_id=uuid.uuid4(), symbol="AAPL", direction="BUY", quantity=QTY,
            entry_price=ENTRY, stop_loss_price=STOP, account_balance=BAL,
        )
        await manager.submit_order(req)
        tid = req.trade_id
        assert (await _get(session_factory, tid)).status == TradeStatus.OPEN
        # Put the entry remainder back to WORKING at the broker, with the 4 fills unchanged.
        broker.set_order_state(f"{tid}:entry", "WORKING", Decimal("4"), ENTRY)
        broker.set_position("AAPL", Decimal("4"), ENTRY)
        entry_broker_id = broker.order_results[f"{tid}:entry"].broker_order_id
        broker.calls.clear()

        results = await _flatten(broker, session_factory, cache)

        assert results == [{"trade_id": str(tid), "symbol": "AAPL", "outcome": "CLOSED"}]
        assert ("cancel", entry_broker_id) in broker.calls
        assert broker.calls.index(("cancel", entry_broker_id)) < broker.calls.index(
            ("place", "SELL")
        )
        assert [s.quantity for s in _exit_sells(broker)] == [4]
    finally:
        await broker.disconnect()
