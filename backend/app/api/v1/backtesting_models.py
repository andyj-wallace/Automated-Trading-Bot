"""Request models and the in-memory job record for the backtesting API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.core.backtesting.engine import BacktestResult
from app.core.backtesting.portfolio_engine import PortfolioBacktestResult
from app.core.risk.manager import RiskManager


class _Job:
    def __init__(self, job_id: uuid.UUID, request: Any) -> None:
        self.job_id = job_id
        self.request = request
        # (RiskManager, engine kwargs) from _build_costs; None keeps the legacy engines.
        self.costs: tuple[RiskManager, dict] | None = None
        # Extra-symbol bars: a dict for a single run, one dict per slot for a portfolio run.
        self.extra_bars: Any = None
        self.status: str = "pending"
        self.error: str | None = None
        self.result: BacktestResult | None = None
        self.portfolio_result: PortfolioBacktestResult | None = None
        self.created_at: datetime = datetime.now(UTC)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class BacktestRunRequest(BaseModel):
    symbol: str
    strategy_type: str
    strategy_config: dict[str, Any] = {}
    account_balance: str = "100000"
    commission_plan: Literal["legacy", "ibkr_fixed", "ibkr_tiered", "zero"] = "legacy"
    slippage_bps: str = "5"
    compound: bool = False


class PortfolioSlotRequest(BaseModel):
    symbol: str
    strategy_type: str
    config: dict[str, Any] = {}


_RANGE_DAYS: dict[str, int] = {
    "1y": 365,
    "2y": 730,
    "5y": 365 * 5,
}


class PortfolioBacktestRunRequest(BaseModel):
    slots: list[PortfolioSlotRequest]
    account_balance: str = "100000"
    range: str = "5y"
    commission_plan: Literal["legacy", "ibkr_fixed", "ibkr_tiered", "zero"] = "legacy"
    slippage_bps: str = "5"
    compound: bool = False
