import uuid
from datetime import datetime

from sqlalchemy import Boolean, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TradingStrategy(Base):
    """
    A trading strategy configuration.

    config (JSONB) holds strategy-specific parameters plus the `symbols` array,
    e.g. {"fast_period": 50, "slow_period": 200, "symbols": ["AAPL", "MSFT"]}.

    Columns match design.md § trading_strategies.
    """

    __tablename__ = "trading_strategies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # e.g. "moving_average", "mean_reversion"
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<TradingStrategy {self.name} enabled={self.is_enabled}>"
