/** Trading controls: status chip, pause/resume, and flatten-all (typed confirmation). */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { useTradingStatus } from "../../hooks/useTradingStatus";
import type { FlattenResult, TradingStatus } from "../../types/api";
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
  return { headline: "Trading automatically", detail: "The bot finds trades, sizes them and manages exits for you.", dot: "bg-emerald-500 animate-pulse" };
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
      setMessage(`Flatten requested for ${n} position${n !== 1 ? "s" : ""}; trading paused.`);
      setFlattenOpen(false);
      refresh();
    },
  });

  const c = status ? summarize(status) : null;

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-wrap items-center gap-4">
      {c ? (
        <div className="flex items-center gap-3 min-w-0">
          <span className={`w-3 h-3 rounded-full shrink-0 ${c.dot}`} />
          <div>
            <p className="text-lg font-semibold text-white leading-tight">{c.headline}</p>
            <p className="text-sm text-gray-400">{c.detail}</p>
          </div>
        </div>
      ) : (
        <span className="text-sm text-gray-600">Loading…</span>
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
          className="text-xs text-red-400 hover:text-red-300 underline underline-offset-2"
        >
          Sell everything
        </button>
      </div>

      {(message || pauseMutation.error) && (
        <p className="basis-full text-xs text-gray-400">
          {pauseMutation.error?.message ?? message}
        </p>
      )}

      {flattenOpen && (
        <ConfirmDialog
          title="Flatten all positions?"
          confirmLabel="Flatten"
          requirePhrase="FLATTEN"
          busy={flattenMutation.isPending}
          error={flattenMutation.error?.message ?? null}
          onConfirm={() => flattenMutation.mutate()}
          onCancel={() => {
            flattenMutation.reset();
            setFlattenOpen(false);
          }}
        >
          Pauses automated trading and closes every open position at market.
        </ConfirmDialog>
      )}
    </div>
  );
}
