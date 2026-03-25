from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, String
from sqlalchemy import TIMESTAMP, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OHLCVBar(Base):
    """
    One OHLCV price bar for a symbol.

    Stored in a TimescaleDB hypertable partitioned on `time`.
    Primary key is (time, symbol) — allows efficient time-range queries
    per symbol and supports upsert (overwrite-on-refresh policy).

    Market data is temporary: when new historical data is fetched for a
    symbol, existing rows are overwritten via INSERT ... ON CONFLICT DO UPDATE.
    """

    __tablename__ = "ohlcv_bars"

    time: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), primary_key=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(
        String(10), primary_key=True, nullable=False
    )
    bar_size: Mapped[str] = mapped_column(
        String(20), nullable=False, default="1 day"
    )
    open: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(precision=18, scale=8), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)

    def __repr__(self) -> str:
        return f"<OHLCVBar {self.symbol} {self.time} close={self.close}>"
