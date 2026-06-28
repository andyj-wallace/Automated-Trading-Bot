"""Unit tests for the walk-forward train/test split harness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.brokers.base import PriceBar
from app.core.backtesting.engine import BacktestMetrics, BacktestResult
from app.core.backtesting.walk_forward import (
    WalkForwardSplit,
    _detect_overfit,
    evaluate_walk_forward,
)
from app.core.risk.manager import RiskManager
from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal


def _bar(i: int, open_: float, high: float, low: float, close: float) -> PriceBar:
    ts = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i)
    return PriceBar(
        timestamp=ts,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=100_000,
    )


def _flat_bars(count: int, price: float = 100.0) -> list[PriceBar]:
    return [_bar(i, price, price + 0.5, price - 0.5, price) for i in range(count)]


def _metrics(trade_count: int, win_rate_pct: float, total_return: str) -> BacktestMetrics:
    return BacktestMetrics(
        trade_count=trade_count,
        win_count=0,
        loss_count=0,
        win_rate_pct=win_rate_pct,
        total_return=Decimal(total_return),
        total_return_pct=0.0,
        avg_trade_pnl=Decimal("0"),
        avg_winner=Decimal("0"),
        avg_loser=Decimal("0"),
        largest_winner=Decimal("0"),
        largest_loser=Decimal("0"),
        max_drawdown_pct=0.0,
        sharpe_ratio=0.0,
        bars_tested=0,
        signals_generated=trade_count,
        signals_rejected=0,
    )


def _result(metrics: BacktestMetrics | None) -> BacktestResult:
    now = datetime.now(UTC)
    return BacktestResult(
        symbol="X",
        strategy_type="stub",
        strategy_config={},
        account_balance=Decimal("100000"),
        start_time=now,
        end_time=now,
        trades=[],
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# WalkForwardSplit.from_ratio
# ---------------------------------------------------------------------------


class TestSplit:
    def test_splits_chronologically(self):
        bars = _flat_bars(10)
        split = WalkForwardSplit.from_ratio(bars, train_ratio=0.7)
        assert len(split.train_bars) == 7
        assert len(split.test_bars) == 3
        assert split.train_bars == bars[:7]
        assert split.test_bars == bars[7:]

    def test_train_ratio_zero_raises(self):
        with pytest.raises(ValueError, match="train_ratio"):
            WalkForwardSplit.from_ratio(_flat_bars(10), train_ratio=0)

    def test_train_ratio_one_raises(self):
        with pytest.raises(ValueError, match="train_ratio"):
            WalkForwardSplit.from_ratio(_flat_bars(10), train_ratio=1)

    def test_train_ratio_negative_raises(self):
        with pytest.raises(ValueError, match="train_ratio"):
            WalkForwardSplit.from_ratio(_flat_bars(10), train_ratio=-0.2)

    def test_too_few_bars_raises(self):
        with pytest.raises(ValueError, match="at least 4 bars"):
            WalkForwardSplit.from_ratio(_flat_bars(3), train_ratio=0.7)

    def test_high_ratio_still_leaves_two_test_bars(self):
        """A 0.95 ratio on 10 bars would naively give train=9.5; test must
        still get at least 2 bars rather than being starved to 0 or 1."""
        bars = _flat_bars(10)
        split = WalkForwardSplit.from_ratio(bars, train_ratio=0.95)
        assert len(split.test_bars) >= 2

    def test_minimum_four_bars_splits_evenly(self):
        bars = _flat_bars(4)
        split = WalkForwardSplit.from_ratio(bars, train_ratio=0.5)
        assert len(split.train_bars) >= 2
        assert len(split.test_bars) >= 2


# ---------------------------------------------------------------------------
# evaluate_walk_forward — integration smoke test with a real engine run
# ---------------------------------------------------------------------------


class NeverTradeStrategy(BaseStrategy):
    async def generate_signal(self, data: MarketData) -> Signal:
        return Signal(symbol=data.symbol, action="HOLD", timestamp=data.timestamp)

    async def calculate_position_size(self, params: RiskParams) -> int:
        return 0

    def get_config_schema(self) -> dict:
        return {}


class TestEvaluateWalkForward:
    @pytest.mark.asyncio
    async def test_runs_both_windows_and_returns_results(self):
        bars = _flat_bars(20)
        split = WalkForwardSplit.from_ratio(bars, train_ratio=0.6)
        strategy = NeverTradeStrategy()

        result = await evaluate_walk_forward(
            strategy,
            RiskManager(),
            split,
            symbol="FLAT",
            account_balance=Decimal("100000"),
        )

        assert result.train_result.metrics is not None
        assert result.test_result.metrics is not None
        assert result.train_result.metrics.bars_tested == len(split.train_bars)
        assert result.test_result.metrics.bars_tested == len(split.test_bars)
        # No trades on either side — nothing to flag as overfit.
        assert result.is_likely_overfit is False


# ---------------------------------------------------------------------------
# _detect_overfit — heuristic logic, tested directly against synthetic metrics
# ---------------------------------------------------------------------------


class TestDetectOverfit:
    def test_no_metrics_is_not_overfit(self):
        is_overfit, reason = _detect_overfit(_result(None), _result(None))
        assert is_overfit is False
        assert reason is None

    def test_zero_train_trades_is_not_overfit(self):
        train = _result(_metrics(trade_count=0, win_rate_pct=0.0, total_return="0"))
        test = _result(_metrics(trade_count=10, win_rate_pct=10.0, total_return="-500"))
        is_overfit, _ = _detect_overfit(train, test)
        assert is_overfit is False

    def test_too_few_test_trades_flagged_low_confidence_not_overfit(self):
        train = _result(_metrics(trade_count=20, win_rate_pct=80.0, total_return="2000"))
        test = _result(_metrics(trade_count=2, win_rate_pct=0.0, total_return="-50"))
        is_overfit, reason = _detect_overfit(train, test)
        assert is_overfit is False
        assert reason is not None
        assert "too few" in reason.lower()

    def test_large_win_rate_drop_flags_overfit(self):
        train = _result(_metrics(trade_count=20, win_rate_pct=80.0, total_return="2000"))
        test = _result(_metrics(trade_count=10, win_rate_pct=30.0, total_return="100"))
        is_overfit, reason = _detect_overfit(train, test)
        assert is_overfit is True
        assert "win rate dropped" in reason.lower()

    def test_profitable_train_unprofitable_test_flags_overfit(self):
        train = _result(_metrics(trade_count=20, win_rate_pct=60.0, total_return="1500"))
        test = _result(_metrics(trade_count=10, win_rate_pct=55.0, total_return="-200"))
        is_overfit, reason = _detect_overfit(train, test)
        assert is_overfit is True
        assert "did not generalize" in reason.lower()

    def test_consistent_performance_is_not_overfit(self):
        train = _result(_metrics(trade_count=20, win_rate_pct=60.0, total_return="1500"))
        test = _result(_metrics(trade_count=10, win_rate_pct=58.0, total_return="700"))
        is_overfit, reason = _detect_overfit(train, test)
        assert is_overfit is False
        assert reason is None
