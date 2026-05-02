// frontend/src/Chart.js
import React, { useEffect, useRef } from "react";
import { createChart } from "lightweight-charts";

function safeNumber(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function normalizeCandle(c) {
  const time = c.time ?? c.epoch ?? c.t ?? null;
  const open = safeNumber(c.open ?? c.o);
  const high = safeNumber(c.high ?? c.h);
  const low = safeNumber(c.low ?? c.l);
  const close = safeNumber(c.close ?? c.c);

  if (
    time === null ||
    open === null ||
    high === null ||
    low === null ||
    close === null
  ) {
    return null;
  }

  return { time, open, high, low, close };
}

export default function Chart({
  candles = [],
  supports = [],
  resistances = [],
  symbol = "R_10",
  timeframe = "5m",
  entry = null,
  sl = null,
  tp = null,
  tp1 = null,
  tp2 = null,
}) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const priceLinesRef = useRef([]);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth || 300,
      height: 460,
      layout: {
        background: { color: "#06141d" },
        textColor: "#d8f6ff",
      },
      grid: {
        vertLines: { color: "rgba(0,255,208,0.08)" },
        horzLines: { color: "rgba(0,255,208,0.08)" },
      },
      rightPriceScale: {
        borderColor: "rgba(0,255,208,0.16)",
      },
      timeScale: {
        borderColor: "rgba(0,255,208,0.16)",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    chartRef.current = chart;

    const candleSeries = chart.addCandlestickSeries({
      upColor: "#2dffb4",
      downColor: "#ff5f8a",
      borderUpColor: "#2dffb4",
      borderDownColor: "#ff5f8a",
      wickUpColor: "#7dffd0",
      wickDownColor: "#ff8cab",
      priceLineVisible: false,
      lastValueVisible: true,
    });

    candleSeriesRef.current = candleSeries;

    const handleResize = () => {
      if (!containerRef.current || !chartRef.current) return;

      chartRef.current.applyOptions({
        width: containerRef.current.clientWidth || 300,
      });

      chartRef.current.timeScale().fitContent();
    };

    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);

      try {
        chart.remove();
      } catch {}
    };
  }, [symbol, timeframe]);

  useEffect(() => {
    const series = candleSeriesRef.current;
    const chart = chartRef.current;

    if (!series || !chart) return;

    const formatted = (Array.isArray(candles) ? candles : [])
      .map(normalizeCandle)
      .filter(Boolean);

    series.setData(formatted);

    if (formatted.length) {
      chart.timeScale().fitContent();
    }
  }, [candles]);

  useEffect(() => {
    const series = candleSeriesRef.current;
    if (!series) return;

    for (const line of priceLinesRef.current) {
      try {
        series.removePriceLine(line);
      } catch {}
    }

    priceLinesRef.current = [];

    const addLine = (price, title, color, lineWidth = 2, lineStyle = 2) => {
      const n = safeNumber(price);
      if (n === null) return;

      try {
        const line = series.createPriceLine({
          price: n,
          color,
          lineWidth,
          lineStyle,
          axisLabelVisible: true,
          title,
        });

        priceLinesRef.current.push(line);
      } catch {}
    };

    const supportList = Array.isArray(supports) ? supports : [];
    const resistanceList = Array.isArray(resistances) ? resistances : [];

    supportList.slice(0, 3).forEach((s, i) => {
      addLine(s, `S${i + 1}`, "#1fd6ff", 1, 2);
    });

    resistanceList.slice(0, 3).forEach((r, i) => {
      addLine(r, `R${i + 1}`, "#ffd76a", 1, 2);
    });

    addLine(entry, "ENTRY", "#2dffb4", 2, 0);
    addLine(sl, "SL", "#ff5f8a", 2, 0);
    addLine(tp, "TP", "#ffd76a", 2, 0);
    addLine(tp1, "TP1", "#8affc1", 2, 1);
    addLine(tp2, "TP2", "#ffe082", 2, 1);
  }, [supports, resistances, entry, sl, tp, tp1, tp2]);

  return (
    <div
      ref={containerRef}
      aria-label={`Chart for ${symbol} ${timeframe}`}
      style={{
        width: "100%",
        height: "460px",
        borderRadius: "18px",
        overflow: "hidden",
        boxShadow: "inset 0 0 30px rgba(0,255,208,0.05)",
      }}
    />
  );
}