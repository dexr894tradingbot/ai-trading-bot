// frontend/src/Dashboard.js
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Chart from "./Chart";
import { analyzeMarket, scanMarkets, getBackendWebSocketUrl } from "./api";

const VOLATILITY_OPTIONS = [
  { symbol: "R_10", name: "Volatility 10" },
  { symbol: "R_25", name: "Volatility 25" },
  { symbol: "R_50", name: "Volatility 50" },
  { symbol: "R_75", name: "Volatility 75" },
  { symbol: "R_100", name: "Volatility 100" },
];

const TIMEFRAMES = [
  { value: "1m", label: "1m" },
  { value: "5m", label: "5m" },
  { value: "15m", label: "15m" },
  { value: "30m", label: "30m" },
  { value: "1h", label: "1h" },
  { value: "2h", label: "2h" },
  { value: "4h", label: "4h" },
];

const LS_KEY_STATE = "dex_bot_ui_state_v1";
const LS_KEY_HISTORY = "dex_bot_history_v1";

function safeNum(x, fallback = 0) {
  const n = Number(x);
  return Number.isFinite(n) ? n : fallback;
}
function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n));
}
function fmt(x, digits = 5) {
  if (x === null || x === undefined) return "-";
  const n = Number(x);
  if (!Number.isFinite(n)) return String(x);
  return n.toFixed(digits);
}
function toIso(tsSec) {
  if (!tsSec) return "—";
  const ms = Number(tsSec) * 1000;
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleString();
}
function withTimeout(promise, ms, label = "Request") {
  let t = null;
  const timeout = new Promise((_, reject) => {
    t = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(t));
}

function pickModeLabel(sig = {}) {
  return (
    sig.entry_type ||
    sig.entryType ||
    sig.mode ||
    sig.type ||
    sig.path ||
    (sig.bos_level ? "SNIPER" : "") ||
    ""
  );
}

function normalizeAlignment(raw) {
  const a =
    raw?.alignment ||
    raw?.weighted_alignment ||
    raw?.htf?.weighted_alignment ||
    raw?.meta?.weighted_alignment ||
    null;

  const score = safeNum(
    a?.score ?? raw?.alignment_score ?? raw?.weighted_alignment_score ?? null,
    null
  );

  const label =
    a?.label ??
    raw?.alignment_label ??
    raw?.weighted_alignment_label ??
    (score !== null ? (score >= 70 ? "STRONG" : score >= 55 ? "OK" : "WEAK") : null);

  const details =
    Array.isArray(a?.details)
      ? a.details
      : Array.isArray(raw?.alignment_details)
      ? raw.alignment_details
      : Array.isArray(raw?.weighted_alignment_details)
      ? raw.weighted_alignment_details
      : Array.isArray(raw?.htf?.details)
      ? raw.htf.details
      : [];

  return {
    score: score === null ? null : clamp(score, 0, 100),
    label: label ?? null,
    details,
  };
}

function normalizeAnalyzeResponse(data) {
  const candles = Array.isArray(data?.candles) ? data.candles : [];

  const supports = Array.isArray(data?.levels?.supports)
    ? data.levels.supports
    : Array.isArray(data?.supports)
    ? data.supports
    : [];

  const resistances = Array.isArray(data?.levels?.resistances)
    ? data.levels.resistances
    : Array.isArray(data?.resistances)
    ? data.resistances
    : [];

  const sig = data?.signal || data?.sig || {};
  const direction = sig?.direction || sig?.action || "HOLD";
  const confidence = safeNum(sig?.confidence ?? sig?.score ?? 0, 0);
  const alignment = normalizeAlignment(data);

  const maxActive =
    safeNum(data?.max_active_total ?? data?.limits?.max_active_total ?? 5, 5) || 5;

  return {
    candles,
    supports,
    resistances,
    direction,
    confidence,
    entry: sig?.entry ?? null,
    sl: sig?.sl ?? null,
    tp: sig?.tp ?? null,
    tp1: sig?.tp1 ?? null,
    tp2: sig?.tp2 ?? null,
    tp1Hit: !!sig?.tp1_hit,
    rMultiple: sig?.r_multiple ?? null,
    reason: sig?.reason ?? data?.reason ?? "—",
    price: data?.price ?? sig?.price ?? null,
    entryType: pickModeLabel(sig),
    alignment,
    active: !!data?.active,
    actions: Array.isArray(data?.actions) ? data.actions : [],
    pending: data?.pending ?? null,
    closedTrade: data?.closed_trade ?? null,
    raw: data,
    maxActive,
  };
}

function makeHistoryItem({ symbol, timeframe, signal, openedAt, closedAt, outcome, closePrice }) {
  const entry = safeNum(signal?.entry, null);
  const sl = safeNum(signal?.sl, null);
  const tp = safeNum(signal?.tp2 ?? signal?.tp, null);
  const direction = signal?.direction || "HOLD";

  let rMult = null;
  if (entry !== null && sl !== null && tp !== null) {
    const risk = Math.abs(entry - sl);
    const reward = Math.abs(tp - entry);
    if (risk > 0) rMult = reward / risk;
  }

  return {
    id: `${symbol}-${timeframe}-${openedAt || Date.now()}-${Math.random().toString(16).slice(2)}`,
    symbol,
    timeframe,
    direction,
    confidence: safeNum(signal?.confidence, 0),
    entry,
    sl,
    tp,
    tp1: safeNum(signal?.tp1, null),
    tp2: safeNum(signal?.tp2 ?? signal?.tp, null),
    openedAt: openedAt ?? null,
    closedAt: closedAt ?? null,
    outcome: outcome ?? "OPEN",
    closePrice: closePrice ?? null,
    rMult: rMult !== null ? Number(rMult.toFixed(2)) : null,
    reason: signal?.reason ?? "—",
    tp1Hit: !!signal?.tp1_hit,
    entryType: pickModeLabel(signal),
  };
}

function calcPerformance(items) {
  const closed = items.filter((x) => x.outcome === "TP2" || x.outcome === "SL");
  const wins = closed.filter((x) => x.outcome === "TP2").length;
  const losses = closed.filter((x) => x.outcome === "SL").length;
  const total = closed.length;

  const winRate = total ? (wins / total) * 100 : 0;

  let sumR = 0;
  for (const t of closed) {
    if (t.outcome === "TP2") sumR += t.rMult ?? 1;
    if (t.outcome === "SL") sumR -= 1;
  }
  const avgR = total ? sumR / total : 0;

  let bestWinStreak = 0;
  let bestLoseStreak = 0;
  let curW = 0;
  let curL = 0;
  for (const t of closed) {
    if (t.outcome === "TP2") {
      curW += 1;
      curL = 0;
    } else if (t.outcome === "SL") {
      curL += 1;
      curW = 0;
    }
    bestWinStreak = Math.max(bestWinStreak, curW);
    bestLoseStreak = Math.max(bestLoseStreak, curL);
  }

  const last30 = closed.slice(-30);
  let last30R = 0;
  for (const t of last30) {
    if (t.outcome === "TP2") last30R += t.rMult ?? 1;
    if (t.outcome === "SL") last30R -= 1;
  }

  return {
    closedCount: total,
    wins,
    losses,
    winRate: Number(winRate.toFixed(1)),
    sumR: Number(sumR.toFixed(2)),
    avgR: Number(avgR.toFixed(2)),
    bestWinStreak,
    bestLoseStreak,
    last30Count: last30.length,
    last30R: Number(last30R.toFixed(2)),
  };
}

export default function Dashboard() {
  const restored = useMemo(() => {
    try {
      const raw = localStorage.getItem(LS_KEY_STATE);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }, []);

  const [selectedSymbol, setSelectedSymbol] = useState(restored?.selectedSymbol || "R_10");
  const [timeframe, setTimeframe] = useState(restored?.timeframe || "5m");

  const [status, setStatus] = useState("IDLE");
  const [error, setError] = useState("");

  const [candles, setCandles] = useState(restored?.candles || []);
  const [supports, setSupports] = useState(restored?.supports || []);
  const [resistances, setResistances] = useState(restored?.resistances || []);

  const [direction, setDirection] = useState(restored?.direction || "HOLD");
  const [confidence, setConfidence] = useState(restored?.confidence || 0);
  const [entry, setEntry] = useState(restored?.entry ?? null);
  const [sl, setSl] = useState(restored?.sl ?? null);
  const [tp, setTp] = useState(restored?.tp ?? null);
  const [tp1, setTp1] = useState(restored?.tp1 ?? null);
  const [tp2, setTp2] = useState(restored?.tp2 ?? null);
  const [reason, setReason] = useState(restored?.reason || "No signal yet.");
  const [price, setPrice] = useState(restored?.price ?? null);

  const [entryType, setEntryType] = useState(restored?.entryType || "");
  const [alignmentScore, setAlignmentScore] = useState(restored?.alignmentScore ?? null);
  const [alignmentLabel, setAlignmentLabel] = useState(restored?.alignmentLabel ?? null);
  const [alignmentDetails, setAlignmentDetails] = useState(restored?.alignmentDetails || []);

  const [scanLoading, setScanLoading] = useState(false);
  const [ranked, setRanked] = useState(restored?.ranked || []);
  const [dailyR, setDailyR] = useState(restored?.dailyR || 0);
  const [activeTotal, setActiveTotal] = useState(restored?.activeTotal || 0);
  const [maxActiveTotal, setMaxActiveTotal] = useState(restored?.maxActiveTotal || 5);

  const [autoScanOn, setAutoScanOn] = useState(restored?.autoScanOn ?? true);
  const [autoPickOn, setAutoPickOn] = useState(restored?.autoPickOn ?? false);
  const [autoScanEverySec, setAutoScanEverySec] = useState(restored?.autoScanEverySec ?? 25);

  const [autoAnalyzeOn, setAutoAnalyzeOn] = useState(restored?.autoAnalyzeOn ?? true);
  const [autoAnalyzeEverySec, setAutoAnalyzeEverySec] = useState(restored?.autoAnalyzeEverySec ?? 15);

  const [activeTrade, setActiveTrade] = useState(restored?.activeTrade || null);
  const [lastActions, setLastActions] = useState(restored?.lastActions || []);

  const [showHistory, setShowHistory] = useState(restored?.showHistory ?? true);
  const [history, setHistory] = useState(() => {
    try {
      const raw = localStorage.getItem(LS_KEY_HISTORY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });

  const performance = useMemo(() => calcPerformance(history), [history]);
  const symbolsList = useMemo(() => VOLATILITY_OPTIONS.map((v) => v.symbol), []);
  const selectedName =
    VOLATILITY_OPTIONS.find((v) => v.symbol === selectedSymbol)?.name || selectedSymbol;

  const busyRef = useRef(false);
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const lastOkAtRef = useRef(restored?.lastOkAt || 0);
  const lastLiveAtRef = useRef(restored?.lastLiveAt || 0);
  const openTradeRef = useRef(null);

  useEffect(() => {
    try {
      localStorage.setItem(
        LS_KEY_STATE,
        JSON.stringify({
          selectedSymbol,
          timeframe,
          candles,
          supports,
          resistances,
          direction,
          confidence,
          entry,
          sl,
          tp,
          tp1,
          tp2,
          reason,
          price,
          entryType,
          alignmentScore,
          alignmentLabel,
          alignmentDetails,
          ranked,
          dailyR,
          activeTotal,
          maxActiveTotal,
          autoScanOn,
          autoPickOn,
          autoScanEverySec,
          autoAnalyzeOn,
          autoAnalyzeEverySec,
          showHistory,
          activeTrade,
          lastActions,
          lastOkAt: lastOkAtRef.current,
          lastLiveAt: lastLiveAtRef.current,
        })
      );
    } catch {}
  }, [
    selectedSymbol,
    timeframe,
    candles,
    supports,
    resistances,
    direction,
    confidence,
    entry,
    sl,
    tp,
    tp1,
    tp2,
    reason,
    price,
    entryType,
    alignmentScore,
    alignmentLabel,
    alignmentDetails,
    ranked,
    dailyR,
    activeTotal,
    maxActiveTotal,
    autoScanOn,
    autoPickOn,
    autoScanEverySec,
    autoAnalyzeOn,
    autoAnalyzeEverySec,
    showHistory,
    activeTrade,
    lastActions,
  ]);

  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY_HISTORY, JSON.stringify(history.slice(-500)));
    } catch {}
  }, [history]);

  useEffect(() => {
    const id = setInterval(() => {
      if (busyRef.current) return;

      const now = Date.now();
      const liveAge = now - (lastLiveAtRef.current || 0);
      const okAge = now - (lastOkAtRef.current || 0);

      if (liveAge <= 8000 || okAge <= 30000) {
        setStatus("LIVE");
      } else {
        setStatus("IDLE");
      }
    }, 1000);

    return () => clearInterval(id);
  }, []);

  const topPick = useMemo(() => {
    if (!ranked?.length) return null;
    const candidates = ranked
      .filter((r) => (r.direction === "BUY" || r.direction === "SELL") && safeNum(r.confidence, 0) > 0)
      .sort((a, b) => {
        const ar = safeNum(a.score_1_10 ?? a.rank ?? 0, 0);
        const br = safeNum(b.score_1_10 ?? b.rank ?? 0, 0);
        if (br !== ar) return br - ar;
        return safeNum(b.confidence, 0) - safeNum(a.confidence, 0);
      });

    return candidates[0] || null;
  }, [ranked]);

  const currentAlignmentChip =
    alignmentScore !== null
      ? `${alignmentLabel ? `${alignmentLabel} ` : ""}${Math.round(alignmentScore)}%`
      : null;

  const runAnalyze = useCallback(
    async (symbol, tf) => {
      if (busyRef.current) return null;
      busyRef.current = true;
      setError("");
      setStatus("ANALYZING");

      try {
        const data = await withTimeout(analyzeMarket(symbol, tf), 15000, "Analyze");
        const norm = normalizeAnalyzeResponse(data);

        setCandles(norm.candles);
        setSupports(norm.supports);
        setResistances(norm.resistances);

        setDirection(norm.direction || "HOLD");
        setConfidence(safeNum(norm.confidence, 0));
        setEntry(norm.entry ?? null);
        setSl(norm.sl ?? null);
        setTp(norm.tp ?? null);
        setTp1(norm.tp1 ?? null);
        setTp2(norm.tp2 ?? null);
        setReason(norm.reason || "—");
        if (norm.price !== null && norm.price !== undefined) setPrice(norm.price);

        setEntryType(norm.entryType || "");
        setAlignmentScore(norm.alignment?.score ?? null);
        setAlignmentLabel(norm.alignment?.label ?? null);
        setAlignmentDetails(Array.isArray(norm.alignment?.details) ? norm.alignment.details : []);

        setMaxActiveTotal(safeNum(norm.maxActive ?? maxActiveTotal, maxActiveTotal));
        setDailyR(safeNum(norm.raw?.daily_R ?? dailyR, dailyR));
        setActiveTotal(safeNum(norm.raw?.active_total ?? activeTotal, activeTotal));

        setLastActions(norm.actions || []);

        if (norm.active && norm.raw?.signal) {
          setActiveTrade({
            symbol,
            timeframe: tf,
            ...norm.raw.signal,
            actions: norm.actions || [],
            price: norm.price ?? null,
            alignment: norm.alignment || null,
          });
        }

        if (norm.active && norm.raw?.signal?.opened_at) {
          const openedAt = norm.raw.signal.opened_at;

          const alreadyOpen =
            openTradeRef.current &&
            openTradeRef.current.symbol === symbol &&
            openTradeRef.current.timeframe === tf &&
            openTradeRef.current.openedAt === openedAt;

          if (!alreadyOpen) {
            openTradeRef.current = { symbol, timeframe: tf, openedAt };

            setHistory((prev) => {
              const exists = prev.some(
                (t) => t.symbol === symbol && t.timeframe === tf && t.outcome === "OPEN" && t.openedAt === openedAt
              );
              if (exists) return prev;

              const item = makeHistoryItem({
                symbol,
                timeframe: tf,
                signal: norm.raw.signal,
                openedAt,
                outcome: "OPEN",
              });
              return [...prev, item].slice(-500);
            });
          }
        }

        if (norm.closedTrade?.outcome && (norm.closedTrade.outcome === "TP2" || norm.closedTrade.outcome === "SL")) {
          const outcome = norm.closedTrade.outcome;
          const closedAt = norm.closedTrade.closed_at ?? Math.floor(Date.now() / 1000);

          setHistory((prev) => {
            const next = [...prev];
            for (let i = next.length - 1; i >= 0; i--) {
              const t = next[i];
              if (t.symbol === symbol && t.timeframe === tf && t.outcome === "OPEN") {
                next[i] = { ...t, outcome, closedAt, closePrice: norm.price ?? t.closePrice };
                return next.slice(-500);
              }
            }
            const item = makeHistoryItem({
              symbol,
              timeframe: tf,
              signal: norm.closedTrade,
              openedAt: norm.closedTrade.opened_at ?? null,
              closedAt,
              outcome,
              closePrice: norm.price ?? null,
            });
            return [...next, item].slice(-500);
          });

          if (openTradeRef.current && openTradeRef.current.symbol === symbol && openTradeRef.current.timeframe === tf) {
            openTradeRef.current = null;
          }
          setActiveTrade(null);
        }

        lastOkAtRef.current = Date.now();
        setStatus("LIVE");
        return norm;
      } catch (e) {
        setError(e?.message || "Analyze failed");
        return null;
      } finally {
        busyRef.current = false;
      }
    },
    [dailyR, activeTotal, maxActiveTotal]
  );

  const onScan = useCallback(async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setError("");
    setScanLoading(true);
    setStatus("SCANNING");

    try {
      const data = await withTimeout(scanMarkets(symbolsList, timeframe), 15000, "Scan");
      const newRanked = Array.isArray(data?.ranked) ? data.ranked : Array.isArray(data) ? data : [];

      newRanked.sort((a, b) => {
        const ar = safeNum(a.score_1_10 ?? a.rank ?? 0, 0);
        const br = safeNum(b.score_1_10 ?? b.rank ?? 0, 0);
        if (br !== ar) return br - ar;
        return safeNum(b.confidence, 0) - safeNum(a.confidence, 0);
      });

      setRanked(newRanked);
      setDailyR(safeNum(data?.daily_R ?? 0, 0));
      setActiveTotal(safeNum(data?.active_total ?? 0, 0));
      if (data?.max_active_total !== undefined) setMaxActiveTotal(safeNum(data.max_active_total, maxActiveTotal));

      if (autoPickOn) {
        const best = newRanked
          .filter((r) => (r.direction === "BUY" || r.direction === "SELL") && safeNum(r.confidence, 0) > 0)
          .sort((a, b) => {
            const ar = safeNum(a.score_1_10 ?? a.rank ?? 0, 0);
            const br = safeNum(b.score_1_10 ?? b.rank ?? 0, 0);
            if (br !== ar) return br - ar;
            return safeNum(b.confidence, 0) - safeNum(a.confidence, 0);
          })[0];

        if (best?.symbol) {
          if (best.symbol !== selectedSymbol) setSelectedSymbol(best.symbol);
          busyRef.current = false;
          return await runAnalyze(best.symbol, timeframe);
        }
      }

      lastOkAtRef.current = Date.now();
      setStatus("LIVE");
      return null;
    } catch (e) {
      setError(e?.message || "Scan failed");
      return null;
    } finally {
      busyRef.current = false;
      setScanLoading(false);
    }
  }, [symbolsList, timeframe, autoPickOn, selectedSymbol, runAnalyze, maxActiveTotal]);

  const onAnalyze = useCallback(async () => {
    await runAnalyze(selectedSymbol, timeframe);
  }, [runAnalyze, selectedSymbol, timeframe]);

  useEffect(() => {
    let closedByUser = false;

    function connect() {
      try {
        const wsUrl = getBackendWebSocketUrl();
        const ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        ws.onopen = () => {
          ws.send(JSON.stringify({
            symbol: selectedSymbol,
            timeframe,
          }));
        };

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);

            if (msg.type === "live_chart") {
              const supports = Array.isArray(msg?.levels?.supports) ? msg.levels.supports : [];
              const resistances = Array.isArray(msg?.levels?.resistances) ? msg.levels.resistances : [];
              const candles = Array.isArray(msg?.candles) ? msg.candles : [];

              setCandles(candles);
              setSupports(supports);
              setResistances(resistances);

              if (msg.price !== null && msg.price !== undefined) {
                setPrice(msg.price);
              }

              lastLiveAtRef.current = Date.now();
              if (!busyRef.current) setStatus("LIVE");
            }
          } catch (err) {
            console.error("WS parse error:", err);
          }
        };

        ws.onclose = () => {
          wsRef.current = null;

          if (!closedByUser) {
            reconnectTimerRef.current = setTimeout(() => {
              connect();
            }, 3000);
          }
        };

        ws.onerror = () => {
          try {
            ws.close();
          } catch {}
        };
      } catch (err) {
        console.error("WS connect error:", err);
      }
    }

    connect();

    return () => {
      closedByUser = true;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {}
      }
    };
  }, [selectedSymbol, timeframe]);

  useEffect(() => {
    if (!autoScanOn) return;
    const everyMs = clamp(safeNum(autoScanEverySec, 25), 5, 300) * 1000;

    onScan();
    const id = setInterval(() => {
      if (!document.hidden) onScan();
    }, everyMs);

    return () => clearInterval(id);
  }, [autoScanOn, autoScanEverySec, onScan]);

  useEffect(() => {
    if (!autoAnalyzeOn) return;
    const everyMs = clamp(safeNum(autoAnalyzeEverySec, 15), 5, 120) * 1000;

    if (!busyRef.current) runAnalyze(selectedSymbol, timeframe);

    const id = setInterval(() => {
      if (!document.hidden && !busyRef.current) {
        runAnalyze(selectedSymbol, timeframe);
      }
    }, everyMs);

    return () => clearInterval(id);
  }, [autoAnalyzeOn, autoAnalyzeEverySec, runAnalyze, selectedSymbol, timeframe]);

  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === "visible") {
        if (autoAnalyzeOn && !busyRef.current) runAnalyze(selectedSymbol, timeframe);
        if (autoScanOn && !busyRef.current) onScan();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [autoAnalyzeOn, autoScanOn, runAnalyze, onScan, selectedSymbol, timeframe]);

  const clearHistory = useCallback(() => {
    openTradeRef.current = null;
    setHistory([]);
    try {
      localStorage.removeItem(LS_KEY_HISTORY);
    } catch {}
  }, []);

  const activeBanner = activeTrade && (activeTrade.direction === "BUY" || activeTrade.direction === "SELL");

  const activeAlignmentChip =
    activeTrade?.alignment?.score !== undefined && activeTrade?.alignment?.score !== null
      ? `${activeTrade?.alignment?.label ? `${activeTrade.alignment.label} ` : ""}${Math.round(activeTrade.alignment.score)}%`
      : null;

  return (
    <div className="layout">
      <div className="card topCard">
        <div className="topRow">
          <div>
            <div className="title">DEXTRADEZ AI TRADING SYSTEM</div>
            <div className="subtitle">Volatility Engine • Liquidity Detection • Signal Intelligence</div>
          </div>

          <div className="badges">
            <span className={`badge ${status === "LIVE" ? "live" : ""}`}>
              {status === "SCANNING" ? <span className="scanning">SCANNING</span> : status}
            </span>
            <span className="badge">Daily R: {dailyR}</span>
            <span className="badge">Active: {activeTotal}/{maxActiveTotal}</span>
            <span className="badge">Price: {price === null || price === undefined ? "—" : String(price)}</span>
          </div>
        </div>

        {activeBanner ? (
          <div className="activeBanner">
            <div className="activeLeft">
              <span className="pill fire">🟢 ACTIVE</span>
              <span className="mono">{activeTrade.symbol} • {activeTrade.timeframe}</span>
              <span className="pill">{activeTrade.direction}</span>
              <span className="pill">Conf {safeNum(activeTrade.confidence, 0)}%</span>
              {pickModeLabel(activeTrade) ? <span className="pill">Mode {pickModeLabel(activeTrade)}</span> : null}
              {activeAlignmentChip ? <span className="pill">Align {activeAlignmentChip}</span> : null}
              {activeTrade.tp1_hit ? <span className="pill pillWin">TP1 hit ✅</span> : <span className="pill">TP1 pending</span>}
            </div>

            <div className="activeRight">
              <div className="activeNums">
                <div><div className="miniLabel">Entry</div><div className="mono">{fmt(activeTrade.entry)}</div></div>
                <div><div className="miniLabel">SL</div><div className="mono">{fmt(activeTrade.sl)}</div></div>
                <div><div className="miniLabel">TP1</div><div className="mono">{fmt(activeTrade.tp1)}</div></div>
                <div><div className="miniLabel">TP2</div><div className="mono">{fmt(activeTrade.tp2 ?? activeTrade.tp)}</div></div>
              </div>

              <button className="btn small" onClick={() => setSelectedSymbol(activeTrade.symbol)}>Select</button>
            </div>

            {Array.isArray(activeTrade.actions) && activeTrade.actions.length ? (
              <div className="activeActions">
                <div className="miniLabel">Actions</div>
                <div className="actionsList">
                  {activeTrade.actions.slice(0, 4).map((a, idx) => (
                    <div key={idx} className="actionChip">
                      <span className="mono">{a.type}</span>
                      {a.to !== undefined ? <span className="mono">→ {a.to}</span> : null}
                      {a.percent !== undefined ? <span className="mono">({Math.round(a.percent * 100)}%)</span> : null}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}

        <div className="controls">
          <select value={selectedSymbol} onChange={(e) => setSelectedSymbol(e.target.value)} className="select">
            {VOLATILITY_OPTIONS.map((v) => (
              <option key={v.symbol} value={v.symbol}>{v.symbol} — {v.name}</option>
            ))}
          </select>

          <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="select">
            {TIMEFRAMES.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>

          <button className="btn primary" onClick={onAnalyze}>Analyze (Manual)</button>
          <button className="btn" onClick={onScan} disabled={scanLoading}>
            {scanLoading ? "Scanning..." : "Scan Markets (Manual)"}
          </button>
        </div>

        <div className="togglesRow">
          <label className="toggle">
            <input type="checkbox" checked={autoScanOn} onChange={(e) => setAutoScanOn(e.target.checked)} />
            <span>Auto-scan</span>
          </label>

          <div className="miniField">
            <span className="miniLabel">every</span>
            <input className="miniInput" value={autoScanEverySec} onChange={(e) => setAutoScanEverySec(e.target.value)} inputMode="numeric" />
            <span className="miniLabel">s</span>
          </div>

          <label className="toggle">
            <input type="checkbox" checked={autoPickOn} onChange={(e) => setAutoPickOn(e.target.checked)} />
            <span>Auto-pick strongest</span>
          </label>

          <label className="toggle">
            <input type="checkbox" checked={autoAnalyzeOn} onChange={(e) => setAutoAnalyzeOn(e.target.checked)} />
            <span>Auto-analyze</span>
          </label>

          <div className="miniField">
            <span className="miniLabel">every</span>
            <input className="miniInput" value={autoAnalyzeEverySec} onChange={(e) => setAutoAnalyzeEverySec(e.target.value)} inputMode="numeric" />
            <span className="miniLabel">s</span>
          </div>

          <label className="toggle">
            <input type="checkbox" checked={showHistory} onChange={(e) => setShowHistory(e.target.checked)} />
            <span>Show history</span>
          </label>
        </div>

        <div className="system-panel">
          <div className="system-panel-title">System Status</div>
          <div className="system-list">
            <div className="system-item">
              <span>Liquidity Engine</span>
              <span className="system-dot"></span>
            </div>
            <div className="system-item">
              <span>Volatility Filter</span>
              <span className="system-dot"></span>
            </div>
            <div className="system-item">
              <span>Regime Detection</span>
              <span className="system-dot"></span>
            </div>
            <div className="system-item">
              <span>Spike Protection</span>
              <span className="system-dot"></span>
            </div>
            <div className="system-item">
              <span>Telegram Alerts</span>
              <span className="system-dot"></span>
            </div>
            <div className="system-item">
              <span>Auto Scanner</span>
              <span className={autoScanOn ? "system-dot" : "system-dot off"}></span>
            </div>
          </div>
        </div>

        {topPick ? (
          <div className="topPick">
            <div className="topPickLeft">
              <span className="pill fire">🔥 TOP</span>
              <span className="mono">{topPick.symbol}</span>
              <span className="pill">{topPick.direction}</span>
              <span className="pill">Rank {safeNum(topPick.score_1_10 ?? topPick.rank ?? 0, 0)}/10</span>
              <span className="pill">Conf {safeNum(topPick.confidence, 0)}%</span>
              {topPick.entry_type || topPick.mode ? <span className="pill">Mode {topPick.entry_type || topPick.mode}</span> : null}
            </div>
            <div className="topPickRight">
              <button className="btn small" onClick={() => setSelectedSymbol(topPick.symbol)}>Select</button>
            </div>
          </div>
        ) : null}

        {error ? <div className="errorBox">Error: {error}</div> : null}
      </div>

      <div className="mainGrid">
        <div className="card">
          <div className="cardHeader">
            <h3>Chart</h3>
            <div className="tiny">Candles: {candles?.length || 0} • S: {supports?.length || 0} • R: {resistances?.length || 0}</div>
          </div>

          <div className="chartWrap">
            <Chart candles={candles} supports={supports} resistances={resistances} symbol={selectedSymbol} timeframe={timeframe} />
          </div>
        </div>

        <div className="card">
          <div className="cardHeader">
            <h3>Signal</h3>
            <span className={`badge ${status === "LIVE" ? "live" : ""}`}>{status}</span>
          </div>

          <div className="signalLine"><span className="smallText">Market</span><strong>{selectedSymbol} — {selectedName}</strong></div>
          <div className="signalLine"><span className="smallText">Timeframe</span><strong>{timeframe}</strong></div>
          <div className="signalLine"><span className="smallText">Direction</span><strong>{direction}</strong></div>
          <div className="signalLine"><span className="smallText">Confidence</span><strong>{confidence}%</strong></div>
          <div className="signalLine"><span className="smallText">Entry Type</span><strong>{entryType || "—"}</strong></div>
          <div className="signalLine"><span className="smallText">Weighted Alignment</span><strong>{currentAlignmentChip || "—"}</strong></div>

          {alignmentDetails?.length ? (
            <div className="alignBox">
              <div className="tiny"><b>Alignment details:</b></div>
              <div className="alignList">
                {alignmentDetails.slice(0, 6).map((d, idx) => {
                  const tf = d.tf || d.timeframe || d.t || "?";
                  const dir = d.dir || d.direction || "-";
                  const w = d.weight !== undefined ? d.weight : d.w;
                  const ok = d.ok ?? d.match ?? d.aligned;
                  return (
                    <span key={idx} className={`pill ${ok === false ? "pillLoss" : ok === true ? "pillWin" : ""}`}>
                      {tf}:{dir}{w !== undefined ? ` (w ${w})` : ""}
                    </span>
                  );
                })}
              </div>
            </div>
          ) : null}

          <div className="signalLine"><span className="smallText">Entry</span><strong>{entry ?? "-"}</strong></div>
          <div className="signalLine"><span className="smallText">SL</span><strong>{sl ?? "-"}</strong></div>
          <div className="signalLine"><span className="smallText">TP (Final)</span><strong>{tp ?? "-"}</strong></div>
          <div className="signalLine"><span className="smallText">TP1</span><strong>{tp1 ?? "-"}</strong></div>
          <div className="signalLine"><span className="smallText">TP2</span><strong>{tp2 ?? "-"}</strong></div>

          <div className="reason"><span className="smallText"><b>Reason:</b> {reason}</span></div>

          {lastActions?.length ? (
            <div className="actionsBox">
              <div className="tiny"><b>Backend actions:</b></div>
              <div className="actionsList">
                {lastActions.slice(0, 4).map((a, idx) => (
                  <div key={idx} className="actionChip">
                    <span className="mono">{a.type}</span>
                    {a.to !== undefined ? <span className="mono">→ {a.to}</span> : null}
                    {a.percent !== undefined ? <span className="mono">({Math.round(a.percent * 100)}%)</span> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="card scannerCard">
        <div className="cardHeader"><h3>Scanner</h3><div className="tiny">Markets: {ranked?.length || 0}</div></div>
        <div className="tableWrap">
          <table className="table">
            <thead>
              <tr>
                <th>Market</th><th>Rank</th><th>Action</th><th>Conf</th><th>Mode</th><th>Entry</th><th>SL</th><th>TP</th><th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {ranked?.length ? (
                ranked.map((r, idx) => {
                  const isHot = idx === 0 && (r.direction === "BUY" || r.direction === "SELL") && safeNum(r.confidence, 0) > 0;
                  const rowMode = r.entry_type || r.mode || (r.pending ? "PENDING" : r.active_trade ? "ACTIVE" : "");
                  return (
                    <tr key={`${r.symbol || idx}-${idx}`} className={isHot ? "hotRow" : ""}>
                      <td className="mono">
                        {r.symbol ?? "-"} {isHot ? <span className="pill fireTiny">🔥</span> : null}
                        {r.active_trade ? <span className="pill pillWin" style={{ marginLeft: 6 }}>ACTIVE</span> : null}
                        {r.pending ? <span className="pill" style={{ marginLeft: 6 }}>PENDING</span> : null}
                      </td>
                      <td>{safeNum(r.rank ?? r.score_1_10 ?? 0, 0)}</td>
                      <td>{r.action ?? r.direction ?? "—"}</td>
                      <td>{safeNum(r.confidence, 0)}%</td>
                      <td>{rowMode || "—"}</td>
                      <td>{r.entry ?? "-"}</td>
                      <td>{r.sl ?? "-"}</td>
                      <td>{r.tp ?? "-"}</td>
                      <td className="notes">{r.reason ?? r.notes ?? "—"}</td>
                    </tr>
                  );
                })
              ) : (
                <tr><td colSpan="9" className="emptyRow">No scan yet. Click “Scan Markets”.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {showHistory ? (
        <div className="card historyCard">
          <div className="cardHeader">
            <h3>History & Performance</h3>
            <div className="tiny">
              Closed: {performance.closedCount} • Wins: {performance.wins} • Losses: {performance.losses} • Win%: {performance.winRate}% • Sum R: {performance.sumR}
            </div>
          </div>

          <div className="perfGrid">
            <div className="perfBox"><div className="perfLabel">Avg R</div><div className="perfValue">{performance.avgR}</div></div>
            <div className="perfBox"><div className="perfLabel">Best Win Streak</div><div className="perfValue">{performance.bestWinStreak}</div></div>
            <div className="perfBox"><div className="perfLabel">Worst Lose Streak</div><div className="perfValue">{performance.bestLoseStreak}</div></div>
            <div className="perfBox"><div className="perfLabel">Last 30 (R)</div><div className="perfValue">{performance.last30R}</div></div>
          </div>

          <div className="historyActions">
            <button className="btn small" onClick={clearHistory}>Clear history</button>
            <span className={`reviewBadge ${performance.closedCount >= 30 ? "ready" : ""}`}>
              {performance.closedCount >= 30 ? "✅ Review ready (30 trades)" : `Review after 30 trades (${performance.closedCount}/30)`}
            </span>
          </div>

          <div className="tableWrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th><th>Market</th><th>TF</th><th>Dir</th><th>Mode</th><th>Conf</th><th>Entry</th><th>SL</th><th>TP2</th><th>Outcome</th><th>R</th>
                </tr>
              </thead>
              <tbody>
                {history?.length ? (
                  [...history].slice(-50).reverse().map((t) => (
                    <tr key={t.id} className={t.outcome === "TP2" ? "winRow" : t.outcome === "SL" ? "lossRow" : ""}>
                      <td className="tiny">{t.closedAt ? toIso(t.closedAt) : toIso(t.openedAt)}</td>
                      <td className="mono">{t.symbol}</td>
                      <td>{t.timeframe}</td>
                      <td>{t.direction}</td>
                      <td>{t.entryType || "—"}</td>
                      <td>{safeNum(t.confidence, 0)}%</td>
                      <td>{fmt(t.entry)}</td>
                      <td>{fmt(t.sl)}</td>
                      <td>{fmt(t.tp2 ?? t.tp)}</td>
                      <td><span className={`pill ${t.outcome === "TP2" ? "pillWin" : t.outcome === "SL" ? "pillLoss" : ""}`}>{t.outcome}</span></td>
                      <td>{t.outcome === "TP2" ? `+${t.rMult ?? "?"}` : t.outcome === "SL" ? "-1" : "—"}</td>
                    </tr>
                  ))
                ) : (
                  <tr><td colSpan="11" className="emptyRow">No history yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="tiny note">
            Note: If your backend sleeps, updates can stall until it wakes.
          </div>
        </div>
      ) : null}
    </div>
  );
}