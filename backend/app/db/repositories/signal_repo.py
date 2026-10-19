"""Repository for the strategy_signals table."""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.strategy_signal import StrategySignal


class SignalRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(
        self,
        *,
        strategy_id: uuid.UUID,
        symbol: str,
        bar_date: date,
        action: str,
        entry_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
        take_profit_price: Decimal | None = None,
    ) -> uuid.UUID | None:
        """Insert the signal; return its id, or None if this key was already claimed."""
        stmt = (
            insert(StrategySignal)
            .values(
                strategy_id=strategy_id,
                symbol=symbol.upper(),
                bar_date=bar_date,
                action=action,
                entry_price=entry_price,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
            )
            .on_conflict_do_nothing(constraint="uq_strategy_signals_dedupe")
            .returning(StrategySignal.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def set_outcome(
        self,
        signal_id: uuid.UUID,
        outcome: str,
        detail: str | None = None,
        trade_id: uuid.UUID | None = None,
    ) -> None:
        """Record what happened to a claimed signal."""
        await self.session.execute(
            update(StrategySignal)
            .where(StrategySignal.id == signal_id)
            .values(outcome=outcome, detail=detail, trade_id=trade_id)
        )

    async def list(
        self,
        symbol: str | None = None,
        strategy_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> list[StrategySignal]:
        """Most recent signals first, optionally filtered."""
        stmt = select(StrategySignal).order_by(StrategySignal.created_at.desc()).limit(limit)
        if symbol:
            stmt = stmt.where(StrategySignal.symbol == symbol.upper())
        if strategy_id:
            stmt = stmt.where(StrategySignal.strategy_id == strategy_id)
        return list((await self.session.execute(stmt)).scalars().all())
