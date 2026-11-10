/**
 * BacktestingPage — single-strategy (15.4) and portfolio mode (21.3).
 *
 * Toggle between:
 *   Single: symbol + strategy + config → POST /api/v1/backtesting/run
 *   Portfolio: N (strategy, symbol) rows → POST /api/v1/backtesting/run-portfolio
 *
 * Both poll GET /api/v1/backtesting/:id at 2s until done/error.
 */

import { PortfolioBacktestForm } from "../components/backtesting/PortfolioBacktestForm";
import { SingleBacktestForm } from "../components/backtesting/SingleBacktestForm";
import { useBacktesting } from "../components/backtesting/useBacktesting";
import { PortfolioJobStatus, SingleJobStatus } from "../components/backtesting/JobStatus";

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function Backtesting() {
  const state = useBacktesting();
  const { mode, setMode, job, portJob } = state;

  return (
    <div className="p-8 max-w-5xl flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold text-white mb-1">Backtesting</h1>
        <p className="text-gray-500 text-sm">
          Replay strategies against historical OHLCV data
        </p>
      </div>

      {/* Mode toggle */}
      <div className="flex gap-1 p-1 bg-gray-800/60 rounded-lg w-fit">
        {(["single", "portfolio"] as const).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={`px-4 py-1.5 text-sm font-medium rounded transition-colors capitalize ${
              mode === m
                ? "bg-blue-600 text-white"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            {m === "single" ? "Single Strategy" : "Portfolio"}
          </button>
        ))}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Single-strategy form                                                */}
      {/* ------------------------------------------------------------------ */}
      {mode === "single" && (
        <SingleBacktestForm {...state} />
      )}

      {mode === "single" && job && (
        <SingleJobStatus job={job} />
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Portfolio form                                                       */}
      {/* ------------------------------------------------------------------ */}
      {mode === "portfolio" && (
        <PortfolioBacktestForm {...state} />
      )}

      {mode === "portfolio" && portJob && (
        <PortfolioJobStatus job={portJob} />
      )}
    </div>
  );
}
