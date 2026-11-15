"""Close path sells only the open quantity and waits for partly filled orders (AR-01)."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import CloseOutcome, OrderManager
from app.core.execution.reconciler import Reconciler
from app.core.risk.manager import RiskManager, TradeRequest
from app.db.models.trade import ExitReason, TradeStatus
from app.db.models.trade_fill import TradeFill
from app.db.repositories.trade_repo import TradeRepo

ENTRY, STOP, QTY, BAL = Decimal("10.00"), Decimal("9.00"), Decimal("10"), Decimal("100000")


class FakeCache:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []
        self.kv: dict[str, str] = {}

    async def publish(self, channel: str, message: str) -> None:
        import json

        self.published.append((channel, json.loads(message)))

    async def get(self, key: str):
        return self.kv.get(key)

    def alerts(self, alert_type: str | None = None) -> list[dict]:
        out = [m["payload"] for c, m in self.published if m["event"] == "system_alert"]
        return [a for a in out if alert_type is None or a["alert_type"] == alert_type]


class SlowStopBroker(MockBroker):
    """After a stop cancel the stop stays WORKING; it turns PARTIAL on the second poll."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False
        self.stop_polls = 0
        self.stop_status_at_exit: str | None = None

    async def cancel_order(self, broker_order_id: str) -> bool:
        self.cancelled = True
        return True

    async def find_order_by_ref(self, order_ref: str):
        if self.cancelled and order_ref.endswith(":stop"):
            self.stop_polls += 1
            if self.stop_polls == 2:
                self.set_order_state(order_ref, "PARTIAL", Decimal("4"), Decimal("9.50"))
        return await super().find_order_by_ref(order_ref)

    async def place_order(self, order):
        if order.order_ref and order.order_ref.endswith(":exit"):
            self.stop_status_at_exit = self.order_results[f"{order.trade_id}:stop"].status
        return await super().place_order(order)


class ErrorAfterCancelBroker(MockBroker):
    """After a stop cancel the stop keeps reporting ERROR."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    async def cancel_order(self, broker_order_id: str) -> bool:
        self.cancelled = True
        return True

    async def find_order_by_ref(self, order_ref: str):
        result = await super().find_order_by_ref(order_ref)
        if self.cancelled and order_ref.endswith(":stop") and result is not None:
            return result.model_copy(update={"status": "ERROR"})
        return result


class EntryFillsOnCancelBroker(MockBroker):
    """Cancelling the entry fills its remainder first, as a fill racing the cancel."""

    async def cancel_order(self, broker_order_id: str) -> bool:
        ref = self._ref_by_order_id.get(broker_order_id)
        if ref and ref.endswith(":entry"):
            self.set_order_state(ref, "FILLED", Decimal("10"), Decimal("10.20"))
            self.set_position("AAPL", Decimal("10"))
            return True
        return await super().cancel_order(broker_order_id)


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
    b = MockBroker()
    await b.connect()
    b.last_prices["AAPL"] = ENTRY
    yield b
    await b.disconnect()


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()


@pytest.fixture
def clock():
    state = {"offset": timedelta(0)}
    fn = lambda: datetime.now(UTC) + state["offset"]  # noqa: E731
    fn.state = state
    return fn


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


def _reconciler(broker, session_factory, manager, cache, clock) -> Reconciler:
    return Reconciler(broker, session_factory, manager, cache, clock=clock, is_market_open=lambda: True)


async def _open_with_stop(broker, session_factory, cache, *, partial_entry: bool = False):
    """Buy 10 at 10.00 (entry commission 1.00) and rest a stop for the filled quantity."""
    manager = _manager(broker, session_factory, cache)
    req = TradeRequest(
        trade_id=uuid.uuid4(), symbol="AAPL", direction="BUY", quantity=QTY,
        entry_price=ENTRY, stop_loss_price=STOP, account_balance=BAL,
    )
    broker.next_commission = Decimal("1.00")
    if partial_entry:
        broker.next_status, broker.next_filled_quantity = "PARTIAL", Decimal("4")
    await manager.submit_order(req)
    return manager, req.trade_id


async def _partial_stop(broker, trade_id) -> None:
    """Stop has filled 4 at 9.50 (commission 1.00); 6 shares remain at the broker."""
    stop_ref = f"{trade_id}:stop"
    broker.set_order_state(stop_ref, "WORKING", Decimal("4"), Decimal("9.50"))
    broker.set_position("AAPL", Decimal("6"))
    stop_exec = next(e for e in await broker.get_executions() if e.order_ref == stop_ref)
    broker.set_execution_commission(stop_exec.exec_id, Decimal("1.00"))


async def _get(session_factory, trade_id):
    async with session_factory() as s:
        return await TradeRepo(s).get_by_id(trade_id)


async def _fill_rows(session_factory, trade_id) -> list[TradeFill]:
    async with session_factory() as s:
        return list((await s.execute(select(TradeFill).where(TradeFill.trade_id == trade_id))).scalars())


async def test_full_close_sells_only_the_open_six_after_partial_stop(broker, session_factory, cache):
    manager, tid = await _open_with_stop(broker, session_factory, cache)
    await _partial_stop(broker, tid)
    broker.last_prices["AAPL"] = Decimal("9.40")
    broker.next_commission = Decimal("1.00")

    assert await manager.close_position(tid, "STOP_LOSS") == CloseOutcome.CLOSED

    sells = [o for o in broker.placed_orders if o.direction == "SELL" and o.order_type == "MKT"]
    assert len(sells) == 1
    assert sells[0].quantity == Decimal("6") and sells[0].order_ref == f"{tid}:exit"
    trade = await _get(session_factory, tid)
    assert trade.status == TradeStatus.CLOSED and trade.exit_reason == ExitReason.STOP_LOSS
    assert trade.exit_price == Decimal("9.44")
    assert trade.exit_commission == Decimal("2.00")
    assert trade.pnl == Decimal("-8.60")
    assert len(await _fill_rows(session_factory, tid)) == 3
    assert await broker.get_positions() == []


async def test_retry_after_partial_exit_sells_only_the_remaining_three(
    broker, session_factory, cache, clock
):
    manager, tid = await _open_with_stop(broker, session_factory, cache)
    await _partial_stop(broker, tid)
    broker.last_prices["AAPL"] = Decimal("9.40")
    broker.next_status, broker.next_filled_quantity = "PARTIAL", Decimal("3")
    broker.next_commission = Decimal("1.00")

    assert await manager.close_position(tid, "STOP_LOSS") == CloseOutcome.FAILED
    assert (await _get(session_factory, tid)).status == TradeStatus.CLOSING
    assert cache.alerts("CLOSE_PARTIAL")

    broker.clear_executions()
    broker.last_prices["AAPL"] = Decimal("9.30")
    broker.next_commission = Decimal("1.00")
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    await rec.run_once()  # first sight of CLOSING starts the timer
    clock.state["offset"] = timedelta(minutes=3)
    report = await rec.run_once()

    exits = [o for o in broker.placed_orders if o.order_ref == f"{tid}:exit"]
    assert report.counts["close_retried"] == 1
    assert [o.quantity for o in exits] == [Decimal("6"), Decimal("3")]
    trade = await _get(session_factory, tid)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_price == Decimal("9.41")
    assert trade.exit_commission == Decimal("3.00")
    assert trade.pnl == Decimal("-9.90")
    assert await broker.get_positions() == []


async def test_close_waits_for_partly_filled_stop_before_selling(session_factory, cache):
    slow = SlowStopBroker()
    await slow.connect()
    try:
        slow.last_prices["AAPL"] = ENTRY
        manager, tid = await _open_with_stop(slow, session_factory, cache)
        await _partial_stop(slow, tid)
        slow.last_prices["AAPL"] = Decimal("9.40")

        assert await manager.close_position(tid, "STOP_LOSS") == CloseOutcome.CLOSED

        assert slow.stop_polls >= 2
        assert slow.stop_status_at_exit == "PARTIAL"
        exits = [o for o in slow.placed_orders if o.order_ref == f"{tid}:exit"]
        assert [o.quantity for o in exits] == [Decimal("6")]
        assert (await _get(session_factory, tid)).status == TradeStatus.CLOSED
    finally:
        await slow.disconnect()


async def test_stop_still_erroring_after_cancel_fails_without_selling(session_factory, cache):
    err = ErrorAfterCancelBroker()
    await err.connect()
    try:
        err.last_prices["AAPL"] = ENTRY
        manager, tid = await _open_with_stop(err, session_factory, cache)

        assert await manager.close_position(tid, "STOP_LOSS") == CloseOutcome.FAILED

        assert not [o for o in err.placed_orders if o.direction == "SELL" and o.order_type == "MKT"]
        assert (await _get(session_factory, tid)).status == TradeStatus.CLOSING
        assert cache.alerts("CLOSE_FAILED")
    finally:
        await err.disconnect()


async def test_entry_that_fills_during_cancel_wait_is_sold_in_full(session_factory, cache):
    grow = EntryFillsOnCancelBroker()
    await grow.connect()
    try:
        grow.last_prices["AAPL"] = ENTRY
        manager, tid = await _open_with_stop(grow, session_factory, cache, partial_entry=True)
        grow.set_order_state(f"{tid}:entry", "WORKING", Decimal("4"), ENTRY)
        grow.set_position("AAPL", Decimal("4"))
        assert (await _get(session_factory, tid)).filled_quantity == Decimal("4")

        assert await manager.close_position(tid, "MANUAL") == CloseOutcome.CLOSED

        sells = [o for o in grow.placed_orders if o.direction == "SELL" and o.order_type == "MKT"]
        assert [o.quantity for o in sells] == [Decimal("10")]
        trade = await _get(session_factory, tid)
        assert trade.status == TradeStatus.CLOSED
        assert trade.filled_quantity == Decimal("10")
        assert await grow.get_positions() == []
    finally:
        await grow.disconnect()
