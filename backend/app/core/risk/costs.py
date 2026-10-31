"""Estimated trading costs (IBKR Pro US stock rates, 2026-10) and the break-even win rate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class CommissionPlan:
    per_share: Decimal
    min_per_order: Decimal
    max_pct_of_value: Decimal
    third_party_per_share: Decimal  # exchange, clearing and regulatory fees billed on top (Tiered)


PLANS: dict[str, CommissionPlan] = {
    "ibkr_fixed": CommissionPlan(Decimal("0.005"), Decimal("1.00"), Decimal("0.01"), Decimal("0")),
    "ibkr_tiered": CommissionPlan(Decimal("0.0035"), Decimal("0.35"), Decimal("0.01"), Decimal("0.0035")),
    "zero": CommissionPlan(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("0")),
}


@dataclass(frozen=True)
class CostModel:
    plan: CommissionPlan
    slippage_bps: Decimal

    @classmethod
    def from_settings(cls, settings) -> CostModel:
        return cls(PLANS[settings.commission_plan], settings.slippage_bps)

    def commission(self, quantity: Decimal, price: Decimal) -> Decimal:
        """One order's commission; IBKR applies the 1%-of-value cap even when it is below the minimum."""
        if quantity <= 0:
            return Decimal("0")
        base = min(
            max(self.plan.per_share * quantity, self.plan.min_per_order),
            self.plan.max_pct_of_value * quantity * price,
        )
        return (base + self.plan.third_party_per_share * quantity).quantize(_CENT, rounding=ROUND_HALF_UP)

    def slippage(self, quantity: Decimal, price: Decimal) -> Decimal:
        return quantity * price * self.slippage_bps / Decimal("10000")

    def round_trip_cost(self, quantity: Decimal, price: Decimal) -> Decimal:
        """Buy and sell commissions plus slippage on both legs, both priced at `price`."""
        return 2 * (self.commission(quantity, price) + self.slippage(quantity, price))


def breakeven_win_rate(risk: Decimal, reward: Decimal, cost: Decimal) -> Decimal:
    """Share of trades that must win to break even after costs: (risk + cost) / (reward + risk)."""
    return (risk + cost) / (reward + risk)
