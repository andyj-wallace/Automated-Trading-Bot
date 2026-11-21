"""Unit tests for DividendRefreshJob: request budget, midnight rollover, freshness and secrecy."""

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from app.core.jobs.dividend_refresh import BUDGET_KEY, DividendRefreshJob
from app.core.market_calendar import ET
from app.data.alpha_vantage import AlphaVantageClient, DividendRow, FetchResult
from app.monitoring.logger import system_logger

SYMBOLS = [f"S{i:02d}" for i in range(30)]
SECRET = "SECRET-KEY-123"


def _at(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def _key(day: date) -> str:
    return BUDGET_KEY.format(day=day.isoformat())


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self.data[key] = value


class FakeSession:
    async def commit(self) -> None:
        pass


@asynccontextmanager
async def _session_factory():
    yield FakeSession()


class Store:
    def __init__(self) -> None:
        self.last: dict[str, datetime] = {}
        self.rows: dict[str, list] = {}


class FakeRepo:
    def __init__(self, session, store: Store) -> None:
        self.store = store

    async def last_fetched(self, symbols):
        return {s: self.store.last[s] for s in symbols if s in self.store.last}

    async def has_ex_date_between(self, symbol, after, through) -> bool:
        return any(
            after < row.ex_dividend_date <= through for row in self.store.rows.get(symbol, [])
        )

    async def upsert_events(self, symbol, rows, fetched_at) -> int:
        self.store.rows.setdefault(symbol, []).extend(rows)
        return len(rows)

    async def mark_fetched(self, symbol, fetched_at) -> None:
        self.store.last[symbol] = fetched_at


class FakeClient:
    def __init__(self, clock: Clock, result: FetchResult | None = None, on_call=None) -> None:
        self.clock = clock
        self.result = result or FetchResult(ok=True, rows=[])
        self.on_call = on_call
        self.calls: list[str] = []
        self.days: list[date] = []

    async def fetch(self, symbol, may_request) -> FetchResult:
        if not await may_request():
            return FetchResult(ok=False, error="budget_exhausted")
        self.calls.append(symbol)
        self.days.append(self.clock().date())
        if self.on_call:
            self.on_call(symbol)
        return self.result


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG)
    system_logger.addHandler(caplog.handler)
    yield caplog
    system_logger.removeHandler(caplog.handler)


def _job(client, clock, cache, store, symbols=SYMBOLS, budget=20) -> DividendRefreshJob:
    return DividendRefreshJob(
        client,
        _session_factory,
        cache,
        symbols=symbols,
        daily_budget=budget,
        clock=clock,
        repo_factory=lambda s: FakeRepo(s, store),
    )


def _real_client(handler) -> AlphaVantageClient:
    return AlphaVantageClient(SECRET, transport=httpx.MockTransport(handler))


async def test_budget_caps_requests_per_day_and_resumes_next_day(logs):
    clock = Clock(_at(2026, 11, 2, 10, 0))
    cache, store = FakeCache(), Store()
    client = FakeClient(clock)
    job = _job(client, clock, cache, store)

    await job.tick()

    assert client.calls == SYMBOLS[:20]
    skipped_records = [r for r in logs.records if r.getMessage() == "Dividend refresh budget spent"]
    assert skipped_records
    assert all(r.skipped_symbols == SYMBOLS[20:] for r in skipped_records)
    assert cache.data[_key(date(2026, 11, 2))] == "20"

    clock.now = _at(2026, 11, 3, 10, 0)
    await job.tick()

    assert client.calls == SYMBOLS
    assert cache.data[_key(date(2026, 11, 3))] == "10"


async def test_midnight_crossing_charges_each_request_to_its_own_et_day():
    clock = Clock(_at(2026, 11, 2, 23, 59))
    cache, store = FakeCache(), Store()

    def advance(symbol: str) -> None:
        if symbol == "S00":
            clock.now = _at(2026, 11, 3, 0, 1)

    client = FakeClient(clock, on_call=advance)
    job = _job(client, clock, cache, store)

    await job.run(date(2026, 11, 2))
    clock.now = _at(2026, 11, 3, 10, 0)
    await job.tick()

    assert client.calls == SYMBOLS[:21]
    assert client.days.count(date(2026, 11, 2)) == 1
    assert client.days.count(date(2026, 11, 3)) == 20
    assert cache.data[_key(date(2026, 11, 2))] == "1"
    assert cache.data[_key(date(2026, 11, 3))] == "20"


async def test_failed_fetch_stores_nothing_but_counts_against_budget():
    clock = Clock(_at(2026, 11, 2, 10, 0))
    cache, store = FakeCache(), Store()
    client = FakeClient(clock, result=FetchResult(ok=False, error="no_data_list"))
    job = _job(client, clock, cache, store, symbols=["S00"])

    await job.run(date(2026, 11, 2))

    assert store.rows == {}
    assert store.last == {}
    assert cache.data[_key(date(2026, 11, 2))] == "1"


async def test_no_client_makes_no_requests_and_does_not_raise(logs):
    clock = Clock(_at(2026, 11, 2, 10, 0))
    cache, store = FakeCache(), Store()
    job = _job(None, clock, cache, store)

    await job.tick()

    assert cache.data == {}
    assert any(r.getMessage() == "Dividend refresh disabled" for r in logs.records)


async def test_http_500_does_not_leak_api_key(logs):
    clock = Clock(_at(2026, 11, 2, 10, 0))
    cache, store = FakeCache(), Store()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    job = _job(_real_client(handler), clock, cache, store, symbols=["AAA"])

    await job.tick()

    assert SECRET not in logs.text
    assert all(SECRET not in str(r.__dict__) for r in logs.records)


async def test_connect_error_does_not_leak_api_key(logs):
    clock = Clock(_at(2026, 11, 2, 10, 0))
    cache, store = FakeCache(), Store()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"fail {request.url}", request=request)

    job = _job(_real_client(handler), clock, cache, store, symbols=["AAA"])

    await job.tick()

    assert SECRET not in logs.text
    assert all(SECRET not in str(r.__dict__) for r in logs.records)


async def test_retry_refused_by_budget_stops_after_one_request():
    clock = Clock(_at(2026, 11, 2, 10, 0))
    cache, store = FakeCache(), Store()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down", request=request)

    job = _job(_real_client(handler), clock, cache, store, symbols=["AAA"], budget=1)

    await job.run(date(2026, 11, 2))

    assert calls == 1
    assert cache.data[_key(date(2026, 11, 2))] == "1"
    assert store.rows == {}
    assert store.last == {}


async def test_fresh_symbol_is_skipped_until_its_ex_date_has_passed():
    clock = Clock(_at(2026, 11, 2, 10, 0))
    cache, store = FakeCache(), Store()
    row = DividendRow(
        ex_dividend_date=date(2026, 11, 4),
        declaration_date=None,
        record_date=None,
        payment_date=None,
        amount=Decimal("0.50"),
        declared=True,
    )
    client = FakeClient(clock, result=FetchResult(ok=True, rows=[row]))
    job = _job(client, clock, cache, store, symbols=["S00"])

    await job.run(date(2026, 11, 2))
    assert client.calls == ["S00"]

    clock.now = _at(2026, 11, 3, 10, 0)
    await job.run(date(2026, 11, 3))
    assert client.calls == ["S00"]

    clock.now = _at(2026, 11, 5, 10, 0)
    await job.run(date(2026, 11, 5))
    assert client.calls == ["S00", "S00"]
