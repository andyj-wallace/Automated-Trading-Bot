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
export type TradeStatus =
  | "PENDING"
  | "SUBMITTED"
  | "OPEN"
  | "CLOSING"
  | "CLOSED"
  | "CANCELLED";
export type ExitReason =
  | "STOP_LOSS"
  | "TAKE_PROFIT"
  | "MANUAL"
  | "STALE_TIMEOUT"
  | "EXTERNAL";

export interface Trade {
  id: string;
  strategy_id: string | null;
  symbol: string;
  direction: TradeDirection;
  quantity: string;
  status: TradeStatus;
  entry_price: string;
  stop_loss_price: string;
  take_profit_price: string;
  reward_to_risk_ratio: string;
  risk_amount: string;
  account_balance_at_entry: string;
  exit_price: string | null;
  exit_reason: ExitReason | null;
  entry_fill_price: string | null;
  filled_quantity: string | null;
  current_stop_price: string | null;
  close_attempts: number;
  pnl: string | null;
  executed_at: string;
  closed_at: string | null;
  created_at: string;
}

export type CloseOutcome = "CLOSED" | "PENDING_EXIT" | "NOT_OPEN" | "MARKET_CLOSED" | "FAILED";

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

export interface StrategyType {
  type: string;
  config_schema: JsonSchema;
}

export interface JsonSchemaProperty {
  type?: string | string[];
  enum?: string[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  description?: string;
  items?: JsonSchemaProperty;
  properties?: Record<string, JsonSchemaProperty>;
}

export interface JsonSchema {
  type?: string;
  properties?: Record<string, JsonSchemaProperty>;
  required?: string[];
}

export interface StrategyCreate {
  name: string;
  type: string;
  config: Record<string, unknown>;
  is_enabled: boolean;
}

export interface StrategySignal {
  id: string;
  strategy_id: string;
  symbol: string;
  bar_date: string;
  action: string;
  entry_price: string | null;
  stop_loss_price: string | null;
  take_profit_price: string | null;
  outcome: string;
  detail: string | null;
  trade_id: string | null;
  created_at: string;
}

export interface PriceBar {
  time: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: number | string;
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

export interface BrokerConnection {
  state: "connected" | "reconnecting" | "down" | "account_mismatch";
  down_since: string | null;
  last_connected_at: string | null;
  last_error: string | null;
  hint: string | null;
}

export interface TradingStatus {
  paused: boolean;
  market_open: boolean;
  in_eval_window: boolean;
  broker_connected: boolean;
  engine_lock_held: boolean;
  scheduler_last_cycle_at: string | null;
  open_trade_count: number;
  circuit_breaker_halted: boolean;
  broker: BrokerConnection;
}

export interface FlattenResult {
  paused: boolean;
  results: { trade_id: string; symbol: string; outcome: string }[];
}

export interface CircuitBreakerStatus {
  daily_halted: boolean;
  daily_realized_pnl: string;
  day_starting_balance: string;
  killed_strategy_ids: string[];
  trading_date: string;
  daily_loss_limit_pct: string;
  halt_threshold_amount: string;
  consecutive_losses: Record<string, number>;
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
  | "trade_closed"
  | "risk_alert"
  | "system_alert";

export interface DashboardEvent {
  event: DashboardEventType | string;
  payload: Record<string, unknown>;
  timestamp: string;
}
