"""
Performance load test — 18.6.

Simulates 5 strategies firing simultaneously and validates that:
  1. All 5 complete without errors or exceptions.
  2. Total wall-clock time is under 10 seconds (strategies are CPU-bound,
     no I/O wait in this test — real threshold is generous to avoid flakiness
     on slow CI runners).
  3. Each engine produces a valid BacktestResult with metrics populated.

Run with:
    pytest backend/tests/load/ -v

For profiling purposes the test also prints per-run and total elapsed times.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.brokers.base import PriceBar
from app.core.backtesting.engine import BacktestingEngine
from app.core.risk.manager import RiskManager
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYMBOLS = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"]


def _make_bars(
    count: int = 300,
    start_price: float = 100.0,
    trend: float = 0.3,
    volatility: float = 1.0,
) -> list[PriceBar]:
    """Generate synthetic daily bars."""
    bars = []
    price = start_price
    base_date = datetime(2024, 1, 1, tzinfo=timezone.utc)

    for i in range(count):
        ts = base_date + timedelta(days=i)
        import math
        # Add some cyclicality so signals fire at different points per symbol
        cycle = math.sin(i / 20.0) * volatility
        open_ = price
        close = price + trend + cycle
        high = max(open_, close) + volatility * 0.5
        low = min(open_, close) - volatility * 0.5

        bars.append(PriceBar(
            timestamp=ts,
            open=Decimal(str(round(open_, 2))),
            high=Decimal(str(round(high, 2))),
            low=Decimal(str(round(low, 2))),
            close=Decimal(str(round(close, 2))),
            volume=1_000_000,
            bar_size="1 day",
        ))
        price = close

    return bars


# ---------------------------------------------------------------------------
# Stub strategy — fires a signal when a simple moving average condition is met
# ---------------------------------------------------------------------------

class _SimpleMAStrategy(BaseStrategy):
    """
    Fires a BUY when close crosses above a 20-bar SMA.
    Fires a SELL (which the engine will skip) when below.
    Realistic enough to generate a mix of signals and rejections.
    """

    def __init__(self, period: int = 20) -> None:
        self._period = period
        self._prev_above: bool | None = None

    async def generate_signal(self, data: MarketData) -> Signal:
        if len(data.bars) < self._period + 1:
            return Signal(symbol=data.symbol, action="HOLD", timestamp=data.timestamp)

        sma = sum(float(b.close) for b in data.bars[-self._period:]) / self._period
        current = float(data.current_price)
        above = current > sma

        if self._prev_above is False and above:  # cross-up → BUY
            stop = data.current_price * Decimal("0.97")
            tp = data.current_price * Decimal("1.07")
            self._prev_above = above
            return Signal(
                symbol=data.symbol,
                action="BUY",
                entry_price=data.current_price,
                stop_loss_price=stop.quantize(Decimal("0.01")),
                take_profit_price=tp.quantize(Decimal("0.01")),
                timestamp=data.timestamp,
            )

        self._prev_above = above
        return Signal(symbol=data.symbol, action="HOLD", timestamp=data.timestamp)

    async def calculate_position_size(self, params: RiskParams) -> int:
        stop_distance = params.entry_price - params.stop_loss_price
        if stop_distance <= 0:
            return 0
        risk_amount = params.account_balance * params.max_risk_pct
        return max(1, int(risk_amount / stop_distance))

    def get_config_schema(self) -> dict:
        return {"period": self._period}


# ---------------------------------------------------------------------------
# Load test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_five_strategies_run_concurrently_without_error() -> None:
    """
    5 BacktestingEngine runs launched concurrently via asyncio.gather.

    All must:
      - Complete without raising
      - Return a BacktestResult with populated metrics
      - Finish in total < 10 seconds
    """
    risk_manager = RiskManager()
    account_balance = Decimal("100000")

    async def _run_one(symbol: str, period: int) -> tuple[str, float]:
        strategy = _SimpleMAStrategy(period=period)
        engine = BacktestingEngine(strategy, risk_manager)
        bars = _make_bars(count=300, start_price=100 + period, trend=0.4)

        t0 = time.perf_counter()
        result = await engine.run(bars, symbol=symbol, account_balance=account_balance)
        elapsed = time.perf_counter() - t0

        # Validate result integrity
        assert result.symbol == symbol
        assert result.metrics is not None
        assert result.metrics.bars_tested == len(bars)
        assert result.metrics.trade_count >= 0
        assert 0.0 <= result.metrics.win_rate_pct <= 100.0
        assert not (result.metrics.sharpe_ratio != result.metrics.sharpe_ratio)  # not NaN
        assert result.metrics.max_drawdown_pct >= 0.0

        return symbol, elapsed

    # Use slightly different MA periods so each strategy behaves differently
    periods = [10, 15, 20, 25, 30]
    wall_t0 = time.perf_counter()

    results = await asyncio.gather(
        *[_run_one(sym, per) for sym, per in zip(_SYMBOLS, periods)]
    )

    total_elapsed = time.perf_counter() - wall_t0

    print(f"\n--- Load test results (5 concurrent strategies) ---")
    for sym, t in results:
        print(f"  {sym}: {t * 1000:.1f} ms")
    print(f"  Total wall time: {total_elapsed * 1000:.1f} ms")
    print(f"---")

    assert len(results) == 5, "All 5 strategies should complete"
    assert total_elapsed < 10.0, (
        f"5 concurrent strategies took {total_elapsed:.2f}s — expected < 10s"
    )


@pytest.mark.asyncio
async def test_concurrent_runs_are_independent() -> None:
    """
    Running the same strategy twice concurrently with different bar histories
    must produce independent results (no shared state leakage).
    """
    risk_manager = RiskManager()

    # Two bar histories with opposite trends
    uptrend = _make_bars(count=150, start_price=100.0, trend=+1.0, volatility=0.2)
    flat = _make_bars(count=150, start_price=100.0, trend=0.0, volatility=0.1)

    async def _run(bars: list[PriceBar], symbol: str):
        strategy = _SimpleMAStrategy(period=10)
        engine = BacktestingEngine(strategy, risk_manager)
        return await engine.run(bars, symbol=symbol, account_balance=Decimal("50000"))

    result_up, result_flat = await asyncio.gather(
        _run(uptrend, "UP"),
        _run(flat, "FLAT"),
    )

    # Uptrend should produce more or equal trades than a flat market
    # (at minimum, strategies are independent — no cross-contamination)
    assert result_up.symbol == "UP"
    assert result_flat.symbol == "FLAT"
    assert result_up.metrics is not None
    assert result_flat.metrics is not None


@pytest.mark.asyncio
async def test_concurrent_risk_manager_calls_are_thread_safe() -> None:
    """
    Multiple concurrent strategies sharing a single RiskManager must not
    interfere with each other's validation results.
    """
    shared_risk_manager = RiskManager()

    async def _validate_and_assert(account_balance: Decimal) -> None:
        from app.core.risk.manager import TradeRequest
        import uuid

        req = TradeRequest(
            trade_id=uuid.uuid4(),
            symbol="AAPL",
            direction="BUY",
            quantity=Decimal("10"),
            entry_price=Decimal("200"),
            stop_loss_price=Decimal("194"),  # 3% stop
            account_balance=account_balance,
        )
        result = shared_risk_manager.validate(req, current_aggregate_risk=Decimal("0"))
        assert result.max_quantity > 0
        assert result.take_profit_price > req.stop_loss_price

    # 20 concurrent validations with different balances
    balances = [Decimal(str(50_000 + i * 1000)) for i in range(20)]
    await asyncio.gather(*[_validate_and_assert(b) for b in balances])
