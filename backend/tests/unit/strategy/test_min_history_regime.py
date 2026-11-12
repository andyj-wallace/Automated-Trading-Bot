"""Unit tests for min_history_bars on the regime-filtered and composite strategies."""

from __future__ import annotations

import app.core.strategy_engine.mean_reversion  # noqa: F401
import app.core.strategy_engine.stock_trend  # noqa: F401
from app.core.strategy_engine.bull_bear import BullBearStrategy
from app.core.strategy_engine.composite import CompositeStrategy
from app.core.strategy_engine.trend_pullback import TrendPullbackStrategy


def test_trend_pullback_min_history_default():
    assert TrendPullbackStrategy(config={}).min_history_bars() >= 201
    assert TrendPullbackStrategy(config={}).min_history_bars() == 201


def test_trend_pullback_min_history_uses_regime_period():
    assert TrendPullbackStrategy(config={"regime_period": 300}).min_history_bars() == 300


def test_bull_bear_min_history_default():
    assert BullBearStrategy(config={}).min_history_bars() == 201


def test_composite_min_history_is_max_of_subs():
    composite = CompositeStrategy({"strategies": [
        {"type": "mean_reversion", "config": {}},
        {"type": "stock_trend", "config": {}},
    ]})
    assert composite.min_history_bars() == 201
