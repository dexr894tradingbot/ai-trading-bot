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
  ) return null;

  return { time, open, high, low, close };
}

export default function Chart({
  candles = [],
  supports = [],
  resistances = [],
  symbol,
  timeframe,
  entry,
  sl,
  tp1,
  tp2,
}) {
  const ref = useRef();
  const chartRef = useRef();
  const candleRef = useRef();
  const linesRef = useRef([]);

  useEffect(() => {
    if (!ref.current) return;

    const chart = createChart(ref.current, {
      height: 460,
      layout: {
        background: { color: "#06141d" },
        textColor: "#d8f6ff",
      },
      grid: {
        vertLines: { color: "rgba(0,255,208,0.08)" },
        horzLines: { color: "rgba(0,255,208,0.08)" },
      },
    });

    chartRef.current = chart;

    const candleSeries = chart.addSeries("Candlestick", {
      upColor: "#2dffb4",
      downColor: "#ff5f8a",
    });

    candleRef.current = candleSeries;

    return () => chart.remove();
  }, [symbol, timeframe]);

  useEffect(() => {
    if (!candleRef.current) return;

    const formatted = candles.map(normalizeCandle).filter(Boolean);
    candleRef.current.setData(formatted);
  }, [candles]);

  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;

    linesRef.current.forEach((l) => {
      try { series.removePriceLine(l); } catch {}
    });
    linesRef.current = [];

    const add = (price, label, color) => {
      const p = safeNumber(price);
      if (!p) return;

      const line = series.createPriceLine({
        price: p,
        color,
        title: label,
      });

      linesRef.current.push(line);
    };

    supports.forEach((s, i) => add(s, `S${i+1}`, "#1fd6ff"));
    resistances.forEach((r, i) => add(r, `R${i+1}`, "#ffd76a"));

    add(entry, "ENTRY", "#00ff99");
    add(sl, "SL", "#ff4d6d");
    add(tp1, "TP1", "#ffd166");
    add(tp2, "TP2", "#ffe082");

  }, [supports, resistances, entry, sl, tp1, tp2]);

  return (
    <div
      ref={ref}
      style={{
        width: "100%",
        height: "460px",
        borderRadius: 12,
        overflow: "hidden",
      }}
    />
  );
}