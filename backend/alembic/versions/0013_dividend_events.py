"""Add dividend_events and dividend_fetch_status

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dividend_events",
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("ex_dividend_date", sa.Date(), nullable=False),
        sa.Column("declaration_date", sa.Date(), nullable=True),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(18, 8), nullable=False),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "ex_dividend_date"),
    )
    op.create_table(
        "dividend_fetch_status",
        sa.Column("symbol", sa.String(10), primary_key=True, nullable=False),
        sa.Column("last_fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dividend_fetch_status")
    op.drop_table("dividend_events")
