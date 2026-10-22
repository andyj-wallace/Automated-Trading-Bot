/** Trading controls: status chip, pause/resume, and flatten-all (typed confirmation). */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { useTradingStatus } from "../../hooks/useTradingStatus";
import type { FlattenResult, TradingStatus } from "../../types/api";
import { ConfirmDialog } from "../common/ConfirmDialog";

function chip(s: TradingStatus): { label: string; cls: string } {
  if (!s.broker_connected) return { label: "Broker disconnected", cls: "bg-red-900/50 text-red-400" };
  if (s.circuit_breaker_halted) return { label: "Halted", cls: "bg-red-900/50 text-red-400" };
  if (s.paused) return { label: "Paused", cls: "bg-amber-900/50 text-amber-400" };
  if (!s.market_open) return { label: "Market closed", cls: "bg-gray-800 text-gray-400" };
  return { label: "Running", cls: "bg-emerald-900/50 text-emerald-400" };
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

  const c = status ? chip(status) : null;

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-4 flex flex-wrap items-center gap-3">
      <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Trading</h2>
      {c ? (
        <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${c.cls}`}>{c.label}</span>
      ) : (
        <span className="text-xs text-gray-600">Loading…</span>
      )}
      {status && (
        <span className="text-xs text-gray-500">{status.open_trade_count} open</span>
      )}

      <div className="ml-auto flex items-center gap-2">
        {status && (
          <button
            onClick={() => pauseMutation.mutate(!status.paused)}
            disabled={pauseMutation.isPending}
            className="px-3 py-1.5 text-xs font-medium rounded border border-gray-700 text-gray-200 hover:bg-gray-800 disabled:opacity-40"
          >
            {status.paused ? "Resume" : "Pause"}
          </button>
        )}
        <button
          onClick={() => setFlattenOpen(true)}
          className="px-3 py-1.5 text-xs font-medium rounded bg-red-700 hover:bg-red-800 text-white"
        >
          Flatten all
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
