"""
Unit tests for NotificationDispatcher (16.4 + 18.3).

Covers: dispatch routing, no-op when unconfigured, SMS config parsing,
fire-and-forget error handling, and all four notification types.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.notifications.dispatcher import NotificationDispatcher


# ---------------------------------------------------------------------------
# Stub settings
# ---------------------------------------------------------------------------


class _Settings:
    def __init__(self, smtp: str = "", sms: str = "") -> None:
        self.notification_email_smtp = smtp
        self.notification_sms_twilio = sms


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_smtp_disabled_when_not_configured() -> None:
    d = NotificationDispatcher(_Settings())
    assert not d._enabled


def test_smtp_enabled_when_url_provided() -> None:
    d = NotificationDispatcher(_Settings(smtp="smtp://user:pass@mail.example.com:587/me@example.com"))
    assert d._enabled


def test_sms_disabled_when_not_configured() -> None:
    d = NotificationDispatcher(_Settings())
    assert not d._sms_enabled


def test_sms_enabled_when_twilio_url_provided() -> None:
    d = NotificationDispatcher(_Settings(sms="twilio://ACxxx:tok@+15551234567/+15559876543"))
    assert d._sms_enabled


# ---------------------------------------------------------------------------
# Fire-and-forget: no-op when unconfigured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_is_noop_when_smtp_disabled() -> None:
    """_send() should silently do nothing when SMTP is unconfigured."""
    d = NotificationDispatcher(_Settings())
    # Should not raise and should not call any executor
    with patch.object(asyncio, "get_event_loop") as mock_loop:
        await d._send("subject", "body")
        mock_loop.assert_not_called()


@pytest.mark.asyncio
async def test_send_sms_is_noop_when_disabled() -> None:
    """_send_sms() should silently do nothing when Twilio is unconfigured."""
    d = NotificationDispatcher(_Settings())
    with patch.object(asyncio, "get_event_loop") as mock_loop:
        await d._send_sms("hello")
        mock_loop.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_routes_risk_alert() -> None:
    d = NotificationDispatcher(_Settings())
    called_with: list[dict] = []

    async def _fake_notify_risk(*args, **kwargs):
        called_with.append(kwargs)

    d.notify_risk_alert = _fake_notify_risk  # type: ignore[method-assign]

    event = {
        "event": "risk_alert",
        "payload": {
            "alert_level": "WARNING",
            "aggregate_risk_pct": "0.035",
            "aggregate_risk_amount": "350",
            "open_trade_count": 3,
            "account_balance": "100000",
        },
    }
    await d.dispatch(event)
    assert len(called_with) == 1
    assert called_with[0]["alert_level"] == "WARNING"


@pytest.mark.asyncio
async def test_dispatch_routes_trade_executed() -> None:
    d = NotificationDispatcher(_Settings())
    received: list[dict] = []

    async def _fake(*args, **kwargs):
        received.append(kwargs)

    d.notify_trade_executed = _fake  # type: ignore[method-assign]

    event = {
        "event": "trade_executed",
        "payload": {
            "symbol": "AAPL",
            "direction": "BUY",
            "quantity": "10",
            "entry_price": "150.00",
            "stop_loss_price": "145.00",
            "take_profit_price": "160.00",
            "risk_amount": "50.00",
        },
    }
    await d.dispatch(event)
    assert len(received) == 1
    assert received[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_dispatch_routes_trade_closed() -> None:
    d = NotificationDispatcher(_Settings())
    received: list[dict] = []

    async def _fake(*args, **kwargs):
        received.append(kwargs)

    d.notify_trade_closed = _fake  # type: ignore[method-assign]

    event = {
        "event": "trade_closed",
        "payload": {
            "symbol": "MSFT",
            "reason": "TAKE_PROFIT",
            "exit_price": "320.00",
            "pnl": "120.00",
        },
    }
    await d.dispatch(event)
    assert len(received) == 1
    assert received[0]["reason"] == "TAKE_PROFIT"


@pytest.mark.asyncio
async def test_dispatch_routes_system_alert() -> None:
    d = NotificationDispatcher(_Settings())
    received: list[dict] = []

    async def _fake(*args, **kwargs):
        received.append(kwargs)

    d.notify_system_alert = _fake  # type: ignore[method-assign]

    event = {
        "event": "system_alert",
        "payload": {"alert_type": "BROKER_RECONNECT_FAILED", "message": "5 attempts exhausted"},
    }
    await d.dispatch(event)
    assert len(received) == 1
    assert received[0]["alert_type"] == "BROKER_RECONNECT_FAILED"


@pytest.mark.asyncio
async def test_dispatch_unknown_event_type_does_not_raise() -> None:
    """Unknown event types should be silently ignored."""
    d = NotificationDispatcher(_Settings())
    await d.dispatch({"event": "completely_unknown", "payload": {}})  # must not raise


# ---------------------------------------------------------------------------
# Error handling — fire-and-forget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_does_not_propagate() -> None:
    """If SMTP delivery fails, the error is swallowed."""
    d = NotificationDispatcher(_Settings(smtp="smtp://bad@nowhere:9/fail@fail.com"))

    async def _boom(executor, fn, *args):
        raise ConnectionRefusedError("smtp unreachable")

    loop = MagicMock()
    loop.run_in_executor = _boom

    with patch("asyncio.get_event_loop", return_value=loop):
        await d._send("subject", "body")  # must not raise


@pytest.mark.asyncio
async def test_sms_failure_does_not_propagate() -> None:
    """If Twilio delivery fails, the error is swallowed."""
    d = NotificationDispatcher(_Settings(sms="twilio://ACxxx:tok@+15550001111/+15550002222"))

    async def _boom(executor, fn, *args):
        raise OSError("network unreachable")

    loop = MagicMock()
    loop.run_in_executor = _boom

    with patch("asyncio.get_event_loop", return_value=loop):
        await d._send_sms("alert body")  # must not raise


# ---------------------------------------------------------------------------
# PnL sign formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trade_closed_positive_pnl_sign() -> None:
    """notify_trade_closed subject should include + sign for wins."""
    d = NotificationDispatcher(_Settings())
    subjects: list[str] = []

    original_send = d._send

    async def capture_send(subject, body):
        subjects.append(subject)

    d._send = capture_send  # type: ignore[method-assign]
    d._send_sms = AsyncMock()

    await d.notify_trade_closed(symbol="AAPL", reason="TAKE_PROFIT", exit_price="160", pnl="50.00")
    assert subjects and "+$50.00" in subjects[0]


@pytest.mark.asyncio
async def test_trade_closed_negative_pnl_sign() -> None:
    """notify_trade_closed subject should not include + sign for losses."""
    d = NotificationDispatcher(_Settings())
    subjects: list[str] = []

    async def capture_send(subject, body):
        subjects.append(subject)

    d._send = capture_send  # type: ignore[method-assign]
    d._send_sms = AsyncMock()

    await d.notify_trade_closed(symbol="AAPL", reason="STOP_LOSS", exit_price="145", pnl="-25.00")
    assert subjects and "$-25.00" in subjects[0]
