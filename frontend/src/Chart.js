// src/Chart.js
import React, { useEffect, useMemo, useRef } from "react";
import { createChart, CandlestickSeries } from "lightweight-charts";

const Chart = ({ symbol, allData }) => {
  const signal = allData?.[symbol]?.signal;
  

let confidencePct = 0;


if (signal) {
  const raw = signal.confidence ?? 0;
  confidencePct = raw <= 1 ? Math.round(raw * 100) : Math.round(raw);
  
}

  const chartContainerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);

  // keep references to lines so we can remove them before redrawing
  const supportLinesRef = useRef([]);
  const resistanceLinesRef = useRef([]);
  const entryLineRef = useRef(null);
  const slLineRef = useRef(null);
  const tpLineRef = useRef(null);

 

  // chart options (memo so it doesn't recreate every render)
  const chartOptions = useMemo(
    () => ({
      height: 320,
      layout: {
        background: { color: "#050914" },
        textColor: "#ffffff",
      },
      grid: {
        vertLines: { color: "#334158" },
        horzLines: { color: "#334158" },
      },
      rightPriceScale: { borderColor: "#334158" },
      timeScale: { borderColor: "#334158" },
    }),
    []
  );

  // 1) Create chart once (or when chartOptions changes)
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      width: chartContainerRef.current.clientWidth,
      ...chartOptions,
    });

    // support both APIs depending on your lightweight-charts version
    const candleSeries =
      typeof chart.addCandlestickSeries === "function"
        ? chart.addCandlestickSeries()
        : chart.addSeries(CandlestickSeries);

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;

    const handleResize = () => {
      if (!chartContainerRef.current) return;
      chart.applyOptions({ width: chartContainerRef.current.clientWidth });
    };

    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      supportLinesRef.current = [];
      resistanceLinesRef.current = [];
    };
  }, [chartOptions]);

  // 2) Update candles when data changes
  useEffect(() => {
    if (!allData || !allData[symbol] || !candleSeriesRef.current) return;

    const data = allData[symbol];
    const candles = Array.isArray(data.candles) ? data.candles : [];

    if (candles.length === 0) return;

    // Expecting candles like: { time, open, high, low, close }
    // If your backend returns different keys, fix them here.
    candleSeriesRef.current.setData(candles);

    // If you store signals per symbol (example)
  
  }, [allData, symbol]);

  // 3) Draw support/resistance lines when levels change
  useEffect(() => {
    if (!allData || !allData[symbol] || !candleSeriesRef.current) return;

    const data = allData[symbol];
    const supports =
      data?.levels?.supports ||
      data?.supports ||
      [];
    const resistances =
      data?.levels?.resistances ||
      data?.resistances ||
      [];

  

const candles = data.candles || [];
if (!candles.length) return;

const currentPrice = candles[candles.length - 1].close;

// Strongest support = highest support below current price
const strongSupport = supports
  .filter(p => p < currentPrice)
  .sort((a, b) => b - a)[0];

// Strongest resistance = lowest resistance above current price
const strongResistance = resistances
  .filter(p => p > currentPrice)
  .sort((a, b) => a - b)[0];

// Remove old lines
supportLinesRef.current.forEach(line => {
  try { candleSeriesRef.current.removePriceLine(line); } catch {}
});
resistanceLinesRef.current.forEach(line => {
  try { candleSeriesRef.current.removePriceLine(line); } catch {}
});
supportLinesRef.current = [];
resistanceLinesRef.current = [];

// Draw only ONE support
if (strongSupport) {
  const line = candleSeriesRef.current.createPriceLine({
    price: strongSupport,
    title: "Support",
    lineWidth: 2,
  });
  supportLinesRef.current.push(line);
}

// Draw only ONE resistance
if (strongResistance) {
  const line = candleSeriesRef.current.createPriceLine({
    price: strongResistance,
    title: "Resistance",
    lineWidth: 2,
  });
  resistanceLinesRef.current.push(line);
}

  }, [allData, symbol]);
  
  // 3C) Draw Entry / SL / TP lines when signal changes
useEffect(() => {
  if (!allData || !allData[symbol] || !candleSeriesRef.current) return;

  const signal = allData[symbol]?.signal;
  if (!signal) return;

  const { entry, sl, tp } = signal;

  // Remove old lines if they exist
  if (entryLineRef.current) {
    try { candleSeriesRef.current.removePriceLine(entryLineRef.current); } catch {}
  }
  if (slLineRef.current) {
    try { candleSeriesRef.current.removePriceLine(slLineRef.current); } catch {}
  }
  if (tpLineRef.current) {
    try { candleSeriesRef.current.removePriceLine(tpLineRef.current); } catch {}
  }

  // Entry line (blue)
  entryLineRef.current = candleSeriesRef.current.createPriceLine({
    price: Number(entry),
    color: "#2196f3",
    lineWidth: 2,
    title: "Entry",
  });

  // Stop Loss (red)
  slLineRef.current = candleSeriesRef.current.createPriceLine({
    price: Number(sl),
    color: "#f44336",
    lineWidth: 2,
    title: "SL",
  });

  // Take Profit (green)
  tpLineRef.current = candleSeriesRef.current.createPriceLine({
    price: Number(tp),
    color: "#4caf50",
    lineWidth: 2,
    title: "TP",
  });

}, [allData, symbol]);
return (
  <div>
    <div
      ref={chartContainerRef}
      style={{ width: "100%", height: "320px" }}
    />
  

    {signal && (
      <div style={{ marginTop: 12 }}>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
          <div>Entry: <b>{signal.entry ?? "-"}</b></div>
          <div>SL: <b>{signal.sl ?? "-"}</b></div>
          <div>TP: <b>{signal.tp ?? "-"}</b></div>
          <div>Direction: <b>{signal.direction ?? "-"}</b></div>
          <div>Reason: <b>{signal.reason ?? "-"}</b></div>
          <div>Confidence: <b>{confidencePct}%</b></div>
        </div>

        <div style={{ marginTop: 6 }}>
          Reason: {signal.reason ?? "-"}
        </div>
      </div>
    )}
  </div>
);
};


export default Chart;
