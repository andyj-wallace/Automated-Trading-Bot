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
        "stop_loss_price": Decimal("150.00"),
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
async def test_cancel_order_returns_true(connected_broker: MockBroker) -> None:
    result = await connected_broker.cancel_order("any-order-id")
    assert result is True


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
