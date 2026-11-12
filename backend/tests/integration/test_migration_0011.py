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


async def _exitreason_values() -> set[str]:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT unnest(enum_range(NULL::exitreason))::text"))
            return {row[0] for row in result}
    finally:
        await engine.dispose()


def test_signal_exit_migration_round_trip() -> None:
    _alembic("upgrade", "head")
    assert "SIGNAL_EXIT" in asyncio.run(_exitreason_values())

    _alembic("downgrade", "0010")
    _alembic("upgrade", "head")
    assert "SIGNAL_EXIT" in asyncio.run(_exitreason_values())
