/**
 * StrategyPerformanceChart — win rate, cumulative P&L, trade count.
 *
 * Fetches closed trades for a given strategy_id, then computes metrics
 * client-side. A date-range selector (7d / 30d / 90d / All) filters the data.
 *
 * Uses lightweight-charts for the cumulative P&L sparkline.
 * Falls back to a simple bar representation if no trades exist.
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { Trade } from "../../types/api";

type Range = "7d" | "30d" | "90d" | "all";

const RANGES: { label: string; value: Range }[] = [
  { label: "7d", value: "7d" },
  { label: "30d", value: "30d" },
  { label: "90d", value: "90d" },
  { label: "All", value: "all" },
];

function cutoffDate(range: Range): Date | null {
  if (range === "all") return null;
  const days = range === "7d" ? 7 : range === "30d" ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d;
}

interface Props {
  strategyId: string;
  strategyName: string;
}

export function StrategyPerformanceChart({ strategyId, strategyName }: Props) {
  const [range, setRange] = useState<Range>("30d");

  const { data: allTrades = [], isLoading } = useQuery({
    queryKey: ["trades", "CLOSED", strategyId],
    queryFn: async () => {
      const res = await api.get<Trade[]>(
        `/trades?status=CLOSED&strategy_id=${strategyId}&limit=500`
      );
      return res.data ?? [];
    },
    staleTime: 60_000,
  });

  const cutoff = cutoffDate(range);
  const trades = cutoff
    ? allTrades.filter((t) => new Date(t.closed_at ?? t.created_at) >= cutoff)
    : allTrades;

  const metrics = computeMetrics(trades);

  return (
    <div className="flex flex-col gap-4">
      {/* Range selector */}
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

      {isLoading && (
        <p className="text-xs text-gray-600">Loading trade history…</p>
      )}

      {!isLoading && trades.length === 0 && (
        <div className="rounded border border-dashed border-gray-700 p-8 text-center">
          <p className="text-xs text-gray-600">No closed trades in this period</p>
        </div>
      )}

      {!isLoading && trades.length > 0 && (
        <>
          {/* KPI strip */}
          <div className="grid grid-cols-4 gap-3">
            <KpiCard
              label="Total P&L"
              value={formatPnl(metrics.totalPnl)}
              valueClass={metrics.totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}
            />
            <KpiCard
              label="Win Rate"
              value={`${metrics.winRate.toFixed(1)}%`}
              valueClass={metrics.winRate >= 50 ? "text-emerald-400" : "text-amber-400"}
            />
            <KpiCard label="Trades" value={String(metrics.count)} />
            <KpiCard label="Avg P&L" value={formatPnl(metrics.avgPnl)} />
          </div>

          {/* Cumulative P&L sparkline */}
          <PnlSparkline trades={trades} strategyName={strategyName} />
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Metrics computation
// ---------------------------------------------------------------------------

interface Metrics {
  count: number;
  wins: number;
  losses: number;
  winRate: number;
  totalPnl: number;
  avgPnl: number;
}

function computeMetrics(trades: Trade[]): Metrics {
  if (trades.length === 0) {
    return { count: 0, wins: 0, losses: 0, winRate: 0, totalPnl: 0, avgPnl: 0 };
  }

  const pnls = trades.map((t) => (t.pnl != null ? parseFloat(t.pnl) : 0));
  const wins = pnls.filter((p) => p > 0).length;
  const totalPnl = pnls.reduce((a, b) => a + b, 0);

  return {
    count: trades.length,
    wins,
    losses: trades.length - wins,
    winRate: (wins / trades.length) * 100,
    totalPnl,
    avgPnl: totalPnl / trades.length,
  };
}

function formatPnl(value: number): string {
  const prefix = value >= 0 ? "+$" : "-$";
  return `${prefix}${Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// ---------------------------------------------------------------------------
// Cumulative P&L sparkline using lightweight-charts
// ---------------------------------------------------------------------------

function PnlSparkline({
  trades,
  strategyName,
}: {
  trades: Trade[];
  strategyName: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<unknown>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    // Sort trades by close date
    const sorted = [...trades]
      .filter((t) => t.pnl != null)
      .sort(
        (a, b) =>
          new Date(a.closed_at ?? a.created_at).getTime() -
          new Date(b.closed_at ?? b.created_at).getTime()
      );

    if (sorted.length === 0) return;

    // Compute cumulative P&L series
    let cumulative = 0;
    const seriesData = sorted.map((t) => {
      cumulative += parseFloat(t.pnl!);
      return {
        time: Math.floor(
          new Date(t.closed_at ?? t.created_at).getTime() / 1000
        ) as unknown as string,
        value: cumulative,
      };
    });

    // Dynamically import lightweight-charts to keep initial bundle smaller
    import("lightweight-charts").then(({ createChart, ColorType }) => {
      if (!containerRef.current) return;

      // Destroy previous chart instance if it exists
      if (chartRef.current) {
        (chartRef.current as { remove: () => void }).remove();
        chartRef.current = null;
      }

      const chart = createChart(containerRef.current, {
        width: containerRef.current.clientWidth,
        height: 160,
        layout: {
          background: { type: ColorType.Solid, color: "transparent" },
          textColor: "#6b7280",
        },
        grid: {
          vertLines: { color: "#1f2937" },
          horzLines: { color: "#1f2937" },
        },
        rightPriceScale: { borderColor: "#374151" },
        timeScale: { borderColor: "#374151", timeVisible: true },
      });

      chartRef.current = chart;

      const lineSeries = chart.addLineSeries({
        color: cumulative >= 0 ? "#10b981" : "#ef4444",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
      });

      lineSeries.setData(seriesData);
      chart.timeScale().fitContent();
    });

    return () => {
      if (chartRef.current) {
        (chartRef.current as { remove: () => void }).remove();
        chartRef.current = null;
      }
    };
  }, [trades, strategyName]);

  return (
    <div>
      <p className="text-xs text-gray-500 mb-1">Cumulative P&L</p>
      <div ref={containerRef} className="w-full" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// KPI card
// ---------------------------------------------------------------------------

function KpiCard({
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
