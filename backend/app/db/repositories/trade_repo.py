"""
Repository for the trades table.

Audit-sensitive fields (entry_price, stop_loss_price, risk_amount,
account_balance_at_entry) are set only at creation — this repo deliberately
provides no method to update them after the fact.

After Layer 6B, trades are created at PENDING status by OrderManager and
transition forward through the lifecycle via update_status(). The only
backward movement allowed is to CANCELLED (from PENDING or SUBMITTED).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.trade import ACTIVE_RISK_STATUSES, ExitReason, Trade, TradeDirection, TradeStatus


class TradeRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        symbol: str,
        direction: TradeDirection,
        quantity: Decimal,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        take_profit_price: Decimal,
        reward_to_risk_ratio: Decimal,
        risk_amount: Decimal,
        account_balance_at_entry: Decimal,
        strategy_id: uuid.UUID | None = None,
        trade_id: uuid.UUID | None = None,
        executed_at: datetime | None = None,
    ) -> Trade:
        """
        Persist a new trade record at PENDING status.

        Audit-sensitive fields are keyword-only so callers must be
        explicit — there is no default value for any financial field.

        Args:
            trade_id: Optional UUID to use as the primary key. If omitted,
                      a new UUID is generated. Pass the TradeRequest.trade_id
                      so the same ID flows through validation → audit → DB.
        """
        trade = Trade(
            symbol=symbol.upper(),
            direction=direction,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            reward_to_risk_ratio=reward_to_risk_ratio,
            risk_amount=risk_amount,
            account_balance_at_entry=account_balance_at_entry,
            strategy_id=strategy_id,
            status=TradeStatus.PENDING,
        )
        if trade_id is not None:
            trade.id = trade_id
        if executed_at is not None:
            trade.executed_at = executed_at

        self.session.add(trade)
        await self.session.flush()
        await self.session.refresh(trade)
        return trade

    async def get_by_id(self, trade_id: uuid.UUID) -> Trade | None:
        """Return a trade by its UUID, or None if not found."""
        stmt = select(Trade).where(Trade.id == trade_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_open_trades(self) -> list[Trade]:
        """Return all trades currently in OPEN status."""
        stmt = (
            select(Trade)
            .where(Trade.status == TradeStatus.OPEN)
            .order_by(Trade.executed_at)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_trades(self) -> list[Trade]:
        """
        Return all trades not yet in a terminal state: PENDING, SUBMITTED,
        OPEN, or CLOSING. Used by OrderManager to compute aggregate portfolio risk for
        the Gate 4 cap — a trade's risk_amount must count immediately on
        creation (PENDING), not only once the broker confirms a fill (OPEN),
        otherwise two trades submitted back-to-back can both pass the cap
        check before either one's risk is "reserved".
        """
        stmt = (
            select(Trade)
            .where(
                Trade.status.in_(ACTIVE_RISK_STATUSES)
            )
            .order_by(Trade.executed_at)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def has_active_trade(self, symbol: str) -> bool:
        """True if the symbol has a PENDING, SUBMITTED, OPEN or CLOSING trade."""
        stmt = (
            select(Trade.id)
            .where(
                Trade.symbol == symbol.upper(),
                Trade.status.in_(ACTIVE_RISK_STATUSES),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def list(
        self,
        symbol: str | None = None,
        strategy_id: uuid.UUID | None = None,
        status: TradeStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Trade]:
        """
        Return trades with optional filters.

        Ordered by executed_at descending (most recent first).
        """
        stmt = select(Trade).order_by(Trade.executed_at.desc())
        if symbol:
            stmt = stmt.where(Trade.symbol == symbol.upper())
        if strategy_id:
            stmt = stmt.where(Trade.strategy_id == strategy_id)
        if status:
            stmt = stmt.where(Trade.status == status)
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        trade_id: uuid.UUID,
        new_status: TradeStatus,
    ) -> Trade | None:
        """
        Transition a trade to a new status.

        Returns the updated trade, or None if not found.
        Does NOT validate the transition — caller (OrderManager) is responsible
        for detecting and logging unexpected transitions as CRITICAL.
        """
        trade = await self.get_by_id(trade_id)
        if trade is None:
            return None

        trade.status = new_status
        await self.session.flush()
        await self.session.refresh(trade)
        return trade

    async def close_trade(
        self,
        trade_id: uuid.UUID,
        exit_price: Decimal,
        pnl: Decimal,
        exit_reason: ExitReason,
        closed_at: datetime | None = None,
    ) -> Trade | None:
        """
        Mark a trade as CLOSED and record the exit details.

        Only updates the allowed post-close fields: exit_price, pnl, exit_reason,
        status, closed_at. Does NOT modify any audit-sensitive fields.

        Returns the updated trade, or None if not found.
        """
        trade = await self.get_by_id(trade_id)
        if trade is None:
            return None

        trade.exit_price = exit_price
        trade.pnl = pnl
        trade.exit_reason = exit_reason
        trade.status = TradeStatus.CLOSED
        trade.closed_at = closed_at or datetime.now(UTC)

        await self.session.flush()
        await self.session.refresh(trade)
        return trade

    async def cancel_trade(self, trade_id: uuid.UUID) -> Trade | None:
        """
        Mark a trade as CANCELLED.

        Returns the updated trade, or None if not found.
        """
        trade = await self.get_by_id(trade_id)
        if trade is None:
            return None

        trade.status = TradeStatus.CANCELLED
        await self.session.flush()
        await self.session.refresh(trade)
        return trade

    async def set_entry_order(self, trade_id: uuid.UUID, order_id: str) -> None:
        """Record the broker order id of the entry order."""
        await self.session.execute(
            update(Trade).where(Trade.id == trade_id).values(entry_order_id=order_id)
        )

    async def set_stop_order(
        self,
        trade_id: uuid.UUID,
        order_id: str | None,
        stop_price: Decimal | None = None,
    ) -> None:
        """Record (or clear, with None) the protective stop order id and its price."""
        values: dict = {"stop_order_id": order_id}
        if stop_price is not None:
            values["current_stop_price"] = stop_price
        await self.session.execute(update(Trade).where(Trade.id == trade_id).values(**values))

    async def set_exit_order(self, trade_id: uuid.UUID, order_id: str) -> None:
        """Record the broker order id of the exit order."""
        await self.session.execute(
            update(Trade).where(Trade.id == trade_id).values(exit_order_id=order_id)
        )

    async def record_entry_fill(
        self,
        trade_id: uuid.UUID,
        order_id: str,
        fill_price: Decimal,
        filled_qty: Decimal,
    ) -> Trade | None:
        """Persist the entry fill separately from the audit-immutable columns."""
        stmt = (
            update(Trade)
            .where(Trade.id == trade_id)
            .values(
                entry_order_id=order_id,
                entry_fill_price=fill_price,
                filled_quantity=filled_qty,
            )
            .returning(Trade)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def begin_close(
        self,
        trade_id: uuid.UUID,
        exit_reason: ExitReason,
        *,
        allow_from_closing: bool = False,
    ) -> Trade | None:
        """Atomically move OPEN (or CLOSING when allowed) to CLOSING; None if not eligible."""
        allowed = (
            (TradeStatus.OPEN, TradeStatus.CLOSING)
            if allow_from_closing
            else (TradeStatus.OPEN,)
        )
        stmt = (
            update(Trade)
            .where(Trade.id == trade_id, Trade.status.in_(allowed))
            .values(
                status=TradeStatus.CLOSING,
                exit_reason=exit_reason,
                close_attempts=Trade.close_attempts + 1,
            )
            .returning(Trade)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def finish_close(
        self,
        trade_id: uuid.UUID,
        exit_price: Decimal,
        pnl: Decimal,
        exit_order_id: str | None,
        closed_at: datetime | None = None,
        exit_reason: ExitReason | None = None,
    ) -> Trade | None:
        """Atomically move CLOSING to CLOSED; None if the trade was not CLOSING."""
        values: dict = {
            "status": TradeStatus.CLOSED,
            "exit_price": exit_price,
            "pnl": pnl,
            "closed_at": closed_at or datetime.now(UTC),
        }
        if exit_order_id is not None:
            values["exit_order_id"] = exit_order_id
        if exit_reason is not None:
            values["exit_reason"] = exit_reason
        stmt = (
            update(Trade)
            .where(Trade.id == trade_id, Trade.status == TradeStatus.CLOSING)
            .values(**values)
            .returning(Trade)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def open_from_fill(
        self,
        trade_id: uuid.UUID,
        order_id: str,
        fill_price: Decimal,
        filled_qty: Decimal,
    ) -> Trade | None:
        """Atomically move PENDING/SUBMITTED to OPEN with the fill; None if already moved on."""
        stmt = (
            update(Trade)
            .where(
                Trade.id == trade_id,
                Trade.status.in_((TradeStatus.PENDING, TradeStatus.SUBMITTED)),
            )
            .values(
                status=TradeStatus.OPEN,
                entry_order_id=order_id,
                entry_fill_price=fill_price,
                filled_quantity=filled_qty,
            )
            .returning(Trade)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def cancel_if_unfilled(self, trade_id: uuid.UUID) -> Trade | None:
        """Atomically move PENDING/SUBMITTED to CANCELLED; None if the trade moved on."""
        stmt = (
            update(Trade)
            .where(
                Trade.id == trade_id,
                Trade.status.in_((TradeStatus.PENDING, TradeStatus.SUBMITTED)),
            )
            .values(status=TradeStatus.CANCELLED)
            .returning(Trade)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def closed_since(self, start: datetime) -> Sequence[Trade]:
        """CLOSED trades with closed_at >= start (used to rebuild the circuit breaker)."""
        stmt = (
            select(Trade)
            .where(Trade.status == TradeStatus.CLOSED, Trade.closed_at >= start)
            .order_by(Trade.closed_at)
        )
        return tuple((await self.session.execute(stmt)).scalars().all())

    async def opened_since(self, start: datetime) -> Sequence[Trade]:
        """Trades created at or after `start` that reached the broker (not CANCELLED)."""
        stmt = (
            select(Trade)
            .where(Trade.executed_at >= start, Trade.status != TradeStatus.CANCELLED)
            .order_by(Trade.executed_at)
        )
        return tuple((await self.session.execute(stmt)).scalars().all())

    async def recent_pnls_by_strategy(self, limit: int) -> dict[uuid.UUID, Sequence[Decimal]]:
        """Each strategy's last `limit` closed-trade PnLs, newest first."""
        ids = (
            await self.session.execute(
                select(Trade.strategy_id)
                .where(Trade.status == TradeStatus.CLOSED, Trade.strategy_id.is_not(None))
                .distinct()
            )
        ).scalars().all()
        result: dict[uuid.UUID, Sequence[Decimal]] = {}
        for strategy_id in ids:
            rows = (
                await self.session.execute(
                    select(Trade.pnl)
                    .where(
                        Trade.status == TradeStatus.CLOSED,
                        Trade.strategy_id == strategy_id,
                        Trade.pnl.is_not(None),
                    )
                    .order_by(Trade.closed_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            result[strategy_id] = tuple(rows)
        return result
