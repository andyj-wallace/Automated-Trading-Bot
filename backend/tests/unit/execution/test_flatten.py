"""Unit tests for flatten_positions: entry settling and closing OPEN trades."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.brokers.base import OrderResult
from app.core.execution.flatten import flatten_positions
from app.core.execution.order_close import CloseOutcome
from app.db.models.trade import TradeStatus


def _order(status: str, qty: int = 0, broker_order_id: str = "O1", trade_id=None) -> OrderResult:
    return OrderResult(
        trade_id=trade_id or uuid.uuid4(),
        broker_order_id=broker_order_id,
        status=status,
        filled_quantity=Decimal(qty),
        avg_fill_price=Decimal("100"),
        timestamp=datetime.now(UTC),
    )


def _row(symbol: str, status: TradeStatus) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), symbol=symbol, status=status)


def _scripted(script: dict[str, list]):
    """Broker lookup that returns each ref's scripted results in order, raising exceptions."""
    queues = {ref: list(items) for ref, items in script.items()}

    async def lookup(ref: str):
        item = queues[ref].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return lookup


@pytest.fixture
def harness():
    open_rows: list = []
    trading_state = SimpleNamespace(pause=AsyncMock())
    order_manager = SimpleNamespace(
        wait_for_entries_idle=AsyncMock(return_value=True),
        adopt_entry_fill=AsyncMock(return_value=True),
        close_position=AsyncMock(return_value=CloseOutcome.CLOSED),
    )
    repo = SimpleNamespace(
        get_active_trades=AsyncMock(return_value=[]),
        get_open_trades=AsyncMock(return_value=open_rows),
    )
    broker = AsyncMock()
    broker.cancel_order = AsyncMock(return_value=True)
    broker.get_execution_summary = AsyncMock(return_value=None)
    sleep = AsyncMock()
    return SimpleNamespace(
        trading_state=trading_state,
        order_manager=order_manager,
        repo=repo,
        broker=broker,
        sleep=sleep,
        open_rows=open_rows,
    )


async def _run(h, **kwargs):
    return await flatten_positions(
        h.trading_state,
        h.order_manager,
        h.broker,
        h.repo,
        sleep=h.sleep,
        clock=lambda: 0.0,
        **kwargs,
    )


async def test_open_closed_and_working_entry_cancelled(harness):
    h = harness
    a = _row("AAPL", TradeStatus.OPEN)
    b = _row("MSFT", TradeStatus.SUBMITTED)
    h.repo.get_active_trades.return_value = [a, b]
    h.repo.get_open_trades.return_value = [a]
    h.broker.find_order_by_ref.side_effect = _scripted(
        {
            f"{b.id}:entry": [
                _order("WORKING", 0, "B1", b.id),
                _order("REJECTED", 0, "B1", b.id),
            ]
        }
    )

    results = await _run(h)

    h.trading_state.pause.assert_awaited_once_with("flatten")
    h.order_manager.close_position.assert_awaited_once_with(a.id, "MANUAL")
    h.broker.cancel_order.assert_awaited_once_with("B1")
    assert results == [
        {"trade_id": str(b.id), "symbol": "MSFT", "outcome": "ENTRY_CANCELLED"},
        {"trade_id": str(a.id), "symbol": "AAPL", "outcome": "CLOSED"},
    ]


@pytest.mark.parametrize(
    "cancel_result",
    [False, RuntimeError("broker down")],
    ids=["cancel-returns-false", "cancel-raises"],
)
async def test_cancel_false_or_raises_fails_that_trade_only(harness, cancel_result):
    h = harness
    a = _row("AAPL", TradeStatus.SUBMITTED)
    b = _row("MSFT", TradeStatus.SUBMITTED)
    h.repo.get_active_trades.return_value = [a, b]
    h.broker.find_order_by_ref.side_effect = _scripted(
        {
            f"{a.id}:entry": [_order("WORKING", 0, "A1", a.id)],
            f"{b.id}:entry": [
                _order("WORKING", 0, "B1", b.id),
                _order("REJECTED", 0, "B1", b.id),
            ],
        }
    )
    h.broker.cancel_order.side_effect = [cancel_result, True]

    results = await _run(h)

    assert results == [
        {"trade_id": str(a.id), "symbol": "AAPL", "outcome": "ENTRY_CANCEL_FAILED"},
        {"trade_id": str(b.id), "symbol": "MSFT", "outcome": "ENTRY_CANCELLED"},
    ]
    h.order_manager.close_position.assert_not_awaited()


async def test_filled_during_cancel_is_adopted(harness):
    h = harness
    b = _row("MSFT", TradeStatus.SUBMITTED)
    filled = _order("FILLED", 5, "B1", b.id)
    h.repo.get_active_trades.return_value = [b]
    h.broker.find_order_by_ref.side_effect = _scripted(
        {f"{b.id}:entry": [_order("WORKING", 0, "B1", b.id), filled]}
    )
    h.broker.get_execution_summary.return_value = None

    async def adopt(trade_id, fill):
        h.open_rows.append(b)
        return True

    h.order_manager.adopt_entry_fill.side_effect = adopt

    results = await _run(h)

    h.broker.cancel_order.assert_awaited_once_with("B1")
    h.order_manager.adopt_entry_fill.assert_awaited_once_with(b.id, filled)
    h.order_manager.close_position.assert_awaited_once_with(b.id, "MANUAL")
    assert results == [
        {"trade_id": str(b.id), "symbol": "MSFT", "outcome": "ENTRY_ADOPTED"},
        {"trade_id": str(b.id), "symbol": "MSFT", "outcome": "CLOSED"},
    ]


async def test_already_filled_entry_adopted_without_cancel(harness):
    h = harness
    b = _row("MSFT", TradeStatus.SUBMITTED)
    summary = _order("FILLED", 5, "B1", b.id)
    h.repo.get_active_trades.return_value = [b]
    h.broker.find_order_by_ref.side_effect = _scripted(
        {f"{b.id}:entry": [_order("FILLED", 5, "B1", b.id)]}
    )
    h.broker.get_execution_summary.return_value = summary

    async def adopt(trade_id, fill):
        h.open_rows.append(b)
        return True

    h.order_manager.adopt_entry_fill.side_effect = adopt

    results = await _run(h)

    h.broker.cancel_order.assert_not_awaited()
    h.order_manager.adopt_entry_fill.assert_awaited_once_with(b.id, summary)
    h.order_manager.close_position.assert_awaited_once_with(b.id, "MANUAL")
    assert results == [
        {"trade_id": str(b.id), "symbol": "MSFT", "outcome": "ENTRY_ADOPTED"},
        {"trade_id": str(b.id), "symbol": "MSFT", "outcome": "CLOSED"},
    ]
