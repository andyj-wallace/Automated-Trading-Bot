"""
HistoricalDataFetcher — fetches 1-year OHLCV data per symbol via the broker
and stores it in TimescaleDB with an overwrite-on-refresh policy.

Market data is treated as temporary: fetching replaces the previous data for
the symbol rather than appending to it. Use OHLCVRepo.delete_symbol() to
remove a symbol's history completely.

Depends on: BaseBroker (Layer 4), OHLCVRepo / OHLCVBar (Layer 3 / 5.3 model).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import BaseBroker
from app.db.repositories.ohlcv_repo import OHLCVRepo
from app.db.repositories.symbol_repo import SymbolRepo
from app.monitoring.logger import system_logger

# Default lookback period and granularity for all historical fetches.
_DEFAULT_DURATION = "5 Y"
_DEFAULT_BAR_SIZE = "1 day"


class HistoricalDataFetcher:
    """
    Fetches and stores historical OHLCV bars for watched symbols.

    Usage:
        fetcher = HistoricalDataFetcher(broker)
        async with AsyncSessionFactory() as session:
            count = await fetcher.fetch_and_store("AAPL", session)
            results = await fetcher.refresh_all_active(session)
    """

    def __init__(self, broker: BaseBroker) -> None:
        self._broker = broker

    async def fetch_and_store(
        self,
        symbol: str,
        session: AsyncSession,
        duration: str = _DEFAULT_DURATION,
        bar_size: str = _DEFAULT_BAR_SIZE,
    ) -> int:
        """
        Fetch historical bars for one symbol and upsert them into TimescaleDB.

        Returns the number of bars written.
        Logs at INFO on success, ERROR on failure (and re-raises).
        """
        symbol = symbol.upper()
        system_logger.info(
            "HistoricalDataFetcher: fetching",
            extra={"symbol": symbol, "duration": duration, "bar_size": bar_size},
        )

        bars = await self._broker.get_historical_data(symbol, duration, bar_size)
        if not bars:
            system_logger.warning(
                "HistoricalDataFetcher: broker returned no bars",
                extra={"symbol": symbol},
            )
            return 0

        repo = OHLCVRepo(session)
        count = await repo.upsert_bars(symbol, bars)

        system_logger.info(
            "HistoricalDataFetcher: stored bars",
            extra={"symbol": symbol, "count": count},
        )
        return count

    async def refresh_all_active(
        self,
        session: AsyncSession,
        duration: str = _DEFAULT_DURATION,
        bar_size: str = _DEFAULT_BAR_SIZE,
    ) -> dict[str, int]:
        """
        Fetch and store historical data for every active watched symbol.

        Returns a mapping of ticker → bars written.
        Failures for individual symbols are logged and skipped so one bad
        symbol does not abort the entire refresh.
        """
        symbols = await SymbolRepo(session).get_all(active_only=True)
        results: dict[str, int] = {}

        for sym in symbols:
            try:
                count = await self.fetch_and_store(sym.ticker, session, duration, bar_size)
                results[sym.ticker] = count
            except Exception as exc:
                system_logger.error(
                    "HistoricalDataFetcher: failed for symbol",
                    extra={"symbol": sym.ticker, "error": str(exc)},
                )
                results[sym.ticker] = 0

        return results
