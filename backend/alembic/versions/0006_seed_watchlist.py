"""Seed initial watchlist symbols

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-11

Inserts the default set of watched symbols into watched_symbols.
ON CONFLICT (ticker) DO NOTHING makes this idempotent — safe to run
against a database that already has any of these tickers.

Downgrade removes only the rows seeded here; any additional symbols
added by the user are left untouched.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_TICKERS = [
    "HUT",
    "SNDK",
    "CEG",
    "VST",
    "CORZ",
    "IREN",
    "WEC",
    "SO",
    "KEEL",
]


def upgrade() -> None:
    values = ", ".join(
        f"(gen_random_uuid(), '{ticker}', true, now(), now())"
        for ticker in _SEED_TICKERS
    )
    op.execute(
        f"""
        INSERT INTO watched_symbols (id, ticker, is_active, added_at, updated_at)
        VALUES {values}
        ON CONFLICT (ticker) DO NOTHING
        """
    )


def downgrade() -> None:
    tickers = ", ".join(f"'{t}'" for t in _SEED_TICKERS)
    op.execute(
        f"DELETE FROM watched_symbols WHERE ticker IN ({tickers})"
    )
