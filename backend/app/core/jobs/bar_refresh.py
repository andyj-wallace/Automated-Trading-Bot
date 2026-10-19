"""BarRefreshJob: keeps daily OHLCV bars current for every symbol the bot evaluates."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, time

from app.core import market_calendar
from app.core.events import publish_system_alert
from app.data.historical import HistoricalDataFetcher
from app.db.repositories.strategy_repo import StrategyRepo
from app.db.repositories.symbol_repo import SymbolRepo
from app.db.repositories.trade_repo import TradeRepo
from app.monitoring.logger import system_logger


class BarRefreshJob:
    """Backfills 2 years at startup, then refreshes 10 days once after each session."""

    def __init__(
        self,
        broker,
        session_factory,
        cache,
        *,
        run_at: time,
        clock: Callable[[], datetime] = market_calendar.now_et,
        pause_seconds: float = 2.0,
        registry=None,
    ) -> None:
        if registry is None:
            from app.core.strategy_engine.registry import registry as default_registry

            registry = default_registry
        self._broker = broker
        self._session_factory = session_factory
        self._cache = cache
        self._run_at = run_at
        self._clock = clock
        self._pause = pause_seconds
        self._registry = registry
        self._fetcher = HistoricalDataFetcher(broker)
        self._startup_done = False
        self._last_run: date | None = None

    async def tick(self) -> None:
        """Run the startup backfill once, then the daily refresh after `run_at`."""
        if not self._broker.is_connected():
            return
        if not self._startup_done:
            await self.refresh(await self.desired_symbols(), duration="2 Y")
            self._startup_done = True
            return
        now = self._clock()
        today = now.date()
        if (
            self._last_run == today
            or not market_calendar.is_trading_day(today)
            or now.time() < self._run_at
        ):
            return
        await self.refresh(await self.desired_symbols(), duration="10 D")
        self._last_run = today

    async def desired_symbols(self) -> set[str]:
        """Watchlist, enabled-strategy symbols and their extra symbols, active-trade symbols."""
        async with self._session_factory() as session:
            watchlist = await SymbolRepo(session).get_all(active_only=True)
            strategies = await StrategyRepo(session).get_enabled()
            active = await TradeRepo(session).get_active_trades()
        symbols = {s.ticker.upper() for s in watchlist}
        symbols.update(t.symbol.upper() for t in active)
        for strategy in strategies:
            config = strategy.config or {}
            symbols.update(str(sym).upper() for sym in config.get("symbols", []))
            try:
                built = self._registry.build(strategy.type, config)
                symbols.update(s.upper() for s in built.required_extra_symbols())
            except Exception as exc:
                system_logger.warning(
                    "BarRefreshJob: could not build strategy for extra symbols",
                    extra={"strategy_id": str(strategy.id), "error": str(exc)},
                )
        return symbols

    async def refresh(self, symbols: set[str], duration: str) -> dict[str, int]:
        """Fetch and store bars per symbol; alert once listing every failed symbol."""
        results: dict[str, int] = {}
        failed: list[str] = []
        for index, symbol in enumerate(sorted(symbols)):
            if index and self._pause:
                await asyncio.sleep(self._pause)
            try:
                async with self._session_factory() as session:
                    count = await self._fetcher.fetch_and_store(symbol, session, duration=duration)
                    await session.commit()
            except Exception as exc:
                system_logger.error(
                    "BarRefreshJob: fetch failed",
                    extra={"symbol": symbol, "error": str(exc)},
                )
                count = 0
            results[symbol] = count
            if count == 0:
                failed.append(symbol)
        if failed:
            await publish_system_alert(
                self._cache,
                "DATA_REFRESH_FAILED",
                f"Daily bar refresh returned no data for: {', '.join(failed)}",
                symbols=failed,
            )
        return results
