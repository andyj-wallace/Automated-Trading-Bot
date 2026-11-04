/**
 * CircuitBreakerBanner — daily loss halt warning.
 *
 * Shows a red banner when the daily halt is active, or an amber banner
 * when the account is close to the daily loss limit.
 */

import { useCircuitBreaker } from "../../hooks/useCircuitBreaker";

const RED = "border-red-800 bg-red-950/50 text-red-300";
const AMBER = "border-amber-800 bg-amber-950/40 text-amber-300";

export function CircuitBreakerBanner(): JSX.Element | null {
  const { data } = useCircuitBreaker();

  if (!data) return null;

  const pnl = Number(data.daily_realized_pnl);
  const threshold = Number(data.halt_threshold_amount);
  const used = threshold > 0 ? Math.max(0, -pnl) / threshold : 0;

  if (data.daily_halted) {
    return (
      <div role="alert" className={`rounded-lg border p-4 ${RED}`}>
        <p className="text-sm font-semibold">Daily loss limit hit — no new trades today</p>
        <p className="text-xs mt-1">
          Today's losses reached ${threshold.toFixed(2)} ({(Number(data.daily_loss_limit_pct) * 100).toFixed(1)}% of the account). Positions already open are still managed normally, including stops and targets. Clearing the halt is on the Home page under "Risk & system details".
        </p>
      </div>
    );
  }

  if (used >= 0.6) {
    return (
      <div role="status" className={`rounded-lg border p-4 ${AMBER}`}>
        <p className="text-sm font-semibold">Close to today's loss limit</p>
        <p className="text-xs mt-1">
          Down ${Math.abs(pnl).toFixed(2)} of a ${threshold.toFixed(2)} limit. New trades stop for the day if the limit is reached.
        </p>
      </div>
    );
  }

  return null;
}
