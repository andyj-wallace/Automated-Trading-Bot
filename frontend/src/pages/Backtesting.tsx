/**
 * BacktestingPage — Layer 15.4.
 *
 * Form: symbol, strategy type, config (fast/slow period for MA), account balance.
 * Submits to POST /api/v1/backtesting/run → polls GET /api/v1/backtesting/:id
 * until status == "done" or "error".
 * Results show metrics KPIs + trade log table + cumulative P&L sparkline.
 */

import { useState, useEffect, useRef } from "react";
import { api } from "../api/client";

// ---------------------------------------------------------------------------
// Types (subset of what the backend returns)
// ---------------------------------------------------------------------------

interface BacktestJob {
  job_id: string;
  status: "pending" | "running" | "done" | "error";
  symbol: string;
  strategy_type: string;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  error?: string;
  result?: BacktestResult;
}

interface BacktestResult {
  symbol: string;
  strategy_type: string;
  account_balance: string;
  start_time: string;
  end_time: string;
  trades: BacktestTrade[];
  metrics: BacktestMetrics | null;
}

interface BacktestTrade {
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

interface BacktestMetrics {
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
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function Backtesting() {
  const [symbol, setSymbol] = useState("AAPL");
  const [strategyType] = useState("moving_average");
  const [fastPeriod, setFastPeriod] = useState(50);
  const [slowPeriod, setSlowPeriod] = useState(200);
  const [stopPct, setStopPct] = useState("0.03");
  const [balance, setBalance] = useState("100000");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [job, setJob] = useState<BacktestJob | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Polling
  useEffect(() => {
    if (!job || job.status === "done" || job.status === "error") {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }

    pollRef.current = setInterval(async () => {
      const res = await api.get<BacktestJob>(`/backtesting/${job.job_id}`);
      if (res.data) setJob(res.data);
    }, 2000);

    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [job?.job_id, job?.status]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitError(null);
    setJob(null);
    setSubmitting(true);

    try {
      const res = await api.post<BacktestJob>("/backtesting/run", {
        symbol: symbol.toUpperCase(),
        strategy_type: strategyType,
        strategy_config: {
          fast_period: fastPeriod,
          slow_period: slowPeriod,
          stop_loss_pct: stopPct,
        },
        account_balance: balance,
      });
      if (res.error) throw new Error(res.error.message);
      if (res.data) setJob(res.data);
    } catch (err: unknown) {
      setSubmitError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="p-8 max-w-5xl flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold text-white mb-1">Backtesting</h1>
        <p className="text-gray-500 text-sm">
          Replay a strategy against historical OHLCV data
        </p>
      </div>

      {/* Run form */}
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-4">
          Configure Backtest
        </h2>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Field label="Symbol">
              <input
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                className={inputCls}
                placeholder="AAPL"
                maxLength={10}
                required
              />
            </Field>
            <Field label="Account Balance ($)">
              <input
                type="number"
                value={balance}
                onChange={(e) => setBalance(e.target.value)}
                className={inputCls}
                min={1000}
                required
              />
            </Field>
            <Field label="Fast MA Period">
              <input
                type="number"
                value={fastPeriod}
                onChange={(e) => setFastPeriod(parseInt(e.target.value, 10))}
                className={inputCls}
                min={1}
                max={slowPeriod - 1}
                required
              />
            </Field>
            <Field label="Slow MA Period">
              <input
                type="number"
                value={slowPeriod}
                onChange={(e) => setSlowPeriod(parseInt(e.target.value, 10))}
                className={inputCls}
                min={fastPeriod + 1}
                required
              />
            </Field>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Field label='Stop Loss % (e.g. "0.03")'>
              <input
                value={stopPct}
                onChange={(e) => setStopPct(e.target.value)}
                className={inputCls}
                placeholder="0.03"
                required
              />
            </Field>
            <div className="flex items-end">
              <button
                type="submit"
                disabled={submitting || (job?.status === "pending") || (job?.status === "running")}
                className="w-full px-4 py-2 text-sm font-medium bg-blue-600 hover:bg-blue-700 disabled:bg-blue-900 disabled:text-blue-400 text-white rounded transition-colors"
              >
                {submitting ? "Submitting…" : "Run Backtest"}
              </button>
            </div>
          </div>

          {submitError && (
            <p className="text-xs text-red-400">{submitError}</p>
          )}
        </form>
      </div>

      {/* Job status */}
      {job && (
        <JobStatus job={job} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Job status + results
// ---------------------------------------------------------------------------

function JobStatus({ job }: { job: BacktestJob }) {
  const statusColour = {
    pending: "text-gray-400",
    running: "text-blue-400",
    done: "text-emerald-400",
    error: "text-red-400",
  }[job.status];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        {(job.status === "pending" || job.status === "running") && (
          <span className="inline-block w-3 h-3 rounded-full bg-blue-500 animate-pulse" />
        )}
        <span className={`text-sm font-medium ${statusColour} capitalize`}>
          {job.status}
        </span>
        <span className="text-xs text-gray-600">
          {job.symbol} · {job.strategy_type}
        </span>
      </div>

      {job.status === "error" && (
        <div className="rounded border border-red-800 bg-red-900/30 px-4 py-3 text-xs text-red-400">
          {job.error}
        </div>
      )}

      {job.status === "done" && job.result && (
        <BacktestResults result={job.result} />
      )}
    </div>
  );
}

function BacktestResults({ result }: { result: BacktestResult }) {
  const m = result.metrics;
  if (!m) return null;

  const totalReturn = parseFloat(result.metrics!.total_return);

  return (
    <div className="flex flex-col gap-4">
      {/* Metrics KPIs */}
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-4">
          Results — {result.symbol} ({new Date(result.start_time).toLocaleDateString()} → {new Date(result.end_time).toLocaleDateString()})
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MetricCard label="Total Return" value={`${totalReturn >= 0 ? "+" : ""}$${Math.abs(totalReturn).toLocaleString("en-US", { minimumFractionDigits: 2 })}`} valueClass={totalReturn >= 0 ? "text-emerald-400" : "text-red-400"} />
          <MetricCard label="Return %" value={`${m.total_return_pct >= 0 ? "+" : ""}${m.total_return_pct.toFixed(2)}%`} valueClass={m.total_return_pct >= 0 ? "text-emerald-400" : "text-red-400"} />
          <MetricCard label="Win Rate" value={`${m.win_rate_pct.toFixed(1)}%`} valueClass={m.win_rate_pct >= 50 ? "text-emerald-400" : "text-amber-400"} />
          <MetricCard label="Trades" value={`${m.win_count}W / ${m.loss_count}L`} />
          <MetricCard label="Avg Trade P&L" value={`$${parseFloat(m.avg_trade_pnl).toFixed(2)}`} />
          <MetricCard label="Max Drawdown" value={`${m.max_drawdown_pct.toFixed(2)}%`} valueClass="text-red-400" />
          <MetricCard label="Sharpe Ratio" value={m.sharpe_ratio.toFixed(3)} />
          <MetricCard label="Bars Tested" value={String(m.bars_tested)} />
        </div>
      </div>

      {/* Signal stats */}
      <div className="flex gap-4 text-xs text-gray-500">
        <span>Signals generated: <span className="text-gray-300">{m.signals_generated}</span></span>
        <span>Rejected by risk: <span className="text-amber-400">{m.signals_rejected}</span></span>
        <span>Executed: <span className="text-gray-300">{m.trade_count}</span></span>
      </div>

      {/* Trade log */}
      {result.trades.length > 0 && (
        <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">
            Trade Log ({result.trades.length})
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left pb-2 font-medium">Entry Date</th>
                  <th className="text-right pb-2 font-medium">Entry $</th>
                  <th className="text-right pb-2 font-medium">Exit $</th>
                  <th className="text-right pb-2 font-medium">Qty</th>
                  <th className="text-right pb-2 font-medium">P&L</th>
                  <th className="text-left pb-2 font-medium pl-3">Exit Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/50">
                {result.trades.map((t) => {
                  const pnl = t.pnl ? parseFloat(t.pnl) : null;
                  return (
                    <tr key={t.trade_id} className="hover:bg-gray-800/30">
                      <td className="py-1.5 text-gray-400">
                        {new Date(t.entry_time).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit" })}
                      </td>
                      <td className="py-1.5 text-right text-gray-300 tabular-nums">
                        ${parseFloat(t.entry_price).toFixed(2)}
                      </td>
                      <td className="py-1.5 text-right text-gray-300 tabular-nums">
                        {t.exit_price ? `$${parseFloat(t.exit_price).toFixed(2)}` : "—"}
                      </td>
                      <td className="py-1.5 text-right text-gray-400 tabular-nums">{t.quantity}</td>
                      <td className={`py-1.5 text-right tabular-nums font-medium ${pnl === null ? "text-gray-500" : pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                        {pnl === null ? "—" : `${pnl >= 0 ? "+" : ""}$${Math.abs(pnl).toFixed(2)}`}
                      </td>
                      <td className="py-1.5 pl-3 text-gray-500 capitalize">
                        {t.exit_reason?.toLowerCase().replace("_", " ") ?? "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const inputCls =
  "w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-600";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-xs font-medium text-gray-400">{label}</label>
      {children}
    </div>
  );
}

function MetricCard({
  label,
  value,
  valueClass = "text-gray-200",
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="bg-gray-800/50 rounded p-3 flex flex-col gap-1">
      <span className="text-xs text-gray-500">{label}</span>
      <span className={`text-sm font-semibold tabular-nums ${valueClass}`}>{value}</span>
    </div>
  );
}
