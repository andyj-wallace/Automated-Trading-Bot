import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TradeFill(Base):
    """One broker execution (fill) that belongs to a trade."""

    __tablename__ = "trade_fills"
    __table_args__ = (
        CheckConstraint("leg IN ('ENTRY','STOP','EXIT')", name="ck_trade_fills_leg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trade_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trades.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    exec_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    leg: Mapped[str] = mapped_column(String(5), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    commission: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=8), nullable=True
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
