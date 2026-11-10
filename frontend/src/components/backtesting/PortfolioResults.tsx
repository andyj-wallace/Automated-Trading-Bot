import type { PortfolioBacktestResult } from "./types";
import { MetricCard } from "./Field";

// ---------------------------------------------------------------------------
// Portfolio results
// ---------------------------------------------------------------------------

export function PortfolioResults({ result }: { result: PortfolioBacktestResult }) {
  const m = result.combined_metrics;
  const totalReturn = parseFloat(m.total_return);

  return (
    <div className="flex flex-col gap-4">
      {/* Combined KPIs */}
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-4">
          Combined Portfolio Results
          <span className="ml-2 font-normal normal-case text-gray-600">
            {new Date(result.start_time).toLocaleDateString()} → {new Date(result.end_time).toLocaleDateString()}
          </span>
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MetricCard label="Total Return" value={`${totalReturn >= 0 ? "+" : ""}$${Math.abs(totalReturn).toLocaleString("en-US", { minimumFractionDigits: 2 })}`} valueClass={totalReturn >= 0 ? "text-emerald-400" : "text-red-400"} />
          <MetricCard label="Return %" value={`${m.total_return_pct >= 0 ? "+" : ""}${m.total_return_pct.toFixed(2)}%`} valueClass={m.total_return_pct >= 0 ? "text-emerald-400" : "text-red-400"} />
          <MetricCard label="Win Rate" value={`${m.win_rate_pct.toFixed(1)}%`} valueClass={m.win_rate_pct >= 50 ? "text-emerald-400" : "text-amber-400"} />
          <MetricCard label="Trades" value={`${m.win_count}W / ${m.loss_count}L`} />
          <MetricCard label="Max Drawdown" value={`${m.max_drawdown_pct.toFixed(2)}%`} valueClass="text-red-400" />
          <MetricCard label="Sharpe Ratio" value={m.sharpe_ratio.toFixed(3)} />
          <MetricCard label="Ending Balance" value={`$${parseFloat(m.ending_balance).toLocaleString("en-US", { minimumFractionDigits: 2 })}`} />
          <MetricCard label="Fees Paid" value={`$${parseFloat(m.total_commission_paid).toFixed(2)}`} />
          <MetricCard label="Signals" value={`${m.signals_generated} generated`} />
          <MetricCard label="Rejected" value={`${m.signals_rejected} by risk`} valueClass={m.signals_rejected > 0 ? "text-amber-400" : "text-gray-200"} />
        </div>
      </div>

      {/* Equity curve */}
      {result.combined_equity_curve.length > 1 && (
        <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">
            Combined Equity Curve
          </h3>
          <EquityCurve curve={result.combined_equity_curve} />
        </div>
      )}

      {/* Per-strategy breakdown */}
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">
          Per-Strategy Breakdown
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-gray-500 border-b border-gray-800">
                <th className="text-left pb-2 font-medium">Symbol</th>
                <th className="text-left pb-2 font-medium">Strategy</th>
                <th className="text-right pb-2 font-medium">Trades</th>
                <th className="text-right pb-2 font-medium">Win Rate</th>
                <th className="text-right pb-2 font-medium">Total P&L</th>
                <th className="text-right pb-2 font-medium">Max DD</th>
                <th className="text-right pb-2 font-medium">Sharpe</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/50">
              {result.per_strategy.map((ps, idx) => {
                const pm = ps.metrics;
                const pnl = pm ? parseFloat(pm.total_return) : 0;
                return (
                  <tr key={idx} className="hover:bg-gray-800/30">
                    <td className="py-2 font-medium text-gray-200">{ps.symbol}</td>
                    <td className="py-2 text-gray-400 capitalize">
                      {ps.strategy_type.replace(/_/g, " ")}
                    </td>
                    <td className="py-2 text-right text-gray-300 tabular-nums">
                      {pm ? `${pm.win_count}W / ${pm.loss_count}L` : "—"}
                    </td>
                    <td className={`py-2 text-right tabular-nums ${pm && pm.win_rate_pct >= 50 ? "text-emerald-400" : "text-amber-400"}`}>
                      {pm ? `${pm.win_rate_pct.toFixed(1)}%` : "—"}
                    </td>
                    <td className={`py-2 text-right tabular-nums font-medium ${pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {pm ? `${pnl >= 0 ? "+" : ""}$${Math.abs(pnl).toLocaleString("en-US", { minimumFractionDigits: 2 })}` : "—"}
                    </td>
                    <td className="py-2 text-right text-gray-400 tabular-nums">
                      {pm ? `${pm.max_drawdown_pct.toFixed(2)}%` : "—"}
                    </td>
                    <td className="py-2 text-right text-gray-400 tabular-nums">
                      {pm ? pm.sharpe_ratio.toFixed(3) : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Equity curve SVG
// ---------------------------------------------------------------------------

function EquityCurve({ curve }: { curve: [string, string][] }) {
  const W = 600;
  const H = 80;
  const PAD = 4;

  const values = curve.map(([, v]) => parseFloat(v));
  const minV = Math.min(0, ...values);
  const maxV = Math.max(0, ...values);
  const range = maxV - minV || 1;

  const points = values.map((v, i) => {
    const x = PAD + ((i / (values.length - 1)) * (W - PAD * 2));
    const y = H - PAD - ((v - minV) / range) * (H - PAD * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  // Zero-line Y position
  const zeroY = H - PAD - ((0 - minV) / range) * (H - PAD * 2);
  const finalPositive = values[values.length - 1] >= 0;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-20" preserveAspectRatio="none">
      {/* Zero line */}
      <line
        x1={PAD} y1={zeroY.toFixed(1)}
        x2={W - PAD} y2={zeroY.toFixed(1)}
        stroke="#374151" strokeWidth="1" strokeDasharray="3 3"
      />
      {/* Equity line */}
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke={finalPositive ? "#34d399" : "#f87171"}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}
