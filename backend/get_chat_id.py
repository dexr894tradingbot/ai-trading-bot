from telegram import Bot

BOT_TOKEN = "8565401725:AAGjf6R40iwB1fYsIl2624PCF8wXMtqJFww" # replace with your bot token

bot = Bot(token=BOT_TOKEN)

print("Send any message to your bot in Telegram now.")
input("After sending a message, press Enter here...")

updates = bot.get_updates()
if not updates:
   print("No messages found. Make sure you sent a message to the bot.")
else:
    chat_id = updates[-1].message.chat.id
    print(f"✅ Your Chat ID is: {chat_id}")