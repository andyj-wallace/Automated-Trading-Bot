"""Reconciler finalization from the fill journal, against MockBroker and the test database."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.risk.manager import RiskManager
from app.db.models.trade import ExitReason, TradeDirection, TradeStatus
from app.db.repositories.fill_repo import FillRepo
from app.db.repositories.trade_repo import TradeRepo
from tests.integration import test_reconciler as base

# Fixtures and helpers shared with test_reconciler.py, bound here so pytest registers the fixtures.
_get = base._get
_reconciler = base._reconciler
broker = base.broker
cache = base.cache
clock = base.clock
db_engine = base.db_engine
session_factory = base.session_factory

QTY = Decimal("10")
ENTRY_PRICE = Decimal("10.00")


class KeepWorkingBroker(MockBroker):
    """MockBroker that records cancel requests and leaves the order WORKING."""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_requests: list[str] = []

    async def cancel_order(self, broker_order_id: str) -> bool:
        self.cancel_requests.append(broker_order_id)
        return True


@pytest.fixture
async def keep_broker():
    b = KeepWorkingBroker()
    await b.connect()
    b.last_prices["AAPL"] = ENTRY_PRICE
    yield b
    await b.disconnect()


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


async def _trade(session_factory, status: TradeStatus, filled: Decimal = QTY) -> uuid.UUID:
    """Insert a 10-share BUY at 10.00 in the given status with `filled` shares of entry recorded."""
    tid = uuid.uuid4()
    async with session_factory() as s:
        repo = TradeRepo(s)
        await repo.create(
            trade_id=tid, symbol="AAPL", direction=TradeDirection.BUY, quantity=QTY,
            entry_price=ENTRY_PRICE, stop_loss_price=Decimal("9.50"),
            take_profit_price=Decimal("10.50"), reward_to_risk_ratio=Decimal("2"),
            risk_amount=Decimal("5"), account_balance_at_entry=base.BAL,
        )
        await repo.update_status(tid, status)
        await repo.record_entry_fill(tid, "1", ENTRY_PRICE, filled)
        if status == TradeStatus.CLOSING:
            await repo.begin_close(tid, ExitReason.STOP_LOSS, allow_from_closing=True)
        await s.commit()
    return tid


async def test_incomplete_exit_fills_book_missing_shares_at_cached_price(
    broker, session_factory, cache, clock
):
    tid = await _trade(session_factory, TradeStatus.OPEN)
    broker.set_order_state(f"{tid}:stop", "PARTIAL", Decimal("4"), Decimal("9.50"))
    cache.kv["price:AAPL"] = '{"price": "9.00", "timestamp": "x"}'
    clock.state["offset"] = timedelta(minutes=2)
    rec = _reconciler(broker, session_factory, _manager(broker, session_factory, cache), cache, clock)

    await rec.run_once()  # first flat run only arms the confirmation
    report = await rec.run_once()

    trade = await _get(session_factory, tid)
    assert trade.status == TradeStatus.CLOSED and trade.exit_reason == ExitReason.STOP_LOSS
    assert trade.exit_price == Decimal("9.20")  # (4 x 9.50 + 6 x 9.00) / 10
    assert trade.pnl == Decimal("-8")  # 92 received - 100 paid
    assert report.counts["closed_stop_fill"] == 1
    alerts = cache.alerts("EXIT_FILLS_MISSING")
    assert len(alerts) == 1
    assert alerts[0]["trade_id"] == str(tid) and alerts[0]["symbol"] == "AAPL"
    assert Decimal(alerts[0]["unaccounted_quantity"]) == Decimal("6")


async def test_restart_books_journaled_stop_fill_from_database(broker, session_factory, cache, clock):
    tid = await _trade(session_factory, TradeStatus.OPEN)
    broker.set_order_state(f"{tid}:stop", "PARTIAL", Decimal("4"), Decimal("9.50"))
    cache.kv["price:AAPL"] = '{"price": "9.00", "timestamp": "x"}'
    clock.state["offset"] = timedelta(minutes=2)
    before_restart = _manager(broker, session_factory, cache)
    await before_restart.journal_fills(tid, await broker.get_executions())

    # The app restarts: new manager and reconciler, and the broker no longer reports the fill.
    broker.clear_executions()
    after_restart = _manager(broker, session_factory, cache)
    rec = _reconciler(broker, session_factory, after_restart, cache, clock)
    await rec.run_once()
    await rec.run_once()

    trade = await _get(session_factory, tid)
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_price == Decimal("9.20")
    assert trade.pnl == Decimal("-8")


async def test_working_entry_blocks_finalization_until_it_is_terminal(
    keep_broker, session_factory, cache, clock
):
    broker = keep_broker
    tid = await _trade(session_factory, TradeStatus.CLOSING, filled=Decimal("4"))
    broker.set_order_state(f"{tid}:entry", "WORKING", Decimal("4"), ENTRY_PRICE, broker_order_id="55")
    broker.set_order_state(f"{tid}:stop", "PARTIAL", Decimal("4"), Decimal("9.50"))
    manager = _manager(broker, session_factory, cache)
    rec = _reconciler(broker, session_factory, manager, cache, clock)

    await rec.run_once()  # starts the CLOSING timer
    clock.state["offset"] = timedelta(minutes=3)
    await rec.run_once()  # first flat run
    report = await rec.run_once()  # second flat run: finalization is refused

    trade = await _get(session_factory, tid)
    assert trade.status == TradeStatus.CLOSING
    assert report.counts["finalize_waiting_entry"] == 1
    assert broker.cancel_requests == ["55"]

    # The rest of the entry fills; the broker holds the 6 shares and the retry sells exactly those.
    broker.set_order_state(f"{tid}:entry", "FILLED", QTY, ENTRY_PRICE)
    broker.set_position("AAPL", Decimal("6"))
    report = await rec.run_once()

    trade = await _get(session_factory, tid)
    assert report.counts["close_retried"] == 1
    assert trade.status == TradeStatus.CLOSED
    async with session_factory() as s:
        totals = await FillRepo(s).totals(tid)
    assert totals["ENTRY"].quantity == QTY
    assert totals["STOP"].quantity == Decimal("4")
    assert totals["EXIT"].quantity == Decimal("6")


async def test_error_entry_blocks_finalization(keep_broker, session_factory, cache, clock):
    broker = keep_broker
    tid = await _trade(session_factory, TradeStatus.CLOSING, filled=Decimal("4"))
    broker.set_order_state(f"{tid}:entry", "ERROR", Decimal("4"), ENTRY_PRICE, broker_order_id="55")
    broker.set_order_state(f"{tid}:stop", "PARTIAL", Decimal("4"), Decimal("9.50"))
    rec = _reconciler(broker, session_factory, _manager(broker, session_factory, cache), cache, clock)

    await rec.run_once()
    clock.state["offset"] = timedelta(minutes=3)
    await rec.run_once()
    report = await rec.run_once()

    trade = await _get(session_factory, tid)
    assert trade.status == TradeStatus.CLOSING
    assert report.counts["finalize_waiting_entry"] == 1
    assert broker.cancel_requests == []
