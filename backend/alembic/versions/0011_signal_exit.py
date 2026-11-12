"""Strategy SELL signals: the SIGNAL_EXIT exit reason

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text("ALTER TYPE exitreason ADD VALUE IF NOT EXISTS 'SIGNAL_EXIT'"))


def downgrade() -> None:
    # Postgres cannot drop enum values; SIGNAL_EXIT remains in exitreason.
    pass
