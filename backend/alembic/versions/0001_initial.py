"""Initial schema: all tables + TimescaleDB hypertable

Revision ID: 0001
Revises:
Create Date: 2026-03-24

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # PostgreSQL ENUM types (must be created before the tables that use them)
    # ------------------------------------------------------------------
    op.execute("CREATE TYPE tradedirection AS ENUM ('BUY', 'SELL')")
    op.execute("CREATE TYPE tradestatus AS ENUM ('OPEN', 'CLOSED', 'CANCELLED')")
    op.execute("CREATE TYPE logcategory AS ENUM ('TRADING', 'RISK', 'SYSTEM', 'ERROR')")

    # ------------------------------------------------------------------
    # watched_symbols
    # ------------------------------------------------------------------
    op.create_table(
        "watched_symbols",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "added_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_watched_symbols_ticker", "watched_symbols", ["ticker"], unique=True)

    # ------------------------------------------------------------------
    # trading_strategies
    # ------------------------------------------------------------------
    op.create_table(
        "trading_strategies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------
    # trades
    # ------------------------------------------------------------------
    op.create_table(
        "trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "strategy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trading_strategies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column(
            "direction",
            postgresql.ENUM("BUY", "SELL", name="tradedirection", create_type=False),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("stop_loss_price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("exit_price", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM("OPEN", "CLOSED", "CANCELLED", name="tradestatus", create_type=False),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("risk_amount", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column(
            "account_balance_at_entry", sa.Numeric(precision=18, scale=8), nullable=False
        ),
        sa.Column("pnl", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column(
            "executed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_trades_symbol", "trades", ["symbol"])
    op.create_index("ix_trades_strategy_id", "trades", ["strategy_id"])

    # ------------------------------------------------------------------
    # system_logs
    # ------------------------------------------------------------------
    op.create_table(
        "system_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column(
            "category",
            postgresql.ENUM("TRADING", "RISK", "SYSTEM", "ERROR", name="logcategory", create_type=False),
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_system_logs_category", "system_logs", ["category"])
    op.create_index("ix_system_logs_created_at", "system_logs", ["created_at"])

    # ------------------------------------------------------------------
    # portfolio_snapshots  (task 3.4 — converted to TimescaleDB hypertable below)
    # ------------------------------------------------------------------
    op.create_table(
        "portfolio_snapshots",
        sa.Column("time", sa.TIMESTAMP(timezone=True), primary_key=True, nullable=False),
        sa.Column("total_equity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("cash_balance", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("open_position_value", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("open_trade_count", sa.Integer(), nullable=False),
        sa.Column("aggregate_risk_amount", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("aggregate_risk_pct", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("max_per_trade_risk_pct", sa.Numeric(precision=10, scale=4), nullable=False),
    )

    # ------------------------------------------------------------------
    # Task 3.4 — Convert portfolio_snapshots to a TimescaleDB hypertable.
    # Partition by the `time` column; if_not_exists=TRUE makes this
    # idempotent in environments where it was already converted.
    # ------------------------------------------------------------------
    op.execute(
        "SELECT create_hypertable('portfolio_snapshots', 'time', if_not_exists => TRUE)"
    )


def downgrade() -> None:
    # Drop tables in reverse dependency order
    op.drop_table("portfolio_snapshots")
    op.drop_index("ix_system_logs_created_at", table_name="system_logs")
    op.drop_index("ix_system_logs_category", table_name="system_logs")
    op.drop_table("system_logs")
    op.drop_index("ix_trades_strategy_id", table_name="trades")
    op.drop_index("ix_trades_symbol", table_name="trades")
    op.drop_table("trades")
    op.drop_table("trading_strategies")
    op.drop_index("ix_watched_symbols_ticker", table_name="watched_symbols")
    op.drop_table("watched_symbols")

    op.execute("DROP TYPE IF EXISTS logcategory")
    op.execute("DROP TYPE IF EXISTS tradestatus")
    op.execute("DROP TYPE IF EXISTS tradedirection")
