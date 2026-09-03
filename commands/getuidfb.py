# ============================================================
# commands/getuidfb.py
# ============================================================

import asyncio
import base64
import html
import re
import requests

from urllib.parse import (
    urlparse,
    parse_qs,
    unquote
)

from telethon import events


# ============================================================
# COMMAND INFO
# ============================================================

from core.task_manager import replace_user_tasks, track_current_task

COMMAND_INFO = {
    "command": "getuidfb",
    "category": "🔎 FACEBOOK",
    "title": "Facebook UID",

    "description": (
        "Lấy UID Facebook từ link profile, post, story, "
        "group, reel..."
    ),

    "usage": "/getuidfb",

    "examples": [
        "/getuidfb",
    ],

    "details": [
        "Gửi /getuidfb.",
        "Sau đó gửi một hoặc nhiều link Facebook.",
        "Không cần xuống dòng giữa các link.",
        "Bot tự động nhận diện tất cả URL.",
        "Sau khi xử lý xong bot tiếp tục chờ link.",
        "Dùng /stop để dừng.",
    ],

    "supported": [
        "Profile",
        "Post",
        "Story",
        "Group",
        "Reel",
        "Share link",
    ],
}


# ============================================================
# HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),

    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,image/webp,"
        "image/apng,*/*;q=0.8"
    ),

    "Accept-Language":
        "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",

    "Upgrade-Insecure-Requests":
        "1",

    "DNT":
        "1",

    "Sec-Fetch-Site":
        "none",

    "Sec-Fetch-Mode":
        "navigate",

    "Sec-Fetch-User":
        "?1",

    "Sec-Fetch-Dest":
        "document",
}


# ============================================================
# SESSION KEY
# ============================================================

SESSION_KEY = "_dragon_sessions"


# ============================================================
# LẤY SESSION
# ============================================================

def get_sessions(bot):

    if not hasattr(bot, SESSION_KEY):

        setattr(
            bot,
            SESSION_KEY,
            {}
        )

    return getattr(
        bot,
        SESSION_KEY
    )


# ============================================================
# TÁCH URL FACEBOOK
# ============================================================

def extract_urls(text):
    """
    Tách tất cả URL Facebook trong một tin nhắn.

    Hỗ trợ:

    https://facebook.com/a

    https://facebook.com/a https://facebook.com/b

    https://facebook.com/a
    https://facebook.com/b

    Và quan trọng:

    https://facebook.com/ahttps://facebook.com/bhttps://facebook.com/c

    => 3 URL
    """

    if not text:
        return []

    # --------------------------------------------------------
    # Tìm tất cả vị trí bắt đầu URL
    # --------------------------------------------------------

    matches = list(
        re.finditer(
            r"https?://",
            text,
            flags=re.IGNORECASE
        )
    )

    if not matches:
        return []

    urls = []

    # --------------------------------------------------------
    # Cắt từ URL này đến URL tiếp theo
    # --------------------------------------------------------

    for index, match in enumerate(matches):

        start = match.start()

        if index + 1 < len(matches):

            end = matches[index + 1].start()

        else:

            end = len(text)

        url = text[start:end].strip()

        # ----------------------------------------------------
        # Loại bỏ khoảng trắng đầu/cuối
        # ----------------------------------------------------

        url = url.strip()

        # ----------------------------------------------------
        # Dấu câu cuối URL
        # ----------------------------------------------------

        url = url.rstrip(
            ".,!?;:)]}>\"'"
        )

        if not url:
            continue

        # ----------------------------------------------------
        # Nếu giữa URL có whitespace thì lấy từng phần
        # ----------------------------------------------------

        parts = re.split(
            r"\s+",
            url
        )

        for part in parts:

            part = part.strip()

            part = part.rstrip(
                ".,!?;:)]}>\"'"
            )

            if not part:
                continue

            # ------------------------------------------------
            # Kiểm tra URL
            # ------------------------------------------------

            try:

                parsed = urlparse(
                    part
                )

                host = (
                    parsed.netloc
                    .lower()
                    .split(":")[0]
                )

            except Exception:

                continue

            valid = (
                host == "facebook.com"
                or host.endswith(".facebook.com")
                or host == "fb.com"
                or host.endswith(".fb.com")
                or host == "fb.watch"
                or host.endswith(".fb.watch")
            )

            if not valid:
                continue

            if part not in urls:

                urls.append(part)

    return urls


# ============================================================
# STORY ID
# ============================================================

def extract_story_id(url):

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=15,
            allow_redirects=True
        )

        fb_url = response.url

        parsed = urlparse(
            fb_url
        )

        query = parse_qs(
            parsed.query
        )

        if "next" in query:

            fb_url = unquote(
                query["next"][0]
            )

        path_parts = (
            urlparse(fb_url)
            .path
            .strip("/")
            .split("/")
        )

        if (
            len(path_parts) >= 3
            and path_parts[0].lower() == "stories"
        ):

            encoded_story_id = (
                path_parts[2]
            )

            try:

                decoded = base64.b64decode(
                    encoded_story_id
                ).decode(
                    "utf-8",
                    errors="ignore"
                )

                story_id = (
                    decoded
                    .split(":")[-1]
                )

                return story_id

            except Exception:

                return encoded_story_id

    except Exception as e:

        print(
            f"[STORY ERROR] {e}"
        )

    return None


# ============================================================
# ASYNC STORY
# ============================================================

async def extract_story_async(url):

    return await asyncio.to_thread(
        extract_story_id,
        url
    )


# ============================================================
# TÌM ID THEO KEY
# ============================================================

def find_ids(
    html_text,
    keys
):

    results = []

    for key in keys:

        pattern = (
            rf'"{re.escape(key)}"'
            r'\s*:\s*"?(.*?)"?(?:,|\}})'
        )

        try:

            matches = re.findall(
                pattern,
                html_text
            )

        except Exception:

            continue

        for match in matches:

            value = match

            if isinstance(
                match,
                tuple
            ):

                value = match[0]

            if not value:
                continue

            value = str(value).strip()

            if not value:
                continue

            if value == "0":
                continue

            if value not in results:

                results.append(
                    value
                )

    return results


# ============================================================
# GET UID
#
# Hàm này KHÔNG reply message.
#
# Nó chỉ trả kết quả về cho vòng xử lý.
# ============================================================

async def get_uid(
    event,
    url,
    notify_bot=None
):

    user = await event.get_sender()

    try:

        # ----------------------------------------------------
        # Request Facebook trong thread
        # ----------------------------------------------------

        response = await asyncio.to_thread(
            requests.get,
            url,
            headers=HEADERS,
            timeout=15,
            allow_redirects=True
        )

        html_text = response.text

        # ----------------------------------------------------
        # POST
        # ----------------------------------------------------

        post_keys = [
            "post_id",
            "pcb.",
            "subscription_target_id",
            "share_fbid",
            "mf_story_key",
            "top_level_post_id",
            "tl_objid",
            "story_fbid",
            "throwback_story_fbid",
        ]

        # ----------------------------------------------------
        # PROFILE
        # ----------------------------------------------------

        profile_keys = [
            "fb://profile/",
            "userID",
        ]

        # ----------------------------------------------------
        # GROUP
        # ----------------------------------------------------

        group_keys = [
            "fb://group/",
            "groupID",
        ]

        # ----------------------------------------------------
        # Tìm profile
        # ----------------------------------------------------

        profile_ids = find_ids(
            html_text,
            profile_keys
        )

        # ----------------------------------------------------
        # Group
        # ----------------------------------------------------

        group_ids = []

        parsed = urlparse(
            url
        )

        path_parts = (
            parsed.path
            .strip("/")
            .split("/")
        )

        if (
            len(path_parts) >= 2
            and path_parts[0].lower() == "groups"
        ):

            group_id = (
                path_parts[1]
                .split("?")[0]
            )

            if group_id:

                group_ids.append(
                    group_id
                )

        html_group_ids = find_ids(
            html_text,
            group_keys
        )

        for value in html_group_ids:

            if value not in group_ids:

                group_ids.append(
                    value
                )

        # ----------------------------------------------------
        # Story
        # ----------------------------------------------------

        story_ids = []

        story_id = await extract_story_async(
            url
        )

        if story_id:

            story_ids.append(
                story_id
            )

        # ----------------------------------------------------
        # Posts
        # ----------------------------------------------------

        post_ids = find_ids(
            html_text,
            post_keys
        )

        # ----------------------------------------------------
        # Ưu tiên giống code cũ
        # ----------------------------------------------------

        all_ids = (
            group_ids
            + profile_ids
            + story_ids
            + post_ids
        )

        uid = (
            all_ids[0]
            if all_ids
            else None
        )

        # ----------------------------------------------------
        # Admin
        # ----------------------------------------------------

        if uid:

            result = {
                "url": url,
                "uid": uid,
                "success": True,
            }

        else:

            result = {
                "url": url,
                "uid": None,
                "success": False,
            }

        # ----------------------------------------------------
        # Admin notify
        # ----------------------------------------------------

        if notify_bot:

            try:

                if uid:

                    admin_result = (
                        f"UID: {uid}\n"
                        f"Link: {url}"
                    )

                else:

                    admin_result = (
                        f"UID: Không tìm thấy\n"
                        f"Link: {url}"
                    )

                await notify_bot(
                    user,
                    "/getuidfb",
                    result=admin_result
                )

            except Exception as e:

                print(
                    f"[UID ADMIN] {e}"
                )

        return result

    except Exception as e:

        print(
            f"[GETUID ERROR] {e}"
        )

        return {
            "url": url,
            "uid": None,
            "success": False,
            "error": str(e),
        }


# ============================================================
# ESCAPE HTML
# ============================================================

def esc(value):

    return html.escape(
        str(value)
    )

# ============================================================
# LỌC FACEBOOK UID SAU KHI API TRẢ RESULT
# ============================================================

def clean_facebook_uid(value):

    if value is None:
        return None

    # Chuyển về string
    value = str(value).strip()

    if not value:
        return None

    # Chỉ lấy chuỗi số liên tục
    match = re.search(
        r"(?<!\d)\d+(?!\d)",
        value
    )

    if not match:
        return None

    uid = match.group(0)

    # Facebook UID phải là số
    if not uid.isdigit():
        return None

    return uid
# ============================================================
# FORMAT KẾT QUẢ
# ============================================================

def format_results(
    results
):

    lines = []

    lines.append(
        "╭────────────────────────╮"
    )

    lines.append(
        "│  🆔 <b>FACEBOOK UID</b>   │"
    )

    lines.append(
        "╰────────────────────────╯"
    )

    lines.append("")

    total = len(results)

    success = sum(
        1
        for item in results
        if item.get("success")
    )

    failed = total - success

    lines.append(
        f"📊 Tổng: <b>{total}</b>  "
        f"✅ {success}  ❌ {failed}"
    )

    lines.append("")

    for index, item in enumerate(
        results,
        start=1
    ):

        url = esc(
            item.get(
                "url",
                ""
            )
        )

        uid = item.get(
            "uid"
        )

        if uid:

            lines.append(
                f"<b>{index}.</b> "
                f"🆔 <code>{esc(uid)}</code>"
            )

            lines.append(
                f"🔗 <a href=\"{url}\">Mở Facebook</a>"
            )

            lines.append(
                "✅ <i>Thành công</i>"
            )

        else:

            lines.append(
                f"<b>{index}.</b> "
                "❌ <b>Không tìm thấy UID</b>"
            )

            lines.append(
                f"🔗 <a href=\"{url}\">Mở Facebook</a>"
            )

            if item.get("error"):

                error = esc(
                    item.get(
                        "error"
                    )
                )[:500]

                lines.append(
                    f"⚠️ <code>{error}</code>"
                )

            else:

                lines.append(
                    "⚠️ <i>Facebook không trả về ID.</i>"
                )

        lines.append("")

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🔄 <b>GET UID SẴN SÀNG</b>"
    )

    lines.append(
        "📥 Gửi link Facebook tiếp theo."
    )

    lines.append(
        "💡 Có thể gửi nhiều link cùng lúc."
    )

    lines.append(
        "🛑 <code>/stop</code> để dừng."
    )

    return "\n".join(
        lines
    )


# ============================================================
# REGISTER
# ============================================================

def register(
    bot,
    notify_bot
):

    sessions = get_sessions(
        bot
    )

    # ========================================================
    # /getuidfb
    # ========================================================

    @bot.on(
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?$"
        )
    )
    async def getuid_start(event):

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


        user_id = event.sender_id

        # ----------------------------------------------------
        # Session RIÊNG cho user
        # ----------------------------------------------------

        sessions[user_id] = {
            "command": "getuidfb",
            "running": True,
            "processing": False,
        }

        await event.reply(
            "╭─────────────────────╮\n"
            "│  🔎 <b>FACEBOOK UID</b>  │\n"
            "╰─────────────────────╯\n\n"

            "📥 <b>Vui lòng gửi link Facebook.</b>\n\n"

            "🔹 1 link → Get 1\n"
            "🔹 2 link → Get 2\n"
            "🔹 Nhiều link → Get lần lượt\n"
            "🔹 Không cần xuống dòng\n\n"

            "💡 Ví dụ:\n"
            "<code>"
            "https://facebook.com/a"
            "https://facebook.com/b"
            "https://facebook.com/c"
            "</code>\n\n"

            "🔄 Sau khi xong bot tiếp tục chờ link.\n"
            "🛑 <b>/stop</b> → Dừng",
            
            parse_mode="html"
        )

    # ========================================================
    # NHẬN LINK
    # ========================================================

    @bot.on(
        events.NewMessage()
    )
    async def getuid_receive(event):

        user_id = event.sender_id

        text = event.raw_text.strip()

        if not text:
            return

        # ----------------------------------------------------
        # Không bắt command
        # ----------------------------------------------------

        if text.startswith("/"):
            return

        # ----------------------------------------------------
        # Session riêng user
        # ----------------------------------------------------

        session = sessions.get(
            user_id
        )

        if not session:
            return

        # ----------------------------------------------------
        # Không phải getuidfb
        # ----------------------------------------------------

        if session.get(
            "command"
        ) != "getuidfb":

            return

        # ----------------------------------------------------
        # Session đã stop
        # ----------------------------------------------------

        if not session.get(
            "running",
            False
        ):

            return

        # ----------------------------------------------------
        # Nếu user đang xử lý
        # ----------------------------------------------------

        if session.get(
            "processing",
            False
        ):

            return

        # ----------------------------------------------------
        # Tách URL
        # ----------------------------------------------------

        urls = extract_urls(
            text
        )

        if not urls:

            await event.reply(
                "❌ <b>Không tìm thấy link Facebook.</b>\n\n"
                "📥 Gửi một hoặc nhiều link.\n"
                "💡 Không cần xuống dòng.\n\n"
                "🔄 Bot vẫn đang chờ link.",
                parse_mode="html"
            )

            return

        # ----------------------------------------------------
        # Đánh dấu đang xử lý
        # ----------------------------------------------------

        session["processing"] = True

        # Task xử lý batch được quản lý tập trung.
        track_current_task(user_id)

        total = len(urls)

        # ----------------------------------------------------
        # Message duy nhất
        # ----------------------------------------------------

        progress = await event.reply(
            "╭─────────────────────╮\n"
            "│  🔎 <b>GET FACEBOOK UID</b>  │\n"
            "╰─────────────────────╯\n\n"

            f"📊 <b>Đã nhận:</b> {total} link\n"
            "⚙️ <b>Đang xử lý...</b>",
            
            parse_mode="html"
        )

        results = []

        # ====================================================
        # XỬ LÝ TỪNG LINK
        # ====================================================

        for index, url in enumerate(
            urls,
            start=1
        ):

            # ------------------------------------------------
            # Lấy session mới nhất
            # ------------------------------------------------

            session = sessions.get(
                user_id
            )

            if not session:
                return

            # ------------------------------------------------
            # /stop
            # ------------------------------------------------

            if not session.get(
                "running",
                False
            ):

                try:

                    await progress.edit(
                        "🛑 <b>Đã dừng Get UID.</b>\n\n"
                        "Dùng <code>/getuidfb</code> "
                        "để bắt đầu lại.",
                        parse_mode="html"
                    )

                except Exception:
                    pass

                return

            # ------------------------------------------------
            # Command khác đã thay thế
            # ------------------------------------------------

            if session.get(
                "command"
            ) != "getuidfb":

                return

            # ------------------------------------------------
            # Cập nhật progress
            # ------------------------------------------------

            try:

                await progress.edit(
                    "╭─────────────────────╮\n"
                    "│  🔎 <b>GET FACEBOOK UID</b>  │\n"
                    "╰─────────────────────╯\n\n"

                    f"📊 <b>Tiến trình:</b> "
                    f"{index}/{total}\n\n"

                    f"🔗 <code>{esc(url)}</code>\n\n"

                    "⏳ Đang lấy UID...",
                    
                    parse_mode="html"
                )

            except Exception:
                pass

            # ------------------------------------------------
            # Get UID
            # ------------------------------------------------

            try:

                result = await get_uid(
                    event,
                    url,
                    notify_bot
                )

                # ============================================================
                # LỌC UID SAU KHI GET RESULT
                # Không thay đổi API / get_uid()
                # ============================================================

                if result.get("uid"):

                    clean_uid = clean_facebook_uid(
                        result.get("uid")
                    )

                    if clean_uid:

                        result["uid"] = clean_uid
                        result["success"] = True

                    else:

                        result["uid"] = None
                        result["success"] = False

                        result["error"] = (
                            "UID trả về không hợp lệ."
                        )

                results.append(
                    result
                )

            except Exception as e:

                print(
                    f"[GETUID LOOP] {e}"
                )

                results.append(
                    {
                        "url": url,
                        "uid": None,
                        "success": False,
                        "error": str(e),
                    }
                )

        # ====================================================
        # KIỂM TRA SESSION
        # ====================================================

        session = sessions.get(
            user_id
        )

        if not session:
            return

        if not session.get(
            "running",
            False
        ):

            return

        # ====================================================
        # HIỂN THỊ KẾT QUẢ
        # ====================================================

        result_message = format_results(
            results
        )

        try:

            await progress.edit(
                result_message,
                parse_mode="html"
            )

        except Exception as e:

            print(
                f"[RESULT EDIT] {e}"
            )

        # ----------------------------------------------------
        # Cho phép nhận batch mới
        # ----------------------------------------------------

        session["processing"] = False

        session["running"] = True

        session["command"] = "getuidfb"