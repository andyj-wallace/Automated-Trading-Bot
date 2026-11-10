// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-xs font-medium text-gray-400">{label}</label>
      {children}
    </div>
  );
}

export function MetricCard({ label, value, valueClass = "text-gray-200" }: {
  label: string;
  value: string;
  valueClass?: string;
}) {
  return (
    <div className="bg-gray-800/50 rounded p-3 flex flex-col gap-1">
      <span className="text-xs text-gray-500">{label}</span>
      <span className={`text-sm font-semibold tabular-nums ${valueClass}`}>{value}</span>
    </div>
  );
}
