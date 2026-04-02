"""
StrategyRegistry — process-level map from strategy type name → class.

Concrete strategy modules call `registry.register(type_name, cls)` at
import time. The scheduler looks up and instantiates strategies by type.

Usage (registration, done once per strategy module):
    from app.core.strategy_engine.registry import registry
    registry.register("moving_average", MovingAverageStrategy)

Usage (instantiation, done by the scheduler):
    strategy = registry.build("moving_average", config={"fast_period": 50})

Each call to build() returns a fresh instance, so strategies are stateless
between scheduler cycles.
"""

from __future__ import annotations

from app.core.strategy_engine.base import BaseStrategy
from app.monitoring.logger import system_logger


class StrategyRegistry:
    """
    In-memory registry of strategy type names → concrete classes.

    The registry itself carries no state beyond the class map. Strategy
    enable/disable state is owned by the DB (TradingStrategy.is_enabled)
    and queried fresh each scheduler cycle.
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[BaseStrategy]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, type_name: str, cls: type[BaseStrategy]) -> None:
        """
        Register a strategy class under type_name.

        Raises ValueError if type_name is already registered.
        """
        if type_name in self._classes:
            raise ValueError(
                f"Strategy type {type_name!r} is already registered. "
                "Each type name must be unique."
            )
        self._classes[type_name] = cls
        system_logger.info(
            "StrategyRegistry: registered strategy",
            extra={"type_name": type_name, "class": cls.__name__},
        )

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_class(self, type_name: str) -> type[BaseStrategy]:
        """
        Return the class for type_name.

        Raises KeyError if the type is not registered.
        """
        if type_name not in self._classes:
            raise KeyError(
                f"Strategy type {type_name!r} is not registered. "
                f"Registered types: {self.registered_types()}"
            )
        return self._classes[type_name]

    def build(self, type_name: str, config: dict) -> BaseStrategy:
        """
        Instantiate and return a strategy by type name with the given config.

        Concrete strategies must accept a `config: dict` keyword argument.
        Returns a fresh instance on every call (strategies are stateless
        between cycles).

        Raises KeyError if the type is not registered.
        Raises TypeError if the class constructor rejects the config.
        """
        cls = self.get_class(type_name)
        return cls(config=config)

    def registered_types(self) -> list[str]:
        """Return a sorted list of all registered type names."""
        return sorted(self._classes.keys())

    def is_registered(self, type_name: str) -> bool:
        """Return True if type_name has been registered."""
        return type_name in self._classes


# ---------------------------------------------------------------------------
# Module-level singleton — import and use this everywhere.
# ---------------------------------------------------------------------------

registry = StrategyRegistry()
