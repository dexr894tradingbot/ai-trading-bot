// frontend/src/Dashboard.js
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Chart from "./Chart";
import { analyzeMarket, scanMarkets } from "./api";

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

// --- Helpers to display new backend fields safely ---
function pickModeLabel(sig = {}) {
  // supports: entry_type, entryType, mode, type, path
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
  // supports multiple shapes:
  // 1) raw.alignment = { score, label, details: [{tf, dir, weight, ok}] }
  // 2) raw.alignment_score, raw.alignment_label, raw.alignment_details
  // 3) raw.htf = { weighted_alignment: {...} }
  const a =
    raw?.alignment ||
    raw?.weighted_alignment ||
    raw?.htf?.weighted_alignment ||
    raw?.meta?.weighted_alignment ||
    null;

  const score =
    safeNum(a?.score ?? raw?.alignment_score ?? raw?.weighted_alignment_score ?? null, null);

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

// ✅ Backend-normalizer (supports your analyze.py + future extra fields)
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

  // max active total (backend can return max_active_total)
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
    tp: sig?.tp ?? null, // backend uses tp as final (TP2)
    tp1: sig?.tp1 ?? null,
    tp2: sig?.tp2 ?? null,
    tp1Hit: !!sig?.tp1_hit,
    rMultiple: sig?.r_multiple ?? null,
    reason: sig?.reason ?? data?.reason ?? "—",
    price: data?.price ?? sig?.price ?? null,

    // new / optional fields
    entryType: pickModeLabel(sig),
    mode: pickModeLabel(sig),
    alignment,

    active: !!data?.active,
    actions: Array.isArray(data?.actions) ? data.actions : [],
    pending: data?.pending ?? null,

    closedTrade: data?.closed_trade ?? null,
    raw: data,
    maxActive,
  };
}

// ✅ History record
function makeHistoryItem({ symbol, timeframe, signal, openedAt, closedAt, outcome, closePrice }) {
  const entry = safeNum(signal?.entry, null);
  const sl = safeNum(signal?.sl, null);

  // IMPORTANT: backend tp is TP2/final
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
    outcome: outcome ?? "OPEN", // OPEN | TP2 | SL
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

  // Sum R (if rMult known). If unknown, TP2 = +1R fallback.
  let sumR = 0;
  for (const t of closed) {
    if (t.outcome === "TP2") sumR += t.rMult ?? 1;
    if (t.outcome === "SL") sumR -= 1;
  }

  const avgR = total ? sumR / total : 0;

  // Streaks
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

  // Last 30 review
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
  const [selectedSymbol, setSelectedSymbol] = useState("R_10");
  const [timeframe, setTimeframe] = useState("5m");

  const [status, setStatus] = useState("IDLE");
  const [error, setError] = useState("");

  // Chart + signal state
  const [candles, setCandles] = useState([]);
  const [supports, setSupports] = useState([]);
  const [resistances, setResistances] = useState([]);

  const [direction, setDirection] = useState("HOLD");
  const [confidence, setConfidence] = useState(0);
  const [entry, setEntry] = useState(null);
  const [sl, setSl] = useState(null);
  const [tp, setTp] = useState(null);
  const [tp1, setTp1] = useState(null);
  const [tp2, setTp2] = useState(null);
  const [reason, setReason] = useState("No signal yet. Click Analyze.");
  const [price, setPrice] = useState(null);

  // NEW: Entry type + alignment for UI
  const [entryType, setEntryType] = useState("");
  const [alignmentScore, setAlignmentScore] = useState(null);
  const [alignmentLabel, setAlignmentLabel] = useState(null);
  const [alignmentDetails, setAlignmentDetails] = useState([]);

  // Scanner state
  const [scanLoading, setScanLoading] = useState(false);
  const [ranked, setRanked] = useState([]);
  const [dailyR, setDailyR] = useState(0);
  const [activeTotal, setActiveTotal] = useState(0);
  const [maxActiveTotal, setMaxActiveTotal] = useState(5); // default to 5 (your update)

  // Automation
  const [autoScanOn, setAutoScanOn] = useState(true);
  const [autoPickOn, setAutoPickOn] = useState(true);
  const [autoScanEverySec, setAutoScanEverySec] = useState(25);

  const [autoAnalyzeOn, setAutoAnalyzeOn] = useState(false);
  const [autoAnalyzeEverySec, setAutoAnalyzeEverySec] = useState(15);

  // ✅ Active Trade Banner state (PINNED)
  const [activeTrade, setActiveTrade] = useState(null); // {symbol, timeframe, ...signal, actions, price}
  const [lastActions, setLastActions] = useState([]);

  // History + performance
  const [showHistory, setShowHistory] = useState(true);
  const [history, setHistory] = useState(() => {
    try {
      const raw = localStorage.getItem("dex_bot_history_v1");
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });

  const performance = useMemo(() => calcPerformance(history), [history]);

  // track last OPEN trade id-ish (avoid duplicates)
  const openTradeRef = useRef(null); // { symbol, timeframe, openedAt }

  const symbolsList = useMemo(() => VOLATILITY_OPTIONS.map((v) => v.symbol), []);
  const selectedName = VOLATILITY_OPTIONS.find((v) => v.symbol === selectedSymbol)?.name || selectedSymbol;

  // persist history
  useEffect(() => {
    try {
      localStorage.setItem("dex_bot_history_v1", JSON.stringify(history.slice(-500)));
    } catch {}
  }, [history]);

  const topPick = useMemo(() => {
    if (!ranked?.length) return null;

    // Prefer backend "score_1_10" (rank) then confidence
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

  const runAnalyze = useCallback(
    async (symbol, tf) => {
      setError("");
      setStatus("ANALYZING");

      try {
        const data = await analyzeMarket(symbol, tf);
        const norm = normalizeAnalyzeResponse(data);

        // Update chart/signal
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

        // NEW: entry type + alignment
        setEntryType(norm.entryType || "");
        setAlignmentScore(norm.alignment?.score ?? null);
        setAlignmentLabel(norm.alignment?.label ?? null);
        setAlignmentDetails(Array.isArray(norm.alignment?.details) ? norm.alignment.details : []);

        // NEW: max active (defaults to 5)
        setMaxActiveTotal(safeNum(norm.maxActive ?? maxActiveTotal, maxActiveTotal));

        setDailyR(safeNum(norm.raw?.daily_R ?? dailyR, dailyR));
        setActiveTotal(safeNum(norm.raw?.active_total ?? activeTotal, activeTotal));

        // actions
        setLastActions(norm.actions || []);

        // PIN active trade in banner
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

        // If backend opened/active trade, add to history ONCE
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

        // If backend says trade closed, update history + banner
        if (norm.closedTrade?.outcome && (norm.closedTrade.outcome === "TP2" || norm.closedTrade.outcome === "SL")) {
          const outcome = norm.closedTrade.outcome;
          const closedAt = norm.closedTrade.closed_at ?? Math.floor(Date.now() / 1000);

          setHistory((prev) => {
            const next = [...prev];
            for (let i = next.length - 1; i >= 0; i--) {
              const t = next[i];
              if (t.symbol === symbol && t.timeframe === tf && t.outcome === "OPEN") {
                next[i] = {
                  ...t,
                  outcome,
                  closedAt,
                  closePrice: norm.price ?? t.closePrice,
                };
                return next.slice(-500);
              }
            }
            // if no OPEN found, append closed
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

          // Remove banner ONLY when trade is actually closed
          setActiveTrade(null);
        }

        setStatus("LIVE");
        return norm;
      } catch (e) {
        setStatus("IDLE");
        setError(e?.message || "Analyze failed");
        return null;
      }
    },
    [dailyR, activeTotal, maxActiveTotal]
  );

  const onAnalyze = useCallback(async () => {
    await runAnalyze(selectedSymbol, timeframe);
  }, [runAnalyze, selectedSymbol, timeframe]);

  const onScan = useCallback(async () => {
    setError("");
    setScanLoading(true);
    setStatus("SCANNING");

    try {
      const data = await scanMarkets(symbolsList, timeframe);
      const newRanked = Array.isArray(data?.ranked) ? data.ranked : Array.isArray(data) ? data : [];

      // sort by backend rank score then confidence
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
          await runAnalyze(best.symbol, timeframe);
        }
      }

      setStatus("LIVE");
    } catch (e) {
      setStatus("IDLE");
      setError(e?.message || "Scan failed");
    } finally {
      setScanLoading(false);
    }
  }, [symbolsList, timeframe, autoPickOn, selectedSymbol, runAnalyze, maxActiveTotal]);

  // Auto-scan loop
  useEffect(() => {
    if (!autoScanOn) return;
    const everyMs = clamp(safeNum(autoScanEverySec, 25), 5, 300) * 1000;

    onScan();
    const id = setInterval(() => onScan(), everyMs);
    return () => clearInterval(id);
  }, [autoScanOn, autoScanEverySec, onScan]);

  // Auto-analyze loop
  useEffect(() => {
    if (!autoAnalyzeOn) return;
    const everyMs = clamp(safeNum(autoAnalyzeEverySec, 15), 5, 120) * 1000;

    const id = setInterval(() => runAnalyze(selectedSymbol, timeframe), everyMs);
    return () => clearInterval(id);
  }, [autoAnalyzeOn, autoAnalyzeEverySec, runAnalyze, selectedSymbol, timeframe]);

  const clearHistory = useCallback(() => {
    openTradeRef.current = null;
    setHistory([]);
    try {
      localStorage.removeItem("dex_bot_history_v1");
    } catch {}
  }, []);

  const reviewReady = performance.closedCount >= 30;

  const activeBanner = activeTrade && (activeTrade.direction === "BUY" || activeTrade.direction === "SELL");

  const currentAlignmentChip =
    alignmentScore !== null
      ? `${alignmentLabel ? `${alignmentLabel} ` : ""}${Math.round(alignmentScore)}%`
      : null;

  const activeAlignmentChip =
    activeTrade?.alignment?.score !== undefined && activeTrade?.alignment?.score !== null
      ? `${activeTrade?.alignment?.label ? `${activeTrade.alignment.label} ` : ""}${Math.round(
          activeTrade.alignment.score
        )}%`
      : null;

  return (
    <div className="layout">
      {/* Header */}
      <div className="card topCard">
        <div className="topRow">
          <div>
            <div className="title">DEXTRADEZBOT</div>
            <div className="subtitle">5m entries • HTF bias • scanner • trade plan</div>
          </div>

          <div className="badges">
            <span className={`badge ${status === "LIVE" ? "live" : ""}`}>{status}</span>
            <span className="badge">Daily R: {dailyR}</span>
            <span className="badge">
              Active: {activeTotal}/{maxActiveTotal}
            </span>
            <span className="badge">Price: {price === null || price === undefined ? "—" : String(price)}</span>
          </div>
        </div>

        {/* ✅ ACTIVE TRADE BANNER (PINNED) */}
        {activeBanner ? (
          <div className="activeBanner">
            <div className="activeLeft">
              <span className="pill fire">🟢 ACTIVE</span>
              <span className="mono">
                {activeTrade.symbol} • {activeTrade.timeframe}
              </span>
              <span className="pill">{activeTrade.direction}</span>
              <span className="pill">Conf {safeNum(activeTrade.confidence, 0)}%</span>
              {pickModeLabel(activeTrade) ? <span className="pill">Mode {pickModeLabel(activeTrade)}</span> : null}
              {activeAlignmentChip ? <span className="pill">Align {activeAlignmentChip}</span> : null}
              {activeTrade.tp1_hit ? (
                <span className="pill pillWin">TP1 hit ✅</span>
              ) : (
                <span className="pill">TP1 pending</span>
              )}
            </div>

            <div className="activeRight">
              <div className="activeNums">
                <div>
                  <div className="miniLabel">Entry</div>
                  <div className="mono">{fmt(activeTrade.entry)}</div>
                </div>
                <div>
                  <div className="miniLabel">SL</div>
                  <div className="mono">{fmt(activeTrade.sl)}</div>
                </div>
                <div>
                  <div className="miniLabel">TP1</div>
                  <div className="mono">{fmt(activeTrade.tp1)}</div>
                </div>
                <div>
                  <div className="miniLabel">TP2</div>
                  <div className="mono">{fmt(activeTrade.tp2 ?? activeTrade.tp)}</div>
                </div>
              </div>

              <button
                className="btn small"
                onClick={() => {
                  setSelectedSymbol(activeTrade.symbol);
                }}
              >
                Select
              </button>
            </div>

            {Array.isArray(activeTrade.actions) && activeTrade.actions.length ? (
              <div className="activeActions">
                <div className="miniLabel">Actions</div>
                <div className="actionsList">
                  {activeTrade.actions.slice(0, 4).map((a, idx) => (
                    <div key={idx} className="actionChip">
                      <span className="mono">{a.type}</span>
                      {a.to !== undefined ? <span className="mono">→ {a.to}</span> : null}
                      {a.percent !== undefined ? (
                        <span className="mono">({Math.round(a.percent * 100)}%)</span>
                      ) : null}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}

        {/* Controls */}
        <div className="controls">
          <select value={selectedSymbol} onChange={(e) => setSelectedSymbol(e.target.value)} className="select">
            {VOLATILITY_OPTIONS.map((v) => (
              <option key={v.symbol} value={v.symbol}>
                {v.symbol} — {v.name}
              </option>
            ))}
          </select>

          <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="select">
            {TIMEFRAMES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>

          <button className="btn primary" onClick={onAnalyze}>
            Analyze (Manual)
          </button>

          <button className="btn" onClick={onScan} disabled={scanLoading}>
            {scanLoading ? "Scanning..." : "Scan Markets (Manual)"}
          </button>
        </div>

        {/* Automation toggles */}
        <div className="togglesRow">
          <label className="toggle">
            <input type="checkbox" checked={autoScanOn} onChange={(e) => setAutoScanOn(e.target.checked)} />
            <span>Auto-scan</span>
          </label>

          <div className="miniField">
            <span className="miniLabel">every</span>
            <input
              className="miniInput"
              value={autoScanEverySec}
              onChange={(e) => setAutoScanEverySec(e.target.value)}
              inputMode="numeric"
            />
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
            <input
              className="miniInput"
              value={autoAnalyzeEverySec}
              onChange={(e) => setAutoAnalyzeEverySec(e.target.value)}
              inputMode="numeric"
              disabled={!autoAnalyzeOn}
            />
            <span className="miniLabel">s</span>
          </div>

          <label className="toggle">
            <input type="checkbox" checked={showHistory} onChange={(e) => setShowHistory(e.target.checked)} />
            <span>Show history</span>
          </label>
        </div>

        {/* Top Pick banner */}
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
              <button className="btn small" onClick={() => setSelectedSymbol(topPick.symbol)}>
                Select
              </button>
            </div>
          </div>
        ) : null}

        {error ? <div className="errorBox">Error: {error}</div> : null}
      </div>

      {/* Main grid */}
      <div className="mainGrid">
        <div className="card">
          <div className="cardHeader">
            <h3>Chart</h3>
            <div className="tiny">
              Candles: {candles?.length || 0} • S: {supports?.length || 0} • R: {resistances?.length || 0}
            </div>
          </div>

          <div className="chartWrap">
            <Chart
              candles={candles}
              supports={supports}
              resistances={resistances}
              symbol={selectedSymbol}
              timeframe={timeframe}
            />
          </div>
        </div>

        <div className="card">
          <div className="cardHeader">
            <h3>Signal</h3>
            <span className={`badge ${status === "LIVE" ? "live" : ""}`}>{status === "LIVE" ? "LIVE" : "IDLE"}</span>
          </div>

          <div className="signalLine">
            <span className="smallText">Market</span>
            <strong>
              {selectedSymbol} — {selectedName} {topPick?.symbol === selectedSymbol ? <span className="fireEmoji">🔥</span> : null}
            </strong>
          </div>

          <div className="signalLine">
            <span className="smallText">Timeframe</span>
            <strong>{timeframe}</strong>
          </div>

          <div className="signalLine">
            <span className="smallText">Direction</span>
            <strong>{direction}</strong>
          </div>

          <div className="signalLine">
            <span className="smallText">Confidence</span>
            <strong>{confidence}%</strong>
          </div>

          {/* NEW: mode / entry type */}
          <div className="signalLine">
            <span className="smallText">Entry Type</span>
            <strong>{entryType || "—"}</strong>
          </div>

          {/* NEW: alignment */}
          <div className="signalLine">
            <span className="smallText">Weighted Alignment</span>
            <strong>{currentAlignmentChip || "—"}</strong>
          </div>

          {alignmentDetails?.length ? (
            <div className="alignBox">
              <div className="tiny">
                <b>Alignment details:</b>
              </div>
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

          <div className="signalLine">
            <span className="smallText">Entry</span>
            <strong>{entry ?? "-"}</strong>
          </div>

          <div className="signalLine">
            <span className="smallText">SL</span>
            <strong>{sl ?? "-"}</strong>
          </div>

          <div className="signalLine">
            <span className="smallText">TP (Final)</span>
            <strong>{tp ?? "-"}</strong>
          </div>

          <div className="signalLine">
            <span className="smallText">TP1</span>
            <strong>{tp1 ?? "-"}</strong>
          </div>

          <div className="signalLine">
            <span className="smallText">TP2</span>
            <strong>{tp2 ?? "-"}</strong>
          </div>

          <div className="reason">
            <span className="smallText">
              <b>Reason:</b> {reason}
            </span>
          </div>

          {/* Optional: show last backend actions even if not active */}
          {lastActions?.length ? (
            <div className="actionsBox">
              <div className="tiny">
                <b>Backend actions:</b>
              </div>
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

      {/* Scanner */}
      <div className="card scannerCard">
        <div className="cardHeader">
          <h3>Scanner</h3>
          <div className="tiny">Markets: {ranked?.length || 0}</div>
        </div>

        <div className="tableWrap">
          <table className="table">
            <thead>
              <tr>
                <th>Market</th>
                <th>Rank</th>
                <th>Action</th>
                <th>Conf</th>
                <th>Mode</th>
                <th>Entry</th>
                <th>SL</th>
                <th>TP</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {ranked?.length ? (
                ranked.map((r, idx) => {
                  const isHot =
                    idx === 0 && (r.direction === "BUY" || r.direction === "SELL") && safeNum(r.confidence, 0) > 0;

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
                <tr>
                  <td colSpan="9" className="emptyRow">
                    No scan yet. Click “Scan Markets”.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* History + Performance */}
      {showHistory ? (
        <div className="card historyCard">
          <div className="cardHeader">
            <h3>History & Performance</h3>
            <div className="tiny">
              Closed: {performance.closedCount} • Wins: {performance.wins} • Losses: {performance.losses} • Win%:{" "}
              {performance.winRate}% • Sum R: {performance.sumR}
            </div>
          </div>

          <div className="perfGrid">
            <div className="perfBox">
              <div className="perfLabel">Avg R</div>
              <div className="perfValue">{performance.avgR}</div>
            </div>
            <div className="perfBox">
              <div className="perfLabel">Best Win Streak</div>
              <div className="perfValue">{performance.bestWinStreak}</div>
            </div>
            <div className="perfBox">
              <div className="perfLabel">Worst Lose Streak</div>
              <div className="perfValue">{performance.bestLoseStreak}</div>
            </div>
            <div className="perfBox">
              <div className="perfLabel">Last 30 (R)</div>
              <div className="perfValue">{performance.last30R}</div>
            </div>
          </div>

          <div className="historyActions">
            <button className="btn small" onClick={clearHistory}>
              Clear history
            </button>

            <span className={`reviewBadge ${reviewReady ? "ready" : ""}`}>
              {reviewReady ? "✅ Review ready (30 trades)" : `Review after 30 trades (${performance.closedCount}/30)`}
            </span>
          </div>

          <div className="tableWrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Market</th>
                  <th>TF</th>
                  <th>Dir</th>
                  <th>Mode</th>
                  <th>Conf</th>
                  <th>Entry</th>
                  <th>SL</th>
                  <th>TP2</th>
                  <th>Outcome</th>
                  <th>R</th>
                </tr>
              </thead>
              <tbody>
                {history?.length ? (
                  [...history]
                    .slice(-50)
                    .reverse()
                    .map((t) => (
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
                        <td>
                          <span className={`pill ${t.outcome === "TP2" ? "pillWin" : t.outcome === "SL" ? "pillLoss" : ""}`}>
                            {t.outcome}
                          </span>
                        </td>
                        <td>{t.outcome === "TP2" ? `+${t.rMult ?? "?"}` : t.outcome === "SL" ? "-1" : "—"}</td>
                      </tr>
                    ))
                ) : (
                  <tr>
                    <td colSpan="11" className="emptyRow">
                      No history yet. When a trade hits TP2/SL, it will show here.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="tiny note">
            Note: History updates when your backend reports TP2/SL (meaning Auto-analyze should be ON, or you should keep pressing Analyze).
          </div>
        </div>
      ) : null}

      {/* Styles */}
      <style>{`
        .layout { max-width: 1100px; margin: 0 auto; padding: 14px; }
        .card {
          background: rgba(255,255,255,0.06);
          border: 1px solid rgba(255,255,255,0.10);
          border-radius: 18px;
          padding: 14px;
          backdrop-filter: blur(14px);
          box-shadow: 0 10px 30px rgba(0,0,0,0.18);
        }
        .topCard { padding: 16px; }
        .topRow { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
        .title { font-size: 20px; font-weight: 800; letter-spacing: 0.4px; }
        .subtitle { margin-top: 3px; opacity: 0.85; font-size: 13px; }
        .badges { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
        .badge {
          font-size: 12px; padding: 6px 10px; border-radius: 999px;
          background: rgba(255,255,255,0.08);
          border: 1px solid rgba(255,255,255,0.10);
          opacity: 0.95;
        }
        .badge.live { border-color: rgba(80, 200, 120, 0.45); }

        .controls { margin-top: 12px; display:flex; gap:10px; flex-wrap:wrap; }
        .select {
          padding: 10px 12px; border-radius: 12px;
          background: rgba(0,0,0,0.25);
          border: 1px solid rgba(255,255,255,0.12);
          color: inherit;
          outline: none;
        }
        .btn {
          padding: 10px 14px; border-radius: 12px;
          background: rgba(255,255,255,0.10);
          border: 1px solid rgba(255,255,255,0.14);
          color: inherit;
          cursor: pointer;
        }
        .btn.primary {
          background: rgba(90, 140, 255, 0.18);
          border-color: rgba(90, 140, 255, 0.35);
        }
        .btn.small { padding: 8px 10px; border-radius: 10px; font-size: 12px; }
        .btn:disabled { opacity: 0.6; cursor: not-allowed; }

        .errorBox {
          margin-top: 10px;
          padding: 10px 12px;
          border-radius: 12px;
          background: rgba(255, 80, 80, 0.12);
          border: 1px solid rgba(255, 80, 80, 0.25);
          font-size: 13px;
        }

        .togglesRow {
          margin-top: 10px;
          display:flex;
          gap: 12px;
          flex-wrap: wrap;
          align-items: center;
          padding-top: 10px;
          border-top: 1px solid rgba(255,255,255,0.08);
        }
        .toggle { display:flex; align-items:center; gap:8px; font-size: 13px; opacity: 0.95; }
        .toggle input { transform: scale(1.05); }
        .miniField { display:flex; align-items:center; gap:6px; }
        .miniLabel { font-size: 12px; opacity: 0.8; }
        .miniInput {
          width: 56px;
          padding: 8px 10px;
          border-radius: 10px;
          background: rgba(0,0,0,0.25);
          border: 1px solid rgba(255,255,255,0.12);
          color: inherit;
          outline: none;
          text-align: center;
        }
        .miniInput:disabled { opacity: 0.6; }

        .pill {
          display:inline-flex;
          align-items:center;
          gap: 6px;
          padding: 4px 10px;
          border-radius: 999px;
          border: 1px solid rgba(255,255,255,0.12);
          background: rgba(0,0,0,0.20);
          font-size: 12px;
          opacity: 0.95;
        }
        .pill.fire { border-color: rgba(255, 170, 60, 0.35); background: rgba(255, 170, 60, 0.12); }
        .pill.fireTiny { margin-left: 6px; padding: 2px 8px; font-size: 11px; border-color: rgba(255, 170, 60, 0.35); background: rgba(255, 170, 60, 0.12); }
        .pillWin { border-color: rgba(80, 200, 120, 0.45); background: rgba(80, 200, 120, 0.12); }
        .pillLoss { border-color: rgba(255, 80, 80, 0.45); background: rgba(255, 80, 80, 0.12); }

        .topPick {
          margin-top: 10px;
          padding: 10px 12px;
          border-radius: 14px;
          background: rgba(255,255,255,0.06);
          border: 1px solid rgba(255,255,255,0.10);
          display:flex;
          justify-content: space-between;
          align-items: center;
          gap: 12px;
        }
        .topPickLeft { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
        .topPickRight { display:flex; gap:10px; align-items:center; }

        .activeBanner {
          margin-top: 12px;
          padding: 12px;
          border-radius: 16px;
          background: rgba(80, 200, 120, 0.06);
          border: 1px solid rgba(80, 200, 120, 0.22);
        }
        .activeLeft { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
        .activeRight { margin-top: 10px; display:flex; justify-content:space-between; align-items:flex-end; gap:12px; flex-wrap:wrap; }
        .activeNums { display:flex; gap:16px; flex-wrap:wrap; }
        .activeNums > div { min-width: 86px; }
        .activeActions { margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.10); }
        .actionsList { display:flex; gap:8px; flex-wrap:wrap; margin-top: 6px; }
        .actionChip {
          padding: 6px 10px;
          border-radius: 999px;
          background: rgba(0,0,0,0.20);
          border: 1px solid rgba(255,255,255,0.12);
          font-size: 12px;
          display:flex;
          gap: 8px;
          align-items:center;
          opacity: 0.95;
        }

        .mainGrid {
          display:grid;
          grid-template-columns: 1.3fr 0.7fr;
          gap: 14px;
          margin-top: 14px;
        }
        @media (max-width: 900px) {
          .mainGrid { grid-template-columns: 1fr; }
          .topRow { flex-direction: column; }
          .badges { justify-content:flex-start; }
        }

        .cardHeader { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom: 10px; }
        .cardHeader h3 { margin:0; font-size: 16px; letter-spacing: 0.2px; }
        .tiny { font-size: 12px; opacity: 0.8; }
        .chartWrap { border-radius: 14px; overflow: hidden; border: 1px solid rgba(255,255,255,0.08); }

        .smallText { opacity: 0.85; font-size: 13px; line-height: 1.35; }
        .signalLine { display:flex; justify-content:space-between; gap:12px; padding: 7px 0; border-bottom: 1px solid rgba(255,255,255,0.08); }
        .signalLine:last-of-type { border-bottom: none; }
        .reason { margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.08); }
        .fireEmoji { margin-left: 6px; }
        .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }

        .actionsBox { margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.08); }

        .alignBox { margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.08); }
        .alignList { display:flex; gap:8px; flex-wrap:wrap; margin-top: 8px; }

        .scannerCard { margin-top: 14px; }
        .historyCard { margin-top: 14px; }

        .tableWrap { overflow:auto; border-radius: 14px; border: 1px solid rgba(255,255,255,0.08); }
        .table { width:100%; border-collapse: collapse; min-width: 820px; }
        .table th, .table td { padding: 10px 10px; text-align:left; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,0.07); }
        .table th { position: sticky; top: 0; background: rgba(20, 20, 30, 0.65); backdrop-filter: blur(10px); }
        .notes { opacity: 0.85; }
        .emptyRow { opacity: 0.8; }
        .hotRow { background: rgba(255, 170, 60, 0.06); }

        .perfGrid {
          display:grid;
          grid-template-columns: repeat(4, 1fr);
          gap: 10px;
          margin: 10px 0 12px 0;
        }
        @media (max-width: 900px) {
          .perfGrid { grid-template-columns: repeat(2, 1fr); }
        }
        .perfBox {
          padding: 10px 12px;
          border-radius: 14px;
          background: rgba(255,255,255,0.05);
          border: 1px solid rgba(255,255,255,0.08);
        }
        .perfLabel { font-size: 12px; opacity: 0.8; }
        .perfValue { font-size: 18px; font-weight: 800; margin-top: 3px; }

        .historyActions {
          display:flex;
          gap: 10px;
          align-items:center;
          justify-content: space-between;
          flex-wrap: wrap;
          margin-bottom: 10px;
        }
        .reviewBadge {
          font-size: 12px;
          padding: 6px 10px;
          border-radius: 999px;
          border: 1px solid rgba(255,255,255,0.12);
          background: rgba(0,0,0,0.20);
          opacity: 0.9;
        }
        .reviewBadge.ready {
          border-color: rgba(80, 200, 120, 0.45);
          background: rgba(80, 200, 120, 0.12);
          opacity: 1;
        }

        .winRow { background: rgba(80, 200, 120, 0.04); }
        .lossRow { background: rgba(255, 80, 80, 0.04); }

        .note { margin-top: 10px; }
      `}</style>
    </div>
  );
}