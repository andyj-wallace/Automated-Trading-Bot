/**
 * StrategyConfigForm — editable form for a strategy's JSONB config.
 *
 * Renders fields based on the known schema for each strategy type:
 *   moving_average → fast_period, slow_period, stop_loss_pct
 *   (unknown types → generic key/value editor)
 *
 * Also renders a multi-select symbol picker drawn from the watchlist.
 *
 * Props:
 *   strategyType  — used to pick the right field set
 *   config        — current config value
 *   symbols       — all watched symbols (for the multi-select)
 *   onChange      — called on every change with the new config dict
 *   disabled      — locks all inputs while a save is in-flight
 */

import { type WatchedSymbol } from "../../types/api";

interface Props {
  strategyType: string;
  config: Record<string, unknown>;
  symbols: WatchedSymbol[];
  onChange: (next: Record<string, unknown>) => void;
  disabled?: boolean;
}

export function StrategyConfigForm({
  strategyType,
  config,
  symbols,
  onChange,
  disabled = false,
}: Props) {
  function set(key: string, value: unknown) {
    onChange({ ...config, [key]: value });
  }

  const assignedSymbols: string[] = Array.isArray(config.symbols)
    ? (config.symbols as string[])
    : [];

  function toggleSymbol(ticker: string) {
    const next = assignedSymbols.includes(ticker)
      ? assignedSymbols.filter((s) => s !== ticker)
      : [...assignedSymbols, ticker];
    set("symbols", next);
  }

  return (
    <div className="flex flex-col gap-5">
      {/* Strategy-type-specific fields */}
      {strategyType === "moving_average" && (
        <MovingAverageFields config={config} set={set} disabled={disabled} />
      )}

      {strategyType !== "moving_average" && (
        <GenericConfigFields config={config} set={set} disabled={disabled} />
      )}

      {/* Symbol multi-select */}
      <div>
        <label className="block text-xs font-medium text-gray-400 mb-2">
          Assigned Symbols
        </label>
        {symbols.length === 0 ? (
          <p className="text-xs text-gray-600">
            No symbols on watchlist — add some first.
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {symbols.map((sym) => {
              const selected = assignedSymbols.includes(sym.ticker);
              return (
                <button
                  key={sym.ticker}
                  type="button"
                  disabled={disabled}
                  onClick={() => toggleSymbol(sym.ticker)}
                  className={`px-3 py-1.5 rounded text-xs font-medium transition-colors border ${
                    selected
                      ? "bg-blue-600 border-blue-600 text-white"
                      : "bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-500"
                  } disabled:opacity-40`}
                >
                  {sym.ticker}
                </button>
              );
            })}
          </div>
        )}
        {assignedSymbols.length > 0 && (
          <p className="text-xs text-gray-600 mt-1.5">
            {assignedSymbols.length} symbol{assignedSymbols.length !== 1 ? "s" : ""} assigned
          </p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Moving average specific fields
// ---------------------------------------------------------------------------

function MovingAverageFields({
  config,
  set,
  disabled,
}: {
  config: Record<string, unknown>;
  set: (k: string, v: unknown) => void;
  disabled: boolean;
}) {
  return (
    <div className="grid grid-cols-3 gap-3">
      <NumberField
        label="Fast Period"
        description="e.g. 50"
        value={config.fast_period as number | undefined}
        defaultValue={50}
        min={1}
        disabled={disabled}
        onChange={(v) => set("fast_period", v)}
      />
      <NumberField
        label="Slow Period"
        description="e.g. 200"
        value={config.slow_period as number | undefined}
        defaultValue={200}
        min={2}
        disabled={disabled}
        onChange={(v) => set("slow_period", v)}
      />
      <TextField
        label="Stop Loss %"
        description='e.g. "0.03" = 3%'
        value={config.stop_loss_pct as string | undefined}
        defaultValue="0.03"
        disabled={disabled}
        onChange={(v) => set("stop_loss_pct", v)}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Generic key/value editor for unknown strategy types
// ---------------------------------------------------------------------------

function GenericConfigFields({
  config,
  set,
  disabled,
}: {
  config: Record<string, unknown>;
  set: (k: string, v: unknown) => void;
  disabled: boolean;
}) {
  const entries = Object.entries(config).filter(([k]) => k !== "symbols");
  if (entries.length === 0) return null;

  return (
    <div className="flex flex-col gap-2">
      <label className="text-xs font-medium text-gray-400">Config Parameters</label>
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center gap-2">
          <span className="text-xs text-gray-500 w-32 shrink-0">{key}</span>
          <input
            type="text"
            value={String(value)}
            disabled={disabled}
            onChange={(e) => set(key, e.target.value)}
            className="flex-1 bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-600 disabled:opacity-40"
          />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Reusable field primitives
// ---------------------------------------------------------------------------

function NumberField({
  label,
  description,
  value,
  defaultValue,
  min,
  disabled,
  onChange,
}: {
  label: string;
  description: string;
  value: number | undefined;
  defaultValue: number;
  min: number;
  disabled: boolean;
  onChange: (v: number) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-xs font-medium text-gray-400">{label}</label>
      <input
        type="number"
        min={min}
        value={value ?? defaultValue}
        disabled={disabled}
        onChange={(e) => onChange(parseInt(e.target.value, 10))}
        className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-600 disabled:opacity-40"
      />
      <span className="text-xs text-gray-600">{description}</span>
    </div>
  );
}

function TextField({
  label,
  description,
  value,
  defaultValue,
  disabled,
  onChange,
}: {
  label: string;
  description: string;
  value: string | undefined;
  defaultValue: string;
  disabled: boolean;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-xs font-medium text-gray-400">{label}</label>
      <input
        type="text"
        value={value ?? defaultValue}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-600 disabled:opacity-40"
      />
      <span className="text-xs text-gray-600">{description}</span>
    </div>
  );
}
