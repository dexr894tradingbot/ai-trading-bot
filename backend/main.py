# backend/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os
import requests
import asyncio
import websockets
import json

from routes.analyze import (
    router as analyze_router,
    analyze_market,
    AnalyzeRequest,
)

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

AUTO_SYMBOLS = os.getenv("AUTO_SYMBOLS", "R_10,R_25,R_50,R_75,R_100").split(",")
AUTO_ANALYZE_SECONDS = int(os.getenv("AUTO_ANALYZE_SECONDS", "20"))

# ---- LIVE STORE ----
LIVE_STATE = {
    "price": None,
    "updated_at": None,
    "markets": {}
}

VOL_MARKETS = ["R_10", "R_25", "R_50", "R_75", "R_100"]

TIMEFRAME_MAP = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}


def send_telegram_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        return {"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
    print(f"Telegram: {r.status_code}, {r.text}")
    return r.json()


def analyze_candles(candles):
    if len(candles) < 20:
        return None

    closes = [c["close"] for c in candles[-20:]]
    highs = [c["high"] for c in candles[-20:]]
    lows = [c["low"] for c in candles[-20:]]
    last_close = closes[-1]

    sma_short = sum(closes[-5:]) / 5
    sma_long = sum(closes) / 20
    trend = "up" if sma_short > sma_long else "down"

    recent_high = max(highs[-10:])
    recent_low = min(lows[-10:])

    volatility_range = recent_high - recent_low
    tp_distance = volatility_range * 2
    sl_distance = volatility_range * 0.5

    if abs(sl_distance) < 3:
        return None

    if trend == "up":
        entry = last_close
        sl = entry - sl_distance
        tp = entry + tp_distance
        direction = "BUY"
    else:
        entry = last_close
        sl = entry + sl_distance
        tp = entry - tp_distance
        direction = "SELL"

    supports = [round(recent_low, 2)]
    resistances = [round(recent_high, 2)]

    return {
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "tp1": round(entry + (tp - entry) * 0.5, 2) if direction == "BUY" else round(entry - (entry - tp) * 0.5, 2),
        "tp2": round(tp, 2),
        "direction": direction,
        "confidence": 70,
        "reason": f"SMA trend {trend}, breakout range logic",
        "supports": supports,
        "resistances": resistances,
    }


async def fetch_candles(symbol: str, granularity: int):
    url = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

    async with websockets.connect(url) as ws:
        req = {
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": 260,
            "end": "latest",
            "granularity": granularity,
            "style": "candles",
        }
        await ws.send(json.dumps(req))
        msg = await ws.recv()
        data = json.loads(msg)

        candles = []
        history = data.get("candles") or data.get("history", {}).get("candles", [])
        for c in history:
            candles.append({
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "epoch": int(c["epoch"]),
            })

        return candles


async def update_market_timeframe(symbol: str, timeframe: str, granularity: int):
    candles = await fetch_candles(symbol, granularity)
    if not candles:
        return

    signal = analyze_candles(candles)
    last_price = candles[-1]["close"]
    updated_at = candles[-1]["epoch"]

    supports = signal["supports"] if signal else []
    resistances = signal["resistances"] if signal else []

    LIVE_STATE["price"] = last_price
    LIVE_STATE["updated_at"] = updated_at

    if symbol not in LIVE_STATE["markets"]:
        LIVE_STATE["markets"][symbol] = {}

    LIVE_STATE["markets"][symbol][timeframe] = {
        "candles": candles,
        "price": last_price,
        "supports": supports,
        "resistances": resistances,
        "signal": signal,
        "updated_at": updated_at,
    }


async def live_loop():
    while True:
        try:
            for symbol in VOL_MARKETS:
                for timeframe, granularity in TIMEFRAME_MAP.items():
                    await update_market_timeframe(symbol, timeframe, granularity)
            await asyncio.sleep(2)
        except Exception as e:
            print("live_loop error:", str(e))
            await asyncio.sleep(5)


async def auto_analyze_loop():
    while True:
        try:
            for symbol in AUTO_SYMBOLS:
                try:
                    result = await analyze_market(AnalyzeRequest(symbol=symbol, timeframe="1m"))
                    signal = result.get("signal", {})
                    reason = signal.get("reason", "-")
                    direction = signal.get("direction", "HOLD")
                    print(f"auto_analyze_loop: {symbol} -> {direction} | {reason}")
                except Exception as inner_e:
                    print(f"auto_analyze_loop symbol error [{symbol}]: {inner_e}")
        except Exception as e:
            print("auto_analyze_loop error:", str(e))

        await asyncio.sleep(AUTO_ANALYZE_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    live_task = asyncio.create_task(live_loop())
    analyze_task = asyncio.create_task(auto_analyze_loop())

    print("✅ live_loop started")
    print("✅ auto_analyze_loop started")

    try:
        yield
    finally:
        live_task.cancel()
        analyze_task.cancel()
        print("🛑 live_loop stopped")
        print("🛑 auto_analyze_loop stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/test_telegram")
def test_telegram():
    try:
        return send_telegram_message("TEST from backend")
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/auto-status")
def auto_status():
    return {
        "ok": True,
        "auto_symbols": AUTO_SYMBOLS,
        "auto_analyze_seconds": AUTO_ANALYZE_SECONDS,
        "live_markets": list(LIVE_STATE.get("markets", {}).keys()),
    }


@app.get("/live")
def get_live(symbol: str = "R_10", timeframe: str = "5m"):
    market_data = LIVE_STATE.get("markets", {}).get(symbol, {}).get(timeframe)

    if not market_data:
        return {
            "ok": False,
            "error": "No live data yet",
            "symbol": symbol,
            "timeframe": timeframe,
        }

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "price": market_data["price"],
        "candles": market_data["candles"],
        "supports": market_data["supports"],
        "resistances": market_data["resistances"],
        "signal": market_data["signal"],
        "updated_at": market_data["updated_at"],
    }


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analyze_router, prefix="/analyze_market")