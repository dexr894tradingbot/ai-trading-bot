import os
import aiohttp

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
print("USING TELEGRAM_CHAT_ID:", TELEGRAM_CHAT_ID)

async def send_telegram_message(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    print("❌ Telegram send failed:", resp.status, await resp.text())
                    return False
                return True
    except Exception as e:
        print("❌ Telegram exception:", str(e))
        return False


async def send_telegram_photo(photo_bytes: bytes, caption: str = "") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    data = aiohttp.FormData()
    data.add_field("chat_id", TELEGRAM_CHAT_ID)
    data.add_field("caption", caption)
    data.add_field("photo", photo_bytes, filename="chart.png", content_type="image/png")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                if resp.status != 200:
                    print("❌ Telegram photo send failed:", resp.status, await resp.text())
                    return False
                return True
    except Exception as e:
        print("❌ Telegram photo exception:", str(e))
        return False