/**
 * ActiveTradesTable — every active trade (PENDING..CLOSING) with live P&L.
 *
 * Data: GET /api/v1/trades/active, refetched when the live store's
 * tradesVersion changes and every 60 s as a fallback.
 * Last price comes from the live store; unrealized P&L = (last - entry) * qty.
 * OPEN rows have a Close button -> POST /trades/{id}/close.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { useLiveStore } from "../../store/liveStore";
import type { Trade, TradeStatus } from "../../types/api";
import { ConfirmDialog } from "../common/ConfirmDialog";

const STATUS_STYLE: Record<TradeStatus, string> = {
  PENDING: "bg-gray-800 text-gray-400",
  SUBMITTED: "bg-blue-900/50 text-blue-300",
  OPEN: "bg-emerald-900/50 text-emerald-400",
  CLOSING: "bg-amber-900/50 text-amber-400",
  CLOSED: "bg-gray-800 text-gray-500",
  CANCELLED: "bg-gray-800 text-gray-500",
};

export function ActiveTradesTable() {
  const queryClient = useQueryClient();
  const tradesVersion = useLiveStore((s) => s.tradesVersion);
  const [closing, setClosing] = useState<Trade | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const { data: trades = [], isLoading } = useQuery({
    queryKey: ["trades", "active"],
    queryFn: async () => {
      const res = await api.get<Trade[]>("/trades/active");
      return res.data ?? [];
    },
    refetchInterval: 60_000,
  });

  useEffect(() => {
    if (tradesVersion > 0) {
      queryClient.invalidateQueries({ queryKey: ["trades"] });
    }
  }, [tradesVersion, queryClient]);

  const closeMutation = useMutation({
    mutationFn: async (trade: Trade) => {
      const res = await api.post<Trade>(`/trades/${trade.id}/close`);
      if (res.error) throw new Error(`${res.error.code}: ${res.error.message}`);
      return res.data;
    },
    onSuccess: (data) => {
      setNotice(
        data?.status === "CLOSING"
          ? `${closing?.symbol}: exit pending`
          : `${closing?.symbol}: closed`
      );
      setClosing(null);
      queryClient.invalidateQueries({ queryKey: ["trades"] });
      queryClient.invalidateQueries({ queryKey: ["portfolio-risk"] });
    },
  });

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Active Trades
        </h2>
        <span className="text-xs text-gray-500">{trades.length} active</span>
      </div>

      {notice && (
        <p className="text-xs text-emerald-400" onClick={() => setNotice(null)}>
          {notice}
        </p>
      )}
      {isLoading && <p className="text-xs text-gray-600">Loading…</p>}
      {!isLoading && trades.length === 0 && (
        <p className="text-xs text-gray-600 py-4 text-center">No active trades</p>
      )}

      {trades.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-gray-500 border-b border-gray-800">
                <th className="text-left pb-2 font-medium">Symbol</th>
                <th className="text-left pb-2 font-medium">Status</th>
                <th className="text-right pb-2 font-medium">Qty</th>
                <th className="text-right pb-2 font-medium">Entry</th>
                <th className="text-right pb-2 font-medium">Stop</th>
                <th className="text-right pb-2 font-medium">Target</th>
                <th className="text-right pb-2 font-medium">Last</th>
                <th className="text-right pb-2 font-medium">Unrealized</th>
                <th className="text-right pb-2 font-medium">Risk $</th>
                <th className="text-right pb-2 font-medium">Opened</th>
                <th className="pb-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/50">
              {trades.map((trade) => (
                <TradeRow key={trade.id} trade={trade} onClose={setClosing} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {closing && (
        <ConfirmDialog
          title={`Close ${closing.symbol}?`}
          confirmLabel="Close position"
          busy={closeMutation.isPending}
          error={closeMutation.error?.message ?? null}
          onConfirm={() => closeMutation.mutate(closing)}
          onCancel={() => {
            closeMutation.reset();
            setClosing(null);
          }}
        >
          Sends a market order to sell the whole position now.
        </ConfirmDialog>
      )}
    </div>
  );
}

function money(v: string | number): string {
  return `$${Number(v).toFixed(2)}`;
}

function TradeRow({
  trade,
  onClose,
}: {
  trade: Trade;
  onClose: (t: Trade) => void;
}) {
  const last = useLiveStore((s) => s.prices[trade.symbol.toUpperCase()]?.price);
  const entry = Number(trade.entry_fill_price ?? trade.entry_price);
  const qty = Number(trade.filled_quantity ?? trade.quantity);
  const sign = trade.direction === "BUY" ? 1 : -1;
  const unrealized =
    last !== undefined && (trade.status === "OPEN" || trade.status === "CLOSING")
      ? (last - entry) * qty * sign
      : null;

  return (
    <tr className="hover:bg-gray-800/30 transition-colors">
      <td className="py-2 font-medium text-gray-200">{trade.symbol}</td>
      <td className="py-2">
        <span className={`px-1.5 py-0.5 rounded ${STATUS_STYLE[trade.status]}`}>
          {trade.status}
        </span>
      </td>
      <td className="py-2 text-right text-gray-300">{qty}</td>
      <td className="py-2 text-right text-gray-300">{money(entry)}</td>
      <td className="py-2 text-right text-gray-400">
        {money(trade.current_stop_price ?? trade.stop_loss_price)}
      </td>
      <td className="py-2 text-right text-gray-400">{money(trade.take_profit_price)}</td>
      <td className="py-2 text-right text-gray-300">
        {last !== undefined ? money(last) : "—"}
      </td>
      <td
        className={`py-2 text-right font-medium tabular-nums ${
          unrealized === null
            ? "text-gray-500"
            : unrealized >= 0
              ? "text-emerald-400"
              : "text-red-400"
        }`}
      >
        {unrealized === null
          ? "—"
          : `${unrealized >= 0 ? "+" : "-"}${money(Math.abs(unrealized))}`}
      </td>
      <td className="py-2 text-right text-amber-400">{money(trade.risk_amount)}</td>
      <td className="py-2 text-right text-gray-500">{formatDuration(trade.executed_at)}</td>
      <td className="py-2 pl-2 text-right">
        {trade.status === "OPEN" && (
          <button
            onClick={() => onClose(trade)}
            className="px-2 py-0.5 rounded border border-red-800 text-red-400 hover:bg-red-950"
          >
            Close
          </button>
        )}
      </td>
    </tr>
  );
}

function formatDuration(executedAt: string): string {
  const totalSeconds = Math.max(0, Math.floor((Date.now() - new Date(executedAt).getTime()) / 1000));
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return "<1m";
}
