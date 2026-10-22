/** GET /trading/status, refreshed every 15 s; every caller shares one cached query. */

import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { TradingStatus } from "../types/api";

export function useTradingStatus() {
  return useQuery({
    queryKey: ["trading-status"],
    queryFn: async () => (await api.get<TradingStatus>("/trading/status")).data,
    refetchInterval: 15_000,
  });
}
