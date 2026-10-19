import asyncio
import logging

import pytest

from app.core.events import envelope, publish_system_alert
from app.core.tasks import PeriodicTask, spawn, supervised_subscribe
from app.notifications.dispatcher import NotificationDispatcher


async def test_periodic_task_survives_exception_and_stops():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")

    pt = PeriodicTask("t", 0.01, fn)
    pt.start()
    await asyncio.sleep(0.1)
    await pt.stop()
    assert calls >= 2
    after = calls
    await asyncio.sleep(0.05)
    assert calls == after


async def test_spawn_registry_empties():
    registry: set[asyncio.Task] = set()

    async def work():
        return 1

    task = spawn(work(), name="w", registry=registry)
    assert task in registry
    await task
    await asyncio.sleep(0)
    assert not registry


class _FlakyCache:
    def __init__(self):
        self.calls = 0

    async def subscribe_many(self, channels, patterns):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("down")
        yield {"data": "hello"}
        await asyncio.Event().wait()


async def test_supervised_subscribe_reconnects(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(d):
        await real_sleep(0)

    monkeypatch.setattr("app.core.tasks.asyncio.sleep", fast_sleep)
    got: list[dict] = []

    async def handler(msg):
        got.append(msg)

    cache = _FlakyCache()
    task = asyncio.create_task(supervised_subscribe(cache, ["c"], handler, name="t"))
    for _ in range(50):
        if got:
            break
        await real_sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cache.calls == 2
    assert got == [{"data": "hello"}]


class _Settings:
    notification_email_smtp = "smtp://u:p@mail.example.com:587/me@example.com"
    notification_sms_twilio = ""


async def test_notify_system_alert_reaches_send_with_real_logger(monkeypatch):
    sent = []
    d = NotificationDispatcher(_Settings())
    monkeypatch.setattr(d, "_send_sync", lambda subject, body: sent.append(subject))
    prev = logging.getLogger("system").level
    logging.getLogger("system").setLevel(logging.INFO)
    try:
        await d.notify_system_alert("broker_down", "gateway unreachable")
    finally:
        logging.getLogger("system").setLevel(prev)
    assert sent


class _PubCache:
    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    async def publish(self, channel, message):
        if self.fail:
            raise ConnectionError("x")
        self.published.append((channel, message))


async def test_publish_system_alert_publishes_and_never_raises():
    cache = _PubCache()
    await publish_system_alert(cache, "t", "msg", severity="WARNING", extra_detail=1)
    assert cache.published[0][0] == "system_alerts"
    assert '"message": "msg"' in cache.published[0][1]
    await publish_system_alert(_PubCache(fail=True), "t", "msg")
    await publish_system_alert(None, "t", "msg")


def test_envelope_shape():
    import json

    data = json.loads(envelope("e", {"a": 1}))
    assert data["event"] == "e" and data["payload"] == {"a": 1} and "timestamp" in data
