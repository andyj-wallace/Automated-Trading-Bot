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
    while monitor._tasks:
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
    exit_deadline: datetime | None = None,
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
        exit_deadline=exit_deadline,
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
    assert not monitor._closing
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
# Time exit (day trades)
# ---------------------------------------------------------------------------


async def test_time_exit_closes_trade_past_its_deadline(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, exit_deadline=datetime.now(UTC) - timedelta(minutes=1))
    await monitor._sweep_time_exits()
    await _settle(monitor)
    monitor._order_manager.close_position.assert_awaited_once_with(trade_id, "TIME_EXIT")


async def test_time_exit_leaves_future_and_no_deadline_trades(monitor: PositionMonitor) -> None:
    _track(monitor, exit_deadline=datetime.now(UTC) + timedelta(hours=1))
    _track(monitor)
    await monitor._sweep_time_exits()
    assert not monitor._tasks


async def test_time_exit_waits_for_market_open() -> None:
    closed = _monitor(is_market_open=lambda: False)
    _track(closed, exit_deadline=datetime.now(UTC) - timedelta(minutes=1))
    await closed._sweep_time_exits()
    assert not closed._tasks


async def test_resync_tracks_the_exit_deadline(monkeypatch) -> None:
    monitor = _monitor()
    monitor._session_factory = _Session
    deadline = datetime.now(UTC)
    trade = _db_trade(exit_deadline=deadline)
    _patch_open_trades(monkeypatch, [trade])
    await monitor._resync()
    assert monitor._tracked[trade.id].exit_deadline == deadline


async def test_track_starts_from_persisted_stop(monkeypatch) -> None:
    monitor = _monitor()
    monitor._session_factory = _Session
    trade = _db_trade(entry="100", stop="95", tp="220", current_stop_price="101")
    _patch_open_trades(monkeypatch, [trade])
    await monitor._resync()
    tracked = monitor._tracked[trade.id]
    assert tracked.stop_loss_price == Decimal("101")
    assert tracked.initial_stop_loss_price == Decimal("95")
    assert tracked.persisted_stop == Decimal("101")


async def test_track_ignores_lower_persisted_stop(monkeypatch) -> None:
    monitor = _monitor()
    monitor._session_factory = _Session
    trade = _db_trade(entry="100", stop="95", tp="220", current_stop_price="90")
    _patch_open_trades(monkeypatch, [trade])
    await monitor._resync()
    assert monitor._tracked[trade.id].stop_loss_price == Decimal("95")


async def test_track_without_persisted_stop(monkeypatch) -> None:
    monitor = _monitor()
    monitor._session_factory = _Session
    trade = _db_trade(entry="100", stop="95", tp="220", current_stop_price=None)
    _patch_open_trades(monkeypatch, [trade])
    await monitor._resync()
    assert monitor._tracked[trade.id].stop_loss_price == Decimal("95")


async def test_restart_with_persisted_stop_closes_with_stop_loss(monkeypatch) -> None:
    monitor = _monitor()
    monitor._session_factory = _Session
    trade = _db_trade(entry="100", stop="95", tp="220", current_stop_price="101")
    _patch_open_trades(monkeypatch, [trade])
    await monitor._resync()
    await monitor.on_price_update(_tick("101", trade.symbol))
    await _settle(monitor)
    monitor._order_manager.close_position.assert_awaited_once_with(trade.id, "STOP_LOSS")


# ---------------------------------------------------------------------------
# DB resync
# ---------------------------------------------------------------------------


def _db_trade(
    trade_id=None,
    symbol="AAPL",
    entry="200",
    stop="190",
    tp="220",
    exit_deadline=None,
    current_stop_price=None,
):
    return SimpleNamespace(
        id=trade_id or uuid.uuid4(),
        symbol=symbol,
        direction=SimpleNamespace(value="BUY"),
        entry_price=Decimal(entry),
        stop_loss_price=Decimal(stop),
        take_profit_price=Decimal(tp),
        executed_at=datetime.now(UTC),
        exit_deadline=exit_deadline,
        current_stop_price=Decimal(current_stop_price) if current_stop_price is not None else None,
    )


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


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


# ---------------------------------------------------------------------------
# Persisting the trailed stop (entry=100, stop=95 -> R=5, 0.1R=0.5)
# ---------------------------------------------------------------------------


def _patch_stop_writes(
    monkeypatch, *, fail_first: bool = False, fail_times: int = 0, gate=None
) -> list:
    calls: list[tuple[uuid.UUID, Decimal]] = []
    state = {"fails": max(fail_times, 1 if fail_first else 0)}

    class Repo:
        def __init__(self, session) -> None:
            pass

        async def raise_current_stop(self, trade_id, stop_price):
            calls.append((trade_id, stop_price))
            if gate is not None:
                await gate.wait()
            if state["fails"] > 0:
                state["fails"] -= 1
                raise RuntimeError("db down")

    monkeypatch.setattr("app.db.repositories.trade_repo.TradeRepo", Repo)
    return calls


async def test_ratchet_of_point_one_r_persists_stop(monkeypatch) -> None:
    calls = _patch_stop_writes(monkeypatch)
    monitor = _monitor()
    monitor._session_factory = _Session
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    await monitor.on_price_update(_tick("106"))
    await _settle(monitor)
    assert calls == [(trade_id, Decimal("101"))]
    assert monitor._tracked[trade_id].persisted_stop == Decimal("101")


async def test_small_ratchet_does_not_persist(monkeypatch) -> None:
    calls = _patch_stop_writes(monkeypatch)
    monitor = _monitor()
    monitor._session_factory = _Session
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    await monitor.on_price_update(_tick("106"))
    await _settle(monitor)
    await monitor.on_price_update(_tick("106.3"))  # stop 101.3, only +0.3 from 101
    await _settle(monitor)
    assert len(calls) == 1
    await monitor.on_price_update(_tick("106.6"))  # stop 101.6, +0.6 from 101
    await _settle(monitor)
    assert calls == [(trade_id, Decimal("101")), (trade_id, Decimal("101.6"))]


async def test_failed_write_retries_on_next_tick(monkeypatch) -> None:
    calls = _patch_stop_writes(monkeypatch, fail_first=True)
    monitor = _monitor()
    monitor._session_factory = _Session
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    await monitor.on_price_update(_tick("106"))
    await _settle(monitor)
    assert calls == [(trade_id, Decimal("101"))]
    assert monitor._tracked[trade_id].persisted_stop != Decimal("101")
    assert monitor._tracked[trade_id].persist_in_flight is False
    await monitor.on_price_update(_tick("106"))  # no new ratchet, still retries
    await _settle(monitor)
    assert calls[-1] == (trade_id, Decimal("101"))
    assert len(calls) == 2
    assert monitor._tracked[trade_id].persisted_stop == Decimal("101")


async def test_no_duplicate_write_while_in_flight(monkeypatch) -> None:
    gate = asyncio.Event()
    calls = _patch_stop_writes(monkeypatch, gate=gate)
    monitor = _monitor()
    monitor._session_factory = _Session
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    await monitor.on_price_update(_tick("106"))
    await monitor.on_price_update(_tick("106.6"))
    await asyncio.sleep(0)
    assert calls == [(trade_id, Decimal("101"))]
    gate.set()
    await _settle(monitor)
    # The rise to 101.6 during the write is saved right after it, one write at a time.
    assert calls == [(trade_id, Decimal("101")), (trade_id, Decimal("101.6"))]
    assert monitor._tracked[trade_id].persisted_stop == Decimal("101.6")


async def test_no_follow_up_write_once_trade_is_untracked(monkeypatch) -> None:
    gate = asyncio.Event()
    calls = _patch_stop_writes(monkeypatch, gate=gate)
    monitor = _monitor()
    monitor._session_factory = _Session
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    await monitor.on_price_update(_tick("106"))
    await monitor.on_price_update(_tick("106.6"))
    await asyncio.sleep(0)
    monitor._tracked.pop(trade_id)
    gate.set()
    await _settle(monitor)
    assert calls == [(trade_id, Decimal("101"))]


def _capture_errors(monkeypatch) -> list:
    errors: list[dict] = []
    monkeypatch.setattr(
        "app.core.execution.position_monitor.system_logger.error",
        lambda msg, *a, **kw: errors.append(kw.get("extra", {})),
    )
    return errors


def _backoff_monitor(now: datetime, seconds: float = 5.0):
    clock = {"now": now}
    monitor = _monitor(clock=lambda: clock["now"], stop_persist_retry_seconds=seconds)
    monitor._session_factory = _Session
    return monitor, clock


async def test_failing_write_backs_off_instead_of_retrying_every_tick(monkeypatch) -> None:
    calls = _patch_stop_writes(monkeypatch, fail_times=2)
    errors = _capture_errors(monkeypatch)
    monitor, clock = _backoff_monitor(_OPEN_UTC)
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    await monitor.on_price_update(_tick("106"))
    await _settle(monitor)
    assert calls == [(trade_id, Decimal("101"))]
    assert len(errors) == 1 and errors[0]["retry_in_seconds"] == 5.0
    assert monitor._tracked[trade_id].next_persist_after == _OPEN_UTC + timedelta(seconds=5)

    for price in ("106.6", "107", "107.5"):  # each rise alone would qualify for a write
        await monitor.on_price_update(_tick(price))
        await _settle(monitor)
    clock["now"] = _OPEN_UTC + timedelta(seconds=4.9)
    await monitor.on_price_update(_tick("107.5"))
    await _settle(monitor)
    assert len(calls) == 1
    assert len(errors) == 1

    clock["now"] = _OPEN_UTC + timedelta(seconds=5.1)
    await monitor.on_price_update(_tick("107.5"))
    await _settle(monitor)
    assert calls[-1] == (trade_id, Decimal("102.5"))
    assert len(calls) == 2 and len(errors) == 2  # still failing: a new window starts
    assert monitor._tracked[trade_id].next_persist_after == _OPEN_UTC + timedelta(seconds=10.1)
    await monitor.on_price_update(_tick("107.5"))
    await _settle(monitor)
    assert len(calls) == 2


async def test_successful_retry_after_backoff_clears_it(monkeypatch) -> None:
    calls = _patch_stop_writes(monkeypatch, fail_first=True)
    monitor, clock = _backoff_monitor(_OPEN_UTC)
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    await monitor.on_price_update(_tick("106"))
    await _settle(monitor)
    clock["now"] = _OPEN_UTC + timedelta(seconds=6)
    await monitor.on_price_update(_tick("106"))
    await _settle(monitor)
    assert calls == [(trade_id, Decimal("101")), (trade_id, Decimal("101"))]
    tracked = monitor._tracked[trade_id]
    assert tracked.persisted_stop == Decimal("101")
    assert tracked.next_persist_after is None
    assert tracked.persist_in_flight is False


async def test_stop_hit_during_backoff_still_closes(monkeypatch) -> None:
    _patch_stop_writes(monkeypatch, fail_first=True)
    monitor, _ = _backoff_monitor(_OPEN_UTC)
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    await monitor.on_price_update(_tick("106"))  # trails the stop to 101; the write fails
    await _settle(monitor)
    assert monitor._tracked[trade_id].next_persist_after is not None
    await monitor.on_price_update(_tick("100.5"))
    await _settle(monitor)
    monitor._order_manager.close_position.assert_awaited_once_with(trade_id, "STOP_LOSS")


async def test_default_without_backoff_retries_failing_write_every_tick(monkeypatch) -> None:
    calls = _patch_stop_writes(monkeypatch, fail_times=3)
    errors = _capture_errors(monkeypatch)
    monitor = _monitor()
    monitor._session_factory = _Session
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    for _ in range(3):
        await monitor.on_price_update(_tick("106"))
        await _settle(monitor)
    assert calls == [(trade_id, Decimal("101"))] * 3
    assert [e["retry_in_seconds"] for e in errors] == [0.0, 0.0, 0.0]
    assert monitor._tracked[trade_id].next_persist_after is None


async def test_rise_during_write_saved_right_after_with_backoff(monkeypatch) -> None:
    gate = asyncio.Event()
    calls = _patch_stop_writes(monkeypatch, gate=gate)
    monitor, _ = _backoff_monitor(_OPEN_UTC)
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    await monitor.on_price_update(_tick("106"))
    await monitor.on_price_update(_tick("106.6"))
    await asyncio.sleep(0)
    gate.set()
    await _settle(monitor)
    assert calls == [(trade_id, Decimal("101")), (trade_id, Decimal("101.6"))]
    assert monitor._tracked[trade_id].persisted_stop == Decimal("101.6")
    assert monitor._tracked[trade_id].next_persist_after is None


async def test_no_follow_up_write_once_untracked_with_backoff(monkeypatch) -> None:
    gate = asyncio.Event()
    calls = _patch_stop_writes(monkeypatch, gate=gate)
    monitor, _ = _backoff_monitor(_OPEN_UTC)
    trade_id = _track(monitor, entry="100", stop="95", tp="220")
    await monitor.on_price_update(_tick("106"))
    await monitor.on_price_update(_tick("106.6"))
    await asyncio.sleep(0)
    tracked = monitor._tracked.pop(trade_id)
    gate.set()
    await _settle(monitor)
    assert calls == [(trade_id, Decimal("101"))]
    assert tracked.next_persist_after is None
