from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.brokers.base import OrderResult
from app.core.risk.manager import RiskRejectionError, TradeRequest, ValidationResult
from app.monitoring.logger import risk_logger


class OrderRecordsMixin:
    """Trade-row (DB) helpers mixed into OrderManager: create, check and record rows."""

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _create_pending(
        self,
        request: TradeRequest,
        validation: ValidationResult,
    ) -> None:
        """Create a PENDING trade row in the DB before the broker call."""
        from app.db.models.trade import TradeDirection
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            repo = TradeRepo(session)
            await repo.create(
                trade_id=request.trade_id,
                symbol=request.symbol,
                direction=TradeDirection(request.direction),
                quantity=request.quantity,
                entry_price=request.entry_price,
                stop_loss_price=request.stop_loss_price,  # type: ignore[arg-type]
                take_profit_price=validation.take_profit_price,
                reward_to_risk_ratio=validation.reward_to_risk_ratio,
                risk_amount=validation.risk_amount,
                account_balance_at_entry=validation.account_balance_at_entry,
                strategy_id=request.strategy_id,
                exit_deadline=request.exit_deadline,
            )
            await session.commit()

    async def _check_account_rules(self, request: TradeRequest, active_trades: list) -> None:
        """Cash accounts pay only with settled cash; margin accounts keep a day trade in reserve."""
        from app.db.models.trade import TradeStatus

        try:
            summary = await self._broker.get_account_summary()
        except Exception as exc:
            self._reject_entry(request, "ACCOUNT_UNAVAILABLE", f"could not read the account: {exc}")
        if self._account_type == "margin":
            if summary.day_trades_remaining == 0:  # -1 means unlimited
                self._reject_entry(
                    request, "NO_DAY_TRADES_LEFT",
                    "IBKR reports 0 day trades left; a same-day stop-out would flag the account",
                )
            return
        settled = summary.settled_cash if summary.settled_cash is not None else summary.cash_balance
        # IBKR refreshes account values about every 3 minutes, so reserve orders it may not have counted yet.
        cutoff = datetime.now(UTC) - timedelta(minutes=5)
        reserved = sum(
            (
                (t.filled_quantity or t.quantity) * (t.entry_fill_price or t.entry_price)
                for t in active_trades
                if t.status in (TradeStatus.PENDING, TradeStatus.SUBMITTED) or t.executed_at >= cutoff
            ),
            Decimal("0"),
        )
        fee = self._cost_model.commission(request.quantity, request.entry_price) if self._cost_model else Decimal("0")
        needed = request.quantity * request.entry_price + fee
        if needed > settled - reserved:
            self._reject_entry(
                request, "INSUFFICIENT_SETTLED_CASH",
                f"needs {needed:.2f}; settled cash {settled:.2f} minus {reserved:.2f} reserved",
            )

    @staticmethod
    def _reject_entry(request: TradeRequest, code: str, detail: str) -> None:
        risk_logger.warning(
            "OrderManager: entry rejected",
            extra={"trade_id": str(request.trade_id), "symbol": request.symbol, "reason": code, "detail": detail},
        )
        raise RiskRejectionError(reason=f"{code}: {detail}", request=request)

    async def _record_entry_fill(self, trade_id: uuid.UUID, result: OrderResult) -> None:
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            await TradeRepo(session).record_entry_fill(
                trade_id, result.broker_order_id, result.avg_fill_price, result.filled_quantity,
                commission=result.commission,
            )
            await session.commit()

    async def _record_exit_order(self, trade_id: uuid.UUID, order_id: str) -> None:
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            await TradeRepo(session).set_exit_order(trade_id, order_id)
            await session.commit()

    async def _record_stop_order(
        self, trade_id: uuid.UUID, order_id: str, stop_price: Decimal
    ) -> None:
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            await TradeRepo(session).set_stop_order(trade_id, order_id, stop_price)
            await session.commit()

    async def _record_entry_order(self, trade_id: uuid.UUID, order_id: str) -> None:
        from app.db.repositories.trade_repo import TradeRepo

        async with self._session_factory() as session:
            await TradeRepo(session).set_entry_order(trade_id, order_id)
            await session.commit()
