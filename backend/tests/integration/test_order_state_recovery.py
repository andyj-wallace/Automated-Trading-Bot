"""Live broker orders are found and cancelled by ref, so a stale or missing saved id never leaves one live."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.brokers.base import OrderRequest
from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import CloseOutcome, OrderManager
from app.core.risk.manager import RiskManager, TradeRequest
from app.db.models.trade import TradeStatus
from tests.integration import test_reconciler as base
from tests.integration import test_reconciler_finalize as fin

# Fixtures and helpers shared with the reconciler tests, bound here so pytest registers the fixtures.
broker = base.broker
cache = base.cache
clock = base.clock
db_engine = base.db_engine
session_factory = base.session_factory
stubborn_broker = fin.stubborn_broker
_get = base._get
_reconciler = base._reconciler
_set_stop = fin._set_stop
_trade = fin._trade

QTY = Decimal("10")
ENTRY = Decimal("10.00")


class FlakyExitBroker(MockBroker):
    """MockBroker whose next exit submission raises before reaching the broker."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_exit = False

    async def place_order(self, order: OrderRequest):
        if self.fail_next_exit and order.order_ref.endswith(":exit"):
            self.fail_next_exit = False
            raise ConnectionError("simulated submit failure")
        return await super().place_order(order)


class ExactCancelBroker(MockBroker):
    """Like IBKR, a cancel acts only on the order with that exact id, never on a newer one."""

    async def cancel_order(self, broker_order_id: str) -> bool:
        ref = self._ref_by_order_id.get(broker_order_id)
        current = self.order_results.get(ref) if ref else None
        if current is None or current.broker_order_id != broker_order_id:
            return False
        return await super().cancel_order(broker_order_id)


@pytest.fixture
async def exact_broker():
    b = ExactCancelBroker()
    await b.connect()
    b.last_prices["AAPL"] = ENTRY
    yield b
    await b.disconnect()


@pytest.fixture
async def flaky_exit_broker():
    b = FlakyExitBroker()
    await b.connect()
    b.last_prices["AAPL"] = ENTRY
    yield b
    await b.disconnect()


def _manager(broker, session_factory, cache, *, attempts: int = 1) -> OrderManager:
    return OrderManager(
        broker,
        RiskManager(),
        session_factory,
        cache,
        order_retry_attempts=attempts,
        order_retry_backoff_seconds=0,
        is_market_open=lambda: True,
        protective_stops=True,
        stop_cancel_wait_seconds=0.5,
    )


async def _open_buy_ten(manager) -> uuid.UUID:
    """Buy 10 at 10.00; the manager rests a protective stop for 10 at the broker."""
    req = TradeRequest(
        trade_id=uuid.uuid4(), symbol="AAPL", direction="BUY", quantity=QTY,
        entry_price=ENTRY, stop_loss_price=Decimal("9.50"), account_balance=base.BAL,
    )
    await manager.submit_order(req)
    return req.trade_id


async def _replace_stop_at_broker(broker, trade_id: uuid.UUID) -> str:
    """A replacement stop goes live at the broker under the same ref; returns its order id."""
    stop_ref = f"{trade_id}:stop"
    await broker.cancel_order(broker.order_results[stop_ref].broker_order_id)
    result = await broker.place_order(
        OrderRequest(
            trade_id=trade_id, symbol="AAPL", direction="SELL", quantity=QTY,
            order_type="STP", stop_price=Decimal("9.50"), tif="GTC", order_ref=stop_ref,
        )
    )
    return result.broker_order_id


async def test_close_cancels_live_stop_when_saved_stop_id_is_stale(
    exact_broker, session_factory, cache
):
    broker = exact_broker
    manager = _manager(broker, session_factory, cache)
    tid = await _open_buy_ten(manager)
    old_id = (await _get(session_factory, tid)).stop_order_id
    live_id = await _replace_stop_at_broker(broker, tid)
    assert live_id != old_id  # the database still holds the cancelled stop's id

    assert await manager.close_position(tid, "MANUAL") == CloseOutcome.CLOSED

    assert broker.order_results[f"{tid}:stop"].status != "WORKING"
    assert await broker.get_positions() == []


async def test_close_cancels_live_stop_when_no_stop_id_was_saved(broker, session_factory, cache):
    manager = _manager(broker, session_factory, cache)
    tid = await _open_buy_ten(manager)
    await _set_stop(session_factory, tid, None)  # the stop's id write failed after the broker ack

    assert await manager.close_position(tid, "MANUAL") == CloseOutcome.CLOSED

    assert broker.order_results[f"{tid}:stop"].status != "WORKING"


async def test_flat_finalize_cancels_a_working_exit_before_closing(
    stubborn_broker, session_factory, cache, clock
):
    broker = stubborn_broker
    tid = await _trade(session_factory, TradeStatus.CLOSING)
    broker.next_status, broker.next_filled_quantity = "WORKING", Decimal("4")
    exit_order = await broker.place_order(
        OrderRequest(
            trade_id=tid, symbol="AAPL", direction="SELL", quantity=QTY,
            order_type="MKT", order_ref=f"{tid}:exit",
        )
    )
    broker.set_position("AAPL", Decimal("0"))  # the other 6 shares were sold outside the bot
    manager = fin._manager(broker, session_factory, cache)
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    clock.state["offset"] = timedelta(minutes=2)
    await rec.run_once()  # starts the CLOSING timer
    clock.state["offset"] = timedelta(minutes=6)
    await rec.run_once()  # first flat run only arms the confirmation

    report = await rec.run_once()  # the exit cancel is accepted but the order stays live

    assert (await _get(session_factory, tid)).status == TradeStatus.CLOSING
    assert report.counts["finalize_exit_live"] == 1
    assert exit_order.broker_order_id in broker.cancel_requests
    assert len(cache.alerts("CLOSE_FAILED")) == 1

    broker.cancel_works = True
    await rec.run_once()  # the cancel now sticks: the trade can be finalized

    assert (await _get(session_factory, tid)).status == TradeStatus.CLOSED
    assert broker.order_results[f"{tid}:exit"].status != "WORKING"


async def test_exit_retry_never_adopts_the_previous_partial_exit(
    flaky_exit_broker, session_factory, cache
):
    broker = flaky_exit_broker
    manager = _manager(broker, session_factory, cache, attempts=2)
    tid = await _open_buy_ten(manager)
    broker.next_status, broker.next_filled_quantity = "PARTIAL", Decimal("4")
    assert await manager.close_position(tid, "MANUAL") == CloseOutcome.FAILED
    assert (await _get(session_factory, tid)).status == TradeStatus.CLOSING

    broker.fail_next_exit = True
    outcome = await manager.close_position(tid, "MANUAL", allow_from_closing=True)

    exits = [o.quantity for o in broker.placed_orders if o.order_ref == f"{tid}:exit"]
    assert exits == [QTY, Decimal("6")]
    assert outcome == CloseOutcome.CLOSED
    assert await broker.get_positions() == []
