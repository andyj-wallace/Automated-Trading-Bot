/** Alerts feed: newest first from the live store, colored by severity, dismissible. */

import { useLiveStore } from "../../store/liveStore";
import type { DashboardEvent } from "../../types/api";

type Severity = "CRITICAL" | "WARNING" | "INFO";

const STYLE: Record<Severity, string> = {
  CRITICAL: "border-red-800 bg-red-950/50 text-red-300",
  WARNING: "border-amber-800 bg-amber-950/40 text-amber-300",
  INFO: "border-gray-700 bg-gray-800/50 text-gray-300",
};

function describe(e: DashboardEvent): { severity: Severity; title: string; message: string } {
  const p = e.payload as Record<string, unknown>;
  if (e.event === "system_alert") {
    const sev = String(p.severity ?? "INFO").toUpperCase();
    return {
      severity: sev === "CRITICAL" || sev === "WARNING" ? sev : "INFO",
      title: String(p.alert_type ?? "system_alert"),
      message: String(p.message ?? ""),
    };
  }
  const level = String(p.alert_level ?? "WARNING").toUpperCase();
  const pct = p.aggregate_risk_pct ? `${(Number(p.aggregate_risk_pct) * 100).toFixed(2)}%` : "";
  return {
    severity: level === "CRITICAL" ? "CRITICAL" : "WARNING",
    title: "risk_alert",
    message: `Portfolio risk ${pct} (${level})`,
  };
}

export function AlertsFeed() {
  const alerts = useLiveStore((s) => s.alerts);
  const dismiss = useLiveStore((s) => s.dismissAlert);

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-3">
      <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Alerts</h2>
      {alerts.length === 0 && (
        <p className="text-xs text-gray-600 py-2 text-center">No alerts this session</p>
      )}
      {alerts.map((e, i) => {
        const a = describe(e);
        return (
          <div
            key={`${e.timestamp}-${i}`}
            className={`flex items-start justify-between gap-3 rounded border px-3 py-2 text-xs ${STYLE[a.severity]}`}
          >
            <div className="min-w-0">
              <div className="font-medium">
                {a.severity} · {a.title}
                <span className="ml-2 font-normal opacity-60">
                  {new Date(e.timestamp).toLocaleTimeString()}
                </span>
              </div>
              <div className="break-words">{a.message}</div>
            </div>
            <button
              onClick={() => dismiss(i)}
              aria-label="Dismiss alert"
              className="shrink-0 opacity-60 hover:opacity-100"
            >
              ✕
            </button>
          </div>
        );
      })}
    </div>
  );
}
