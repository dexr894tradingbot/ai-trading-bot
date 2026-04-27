// frontend/src/Chart.js
import React, { useEffect, useRef } from "react";
import { createChart, LineSeries, CandlestickSeries} from "lightweight-charts";

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

function lineStyleFromSignalState(signalState) {
  if (signalState === "READY") return 0;
  if (signalState === "CONFIRMED") return 1;
  if (signalState === "WATCH") return 2;
  return 2;
}

function zoneLabelPrefix(signalState) {
  if (signalState === "READY") return "READY ZONE";
  if (signalState === "CONFIRMED") return "CONFIRM ZONE";
  if (signalState === "WATCH") return "WATCH ZONE";
  return "ZONE";
}

function zoneColors(signalState) {
  if (signalState === "READY") {
    return {
      line: "#2dffb4",
      top: "rgba(45,255,180,0.18)",
      bottom: "rgba(45,255,180,0.03)",
    };
  }
  if (signalState === "CONFIRMED") {
    return {
      line: "#ffd76a",
      top: "rgba(255,215,106,0.16)",
      bottom: "rgba(255,215,106,0.03)",
    };
  }
  return {
    line: "#7ae7ff",
    top: "rgba(122,231,255,0.14)",
    bottom: "rgba(122,231,255,0.03)",
  };
}

function lastTimeFromCandles(candles) {
  if (!Array.isArray(candles) || !candles.length) return null;
  const last = normalizeCandle(candles[candles.length - 1]);
  return last?.time ?? null;
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
  zoneLow = null,
  zoneHigh = null,
  breakLevel = null,
  signalState = "HOLD",
}) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const priceLinesRef = useRef([]);
  const resizeObserverRef = useRef(null);

  const zoneTopSeriesRef = useRef(null);
  const zoneBottomSeriesRef = useRef(null);
  const breakSeriesRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;

    if (chartRef.current) {
      try {
        chartRef.current.remove();
      } catch {}
      chartRef.current = null;
      candleSeriesRef.current = null;
      priceLinesRef.current = [];
      zoneTopSeriesRef.current = null;
      zoneBottomSeriesRef.current = null;
      breakSeriesRef.current = null;
    }

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth || 300,
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

    let candleSeries;
    if (typeof chart.addCandlestickSeries === "function") {
      candleSeries = chart.addCandlestickSeries({
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
      candleSeries = chart.addSeries(CandlestickSeries, {
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

    candleSeriesRef.current = candleSeries;

    let zoneTopSeries;
    let zoneBottomSeries;
    let breakSeries;

    if (typeof chart.addLineSeries === "function") {
      zoneTopSeries = chart.addLineSeries({
        color: "#7ae7ff",
        lineWidth: 2,
        lineVisible: true,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });

      zoneBottomSeries = chart.addLineSeries({
        color: "#7ae7ff",
        lineWidth: 2,
        lineVisible: true,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });

      breakSeries = chart.addLineSeries({
        color: "#ffb86b",
        lineWidth: 2,
        lineVisible: true,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
    } else if (typeof chart.addSeries === "function") {
      zoneTopSeries = chart.addSeries(LineSeries, {
        color: "#7ae7ff",
        lineWidth: 2,
        lineVisible: true,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });

      zoneBottomSeries = chart.addSeries(LineSeries, {
        color: "#7ae7ff",
        lineWidth: 2,
        lineVisible: true,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });

      breakSeries = chart.addSeries(LineSeries, {
        color: "#ffb86b",
        lineWidth: 2,
        lineVisible: true,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
    }

    zoneTopSeriesRef.current = zoneTopSeries;
    zoneBottomSeriesRef.current = zoneBottomSeries;
    breakSeriesRef.current = breakSeries;

    const handleResize = () => {
      if (!containerRef.current || !chartRef.current) return;
      chartRef.current.applyOptions({
        width: containerRef.current.clientWidth || 300,
      });
      chartRef.current.timeScale().fitContent();
    };

    if (typeof ResizeObserver !== "undefined") {
      resizeObserverRef.current = new ResizeObserver(() => {
        handleResize();
      });
      resizeObserverRef.current.observe(containerRef.current);
    }

    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);

      if (resizeObserverRef.current) {
        try {
          resizeObserverRef.current.disconnect();
        } catch {}
      }

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

    if (!formatted.length) {
      series.setData([]);
      if (zoneTopSeriesRef.current) zoneTopSeriesRef.current.setData([]);
      if (zoneBottomSeriesRef.current) zoneBottomSeriesRef.current.setData([]);
      if (breakSeriesRef.current) breakSeriesRef.current.setData([]);
      return;
    }

    series.setData(formatted);
    chart.timeScale().fitContent();
  }, [candles, symbol, timeframe]);

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

    const zoneStyle = lineStyleFromSignalState(signalState);
    const zonePrefix = zoneLabelPrefix(signalState);
    const colors = zoneColors(signalState);

    addLine(zoneLow, `${zonePrefix} LOW`, colors.line, 2, zoneStyle);
    addLine(zoneHigh, `${zonePrefix} HIGH`, colors.line, 2, zoneStyle);
    addLine(breakLevel, "BREAK LEVEL", "#ffb86b", 2, 1);

    addLine(entry, "ENTRY", "#2dffb4", 2, 0);
    addLine(sl, "SL", "#ff5f8a", 2, 0);
    addLine(tp, "TP", "#ffd76a", 2, 0);
    addLine(tp1, "TP1", "#8affc1", 2, 1);
    addLine(tp2, "TP2", "#ffe082", 2, 1);
  }, [
    supports,
    resistances,
    entry,
    sl,
    tp,
    tp1,
    tp2,
    zoneLow,
    zoneHigh,
    breakLevel,
    signalState,
    symbol,
    timeframe,
  ]);

  useEffect(() => {
    const zoneTopSeries = zoneTopSeriesRef.current;
    const zoneBottomSeries = zoneBottomSeriesRef.current;
    const breakSeries = breakSeriesRef.current;

    const formatted = (Array.isArray(candles) ? candles : [])
      .map(normalizeCandle)
      .filter(Boolean);

    if (!formatted.length) {
      if (zoneTopSeries) zoneTopSeries.setData([]);
      if (zoneBottomSeries) zoneBottomSeries.setData([]);
      if (breakSeries) breakSeries.setData([]);
      return;
    }

    const firstTime = formatted[0]?.time ?? null;
    const lastTime = formatted[formatted.length - 1]?.time ?? null;

    const top = safeNumber(zoneHigh);
    const bottom = safeNumber(zoneLow);
    const breakLvl = safeNumber(breakLevel);

    const colors = zoneColors(signalState);

    if (zoneTopSeries) {
      try {
        zoneTopSeries.applyOptions({ color: colors.line });
      } catch {}
    }
    if (zoneBottomSeries) {
      try {
        zoneBottomSeries.applyOptions({ color: colors.line });
      } catch {}
    }

    if (top !== null && firstTime !== null && lastTime !== null && zoneTopSeries) {
      zoneTopSeries.setData([
        { time: firstTime, value: top },
        { time: lastTime, value: top },
      ]);
    } else if (zoneTopSeries) {
      zoneTopSeries.setData([]);
    }

    if (bottom !== null && firstTime !== null && lastTime !== null && zoneBottomSeries) {
      zoneBottomSeries.setData([
        { time: firstTime, value: bottom },
        { time: lastTime, value: bottom },
      ]);
    } else if (zoneBottomSeries) {
      zoneBottomSeries.setData([]);
    }

    if (breakLvl !== null && firstTime !== null && lastTime !== null && breakSeries) {
      breakSeries.setData([
        { time: firstTime, value: breakLvl },
        { time: lastTime, value: breakLvl },
      ]);
    } else if (breakSeries) {
      breakSeries.setData([]);
    }
  }, [candles, zoneLow, zoneHigh, breakLevel, signalState]);

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