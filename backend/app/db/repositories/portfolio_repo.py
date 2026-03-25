"""
Repository for the portfolio_snapshots TimescaleDB hypertable.

Only insert and query operations are provided — snapshots are immutable once written.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.portfolio import PortfolioSnapshot


class PortfolioRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_snapshot(self, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
        """
        Persist a new portfolio snapshot.

        The caller is responsible for setting `snapshot.time` to a unique
        timestamp. Duplicate timestamps will raise an IntegrityError.
        """
        self.session.add(snapshot)
        await self.session.flush()
        await self.session.refresh(snapshot)
        return snapshot

    async def get_by_time_range(
        self, start: datetime, end: datetime
    ) -> list[PortfolioSnapshot]:
        """
        Return snapshots within [start, end] inclusive, ordered ascending by time.

        TimescaleDB's chunk exclusion makes this query efficient for any range.
        """
        stmt = (
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.time >= start)
            .where(PortfolioSnapshot.time <= end)
            .order_by(PortfolioSnapshot.time)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_latest(self) -> PortfolioSnapshot | None:
        """Return the most recent snapshot, or None if the table is empty."""
        stmt = select(PortfolioSnapshot).order_by(PortfolioSnapshot.time.desc()).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
