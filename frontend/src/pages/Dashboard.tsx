export function Dashboard() {
  return (
    <div className="p-8">
      <h1 className="text-2xl font-bold text-white mb-1">Dashboard</h1>
      <p className="text-gray-500 text-sm mb-8">
        Risk gauge · active trades · watchlist · system status
      </p>
      <PlaceholderGrid
        panels={[
          { label: "Risk Gauge",          layer: 10 },
          { label: "Active Trades Table", layer: 10 },
          { label: "Watchlist Panel",     layer: 10 },
          { label: "System Health Panel", layer: 10 },
        ]}
      />
    </div>
  );
}

interface PanelDef {
  label: string;
  layer: number;
}

function PlaceholderGrid({ panels }: { panels: PanelDef[] }) {
  return (
    <div className="grid grid-cols-2 gap-4">
      {panels.map((p) => (
        <PlaceholderPanel key={p.label} label={p.label} layer={p.layer} />
      ))}
    </div>
  );
}

function PlaceholderPanel({ label, layer }: PanelDef) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-6 flex flex-col gap-2">
      <p className="text-sm font-semibold text-gray-300">{label}</p>
      <p className="text-xs text-gray-600">Coming in Layer {layer}</p>
    </div>
  );
}
