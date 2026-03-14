from __future__ import annotations

import os
import time
import uuid
import json
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from utils.deriv import fetch_deriv_candles
from utils.telegram import send_telegram_message

router = APIRouter()

# =========================================================
# CONFIG
# =========================================================
ENTRY_TF = "5m"
BIAS_TF_1 = "15m"
BIAS_TF_2 = "1h"
FINE_TUNE_TF = "1m"  # optional micro-timing context only

EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14

SWING_LEN = 3
STRUCTURE_LOOKBACK = 90
RETEST_BARS = 8

SL_BUFFER_ATR = 0.28
TRAIL_BUFFER_ATR = 0.18
TRAIL_LOOKBACK = 8

MIN_RR = 2.0
MIN_EXPANSION_ATR = 1.10
MIN_DISPLACEMENT_BODY_ATR = 0.45
MIN_TREND_STRENGTH = 0.08
trade_progress_percent = 0

MIN_SIGNAL_GAP_SEC = 45
MIN_HOLD_SECONDS_AFTER_OPEN = 30

MAX_ACTIVE_TOTAL = 2
COOLDOWN_MIN_AFTER_LOSS = 5

BE_AFTER_TP1 = True
TRAIL_AFTER_TP1 = True

MAX_HISTORY = 500
PERFORMANCE_REVIEW_N = 30
LIVE_CACHE_TTL_SEC = 2

ZONE_LOOKBACK = 220
ZONE_CLUSTER_ATR = 0.32
ZONE_MIN_TOUCHES = 2

POOL_LOOKBACK = 160
POOL_CLUSTER_ATR = 0.24
POOL_MIN_TOUCHES = 2

# Smart Telegram throttling
TELEGRAM_MIN_BRIEFING_GAP_SEC = 1800
TELEGRAM_MIN_WATCH_GAP_SEC = 600
TELEGRAM_MIN_READY_GAP_SEC = 180
TELEGRAM_MIN_INVALIDATED_GAP_SEC = 300

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
    "cooldown_until": {},
}

LAST_SIGNAL_TS: Dict[str, int] = {}
LAST_TELEGRAM_TS: Dict[str, int] = {}
LAST_MARKET_STATE: Dict[str, str] = {}

# =========================================================
# REQUEST MODELS
# =========================================================
class AnalyzeRequest(BaseModel):
    symbol: str
    timeframe: str = ENTRY_TF


class ScanRequest(BaseModel):
    timeframe: str = ENTRY_TF
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


def fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "-"
    return f"{float(x):.5f}"


def fmt_range(low: Optional[float], high: Optional[float]) -> str:
    if low is None or high is None:
        return "-"
    return f"{low:.5f} - {high:.5f}"


def closed_candles(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return candles[:-1] if len(candles) >= 3 else candles


# =========================================================
# TELEGRAM / PERFORMANCE
# =========================================================
async def maybe_send_telegram(key: str, min_gap_sec: int, text: str) -> bool:
    now = _now_ts()
    last_ts = int(LAST_TELEGRAM_TS.get(key, 0))
    if (now - last_ts) < min_gap_sec:
        return False
    try:
        await send_telegram_message(text)
    except TypeError:
        send_telegram_message(text)
    LAST_TELEGRAM_TS[key] = now
    return True


def build_signal_message(symbol: str, timeframe: str, signal: Dict[str, Any]) -> str:
    direction = signal.get("direction", "-")
    confidence = signal.get("confidence", 0)
    entry = signal.get("entry", "-")
    sl = signal.get("sl", "-")
    tp1 = signal.get("tp1", "-")
    tp2 = signal.get("tp2", signal.get("tp", "-"))
    mode = signal.get("entry_type") or signal.get("mode") or "-"
    arrow = "🟢 BUY" if direction == "BUY" else "🔴 SELL"

    return (
        f"🚨 DEXTRADEZ SETUP READY\n\n"
        f"{arrow} {symbol}\n"
        f"Timeframe: {timeframe}\n"
        f"Setup: {mode}\n\n"
        f"Entry: {entry}\n"
        f"SL: {sl}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n\n"
        f"Confidence: {confidence}%"
    )


def build_briefing_message(symbol: str, timeframe: str, briefing: Dict[str, Any]) -> str:
    return (
        f"📊 DEXTRADEZ MARKET BRIEFING\n\n"
        f"Symbol: {symbol}\n"
        f"Timeframe: {timeframe}\n"
        f"Bias: {briefing.get('bias', '-')}\n"
        f"Market state: {briefing.get('market_state', '-')}\n"
        f"Structure: {briefing.get('structure', '-')}\n"
        f"Previous trend: {briefing.get('previous_trend', '-')}\n"
        f"Trend strength: {briefing.get('trend_strength', '-')}\n"
        f"Reversal risk: {briefing.get('reversal_risk', '-')}\n\n"
        f"Buyer zone: {briefing.get('buyer_zone', '-')}\n"
        f"Seller zone: {briefing.get('seller_zone', '-')}\n"
        f"AOI: {briefing.get('area_of_interest', '-')}\n"
        f"Liquidity below: {briefing.get('liquidity_below', '-')}\n"
        f"Liquidity above: {briefing.get('liquidity_above', '-')}\n\n"
        f"Preferred setup: {briefing.get('preferred_setup', '-')}\n"
        f"Confirmation needed: {briefing.get('confirmation_needed', '-')}\n"
        f"Invalidation: {briefing.get('invalidation', '-')}\n"
    )


def build_watch_message(symbol: str, timeframe: str, briefing: Dict[str, Any]) -> str:
    return (
        f"👀 DEXTRADEZ WATCHLIST\n\n"
        f"{symbol} • {timeframe}\n"
        f"Bias: {briefing.get('bias', '-')}\n"
        f"AOI: {briefing.get('area_of_interest', '-')}\n"
        f"Preferred setup: {briefing.get('preferred_setup', '-')}\n"
        f"Confirmation needed: {briefing.get('confirmation_needed', '-')}"
    )


def build_invalidated_message(symbol: str, timeframe: str, briefing: Dict[str, Any]) -> str:
    return (
        f"⚠️ DEXTRADEZ IDEA INVALIDATED\n\n"
        f"{symbol} • {timeframe}\n"
        f"Bias: {briefing.get('bias', '-')}\n"
        f"Reason: {briefing.get('invalidation', '-')}\n"
        f"Market state: {briefing.get('market_state', '-')}"
    )


def build_tp1_message(symbol: str, timeframe: str, trade: Dict[str, Any]) -> str:
    return (
        f"🎯 TP1 HIT\n\n"
        f"Symbol: {symbol}\n"
        f"Timeframe: {timeframe}\n"
        f"Direction: {trade.get('direction', '-')}\n"
        f"Entry: {trade.get('entry', '-')}\n"
        f"TP1: {trade.get('tp1', '-')}\n"
        f"TP2: {trade.get('tp2', trade.get('tp', '-'))}\n"
    )


def build_closed_message(symbol: str, timeframe: str, trade: Dict[str, Any], outcome: str, price: float) -> str:
    emoji = "🏆" if outcome == "TP2" else "🛑"
    return (
        f"{emoji} TRADE CLOSED\n\n"
        f"Symbol: {symbol}\n"
        f"Timeframe: {timeframe}\n"
        f"Direction: {trade.get('direction', '-')}\n"
        f"Outcome: {outcome}\n"
        f"Close Price: {round(price, 5)}"
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
# INDICATORS
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


def candle_body(c: Dict[str, float]) -> float:
    return abs(safe_float(c["close"]) - safe_float(c["open"]))


def candle_range(c: Dict[str, float]) -> float:
    return safe_float(c["high"]) - safe_float(c["low"])


def candle_body_ratio(c: Dict[str, float]) -> float:
    rng = max(1e-9, candle_range(c))
    return candle_body(c) / rng


def is_bullish(c: Dict[str, float]) -> bool:
    return safe_float(c["close"]) > safe_float(c["open"])


def is_bearish(c: Dict[str, float]) -> bool:
    return safe_float(c["close"]) < safe_float(c["open"])


def upper_wick(c: Dict[str, float]) -> float:
    o = safe_float(c["open"])
    cl = safe_float(c["close"])
    h = safe_float(c["high"])
    return h - max(o, cl)


def lower_wick(c: Dict[str, float]) -> float:
    o = safe_float(c["open"])
    cl = safe_float(c["close"])
    l = safe_float(c["low"])
    return min(o, cl) - l


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
# SWINGS / STRUCTURE / ZONES
# =========================================================
def find_swings(candles: List[Dict[str, float]], swing_len: int = SWING_LEN) -> Dict[str, List[Dict[str, Any]]]:
    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []

    if len(candles) < (swing_len * 2 + 1):
        return {"highs": highs, "lows": lows}

    for i in range(swing_len, len(candles) - swing_len):
        h = safe_float(candles[i]["high"])
        l = safe_float(candles[i]["low"])

        left = candles[i - swing_len : i]
        right = candles[i + 1 : i + 1 + swing_len]

        if all(h >= safe_float(c["high"]) for c in left + right):
            highs.append({"index": i, "price": h})

        if all(l <= safe_float(c["low"]) for c in left + right):
            lows.append({"index": i, "price": l})

    return {"highs": highs, "lows": lows}


def get_structure_state(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    data = candles[-STRUCTURE_LOOKBACK:] if len(candles) > STRUCTURE_LOOKBACK else candles
    swings = find_swings(data, SWING_LEN)
    highs = swings["highs"]
    lows = swings["lows"]

    if len(highs) < 2 or len(lows) < 2:
        return {
            "bias": "NEUTRAL",
            "bos_up": False,
            "bos_down": False,
            "last_swing_high": highs[-1]["price"] if highs else None,
            "last_swing_low": lows[-1]["price"] if lows else None,
            "swings": swings,
        }

    last_close = safe_float(data[-1]["close"])
    last_swing_high = highs[-1]
    last_swing_low = lows[-1]

    bos_up = last_close > (last_swing_high["price"] + atr_value * 0.03)
    bos_down = last_close < (last_swing_low["price"] - atr_value * 0.03)

    recent_high_prices = [x["price"] for x in highs[-3:]]
    recent_low_prices = [x["price"] for x in lows[-3:]]

    hh = recent_high_prices[-1] > recent_high_prices[-2]
    hl = recent_low_prices[-1] > recent_low_prices[-2]
    lh = recent_high_prices[-1] < recent_high_prices[-2]
    ll = recent_low_prices[-1] < recent_low_prices[-2]

    if hh and hl:
        bias = "UP"
    elif lh and ll:
        bias = "DOWN"
    elif bos_up:
        bias = "UP"
    elif bos_down:
        bias = "DOWN"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "bos_up": bos_up,
        "bos_down": bos_down,
        "last_swing_high": last_swing_high["price"],
        "last_swing_low": last_swing_low["price"],
        "swings": swings,
    }


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
    if len(candles) < 30 or atr_value <= 0:
        return {"supports": [], "resistances": [], "nearest_support": None, "nearest_resistance": None}

    data = candles[-ZONE_LOOKBACK:] if len(candles) >= ZONE_LOOKBACK else candles
    swings = find_swings(data, SWING_LEN)
    tol = max(1e-9, atr_value * ZONE_CLUSTER_ATR)

    lows = [x["price"] for x in swings["lows"]]
    highs = [x["price"] for x in swings["highs"]]

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
        return {"high_pools": [], "low_pools": [], "nearest_high_pool": None, "nearest_low_pool": None}

    data = candles[-POOL_LOOKBACK:] if len(candles) >= POOL_LOOKBACK else candles
    swings = find_swings(data, SWING_LEN)
    tol = max(1e-9, atr_value * POOL_CLUSTER_ATR)

    highs = [x["price"] for x in swings["highs"]]
    lows = [x["price"] for x in swings["lows"]]

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


# =========================================================
# MARKET INTELLIGENCE
# =========================================================
def combine_bias(entry_bias: str, bias_15m: str, bias_1h: str) -> str:
    votes = [entry_bias, bias_15m, bias_1h]
    up = sum(1 for x in votes if x == "UP")
    down = sum(1 for x in votes if x == "DOWN")

    if up >= 2:
        return "UP"
    if down >= 2:
        return "DOWN"

    if entry_bias == bias_15m and entry_bias in ("UP", "DOWN"):
        return entry_bias

    if entry_bias in ("UP", "DOWN"):
        return entry_bias

    return "NEUTRAL"


def classify_market_state(
    combined_bias: str,
    trend_strength: float,
    reversal_risk: str,
    fine_tune_trend: str,
) -> str:
    if combined_bias == "NEUTRAL":
        return "CHOPPY / NO-TRADE"
    if reversal_risk == "HIGH":
        return "REVERSAL RISK"
    if trend_strength >= 0.20 and fine_tune_trend == combined_bias:
        return "TRENDING CLEAN"
    if trend_strength >= 0.10:
        return "TRENDING PULLBACK"
    return "WEAK TREND"


def detect_reversal_risk(
    combined_bias: str,
    trend_entry: Dict[str, Any],
    trend_15m: Dict[str, Any],
    structure: Dict[str, Any],
    fine_tune_trend: str,
    nearest_support: Optional[Dict[str, Any]],
    nearest_resistance: Optional[Dict[str, Any]],
    current_price: float,
    atr_value: float,
) -> str:
    risk_points = 0

    if trend_entry.get("prev_trend") != trend_entry.get("trend"):
        risk_points += 1

    if combined_bias == "UP" and structure.get("bias") == "DOWN":
        risk_points += 2
    if combined_bias == "DOWN" and structure.get("bias") == "UP":
        risk_points += 2

    if combined_bias == "UP" and fine_tune_trend == "DOWN":
        risk_points += 1
    if combined_bias == "DOWN" and fine_tune_trend == "UP":
        risk_points += 1

    if trend_15m.get("trend") not in ("SIDEWAYS", combined_bias):
        risk_points += 1

    if atr_value > 0:
        if combined_bias == "UP" and nearest_resistance:
            dist = abs(float(nearest_resistance["level"]) - current_price)
            if dist < atr_value * 0.8:
                risk_points += 1
        if combined_bias == "DOWN" and nearest_support:
            dist = abs(current_price - float(nearest_support["level"]))
            if dist < atr_value * 0.8:
                risk_points += 1

    if risk_points >= 4:
        return "HIGH"
    if risk_points >= 2:
        return "MEDIUM"
    return "LOW"


def buyer_seller_zones(
    zones: Dict[str, Any],
) -> Dict[str, Optional[Dict[str, Any]]]:
    return {
        "buyer_zone": zones.get("nearest_support"),
        "seller_zone": zones.get("nearest_resistance"),
    }


def build_market_context(
    entry_data: List[Dict[str, float]],
    tf15_data: List[Dict[str, float]],
    tf1h_data: List[Dict[str, float]],
    fine_data: List[Dict[str, float]],
) -> Dict[str, Any]:
    atr_value = float(atr_last(entry_data, ATR_PERIOD) or 0.0)
    trend_entry = get_trend_memory(entry_data)
    trend_15m = get_trend_memory(tf15_data)
    trend_1h = get_trend_memory(tf1h_data)
    trend_fine = get_trend_memory(fine_data) if fine_data else {"trend": "SIDEWAYS"}

    structure = get_structure_state(entry_data, atr_value)
    combined_bias = combine_bias(
        structure.get("bias", "NEUTRAL"),
        trend_15m.get("trend", "NEUTRAL"),
        trend_1h.get("trend", "NEUTRAL"),
    )

    zones = reaction_zones(entry_data, atr_value)
    pools = liquidity_pools(entry_data, atr_value)
    zone_info = buyer_seller_zones(zones)

    current_price = safe_float(entry_data[-1]["close"]) if entry_data else 0.0

    nearest_support = zones.get("nearest_support")
    nearest_resistance = zones.get("nearest_resistance")
    nearest_low_pool = pools.get("nearest_low_pool")
    nearest_high_pool = pools.get("nearest_high_pool")

    reversal_risk = detect_reversal_risk(
        combined_bias=combined_bias,
        trend_entry=trend_entry,
        trend_15m=trend_15m,
        structure=structure,
        fine_tune_trend=trend_fine.get("trend", "SIDEWAYS"),
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        current_price=current_price,
        atr_value=atr_value,
    )

    if combined_bias == "UP":
        aoi = nearest_support or nearest_low_pool
        preferred_setup = "Wait for bullish pullback, sweep-and-reclaim, or breakout retest"
        confirmation_needed = "Bullish rejection candle or reclaim of local 5m high"
        invalidation = (
            f"5m closes below {fmt_price(aoi['low'])}" if aoi and aoi.get("low") is not None else "Break of bullish structure"
        )
    elif combined_bias == "DOWN":
        aoi = nearest_resistance or nearest_high_pool
        preferred_setup = "Wait for bearish rejection, failed retest, or sweep reversal"
        confirmation_needed = "Bearish rejection candle or loss of local 5m low"
        invalidation = (
            f"5m closes above {fmt_price(aoi['high'])}" if aoi and aoi.get("high") is not None else "Break of bearish structure"
        )
    else:
        aoi = None
        preferred_setup = "No clean directional edge"
        confirmation_needed = "Wait for higher timeframe and structure alignment"
        invalidation = "N/A"

    market_state = classify_market_state(
        combined_bias=combined_bias,
        trend_strength=float(trend_entry.get("trend_strength") or 0.0),
        reversal_risk=reversal_risk,
        fine_tune_trend=trend_fine.get("trend", "SIDEWAYS"),
    )

    return {
        "bias": combined_bias,
        "structure": structure.get("bias", "NEUTRAL"),
        "previous_trend": trend_entry.get("prev_trend", "SIDEWAYS"),
        "trend_strength": trend_entry.get("trend_strength", 0.0),
        "reversal_risk": reversal_risk,
        "market_state": market_state,
        "support": fmt_price(nearest_support["level"]) if nearest_support else "-",
        "resistance": fmt_price(nearest_resistance["level"]) if nearest_resistance else "-",
        "buyer_zone": fmt_range(
            zone_info["buyer_zone"]["low"], zone_info["buyer_zone"]["high"]
        ) if zone_info["buyer_zone"] else "-",
        "seller_zone": fmt_range(
            zone_info["seller_zone"]["low"], zone_info["seller_zone"]["high"]
        ) if zone_info["seller_zone"] else "-",
        "liquidity_below": fmt_price(nearest_low_pool["level"]) if nearest_low_pool else "-",
        "liquidity_above": fmt_price(nearest_high_pool["level"]) if nearest_high_pool else "-",
        "area_of_interest": fmt_range(aoi.get("low"), aoi.get("high")) if aoi else "-",
        "preferred_setup": preferred_setup,
        "confirmation_needed": confirmation_needed,
        "invalidation": invalidation,
        "zones": zones,
        "pools": pools,
        "structure_raw": structure,
        "trend_entry": trend_entry,
        "trend_15m": trend_15m,
        "trend_1h": trend_1h,
        "trend_fine": trend_fine,
        "atr_value": atr_value,
    }


# =========================================================
# TRADE SETUP DETECTION
# =========================================================
def nearest_obstacle_distance(direction: str, entry: float, zones: Dict[str, Any], pools: Dict[str, Any]) -> Optional[float]:
    candidates: List[float] = []

    if direction == "BUY":
        nr = zones.get("nearest_resistance")
        hp = pools.get("nearest_high_pool")
        if nr:
            candidates.append(float(nr["level"]))
        if hp:
            candidates.append(float(hp["level"]))
        above = [x for x in candidates if x > entry]
        return min(above) - entry if above else None

    ns = zones.get("nearest_support")
    lp = pools.get("nearest_low_pool")
    if ns:
        candidates.append(float(ns["level"]))
    if lp:
        candidates.append(float(lp["level"]))
    below = [x for x in candidates if x < entry]
    return entry - max(below) if below else None


def enough_room_to_run(direction: str, entry: float, tp2: float, zones: Dict[str, Any], pools: Dict[str, Any], atr_value: float) -> bool:
    obstacle_dist = nearest_obstacle_distance(direction, entry, zones, pools)
    projected = abs(tp2 - entry)

    if projected < atr_value * MIN_EXPANSION_ATR:
        return False

    if obstacle_dist is not None and obstacle_dist < atr_value * 0.75:
        return False

    return True


def strong_displacement(candle: Dict[str, float], atr_value: float, direction: str) -> bool:
    if atr_value <= 0:
        return False

    body = candle_body(candle)
    ratio = candle_body_ratio(candle)

    if body < atr_value * MIN_DISPLACEMENT_BODY_ATR:
        return False

    if ratio < 0.42:
        return False

    if direction == "BUY" and not is_bullish(candle):
        return False
    if direction == "SELL" and not is_bearish(candle):
        return False

    return True


def strong_rejection(candle: Dict[str, float], direction: str) -> bool:
    ratio = candle_body_ratio(candle)
    uw = upper_wick(candle)
    lw = lower_wick(candle)
    body = candle_body(candle)

    if direction == "BUY":
        return is_bullish(candle) and (ratio >= 0.35 or lw >= body * 0.6)
    if direction == "SELL":
        return is_bearish(candle) and (ratio >= 0.35 or uw >= body * 0.6)
    return False


def detect_trade_setup(entry_data: List[Dict[str, float]], context: Dict[str, Any]) -> Dict[str, Any]:
    if len(entry_data) < 25:
        return {"direction": "HOLD", "reason": "not_enough_candles"}

    combined_bias = context["bias"]
    atr_value = context["atr_value"]
    structure = context["structure_raw"]
    trend_info = context["trend_entry"]
    zones = context["zones"]
    pools = context["pools"]

    if combined_bias == "NEUTRAL" or atr_value <= 0:
        return {"direction": "HOLD", "reason": "neutral_bias"}

    if context.get("reversal_risk") == "HIGH":
        return {"direction": "HOLD", "reason": "reversal_risk_high"}

    last = entry_data[-1]
    prev = entry_data[-2]
    prev2 = entry_data[-3]

    last_close = safe_float(last["close"])
    trend_strength = float(trend_info.get("trend_strength") or 0.0)

    if combined_bias == "UP" and structure.get("last_swing_low"):
        swing_low = float(structure["last_swing_low"])
        swept = safe_float(prev["low"]) < swing_low
        closed_back = safe_float(prev["close"]) > swing_low
        if swept and closed_back and strong_displacement(last, atr_value, "BUY") and strong_rejection(last, "BUY") and trend_strength >= MIN_TREND_STRENGTH:
            entry = last_close
            sl = min(safe_float(prev["low"]), safe_float(prev2["low"]), safe_float(last["low"])) - atr_value * SL_BUFFER_ATR
            tp2 = entry + max((entry - sl) * MIN_RR, atr_value * MIN_EXPANSION_ATR)
            if entry > sl and enough_room_to_run("BUY", entry, tp2, zones, pools, atr_value):
                return {"direction": "BUY", "entry": entry, "sl": sl, "tp2": tp2, "reason": "smc_reversal_buy"}

    if combined_bias == "DOWN" and structure.get("last_swing_high"):
        swing_high = float(structure["last_swing_high"])
        swept = safe_float(prev["high"]) > swing_high
        closed_back = safe_float(prev["close"]) < swing_high
        if swept and closed_back and strong_displacement(last, atr_value, "SELL") and strong_rejection(last, "SELL") and trend_strength >= MIN_TREND_STRENGTH:
            entry = last_close
            sl = max(safe_float(prev["high"]), safe_float(prev2["high"]), safe_float(last["high"])) + atr_value * SL_BUFFER_ATR
            tp2 = entry - max((sl - entry) * MIN_RR, atr_value * MIN_EXPANSION_ATR)
            if sl > entry and enough_room_to_run("SELL", entry, tp2, zones, pools, atr_value):
                return {"direction": "SELL", "entry": entry, "sl": sl, "tp2": tp2, "reason": "smc_reversal_sell"}

    closes = [safe_float(c["close"]) for c in entry_data]
    ema20 = float(ema_last(closes, EMA_FAST) or 0.0)

    if combined_bias == "UP":
        near_ema = safe_float(last["low"]) <= ema20 + atr_value * 0.25
        if near_ema and strong_rejection(last, "BUY") and strong_displacement(prev, atr_value, "BUY") and safe_float(last["close"]) > ema20 and trend_strength >= MIN_TREND_STRENGTH:
            entry = last_close
            sl = min(safe_float(prev["low"]), safe_float(last["low"])) - atr_value * SL_BUFFER_ATR
            tp2 = entry + max((entry - sl) * MIN_RR, atr_value * MIN_EXPANSION_ATR)
            if entry > sl and enough_room_to_run("BUY", entry, tp2, zones, pools, atr_value):
                return {"direction": "BUY", "entry": entry, "sl": sl, "tp2": tp2, "reason": "pullback_continuation_buy"}

    if combined_bias == "DOWN":
        near_ema = safe_float(last["high"]) >= ema20 - atr_value * 0.25
        if near_ema and strong_rejection(last, "SELL") and strong_displacement(prev, atr_value, "SELL") and safe_float(last["close"]) < ema20 and trend_strength >= MIN_TREND_STRENGTH:
            entry = last_close
            sl = max(safe_float(prev["high"]), safe_float(last["high"])) + atr_value * SL_BUFFER_ATR
            tp2 = entry - max((sl - entry) * MIN_RR, atr_value * MIN_EXPANSION_ATR)
            if sl > entry and enough_room_to_run("SELL", entry, tp2, zones, pools, atr_value):
                return {"direction": "SELL", "entry": entry, "sl": sl, "tp2": tp2, "reason": "pullback_continuation_sell"}

    return {"direction": "HOLD", "reason": f"no_setup_{combined_bias.lower()}"}


def build_signal_from_setup(setup: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    if setup.get("direction") not in ("BUY", "SELL"):
        return {"direction": "HOLD", "confidence": 0, "reason": setup.get("reason", "no_setup")}

    direction = setup["direction"]
    entry = float(setup["entry"])
    sl = float(setup["sl"])
    tp2 = float(setup["tp2"])
    risk = abs(entry - sl)

    if risk <= 0:
        return {"direction": "HOLD", "confidence": 0, "reason": "invalid_risk"}

    tp1 = entry + risk if direction == "BUY" else entry - risk
    trend_strength = float(context["trend_entry"].get("trend_strength") or 0.0)
    confidence = 70

    if trend_strength >= 0.18:
        confidence += 6
    elif trend_strength >= 0.10:
        confidence += 3

    if "smc_reversal" in setup["reason"]:
        confidence += 7
    elif "pullback_continuation" in setup["reason"]:
        confidence += 4

    if context.get("reversal_risk") == "MEDIUM":
        confidence -= 4

    confidence = max(0, min(92, confidence))

    return {
        "direction": direction,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp": round(tp2, 5),
        "tp1": round(tp1, 5),
        "tp2": round(tp2, 5),
        "confidence": confidence,
        "reason": setup["reason"],
        "entry_type": setup["reason"].upper(),
        "mode": setup["reason"].upper(),
        "r_multiple": round(abs(tp2 - entry) / risk, 2),
        "meta": {
            "bias": context["bias"],
            "structure": context["structure"],
            "trend_strength": context["trend_strength"],
            "area_of_interest": context["area_of_interest"],
            "confirmation_needed": context["confirmation_needed"],
            "reversal_risk": context["reversal_risk"],
        },
    }


# =========================================================
# TRADE MANAGEMENT
# =========================================================
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
    LAST_SIGNAL_TS[symbol] = _now_ts()
    _push_history(dict(trade))
    await maybe_send_telegram(
        key=f"ready:{symbol}:{timeframe}",
        min_gap_sec=TELEGRAM_MIN_READY_GAP_SEC,
        text=build_signal_message(symbol, timeframe, trade),
    )
    return trade


async def close_trade(symbol: str, timeframe: str, trade: Dict[str, Any], outcome: str, price: float) -> Dict[str, Any]:
    trade["status"] = "CLOSED"
    trade["outcome"] = outcome
    trade["closed_price"] = round(price, 5)
    trade["closed_at"] = _now_ts()

    ACTIVE_TRADES.pop(_trade_key(symbol, timeframe), None)

    if outcome == "SL":
        RISK_STATE["daily_R"] += -1.0
        RISK_STATE["cooldown_until"][symbol] = _now_ts() + COOLDOWN_MIN_AFTER_LOSS * 60
    elif outcome == "TP2":
        RISK_STATE["daily_R"] += float(trade.get("r_multiple") or 1.0)

    idx = _find_history_index(trade["trade_id"])
    if idx is not None:
        TRADE_HISTORY[idx] = {**TRADE_HISTORY[idx], **trade}

    await maybe_send_telegram(
        key=f"closed:{symbol}:{timeframe}",
        min_gap_sec=5,
        text=build_closed_message(symbol, timeframe, trade, outcome, price),
    )
    return trade


async def manage_active_trade(symbol: str, timeframe: str, candles_tf: List[Dict[str, float]], trade: Dict[str, Any]) -> Dict[str, Any]:
    if len(candles_tf) < 2:
        return {"signal": trade, "active": True, "actions": []}

    last = closed_candles(candles_tf)[-1]
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

    if direction == "BUY":
        if last_low <= sl:
            closed = await close_trade(symbol, timeframe, trade, "SL", sl)
            return {"closed_trade": closed, "signal": {"direction": "HOLD", "confidence": 0, "reason": "trade_closed_sl"}, "active": False, "actions": actions}

        if tp1 and not trade.get("tp1_hit", False) and last_high >= tp1:
            trade["tp1_hit"] = True
            actions.append({"type": "TP1_HIT"})
            if BE_AFTER_TP1:
                trade["sl"] = entry
                actions.append({"type": "MOVE_SL", "to": round(entry, 5)})
            await maybe_send_telegram(
                key=f"tp1:{symbol}:{timeframe}:{trade['trade_id']}",
                min_gap_sec=5,
                text=build_tp1_message(symbol, timeframe, trade),
            )

        if last_high >= tp2:
            closed = await close_trade(symbol, timeframe, trade, "TP2", tp2)
            return {"closed_trade": closed, "signal": {"direction": "HOLD", "confidence": 0, "reason": "trade_closed_tp2"}, "active": False, "actions": actions}
    else:
        if last_high >= sl:
            closed = await close_trade(symbol, timeframe, trade, "SL", sl)
            return {"closed_trade": closed, "signal": {"direction": "HOLD", "confidence": 0, "reason": "trade_closed_sl"}, "active": False, "actions": actions}

        if tp1 and not trade.get("tp1_hit", False) and last_low <= tp1:
            trade["tp1_hit"] = True
            actions.append({"type": "TP1_HIT"})
            if BE_AFTER_TP1:
                trade["sl"] = entry
                actions.append({"type": "MOVE_SL", "to": round(entry, 5)})
            await maybe_send_telegram(
                key=f"tp1:{symbol}:{timeframe}:{trade['trade_id']}",
                min_gap_sec=5,
                text=build_tp1_message(symbol, timeframe, trade),
            )

        if last_low <= tp2:
            closed = await close_trade(symbol, timeframe, trade, "TP2", tp2)
            return {"closed_trade": closed, "signal": {"direction": "HOLD", "confidence": 0, "reason": "trade_closed_tp2"}, "active": False, "actions": actions}

    if TRAIL_AFTER_TP1 and trade.get("tp1_hit"):
        atr_value = float(atr_last(closed_candles(candles_tf), ATR_PERIOD) or 0.0)
        if atr_value > 0:
            trail = trail_stop_suggestion(closed_candles(candles_tf), direction, atr_value)
            if direction == "BUY":
                trade["sl"] = max(safe_float(trade["sl"]), trail)
            else:
                trade["sl"] = min(safe_float(trade["sl"]), trail)
            actions.append({"type": "TRAIL_SUGGESTION", "to": round(trade["sl"], 5)})

    trade["progress_pct"] = round(trade_progress_percent(direction, entry, safe_float(trade["sl"]), tp2, last_close), 2)
    return {"signal": trade, "active": True, "actions": actions}


# =========================================================
# TELEGRAM SMART MARKET STATE
# =========================================================
async def maybe_emit_market_messages(
    symbol: str,
    timeframe: str,
    context: Dict[str, Any],
    signal: Dict[str, Any],
) -> None:
    state = "INFO"
    if signal.get("direction") in ("BUY", "SELL"):
        state = "READY"
    else:
        market_state = context.get("market_state", "")
        if context.get("bias") in ("UP", "DOWN") and context.get("area_of_interest") != "-" and market_state not in ("CHOPPY / NO-TRADE",):
            state = "WATCH"
        if context.get("reversal_risk") == "HIGH":
            state = "INVALIDATED"

    state_key = f"{symbol}:{timeframe}"
    prev_state = LAST_MARKET_STATE.get(state_key)

    if prev_state == state:
        return

    LAST_MARKET_STATE[state_key] = state

    if state == "WATCH":
        await maybe_send_telegram(
            key=f"watch:{symbol}:{timeframe}",
            min_gap_sec=TELEGRAM_MIN_WATCH_GAP_SEC,
            text=build_watch_message(symbol, timeframe, context),
        )
    elif state == "READY":
        return
    elif state == "INVALIDATED":
        await maybe_send_telegram(
            key=f"invalidated:{symbol}:{timeframe}",
            min_gap_sec=TELEGRAM_MIN_INVALIDATED_GAP_SEC,
            text=build_invalidated_message(symbol, timeframe, context),
        )
    else:
        await maybe_send_telegram(
            key=f"briefing:{symbol}:{timeframe}",
            min_gap_sec=TELEGRAM_MIN_BRIEFING_GAP_SEC,
            text=build_briefing_message(symbol, timeframe, context),
        )


# =========================================================
# ANALYZE CORE + ROUTES + WS
# =========================================================
async def analyze_market(req: AnalyzeRequest):
    _reset_daily_if_needed()

    symbol = req.symbol.strip()
    timeframe = _requested_chart_tf(req.timeframe or ENTRY_TF)
    key = _trade_key(symbol, timeframe)

    app_id = os.getenv("DERIV_APP_ID", "1089")

    candles_entry = await _cached_fetch_candles(app_id, symbol, timeframe, 320)
    candles_15m = await _cached_fetch_candles(app_id, symbol, BIAS_TF_1, 320)
    candles_1h = await _cached_fetch_candles(app_id, symbol, BIAS_TF_2, 320)
    candles_1m = await _cached_fetch_candles(app_id, symbol, FINE_TUNE_TF, 220)

    if not candles_entry:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": None,
            "candles": [],
            "levels": {"supports": [], "resistances": []},
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "no_candles"},
            "briefing": {},
            "active": False,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }

    entry_data = closed_candles(candles_entry)
    tf15_data = closed_candles(candles_15m)
    tf1h_data = closed_candles(candles_1h)
    fine_data = closed_candles(candles_1m)

    last_live = candles_entry[-1]
    price = safe_float(last_live["close"])

    context = build_market_context(entry_data, tf15_data, tf1h_data, fine_data)
    zones = context["zones"]
    levels = {
        "supports": [z["level"] for z in zones.get("supports", [])[:6]],
        "resistances": [z["level"] for z in zones.get("resistances", [])[:6]],
    }

    if key in ACTIVE_TRADES:
        managed = await manage_active_trade(symbol, timeframe, candles_entry, ACTIVE_TRADES[key])
        result = {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_entry,
            "levels": levels,
            "briefing": context,
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

    hold_reason = ""
    if _active_total() >= MAX_ACTIVE_TOTAL:
        hold_reason = "max_active_trades"
    elif _now_ts() < int(RISK_STATE.get("cooldown_until", {}).get(symbol, 0)):
        hold_reason = "cooldown_active"
    elif (_now_ts() - int(LAST_SIGNAL_TS.get(symbol, 0))) < MIN_SIGNAL_GAP_SEC:
        hold_reason = "signal_gap_active"

    setup = detect_trade_setup(entry_data, context)
    signal = build_signal_from_setup(setup, context)

    if hold_reason:
        signal = {"direction": "HOLD", "confidence": 0, "reason": hold_reason}

    await maybe_emit_market_messages(symbol, timeframe, context, signal)

    if signal.get("direction") in ("BUY", "SELL"):
        opened = await open_new_trade(symbol, timeframe, signal)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_entry,
            "levels": levels,
            "briefing": context,
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
        "candles": candles_entry,
        "levels": levels,
        "briefing": context,
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

    timeframe = _requested_chart_tf(req.timeframe or ENTRY_TF)
    rows: List[Dict[str, Any]] = []

    for sym in req.symbols:
        try:
            res = await analyze_market(AnalyzeRequest(symbol=sym, timeframe=timeframe))
            sig = res.get("signal", {})
            brief = res.get("briefing", {})
            rows.append({
                "symbol": sym,
                "direction": sig.get("direction", "HOLD"),
                "confidence": int(sig.get("confidence", 0)),
                "reason": sig.get("reason", ""),
                "entry": sig.get("entry"),
                "sl": sig.get("sl"),
                "tp": sig.get("tp2", sig.get("tp")),
                "entry_type": sig.get("entry_type", sig.get("mode", "")),
                "active_trade": bool(res.get("active", False)),
                "bias": brief.get("bias"),
                "market_state": brief.get("market_state"),
                "area_of_interest": brief.get("area_of_interest"),
                "preferred_setup": brief.get("preferred_setup"),
                "reversal_risk": brief.get("reversal_risk"),
            })
        except Exception as e:
            rows.append({
                "symbol": sym,
                "direction": "HOLD",
                "confidence": 0,
                "reason": str(e),
                "entry": None,
                "sl": None,
                "tp": None,
                "entry_type": "",
                "active_trade": False,
                "bias": None,
                "market_state": None,
                "area_of_interest": None,
                "preferred_setup": None,
                "reversal_risk": None,
            })

    rows.sort(
        key=lambda x: (
            x.get("direction") in ("BUY", "SELL"),
            x.get("confidence", 0),
            x.get("market_state") == "TRENDING CLEAN",
        ),
        reverse=True,
    )

    return {
        "ranked": rows,
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
        "max_active_total": MAX_ACTIVE_TOTAL,
    }


@router.get("/live")
async def live_market(symbol: str = "R_10", timeframe: str = ENTRY_TF) -> Dict[str, Any]:
    chart_tf = _requested_chart_tf(timeframe)
    app_id = os.getenv("DERIV_APP_ID", "1089")
    candles = await _cached_fetch_candles(app_id, symbol, chart_tf, 320)

    if not candles:
        return {"ok": False, "error": "No candles returned", "symbol": symbol, "timeframe": chart_tf}

    last = candles[-1]
    entry_data = closed_candles(candles)
    atr_value = float(atr_last(entry_data, ATR_PERIOD) or 0.0)
    zones = reaction_zones(entry_data, atr_value)

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": chart_tf,
        "price": round(safe_float(last["close"]), 5),
        "candles": candles,
        "levels": {
            "supports": [z["level"] for z in zones.get("supports", [])[:6]],
            "resistances": [z["level"] for z in zones.get("resistances", [])[:6]],
        },
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
            candles = await _cached_fetch_candles(app_id, symbol, timeframe, 320)

            if candles:
                last = candles[-1]
                last_close = safe_float(last["close"])
                last_time = last.get("time") or last.get("epoch") or last.get("t")
                entry_data = closed_candles(candles)
                atr_value = float(atr_last(entry_data, ATR_PERIOD) or 0.0)
                zones = reaction_zones(entry_data, atr_value)

                payload = {
                    "type": "live_chart",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "price": round(last_close, 5),
                    "candles": candles,
                    "levels": {
                        "supports": [z["level"] for z in zones.get("supports", [])[:6]],
                        "resistances": [z["level"] for z in zones.get("resistances", [])[:6]],
                    },
                    "updated_at": _now_ts(),
                }

                if last_sent_candle_time != last_time:
                    await ws.send_json(payload)
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