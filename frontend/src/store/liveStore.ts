/** Live event store: every WebSocket message lands here; components read selectors. */

import { create } from "zustand";
import type { DashboardEvent } from "../types/api";

export interface PriceTick {
  price: number;
  changePct: number | null;
  at: string;
}

interface LiveState {
  prices: Record<string, PriceTick>;
  tradesVersion: number;
  riskVersion: number;
  alerts: DashboardEvent[];
  socketState: number;
  applyEvent: (e: DashboardEvent) => void;
  setSocketState: (s: number) => void;
  dismissAlert: (index: number) => void;
}

export const useLiveStore = create<LiveState>((set) => ({
  prices: {},
  tradesVersion: 0,
  riskVersion: 0,
  alerts: [],
  socketState: WebSocket.CONNECTING,
  applyEvent: (e) =>
    set((s) => {
      const p = e.payload as Record<string, string | null>;
      switch (e.event) {
        case "price_update": {
          if (!p.symbol || p.price == null) return {};
          return {
            prices: {
              ...s.prices,
              [String(p.symbol).toUpperCase()]: {
                price: Number(p.price),
                changePct: p.change_pct ? Number(p.change_pct) : null,
                at: e.timestamp,
              },
            },
          };
        }
        case "trade_executed":
        case "trade_closed":
          return { tradesVersion: s.tradesVersion + 1 };
        case "risk_alert":
          return {
            riskVersion: s.riskVersion + 1,
            alerts: [e, ...s.alerts].slice(0, 50),
          };
        case "system_alert":
          return { alerts: [e, ...s.alerts].slice(0, 50) };
        default:
          return {};
      }
    }),
  setSocketState: (socketState) => set({ socketState }),
  dismissAlert: (i) =>
    set((s) => ({ alerts: s.alerts.filter((_, j) => j !== i) })),
}));
