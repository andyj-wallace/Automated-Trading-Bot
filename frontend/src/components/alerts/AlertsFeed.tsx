/** Alerts feed: newest first from the live store, colored by severity, dismissible. */

import { useLiveStore } from "../../store/liveStore";
import type { DashboardEvent } from "../../types/api";

type Severity = "CRITICAL" | "WARNING" | "INFO";

const STYLE: Record<Severity, string> = {
  CRITICAL: "border-red-800 bg-red-950/50 text-red-300",
  WARNING: "border-amber-800 bg-amber-950/40 text-amber-300",
  INFO: "border-gray-700 bg-gray-800/50 text-gray-300",
};

/** Plain-English title and next step for known backend alert_type values. */
const KNOWN: Record<string, { title: string; step: string }> = {
  BROKER_DISCONNECTED: { title: "Broker connection lost", step: "The bot reconnects on its own. If it persists, check that IB Gateway is running." },
  BROKER_RECONNECTED: { title: "Broker reconnected", step: "No action needed." },
  BROKER_ACCOUNT_MODE_MISMATCH: { title: "Wrong account type", step: "Trading is blocked. Make sure IB Gateway is logged into the paper account, then restart the bot." },
  ENGINE_LOCK_LOST: { title: "Bot lost its lock", step: "Another copy of the bot may be running. Make sure only one is running, then restart it." },
  PRICE_FEED_STALE: { title: "Prices stopped updating", step: "The bot waits for fresh prices. If it persists, check IB Gateway and your internet." },
  DATA_REFRESH_FAILED: { title: "Market data update failed", step: "The bot retries on its own. No action unless it keeps happening." },
  SCHEDULER_FAILING: { title: "Strategy checks are failing", step: "Check the bot logs if this keeps repeating." },
  GROWTH_MODE_CHANGED: { title: "Small-account mode changed", step: "No action needed." },
  CIRCUIT_BREAKER_HALT: { title: "Trading paused after losses", step: "No action needed. The bot stopped opening trades to protect your money." },
  STRATEGY_AUTO_DISABLED: { title: "A strategy was turned off", step: "It was losing too much. Review it on the strategies page before re-enabling." },
  PROTECTIVE_STOP_FAILED: { title: "Safety exit not placed", step: "Check the position in IB Gateway and close it by hand if needed." },
  CLOSE_FAILED: { title: "Could not close a trade", step: "The bot will retry. If it persists, close the position in IB Gateway." },
  CLOSE_PARTIAL: { title: "Trade only partly closed", step: "Check the remaining shares in IB Gateway." },
  CLOSE_NEEDS_MANUAL_INTERVENTION: { title: "A trade needs your help", step: "Close the position in IB Gateway, then check the bot's positions." },
  ORDER_STUCK_WORKING: { title: "An order is taking too long", step: "Check the order in IB Gateway and cancel it if it looks stuck." },
  ORDER_STATE_UNKNOWN: { title: "Order status unclear", step: "Check IB Gateway to see whether the order went through." },
  QUANTITY_MISMATCH: { title: "Share count differs from broker", step: "The bot noted it. Compare positions with IB Gateway." },
  POSITION_CLOSED_EXTERNALLY: { title: "A trade was closed outside the bot", step: "No action needed. The bot updated its records." },
  UNMANAGED_POSITION: { title: "Broker holds a trade the bot didn't open", step: "The bot won't manage it. Close it in IB Gateway if unwanted." },
};

function describe(e: DashboardEvent): { severity: Severity; title: string; message: string; step?: string } {
  const p = e.payload as Record<string, unknown>;
  if (e.event === "system_alert") {
    const sev = String(p.severity ?? "INFO").toUpperCase();
    const known = KNOWN[String(p.alert_type)];
    return {
      severity: sev === "CRITICAL" || sev === "WARNING" ? sev : "INFO",
      step: known?.step,
      title: known?.title ?? String(p.alert_type ?? "Notice").replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()),
      message: String(p.message ?? ""),
    };
  }
  const level = String(p.alert_level ?? "WARNING").toUpperCase();
  const pct = p.aggregate_risk_pct ? `${(Number(p.aggregate_risk_pct) * 100).toFixed(2)}%` : "";
  return {
    severity: level === "CRITICAL" ? "CRITICAL" : "WARNING",
    title: "Risk is high",
    message: `${pct} of your account is at risk. The bot won't open new trades beyond its limit.`,
    step: "No action needed. The bot stops opening trades until risk drops.",
  };
}

export function AlertsFeed() {
  const alerts = useLiveStore((s) => s.alerts);
  const dismiss = useLiveStore((s) => s.dismissAlert);

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-3">
      <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Alerts</h2>
      {alerts.length === 0 && (
        <p className="text-xs text-gray-400 py-2 text-center">No alerts this session</p>
      )}
      {alerts.map((e, i) => {
        const a = describe(e);
        return (
          <div
            key={`${e.timestamp}-${i}`}
            className={`flex items-start justify-between gap-3 rounded border px-3 py-2 text-sm ${STYLE[a.severity]}`}
          >
            <div className="min-w-0">
              <div className="font-medium">
                {a.title}
                <span className="ml-2 font-normal opacity-60">
                  {new Date(e.timestamp).toLocaleTimeString()}
                </span>
              </div>
              <div className="break-words">{a.message}</div>
              {a.step && <div className="break-words text-gray-300">Next step: {a.step}</div>}
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
