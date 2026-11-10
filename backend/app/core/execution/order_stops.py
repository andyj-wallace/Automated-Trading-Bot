from __future__ import annotations

import asyncio
import uuid

from app.brokers.base import OrderRequest, OrderResult
from app.core.events import publish_system_alert
from app.monitoring.logger import trading_logger


class ProtectiveStopMixin:
    """Protective broker stop management mixed into OrderManager."""

    async def place_protective_stop(self, trade_id: uuid.UUID, *, force: bool = False) -> bool:
        """
        Rest a GTC stop order at the broker at the risk-approved stop price.

        Idempotent: does nothing unless the trade is OPEN with no stop recorded.
        The internal monitor stays the primary exit; this is the outage backstop.
        A failure alerts PROTECTIVE_STOP_FAILED and never raises.
        """
        if not (self._protective_stops or force) or not self._session_factory:
            return False

        from app.db.models.trade import TradeDirection, TradeStatus
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            trade = await TradeRepo(session).get_by_id(trade_id)
        if trade is None or trade.status != TradeStatus.OPEN or trade.stop_order_id:
            return False

        symbol = trade.symbol
        quantity = trade.filled_quantity if trade.filled_quantity else trade.quantity
        stop_ref = f"{trade_id}:stop"
        try:
            existing = await self._broker.find_order_by_ref(stop_ref)
            if existing is not None and existing.status == "WORKING":
                await self._record_stop_order(trade_id, existing.broker_order_id, trade.stop_loss_price)
                return True
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
            result, error = await self._place_order_with_retry(request, context="protective stop")
        except Exception as exc:
            result, error = None, exc

        if result is None or result.status in ("REJECTED", "ERROR"):
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

    async def _cancel_protective_stop(
        self, trade_id: uuid.UUID, stop_order_id: str
    ) -> OrderResult | None:
        """
        Cancel the resting stop and wait for it to settle.

        Returns the stop's fill summary if it executed (so the caller must not sell
        again), None once it is gone. Raises if it is still working after the wait.
        """
        stop_ref = f"{trade_id}:stop"
        await self._broker.cancel_order(stop_order_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stop_cancel_wait_seconds
        while True:
            state = await self._broker.find_order_by_ref(stop_ref)
            if state is None or state.status != "WORKING":
                break
            if loop.time() >= deadline:
                raise RuntimeError("protective stop still working after cancel")
            await asyncio.sleep(0.25)
        fill = await self._broker.get_execution_summary(stop_ref)
        if fill is not None and fill.filled_quantity > 0:
            return fill
        return None

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
