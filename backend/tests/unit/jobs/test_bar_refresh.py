"""Unit tests for BarRefreshJob (B6)."""

from contextlib import asynccontextmanager
from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.brokers.mock.client import MockBroker
from app.core.jobs.bar_refresh import BarRefreshJob
from app.core.market_calendar import ET


class FakeSession:
    async def commit(self) -> None:
        pass


@asynccontextmanager
async def _session_factory():
    yield FakeSession()


def _at(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _job(clock: Clock, fetch=None):
    broker = MockBroker()
    await broker.connect()
    cache = AsyncMock()
    job = BarRefreshJob(
        broker, _session_factory, cache, run_at=time(16, 30), clock=clock, pause_seconds=0
    )
    job.desired_symbols = AsyncMock(return_value={"AAPL"})
    job._fetcher = SimpleNamespace(fetch_and_store=fetch or AsyncMock(return_value=5))
    return job, cache


async def test_startup_backfill_runs_once_then_waits_for_run_time():
    clock = Clock(_at(2026, 10, 19, 10, 0))  # Monday
    job, _ = await _job(clock)
    await job.tick()
    assert job._fetcher.fetch_and_store.await_args.kwargs["duration"] == "2 Y"
    await job.tick()  # before 16:30: nothing
    assert job._fetcher.fetch_and_store.await_count == 1


async def test_daily_refresh_runs_once_after_run_time():
    clock = Clock(_at(2026, 10, 19, 10, 0))
    job, _ = await _job(clock)
    await job.tick()
    clock.now = _at(2026, 10, 19, 16, 31)
    await job.tick()
    assert job._fetcher.fetch_and_store.await_count == 2
    assert job._fetcher.fetch_and_store.await_args.kwargs["duration"] == "10 D"
    await job.tick()
    assert job._fetcher.fetch_and_store.await_count == 2


async def test_holiday_does_nothing():
    clock = Clock(_at(2026, 12, 25, 16, 45))  # Christmas
    job, _ = await _job(clock)
    job._startup_done = True
    await job.tick()
    job._fetcher.fetch_and_store.assert_not_awaited()


async def test_disconnected_broker_does_nothing():
    job, _ = await _job(Clock(_at(2026, 10, 19, 16, 45)))
    await job._broker.disconnect()
    await job.tick()
    job._fetcher.fetch_and_store.assert_not_awaited()


async def test_empty_response_publishes_one_alert():
    fetch = AsyncMock(return_value=0)
    job, cache = await _job(Clock(_at(2026, 10, 19, 10, 0)), fetch=fetch)
    job.desired_symbols = AsyncMock(return_value={"AAPL", "SPY"})
    await job.tick()
    cache.publish.assert_awaited_once()
    assert "AAPL, SPY" in cache.publish.await_args.args[1]


async def test_raising_fetch_counts_as_failed():
    fetch = AsyncMock(side_effect=RuntimeError("pacing"))
    job, cache = await _job(Clock(_at(2026, 10, 19, 10, 0)), fetch=fetch)
    await job.tick()
    cache.publish.assert_awaited_once()


async def test_desired_symbols_includes_extra_symbols(monkeypatch):
    from app.core.strategy_engine.builtin import load_builtin_strategies
    from app.core.strategy_engine.registry import registry

    load_builtin_strategies()
    job = BarRefreshJob(
        MockBroker(), _session_factory, AsyncMock(), run_at=time(16, 30), registry=registry
    )
    strategy = SimpleNamespace(id=1, type="bull_bear", config={"symbols": ["qqq"]})
    import app.core.jobs.bar_refresh as mod

    class Repo:
        def __init__(self, _s):
            pass

        async def get_all(self, active_only=False):
            return [SimpleNamespace(ticker="msft")]

        async def get_enabled(self):
            return [strategy]

        async def get_active_trades(self):
            return [SimpleNamespace(symbol="tsla")]

    for name in ("SymbolRepo", "StrategyRepo", "TradeRepo"):
        monkeypatch.setattr(mod, name, Repo)
    symbols = await job.desired_symbols()
    assert {"MSFT", "QQQ", "TSLA", "SPY"} <= symbols
