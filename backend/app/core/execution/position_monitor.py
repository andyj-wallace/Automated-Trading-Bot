"""
PositionMonitor — watches live prices against open trade stop/target levels.

Subscribes to the Redis `price:*` pattern for all active tickers and to
`trade_events` for real-time updates to the open-trade set. When a price
crosses a stop-loss or take-profit level it calls OrderManager.close_position()
to trigger the exit flow.

Trailing / breakeven stop (long trades only):
  Once a BUY trade has moved at least `trailing_activation_r` risk-units (R)
  in its favor (1R = |entry_price − initial stop distance|), the live stop
  is ratcheted up to `trailing_distance_r` × R behind the current price and
  never loosened. At the default 1R/1R settings this means: the stop moves
  to breakeven exactly when the trade is up 1R, and keeps trailing R behind
  price from there — turning "winner that reverses" into a scratch instead
  of a full loss, without changing the original 1%-risk or R:R rules.

  This only adjusts PositionMonitor's in-memory tracked copy of the stop —
  the DB row's stop_loss_price is one of the audit-sensitive fields that is
  never updated after creation (see Trade model), so the original
  risk-approved stop remains the permanent audit record. The trailed stop is
  purely an operational, in-memory protective level; closes still report
  exit_reason="STOP_LOSS" (logged with both the original and effective stop
  so a trailed exit is distinguishable in system.log).

Stale-trade timeout:
  A trade that never reaches its stop or target just sits open indefinitely,
  quietly occupying part of the portfolio's risk budget for no return. A
  separate background sweep (interval: `stale_trade_check_interval_seconds`)
  force-closes any tracked trade open longer than `stale_trade_max_days`
  (reason "STALE_TIMEOUT" — OrderManager records it as exit_reason=MANUAL
  since ExitReason has no dedicated value, but the original "STALE_TIMEOUT"
  string is preserved in logs and the published trade_closed event). Set
  stale_trade_max_days=None to disable.

Runs as a background asyncio task, started on app startup (Layer 6B.9).

Depends on: RedisCache (Layer 5.1), TradeRepo (Layer 3.7), OrderManager (6B.8)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.data.cache import RedisCache
from app.db.models.trade import Trade
from app.monitoring.logger import system_logger, trading_logger

TRADE_EVENTS_CHANNEL = "trade_events"
PRICE_PATTERN = "price:*"


@dataclass
class _TrackedTrade:
    """
    Lightweight snapshot of the fields needed for level monitoring.

    stop_loss_price is the *live* effective stop checked against incoming
    ticks — it may ratchet up over time via the trailing-stop logic.
    initial_stop_loss_price is the original, immutable risk-approved stop
    (matches the DB row) and is only ever used to compute the trade's R unit.
    """

    trade_id: uuid.UUID
    symbol: str
    direction: str
    entry_price: Decimal
    stop_loss_price: Decimal
    initial_stop_loss_price: Decimal
    take_profit_price: Decimal
    opened_at: datetime


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
        trailing_stop_enabled: bool = True,
        trailing_activation_r: Decimal = Decimal("1"),
        trailing_distance_r: Decimal = Decimal("1"),
        stale_trade_max_days: int | None = 14,
        stale_trade_check_interval_seconds: int = 3600,
    ) -> None:
        self._cache = cache
        self._session_factory = session_factory
        self._order_manager = order_manager
        self._tracked: dict[uuid.UUID, _TrackedTrade] = {}
        self._closing: set[uuid.UUID] = set()  # prevent double-close
        self._task: asyncio.Task | None = None
        self._stale_sweep_task: asyncio.Task | None = None
        self._running = False
        self._trailing_stop_enabled = trailing_stop_enabled
        self._trailing_activation_r = trailing_activation_r
        self._trailing_distance_r = trailing_distance_r
        self._stale_trade_max_days = stale_trade_max_days
        self._stale_trade_check_interval_seconds = stale_trade_check_interval_seconds

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
        if self._stale_trade_max_days is not None:
            self._stale_sweep_task = asyncio.create_task(
                self._stale_trade_sweep_loop(), name="position_monitor_stale_sweep"
            )
        system_logger.info(
            "PositionMonitor started",
            extra={"tracked_trades": len(self._tracked)},
        )

    async def stop(self) -> None:
        """Signal the background loop to stop and wait for it to exit."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._stale_sweep_task and not self._stale_sweep_task.done():
            self._stale_sweep_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stale_sweep_task
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
            direction=trade.direction.value,
            entry_price=trade.entry_price,
            stop_loss_price=trade.stop_loss_price,
            initial_stop_loss_price=trade.stop_loss_price,
            take_profit_price=trade.take_profit_price,
            opened_at=trade.executed_at,
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
                system_logger.warning(
                    "PositionMonitor: unparseable price update — tick skipped, "
                    "stop/target check did not run for this update",
                    extra={"ticker": ticker, "raw_data": str(msg.get("data"))[:200]},
                )
                return

        # Check all tracked trades for this ticker
        hits: list[tuple[uuid.UUID, str, _TrackedTrade]] = []
        for tracked in list(self._tracked.values()):
            if tracked.symbol.upper() != ticker:
                continue
            if tracked.trade_id in self._closing:
                continue

            self._update_trailing_stop(tracked, price)

            if price <= tracked.stop_loss_price:
                hits.append((tracked.trade_id, "STOP_LOSS", tracked))
            elif price >= tracked.take_profit_price:
                hits.append((tracked.trade_id, "TAKE_PROFIT", tracked))

        for trade_id, reason, tracked in hits:
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
                    "effective_stop_loss_price": str(tracked.stop_loss_price),
                    "initial_stop_loss_price": str(tracked.initial_stop_loss_price),
                    "trailed": tracked.stop_loss_price != tracked.initial_stop_loss_price,
                },
            )
            asyncio.create_task(self._close(trade_id, reason))

    def _update_trailing_stop(self, tracked: _TrackedTrade, price: Decimal) -> None:
        """
        Ratchet tracked.stop_loss_price toward price once the trade is at
        least `_trailing_activation_r` risk-units (R) in profit, trailing
        `_trailing_distance_r` × R behind price. Never loosens the stop.

        Long trades only — short-side trading isn't supported by RiskManager
        yet (see risk/manager.py), so the BUY-oriented hit-check above would
        be wrong for SELL trades and trailing is skipped for them.
        """
        if not self._trailing_stop_enabled or tracked.direction != "BUY":
            return

        r = tracked.entry_price - tracked.initial_stop_loss_price
        if r <= 0:
            return  # degenerate stop — nothing to measure profit against

        favorable_move = price - tracked.entry_price
        if favorable_move < r * self._trailing_activation_r:
            return

        candidate_stop = price - (r * self._trailing_distance_r)
        if candidate_stop > tracked.stop_loss_price:
            tracked.stop_loss_price = candidate_stop

    # ------------------------------------------------------------------
    # Internal: stale-trade timeout sweep
    # ------------------------------------------------------------------

    async def _stale_trade_sweep_loop(self) -> None:
        """Periodically force-close trades open longer than the configured max."""
        while self._running:
            try:
                await asyncio.sleep(self._stale_trade_check_interval_seconds)
                if self._running:
                    self._sweep_stale_trades()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                system_logger.error(
                    "PositionMonitor: stale-trade sweep error",
                    extra={"error": str(exc)},
                )

    def _sweep_stale_trades(self) -> None:
        if self._stale_trade_max_days is None:
            return
        max_age = timedelta(days=self._stale_trade_max_days)
        now = datetime.now(UTC)

        for tracked in list(self._tracked.values()):
            if tracked.trade_id in self._closing:
                continue
            age = now - tracked.opened_at
            if age < max_age:
                continue

            self._closing.add(tracked.trade_id)
            self._tracked.pop(tracked.trade_id, None)
            system_logger.warning(
                "PositionMonitor: trade exceeded max hold duration — force-closing",
                extra={
                    "trade_id": str(tracked.trade_id),
                    "symbol": tracked.symbol,
                    "opened_at": tracked.opened_at.isoformat(),
                    "age_days": age.days,
                    "max_days": self._stale_trade_max_days,
                },
            )
            asyncio.create_task(self._close(tracked.trade_id, "STALE_TIMEOUT"))

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
            system_logger.warning(
                "PositionMonitor: unparseable trade_events message — ignored",
                extra={"raw_data": str(msg.get("data"))[:200]},
            )
            return

        event_type = event.get("event")
        payload = event.get("payload", {})

        if event_type == "trade_executed":
            trade_id_str = payload.get("trade_id")
            symbol = payload.get("symbol", "")
            direction = payload.get("direction", "BUY")
            entry_str = payload.get("entry_price")
            stop_str = payload.get("stop_loss_price")
            tp_str = payload.get("take_profit_price")
            if trade_id_str and entry_str and stop_str and tp_str:
                try:
                    opened_at = datetime.now(UTC)
                    with suppress(Exception):
                        opened_at = datetime.fromisoformat(event.get("timestamp", ""))
                    self._tracked[uuid.UUID(trade_id_str)] = _TrackedTrade(
                        trade_id=uuid.UUID(trade_id_str),
                        symbol=symbol,
                        direction=direction,
                        entry_price=Decimal(entry_str),
                        stop_loss_price=Decimal(stop_str),
                        initial_stop_loss_price=Decimal(stop_str),
                        take_profit_price=Decimal(tp_str),
                        opened_at=opened_at,
                    )
                except Exception as exc:
                    system_logger.error(
                        "PositionMonitor: trade_executed event had unparseable "
                        "fields — position will NOT be monitored for stop/target",
                        extra={"trade_id": trade_id_str, "error": str(exc)},
                    )
            else:
                system_logger.error(
                    "PositionMonitor: trade_executed event missing required "
                    "fields — position will NOT be monitored for stop/target",
                    extra={
                        "trade_id": trade_id_str,
                        "has_entry_price": bool(entry_str),
                        "has_stop": bool(stop_str),
                        "has_take_profit": bool(tp_str),
                    },
                )

        elif event_type == "trade_closed":
            trade_id_str = payload.get("trade_id")
            if trade_id_str:
                with suppress(Exception):
                    self._tracked.pop(uuid.UUID(trade_id_str), None)
