"""
Unit tests for PositionMonitor (Layer 6B.9).

Focus: price-feed and trade-event message handling, including the
previously-silent parse-failure paths (malformed price ticks, malformed or
incomplete trade_events payloads) which now log instead of vanishing.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.core.execution.position_monitor import PositionMonitor, _TrackedTrade


@pytest.fixture
def monitor() -> PositionMonitor:
    return PositionMonitor(cache=AsyncMock(), session_factory=AsyncMock(), order_manager=AsyncMock())


def _price_msg(channel: str, data) -> dict:
    return {"type": "pmessage", "channel": channel, "data": data}


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
# Price parsing — valid forms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_price_json_payload_triggers_stop_loss(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, stop="190", tp="220")
    with patch("app.core.execution.position_monitor.asyncio.create_task") as mock_task:
        await monitor._handle_price(_price_msg("price:AAPL", '{"price": "189.50"}'))
    mock_task.assert_called_once()
    assert trade_id in monitor._closing
    assert trade_id not in monitor._tracked


@pytest.mark.asyncio
async def test_handle_price_plain_decimal_string_triggers_take_profit(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, stop="190", tp="220")
    with patch("app.core.execution.position_monitor.asyncio.create_task") as mock_task:
        await monitor._handle_price(_price_msg("price:AAPL", "221.00"))
    mock_task.assert_called_once()
    assert trade_id in monitor._closing


@pytest.mark.asyncio
async def test_handle_price_within_range_no_close(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, stop="190", tp="220")
    with patch("app.core.execution.position_monitor.asyncio.create_task") as mock_task:
        await monitor._handle_price(_price_msg("price:AAPL", "205.00"))
    mock_task.assert_not_called()
    assert trade_id in monitor._tracked


# ---------------------------------------------------------------------------
# Price parsing — malformed payloads now log instead of vanishing silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_price_unparseable_data_logs_warning_and_skips(monitor: PositionMonitor) -> None:
    _track(monitor, stop="190", tp="220")
    with (
        patch("app.core.execution.position_monitor.system_logger") as mock_logger,
        patch("app.core.execution.position_monitor.asyncio.create_task") as mock_task,
    ):
        await monitor._handle_price(_price_msg("price:AAPL", "not-a-price"))
    mock_task.assert_not_called()
    mock_logger.warning.assert_called_once()
    assert "unparseable" in mock_logger.warning.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_handle_price_bad_channel_format_is_ignored(monitor: PositionMonitor) -> None:
    # No exception, no log — channel itself is malformed, nothing to check yet.
    await monitor._handle_price(_price_msg("not-a-channel", "100"))


# ---------------------------------------------------------------------------
# trade_events — malformed/incomplete payloads now log instead of vanishing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_executed_with_all_fields_is_tracked(monitor: PositionMonitor) -> None:
    trade_id = uuid.uuid4()
    event = {
        "event": "trade_executed",
        "payload": {
            "trade_id": str(trade_id),
            "symbol": "AAPL",
            "direction": "BUY",
            "entry_price": "200",
            "stop_loss_price": "190",
            "take_profit_price": "220",
        },
    }
    import json

    await monitor._handle_trade_event({"type": "message", "data": json.dumps(event)})
    assert trade_id in monitor._tracked
    tracked = monitor._tracked[trade_id]
    assert tracked.entry_price == Decimal("200")
    assert tracked.direction == "BUY"
    assert tracked.initial_stop_loss_price == Decimal("190")


@pytest.mark.asyncio
async def test_trade_executed_missing_fields_logs_error_and_is_not_tracked(monitor: PositionMonitor) -> None:
    trade_id = uuid.uuid4()
    event = {
        "event": "trade_executed",
        "payload": {"trade_id": str(trade_id), "symbol": "AAPL"},  # no SL/TP
    }
    import json

    with patch("app.core.execution.position_monitor.system_logger") as mock_logger:
        await monitor._handle_trade_event({"type": "message", "data": json.dumps(event)})
    assert trade_id not in monitor._tracked
    mock_logger.error.assert_called_once()
    assert "missing required fields" in mock_logger.error.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_unparseable_trade_event_logs_warning(monitor: PositionMonitor) -> None:
    with patch("app.core.execution.position_monitor.system_logger") as mock_logger:
        await monitor._handle_trade_event({"type": "message", "data": "{not json"})
    mock_logger.warning.assert_called_once()
    assert "unparseable" in mock_logger.warning.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_trade_closed_removes_from_tracked(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor)
    import json

    event = {"event": "trade_closed", "payload": {"trade_id": str(trade_id)}}
    await monitor._handle_trade_event({"type": "message", "data": json.dumps(event)})
    assert trade_id not in monitor._tracked


# ---------------------------------------------------------------------------
# Trailing / breakeven stop (BUY trades only) — entry=200, stop=190 → R=10
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_unchanged_before_1r_profit(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, entry="200", stop="190", tp="999")
    with patch("app.core.execution.position_monitor.asyncio.create_task"):
        await monitor._handle_price(_price_msg("price:AAPL", "205"))  # +0.5R
    assert monitor._tracked[trade_id].stop_loss_price == Decimal("190")


@pytest.mark.asyncio
async def test_stop_moves_to_breakeven_at_exactly_1r(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, entry="200", stop="190", tp="999")
    with patch("app.core.execution.position_monitor.asyncio.create_task"):
        await monitor._handle_price(_price_msg("price:AAPL", "210"))  # +1R exactly
    assert monitor._tracked[trade_id].stop_loss_price == Decimal("200")


@pytest.mark.asyncio
async def test_stop_trails_further_as_price_advances(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, entry="200", stop="190", tp="999")
    with patch("app.core.execution.position_monitor.asyncio.create_task"):
        await monitor._handle_price(_price_msg("price:AAPL", "220"))  # +2R
    # trailing 1R (10) behind price 220 → stop = 210, locking in +1R of profit
    assert monitor._tracked[trade_id].stop_loss_price == Decimal("210")


@pytest.mark.asyncio
async def test_stop_never_loosens_on_pullback(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, entry="200", stop="190", tp="999")
    with patch("app.core.execution.position_monitor.asyncio.create_task"):
        await monitor._handle_price(_price_msg("price:AAPL", "220"))  # ratchets stop to 210
        await monitor._handle_price(_price_msg("price:AAPL", "212"))  # pulls back, still > 210
    assert monitor._tracked[trade_id].stop_loss_price == Decimal("210")


@pytest.mark.asyncio
async def test_trailed_stop_eventually_triggers_close_at_better_price_than_original(
    monitor: PositionMonitor,
) -> None:
    trade_id = _track(monitor, entry="200", stop="190", tp="999")
    with patch("app.core.execution.position_monitor.asyncio.create_task") as mock_task:
        await monitor._handle_price(_price_msg("price:AAPL", "220"))  # ratchets stop to 210
        mock_task.assert_not_called()
        await monitor._handle_price(_price_msg("price:AAPL", "209"))  # falls through trailed stop
    mock_task.assert_called_once()
    assert trade_id in monitor._closing


@pytest.mark.asyncio
async def test_trailing_disabled_keeps_original_stop(monitor: PositionMonitor) -> None:
    disabled_monitor = PositionMonitor(
        cache=AsyncMock(),
        session_factory=AsyncMock(),
        order_manager=AsyncMock(),
        trailing_stop_enabled=False,
    )
    trade_id = _track(disabled_monitor, entry="200", stop="190", tp="999")
    with patch("app.core.execution.position_monitor.asyncio.create_task"):
        await disabled_monitor._handle_price(_price_msg("price:AAPL", "230"))  # +3R
    assert disabled_monitor._tracked[trade_id].stop_loss_price == Decimal("190")


@pytest.mark.asyncio
async def test_trailing_skipped_for_non_buy_direction(monitor: PositionMonitor) -> None:
    trade_id = _track(monitor, direction="SELL", entry="200", stop="190", tp="999")
    with patch("app.core.execution.position_monitor.asyncio.create_task"):
        await monitor._handle_price(_price_msg("price:AAPL", "220"))
    assert monitor._tracked[trade_id].stop_loss_price == Decimal("190")


# ---------------------------------------------------------------------------
# Stale-trade timeout sweep
# ---------------------------------------------------------------------------


def test_sweep_force_closes_trade_past_max_age(monitor: PositionMonitor) -> None:
    old = datetime.now(UTC) - timedelta(days=20)
    trade_id = _track(monitor, opened_at=old)  # default max_days=14

    with patch("app.core.execution.position_monitor.asyncio.create_task") as mock_task:
        monitor._sweep_stale_trades()

    mock_task.assert_called_once()
    assert trade_id in monitor._closing
    assert trade_id not in monitor._tracked


def test_sweep_leaves_recent_trade_alone(monitor: PositionMonitor) -> None:
    recent = datetime.now(UTC) - timedelta(days=2)
    trade_id = _track(monitor, opened_at=recent)

    with patch("app.core.execution.position_monitor.asyncio.create_task") as mock_task:
        monitor._sweep_stale_trades()

    mock_task.assert_not_called()
    assert trade_id in monitor._tracked


def test_sweep_disabled_when_max_days_is_none() -> None:
    disabled_monitor = PositionMonitor(
        cache=AsyncMock(),
        session_factory=AsyncMock(),
        order_manager=AsyncMock(),
        stale_trade_max_days=None,
    )
    old = datetime.now(UTC) - timedelta(days=999)
    trade_id = _track(disabled_monitor, opened_at=old)

    with patch("app.core.execution.position_monitor.asyncio.create_task") as mock_task:
        disabled_monitor._sweep_stale_trades()

    mock_task.assert_not_called()
    assert trade_id in disabled_monitor._tracked


def test_sweep_skips_trade_already_closing(monitor: PositionMonitor) -> None:
    old = datetime.now(UTC) - timedelta(days=20)
    trade_id = _track(monitor, opened_at=old)
    monitor._closing.add(trade_id)

    with patch("app.core.execution.position_monitor.asyncio.create_task") as mock_task:
        monitor._sweep_stale_trades()

    mock_task.assert_not_called()


@pytest.mark.asyncio
async def test_trade_executed_event_timestamp_is_used_as_opened_at(
    monitor: PositionMonitor,
) -> None:
    import json

    trade_id = uuid.uuid4()
    ts = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    event = {
        "event": "trade_executed",
        "payload": {
            "trade_id": str(trade_id),
            "symbol": "AAPL",
            "direction": "BUY",
            "entry_price": "200",
            "stop_loss_price": "190",
            "take_profit_price": "220",
        },
        "timestamp": ts,
    }
    await monitor._handle_trade_event({"type": "message", "data": json.dumps(event)})
    assert monitor._tracked[trade_id].opened_at.isoformat() == ts
