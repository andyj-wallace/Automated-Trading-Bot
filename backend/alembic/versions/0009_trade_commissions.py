"""Trade commissions: per-leg broker fees, so P&L can be reported after costs

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("entry_commission", sa.Numeric(18, 8), nullable=True))
    op.add_column("trades", sa.Column("exit_commission", sa.Numeric(18, 8), nullable=True))


def downgrade() -> None:
    op.drop_column("trades", "exit_commission")
    op.drop_column("trades", "entry_commission")
