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
ENTRY_TF = "1m"

EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14

PULLBACK_LOOKBACK = 8
BREAKOUT_LOOKBACK = 12
SWEEP_LOOKBACK = 6
SWEEP_CLOSEBACK_ATR = 0.10

SL_BUFFER_ATR = 0.20
MIN_RR = 2.0
MIN_SIGNAL_GAP_SEC = 30

MAX_ACTIVE_TOTAL = 5
MAX_SIGNALS_PER_DAY_PER_SYMBOL = 6
DAILY_LOSS_LIMIT_R = -3.0
COOLDOWN_MIN_AFTER_LOSS = 20

BE_AFTER_TP1 = True
TRAIL_AFTER_TP1 = True
TRAIL_LOOKBACK = 8
TRAIL_BUFFER_ATR = 0.15
MIN_HOLD_SECONDS_AFTER_OPEN = 15

LIVE_CACHE_TTL_SEC = 2
MAX_HISTORY = 500
PERFORMANCE_REVIEW_N = 30

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
    timeframe: str = "1m"


class ScanRequest(BaseModel):
    timeframe: str = "1m"
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
        f"R:R: {r_mult}\n\n"
        f"⚡ Generated by DEXTRADEZ AI"
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
# INDICATORS / LEVELS
# =========================================================
def ema_last(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


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


def calculate_levels(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    supports = [recent_swing_low(candles, 20)] if len(candles) >= 20 else []
    resistances = [recent_swing_high(candles, 20)] if len(candles) >= 20 else []

    support_zone = None
    resistance_zone = None

    if supports:
        support_zone = {"low": supports[0], "high": supports[0], "mid": supports[0], "touches": 1}
    if resistances:
        resistance_zone = {"low": resistances[0], "high": resistances[0], "mid": resistances[0], "touches": 1}

    return {
        "support_zone": support_zone,
        "resistance_zone": resistance_zone,
        "supports": supports,
        "resistances": resistances,
    }


def get_trend(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    closes = [safe_float(c["close"]) for c in candles]
    ema20 = ema_last(closes, EMA_FAST)
    ema50 = ema_last(closes, EMA_SLOW)

    if ema20 is None or ema50 is None:
        return {"trend": "SIDEWAYS", "ema20": ema20, "ema50": ema50}

    if ema20 > ema50:
        return {"trend": "UP", "ema20": ema20, "ema50": ema50}
    if ema20 < ema50:
        return {"trend": "DOWN", "ema20": ema20, "ema50": ema50}
    return {"trend": "SIDEWAYS", "ema20": ema20, "ema50": ema50}
# =========================================================
# SIMPLE STRATEGIES
# =========================================================
def detect_pullback_continuation(candles: List[Dict[str, float]], trend_info: Dict[str, Any], atr_value: float) -> Optional[Dict[str, Any]]:
    if len(candles) < max(EMA_FAST, PULLBACK_LOOKBACK + 2) or atr_value <= 0:
        return None

    trend = trend_info.get("trend")
    ema20 = float(trend_info.get("ema20") or 0.0)

    last = candles[-1]
    last_close = safe_float(last["close"])
    last_open = safe_float(last["open"])
    last_high = safe_float(last["high"])
    last_low = safe_float(last["low"])

    if trend == "UP":
        touched = last_low <= ema20 + atr_value * 0.15
        bullish_close = last_close > last_open
        if touched and bullish_close:
            sl = recent_swing_low(candles, PULLBACK_LOOKBACK) - atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry + max((entry - sl) * MIN_RR, atr_value * 1.5)
            return {"direction": "BUY", "entry": entry, "sl": sl, "tp2": tp2, "reason": "pullback_continuation"}

    if trend == "DOWN":
        touched = last_high >= ema20 - atr_value * 0.15
        bearish_close = last_close < last_open
        if touched and bearish_close:
            sl = recent_swing_high(candles, PULLBACK_LOOKBACK) + atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry - max((sl - entry) * MIN_RR, atr_value * 1.5)
            return {"direction": "SELL", "entry": entry, "sl": sl, "tp2": tp2, "reason": "pullback_continuation"}

    return None


def detect_breakout_retest(candles: List[Dict[str, float]], trend_info: Dict[str, Any], atr_value: float) -> Optional[Dict[str, Any]]:
    if len(candles) < BREAKOUT_LOOKBACK + 3 or atr_value <= 0:
        return None

    trend = trend_info.get("trend")
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
        if broke and retested and held:
            sl = min(safe_float(prev["low"]), last_low) - atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry + max((entry - sl) * MIN_RR, atr_value * 1.5)
            return {"direction": "BUY", "entry": entry, "sl": sl, "tp2": tp2, "reason": "breakout_retest"}

    if trend == "DOWN":
        broke = prev_close < recent_low
        retested = last_high >= recent_low - atr_value * 0.20
        held = last_close < recent_low
        if broke and retested and held:
            sl = max(safe_float(prev["high"]), last_high) + atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry - max((sl - entry) * MIN_RR, atr_value * 1.5)
            return {"direction": "SELL", "entry": entry, "sl": sl, "tp2": tp2, "reason": "breakout_retest"}

    return None


def detect_liquidity_sweep(candles: List[Dict[str, float]], trend_info: Dict[str, Any], atr_value: float) -> Optional[Dict[str, Any]]:
    if len(candles) < SWEEP_LOOKBACK + 2 or atr_value <= 0:
        return None

    trend = trend_info.get("trend")
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
        closed_back = last_close >= recent_low + atr_value * SWEEP_CLOSEBACK_ATR
        bullish = last_close > last_open
        if swept and closed_back and bullish:
            sl = last_low - atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry + max((entry - sl) * MIN_RR, atr_value * 1.5)
            return {"direction": "BUY", "entry": entry, "sl": sl, "tp2": tp2, "reason": "liquidity_sweep_with_trend"}

    if trend == "DOWN":
        swept = last_high > recent_high
        closed_back = last_close <= recent_high - atr_value * SWEEP_CLOSEBACK_ATR
        bearish = last_close < last_open
        if swept and closed_back and bearish:
            sl = last_high + atr_value * SL_BUFFER_ATR
            entry = last_close
            tp2 = entry - max((sl - entry) * MIN_RR, atr_value * 1.5)
            return {"direction": "SELL", "entry": entry, "sl": sl, "tp2": tp2, "reason": "liquidity_sweep_with_trend"}

    return None
# =========================================================
# BUILD SIGNAL
# =========================================================
def build_signal_from_setup(setup: Dict[str, Any], trend_info: Dict[str, Any]) -> Dict[str, Any]:
    direction = setup["direction"]
    entry = float(setup["entry"])
    sl = float(setup["sl"])
    tp2 = float(setup["tp2"])

    risk = abs(entry - sl)
    if risk <= 0:
        return {"direction": "HOLD", "confidence": 0, "reason": "invalid_risk"}

    if direction == "BUY":
        tp1 = entry + risk
    else:
        tp1 = entry - risk

    r_mult = abs(tp2 - entry) / risk if risk > 0 else 0.0

    confidence = 72
    if setup["reason"] == "pullback_continuation":
        confidence = 75
    elif setup["reason"] == "breakout_retest":
        confidence = 78
    elif setup["reason"] == "liquidity_sweep_with_trend":
        confidence = 74

    score = 7 if confidence < 78 else 8
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
            "ema20": trend_info.get("ema20"),
            "ema50": trend_info.get("ema50"),
        },
    }


def pick_best_setup(candles_1m: List[Dict[str, float]]) -> Dict[str, Any]:
    atr_value = float(atr_last(candles_1m, ATR_PERIOD) or 0.0)
    if atr_value <= 0:
        return {"direction": "HOLD", "confidence": 0, "reason": "atr_not_ready"}

    trend_info = get_trend(candles_1m)
    trend = trend_info.get("trend")

    if trend == "SIDEWAYS":
        return {"direction": "HOLD", "confidence": 0, "reason": "sideways_trend"}

    setup = detect_pullback_continuation(candles_1m, trend_info, atr_value)
    if not setup:
        setup = detect_breakout_retest(candles_1m, trend_info, atr_value)
    if not setup:
        setup = detect_liquidity_sweep(candles_1m, trend_info, atr_value)

    if not setup:
        return {"direction": "HOLD", "confidence": 0, "reason": f"no_setup_{trend.lower()}"}

    return build_signal_from_setup(setup, trend_info)
# =========================================================
# TRADE MANAGEMENT
# =========================================================
def hit_level(direction: str, last_high: float, last_low: float, level: float, kind: str) -> bool:
    if direction == "BUY":
        return last_high >= level if kind in ("TP1", "TP2", "TP") else last_low <= level
    return last_low <= level if kind in ("TP1", "TP2", "TP") else last_high >= level


def trail_stop_suggestion(candles_1m: List[Dict[str, float]], direction: str, atr_value: float) -> float:
    data = candles_1m[-TRAIL_LOOKBACK:] if len(candles_1m) >= TRAIL_LOOKBACK else candles_1m
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


async def manage_active_trade(symbol: str, timeframe: str, candles_1m: List[Dict[str, float]], trade: Dict[str, Any]) -> Dict[str, Any]:
    last = candles_1m[-1]
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
        return {"closed_trade": trade, "signal": {"direction": "HOLD", "confidence": 0, "reason": "trade_closed_sl"}, "active": False, "actions": actions}

    if tp1 and not trade.get("tp1_hit", False):
        if hit_level(direction, last_high, last_low, tp1, "TP1"):
            trade["tp1_hit"] = True
            actions.append({"type": "TP1_HIT"})
            if BE_AFTER_TP1:
                trade["sl"] = entry
                actions.append({"type": "MOVE_SL", "to": round(entry, 5)})
            await send_telegram_message(build_tp1_message(symbol, timeframe, trade))

    if TRAIL_AFTER_TP1 and trade.get("tp1_hit"):
        atr_value = float(atr_last(candles_1m, ATR_PERIOD) or 0.0)
        if atr_value > 0:
            trail = trail_stop_suggestion(candles_1m, direction, atr_value)
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
        return {"closed_trade": trade, "signal": {"direction": "HOLD", "confidence": 0, "reason": "trade_closed_tp2"}, "active": False, "actions": actions}

    trade["progress_pct"] = round(trade_progress_percent(direction, entry, safe_float(trade["sl"]), tp2, last_close), 2)
    return {"signal": trade, "active": True, "actions": actions}
# =========================================================
# ANALYZE CORE
# =========================================================
async def analyze_market(req: AnalyzeRequest):
    _reset_daily_if_needed()

    symbol = req.symbol.strip()
    timeframe = ENTRY_TF
    key = _trade_key(symbol, timeframe)

    app_id = os.getenv("DERIV_APP_ID", "1089")
    candles_1m = await _cached_fetch_candles(app_id, symbol, "1m", 260)

    if not candles_1m:
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

    last = candles_1m[-1]
    price = safe_float(last["close"])
    levels = calculate_levels(candles_1m)

    if key in ACTIVE_TRADES:
        managed = await manage_active_trade(symbol, timeframe, candles_1m, ACTIVE_TRADES[key])
        result = {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_1m,
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
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_1m,
            "levels": levels,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "max_active_trades"},
            "active": False,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }

    if RISK_STATE["daily_R"] <= DAILY_LOSS_LIMIT_R:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_1m,
            "levels": levels,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "daily_loss_limit_hit"},
            "active": False,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }

    if RISK_STATE["signals_today"].get(symbol, 0) >= MAX_SIGNALS_PER_DAY_PER_SYMBOL:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_1m,
            "levels": levels,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "max_signals_today_symbol"},
            "active": False,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }

    cooldown_until = int(RISK_STATE.get("cooldown_until", {}).get(symbol, 0))
    if _now_ts() < cooldown_until:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_1m,
            "levels": levels,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "cooldown_active"},
            "active": False,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }

    if (_now_ts() - int(LAST_SIGNAL_TS.get(symbol, 0))) < MIN_SIGNAL_GAP_SEC:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_1m,
            "levels": levels,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "signal_gap_active"},
            "active": False,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }

    signal = pick_best_setup(candles_1m)

    if signal.get("direction") in ("BUY", "SELL"):
        opened = await open_new_trade(symbol, timeframe, signal)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_1m,
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
        "candles": candles_1m,
        "levels": levels,
        "signal": signal,
        "active": False,
        "actions": [],
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
        "max_active_total": MAX_ACTIVE_TOTAL,
    }
# =========================================================
# API ROUTES
# =========================================================
@router.post("/analyze")
async def analyze(req: AnalyzeRequest):
    return await analyze_market(req)


@router.post("/scan")
async def scan(req: ScanRequest):
    _reset_daily_if_needed()

    rows: List[Dict[str, Any]] = []
    for sym in req.symbols:
        try:
            res = await analyze_market(AnalyzeRequest(symbol=sym, timeframe="1m"))
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


@router.get("/live")
async def live_market(symbol: str = "R_10", timeframe: str = "1m") -> Dict[str, Any]:
    app_id = os.getenv("DERIV_APP_ID", "1089")
    candles = await _cached_fetch_candles(app_id, symbol, "1m", 260)

    if not candles:
        return {"ok": False, "error": "No candles returned", "symbol": symbol, "timeframe": timeframe}

    last = candles[-1]
    levels = calculate_levels(candles)

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": "1m",
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
# =========================================================
# WEBSOCKET
# =========================================================
@router.websocket("/ws")
async def websocket_live(ws: WebSocket):
    await ws.accept()

    try:
        raw = await ws.receive_text()
        msg = json.loads(raw)

        symbol = (msg.get("symbol") or "R_10").strip()
        timeframe = "1m"

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