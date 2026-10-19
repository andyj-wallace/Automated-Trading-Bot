"""Background-task helpers: referenced tasks, periodic loops, supervised subscribers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from typing import Any

from app.monitoring.logger import system_logger


def spawn(
    coro: Coroutine[Any, Any, Any], *, name: str, registry: set[asyncio.Task]
) -> asyncio.Task:
    """Create a task and keep a strong reference until it finishes."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    registry.add(task)
    task.add_done_callback(registry.discard)
    return task


class PeriodicTask:
    """Run an async function every interval; log and survive its exceptions."""

    def __init__(
        self,
        name: str,
        interval_seconds: float,
        fn: Callable[[], Awaitable[None]],
        *,
        initial_delay: float = 0.0,
    ) -> None:
        self._name, self._interval, self._fn, self._initial_delay = (
            name,
            interval_seconds,
            fn,
            initial_delay,
        )
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop(), name=self._name)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        if self._initial_delay:
            await asyncio.sleep(self._initial_delay)
        while True:
            try:
                await self._fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                system_logger.exception("Periodic task failed", extra={"task": self._name})
            await asyncio.sleep(self._interval)


async def supervised_subscribe(
    cache,
    channels: list[str],
    handler: Callable[[dict], Awaitable[None]],
    *,
    name: str,
) -> None:
    """Consume Redis pub/sub forever, reconnecting with backoff on connection errors."""
    backoff = 1.0
    while True:
        try:
            async for msg in cache.subscribe_many(channels=channels, patterns=[]):
                backoff = 1.0
                try:
                    await handler(msg)
                except Exception:
                    system_logger.exception("Subscriber handler failed", extra={"subscriber": name})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            system_logger.warning(
                "Subscriber lost Redis connection, retrying",
                extra={"subscriber": name, "error": str(exc), "retry_in_seconds": backoff},
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
