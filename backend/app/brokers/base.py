"""
Broker abstraction layer.

Internal Pydantic models and the BaseBroker abstract interface.
All strategy and execution code depends on these types — never on broker-specific objects.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Awaitable, Callable, Literal
from uuid import UUID

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Internal Pydantic models
# ---------------------------------------------------------------------------


class AccountSummary(BaseModel):
    account_id: str
    net_liquidation: Decimal
    cash_balance: Decimal
    buying_power: Decimal
    gross_position_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    currency: str = "USD"


class Position(BaseModel):
    account_id: str
    symbol: str
    quantity: Decimal
    average_cost: Decimal
    market_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal


class OrderRequest(BaseModel):
    trade_id: UUID
    symbol: str
    direction: Literal["BUY", "SELL"]
    quantity: Decimal
    order_type: Literal["MKT", "LMT"] = "MKT"
    limit_price: Decimal | None = None
    stop_loss_price: Decimal  # required — enforced upstream by risk layer
    strategy_id: UUID | None = None


class OrderResult(BaseModel):
    trade_id: UUID
    broker_order_id: str
    status: Literal["FILLED", "PARTIAL", "REJECTED", "ERROR"]
    filled_quantity: Decimal
    avg_fill_price: Decimal
    error_message: str | None = None
    timestamp: datetime


class PriceUpdate(BaseModel):
    ticker: str
    price: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    day_change: Decimal | None = None
    day_change_pct: Decimal | None = None
    timestamp: datetime


class PriceBar(BaseModel):
    """One OHLCV bar returned by the broker's historical data feed."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    bar_size: str = "1 day"


# ---------------------------------------------------------------------------
# Abstract broker interface
# ---------------------------------------------------------------------------

PriceFeedCallback = Callable[[PriceUpdate], Awaitable[None]]


class BaseBroker(ABC):
    """
    Abstract interface for all broker implementations.

    All business logic must depend on this interface — never on IBKRClient
    or MockBroker directly. The concrete implementation is injected via
    app/dependencies.py::get_broker().
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the broker."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the broker connection."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if currently connected."""

    @abstractmethod
    async def get_account_summary(self) -> AccountSummary:
        """Return current account balances and P&L summary."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return all currently open positions."""

    @abstractmethod
    async def subscribe_price_feed(
        self,
        tickers: list[str],
        callback: PriceFeedCallback,
    ) -> None:
        """
        Subscribe to real-time price updates for the given tickers.
        The callback is invoked for each tick received.
        """

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """
        Submit an order to the broker and return the result.
        Waits for acknowledgment/fill before returning.
        """

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """
        Cancel a pending order by broker order ID.
        Returns True if the cancellation was accepted.
        """

    @abstractmethod
    async def unsubscribe_price_feed(self, ticker: str) -> None:
        """Remove a single ticker from the active price subscription."""

    @abstractmethod
    async def validate_ticker(self, symbol: str) -> bool:
        """
        Return True if the symbol is a valid tradeable security.

        Used by the POST /api/v1/symbols endpoint before persisting a new
        watchlist entry. Implementations that cannot validate (e.g. mock)
        should return True.
        """

    @abstractmethod
    async def get_historical_data(
        self,
        symbol: str,
        duration: str = "1 Y",
        bar_size: str = "1 day",
    ) -> list[PriceBar]:
        """
        Fetch OHLCV bars for the given symbol.

        Args:
            symbol:   Ticker symbol, e.g. "AAPL".
            duration: How far back to fetch. IBKR duration string format:
                      "1 Y", "6 M", "3 M", "1 W", "1 D".
            bar_size: Granularity of each bar. IBKR bar size format:
                      "1 day", "1 hour", "30 mins", "5 mins", "1 min".
        """
