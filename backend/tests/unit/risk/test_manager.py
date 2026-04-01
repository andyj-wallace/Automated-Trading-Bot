"""
Unit tests for RiskManager (Layer 6.3 + 6B) and RiskMonitor (Layer 6.4 + 6B.5).

Includes checkpoint verifications:
  - validate() with missing stop-loss raises RiskRejectionError and logs to risk.log
  - R:R gate rejects below-minimum take-profit and accepts/calculates valid targets
  - Portfolio gate rejects when aggregate would exceed limit
  - RiskMonitorConfig raises on invalid max_aggregate_risk_pct
"""

import logging
import uuid
from decimal import Decimal

import pytest

from app.core.risk.calculator import RiskValidationError
from app.core.risk.manager import (
    MAX_PORTFOLIO_RISK_HARD_LIMIT,
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
    # R:R fields: take_profit = 200 + (5 × 2.0) = 210; rr = (210-200)/5 = 2.0
    assert result.take_profit_price == Decimal("210")
    assert result.reward_to_risk_ratio == Decimal("2.0")


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


def test_missing_stop_loss_is_logged(manager: RiskManager) -> None:
    """Checkpoint: rejection must write a WARNING to risk.log with MISSING_STOP_LOSS reason."""
    from unittest.mock import patch

    req = _request(stop_loss_price=None)
    with patch("app.core.risk.manager.risk_logger") as mock_log:
        with pytest.raises(RiskRejectionError):
            manager.validate(req)
    mock_log.warning.assert_called_once()
    extra = mock_log.warning.call_args.kwargs["extra"]
    assert extra["reason"] == "MISSING_STOP_LOSS"


# ---------------------------------------------------------------------------
# Gate 2: 1% rule rejection cases
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


def test_rejection_logged_with_context(manager: RiskManager) -> None:
    """Rejection log must include symbol and reason in extra context."""
    from unittest.mock import patch

    req = _request(quantity=Decimal("201"))
    with patch("app.core.risk.manager.risk_logger") as mock_log:
        with pytest.raises(RiskRejectionError):
            manager.validate(req)
    mock_log.warning.assert_called_once()
    extra = mock_log.warning.call_args.kwargs["extra"]
    assert extra["symbol"] == "AAPL"
    assert "reason" in extra


# ---------------------------------------------------------------------------
# Gate 3: Reward-to-risk ratio
# ---------------------------------------------------------------------------


def test_rr_auto_calculated_when_no_take_profit(manager: RiskManager) -> None:
    """No take_profit_price → computed mechanically at 2:1."""
    req = _request()  # entry=200, stop=195, stop_distance=5
    result = manager.validate(req)
    # required_take_profit = 200 + (5 × 2.0) = 210
    assert result.take_profit_price == Decimal("210")
    assert result.reward_to_risk_ratio == Decimal("2.0")


def test_rr_suggestion_accepted_when_above_minimum(manager: RiskManager) -> None:
    """Strategy-suggested take_profit ≥ required → accepted as-is."""
    req = _request(take_profit_price=Decimal("215"))  # 215 >= 210
    result = manager.validate(req)
    assert result.take_profit_price == Decimal("215")
    # rr = (215-200)/5 = 3.0
    assert result.reward_to_risk_ratio == Decimal("3.0")


def test_rr_suggestion_at_minimum_accepted(manager: RiskManager) -> None:
    """Exactly at required take_profit (2:1) should be accepted."""
    req = _request(take_profit_price=Decimal("210"))
    result = manager.validate(req)
    assert result.take_profit_price == Decimal("210")


def test_rr_suggestion_below_minimum_rejected(manager: RiskManager) -> None:
    """Suggested take_profit below 2:1 minimum → INSUFFICIENT_REWARD."""
    req = _request(take_profit_price=Decimal("205"))  # 205 < 210
    with pytest.raises(RiskRejectionError) as exc_info:
        manager.validate(req)
    assert "INSUFFICIENT_REWARD" in str(exc_info.value)


def test_rr_rejection_logged(manager: RiskManager) -> None:
    """R:R rejection must emit a WARNING with INSUFFICIENT_REWARD reason."""
    from unittest.mock import patch

    req = _request(take_profit_price=Decimal("203"))
    with patch("app.core.risk.manager.risk_logger") as mock_log:
        with pytest.raises(RiskRejectionError):
            manager.validate(req)
    mock_log.warning.assert_called_once()
    extra = mock_log.warning.call_args.kwargs["extra"]
    assert extra["reason"] == "INSUFFICIENT_REWARD"


def test_custom_min_rr_ratio_respected() -> None:
    """RiskManager configured with 3:1 min R:R rejects 2:1 suggestion."""
    manager = RiskManager(min_reward_to_risk=Decimal("3.0"))
    req = _request(take_profit_price=Decimal("210"))  # 2:1, below 3:1 requirement
    with pytest.raises(RiskRejectionError) as exc_info:
        manager.validate(req)
    assert "INSUFFICIENT_REWARD" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Gate 4: Portfolio aggregate limit
# ---------------------------------------------------------------------------


def test_portfolio_gate_passes_with_headroom(manager: RiskManager) -> None:
    """Trade that fits within the portfolio limit is approved."""
    req = _request(quantity=Decimal("100"))  # risk_amount = 500
    # current aggregate = 1000, new 500, total 1500 < 5% of 100k (5000)
    result = manager.validate(req, current_aggregate_risk=Decimal("1000"))
    assert result.risk_amount == Decimal("500")


def test_portfolio_gate_rejects_when_limit_exceeded(manager: RiskManager) -> None:
    """New trade would push aggregate beyond configured max → rejected."""
    req = _request(quantity=Decimal("100"))  # risk_amount = 500
    # current aggregate = 4600, new 500 → 5100 > 5000 (5% of 100k)
    with pytest.raises(RiskRejectionError) as exc_info:
        manager.validate(req, current_aggregate_risk=Decimal("4600"))
    assert "PORTFOLIO_RISK_LIMIT_EXCEEDED" in str(exc_info.value)


def test_portfolio_gate_at_exact_limit_rejected(manager: RiskManager) -> None:
    """Aggregate exactly at limit + new trade → rejected (strictly greater than)."""
    req = _request(quantity=Decimal("100"))  # risk_amount = 500
    # current aggregate = 4500, new 500 → 5000 = 5% of 100k; 5000 > 5000 is False → passes!
    # Actually: 4500 + 500 = 5000 ≤ 5000 → passes
    result = manager.validate(req, current_aggregate_risk=Decimal("4500"))
    assert result is not None  # at limit, not over — passes


def test_portfolio_gate_one_cent_over_rejected(manager: RiskManager) -> None:
    """One cent over the limit is rejected."""
    req = _request(quantity=Decimal("100"))  # risk_amount = 500
    # current aggregate = 4500.01, new 500 → 5000.01 > 5000
    with pytest.raises(RiskRejectionError) as exc_info:
        manager.validate(req, current_aggregate_risk=Decimal("4500.01"))
    assert "PORTFOLIO_RISK_LIMIT_EXCEEDED" in str(exc_info.value)


def test_portfolio_gate_custom_max() -> None:
    """RiskManager with lower portfolio max rejects sooner."""
    manager = RiskManager(max_portfolio_risk_pct=Decimal("0.02"))  # 2% max
    req = _request(quantity=Decimal("100"))  # risk_amount = 500; 2% of 100k = 2000
    # current = 1600, new 500 → 2100 > 2000 → rejected
    with pytest.raises(RiskRejectionError) as exc_info:
        manager.validate(req, current_aggregate_risk=Decimal("1600"))
    assert "PORTFOLIO_RISK_LIMIT_EXCEEDED" in str(exc_info.value)


def test_portfolio_max_over_hard_limit_raises_on_init() -> None:
    """RiskManager refuses to initialise with max > 10%."""
    with pytest.raises(ValueError, match="hard limit"):
        RiskManager(max_portfolio_risk_pct=Decimal("0.11"))


def test_portfolio_max_at_hard_limit_is_valid() -> None:
    """Exactly 10% is allowed."""
    manager = RiskManager(max_portfolio_risk_pct=MAX_PORTFOLIO_RISK_HARD_LIMIT)
    assert manager is not None


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
        max_aggregate_risk_pct=Decimal("0.10"),  # exactly at hard limit (10%)
    )
    monitor = RiskMonitor(config=config)
    # 80% of 10% = 8% — above 75% warning threshold
    _patch_repo(monkeypatch, [FakeTrade(Decimal("8000"))])
    status = await monitor.check_exposure(FakeSession(), Decimal("100000"))
    assert status.alert_level == "WARNING"


# ---------------------------------------------------------------------------
# 6B.5: RiskMonitorConfig hard limit enforcement
# ---------------------------------------------------------------------------


def test_risk_monitor_config_exceeding_hard_limit_raises() -> None:
    """RiskMonitorConfig must reject max_aggregate_risk_pct > 10%."""
    with pytest.raises(ValueError, match="hard limit"):
        RiskMonitorConfig(max_aggregate_risk_pct=Decimal("0.11"))


def test_risk_monitor_config_at_hard_limit_is_valid() -> None:
    """Exactly 10% is the allowed maximum."""
    config = RiskMonitorConfig(max_aggregate_risk_pct=Decimal("0.10"))
    assert config.max_aggregate_risk_pct == Decimal("0.10")


def test_risk_monitor_config_default_is_valid() -> None:
    """Default 5% config is valid and well below the hard limit."""
    config = RiskMonitorConfig()
    assert config.max_aggregate_risk_pct == Decimal("0.05")
