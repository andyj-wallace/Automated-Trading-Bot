"""
Shared FastAPI dependency providers.

Each function here is injected via FastAPI's Depends() mechanism.
"""

from functools import lru_cache
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import BaseBroker
from app.config import get_settings
from app.db.session import AsyncSessionFactory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session, closing it on exit."""
    async with AsyncSessionFactory() as session:
        yield session


@lru_cache
def _build_cache() -> "RedisCache":
    from app.data.cache import RedisCache
    settings = get_settings()
    return RedisCache(settings.redis_url)


async def get_cache() -> "RedisCache":
    """
    Redis cache dependency.

    Returns the same RedisCache instance for the process lifetime (cached by
    _build_cache). Inject via FastAPI Depends().
    """
    return _build_cache()


@lru_cache
def _build_broker() -> BaseBroker:
    """
    Build and cache the broker instance for the lifetime of the process.

    Returns MockBroker when ENVIRONMENT=development (no live connection needed).
    Returns IBKRClient for all other environments (requires IB Gateway running).
    """
    settings = get_settings()

    if settings.environment == "development":
        from app.brokers.mock.client import MockBroker
        return MockBroker()

    from app.brokers.ibkr.client import IBKRClient
    return IBKRClient(settings)


async def get_broker() -> BaseBroker:
    """
    Broker client dependency.

    Injected via FastAPI Depends(). Returns the same broker instance for
    the process lifetime (cached by _build_broker).
    """
    return _build_broker()

