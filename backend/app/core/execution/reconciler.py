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

from app.brokers.base import BaseBroker, Execution
from app.core import market_calendar
from app.core.events import publish_system_alert
from app.db.models.trade import ExitReason, Trade, TradeDirection, TradeStatus
from app.db.repositories.fill_repo import LegTotals
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
            try:
                executions = await self._broker.get_executions()
            except Exception:
                system_logger.exception("Reconciler: could not read broker executions")
                report.skipped = "executions unavailable"
                return report

            async with self._session_factory() as session:
                repo = TradeRepo(session)
                active = await repo.get_active_trades()
                recent_closed = await repo.list(status=TradeStatus.CLOSED, limit=50)

            for trade in active:
                prev_filled: Decimal | None = None
                totals = None
                if now - trade.executed_at >= MIN_AGE:
                    try:
                        prev_filled = trade.filled_quantity
                        totals = await self._order_manager.sync_fills(trade.id, executions)
                        async with self._session_factory() as session:
                            fresh = await TradeRepo(session).get_by_id(trade.id)
                    except Exception:
                        system_logger.exception(
                            "Reconciler: fill sync failed", extra={"trade_id": str(trade.id)}
                        )
                        continue
                    if fresh is None:
                        continue
                    trade = fresh
                try:
                    await self._reconcile_trade(
                        trade, positions, now, report, prev_filled, totals, executions
                    )
                except Exception:
                    system_logger.exception(
                        "Reconciler: rule failed", extra={"trade_id": str(trade.id)}
                    )

            for closed in recent_closed:
                try:
                    await self._order_manager.journal_fills(closed.id, executions)
                except Exception:
                    system_logger.exception(
                        "Reconciler: journaling late fills failed",
                        extra={"trade_id": str(closed.id)},
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
        self,
        trade: Trade,
        positions: dict[str, Decimal],
        now: datetime,
        report: ReconcileReport,
        prev_filled: Decimal | None = None,
        totals: dict[str, LegTotals] | None = None,
        executions: list[Execution] | None = None,
    ) -> None:
        age = now - trade.executed_at
        if trade.status in (TradeStatus.PENDING, TradeStatus.SUBMITTED):
            await self._reconcile_entry(trade, age, report)
        elif trade.status == TradeStatus.OPEN:
            await self._reconcile_open(
                trade, age, positions, now, report, prev_filled, totals, executions
            )
        elif trade.status == TradeStatus.CLOSING:
            await self._reconcile_closing(trade, positions, now, report, executions)

    async def _reconcile_entry(self, trade: Trade, age: timedelta, report: ReconcileReport) -> None:
        if age < MIN_AGE:
            return
        entry = await self._broker.find_order_by_ref(f"{trade.id}:entry")

        if entry is not None and entry.status in ("FILLED", "PARTIAL", "WORKING") and entry.filled_quantity > 0:
            if entry.status == "WORKING":
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
        prev_filled: Decimal | None = None,
        totals: dict[str, LegTotals] | None = None,
        executions: list[Execution] | None = None,
    ) -> None:
        if age < MIN_AGE:
            return
        if totals is not None and prev_filled is not None and totals["ENTRY"].quantity > prev_filled:
            await self._alert_once(
                "LATE_ENTRY_FILL",
                str(trade.id),
                now,
                f"{trade.symbol}: entry filled more after the trade opened "
                f"({prev_filled} to {totals['ENTRY'].quantity} shares)",
                trade_id=str(trade.id),
                symbol=trade.symbol,
                previous_quantity=str(prev_filled),
                filled_quantity=str(totals["ENTRY"].quantity),
            )
            report.add("late_entry_fill")
        if age >= SUBMITTED_TIMEOUT and self._is_market_open():
            entry = await self._broker.find_order_by_ref(f"{trade.id}:entry")
            if entry is not None and entry.status == "WORKING":
                await self._cancel_quietly(entry.broker_order_id)
        held = positions.get(trade.symbol.upper(), Decimal("0"))
        if held != 0:
            self._zero_seen.discard(trade.id)
        if held == 0:
            if self._confirmed_flat(trade.id):
                await self._finalize_from_broker(trade, now, report, executions)
            return
        signed =-held if trade.direction == TradeDirection.SELL else held
        open_qty = self._order_manager.open_quantity(trade, totals)
        if signed < 0:
            kind = "DIRECTION"
        elif signed < open_qty:
            kind = "MISSING"
        elif signed > open_qty:
            kind = "EXCESS"
        else:
            kind = None
        if kind is not None:
            await self._alert_once(
                "QUANTITY_MISMATCH",
                f"{trade.id}:{kind}",
                now,
                f"{trade.symbol}: broker holds {held} but the trade has {open_qty} open ({kind})",
                trade_id=str(trade.id),
                symbol=trade.symbol,
                kind=kind,
                broker_quantity=str(held),
                expected_quantity=str(open_qty),
            )
            report.add("quantity_mismatch")
        if kind in (None, "EXCESS") and await self._order_manager.ensure_protective_stop(
            trade.id, executions
        ):
            report.add("stop_replaced")

    async def _reconcile_closing(
        self,
        trade: Trade,
        positions: dict[str, Decimal],
        now: datetime,
        report: ReconcileReport,
        executions: list[Execution] | None = None,
    ) -> None:
        first_seen = self._closing_first_seen.setdefault(trade.id, now)
        if now - first_seen < CLOSING_TIMEOUT:
            return
        held = positions.get(trade.symbol.upper(), Decimal("0"))
        if held == 0:
            if self._confirmed_flat(trade.id):
                await self._finalize_from_broker(trade, now, report, executions)
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

    async def _finalize_from_broker(
        self,
        trade: Trade,
        now: datetime,
        report: ReconcileReport,
        executions: list[Execution] | None = None,
    ) -> None:
        """The broker position is gone: book the exit from the fill journal, under the trade lock."""
        async with self._order_manager._trade_lock(trade.id):
            entry = await self._broker.find_order_by_ref(f"{trade.id}:entry")
            if entry is not None and not entry.is_terminal:
                if entry.status == "WORKING":
                    await self._cancel_quietly(entry.broker_order_id)
                report.add("finalize_waiting_entry")
                return

            totals = await self._order_manager.sync_fills(trade.id, executions)
            trade = await self._reload_active(trade.id)
            if trade is None:
                return

            held = await self._broker_held(trade.symbol)
            if held != 0:
                self._zero_seen.discard(trade.id)
                report.add("finalize_aborted_holdings")
                system_logger.warning(
                    "Reconciler: broker still holds shares; finalize aborted",
                    extra={"trade_id": str(trade.id), "symbol": trade.symbol, "held": str(held)},
                )
                return

            if trade.stop_order_id:
                stop = await self._order_manager._cancel_and_wait(
                    f"{trade.id}:stop", trade.stop_order_id
                )
                if stop is not None and not stop.is_terminal:
                    await self._alert_once(
                        "CLOSE_FAILED",
                        f"{trade.id}:stop_live",
                        now,
                        f"{trade.symbol}: broker stop still live after cancel; trade kept open",
                        trade_id=str(trade.id),
                        symbol=trade.symbol,
                    )
                    report.add("finalize_stop_live")
                    return
                totals = await self._order_manager.sync_fills(trade.id)
                trade = await self._reload_active(trade.id)
                if trade is None:
                    return

            stop_qty = totals["STOP"].quantity
            exit_qty = totals["EXIT"].quantity
            price = await self._last_price(trade.symbol) or trade.entry_fill_price or trade.entry_price
            if stop_qty > 0:
                reason = ExitReason.STOP_LOSS
            elif exit_qty > 0:
                reason = trade.exit_reason or ExitReason.MANUAL
            else:
                reason = ExitReason.EXTERNAL
            await self._order_manager.finalize_close(trade.id, reason, None, price)

            if reason == ExitReason.EXTERNAL:
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
            else:
                report.add("closed_stop_fill" if stop_qty > 0 else "closed_exit_fill")

            exited = stop_qty + exit_qty
            unaccounted = self._order_manager.entry_quantity(trade) - exited
            if exited > 0 and unaccounted > 0:
                await self._alert_once(
                    "EXIT_FILLS_MISSING",
                    str(trade.id),
                    now,
                    f"{trade.symbol}: exit fills cover {exited} of {exited + unaccounted} shares; "
                    f"{unaccounted} booked at {price}",
                    trade_id=str(trade.id),
                    symbol=trade.symbol,
                    unaccounted_quantity=str(unaccounted),
                )
                report.add("exit_fills_missing")

    async def _reload_active(self, trade_id: UUID) -> Trade | None:
        """Re-read a trade; None if it is gone or no longer OPEN/CLOSING."""
        async with self._session_factory() as session:
            trade = await TradeRepo(session).get_by_id(trade_id)
        if trade is None or trade.status not in (TradeStatus.OPEN, TradeStatus.CLOSING):
            return None
        return trade

    async def _broker_held(self, symbol: str) -> Decimal:
        """Shares of symbol the broker holds right now (a fresh positions read)."""
        positions = await self._broker.get_positions()
        return next(
            (p.quantity for p in positions if p.symbol.upper() == symbol.upper()),
            Decimal("0"),
        )

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

