"""K2: backtest API fee schedules, compounding, and extra-symbol bars."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import backtesting
from app.core.backtesting.engine import BacktestingEngine
from app.core.risk.manager import RiskManager
from app.core.strategy_engine.builtin import load_builtin_strategies
from app.dependencies import get_db
from tests.unit.strategy.conftest import make_bars_from_closes

load_builtin_strategies()

MR_CONFIG = {"lookback": 4, "z_score_threshold": "1.0"}
CLOSES = [10.0] * 30 + [4.0] + [10.0] * 10


def _rows():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        SimpleNamespace(
            time=start + timedelta(days=i), open=Decimal("25"), high=Decimal("25.5"),
            low=Decimal("24.5"), close=Decimal("25"), volume=1_000_000, bar_size="1 day",
        )
        for i in range(30)
    ]


@pytest.fixture
def client(monkeypatch):
    async def get_bars(self, symbol, start, end):
        return _rows() if symbol == "SCHB" else []

    monkeypatch.setattr(backtesting.OHLCVRepo, "get_bars", get_bars)
    app = FastAPI()
    app.include_router(backtesting.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: MagicMock()
    return TestClient(app)


def test_regime_strategy_without_spy_bars_is_422_naming_spy(client):
    r = client.post("/api/v1/backtesting/run", json={
        "symbol": "SCHB", "strategy_type": "trend_pullback", "commission_plan": "ibkr_tiered",
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NO_HISTORICAL_DATA"
    assert "SPY" in r.json()["error"]["message"]


def test_invalid_strategy_config_is_422(client):
    r = client.post("/api/v1/backtesting/run", json={
        "symbol": "SCHB", "strategy_type": "trend_pullback", "strategy_config": {"fast_period": 99},
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_CONFIG"


def test_day_trade_strategy_is_422_in_backtests(client):
    r = client.post("/api/v1/backtesting/run", json={"symbol": "SCHB", "strategy_type": "vwap_reclaim"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_CONFIG"
    assert "5-minute" in r.json()["error"]["message"]


async def _run(**request):
    body = backtesting.BacktestRunRequest(symbol="SCHB", strategy_type="mean_reversion",
                                          strategy_config=MR_CONFIG, account_balance="1000", **request)
    job = backtesting._Job(job_id="j", request=body)
    job.costs = backtesting._build_costs(body)
    await backtesting._run_job_locked(job, make_bars_from_closes(CLOSES))
    assert job.status == "done", job.error
    return job


async def test_priced_run_reports_ending_balance_and_fees():
    job = await _run(commission_plan="ibkr_tiered", compound=True)
    metrics = backtesting._serialise_metrics(job.result.metrics)
    assert Decimal(metrics["ending_balance"]) == Decimal("1000") + Decimal(metrics["total_return"])
    assert "total_commission_paid" in metrics


async def test_legacy_matches_the_plain_engine():
    job = await _run()
    strategy = backtesting.registry.build("mean_reversion", MR_CONFIG)
    plain = await BacktestingEngine(strategy, RiskManager()).run(
        make_bars_from_closes(CLOSES), "SCHB", Decimal("1000")
    )
    assert backtesting._serialise_metrics(job.result.metrics) == backtesting._serialise_metrics(plain.metrics)
