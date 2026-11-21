import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _alembic(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        check=True,
        env=os.environ.copy(),
    )


async def _table_exists(name: str) -> bool:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{name}"}
            )
            return bool(result.scalar())
    finally:
        await engine.dispose()


def test_dividend_tables_migration_round_trip() -> None:
    tables = ("dividend_events", "dividend_fetch_status")

    _alembic("upgrade", "head")
    assert all(asyncio.run(_table_exists(t)) for t in tables)

    _alembic("downgrade", "0012")
    assert not any(asyncio.run(_table_exists(t)) for t in tables)

    _alembic("upgrade", "head")
    assert all(asyncio.run(_table_exists(t)) for t in tables)
