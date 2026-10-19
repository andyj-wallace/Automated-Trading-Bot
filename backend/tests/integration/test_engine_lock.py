"""C8: the Redis engine lock keeps a second backend process from running the trading loops."""

from __future__ import annotations

import asyncio
import os

import pytest_asyncio
from sqlalchemy import text

from app.config import Settings
from app.core.engine_lock import LOCK_KEY, EngineLock
from app.data.cache import RedisCache
from app.dependencies import _build_broker, _build_cache, _build_circuit_breaker


@pytest_asyncio.fixture
async def cache():
    c = RedisCache(os.environ["REDIS_URL"])
    await c.delete(LOCK_KEY)
    await c.delete("test:lock")
    yield c
    await c.delete(LOCK_KEY)
    await c.delete("test:lock")
    await c.close()


async def test_second_lock_cannot_acquire_until_first_releases(cache):
    first = EngineLock(cache, key="test:lock")
    second = EngineLock(cache, key="test:lock")

    assert await first.acquire() is True
    assert await second.acquire() is False
    assert await second.release() is False  # not the owner: leaves the lease alone
    assert await second.acquire() is False

    assert await first.release() is True
    assert await second.acquire() is True


async def test_renew_with_wrong_token_fails(cache):
    first = EngineLock(cache, key="test:lock")
    intruder = EngineLock(cache, key="test:lock")
    await first.acquire()

    assert await intruder.renew() is False
    assert await first.renew() is True


async def test_lease_expires_without_renewal(cache):
    first = EngineLock(cache, ttl_seconds=1, key="test:lock")
    await first.acquire()
    await asyncio.sleep(1.3)
    assert await EngineLock(cache, key="test:lock").acquire() is True
    assert await first.renew() is False


def _build_engine(**kw):
    from app.core.engine import TradingEngine

    settings = Settings()
    settings.scheduler_interval_seconds = 3600
    eng = TradingEngine(
        settings,
        is_market_open=lambda: True,
        feed_sync_interval=3600,
        bar_refresh_interval=3600,
        monitor_resync_interval=3600,
        lock_renew_interval=0.1,
        lock_retry_interval=0.1,
        **kw,
    )
    eng.bar_refresh._startup_done = True
    return eng


async def test_only_one_engine_runs_the_scheduler_and_takeover_works(cache):
    from app.db.session import engine as db_engine

    for builder in (_build_broker, _build_cache, _build_circuit_breaker):
        builder.cache_clear()
    async with db_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE trades RESTART IDENTITY CASCADE"))

    first, second = _build_engine(), _build_engine()
    try:
        await first.start()
        await second.start()
        assert first.is_leader and not second.is_leader
        assert first.scheduler._running and not second.scheduler._running

        await first.stop()
        for _ in range(30):
            if second.is_leader and second.scheduler._running:
                break
            await asyncio.sleep(0.1)
        assert second.is_leader and second.scheduler._running
    finally:
        await second.stop()
        await db_engine.dispose()
        for builder in (_build_broker, _build_cache, _build_circuit_breaker):
            builder.cache_clear()


async def test_losing_the_lock_stops_trading_loops(cache):
    from app.db.session import engine as db_engine

    for builder in (_build_broker, _build_cache, _build_circuit_breaker):
        builder.cache_clear()
    eng = _build_engine()
    try:
        await eng.start()
        assert eng.is_leader and eng.scheduler._running

        await cache.set(LOCK_KEY, "someone-else", ttl=30)
        for _ in range(30):
            if not eng.is_leader:
                break
            await asyncio.sleep(0.1)

        assert not eng.is_leader
        assert not eng.scheduler._running
        assert eng.position_monitor._running is False
    finally:
        await eng.stop()
        await db_engine.dispose()
        for builder in (_build_broker, _build_cache, _build_circuit_breaker):
            builder.cache_clear()
