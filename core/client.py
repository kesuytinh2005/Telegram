from telethon import TelegramClient

from config import (
    API_ID, API_HASH, BOT_TOKEN, NOTIFY_BOT_TOKEN,
    BOT_SESSION, NOTIFY_SESSION
)

def create_clients():
    bot = TelegramClient(BOT_SESSION, API_ID, API_HASH)
    notify = TelegramClient(NOTIFY_SESSION, API_ID, API_HASH)
    return bot, notify
