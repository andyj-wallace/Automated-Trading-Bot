"""Growth-mode gates 5 and 6 on RiskManager, and the OrderManager account checks (I1)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.risk.costs import PLANS, CostModel
from app.core.risk.growth import GrowthPolicy
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest
from app.db.models.trade import TradeStatus

TIERED = CostModel(PLANS["ibkr_tiered"], D("5"))


def _req(symbol="SCHB", qty="2", balance="100") -> TradeRequest:
    return TradeRequest(
        trade_id=uuid.uuid4(), symbol=symbol, direction="BUY", quantity=D(qty),
        entry_price=D("25.00"), stop_loss_price=D("24.50"), take_profit_price=D("26.00"),
        account_balance=D(balance),
    )


def _growth(**kw) -> GrowthPolicy:
    args = {"enabled": True, "exit_equity": D("2000"), "max_open_positions": 2, "universe": frozenset({"SCHB"})}
    return GrowthPolicy(**{**args, **kw})


def test_fee_gate_blocks_small_trade_and_passes_larger_one():
    rm = RiskManager(cost_model=TIERED)
    with pytest.raises(RiskRejectionError, match="COSTS_TOO_HIGH"):
        rm.validate(_req())
    assert rm.validate(_req(qty="10", balance="500"))


def test_fee_gate_off_without_cost_model():
    assert RiskManager().validate(_req())


def test_growth_universe():
    rm = RiskManager(growth=_growth())
    with pytest.raises(RiskRejectionError, match="GROWTH_SYMBOL_NOT_ALLOWED"):
        rm.validate(_req(symbol="HUT"))
    assert rm.validate(_req(symbol="HUT", balance="5000", qty="10"))
    assert RiskManager(growth=_growth(enabled=False)).validate(_req(symbol="HUT"))


def _active(executed_at=None, status=TradeStatus.OPEN):
    return SimpleNamespace(
        symbol="SCHX", status=status, risk_amount=D("1"), quantity=D("2"), filled_quantity=None,
        entry_price=D("25"), entry_fill_price=None,
        executed_at=executed_at or datetime.now(UTC) - timedelta(hours=1),
    )


async def _om(account_type="cash", growth=None, active=None, **summary) -> tuple[OrderManager, MockBroker]:
    broker = MockBroker()
    await broker.connect()
    base = await broker.get_account_summary()
    broker.get_account_summary = AsyncMock(return_value=base.model_copy(update=summary))
    om = OrderManager(broker, RiskManager(), growth=growth, account_type=account_type, cost_model=None)
    om._load_active_trades = AsyncMock(return_value=active or [])
    return om, broker


def _order(qty="2") -> TradeRequest:
    return _req(qty=qty, balance="100")


async def test_growth_max_positions():
    om, _ = await _om(account_type=None, growth=_growth(max_open_positions=1), active=[_active()])
    with pytest.raises(RiskRejectionError, match="GROWTH_MAX_POSITIONS"):
        await om.submit_order(_order())


async def test_cash_account_needs_settled_cash():
    om, _ = await _om(settled_cash=D("40"))
    with pytest.raises(RiskRejectionError, match="INSUFFICIENT_SETTLED_CASH"):
        await om.submit_order(_order())
    om, _ = await _om(settled_cash=D("200"))
    await om.submit_order(_order())  # passes the account check


async def test_recent_fill_reserves_cash():
    recent = _active(executed_at=datetime.now(UTC) - timedelta(minutes=1))
    om, _ = await _om(settled_cash=D("90"), active=[recent])  # 90 - 50 reserved < 50 needed
    with pytest.raises(RiskRejectionError, match="INSUFFICIENT_SETTLED_CASH"):
        await om.submit_order(_order())


async def test_margin_account_day_trades():
    om, _ = await _om(account_type="margin", day_trades_remaining=0)
    with pytest.raises(RiskRejectionError, match="NO_DAY_TRADES_LEFT"):
        await om.submit_order(_order())
    om, _ = await _om(account_type="margin", day_trades_remaining=-1)
    await om.submit_order(_order())


async def test_unreadable_account_rejects_entry():
    om, broker = await _om()
    broker.get_account_summary = AsyncMock(side_effect=RuntimeError("down"))
    with pytest.raises(RiskRejectionError, match="ACCOUNT_UNAVAILABLE"):
        await om.submit_order(_order())
