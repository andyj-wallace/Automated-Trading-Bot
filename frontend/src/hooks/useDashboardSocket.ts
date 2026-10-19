/**
 * useDashboardSocket — connects to /ws/dashboard, reconnects on drop, and
 * pushes every message into the live store. Mount once, in Layout.
 */

import { useEffect } from "react";
import { useLiveStore } from "../store/liveStore";
import type { DashboardEvent } from "../types/api";

const WS_PATH = "/ws/dashboard";
const RECONNECT_DELAY_MS = 3000;

export function useDashboardSocket(): void {
  useEffect(() => {
    let disposed = false;
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const { applyEvent, setSocketState } = useLiveStore.getState();

    const connect = () => {
      if (disposed) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      ws = new WebSocket(`${protocol}//${window.location.host}${WS_PATH}`);
      setSocketState(WebSocket.CONNECTING);

      ws.onopen = () => setSocketState(WebSocket.OPEN);
      ws.onmessage = (evt: MessageEvent<string>) => {
        try {
          applyEvent(JSON.parse(evt.data) as DashboardEvent);
        } catch {
          // ignore non-JSON frames
        }
      };
      ws.onclose = () => {
        if (disposed) return;
        setSocketState(WebSocket.CLOSED);
        timer = setTimeout(connect, RECONNECT_DELAY_MS);
      };
      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      disposed = true;
      if (timer !== null) clearTimeout(timer);
      ws?.close();
    };
  }, []);
}
