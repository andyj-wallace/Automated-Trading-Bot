"""FillRepo, TradeRepo.raise_entry_fill and never-lowering entry-fill writes."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.base import Execution
from app.db.models.trade import TradeDirection, TradeStatus
from app.db.repositories.fill_repo import FillRepo
from app.db.repositories.trade_repo import TradeRepo

_DB_URL = os.environ["DATABASE_URL"]


@pytest_asyncio.fixture
async def factory():
    eng = create_async_engine(_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE trades RESTART IDENTITY CASCADE"))
    yield sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


async def _make_trade(factory, status: TradeStatus = TradeStatus.PENDING) -> uuid.UUID:
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
        if status != TradeStatus.PENDING:
            await repo.update_status(tid, status)
        await s.commit()
    return tid


def _ex(
    trade_id: uuid.UUID,
    exec_id: str,
    *,
    leg: str = "entry",
    quantity: str = "4",
    price: str = "10.00",
    commission: str | None = None,
) -> Execution:
    return Execution(
        exec_id=exec_id,
        order_ref=f"{trade_id}:{leg}",
        broker_order_id="1",
        side="BUY",
        quantity=Decimal(quantity),
        price=Decimal(price),
        commission=None if commission is None else Decimal(commission),
        executed_at=datetime.now(UTC),
    )


async def _trade_row(factory, tid: uuid.UUID):
    async with factory() as s:
        return await TradeRepo(s).get_by_id(tid)


async def test_record_twice_stores_one_row_per_exec_and_ignores_unrelated_refs(factory):
    tid = await _make_trade(factory)
    other = uuid.uuid4()
    batch = [
        _ex(tid, "e1"),
        _ex(tid, "e2", leg="stop", quantity="10", price="9.50"),
        _ex(tid, "e3", leg="exit", quantity="10", price="11.00"),
        _ex(other, "e4"),
        Execution(
            exec_id="e5",
            order_ref="unrelated",
            broker_order_id="1",
            side="BUY",
            quantity=Decimal("1"),
            price=Decimal("1"),
            executed_at=datetime.now(UTC),
        ),
    ]
    async with factory() as s:
        assert await FillRepo(s).record(tid, batch) == 3
        await s.commit()
    async with factory() as s:
        assert await FillRepo(s).record(tid, batch) == 0
        await s.commit()
    async with factory() as s:
        totals = await FillRepo(s).totals(tid)
    assert totals["ENTRY"].quantity == Decimal("4")
    assert totals["STOP"].quantity == Decimal("10")
    assert totals["EXIT"].quantity == Decimal("10")
    async with factory() as s:
        other_totals = await FillRepo(s).totals(other)
    assert other_totals["ENTRY"].quantity == Decimal("0")


async def test_no_matching_executions_returns_zero(factory):
    tid = await _make_trade(factory)
    async with factory() as s:
        assert await FillRepo(s).record(tid, []) == 0


async def test_later_commission_replaces_null_and_quantity_price_never_change(factory):
    tid = await _make_trade(factory)
    async with factory() as s:
        await FillRepo(s).record(tid, [_ex(tid, "c1", commission=None)])
        await s.commit()
    async with factory() as s:
        await FillRepo(s).record(tid, [_ex(tid, "c1", commission="1.00")])
        await s.commit()
    async with factory() as s:
        assert (await FillRepo(s).totals(tid))["ENTRY"].commission == Decimal("1.00")
    async with factory() as s:
        # A later report with a different commission must not replace a non-NULL one.
        await FillRepo(s).record(tid, [_ex(tid, "c1", commission="0.50")])
        await s.commit()
    async with factory() as s:
        assert (await FillRepo(s).totals(tid))["ENTRY"].commission == Decimal("1.00")
    async with factory() as s:
        # Same exec_id with a different quantity and price changes neither.
        await FillRepo(s).record(tid, [_ex(tid, "c1", quantity="7", price="99.00")])
        await s.commit()
    async with factory() as s:
        entry = (await FillRepo(s).totals(tid))["ENTRY"]
    assert entry.quantity == Decimal("4")
    assert entry.notional == Decimal("40.00")
    assert entry.commission == Decimal("1.00")


async def test_totals_sum_each_leg_and_null_commission_counts_as_zero(factory):
    tid = await _make_trade(factory)
    async with factory() as s:
        await FillRepo(s).record(
            tid,
            [
                _ex(tid, "t1", quantity="4", price="10.00", commission="1.00"),
                _ex(tid, "t2", quantity="6", price="10.50", commission=None),
                _ex(tid, "t3", leg="exit", quantity="10", price="12.00", commission="2.00"),
            ],
        )
        await s.commit()
    async with factory() as s:
        totals = await FillRepo(s).totals(tid)
    assert set(totals) == {"ENTRY", "STOP", "EXIT"}
    assert totals["ENTRY"].quantity == Decimal("10")
    assert totals["ENTRY"].notional == Decimal("103.00")
    assert totals["ENTRY"].commission == Decimal("1.00")
    assert totals["STOP"].quantity == Decimal("0")
    assert totals["STOP"].commission == Decimal("0")
    assert totals["EXIT"].notional == Decimal("120.00")
    assert totals["EXIT"].commission == Decimal("2.00")


async def test_raise_entry_fill_quantity_up_only(factory):
    tid = await _make_trade(factory)
    async with factory() as s:
        repo = TradeRepo(s)
        await repo.record_entry_fill(tid, "1", Decimal("10.00"), Decimal("4"))
        assert await repo.raise_entry_fill(tid, Decimal("10"), Decimal("10.12"), Decimal("2.00"))
        assert not await repo.raise_entry_fill(tid, Decimal("4"), Decimal("10.00"), Decimal("1"))
        await s.commit()
    t = await _trade_row(factory, tid)
    assert t.filled_quantity == Decimal("10")
    assert t.entry_fill_price == Decimal("10.12")
    assert t.entry_commission == Decimal("2.00")


async def test_raise_entry_fill_same_quantity_raises_commission_only(factory):
    tid = await _make_trade(factory)
    async with factory() as s:
        repo = TradeRepo(s)
        await repo.record_entry_fill(tid, "1", Decimal("10.00"), Decimal("4"))
        await s.commit()
    async with factory() as s:
        assert await TradeRepo(s).raise_entry_fill(
            tid, Decimal("4"), Decimal("10.00"), Decimal("1.00")
        )
        await s.commit()
    async with factory() as s:
        assert not await TradeRepo(s).raise_entry_fill(
            tid, Decimal("4"), Decimal("10.00"), Decimal("0.50")
        )
        await s.commit()
    t = await _trade_row(factory, tid)
    assert t.filled_quantity == Decimal("4")
    assert t.entry_commission == Decimal("1.00")


async def test_stale_entry_writes_keep_journal_values(factory):
    tid = await _make_trade(factory, status=TradeStatus.SUBMITTED)
    async with factory() as s:
        await TradeRepo(s).raise_entry_fill(tid, Decimal("10"), Decimal("10.12"), Decimal("2.00"))
        await s.commit()
    async with factory() as s:
        await TradeRepo(s).record_entry_fill(tid, "1", Decimal("10.00"), Decimal("4"), Decimal("1.00"))
        await s.commit()
    async with factory() as s:
        t = await TradeRepo(s).open_from_fill(
            tid, "1", Decimal("10.00"), Decimal("4"), Decimal("1.00")
        )
        await s.commit()
    assert t is not None
    assert t.status == TradeStatus.OPEN
    t = await _trade_row(factory, tid)
    assert t.filled_quantity == Decimal("10")
    assert t.entry_fill_price == Decimal("10.12")
    assert t.entry_commission == Decimal("2.00")
    assert t.entry_order_id == "1"


async def test_first_entry_write_on_fresh_trade_still_writes(factory):
    tid = await _make_trade(factory)
    async with factory() as s:
        await TradeRepo(s).record_entry_fill(tid, "7", Decimal("200.50"), Decimal("4"), None)
        await s.commit()
    t = await _trade_row(factory, tid)
    assert t.filled_quantity == Decimal("4")
    assert t.entry_fill_price == Decimal("200.50")
    assert t.entry_commission is None
    assert t.entry_order_id == "7"


async def test_audit_columns_never_change(factory):
    tid = await _make_trade(factory, status=TradeStatus.SUBMITTED)
    async with factory() as s:
        await TradeRepo(s).raise_entry_fill(tid, Decimal("10"), Decimal("10.12"), Decimal("2.00"))
        await TradeRepo(s).record_entry_fill(tid, "1", Decimal("10.00"), Decimal("4"), Decimal("1"))
        await TradeRepo(s).open_from_fill(tid, "1", Decimal("10.00"), Decimal("4"), Decimal("1"))
        await s.commit()
    t = await _trade_row(factory, tid)
    assert t.entry_price == Decimal("200")
    assert t.stop_loss_price == Decimal("195")
    assert t.status == TradeStatus.OPEN
