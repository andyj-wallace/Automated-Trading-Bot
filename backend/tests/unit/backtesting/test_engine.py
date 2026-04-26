"""
Unit tests for BacktestingEngine (15.1 / 15.2).

Covers: run() with no trades, BUY entry + take-profit exit, stop-loss exit,
end-of-data close, metrics computation, invalid input handling.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.brokers.base import PriceBar
from app.core.backtesting.engine import BacktestingEngine, BacktestMetrics
from app.core.risk.manager import RiskManager
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(
    i: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> PriceBar:
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
    return PriceBar(
        timestamp=ts,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=100_000,
        bar_size="1 day",
    )


def _steady_bars(count: int, price: float = 100.0, trend: float = 0.0) -> list[PriceBar]:
    bars = []
    p = price
    for i in range(count):
        bars.append(_bar(i, open_=p, high=p + 0.5, low=p - 0.5, close=p + trend))
        p += trend
    return bars


# ---------------------------------------------------------------------------
# Stub strategy implementations
# ---------------------------------------------------------------------------


class AlwaysHoldStrategy(BaseStrategy):
    """Never fires a signal — used to test the no-trade case."""

    async def generate_signal(self, data: MarketData) -> Signal:
        return Signal(symbol=data.symbol, action="HOLD", timestamp=data.timestamp)

    async def calculate_position_size(self, params: RiskParams) -> int:
        return 10

    def get_config_schema(self) -> dict:
        return {}


class BuyOnBarNStrategy(BaseStrategy):
    """Fires a BUY signal on the Nth bar (1-indexed), then HOLDs."""

    def __init__(self, fire_on_bar: int, stop_pct: float = 0.03) -> None:
        self._fire_on = fire_on_bar
        self._stop_pct = stop_pct
        self._calls = 0

    async def generate_signal(self, data: MarketData) -> Signal:
        self._calls += 1
        if self._calls == self._fire_on:
            entry = data.current_price
            stop = entry * Decimal(str(1 - self._stop_pct))
            tp = entry * Decimal(str(1 + self._stop_pct * 2.5))  # 2.5:1 R:R
            return Signal(
                symbol=data.symbol,
                action="BUY",
                entry_price=entry,
                stop_loss_price=stop.quantize(Decimal("0.01")),
                take_profit_price=tp.quantize(Decimal("0.01")),
                timestamp=data.timestamp,
            )
        return Signal(symbol=data.symbol, action="HOLD", timestamp=data.timestamp)

    async def calculate_position_size(self, params: RiskParams) -> int:
        return 5

    def get_config_schema(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def risk_manager() -> RiskManager:
    return RiskManager()


@pytest.mark.asyncio
async def test_no_trades_returns_empty_metrics(risk_manager: RiskManager) -> None:
    """AlwaysHoldStrategy should produce zero trades and zeroed metrics."""
    strategy = AlwaysHoldStrategy()
    engine = BacktestingEngine(strategy, risk_manager)
    bars = _steady_bars(50, price=200.0)

    result = await engine.run(bars, symbol="TEST", account_balance=Decimal("100000"))

    assert result.symbol == "TEST"
    assert len(result.trades) == 0
    assert result.metrics is not None
    assert result.metrics.trade_count == 0
    assert result.metrics.win_rate_pct == 0.0
    assert result.metrics.total_return == Decimal("0")
    assert result.metrics.max_drawdown_pct == 0.0


@pytest.mark.asyncio
async def test_raises_on_too_few_bars(risk_manager: RiskManager) -> None:
    """Engine requires at least 2 bars to run."""
    strategy = AlwaysHoldStrategy()
    engine = BacktestingEngine(strategy, risk_manager)

    with pytest.raises(ValueError, match="2 bars"):
        await engine.run([_bar(0, 100, 101, 99, 100)], symbol="X", account_balance=Decimal("10000"))


@pytest.mark.asyncio
async def test_take_profit_exit(risk_manager: RiskManager) -> None:
    """Trade opened on bar 3, price surges above take-profit on bar 8."""
    strategy = BuyOnBarNStrategy(fire_on_bar=2)  # signal on 2nd call = bar index 2
    engine = BacktestingEngine(strategy, risk_manager)

    # Build bars: entry around 100, then rally well past take-profit
    bars = [
        _bar(0, 100, 100.5, 99.5, 100),  # bar 0
        _bar(1, 100, 100.5, 99.5, 100),  # bar 1 — signal fires here
        _bar(2, 100, 100.5, 99.5, 100),  # bar 2 — fill at open (100)
        _bar(3, 100, 115, 99.5, 114),    # bar 3 — high=115, take-profit ~107.5 hit
        _bar(4, 114, 115, 113, 114),     # bar 4
    ]

    result = await engine.run(bars, symbol="RALLY", account_balance=Decimal("100000"))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "TAKE_PROFIT"
    assert trade.pnl is not None and trade.pnl > 0
    assert result.metrics is not None
    assert result.metrics.win_count == 1
    assert result.metrics.win_rate_pct == 100.0


@pytest.mark.asyncio
async def test_stop_loss_exit(risk_manager: RiskManager) -> None:
    """Trade opened and then price falls through stop-loss."""
    strategy = BuyOnBarNStrategy(fire_on_bar=2, stop_pct=0.05)
    engine = BacktestingEngine(strategy, risk_manager)

    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 100.5, 99.5, 100),   # signal
        _bar(2, 100, 100.5, 99.5, 100),   # fill at 100
        _bar(3, 100, 100.5, 90, 91),      # low=90 < stop (~95) → stopped out
        _bar(4, 91, 92, 90, 91),
    ]

    result = await engine.run(bars, symbol="DUMP", account_balance=Decimal("100000"))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "STOP_LOSS"
    assert trade.pnl is not None and trade.pnl < 0
    assert result.metrics is not None
    assert result.metrics.loss_count == 1


@pytest.mark.asyncio
async def test_end_of_data_close(risk_manager: RiskManager) -> None:
    """Trade opened but no stop or target hit — closed at last bar close."""
    strategy = BuyOnBarNStrategy(fire_on_bar=2, stop_pct=0.5)  # wide stop won't trigger
    engine = BacktestingEngine(strategy, risk_manager)

    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 100.5, 99.5, 100),   # signal
        _bar(2, 100, 101, 99.8, 101),     # fill at 100
        _bar(3, 101, 102, 100, 102),      # minor moves — no exit
    ]

    result = await engine.run(bars, symbol="HOLD", account_balance=Decimal("100000"))

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "END_OF_DATA"


@pytest.mark.asyncio
async def test_metrics_sharpe_ratio_non_zero_when_trades_exist(risk_manager: RiskManager) -> None:
    """Sharpe ratio should be computed (non-NaN) when trades produce returns."""
    strategy = BuyOnBarNStrategy(fire_on_bar=2)
    engine = BacktestingEngine(strategy, risk_manager)

    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 100.5, 99.5, 100),
        _bar(2, 100, 130, 99.5, 129),   # take-profit hit immediately
        _bar(3, 129, 130, 128, 129),
    ]

    result = await engine.run(bars, symbol="SHARP", account_balance=Decimal("100000"))
    assert result.metrics is not None
    assert not (result.metrics.sharpe_ratio != result.metrics.sharpe_ratio)  # not NaN


@pytest.mark.asyncio
async def test_metadata_stored_in_result(risk_manager: RiskManager) -> None:
    """BacktestResult carries correct symbol, strategy_type, account_balance."""
    strategy = AlwaysHoldStrategy()
    engine = BacktestingEngine(strategy, risk_manager)
    bars = _steady_bars(10, price=50.0)

    result = await engine.run(
        bars,
        symbol="META",
        account_balance=Decimal("50000"),
        strategy_type="test_strategy",
        strategy_config={"foo": "bar"},
    )

    assert result.symbol == "META"
    assert result.strategy_type == "test_strategy"
    assert result.strategy_config == {"foo": "bar"}
    assert result.account_balance == Decimal("50000")


@pytest.mark.asyncio
async def test_max_drawdown_zero_with_no_trades(risk_manager: RiskManager) -> None:
    """No trades means drawdown is 0."""
    strategy = AlwaysHoldStrategy()
    engine = BacktestingEngine(strategy, risk_manager)
    bars = _steady_bars(20)

    result = await engine.run(bars, symbol="FLAT", account_balance=Decimal("10000"))
    assert result.metrics is not None
    assert result.metrics.max_drawdown_pct == 0.0
