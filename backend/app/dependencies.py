"""
Shared FastAPI dependency providers.

Each function here is injected via FastAPI's Depends() mechanism.
Broker, DB session, and cache dependencies will be wired up in later layers.
"""

from typing import AsyncGenerator

from fastapi import Depends

from app.config import Settings, get_settings


async def get_db():
    """
    Async database session dependency.
    Implemented in Layer 3 (database layer).
    """
    raise NotImplementedError("Database session not yet configured (Layer 3)")


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
