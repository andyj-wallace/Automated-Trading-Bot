"""C5 reconciler rules and C6 broker protective stop, against MockBroker and the test database."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import CloseOutcome, OrderManager
from app.core.execution.reconciler import Reconciler
from app.core.risk.manager import RiskManager, TradeRequest
from app.db.models.trade import ExitReason, TradeDirection, TradeStatus
from app.db.repositories.trade_repo import TradeRepo

ENTRY, STOP, QTY, BAL = Decimal("200"), Decimal("195"), Decimal("10"), Decimal("100000")


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

    def events(self, name: str) -> list[dict]:
        return [m["payload"] for _, m in self.published if m["event"] == name]


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


def _manager(broker, session_factory, cache, **kw) -> OrderManager:
    return OrderManager(
        broker,
        RiskManager(),
        session_factory,
        cache,
        order_retry_attempts=1,
        order_retry_backoff_seconds=0,
        is_market_open=lambda: True,
        protective_stops=kw.pop("protective_stops", True),
        stop_cancel_wait_seconds=0,
        **kw,
    )


def _reconciler(broker, session_factory, manager, cache, clock) -> Reconciler:
    return Reconciler(broker, session_factory, manager, cache, clock=clock, is_market_open=lambda: True)


def _request(symbol: str = "AAPL") -> TradeRequest:
    return TradeRequest(
        trade_id=uuid.uuid4(), symbol=symbol, direction="BUY", quantity=QTY,
        entry_price=ENTRY, stop_loss_price=STOP, account_balance=BAL,
    )


async def _get(session_factory, trade_id):
    async with session_factory() as s:
        return await TradeRepo(s).get_by_id(trade_id)


async def _insert(session_factory, status: TradeStatus, symbol: str = "AAPL", **extra):
    """Insert a trade row directly in a given status."""
    tid = uuid.uuid4()
    async with session_factory() as s:
        repo = TradeRepo(s)
        await repo.create(
            trade_id=tid, symbol=symbol, direction=TradeDirection.BUY, quantity=QTY,
            entry_price=ENTRY, stop_loss_price=STOP, take_profit_price=Decimal("210"),
            reward_to_risk_ratio=Decimal("2"), risk_amount=Decimal("50"),
            account_balance_at_entry=BAL,
        )
        await repo.update_status(tid, status)
        if status in (TradeStatus.OPEN, TradeStatus.CLOSING):
            await repo.record_entry_fill(tid, "1", ENTRY, QTY)
        if status == TradeStatus.CLOSING:
            await repo.begin_close(tid, extra.get("reason", ExitReason.STOP_LOSS), allow_from_closing=True)
        await s.commit()
    return tid


# ---------------------------------------------------------------------------
# C6: protective stop
# ---------------------------------------------------------------------------


async def test_entry_places_exactly_one_stop_with_filled_quantity(broker, session_factory, cache):
    manager = _manager(broker, session_factory, cache)
    req = _request()
    await manager.submit_order(req)

    stops = [o for o in broker.placed_orders if o.order_type == "STP"]
    assert len(stops) == 1
    assert stops[0].quantity == QTY and stops[0].stop_price == STOP
    assert stops[0].tif == "GTC" and stops[0].direction == "SELL"
    assert stops[0].order_ref == f"{req.trade_id}:stop"
    trade = await _get(session_factory, req.trade_id)
    assert trade.status == TradeStatus.OPEN and trade.stop_order_id is not None
    assert trade.current_stop_price == STOP
    assert not [o for o in broker.placed_orders if o.order_type == "LMT"]  # no take-profit order


async def test_partial_entry_stop_uses_filled_quantity(broker, session_factory, cache):
    manager = _manager(broker, session_factory, cache)
    req = _request()
    broker.next_status, broker.next_filled_quantity = "PARTIAL", Decimal("4")
    await manager.submit_order(req)
    stop = next(o for o in broker.placed_orders if o.order_type == "STP")
    assert stop.quantity == Decimal("4")


async def test_stops_disabled_places_nothing(broker, session_factory, cache):
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    await manager.submit_order(_request())
    assert not [o for o in broker.placed_orders if o.order_type == "STP"]


async def test_stop_failure_alerts_and_trade_stays_open(broker, session_factory, cache):
    manager = _manager(broker, session_factory, cache)
    real = broker.place_order

    async def flaky(order):
        if order.order_type == "STP":
            raise ConnectionError("down")
        return await real(order)

    broker.place_order = flaky
    req = _request()
    await manager.submit_order(req)
    trade = await _get(session_factory, req.trade_id)
    assert trade.status == TradeStatus.OPEN and trade.stop_order_id is None
    assert len(cache.alerts("PROTECTIVE_STOP_FAILED")) == 1


async def test_manual_close_cancels_stop_then_sells(broker, session_factory, cache):
    manager = _manager(broker, session_factory, cache)
    req = _request()
    await manager.submit_order(req)

    outcome = await manager.close_position(req.trade_id, "MANUAL")

    assert outcome == CloseOutcome.CLOSED
    stop_state = await broker.find_order_by_ref(f"{req.trade_id}:stop")
    assert stop_state.status == "REJECTED"
    exits = [o for o in broker.placed_orders if o.order_ref == f"{req.trade_id}:exit"]
    assert len(exits) == 1 and exits[0].direction == "SELL"
    assert (await _get(session_factory, req.trade_id)).status == TradeStatus.CLOSED


async def test_close_after_stop_filled_at_broker_sends_no_market_order(broker, session_factory, cache):
    manager = _manager(broker, session_factory, cache)
    req = _request()
    await manager.submit_order(req)
    broker.trigger_stop(f"{req.trade_id}:stop", Decimal("194"))

    outcome = await manager.close_position(req.trade_id, "MANUAL")

    assert outcome == CloseOutcome.CLOSED
    assert not [o for o in broker.placed_orders if o.order_ref.endswith(":exit")]
    trade = await _get(session_factory, req.trade_id)
    assert trade.status == TradeStatus.CLOSED and trade.exit_reason == ExitReason.STOP_LOSS
    assert trade.exit_price == Decimal("194") and trade.pnl == Decimal("-60")


async def test_close_does_not_sell_when_stop_cannot_be_cancelled(broker, session_factory, cache):
    manager = _manager(broker, session_factory, cache)
    req = _request()
    await manager.submit_order(req)

    async def refuse(_id):
        return False

    broker.cancel_order = refuse
    outcome = await manager.close_position(req.trade_id, "MANUAL")

    assert outcome == CloseOutcome.FAILED
    assert not [o for o in broker.placed_orders if o.order_ref.endswith(":exit")]
    assert (await _get(session_factory, req.trade_id)).status == TradeStatus.CLOSING
    assert len(cache.alerts("CLOSE_FAILED")) == 1


# ---------------------------------------------------------------------------
# C5: reconciler rules
# ---------------------------------------------------------------------------


async def test_noop_while_broker_disconnected(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache)
    await broker.disconnect()
    report = await _reconciler(broker, session_factory, manager, cache, clock).run_once()
    assert report.skipped and not report.counts


async def test_pending_without_order_is_cancelled_after_two_minutes(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache)
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    tid = await _insert(session_factory, TradeStatus.PENDING)

    await rec.run_once()
    assert (await _get(session_factory, tid)).status == TradeStatus.PENDING  # too young

    clock.state["offset"] = timedelta(minutes=3)
    await rec.run_once()
    assert (await _get(session_factory, tid)).status == TradeStatus.CANCELLED


async def test_submitted_trade_whose_order_filled_becomes_open_with_stop(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache)
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    tid = await _insert(session_factory, TradeStatus.SUBMITTED)
    broker.set_order_state(f"{tid}:entry", "FILLED", QTY, Decimal("200.5"))
    broker.set_position("AAPL", QTY, Decimal("200.5"))
    clock.state["offset"] = timedelta(minutes=2)

    report = await rec.run_once()
    await rec.run_once()

    trade = await _get(session_factory, tid)
    assert report.counts["entry_adopted"] == 1
    assert trade.status == TradeStatus.OPEN
    assert trade.entry_fill_price == Decimal("200.5") and trade.filled_quantity == QTY
    assert trade.stop_order_id is not None
    assert len([o for o in broker.placed_orders if o.order_type == "STP"]) == 1
    assert len(cache.events("trade_executed")) == 1
    assert not cache.alerts("UNMANAGED_POSITION")


async def test_submitted_working_order_is_cancelled_then_trade_cancelled(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache)
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    tid = await _insert(session_factory, TradeStatus.SUBMITTED)
    broker.set_order_state(f"{tid}:entry", "WORKING", broker_order_id="77")
    clock.state["offset"] = timedelta(minutes=6)

    first = await rec.run_once()
    assert first.counts["entry_working_cancelled"] == 1
    assert (await _get(session_factory, tid)).status == TradeStatus.SUBMITTED

    second = await rec.run_once()
    assert second.counts["entry_cancelled"] == 1
    assert (await _get(session_factory, tid)).status == TradeStatus.CANCELLED


async def test_submitted_not_found_is_cancelled_after_five_minutes(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache)
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    tid = await _insert(session_factory, TradeStatus.SUBMITTED)

    clock.state["offset"] = timedelta(minutes=3)
    await rec.run_once()
    assert (await _get(session_factory, tid)).status == TradeStatus.SUBMITTED

    clock.state["offset"] = timedelta(minutes=6)
    await rec.run_once()
    assert (await _get(session_factory, tid)).status == TradeStatus.CANCELLED


async def test_stop_filled_while_app_was_down_closes_as_stop_loss(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache)
    req = _request()
    await manager.submit_order(req)
    broker.trigger_stop(f"{req.trade_id}:stop", Decimal("194.5"))
    clock.state["offset"] = timedelta(minutes=2)
    rec = _reconciler(broker, session_factory, manager, cache, clock)

    await rec.run_once()  # a flat position must be seen twice before booking
    report = await rec.run_once()

    trade = await _get(session_factory, req.trade_id)
    assert report.counts["closed_stop_fill"] == 1
    assert trade.status == TradeStatus.CLOSED and trade.exit_reason == ExitReason.STOP_LOSS
    assert trade.exit_price == Decimal("194.5") and trade.pnl == Decimal("-55")
    assert len(cache.events("trade_closed")) == 1


async def test_position_closed_outside_app_is_booked_external_at_cached_price(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    req = _request()
    await manager.submit_order(req)
    broker.set_position("AAPL", Decimal("0"))
    cache.kv["price:AAPL"] = '{"price": "203", "timestamp": "x"}'
    clock.state["offset"] = timedelta(minutes=2)
    rec = _reconciler(broker, session_factory, manager, cache, clock)

    await rec.run_once()
    await rec.run_once()

    trade = await _get(session_factory, req.trade_id)
    assert trade.status == TradeStatus.CLOSED and trade.exit_reason == ExitReason.EXTERNAL
    assert trade.exit_price == Decimal("203") and trade.pnl == Decimal("30")
    assert len(cache.alerts("POSITION_CLOSED_EXTERNALLY")) == 1


async def test_quantity_mismatch_alerts_once_and_changes_nothing(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    req = _request()
    await manager.submit_order(req)
    broker.set_position("AAPL", Decimal("6"))
    clock.state["offset"] = timedelta(minutes=2)
    rec = _reconciler(broker, session_factory, manager, cache, clock)

    await rec.run_once()
    await rec.run_once()

    assert (await _get(session_factory, req.trade_id)).status == TradeStatus.OPEN
    assert len(cache.alerts("QUANTITY_MISMATCH")) == 1


async def test_stuck_closing_trade_is_retried_and_closed(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    req = _request()
    await manager.submit_order(req)
    broker.next_status = "REJECTED"
    assert await manager.close_position(req.trade_id, "STOP_LOSS") == CloseOutcome.FAILED
    rec = _reconciler(broker, session_factory, manager, cache, clock)

    await rec.run_once()  # first sight of CLOSING: starts the 2 minute timer
    assert (await _get(session_factory, req.trade_id)).status == TradeStatus.CLOSING

    clock.state["offset"] = timedelta(minutes=3)
    report = await rec.run_once()

    trade = await _get(session_factory, req.trade_id)
    assert report.counts["close_retried"] == 1
    assert trade.status == TradeStatus.CLOSED and trade.exit_reason == ExitReason.STOP_LOSS


async def test_closing_with_broker_flat_is_finalized_from_exit_fill(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    req = _request()
    await manager.submit_order(req)
    broker.next_status = "WORKING"
    assert await manager.close_position(req.trade_id, "TAKE_PROFIT") == CloseOutcome.PENDING_EXIT
    broker.set_order_state(f"{req.trade_id}:exit", "FILLED", QTY, Decimal("211"))
    broker.set_position("AAPL", Decimal("0"))
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    await rec.run_once()
    clock.state["offset"] = timedelta(minutes=3)

    await rec.run_once()
    await rec.run_once()  # a flat position must be seen twice before booking

    trade = await _get(session_factory, req.trade_id)
    assert trade.status == TradeStatus.CLOSED and trade.exit_reason == ExitReason.TAKE_PROFIT
    assert trade.exit_price == Decimal("211") and trade.pnl == Decimal("110")


async def test_closing_after_three_attempts_alerts_once(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    req = _request()
    await manager.submit_order(req)
    for _ in range(3):
        broker.next_status = "REJECTED"
        await manager.close_position(req.trade_id, "MANUAL", allow_from_closing=True)
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    await rec.run_once()
    clock.state["offset"] = timedelta(minutes=3)

    await rec.run_once()
    await rec.run_once()

    assert len(cache.alerts("CLOSE_NEEDS_MANUAL_INTERVENTION")) == 1
    assert (await _get(session_factory, req.trade_id)).status == TradeStatus.CLOSING


async def test_unmanaged_position_alerts_once_and_is_never_closed(broker, session_factory, cache, clock):
    manager = _manager(broker, session_factory, cache)
    broker.set_position("MSFT", Decimal("5"))
    rec = _reconciler(broker, session_factory, manager, cache, clock)

    await rec.run_once()
    await rec.run_once()

    assert len(cache.alerts("UNMANAGED_POSITION")) == 1
    assert cache.alerts("UNMANAGED_POSITION")[0]["symbol"] == "MSFT"
    assert broker.placed_orders == []


# ---------------------------------------------------------------------------
# Close and entry paths when the broker's answer is incomplete
# ---------------------------------------------------------------------------


async def test_close_never_sells_shares_the_broker_does_not_hold(broker, session_factory, cache):
    # e.g. the stop filled on an earlier day: IBKR reports today's fills only, so it looks unfilled.
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    req = _request()
    await manager.submit_order(req)
    broker.set_position("AAPL", Decimal("0"))

    assert await manager.close_position(req.trade_id, "STOP_LOSS") == CloseOutcome.FAILED

    assert not [o for o in broker.placed_orders if o.order_ref.endswith(":exit")]
    assert (await _get(session_factory, req.trade_id)).status == TradeStatus.CLOSING
    assert "nothing sold" in cache.alerts("CLOSE_FAILED")[0]["message"]


async def test_close_sells_only_what_the_broker_holds(broker, session_factory, cache):
    manager = _manager(broker, session_factory, cache, protective_stops=False)
    req = _request()
    await manager.submit_order(req)
    broker.set_position("AAPL", Decimal("4"))

    assert await manager.close_position(req.trade_id, "MANUAL") == CloseOutcome.CLOSED

    exits = [o for o in broker.placed_orders if o.order_ref.endswith(":exit")]
    assert [o.quantity for o in exits] == [Decimal("4")]


async def test_entry_with_unknown_broker_state_stays_submitted_until_reconciled(
    broker, session_factory, cache, clock
):
    manager = _manager(broker, session_factory, cache, protective_stops=False)

    async def lookup_down(order_ref):
        raise ConnectionError("gateway gone")

    broker.raise_after_next_order = True  # the order reaches the broker, then the call raises
    broker.find_order_by_ref = lookup_down  # and asking "did it arrive?" fails too
    req = _request()
    with pytest.raises(ConnectionError):
        await manager.submit_order(req)

    assert (await _get(session_factory, req.trade_id)).status == TradeStatus.SUBMITTED
    assert len(broker.placed_orders) == 1
    assert cache.alerts("ORDER_STATE_UNKNOWN")

    del broker.find_order_by_ref  # the broker answers again
    clock.state["offset"] = timedelta(minutes=2)
    await _reconciler(broker, session_factory, manager, cache, clock).run_once()
    assert (await _get(session_factory, req.trade_id)).status == TradeStatus.OPEN
