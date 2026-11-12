from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.brokers.base import PriceBar
from app.core import market_calendar
from app.monitoring.logger import system_logger

_HISTORY_LOOKBACK_DAYS = 365  # 1 year of daily bars


class SchedulerDataMixin:
    """Market data helpers for StrategyScheduler: prices, spreads and DB or intraday bars."""

    async def _spread_bps(self, symbol: str) -> Decimal | None:
        """Bid/ask spread in basis points from the price key; None when no usable quote is cached."""
        try:
            raw = await self._cache.get(f"price:{symbol}")
            payload = json.loads(raw) if raw else {}
            bid, ask = Decimal(str(payload["bid"])), Decimal(str(payload["ask"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None
        if bid <= 0 or ask < bid:
            return None
        return (ask - bid) / ((ask + bid) / 2) * Decimal("10000")

    # ------------------------------------------------------------------
    # Internal: data helpers
    # ------------------------------------------------------------------

    async def _load_enabled_strategies(self):
        """Return enabled TradingStrategy rows from the DB."""
        try:
            from app.db.repositories.strategy_repo import StrategyRepo

            async with self._session_factory() as session:
                return await StrategyRepo(session).get_enabled()
        except Exception as exc:
            system_logger.error(
                "StrategyScheduler: failed to load strategies from DB",
                extra={"error": str(exc)},
            )
            return []

    async def _get_current_price(self, symbol: str) -> Decimal | None:
        """Price from Redis price:{symbol}, only if its timestamp is fresh."""
        try:
            raw = await self._cache.get(f"price:{symbol}")
            if not raw:
                return None
            payload = json.loads(raw)
            if not isinstance(payload, dict) or "timestamp" not in payload:
                return None
            stamp = datetime.fromisoformat(payload["timestamp"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - stamp).total_seconds()
            if age > self._price_stale_seconds:
                return None
            return Decimal(str(payload["price"]))
        except Exception as exc:
            system_logger.warning(
                "StrategyScheduler: Redis price lookup failed",
                extra={"symbol": symbol, "error": str(exc)},
            )
            return None

    async def _fetch_db_bars(self, symbol: str, trading_day: date) -> list[PriceBar]:
        """Completed daily sessions (strictly before trading_day) from the DB."""
        from app.db.repositories.ohlcv_repo import OHLCVRepo

        end = datetime.now(UTC)
        start = end - timedelta(days=_HISTORY_LOOKBACK_DAYS)
        async with self._session_factory() as session:
            rows = await OHLCVRepo(session).get_bars(symbol, start=start, end=end)
        return [
            PriceBar(
                timestamp=row.time,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                bar_size=row.bar_size,
            )
            for row in rows
            if row.time.date() < trading_day
        ]

    async def _get_intraday_bars(self, symbol: str) -> list[PriceBar]:
        """Today's completed 5-minute bars, fetched at most once per symbol per 5-minute bucket (IB pacing)."""
        now = self._clock().astimezone(UTC)
        bucket = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
        key = (symbol, bucket)
        if key not in self._intraday_cache:
            try:
                fetched = await self._broker.get_intraday_bars(symbol)
            except Exception as exc:
                system_logger.warning(
                    "StrategyScheduler: intraday bar fetch failed",
                    extra={"symbol": symbol, "error": str(exc)},
                )
                return []
            today = market_calendar.trading_date(now)
            for old in [k for k in self._intraday_cache if k[1] != bucket]:
                del self._intraday_cache[old]
            self._intraday_cache[key] = [
                b for b in fetched if market_calendar.trading_date(b.timestamp) == today
            ]
        return self._intraday_cache[key]

    def invalidate_bars(self, symbol: str) -> None:
        """Drop cached daily bars for symbol so the next cycle re-reads the DB."""
        key = symbol.upper()
        self._bar_generation[key] = self._bar_generation.get(key, 0) + 1
        for cached_key in [k for k in self._bar_cache if k[0] == key]:
            del self._bar_cache[cached_key]

    async def _get_historical_bars(self, symbol: str) -> list[PriceBar]:
        """Completed-session bars from the DB; [] if missing or stale."""
        trading_day = market_calendar.trading_date(self._clock())
        key = (symbol, trading_day)
        cached = self._bar_cache.get(key)
        if cached is not None:
            return cached
        generation = self._bar_generation.get(symbol, 0)
        try:
            bars = await self._fetch_db_bars(symbol, trading_day)
        except Exception as exc:
            system_logger.warning(
                "StrategyScheduler: DB bar fetch failed",
                extra={"symbol": symbol, "error": str(exc)},
            )
            return []
        if not bars:
            self._warn_once("no_bars", symbol, trading_day,
                            "StrategyScheduler: no completed bars for symbol — skipping")
            return []
        if bars[-1].timestamp.date() < market_calendar.previous_trading_day(trading_day):
            self._warn_once("stale_bars", symbol, trading_day,
                            "StrategyScheduler: bars are stale — skipping")
            return []
        for old in [k for k in self._bar_cache if k[1] != trading_day]:
            del self._bar_cache[old]
        if self._bar_generation.get(symbol, 0) == generation:
            self._bar_cache[key] = bars
        return bars
