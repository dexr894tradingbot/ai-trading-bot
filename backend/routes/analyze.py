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
BIAS_TF_1 = "15m"
BIAS_TF_2 = "1h"
DAILY_TF = "1d"
WEEKLY_TF = "1w"
MONTHLY_TF = "1M"
FINE_TUNE_TF = "1m"

EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14

SWING_LEN = 3
STRUCTURE_LOOKBACK = 90

SL_BUFFER_ATR = 0.18
TRAIL_BUFFER_ATR = 0.15
TRAIL_LOOKBACK = 8

MIN_RR = 1.80
MIN_EXPANSION_ATR = 1.00
MIN_DISPLACEMENT_BODY_ATR = 0.38

# Stronger filters so bot avoids weak / late / choppy trades
MIN_TREND_STRENGTH = 0.12
MIN_CONFIDENCE_TO_OPEN = 60

MIN_SIGNAL_GAP_SEC = 45
MIN_HOLD_SECONDS_AFTER_OPEN = 30

MAX_ACTIVE_TOTAL = 3
MAX_ACTIVE_PER_SYMBOL = 1
COOLDOWN_MIN_AFTER_LOSS = 5

BE_AFTER_TP1 = True
TRAIL_AFTER_TP1 = True

TRADE_A_SIZE = 0.70
TRADE_B_SIZE = 0.30

MAX_HISTORY = 500
PERFORMANCE_REVIEW_N = 30
LIVE_CACHE_TTL_SEC = 2

ZONE_LOOKBACK = 220
ZONE_CLUSTER_ATR = 0.30
ZONE_MIN_TOUCHES = 2

POOL_LOOKBACK = 160
POOL_CLUSTER_ATR = 0.22
POOL_MIN_TOUCHES = 2

TELEGRAM_MIN_BRIEFING_GAP_SEC = 1800
TELEGRAM_MIN_WATCH_GAP_SEC = 600
TELEGRAM_MIN_READY_GAP_SEC = 180
TELEGRAM_MIN_INVALIDATED_GAP_SEC = 300

PROGRESS_ALERT_STEPS = [25, 50, 75, 90]

ALLOWED_TFS = {
    "1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w", "1M"
}

# =========================================================
# SNIPER STRATEGY TUNING
# =========================================================
PULLBACK_LOOKBACK = 6
CONFIRMATION_LOOKBACK = 3
RETEST_LOOKBACK = 5

NO_TRADE_AFTER_IMPULSE_ATR = 0.90
NO_TRADE_IF_TOO_FAR_FROM_EMA = 1.10
EMA_PULLBACK_TOLERANCE_ATR = 0.45

BREAKOUT_BODY_ATR = 0.35
MIN_CONFIRMATION_BODY_RATIO = 0.35

MIN_ZONE_DISTANCE_ATR = 0.10
MAX_ZONE_DISTANCE_ATR = 1.35

EXHAUSTION_WICK_BODY_RATIO = 1.15
REVERSAL_BLOCK_BARS = 2
CHOP_LOOKBACK = 8
CHOP_MAX_DIRECTIONAL_BODY_RATIO = 0.52

# Anti-fast-entry controls
WAIT_FOR_CONFIRM_CLOSE = True
MIN_CONFIRM_BARS_AFTER_TOUCH = 1
MAX_CONFIRM_BARS_AFTER_TOUCH = 3
TOUCH_TOLERANCE_ATR = 0.12
MIN_TREND_ALIGNMENT_SCORE = 5

# TP1 logic
COUNT_TP1_AS_WIN = True

# =========================================================
# TIMEFRAME FETCH CANDIDATES
# =========================================================
HTF_CANDIDATES: Dict[str, List[str]] = {
    "monthly": ["1M", "30d", "4h"],
    "weekly": ["1w", "7d", "1d"],
    "daily": ["1d", "4h"],
    "h1": ["1h"],
    "m15": ["15m"],
    "m5": ["5m"],
    "m1": ["1m"],
}

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
LAST_PROGRESS_ALERT: Dict[str, int] = {}

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


def _week_key() -> str:
    now = datetime.now()
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


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


def _active_symbol_count(symbol: str) -> int:
    return sum(1 for t in ACTIVE_TRADES.values() if t.get("symbol") == symbol)


def _push_history(item: Dict[str, Any]) -> None:
    TRADE_HISTORY.append(item)
    if len(TRADE_HISTORY) > MAX_HISTORY:
        del TRADE_HISTORY[0: len(TRADE_HISTORY) - MAX_HISTORY]


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


def safe_int(x: Any, fallback: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return fallback


def _requested_chart_tf(raw_tf: Optional[str]) -> str:
    tf = (raw_tf or ENTRY_TF).strip()
    return tf if tf in ALLOWED_TFS else ENTRY_TF


def _live_cache_key(symbol: str, timeframe: str, count: int) -> str:
    return f"{symbol}:{timeframe}:{count}"


async def _cached_fetch_candles(
    app_id: str,
    symbol: str,
    timeframe: str,
    count: int,
) -> List[Dict[str, Any]]:
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


async def _fetch_first_supported_timeframe(
    app_id: str,
    symbol: str,
    tf_candidates: List[str],
    count: int,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    for tf in tf_candidates:
        try:
            candles = await _cached_fetch_candles(app_id, symbol, tf, count)
            if candles:
                return candles, tf
        except Exception:
            continue
    return [], None


def fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "-"
    return f"{float(x):.5f}"


def fmt_range(low: Optional[float], high: Optional[float]) -> str:
    if low is None or high is None:
        return "-"
    return f"{float(low):.5f} - {float(high):.5f}"


def closed_candles(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return candles[:-1] if len(candles) >= 3 else candles


def trade_progress_percent_fn(
    direction: str,
    entry: float,
    sl: float,
    tp2: float,
    current_price: float,
) -> float:
    try:
        if direction == "BUY":
            total = tp2 - entry
            done = current_price - entry
        else:
            total = entry - tp2
            done = entry - current_price

        if total <= 0:
            return 0.0

        pct = (done / total) * 100.0
        return max(0.0, min(100.0, pct))
    except Exception:
        return 0.0


def build_live_trade_tracker(trade: Dict[str, Any], current_price: float) -> Dict[str, Any]:
    direction = trade.get("direction", "HOLD")
    entry = safe_float(trade.get("entry"))
    sl = safe_float(trade.get("sl"))
    tp1 = safe_float(trade.get("tp1"))
    tp2 = safe_float(trade.get("tp2") or trade.get("tp"))

    progress_pct = trade_progress_percent_fn(direction, entry, sl, tp2, current_price)

    status = "Running"
    if progress_pct >= 90:
        status = "Near TP2"
    elif progress_pct >= 75:
        status = "Strong progress"
    elif progress_pct >= 50:
        status = "Mid move"
    elif progress_pct >= 25:
        status = "Early move"

    if trade.get("tp1_hit"):
        status = "Protected / TP1 hit"

    return {
        "direction": direction,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp1": round(tp1, 5) if tp1 else None,
        "tp2": round(tp2, 5) if tp2 else None,
        "current_price": round(current_price, 5),
        "progress_pct": round(progress_pct, 2),
        "status": status,
        "tp1_hit": bool(trade.get("tp1_hit")),
        "quality_grade": trade.get("quality_grade"),
        "quality_stars": trade.get("quality_stars"),
    }


def build_daily_market_outlook(ranked_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    actionable = [r for r in ranked_rows if r.get("direction") in ("BUY", "SELL")]
    best = actionable[0] if actionable else (ranked_rows[0] if ranked_rows else None)

    if not best:
        return {
            "headline": "No strong market edge right now",
            "best_symbol": "-",
            "best_direction": "-",
            "best_confidence": 0,
            "market_state": "-",
            "area_of_interest": "-",
            "preferred_setup": "-",
            "note": "Wait for better structure and cleaner confirmation.",
        }

    note = "Focus on zone touch + confirmation only."
    if best.get("market_state") == "TRENDING CLEAN":
        note = "Best conditions are in a clean directional structure."
    elif best.get("reversal_risk") == "HIGH":
        note = "High reversal risk. Stay selective."

    return {
        "headline": "Best market opportunity right now",
        "best_symbol": best.get("symbol", "-"),
        "best_direction": best.get("direction", "-"),
        "best_confidence": int(best.get("confidence", 0)),
        "market_state": best.get("market_state", "-"),
        "area_of_interest": best.get("area_of_interest", "-"),
        "preferred_setup": best.get("preferred_setup", "-"),
        "note": note,
    }


def quality_grade_from_signal(
    context: Dict[str, Any],
    setup_reason: str,
    confidence: int,
) -> Dict[str, Any]:
    trend_strength = float(context.get("trend_strength") or 0.0)
    reversal_risk = context.get("reversal_risk", "LOW")
    market_state = context.get("market_state", "")
    bias = context.get("bias", "NEUTRAL")

    score = int(confidence)

    if market_state == "TRENDING CLEAN":
        score += 8
    elif market_state == "TRENDING PULLBACK":
        score += 5
    elif market_state == "WEAK TREND":
        score -= 4

    if reversal_risk == "LOW":
        score += 2
    elif reversal_risk == "MEDIUM":
        score -= 4
    elif reversal_risk == "HIGH":
        score -= 10

    if trend_strength >= 0.20:
        score += 6
    elif trend_strength >= 0.12:
        score += 3

    if "retest" in setup_reason:
        score += 4
    if "confirmed" in setup_reason:
        score += 5
    if "breakout" in setup_reason:
        score += 4
    if "rejection" in setup_reason:
        score += 4
    if "sniper" in setup_reason:
        score += 2

    if bias == "NEUTRAL":
        score -= 10

    score = max(0, min(100, int(round(score))))

    if score >= 90:
        grade = "A+"
        stars = "⭐⭐⭐⭐⭐"
    elif score >= 84:
        grade = "A"
        stars = "⭐⭐⭐⭐"
    elif score >= 76:
        grade = "B+"
        stars = "⭐⭐⭐⭐"
    elif score >= 68:
        grade = "B"
        stars = "⭐⭐⭐"
    elif score >= 60:
        grade = "C"
        stars = "⭐⭐"
    else:
        grade = "D"
        stars = "⭐"

    return {
        "quality_score": score,
        "quality_grade": grade,
        "quality_stars": stars,
    }


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
    entry = fmt_price(signal.get("entry"))
    sl = fmt_price(signal.get("sl"))
    tp1 = fmt_price(signal.get("tp1"))
    tp2 = fmt_price(signal.get("tp2", signal.get("tp")))
    confidence = signal.get("confidence", 0)
    quality_grade = signal.get("quality_grade", "-")
    quality_stars = signal.get("quality_stars", "")

    title = "🟢 BUY SETUP" if direction == "BUY" else "🔴 SELL SETUP"

    return (
        f"{title} — {symbol} ({timeframe})\n\n"
        f"Quality: {quality_grade} {quality_stars}\n"
        f"Confidence: {confidence}%\n\n"
        f"Main Entry: {entry}\n"
        f"Stop Loss: {sl}\n\n"
        f"Execution Plan:\n"
        f"Trade A: {int(TRADE_A_SIZE * 100)}% size → TP1: {tp1}\n"
        f"Trade B: {int(TRADE_B_SIZE * 100)}% size → TP2: {tp2}\n\n"
        f"Management:\n"
        f"• Only enter after zone touch + confirmation close\n"
        f"• When TP1 hits, close Trade A\n"
        f"• Move Trade B stop loss to breakeven\n"
        f"• Let Trade B run to TP2\n"
    )


def build_briefing_message(symbol: str, timeframe: str, briefing: Dict[str, Any]) -> str:
    return (
        f"📊 MARKET UPDATE — {symbol} ({timeframe})\n\n"
        f"Bias: {briefing.get('bias', '-')}\n"
        f"Monthly Bias: {briefing.get('monthly_bias', '-')}\n"
        f"Weekly Bias: {briefing.get('weekly_bias', '-')}\n"
        f"Daily Bias: {briefing.get('daily_bias', '-')}\n"
        f"Market State: {briefing.get('market_state', '-')}\n"
        f"Trend Strength: {briefing.get('trend_strength', '-')}\n\n"
        f"Support Zone: {briefing.get('buyer_zone', '-')}\n"
        f"Resistance Zone: {briefing.get('seller_zone', '-')}\n"
        f"EMA Pullback Buy: {briefing.get('ema_pullback_buy', '-')}\n"
        f"EMA Pullback Sell: {briefing.get('ema_pullback_sell', '-')}\n\n"
        f"Liquidity Below: {briefing.get('liquidity_below', '-')}\n"
        f"Liquidity Above: {briefing.get('liquidity_above', '-')}"
    )


def build_watch_message(symbol: str, timeframe: str, briefing: Dict[str, Any]) -> str:
    return (
        f"👀 WATCHLIST — {symbol} ({timeframe})\n\n"
        f"Bias: {briefing.get('bias', '-')}\n"
        f"Zone: {briefing.get('area_of_interest', '-')}\n\n"
        f"Plan:\n"
        f"{briefing.get('preferred_setup', '-')}\n\n"
        f"Trigger:\n"
        f"{briefing.get('confirmation_needed', '-')}\n"
    )


def build_invalidated_message(symbol: str, timeframe: str, briefing: Dict[str, Any]) -> str:
    return (
        f"⚠️ IDEA INVALIDATED — {symbol} ({timeframe})\n\n"
        f"Bias: {briefing.get('bias', '-')}\n"
        f"Market State: {briefing.get('market_state', '-')}\n"
        f"Reason: {briefing.get('invalidation', '-')}"
    )


def build_tp1_message(symbol: str, timeframe: str, trade: Dict[str, Any]) -> str:
    return (
        f"✅ TP1 WIN — {symbol} ({timeframe})\n\n"
        f"Direction: {trade.get('direction', '-')}\n"
        f"Entry: {trade.get('entry', '-')}\n"
        f"TP1: {trade.get('tp1', '-')}\n\n"
        f"Trade A closed in profit.\n"
        f"Trade B remains open.\n"
        f"Stop moved to breakeven."
    )


def build_closed_message(
    symbol: str,
    timeframe: str,
    trade: Dict[str, Any],
    outcome: str,
    price: float,
) -> str:
    if outcome == "TP2":
        return (
            f"🏆 TP2 WIN — {symbol} ({timeframe})\n\n"
            f"Direction: {trade.get('direction', '-')}\n"
            f"Entry: {trade.get('entry', '-')}\n"
            f"Exit: {round(price, 5)}\n\n"
            f"Trade B reached TP2."
        )

    if outcome == "TP1_ONLY":
        return (
            f"✅ TP1 SECURED WIN — {symbol} ({timeframe})\n\n"
            f"Direction: {trade.get('direction', '-')}\n"
            f"Entry: {trade.get('entry', '-')}\n"
            f"Exit: {round(price, 5)}\n\n"
            f"TP1 was hit and secured.\n"
            f"Runner did not reach TP2."
        )

    if outcome == "BE":
        return (
            f"⚖️ RUNNER CLOSED AT BREAKEVEN — {symbol} ({timeframe})\n\n"
            f"Direction: {trade.get('direction', '-')}\n"
            f"Entry: {trade.get('entry', '-')}\n"
            f"Exit: {round(price, 5)}\n\n"
            f"TP1 was already secured.\n"
            f"Trade B closed at breakeven."
        )

    return (
        f"🛑 STOP LOSS HIT — {symbol} ({timeframe})\n\n"
        f"Direction: {trade.get('direction', '-')}\n"
        f"Entry: {trade.get('entry', '-')}\n"
        f"Exit: {round(price, 5)}\n\n"
        f"Trade closed in loss."
    )


def _performance(last_n: int) -> Dict[str, Any]:
    last_n = max(1, min(int(last_n), 200))
    closed = [t for t in TRADE_HISTORY if t.get("status") == "CLOSED"]
    window = closed[-last_n:]

    total = len(window)
    wins = sum(1 for t in window if t.get("outcome") in ("TP2", "TP1_ONLY"))
    losses = sum(1 for t in window if t.get("outcome") == "SL")
    partials = sum(1 for t in window if t.get("outcome") == "TP1_ONLY")
    full_wins = sum(1 for t in window if t.get("outcome") == "TP2")
    breakevens = sum(1 for t in window if t.get("outcome") == "BE")
    win_rate = (wins / total * 100.0) if total else 0.0

    r_vals: List[float] = []
    for t in window:
        outcome = t.get("outcome")
        if outcome == "TP2":
            r_vals.append(float(t.get("r_multiple") or 0.0))
        elif outcome == "TP1_ONLY":
            r_vals.append(float(t.get("tp1_r_multiple") or TRADE_A_SIZE))
        elif outcome == "BE":
            r_vals.append(0.0)
        elif outcome == "SL":
            r_vals.append(-1.0)

    avg_r = (sum(r_vals) / len(r_vals)) if r_vals else 0.0
    total_r = sum(r_vals) if r_vals else 0.0

    return {
        "last_n": last_n,
        "total_closed": total,
        "wins": wins,
        "losses": losses,
        "partials": partials,
        "full_wins": full_wins,
        "breakevens": breakevens,
        "win_rate": round(win_rate, 2),
        "avg_R": round(avg_r, 3),
        "total_R": round(total_r, 3),
    }


def _weekly_performance(week_key: Optional[str] = None) -> Dict[str, Any]:
    wk = week_key or _week_key()
    closed = [
        t for t in TRADE_HISTORY
        if t.get("status") == "CLOSED" and t.get("week_key") == wk
    ]

    total = len(closed)
    wins = sum(1 for t in closed if t.get("outcome") in ("TP2", "TP1_ONLY"))
    losses = sum(1 for t in closed if t.get("outcome") == "SL")
    partials = sum(1 for t in closed if t.get("outcome") == "TP1_ONLY")
    full_wins = sum(1 for t in closed if t.get("outcome") == "TP2")
    breakevens = sum(1 for t in closed if t.get("outcome") == "BE")

    win_rate = (wins / total * 100.0) if total else 0.0
    loss_rate = (losses / total * 100.0) if total else 0.0

    return {
        "week_key": wk,
        "total_closed": total,
        "wins": wins,
        "losses": losses,
        "partials": partials,
        "full_wins": full_wins,
        "breakevens": breakevens,
        "win_rate": round(win_rate, 2),
        "loss_rate": round(loss_rate, 2),
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


def candle_closes_near_high(c: Dict[str, float], threshold: float = 0.70) -> bool:
    rng = max(1e-9, candle_range(c))
    pos = (safe_float(c["close"]) - safe_float(c["low"])) / rng
    return pos >= threshold


def candle_closes_near_low(c: Dict[str, float], threshold: float = 0.30) -> bool:
    rng = max(1e-9, candle_range(c))
    pos = (safe_float(c["close"]) - safe_float(c["low"])) / rng
    return pos <= threshold


def candle_closes_strong_bullish(c: Dict[str, float]) -> bool:
    return is_bullish(c) and candle_body_ratio(c) >= MIN_CONFIRMATION_BODY_RATIO


def candle_closes_strong_bearish(c: Dict[str, float]) -> bool:
    return is_bearish(c) and candle_body_ratio(c) >= MIN_CONFIRMATION_BODY_RATIO


def bullish_engulfing(prev: Dict[str, float], curr: Dict[str, float]) -> bool:
    return (
        is_bearish(prev)
        and is_bullish(curr)
        and safe_float(curr["close"]) > safe_float(prev["high"])
    )


def bearish_engulfing(prev: Dict[str, float], curr: Dict[str, float]) -> bool:
    return (
        is_bullish(prev)
        and is_bearish(curr)
        and safe_float(curr["close"]) < safe_float(prev["low"])
    )


def strong_displacement(candle: Dict[str, float], atr_value: float, direction: str) -> bool:
    if atr_value <= 0:
        return False

    body = candle_body(candle)
    ratio = candle_body_ratio(candle)

    if body < atr_value * MIN_DISPLACEMENT_BODY_ATR:
        return False
    if ratio < 0.35:
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
        return is_bullish(candle) and (ratio >= 0.28 or lw >= body * 0.5)
    if direction == "SELL":
        return is_bearish(candle) and (ratio >= 0.28 or uw >= body * 0.5)
    return False


def opposite_rejection_block(candle: Dict[str, float], intended_direction: str) -> bool:
    body = max(1e-9, candle_body(candle))
    uw = upper_wick(candle)
    lw = lower_wick(candle)

    if intended_direction == "BUY":
        return is_bearish(candle) and uw >= body * EXHAUSTION_WICK_BODY_RATIO
    return is_bullish(candle) and lw >= body * EXHAUSTION_WICK_BODY_RATIO


def is_impulse_candle(candle: Dict[str, float], atr_value: float) -> bool:
    if atr_value <= 0:
        return False
    return candle_body(candle) >= atr_value * NO_TRADE_AFTER_IMPULSE_ATR


def too_far_from_ema(price: float, ema20: float, atr_value: float) -> bool:
    if atr_value <= 0:
        return False
    return abs(price - ema20) >= atr_value * NO_TRADE_IF_TOO_FAR_FROM_EMA


def valid_pullback(direction: str, candles: List[Dict[str, float]], ema20: float, atr_value: float) -> bool:
    recent = candles[-PULLBACK_LOOKBACK:] if len(candles) >= PULLBACK_LOOKBACK else candles
    if not recent or atr_value <= 0:
        return False

    for c in recent:
        if direction == "BUY":
            if safe_float(c["low"]) <= ema20 + atr_value * EMA_PULLBACK_TOLERANCE_ATR:
                return True
        else:
            if safe_float(c["high"]) >= ema20 - atr_value * EMA_PULLBACK_TOLERANCE_ATR:
                return True
    return False


def recent_exhaustion_against_trade(
    direction: str,
    candles: List[Dict[str, float]],
    atr_value: float,
) -> bool:
    if len(candles) < 1 or atr_value <= 0:
        return False

    recent = candles[-REVERSAL_BLOCK_BARS:] if len(candles) >= REVERSAL_BLOCK_BARS else candles

    for c in recent:
        if direction == "BUY":
            if opposite_rejection_block(c, "BUY"):
                return True
            if is_bearish(c) and candle_body(c) >= atr_value * 0.80 and candle_closes_near_low(c, 0.35):
                return True
        else:
            if opposite_rejection_block(c, "SELL"):
                return True
            if is_bullish(c) and candle_body(c) >= atr_value * 0.80 and candle_closes_near_high(c, 0.65):
                return True

    return False


def market_is_too_choppy(candles: List[Dict[str, float]], atr_value: float) -> bool:
    if len(candles) < CHOP_LOOKBACK or atr_value <= 0:
        return False

    recent = candles[-CHOP_LOOKBACK:]
    directional_body_sum = 0.0
    total_range_sum = 0.0
    alternating = 0

    for i, c in enumerate(recent):
        directional_body_sum += candle_body(c)
        total_range_sum += candle_range(c)
        if i > 0:
            prev = recent[i - 1]
            if is_bullish(prev) != is_bullish(c):
                alternating += 1

    avg_body_ratio = directional_body_sum / max(1e-9, total_range_sum)
    avg_range = total_range_sum / len(recent)

    if avg_body_ratio < CHOP_MAX_DIRECTIONAL_BODY_RATIO and alternating >= len(recent) // 2:
        return True

    if avg_range < atr_value * 0.85 and alternating >= len(recent) // 2:
        return True

    return False


def breakout_retest_detected(
    direction: str,
    candles: List[Dict[str, float]],
    level: float,
    atr_value: float,
) -> bool:
    if len(candles) < RETEST_LOOKBACK + 2 or atr_value <= 0:
        return False

    recent = candles[-RETEST_LOOKBACK:]
    breakout = recent[-2]
    last = recent[-1]

    if direction == "BUY":
        broke = (
            is_bullish(breakout)
            and candle_body(breakout) >= atr_value * BREAKOUT_BODY_ATR
            and safe_float(breakout["close"]) > level + atr_value * 0.10
        )
        retest = safe_float(last["low"]) <= level + atr_value * 0.18
        confirm = candle_closes_strong_bullish(last) or bullish_engulfing(breakout, last)
        return broke and retest and confirm

    broke = (
        is_bearish(breakout)
        and candle_body(breakout) >= atr_value * BREAKOUT_BODY_ATR
        and safe_float(breakout["close"]) < level - atr_value * 0.10
    )
    retest = safe_float(last["high"]) >= level - atr_value * 0.18
    confirm = candle_closes_strong_bearish(last) or bearish_engulfing(breakout, last)
    return broke and retest and confirm


def confirmation_candle_present(
    direction: str,
    candles: List[Dict[str, float]],
    atr_value: float,
) -> bool:
    if len(candles) < 2:
        return False

    recent = candles[-CONFIRMATION_LOOKBACK:] if len(candles) >= CONFIRMATION_LOOKBACK else candles
    last = recent[-1]
    prev = recent[-2] if len(recent) >= 2 else recent[-1]

    if direction == "BUY":
        return (
            bullish_engulfing(prev, last)
            or strong_rejection(last, "BUY")
            or strong_displacement(last, atr_value, "BUY")
            or candle_closes_strong_bullish(last)
            or (is_bullish(last) and candle_closes_near_high(last, 0.60))
        )

    return (
        bearish_engulfing(prev, last)
        or strong_rejection(last, "SELL")
        or strong_displacement(last, atr_value, "SELL")
        or candle_closes_strong_bearish(last)
        or (is_bearish(last) and candle_closes_near_low(last, 0.40))
    )


def get_ema_pullback_zone(
    direction: str,
    ema20: float,
    ema50: float,
    atr_value: float,
) -> Dict[str, float]:
    if direction == "BUY":
        low = min(ema20, ema50) - atr_value * 0.08
        high = max(ema20, ema50) + atr_value * EMA_PULLBACK_TOLERANCE_ATR
    else:
        low = min(ema20, ema50) - atr_value * EMA_PULLBACK_TOLERANCE_ATR
        high = max(ema20, ema50) + atr_value * 0.08

    return {
        "level": round((low + high) / 2.0, 5),
        "low": round(low, 5),
        "high": round(high, 5),
        "touches": 1,
    }


def trend_alignment_score(
    entry_trend: str,
    tf15_trend: str,
    tf1h_trend: str,
    daily_trend: str,
    weekly_trend: str,
    monthly_trend: str,
    desired: str,
) -> int:
    score = 0
    if entry_trend == desired:
        score += 1
    if tf15_trend == desired:
        score += 1
    if tf1h_trend == desired:
        score += 1
    if daily_trend == desired:
        score += 2
    if weekly_trend == desired:
        score += 2
    if monthly_trend == desired:
        score += 2
    return score


def get_recent_local_high(candles: List[Dict[str, float]], lookback: int = 4) -> Optional[float]:
    if len(candles) < lookback + 1:
        return None
    sample = candles[-(lookback + 1):-1]
    return max(safe_float(c["high"]) for c in sample) if sample else None


def get_recent_local_low(candles: List[Dict[str, float]], lookback: int = 4) -> Optional[float]:
    if len(candles) < lookback + 1:
        return None
    sample = candles[-(lookback + 1):-1]
    return min(safe_float(c["low"]) for c in sample) if sample else None


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

        left = candles[i - swing_len: i]
        right = candles[i + 1: i + 1 + swing_len]

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


def zone_mid(zone: Optional[Dict[str, Any]]) -> Optional[float]:
    if not zone:
        return None
    return (safe_float(zone["low"]) + safe_float(zone["high"])) / 2.0


def candle_interacts_with_zone(candle: Dict[str, float], zone: Dict[str, Any]) -> bool:
    low = safe_float(candle["low"])
    high = safe_float(candle["high"])
    z_low = safe_float(zone["low"])
    z_high = safe_float(zone["high"])
    return not (high < z_low or low > z_high)


def zone_reaction_metrics(
    candles: List[Dict[str, float]],
    zone: Dict[str, Any],
    direction: str,
    atr_value: float,
) -> Dict[str, float]:
    touches = 0
    rejection_score = 0.0
    move_away_score = 0.0
    last_touch_index = -1

    if not candles:
        return {
            "touches_seen": 0.0,
            "rejection_score": 0.0,
            "move_away_score": 0.0,
            "recency_score": 0.0,
        }

    for i, c in enumerate(candles):
        if candle_interacts_with_zone(c, zone):
            touches += 1
            last_touch_index = i

            body = candle_body(c)
            uw = upper_wick(c)
            lw = lower_wick(c)

            if direction == "BUY":
                if lw >= body * 0.8 or is_bullish(c):
                    rejection_score += 1.0
            else:
                if uw >= body * 0.8 or is_bearish(c):
                    rejection_score += 1.0

            if i + 2 < len(candles):
                c0 = safe_float(c["close"])
                c1 = safe_float(candles[i + 1]["close"])
                c2 = safe_float(candles[i + 2]["close"])

                if atr_value > 0:
                    if direction == "BUY":
                        move_away = max(c1 - c0, c2 - c0, 0.0)
                    else:
                        move_away = max(c0 - c1, c0 - c2, 0.0)
                    move_away_score += move_away / max(1e-9, atr_value)

    recency_score = 0.0
    if last_touch_index >= 0:
        bars_ago = len(candles) - 1 - last_touch_index
        if bars_ago <= 12:
            recency_score = 3.0
        elif bars_ago <= 25:
            recency_score = 1.5

    return {
        "touches_seen": float(touches),
        "rejection_score": rejection_score,
        "move_away_score": move_away_score,
        "recency_score": recency_score,
    }


def score_zone(
    direction: str,
    zone: Dict[str, Any],
    current_price: float,
    atr_value: float,
    matching_pool: Optional[Dict[str, Any]],
    candles: List[Dict[str, float]],
) -> float:
    score = 0.0

    touches = int(zone.get("touches", 0))
    width = max(0.0, safe_float(zone["high"]) - safe_float(zone["low"]))
    mid = zone_mid(zone)
    dist = abs(mid - current_price) if mid is not None else 999999.0

    score += touches * 10.0

    if atr_value > 0:
        if MIN_ZONE_DISTANCE_ATR * atr_value <= dist <= MAX_ZONE_DISTANCE_ATR * atr_value:
            score += 11.0
        elif dist < MIN_ZONE_DISTANCE_ATR * atr_value:
            score += 5.0
        else:
            score -= 4.0

        if 0.10 * atr_value <= width <= 1.5 * atr_value:
            score += 6.0
        elif width > 2.2 * atr_value:
            score -= 2.0

    if matching_pool is not None:
        if abs(safe_float(matching_pool["level"]) - safe_float(zone["level"])) <= atr_value * 0.8:
            score += 5.0

    metrics = zone_reaction_metrics(candles, zone, direction, atr_value)
    score += metrics["rejection_score"] * 3.0
    score += metrics["move_away_score"] * 2.5
    score += metrics["recency_score"]

    if direction == "BUY" and safe_float(zone["level"]) <= current_price:
        score += 3.0
    if direction == "SELL" and safe_float(zone["level"]) >= current_price:
        score += 3.0

    return score


def pick_best_trade_zone(
    direction: str,
    zones: Dict[str, Any],
    pools: Dict[str, Any],
    current_price: float,
    atr_value: float,
    candles: List[Dict[str, float]],
) -> Optional[Dict[str, Any]]:
    if direction == "BUY":
        candidates = [z for z in zones.get("supports", []) if safe_float(z["level"]) <= current_price]
        pool = pools.get("nearest_low_pool")
    else:
        candidates = [z for z in zones.get("resistances", []) if safe_float(z["level"]) >= current_price]
        pool = pools.get("nearest_high_pool")

    if not candidates:
        return None

    best_zone = None
    best_score = -999999.0
    for z in candidates:
        s = score_zone(direction, z, current_price, atr_value, pool, candles)
        if s > best_score:
            best_score = s
            best_zone = z

    return best_zone


# =========================================================
# MARKET INTELLIGENCE
# =========================================================
def combine_bias(
    entry_bias: str,
    bias_15m: str,
    bias_1h: str,
    daily_bias: str,
    weekly_bias: str,
    monthly_bias: str,
) -> str:
    up = 0
    down = 0

    for x in [entry_bias, bias_15m, bias_1h]:
        if x == "UP":
            up += 1
        elif x == "DOWN":
            down += 1

    if daily_bias == "UP":
        up += 2
    elif daily_bias == "DOWN":
        down += 2

    if weekly_bias == "UP":
        up += 2
    elif weekly_bias == "DOWN":
        down += 2

    if monthly_bias == "UP":
        up += 2
    elif monthly_bias == "DOWN":
        down += 2

    if up > down and up >= 4:
        return "UP"
    if down > up and down >= 4:
        return "DOWN"

    return "NEUTRAL"


def classify_market_state(
    combined_bias: str,
    trend_strength: float,
    reversal_risk: str,
    fine_tune_trend: str,
    alignment_score: int,
) -> str:
    if combined_bias == "NEUTRAL":
        return "CHOPPY / NO-TRADE"
    if reversal_risk == "HIGH":
        return "REVERSAL RISK"
    if alignment_score >= 7 and trend_strength >= 0.18 and fine_tune_trend == combined_bias:
        return "TRENDING CLEAN"
    if alignment_score >= 5 and trend_strength >= 0.12:
        return "TRENDING PULLBACK"
    return "WEAK TREND"


def detect_reversal_risk(
    combined_bias: str,
    trend_entry: Dict[str, Any],
    trend_15m: Dict[str, Any],
    trend_1h: Dict[str, Any],
    trend_daily: Dict[str, Any],
    trend_weekly: Dict[str, Any],
    trend_monthly: Dict[str, Any],
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

    for trend_blob, weight in [
        (trend_15m, 1),
        (trend_1h, 1),
        (trend_daily, 2),
        (trend_weekly, 2),
        (trend_monthly, 2),
    ]:
        if trend_blob.get("trend") not in ("SIDEWAYS", combined_bias):
            risk_points += weight

    if atr_value > 0:
        if combined_bias == "UP" and nearest_resistance:
            dist = abs(float(nearest_resistance["level"]) - current_price)
            if dist < atr_value * 0.7:
                risk_points += 1
        if combined_bias == "DOWN" and nearest_support:
            dist = abs(current_price - float(nearest_support["level"]))
            if dist < atr_value * 0.7:
                risk_points += 1

    if risk_points >= 7:
        return "HIGH"
    if risk_points >= 4:
        return "MEDIUM"
    return "LOW"
def price_touched_zone_recently(
    direction: str,
    candles: List[Dict[str, float]],
    zone: Dict[str, Any],
    atr_value: float,
    bars: int = PULLBACK_LOOKBACK,
) -> bool:
    recent = candles[-bars:] if len(candles) >= bars else candles
    z_low = safe_float(zone["low"])
    z_high = safe_float(zone["high"])

    for c in recent:
        lo = safe_float(c["low"])
        hi = safe_float(c["high"])

        if direction == "BUY":
            if lo <= z_high + atr_value * TOUCH_TOLERANCE_ATR:
                return True
        else:
            if hi >= z_low - atr_value * TOUCH_TOLERANCE_ATR:
                return True

    return False


def bars_since_zone_touch(
    direction: str,
    candles: List[Dict[str, float]],
    zone: Dict[str, Any],
    atr_value: float,
    lookback: int = 8,
) -> Optional[int]:
    if not candles:
        return None

    recent = candles[-lookback:] if len(candles) >= lookback else candles
    z_low = safe_float(zone["low"])
    z_high = safe_float(zone["high"])

    for idx in range(len(recent) - 1, -1, -1):
        c = recent[idx]
        lo = safe_float(c["low"])
        hi = safe_float(c["high"])

        touched = False
        if direction == "BUY":
            touched = lo <= z_high + atr_value * TOUCH_TOLERANCE_ATR
        else:
            touched = hi >= z_low - atr_value * TOUCH_TOLERANCE_ATR

        if touched:
            return len(recent) - 1 - idx

    return None


def select_targets(
    direction: str,
    entry: float,
    zones: Dict[str, Any],
    pools: Dict[str, Any],
    min_distance: float,
) -> Dict[str, Optional[float]]:
    targets: List[float] = []

    if direction == "BUY":
        for z in zones.get("resistances", []):
            lvl = safe_float(z["level"])
            if lvl > entry:
                targets.append(lvl)
        for p in pools.get("high_pools", []):
            lvl = safe_float(p["level"])
            if lvl > entry:
                targets.append(lvl)

        targets = sorted(set(round(x, 5) for x in targets))
        eligible = [x for x in targets if (x - entry) >= min_distance]
        if not eligible:
            return {"tp1": None, "tp2": None}

        tp2 = eligible[0]
        tp1 = entry + min_distance
        if tp1 >= tp2:
            tp1 = entry + (tp2 - entry) * 0.55
        return {"tp1": tp1, "tp2": tp2}

    for z in zones.get("supports", []):
        lvl = safe_float(z["level"])
        if lvl < entry:
            targets.append(lvl)
    for p in pools.get("low_pools", []):
        lvl = safe_float(p["level"])
        if lvl < entry:
            targets.append(lvl)

    targets = sorted(set(round(x, 5) for x in targets), reverse=True)
    eligible = [x for x in targets if (entry - x) >= min_distance]
    if not eligible:
        return {"tp1": None, "tp2": None}

    tp2 = eligible[0]
    tp1 = entry - min_distance
    if tp1 <= tp2:
        tp1 = entry - (entry - tp2) * 0.55
    return {"tp1": tp1, "tp2": tp2}


def build_market_context(
    entry_data: List[Dict[str, float]],
    tf15_data: List[Dict[str, float]],
    tf1h_data: List[Dict[str, float]],
    daily_data: List[Dict[str, float]],
    weekly_data: List[Dict[str, float]],
    monthly_data: List[Dict[str, float]],
    fine_data: List[Dict[str, float]],
) -> Dict[str, Any]:
    atr_value = float(atr_last(entry_data, ATR_PERIOD) or 0.0)
    trend_entry = get_trend_memory(entry_data)
    trend_15m = get_trend_memory(tf15_data)
    trend_1h = get_trend_memory(tf1h_data)
    trend_daily = get_trend_memory(daily_data) if daily_data else {"trend": "SIDEWAYS"}
    trend_weekly = get_trend_memory(weekly_data) if weekly_data else {"trend": "SIDEWAYS"}
    trend_monthly = get_trend_memory(monthly_data) if monthly_data else {"trend": "SIDEWAYS"}
    trend_fine = get_trend_memory(fine_data) if fine_data else {"trend": "SIDEWAYS"}

    structure = get_structure_state(entry_data, atr_value)
    combined_bias = combine_bias(
        structure.get("bias", "NEUTRAL"),
        trend_15m.get("trend", "NEUTRAL"),
        trend_1h.get("trend", "NEUTRAL"),
        trend_daily.get("trend", "NEUTRAL"),
        trend_weekly.get("trend", "NEUTRAL"),
        trend_monthly.get("trend", "NEUTRAL"),
    )

    zones = reaction_zones(entry_data, atr_value)
    pools = liquidity_pools(entry_data, atr_value)

    current_price = safe_float(entry_data[-1]["close"]) if entry_data else 0.0
    nearest_support = zones.get("nearest_support")
    nearest_resistance = zones.get("nearest_resistance")
    nearest_low_pool = pools.get("nearest_low_pool")
    nearest_high_pool = pools.get("nearest_high_pool")

    best_buy_zone = pick_best_trade_zone("BUY", zones, pools, current_price, atr_value, entry_data)
    best_sell_zone = pick_best_trade_zone("SELL", zones, pools, current_price, atr_value, entry_data)

    ema20 = safe_float(trend_entry.get("ema20"))
    ema50 = safe_float(trend_entry.get("ema50"))

    ema_pullback_buy = get_ema_pullback_zone("BUY", ema20, ema50, atr_value) if ema20 and ema50 and atr_value > 0 else None
    ema_pullback_sell = get_ema_pullback_zone("SELL", ema20, ema50, atr_value) if ema20 and ema50 and atr_value > 0 else None

    alignment_score = trend_alignment_score(
        trend_entry.get("trend", "SIDEWAYS"),
        trend_15m.get("trend", "SIDEWAYS"),
        trend_1h.get("trend", "SIDEWAYS"),
        trend_daily.get("trend", "SIDEWAYS"),
        trend_weekly.get("trend", "SIDEWAYS"),
        trend_monthly.get("trend", "SIDEWAYS"),
        combined_bias if combined_bias in ("UP", "DOWN") else "SIDEWAYS",
    )

    reversal_risk = detect_reversal_risk(
        combined_bias=combined_bias,
        trend_entry=trend_entry,
        trend_15m=trend_15m,
        trend_1h=trend_1h,
        trend_daily=trend_daily,
        trend_weekly=trend_weekly,
        trend_monthly=trend_monthly,
        structure=structure,
        fine_tune_trend=trend_fine.get("trend", "SIDEWAYS"),
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        current_price=current_price,
        atr_value=atr_value,
    )

    if combined_bias == "UP":
        aoi = best_buy_zone or nearest_support or nearest_low_pool or ema_pullback_buy
        preferred_setup = "Wait for bullish reaction at support / demand zone, then confirm on M5"
        confirmation_needed = "Zone touch + bullish rejection/engulfing OR breakout-retest continuation"
        invalidation = (
            f"5m closes below {fmt_price(aoi['low'])}"
            if aoi and aoi.get("low") is not None
            else "Break of bullish structure"
        )
    elif combined_bias == "DOWN":
        aoi = best_sell_zone or nearest_resistance or nearest_high_pool or ema_pullback_sell
        preferred_setup = "Wait for bearish reaction at resistance / supply zone, then confirm on M5"
        confirmation_needed = "Zone touch + bearish rejection/engulfing OR breakout-retest continuation"
        invalidation = (
            f"5m closes above {fmt_price(aoi['high'])}"
            if aoi and aoi.get("high") is not None
            else "Break of bearish structure"
        )
    else:
        aoi = None
        preferred_setup = "No clean directional edge"
        confirmation_needed = "Wait for structure and higher-timeframe alignment"
        invalidation = "N/A"

    market_state = classify_market_state(
        combined_bias=combined_bias,
        trend_strength=float(trend_entry.get("trend_strength") or 0.0),
        reversal_risk=reversal_risk,
        fine_tune_trend=trend_fine.get("trend", "SIDEWAYS"),
        alignment_score=alignment_score,
    )

    return {
        "bias": combined_bias,
        "monthly_bias": trend_monthly.get("trend", "SIDEWAYS"),
        "weekly_bias": trend_weekly.get("trend", "SIDEWAYS"),
        "daily_bias": trend_daily.get("trend", "SIDEWAYS"),
        "structure": structure.get("bias", "NEUTRAL"),
        "previous_trend": trend_entry.get("prev_trend", "SIDEWAYS"),
        "trend_strength": trend_entry.get("trend_strength", 0.0),
        "reversal_risk": reversal_risk,
        "market_state": market_state,
        "alignment_score": alignment_score,
        "support": fmt_price(nearest_support["level"]) if nearest_support else "-",
        "resistance": fmt_price(nearest_resistance["level"]) if nearest_resistance else "-",
        "buyer_zone": fmt_range(best_buy_zone["low"], best_buy_zone["high"]) if best_buy_zone else "-",
        "seller_zone": fmt_range(best_sell_zone["low"], best_sell_zone["high"]) if best_sell_zone else "-",
        "ema_pullback_buy": fmt_range(ema_pullback_buy["low"], ema_pullback_buy["high"]) if ema_pullback_buy else "-",
        "ema_pullback_sell": fmt_range(ema_pullback_sell["low"], ema_pullback_sell["high"]) if ema_pullback_sell else "-",
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
        "trend_daily": trend_daily,
        "trend_weekly": trend_weekly,
        "trend_monthly": trend_monthly,
        "trend_fine": trend_fine,
        "atr_value": atr_value,
        "best_buy_zone": best_buy_zone,
        "best_sell_zone": best_sell_zone,
        "ema_pullback_buy_zone": ema_pullback_buy,
        "ema_pullback_sell_zone": ema_pullback_sell,
    }


# =========================================================
# TRADE SETUP DETECTION
# =========================================================
def enough_room_to_run(direction: str, entry: float, tp2: float, atr_value: float) -> bool:
    projected = abs(tp2 - entry)
    return projected >= atr_value * MIN_EXPANSION_ATR


def detect_trade_setup(entry_data: List[Dict[str, float]], context: Dict[str, Any]) -> Dict[str, Any]:
    if len(entry_data) < 30:
        return {"direction": "HOLD", "reason": "not_enough_candles"}

    combined_bias = context["bias"]
    atr_value = float(context["atr_value"] or 0.0)
    structure = context["structure_raw"]
    trend_info = context["trend_entry"]
    trend_15m = context["trend_15m"]
    trend_1h = context["trend_1h"]
    trend_daily = context["trend_daily"]
    trend_weekly = context["trend_weekly"]
    trend_monthly = context["trend_monthly"]
    zones = context["zones"]
    pools = context["pools"]
    market_state = context.get("market_state", "")
    trend_strength = float(trend_info.get("trend_strength") or 0.0)

    if combined_bias == "NEUTRAL" or atr_value <= 0:
        return {"direction": "HOLD", "reason": "neutral_bias"}
    if market_state == "CHOPPY / NO-TRADE":
        return {"direction": "HOLD", "reason": "market_state_blocked"}
    if context.get("reversal_risk") == "HIGH":
        return {"direction": "HOLD", "reason": "reversal_risk_high"}
    if trend_strength < MIN_TREND_STRENGTH:
        return {"direction": "HOLD", "reason": "trend_strength_too_low"}
    if market_is_too_choppy(entry_data, atr_value):
        return {"direction": "HOLD", "reason": "market_too_choppy"}

    last = entry_data[-1]
    prev = entry_data[-2]
    last_close = safe_float(last["close"])

    closes = [safe_float(c["close"]) for c in entry_data]
    ema20 = float(ema_last(closes, EMA_FAST) or 0.0)
    ema50 = float(ema_last(closes, EMA_SLOW) or 0.0)

    if ema20 == 0.0 or ema50 == 0.0:
        return {"direction": "HOLD", "reason": "ema_unavailable"}

    if combined_bias == "UP":
        align_score = trend_alignment_score(
            trend_info.get("trend", "SIDEWAYS"),
            trend_15m.get("trend", "SIDEWAYS"),
            trend_1h.get("trend", "SIDEWAYS"),
            trend_daily.get("trend", "SIDEWAYS"),
            trend_weekly.get("trend", "SIDEWAYS"),
            trend_monthly.get("trend", "SIDEWAYS"),
            "UP",
        )
        if align_score < MIN_TREND_ALIGNMENT_SCORE:
            return {"direction": "HOLD", "reason": "buy_alignment_too_weak"}

        zone = context.get("best_buy_zone") or context.get("ema_pullback_buy_zone")
        if not zone:
            return {"direction": "HOLD", "reason": "no_buy_aoi"}
        if ema20 <= ema50:
            return {"direction": "HOLD", "reason": "trend_not_up"}
        if trend_daily.get("trend") == "DOWN" or trend_weekly.get("trend") == "DOWN" or trend_monthly.get("trend") == "DOWN":
            return {"direction": "HOLD", "reason": "htf_bias_conflict_buy"}
        if is_impulse_candle(last, atr_value):
            return {"direction": "HOLD", "reason": "buy_impulse_chase_blocked"}
        if recent_exhaustion_against_trade("BUY", entry_data, atr_value):
            return {"direction": "HOLD", "reason": "buy_exhaustion_block"}
        if too_far_from_ema(last_close, ema20, atr_value) and not price_touched_zone_recently("BUY", entry_data, zone, atr_value):
            return {"direction": "HOLD", "reason": "buy_extended_from_ema"}
        if not valid_pullback("BUY", entry_data, ema20, atr_value):
            return {"direction": "HOLD", "reason": "no_buy_pullback"}

        zone_touched = price_touched_zone_recently("BUY", entry_data, zone, atr_value)
        touch_bars_ago = bars_since_zone_touch("BUY", entry_data, zone, atr_value, lookback=8)
        rejection_confirm = confirmation_candle_present("BUY", entry_data, atr_value)
        breakout_confirm = breakout_retest_detected("BUY", entry_data, safe_float(zone["high"]), atr_value)

        if not zone_touched and not breakout_confirm:
            return {"direction": "HOLD", "reason": "buy_zone_not_tested"}

        if WAIT_FOR_CONFIRM_CLOSE and touch_bars_ago is not None:
            if touch_bars_ago < MIN_CONFIRM_BARS_AFTER_TOUCH and not breakout_confirm:
                return {"direction": "HOLD", "reason": "buy_wait_for_confirm_close"}
            if touch_bars_ago > MAX_CONFIRM_BARS_AFTER_TOUCH and not breakout_confirm:
                return {"direction": "HOLD", "reason": "buy_touch_too_old"}

        if not (rejection_confirm or breakout_confirm):
            return {"direction": "HOLD", "reason": "buy_confirmation_missing"}

        zone_mid_price = zone_mid(zone)
        if zone_mid_price is not None and abs(last_close - zone_mid_price) > atr_value * 2.5:
            return {"direction": "HOLD", "reason": "mid_zone_block"}

        swing_low = safe_float(structure.get("last_swing_low"), safe_float(zone["low"]))
        local_low = safe_float(get_recent_local_low(entry_data, 4), safe_float(zone["low"]))
        sl_anchor = min(
            safe_float(zone["low"]),
            swing_low,
            local_low,
            safe_float(last["low"]),
            safe_float(prev["low"]),
        )
        sl = sl_anchor - atr_value * SL_BUFFER_ATR
        entry = last_close
        risk = entry - sl
        if risk <= 0:
            return {"direction": "HOLD", "reason": "invalid_buy_risk"}

        targets = select_targets("BUY", entry, zones, pools, min_distance=risk * MIN_RR)
        tp2 = targets.get("tp2")
        if tp2 is None:
            return {"direction": "HOLD", "reason": "no_buy_target"}
        if not enough_room_to_run("BUY", entry, tp2, atr_value):
            return {"direction": "HOLD", "reason": "buy_target_too_close"}

        rr = (tp2 - entry) / max(1e-9, risk)
        if rr < MIN_RR:
            return {"direction": "HOLD", "reason": "buy_rr_too_low"}

        reason = "bullish_breakout_retest_confirmed_sniper" if breakout_confirm else "bullish_rejection_retest_confirmed_sniper"
        return {
            "direction": "BUY",
            "entry": entry,
            "sl": sl,
            "tp2": tp2,
            "tp1_hint": targets.get("tp1"),
            "reason": reason,
        }

    align_score = trend_alignment_score(
        trend_info.get("trend", "SIDEWAYS"),
        trend_15m.get("trend", "SIDEWAYS"),
        trend_1h.get("trend", "SIDEWAYS"),
        trend_daily.get("trend", "SIDEWAYS"),
        trend_weekly.get("trend", "SIDEWAYS"),
        trend_monthly.get("trend", "SIDEWAYS"),
        "DOWN",
    )
    if align_score < MIN_TREND_ALIGNMENT_SCORE:
        return {"direction": "HOLD", "reason": "sell_alignment_too_weak"}

    zone = context.get("best_sell_zone") or context.get("ema_pullback_sell_zone")
    if not zone:
        return {"direction": "HOLD", "reason": "no_sell_aoi"}
    if ema20 >= ema50:
        return {"direction": "HOLD", "reason": "trend_not_down"}
    if trend_daily.get("trend") == "UP" or trend_weekly.get("trend") == "UP" or trend_monthly.get("trend") == "UP":
        return {"direction": "HOLD", "reason": "htf_bias_conflict_sell"}
    if is_impulse_candle(last, atr_value):
        return {"direction": "HOLD", "reason": "sell_impulse_chase_blocked"}
    if recent_exhaustion_against_trade("SELL", entry_data, atr_value):
        return {"direction": "HOLD", "reason": "sell_exhaustion_block"}
    if too_far_from_ema(last_close, ema20, atr_value) and not price_touched_zone_recently("SELL", entry_data, zone, atr_value):
        return {"direction": "HOLD", "reason": "sell_extended_from_ema"}
    if not valid_pullback("SELL", entry_data, ema20, atr_value):
        return {"direction": "HOLD", "reason": "no_sell_pullback"}

    zone_touched = price_touched_zone_recently("SELL", entry_data, zone, atr_value)
    touch_bars_ago = bars_since_zone_touch("SELL", entry_data, zone, atr_value, lookback=8)
    rejection_confirm = confirmation_candle_present("SELL", entry_data, atr_value)
    breakout_confirm = breakout_retest_detected("SELL", entry_data, safe_float(zone["low"]), atr_value)

    if not zone_touched and not breakout_confirm:
        return {"direction": "HOLD", "reason": "sell_zone_not_tested"}

    if WAIT_FOR_CONFIRM_CLOSE and touch_bars_ago is not None:
        if touch_bars_ago < MIN_CONFIRM_BARS_AFTER_TOUCH and not breakout_confirm:
            return {"direction": "HOLD", "reason": "sell_wait_for_confirm_close"}
        if touch_bars_ago > MAX_CONFIRM_BARS_AFTER_TOUCH and not breakout_confirm:
            return {"direction": "HOLD", "reason": "sell_touch_too_old"}

    if not (rejection_confirm or breakout_confirm):
        return {"direction": "HOLD", "reason": "sell_confirmation_missing"}

    zone_mid_price = zone_mid(zone)
    if zone_mid_price is not None and abs(last_close - zone_mid_price) > atr_value * 2.5:
        return {"direction": "HOLD", "reason": "mid_zone_block"}

    swing_high = safe_float(structure.get("last_swing_high"), safe_float(zone["high"]))
    local_high = safe_float(get_recent_local_high(entry_data, 4), safe_float(zone["high"]))
    sl_anchor = max(
        safe_float(zone["high"]),
        swing_high,
        local_high,
        safe_float(last["high"]),
        safe_float(prev["high"]),
    )
    sl = sl_anchor + atr_value * SL_BUFFER_ATR
    entry = last_close
    risk = sl - entry
    if risk <= 0:
        return {"direction": "HOLD", "reason": "invalid_sell_risk"}

    targets = select_targets("SELL", entry, zones, pools, min_distance=risk * MIN_RR)
    tp2 = targets.get("tp2")
    if tp2 is None:
        return {"direction": "HOLD", "reason": "no_sell_target"}
    if not enough_room_to_run("SELL", entry, tp2, atr_value):
        return {"direction": "HOLD", "reason": "sell_target_too_close"}

    rr = (entry - tp2) / max(1e-9, risk)
    if rr < MIN_RR:
        return {"direction": "HOLD", "reason": "sell_rr_too_low"}

    reason = "bearish_breakout_retest_confirmed_sniper" if breakout_confirm else "bearish_rejection_retest_confirmed_sniper"
    return {
        "direction": "SELL",
        "entry": entry,
        "sl": sl,
        "tp2": tp2,
        "tp1_hint": targets.get("tp1"),
        "reason": reason,
    }
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

    tp1 = setup.get("tp1_hint")
    if tp1 is None:
        tp1 = entry + risk if direction == "BUY" else entry - risk

    trend_strength = float(context["trend_entry"].get("trend_strength") or 0.0)
    alignment_score = int(context.get("alignment_score") or 0)
    confidence = 56

    if alignment_score >= 8:
        confidence += 8
    elif alignment_score >= 5:
        confidence += 4

    if trend_strength >= 0.22:
        confidence += 8
    elif trend_strength >= 0.15:
        confidence += 5
    elif trend_strength >= 0.12:
        confidence += 3

    if context.get("market_state") == "TRENDING CLEAN":
        confidence += 6
    elif context.get("market_state") == "TRENDING PULLBACK":
        confidence += 4

    if context.get("reversal_risk") == "LOW":
        confidence += 3
    elif context.get("reversal_risk") == "MEDIUM":
        confidence -= 3

    rr = abs(tp2 - entry) / max(1e-9, risk)
    if rr >= 2.2:
        confidence += 5
    elif rr >= 1.9:
        confidence += 3

    confidence = max(0, min(94, confidence))
    quality = quality_grade_from_signal(context, setup["reason"], confidence)

    tp1_r_multiple = abs(float(tp1) - entry) / max(1e-9, risk)

    return {
        "direction": direction,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp": round(tp2, 5),
        "tp1": round(float(tp1), 5),
        "tp2": round(tp2, 5),
        "confidence": confidence,
        "reason": setup["reason"],
        "entry_type": setup["reason"].upper(),
        "mode": setup["reason"].upper(),
        "r_multiple": round(abs(tp2 - entry) / risk, 2),
        "tp1_r_multiple": round(tp1_r_multiple, 2),
        "quality_score": quality["quality_score"],
        "quality_grade": quality["quality_grade"],
        "quality_stars": quality["quality_stars"],
        "meta": {
            "bias": context["bias"],
            "monthly_bias": context["monthly_bias"],
            "weekly_bias": context["weekly_bias"],
            "daily_bias": context["daily_bias"],
            "structure": context["structure"],
            "trend_strength": context["trend_strength"],
            "area_of_interest": context["area_of_interest"],
            "confirmation_needed": context["confirmation_needed"],
            "reversal_risk": context["reversal_risk"],
            "alignment_score": alignment_score,
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
        "week_key": _week_key(),
        "day_key": _today_key(),
        "tp1_hit": False,
        "tp1_hit_at": None,
        "tp1_price": None,
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
    elif outcome == "TP1_ONLY":
        RISK_STATE["daily_R"] += float(trade.get("tp1_r_multiple") or TRADE_A_SIZE)
    elif outcome == "BE":
        RISK_STATE["daily_R"] += float(trade.get("tp1_r_multiple") or TRADE_A_SIZE)

    idx = _find_history_index(trade["trade_id"])
    if idx is not None:
        TRADE_HISTORY[idx] = {**TRADE_HISTORY[idx], **trade}

    await maybe_send_telegram(
        key=f"closed:{symbol}:{timeframe}:{trade['trade_id']}:{outcome}",
        min_gap_sec=5,
        text=build_closed_message(symbol, timeframe, trade, outcome, price),
    )
    return trade


async def manage_active_trade(
    symbol: str,
    timeframe: str,
    candles_tf: List[Dict[str, float]],
    trade: Dict[str, Any],
) -> Dict[str, Any]:
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
            trade["progress_pct"] = round(
                trade_progress_percent_fn(direction, entry, sl, tp2, last_close), 2
            )
        return {"signal": trade, "active": True, "actions": actions}

    if direction == "BUY":
        if tp1 and not trade.get("tp1_hit", False) and last_high >= tp1:
            trade["tp1_hit"] = True
            trade["tp1_hit_at"] = _now_ts()
            trade["tp1_price"] = round(tp1, 5)
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
            return {
                "closed_trade": closed,
                "signal": {"direction": "HOLD", "confidence": 0, "reason": "trade_closed_tp2"},
                "active": False,
                "actions": actions,
            }

        if last_low <= sl:
            if trade.get("tp1_hit"):
                outcome = "TP1_ONLY" if COUNT_TP1_AS_WIN else "BE"
            else:
                outcome = "SL"
            closed = await close_trade(symbol, timeframe, trade, outcome, sl)
            return {
                "closed_trade": closed,
                "signal": {"direction": "HOLD", "confidence": 0, "reason": f"trade_closed_{outcome.lower()}"},
                "active": False,
                "actions": actions,
            }

    else:
        if tp1 and not trade.get("tp1_hit", False) and last_low <= tp1:
            trade["tp1_hit"] = True
            trade["tp1_hit_at"] = _now_ts()
            trade["tp1_price"] = round(tp1, 5)
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
            return {
                "closed_trade": closed,
                "signal": {"direction": "HOLD", "confidence": 0, "reason": "trade_closed_tp2"},
                "active": False,
                "actions": actions,
            }

        if last_high >= sl:
            if trade.get("tp1_hit"):
                outcome = "TP1_ONLY" if COUNT_TP1_AS_WIN else "BE"
            else:
                outcome = "SL"
            closed = await close_trade(symbol, timeframe, trade, outcome, sl)
            return {
                "closed_trade": closed,
                "signal": {"direction": "HOLD", "confidence": 0, "reason": f"trade_closed_{outcome.lower()}"},
                "active": False,
                "actions": actions,
            }

    if TRAIL_AFTER_TP1 and trade.get("tp1_hit"):
        atr_value = float(atr_last(closed_candles(candles_tf), ATR_PERIOD) or 0.0)
        if atr_value > 0:
            trail = trail_stop_suggestion(closed_candles(candles_tf), direction, atr_value)
            if direction == "BUY":
                trade["sl"] = max(safe_float(trade["sl"]), trail)
            else:
                trade["sl"] = min(safe_float(trade["sl"]), trail)
            actions.append({"type": "TRAIL_SUGGESTION", "to": round(trade["sl"], 5)})

    progress_pct = trade_progress_percent_fn(direction, entry, safe_float(trade["sl"]), tp2, last_close)
    trade["progress_pct"] = round(progress_pct, 2)

    return {"signal": trade, "active": True, "actions": actions}


# =========================================================
# TELEGRAM SMART MARKET STATE
# =========================================================
async def maybe_emit_market_messages(symbol: str, timeframe: str, context: Dict[str, Any], signal: Dict[str, Any]) -> None:
    state = "INFO"
    if signal.get("direction") in ("BUY", "SELL"):
        state = "READY"
    else:
        market_state = context.get("market_state", "")
        if (
            context.get("bias") in ("UP", "DOWN")
            and context.get("area_of_interest") != "-"
            and market_state not in ("CHOPPY / NO-TRADE",)
        ):
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
async def analyze_market(req: AnalyzeRequest, manage_trade: bool = True, emit_telegram: bool = True):
    _reset_daily_if_needed()

    symbol = req.symbol.strip()
    timeframe = _requested_chart_tf(req.timeframe or ENTRY_TF)
    key = _trade_key(symbol, timeframe)
    app_id = os.getenv("DERIV_APP_ID", "1089")

    candles_entry = await _cached_fetch_candles(app_id, symbol, timeframe, 320)
    candles_15m, _ = await _fetch_first_supported_timeframe(app_id, symbol, HTF_CANDIDATES["m15"], 320)
    candles_1h, _ = await _fetch_first_supported_timeframe(app_id, symbol, HTF_CANDIDATES["h1"], 320)
    candles_daily, _ = await _fetch_first_supported_timeframe(app_id, symbol, HTF_CANDIDATES["daily"], 260)
    candles_weekly, _ = await _fetch_first_supported_timeframe(app_id, symbol, HTF_CANDIDATES["weekly"], 180)
    candles_monthly, _ = await _fetch_first_supported_timeframe(app_id, symbol, HTF_CANDIDATES["monthly"], 180)
    candles_1m, _ = await _fetch_first_supported_timeframe(app_id, symbol, HTF_CANDIDATES["m1"], 220)

    if not candles_entry:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": None,
            "candles": [],
            "levels": {"supports": [], "resistances": []},
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "no_candles"},
            "briefing": {},
            "live_tracker": None,
            "daily_outlook": None,
            "weekly_outlook": _weekly_performance(),
            "active": False,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
            "max_active_per_symbol": MAX_ACTIVE_PER_SYMBOL,
        }

    entry_data = closed_candles(candles_entry)
    tf15_data = closed_candles(candles_15m)
    tf1h_data = closed_candles(candles_1h)
    daily_data = closed_candles(candles_daily)
    weekly_data = closed_candles(candles_weekly)
    monthly_data = closed_candles(candles_monthly)
    fine_data = closed_candles(candles_1m)

    last_live = candles_entry[-1]
    price = safe_float(last_live["close"])

    context = build_market_context(
        entry_data,
        tf15_data,
        tf1h_data,
        daily_data,
        weekly_data,
        monthly_data,
        fine_data,
    )
    zones = context["zones"]
    levels = {
        "supports": [z["level"] for z in zones.get("supports", [])[:6]],
        "resistances": [z["level"] for z in zones.get("resistances", [])[:6]],
    }

    if key in ACTIVE_TRADES:
        if manage_trade:
            managed = await manage_active_trade(symbol, timeframe, candles_entry, ACTIVE_TRADES[key])
            tracker_source = managed.get("signal", {})
            live_tracker = build_live_trade_tracker(tracker_source, price) if tracker_source else None

            result = {
                "symbol": symbol,
                "timeframe": timeframe,
                "price": round(price, 5),
                "candles": candles_entry,
                "levels": levels,
                "briefing": context,
                "signal": managed.get("signal", {"direction": "HOLD", "confidence": 0}),
                "live_tracker": live_tracker,
                "daily_outlook": None,
                "weekly_outlook": _weekly_performance(),
                "active": managed.get("active", False),
                "actions": managed.get("actions", []),
                "daily_R": RISK_STATE["daily_R"],
                "active_total": _active_total(),
                "max_active_total": MAX_ACTIVE_TOTAL,
                "max_active_per_symbol": MAX_ACTIVE_PER_SYMBOL,
            }
            if managed.get("closed_trade"):
                result["closed_trade"] = managed["closed_trade"]
            return result

        active_trade = ACTIVE_TRADES[key]
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_entry,
            "levels": levels,
            "briefing": context,
            "signal": active_trade,
            "live_tracker": build_live_trade_tracker(active_trade, price),
            "daily_outlook": None,
            "weekly_outlook": _weekly_performance(),
            "active": True,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
            "max_active_per_symbol": MAX_ACTIVE_PER_SYMBOL,
        }

    hold_reason = ""
    if _active_total() >= MAX_ACTIVE_TOTAL:
        hold_reason = "max_active_trades"
    elif _active_symbol_count(symbol) >= MAX_ACTIVE_PER_SYMBOL:
        hold_reason = "max_active_per_symbol"
    elif _now_ts() < int(RISK_STATE.get("cooldown_until", {}).get(symbol, 0)):
        hold_reason = "cooldown_active"
    elif (_now_ts() - int(LAST_SIGNAL_TS.get(symbol, 0))) < MIN_SIGNAL_GAP_SEC:
        hold_reason = "signal_gap_active"

    setup = detect_trade_setup(entry_data, context)
    signal = build_signal_from_setup(setup, context)

    if hold_reason:
        signal = {"direction": "HOLD", "confidence": 0, "reason": hold_reason}
    elif signal.get("direction") in ("BUY", "SELL") and int(signal.get("confidence", 0)) < MIN_CONFIDENCE_TO_OPEN:
        signal = {
            "direction": "HOLD",
            "confidence": int(signal.get("confidence", 0)),
            "reason": f"below_min_confidence_{MIN_CONFIDENCE_TO_OPEN}",
            "entry": signal.get("entry"),
            "sl": signal.get("sl"),
            "tp": signal.get("tp"),
            "tp1": signal.get("tp1"),
            "tp2": signal.get("tp2"),
            "entry_type": signal.get("entry_type"),
            "mode": signal.get("mode"),
            "quality_score": signal.get("quality_score"),
            "quality_grade": signal.get("quality_grade"),
            "quality_stars": signal.get("quality_stars"),
        }

    if emit_telegram:
        await maybe_emit_market_messages(symbol, timeframe, context, signal)

    if manage_trade and signal.get("direction") in ("BUY", "SELL"):
        opened = await open_new_trade(symbol, timeframe, signal)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "candles": candles_entry,
            "levels": levels,
            "briefing": context,
            "signal": opened,
            "live_tracker": build_live_trade_tracker(opened, price),
            "daily_outlook": None,
            "weekly_outlook": _weekly_performance(),
            "active": True,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
            "max_active_per_symbol": MAX_ACTIVE_PER_SYMBOL,
        }

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "price": round(price, 5),
        "candles": candles_entry,
        "levels": levels,
        "briefing": context,
        "signal": signal,
        "live_tracker": None,
        "daily_outlook": None,
        "weekly_outlook": _weekly_performance(),
        "active": False,
        "actions": [],
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
        "max_active_total": MAX_ACTIVE_TOTAL,
        "max_active_per_symbol": MAX_ACTIVE_PER_SYMBOL,
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
            res = await analyze_market(
                AnalyzeRequest(symbol=sym, timeframe=timeframe),
                manage_trade=False,
                emit_telegram=False,
            )
            sig = res.get("signal", {})
            brief = res.get("briefing", {})
            rows.append({
                "symbol": sym,
                "direction": sig.get("direction", "HOLD"),
                "confidence": int(sig.get("confidence", 0)),
                "quality_grade": sig.get("quality_grade"),
                "quality_stars": sig.get("quality_stars"),
                "quality_score": sig.get("quality_score"),
                "reason": sig.get("reason", ""),
                "entry": sig.get("entry"),
                "sl": sig.get("sl"),
                "tp": sig.get("tp2", sig.get("tp")),
                "entry_type": sig.get("entry_type", sig.get("mode", "")),
                "active_trade": bool(res.get("active", False)),
                "bias": brief.get("bias"),
                "monthly_bias": brief.get("monthly_bias"),
                "weekly_bias": brief.get("weekly_bias"),
                "daily_bias": brief.get("daily_bias"),
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
                "quality_grade": None,
                "quality_stars": None,
                "quality_score": None,
                "reason": str(e),
                "entry": None,
                "sl": None,
                "tp": None,
                "entry_type": "",
                "active_trade": False,
                "bias": None,
                "monthly_bias": None,
                "weekly_bias": None,
                "daily_bias": None,
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
        "daily_outlook": build_daily_market_outlook(rows),
        "weekly_outlook": _weekly_performance(),
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
        "max_active_total": MAX_ACTIVE_TOTAL,
        "max_active_per_symbol": MAX_ACTIVE_PER_SYMBOL,
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

    trade = ACTIVE_TRADES.get(_trade_key(symbol, chart_tf))
    live_tracker = None
    if trade:
        live_tracker = build_live_trade_tracker(trade, safe_float(last["close"]))

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": chart_tf,
        "price": round(safe_float(last["close"]), 5),
        "candles": candles,
        "live_tracker": live_tracker,
        "weekly_outlook": _weekly_performance(),
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
    return {
        **_performance(last_n),
        "weekly": _weekly_performance(),
    }


@router.get("/state")
async def state(limit: int = 100) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 500))

    active_trades = list(ACTIVE_TRADES.values())
    recent_history = TRADE_HISTORY[-limit:]

    return {
        "active_trades": active_trades,
        "history": recent_history,
        "performance": _performance(PERFORMANCE_REVIEW_N),
        "weekly": _weekly_performance(),
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
        "max_active_total": MAX_ACTIVE_TOTAL,
        "max_active_per_symbol": MAX_ACTIVE_PER_SYMBOL,
        "server_time": _now_ts(),
    }


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

                trade = ACTIVE_TRADES.get(_trade_key(symbol, timeframe))
                live_tracker = None
                if trade:
                    live_tracker = build_live_trade_tracker(trade, last_close)

                payload = {
                    "type": "live_chart",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "price": round(last_close, 5),
                    "live_tracker": live_tracker,
                    "weekly_outlook": _weekly_performance(),
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
                        "live_tracker": live_tracker,
                        "weekly_outlook": _weekly_performance(),
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