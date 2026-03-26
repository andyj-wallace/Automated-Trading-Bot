"""
BaseStrategy abstract class and the core domain models shared across the
strategy and risk layers.

Models defined here are intentionally small — they represent the contracts
between the strategy engine, risk manager, and order manager. No I/O or DB
logic lives here.

Used by:
  - All concrete strategy implementations (Layer 11–12)
  - RiskCalculator (Layer 6.2) — consumes RiskParams
  - OrderManager (Layer 7) — consumes Signal
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.brokers.base import PriceBar


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


class MarketData(BaseModel):
    """
    Market data snapshot passed to a strategy's generate_signal() method.

    `bars` contains historical OHLCV data (most recent bar last); the strategy
    uses this to compute indicators and decide on a signal.
    """

    symbol: str
    current_price: Decimal
    bars: list[PriceBar]
    timestamp: datetime


class RiskParams(BaseModel):
    """
    Risk sizing parameters passed to a strategy's calculate_position_size().

    The strategy uses these to compute a safe quantity within the 1% rule.
    RiskCalculator.max_quantity() provides the hard ceiling.
    """

    account_balance: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    max_risk_pct: Decimal = Decimal("0.01")  # 1% rule — not configurable


class Signal(BaseModel):
    """
    Trading signal produced by a strategy.

    A HOLD signal carries no financial fields. BUY/SELL signals must include
    entry_price, stop_loss_price, and quantity (sized by calculate_position_size).
    The RiskManager validates these before any order is submitted.
    """

    symbol: str
    action: Literal["BUY", "SELL", "HOLD"]
    entry_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    quantity: int | None = None
    strategy_id: UUID | None = None
    timestamp: datetime


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class BaseStrategy(ABC):
    """
    Common interface all trading strategies must implement.

    Strategies are registered in StrategyRegistry (Layer 11) and scheduled
    by the strategy scheduler (Layer 11). They receive market data and produce
    signals that flow through the risk layer before reaching the broker.
    """

    @abstractmethod
    async def generate_signal(self, market_data: MarketData) -> Signal:
        """
        Analyse market_data and return a trading signal.

        Must return a Signal with action="HOLD" if no trade is warranted.
        Must never raise — catch internal errors and return HOLD.
        """

    @abstractmethod
    async def calculate_position_size(self, risk_params: RiskParams) -> int:
        """
        Return the number of shares to trade, respecting the 1% risk rule.

        Implementations should call RiskCalculator.max_quantity() and return
        at most that value. Return 0 to indicate no trade.
        """

    @abstractmethod
    def get_config_schema(self) -> dict:
        """
        Return a JSON-schema dict describing the strategy's config parameters.

        Used by the frontend StrategyConfigForm to render editable fields.
        """
