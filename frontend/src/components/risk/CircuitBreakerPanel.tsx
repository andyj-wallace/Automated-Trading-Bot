/** Circuit breaker: today's realized P&L vs the halt threshold, plus loss streak resets. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import { useCircuitBreaker } from "../../hooks/useCircuitBreaker";
import type { Strategy } from "../../types/api";
import { ConfirmDialog } from "../common/ConfirmDialog";

export function CircuitBreakerPanel() {
  const queryClient = useQueryClient();
  const [confirmReset, setConfirmReset] = useState(false);

  const { data, isLoading, error } = useCircuitBreaker();

  const { data: strategies = [] } = useQuery({
    queryKey: ["strategies"],
    queryFn: async () => (await api.get<Strategy[]>("/strategies")).data ?? [],
  });

  const resetMutation = useMutation({
    mutationFn: async (path: string) => {
      const res = await api.post(path);
      if (res.error) throw new Error(res.error.message);
    },
    onSuccess: () => {
      setConfirmReset(false);
      queryClient.invalidateQueries({ queryKey: ["circuit-breaker"] });
      queryClient.invalidateQueries({ queryKey: ["trading-status"] });
    },
  });

  if (isLoading) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Circuit Breaker
        </h2>
        <p className="text-xs text-gray-600">Loading loss limit…</p>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Circuit Breaker
        </h2>
        <p role="alert" className="text-xs text-red-400">
          Could not load the loss limit: {error.message}
        </p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Circuit Breaker
        </h2>
        <p className="text-xs text-gray-600">No loss-limit data yet.</p>
      </div>
    );
  }

  const pnl = Number(data.daily_realized_pnl);
  const threshold = Number(data.halt_threshold_amount);
  const lossFill = threshold > 0 ? Math.min((Math.max(0, -pnl) / threshold) * 100, 100) : 0;
  const names = new Map(strategies.map((s) => [s.id, s.name]));

  const ids = new Set([
    ...Object.keys(data.consecutive_losses).filter((id) => data.consecutive_losses[id] > 0),
    ...data.killed_strategy_ids,
  ]);
  const rows = [...ids]
    .map((id) => ({
      id,
      streak: data.consecutive_losses[id] ?? 0,
      killed: data.killed_strategy_ids.includes(id),
    }))
    .sort((a, b) => Number(b.killed) - Number(a.killed) || b.streak - a.streak);

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Circuit Breaker
        </h2>
        <span className="text-xs text-gray-500">{data.trading_date}</span>
      </div>

      <div className="flex items-baseline gap-2">
        <span
          className={`text-2xl font-bold tabular-nums ${pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}
        >
          {pnl >= 0 ? "+" : "-"}${Math.abs(pnl).toFixed(2)}
        </span>
        <span className="text-xs text-gray-500">
          realized today · halts at -${threshold.toFixed(2)} (
          {(Number(data.daily_loss_limit_pct) * 100).toFixed(1)}%)
        </span>
      </div>

      <div className="w-full h-3 bg-gray-800 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${lossFill >= 100 ? "bg-red-500" : lossFill >= 60 ? "bg-amber-500" : "bg-emerald-500"}`}
          style={{ width: `${lossFill}%` }}
        />
      </div>

      {data.daily_halted && (
        <div className="flex items-center justify-between rounded border border-red-800 bg-red-950/50 px-3 py-2">
          <span className="text-xs text-red-300">Trading halted for the day</span>
          <button
            onClick={() => setConfirmReset(true)}
            className="px-2 py-1 text-xs rounded bg-red-700 hover:bg-red-800 text-white"
          >
            Reset halt
          </button>
        </div>
      )}

      {rows.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <span className="text-xs text-gray-500">Consecutive losses</span>
          {rows.map((row) => (
            <div key={row.id} className="flex items-center justify-between text-xs">
              <span className="text-gray-300">
                {names.get(row.id) ?? row.id.slice(0, 8)}
                {row.killed && (
                  <span className="ml-2 text-red-400">killed</span>
                )}
              </span>
              <span className="flex items-center gap-3">
                <span className="tabular-nums text-amber-400">{row.streak}</span>
                <button
                  onClick={() =>
                    resetMutation.mutate(`/risk/circuit-breaker/strategies/${row.id}/reset`)
                  }
                  disabled={resetMutation.isPending}
                  className="px-2 py-0.5 rounded border border-gray-700 text-gray-300 hover:bg-gray-800"
                >
                  Reset
                </button>
              </span>
            </div>
          ))}
          <p className="text-xs text-gray-600">
            Reset clears the loss streak so the strategy can trade again. A halted strategy also has to be switched back on from the Strategies page.
          </p>
        </div>
      )}

      {resetMutation.error && !confirmReset && (
        <p className="text-xs text-red-400">{resetMutation.error.message}</p>
      )}

      {confirmReset && (
        <ConfirmDialog
          title="Reset the daily halt?"
          confirmLabel="Reset halt"
          busy={resetMutation.isPending}
          error={resetMutation.error?.message ?? null}
          onConfirm={() => resetMutation.mutate("/risk/circuit-breaker/reset")}
          onCancel={() => {
            resetMutation.reset();
            setConfirmReset(false);
          }}
        >
          Automated trading may resume today even though the daily loss limit was hit.
          The halt returns after a restart.
        </ConfirmDialog>
      )}
    </div>
  );
}
