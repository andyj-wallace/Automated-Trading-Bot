/**
 * BacktestingPage — single-strategy (15.4) and portfolio mode (21.3).
 *
 * Toggle between:
 *   Single: symbol + strategy + config → POST /api/v1/backtesting/run
 *   Portfolio: N (strategy, symbol) rows → POST /api/v1/backtesting/run-portfolio
 *
 * Both poll GET /api/v1/backtesting/:id at 2s until done/error.
 */

import { useState, useEffect, useRef } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface BacktestJob {
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

interface BacktestResult {
  symbol: string;
  strategy_type: string;
  account_balance: string;
  start_time: string;
  end_time: string;
  trades: BacktestTrade[];
  metrics: BacktestMetrics | null;
}

interface PortfolioBacktestResult {
  account_balance: string;
  start_time: string;
  end_time: string;
  per_strategy: BacktestResult[];
  combined_equity_curve: [string, string][];
  combined_metrics: BacktestMetrics;
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

// Portfolio slot row state
interface SlotRow {
  id: number;
  symbol: string;
  strategyType: string;
}

const STRATEGY_OPTIONS = [
  { value: "moving_average",     label: "Moving Average" },
  { value: "mean_reversion",     label: "Mean Reversion" },
  { value: "stock_trend",        label: "Stock Trend" },
  { value: "bull_bear",          label: "Bull/Bear Regime" },
  { value: "intra_week_reversion", label: "Intra-Week Reversion" },
];

const RANGE_OPTIONS = [
  { value: "1y", label: "1 Year" },
  { value: "2y", label: "2 Years" },
  { value: "5y", label: "5 Years" },
];

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function Backtesting() {
  const [mode, setMode] = useState<"single" | "portfolio">("single");

  // ---- Single-strategy state ----
  const [symbol, setSymbol] = useState("AAPL");
  const [fastPeriod, setFastPeriod] = useState(50);
  const [slowPeriod, setSlowPeriod] = useState(200);
  const [stopPct, setStopPct] = useState("0.03");
  const [balance, setBalance] = useState("100000");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitErrorCode, setSubmitErrorCode] = useState<string | null>(null);
  const [fetchConfirm, setFetchConfirm] = useState<string | null>(null);
  const [job, setJob] = useState<BacktestJob | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ---- Portfolio state ----
  const [slots, setSlots] = useState<SlotRow[]>([
    { id: 1, symbol: "AAPL", strategyType: "moving_average" },
    { id: 2, symbol: "MSFT", strategyType: "mean_reversion" },
  ]);
  const [nextSlotId, setNextSlotId] = useState(3);
  const [portBalance, setPortBalance] = useState("100000");
  const [portRange, setPortRange] = useState("5y");
  const [portSubmitting, setPortSubmitting] = useState(false);
  const [portError, setPortError] = useState<string | null>(null);
  const [portJob, setPortJob] = useState<BacktestJob | null>(null);
  const portPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Single-strategy polling
  useEffect(() => {
    if (!job || job.status === "done" || job.status === "error") {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      return;
    }
    pollRef.current = setInterval(async () => {
      const res = await api.get<BacktestJob>(`/backtesting/${job.job_id}`);
      if (res.data) setJob(res.data);
    }, 2000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.job_id, job?.status]);

  // Portfolio polling
  useEffect(() => {
    if (!portJob || portJob.status === "done" || portJob.status === "error") {
      if (portPollRef.current) { clearInterval(portPollRef.current); portPollRef.current = null; }
      return;
    }
    portPollRef.current = setInterval(async () => {
      const res = await api.get<BacktestJob>(`/backtesting/${portJob.job_id}`);
      if (res.data) setPortJob(res.data);
    }, 2000);
    return () => { if (portPollRef.current) clearInterval(portPollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [portJob?.job_id, portJob?.status]);

  async function handleSingleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitError(null);
    setSubmitErrorCode(null);
    setFetchConfirm(null);
    setJob(null);
    setSubmitting(true);
    try {
      const res = await api.post<BacktestJob>("/backtesting/run", {
        symbol: symbol.toUpperCase(),
        strategy_type: "moving_average",
        strategy_config: { fast_period: fastPeriod, slow_period: slowPeriod, stop_loss_pct: stopPct },
        account_balance: balance,
      });
      if (res.error) { setSubmitError(res.error.message); setSubmitErrorCode(res.error.code); return; }
      if (res.data) setJob(res.data);
    } catch (err: unknown) {
      setSubmitError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setSubmitting(false);
    }
  }

  async function handlePortfolioSubmit(e: React.FormEvent) {
    e.preventDefault();
    setPortError(null);
    setPortJob(null);
    setPortSubmitting(true);
    try {
      const res = await api.post<BacktestJob>("/backtesting/run-portfolio", {
        slots: slots.map((s) => ({
          symbol: s.symbol.toUpperCase(),
          strategy_type: s.strategyType,
          config: {},
        })),
        account_balance: portBalance,
        range: portRange,
      });
      if (res.error) { setPortError(res.error.message); return; }
      if (res.data) setPortJob(res.data);
    } catch (err: unknown) {
      setPortError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setPortSubmitting(false);
    }
  }

  function addSlot() {
    setSlots((prev) => [...prev, { id: nextSlotId, symbol: "", strategyType: "moving_average" }]);
    setNextSlotId((n) => n + 1);
  }

  function removeSlot(id: number) {
    setSlots((prev) => prev.filter((s) => s.id !== id));
  }

  function updateSlot(id: number, field: keyof SlotRow, value: string) {
    setSlots((prev) => prev.map((s) => s.id === id ? { ...s, [field]: value } : s));
  }

  const isRunning = (j: BacktestJob | null) =>
    j?.status === "pending" || j?.status === "running";

  return (
    <div className="p-8 max-w-5xl flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold text-white mb-1">Backtesting</h1>
        <p className="text-gray-500 text-sm">
          Replay strategies against historical OHLCV data
        </p>
      </div>

      {/* Mode toggle */}
      <div className="flex gap-1 p-1 bg-gray-800/60 rounded-lg w-fit">
        {(["single", "portfolio"] as const).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={`px-4 py-1.5 text-sm font-medium rounded transition-colors capitalize ${
              mode === m
                ? "bg-blue-600 text-white"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            {m === "single" ? "Single Strategy" : "Portfolio"}
          </button>
        ))}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Single-strategy form                                                */}
      {/* ------------------------------------------------------------------ */}
      {mode === "single" && (
        <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
          <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-4">
            Configure Backtest
          </h2>
          <form onSubmit={handleSingleSubmit} className="flex flex-col gap-4">
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
                  disabled={submitting || isRunning(job)}
                  className="w-full px-4 py-2 text-sm font-medium bg-blue-600 hover:bg-blue-700 disabled:bg-blue-900 disabled:text-blue-400 text-white rounded transition-colors"
                >
                  {submitting ? "Submitting…" : "Run Backtest"}
                </button>
              </div>
            </div>

            {submitError && submitErrorCode !== "NO_HISTORICAL_DATA" && (
              <p className="text-xs text-red-400">{submitError}</p>
            )}
            {submitError && submitErrorCode === "NO_HISTORICAL_DATA" && (
              <NoHistoricalDataPanel
                symbol={symbol}
                errorMessage={submitError}
                onSuccess={() => { setSubmitError(null); setFetchConfirm("History fetched — you can now run the backtest"); }}
              />
            )}
            {fetchConfirm && <p className="text-xs text-emerald-400">{fetchConfirm}</p>}
          </form>
        </div>
      )}

      {mode === "single" && job && (
        <SingleJobStatus job={job} />
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Portfolio form                                                       */}
      {/* ------------------------------------------------------------------ */}
      {mode === "portfolio" && (
        <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
          <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-4">
            Configure Portfolio Backtest
          </h2>
          <form onSubmit={handlePortfolioSubmit} className="flex flex-col gap-4">
            {/* Slot rows */}
            <div className="flex flex-col gap-2">
              <div className="grid grid-cols-12 gap-2 text-xs font-medium text-gray-500 uppercase tracking-wide px-1">
                <span className="col-span-4">Symbol</span>
                <span className="col-span-7">Strategy</span>
              </div>
              {slots.map((slot) => (
                <div key={slot.id} className="grid grid-cols-12 gap-2 items-center">
                  <input
                    value={slot.symbol}
                    onChange={(e) => updateSlot(slot.id, "symbol", e.target.value.toUpperCase())}
                    className={`col-span-4 ${inputCls}`}
                    placeholder="AAPL"
                    maxLength={10}
                    required
                  />
                  <select
                    value={slot.strategyType}
                    onChange={(e) => updateSlot(slot.id, "strategyType", e.target.value)}
                    className={`col-span-7 ${inputCls}`}
                  >
                    {STRATEGY_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => removeSlot(slot.id)}
                    disabled={slots.length === 1}
                    className="col-span-1 text-gray-600 hover:text-red-400 disabled:opacity-30 text-lg leading-none transition-colors"
                    title="Remove row"
                  >
                    ×
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={addSlot}
                disabled={slots.length >= 10}
                className="self-start mt-1 px-3 py-1 text-xs font-medium text-blue-400 hover:text-blue-300 disabled:text-gray-600 border border-blue-800 hover:border-blue-600 disabled:border-gray-700 rounded transition-colors"
              >
                + Add slot
              </button>
            </div>

            {/* Global settings */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 pt-2 border-t border-gray-800">
              <Field label="Account Balance ($)">
                <input
                  type="number"
                  value={portBalance}
                  onChange={(e) => setPortBalance(e.target.value)}
                  className={inputCls}
                  min={1000}
                  required
                />
              </Field>
              <Field label="Historical Range">
                <select
                  value={portRange}
                  onChange={(e) => setPortRange(e.target.value)}
                  className={inputCls}
                >
                  {RANGE_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                </select>
              </Field>
              <div className="flex items-end md:col-span-2">
                <button
                  type="submit"
                  disabled={portSubmitting || isRunning(portJob) || slots.some((s) => !s.symbol)}
                  className="w-full px-4 py-2 text-sm font-medium bg-blue-600 hover:bg-blue-700 disabled:bg-blue-900 disabled:text-blue-400 text-white rounded transition-colors"
                >
                  {portSubmitting ? "Submitting…" : `Run Portfolio Backtest (${slots.length} slots)`}
                </button>
              </div>
            </div>

            {portError && (
              <p className="text-xs text-red-400">{portError}</p>
            )}
          </form>
        </div>
      )}

      {mode === "portfolio" && portJob && (
        <PortfolioJobStatus job={portJob} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Single-strategy job status
// ---------------------------------------------------------------------------

function SingleJobStatus({ job }: { job: BacktestJob }) {
  const statusColour = { pending: "text-gray-400", running: "text-blue-400", done: "text-emerald-400", error: "text-red-400" }[job.status];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        {(job.status === "pending" || job.status === "running") && (
          <span className="inline-block w-3 h-3 rounded-full bg-blue-500 animate-pulse" />
        )}
        <span className={`text-sm font-medium ${statusColour} capitalize`}>{job.status}</span>
        {job.symbol && (
          <span className="text-xs text-gray-600">{job.symbol} · {job.strategy_type}</span>
        )}
      </div>
      {job.status === "error" && (
        <div className="rounded border border-red-800 bg-red-900/30 px-4 py-3 text-xs text-red-400">
          {job.error}
        </div>
      )}
      {job.status === "done" && job.result && (
        <BacktestResults result={job.result as BacktestResult} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Portfolio job status
// ---------------------------------------------------------------------------

function PortfolioJobStatus({ job }: { job: BacktestJob }) {
  const statusColour = { pending: "text-gray-400", running: "text-blue-400", done: "text-emerald-400", error: "text-red-400" }[job.status];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        {(job.status === "pending" || job.status === "running") && (
          <span className="inline-block w-3 h-3 rounded-full bg-blue-500 animate-pulse" />
        )}
        <span className={`text-sm font-medium ${statusColour} capitalize`}>{job.status}</span>
        {job.slot_count != null && (
          <span className="text-xs text-gray-600">{job.slot_count} strategy/symbol pairs</span>
        )}
      </div>
      {job.status === "error" && (
        <div className="rounded border border-red-800 bg-red-900/30 px-4 py-3 text-xs text-red-400">
          {job.error}
        </div>
      )}
      {job.status === "done" && job.result && (
        <PortfolioResults result={job.result as PortfolioBacktestResult} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Portfolio results
// ---------------------------------------------------------------------------

function PortfolioResults({ result }: { result: PortfolioBacktestResult }) {
  const m = result.combined_metrics;
  const totalReturn = parseFloat(m.total_return);

  return (
    <div className="flex flex-col gap-4">
      {/* Combined KPIs */}
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-4">
          Combined Portfolio Results
          <span className="ml-2 font-normal normal-case text-gray-600">
            {new Date(result.start_time).toLocaleDateString()} → {new Date(result.end_time).toLocaleDateString()}
          </span>
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MetricCard label="Total Return" value={`${totalReturn >= 0 ? "+" : ""}$${Math.abs(totalReturn).toLocaleString("en-US", { minimumFractionDigits: 2 })}`} valueClass={totalReturn >= 0 ? "text-emerald-400" : "text-red-400"} />
          <MetricCard label="Return %" value={`${m.total_return_pct >= 0 ? "+" : ""}${m.total_return_pct.toFixed(2)}%`} valueClass={m.total_return_pct >= 0 ? "text-emerald-400" : "text-red-400"} />
          <MetricCard label="Win Rate" value={`${m.win_rate_pct.toFixed(1)}%`} valueClass={m.win_rate_pct >= 50 ? "text-emerald-400" : "text-amber-400"} />
          <MetricCard label="Trades" value={`${m.win_count}W / ${m.loss_count}L`} />
          <MetricCard label="Max Drawdown" value={`${m.max_drawdown_pct.toFixed(2)}%`} valueClass="text-red-400" />
          <MetricCard label="Sharpe Ratio" value={m.sharpe_ratio.toFixed(3)} />
          <MetricCard label="Signals" value={`${m.signals_generated} generated`} />
          <MetricCard label="Rejected" value={`${m.signals_rejected} by risk`} valueClass={m.signals_rejected > 0 ? "text-amber-400" : "text-gray-200"} />
        </div>
      </div>

      {/* Equity curve */}
      {result.combined_equity_curve.length > 1 && (
        <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">
            Combined Equity Curve
          </h3>
          <EquityCurve curve={result.combined_equity_curve} />
        </div>
      )}

      {/* Per-strategy breakdown */}
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">
          Per-Strategy Breakdown
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-gray-500 border-b border-gray-800">
                <th className="text-left pb-2 font-medium">Symbol</th>
                <th className="text-left pb-2 font-medium">Strategy</th>
                <th className="text-right pb-2 font-medium">Trades</th>
                <th className="text-right pb-2 font-medium">Win Rate</th>
                <th className="text-right pb-2 font-medium">Total P&L</th>
                <th className="text-right pb-2 font-medium">Max DD</th>
                <th className="text-right pb-2 font-medium">Sharpe</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/50">
              {result.per_strategy.map((ps, idx) => {
                const pm = ps.metrics;
                const pnl = pm ? parseFloat(pm.total_return) : 0;
                return (
                  <tr key={idx} className="hover:bg-gray-800/30">
                    <td className="py-2 font-medium text-gray-200">{ps.symbol}</td>
                    <td className="py-2 text-gray-400 capitalize">
                      {ps.strategy_type.replace(/_/g, " ")}
                    </td>
                    <td className="py-2 text-right text-gray-300 tabular-nums">
                      {pm ? `${pm.win_count}W / ${pm.loss_count}L` : "—"}
                    </td>
                    <td className={`py-2 text-right tabular-nums ${pm && pm.win_rate_pct >= 50 ? "text-emerald-400" : "text-amber-400"}`}>
                      {pm ? `${pm.win_rate_pct.toFixed(1)}%` : "—"}
                    </td>
                    <td className={`py-2 text-right tabular-nums font-medium ${pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {pm ? `${pnl >= 0 ? "+" : ""}$${Math.abs(pnl).toLocaleString("en-US", { minimumFractionDigits: 2 })}` : "—"}
                    </td>
                    <td className="py-2 text-right text-gray-400 tabular-nums">
                      {pm ? `${pm.max_drawdown_pct.toFixed(2)}%` : "—"}
                    </td>
                    <td className="py-2 text-right text-gray-400 tabular-nums">
                      {pm ? pm.sharpe_ratio.toFixed(3) : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Equity curve SVG
// ---------------------------------------------------------------------------

function EquityCurve({ curve }: { curve: [string, string][] }) {
  const W = 600;
  const H = 80;
  const PAD = 4;

  const values = curve.map(([, v]) => parseFloat(v));
  const minV = Math.min(0, ...values);
  const maxV = Math.max(0, ...values);
  const range = maxV - minV || 1;

  const points = values.map((v, i) => {
    const x = PAD + ((i / (values.length - 1)) * (W - PAD * 2));
    const y = H - PAD - ((v - minV) / range) * (H - PAD * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  // Zero-line Y position
  const zeroY = H - PAD - ((0 - minV) / range) * (H - PAD * 2);
  const finalPositive = values[values.length - 1] >= 0;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-20" preserveAspectRatio="none">
      {/* Zero line */}
      <line
        x1={PAD} y1={zeroY.toFixed(1)}
        x2={W - PAD} y2={zeroY.toFixed(1)}
        stroke="#374151" strokeWidth="1" strokeDasharray="3 3"
      />
      {/* Equity line */}
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke={finalPositive ? "#34d399" : "#f87171"}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Single-strategy results
// ---------------------------------------------------------------------------

function BacktestResults({ result }: { result: BacktestResult }) {
  const m = result.metrics;
  if (!m) return null;
  const totalReturn = parseFloat(m.total_return);

  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-4">
          Results — {result.symbol}{" "}
          <span className="font-normal normal-case text-gray-600">
            ({new Date(result.start_time).toLocaleDateString()} → {new Date(result.end_time).toLocaleDateString()})
          </span>
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

      <div className="flex gap-4 text-xs text-gray-500">
        <span>Signals generated: <span className="text-gray-300">{m.signals_generated}</span></span>
        <span>Rejected by risk: <span className="text-amber-400">{m.signals_rejected}</span></span>
        <span>Executed: <span className="text-gray-300">{m.trade_count}</span></span>
      </div>

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
                      <td className="py-1.5 text-right text-gray-300 tabular-nums">${parseFloat(t.entry_price).toFixed(2)}</td>
                      <td className="py-1.5 text-right text-gray-300 tabular-nums">{t.exit_price ? `$${parseFloat(t.exit_price).toFixed(2)}` : "—"}</td>
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
// No historical data inline panel
// ---------------------------------------------------------------------------

function NoHistoricalDataPanel({ symbol, errorMessage, onSuccess }: {
  symbol: string;
  errorMessage: string;
  onSuccess: () => void;
}) {
  const [fetchError, setFetchError] = useState<string | null>(null);

  const fetchMutation = useMutation({
    mutationFn: async () => {
      const res = await api.post<{ ticker: string; bars_stored: number }>(
        `/symbols/${symbol.toUpperCase()}/fetch-history`,
        {}
      );
      if (res.error) throw new Error(res.error.message);
      return res.data;
    },
    onSuccess: () => { setFetchError(null); onSuccess(); },
    onError: (err: Error) => { setFetchError(err.message); },
  });

  return (
    <div className="rounded border border-red-800 bg-red-900/20 px-4 py-3 flex flex-col gap-2">
      <p className="text-xs text-red-400">{errorMessage}</p>
      <button
        type="button"
        onClick={() => fetchMutation.mutate()}
        disabled={fetchMutation.isPending}
        className="self-start px-3 py-1.5 text-xs font-medium bg-blue-600 hover:bg-blue-700 disabled:bg-blue-900 disabled:text-blue-400 text-white rounded transition-colors"
      >
        {fetchMutation.isPending ? "Fetching…" : `Fetch History for ${symbol.toUpperCase()}`}
      </button>
      {fetchError && <p className="text-xs text-red-400">{fetchError}</p>}
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

function MetricCard({ label, value, valueClass = "text-gray-200" }: {
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
