/**
 * Shared TypeScript types for the trading bot API.
 *
 * All REST responses follow the envelope shape:
 *   { data, meta, error }
 *
 * These types mirror the Pydantic schemas in app/api/v1/schemas.py.
 */

// ---------------------------------------------------------------------------
// Response envelope
// ---------------------------------------------------------------------------

export interface ApiMeta {
  timestamp: string;
  request_id: string;
}

export interface ApiError {
  code: string;
  message: string;
}

export interface ApiResponse<T> {
  data: T | null;
  meta: ApiMeta;
  error: ApiError | null;
}

// ---------------------------------------------------------------------------
// Symbol
// ---------------------------------------------------------------------------

export interface WatchedSymbol {
  id: string;
  ticker: string;
  display_name: string | null;
  is_active: boolean;
  added_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Trade
// ---------------------------------------------------------------------------

export type TradeDirection = "BUY" | "SELL";
export type TradeStatus = "OPEN" | "CLOSED" | "CANCELLED";

export interface Trade {
  id: string;
  strategy_id: string | null;
  symbol: string;
  direction: TradeDirection;
  quantity: string;
  entry_price: string;
  stop_loss_price: string;
  exit_price: string | null;
  status: TradeStatus;
  risk_amount: string;
  account_balance_at_entry: string;
  pnl: string | null;
  executed_at: string;
  closed_at: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Strategy
// ---------------------------------------------------------------------------

export interface Strategy {
  id: string;
  name: string;
  type: string;
  is_enabled: boolean;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface StrategyPatch {
  is_enabled?: boolean;
  config?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Portfolio / risk
// ---------------------------------------------------------------------------

export interface RiskStatus {
  aggregate_risk_amount: string;
  aggregate_risk_pct: string;
  open_trade_count: number;
  account_balance: string;
  alert_level: "NONE" | "WARNING" | "CRITICAL";
  warning_threshold_pct: string;
  critical_threshold_pct: string;
}

// ---------------------------------------------------------------------------
// System health
// ---------------------------------------------------------------------------

export interface ComponentStatus {
  status: "ok" | "error" | "disconnected";
  detail: string | null;
}

export interface SystemHealth {
  status: "ok" | "degraded";
  broker: ComponentStatus;
  database: ComponentStatus;
  redis: ComponentStatus;
}

// ---------------------------------------------------------------------------
// WebSocket dashboard events
// ---------------------------------------------------------------------------

export type DashboardEventType =
  | "price_update"
  | "trade_executed"
  | "risk_alert"
  | "position_update";

export interface DashboardEvent {
  event: DashboardEventType | string;
  payload: Record<string, unknown>;
  timestamp: string;
}
