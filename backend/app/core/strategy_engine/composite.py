"""
CompositeStrategy — chains multiple strategies together into a single signal source.

Three combination modes:

  "any"      — OR gate. Runs all sub-strategies in order and returns the first
               non-HOLD signal found. Useful for running several independent
               strategies against the same symbols and taking any entry signal.

  "all"      — AND gate. All sub-strategies must return the same non-HOLD action
               on the same bar; otherwise HOLD. Acts as a confirmation gate —
               each sub-strategy is a condition that must be satisfied before
               the trade fires. Canonical use case: pair a trend-following
               strategy (macro regime confirmation) with a mean-reversion
               strategy (precise entry timing) so that mean-reversion entries
               are only taken during uptrends.

  "majority" — Vote gate. Requires a strict majority (more than half) of all
               sub-strategies to agree on the same non-HOLD action; otherwise
               HOLD. Looser than "all" — useful when chaining 3+ strategies
               where requiring unanimous agreement is too strict but a single
               outlier shouldn't block the trade either.

  When "all" or "majority" fires, the combined signal uses the agreeing
  sub-strategies' entry_price (from the first agreeing signal) but takes the
  most conservative (tightest) stop-loss among all of them — not an arbitrary
  "first sub-strategy wins". If the tightest stop differs from the first
  agreeing signal's own stop, its take_profit suggestion is dropped so
  RiskManager recalculates a fresh, R:R-consistent target for the tighter stop.

Sub-strategies are built from the global StrategyRegistry at construction time,
so all required strategy modules must be imported (registered) before a
CompositeStrategy can be instantiated.

Config keys:
    combination_mode  str   "any", "all", or "majority" (default "any")
    strategies        list  Ordered list of sub-strategy descriptors:
                            [{"type": "mean_reversion", "config": {...}}, ...]
                            "type" is required; "config" defaults to {}.
    symbols           list  Ticker symbols this strategy trades (default [])

Registration:
    Self-registers as "composite" when this module is imported.
    Import once on app startup (see main.py lifespan).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.strategy_engine.base import BaseStrategy, MarketData, RiskParams, Signal

_VALID_MODES = frozenset({"any", "all", "majority"})


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

        "any" mode:      returns the first non-HOLD signal encountered.
        "all" mode:      combines all sub-strategies' signals only when every
                          one produces the same non-HOLD action; otherwise HOLD.
        "majority" mode: combines the agreeing sub-strategies' signals when a
                          strict majority (> half) agree on the same non-HOLD
                          action; otherwise HOLD.
        """
        signals = [await s.generate_signal(market_data) for s in self._strategies]

        if self._mode == "any":
            for sig in signals:
                if sig.action != "HOLD":
                    return sig
            return self._hold(market_data.symbol)

        if self._mode == "all":
            actions = {sig.action for sig in signals}
            if len(actions) == 1 and "HOLD" not in actions:
                return self._combine_agreeing_signals(signals)
            return self._hold(market_data.symbol)

        # "majority" mode
        non_hold = [sig for sig in signals if sig.action != "HOLD"]
        if not non_hold:
            return self._hold(market_data.symbol)

        counts: dict[str, int] = {}
        for sig in non_hold:
            counts[sig.action] = counts.get(sig.action, 0) + 1
        majority_action, majority_count = max(counts.items(), key=lambda kv: kv[1])

        if majority_count > len(signals) / 2:
            agreeing = [sig for sig in non_hold if sig.action == majority_action]
            return self._combine_agreeing_signals(agreeing)
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
                    "enum": ["any", "all", "majority"],
                    "default": "any",
                    "description": (
                        "'any': fire on the first sub-strategy signal. "
                        "'all': require all sub-strategies to agree on the same action. "
                        "'majority': require a strict majority (> half) to agree."
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
            timestamp=datetime.now(UTC),
        )

    @staticmethod
    def _combine_agreeing_signals(agreeing: list[Signal]) -> Signal:
        """
        Build one signal from multiple sub-strategies that agree on direction.

        Uses the first agreeing signal's entry_price, but takes the most
        conservative (tightest) stop-loss among all of them rather than
        arbitrarily defaulting to whichever sub-strategy happened to be
        listed first — a confirmation gate should use the tightest risk any
        confirming strategy proposed.

        If the tightest stop differs from the primary signal's own stop, the
        primary's take_profit suggestion is dropped (set to None) so
        RiskManager computes a fresh target consistent with the tighter stop,
        rather than carrying over a take-profit sized for a different stop
        distance.
        """
        primary = agreeing[0]
        direction = primary.action  # "BUY" or "SELL"

        stops = [sig.stop_loss_price for sig in agreeing if sig.stop_loss_price is not None]
        tightest_stop = primary.stop_loss_price
        if stops:
            tightest_stop = max(stops) if direction == "BUY" else min(stops)

        stop_was_overridden = tightest_stop != primary.stop_loss_price

        return Signal(
            symbol=primary.symbol,
            action=primary.action,
            entry_price=primary.entry_price,
            stop_loss_price=tightest_stop,
            quantity=primary.quantity,
            strategy_id=primary.strategy_id,
            timestamp=primary.timestamp,
            take_profit_price=None if stop_was_overridden else primary.take_profit_price,
            submit_stop_to_broker=primary.submit_stop_to_broker,
        )


# ---------------------------------------------------------------------------
# Self-registration — runs when this module is imported.
# ---------------------------------------------------------------------------

from app.core.strategy_engine.registry import registry  # noqa: E402

registry.register("composite", CompositeStrategy)
