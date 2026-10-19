"""Seed one disabled strategy row per registered strategy type; config validation helpers."""

from __future__ import annotations

from app.core.strategy_engine.registry import StrategyRegistry
from app.monitoring.logger import system_logger


def default_config(schema: dict) -> dict:
    """Default values declared in a strategy's JSON schema."""
    return {
        key: spec["default"]
        for key, spec in schema.get("properties", {}).items()
        if "default" in spec
    }


def config_error(registry: StrategyRegistry, type_name: str, config: dict) -> str | None:
    """Return why `config` is invalid for `type_name`, or None if it builds."""
    if not registry.is_registered(type_name):
        return f"Unknown strategy type {type_name!r}"
    symbols = config.get("symbols", [])
    if not isinstance(symbols, list) or not all(
        isinstance(s, str) and 1 <= len(s.strip()) <= 10 for s in symbols
    ):
        return "config.symbols must be a list of 1-10 character strings"
    try:
        registry.build(type_name, config)
    except Exception as exc:
        return str(exc)
    return None


def normalize_config(config: dict) -> dict:
    """Return a copy of `config` with symbols stripped and uppercased."""
    normalized = dict(config)
    if isinstance(normalized.get("symbols"), list):
        normalized["symbols"] = [s.strip().upper() for s in normalized["symbols"]]
    return normalized


async def seed_strategies(registry: StrategyRegistry, session_factory) -> int:
    """Create a disabled row for each registered non-composite type that has none."""
    from app.db.repositories.strategy_repo import StrategyRepo

    created = 0
    async with session_factory() as session:
        repo = StrategyRepo(session)
        existing = {s.type for s in await repo.get_all()}
        for type_name in registry.registered_types():
            if type_name == "composite" or type_name in existing:
                continue
            config = default_config(registry.get_class(type_name).get_config_schema())
            error = config_error(registry, type_name, config)
            if error:
                system_logger.warning(
                    "Strategy seed skipped, default config invalid",
                    extra={"strategy_type": type_name, "error": error},
                )
                continue
            await repo.create(name=type_name, type=type_name, config=config, is_enabled=False)
            created += 1
        await session.commit()
    return created
