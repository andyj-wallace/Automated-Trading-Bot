import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StrategySignal(Base):
    """One BUY/SELL signal acted on at most once per (strategy, symbol, bar date, action)."""

    __tablename__ = "strategy_signals"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id", "symbol", "bar_date", "action", name="uq_strategy_signals_dedupe"
        ),
        Index("ix_strategy_signals_symbol_created_at", "symbol", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trading_strategies.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    bar_date: Mapped[date] = mapped_column(Date, nullable=False)
    action: Mapped[str] = mapped_column(String(4), nullable=False)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    stop_loss_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    take_profit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, server_default="CLAIMED")
    detail: Mapped[str | None] = mapped_column(Text)
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
