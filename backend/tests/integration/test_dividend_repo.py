"""DividendRepo: unique key, upsert keeps omitted rows, fetch status, ex-date window."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import DividendEvent
from app.db.repositories.dividend_repo import DividendRepo

_DB_URL = os.environ["DATABASE_URL"]


@dataclass(frozen=True)
class _Row:
    ex_dividend_date: date
    declaration_date: date | None
    record_date: date | None
    payment_date: date | None
    amount: Decimal


@pytest_asyncio.fixture
async def factory():
    eng = create_async_engine(_DB_URL, echo=False)
    async with eng.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE dividend_events, dividend_fetch_status"))
    yield sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


async def test_unique_symbol_ex_date_key(factory):
    fetched = datetime(2026, 1, 1, tzinfo=UTC)
    kwargs = {"symbol": "KO", "ex_dividend_date": date(2026, 3, 14), "amount": Decimal("0.49"),
              "fetched_at": fetched}
    async with factory() as s:
        s.add(DividendEvent(**kwargs))
        s.add(DividendEvent(**kwargs))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_upsert_updates_existing_and_keeps_omitted_rows(factory):
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 2, 1, tzinfo=UTC)
    first = [
        _Row(date(2026, 3, 14), date(2026, 2, 1), date(2026, 3, 15), date(2026, 4, 1),
             Decimal("0.49")),
        _Row(date(2026, 6, 13), None, None, None, Decimal("0.50")),
    ]
    async with factory() as s:
        assert await DividendRepo(s).upsert_events("KO", first, t1) == 2
        await s.commit()

    updated = [
        _Row(date(2026, 3, 14), date(2026, 2, 1), date(2026, 3, 15), date(2026, 4, 5),
             Decimal("0.51")),
    ]
    async with factory() as s:
        assert await DividendRepo(s).upsert_events("KO", updated, t2) == 1
        await s.commit()

    async with factory() as s:
        events = await DividendRepo(s).list_events("KO")
    assert len(events) == 2
    assert events[0].ex_dividend_date == date(2026, 3, 14)
    assert events[0].amount == Decimal("0.51")
    assert events[0].payment_date == date(2026, 4, 5)
    assert events[0].fetched_at == t2
    assert events[1].ex_dividend_date == date(2026, 6, 13)
    assert events[1].amount == Decimal("0.50")


async def test_upsert_empty_returns_zero(factory):
    async with factory() as s:
        assert await DividendRepo(s).upsert_events("KO", [], datetime.now(UTC)) == 0


async def test_mark_fetched_and_last_fetched(factory):
    early = datetime(2026, 1, 1, tzinfo=UTC)
    late = datetime(2026, 2, 1, tzinfo=UTC)
    async with factory() as s:
        await DividendRepo(s).mark_fetched("KO", early)
        await s.commit()
    async with factory() as s:
        await DividendRepo(s).mark_fetched("KO", late)
        await s.commit()
    async with factory() as s:
        got = await DividendRepo(s).last_fetched(["KO", "PEP"])
    assert got == {"KO": late}


async def test_has_ex_date_between_window(factory):
    fetched = datetime(2026, 1, 1, tzinfo=UTC)
    async with factory() as s:
        await DividendRepo(s).upsert_events(
            "KO", [_Row(date(2026, 3, 14), None, None, None, Decimal("0.49"))], fetched
        )
        await s.commit()
    async with factory() as s:
        repo = DividendRepo(s)
        assert await repo.has_ex_date_between("KO", date(2026, 3, 1), date(2026, 3, 31))
        assert await repo.has_ex_date_between("KO", date(2026, 3, 13), date(2026, 3, 14))
        assert not await repo.has_ex_date_between("KO", date(2026, 3, 14), date(2026, 3, 31))
        assert not await repo.has_ex_date_between("PEP", date(2026, 3, 1), date(2026, 3, 31))
