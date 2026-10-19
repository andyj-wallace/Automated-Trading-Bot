"""C9: the portfolio cap, the risk monitor and the metrics snapshot share one definition of active risk."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.mock.client import MockBroker
from app.core.metrics.collector import MetricsCollector
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest
from app.core.risk.monitor import RiskMonitor
from app.db.models.trade import ACTIVE_RISK_STATUSES, TradeDirection, TradeStatus
from app.db.repositories.trade_repo import TradeRepo


@pytest_asyncio.fixture
async def db_engine():
    eng = create_async_engine(os.environ["DATABASE_URL"])
    async with eng.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE trades RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    factory = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _factory():
        async with factory() as sess:
            yield sess

    return _factory


async def _insert(session, symbol: str, status: TradeStatus, risk: str = "1000") -> None:
    repo = TradeRepo(session)
    tid = uuid.uuid4()
    await repo.create(
        trade_id=tid, symbol=symbol, direction=TradeDirection.BUY, quantity=Decimal("10"),
        entry_price=Decimal("200"), stop_loss_price=Decimal("100"),
        take_profit_price=Decimal("400"), reward_to_risk_ratio=Decimal("2"),
        risk_amount=Decimal(risk), account_balance_at_entry=Decimal("100000"),
    )
    await repo.update_status(tid, status)


async def test_closing_trade_counts_for_monitor_and_snapshot(session_factory):
    assert TradeStatus.CLOSING in ACTIVE_RISK_STATUSES
    async with session_factory() as s:
        await _insert(s, "AAA", TradeStatus.OPEN)
        await _insert(s, "BBB", TradeStatus.CLOSING)
        await _insert(s, "CCC", TradeStatus.CLOSED)
        await s.commit()

    async with session_factory() as s:
        status = await RiskMonitor().check_exposure(s, Decimal("100000"))
        assert status.open_trade_count == 2
        assert status.aggregate_risk_amount == Decimal("2000")

    broker = MockBroker()
    await broker.connect()
    async with session_factory() as s:
        snapshot = await MetricsCollector().record_snapshot(s, broker)
    assert snapshot is not None
    assert snapshot.open_trade_count == 2
    assert snapshot.aggregate_risk_amount == Decimal("2000")


async def test_closing_trade_counts_toward_the_portfolio_cap(session_factory):
    from app.core.execution.order_manager import OrderManager

    async with session_factory() as s:
        for symbol in ("AAA", "BBB", "CCC", "DDD"):
            await _insert(s, symbol, TradeStatus.CLOSING, risk="1000")  # 4% of 100k
        await s.commit()

    broker = MockBroker()
    await broker.connect()
    manager = OrderManager(broker, RiskManager(), session_factory, is_market_open=lambda: True)
    request = TradeRequest(
        trade_id=uuid.uuid4(), symbol="EEE", direction="BUY", quantity=Decimal("200"),
        entry_price=Decimal("200"), stop_loss_price=Decimal("195"),
        account_balance=Decimal("100000"),
    )  # 1% risk: 4% + 1% = 5% is allowed, 4% + 1.01% is not
    await manager.submit_order(request)  # exactly at the cap: fine

    over = request.model_copy(update={"trade_id": uuid.uuid4(), "symbol": "FFF"})
    try:
        await manager.submit_order(over)
        raise AssertionError("expected the 5% portfolio cap to reject the trade")
    except RiskRejectionError:
        pass
