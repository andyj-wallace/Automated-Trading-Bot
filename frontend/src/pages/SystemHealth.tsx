/**
 * System Health page — Layer 16.3.
 *
 * Combines:
 *   - SystemHealthPanel (broker / DB / Redis status, Layer 10.1)
 *   - SystemMetricsPanel (API/DB latency, cache ping, updates every 30s)
 *   - PerformanceDashboard (trade KPIs with date range selector)
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { SystemHealthPanel } from "../components/dashboard/SystemHealthPanel";
import { api } from "../api/client";

// ---------------------------------------------------------------------------
// Performance types
// ---------------------------------------------------------------------------

interface PerfMetrics {
  range: string;
  trade_count: number;
  win_count: number;
  loss_count: number;
  win_rate_pct: number;
  total_pnl: string;
  avg_pnl: string;
  avg_duration_hours: number;
  largest_winner: string;
  largest_loser: string;
}

type Range = "7d" | "30d" | "90d" | "all";

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function SystemHealth() {
  return (
    <div className="p-8 max-w-4xl flex flex-col gap-4">
      <div>
        <h1 className="text-2xl font-bold text-white mb-1">System Health</h1>
        <p className="text-gray-500 text-sm">
          Broker · database · Redis · performance metrics
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <SystemHealthPanel />
        <SystemMetricsPanel />
      </div>

      <PerformanceDashboard />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 16.3 SystemMetricsPanel — latency probes every 30s
// ---------------------------------------------------------------------------

interface LatencyResult {
  ok: boolean;
  latency_ms: number | null;
  label: string;
}

function SystemMetricsPanel() {
  const { data, dataUpdatedAt } = useQuery({
    queryKey: ["system-metrics"],
    queryFn: async (): Promise<LatencyResult[]> => {
      const results: LatencyResult[] = [];

      // API round-trip (ping the health endpoint)
      const t0 = performance.now();
      try {
        await api.get("/system/health");
        results.push({ label: "API round-trip", ok: true, latency_ms: Math.round(performance.now() - t0) });
      } catch {
        results.push({ label: "API round-trip", ok: false, latency_ms: null });
      }

      // DB probe via portfolio/risk (hits the DB)
      const t1 = performance.now();
      try {
        await api.get("/portfolio/risk");
        results.push({ label: "DB query", ok: true, latency_ms: Math.round(performance.now() - t1) });
      } catch {
        results.push({ label: "DB query", ok: false, latency_ms: null });
      }

      // Symbols (small Redis-friendly endpoint)
      const t2 = performance.now();
      try {
        await api.get("/symbols?active_only=true");
        results.push({ label: "Cache / symbols", ok: true, latency_ms: Math.round(performance.now() - t2) });
      } catch {
        results.push({ label: "Cache / symbols", ok: false, latency_ms: null });
      }

      return results;
    },
    refetchInterval: 30_000,
  });

  const lastChecked = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : null;

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Response Latency
        </h2>
        {lastChecked && (
          <span className="text-xs text-gray-600">Updated {lastChecked}</span>
        )}
      </div>

      {!data && <p className="text-xs text-gray-600">Measuring…</p>}

      {data && (
        <div className="flex flex-col gap-3">
          {data.map((r) => (
            <div key={r.label} className="flex items-center justify-between">
              <span className="text-xs text-gray-400">{r.label}</span>
              <div className="flex items-center gap-2">
                {r.latency_ms !== null && (
                  <LatencyBar ms={r.latency_ms} />
                )}
                <span className={`text-xs tabular-nums font-medium w-16 text-right ${r.ok ? latencyColour(r.latency_ms) : "text-red-400"}`}>
                  {r.ok && r.latency_ms !== null ? `${r.latency_ms} ms` : "error"}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function latencyColour(ms: number | null): string {
  if (ms === null) return "text-gray-500";
  if (ms < 100) return "text-emerald-400";
  if (ms < 300) return "text-amber-400";
  return "text-red-400";
}

function LatencyBar({ ms }: { ms: number }) {
  const pct = Math.min((ms / 500) * 100, 100);
  const colour = ms < 100 ? "bg-emerald-500" : ms < 300 ? "bg-amber-500" : "bg-red-500";
  return (
    <div className="w-24 h-1.5 bg-gray-800 rounded-full overflow-hidden">
      <div className={`h-full rounded-full ${colour}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 16.2 PerformanceDashboard — KPI panel with date range selector
// ---------------------------------------------------------------------------

const RANGES: { label: string; value: Range }[] = [
  { label: "7d", value: "7d" },
  { label: "30d", value: "30d" },
  { label: "90d", value: "90d" },
  { label: "All", value: "all" },
];

function PerformanceDashboard() {
  const [range, setRange] = useState<Range>("30d");

  const { data, isLoading } = useQuery({
    queryKey: ["perf-metrics", range],
    queryFn: async () => {
      const res = await api.get<PerfMetrics>(`/metrics/performance?range=${range}`);
      return res.data;
    },
    staleTime: 60_000,
  });

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Trade Performance
        </h2>
        <div className="flex items-center gap-1">
          {RANGES.map((r) => (
            <button
              key={r.value}
              onClick={() => setRange(r.value)}
              className={`px-3 py-1 text-xs rounded transition-colors ${
                range === r.value
                  ? "bg-blue-600 text-white"
                  : "text-gray-500 hover:text-gray-300"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {isLoading && <p className="text-xs text-gray-600">Loading…</p>}

      {data && data.trade_count === 0 && (
        <p className="text-xs text-gray-600 py-4 text-center">
          No closed trades in this period
        </p>
      )}

      {data && data.trade_count > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <KpiCard
            label="Total P&L"
            value={formatPnl(parseFloat(data.total_pnl))}
            valueClass={parseFloat(data.total_pnl) >= 0 ? "text-emerald-400" : "text-red-400"}
          />
          <KpiCard
            label="Win Rate"
            value={`${data.win_rate_pct.toFixed(1)}%`}
            valueClass={data.win_rate_pct >= 50 ? "text-emerald-400" : "text-amber-400"}
            sub={`${data.win_count}W / ${data.loss_count}L`}
          />
          <KpiCard
            label="Trades"
            value={String(data.trade_count)}
            sub={`Avg ${data.avg_duration_hours.toFixed(1)}h`}
          />
          <KpiCard
            label="Avg P&L"
            value={formatPnl(parseFloat(data.avg_pnl))}
          />
          <KpiCard
            label="Largest Winner"
            value={formatPnl(parseFloat(data.largest_winner))}
            valueClass="text-emerald-400"
          />
          <KpiCard
            label="Largest Loser"
            value={formatPnl(parseFloat(data.largest_loser))}
            valueClass="text-red-400"
          />
        </div>
      )}
    </div>
  );
}

function formatPnl(value: number): string {
  const prefix = value >= 0 ? "+$" : "-$";
  return `${prefix}${Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function KpiCard({
  label,
  value,
  valueClass = "text-gray-200",
  sub,
}: {
  label: string;
  value: string;
  valueClass?: string;
  sub?: string;
}) {
  return (
    <div className="bg-gray-800/50 rounded p-3 flex flex-col gap-0.5">
      <span className="text-xs text-gray-500">{label}</span>
      <span className={`text-sm font-semibold tabular-nums ${valueClass}`}>{value}</span>
      {sub && <span className="text-xs text-gray-600">{sub}</span>}
    </div>
  );
}
