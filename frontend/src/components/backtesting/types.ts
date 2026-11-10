// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface BacktestJob {
  job_id: string;
  status: "pending" | "running" | "done" | "error";
  job_type?: "single" | "portfolio";
  symbol?: string;
  strategy_type?: string;
  slot_count?: number;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  error?: string;
  result?: BacktestResult | PortfolioBacktestResult;
}

export interface BacktestResult {
  symbol: string;
  strategy_type: string;
  account_balance: string;
  start_time: string;
  end_time: string;
  trades: BacktestTrade[];
  metrics: BacktestMetrics | null;
}

export interface PortfolioBacktestResult {
  account_balance: string;
  start_time: string;
  end_time: string;
  per_strategy: BacktestResult[];
  combined_equity_curve: [string, string][];
  combined_metrics: BacktestMetrics;
}

export interface BacktestTrade {
  trade_id: string;
  entry_price: string;
  exit_price: string | null;
  stop_loss_price: string;
  take_profit_price: string;
  quantity: number;
  pnl: string | null;
  exit_reason: string | null;
  entry_time: string;
  exit_time: string | null;
}

export interface BacktestMetrics {
  trade_count: number;
  win_count: number;
  loss_count: number;
  win_rate_pct: number;
  total_return: string;
  total_return_pct: number;
  avg_trade_pnl: string;
  avg_winner: string;
  avg_loser: string;
  largest_winner: string;
  largest_loser: string;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  bars_tested: number;
  signals_generated: number;
  signals_rejected: number;
  ending_balance: string;
  total_commission_paid: string;
}

// Portfolio slot row state
export interface SlotRow {
  id: number;
  symbol: string;
  strategyType: string;
}
