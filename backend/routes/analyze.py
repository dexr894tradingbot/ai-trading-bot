# backend/analyze.py
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from utils.deriv import fetch_deriv_candles

router = APIRouter()

# =========================================================
# CONFIG (UPGRADES + STABILITY + UI FIELDS)
# - Weighted alignment bias: 30m + 1h + 4h
# - Entry TF: 5m
# - Lock signals into ACTIVE_TRADES until TP2/SL
# - Return extra fields for UI: entry_type, bias_score/details, mode
# =========================================================
ENTRY_TF = "5m"

# Weighted alignment timeframes
ALIGN_TFS = ["30m", "1h", "4h"]
ALIGN_WEIGHTS = {"30m": 0.25, "1h": 0.35, "4h": 0.40}
ALIGN_MIN_SCORE = 0.60      # must be strong enough to trade
ALIGN_MIN_MARGIN = 0.12     # buy_score and sell_score must not be too close

EMA_FAST = 20
EMA_SLOW = 50
EMA_ENTRY = 20

ATR_PERIOD = 14

# --- Regime filter
USE_REGIME_FILTER = True
EMA_CROSS_LOOKBACK = 45
EMA_CROSS_MAX = 6
ATR_COMP_LOOKBACK = 70
ATR_COMP_RATIO_MIN = 0.65

# --- Trend strength gate (per TF in bias calc)
USE_CHOP_FILTER = True
TREND_STRENGTH_MIN = 0.35

# --- Rejection candle
MIN_BODY_TO_RANGE = 0.30
MIN_WICK_TO_RANGE = 0.25
REJECTION_CLOSE_OUTSIDE_ZONE = True

# --- Zones
ZONE_LOOKBACK = 160
ZONE_ATR_MULT = 0.25
MIN_ZONE_TOUCHES = 2
RETURN_TOP_ZONES = 8

# --- BOS / Retest (Sniper path)
BOS_LOOKBACK = 12
RETEST_TOL_ATR = 0.35
RETEST_MAX_CANDLES = 18

# --- SL/TP logic
SL_BUFFER_ATR = 0.20
MAX_SL_ATR = 2.50

MIN_TP1_ATR = 0.8
MIN_TP2_ATR = 1.5
RR_FALLBACK_TP2 = 4.0

# --- Trade management guidance
BE_BUFFER_R = 0.10
PARTIAL_TP_PERCENT = 0.5
TRAIL_LOOKBACK = 12
TRAIL_BUFFER_ATR = 0.20

# --- Risk gates
MAX_ACTIVE_TOTAL = 5                  # ✅ UPDATED (was 2)
DAILY_LOSS_LIMIT_R = -3.0
COOLDOWN_MIN_AFTER_LOSS = 20
MAX_SIGNALS_PER_DAY_PER_SYMBOL = 5

# --- Hybrid mode
ENABLE_FAST_ENTRY = True
FAST_ENTRY_MIN_CONF = 70

# --- Signal locking
ENABLE_SIGNAL_LOCK = True
MIN_LOCK_CONF = 70

# --- History
MAX_HISTORY = 500
PERFORMANCE_REVIEW_N = 30

# =========================================================
# STATE (in-memory)
# =========================================================
ACTIVE_TRADES: Dict[str, Dict[str, Any]] = {}
PENDING_SETUPS: Dict[str, Dict[str, Any]] = {}
TRADE_HISTORY: List[Dict[str, Any]] = []

RISK_STATE: Dict[str, Any] = {
    "day_key": None,
    "daily_R": 0.0,
    "signals_today": {},
    "cooldown_until": {},
}

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
# HELPERS
# =========================================================
def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _reset_daily_if_needed() -> None:
    dk = _today_key()
    if RISK_STATE.get("day_key") != dk:
        RISK_STATE["day_key"] = dk
        RISK_STATE["daily_R"] = 0.0
        RISK_STATE["signals_today"] = {}


def _active_total() -> int:
    return len(ACTIVE_TRADES)


def _now_ts() -> int:
    return int(time.time())


def _trade_key(symbol: str, tf: str) -> str:
    return f"{symbol}:{tf}"


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


# =========================================================
# INDICATORS
# =========================================================
def ema_last(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    e = values[0]
    out[0] = None
    for i in range(1, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e if i >= period - 1 else None
    return out


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


def atr_series_sma(candles: List[Dict[str, float]], period: int = ATR_PERIOD) -> List[Optional[float]]:
    if len(candles) < period + 1:
        return [None] * len(candles)
    trs: List[float] = [0.0] * len(candles)
    trs[0] = 0.0
    for i in range(1, len(candles)):
        h = safe_float(candles[i]["high"])
        l = safe_float(candles[i]["low"])
        pc = safe_float(candles[i - 1]["close"])
        trs[i] = max(h - l, abs(h - pc), abs(l - pc))
    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period, len(candles)):
        window = trs[i - period + 1 : i + 1]
        out[i] = sum(window) / period
    return out


def is_bullish(c: Dict[str, float]) -> bool:
    return safe_float(c["close"]) > safe_float(c["open"])


def is_bearish(c: Dict[str, float]) -> bool:
    return safe_float(c["close"]) < safe_float(c["open"])


def candle_body_ratio(c: Dict[str, float]) -> float:
    o = safe_float(c["open"])
    cl = safe_float(c["close"])
    h = safe_float(c["high"])
    l = safe_float(c["low"])
    rng = max(1e-9, h - l)
    return abs(cl - o) / rng


def candle_wick_ratio(c: Dict[str, float], direction: str) -> float:
    o = safe_float(c["open"])
    cl = safe_float(c["close"])
    h = safe_float(c["high"])
    l = safe_float(c["low"])
    rng = max(1e-9, h - l)
    if direction == "BUY":
        lower_wick = min(o, cl) - l
        return max(0.0, lower_wick / rng)
    upper_wick = h - max(o, cl)
    return max(0.0, upper_wick / rng)


def recent_swing_low(candles: List[Dict[str, float]], lookback: int = 20) -> float:
    data = candles[-lookback:]
    return min(safe_float(c["low"]) for c in data)


def recent_swing_high(candles: List[Dict[str, float]], lookback: int = 20) -> float:
    data = candles[-lookback:]
    return max(safe_float(c["high"]) for c in data)


# =========================================================
# ZONES
# =========================================================
def build_pivots(candles: List[Dict[str, float]], side: str, pivot_left: int = 2, pivot_right: int = 2) -> List[float]:
    highs = [safe_float(c["high"]) for c in candles]
    lows = [safe_float(c["low"]) for c in candles]
    pivots: List[float] = []
    for i in range(pivot_left, len(candles) - pivot_right):
        if side == "resistance":
            window = highs[i - pivot_left : i + pivot_right + 1]
            if highs[i] == max(window):
                pivots.append(highs[i])
        else:
            window = lows[i - pivot_left : i + pivot_right + 1]
            if lows[i] == min(window):
                pivots.append(lows[i])
    return pivots


def cluster_to_zones(prices: List[float], zone_width: float) -> List[Dict[str, float]]:
    if not prices:
        return []
    prices = sorted(prices)
    groups: List[List[float]] = []
    cur = [prices[0]]
    for p in prices[1:]:
        if abs(p - cur[-1]) <= zone_width:
            cur.append(p)
        else:
            groups.append(cur)
            cur = [p]
    groups.append(cur)

    zones: List[Dict[str, float]] = []
    for g in groups:
        zones.append({
            "low": float(min(g)),
            "high": float(max(g)),
            "mid": float(sum(g) / len(g)),
            "pivot_count": float(len(g)),
            "touches": 0.0,
        })
    return zones


def count_zone_touches(candles: List[Dict[str, float]], zlow: float, zhigh: float) -> int:
    touches = 0
    for c in candles:
        h = safe_float(c["high"])
        l = safe_float(c["low"])
        if h >= zlow and l <= zhigh:
            touches += 1
    return touches


def strongest_zones(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    data = candles[-ZONE_LOOKBACK:] if len(candles) >= ZONE_LOOKBACK else candles
    zone_width = max(atr_value * ZONE_ATR_MULT, 1e-6)

    sup_pivots = build_pivots(data, "support")
    res_pivots = build_pivots(data, "resistance")

    sup_zones = cluster_to_zones(sup_pivots, zone_width)
    res_zones = cluster_to_zones(res_pivots, zone_width)

    for z in sup_zones:
        z["touches"] = float(count_zone_touches(data, z["low"], z["high"]))
    for z in res_zones:
        z["touches"] = float(count_zone_touches(data, z["low"], z["high"]))

    sup_zones = [z for z in sup_zones if z["touches"] >= MIN_ZONE_TOUCHES]
    res_zones = [z for z in res_zones if z["touches"] >= MIN_ZONE_TOUCHES]

    last_close = safe_float(data[-1]["close"])

    sup_zones.sort(key=lambda z: (-z["touches"], abs(last_close - z["mid"])))
    res_zones.sort(key=lambda z: (-z["touches"], abs(last_close - z["mid"])))

    return {
        "support_zone": sup_zones[0] if sup_zones else None,
        "resistance_zone": res_zones[0] if res_zones else None,
        "support_zones": sup_zones[:RETURN_TOP_ZONES],
        "resistance_zones": res_zones[:RETURN_TOP_ZONES],
        "zone_width": zone_width,
    }


def price_overlaps_zone(candle: Dict[str, float], zone: Dict[str, float]) -> bool:
    h = safe_float(candle["high"])
    l = safe_float(candle["low"])
    return h >= safe_float(zone["low"]) and l <= safe_float(zone["high"])


# =========================================================
# REGIME FILTER
# =========================================================
def regime_ok(candles_5m: List[Dict[str, float]]) -> Dict[str, Any]:
    if not USE_REGIME_FILTER:
        return {"ok": True, "reason": "regime_filter_off"}

    if len(candles_5m) < max(EMA_CROSS_LOOKBACK + 5, ATR_COMP_LOOKBACK + 5):
        return {"ok": True, "reason": "regime_ok_not_enough_data"}

    closes = [safe_float(c["close"]) for c in candles_5m]
    ema20 = ema_series(closes, EMA_ENTRY)

    crosses = 0
    look = EMA_CROSS_LOOKBACK
    start = len(candles_5m) - look
    for i in range(start + 1, len(candles_5m)):
        if ema20[i] is None or ema20[i - 1] is None:
            continue
        prev_side = closes[i - 1] - float(ema20[i - 1])
        cur_side = closes[i] - float(ema20[i])
        if prev_side == 0:
            continue
        if (prev_side > 0 and cur_side < 0) or (prev_side < 0 and cur_side > 0):
            crosses += 1

    if crosses > EMA_CROSS_MAX:
        return {"ok": False, "reason": f"regime_block_chop (ema_crosses={crosses})", "ema_crosses": crosses}

    atrs = atr_series_sma(candles_5m, ATR_PERIOD)
    recent_atrs = [a for a in atrs[-ATR_COMP_LOOKBACK:] if a is not None]
    if len(recent_atrs) >= 20:
        cur_atr = float(recent_atrs[-1])
        avg_atr = sum(recent_atrs) / len(recent_atrs)
        if avg_atr > 0 and (cur_atr / avg_atr) < ATR_COMP_RATIO_MIN:
            return {
                "ok": False,
                "reason": f"regime_block_compress (atr_ratio={cur_atr/avg_atr:.2f})",
                "atr_ratio": round(cur_atr / avg_atr, 3),
            }

    return {"ok": True, "reason": "regime_ok"}


# =========================================================
# WEIGHTED ALIGNMENT BIAS (30m/1h/4h)
# =========================================================
def _tf_bias(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    """
    Bias = EMA20 vs EMA50. Strength = abs(ema20-ema50)/ATR.
    """
    if len(candles) < 120:
        return {"direction": "HOLD", "reason": "Not enough data", "meta": {"trend_strength": 0.0}}

    closes = [safe_float(c["close"]) for c in candles]
    ema20 = ema_last(closes[-90:], EMA_FAST)
    ema50 = ema_last(closes[-180:], EMA_SLOW)
    a = atr_last(candles, ATR_PERIOD)

    if None in (ema20, ema50, a) or float(a or 0.0) <= 0:
        return {"direction": "HOLD", "reason": "Indicators not ready", "meta": {"trend_strength": 0.0}}

    ema20 = float(ema20)
    ema50 = float(ema50)
    a = float(a)

    bias = "BUY" if ema20 > ema50 else "SELL" if ema20 < ema50 else "HOLD"
    ts = abs(ema20 - ema50) / max(1e-9, a)

    if USE_CHOP_FILTER and ts < TREND_STRENGTH_MIN:
        return {
            "direction": "HOLD",
            "reason": f"Choppy (trend_strength {ts:.2f})",
            "meta": {"trend_strength": round(ts, 3), "atr": round(a, 6)},
        }

    return {"direction": bias, "reason": f"{bias} bias ok", "meta": {"trend_strength": round(ts, 3)}}


def weighted_alignment(candles_by_tf: Dict[str, List[Dict[str, float]]]) -> Dict[str, Any]:
    buy_score = 0.0
    sell_score = 0.0
    details: Dict[str, Any] = {}

    for tf in ALIGN_TFS:
        w = float(ALIGN_WEIGHTS.get(tf, 0.0))
        cs = candles_by_tf.get(tf) or []
        b = _tf_bias(cs)
        d = b.get("direction", "HOLD")
        ts = float((b.get("meta") or {}).get("trend_strength") or 0.0)
        ts01 = min(1.0, max(0.0, ts / 1.0))  # normalize

        if d == "BUY":
            buy_score += w * ts01
        elif d == "SELL":
            sell_score += w * ts01

        details[tf] = {
            "dir": d,
            "trend_strength": ts,
            "weight": w,
            "reason": b.get("reason", ""),
        }

    margin = abs(buy_score - sell_score)
    best = max(buy_score, sell_score)
    direction = "BUY" if buy_score > sell_score else "SELL" if sell_score > buy_score else "HOLD"

    if best < ALIGN_MIN_SCORE:
        return {
            "direction": "HOLD",
            "score": round(best, 3),
            "buy": round(buy_score, 3),
            "sell": round(sell_score, 3),
            "margin": round(margin, 3),
            "details": details,
            "reason": "Alignment too weak",
        }

    if margin < ALIGN_MIN_MARGIN:
        return {
            "direction": "HOLD",
            "score": round(best, 3),
            "buy": round(buy_score, 3),
            "sell": round(sell_score, 3),
            "margin": round(margin, 3),
            "details": details,
            "reason": "Alignment unclear (too close)",
        }

    return {
        "direction": direction,
        "score": round(best, 3),
        "buy": round(buy_score, 3),
        "sell": round(sell_score, 3),
        "margin": round(margin, 3),
        "details": details,
        "reason": "Weighted alignment ok",
    }


# =========================================================
# BOS / RETEST
# =========================================================
def bos_level(candles: List[Dict[str, float]], direction: str, lookback: int = BOS_LOOKBACK) -> Optional[float]:
    if len(candles) < lookback + 2:
        return None
    recent = candles[-(lookback + 1) : -1]
    if direction == "BUY":
        return max(safe_float(c["high"]) for c in recent)
    if direction == "SELL":
        return min(safe_float(c["low"]) for c in recent)
    return None


def bos_confirm(candles: List[Dict[str, float]], direction: str, lookback: int = BOS_LOOKBACK) -> bool:
    lvl = bos_level(candles, direction, lookback)
    if lvl is None:
        return False
    last_close = safe_float(candles[-1]["close"])
    return (last_close > lvl) if direction == "BUY" else (last_close < lvl)


def in_retest_zone(c: Dict[str, float], level: float, tol: float) -> bool:
    h = safe_float(c["high"])
    l = safe_float(c["low"])
    return (l <= level + tol) and (h >= level - tol)


# =========================================================
# REJECTION
# =========================================================
def rejection_ok(last: Dict[str, float], direction: str, zone: Dict[str, float]) -> bool:
    if not zone:
        return False

    cl = safe_float(last["close"])
    h = safe_float(last["high"])
    l = safe_float(last["low"])

    zlow = safe_float(zone["low"])
    zhigh = safe_float(zone["high"])

    body_ok = candle_body_ratio(last) >= MIN_BODY_TO_RANGE
    wick_ok = candle_wick_ratio(last, direction) >= MIN_WICK_TO_RANGE

    if direction == "BUY":
        wick_into = l <= zhigh
        close_out = cl >= zhigh if REJECTION_CLOSE_OUTSIDE_ZONE else cl >= zlow
        return is_bullish(last) and wick_into and close_out and body_ok and wick_ok

    wick_into = h >= zlow
    close_out = cl <= zlow if REJECTION_CLOSE_OUTSIDE_ZONE else cl <= zhigh
    return is_bearish(last) and wick_into and close_out and body_ok and wick_ok


# =========================================================
# TP TARGETS
# =========================================================
def pick_tp1_tp2(direction: str, entry: float, atr_5m: float, zones: Dict[str, Any], risk: float) -> Dict[str, Optional[float]]:
    min_tp1 = atr_5m * MIN_TP1_ATR
    min_tp2 = atr_5m * MIN_TP2_ATR

    tp1: Optional[float] = None
    tp2: Optional[float] = None

    if direction == "BUY":
        res_list = zones.get("resistance_zones") or []

        # TP1 = nearest resistance EDGE above entry
        best_dist = None
        for rz in res_list:
            low = safe_float(rz["low"])
            high = safe_float(rz["high"])
            cand = low if low > entry else (high if high > entry else None)
            if cand is None:
                continue
            dist = cand - entry
            if dist < min_tp1:
                continue
            if best_dist is None or dist < best_dist:
                best_dist = dist
                tp1 = cand

        # TP2 = next resistance MID above entry
        best2_dist = None
        for rz in res_list:
            mid = safe_float(rz["mid"])
            if mid <= entry:
                continue
            dist = mid - entry
            if dist < min_tp2:
                continue
            if best2_dist is None or dist < best2_dist:
                best2_dist = dist
                tp2 = mid

        if tp2 is None:
            tp2 = entry + max(min_tp2, risk * RR_FALLBACK_TP2)

    else:
        sup_list = zones.get("support_zones") or []

        best_dist = None
        for sz in sup_list:
            low = safe_float(sz["low"])
            high = safe_float(sz["high"])
            cand = high if high < entry else (low if low < entry else None)
            if cand is None:
                continue
            dist = entry - cand
            if dist < min_tp1:
                continue
            if best_dist is None or dist < best_dist:
                best_dist = dist
                tp1 = cand

        best2_dist = None
        for sz in sup_list:
            mid = safe_float(sz["mid"])
            if mid >= entry:
                continue
            dist = entry - mid
            if dist < min_tp2:
                continue
            if best2_dist is None or dist < best2_dist:
                best2_dist = dist
                tp2 = mid

        if tp2 is None:
            tp2 = entry - max(min_tp2, risk * RR_FALLBACK_TP2)

    return {"tp1": tp1, "tp2": tp2}


def _calc_r_multiple(direction: str, entry: float, sl: float, tp: float) -> float:
    risk = (entry - sl) if direction == "BUY" else (sl - entry)
    reward = (tp - entry) if direction == "BUY" else (entry - tp)
    if risk <= 0:
        return 0.0
    return reward / risk


# =========================================================
# CONFIDENCE (includes alignment score)
# =========================================================
def confidence_score(
    align_score: float,
    zone_touches: int,
    rej_ok: bool,
    bos_ok: bool,
    tp1: Optional[float],
    entry: float,
    atr_5m: float,
) -> int:
    pts = 0.0

    # alignment contributes up to 35
    pts += max(0.0, min(35.0, float(align_score) * 35.0))

    # zone touches up to 20
    if zone_touches >= 4:
        pts += 20
    elif zone_touches == 3:
        pts += 16
    elif zone_touches == 2:
        pts += 12
    else:
        pts += 6

    # rejection up to 20
    pts += 20 if rej_ok else 0

    # BOS up to 15
    pts += 15 if bos_ok else 6

    # TP1 meaningful up to 5
    if tp1 is not None:
        dist = abs(tp1 - entry)
        pts += 5 if dist >= atr_5m * MIN_TP1_ATR else 2

    return int(max(0, min(95, round(pts))))


# =========================================================
# BUILD TRADE
# =========================================================
def build_trade(
    candles_5m: List[Dict[str, float]],
    direction: str,
    zones: Dict[str, Any],
    atr_5m: float,
    align_score: float,
) -> Dict[str, Any]:
    last = candles_5m[-1]
    entry = safe_float(last["close"])
    buf = atr_5m * SL_BUFFER_ATR

    sup = zones.get("support_zone")
    res = zones.get("resistance_zone")

    if direction == "BUY":
        sl = (safe_float(sup["low"]) - buf) if sup else (recent_swing_low(candles_5m, 20) - buf)
        risk = entry - sl
        if risk <= 0 or risk > atr_5m * MAX_SL_ATR:
            return {"direction": "HOLD", "confidence": 0, "reason": "SL invalid/too wide"}
    else:
        sl = (safe_float(res["high"]) + buf) if res else (recent_swing_high(candles_5m, 20) + buf)
        risk = sl - entry
        if risk <= 0 or risk > atr_5m * MAX_SL_ATR:
            return {"direction": "HOLD", "confidence": 0, "reason": "SL invalid/too wide"}

    tps = pick_tp1_tp2(direction, entry, atr_5m, zones, risk=risk)
    tp1 = tps["tp1"]
    tp2 = tps["tp2"]

    if tp2 is None:
        return {"direction": "HOLD", "confidence": 0, "reason": "No TP target"}

    rej_zone = sup if direction == "BUY" else res
    rej_ok = rejection_ok(last, direction, rej_zone) if rej_zone else False
    bos_ok = bos_confirm(candles_5m, direction, BOS_LOOKBACK)
    touches = int(sup["touches"]) if (direction == "BUY" and sup) else int(res["touches"]) if (direction == "SELL" and res) else 0

    conf = confidence_score(
        align_score=align_score,
        zone_touches=touches,
        rej_ok=rej_ok,
        bos_ok=bos_ok,
        tp1=tp1,
        entry=entry,
        atr_5m=atr_5m,
    )

    return {
        "direction": direction,
        "confidence": conf,
        "entry": round(entry, 5),
        "sl": round(float(sl), 5),
        "tp": round(float(tp2), 5),  # UI compatibility
        "tp1": round(float(tp1), 5) if tp1 is not None else None,
        "tp2": round(float(tp2), 5),
        "r_multiple": round(_calc_r_multiple(direction, entry, float(sl), float(tp2)), 3),
        "reason": "Weighted alignment + Regime ok + Zone + Rejection + Targets(TP1/TP2)",
        "meta": {
            "atr_5m": round(atr_5m, 6),
            "align_score": round(float(align_score), 3),
            "support_touches": int(sup["touches"]) if sup else 0,
            "resistance_touches": int(res["touches"]) if res else 0,
            "rejection_ok": rej_ok,
            "bos_ok": bos_ok,
        },
        "plan": {
            "tp1": round(float(tp1), 5) if tp1 is not None else None,
            "tp2": round(float(tp2), 5),
            "partial_tp_percent_at_tp1": PARTIAL_TP_PERCENT,
            "move_be_after_tp1": True,
            "be_buffer_r": BE_BUFFER_R,
        },
    }


# =========================================================
# TRADE MANAGEMENT
# =========================================================
def hit_level(direction: str, last_high: float, last_low: float, level: float, kind: str) -> bool:
    if direction == "BUY":
        return last_high >= level if kind in ("TP1", "TP2", "TP") else last_low <= level
    return last_low <= level if kind in ("TP1", "TP2", "TP") else last_high >= level


def trail_stop_suggestion(candles_5m: List[Dict[str, float]], direction: str, atr_value: float) -> float:
    data = candles_5m[-TRAIL_LOOKBACK:] if len(candles_5m) >= TRAIL_LOOKBACK else candles_5m
    buf = atr_value * TRAIL_BUFFER_ATR
    if direction == "BUY":
        structure = min(safe_float(c["low"]) for c in data)
        return structure - buf
    structure = max(safe_float(c["high"]) for c in data)
    return structure + buf


# =========================================================
# LOCK SIGNAL HELPER
# =========================================================
def _open_locked_signal(
    symbol: str,
    timeframe: str,
    base_reason: str,
    bias_info: Dict[str, Any],
    reg_reason: str,
    trade_like: Dict[str, Any],
) -> Dict[str, Any]:
    trade_id = str(uuid.uuid4())
    stored = dict(trade_like)
    stored.update({
        "trade_id": trade_id,
        "opened_at": _now_ts(),
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "OPEN",
        "tp1_hit": False,
        "mode": "LOCKED_SIGNAL",
        "entry_type": "LOCKED_SIGNAL",
        "reason": f"{base_reason}; LOCKED SIGNAL",
        "meta": {**(stored.get("meta") or {}), "regime": reg_reason, "bias": bias_info},
    })
    ACTIVE_TRADES[_trade_key(symbol, timeframe)] = stored
    RISK_STATE["signals_today"][symbol] = int(RISK_STATE["signals_today"].get(symbol, 0)) + 1
    _push_history(dict(stored))
    return stored


# =========================================================
# PERFORMANCE + HISTORY
# =========================================================
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
        "review_ready": total >= PERFORMANCE_REVIEW_N,
        "review_target": PERFORMANCE_REVIEW_N,
    }


@router.get("/history")
async def history(symbol: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    items = TRADE_HISTORY
    if symbol:
        symbol = symbol.strip()
        items = [t for t in items if t.get("symbol") == symbol]
    return {"count": len(items[-limit:]), "items": items[-limit:]}


@router.get("/performance")
async def performance(last_n: int = PERFORMANCE_REVIEW_N) -> Dict[str, Any]:
    return _performance(last_n)


# =========================================================
# ROUTES
# =========================================================
@router.post("/scan")
async def scan_markets(payload: ScanRequest) -> Dict[str, Any]:
    _reset_daily_if_needed()
    app_id = os.getenv("DERIV_APP_ID", "1089")

    rows: List[Dict[str, Any]] = []

    for sym in payload.symbols:
        key = _trade_key(sym, ENTRY_TF)

        candles_5m = await fetch_deriv_candles(app_id=app_id, symbol=sym, timeframe=ENTRY_TF, count=260)
        candles_30m = await fetch_deriv_candles(app_id=app_id, symbol=sym, timeframe="30m", count=220)
        candles_1h = await fetch_deriv_candles(app_id=app_id, symbol=sym, timeframe="1h", count=220)
        candles_4h = await fetch_deriv_candles(app_id=app_id, symbol=sym, timeframe="4h", count=220)

        if not candles_5m or not candles_30m or not candles_1h or not candles_4h:
            rows.append({"symbol": sym, "score_1_10": 1, "direction": "HOLD", "confidence": 0, "reason": "no_data"})
            continue

        a5 = float(atr_last(candles_5m, ATR_PERIOD) or 0.0)
        zones = strongest_zones(candles_5m, atr_value=a5)

        reg = regime_ok(candles_5m)
        bias = weighted_alignment({"30m": candles_30m, "1h": candles_1h, "4h": candles_4h})

        direction = bias.get("direction", "HOLD")
        base_reason = bias.get("reason", "")

        if not reg["ok"]:
            direction = "HOLD"
            base_reason = reg["reason"]

        row: Dict[str, Any] = {
            "symbol": sym,
            "score_1_10": 2,
            "direction": "HOLD",
            "confidence": 0,
            "reason": base_reason,
            "active_trade": key in ACTIVE_TRADES,
            "pending": key in PENDING_SETUPS,
            "entry": None,
            "sl": None,
            "tp": None,
            "tp1": None,
            "tp2": None,
            "entry_type": "NONE",
            "bias": direction,
            "bias_score": bias.get("score", 0.0),
            "bias_details": bias.get("details", {}),
        }

        if key in ACTIVE_TRADES:
            t = ACTIVE_TRADES[key]
            row.update({
                "score_1_10": 10,
                "direction": t.get("direction", "HOLD"),
                "confidence": int(t.get("confidence", 80)),
                "reason": "ACTIVE TRADE",
                "entry": t.get("entry"),
                "sl": t.get("sl"),
                "tp": t.get("tp"),
                "tp1": t.get("tp1"),
                "tp2": t.get("tp2"),
                "entry_type": t.get("entry_type", t.get("mode", "ACTIVE")),
            })
        elif key in PENDING_SETUPS:
            row.update({
                "score_1_10": 8,
                "direction": PENDING_SETUPS[key].get("direction", "HOLD"),
                "confidence": 65,
                "reason": "Pending: BOS done, waiting retest+confirm",
                "entry_type": "SNIPER_PENDING",
            })
        else:
            if direction in ("BUY", "SELL"):
                t = build_trade(candles_5m, direction, zones, a5, float(bias.get("score") or 0.0))
                conf = int(t.get("confidence", 0))
                row.update({
                    "direction": direction,
                    "confidence": conf,
                    "entry": t.get("entry"),
                    "sl": t.get("sl"),
                    "tp": t.get("tp"),
                    "tp1": t.get("tp1"),
                    "tp2": t.get("tp2"),
                    "reason": base_reason,
                    "entry_type": "SETUP",
                })
                score10 = 3
                if conf >= 60:
                    score10 = 5
                if conf >= 70:
                    score10 = 6
                if bos_confirm(candles_5m, direction, BOS_LOOKBACK):
                    score10 = max(score10, 7)
                if reg["ok"] and conf >= 75:
                    score10 = max(score10, 8)
                row["score_1_10"] = score10

        sup = zones.get("support_zone")
        res = zones.get("resistance_zone")
        row["support_touches"] = int(sup["touches"]) if sup else 0
        row["resistance_touches"] = int(res["touches"]) if res else 0

        rows.append(row)

    rows.sort(key=lambda x: (x.get("score_1_10", 0), x.get("confidence", 0)), reverse=True)

    return {
        "timeframe": ENTRY_TF,
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
        "ranked": rows,
        "top": rows[0] if rows else None,
    }


@router.post("/analyze")
async def analyze_market(data: AnalyzeRequest) -> Dict[str, Any]:
    _reset_daily_if_needed()

    symbol = data.symbol.strip()
    timeframe = ENTRY_TF
    key = _trade_key(symbol, timeframe)

    app_id = os.getenv("DERIV_APP_ID", "1089")

    candles_5m = await fetch_deriv_candles(app_id=app_id, symbol=symbol, timeframe=timeframe, count=260)
    candles_30m = await fetch_deriv_candles(app_id=app_id, symbol=symbol, timeframe="30m", count=220)
    candles_1h = await fetch_deriv_candles(app_id=app_id, symbol=symbol, timeframe="1h", count=220)
    candles_4h = await fetch_deriv_candles(app_id=app_id, symbol=symbol, timeframe="4h", count=220)

    if not candles_5m or not candles_30m or not candles_1h or not candles_4h:
        return {"error": "No candles returned", "symbol": symbol, "timeframe": timeframe}

    last = candles_5m[-1]
    last_high = safe_float(last["high"])
    last_low = safe_float(last["low"])
    last_close = safe_float(last["close"])

    a5 = float(atr_last(candles_5m, ATR_PERIOD) or 0.0)
    zones = strongest_zones(candles_5m, atr_value=a5)

    levels = {
        "support_zone": zones.get("support_zone"),
        "resistance_zone": zones.get("resistance_zone"),
        "supports": [zones["support_zone"]["mid"]] if zones.get("support_zone") else [],
        "resistances": [zones["resistance_zone"]["mid"]] if zones.get("resistance_zone") else [],
    }

    bias = weighted_alignment({"30m": candles_30m, "1h": candles_1h, "4h": candles_4h})
    bias_dir = bias.get("direction", "HOLD")
    bias_score = float(bias.get("score") or 0.0)

    # -------------------------------------------------
    # ACTIVE TRADE MANAGEMENT
    # -------------------------------------------------
    if key in ACTIVE_TRADES:
        trade = ACTIVE_TRADES[key]
        direction = trade["direction"]
        sl = safe_float(trade["sl"])
        tp1 = trade.get("tp1")
        tp2 = safe_float(trade.get("tp2") or trade.get("tp"))
        entry = safe_float(trade["entry"])

        risk = abs(entry - sl) if abs(entry - sl) > 0 else 1e-9
        actions = []

        # TP1 milestone
        if tp1 is not None and not trade.get("tp1_hit", False):
            tp1v = safe_float(tp1)
            if hit_level(direction, last_high, last_low, tp1v, "TP1"):
                trade["tp1_hit"] = True
                be_to = entry + (risk * BE_BUFFER_R) if direction == "BUY" else entry - (risk * BE_BUFFER_R)

                actions.append({"type": "TP1_HIT", "why": "TP1 hit → take partial + protect trade"})
                actions.append({"type": "PARTIAL_TP", "percent": PARTIAL_TP_PERCENT, "why": "Take partial at TP1"})
                actions.append({"type": "MOVE_SL", "to": round(be_to, 5), "why": "Move SL to protected BE (after TP1)"})

        trail_price = trail_stop_suggestion(candles_5m, direction, a5)
        actions.append({"type": "TRAIL_SUGGESTION", "to": round(trail_price, 5), "why": "Trail behind structure (optional)"})

        # Close at SL or TP2
        if direction == "BUY":
            outcome = "SL" if last_low <= sl else "TP2" if last_high >= tp2 else None
        else:
            outcome = "SL" if last_high >= sl else "TP2" if last_low <= tp2 else None

        if outcome is None:
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "price": round(last_close, 5),
                "candles": candles_5m,
                "levels": levels,
                "active": True,
                "signal": {**trade, "entry_type": trade.get("entry_type", "ACTIVE_MANAGEMENT")},
                "entry_type": trade.get("entry_type", "ACTIVE_MANAGEMENT"),
                "bias": bias_dir,
                "bias_score": bias_score,
                "bias_details": bias.get("details", {}),
                "actions": actions,
                "daily_R": RISK_STATE["daily_R"],
                "active_total": _active_total(),
            }

        closed = dict(trade)
        closed["closed_at"] = _now_ts()
        closed["outcome"] = outcome
        closed["status"] = "CLOSED"

        tid = trade.get("trade_id")
        if tid:
            idx = _find_history_index(tid)
            if idx is not None:
                TRADE_HISTORY[idx] = {**TRADE_HISTORY[idx], **closed}

        if outcome == "SL":
            RISK_STATE["daily_R"] += -1.0
            RISK_STATE["cooldown_until"][symbol] = int(time.time() + COOLDOWN_MIN_AFTER_LOSS * 60)
        else:
            RISK_STATE["daily_R"] += float(trade.get("r_multiple") or 1.0)

        ACTIVE_TRADES.pop(key, None)

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "closed_trade": closed,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": f"Trade closed by {outcome}"},
            "actions": actions,
        }

    # -------------------------------------------------
    # RISK GATES
    # -------------------------------------------------
    if RISK_STATE["daily_R"] <= DAILY_LOSS_LIMIT_R:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Daily loss limit hit — paused"},
            "actions": [],
        }

    if _active_total() >= MAX_ACTIVE_TOTAL:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Max active trades reached"},
            "actions": [],
        }

    cooldown_until = int(RISK_STATE.get("cooldown_until", {}).get(symbol, 0))
    if time.time() < cooldown_until:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Cooldown after loss"},
            "cooldown_seconds_left": int(cooldown_until - time.time()),
            "actions": [],
        }

    count_today = int(RISK_STATE["signals_today"].get(symbol, 0))
    if count_today >= MAX_SIGNALS_PER_DAY_PER_SYMBOL:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Max signals/day reached"},
            "actions": [],
        }

    # -------------------------------------------------
    # REGIME FILTER + ALIGNMENT FILTER
    # -------------------------------------------------
    reg = regime_ok(candles_5m)
    if not reg["ok"]:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": reg["reason"], "meta": reg},
            "actions": [],
        }

    if bias_dir not in ("BUY", "SELL"):
        PENDING_SETUPS.pop(key, None)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": bias.get("reason", "No bias"), "meta": {"bias": bias}},
            "actions": [],
        }

    # Must have zone on the correct side
    if bias_dir == "BUY" and not zones.get("support_zone"):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "BUY bias but no support zone"},
            "actions": [],
        }
    if bias_dir == "SELL" and not zones.get("resistance_zone"):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "SELL bias but no resistance zone"},
            "actions": [],
        }

    # -------------------------------------------------
    # FAST ENTRY (strict)
    # -------------------------------------------------
    if ENABLE_FAST_ENTRY and key not in PENDING_SETUPS:
        t = build_trade(candles_5m, bias_dir, zones, a5, bias_score)
        rej_zone = zones.get("support_zone") if bias_dir == "BUY" else zones.get("resistance_zone")
        rej_ok = rejection_ok(last, bias_dir, rej_zone) if rej_zone else False

        if (
            t.get("direction") in ("BUY", "SELL")
            and int(t.get("confidence", 0)) >= FAST_ENTRY_MIN_CONF
            and rej_ok
            and t.get("tp1") is not None
        ):
            trade_id = str(uuid.uuid4())
            stored = dict(t)
            stored.update({
                "trade_id": trade_id,
                "opened_at": _now_ts(),
                "symbol": symbol,
                "timeframe": timeframe,
                "status": "OPEN",
                "tp1_hit": False,
                "mode": "FAST_ENTRY",
                "entry_type": "FAST_ENTRY",
                "reason": f"{bias.get('reason','bias ok')}; FAST ENTRY (strict)",
                "meta": {**(stored.get("meta") or {}), "regime": reg.get("reason"), "bias": bias},
            })

            ACTIVE_TRADES[key] = stored
            RISK_STATE["signals_today"][symbol] = int(RISK_STATE["signals_today"].get(symbol, 0)) + 1
            _push_history(dict(stored))

            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "price": round(last_close, 5),
                "candles": candles_5m,
                "levels": levels,
                "active": True,
                "signal": stored,
                "entry_type": "FAST_ENTRY",
                "bias": bias_dir,
                "bias_score": bias_score,
                "bias_details": bias.get("details", {}),
                "actions": [],
                "daily_R": RISK_STATE["daily_R"],
                "active_total": _active_total(),
            }

    # -------------------------------------------------
    # SIGNAL LOCK (keeps strong setup stable on screen)
    # -------------------------------------------------
    if ENABLE_SIGNAL_LOCK and key not in ACTIVE_TRADES:
        t = build_trade(candles_5m, bias_dir, zones, a5, bias_score)
        if t.get("direction") in ("BUY", "SELL"):
            rej_zone = zones.get("support_zone") if bias_dir == "BUY" else zones.get("resistance_zone")
            rej_ok = rejection_ok(last, bias_dir, rej_zone) if rej_zone else False

            if int(t.get("confidence", 0)) >= MIN_LOCK_CONF and rej_ok and t.get("tp1") is not None:
                stored = _open_locked_signal(
                    symbol=symbol,
                    timeframe=timeframe,
                    base_reason=bias.get("reason", "bias ok"),
                    bias_info=bias,
                    reg_reason=reg.get("reason", "regime_ok"),
                    trade_like=t,
                )
                return {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "price": round(last_close, 5),
                    "candles": candles_5m,
                    "levels": levels,
                    "active": True,
                    "signal": stored,
                    "entry_type": "LOCKED_SIGNAL",
                    "bias": bias_dir,
                    "bias_score": bias_score,
                    "bias_details": bias.get("details", {}),
                    "actions": [],
                    "daily_R": RISK_STATE["daily_R"],
                    "active_total": _active_total(),
                }

    # -------------------------------------------------
    # SNIPER PATH: BOS -> RETEST -> REJECTION -> OPEN
    # -------------------------------------------------
    if key not in PENDING_SETUPS:
        if bos_confirm(candles_5m, bias_dir, BOS_LOOKBACK):
            lvl = bos_level(candles_5m, bias_dir, BOS_LOOKBACK)
            if lvl is not None:
                PENDING_SETUPS[key] = {
                    "direction": bias_dir,
                    "bos_level": float(lvl),
                    "created_idx": len(candles_5m) - 1,
                    "bias": bias,
                }
                return {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "price": round(last_close, 5),
                    "candles": candles_5m,
                    "levels": levels,
                    "active": False,
                    "entry_type": "SNIPER_PENDING",
                    "bias": bias_dir,
                    "bias_score": bias_score,
                    "bias_details": bias.get("details", {}),
                    "signal": {"direction": "HOLD", "confidence": 0, "reason": f"{bias.get('reason','')}; BOS confirmed → waiting retest"},
                    "pending": PENDING_SETUPS[key],
                    "actions": [],
                    "daily_R": RISK_STATE["daily_R"],
                    "active_total": _active_total(),
                }

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": f"{bias.get('reason','')}; waiting BOS"},
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
        }

    pending = PENDING_SETUPS[key]
    p_dir = pending["direction"]
    bos_lvl = float(pending["bos_level"])
    created_idx = int(pending["created_idx"])
    tol = float(a5) * RETEST_TOL_ATR

    if (len(candles_5m) - 1) - created_idx > RETEST_MAX_CANDLES:
        PENDING_SETUPS.pop(key, None)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Pending expired (no retest)"},
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
        }

    if not in_retest_zone(last, bos_lvl, tol):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "SNIPER_PENDING",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": f"Pending → waiting retest near BOS {round(bos_lvl,5)}"},
            "pending": pending,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
        }

    z = zones.get("support_zone") if p_dir == "BUY" else zones.get("resistance_zone")
    if not z or not price_overlaps_zone(last, z):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "SNIPER_PENDING",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Retest not inside correct zone"},
            "pending": pending,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
        }

    if not rejection_ok(last, p_dir, z):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "SNIPER_PENDING",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Waiting rejection candle in zone"},
            "pending": pending,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
        }

    t = build_trade(candles_5m, p_dir, zones, a5, bias_score)
    if t.get("direction") not in ("BUY", "SELL") or t.get("tp1") is None:
        PENDING_SETUPS.pop(key, None)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "entry_type": "NONE",
            "bias": bias_dir,
            "bias_score": bias_score,
            "bias_details": bias.get("details", {}),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": t.get("reason", "Trade rejected")},
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
        }

    trade_id = str(uuid.uuid4())
    stored = dict(t)
    stored.update({
        "trade_id": trade_id,
        "opened_at": _now_ts(),
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "OPEN",
        "tp1_hit": False,
        "bos_level": round(bos_lvl, 5),
        "mode": "SNIPER",
        "entry_type": "SNIPER",
        "reason": f"{bias.get('reason','bias ok')}; SNIPER (BOS→Retest→Rejection)",
        "meta": {**(stored.get("meta") or {}), "bias": bias, "regime": reg.get("reason")},
    })

    ACTIVE_TRADES[key] = stored
    PENDING_SETUPS.pop(key, None)
    RISK_STATE["signals_today"][symbol] = int(RISK_STATE["signals_today"].get(symbol, 0)) + 1
    _push_history(dict(stored))

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "price": round(last_close, 5),
        "candles": candles_5m,
        "levels": levels,
        "active": True,
        "signal": stored,
        "entry_type": "SNIPER",
        "bias": bias_dir,
        "bias_score": bias_score,
        "bias_details": bias.get("details", {}),
        "actions": [],
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
    }