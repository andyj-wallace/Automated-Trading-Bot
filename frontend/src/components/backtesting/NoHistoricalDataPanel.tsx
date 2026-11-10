import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../api/client";

// ---------------------------------------------------------------------------
// No historical data inline panel
// ---------------------------------------------------------------------------

export function NoHistoricalDataPanel({ symbol, errorMessage, onSuccess }: {
  symbol: string;
  errorMessage: string;
  onSuccess: () => void;
}) {
  const [fetchError, setFetchError] = useState<string | null>(null);

  const fetchMutation = useMutation({
    mutationFn: async () => {
      const res = await api.post<{ ticker: string; bars_stored: number }>(
        `/symbols/${symbol.toUpperCase()}/fetch-history`,
        {}
      );
      if (res.error) throw new Error(res.error.message);
      return res.data;
    },
    onSuccess: () => { setFetchError(null); onSuccess(); },
    onError: (err: Error) => { setFetchError(err.message); },
  });

  return (
    <div className="rounded border border-red-800 bg-red-900/20 px-4 py-3 flex flex-col gap-2">
      <p className="text-xs text-red-400">{errorMessage}</p>
      <button
        type="button"
        onClick={() => fetchMutation.mutate()}
        disabled={fetchMutation.isPending}
        className="self-start px-3 py-1.5 text-xs font-medium bg-blue-600 hover:bg-blue-700 disabled:bg-blue-900 disabled:text-blue-400 text-white rounded transition-colors"
      >
        {fetchMutation.isPending ? "Fetching…" : `Fetch History for ${symbol.toUpperCase()}`}
      </button>
      {fetchError && <p className="text-xs text-red-400">{fetchError}</p>}
    </div>
  );
}
