"""
Entry-path methods mixed into OrderManager: validation, broker submission,
outcome recording and the post-entry KPI snapshot.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.base import OrderRequest, OrderResult
from app.core.events import publish_system_alert
from app.core.risk.manager import RiskRejectionError, TradeRequest, ValidationResult
from app.monitoring.logger import error_logger, risk_logger, trading_logger


class OrderEntryMixin:
    """Entry-path methods mixed into OrderManager."""


    async def _validate_entry(self, request: TradeRequest) -> ValidationResult:
        """Run the duplicate-symbol, risk, notional, growth and account-rule checks."""
        active_trades = await self._load_active_trades()
        symbol = request.symbol.upper()
        if any(t.symbol.upper() == symbol for t in active_trades):
            reason = f"SYMBOL_ALREADY_ACTIVE: {symbol} already has an active trade"
            risk_logger.warning(
                "OrderManager: trade rejected, symbol already active",
                extra={"trade_id": str(request.trade_id), "symbol": symbol},
            )
            raise RiskRejectionError(reason=reason, request=request)
        current_aggregate_risk = sum((t.risk_amount for t in active_trades), Decimal("0"))
        validation = self._risk_manager.validate(request, current_aggregate_risk)
        position_value = request.quantity * request.entry_price + sum(
            (
                (t.filled_quantity or t.quantity) * (t.entry_fill_price or t.entry_price)
                for t in active_trades
            ),
            Decimal("0"),
        )
        if position_value > request.account_balance:
            reason = (
                f"NOTIONAL_LIMIT_EXCEEDED: open positions plus this trade would be worth "
                f"{position_value}, more than the account ({request.account_balance}); "
                "the bot never trades on margin"
            )
            risk_logger.warning(
                "OrderManager: trade rejected, total position value above account value",
                extra={"trade_id": str(request.trade_id), "symbol": symbol,
                       "position_value": str(position_value)},
            )
            raise RiskRejectionError(reason=reason, request=request)

        if (
            self._growth is not None
            and self._growth.is_active(request.account_balance)
            and len(active_trades) >= self._growth.max_open_positions
        ):
            self._reject_entry(
                request, "GROWTH_MAX_POSITIONS",
                f"growth mode allows {self._growth.max_open_positions} active trades; "
                f"{len(active_trades)} already active",
            )
        if self._account_type is not None:
            await self._check_account_rules(request, active_trades)
        return validation

    async def _submit_entry(
        self,
        request: TradeRequest,
    ) -> tuple[OrderRequest, OrderResult, BaseException | None, bool]:
        """Send the entry market order; look up the broker record if the call failed."""
        broker_request = OrderRequest(
            trade_id=request.trade_id,
            symbol=request.symbol,
            direction=request.direction,
            quantity=request.quantity,
            order_type="MKT",
            order_ref=f"{request.trade_id}:entry",
            strategy_id=request.strategy_id,
        )

        order_result, broker_error = await self._place_order_with_retry(
            broker_request, context="entry order"
        )
        state_unknown = False
        if order_result is None:
            try:
                order_result = await self._find_existing_order(broker_request)
            except Exception:
                state_unknown = True
            if order_result is not None:
                broker_error = None
        if order_result is None:
            order_result = OrderResult(
                trade_id=request.trade_id,
                broker_order_id="",
                status="ERROR",
                filled_quantity=Decimal("0"),
                avg_fill_price=Decimal("0"),
                order_ref=broker_request.order_ref,
                error_message=str(broker_error),
                timestamp=datetime.now(UTC),
            )
        return broker_request, order_result, broker_error, state_unknown

    async def _record_entry_outcome(
        self,
        request: TradeRequest,
        validation: ValidationResult,
        order_result: OrderResult,
        broker_request: OrderRequest,
        state_unknown: bool,
    ) -> bool:
        """Record a filled (in full or in part), working, unknown or rejected entry; return True if it filled."""
        filled = order_result.filled_quantity > 0
        if self._session_factory:
            if filled:
                if order_result.status == "WORKING":
                    order_result = await self._settle_working_entry(request, order_result)
                await self._record_entry_fill(request.trade_id, order_result)
                await self._transition(request.trade_id, "OPEN")
                try:
                    await self.sync_fills(request.trade_id)
                except Exception as exc:
                    # The stop below is still placed: it is sized from the broker's order result
                    # already recorded above, and it can only reduce risk.
                    error_logger.error(
                        "Fill journal sync failed after entry fill; placing the stop anyway",
                        extra={
                            "trade_id": str(request.trade_id),
                            "symbol": request.symbol,
                            "error": str(exc),
                        },
                    )
                await self._publish_trade_executed(request, validation, order_result)
                await self.place_protective_stop(
                    request.trade_id, force=request.submit_stop_to_broker
                )
            elif order_result.status == "WORKING":
                await self._record_entry_order(request.trade_id, order_result.broker_order_id)
                await publish_system_alert(
                    self._cache,
                    "ORDER_STUCK_WORKING",
                    f"Entry order for {request.symbol} is still working at the broker",
                    trade_id=str(request.trade_id),
                    symbol=request.symbol,
                    order_ref=broker_request.order_ref,
                )
            elif state_unknown:
                # The order may be live at the broker: stay SUBMITTED so the reconciler adopts or cancels it.
                await publish_system_alert(
                    self._cache,
                    "ORDER_STATE_UNKNOWN",
                    f"Entry order for {request.symbol} failed and the broker could not say whether "
                    "it arrived; the reconciler will resolve it",
                    trade_id=str(request.trade_id),
                    symbol=request.symbol,
                    order_ref=broker_request.order_ref,
                )
            else:
                await self._transition(request.trade_id, "CANCELLED")
        return filled

    async def _settle_working_entry(
        self, request: TradeRequest, order_result: OrderResult
    ) -> OrderResult:
        """Cancel a partly filled working entry; return the freshest result that is not behind it."""
        chosen = order_result
        try:
            final = await self._cancel_and_wait(
                f"{request.trade_id}:entry", order_result.broker_order_id
            )
        except Exception as exc:
            error_logger.error(
                "Entry cancel failed after a partial fill; continuing with the fills so far",
                extra={
                    "trade_id": str(request.trade_id),
                    "symbol": request.symbol,
                    "error": str(exc),
                },
            )
        else:
            if final is not None and final.filled_quantity >= order_result.filled_quantity:
                chosen = final
        if not chosen.is_terminal:
            await publish_system_alert(
                self._cache,
                "ENTRY_REMAINDER_WORKING",
                f"Entry for {request.symbol} is partly filled; the rest is still working at the broker",
                trade_id=str(request.trade_id),
                symbol=request.symbol,
                filled_quantity=str(chosen.filled_quantity),
                ordered_quantity=(
                    str(chosen.ordered_quantity) if chosen.ordered_quantity is not None else None
                ),
            )
        return chosen

    async def _snapshot_after_entry(self, request: TradeRequest, filled: bool) -> None:
        """Record a KPI snapshot after a fill; a failure is logged and never fails the order."""
        if self._session_factory and self._broker and filled:
            try:
                from app.core.metrics.collector import MetricsCollector
                async with self._session_factory() as session:
                    await MetricsCollector().record_snapshot(session, self._broker)
            except Exception as exc:
                trading_logger.warning(
                    "OrderManager: MetricsCollector snapshot failed",
                    extra={"trade_id": str(request.trade_id), "error": str(exc)},
                )

    async def _publish_trade_executed(
        self,
        request: TradeRequest,
        validation: ValidationResult,
        order_result: OrderResult,
    ) -> None:
        """Publish trade_executed for a fill; a failure here never fails the order."""
        if self._cache is None or self._session_factory is None:
            return
        try:
            from app.core.execution.trade_handler import TradeHandler

            async with self._session_factory() as session:
                await TradeHandler(self._cache).on_fill(request, validation, order_result, session)
        except Exception as exc:
            trading_logger.warning(
                "OrderManager: trade_executed publish failed",
                extra={"trade_id": str(request.trade_id), "error": str(exc)},
            )

    async def _place_order_with_retry(
        self,
        broker_request: OrderRequest,
        *,
        context: str,
    ) -> tuple[OrderResult | None, BaseException | None]:
        """
        Call broker.place_order() with retry on transient failures.

        Retries only when the broker call *raises* (network blip, timeout,
        transient disconnect) — up to `_order_retry_attempts` times with
        doubling backoff. A clean returned OrderResult (including a broker
        REJECTED status) is not a transient failure and is returned as-is
        on the first attempt.

        Returns (order_result, None) on success, or (None, last_exception)
        if every attempt raised.
        """
        last_exc: BaseException | None = None
        for attempt in range(1, self._order_retry_attempts + 1):
            try:
                result = await self._broker.place_order(broker_request)
                return result, None
            except Exception as exc:
                last_exc = exc
                try:
                    existing = await self._find_existing_order(broker_request)
                except Exception as lookup_exc:
                    trading_logger.warning(
                        f"OrderManager: {context} failed and the broker lookup failed too; not resubmitting",
                        extra={
                            "trade_id": str(broker_request.trade_id),
                            "error": str(exc),
                            "lookup_error": str(lookup_exc),
                        },
                    )
                    return None, exc
                if existing is not None:
                    return existing, None
                if attempt < self._order_retry_attempts:
                    delay = self._order_retry_backoff_seconds * (2 ** (attempt - 1))
                    trading_logger.warning(
                        f"OrderManager: {context} place_order failed — retrying",
                        extra={
                            "trade_id": str(broker_request.trade_id),
                            "attempt": attempt,
                            "max_attempts": self._order_retry_attempts,
                            "retry_in_seconds": delay,
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(delay)
        return None, last_exc

    async def _find_existing_order(self, broker_request: OrderRequest) -> OrderResult | None:
        """The broker's record of this order_ref, None if it has none; raises if the lookup fails."""
        if not broker_request.order_ref:
            return None
        return await self._broker.find_order_by_ref(broker_request.order_ref)
