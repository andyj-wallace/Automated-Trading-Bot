from __future__ import annotations

import uuid
from decimal import Decimal

from app.brokers.base import Execution, OrderRequest, OrderResult
from app.core.events import publish_system_alert
from app.monitoring.logger import error_logger, trading_logger


class ProtectiveStopMixin:
    """Protective broker stop management mixed into OrderManager."""

    async def place_protective_stop(self, trade_id: uuid.UUID, *, force: bool = False) -> bool:
        """
        Rest a GTC stop order at the broker at the risk-approved stop price.

        Idempotent: does nothing unless the trade is OPEN with no stop recorded.
        The internal monitor stays the primary exit; this is the outage backstop.
        A failure alerts PROTECTIVE_STOP_FAILED and never raises.
        """
        async with self._trade_lock(trade_id):
            return await self._place_protective_stop_unlocked(trade_id, force=force)

    async def _place_protective_stop_unlocked(
        self,
        trade_id: uuid.UUID,
        *,
        force: bool,
        replace_stop_id: str | None = None,
    ) -> bool:
        """Place the stop for the open quantity; the caller holds the trade lock."""
        if not (self._protective_stops or force) or not self._session_factory:
            return False

        from app.db.models.trade import TradeDirection, TradeStatus

        trade = await self._load_trade(trade_id)
        if (
            trade is None
            or trade.status != TradeStatus.OPEN
            or (trade.stop_order_id and trade.stop_order_id != replace_stop_id)
        ):
            return False

        symbol = trade.symbol
        stop_ref = f"{trade_id}:stop"
        old_ids: set[str] = {replace_stop_id} if replace_stop_id else set()
        try:
            quantity = int(self.open_quantity(trade, await self._journal_totals(trade_id)))
            if quantity <= 0:
                return False
            existing = await self._broker.find_order_by_ref(stop_ref)
            if (
                existing is not None
                and existing.status == "WORKING"
                and existing.broker_order_id not in old_ids
            ):
                await self._record_stop_order(trade_id, existing.broker_order_id, trade.stop_loss_price)
                return True
            if existing is not None and existing.broker_order_id:
                # Whatever already sits under this ref is dead; a new stop must have a new id.
                old_ids.add(existing.broker_order_id)
            request = OrderRequest(
                trade_id=trade_id,
                symbol=symbol,
                direction="SELL" if trade.direction == TradeDirection.BUY else "BUY",
                quantity=quantity,
                order_type="STP",
                stop_price=trade.stop_loss_price,
                tif="GTC",
                order_ref=stop_ref,
            )
            result, error = await self._place_order_with_retry(
                request,
                context="protective stop",
                exclude_order_ids=frozenset(old_ids),
            )
        except Exception as exc:
            result, error = None, exc

        replaced_old_stop = result is not None and result.broker_order_id in old_ids
        if result is None or result.status in ("REJECTED", "ERROR") or replaced_old_stop:
            await publish_system_alert(
                self._cache,
                "PROTECTIVE_STOP_FAILED",
                f"Broker stop for {symbol} could not be placed; internal monitor still protects it",
                trade_id=str(trade_id),
                symbol=symbol,
                error=str(error) if error else (result.error_message if result else None),
            )
            return False
        await self._record_stop_order(trade_id, result.broker_order_id, trade.stop_loss_price)
        return True

    async def ensure_protective_stop(
        self, trade_id: uuid.UUID, executions: list[Execution] | None = None
    ) -> bool:
        """Replace the broker stop when it no longer covers the open quantity."""
        if not self._session_factory:
            return False
        async with self._trade_lock(trade_id):
            from app.db.models.trade import TradeStatus

            trade = await self._load_trade(trade_id)
            if trade is None or trade.status != TradeStatus.OPEN or not trade.stop_order_id:
                return False

            totals = await self.sync_fills(trade_id, executions)
            trade = await self._load_trade(trade_id)
            if trade is None:
                return False
            open_qty = self.open_quantity(trade, totals)
            stop_ref = f"{trade_id}:stop"
            stop = await self._broker.find_order_by_ref(stop_ref)
            remaining = self._stop_remaining(stop)
            if stop is None or stop.is_terminal:
                needed = open_qty > 0
            elif stop.status == "WORKING":
                needed = remaining != open_qty
            else:
                needed = False
            if not needed:
                return False

            if not await self._broker_holds_open_quantity(trade, open_qty):
                return False

            old_stop_order_id = trade.stop_order_id
            cancelled = await self._cancel_and_wait(stop_ref, old_stop_order_id)
            if cancelled is not None and not cancelled.is_terminal:
                error_logger.error(
                    "Protective stop still live after cancel; not replacing",
                    extra={
                        "trade_id": str(trade_id),
                        "symbol": trade.symbol,
                        "stop_status": cancelled.status,
                    },
                )
                return False

            totals = await self.sync_fills(trade_id)
            trade = await self._load_trade(trade_id)
            if trade is None:
                return False
            open_qty = self.open_quantity(trade, totals)
            if open_qty <= 0 or not await self._broker_holds_open_quantity(trade, open_qty):
                return False

            placed = await self._place_protective_stop_unlocked(
                trade_id, force=True, replace_stop_id=old_stop_order_id
            )
            if not placed:
                return False
            await publish_system_alert(
                self._cache,
                "PROTECTIVE_STOP_REPLACED",
                f"Broker stop for {trade.symbol} resized to the open quantity",
                severity="WARNING",
                trade_id=str(trade_id),
                symbol=trade.symbol,
                old_quantity=str(remaining) if remaining is not None else None,
                new_quantity=str(int(open_qty)),
            )
            return True

    async def adopt_entry_fill(self, trade_id: uuid.UUID, fill: OrderResult) -> bool:
        """
        Move a PENDING/SUBMITTED trade to OPEN from a fill found at the broker
        (reconciler path): atomic, publishes trade_executed, places the protective stop.
        Returns False if the trade had already moved on.
        """
        from app.core.execution.trade_handler import TradeHandler
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            trade = await TradeRepo(session).open_from_fill(
                trade_id, fill.broker_order_id, fill.avg_fill_price, fill.filled_quantity,
                commission=fill.commission,
            )
            await session.commit()
        if trade is None:
            return False
        trading_logger.info(
            "Entry fill adopted from broker",
            extra={
                "trade_id": str(trade_id),
                "symbol": trade.symbol,
                "filled_quantity": str(fill.filled_quantity),
            },
        )
        if self._cache is not None:
            try:
                await TradeHandler(self._cache).publish_for_trade(trade, fill)
            except Exception as exc:
                trading_logger.warning(
                    "OrderManager: trade_executed publish failed",
                    extra={"trade_id": str(trade_id), "error": str(exc)},
                )
        await self.place_protective_stop(trade_id)
        return True

    async def _load_trade(self, trade_id: uuid.UUID):
        """Read the trade row in its own session."""
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            return await TradeRepo(session).get_by_id(trade_id)

    @staticmethod
    def _stop_remaining(stop: OrderResult | None) -> Decimal | None:
        """Shares a stop order still asks for, or None when the broker did not say."""
        if stop is None or stop.ordered_quantity is None:
            return None
        return stop.ordered_quantity - stop.filled_quantity

    async def _broker_holds_open_quantity(self, trade, open_qty: Decimal) -> bool:
        """True when the broker holds at least open_qty shares in the trade's direction."""
        from app.db.models.trade import TradeDirection

        symbol = trade.symbol.upper()
        positions = await self._broker.get_positions()
        held = sum(
            (p.quantity for p in positions if p.symbol.upper() == symbol), Decimal("0")
        )
        if trade.direction == TradeDirection.SELL:
            held = -held
        if held < open_qty:
            trading_logger.warning(
                "Protective stop not replaced: broker holds less than the open quantity",
                extra={
                    "trade_id": str(trade.id),
                    "symbol": trade.symbol,
                    "held": str(held),
                    "open_quantity": str(open_qty),
                },
            )
            return False
        return True
