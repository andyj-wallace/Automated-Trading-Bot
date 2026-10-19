"""D6: engine component in /system/health; /health stays liveness-only."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.brokers.base import BaseBroker
from app.data.cache import RedisCache
from app.dependencies import get_broker, get_cache, get_db
from app.main import app


def _engine(leader=True, started=True):
    return SimpleNamespace(
        started=started,
        is_leader=leader,
        feed=SimpleNamespace(
            subscribed_tickers=frozenset({"AAPL", "MSFT"}),
            last_tick_at=lambda s: datetime.now(UTC) - timedelta(seconds=7),
        ),
        position_monitor=SimpleNamespace(tracked_symbols=lambda: {"AAPL"}, tracked_count=1),
        scheduler=SimpleNamespace(last_cycle_at=None),
        trading_state=SimpleNamespace(is_paused=AsyncMock(return_value=False)),
        market_open=lambda: True,
    )


@pytest.fixture
def client():
    broker = MagicMock(spec=BaseBroker)
    broker.is_connected.return_value = False
    cache = MagicMock(spec=RedisCache)
    cache.ping = AsyncMock(return_value=True)
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_cache] = lambda: cache
    yield TestClient(app), broker
    app.dependency_overrides.clear()
    if hasattr(app.state, "engine"):
        del app.state.engine


def test_engine_missing_is_error_and_503(client):
    c, broker = client
    broker.is_connected.return_value = True
    resp = c.get("/api/v1/system/health")
    assert resp.status_code == 503
    assert resp.json()["data"]["engine"]["status"] == "error"


def test_engine_ok_reports_info(client):
    c, broker = client
    broker.is_connected.return_value = True
    app.state.engine = _engine()
    resp = c.get("/api/v1/system/health")
    engine = resp.json()["data"]["engine"]
    assert resp.status_code == 200 and engine["status"] == "ok"
    info = engine["info"]
    assert info["feed_subscriptions"] == 2 and info["tracked_trades"] == 1
    assert 6 <= info["max_tick_age_seconds"] <= 10
    assert info["paused"] is False and info["market_open"] is True
    assert info["scheduler_last_cycle_at"] is None


def test_engine_without_lock_is_error(client):
    c, broker = client
    broker.is_connected.return_value = True
    app.state.engine = _engine(leader=False)
    resp = c.get("/api/v1/system/health")
    assert resp.status_code == 503 and resp.json()["data"]["engine"]["status"] == "error"


def test_liveness_is_200_with_broker_disconnected(client):
    c, _ = client
    assert c.get("/health").status_code == 200
