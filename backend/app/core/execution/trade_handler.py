"""
TradeHandler — post-fill callback that publishes a trade event to Redis.

After Layer 6B.8, trade rows are created at PENDING by OrderManager and
transitioned through SUBMITTED → OPEN before on_fill() is called. This
handler looks up the existing trade and publishes the trade_executed event
for the dashboard WebSocket and PositionMonitor.

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
from app.core.risk.manager import TradeRequest, ValidationResult
from app.data.cache import RedisCache
from app.db.repositories.trade_repo import TradeRepo
from app.monitoring.logger import trading_logger

# Redis channel for trade events — consumed by WebSocket handler (Layer 8)
TRADE_EVENTS_CHANNEL = "trade_events"


class TradeHandler:
    """
    Publishes a confirmed trade event to all connected dashboard clients
    via Redis pub/sub.

    The trade row is expected to already exist in the DB (created at PENDING
    by OrderManager and transitioned to OPEN on fill). This handler looks
    it up by trade_id and publishes the event.

    Usage:
        handler = TradeHandler(cache)
        async with AsyncSessionFactory() as session:
            trade = await handler.on_fill(request, validation, order_result, session)
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
        Look up the confirmed trade and publish the trade event to Redis.

        Args:
            request:      The original trade request (symbol, stop-loss, etc.).
            validation:   Risk validation result (includes take_profit_price).
            order_result: Broker confirmation (actual fill price/qty).
            session:      Active DB session for looking up the trade.

        Returns:
            The Trade ORM object.

        Raises:
            ValueError: If called with a non-fill status (programming error).
        """
        if order_result.status not in ("FILLED", "PARTIAL"):
            raise ValueError(
                f"on_fill called with non-fill status '{order_result.status}' "
                f"for trade {request.trade_id}. Only call on_fill for FILLED/PARTIAL orders."
            )

        # ------------------------------------------------------------------
        # Look up the trade (created at PENDING by OrderManager, now OPEN)
        # ------------------------------------------------------------------
        repo = TradeRepo(session)
        trade = await repo.get_by_id(request.trade_id)

        if trade is None:
            raise RuntimeError(
                f"Trade {request.trade_id} not found in DB. "
                "OrderManager must create the trade row before on_fill is called."
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
                "quantity": str(order_result.filled_quantity),
                "entry_price": str(order_result.avg_fill_price),
                "stop_loss_price": str(request.stop_loss_price),
                "take_profit_price": str(validation.take_profit_price),
                "risk_amount": str(validation.risk_amount),
                "reward_to_risk_ratio": str(validation.reward_to_risk_ratio),
                "status": trade.status.value,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._cache.publish(TRADE_EVENTS_CHANNEL, json.dumps(event))

        trading_logger.info(
            "Trade event published",
            extra={
                "trade_id": str(trade.id),
                "symbol": trade.symbol,
                "direction": trade.direction.value,
                "broker_order_id": order_result.broker_order_id,
            },
        )

        return trade
