/**
 * Dashboard.
 *
 * Top: broker-connection banner (only when the Gateway is down) and trading
 * controls. Then risk gauge + circuit breaker, active trades +
 * watchlist, alerts feed, system health. All live data comes from the live
 * store; the single WebSocket is mounted in Layout.
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

  return (
    <div className="p-8 flex flex-col gap-4">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold text-white mb-1">Dashboard</h1>
          <p className="text-gray-500 text-sm">
            Controls · risk · active trades · watchlist · alerts · system status
          </p>
        </div>
        <WsIndicator readyState={socketState} />
      </div>

      <BrokerConnectionBanner />

      <TradingControlsPanel />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <RiskGauge />
        <CircuitBreakerPanel />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <ActiveTradesTable />
        <WatchlistPanel />
      </div>

      <AlertsFeed />

      <SystemHealthPanel />
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
