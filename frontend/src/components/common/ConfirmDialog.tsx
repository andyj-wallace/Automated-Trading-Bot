/** Minimal modal confirm dialog, optionally requiring a typed phrase. */

import { useState, type ReactNode } from "react";

interface Props {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  requirePhrase?: string;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  requirePhrase,
  busy = false,
  error,
  onConfirm,
  onCancel,
}: Props) {
  const [typed, setTyped] = useState("");
  const blocked = busy || (requirePhrase !== undefined && typed !== requirePhrase);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-lg border border-gray-700 bg-gray-900 p-5 flex flex-col gap-4">
        <h3 className="text-sm font-semibold text-gray-100">{title}</h3>
        <div className="text-sm text-gray-400">{children}</div>
        {requirePhrase !== undefined && (
          <input
            autoFocus
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={`Type ${requirePhrase} to confirm`}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-red-600"
          />
        )}
        {error && <p className="text-xs text-red-400">{error}</p>}
        <div className="flex justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={busy}
            className="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={blocked}
            className="px-3 py-1.5 text-sm font-medium bg-red-600 hover:bg-red-700 disabled:bg-red-950 disabled:text-red-800 text-white rounded"
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
