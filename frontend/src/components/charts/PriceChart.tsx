/** Candlestick chart (lightweight-charts v4) with signal and trade markers. */

import {
  createChart,
  type CandlestickData,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

export interface ChartBar {
  time: string; // YYYY-MM-DD
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface ChartMarker {
  time: string; // YYYY-MM-DD
  position: "aboveBar" | "belowBar";
  color: string;
  shape: "arrowUp" | "arrowDown" | "circle";
  text: string;
}

interface Props {
  bars: ChartBar[];
  markers: ChartMarker[];
  height?: number;
}

export function PriceChart({ bars, markers, height = 380 }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const chart = createChart(el, {
      height,
      autoSize: true,
      layout: { background: { color: "#111827" }, textColor: "#9ca3af" },
      grid: { vertLines: { color: "#1f2937" }, horzLines: { color: "#1f2937" } },
      timeScale: { borderColor: "#374151" },
      rightPriceScale: { borderColor: "#374151" },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#10b981",
      downColor: "#ef4444",
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
      borderVisible: false,
    });
    series.setData(bars as CandlestickData<Time>[]);
    const sorted = [...markers].sort((a, b) => a.time.localeCompare(b.time));
    series.setMarkers(sorted as SeriesMarker<Time>[]);
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [bars, markers, height]);

  return <div ref={ref} className="w-full" style={{ height }} />;
}
