"""Unit tests for _cancel_and_wait: every broker await is bounded by the cancel-wait deadline."""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.base import OrderResult
from app.core.execution.order_fills import FillSyncMixin


def _order(order_ref: str, status: str, qty: str = "0") -> OrderResult:
    return OrderResult(
        trade_id=uuid.UUID(order_ref.split(":")[0]),
        broker_order_id="O1",
        status=status,
        filled_quantity=Decimal(qty),
        avg_fill_price=Decimal("0"),
        order_ref=order_ref,
        timestamp=datetime.now(UTC),
    )


class _Broker:
    """Scripted broker: lookups return `states` in order, then the last state forever."""

    def __init__(self, states, *, stall_find=None, stall_cancel=False):
        self._states = list(states)
        self._stall_find = stall_find  # index of the lookup that never returns
        self._stall_cancel = stall_cancel
        self.stuck = asyncio.Event()  # never set
        self.lookups = 0

    async def find_order_by_ref(self, order_ref: str):
        index = self.lookups
        self.lookups += 1
        if self._stall_find is not None and index == self._stall_find:
            await self.stuck.wait()
        return self._states[min(index, len(self._states) - 1)]

    async def cancel_order(self, broker_order_id: str) -> bool:
        if self._stall_cancel:
            await self.stuck.wait()
        return True


class _Host(FillSyncMixin):
    def __init__(self, broker):
        self._broker = broker
        self._stop_cancel_wait_seconds = 0.2


async def test_cancel_and_wait_bounds_stalled_broker_calls():
    order_ref = f"{uuid.uuid4()}:stop"
    host = _Host(_Broker([None], stall_find=0))

    result = await asyncio.wait_for(host._cancel_and_wait(order_ref), timeout=2)

    assert result is not None
    assert not result.is_terminal
    assert result.status == "ERROR"
    assert result.order_ref == order_ref
    assert result.error_message == "broker call timed out during cancel wait"


async def test_cancel_and_wait_returns_working_when_cancel_stalls():
    order_ref = f"{uuid.uuid4()}:stop"
    host = _Host(_Broker([_order(order_ref, "WORKING")], stall_cancel=True))

    result = await asyncio.wait_for(host._cancel_and_wait(order_ref), timeout=2)

    assert result is not None
    assert result.status == "WORKING"
    assert not result.is_terminal


async def test_cancel_and_wait_returns_non_terminal_when_polling_stalls():
    order_ref = f"{uuid.uuid4()}:stop"
    host = _Host(_Broker([_order(order_ref, "WORKING"), None], stall_find=1))

    result = await asyncio.wait_for(host._cancel_and_wait(order_ref), timeout=2)

    assert result is not None
    assert result.status == "WORKING"
    assert not result.is_terminal


async def test_cancel_and_wait_returns_partial_after_cancel_when_broker_responds():
    order_ref = f"{uuid.uuid4()}:stop"
    broker = _Broker(
        [_order(order_ref, "WORKING"), _order(order_ref, "PARTIAL", "3")],
    )
    host = _Host(broker)

    result = await asyncio.wait_for(host._cancel_and_wait(order_ref), timeout=2)

    assert result is not None
    assert result.status == "PARTIAL"
    assert result.filled_quantity == Decimal("3")
