export const STRATEGY_OPTIONS = [
  { value: "moving_average",     label: "Moving Average" },
  { value: "mean_reversion",     label: "Mean Reversion" },
  { value: "stock_trend",        label: "Stock Trend" },
  { value: "bull_bear",          label: "Bull/Bear Regime" },
  { value: "intra_week_reversion", label: "Intra-Week Reversion" },
  { value: "trend_pullback",     label: "Trend Pullback" },
];

export const FEE_OPTIONS = [
  { value: "legacy",      label: "Legacy ($1 per order)" },
  { value: "ibkr_fixed",  label: "IBKR Fixed" },
  { value: "ibkr_tiered", label: "IBKR Tiered" },
  { value: "zero",        label: "None" },
];

export const RANGE_OPTIONS = [
  { value: "1y", label: "1 Year" },
  { value: "2y", label: "2 Years" },
  { value: "5y", label: "5 Years" },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export const inputCls =
  "w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-600";
