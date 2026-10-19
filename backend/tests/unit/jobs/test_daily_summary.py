"""Unit tests for DailySummaryJob (D5)."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.jobs import daily_summary
from app.core.jobs.daily_summary import LAST_RUN_KEY, DailySummaryJob
from app.core.market_calendar import ET


@asynccontextmanager
async def _session_factory():
    yield object()


class FakeCache:
    def __init__(self):
        self.data = {
            "price:AAPL": json.dumps({"price": "110", "timestamp": "2026-10-19T20:00:00+00:00"})
        }

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.data[key] = value


def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def _job(monkeypatch, now):
    clock = SimpleNamespace(now=now)
    collector = SimpleNamespace(record_snapshot=AsyncMock())
    dispatcher = SimpleNamespace(notify_daily_summary=AsyncMock())
    closed = [SimpleNamespace(pnl=Decimal("50")), SimpleNamespace(pnl=Decimal("-20"))]
    open_trade = SimpleNamespace(
        symbol="AAPL", entry_fill_price=Decimal("100"), entry_price=Decimal("99"),
        filled_quantity=Decimal("10"), quantity=Decimal("10"),
    )

    class Repo:
        def __init__(self, _s):
            pass

        async def closed_since(self, _start):
            return closed

        async def opened_since(self, _start):
            return closed + [open_trade]

        async def get_open_trades(self):
            return [open_trade]

    monkeypatch.setattr(daily_summary, "TradeRepo", Repo)
    job = DailySummaryJob(
        SimpleNamespace(), _session_factory, FakeCache(), dispatcher,
        run_at=time(16, 45), clock=lambda: clock.now, collector=collector,
    )
    return job, clock, collector, dispatcher


async def test_runs_once_per_trading_day(monkeypatch):
    job, clock, collector, dispatcher = _job(monkeypatch, _at(2026, 10, 19, 16, 50))
    await job.tick()
    await job.tick()
    assert dispatcher.notify_daily_summary.await_count == 1
    collector.record_snapshot.assert_awaited_once()
    assert job._cache.data[LAST_RUN_KEY] == "2026-10-19"
    clock.now = _at(2026, 10, 20, 16, 50)
    await job.tick()
    assert dispatcher.notify_daily_summary.await_count == 2


async def test_not_before_run_time_and_not_on_holiday(monkeypatch):
    job, clock, _, dispatcher = _job(monkeypatch, _at(2026, 10, 19, 16, 0))
    await job.tick()
    clock.now = _at(2026, 12, 25, 17, 0)
    await job.tick()
    dispatcher.notify_daily_summary.assert_not_awaited()


async def test_summary_content(monkeypatch):
    job, _, _, dispatcher = _job(monkeypatch, _at(2026, 10, 19, 16, 50))
    job.record_alert("CIRCUIT_BREAKER_HALT")
    job.record_alert("PRICE_FEED_STALE")
    await job.tick()
    subject, body = dispatcher.notify_daily_summary.await_args.args
    assert "$30.00" in subject
    assert "Trades opened today: 3" in body and "Trades closed today: 2" in body
    assert "AAPL: 10 @ $100, last $110, P&L $100.00" in body
    assert "Halts and strategy kills: 1" in body and "PRICE_FEED_STALE: 1" in body
    assert not job._alert_counts


async def test_redis_failure_skips_instead_of_duplicating(monkeypatch):
    job, _, _, dispatcher = _job(monkeypatch, _at(2026, 10, 19, 16, 50))
    job._cache.get = AsyncMock(side_effect=ConnectionError("down"))
    await job.tick()
    dispatcher.notify_daily_summary.assert_not_awaited()
