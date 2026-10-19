/** Dialog to create a strategy: type picker, name, schema-driven config -> POST /strategies. */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import type { Strategy, StrategyCreate, WatchedSymbol } from "../../types/api";
import { SchemaForm } from "./SchemaForm";
import { defaultsFromSchema } from "./schemaDefaults";
import { useStrategyTypes } from "./useStrategyTypes";

interface Props {
  symbols: WatchedSymbol[];
  onClose: () => void;
}

export function NewStrategyDialog({ symbols, onClose }: Props) {
  const queryClient = useQueryClient();
  const { data: types = [], isLoading } = useStrategyTypes();
  const [type, setType] = useState("");
  const [name, setName] = useState("");
  const [config, setConfig] = useState<Record<string, unknown>>({});

  const selected = types.find((t) => t.type === type);

  const create = useMutation({
    mutationFn: async (body: StrategyCreate) => {
      const res = await api.post<Strategy>("/strategies", body);
      if (res.error) throw new Error(res.error.message);
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["strategies"] });
      onClose();
    },
  });

  function pickType(next: string) {
    setType(next);
    const t = types.find((x) => x.type === next);
    setConfig(t ? defaultsFromSchema(t.config_schema) : {});
    create.reset();
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-lg border border-gray-700 bg-gray-900 p-5 flex flex-col gap-4">
        <h3 className="text-sm font-semibold text-gray-100">New strategy</h3>

        {isLoading && <p className="text-xs text-gray-600">Loading types…</p>}

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div className="flex flex-col gap-1">
            <label className="text-xs font-medium text-gray-400">Type</label>
            <select
              value={type}
              onChange={(e) => pickType(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200"
            >
              <option value="">Select a type…</option>
              {types.map((t) => (
                <option key={t.type} value={t.type}>
                  {t.type}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-xs font-medium text-gray-400">Name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200"
            />
          </div>
        </div>

        {selected && (
          <SchemaForm
            schema={selected.config_schema}
            value={config}
            symbols={symbols}
            onChange={setConfig}
            disabled={create.isPending}
          />
        )}

        {create.error && <p className="text-xs text-red-400">{create.error.message}</p>}

        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200">
            Cancel
          </button>
          <button
            disabled={!selected || name.trim() === "" || create.isPending}
            onClick={() =>
              create.mutate({ name: name.trim(), type, config, is_enabled: false })
            }
            className="px-4 py-1.5 text-sm font-medium bg-blue-600 hover:bg-blue-700 disabled:bg-blue-900 disabled:text-blue-400 text-white rounded"
          >
            {create.isPending ? "Creating…" : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
