/**
 * My Trades: realized P&L and closed-trade history. Open positions live on Home.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Trade } from "../types/api";

function signed(v: number): string {
  return `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(2)}`;
}

export function Portfolio() {
  const { data: closedTrades = [], isLoading } = useQuery({
    queryKey: ["trades", "CLOSED"],
    queryFn: async () => {
      const res = await api.get<Trade[]>("/trades?status=CLOSED&limit=50");
      return res.data ?? [];
    },
    staleTime: 60_000,
  });

  const { data: allTime } = useQuery({
    queryKey: ["metrics", "performance", "all"],
    queryFn: async () =>
      (await api.get<{ trade_count: number; total_pnl: string }>("/metrics/performance?range=all")).data,
    staleTime: 60_000,
  });

  const pnls = closedTrades.filter((t) => t.pnl != null).map((t) => parseFloat(t.pnl as string));
  const totalPnl = pnls.reduce((a, b) => a + b, 0);
  const wins = pnls.filter((p) => p > 0).length;

  return (
    <div className="p-8 max-w-4xl flex flex-col gap-4">
      <div>
        <h1 className="text-2xl font-bold text-white mb-1">My Trades</h1>
        <p className="text-sm text-gray-400">
          {closedTrades.length === 0 ? (
            "Nothing closed yet. Results appear here after the bot's first completed trade."
          ) : (
            <>
              <span className={`font-semibold tabular-nums ${totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                {signed(totalPnl)}
              </span>{" "}
              across your last {pnls.length} closed trades, {wins} winning
              {allTime && allTime.trade_count > 0 && (
                <>
                  {". All time: "}
                  <span className="tabular-nums">{signed(Number(allTime.total_pnl))}</span>
                  {` across ${allTime.trade_count} closed trades`}
                </>
              )}
            </>
          )}
        </p>
      </div>

      {/* Closed trade history */}
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Trade history
        </h2>

        {isLoading && <p className="text-sm text-gray-400">Loading…</p>}

        {!isLoading && closedTrades.length === 0 && (
          <p className="text-sm text-gray-400 py-4 text-center">No closed trades yet</p>
        )}

        {closedTrades.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
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
                      <td className={`py-2 font-medium text-gray-300`}>
                        {trade.direction}
                      </td>
                      <td className="py-2 text-right text-gray-300 tabular-nums">
                        ${parseFloat(trade.entry_price).toFixed(2)}
                      </td>
                      <td className="py-2 text-right text-gray-300">
                        {trade.exit_price ? `$${parseFloat(trade.exit_price).toFixed(2)}` : "—"}
                      </td>
                      <td className={`py-2 text-right font-medium tabular-nums ${
                        pnl === null ? "text-gray-500" : pnl >= 0 ? "text-emerald-400" : "text-red-400"
                      }`}>
                        {pnl === null ? "—" : signed(pnl)}
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
