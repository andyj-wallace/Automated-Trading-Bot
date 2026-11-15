"""finalize_close computes exit facts from the fill journal (exit VWAP, exit fees, P&L)."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.base import Execution, OrderResult
from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.risk.manager import RiskManager, TradeRequest
from app.db.models.trade import ExitReason, Trade, TradeStatus
from app.db.repositories.fill_repo import FillRepo
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


def _order_manager(broker, session_factory) -> OrderManager:
    return OrderManager(
        broker=broker,
        risk_manager=RiskManager(),
        session_factory=session_factory,
        order_retry_attempts=1,
        order_retry_backoff_seconds=0,
        is_market_open=lambda: True,
        stop_cancel_wait_seconds=1.0,
    )


async def _open_trade_at_10(om: OrderManager, broker: MockBroker, tid: uuid.UUID) -> None:
    """Fill a 10-share long at 10.00 and journal its entry (commission None until set)."""
    broker.last_prices[_SYMBOL] = Decimal("10.00")
    request = TradeRequest(
        trade_id=tid,
        symbol=_SYMBOL,
        direction="BUY",
        quantity=Decimal("10"),
        entry_price=Decimal("10.00"),
        stop_loss_price=Decimal("9.50"),
        account_balance=_BALANCE,
    )
    _, result = await om.submit_order(request)
    assert result.status == "FILLED"


async def _set_entry_commission(
    om: OrderManager, broker: MockBroker, tid: uuid.UUID, commission: Decimal
) -> None:
    """Give the entry execution its commission and sync so the trade row picks it up."""
    broker.set_execution_commission(broker.executions[-1].exec_id, commission)
    await om.sync_fills(tid)


async def _begin_closing(session_factory, tid: uuid.UUID) -> None:
    async with session_factory() as s:
        await TradeRepo(s).begin_close(tid, ExitReason.STOP_LOSS)
        await s.commit()


def _leg(tid: uuid.UUID, leg: str, n: int, side: str, qty: str, price: str,
         commission: str | None) -> Execution:
    return Execution(
        exec_id=f"{tid}-{leg}-{n}",
        order_ref=f"{tid}:{leg}",
        broker_order_id=f"broker-{leg}",
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        commission=Decimal(commission) if commission is not None else None,
        executed_at=datetime.now(UTC),
    )


async def _journal(session_factory, tid: uuid.UUID, executions: list[Execution]) -> None:
    async with session_factory() as s:
        await FillRepo(s).record(tid, executions)
        await s.commit()


async def _trade(session_factory, tid: uuid.UUID) -> Trade:
    async with session_factory() as s:
        return await TradeRepo(s).get_by_id(tid)


async def _closing_trade(om, broker, session_factory, tid: uuid.UUID) -> None:
    """A 10-share long at 10.00 with entry commission 1.00, moved to CLOSING."""
    await _open_trade_at_10(om, broker, tid)
    await om.sync_fills(tid)
    await _set_entry_commission(om, broker, tid, Decimal("1.00"))
    await _begin_closing(session_factory, tid)


@pytest.mark.parametrize(
    ("legs", "fallback", "exit_price", "exit_commission", "pnl"),
    [
        # STOP 4 at 9.50 + EXIT 6 at 9.40, 1.00 commission each
        (
            [("stop", 1, "SELL", "4", "9.50", "1.00"), ("exit", 1, "SELL", "6", "9.40", "1.00")],
            "9.00",
            Decimal("9.44"),
            Decimal("2.00"),
            Decimal("-8.60"),
        ),
        # STOP 4 at 9.50 + EXIT 3 at 9.40 + EXIT 3 at 9.30, 1.00 commission each
        (
            [
                ("stop", 1, "SELL", "4", "9.50", "1.00"),
                ("exit", 1, "SELL", "3", "9.40", "1.00"),
                ("exit", 2, "SELL", "3", "9.30", "1.00"),
            ],
            "9.00",
            Decimal("9.41"),
            Decimal("3.00"),
            Decimal("-9.90"),
        ),
        # Only STOP 4 at 9.50; the 6 unsold shares are valued at the fallback 9.00
        (
            [("stop", 1, "SELL", "4", "9.50", "1.00")],
            "9.00",
            Decimal("9.20"),
            Decimal("1.00"),
            Decimal("-10.00"),
        ),
    ],
)
async def test_journal_worked_examples(
    broker, session_factory, legs, fallback, exit_price, exit_commission, pnl
):
    om = _order_manager(broker, session_factory)
    tid = uuid.uuid4()
    await _closing_trade(om, broker, session_factory, tid)
    await _journal(
        session_factory,
        tid,
        [_leg(tid, leg, n, side, qty, px, comm) for leg, n, side, qty, px, comm in legs],
    )

    assert await om.finalize_close(tid, ExitReason.STOP_LOSS, None, Decimal(fallback))

    trade = await _trade(session_factory, tid)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_price == exit_price
    assert trade.exit_commission == exit_commission
    assert trade.pnl == pnl


async def test_second_finalize_close_returns_false_and_keeps_pnl(broker, session_factory):
    om = _order_manager(broker, session_factory)
    tid = uuid.uuid4()
    await _closing_trade(om, broker, session_factory, tid)
    await _journal(
        session_factory,
        tid,
        [
            _leg(tid, "stop", 1, "SELL", "4", "9.50", "1.00"),
            _leg(tid, "exit", 1, "SELL", "6", "9.40", "1.00"),
        ],
    )
    assert await om.finalize_close(tid, ExitReason.STOP_LOSS, None, Decimal("9.00"))

    fill = OrderResult(
        trade_id=tid, broker_order_id="late", status="FILLED", filled_quantity=Decimal("10"),
        avg_fill_price=Decimal("1.00"), commission=Decimal("0"), timestamp=datetime.now(UTC),
    )
    assert not await om.finalize_close(tid, ExitReason.STOP_LOSS, fill, Decimal("1.00"))

    trade = await _trade(session_factory, tid)
    assert trade.pnl == Decimal("-8.60")
    assert trade.exit_price == Decimal("9.44")


async def test_no_journal_path_still_uses_fill(broker, session_factory):
    om = _order_manager(broker, session_factory)
    tid = uuid.uuid4()
    await _closing_trade(om, broker, session_factory, tid)
    fill = OrderResult(
        trade_id=tid, broker_order_id="exit-1", status="FILLED", filled_quantity=Decimal("10"),
        avg_fill_price=Decimal("10.20"), commission=Decimal("0.50"), timestamp=datetime.now(UTC),
    )

    assert await om.finalize_close(tid, ExitReason.TAKE_PROFIT, fill, Decimal("1.00"))

    trade = await _trade(session_factory, tid)
    assert trade.exit_price == Decimal("10.20")
    assert trade.exit_commission == Decimal("0.50")
    assert trade.pnl == Decimal("0.50")  # (10.20 - 10.00) * 10 - 1.00 - 0.50


async def test_late_entry_commission_is_included_in_final_pnl(broker, session_factory):
    om = _order_manager(broker, session_factory)
    tid = uuid.uuid4()
    await _open_trade_at_10(om, broker, tid)
    await om.sync_fills(tid)
    entry_exec_id = broker.executions[-1].exec_id
    async with session_factory() as s:
        trade = await TradeRepo(s).get_by_id(tid)
    assert trade.entry_commission == Decimal("0")  # the unpriced entry leg counts as no fee yet

    broker.set_execution_commission(entry_exec_id, Decimal("1.00"))
    await om.sync_fills(tid)
    await _begin_closing(session_factory, tid)
    await _journal(
        session_factory,
        tid,
        [
            _leg(tid, "stop", 1, "SELL", "4", "9.50", "1.00"),
            _leg(tid, "exit", 1, "SELL", "6", "9.40", "1.00"),
        ],
    )

    assert await om.finalize_close(tid, ExitReason.STOP_LOSS, None, Decimal("9.00"))

    trade = await _trade(session_factory, tid)
    assert trade.entry_commission == Decimal("1.00")
    assert trade.pnl == Decimal("-8.60")
