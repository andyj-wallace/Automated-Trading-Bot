/**
 * Watchlist management page — Layer 10.6.
 *
 * Features:
 *   - List all watched symbols with add-date and active status
 *   - Inline "Add symbol" form with broker validation error display
 *   - Remove with confirm dialog when symbol has an open position
 *   - Rows with open positions are highlighted
 *
 * API:
 *   GET    /api/v1/symbols          — list all
 *   POST   /api/v1/symbols          — add { ticker, display_name? }
 *   DELETE /api/v1/symbols/:ticker  — remove; body { confirm: true } if open position
 *   GET    /api/v1/trades?status=OPEN — detect open positions
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api } from "../api/client";
import type { Trade, WatchedSymbol } from "../types/api";

export function Watchlist() {
  return (
    <div className="p-8 max-w-3xl">
      <h1 className="text-2xl font-bold text-white mb-1">Watchlist</h1>
      <p className="text-gray-500 text-sm mb-8">
        Add / remove symbols · manage strategy assignments
      </p>
      <AddSymbolForm />
      <SymbolList />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add symbol form
// ---------------------------------------------------------------------------

function AddSymbolForm() {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async (ticker: string) => {
      const res = await api.post<WatchedSymbol>("/symbols", { ticker });
      if (res.error) throw new Error(res.error.message);
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["symbols"] });
      setError(null);
      if (inputRef.current) inputRef.current.value = "";
    },
    onError: (err: Error) => {
      setError(err.message);
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const ticker = inputRef.current?.value.trim().toUpperCase() ?? "";
    if (!ticker) return;
    setError(null);
    mutation.mutate(ticker);
  }

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 mb-4">
      <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-3">
        Add Symbol
      </h2>
      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          ref={inputRef}
          type="text"
          placeholder="e.g. AAPL"
          maxLength={10}
          className="flex-1 bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-600 uppercase"
          style={{ textTransform: "uppercase" }}
          disabled={mutation.isPending}
        />
        <button
          type="submit"
          disabled={mutation.isPending}
          className="px-4 py-2 text-sm font-medium bg-blue-600 hover:bg-blue-700 disabled:bg-blue-900 disabled:text-blue-400 text-white rounded transition-colors"
        >
          {mutation.isPending ? "Adding…" : "Add"}
        </button>
      </form>
      {error && (
        <p className="mt-2 text-xs text-red-400">{error}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Symbol list
// ---------------------------------------------------------------------------

function SymbolList() {
  const { data: symbols = [], isLoading } = useQuery({
    queryKey: ["symbols"],
    queryFn: async () => {
      const res = await api.get<WatchedSymbol[]>("/symbols");
      return res.data ?? [];
    },
  });

  const { data: openTrades = [] } = useQuery({
    queryKey: ["trades", "OPEN"],
    queryFn: async () => {
      const res = await api.get<Trade[]>("/trades?status=OPEN");
      return res.data ?? [];
    },
  });

  if (isLoading) {
    return <p className="text-xs text-gray-600">Loading…</p>;
  }

  if (symbols.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-gray-700 bg-gray-900/50 p-12 text-center">
        <p className="text-gray-500 text-sm">No symbols on watchlist yet</p>
        <p className="text-gray-600 text-xs mt-1">Add a ticker above to start watching it</p>
      </div>
    );
  }

  const openSymbols = new Set(openTrades.map((t) => t.symbol.toUpperCase()));

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 overflow-hidden">
      <div className="px-5 py-3 border-b border-gray-800 flex items-center justify-between">
        <span className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Symbols
        </span>
        <span className="text-xs text-gray-500">{symbols.length} total</span>
      </div>
      <div className="divide-y divide-gray-800">
        {symbols.map((sym) => (
          <SymbolRow
            key={sym.id}
            symbol={sym}
            hasOpenPosition={openSymbols.has(sym.ticker.toUpperCase())}
          />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Individual symbol row
// ---------------------------------------------------------------------------

function SymbolRow({
  symbol,
  hasOpenPosition,
}: {
  symbol: WatchedSymbol;
  hasOpenPosition: boolean;
}) {
  const queryClient = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [fetchSuccess, setFetchSuccess] = useState(false);

  const fetchHistoryMutation = useMutation({
    mutationFn: async () => {
      const res = await api.post<{ ticker: string; bars_stored: number }>(
        `/symbols/${symbol.ticker}/fetch-history`,
        {}
      );
      if (res.error) throw new Error(res.error.message);
      return res.data;
    },
    onSuccess: () => {
      setFetchError(null);
      setFetchSuccess(true);
      setTimeout(() => setFetchSuccess(false), 3000);
    },
    onError: (err: Error) => {
      setFetchError(err.message);
    },
  });

  const removeMutation = useMutation({
    mutationFn: async (confirmed: boolean) => {
      const body = confirmed ? { confirm: true } : {};
      const res = await api.delete<{ ticker: string; deleted: boolean }>(
        `/symbols/${symbol.ticker}`,
        body
      );
      if (res.error) {
        if (res.error.code === "OPEN_POSITION") {
          // Backend requires confirmation
          throw Object.assign(new Error(res.error.message), {
            code: "OPEN_POSITION",
          });
        }
        throw new Error(res.error.message);
      }
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["symbols"] });
      setConfirmOpen(false);
      setRemoveError(null);
    },
    onError: (err: Error & { code?: string }) => {
      if (err.code === "OPEN_POSITION") {
        setConfirmOpen(true);
      } else {
        setRemoveError(err.message);
      }
    },
  });

  function handleRemoveClick() {
    setRemoveError(null);
    if (hasOpenPosition) {
      setConfirmOpen(true);
    } else {
      removeMutation.mutate(false);
    }
  }

  const addedAt = new Date(symbol.added_at).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });

  return (
    <>
      <div
        className={`flex items-center justify-between px-5 py-3 ${
          hasOpenPosition ? "border-l-2 border-emerald-500" : ""
        }`}
      >
        <div className="flex items-center gap-3 min-w-0">
          <span className="text-sm font-semibold text-gray-200">
            {symbol.ticker}
          </span>
          {symbol.display_name && (
            <span className="text-xs text-gray-500 truncate hidden sm:block">
              {symbol.display_name}
            </span>
          )}
          {hasOpenPosition && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-emerald-900/50 text-emerald-400 shrink-0">
              Open position
            </span>
          )}
        </div>

        <div className="flex items-center gap-4 shrink-0">
          <span className="text-xs text-gray-600 hidden sm:block">
            Added {addedAt}
          </span>
          <button
            onClick={() => fetchHistoryMutation.mutate()}
            disabled={fetchHistoryMutation.isPending}
            className="text-xs text-gray-500 hover:text-blue-400 disabled:text-gray-700 transition-colors"
          >
            {fetchHistoryMutation.isPending
              ? "Fetching…"
              : fetchSuccess
              ? "Fetched ✓"
              : "Fetch History"}
          </button>
          <button
            onClick={handleRemoveClick}
            disabled={removeMutation.isPending}
            className="text-xs text-gray-500 hover:text-red-400 disabled:text-gray-700 transition-colors"
          >
            {removeMutation.isPending ? "Removing…" : "Remove"}
          </button>
        </div>
      </div>

      {removeError && (
        <div className="px-5 pb-2">
          <p className="text-xs text-red-400">{removeError}</p>
        </div>
      )}

      {fetchError && (
        <div className="px-5 pb-2">
          <p className="text-xs text-red-400">{fetchError}</p>
        </div>
      )}

      {confirmOpen && (
        <ConfirmDialog
          ticker={symbol.ticker}
          onConfirm={() => removeMutation.mutate(true)}
          onCancel={() => setConfirmOpen(false)}
          isPending={removeMutation.isPending}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Confirm dialog overlay
// ---------------------------------------------------------------------------

function ConfirmDialog({
  ticker,
  onConfirm,
  onCancel,
  isPending,
}: {
  ticker: string;
  onConfirm: () => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="bg-gray-900 border border-gray-700 rounded-lg p-6 max-w-sm w-full mx-4 shadow-xl">
        <h3 className="text-base font-semibold text-white mb-2">
          Open Position — Confirm Removal
        </h3>
        <p className="text-sm text-gray-400 mb-6">
          <span className="text-white font-medium">{ticker}</span> has an open
          position. Removing it from the watchlist will not close the trade.
          Continue?
        </p>
        <div className="flex gap-3 justify-end">
          <button
            onClick={onCancel}
            disabled={isPending}
            className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200 disabled:text-gray-600 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={isPending}
            className="px-4 py-2 text-sm font-medium bg-red-700 hover:bg-red-600 disabled:bg-red-900 text-white rounded transition-colors"
          >
            {isPending ? "Removing…" : "Remove Anyway"}
          </button>
        </div>
      </div>
    </div>
  );
}
