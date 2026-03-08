// frontend/src/Chart.js
import React, { useEffect, useRef } from "react";
import { createChart, CandlestickSeries } from "lightweight-charts";

function safeNumber(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
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
      width: containerRef.current.clientWidth,
      height: 460,
      layout: {
        background: { type: "solid", color: "#06141d" },
        textColor: "#d8f6ff",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "rgba(0,255,208,0.08)" },
        horzLines: { color: "rgba(0,255,208,0.08)" },
      },
      rightPriceScale: {
        borderColor: "rgba(0,255,208,0.16)",
        textColor: "#bfefff",
      },
      timeScale: {
        borderColor: "rgba(0,255,208,0.16)",
        timeVisible: true,
        secondsVisible: false,
      },
      crosshair: {
        vertLine: {
          color: "rgba(0,255,208,0.25)",
          width: 1,
          style: 0,
          labelBackgroundColor: "#0f2e3a",
        },
        horzLine: {
          color: "rgba(0,255,208,0.25)",
          width: 1,
          style: 0,
          labelBackgroundColor: "#0f2e3a",
        },
      },
    });

    chartRef.current = chart;

    let series;
    if (typeof chart.addCandlestickSeries === "function") {
      series = chart.addCandlestickSeries({
        upColor: "#2dffb4",
        downColor: "#ff5f8a",
        borderUpColor: "#2dffb4",
        borderDownColor: "#ff5f8a",
        wickUpColor: "#7dffd0",
        wickDownColor: "#ff8cab",
        priceLineVisible: false,
        lastValueVisible: true,
      });
    } else if (typeof chart.addSeries === "function") {
      series = chart.addSeries(CandlestickSeries, {
        upColor: "#2dffb4",
        downColor: "#ff5f8a",
        borderUpColor: "#2dffb4",
        borderDownColor: "#ff5f8a",
        wickUpColor: "#7dffd0",
        wickDownColor: "#ff8cab",
        priceLineVisible: false,
        lastValueVisible: true,
      });
    } else {
      console.error("No supported candlestick series API found.");
      return;
    }

    candleSeriesRef.current = series;

    const onResize = () => {
      if (!containerRef.current || !chartRef.current) return;
      chartRef.current.applyOptions({
        width: containerRef.current.clientWidth,
      });
      chartRef.current.timeScale().fitContent();
    };

    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      try {
        chart.remove();
      } catch {}
    };
  }, []);

  useEffect(() => {
    const series = candleSeriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart) return;

    const arr = Array.isArray(candles) ? candles : [];

    const formatted = arr
      .map((c) => ({
        time:
          c.time ??
          c.epoch ??
          c.t ??
          null,
        open: safeNumber(c.open ?? c.o),
        high: safeNumber(c.high ?? c.h),
        low: safeNumber(c.low ?? c.l),
        close: safeNumber(c.close ?? c.c),
      }))
      .filter(
        (c) =>
          c.time !== null &&
          c.open !== null &&
          c.high !== null &&
          c.low !== null &&
          c.close !== null
      );

    if (!formatted.length) return;

    series.setData(formatted);
    chart.timeScale().fitContent();
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

    const addLine = (price, title, color) => {
      const n = safeNumber(price);
      if (n === null) return;

      try {
        const line = series.createPriceLine({
          price: n,
          color,
          lineWidth: 2,
          lineStyle: 2,
          axisLabelVisible: true,
          title,
        });
        priceLinesRef.current.push(line);
      } catch {}
    };

    const supportList = Array.isArray(supports) ? supports : [];
    const resistanceList = Array.isArray(resistances) ? resistances : [];

    supportList.slice(0, 3).forEach((s, i) => addLine(s, `S${i + 1}`, "#1fd6ff"));
    resistanceList.slice(0, 3).forEach((r, i) => addLine(r, `R${i + 1}`, "#ffd76a"));

    addLine(entry, "ENTRY", "#2dffb4");
    addLine(sl, "SL", "#ff5f8a");
    addLine(tp, "TP", "#ffd76a");
    addLine(tp1, "TP1", "#8affc1");
    addLine(tp2, "TP2", "#ffe082");
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