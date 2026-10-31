"""Small-account growth mode: entry limits while equity is below the exit level."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.brokers.base import AccountSummary


def effective_equity(summary: AccountSummary, capital_limit: Decimal | None) -> Decimal:
    """Net liquidation, capped at TRADING_CAPITAL_LIMIT when that is set."""
    if capital_limit is None:
        return summary.net_liquidation
    return min(summary.net_liquidation, capital_limit)


@dataclass(frozen=True)
class GrowthPolicy:
    enabled: bool
    exit_equity: Decimal
    max_open_positions: int
    universe: frozenset[str]

    @classmethod
    def from_settings(cls, settings) -> GrowthPolicy:
        return cls(
            enabled=settings.growth_mode_enabled,
            exit_equity=settings.growth_mode_exit_equity,
            max_open_positions=settings.growth_max_open_positions,
            universe=frozenset(s.upper() for s in settings.growth_universe),
        )

    def is_active(self, equity: Decimal) -> bool:
        return self.enabled and equity < self.exit_equity

    def allows(self, symbol: str) -> bool:
        return not self.universe or symbol.upper() in self.universe
