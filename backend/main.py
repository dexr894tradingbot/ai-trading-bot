# backend/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes.analyze import router as analyze_router
import os
import requests
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
app = FastAPI()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
    print(f"Telegram: {r.status_code}, {r.text}")
    return r.json()

@app.get("/test_telegram")
def test_telegram():
    try:
        return send_telegram_message("TEST from backend")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # allow all origins (DEV ONLY)
        
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(analyze_router, prefix="/analyze_market")
    
import asyncio
import websockets
import json
from datetime import datetime




# Volatility markets to track
VOL_MARKETS = ["R_25", "R_50", "R_75", "R_100"]
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1hr", "4hr"]  # example timeframes



def analyze_candles(candles):
    """
    Trend + support/resistance + breakout/retest logic
    Produces entry, SL, TP, direction
    """
    if len(candles) < 20:
        return None

    closes = [c["close"] for c in candles[-20:]]
    highs = [c["high"] for c in candles[-20:]]
    lows = [c["low"] for c in candles[-20:]]
    last_close = closes[-1]

    # Simple trend
    sma_short = sum(closes[-5:]) / 5
    sma_long = sum(closes) / 20
    trend = "up" if sma_short > sma_long else "down"

    # Support/resistance
    recent_high = max(highs[-10:])
    recent_low = min(lows[-10:])

    volatility_range = recent_high - recent_low
    TP_distance = volatility_range * 2  # long trades
    SL_distance = volatility_range * 0.5

    if trend == "up":
        entry = last_close
        SL = entry - SL_distance
        TP = entry + TP_distance
        direction = "BUY"
    else:
        entry = last_close
        SL = entry + SL_distance
        TP = entry - TP_distance
        direction = "SELL"

    # Skip tiny trades
    if abs(SL_distance) < 3:
        return None

    return {
        "entry": round(entry, 2),
        "SL": round(SL, 2),
        "TP": round(TP, 2),
        "direction": direction
    }

# ---- MAIN LOOP ----
async def run_market(market):
    url = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

    async with websockets.connect(url) as ws:
        # Subscribe to candles for all timeframes
        for tf in TIMEFRAMES:
            sub_msg = {
                "ticks_history": market,
                "subscribe": 1,
                "granularity": int(tf.replace("m", "")) * 60,
                "adjust_start_time": 1,
                "end": "latest",
                "start": 1
            }
            await ws.send(json.dumps(sub_msg))

        candles_buffer = []

        async for message in ws:
            data = json.loads(message)
            if "history" in data:
                candles = data["history"]["candles"]
                # convert to our format
                for c in candles:
                    candle_obj = {
                        "open": float(c["open"]),
                        "high": float(c["high"]),
                        "low": float(c["low"]),
                        "close": float(c["close"]),
                        "epoch": c["epoch"]
                    }
                    candles_buffer.append(candle_obj)

                signal = analyze_candles(candles_buffer)
                if signal:
                    text = f"Market: {market}\nDirection: {signal['direction']}\nEntry: {signal['entry']}\nSL: {signal['SL']}\nTP: {signal['TP']}"
                    print(text)
                    candles_buffer = []  # reset for next batch

async def main():
    tasks = [run_market(m) for m in VOL_MARKETS]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
