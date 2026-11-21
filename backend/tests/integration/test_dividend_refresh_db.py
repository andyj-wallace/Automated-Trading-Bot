"""DividendRefreshJob against a real database: last_fetched_at persists across success and failure."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.jobs.dividend_refresh import DividendRefreshJob
from app.core.market_calendar import ET
from app.data.alpha_vantage import DividendRow, FetchResult
from app.db.repositories.dividend_repo import DividendRepo

_DB_URL = os.environ["DATABASE_URL"]


@pytest_asyncio.fixture
async def factory():
    eng = create_async_engine(_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE dividend_events, dividend_fetch_status"))
    yield sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


class FakeCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.data[key] = value


class FakeClient:
    def __init__(self) -> None:
        self.result: FetchResult | None = None

    async def fetch(self, symbol, may_request):
        if not await may_request():
            return FetchResult(ok=False, error="budget_exhausted")
        return self.result


def _row(ex: date, amount: str) -> DividendRow:
    return DividendRow(
        ex_dividend_date=ex,
        declaration_date=None,
        record_date=None,
        payment_date=None,
        amount=Decimal(amount),
        declared=False,
    )


async def test_last_fetched_persists_after_success_and_untouched_after_failure(factory):
    clock = SimpleNamespace(now=datetime(2026, 11, 2, 10, 0, tzinfo=ET))
    client = FakeClient()
    job = DividendRefreshJob(
        client,
        factory,
        FakeCache(),
        symbols=["KO"],
        daily_budget=20,
        clock=lambda: clock.now,
    )
    day1_utc = datetime(2026, 11, 2, 10, 0, tzinfo=ET).astimezone(UTC)

    # Day 1: successful fetch stores two events and the fetch time.
    client.result = FetchResult(
        ok=True,
        rows=[_row(date(2026, 9, 15), "0.51"), _row(date(2026, 6, 13), "0.51")],
    )
    await job.run(date(2026, 11, 2))
    async with factory() as s:
        repo = DividendRepo(s)
        events = await repo.list_events("KO")
        assert len(events) == 2
        assert (await repo.last_fetched(["KO"]))["KO"] == day1_utc

    # Day 9: failed fetch writes nothing and leaves last_fetched at day 1.
    clock.now = datetime(2026, 11, 10, 10, 0, tzinfo=ET)
    client.result = FetchResult(ok=False, error="no_data_list")
    await job.run(date(2026, 11, 10))
    async with factory() as s:
        repo = DividendRepo(s)
        events = await repo.list_events("KO")
        assert [(e.ex_dividend_date, e.amount) for e in events] == [
            (date(2026, 6, 13), Decimal("0.51")),
            (date(2026, 9, 15), Decimal("0.51")),
        ]
        assert (await repo.last_fetched(["KO"]))["KO"] == day1_utc

    # Day 10: successful fetch with one row keeps the missing row and updates the rest.
    day10_utc = datetime(2026, 11, 11, 10, 0, tzinfo=ET).astimezone(UTC)
    clock.now = datetime(2026, 11, 11, 10, 0, tzinfo=ET)
    client.result = FetchResult(ok=True, rows=[_row(date(2026, 9, 15), "0.52")])
    await job.run(date(2026, 11, 11))
    async with factory() as s:
        repo = DividendRepo(s)
        events = await repo.list_events("KO")
        assert [(e.ex_dividend_date, e.amount) for e in events] == [
            (date(2026, 6, 13), Decimal("0.51")),
            (date(2026, 9, 15), Decimal("0.52")),
        ]
        assert (await repo.last_fetched(["KO"]))["KO"] == day10_utc


class PerSymbolClient:
    def __init__(self, results: dict[str, FetchResult]) -> None:
        self.results = results

    async def fetch(self, symbol, may_request):
        await may_request()
        return self.results[symbol]


async def test_rows_postgres_rejects_skip_that_symbol_only(factory):
    # These rows bypass parse_dividends, which drops them; this proves the job's fallback.
    now = datetime(2026, 11, 2, 10, 0, tzinfo=ET)
    client = PerSymbolClient(
        {
            "BIG": FetchResult(ok=True, rows=[_row(date(2026, 9, 15), "1000000000000")]),
            "DUP": FetchResult(
                ok=True, rows=[_row(date(2026, 9, 15), "0.51"), _row(date(2026, 9, 15), "0.52")]
            ),
            "KO": FetchResult(ok=True, rows=[_row(date(2026, 9, 15), "0.51")]),
        }
    )
    job = DividendRefreshJob(
        client, factory, FakeCache(), symbols=["BIG", "DUP", "KO"], clock=lambda: now
    )

    await job.run(date(2026, 11, 2))

    async with factory() as s:
        repo = DividendRepo(s)
        assert await repo.list_events("BIG") == []
        assert await repo.list_events("DUP") == []
        assert len(await repo.list_events("KO")) == 1
        assert set(await repo.last_fetched(["BIG", "DUP", "KO"])) == {"KO"}
