"""
Unit tests for TradeHandler (Layer 7.2 / 6B.8).

After 6B.8, TradeHandler.on_fill() no longer creates the trade row.
Instead it looks up the existing trade (created at PENDING by OrderManager)
and publishes the trade_executed event to Redis.

Uses an in-memory fake cache and a mock DB session — no Redis or PostgreSQL needed.
"""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.brokers.base import OrderResult
from app.core.execution.trade_handler import TRADE_EVENTS_CHANNEL, TradeHandler
from app.core.risk.manager import TradeRequest, ValidationResult
from app.db.models.trade import ExitReason, TradeDirection, TradeStatus


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCache:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


class FakeTrade:
    """Simulates what TradeRepo.get_by_id() returns after 6B.8."""

    def __init__(self, request: TradeRequest, order_result: OrderResult) -> None:
        self.id = request.trade_id
        self.symbol = request.symbol
        self.direction = TradeDirection(request.direction)
        self.quantity = order_result.filled_quantity
        self.entry_price = request.entry_price  # requested price (set at PENDING)
        self.stop_loss_price = request.stop_loss_price
        self.risk_amount = Decimal("50")
        self.account_balance_at_entry = Decimal("100000")
        self.strategy_id = request.strategy_id
        self.status = TradeStatus.OPEN


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


def _make_order_result(request: TradeRequest, **kwargs) -> OrderResult:
    defaults = {
        "trade_id": request.trade_id,
        "broker_order_id": "42",
        "status": "FILLED",
        "filled_quantity": request.quantity,
        "avg_fill_price": Decimal("200.01"),
        "timestamp": datetime.now(timezone.utc),
    }
    defaults.update(kwargs)
    return OrderResult(**defaults)


def _make_validation(request: TradeRequest) -> ValidationResult:
    return ValidationResult(
        trade_id=request.trade_id,
        risk_amount=Decimal("50.05"),
        max_quantity=200,
        account_balance_at_entry=Decimal("100000"),
        take_profit_price=Decimal("210"),
        reward_to_risk_ratio=Decimal("2.0"),
    )


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()


@pytest.fixture
def handler(cache: FakeCache) -> TradeHandler:
    return TradeHandler(cache)


def _make_mock_session(request: TradeRequest, order_result: OrderResult):
    """Returns a patched TradeRepo whose get_by_id returns a FakeTrade."""
    session = MagicMock()
    fake_trade = FakeTrade(request, order_result)

    class PatchedRepo:
        def __init__(self, _session):
            pass

        async def get_by_id(self, trade_id):
            return fake_trade

    return session, PatchedRepo, fake_trade


# ---------------------------------------------------------------------------
# on_fill looks up existing trade (not creates)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_fill_looks_up_existing_trade(handler: TradeHandler, monkeypatch) -> None:
    req = _request()
    result = _make_order_result(req)
    validation = _make_validation(req)
    session, PatchedRepo, fake_trade = _make_mock_session(req, result)

    import app.core.execution.trade_handler as th_module
    monkeypatch.setattr(th_module, "TradeRepo", PatchedRepo)

    trade = await handler.on_fill(req, validation, result, session)
    assert trade.id == req.trade_id
    assert trade.symbol == "AAPL"


@pytest.mark.asyncio
async def test_on_fill_raises_when_trade_not_found(
    handler: TradeHandler, monkeypatch
) -> None:
    """on_fill must raise if OrderManager didn't create the trade row."""
    req = _request()
    result = _make_order_result(req)
    validation = _make_validation(req)
    session = MagicMock()

    class RepoReturningNone:
        def __init__(self, _session):
            pass

        async def get_by_id(self, trade_id):
            return None

    import app.core.execution.trade_handler as th_module
    monkeypatch.setattr(th_module, "TradeRepo", RepoReturningNone)

    with pytest.raises(RuntimeError, match="not found"):
        await handler.on_fill(req, validation, result, session)


# ---------------------------------------------------------------------------
# Trade event published to Redis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_fill_publishes_to_trade_events_channel(
    handler: TradeHandler, cache: FakeCache, monkeypatch
) -> None:
    req = _request()
    result = _make_order_result(req)
    validation = _make_validation(req)
    session, PatchedRepo, _ = _make_mock_session(req, result)

    import app.core.execution.trade_handler as th_module
    monkeypatch.setattr(th_module, "TradeRepo", PatchedRepo)

    await handler.on_fill(req, validation, result, session)

    assert len(cache.published) == 1
    channel, raw = cache.published[0]
    assert channel == TRADE_EVENTS_CHANNEL


@pytest.mark.asyncio
async def test_on_fill_published_event_format(
    handler: TradeHandler, cache: FakeCache, monkeypatch
) -> None:
    req = _request()
    result = _make_order_result(req)
    validation = _make_validation(req)
    session, PatchedRepo, _ = _make_mock_session(req, result)

    import app.core.execution.trade_handler as th_module
    monkeypatch.setattr(th_module, "TradeRepo", PatchedRepo)

    await handler.on_fill(req, validation, result, session)

    _, raw = cache.published[0]
    event = json.loads(raw)

    assert event["event"] == "trade_executed"
    assert "payload" in event
    assert "timestamp" in event
    payload = event["payload"]
    assert payload["symbol"] == "AAPL"
    assert payload["direction"] == "BUY"
    assert payload["status"] == "OPEN"


@pytest.mark.asyncio
async def test_on_fill_event_includes_take_profit_and_rr(
    handler: TradeHandler, cache: FakeCache, monkeypatch
) -> None:
    """6B: published event must include take_profit_price and reward_to_risk_ratio."""
    req = _request()
    result = _make_order_result(req)
    validation = _make_validation(req)  # take_profit=210, rr=2.0
    session, PatchedRepo, _ = _make_mock_session(req, result)

    import app.core.execution.trade_handler as th_module
    monkeypatch.setattr(th_module, "TradeRepo", PatchedRepo)

    await handler.on_fill(req, validation, result, session)

    _, raw = cache.published[0]
    payload = json.loads(raw)["payload"]
    assert payload["take_profit_price"] == "210"
    assert payload["reward_to_risk_ratio"] == "2.0"


# ---------------------------------------------------------------------------
# Non-fill status rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_fill_rejects_rejected_status(
    handler: TradeHandler, monkeypatch
) -> None:
    req = _request()
    result = _make_order_result(req, status="REJECTED")
    validation = _make_validation(req)
    session = MagicMock()

    with pytest.raises(ValueError, match="non-fill status"):
        await handler.on_fill(req, validation, result, session)


@pytest.mark.asyncio
async def test_on_fill_rejects_error_status(
    handler: TradeHandler, monkeypatch
) -> None:
    req = _request()
    result = _make_order_result(req, status="ERROR")
    validation = _make_validation(req)
    session = MagicMock()

    with pytest.raises(ValueError, match="non-fill status"):
        await handler.on_fill(req, validation, result, session)


@pytest.mark.asyncio
async def test_on_fill_accepts_partial_fill(
    handler: TradeHandler, cache: FakeCache, monkeypatch
) -> None:
    req = _request(quantity=Decimal("100"))
    result = _make_order_result(req, status="PARTIAL", filled_quantity=Decimal("50"))
    validation = _make_validation(req)
    session, PatchedRepo, _ = _make_mock_session(req, result)

    import app.core.execution.trade_handler as th_module
    monkeypatch.setattr(th_module, "TradeRepo", PatchedRepo)

    trade = await handler.on_fill(req, validation, result, session)
    assert trade is not None
    assert len(cache.published) == 1
