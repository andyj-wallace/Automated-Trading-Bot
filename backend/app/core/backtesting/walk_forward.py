"""
Walk-forward / train-test split harness for BacktestingEngine.

Addresses an overfitting risk a single `.run()` call can't prevent on its
own: nothing stops you from tuning a strategy's parameters against a
backtest, then calling `.run()` again on the exact same bars to "validate"
it — of course it looks great, you just fit it to noise in that data.

This harness makes the split explicit: build/configure your strategy using
only the train segment, then call `evaluate_walk_forward()` to see how that
SAME already-built strategy performs on a segment it never saw. It does not
run any parameter search itself — that responsibility (and the discipline
of only looking at train_bars while tuning) stays with the caller. What it
provides is the split plus a side-by-side comparison so a large train/test
performance gap is visible rather than silently ignored.

Usage:
    split = WalkForwardSplit.from_ratio(bars, train_ratio=0.7)

    # Tune strategy parameters using ONLY split.train_bars here.
    strategy = MovingAverageStrategy(config={...})

    result = await evaluate_walk_forward(
        strategy, risk_manager, split, symbol="AAPL", account_balance=Decimal("100000")
    )
    if result.is_likely_overfit:
        print(result.overfit_reason)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.brokers.base import PriceBar
from app.core.backtesting.engine import BacktestingEngine, BacktestResult
from app.core.risk.manager import RiskManager
from app.core.strategy_engine.base import BaseStrategy

# Heuristics, not proof — always look at both full results yourself.
_WIN_RATE_DEGRADATION_THRESHOLD_PCT = 20.0  # train - test win_rate_pct
_MIN_TEST_TRADES_FOR_CONFIDENCE = 5


@dataclass
class WalkForwardSplit:
    """A chronological train (in-sample) / test (out-of-sample) bar split."""

    train_bars: list[PriceBar]
    test_bars: list[PriceBar]

    @classmethod
    def from_ratio(
        cls, bars: list[PriceBar], train_ratio: Decimal | float = 0.7
    ) -> WalkForwardSplit:
        """
        Split `bars` chronologically: the first `train_ratio` fraction is the
        train window, the remainder is test.

        Splitting chronologically (not randomly) matters — shuffling bars
        would leak future information into the train set.
        """
        ratio = float(train_ratio)
        if not (0 < ratio < 1):
            raise ValueError(f"train_ratio must be between 0 and 1, got {train_ratio}")
        if len(bars) < 4:
            raise ValueError(
                "Need at least 4 bars to create a meaningful walk-forward split."
            )

        split_idx = max(2, int(len(bars) * ratio))
        split_idx = min(split_idx, len(bars) - 2)  # leave >= 2 bars for the test window
        return cls(train_bars=bars[:split_idx], test_bars=bars[split_idx:])


@dataclass
class WalkForwardResult:
    """Side-by-side train vs. test backtest results plus an overfit flag."""

    train_result: BacktestResult
    test_result: BacktestResult
    is_likely_overfit: bool
    overfit_reason: str | None


async def evaluate_walk_forward(
    strategy: BaseStrategy,
    risk_manager: RiskManager,
    split: WalkForwardSplit,
    symbol: str,
    account_balance: Decimal,
    strategy_type: str = "unknown",
    strategy_config: dict | None = None,
) -> WalkForwardResult:
    """
    Run the same already-configured strategy against both windows of `split`
    and flag a likely overfit if test performance collapses relative to
    train. The strategy must already be fully configured before calling
    this — tuning must only ever look at split.train_bars.
    """
    engine = BacktestingEngine(strategy, risk_manager)

    train_result = await engine.run(
        split.train_bars,
        symbol=symbol,
        account_balance=account_balance,
        strategy_type=strategy_type,
        strategy_config=strategy_config,
    )
    test_result = await engine.run(
        split.test_bars,
        symbol=symbol,
        account_balance=account_balance,
        strategy_type=strategy_type,
        strategy_config=strategy_config,
    )

    is_overfit, reason = _detect_overfit(train_result, test_result)

    return WalkForwardResult(
        train_result=train_result,
        test_result=test_result,
        is_likely_overfit=is_overfit,
        overfit_reason=reason,
    )


def _detect_overfit(train: BacktestResult, test: BacktestResult) -> tuple[bool, str | None]:
    """Heuristic-only overfit check — a hint to investigate, not a verdict."""
    if train.metrics is None or test.metrics is None or train.metrics.trade_count == 0:
        return False, None

    if test.metrics.trade_count < _MIN_TEST_TRADES_FOR_CONFIDENCE:
        return False, (
            f"Test window only produced {test.metrics.trade_count} trade(s) — "
            "too few to draw a confident conclusion either way."
        )

    win_rate_drop = train.metrics.win_rate_pct - test.metrics.win_rate_pct
    if win_rate_drop >= _WIN_RATE_DEGRADATION_THRESHOLD_PCT:
        return True, (
            f"Win rate dropped {win_rate_drop:.1f} percentage points from train "
            f"({train.metrics.win_rate_pct:.1f}%) to test ({test.metrics.win_rate_pct:.1f}%) "
            "— the strategy may be fit to noise in the train window rather than a "
            "real, repeatable edge."
        )

    if train.metrics.total_return > 0 and test.metrics.total_return <= 0:
        return True, (
            "Train window was profitable but the test window was flat or lost "
            "money — performance did not generalize out of sample."
        )

    return False, None
