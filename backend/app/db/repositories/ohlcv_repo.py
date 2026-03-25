"""
Repository for the ohlcv_bars table.

Implements the overwrite-on-refresh policy: when new historical data is
fetched for a symbol, existing rows are updated in place via
INSERT ... ON CONFLICT DO UPDATE (upsert). This avoids deleting and
re-inserting the entire year of data on each refresh.
"""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import PriceBar
from app.db.models.ohlcv_bar import OHLCVBar


class OHLCVRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_bars(self, symbol: str, bars: list[PriceBar]) -> int:
        """
        Insert or update OHLCV bars for a symbol (overwrite-on-refresh policy).

        Uses PostgreSQL INSERT ... ON CONFLICT DO UPDATE so that re-fetching
        the same date range updates existing rows rather than duplicating them.

        Returns the number of rows upserted.
        """
        if not bars:
            return 0

        symbol = symbol.upper()
        rows = [
            {
                "time": bar.timestamp,
                "symbol": symbol,
                "bar_size": bar.bar_size,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in bars
        ]

        stmt = insert(OHLCVBar).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["time", "symbol"],
            set_={
                "bar_size": stmt.excluded.bar_size,
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return len(rows)

    async def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        bar_size: str = "1 day",
    ) -> list[OHLCVBar]:
        """Return OHLCV bars for a symbol within the given time range (inclusive)."""
        stmt = (
            select(OHLCVBar)
            .where(
                OHLCVBar.symbol == symbol.upper(),
                OHLCVBar.bar_size == bar_size,
                OHLCVBar.time >= start,
                OHLCVBar.time <= end,
            )
            .order_by(OHLCVBar.time)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def delete_symbol(self, symbol: str) -> int:
        """Delete all bars for a symbol. Returns row count deleted."""
        stmt = delete(OHLCVBar).where(OHLCVBar.symbol == symbol.upper())
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount
