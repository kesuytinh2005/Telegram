import os
from config import NOTIFY_CHAT_ID

async def admin(notify_bot, user, command, result=None, file_path=None, extra_info=None):
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    msg = (
        f"👤 <b>Người dùng:</b> {name}\n"
        f"🆔 <b>ID Telegram:</b> {user.id}\n"
        f"⚡ <b>Lệnh:</b> {command}"
    )

    if extra_info:
        msg += f"\n📎 <b>Thông tin thêm:</b> {extra_info}"

    if result:
        if isinstance(result, dict):
            result_str = "\n".join(f"{k}: {v}" for k, v in result.items())
            msg += f"\n📌 <b>Kết quả:</b>\n{result_str}"
        else:
            msg += f"\n📌 <b>Kết quả:</b> {result}"

    try:
        await notify_bot.send_message(NOTIFY_CHAT_ID, msg, parse_mode="html")
        if file_path and os.path.exists(file_path):
            await notify_bot.send_file(
                NOTIFY_CHAT_ID,
                file=file_path,
                caption=msg,
                supports_streaming=True,
                force_document=False,
                parse_mode="html"
            )
    except Exception as e:
        print(f"❌ Lỗi gửi thông báo admin: {e}")
