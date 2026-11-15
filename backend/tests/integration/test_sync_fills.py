"""OrderManager fill sync: journal, entry-fill raise, late commission, open quantity, cancel-and-wait."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.base import OrderResult
from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.risk.manager import RiskManager, TradeRequest
from app.db.models.trade import Trade
from app.db.models.trade_fill import TradeFill
from app.db.repositories.trade_repo import TradeRepo

_DB_URL = os.environ["DATABASE_URL"]
_SYMBOL = "AAPL"
_BALANCE = Decimal("100000")


@pytest_asyncio.fixture
async def broker() -> MockBroker:
    b = MockBroker()
    await b.connect()
    yield b
    await b.disconnect()


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(_DB_URL, echo=False)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _factory():
        async with factory() as sess:
            yield sess

    return _factory


@pytest_asyncio.fixture(autouse=True)
async def _clean_trades_table(engine):
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE trades RESTART IDENTITY CASCADE"))
    yield


def _order_manager(broker, session_factory, wait_seconds: float = 5.0) -> OrderManager:
    return OrderManager(
        broker=broker,
        risk_manager=RiskManager(),
        session_factory=session_factory,
        order_retry_attempts=1,
        order_retry_backoff_seconds=0,
        is_market_open=lambda: True,
        stop_cancel_wait_seconds=wait_seconds,
    )


async def _submit_open(
    om: OrderManager,
    broker: MockBroker,
    *,
    quantity: Decimal,
    price: Decimal,
    stop: Decimal,
    partial: Decimal | None = None,
) -> uuid.UUID:
    """Submit a long entry; with `partial` the broker fills only that many shares first."""
    tid = uuid.uuid4()
    broker.last_prices[_SYMBOL] = price
    if partial is not None:
        broker.next_status = "PARTIAL"
        broker.next_filled_quantity = partial
    request = TradeRequest(
        trade_id=tid,
        symbol=_SYMBOL,
        direction="BUY",
        quantity=quantity,
        entry_price=price,
        stop_loss_price=stop,
        account_balance=_BALANCE,
    )
    _, result = await om.submit_order(request)
    assert result.status in ("FILLED", "PARTIAL")
    return tid


async def _trade(session_factory, tid: uuid.UUID) -> Trade:
    async with session_factory() as s:
        return await TradeRepo(s).get_by_id(tid)


async def _trade_columns(session_factory, tid: uuid.UUID) -> dict:
    trade = await _trade(session_factory, tid)
    return {c.name: getattr(trade, c.name) for c in Trade.__table__.columns}


async def _fill_count(session_factory, tid: uuid.UUID) -> int:
    async with session_factory() as s:
        stmt = select(func.count()).select_from(TradeFill).where(TradeFill.trade_id == tid)
        return (await s.execute(stmt)).scalar_one()


async def test_sync_journals_entry_and_stop_and_second_call_adds_nothing(broker, session_factory):
    om = _order_manager(broker, session_factory)
    tid = await _submit_open(om, broker, quantity=Decimal("10"), price=Decimal("10.00"),
                             stop=Decimal("9.50"))
    broker.set_order_state(f"{tid}:stop", "FILLED", Decimal("10"), Decimal("9.50"))

    totals = await om.sync_fills(tid)

    assert totals["ENTRY"].quantity == Decimal("10")
    assert totals["STOP"].quantity == Decimal("10")
    assert await _fill_count(session_factory, tid) == 2

    again = await om.sync_fills(tid)

    assert await _fill_count(session_factory, tid) == 2
    assert again == totals


async def test_more_entry_fills_raise_quantity_and_vwap_price(broker, session_factory):
    om = _order_manager(broker, session_factory)
    tid = await _submit_open(om, broker, quantity=Decimal("10"), price=Decimal("10.00"),
                             stop=Decimal("9.50"), partial=Decimal("4"))
    before = await _trade(session_factory, tid)
    assert before.filled_quantity == Decimal("4")
    await om.sync_fills(tid)

    broker.set_order_state(f"{tid}:entry", "FILLED", Decimal("10"), Decimal("10.20"))
    await om.sync_fills(tid)

    after = await _trade(session_factory, tid)
    assert after.filled_quantity == Decimal("10")
    assert after.entry_fill_price == Decimal("10.12")  # (4 x 10.00 + 6 x 10.20) / 10
    assert after.entry_price == before.entry_price == Decimal("10.00")
    assert after.stop_loss_price == before.stop_loss_price == Decimal("9.50")


async def test_late_entry_commission_is_applied_at_unchanged_quantity(broker, session_factory):
    om = _order_manager(broker, session_factory)
    tid = await _submit_open(om, broker, quantity=Decimal("4"), price=Decimal("10.00"),
                             stop=Decimal("9.50"))
    await om.sync_fills(tid)
    exec_id = broker.executions[-1].exec_id
    async with session_factory() as s:
        journaled = (
            await s.execute(select(TradeFill.commission).where(TradeFill.exec_id == exec_id))
        ).scalar_one()
    assert journaled is None

    broker.set_execution_commission(exec_id, Decimal("1.00"))
    await om.sync_fills(tid)

    trade = await _trade(session_factory, tid)
    assert trade.entry_commission == Decimal("1.00")
    assert trade.filled_quantity == Decimal("4")


async def test_open_quantity_subtracts_stop_and_exit_legs(broker, session_factory):
    om = _order_manager(broker, session_factory)
    tid = await _submit_open(om, broker, quantity=Decimal("10"), price=Decimal("10.00"),
                             stop=Decimal("9.50"))
    broker.set_order_state(f"{tid}:stop", "FILLED", Decimal("3"), Decimal("9.50"))
    broker.set_order_state(f"{tid}:exit", "FILLED", Decimal("2"), Decimal("10.50"))

    totals = await om.sync_fills(tid)
    trade = await _trade(session_factory, tid)

    assert OrderManager.open_quantity(trade, totals) == Decimal("5")


async def test_journal_fills_fills_null_commission_and_changes_no_trade_column(
    broker, session_factory
):
    om = _order_manager(broker, session_factory)
    tid = await _submit_open(om, broker, quantity=Decimal("4"), price=Decimal("10.00"),
                             stop=Decimal("9.50"))
    # The entry path already journaled this fill (commission still None).
    assert await om.journal_fills(tid, [broker.executions[-1]]) == 0
    assert (await om._journal_totals(tid))["ENTRY"].commission == Decimal("0")
    before = await _trade_columns(session_factory, tid)
    priced = broker.executions[-1].model_copy(update={"commission": Decimal("1.00")})

    added = await om.journal_fills(tid, [priced])

    assert added == 0  # the execution was already journaled; only its commission changed
    assert await _trade_columns(session_factory, tid) == before
    totals = await om._journal_totals(tid)
    assert totals["ENTRY"].commission == Decimal("1.00")


async def test_cancel_and_wait_returns_partial_after_cancelling_working_order(
    broker, session_factory
):
    om = _order_manager(broker, session_factory, wait_seconds=2.0)
    stop_ref = f"{uuid.uuid4()}:stop"
    broker.set_order_state(stop_ref, "WORKING", Decimal("3"), Decimal("9.50"))

    result = await om._cancel_and_wait(stop_ref)

    assert result is not None
    assert result.status == "PARTIAL"
    assert result.filled_quantity == Decimal("3")


class _StuckErrorBroker(MockBroker):
    """Reports ERROR for every order and never settles."""

    async def find_order_by_ref(self, order_ref: str) -> OrderResult | None:
        return OrderResult(
            trade_id=uuid.uuid4(),
            broker_order_id="9",
            status="ERROR",
            filled_quantity=Decimal("0"),
            avg_fill_price=Decimal("0"),
            order_ref=order_ref,
            error_message="state unknown",
            timestamp=datetime.now(UTC),
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        return False


async def test_cancel_and_wait_returns_non_terminal_result_after_wait_bound():
    om = OrderManager(
        broker=_StuckErrorBroker(),
        risk_manager=RiskManager(),
        session_factory=None,
        stop_cancel_wait_seconds=0.3,
    )
    loop = asyncio.get_running_loop()

    started = loop.time()
    result = await om._cancel_and_wait("some-trade:stop")
    elapsed = loop.time() - started

    assert result is not None
    assert result.status == "ERROR"
    assert not result.is_terminal
    assert 0.25 <= elapsed < 2.0
