"""
CompositeStrategy — chains multiple strategies together into a single signal source.

Two combination modes:

  "any"  — OR gate. Runs all sub-strategies in order and returns the first
            non-HOLD signal found. Useful for running several independent
            strategies against the same symbols and taking any entry signal.

  "all"  — AND gate. All sub-strategies must return the same non-HOLD action
            on the same bar. When they agree, the signal from the first
            sub-strategy (which carries its entry_price, stop_loss_price, and
            take_profit_price) is returned. If any sub-strategy is HOLD, or
            they disagree on direction, the composite returns HOLD.

            This mode acts as a confirmation gate — each sub-strategy is a
            condition that must be satisfied before the trade fires.
            Canonical use case: pair a trend-following strategy (macro regime
            confirmation) with a mean-reversion strategy (precise entry timing)
            so that mean-reversion entries are only taken during uptrends.

Sub-strategies are built from the global StrategyRegistry at construction time,
so all required strategy modules must be imported (registered) before a
CompositeStrategy can be instantiated.

Config keys:
    combination_mode  str   "any" or "all" (default "any")
    strategies        list  Ordered list of sub-strategy descriptors:
                            [{"type": "mean_reversion", "config": {...}}, ...]
                            "type" is required; "config" defaults to {}.
    symbols           list  Ticker symbols this strategy trades (default [])

Registration:
    Self-registers as "composite" when this module is imported.
    Import once on app startup (see main.py lifespan).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal

_VALID_MODES = frozenset({"any", "all"})


class CompositeStrategy(BaseStrategy):
    """
    Combines multiple strategies into a single signal using an AND or OR gate.

    Sub-strategies are instantiated from the registry during __init__, so
    any registry-registered strategy type can be nested inside a composite.
    Composites can be nested (a composite can contain another composite).

    The first sub-strategy in the list is the "primary": when "all" mode
    fires, its signal details (entry_price, stop_loss_price, take_profit_price)
    are used, and its calculate_position_size() is called by the scheduler.
    """

    def __init__(self, config: dict) -> None:
        mode = config.get("combination_mode", "any")
        if mode not in _VALID_MODES:
            raise ValueError(
                f"combination_mode must be one of {sorted(_VALID_MODES)}, got {mode!r}"
            )
        self._mode: str = mode

        raw = config.get("strategies", [])
        if not raw:
            raise ValueError(
                "CompositeStrategy requires at least one sub-strategy in 'strategies'"
            )

        # Import here to avoid circular imports at module load time
        from app.core.strategy_engine.registry import registry

        self._strategies: list[BaseStrategy] = []
        for i, spec in enumerate(raw):
            type_name = spec.get("type")
            if not type_name:
                raise ValueError(
                    f"Sub-strategy at index {i} is missing required 'type' key"
                )
            sub_config = spec.get("config", {})
            try:
                self._strategies.append(registry.build(type_name, sub_config))
            except KeyError as exc:
                raise ValueError(
                    f"Sub-strategy type {type_name!r} is not registered. "
                    "Ensure its module is imported before building a CompositeStrategy."
                ) from exc

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    async def generate_signal(self, market_data: MarketData) -> Signal:
        """
        Run all sub-strategies on the same market data and combine results.

        "any" mode: returns the first non-HOLD signal encountered.
        "all" mode: returns the first sub-strategy's signal only when every
                    sub-strategy produces the same non-HOLD action; otherwise HOLD.
        """
        signals = [await s.generate_signal(market_data) for s in self._strategies]

        if self._mode == "any":
            for sig in signals:
                if sig.action != "HOLD":
                    return sig
            return self._hold(market_data.symbol)

        # "all" mode
        actions = {sig.action for sig in signals}
        if len(actions) == 1 and "HOLD" not in actions:
            # All sub-strategies agree on the same non-HOLD action
            return signals[0]
        return self._hold(market_data.symbol)

    async def calculate_position_size(self, risk_params: RiskParams) -> int:
        """Delegates to the first (primary) sub-strategy."""
        return await self._strategies[0].calculate_position_size(risk_params)

    def get_config_schema(self) -> dict:
        """JSON Schema for the composite strategy configuration."""
        return {
            "type": "object",
            "properties": {
                "combination_mode": {
                    "type": "string",
                    "enum": ["any", "all"],
                    "default": "any",
                    "description": (
                        "'any': fire on the first sub-strategy signal. "
                        "'all': require all sub-strategies to agree on the same action."
                    ),
                },
                "strategies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "description": "Registered strategy type name",
                            },
                            "config": {
                                "type": "object",
                                "description": "Config dict passed to the sub-strategy constructor",
                            },
                        },
                        "required": ["type"],
                    },
                    "minItems": 1,
                    "description": "Ordered list of sub-strategy descriptors",
                },
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Ticker symbols this composite strategy trades",
                },
            },
            "required": ["strategies"],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _hold(self, symbol: str) -> Signal:
        return Signal(
            symbol=symbol,
            action="HOLD",
            timestamp=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Self-registration — runs when this module is imported.
# ---------------------------------------------------------------------------

from app.core.strategy_engine.registry import registry  # noqa: E402

registry.register("composite", CompositeStrategy)
