import uuid
from datetime import datetime

from sqlalchemy import Boolean, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WatchedSymbol(Base):
    """
    A ticker symbol the system actively tracks and may trade.

    Columns match design.md § watched_symbols.
    """

    __tablename__ = "watched_symbols"
    __table_args__ = (
        # GET /api/v1/symbols?active_only=true: WHERE is_active = TRUE ORDER BY added_at
        Index("ix_watched_symbols_is_active_added_at", "is_active", "added_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticker: Mapped[str] = mapped_column(String(10), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    added_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<WatchedSymbol {self.ticker} active={self.is_active}>"
