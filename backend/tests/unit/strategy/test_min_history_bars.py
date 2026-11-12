"""Unit tests for BaseStrategy.min_history_bars and the daily strategy overrides."""

from __future__ import annotations

from app.core.strategy_engine.base import BaseStrategy
from app.core.strategy_engine.intra_week_reversion import IntraWeekMeanReversionStrategy
from app.core.strategy_engine.mean_reversion import MeanReversionStrategy
from app.core.strategy_engine.moving_average import MovingAverageStrategy
from app.core.strategy_engine.stock_trend import StockTrendStrategy


class _MinimalStrategy(BaseStrategy):
    """Implements only the abstract methods so the base default can be checked."""

    async def generate_signal(self, market_data):  # pragma: no cover - not called
        raise NotImplementedError

    async def calculate_position_size(self, risk_params) -> int:  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def get_config_schema(cls) -> dict:
        return {}


def test_base_default_is_zero() -> None:
    assert _MinimalStrategy().min_history_bars() == 0


def test_moving_average_min_history() -> None:
    assert MovingAverageStrategy({}).min_history_bars() == 201
    assert MovingAverageStrategy({"slow_period": 100}).min_history_bars() == 101


def test_mean_reversion_min_history() -> None:
    assert MeanReversionStrategy({}).min_history_bars() == 21
    assert MeanReversionStrategy({"max_trend_pct": "0.03"}).min_history_bars() == 30


def test_stock_trend_min_history() -> None:
    assert StockTrendStrategy({}).min_history_bars() == 201


def test_intra_week_reversion_min_history() -> None:
    assert IntraWeekMeanReversionStrategy({}).min_history_bars() == 50
