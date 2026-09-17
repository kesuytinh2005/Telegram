from core.task_manager import replace_user_tasks
# ============================================================
# DRAGON BOT - START
# ============================================================

from telethon import events
from html import escape
import time
import importlib
import pkgutil
from core.user_notify import check_new_user
from core.users import get_user_count

# ============================================================
# CONFIG
# ============================================================

BOT_NAME = "DRAGON BOT"
BOT_VERSION = "1.0.0"

START_TIME = time.time()


# ============================================================
# CACHE
# ============================================================

_COMMAND_CACHE = None


# ============================================================
# LOAD COMMAND INFO
# ============================================================

def load_command_info():

    global _COMMAND_CACHE

    command_list = []

    try:

        import commands as commands_package

        package_path = commands_package.__path__

        for module_info in pkgutil.iter_modules(package_path):

            module_name = module_info.name

            # Bỏ qua file private
            if module_name.startswith("_"):
                continue

            # Bỏ qua chính start.py
            if module_name == "start":
                continue

            try:

                module = importlib.import_module(
                    f"commands.{module_name}"
                )

                info = getattr(
                    module,
                    "COMMAND_INFO",
                    None
                )

                # Không có COMMAND_INFO thì bỏ qua
                if not isinstance(info, dict):
                    continue

                command = info.get("command")

                if not command:
                    continue

                # Copy để không sửa dictionary gốc
                info = dict(info)

                info["_module"] = module

                command_list.append(info)

                print(
                    f"[START] Loaded /{command}"
                )

            except Exception as e:

                print(
                    f"[START] Không thể load "
                    f"commands.{module_name}: {e}"
                )

    except Exception as e:

        print(
            f"[START] Không thể quét commands/: {e}"
        )


    # ========================================================
    # SORT
    # ========================================================

    command_list.sort(
        key=lambda item: (
            str(item.get("category", "")),
            str(item.get("command", ""))
        )
    )


    _COMMAND_CACHE = command_list

    return command_list


# ============================================================
# GET COMMANDS
# ============================================================

def get_commands():

    global _COMMAND_CACHE

    if _COMMAND_CACHE is None:
        return load_command_info()

    return _COMMAND_CACHE


# ============================================================
# REFRESH
# ============================================================

def refresh_commands():

    global _COMMAND_CACHE

    _COMMAND_CACHE = None

    return load_command_info()


# ============================================================
# USER NAME
# ============================================================

def get_user_name(user):

    first_name = user.first_name or ""
    last_name = user.last_name or ""

    name = (
        f"{first_name} {last_name}"
    ).strip()

    if not name:
        name = "Không có tên"

    return escape(name)


# ============================================================
# USERNAME
# ============================================================

def get_username(user):

    if user.username:
        return f"@{escape(user.username)}"

    return "Không có"


# ============================================================
# UPTIME
# ============================================================

def get_uptime():

    seconds = int(
        time.time() - START_TIME
    )

    days, seconds = divmod(
        seconds,
        86400
    )

    hours, seconds = divmod(
        seconds,
        3600
    )

    minutes, seconds = divmod(
        seconds,
        60
    )

    result = []

    if days:
        result.append(
            f"{days}d"
        )

    if hours:
        result.append(
            f"{hours}h"
        )

    if minutes:
        result.append(
            f"{minutes}m"
        )

    result.append(
        f"{seconds}s"
    )

    return " ".join(result)


# ============================================================
# SAFE TEXT
# ============================================================

def safe_text(value):

    if value is None:
        return ""

    return escape(
        str(value)
    )


# ============================================================
# COMMAND SECTION
# ============================================================

def build_command_section(info):

    command = safe_text(
        info.get(
            "command",
            ""
        )
    )

    title = safe_text(
        info.get(
            "title",
            ""
        )
    )

    description = safe_text(
        info.get(
            "description",
            "Chưa có mô tả."
        )
    )

    usage = safe_text(
        info.get(
            "usage",
            f"/{command}"
        )
    )


    # ========================================================
    # COMMAND
    # ========================================================

    text = (
        f"  🔹 <b>/{command}</b>"
    )

    if title:

        text += (
            f"  <b>— {title}</b>"
        )

    text += "\n"


    # ========================================================
    # DESCRIPTION
    # ========================================================

    if description:

        text += (
            f"     └ <i>{description}</i>\n"
        )


    

    # ========================================================
    # EXAMPLES
    # ========================================================

    examples = info.get(
        "examples",
        []
    )

    if examples:

        if not isinstance(
            examples,
            (list, tuple)
        ):
            examples = [examples]

        

        


    # ========================================================
    # DETAILS
    # ========================================================

    details = info.get(
        "details",
        []
    )

    if details:

        if not isinstance(
            details,
            (list, tuple)
        ):
            details = [details]

        


    # ========================================================
    # SUPPORTED
    # ========================================================

    supported = info.get(
        "supported"
    )

    if supported:

        if not isinstance(
            supported,
            (list, tuple)
        ):
            supported = [supported]

        text += (
            "     🧩 <i>Hỗ trợ:</i> "
        )

        text += " • ".join(
            safe_text(item)
            for item in supported
        )

        text += "\n"


    # ========================================================
    # PERMISSION
    # ========================================================

    permission = info.get(
        "permission"
    )

    if permission:

        text += (
            "     🔐 <i>Quyền: "
            f"{safe_text(permission)}</i>\n"
        )


    text += "\n"

    return text


# ============================================================
# COMMAND LIST
# ============================================================

def build_commands_text():

    command_list = get_commands()


    if not command_list:

        return (
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📚 <b>DANH SÁCH LỆNH</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "└ ⚠️ <i>Chưa có lệnh nào.</i>\n\n"
        )


    # ========================================================
    # GROUP CATEGORY
    # ========================================================

    categories = {}


    for info in command_list:

        category = info.get(
            "category",
            "🛠 SYSTEM"
        )

        categories.setdefault(
            category,
            []
        ).append(info)


    text = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📚 <b>DANH SÁCH LỆNH</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )


    # ========================================================
    # RENDER CATEGORY
    # ========================================================

    for category, items in categories.items():

        text += (
            f"▌ <b>{safe_text(category)}</b>\n"
        )


        for info in items:

            text += build_command_section(
                info
            )


        text += (
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )


    return text


# ============================================================
# DASHBOARD
# ============================================================

def build_start_text(user, user_cont):
    name = get_user_name(
        user
    )

    username = get_username(
        user
    )


    # ========================================================
    # HEADER
    # ========================================================

    text = (
        "╭──────────────────────╮\n"
        "│           🤖 <b>DRAGON BOT</b>                      │\n"
        "│             <i>Control Panel</i>                           │\n"
        "╰──────────────────────╯\n\n"
    )


    # ========================================================
    # USER INFO
    # ========================================================

    text += (
        "👤 <b>THÔNG TIN NGƯỜI DÙNG</b>\n"
        "┌──────────────────────\n"
        f"├ 👤 Tên: <b>{name}</b>\n"
        f"├ 🌐 Username: <b>{username}</b>\n"
        f"├ 🆔 Telegram ID: <code>{user.id}</code>\n"
        "└ 🌐 Ngôn ngữ: 🇻🇳 Tiếng Việt\n"
        "└──────────────────────\n\n"
    )


    # ========================================================
    # BOT STATUS
    # ========================================================

    text += (
        "⚡ <b>TRẠNG THÁI BOT</b>\n"
        "┌──────────────────────\n"
        "├ 🤖 Bot: 🟢 <b>ONLINE</b>\n"
        "├ 🔗 Account: 🟢 <b>CONNECTED</b>\n"
        f"├ 📦 Version: <code>{BOT_VERSION}</code>\n"
        f"├ ⏱ Uptime: <code>{get_uptime()}</code>\n"
        f"└👥User count: <code>{user_cont}</code>\n"
        "└──────────────────────\n\n"
    )


    # ========================================================
    # COMMANDS
    # ========================================================

    text += build_commands_text()


    # ========================================================
    # FOOTER
    # ========================================================

    text += (
        "💡 <b>HƯỚNG DẪN NHANH</b>\n"
        "┌──────────────────────\n"
        "│ • Gửi trực tiếp command để sử dụng.\n"
        "│ • Xem <i>cú pháp</i> và <i>ví dụ</i> bên trên.\n"
        "│ • Phần <i>hướng dẫn</i> giải thích cách dùng.\n"
        "└──────────────────────\n\n"
        "🚀 <b>DRAGON BOT</b>\n"
        "<i>Fast • Stable • Always Ready</i>"
    )


    return text


# ============================================================
# REGISTER
# ============================================================

def register(bot, notify_bot):

    @bot.on(
        events.NewMessage(
            pattern=r"^/start$"
        )
    )
    async def start(event):

        # Command mới thay thế toàn bộ task nền cũ của user.
        await replace_user_tasks(event.sender_id)

        # Xóa session chờ của command cũ.
        for _attr in ("_dragon_sessions", "_dragon_download_sessions"):
            _sessions = getattr(bot, _attr, None)
            if isinstance(_sessions, dict):
                _sessions.pop(event.sender_id, None)

        try:
            from core.power.session import clear_session
            clear_session(bot, event.sender_id)
        except Exception:
            pass


        try:

            await check_new_user(
                event,
                notify_bot
            )

            user_count = get_user_count()

            user = await event.get_sender()

            text = build_start_text(
                user,
                user_count
            )

            await event.reply(
                text,
                parse_mode="html"
            )

        except Exception as e:

            print(
                f"[START ERROR] {e}"
            )

            try:

                await event.reply(
                    "❌ Không thể tạo dashboard."
                )

            except Exception:
                pass
