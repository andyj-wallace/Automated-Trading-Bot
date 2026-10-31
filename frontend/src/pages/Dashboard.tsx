/**
 * Home. Shows one plain-English status line with pause/resume, any alerts, and
 * open positions. Risk, watchlist and system panels are tucked in a collapsed
 * "details" section. Live data comes from the live store; the single WebSocket
 * is mounted in Layout.
 */

import { ErrorBoundary } from "../components/common/ErrorBoundary";
import { AlertsFeed } from "../components/alerts/AlertsFeed";
import { BrokerConnectionBanner } from "../components/controls/BrokerConnectionBanner";
import { TradingControlsPanel } from "../components/controls/TradingControlsPanel";
import { ActiveTradesTable } from "../components/dashboard/ActiveTradesTable";
import { RiskGauge } from "../components/dashboard/RiskGauge";
import { SystemHealthPanel } from "../components/dashboard/SystemHealthPanel";
import { WatchlistPanel } from "../components/dashboard/WatchlistPanel";
import { CircuitBreakerPanel } from "../components/risk/CircuitBreakerPanel";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { useLiveStore } from "../store/liveStore";
import type { CircuitBreakerStatus, Trade } from "../types/api";

export function Dashboard() {
  const socketState = useLiveStore((s) => s.socketState);
  const hasAlerts = useLiveStore((s) => s.alerts.length > 0);

  return (
    <div className="p-8 flex flex-col gap-4 max-w-4xl">
      <h1 className="text-2xl font-bold text-white mb-2">Home</h1>

      <BrokerConnectionBanner />

      {socketState !== WebSocket.OPEN && (
        <p role="status" className="text-sm text-amber-300">
          Reconnecting to live updates. What you see below may be out of date.
        </p>
      )}

      <ErrorBoundary label="Trading controls"><TradingControlsPanel /></ErrorBoundary>

      <TodayResult />

      {hasAlerts && <AlertsFeed />}

      <ErrorBoundary label="Open positions"><ActiveTradesTable compact /></ErrorBoundary>

      <details className="rounded-lg border border-gray-800 bg-gray-900/50">
        <summary className="px-5 py-3 text-sm text-gray-400 cursor-pointer select-none">
          Risk &amp; system details
        </summary>
        <div className="p-4 flex flex-col gap-4">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <ErrorBoundary label="Risk gauge"><RiskGauge /></ErrorBoundary>
            <ErrorBoundary label="Loss limit"><CircuitBreakerPanel /></ErrorBoundary>
          </div>
          <ErrorBoundary label="Watchlist"><WatchlistPanel /></ErrorBoundary>
          <ErrorBoundary label="System status"><SystemHealthPanel /></ErrorBoundary>
        </div>
      </details>
    </div>
  );
}

function TodayResult() {
  const tradesVersion = useLiveStore((s) => s.tradesVersion);
  const { data } = useQuery({
    queryKey: ["circuit-breaker", tradesVersion],
    queryFn: async () => (await api.get<CircuitBreakerStatus>("/risk/circuit-breaker")).data,
    refetchInterval: 30_000,
  });
  const prices = useLiveStore((s) => s.prices);
  const { data: open = [] } = useQuery({
    queryKey: ["trades", "active"],
    queryFn: async () => (await api.get<Trade[]>("/trades/active")).data ?? [],
    refetchInterval: 60_000,
  });
  if (!data) return null;
  const pnl = Number(data.daily_realized_pnl);
  let unrealized = 0;
  let priced = 0;
  for (const t of open) {
    if (t.status !== "OPEN") continue;
    const last = prices[t.symbol]?.price;
    if (last == null) continue;
    const sign = t.direction === "BUY" ? 1 : -1;
    unrealized += (last - Number(t.entry_fill_price ?? t.entry_price)) * Number(t.filled_quantity ?? t.quantity) * sign;
    priced++;
  }
  return (
    <p className="text-base text-gray-300">
      Earned today:{" "}
      <span className={`text-2xl font-semibold tabular-nums ${pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
        {pnl >= 0 ? "+" : "-"}${Math.abs(pnl).toFixed(2)}
      </span>
      {priced > 0 && (
        <span className="ml-3 text-sm text-gray-400">
          open positions:{" "}
          <span className={`tabular-nums ${unrealized >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {unrealized >= 0 ? "+" : "-"}${Math.abs(unrealized).toFixed(2)}
          </span>
        </span>
      )}
    </p>
  );
}
