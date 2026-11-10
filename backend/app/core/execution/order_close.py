from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from enum import Enum

from app.brokers.base import OrderRequest, OrderResult
from app.core.events import envelope, publish_system_alert
from app.monitoring.logger import error_logger, trading_logger

# Redis channel for trade events — consumed by WebSocket handler and PositionMonitor
TRADE_EVENTS_CHANNEL = "trade_events"


class CloseOutcome(str, Enum):
    CLOSED = "CLOSED"
    PENDING_EXIT = "PENDING_EXIT"  # exit order is working at the broker
    FAILED = "FAILED"  # trade stays CLOSING; the reconciler retries
    NOT_OPEN = "NOT_OPEN"
    MARKET_CLOSED = "MARKET_CLOSED"


class OrderCloseMixin:
    """Close-path methods mixed into OrderManager: exit orders and exit bookkeeping."""

    async def close_position(
        self,
        trade_id: uuid.UUID,
        reason: str,  # an ExitReason value: "STOP_LOSS" | "TAKE_PROFIT" | "MANUAL" | "STALE_TIMEOUT" | "EXTERNAL" | "TIME_EXIT"
        *,
        allow_from_closing: bool = False,
    ) -> CloseOutcome:
        """
        Close a position with one market order, at most once per trade.

        OPEN -> CLOSING (atomic, stores the intended exit_reason) -> order ref
        "<id>:exit" -> CLOSED only after a real fill. Raises ValueError for an
        unknown reason string. Never touches state while the market is closed.
        """
        if not self._session_factory:
            raise RuntimeError("close_position requires a session_factory")

        from app.db.models.trade import ExitReason, TradeDirection
        from app.db.repositories.trade_repo import TradeRepo

        exit_reason = ExitReason(reason)

        if not self._is_market_open():
            return CloseOutcome.MARKET_CLOSED

        async with self._session_factory() as session:
            trade = await TradeRepo(session).begin_close(
                trade_id, exit_reason, allow_from_closing=allow_from_closing
            )
            await session.commit()
        if trade is None:
            trading_logger.warning(
                "close_position: trade not found or not eligible to close",
                extra={"trade_id": str(trade_id), "exit_reason": reason},
            )
            return CloseOutcome.NOT_OPEN

        symbol = trade.symbol
        close_direction = "SELL" if trade.direction == TradeDirection.BUY else "BUY"
        total_quantity = trade.filled_quantity if trade.filled_quantity else trade.quantity
        stop_order_id = trade.stop_order_id
        exit_ref = f"{trade_id}:exit"

        async def fail(alert_type: str, message: str, **details) -> CloseOutcome:
            await publish_system_alert(
                self._cache,
                alert_type,
                message,
                trade_id=str(trade_id),
                symbol=symbol,
                exit_reason=reason,
                **details,
            )
            return CloseOutcome.FAILED

        return await self._close_open_trade(
            trade_id,
            exit_reason,
            trade,
            symbol,
            close_direction,
            total_quantity,
            stop_order_id,
            exit_ref,
            fail,
        )

    async def _close_open_trade(
        self,
        trade_id: uuid.UUID,
        exit_reason,
        trade,
        symbol: str,
        close_direction: str,
        total_quantity: Decimal,
        stop_order_id: str | None,
        exit_ref: str,
        fail: Callable[..., Awaitable[CloseOutcome]],
    ) -> CloseOutcome:
        """Clear the protective stop, book an exit already at the broker, then send the close."""
        from app.db.models.trade import ExitReason

        # A protective stop must be gone (or found filled) before we sell.
        if stop_order_id:
            try:
                stop_fill = await self._cancel_protective_stop(trade_id, stop_order_id)
            except Exception as exc:
                return await fail(
                    "CLOSE_FAILED",
                    f"Could not cancel protective stop for {symbol}; exit not sent",
                    error=str(exc),
                )
            if stop_fill is not None:
                done = await self.finalize_close(
                    trade_id, ExitReason.STOP_LOSS, stop_fill, stop_fill.avg_fill_price
                )
                return CloseOutcome.CLOSED if done else CloseOutcome.FAILED

        try:
            existing = await self._broker.find_order_by_ref(exit_ref)
            sold_summary = await self._broker.get_execution_summary(exit_ref)
        except Exception as exc:
            return await fail(
                "CLOSE_FAILED",
                f"Could not query broker for existing exit of {symbol}; exit not sent",
                error=str(exc),
            )

        if existing is not None and existing.status == "WORKING":
            await self._record_exit_order(trade_id, existing.broker_order_id)
            return CloseOutcome.PENDING_EXIT

        sold = sold_summary.filled_quantity if sold_summary else Decimal("0")
        remaining = total_quantity - sold
        if remaining <= 0 and sold_summary is not None:
            done = await self.finalize_close(
                trade_id, exit_reason, sold_summary, sold_summary.avg_fill_price
            )
            return CloseOutcome.CLOSED if done else CloseOutcome.FAILED

        return await self._send_close_order(
            trade_id, exit_reason, trade, symbol, close_direction, remaining, exit_ref, fail
        )

    async def _send_close_order(
        self,
        trade_id: uuid.UUID,
        exit_reason,
        trade,
        symbol: str,
        close_direction: str,
        remaining: Decimal,
        exit_ref: str,
        fail: Callable[..., Awaitable[CloseOutcome]],
    ) -> CloseOutcome:
        """Check the broker still holds the shares, place the market exit, and act on its status."""
        from app.db.models.trade import TradeDirection

        try:
            held = sum(
                (p.quantity for p in await self._broker.get_positions() if p.symbol.upper() == symbol.upper()),
                Decimal("0"),
            )
        except Exception as exc:
            return await fail(
                "CLOSE_FAILED",
                f"Could not read the broker position for {symbol}; exit not sent",
                error=str(exc),
            )
        if trade.direction != TradeDirection.BUY:
            held = -held
        if held <= 0:
            # IBKR reports today's fills only, so a stop that filled on an earlier day looks unfilled
            # here; selling would open a short. The reconciler books the exit from the broker instead.
            return await fail(
                "CLOSE_FAILED",
                f"Broker holds no {symbol}; nothing sold. The reconciler will book the exit.",
            )
        remaining = min(remaining, held)

        close_request = OrderRequest(
            trade_id=trade_id,
            symbol=symbol,
            direction=close_direction,
            quantity=remaining,
            order_type="MKT",
            order_ref=exit_ref,
        )
        order_result, broker_error = await self._place_order_with_retry(
            close_request, context="close order"
        )
        if order_result is None:
            return await fail(
                "CLOSE_FAILED",
                f"Exit order for {symbol} failed after all retries; trade left CLOSING",
                attempts=self._order_retry_attempts,
                error=str(broker_error),
            )

        if order_result.status == "FILLED":
            try:
                summary = await self._broker.get_execution_summary(exit_ref)
            except Exception:
                summary = None
            fill = summary or order_result
            done = await self.finalize_close(trade_id, exit_reason, fill, fill.avg_fill_price)
            return CloseOutcome.CLOSED if done else CloseOutcome.FAILED
        if order_result.status == "WORKING":
            await self._record_exit_order(trade_id, order_result.broker_order_id)
            return CloseOutcome.PENDING_EXIT
        if order_result.status == "PARTIAL":
            return await fail(
                "CLOSE_PARTIAL",
                f"Exit for {symbol} only partly filled; trade left CLOSING",
                filled_quantity=str(order_result.filled_quantity),
            )
        return await fail(
            "CLOSE_FAILED",
            f"Exit order for {symbol} was {order_result.status}; trade left CLOSING",
            error=order_result.error_message,
        )

    async def finalize_close(
        self,
        trade_id: uuid.UUID,
        exit_reason,
        fill: OrderResult | None,
        fallback_price: Decimal,
    ) -> bool:
        """
        Record a confirmed exit: PnL, CLOSED row, circuit breaker, event, snapshot.

        Never places an order and ignores market hours. Returns True only if this
        call moved the trade CLOSING -> CLOSED, so PnL is recorded exactly once.
        """
        if not self._session_factory:
            raise RuntimeError("finalize_close requires a session_factory")

        from app.db.models.trade import TradeStatus
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            repo = TradeRepo(session)
            trade = await repo.get_by_id(trade_id)
            if trade is None:
                error_logger.error(
                    "finalize_close: trade not found", extra={"trade_id": str(trade_id)}
                )
                return False
            if trade.status == TradeStatus.OPEN:
                trade = await repo.begin_close(trade_id, exit_reason)
                if trade is None:
                    await session.commit()
                    return False
            elif trade.status != TradeStatus.CLOSING:
                return False

            exit_price, pnl, exit_commission = self._close_pnl(trade, fill, fallback_price)

            closed = await repo.finish_close(
                trade_id,
                exit_price,
                pnl,
                fill.broker_order_id if fill else None,
                exit_reason=exit_reason,
                exit_commission=exit_commission,
            )
            await session.commit()
        if closed is None:
            return False

        await self._after_close(trade_id, exit_reason, closed, exit_price, pnl)
        return True

    @staticmethod
    def _close_pnl(
        trade, fill: OrderResult | None, fallback_price: Decimal
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Return (exit_price, pnl, exit_commission) for a close; entry and exit fees come off PnL."""
        from app.db.models.trade import TradeDirection

        if fill is not None:
            shares, exit_price = fill.filled_quantity, fill.avg_fill_price
        else:
            shares = trade.filled_quantity if trade.filled_quantity else trade.quantity
            exit_price = fallback_price
        entry = trade.entry_fill_price if trade.entry_fill_price else trade.entry_price
        exit_commission = fill.commission if fill is not None else Decimal("0")
        fees = (trade.entry_commission or Decimal("0")) + exit_commission
        if trade.direction == TradeDirection.BUY:
            pnl = (exit_price - entry) * shares - fees
        else:
            pnl = (entry - exit_price) * shares - fees
        return exit_price, pnl, exit_commission

    async def _after_close(
        self,
        trade_id: uuid.UUID,
        exit_reason,
        closed,
        exit_price: Decimal,
        pnl: Decimal,
    ) -> None:
        """Feed the circuit breaker, log the close, publish trade_closed, and snapshot metrics."""
        if self._circuit_breaker is not None:
            events = self._circuit_breaker.record_closed_trade(
                pnl=pnl,
                account_balance=closed.account_balance_at_entry,
                strategy_id=closed.strategy_id,
            )
            await self._handle_breaker_events(events, closed.strategy_id)

        trading_logger.info(
            "Position closed",
            extra={
                "trade_id": str(trade_id),
                "symbol": closed.symbol,
                "reason": exit_reason.value,
                "exit_price": str(exit_price),
                "pnl": str(pnl),
            },
        )
        if self._cache:
            try:
                await self._cache.publish(
                    TRADE_EVENTS_CHANNEL,
                    envelope(
                        "trade_closed",
                        {
                            "trade_id": str(trade_id),
                            "symbol": closed.symbol,
                            "reason": exit_reason.value,
                            "exit_price": str(exit_price),
                            "pnl": str(pnl),
                        },
                    ),
                )
            except Exception as exc:
                trading_logger.warning(
                    "OrderManager: trade_closed publish failed",
                    extra={"trade_id": str(trade_id), "error": str(exc)},
                )

        try:
            from app.core.metrics.collector import MetricsCollector

            async with self._session_factory() as session:
                await MetricsCollector().record_snapshot(session, self._broker)
        except Exception as exc:
            trading_logger.warning(
                "OrderManager: MetricsCollector snapshot failed on close",
                extra={"trade_id": str(trade_id), "error": str(exc)},
            )

    async def _handle_breaker_events(self, events: list[str], strategy_id: uuid.UUID | None) -> None:
        """Alert on a new daily halt; durably disable a strategy the breaker just killed."""
        for event in events:
            if event == "DAILY_HALT":
                await publish_system_alert(
                    self._cache,
                    "CIRCUIT_BREAKER_HALT",
                    "Daily loss limit reached: new trades are halted for the rest of the trading day",
                )
            elif event == "STRATEGY_KILLED" and strategy_id is not None:
                try:
                    from app.db.repositories.strategy_repo import StrategyRepo

                    async with self._session_factory() as session:
                        await StrategyRepo(session).set_enabled(strategy_id, False)
                        await session.commit()
                except Exception as exc:
                    error_logger.error(
                        "OrderManager: could not disable killed strategy",
                        extra={"strategy_id": str(strategy_id), "error": str(exc)},
                    )
                await publish_system_alert(
                    self._cache,
                    "STRATEGY_AUTO_DISABLED",
                    "Strategy disabled after too many consecutive losses",
                    strategy_id=str(strategy_id),
                )
