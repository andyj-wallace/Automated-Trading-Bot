"""Circuit-breaker admin endpoints (C7): status fields and the durable kill."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import risk
from app.core.risk.circuit_breaker import CircuitBreaker
from app.dependencies import get_circuit_breaker, get_db


@pytest.fixture
def setup(monkeypatch):
    cb = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"))
    db = AsyncMock()
    set_enabled = AsyncMock()
    monkeypatch.setattr(risk.StrategyRepo, "set_enabled", set_enabled)
    app = FastAPI()
    app.include_router(risk.router, prefix="/api/v1")
    app.dependency_overrides[get_circuit_breaker] = lambda: cb
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), cb, db, set_enabled


def test_status_reports_thresholds_date_and_streaks(setup):
    client, cb, _, _ = setup
    sid = uuid.uuid4()
    cb.record_closed_trade(Decimal("-100"), Decimal("100000"), sid)

    data = client.get("/api/v1/risk/circuit-breaker").json()["data"]

    assert data["daily_loss_limit_pct"] == "0.03"
    assert data["halt_threshold_amount"] == "3000.00"
    assert data["trading_date"] is not None
    assert data["consecutive_losses"] == {str(sid): 1}
    assert data["daily_halted"] is False


def test_kill_also_disables_strategy_in_database(setup):
    client, cb, db, set_enabled = setup
    sid = uuid.uuid4()

    resp = client.post(f"/api/v1/risk/circuit-breaker/strategies/{sid}/kill")

    assert resp.status_code == 200
    assert cb.is_strategy_killed(sid)
    set_enabled.assert_awaited_once_with(sid, False)
    db.commit.assert_awaited_once()


def test_reset_strategy_clears_streak_without_touching_database(setup):
    client, cb, _, set_enabled = setup
    sid = uuid.uuid4()
    cb.kill_strategy(sid)

    client.post(f"/api/v1/risk/circuit-breaker/strategies/{sid}/reset")

    assert not cb.is_strategy_killed(sid)
    set_enabled.assert_not_awaited()
