"""
MarketDataFeed — manages live price subscriptions for all active watched symbols.

On startup, loads all active symbols from the database and subscribes to the
broker's price feed. Each tick is:
  1. Written to Redis as  price:{ticker}  (latest price cache, no TTL)
  2. Published to Redis channel  watchlist_prices  (for the WebSocket handler)

Symbols can be added or removed dynamically without restarting the feed.

Depends on: BaseBroker (Layer 4), RedisCache (Layer 5.1), SymbolRepo (Layer 3).
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import BaseBroker, PriceUpdate
from app.data.cache import RedisCache
from app.db.repositories.symbol_repo import SymbolRepo
from app.monitoring.logger import system_logger

# Redis key format for the latest price of a ticker
_PRICE_KEY = "price:{ticker}"

# Redis pub/sub channel for live price updates (consumed by WebSocket handler)
WATCHLIST_PRICES_CHANNEL = "watchlist_prices"


class MarketDataFeed:
    """
    Subscribes to live price ticks for all active watched symbols and
    forwards them to Redis.

    Lifecycle:
        feed = MarketDataFeed(broker, cache)
        await feed.start(session)          # subscribe to initial symbols
        await feed.add_symbol("TSLA")      # dynamic add (no restart needed)
        await feed.remove_symbol("TSLA")   # dynamic remove
    """

    def __init__(self, broker: BaseBroker, cache: RedisCache) -> None:
        self._broker = broker
        self._cache = cache
        self._subscribed: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, session: AsyncSession) -> None:
        """
        Load all active symbols from the DB and subscribe to the price feed.

        Call once at application startup (after broker is connected).
        Idempotent: already-subscribed tickers are skipped.
        """
        repo = SymbolRepo(session)
        symbols = await repo.get_all(active_only=True)
        tickers = [s.ticker for s in symbols if s.ticker not in self._subscribed]

        if tickers:
            await self._broker.subscribe_price_feed(tickers, self._on_price_update)
            self._subscribed.update(tickers)
            system_logger.info(
                "MarketDataFeed started",
                extra={"tickers": tickers, "count": len(tickers)},
            )
        else:
            system_logger.info("MarketDataFeed started with no active symbols")

    # ------------------------------------------------------------------
    # Dynamic symbol management
    # ------------------------------------------------------------------

    async def add_symbol(self, ticker: str) -> None:
        """
        Subscribe to live prices for a newly added symbol.

        Safe to call if already subscribed — will be a no-op.
        """
        ticker = ticker.upper()
        if ticker in self._subscribed:
            return
        await self._broker.subscribe_price_feed([ticker], self._on_price_update)
        self._subscribed.add(ticker)
        system_logger.info("MarketDataFeed: added symbol", extra={"ticker": ticker})

    async def remove_symbol(self, ticker: str) -> None:
        """
        Unsubscribe from live prices for a removed symbol and clear its
        cached price from Redis.

        Safe to call if not subscribed — will be a no-op.
        """
        ticker = ticker.upper()
        if ticker not in self._subscribed:
            return
        await self._broker.unsubscribe_price_feed(ticker)
        self._subscribed.discard(ticker)
        await self._cache.delete(_PRICE_KEY.format(ticker=ticker))
        system_logger.info("MarketDataFeed: removed symbol", extra={"ticker": ticker})

    @property
    def subscribed_tickers(self) -> frozenset[str]:
        """Return the current set of subscribed ticker symbols."""
        return frozenset(self._subscribed)

    # ------------------------------------------------------------------
    # Price tick handler
    # ------------------------------------------------------------------

    async def _on_price_update(self, update: PriceUpdate) -> None:
        """
        Called for every incoming price tick.

        Writes to Redis key-value store and publishes to pub/sub channel.
        Errors are logged but never propagated — a single bad tick must not
        crash the feed.
        """
        try:
            message = update.model_dump_json()
            await self._cache.set(_PRICE_KEY.format(ticker=update.ticker), message)
            await self._cache.publish(WATCHLIST_PRICES_CHANNEL, message)
        except Exception as exc:
            system_logger.error(
                "MarketDataFeed: failed to forward price update",
                extra={"ticker": update.ticker, "error": str(exc)},
            )
