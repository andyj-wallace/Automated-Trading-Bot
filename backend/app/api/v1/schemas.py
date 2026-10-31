"""
Standard JSON response envelope for all API v1 endpoints.

Success response shape:
    {
        "data": <resource or list>,
        "meta": { "timestamp": "...", "request_id": "..." },
        "error": null
    }

Error response shape:
    {
        "data": null,
        "meta": { "timestamp": "...", "request_id": "..." },
        "error": { "code": "...", "message": "..." }
    }
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------

def _meta(request_id: str | None = None) -> dict:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "request_id": request_id or str(uuid.uuid4()),
    }


def ok(data: Any, request_id: str | None = None) -> dict:
    """Build a success response envelope."""
    return {"data": data, "meta": _meta(request_id), "error": None}


def err(code: str, message: str, request_id: str | None = None) -> dict:
    """Build an error response envelope."""
    return {"data": None, "meta": _meta(request_id), "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Symbol schemas
# ---------------------------------------------------------------------------

class SymbolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticker: str
    display_name: str | None
    is_active: bool
    added_at: datetime
    updated_at: datetime


class SymbolCreateIn(BaseModel):
    ticker: str
    display_name: str | None = None


class SymbolDeleteConfirmIn(BaseModel):
    confirm: bool = False


# ---------------------------------------------------------------------------
# Trade schemas
# ---------------------------------------------------------------------------

class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    strategy_id: uuid.UUID | None
    symbol: str
    direction: str
    quantity: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal
    reward_to_risk_ratio: Decimal
    exit_price: Decimal | None
    exit_reason: str | None
    status: str
    risk_amount: Decimal
    account_balance_at_entry: Decimal
    entry_fill_price: Decimal | None
    filled_quantity: Decimal | None
    entry_commission: Decimal | None = None
    exit_commission: Decimal | None = None
    current_stop_price: Decimal | None
    close_attempts: int
    pnl: Decimal | None
    executed_at: datetime
    closed_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Strategy schemas
# ---------------------------------------------------------------------------

class StrategyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    type: str
    is_enabled: bool
    config: dict
    created_at: datetime
    updated_at: datetime


class StrategyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: str
    config: dict = {}
    is_enabled: bool = False


class StrategyPatchIn(BaseModel):
    is_enabled: bool | None = None
    config: dict | None = None


# ---------------------------------------------------------------------------
# Portfolio / risk schemas
# ---------------------------------------------------------------------------

class RiskStatusOut(BaseModel):
    aggregate_risk_amount: str  # Decimal serialised as string for JSON precision
    aggregate_risk_pct: str
    open_trade_count: int
    account_balance: str
    alert_level: str  # "NONE" | "WARNING" | "CRITICAL"
    warning_threshold_pct: str
    critical_threshold_pct: str


# ---------------------------------------------------------------------------
# System health schemas
# ---------------------------------------------------------------------------

class ComponentStatus(BaseModel):
    status: str  # "ok" | "error" | "disconnected"
    detail: str | None = None
    info: dict | None = None


class SystemHealthOut(BaseModel):
    status: str  # "ok" | "degraded"
    broker: ComponentStatus
    database: ComponentStatus
    redis: ComponentStatus
    engine: ComponentStatus


# ---------------------------------------------------------------------------
# Trading control schemas
# ---------------------------------------------------------------------------

class PauseIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class FlattenIn(BaseModel):
    confirm: str


class ManualTradeIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=10)
    stop_loss_price: Decimal
    take_profit_price: Decimal | None = None
    quantity: int | None = Field(default=None, ge=1)


class SignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    strategy_id: uuid.UUID
    symbol: str
    bar_date: date
    action: str
    entry_price: Decimal | None
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    outcome: str
    detail: str | None
    trade_id: uuid.UUID | None
    created_at: datetime


class BarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
