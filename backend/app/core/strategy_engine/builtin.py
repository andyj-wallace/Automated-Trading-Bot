"""Imports every built-in strategy module so each self-registers."""


def load_builtin_strategies() -> None:
    """Trigger self-registration of all built-in strategies."""
    import app.core.strategy_engine.bull_bear  # noqa: F401
    import app.core.strategy_engine.composite  # noqa: F401
    import app.core.strategy_engine.intra_week_reversion  # noqa: F401
    import app.core.strategy_engine.mean_reversion  # noqa: F401
    import app.core.strategy_engine.moving_average  # noqa: F401
    import app.core.strategy_engine.stock_trend  # noqa: F401
