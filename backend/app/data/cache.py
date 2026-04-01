"""
RedisCache — thin async wrapper over redis.asyncio.

Provides the five operations used throughout the application:
  get / set / delete — key-value store for current prices and metrics
  publish / subscribe — pub/sub for real-time dashboard updates

All values are stored and returned as strings (decode_responses=True).
Callers are responsible for JSON serialisation before calling set/publish.

Usage:
    cache = RedisCache(settings.redis_url)
    await cache.set("price:AAPL", json.dumps({...}), ttl=60)
    await cache.publish("watchlist_prices", json.dumps({...}))

    async for message in cache.subscribe("watchlist_prices"):
        data = json.loads(message)
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis


class RedisCache:
    """
    Async Redis wrapper for the trading bot.

    A single instance is shared for the process lifetime (cached in
    app/dependencies.py). The underlying connection pool is managed by
    the redis.asyncio client.
    """

    def __init__(self, url: str) -> None:
        self._client: aioredis.Redis = aioredis.from_url(url, decode_responses=True)

    # ------------------------------------------------------------------
    # Key-value operations
    # ------------------------------------------------------------------

    async def get(self, key: str) -> str | None:
        """Return the value for key, or None if the key does not exist."""
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        """
        Store value at key.

        Args:
            key:   Redis key.
            value: String value (JSON-encode before calling if needed).
            ttl:   Optional expiry in seconds. No expiry if omitted.
        """
        await self._client.set(key, value, ex=ttl)

    async def delete(self, key: str) -> None:
        """Delete key. No-op if the key does not exist."""
        await self._client.delete(key)

    # ------------------------------------------------------------------
    # Pub/sub operations
    # ------------------------------------------------------------------

    async def publish(self, channel: str, message: str) -> None:
        """Publish a message to a Redis pub/sub channel."""
        await self._client.publish(channel, message)

    async def subscribe_many(
        self,
        channels: list[str],
        patterns: list[str],
    ) -> AsyncGenerator[dict, None]:
        """
        Async generator that yields raw message dicts from one or more channels
        and/or glob patterns.

        Yielded dicts contain at minimum:
            type     — "message" (channel) or "pmessage" (pattern)
            channel  — the matched channel name (bytes decoded to str)
            data     — the message payload (str)
            pattern  — the matched pattern, only present for "pmessage"

        Usage:
            async for msg in cache.subscribe_many(
                channels=["trade_events"],
                patterns=["price:*"],
            ):
                ...
        """
        pubsub = self._client.pubsub()
        if channels:
            await pubsub.subscribe(*channels)
        if patterns:
            await pubsub.psubscribe(*patterns)
        try:
            async for raw in pubsub.listen():
                if raw["type"] in ("message", "pmessage"):
                    yield raw
        finally:
            if channels:
                await pubsub.unsubscribe(*channels)
            if patterns:
                await pubsub.punsubscribe(*patterns)
            await pubsub.aclose()

    async def subscribe(self, channel: str) -> AsyncGenerator[str, None]:
        """
        Async generator that yields messages from a Redis pub/sub channel.

        Each yielded value is the raw string message (not the full Redis
        message envelope). Unsubscribes and releases the connection when
        the generator is closed.

        Usage:
            async for message in cache.subscribe("watchlist_prices"):
                data = json.loads(message)
        """
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for raw in pubsub.listen():
                if raw["type"] == "message":
                    yield raw["data"]
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying Redis connection pool."""
        await self._client.aclose()

    async def ping(self) -> bool:
        """Return True if Redis is reachable. Used by the health check."""
        try:
            return await self._client.ping()
        except Exception:
            return False
