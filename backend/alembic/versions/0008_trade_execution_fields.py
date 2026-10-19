"""Trade execution columns: fills, order ids, current stop, close attempts

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_COLUMNS = (
    "entry_order_id",
    "entry_fill_price",
    "filled_quantity",
    "exit_order_id",
    "stop_order_id",
    "current_stop_price",
    "close_attempts",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text("ALTER TYPE exitreason ADD VALUE IF NOT EXISTS 'STALE_TIMEOUT'"))
        op.execute(sa.text("ALTER TYPE exitreason ADD VALUE IF NOT EXISTS 'EXTERNAL'"))

    op.add_column("trades", sa.Column("entry_order_id", sa.String(32), nullable=True))
    op.add_column("trades", sa.Column("entry_fill_price", sa.Numeric(18, 8), nullable=True))
    op.add_column("trades", sa.Column("filled_quantity", sa.Numeric(18, 8), nullable=True))
    op.add_column("trades", sa.Column("exit_order_id", sa.String(32), nullable=True))
    op.add_column("trades", sa.Column("stop_order_id", sa.String(32), nullable=True))
    op.add_column("trades", sa.Column("current_stop_price", sa.Numeric(18, 8), nullable=True))
    op.add_column(
        "trades",
        sa.Column("close_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # Postgres cannot drop enum values; STALE_TIMEOUT and EXTERNAL remain in exitreason.
    for name in reversed(_NEW_COLUMNS):
        op.drop_column("trades", name)
