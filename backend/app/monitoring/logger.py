"""
Structured JSON logging for the trading bot.

Four rotating log streams: trading, risk, system, error (10 MB / 5 backups each).
One append-only audit log: audit (no rotation — permanent record).

Usage:
    from app.monitoring.logger import setup_logging, trading_logger, risk_logger, ...

    # Call once at app startup:
    setup_logging(log_level="INFO")

    # Then use anywhere:
    trading_logger.info("Order submitted", extra={"trade_id": "...", "symbol": "AAPL"})
    audit_logger.info("PRE_SUBMISSION", extra={"trade_id": "...", "symbol": "AAPL", ...})
"""

import json
import logging
import logging.handlers
import os
import re
import traceback
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIGURED = False

# Fields whose values are masked in all log output, regardless of depth.
_SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "account_number",
        "account_id",
        "ibkr_username",
        "ibkr_password",
        "secret_key",
        "password",
        "api_key",
        "token",
        "access_token",
        "refresh_token",
        "vnc_password",
    }
)

# Standard LogRecord attributes that are NOT copied into the "context" dict.
_STANDARD_LOG_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    }
)

# Regex patterns for masking sensitive values embedded in message strings.
# Order matters: more specific patterns first.
_SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Long random alphanumeric strings (32+ chars) — API keys / tokens
    (re.compile(r"\b[A-Za-z0-9]{32,}\b"), "***REDACTED***"),
    # Account-number-shaped sequences (8–15 consecutive digits)
    (re.compile(r"\b\d{8,15}\b"), "***REDACTED***"),
]

_REDACTED = "***REDACTED***"

# ---------------------------------------------------------------------------
# JSON Formatter
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Serialises each log record to a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        # Let the base class populate record.message and record.exc_text
        record.message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)

        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()

        context: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_FIELDS and not key.startswith("_"):
                context[key] = value

        log_entry: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }

        if context:
            log_entry["context"] = context

        if record.exc_text:
            log_entry["exception"] = record.exc_text

        return json.dumps(log_entry, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Sensitive-field Masking Filter
# ---------------------------------------------------------------------------


class MaskingFilter(logging.Filter):
    """
    Masks known sensitive field names in the LogRecord's extra attributes
    and applies regex-based masking to the formatted message.

    Attached to handlers (not loggers) so it runs just before the record
    is serialised to text.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # Mask sensitive fields in extra attributes
        for field in _SENSITIVE_FIELD_NAMES:
            if hasattr(record, field):
                setattr(record, field, _REDACTED)

        # Mask sensitive patterns in the message string
        msg = record.getMessage()
        for pattern, replacement in _SENSITIVE_PATTERNS:
            msg = pattern.sub(replacement, msg)
        record.msg = msg
        record.args = ()

        return True


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------


def _make_logger(
    name: str,
    log_path: str,
    level: int,
    rotating: bool = True,
) -> logging.Logger:
    """
    Build and return a configured logger.

    Args:
        name: Logger name (e.g. "trading").
        log_path: Absolute path to the log file.
        level: Logging level integer.
        rotating: If True, use RotatingFileHandler; if False, use plain FileHandler.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # Don't propagate to root logger — avoids double-logging
    logger.propagate = False

    formatter = JsonFormatter()
    masking_filter = MaskingFilter()

    # File handler
    if rotating:
        file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
    else:
        # Append-only, no rotation — for audit log
        file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")

    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(masking_filter)
    logger.addHandler(file_handler)

    # Console handler (always on — useful in development and in Docker logs)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(masking_filter)
    logger.addHandler(stream_handler)

    return logger


# ---------------------------------------------------------------------------
# Module-level loggers (populated by setup_logging)
# ---------------------------------------------------------------------------

trading_logger: logging.Logger = logging.getLogger("trading")
risk_logger: logging.Logger = logging.getLogger("risk")
system_logger: logging.Logger = logging.getLogger("system")
error_logger: logging.Logger = logging.getLogger("error")
audit_logger: logging.Logger = logging.getLogger("audit")


# ---------------------------------------------------------------------------
# Public setup function
# ---------------------------------------------------------------------------


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> None:
    """
    Configure all application loggers.

    Idempotent — safe to call more than once (subsequent calls are no-ops).

    Args:
        log_level: Minimum log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_dir: Directory where log files are written. Created if it doesn't exist.
    """
    global _CONFIGURED, trading_logger, risk_logger, system_logger, error_logger, audit_logger

    if _CONFIGURED:
        return

    os.makedirs(log_dir, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)

    trading_logger = _make_logger(
        "trading",
        os.path.join(log_dir, "trading.log"),
        level,
        rotating=True,
    )
    risk_logger = _make_logger(
        "risk",
        os.path.join(log_dir, "risk.log"),
        level,
        rotating=True,
    )
    system_logger = _make_logger(
        "system",
        os.path.join(log_dir, "system.log"),
        level,
        rotating=True,
    )
    error_logger = _make_logger(
        "error",
        os.path.join(log_dir, "error.log"),
        logging.ERROR,  # error log always captures ERROR and above
        rotating=True,
    )
    audit_logger = _make_logger(
        "audit",
        os.path.join(log_dir, "audit.log"),
        logging.INFO,
        rotating=False,  # append-only — never rotated
    )

    _CONFIGURED = True
