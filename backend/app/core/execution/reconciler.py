"""
Reconciler: keeps the trades table consistent with the broker.

Runs at startup, after every broker reconnect and periodically. It resolves
PENDING/SUBMITTED entries, finalizes positions that were closed outside the
normal path (a broker stop that filled while the app was down, a manual close
in TWS), retries stuck CLOSING trades, and alerts on anything it cannot
explain. It never closes a position it does not track (those may be the
user's manual trades).
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from app.brokers.base import BaseBroker
from app.core import market_calendar
from app.core.events import publish_system_alert
from app.db.models.trade import ExitReason, Trade, TradeStatus
from app.db.repositories.trade_repo import TradeRepo
from app.monitoring.logger import system_logger

PENDING_TIMEOUT = timedelta(minutes=2)
SUBMITTED_TIMEOUT = timedelta(minutes=5)
CLOSING_TIMEOUT = timedelta(minutes=2)
# Younger trades may still be mid-flight inside submit_order()/close_position().
MIN_AGE = timedelta(seconds=60)
MAX_CLOSE_ATTEMPTS = 3


@dataclass
class ReconcileReport:
    """Count of actions taken per rule in one run."""

    counts: Counter = field(default_factory=Counter)
    skipped: str | None = None

    def add(self, key: str) -> None:
        self.counts[key] += 1


class Reconciler:
    """Compare broker positions/orders with active DB trades and repair the difference."""

    def __init__(
        self,
        broker: BaseBroker,
        session_factory,
        order_manager,
        cache,
        *,
        clock: Callable[[], datetime] = market_calendar.now_et,
        is_market_open: Callable[[], bool] = market_calendar.is_market_open,
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory
        self._order_manager = order_manager
        self._cache = cache
        self._clock = clock
        self._is_market_open = is_market_open
        self._alerted: set[tuple[str, str, date]] = set()
        self._closing_first_seen: dict[UUID, datetime] = {}
        self._zero_seen: set[UUID] = set()
        self._lock = asyncio.Lock()

    async def run_once(self) -> ReconcileReport:
        """Apply every rule once; does nothing while the broker is disconnected."""
        report = ReconcileReport()
        if not self._broker.is_connected():
            report.skipped = "broker disconnected"
            return report
        async with self._lock:
            now = self._clock()
            self._prune_alerts(now)
            try:
                positions = {
                    p.symbol.upper(): p.quantity for p in await self._broker.get_positions()
                }
            except Exception:
                system_logger.exception("Reconciler: could not read broker positions")
                report.skipped = "positions unavailable"
                return report

            async with self._session_factory() as session:
                repo = TradeRepo(session)
                active = await repo.get_active_trades()
                recent_closed = await repo.list(status=TradeStatus.CLOSED, limit=50)

            for trade in active:
                try:
                    await self._reconcile_trade(trade, positions, now, report)
                except Exception:
                    system_logger.exception(
                        "Reconciler: rule failed", extra={"trade_id": str(trade.id)}
                    )
            active_ids = {t.id for t in active}
            self._closing_first_seen = {
                k: v for k, v in self._closing_first_seen.items() if k in active_ids
            }
            self._zero_seen &= active_ids

            await self._flag_unmanaged(positions, active, recent_closed, now, report)

        if report.counts:
            system_logger.info("Reconciler run", extra={"actions": dict(report.counts)})
        return report

    # ------------------------------------------------------------------
    # Per-trade rules
    # ------------------------------------------------------------------

    async def _reconcile_trade(
        self, trade: Trade, positions: dict[str, Decimal], now: datetime, report: ReconcileReport
    ) -> None:
        age = now - trade.executed_at
        if trade.status in (TradeStatus.PENDING, TradeStatus.SUBMITTED):
            await self._reconcile_entry(trade, age, report)
        elif trade.status == TradeStatus.OPEN:
            await self._reconcile_open(trade, age, positions, now, report)
        elif trade.status == TradeStatus.CLOSING:
            await self._reconcile_closing(trade, positions, now, report)

    async def _reconcile_entry(self, trade: Trade, age: timedelta, report: ReconcileReport) -> None:
        if age < MIN_AGE:
            return
        entry = await self._broker.find_order_by_ref(f"{trade.id}:entry")

        if entry is not None and entry.status in ("FILLED", "PARTIAL") and entry.filled_quantity > 0:
            if entry.status == "PARTIAL":
                await self._cancel_quietly(entry.broker_order_id)
            fill = await self._broker.get_execution_summary(f"{trade.id}:entry") or entry
            if await self._order_manager.adopt_entry_fill(trade.id, fill):
                report.add("entry_adopted")
            return

        if trade.status == TradeStatus.PENDING:
            if entry is None and age >= PENDING_TIMEOUT or entry is not None and entry.status == "REJECTED" and age >= PENDING_TIMEOUT:
                await self._cancel_trade(trade.id, report, "pending_cancelled")
            return

        if entry is not None and entry.status == "WORKING":
            if age >= SUBMITTED_TIMEOUT and self._is_market_open():
                await self._cancel_quietly(entry.broker_order_id)
                report.add("entry_working_cancelled")
            return
        if age >= SUBMITTED_TIMEOUT:
            await self._cancel_trade(trade.id, report, "entry_cancelled")

    async def _reconcile_open(
        self,
        trade: Trade,
        age: timedelta,
        positions: dict[str, Decimal],
        now: datetime,
        report: ReconcileReport,
    ) -> None:
        if age < MIN_AGE:
            return
        held = positions.get(trade.symbol.upper(), Decimal("0"))
        expected = trade.filled_quantity if trade.filled_quantity else trade.quantity
        if held > 0:
            self._zero_seen.discard(trade.id)
        if held == 0:
            if self._confirmed_flat(trade.id):
                await self._finalize_from_broker(trade, now, report)
        elif 0 < held < expected:
            self._zero_seen.discard(trade.id)
            await self._alert_once(
                "QUANTITY_MISMATCH",
                trade.symbol,
                now,
                f"{trade.symbol}: broker holds {held} but the trade expects {expected}",
                trade_id=str(trade.id),
                broker_quantity=str(held),
                expected_quantity=str(expected),
            )
            report.add("quantity_mismatch")

    async def _reconcile_closing(
        self, trade: Trade, positions: dict[str, Decimal], now: datetime, report: ReconcileReport
    ) -> None:
        first_seen = self._closing_first_seen.setdefault(trade.id, now)
        if now - first_seen < CLOSING_TIMEOUT:
            return
        held = positions.get(trade.symbol.upper(), Decimal("0"))
        if held == 0:
            if self._confirmed_flat(trade.id):
                await self._finalize_from_broker(trade, now, report)
            return
        self._zero_seen.discard(trade.id)
        reason = trade.exit_reason or ExitReason.MANUAL
        if trade.close_attempts < MAX_CLOSE_ATTEMPTS:
            if self._is_market_open():
                await self._order_manager.close_position(
                    trade.id, reason.value, allow_from_closing=True
                )
                report.add("close_retried")
            return
        await self._alert_once(
            "CLOSE_NEEDS_MANUAL_INTERVENTION",
            str(trade.id),
            now,
            f"{trade.symbol}: close failed {trade.close_attempts} times, close it manually",
            trade_id=str(trade.id),
            symbol=trade.symbol,
        )
        report.add("close_manual_needed")

    def _confirmed_flat(self, trade_id: UUID) -> bool:
        """True only on the second consecutive run that sees no position (guards a partial broker snapshot)."""
        if trade_id in self._zero_seen:
            return True
        self._zero_seen.add(trade_id)
        return False

    async def _finalize_from_broker(self, trade: Trade, now: datetime, report: ReconcileReport) -> None:
        """The broker position is gone: book the exit using whatever fills the broker reports."""
        if trade.stop_order_id:
            await self._cancel_quietly(trade.stop_order_id)  # never leave a stop that could open a short
        stop = await self._broker.get_execution_summary(f"{trade.id}:stop")
        if stop is not None and stop.filled_quantity > 0:
            await self._order_manager.finalize_close(
                trade.id, ExitReason.STOP_LOSS, stop, stop.avg_fill_price
            )
            report.add("closed_stop_fill")
            return
        exit_fill = await self._broker.get_execution_summary(f"{trade.id}:exit")
        if exit_fill is not None and exit_fill.filled_quantity > 0:
            await self._order_manager.finalize_close(
                trade.id, trade.exit_reason or ExitReason.MANUAL, exit_fill, exit_fill.avg_fill_price
            )
            report.add("closed_exit_fill")
            return

        price = await self._last_price(trade.symbol) or trade.entry_fill_price or trade.entry_price
        await self._order_manager.finalize_close(trade.id, ExitReason.EXTERNAL, None, price)
        await self._alert_once(
            "POSITION_CLOSED_EXTERNALLY",
            str(trade.id),
            now,
            f"{trade.symbol}: position was closed outside the bot; booked at {price}",
            trade_id=str(trade.id),
            symbol=trade.symbol,
            exit_price=str(price),
        )
        report.add("closed_external")

    # ------------------------------------------------------------------
    # Rule 5: positions the bot does not track
    # ------------------------------------------------------------------

    async def _flag_unmanaged(
        self,
        positions: dict[str, Decimal],
        active: list[Trade],
        recent_closed: list[Trade],
        now: datetime,
        report: ReconcileReport,
    ) -> None:
        tracked = {t.symbol.upper() for t in active}
        tracked |= {
            t.symbol.upper()
            for t in recent_closed
            if t.closed_at is not None and now - t.closed_at < PENDING_TIMEOUT
        }
        for symbol, quantity in positions.items():
            if quantity == 0 or symbol in tracked:
                continue
            await self._alert_once(
                "UNMANAGED_POSITION",
                symbol,
                now,
                f"Broker holds {quantity} {symbol} with no matching trade; the bot will not touch it",
                symbol=symbol,
                quantity=str(quantity),
            )
            report.add("unmanaged_position")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _cancel_trade(self, trade_id: UUID, report: ReconcileReport, key: str) -> None:
        async with self._session_factory() as session:
            cancelled = await TradeRepo(session).cancel_if_unfilled(trade_id)
            await session.commit()
        if cancelled is not None:
            report.add(key)

    async def _cancel_quietly(self, broker_order_id: str) -> None:
        if not broker_order_id:
            return
        try:
            await self._broker.cancel_order(broker_order_id)
        except Exception as exc:
            system_logger.warning(
                "Reconciler: cancel failed",
                extra={"broker_order_id": broker_order_id, "error": str(exc)},
            )

    async def _last_price(self, symbol: str) -> Decimal | None:
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(f"price:{symbol}")
            return Decimal(str(json.loads(raw)["price"])) if raw else None
        except Exception:
            return None

    async def _alert_once(
        self, alert_type: str, key: str, now: datetime, message: str, **details
    ) -> None:
        token = (alert_type, key, now.date())
        if token in self._alerted:
            return
        self._alerted.add(token)
        await publish_system_alert(self._cache, alert_type, message, **details)

    def _prune_alerts(self, now: datetime) -> None:
        self._alerted = {t for t in self._alerted if t[2] == now.date()}

