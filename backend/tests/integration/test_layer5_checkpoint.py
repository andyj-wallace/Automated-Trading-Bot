"""
Layer 5 integration checkpoint — requires live Docker services.

Verifies:
  1. MarketDataFeed.start() subscribes to active symbols with MockBroker
  2. broker.simulate_price_tick() causes Redis to hold price:{ticker}
  3. HistoricalDataFetcher.fetch_and_store() writes rows to ohlcv_bars hypertable

Run after:
    docker-compose up -d
    docker-compose exec backend alembic upgrade head

Then:
    docker-compose exec backend pytest tests/integration/test_layer5_checkpoint.py -v
"""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.mock.client import MockBroker
from app.data.cache import RedisCache
from app.data.feed import MarketDataFeed
from app.data.historical import HistoricalDataFetcher
from app.db.repositories.ohlcv_repo import OHLCVRepo

# Use env vars when running inside the container; fall back to localhost for host-side runs.
_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://trading:trading@localhost:5432/trading_bot",
)
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Unique symbol that won't conflict with any real watched_symbols rows.
_TEST_SYMBOL = "TSTTICK"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cache():
    c = RedisCache(_REDIS_URL)
    yield c
    # Clean up test keys so reruns start fresh.
    await c.delete("price:AAPL")
    await c.delete(f"price:{_TEST_SYMBOL}")
    await c.close()


@pytest_asyncio.fixture
async def broker():
    b = MockBroker()
    await b.connect()
    yield b
    await b.disconnect()


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(_DB_URL, echo=False)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()  # undo all writes — keeps the DB clean between runs
    await engine.dispose()


# ---------------------------------------------------------------------------
# Checkpoint 1: MarketDataFeed + simulate_price_tick → Redis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_price_tick_reaches_redis(broker: MockBroker, cache: RedisCache) -> None:
    """
    With MockBroker and real Redis:
      feed.add_symbol("AAPL") + broker.simulate_price_tick()
      → price:AAPL key is written to Redis.
    """
    feed = MarketDataFeed(broker, cache)
    await feed.add_symbol("AAPL")

    await broker.simulate_price_tick("AAPL", Decimal("190.50"))

    raw = await cache.get("price:AAPL")
    assert raw is not None, "Expected 'price:AAPL' in Redis but got None"

    data = json.loads(raw)
    assert data["ticker"] == "AAPL"
    # Decimal may serialize as string or number depending on Pydantic version;
    # convert to Decimal for a precise comparison.
    assert Decimal(str(data["price"])) == Decimal("190.50")


@pytest.mark.asyncio
async def test_price_tick_publishes_to_watchlist_channel(
    broker: MockBroker, cache: RedisCache
) -> None:
    """
    simulate_price_tick() also publishes to the watchlist_prices pub/sub channel.
    Verified by subscribing before the tick fires and reading one message.
    """
    feed = MarketDataFeed(broker, cache)
    await feed.add_symbol("AAPL")

    # Collect publish calls via a thin wrapper around the real cache.
    published: list[tuple[str, str]] = []
    original_publish = cache.publish

    async def capturing_publish(channel: str, message: str) -> None:
        published.append((channel, message))
        await original_publish(channel, message)

    cache.publish = capturing_publish  # type: ignore[method-assign]

    await broker.simulate_price_tick("AAPL", Decimal("191.00"))

    cache.publish = original_publish  # type: ignore[method-assign]

    assert len(published) == 1
    channel, message = published[0]
    assert channel == "watchlist_prices"
    data = json.loads(message)
    assert data["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# Checkpoint 2: HistoricalDataFetcher → ohlcv_bars hypertable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_historical_fetcher_writes_hypertable(
    broker: MockBroker, session: AsyncSession
) -> None:
    """
    HistoricalDataFetcher.fetch_and_store() with MockBroker writes ~252
    daily OHLCV bars into the ohlcv_bars TimescaleDB hypertable.
    """
    repo = OHLCVRepo(session)

    # Pre-clean any leftover rows from a previous run (safe no-op if empty).
    await repo.delete_symbol(_TEST_SYMBOL)
    await session.flush()

    fetcher = HistoricalDataFetcher(broker)
    count = await fetcher.fetch_and_store(_TEST_SYMBOL, session)

    assert count > 200, f"Expected >200 bars from MockBroker, got {count}"

    bars = await repo.get_bars(
        _TEST_SYMBOL,
        start=datetime.now(timezone.utc) - timedelta(days=400),
        end=datetime.now(timezone.utc),
    )
    assert len(bars) > 200, "Rows written but not readable back from DB"
    assert all(b.symbol == _TEST_SYMBOL for b in bars)
    assert all(b.close > 0 for b in bars)
    assert all(b.high >= b.low for b in bars)
    # Session is rolled back by the fixture — DB is clean after the test.
