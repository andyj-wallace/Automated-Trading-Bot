"""
PositionMonitor — watches live prices against open trade stop/target levels.

Subscribes to the Redis `price:*` pattern for all active tickers and to
`trade_events` for real-time updates to the open-trade set. When a price
crosses a stop-loss or take-profit level it calls OrderManager.close_position()
to trigger the exit flow.

Runs as a background asyncio task, started on app startup (Layer 6B.9).

Depends on: RedisCache (Layer 5.1), TradeRepo (Layer 3.7), OrderManager (6B.8)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.data.cache import RedisCache
from app.db.models.trade import Trade, TradeStatus
from app.monitoring.logger import system_logger, trading_logger

TRADE_EVENTS_CHANNEL = "trade_events"
PRICE_PATTERN = "price:*"


@dataclass
class _TrackedTrade:
    """Lightweight snapshot of the fields needed for level monitoring."""

    trade_id: uuid.UUID
    symbol: str
    stop_loss_price: Decimal
    take_profit_price: Decimal


class PositionMonitor:
    """
    Monitors open trade levels against live Redis price updates.

    Usage:
        monitor = PositionMonitor(cache, session_factory, order_manager)
        await monitor.start()           # begins background task
        ...
        await monitor.stop()            # graceful shutdown
    """

    def __init__(
        self,
        cache: RedisCache,
        session_factory,
        order_manager,  # OrderManager — imported lazily to avoid circular imports
    ) -> None:
        self._cache = cache
        self._session_factory = session_factory
        self._order_manager = order_manager
        self._tracked: dict[uuid.UUID, _TrackedTrade] = {}
        self._closing: set[uuid.UUID] = set()  # prevent double-close
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Load open trades from DB and start the background monitoring loop."""
        try:
            await self._load_open_trades()
        except Exception as exc:
            # DB may be temporarily unavailable at startup (e.g. migration pending).
            # Start with an empty watchlist — PositionMonitor picks up new trades
            # via trade_events as they are opened.
            system_logger.warning(
                "PositionMonitor: could not pre-load open trades from DB",
                extra={"error": str(exc)},
            )
        self._running = True
        self._task = asyncio.create_task(self._run(), name="position_monitor")
        system_logger.info(
            "PositionMonitor started",
            extra={"tracked_trades": len(self._tracked)},
        )

    async def stop(self) -> None:
        """Signal the background loop to stop and wait for it to exit."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        system_logger.info("PositionMonitor stopped")

    # ------------------------------------------------------------------
    # Internal: initial load
    # ------------------------------------------------------------------

    async def _load_open_trades(self) -> None:
        """Populate _tracked from the DB on startup."""
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            repo = TradeRepo(session)
            open_trades: list[Trade] = await repo.get_open_trades()

        for trade in open_trades:
            self._track(trade)

    def _track(self, trade: Trade) -> None:
        self._tracked[trade.id] = _TrackedTrade(
            trade_id=trade.id,
            symbol=trade.symbol,
            stop_loss_price=trade.stop_loss_price,
            take_profit_price=trade.take_profit_price,
        )

    # ------------------------------------------------------------------
    # Internal: background run loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Subscribe to price updates and trade events; process until stopped."""
        try:
            async for msg in self._cache.subscribe_many(
                channels=[TRADE_EVENTS_CHANNEL],
                patterns=[PRICE_PATTERN],
            ):
                if not self._running:
                    break
                try:
                    if msg["type"] == "pmessage":
                        await self._handle_price(msg)
                    elif msg["type"] == "message":
                        await self._handle_trade_event(msg)
                except Exception as exc:
                    system_logger.error(
                        "PositionMonitor: error processing message",
                        extra={"error": str(exc), "msg_type": msg.get("type")},
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            system_logger.error(
                "PositionMonitor: unexpected loop error",
                extra={"error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Internal: price message handler
    # ------------------------------------------------------------------

    async def _handle_price(self, msg: dict) -> None:
        """
        Check a price update against all open trades for that ticker.

        msg["channel"] looks like b"price:AAPL" or "price:AAPL".
        """
        channel = msg["channel"]
        if isinstance(channel, bytes):
            channel = channel.decode()

        # channel format: "price:TICKER"
        parts = channel.split(":", 1)
        if len(parts) != 2:
            return
        ticker = parts[1].upper()

        try:
            payload = json.loads(msg["data"])
            price = Decimal(str(payload.get("price", payload)))
        except Exception:
            # Price data might be a plain decimal string or JSON object
            try:
                price = Decimal(str(msg["data"]))
            except Exception:
                return

        # Check all tracked trades for this ticker
        hits: list[tuple[uuid.UUID, str]] = []
        for tracked in list(self._tracked.values()):
            if tracked.symbol.upper() != ticker:
                continue
            if tracked.trade_id in self._closing:
                continue

            if price <= tracked.stop_loss_price:
                hits.append((tracked.trade_id, "STOP_LOSS"))
            elif price >= tracked.take_profit_price:
                hits.append((tracked.trade_id, "TAKE_PROFIT"))

        for trade_id, reason in hits:
            if trade_id in self._closing:
                continue  # another hit raced us to it
            self._closing.add(trade_id)
            self._tracked.pop(trade_id, None)
            trading_logger.info(
                "PositionMonitor: level hit — closing position",
                extra={
                    "trade_id": str(trade_id),
                    "ticker": ticker,
                    "price": str(price),
                    "reason": reason,
                },
            )
            asyncio.create_task(self._close(trade_id, reason))

    async def _close(self, trade_id: uuid.UUID, reason: str) -> None:
        """Fire close_position and clean up the in-flight set."""
        try:
            await self._order_manager.close_position(trade_id, reason)
        except Exception as exc:
            system_logger.error(
                "PositionMonitor: close_position failed",
                extra={"trade_id": str(trade_id), "reason": reason, "error": str(exc)},
            )
        finally:
            self._closing.discard(trade_id)

    # ------------------------------------------------------------------
    # Internal: trade event handler
    # ------------------------------------------------------------------

    async def _handle_trade_event(self, msg: dict) -> None:
        """
        Update the tracked set when trades open or close.

        Expected events published on trade_events channel:
            trade_executed — add to tracked set
            trade_closed   — remove from tracked set (if not already)
        """
        try:
            event = json.loads(msg["data"])
        except Exception:
            return

        event_type = event.get("event")
        payload = event.get("payload", {})

        if event_type == "trade_executed":
            trade_id_str = payload.get("trade_id")
            symbol = payload.get("symbol", "")
            stop_str = payload.get("stop_loss_price")
            tp_str = payload.get("take_profit_price")
            if trade_id_str and stop_str and tp_str:
                try:
                    self._tracked[uuid.UUID(trade_id_str)] = _TrackedTrade(
                        trade_id=uuid.UUID(trade_id_str),
                        symbol=symbol,
                        stop_loss_price=Decimal(stop_str),
                        take_profit_price=Decimal(tp_str),
                    )
                except Exception:
                    pass

        elif event_type == "trade_closed":
            trade_id_str = payload.get("trade_id")
            if trade_id_str:
                try:
                    self._tracked.pop(uuid.UUID(trade_id_str), None)
                except Exception:
                    pass
