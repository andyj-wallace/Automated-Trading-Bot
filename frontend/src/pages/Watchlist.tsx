export function Watchlist() {
  return (
    <div className="p-8">
      <h1 className="text-2xl font-bold text-white mb-1">Watchlist</h1>
      <p className="text-gray-500 text-sm mb-8">
        Add / remove symbols · manage strategy assignments
      </p>
      <ComingSoon layer={10} />
    </div>
  );
}

function ComingSoon({ layer }: { layer: number }) {
  return (
    <div className="rounded-lg border border-dashed border-gray-700 bg-gray-900/50 p-12 text-center">
      <p className="text-gray-500 text-sm">Coming in Layer {layer}</p>
    </div>
  );
}
