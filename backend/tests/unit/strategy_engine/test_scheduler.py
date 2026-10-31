"""Unit tests for StrategyScheduler (B7): window, completed bars, fresh prices, one action per signal."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.brokers.base import PriceBar
from app.core.market_calendar import ET
from app.core.risk.growth import GrowthPolicy
from app.core.risk.manager import RiskRejectionError
from app.core.strategy_engine.base import Signal
from app.core.strategy_engine.scheduler import (
    _CONSECUTIVE_MISS_ESCALATION_THRESHOLD,
    StrategyScheduler,
)

STRATEGY_ID = uuid.uuid4()
MONDAY_11AM = datetime(2026, 10, 19, 11, 0, tzinfo=ET)
MONDAY_8AM = datetime(2026, 10, 19, 8, 0, tzinfo=ET)
SATURDAY = datetime(2026, 10, 17, 11, 0, tzinfo=ET)
FRIDAY = date(2026, 10, 16)


def _bar(day: date, close: str = "100") -> PriceBar:
    return PriceBar(
        timestamp=datetime(day.year, day.month, day.day, tzinfo=UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1000,
    )


class FakeSession:
    async def commit(self) -> None:
        pass


@asynccontextmanager
async def _factory():
    yield FakeSession()


class FakeSignalRepo:
    def __init__(self, claims: list):
        self.claims = claims
        self.outcomes: list[tuple] = []

    async def claim(self, **kwargs):
        self.claims.append(kwargs)
        return uuid.uuid4() if len(self.claims) == 1 else None

    async def set_outcome(self, signal_id, outcome, detail=None, trade_id=None):
        self.outcomes.append((outcome, detail, trade_id))


class FakeTradeRepo:
    active = False

    def __init__(self, _session):
        pass

    async def has_active_trade(self, symbol):
        return FakeTradeRepo.active


def _price_json(price="150", age=0, bid=None, ask=None) -> str:
    stamp = datetime.now(UTC) - timedelta(seconds=age)
    payload = {"price": price, "timestamp": stamp.isoformat()}
    if bid is not None:
        payload.update(bid=bid, ask=ask)
    return json.dumps(payload)


def _make(clock_now=MONDAY_11AM, **kwargs):
    claims: list = []
    signals = FakeSignalRepo(claims)
    FakeTradeRepo.active = False
    sched = StrategyScheduler(
        registry=AsyncMock(),
        broker=AsyncMock(),
        risk_manager=AsyncMock(),
        cache=AsyncMock(),
        session_factory=_factory,
        order_manager=AsyncMock(),
        clock=lambda: clock_now,
        signal_repo_factory=lambda _s: signals,
        trade_repo_factory=FakeTradeRepo,
        **kwargs,
    )
    sched._broker.is_connected = lambda: True
    sched._broker.get_account_summary = AsyncMock(
        return_value=SimpleNamespace(net_liquidation=Decimal("100000"))
    )
    sched._cache.get = AsyncMock(return_value=_price_json())
    sched._fetch_db_bars = AsyncMock(return_value=[_bar(FRIDAY)])
    sched._signals = signals
    return sched


def _buy_strategy(quantity=10):
    strategy = AsyncMock()
    strategy.required_extra_symbols = lambda: []
    strategy.needs_intraday = False
    strategy.generate_signal = AsyncMock(
        return_value=Signal(
            symbol="AAPL",
            action="BUY",
            entry_price=Decimal("150"),
            stop_loss_price=Decimal("145"),
            quantity=quantity,
            timestamp=datetime.now(UTC),
        )
    )
    strategy.calculate_position_size = AsyncMock(return_value=quantity)
    return strategy


async def _run(sched, strategy, symbol="AAPL"):
    await sched._run_strategy_for_symbol(
        strategy=strategy,
        strategy_id=STRATEGY_ID,
        symbol=symbol,
        account_balance=Decimal("100000"),
    )


# --- window / gating -------------------------------------------------------


@pytest.mark.parametrize("now", [MONDAY_8AM, SATURDAY])
async def test_outside_window_loads_no_strategies(now):
    sched = _make(now)
    sched._load_enabled_strategies = AsyncMock(return_value=[])
    await sched._run_cycle()
    sched._load_enabled_strategies.assert_not_awaited()
    assert sched.last_cycle_at is None


async def test_inside_window_sets_last_cycle_at():
    sched = _make()
    sched._load_enabled_strategies = AsyncMock(return_value=[])
    await sched._run_cycle()
    sched._load_enabled_strategies.assert_awaited_once()
    assert sched.last_cycle_at is not None


async def test_paused_skips_cycle():
    sched = _make(is_paused=AsyncMock(return_value=True))
    sched._load_enabled_strategies = AsyncMock(return_value=[])
    await sched._run_cycle()
    sched._load_enabled_strategies.assert_not_awaited()


async def test_disconnected_broker_skips_cycle():
    sched = _make()
    sched._broker.is_connected = lambda: False
    sched._load_enabled_strategies = AsyncMock(return_value=[])
    await sched._run_cycle()
    sched._load_enabled_strategies.assert_not_awaited()


async def test_zero_balance_skips_cycle():
    sched = _make()
    sched._broker.get_account_summary = AsyncMock(
        return_value=SimpleNamespace(net_liquidation=Decimal("0"))
    )
    sched._load_enabled_strategies = AsyncMock(return_value=[])
    await sched._run_cycle()
    sched._load_enabled_strategies.assert_not_awaited()


# --- one action per signal -------------------------------------------------


async def test_always_buy_submits_once_across_three_cycles():
    sched = _make()
    strategy = _buy_strategy()
    for _ in range(3):
        await _run(sched, strategy)
    sched._order_manager.submit_order.assert_awaited_once()
    assert len(sched._signals.claims) == 3
    assert sched._signals.claims[0]["bar_date"] == FRIDAY
    assert sched._signals.outcomes[0][0] == "SUBMITTED"
    assert sched._signals.outcomes[0][2] is not None


async def test_active_trade_blocks_submit_without_claim():
    sched = _make()
    FakeTradeRepo.active = True
    await _run(sched, _buy_strategy())
    sched._order_manager.submit_order.assert_not_awaited()
    assert sched._signals.claims == []


async def test_open_position_flag_reaches_strategy():
    sched = _make()
    FakeTradeRepo.active = True
    strategy = _buy_strategy()
    await _run(sched, strategy)
    assert strategy.generate_signal.await_args.args[0].open_position is True


async def test_risk_rejection_recorded():
    sched = _make()
    sched._order_manager.submit_order = AsyncMock(
        side_effect=RiskRejectionError("SYMBOL_ALREADY_ACTIVE: x", request=None)
    )
    await _run(sched, _buy_strategy())
    assert sched._signals.outcomes == [("RISK_REJECTED", "SYMBOL_ALREADY_ACTIVE: x", None)]


async def test_order_failure_recorded():
    sched = _make()
    sched._order_manager.submit_order = AsyncMock(side_effect=RuntimeError("boom"))
    await _run(sched, _buy_strategy())
    assert sched._signals.outcomes == [("ORDER_FAILED", "boom", None)]


async def test_zero_quantity_recorded():
    sched = _make()
    strategy = _buy_strategy(quantity=0)
    await _run(sched, strategy)
    sched._order_manager.submit_order.assert_not_awaited()
    assert sched._signals.outcomes[0][0] == "ZERO_QUANTITY"


# --- bars ------------------------------------------------------------------


async def test_stale_bars_do_not_submit():
    sched = _make()
    sched._fetch_db_bars = AsyncMock(return_value=[_bar(FRIDAY - timedelta(days=7))])
    await _run(sched, _buy_strategy())
    sched._order_manager.submit_order.assert_not_awaited()


async def test_bars_dated_today_are_excluded():
    NS = SimpleNamespace
    sched = _make()
    rows = [
        NS(time=datetime(2026, 10, 16, tzinfo=UTC), open=1, high=1, low=1, close=1,
           volume=1, bar_size="1 day"),
        NS(time=datetime(2026, 10, 19, tzinfo=UTC), open=2, high=2, low=2, close=2,
           volume=1, bar_size="1 day"),
    ]
    with patch("app.db.repositories.ohlcv_repo.OHLCVRepo") as repo_cls:
        repo_cls.return_value.get_bars = AsyncMock(return_value=rows)
        bars = await StrategyScheduler._fetch_db_bars(sched, "AAPL", date(2026, 10, 19))
    assert [b.close for b in bars] == [Decimal("1")]


async def test_bars_cached_per_symbol_and_day():
    sched = _make()
    await sched._get_historical_bars("AAPL")
    await sched._get_historical_bars("AAPL")
    sched._fetch_db_bars.assert_awaited_once()


# --- price -----------------------------------------------------------------


async def test_stale_price_does_not_submit():
    sched = _make()
    sched._cache.get = AsyncMock(return_value=_price_json(age=600))
    await _run(sched, _buy_strategy())
    sched._order_manager.submit_order.assert_not_awaited()


async def test_price_without_timestamp_is_stale():
    sched = _make()
    sched._cache.get = AsyncMock(return_value=json.dumps({"price": "150"}))
    assert await sched._get_current_price("AAPL") is None


async def test_fresh_price_read_and_no_broker_data_calls():
    sched = _make()
    assert await sched._get_current_price("AAPL") == Decimal("150")
    sched._broker.get_historical_data.assert_not_called()


async def test_redis_error_returns_none():
    sched = _make()
    sched._cache.get = AsyncMock(side_effect=ConnectionError("redis down"))
    assert await sched._get_current_price("AAPL") is None


async def test_no_price_logs_warning_below_escalation_threshold():
    sched = _make()
    sched._get_current_price = AsyncMock(return_value=None)
    with patch("app.core.strategy_engine.scheduler.system_logger") as log:
        await _run(sched, AsyncMock())
    log.warning.assert_called_once()
    log.error.assert_not_called()
    assert sched._consecutive_price_misses["AAPL"] == 1


async def test_no_price_escalates_to_error_after_threshold():
    sched = _make()
    sched._get_current_price = AsyncMock(return_value=None)
    sched._consecutive_price_misses["AAPL"] = _CONSECUTIVE_MISS_ESCALATION_THRESHOLD - 1
    with patch("app.core.strategy_engine.scheduler.system_logger") as log:
        await _run(sched, AsyncMock())
    log.error.assert_called_once()


async def test_miss_counter_resets_once_price_is_found():
    sched = _make()
    sched._consecutive_price_misses["AAPL"] = 3
    strategy = _buy_strategy()
    strategy.generate_signal = AsyncMock(
        return_value=Signal(symbol="AAPL", action="HOLD", timestamp=datetime.now(UTC))
    )
    await _run(sched, strategy)
    assert "AAPL" not in sched._consecutive_price_misses


# --- extra bars ------------------------------------------------------------


async def test_extra_bars_populated_for_required_symbol():
    sched = _make()
    strategy = _buy_strategy()
    strategy.required_extra_symbols = lambda: ["SPY"]
    await _run(sched, strategy)
    assert "SPY" in strategy.generate_signal.await_args.args[0].extra_bars


async def test_extra_symbol_equal_to_target_is_not_refetched():
    sched = _make()
    strategy = _buy_strategy()
    strategy.required_extra_symbols = lambda: ["AAPL"]
    await _run(sched, strategy)
    assert sched._fetch_db_bars.await_count == 1


async def test_missing_extra_bars_warn_but_continue():
    sched = _make()

    async def fetch(symbol, day):
        return [_bar(FRIDAY)] if symbol == "AAPL" else []

    sched._fetch_db_bars = AsyncMock(side_effect=fetch)
    strategy = _buy_strategy()
    strategy.required_extra_symbols = lambda: ["SPY"]
    await _run(sched, strategy)
    strategy.generate_signal.assert_awaited_once()


async def test_three_consecutive_failed_cycles_publish_scheduler_failing():
    import asyncio

    sched = _make(interval_seconds=0.01)
    sched._run_cycle = AsyncMock(side_effect=RuntimeError("boom"))
    published = []
    sched._cache.publish = AsyncMock(side_effect=lambda ch, msg: published.append(json.loads(msg)))
    await sched.start()
    await asyncio.sleep(0.2)
    await sched.stop()
    alerts = [m for m in published if m["payload"]["alert_type"] == "SCHEDULER_FAILING"]
    assert len(alerts) == 1


# --- growth mode: spread, liquidity, effective equity (I2) -----------------


@pytest.mark.parametrize(
    ("bid", "ask", "claimed"),
    [("100.00", "100.50", False), ("100.00", "100.01", True), (None, None, True)],
)
async def test_spread_gate(bid, ask, claimed):
    sched = _make(max_spread_bps=Decimal("20"))
    sched._cache.get = AsyncMock(return_value=_price_json(bid=bid, ask=ask))
    await _run(sched, _buy_strategy())
    assert bool(sched._signals.claims) is claimed


async def test_low_liquidity_is_ineligible():
    sched = _make(min_avg_dollar_volume=Decimal(10**9))
    await _run(sched, _buy_strategy())
    assert sched._signals.outcomes[0][0] == "INELIGIBLE"
    sched._order_manager.submit_order.assert_not_awaited()


async def test_growth_mode_alert_only_on_change():
    growth = GrowthPolicy(True, Decimal("2000"), 2, frozenset())
    sched = _make(growth=growth)
    sched._load_enabled_strategies = AsyncMock(return_value=[])
    with patch("app.core.strategy_engine.scheduler.publish_system_alert", new=AsyncMock()) as alert:
        for equity in ("100", "100", "3000"):
            sched._broker.get_account_summary = AsyncMock(
                return_value=SimpleNamespace(net_liquidation=Decimal(equity))
            )
            await sched._run_cycle()
    alert.assert_awaited_once()
    assert alert.await_args.args[1] == "GROWTH_MODE_CHANGED"


async def test_capital_limit_sizes_from_limit():
    sched = _make(capital_limit=Decimal("100"))
    sched._broker.get_account_summary = AsyncMock(
        return_value=SimpleNamespace(net_liquidation=Decimal("100000"))
    )
    strategy = _buy_strategy(quantity=None)
    sched._registry = SimpleNamespace(is_registered=lambda _t: True, build=lambda _t, _c: strategy)
    sched._load_enabled_strategies = AsyncMock(
        return_value=[SimpleNamespace(id=STRATEGY_ID, type="x", config={"symbols": ["AAPL"]})]
    )
    await sched._run_cycle()
    assert strategy.calculate_position_size.await_args.args[0].account_balance == Decimal("100")


# --- intraday bars -----------------------------------------------------------


def _intraday_bar(hour: int, minute: int) -> PriceBar:
    return PriceBar(
        timestamp=datetime(2026, 10, 19, hour, minute, tzinfo=ET).astimezone(UTC),
        open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100"),
        volume=1000, bar_size="5 mins",
    )


async def test_intraday_bars_reach_strategy_and_cache_per_bucket():
    sched = _make()
    strategy = _buy_strategy()
    strategy.needs_intraday = True
    # one bar from today, one stale bar from the previous session (dropped)
    stale = _intraday_bar(10, 0).model_copy(update={"timestamp": datetime(2026, 10, 16, 15, 0, tzinfo=UTC)})
    sched._broker.get_intraday_bars = AsyncMock(return_value=[stale, _intraday_bar(10, 0)])

    await _run(sched, strategy)
    await _run(sched, strategy)  # same 5-minute bucket: no second broker call

    sched._broker.get_intraday_bars.assert_awaited_once_with("AAPL")
    seen = strategy.generate_signal.await_args.args[0]
    assert [b.timestamp for b in seen.intraday_bars] == [_intraday_bar(10, 0).timestamp]


async def test_new_bucket_fetches_again():
    clock = {"now": MONDAY_11AM}
    sched = _make()
    sched._clock = lambda: clock["now"]
    sched._broker.get_intraday_bars = AsyncMock(return_value=[_intraday_bar(10, 0)])
    await sched._get_intraday_bars("AAPL")
    clock["now"] = MONDAY_11AM + timedelta(minutes=5)
    await sched._get_intraday_bars("AAPL")
    assert sched._broker.get_intraday_bars.await_count == 2


async def test_no_intraday_bars_skips_strategy():
    sched = _make()
    strategy = _buy_strategy()
    strategy.needs_intraday = True
    sched._broker.get_intraday_bars = AsyncMock(return_value=[])
    await _run(sched, strategy)
    strategy.generate_signal.assert_not_awaited()


async def test_exit_deadline_flows_into_trade_request():
    sched = _make()
    strategy = _buy_strategy()
    deadline = datetime(2026, 10, 19, 15, 55, tzinfo=ET)
    strategy.generate_signal.return_value = strategy.generate_signal.return_value.model_copy(
        update={"exit_deadline": deadline}
    )
    await _run(sched, strategy)
    request = sched._order_manager.submit_order.await_args.args[0]
    assert request.exit_deadline == deadline
