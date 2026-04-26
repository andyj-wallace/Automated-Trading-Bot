/**
 * AdvancedAnalyticsPanel — rolling Sharpe, drawdown chart, trade day-of-week heatmap.
 *
 * Fetches GET /api/v1/metrics/analytics on mount and when the range selector changes.
 * All charts are SVG-based (no extra dependencies).
 *
 * Layer 18.2 — Advanced analytics.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";

type RangeOption = "30d" | "90d" | "all";

interface DrawdownPoint {
  time: string;
  equity: string;
  drawdown_pct: number;
}

interface SharpePoint {
  time: string;
  sharpe: number;
}

interface HeatmapDay {
  day: number;
  day_name: string;
  trade_count: number;
  total_pnl: string;
  avg_pnl: string;
}

interface AnalyticsData {
  range: string;
  drawdown_series: DrawdownPoint[];
  rolling_sharpe_series: SharpePoint[];
  trade_heatmap: HeatmapDay[];
}

const RANGE_OPTIONS: RangeOption[] = ["30d", "90d", "all"];

export function AdvancedAnalyticsPanel() {
  const [range, setRange] = useState<RangeOption>("30d");

  const { data, isLoading } = useQuery({
    queryKey: ["metrics-analytics", range],
    queryFn: async () => {
      const res = await api.get<AnalyticsData>(`/metrics/analytics?range=${range}`);
      return res.data;
    },
    staleTime: 120_000,
  });

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-5">
      {/* Header + range selector */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Advanced Analytics
        </h2>
        <div className="flex gap-1">
          {RANGE_OPTIONS.map((r) => (
            <button
              key={r}
              onClick={() => setRange(r)}
              className={`px-2 py-0.5 rounded text-xs font-medium transition-colors ${
                range === r
                  ? "bg-indigo-600 text-white"
                  : "text-gray-500 hover:text-gray-300"
              }`}
            >
              {r}
            </button>
          ))}
        </div>
      </div>

      {isLoading && (
        <p className="text-xs text-gray-600 py-4 text-center">Loading…</p>
      )}

      {data && (
        <>
          {/* Drawdown chart */}
          <section>
            <p className="text-xs text-gray-500 mb-2 font-medium">
              Equity Drawdown
            </p>
            {data.drawdown_series.length > 1 ? (
              <DrawdownChart series={data.drawdown_series} />
            ) : (
              <EmptyState message="No portfolio snapshots yet" />
            )}
          </section>

          {/* Rolling Sharpe */}
          <section>
            <p className="text-xs text-gray-500 mb-2 font-medium">
              Rolling Sharpe Ratio (20-snapshot window, annualised)
            </p>
            {data.rolling_sharpe_series.length > 1 ? (
              <SharpeChart series={data.rolling_sharpe_series} />
            ) : (
              <EmptyState message="Needs 21+ portfolio snapshots to compute" />
            )}
          </section>

          {/* Day-of-week heatmap */}
          <section>
            <p className="text-xs text-gray-500 mb-2 font-medium">
              P&L by Day of Week
            </p>
            <HeatmapTable heatmap={data.trade_heatmap} />
          </section>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Drawdown chart
// ---------------------------------------------------------------------------

function DrawdownChart({ series }: { series: DrawdownPoint[] }) {
  const W = 500;
  const H = 80;
  const pad = 4;

  const values = series.map((p) => p.drawdown_pct);
  const minVal = Math.min(...values, -0.01); // always goes below 0
  const maxVal = 0; // drawdown is always ≤ 0

  function x(i: number) {
    return pad + (i / (series.length - 1)) * (W - pad * 2);
  }
  function y(val: number) {
    const range = maxVal - minVal || 1;
    return pad + ((maxVal - val) / range) * (H - pad * 2);
  }

  const points = series.map((p, i) => `${x(i)},${y(p.drawdown_pct)}`).join(" ");
  const lastVal = values[values.length - 1];
  const fillPath = `M ${x(0)},${y(0)} ${series.map((p, i) => `L ${x(i)},${y(p.drawdown_pct)}`).join(" ")} L ${x(series.length - 1)},${y(0)} Z`;

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" preserveAspectRatio="none">
        {/* Zero baseline */}
        <line
          x1={pad} y1={y(0)} x2={W - pad} y2={y(0)}
          stroke="#374151" strokeWidth="0.5"
        />
        {/* Fill area */}
        <path d={fillPath} fill="#ef4444" opacity="0.15" />
        {/* Line */}
        <polyline
          points={points}
          fill="none"
          stroke="#ef4444"
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {/* Current dot */}
        <circle
          cx={x(series.length - 1)}
          cy={y(lastVal)}
          r="3"
          fill="#ef4444"
        />
      </svg>
      <div className="flex justify-between text-xs text-gray-600 mt-1 px-1">
        <span>{new Date(series[0].time).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</span>
        <span className={lastVal < -5 ? "text-red-400" : lastVal < -2 ? "text-amber-400" : "text-gray-400"}>
          Current: {lastVal.toFixed(2)}%
        </span>
        <span>{new Date(series[series.length - 1].time).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Rolling Sharpe chart
// ---------------------------------------------------------------------------

function SharpeChart({ series }: { series: SharpePoint[] }) {
  const W = 500;
  const H = 80;
  const pad = 4;

  const values = series.map((p) => p.sharpe);
  const minVal = Math.min(...values, -0.5);
  const maxVal = Math.max(...values, 0.5);

  function x(i: number) {
    return pad + (i / (series.length - 1)) * (W - pad * 2);
  }
  function y(val: number) {
    const range = maxVal - minVal || 1;
    return pad + ((maxVal - val) / range) * (H - pad * 2);
  }

  const points = series.map((p, i) => `${x(i)},${y(p.sharpe)}`).join(" ");
  const lastVal = values[values.length - 1];
  const zeroY = y(0);
  const lineColour = lastVal >= 1.0 ? "#10b981" : lastVal >= 0 ? "#f59e0b" : "#ef4444";

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" preserveAspectRatio="none">
        {/* Zero baseline */}
        <line
          x1={pad} y1={zeroY} x2={W - pad} y2={zeroY}
          stroke="#374151" strokeWidth="0.5" strokeDasharray="4,4"
        />
        {/* Sharpe = 1 reference */}
        {maxVal >= 1 && (
          <line
            x1={pad} y1={y(1)} x2={W - pad} y2={y(1)}
            stroke="#10b981" strokeWidth="0.5" strokeDasharray="4,4" opacity="0.4"
          />
        )}
        {/* Line */}
        <polyline
          points={points}
          fill="none"
          stroke={lineColour}
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <circle cx={x(series.length - 1)} cy={y(lastVal)} r="3" fill={lineColour} />
      </svg>
      <div className="flex justify-between text-xs text-gray-600 mt-1 px-1">
        <span>{new Date(series[0].time).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</span>
        <span className={lastVal >= 1 ? "text-emerald-400" : lastVal >= 0 ? "text-amber-400" : "text-red-400"}>
          Current: {lastVal.toFixed(2)}
        </span>
        <span>{new Date(series[series.length - 1].time).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Day-of-week heatmap table
// ---------------------------------------------------------------------------

function HeatmapTable({ heatmap }: { heatmap: HeatmapDay[] }) {
  const maxAbsPnl = Math.max(
    ...heatmap.map((d) => Math.abs(parseFloat(d.total_pnl))),
    1,
  );

  return (
    <div className="grid grid-cols-7 gap-1">
      {heatmap.map((day) => {
        const pnl = parseFloat(day.total_pnl);
        const intensity = Math.min(Math.abs(pnl) / maxAbsPnl, 1);
        const bgClass =
          day.trade_count === 0
            ? "bg-gray-800"
            : pnl > 0
              ? `bg-emerald-900`
              : `bg-red-900`;
        const textClass = pnl > 0 ? "text-emerald-400" : pnl < 0 ? "text-red-400" : "text-gray-500";

        return (
          <div
            key={day.day}
            className={`${bgClass} rounded p-2 flex flex-col gap-1 text-center`}
            style={{ opacity: day.trade_count === 0 ? 0.4 : 0.4 + intensity * 0.6 }}
            title={`${day.day_name}: ${day.trade_count} trade${day.trade_count !== 1 ? "s" : ""}`}
          >
            <span className="text-xs text-gray-400 font-medium">
              {day.day_name.slice(0, 3)}
            </span>
            <span className={`text-xs font-medium tabular-nums ${textClass}`}>
              {day.trade_count === 0
                ? "—"
                : `${pnl >= 0 ? "+" : ""}$${Math.abs(pnl).toFixed(0)}`}
            </span>
            <span className="text-xs text-gray-600">{day.trade_count}t</span>
          </div>
        );
      })}
    </div>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="h-20 flex items-center justify-center">
      <p className="text-xs text-gray-600">{message}</p>
    </div>
  );
}
