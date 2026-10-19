"""
MarketDataFeed — manages live price subscriptions for all active watched symbols.

sync() subscribes the broker's price feed to every symbol the bot needs. Each tick:
  1. Goes to in-process listeners first (PositionMonitor)
  2. Is written to Redis as  price:{ticker}  (latest price cache, no TTL)
  3. Is published as a price_update envelope on  watchlist_prices  (WebSocket)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime

from app.brokers.base import BaseBroker, PriceUpdate
from app.core.events import WATCHLIST_PRICES, envelope
from app.data.cache import RedisCache
from app.db.repositories.strategy_repo import StrategyRepo
from app.db.repositories.symbol_repo import SymbolRepo
from app.db.repositories.trade_repo import TradeRepo
from app.monitoring.logger import system_logger

# Redis key format for the latest price of a ticker
_PRICE_KEY = "price:{ticker}"

# Redis pub/sub channel for live price updates (consumed by WebSocket handler)
WATCHLIST_PRICES_CHANNEL = WATCHLIST_PRICES

PriceListener = Callable[[PriceUpdate], Awaitable[None]]


class MarketDataFeed:
    """
    Subscribes to live price ticks for every symbol the bot needs and forwards
    them to in-process listeners and Redis.

    Desired symbols = active watchlist + symbols of enabled strategies +
    symbols with an active trade. sync() reconciles subscriptions to that set.
    """

    def __init__(self, broker: BaseBroker, cache: RedisCache, session_factory) -> None:
        self._broker = broker
        self._cache = cache
        self._session_factory = session_factory
        self._subscribed: set[str] = set()
        self._listeners: list[PriceListener] = []
        self._last_tick_at: dict[str, datetime] = {}

    def add_listener(self, fn: PriceListener) -> None:
        """Register an in-process price listener; called before any Redis write."""
        self._listeners.append(fn)

    def last_tick_at(self, symbol: str) -> datetime | None:
        return self._last_tick_at.get(symbol.upper())

    async def desired_symbols(self) -> set[str]:
        async with self._session_factory() as session:
            watchlist = await SymbolRepo(session).get_all(active_only=True)
            strategies = await StrategyRepo(session).get_enabled()
            active = await TradeRepo(session).get_active_trades()
        symbols = {s.ticker.upper() for s in watchlist}
        for strategy in strategies:
            symbols.update(str(sym).upper() for sym in (strategy.config or {}).get("symbols", []))
        symbols.update(t.symbol.upper() for t in active)
        return symbols

    async def sync(self) -> None:
        """Subscribe new desired symbols and unsubscribe ones no longer needed."""
        if not self._broker.is_connected():
            return
        desired = await self.desired_symbols()
        for ticker in sorted(desired - self._subscribed):
            await self.add_symbol(ticker)
        for ticker in sorted(self._subscribed - desired):
            await self.remove_symbol(ticker)

    # ------------------------------------------------------------------
    # Dynamic symbol management
    # ------------------------------------------------------------------

    async def add_symbol(self, ticker: str) -> None:
        """
        Subscribe to live prices for a newly added symbol.

        Safe to call if already subscribed — will be a no-op.
        """
        ticker = ticker.upper()
        if ticker in self._subscribed:
            return
        await self._broker.subscribe_price_feed([ticker], self._on_price_update)
        self._subscribed.add(ticker)
        system_logger.info("MarketDataFeed: added symbol", extra={"ticker": ticker})

    async def remove_symbol(self, ticker: str) -> None:
        """
        Unsubscribe from live prices for a removed symbol and clear its
        cached price from Redis.

        Safe to call if not subscribed — will be a no-op.
        """
        ticker = ticker.upper()
        if ticker not in self._subscribed:
            return
        await self._broker.unsubscribe_price_feed(ticker)
        self._subscribed.discard(ticker)
        await self._cache.delete(_PRICE_KEY.format(ticker=ticker))
        system_logger.info("MarketDataFeed: removed symbol", extra={"ticker": ticker})

    @property
    def subscribed_tickers(self) -> frozenset[str]:
        """Return the current set of subscribed ticker symbols."""
        return frozenset(self._subscribed)

    # ------------------------------------------------------------------
    # Price tick handler
    # ------------------------------------------------------------------

    async def _on_price_update(self, update: PriceUpdate) -> None:
        """
        Handle one tick: in-process listeners first, then the Redis key and
        the price_update envelope. Failures are logged, never propagated.
        """
        symbol = update.ticker.upper()
        self._last_tick_at[symbol] = update.timestamp
        for listener in self._listeners:
            try:
                await listener(update)
            except Exception:
                system_logger.exception("MarketDataFeed: listener failed", extra={"ticker": symbol})
        try:
            await self._cache.set(
                _PRICE_KEY.format(ticker=symbol),
                json.dumps(
                    {"price": str(update.price), "timestamp": update.timestamp.isoformat()}
                ),
            )
            change = str(update.day_change_pct) if update.day_change_pct is not None else None
            await self._cache.publish(
                WATCHLIST_PRICES,
                envelope(
                    "price_update",
                    {"symbol": symbol, "price": str(update.price), "change_pct": change},
                ),
            )
        except Exception as exc:
            system_logger.error(
                "MarketDataFeed: failed to forward price update",
                extra={"ticker": symbol, "error": str(exc)},
            )
