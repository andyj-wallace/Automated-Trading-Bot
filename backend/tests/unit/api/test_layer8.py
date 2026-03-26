"""
Layer 8 checkpoint tests — REST API & WebSocket.

Tests the response envelope shape and key route behaviours for every
Layer 8 endpoint. Dependencies (DB session, broker, Redis) are replaced
with lightweight mocks via FastAPI's dependency_overrides mechanism.

Route-level repo calls are patched at the class level so the test does not
need a real database connection.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.brokers.base import BaseBroker
from app.data.cache import RedisCache
from app.dependencies import get_broker, get_cache, get_db
from app.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)


def _make_symbol(ticker: str = "AAPL") -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.ticker = ticker
    s.display_name = f"{ticker} Inc."
    s.is_active = True
    s.added_at = _NOW
    s.updated_at = _NOW
    return s


def _make_trade(symbol: str = "AAPL") -> MagicMock:
    t = MagicMock()
    t.id = uuid.uuid4()
    t.strategy_id = None
    t.symbol = symbol
    t.direction = "BUY"
    t.quantity = Decimal("10")
    t.entry_price = Decimal("150.00")
    t.stop_loss_price = Decimal("148.50")
    t.exit_price = None
    t.status = "OPEN"
    t.risk_amount = Decimal("15.00")
    t.account_balance_at_entry = Decimal("100000.00")
    t.pnl = None
    t.executed_at = _NOW
    t.closed_at = None
    t.created_at = _NOW
    return t


def _make_strategy(name: str = "MA Crossover") -> MagicMock:
    s = MagicMock()
    s.id = uuid.uuid4()
    s.name = name
    s.type = "moving_average"
    s.is_enabled = True
    s.config = {"fast_period": 50, "slow_period": 200, "symbols": ["AAPL"]}
    s.created_at = _NOW
    s.updated_at = _NOW
    return s


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_dependencies():
    """Replace all I/O dependencies with mocks for every test in this module."""
    mock_broker = MagicMock(spec=BaseBroker)
    mock_broker.is_connected.return_value = False
    mock_broker.validate_ticker = AsyncMock(return_value=True)
    mock_broker.get_positions = AsyncMock(return_value=[])
    mock_broker.get_account_summary = AsyncMock(
        return_value=MagicMock(net_liquidation=Decimal("100000"))
    )

    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()

    mock_cache = MagicMock(spec=RedisCache)
    mock_cache.ping = AsyncMock(return_value=True)

    app.dependency_overrides[get_broker] = lambda: mock_broker
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_cache] = lambda: mock_cache

    yield {"broker": mock_broker, "db": mock_db, "cache": mock_cache}

    app.dependency_overrides.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 8.1 — Response envelope format
# ---------------------------------------------------------------------------

class TestResponseEnvelope:
    def test_root_returns_ok_key(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_symbols_envelope_has_required_keys(self, client):
        with patch("app.api.v1.symbols.SymbolRepo") as MockRepo:
            MockRepo.return_value.get_all = AsyncMock(return_value=[])
            response = client.get("/api/v1/symbols")

        body = response.json()
        assert "data" in body
        assert "meta" in body
        assert "error" in body
        assert "timestamp" in body["meta"]
        assert "request_id" in body["meta"]

    def test_not_found_envelope_has_error_block(self, client):
        tid = uuid.uuid4()
        with patch("app.api.v1.trades.TradeRepo") as MockRepo:
            MockRepo.return_value.get_by_id = AsyncMock(return_value=None)
            response = client.get(f"/api/v1/trades/{tid}")

        assert response.status_code == 404
        body = response.json()
        assert body["data"] is None
        assert body["error"]["code"] == "NOT_FOUND"
        assert body["error"]["message"]


# ---------------------------------------------------------------------------
# 8.2 — Symbols endpoints
# ---------------------------------------------------------------------------

class TestSymbolsEndpoints:
    def test_list_symbols_returns_empty_list(self, client):
        with patch("app.api.v1.symbols.SymbolRepo") as MockRepo:
            MockRepo.return_value.get_all = AsyncMock(return_value=[])
            response = client.get("/api/v1/symbols")

        assert response.status_code == 200
        assert response.json()["data"] == []

    def test_list_symbols_returns_records(self, client):
        symbols = [_make_symbol("AAPL"), _make_symbol("MSFT")]
        with patch("app.api.v1.symbols.SymbolRepo") as MockRepo:
            MockRepo.return_value.get_all = AsyncMock(return_value=symbols)
            response = client.get("/api/v1/symbols")

        data = response.json()["data"]
        assert len(data) == 2
        tickers = {s["ticker"] for s in data}
        assert tickers == {"AAPL", "MSFT"}

    def test_post_symbol_creates_and_returns_201(self, client):
        new_symbol = _make_symbol("TSLA")
        with patch("app.api.v1.symbols.SymbolRepo") as MockRepo:
            MockRepo.return_value.create = AsyncMock(return_value=new_symbol)
            response = client.post(
                "/api/v1/symbols", json={"ticker": "tsla", "display_name": "Tesla"}
            )

        assert response.status_code == 201
        body = response.json()
        assert body["data"]["ticker"] == "TSLA"
        assert body["error"] is None

    def test_post_symbol_rejects_duplicate(self, client):
        with patch("app.api.v1.symbols.SymbolRepo") as MockRepo:
            MockRepo.return_value.create = AsyncMock(
                side_effect=ValueError("Symbol 'AAPL' is already on the watchlist")
            )
            response = client.post("/api/v1/symbols", json={"ticker": "AAPL"})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SYMBOL_EXISTS"

    def test_post_symbol_rejects_invalid_ticker_from_broker(
        self, client, mock_dependencies
    ):
        mock_dependencies["broker"].validate_ticker = AsyncMock(return_value=False)
        response = client.post("/api/v1/symbols", json={"ticker": "FAKEXYZ"})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_TICKER"

    def test_delete_symbol_returns_deleted_true(self, client):
        with patch("app.api.v1.symbols.SymbolRepo") as MockRepo:
            MockRepo.return_value.delete = AsyncMock(return_value=True)
            response = client.delete("/api/v1/symbols/AAPL")

        assert response.status_code == 200
        assert response.json()["data"]["deleted"] is True

    def test_delete_symbol_returns_404_when_not_found(self, client):
        with patch("app.api.v1.symbols.SymbolRepo") as MockRepo:
            MockRepo.return_value.delete = AsyncMock(return_value=False)
            response = client.delete("/api/v1/symbols/ZZZZZ")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_delete_symbol_with_open_position_requires_confirm(
        self, client, mock_dependencies
    ):
        mock_pos = MagicMock()
        mock_pos.symbol = "AAPL"
        mock_dependencies["broker"].is_connected.return_value = True
        mock_dependencies["broker"].get_positions = AsyncMock(return_value=[mock_pos])

        with patch("app.api.v1.symbols.SymbolRepo"):
            response = client.request(
                "DELETE", "/api/v1/symbols/AAPL", json={"confirm": False}
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "OPEN_POSITION"

    def test_delete_symbol_with_open_position_succeeds_with_confirm(
        self, client, mock_dependencies
    ):
        mock_pos = MagicMock()
        mock_pos.symbol = "AAPL"
        mock_dependencies["broker"].is_connected.return_value = True
        mock_dependencies["broker"].get_positions = AsyncMock(return_value=[mock_pos])

        with patch("app.api.v1.symbols.SymbolRepo") as MockRepo:
            MockRepo.return_value.delete = AsyncMock(return_value=True)
            response = client.request(
                "DELETE", "/api/v1/symbols/AAPL", json={"confirm": True}
            )

        assert response.status_code == 200
        assert response.json()["data"]["deleted"] is True


# ---------------------------------------------------------------------------
# 8.3 — Trades endpoints
# ---------------------------------------------------------------------------

class TestTradesEndpoints:
    def test_list_trades_empty(self, client):
        with patch("app.api.v1.trades.TradeRepo") as MockRepo:
            MockRepo.return_value.list = AsyncMock(return_value=[])
            response = client.get("/api/v1/trades")

        assert response.status_code == 200
        assert response.json()["data"] == []

    def test_list_trades_returns_records(self, client):
        trades = [_make_trade("AAPL"), _make_trade("MSFT")]
        with patch("app.api.v1.trades.TradeRepo") as MockRepo:
            MockRepo.return_value.list = AsyncMock(return_value=trades)
            response = client.get("/api/v1/trades")

        data = response.json()["data"]
        assert len(data) == 2

    def test_get_trade_by_id(self, client):
        trade = _make_trade("AAPL")
        with patch("app.api.v1.trades.TradeRepo") as MockRepo:
            MockRepo.return_value.get_by_id = AsyncMock(return_value=trade)
            response = client.get(f"/api/v1/trades/{trade.id}")

        assert response.status_code == 200
        assert response.json()["data"]["symbol"] == "AAPL"

    def test_get_trade_not_found(self, client):
        with patch("app.api.v1.trades.TradeRepo") as MockRepo:
            MockRepo.return_value.get_by_id = AsyncMock(return_value=None)
            response = client.get(f"/api/v1/trades/{uuid.uuid4()}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 8.4 — Strategies endpoints
# ---------------------------------------------------------------------------

class TestStrategiesEndpoints:
    def test_list_strategies_returns_records(self, client):
        strategies = [_make_strategy("MA"), _make_strategy("MR")]
        with patch("app.api.v1.strategies.StrategyRepo") as MockRepo:
            MockRepo.return_value.get_all = AsyncMock(return_value=strategies)
            response = client.get("/api/v1/strategies")

        data = response.json()["data"]
        assert len(data) == 2

    def test_patch_strategy_toggle_enabled(self, client):
        strategy = _make_strategy()
        strategy.is_enabled = False
        with patch("app.api.v1.strategies.StrategyRepo") as MockRepo:
            MockRepo.return_value.patch = AsyncMock(return_value=strategy)
            response = client.patch(
                f"/api/v1/strategies/{strategy.id}",
                json={"is_enabled": False},
            )

        assert response.status_code == 200
        assert response.json()["data"]["is_enabled"] is False

    def test_patch_strategy_update_config(self, client):
        strategy = _make_strategy()
        strategy.config = {"fast_period": 20, "slow_period": 50, "symbols": ["TSLA"]}
        with patch("app.api.v1.strategies.StrategyRepo") as MockRepo:
            MockRepo.return_value.patch = AsyncMock(return_value=strategy)
            response = client.patch(
                f"/api/v1/strategies/{strategy.id}",
                json={"config": {"fast_period": 20, "slow_period": 50, "symbols": ["TSLA"]}},
            )

        assert response.status_code == 200
        assert response.json()["data"]["config"]["symbols"] == ["TSLA"]

    def test_patch_strategy_empty_body_returns_422(self, client):
        response = client.patch(
            f"/api/v1/strategies/{uuid.uuid4()}",
            json={},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "NO_UPDATE"

    def test_patch_strategy_not_found(self, client):
        with patch("app.api.v1.strategies.StrategyRepo") as MockRepo:
            MockRepo.return_value.patch = AsyncMock(return_value=None)
            response = client.patch(
                f"/api/v1/strategies/{uuid.uuid4()}",
                json={"is_enabled": True},
            )

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# 8.5 — Portfolio risk endpoint
# ---------------------------------------------------------------------------

class TestPortfolioEndpoints:
    def test_risk_returns_exposure_fields(self, client):
        from app.core.risk.monitor import ExposureStatus

        mock_status = ExposureStatus(
            aggregate_risk_amount=Decimal("150"),
            aggregate_risk_pct=Decimal("0.0015"),
            open_trade_count=1,
            account_balance=Decimal("100000"),
            alert_level="NONE",
        )

        with patch("app.api.v1.portfolio.RiskMonitor") as MockMonitor:
            MockMonitor.return_value.check_exposure = AsyncMock(return_value=mock_status)
            response = client.get("/api/v1/portfolio/risk")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["open_trade_count"] == 1
        assert data["alert_level"] == "NONE"
        assert "aggregate_risk_amount" in data
        assert "warning_threshold_pct" in data


# ---------------------------------------------------------------------------
# 8.6 — System health endpoint
# ---------------------------------------------------------------------------

class TestSystemHealthEndpoint:
    def test_health_envelope_shape(self, client):
        """Health endpoint always returns a valid envelope with all component fields."""
        response = client.get("/api/v1/system/health")
        body = response.json()
        assert "data" in body
        assert "meta" in body
        data = body["data"]
        assert "status" in data
        assert "broker" in data
        assert "database" in data
        assert "redis" in data

    def test_health_broker_disconnected_returns_503(self, client, mock_dependencies):
        """503 when broker is not connected — DB/Redis mocks will either pass or fail."""
        mock_dependencies["broker"].is_connected.return_value = False
        response = client.get("/api/v1/system/health")
        # Broker disconnected → degraded → 503
        assert response.status_code == 503
        assert response.json()["data"]["broker"]["status"] == "disconnected"

    def test_health_component_statuses_are_valid_strings(self, client):
        """Each component status is one of the expected string values."""
        response = client.get("/api/v1/system/health")
        data = response.json()["data"]
        valid = {"ok", "disconnected", "error", "degraded"}
        assert data["broker"]["status"] in valid
        assert data["database"]["status"] in valid
        assert data["redis"]["status"] in valid
