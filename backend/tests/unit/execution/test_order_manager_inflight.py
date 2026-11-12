"""Unit tests for OrderManager's in-flight submit_order counter and wait_for_entries_idle."""

import asyncio
import uuid
from decimal import Decimal

import pytest

from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest


def _request(**kwargs) -> TradeRequest:
    defaults = {
        "trade_id": uuid.uuid4(),
        "symbol": "AAPL",
        "direction": "BUY",
        "quantity": Decimal("10"),
        "entry_price": Decimal("200"),
        "stop_loss_price": Decimal("195"),
        "account_balance": Decimal("100000"),
    }
    defaults.update(kwargs)
    return TradeRequest(**defaults)


@pytest.fixture
async def broker() -> MockBroker:
    b = MockBroker()
    await b.connect()
    return b


@pytest.fixture
def manager(broker: MockBroker) -> OrderManager:
    return OrderManager(broker, RiskManager(), order_retry_attempts=1)


class GatedBroker(MockBroker):
    """MockBroker whose place_order blocks until the gate is set."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def place_order(self, order):
        self.entered.set()
        await self.gate.wait()
        return await super().place_order(order)


async def test_idle_when_nothing_in_flight(manager: OrderManager) -> None:
    assert await manager.wait_for_entries_idle(0.01) is True


async def test_waits_for_blocked_entry() -> None:
    broker = GatedBroker()
    await broker.connect()
    manager = OrderManager(broker, RiskManager(), order_retry_attempts=1)

    task = asyncio.create_task(manager.submit_order(_request()))
    try:
        await broker.entered.wait()
        assert await manager.wait_for_entries_idle(0.05) is False

        broker.gate.set()
        await task
        assert await manager.wait_for_entries_idle(0.05) is True
    finally:
        broker.gate.set()
        if not task.done():
            task.cancel()


async def test_counter_released_on_error(broker: MockBroker, manager: OrderManager) -> None:
    async def failing_place_order(order_request):
        raise ConnectionError("broker unavailable")

    broker.place_order = failing_place_order

    with pytest.raises(ConnectionError):
        await manager.submit_order(_request())

    assert manager._entries_in_flight == 0
    assert await manager.wait_for_entries_idle(0.01) is True


async def test_counter_released_on_validation_rejection(manager: OrderManager) -> None:
    with pytest.raises(RiskRejectionError):
        await manager.submit_order(_request(stop_loss_price=None))

    assert manager._entries_in_flight == 0
    assert await manager.wait_for_entries_idle(0.01) is True
