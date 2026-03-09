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


# =========================================================
# CONFIG
# =========================================================
ENTRY_TF = "1m"

ALIGN_TFS = ["30m", "1h", "4h"]
ALIGN_WEIGHTS = {"30m": 0.25, "1h": 0.35, "4h": 0.40}
ALIGN_MIN_SCORE = 0.50
ALIGN_MIN_MARGIN = 0.08

EMA_FAST = 20
EMA_SLOW = 50
EMA_ENTRY = 20
ATR_PERIOD = 14

USE_REGIME_FILTER = True
USE_REGIME_CLASSIFIER = True
REGIME_LOOKBACK = 60
REGIME_TREND_EMA_GAP_MIN_ATR = 0.35
REGIME_CHAOS_CROSS_COUNT = 7
EMA_CROSS_LOOKBACK = 45
EMA_CROSS_MAX = 10

USE_CHOP_FILTER = True
TREND_STRENGTH_MIN = 0.25

MIN_BODY_TO_RANGE = 0.30
MIN_WICK_TO_RANGE = 0.25
REJECTION_CLOSE_OUTSIDE_ZONE = True

ZONE_LOOKBACK = 160
ZONE_ATR_MULT = 0.25
MIN_ZONE_TOUCHES = 2
RETURN_TOP_ZONES = 8

BOS_LOOKBACK = 12
RETEST_TOL_ATR = 0.20
RETEST_MAX_CANDLES = 8

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
FAST_ENTRY_MIN_CONF = 50

ENABLE_SIGNAL_LOCK = True
MIN_LOCK_CONF = 55

MAX_HISTORY = 500
PERFORMANCE_REVIEW_N = 30

USE_MARKET_QUALITY_FILTER = True
BREAKOUT_BODY_MIN_RATIO = 0.35
BREAKOUT_WICK_MAX_OPPOSITE_RATIO = 0.35
BREAKOUT_RANGE_MIN_ATR = 0.55
BREAKOUT_CLOSE_NEAR_EXTREME_RATIO = 0.35
MAX_ENTRY_DISTANCE_FROM_EMA_ATR = 1.60
MIN_TP2_R_MULT = 1.40

USE_VOLATILITY_FILTER = True
VOL_ATR_LOOKBACK = 50
VOL_MIN_RATIO = 0.6
VOL_MAX_RATIO = 1.8

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

ENABLE_MOMENTUM_ENTRY = True
MOMENTUM_BODY_MIN = 0.65

USE_SMART_TRAP_DETECTOR = True
TRAP_LOOKBACK = 12
TRAP_CLOSEBACK_RATIO = 0.20
TRAP_MIN_WICK_RATIO = 0.45
TRAP_BONUS_POINTS = 10

USE_SPIKE_REVERSAL_ENGINE = True
SPIKE_REVERSAL_BONUS_POINTS = 12
SPIKE_REVERSAL_WICK_MIN = 0.55
SPIKE_REVERSAL_LOOKBACK = 6

USE_LIQUIDITY_VACUUM = True
VACUUM_BODY_MIN_RATIO = 0.70
VACUUM_MAX_OPPOSITE_WICK = 0.15
VACUUM_RANGE_ATR_MIN = 1.10
VACUUM_BONUS_POINTS = 8

LIVE_CACHE_TTL_SEC = 2


# =========================================================
# MEMORY / STATE
# =========================================================
LIVE_CACHE: Dict[str, Dict[str, Any]] = {}
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
# TELEGRAM / REPORT HELPERS
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
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out

    k = 2 / (period + 1)
    ema = values[0]

    for i in range(len(values)):
        ema = values[i] * k + ema * (1 - k)
        if i >= period - 1:
            out[i] = ema

    return out


def atr_last(candles: List[Dict[str, float]], period: int = ATR_PERIOD) -> Optional[float]:
    if len(candles) < period + 1:
        return None

    trs: List[float] = []
    for i in range(1, len(candles)):
        high = safe_float(candles[i]["high"])
        low = safe_float(candles[i]["low"])
        prev_close = safe_float(candles[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    window = trs[-period:]
    return sum(window) / len(window)


def atr_series_sma(candles: List[Dict[str, float]], period: int = ATR_PERIOD) -> List[Optional[float]]:
    if len(candles) < period + 1:
        return [None] * len(candles)

    trs: List[float] = [0.0] * len(candles)
    for i in range(1, len(candles)):
        high = safe_float(candles[i]["high"])
        low = safe_float(candles[i]["low"])
        prev_close = safe_float(candles[i - 1]["close"])
        trs[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))

    out: List[Optional[float]] = [None] * len(candles)
    for i in range(period, len(candles)):
        window = trs[i - period + 1 : i + 1]
        out[i] = sum(window) / period
    return out


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


def is_bullish(candle: Dict[str, float]) -> bool:
    return float(candle["close"]) > float(candle["open"])


def is_bearish(candle: Dict[str, float]) -> bool:
    return float(candle["close"]) < float(candle["open"])


def opposite_wick_ratio(candle: Dict[str, float], direction: str) -> float:
    o = float(candle["open"])
    c = float(candle["close"])
    h = float(candle["high"])
    l = float(candle["low"])
    rng = max(1e-9, h - l)

    if direction == "BUY":
        upper_wick = h - max(o, c)
        return upper_wick / rng

    lower_wick = min(o, c) - l
    return lower_wick / rng


def close_near_extreme_ratio(candle: Dict[str, float], direction: str) -> float:
    c = float(candle["close"])
    h = float(candle["high"])
    l = float(candle["low"])
    rng = max(1e-9, h - l)

    if direction == "BUY":
        return (h - c) / rng

    return (c - l) / rng


def recent_avg_body(candles: List[Dict[str, float]], lookback: int = 12) -> float:
    if len(candles) < lookback:
        return 0.0

    bodies = [abs(float(c["close"]) - float(c["open"])) for c in candles[-lookback:]]
    return sum(bodies) / len(bodies)


def recent_swing_low(candles: List[Dict[str, float]], lookback: int = 20) -> float:
    return min(safe_float(c["low"]) for c in candles[-lookback:])


def recent_swing_high(candles: List[Dict[str, float]], lookback: int = 20) -> float:
    return max(safe_float(c["high"]) for c in candles[-lookback:])
# =========================================================
# VOLATILITY / SPIKE / MOMENTUM
# =========================================================
def spike_filter_ok(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if not USE_SPIKE_FILTER:
        return {"ok": True}

    if len(candles) < SPIKE_LOOKBACK:
        return {"ok": True}

    recent = candles[-SPIKE_LOOKBACK:]
    for c in recent:
        rng = float(c["high"]) - float(c["low"])
        if rng > atr_value * SPIKE_MAX_ATR_MULT:
            return {"ok": False, "reason": "Spike candle detected"}

    return {"ok": True}


def volatility_ok(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if not USE_VOLATILITY_FILTER:
        return {"ok": True, "reason": "vol_filter_off"}

    if len(candles) < VOL_ATR_LOOKBACK or atr_value <= 0:
        return {"ok": True, "reason": "not_enough_data"}

    atrs = atr_series_sma(candles, ATR_PERIOD)
    recent = [x for x in atrs[-VOL_ATR_LOOKBACK:] if x]

    if len(recent) < 10:
        return {"ok": True}

    avg_atr = sum(recent) / len(recent)
    ratio = atr_value / avg_atr if avg_atr else 1

    if ratio < VOL_MIN_RATIO:
        return {"ok": False, "reason": "volatility_too_low"}

    if ratio > VOL_MAX_RATIO:
        return {"ok": False, "reason": "volatility_too_high"}

    return {"ok": True}


def momentum_breakout(candles: List[Dict[str, float]]) -> Optional[str]:
    if not ENABLE_MOMENTUM_ENTRY:
        return None

    if len(candles) < 5:
        return None

    last = candles[-1]
    body = candle_body_ratio(last)

    if body < MOMENTUM_BODY_MIN:
        return None

    closes = [safe_float(c["close"]) for c in candles]
    ema20 = ema_last(closes, EMA_ENTRY)

    if ema20 is None:
        return None

    if safe_float(last["close"]) > ema20:
        return "BUY"

    if safe_float(last["close"]) < ema20:
        return "SELL"

    return None


# =========================================================
# LIQUIDITY / TRAPS / SPIKES / VACUUM
# =========================================================
def liquidity_engine(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if not USE_LIQUIDITY_SWEEP:
        return {"signal": "NONE"}

    if len(candles) < SWEEP_LOOKBACK + 2:
        return {"signal": "NONE"}

    last = candles[-1]
    recent = candles[-(SWEEP_LOOKBACK + 1):-1]

    highs = [safe_float(c["high"]) for c in recent]
    lows = [safe_float(c["low"]) for c in recent]

    recent_high = max(highs)
    recent_low = min(lows)

    last_high = safe_float(last["high"])
    last_low = safe_float(last["low"])
    last_close = safe_float(last["close"])

    if last_low < recent_low and last_close > recent_low + atr_value * SWEEP_CLOSEBACK_ATR:
        return {"signal": "BULLISH_SWEEP", "level": round(recent_low, 5)}

    if last_high > recent_high and last_close < recent_high - atr_value * SWEEP_CLOSEBACK_ATR:
        return {"signal": "BEARISH_SWEEP", "level": round(recent_high, 5)}

    return {"signal": "NONE"}


def smart_trap_detector(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if not USE_SMART_TRAP_DETECTOR:
        return {"signal": "NONE"}

    if len(candles) < TRAP_LOOKBACK + 2:
        return {"signal": "NONE"}

    last = candles[-1]
    recent = candles[-(TRAP_LOOKBACK + 1):-1]

    prev_high = max(safe_float(c["high"]) for c in recent)
    prev_low = min(safe_float(c["low"]) for c in recent)

    high = safe_float(last["high"])
    low = safe_float(last["low"])
    close = safe_float(last["close"])
    openp = safe_float(last["open"])

    rng = max(1e-9, high - low)
    upper_wick = (high - max(openp, close)) / rng
    lower_wick = (min(openp, close) - low) / rng

    if (
        low < prev_low
        and close > prev_low + atr_value * TRAP_CLOSEBACK_RATIO
        and lower_wick >= TRAP_MIN_WICK_RATIO
    ):
        return {"signal": "BULLISH_TRAP", "level": round(prev_low, 5)}

    if (
        high > prev_high
        and close < prev_high - atr_value * TRAP_CLOSEBACK_RATIO
        and upper_wick >= TRAP_MIN_WICK_RATIO
    ):
        return {"signal": "BEARISH_TRAP", "level": round(prev_high, 5)}

    return {"signal": "NONE"}


def spike_reversal_engine(candles: List[Dict[str, float]], atr: float) -> Dict[str, Any]:
    if not USE_SPIKE_REVERSAL_ENGINE:
        return {"signal": "NONE"}

    if len(candles) < SPIKE_REVERSAL_LOOKBACK:
        return {"signal": "NONE"}

    spike = candles[-2]
    confirm = candles[-1]

    high = float(spike["high"])
    low = float(spike["low"])
    openp = float(spike["open"])
    close = float(spike["close"])

    rng = max(1e-9, high - low)
    upper_wick = (high - max(openp, close)) / rng
    lower_wick = (min(openp, close) - low) / rng

    prev_high = max(float(c["high"]) for c in candles[-SPIKE_REVERSAL_LOOKBACK:-2])
    prev_low = min(float(c["low"]) for c in candles[-SPIKE_REVERSAL_LOOKBACK:-2])

    if high > prev_high and upper_wick > SPIKE_REVERSAL_WICK_MIN and float(confirm["close"]) < float(confirm["open"]):
        return {"signal": "BEARISH_SPIKE_REVERSAL", "level": prev_high}

    if low < prev_low and lower_wick > SPIKE_REVERSAL_WICK_MIN and float(confirm["close"]) > float(confirm["open"]):
        return {"signal": "BULLISH_SPIKE_REVERSAL", "level": prev_low}

    return {"signal": "NONE"}


def liquidity_vacuum_detector(candles: List[Dict[str, float]], atr: float) -> Dict[str, Any]:
    if not USE_LIQUIDITY_VACUUM:
        return {"signal": "NONE"}

    if len(candles) < 3 or atr <= 0:
        return {"signal": "NONE"}

    last = candles[-1]

    o = float(last["open"])
    c = float(last["close"])
    h = float(last["high"])
    l = float(last["low"])

    rng = max(1e-9, h - l)
    body_ratio = abs(c - o) / rng
    range_atr = rng / atr

    upper_wick = (h - max(o, c)) / rng
    lower_wick = (min(o, c) - l) / rng

    if c > o and body_ratio >= VACUUM_BODY_MIN_RATIO and upper_wick <= VACUUM_MAX_OPPOSITE_WICK and range_atr >= VACUUM_RANGE_ATR_MIN:
        return {"signal": "BULLISH_VACUUM"}

    if c < o and body_ratio >= VACUUM_BODY_MIN_RATIO and lower_wick <= VACUUM_MAX_OPPOSITE_WICK and range_atr >= VACUUM_RANGE_ATR_MIN:
        return {"signal": "BEARISH_VACUUM"}

    return {"signal": "NONE"}
# =========================================================
# REGIME / ALIGNMENT
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
        return {"regime": "CHAOTIC", "reason": f"too_many_crosses({cross_count})", "ema_gap_atr": round(ema_gap_atr, 3)}

    if ema_gap_atr >= REGIME_TREND_EMA_GAP_MIN_ATR:
        return {"regime": "TRENDING", "reason": "ema_gap_supports_trend", "ema_gap_atr": round(ema_gap_atr, 3)}

    return {"regime": "RANGING", "reason": "no_clean_trend_detected", "ema_gap_atr": round(ema_gap_atr, 3)}


def regime_allows_trade(direction: str, regime_info: Dict[str, Any]) -> Dict[str, Any]:
    regime = regime_info.get("regime", "UNKNOWN")

    if regime == "CHAOTIC":
        return {"ok": False, "reason": "chaotic_market_blocked"}

    if regime in ("TRENDING", "RANGING", "UNKNOWN"):
        return {"ok": True, "reason": f"{regime.lower()}_allowed"}

    return {"ok": True, "reason": "default_allowed"}


def regime_ok(candles_1m: List[Dict[str, float]]) -> Dict[str, Any]:
    if not USE_REGIME_FILTER:
        return {"ok": True, "reason": "regime_filter_off"}

    if len(candles_1m) < max(EMA_CROSS_LOOKBACK + 5, REGIME_LOOKBACK):
        return {"ok": True, "reason": "regime_ok_not_enough_data"}

    closes = [safe_float(c["close"]) for c in candles_1m]
    ema20 = ema_series(closes, EMA_ENTRY)

    crosses = 0
    start = len(candles_1m) - EMA_CROSS_LOOKBACK

    for i in range(start + 1, len(candles_1m)):
        if ema20[i] is None or ema20[i - 1] is None:
            continue

        prev_side = closes[i - 1] - float(ema20[i - 1])
        cur_side = closes[i] - float(ema20[i])

        if prev_side == 0:
            continue

        if (prev_side > 0 and cur_side < 0) or (prev_side < 0 and cur_side > 0):
            crosses += 1

    if crosses > EMA_CROSS_MAX:
        return {"ok": False, "reason": f"regime_block_chop(ema_crosses={crosses})"}

    return {"ok": True, "reason": "regime_ok"}


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
    trend_strength = abs(ema20 - ema50) / max(1e-9, a)

    if USE_CHOP_FILTER and trend_strength < TREND_STRENGTH_MIN:
        return {"direction": "HOLD", "reason": f"Choppy({trend_strength:.2f})", "meta": {"trend_strength": round(trend_strength, 3)}}

    return {"direction": bias, "reason": f"{bias} bias ok", "meta": {"trend_strength": round(trend_strength, 3)}}


def weighted_alignment(candles_by_tf: Dict[str, List[Dict[str, float]]]) -> Dict[str, Any]:
    buy_score = 0.0
    sell_score = 0.0
    details: Dict[str, Any] = {}

    for tf in ALIGN_TFS:
        weight = float(ALIGN_WEIGHTS.get(tf, 0.0))
        cs = candles_by_tf.get(tf) or []
        bias = _tf_bias(cs)
        direction = bias.get("direction", "HOLD")
        trend_strength = float((bias.get("meta") or {}).get("trend_strength") or 0.0)
        ts01 = min(1.0, max(0.0, trend_strength / 1.0))

        if direction == "BUY":
            buy_score += weight * ts01
        elif direction == "SELL":
            sell_score += weight * ts01

        details[tf] = {
            "dir": direction,
            "trend_strength": trend_strength,
            "weight": weight,
            "reason": bias.get("reason", ""),
        }

    margin = abs(buy_score - sell_score)
    best = max(buy_score, sell_score)
    direction = "BUY" if buy_score > sell_score else "SELL" if sell_score > buy_score else "HOLD"

    if best < ALIGN_MIN_SCORE:
        return {"direction": "HOLD", "score": round(best, 3), "buy": round(buy_score, 3), "sell": round(sell_score, 3), "margin": round(margin, 3), "details": details, "reason": "Alignment too weak"}

    if margin < ALIGN_MIN_MARGIN:
        return {"direction": "HOLD", "score": round(best, 3), "buy": round(buy_score, 3), "sell": round(sell_score, 3), "margin": round(margin, 3), "details": details, "reason": "Alignment unclear"}

    return {"direction": direction, "score": round(best, 3), "buy": round(buy_score, 3), "sell": round(sell_score, 3), "margin": round(margin, 3), "details": details, "reason": "Weighted alignment ok"}
# =========================================================
# STRUCTURE / ZONES / BOS
# =========================================================
def _pivot_high_values(candles: List[Dict[str, float]], pivot_left: int = STRUCTURE_PIVOT_LEFT, pivot_right: int = STRUCTURE_PIVOT_RIGHT) -> List[float]:
    highs = [safe_float(c["high"]) for c in candles]
    vals: List[float] = []

    for i in range(pivot_left, len(candles) - pivot_right):
        window = highs[i - pivot_left : i + pivot_right + 1]
        if highs[i] == max(window):
            vals.append(highs[i])

    return vals


def _pivot_low_values(candles: List[Dict[str, float]], pivot_left: int = STRUCTURE_PIVOT_LEFT, pivot_right: int = STRUCTURE_PIVOT_RIGHT) -> List[float]:
    lows = [safe_float(c["low"]) for c in candles]
    vals: List[float] = []

    for i in range(pivot_left, len(candles) - pivot_right):
        window = lows[i - pivot_left : i + pivot_right + 1]
        if lows[i] == min(window):
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
        return {"direction": "BUY", "reason": "HH_HL_structure"}

    if h2 < h1 and l2 < l1:
        return {"direction": "SELL", "reason": "LH_LL_structure"}

    return {"direction": "NEUTRAL", "reason": "mixed_structure"}


def structure_matches(direction: str, structure: Dict[str, Any], liq_engine_info: Dict[str, Any], trap_signal: Optional[Dict[str, Any]] = None) -> bool:
    structure_dir = structure.get("direction", "NEUTRAL")
    liq_signal = liq_engine_info.get("signal", "NONE")
    trap = (trap_signal or {}).get("signal", "NONE")

    if direction == "BUY":
        return structure_dir in ("BUY", "NEUTRAL") or liq_signal == "BULLISH_SWEEP" or trap == "BULLISH_TRAP"

    if direction == "SELL":
        return structure_dir in ("SELL", "NEUTRAL") or liq_signal == "BEARISH_SWEEP" or trap == "BEARISH_TRAP"

    return False


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
        zones.append({"low": float(min(g)), "high": float(max(g)), "mid": float(sum(g) / len(g)), "pivot_count": float(len(g)), "touches": 0.0})

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
    }


def price_overlaps_zone(candle: Dict[str, float], zone: Dict[str, float]) -> bool:
    h = safe_float(candle["high"])
    l = safe_float(candle["low"])
    return h >= safe_float(zone["low"]) and l <= safe_float(zone["high"])


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
# LIQUIDITY CLUSTERS / TARGETS / QUALITY / SCORING
# =========================================================
def detect_liquidity_clusters(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:
    if not USE_LIQUIDITY_CLUSTER_FILTER:
        return {"high_clusters": [], "low_clusters": [], "nearest_high_cluster": None, "nearest_low_cluster": None}

    if len(candles) < LIQ_CLUSTER_LOOKBACK or atr_value <= 0:
        return {"high_clusters": [], "low_clusters": [], "nearest_high_cluster": None, "nearest_low_cluster": None}

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
                out.append({"level": round(sum(g) / len(g), 5), "touches": len(g), "low": round(min(g), 5), "high": round(max(g), 5)})

        return out

    high_clusters = cluster_levels(highs)
    low_clusters = cluster_levels(lows)

    last_close = safe_float(data[-1]["close"])
    above = [c for c in high_clusters if c["level"] >= last_close]
    below = [c for c in low_clusters if c["level"] <= last_close]

    nearest_high = min(above, key=lambda x: abs(x["level"] - last_close)) if above else None
    nearest_low = min(below, key=lambda x: abs(x["level"] - last_close)) if below else None

    return {"high_clusters": high_clusters, "low_clusters": low_clusters, "nearest_high_cluster": nearest_high, "nearest_low_cluster": nearest_low}


def trap_avoidance_ok(direction: str, entry: float, atr_value: float, liq: Dict[str, Any]) -> Dict[str, Any]:
    if not USE_TRAP_AVOIDANCE:
        return {"ok": True, "reason": "trap_avoidance_off"}

    tol = atr_value * TRAP_AVOID_TOL_ATR
    nearest_high = liq.get("nearest_high_cluster")
    nearest_low = liq.get("nearest_low_cluster")

    if direction == "BUY" and nearest_high and abs(float(nearest_high["level"]) - entry) <= tol:
        return {"ok": False, "reason": f"buying_into_high_liquidity_cluster({nearest_high['level']})"}

    if direction == "SELL" and nearest_low and abs(entry - float(nearest_low["level"])) <= tol:
        return {"ok": False, "reason": f"selling_into_low_liquidity_cluster({nearest_low['level']})"}

    return {"ok": True, "reason": "trap_avoidance_ok"}


def pick_tp1_tp2(direction: str, entry: float, atr_1m: float, zones: Dict[str, Any], risk: float) -> Dict[str, Optional[float]]:
    min_tp1 = atr_1m * MIN_TP1_ATR
    min_tp2 = atr_1m * MIN_TP2_ATR

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


def market_quality_ok(candles_1m: List[Dict[str, float]], direction: str, atr_1m: float, entry: float, tp2: float, sl: float) -> Dict[str, Any]:
    if not USE_MARKET_QUALITY_FILTER:
        return {"ok": True, "reason": "market_quality_filter_off"}

    if len(candles_1m) < 25 or atr_1m <= 0:
        return {"ok": True, "reason": "market_quality_not_enough_data"}

    last = candles_1m[-1]
    closes = [safe_float(c["close"]) for c in candles_1m]
    ema20 = ema_last(closes, EMA_ENTRY)

    if ema20 is None:
        return {"ok": True, "reason": "market_quality_no_ema"}

    body_ratio = candle_body_ratio(last)
    opp_wick = opposite_wick_ratio(last, direction)
    last_range = max(1e-9, safe_float(last["high"]) - safe_float(last["low"]))
    range_atr = last_range / max(1e-9, atr_1m)
    near_extreme = close_near_extreme_ratio(last, direction)
    avg_body = recent_avg_body(candles_1m[:-1], 12)
    cur_body = abs(safe_float(last["close"]) - safe_float(last["open"]))
    body_vs_recent = (cur_body / max(1e-9, avg_body)) if avg_body > 0 else 1.0
    entry_dist_atr = abs(entry - float(ema20)) / max(1e-9, atr_1m)
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

    return {"ok": len(reasons) == 0, "reason": "market_quality_ok" if len(reasons) == 0 else "; ".join(reasons)}


def liquidity_bonus_points(
    direction: str,
    sweep: Dict[str, Any],
    trap_signal: Optional[Dict[str, Any]] = None,
    spike_signal: Optional[Dict[str, Any]] = None,
    vacuum_signal: Optional[Dict[str, Any]] = None,
) -> int:
    if not USE_SWEEP_ENTRY_BONUS:
        return 0

    signal = sweep.get("signal")
    bonus = 0

    if direction == "BUY" and signal == "BULLISH_SWEEP":
        bonus += SWEEP_ENTRY_BONUS_POINTS
    if direction == "SELL" and signal == "BEARISH_SWEEP":
        bonus += SWEEP_ENTRY_BONUS_POINTS

    if trap_signal:
        trap = trap_signal.get("signal")
        if direction == "BUY" and trap == "BULLISH_TRAP":
            bonus += TRAP_BONUS_POINTS
        if direction == "SELL" and trap == "BEARISH_TRAP":
            bonus += TRAP_BONUS_POINTS

    if spike_signal:
        spike = spike_signal.get("signal")
        if direction == "BUY" and spike == "BULLISH_SPIKE_REVERSAL":
            bonus += SPIKE_REVERSAL_BONUS_POINTS
        if direction == "SELL" and spike == "BEARISH_SPIKE_REVERSAL":
            bonus += SPIKE_REVERSAL_BONUS_POINTS

    if vacuum_signal:
        vac = vacuum_signal.get("signal")
        if direction == "BUY" and vac == "BULLISH_VACUUM":
            bonus += VACUUM_BONUS_POINTS
        if direction == "SELL" and vac == "BEARISH_VACUUM":
            bonus += VACUUM_BONUS_POINTS

    return bonus
# =========================================================
# TRADE BUILDER / ACTIVE TRADE HELPERS
# =========================================================
def build_trade(
    candles_1m: List[Dict[str, float]],
    direction: str,
    zones: Dict[str, Any],
    atr_1m: float,
    align_score: float,
) -> Dict[str, Any]:

    if direction not in ("BUY", "SELL"):
        return {"direction": "HOLD", "confidence": 0, "reason": "invalid_direction"}

    last = candles_1m[-1]
    entry = safe_float(last["close"])

    vol_check = volatility_ok(candles_1m, atr_1m)
    if not vol_check.get("ok", True):
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": vol_check.get("reason", "volatility_blocked"),
        }

    regime_info = classify_market_regime(candles_1m, atr_1m)
    regime_check = regime_allows_trade(direction, regime_info)
    if not regime_check.get("ok", True):
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": regime_check.get("reason", "regime_blocked"),
        }

    spike_check = spike_filter_ok(candles_1m, atr_1m)
    if not spike_check.get("ok", True):
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": spike_check.get("reason", "spike_blocked"),
        }

    liq = detect_liquidity_clusters(candles_1m, atr_1m)
    sweep = liquidity_engine(candles_1m, atr_1m)
    trap_signal = smart_trap_detector(candles_1m, atr_1m)
    spike_signal = spike_reversal_engine(candles_1m, atr_1m)
    vacuum_signal = liquidity_vacuum_detector(candles_1m, atr_1m)
    structure = market_structure_state(candles_1m)

    closes = [safe_float(c["close"]) for c in candles_1m]
    ema20_now = ema_last(closes, 20)
    ema50_now = ema_last(closes, 50)

    strong_uptrend = (
        ema20_now is not None
        and ema50_now is not None
        and ema20_now > ema50_now
        and (ema20_now - ema50_now) > atr_1m * 0.6
    )

    strong_downtrend = (
        ema20_now is not None
        and ema50_now is not None
        and ema20_now < ema50_now
        and (ema50_now - ema20_now) > atr_1m * 0.6
    )

    if direction == "SELL" and strong_uptrend:
        if not (
            trap_signal.get("signal") == "BEARISH_TRAP"
            and spike_signal.get("signal") == "BEARISH_SPIKE_REVERSAL"
        ):
            return {
                "direction": "HOLD",
                "confidence": 0,
                "reason": "blocked_sell_against_strong_uptrend",
            }

    if direction == "BUY" and strong_downtrend:
        if not (
            trap_signal.get("signal") == "BULLISH_TRAP"
            and spike_signal.get("signal") == "BULLISH_SPIKE_REVERSAL"
        ):
            return {
                "direction": "HOLD",
                "confidence": 0,
                "reason": "blocked_buy_against_strong_downtrend",
            }

    if USE_STRUCTURE_FILTER and not structure_matches(direction, structure, sweep, trap_signal):
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "structure_mismatch",
        }

    buf = atr_1m * SL_BUFFER_ATR

    if direction == "BUY":
        swing = recent_swing_low(candles_1m, 20)
        sl = swing - buf
    else:
        swing = recent_swing_high(candles_1m, 20)
        sl = swing + buf

    risk = abs(entry - sl)
    if risk <= 0:
        return {"direction": "HOLD", "confidence": 0, "reason": "invalid_risk"}

    if risk > atr_1m * MAX_SL_ATR:
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "sl_too_wide_vs_atr",
        }

    targets = pick_tp1_tp2(direction, entry, atr_1m, zones, risk)
    tp1 = targets.get("tp1")
    tp2 = targets.get("tp2")

    if tp2 is None:
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "no_tp2_available",
        }

    if tp1 is not None and tp2 is not None and abs(tp2 - tp1) < atr_1m * 0.2:
        tp2 = tp1 + atr_1m * 0.8 if direction == "BUY" else tp1 - atr_1m * 0.8

    quality = market_quality_ok(
        candles_1m=candles_1m,
        direction=direction,
        atr_1m=atr_1m,
        entry=entry,
        tp2=tp2,
        sl=sl,
    )
    if not quality.get("ok", True):
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": quality.get("reason", "quality_blocked"),
        }

    trap_ok = trap_avoidance_ok(direction, entry, atr_1m, liq)
    if not trap_ok.get("ok", True):
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": trap_ok.get("reason", "trap_avoidance_blocked"),
        }

    confidence = int(align_score * 100)
    confidence += liquidity_bonus_points(
        direction,
        sweep,
        trap_signal,
        spike_signal,
        vacuum_signal,
    )

    if direction == "SELL" and strong_uptrend:
        confidence -= 25

    if direction == "BUY" and strong_downtrend:
        confidence -= 25

    confidence = max(0, min(92, confidence))

    score = max(1, min(10, int(confidence / 10)))
    grade = score_to_grade(score)

    return {
        "direction": direction,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp1": round(tp1, 5) if tp1 is not None else None,
        "tp2": round(tp2, 5),
        "tp": round(tp2, 5),
        "r_multiple": round(_calc_r_multiple(direction, entry, sl, tp2), 2),
        "confidence": confidence,
        "score_1_10": score,
        "grade": grade,
        "reason": "trade_validated",
        "meta": {
            "regime": regime_info,
            "structure": structure,
            "sweep": sweep,
            "trap_signal": trap_signal,
            "spike_signal": spike_signal,
            "vacuum_signal": vacuum_signal,
            "liquidity_clusters": liq,
            "strong_uptrend": strong_uptrend,
            "strong_downtrend": strong_downtrend,
        },
    }


def hit_level(direction: str, last_high: float, last_low: float, level: float, kind: str) -> bool:
    if direction == "BUY":
        return last_high >= level if kind in ("TP1", "TP2", "TP") else last_low <= level
    return last_low <= level if kind in ("TP1", "TP2", "TP") else last_high >= level


def trail_stop_suggestion(candles_1m: List[Dict[str, float]], direction: str, atr_value: float) -> float:
    data = candles_1m[-TRAIL_LOOKBACK:] if len(candles_1m) >= TRAIL_LOOKBACK else candles_1m
    buf = atr_value * TRAIL_BUFFER_ATR

    if direction == "BUY":
        structure = min(safe_float(c["low"]) for c in data)
        return structure - buf

    structure = max(safe_float(c["high"]) for c in data)
    return structure + buf


def _open_locked_signal(symbol: str, timeframe: str, base_reason: str, bias_info: Dict[str, Any], reg_reason: str, trade_like: Dict[str, Any]) -> Dict[str, Any]:
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
        "meta": {**(stored.get("meta") or {}), "regime_reason": reg_reason, "bias": bias_info},
    })

    ACTIVE_TRADES[_trade_key(symbol, timeframe)] = stored
    RISK_STATE["signals_today"][symbol] = int(RISK_STATE["signals_today"].get(symbol, 0)) + 1
    _push_history(dict(stored))
    return stored
# =========================================================
# ACTIVE TRADE MANAGEMENT / ANALYZE CORE
# =========================================================
async def manage_active_trade(symbol: str, timeframe: str, candles_1m: List[Dict[str, float]], trade: Dict[str, Any]):
    if not trade:
        return None

    last = candles_1m[-1]
    last_high = safe_float(last["high"])
    last_low = safe_float(last["low"])
    last_close = safe_float(last["close"])

    direction = trade.get("direction")
    entry = safe_float(trade.get("entry"))
    sl = safe_float(trade.get("sl"))
    tp1 = trade.get("tp1")
    tp2 = trade.get("tp2") or trade.get("tp")

    if hit_level(direction, last_high, last_low, sl, "SL"):
        trade["status"] = "CLOSED"
        trade["outcome"] = "SL"
        trade["closed_price"] = round(sl, 5)
        trade["closed_at"] = _now_ts()

        RISK_STATE["daily_R"] += -1.0
        RISK_STATE["cooldown_until"][symbol] = _now_ts() + COOLDOWN_MIN_AFTER_LOSS * 60

        ACTIVE_TRADES.pop(_trade_key(symbol, timeframe), None)
        await send_telegram_message(build_closed_message(symbol, timeframe, trade, "SL", sl))

        idx = _find_history_index(trade["trade_id"])
        if idx is not None:
            TRADE_HISTORY[idx].update(trade)

        return None

    if tp1 and not trade.get("tp1_hit"):
        if hit_level(direction, last_high, last_low, tp1, "TP1"):
            trade["tp1_hit"] = True
            trade["tp1_price"] = round(tp1, 5)

            if direction == "BUY":
                trade["sl"] = entry + (entry - sl) * BE_BUFFER_R
            else:
                trade["sl"] = entry - (sl - entry) * BE_BUFFER_R

            await send_telegram_message(build_tp1_message(symbol, timeframe, trade))

    if tp2 and hit_level(direction, last_high, last_low, tp2, "TP2"):
        trade["status"] = "CLOSED"
        trade["outcome"] = "TP2"
        trade["closed_price"] = round(tp2, 5)
        trade["closed_at"] = _now_ts()

        RISK_STATE["daily_R"] += trade.get("r_multiple", 1.0)
        ACTIVE_TRADES.pop(_trade_key(symbol, timeframe), None)
        await send_telegram_message(build_closed_message(symbol, timeframe, trade, "TP2", tp2))

        idx = _find_history_index(trade["trade_id"])
        if idx is not None:
            TRADE_HISTORY[idx].update(trade)

        return None

    atr_val = atr_last(candles_1m, ATR_PERIOD)
    if atr_val:
        new_sl = trail_stop_suggestion(candles_1m, direction, atr_val)
        if direction == "BUY":
            trade["sl"] = max(trade["sl"], new_sl)
        else:
            trade["sl"] = min(trade["sl"], new_sl)

    if tp2:
        progress = trade_progress_percent(direction, entry, sl, tp2, last_close)
        trade["progress_pct"] = round(progress, 2)

    return trade


async def open_new_trade(symbol: str, timeframe: str, trade_like: Dict[str, Any]):
    trade_id = str(uuid.uuid4())
    trade = dict(trade_like)

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
    _push_history(dict(trade))
    await send_telegram_message(build_signal_message(symbol, timeframe, trade))
    return trade


async def analyze_symbol(symbol: str, timeframe: str = ENTRY_TF) -> Dict[str, Any]:
    app_id = os.getenv("DERIV_APP_ID", "1089")

    candles_1m = await _cached_fetch_candles(app_id, symbol, "1m", 260)
    candles_30m = await _cached_fetch_candles(app_id, symbol, "30m", 220)
    candles_1h = await _cached_fetch_candles(app_id, symbol, "1h", 220)
    candles_4h = await _cached_fetch_candles(app_id, symbol, "4h", 220)

    if not candles_1m or not candles_30m or not candles_1h or not candles_4h:
        return {"symbol": symbol, "timeframe": timeframe, "signal": {"direction": "HOLD", "confidence": 0, "reason": "No candles returned"}}

    last = candles_1m[-1]
    price = safe_float(last["close"])
    atr_1m = float(atr_last(candles_1m, ATR_PERIOD) or 0.0)

    reg = regime_ok(candles_1m)
    if not reg.get("ok", True):
        return {"symbol": symbol, "timeframe": timeframe, "price": round(price, 5), "signal": {"direction": "HOLD", "confidence": 0, "reason": reg.get("reason")}}

    align = weighted_alignment({"30m": candles_30m, "1h": candles_1h, "4h": candles_4h})
    direction = align.get("direction", "HOLD")

    if direction == "HOLD":
        return {"symbol": symbol, "timeframe": timeframe, "price": round(price, 5), "signal": {"direction": "HOLD", "confidence": 0, "reason": align.get("reason", "alignment_hold")}}

    zones = strongest_zones(candles_1m, atr_1m)
    trade = build_trade(candles_1m, direction, zones, atr_1m, float(align.get("score", 0.0)))

    if trade.get("direction") == "HOLD":
        return {"symbol": symbol, "timeframe": timeframe, "price": round(price, 5), "signal": trade, "zones": zones}

    return {"symbol": symbol, "timeframe": timeframe, "price": round(price, 5), "signal": trade, "zones": zones}
# =========================================================
# ROUTES
# =========================================================
@router.get("/telegram-test")
async def telegram_test() -> Dict[str, Any]:
    ok = await send_telegram_message("✅ DEXTRADEZ BOT Telegram test works")
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
        lines.append(f"{t.get('symbol')} {t.get('direction')} | Entry {t.get('entry')} | SL {t.get('sl')} | TP2 {t.get('tp2', t.get('tp'))}")

    ok = await send_telegram_message("\n".join(lines))
    return {"ok": ok}


@router.post("/analyze")
async def analyze(req: AnalyzeRequest):
    _reset_daily_if_needed()

    symbol = req.symbol
    timeframe = req.timeframe

    if _active_total() >= MAX_ACTIVE_TOTAL:
        return {"direction": "HOLD", "reason": "max_active_trades"}

    if RISK_STATE["daily_R"] <= DAILY_LOSS_LIMIT_R:
        return {"direction": "HOLD", "reason": "daily_loss_limit_hit"}

    if RISK_STATE["signals_today"].get(symbol, 0) >= MAX_SIGNALS_PER_DAY_PER_SYMBOL:
        return {"direction": "HOLD", "reason": "max_signals_today_symbol"}

    if symbol in RISK_STATE["cooldown_until"] and _now_ts() < RISK_STATE["cooldown_until"][symbol]:
        return {"direction": "HOLD", "reason": "cooldown_active"}

    app_id = os.getenv("DERIV_APP_ID", "1089")
    candles_1m = await _cached_fetch_candles(app_id, symbol, "1m", 260)

    if len(candles_1m) < 120:
        return {"direction": "HOLD", "reason": "not_enough_candles"}

    key = _trade_key(symbol, timeframe)

    if key in ACTIVE_TRADES:
        trade = await manage_active_trade(symbol, timeframe, candles_1m, ACTIVE_TRADES[key])
        return trade or {"direction": "HOLD", "reason": "trade_closed"}

    result = await analyze_symbol(symbol, timeframe)
    signal = result.get("signal", {})

    if signal.get("direction") == "HOLD":
        return result

    last = candles_1m[-1]
    zones = result.get("zones", {})
    rej_zone = zones.get("support_zone") if signal.get("direction") == "BUY" else zones.get("resistance_zone")
    fast_rej_ok = rejection_ok(last, signal.get("direction"), rej_zone) if rej_zone else False
    fast_bos_ok = bos_confirm(candles_1m, signal.get("direction"), BOS_LOOKBACK)
    momentum_dir = momentum_breakout(candles_1m)
    atr_val = float(atr_last(candles_1m, ATR_PERIOD) or 0.0)
    trap_signal = smart_trap_detector(candles_1m, atr_val)
    spike_signal = spike_reversal_engine(candles_1m, atr_val)
    vacuum_signal = liquidity_vacuum_detector(candles_1m, atr_val)

    can_fast_enter = (
        ENABLE_FAST_ENTRY
        and signal.get("direction") in ("BUY", "SELL")
        and int(signal.get("confidence", 0)) >= FAST_ENTRY_MIN_CONF
        and (
            fast_rej_ok
            or fast_bos_ok
            or momentum_dir == signal.get("direction")
            or (trap_signal.get("signal") == "BULLISH_TRAP" and signal.get("direction") == "BUY")
            or (trap_signal.get("signal") == "BEARISH_TRAP" and signal.get("direction") == "SELL")
            or (spike_signal.get("signal") == "BULLISH_SPIKE_REVERSAL" and signal.get("direction") == "BUY")
            or (spike_signal.get("signal") == "BEARISH_SPIKE_REVERSAL" and signal.get("direction") == "SELL")
            or (vacuum_signal.get("signal") == "BULLISH_VACUUM" and signal.get("direction") == "BUY")
            or (vacuum_signal.get("signal") == "BEARISH_VACUUM" and signal.get("direction") == "SELL")
        )
    )

    if not can_fast_enter:
        return result

    new_trade = await open_new_trade(symbol, timeframe, signal)
    return new_trade


@router.post("/scan")
async def scan(req: ScanRequest):
    results = []

    for symbol in req.symbols:
        try:
            res = await analyze(AnalyzeRequest(symbol=symbol, timeframe=req.timeframe))
            results.append({"symbol": symbol, "result": res})
        except Exception as e:
            results.append({"symbol": symbol, "result": {"direction": "HOLD", "reason": str(e)}})

    return {"results": results}


@router.websocket("/live")
async def live_feed(ws: WebSocket):
    await ws.accept()

    try:
        while True:
            payload = {
                "active_trades": list(ACTIVE_TRADES.values()),
                "daily_R": RISK_STATE["daily_R"],
                "history": TRADE_HISTORY[-50:],
                "timestamp": _now_ts(),
            }
            await ws.send_text(json.dumps(payload))
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        pass
 # Backward compatibility aliases
analyze_market = analyze
scan_markets = scan