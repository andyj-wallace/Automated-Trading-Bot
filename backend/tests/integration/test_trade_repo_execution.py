"""C1: atomic begin_close / finish_close and execution columns."""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.models.trade import ExitReason, TradeDirection, TradeStatus
from app.db.repositories.trade_repo import TradeRepo

_DB_URL = os.environ["DATABASE_URL"]


@pytest_asyncio.fixture
async def factory():
    eng = create_async_engine(_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE trades RESTART IDENTITY CASCADE"))
    yield sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


async def _make_open(factory) -> uuid.UUID:
    tid = uuid.uuid4()
    async with factory() as s:
        repo = TradeRepo(s)
        await repo.create(
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
        await repo.update_status(tid, TradeStatus.OPEN)
        await repo.record_entry_fill(tid, "1", Decimal("200.5"), Decimal("4"))
        await s.commit()
    return tid


async def test_record_entry_fill_keeps_audit_columns(factory):
    tid = await _make_open(factory)
    async with factory() as s:
        t = await TradeRepo(s).get_by_id(tid)
    assert t.entry_fill_price == Decimal("200.5")
    assert t.filled_quantity == Decimal("4")
    assert t.entry_order_id == "1"
    assert t.entry_price == Decimal("200")
    assert t.quantity == Decimal("10")
    assert t.close_attempts == 0


async def test_concurrent_begin_close_only_one_wins(factory):
    tid = await _make_open(factory)

    async def attempt():
        async with factory() as s:
            got = await TradeRepo(s).begin_close(tid, ExitReason.STOP_LOSS)
            await s.commit()
            return got

    results = await asyncio.gather(attempt(), attempt(), attempt())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].status == TradeStatus.CLOSING
    assert winners[0].close_attempts == 1


async def test_begin_close_allow_from_closing(factory):
    tid = await _make_open(factory)
    async with factory() as s:
        repo = TradeRepo(s)
        assert await repo.begin_close(tid, ExitReason.STALE_TIMEOUT) is not None
        assert await repo.begin_close(tid, ExitReason.MANUAL) is None
        again = await repo.begin_close(tid, ExitReason.STALE_TIMEOUT, allow_from_closing=True)
        await s.commit()
    assert again is not None and again.close_attempts == 2


async def test_finish_close_is_atomic(factory):
    tid = await _make_open(factory)
    async with factory() as s:
        repo = TradeRepo(s)
        assert await repo.finish_close(tid, Decimal("1"), Decimal("1"), "9") is None  # OPEN
        await repo.begin_close(tid, ExitReason.EXTERNAL)
        done = await repo.finish_close(tid, Decimal("210"), Decimal("38"), "9")
        again = await repo.finish_close(tid, Decimal("210"), Decimal("38"), "9")
        await s.commit()
    assert done is not None and done.status == TradeStatus.CLOSED
    assert done.exit_order_id == "9" and done.closed_at is not None
    assert again is None
