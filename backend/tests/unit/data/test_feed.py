"""
Unit tests for MarketDataFeed (Layer 5.2).

Uses MockBroker and an in-memory fake cache — no Redis or DB required.
"""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.brokers.mock.client import MockBroker
from app.data.feed import WATCHLIST_PRICES_CHANNEL, MarketDataFeed

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
    tickers: list[str] = []

    def __init__(self, session) -> None:
        pass

    async def get_all(self, active_only: bool = False) -> list[FakeSymbol]:
        return [FakeSymbol(t) for t in self.tickers]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _session_factory():
    return _Session()


class FakeStrategy:
    def __init__(self, symbols: list[str]) -> None:
        self.config = {"symbols": symbols}


class FakeStrategyRepo:
    symbols: list[list[str]] = []

    def __init__(self, session) -> None:
        pass

    async def get_enabled(self):
        return [FakeStrategy(s) for s in self.symbols]


class FakeTradeRepo:
    symbols: list[str] = []

    def __init__(self, session) -> None:
        pass

    async def get_active_trades(self):
        return [SimpleNamespace(symbol=s) for s in self.symbols]


@pytest.fixture
async def broker() -> MockBroker:
    b = MockBroker()
    await b.connect()
    return b


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()


@pytest.fixture
def repos(monkeypatch):
    import app.data.feed as feed_module

    FakeSymbolRepo.tickers = []
    FakeStrategyRepo.symbols = []
    FakeTradeRepo.symbols = []
    monkeypatch.setattr(feed_module, "SymbolRepo", FakeSymbolRepo)
    monkeypatch.setattr(feed_module, "StrategyRepo", FakeStrategyRepo)
    monkeypatch.setattr(feed_module, "TradeRepo", FakeTradeRepo)
    return SimpleNamespace(watchlist=FakeSymbolRepo, strategies=FakeStrategyRepo, trades=FakeTradeRepo)


@pytest.fixture
def feed(broker: MockBroker, cache: FakeCache, repos) -> MarketDataFeed:
    return MarketDataFeed(broker, cache, _session_factory)


# ---------------------------------------------------------------------------
# desired_symbols() / sync()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_desired_symbols_is_union(feed: MarketDataFeed, repos) -> None:
    repos.watchlist.tickers = ["aapl", "MSFT"]
    repos.strategies.symbols = [["msft", "NVDA"], ["amd"]]
    repos.trades.symbols = ["TSLA", "AAPL"]
    assert await feed.desired_symbols() == {"AAPL", "MSFT", "NVDA", "AMD", "TSLA"}


@pytest.mark.asyncio
async def test_sync_subscribes_new_and_unsubscribes_removed(feed: MarketDataFeed, repos) -> None:
    repos.watchlist.tickers = ["AAPL", "MSFT"]
    await feed.sync()
    assert feed.subscribed_tickers == {"AAPL", "MSFT"}
    repos.watchlist.tickers = ["AAPL", "NVDA"]
    await feed.sync()
    assert feed.subscribed_tickers == {"AAPL", "NVDA"}


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_skips_when_disconnected(
    feed: MarketDataFeed, broker: MockBroker, repos
) -> None:
    repos.watchlist.tickers = ["AAPL"]
    await broker.disconnect()
    await feed.sync()
    assert feed.subscribed_tickers == frozenset()
    await broker.connect()
    await feed.sync()
    await feed.sync()
    assert feed.subscribed_tickers == {"AAPL"}


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
async def test_price_tick_writes_key_with_price_and_timestamp(
    feed: MarketDataFeed, broker: MockBroker, cache: FakeCache
) -> None:
    await feed.add_symbol("AAPL")
    await broker.simulate_price_tick("AAPL", Decimal("185.00"))

    data = json.loads(await cache.get("price:AAPL"))
    assert data["price"] == "185.00"
    assert "timestamp" in data
    assert feed.last_tick_at("aapl") is not None


@pytest.mark.asyncio
async def test_price_tick_publishes_price_update_envelope(
    feed: MarketDataFeed, broker: MockBroker, cache: FakeCache
) -> None:
    await feed.add_symbol("AAPL")
    await broker.simulate_price_tick("AAPL", Decimal("185.00"))

    assert len(cache.published) == 1
    channel, message = cache.published[0]
    assert channel == WATCHLIST_PRICES_CHANNEL
    msg = json.loads(message)
    assert msg["event"] == "price_update"
    assert msg["payload"] == {"symbol": "AAPL", "price": "185.00", "change_pct": None}


@pytest.mark.asyncio
async def test_listener_called_before_redis_write_and_failure_is_isolated(
    feed: MarketDataFeed, broker: MockBroker, cache: FakeCache
) -> None:
    order: list[str] = []
    orig_set = cache.set

    async def recording_set(*a, **kw):
        order.append("set")
        await orig_set(*a, **kw)

    cache.set = recording_set

    async def bad(update) -> None:
        order.append("bad")
        raise RuntimeError("boom")

    async def good(update) -> None:
        order.append("good")

    feed.add_listener(bad)
    feed.add_listener(good)
    await feed.add_symbol("AAPL")
    await broker.simulate_price_tick("AAPL", Decimal("10"))
    assert order == ["bad", "good", "set"]
    assert cache.published


@pytest.mark.asyncio
async def test_multiple_ticks_each_update_cache(
    feed: MarketDataFeed, broker: MockBroker, cache: FakeCache
) -> None:
    await feed.add_symbol("NVDA")
    await broker.simulate_price_tick("NVDA", Decimal("500.00"))
    await broker.simulate_price_tick("NVDA", Decimal("505.50"))

    assert json.loads(await cache.get("price:NVDA"))["price"] == "505.50"


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
