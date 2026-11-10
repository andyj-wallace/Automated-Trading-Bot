import { useState, useEffect, useRef } from "react";
import { api } from "../../api/client";
import { defaultsFromSchema } from "../strategies/schemaDefaults";
import { useStrategyTypes } from "../strategies/useStrategyTypes";
import type { BacktestJob, SlotRow } from "./types";

export function useBacktesting() {
  const [mode, setMode] = useState<"single" | "portfolio">("single");

  // ---- Single-strategy state ----
  const [symbol, setSymbol] = useState("AAPL");
  const { data: strategyTypes = [] } = useStrategyTypes();
  const [strategyType, setStrategyType] = useState("moving_average");
  // Edits on top of the selected type's schema defaults; reset when the type changes.
  const [configEdits, setConfigEdits] = useState<Record<string, unknown>>({});
  const selectedType = strategyTypes.find((t) => t.type === strategyType);
  const config = { ...(selectedType ? defaultsFromSchema(selectedType.config_schema) : {}), ...configEdits };
  const [balance, setBalance] = useState("100000");
  const [commissionPlan, setCommissionPlan] = useState("legacy");
  const [compound, setCompound] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitErrorCode, setSubmitErrorCode] = useState<string | null>(null);
  const [fetchConfirm, setFetchConfirm] = useState<string | null>(null);
  const [job, setJob] = useState<BacktestJob | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ---- Portfolio state ----
  const [slots, setSlots] = useState<SlotRow[]>([
    { id: 1, symbol: "AAPL", strategyType: "moving_average" },
    { id: 2, symbol: "MSFT", strategyType: "mean_reversion" },
  ]);
  const [nextSlotId, setNextSlotId] = useState(3);
  const [portBalance, setPortBalance] = useState("100000");
  const [portRange, setPortRange] = useState("5y");
  const [portCommissionPlan, setPortCommissionPlan] = useState("legacy");
  const [portCompound, setPortCompound] = useState(false);
  const [portSubmitting, setPortSubmitting] = useState(false);
  const [portError, setPortError] = useState<string | null>(null);
  const [portJob, setPortJob] = useState<BacktestJob | null>(null);
  const portPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Single-strategy polling
  useEffect(() => {
    if (!job || job.status === "done" || job.status === "error") {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      return;
    }
    pollRef.current = setInterval(async () => {
      const res = await api.get<BacktestJob>(`/backtesting/${job.job_id}`);
      if (res.data) setJob(res.data);
    }, 2000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.job_id, job?.status]);

  // Portfolio polling
  useEffect(() => {
    if (!portJob || portJob.status === "done" || portJob.status === "error") {
      if (portPollRef.current) { clearInterval(portPollRef.current); portPollRef.current = null; }
      return;
    }
    portPollRef.current = setInterval(async () => {
      const res = await api.get<BacktestJob>(`/backtesting/${portJob.job_id}`);
      if (res.data) setPortJob(res.data);
    }, 2000);
    return () => { if (portPollRef.current) clearInterval(portPollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [portJob?.job_id, portJob?.status]);

  async function handleSingleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitError(null);
    setSubmitErrorCode(null);
    setFetchConfirm(null);
    setJob(null);
    setSubmitting(true);
    try {
      const res = await api.post<BacktestJob>("/backtesting/run", {
        symbol: symbol.toUpperCase(),
        strategy_type: strategyType,
        strategy_config: config,
        account_balance: balance,
        commission_plan: commissionPlan,
        compound,
      });
      if (res.error) { setSubmitError(res.error.message); setSubmitErrorCode(res.error.code); return; }
      if (res.data) setJob(res.data);
    } catch (err: unknown) {
      setSubmitError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setSubmitting(false);
    }
  }

  async function handlePortfolioSubmit(e: React.FormEvent) {
    e.preventDefault();
    setPortError(null);
    setPortJob(null);
    setPortSubmitting(true);
    try {
      const res = await api.post<BacktestJob>("/backtesting/run-portfolio", {
        slots: slots.map((s) => ({
          symbol: s.symbol.toUpperCase(),
          strategy_type: s.strategyType,
          config: {},
        })),
        account_balance: portBalance,
        range: portRange,
        commission_plan: portCommissionPlan,
        compound: portCompound,
      });
      if (res.error) { setPortError(res.error.message); return; }
      if (res.data) setPortJob(res.data);
    } catch (err: unknown) {
      setPortError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setPortSubmitting(false);
    }
  }

  function addSlot() {
    setSlots((prev) => [...prev, { id: nextSlotId, symbol: "", strategyType: "moving_average" }]);
    setNextSlotId((n) => n + 1);
  }

  function removeSlot(id: number) {
    setSlots((prev) => prev.filter((s) => s.id !== id));
  }

  function updateSlot(id: number, field: keyof SlotRow, value: string) {
    setSlots((prev) => prev.map((s) => s.id === id ? { ...s, [field]: value } : s));
  }

  const isRunning = (j: BacktestJob | null) =>
    j?.status === "pending" || j?.status === "running";

  return {
    mode, setMode, job, portJob, isRunning,
    symbol, setSymbol, balance, setBalance, commissionPlan, setCommissionPlan,
    strategyType, setStrategyType, strategyTypes, selectedType, config, setConfigEdits,
    compound, setCompound, submitting, submitError, submitErrorCode, setSubmitError,
    fetchConfirm, setFetchConfirm, handleSingleSubmit,
    slots, addSlot, removeSlot, updateSlot,
    portBalance, setPortBalance, portCommissionPlan, setPortCommissionPlan,
    portCompound, setPortCompound, portRange, setPortRange,
    portSubmitting, portError, handlePortfolioSubmit,
  };
}

export type BacktestingState = ReturnType<typeof useBacktesting>;
