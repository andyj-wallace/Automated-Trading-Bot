import type { BacktestResult } from "./types";
import { MetricCard } from "./Field";

// ---------------------------------------------------------------------------
// Single-strategy results
// ---------------------------------------------------------------------------

export function BacktestResults({ result }: { result: BacktestResult }) {
  const m = result.metrics;
  if (!m) return null;
  const totalReturn = parseFloat(m.total_return);

  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-4">
          Results — {result.symbol}{" "}
          <span className="font-normal normal-case text-gray-600">
            ({new Date(result.start_time).toLocaleDateString()} → {new Date(result.end_time).toLocaleDateString()})
          </span>
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MetricCard label="Total Return" value={`${totalReturn >= 0 ? "+" : ""}$${Math.abs(totalReturn).toLocaleString("en-US", { minimumFractionDigits: 2 })}`} valueClass={totalReturn >= 0 ? "text-emerald-400" : "text-red-400"} />
          <MetricCard label="Return %" value={`${m.total_return_pct >= 0 ? "+" : ""}${m.total_return_pct.toFixed(2)}%`} valueClass={m.total_return_pct >= 0 ? "text-emerald-400" : "text-red-400"} />
          <MetricCard label="Win Rate" value={`${m.win_rate_pct.toFixed(1)}%`} valueClass={m.win_rate_pct >= 50 ? "text-emerald-400" : "text-amber-400"} />
          <MetricCard label="Trades" value={`${m.win_count}W / ${m.loss_count}L`} />
          <MetricCard label="Avg Trade P&L" value={`$${parseFloat(m.avg_trade_pnl).toFixed(2)}`} />
          <MetricCard label="Ending Balance" value={`$${parseFloat(m.ending_balance).toLocaleString("en-US", { minimumFractionDigits: 2 })}`} />
          <MetricCard label="Fees Paid" value={`$${parseFloat(m.total_commission_paid).toFixed(2)}`} />
          <MetricCard label="Max Drawdown" value={`${m.max_drawdown_pct.toFixed(2)}%`} valueClass="text-red-400" />
          <MetricCard label="Sharpe Ratio" value={m.sharpe_ratio.toFixed(3)} />
          <MetricCard label="Bars Tested" value={String(m.bars_tested)} />
        </div>
      </div>

      <div className="flex gap-4 text-xs text-gray-500">
        <span>Signals generated: <span className="text-gray-300">{m.signals_generated}</span></span>
        <span>Rejected by risk: <span className="text-amber-400">{m.signals_rejected}</span></span>
        <span>Executed: <span className="text-gray-300">{m.trade_count}</span></span>
      </div>

      {result.trades.length > 0 && (
        <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-3">
            Trade Log ({result.trades.length})
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left pb-2 font-medium">Entry Date</th>
                  <th className="text-right pb-2 font-medium">Entry $</th>
                  <th className="text-right pb-2 font-medium">Exit $</th>
                  <th className="text-right pb-2 font-medium">Qty</th>
                  <th className="text-right pb-2 font-medium">P&L</th>
                  <th className="text-left pb-2 font-medium pl-3">Exit Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/50">
                {result.trades.map((t) => {
                  const pnl = t.pnl ? parseFloat(t.pnl) : null;
                  return (
                    <tr key={t.trade_id} className="hover:bg-gray-800/30">
                      <td className="py-1.5 text-gray-400">
                        {new Date(t.entry_time).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit" })}
                      </td>
                      <td className="py-1.5 text-right text-gray-300 tabular-nums">${parseFloat(t.entry_price).toFixed(2)}</td>
                      <td className="py-1.5 text-right text-gray-300 tabular-nums">{t.exit_price ? `$${parseFloat(t.exit_price).toFixed(2)}` : "—"}</td>
                      <td className="py-1.5 text-right text-gray-400 tabular-nums">{t.quantity}</td>
                      <td className={`py-1.5 text-right tabular-nums font-medium ${pnl === null ? "text-gray-500" : pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                        {pnl === null ? "—" : `${pnl >= 0 ? "+" : ""}$${Math.abs(pnl).toFixed(2)}`}
                      </td>
                      <td className="py-1.5 pl-3 text-gray-500 capitalize">
                        {t.exit_reason?.toLowerCase().replace("_", " ") ?? "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
