/** Trading controls: status chip, pause/resume, and flatten-all (typed confirmation). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { useTradingStatus } from "../../hooks/useTradingStatus";
import { useLiveStore } from "../../store/liveStore";
import type { FlattenResult, Trade, TradingStatus } from "../../types/api";
import { ConfirmDialog } from "../common/ConfirmDialog";

function summarize(s: TradingStatus): { headline: string; detail: string; dot: string } {
  if (!s.broker_connected)
    return { headline: "Not trading", detail: "The bot can't reach your broker. It will reconnect on its own.", dot: "bg-red-500" };
  if (s.circuit_breaker_halted)
    return { headline: "Stopped for today", detail: "The daily loss limit was hit, so the bot stops to protect your money. It resumes tomorrow.", dot: "bg-red-500" };
  if (s.paused)
    return { headline: "Paused", detail: "The bot won't open new trades until you resume.", dot: "bg-amber-500" };
  if (!s.market_open)
    return { headline: "Waiting for the market to open", detail: "Trading starts automatically when the market opens.", dot: "bg-gray-500" };
  return { headline: "Trading automatically", detail: "The bot finds trades, sizes them and manages exits for you.", dot: "bg-emerald-500" };
}

export function TradingControlsPanel() {
  const queryClient = useQueryClient();
  const [flattenOpen, setFlattenOpen] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const { data: status } = useTradingStatus();

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["trading-status"] });
    queryClient.invalidateQueries({ queryKey: ["trades"] });
  };

  const pauseMutation = useMutation({
    mutationFn: async (pause: boolean) => {
      const res = await api.post(pause ? "/trading/pause" : "/trading/resume", pause ? {} : undefined);
      if (res.error) throw new Error(res.error.message);
    },
    onSuccess: refresh,
  });

  const flattenMutation = useMutation({
    mutationFn: async () => {
      const res = await api.post<FlattenResult>("/trading/flatten", { confirm: "FLATTEN" });
      if (res.error) throw new Error(`${res.error.code}: ${res.error.message}`);
      return res.data;
    },
    onSuccess: (data) => {
      const n = data?.results.length ?? 0;
      setMessage(`Selling ${n} position${n !== 1 ? "s" : ""}. The bot is paused.`);
      setFlattenOpen(false);
      refresh();
    },
  });

  const prices = useLiveStore((s) => s.prices);
  const { data: trades = [] } = useQuery({
    queryKey: ["trades", "active"],
    queryFn: async () => {
      const res = await api.get<Trade[]>("/trades/active");
      return res.data ?? [];
    },
    refetchInterval: 60_000,
  });
  const open = trades.filter((t) => t.status === "OPEN");
  let pnl = 0;
  let missing = 0;
  for (const t of open) {
    const last = prices[t.symbol.toUpperCase()]?.price;
    if (last === undefined) {
      missing++;
      continue;
    }
    const entry = Number(t.entry_fill_price ?? t.entry_price);
    const qty = Number(t.filled_quantity ?? t.quantity);
    pnl += (last - entry) * qty * (t.direction === "BUY" ? 1 : -1);
  }
  const n = open.length;
  const plural = n !== 1 ? "s" : "";
  const pnlText = `${pnl >= 0 ? "+" : "-"}$${Math.abs(pnl).toFixed(2)}`;
  const impact =
    n === 0
      ? "You have no open positions, so there is nothing to sell. This still pauses the bot so it won't buy again until you resume."
      : missing === n
        ? `Sells ${n} position${plural} at the current price (live prices unavailable, so the profit or loss is unknown) and pauses the bot.`
        : `Sells ${n} position${plural} at the current price (now about ${pnlText} in total${missing ? `, not counting ${missing} with no live price` : ""}) and pauses the bot. It won't buy again until you resume.`;

  const c = status ? summarize(status) : null;

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-wrap items-center gap-4">
      {c ? (
        <div className="flex items-center gap-3 min-w-0">
          <span className={`w-3 h-3 rounded-full shrink-0 ${c.dot}`} />
          <div>
            <p className="text-lg font-semibold text-white leading-tight">{c.headline}</p>
            <p className="text-sm text-gray-300">{c.detail}</p>
          </div>
        </div>
      ) : (
        <span className="text-sm text-gray-400">Loading…</span>
      )}

      <div className="ml-auto flex items-center gap-3">
        {status && (
          <button
            onClick={() => pauseMutation.mutate(!status.paused)}
            disabled={pauseMutation.isPending}
            className={`px-4 py-2 text-sm font-medium rounded text-white disabled:opacity-40 ${
              status.paused ? "bg-emerald-600 hover:bg-emerald-700" : "bg-gray-700 hover:bg-gray-600"
            }`}
          >
            {status.paused ? "Resume trading" : "Pause trading"}
          </button>
        )}
        <button
          onClick={() => setFlattenOpen(true)}
          className="px-3 py-2 text-sm font-medium rounded border border-red-800 text-red-300 hover:bg-red-950"
        >
          Sell everything
        </button>
      </div>

      {(message || pauseMutation.error) && (
        <p className="basis-full text-sm text-gray-300" role="status">
          {pauseMutation.error?.message ?? message}
        </p>
      )}

      {flattenOpen && (
        <ConfirmDialog
          title="Sell everything now?"
          confirmLabel="Sell everything"
          busy={flattenMutation.isPending}
          error={flattenMutation.error?.message ?? null}
          onConfirm={() => flattenMutation.mutate()}
          onCancel={() => {
            flattenMutation.reset();
            setFlattenOpen(false);
          }}
        >
          {impact}
        </ConfirmDialog>
      )}
    </div>
  );
}
