"""Add strategy_signals for one-action-per-signal deduplication

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "strategy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trading_strategies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("bar_date", sa.Date(), nullable=False),
        sa.Column("action", sa.String(4), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 8)),
        sa.Column("stop_loss_price", sa.Numeric(18, 8)),
        sa.Column("take_profit_price", sa.Numeric(18, 8)),
        sa.Column("outcome", sa.String(32), nullable=False, server_default="CLAIMED"),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "trade_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trades.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "strategy_id", "symbol", "bar_date", "action", name="uq_strategy_signals_dedupe"
        ),
    )
    op.create_index(
        "ix_strategy_signals_symbol_created_at", "strategy_signals", ["symbol", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_signals_symbol_created_at", table_name="strategy_signals")
    op.drop_table("strategy_signals")
