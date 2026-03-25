"""
Repository for the watched_symbols table.

All database access for WatchedSymbol goes through this class.
"""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.watched_symbol import WatchedSymbol


class SymbolRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self, active_only: bool = False) -> list[WatchedSymbol]:
        """Return all watched symbols, optionally filtering to active ones only."""
        stmt = select(WatchedSymbol).order_by(WatchedSymbol.added_at)
        if active_only:
            stmt = stmt.where(WatchedSymbol.is_active.is_(True))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_ticker(self, ticker: str) -> WatchedSymbol | None:
        """Return the symbol with the given ticker, or None if not found."""
        stmt = select(WatchedSymbol).where(WatchedSymbol.ticker == ticker.upper())
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, ticker: str, display_name: str | None = None) -> WatchedSymbol:
        """
        Add a new symbol to the watchlist.

        Raises ValueError if the ticker already exists.
        """
        ticker = ticker.upper()
        existing = await self.get_by_ticker(ticker)
        if existing:
            raise ValueError(f"Symbol {ticker!r} is already on the watchlist")

        symbol = WatchedSymbol(ticker=ticker, display_name=display_name, is_active=True)
        self.session.add(symbol)
        await self.session.flush()  # assigns id without committing
        await self.session.refresh(symbol)
        return symbol

    async def set_active(self, ticker: str, is_active: bool) -> WatchedSymbol | None:
        """Soft-enable or soft-disable a symbol. Returns the updated record or None."""
        symbol = await self.get_by_ticker(ticker)
        if symbol is None:
            return None
        symbol.is_active = is_active
        await self.session.flush()
        await self.session.refresh(symbol)
        return symbol

    async def delete(self, ticker: str) -> bool:
        """
        Permanently delete a symbol from the watchlist.

        Returns True if deleted, False if ticker was not found.
        Callers are responsible for ensuring no open positions exist before calling.
        """
        symbol = await self.get_by_ticker(ticker)
        if symbol is None:
            return False
        await self.session.delete(symbol)
        await self.session.flush()
        return True
