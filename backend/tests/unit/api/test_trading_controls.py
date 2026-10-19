"""D1-D3 and E3/E6 backend: trading controls, manual close/order, active trades, signals, bars."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import signals, symbols, trades, trading
from app.core.execution.order_manager import CloseOutcome
from app.core.risk.manager import RiskRejectionError
from app.core.trading_state import TradingState
from app.dependencies import get_db, get_engine

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)


class FakeCache:
    def __init__(self, fail: bool = False):
        self.data: dict[str, str] = {}
        self.fail = fail

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


def trade_row(symbol="AAPL", status="OPEN"):
    return SimpleNamespace(
        id=uuid.uuid4(), strategy_id=None, symbol=symbol, direction="BUY",
        quantity=Decimal("10"), entry_price=Decimal("150"), stop_loss_price=Decimal("148"),
        take_profit_price=Decimal("154"), reward_to_risk_ratio=Decimal("2"), exit_price=None,
        exit_reason=None, status=status, risk_amount=Decimal("20"),
        account_balance_at_entry=Decimal("100000"), entry_fill_price=None, filled_quantity=None,
        current_stop_price=None, close_attempts=0, pnl=None, executed_at=NOW, closed_at=None,
        created_at=NOW,
    )


@pytest.fixture
def env(monkeypatch):
    cache = FakeCache()
    order_manager = SimpleNamespace(
        close_position=AsyncMock(return_value=CloseOutcome.CLOSED),
        submit_order=AsyncMock(),
    )
    engine = SimpleNamespace(
        trading_state=TradingState(cache),
        order_manager=order_manager,
        scheduler=SimpleNamespace(last_cycle_at=NOW),
        circuit_breaker=SimpleNamespace(is_halted=lambda: False),
        broker=SimpleNamespace(
            is_connected=lambda: True,
            get_account_summary=AsyncMock(
                return_value=SimpleNamespace(net_liquidation=Decimal("100000"))
            ),
        ),
        is_leader=True,
        market_open=lambda: True,
        in_eval_window=lambda: False,
        fresh_price=AsyncMock(return_value=Decimal("150")),
    )
    app = FastAPI()
    for module in (trading, trades, signals, symbols):
        app.include_router(module.router, prefix="/api/v1")
    db = AsyncMock()
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_db] = lambda: db
    return SimpleNamespace(
        client=TestClient(app), engine=engine, cache=cache, monkeypatch=monkeypatch, app=app
    )


async def test_redis_down_is_treated_as_paused():
    assert await TradingState(FakeCache(fail=True)).is_paused() is True


async def test_pause_resume_roundtrip():
    state = TradingState(FakeCache())
    assert await state.is_paused() is False
    await state.pause("test")
    assert await state.is_paused() is True
    await state.resume()
    assert await state.is_paused() is False


def test_status_shape(env):
    env.monkeypatch.setattr(trading.TradeRepo, "get_open_trades", AsyncMock(return_value=[trade_row()]))
    data = env.client.get("/api/v1/trading/status").json()["data"]
    assert data == {
        "paused": False, "market_open": True, "in_eval_window": False,
        "broker_connected": True, "engine_lock_held": True,
        "scheduler_last_cycle_at": NOW.isoformat(), "open_trade_count": 1,
        "circuit_breaker_halted": False,
    }


def test_pause_and_resume_endpoints(env):
    assert env.client.post("/api/v1/trading/pause", json={"reason": "x"}).json()["data"] == {"paused": True}
    assert env.cache.data["trading:paused"] == "1"
    assert env.client.post("/api/v1/trading/pause").status_code == 200
    assert env.client.post("/api/v1/trading/resume").json()["data"] == {"paused": False}
    assert "trading:paused" not in env.cache.data


def test_flatten_requires_confirm(env):
    assert env.client.post("/api/v1/trading/flatten", json={"confirm": "yes"}).status_code == 422
    assert env.client.post("/api/v1/trading/flatten", json={}).status_code == 422
    assert "trading:paused" not in env.cache.data
    env.engine.order_manager.close_position.assert_not_awaited()


def test_flatten_closes_all_and_leaves_paused(env):
    rows = [trade_row("AAPL"), trade_row("MSFT")]
    env.monkeypatch.setattr(trading.TradeRepo, "get_open_trades", AsyncMock(return_value=rows))
    env.engine.order_manager.close_position.side_effect = [
        CloseOutcome.CLOSED, CloseOutcome.PENDING_EXIT
    ]
    resp = env.client.post("/api/v1/trading/flatten", json={"confirm": "FLATTEN"})
    data = resp.json()["data"]
    assert [r["outcome"] for r in data["results"]] == ["CLOSED", "PENDING_EXIT"]
    assert env.cache.data["trading:paused"] == "1"
    calls = env.engine.order_manager.close_position.await_args_list
    assert all(c.args[1] == "MANUAL" for c in calls)


@pytest.mark.parametrize(
    "outcome,status",
    [
        (CloseOutcome.CLOSED, 200), (CloseOutcome.PENDING_EXIT, 202),
        (CloseOutcome.NOT_OPEN, 409), (CloseOutcome.MARKET_CLOSED, 409),
        (CloseOutcome.FAILED, 502),
    ],
)
def test_manual_close_maps_outcome(env, outcome, status):
    row = trade_row()
    env.monkeypatch.setattr(trades.TradeRepo, "get_by_id", AsyncMock(return_value=row))
    env.engine.order_manager.close_position.return_value = outcome
    resp = env.client.post(f"/api/v1/trades/{row.id}/close")
    assert resp.status_code == status
    env.engine.order_manager.close_position.assert_awaited_with(row.id, "MANUAL")
    if status < 400:
        assert resp.json()["data"]["symbol"] == "AAPL"
    else:
        assert resp.json()["error"]["code"] == outcome.value


def _manual(env, **over):
    body = {"symbol": "aapl", "stop_loss_price": "148"}
    body.update(over)
    return env.client.post("/api/v1/trades", json=body)


def test_manual_order_opens_trade_with_default_quantity(env):
    row = trade_row()
    env.monkeypatch.setattr(trades.TradeRepo, "get_by_id", AsyncMock(return_value=row))
    resp = _manual(env)
    assert resp.status_code == 201
    req = env.engine.order_manager.submit_order.await_args.args[0]
    assert req.symbol == "AAPL" and req.strategy_id is None
    assert req.entry_price == Decimal("150") and req.quantity == Decimal("500")


@pytest.mark.parametrize(
    "reason,code",
    [
        ("INVALID_STOP_LOSS: bad", "INVALID_STOP_LOSS"),
        ("RISK_LIMIT_EXCEEDED: too big", "RISK_LIMIT_EXCEEDED"),
        ("SYMBOL_ALREADY_ACTIVE: AAPL already has an active trade", "SYMBOL_ALREADY_ACTIVE"),
    ],
)
def test_manual_order_risk_rejection_is_422(env, reason, code):
    env.engine.order_manager.submit_order.side_effect = RiskRejectionError(reason, MagicMock())
    resp = _manual(env, quantity=1)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code


def test_manual_order_stop_above_entry_is_422(env):
    resp = _manual(env, stop_loss_price="155")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_STOP_LOSS"
    env.engine.order_manager.submit_order.assert_not_awaited()


def test_manual_order_market_closed_and_no_price_are_409(env):
    env.engine.market_open = lambda: False
    assert _manual(env).status_code == 409
    env.engine.market_open = lambda: True
    env.engine.fresh_price.return_value = None
    resp = _manual(env)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "NO_FRESH_PRICE"


def test_manual_order_broker_disconnected_is_409(env):
    env.engine.broker.is_connected = lambda: False
    assert _manual(env).status_code == 409


def test_manual_order_quantity_must_be_positive(env):
    assert _manual(env, quantity=0).status_code == 422


def test_active_trades_route_not_shadowed_by_id(env):
    env.monkeypatch.setattr(
        trades.TradeRepo, "get_active_trades",
        AsyncMock(return_value=[trade_row(status="CLOSING")]),
    )
    data = env.client.get("/api/v1/trades/active").json()["data"]
    assert data[0]["status"] == "CLOSING"


def test_signals_endpoint(env):
    sig = SimpleNamespace(
        id=uuid.uuid4(), strategy_id=uuid.uuid4(), symbol="AAPL", bar_date=date(2026, 10, 16),
        action="BUY", entry_price=Decimal("150"), stop_loss_price=Decimal("148"),
        take_profit_price=None, outcome="SUBMITTED", detail=None, trade_id=None, created_at=NOW,
    )
    lst = AsyncMock(return_value=[sig])
    env.monkeypatch.setattr(signals.SignalRepo, "list", lst)
    data = env.client.get("/api/v1/signals?symbol=aapl&limit=5").json()["data"]
    assert data[0]["outcome"] == "SUBMITTED" and data[0]["bar_date"] == "2026-10-16"
    assert lst.await_args.kwargs["symbol"] == "aapl" and lst.await_args.kwargs["limit"] == 5


def test_bars_endpoint(env):
    bar = SimpleNamespace(
        time=NOW, open=Decimal("1"), high=Decimal("2"), low=Decimal("0.5"),
        close=Decimal("1.5"), volume=1000,
    )
    get_bars = AsyncMock(return_value=[bar])
    env.monkeypatch.setattr(symbols.OHLCVRepo, "get_bars", get_bars)
    data = env.client.get("/api/v1/symbols/aapl/bars?days=30").json()["data"]
    assert data[0]["close"] == "1.5" and data[0]["volume"] == 1000
    assert get_bars.await_args.args[0] == "AAPL"
