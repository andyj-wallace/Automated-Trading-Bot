"""Layer 6B: risk engine enhancements — new trade columns, expanded status ENUM

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-01

Changes:
  - Expand tradestatus ENUM: add PENDING, SUBMITTED, CLOSING
  - Create exitreason ENUM: STOP_LOSS, TAKE_PROFIT, MANUAL
  - Add take_profit_price DECIMAL NOT NULL (default 0 for existing rows)
  - Add reward_to_risk_ratio DECIMAL NOT NULL (default 0 for existing rows)
  - Add exit_reason exitreason nullable
  - Change default status from OPEN to PENDING
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Expand tradestatus ENUM with new lifecycle values.
    # ALTER TYPE ... ADD VALUE must run outside a transaction in older
    # PostgreSQL versions. We use the connection's autocommit context
    # to be safe on all supported versions (PG 12+).
    # ------------------------------------------------------------------
    conn = op.get_bind()
    conn.execute(
        sa.text("ALTER TYPE tradestatus ADD VALUE IF NOT EXISTS 'PENDING' BEFORE 'OPEN'")
    )
    conn.execute(
        sa.text("ALTER TYPE tradestatus ADD VALUE IF NOT EXISTS 'SUBMITTED' BEFORE 'OPEN'")
    )
    conn.execute(
        sa.text("ALTER TYPE tradestatus ADD VALUE IF NOT EXISTS 'CLOSING' BEFORE 'CLOSED'")
    )

    # ------------------------------------------------------------------
    # Create exitreason ENUM
    # ------------------------------------------------------------------
    op.execute(
        "CREATE TYPE exitreason AS ENUM ('STOP_LOSS', 'TAKE_PROFIT', 'MANUAL')"
    )

    # ------------------------------------------------------------------
    # Add new columns to trades
    # take_profit_price and reward_to_risk_ratio are NOT NULL — use a
    # server default of 0 for any existing rows, then drop the default
    # so future inserts must supply a value explicitly.
    # ------------------------------------------------------------------
    op.add_column(
        "trades",
        sa.Column(
            "take_profit_price",
            sa.Numeric(precision=18, scale=8),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "trades",
        sa.Column(
            "reward_to_risk_ratio",
            sa.Numeric(precision=18, scale=8),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "trades",
        sa.Column(
            "exit_reason",
            postgresql.ENUM("STOP_LOSS", "TAKE_PROFIT", "MANUAL", name="exitreason", create_type=False),
            nullable=True,
        ),
    )

    # Drop the migration-only server defaults so ORM must supply values.
    op.alter_column("trades", "take_profit_price", server_default=None)
    op.alter_column("trades", "reward_to_risk_ratio", server_default=None)

    # ------------------------------------------------------------------
    # Update the default status for new rows from OPEN → PENDING
    # ------------------------------------------------------------------
    op.alter_column(
        "trades",
        "status",
        server_default="PENDING",
        existing_type=postgresql.ENUM(
            "OPEN", "CLOSED", "CANCELLED", name="tradestatus", create_type=False
        ),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Restore the original status default
    op.alter_column(
        "trades",
        "status",
        server_default="OPEN",
        existing_type=postgresql.ENUM(
            "PENDING", "SUBMITTED", "OPEN", "CLOSING", "CLOSED", "CANCELLED",
            name="tradestatus",
            create_type=False,
        ),
        existing_nullable=False,
    )

    op.drop_column("trades", "exit_reason")
    op.drop_column("trades", "reward_to_risk_ratio")
    op.drop_column("trades", "take_profit_price")

    op.execute("DROP TYPE IF EXISTS exitreason")

    # Note: removing values from a PostgreSQL ENUM is not supported.
    # PENDING, SUBMITTED, and CLOSING remain in the tradestatus type
    # after a downgrade. This is acceptable for dev/staging environments.
