"""
PositionMonitor — watches live prices against open trade stop/target levels.

Receives live prices in-process (MarketDataFeed calls on_price_update) and
keeps its tracked set in sync by re-reading OPEN trades from the database
every few seconds. When a price crosses a stop-loss or take-profit level while
the market is open it calls OrderManager.close_position() to start the exit
flow. Nothing safety-critical depends on Redis.

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
  (reason "STALE_TIMEOUT", stored as exit_reason=STALE_TIMEOUT). Set
  stale_trade_max_days=None to disable.

Price-feed watchdog: while the market is open, a tracked symbol with no recent
tick raises a PRICE_FEED_STALE system alert (once per symbol per day).

Depends on: TradeRepo (Layer 3.7), OrderManager (6B.8)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.brokers.base import PriceUpdate
from app.core import market_calendar
from app.core.events import publish_system_alert
from app.core.tasks import PeriodicTask, spawn
from app.db.models.trade import Trade
from app.monitoring.logger import system_logger, trading_logger


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
    Monitors open trade levels against live in-process price updates.

    Usage:
        monitor = PositionMonitor(cache, session_factory, order_manager)
        feed.add_listener(monitor.on_price_update)
        await monitor.start()
        ...
        await monitor.stop()            # waits for in-flight closes
    """

    def __init__(
        self,
        cache,
        session_factory,
        order_manager,  # OrderManager — imported lazily to avoid circular imports
        trailing_stop_enabled: bool = True,
        trailing_activation_r: Decimal = Decimal("1"),
        trailing_distance_r: Decimal = Decimal("1"),
        stale_trade_max_days: int | None = 14,
        stale_trade_check_interval_seconds: float = 3600,
        *,
        is_market_open: Callable[[], bool] = market_calendar.is_market_open,
        clock: Callable[[], datetime] = market_calendar.now_et,
        last_tick_at: Callable[[str], datetime | None] | None = None,
        resync_interval_seconds: float = 5.0,
        watchdog_interval_seconds: float = 30.0,
        price_stale_seconds: int = 120,
    ) -> None:
        self._cache = cache
        self._session_factory = session_factory
        self._order_manager = order_manager
        self._tracked: dict[uuid.UUID, _TrackedTrade] = {}
        self._closing: set[uuid.UUID] = set()  # prevent double-close
        self._tasks: set[asyncio.Task] = set()
        self._periodic: list[PeriodicTask] = []
        self._running = False
        self._trailing_stop_enabled = trailing_stop_enabled
        self._trailing_activation_r = trailing_activation_r
        self._trailing_distance_r = trailing_distance_r
        self._stale_trade_max_days = stale_trade_max_days
        self._stale_trade_check_interval_seconds = stale_trade_check_interval_seconds
        self._is_market_open = is_market_open
        self._clock = clock
        self._last_tick_at = last_tick_at or (lambda symbol: None)
        self._resync_interval_seconds = resync_interval_seconds
        self._watchdog_interval_seconds = watchdog_interval_seconds
        self._price_stale_seconds = price_stale_seconds
        self._started_at: datetime | None = None
        self._stale_alerted: set[tuple[str, date]] = set()

    @property
    def tracked_count(self) -> int:
        return len(self._tracked)

    def tracked_symbols(self) -> set[str]:
        return {t.symbol.upper() for t in self._tracked.values()}

    async def start(self) -> None:
        """Load open trades from DB and start the periodic resync, sweep and watchdog."""
        self._started_at = self._clock()
        try:
            await self._resync()
        except Exception as exc:
            system_logger.warning(
                "PositionMonitor: could not pre-load open trades from DB",
                extra={"error": str(exc)},
            )
        self._running = True
        self._periodic = [
            PeriodicTask(
                "monitor_resync",
                self._resync_interval_seconds,
                self._resync,
                initial_delay=self._resync_interval_seconds,
            ),
            PeriodicTask(
                "monitor_watchdog",
                self._watchdog_interval_seconds,
                self._check_price_feed,
                initial_delay=self._watchdog_interval_seconds,
            ),
        ]
        if self._stale_trade_max_days is not None:
            self._periodic.append(
                PeriodicTask(
                    "monitor_stale_sweep",
                    self._stale_trade_check_interval_seconds,
                    self._sweep_stale_trades,
                    initial_delay=self._stale_trade_check_interval_seconds,
                )
            )
        for task in self._periodic:
            task.start()
        system_logger.info(
            "PositionMonitor started",
            extra={"tracked_trades": len(self._tracked)},
        )

    async def stop(self) -> None:
        """Stop periodic tasks, then wait for in-flight closes to finish."""
        self._running = False
        for task in self._periodic:
            await task.stop()
        self._periodic = []
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=20)
        self._tracked.clear()  # a stopped monitor must not act on late ticks
        system_logger.info("PositionMonitor stopped")

    # ------------------------------------------------------------------
    # Internal: DB resync
    # ------------------------------------------------------------------

    async def _resync(self) -> None:
        """Merge DB OPEN trades into _tracked, keeping trailing state of known trades."""
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            open_trades: list[Trade] = await TradeRepo(session).get_open_trades()
        open_ids = {t.id for t in open_trades}
        for trade in open_trades:
            if trade.id not in self._tracked and trade.id not in self._closing:
                self._track(trade)
        for trade_id in list(self._tracked):
            if trade_id not in open_ids:
                self._tracked.pop(trade_id, None)

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
    # Price handler
    # ------------------------------------------------------------------

    async def on_price_update(self, update: PriceUpdate) -> None:
        """Check a live price against all open trades for that ticker (market hours only)."""
        if not self._is_market_open():
            return
        ticker = update.ticker.upper()
        price = update.price

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
            spawn(self._close(trade_id, reason), name=f"close-{trade_id}", registry=self._tasks)

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

    async def _sweep_stale_trades(self) -> None:
        if self._stale_trade_max_days is None or not self._is_market_open():
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
            spawn(
                self._close(tracked.trade_id, "STALE_TIMEOUT"),
                name=f"close-{tracked.trade_id}",
                registry=self._tasks,
            )

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
    # Internal: price-feed watchdog
    # ------------------------------------------------------------------

    async def _check_price_feed(self) -> None:
        """Alert once per symbol per day when a tracked symbol has no recent tick."""
        if not self._is_market_open():
            return
        now = self._clock()
        today = market_calendar.trading_date(now)
        bounds = market_calendar.session_bounds(today)
        grace = timedelta(seconds=self._price_stale_seconds)
        if bounds is not None and now - bounds[0] < grace:
            return
        if self._started_at is not None and now - self._started_at < grace:
            return
        self._stale_alerted = {k for k in self._stale_alerted if k[1] == today}
        for symbol in sorted({t.symbol.upper() for t in self._tracked.values()}):
            if (symbol, today) in self._stale_alerted:
                continue
            last = self._last_tick_at(symbol)
            if last is not None and (now - last).total_seconds() <= self._price_stale_seconds:
                continue
            self._stale_alerted.add((symbol, today))
            await publish_system_alert(
                self._cache,
                "PRICE_FEED_STALE",
                f"No price tick for {symbol} in over {self._price_stale_seconds}s "
                "while the market is open; stop/target checks are blind",
                symbol=symbol,
                last_tick_at=last.isoformat() if last else None,
            )
