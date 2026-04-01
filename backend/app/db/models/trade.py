import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import ENUM as PGENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TradeDirection(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, enum.Enum):
    PENDING = "PENDING"      # row created; risk validated; order not yet sent
    SUBMITTED = "SUBMITTED"  # entry order sent to broker; awaiting fill
    OPEN = "OPEN"            # broker confirmed fill; PositionMonitor active
    CLOSING = "CLOSING"      # stop/target hit; close order submitted
    CLOSED = "CLOSED"        # close confirmed; exit fields written
    CANCELLED = "CANCELLED"  # rejected or cancelled before reaching OPEN


class ExitReason(str, enum.Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL = "MANUAL"


class Trade(Base):
    """
    A single trade execution record.

    Columns match design.md § trades.

    Audit-sensitive fields (entry_price, stop_loss_price, risk_amount,
    account_balance_at_entry) must never be updated after creation.
    TradeRepo enforces this by providing no update path for these columns.

    Status lifecycle: PENDING → SUBMITTED → OPEN → CLOSING → CLOSED
                                                  ↘ CANCELLED (from PENDING or SUBMITTED)
    """

    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trading_strategies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    direction: Mapped[TradeDirection] = mapped_column(
        PGENUM(TradeDirection, name="tradedirection", create_type=False), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    stop_loss_price: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8), nullable=False
    )
    take_profit_price: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8), nullable=False
    )
    reward_to_risk_ratio: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8), nullable=False
    )
    exit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=18, scale=8), nullable=True
    )
    exit_reason: Mapped[ExitReason | None] = mapped_column(
        PGENUM(ExitReason, name="exitreason", create_type=False), nullable=True
    )
    status: Mapped[TradeStatus] = mapped_column(
        PGENUM(TradeStatus, name="tradestatus", create_type=False),
        nullable=False,
        default=TradeStatus.PENDING,
    )
    # Calculated at validation time: quantity × (entry_price − stop_loss_price)
    risk_amount: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    # Snapshot of account balance used for the 1% rule check — never recalculated
    account_balance_at_entry: Mapped[Decimal] = mapped_column(
        Numeric(precision=18, scale=8), nullable=False
    )
    pnl: Mapped[Decimal | None] = mapped_column(Numeric(precision=18, scale=8), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Trade {self.symbol} {self.direction} qty={self.quantity} status={self.status}>"
