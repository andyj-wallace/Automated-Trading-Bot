"""
B9: end-to-end engine wiring. A real TradingEngine (MockBroker, test DB, test Redis)
must close a stopped-out trade from a broker tick and open exactly one trade from a signal.
Any change that disconnects the live loop fails here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import text

from app.brokers.base import PriceBar
from app.config import Settings
from app.core.market_calendar import ET
from app.core.risk.manager import TradeRequest
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal
from app.core.strategy_engine.registry import registry
from app.data.cache import RedisCache
from app.db.models.trade import ExitReason, TradeStatus
from app.db.repositories.ohlcv_repo import OHLCVRepo
from app.db.repositories.signal_repo import SignalRepo
from app.db.repositories.strategy_repo import StrategyRepo
from app.db.repositories.symbol_repo import SymbolRepo
from app.db.repositories.trade_repo import TradeRepo
from app.dependencies import _build_broker, _build_cache, _build_circuit_breaker

FIXED_NOW = datetime(2026, 10, 19, 11, 0, tzinfo=ET)  # Monday, inside the 09:45-15:45 window


async def _poll(predicate, timeout: float = 8.0, interval: float = 0.1):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    return None


@pytest_asyncio.fixture
async def trading_engine():
    from app.core.engine import TradingEngine
    from app.db.session import AsyncSessionFactory
    from app.db.session import engine as db_engine

    for builder in (_build_broker, _build_cache, _build_circuit_breaker):
        builder.cache_clear()
    async with db_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE trades, trading_strategies RESTART IDENTITY CASCADE"))

    settings = Settings()
    settings.scheduler_interval_seconds = 3600
    eng = TradingEngine(
        settings,
        clock=lambda: FIXED_NOW,
        is_market_open=lambda: True,
        feed_sync_interval=0.1,
        bar_refresh_interval=3600,
        monitor_resync_interval=0.1,
    )
    eng.bar_refresh._startup_done = True
    eng.bar_refresh._last_run = FIXED_NOW.date()
    await eng.start()
    assert eng.started
    yield eng, AsyncSessionFactory
    await eng.stop()
    await db_engine.dispose()
    for builder in (_build_broker, _build_cache, _build_circuit_breaker):
        builder.cache_clear()


async def test_stop_loss_closes_trade_from_broker_tick(trading_engine):
    eng, session_factory = trading_engine
    broker = eng.broker
    listener_cache = RedisCache(os.environ["REDIS_URL"])
    received: list[dict] = []

    async def _listen() -> None:
        async for raw in listener_cache.subscribe("watchlist_prices"):
            received.append(json.loads(raw))

    async with session_factory() as s:
        repo = SymbolRepo(s)
        added_tsla = await repo.get_by_ticker("TSLA") is None
        if added_tsla:
            await repo.create("TSLA")
        await s.commit()

    listener = asyncio.create_task(_listen())
    await asyncio.sleep(0.3)
    try:
        await broker.simulate_price_tick("TSLA", Decimal("200"))
        request = TradeRequest(
            trade_id=uuid.uuid4(),
            symbol="TSLA",
            direction="BUY",
            quantity=Decimal("10"),
            entry_price=Decimal("200"),
            stop_loss_price=Decimal("195"),
            account_balance=Decimal("100000"),
        )
        await eng.order_manager.submit_order(request)

        async def _wired() -> bool:
            return "TSLA" in eng.feed.subscribed_tickers and request.trade_id in eng.position_monitor._tracked

        assert await _poll(_wired), "feed never subscribed to TSLA / monitor never tracked the trade"

        await broker.simulate_price_tick("TSLA", Decimal("194"))

        async def _closed():
            async with session_factory() as s:
                trade = await TradeRepo(s).get_by_id(request.trade_id)
                return trade if trade and trade.status == TradeStatus.CLOSED else None

        trade = await _poll(_closed)
        assert trade is not None, "trade was not closed by the monitor"
        assert trade.exit_reason == ExitReason.STOP_LOSS
        assert "TSLA" not in broker.positions

        raw_price = await eng.cache.get("price:TSLA")
        assert raw_price is not None and json.loads(raw_price)["price"] == "194"
        await asyncio.sleep(0.3)
        assert any(
            m["event"] == "price_update" and m["payload"]["symbol"] == "TSLA" for m in received
        )
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        await listener_cache.close()
        if added_tsla:
            async with session_factory() as s:
                await SymbolRepo(s).delete("TSLA")
                await s.commit()


class _AlwaysBuy(BaseStrategy):
    def __init__(self, config: dict) -> None:
        self.config = config

    async def generate_signal(self, market_data: MarketData) -> Signal:
        price = market_data.current_price
        return Signal(
            symbol=market_data.symbol,
            action="BUY",
            entry_price=price,
            stop_loss_price=price - Decimal("10"),
            timestamp=datetime.now(UTC),
        )

    async def calculate_position_size(self, risk_params: RiskParams) -> int:
        return 10

    @classmethod
    def get_config_schema(cls) -> dict:
        return {"type": "object", "properties": {}}


async def test_signal_opens_exactly_one_trade(trading_engine):
    eng, session_factory = trading_engine
    registry.register("test_always_buy", _AlwaysBuy)
    try:
        yesterday = date(2026, 10, 16)
        bars = [
            PriceBar(
                timestamp=datetime.combine(yesterday - timedelta(days=i), datetime.min.time(), UTC),
                open=Decimal("199"), high=Decimal("201"), low=Decimal("198"),
                close=Decimal("200"), volume=1000,
            )
            for i in range(5)
        ]
        async with session_factory() as s:
            await OHLCVRepo(s).delete_symbol("NVDA")
            await OHLCVRepo(s).upsert_bars("NVDA", bars)
            strategy = await StrategyRepo(s).create(
                name="always-buy", type="test_always_buy",
                config={"symbols": ["NVDA"]}, is_enabled=True,
            )
            await s.commit()

        await eng.broker.simulate_price_tick("NVDA", Decimal("200"))
        await eng.cache.set(
            "price:NVDA",
            json.dumps({"price": "200", "timestamp": datetime.now(UTC).isoformat()}),
        )

        for _ in range(3):
            await eng.scheduler._run_cycle()

        async with session_factory() as s:
            trades = [t for t in await TradeRepo(s).list() if t.symbol == "NVDA"]
            signals = await SignalRepo(s).list(symbol="NVDA")
        assert len(trades) == 1
        assert trades[0].status == TradeStatus.OPEN
        assert trades[0].strategy_id == strategy.id
        assert len(signals) == 1
        assert signals[0].outcome == "SUBMITTED"
        assert signals[0].trade_id == trades[0].id
    finally:
        registry._classes.pop("test_always_buy", None)


async def test_auto_kill_and_halt_are_durable_and_alerted(trading_engine):
    eng, session_factory = trading_engine
    eng.circuit_breaker._max_consecutive_losses = 1
    eng.circuit_breaker._daily_loss_limit_pct = Decimal("0.0005")
    published: list[dict] = []
    real_publish = eng.cache.publish

    async def spy(channel, message):
        published.append(json.loads(message))
        await real_publish(channel, message)

    eng.cache.publish = spy
    async with session_factory() as s:
        strategy = await StrategyRepo(s).create(
            name="loser", type="test_always_buy", config={"symbols": ["AAPL"]}, is_enabled=True
        )
        await s.commit()

    eng.broker.last_prices["AAPL"] = Decimal("200")
    request = TradeRequest(
        trade_id=uuid.uuid4(), symbol="AAPL", direction="BUY", quantity=Decimal("10"),
        entry_price=Decimal("200"), stop_loss_price=Decimal("195"),
        account_balance=Decimal("100000"), strategy_id=strategy.id,
    )
    await eng.order_manager.submit_order(request)
    eng.broker.last_prices["AAPL"] = Decimal("190")
    await eng.order_manager.close_position(request.trade_id, "MANUAL")

    async with session_factory() as s:
        refreshed = await StrategyRepo(s).get_by_id(strategy.id)
    assert refreshed.is_enabled is False
    assert eng.circuit_breaker.is_strategy_killed(strategy.id)
    alert_types = {m["payload"]["alert_type"] for m in published if m["event"] == "system_alert"}
    assert {"CIRCUIT_BREAKER_HALT", "STRATEGY_AUTO_DISABLED"} <= alert_types


async def test_restart_rebuilds_todays_halt_from_the_database(trading_engine):
    from app.core.risk.circuit_breaker import CircuitBreaker
    from app.db.models.trade import TradeDirection

    eng, session_factory = trading_engine
    async with session_factory() as s:
        repo = TradeRepo(s)
        tid = uuid.uuid4()
        await repo.create(
            trade_id=tid, symbol="AAPL", direction=TradeDirection.BUY, quantity=Decimal("10"),
            entry_price=Decimal("200"), stop_loss_price=Decimal("195"),
            take_profit_price=Decimal("210"), reward_to_risk_ratio=Decimal("2"),
            risk_amount=Decimal("50"), account_balance_at_entry=Decimal("100000"),
        )
        await repo.update_status(tid, TradeStatus.CLOSING)
        await repo.finish_close(
            tid, Decimal("100"), Decimal("-3100"), None, closed_at=FIXED_NOW - timedelta(hours=1)
        )
        await s.commit()

    eng.circuit_breaker = CircuitBreaker(daily_loss_limit_pct=Decimal("0.03"))
    assert eng.circuit_breaker.is_halted(now=FIXED_NOW) is False
    await eng.rehydrate_circuit_breaker()

    assert eng.circuit_breaker.is_halted(now=FIXED_NOW) is True
    assert eng.circuit_breaker.is_halted(now=FIXED_NOW + timedelta(days=1)) is False
