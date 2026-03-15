import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Chart from "./Chart";
import { analyzeMarket, scanMarkets, fetchGlobalState, getBackendWebSocketUrl } from "./api";

const VOLATILITY_OPTIONS = [
  { symbol: "ALL", name: "All Markets" },
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

const LS_KEY_STATE = "dex_bot_ui_state_v6";
const LS_KEY_HISTORY = "dex_bot_history_v2";

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

function qualityText(grade, stars) {
  if (!grade) return "—";
  return `${grade}${stars ? ` ${stars}` : ""}`;
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

function normalizeBriefing(brief = {}) {
  return {
    bias: brief?.bias ?? "-",
    structure: brief?.structure ?? "-",
    previousTrend: brief?.previous_trend ?? "-",
    trendStrength:
      brief?.trend_strength === 0 || brief?.trend_strength
        ? brief.trend_strength
        : null,
    marketState: brief?.market_state ?? "-",
    reversalRisk: brief?.reversal_risk ?? "-",
    support: brief?.support ?? "-",
    resistance: brief?.resistance ?? "-",
    buyerZone: brief?.buyer_zone ?? "-",
    sellerZone: brief?.seller_zone ?? "-",
    liquidityBelow: brief?.liquidity_below ?? "-",
    liquidityAbove: brief?.liquidity_above ?? "-",
    areaOfInterest: brief?.area_of_interest ?? "-",
    preferredSetup: brief?.preferred_setup ?? "-",
    confirmationNeeded: brief?.confirmation_needed ?? "-",
    invalidation: brief?.invalidation ?? "-",
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
  const briefing = normalizeBriefing(data?.briefing || {});
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
    qualityGrade: sig?.quality_grade ?? null,
    qualityStars: sig?.quality_stars ?? "",
    qualityScore: safeNum(sig?.quality_score ?? null, null),
    alignment,
    briefing,
    active: !!data?.active,
    actions: Array.isArray(data?.actions) ? data.actions : [],
    closedTrade: data?.closed_trade ?? null,
    liveTracker: data?.live_tracker ?? null,
    dailyOutlook: data?.daily_outlook ?? null,
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
    qualityGrade: signal?.quality_grade ?? null,
    qualityStars: signal?.quality_stars ?? "",
  };
}
function normalizeBackendHistoryItem(t = {}) {
  return {
    id:
      t.trade_id ||
      `${t.symbol || "UNK"}-${t.timeframe || "UNK"}-${t.opened_at || Date.now()}`,
    symbol: t.symbol || "—",
    timeframe: t.timeframe || "—",
    direction: t.direction || "HOLD",
    confidence: safeNum(t.confidence, 0),
    entry: safeNum(t.entry, null),
    sl: safeNum(t.sl, null),
    tp: safeNum(t.tp2 ?? t.tp, null),
    tp1: safeNum(t.tp1, null),
    tp2: safeNum(t.tp2 ?? t.tp, null),
    openedAt: t.opened_at ?? null,
    closedAt: t.closed_at ?? null,
    outcome:
      t.status === "CLOSED"
        ? t.outcome || "CLOSED"
        : "OPEN",
    closePrice: safeNum(t.closed_price, null),
    rMult: safeNum(t.r_multiple, null),
    reason: t.reason || "—",
    tp1Hit: !!t.tp1_hit,
    entryType: pickModeLabel(t),
    qualityGrade: t.quality_grade ?? null,
    qualityStars: t.quality_stars ?? "",
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
function computeStrength(row) {
  const conf = safeNum(row.confidence, 0);

  const actionBonus =
    row.direction === "BUY" || row.direction === "SELL" ? 0.8 : 0.2;

  const stateBonus =
    row.market_state === "TRENDING CLEAN"
      ? 1.0
      : row.market_state === "TRENDING PULLBACK"
      ? 0.7
      : row.market_state === "WEAK TREND"
      ? 0.35
      : 0.1;

  const riskPenalty =
    row.reversal_risk === "HIGH"
      ? -1.3
      : row.reversal_risk === "MEDIUM"
      ? -0.6
      : 0;

  const confPart = (conf / 100) * 7;
  const raw = confPart + actionBonus + stateBonus + riskPenalty;

  return clamp(raw, 0, 10);
}

function renderStrengthBar(score) {
  const filled = Math.max(0, Math.min(10, Math.round(score)));
  const empty = 10 - filled;
  return "█".repeat(filled) + "░".repeat(empty);
}

function buildAiExplanation({
  bias,
  structure,
  marketState,
  reversalRisk,
  areaOfInterest,
  preferredSetup,
  confirmationNeeded,
  liquidityAbove,
  liquidityBelow,
}) {
  const lines = [];

  lines.push(`Bias is ${bias || "-"}.`);
  lines.push(`Structure is ${structure || "-"}.`);
  lines.push(`Market state is ${marketState || "-"}.`);

  if (reversalRisk && reversalRisk !== "-") lines.push(`Reversal risk is ${reversalRisk}.`);
  if (areaOfInterest && areaOfInterest !== "-") lines.push(`Main area of interest is ${areaOfInterest}.`);
  if (preferredSetup && preferredSetup !== "-") lines.push(`Preferred setup: ${preferredSetup}.`);
  if (confirmationNeeded && confirmationNeeded !== "-") lines.push(`Confirmation needed: ${confirmationNeeded}.`);
  if (liquidityAbove && liquidityAbove !== "-") lines.push(`Liquidity above sits near ${liquidityAbove}.`);
  if (liquidityBelow && liquidityBelow !== "-") lines.push(`Liquidity below sits near ${liquidityBelow}.`);

  return lines.join(" ");
}

function getBestSetupFromHistory(items) {
  const counts = {};
  for (const t of items) {
    const key = t.entryType || "Unknown";
    counts[key] = (counts[key] || 0) + 1;
  }
  let best = "—";
  let bestCount = 0;
  for (const [k, v] of Object.entries(counts)) {
    if (v > bestCount) {
      best = k;
      bestCount = v;
    }
  }
  return best;
}

function getBestMarketFromHistory(items) {
  const counts = {};
  for (const t of items) {
    const key = t.symbol || "Unknown";
    counts[key] = (counts[key] || 0) + 1;
  }
  let best = "—";
  let bestCount = 0;
  for (const [k, v] of Object.entries(counts)) {
    if (v > bestCount) {
      best = k;
      bestCount = v;
    }
  }
  return best;
}

function CollapsibleCard({
  title,
  right = null,
  isOpen,
  onToggle,
  children,
  className = "",
}) {
  return (
    <div className={`card ${className}`.trim()}>
      <button
        type="button"
        className="collapseToggle"
        onClick={onToggle}
        aria-expanded={isOpen}
        style={{
          width: "100%",
          background: "transparent",
          border: "none",
          padding: 0,
          color: "inherit",
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        <div className="cardHeader" style={{ marginBottom: isOpen ? 14 : 0 }}>
          <div
            className="collapseLeft"
            style={{ display: "flex", alignItems: "center", gap: 10 }}
          >
            <span
              className="collapseArrow"
              style={{ fontSize: 13, opacity: 0.9, minWidth: 12 }}
            >
              {isOpen ? "▼" : "▶"}
            </span>
            <h3 className="collapseTitle" style={{ margin: 0, fontSize: "inherit" }}>
              {title}
            </h3>
          </div>
          <div className="collapseRight">{right}</div>
        </div>
      </button>

      {isOpen ? <div className="collapseBody">{children}</div> : null}
    </div>
  );
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
  const [qualityGrade, setQualityGrade] = useState(restored?.qualityGrade || null);
  const [qualityStars, setQualityStars] = useState(restored?.qualityStars || "");
  const [qualityScore, setQualityScore] = useState(restored?.qualityScore ?? null);

  const [alignmentScore, setAlignmentScore] = useState(restored?.alignmentScore ?? null);
  const [alignmentLabel, setAlignmentLabel] = useState(restored?.alignmentLabel ?? null);
  const [alignmentDetails, setAlignmentDetails] = useState(restored?.alignmentDetails || []);

  const [bias, setBias] = useState(restored?.bias || "-");
  const [structure, setStructure] = useState(restored?.structure || "-");
  const [previousTrend, setPreviousTrend] = useState(restored?.previousTrend || "-");
  const [trendStrength, setTrendStrength] = useState(restored?.trendStrength ?? null);
  const [marketState, setMarketState] = useState(restored?.marketState || "-");
  const [reversalRisk, setReversalRisk] = useState(restored?.reversalRisk || "-");
  const [buyerZone, setBuyerZone] = useState(restored?.buyerZone || "-");
  const [sellerZone, setSellerZone] = useState(restored?.sellerZone || "-");
  const [areaOfInterest, setAreaOfInterest] = useState(restored?.areaOfInterest || "-");
  const [confirmationNeeded, setConfirmationNeeded] = useState(restored?.confirmationNeeded || "-");
  const [preferredSetup, setPreferredSetup] = useState(restored?.preferredSetup || "-");
  const [invalidation, setInvalidation] = useState(restored?.invalidation || "-");
  const [briefSupport, setBriefSupport] = useState(restored?.briefSupport || "-");
  const [briefResistance, setBriefResistance] = useState(restored?.briefResistance || "-");
  const [liquidityBelow, setLiquidityBelow] = useState(restored?.liquidityBelow || "-");
  const [liquidityAbove, setLiquidityAbove] = useState(restored?.liquidityAbove || "-");

  const [scanLoading, setScanLoading] = useState(false);
  const [ranked, setRanked] = useState(restored?.ranked || []);
  const [dailyOutlook, setDailyOutlook] = useState(restored?.dailyOutlook || null);
  const [liveTracker, setLiveTracker] = useState(restored?.liveTracker || null);
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

  const [accountSize, setAccountSize] = useState(restored?.accountSize ?? 100);
  const [riskPercent, setRiskPercent] = useState(restored?.riskPercent ?? 2);

  const [openSections, setOpenSections] = useState(
    restored?.openSections || {
      dailyOutlook: true,
      openTrades: true,
      topPick: false,
      chart: false,
      marketBriefing: true,
      riskCalculator: false,
      scanner: true,
      history: false,
    }
  );
  const toggleSection = useCallback((key) => {
    setOpenSections((prev) => ({
      ...prev,
      [key]: !prev[key],
    }));
  }, []);

  const performance = useMemo(() => calcPerformance(history), [history]);

  const symbolsList = useMemo(
    () => VOLATILITY_OPTIONS.filter((v) => v.symbol !== "ALL").map((v) => v.symbol),
    []
  );

  const selectedName =
    VOLATILITY_OPTIONS.find((v) => v.symbol === selectedSymbol)?.name || selectedSymbol;

  const openMarkets = useMemo(() => {
    const rows = ranked.filter((r) => r.active_trade);
    if (activeTrade && !rows.some((r) => r.symbol === activeTrade.symbol)) {
      return [
        {
          symbol: activeTrade.symbol,
          direction: activeTrade.direction,
          confidence: activeTrade.confidence,
          active_trade: true,
          market_state: marketState,
          preferred_setup: preferredSetup,
          quality_grade: activeTrade.quality_grade || qualityGrade,
          quality_stars: activeTrade.quality_stars || qualityStars,
        },
        ...rows,
      ];
    }
    return rows;
  }, [ranked, activeTrade, marketState, preferredSetup, qualityGrade, qualityStars]);

  const aiExplanation = useMemo(
    () =>
      buildAiExplanation({
        bias,
        structure,
        marketState,
        reversalRisk,
        areaOfInterest,
        preferredSetup,
        confirmationNeeded,
        liquidityAbove,
        liquidityBelow,
      }),
    [
      bias,
      structure,
      marketState,
      reversalRisk,
      areaOfInterest,
      preferredSetup,
      confirmationNeeded,
      liquidityAbove,
      liquidityBelow,
    ]
  );

  const riskAmount = useMemo(() => {
    const size = safeNum(accountSize, 0);
    const pct = safeNum(riskPercent, 0);
    return (size * pct) / 100;
  }, [accountSize, riskPercent]);

  const rrMultiple = useMemo(() => {
    if (entry == null || sl == null || tp2 == null) return null;
    const risk = Math.abs(Number(entry) - Number(sl));
    const reward = Math.abs(Number(tp2) - Number(entry));
    if (!Number.isFinite(risk) || risk <= 0) return null;
    return reward / risk;
  }, [entry, sl, tp2]);

  const estimatedTp1Profit = useMemo(() => {
    if (!riskAmount || entry == null || sl == null || tp1 == null) return null;
    const risk = Math.abs(Number(entry) - Number(sl));
    const reward = Math.abs(Number(tp1) - Number(entry));
    if (!Number.isFinite(risk) || risk <= 0) return null;
    return (reward / risk) * riskAmount;
  }, [riskAmount, entry, sl, tp1]);

  const estimatedTp2Profit = useMemo(() => {
    if (!riskAmount || entry == null || sl == null || tp2 == null) return null;
    const risk = Math.abs(Number(entry) - Number(sl));
    const reward = Math.abs(Number(tp2) - Number(entry));
    if (!Number.isFinite(risk) || risk <= 0) return null;
    return (reward / risk) * riskAmount;
  }, [riskAmount, entry, sl, tp2]);

  const personalBestSetup = useMemo(() => getBestSetupFromHistory(history), [history]);
  const personalBestMarket = useMemo(() => getBestMarketFromHistory(history), [history]);

  const analyzeBusyRef = useRef(false);
  const scanBusyRef = useRef(false);
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const lastOkAtRef = useRef(restored?.lastOkAt || 0);
  const lastLiveAtRef = useRef(restored?.lastLiveAt || 0);
  const openTradeRef = useRef(null);
  const analyzeRequestRef = useRef(0);

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
          qualityGrade,
          qualityStars,
          qualityScore,
          alignmentScore,
          alignmentLabel,
          alignmentDetails,
          bias,
          structure,
          previousTrend,
          trendStrength,
          marketState,
          reversalRisk,
          buyerZone,
          sellerZone,
          areaOfInterest,
          confirmationNeeded,
          preferredSetup,
          invalidation,
          briefSupport,
          briefResistance,
          liquidityBelow,
          liquidityAbove,
          ranked,
          dailyOutlook,
          liveTracker,
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
          accountSize,
          riskPercent,
          openSections,
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
    qualityGrade,
    qualityStars,
    qualityScore,
    alignmentScore,
    alignmentLabel,
    alignmentDetails,
    bias,
    structure,
    previousTrend,
    trendStrength,
    marketState,
    reversalRisk,
    buyerZone,
    sellerZone,
    areaOfInterest,
    confirmationNeeded,
    preferredSetup,
    invalidation,
    briefSupport,
    briefResistance,
    liquidityBelow,
    liquidityAbove,
    ranked,
    dailyOutlook,
    liveTracker,
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
    accountSize,
    riskPercent,
    openSections,
  ]);

  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY_HISTORY, JSON.stringify(history.slice(-500)));
    } catch {}
  }, [history]);

  useEffect(() => {
    const id = setInterval(() => {
      if (analyzeBusyRef.current || scanBusyRef.current) return;

      const now = Date.now();
      const liveAge = now - (lastLiveAtRef.current || 0);
      const okAge = now - (lastOkAtRef.current || 0);

      if (liveAge <= 8000 || okAge <= 30000) setStatus("LIVE");
      else setStatus("IDLE");
    }, 1000);

    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    setCandles([]);
    setSupports([]);
    setResistances([]);
    setPrice(null);
    setDirection("HOLD");
    setConfidence(0);
    setEntry(null);
    setSl(null);
    setTp(null);
    setTp1(null);
    setTp2(null);
    setReason("Loading new market...");
    setEntryType("");
    setQualityGrade(null);
    setQualityStars("");
    setQualityScore(null);
    setAlignmentScore(null);
    setAlignmentLabel(null);
    setAlignmentDetails([]);
    setBias("-");
    setStructure("-");
    setPreviousTrend("-");
    setTrendStrength(null);
    setMarketState("-");
    setReversalRisk("-");
    setBuyerZone("-");
    setSellerZone("-");
    setAreaOfInterest("-");
    setConfirmationNeeded("-");
    setPreferredSetup("-");
    setInvalidation("-");
    setBriefSupport("-");
    setBriefResistance("-");
    setLiquidityBelow("-");
    setLiquidityAbove("-");
    setLastActions([]);
    setLiveTracker(null);
    setStatus("ANALYZING");
    setError("");
  }, [selectedSymbol, timeframe]);

  const topPick = useMemo(() => {
    if (!ranked?.length) return null;
    const candidates = ranked
      .filter((r) => (r.direction === "BUY" || r.direction === "SELL") && safeNum(r.confidence, 0) > 0)
      .sort((a, b) => {
        const ac = safeNum(a.confidence, 0);
        const bc = safeNum(b.confidence, 0);
        if (bc !== ac) return bc - ac;
        return (b.market_state === "TRENDING CLEAN") - (a.market_state === "TRENDING CLEAN");
      });

    return candidates[0] || null;
  }, [ranked]);

  const currentAlignmentChip =
    alignmentScore !== null
      ? `${alignmentLabel ? `${alignmentLabel} ` : ""}${Math.round(alignmentScore)}%`
      : null;

  const marketStateTone =
    marketState === "TRENDING CLEAN"
      ? "pillWin"
      : marketState === "REVERSAL RISK" || marketState === "CHOPPY / NO-TRADE"
      ? "pillLoss"
      : "";

  const reversalTone =
    reversalRisk === "LOW"
      ? "pillWin"
      : reversalRisk === "HIGH"
      ? "pillLoss"
      : "";
  const runAnalyze = useCallback(
    async (symbol, tf) => {
      if (symbol === "ALL") return null;
      if (analyzeBusyRef.current) return null;

      const requestId = ++analyzeRequestRef.current;
      analyzeBusyRef.current = true;
      setError("");
      setStatus("ANALYZING");

      try {
        const data = await withTimeout(analyzeMarket(symbol, tf), 15000, "Analyze");
        if (requestId !== analyzeRequestRef.current) return null;

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
        setQualityGrade(norm.qualityGrade ?? null);
        setQualityStars(norm.qualityStars ?? "");
        setQualityScore(norm.qualityScore ?? null);

        if (norm.price !== null && norm.price !== undefined) setPrice(norm.price);

        setEntryType(norm.entryType || "");
        setAlignmentScore(norm.alignment?.score ?? null);
        setAlignmentLabel(norm.alignment?.label ?? null);
        setAlignmentDetails(Array.isArray(norm.alignment?.details) ? norm.alignment.details : []);

        setBias(norm.briefing.bias);
        setStructure(norm.briefing.structure);
        setPreviousTrend(norm.briefing.previousTrend);
        setTrendStrength(norm.briefing.trendStrength);
        setMarketState(norm.briefing.marketState);
        setReversalRisk(norm.briefing.reversalRisk);
        setBuyerZone(norm.briefing.buyerZone);
        setSellerZone(norm.briefing.sellerZone);
        setAreaOfInterest(norm.briefing.areaOfInterest);
        setConfirmationNeeded(norm.briefing.confirmationNeeded);
        setPreferredSetup(norm.briefing.preferredSetup);
        setInvalidation(norm.briefing.invalidation);
        setBriefSupport(norm.briefing.support);
        setBriefResistance(norm.briefing.resistance);
        setLiquidityBelow(norm.briefing.liquidityBelow);
        setLiquidityAbove(norm.briefing.liquidityAbove);

        setMaxActiveTotal(safeNum(norm.maxActive ?? maxActiveTotal, maxActiveTotal));
        setDailyR(safeNum(norm.raw?.daily_R ?? dailyR, dailyR));
        setActiveTotal(safeNum(norm.raw?.active_total ?? activeTotal, activeTotal));
        setLastActions(norm.actions || []);
        setLiveTracker(norm.liveTracker || null);
        if (norm.dailyOutlook) setDailyOutlook(norm.dailyOutlook);

        if (norm.active && norm.raw?.signal) {
          setActiveTrade({
            symbol,
            timeframe: tf,
            ...norm.raw.signal,
            actions: norm.actions || [],
            price: norm.price ?? null,
            alignment: norm.alignment || null,
          });
        } else if (!norm.active) {
          setActiveTrade((prev) => {
            if (prev?.symbol === symbol && prev?.timeframe === tf) return null;
            return prev;
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
                (t) =>
                  t.symbol === symbol &&
                  t.timeframe === tf &&
                  t.outcome === "OPEN" &&
                  t.openedAt === openedAt
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

        if (
          norm.closedTrade?.outcome &&
          (norm.closedTrade.outcome === "TP2" || norm.closedTrade.outcome === "SL")
        ) {
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

          if (
            openTradeRef.current &&
            openTradeRef.current.symbol === symbol &&
            openTradeRef.current.timeframe === tf
          ) {
            openTradeRef.current = null;
          }

          setActiveTrade(null);
          setLiveTracker(null);
        }

        lastOkAtRef.current = Date.now();
        setStatus("LIVE");
        return norm;
      } catch (e) {
        if (requestId === analyzeRequestRef.current) {
          setError(e?.message || "Analyze failed");
        }
        return null;
      } finally {
        if (requestId === analyzeRequestRef.current) {
          analyzeBusyRef.current = false;
        }
      }
    },
    [dailyR, activeTotal, maxActiveTotal]
  );

    const syncGlobalState = useCallback(async () => {
    try {
      const data = await withTimeout(fetchGlobalState(150), 12000, "Global state");

      const activeTrades = Array.isArray(data?.active_trades) ? data.active_trades : [];
      const backendHistory = Array.isArray(data?.history) ? data.history : [];
      const perf = data?.performance || null;

      setDailyR(safeNum(data?.daily_R ?? 0, 0));
      setActiveTotal(safeNum(data?.active_total ?? 0, 0));
      setMaxActiveTotal(safeNum(data?.max_active_total ?? 5, 5));

      const normalizedHistory = backendHistory.map(normalizeBackendHistoryItem);
      setHistory(normalizedHistory);

      if (activeTrades.length) {
        const activeRows = activeTrades.map((t) => ({
          symbol: t.symbol,
          direction: t.direction || "HOLD",
          confidence: safeNum(t.confidence, 0),
          active_trade: true,
          market_state: t?.meta?.market_state || null,
          preferred_setup: t?.meta?.preferred_setup || t?.mode || t?.entry_type || null,
          quality_grade: t.quality_grade || null,
          quality_stars: t.quality_stars || "",
          entry: t.entry ?? null,
          sl: t.sl ?? null,
          tp: t.tp2 ?? t.tp ?? null,
          entry_type: pickModeLabel(t),
        }));

        setRanked((prev) => {
          const nonActive = Array.isArray(prev) ? prev.filter((r) => !r.active_trade) : [];
          const merged = [...activeRows, ...nonActive];

          const seen = new Set();
          return merged.filter((r) => {
            const key = `${r.symbol}-${r.entry_type || ""}-${r.active_trade ? "A" : "N"}`;
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
          });
        });

        const selectedActive = activeTrades.find(
          (t) => t.symbol === selectedSymbol && t.timeframe === timeframe
        );

        if (selectedActive) {
          setActiveTrade({
            symbol: selectedActive.symbol,
            timeframe: selectedActive.timeframe,
            ...selectedActive,
          });
          setLiveTracker(selectedActive ? {
            status: selectedActive.status || "OPEN",
            direction: selectedActive.direction,
            entry: selectedActive.entry,
            sl: selectedActive.sl,
            tp1: selectedActive.tp1,
            tp2: selectedActive.tp2 ?? selectedActive.tp,
            current_price: price,
            progress_pct: safeNum(selectedActive.progress_pct, 0),
            tp1_hit: !!selectedActive.tp1_hit,
            quality_grade: selectedActive.quality_grade,
            quality_stars: selectedActive.quality_stars,
          } : null);
        }
      } else {
        setActiveTrade(null);
        setLiveTracker(null);
      }

      if (perf) {
        lastOkAtRef.current = Date.now();
      }
    } catch (e) {
      console.error("Global state sync failed:", e);
    }
  }, [selectedSymbol, timeframe, price]);
   useEffect(() => {
    syncGlobalState();

    const id = setInterval(() => {
      if (!document.hidden) {
        syncGlobalState();
      }
    }, 7000);

    return () => clearInterval(id);
  }, [syncGlobalState]); 

  const onScan = useCallback(async () => {
    if (scanBusyRef.current) return null;
    scanBusyRef.current = true;
    setError("");
    setScanLoading(true);
    setStatus("SCANNING");

    try {
      const data = await withTimeout(scanMarkets(symbolsList, timeframe), 15000, "Scan");
      const newRanked = Array.isArray(data?.ranked) ? data.ranked : Array.isArray(data) ? data : [];

      newRanked.sort((a, b) => {
        const ac = safeNum(a.confidence, 0);
        const bc = safeNum(b.confidence, 0);
        if (bc !== ac) return bc - ac;
        return (b.market_state === "TRENDING CLEAN") - (a.market_state === "TRENDING CLEAN");
      });

      setRanked(newRanked);
      setDailyOutlook(data?.daily_outlook || null);
      setDailyR(safeNum(data?.daily_R ?? 0, 0));
      setActiveTotal(safeNum(data?.active_total ?? 0, 0));

      if (data?.max_active_total !== undefined) {
        setMaxActiveTotal(safeNum(data.max_active_total, maxActiveTotal));
      }

      const activeRow = newRanked.find((r) => r.active_trade);
      if (activeRow?.symbol) {
        if (activeRow.symbol !== selectedSymbol) setSelectedSymbol(activeRow.symbol);
        scanBusyRef.current = false;
        return await runAnalyze(activeRow.symbol, timeframe);
      }

      if (autoPickOn) {
        const best = newRanked
          .filter((r) => (r.direction === "BUY" || r.direction === "SELL") && safeNum(r.confidence, 0) > 0)
          .sort((a, b) => safeNum(b.confidence, 0) - safeNum(a.confidence, 0))[0];

        if (best?.symbol) {
          if (best.symbol !== selectedSymbol) setSelectedSymbol(best.symbol);
          scanBusyRef.current = false;
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
      scanBusyRef.current = false;
      setScanLoading(false);
    }
  }, [symbolsList, timeframe, autoPickOn, selectedSymbol, runAnalyze, maxActiveTotal]);

  const onAnalyze = useCallback(async () => {
    if (selectedSymbol === "ALL") {
      await onScan();
      return;
    }
    await runAnalyze(selectedSymbol, timeframe);
  }, [runAnalyze, onScan, selectedSymbol, timeframe]);

  useEffect(() => {
    if (selectedSymbol === "ALL") return;

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

            if (
              (msg.type === "live_chart" || msg.type === "live_tick") &&
              (msg.symbol === selectedSymbol || !msg.symbol) &&
              (msg.timeframe === timeframe || !msg.timeframe)
            ) {
              if (msg.type === "live_chart") {
                const nextSupports = Array.isArray(msg?.levels?.supports) ? msg.levels.supports : [];
                const nextResistances = Array.isArray(msg?.levels?.resistances) ? msg.levels.resistances : [];
                const nextCandles = Array.isArray(msg?.candles) ? msg.candles : [];

                setCandles(nextCandles);
                setSupports(nextSupports);
                setResistances(nextResistances);
              }

              if (msg.price !== null && msg.price !== undefined) {
                setPrice(msg.price);
              }

              setLiveTracker(msg?.live_tracker || null);

              lastLiveAtRef.current = Date.now();
              if (!analyzeBusyRef.current && !scanBusyRef.current) setStatus("LIVE");
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

    if (!scanBusyRef.current) onScan();

    const id = setInterval(() => {
      if (!document.hidden && !scanBusyRef.current) onScan();
    }, everyMs);

    return () => clearInterval(id);
  }, [autoScanOn, autoScanEverySec, onScan]);

  useEffect(() => {
    if (!autoAnalyzeOn) return;
    if (selectedSymbol === "ALL") return;

    const everyMs = clamp(safeNum(autoAnalyzeEverySec, 15), 5, 120) * 1000;

    if (!analyzeBusyRef.current) runAnalyze(selectedSymbol, timeframe);

    const id = setInterval(() => {
      if (!document.hidden && !analyzeBusyRef.current) {
        runAnalyze(selectedSymbol, timeframe);
      }
    }, everyMs);

    return () => clearInterval(id);
  }, [autoAnalyzeOn, autoAnalyzeEverySec, runAnalyze, selectedSymbol, timeframe]);

  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === "visible") {
        if (autoAnalyzeOn && selectedSymbol !== "ALL" && !analyzeBusyRef.current) {
          runAnalyze(selectedSymbol, timeframe);
        }
        if (autoScanOn && !scanBusyRef.current) {
          onScan();
        }
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
            <div className="subtitle">Market Intelligence • Liquidity Mapping • Selective Trade Ideas</div>
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
              <span className="pill">
                Quality {qualityText(activeTrade.quality_grade || qualityGrade, activeTrade.quality_stars || qualityStars)}
              </span>
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

            {liveTracker ? (
              <div className="activeActions" style={{ marginTop: 10 }}>
                <div className="miniLabel">Live Trade Tracker</div>
                <div className="actionsList">
                  <div className="actionChip"><span className="mono">Status: {liveTracker.status || "-"}</span></div>
                  <div className="actionChip"><span className="mono">Progress: {safeNum(liveTracker.progress_pct, 0)}%</span></div>
                  <div className="actionChip"><span className="mono">Price: {liveTracker.current_price ?? "-"}</span></div>
                  <div className="actionChip"><span className="mono">Quality: {qualityText(liveTracker.quality_grade, liveTracker.quality_stars)}</span></div>
                </div>
              </div>
            ) : null}

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
            {selectedSymbol === "ALL" ? "Scan All Markets" : "Analyze (Manual)"}
          </button>
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

        {error ? <div className="errorBox">Error: {error}</div> : null}

        <div className="perfGrid" style={{ marginTop: 14 }}>
          <div className="perfBox"><div className="perfLabel">Today Win %</div><div className="perfValue">{performance.winRate}%</div></div>
          <div className="perfBox"><div className="perfLabel">Closed Trades</div><div className="perfValue">{performance.closedCount}</div></div>
          <div className="perfBox"><div className="perfLabel">Sum R</div><div className="perfValue">{performance.sumR}</div></div>
          <div className="perfBox"><div className="perfLabel">Best Setup</div><div className="perfValue" style={{ fontSize: 14 }}>{personalBestSetup}</div></div>
        </div>
      </div>

      {dailyOutlook ? (
        <CollapsibleCard
          title="Daily Market Outlook"
          isOpen={openSections.dailyOutlook}
          onToggle={() => toggleSection("dailyOutlook")}
          right={<div className="tiny">Scanner overview</div>}
        >
          <div className="signalLine"><span className="smallText">Headline</span><strong>{dailyOutlook.headline || "-"}</strong></div>
          <div className="signalLine"><span className="smallText">Best Market</span><strong>{dailyOutlook.best_symbol || "-"}</strong></div>
          <div className="signalLine"><span className="smallText">Direction</span><strong>{dailyOutlook.best_direction || "-"}</strong></div>
          <div className="signalLine"><span className="smallText">Confidence</span><strong>{safeNum(dailyOutlook.best_confidence, 0)}%</strong></div>
          <div className="signalLine"><span className="smallText">Market State</span><strong>{dailyOutlook.market_state || "-"}</strong></div>
          <div className="signalLine"><span className="smallText">AOI</span><strong>{dailyOutlook.area_of_interest || "-"}</strong></div>
          <div className="reason"><span className="smallText"><b>Preferred Setup:</b> {dailyOutlook.preferred_setup || "-"}</span></div>
          <div className="reason"><span className="smallText"><b>Note:</b> {dailyOutlook.note || "-"}</span></div>
        </CollapsibleCard>
      ) : null}

      <CollapsibleCard
        title="Open Trades"
        isOpen={openSections.openTrades}
        onToggle={() => toggleSection("openTrades")}
        right={<div className="tiny">{openMarkets.length} open</div>}
      >
        {openMarkets.length ? (
          <div className="actionsList">
            {openMarkets.map((m, idx) => (
              <div key={`${m.symbol}-${idx}`} className="actionChip" style={{ padding: 10 }}>
                <span className="mono">{m.symbol}</span>
                <span className="mono">{m.direction || "-"}</span>
                <span className="mono">Conf {safeNum(m.confidence, 0)}%</span>
                <span className="mono">{qualityText(m.quality_grade, m.quality_stars)}</span>
                {m.market_state ? <span className="mono">{m.market_state}</span> : null}
                <button className="btn small" onClick={() => setSelectedSymbol(m.symbol)}>Select</button>
              </div>
            ))}
          </div>
        ) : (
          <div className="emptyRow">No open trades right now.</div>
        )}
      </CollapsibleCard>

      {topPick ? (
        <CollapsibleCard
          title="Top Pick"
          className="topPick"
          isOpen={openSections.topPick}
          onToggle={() => toggleSection("topPick")}
          right={<div className="tiny">Best live setup</div>}
        >
          <div className="topPick">
            <div className="topPickLeft">
              <span className="pill fire">🔥 TOP</span>
              <span className="mono">{topPick.symbol}</span>
              <span className="pill">{topPick.direction}</span>
              <span className="pill">Conf {safeNum(topPick.confidence, 0)}%</span>
              <span className="pill">Quality {qualityText(topPick.quality_grade, topPick.quality_stars)}</span>
              {topPick.market_state ? <span className="pill">{topPick.market_state}</span> : null}
              {topPick.preferred_setup ? <span className="pill">{topPick.preferred_setup}</span> : null}
              <span className="pill">
                Strength {renderStrengthBar(computeStrength(topPick))} {computeStrength(topPick).toFixed(1)} / 10
              </span>
            </div>
            <div className="topPickRight">
              <button className="btn small" onClick={() => setSelectedSymbol(topPick.symbol)}>Select</button>
            </div>
          </div>
        </CollapsibleCard>
      ) : null}

      <div className="mainGrid">
        <CollapsibleCard
          title="Chart"
          isOpen={openSections.chart}
          onToggle={() => toggleSection("chart")}
          right={<div className="tiny">Candles: {candles?.length || 0} • S: {supports?.length || 0} • R: {resistances?.length || 0}</div>}
        >
          <div className="chartWrap">
            {selectedSymbol === "ALL" ? (
              <div
                style={{
                  minHeight: 460,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  opacity: 0.75,
                  padding: 20,
                  textAlign: "center",
                }}
              >
                Scanner mode selected. Pick one market to view the live chart.
              </div>
            ) : (
              <Chart
                key={`${selectedSymbol}-${timeframe}`}
                candles={candles}
                supports={supports}
                resistances={resistances}
                symbol={selectedSymbol}
                timeframe={timeframe}
                entry={entry}
                sl={sl}
                tp={tp}
                tp1={tp1}
                tp2={tp2}
              />
            )}
          </div>
        </CollapsibleCard>

        <CollapsibleCard
          title="Market Briefing"
          isOpen={openSections.marketBriefing}
          onToggle={() => toggleSection("marketBriefing")}
          right={<span className={`badge ${status === "LIVE" ? "live" : ""}`}>{status}</span>}
        >
          <div className="signalLine"><span className="smallText">Market</span><strong>{selectedSymbol} — {selectedName}</strong></div>
          <div className="signalLine"><span className="smallText">Timeframe</span><strong>{timeframe}</strong></div>
          <div className="signalLine"><span className="smallText">Bias</span><strong>{bias}</strong></div>
          <div className="signalLine"><span className="smallText">Structure</span><strong>{structure}</strong></div>
          <div className="signalLine"><span className="smallText">Previous Trend</span><strong>{previousTrend}</strong></div>
          <div className="signalLine"><span className="smallText">Trend Strength</span><strong>{trendStrength === null ? "-" : trendStrength}</strong></div>
          <div className="signalLine"><span className="smallText">Market State</span><strong><span className={`pill ${marketStateTone}`}>{marketState}</span></strong></div>
          <div className="signalLine"><span className="smallText">Reversal Risk</span><strong><span className={`pill ${reversalTone}`}>{reversalRisk}</span></strong></div>
          <div className="signalLine"><span className="smallText">Buyer Zone</span><strong>{buyerZone}</strong></div>
          <div className="signalLine"><span className="smallText">Seller Zone</span><strong>{sellerZone}</strong></div>
          <div className="signalLine"><span className="smallText">AOI</span><strong>{areaOfInterest}</strong></div>
          <div className="signalLine"><span className="smallText">Support</span><strong>{briefSupport}</strong></div>
          <div className="signalLine"><span className="smallText">Resistance</span><strong>{briefResistance}</strong></div>
          <div className="signalLine"><span className="smallText">Liquidity Below</span><strong>{liquidityBelow}</strong></div>
          <div className="signalLine"><span className="smallText">Liquidity Above</span><strong>{liquidityAbove}</strong></div>
          <div className="reason"><span className="smallText"><b>Preferred Setup:</b> {preferredSetup}</span></div>
          <div className="reason"><span className="smallText"><b>Confirmation Needed:</b> {confirmationNeeded}</span></div>
          <div className="reason"><span className="smallText"><b>Invalidation:</b> {invalidation}</span></div>

          <hr style={{ opacity: 0.15, margin: "14px 0" }} />

          <div className="signalLine"><span className="smallText">Signal Direction</span><strong>{direction}</strong></div>
          <div className="signalLine"><span className="smallText">Confidence</span><strong>{confidence}%</strong></div>
          <div className="signalLine"><span className="smallText">Signal Quality</span><strong>{qualityText(qualityGrade, qualityStars)}</strong></div>
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

          <div className="reason" style={{ marginTop: 12 }}>
            <span className="smallText"><b>AI Trade Explanation:</b></span>
            <div className="tiny note" style={{ marginTop: 6 }}>{aiExplanation}</div>
          </div>

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
        </CollapsibleCard>
      </div>

      <CollapsibleCard
        title="Risk Calculator"
        isOpen={openSections.riskCalculator}
        onToggle={() => toggleSection("riskCalculator")}
        right={<div className="tiny">Premium Tool</div>}
      >
        <div className="togglesRow" style={{ alignItems: "flex-end" }}>
          <div className="miniField">
            <span className="miniLabel">Account</span>
            <input className="miniInput" value={accountSize} onChange={(e) => setAccountSize(e.target.value)} inputMode="decimal" />
          </div>

          <div className="miniField">
            <span className="miniLabel">Risk %</span>
            <input className="miniInput" value={riskPercent} onChange={(e) => setRiskPercent(e.target.value)} inputMode="decimal" />
          </div>
        </div>

        <div className="perfGrid" style={{ marginTop: 14 }}>
          <div className="perfBox"><div className="perfLabel">Risk Amount</div><div className="perfValue">${safeNum(riskAmount, 0).toFixed(2)}</div></div>
          <div className="perfBox"><div className="perfLabel">R:R</div><div className="perfValue">{rrMultiple === null ? "—" : rrMultiple.toFixed(2)}</div></div>
          <div className="perfBox"><div className="perfLabel">Est. TP1</div><div className="perfValue">{estimatedTp1Profit === null ? "—" : `$${estimatedTp1Profit.toFixed(2)}`}</div></div>
          <div className="perfBox"><div className="perfLabel">Est. TP2</div><div className="perfValue">{estimatedTp2Profit === null ? "—" : `$${estimatedTp2Profit.toFixed(2)}`}</div></div>
        </div>
      </CollapsibleCard>

      <CollapsibleCard
        title="Scanner"
        className="scannerCard"
        isOpen={openSections.scanner}
        onToggle={() => toggleSection("scanner")}
        right={<div className="tiny">Markets: {ranked?.length || 0}</div>}
      >
        <div className="tableWrap">
          <table className="table">
            <thead>
              <tr>
                <th>Market</th>
                <th>Bias</th>
                <th>State</th>
                <th>Risk</th>
                <th>Action</th>
                <th>Conf</th>
                <th>Quality</th>
                <th>Strength</th>
                <th>AOI</th>
                <th>Setup</th>
                <th>Entry</th>
                <th>SL</th>
              </tr>
            </thead>
            <tbody>
              {ranked?.length ? (
                ranked.map((r, idx) => {
                  const isHot =
                    idx === 0 &&
                    (r.direction === "BUY" || r.direction === "SELL") &&
                    safeNum(r.confidence, 0) > 0;

                  return (
                    <tr key={`${r.symbol || idx}-${idx}`} className={isHot ? "hotRow" : ""}>
                      <td className="mono">
                        {r.symbol ?? "-"} {isHot ? <span className="pill fireTiny">🔥</span> : null}
                        {r.active_trade ? <span className="pill pillWin" style={{ marginLeft: 6 }}>ACTIVE</span> : null}
                      </td>
                      <td>{r.bias ?? "—"}</td>
                      <td className="notes">{r.market_state ?? "—"}</td>
                      <td>{r.reversal_risk ?? "—"}</td>
                      <td>{r.direction ?? "—"}</td>
                      <td>{safeNum(r.confidence, 0)}%</td>
                      <td>{qualityText(r.quality_grade, r.quality_stars)}</td>
                      <td className="mono">
                        {(() => {
                          const s = computeStrength(r);
                          return `${renderStrengthBar(s)} ${s.toFixed(1)} / 10`;
                        })()}
                      </td>
                      <td className="notes">{r.area_of_interest ?? "—"}</td>
                      <td className="notes">{r.preferred_setup ?? r.entry_type ?? "—"}</td>
                      <td>{r.entry ?? "-"}</td>
                      <td>{r.sl ?? "-"}</td>
                    </tr>
                  );
                })
              ) : (
                <tr><td colSpan="12" className="emptyRow">No markets load yet. Scan markets or wait for sync.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </CollapsibleCard>

      {showHistory ? (
        <CollapsibleCard
          title="History & Performance"
          className="historyCard"
          isOpen={openSections.history}
          onToggle={() => toggleSection("history")}
          right={<div className="tiny">Closed: {performance.closedCount} • Wins: {performance.wins} • Losses: {performance.losses} • Win%: {performance.winRate}% • Sum R: {performance.sumR}</div>}
        >
          <div className="perfGrid">
            <div className="perfBox"><div className="perfLabel">Avg R</div><div className="perfValue">{performance.avgR}</div></div>
            <div className="perfBox"><div className="perfLabel">Best Win Streak</div><div className="perfValue">{performance.bestWinStreak}</div></div>
            <div className="perfBox"><div className="perfLabel">Worst Lose Streak</div><div className="perfValue">{performance.bestLoseStreak}</div></div>
            <div className="perfBox"><div className="perfLabel">Last 30 (R)</div><div className="perfValue">{performance.last30R}</div></div>
            <div className="perfBox"><div className="perfLabel">Best Market</div><div className="perfValue">{personalBestMarket}</div></div>
            <div className="perfBox"><div className="perfLabel">Best Setup</div><div className="perfValue" style={{ fontSize: 14 }}>{personalBestSetup}</div></div>
            <div className="perfBox"><div className="perfLabel">Trades Logged</div><div className="perfValue">{history.length}</div></div>
            <div className="perfBox"><div className="perfLabel">TP Style</div><div className="perfValue">{performance.sumR}</div></div>
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
                  <th>Time</th>
                  <th>Market</th>
                  <th>TF</th>
                  <th>Dir</th>
                  <th>Mode</th>
                  <th>Quality</th>
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
                  [...history].slice(-50).reverse().map((t) => (
                    <tr key={t.id} className={t.outcome === "TP2" ? "winRow" : t.outcome === "SL" ? "lossRow" : ""}>
                      <td className="tiny">{t.closedAt ? toIso(t.closedAt) : toIso(t.openedAt)}</td>
                      <td className="mono">{t.symbol}</td>
                      <td>{t.timeframe}</td>
                      <td>{t.direction}</td>
                      <td>{t.entryType || "—"}</td>
                      <td>{qualityText(t.qualityGrade, t.qualityStars)}</td>
                      <td>{safeNum(t.confidence, 0)}%</td>
                      <td>{fmt(t.entry)}</td>
                      <td>{fmt(t.sl)}</td>
                      <td>{fmt(t.tp2 ?? t.tp)}</td>
                      <td><span className={`pill ${t.outcome === "TP2" ? "pillWin" : t.outcome === "SL" ? "pillLoss" : ""}`}>{t.outcome}</span></td>
                      <td>{t.outcome === "TP2" ? `+${t.rMult ?? "?"}` : t.outcome === "SL" ? "-1" : "—"}</td>
                    </tr>
                  ))
                ) : (
                  <tr><td colSpan="12" className="emptyRow">No history yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="tiny note">
            Note: If your backend sleeps, updates can stall until it wakes.
          </div>
        </CollapsibleCard>
      ) : null}
    </div>
  );
}
