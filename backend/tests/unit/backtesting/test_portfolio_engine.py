"""
Unit tests for PortfolioBacktestEngine (21.4).

Covers:
  - Empty slot list raises ValueError
  - Two slots: combined equity curve equals sum of individual P&Ls
  - Portfolio risk gate rejects second trade when aggregate would exceed limit
  - Single-slot result matches BacktestingEngine output
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.brokers.base import PriceBar
from app.core.backtesting.engine import BacktestingEngine
from app.core.backtesting.portfolio_engine import PortfolioBacktestEngine, PortfolioSlot
from app.core.risk.manager import RiskManager
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal

# ---------------------------------------------------------------------------
# Shared helpers (duplicated from test_engine.py to keep tests self-contained)
# ---------------------------------------------------------------------------


def _bar(i: int, open_: float, high: float, low: float, close: float) -> PriceBar:
    ts = datetime(2025, 1, 2, tzinfo=UTC) + timedelta(days=i)
    return PriceBar(
        timestamp=ts,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=100_000,
        bar_size="1 day",
    )


def _flat_bars(count: int, price: float = 100.0) -> list[PriceBar]:
    return [_bar(i, price, price + 0.5, price - 0.5, price) for i in range(count)]


# ---------------------------------------------------------------------------
# Stub strategies
# ---------------------------------------------------------------------------


class NeverBuyStrategy(BaseStrategy):
    async def generate_signal(self, data: MarketData) -> Signal:
        return Signal(symbol=data.symbol, action="HOLD", timestamp=data.timestamp)

    async def calculate_position_size(self, params: RiskParams) -> int:
        return 10

    def get_config_schema(self) -> dict:
        return {}


class BuyOnCallN(BaseStrategy):
    """Fires a BUY signal exactly once, on the Nth call to generate_signal."""

    def __init__(
        self,
        fire_on: int,
        stop_pct: float = 0.10,
        quantity: int = 5,
    ) -> None:
        self._fire_on = fire_on
        self._stop_pct = stop_pct
        self._quantity = quantity
        self._calls = 0

    async def generate_signal(self, data: MarketData) -> Signal:
        self._calls += 1
        if self._calls == self._fire_on:
            entry = data.current_price
            stop = (entry * Decimal(str(1 - self._stop_pct))).quantize(Decimal("0.01"))
            tp = (entry * Decimal(str(1 + self._stop_pct * 2.5))).quantize(Decimal("0.01"))
            return Signal(
                symbol=data.symbol,
                action="BUY",
                entry_price=entry,
                stop_loss_price=stop,
                take_profit_price=tp,
                timestamp=data.timestamp,
            )
        return Signal(symbol=data.symbol, action="HOLD", timestamp=data.timestamp)

    async def calculate_position_size(self, params: RiskParams) -> int:
        return self._quantity

    def get_config_schema(self) -> dict:
        return {}


class FixedRiskBuyStrategy(BaseStrategy):
    """
    Returns a BUY on the first call with a controlled stop distance and quantity,
    so risk_amount = quantity × stop_distance is deterministic.

    Used to test the portfolio aggregate risk gate precisely.
    """

    def __init__(
        self,
        fire_on: int,
        stop_distance: Decimal,
        quantity: int,
    ) -> None:
        self._fire_on = fire_on
        self._stop_distance = stop_distance
        self._quantity = quantity
        self._calls = 0

    async def generate_signal(self, data: MarketData) -> Signal:
        self._calls += 1
        if self._calls == self._fire_on:
            entry = data.current_price
            stop = (entry - self._stop_distance).quantize(Decimal("0.01"))
            tp = (entry + self._stop_distance * Decimal("2.5")).quantize(Decimal("0.01"))
            return Signal(
                symbol=data.symbol,
                action="BUY",
                entry_price=entry,
                stop_loss_price=stop,
                take_profit_price=tp,
                timestamp=data.timestamp,
            )
        return Signal(symbol=data.symbol, action="HOLD", timestamp=data.timestamp)

    async def calculate_position_size(self, params: RiskParams) -> int:
        return self._quantity

    def get_config_schema(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def risk_manager() -> RiskManager:
    return RiskManager()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_slots_raises(risk_manager: RiskManager) -> None:
    """An empty slot list must raise ValueError immediately."""
    engine = PortfolioBacktestEngine(risk_manager)
    with pytest.raises(ValueError, match="at least one slot"):
        await engine.run([], account_balance=Decimal("100000"))


@pytest.mark.asyncio
async def test_combined_equity_equals_sum_of_per_strategy_pnls(
    risk_manager: RiskManager,
) -> None:
    """
    Combined equity curve final value must equal the sum of per-strategy
    total_returns when two slots each close exactly one profitable trade.
    """
    balance = Decimal("100000")

    # Slot 0: BUY fires on bar 1; price rises well past take-profit on bar 3
    bars_a = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 100.5, 99.5, 100),   # signal fires here (call 1)
        _bar(2, 100, 100.5, 99.5, 100),   # fill at open=100
        _bar(3, 100, 135, 99, 134),        # high=135 > take-profit (~125) → exit TP
        _bar(4, 134, 135, 133, 134),
    ]
    # Slot 1: same shape, independent run
    bars_b = [
        _bar(0, 200, 200.5, 199.5, 200),
        _bar(1, 200, 200.5, 199.5, 200),
        _bar(2, 200, 200.5, 199.5, 200),
        _bar(3, 200, 260, 199, 259),
        _bar(4, 259, 260, 258, 259),
    ]

    slots = [
        PortfolioSlot(
            strategy=BuyOnCallN(fire_on=1, stop_pct=0.10, quantity=5),
            symbol="AAPL",
            bars=bars_a,
            strategy_type="stub",
        ),
        PortfolioSlot(
            strategy=BuyOnCallN(fire_on=1, stop_pct=0.10, quantity=5),
            symbol="MSFT",
            bars=bars_b,
            strategy_type="stub",
        ),
    ]

    engine = PortfolioBacktestEngine(risk_manager)
    result = await engine.run(slots, account_balance=balance)

    assert len(result.per_strategy) == 2

    sum_per_strategy = sum(
        ps.metrics.total_return
        for ps in result.per_strategy
        if ps.metrics is not None
    )

    # The combined equity curve's last entry is cumulative P&L of all closed trades
    if result.combined_equity_curve:
        last_cumulative = result.combined_equity_curve[-1][1]
        assert last_cumulative == sum_per_strategy, (
            f"Equity curve final {last_cumulative} != sum of per-strategy returns {sum_per_strategy}"
        )

    # Combined metrics total_return also matches
    assert result.combined_metrics.total_return == sum_per_strategy


@pytest.mark.asyncio
async def test_portfolio_risk_gate_rejects_second_trade() -> None:
    """
    When two slots both want to enter on the same bar, the second slot must be
    rejected if the combined risk would exceed the portfolio cap.

    Setup:
      account_balance = $20,000
      portfolio cap   = 1.5% of balance = $300
      per-trade cap   = 1% of balance   = $200

      Each slot uses FixedRiskBuyStrategy with stop_distance=$10, quantity=20:
        risk_amount = 20 × $10 = $200 (exactly at the 1% per-trade limit → passes gate 1)

      Slot 0: agg_risk=0  → 0+200=200 ≤ 300 → ACCEPTED
      Slot 1: agg_risk=200 → 200+200=400 > 300 → REJECTED
    """
    balance = Decimal("20000")
    tight_rm = RiskManager(max_portfolio_risk_pct=Decimal("0.015"))

    # Both slots fire a BUY on call 1. Fill at bar index 2 open = 100.
    # stop_distance=10 → stop=90; tp=125 (2.5×10 above 100) → R:R ≥ 2.0 ✓
    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 100.5, 99.5, 100),   # signal fires (call 1)
        _bar(2, 100, 100.5, 99.5, 100),   # fill bar (open=100 used as fill price)
        _bar(3, 100, 100.5, 99.5, 100),
        _bar(4, 100, 100.5, 99.5, 100),
    ]

    slots = [
        PortfolioSlot(
            strategy=FixedRiskBuyStrategy(fire_on=1, stop_distance=Decimal("10"), quantity=20),
            symbol="AAA",
            bars=bars,
            strategy_type="stub",
        ),
        PortfolioSlot(
            strategy=FixedRiskBuyStrategy(fire_on=1, stop_distance=Decimal("10"), quantity=20),
            symbol="BBB",
            bars=list(bars),  # same bar data, independent copy
            strategy_type="stub",
        ),
    ]

    engine = PortfolioBacktestEngine(tight_rm)
    result = await engine.run(slots, account_balance=balance)

    # Slot 0 should have exactly one trade; slot 1 should have zero
    slot0_trades = result.per_strategy[0].trades
    slot1_trades = result.per_strategy[1].trades

    assert len(slot0_trades) == 1, f"Expected slot 0 to have 1 trade, got {len(slot0_trades)}"
    assert len(slot1_trades) == 0, f"Expected slot 1 to have 0 trades (rejected), got {len(slot1_trades)}"

    # signals_rejected should be 1 for slot 1
    assert result.per_strategy[1].metrics is not None
    assert result.per_strategy[1].metrics.signals_rejected >= 1


@pytest.mark.asyncio
async def test_single_slot_matches_single_strategy_engine() -> None:
    """
    Running PortfolioBacktestEngine with a single slot must produce the same
    trade outcomes (entry/exit prices, exit reasons, P&L) as BacktestingEngine
    with the same strategy and bars.
    """
    balance = Decimal("100000")
    rm = RiskManager()

    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 100.5, 99.5, 100),   # signal on call 1
        _bar(2, 100, 100.5, 99.5, 100),   # fill
        _bar(3, 100, 130, 99.5, 129),      # take-profit hit
        _bar(4, 129, 130, 128, 129),
    ]

    # Reference: single-strategy engine
    ref_strategy = BuyOnCallN(fire_on=1, stop_pct=0.10, quantity=5)
    single_engine = BacktestingEngine(ref_strategy, rm)
    ref_result = await single_engine.run(
        bars, symbol="X", account_balance=balance, strategy_type="stub"
    )

    # Portfolio engine with one slot
    port_strategy = BuyOnCallN(fire_on=1, stop_pct=0.10, quantity=5)
    port_engine = PortfolioBacktestEngine(rm)
    port_result = await port_engine.run(
        [PortfolioSlot(strategy=port_strategy, symbol="X", bars=bars, strategy_type="stub")],
        account_balance=balance,
    )

    per_strat = port_result.per_strategy[0]

    assert len(per_strat.trades) == len(ref_result.trades), (
        f"Trade counts differ: portfolio={len(per_strat.trades)}, single={len(ref_result.trades)}"
    )

    for i, (pt, rt) in enumerate(zip(per_strat.trades, ref_result.trades, strict=False)):
        assert pt.entry_price == rt.entry_price, f"Trade {i}: entry_price mismatch"
        assert pt.exit_reason == rt.exit_reason, f"Trade {i}: exit_reason mismatch"
        assert pt.pnl == rt.pnl, f"Trade {i}: pnl mismatch"


@pytest.mark.asyncio
async def test_combined_metrics_aggregate_all_slots(risk_manager: RiskManager) -> None:
    """combined_metrics.trade_count equals total trades across all slots."""
    balance = Decimal("100000")

    bars_a = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 101, 99, 100),
        _bar(2, 100, 101, 99, 100),
        _bar(3, 100, 135, 99, 134),
        _bar(4, 134, 135, 133, 134),
    ]
    bars_b = list(bars_a)

    slots = [
        PortfolioSlot(
            strategy=BuyOnCallN(fire_on=1, stop_pct=0.10, quantity=5),
            symbol="S0",
            bars=bars_a,
        ),
        PortfolioSlot(
            strategy=BuyOnCallN(fire_on=1, stop_pct=0.10, quantity=5),
            symbol="S1",
            bars=bars_b,
        ),
    ]

    engine = PortfolioBacktestEngine(risk_manager)
    result = await engine.run(slots, account_balance=balance)

    total_from_per_strategy = sum(len(ps.trades) for ps in result.per_strategy)
    assert result.combined_metrics.trade_count == total_from_per_strategy


# ---------------------------------------------------------------------------
# Friction modelling — slippage + commission (mirrors BacktestingEngine)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_friction_reduces_pnl_versus_frictionless(
    risk_manager: RiskManager,
) -> None:
    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 100.5, 99.5, 100),
        _bar(2, 100, 100.5, 99.5, 100),
        _bar(3, 100, 130, 99.5, 129),
        _bar(4, 129, 130, 128, 129),
    ]

    frictionless = PortfolioBacktestEngine(
        risk_manager, slippage_pct=Decimal("0"), commission_per_trade=Decimal("0")
    )
    realistic = PortfolioBacktestEngine(risk_manager)

    def slot_factory() -> list[PortfolioSlot]:
        return [
            PortfolioSlot(
                strategy=BuyOnCallN(fire_on=1, stop_pct=0.10, quantity=5),
                symbol="X",
                bars=list(bars),
            )
        ]

    frictionless_result = await frictionless.run(slot_factory(), account_balance=Decimal("100000"))
    realistic_result = await realistic.run(slot_factory(), account_balance=Decimal("100000"))

    assert (
        realistic_result.per_strategy[0].trades[0].pnl
        < frictionless_result.per_strategy[0].trades[0].pnl
    )


@pytest.mark.asyncio
async def test_zero_friction_matches_frictionless_behavior(risk_manager: RiskManager) -> None:
    bars = [
        _bar(0, 100, 100.5, 99.5, 100),
        _bar(1, 100, 100.5, 99.5, 100),
        _bar(2, 100, 100.5, 99.5, 100),
        _bar(3, 100, 130, 99.5, 129),
        _bar(4, 129, 130, 128, 129),
    ]
    engine = PortfolioBacktestEngine(
        risk_manager, slippage_pct=Decimal("0"), commission_per_trade=Decimal("0")
    )
    slots = [
        PortfolioSlot(strategy=BuyOnCallN(fire_on=1, stop_pct=0.10, quantity=5), symbol="X", bars=bars)
    ]
    result = await engine.run(slots, account_balance=Decimal("100000"))

    trade = result.per_strategy[0].trades[0]
    assert trade.entry_price == Decimal("100")
    assert trade.commission_paid == Decimal("0")
    assert trade.pnl == (trade.exit_price - trade.entry_price) * trade.quantity
