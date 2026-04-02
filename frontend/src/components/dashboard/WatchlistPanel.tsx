/**
 * WatchlistPanel — symbol rows with live price and open-position indicator.
 *
 * Initial data: GET /api/v1/symbols and GET /api/v1/trades?status=OPEN
 * Live prices:  WebSocket "price_update" events { symbol, price, change_pct }
 * Navigation:   clicking a row navigates to /watchlist/:ticker
 *
 * A "Market Closed" badge is shown outside US equity trading hours (09:30–16:00 ET).
 * Rows with an open position are highlighted with a left border accent.
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import type { DashboardEvent, Trade, WatchedSymbol } from "../../types/api";

interface PriceState {
  price: number;
  change_pct: number | null;
}

interface Props {
  lastEvent: DashboardEvent | null;
}

export function WatchlistPanel({ lastEvent }: Props) {
  const navigate = useNavigate();
  const [prices, setPrices] = useState<Record<string, PriceState>>({});

  const { data: symbols = [] } = useQuery({
    queryKey: ["symbols"],
    queryFn: async () => {
      const res = await api.get<WatchedSymbol[]>("/symbols");
      return res.data ?? [];
    },
    refetchInterval: 60_000,
  });

  const { data: openTrades = [] } = useQuery({
    queryKey: ["trades", "OPEN"],
    queryFn: async () => {
      const res = await api.get<Trade[]>("/trades?status=OPEN");
      return res.data ?? [];
    },
    refetchInterval: 60_000,
  });

  // Consume price_update WebSocket events
  useEffect(() => {
    if (lastEvent?.event !== "price_update") return;
    const p = lastEvent.payload as { symbol?: string; price?: number; change_pct?: number };
    if (!p.symbol || p.price === undefined) return;
    setPrices((prev) => ({
      ...prev,
      [p.symbol!.toUpperCase()]: {
        price: p.price!,
        change_pct: p.change_pct ?? null,
      },
    }));
  }, [lastEvent]);

  const openSymbols = new Set(openTrades.map((t) => t.symbol.toUpperCase()));
  const marketOpen = isMarketOpen();

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
                    {priceData.change_pct !== null && (
                      <span
                        className={`text-xs tabular-nums font-medium w-14 text-right ${
                          priceData.change_pct >= 0
                            ? "text-emerald-400"
                            : "text-red-400"
                        }`}
                      >
                        {priceData.change_pct >= 0 ? "+" : ""}
                        {priceData.change_pct.toFixed(2)}%
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

/**
 * Returns true if current US/Eastern time is within regular equity market hours
 * (09:30–16:00, Mon–Fri). Does not account for market holidays.
 */
function isMarketOpen(): boolean {
  const now = new Date();
  const et = new Date(
    now.toLocaleString("en-US", { timeZone: "America/New_York" })
  );
  const day = et.getDay(); // 0=Sun, 6=Sat
  if (day === 0 || day === 6) return false;

  const hours = et.getHours();
  const minutes = et.getMinutes();
  const timeMinutes = hours * 60 + minutes;
  return timeMinutes >= 9 * 60 + 30 && timeMinutes < 16 * 60;
}
