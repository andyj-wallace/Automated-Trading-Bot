import type { BacktestJob, BacktestResult, PortfolioBacktestResult } from "./types";
import { BacktestResults } from "./BacktestResults";
import { PortfolioResults } from "./PortfolioResults";

// ---------------------------------------------------------------------------
// Single-strategy job status
// ---------------------------------------------------------------------------

export function SingleJobStatus({ job }: { job: BacktestJob }) {
  const statusColour = { pending: "text-gray-400", running: "text-blue-400", done: "text-emerald-400", error: "text-red-400" }[job.status];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        {(job.status === "pending" || job.status === "running") && (
          <span className="inline-block w-3 h-3 rounded-full bg-blue-500 animate-pulse" />
        )}
        <span className={`text-sm font-medium ${statusColour} capitalize`}>{job.status}</span>
        {job.symbol && (
          <span className="text-xs text-gray-600">{job.symbol} · {job.strategy_type}</span>
        )}
      </div>
      {job.status === "error" && (
        <div className="rounded border border-red-800 bg-red-900/30 px-4 py-3 text-xs text-red-400">
          {job.error}
        </div>
      )}
      {job.status === "done" && job.result && (
        <BacktestResults result={job.result as BacktestResult} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Portfolio job status
// ---------------------------------------------------------------------------

export function PortfolioJobStatus({ job }: { job: BacktestJob }) {
  const statusColour = { pending: "text-gray-400", running: "text-blue-400", done: "text-emerald-400", error: "text-red-400" }[job.status];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        {(job.status === "pending" || job.status === "running") && (
          <span className="inline-block w-3 h-3 rounded-full bg-blue-500 animate-pulse" />
        )}
        <span className={`text-sm font-medium ${statusColour} capitalize`}>{job.status}</span>
        {job.slot_count != null && (
          <span className="text-xs text-gray-600">{job.slot_count} strategy/symbol pairs</span>
        )}
      </div>
      {job.status === "error" && (
        <div className="rounded border border-red-800 bg-red-900/30 px-4 py-3 text-xs text-red-400">
          {job.error}
        </div>
      )}
      {job.status === "done" && job.result && (
        <PortfolioResults result={job.result as PortfolioBacktestResult} />
      )}
    </div>
  );
}
