"""
Unit tests for the startup security audit (18.4).

Verifies that _run_security_audit() emits expected warnings for known
misconfigurations and passes cleanly when correctly configured.
"""

from __future__ import annotations

import logging
from unittest.mock import patch, call

import pytest

from app.main import _run_security_audit


class _Cfg:
    """Minimal fake settings object."""

    def __init__(
        self,
        environment: str = "development",
        secret_key: str = "a" * 64,
        api_host: str = "127.0.0.1",
        notification_email_smtp: str = "",
        broker: str = "mock",
    ) -> None:
        self.environment = environment
        self.secret_key = secret_key
        self.api_host = api_host
        self.notification_email_smtp = notification_email_smtp
        self.broker = broker

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


def test_clean_production_config_passes_all_checks() -> None:
    """A correctly configured production setup should emit no warnings."""
    cfg = _Cfg(
        environment="production",
        secret_key="a" * 64,
        api_host="127.0.0.1",
        notification_email_smtp="smtps://u:p@mail.example.com:465/me@example.com",
        broker="ibkr",
    )
    with patch("app.main.system_logger") as mock_log:
        _run_security_audit(cfg)
        # Only the "all checks passed" info call, no warnings
        warning_calls = [c for c in mock_log.warning.call_args_list]
        assert len(warning_calls) == 0
        mock_log.info.assert_called_once()
        assert "passed" in mock_log.info.call_args[0][0]


def test_empty_secret_key_triggers_warning() -> None:
    cfg = _Cfg(secret_key="")
    with patch("app.main.system_logger") as mock_log:
        _run_security_audit(cfg)
        assert mock_log.warning.called
        warning_text = mock_log.warning.call_args[0][0]
        assert "SECRET_KEY" in warning_text


def test_short_secret_key_triggers_warning() -> None:
    cfg = _Cfg(secret_key="short")
    with patch("app.main.system_logger") as mock_log:
        _run_security_audit(cfg)
        assert mock_log.warning.called


def test_known_weak_secret_key_triggers_warning() -> None:
    for weak in ("secret", "changeme", "change-me", "dev"):
        cfg = _Cfg(secret_key=weak)
        with patch("app.main.system_logger") as mock_log:
            _run_security_audit(cfg)
            assert mock_log.warning.called, f"Expected warning for weak key: {weak!r}"


def test_non_localhost_api_host_warns_in_production() -> None:
    cfg = _Cfg(environment="production", api_host="0.0.0.0", broker="ibkr",
               notification_email_smtp="smtps://u:p@h:465/me@x.com")
    with patch("app.main.system_logger") as mock_log:
        _run_security_audit(cfg)
        all_warning_text = " ".join(str(c) for c in mock_log.warning.call_args_list)
        assert "API_HOST" in all_warning_text


def test_non_localhost_api_host_ok_in_development() -> None:
    """Non-localhost binding in development should not warn."""
    cfg = _Cfg(environment="development", api_host="0.0.0.0")
    with patch("app.main.system_logger") as mock_log:
        _run_security_audit(cfg)
        warning_text = " ".join(str(c) for c in mock_log.warning.call_args_list)
        assert "API_HOST" not in warning_text


def test_plain_smtp_warns_in_production() -> None:
    cfg = _Cfg(
        environment="production",
        api_host="127.0.0.1",
        broker="ibkr",
        notification_email_smtp="smtp://u:p@mail.example.com:587/me@x.com",
    )
    with patch("app.main.system_logger") as mock_log:
        _run_security_audit(cfg)
        warning_text = " ".join(str(c) for c in mock_log.warning.call_args_list)
        assert "smtp://" in warning_text or "TLS" in warning_text or "smtps" in warning_text


def test_plain_smtp_ok_in_development() -> None:
    """Plain SMTP in development should not warn."""
    cfg = _Cfg(
        environment="development",
        notification_email_smtp="smtp://u:p@mail.example.com:587/me@x.com",
    )
    with patch("app.main.system_logger") as mock_log:
        _run_security_audit(cfg)
        warning_text = " ".join(str(c) for c in mock_log.warning.call_args_list)
        # smtp warning is production-only; secret_key warning may still appear
        assert "smtps" not in warning_text or "SECRET" in warning_text


def test_mock_broker_warns_in_production() -> None:
    cfg = _Cfg(
        environment="production",
        api_host="127.0.0.1",
        broker="mock",
        notification_email_smtp="smtps://u:p@h:465/me@x.com",
    )
    with patch("app.main.system_logger") as mock_log:
        _run_security_audit(cfg)
        warning_text = " ".join(str(c) for c in mock_log.warning.call_args_list)
        assert "mock" in warning_text.lower() or "BROKER" in warning_text
