# ============================================================
# core/user_notify.py
# ============================================================

from core.users import register_user


async def check_new_user(
    event,
    notify_bot
):

    try:

        user = await event.get_sender()

        is_new, info = register_user(
            user
        )

        # User cũ -> không thông báo
        if not is_new:
            return False

        # Không có bot thông báo
        if not notify_bot:
            return True

        username = info.get(
            "username"
        )

        username_text = (
            f"@{username}"
            if username
            else "Không có username"
        )

        name = info.get(
            "name",
            "Không có tên"
        )

        user_id = info.get(
            "user_id"
        )

        message = (
            "╭─────────────────────╮\n"
            "│  👤 <b>USER MỚI</b>  │\n"
            "╰─────────────────────╯\n\n"

            f"👤 <b>Tên:</b> {name}\n"
            f"🔗 <b>Username:</b> {username_text}\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"

            "📌 <b>Trạng thái:</b> Người dùng mới\n"
            "🤖 <b>Bot:</b> Đã bắt đầu sử dụng bot"
        )

        try:

            await notify_bot(
                user,
                "USER_NEW",
                result=message
            )

        except Exception as e:

            print(
                f"[USER NOTIFY] {e}"
            )

        return True

    except Exception as e:

        print(
            f"[USER CHECK] {e}"
        )

        return False