import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Chart from "./Chart";
import {
  analyzeMarket,
  scanMarkets,
  fetchGlobalState,
  getBackendWebSocketUrl,
  getBackendUrl,
} from "./api";

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

const TABS = [
  "Overview",
  "Signal",
  "Lot Calculator",
  "Active Trade",
  "Daily Stats",
  "Weekly Stats",
  "Scanner",
  "Chart",
  "History",
  "Health",
];

const LS_KEY_STATE = "dex_bot_smart_dashboard_v1";
const LS_KEY_HISTORY = "dex_bot_history_v4";
const LS_KEY_POINT_VALUES = "dex_bot_point_values_v1";

const DEFAULT_POINT_VALUES = {
  R_10: 1,
  R_25: 1,
  R_50: 1,
  R_75: 1,
  R_100: 1,
};

function safeNum(x, fallback = 0) {
  const n = Number(x);
  return Number.isFinite(n) ? n : fallback;
}

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n));
}

function fmt(x, digits = 5) {
  if (x === null || x === undefined || x === "") return "—";
  const n = Number(x);
  if (!Number.isFinite(n)) return String(x);
  return n.toFixed(digits);
}

function money(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return `$${n.toFixed(2)}`;
}

function percent(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "0%";
  return `${n.toFixed(1)}%`;
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
    sig.entry_timing ||
    sig.mode ||
    sig.type ||
    sig.path ||
    ""
  );
}

function qualityText(grade, stars) {
  if (!grade) return "—";
  return `${grade}${stars ? ` ${stars}` : ""}`;
}

function formatOutcomeLabel(outcome) {
  if (outcome === "TP2") return "TP2 WIN";
  if (outcome === "TP1_ONLY") return "TP1 WIN";
  if (outcome === "BE") return "BREAKEVEN";
  if (outcome === "SL") return "STOP LOSS";
  if (outcome === "OPEN") return "OPEN";
  return outcome || "—";
}

function outcomeTone(outcome) {
  if (outcome === "TP2" || outcome === "TP1_ONLY") return "pillWin";
  if (outcome === "SL") return "pillLoss";
  if (outcome === "BE") return "";
  return "";
}

function tradeActionTone(action) {
  if (action === "ENTER_NOW") return "pillWin";
  if (action === "SKIP") return "pillLoss";
  if (action === "WAIT") return "fire";
  return "";
}

function tradeActionLabel(action) {
  if (action === "ENTER_NOW") return "✅ ENTER NOW";
  if (action === "WAIT") return "⏳ WAIT";
  if (action === "SKIP") return "❌ SKIP";
  return "—";
}

function directionTone(direction) {
  if (direction === "BUY") return "pillWin";
  if (direction === "SELL") return "pillLoss";
  return "";
}

function renderStrengthBar(score) {
  const filled = Math.max(0, Math.min(10, Math.round(score)));
  const empty = 10 - filled;
  return "█".repeat(filled) + "░".repeat(empty);
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
    monthlyBias: brief?.monthly_bias ?? "-",
    weeklyBias: brief?.weekly_bias ?? "-",
    dailyBias: brief?.daily_bias ?? "-",
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

function normalizeWeeklyOutlook(weekly = {}) {
  const total =
    safeNum(weekly?.total_closed, null) ??
    safeNum(weekly?.total, 0);

  const wins = safeNum(weekly?.wins, 0);
  const losses = safeNum(weekly?.losses, 0);
  const breakevens = safeNum(weekly?.breakevens, 0);
  const winRate =
    weekly?.win_rate !== undefined
      ? safeNum(weekly.win_rate, 0)
      : total
      ? (wins / total) * 100
      : 0;

  const lossRate =
    weekly?.loss_rate !== undefined
      ? safeNum(weekly.loss_rate, 0)
      : total
      ? (losses / total) * 100
      : 0;

  return {
    weekKey: weekly?.week_key ?? weekly?.week ?? "-",
    totalClosed: safeNum(total, 0),
    wins,
    losses,
    partials: safeNum(weekly?.partials, 0),
    fullWins: safeNum(weekly?.full_wins, 0),
    breakevens,
    winRate: Number(winRate.toFixed(1)),
    lossRate: Number(lossRate.toFixed(1)),
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
  const weeklyOutlook = normalizeWeeklyOutlook(data?.weekly_outlook || {});
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
    tp2: sig?.tp2 ?? sig?.tp ?? null,
    tp1Hit: !!sig?.tp1_hit,
    tp1RMultiple: sig?.tp1_r_multiple ?? null,
    rMultiple: sig?.r_multiple ?? null,
    reason: sig?.reason ?? data?.reason ?? "—",
    price: data?.price ?? sig?.price ?? null,
    entryType: pickModeLabel(sig),
    entryInstruction: sig?.entry_instruction ?? "",
    entryTiming: sig?.entry_timing ?? "",
    tradeAction: sig?.trade_action ?? "NONE",
    actionMessage: sig?.action_message ?? "",
    qualityGrade: sig?.quality_grade ?? null,
    qualityStars: sig?.quality_stars ?? "",
    qualityScore: safeNum(sig?.quality_score ?? null, null),
    alignment,
    briefing,
    weeklyOutlook,
    active: !!data?.active,
    actions: Array.isArray(data?.actions) ? data.actions : [],
    closedTrade: data?.closed_trade ?? null,
    liveTracker: data?.live_tracker ?? null,
    dailyOutlook: data?.daily_outlook ?? null,
    raw: data,
    maxActive,
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
    outcome: t.status === "CLOSED" ? t.outcome || "CLOSED" : "OPEN",
    closePrice: safeNum(t.closed_price, null),
    rMult: safeNum(t.r_multiple, null),
    tp1RMult: safeNum(t.tp1_r_multiple, null),
    reason: t.reason || "—",
    tp1Hit: !!t.tp1_hit,
    entryType: pickModeLabel(t),
    entryInstruction: t.entry_instruction ?? "",
    entryTiming: t.entry_timing ?? "",
    tradeAction: t.trade_action ?? "NONE",
    actionMessage: t.action_message ?? "",
    qualityGrade: t.quality_grade ?? null,
    qualityStars: t.quality_stars ?? "",
    weekKey: t.week_key ?? null,
    dayKey: t.day_key ?? null,
  };
}
function calcStats(items, scope = "all") {
  const now = new Date();
  const todayKey = now.toISOString().slice(0, 10);

  const getWeekKey = (d) => {
    const date = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    const dayNum = date.getUTCDay() || 7;
    date.setUTCDate(date.getUTCDate() + 4 - dayNum);
    const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
    const weekNo = Math.ceil((((date - yearStart) / 86400000) + 1) / 7);
    return `${date.getUTCFullYear()}-W${String(weekNo).padStart(2, "0")}`;
  };

  const currentWeekKey = getWeekKey(now);

  const scoped = items.filter((x) => {
    const ts = x.closedAt || x.openedAt;
    if (!ts) return scope === "all";
    const d = new Date(Number(ts) * 1000);

    if (scope === "today") {
      return d.toISOString().slice(0, 10) === todayKey;
    }

    if (scope === "week") {
      return getWeekKey(d) === currentWeekKey;
    }

    return true;
  });

  const closed = scoped.filter((x) =>
    x.outcome === "TP2" ||
    x.outcome === "TP1_ONLY" ||
    x.outcome === "BE" ||
    x.outcome === "SL"
  );

  const open = scoped.filter((x) => x.outcome === "OPEN").length;
  const wins = closed.filter((x) => x.outcome === "TP2" || x.outcome === "TP1_ONLY").length;
  const losses = closed.filter((x) => x.outcome === "SL").length;
  const breakevens = closed.filter((x) => x.outcome === "BE").length;
  const tp1Wins = closed.filter((x) => x.outcome === "TP1_ONLY").length;
  const tp2Wins = closed.filter((x) => x.outcome === "TP2").length;
  const total = closed.length;

  const winRate = total ? (wins / total) * 100 : 0;
  const lossRate = total ? (losses / total) * 100 : 0;

  let sumR = 0;
  for (const t of closed) {
    if (t.outcome === "TP2") sumR += safeNum(t.rMult, 1);
    if (t.outcome === "TP1_ONLY") sumR += safeNum(t.tp1RMult, 0.7);
    if (t.outcome === "BE") sumR += 0;
    if (t.outcome === "SL") sumR -= 1;
  }

  return {
    totalClosed: total,
    open,
    trades: scoped.length,
    wins,
    losses,
    breakevens,
    tp1Wins,
    tp2Wins,
    winRate: Number(winRate.toFixed(1)),
    lossRate: Number(lossRate.toFixed(1)),
    sumR: Number(sumR.toFixed(2)),
    avgR: total ? Number((sumR / total).toFixed(2)) : 0,
  };
}

function getBestFromHistory(items, field) {
  const counts = {};
  for (const t of items) {
    const key = t[field] || "Unknown";
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

function computeStrength(row) {
  const conf = safeNum(row.confidence, 0);
  const action = row.trade_action || row.tradeAction || "";
  const direction = row.direction;

  const actionBonus =
    action === "ENTER_NOW"
      ? 1.5
      : action === "WAIT"
      ? 0.6
      : action === "SKIP"
      ? -1.0
      : direction === "BUY" || direction === "SELL"
      ? 0.5
      : 0;

  const stateBonus =
    row.market_state === "TRENDING CLEAN"
      ? 1.0
      : row.market_state === "TRENDING PULLBACK"
      ? 0.7
      : row.market_state === "WEAK TREND"
      ? 0.25
      : 0.1;

  const riskPenalty =
    row.reversal_risk === "HIGH"
      ? -1.3
      : row.reversal_risk === "MEDIUM"
      ? -0.6
      : 0;

  const qualityBonus =
    row.quality_grade === "A+" || row.quality_grade === "A"
      ? 0.9
      : row.quality_grade === "B+"
      ? 0.5
      : 0;

  const raw = (conf / 100) * 7 + actionBonus + stateBonus + riskPenalty + qualityBonus;
  return clamp(raw, 0, 10);
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

function buildAiSentimentModel({
  ranked = [],
  dailyOutlook = null,
  bias = "-",
  marketState = "-",
  reversalRisk = "-",
  trendStrength = null,
  activeTotal = 0,
  maxActiveTotal = 5,
}) {
  const tradable = ranked.filter(
    (r) =>
      (r.direction === "BUY" || r.direction === "SELL") &&
      safeNum(r.confidence, 0) > 0 &&
      r.trade_action !== "SKIP"
  );

  let score = 50;

  if (dailyOutlook?.best_direction === "BUY" || dailyOutlook?.best_direction === "SELL") score += 10;
  if (bias === "UP" || bias === "DOWN") score += 6;
  if (marketState === "TRENDING CLEAN") score += 12;
  else if (marketState === "TRENDING PULLBACK") score += 7;
  else if (marketState === "WEAK TREND") score -= 4;
  else if (marketState === "CHOPPY / NO-TRADE") score -= 12;

  if (reversalRisk === "LOW") score += 6;
  else if (reversalRisk === "MEDIUM") score -= 5;
  else if (reversalRisk === "HIGH") score -= 12;

  if (trendStrength !== null) {
    const ts = safeNum(trendStrength, 0);
    if (ts >= 0.2) score += 8;
    else if (ts >= 0.1) score += 4;
    else score -= 3;
  }

  if (tradable.length >= 3) score += 8;
  else if (tradable.length === 2) score += 5;
  else if (tradable.length === 1) score += 2;
  else score -= 8;

  if (activeTotal >= maxActiveTotal) score -= 5;

  score = clamp(score, 0, 100);

  let sentiment = "NEUTRAL";
  let tone = "";
  if (score >= 72) {
    sentiment = "FAVORABLE";
    tone = "pillWin";
  } else if (score <= 38) {
    sentiment = "CAUTION";
    tone = "pillLoss";
  }

  const summary =
    sentiment === "FAVORABLE"
      ? "Conditions are supportive for selective setups."
      : sentiment === "CAUTION"
      ? "Market quality is weak. Protection matters more than aggression."
      : "Mixed conditions. Stay selective and wait for clean confirmation.";

  return {
    score,
    sentiment,
    tone,
    tradableCount: tradable.length,
    summary,
  };
}
function buildCommandCenter(activeTrade, liveTracker, price) {
  if (!activeTrade) {
    return {
      title: "No active trade",
      statusTone: "",
      secured: false,
      checklist: [
        "Wait for a clean ENTER_NOW signal.",
        "Check lot size before entering manually.",
        "Avoid forcing trades when action says WAIT or SKIP.",
      ],
      stats: [],
    };
  }

  const entry = safeNum(activeTrade.entry, null);
  const sl = safeNum(activeTrade.sl, null);
  const tp1 = safeNum(activeTrade.tp1, null);
  const tp2 = safeNum(activeTrade.tp2 ?? activeTrade.tp, null);
  const current = safeNum(liveTracker?.current_price ?? price, null);
  const progress = safeNum(liveTracker?.progress_pct, 0);
  const openedAt = activeTrade.opened_at || activeTrade.openedAt || null;
  const minsOpen = openedAt ? Math.max(0, Math.round((Date.now() / 1000 - openedAt) / 60)) : null;

  const tp1Hit = !!activeTrade.tp1_hit || !!liveTracker?.tp1_hit;
  const secured = tp1Hit && entry !== null && sl !== null && Math.abs(sl - entry) < 0.00001;

  let riskDistance = null;
  let rewardDistance = null;
  if (entry !== null && sl !== null) riskDistance = Math.abs(entry - sl);
  if (entry !== null && tp2 !== null) rewardDistance = Math.abs(tp2 - entry);

  const checklist = [];

  if (tp1Hit) {
    checklist.push("TP1 hit ✅ — position should be protected.");
    checklist.push(secured ? "SL appears moved to entry 🔒." : "Check if SL has moved to entry.");
    checklist.push("Runner active: let TP2 work unless structure changes.");
  } else {
    checklist.push("TP1 not hit yet — main focus is protecting risk.");
    checklist.push("Do not move SL too early before TP1.");
  }

  if (progress >= 75) checklist.push("Trade is in strong progress territory.");
  else if (progress >= 40) checklist.push("Trade is moving, but still needs follow-through.");
  else checklist.push("Trade is early. Stay patient and let structure develop.");

  if (liveTracker?.status) checklist.push(`Live status: ${liveTracker.status}.`);

  return {
    title: `${activeTrade.symbol} ${activeTrade.direction} Command Center`,
    statusTone: secured ? "pillWin" : tp1Hit ? "fire" : "",
    secured,
    checklist,
    stats: [
      { label: "Progress", value: `${progress}%` },
      { label: "Minutes Open", value: minsOpen === null ? "—" : String(minsOpen) },
      { label: "Current", value: current === null ? "—" : fmt(current) },
      { label: "Entry", value: entry === null ? "—" : fmt(entry) },
      { label: "SL", value: sl === null ? "—" : fmt(sl) },
      { label: "TP1", value: tp1Hit ? "Hit ✅" : tp1 === null ? "—" : fmt(tp1) },
      { label: "TP2", value: tp2 === null ? "—" : fmt(tp2) },
      { label: "Risk Dist.", value: riskDistance === null ? "—" : fmt(riskDistance) },
      { label: "Reward Dist.", value: rewardDistance === null ? "—" : fmt(rewardDistance) },
      { label: "Secured", value: secured ? "YES 🔒" : "NO" },
      { label: "Quality", value: qualityText(activeTrade.quality_grade, activeTrade.quality_stars) },
      { label: "Mode", value: pickModeLabel(activeTrade) || "—" },
    ],
  };
}

function calculateLotPlan({
  accountSize,
  riskPercent,
  entry,
  sl,
  tp1,
  tp2,
  pointValue,
}) {
  const account = safeNum(accountSize, 0);
  const riskPct = safeNum(riskPercent, 0);
  const riskAmount = (account * riskPct) / 100;

  const e = safeNum(entry, null);
  const stop = safeNum(sl, null);
  const target1 = safeNum(tp1, null);
  const target2 = safeNum(tp2, null);
  const pv = safeNum(pointValue, 1);

  if (e === null || stop === null || pv <= 0) {
    return {
      valid: false,
      riskAmount,
      suggestedLot: null,
      lossAtSl: null,
      profitTp1: null,
      profitTp2: null,
      tp1R: null,
      tp2R: null,
      riskDistance: null,
    };
  }

  const riskDistance = Math.abs(e - stop);
  if (riskDistance <= 0) {
    return {
      valid: false,
      riskAmount,
      suggestedLot: null,
      lossAtSl: null,
      profitTp1: null,
      profitTp2: null,
      tp1R: null,
      tp2R: null,
      riskDistance,
    };
  }

  const suggestedLot = riskAmount / (riskDistance * pv);
  const lossAtSl = riskDistance * suggestedLot * pv;

  let profitTp1 = null;
  let profitTp2 = null;
  let tp1R = null;
  let tp2R = null;

  if (target1 !== null) {
    const reward1 = Math.abs(target1 - e);
    profitTp1 = reward1 * suggestedLot * pv;
    tp1R = reward1 / riskDistance;
  }

  if (target2 !== null) {
    const reward2 = Math.abs(target2 - e);
    profitTp2 = reward2 * suggestedLot * pv;
    tp2R = reward2 / riskDistance;
  }

  return {
    valid: true,
    riskAmount,
    suggestedLot,
    lossAtSl,
    profitTp1,
    profitTp2,
    tp1R,
    tp2R,
    riskDistance,
  };
}

function buildCopySignal({
  selectedSymbol,
  timeframe,
  direction,
  tradeAction,
  entry,
  sl,
  tp1,
  tp2,
  confidence,
  qualityGrade,
  actionMessage,
  entryInstruction,
  entryTiming,
  lotPlan,
}) {
  return [
    `DEXTRADEZBOT AI SIGNAL`,
    `${selectedSymbol} (${timeframe})`,
    `Direction: ${direction}`,
    `Action: ${tradeAction}`,
    `Confidence: ${confidence}%`,
    `Quality: ${qualityGrade || "-"}`,
    ``,
    `Entry: ${entry ?? "-"}`,
    `SL: ${sl ?? "-"}`,
    `TP1: ${tp1 ?? "-"}`,
    `TP2: ${tp2 ?? "-"}`,
    ``,
    `Suggested Lot: ${lotPlan?.suggestedLot ? lotPlan.suggestedLot.toFixed(3) : "-"}`,
    `Risk: ${lotPlan?.lossAtSl ? money(lotPlan.lossAtSl) : "-"}`,
    `TP1 Est: ${lotPlan?.profitTp1 ? money(lotPlan.profitTp1) : "-"}`,
    `TP2 Est: ${lotPlan?.profitTp2 ? money(lotPlan.profitTp2) : "-"}`,
    ``,
    `Timing: ${entryTiming || "-"}`,
    `Instruction: ${actionMessage || entryInstruction || "-"}`,
  ].join("\n");
}

function StatBox({ label, value, tone = "", small = false }) {
  return (
    <div className="perfBox">
      <div className="perfLabel">{label}</div>
      <div className={`perfValue ${tone}`.trim()} style={{ fontSize: small ? 14 : undefined }}>
        {value}
      </div>
    </div>
  );
}

function SignalLine({ label, value, tone = "" }) {
  return (
    <div className="signalLine">
      <span className="smallText">{label}</span>
      <strong className={tone}>{value}</strong>
    </div>
  );
}

function SmartCard({ title, right = null, children, className = "" }) {
  return (
    <div className={`card ${className}`.trim()}>
      <div className="cardHeader">
        <h3 style={{ margin: 0 }}>{title}</h3>
        <div>{right}</div>
      </div>
      {children}
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

  const restoredPointValues = useMemo(() => {
    try {
      const raw = localStorage.getItem(LS_KEY_POINT_VALUES);
      const parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === "object"
        ? { ...DEFAULT_POINT_VALUES, ...parsed }
        : DEFAULT_POINT_VALUES;
    } catch {
      return DEFAULT_POINT_VALUES;
    }
  }, []);

  const [selectedSymbol, setSelectedSymbol] = useState(restored?.selectedSymbol || "R_10");
  const [timeframe, setTimeframe] = useState(restored?.timeframe || "5m");
  const [activeTab, setActiveTab] = useState(restored?.activeTab || "Overview");

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

  const [tradeAction, setTradeAction] = useState(restored?.tradeAction || "NONE");
  const [actionMessage, setActionMessage] = useState(restored?.actionMessage || "");
  const [entryInstruction, setEntryInstruction] = useState(restored?.entryInstruction || "");
  const [entryTiming, setEntryTiming] = useState(restored?.entryTiming || "");

  const [entryType, setEntryType] = useState(restored?.entryType || "");
  const [qualityGrade, setQualityGrade] = useState(restored?.qualityGrade || null);
  const [qualityStars, setQualityStars] = useState(restored?.qualityStars || "");
  const [qualityScore, setQualityScore] = useState(restored?.qualityScore ?? null);

  const [alignmentScore, setAlignmentScore] = useState(restored?.alignmentScore ?? null);
  const [alignmentLabel, setAlignmentLabel] = useState(restored?.alignmentLabel ?? null);
  const [alignmentDetails, setAlignmentDetails] = useState(restored?.alignmentDetails || []);

  const [bias, setBias] = useState(restored?.bias || "-");
  const [monthlyBias, setMonthlyBias] = useState(restored?.monthlyBias || "-");
  const [weeklyBias, setWeeklyBias] = useState(restored?.weeklyBias || "-");
  const [dailyBias, setDailyBias] = useState(restored?.dailyBias || "-");
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
  const [weeklyOutlook, setWeeklyOutlook] = useState(restored?.weeklyOutlook || null);
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
  const [pointValues, setPointValues] = useState(restoredPointValues);

  const analyzeBusyRef = useRef(false);
  const scanBusyRef = useRef(false);
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const lastOkAtRef = useRef(restored?.lastOkAt || 0);
  const lastLiveAtRef = useRef(restored?.lastLiveAt || 0);
  const openTradeRef = useRef(null);
  const analyzeRequestRef = useRef(0);
   const symbolsList = useMemo(
    () => VOLATILITY_OPTIONS.map((v) => v.symbol),
    []
  );

  const selectedName =
    VOLATILITY_OPTIONS.find((v) => v.symbol === selectedSymbol)?.name || selectedSymbol;

  const selectedPointValue = safeNum(pointValues?.[selectedSymbol], 1);

  const dailyStats = useMemo(() => calcStats(history, "today"), [history]);
  const weeklyStats = useMemo(() => calcStats(history, "week"), [history]);
  const allStats = useMemo(() => calcStats(history, "all"), [history]);

  const personalBestSetup = useMemo(() => getBestFromHistory(history, "entryType"), [history]);
  const personalBestMarket = useMemo(() => getBestFromHistory(history, "symbol"), [history]);

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

  const lotPlan = useMemo(
    () =>
      calculateLotPlan({
        accountSize,
        riskPercent,
        entry,
        sl,
        tp1,
        tp2,
        pointValue: selectedPointValue,
      }),
    [accountSize, riskPercent, entry, sl, tp1, tp2, selectedPointValue]
  );

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

  const aiSentiment = useMemo(
    () =>
      buildAiSentimentModel({
        ranked,
        dailyOutlook,
        bias,
        marketState,
        reversalRisk,
        trendStrength,
        activeTotal,
        maxActiveTotal,
      }),
    [ranked, dailyOutlook, bias, marketState, reversalRisk, trendStrength, activeTotal, maxActiveTotal]
  );

  const commandCenter = useMemo(
    () => buildCommandCenter(activeTrade, liveTracker, price),
    [activeTrade, liveTracker, price]
  );

  const _openMarkets = useMemo(() => {
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
          trade_action: activeTrade.trade_action || tradeAction,
        },
        ...rows,
      ];
    }

    return rows;
  }, [ranked, activeTrade, marketState, preferredSetup, qualityGrade, qualityStars, tradeAction]);

  const topPick = useMemo(() => {
    if (!ranked?.length) return null;

    const candidates = ranked
      .filter(
        (r) =>
          (r.direction === "BUY" || r.direction === "SELL") &&
          safeNum(r.confidence, 0) > 0
      )
      .sort((a, b) => {
        const aEnter = a.trade_action === "ENTER_NOW" ? 1 : 0;
        const bEnter = b.trade_action === "ENTER_NOW" ? 1 : 0;
        if (bEnter !== aEnter) return bEnter - aEnter;

        const ac = safeNum(a.confidence, 0);
        const bc = safeNum(b.confidence, 0);
        if (bc !== ac) return bc - ac;

        return (b.market_state === "TRENDING CLEAN") - (a.market_state === "TRENDING CLEAN");
      });

    return candidates[0] || null;
  }, [ranked]);

  const backendUrl = useMemo(() => getBackendUrl(), []);

  const copyText = useCallback(async (text) => {
    try {
      await navigator.clipboard.writeText(text);
      setStatus("COPIED");
      setTimeout(() => setStatus("LIVE"), 1200);
    } catch {
      setError("Copy failed. Your browser may block clipboard access.");
    }
  }, []);

  const copySignalText = useMemo(
    () =>
      buildCopySignal({
        selectedSymbol,
        timeframe,
        direction,
        tradeAction,
        entry,
        sl,
        tp1,
        tp2,
        confidence,
        qualityGrade,
        actionMessage,
        entryInstruction,
        entryTiming,
        lotPlan,
      }),
    [
      selectedSymbol,
      timeframe,
      direction,
      tradeAction,
      entry,
      sl,
      tp1,
      tp2,
      confidence,
      qualityGrade,
      actionMessage,
      entryInstruction,
      entryTiming,
      lotPlan,
    ]
  );

  useEffect(() => {
    try {
      localStorage.setItem(
        LS_KEY_STATE,
        JSON.stringify({
          selectedSymbol,
          timeframe,
          activeTab,
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
          tradeAction,
          actionMessage,
          entryInstruction,
          entryTiming,
          entryType,
          qualityGrade,
          qualityStars,
          qualityScore,
          alignmentScore,
          alignmentLabel,
          alignmentDetails,
          bias,
          monthlyBias,
          weeklyBias,
          dailyBias,
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
          weeklyOutlook,
          liveTracker,
          dailyR,
          activeTotal,
          maxActiveTotal,
          autoScanOn,
          autoPickOn,
          autoScanEverySec,
          autoAnalyzeOn,
          autoAnalyzeEverySec,
          activeTrade,
          lastActions,
          accountSize,
          riskPercent,
          lastOkAt: lastOkAtRef.current,
          lastLiveAt: lastLiveAtRef.current,
        })
      );
    } catch {}
  }, [
    selectedSymbol,
    timeframe,
    activeTab,
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
    tradeAction,
    actionMessage,
    entryInstruction,
    entryTiming,
    entryType,
    qualityGrade,
    qualityStars,
    qualityScore,
    alignmentScore,
    alignmentLabel,
    alignmentDetails,
    bias,
    monthlyBias,
    weeklyBias,
    dailyBias,
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
    weeklyOutlook,
    liveTracker,
    dailyR,
    activeTotal,
    maxActiveTotal,
    autoScanOn,
    autoPickOn,
    autoScanEverySec,
    autoAnalyzeOn,
    autoAnalyzeEverySec,
    activeTrade,
    lastActions,
    accountSize,
    riskPercent,
  ]);

  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY_HISTORY, JSON.stringify(history.slice(-500)));
    } catch {}
  }, [history]);

  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY_POINT_VALUES, JSON.stringify(pointValues));
    } catch {}
  }, [pointValues]); 
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
    setTradeAction("NONE");
    setActionMessage("");
    setEntryInstruction("");
    setEntryTiming("");
    setEntryType("");
    setQualityGrade(null);
    setQualityStars("");
    setQualityScore(null);
    setAlignmentScore(null);
    setAlignmentLabel(null);
    setAlignmentDetails([]);
    setBias("-");
    setMonthlyBias("-");
    setWeeklyBias("-");
    setDailyBias("-");
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

  const updateFromAnalyze = useCallback(
    (norm, symbol, tf) => {
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

      setTradeAction(norm.tradeAction || "NONE");
      setActionMessage(norm.actionMessage || "");
      setEntryInstruction(norm.entryInstruction || "");
      setEntryTiming(norm.entryTiming || "");

      setQualityGrade(norm.qualityGrade ?? null);
      setQualityStars(norm.qualityStars ?? "");
      setQualityScore(norm.qualityScore ?? null);

      if (norm.price !== null && norm.price !== undefined) setPrice(norm.price);

      setEntryType(norm.entryType || "");
      setAlignmentScore(norm.alignment?.score ?? null);
      setAlignmentLabel(norm.alignment?.label ?? null);
      setAlignmentDetails(Array.isArray(norm.alignment?.details) ? norm.alignment.details : []);

      setBias(norm.briefing.bias);
      setMonthlyBias(norm.briefing.monthlyBias);
      setWeeklyBias(norm.briefing.weeklyBias);
      setDailyBias(norm.briefing.dailyBias);
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
      if (norm.weeklyOutlook) setWeeklyOutlook(norm.weeklyOutlook);

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

      if (norm.closedTrade?.outcome) {
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
                tp1Hit: !!norm.closedTrade?.tp1_hit,
                rMult: safeNum(norm.closedTrade?.r_multiple, t.rMult),
                tp1RMult: safeNum(norm.closedTrade?.tp1_r_multiple, t.tp1RMult),
              };
              return next.slice(-500);
            }
          }

          return next.slice(-500);
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
    },
    [activeTotal, dailyR, maxActiveTotal]
  );

  const runAnalyze = useCallback(
    async (symbol, tf) => {
      if (analyzeBusyRef.current) return null;

      const requestId = ++analyzeRequestRef.current;
      analyzeBusyRef.current = true;
      setError("");
      setStatus("ANALYZING");

      try {
        const data = await withTimeout(analyzeMarket(symbol, tf), 15000, "Analyze");
        if (requestId !== analyzeRequestRef.current) return null;

        const norm = normalizeAnalyzeResponse(data);
        updateFromAnalyze(norm, symbol, tf);

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
    [updateFromAnalyze]
  );

  const syncGlobalState = useCallback(async () => {
    try {
      const data = await withTimeout(fetchGlobalState(200), 12000, "Global state");

      const activeTrades = Array.isArray(data?.active_trades) ? data.active_trades : [];
      const backendHistory = Array.isArray(data?.history) ? data.history : [];
      const weekly = data?.weekly || data?.weekly_outlook || null;

      setDailyR(safeNum(data?.daily_R ?? 0, 0));
      setActiveTotal(safeNum(data?.active_total ?? 0, 0));
      setMaxActiveTotal(safeNum(data?.max_active_total ?? 5, 5));
      if (weekly) setWeeklyOutlook(normalizeWeeklyOutlook(weekly));

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
          tp1: t.tp1 ?? null,
          tp2: t.tp2 ?? t.tp ?? null,
          entry_type: pickModeLabel(t),
          trade_action: t.trade_action ?? "NONE",
          action_message: t.action_message ?? "",
          reversal_risk: t?.meta?.reversal_risk || null,
        }));

        setRanked((prev) => {
          const nonActive = Array.isArray(prev) ? prev.filter((r) => !r.active_trade) : [];
          return [...activeRows, ...nonActive];
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
          setLiveTracker({
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
          });
        }
      } else {
        setActiveTrade(null);
        setLiveTracker(null);
      }

      lastOkAtRef.current = Date.now();
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
        const aEnter = a.trade_action === "ENTER_NOW" ? 1 : 0;
        const bEnter = b.trade_action === "ENTER_NOW" ? 1 : 0;
        if (bEnter !== aEnter) return bEnter - aEnter;

        const ac = safeNum(a.confidence, 0);
        const bc = safeNum(b.confidence, 0);
        if (bc !== ac) return bc - ac;

        return (b.market_state === "TRENDING CLEAN") - (a.market_state === "TRENDING CLEAN");
      });

      setRanked(newRanked);
      setDailyOutlook(data?.daily_outlook || null);
      if (data?.weekly_outlook) setWeeklyOutlook(normalizeWeeklyOutlook(data.weekly_outlook));
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
          .sort((a, b) => {
            const aEnter = a.trade_action === "ENTER_NOW" ? 1 : 0;
            const bEnter = b.trade_action === "ENTER_NOW" ? 1 : 0;
            if (bEnter !== aEnter) return bEnter - aEnter;
            return safeNum(b.confidence, 0) - safeNum(a.confidence, 0);
          })[0];

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
              if (msg?.weekly_outlook) {
                setWeeklyOutlook(normalizeWeeklyOutlook(msg.weekly_outlook));
              }

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
        if (autoAnalyzeOn && !analyzeBusyRef.current) {
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

  const setMarketAndAnalyze = useCallback(
    async (symbol) => {
      setSelectedSymbol(symbol);
      setActiveTab("Signal");
      await runAnalyze(symbol, timeframe);
    },
    [runAnalyze, timeframe]
  );

  const updatePointValue = useCallback((symbol, value) => {
    setPointValues((prev) => ({
      ...prev,
      [symbol]: value,
    }));
  }, []);

  const resetPointValues = useCallback(() => {
    setPointValues(DEFAULT_POINT_VALUES);
  }, []);

  const activeBanner =
    activeTrade && (activeTrade.direction === "BUY" || activeTrade.direction === "SELL");

  const activeAlignmentChip =
    activeTrade?.alignment?.score !== undefined && activeTrade?.alignment?.score !== null
      ? `${activeTrade?.alignment?.label ? `${activeTrade.alignment.label} ` : ""}${Math.round(activeTrade.alignment.score)}%`
      : null;

  const tradeChecklist = [
    tradeAction === "ENTER_NOW" ? "Action is ENTER_NOW ✅" : `Action is ${tradeAction || "NONE"} — be careful`,
    direction === "BUY" || direction === "SELL" ? `Direction is ${direction} ✅` : "No BUY/SELL direction yet",
    entry !== null && sl !== null ? "Entry and SL are available ✅" : "Entry/SL missing",
    tp1 !== null && tp2 !== null ? "TP1 and TP2 are available ✅" : "Targets missing",
    lotPlan.valid ? `Suggested lot calculated: ${lotPlan.suggestedLot.toFixed(3)}` : "Lot calculation not ready",
    safeNum(riskPercent, 0) <= 3 ? "Risk percent is controlled ✅" : "Risk percent is high",
  ];
  const renderOverview = () => (
    <>
      <div className="perfGrid">
        <StatBox label="Decision" value={tradeActionLabel(tradeAction)} tone={tradeActionTone(tradeAction)} />
        <StatBox label="Direction" value={direction} tone={directionTone(direction)} />
        <StatBox label="Confidence" value={`${confidence}%`} />
        <StatBox label="Quality" value={qualityText(qualityGrade, qualityStars)} small />
        <StatBox label="Daily Win %" value={percent(dailyStats.winRate)} />
        <StatBox label="Weekly Win %" value={percent(weeklyStats.winRate)} />
        <StatBox label="Daily R" value={dailyR} />
        <StatBox label="Active" value={`${activeTotal}/${maxActiveTotal}`} />
      </div>

      {topPick ? (
        <SmartCard
          title="Top Market Right Now"
          className="topPickCard"
          right={<span className={`pill ${tradeActionTone(topPick.trade_action)}`}>{tradeActionLabel(topPick.trade_action)}</span>}
        >
          <div className="topPick">
            <div className="topPickLeft">
              <span className="pill fire">🔥 TOP</span>
              <span className="mono">{topPick.symbol}</span>
              <span className={`pill ${directionTone(topPick.direction)}`}>{topPick.direction}</span>
              <span className="pill">Conf {safeNum(topPick.confidence, 0)}%</span>
              <span className="pill">Quality {qualityText(topPick.quality_grade, topPick.quality_stars)}</span>
              {topPick.market_state ? <span className="pill">{topPick.market_state}</span> : null}
              <span className="pill">
                Strength {renderStrengthBar(computeStrength(topPick))} {computeStrength(topPick).toFixed(1)} / 10
              </span>
            </div>
            <div className="topPickRight">
              <button className="btn small" onClick={() => setMarketAndAnalyze(topPick.symbol)}>Open Signal</button>
            </div>
          </div>
        </SmartCard>
      ) : null}

      <SmartCard
        title="AI Sentiment"
        className="sentimentCard"
        right={<span className={`pill ${aiSentiment.tone}`}>{aiSentiment.sentiment}</span>}
      >
        <SignalLine label="Sentiment Score" value={`${aiSentiment.score}/100`} />
        <SignalLine label="Tradable Markets" value={aiSentiment.tradableCount} />
        <SignalLine label="Current Bias" value={bias} />
        <SignalLine label="Current State" value={marketState} tone={marketStateTone} />
        <div className="reason"><span className="smallText"><b>Summary:</b> {aiSentiment.summary}</span></div>
      </SmartCard>
    </>
  );

  const renderSignal = () => (
    <SmartCard
      title="Smart Signal Command Center"
      className="commandCenterCard"
      right={<span className={`pill ${tradeActionTone(tradeAction)}`}>{tradeActionLabel(tradeAction)}</span>}
    >
      <div className="perfGrid">
        <StatBox label="Market" value={selectedSymbol} />
        <StatBox label="Timeframe" value={timeframe} />
        <StatBox label="Direction" value={direction} tone={directionTone(direction)} />
        <StatBox label="Confidence" value={`${confidence}%`} />
        <StatBox label="Quality" value={qualityText(qualityGrade, qualityStars)} small />
        <StatBox label="Mode" value={entryType || "—"} small />
      </div>

      <div className="perfGrid" style={{ marginTop: 14 }}>
        <StatBox label="Entry" value={entry ?? "—"} />
        <StatBox label="SL" value={sl ?? "—"} />
        <StatBox label="TP1" value={tp1 ?? "—"} />
        <StatBox label="TP2" value={tp2 ?? "—"} />
      </div>

      <div className="actionsBox" style={{ marginTop: 14 }}>
        <div className="tiny"><b>What to do:</b></div>
        <div className="reason">
          <span className="smallText">
            {actionMessage || entryInstruction || reason || "No instruction yet."}
          </span>
        </div>

        <div className="reason" style={{ marginTop: 8 }}>
          <span className="smallText"><b>Timing:</b> {entryTiming || "—"}</span>
        </div>
      </div>

      <div className="actionsBox">
        <div className="tiny"><b>Manual Trade Checklist:</b></div>
        <div className="actionsList">
          {tradeChecklist.map((item, idx) => (
            <div key={idx} className="actionChip">
              <span>{item}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="controls" style={{ marginTop: 14 }}>
        <button className="btn primary" onClick={() => copyText(copySignalText)}>
          Copy Signal
        </button>
        <button className="btn" onClick={onAnalyze}>
          Refresh Signal
        </button>
      </div>
    </SmartCard>
  );

  const renderLotCalculator = () => (
    <SmartCard
      title="Signal Lot Calculator"
      className="riskCard"
      right={<span className="pill">{selectedSymbol}</span>}
    >
      <div className="reason">
        <span className="smallText">
          This calculator auto-fills from the current signal. Adjust point value once you confirm how your Deriv lot size behaves for each market.
        </span>
      </div>

      <div className="togglesRow" style={{ alignItems: "flex-end", marginTop: 12 }}>
        <div className="miniField">
          <span className="miniLabel">Account</span>
          <input className="miniInput" value={accountSize} onChange={(e) => setAccountSize(e.target.value)} inputMode="decimal" />
        </div>

        <div className="miniField">
          <span className="miniLabel">Risk %</span>
          <input className="miniInput" value={riskPercent} onChange={(e) => setRiskPercent(e.target.value)} inputMode="decimal" />
        </div>

        <div className="miniField">
          <span className="miniLabel">Point Value / 1 lot</span>
          <input
            className="miniInput"
            value={pointValues[selectedSymbol] ?? 1}
            onChange={(e) => updatePointValue(selectedSymbol, e.target.value)}
            inputMode="decimal"
          />
        </div>

        <button className="btn small" onClick={resetPointValues}>Reset values</button>
      </div>

      <div className="perfGrid" style={{ marginTop: 14 }}>
        <StatBox label="Risk Amount" value={money(lotPlan.riskAmount)} />
        <StatBox label="Suggested Lot" value={lotPlan.suggestedLot === null ? "—" : lotPlan.suggestedLot.toFixed(3)} />
        <StatBox label="Loss at SL" value={lotPlan.lossAtSl === null ? "—" : money(lotPlan.lossAtSl)} tone="pillLoss" />
        <StatBox label="Profit at TP1" value={lotPlan.profitTp1 === null ? "—" : money(lotPlan.profitTp1)} tone="pillWin" />
        <StatBox label="Profit at TP2" value={lotPlan.profitTp2 === null ? "—" : money(lotPlan.profitTp2)} tone="pillWin" />
        <StatBox label="Risk Distance" value={lotPlan.riskDistance === null ? "—" : fmt(lotPlan.riskDistance)} />
        <StatBox label="TP1 R:R" value={lotPlan.tp1R === null ? "—" : lotPlan.tp1R.toFixed(2)} />
        <StatBox label="TP2 R:R" value={lotPlan.tp2R === null ? "—" : lotPlan.tp2R.toFixed(2)} />
      </div>

      <div className="controls" style={{ marginTop: 14 }}>
        <button className="btn primary" onClick={() => copyText(copySignalText)}>
          Copy Lot Plan
        </button>
      </div>
    </SmartCard>
  );

  const renderActiveTrade = () => (
    <SmartCard
      title="Active Trade Protection"
      className="openTradesCard"
      right={<span className={`pill ${commandCenter.statusTone}`}>{commandCenter.secured ? "SECURED 🔒" : "MONITOR"}</span>}
    >
      {activeTrade ? (
        <>
          <div className="perfGrid">
            {commandCenter.stats.map((s, idx) => (
              <StatBox key={idx} label={s.label} value={s.value} small />
            ))}
          </div>

          <div className="actionsBox">
            <div className="tiny"><b>Trade Management:</b></div>
            <div className="actionsList">
              {commandCenter.checklist.map((item, idx) => (
                <div key={idx} className="actionChip">
                  <span>{item}</span>
                </div>
              ))}
            </div>
          </div>
        </>
      ) : (
        <div className="emptyRow">No active trade right now.</div>
      )}
    </SmartCard>
  );

  const renderStats = (stats, title) => (
    <SmartCard
      title={title}
      className="weeklyStatsCard"
      right={<span className={`pill ${stats.winRate >= 60 ? "pillWin" : stats.lossRate >= 50 && stats.totalClosed > 0 ? "pillLoss" : ""}`}>{percent(stats.winRate)}</span>}
    >
      <div className="perfGrid">
        <StatBox label="Trades" value={stats.trades} />
        <StatBox label="Closed" value={stats.totalClosed} />
        <StatBox label="Open" value={stats.open} />
        <StatBox label="Wins" value={stats.wins} tone="pillWin" />
        <StatBox label="Losses" value={stats.losses} tone="pillLoss" />
        <StatBox label="Breakevens" value={stats.breakevens} />
        <StatBox label="TP1 Wins" value={stats.tp1Wins} />
        <StatBox label="TP2 Wins" value={stats.tp2Wins} />
        <StatBox label="Win Rate" value={percent(stats.winRate)} />
        <StatBox label="Loss Rate" value={percent(stats.lossRate)} />
        <StatBox label="Sum R" value={stats.sumR} />
        <StatBox label="Avg R" value={stats.avgR} />
      </div>
    </SmartCard>
  );
   const renderScanner = () => (
    <SmartCard title="Market Scanner" className="scannerCard" right={<span className="tiny">{ranked?.length || 0} markets</span>}>
      <div className="tableWrap">
        <table className="table">
          <thead>
            <tr>
              <th>Market</th>
              <th>Decision</th>
              <th>Direction</th>
              <th>Conf</th>
              <th>Quality</th>
              <th>State</th>
              <th>Risk</th>
              <th>Strength</th>
              <th>Entry</th>
              <th>SL</th>
              <th>TP1</th>
              <th>TP2</th>
              <th>Open</th>
            </tr>
          </thead>
          <tbody>
            {ranked?.length ? ranked.map((r, idx) => {
              const strength = computeStrength(r);
              return (
                <tr key={`${r.symbol || idx}-${idx}`}>
                  <td className="mono">{r.symbol || "—"}</td>
                  <td><span className={`pill ${tradeActionTone(r.trade_action)}`}>{tradeActionLabel(r.trade_action)}</span></td>
                  <td><span className={`pill ${directionTone(r.direction)}`}>{r.direction || "—"}</span></td>
                  <td>{safeNum(r.confidence, 0)}%</td>
                  <td>{qualityText(r.quality_grade, r.quality_stars)}</td>
                  <td className="notes">{r.market_state || "—"}</td>
                  <td>{r.reversal_risk || "—"}</td>
                  <td className="mono">{renderStrengthBar(strength)} {strength.toFixed(1)}</td>
                  <td>{r.entry ?? "—"}</td>
                  <td>{r.sl ?? "—"}</td>
                  <td>{r.tp1 ?? "—"}</td>
                  <td>{r.tp2 ?? r.tp ?? "—"}</td>
                  <td><button className="btn small" onClick={() => setMarketAndAnalyze(r.symbol)}>Open</button></td>
                </tr>
              );
            }) : (
              <tr><td colSpan="13" className="emptyRow">No scanner data yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </SmartCard>
  );

  const renderChart = () => (
    <SmartCard title="Live Chart" className="chartCard" right={<span className="tiny">Candles: {candles?.length || 0}</span>}>
      <div className="chartWrap">
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
      </div>
    </SmartCard>
  );

  const renderHistory = () => (
    <SmartCard title="History & Performance" className="historyCard" right={<span className="tiny">Trades logged: {history.length}</span>}>
      <div className="perfGrid">
        <StatBox label="All Win %" value={percent(allStats.winRate)} />
        <StatBox label="All Closed" value={allStats.totalClosed} />
        <StatBox label="All Wins" value={allStats.wins} />
        <StatBox label="All Losses" value={allStats.losses} />
        <StatBox label="All B/E" value={allStats.breakevens} />
        <StatBox label="All Sum R" value={allStats.sumR} />
        <StatBox label="Best Market" value={personalBestMarket} small />
        <StatBox label="Best Setup" value={personalBestSetup} small />
      </div>

      <div className="controls" style={{ marginTop: 14 }}>
        <button className="btn small" onClick={clearHistory}>Clear local history</button>
      </div>

      <div className="tableWrap" style={{ marginTop: 14 }}>
        <table className="table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Market</th>
              <th>TF</th>
              <th>Dir</th>
              <th>Action</th>
              <th>Quality</th>
              <th>Conf</th>
              <th>Entry</th>
              <th>SL</th>
              <th>TP1</th>
              <th>TP2</th>
              <th>Outcome</th>
              <th>R</th>
            </tr>
          </thead>
          <tbody>
            {history?.length ? [...history].slice(-60).reverse().map((t) => (
              <tr key={t.id}>
                <td className="tiny">{t.closedAt ? toIso(t.closedAt) : toIso(t.openedAt)}</td>
                <td className="mono">{t.symbol}</td>
                <td>{t.timeframe}</td>
                <td>{t.direction}</td>
                <td>{tradeActionLabel(t.tradeAction)}</td>
                <td>{qualityText(t.qualityGrade, t.qualityStars)}</td>
                <td>{safeNum(t.confidence, 0)}%</td>
                <td>{fmt(t.entry)}</td>
                <td>{fmt(t.sl)}</td>
                <td>{fmt(t.tp1)}</td>
                <td>{fmt(t.tp2 ?? t.tp)}</td>
                <td><span className={`pill ${outcomeTone(t.outcome)}`}>{formatOutcomeLabel(t.outcome)}</span></td>
                <td>
                  {t.outcome === "TP2"
                    ? `+${t.rMult ?? "?"}`
                    : t.outcome === "TP1_ONLY"
                    ? `+${t.tp1RMult ?? "?"}`
                    : t.outcome === "SL"
                    ? "-1"
                    : t.outcome === "BE"
                    ? "0"
                    : "—"}
                </td>
              </tr>
            )) : (
              <tr><td colSpan="13" className="emptyRow">No history yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </SmartCard>
  );

  const renderHealth = () => (
    <SmartCard title="System Health" className="sentimentCard" right={<span className={`badge ${status === "LIVE" ? "live" : ""}`}>{status}</span>}>
      <SignalLine label="Backend URL" value={backendUrl} />
      <SignalLine label="Selected Market" value={selectedSymbol} />
      <SignalLine label="Timeframe" value={timeframe} />
      <SignalLine label="Last API OK" value={lastOkAtRef.current ? new Date(lastOkAtRef.current).toLocaleTimeString() : "—"} />
      <SignalLine label="Last Live Tick" value={lastLiveAtRef.current ? new Date(lastLiveAtRef.current).toLocaleTimeString() : "—"} />
      <SignalLine label="Auto Scan" value={autoScanOn ? `ON every ${autoScanEverySec}s` : "OFF"} />
      <SignalLine label="Auto Analyze" value={autoAnalyzeOn ? `ON every ${autoAnalyzeEverySec}s` : "OFF"} />
      <SignalLine label="Active Trades" value={`${activeTotal}/${maxActiveTotal}`} />
      {error ? <div className="errorBox">Error: {error}</div> : null}
    </SmartCard>
  );

  const renderMarketBriefing = () => (
    <SmartCard title="Market Briefing" className="marketBriefingCard" right={<span className={`pill ${marketStateTone}`}>{marketState}</span>}>
      <SignalLine label="Market" value={`${selectedSymbol} — ${selectedName}`} />
      <SignalLine label="Bias" value={bias} />
      <SignalLine label="Monthly Bias" value={monthlyBias} />
      <SignalLine label="Weekly Bias" value={weeklyBias} />
      <SignalLine label="Daily Bias" value={dailyBias} />
      <SignalLine label="Structure" value={structure} />
      <SignalLine label="Previous Trend" value={previousTrend} />
      <SignalLine label="Trend Strength" value={trendStrength === null ? "—" : trendStrength} />
      <SignalLine label="Reversal Risk" value={reversalRisk} tone={reversalTone} />
      <SignalLine label="Buyer Zone" value={buyerZone} />
      <SignalLine label="Seller Zone" value={sellerZone} />
      <SignalLine label="AOI" value={areaOfInterest} />
      <SignalLine label="Support" value={briefSupport} />
      <SignalLine label="Resistance" value={briefResistance} />
      <SignalLine label="Liquidity Below" value={liquidityBelow} />
      <SignalLine label="Liquidity Above" value={liquidityAbove} />
      <SignalLine label="Weighted Alignment" value={currentAlignmentChip || "—"} />
      <div className="reason"><span className="smallText"><b>Preferred Setup:</b> {preferredSetup}</span></div>
      <div className="reason"><span className="smallText"><b>Confirmation Needed:</b> {confirmationNeeded}</span></div>
      <div className="reason"><span className="smallText"><b>Invalidation:</b> {invalidation}</span></div>
      <div className="reason" style={{ marginTop: 10 }}><span className="smallText"><b>AI Explanation:</b> {aiExplanation}</span></div>
    </SmartCard>
  );

  const renderTab = () => {
    if (activeTab === "Overview") return renderOverview();
    if (activeTab === "Signal") return renderSignal();
    if (activeTab === "Lot Calculator") return renderLotCalculator();
    if (activeTab === "Active Trade") return renderActiveTrade();
    if (activeTab === "Daily Stats") return renderStats(dailyStats, "Daily Performance");
    if (activeTab === "Weekly Stats") return renderStats(weeklyStats, "Weekly Performance");
    if (activeTab === "Scanner") return renderScanner();
    if (activeTab === "Chart") return renderChart();
    if (activeTab === "History") return renderHistory();
    if (activeTab === "Health") return renderHealth();
    return renderOverview();
  };

  return (
    <div className="layout">
      <div className="card topCard">
        <div className="topRow">
          <div>
            <div className="title">DEXTRADEZBOT AI</div>
            <div className="subtitle">Smart Manual Trading Dashboard • Signals • Lot Calculator • Performance</div>
          </div>

          <div className="badges">
            <span className={`badge ${status === "LIVE" ? "live" : ""}`}>{status}</span>
            <span className={`badge ${tradeActionTone(tradeAction)}`}>{tradeActionLabel(tradeAction)}</span>
            <span className="badge">Daily: {percent(dailyStats.winRate)}</span>
            <span className="badge">Weekly: {percent(weeklyStats.winRate)}</span>
            <span className="badge">Active: {activeTotal}/{maxActiveTotal}</span>
            <span className="badge">Price: {price ?? "—"}</span>
          </div>
        </div>

        {activeBanner ? (
          <div className="activeBanner">
            <div className="activeLeft">
              <span className="pill fire">🟢 ACTIVE</span>
              <span className="mono">{activeTrade.symbol} • {activeTrade.timeframe}</span>
              <span className={`pill ${directionTone(activeTrade.direction)}`}>{activeTrade.direction}</span>
              <span className="pill">Conf {safeNum(activeTrade.confidence, 0)}%</span>
              <span className="pill">Quality {qualityText(activeTrade.quality_grade || qualityGrade, activeTrade.quality_stars || qualityStars)}</span>
              {activeTrade.tp1_hit ? <span className="pill pillWin">TP1 hit ✅</span> : <span className="pill">TP1 pending</span>}
              {commandCenter.secured ? <span className="pill pillWin">Secured 🔒</span> : null}
              {activeAlignmentChip ? <span className="pill">Align {activeAlignmentChip}</span> : null}
            </div>
          </div>
        ) : null}

        <div className="controls">
          {VOLATILITY_OPTIONS.map((v) => (
            <button
              key={v.symbol}
              className={`btn small ${selectedSymbol === v.symbol ? "primary" : ""}`}
              onClick={() => setMarketAndAnalyze(v.symbol)}
            >
              {v.symbol}
            </button>
          ))}

          <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="select">
            {TIMEFRAMES.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>

          <button className="btn primary" onClick={onAnalyze}>Analyze</button>
          <button className="btn" onClick={onScan} disabled={scanLoading}>{scanLoading ? "Scanning..." : "Scan"}</button>
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
            <span>Auto-pick</span>
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
        </div>

        {error ? <div className="errorBox">Error: {error}</div> : null}
      </div>

      <div className="card">
        <div className="controls" style={{ flexWrap: "wrap" }}>
          {TABS.map((tab) => (
            <button
              key={tab}
              className={`btn small ${activeTab === tab ? "primary" : ""}`}
              onClick={() => setActiveTab(tab)}
            >
              {tab}
            </button>
          ))}
        </div>
      </div>

      {renderTab()}

      {activeTab !== "Health" ? renderMarketBriefing() : null}
    </div>
  );
}     