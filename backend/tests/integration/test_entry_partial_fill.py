"""Entry path journals fills and finishes partly filled entries (AR-02)."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.base import Execution
from app.brokers.mock.client import MockBroker
from app.core.execution import order_entry
from app.core.execution.order_manager import OrderManager
from app.core.risk.manager import RiskManager, TradeRequest
from app.db.models.trade import TradeStatus
from app.db.models.trade_fill import TradeFill
from app.db.repositories.trade_repo import TradeRepo

ENTRY, STOP, QTY, BAL = Decimal("10.00"), Decimal("9.00"), Decimal("10"), Decimal("100000")


class FakeCache:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel: str, message: str) -> None:
        import json

        self.published.append((channel, json.loads(message)))

    def alerts(self, alert_type: str | None = None) -> list[dict]:
        out = [m["payload"] for c, m in self.published if m["event"] == "system_alert"]
        return [a for a in out if alert_type is None or a["alert_type"] == alert_type]


class RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[tuple[str, dict]] = []

    def error(self, message: str, **kwargs) -> None:
        self.errors.append((message, kwargs.get("extra", {})))


class NoConfirmCancelBroker(MockBroker):
    """Cancel is acknowledged but the entry stays WORKING at the broker."""

    async def cancel_order(self, broker_order_id: str) -> bool:
        return True


class ExtraExecutionBroker(MockBroker):
    """Cancel leaves the entry PARTIAL with 4; executions also report 6 more at 10.20."""

    def __init__(self) -> None:
        super().__init__()
        self.extra: list[Execution] = []

    async def cancel_order(self, broker_order_id: str) -> bool:
        ref = self._ref_by_order_id[broker_order_id]
        result = await super().cancel_order(broker_order_id)
        self.extra.append(
            Execution(
                exec_id="mock-extra-1",
                order_ref=ref,
                broker_order_id=broker_order_id,
                side="BUY",
                quantity=Decimal("6"),
                price=Decimal("10.20"),
                executed_at=datetime.now(UTC),
            )
        )
        return result

    async def get_executions(self) -> list[Execution]:
        return await super().get_executions() + self.extra


class FailingExecutionsBroker(MockBroker):
    """The broker cannot list executions."""

    async def get_executions(self) -> list[Execution]:
        raise RuntimeError("executions unavailable")


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


async def _submit_partial_entry(broker, session_factory, cache):
    """Submit a 10-share buy at 10.00 whose broker result is WORKING with 4 filled."""
    manager = _manager(broker, session_factory, cache)
    req = TradeRequest(
        trade_id=uuid.uuid4(), symbol="AAPL", direction="BUY", quantity=QTY,
        entry_price=ENTRY, stop_loss_price=STOP, account_balance=BAL,
    )
    broker.next_status, broker.next_filled_quantity = "WORKING", Decimal("4")
    await manager.submit_order(req)
    return req.trade_id


async def _get(session_factory, trade_id):
    async with session_factory() as s:
        return await TradeRepo(s).get_by_id(trade_id)


async def _entry_fill_rows(session_factory, trade_id) -> list[TradeFill]:
    async with session_factory() as s:
        rows = await s.execute(select(TradeFill).where(TradeFill.trade_id == trade_id))
        return [r for r in rows.scalars() if r.leg == "ENTRY"]


def _stops(broker) -> list:
    return [o for o in broker.placed_orders if o.order_type == "STP"]


async def test_cancel_confirmed_opens_with_fills_and_one_stop(broker, session_factory, cache):
    tid = await _submit_partial_entry(broker, session_factory, cache)

    trade = await _get(session_factory, tid)
    assert trade.status == TradeStatus.OPEN
    assert trade.filled_quantity == Decimal("4")
    assert [o.quantity for o in _stops(broker)] == [4]
    assert len(await _entry_fill_rows(session_factory, tid)) == 1
    assert cache.alerts("ENTRY_REMAINDER_WORKING") == []


async def test_cancel_not_confirmed_opens_with_fills_and_alerts(session_factory, cache):
    broker = NoConfirmCancelBroker()
    await broker.connect()
    try:
        broker.last_prices["AAPL"] = ENTRY
        tid = await _submit_partial_entry(broker, session_factory, cache)

        trade = await _get(session_factory, tid)
        assert trade.status == TradeStatus.OPEN
        assert trade.filled_quantity == Decimal("4")
        assert [o.quantity for o in _stops(broker)] == [4]
        alerts = cache.alerts("ENTRY_REMAINDER_WORKING")
        assert len(alerts) == 1
        assert alerts[0]["trade_id"] == str(tid)
        assert alerts[0]["symbol"] == "AAPL"
        assert alerts[0]["filled_quantity"] == "4"
        assert alerts[0]["ordered_quantity"] == "10"
    finally:
        await broker.disconnect()


async def test_executions_ahead_of_result_are_journaled_before_the_stop(session_factory, cache):
    broker = ExtraExecutionBroker()
    await broker.connect()
    try:
        broker.last_prices["AAPL"] = ENTRY
        tid = await _submit_partial_entry(broker, session_factory, cache)

        trade = await _get(session_factory, tid)
        assert trade.status == TradeStatus.OPEN
        assert trade.filled_quantity == Decimal("10")
        assert trade.entry_fill_price == Decimal("10.12")
        assert [o.quantity for o in _stops(broker)] == [10]
        assert len(await _entry_fill_rows(session_factory, tid)) == 2
    finally:
        await broker.disconnect()


async def test_sync_failure_still_places_stop_and_logs(session_factory, cache, monkeypatch):
    recorder = RecordingLogger()
    monkeypatch.setattr(order_entry, "error_logger", recorder)
    broker = FailingExecutionsBroker()
    await broker.connect()
    try:
        broker.last_prices["AAPL"] = ENTRY
        tid = await _submit_partial_entry(broker, session_factory, cache)

        trade = await _get(session_factory, tid)
        assert trade.status == TradeStatus.OPEN
        assert trade.filled_quantity == Decimal("4")
        assert [o.quantity for o in _stops(broker)] == [4]
        assert any(
            extra.get("trade_id") == str(tid) and "executions unavailable" in extra.get("error", "")
            for _, extra in recorder.errors
        )
    finally:
        await broker.disconnect()
