"""Add ohlcv_bars table as TimescaleDB hypertable

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-25

Market data is temporary (overwrite-on-refresh policy). The table uses a
composite primary key (time, symbol) to support efficient per-symbol
time-range queries and INSERT ... ON CONFLICT DO UPDATE upserts.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ohlcv_bars",
        sa.Column("time", sa.TIMESTAMP(timezone=True), primary_key=True, nullable=False),
        sa.Column("symbol", sa.String(10), primary_key=True, nullable=False),
        sa.Column("bar_size", sa.String(20), nullable=False, server_default="1 day"),
        sa.Column("open", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_ohlcv_bars_symbol", "ohlcv_bars", ["symbol"])

    # Convert to TimescaleDB hypertable partitioned on `time`.
    # if_not_exists makes this idempotent (safe on re-run).
    op.execute(
        "SELECT create_hypertable('ohlcv_bars', 'time', if_not_exists => TRUE)"
    )


def downgrade() -> None:
    op.drop_index("ix_ohlcv_bars_symbol", table_name="ohlcv_bars")
    op.drop_table("ohlcv_bars")
