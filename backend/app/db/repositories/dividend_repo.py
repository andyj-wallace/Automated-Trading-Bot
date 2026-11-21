"""Repository for the dividend_events and dividend_fetch_status tables."""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DividendEvent, DividendFetchStatus


class DividendRowLike(Protocol):
    ex_dividend_date: date
    declaration_date: date | None
    record_date: date | None
    payment_date: date | None
    amount: Decimal


class DividendRepo:
    """Database access for dividend events and per-symbol fetch status."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_events(
        self, symbol: str, rows: Sequence[DividendRowLike], fetched_at: datetime
    ) -> int:
        """Insert or update dividend events for a symbol. Never deletes. Returns rows upserted."""
        if not rows:
            return 0

        values = [
            {
                "symbol": symbol,
                "ex_dividend_date": row.ex_dividend_date,
                "declaration_date": row.declaration_date,
                "record_date": row.record_date,
                "payment_date": row.payment_date,
                "amount": row.amount,
                "fetched_at": fetched_at,
            }
            for row in rows
        ]
        stmt = insert(DividendEvent).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "ex_dividend_date"],
            set_={
                "declaration_date": stmt.excluded.declaration_date,
                "record_date": stmt.excluded.record_date,
                "payment_date": stmt.excluded.payment_date,
                "amount": stmt.excluded.amount,
                "fetched_at": stmt.excluded.fetched_at,
            },
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return len(rows)

    async def mark_fetched(self, symbol: str, fetched_at: datetime) -> None:
        """Record the last successful dividend fetch for a symbol."""
        stmt = insert(DividendFetchStatus).values(symbol=symbol, last_fetched_at=fetched_at)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol"],
            set_={"last_fetched_at": stmt.excluded.last_fetched_at},
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def last_fetched(self, symbols: Sequence[str]) -> dict[str, datetime]:
        """Return last fetch time for the given symbols that have a status row."""
        if not symbols:
            return {}
        stmt = select(DividendFetchStatus).where(DividendFetchStatus.symbol.in_(symbols))
        result = await self.session.execute(stmt)
        return {row.symbol: row.last_fetched_at for row in result.scalars().all()}

    async def has_ex_date_between(self, symbol: str, after: date, through: date) -> bool:
        """True if the symbol has an event with after < ex_dividend_date <= through."""
        stmt = select(
            exists().where(
                DividendEvent.symbol == symbol,
                DividendEvent.ex_dividend_date > after,
                DividendEvent.ex_dividend_date <= through,
            )
        )
        result = await self.session.execute(stmt)
        return bool(result.scalar())

    async def list_events(self, symbol: str) -> list[DividendEvent]:
        """Return the symbol's dividend events ordered by ex-dividend date ascending."""
        stmt = (
            select(DividendEvent)
            .where(DividendEvent.symbol == symbol)
            .order_by(DividendEvent.ex_dividend_date)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
