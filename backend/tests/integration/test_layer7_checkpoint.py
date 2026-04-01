"""
Layer 7 integration checkpoint — requires live Docker services.

Verifies (against real DB and Redis):
  (1) trade row created at PENDING before broker call (6B.8)
  (2) status transitions SUBMITTED → OPEN on fill (6B.8)
  (3) PRE_SUBMISSION and POST_CONFIRMATION audit entries written (7.1)
  (4) trade row readable from DB with correct final status (7.1)
  (5) trade event published to trade_events Redis channel (7.2)

Run after:
    docker-compose up -d
    docker-compose exec backend alembic upgrade head

Then:
    docker-compose exec backend pytest tests/integration/test_layer7_checkpoint.py -v
"""

import json
import os
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.execution.trade_handler import TRADE_EVENTS_CHANNEL, TradeHandler
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest
from app.data.cache import RedisCache
from app.db.models.trade import TradeStatus
from app.db.repositories.trade_repo import TradeRepo

_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://trading:trading@localhost:5432/trading_bot",
)
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def broker() -> MockBroker:
    b = MockBroker()
    await b.connect()
    yield b
    await b.disconnect()


@pytest_asyncio.fixture
async def cache() -> RedisCache:
    c = RedisCache(_REDIS_URL)
    yield c
    await c.close()


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(_DB_URL, echo=False)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    """Returns an async session factory compatible with OrderManager."""
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _factory():
        async with factory() as sess:
            yield sess

    return _factory


@pytest_asyncio.fixture
async def session(engine) -> AsyncSession:
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()


def _valid_request(**kwargs) -> TradeRequest:
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


# ---------------------------------------------------------------------------
# Checkpoint (1): trade row created at PENDING before broker call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_created_at_pending_before_broker_call(
    broker: MockBroker,
    session_factory,
    session: AsyncSession,
) -> None:
    """
    6B.8: OrderManager creates a PENDING row before place_order() is called.
    Verified by intercepting place_order and reading the DB inside it.
    """
    order_manager = OrderManager(broker, RiskManager(), session_factory)
    req = _valid_request()

    pending_status_observed = []

    original = broker.place_order

    async def spy_place_order(order_request):
        # Inside broker call — the trade should already be at SUBMITTED
        repo = TradeRepo(session)
        trade = await repo.get_by_id(req.trade_id)
        if trade:
            pending_status_observed.append(trade.status)
        return await original(order_request)

    broker.place_order = spy_place_order
    await order_manager.submit_order(req)
    broker.place_order = original

    # Should have been at SUBMITTED (PENDING → SUBMITTED before broker call)
    assert pending_status_observed, "Broker spy was not called"
    assert pending_status_observed[0] in (TradeStatus.PENDING, TradeStatus.SUBMITTED)


# ---------------------------------------------------------------------------
# Checkpoint (2): status transitions to OPEN on fill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_status_open_after_fill(
    broker: MockBroker,
    session_factory,
    session: AsyncSession,
) -> None:
    order_manager = OrderManager(broker, RiskManager(), session_factory)
    req = _valid_request()

    validation, order_result = await order_manager.submit_order(req)
    assert order_result.status == "FILLED"

    repo = TradeRepo(session)
    trade = await repo.get_by_id(req.trade_id)
    assert trade is not None
    assert trade.status == TradeStatus.OPEN


# ---------------------------------------------------------------------------
# Checkpoint (3): audit entries written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_entries_written(
    broker: MockBroker,
    session_factory,
) -> None:
    order_manager = OrderManager(broker, RiskManager(), session_factory)
    req = _valid_request()

    with patch("app.core.execution.order_manager.audit_logger") as mock_audit:
        await order_manager.submit_order(req)

    messages = [c.args[0] for c in mock_audit.info.call_args_list]
    assert "PRE_SUBMISSION" in messages
    assert "POST_CONFIRMATION" in messages


# ---------------------------------------------------------------------------
# Checkpoint (4): trade row readable with take_profit and R:R fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_fill_persists_trade_to_db(
    broker: MockBroker,
    cache: RedisCache,
    session_factory,
    session: AsyncSession,
) -> None:
    """
    Full flow: submit_order() → on_fill() → trade row readable from DB.
    """
    order_manager = OrderManager(broker, RiskManager(), session_factory)
    trade_handler = TradeHandler(cache)
    req = _valid_request()

    validation, order_result = await order_manager.submit_order(req)
    assert order_result.status == "FILLED"

    trade = await trade_handler.on_fill(req, validation, order_result, session)

    repo = TradeRepo(session)
    fetched = await repo.get_by_id(trade.id)

    assert fetched is not None
    assert fetched.symbol == "AAPL"
    assert fetched.stop_loss_price == req.stop_loss_price
    assert str(fetched.direction.value) == "BUY"
    assert fetched.status == TradeStatus.OPEN
    # 6B fields
    assert fetched.take_profit_price is not None
    assert fetched.reward_to_risk_ratio is not None


# ---------------------------------------------------------------------------
# Checkpoint (5): trade event published to Redis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_fill_publishes_trade_event_to_redis(
    broker: MockBroker,
    cache: RedisCache,
    session_factory,
    session: AsyncSession,
) -> None:
    order_manager = OrderManager(broker, RiskManager(), session_factory)
    trade_handler = TradeHandler(cache)
    req = _valid_request(symbol="MSFT")

    validation, order_result = await order_manager.submit_order(req)

    published: list[tuple[str, str]] = []
    original_publish = cache.publish

    async def capturing_publish(channel: str, message: str) -> None:
        published.append((channel, message))
        await original_publish(channel, message)

    cache.publish = capturing_publish  # type: ignore[method-assign]
    await trade_handler.on_fill(req, validation, order_result, session)
    cache.publish = original_publish  # type: ignore[method-assign]

    assert len(published) == 1
    channel, raw = published[0]
    assert channel == TRADE_EVENTS_CHANNEL
    event = json.loads(raw)
    assert event["event"] == "trade_executed"
    assert event["payload"]["symbol"] == "MSFT"
    assert event["payload"]["direction"] == "BUY"
    # 6B fields in event
    assert "take_profit_price" in event["payload"]
    assert "reward_to_risk_ratio" in event["payload"]


# ---------------------------------------------------------------------------
# Rejection: no DB row written
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_rejection_writes_no_db_row(
    broker: MockBroker,
    session_factory,
    session: AsyncSession,
) -> None:
    """A rejected trade must leave zero rows in the DB for that trade_id."""
    order_manager = OrderManager(broker, RiskManager(), session_factory)
    req = _valid_request(stop_loss_price=None)

    with pytest.raises(RiskRejectionError):
        await order_manager.submit_order(req)

    repo = TradeRepo(session)
    fetched = await repo.get_by_id(req.trade_id)
    assert fetched is None, "Rejected trade should not appear in DB"
