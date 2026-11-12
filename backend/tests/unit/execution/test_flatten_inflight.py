"""Deterministic test that flatten_positions waits for a strategy entry already in flight."""

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.brokers.mock.client import MockBroker
from app.core.execution.flatten import flatten_positions
from app.core.execution.order_close import CloseOutcome
from app.core.execution.order_manager import OrderManager
from app.core.risk.manager import RiskManager, TradeRequest
from app.db.models.trade import TradeStatus


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


class _TradingState:
    """Records a pause the same way the real trading state does, without a cache."""

    def __init__(self, paused: dict) -> None:
        self._paused = paused

    async def pause(self, reason: str = "") -> None:
        self._paused["v"] = True


async def test_flatten_waits_for_in_flight_entry_then_closes_it() -> None:
    broker = GatedBroker()
    await broker.connect()
    paused = {"v": False}

    async def is_paused() -> bool:
        return paused["v"]

    trading_state = _TradingState(paused)
    manager = OrderManager(broker, RiskManager(), order_retry_attempts=1, is_paused=is_paused)
    manager.close_position = AsyncMock(return_value=CloseOutcome.CLOSED)

    request = _request(strategy_id=uuid.uuid4())
    submit = asyncio.create_task(manager.submit_order(request))
    flat = None
    try:
        await broker.entered.wait()

        repo = _repo(submit, request)
        flat = asyncio.create_task(
            flatten_positions(trading_state, manager, broker, repo, idle_timeout=5.0)
        )
        for _ in range(10):
            await asyncio.sleep(0)

        assert not flat.done()
        assert paused["v"] is True

        broker.gate.set()
        await submit
        results = await flat
    finally:
        broker.gate.set()
        if not submit.done():
            submit.cancel()
        if flat is not None and not flat.done():
            flat.cancel()

    manager.close_position.assert_awaited_once_with(request.trade_id, "MANUAL")
    assert {
        "trade_id": str(request.trade_id),
        "symbol": request.symbol,
        "outcome": "CLOSED",
    } in results
    assert all(r.get("outcome") != "ENTRIES_STILL_IN_FLIGHT" for r in results)


async def test_flatten_reports_entries_still_in_flight_on_timeout() -> None:
    broker = GatedBroker()
    await broker.connect()
    paused = {"v": False}

    async def is_paused() -> bool:
        return paused["v"]

    trading_state = _TradingState(paused)
    manager = OrderManager(broker, RiskManager(), order_retry_attempts=1, is_paused=is_paused)
    manager.close_position = AsyncMock(return_value=CloseOutcome.CLOSED)

    request = _request(strategy_id=uuid.uuid4())
    submit = asyncio.create_task(manager.submit_order(request))
    try:
        await broker.entered.wait()

        repo = _repo(submit, request)
        results = await flatten_positions(
            trading_state, manager, broker, repo, idle_timeout=0.05
        )

        assert results[0] == {"outcome": "ENTRIES_STILL_IN_FLIGHT"}
        assert paused["v"] is True
    finally:
        broker.gate.set()
        await submit


def _repo(submit: asyncio.Task, request: TradeRequest) -> SimpleNamespace:
    """Fake repo: the entry shows as an OPEN trade only once its submit_order has finished."""

    async def get_open_trades() -> list:
        if submit.done():
            return [
                SimpleNamespace(id=request.trade_id, symbol=request.symbol, status=TradeStatus.OPEN)
            ]
        return []

    return SimpleNamespace(
        get_active_trades=AsyncMock(return_value=[]),
        get_open_trades=get_open_trades,
    )
