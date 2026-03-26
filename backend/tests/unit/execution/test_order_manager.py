"""
Unit tests for OrderManager (Layer 7.1).

Verifies the full Checkpoint requirement:
  (1) pre-submission entry appears in audit.log
  (2) mock order is placed
  (3) post-confirmation entry appears in audit.log
  (4) trade row persistence is delegated correctly (TradeHandler — tested separately)
  (5) failed post-confirmation audit write escalates to error.log
"""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call, patch

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


@pytest.fixture
def manager(broker: MockBroker) -> OrderManager:
    return OrderManager(broker, RiskManager())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_order_returns_validation_and_result(
    manager: OrderManager,
) -> None:
    req = _request()
    validation, result = await manager.submit_order(req)

    assert validation.trade_id == req.trade_id
    assert result.trade_id == req.trade_id
    assert result.status == "FILLED"


@pytest.mark.asyncio
async def test_submit_order_fills_requested_quantity(manager: OrderManager) -> None:
    req = _request(quantity=Decimal("25"))
    _, result = await manager.submit_order(req)
    assert result.filled_quantity == Decimal("25")


# ---------------------------------------------------------------------------
# Checkpoint (1): pre-submission entry written to audit.log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_submission_audit_entry_written(manager: OrderManager) -> None:
    req = _request()
    with patch("app.core.execution.order_manager.audit_logger") as mock_audit:
        await manager.submit_order(req)

    messages = [c.args[0] for c in mock_audit.info.call_args_list]
    assert "PRE_SUBMISSION" in messages


@pytest.mark.asyncio
async def test_pre_submission_audit_contains_trade_id(manager: OrderManager) -> None:
    req = _request()
    with patch("app.core.execution.order_manager.audit_logger") as mock_audit:
        await manager.submit_order(req)

    pre_call = next(
        c for c in mock_audit.info.call_args_list if c.args[0] == "PRE_SUBMISSION"
    )
    extra = pre_call.kwargs["extra"]
    assert extra["trade_id"] == str(req.trade_id)
    assert extra["symbol"] == "AAPL"
    assert extra["risk_validation"] == "APPROVED"
    assert extra["stop_loss_price"] == "195"


# ---------------------------------------------------------------------------
# Checkpoint (2): mock order is placed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broker_place_order_called(broker: MockBroker, manager: OrderManager) -> None:
    req = _request()
    original = broker.place_order
    calls = []

    async def spy(order_request):
        calls.append(order_request)
        return await original(order_request)

    broker.place_order = spy
    await manager.submit_order(req)

    assert len(calls) == 1
    assert calls[0].symbol == "AAPL"
    assert calls[0].trade_id == req.trade_id


# ---------------------------------------------------------------------------
# Checkpoint (3): post-confirmation entry written to audit.log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_confirmation_audit_entry_written(manager: OrderManager) -> None:
    req = _request()
    with patch("app.core.execution.order_manager.audit_logger") as mock_audit:
        await manager.submit_order(req)

    messages = [c.args[0] for c in mock_audit.info.call_args_list]
    assert "POST_CONFIRMATION" in messages


@pytest.mark.asyncio
async def test_post_confirmation_contains_broker_order_id(
    manager: OrderManager,
) -> None:
    req = _request()
    with patch("app.core.execution.order_manager.audit_logger") as mock_audit:
        await manager.submit_order(req)

    post_call = next(
        c for c in mock_audit.info.call_args_list if c.args[0] == "POST_CONFIRMATION"
    )
    extra = post_call.kwargs["extra"]
    assert extra["broker_order_id"] != ""
    assert extra["status"] == "FILLED"


@pytest.mark.asyncio
async def test_both_audit_entries_written_in_order(manager: OrderManager) -> None:
    req = _request()
    with patch("app.core.execution.order_manager.audit_logger") as mock_audit:
        await manager.submit_order(req)

    messages = [c.args[0] for c in mock_audit.info.call_args_list]
    pre_idx = messages.index("PRE_SUBMISSION")
    post_idx = messages.index("POST_CONFIRMATION")
    assert pre_idx < post_idx


# ---------------------------------------------------------------------------
# Risk rejection propagates — no audit entries written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_rejection_propagates(manager: OrderManager) -> None:
    req = _request(stop_loss_price=None)
    with pytest.raises(RiskRejectionError):
        await manager.submit_order(req)


@pytest.mark.asyncio
async def test_no_audit_entry_on_risk_rejection(manager: OrderManager) -> None:
    req = _request(stop_loss_price=None)
    with patch("app.core.execution.order_manager.audit_logger") as mock_audit:
        with pytest.raises(RiskRejectionError):
            await manager.submit_order(req)
    mock_audit.info.assert_not_called()


# ---------------------------------------------------------------------------
# Checkpoint (5): failed post-confirmation audit → escalated to error.log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_post_confirmation_audit_escalates_to_error_log(
    manager: OrderManager,
) -> None:
    req = _request()

    def audit_side_effect(message, **kwargs):
        if message == "POST_CONFIRMATION":
            raise OSError("disk full")

    with (
        patch("app.core.execution.order_manager.audit_logger") as mock_audit,
        patch("app.core.execution.order_manager.error_logger") as mock_error,
    ):
        mock_audit.info.side_effect = audit_side_effect
        with pytest.raises(OSError, match="disk full"):
            await manager.submit_order(req)

    mock_error.critical.assert_called_once()
    call_extra = mock_error.critical.call_args.kwargs["extra"]
    assert call_extra["trade_id"] == str(req.trade_id)
    assert "disk full" in call_extra["audit_error"]


@pytest.mark.asyncio
async def test_broker_error_still_writes_post_confirmation_as_error(
    broker: MockBroker, manager: OrderManager
) -> None:
    """Broker failure should produce a POST_CONFIRMATION with status=ERROR."""
    req = _request()

    async def failing_place_order(order_request):
        raise ConnectionError("broker unavailable")

    broker.place_order = failing_place_order

    with patch("app.core.execution.order_manager.audit_logger") as mock_audit:
        with pytest.raises(ConnectionError):
            await manager.submit_order(req)

    messages = [c.args[0] for c in mock_audit.info.call_args_list]
    assert "PRE_SUBMISSION" in messages
    assert "POST_CONFIRMATION" in messages

    post_call = next(
        c for c in mock_audit.info.call_args_list if c.args[0] == "POST_CONFIRMATION"
    )
    assert post_call.kwargs["extra"]["status"] == "ERROR"
