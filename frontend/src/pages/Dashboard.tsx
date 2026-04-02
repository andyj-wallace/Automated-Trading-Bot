/**
 * Dashboard — Layer 10 assembly.
 *
 * Layout:
 *   Top row (full-width): RiskGauge
 *   Middle row:           ActiveTradesTable | WatchlistPanel
 *   Bottom row:           SystemHealthPanel (full-width)
 *
 * A single useWebSocket() call lives here; the lastEvent is passed down to
 * each panel so they can react to live data without opening their own sockets.
 */

import { ActiveTradesTable } from "../components/dashboard/ActiveTradesTable";
import { RiskGauge } from "../components/dashboard/RiskGauge";
import { SystemHealthPanel } from "../components/dashboard/SystemHealthPanel";
import { WatchlistPanel } from "../components/dashboard/WatchlistPanel";
import { useWebSocket } from "../hooks/useWebSocket";

export function Dashboard() {
  const { lastEvent, readyState } = useWebSocket();

  return (
    <div className="p-8 flex flex-col gap-4">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold text-white mb-1">Dashboard</h1>
          <p className="text-gray-500 text-sm">
            Risk gauge · active trades · watchlist · system status
          </p>
        </div>
        <WsIndicator readyState={readyState} />
      </div>

      {/* Row 1: Risk Gauge — full width, prominent */}
      <RiskGauge lastEvent={lastEvent} />

      {/* Row 2: Active Trades + Watchlist */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <ActiveTradesTable lastEvent={lastEvent} />
        <WatchlistPanel lastEvent={lastEvent} />
      </div>

      {/* Row 3: System Health */}
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
