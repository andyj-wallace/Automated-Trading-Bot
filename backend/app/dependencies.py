"""
Shared FastAPI dependency providers.

Each function here is injected via FastAPI's Depends() mechanism.
"""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionFactory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session, closing it on exit."""
    async with AsyncSessionFactory() as session:
        yield session


async def get_cache():
    """
    Redis cache dependency.
    Implemented in Layer 5 (market data layer).
    """
    raise NotImplementedError("Redis cache not yet configured (Layer 5)")


async def get_broker():
    """
    Broker client dependency. Returns IBKRClient or MockBroker
    based on the ENVIRONMENT env var.
    Implemented in Layer 4 (broker abstraction layer).
    """
    raise NotImplementedError("Broker not yet configured (Layer 4)")

