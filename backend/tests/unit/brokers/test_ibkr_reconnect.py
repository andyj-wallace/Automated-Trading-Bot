"""
Unit tests for IBKRClient reconnection logic (task 17.1).

Covers:
  - Exponential backoff delays (5 attempts: 5, 10, 20, 40, 80)
  - Each retry attempt logged at WARNING
  - Successful reconnect on attempt N stops the loop
  - On exhaustion: error_logger.critical fired
  - On exhaustion with cache: system_alert published to "system_alerts"
  - On exhaustion without cache: no publish attempted
  - disconnect() mid-loop stops retrying without firing exhaustion alert
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.brokers.ibkr.client import IBKRClient
from app.config import Settings


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_settings(**overrides) -> Settings:
    defaults = {
        "ibkr_host": "127.0.0.1",
        "ibkr_port": 4001,
        "ibkr_client_id": 1,
        "ibkr_trading_mode": "paper",
        "environment": "development",
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "redis_url": "redis://localhost:6379/0",
        "log_level": "INFO",
        "broker": "mock",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_client(cache=None) -> IBKRClient:
    settings = _make_settings()
    client = IBKRClient.__new__(IBKRClient)
    client._settings = settings
    client._cache = cache
    client._reconnecting = False
    # Minimal stub for _ib
    ib = MagicMock()
    ib.isConnected.return_value = False
    client._ib = ib
    return client


# ---------------------------------------------------------------------------
# Reconnect delay contract
# ---------------------------------------------------------------------------

class TestReconnectDelays:
    def test_has_five_attempts(self):
        assert len(IBKRClient._RECONNECT_DELAYS) == 5

    def test_delays_are_exponential(self):
        delays = IBKRClient._RECONNECT_DELAYS
        # Each delay should be approximately double the previous
        for i in range(1, len(delays)):
            assert delays[i] == delays[i - 1] * 2, (
                f"Expected delay[{i}]={delays[i - 1] * 2}, got {delays[i]}"
            )

    def test_first_delay_is_five_seconds(self):
        assert IBKRClient._RECONNECT_DELAYS[0] == 5


# ---------------------------------------------------------------------------
# Retry logging at WARNING
# ---------------------------------------------------------------------------

class TestReconnectLogging:
    @pytest.mark.asyncio
    async def test_each_retry_logged_at_warning(self):
        client = _make_client()
        # All connect attempts fail
        client._ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))

        with (
            patch("app.brokers.ibkr.client.system_logger") as mock_sys,
            patch("app.brokers.ibkr.client.error_logger"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await client._reconnect_loop()

        # 5 attempts → 5 "attempt N/5 in Xs" warnings
        warning_calls = [c for c in mock_sys.warning.call_args_list]
        # Expect at least 5 warning calls (attempt announcements); some may be fail messages too
        attempt_warnings = [
            c for c in warning_calls
            if "reconnect attempt" in str(c.args[0]) and "in" in str(c.args[0])
        ]
        assert len(attempt_warnings) == 5

    @pytest.mark.asyncio
    async def test_retry_attempt_numbers_are_correct(self):
        client = _make_client()
        client._ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))

        attempt_numbers = []

        def capture_warning(msg, *args, **kwargs):
            if "reconnect attempt" in str(msg) and "in" in str(msg):
                # args are (attempt, total, delay)
                attempt_numbers.append(args[0])

        with (
            patch("app.brokers.ibkr.client.system_logger") as mock_sys,
            patch("app.brokers.ibkr.client.error_logger"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_sys.warning.side_effect = capture_warning
            await client._reconnect_loop()

        assert attempt_numbers == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# Successful reconnect
# ---------------------------------------------------------------------------

class TestReconnectSuccess:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt_stops_loop(self):
        client = _make_client()
        client._ib.connectAsync = AsyncMock(return_value=None)

        with (
            patch("app.brokers.ibkr.client.system_logger"),
            patch("app.brokers.ibkr.client.error_logger") as mock_err,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await client._reconnect_loop()

        assert client._reconnecting is False
        mock_err.critical.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_on_third_attempt_stops_loop(self):
        client = _make_client()
        call_count = 0

        async def connect_side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionRefusedError("down")

        client._ib.connectAsync = AsyncMock(side_effect=connect_side_effect)

        with (
            patch("app.brokers.ibkr.client.system_logger"),
            patch("app.brokers.ibkr.client.error_logger") as mock_err,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await client._reconnect_loop()

        assert call_count == 3
        assert client._reconnecting is False
        mock_err.critical.assert_not_called()


# ---------------------------------------------------------------------------
# Exhaustion: CRITICAL log
# ---------------------------------------------------------------------------

class TestReconnectExhaustion:
    @pytest.mark.asyncio
    async def test_exhaustion_logs_critical(self):
        client = _make_client()
        client._ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))

        with (
            patch("app.brokers.ibkr.client.system_logger"),
            patch("app.brokers.ibkr.client.error_logger") as mock_err,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await client._reconnect_loop()

        mock_err.critical.assert_called_once()
        call_args = mock_err.critical.call_args
        # Message should mention attempts exhausted
        assert "exhausted" in call_args.args[0].lower() or "exhausted" in str(call_args)

    @pytest.mark.asyncio
    async def test_exhaustion_sets_reconnecting_false(self):
        client = _make_client()
        client._ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))

        with (
            patch("app.brokers.ibkr.client.system_logger"),
            patch("app.brokers.ibkr.client.error_logger"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await client._reconnect_loop()

        assert client._reconnecting is False


# ---------------------------------------------------------------------------
# Exhaustion: system_alert Redis publish
# ---------------------------------------------------------------------------

class TestReconnectSystemAlert:
    @pytest.mark.asyncio
    async def test_exhaustion_publishes_to_system_alerts_when_cache_present(self):
        mock_cache = AsyncMock()
        mock_cache.publish = AsyncMock()
        client = _make_client(cache=mock_cache)
        client._ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))

        with (
            patch("app.brokers.ibkr.client.system_logger"),
            patch("app.brokers.ibkr.client.error_logger"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await client._reconnect_loop()

        mock_cache.publish.assert_called_once()
        channel, payload = mock_cache.publish.call_args.args
        assert channel == "system_alerts"
        event = json.loads(payload)
        assert event["event"] == "system_alert"
        assert event["payload"]["alert_type"] == "BROKER_RECONNECT_EXHAUSTED"

    @pytest.mark.asyncio
    async def test_exhaustion_without_cache_does_not_publish(self):
        client = _make_client(cache=None)
        client._ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))

        with (
            patch("app.brokers.ibkr.client.system_logger"),
            patch("app.brokers.ibkr.client.error_logger"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # Should not raise even without cache
            await client._reconnect_loop()

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_raise(self):
        mock_cache = AsyncMock()
        mock_cache.publish = AsyncMock(side_effect=Exception("Redis down"))
        client = _make_client(cache=mock_cache)
        client._ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))

        with (
            patch("app.brokers.ibkr.client.system_logger"),
            patch("app.brokers.ibkr.client.error_logger"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # Must not propagate the publish exception
            await client._reconnect_loop()

        assert client._reconnecting is False


# ---------------------------------------------------------------------------
# Intentional disconnect stops the loop
# ---------------------------------------------------------------------------

class TestReconnectCancellation:
    @pytest.mark.asyncio
    async def test_disconnect_mid_loop_stops_retrying(self):
        client = _make_client()
        sleep_call_count = 0

        async def fake_sleep(delay):
            nonlocal sleep_call_count
            sleep_call_count += 1
            # Simulate disconnect() being called after first sleep
            if sleep_call_count >= 1:
                client._reconnecting = False

        client._ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))

        with (
            patch("app.brokers.ibkr.client.system_logger"),
            patch("app.brokers.ibkr.client.error_logger") as mock_err,
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            await client._reconnect_loop()

        # Should stop after first sleep — no exhaustion alert
        mock_err.critical.assert_not_called()
        assert sleep_call_count == 1
