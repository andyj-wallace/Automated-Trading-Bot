"""Event contract: Redis channels, message envelope, system alerts."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.monitoring.logger import error_logger, system_logger

TRADE_EVENTS = "trade_events"
RISK_UPDATES = "risk_updates"
SYSTEM_ALERTS = "system_alerts"
WATCHLIST_PRICES = "watchlist_prices"


def envelope(event: str, payload: dict) -> str:
    """Serialize an event as {"event", "payload", "timestamp"} JSON."""
    return json.dumps(
        {"event": event, "payload": payload, "timestamp": datetime.now(UTC).isoformat()},
        default=str,
    )


async def publish_system_alert(
    cache, alert_type: str, message: str, *, severity: str = "CRITICAL", **details
) -> None:
    """Log and publish a system_alert; never raises."""
    log = error_logger.error if severity == "CRITICAL" else system_logger.warning
    log(
        "SYSTEM ALERT",
        extra={"alert_type": alert_type, "alert_message": message, "severity": severity, **details},
    )
    if cache is None:
        return
    try:
        payload = {"alert_type": alert_type, "message": message, "severity": severity, **details}
        await cache.publish(SYSTEM_ALERTS, envelope("system_alert", payload))
    except Exception as exc:
        system_logger.warning(
            "publish_system_alert failed", extra={"alert_type": alert_type, "error": str(exc)}
        )
