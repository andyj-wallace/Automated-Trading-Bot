/**
 * BrokerConnectionBanner — explains an IB Gateway outage and what to do about it.
 *
 * Driven by server state (/trading/status), so it is right even if the page was
 * closed when the alert fired. Hidden while the broker is connected.
 */

import { useTradingStatus } from "../../hooks/useTradingStatus";

// Same colours as the AlertsFeed severities.
const RED = "border-red-800 bg-red-950/50 text-red-300";
const AMBER = "border-amber-800 bg-amber-950/40 text-amber-300";

function minutesSince(iso: string | null): number {
  if (!iso) return 0;
  return Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60_000));
}

export function BrokerConnectionBanner() {
  const { data: status } = useTradingStatus();
  const broker = status?.broker;
  if (!broker || broker.state === "connected") return null;

  if (broker.state === "account_mismatch") {
    return (
      <div role="alert" className={`rounded-lg border p-4 ${RED}`}>
        <p className="text-sm font-semibold">IB Gateway is logged into a non-paper account</p>
        <p className="text-xs mt-1">Trading is blocked. Log the Gateway into the paper account.</p>
      </div>
    );
  }

  const down = broker.state === "down";
  const title = down
    ? `IB Gateway not connected for ${minutesSince(broker.down_since)} min`
    : "Reconnecting to IB Gateway…";

  return (
    <div role={down ? "alert" : "status"} className={`rounded-lg border p-4 ${down ? RED : AMBER}`}>
      <p className="text-sm font-semibold">{title}</p>
      {broker.hint && <p className="text-xs mt-1">{broker.hint}</p>}
      <p className="text-xs mt-2 text-gray-500">
        {broker.last_connected_at
          ? `Last connected: ${new Date(broker.last_connected_at).toLocaleString()}`
          : "Not connected since the bot started."}
        {broker.last_error && ` · ${broker.last_error}`}
      </p>
    </div>
  );
}
