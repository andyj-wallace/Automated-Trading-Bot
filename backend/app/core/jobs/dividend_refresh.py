"""DividendRefreshJob: keeps stored dividend history fresh within a daily Alpha Vantage budget."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.exc import DBAPIError

from app.core import market_calendar
from app.data.alpha_vantage import DividendSource
from app.db.repositories.dividend_repo import DividendRepo
from app.monitoring.logger import system_logger

BUDGET_KEY = "dividends:alpha_vantage:requests:{day}"
BUDGET_TTL_SECONDS = 48 * 3600
FRESH_DAYS = 7
_EPOCH = datetime.min.replace(tzinfo=UTC)
# Postgres SQLSTATE classes caused by the rows themselves: cardinality, data exception, constraint.
# asyncpg reports these as plain DBAPIError, not DataError/IntegrityError.
_DATA_SQLSTATE_CLASSES = ("21", "22", "23")


def _sqlstate(exc: DBAPIError) -> str:
    return str(getattr(exc.orig, "sqlstate", "") or "")


class DividendRefreshJob:
    """Refreshes stored dividend history once per ET day within a request budget."""

    def __init__(
        self,
        client: DividendSource | None,
        session_factory,
        cache,
        *,
        symbols: Sequence[str] = (),
        daily_budget: int = 20,
        clock: Callable[[], datetime] = market_calendar.now_et,
        repo_factory: Callable[[Any], Any] = DividendRepo,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._cache = cache
        self._symbols = list(symbols)
        self._daily_budget = daily_budget
        self._clock = clock
        self._repo_factory = repo_factory
        self._last_run: date | None = None

    async def tick(self) -> None:
        """Run the refresh at most once per ET day; log and skip when disabled."""
        today = self._clock().date()
        if self._last_run == today:
            return
        self._last_run = today
        if self._client is None or not self._symbols:
            reason = "no_api_key" if self._client is None else "no_symbols"
            system_logger.info("Dividend refresh disabled", extra={"reason": reason})
            return
        await self.run(today)

    async def run(self, today: date) -> list[str]:
        """Refresh stale symbols until the budget runs out. Returns the budget-skipped symbols."""
        now_utc = self._clock().astimezone(UTC)
        skipped: list[str] = []
        async with self._session_factory() as session:
            repo = self._repo_factory(session)
            last = await repo.last_fetched(self._symbols)
            order = sorted(self._symbols, key=lambda s: (s in last, last.get(s, _EPOCH)))
            for symbol in order:
                fetched = last.get(symbol)
                if (
                    fetched
                    and now_utc - fetched < timedelta(days=FRESH_DAYS)
                    and not await repo.has_ex_date_between(
                        symbol, fetched.astimezone(market_calendar.ET).date(), today
                    )
                ):
                    continue
                if await self._used(self._budget_day()) >= self._daily_budget:
                    skipped.append(symbol)
                    continue
                result = await self._client.fetch(symbol, self._may_request)
                if result.error == "budget_exhausted":
                    skipped.append(symbol)
                elif not result.ok:
                    system_logger.warning(
                        "Dividend fetch failed", extra={"symbol": symbol, "error": result.error}
                    )
                else:
                    try:
                        await repo.upsert_events(symbol, result.rows, now_utc)
                        await repo.mark_fetched(symbol, now_utc)
                        await session.commit()
                    except DBAPIError as exc:
                        # Bad data for one symbol must not stop the others; other DB errors still abort.
                        if _sqlstate(exc)[:2] not in _DATA_SQLSTATE_CLASSES:
                            raise
                        await session.rollback()
                        system_logger.warning(
                            "Dividend store failed",
                            extra={"symbol": symbol, "sqlstate": _sqlstate(exc)},
                        )
        if skipped:
            system_logger.warning(
                "Dividend refresh budget spent",
                extra={"skipped_symbols": skipped, "skipped_count": len(skipped)},
            )
        return skipped

    async def _used(self, day: date) -> int:
        return int(await self._cache.get(BUDGET_KEY.format(day=day.isoformat())) or 0)

    def _budget_day(self) -> date:
        # Read the clock on every call so a run that crosses midnight ET charges the new day.
        return self._clock().astimezone(market_calendar.ET).date()

    async def _may_request(self) -> bool:
        day = self._budget_day()
        used = await self._used(day)
        if used >= self._daily_budget:
            return False
        key = BUDGET_KEY.format(day=day.isoformat())
        await self._cache.set(key, str(used + 1), ttl=BUDGET_TTL_SECONDS)
        return True
