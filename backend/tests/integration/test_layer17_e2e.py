"""
Layer 17 end-to-end integration test suite — requires live Docker services.

Verifies the full trade execution pipeline from signal generation through to
position close, covering every component boundary in the execution path:

  Entry flow
    (1)  OrderManager: PENDING → SUBMITTED → OPEN status transitions
    (2)  Audit trail: both PRE_SUBMISSION and POST_CONFIRMATION entries written
    (3)  Risk rejected trade leaves no DB row

  Exit flow
    (4)  close_position(STOP_LOSS): OPEN → CLOSING → CLOSED, correct pnl + exit_reason
    (5)  close_position(TAKE_PROFIT): OPEN → CLOSING → CLOSED, correct exit_reason
    (6)  close_position publishes trade_closed event to Redis
    (7)  close_position on missing trade: graceful no-op, no crash

  PositionMonitor integration
    (8)  Price below stop_loss published to Redis → PositionMonitor fires close → CLOSED
    (9)  Price above take_profit published to Redis → PositionMonitor fires close → CLOSED

  Strategy → execution pipeline
    (10) MovingAverageStrategy golden-cross signal flows to OPEN trade in DB

  Portfolio risk gate
    (11) New trade blocked when current aggregate risk + new risk would exceed cap

Prerequisites:
    docker-compose up -d
    docker-compose exec backend alembic upgrade head

Run:
    docker-compose exec backend pytest tests/integration/test_layer17_e2e.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.brokers.mock.client import MockBroker
from app.core.execution.order_manager import OrderManager
from app.core.execution.position_monitor import PositionMonitor
from app.core.risk.manager import RiskManager, RiskRejectionError, TradeRequest
from app.core.strategy_engine.moving_average import MovingAverageStrategy
from app.data.cache import RedisCache
from app.db.models.trade import TradeStatus
from app.db.repositories.trade_repo import TradeRepo

_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://trading:trading@localhost:5432/trading_bot",
)
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def broker() -> MockBroker:
    b = MockBroker()
    await b.connect()
    yield b
    await b.disconnect()


@pytest_asyncio.fixture
async def cache() -> RedisCache:
    c = RedisCache(_REDIS_URL)
    yield c
    await c.close()


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(_DB_URL, echo=False)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    """Async session factory compatible with OrderManager and PositionMonitor."""
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _factory():
        async with factory() as sess:
            yield sess

    return _factory


@pytest_asyncio.fixture
async def session(engine) -> AsyncSession:
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Standard test parameters:
#   entry_price   = 200.00
#   stop_loss     = 195.00  → stop_distance = 5.00
#   account       = 100,000
#   risk_amount   = qty × 5 = 10 × 5 = 50  (0.05% of account)
#   take_profit   = entry + (stop_distance × min_rr 2.0) = 210.00
_ENTRY = Decimal("200")
_STOP  = Decimal("195")
_QTY   = Decimal("10")
_BAL   = Decimal("100000")
_TP    = Decimal("210")   # = 200 + (5 × 2.0)

_STOP_TRIGGER = Decimal("194")   # price that fires stop loss (≤ 195)
_TP_TRIGGER   = Decimal("211")   # price that fires take profit (≥ 210)


def _valid_request(**kwargs) -> TradeRequest:
    defaults = dict(
        trade_id=uuid.uuid4(),
        symbol="AAPL",
        direction="BUY",
        quantity=_QTY,
        entry_price=_ENTRY,
        stop_loss_price=_STOP,
        account_balance=_BAL,
    )
    defaults.update(kwargs)
    return TradeRequest(**defaults)


def _make_order_manager(broker, session_factory, cache=None) -> OrderManager:
    return OrderManager(
        broker=broker,
        risk_manager=RiskManager(),
        session_factory=session_factory,
        cache=cache,
    )


async def _poll_for_status(
    session_factory,
    trade_id: uuid.UUID,
    expected: TradeStatus,
    timeout: float = 5.0,
    interval: float = 0.1,
) -> "Trade | None":
    """Poll the DB until trade reaches expected status or timeout expires."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with session_factory() as s:
            trade = await TradeRepo(s).get_by_id(trade_id)
            if trade and trade.status == expected:
                return trade
        await asyncio.sleep(interval)
    return None


async def _open_trade(order_manager: OrderManager, **kwargs) -> TradeRequest:
    """Submit an order and assert it reaches OPEN status. Returns the request."""
    req = _valid_request(**kwargs)
    validation, result = await order_manager.submit_order(req)
    assert result.status == "FILLED", f"Expected FILLED, got {result.status}"
    return req


# ---------------------------------------------------------------------------
# Group 1 — Full entry flow
# ---------------------------------------------------------------------------


class TestFullEntryFlow:
    @pytest.mark.asyncio
    async def test_pending_submitted_open_transitions(
        self, broker, session_factory, session
    ) -> None:
        """Trade row transitions PENDING → SUBMITTED → OPEN during submit_order()."""
        om = _make_order_manager(broker, session_factory)
        req = _valid_request()

        # Spy on place_order to capture DB status at broker-call time
        status_at_broker_call: list[TradeStatus] = []
        original = broker.place_order

        async def _spy(order_req):
            async with session_factory() as s:
                t = await TradeRepo(s).get_by_id(req.trade_id)
                if t:
                    status_at_broker_call.append(t.status)
            return await original(order_req)

        broker.place_order = _spy
        await om.submit_order(req)
        broker.place_order = original

        assert status_at_broker_call, "Broker spy was never called"
        # Must be SUBMITTED by the time the broker receives the order
        assert status_at_broker_call[0] == TradeStatus.SUBMITTED

        # Final status in DB must be OPEN
        repo = TradeRepo(session)
        trade = await repo.get_by_id(req.trade_id)
        assert trade is not None
        assert trade.status == TradeStatus.OPEN

    @pytest.mark.asyncio
    async def test_trade_db_row_has_correct_fields(
        self, broker, session_factory, session
    ) -> None:
        """Filled trade row has correct symbol, stop, take_profit, and R:R."""
        om = _make_order_manager(broker, session_factory)
        req = _valid_request(symbol="MSFT")
        await om.submit_order(req)

        trade = await TradeRepo(session).get_by_id(req.trade_id)
        assert trade is not None
        assert trade.symbol == "MSFT"
        assert trade.stop_loss_price == _STOP
        assert trade.take_profit_price == _TP
        assert trade.reward_to_risk_ratio == Decimal("2")
        assert trade.risk_amount == Decimal("50")          # 10 × (200 − 195)
        assert trade.account_balance_at_entry == _BAL

    @pytest.mark.asyncio
    async def test_audit_both_entries_written(self, broker, session_factory) -> None:
        """PRE_SUBMISSION and POST_CONFIRMATION entries both appear in audit.log."""
        om = _make_order_manager(broker, session_factory)
        req = _valid_request()

        with patch("app.core.execution.order_manager.audit_logger") as mock_audit:
            await om.submit_order(req)

        written = [c.args[0] for c in mock_audit.info.call_args_list]
        assert "PRE_SUBMISSION" in written
        assert "POST_CONFIRMATION" in written

    @pytest.mark.asyncio
    async def test_risk_rejected_trade_has_no_db_row(
        self, broker, session_factory, session
    ) -> None:
        """A trade rejected by risk validation must leave no row in the DB."""
        om = _make_order_manager(broker, session_factory)
        req = _valid_request(stop_loss_price=None)  # missing stop-loss → rejected

        with pytest.raises(RiskRejectionError):
            await om.submit_order(req)

        trade = await TradeRepo(session).get_by_id(req.trade_id)
        assert trade is None, "Rejected trade must not appear in the DB"

    @pytest.mark.asyncio
    async def test_cancelled_status_on_broker_rejection(
        self, broker, session_factory, session
    ) -> None:
        """A trade whose broker order is rejected transitions to CANCELLED."""
        om = _make_order_manager(broker, session_factory)
        req = _valid_request()

        # Make MockBroker return REJECTED
        from app.brokers.base import OrderResult
        original = broker.place_order

        async def _reject(order_req):
            return OrderResult(
                trade_id=order_req.trade_id,
                broker_order_id="REJECTED-001",
                status="REJECTED",
                filled_quantity=Decimal("0"),
                avg_fill_price=Decimal("0"),
                error_message="Insufficient buying power",
                timestamp=datetime.now(timezone.utc),
            )

        broker.place_order = _reject
        await om.submit_order(req)
        broker.place_order = original

        trade = await TradeRepo(session).get_by_id(req.trade_id)
        assert trade is not None
        assert trade.status == TradeStatus.CANCELLED


# ---------------------------------------------------------------------------
# Group 2 — Exit flow (close_position)
# ---------------------------------------------------------------------------


class TestClosePositionFlow:
    @pytest.mark.asyncio
    async def test_close_stop_loss_sets_correct_status_and_exit_reason(
        self, broker, session_factory, session
    ) -> None:
        """close_position(STOP_LOSS) transitions OPEN → CLOSED with correct reason."""
        om = _make_order_manager(broker, session_factory)
        req = await _open_trade(om)

        await om.close_position(req.trade_id, "STOP_LOSS")

        from app.db.models.trade import ExitReason
        trade = await TradeRepo(session).get_by_id(req.trade_id)
        assert trade is not None
        assert trade.status == TradeStatus.CLOSED
        assert trade.exit_reason == ExitReason.STOP_LOSS
        assert trade.exit_price is not None
        assert trade.closed_at is not None

    @pytest.mark.asyncio
    async def test_close_take_profit_sets_correct_exit_reason(
        self, broker, session_factory, session
    ) -> None:
        """close_position(TAKE_PROFIT) sets the correct exit_reason."""
        om = _make_order_manager(broker, session_factory)
        req = await _open_trade(om)

        await om.close_position(req.trade_id, "TAKE_PROFIT")

        from app.db.models.trade import ExitReason
        trade = await TradeRepo(session).get_by_id(req.trade_id)
        assert trade is not None
        assert trade.status == TradeStatus.CLOSED
        assert trade.exit_reason == ExitReason.TAKE_PROFIT

    @pytest.mark.asyncio
    async def test_close_pnl_calculated_correctly_for_buy(
        self, broker, session_factory, session
    ) -> None:
        """PnL = (fill_price − entry_price) × qty for a BUY trade."""
        om = _make_order_manager(broker, session_factory)
        req = await _open_trade(om, entry_price=Decimal("200"), quantity=Decimal("10"))

        await om.close_position(req.trade_id, "TAKE_PROFIT")

        trade = await TradeRepo(session).get_by_id(req.trade_id)
        assert trade is not None
        assert trade.pnl is not None
        # MockBroker fills at entry_price; pnl = (200 − 200) × 10 = 0
        assert trade.pnl == Decimal("0")

    @pytest.mark.asyncio
    async def test_close_publishes_trade_closed_event(
        self, broker, session_factory, cache
    ) -> None:
        """close_position publishes a trade_closed event to the trade_events channel."""
        om = _make_order_manager(broker, session_factory, cache=cache)
        req = await _open_trade(om)

        published: list[tuple[str, dict]] = []
        original_publish = cache.publish

        async def _capture(channel: str, message: str) -> None:
            published.append((channel, json.loads(message)))
            await original_publish(channel, message)

        cache.publish = _capture  # type: ignore[method-assign]
        await om.close_position(req.trade_id, "MANUAL")
        cache.publish = original_publish  # type: ignore[method-assign]

        trade_closed_events = [
            (ch, p) for (ch, p) in published
            if p.get("event") == "trade_closed"
        ]
        assert len(trade_closed_events) == 1
        ch, event = trade_closed_events[0]
        assert ch == "trade_events"
        assert event["payload"]["trade_id"] == str(req.trade_id)
        assert event["payload"]["reason"] == "MANUAL"

    @pytest.mark.asyncio
    async def test_close_nonexistent_trade_is_noop(
        self, broker, session_factory
    ) -> None:
        """close_position on an unknown trade_id must not raise — logs error only."""
        om = _make_order_manager(broker, session_factory)
        ghost_id = uuid.uuid4()

        # Must not raise
        await om.close_position(ghost_id, "STOP_LOSS")

    @pytest.mark.asyncio
    async def test_close_already_closed_trade_logs_critical(
        self, broker, session_factory
    ) -> None:
        """Attempting to close an already-CLOSED trade is logged as CRITICAL."""
        om = _make_order_manager(broker, session_factory)
        req = await _open_trade(om)
        await om.close_position(req.trade_id, "STOP_LOSS")

        # Second close attempt — trade is now CLOSED, not OPEN
        with patch("app.core.execution.order_manager.error_logger") as mock_err:
            await om.close_position(req.trade_id, "STOP_LOSS")

        mock_err.critical.assert_called_once()
        assert "unexpected status" in mock_err.critical.call_args.args[0].lower()


# ---------------------------------------------------------------------------
# Group 3 — PositionMonitor integration
# ---------------------------------------------------------------------------


class TestPositionMonitorIntegration:
    @pytest.mark.asyncio
    async def test_stop_loss_price_triggers_close_via_position_monitor(
        self, broker, session_factory, cache
    ) -> None:
        """
        PositionMonitor detects a price at stop_loss level and closes the trade.

        Flow:
          1. Submit order → OPEN in DB
          2. Start PositionMonitor (loads open trade from DB)
          3. Publish price at stop_loss trigger level to Redis
          4. Poll DB for CLOSED status
        """
        om = _make_order_manager(broker, session_factory, cache=cache)
        req = await _open_trade(om, symbol="TSLA")

        monitor = PositionMonitor(
            cache=cache,
            session_factory=session_factory,
            order_manager=om,
        )
        await monitor.start()
        # Allow the subscription task to set up
        await asyncio.sleep(0.2)

        try:
            # Publish a price below stop_loss (195) to trigger the monitor
            price_payload = json.dumps({"price": str(_STOP_TRIGGER), "ticker": "TSLA"})
            await cache.publish("price:TSLA", price_payload)

            # Poll for CLOSED status (PositionMonitor fires close asynchronously)
            trade = await _poll_for_status(
                session_factory, req.trade_id, TradeStatus.CLOSED, timeout=5.0
            )
            assert trade is not None, "Trade did not reach CLOSED within timeout"
            assert trade.status == TradeStatus.CLOSED

            from app.db.models.trade import ExitReason
            assert trade.exit_reason == ExitReason.STOP_LOSS
        finally:
            await monitor.stop()

    @pytest.mark.asyncio
    async def test_take_profit_price_triggers_close_via_position_monitor(
        self, broker, session_factory, cache
    ) -> None:
        """PositionMonitor closes a trade when price crosses the take-profit level."""
        om = _make_order_manager(broker, session_factory, cache=cache)
        req = await _open_trade(om, symbol="NVDA")

        monitor = PositionMonitor(
            cache=cache,
            session_factory=session_factory,
            order_manager=om,
        )
        await monitor.start()
        await asyncio.sleep(0.2)

        try:
            price_payload = json.dumps({"price": str(_TP_TRIGGER), "ticker": "NVDA"})
            await cache.publish("price:NVDA", price_payload)

            trade = await _poll_for_status(
                session_factory, req.trade_id, TradeStatus.CLOSED, timeout=5.0
            )
            assert trade is not None, "Trade did not reach CLOSED within timeout"

            from app.db.models.trade import ExitReason
            assert trade.exit_reason == ExitReason.TAKE_PROFIT
        finally:
            await monitor.stop()

    @pytest.mark.asyncio
    async def test_price_between_stop_and_target_does_not_close(
        self, broker, session_factory, cache
    ) -> None:
        """A price between stop (195) and take-profit (210) leaves the trade OPEN."""
        om = _make_order_manager(broker, session_factory, cache=cache)
        req = await _open_trade(om, symbol="AMD")

        monitor = PositionMonitor(
            cache=cache,
            session_factory=session_factory,
            order_manager=om,
        )
        await monitor.start()
        await asyncio.sleep(0.2)

        try:
            # Publish a neutral price (between stop and target)
            neutral_price = json.dumps({"price": "202.00", "ticker": "AMD"})
            await cache.publish("price:AMD", neutral_price)
            await asyncio.sleep(0.3)

            async with session_factory() as s:
                trade = await TradeRepo(s).get_by_id(req.trade_id)
            assert trade is not None
            assert trade.status == TradeStatus.OPEN
        finally:
            await monitor.stop()

    @pytest.mark.asyncio
    async def test_position_monitor_does_not_double_close(
        self, broker, session_factory, cache
    ) -> None:
        """Rapid price publishes for the same trade fire close_position only once."""
        close_calls: list[uuid.UUID] = []
        om = _make_order_manager(broker, session_factory, cache=cache)
        req = await _open_trade(om, symbol="META")

        monitor = PositionMonitor(
            cache=cache,
            session_factory=session_factory,
            order_manager=om,
        )
        original_close = om.close_position

        async def _count_close(trade_id, reason):
            close_calls.append(trade_id)
            await original_close(trade_id, reason)

        om.close_position = _count_close  # type: ignore[method-assign]
        await monitor.start()
        await asyncio.sleep(0.2)

        try:
            payload = json.dumps({"price": str(_STOP_TRIGGER), "ticker": "META"})
            # Publish twice in rapid succession
            await cache.publish("price:META", payload)
            await cache.publish("price:META", payload)
            await asyncio.sleep(0.5)

            # close_position should have been called exactly once
            meta_closes = [t for t in close_calls if t == req.trade_id]
            assert len(meta_closes) == 1, (
                f"Expected 1 close call, got {len(meta_closes)}"
            )
        finally:
            om.close_position = original_close  # type: ignore[method-assign]
            await monitor.stop()


# ---------------------------------------------------------------------------
# Group 4 — Strategy → execution pipeline
# ---------------------------------------------------------------------------


class TestStrategyToExecution:
    @pytest.mark.asyncio
    async def test_moving_average_golden_cross_signal_creates_open_trade(
        self, broker, session_factory, session
    ) -> None:
        """
        MovingAverageStrategy generates a BUY signal from golden-cross data
        and submit_order() creates an OPEN trade in the DB.
        """
        from app.brokers.base import PriceBar
        from app.core.strategy_engine.base import MarketData
        from tests.unit.strategy.conftest import make_bars

        # Build a golden-cross bar sequence (200-day base + 50-day rally)
        base = make_bars(200, start_price=Decimal("200"), trend=Decimal("-0.1"))
        rally = make_bars(50, start_price=base[-1].close, trend=Decimal("0.6"))
        bars = base + rally

        # Re-timestamp to be contiguous
        end_date = datetime.now(timezone.utc).replace(
            hour=16, minute=0, second=0, microsecond=0
        )
        from app.brokers.base import PriceBar as PB
        retstamped: list[PB] = []
        for i, bar in enumerate(bars):
            ts = end_date - timedelta(days=(len(bars) - 1 - i))
            retstamped.append(bar.model_copy(update={"timestamp": ts}))
        bars = retstamped

        strategy = MovingAverageStrategy(
            config={"fast_period": 50, "slow_period": 200, "stop_loss_pct": "0.03"}
        )
        market_data = MarketData(
            symbol="AAPL",
            current_price=bars[-1].close,
            bars=bars,
            timestamp=datetime.now(timezone.utc),
        )

        signal = await strategy.generate_signal(market_data)
        assert signal.action == "BUY", (
            f"Expected BUY signal from golden cross, got {signal.action}"
        )
        assert signal.entry_price is not None
        assert signal.stop_loss_price is not None
        assert signal.stop_loss_price < signal.entry_price

        # Build TradeRequest from the signal and submit
        trade_id = uuid.uuid4()
        req = TradeRequest(
            trade_id=trade_id,
            symbol="AAPL",
            direction="BUY",
            quantity=Decimal("5"),
            entry_price=signal.entry_price,
            stop_loss_price=signal.stop_loss_price,
            account_balance=_BAL,
        )
        om = _make_order_manager(broker, session_factory)
        validation, result = await om.submit_order(req)

        assert result.status == "FILLED"

        trade = await TradeRepo(session).get_by_id(trade_id)
        assert trade is not None
        assert trade.status == TradeStatus.OPEN
        assert trade.stop_loss_price == signal.stop_loss_price
        assert trade.take_profit_price == validation.take_profit_price


# ---------------------------------------------------------------------------
# Group 5 — Portfolio risk gate
# ---------------------------------------------------------------------------


class TestPortfolioRiskGate:
    @pytest.mark.asyncio
    async def test_new_trade_blocked_when_aggregate_risk_near_limit(
        self, broker, session_factory
    ) -> None:
        """
        Gate 4: a trade is rejected with PORTFOLIO_RISK_LIMIT_EXCEEDED when
        current_aggregate_risk + new_risk_amount > max_portfolio_risk × balance.

        Setup: pass current_aggregate_risk = 4,960 (99.2% of the 5,000 cap).
        New trade risk = qty × stop_distance = 10 × 5 = 50.
        4,960 + 50 = 5,010 > 5,000 → REJECTED.
        """
        om = _make_order_manager(broker, session_factory)
        req = _valid_request()
        current_aggregate = Decimal("4960")  # leaves only $40 headroom

        with pytest.raises(RiskRejectionError) as exc_info:
            await om.submit_order(req, current_aggregate_risk=current_aggregate)

        assert "PORTFOLIO_RISK_LIMIT_EXCEEDED" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_trade_allowed_when_aggregate_risk_has_headroom(
        self, broker, session_factory, session
    ) -> None:
        """A trade is accepted when current aggregate risk leaves sufficient headroom."""
        om = _make_order_manager(broker, session_factory)
        req = _valid_request()
        current_aggregate = Decimal("4000")  # $1,000 headroom; new risk = $50

        validation, result = await om.submit_order(
            req, current_aggregate_risk=current_aggregate
        )
        assert result.status == "FILLED"

        trade = await TradeRepo(session).get_by_id(req.trade_id)
        assert trade is not None
        assert trade.status == TradeStatus.OPEN
