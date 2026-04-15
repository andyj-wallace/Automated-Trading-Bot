"""Enable TimescaleDB chunk compression for portfolio_snapshots

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-12

Enables automatic chunk compression on the portfolio_snapshots hypertable
with a 7-day retention threshold. Chunks older than 7 days are compressed
in the background by the TimescaleDB scheduler.

Compression settings:
  - compress_orderby: time DESC  — optimises time-range queries
  - Policy interval: 7 days      — compress chunks that are fully older than 7 days

Note: compression requires TimescaleDB 2.0+. The migration is safe to run
when no actively-written chunks are older than 7 days (which is the initial
state of a fresh deployment).
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Step 1: Enable compression on the hypertable.
    #
    # compress_orderby='time DESC' matches the primary query pattern
    # (most-recent snapshots first) and improves decompression efficiency.
    # No compress_segmentby needed — portfolio_snapshots has no natural
    # grouping column to segment by (all rows belong to the same portfolio).
    # ------------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE portfolio_snapshots
        SET (
            timescaledb.compress,
            timescaledb.compress_orderby = 'time DESC'
        )
        """
    )

    # ------------------------------------------------------------------
    # Step 2: Add automatic compression policy.
    #
    # Compress chunks whose data is older than 7 days. The TimescaleDB
    # background scheduler runs this policy automatically.
    #
    # if_not_exists => TRUE makes the call idempotent: safe to apply
    # against a DB that already has the policy set (e.g. re-running
    # migrations after a partial failure).
    # ------------------------------------------------------------------
    op.execute(
        "SELECT add_compression_policy('portfolio_snapshots', INTERVAL '7 days', "
        "if_not_exists => TRUE)"
    )


def downgrade() -> None:
    # Remove the compression policy first so no new compression runs.
    op.execute(
        "SELECT remove_compression_policy('portfolio_snapshots', if_exists => TRUE)"
    )

    # Disable compression on the hypertable.
    # Note: this does not decompress already-compressed chunks;
    # those would need to be manually decompressed if required.
    op.execute(
        "ALTER TABLE portfolio_snapshots SET (timescaledb.compress = false)"
    )
