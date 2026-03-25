from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PortfolioSnapshot(Base):
    """
    Point-in-time snapshot of portfolio risk metrics.

    Stored in a TimescaleDB hypertable partitioned on `time`.
    The `time` column is the primary key — each snapshot is at a unique instant.

    Columns match design.md § portfolio_snapshots.
    """

    __tablename__ = "portfolio_snapshots"

    time: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), primary_key=True, nullable=False
    )
    total_equity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    open_position_value: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8), nullable=False
    )
    open_trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Sum of risk_amount across all open trades
    aggregate_risk_amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8), nullable=False
    )
    # aggregate_risk_amount / account_balance
    aggregate_risk_pct: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=4), nullable=False
    )
    # Should always be ≤ 1% — flags breaches if > 1%
    max_per_trade_risk_pct: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=4), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<PortfolioSnapshot time={self.time} "
            f"equity={self.total_equity} risk={self.aggregate_risk_pct}%>"
        )
