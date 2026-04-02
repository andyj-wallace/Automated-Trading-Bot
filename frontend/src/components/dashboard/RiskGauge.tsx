/**
 * RiskGauge — shows aggregate portfolio risk as a % of account balance.
 *
 * Initial data: GET /api/v1/portfolio/risk
 * Live updates: WebSocket "risk_alert" events trigger a refetch.
 *
 * Color states mirror RiskMonitor alert levels:
 *   NONE     → emerald (green)
 *   WARNING  → amber
 *   CRITICAL → red
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { api } from "../../api/client";
import type { DashboardEvent, RiskStatus } from "../../types/api";

interface Props {
  lastEvent: DashboardEvent | null;
}

export function RiskGauge({ lastEvent }: Props) {
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["portfolio-risk"],
    queryFn: async () => {
      const res = await api.get<RiskStatus>("/portfolio/risk");
      return res.data;
    },
    refetchInterval: 30_000,
  });

  useEffect(() => {
    if (lastEvent?.event === "risk_alert" || lastEvent?.event === "trade_executed" || lastEvent?.event === "trade_closed") {
      queryClient.invalidateQueries({ queryKey: ["portfolio-risk"] });
    }
  }, [lastEvent, queryClient]);

  const alertLevel = data?.alert_level ?? "NONE";
  const riskPct = data ? parseFloat(data.aggregate_risk_pct) * 100 : 0;
  const maxPct = data ? parseFloat(data.critical_threshold_pct) * 100 * (1 / 0.9) : 5; // extrapolate 100% bar to max cap
  const fillPct = Math.min((riskPct / maxPct) * 100, 100);

  const colours = {
    NONE: {
      bar: "bg-emerald-500",
      text: "text-emerald-400",
      badge: "bg-emerald-900/50 text-emerald-400",
      label: "Normal",
    },
    WARNING: {
      bar: "bg-amber-500",
      text: "text-amber-400",
      badge: "bg-amber-900/50 text-amber-400",
      label: "Warning",
    },
    CRITICAL: {
      bar: "bg-red-500",
      text: "text-red-400",
      badge: "bg-red-900/50 text-red-400",
      label: "Critical",
    },
  } as const;

  const c = colours[alertLevel] ?? colours.NONE;

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Portfolio Risk
        </h2>
        {data && (
          <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${c.badge}`}>
            {c.label}
          </span>
        )}
      </div>

      {isLoading && (
        <p className="text-xs text-gray-600">Loading…</p>
      )}

      {data && (
        <>
          {/* Big risk % number */}
          <div className="flex items-baseline gap-2">
            <span className={`text-3xl font-bold tabular-nums ${c.text}`}>
              {riskPct.toFixed(2)}%
            </span>
            <span className="text-xs text-gray-500">of account at risk</span>
          </div>

          {/* Bar */}
          <div className="w-full h-3 bg-gray-800 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-500 ${c.bar}`}
              style={{ width: `${fillPct}%` }}
            />
          </div>

          {/* Threshold markers + stats */}
          <div className="grid grid-cols-3 gap-2 text-xs">
            <Stat
              label="Aggregate Risk"
              value={`$${parseFloat(data.aggregate_risk_amount).toLocaleString("en-US", { minimumFractionDigits: 2 })}`}
            />
            <Stat
              label="Open Trades"
              value={String(data.open_trade_count)}
            />
            <Stat
              label="Account Balance"
              value={`$${parseFloat(data.account_balance).toLocaleString("en-US", { maximumFractionDigits: 0 })}`}
            />
          </div>

          {/* Warning / critical thresholds */}
          <div className="flex gap-4 text-xs text-gray-600">
            <span>
              ⚠ Warning ≥ {(parseFloat(data.warning_threshold_pct) * 100).toFixed(1)}%
            </span>
            <span>
              🔴 Critical ≥ {(parseFloat(data.critical_threshold_pct) * 100).toFixed(1)}%
            </span>
          </div>
        </>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-200 font-medium tabular-nums">{value}</span>
    </div>
  );
}
