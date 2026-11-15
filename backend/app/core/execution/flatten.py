"""Flatten steps: pause trading, settle in-flight entries, then close every OPEN trade."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.brokers.base import BaseBroker, OrderResult
from app.db.models.trade import TradeStatus
from app.monitoring.logger import system_logger


async def flatten_positions(
    trading_state: Any,
    order_manager: Any,
    broker: BaseBroker,
    repo: Any,
    *,
    idle_timeout: float = 30.0,
    poll_interval: float = 0.25,
    poll_timeout: float = 10.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Pause trading, settle in-flight entries, then close every OPEN trade."""
    await trading_state.pause("flatten")
    results: list[dict] = []
    if not await order_manager.wait_for_entries_idle(idle_timeout):
        results.append({"outcome": "ENTRIES_STILL_IN_FLIGHT"})

    for trade in await repo.get_active_trades():
        if trade.status not in (TradeStatus.PENDING, TradeStatus.SUBMITTED):
            continue
        outcome = await _settle_entry(
            trade,
            order_manager,
            broker,
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            sleep=sleep,
            clock=clock,
        )
        results.append({"trade_id": str(trade.id), "symbol": trade.symbol, "outcome": outcome})

    for trade in await repo.get_open_trades():
        try:
            outcome = (await order_manager.close_position(trade.id, "MANUAL")).value
        except Exception as exc:
            system_logger.error(
                "Flatten: close failed", extra={"trade_id": str(trade.id), "error": str(exc)}
            )
            outcome = "FAILED"
        results.append({"trade_id": str(trade.id), "symbol": trade.symbol, "outcome": outcome})
    return results


async def _settle_entry(
    trade: Any,
    order_manager: Any,
    broker: BaseBroker,
    *,
    poll_interval: float,
    poll_timeout: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> str:
    """Cancel a working entry order, or adopt its fill if it filled first."""
    ref = f"{trade.id}:entry"
    try:
        order = await broker.find_order_by_ref(ref)
        if order is not None and order.status == "WORKING":
            cancelled = await broker.cancel_order(order.broker_order_id)
            if not cancelled:
                return "ENTRY_CANCEL_FAILED"
        if order is not None and not order.is_terminal:
            deadline = clock() + poll_timeout
            while True:
                remaining = deadline - clock()
                if remaining <= 0:
                    return "ENTRY_CANCEL_FAILED"
                await sleep(min(poll_interval, remaining))
                remaining = deadline - clock()
                if remaining <= 0:
                    return "ENTRY_CANCEL_FAILED"
                order = await asyncio.wait_for(broker.find_order_by_ref(ref), remaining)
                if order is None or order.is_terminal:
                    break
        if order is None or order.filled_quantity <= 0:
            return "ENTRY_CANCELLED"
        await order_manager.sync_fills(trade.id)
        fill: OrderResult = await broker.get_execution_summary(ref) or order
        await order_manager.adopt_entry_fill(trade.id, fill)
        return "ENTRY_ADOPTED"
    except Exception as exc:
        system_logger.error(
            "Flatten: entry settle failed",
            extra={"trade_id": str(trade.id), "error": str(exc)},
        )
        return "ENTRY_CANCEL_FAILED"
