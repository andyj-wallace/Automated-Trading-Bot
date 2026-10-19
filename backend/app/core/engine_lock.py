"""EngineLock: a Redis lease so only one backend process runs the trading loops."""

from __future__ import annotations

import uuid

LOCK_KEY = "trading_engine:lock"


class EngineLock:
    """Lease with a random owner token; renew before the TTL runs out, release on shutdown."""

    def __init__(self, cache, ttl_seconds: int = 30, key: str = LOCK_KEY) -> None:
        self._cache = cache
        self._ttl = ttl_seconds
        self._key = key
        self._token = uuid.uuid4().hex

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    async def acquire(self) -> bool:
        """Take the lease if nobody holds it."""
        return await self._cache.set_if_absent(self._key, self._token, self._ttl)

    async def renew(self) -> bool:
        """Extend the lease; False if we no longer own it."""
        return await self._cache.compare_and_expire(self._key, self._token, self._ttl)

    async def release(self) -> bool:
        """Give the lease up if we still own it."""
        return await self._cache.compare_and_delete(self._key, self._token)
