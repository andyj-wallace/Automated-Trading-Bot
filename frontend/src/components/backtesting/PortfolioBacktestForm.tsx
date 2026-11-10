import { Field } from "./Field";
import { FEE_OPTIONS, RANGE_OPTIONS, STRATEGY_OPTIONS, inputCls } from "./constants";
import type { BacktestingState } from "./useBacktesting";

type PortfolioBacktestFormProps = Pick<
  BacktestingState,
  | "handlePortfolioSubmit" | "slots" | "addSlot" | "removeSlot" | "updateSlot"
  | "portBalance" | "setPortBalance" | "portCommissionPlan" | "setPortCommissionPlan"
  | "portCompound" | "setPortCompound" | "portRange" | "setPortRange"
  | "portSubmitting" | "portJob" | "isRunning" | "portError"
>;

export function PortfolioBacktestForm({
  handlePortfolioSubmit, slots, addSlot, removeSlot, updateSlot, portBalance, setPortBalance,
  portCommissionPlan, setPortCommissionPlan, portCompound, setPortCompound, portRange,
  setPortRange, portSubmitting, portJob, isRunning, portError,
}: PortfolioBacktestFormProps) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
      <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-4">
        Configure Portfolio Backtest
      </h2>
      <form onSubmit={handlePortfolioSubmit} className="flex flex-col gap-4">
        {/* Slot rows */}
        <div className="flex flex-col gap-2">
          <div className="grid grid-cols-12 gap-2 text-xs font-medium text-gray-500 uppercase tracking-wide px-1">
            <span className="col-span-4">Symbol</span>
            <span className="col-span-7">Strategy</span>
          </div>
          {slots.map((slot) => (
            <div key={slot.id} className="grid grid-cols-12 gap-2 items-center">
              <input
                value={slot.symbol}
                onChange={(e) => updateSlot(slot.id, "symbol", e.target.value.toUpperCase())}
                className={`col-span-4 ${inputCls}`}
                placeholder="AAPL"
                maxLength={10}
                required
              />
              <select
                value={slot.strategyType}
                onChange={(e) => updateSlot(slot.id, "strategyType", e.target.value)}
                className={`col-span-7 ${inputCls}`}
              >
                {STRATEGY_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => removeSlot(slot.id)}
                disabled={slots.length === 1}
                className="col-span-1 text-gray-600 hover:text-red-400 disabled:opacity-30 text-lg leading-none transition-colors"
                title="Remove row"
              >
                ×
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={addSlot}
            disabled={slots.length >= 10}
            className="self-start mt-1 px-3 py-1 text-xs font-medium text-blue-400 hover:text-blue-300 disabled:text-gray-600 border border-blue-800 hover:border-blue-600 disabled:border-gray-700 rounded transition-colors"
          >
            + Add slot
          </button>
        </div>

        {/* Global settings */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 pt-2 border-t border-gray-800">
          <Field label="Account Balance ($)">
            <input
              type="number"
              value={portBalance}
              onChange={(e) => setPortBalance(e.target.value)}
              className={inputCls}
              min={1}
              required
            />
          </Field>
          <Field label="Fees">
            <select value={portCommissionPlan} onChange={(e) => setPortCommissionPlan(e.target.value)} className={inputCls}>
              {FEE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </Field>
          <label className="flex items-end gap-2 pb-2 text-xs text-gray-400">
            <input type="checkbox" checked={portCompound} onChange={(e) => setPortCompound(e.target.checked)} />
            Compound gains
          </label>
          <Field label="Historical Range">
            <select
              value={portRange}
              onChange={(e) => setPortRange(e.target.value)}
              className={inputCls}
            >
              {RANGE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </Field>
          <div className="flex items-end md:col-span-4">
            <button
              type="submit"
              disabled={portSubmitting || isRunning(portJob) || slots.some((s) => !s.symbol)}
              className="w-full px-4 py-2 text-sm font-medium bg-blue-600 hover:bg-blue-700 disabled:bg-blue-900 disabled:text-blue-400 text-white rounded transition-colors"
            >
              {portSubmitting ? "Submitting…" : `Run Portfolio Backtest (${slots.length} slots)`}
            </button>
          </div>
        </div>

        {portError && (
          <p className="text-xs text-red-400">{portError}</p>
        )}
      </form>
    </div>
  );
}
