"""
Unit tests for StrategyRegistry (Layer 11.1).
"""

from decimal import Decimal

import pytest

from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal
from app.core.strategy_engine.registry import StrategyRegistry


# ---------------------------------------------------------------------------
# Minimal concrete strategy for testing
# ---------------------------------------------------------------------------


class AlwaysHoldStrategy(BaseStrategy):
    def __init__(self, config: dict) -> None:
        self.config = config

    async def generate_signal(self, market_data: MarketData) -> Signal:
        from datetime import datetime, timezone

        return Signal(symbol=market_data.symbol, action="HOLD", timestamp=datetime.now(timezone.utc))

    async def calculate_position_size(self, risk_params: RiskParams) -> int:
        return 0

    def get_config_schema(self) -> dict:
        return {}


class AlwaysBuyStrategy(BaseStrategy):
    def __init__(self, config: dict) -> None:
        self.config = config

    async def generate_signal(self, market_data: MarketData) -> Signal:
        from datetime import datetime, timezone

        return Signal(
            symbol=market_data.symbol,
            action="BUY",
            entry_price=market_data.current_price,
            stop_loss_price=market_data.current_price - Decimal("5"),
            timestamp=datetime.now(timezone.utc),
        )

    async def calculate_position_size(self, risk_params: RiskParams) -> int:
        return 10

    def get_config_schema(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def reg() -> StrategyRegistry:
    """Fresh registry per test — avoids cross-test pollution."""
    return StrategyRegistry()


def test_register_and_get_class(reg: StrategyRegistry) -> None:
    reg.register("hold", AlwaysHoldStrategy)
    assert reg.get_class("hold") is AlwaysHoldStrategy


def test_registered_types_sorted(reg: StrategyRegistry) -> None:
    reg.register("zebra", AlwaysBuyStrategy)
    reg.register("apple", AlwaysHoldStrategy)
    assert reg.registered_types() == ["apple", "zebra"]


def test_is_registered_true(reg: StrategyRegistry) -> None:
    reg.register("hold", AlwaysHoldStrategy)
    assert reg.is_registered("hold") is True


def test_is_registered_false(reg: StrategyRegistry) -> None:
    assert reg.is_registered("nonexistent") is False


def test_duplicate_registration_raises(reg: StrategyRegistry) -> None:
    reg.register("hold", AlwaysHoldStrategy)
    with pytest.raises(ValueError, match="already registered"):
        reg.register("hold", AlwaysBuyStrategy)


def test_get_class_unknown_raises(reg: StrategyRegistry) -> None:
    with pytest.raises(KeyError, match="not registered"):
        reg.get_class("unknown")


def test_build_returns_correct_type(reg: StrategyRegistry) -> None:
    reg.register("hold", AlwaysHoldStrategy)
    instance = reg.build("hold", config={"param": 1})
    assert isinstance(instance, AlwaysHoldStrategy)


def test_build_passes_config(reg: StrategyRegistry) -> None:
    reg.register("hold", AlwaysHoldStrategy)
    instance = reg.build("hold", config={"key": "value"})
    assert instance.config == {"key": "value"}


def test_build_returns_fresh_instance_each_call(reg: StrategyRegistry) -> None:
    reg.register("hold", AlwaysHoldStrategy)
    a = reg.build("hold", config={})
    b = reg.build("hold", config={})
    assert a is not b


def test_build_unknown_type_raises(reg: StrategyRegistry) -> None:
    with pytest.raises(KeyError):
        reg.build("ghost", config={})


def test_empty_registry_has_no_types(reg: StrategyRegistry) -> None:
    assert reg.registered_types() == []
