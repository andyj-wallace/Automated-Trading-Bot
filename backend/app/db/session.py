"""
Async SQLAlchemy session factory.

Usage (in repositories or via FastAPI dependency injection):

    from app.db.session import AsyncSessionFactory

    async with AsyncSessionFactory() as session:
        # ... use session ...

Or via the get_db() dependency in app/dependencies.py.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    # Connection pool: min 5 always open, up to 20 total (pool_size + max_overflow)
    pool_size=5,
    max_overflow=15,
    pool_pre_ping=True,  # verify connections before use; handles dropped connections
    echo=False,
)

AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # keep attributes accessible after commit
)
