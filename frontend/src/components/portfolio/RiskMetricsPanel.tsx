/**
 * RiskMetricsPanel — historical aggregate risk utilization for today.
 *
 * Polls GET /api/v1/portfolio/risk on mount and every 30s.
 * Also reacts to WebSocket "risk_alert" events and shows a dismissible banner.
 *
 * Displays a sparkline of the risk% observed over the current session
 * (in-memory; resets on page reload — no server-side history required).
 *
 * Layer 14.2 — depends on 14.1 (risk_alert WS events) and 9.2 (API client).
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { DashboardEvent, RiskStatus } from "../../types/api";

interface Props {
  lastEvent: DashboardEvent | null;
}

interface HistorySample {
  time: number; // epoch ms
  pct: number;  // 0–100
}

export function RiskMetricsPanel({ lastEvent }: Props) {
  const [history, setHistory] = useState<HistorySample[]>([]);
  const [alert, setAlert] = useState<{ level: string; message: string } | null>(null);

  const { data, refetch } = useQuery({
    queryKey: ["portfolio-risk"],
    queryFn: async () => {
      const res = await api.get<RiskStatus>("/portfolio/risk");
      return res.data;
    },
    refetchInterval: 30_000,
  });

  // Record samples as data arrives
  useEffect(() => {
    if (!data) return;
    const pct = parseFloat(data.aggregate_risk_pct) * 100;
    setHistory((prev) => {
      const next = [...prev, { time: Date.now(), pct }];
      return next.slice(-60); // keep last 60 samples (~30 minutes at 30s interval)
    });
  }, [data]);

  // React to WebSocket risk_alert events
  useEffect(() => {
    if (lastEvent?.event !== "risk_alert") return;
    const p = lastEvent.payload as {
      alert_level?: string;
      aggregate_risk_pct?: string;
    };
    if (!p.alert_level || p.alert_level === "NONE") return;

    const pct = p.aggregate_risk_pct
      ? (parseFloat(p.aggregate_risk_pct) * 100).toFixed(2)
      : "?";

    setAlert({
      level: p.alert_level,
      message: `Portfolio risk at ${pct}% — ${p.alert_level} threshold crossed`,
    });
    refetch();
  }, [lastEvent, refetch]);

  const alertColours = {
    WARNING: "bg-amber-900/50 border-amber-700 text-amber-300",
    CRITICAL: "bg-red-900/50 border-red-700 text-red-300",
  } as const;

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
      <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
        Risk Utilization — Today
      </h2>

      {/* Alert banner */}
      {alert && (
        <div
          className={`flex items-center justify-between rounded border px-3 py-2 text-xs ${
            alertColours[alert.level as keyof typeof alertColours] ??
            "bg-gray-800 border-gray-700 text-gray-400"
          }`}
        >
          <span>{alert.message}</span>
          <button
            onClick={() => setAlert(null)}
            className="ml-3 text-gray-400 hover:text-gray-200"
          >
            ✕
          </button>
        </div>
      )}

      {/* Current stats */}
      {data && (
        <div className="grid grid-cols-3 gap-3 text-xs">
          <Stat label="Current Risk %" value={`${(parseFloat(data.aggregate_risk_pct) * 100).toFixed(2)}%`} />
          <Stat label="Open Trades" value={String(data.open_trade_count)} />
          <Stat
            label="Alert Level"
            value={data.alert_level}
            valueClass={
              data.alert_level === "CRITICAL"
                ? "text-red-400"
                : data.alert_level === "WARNING"
                  ? "text-amber-400"
                  : "text-emerald-400"
            }
          />
        </div>
      )}

      {/* In-session sparkline */}
      {history.length > 1 ? (
        <RiskSparkline history={history} maxPct={data ? parseFloat(data.critical_threshold_pct) * 100 * (1 / 0.9) : 5} />
      ) : (
        <p className="text-xs text-gray-600">
          Collecting data — chart appears after the second poll.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// SVG sparkline
// ---------------------------------------------------------------------------

function RiskSparkline({
  history,
  maxPct,
}: {
  history: HistorySample[];
  maxPct: number;
}) {
  const W = 400;
  const H = 80;
  const pad = 4;

  const minTime = history[0].time;
  const maxTime = history[history.length - 1].time;
  const timeRange = maxTime - minTime || 1;

  function x(t: number) {
    return pad + ((t - minTime) / timeRange) * (W - pad * 2);
  }
  function y(pct: number) {
    return H - pad - (pct / maxPct) * (H - pad * 2);
  }

  const points = history.map((s) => `${x(s.time)},${y(s.pct)}`).join(" ");
  const lastSample = history[history.length - 1];
  const lineColour =
    lastSample.pct >= maxPct * 0.9
      ? "#ef4444"
      : lastSample.pct >= maxPct * 0.75
        ? "#f59e0b"
        : "#10b981";

  return (
    <div>
      <p className="text-xs text-gray-500 mb-1">Session risk history</p>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full"
        preserveAspectRatio="none"
      >
        {/* Reference lines at 75% and 90% of max */}
        <line
          x1={pad} y1={y(maxPct * 0.75)} x2={W - pad} y2={y(maxPct * 0.75)}
          stroke="#f59e0b" strokeWidth="0.5" strokeDasharray="4,4" opacity="0.5"
        />
        <line
          x1={pad} y1={y(maxPct * 0.9)} x2={W - pad} y2={y(maxPct * 0.9)}
          stroke="#ef4444" strokeWidth="0.5" strokeDasharray="4,4" opacity="0.5"
        />
        {/* Data line */}
        <polyline
          points={points}
          fill="none"
          stroke={lineColour}
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {/* Current value dot */}
        <circle
          cx={x(lastSample.time)}
          cy={y(lastSample.pct)}
          r="3"
          fill={lineColour}
        />
      </svg>
    </div>
  );
}

function Stat({
  label,
  value,
  valueClass = "text-gray-200",
}: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-gray-500">{label}</span>
      <span className={`font-medium ${valueClass}`}>{value}</span>
    </div>
  );
}
