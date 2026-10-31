/**
 * Home. Shows one plain-English status line with pause/resume, any alerts, and
 * open positions. Risk, watchlist and system panels are tucked in a collapsed
 * "details" section. Live data comes from the live store; the single WebSocket
 * is mounted in Layout.
 */

import { AlertsFeed } from "../components/alerts/AlertsFeed";
import { BrokerConnectionBanner } from "../components/controls/BrokerConnectionBanner";
import { TradingControlsPanel } from "../components/controls/TradingControlsPanel";
import { ActiveTradesTable } from "../components/dashboard/ActiveTradesTable";
import { RiskGauge } from "../components/dashboard/RiskGauge";
import { SystemHealthPanel } from "../components/dashboard/SystemHealthPanel";
import { WatchlistPanel } from "../components/dashboard/WatchlistPanel";
import { CircuitBreakerPanel } from "../components/risk/CircuitBreakerPanel";
import { useLiveStore } from "../store/liveStore";

export function Dashboard() {
  const socketState = useLiveStore((s) => s.socketState);
  const hasAlerts = useLiveStore((s) => s.alerts.length > 0);

  return (
    <div className="p-8 flex flex-col gap-4 max-w-4xl">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-2xl font-bold text-white">Home</h1>
        <WsIndicator readyState={socketState} />
      </div>

      <BrokerConnectionBanner />

      <TradingControlsPanel />

      {hasAlerts && <AlertsFeed />}

      <ActiveTradesTable compact />

      <details className="rounded-lg border border-gray-800 bg-gray-900/50">
        <summary className="px-5 py-3 text-sm text-gray-400 cursor-pointer select-none">
          Risk &amp; system details
        </summary>
        <div className="p-4 flex flex-col gap-4">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <RiskGauge />
            <CircuitBreakerPanel />
          </div>
          <WatchlistPanel />
          <SystemHealthPanel />
        </div>
      </details>
    </div>
  );
}

function WsIndicator({ readyState }: { readyState: number }) {
  const connected = readyState === WebSocket.OPEN;
  return (
    <div className="flex items-center gap-1.5 text-xs text-gray-500">
      <span
        className={`w-2 h-2 rounded-full ${connected ? "bg-emerald-500 animate-pulse" : "bg-gray-600"}`}
      />
      {connected ? "Live" : "Connecting…"}
    </div>
  );
}
