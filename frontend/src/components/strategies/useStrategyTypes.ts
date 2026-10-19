import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { StrategyType } from "../../types/api";

export function useStrategyTypes() {
  return useQuery({
    queryKey: ["strategy-types"],
    queryFn: async () => (await api.get<StrategyType[]>("/strategies/types")).data ?? [],
    staleTime: 5 * 60_000,
  });
}
