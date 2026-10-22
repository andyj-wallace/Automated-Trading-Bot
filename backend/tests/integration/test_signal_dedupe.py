"""B7 integration: signal claim dedupe and one-active-trade-per-symbol."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest
from app.db.models.trade import TradeStatus
from app.db.repositories.signal_repo import SignalRepo
from app.db.repositories.strategy_repo import StrategyRepo
from app.db.repositories.trade_repo import TradeRepo


@pytest_asyncio.fixture
async def db_engine():
    eng = create_async_engine(os.environ["DATABASE_URL"])
    async with eng.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE trades, trading_strategies RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(db_engine):
    factory = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _factory():
        async with factory() as sess:
            yield sess

    return _factory


async def test_second_claim_with_same_key_returns_none(session_factory):
    async with session_factory() as s:
        strategy = await StrategyRepo(s).create(name="dedupe", type="moving_average")
        await s.commit()
    key = dict(strategy_id=strategy.id, symbol="aapl", bar_date=date(2026, 10, 16), action="BUY")
    async with session_factory() as s:
        first = await SignalRepo(s).claim(**key)
        await s.commit()
    async with session_factory() as s:
        second = await SignalRepo(s).claim(**key)
        await s.commit()
        other_day = await SignalRepo(s).claim(**{**key, "bar_date": date(2026, 10, 17)})
        await s.commit()
    assert first is not None and second is None and other_day is not None

    async with session_factory() as s:
        repo = SignalRepo(s)
        await repo.set_outcome(first, "RISK_REJECTED", detail="x")
        await s.commit()
        rows = await repo.list(symbol="AAPL")
    assert {r.outcome for r in rows} == {"RISK_REJECTED", "CLAIMED"}


async def test_order_manager_rejects_second_trade_for_same_symbol(session_factory):
    broker = MockBroker()
    await broker.connect()
    manager = OrderManager(
        broker=broker,
        risk_manager=RiskManager(),
        session_factory=session_factory,
        order_retry_attempts=1,
        order_retry_backoff_seconds=0,
    )

    def request(symbol: str) -> TradeRequest:
        return TradeRequest(
            trade_id=uuid.uuid4(),
            symbol=symbol,
            direction="BUY",
            quantity=Decimal("10"),
            entry_price=Decimal("200"),
            stop_loss_price=Decimal("195"),
            account_balance=Decimal("100000"),
        )

    first = request("AAPL")
    await manager.submit_order(first)
    with pytest.raises(RiskRejectionError) as exc:
        await manager.submit_order(request("AAPL"))
    assert exc.value.reason.startswith("SYMBOL_ALREADY_ACTIVE")
    await manager.submit_order(request("MSFT"))

    async with session_factory() as s:
        repo = TradeRepo(s)
        assert await repo.has_active_trade("aapl")
        assert not await repo.has_active_trade("TSLA")
        await repo.update_status(first.trade_id, TradeStatus.CLOSING)
        await s.commit()
        assert await repo.has_active_trade("AAPL")
    await broker.disconnect()


async def test_order_manager_never_trades_on_margin(session_factory):
    broker = MockBroker()
    await broker.connect()
    manager = OrderManager(
        broker=broker,
        risk_manager=RiskManager(),
        session_factory=session_factory,
        order_retry_attempts=1,
        order_retry_backoff_seconds=0,
    )

    def request(symbol: str, quantity: str) -> TradeRequest:
        # $3 stop: each 300-share trade risks $900 (under 1%) but is worth $60,000.
        return TradeRequest(
            trade_id=uuid.uuid4(),
            symbol=symbol,
            direction="BUY",
            quantity=Decimal(quantity),
            entry_price=Decimal("200"),
            stop_loss_price=Decimal("197"),
            account_balance=Decimal("100000"),
        )

    await broker.simulate_price_tick("AAPL", Decimal("200"))  # mock fills at the last simulated price
    await manager.submit_order(request("AAPL", "300"))
    with pytest.raises(RiskRejectionError) as exc:
        await manager.submit_order(request("MSFT", "300"))  # $120,000 total on a $100,000 account
    assert exc.value.reason.startswith("NOTIONAL_LIMIT_EXCEEDED")
    await manager.submit_order(request("MSFT", "200"))  # $100,000 total: allowed
    await broker.disconnect()
