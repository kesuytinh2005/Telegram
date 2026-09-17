from dotenv import load_dotenv
load_dotenv()
import os

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
NOTIFY_BOT_TOKEN = os.getenv("TG_NOTIFY_BOT_TOKEN", "")
NOTIFY_CHAT_ID = int(os.getenv("TG_NOTIFY_CHAT_ID", "0"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.join(BASE_DIR, "BOT")
DATA_DIR = os.path.join(BASE_DIR, "data")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

USER_SESSION = os.path.join(BOT_DIR, "mysession")
BOT_SESSION = os.path.join(BOT_DIR, "bot")
NOTIFY_SESSION = os.path.join(BOT_DIR, "bot_X")
USERS_FILE = os.path.join(DATA_DIR, "users.json")

MAX_FILE_SIZE = 50 * 1024 * 1024

# ============================================================
# EVNSPC
# ============================================================

EVNSPC_API_URL = os.getenv(
    "EVNSPC_API_URL",
    "https://cskh.evnspc.vn/TraCuu/GetThongTinLichNgungGiamCungCapDien"
)

EVNSPC_REFERER = os.getenv(
    "EVNSPC_REFERER",
    "https://cskh.evnspc.vn/"
)

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)

API_TIMEOUT = int(
    os.getenv(
        "EVNSPC_TIMEOUT",
        "30"
    )
)

LOOKAHEAD_DAYS = int(
    os.getenv(
        "EVNSPC_LOOKAHEAD_DAYS",
        "7"
    )
)




os.makedirs(BOT_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
