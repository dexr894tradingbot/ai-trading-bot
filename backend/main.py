from __future__ import annotations

import os
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import init_db
from routes.analyze import (
    router as analyze_router,
    analyze_market,
    AnalyzeRequest,
    restore_state_from_db,
)

AUTO_SYMBOLS = os.getenv("AUTO_SYMBOLS", "R_10,R_25,R_50,R_75,R_100").split(",")
AUTO_ANALYZE_SECONDS = int(os.getenv("AUTO_ANALYZE_SECONDS", "20"))


async def auto_analyze_loop():
    while True:
        try:
            for symbol in AUTO_SYMBOLS:
                try:
                    result = await analyze_market(
                        AnalyzeRequest(symbol=symbol.strip(), timeframe="5m")
                    )
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
    print("🚀 Starting backend...")

    try:
        init_db()
        print("✅ Database initialized")
    except Exception as e:
        print(f"❌ Database init failed: {e}")

    try:
        restore_state_from_db()
        print("✅ Bot state restored from database")
    except Exception as e:
        print(f"❌ State restore failed: {e}")

    analyze_task = asyncio.create_task(auto_analyze_loop())
    print("✅ auto_analyze_loop started")

    try:
        yield
    finally:
        analyze_task.cancel()
        print("🛑 auto_analyze_loop stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/")
def root():
    return {"status": "bot running"}


@app.get("/health")
def health():
    return {
        "ok": True,
        "auto_symbols": AUTO_SYMBOLS,
        "auto_analyze_seconds": AUTO_ANALYZE_SECONDS,
    }


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analyze_router, prefix="/analyze_market")