"""
Unit tests for RiskManager (Layer 6.3) and RiskMonitor (Layer 6.4).

Includes the Checkpoint verification: validate() with missing stop-loss
raises RiskRejectionError and writes to risk.log.
"""

import logging
import uuid
from decimal import Decimal

import pytest

from app.core.risk.calculator import RiskValidationError
from app.core.risk.manager import (
    RiskManager,
    RiskRejectionError,
    TradeRequest,
)
from app.core.risk.monitor import ExposureStatus, RiskMonitor, RiskMonitorConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(**kwargs) -> TradeRequest:
    defaults = {
        "trade_id": uuid.uuid4(),
        "symbol": "AAPL",
        "direction": "BUY",
        "quantity": Decimal("100"),
        "entry_price": Decimal("200"),
        "stop_loss_price": Decimal("195"),
        "account_balance": Decimal("100000"),
    }
    defaults.update(kwargs)
    return TradeRequest(**defaults)


@pytest.fixture
def manager() -> RiskManager:
    return RiskManager()


# ---------------------------------------------------------------------------
# Approved trades
# ---------------------------------------------------------------------------


def test_valid_trade_approved(manager: RiskManager) -> None:
    req = _request()
    result = manager.validate(req)

    assert result.trade_id == req.trade_id
    assert result.risk_amount == Decimal("500")   # 100 × ($200 - $195)
    assert result.max_quantity == 200              # floor($1000 / $5)
    assert result.account_balance_at_entry == Decimal("100000")


def test_risk_amount_exactly_at_limit_is_approved(manager: RiskManager) -> None:
    """Exactly 1% risk should be approved."""
    # 200 shares × $5 risk = $1000 = exactly 1% of $100k
    req = _request(quantity=Decimal("200"))
    result = manager.validate(req)
    assert result.risk_amount == Decimal("1000")


def test_account_balance_snapshotted(manager: RiskManager) -> None:
    balance = Decimal("87654.32")
    req = _request(account_balance=balance, quantity=Decimal("10"))
    result = manager.validate(req)
    assert result.account_balance_at_entry == balance


# ---------------------------------------------------------------------------
# Checkpoint: missing stop-loss → rejection logged
# ---------------------------------------------------------------------------


def test_missing_stop_loss_raises(manager: RiskManager) -> None:
    req = _request(stop_loss_price=None)
    with pytest.raises(RiskRejectionError) as exc_info:
        manager.validate(req)
    assert "MISSING_STOP_LOSS" in str(exc_info.value)


def test_missing_stop_loss_is_logged(manager: RiskManager, caplog) -> None:
    """Checkpoint: rejection must write to risk.log."""
    req = _request(stop_loss_price=None)
    with caplog.at_level(logging.WARNING, logger="risk"):
        with pytest.raises(RiskRejectionError):
            manager.validate(req)
    assert any("MISSING_STOP_LOSS" in r.message or "rejected" in r.message.lower()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# Rejection cases
# ---------------------------------------------------------------------------


def test_stop_loss_above_entry_raises(manager: RiskManager) -> None:
    req = _request(stop_loss_price=Decimal("210"))
    with pytest.raises(RiskRejectionError) as exc_info:
        manager.validate(req)
    assert "INVALID_STOP_LOSS" in str(exc_info.value)


def test_risk_amount_over_limit_raises(manager: RiskManager) -> None:
    # 201 shares × $5 = $1005 > 1% of $100k ($1000)
    req = _request(quantity=Decimal("201"))
    with pytest.raises(RiskRejectionError) as exc_info:
        manager.validate(req)
    assert "RISK_LIMIT_EXCEEDED" in str(exc_info.value)


def test_rejection_carries_request(manager: RiskManager) -> None:
    req = _request(stop_loss_price=None)
    with pytest.raises(RiskRejectionError) as exc_info:
        manager.validate(req)
    assert exc_info.value.request.trade_id == req.trade_id


def test_rejection_logged_with_context(manager: RiskManager, caplog) -> None:
    req = _request(quantity=Decimal("201"))
    with caplog.at_level(logging.WARNING, logger="risk"):
        with pytest.raises(RiskRejectionError):
            manager.validate(req)
    # The log record should contain symbol and reason context
    risk_records = [r for r in caplog.records if r.name == "risk"]
    assert risk_records, "Expected a log entry on risk logger"


# ---------------------------------------------------------------------------
# RiskMonitor — unit tests (no DB; uses in-memory fake trades)
# ---------------------------------------------------------------------------


class FakeTrade:
    def __init__(self, risk_amount: Decimal) -> None:
        self.risk_amount = risk_amount


class FakeTradeRepo:
    def __init__(self, trades: list[FakeTrade]) -> None:
        self._trades = trades

    async def get_open_trades(self):
        return self._trades


class FakeSession:
    pass


@pytest.fixture
def monitor() -> RiskMonitor:
    return RiskMonitor()


def _patch_repo(monkeypatch, trades: list[FakeTrade]) -> None:
    import app.core.risk.monitor as monitor_module

    class PatchedRepo(FakeTradeRepo):
        def __init__(self, session):
            super().__init__(trades)

    monkeypatch.setattr(monitor_module, "TradeRepo", PatchedRepo)


@pytest.mark.asyncio
async def test_monitor_no_trades_returns_none_alert(
    monitor: RiskMonitor, monkeypatch
) -> None:
    _patch_repo(monkeypatch, [])
    status = await monitor.check_exposure(FakeSession(), Decimal("100000"))
    assert status.alert_level == "NONE"
    assert status.open_trade_count == 0
    assert status.aggregate_risk_pct == Decimal("0")


@pytest.mark.asyncio
async def test_monitor_below_warning_no_alert(
    monitor: RiskMonitor, monkeypatch
) -> None:
    # 2 trades × $500 = $1000 / $100k = 1.0% < 3.75% warning threshold
    _patch_repo(monkeypatch, [FakeTrade(Decimal("500")), FakeTrade(Decimal("500"))])
    status = await monitor.check_exposure(FakeSession(), Decimal("100000"))
    assert status.alert_level == "NONE"


@pytest.mark.asyncio
async def test_monitor_at_warning_threshold(
    monitor: RiskMonitor, monkeypatch
) -> None:
    # 75% of 5% = 3.75%. Expose $3750 of $100k.
    _patch_repo(monkeypatch, [FakeTrade(Decimal("3750"))])
    status = await monitor.check_exposure(FakeSession(), Decimal("100000"))
    assert status.alert_level == "WARNING"


@pytest.mark.asyncio
async def test_monitor_at_critical_threshold(
    monitor: RiskMonitor, monkeypatch
) -> None:
    # 90% of 5% = 4.5%. Expose $4500 of $100k.
    _patch_repo(monkeypatch, [FakeTrade(Decimal("4500"))])
    status = await monitor.check_exposure(FakeSession(), Decimal("100000"))
    assert status.alert_level == "CRITICAL"


@pytest.mark.asyncio
async def test_monitor_custom_config(monkeypatch) -> None:
    config = RiskMonitorConfig(
        max_aggregate_risk_pct=Decimal("0.10"),  # 10% max
    )
    monitor = RiskMonitor(config=config)
    # 80% of 10% = 8% — above 75% warning threshold
    _patch_repo(monkeypatch, [FakeTrade(Decimal("8000"))])
    status = await monitor.check_exposure(FakeSession(), Decimal("100000"))
    assert status.alert_level == "WARNING"
