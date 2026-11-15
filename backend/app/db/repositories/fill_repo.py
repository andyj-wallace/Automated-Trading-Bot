"""Repository for the trade_fills table: one row per broker execution."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import Execution
from app.db.models.trade_fill import TradeFill

_LEG_SUFFIXES = ("entry", "stop", "exit")


@dataclass(frozen=True)
class LegTotals:
    """Summed fills for one leg of a trade."""

    quantity: Decimal
    notional: Decimal  # sum of quantity * price
    commission: Decimal  # NULL commissions count as 0


class FillRepo:
    """Stores broker executions against a trade and totals them per leg."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(self, trade_id: uuid.UUID, executions: Iterable[Execution]) -> int:
        """Insert executions for this trade; return how many were new. Caller commits."""
        leg_by_ref = {f"{trade_id}:{suffix}": suffix.upper() for suffix in _LEG_SUFFIXES}
        new_rows = 0
        for ex in executions:
            leg = leg_by_ref.get(ex.order_ref)
            if leg is None:
                continue
            insert_stmt = insert(TradeFill).values(
                exec_id=ex.exec_id,
                trade_id=trade_id,
                leg=leg,
                side=ex.side,
                quantity=ex.quantity,
                price=ex.price,
                commission=ex.commission,
                broker_order_id=ex.broker_order_id,
                executed_at=ex.executed_at,
            )
            stmt = insert_stmt.on_conflict_do_update(
                index_elements=["exec_id"],
                set_={
                    "commission": func.coalesce(
                        TradeFill.commission, insert_stmt.excluded.commission
                    )
                },
            ).returning(literal_column("(xmax = 0)"))
            inserted = (await self.session.execute(stmt)).scalar_one()
            if inserted:
                new_rows += 1
        return new_rows

    async def totals(self, trade_id: uuid.UUID) -> dict[str, LegTotals]:
        """Return ENTRY, STOP and EXIT totals for the trade (zeros when a leg has no fills)."""
        stmt = (
            select(
                TradeFill.leg,
                func.coalesce(func.sum(TradeFill.quantity), 0),
                func.coalesce(func.sum(TradeFill.quantity * TradeFill.price), 0),
                func.coalesce(func.sum(TradeFill.commission), 0),
            )
            .where(TradeFill.trade_id == trade_id)
            .group_by(TradeFill.leg)
        )
        totals = {
            leg: LegTotals(
                quantity=Decimal(str(quantity)),
                notional=Decimal(str(notional)),
                commission=Decimal(str(commission)),
            )
            for leg, quantity, notional, commission in (await self.session.execute(stmt)).all()
        }
        zero = LegTotals(quantity=Decimal(0), notional=Decimal(0), commission=Decimal(0))
        return {leg: totals.get(leg, zero) for leg in ("ENTRY", "STOP", "EXIT")}
