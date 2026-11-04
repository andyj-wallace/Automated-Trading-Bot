/** GET /risk/circuit-breaker, refreshed every 30 s; invalidated on risk or trade events. */

import { useEffect } from "react";
import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { api } from "../api/client";
import { useLiveStore } from "../store/liveStore";
import type { CircuitBreakerStatus } from "../types/api";

export function useCircuitBreaker(): UseQueryResult<CircuitBreakerStatus, Error> {
  const queryClient = useQueryClient();
  const riskVersion = useLiveStore((s) => s.riskVersion);
  const tradesVersion = useLiveStore((s) => s.tradesVersion);

  useEffect(() => {
    if (riskVersion + tradesVersion > 0) {
      queryClient.invalidateQueries({ queryKey: ["circuit-breaker"] });
    }
  }, [riskVersion, tradesVersion, queryClient]);

  return useQuery({
    queryKey: ["circuit-breaker"],
    queryFn: async () => {
      const res = await api.get<CircuitBreakerStatus>("/risk/circuit-breaker");
      if (res.error) throw new Error(res.error.message);
      if (!res.data) throw new Error("No circuit breaker data");
      return res.data;
    },
    refetchInterval: 30_000,
  });
}
