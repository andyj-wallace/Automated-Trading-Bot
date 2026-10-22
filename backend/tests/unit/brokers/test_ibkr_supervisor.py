"""IBKRClient connection supervisor and paper-account guard (B2)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.brokers.ibkr.client import IBKRClient, LiveTradingGuardError
from app.config import Settings


def _settings(**overrides) -> Settings:
    defaults = {
        "ibkr_host": "127.0.0.1",
        "ibkr_port": 4002,
        "ibkr_trading_mode": "paper",
        "environment": "development",
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "redis_url": "redis://localhost:6379/0",
        "broker": "mock",
    }
    defaults.update(overrides)
    return Settings(**defaults)


class FakeIB:
    def __init__(self, fail_times=0, accounts=("DU123456",)):
        self.connected = False
        self.fail_times = fail_times
        self.accounts = list(accounts)
        self.connect_calls = 0
        self.req_mkt = []
        self.disconnect_calls = 0
        self.market_data_type = None

    def isConnected(self):
        return self.connected

    async def connectAsync(self, *a, **kw):
        self.connect_calls += 1
        if self.connect_calls <= self.fail_times:
            raise ConnectionRefusedError("down")
        self.connected = True

    def managedAccounts(self):
        return self.accounts

    def reqMarketDataType(self, t):
        self.market_data_type = t

    def reqMktData(self, contract):
        self.req_mkt.append(contract.symbol)

    def disconnect(self):
        self.connected = False
        self.disconnect_calls += 1


def _client(ib, cache=None, **kw) -> IBKRClient:
    client = IBKRClient(
        _settings(), cache, backoff_seconds=(0.01,), alert_after_seconds=0.05, check_seconds=0.01, **kw
    )
    client._ib = ib
    return client


async def _until(pred, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        assert loop.time() < end, "condition not met"
        await asyncio.sleep(0.01)


async def test_initial_failure_then_supervisor_connects_and_resubscribes():
    ib = FakeIB(fail_times=1)
    client = _client(ib)
    client._subscribed_contracts = {
        "AAPL": SimpleNamespace(symbol="AAPL"),
        "MSFT": SimpleNamespace(symbol="MSFT"),
    }
    await client.connect()
    assert not ib.connected
    await _until(lambda: ib.connected)
    assert sorted(ib.req_mkt) == ["AAPL", "MSFT"]
    assert ib.market_data_type == client._settings.ibkr_market_data_type
    await client.disconnect()


async def test_reconnect_after_drop_resubscribes_and_calls_listener():
    ib = FakeIB()
    client = _client(ib)
    client._subscribed_contracts = {"AAPL": SimpleNamespace(symbol="AAPL")}
    listener = AsyncMock()
    client.add_reconnect_listener(listener)
    await client.connect()
    assert ib.req_mkt == ["AAPL"]
    ib.connected = False
    await _until(lambda: listener.await_count == 1)
    assert ib.req_mkt == ["AAPL", "AAPL"]
    await client.disconnect()


async def test_non_paper_account_raises_and_disconnects():
    ib = FakeIB(accounts=("U1234567",))
    client = _client(ib)
    with pytest.raises(LiveTradingGuardError):
        await client.connect()
    assert ib.disconnect_calls == 1
    assert client._supervisor_task is None


async def test_supervisor_stops_on_account_mismatch_and_alerts():
    ib = FakeIB()
    cache = MagicMock()
    cache.publish = AsyncMock()
    client = _client(ib, cache)
    await client.connect()
    ib.accounts = ["U1"]
    ib.connected = False
    await _until(lambda: cache.publish.await_count == 1)
    payload = json.loads(cache.publish.call_args.args[1])
    assert payload["payload"]["alert_type"] == "BROKER_ACCOUNT_MODE_MISMATCH"
    await _until(lambda: client._supervisor_task.done())
    await client.disconnect()


async def test_alert_published_once_after_threshold_then_restored():
    ib = FakeIB(fail_times=10_000)
    cache = MagicMock()
    cache.publish = AsyncMock()
    client = _client(ib, cache)
    await client.connect()
    await _until(lambda: cache.publish.await_count >= 1)
    await asyncio.sleep(0.15)
    types = [json.loads(c.args[1])["payload"]["alert_type"] for c in cache.publish.call_args_list]
    assert types == ["BROKER_DISCONNECTED"]
    ib.fail_times = 0
    await _until(lambda: ib.connected)
    await _until(lambda: cache.publish.await_count == 2)
    types = [json.loads(c.args[1])["payload"]["alert_type"] for c in cache.publish.call_args_list]
    assert types == ["BROKER_DISCONNECTED", "BROKER_RECONNECTED"]
    await client.disconnect()


async def test_disconnect_stops_supervisor():
    ib = FakeIB()
    client = _client(ib)
    await client.connect()
    task = client._supervisor_task
    assert task is not None and not task.done()
    await client.disconnect()
    assert task.done()
    assert client._supervisor_task is None


async def test_connection_info_tracks_outage_and_recovery():
    ib = FakeIB(fail_times=10_000)
    cache = MagicMock()
    cache.publish = AsyncMock()
    client = _client(ib, cache)
    await client.connect()
    info = client.connection_info()
    assert info.state == "reconnecting"
    assert info.down_since is not None and info.last_error == "down" and info.hint
    await _until(lambda: cache.publish.await_count >= 1)
    assert client.connection_info().state == "down"
    payload = json.loads(cache.publish.call_args.args[1])["payload"]
    assert payload["alert_type"] == "BROKER_DISCONNECTED"
    assert payload["hint"] and payload["hint"] in payload["message"]
    ib.fail_times = 0
    await _until(lambda: ib.connected)
    info = client.connection_info()
    assert info.state == "connected"
    assert info.last_connected_at is not None and info.down_since is None
    await client.disconnect()


async def test_connection_info_reports_account_mismatch():
    client = _client(FakeIB(accounts=("U1234567",)))
    with pytest.raises(LiveTradingGuardError):
        await client.connect()
    assert client.connection_info().state == "account_mismatch"
