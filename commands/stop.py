# ============================================================
# commands/stop.py
# ============================================================

from telethon import events
from core.state import state
from core.task_manager import stop_user_tasks


COMMAND_INFO = {
    "command": "stop",
    "category": "⚙️ SYSTEM",
    "title": "Dừng tiến trình",

    "description": (
        "Dừng phiên xử lý hiện tại của người dùng."
    ),

    "usage": "/stop",

    "examples": [
        "/stop",
    ],

    "details": [
        "Dừng phiên /download đang chờ link.",
        "Dừng phiên /getuidfb đang chờ link.",
        "Xóa session của người dùng hiện tại.",
        "Người dùng khác không bị ảnh hưởng.",
    ],
}


def register(bot, notify_bot):

    @bot.on(events.NewMessage(pattern=r"^/stop$"))
    async def stop(event):

        user_id = event.sender_id

        # Hủy ngay toàn bộ task nền của user.
        await stop_user_tasks(user_id)

        # Xóa session chờ của mọi command.
        for _attr in ("_dragon_sessions", "_dragon_download_sessions"):
            _sessions = getattr(bot, _attr, None)
            if isinstance(_sessions, dict):
                _sessions.pop(user_id, None)

        try:
            from core.power.session import clear_session
            clear_session(bot, user_id)
        except Exception:
            pass


        sessions = getattr(
            bot,
            "_dragon_sessions",
            {}
        )

        session = sessions.get(
            user_id
        )

        if session:

            session["running"] = False
            session["processing"] = False

            # Xóa riêng user này
            sessions.pop(
                user_id,
                None
            )

        await event.reply(
            "🛑 <b>Đã dừng phiên hiện tại.</b>\n\n"
            "Dùng <code>/getuidfb</code> "
            "hoặc command khác để tiếp tục.",
            parse_mode="html"
        )