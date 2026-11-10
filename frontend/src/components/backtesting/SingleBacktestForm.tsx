import { Field } from "./Field";
import { FEE_OPTIONS, STRATEGY_OPTIONS, inputCls } from "./constants";
import { NoHistoricalDataPanel } from "./NoHistoricalDataPanel";
import type { BacktestingState } from "./useBacktesting";
import { SchemaForm } from "../strategies/SchemaForm";

type SingleBacktestFormProps = Pick<
  BacktestingState,
  | "handleSingleSubmit" | "symbol" | "setSymbol" | "balance" | "setBalance"
  | "commissionPlan" | "setCommissionPlan" | "strategyType" | "setStrategyType"
  | "strategyTypes" | "setConfigEdits" | "selectedType" | "config" | "compound"
  | "setCompound" | "submitting" | "job" | "isRunning" | "submitError"
  | "submitErrorCode" | "setSubmitError" | "fetchConfirm" | "setFetchConfirm"
>;

export function SingleBacktestForm({
  handleSingleSubmit, symbol, setSymbol, balance, setBalance, commissionPlan, setCommissionPlan,
  strategyType, setStrategyType, strategyTypes, setConfigEdits, selectedType, config,
  compound, setCompound, submitting, job, isRunning, submitError, submitErrorCode,
  setSubmitError, fetchConfirm, setFetchConfirm,
}: SingleBacktestFormProps) {
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5">
      <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-4">
        Configure Backtest
      </h2>
      <form onSubmit={handleSingleSubmit} className="flex flex-col gap-4">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Field label="Symbol">
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              className={inputCls}
              placeholder="AAPL"
              maxLength={10}
              required
            />
          </Field>
          <Field label="Account Balance ($)">
            <input
              type="number"
              value={balance}
              onChange={(e) => setBalance(e.target.value)}
              className={inputCls}
              min={1}
              required
            />
          </Field>
          <Field label="Strategy">
            <select
              value={strategyType}
              onChange={(e) => { setStrategyType(e.target.value); setConfigEdits({}); }}
              className={inputCls}
            >
              {(strategyTypes.length ? strategyTypes.map((t) => t.type) : STRATEGY_OPTIONS.map((o) => o.value)).map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </Field>
          <Field label="Fees">
            <select value={commissionPlan} onChange={(e) => setCommissionPlan(e.target.value)} className={inputCls}>
              {FEE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </Field>
        </div>
        {selectedType && (
          <SchemaForm
            schema={selectedType.config_schema}
            value={config}
            symbols={[]}
            onChange={setConfigEdits}
            disabled={submitting}
          />
        )}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <label className="flex items-center gap-2 text-xs text-gray-400">
            <input type="checkbox" checked={compound} onChange={(e) => setCompound(e.target.checked)} />
            Compound gains
          </label>
          <button
            type="button"
            onClick={() => { setBalance("100"); setCommissionPlan("ibkr_fixed"); setCompound(true); }}
            className="px-3 py-1 text-xs font-medium text-blue-400 hover:text-blue-300 border border-blue-800 hover:border-blue-600 rounded transition-colors"
            title="$100 account, IBKR Fixed fees, gains compounded"
          >
            Small account preset
          </button>
          <div className="flex items-end md:col-start-4">
            <button
              type="submit"
              disabled={submitting || isRunning(job)}
              className="w-full px-4 py-2 text-sm font-medium bg-blue-600 hover:bg-blue-700 disabled:bg-blue-900 disabled:text-blue-400 text-white rounded transition-colors"
            >
              {submitting ? "Submitting…" : "Run Backtest"}
            </button>
          </div>
        </div>

        {submitError && submitErrorCode !== "NO_HISTORICAL_DATA" && (
          <p className="text-xs text-red-400">{submitError}</p>
        )}
        {submitError && submitErrorCode === "NO_HISTORICAL_DATA" && (
          <NoHistoricalDataPanel
            symbol={symbol}
            errorMessage={submitError}
            onSuccess={() => { setSubmitError(null); setFetchConfirm("History fetched — you can now run the backtest"); }}
          />
        )}
        {fetchConfirm && <p className="text-xs text-emerald-400">{fetchConfirm}</p>}
      </form>
    </div>
  );
}
