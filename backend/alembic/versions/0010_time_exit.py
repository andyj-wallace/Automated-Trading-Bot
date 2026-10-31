"""Day trades: exit_deadline column and the TIME_EXIT exit reason

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text("ALTER TYPE exitreason ADD VALUE IF NOT EXISTS 'TIME_EXIT'"))
    op.add_column("trades", sa.Column("exit_deadline", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    # Postgres cannot drop enum values; TIME_EXIT remains in exitreason.
    op.drop_column("trades", "exit_deadline")
