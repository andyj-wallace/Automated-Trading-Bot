/** Symbol detail: candlestick chart with signal/trade markers, live price, signals, trades. */

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { PriceChart, type ChartBar, type ChartMarker } from "../components/charts/PriceChart";
import { useLiveStore } from "../store/liveStore";
import type { PriceBar, StrategySignal, Trade } from "../types/api";

const day = (iso: string) => iso.slice(0, 10);

export function SymbolDetail() {
  const { ticker: raw } = useParams<{ ticker: string }>();
  const ticker = (raw ?? "").toUpperCase();
  const live = useLiveStore((s) => s.prices[ticker]);

  const { data: rawBars = [], isLoading } = useQuery({
    queryKey: ["bars", ticker],
    queryFn: async () =>
      (await api.get<PriceBar[]>(`/symbols/${ticker}/bars?days=365`)).data ?? [],
    enabled: ticker !== "",
  });
  const { data: signals = [] } = useQuery({
    queryKey: ["signals", ticker],
    queryFn: async () =>
      (await api.get<StrategySignal[]>(`/signals?symbol=${ticker}&limit=100`)).data ?? [],
    enabled: ticker !== "",
  });
  const { data: trades = [] } = useQuery({
    queryKey: ["trades", "symbol", ticker],
    queryFn: async () =>
      (await api.get<Trade[]>(`/trades?symbol=${ticker}&limit=100`)).data ?? [],
    enabled: ticker !== "",
  });

  const bars: ChartBar[] = useMemo(() => {
    const byDay = new Map<string, ChartBar>();
    for (const b of rawBars) {
      const time = day(b.time);
      byDay.set(time, {
        time,
        open: Number(b.open),
        high: Number(b.high),
        low: Number(b.low),
        close: Number(b.close),
      });
    }
    return [...byDay.values()].sort((a, b) => a.time.localeCompare(b.time));
  }, [rawBars]);

  const markers: ChartMarker[] = useMemo(() => {
    const out: ChartMarker[] = [];
    for (const s of signals) {
      const buy = s.action.toUpperCase() === "BUY";
      const sell = s.action.toUpperCase() === "SELL";
      if (!buy && !sell) continue;
      out.push({
        time: s.bar_date,
        position: buy ? "belowBar" : "aboveBar",
        color: buy ? "#34d399" : "#f87171",
        shape: buy ? "arrowUp" : "arrowDown",
        text: `${s.action} (${s.outcome})`,
      });
    }
    for (const t of trades) {
      if (t.status === "PENDING" || t.status === "CANCELLED") continue;
      out.push({
        time: day(t.executed_at),
        position: "belowBar",
        color: "#60a5fa",
        shape: "circle",
        text: "entry",
      });
      if (t.closed_at) {
        out.push({
          time: day(t.closed_at),
          position: "aboveBar",
          color: "#fbbf24",
          shape: "circle",
          text: `exit${t.exit_reason ? ` ${t.exit_reason}` : ""}`,
        });
      }
    }
    return out;
  }, [signals, trades]);

  const lastClose = bars.length ? bars[bars.length - 1].close : null;

  return (
    <div className="p-8 flex flex-col gap-4 max-w-5xl">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white mb-1">{ticker}</h1>
          <p className="text-gray-500 text-sm">Price chart · strategy signals · trade history</p>
        </div>
        <div className="text-right">
          <div className="text-2xl font-bold tabular-nums text-gray-100">
            {live ? `$${live.price.toFixed(2)}` : lastClose !== null ? `$${lastClose.toFixed(2)}` : "—"}
          </div>
          <div className="text-xs text-gray-500">
            {live
              ? `live${live.changePct !== null ? ` · ${live.changePct >= 0 ? "+" : ""}${live.changePct.toFixed(2)}%` : ""}`
              : "last daily close"}
          </div>
        </div>
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-3">
        {isLoading && <p className="text-xs text-gray-600 p-4">Loading bars…</p>}
        {!isLoading && bars.length === 0 && (
          <p className="text-xs text-gray-600 p-8 text-center">
            No stored daily bars for {ticker}. They are fetched after the next bar refresh.
          </p>
        )}
        {bars.length > 0 && <PriceChart bars={bars} markers={markers} />}
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Signals</h2>
        {signals.length === 0 ? (
          <p className="text-xs text-gray-600">No signals yet</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left pb-2 font-medium">Bar date</th>
                  <th className="text-left pb-2 font-medium">Action</th>
                  <th className="text-right pb-2 font-medium">Entry</th>
                  <th className="text-right pb-2 font-medium">Stop</th>
                  <th className="text-left pb-2 pl-4 font-medium">Outcome</th>
                  <th className="text-left pb-2 font-medium">Detail</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/50">
                {signals.map((s) => (
                  <tr key={s.id}>
                    <td className="py-1.5 text-gray-300">{s.bar_date}</td>
                    <td className={`py-1.5 font-medium ${s.action.toUpperCase() === "BUY" ? "text-emerald-400" : "text-red-400"}`}>
                      {s.action}
                    </td>
                    <td className="py-1.5 text-right text-gray-300">{price(s.entry_price)}</td>
                    <td className="py-1.5 text-right text-gray-400">{price(s.stop_loss_price)}</td>
                    <td className="py-1.5 pl-4 text-gray-300">{s.outcome}</td>
                    <td className="py-1.5 text-gray-500">{s.detail ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Trades</h2>
        {trades.length === 0 ? (
          <p className="text-xs text-gray-600">No trades yet</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left pb-2 font-medium">Opened</th>
                  <th className="text-left pb-2 font-medium">Status</th>
                  <th className="text-right pb-2 font-medium">Qty</th>
                  <th className="text-right pb-2 font-medium">Entry</th>
                  <th className="text-right pb-2 font-medium">Exit</th>
                  <th className="text-left pb-2 pl-4 font-medium">Reason</th>
                  <th className="text-right pb-2 font-medium">P&L</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/50">
                {trades.map((t) => {
                  const pnl = t.pnl !== null ? Number(t.pnl) : null;
                  return (
                    <tr key={t.id}>
                      <td className="py-1.5 text-gray-300">{day(t.executed_at)}</td>
                      <td className="py-1.5 text-gray-300">{t.status}</td>
                      <td className="py-1.5 text-right text-gray-300">{t.filled_quantity ?? t.quantity}</td>
                      <td className="py-1.5 text-right text-gray-300">{price(t.entry_fill_price ?? t.entry_price)}</td>
                      <td className="py-1.5 text-right text-gray-300">{price(t.exit_price)}</td>
                      <td className="py-1.5 pl-4 text-gray-500">{t.exit_reason ?? ""}</td>
                      <td className={`py-1.5 text-right font-medium tabular-nums ${pnl === null ? "text-gray-500" : pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                        {pnl === null ? "—" : `${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toFixed(2)}`}
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

function price(v: string | null): string {
  return v === null ? "—" : `$${Number(v).toFixed(2)}`;
}
