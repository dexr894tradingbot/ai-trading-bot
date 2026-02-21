from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

# You already have this in your project (based on your screenshots)
# It should return a list of candles: [{"open":..,"high":..,"low":..,"close":..,"time":..}, ...]
from utils.deriv import fetch_deriv_candles

router = APIRouter()

# =========================================================
# CONFIG (tune these later)
# =========================================================
ENTRY_TF = "5m"
BIAS_TF = "1h"

EMA_FAST = 20
EMA_SLOW = 50

ATR_PERIOD = 14
RSI_PERIOD = 14

# Trend strength filter (avoid sideways)
USE_CHOP_FILTER = True
TREND_STRENGTH_MIN = 0.60  # higher = stricter

# Pullback tolerance
PULLBACK_TOL_ATR = 0.50

# BOS / Retest / Confirm
BOS_LOOKBACK = 12
RETEST_TOL_ATR = 0.35
RETEST_MAX_CANDLES = 18

# Confirmation candle strength
MIN_BODY_TO_RANGE = 0.45

# Zones
ZONE_LOOKBACK = 160
ZONE_ATR_MULT = 0.25        # zone thickness = ATR * 0.25
MIN_ZONE_TOUCHES = 3

# SL/TP
SL_BUFFER_ATR = 0.20        # extra buffer behind zone
RR_FALLBACK = 5.0           # if no opposing zone exists, use RR fallback
MAX_SL_ATR = 2.50           # block trades if SL too wide vs ATR

# Trade management (signals to show on UI)
MOVE_BE_AT_R = 1.0
PARTIAL_TP_AT_R = 2.0
PARTIAL_TP_PERCENT = 0.5
TRAIL_LOOKBACK = 12
TRAIL_BUFFER_ATR = 0.20

# Risk gates
MAX_ACTIVE_TOTAL = 2
DAILY_LOSS_LIMIT_R = -3.0
COOLDOWN_MIN_AFTER_LOSS = 20
MAX_SIGNALS_PER_DAY_PER_SYMBOL = 5

# =========================================================
# STATE (in-memory)
# =========================================================
ACTIVE_TRADES: Dict[str, Dict[str, Any]] = {}   # key: SYMBOL:TF
PENDING_SETUPS: Dict[str, Dict[str, Any]] = {}  # BOS done -> waiting retest+confirm
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
    timeframe: str = "5m"   # front-end can send, but we enforce ENTRY_TF

class ScanRequest(BaseModel):
    timeframe: str = "5m"
    symbols: List[str] = ["R_10", "R_25", "R_50", "R_75", "R_100"]

# =========================================================
# UTILS
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

def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def atr(candles: List[Dict[str, float]], period: int = ATR_PERIOD) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs: List[float] = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    window = trs[-period:]
    return sum(window) / len(window) if window else None

def rsi(values: List[float], period: int = RSI_PERIOD) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def is_bullish(c: Dict[str, float]) -> bool:
    return float(c["close"]) > float(c["open"])

def is_bearish(c: Dict[str, float]) -> bool:
    return float(c["close"]) < float(c["open"])

def candle_body_ratio(c: Dict[str, float]) -> float:
    o = float(c["open"])
    cl = float(c["close"])
    h = float(c["high"])
    l = float(c["low"])
    rng = max(1e-9, h - l)
    body = abs(cl - o)
    return body / rng

def recent_swing_low(candles: List[Dict[str, float]], lookback: int = 20) -> float:
    data = candles[-lookback:]
    return min(float(c["low"]) for c in data)

def recent_swing_high(candles: List[Dict[str, float]], lookback: int = 20) -> float:
    data = candles[-lookback:]
    return max(float(c["high"]) for c in data)

# =========================================================
# ZONE BUILDING (3+ touches, ATR sized)
# =========================================================
def build_pivots(
    candles: List[Dict[str, float]],
    side: str,  # "support" or "resistance"
    pivot_left: int = 2,
    pivot_right: int = 2,
) -> List[float]:
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]

    pivots: List[float] = []
    for i in range(pivot_left, len(candles) - pivot_right):
        if side == "resistance":
            window = highs[i - pivot_left:i + pivot_right + 1]
            if highs[i] == max(window):
                pivots.append(highs[i])
        else:
            window = lows[i - pivot_left:i + pivot_right + 1]
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
        h = float(c["high"])
        l = float(c["low"])
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

    # keep only strong zones
    sup_zones = [z for z in sup_zones if z["touches"] >= MIN_ZONE_TOUCHES]
    res_zones = [z for z in res_zones if z["touches"] >= MIN_ZONE_TOUCHES]

    last_close = float(data[-1]["close"])

    # strongest = most touches, then closest to current price
    sup_zones.sort(key=lambda z: (-z["touches"], abs(last_close - z["mid"])))
    res_zones.sort(key=lambda z: (-z["touches"], abs(last_close - z["mid"])))

    return {
        "support_zone": sup_zones[0] if sup_zones else None,
        "resistance_zone": res_zones[0] if res_zones else None,
        "zone_width": zone_width,
    }

def price_overlaps_zone(candle: Dict[str, float], zone: Dict[str, float]) -> bool:
    h = float(candle["high"])
    l = float(candle["low"])
    return h >= float(zone["low"]) and l <= float(zone["high"])

# =========================================================
# BOS / RETEST / CONFIRM
# =========================================================
def bos_level(candles: List[Dict[str, float]], direction: str, lookback: int = BOS_LOOKBACK) -> Optional[float]:
    if len(candles) < lookback + 2:
        return None
    recent = candles[-(lookback + 1):-1]
    if direction == "BUY":
        return max(float(c["high"]) for c in recent)
    if direction == "SELL":
        return min(float(c["low"]) for c in recent)
    return None

def bos_confirm(candles: List[Dict[str, float]], direction: str, lookback: int = BOS_LOOKBACK) -> bool:
    lvl = bos_level(candles, direction, lookback)
    if lvl is None:
        return False
    last_close = float(candles[-1]["close"])
    return (last_close > lvl) if direction == "BUY" else (last_close < lvl)

def in_retest_zone(c: Dict[str, float], level: float, tol: float) -> bool:
    h = float(c["high"])
    l = float(c["low"])
    return (l <= level + tol) and (h >= level - tol)

def confirm_candle(c: Dict[str, float], direction: str, level: float) -> bool:
    if direction == "BUY":
        if not is_bullish(c):
            return False
        if float(c["close"]) <= level:
            return False
    else:
        if not is_bearish(c):
            return False
        if float(c["close"]) >= level:
            return False
    return candle_body_ratio(c) >= MIN_BODY_TO_RANGE

# =========================================================
# TRADE HELPERS
# =========================================================
def hit_trade_levels(direction: str, last_high: float, last_low: float, sl: float, tp: float) -> Optional[str]:
    if direction == "BUY":
        if last_low <= sl:
            return "SL"
        if last_high >= tp:
            return "TP"
    else:
        if last_high >= sl:
            return "SL"
        if last_low <= tp:
            return "TP"
    return None

def trail_stop_suggestion(candles_5m: List[Dict[str, float]], direction: str, atr_value: float) -> float:
    data = candles_5m[-TRAIL_LOOKBACK:] if len(candles_5m) >= TRAIL_LOOKBACK else candles_5m
    buf = atr_value * TRAIL_BUFFER_ATR
    if direction == "BUY":
        structure = min(float(c["low"]) for c in data)
        return structure - buf
    structure = max(float(c["high"]) for c in data)
    return structure + buf

# =========================================================
# STRATEGY CORE (Trend-only + Pullback)
# =========================================================
def base_direction(candles_5m: List[Dict[str, float]], candles_1h: List[Dict[str, float]]) -> Dict[str, Any]:
    if len(candles_5m) < 220 or len(candles_1h) < 120:
        return {"direction": "HOLD", "reason": "Not enough data"}

    closes_5m = [float(c["close"]) for c in candles_5m]
    closes_1h = [float(c["close"]) for c in candles_1h]

    ema20_1h = ema(closes_1h[-80:], EMA_FAST)
    ema50_1h = ema(closes_1h[-160:], EMA_SLOW)

    ema20_5m = ema(closes_5m[-80:], EMA_FAST)
    ema50_5m = ema(closes_5m[-160:], EMA_SLOW)

    a5 = atr(candles_5m, ATR_PERIOD)
    a1 = atr(candles_1h, ATR_PERIOD)
    r5 = rsi(closes_5m, RSI_PERIOD)

    if None in (ema20_1h, ema50_1h, ema20_5m, ema50_5m, a5, a1, r5):
        return {"direction": "HOLD", "reason": "Indicators not ready"}

    ema20_1h = float(ema20_1h); ema50_1h = float(ema50_1h)
    ema20_5m = float(ema20_5m); ema50_5m = float(ema50_5m)
    a5 = float(a5); a1 = float(a1); r5 = float(r5)

    last = candles_5m[-1]
    close = float(last["close"])

    # Trend bias
    bias = "HOLD"
    if ema20_1h > ema50_1h:
        bias = "BUY"
    elif ema20_1h < ema50_1h:
        bias = "SELL"

    if bias == "HOLD":
        return {"direction": "HOLD", "reason": "No 1H trend bias"}

    # Chop filter
    trend_strength = abs(ema20_1h - ema50_1h) / max(1e-9, a1)
    if USE_CHOP_FILTER and trend_strength < TREND_STRENGTH_MIN:
        return {"direction": "HOLD", "reason": f"Choppy (trend_strength {trend_strength:.2f})"}

    # Pullback near EMA20 on 5m
    pull_tol = a5 * PULLBACK_TOL_ATR
    pullback_ok = abs(close - ema20_5m) <= pull_tol

    # Simple momentum confirm candle
    prev = candles_5m[-2]
    if bias == "BUY":
        confirm = is_bullish(last) and close > float(prev["close"]) and close > ema20_5m
    else:
        confirm = is_bearish(last) and close < float(prev["close"]) and close < ema20_5m

    if not (pullback_ok and confirm):
        return {"direction": "HOLD", "reason": "No pullback+confirm yet"}

    return {
        "direction": bias,
        "reason": f"{bias} bias; pullback ok; confirm ok",
        "meta": {"atr_5m": round(a5, 6), "atr_1h": round(a1, 6), "rsi_5m": round(r5, 2), "trend_strength": round(trend_strength, 3)}
    }

# =========================================================
# BUILD TRADE (zones tied to SL + TP)
# =========================================================
def build_trade(
    candles_5m: List[Dict[str, float]],
    direction: str,
    zones: Dict[str, Any],
    atr_5m: float,
) -> Dict[str, Any]:
    last = candles_5m[-1]
    entry = float(last["close"])
    buf = atr_5m * SL_BUFFER_ATR

    sup = zones.get("support_zone")
    res = zones.get("resistance_zone")

    # SL: behind the zone
    if direction == "BUY":
        if not sup:
            # fallback swing
            base = recent_swing_low(candles_5m, 20)
            sl = base - buf
        else:
            sl = float(sup["low"]) - buf
        risk = entry - sl
        if risk <= 0 or risk > atr_5m * MAX_SL_ATR:
            return {"direction": "HOLD", "confidence": 0, "reason": "SL invalid/too wide"}
        # TP: prefer next resistance zone mid (real trader target)
        if res and float(res["mid"]) > entry:
            tp = float(res["mid"])
        else:
            tp = entry + risk * RR_FALLBACK
    else:
        if not res:
            base = recent_swing_high(candles_5m, 20)
            sl = base + buf
        else:
            sl = float(res["high"]) + buf
        risk = sl - entry
        if risk <= 0 or risk > atr_5m * MAX_SL_ATR:
            return {"direction": "HOLD", "confidence": 0, "reason": "SL invalid/too wide"}
        if sup and float(sup["mid"]) < entry:
            tp = float(sup["mid"])
        else:
            tp = entry - risk * RR_FALLBACK

    # Confidence: you can tune, but because it’s full stack (trend + BOS + zone + confirm) keep it high
    confidence = 80

    return {
        "direction": direction,
        "confidence": confidence,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp": round(tp, 5),
        "reason": "Trend + Pullback + BOS + Retest + Confirm + Zone",
        "meta": {
            "atr_5m": round(atr_5m, 6),
            "zone_width": round(float(zones.get("zone_width") or 0.0), 6),
            "support_touches": int(sup["touches"]) if sup else 0,
            "resistance_touches": int(res["touches"]) if res else 0,
        },
        "plan": {
            "move_be_at_r": MOVE_BE_AT_R,
            "partial_tp_at_r": PARTIAL_TP_AT_R,
            "partial_tp_percent": PARTIAL_TP_PERCENT,
        }
    }

# =========================================================
# ROUTES
# =========================================================
@router.post("/scan")
async def scan_markets(payload: ScanRequest) -> Dict[str, Any]:
    _reset_daily_if_needed()
    app_id = os.getenv("DERIV_APP_ID", "1089")

    rows = []
    for sym in payload.symbols:
        key = f"{sym}:{ENTRY_TF}"

        candles_5m = await fetch_deriv_candles(app_id=app_id, symbol=sym, timeframe=ENTRY_TF, count=260)
        candles_1h = await fetch_deriv_candles(app_id=app_id, symbol=sym, timeframe=BIAS_TF, count=220)

        if not candles_5m or not candles_1h:
            rows.append({"symbol": sym, "score_1_10": 1, "direction": "HOLD", "confidence": 0, "reason": "no_data"})
            continue

        a5 = float(atr(candles_5m, ATR_PERIOD) or 0.0)
        zones = strongest_zones(candles_5m, atr_value=a5)

        base = base_direction(candles_5m, candles_1h)
        direction = base.get("direction", "HOLD")
        reason = base.get("reason", "")
        conf = 0
        score10 = 2

        if key in ACTIVE_TRADES:
            score10 = 10
            conf = 90
            direction = ACTIVE_TRADES[key]["direction"]
            reason = "ACTIVE TRADE"
        elif key in PENDING_SETUPS:
            score10 = 8
            conf = 70
            reason = "Pending: BOS done, waiting retest+confirm"
        else:
            if direction in ("BUY", "SELL"):
                if zones.get("support_zone") and direction == "BUY":
                    score10 = 6
                    conf = 50
                elif zones.get("resistance_zone") and direction == "SELL":
                    score10 = 6
                    conf = 50
                if bos_confirm(candles_5m, direction, BOS_LOOKBACK):
                    score10 = 7
                    conf = 60
                    reason += "; BOS ready"
            else:
                score10 = 2
                conf = 0

        rows.append({
            "symbol": sym,
            "score_1_10": score10,
            "direction": direction,
            "confidence": conf,
            "reason": reason,
            "active_trade": key in ACTIVE_TRADES,
            "pending": key in PENDING_SETUPS,
            "support_touches": int(zones["support_zone"]["touches"]) if zones.get("support_zone") else 0,
            "resistance_touches": int(zones["resistance_zone"]["touches"]) if zones.get("resistance_zone") else 0,
        })

    rows.sort(key=lambda x: (x["score_1_10"], x["confidence"]), reverse=True)

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
    key = f"{symbol}:{timeframe}"

    app_id = os.getenv("DERIV_APP_ID", "1089")

    candles_5m = await fetch_deriv_candles(app_id=app_id, symbol=symbol, timeframe=timeframe, count=260)
    candles_1h = await fetch_deriv_candles(app_id=app_id, symbol=symbol, timeframe=BIAS_TF, count=220)

    if not candles_5m or not candles_1h:
        return {"error": "No candles returned", "symbol": symbol, "timeframe": timeframe}

    last = candles_5m[-1]
    last_high = float(last["high"])
    last_low = float(last["low"])
    last_close = float(last["close"])

    a5 = float(atr(candles_5m, ATR_PERIOD) or 0.0)
    zones = strongest_zones(candles_5m, atr_value=a5)

    # Levels object for your chart
    levels = {
        "support_zone": zones.get("support_zone"),
        "resistance_zone": zones.get("resistance_zone"),
        "supports": [zones["support_zone"]["mid"]] if zones.get("support_zone") else [],
        "resistances": [zones["resistance_zone"]["mid"]] if zones.get("resistance_zone") else [],
    }

    # -------------------------
    # ACTIVE TRADE MANAGEMENT
    # -------------------------
    if key in ACTIVE_TRADES:
        trade = ACTIVE_TRADES[key]
        outcome = hit_trade_levels(trade["direction"], last_high, last_low, float(trade["sl"]), float(trade["tp"]))

        entry = float(trade["entry"])
        sl = float(trade["sl"])
        direction = trade["direction"]
        risk = abs(entry - sl)

        one_r = entry + risk if direction == "BUY" else entry - risk
        two_r = entry + (risk * 2) if direction == "BUY" else entry - (risk * 2)
        trail_price = trail_stop_suggestion(candles_5m, direction, a5)

        actions = []
        if not trade.get("be_done", False):
            if (direction == "BUY" and last_high >= one_r) or (direction == "SELL" and last_low <= one_r):
                trade["be_done"] = True
                actions.append({"type": "MOVE_SL", "to": round(entry, 5), "why": "Hit 1R → move SL to breakeven"})

        if not trade.get("partial_done", False):
            if (direction == "BUY" and last_high >= two_r) or (direction == "SELL" and last_low <= two_r):
                trade["partial_done"] = True
                actions.append({"type": "PARTIAL_TP", "percent": PARTIAL_TP_PERCENT, "why": "Hit 2R → take partial profit"})

        actions.append({"type": "TRAIL_SUGGESTION", "to": round(trail_price, 5), "why": "Trail SL behind recent structure"})

        if outcome is None:
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "price": round(last_close, 5),
                "candles": candles_5m,
                "levels": levels,
                "active": True,
                "signal": trade,
                "actions": actions,
                "daily_R": RISK_STATE["daily_R"],
                "active_total": _active_total(),
            }

        closed = dict(trade)
        closed["closed_at"] = int(time.time())
        closed["outcome"] = outcome

        if outcome == "SL":
            RISK_STATE["daily_R"] += -1.0
            RISK_STATE["cooldown_until"][symbol] = int(time.time() + COOLDOWN_MIN_AFTER_LOSS * 60)
        else:
            # count TP as +RR_FALLBACK (or you can compute real R)
            RISK_STATE["daily_R"] += RR_FALLBACK

        ACTIVE_TRADES.pop(key, None)

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(last_close, 5),
            "candles": candles_5m,
            "levels": levels,
            "active": False,
            "closed_trade": closed,
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": f"Trade closed by {outcome}"},
            "actions": [],
        }

    # -------------------------
    # RISK GATES (NEW TRADES)
    # -------------------------
    if RISK_STATE["daily_R"] <= DAILY_LOSS_LIMIT_R:
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Daily loss limit hit — paused"},
            "actions": [],
        }

    if _active_total() >= MAX_ACTIVE_TOTAL:
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Max active trades reached"},
            "actions": [],
        }

    cooldown_until = int(RISK_STATE.get("cooldown_until", {}).get(symbol, 0))
    if time.time() < cooldown_until:
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Cooldown after loss"},
            "cooldown_seconds_left": int(cooldown_until - time.time()),
            "actions": [],
        }

    count_today = int(RISK_STATE["signals_today"].get(symbol, 0))
    if count_today >= MAX_SIGNALS_PER_DAY_PER_SYMBOL:
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Max signals/day reached"},
            "actions": [],
        }

    # -------------------------
    # BASE DIRECTION FILTER
    # -------------------------
    base = base_direction(candles_5m, candles_1h)
    direction = base.get("direction", "HOLD")
    meta = base.get("meta", {})
    base_reason = base.get("reason", "")

    if direction == "HOLD":
        # clear stale pending for this symbol
        PENDING_SETUPS.pop(key, None)
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": base_reason},
            "actions": [],
        }

    # Must have the proper zone for direction
    if direction == "BUY" and not zones.get("support_zone"):
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "BUY setup but no strong support zone (3+ touches)"},
            "actions": [],
        }

    if direction == "SELL" and not zones.get("resistance_zone"):
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "SELL setup but no strong resistance zone (3+ touches)"},
            "actions": [],
        }

    # -------------------------
    # BOS -> RETEST -> CONFIRM + ZONE REQUIREMENT
    # -------------------------
    if key not in PENDING_SETUPS:
        # wait for BOS
        if bos_confirm(candles_5m, direction, BOS_LOOKBACK):
            lvl = bos_level(candles_5m, direction, BOS_LOOKBACK)
            if lvl is not None:
                PENDING_SETUPS[key] = {
                    "direction": direction,
                    "bos_level": float(lvl),
                    "created_idx": len(candles_5m) - 1,
                    "meta": meta,
                }
                return {
                    "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
                    "candles": candles_5m, "levels": levels, "active": False,
                    "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
                    "signal": {"direction": "HOLD", "confidence": 0, "reason": f"{base_reason}; BOS confirmed → waiting retest into zone"},
                    "pending": PENDING_SETUPS[key],
                    "actions": [],
                }

        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": f"{base_reason}; waiting BOS"},
            "actions": [],
        }

    pending = PENDING_SETUPS[key]
    p_dir = pending["direction"]
    bos_lvl = float(pending["bos_level"])
    created_idx = int(pending["created_idx"])
    tol = float(a5) * RETEST_TOL_ATR

    # expire pending
    if (len(candles_5m) - 1) - created_idx > RETEST_MAX_CANDLES:
        PENDING_SETUPS.pop(key, None)
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Pending expired (no retest)"},
            "actions": [],
        }

    # retest must happen near BOS level
    if not in_retest_zone(last, bos_lvl, tol):
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": f"Pending → waiting retest near BOS {round(bos_lvl,5)}"},
            "pending": pending,
            "actions": [],
        }

    # retest must overlap correct zone (THIS is your “strong entries” rule)
    if p_dir == "BUY":
        z = zones["support_zone"]
        if not price_overlaps_zone(last, z):
            return {
                "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
                "candles": candles_5m, "levels": levels, "active": False,
                "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
                "signal": {"direction": "HOLD", "confidence": 0, "reason": "Retest happened but not inside strong SUPPORT zone"},
                "pending": pending,
                "actions": [],
            }
    else:
        z = zones["resistance_zone"]
        if not price_overlaps_zone(last, z):
            return {
                "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
                "candles": candles_5m, "levels": levels, "active": False,
                "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
                "signal": {"direction": "HOLD", "confidence": 0, "reason": "Retest happened but not inside strong RESISTANCE zone"},
                "pending": pending,
                "actions": [],
            }

    # confirmation candle at retest
    if not confirm_candle(last, p_dir, bos_lvl):
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Retest in zone → waiting confirmation candle"},
            "pending": pending,
            "actions": [],
        }

    # build final trade
    trade = build_trade(candles_5m, p_dir, zones, a5)
    if trade.get("direction") not in ("BUY", "SELL"):
        PENDING_SETUPS.pop(key, None)
        return {
            "symbol": symbol, "timeframe": timeframe, "price": round(last_close, 5),
            "candles": candles_5m, "levels": levels, "active": False,
            "daily_R": RISK_STATE["daily_R"], "active_total": _active_total(),
            "signal": {"direction": "HOLD", "confidence": 0, "reason": trade.get("reason", "Trade rejected")},
            "actions": [],
        }

    stored = dict(trade)
    stored["opened_at"] = int(time.time())
    stored["symbol"] = symbol
    stored["timeframe"] = timeframe
    stored["be_done"] = False
    stored["partial_done"] = False
    stored["bos_level"] = round(bos_lvl, 5)

    ACTIVE_TRADES[key] = stored
    PENDING_SETUPS.pop(key, None)
    RISK_STATE["signals_today"][symbol] = int(RISK_STATE["signals_today"].get(symbol, 0)) + 1

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "price": round(last_close, 5),
        "candles": candles_5m,
        "levels": levels,
        "active": True,
        "signal": stored,
        "actions": [],
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
    }