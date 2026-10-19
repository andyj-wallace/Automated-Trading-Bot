/**
 * StrategyConfigForm — editable form for a strategy's config.
 *
 * The field set comes from the backend (GET /strategies/types); nothing about
 * individual strategy types is hardcoded here.
 */

import type { WatchedSymbol } from "../../types/api";
import { SchemaForm } from "./SchemaForm";
import { useStrategyTypes } from "./useStrategyTypes";

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
  const { data: types, isLoading } = useStrategyTypes();
  const schema = types?.find((t) => t.type === strategyType)?.config_schema;

  if (isLoading) return <p className="text-xs text-gray-600">Loading schema…</p>;
  if (!schema) {
    return <p className="text-xs text-gray-600">No config schema for type "{strategyType}".</p>;
  }
  return (
    <SchemaForm
      schema={schema}
      value={config}
      symbols={symbols}
      onChange={onChange}
      disabled={disabled}
    />
  );
}
