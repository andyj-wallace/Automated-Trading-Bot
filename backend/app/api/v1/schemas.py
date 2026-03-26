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

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------

def _meta(request_id: str | None = None) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
    exit_price: Decimal | None
    status: str
    risk_amount: Decimal
    account_balance_at_entry: Decimal
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


class SystemHealthOut(BaseModel):
    status: str  # "ok" | "degraded"
    broker: ComponentStatus
    database: ComponentStatus
    redis: ComponentStatus
