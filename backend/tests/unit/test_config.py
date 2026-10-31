from datetime import time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.fixture(autouse=True)
def _base_env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db_test")
    for name in (
        "MAX_PORTFOLIO_RISK_PCT",
        "STRATEGY_EVAL_START_ET",
        "CORS_ORIGINS",
        "ENGINE_ENABLED",
        "LOG_DIR",
        "IB_AUTO_RESTART_TIME",
    ):
        monkeypatch.delenv(name, raising=False)


def _settings() -> Settings:
    return Settings(_env_file=None)


def test_defaults_load():
    s = _settings()
    assert s.max_portfolio_risk_pct == Decimal("0.05")
    assert s.min_reward_to_risk == Decimal("2.0")
    assert s.daily_loss_limit_pct == Decimal("0.03")
    assert s.max_consecutive_losses == 5
    assert s.strategy_eval_start_et == time(9, 45)
    assert s.strategy_eval_end_et == time(15, 45)
    assert s.cors_origins == ["http://localhost:5173"]
    assert s.broker_protective_stops is True
    assert s.ib_auto_restart_time == time(19, 0)


def test_portfolio_risk_above_cap_rejected(monkeypatch):
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "0.11")
    with pytest.raises(ValidationError):
        _settings()


def test_portfolio_risk_must_be_positive(monkeypatch):
    monkeypatch.setenv("MAX_PORTFOLIO_RISK_PCT", "0")
    with pytest.raises(ValidationError):
        _settings()


def test_time_parses(monkeypatch):
    monkeypatch.setenv("STRATEGY_EVAL_START_ET", "10:00")
    assert _settings().strategy_eval_start_et == time(10, 0)


def test_cors_origins_json(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["https://a"]')
    assert _settings().cors_origins == ["https://a"]


@pytest.mark.parametrize("value", ["07:00 PM", "19:00"])
def test_gateway_restart_time_accepts_both_formats(monkeypatch, value):
    monkeypatch.setenv("IB_AUTO_RESTART_TIME", value)
    assert _settings().ib_auto_restart_time == time(19, 0)


def test_gateway_restart_time_rejects_garbage(monkeypatch):
    monkeypatch.setenv("IB_AUTO_RESTART_TIME", "7pm")
    with pytest.raises(ValidationError):
        _settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COMMISSION_PLAN", "bogus"),
        ("ACCOUNT_TYPE", "bogus"),
        ("MAX_BREAKEVEN_WIN_RATE", "1"),
        ("GROWTH_MAX_OPEN_POSITIONS", "0"),
    ],
)
def test_growth_settings_reject_bad_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        _settings()


def test_growth_universe_is_normalized(monkeypatch):
    monkeypatch.setenv("GROWTH_UNIVERSE", '[" schb"]')
    assert _settings().growth_universe == ["SCHB"]
