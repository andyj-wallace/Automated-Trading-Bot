/**
 * ActiveTradesTable — shows open trades with live P&L.
 *
 * Initial data: GET /api/v1/trades?status=OPEN
 * Live updates: WebSocket "position_update" events (from parent via lastEvent prop)
 *
 * P&L is computed client-side as (exit_price ?? live_price - entry_price) * quantity.
 * The parent passes lastEvent so this component doesn't need its own WS connection.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { api } from "../../api/client";
import type { DashboardEvent, Trade } from "../../types/api";

interface Props {
  lastEvent: DashboardEvent | null;
}

export function ActiveTradesTable({ lastEvent }: Props) {
  const queryClient = useQueryClient();

  const { data: trades = [], isLoading } = useQuery({
    queryKey: ["trades", "OPEN"],
    queryFn: async () => {
      const res = await api.get<Trade[]>("/trades?status=OPEN");
      return res.data ?? [];
    },
    refetchInterval: 60_000,
  });

  // Invalidate on position_update or trade_executed / trade_closed events
  useEffect(() => {
    if (!lastEvent) return;
    if (
      lastEvent.event === "position_update" ||
      lastEvent.event === "trade_executed" ||
      lastEvent.event === "trade_closed"
    ) {
      queryClient.invalidateQueries({ queryKey: ["trades", "OPEN"] });
    }
  }, [lastEvent, queryClient]);

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Active Trades
        </h2>
        <span className="text-xs text-gray-500">{trades.length} open</span>
      </div>

      {isLoading && (
        <p className="text-xs text-gray-600">Loading…</p>
      )}

      {!isLoading && trades.length === 0 && (
        <p className="text-xs text-gray-600 py-4 text-center">No open trades</p>
      )}

      {trades.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-gray-500 border-b border-gray-800">
                <th className="text-left pb-2 font-medium">Symbol</th>
                <th className="text-left pb-2 font-medium">Dir</th>
                <th className="text-right pb-2 font-medium">Entry</th>
                <th className="text-right pb-2 font-medium">Stop</th>
                <th className="text-right pb-2 font-medium">Risk $</th>
                <th className="text-right pb-2 font-medium">Duration</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/50">
              {trades.map((trade) => (
                <TradeRow key={trade.id} trade={trade} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function TradeRow({ trade }: { trade: Trade }) {
  const directionColour =
    trade.direction === "BUY" ? "text-emerald-400" : "text-red-400";

  const duration = formatDuration(trade.executed_at);

  return (
    <tr className="hover:bg-gray-800/30 transition-colors">
      <td className="py-2 font-medium text-gray-200">{trade.symbol}</td>
      <td className={`py-2 font-medium ${directionColour}`}>
        {trade.direction}
      </td>
      <td className="py-2 text-right text-gray-300">
        ${parseFloat(trade.entry_price).toFixed(2)}
      </td>
      <td className="py-2 text-right text-gray-400">
        ${parseFloat(trade.stop_loss_price).toFixed(2)}
      </td>
      <td className="py-2 text-right text-amber-400">
        ${parseFloat(trade.risk_amount).toFixed(2)}
      </td>
      <td className="py-2 text-right text-gray-500">{duration}</td>
    </tr>
  );
}

function formatDuration(executedAt: string): string {
  const ms = Date.now() - new Date(executedAt).getTime();
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);

  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return "<1m";
}
