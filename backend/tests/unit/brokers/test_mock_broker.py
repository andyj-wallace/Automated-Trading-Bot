"""
Checkpoint A tests for Layer 4 — Broker Abstraction.

These tests use MockBroker exclusively. No live connection or IB Gateway
required. They verify connect(), get_account_summary(), and place_order()
all work correctly in isolation.
"""

import uuid
from decimal import Decimal

import pytest

from app.brokers.base import OrderRequest
from app.brokers.mock.client import MockBroker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def broker() -> MockBroker:
    return MockBroker()


@pytest.fixture
async def connected_broker() -> MockBroker:
    b = MockBroker()
    await b.connect()
    return b


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_state_is_disconnected(broker: MockBroker) -> None:
    assert not broker.is_connected()


@pytest.mark.asyncio
async def test_connect_sets_connected(broker: MockBroker) -> None:
    await broker.connect()
    assert broker.is_connected()


@pytest.mark.asyncio
async def test_disconnect_clears_connected(broker: MockBroker) -> None:
    await broker.connect()
    await broker.disconnect()
    assert not broker.is_connected()


@pytest.mark.asyncio
async def test_operations_raise_when_disconnected(broker: MockBroker) -> None:
    with pytest.raises(RuntimeError, match="not connected"):
        await broker.get_account_summary()

    with pytest.raises(RuntimeError, match="not connected"):
        await broker.get_positions()

    with pytest.raises(RuntimeError, match="not connected"):
        await broker.cancel_order("1")


# ---------------------------------------------------------------------------
# Account summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_account_summary_returns_valid_data(connected_broker: MockBroker) -> None:
    summary = await connected_broker.get_account_summary()

    assert summary.account_id == "DU999999"
    assert summary.net_liquidation > 0
    assert summary.cash_balance > 0
    assert summary.buying_power > 0
    assert summary.currency == "USD"


@pytest.mark.asyncio
async def test_get_positions_returns_empty_list(connected_broker: MockBroker) -> None:
    positions = await connected_broker.get_positions()
    assert positions == []


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------


def _make_order(**kwargs) -> OrderRequest:
    defaults = {
        "trade_id": uuid.uuid4(),
        "symbol": "AAPL",
        "direction": "BUY",
        "quantity": Decimal("10"),
        "order_type": "MKT",
    }
    defaults.update(kwargs)
    return OrderRequest(**defaults)


@pytest.mark.asyncio
async def test_place_market_order_filled(connected_broker: MockBroker) -> None:
    order = _make_order()
    result = await connected_broker.place_order(order)

    assert result.trade_id == order.trade_id
    assert result.status == "FILLED"
    assert result.filled_quantity == order.quantity
    assert result.broker_order_id != ""
    assert result.timestamp is not None


@pytest.mark.asyncio
async def test_place_limit_order_fills_at_limit_price(connected_broker: MockBroker) -> None:
    order = _make_order(
        order_type="LMT",
        limit_price=Decimal("175.50"),
    )
    result = await connected_broker.place_order(order)

    assert result.status == "FILLED"
    assert result.avg_fill_price == Decimal("175.50")


@pytest.mark.asyncio
async def test_place_sell_order(connected_broker: MockBroker) -> None:
    order = _make_order(direction="SELL", quantity=Decimal("5"))
    result = await connected_broker.place_order(order)

    assert result.status == "FILLED"
    assert result.filled_quantity == Decimal("5")


@pytest.mark.asyncio
async def test_broker_order_ids_are_unique(connected_broker: MockBroker) -> None:
    r1 = await connected_broker.place_order(_make_order())
    r2 = await connected_broker.place_order(_make_order())

    assert r1.broker_order_id != r2.broker_order_id


@pytest.mark.asyncio
async def test_cancel_order_cancels_working_order_only(connected_broker: MockBroker) -> None:
    assert await connected_broker.cancel_order("unknown") is False

    tid = uuid.uuid4()
    stop = OrderRequest(
        trade_id=tid, symbol="AAPL", direction="SELL", quantity=Decimal("5"),
        order_type="STP", stop_price=Decimal("95"), tif="GTC", order_ref=f"{tid}:stop",
    )
    placed = await connected_broker.place_order(stop)
    assert placed.status == "WORKING"
    assert await connected_broker.cancel_order(placed.broker_order_id) is True
    after = await connected_broker.find_order_by_ref(f"{tid}:stop")
    assert after is not None and after.status == "REJECTED"
    assert await connected_broker.cancel_order(placed.broker_order_id) is False


@pytest.mark.asyncio
async def test_trigger_stop_fills_order_and_reduces_position(connected_broker: MockBroker) -> None:
    tid = uuid.uuid4()
    connected_broker.set_position("AAPL", Decimal("5"), Decimal("100"))
    await connected_broker.place_order(
        OrderRequest(
            trade_id=tid, symbol="AAPL", direction="SELL", quantity=Decimal("5"),
            order_type="STP", stop_price=Decimal("95"), tif="GTC", order_ref=f"{tid}:stop",
        )
    )
    connected_broker.trigger_stop(f"{tid}:stop", Decimal("94.5"))

    assert "AAPL" not in connected_broker.positions
    summary = await connected_broker.get_execution_summary(f"{tid}:stop")
    assert summary is not None
    assert summary.filled_quantity == Decimal("5") and summary.avg_fill_price == Decimal("94.5")


# ---------------------------------------------------------------------------
# Price feed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_price_feed_and_simulate_tick(connected_broker: MockBroker) -> None:
    received: list = []

    async def on_price(update) -> None:
        received.append(update)

    await connected_broker.subscribe_price_feed(["AAPL", "MSFT"], on_price)
    await connected_broker.simulate_price_tick("AAPL", Decimal("182.50"))

    assert len(received) == 1
    assert received[0].ticker == "AAPL"
    assert received[0].price == Decimal("182.50")


@pytest.mark.asyncio
async def test_market_fill_uses_last_simulated_price_and_tracks_positions(
    connected_broker: MockBroker,
) -> None:
    import uuid
    from decimal import Decimal

    from app.brokers.base import OrderRequest

    await connected_broker.simulate_price_tick("tsla", Decimal("200"))

    def order(direction: str, qty: str) -> OrderRequest:
        return OrderRequest(
            trade_id=uuid.uuid4(), symbol="TSLA", direction=direction, quantity=Decimal(qty)
        )

    buy = await connected_broker.place_order(order("BUY", "10"))
    assert buy.avg_fill_price == Decimal("200")
    positions = await connected_broker.get_positions()
    assert [(p.symbol, p.quantity) for p in positions] == [("TSLA", Decimal("10"))]

    await connected_broker.place_order(order("SELL", "4"))
    assert (await connected_broker.get_positions())[0].quantity == Decimal("6")
    await connected_broker.place_order(order("SELL", "6"))
    assert await connected_broker.get_positions() == []

    unknown = OrderRequest(
        trade_id=uuid.uuid4(), symbol="ZZZ", direction="BUY", quantity=Decimal("1")
    )
    assert (await connected_broker.place_order(unknown)).avg_fill_price == Decimal("100.00")


# ---------------------------------------------------------------------------
# C2 / C3: order refs, outcome toggles, lookups
# ---------------------------------------------------------------------------


async def _connected() -> MockBroker:
    b = MockBroker()
    await b.connect()
    return b


async def test_next_status_toggles_are_one_shot():
    b = await _connected()
    b.next_status = "WORKING"
    working = await b.place_order(_make_order(order_ref="r1"))
    assert working.status == "WORKING" and working.filled_quantity == 0
    assert b.positions == {}
    normal = await b.place_order(_make_order(order_ref="r2"))
    assert normal.status == "FILLED"


async def test_partial_fill_quantity_and_lookup():
    b = await _connected()
    b.next_status = "PARTIAL"
    b.next_filled_quantity = Decimal("4")
    res = await b.place_order(_make_order(quantity=Decimal("10"), order_ref="r1"))
    assert res.status == "PARTIAL" and res.filled_quantity == Decimal("4")
    assert (await b.find_order_by_ref("r1")).status == "PARTIAL"
    assert await b.find_order_by_ref("missing") is None


async def test_execution_summary_aggregates_fills_under_one_ref():
    b = await _connected()
    b.last_prices["AAPL"] = Decimal("10")
    b.next_status = "PARTIAL"
    b.next_filled_quantity = Decimal("4")
    await b.place_order(_make_order(quantity=Decimal("10"), order_ref="x:exit"))
    b.last_prices["AAPL"] = Decimal("20")
    await b.place_order(_make_order(quantity=Decimal("6"), order_ref="x:exit"))
    summary = await b.get_execution_summary("x:exit")
    assert summary.filled_quantity == Decimal("10")
    assert summary.avg_fill_price == Decimal("16")


async def test_stop_order_rests_working():
    b = await _connected()
    res = await b.place_order(_make_order(order_type="STP", stop_price=Decimal("95"), order_ref="s"))
    assert res.status == "WORKING"
    assert b.positions == {}
