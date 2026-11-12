"""B8: strategy seeding, /strategies/types, POST validation, PATCH validation."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.v1.strategies import _strategy_symbols
from app.core.strategy_engine.builtin import load_builtin_strategies
from app.core.strategy_engine.registry import registry
from app.core.strategy_engine.seed import config_error, default_config, seed_strategies
from app.dependencies import get_db
from app.main import app

load_builtin_strategies()


class MemRepo:
    rows: list = []

    def __init__(self, _session):
        pass

    async def get_all(self):
        return list(MemRepo.rows)

    async def get_by_id(self, strategy_id):
        return next((r for r in MemRepo.rows if r.id == strategy_id), None)

    async def create(self, *, name, type, config=None, is_enabled=False):
        row = SimpleNamespace(
            id=uuid.uuid4(), name=name, type=type, config=config or {}, is_enabled=is_enabled,
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        )
        MemRepo.rows.append(row)
        return row

    async def patch(self, strategy_id, *, is_enabled=None, config=None):
        row = await self.get_by_id(strategy_id)
        if config is not None:
            row.config = config
        if is_enabled is not None:
            row.is_enabled = is_enabled
        return row


class FakeSession:
    async def commit(self):
        pass


@asynccontextmanager
async def _factory():
    yield FakeSession()


@pytest.fixture(autouse=True)
def _mem(monkeypatch):
    MemRepo.rows = []
    monkeypatch.setattr("app.db.repositories.strategy_repo.StrategyRepo", MemRepo)
    monkeypatch.setattr("app.api.v1.strategies.StrategyRepo", MemRepo)


async def test_seed_twice_creates_each_type_once_all_disabled():
    first = await seed_strategies(registry, _factory)
    second = await seed_strategies(registry, _factory)
    assert first == 8 and second == 0
    assert {r.type for r in MemRepo.rows} == set(registry.registered_types()) - {"composite"}
    assert all(r.is_enabled is False for r in MemRepo.rows)


def test_default_config_uses_schema_defaults():
    schema = registry.get_class("moving_average").get_config_schema()
    cfg = default_config(schema)
    assert config_error(registry, "moving_average", cfg) is None


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_types_lists_all_nine(client):
    data = client.get("/api/v1/strategies/types").json()["data"]
    assert len(data) == 9
    assert all(item["config_schema"]["properties"] for item in data)


def test_post_invalid_periods_is_422(client):
    r = client.post(
        "/api/v1/strategies",
        json={"name": "bad", "type": "moving_average",
              "config": {"fast_period": 200, "slow_period": 50, "symbols": ["AAPL"]}},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_STRATEGY_CONFIG"


def test_post_unknown_type_is_422(client):
    r = client.post("/api/v1/strategies", json={"name": "x", "type": "nope"})
    assert r.status_code == 422


def test_post_valid_creates_and_uppercases_symbols(client):
    r = client.post(
        "/api/v1/strategies",
        json={"name": "ma", "type": "moving_average",
              "config": {"fast_period": 10, "slow_period": 50, "symbols": ["aapl"]}},
    )
    assert r.status_code == 201
    assert r.json()["data"]["config"]["symbols"] == ["AAPL"]
    assert r.json()["data"]["is_enabled"] is False


def test_post_bad_symbols_is_422(client):
    r = client.post(
        "/api/v1/strategies",
        json={"name": "ma", "type": "moving_average", "config": {"symbols": ["WAYTOOLONGTICKER"]}},
    )
    assert r.status_code == 422


def test_patch_invalid_config_is_422_and_row_unchanged(client):
    row = SimpleNamespace(
        id=uuid.uuid4(), name="ma", type="moving_average",
        config={"fast_period": 10, "slow_period": 50, "symbols": []}, is_enabled=False,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    MemRepo.rows.append(row)
    r = client.patch(
        f"/api/v1/strategies/{row.id}",
        json={"config": {"fast_period": 200, "slow_period": 50}},
    )
    assert r.status_code == 422
    assert row.config["fast_period"] == 10


def _fake_engine(started=True):
    return SimpleNamespace(
        started=started,
        bar_refresh=SimpleNamespace(refresh=MagicMock(return_value="job")),
        spawn=MagicMock(),
    )


def test_post_strategy_spawns_backfill_with_extra_symbols(client, monkeypatch):
    fake = _fake_engine()
    monkeypatch.setattr(app.state, "engine", fake, raising=False)
    r = client.post(
        "/api/v1/strategies",
        json={"name": "tp", "type": "trend_pullback", "config": {"symbols": ["aapl"]}},
    )
    assert r.status_code == 201
    fake.bar_refresh.refresh.assert_called_once()
    symbols, duration = fake.bar_refresh.refresh.call_args.args
    assert set(symbols) >= {"AAPL", "SPY"}
    assert duration == "2 Y"
    fake.spawn.assert_called_once_with("job", "bar-backfill")


def test_patch_strategy_spawns_backfill(client, monkeypatch):
    fake = _fake_engine()
    monkeypatch.setattr(app.state, "engine", fake, raising=False)
    r = client.post(
        "/api/v1/strategies",
        json={"name": "tp", "type": "trend_pullback", "config": {"symbols": ["aapl"]}},
    )
    strategy_id = r.json()["data"]["id"]
    fake.bar_refresh.refresh.reset_mock()
    fake.spawn.reset_mock()

    r = client.patch(
        f"/api/v1/strategies/{strategy_id}", json={"config": {"symbols": ["msft"]}}
    )
    assert r.status_code == 200
    fake.bar_refresh.refresh.assert_called_once()
    symbols, duration = fake.bar_refresh.refresh.call_args.args
    assert set(symbols) >= {"MSFT", "SPY"}
    assert duration == "2 Y"
    fake.spawn.assert_called_once_with("job", "bar-backfill")


def test_no_backfill_when_engine_not_started(client, monkeypatch):
    fake = _fake_engine(started=False)
    monkeypatch.setattr(app.state, "engine", fake, raising=False)
    r = client.post(
        "/api/v1/strategies",
        json={"name": "tp", "type": "trend_pullback", "config": {"symbols": ["aapl"]}},
    )
    assert r.status_code == 201
    fake.bar_refresh.refresh.assert_not_called()
    fake.spawn.assert_not_called()


def test_strategy_symbols_survives_build_failure():
    assert _strategy_symbols("no_such_type", {"symbols": ["msft"]}) == {"MSFT"}
