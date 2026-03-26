"""
TradeHandler — post-fill callback that persists a trade to the DB and
publishes a trade event to Redis for the dashboard WebSocket.

Called by the orchestration layer after OrderManager.submit_order() returns
a FILLED or PARTIAL result. Never called for REJECTED or ERROR results.

Depends on: OrderResult/ValidationResult (Layer 7.1), TradeRepo (Layer 3.7),
            RedisCache (Layer 5.1)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import OrderResult
from app.core.risk.calculator import RiskCalculator
from app.core.risk.manager import TradeRequest, ValidationResult
from app.data.cache import RedisCache
from app.db.models.trade import TradeDirection
from app.db.repositories.trade_repo import TradeRepo
from app.monitoring.logger import trading_logger

# Redis channel for trade events — consumed by WebSocket handler (Layer 8)
TRADE_EVENTS_CHANNEL = "trade_events"

_calculator = RiskCalculator()


class TradeHandler:
    """
    Persists a confirmed trade to the database and broadcasts the trade event
    to all connected dashboard clients via Redis pub/sub.

    Usage:
        handler = TradeHandler(cache)
        async with AsyncSessionFactory() as session:
            trade = await handler.on_fill(request, validation, order_result, session)
            await session.commit()
    """

    def __init__(self, cache: RedisCache) -> None:
        self._cache = cache

    async def on_fill(
        self,
        request: TradeRequest,
        validation: ValidationResult,
        order_result: OrderResult,
        session: AsyncSession,
    ):
        """
        Persist a filled trade and publish the trade event to Redis.

        Args:
            request:      The original trade request (symbol, stop-loss, etc.).
            validation:   Risk validation result (account balance snapshot).
            order_result: Broker confirmation (actual fill price/qty).
            session:      Active DB session. Caller must commit after returning.

        Returns:
            The persisted Trade ORM object.

        Raises:
            ValueError: If called with a non-fill status (programming error).
        """
        if order_result.status not in ("FILLED", "PARTIAL"):
            raise ValueError(
                f"on_fill called with non-fill status '{order_result.status}' "
                f"for trade {request.trade_id}. Only call on_fill for FILLED/PARTIAL orders."
            )

        # Recalculate risk based on actual fill price (may differ from requested)
        try:
            actual_risk = _calculator.risk_amount(
                quantity=order_result.filled_quantity,
                entry_price=order_result.avg_fill_price,
                stop_loss_price=request.stop_loss_price,  # type: ignore[arg-type]
            )
        except Exception:
            # Fallback: use the pre-validated risk amount if recalculation fails
            actual_risk = validation.risk_amount

        # ------------------------------------------------------------------
        # Persist trade to DB
        # ------------------------------------------------------------------
        repo = TradeRepo(session)
        trade = await repo.create(
            symbol=request.symbol,
            direction=TradeDirection(request.direction),
            quantity=order_result.filled_quantity,
            entry_price=order_result.avg_fill_price,
            stop_loss_price=request.stop_loss_price,  # type: ignore[arg-type]
            risk_amount=actual_risk,
            account_balance_at_entry=validation.account_balance_at_entry,
            strategy_id=request.strategy_id,
        )

        # ------------------------------------------------------------------
        # Publish trade event to Redis
        # ------------------------------------------------------------------
        event = {
            "event": "trade_executed",
            "payload": {
                "trade_id": str(trade.id),
                "symbol": trade.symbol,
                "direction": trade.direction.value,
                "quantity": str(trade.quantity),
                "entry_price": str(trade.entry_price),
                "stop_loss_price": str(trade.stop_loss_price),
                "risk_amount": str(trade.risk_amount),
                "status": trade.status.value,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._cache.publish(TRADE_EVENTS_CHANNEL, json.dumps(event))

        trading_logger.info(
            "Trade persisted and published",
            extra={
                "trade_id": str(trade.id),
                "symbol": trade.symbol,
                "direction": trade.direction.value,
                "broker_order_id": order_result.broker_order_id,
            },
        )

        return trade
