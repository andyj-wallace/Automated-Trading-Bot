"""Fill journal sync for OrderManager: trade_fills rows, entry totals and cancel-and-wait."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.base import Execution, OrderResult
from app.db.repositories.fill_repo import LegTotals
from app.monitoring.logger import error_logger


class FillSyncMixin:
    """Journals broker executions per trade and keeps the entry fill in step with them."""

    async def sync_fills(
        self, trade_id: uuid.UUID, executions: list[Execution] | None = None
    ) -> dict[str, LegTotals]:
        """Journal the trade's executions, raise the entry fill to match, and return leg totals."""
        from app.db.repositories.fill_repo import FillRepo
        from app.db.repositories.trade_repo import TradeRepo

        if executions is None:
            executions = await self._broker.get_executions()
        async with self._session_factory() as session:
            await FillRepo(session).record(trade_id, executions)
            totals = await FillRepo(session).totals(trade_id)
            entry = totals["ENTRY"]
            if entry.quantity > 0:
                await TradeRepo(session).raise_entry_fill(
                    trade_id,
                    entry.quantity,
                    entry.notional / entry.quantity,
                    entry.commission,
                )
            await session.commit()
        return totals

    async def journal_fills(self, trade_id: uuid.UUID, executions: list[Execution]) -> int:
        """Journal executions for a trade without touching trade columns; return rows added."""
        from app.db.repositories.fill_repo import FillRepo

        async with self._session_factory() as session:
            added = await FillRepo(session).record(trade_id, executions)
            await session.commit()
        return added

    async def _journal_totals(self, trade_id: uuid.UUID) -> dict[str, LegTotals]:
        """Leg totals read from the fill journal only."""
        from app.db.repositories.fill_repo import FillRepo

        async with self._session_factory() as session:
            return await FillRepo(session).totals(trade_id)

    @staticmethod
    def entry_quantity(trade) -> Decimal:
        """Quantity the entry actually filled, falling back to the ordered quantity."""
        return Decimal(str(trade.filled_quantity or trade.quantity))

    @classmethod
    def open_quantity(cls, trade, totals: dict[str, LegTotals]) -> Decimal:
        """Entry quantity still held: entry minus journaled stop and exit fills."""
        return cls.entry_quantity(trade) - totals["STOP"].quantity - totals["EXIT"].quantity

    def _trade_lock(self, trade_id: uuid.UUID) -> asyncio.Lock:
        """The per-trade lock, created on first use."""
        return self._trade_locks.setdefault(trade_id, asyncio.Lock())

    async def _cancel_and_wait(
        self, order_ref: str, broker_order_id: str | None = None
    ) -> OrderResult | None:
        """Cancel a live order and poll until it is terminal, gone, or the wait runs out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stop_cancel_wait_seconds
        last: OrderResult | None = None
        step = "lookup"
        try:
            current = await asyncio.wait_for(
                self._broker.find_order_by_ref(order_ref),
                timeout=max(deadline - loop.time(), 0.01),
            )
            if current is None or current.is_terminal:
                return current
            last = current
            step = "cancel"
            await asyncio.wait_for(
                self._broker.cancel_order(broker_order_id or current.broker_order_id),
                timeout=max(deadline - loop.time(), 0.01),
            )
            while True:
                step = "poll"
                state = await asyncio.wait_for(
                    self._broker.find_order_by_ref(order_ref),
                    timeout=max(deadline - loop.time(), 0.01),
                )
                if state is None or state.is_terminal:
                    return state
                last = state
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return state
                await asyncio.sleep(min(0.25, remaining))
        except TimeoutError:
            error_logger.warning(
                "broker call timed out during cancel wait",
                extra={"order_ref": order_ref, "step": step},
            )
            if last is not None:
                return last
            return OrderResult(
                trade_id=uuid.UUID(order_ref.split(":")[0]),
                broker_order_id=broker_order_id or "",
                status="ERROR",
                filled_quantity=Decimal("0"),
                avg_fill_price=Decimal("0"),
                order_ref=order_ref,
                error_message="broker call timed out during cancel wait",
                timestamp=datetime.now(UTC),
            )
