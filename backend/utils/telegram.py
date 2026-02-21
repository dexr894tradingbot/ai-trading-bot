import aiohttp
import os

TELEGRAM_BOT_TOKEN =  "PUT_YOUR_REAL_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID =  "PUT_YOUR_REAL_CHAT_ID_HERE"

async def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                print("❌ Telegram send failed:", resp.status, text)
