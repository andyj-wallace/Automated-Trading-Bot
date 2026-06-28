"""
Unit tests for StrategyScheduler price-lookup fallback and the
consecutive-miss escalation behavior added to address the gap where a
symbol with no price data was skipped indefinitely with no escalation.
"""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.brokers.base import PriceBar
from app.core.strategy_engine.base import Signal
from app.core.strategy_engine.scheduler import (
    _CONSECUTIVE_MISS_ESCALATION_THRESHOLD,
    StrategyScheduler,
)


def _bar(close: str) -> PriceBar:
    from datetime import UTC, datetime

    return PriceBar(
        timestamp=datetime.now(UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1000,
    )


@pytest.fixture
def scheduler() -> StrategyScheduler:
    return StrategyScheduler(
        registry=AsyncMock(),
        broker=AsyncMock(),
        risk_manager=AsyncMock(),
        cache=AsyncMock(),
        session_factory=AsyncMock(),
        order_manager=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# _get_current_price — cache hit / broker fallback / total miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_price_from_cache_hit(scheduler: StrategyScheduler) -> None:
    scheduler._cache.get = AsyncMock(return_value=json.dumps({"price": "150.25"}))
    price = await scheduler._get_current_price("AAPL")
    assert price == Decimal("150.25")
    scheduler._broker.get_historical_data.assert_not_called()


@pytest.mark.asyncio
async def test_price_falls_back_to_broker_on_cache_miss(scheduler: StrategyScheduler) -> None:
    scheduler._cache.get = AsyncMock(return_value=None)
    scheduler._broker.get_historical_data = AsyncMock(return_value=[_bar("100"), _bar("101.50")])

    price = await scheduler._get_current_price("AAPL")

    assert price == Decimal("101.50")
    scheduler._broker.get_historical_data.assert_awaited_once()


@pytest.mark.asyncio
async def test_price_falls_back_to_broker_when_cache_raises(scheduler: StrategyScheduler) -> None:
    scheduler._cache.get = AsyncMock(side_effect=ConnectionError("redis down"))
    scheduler._broker.get_historical_data = AsyncMock(return_value=[_bar("99.00")])

    with patch("app.core.strategy_engine.scheduler.system_logger") as mock_logger:
        price = await scheduler._get_current_price("AAPL")

    assert price == Decimal("99.00")
    mock_logger.warning.assert_called()


@pytest.mark.asyncio
async def test_price_returns_none_and_logs_when_both_sources_fail(
    scheduler: StrategyScheduler,
) -> None:
    scheduler._cache.get = AsyncMock(return_value=None)
    scheduler._broker.get_historical_data = AsyncMock(side_effect=ConnectionError("ibkr down"))

    with patch("app.core.strategy_engine.scheduler.system_logger") as mock_logger:
        price = await scheduler._get_current_price("AAPL")

    assert price is None
    assert mock_logger.warning.call_count >= 1


# ---------------------------------------------------------------------------
# Consecutive-miss escalation in _run_strategy_for_symbol
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_price_logs_warning_below_escalation_threshold(
    scheduler: StrategyScheduler,
) -> None:
    scheduler._get_current_price = AsyncMock(return_value=None)

    with patch("app.core.strategy_engine.scheduler.system_logger") as mock_logger:
        await scheduler._run_strategy_for_symbol(
            strategy=AsyncMock(),
            strategy_id="11111111-1111-1111-1111-111111111111",
            symbol="AAPL",
            account_balance=Decimal("100000"),
        )

    mock_logger.warning.assert_called_once()
    mock_logger.error.assert_not_called()
    assert scheduler._consecutive_price_misses["AAPL"] == 1


@pytest.mark.asyncio
async def test_no_price_escalates_to_error_after_threshold(
    scheduler: StrategyScheduler,
) -> None:
    scheduler._get_current_price = AsyncMock(return_value=None)
    scheduler._consecutive_price_misses["AAPL"] = _CONSECUTIVE_MISS_ESCALATION_THRESHOLD - 1

    with patch("app.core.strategy_engine.scheduler.system_logger") as mock_logger:
        await scheduler._run_strategy_for_symbol(
            strategy=AsyncMock(),
            strategy_id="11111111-1111-1111-1111-111111111111",
            symbol="AAPL",
            account_balance=Decimal("100000"),
        )

    mock_logger.error.assert_called_once()
    mock_logger.warning.assert_not_called()
    assert scheduler._consecutive_price_misses["AAPL"] == _CONSECUTIVE_MISS_ESCALATION_THRESHOLD


@pytest.mark.asyncio
async def test_miss_counter_resets_once_price_is_found(scheduler: StrategyScheduler) -> None:
    from datetime import UTC, datetime

    scheduler._consecutive_price_misses["AAPL"] = 3
    scheduler._get_current_price = AsyncMock(return_value=Decimal("150"))
    scheduler._get_historical_bars = AsyncMock(return_value=[_bar("150")])
    strategy = AsyncMock()
    strategy.required_extra_symbols = lambda: []  # sync method per BaseStrategy interface
    strategy.generate_signal = AsyncMock(
        return_value=Signal(symbol="AAPL", action="HOLD", timestamp=datetime.now(UTC))
    )

    await scheduler._run_strategy_for_symbol(
        strategy=strategy,
        strategy_id="11111111-1111-1111-1111-111111111111",
        symbol="AAPL",
        account_balance=Decimal("100000"),
    )

    assert "AAPL" not in scheduler._consecutive_price_misses


# ---------------------------------------------------------------------------
# extra_bars wiring — required_extra_symbols() (e.g. BullBearStrategy regime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extra_bars_populated_for_required_symbol(scheduler: StrategyScheduler) -> None:
    from datetime import UTC, datetime

    scheduler._get_current_price = AsyncMock(return_value=Decimal("150"))

    async def fake_bars(symbol: str):
        return [_bar("100")] if symbol == "SPY" else [_bar("150")]

    scheduler._get_historical_bars = AsyncMock(side_effect=fake_bars)

    strategy = AsyncMock()
    strategy.required_extra_symbols = lambda: ["SPY"]
    captured = {}

    async def capture_signal(market_data):
        captured["market_data"] = market_data
        return Signal(symbol=market_data.symbol, action="HOLD", timestamp=datetime.now(UTC))

    strategy.generate_signal = capture_signal

    await scheduler._run_strategy_for_symbol(
        strategy=strategy,
        strategy_id="11111111-1111-1111-1111-111111111111",
        symbol="AAPL",
        account_balance=Decimal("100000"),
    )

    market_data = captured["market_data"]
    assert "SPY" in market_data.extra_bars
    assert market_data.extra_bars["SPY"][0].close == Decimal("100")


@pytest.mark.asyncio
async def test_extra_bars_skips_when_required_symbol_equals_target(
    scheduler: StrategyScheduler,
) -> None:
    """A strategy whose required extra symbol IS the symbol being evaluated
    (e.g. the inverse-ETF leg requiring its own regime symbol which happens
    to equal the target) doesn't double-fetch."""
    from datetime import UTC, datetime

    scheduler._get_current_price = AsyncMock(return_value=Decimal("150"))
    scheduler._get_historical_bars = AsyncMock(return_value=[_bar("150")])

    strategy = AsyncMock()
    strategy.required_extra_symbols = lambda: ["AAPL"]  # same as target symbol
    strategy.generate_signal = AsyncMock(
        return_value=Signal(symbol="AAPL", action="HOLD", timestamp=datetime.now(UTC))
    )

    await scheduler._run_strategy_for_symbol(
        strategy=strategy,
        strategy_id="11111111-1111-1111-1111-111111111111",
        symbol="AAPL",
        account_balance=Decimal("100000"),
    )

    # _get_historical_bars called once for the primary symbol only — not twice
    assert scheduler._get_historical_bars.await_count == 1


@pytest.mark.asyncio
async def test_extra_bars_missing_logs_warning_but_continues(
    scheduler: StrategyScheduler,
) -> None:
    from datetime import UTC, datetime

    scheduler._get_current_price = AsyncMock(return_value=Decimal("150"))

    async def fake_bars(symbol: str):
        return [_bar("150")] if symbol == "AAPL" else []  # SPY fetch fails

    scheduler._get_historical_bars = AsyncMock(side_effect=fake_bars)

    strategy = AsyncMock()
    strategy.required_extra_symbols = lambda: ["SPY"]
    strategy.generate_signal = AsyncMock(
        return_value=Signal(symbol="AAPL", action="HOLD", timestamp=datetime.now(UTC))
    )

    with patch("app.core.strategy_engine.scheduler.system_logger") as mock_logger:
        await scheduler._run_strategy_for_symbol(
            strategy=strategy,
            strategy_id="11111111-1111-1111-1111-111111111111",
            symbol="AAPL",
            account_balance=Decimal("100000"),
        )

    mock_logger.warning.assert_called()
    strategy.generate_signal.assert_awaited_once()
