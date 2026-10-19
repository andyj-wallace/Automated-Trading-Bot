"""D7: backtests run in a worker thread so the trading event loop keeps ticking."""

import asyncio
import threading
import time
from types import SimpleNamespace

from app.api.v1 import backtesting


async def test_backtest_does_not_block_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen = {}

    async def slow_run(self, **kwargs):
        seen["thread"] = threading.get_ident()
        time.sleep(0.4)
        return "result"

    monkeypatch.setattr(backtesting.BacktestingEngine, "run", slow_run)
    monkeypatch.setattr(backtesting.registry, "build", lambda *a, **k: object())
    job = backtesting._Job(
        job_id="j",
        request=SimpleNamespace(
            strategy_type="x", strategy_config={}, symbol="aapl", account_balance="1000"
        ),
    )

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    task = asyncio.create_task(ticker())
    await backtesting._run_job(job, [])
    task.cancel()

    assert job.status == "done" and job.result == "result"
    assert seen["thread"] != loop_thread
    assert ticks >= 5


async def test_one_backtest_at_a_time(monkeypatch):
    running = 0
    peak = 0

    async def fake_locked(job, bars):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    monkeypatch.setattr(backtesting, "_run_job_locked", fake_locked)
    monkeypatch.setattr(backtesting, "_backtest_slot", asyncio.Semaphore(1))
    await asyncio.gather(*(backtesting._run_job(object(), []) for _ in range(3)))
    assert peak == 1
