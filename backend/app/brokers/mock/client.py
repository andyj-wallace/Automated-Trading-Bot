"""
MockBroker — a fully in-memory broker for development and testing.

Simulates connection state and synthetic account data. Market orders fill
immediately at the last simulated price per symbol and update positions. No live connection required.

Used automatically when ENVIRONMENT=development (see app/dependencies.py).
"""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

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
        self.last_prices: dict[str, Decimal] = {}
        self.intraday_bars: dict[str, list[PriceBar]] = {}  # tests fill this
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, OrderRequest] = {}
        self.order_results: dict[str, OrderResult] = {}
        self.fills: list[tuple[str, Decimal, Decimal]] = []
        self._ref_by_order_id: dict[str, str] = {}
        self.placed_orders: list[OrderRequest] = []
        # One-shot test toggles, cleared after the next place_order().
        self.next_status: str | None = None  # "WORKING" | "PARTIAL" | "REJECTED" | "ERROR"
        self.next_filled_quantity: Decimal | None = None
        self.raise_after_next_order: bool = False  # record the order, then raise

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
            settled_cash=_MOCK_BALANCE,
            available_funds=_MOCK_BUYING_POWER,
            day_trades_remaining=-1,
        )

    async def get_positions(self) -> list[Position]:
        self._require_connected()
        return list(self.positions.values())

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def subscribe_price_feed(
        self,
        tickers: list[str],
        callback: PriceFeedCallback,
    ) -> None:
        self._require_connected()
        if callback not in self._price_callbacks:
            self._price_callbacks.append(callback)
        # No ticks are pushed here — tests drive callbacks directly via
        # simulate_price_tick() or the MarketDataFeed layer.

    async def simulate_price_tick(
        self, ticker: str, price: Decimal, bid: Decimal | None = None, ask: Decimal | None = None
    ) -> None:
        """
        Test helper: push a synthetic PriceUpdate to all registered callbacks.
        Not part of BaseBroker — only available on MockBroker.
        """
        ticker = ticker.upper()
        self.last_prices[ticker] = price
        update = PriceUpdate(
            ticker=ticker,
            price=price,
            bid=bid,
            ask=ask,
            timestamp=datetime.now(UTC),
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

        # Fill at the limit price for LMT orders, otherwise at the last simulated
        # price for the symbol (fallback 100.00).
        symbol = order.symbol.upper()
        if order.order_type == "LMT" and order.limit_price is not None:
            fill_price = order.limit_price
        else:
            fill_price = self.last_prices.get(symbol, Decimal("100.00"))

        status = self.next_status or ("WORKING" if order.order_type == "STP" else "FILLED")
        if status == "PARTIAL":
            filled = self.next_filled_quantity if self.next_filled_quantity is not None else (
                order.quantity / 2
            )
        elif status == "FILLED":
            filled = order.quantity
        else:
            filled = Decimal("0")
        raise_after = self.raise_after_next_order
        self.next_status = None
        self.next_filled_quantity = None
        self.raise_after_next_order = False

        self.placed_orders.append(order)
        if order.order_ref:
            self.orders[order.order_ref] = order
        if filled > 0:
            self._apply_fill(symbol, order.direction, filled, fill_price)
            self.fills.append((order.order_ref, filled, fill_price))

        result = OrderResult(
            trade_id=order.trade_id,
            broker_order_id=broker_order_id,
            status=status,  # type: ignore[arg-type]
            filled_quantity=filled,
            avg_fill_price=fill_price if filled > 0 else Decimal("0"),
            order_ref=order.order_ref,
            timestamp=datetime.now(UTC),
        )
        if order.order_ref:
            self.order_results[order.order_ref] = result
            self._ref_by_order_id[broker_order_id] = order.order_ref
        if raise_after:
            raise ConnectionError("simulated connection loss after order was recorded")
        return result

    async def find_order_by_ref(self, order_ref: str) -> OrderResult | None:
        self._require_connected()
        return self.order_results.get(order_ref)

    async def get_execution_summary(self, order_ref: str) -> OrderResult | None:
        self._require_connected()
        matching = [(q, p) for ref, q, p in self.fills if ref == order_ref]
        if not matching:
            return None
        total = sum((q for q, _ in matching), Decimal("0"))
        avg = sum((q * p for q, p in matching), Decimal("0")) / total
        latest = self.order_results[order_ref]
        return OrderResult(
            trade_id=latest.trade_id,
            broker_order_id=latest.broker_order_id,
            status="FILLED",
            filled_quantity=total,
            avg_fill_price=avg,
            order_ref=order_ref,
            timestamp=datetime.now(UTC),
        )

    def _apply_fill(
        self, symbol: str, direction: str, quantity: Decimal, price: Decimal
    ) -> None:
        """Update the per-symbol position for a fill (BUY adds, SELL subtracts)."""
        current = self.positions.get(symbol)
        held = current.quantity if current else Decimal("0")
        cost = current.average_cost if current else price
        new_qty = held + quantity if direction == "BUY" else held - quantity
        if new_qty == 0:
            self.positions.pop(symbol, None)
            return
        if direction == "BUY" and held > 0:
            cost = (held * cost + quantity * price) / new_qty
        elif direction == "BUY":
            cost = price
        self.positions[symbol] = Position(
            account_id=_MOCK_ACCOUNT_ID,
            symbol=symbol,
            quantity=new_qty,
            average_cost=cost,
            market_price=price,
            market_value=new_qty * price,
            unrealized_pnl=new_qty * (price - cost),
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a WORKING order (it then reads as REJECTED); False for unknown/finished orders."""
        self._require_connected()
        ref = self._ref_by_order_id.get(broker_order_id)
        result = self.order_results.get(ref) if ref else None
        if result is None or result.status != "WORKING":
            return False
        self.order_results[ref] = result.model_copy(update={"status": "REJECTED"})
        return True

    # ------------------------------------------------------------------
    # Test helpers (not part of BaseBroker)
    # ------------------------------------------------------------------

    def set_position(
        self, symbol: str, quantity: Decimal, average_cost: Decimal | None = None
    ) -> None:
        """Set the broker position for a symbol directly (0 removes it)."""
        symbol = symbol.upper()
        if quantity == 0:
            self.positions.pop(symbol, None)
            return
        price = self.last_prices.get(symbol, average_cost or Decimal("100.00"))
        cost = average_cost if average_cost is not None else price
        self.positions[symbol] = Position(
            account_id=_MOCK_ACCOUNT_ID,
            symbol=symbol,
            quantity=quantity,
            average_cost=cost,
            market_price=price,
            market_value=quantity * price,
            unrealized_pnl=quantity * (price - cost),
        )

    def set_order_state(
        self,
        order_ref: str,
        status: str,
        filled_quantity: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        """
        Force the broker-side state of an order ref (creates it if unknown).

        A larger filled_quantity than before records the difference as a fill so
        get_execution_summary() reflects it. Positions are not touched; use
        set_position() for that.
        """
        previous = self.order_results.get(order_ref)
        prev_filled = previous.filled_quantity if previous else Decimal("0")
        new_filled = filled_quantity if filled_quantity is not None else prev_filled
        price = avg_fill_price if avg_fill_price is not None else (
            previous.avg_fill_price if previous else Decimal("0")
        )
        if new_filled > prev_filled:
            self.fills.append((order_ref, new_filled - prev_filled, price))
        order_id = broker_order_id or (previous.broker_order_id if previous else "")
        if not order_id:
            order_id = str(self._next_order_id)
            self._next_order_id += 1
        self._ref_by_order_id[order_id] = order_ref
        self.order_results[order_ref] = OrderResult(
            trade_id=UUID(order_ref.split(":")[0]),
            broker_order_id=order_id,
            status=status,  # type: ignore[arg-type]
            filled_quantity=new_filled,
            avg_fill_price=price,
            order_ref=order_ref,
            timestamp=datetime.now(UTC),
        )

    def trigger_stop(self, order_ref: str, price: Decimal) -> None:
        """Fill a resting STP order at `price` and reduce the position accordingly."""
        order = self.orders[order_ref]
        result = self.order_results[order_ref]
        remaining = order.quantity - result.filled_quantity
        self._apply_fill(order.symbol.upper(), order.direction, remaining, price)
        self.set_order_state(order_ref, "FILLED", order.quantity, price)

    # ------------------------------------------------------------------
    # Price feed management
    # ------------------------------------------------------------------

    async def unsubscribe_price_feed(self, ticker: str) -> None:
        self._require_connected()
        # No-op in the mock — subscriptions are callback lists, not per-ticker

    # ------------------------------------------------------------------
    # Ticker validation
    # ------------------------------------------------------------------

    async def validate_ticker(self, symbol: str) -> bool:
        """Accept any non-empty ticker in mock mode — no live lookup needed."""
        return bool(symbol and symbol.strip())

    # ------------------------------------------------------------------
    # Historical data
    # ------------------------------------------------------------------

    async def get_intraday_bars(self, symbol: str) -> list[PriceBar]:
        return self.intraday_bars.get(symbol, [])

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
        end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
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
