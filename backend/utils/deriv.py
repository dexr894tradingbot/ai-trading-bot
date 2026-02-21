import json
import websockets

DERIV_WS = "wss://ws.binaryws.com/websockets/v3"

def tf_to_granularity(tf: str) -> int:
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 60 * 60
    # fallback default 1 minute
    return 60

async def fetch_deriv_candles(app_id: str, symbol: str, timeframe: str, count: int = 100):
    granularity = tf_to_granularity(timeframe)
    url = f"{DERIV_WS}?app_id={app_id}"

    req = {
        "ticks_history": symbol,
        "style": "candles",
        "granularity": granularity,
        "count": count,
        "end": "latest",
        "start": 1
    }

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(req))
        msg = await ws.recv()
        data = json.loads(msg)

    if "error" in data:
        raise Exception(data["error"].get("message", "Deriv API error"))

    candles = data.get("candles", [])
    # Convert into your frontend-friendly format
    # (Lightweight charts uses time in seconds)
    out = []
    for c in candles:
        out.append({
            "time": int(c["epoch"]),          # seconds epoch
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
        })
    return out
