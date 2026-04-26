/**
 * Portfolio page — Layer 14.2.
 *
 * Shows current risk exposure, session utilization history, and open trades.
 * The RiskMetricsPanel reacts to live risk_alert WebSocket events.
 */

import { useQuery } from "@tanstack/react-query";
import { RiskMetricsPanel } from "../components/portfolio/RiskMetricsPanel";
import { AdvancedAnalyticsPanel } from "../components/portfolio/AdvancedAnalyticsPanel";
import { RiskGauge } from "../components/dashboard/RiskGauge";
import { ActiveTradesTable } from "../components/dashboard/ActiveTradesTable";
import { useWebSocket } from "../hooks/useWebSocket";
import { api } from "../api/client";
import type { Trade } from "../types/api";

export function Portfolio() {
  const { lastEvent } = useWebSocket();

  const { data: closedTrades = [], isLoading } = useQuery({
    queryKey: ["trades", "CLOSED"],
    queryFn: async () => {
      const res = await api.get<Trade[]>("/trades?status=CLOSED&limit=50");
      return res.data ?? [];
    },
    staleTime: 60_000,
  });

  return (
    <div className="p-8 max-w-4xl flex flex-col gap-4">
      <div>
        <h1 className="text-2xl font-bold text-white mb-1">Portfolio</h1>
        <p className="text-gray-500 text-sm">
          Open positions · P&L · risk utilization
        </p>
      </div>

      {/* Risk snapshot + gauage side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <RiskGauge lastEvent={lastEvent} />
        <RiskMetricsPanel lastEvent={lastEvent} />
      </div>

      {/* Open positions */}
      <ActiveTradesTable lastEvent={lastEvent} />

      {/* Advanced analytics */}
      <AdvancedAnalyticsPanel />

      {/* Closed trade history */}
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Recent Closed Trades
        </h2>

        {isLoading && <p className="text-xs text-gray-600">Loading…</p>}

        {!isLoading && closedTrades.length === 0 && (
          <p className="text-xs text-gray-600 py-4 text-center">No closed trades yet</p>
        )}

        {closedTrades.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left pb-2 font-medium">Symbol</th>
                  <th className="text-left pb-2 font-medium">Dir</th>
                  <th className="text-right pb-2 font-medium">Entry</th>
                  <th className="text-right pb-2 font-medium">Exit</th>
                  <th className="text-right pb-2 font-medium">P&L</th>
                  <th className="text-right pb-2 font-medium">Closed</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/50">
                {closedTrades.map((trade) => {
                  const pnl = trade.pnl ? parseFloat(trade.pnl) : null;
                  return (
                    <tr key={trade.id} className="hover:bg-gray-800/30 transition-colors">
                      <td className="py-2 font-medium text-gray-200">{trade.symbol}</td>
                      <td className={`py-2 font-medium ${trade.direction === "BUY" ? "text-emerald-400" : "text-red-400"}`}>
                        {trade.direction}
                      </td>
                      <td className="py-2 text-right text-gray-300">
                        ${parseFloat(trade.entry_price).toFixed(2)}
                      </td>
                      <td className="py-2 text-right text-gray-300">
                        {trade.exit_price ? `$${parseFloat(trade.exit_price).toFixed(2)}` : "—"}
                      </td>
                      <td className={`py-2 text-right font-medium tabular-nums ${
                        pnl === null ? "text-gray-500" : pnl >= 0 ? "text-emerald-400" : "text-red-400"
                      }`}>
                        {pnl === null ? "—" : `${pnl >= 0 ? "+" : ""}$${Math.abs(pnl).toFixed(2)}`}
                      </td>
                      <td className="py-2 text-right text-gray-500">
                        {trade.closed_at
                          ? new Date(trade.closed_at).toLocaleDateString("en-US", { month: "short", day: "numeric" })
                          : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
