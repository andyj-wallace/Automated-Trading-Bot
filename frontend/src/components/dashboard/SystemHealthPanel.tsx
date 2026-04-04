/**
 * SystemHealthPanel — polls GET /api/v1/system/health every 60 s.
 *
 * Shows a coloured dot + label for broker, database, and Redis.
 * Overall banner turns red when any component is degraded.
 */

import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { SystemHealth } from "../../types/api";

export function SystemHealthPanel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["system-health"],
    queryFn: async () => {
      const res = await api.get<SystemHealth>("/system/health");
      return res.data;
    },
    refetchInterval: 60_000,
    retry: false,
  });

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-5 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          System Health
        </h2>
        {data && (
          <span
            className={`text-xs font-medium px-2 py-0.5 rounded-full ${
              data.status === "ok"
                ? "bg-emerald-900/50 text-emerald-400"
                : "bg-red-900/50 text-red-400"
            }`}
          >
            {data.status === "ok" ? "All Systems Operational" : "Degraded"}
          </span>
        )}
      </div>

      {isLoading && (
        <p className="text-xs text-gray-600">Checking components…</p>
      )}

      {isError && (
        <p className="text-xs text-red-500">Health check unreachable</p>
      )}

      {data && (
        <div className="grid grid-cols-3 gap-3">
          <ComponentRow label="Broker" status={data.broker} />
          <ComponentRow label="Database" status={data.database} />
          <ComponentRow label="Redis" status={data.redis} />
        </div>
      )}
    </div>
  );
}

function ComponentRow({
  label,
  status,
}: {
  label: string;
  status: { status: "ok" | "error" | "disconnected"; detail: string | null };
}) {
  const colour =
    status.status === "ok"
      ? "bg-emerald-500"
      : status.status === "disconnected"
        ? "bg-amber-500"
        : "bg-red-500";

  const textColour =
    status.status === "ok"
      ? "text-emerald-400"
      : status.status === "disconnected"
        ? "text-amber-400"
        : "text-red-400";

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1.5">
        <span className={`inline-block w-2 h-2 rounded-full ${colour}`} />
        <span className="text-xs text-gray-400">{label}</span>
      </div>
      <span className={`text-xs font-medium ${textColour} capitalize`}>
        {status.status}
      </span>
      {status.detail && (
        <span className="text-xs text-gray-600 truncate" title={status.detail}>
          {status.detail}
        </span>
      )}
    </div>
  );
}
