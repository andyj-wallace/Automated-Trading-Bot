from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, Date, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DividendEvent(Base):
    """One dividend event per symbol and ex-dividend date (paid or declared)."""

    __tablename__ = "dividend_events"

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True, nullable=False)
    ex_dividend_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    declaration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class DividendFetchStatus(Base):
    """Last successful dividend fetch per symbol."""

    __tablename__ = "dividend_fetch_status"

    symbol: Mapped[str] = mapped_column(String(10), primary_key=True, nullable=False)
    last_fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
