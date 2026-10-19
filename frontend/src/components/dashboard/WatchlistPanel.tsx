/**
 * WatchlistPanel — symbol rows with live price and open-position indicator.
 *
 * Initial data: GET /api/v1/symbols and GET /api/v1/trades/active
 * Live prices:  live store (fed by the WebSocket "price_update" events)
 * Navigation:   clicking a row navigates to /watchlist/:ticker
 *
 * A "Market Closed" badge is shown outside US equity trading hours (from /trading/status).
 * Rows with an open position are highlighted with a left border accent.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { useLiveStore } from "../../store/liveStore";
import type { Trade, TradingStatus, WatchedSymbol } from "../../types/api";

export function WatchlistPanel() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const prices = useLiveStore((s) => s.prices);
  const tradesVersion = useLiveStore((s) => s.tradesVersion);

  const { data: symbols = [] } = useQuery({
    queryKey: ["symbols"],
    queryFn: async () => {
      const res = await api.get<WatchedSymbol[]>("/symbols");
      return res.data ?? [];
    },
    refetchInterval: 60_000,
  });

  const { data: openTrades = [] } = useQuery({
    queryKey: ["trades", "active"],
    queryFn: async () => {
      const res = await api.get<Trade[]>("/trades/active");
      return res.data ?? [];
    },
    refetchInterval: 60_000,
  });

  const { data: status } = useQuery({
    queryKey: ["trading-status"],
    queryFn: async () => (await api.get<TradingStatus>("/trading/status")).data,
    refetchInterval: 15_000,
  });

  useEffect(() => {
    if (tradesVersion > 0) {
      queryClient.invalidateQueries({ queryKey: ["trades"] });
    }
  }, [tradesVersion, queryClient]);

  const openSymbols = new Set(openTrades.map((t) => t.symbol.toUpperCase()));
  const marketOpen = status?.market_open ?? true;

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Watchlist
        </h2>
        <div className="flex items-center gap-2">
          {!marketOpen && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-gray-800 text-gray-500">
              Market Closed
            </span>
          )}
          <span className="text-xs text-gray-500">{symbols.length} symbols</span>
        </div>
      </div>

      {symbols.length === 0 && (
        <p className="text-xs text-gray-600 py-4 text-center">
          No symbols on watchlist
        </p>
      )}

      <div className="flex flex-col divide-y divide-gray-800/50">
        {symbols.map((sym) => {
          const ticker = sym.ticker.toUpperCase();
          const hasPosition = openSymbols.has(ticker);
          const priceData = prices[ticker];

          return (
            <button
              key={sym.id}
              onClick={() => navigate(`/watchlist/${ticker}`)}
              className={`flex items-center justify-between py-2.5 text-left transition-colors hover:bg-gray-800/30 -mx-1 px-1 rounded ${
                hasPosition ? "border-l-2 border-emerald-500 pl-2" : ""
              }`}
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-sm font-medium text-gray-200 shrink-0">
                  {ticker}
                </span>
                {sym.display_name && (
                  <span className="text-xs text-gray-600 truncate hidden sm:block">
                    {sym.display_name}
                  </span>
                )}
                {hasPosition && (
                  <span className="text-xs px-1.5 py-0.5 rounded bg-emerald-900/50 text-emerald-400 shrink-0">
                    Open
                  </span>
                )}
              </div>

              <div className="flex items-center gap-3 shrink-0">
                {priceData ? (
                  <>
                    <span className="text-sm tabular-nums text-gray-200">
                      ${priceData.price.toFixed(2)}
                    </span>
                    {priceData.changePct !== null && (
                      <span
                        className={`text-xs tabular-nums font-medium w-14 text-right ${
                          priceData.changePct >= 0
                            ? "text-emerald-400"
                            : "text-red-400"
                        }`}
                      >
                        {priceData.changePct >= 0 ? "+" : ""}
                        {priceData.changePct.toFixed(2)}%
                      </span>
                    )}
                  </>
                ) : (
                  <span className="text-xs text-gray-600 w-20 text-right">—</span>
                )}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
