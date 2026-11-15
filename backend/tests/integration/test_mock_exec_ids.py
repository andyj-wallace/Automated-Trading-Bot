"""A new MockBroker's executions must not collide with fills already stored for an earlier broker."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.base import OrderRequest
from app.brokers.mock.client import MockBroker
from app.db.models.trade import TradeDirection
from app.db.repositories.fill_repo import FillRepo
from app.db.repositories.trade_repo import TradeRepo

_DB_URL = os.environ["DATABASE_URL"]
_QUANTITY = Decimal("7")


@pytest_asyncio.fixture
async def factory():
    eng = create_async_engine(_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE trades RESTART IDENTITY CASCADE"))
    yield sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


async def _make_trade(factory) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as s:
        await TradeRepo(s).create(
            trade_id=tid,
            symbol="AAPL",
            direction=TradeDirection.BUY,
            quantity=Decimal("10"),
            entry_price=Decimal("200"),
            stop_loss_price=Decimal("195"),
            take_profit_price=Decimal("210"),
            reward_to_risk_ratio=Decimal("2"),
            risk_amount=Decimal("50"),
            account_balance_at_entry=Decimal("100000"),
        )
        await s.commit()
    return tid


async def _broker_filling_entry(trade_id: uuid.UUID) -> MockBroker:
    broker = MockBroker()
    await broker.connect()
    await broker.place_order(
        OrderRequest(
            trade_id=trade_id,
            symbol="AAPL",
            direction="BUY",
            quantity=_QUANTITY,
            order_ref=f"{trade_id}:entry",
        )
    )
    return broker


async def test_new_broker_entry_fill_is_stored_alongside_earlier_broker_fill(factory):
    trade_1 = await _make_trade(factory)
    trade_2 = await _make_trade(factory)

    first = await _broker_filling_entry(trade_1)
    async with factory() as s:
        assert await FillRepo(s).record(trade_1, await first.get_executions()) == 1
        await s.commit()

    second = await _broker_filling_entry(trade_2)
    async with factory() as s:
        repo = FillRepo(s)
        assert await repo.record(trade_2, await second.get_executions()) == 1
        totals = await repo.totals(trade_2)
        await s.commit()
    assert totals["ENTRY"].quantity == _QUANTITY
