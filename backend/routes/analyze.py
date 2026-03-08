from __future__ import annotations

import os
import time
import uuid
import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from utils.deriv import fetch_deriv_candles
from utils.telegram import send_telegram_message
router = APIRouter()

@router.get("/telegram-test")
async def telegram_test():
    ok = await send_telegram_message("Telegram test works")
    return {"ok": ok}

@router.get("/daily-report")
async def daily_report() -> Dict[str, Any]:
    ok = await send_telegram_message(build_daily_report_message())
    return {"ok": ok}

@router.get("/telegram-performance")
async def telegram_performance() -> Dict[str, Any]:
    ok = await send_telegram_message(build_daily_report_message())
    return {"ok": ok}


@router.get("/telegram-active")
async def telegram_active() -> Dict[str, Any]:
    if not ACTIVE_TRADES:
        ok = await send_telegram_message("📭 No active trades right now.")
        return {"ok": ok}

    lines = ["📌 ACTIVE TRADES\n"]
    for _, t in ACTIVE_TRADES.items():
        lines.append(
            f"{t.get('symbol')} {t.get('direction')} | "
            f"Entry {t.get('entry')} | SL {t.get('sl')} | TP2 {t.get('tp2', t.get('tp'))}"
        )
    ok = await send_telegram_message("\n".join(lines))
    return {"ok": ok}

# =========================================================
# CONFIG
# =========================================================
ENTRY_TF = "5m"

ALIGN_TFS = ["30m", "1h", "4h"]
ALIGN_WEIGHTS = {"30m": 0.25, "1h": 0.35, "4h": 0.40}
ALIGN_MIN_SCORE = 0.60
ALIGN_MIN_MARGIN = 0.12

EMA_FAST = 20
EMA_SLOW = 50
EMA_ENTRY = 20
ATR_PERIOD = 14

USE_REGIME_FILTER = True
USE_REGIME_CLASSIFIER = True
REGIME_LOOKBACK = 60
REGIME_TREND_EMA_GAP_MIN_ATR = 0.35
REGIME_RANGE_ATR_RATIO_MAX = 0.85
REGIME_CHAOS_CROSS_COUNT = 7
EMA_CROSS_LOOKBACK = 45
EMA_CROSS_MAX = 6
ATR_COMP_LOOKBACK = 70
ATR_COMP_RATIO_MIN = 0.65

USE_CHOP_FILTER = True
TREND_STRENGTH_MIN = 0.35

MIN_BODY_TO_RANGE = 0.30
MIN_WICK_TO_RANGE = 0.25
REJECTION_CLOSE_OUTSIDE_ZONE = True

ZONE_LOOKBACK = 160
ZONE_ATR_MULT = 0.25
MIN_ZONE_TOUCHES = 2
RETURN_TOP_ZONES = 8

BOS_LOOKBACK = 12
RETEST_TOL_ATR = 0.35
RETEST_MAX_CANDLES = 18

SL_BUFFER_ATR = 0.20
MAX_SL_ATR = 2.50

MIN_TP1_ATR = 0.8
MIN_TP2_ATR = 1.5
RR_FALLBACK_TP2 = 4.0

BE_BUFFER_R = 0.10
PARTIAL_TP_PERCENT = 0.5
TRAIL_LOOKBACK = 12
TRAIL_BUFFER_ATR = 0.20

MAX_ACTIVE_TOTAL = 5
DAILY_LOSS_LIMIT_R = -3.0
COOLDOWN_MIN_AFTER_LOSS = 20
MAX_SIGNALS_PER_DAY_PER_SYMBOL = 5

ENABLE_FAST_ENTRY = True
FAST_ENTRY_MIN_CONF = 70

ENABLE_SIGNAL_LOCK = True
MIN_LOCK_CONF = 70

MAX_HISTORY = 500
PERFORMANCE_REVIEW_N = 30

USE_MARKET_QUALITY_FILTER = True
BREAKOUT_BODY_MIN_RATIO = 0.35
BREAKOUT_WICK_MAX_OPPOSITE_RATIO = 0.35
BREAKOUT_RANGE_MIN_ATR = 0.55
BREAKOUT_CLOSE_NEAR_EXTREME_RATIO = 0.35
MAX_ENTRY_DISTANCE_FROM_EMA_ATR = 1.60
MIN_TP2_R_MULT = 1.80

# Volatility filter
USE_VOLATILITY_FILTER = True
VOL_ATR_LOOKBACK = 50
VOL_MIN_RATIO = 0.6
VOL_MAX_RATIO = 1.8

# =========================================================
# NEW UPGRADE CONFIG
# =========================================================
USE_STRUCTURE_FILTER = True
STRUCTURE_LOOKBACK = 50
STRUCTURE_PIVOT_LEFT = 2
STRUCTURE_PIVOT_RIGHT = 2

USE_LIQUIDITY_CLUSTER_FILTER = True
LIQ_CLUSTER_LOOKBACK = 80
LIQ_CLUSTER_TOL_ATR = 0.20
LIQ_CLUSTER_MIN_TOUCHES = 3

USE_SPIKE_FILTER = True
SPIKE_LOOKBACK = 5
SPIKE_MAX_ATR_MULT = 2.8
USE_SWEEP_ENTRY_BONUS = True
SWEEP_ENTRY_BONUS_POINTS = 6

USE_TRAP_AVOIDANCE = True
TRAP_AVOID_TOL_ATR = 0.25

USE_LIQUIDITY_SWEEP = True
SWEEP_LOOKBACK = 16
SWEEP_CLOSEBACK_ATR = 0.10

USE_FAKE_BREAKOUT_FILTER = True
FAKE_BREAKOUT_LOOKBACK = 10
FAKE_BREAKOUT_BACKINSIDE_ATR = 0.12

LIVE_CACHE: Dict[str, Dict[str, Any]] = {}
LIVE_CACHE_TTL_SEC = 2

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
# BASIC HELPERS
# =========================================================
def classify_market_regime(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if not USE_REGIME_CLASSIFIER:
        return {"regime": "UNKNOWN", "reason": "regime_classifier_off"}

    if len(candles) < REGIME_LOOKBACK or atr_value <= 0:
        return {"regime": "UNKNOWN", "reason": "not_enough_regime_data"}

    data = candles[-REGIME_LOOKBACK:]
    closes = [safe_float(c["close"]) for c in data]

    ema_fast = ema_last(closes, EMA_FAST)
    ema_slow = ema_last(closes, EMA_SLOW)

    if ema_fast is None or ema_slow is None:
        return {"regime": "UNKNOWN", "reason": "ema_not_ready"}

    ema_gap_atr = abs(float(ema_fast) - float(ema_slow)) / max(1e-9, atr_value)

    atrs = atr_series_sma(data, ATR_PERIOD)
    recent_atrs = [a for a in atrs if a is not None]
    atr_ratio = 1.0
    if len(recent_atrs) >= 10:
        avg_atr = sum(recent_atrs) / len(recent_atrs)
        if avg_atr > 0:
            atr_ratio = atr_value / avg_atr

    ema20_series = ema_series(closes, EMA_ENTRY)
    cross_count = 0
    for i in range(1, len(closes)):
        if ema20_series[i] is None or ema20_series[i - 1] is None:
            continue
        prev_side = closes[i - 1] - float(ema20_series[i - 1])
        cur_side = closes[i] - float(ema20_series[i])
        if prev_side == 0:
            continue
        if (prev_side > 0 and cur_side < 0) or (prev_side < 0 and cur_side > 0):
            cross_count += 1

    if cross_count >= REGIME_CHAOS_CROSS_COUNT:
        return {
            "regime": "CHAOTIC",
            "reason": f"too_many_crosses({cross_count})",
            "ema_gap_atr": round(ema_gap_atr, 3),
            "atr_ratio": round(atr_ratio, 3),
        }

    if ema_gap_atr >= REGIME_TREND_EMA_GAP_MIN_ATR and atr_ratio >= REGIME_RANGE_ATR_RATIO_MAX:
        return {
            "regime": "TRENDING",
            "reason": "ema_gap_and_atr_support_trend",
            "ema_gap_atr": round(ema_gap_atr, 3),
            "atr_ratio": round(atr_ratio, 3),
        }

    return {
        "regime": "RANGING",
        "reason": "no_clean_trend_detected",
        "ema_gap_atr": round(ema_gap_atr, 3),
        "atr_ratio": round(atr_ratio, 3),
    }

def regime_allows_trade(direction: str, regime_info: Dict[str, Any]) -> Dict[str, Any]:
    regime = regime_info.get("regime", "UNKNOWN")

    if regime == "CHAOTIC":
        return {"ok": False, "reason": "chaotic_market_blocked"}

    if regime == "UNKNOWN":
        return {"ok": True, "reason": "unknown_regime_allowed"}

    if regime in ("TRENDING", "RANGING"):
        return {"ok": True, "reason": f"{regime.lower()}_allowed"}

    return {"ok": True, "reason": "default_allowed"}

def spike_filter_ok(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:

    if not USE_SPIKE_FILTER:
        return {"ok": True, "reason": "spike_filter_off"}

    if len(candles) < SPIKE_LOOKBACK:
        return {"ok": True, "reason": "not_enough_data"}

    recent = candles[-SPIKE_LOOKBACK:]

    for c in recent:
        high = float(c["high"])
        low = float(c["low"])

        candle_range = abs(high - low)

        if candle_range > atr_value * SPIKE_MAX_ATR_MULT:
            return {
                "ok": False,
                "reason": f"spike_detected ({round(candle_range,2)})"
            }

    return {"ok": True, "reason": "no_spike"}

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

    LIVE_CACHE[key] = {
        "ts": now,
        "candles": candles or [],
    }
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

def volatility_ok(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if not USE_VOLATILITY_FILTER:
        return {"ok": True, "reason": "vol_filter_off"}

    if len(candles) < VOL_ATR_LOOKBACK or atr_value <= 0:
        return {"ok": True, "reason": "not_enough_data"}

    atrs = atr_series_sma(candles, ATR_PERIOD)
    recent = [a for a in atrs[-VOL_ATR_LOOKBACK:] if a is not None]

    if len(recent) < 10:
        return {"ok": True, "reason": "not_enough_atr"}

    avg_atr = sum(recent) / len(recent)
    ratio = atr_value / avg_atr if avg_atr > 0 else 1.0

    if ratio < VOL_MIN_RATIO:
        return {"ok": False, "reason": f"volatility_too_low ({ratio:.2f})"}

    if ratio > VOL_MAX_RATIO:
        return {"ok": False, "reason": f"volatility_too_high ({ratio:.2f})"}

    return {"ok": True, "reason": "volatility_ok"}

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
        f"Direction: {trade.get('direction', '-')}\n"
        f"Mode: {trade.get('entry_type') or trade.get('mode') or '-'}\n\n"
        f"Entry: {trade.get('entry', '-')}\n"
        f"TP1: {trade.get('tp1', '-')}\n"
        f"TP2: {trade.get('tp2', trade.get('tp', '-'))}\n\n"
        f"✅ Partial profit taken\n"
        f"🔒 Stop moved to protection zone"
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
        f"R: {trade.get('r_multiple', '-')}\n\n"
        f"📊 DEXTRADEZ BOT result logged"
    )
def build_daily_report_message() -> str:
    perf = _performance(PERFORMANCE_REVIEW_N)
    return (
        f"📈 DEXTRADEZ DAILY REPORT\n\n"
        f"Daily R: {round(float(RISK_STATE.get('daily_R', 0.0)), 2)}\n"
        f"Active Trades: {_active_total()}/{MAX_ACTIVE_TOTAL}\n"
        f"Closed Trades Reviewed: {perf.get('total_closed', 0)}\n"
        f"Wins: {perf.get('wins', 0)}\n"
        f"Losses: {perf.get('losses', 0)}\n"
        f"Win Rate: {perf.get('win_rate', 0)}%\n"
        f"Avg R: {perf.get('avg_R', 0)}\n"
        f"Total R: {perf.get('total_R', 0)}"
    )

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
    return min(safe_float(c["low"]) for c in candles[-lookback:])


def recent_swing_high(candles: List[Dict[str, float]], lookback: int = 20) -> float:
    return max(safe_float(c["high"]) for c in candles[-lookback:])


def recent_avg_body(candles: List[Dict[str, float]], lookback: int = 12) -> float:
    data = candles[-lookback:] if len(candles) >= lookback else candles
    if not data:
        return 0.0
    vals = [abs(safe_float(c["close"]) - safe_float(c["open"])) for c in data]
    return sum(vals) / max(1, len(vals))


def close_near_extreme_ratio(c: Dict[str, float], direction: str) -> float:
    h = safe_float(c["high"])
    l = safe_float(c["low"])
    cl = safe_float(c["close"])
    rng = max(1e-9, h - l)
    if direction == "BUY":
        return max(0.0, (h - cl) / rng)
    return max(0.0, (cl - l) / rng)


def opposite_wick_ratio(c: Dict[str, float], direction: str) -> float:
    o = safe_float(c["open"])
    cl = safe_float(c["close"])
    h = safe_float(c["high"])
    l = safe_float(c["low"])
    rng = max(1e-9, h - l)
    if direction == "BUY":
        upper = h - max(o, cl)
        return max(0.0, upper / rng)
    lower = min(o, cl) - l
    return max(0.0, lower / rng)


# =========================================================
# STRUCTURE / SWEEPS / FAKE BREAKOUTS
# =========================================================
def _pivot_high_values(
    candles: List[Dict[str, float]],
    pivot_left: int = STRUCTURE_PIVOT_LEFT,
    pivot_right: int = STRUCTURE_PIVOT_RIGHT,
) -> List[float]:
    highs = [safe_float(c["high"]) for c in candles]
    vals: List[float] = []
    for i in range(pivot_left, len(candles) - pivot_right):
        w = highs[i - pivot_left : i + pivot_right + 1]
        if highs[i] == max(w):
            vals.append(highs[i])
    return vals


def _pivot_low_values(
    candles: List[Dict[str, float]],
    pivot_left: int = STRUCTURE_PIVOT_LEFT,
    pivot_right: int = STRUCTURE_PIVOT_RIGHT,
) -> List[float]:
    lows = [safe_float(c["low"]) for c in candles]
    vals: List[float] = []
    for i in range(pivot_left, len(candles) - pivot_right):
        w = lows[i - pivot_left : i + pivot_right + 1]
        if lows[i] == min(w):
            vals.append(lows[i])
    return vals


def market_structure_state(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    if len(candles) < max(STRUCTURE_LOOKBACK, 20):
        return {"direction": "NEUTRAL", "reason": "not_enough_structure_data"}

    data = candles[-STRUCTURE_LOOKBACK:]
    highs = _pivot_high_values(data)
    lows = _pivot_low_values(data)

    if len(highs) < 2 or len(lows) < 2:
        return {"direction": "NEUTRAL", "reason": "not_enough_pivots"}

    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]

    if h2 > h1 and l2 > l1:
        return {
            "direction": "BUY",
            "reason": "HH_HL_structure",
            "last_high_1": round(h1, 5),
            "last_high_2": round(h2, 5),
            "last_low_1": round(l1, 5),
            "last_low_2": round(l2, 5),
        }

    if h2 < h1 and l2 < l1:
        return {
            "direction": "SELL",
            "reason": "LH_LL_structure",
            "last_high_1": round(h1, 5),
            "last_high_2": round(h2, 5),
            "last_low_1": round(l1, 5),
            "last_low_2": round(l2, 5),
        }

    return {
        "direction": "NEUTRAL",
        "reason": "mixed_structure",
        "last_high_1": round(h1, 5),
        "last_high_2": round(h2, 5),
        "last_low_1": round(l1, 5),
        "last_low_2": round(l2, 5),
    }


def liquidity_sweep_info(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if not USE_LIQUIDITY_SWEEP:
        return {"type": "NONE", "reason": "sweep_filter_off"}

    if len(candles) < SWEEP_LOOKBACK + 2 or atr_value <= 0:
        return {"type": "NONE", "reason": "not_enough_sweep_data"}

    last = candles[-1]
    recent = candles[-(SWEEP_LOOKBACK + 1) : -1]

    prev_high = max(safe_float(c["high"]) for c in recent)
    prev_low = min(safe_float(c["low"]) for c in recent)

    last_high = safe_float(last["high"])
    last_low = safe_float(last["low"])
    last_close = safe_float(last["close"])

    if last_low < prev_low and last_close >= prev_low + atr_value * SWEEP_CLOSEBACK_ATR:
        return {
            "type": "BULLISH_SWEEP",
            "level": round(prev_low, 5),
            "reason": "swept_lows_closed_back_above",
        }

    if last_high > prev_high and last_close <= prev_high - atr_value * SWEEP_CLOSEBACK_ATR:
        return {
            "type": "BEARISH_SWEEP",
            "level": round(prev_high, 5),
            "reason": "swept_highs_closed_back_below",
        }

    return {"type": "NONE", "reason": "no_sweep"}


def fake_breakout_block(candles: List[Dict[str, float]], direction: str, atr_value: float) -> Dict[str, Any]:
    if not USE_FAKE_BREAKOUT_FILTER:
        return {"ok": True, "reason": "fake_breakout_filter_off"}

    if len(candles) < FAKE_BREAKOUT_LOOKBACK + 2 or atr_value <= 0:
        return {"ok": True, "reason": "not_enough_fake_breakout_data"}

    last = candles[-1]
    recent = candles[-(FAKE_BREAKOUT_LOOKBACK + 1) : -1]

    recent_high = max(safe_float(c["high"]) for c in recent)
    recent_low = min(safe_float(c["low"]) for c in recent)
    close_ = safe_float(last["close"])

    if direction == "BUY":
        broke = safe_float(last["high"]) > recent_high
        back_inside = close_ < recent_high - atr_value * FAKE_BREAKOUT_BACKINSIDE_ATR
        weak_body = candle_body_ratio(last) < BREAKOUT_BODY_MIN_RATIO
        if broke and (back_inside or weak_body):
            return {"ok": False, "reason": "fake_breakout_buy"}
        return {"ok": True, "reason": "breakout_ok_buy"}

    if direction == "SELL":
        broke = safe_float(last["low"]) < recent_low
        back_inside = close_ > recent_low + atr_value * FAKE_BREAKOUT_BACKINSIDE_ATR
        weak_body = candle_body_ratio(last) < BREAKOUT_BODY_MIN_RATIO
        if broke and (back_inside or weak_body):
            return {"ok": False, "reason": "fake_breakout_sell"}
        return {"ok": True, "reason": "breakout_ok_sell"}

    return {"ok": True, "reason": "no_direction"}


def structure_matches(direction: str, structure: Dict[str, Any], sweep: Dict[str, Any]) -> bool:
    sdir = structure.get("direction", "NEUTRAL")
    stype = sweep.get("type", "NONE")

    if direction == "BUY":
        return sdir in ("BUY", "NEUTRAL") or stype == "BULLISH_SWEEP"
    if direction == "SELL":
        return sdir in ("SELL", "NEUTRAL") or stype == "BEARISH_SWEEP"
    return False


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
    start = len(candles_5m) - EMA_CROSS_LOOKBACK

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
        return {"ok": False, "reason": f"regime_block_chop (ema_crosses={crosses})"}

    atrs = atr_series_sma(candles_5m, ATR_PERIOD)
    recent_atrs = [a for a in atrs[-ATR_COMP_LOOKBACK:] if a is not None]
    if len(recent_atrs) >= 20:
        cur_atr = float(recent_atrs[-1])
        avg_atr = sum(recent_atrs) / len(recent_atrs)
        if avg_atr > 0 and (cur_atr / avg_atr) < ATR_COMP_RATIO_MIN:
            return {"ok": False, "reason": f"regime_block_compress (atr_ratio={cur_atr/avg_atr:.2f})"}

    return {"ok": True, "reason": "regime_ok"}


# =========================================================
# WEIGHTED ALIGNMENT
# =========================================================
def _tf_bias(candles: List[Dict[str, float]]) -> Dict[str, Any]:
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
        return {"direction": "HOLD", "reason": f"Choppy (trend_strength {ts:.2f})", "meta": {"trend_strength": round(ts, 3)}}

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
        ts01 = min(1.0, max(0.0, ts / 1.0))

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
            "reason": "Alignment unclear",
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
# TARGETS
# =========================================================
def pick_tp1_tp2(direction: str, entry: float, atr_5m: float, zones: Dict[str, Any], risk: float) -> Dict[str, Optional[float]]:
    min_tp1 = atr_5m * MIN_TP1_ATR
    min_tp2 = atr_5m * MIN_TP2_ATR

    tp1: Optional[float] = None
    tp2: Optional[float] = None

    if direction == "BUY":
        res_list = zones.get("resistance_zones") or []

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
# QUALITY
# =========================================================
def market_quality_ok(
    candles_5m: List[Dict[str, float]],
    direction: str,
    atr_5m: float,
    entry: float,
    tp2: float,
    sl: float,
) -> Dict[str, Any]:
    if not USE_MARKET_QUALITY_FILTER:
        return {"ok": True, "reason": "market_quality_filter_off"}

    if len(candles_5m) < 25 or atr_5m <= 0:
        return {"ok": True, "reason": "market_quality_not_enough_data"}

    last = candles_5m[-1]
    closes = [safe_float(c["close"]) for c in candles_5m]
    ema20 = ema_last(closes, EMA_ENTRY)
    if ema20 is None:
        return {"ok": True, "reason": "market_quality_no_ema"}

    body_ratio = candle_body_ratio(last)
    opp_wick = opposite_wick_ratio(last, direction)
    last_range = max(1e-9, safe_float(last["high"]) - safe_float(last["low"]))
    range_atr = last_range / max(1e-9, atr_5m)
    near_extreme = close_near_extreme_ratio(last, direction)
    avg_body = recent_avg_body(candles_5m[:-1], 12)
    cur_body = abs(safe_float(last["close"]) - safe_float(last["open"]))
    body_vs_recent = (cur_body / max(1e-9, avg_body)) if avg_body > 0 else 1.0
    entry_dist_atr = abs(entry - float(ema20)) / max(1e-9, atr_5m)
    r_mult = _calc_r_multiple(direction, entry, sl, tp2)

    reasons = []

    if body_ratio < BREAKOUT_BODY_MIN_RATIO:
        reasons.append(f"weak_body({body_ratio:.2f})")
    if opp_wick > BREAKOUT_WICK_MAX_OPPOSITE_RATIO:
        reasons.append(f"opposite_wick_too_big({opp_wick:.2f})")
    if range_atr < BREAKOUT_RANGE_MIN_ATR:
        reasons.append(f"small_range_vs_atr({range_atr:.2f})")
    if near_extreme > BREAKOUT_CLOSE_NEAR_EXTREME_RATIO:
        reasons.append(f"close_not_near_extreme({near_extreme:.2f})")
    if body_vs_recent < 0.90:
        reasons.append(f"body_weaker_than_recent({body_vs_recent:.2f})")
    if entry_dist_atr > MAX_ENTRY_DISTANCE_FROM_EMA_ATR:
        reasons.append(f"stretched_from_ema({entry_dist_atr:.2f})")
    if r_mult < MIN_TP2_R_MULT:
        reasons.append(f"tp2_rr_too_low({r_mult:.2f})")

    return {
        "ok": len(reasons) == 0,
        "reason": "market_quality_ok" if len(reasons) == 0 else "; ".join(reasons),
    }


# =========================================================
# SCORING
# =========================================================
def confidence_score(
    align_score: float,
    zone_touches: int,
    rej_ok: bool,
    bos_ok: bool,
    tp1: Optional[float],
    entry: float,
    atr_5m: float,
    structure_bonus: int = 0,
    sweep_bonus: int = 0,
) -> int:
    pts = 0.0

    pts += max(0.0, min(35.0, float(align_score) * 35.0))

    if zone_touches >= 4:
        pts += 20
    elif zone_touches == 3:
        pts += 16
    elif zone_touches == 2:
        pts += 12
    else:
        pts += 6

    pts += 20 if rej_ok else 0
    pts += 15 if bos_ok else 6

    if tp1 is not None:
        dist = abs(tp1 - entry)
        pts += 5 if dist >= atr_5m * MIN_TP1_ATR else 2

    pts += structure_bonus
    pts += sweep_bonus

    return int(max(0, min(98, round(pts))))


def setup_rank_1_10(
    confidence: int,
    bos_ok: bool,
    rej_ok: bool,
    align_score: float,
    market_quality_ok_flag: bool,
    structure_match: bool,
    sweep_type: str,
    fake_breakout_ok: bool,
) -> int:
    rank = 3
    if confidence >= 55:
        rank = 5
    if confidence >= 65:
        rank = 6
    if confidence >= 75:
        rank = 7
    if confidence >= 82:
        rank = 8
    if confidence >= 90:
        rank = 9

    if bos_ok:
        rank = max(rank, 7)
    if rej_ok:
        rank = min(10, rank + 1)
    if align_score >= 0.80:
        rank = min(10, rank + 1)
    if structure_match:
        rank = min(10, rank + 1)
    if sweep_type in ("BULLISH_SWEEP", "BEARISH_SWEEP"):
        rank = min(10, rank + 1)

    if not fake_breakout_ok:
        rank = min(rank, 4)
    if not market_quality_ok_flag:
        rank = min(rank, 4)

    return max(1, min(10, rank))

def detect_liquidity_clusters(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if not USE_LIQUIDITY_CLUSTER_FILTER:
        return {
            "high_clusters": [],
            "low_clusters": [],
            "nearest_high_cluster": None,
            "nearest_low_cluster": None,
        }

    if len(candles) < LIQ_CLUSTER_LOOKBACK or atr_value <= 0:
        return {
            "high_clusters": [],
            "low_clusters": [],
            "nearest_high_cluster": None,
            "nearest_low_cluster": None,
        }

    data = candles[-LIQ_CLUSTER_LOOKBACK:]
    tol = max(1e-9, atr_value * LIQ_CLUSTER_TOL_ATR)

    highs = sorted(safe_float(c["high"]) for c in data)
    lows = sorted(safe_float(c["low"]) for c in data)

    def cluster_levels(levels: List[float]) -> List[Dict[str, Any]]:
        if not levels:
            return []
        groups: List[List[float]] = []
        cur = [levels[0]]
        for p in levels[1:]:
            if abs(p - cur[-1]) <= tol:
                cur.append(p)
            else:
                groups.append(cur)
                cur = [p]
        groups.append(cur)

        out: List[Dict[str, Any]] = []
        for g in groups:
            if len(g) >= LIQ_CLUSTER_MIN_TOUCHES:
                out.append({
                    "level": round(sum(g) / len(g), 5),
                    "touches": len(g),
                    "low": round(min(g), 5),
                    "high": round(max(g), 5),
                })
        return out

    high_clusters = cluster_levels(highs)
    low_clusters = cluster_levels(lows)

    last_close = safe_float(data[-1]["close"])

    nearest_high = None
    nearest_low = None

    above = [c for c in high_clusters if c["level"] >= last_close]
    below = [c for c in low_clusters if c["level"] <= last_close]

    if above:
        nearest_high = min(above, key=lambda x: abs(x["level"] - last_close))
    if below:
        nearest_low = min(below, key=lambda x: abs(x["level"] - last_close))

    return {
        "high_clusters": high_clusters,
        "low_clusters": low_clusters,
        "nearest_high_cluster": nearest_high,
        "nearest_low_cluster": nearest_low,
    }


def trap_avoidance_ok(
    direction: str,
    entry: float,
    atr_value: float,
    liq: Dict[str, Any],
) -> Dict[str, Any]:
    if not USE_TRAP_AVOIDANCE:
        return {"ok": True, "reason": "trap_avoidance_off"}

    tol = atr_value * TRAP_AVOID_TOL_ATR

    nearest_high = liq.get("nearest_high_cluster")
    nearest_low = liq.get("nearest_low_cluster")

    if direction == "BUY" and nearest_high:
        if abs(float(nearest_high["level"]) - entry) <= tol:
            return {
                "ok": False,
                "reason": f"buying_into_high_liquidity_cluster({nearest_high['level']})",
            }

    if direction == "SELL" and nearest_low:
        if abs(entry - float(nearest_low["level"])) <= tol:
            return {
                "ok": False,
                "reason": f"selling_into_low_liquidity_cluster({nearest_low['level']})",
            }

    return {"ok": True, "reason": "trap_avoidance_ok"}


def liquidity_bonus_points(direction: str, sweep: Dict[str, Any], liq: Dict[str, Any]) -> int:
    if not USE_SWEEP_ENTRY_BONUS:
        return 0

    stype = sweep.get("type", "NONE")

    if direction == "BUY" and stype == "BULLISH_SWEEP":
        return SWEEP_ENTRY_BONUS_POINTS

    if direction == "SELL" and stype == "BEARISH_SWEEP":
        return SWEEP_ENTRY_BONUS_POINTS

    return 0

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
    vol_check = volatility_ok(candles_5m, atr_5m)
    if not vol_check["ok"]:
        return {
        "direction": "HOLD",
        "confidence": 0,
        "reason": f"Volatility blocked: {vol_check['reason']}",
        "score_1_10": 4,
        "grade": "IGNORE",
    }
    regime_info = classify_market_regime(candles_5m, atr_5m)
    regime_ok = regime_allows_trade(direction, regime_info)

    if not regime_ok["ok"]:
        return {
        "direction": "HOLD",
        "confidence": 0,
        "reason": f"Regime blocked: {regime_ok['reason']}",
        "score_1_10": 4,
        "grade": "IGNORE",
    }

    spike_check = spike_filter_ok(candles_5m, atr_5m)
    if not spike_check["ok"]:
        return {
        "direction": "HOLD",
        "confidence": 0,
        "reason": f"Spike filter blocked: {spike_check['reason']}",
        "score_1_10": 4,
        "grade": "IGNORE",
    }

    sup = zones.get("support_zone")
    res = zones.get("resistance_zone")

    if direction == "BUY":
        sl = (safe_float(sup["low"]) - buf) if sup else (recent_swing_low(candles_5m, 20) - buf)
        risk = entry - sl
        if risk <= 0 or risk > atr_5m * MAX_SL_ATR:
            return {"direction": "HOLD", "confidence": 0, "reason": "SL invalid"}
    else:
        sl = (safe_float(res["high"]) + buf) if res else (recent_swing_high(candles_5m, 20) + buf)
        risk = sl - entry
        if risk <= 0 or risk > atr_5m * MAX_SL_ATR:
            return {"direction": "HOLD", "confidence": 0, "reason": "SL invalid"}

    tps = pick_tp1_tp2(direction, entry, atr_5m, zones, risk=risk)
    tp1 = tps["tp1"]
    tp2 = tps["tp2"]

    if tp2 is None:
        return {"direction": "HOLD", "confidence": 0, "reason": "No TP target"}
    trap_ok = trap_avoidance_ok(direction, entry, atr_5m, liq)
    if not trap_ok["ok"]:
        return {
        "direction": "HOLD",
        "confidence": 0,
        "reason": f"Trap avoidance blocked: {trap_ok['reason']}",
        "score_1_10": 4,
        "grade": "IGNORE",
    }

    quality = market_quality_ok(
        candles_5m=candles_5m,
        direction=direction,
        atr_5m=atr_5m,
        entry=entry,
        tp2=float(tp2),
        sl=float(sl),
    )
    if not quality["ok"]:
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": f"Market quality blocked: {quality['reason']}",
            "score_1_10": 4,
            "grade": "IGNORE",
        }

    structure = market_structure_state(candles_5m)
    sweep = liquidity_sweep_info(candles_5m, atr_5m)
    fake_break = fake_breakout_block(candles_5m, direction, atr_5m)

    if USE_STRUCTURE_FILTER and not structure_matches(direction, structure, sweep):
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": f"Structure mismatch: {structure.get('reason')}",
            "score_1_10": 4,
            "grade": "IGNORE",
        }

    if not fake_break["ok"]:
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": f"Blocked by fake breakout filter: {fake_break['reason']}",
            "score_1_10": 4,
            "grade": "IGNORE",
        }
    liq = detect_liquidity_clusters(candles_5m, atr_5m)

    rej_zone = sup if direction == "BUY" else res
    rej_ok = rejection_ok(last, direction, rej_zone) if rej_zone else False
    bos_ok = bos_confirm(candles_5m, direction, BOS_LOOKBACK)
    touches = int(sup["touches"]) if (direction == "BUY" and sup) else int(res["touches"]) if (direction == "SELL" and res) else 0

    structure_bonus = 8 if structure_matches(direction, structure, sweep) else 0
    
    sweep_bonus = liquidity_bonus_points(direction, sweep, liq)
    conf = confidence_score(
        align_score=align_score, 
        zone_touches=touches,
        rej_ok=rej_ok,
        bos_ok=bos_ok,
        tp1=tp1,
        entry=entry,
        atr_5m=atr_5m,
        structure_bonus=structure_bonus,
        sweep_bonus=sweep_bonus,
    )

    rank = setup_rank_1_10(
        confidence=conf,
        bos_ok=bos_ok,
        rej_ok=rej_ok,
        align_score=align_score,
        market_quality_ok_flag=True,
        structure_match=structure_matches(direction, structure, sweep),
        sweep_type=sweep.get("type", "NONE"),
        fake_breakout_ok=fake_break.get("ok", True),
    )
    grade = score_to_grade(rank)

    return {
        "direction": direction,
        "confidence": conf,
        "score_1_10": rank,
        "grade": grade,
        "entry": round(entry, 5),
        "sl": round(float(sl), 5),
        "tp": round(float(tp2), 5),
        "tp1": round(float(tp1), 5) if tp1 is not None else None,
        "tp2": round(float(tp2), 5),
        "r_multiple": round(_calc_r_multiple(direction, entry, float(sl), float(tp2)), 3),
        "reason": "Weighted alignment + Structure + Sweep + Zone + Rejection + Targets + Quality",
        "meta": {
            "structure": structure,
            "sweep": sweep,
            "fake_breakout": fake_break,
            "atr_5m": round(atr_5m, 6),
            "align_score": round(float(align_score), 3),
            "rejection_ok": rej_ok,
            "bos_ok": bos_ok,
            "liquidity_clusters": liq,
            "regime": regime_info,
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
# LOCK HELPER
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
        "progress_pct": 0.0,
        "meta": {**(stored.get("meta") or {}), "regime": reg_reason, "bias": bias_info},
    })
    ACTIVE_TRADES[_trade_key(symbol, timeframe)] = stored
    RISK_STATE["signals_today"][symbol] = int(RISK_STATE["signals_today"].get(symbol, 0)) + 1
    _push_history(dict(stored))
    return stored


# =========================================================
# PERFORMANCE
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
    }


@router.get("/history")
async def history(symbol: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    items = TRADE_HISTORY
    if symbol:
        items = [t for t in items if t.get("symbol") == symbol.strip()]
    return {"count": len(items[-limit:]), "items": items[-limit:]}


@router.get("/performance")
async def performance(last_n: int = PERFORMANCE_REVIEW_N) -> Dict[str, Any]:
    return _performance(last_n)


@router.get("/telegram-test")
async def telegram_test() -> Dict[str, Any]:
    ok = await send_telegram_message("✅ DEXTRADEZ BOT Telegram test works")
    return {"ok": ok}


# =========================================================
# LIVE ROUTE
# =========================================================
@router.get("/live")
async def live_market(symbol: str = "R_10", timeframe: str = "5m") -> Dict[str, Any]:
    app_id = os.getenv("DERIV_APP_ID", "1089")

    candles = await _cached_fetch_candles(
        app_id=app_id,
        symbol=symbol,
        timeframe=timeframe,
        count=260,
    )

    if not candles:
        return {"ok": False, "error": "No candles returned", "symbol": symbol, "timeframe": timeframe}

    last = candles[-1]
    last_close = safe_float(last["close"])

    a = float(atr_last(candles, ATR_PERIOD) or 0.0)
    zones = strongest_zones(candles, atr_value=a) if a > 0 else {
        "support_zone": None,
        "resistance_zone": None,
        "support_zones": [],
        "resistance_zones": [],
    }

    levels = {
        "support_zone": zones.get("support_zone"),
        "resistance_zone": zones.get("resistance_zone"),
        "supports": [zones["support_zone"]["mid"]] if zones.get("support_zone") else [],
        "resistances": [zones["resistance_zone"]["mid"]] if zones.get("resistance_zone") else [],
    }

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "price": round(last_close, 5),
        "candles": candles,
        "levels": levels,
        "updated_at": _now_ts(),
    }


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
        timeframe = (msg.get("timeframe") or ENTRY_TF).strip()

        app_id = os.getenv("DERIV_APP_ID", "1089")
        last_sent_candle_time = None

        while True:
            candles = await _cached_fetch_candles(
                app_id=app_id,
                symbol=symbol,
                timeframe=timeframe,
                count=260,
            )

            if candles:
                last = candles[-1]
                last_close = safe_float(last["close"])
                last_time = last.get("time") or last.get("epoch") or last.get("t")

                a = float(atr_last(candles, ATR_PERIOD) or 0.0)
                zones = strongest_zones(candles, atr_value=a) if a > 0 else {
                    "support_zone": None,
                    "resistance_zone": None,
                    "support_zones": [],
                    "resistance_zones": [],
                }

                levels = {
                    "support_zone": zones.get("support_zone"),
                    "resistance_zone": zones.get("resistance_zone"),
                    "supports": [zones["support_zone"]["mid"]] if zones.get("support_zone") else [],
                    "resistances": [zones["resistance_zone"]["mid"]] if zones.get("resistance_zone") else [],
                }

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
        print("WebSocket disconnected")
    except Exception as e:
        print("WebSocket error:", str(e))
        try:
            await ws.close()
        except Exception:
            pass


# =========================================================
# SCAN
# =========================================================
@router.post("/scan")
async def scan_markets(payload: ScanRequest) -> Dict[str, Any]:
    _reset_daily_if_needed()
    app_id = os.getenv("DERIV_APP_ID", "1089")
    rows: List[Dict[str, Any]] = []

    for sym in payload.symbols:
        key = _trade_key(sym, ENTRY_TF)

        candles_5m, candles_30m, candles_1h, candles_4h = await asyncio.gather(
            fetch_deriv_candles(app_id=app_id, symbol=sym, timeframe=ENTRY_TF, count=260),
            fetch_deriv_candles(app_id=app_id, symbol=sym, timeframe="30m", count=220),
            fetch_deriv_candles(app_id=app_id, symbol=sym, timeframe="1h", count=220),
            fetch_deriv_candles(app_id=app_id, symbol=sym, timeframe="4h", count=220),
        )

        if not candles_5m or not candles_30m or not candles_1h or not candles_4h:
            rows.append({
                "symbol": sym,
                "score_1_10": 1,
                "grade": "IGNORE",
                "direction": "HOLD",
                "confidence": 0,
                "reason": "no_data",
            })
            continue

        a5 = float(atr_last(candles_5m, ATR_PERIOD) or 0.0)
        zones = strongest_zones(candles_5m, atr_value=a5)

        reg = regime_ok(candles_5m)
        bias = weighted_alignment({"30m": candles_30m, "1h": candles_1h, "4h": candles_4h})

        direction = bias.get("direction", "HOLD")
        base_reason = bias.get("reason", "")

        row: Dict[str, Any] = {
            "symbol": sym,
            "score_1_10": 2,
            "grade": "IGNORE",
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

        if not reg["ok"]:
            row["reason"] = reg["reason"]
            rows.append(row)
            continue

        if key in ACTIVE_TRADES:
            t = ACTIVE_TRADES[key]
            row.update({
                "score_1_10": int(t.get("score_1_10", 8)),
                "grade": t.get("grade", score_to_grade(int(t.get("score_1_10", 8)))),
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
                "score_1_10": 5,
                "grade": "WATCH",
                "direction": PENDING_SETUPS[key].get("direction", "HOLD"),
                "confidence": 65,
                "reason": "Pending: BOS done, waiting retest+confirm",
                "entry_type": "SNIPER_PENDING",
            })
        elif direction in ("BUY", "SELL"):
            t = build_trade(candles_5m, direction, zones, a5, float(bias.get("score") or 0.0))
            row.update({
                "direction": t.get("direction", "HOLD"),
                "confidence": int(t.get("confidence", 0)),
                "entry": t.get("entry"),
                "sl": t.get("sl"),
                "tp": t.get("tp"),
                "tp1": t.get("tp1"),
                "tp2": t.get("tp2"),
                "reason": t.get("reason", base_reason),
                "entry_type": "SETUP" if t.get("direction") in ("BUY", "SELL") else "NONE",
                "score_1_10": int(t.get("score_1_10", 3)),
                "grade": t.get("grade", "IGNORE"),
            })

        rows.append(row)

    rows.sort(key=lambda x: (x.get("score_1_10", 0), x.get("confidence", 0)), reverse=True)

    return {
        "timeframe": ENTRY_TF,
        "daily_R": RISK_STATE["daily_R"],
        "active_total": _active_total(),
        "max_active_total": MAX_ACTIVE_TOTAL,
        "ranked": rows,
        "top": rows[0] if rows else None,
    }


# =========================================================
# ANALYZE
# =========================================================
@router.post("/analyze")
async def analyze_market(data: AnalyzeRequest) -> Dict[str, Any]:
    _reset_daily_if_needed()

    symbol = data.symbol.strip()
    timeframe = ENTRY_TF
    key = _trade_key(symbol, timeframe)

    app_id = os.getenv("DERIV_APP_ID", "1089")

    candles_5m, candles_30m, candles_1h, candles_4h = await asyncio.gather(
        fetch_deriv_candles(app_id=app_id, symbol=symbol, timeframe=timeframe, count=260),
        fetch_deriv_candles(app_id=app_id, symbol=symbol, timeframe="30m", count=220),
        fetch_deriv_candles(app_id=app_id, symbol=symbol, timeframe="1h", count=220),
        fetch_deriv_candles(app_id=app_id, symbol=symbol, timeframe="4h", count=220),
    )

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

    # =====================================================
    # ACTIVE TRADE MANAGEMENT
    # =====================================================
    if key in ACTIVE_TRADES:
        trade = ACTIVE_TRADES[key]
        direction = trade["direction"]
        sl = safe_float(trade["sl"])
        tp1 = trade.get("tp1")
        tp2 = safe_float(trade.get("tp2") or trade.get("tp"))
        entry = safe_float(trade["entry"])

        risk = abs(entry - sl) if abs(entry - sl) > 0 else 1e-9
        actions = []

        if tp1 is not None and not trade.get("tp1_hit", False):
            tp1v = safe_float(tp1)
            if hit_level(direction, last_high, last_low, tp1v, "TP1"):
                trade["tp1_hit"] = True
                await send_telegram_message(build_tp1_message(symbol, timeframe, trade))
                be_to = entry + (risk * BE_BUFFER_R) if direction == "BUY" else entry - (risk * BE_BUFFER_R)

                actions.append({"type": "TP1_HIT", "why": "TP1 hit"})
                actions.append({"type": "PARTIAL_TP", "percent": PARTIAL_TP_PERCENT, "why": "Take partial"})
                actions.append({"type": "MOVE_SL", "to": round(be_to, 5), "why": "Move SL to BE"})

        trail_price = trail_stop_suggestion(candles_5m, direction, a5)
        actions.append({"type": "TRAIL_SUGGESTION", "to": round(trail_price, 5), "why": "Trail stop suggestion"})

        progress_pct = trade_progress_percent(direction, entry, sl, tp2, last_close)

        if direction == "BUY":
            outcome = "SL" if last_low <= sl else "TP2" if last_high >= tp2 else None
        else:
            outcome = "SL" if last_high >= sl else "TP2" if last_low <= tp2 else None

        if outcome is None:
            live_signal = {
                **trade,
                "entry_type": trade.get("entry_type", "ACTIVE_MANAGEMENT"),
                "progress_pct": round(progress_pct, 1),
                "grade": trade.get("grade", score_to_grade(int(trade.get("score_1_10", 8)))),
                "score_1_10": int(trade.get("score_1_10", 8)),
            }
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "price": round(last_close, 5),
                "candles": candles_5m,
                "levels": levels,
                "active": True,
                "signal": live_signal,
                "entry_type": trade.get("entry_type", "ACTIVE_MANAGEMENT"),
                "bias": bias_dir,
                "bias_score": bias_score,
                "bias_details": bias.get("details", {}),
                "actions": actions,
                "daily_R": RISK_STATE["daily_R"],
                "active_total": _active_total(),
                "max_active_total": MAX_ACTIVE_TOTAL,
            }

        closed = dict(trade)
        closed["closed_at"] = _now_ts()
        closed["outcome"] = outcome
        closed["status"] = "CLOSED"
        closed["progress_pct"] = round(progress_pct, 1)

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

        await send_telegram_message(build_closed_message(symbol, timeframe, closed, outcome, last_close))

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
            "max_active_total": MAX_ACTIVE_TOTAL,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": f"Trade closed by {outcome}"},
            "actions": actions,
        }

    # =====================================================
    # RISK GUARDS
    # =====================================================
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
            "max_active_total": MAX_ACTIVE_TOTAL,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Daily loss limit hit"},
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
            "max_active_total": MAX_ACTIVE_TOTAL,
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
            "max_active_total": MAX_ACTIVE_TOTAL,
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
            "max_active_total": MAX_ACTIVE_TOTAL,
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "Max signals/day reached"},
            "actions": [],
        }

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
            "signal": {"direction": "HOLD", "confidence": 0, "reason": reg["reason"]},
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
            "signal": {"direction": "HOLD", "confidence": 0, "reason": bias.get("reason", "No bias")},
            "actions": [],
        }

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
            "signal": {"direction": "HOLD", "confidence": 0, "reason": "SELL bias but no resistance zone"},
            "actions": [],
        }

    # =====================================================
    # FAST ENTRY
    # =====================================================
    if ENABLE_FAST_ENTRY and key not in PENDING_SETUPS:
        t = build_trade(candles_5m, bias_dir, zones, a5, bias_score)
        rej_zone = zones.get("support_zone") if bias_dir == "BUY" else zones.get("resistance_zone")
        rej_ok = rejection_ok(last, bias_dir, rej_zone) if rej_zone else False

        if (
            t.get("direction") in ("BUY", "SELL")
            and int(t.get("confidence", 0)) >= FAST_ENTRY_MIN_CONF
            and int(t.get("score_1_10", 0)) >= 6
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
                "reason": f"{bias.get('reason','bias ok')}; FAST ENTRY",
                "progress_pct": 0.0,
                "meta": {"regime": reg.get("reason"), "bias": bias},
            })

            ACTIVE_TRADES[key] = stored
            RISK_STATE["signals_today"][symbol] = int(RISK_STATE["signals_today"].get(symbol, 0)) + 1
            _push_history(dict(stored))
            await send_telegram_message(build_signal_message(symbol, timeframe, stored))

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
                "max_active_total": MAX_ACTIVE_TOTAL,
            }

    # =====================================================
    # LOCK SIGNAL
    # =====================================================
    if ENABLE_SIGNAL_LOCK and key not in ACTIVE_TRADES:
        t = build_trade(candles_5m, bias_dir, zones, a5, bias_score)
        rej_zone = zones.get("support_zone") if bias_dir == "BUY" else zones.get("resistance_zone")
        rej_ok = rejection_ok(last, bias_dir, rej_zone) if rej_zone else False

        if (
            int(t.get("confidence", 0)) >= MIN_LOCK_CONF
            and int(t.get("score_1_10", 0)) >= 6
            and rej_ok
            and t.get("tp1") is not None
        ):
            stored = _open_locked_signal(
                symbol=symbol,
                timeframe=timeframe,
                base_reason=bias.get("reason", "bias ok"),
                bias_info=bias,
                reg_reason=reg.get("reason", "regime_ok"),
                trade_like=t,
            )
            await send_telegram_message(build_signal_message(symbol, timeframe, stored))

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
                "max_active_total": MAX_ACTIVE_TOTAL,
            }

    # =====================================================
    # PENDING SNIPER
    # =====================================================
    if key not in PENDING_SETUPS:
        if bos_confirm(candles_5m, bias_dir, BOS_LOOKBACK):
            lvl = bos_level(candles_5m, bias_dir, BOS_LOOKBACK)
            if lvl is not None:
                PENDING_SETUPS[key] = {
                    "direction": bias_dir,
                    "bos_level": float(lvl),
                    "created_idx": len(candles_5m) - 1,
                    "bias": bias,
                    "score_1_10": 5,
                    "grade": "WATCH",
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
                    "signal": {
                        "direction": "HOLD",
                        "confidence": 0,
                        "score_1_10": 5,
                        "grade": "WATCH",
                        "reason": f"{bias.get('reason','')}; BOS confirmed -> waiting retest",
                    },
                    "pending": PENDING_SETUPS[key],
                    "actions": [],
                    "daily_R": RISK_STATE["daily_R"],
                    "active_total": _active_total(),
                    "max_active_total": MAX_ACTIVE_TOTAL,
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
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "score_1_10": 4,
                "grade": "IGNORE",
                "reason": f"{bias.get('reason','')}; waiting BOS",
            },
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
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
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "score_1_10": 4,
                "grade": "IGNORE",
                "reason": "Pending expired",
            },
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
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
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "score_1_10": 5,
                "grade": "WATCH",
                "reason": f"Waiting retest near BOS {round(bos_lvl,5)}",
            },
            "pending": pending,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
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
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "score_1_10": 5,
                "grade": "WATCH",
                "reason": "Retest not inside correct zone",
            },
            "pending": pending,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
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
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "score_1_10": 5,
                "grade": "WATCH",
                "reason": "Waiting rejection candle",
            },
            "pending": pending,
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
        }

    t = build_trade(candles_5m, p_dir, zones, a5, bias_score)
    if t.get("direction") not in ("BUY", "SELL") or t.get("tp1") is None or int(t.get("score_1_10", 0)) < 6:
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
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "score_1_10": int(t.get("score_1_10", 4)),
                "grade": t.get("grade", "IGNORE"),
                "reason": t.get("reason", "Trade rejected"),
            },
            "actions": [],
            "daily_R": RISK_STATE["daily_R"],
            "active_total": _active_total(),
            "max_active_total": MAX_ACTIVE_TOTAL,
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
        "reason": f"{bias.get('reason','bias ok')}; SNIPER",
        "progress_pct": 0.0,
        "meta": {"bias": bias, "regime": reg.get("reason")},
    })

    ACTIVE_TRADES[key] = stored
    PENDING_SETUPS.pop(key, None)
    RISK_STATE["signals_today"][symbol] = int(RISK_STATE["signals_today"].get(symbol, 0)) + 1
    _push_history(dict(stored))
    await send_telegram_message(build_signal_message(symbol, timeframe, stored))

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
        "max_active_total": MAX_ACTIVE_TOTAL,
    }