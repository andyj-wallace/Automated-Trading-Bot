"""IBKRClient tick sanitizing and callback hygiene (B1)."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.brokers.ibkr.client import IBKRClient
from app.config import Settings


def _client() -> IBKRClient:
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        broker="mock",
        environment="development",
    )
    return IBKRClient(settings)


def _ticker(last, close=100.0, bid=None, ask=None, symbol="AAPL"):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol), last=last, close=close, bid=bid, ask=ask
    )


class _Event:
    def __iadd__(self, other):
        return self

    def __isub__(self, other):
        return self


class _Recorder:
    def __init__(self) -> None:
        self.updates = []

    async def __call__(self, update) -> None:
        self.updates.append(update)


async def _drain(client: IBKRClient) -> None:
    await asyncio.gather(*list(client._tasks))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, 0.0, None])
async def test_invalid_last_is_dropped(bad):
    client, rec = _client(), _Recorder()
    client._price_callbacks.append(rec)
    client._on_pending_tickers([_ticker(bad, close=99.0)])
    assert not client._tasks
    assert rec.updates == []


async def test_valid_tick_emits_once_even_if_subscribed_thrice():
    client, rec = _client(), _Recorder()
    client._ib = SimpleNamespace(
        isConnected=lambda: True,
        reqMktData=lambda c: None,
        pendingTickersEvent=_Event(),
    )
    for _ in range(3):
        await client.subscribe_price_feed(["AAPL"], rec)
    client._on_pending_tickers([_ticker(101.5, close=100.0, bid=float("nan"))])
    await _drain(client)
    assert len(rec.updates) == 1
    update = rec.updates[0]
    assert update.price == Decimal("101.5")
    assert update.bid is None
    assert update.day_change == Decimal("1.5")
    assert update.day_change_pct == Decimal("0.015")


async def test_close_is_never_used_as_price():
    client, rec = _client(), _Recorder()
    client._price_callbacks.append(rec)
    client._on_pending_tickers([_ticker(None, close=100.0)])
    assert rec.updates == [] and not client._tasks


async def test_unchanged_price_suppressed_then_reemitted_after_5s():
    client, rec = _client(), _Recorder()
    client._price_callbacks.append(rec)
    client._on_pending_tickers([_ticker(50.0)])
    client._on_pending_tickers([_ticker(50.0)])
    await _drain(client)
    assert len(rec.updates) == 1
    price, ts = client._last_emitted["AAPL"]
    client._last_emitted["AAPL"] = (price, ts - timedelta(seconds=6))
    client._on_pending_tickers([_ticker(50.0)])
    await _drain(client)
    assert len(rec.updates) == 2
    client._on_pending_tickers([_ticker(50.5)])
    await _drain(client)
    assert len(rec.updates) == 3
    assert isinstance(client._last_emitted["AAPL"][1], datetime)
    assert client._last_emitted["AAPL"][1].tzinfo is UTC
