"""Price subscriptions, historical bars and ticker validation for IBKRClient."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ib_async import Stock

from app.brokers.base import PriceBar, PriceFeedCallback, PriceUpdate
from app.brokers.ibkr import mapper
from app.core.tasks import spawn

# Re-emit an unchanged price after this many seconds so the Redis price key stays fresh.
_REEMIT_SECONDS = 5


def _positive_decimal(value: float | None) -> Decimal | None:
    """Convert a tick value to Decimal; None for missing, NaN, infinite or non-positive."""
    if value is None or math.isnan(value) or math.isinf(value) or value <= 0:
        return None
    return Decimal(str(value))


class IBKRMarketDataMixin:
    """Market data methods mixed into IBKRClient: price feed, history and ticker checks."""

    async def subscribe_price_feed(
        self,
        tickers: list[str],
        callback: PriceFeedCallback,
    ) -> None:
        """
        Subscribe to real-time price ticks for the given symbols.

        Registers new contracts with IBKR and attaches the callback to
        the pendingTickersEvent. Subsequent calls with the same symbols
        are idempotent (already-subscribed tickers are skipped).
        """
        self._require_connected()

        if callback not in self._price_callbacks:
            self._price_callbacks.append(callback)

        for symbol in tickers:
            if symbol not in self._subscribed_contracts:
                contract = Stock(symbol, "SMART", "USD")
                self._ib.reqMktData(contract)
                self._subscribed_contracts[symbol] = contract

        if not self._pending_tickers_handler_registered:
            self._ib.pendingTickersEvent += self._on_pending_tickers
            self._pending_tickers_handler_registered = True

    def _on_pending_tickers(self, tickers: set) -> None:
        """
        Synchronous callback fired by ib_async when ticker data updates.

        Only valid last-trade prices are forwarded; callbacks run as referenced tasks.
        """
        now = datetime.now(UTC)
        for ticker in tickers:
            price = _positive_decimal(ticker.last)
            if price is None:
                continue
            symbol = ticker.contract.symbol
            last = self._last_emitted.get(symbol)
            if last and last[0] == price and (now - last[1]).total_seconds() < _REEMIT_SECONDS:
                continue
            self._last_emitted[symbol] = (price, now)
            prev_close = _positive_decimal(ticker.close)
            update = PriceUpdate(
                ticker=symbol,
                price=price,
                bid=_positive_decimal(ticker.bid),
                ask=_positive_decimal(ticker.ask),
                day_change=(price - prev_close) if prev_close else None,
                day_change_pct=((price - prev_close) / prev_close) if prev_close else None,
                timestamp=now,
            )
            for cb in list(self._price_callbacks):
                spawn(cb(update), name=f"tick-{symbol}", registry=self._tasks)

    async def unsubscribe_price_feed(self, ticker: str) -> None:
        """Cancel a real-time market data subscription for one ticker."""
        self._require_connected()
        if ticker in self._subscribed_contracts:
            self._ib.cancelMktData(self._subscribed_contracts[ticker])
            del self._subscribed_contracts[ticker]

    async def get_historical_data(
        self,
        symbol: str,
        duration: str = "1 Y",
        bar_size: str = "1 day",
    ) -> list[PriceBar]:
        """Fetch OHLCV history from IB Gateway via reqHistoricalDataAsync."""
        self._require_connected()
        contract = Stock(symbol, "SMART", "USD")
        bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",       # empty string = now
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,          # regular trading hours only
        )
        return [mapper.map_price_bar(bar, bar_size) for bar in bars]

    async def get_intraday_bars(self, symbol: str) -> list[PriceBar]:
        """Today's completed 5-minute regular-hours bars; the still-forming bar is dropped."""
        self._require_connected()
        bars = await self._ib.reqHistoricalDataAsync(
            Stock(symbol, "SMART", "USD"),
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="5 mins",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,  # UTC datetimes; the default is exchange-local time that would be mislabelled as UTC
        )
        now = datetime.now(UTC)
        mapped = [mapper.map_price_bar(bar, "5 mins") for bar in bars]
        return [b for b in mapped if b.timestamp + timedelta(minutes=5) <= now]

    async def validate_ticker(self, symbol: str) -> bool:
        """
        Check whether the symbol resolves to a valid IBKR-tradeable stock.

        Qualifies the contract against IBKR's contract database. Returns True
        if at least one matching contract is found, False otherwise. Falls back
        to True if not connected so callers are never blocked by connectivity.
        """
        if not self._ib.isConnected():
            return True  # cannot validate without connection — allow the add
        contract = Stock(symbol.upper(), "SMART", "USD")
        try:
            details = await self._ib.reqContractDetailsAsync(contract)
            return len(details) > 0
        except Exception:
            return False
