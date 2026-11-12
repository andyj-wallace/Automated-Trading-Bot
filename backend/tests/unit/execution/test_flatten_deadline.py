"""Unit tests for the flatten cancel-poll deadline: it is elapsed time, not a count of polls."""

import asyncio
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.brokers.base import OrderResult
from app.core.execution.flatten import flatten_positions
from app.db.models.trade import TradeStatus


def _order(trade_id, status: str, qty: int = 0) -> OrderResult:
    return OrderResult(
        trade_id=trade_id,
        broker_order_id="O1",
        status=status,
        filled_quantity=Decimal(qty),
        avg_fill_price=Decimal("0"),
        timestamp=datetime.now(UTC),
    )


@pytest.fixture
def h():
    row = SimpleNamespace(id=uuid.uuid4(), symbol="AAPL", status=TradeStatus.SUBMITTED)
    broker = AsyncMock()
    broker.cancel_order = AsyncMock(return_value=True)
    return SimpleNamespace(
        row=row,
        working=_order(row.id, "WORKING"),
        trading_state=SimpleNamespace(pause=AsyncMock()),
        order_manager=SimpleNamespace(
            wait_for_entries_idle=AsyncMock(return_value=True),
            adopt_entry_fill=AsyncMock(),
            close_position=AsyncMock(),
        ),
        repo=SimpleNamespace(
            get_active_trades=AsyncMock(return_value=[row]),
            get_open_trades=AsyncMock(return_value=[]),
        ),
        broker=broker,
    )


def _fake_clock():
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    return now, sleeps, fake_sleep


async def test_still_working_after_deadline_fails(h):
    now, sleeps, fake_sleep = _fake_clock()
    h.broker.find_order_by_ref = AsyncMock(return_value=h.working)

    results = await flatten_positions(
        h.trading_state,
        h.order_manager,
        h.broker,
        h.repo,
        poll_timeout=1.0,
        poll_interval=0.25,
        sleep=fake_sleep,
        clock=lambda: now[0],
    )

    assert results == [
        {"trade_id": str(h.row.id), "symbol": "AAPL", "outcome": "ENTRY_CANCEL_FAILED"}
    ]
    assert sum(sleeps) <= 1.0
    assert now[0] <= 1.0


async def test_delayed_lookup_is_bounded_by_deadline(h):
    calls = 0

    async def lookup(ref: str):
        nonlocal calls
        calls += 1
        if calls > 1:
            await asyncio.sleep(5)
        return h.working

    h.broker.find_order_by_ref = AsyncMock(side_effect=lookup)

    start = time.monotonic()
    results = await flatten_positions(
        h.trading_state,
        h.order_manager,
        h.broker,
        h.repo,
        poll_timeout=0.3,
        poll_interval=0.05,
    )
    elapsed = time.monotonic() - start

    assert results[0]["outcome"] == "ENTRY_CANCEL_FAILED"
    assert elapsed < 2.0


async def test_settles_before_deadline(h):
    now, sleeps, fake_sleep = _fake_clock()
    rejected = _order(h.row.id, "REJECTED", 0)
    h.broker.find_order_by_ref = AsyncMock(side_effect=[h.working, h.working, rejected])

    results = await flatten_positions(
        h.trading_state,
        h.order_manager,
        h.broker,
        h.repo,
        poll_timeout=1.0,
        poll_interval=0.25,
        sleep=fake_sleep,
        clock=lambda: now[0],
    )

    assert results[0]["outcome"] == "ENTRY_CANCELLED"
    assert len(sleeps) == 2
    h.broker.cancel_order.assert_awaited_once_with("O1")


async def test_no_broker_order_is_cancelled_outcome(h):
    h.broker.find_order_by_ref = AsyncMock(return_value=None)

    results = await flatten_positions(h.trading_state, h.order_manager, h.broker, h.repo)

    assert results[0]["outcome"] == "ENTRY_CANCELLED"
    h.broker.cancel_order.assert_not_awaited()


async def test_idle_timeout_adds_in_flight_result(h):
    h.order_manager.wait_for_entries_idle = AsyncMock(return_value=False)
    h.broker.find_order_by_ref = AsyncMock(return_value=None)

    results = await flatten_positions(h.trading_state, h.order_manager, h.broker, h.repo)

    assert results[0] == {"outcome": "ENTRIES_STILL_IN_FLIGHT"}


async def test_lookup_error_fails_that_trade(h):
    h.broker.find_order_by_ref = AsyncMock(side_effect=[RuntimeError("lookup down")])

    results = await flatten_positions(h.trading_state, h.order_manager, h.broker, h.repo)

    assert results[0]["outcome"] == "ENTRY_CANCEL_FAILED"
    h.broker.cancel_order.assert_not_awaited()
