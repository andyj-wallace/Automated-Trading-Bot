"""
NotificationDispatcher — delivers event notifications via email (SMTP).

Supported notification types:
  - risk_alert:    WARNING or CRITICAL aggregate risk threshold crossed
  - trade_executed: a trade was filled (BUY/SELL confirmed)
  - trade_closed:  a position was closed (with P&L summary)

Configuration (via Settings):
  notification_email_smtp — SMTP URL string:
      "smtp://user:pass@host:port/recipient@example.com"
      "smtps://user:pass@smtp.gmail.com:465/you@gmail.com"

  If notification_email_smtp is empty, all notifications are logged at DEBUG
  and silently skipped (no error raised).

SMTP URL format:
  scheme://user:pass@host:port/to_address
  - scheme: smtp (STARTTLS on port 587) or smtps (SSL on port 465)
  - to_address is the path component (after the final /)

The dispatcher is fire-and-forget — it never raises. All failures are logged
to system.log at WARNING level so the trading pipeline is never blocked.

Usage:
    dispatcher = NotificationDispatcher(settings)

    # One-off:
    await dispatcher.notify_risk_alert(alert_level="CRITICAL", aggregate_risk_pct="0.045", ...)

    # From a raw event dict (14.1 / 7.2 event format):
    await dispatcher.dispatch(event)
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlparse

from app.monitoring.logger import system_logger

_log = system_logger


class NotificationDispatcher:
    """
    Async notification dispatcher with SMTP email backend.

    All public methods are fire-and-forget coroutines — they catch all
    exceptions internally and log them rather than propagating.
    """

    def __init__(self, settings) -> None:
        self._smtp_url: str = getattr(settings, "notification_email_smtp", "")
        self._enabled = bool(self._smtp_url)

        if self._enabled:
            _log.info(
                "NotificationDispatcher: SMTP notifications enabled",
                extra={"smtp_host": self._parse_smtp_host()},
            )
        else:
            _log.info(
                "NotificationDispatcher: notification_email_smtp not set — "
                "notifications will be logged only"
            )

    # ------------------------------------------------------------------
    # Public dispatch interface
    # ------------------------------------------------------------------

    async def dispatch(self, event: dict) -> None:
        """
        Route a raw event dict to the correct notification method.

        Handles the event shapes produced by RiskMonitor (14.1),
        TradeHandler (7.2), and OrderManager close_position.
        """
        event_type = event.get("event", "")
        payload = event.get("payload", {})

        if event_type == "risk_alert":
            await self.notify_risk_alert(
                alert_level=payload.get("alert_level", "UNKNOWN"),
                aggregate_risk_pct=payload.get("aggregate_risk_pct", "?"),
                aggregate_risk_amount=payload.get("aggregate_risk_amount", "?"),
                open_trade_count=payload.get("open_trade_count", 0),
                account_balance=payload.get("account_balance", "?"),
            )

        elif event_type == "trade_executed":
            await self.notify_trade_executed(
                symbol=payload.get("symbol", "?"),
                direction=payload.get("direction", "?"),
                quantity=payload.get("quantity", "?"),
                entry_price=payload.get("entry_price", "?"),
                stop_loss_price=payload.get("stop_loss_price", "?"),
                take_profit_price=payload.get("take_profit_price", "?"),
                risk_amount=payload.get("risk_amount", "?"),
            )

        elif event_type == "trade_closed":
            await self.notify_trade_closed(
                symbol=payload.get("symbol", "?"),
                reason=payload.get("reason", "?"),
                exit_price=payload.get("exit_price", "?"),
                pnl=payload.get("pnl", "?"),
            )

    async def notify_risk_alert(
        self,
        alert_level: str,
        aggregate_risk_pct: str,
        aggregate_risk_amount: str,
        open_trade_count: int,
        account_balance: str,
    ) -> None:
        """Send a risk threshold alert notification."""
        pct_display = f"{float(aggregate_risk_pct) * 100:.2f}%" if _is_numeric(aggregate_risk_pct) else aggregate_risk_pct
        subject = f"[TradingBot] {alert_level} — Portfolio risk at {pct_display}"
        body = (
            f"Risk Alert: {alert_level}\n\n"
            f"Aggregate risk: {pct_display} of account\n"
            f"Risk amount:    ${aggregate_risk_amount}\n"
            f"Open trades:    {open_trade_count}\n"
            f"Account balance: ${account_balance}\n"
        )
        _log.warning(
            f"NotificationDispatcher: {alert_level} risk alert",
            extra={"aggregate_risk_pct": aggregate_risk_pct, "open_trade_count": open_trade_count},
        )
        await self._send(subject, body)

    async def notify_trade_executed(
        self,
        symbol: str,
        direction: str,
        quantity: str,
        entry_price: str,
        stop_loss_price: str,
        take_profit_price: str,
        risk_amount: str,
    ) -> None:
        """Send a trade fill notification."""
        subject = f"[TradingBot] Trade executed — {direction} {quantity} {symbol} @ ${entry_price}"
        body = (
            f"Trade Executed\n\n"
            f"Symbol:       {symbol}\n"
            f"Direction:    {direction}\n"
            f"Quantity:     {quantity}\n"
            f"Entry price:  ${entry_price}\n"
            f"Stop-loss:    ${stop_loss_price}\n"
            f"Take-profit:  ${take_profit_price}\n"
            f"Risk amount:  ${risk_amount}\n"
        )
        _log.info(
            "NotificationDispatcher: trade executed notification",
            extra={"symbol": symbol, "direction": direction},
        )
        await self._send(subject, body)

    async def notify_trade_closed(
        self,
        symbol: str,
        reason: str,
        exit_price: str,
        pnl: str,
    ) -> None:
        """Send a position close notification."""
        pnl_val = float(pnl) if _is_numeric(pnl) else 0.0
        pnl_sign = "+" if pnl_val >= 0 else ""
        subject = f"[TradingBot] Position closed — {symbol} | P&L {pnl_sign}${pnl_val:.2f}"
        body = (
            f"Position Closed\n\n"
            f"Symbol:     {symbol}\n"
            f"Exit reason: {reason}\n"
            f"Exit price:  ${exit_price}\n"
            f"P&L:         {pnl_sign}${pnl_val:.2f}\n"
        )
        _log.info(
            "NotificationDispatcher: trade closed notification",
            extra={"symbol": symbol, "reason": reason, "pnl": pnl},
        )
        await self._send(subject, body)

    # ------------------------------------------------------------------
    # Internal: SMTP delivery
    # ------------------------------------------------------------------

    async def _send(self, subject: str, body: str) -> None:
        """
        Send an email. Runs the blocking smtplib call in a thread executor
        so it never blocks the event loop.

        Silently no-ops when SMTP is not configured.
        """
        if not self._enabled:
            return
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self._send_sync, subject, body
            )
        except Exception as exc:
            _log.warning(
                "NotificationDispatcher: email delivery failed",
                extra={"subject": subject, "error": str(exc)},
            )

    def _send_sync(self, subject: str, body: str) -> None:
        """Blocking SMTP send — called from thread executor."""
        parsed = urlparse(self._smtp_url)
        host = parsed.hostname or ""
        port = parsed.port or (465 if parsed.scheme == "smtps" else 587)
        user = parsed.username or ""
        password = parsed.password or ""
        to_addr = parsed.path.lstrip("/")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = user or f"tradingbot@{host}"
        msg["To"] = to_addr
        msg.set_content(body)

        if parsed.scheme == "smtps":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx) as server:
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as server:
                server.ehlo()
                server.starttls()
                if user and password:
                    server.login(user, password)
                server.send_message(msg)

    def _parse_smtp_host(self) -> str:
        try:
            return urlparse(self._smtp_url).hostname or "?"
        except Exception:
            return "?"


def _is_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False
