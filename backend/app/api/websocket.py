"""
WebSocket endpoint for real-time dashboard updates.

WS /ws/dashboard

Multiplexes three Redis pub/sub channels over a single WebSocket connection:

  Channel            Publisher              Consumer
  -----------------  ---------------------  --------------------
  trade_events       TradeHandler           ActiveTradesTable
  risk_updates       RiskMonitor (Layer 14) RiskGauge
  watchlist_prices   MarketDataFeed         WatchlistPanel

Each forwarded message is already a JSON string — publishers are responsible
for serialisation before calling redis.publish(). The WebSocket handler is a
thin forwarder only; no transformation is applied here.

When the client disconnects, all three subscription tasks are cancelled.
The RedisCache.subscribe() generator's finally-block unsubscribes and closes
the pubsub connection automatically on cancellation.
"""

import asyncio

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.data.cache import RedisCache
from app.dependencies import get_cache

router = APIRouter()

_DASHBOARD_CHANNELS = ("trade_events", "risk_updates", "watchlist_prices")


@router.websocket("/ws/dashboard")
async def dashboard_websocket(
    websocket: WebSocket,
    cache: RedisCache = Depends(get_cache),
) -> None:
    """
    Accept a WebSocket connection and stream all dashboard events.

    One asyncio Task is created per Redis channel. Each task iterates the
    RedisCache.subscribe() generator and forwards messages directly to the
    client. When the client disconnects (WebSocketDisconnect), all tasks are
    cancelled and their Redis subscriptions are cleaned up.
    """
    await websocket.accept()

    async def forward(channel: str) -> None:
        """Subscribe to one Redis channel and push messages to the WebSocket."""
        async for message in cache.subscribe(channel):
            try:
                await websocket.send_text(message)
            except Exception:
                # WebSocket closed or errored — stop forwarding this channel
                return

    tasks = [asyncio.create_task(forward(ch)) for ch in _DASHBOARD_CHANNELS]

    try:
        # Block here until the client disconnects.
        # Consume (and discard) any messages the client might send.
        async for _ in websocket.iter_text():
            pass
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        # Wait for all tasks to finish cleanup (unsubscribe from Redis)
        await asyncio.gather(*tasks, return_exceptions=True)
