"""
Unit tests for MarketDataFeed (Layer 5.2).

Uses MockBroker and an in-memory fake cache — no Redis or DB required.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.brokers.mock.client import MockBroker
from app.brokers.base import PriceUpdate
from app.data.feed import MarketDataFeed, WATCHLIST_PRICES_CHANNEL
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Fake cache for testing (no Redis needed)
# ---------------------------------------------------------------------------


class FakeCache:
    """In-memory stand-in for RedisCache."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []  # (channel, message)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


# ---------------------------------------------------------------------------
# Fake SymbolRepo for testing (no DB needed)
# ---------------------------------------------------------------------------


class FakeSymbol:
    def __init__(self, ticker: str, is_active: bool = True) -> None:
        self.ticker = ticker
        self.is_active = is_active


class FakeSymbolRepo:
    def __init__(self, tickers: list[str]) -> None:
        self._tickers = tickers

    async def get_all(self, active_only: bool = False) -> list[FakeSymbol]:
        return [FakeSymbol(t) for t in self._tickers]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def broker() -> MockBroker:
    b = MockBroker()
    await b.connect()
    return b


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()


@pytest.fixture
def feed(broker: MockBroker, cache: FakeCache) -> MarketDataFeed:
    return MarketDataFeed(broker, cache)


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_subscribes_active_symbols(
    feed: MarketDataFeed, broker: MockBroker
) -> None:
    fake_session = MagicMock()

    # Patch SymbolRepo inside feed.start by monkeypatching the import
    import app.data.feed as feed_module
    original = feed_module.SymbolRepo

    class PatchedRepo(FakeSymbolRepo):
        def __init__(self, session):
            super().__init__(["AAPL", "MSFT"])

    feed_module.SymbolRepo = PatchedRepo
    try:
        await feed.start(fake_session)
    finally:
        feed_module.SymbolRepo = original

    assert "AAPL" in feed.subscribed_tickers
    assert "MSFT" in feed.subscribed_tickers


@pytest.mark.asyncio
async def test_start_is_idempotent(feed: MarketDataFeed) -> None:
    """Calling start twice should not double-subscribe."""
    fake_session = MagicMock()
    import app.data.feed as feed_module
    original = feed_module.SymbolRepo

    class PatchedRepo(FakeSymbolRepo):
        def __init__(self, session):
            super().__init__(["AAPL"])

    feed_module.SymbolRepo = PatchedRepo
    try:
        await feed.start(fake_session)
        await feed.start(fake_session)  # second call should be no-op
    finally:
        feed_module.SymbolRepo = original

    assert len(feed.subscribed_tickers) == 1


# ---------------------------------------------------------------------------
# add_symbol / remove_symbol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_symbol(feed: MarketDataFeed, broker: MockBroker) -> None:
    await feed.add_symbol("TSLA")
    assert "TSLA" in feed.subscribed_tickers


@pytest.mark.asyncio
async def test_add_symbol_is_idempotent(feed: MarketDataFeed) -> None:
    await feed.add_symbol("TSLA")
    await feed.add_symbol("TSLA")
    assert "TSLA" in feed.subscribed_tickers


@pytest.mark.asyncio
async def test_remove_symbol(
    feed: MarketDataFeed, broker: MockBroker, cache: FakeCache
) -> None:
    await feed.add_symbol("GOOG")
    # Seed a price so we can confirm it's deleted
    await cache.set("price:GOOG", '{"ticker":"GOOG","price":"150.00"}')

    await feed.remove_symbol("GOOG")

    assert "GOOG" not in feed.subscribed_tickers
    assert await cache.get("price:GOOG") is None


@pytest.mark.asyncio
async def test_remove_nonexistent_symbol_is_noop(feed: MarketDataFeed) -> None:
    await feed.remove_symbol("FAKE")  # should not raise


# ---------------------------------------------------------------------------
# Price tick forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_price_tick_writes_to_cache(
    feed: MarketDataFeed, broker: MockBroker, cache: FakeCache
) -> None:
    await feed.add_symbol("AAPL")
    await broker.simulate_price_tick("AAPL", Decimal("185.00"))

    stored = await cache.get("price:AAPL")
    assert stored is not None
    assert "185.00" in stored


@pytest.mark.asyncio
async def test_price_tick_publishes_to_channel(
    feed: MarketDataFeed, broker: MockBroker, cache: FakeCache
) -> None:
    await feed.add_symbol("AAPL")
    await broker.simulate_price_tick("AAPL", Decimal("185.00"))

    assert len(cache.published) == 1
    channel, message = cache.published[0]
    assert channel == WATCHLIST_PRICES_CHANNEL
    assert "185.00" in message


@pytest.mark.asyncio
async def test_multiple_ticks_each_update_cache(
    feed: MarketDataFeed, broker: MockBroker, cache: FakeCache
) -> None:
    await feed.add_symbol("NVDA")
    await broker.simulate_price_tick("NVDA", Decimal("500.00"))
    await broker.simulate_price_tick("NVDA", Decimal("505.50"))

    stored = await cache.get("price:NVDA")
    assert "505.50" in stored  # latest price wins


# ---------------------------------------------------------------------------
# MockBroker historical data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_broker_get_historical_data_returns_bars(broker: MockBroker) -> None:
    bars = await broker.get_historical_data("AAPL")

    assert len(bars) > 200  # ~252 trading days in a year
    assert all(b.close > 0 for b in bars)
    assert all(b.high >= b.close for b in bars)
    assert all(b.low <= b.close for b in bars)
    assert all(b.volume > 0 for b in bars)


@pytest.mark.asyncio
async def test_mock_broker_historical_data_is_deterministic(broker: MockBroker) -> None:
    bars1 = await broker.get_historical_data("AAPL")
    bars2 = await broker.get_historical_data("AAPL")

    assert [b.close for b in bars1] == [b.close for b in bars2]


@pytest.mark.asyncio
async def test_mock_broker_different_symbols_produce_different_data(
    broker: MockBroker,
) -> None:
    aapl = await broker.get_historical_data("AAPL")
    msft = await broker.get_historical_data("MSFT")

    assert [b.close for b in aapl] != [b.close for b in msft]
