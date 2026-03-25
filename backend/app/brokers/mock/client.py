"""
MockBroker — a fully in-memory broker for development and testing.

Simulates connection state, returns synthetic account/position data,
and echoes all orders back as immediately filled. No live connection required.

Used automatically when ENVIRONMENT=development (see app/dependencies.py).
"""

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.brokers.base import (
    AccountSummary,
    BaseBroker,
    OrderRequest,
    OrderResult,
    Position,
    PriceBar,
    PriceFeedCallback,
    PriceUpdate,
)

_MOCK_ACCOUNT_ID = "DU999999"  # paper trading account format
_MOCK_BALANCE = Decimal("100_000.00")
_MOCK_BUYING_POWER = Decimal("200_000.00")  # 2× for margin accounts


class MockBroker(BaseBroker):
    """
    In-memory broker for development and testing.

    All operations succeed without a live connection. Orders are echoed back
    as FILLED at a synthetic price. subscribe_price_feed() registers the
    callback but does not generate ticks — Layer 5 (MarketDataFeed) drives
    the feed loop and can call the callback directly in tests.
    """

    def __init__(self) -> None:
        self._connected = False
        self._next_order_id = 1
        self._price_callbacks: list[PriceFeedCallback] = []

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._price_callbacks.clear()

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Account & positions
    # ------------------------------------------------------------------

    async def get_account_summary(self) -> AccountSummary:
        self._require_connected()
        return AccountSummary(
            account_id=_MOCK_ACCOUNT_ID,
            net_liquidation=_MOCK_BALANCE,
            cash_balance=_MOCK_BALANCE,
            buying_power=_MOCK_BUYING_POWER,
            gross_position_value=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            realized_pnl=Decimal("0.00"),
            currency="USD",
        )

    async def get_positions(self) -> list[Position]:
        self._require_connected()
        return []

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def subscribe_price_feed(
        self,
        tickers: list[str],
        callback: PriceFeedCallback,
    ) -> None:
        self._require_connected()
        self._price_callbacks.append(callback)
        # No ticks are pushed here — tests drive callbacks directly via
        # simulate_price_tick() or the MarketDataFeed layer.

    async def simulate_price_tick(self, ticker: str, price: Decimal) -> None:
        """
        Test helper: push a synthetic PriceUpdate to all registered callbacks.
        Not part of BaseBroker — only available on MockBroker.
        """
        update = PriceUpdate(
            ticker=ticker,
            price=price,
            timestamp=datetime.now(timezone.utc),
        )
        for cb in self._price_callbacks:
            await cb(update)

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def place_order(self, order: OrderRequest) -> OrderResult:
        self._require_connected()

        broker_order_id = str(self._next_order_id)
        self._next_order_id += 1

        # Echo back as a market fill at a synthetic price.
        # For LMT orders, use the provided limit price; otherwise use a
        # placeholder. The actual fill price is not meaningful in the mock.
        if order.order_type == "LMT" and order.limit_price is not None:
            fill_price = order.limit_price
        else:
            fill_price = Decimal("100.00")  # synthetic MKT fill price

        return OrderResult(
            trade_id=order.trade_id,
            broker_order_id=broker_order_id,
            status="FILLED",
            filled_quantity=order.quantity,
            avg_fill_price=fill_price,
            timestamp=datetime.now(timezone.utc),
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        self._require_connected()
        return True  # mock always accepts cancellations

    # ------------------------------------------------------------------
    # Price feed management
    # ------------------------------------------------------------------

    async def unsubscribe_price_feed(self, ticker: str) -> None:
        self._require_connected()
        # No-op in the mock — subscriptions are callback lists, not per-ticker

    # ------------------------------------------------------------------
    # Historical data
    # ------------------------------------------------------------------

    async def get_historical_data(
        self,
        symbol: str,
        duration: str = "1 Y",
        bar_size: str = "1 day",
    ) -> list[PriceBar]:
        """
        Generate synthetic daily OHLCV bars for the requested duration.

        Produces a simple random-walk price series seeded on the symbol name
        so output is stable across calls for the same symbol.
        """
        self._require_connected()

        rng = random.Random(hash(symbol) & 0xFFFFFFFF)
        end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=365)

        bars: list[PriceBar] = []
        price = Decimal("100.00")
        current = start

        while current <= end:
            if current.weekday() < 5:  # skip weekends
                change = Decimal(str(rng.uniform(-0.015, 0.018)))
                close = (price * (1 + change)).quantize(Decimal("0.01"))
                high = (max(price, close) * Decimal("1.005")).quantize(Decimal("0.01"))
                low = (min(price, close) * Decimal("0.995")).quantize(Decimal("0.01"))
                bars.append(
                    PriceBar(
                        timestamp=current,
                        open=price,
                        high=high,
                        low=low,
                        close=close,
                        volume=rng.randint(1_000_000, 10_000_000),
                        bar_size=bar_size,
                    )
                )
                price = close
            current += timedelta(days=1)

        return bars

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError(
                "MockBroker is not connected. Call connect() first."
            )
