"""The dividend request budget against real Redis: concurrent requests never overspend it."""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime

import pytest_asyncio

from app.core.jobs.dividend_refresh import BUDGET_KEY, BUDGET_TTL_SECONDS, DividendRefreshJob
from app.core.market_calendar import ET
from app.data.cache import RedisCache

DAY = date(2026, 11, 2)
KEY = BUDGET_KEY.format(day=DAY.isoformat())


@pytest_asyncio.fixture
async def cache():
    c = RedisCache(os.environ["REDIS_URL"])
    await c.delete(KEY)
    yield c
    await c.delete(KEY)
    await c.close()


async def test_concurrent_requests_never_exceed_the_daily_budget(cache):
    job = DividendRefreshJob(
        None,
        None,
        cache,
        daily_budget=20,
        clock=lambda: datetime(2026, 11, 2, 10, 0, tzinfo=ET),
    )

    allowed = await asyncio.gather(*(job._may_request() for _ in range(50)))

    assert allowed.count(True) == 20
    assert await cache.get(KEY) == "20"
    assert 0 < await cache._client.ttl(KEY) <= BUDGET_TTL_SECONDS
