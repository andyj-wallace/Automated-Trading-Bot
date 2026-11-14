"""TradingEngine startup: wrong Gateway account and position-monitor wiring."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.brokers.ibkr.client import LiveTradingGuardError
from app.config import get_settings
from app.core.engine import TradingEngine


async def test_account_mismatch_at_startup_is_emailed_directly():
    engine = TradingEngine.__new__(TradingEngine)  # skip __init__: no DB, Redis or broker needed
    engine._seed_strategies = AsyncMock()
    engine.broker = SimpleNamespace(connect=AsyncMock(side_effect=LiveTradingGuardError("live account")))
    engine.cache = None
    engine.dispatcher = SimpleNamespace(notify_system_alert=AsyncMock())

    await engine.start()

    engine.dispatcher.notify_system_alert.assert_awaited_once_with(
        "BROKER_ACCOUNT_MODE_MISMATCH", "live account"
    )


def test_engine_backs_off_failed_trailing_stop_writes(monkeypatch):
    captured: dict = {}

    class _MonitorBuilt(Exception):
        pass

    def fake_monitor(**kwargs):
        captured.update(kwargs)
        raise _MonitorBuilt  # stop before the rest of the engine is built

    monkeypatch.setattr("app.core.execution.position_monitor.PositionMonitor", fake_monitor)
    for builder in ("_build_broker", "_build_cache", "_build_circuit_breaker"):
        monkeypatch.setattr(f"app.dependencies.{builder}", AsyncMock)

    with pytest.raises(_MonitorBuilt):
        TradingEngine(get_settings())

    assert captured["stop_persist_retry_seconds"] > 0
