import React, { useEffect, useRef } from "react";
import { createChart, CandlestickSeries } from "lightweight-charts";

export default function Chart({
  candles = [],
  support = null,
  resistance = null,
  entry = null,
  sl = null,
  tp = null,
}) {
  const containerRef = useRef(null);

  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const priceLinesRef = useRef([]); // keep track of created lines so we can remove safely

  // 1) Create chart + series ONCE
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 420,
      layout: {
        background: { type: "solid", color: "#0f172a" },
        textColor: "#cbd5e1",
      },
      grid: {
        vertLines: { visible: false },
        horzLines: { visible: false },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
    });

    chartRef.current = chart;

    // ✅ Compatibility: old vs new lightweight-charts API
    let series;
    if (typeof chart.addCandlestickSeries === "function") {
      // OLD API (v3/v4)
      series = chart.addCandlestickSeries();
    } else if (typeof chart.addSeries === "function") {
      // NEW API (v5+)
      series = chart.addSeries(CandlestickSeries);
    } else {
      console.error("No supported candlestick series API found.");
      return;
    }

    candleSeriesRef.current = series;

    // handle resize
    const onResize = () => {
      if (!containerRef.current || !chartRef.current) return;
      chartRef.current.applyOptions({ width: containerRef.current.clientWidth });
    };
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      try {
        chart.remove();
      } catch {}
    };
  }, []);

  // 2) Update candles
  useEffect(() => {
    const series = candleSeriesRef.current;
    if (!series) return;

    const arr = Array.isArray(candles) ? candles : [];

    // Convert candle format safely:
    const formatted = arr
      .map((c) => ({
        time: c.time ?? c.t, // must be unix seconds or business day object
        open: Number(c.open ?? c.o),
        high: Number(c.high ?? c.h),
        low: Number(c.low ?? c.l),
        close: Number(c.close ?? c.c),
      }))
      .filter(
        (c) =>
          c.time !== undefined &&
          Number.isFinite(c.open) &&
          Number.isFinite(c.high) &&
          Number.isFinite(c.low) &&
          Number.isFinite(c.close)
      );

    series.setData(formatted);
  }, [candles]);

  // 3) Update price lines (S/R/Entry/SL/TP)
  useEffect(() => {
    const series = candleSeriesRef.current;
    if (!series) return;

    // remove old lines
    for (const line of priceLinesRef.current) {
      try {
        series.removePriceLine(line);
      } catch {}
    }
    priceLinesRef.current = [];

    const addLine = (price, label) => {
      if (price === null || price === undefined) return;
      const n = Number(price);
      if (!Number.isFinite(n)) return;

      const line = series.createPriceLine({
        price: n,
        color: "#94a3b8",
        lineWidth: 1,
        title: label,
      });

      priceLinesRef.current.push(line);
    };

    addLine(support, "S");
    addLine(resistance, "R");
    addLine(entry, "ENTRY");
    addLine(sl, "SL");
    addLine(tp, "TP");
  }, [support, resistance, entry, sl, tp]);

  return (
    <div
      ref={containerRef}
      style={{
        width: "100%",
        height: "420px",
        borderRadius: "16px",
        overflow: "hidden",
      }}
    />
  );
}