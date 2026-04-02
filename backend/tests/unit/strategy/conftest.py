"""
Shared fixtures for strategy unit tests.

Provides helpers and pytest fixtures for constructing realistic MarketData,
PriceBar sequences, and common market scenarios (bull trend, bear trend,
flat/sideways, golden-cross setup, death-cross setup).

Usage in test files:
    def test_something(bull_market: MarketData, bear_market: MarketData):
        ...

Or using the low-level helpers directly:
    bars = make_bars(200, start_price=Decimal("100"), trend=+0.5)
    data = make_market_data("AAPL", bars)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.brokers.base import PriceBar
from app.core.strategy_engine.base import MarketData


# ---------------------------------------------------------------------------
# Low-level helpers — usable directly in test files
# ---------------------------------------------------------------------------


def make_bars(
    count: int,
    start_price: Decimal = Decimal("100"),
    trend: float = 0.0,
    volatility: float = 0.5,
    bar_size: str = "1 day",
    end_date: datetime | None = None,
) -> list[PriceBar]:
    """
    Generate a list of synthetic daily OHLCV bars.

    Args:
        count:       Number of bars to generate (most-recent bar last).
        start_price: Opening price of the first bar.
        trend:       Daily price change in dollars (positive=up, negative=down).
                     e.g. +0.5 means prices rise $0.50 per bar on average.
        volatility:  Intra-day range added around each bar's midpoint (±$).
        bar_size:    Bar granularity string, passed through to PriceBar.
        end_date:    Date of the last bar. Defaults to today UTC.
    """
    if end_date is None:
        end_date = datetime.now(timezone.utc).replace(
            hour=16, minute=0, second=0, microsecond=0
        )

    bars: list[PriceBar] = []
    price = float(start_price)

    for i in range(count):
        ts = end_date - timedelta(days=(count - 1 - i))
        open_ = price
        close = price + trend
        high = max(open_, close) + volatility
        low = min(open_, close) - volatility
        bars.append(
            PriceBar(
                timestamp=ts,
                open=Decimal(str(round(open_, 2))),
                high=Decimal(str(round(high, 2))),
                low=Decimal(str(round(low, 2))),
                close=Decimal(str(round(close, 2))),
                volume=100_000,
                bar_size=bar_size,
            )
        )
        price = close  # next bar opens at this bar's close

    return bars


def make_bars_from_closes(
    closes: list[float],
    bar_size: str = "1 day",
    end_date: datetime | None = None,
) -> list[PriceBar]:
    """
    Generate bars from an explicit list of closing prices (most-recent last).

    Useful when you need precise control over the price sequence —
    e.g. engineering a golden-cross crossover at a specific bar index.
    """
    if end_date is None:
        end_date = datetime.now(timezone.utc).replace(
            hour=16, minute=0, second=0, microsecond=0
        )

    count = len(closes)
    bars: list[PriceBar] = []

    for i, close in enumerate(closes):
        ts = end_date - timedelta(days=(count - 1 - i))
        prev_close = closes[i - 1] if i > 0 else close
        open_ = prev_close
        high = max(open_, close) + 0.5
        low = min(open_, close) - 0.5
        bars.append(
            PriceBar(
                timestamp=ts,
                open=Decimal(str(round(open_, 2))),
                high=Decimal(str(round(high, 2))),
                low=Decimal(str(round(low, 2))),
                close=Decimal(str(round(close, 2))),
                volume=100_000,
                bar_size=bar_size,
            )
        )

    return bars


def make_market_data(
    symbol: str,
    bars: list[PriceBar],
    current_price: Decimal | None = None,
) -> MarketData:
    """
    Wrap bars in a MarketData object.

    current_price defaults to the close of the last bar.
    """
    if not bars:
        raise ValueError("bars must not be empty")
    if current_price is None:
        current_price = bars[-1].close
    return MarketData(
        symbol=symbol,
        current_price=current_price,
        bars=bars,
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Pre-built scenario helpers
# ---------------------------------------------------------------------------


def _golden_cross_bars(fast: int = 50, slow: int = 200) -> list[PriceBar]:
    """
    Build a bar sequence where the fast MA just crossed above the slow MA.

    Construction:
      - First `slow` bars trend down slightly so the fast MA sits below the slow MA.
      - Final `fast` bars trend up sharply so the fast MA crosses above the slow MA.
    """
    # Downtrend base — slow MA stays high
    base = make_bars(slow, start_price=Decimal("200"), trend=-0.1, volatility=0.2)
    # Sharp reversal — fast MA pulls up above slow MA
    rally = make_bars(fast, start_price=base[-1].close, trend=0.6, volatility=0.2)
    # Stitch together, re-timestamp so dates are contiguous
    combined = base + rally
    end_date = datetime.now(timezone.utc).replace(
        hour=16, minute=0, second=0, microsecond=0
    )
    for i, bar in enumerate(combined):
        ts = end_date - timedelta(days=(len(combined) - 1 - i))
        combined[i] = bar.model_copy(update={"timestamp": ts})
    return combined


def _death_cross_bars(fast: int = 50, slow: int = 200) -> list[PriceBar]:
    """
    Build a bar sequence where the fast MA just crossed below the slow MA.
    """
    # Uptrend base — slow MA stays low
    base = make_bars(slow, start_price=Decimal("150"), trend=0.1, volatility=0.2)
    # Sharp reversal — fast MA drops below slow MA
    decline = make_bars(fast, start_price=base[-1].close, trend=-0.6, volatility=0.2)
    combined = base + decline
    end_date = datetime.now(timezone.utc).replace(
        hour=16, minute=0, second=0, microsecond=0
    )
    for i, bar in enumerate(combined):
        ts = end_date - timedelta(days=(len(combined) - 1 - i))
        combined[i] = bar.model_copy(update={"timestamp": ts})
    return combined


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def flat_market() -> MarketData:
    """200 bars with no trend — expect HOLD from most strategies."""
    bars = make_bars(200, start_price=Decimal("100"), trend=0.0, volatility=0.5)
    return make_market_data("FLAT", bars)


@pytest.fixture
def bull_market() -> MarketData:
    """200 bars in a steady uptrend."""
    bars = make_bars(200, start_price=Decimal("100"), trend=0.5, volatility=0.3)
    return make_market_data("BULL", bars)


@pytest.fixture
def bear_market() -> MarketData:
    """200 bars in a steady downtrend."""
    bars = make_bars(200, start_price=Decimal("200"), trend=-0.5, volatility=0.3)
    return make_market_data("BEAR", bars)


@pytest.fixture
def golden_cross_market() -> MarketData:
    """
    A bar sequence engineering a 50/200 golden cross.

    The 50-day MA has just crossed above the 200-day MA — a BUY signal for
    a moving-average crossover strategy.
    """
    bars = _golden_cross_bars(fast=50, slow=200)
    return make_market_data("GOLD", bars)


@pytest.fixture
def death_cross_market() -> MarketData:
    """
    A bar sequence engineering a 50/200 death cross.

    The 50-day MA has just crossed below the 200-day MA — a SELL signal.
    """
    bars = _death_cross_bars(fast=50, slow=200)
    return make_market_data("DEAD", bars)


@pytest.fixture
def insufficient_bars_market() -> MarketData:
    """Only 10 bars — strategies requiring 200+ should return HOLD."""
    bars = make_bars(10, start_price=Decimal("100"), trend=0.5)
    return make_market_data("SHORT", bars)
