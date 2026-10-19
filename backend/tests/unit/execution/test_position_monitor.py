"""
Unit tests for PositionMonitor (B4): in-process prices, market-hours gate,
DB resync, stale sweep, graceful stop and the price-feed watchdog.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.brokers.base import PriceUpdate
from app.core.execution.position_monitor import PositionMonitor, _TrackedTrade


def _monitor(**kwargs) -> PositionMonitor:
    kwargs.setdefault("is_market_open", lambda: True)
    kwargs.setdefault("order_manager", AsyncMock())
    return PositionMonitor(cache=AsyncMock(), session_factory=AsyncMock(), **kwargs)


@pytest.fixture
def monitor() -> PositionMonitor:
    return _monitor()


def _tick(price: str, ticker: str = "AAPL") -> PriceUpdate:
    return PriceUpdate(ticker=ticker, price=Decimal(price), timestamp=datetime.now(UTC))


async def _settle(monitor: PositionMonitor) -> None:
    if monitor._tasks:
        await asyncio.gather(*list(monitor._tasks))


def _track(
    monitor: PositionMonitor,
    *,
    symbol="AAPL",
    direction="BUY",
    entry="200",
    stop="190",
    tp="220",
    opened_at: datetime | None = None,
) -> uuid.UUID:
    trade_id = uuid.uuid4()
    monitor._tracked[trade_id] = _TrackedTrade(
        trade_id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_price=Decimal(entry),
        stop_loss_price=Decimal(stop),
        initial_stop_loss_price=Decimal(stop),
        take_profit_price=Decimal(tp),
        opened_at=opened_at or datetime.now(UTC),
    )
    return trade_id


# ---------------------------------------------------------------------------
# Level hits and market-hours gate
# ---------------------------------------------------------------------------


async def test_price_below_stop_closes_once(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, stop="190", tp="220")
    await monitor.on_price_update(_tick("189.50"))
    await monitor.on_price_update(_tick("189.00"))
    await _settle(monitor)
    monitor._order_manager.close_position.assert_awaited_once_with(trade_id, "STOP_LOSS")
    assert trade_id not in monitor._tracked
    assert trade_id not in monitor._closing


async def test_price_above_target_closes_take_profit(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, stop="190", tp="220")
    await monitor.on_price_update(_tick("221"))
    await _settle(monitor)
    monitor._order_manager.close_position.assert_awaited_once_with(trade_id, "TAKE_PROFIT")


async def test_market_closed_never_closes() -> None:
    monitor = _monitor(is_market_open=lambda: False)
    trade_id = _track(monitor, stop="190", tp="220")
    await monitor.on_price_update(_tick("100"))
    await _settle(monitor)
    monitor._order_manager.close_position.assert_not_called()
    assert trade_id in monitor._tracked


async def test_price_within_range_no_close(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, stop="190", tp="220")
    await monitor.on_price_update(_tick("205"))
    assert not monitor._tasks
    assert trade_id in monitor._tracked


async def test_other_ticker_ignored(monitor: PositionMonitor) -> None:
    _track(monitor, symbol="MSFT")
    await monitor.on_price_update(_tick("1", "AAPL"))
    assert not monitor._tasks


async def test_failed_close_clears_in_flight_marker(monitor: PositionMonitor) -> None:
    monitor._order_manager.close_position.side_effect = RuntimeError("broker down")
    trade_id = _track(monitor)
    await monitor.on_price_update(_tick("1"))
    await _settle(monitor)
    assert trade_id not in monitor._closing


# ---------------------------------------------------------------------------
# Trailing / breakeven stop (BUY trades only) - entry=200, stop=190 -> R=10
# ---------------------------------------------------------------------------


async def test_stop_unchanged_before_1r_profit(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, entry="200", stop="190", tp="999")
    await monitor.on_price_update(_tick("205"))
    assert monitor._tracked[trade_id].stop_loss_price == Decimal("190")


async def test_stop_moves_to_breakeven_at_exactly_1r(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, entry="200", stop="190", tp="999")
    await monitor.on_price_update(_tick("210"))
    assert monitor._tracked[trade_id].stop_loss_price == Decimal("200")


async def test_stop_trails_further_as_price_advances(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, entry="200", stop="190", tp="999")
    await monitor.on_price_update(_tick("220"))
    assert monitor._tracked[trade_id].stop_loss_price == Decimal("210")


async def test_stop_never_loosens_on_pullback(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, entry="200", stop="190", tp="999")
    await monitor.on_price_update(_tick("220"))
    await monitor.on_price_update(_tick("212"))
    assert monitor._tracked[trade_id].stop_loss_price == Decimal("210")


async def test_trailed_stop_eventually_triggers_close(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, entry="200", stop="190", tp="999")
    await monitor.on_price_update(_tick("220"))
    assert not monitor._tasks
    await monitor.on_price_update(_tick("209"))
    await _settle(monitor)
    monitor._order_manager.close_position.assert_awaited_once_with(trade_id, "STOP_LOSS")


async def test_trailing_disabled_keeps_original_stop() -> None:
    disabled = _monitor(trailing_stop_enabled=False)
    trade_id = _track(disabled, entry="200", stop="190", tp="999")
    await disabled.on_price_update(_tick("230"))
    assert disabled._tracked[trade_id].stop_loss_price == Decimal("190")


async def test_trailing_skipped_for_non_buy_direction(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, direction="SELL", entry="200", stop="190", tp="999")
    await monitor.on_price_update(_tick("220"))
    assert monitor._tracked[trade_id].stop_loss_price == Decimal("190")


# ---------------------------------------------------------------------------
# Stale-trade timeout sweep
# ---------------------------------------------------------------------------


async def test_sweep_force_closes_trade_past_max_age(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, opened_at=datetime.now(UTC) - timedelta(days=20))
    await monitor._sweep_stale_trades()
    await _settle(monitor)
    monitor._order_manager.close_position.assert_awaited_once_with(trade_id, "STALE_TIMEOUT")


async def test_sweep_leaves_recent_trade_alone(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, opened_at=datetime.now(UTC) - timedelta(days=2))
    await monitor._sweep_stale_trades()
    assert not monitor._tasks
    assert trade_id in monitor._tracked


async def test_sweep_disabled_when_max_days_is_none() -> None:
    disabled = _monitor(stale_trade_max_days=None)
    _track(disabled, opened_at=datetime.now(UTC) - timedelta(days=999))
    await disabled._sweep_stale_trades()
    assert not disabled._tasks


async def test_sweep_skips_trade_already_closing(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, opened_at=datetime.now(UTC) - timedelta(days=20))
    monitor._closing.add(trade_id)
    await monitor._sweep_stale_trades()
    assert not monitor._tasks


async def test_sweep_gated_by_market_open() -> None:
    closed = _monitor(is_market_open=lambda: False)
    _track(closed, opened_at=datetime.now(UTC) - timedelta(days=20))
    await closed._sweep_stale_trades()
    assert not closed._tasks


# ---------------------------------------------------------------------------
# DB resync
# ---------------------------------------------------------------------------


def _db_trade(trade_id=None, symbol="AAPL", entry="200", stop="190", tp="220"):
    return SimpleNamespace(
        id=trade_id or uuid.uuid4(),
        symbol=symbol,
        direction=SimpleNamespace(value="BUY"),
        entry_price=Decimal(entry),
        stop_loss_price=Decimal(stop),
        take_profit_price=Decimal(tp),
        executed_at=datetime.now(UTC),
    )


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_open_trades(monkeypatch, holder: list):
    class Repo:
        def __init__(self, session) -> None:
            pass

        async def get_open_trades(self):
            return list(holder)

    monkeypatch.setattr("app.db.repositories.trade_repo.TradeRepo", Repo)


async def test_resync_adds_removes_and_preserves_trailing_state(monkeypatch) -> None:
    monitor = _monitor()
    monitor._session_factory = _Session
    survivor = _db_trade(tp="999")
    holder = [survivor]
    _patch_open_trades(monkeypatch, holder)

    await monitor._resync()
    await monitor.on_price_update(_tick("220"))  # ratchets stop to 210
    assert monitor._tracked[survivor.id].stop_loss_price == Decimal("210")

    gone = _track(monitor, symbol="MSFT")
    new = _db_trade(symbol="NVDA")
    holder.append(new)
    await monitor._resync()

    assert new.id in monitor._tracked
    assert gone not in monitor._tracked
    assert monitor._tracked[survivor.id].stop_loss_price == Decimal("210")

    holder.remove(survivor)
    await monitor._resync()
    assert survivor.id not in monitor._tracked


async def test_resync_does_not_retrack_closing_trade(monkeypatch) -> None:
    monitor = _monitor()
    monitor._session_factory = _Session
    trade = _db_trade()
    _patch_open_trades(monkeypatch, [trade])
    monitor._closing.add(trade.id)
    await monitor._resync()
    assert trade.id not in monitor._tracked


# ---------------------------------------------------------------------------
# stop() waits for in-flight closes
# ---------------------------------------------------------------------------


async def test_stop_waits_for_inflight_close() -> None:
    monitor = _monitor()
    finished = asyncio.Event()

    async def slow_close(trade_id, reason):
        await asyncio.sleep(0.1)
        finished.set()

    monitor._order_manager.close_position = slow_close
    _track(monitor)
    await monitor.on_price_update(_tick("1"))
    await monitor.stop()
    assert finished.is_set()


# ---------------------------------------------------------------------------
# Price-feed watchdog (Monday 2026-10-19 is an NYSE session; open 09:30 ET = 13:30 UTC)
# ---------------------------------------------------------------------------

_OPEN_UTC = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)


def _watchdog_monitor(now: datetime, last_tick):
    clock = {"now": now}
    monitor = _monitor(clock=lambda: clock["now"], last_tick_at=lambda s: last_tick, price_stale_seconds=120)
    monitor._started_at = _OPEN_UTC - timedelta(hours=1)
    return monitor, clock


async def test_watchdog_alerts_once_per_symbol_per_day(monkeypatch) -> None:
    alerts: list[tuple] = []

    async def fake_alert(cache, alert_type, message, **kw):
        alerts.append((alert_type, kw.get("symbol")))

    monkeypatch.setattr("app.core.execution.position_monitor.publish_system_alert", fake_alert)
    monitor, _ = _watchdog_monitor(_OPEN_UTC + timedelta(minutes=30), last_tick=None)
    _track(monitor, symbol="AAPL")
    await monitor._check_price_feed()
    await monitor._check_price_feed()
    assert alerts == [("PRICE_FEED_STALE", "AAPL")]


async def test_watchdog_silent_in_open_grace_period_and_with_fresh_tick(monkeypatch) -> None:
    alerts: list = []

    async def fake_alert(*a, **kw):
        alerts.append(a)

    monkeypatch.setattr("app.core.execution.position_monitor.publish_system_alert", fake_alert)
    monitor, _ = _watchdog_monitor(_OPEN_UTC + timedelta(seconds=60), last_tick=None)
    _track(monitor)
    await monitor._check_price_feed()
    assert alerts == []

    now = _OPEN_UTC + timedelta(minutes=30)
    fresh, _ = _watchdog_monitor(now, last_tick=now - timedelta(seconds=10))
    _track(fresh)
    await fresh._check_price_feed()
    assert alerts == []


async def test_watchdog_silent_when_market_closed(monkeypatch) -> None:
    alerts: list = []

    async def fake_alert(*a, **kw):
        alerts.append(a)

    monkeypatch.setattr("app.core.execution.position_monitor.publish_system_alert", fake_alert)
    monitor, _ = _watchdog_monitor(_OPEN_UTC + timedelta(minutes=30), last_tick=None)
    monitor._is_market_open = lambda: False
    _track(monitor)
    await monitor._check_price_feed()
    assert alerts == []
