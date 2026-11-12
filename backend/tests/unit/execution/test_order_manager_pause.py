"""
Unit tests for OrderManager's trading pause gate (AC6).

A paused bot must not open new strategy positions, while manual entries still go through.
"""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Pause gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_entry_rejected_while_paused() -> None:
    broker = AsyncMock()
    manager = OrderManager(
        broker,
        RiskManager(),
        order_retry_attempts=1,
        is_paused=AsyncMock(return_value=True),
    )

    with pytest.raises(RiskRejectionError) as exc_info:
        await manager.submit_order(_request(strategy_id=uuid.uuid4()))

    assert exc_info.value.reason.startswith("TRADING_PAUSED")
    assert broker.mock_calls == []
    assert manager._entries_in_flight == 0


@pytest.mark.asyncio
async def test_manual_entry_proceeds_while_paused(broker: MockBroker) -> None:
    manager = OrderManager(
        broker,
        RiskManager(),
        order_retry_attempts=1,
        is_paused=AsyncMock(return_value=True),
    )

    _, result = await manager.submit_order(_request(strategy_id=None))

    assert result.status == "FILLED"


@pytest.mark.asyncio
async def test_not_paused_strategy_entry_proceeds(broker: MockBroker) -> None:
    manager = OrderManager(
        broker,
        RiskManager(),
        order_retry_attempts=1,
        is_paused=AsyncMock(return_value=False),
    )

    _, result = await manager.submit_order(_request(strategy_id=uuid.uuid4()))

    assert result.status == "FILLED"


@pytest.mark.asyncio
async def test_paused_before_broker_call_cancels(broker: MockBroker) -> None:
    manager = OrderManager(
        broker,
        RiskManager(),
        order_retry_attempts=1,
        is_paused=AsyncMock(side_effect=[False, True]),
    )
    manager._session_factory = MagicMock()
    manager._validate_entry = AsyncMock()
    manager._create_pending = AsyncMock()
    manager._transition = AsyncMock()
    manager._write_pre_submission = MagicMock()
    broker.place_order = AsyncMock(wraps=broker.place_order)

    req = _request(strategy_id=uuid.uuid4())
    with pytest.raises(RiskRejectionError) as exc_info:
        await manager.submit_order(req)

    assert exc_info.value.reason.startswith("TRADING_PAUSED")
    broker.place_order.assert_not_awaited()
    assert manager._transition.await_args_list == [
        call(req.trade_id, "SUBMITTED"),
        call(req.trade_id, "CANCELLED"),
    ]
    assert manager._entries_in_flight == 0
