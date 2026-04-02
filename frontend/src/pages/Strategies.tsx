/**
 * Strategies page — Layer 13.
 *
 * Lists all strategies. Each row shows:
 *   - Name + type badge
 *   - Enable/disable toggle (PATCH /api/v1/strategies/:id)
 *   - Expand button → inline StrategyConfigForm + StrategyPerformanceChart
 *
 * Config edits are saved explicitly via a "Save" button (not auto-save),
 * so accidental changes are not immediately applied to the live scheduler.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useState } from "react";
import { StrategyConfigForm } from "../components/strategies/StrategyConfigForm";
import { StrategyPerformanceChart } from "../components/strategies/StrategyPerformanceChart";
import { api } from "../api/client";
import type { Strategy, StrategyPatch, WatchedSymbol } from "../types/api";

export function Strategies() {
  const { data: strategies = [], isLoading } = useQuery({
    queryKey: ["strategies"],
    queryFn: async () => {
      const res = await api.get<Strategy[]>("/strategies");
      return res.data ?? [];
    },
  });

  const { data: symbols = [] } = useQuery({
    queryKey: ["symbols"],
    queryFn: async () => {
      const res = await api.get<WatchedSymbol[]>("/symbols");
      return res.data ?? [];
    },
  });

  return (
    <div className="p-8 max-w-4xl">
      <h1 className="text-2xl font-bold text-white mb-1">Strategies</h1>
      <p className="text-gray-500 text-sm mb-8">
        Enable / disable · configure parameters · assign symbols
      </p>

      {isLoading && (
        <p className="text-xs text-gray-600">Loading strategies…</p>
      )}

      {!isLoading && strategies.length === 0 && (
        <div className="rounded-lg border border-dashed border-gray-700 bg-gray-900/50 p-12 text-center">
          <p className="text-gray-500 text-sm">No strategies registered</p>
          <p className="text-gray-600 text-xs mt-1">
            Strategies appear here once registered with the backend registry.
          </p>
        </div>
      )}

      <div className="flex flex-col gap-3">
        {strategies.map((strategy) => (
          <StrategyCard
            key={strategy.id}
            strategy={strategy}
            symbols={symbols}
          />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Strategy card — collapsed + expanded
// ---------------------------------------------------------------------------

function StrategyCard({
  strategy,
  symbols,
}: {
  strategy: Strategy;
  symbols: WatchedSymbol[];
}) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [draftConfig, setDraftConfig] = useState<Record<string, unknown>>(
    strategy.config
  );
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  const toggleMutation = useMutation({
    mutationFn: async (is_enabled: boolean) => {
      const res = await api.patch<Strategy>(`/strategies/${strategy.id}`, {
        is_enabled,
      } satisfies StrategyPatch);
      if (res.error) throw new Error(res.error.message);
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
    },
  });

  const saveMutation = useMutation({
    mutationFn: async (config: Record<string, unknown>) => {
      const res = await api.patch<Strategy>(`/strategies/${strategy.id}`, {
        config,
      } satisfies StrategyPatch);
      if (res.error) throw new Error(res.error.message);
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      setSaveError(null);
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 2000);
    },
    onError: (err: Error) => {
      setSaveError(err.message);
    },
  });

  const assignedCount = Array.isArray(strategy.config.symbols)
    ? (strategy.config.symbols as string[]).length
    : 0;

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 overflow-hidden">
      {/* Header row */}
      <div className="flex items-center justify-between px-5 py-4">
        <div className="flex items-center gap-3 min-w-0">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="flex items-center gap-3 min-w-0 text-left"
          >
            <span
              className={`text-xs transition-transform duration-150 text-gray-500 ${
                expanded ? "rotate-90" : ""
              }`}
            >
              ▶
            </span>
            <div className="min-w-0">
              <span className="text-sm font-semibold text-gray-200">
                {strategy.name}
              </span>
              <div className="flex items-center gap-2 mt-0.5">
                <span className="text-xs px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 font-mono">
                  {strategy.type}
                </span>
                {assignedCount > 0 && (
                  <span className="text-xs text-gray-600">
                    {assignedCount} symbol{assignedCount !== 1 ? "s" : ""}
                  </span>
                )}
              </div>
            </div>
          </button>
        </div>

        {/* Enable/disable toggle */}
        <Toggle
          enabled={strategy.is_enabled}
          disabled={toggleMutation.isPending}
          onChange={(v) => toggleMutation.mutate(v)}
        />
      </div>

      {/* Expanded: config form + performance chart */}
      {expanded && (
        <div className="border-t border-gray-800 px-5 py-5 flex flex-col gap-6">
          {/* Config form */}
          <div>
            <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">
              Configuration
            </h3>
            <StrategyConfigForm
              strategyType={strategy.type}
              config={draftConfig}
              symbols={symbols}
              onChange={setDraftConfig}
              disabled={saveMutation.isPending}
            />

            <div className="flex items-center gap-3 mt-4">
              <button
                onClick={() => saveMutation.mutate(draftConfig)}
                disabled={saveMutation.isPending}
                className="px-4 py-2 text-sm font-medium bg-blue-600 hover:bg-blue-700 disabled:bg-blue-900 disabled:text-blue-400 text-white rounded transition-colors"
              >
                {saveMutation.isPending ? "Saving…" : "Save Config"}
              </button>
              <button
                onClick={() => {
                  setDraftConfig(strategy.config);
                  setSaveError(null);
                }}
                disabled={saveMutation.isPending}
                className="px-4 py-2 text-sm text-gray-500 hover:text-gray-300 transition-colors"
              >
                Reset
              </button>
              {saveSuccess && (
                <span className="text-xs text-emerald-400">Saved</span>
              )}
              {saveError && (
                <span className="text-xs text-red-400">{saveError}</span>
              )}
            </div>
          </div>

          {/* Performance chart */}
          <div>
            <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">
              Performance
            </h3>
            <StrategyPerformanceChart
              strategyId={strategy.id}
              strategyName={strategy.name}
            />
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Toggle switch
// ---------------------------------------------------------------------------

function Toggle({
  enabled,
  disabled,
  onChange,
}: {
  enabled: boolean;
  disabled: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      disabled={disabled}
      onClick={() => onChange(!enabled)}
      className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none disabled:opacity-40 ${
        enabled ? "bg-blue-600" : "bg-gray-700"
      }`}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
          enabled ? "translate-x-6" : "translate-x-1"
        }`}
      />
      <span className="sr-only">{enabled ? "Disable" : "Enable"} strategy</span>
    </button>
  );
}
