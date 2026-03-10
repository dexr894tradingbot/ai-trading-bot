from __future__ import annotations

import os
import time
import uuid
import json
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from utils.deriv import fetch_deriv_candles
from utils.telegram import send_telegram_message

router = APIRouter()

# =========================================================
# CONFIG
# =========================================================
ENTRY_TF = "5m"

EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14

PULLBACK_LOOKBACK = 8
BREAKOUT_LOOKBACK = 12
SWEEP_LOOKBACK = 8

SL_BUFFER_ATR = 0.18
MIN_RR = 2.0

MIN_SIGNAL_GAP_SEC = 60
MIN_HOLD_SECONDS_AFTER_OPEN = 30

MAX_ACTIVE_TOTAL = 5
MAX_SIGNALS_PER_DAY_PER_SYMBOL = 22
DAILY_LOSS_LIMIT_R = -999.0
COOLDOWN_MIN_AFTER_LOSS = 5

BE_AFTER_TP1 = True
TRAIL_AFTER_TP1 = True
TRAIL_LOOKBACK = 8
TRAIL_BUFFER_ATR = 0.15

MAX_HISTORY = 500
PERFORMANCE_REVIEW_N = 30
LIVE_CACHE_TTL_SEC = 2

ZONE_LOOKBACK = 180
ZONE_CLUSTER_ATR = 0.35
ZONE_MIN_TOUCHES = 3

POOL_LOOKBACK = 120
POOL_CLUSTER_ATR = 0.25
POOL_MIN_TOUCHES = 3

ALLOWED_TFS = {"1m", "5m", "15m", "30m", "1h", "2h", "4h"}

# =========================================================
# STATE
# =========================================================
LIVE_CACHE: Dict[str, Dict[str, Any]] = {}
ACTIVE_TRADES: Dict[str, Dict[str, Any]] = {}
TRADE_HISTORY: List[Dict[str, Any]] = []

RISK_STATE: Dict[str, Any] = {
    "day_key": None,
    "daily_R": 0.0,
    "signals_today": {},
    "cooldown_until": {},
}

LAST_SIGNAL_TS: Dict[str, int] = {}

# =========================================================
# REQUEST MODELS
# =========================================================
class AnalyzeRequest(BaseModel):
    symbol: str
    timeframe: str = "5m"


class ScanRequest(BaseModel):
    timeframe: str = "5m"
    symbols: List[str] = ["R_10", "R_25", "R_50", "R_75", "R_100"]
 # =========================================================
# BASIC HELPERS
# =========================================================
def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _reset_daily_if_needed() -> None:
    dk = _today_key()
    if RISK_STATE.get("day_key") != dk:
        RISK_STATE["day_key"] = dk
        RISK_STATE["daily_R"] = 0.0
        RISK_STATE["signals_today"] = {}


def _now_ts() -> int:
    return int(time.time())


def _trade_key(symbol: str, tf: str) -> str:
    return f"{symbol}:{tf}"


def _active_total() -> int:
    return len(ACTIVE_TRADES)


def _push_history(item: Dict[str, Any]) -> None:
    TRADE_HISTORY.append(item)
    if len(TRADE_HISTORY) > MAX_HISTORY:
        del TRADE_HISTORY[0 : len(TRADE_HISTORY) - MAX_HISTORY]


def _find_history_index(trade_id: str) -> Optional[int]:
    for i in range(len(TRADE_HISTORY) - 1, -1, -1):
        if TRADE_HISTORY[i].get("trade_id") == trade_id:
            return i
    return None


def safe_float(x: Any, fallback: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return fallback


def _requested_chart_tf(raw_tf: Optional[str]) -> str:
    tf = (raw_tf or ENTRY_TF).strip()
    return tf if tf in ALLOWED_TFS else ENTRY_TF


def _live_cache_key(symbol: str, timeframe: str, count: int) -> str:
    return f"{symbol}:{timeframe}:{count}"


async def _cached_fetch_candles(app_id: str, symbol: str, timeframe: str, count: int) -> List[Dict[str, Any]]:
    key = _live_cache_key(symbol, timeframe, count)
    now = time.time()
    cached = LIVE_CACHE.get(key)

    if cached and (now - float(cached.get("ts", 0))) < LIVE_CACHE_TTL_SEC:
        return cached.get("candles") or []

    candles = await fetch_deriv_candles(
        app_id=app_id,
        symbol=symbol,
        timeframe=timeframe,
        count=count,
    )

    LIVE_CACHE[key] = {"ts": now, "candles": candles or []}
    return candles or []


def score_to_grade(score_1_10: int) -> str:
    s = int(max(0, min(10, score_1_10)))
    if s >= 8:
        return "PREMIUM"
    if s >= 7:
        return "STRONG"
    if s >= 6:
        return "GOOD"
    if s >= 5:
        return "WATCH"
    return "IGNORE"


def trade_progress_percent(direction: str, entry: float, sl: float, tp2: float, current_price: float) -> float:
    if direction == "BUY":
        total = tp2 - entry
        if total <= 0:
            return 0.0
        return ((current_price - entry) / total) * 100.0

    if direction == "SELL":
        total = entry - tp2
        if total <= 0:
            return 0.0
        return ((entry - current_price) / total) * 100.0

    return 0.0
# =========================================================
# TELEGRAM / PERFORMANCE
# =========================================================
def build_signal_message(symbol: str, timeframe: str, signal: Dict[str, Any]) -> str:
    direction = signal.get("direction", "-")
    confidence = signal.get("confidence", 0)
    score = signal.get("score_1_10", 0)
    grade = signal.get("grade", "-")
    entry = signal.get("entry", "-")
    sl = signal.get("sl", "-")
    tp1 = signal.get("tp1", "-")
    tp2 = signal.get("tp2", signal.get("tp", "-"))
    mode = signal.get("entry_type") or signal.get("mode") or "-"
    r_mult = signal.get("r_multiple", "-")
    arrow = "🟢 BUY" if direction == "BUY" else "🔴 SELL"

    return (
        f"🚨 DEXTRADEZ BOT SIGNAL 🚨\n\n"
        f"{arrow} {symbol}\n"
        f"Timeframe: {timeframe}\n"
        f"Mode: {mode}\n\n"
        f"Entry: {entry}\n"
        f"Stop Loss: {sl}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n\n"
        f"Confidence: {confidence}%\n"
        f"Setup Score: {score}/10 ({grade})\n"
        f"R:R: {r_mult}"
    )


def build_tp1_message(symbol: str, timeframe: str, trade: Dict[str, Any]) -> str:
    return (
        f"🎯 TP1 HIT\n\n"
        f"Symbol: {symbol}\n"
        f"Timeframe: {timeframe}\n"
        f"Direction: {trade.get('direction', '-')}\n\n"
        f"Entry: {trade.get('entry', '-')}\n"
        f"TP1: {trade.get('tp1', '-')}\n"
        f"TP2: {trade.get('tp2', trade.get('tp', '-'))}\n\n"
        f"✅ Partial secured\n"
        f"🔒 Stop moved safer"
    )


def build_closed_message(symbol: str, timeframe: str, trade: Dict[str, Any], outcome: str, price: float) -> str:
    emoji = "🏆" if outcome == "TP2" else "🛑"
    return (
        f"{emoji} TRADE CLOSED\n\n"
        f"Symbol: {symbol}\n"
        f"Timeframe: {timeframe}\n"
        f"Direction: {trade.get('direction', '-')}\n"
        f"Outcome: {outcome}\n"
        f"Close Price: {round(price, 5)}\n\n"
        f"Entry: {trade.get('entry', '-')}\n"
        f"SL: {trade.get('sl', '-')}\n"
        f"TP2: {trade.get('tp2', trade.get('tp', '-'))}\n"
        f"R: {trade.get('r_multiple', '-')}"
    )


def _performance(last_n: int) -> Dict[str, Any]:
    last_n = max(1, min(int(last_n), 200))
    closed = [t for t in TRADE_HISTORY if t.get("status") == "CLOSED"]
    window = closed[-last_n:]

    total = len(window)
    wins = sum(1 for t in window if t.get("outcome") == "TP2")
    losses = sum(1 for t in window if t.get("outcome") == "SL")
    win_rate = (wins / total * 100.0) if total else 0.0

    r_vals: List[float] = []
    for t in window:
        if t.get("outcome") == "TP2":
            r_vals.append(float(t.get("r_multiple") or 0.0))
        elif t.get("outcome") == "SL":
            r_vals.append(-1.0)

    avg_r = (sum(r_vals) / len(r_vals)) if r_vals else 0.0
    total_r = sum(r_vals) if r_vals else 0.0

    return {
        "last_n": last_n,
        "total_closed": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "avg_R": round(avg_r, 3),
        "total_R": round(total_r, 3),
    } 
 # =========================================================
# INDICATORS / STRUCTURE
# =========================================================
def ema_last(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None

    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def atr_last(candles: List[Dict[str, float]], period: int = ATR_PERIOD) -> Optional[float]:
    if len(candles) < period + 1:
        return None

    trs: List[float] = []
    for i in range(1, len(candles)):
        h = safe_float(candles[i]["high"])
        l = safe_float(candles[i]["low"])
        pc = safe_float(candles[i - 1]["close"])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    window = trs[-period:]
    return sum(window) / len(window) if window else None


def recent_swing_low(candles: List[Dict[str, float]], lookback: int = 10) -> float:
    return min(safe_float(c["low"]) for c in candles[-lookback:])


def recent_swing_high(candles: List[Dict[str, float]], lookback: int = 10) -> float:
    return max(safe_float(c["high"]) for c in candles[-lookback:])


def candle_body_ratio(candle: Dict[str, float]) -> float:
    o = safe_float(candle["open"])
    c = safe_float(candle["close"])
    h = safe_float(candle["high"])
    l = safe_float(candle["low"])
    rng = max(1e-9, h - l)
    return abs(c - o) / rng


def get_trend_memory(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    closes = [safe_float(c["close"]) for c in candles]
    ema20_now = ema_last(closes, EMA_FAST)
    ema50_now = ema_last(closes, EMA_SLOW)

    if ema20_now is None or ema50_now is None:
        return {
            "trend": "SIDEWAYS",
            "prev_trend": "SIDEWAYS",
            "trend_strength": 0.0,
            "ema20": ema20_now,
            "ema50": ema50_now,
        }

    trend = "UP" if ema20_now > ema50_now else "DOWN" if ema20_now < ema50_now else "SIDEWAYS"

    past_slice = closes[:-10] if len(closes) > 60 else closes
    ema20_prev = ema_last(past_slice, EMA_FAST) if len(past_slice) >= EMA_FAST else None
    ema50_prev = ema_last(past_slice, EMA_SLOW) if len(past_slice) >= EMA_SLOW else None

    if ema20_prev is None or ema50_prev is None:
        prev_trend = trend
    else:
        prev_trend = "UP" if ema20_prev > ema50_prev else "DOWN" if ema20_prev < ema50_prev else "SIDEWAYS"

    atr_value = float(atr_last(candles, ATR_PERIOD) or 0.0)
    trend_strength = abs(float(ema20_now) - float(ema50_now)) / max(1e-9, atr_value) if atr_value > 0 else 0.0

    return {
        "trend": trend,
        "prev_trend": prev_trend,
        "trend_strength": round(trend_strength, 3),
        "ema20": ema20_now,
        "ema50": ema50_now,
    }
# =========================================================
# REACTION ZONES / LIQUIDITY POOLS
# =========================================================
def _cluster_levels(levels: List[float], tolerance: float) -> List[List[float]]:
    if not levels:
        return []

    levels = sorted(levels)
    groups: List[List[float]] = [[levels[0]]]

    for p in levels[1:]:
        if abs(p - groups[-1][-1]) <= tolerance:
            groups[-1].append(p)
        else:
            groups.append([p])

    return groups


def reaction_zones(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if len(candles) < 40 or atr_value <= 0:
        return {
            "supports": [],
            "resistances": [],
            "nearest_support": None,
            "nearest_resistance": None,
        }

    data = candles[-ZONE_LOOKBACK:] if len(candles) >= ZONE_LOOKBACK else candles
    tol = max(1e-9, atr_value * ZONE_CLUSTER_ATR)

    lows = [safe_float(c["low"]) for c in data]
    highs = [safe_float(c["high"]) for c in data]

    support_groups = _cluster_levels(lows, tol)
    resistance_groups = _cluster_levels(highs, tol)

    supports = []
    for g in support_groups:
        if len(g) >= ZONE_MIN_TOUCHES:
            supports.append({
                "level": round(sum(g) / len(g), 5),
                "touches": len(g),
                "low": round(min(g), 5),
                "high": round(max(g), 5),
            })

    resistances = []
    for g in resistance_groups:
        if len(g) >= ZONE_MIN_TOUCHES:
            resistances.append({
                "level": round(sum(g) / len(g), 5),
                "touches": len(g),
                "low": round(min(g), 5),
                "high": round(max(g), 5),
            })

    last_close = safe_float(data[-1]["close"])
    sup_candidates = [z for z in supports if z["level"] <= last_close]
    res_candidates = [z for z in resistances if z["level"] >= last_close]

    nearest_support = min(sup_candidates, key=lambda z: abs(z["level"] - last_close)) if sup_candidates else None
    nearest_resistance = min(res_candidates, key=lambda z: abs(z["level"] - last_close)) if res_candidates else None

    return {
        "supports": supports,
        "resistances": resistances,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    }


def liquidity_pools(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if len(candles) < 30 or atr_value <= 0:
        return {
            "high_pools": [],
            "low_pools": [],
            "nearest_high_pool": None,
            "nearest_low_pool": None,
        }

    data = candles[-POOL_LOOKBACK:] if len(candles) >= POOL_LOOKBACK else candles
    tol = max(1e-9, atr_value * POOL_CLUSTER_ATR)

    highs = [safe_float(c["high"]) for c in data]
    lows = [safe_float(c["low"]) for c in data]

    high_groups = _cluster_levels(highs, tol)
    low_groups = _cluster_levels(lows, tol)

    high_pools = []
    for g in high_groups:
        if len(g) >= POOL_MIN_TOUCHES:
            high_pools.append({
                "level": round(sum(g) / len(g), 5),
                "touches": len(g),
                "low": round(min(g), 5),
                "high": round(max(g), 5),
            })

    low_pools = []
    for g in low_groups:
        if len(g) >= POOL_MIN_TOUCHES:
            low_pools.append({
                "level": round(sum(g) / len(g), 5),
                "touches": len(g),
                "low": round(min(g), 5),
                "high": round(max(g), 5),
            })

    last_close = safe_float(data[-1]["close"])
    highs_above = [p for p in high_pools if p["level"] >= last_close]
    lows_below = [p for p in low_pools if p["level"] <= last_close]

    nearest_high_pool = min(highs_above, key=lambda p: abs(p["level"] - last_close)) if highs_above else None
    nearest_low_pool = min(lows_below, key=lambda p: abs(p["level"] - last_close)) if lows_below else None

    return {
        "high_pools": high_pools,
        "low_pools": low_pools,
        "nearest_high_pool": nearest_high_pool,
        "nearest_low_pool": nearest_low_pool,
    }


def calculate_levels(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    atr_value = float(atr_last(candles, ATR_PERIOD) or 0.0)
    zones = reaction_zones(candles, atr_value)

    supports = [z["level"] for z in zones["supports"][:6]]
    resistances = [z["level"] for z in zones["resistances"][:6]]

    nearest_support = zones.get("nearest_support")
    nearest_resistance = zones.get("nearest_resistance")

    return {
        "supports": supports,
        "resistances": resistances,
        "support_zone": nearest_support,
        "resistance_zone": nearest_resistance,
        "reaction_zones": zones,
    }
# =========================================================
# STRATEGIES
# =========================================================
def detect_pullback_continuation(
    candles: List[Dict[str, float]],
    trend_info: Dict[str, Any],
    atr_value: float,
    zones: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if len(candles) < max(EMA_FAST, PULLBACK_LOOKBACK + 3) or atr_value <= 0:
        return None

    trend = trend_info.get("trend")
    prev_trend = trend_info.get("prev_trend")
    ema20 = float(trend_info.get("ema20") or 0.0)
    trend_strength = float(trend_info.get("trend_strength") or 0.0)

    last = candles[-1]
    prev = candles[-2]

    last_close = safe_float(last["close"])
    last_open = safe_float(last["open"])
    last_high = safe_float(last["high"])
    last_low = safe_float(last["low"])

    prev_high = safe_float(prev["high"])
    prev_low = safe_float(prev["low"])

    body_ratio = candle_body_ratio(last)

    if trend == "UP":
        near_ema = last_low <= ema20 + atr_value * 0.15
        bullish_close = last_close > last_open
        continuation_break = last_close > prev_high
        strong_body = body_ratio >= 0.50
        zone_ok = True

        ns = zones.get("nearest_support")
        if ns:
            zone_ok = abs(last_low - ns["level"]) <= atr_value * 0.8

        if near_ema and bullish_close and continuation_break and strong_body and zone_ok and trend_strength >= 0.15:
            sl = recent_swing_low(candles, PULLBACK_LOOKBACK) - atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry + max((entry - sl) * MIN_RR, atr_value * 1.5)
            return {
                "direction": "BUY",
                "entry": entry,
                "sl": sl,
                "tp2": tp2,
                "reason": "pullback_continuation",
                "prev_trend": prev_trend,
            }

    if trend == "DOWN":
        near_ema = last_high >= ema20 - atr_value * 0.15
        bearish_close = last_close < last_open
        continuation_break = last_close < prev_low
        strong_body = body_ratio >= 0.50
        zone_ok = True

        nr = zones.get("nearest_resistance")
        if nr:
            zone_ok = abs(last_high - nr["level"]) <= atr_value * 0.8

        if near_ema and bearish_close and continuation_break and strong_body and zone_ok and trend_strength >= 0.15:
            sl = recent_swing_high(candles, PULLBACK_LOOKBACK) + atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry - max((sl - entry) * MIN_RR, atr_value * 1.5)
            return {
                "direction": "SELL",
                "entry": entry,
                "sl": sl,
                "tp2": tp2,
                "reason": "pullback_continuation",
                "prev_trend": prev_trend,
            }

    return None


def detect_breakout_retest(
    candles: List[Dict[str, float]],
    trend_info: Dict[str, Any],
    atr_value: float,
    pools: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if len(candles) < BREAKOUT_LOOKBACK + 3 or atr_value <= 0:
        return None

    trend = trend_info.get("trend")
    prev_trend = trend_info.get("prev_trend")

    last = candles[-1]
    prev = candles[-2]
    recent = candles[-(BREAKOUT_LOOKBACK + 2):-2]

    recent_high = max(safe_float(c["high"]) for c in recent)
    recent_low = min(safe_float(c["low"]) for c in recent)

    last_close = safe_float(last["close"])
    last_low = safe_float(last["low"])
    last_high = safe_float(last["high"])
    prev_close = safe_float(prev["close"])

    if trend == "UP":
        broke = prev_close > recent_high
        retested = last_low <= recent_high + atr_value * 0.20
        held = last_close > recent_high
        pool_bonus_ok = True

        hp = pools.get("nearest_high_pool")
        if hp:
            pool_bonus_ok = hp["level"] >= recent_high

        if broke and retested and held and pool_bonus_ok:
            sl = min(safe_float(prev["low"]), last_low) - atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry + max((entry - sl) * MIN_RR, atr_value * 1.5)
            return {
                "direction": "BUY",
                "entry": entry,
                "sl": sl,
                "tp2": tp2,
                "reason": "breakout_retest",
                "prev_trend": prev_trend,
            }

    if trend == "DOWN":
        broke = prev_close < recent_low
        retested = last_high >= recent_low - atr_value * 0.20
        held = last_close < recent_low
        pool_bonus_ok = True

        lp = pools.get("nearest_low_pool")
        if lp:
            pool_bonus_ok = lp["level"] <= recent_low

        if broke and retested and held and pool_bonus_ok:
            sl = max(safe_float(prev["high"]), last_high) + atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry - max((sl - entry) * MIN_RR, atr_value * 1.5)
            return {
                "direction": "SELL",
                "entry": entry,
                "sl": sl,
                "tp2": tp2,
                "reason": "breakout_retest",
                "prev_trend": prev_trend,
            }

    return None


def detect_liquidity_sweep_with_trend(
    candles: List[Dict[str, float]],
    trend_info: Dict[str, Any],
    atr_value: float,
    pools: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if len(candles) < SWEEP_LOOKBACK + 2 or atr_value <= 0:
        return None

    trend = trend_info.get("trend")
    prev_trend = trend_info.get("prev_trend")

    last = candles[-1]
    recent = candles[-(SWEEP_LOOKBACK + 1):-1]

    recent_high = max(safe_float(c["high"]) for c in recent)
    recent_low = min(safe_float(c["low"]) for c in recent)

    last_high = safe_float(last["high"])
    last_low = safe_float(last["low"])
    last_close = safe_float(last["close"])
    last_open = safe_float(last["open"])

    if trend == "UP":
        swept = last_low < recent_low
        closed_back = last_close > recent_low
        bullish = last_close > last_open
        low_pool = pools.get("nearest_low_pool")
        pool_ok = True if low_pool is None else abs(last_low - low_pool["level"]) <= atr_value * 0.8

        if swept and closed_back and bullish and pool_ok:
            sl = last_low - atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry + max((entry - sl) * MIN_RR, atr_value * 1.5)
            return {
                "direction": "BUY",
                "entry": entry,
                "sl": sl,
                "tp2": tp2,
                "reason": "liquidity_sweep_with_trend",
                "prev_trend": prev_trend,
            }

    if trend == "DOWN":
        swept = last_high > recent_high
        closed_back = last_close < recent_high
        bearish = last_close < last_open
        high_pool = pools.get("nearest_high_pool")
        pool_ok = True if high_pool is None else abs(last_high - high_pool["level"]) <= atr_value * 0.8

        if swept and closed_back and bearish and pool_ok:
            sl = last_high + atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry - max((sl - entry) * MIN_RR, atr_value * 1.5)
            return {
                "direction": "SELL",
                "entry": entry,
                "sl": sl,
                "tp2": tp2,
                "reason": "liquidity_sweep_with_trend",
                "prev_trend": prev_trend,
            }

    return None
# =========================================================
# BUILD SIGNAL
# =========================================================
def build_signal_from_setup(
    setup: Dict[str, Any],
    trend_info: Dict[str, Any],
    zones: Dict[str, Any],
    pools: Dict[str, Any],
    atr_value: float,
) -> Dict[str, Any]:
    direction = setup["direction"]
    entry = float(setup["entry"])
    sl = float(setup["sl"])
    tp2 = float(setup["tp2"])

    risk = abs(entry - sl)
    if risk <= 0:
        return {"direction": "HOLD", "confidence": 0, "reason": "invalid_risk"}

    tp1 = entry + risk if direction == "BUY" else entry - risk
    r_mult = abs(tp2 - entry) / risk if risk > 0 else 0.0

    confidence = 70
    if setup["reason"] == "pullback_continuation":
        confidence = 74
    elif setup["reason"] == "breakout_retest":
        confidence = 80
    elif setup["reason"] == "liquidity_sweep_with_trend":
        confidence = 76

    trend_strength = float(trend_info.get("trend_strength") or 0.0)
    if trend_strength >= 0.35:
        confidence += 4

    if direction == "BUY":
        ns = zones.get("nearest_support")
        lp = pools.get("nearest_low_pool")
        if ns and abs(entry - ns["level"]) <= atr_value * 1.0:
            confidence += 3
        if lp and abs(entry - lp["level"]) <= atr_value * 1.0:
            confidence += 3
    else:
        nr = zones.get("nearest_resistance")
        hp = pools.get("nearest_high_pool")
        if nr and abs(entry - nr["level"]) <= atr_value * 1.0:
            confidence += 3
        if hp and abs(entry - hp["level"]) <= atr_value * 1.0:
            confidence += 3

    if trend_info.get("prev_trend") == trend_info.get("trend"):
        confidence += 2

    confidence = max(0, min(90, confidence))
    score = max(1, min(10, int(round(confidence / 10))))
    grade = score_to_grade(score)

    return {
        "direction": direction,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp": round(tp2, 5),
        "tp1": round(tp1, 5),
        "tp2": round(tp2, 5),
        "confidence": confidence,
        "score_1_10": score,
        "grade": grade,
        "r_multiple": round(r_mult, 2),
        "reason": setup["reason"],
        "entry_type": setup["reason"].upper(),
        "mode": setup["reason"].upper(),
        "meta": {
            "trend": trend_info.get("trend"),
            "prev_trend": trend_info.get("prev_trend"),
            "trend_strength": trend_info.get("trend_strength"),
            "ema20": trend_info.get("ema20"),
            "ema50": trend_info.get("ema50"),
        },
    }


def pick_best_setup(candles_tf: List[Dict[str, float]]) -> Dict[str, Any]:
    atr_value = float(atr_last(candles_tf, ATR_PERIOD) or 0.0)
    if atr_value <= 0:
        return {"direction": "HOLD", "confidence": 0, "reason": "atr_not_ready"}

    trend_info = get_trend_memory(candles_tf)
    if trend_info.get("trend") == "SIDEWAYS":
        return {"direction": "HOLD", "confidence": 0, "reason": "sideways_trend"}

    zones = reaction_zones(candles_tf, atr_value)
    pools = liquidity_pools(candles_tf, atr_value)

    breakout = detect_breakout_retest(candles_tf, trend_info, atr_value, pools)
    if breakout:
        return build_signal_from_setup(breakout, trend_info, zones, pools, atr_value)

    sweep = detect_liquidity_sweep_with_trend(candles_tf, trend_info, atr_value, pools)
    if sweep:
        return build_signal_from_setup(sweep, trend_info, zones, pools, atr_value)

    pullback = detect_pullback_continuation(candles_tf, trend_info, atr_value, zones)
    if pullback:
        return build_signal_from_setup(pullback, trend_info, zones, pools, atr_value)

    return {"direction": "HOLD", "confidence": 0, "reason": f"no_setup_{trend_info.get('trend', 'sideways').lower()}"}
# =========================================================
# TRADE MANAGEMENT
# =========================================================
def hit_level(direction: str, last_high: float, last_low: float, level: float, kind: str) -> bool:
    if direction == "BUY":
        return last_high >= level if kind in ("TP1", "TP2", "TP") else last_low <= level
    return last_low <= level if kind in ("TP1", "TP2", "TP") else last_high >= level


def trail_stop_suggestion(candles_tf: List[Dict[str, float]], direction: str, atr_value: float) -> float:
    data = candles_tf[-TRAIL_LOOKBACK:] if len(candles_tf) >= TRAIL_LOOKBACK else candles_tf
    buf = atr_value * TRAIL_BUFFER_ATR

    if direction == "BUY":
        return min(safe_float(c["low"]) for c in data) - buf
    return max(safe_float(c["high"]) for c in data) + buf


async def open_new_trade(symbol: str, timeframe: str, signal: Dict[str, Any]) -> Dict[str, Any]:
    trade_id = str(uuid.uuid4())
    trade = dict(signal)

    trade.update({
        "trade_id": trade_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "OPEN",
        "opened_at": _now_ts(),
        "tp1_hit": False,
        "progress_pct": 0.0,
    })

    ACTIVE_TRADES[_trade_key(symbol, timeframe)] = trade
    RISK_STATE["signals_today"][symbol] = int(RISK_STATE["signals_today"].get(symbol, 0)) + 1
    LAST_SIGNAL_TS[symbol] = _now_ts()
    _push_history(dict(trade))
    await send_telegram_message(build_signal_message(symbol, timeframe, trade))
    return trade


async def manage_active_trade(symbol: str, timeframe: str, candles_tf: List[Dict[str, float]], trade: Dict[str, Any]) -> Dict[str, Any]:
    last = candles_tf[-1]
    last_high = safe_float(last["high"])
    last_low = safe_float(last["low"])
    last_close = safe_float(last["close"])

    direction = trade.get("direction")
    entry = safe_float(trade.get("entry"))
    sl = safe_float(trade.get("sl"))
    tp1 = safe_float(trade.get("tp1"))
    tp2 = safe_float(trade.get("tp2") or trade.get("tp"))

    actions: List[Dict[str, Any]] = []

    if (_now_ts() - int(trade.get("opened_at") or 0)) < MIN_HOLD_SECONDS_AFTER_OPEN:
        if tp2:
            trade["progress_pct"] = round(trade_progress_percent(direction, entry, sl, tp2, last_close), 2)
        return {"signal": trade, "active": True, "actions": actions}

    if hit_level(direction, last_high, last_low, sl, "SL"):
        trade["status"] = "CLOSED"
        trade["outcome"] = "SL"
        trade["closed_price"] = round(sl, 5)
        trade["closed_at"] = _now_ts()

        ACTIVE_TRADES.pop(_trade_key(symbol, timeframe), None)
        RISK_STATE["daily_R"] += -1.0
        RISK_STATE["cooldown_until"][symbol] = _now_ts() + COOLDOWN_MIN_AFTER_LOSS * 60

        idx = _find_history_index(trade["trade_id"])
        if idx is not None:
            TRADE_HISTORY[idx] = {**TRADE_HISTORY[idx], **trade}

        await send_telegram_message(build_closed_message(symbol, timeframe, trade, "SL", sl))
        return {
            "closed_trade": trade,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "trade_closed_sl"},
            "active": False,
            "actions": actions,
        }

    if tp1 and not trade.get("tp1_hit", False):
        if hit_level(direction, last_high, last_low, tp1, "TP1"):
            trade["tp1_hit"] = True
            actions.append({"type": "TP1_HIT"})
            if BE_AFTER_TP1:
                trade["sl"] = entry
                actions.append({"type": "MOVE_SL", "to": round(entry, 5)})
            await send_telegram_message(build_tp1_message(symbol, timeframe, trade))

    if TRAIL_AFTER_TP1 and trade.get("tp1_hit"):
        atr_value = float(atr_last(candles_tf, ATR_PERIOD) or 0.0)
        if atr_value > 0:
            trail = trail_stop_suggestion(candles_tf, direction, atr_value)
            if direction == "BUY":
                trade["sl"] = max(safe_float(trade["sl"]), trail)
            else:
                trade["sl"] = min(safe_float(trade["sl"]), trail)
            actions.append({"type": "TRAIL_SUGGESTION", "to": round(trade["sl"], 5)})

    if hit_level(direction, last_high, last_low, tp2, "TP2"):
        trade["status"] = "CLOSED"
        trade["outcome"] = "TP2"
        trade["closed_price"] = round(tp2, 5)
        trade["closed_at"] = _now_ts()

        ACTIVE_TRADES.pop(_trade_key(symbol, timeframe), None)
        RISK_STATE["daily_R"] += float(trade.get("r_multiple") or 1.0)

        idx = _find_history_index(trade["trade_id"])
        if idx is not None:
            TRADE_HISTORY[idx] = {**TRADE_HISTORY[idx], **trade}

        await send_telegram_message(build_closed_message(symbol, timeframe, trade, "TP2", tp2))
        return {
            "closed_trade": trade,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "trade_closed_tp2"},
            "active": False,
            "actions": actions,
        }

    trade["progress_pct"] = round(trade_progress_percent(direction, entry, safe_float(trade["sl"]), tp2, last_close), 2)
    return {"signal": trade, "active": True, "actions": actions}
# =========================================================
# ANALYZE CORE + ROUTES
# =========================================================
async def analyze_market(req: AnalyzeRequest):
    _reset_daily_if_needed()

    symbol = req.symbol.strip()
    timeframe = ENTRY_TF
    key = _trade_key(symbol, timeframe)

    app_id = os.getenv("DERIV_APP_ID", "1089")
    candles_tf = await _cached_fetch_candles(app_id, symbol, timeframe, 260)

    if not candles_tf:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": None,
            "candles": [],
            "levels": {"supports": [], "resistances": []},
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "no_candles"},
            "active": False,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }

    last = candles_tf[-1]
    price = safe_float(last["close"])
    levels = calculate_levels(candles_tf)

    if key in ACTIVE_TRADES:
        managed = await manage_active_trade(symbol, timeframe, candles_tf, ACTIVE_TRADES[key])
        result = {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_tf,
            "levels": levels,
            "signal": managed.get("signal", {"direction": "HOLD", "confidence": 0}),
            "active": managed.get("active", False),
            "actions": managed.get("actions", []),
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }
        if managed.get("closed_trade"):
            result["closed_trade"] = managed["closed_trade"]
        return result

    if _active_total() >= MAX_ACTIVE_TOTAL:
        hold_reason = "max_active_trades"
    elif RISK_STATE["daily_R"] <= DAILY_LOSS_LIMIT_R:
        hold_reason = "daily_loss_limit_hit"
    elif RISK_STATE["signals_today"].get(symbol, 0) >= MAX_SIGNALS_PER_DAY_PER_SYMBOL:
        hold_reason = "max_signals_today_symbol"
    elif _now_ts() < int(RISK_STATE.get("cooldown_until", {}).get(symbol, 0)):
        hold_reason = "cooldown_active"
    elif (_now_ts() - int(LAST_SIGNAL_TS.get(symbol, 0))) < MIN_SIGNAL_GAP_SEC:
        hold_reason = "signal_gap_active"
    else:
        hold_reason = ""

    if hold_reason:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_tf,
            "levels": levels,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": hold_reason},
            "active": False,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }

    signal = pick_best_setup(candles_tf)

    if signal.get("direction") in ("BUY", "SELL"):
        opened = await open_new_trade(symbol, timeframe, signal)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_tf,
            "levels": levels,
            "signal": opened,
            "active": True,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "price": round(price, 5),
        "candles": candles_tf,
        "levels": levels,
        "signal": signal,
        "active": False,
        "actions": [],
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
        "max_active_total": MAX_ACTIVE_TOTAL,
    }


@router.post("/analyze")
async def analyze(req: AnalyzeRequest):
    return await analyze_market(req)


@router.post("/scan")
async def scan(req: ScanRequest):
    _reset_daily_if_needed()

    rows: List[Dict[str, Any]] = []
    for sym in req.symbols:
        try:
            res = await analyze_market(AnalyzeRequest(symbol=sym, timeframe=ENTRY_TF))
            sig = res.get("signal", {})
            rows.append({
                "symbol": sym,
                "direction": sig.get("direction", "HOLD"),
                "confidence": int(sig.get("confidence", 0)),
                "score_1_10": int(sig.get("score_1_10", 0)),
                "grade": sig.get("grade", "IGNORE"),
                "reason": sig.get("reason", ""),
                "entry": sig.get("entry"),
                "sl": sig.get("sl"),
                "tp": sig.get("tp2", sig.get("tp")),
                "tp1": sig.get("tp1"),
                "tp2": sig.get("tp2"),
                "entry_type": sig.get("entry_type", sig.get("mode", "")),
                "active_trade": bool(res.get("active", False)),
                "pending": False,
            })
        except Exception as e:
            rows.append({
                "symbol": sym,
                "direction": "HOLD",
                "confidence": 0,
                "score_1_10": 0,
                "grade": "IGNORE",
                "reason": str(e),
                "entry": None,
                "sl": None,
                "tp": None,
                "tp1": None,
                "tp2": None,
                "entry_type": "",
                "active_trade": False,
                "pending": False,
            })

    rows.sort(key=lambda x: (x.get("score_1_10", 0), x.get("confidence", 0)), reverse=True)

    return {
        "ranked": rows,
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
        "max_active_total": MAX_ACTIVE_TOTAL,
    }
# =========================================================
# LIVE / HISTORY / PERFORMANCE / WS
# =========================================================
@router.get("/live")
async def live_market(symbol: str = "R_10", timeframe: str = "5m") -> Dict[str, Any]:
    chart_tf = _requested_chart_tf(timeframe)
    app_id = os.getenv("DERIV_APP_ID", "1089")
    candles = await _cached_fetch_candles(app_id, symbol, chart_tf, 260)

    if not candles:
        return {"ok": False, "error": "No candles returned", "symbol": symbol, "timeframe": chart_tf}

    last = candles[-1]
    levels = calculate_levels(candles)

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": chart_tf,
        "price": round(safe_float(last["close"]), 5),
        "candles": candles,
        "levels": levels,
        "updated_at": _now_ts(),
    }


@router.get("/history")
async def history(symbol: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    items = TRADE_HISTORY
    if symbol:
        items = [t for t in items if t.get("symbol") == symbol.strip()]
    return {"count": len(items[-limit:]), "items": items[-limit:]}


@router.get("/performance")
async def performance(last_n: int = PERFORMANCE_REVIEW_N) -> Dict[str, Any]:
    return _performance(last_n)


@router.websocket("/ws")
async def websocket_live(ws: WebSocket):
    await ws.accept()

    try:
        raw = await ws.receive_text()
        msg = json.loads(raw)

        symbol = (msg.get("symbol") or "R_10").strip()
        timeframe = _requested_chart_tf(msg.get("timeframe") or ENTRY_TF)

        app_id = os.getenv("DERIV_APP_ID", "1089")
        last_sent_candle_time = None

        while True:
            candles = await _cached_fetch_candles(app_id, symbol, timeframe, 260)

            if candles:
                last = candles[-1]
                last_close = safe_float(last["close"])
                last_time = last.get("time") or last.get("epoch") or last.get("t")

                levels = calculate_levels(candles)

                if last_sent_candle_time != last_time:
                    await ws.send_json({
                        "type": "live_chart",
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "price": round(last_close, 5),
                        "candles": candles,
                        "levels": levels,
                        "updated_at": _now_ts(),
                    })
                    last_sent_candle_time = last_time
                else:
                    await ws.send_json({
                        "type": "live_tick",
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "price": round(last_close, 5),
                        "updated_at": _now_ts(),
                    })

            await asyncio.sleep(3)

    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass