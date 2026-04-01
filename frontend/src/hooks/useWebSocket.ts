/**
 * useWebSocket — connects to /ws/dashboard and reconnects on drop.
 *
 * The Vite dev proxy forwards /ws/* → ws://localhost:8000/ws/*
 *
 * Usage:
 *   const { lastEvent, readyState } = useWebSocket();
 *
 *   useEffect(() => {
 *     if (lastEvent?.event === "price_update") { ... }
 *   }, [lastEvent]);
 *
 * readyState mirrors the WebSocket.readyState constants:
 *   0 = CONNECTING, 1 = OPEN, 2 = CLOSING, 3 = CLOSED
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { DashboardEvent } from "../types/api";

const WS_PATH = "/ws/dashboard";
const RECONNECT_DELAY_MS = 3000;

export interface UseWebSocketResult {
  lastEvent: DashboardEvent | null;
  readyState: number;
}

export function useWebSocket(): UseWebSocketResult {
  const [lastEvent, setLastEvent] = useState<DashboardEvent | null>(null);
  const [readyState, setReadyState] = useState<number>(WebSocket.CONNECTING);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}${WS_PATH}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (mountedRef.current) setReadyState(WebSocket.OPEN);
    };

    ws.onmessage = (evt: MessageEvent<string>) => {
      if (!mountedRef.current) return;
      try {
        const data = JSON.parse(evt.data) as DashboardEvent;
        setLastEvent(data);
      } catch {
        // ignore non-JSON frames
      }
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      setReadyState(WebSocket.CLOSED);
      // Schedule reconnect
      reconnectRef.current = setTimeout(connect, RECONNECT_DELAY_MS);
    };

    ws.onerror = () => {
      // onerror is always followed by onclose; close triggers reconnect
      ws.close();
    };
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    connect();

    return () => {
      mountedRef.current = false;
      if (reconnectRef.current !== null) clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { lastEvent, readyState };
}
