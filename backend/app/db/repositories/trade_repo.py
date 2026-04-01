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
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.trade import ExitReason, Trade, TradeDirection, TradeStatus


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
        trade.closed_at = closed_at or datetime.now(timezone.utc)

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
