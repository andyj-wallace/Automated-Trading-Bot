"""Reconciler quantity-mismatch alerts and holdings-gated stop replacement, against MockBroker."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.risk.manager import RiskManager
from app.db.models.trade import TradeStatus
from tests.integration import test_reconciler as base

# Fixtures and helpers shared with test_reconciler.py, bound here so pytest registers the fixtures.
_get = base._get
_reconciler = base._reconciler
_request = base._request
broker = base.broker
cache = base.cache
clock = base.clock
db_engine = base.db_engine
session_factory = base.session_factory

QTY = Decimal("10")


class CountingBroker(MockBroker):
    """MockBroker that counts get_executions() calls."""

    def __init__(self) -> None:
        super().__init__()
        self.executions_calls = 0

    async def get_executions(self):
        self.executions_calls += 1
        return await super().get_executions()


@pytest.fixture
async def counting_broker():
    b = CountingBroker()
    await b.connect()
    b.last_prices["AAPL"] = base.ENTRY
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


async def _open_trade(broker, session_factory, cache, clock, symbol="AAPL", filled=None):
    """Submit a BUY through OrderManager (entry plus protective stop), then age it past MIN_AGE."""
    manager = _manager(broker, session_factory, cache)
    req = _request(symbol)
    if filled is not None:
        broker.next_status, broker.next_filled_quantity = "PARTIAL", filled
    await manager.submit_order(req)
    clock.state["offset"] = timedelta(minutes=2)
    return req.trade_id, manager


def _stops(broker) -> list:
    return [o for o in broker.placed_orders if o.order_type == "STP"]


async def test_single_fetch_and_no_order_when_quantities_match(
    counting_broker, session_factory, cache, clock
):
    broker = counting_broker
    tid_a, manager = await _open_trade(broker, session_factory, cache, clock)
    broker.last_prices["MSFT"] = Decimal("100.00")
    tid_b, _ = await _open_trade(broker, session_factory, cache, clock, symbol="MSFT")
    broker.set_position("AAPL", QTY)
    broker.set_position("MSFT", QTY)
    orders_before = len(broker.placed_orders)
    broker.executions_calls = 0

    report = await _reconciler(broker, session_factory, manager, cache, clock).run_once()

    assert broker.executions_calls == 1
    assert len(broker.placed_orders) == orders_before
    assert not report.counts.get("quantity_mismatch")
    assert not cache.alerts("QUANTITY_MISMATCH")
    assert (await _get(session_factory, tid_a)).status == TradeStatus.OPEN
    assert (await _get(session_factory, tid_b)).status == TradeStatus.OPEN


async def test_excess_alerts_once_and_places_no_order(broker, session_factory, cache, clock):
    tid, manager = await _open_trade(broker, session_factory, cache, clock, filled=Decimal("4"))
    broker.set_position("AAPL", QTY)
    rec = _reconciler(broker, session_factory, manager, cache, clock)
    orders_before = len(broker.placed_orders)

    await rec.run_once()
    await rec.run_once()

    alerts = cache.alerts("QUANTITY_MISMATCH")
    assert len(alerts) == 1 and alerts[0]["kind"] == "EXCESS"
    assert len(broker.placed_orders) == orders_before
    assert (await _get(session_factory, tid)).status == TradeStatus.OPEN


async def test_direction_mismatch_alerts_and_places_no_order(broker, session_factory, cache, clock):
    tid, manager = await _open_trade(broker, session_factory, cache, clock)
    broker.set_position("AAPL", Decimal("-5"))
    orders_before = len(broker.placed_orders)

    await _reconciler(broker, session_factory, manager, cache, clock).run_once()

    alerts = cache.alerts("QUANTITY_MISMATCH")
    assert len(alerts) == 1 and alerts[0]["kind"] == "DIRECTION"
    assert len(broker.placed_orders) == orders_before


async def test_missing_alerts_and_places_no_order(broker, session_factory, cache, clock):
    tid, manager = await _open_trade(broker, session_factory, cache, clock)
    broker.set_position("AAPL", Decimal("6"))
    orders_before = len(broker.placed_orders)

    await _reconciler(broker, session_factory, manager, cache, clock).run_once()

    alerts = cache.alerts("QUANTITY_MISMATCH")
    assert len(alerts) == 1 and alerts[0]["kind"] == "MISSING"
    assert Decimal(alerts[0]["expected_quantity"]) == QTY
    assert Decimal(alerts[0]["broker_quantity"]) == Decimal("6")
    assert len(broker.placed_orders) == orders_before


async def test_partial_stop_fill_is_not_a_mismatch(broker, session_factory, cache, clock):
    tid, manager = await _open_trade(broker, session_factory, cache, clock)
    stop_ref = f"{tid}:stop"
    broker.set_order_state(stop_ref, "WORKING", Decimal("4"), Decimal("9.50"))
    broker.set_position("AAPL", Decimal("6"))
    stop_id_before = (await _get(session_factory, tid)).stop_order_id
    orders_before = len(broker.placed_orders)

    report = await _reconciler(broker, session_factory, manager, cache, clock).run_once()

    trade = await _get(session_factory, tid)
    assert trade.status == TradeStatus.OPEN
    assert not cache.alerts("QUANTITY_MISMATCH")
    assert not report.counts.get("stop_replaced")
    assert len(broker.placed_orders) == orders_before
    assert trade.stop_order_id == stop_id_before


async def test_terminal_partial_stop_is_replaced_for_open_quantity(
    broker, session_factory, cache, clock
):
    tid, manager = await _open_trade(broker, session_factory, cache, clock)
    stop_ref = f"{tid}:stop"
    broker.set_order_state(stop_ref, "WORKING", Decimal("4"), Decimal("9.50"))
    broker.set_order_state(stop_ref, "PARTIAL", Decimal("4"), Decimal("9.50"))
    broker.set_position("AAPL", Decimal("6"))
    stops_before = len(_stops(broker))
    rec = _reconciler(broker, session_factory, manager, cache, clock)

    report = await rec.run_once()

    new_stops = _stops(broker)[stops_before:]
    assert len(new_stops) == 1 and new_stops[0].quantity == Decimal("6")
    assert report.counts["stop_replaced"] == 1
    assert len(cache.alerts("PROTECTIVE_STOP_REPLACED")) == 1
    assert (await _get(session_factory, tid)).status == TradeStatus.OPEN


@pytest.mark.parametrize(
    "held, kind, stop_qty",
    [
        (Decimal("-6"), "DIRECTION", None),
        (Decimal("3"), "MISSING", None),
        (Decimal("6"), None, Decimal("6")),
        (Decimal("8"), "EXCESS", Decimal("6")),
    ],
)
async def test_terminal_stop_replacement_only_when_holdings_allow(
    broker, session_factory, cache, clock, held, kind, stop_qty
):
    tid, manager = await _open_trade(broker, session_factory, cache, clock)
    stop_ref = f"{tid}:stop"
    broker.set_order_state(stop_ref, "WORKING", Decimal("4"), Decimal("9.50"))
    broker.set_order_state(stop_ref, "PARTIAL", Decimal("4"), Decimal("9.50"))
    broker.set_position("AAPL", held)
    stops_before = len(_stops(broker))
    rec = _reconciler(broker, session_factory, manager, cache, clock)

    await rec.run_once()
    await rec.run_once()

    new_stops = _stops(broker)[stops_before:]
    if stop_qty is None:
        assert new_stops == []
    else:
        assert len(new_stops) == 1 and new_stops[0].quantity == stop_qty
    alerts = cache.alerts("QUANTITY_MISMATCH")
    if kind is None:
        assert alerts == []
    else:
        assert len(alerts) == 1 and alerts[0]["kind"] == kind


async def test_flat_terminal_stop_places_no_stop(broker, session_factory, cache, clock):
    tid, manager = await _open_trade(broker, session_factory, cache, clock)
    stop_ref = f"{tid}:stop"
    broker.set_order_state(stop_ref, "WORKING", Decimal("4"), Decimal("9.50"))
    broker.set_order_state(stop_ref, "PARTIAL", Decimal("4"), Decimal("9.50"))
    broker.set_position("AAPL", Decimal("0"))
    stops_before = len(_stops(broker))
    rec = _reconciler(broker, session_factory, manager, cache, clock)

    await rec.run_once()
    await rec.run_once()

    assert len(_stops(broker)) == stops_before


async def test_late_entry_fill_is_adopted_and_stop_resized_to_ten(broker, session_factory, cache, clock):
    broker.last_prices["AAPL"] = Decimal("10.00")
    tid, manager = await _open_trade(broker, session_factory, cache, clock, filled=Decimal("4"))
    broker.set_order_state(f"{tid}:entry", "WORKING", Decimal("4"), Decimal("10.00"))
    broker.set_order_state(f"{tid}:entry", "FILLED", QTY, Decimal("10.20"))
    broker.set_position("AAPL", QTY)
    rec = _reconciler(broker, session_factory, manager, cache, clock)

    report = await rec.run_once()

    trade = await _get(session_factory, tid)
    assert trade.status == TradeStatus.OPEN
    assert trade.filled_quantity == QTY
    assert trade.entry_fill_price == Decimal("10.12")
    assert report.counts["late_entry_fill"] == 1
    assert len(cache.alerts("LATE_ENTRY_FILL")) == 1
    assert report.counts["stop_replaced"] == 1
    stop = await broker.find_order_by_ref(f"{tid}:stop")
    assert stop.status == "WORKING" and stop.ordered_quantity == QTY
    assert trade.stop_order_id == stop.broker_order_id
