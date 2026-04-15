"""Add performance indexes for hot-path queries

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-12

Audit of query patterns identified the following missing indexes:

Hot paths (called every 30–60 seconds during market hours):
  - trades(status, executed_at)          — get_open_trades() in RiskMonitor,
                                           PositionMonitor, MetricsCollector
  - trading_strategies(is_enabled, name) — StrategyScheduler.get_enabled()
  - ohlcv_bars(symbol, bar_size)         — StrategyScheduler historical data
                                           fetch per symbol

On-demand paths (API / dashboard):
  - trades(status, closed_at)            — GET /api/v1/metrics/performance
                                           WHERE status=CLOSED AND closed_at>=cutoff
  - trades(status, symbol, strategy_id)  — GET /api/v1/trades with optional
                                           status/symbol/strategy filters
  - watched_symbols(is_active, added_at) — GET /api/v1/symbols active_only=true

Note: portfolio_snapshots and the time column of ohlcv_bars need no additional
indexes — TimescaleDB chunk exclusion handles time-range queries natively.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # trades — hot-path composite indexes
    # ------------------------------------------------------------------

    # get_open_trades(): WHERE status = 'OPEN' ORDER BY executed_at
    # Also serves RiskMonitor, PositionMonitor, MetricsCollector (every 30s)
    op.create_index(
        "ix_trades_status_executed_at",
        "trades",
        ["status", "executed_at"],
    )

    # GET /metrics/performance: WHERE status = 'CLOSED' AND closed_at >= cutoff
    # ORDER BY closed_at
    op.create_index(
        "ix_trades_status_closed_at",
        "trades",
        ["status", "closed_at"],
    )

    # GET /api/v1/trades with optional filters: status, symbol, strategy_id
    # ORDER BY executed_at DESC — composite covers all filter combinations
    op.create_index(
        "ix_trades_status_symbol_strategy",
        "trades",
        ["status", "symbol", "strategy_id"],
    )

    # ------------------------------------------------------------------
    # trading_strategies — StrategyScheduler hot path
    # ------------------------------------------------------------------

    # get_enabled(): WHERE is_enabled = TRUE ORDER BY name  (every 60s)
    op.create_index(
        "ix_trading_strategies_is_enabled_name",
        "trading_strategies",
        ["is_enabled", "name"],
    )

    # ------------------------------------------------------------------
    # ohlcv_bars — StrategyScheduler historical data fetch
    # ------------------------------------------------------------------

    # WHERE symbol = ? AND bar_size = ? ORDER BY time  (every 60s per symbol)
    # TimescaleDB handles the time-range portion via chunk exclusion;
    # this index narrows symbol + bar_size before the time scan.
    op.create_index(
        "ix_ohlcv_bars_symbol_bar_size",
        "ohlcv_bars",
        ["symbol", "bar_size"],
    )

    # ------------------------------------------------------------------
    # watched_symbols — active-filter query
    # ------------------------------------------------------------------

    # WHERE is_active = TRUE ORDER BY added_at
    op.create_index(
        "ix_watched_symbols_is_active_added_at",
        "watched_symbols",
        ["is_active", "added_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_watched_symbols_is_active_added_at", table_name="watched_symbols")
    op.drop_index("ix_ohlcv_bars_symbol_bar_size", table_name="ohlcv_bars")
    op.drop_index("ix_trading_strategies_is_enabled_name", table_name="trading_strategies")
    op.drop_index("ix_trades_status_symbol_strategy", table_name="trades")
    op.drop_index("ix_trades_status_closed_at", table_name="trades")
    op.drop_index("ix_trades_status_executed_at", table_name="trades")
