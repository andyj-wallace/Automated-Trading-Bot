"""
Repository for the trading_strategies table.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.strategy import TradingStrategy


class StrategyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self) -> list[TradingStrategy]:
        """Return all strategies ordered by name."""
        stmt = select(TradingStrategy).order_by(TradingStrategy.name)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_enabled(self) -> list[TradingStrategy]:
        """Return only enabled strategies."""
        stmt = (
            select(TradingStrategy)
            .where(TradingStrategy.is_enabled.is_(True))
            .order_by(TradingStrategy.name)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, strategy_id: uuid.UUID) -> TradingStrategy | None:
        """Return a strategy by UUID, or None if not found."""
        stmt = select(TradingStrategy).where(TradingStrategy.id == strategy_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        name: str,
        type: str,
        config: dict | None = None,
        is_enabled: bool = False,
    ) -> TradingStrategy:
        """Create and persist a new strategy."""
        strategy = TradingStrategy(
            name=name,
            type=type,
            config=config or {},
            is_enabled=is_enabled,
        )
        self.session.add(strategy)
        await self.session.flush()
        await self.session.refresh(strategy)
        return strategy

    async def set_enabled(
        self, strategy_id: uuid.UUID, is_enabled: bool
    ) -> TradingStrategy | None:
        """
        Toggle a strategy on or off.

        The change takes effect on the next strategy run cycle without restart.
        Returns the updated strategy, or None if not found.
        """
        strategy = await self.get_by_id(strategy_id)
        if strategy is None:
            return None
        strategy.is_enabled = is_enabled
        await self.session.flush()
        await self.session.refresh(strategy)
        return strategy

    async def update_config(
        self, strategy_id: uuid.UUID, config: dict
    ) -> TradingStrategy | None:
        """
        Replace the strategy's JSONB config entirely.

        The config dict may include any parameters plus the `symbols` array,
        e.g. {"fast_period": 50, "slow_period": 200, "symbols": ["AAPL"]}.

        Returns the updated strategy, or None if not found.
        """
        strategy = await self.get_by_id(strategy_id)
        if strategy is None:
            return None
        strategy.config = config
        await self.session.flush()
        await self.session.refresh(strategy)
        return strategy

    async def patch(
        self,
        strategy_id: uuid.UUID,
        *,
        is_enabled: bool | None = None,
        config: dict | None = None,
    ) -> TradingStrategy | None:
        """
        Apply a partial update — is_enabled and/or config.

        Used by the PATCH /api/v1/strategies/{id} endpoint.
        Returns the updated strategy, or None if not found.
        """
        strategy = await self.get_by_id(strategy_id)
        if strategy is None:
            return None
        if is_enabled is not None:
            strategy.is_enabled = is_enabled
        if config is not None:
            strategy.config = config
        await self.session.flush()
        await self.session.refresh(strategy)
        return strategy
