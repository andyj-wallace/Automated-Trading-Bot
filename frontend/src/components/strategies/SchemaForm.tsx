/**
 * SchemaForm — renders a strategy config from its JSON schema (GET /strategies/types).
 *
 * integer/number -> number input; string -> text input (enum -> select);
 * `symbols` -> watchlist multi-select; other object/array fields -> JSON textarea.
 */

import { useState } from "react";
import type { JsonSchema, JsonSchemaProperty, WatchedSymbol } from "../../types/api";

interface Props {
  schema: JsonSchema;
  value: Record<string, unknown>;
  symbols: WatchedSymbol[];
  onChange: (next: Record<string, unknown>) => void;
  disabled?: boolean;
}

const INPUT =
  "bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-600 disabled:opacity-40";

function baseType(p: JsonSchemaProperty): string {
  const t = Array.isArray(p.type) ? p.type.find((x) => x !== "null") : p.type;
  return t ?? "string";
}

export function SchemaForm({ schema, value, symbols, onChange, disabled = false }: Props) {
  const required = new Set(schema.required ?? []);
  const entries = Object.entries(schema.properties ?? {}).filter(([k]) => k !== "symbols");
  const hasSymbols = schema.properties?.symbols !== undefined;

  function set(key: string, v: unknown) {
    const next = { ...value };
    if (v === undefined) delete next[key];
    else next[key] = v;
    onChange(next);
  }

  const assigned = Array.isArray(value.symbols) ? (value.symbols as string[]) : [];

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {entries.map(([key, prop]) => (
          <Field
            key={key}
            name={key}
            prop={prop}
            required={required.has(key)}
            value={value[key]}
            disabled={disabled}
            onChange={(v) => set(key, v)}
          />
        ))}
      </div>

      {hasSymbols && (
        <div>
          <label className="block text-xs font-medium text-gray-400 mb-2">Assigned Symbols</label>
          {symbols.length === 0 ? (
            <p className="text-xs text-gray-600">No symbols on watchlist — add some first.</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {symbols.map((sym) => {
                const selected = assigned.includes(sym.ticker);
                return (
                  <button
                    key={sym.ticker}
                    type="button"
                    disabled={disabled}
                    onClick={() =>
                      set(
                        "symbols",
                        selected
                          ? assigned.filter((s) => s !== sym.ticker)
                          : [...assigned, sym.ticker]
                      )
                    }
                    className={`px-3 py-1.5 rounded text-xs font-medium border transition-colors ${
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
        </div>
      )}
    </div>
  );
}

function Field({
  name,
  prop,
  required,
  value,
  disabled,
  onChange,
}: {
  name: string;
  prop: JsonSchemaProperty;
  required: boolean;
  value: unknown;
  disabled: boolean;
  onChange: (v: unknown) => void;
}) {
  const type = baseType(prop);
  const wide = type === "object" || type === "array";
  const label = (
    <label className="text-xs font-medium text-gray-400">
      {name}
      {required && <span className="text-red-400"> *</span>}
    </label>
  );

  let control;
  if (prop.enum) {
    control = (
      <select
        value={String(value ?? prop.default ?? "")}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className={INPUT}
      >
        {prop.enum.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    );
  } else if (type === "integer" || type === "number") {
    control = (
      <input
        type="number"
        step={type === "integer" ? 1 : "any"}
        min={prop.minimum}
        max={prop.maximum}
        value={value === undefined ? "" : String(value)}
        placeholder={prop.default !== undefined ? String(prop.default) : undefined}
        disabled={disabled}
        onChange={(e) => {
          const raw = e.target.value;
          if (raw === "") return onChange(undefined);
          onChange(type === "integer" ? parseInt(raw, 10) : Number(raw));
        }}
        className={INPUT}
      />
    );
  } else if (wide) {
    control = <JsonField value={value} disabled={disabled} onChange={onChange} />;
  } else {
    control = (
      <input
        type="text"
        value={value === undefined ? "" : String(value)}
        placeholder={prop.default !== undefined && prop.default !== null ? String(prop.default) : undefined}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
        className={INPUT}
      />
    );
  }

  return (
    <div className={`flex flex-col gap-1 ${wide ? "sm:col-span-2" : ""}`}>
      {label}
      {control}
      {prop.description && <span className="text-xs text-gray-600">{prop.description}</span>}
    </div>
  );
}

function JsonField({
  value,
  disabled,
  onChange,
}: {
  value: unknown;
  disabled: boolean;
  onChange: (v: unknown) => void;
}) {
  const [text, setText] = useState(value === undefined ? "" : JSON.stringify(value, null, 2));
  const [invalid, setInvalid] = useState(false);

  return (
    <>
      <textarea
        rows={6}
        value={text}
        disabled={disabled}
        spellCheck={false}
        onChange={(e) => {
          setText(e.target.value);
          if (e.target.value.trim() === "") {
            setInvalid(false);
            return onChange(undefined);
          }
          try {
            onChange(JSON.parse(e.target.value));
            setInvalid(false);
          } catch {
            setInvalid(true);
          }
        }}
        className={`${INPUT} font-mono ${invalid ? "border-red-600" : ""}`}
      />
      {invalid && <span className="text-xs text-red-400">Invalid JSON; not applied yet</span>}
    </>
  );
}
