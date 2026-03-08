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
        lines.append(
            f"{t.get('symbol')} {t.get('direction')} | "
            f"Entry {t.get('entry')} | SL {t.get('sl')} | TP2 {t.get('tp2', t.get('tp'))}"
        )
    ok = await send_telegram_message("\n".join(lines))
    return {"ok": ok}


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
REGIME_RANGE_ATR_RATIO_MAX = 0.85
REGIME_CHAOS_CROSS_COUNT = 7
EMA_CROSS_LOOKBACK = 45
EMA_CROSS_MAX = 6
ATR_COMP_LOOKBACK = 70
ATR_COMP_RATIO_MIN = 0.65

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
FAST_ENTRY_MIN_CONF = 55

ENABLE_SIGNAL_LOCK = True
MIN_LOCK_CONF = 60

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

USE_FAKE_BREAKOUT_FILTER = True
FAKE_BREAKOUT_LOOKBACK = 10
FAKE_BREAKOUT_BACKINSIDE_ATR = 0.12

ENABLE_MOMENTUM_ENTRY = True
MOMENTUM_BODY_MIN = 0.65

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
# =========================================================
# PERFORMANCE REPORT
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

        lower = min(o, cl) - l

        return max(0.0, lower / rng)

    upper = h - max(o, cl)

    return max(0.0, upper / rng)


def recent_swing_low(candles: List[Dict[str, float]], lookback: int = 20) -> float:

    return min(safe_float(c["low"]) for c in candles[-lookback:])


def recent_swing_high(candles: List[Dict[str, float]], lookback: int = 20) -> float:

    return max(safe_float(c["high"]) for c in candles[-lookback:])
# =========================================================
# LIQUIDITY ENGINE
# =========================================================

def liquidity_engine(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:

    if len(candles) < 25:
        return {"signal": "NONE"}

    last = candles[-1]

    highs = [safe_float(c["high"]) for c in candles[-20:-1]]
    lows = [safe_float(c["low"]) for c in candles[-20:-1]]

    if not highs or not lows:
        return {"signal": "NONE"}

    recent_high = max(highs)
    recent_low = min(lows)

    last_high = safe_float(last["high"])
    last_low = safe_float(last["low"])
    last_close = safe_float(last["close"])

    signal = "NONE"

    # Bullish liquidity sweep
    if last_low < recent_low and last_close > recent_low:
        signal = "BULLISH_SWEEP"

    # Bearish liquidity sweep
    elif last_high > recent_high and last_close < recent_high:
        signal = "BEARISH_SWEEP"

    return {
        "signal": signal,
        "recent_high": round(recent_high, 5),
        "recent_low": round(recent_low, 5),
    }


# =========================================================
# MOMENTUM ENGINE
# =========================================================

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
# MARKET STATE
# =========================================================

def detect_market_state(candles: List[Dict[str, float]], atr_value: float) -> str:

    if len(candles) < 30:
        return "UNKNOWN"

    closes = [safe_float(c["close"]) for c in candles]

    ema20 = ema_last(closes, 20)
    ema50 = ema_last(closes, 50)

    if ema20 is None or ema50 is None:
        return "UNKNOWN"

    liq = liquidity_engine(candles, atr_value)

    if liq.get("signal") in ("BULLISH_SWEEP", "BEARISH_SWEEP"):
        return "LIQUIDITY_SWEEP"

    last = candles[-1]
    body = candle_body_ratio(last)

    if abs(float(ema20) - float(ema50)) > atr_value * 0.4:
        return "TRENDING"

    if body > 0.6:
        return "BREAKOUT"

    recent_ranges = [
        safe_float(c["high"]) - safe_float(c["low"])
        for c in candles[-10:]
    ]

    avg_range = sum(recent_ranges) / max(1, len(recent_ranges))

    if avg_range < atr_value * 0.7:
        return "CONSOLIDATION"

    return "RANGING"


# =========================================================
# VOLATILITY FILTER
# =========================================================

def volatility_ok(candles: List[Dict[str, float]], atr_value: float) -> Dict[str, Any]:

    if not USE_VOLATILITY_FILTER:
        return {"ok": True, "reason": "vol_filter_off"}

    if len(candles) < VOL_ATR_LOOKBACK or atr_value <= 0:
        return {"ok": True, "reason": "not_enough_data"}

    atrs = []

    for i in range(ATR_PERIOD, len(candles)):

        window = candles[i-ATR_PERIOD:i]

        ranges = [
            abs(
                safe_float(c["high"]) -
                safe_float(c["low"])
            ) for c in window
        ]

        atrs.append(sum(ranges) / ATR_PERIOD)

    recent = atrs[-VOL_ATR_LOOKBACK:]

    if len(recent) < 10:
        return {"ok": True}

    avg_atr = sum(recent) / len(recent)

    ratio = atr_value / avg_atr if avg_atr else 1

    if ratio < VOL_MIN_RATIO:
        return {"ok": False, "reason": "volatility_too_low"}

    if ratio > VOL_MAX_RATIO:
        return {"ok": False, "reason": "volatility_too_high"}

    return {"ok": True}
# =========================================================
# REGIME / TREND FILTERS
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
        return {
            "regime": "CHAOTIC",
            "reason": f"too_many_crosses({cross_count})",
            "ema_gap_atr": round(ema_gap_atr, 3),
        }

    if ema_gap_atr >= REGIME_TREND_EMA_GAP_MIN_ATR:
        return {
            "regime": "TRENDING",
            "reason": "ema_gap_supports_trend",
            "ema_gap_atr": round(ema_gap_atr, 3),
        }

    return {
        "regime": "RANGING",
        "reason": "no_clean_trend_detected",
        "ema_gap_atr": round(ema_gap_atr, 3),
    }


def regime_allows_trade(direction: str, regime_info: Dict[str, Any]) -> Dict[str, Any]:

    regime = regime_info.get("regime", "UNKNOWN")

    if regime == "CHAOTIC":
        return {"ok": False, "reason": "chaotic_market_blocked"}

    if regime in ("TRENDING", "RANGING", "UNKNOWN"):
        return {"ok": True, "reason": f"{regime.lower()}_allowed"}

    return {"ok": True, "reason": "default_allowed"}


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
        return {
            "direction": "HOLD",
            "reason": f"Choppy({trend_strength:.2f})",
            "meta": {"trend_strength": round(trend_strength, 3)},
        }

    return {
        "direction": bias,
        "reason": f"{bias} bias ok",
        "meta": {"trend_strength": round(trend_strength, 3)},
    }


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
# STRUCTURE
# =========================================================

def _pivot_high_values(
    candles: List[Dict[str, float]],
    pivot_left: int = STRUCTURE_PIVOT_LEFT,
    pivot_right: int = STRUCTURE_PIVOT_RIGHT,
) -> List[float]:

    highs = [safe_float(c["high"]) for c in candles]
    vals: List[float] = []

    for i in range(pivot_left, len(candles) - pivot_right):
        window = highs[i - pivot_left : i + pivot_right + 1]
        if highs[i] == max(window):
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


def structure_matches(direction: str, structure: Dict[str, Any], liq_engine_info: Dict[str, Any]) -> bool:

    structure_dir = structure.get("direction", "NEUTRAL")
    liq_signal = liq_engine_info.get("signal", "NONE")

    if direction == "BUY":
        return structure_dir in ("BUY", "NEUTRAL") or liq_signal == "BULLISH_SWEEP"

    if direction == "SELL":
        return structure_dir in ("SELL", "NEUTRAL") or liq_signal == "BEARISH_SWEEP"

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
# LIQUIDITY CLUSTERS / TRAP AVOIDANCE
# =========================================================

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
def is_bullish(candle):
    return float(candle["close"]) > float(candle["open"])


def is_bearish(candle):
    return float(candle["close"]) < float(candle["open"])


def opposite_wick_ratio(candle, direction):
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


def close_near_extreme_ratio(candle, direction):
    c = float(candle["close"])
    h = float(candle["high"])
    l = float(candle["low"])

    rng = max(1e-9, h - l)

    if direction == "BUY":
        return (h - c) / rng

    return (c - l) / rng


def recent_avg_body(candles, lookback=12):
    if len(candles) < lookback:
        return 0

    bodies = [
        abs(float(c["close"]) - float(c["open"]))
        for c in candles[-lookback:]
    ]

    return sum(bodies) / len(bodies)


def spike_filter_ok(candles, atr_value):
    if not USE_SPIKE_FILTER:
        return {"ok": True}

    if len(candles) < SPIKE_LOOKBACK:
        return {"ok": True}

    recent = candles[-SPIKE_LOOKBACK:]

    for c in recent:
        rng = float(c["high"]) - float(c["low"])

        if rng > atr_value * SPIKE_MAX_ATR_MULT:
            return {
                "ok": False,
                "reason": "Spike candle detected"
            }

    return {"ok": True}


# =========================================================
# MARKET QUALITY
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
# TRADE BUILDER
# =========================================================

def liquidity_bonus_points(direction: str, sweep: Dict[str, Any], liq: Dict[str, Any]) -> int:

    if not USE_SWEEP_ENTRY_BONUS:
        return 0

    signal = sweep.get("signal")

    if direction == "BUY" and signal == "BULLISH_SWEEP":
        return SWEEP_ENTRY_BONUS_POINTS

    if direction == "SELL" and signal == "BEARISH_SWEEP":
        return SWEEP_ENTRY_BONUS_POINTS

    return 0


def build_trade(
    candles_5m: List[Dict[str, float]],
    direction: str,
    zones: Dict[str, Any],
    atr_5m: float,
    align_score: float,
) -> Dict[str, Any]:

    if direction not in ("BUY", "SELL"):
        return {"direction": "HOLD", "confidence": 0}

    last = candles_5m[-1]
    entry = safe_float(last["close"])

    # Liquidity
    liq = detect_liquidity_clusters(candles_5m, atr_5m)
    sweep = liquidity_engine(candles_5m, atr_5m)

    # Stop loss
    buf = atr_5m * SL_BUFFER_ATR

    if direction == "BUY":
        swing = recent_swing_low(candles_5m, 20)
        sl = swing - buf
    else:
        swing = recent_swing_high(candles_5m, 20)
        sl = swing + buf

    risk = abs(entry - sl)

    if risk <= 0:
        return {"direction": "HOLD", "confidence": 0}

    if risk > atr_5m * MAX_SL_ATR:
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "SL too wide vs ATR",
        }

    targets = pick_tp1_tp2(direction, entry, atr_5m, zones, risk)

    tp1 = targets.get("tp1")
    tp2 = targets.get("tp2")

    if tp2 is None:
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": "No TP2 available",
        }

    # R:R
    r_multiple = _calc_r_multiple(direction, entry, sl, tp2)

    # Quality check
    quality = market_quality_ok(
        candles_5m,
        direction,
        atr_5m,
        entry,
        tp2,
        sl,
    )

    if not quality["ok"]:
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": quality["reason"],
        }

    # Trap avoidance
    trap_ok = trap_avoidance_ok(direction, entry, atr_5m, liq)

    if not trap_ok["ok"]:
        return {
            "direction": "HOLD",
            "confidence": 0,
            "reason": trap_ok["reason"],
        }

    # Base confidence
    confidence = int(align_score * 100)

    # Liquidity bonus
    confidence += liquidity_bonus_points(direction, sweep, liq)

    confidence = max(0, min(100, confidence))

    # Score (1-10)
    score = int(confidence / 10)

    grade = score_to_grade(score)

    return {
        "direction": direction,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp1": round(tp1, 5) if tp1 else None,
        "tp2": round(tp2, 5),
        "r_multiple": round(r_multiple, 2),
        "confidence": confidence,
        "score_1_10": score,
        "grade": grade,
        "reason": "trade_validated",
    }
# =========================================================
# ANALYZE CORE
# =========================================================

async def analyze_symbol(symbol: str, timeframe: str = ENTRY_TF) -> Dict[str, Any]:

    app_id = os.getenv("DERIV_APP_ID", "1089")

    candles_5m = await fetch_deriv_candles(
        app_id=app_id,
        symbol=symbol,
        timeframe=timeframe,
        count=260,
    )

    candles_30m = await fetch_deriv_candles(
        app_id=app_id,
        symbol=symbol,
        timeframe="30m",
        count=220,
    )

    candles_1h = await fetch_deriv_candles(
        app_id=app_id,
        symbol=symbol,
        timeframe="1h",
        count=220,
    )

    candles_4h = await fetch_deriv_candles(
        app_id=app_id,
        symbol=symbol,
        timeframe="4h",
        count=220,
    )

    if not candles_5m or not candles_30m or not candles_1h or not candles_4h:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "reason": "No candles returned",
            },
        }

    last = candles_5m[-1]
    price = safe_float(last["close"])
    atr_5m = float(atr_last(candles_5m, ATR_PERIOD) or 0.0)

    zones = strongest_zones(candles_5m, atr_5m)

    bias = weighted_alignment({
        "30m": candles_30m,
        "1h": candles_1h,
        "4h": candles_4h,
    })

    bias_dir = bias.get("direction", "HOLD")
    bias_score = float(bias.get("score", 0.0))

    market_state = detect_market_state(candles_5m, atr_5m)
    regime_state = classify_market_regime(candles_5m, atr_5m)
    regime_check = regime_allows_trade(bias_dir, regime_state)
    vol_check = volatility_ok(candles_5m, atr_5m)
    spike_check = spike_filter_ok(candles_5m, atr_5m)
    structure = market_structure_state(candles_5m)
    momentum_dir = momentum_breakout(candles_5m)

    if bias_dir not in ("BUY", "SELL"):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "market_state": market_state,
            "bias": bias,
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "reason": bias.get("reason", "No clear bias"),
            },
            "zones": zones,
        }

    if not regime_check["ok"]:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "market_state": market_state,
            "bias": bias,
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "reason": regime_check["reason"],
            },
            "zones": zones,
        }

    if not vol_check["ok"]:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "market_state": market_state,
            "bias": bias,
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "reason": vol_check["reason"],
            },
            "zones": zones,
        }

    if not spike_check["ok"]:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "market_state": market_state,
            "bias": bias,
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "reason": spike_check["reason"],
            },
            "zones": zones,
        }

    if USE_STRUCTURE_FILTER and not structure_matches(
        bias_dir,
        structure,
        liquidity_engine(candles_5m, atr_5m),
    ):
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "market_state": market_state,
            "bias": bias,
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "reason": "Structure mismatch",
            },
            "zones": zones,
        }

    trade = build_trade(
        candles_5m=candles_5m,
        direction=bias_dir,
        zones=zones,
        atr_5m=atr_5m,
        align_score=bias_score,
    )

    key = _trade_key(symbol, timeframe)

    # FAST ENTRY
    rej_zone = zones.get("support_zone") if bias_dir == "BUY" else zones.get("resistance_zone")
    fast_rej_ok = rejection_ok(last, bias_dir, rej_zone) if rej_zone else False
    fast_bos_ok = bos_confirm(candles_5m, bias_dir, BOS_LOOKBACK)

    can_fast_enter = (
        ENABLE_FAST_ENTRY
        and trade.get("direction") in ("BUY", "SELL")
        and int(trade.get("confidence", 0)) >= FAST_ENTRY_MIN_CONF
        and int(trade.get("score_1_10", 0)) >= 6
        and (
            fast_rej_ok
            or fast_bos_ok
            or momentum_dir == trade.get("direction")
        )
        and trade.get("tp1") is not None
    )

    if can_fast_enter:
        trade_id = str(uuid.uuid4())

        stored = dict(trade)
        stored.update({
            "trade_id": trade_id,
            "opened_at": _now_ts(),
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "OPEN",
            "tp1_hit": False,
            "mode": "FAST_ENTRY",
            "entry_type": "FAST_ENTRY",
            "progress_pct": 0.0,
            "meta": {
                "market_state": market_state,
                "bias": bias,
                "structure": structure,
                "regime": regime_state,
            },
        })

        ACTIVE_TRADES[key] = stored
        _push_history(dict(stored))
        await send_telegram_message(build_signal_message(symbol, timeframe, stored))

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "market_state": market_state,
            "bias": bias,
            "signal": stored,
            "zones": zones,
            "active": True,
        }

    # PENDING SETUP
    if bos_confirm(candles_5m, bias_dir, BOS_LOOKBACK) or momentum_dir == bias_dir:
        lvl = bos_level(candles_5m, bias_dir, BOS_LOOKBACK)
        if lvl is None:
            lvl = price

        pending = {
            "direction": bias_dir,
            "bos_level": float(lvl),
            "created_idx": len(candles_5m) - 1,
            "score_1_10": 5,
            "grade": "WATCH",
        }

        PENDING_SETUPS[key] = pending

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": round(price, 5),
            "market_state": market_state,
            "bias": bias,
            "signal": {
                "direction": "HOLD",
                "confidence": 0,
                "score_1_10": 5,
                "grade": "WATCH",
                "reason": "Setup detected, waiting retest",
            },
            "pending": pending,
            "zones": zones,
            "active": False,
        }

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "price": round(price, 5),
        "market_state": market_state,
        "bias": bias,
        "signal": {
            "direction": "HOLD",
            "confidence": 0,
            "score_1_10": int(trade.get("score_1_10", 4)),
            "grade": trade.get("grade", "IGNORE"),
            "reason": trade.get("reason", "Waiting setup"),
        },
        "zones": zones,
        "active": False,
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


async def manage_active_trade(symbol: str, timeframe: str, candles_5m: List[Dict[str, float]]) -> Optional[Dict[str, Any]]:
    key = _trade_key(symbol, timeframe)

    if key not in ACTIVE_TRADES:
        return None

    trade = ACTIVE_TRADES[key]
    last = candles_5m[-1]
    last_high = safe_float(last["high"])
    last_low = safe_float(last["low"])
    last_close = safe_float(last["close"])
    atr_5m = float(atr_last(candles_5m, ATR_PERIOD) or 0.0)

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

    trail_price = trail_stop_suggestion(candles_5m, direction, atr_5m)
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
            "active": True,
            "signal": live_signal,
            "actions": actions,
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

    ACTIVE_TRADES.pop(key, None)

    await send_telegram_message(build_closed_message(symbol, timeframe, closed, outcome, last_close))

    return {
        "active": False,
        "closed_trade": closed,
        "signal": {"direction": "HOLD", "confidence": 0, "reason": f"Trade closed by {outcome}"},
        "actions": actions,
    }


# =========================================================
# ANALYZE ROUTE
# =========================================================

@router.post("/analyze")
async def analyze_market(data: AnalyzeRequest) -> Dict[str, Any]:
    _reset_daily_if_needed()

    symbol = data.symbol.strip()
    timeframe = ENTRY_TF
    app_id = os.getenv("DERIV_APP_ID", "1089")

    candles_5m = await fetch_deriv_candles(
        app_id=app_id,
        symbol=symbol,
        timeframe=timeframe,
        count=260,
    )

    if not candles_5m:
        return {"error": "No candles returned", "symbol": symbol, "timeframe": timeframe}

    managed = await manage_active_trade(symbol, timeframe, candles_5m)

    base = await analyze_symbol(symbol, timeframe)
    base["candles"] = candles_5m

    if managed is not None:
        base["active"] = managed["active"]
        base["actions"] = managed.get("actions", [])

        if managed["active"]:
            base["signal"] = managed["signal"]
            return base

        base["closed_trade"] = managed.get("closed_trade")
        base["signal"] = managed["signal"]
        return base

    return base


# =========================================================
# SCAN ROUTE
# =========================================================

@router.post("/scan")
async def scan_markets(payload: ScanRequest) -> Dict[str, Any]:
    _reset_daily_if_needed()

    rows: List[Dict[str, Any]] = []

    for sym in payload.symbols:
        result = await analyze_symbol(sym, ENTRY_TF)

        signal = result.get("signal", {})

        row = {
            "symbol": sym,
            "direction": signal.get("direction", "HOLD"),
            "confidence": int(signal.get("confidence", 0)),
            "score_1_10": int(signal.get("score_1_10", 0)),
            "grade": signal.get("grade", "IGNORE"),
            "reason": signal.get("reason", ""),
            "entry": signal.get("entry"),
            "sl": signal.get("sl"),
            "tp1": signal.get("tp1"),
            "tp2": signal.get("tp2"),
            "entry_type": signal.get("entry_type", "NONE"),
            "market_state": result.get("market_state", "UNKNOWN"),
            "active": bool(result.get("active", False)),
        }

        rows.append(row)

    rows.sort(key=lambda x: (x.get("score_1_10", 0), x.get("confidence", 0)), reverse=True)

    return {
        "timeframe": ENTRY_TF,
        "active_total": _active_total(),
        "max_active_total": MAX_ACTIVE_TOTAL,
        "ranked": rows,
        "top": rows[0] if rows else None,
    }
# =========================================================
# LIVE WEBSOCKET ANALYSIS
# =========================================================

@router.websocket("/ws/analyze")
async def websocket_analyze(ws: WebSocket):

    await ws.accept()

    try:

        while True:

            msg = await ws.receive_text()

            try:
                payload = json.loads(msg)
            except Exception:
                await ws.send_json({"error": "Invalid JSON"})
                continue

            symbol = payload.get("symbol")
            timeframe = payload.get("timeframe", ENTRY_TF)

            if not symbol:
                await ws.send_json({"error": "Symbol required"})
                continue

            result = await analyze_symbol(symbol, timeframe)

            await ws.send_json(result)

    except WebSocketDisconnect:
        return


# =========================================================
# LIVE SCANNER WEBSOCKET
# =========================================================

@router.websocket("/ws/scan")
async def websocket_scan(ws: WebSocket):

    await ws.accept()

    try:

        while True:

            msg = await ws.receive_text()

            try:
                payload = json.loads(msg)
            except Exception:
                await ws.send_json({"error": "Invalid JSON"})
                continue

            symbols = payload.get(
                "symbols",
                ["R_10", "R_25", "R_50", "R_75", "R_100"],
            )

            rows = []

            for sym in symbols:

                result = await analyze_symbol(sym, ENTRY_TF)

                signal = result.get("signal", {})

                rows.append({
                    "symbol": sym,
                    "direction": signal.get("direction", "HOLD"),
                    "confidence": signal.get("confidence", 0),
                    "score": signal.get("score_1_10", 0),
                    "grade": signal.get("grade", "IGNORE"),
                    "entry": signal.get("entry"),
                    "sl": signal.get("sl"),
                    "tp1": signal.get("tp1"),
                    "tp2": signal.get("tp2"),
                    "market_state": result.get("market_state"),
                })

            rows.sort(
                key=lambda x: (x.get("score", 0), x.get("confidence", 0)),
                reverse=True,
            )

            await ws.send_json({
                "timeframe": ENTRY_TF,
                "ranked": rows,
                "top": rows[0] if rows else None,
            })

    except WebSocketDisconnect:
        return