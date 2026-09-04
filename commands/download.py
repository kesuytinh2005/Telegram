# ============================================================
# commands/download.py
# DRAGON BOT - DOWNLOAD CENTER
#
# TikTok:
#   /download
#       -> TikTok
#           -> Tải video
#           -> Tải profile / playlist
#
# TikTok playlist/profile sẽ gọi trực tiếp tiktok.py
# ============================================================

import os
import re
import asyncio
import inspect

import yt_dlp

from datetime import datetime

from telethon import events, Button
from telethon.tl.types import DocumentAttributeVideo

from core.task_manager import (
    replace_user_tasks,
    track_current_task,
)


# ============================================================
# COMMAND INFO
# ============================================================

COMMAND_INFO = {
    "command": "download",
    "category": "📥 DOWNLOAD",
    "title": "Tải Video",

    "description": (
        "Tải video từ nhiều nền tảng bằng yt-dlp "
        "và module TikTok riêng."
    ),

    "usage": "/download",

    "examples": [
        "/download",
    ],

    "details": [
        "Gửi /download để mở menu tải.",
        "TikTok video dùng trình tải video.",
        "TikTok profile/playlist chạy bằng tiktok.py.",
        "YouTube video dùng yt-dlp.",
        "YouTube playlist/channel dùng yt-dlp.",
        "Dùng /stop để dừng tiến trình.",
    ],

    "supported": [
        "TikTok",
        "YouTube",
        "Facebook",
        "Instagram",
        "X",
        "Reddit",
        "Pinterest",
        "Twitch",
        "Vimeo",
    ],
}


# ============================================================
# SESSION
# ============================================================

def get_sessions(bot):

    if not hasattr(
        bot,
        "_dragon_download_sessions"
    ):
        bot._dragon_download_sessions = {}

    return bot._dragon_download_sessions


def set_session(
    bot,
    user_id,
    **kwargs
):

    sessions = get_sessions(bot)

    current = sessions.setdefault(
        user_id,
        {}
    )

    current.update(
        kwargs
    )

    return current


def get_session(
    bot,
    user_id
):

    return get_sessions(
        bot
    ).get(
        user_id
    )


def clear_session(
    bot,
    user_id
):

    get_sessions(
        bot
    ).pop(
        user_id,
        None
    )


# ============================================================
# MENU
# ============================================================

def main_menu():

    return [
        [
            Button.inline(
                "🎵 TikTok",
                b"dl:tiktok"
            ),
            Button.inline(
                "▶️ YouTube",
                b"dl:youtube"
            ),
        ],
        [
            Button.inline(
                "🌐 Nền tảng khác",
                b"dl:other"
            ),
        ],
        [
            Button.inline(
                "❌ Đóng",
                b"dl:close"
            ),
        ],
    ]


def tiktok_menu():

    return [
        [
            Button.inline(
                "🎬 Tải video",
                b"dl:tt_video"
            ),
        ],
        [
            Button.inline(
                "📚 Tải profile / playlist",
                b"dl:tt_playlist"
            ),
        ],
        [
            Button.inline(
                "⬅️ Quay lại",
                b"dl:back"
            ),
        ],
    ]


def youtube_menu():

    return [
        [
            Button.inline(
                "🎬 Tải video",
                b"dl:yt_video"
            ),
        ],
        [
            Button.inline(
                "📺 Tải playlist / channel",
                b"dl:yt_channel"
            ),
        ],
        [
            Button.inline(
                "⬅️ Quay lại",
                b"dl:back"
            ),
        ],
    ]


# ============================================================
# TEXT
# ============================================================

MAIN_TEXT = """
╭────────────────────────────╮
│      📥 <b>DOWNLOAD CENTER</b>      │
╰────────────────────────────╯

🚀 <b>Chọn loại tải xuống</b>

🎵 <b>TIKTOK</b>
├ 🎬 Tải video
└ 📚 Tải profile / playlist

▶️ <b>YOUTUBE</b>
├ 🎬 Tải video
└ 📺 Tải playlist / channel

🌐 <b>NỀN TẢNG KHÁC</b>
└ 🔗 Nhập trực tiếp link video

━━━━━━━━━━━━━━━━━━━━
💡 <i>Chọn một chức năng bên dưới.</i>
"""


# ============================================================
# URL
# ============================================================

def is_url(text):

    return bool(
        re.match(
            r"^https?://",
            text.strip(),
            re.I
        )
    )


def clean_username(text):

    text = text.strip()

    text = re.sub(
        r"^https?://(www\.)?tiktok\.com/@?",
        "",
        text,
        flags=re.I
    )

    text = text.replace(
        "@",
        ""
    )

    text = text.split(
        "?",
        1
    )[0]

    text = text.split(
        "/",
        1
    )[0]

    return text.strip()


def clean_youtube_username(text):

    text = text.strip()

    text = re.sub(
        r"^https?://(www\.)?youtube\.com/",
        "",
        text,
        flags=re.I
    )

    text = text.rstrip("/")

    return text


# ============================================================
# CAPTION
# ============================================================

def format_size(value):

    if not value:
        return "❓"

    try:
        size = float(value)
    except Exception:
        return "❓"

    for unit in (
        "B",
        "KB",
        "MB",
        "GB",
        "TB"
    ):

        if size < 1024:

            return (
                f"{size:.2f} {unit}"
            )

        size /= 1024

    return f"{size:.2f} PB"


def format_duration(value):

    if not value:
        return "❓"

    try:
        value = int(value)
    except Exception:
        return "❓"

    hours, remain = divmod(
        value,
        3600
    )

    minutes, seconds = divmod(
        remain,
        60
    )

    if hours:

        return (
            f"{hours}h "
            f"{minutes}m "
            f"{seconds}s"
        )

    return (
        f"{minutes}m "
        f"{seconds}s"
    )


def make_caption(info):

    upload_date = info.get(
        "upload_date"
    )

    if upload_date:

        try:

            upload_date = datetime.strptime(
                upload_date,
                "%Y%m%d"
            ).strftime(
                "%d/%m/%Y"
            )

        except Exception:
            pass

    else:

        upload_date = "❓"

    tags = info.get(
        "tags"
    )

    if not tags:

        title = info.get(
            "title",
            ""
        )

        tags = [
            word.strip(
                "#,.!? "
            )

            for word in title.split()

            if len(word) > 2
        ]

    tags = tags[:10]

    tags_text = (
        " ".join(
            f"#{str(x).lstrip('#')}"
            for x in tags
        )
        if tags
        else "❌"
    )

    return (
        "<b>📌 THÔNG TIN VIDEO</b>\n\n"

        f"🎬 <b>Tiêu đề:</b>\n"
        f"{info.get('title', '?')}\n\n"

        f"🆔 <b>ID:</b> "
        f"<code>{info.get('id', '?')}</code>\n"

        f"📺 <b>Độ phân giải:</b> "
        f"<code>"
        f"{info.get('width', '?')} × "
        f"{info.get('height', '?')}"
        f"</code>\n"

        f"⏱ <b>Thời lượng:</b> "
        f"<code>"
        f"{format_duration(info.get('duration'))}"
        f"</code>\n"

        f"📦 <b>Kích thước:</b> "
        f"<code>"
        f"{format_size(info.get('filesize') or info.get('filesize_approx'))}"
        f"</code>\n"

        f"👤 <b>Uploader:</b> "
        f"{info.get('uploader', '?')}\n"

        f"📅 <b>Ngày:</b> "
        f"<code>{upload_date}</code>\n"

        f"👁 <b>Lượt xem:</b> "
        f"{info.get('view_count', '?')}\n"

        f"👍 <b>Lượt thích:</b> "
        f"{info.get('like_count', '?')}\n\n"

        f"🏷 <b>Tags:</b>\n"
        f"{tags_text}\n\n"

        f"🌐 <b>Nền tảng:</b> "
        f"{info.get('extractor_key', '?')}\n\n"

        f"🔗 <a href=\"{info.get('webpage_url', '#')}\">"
        f"Mở video gốc"
        f"</a>"
    )


# ============================================================
# YT-DLP BASE OPTIONS
# ============================================================

def base_options():

    return {

        "format":
            "bestvideo+bestaudio/best",

        "merge_output_format":
            "mp4",

        "quiet":
            True,

        "no_warnings":
            True,

        "ignoreerrors":
            False,

        "retries":
            5,

        "fragment_retries":
            5,

        "file_access_retries":
            5,

        "extractor_retries":
            3,

        "socket_timeout":
            30,

        "concurrent_fragment_downloads":
            1,

        "noplaylist":
            True,

        "outtmpl": (
            "downloads/"
            "%(extractor_key)s_"
            "%(id)s.%(ext)s"
        ),
    }


# ============================================================
# TIKTOK OPTIONS
# ============================================================

def tiktok_options():

    options = base_options()

    options.update({

        "extractor_args": {

            "tiktok": {

                "app_name": [
                    "musical_ly"
                ],

            },

        },

        "http_headers": {

            "User-Agent": (
                "Mozilla/5.0 "
                "(Linux; Android 15; Mobile) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Mobile Safari/537.36"
            ),

            "Referer":
                "https://www.tiktok.com/",
        },
    })

    return options


# ============================================================
# YOUTUBE OPTIONS
# ============================================================

def youtube_options():

    options = base_options()

    options.update({

        "http_headers": {

            "User-Agent": (
                "Mozilla/5.0 "
                "(Linux; Android 15; Mobile) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Mobile Safari/537.36"
            ),
        },

        "extractor_args": {

            "youtube": {

                "player_client": [
                    "android"
                ]

            }

        },

    })

    return options


# ============================================================
# DOWNLOAD ONE VIDEO
# ============================================================

async def download_one(
    event,
    url,
    notify_bot=None,
    options=None
):

    user = await event.get_sender()

    msg = await event.reply(
        "⏳ <b>Đang phân tích video...</b>",
        parse_mode="html"
    )

    filepath = None

    try:

        if options is None:

            options = base_options()

        os.makedirs(
            "downloads",
            exist_ok=True
        )

        def run():

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                return ydl.extract_info(
                    url,
                    download=True
                )

        info = await asyncio.to_thread(
            run
        )

        if not info:

            raise RuntimeError(
                "yt-dlp không trả về dữ liệu."
            )

        filepath = yt_dlp.YoutubeDL(
            options
        ).prepare_filename(
            info
        )

        # ----------------------------------------------------
        # Tìm file sau merge
        # ----------------------------------------------------

        if not os.path.exists(
            filepath
        ):

            base = os.path.splitext(
                filepath
            )[0]

            for ext in (
                ".mp4",
                ".mkv",
                ".webm"
            ):

                candidate = (
                    base + ext
                )

                if os.path.exists(
                    candidate
                ):

                    filepath = candidate
                    break

        if not os.path.exists(
            filepath
        ):

            raise FileNotFoundError(
                "Không tìm thấy file sau khi tải."
            )

        await msg.edit(
            "📤 <b>Đang gửi video...</b>",
            parse_mode="html"
        )

        duration = int(
            info.get(
                "duration",
                0
            ) or 0
        )

        width = int(
            info.get(
                "width",
                0
            ) or 0
        )

        height = int(
            info.get(
                "height",
                0
            ) or 0
        )

        attributes = [
            DocumentAttributeVideo(
                duration=duration,
                w=width,
                h=height,
                supports_streaming=True
            )
        ]

        caption = make_caption(
            info
        )

        await event.client.send_file(
            event.chat_id,
            file=filepath,
            caption=caption,
            attributes=attributes,
            supports_streaming=True,
            force_document=False,
            parse_mode="html"
        )

        if notify_bot:

            try:

                await notify_bot(
                    user,
                    "/download",
                    result=caption,
                    file_path=filepath
                )

            except Exception as e:

                print(
                    f"[ADMIN] {e}"
                )

        await msg.delete()

        return info

    except Exception as e:

        print(
            f"[DOWNLOAD ERROR] {e}"
        )

        await msg.edit(
            "❌ <b>KHÔNG THỂ TẢI VIDEO</b>\n\n"
            f"⚠️ <code>{str(e)[:2500]}</code>\n\n"
            "💡 Nếu là TikTok, hãy thử lại sau.",
            parse_mode="html"
        )

        return None

    finally:

        if filepath:

            try:

                if os.path.exists(
                    filepath
                ):

                    os.remove(
                        filepath
                    )

            except Exception as e:

                print(
                    f"[CLEANUP] {e}"
                )


# ============================================================
# TIKTOK VIDEO
# ============================================================

async def process_tiktok_video(
    event,
    url,
    notify_bot
):

    if not is_url(url):

        await event.reply(
            "❌ Vui lòng gửi <b>link TikTok video</b>.",
            parse_mode="html"
        )

        return

    await download_one(
        event,
        url,
        notify_bot,
        tiktok_options()
    )


# ============================================================
# TIKTOK PLAYLIST / PROFILE
#
# QUAN TRỌNG:
#
# Không xử lý TikTok profile tại đây.
#
# download.py chỉ gọi tiktok.py.
# ============================================================

async def process_tiktok_playlist(
    event,
    text,
    bot,
    notify_bot=None
):

    user_id = event.sender_id

    text = text.strip()

    if not text:

        await event.reply(
            "❌ <b>Username / link TikTok không hợp lệ.</b>",
            parse_mode="html"
        )

        return

    # ========================================================
    # KIỂM TRA SESSION
    # ========================================================

    session = get_session(
        bot,
        user_id
    )

    if not session:

        return

    if not session.get(
        "running",
        False
    ):

        return

    # ========================================================
    # IMPORT TIKTOK.PY
    # ========================================================

    try:

        import tiktok

    except Exception as e:

        print(
            f"[TIKTOK IMPORT ERROR] {e}"
        )

        await event.reply(
            "❌ <b>Không thể load tiktok.py</b>\n\n"
            f"⚠️ <code>{str(e)[:2000]}</code>",
            parse_mode="html"
        )

        return

    # ========================================================
    # HIỂN THỊ
    # ========================================================

    msg = await event.reply(
        "╭────────────────────────────╮\n"
        "│   🎵 <b>TIKTOK PLAYLIST</b>   │\n"
        "╰────────────────────────────╯\n\n"

        f"🔗 <b>Input:</b>\n"
        f"<code>{text[:1000]}</code>\n\n"

        "🚀 <b>Đang chuyển sang tiktok.py...</b>\n\n"
        "🛑 Dùng <code>/stop</code> để dừng.",
        parse_mode="html"
    )

    # ========================================================
    # GỌI HÀM TIKTOK.PY
    # ========================================================

    try:

        # ----------------------------------------------------
        # Tìm hàm phù hợp trong tiktok.py
        #
        # Ưu tiên:
        #
        # process_tiktok_profile
        # download_profile
        # download_playlist
        # main
        # ----------------------------------------------------

        function = None

        possible_functions = (
            "process_tiktok_profile",
            "download_profile",
            "download_playlist",
            "process_profile",
            "run",
            "main",
        )

        for function_name in possible_functions:

            candidate = getattr(
                tiktok,
                function_name,
                None
            )

            if callable(candidate):

                function = candidate

                print(
                    f"[TIKTOK] Using "
                    f"tiktok.{function_name}()"
                )

                break

        if function is None:

            raise RuntimeError(
                "Không tìm thấy hàm chạy playlist/profile "
                "trong tiktok.py.\n\n"
                "Hãy export một trong các hàm:\n"
                "process_tiktok_profile()\n"
                "download_profile()\n"
                "download_playlist()"
            )

        # ----------------------------------------------------
        # Đọc signature
        # ----------------------------------------------------

        try:

            signature = inspect.signature(
                function
            )

            parameters = list(
                signature.parameters.values()
            )

        except Exception:

            parameters = []

        # ----------------------------------------------------
        # Chuẩn bị arguments
        #
        # Hỗ trợ nhiều kiểu tiktok.py
        # ----------------------------------------------------

        kwargs = {}

        args = []

        parameter_names = {
            p.name
            for p in parameters
        }

        # event
        if "event" in parameter_names:
            kwargs["event"] = event

        # username / text / url
        if "username" in parameter_names:

            kwargs["username"] = text

        elif "text" in parameter_names:

            kwargs["text"] = text

        elif "url" in parameter_names:

            kwargs["url"] = text

        elif "video_url" in parameter_names:

            kwargs["video_url"] = text

        # bot
        if "bot" in parameter_names:

            kwargs["bot"] = bot

        # notify
        if "notify_bot" in parameter_names:

            kwargs["notify_bot"] = notify_bot

        # user_id
        if "user_id" in parameter_names:

            kwargs["user_id"] = user_id

        # ----------------------------------------------------
        # Nếu function không có named args,
        # thử kiểu phổ biến:
        #
        # function(event, text, bot, notify_bot)
        # ----------------------------------------------------

        if not kwargs:

            required = [
                p
                for p in parameters
                if (
                    p.default
                    is inspect.Parameter.empty
                    and
                    p.kind
                    in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                )
            ]

            count = len(required)

            if count >= 4:

                args = [
                    event,
                    text,
                    bot,
                    notify_bot,
                ]

            elif count == 3:

                args = [
                    event,
                    text,
                    bot,
                ]

            elif count == 2:

                args = [
                    event,
                    text,
                ]

            elif count == 1:

                args = [
                    text,
                ]

            elif count == 0:

                args = []

        # ====================================================
        # CHẠY
        # ====================================================

        result = function(
            *args,
            **kwargs
        )

        # ----------------------------------------------------
        # Nếu là coroutine -> await
        # ----------------------------------------------------

        if inspect.isawaitable(
            result
        ):

            result = await result

        print(
            f"[TIKTOK] tiktok.py finished: "
            f"{type(result).__name__}"
        )

        # ====================================================
        # KIỂM TRA USER CÓ STOP KHÔNG
        # ====================================================

        current_session = get_session(
            bot,
            user_id
        )

        if not current_session:

            return

        if not current_session.get(
            "running",
            False
        ):

            return

        # ====================================================
        # HOÀN TẤT
        # ====================================================

        try:

            await msg.edit(
                "╭────────────────────────────╮\n"
                "│   ✅ <b>TIKTOK HOÀN TẤT</b>   │\n"
                "╰────────────────────────────╯\n\n"

                f"📌 <b>Input:</b>\n"
                f"<code>{text[:1000]}</code>\n\n"

                "🎵 Module <code>tiktok.py</code> "
                "đã xử lý playlist/profile.\n\n"

                "🛑 Có thể dùng <code>/stop</code> "
                "để thoát.",
                parse_mode="html"
            )

        except Exception:
            pass

    except asyncio.CancelledError:

        print(
            f"[TIKTOK] User {user_id} task cancelled"
        )

        raise

    except Exception as e:

        print(
            f"[TIKTOK PLAYLIST ERROR] {e}"
        )

        try:

            await msg.edit(
                "❌ <b>TIKTOK PLAYLIST ERROR</b>\n\n"
                f"⚠️ <code>{str(e)[:2500]}</code>",
                parse_mode="html"
            )

        except Exception:
            pass


# ============================================================
# YOUTUBE CHANNEL / PLAYLIST
# ============================================================

async def process_youtube_channel(
    event,
    text
):

    text = text.strip()

    if is_url(text):

        url = text

    else:

        text = clean_youtube_username(
            text
        )

        if text.startswith("@"):

            url = (
                f"https://www.youtube.com/{text}"
            )

        else:

            url = (
                f"https://www.youtube.com/@{text}"
            )

    msg = await event.reply(
        "🔎 <b>Đang quét YouTube...</b>\n\n"
        f"🔗 <code>{url}</code>",
        parse_mode="html"
    )

    try:

        options = youtube_options()

        options["noplaylist"] = False

        options["extract_flat"] = True

        def run():

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                return ydl.extract_info(
                    url,
                    download=False
                )

        info = await asyncio.to_thread(
            run
        )

        if not info:

            raise RuntimeError(
                "Không lấy được channel/playlist."
            )

        entries = [
            x
            for x in (
                info.get("entries")
                or []
            )
            if x
        ]

        if not entries:

            await msg.edit(
                "⚠️ Không tìm thấy video.",
                parse_mode="html"
            )

            return

        await msg.edit(
            "📺 <b>YOUTUBE</b>\n\n"
            f"🎬 Tìm thấy: "
            f"<b>{len(entries)}</b> video\n\n"
            "📥 Bắt đầu tải...",
            parse_mode="html"
        )

        entries = entries[:20]

        for index, entry in enumerate(
            entries,
            1
        ):

            video_id = entry.get(
                "id"
            )

            if not video_id:
                continue

            video_url = (
                "https://www.youtube.com/"
                f"watch?v={video_id}"
            )

            await event.reply(
                f"📥 <b>YouTube "
                f"{index}/{len(entries)}</b>",
                parse_mode="html"
            )

            await download_one(
                event,
                video_url,
                None,
                youtube_options()
            )

    except asyncio.CancelledError:

        raise

    except Exception as e:

        print(
            f"[YOUTUBE CHANNEL ERROR] {e}"
        )

        try:

            await msg.edit(
                "❌ <b>Không thể đọc YouTube</b>\n\n"
                f"⚠️ <code>{str(e)[:2500]}</code>",
                parse_mode="html"
            )

        except Exception:
            pass


# ============================================================
# REGISTER
# ============================================================

def register(
    bot,
    notify_bot
):

    # ========================================================
    # /download
    # ========================================================

    @bot.on(
        events.NewMessage(
            pattern=r"^/download(?:@\w+)?$"
        )
    )
    async def download_start(event):

        user_id = event.sender_id

        # ----------------------------------------------------
        # Command mới:
        #
        # HỦY task cũ
        # ----------------------------------------------------

        await replace_user_tasks(
            user_id
        )

        # ----------------------------------------------------
        # Xóa session command cũ
        # ----------------------------------------------------

        for _attr in (
            "_dragon_sessions",
            "_dragon_download_sessions"
        ):

            sessions = getattr(
                bot,
                _attr,
                None
            )

            if isinstance(
                sessions,
                dict
            ):

                sessions.pop(
                    user_id,
                    None
                )

        # ----------------------------------------------------
        # Power session
        # ----------------------------------------------------

        try:

            from core.power.session import clear_session

            clear_session(
                bot,
                user_id
            )

        except Exception:
            pass

        # ----------------------------------------------------
        # Tạo session mới
        # ----------------------------------------------------

        set_session(
            bot,
            user_id,

            command="download",

            state="menu",

            running=True
        )

        await event.reply(
            MAIN_TEXT,
            buttons=main_menu(),
            parse_mode="html"
        )


    # ========================================================
    # CALLBACK
    # ========================================================

    @bot.on(
        events.CallbackQuery(
            pattern=b"dl:"
        )
    )
    async def download_callback(event):

        user_id = event.sender_id

        data = event.data.decode(
            "utf-8"
        )

        # ====================================================
        # CLOSE
        # ====================================================

        if data == "dl:close":

            clear_session(
                bot,
                user_id
            )

            await event.edit(
                "✅ <b>Đã đóng Download Center.</b>",
                parse_mode="html"
            )

            return

        # ====================================================
        # BACK
        # ====================================================

        if data == "dl:back":

            set_session(
                bot,
                user_id,

                state="menu",

                running=True
            )

            await event.edit(
                MAIN_TEXT,
                buttons=main_menu(),
                parse_mode="html"
            )

            return

        # ====================================================
        # TIKTOK
        # ====================================================

        if data == "dl:tiktok":

            set_session(
                bot,
                user_id,

                state="tiktok_menu",

                running=True
            )

            await event.edit(
                """
╭────────────────────────────╮
│       🎵 <b>TIKTOK</b>       │
╰────────────────────────────╯

Chọn chức năng:

🎬 <b>Tải video</b>
└ Gửi link TikTok video

📚 <b>Tải profile / playlist</b>
└ Chạy module <code>tiktok.py</code>
└ Nhập username hoặc link profile

━━━━━━━━━━━━━━━━━━━━
🛑 <code>/stop</code> để dừng.
""",
                buttons=tiktok_menu(),
                parse_mode="html"
            )

            return

        # ====================================================
        # YOUTUBE
        # ====================================================

        if data == "dl:youtube":

            set_session(
                bot,
                user_id,

                state="youtube_menu",

                running=True
            )

            await event.edit(
                """
╭────────────────────────────╮
│       ▶️ <b>YOUTUBE</b>       │
╰────────────────────────────╯

Chọn chức năng:

🎬 <b>Tải video</b>
└ Gửi link video

📺 <b>Playlist / Channel</b>
└ Nhập @username
└ Hoặc gửi link channel/playlist
""",
                buttons=youtube_menu(),
                parse_mode="html"
            )

            return

        # ====================================================
        # OTHER
        # ====================================================

        if data == "dl:other":

            set_session(
                bot,
                user_id,

                state="other",

                running=True
            )

            await event.edit(
                """
╭────────────────────────────╮
│      🌐 <b>NỀN TẢNG KHÁC</b>      │
╰────────────────────────────╯

🔗 Gửi link video.

Bot sẽ tự nhận diện nền tảng
và dùng yt-dlp để tải.

🛑 <code>/stop</code> để dừng.
""",
                parse_mode="html"
            )

            return

        # ====================================================
        # TIKTOK VIDEO
        # ====================================================

        if data == "dl:tt_video":

            set_session(
                bot,
                user_id,

                state="tt_video",

                running=True
            )

            await event.edit(
                "🎬 <b>TIKTOK VIDEO</b>\n\n"
                "🔗 Gửi link TikTok video.\n\n"
                "🛑 <code>/stop</code> để dừng.",
                parse_mode="html"
            )

            return

        # ====================================================
        # TIKTOK PLAYLIST
        #
        # Đây chính là nhánh gọi tiktok.py
        # ====================================================

        if data == "dl:tt_playlist":

            set_session(
                bot,
                user_id,

                state="tt_playlist",

                running=True
            )

            await event.edit(
                """
╭────────────────────────────╮
│   📚 <b>TIKTOK PLAYLIST</b>   │
╰────────────────────────────╯

🔗 <b>Nhập username hoặc link profile</b>

Ví dụ:

<code>@nguyenvanloiofficial</code>

hoặc:

<code>nguyenvanloiofficial</code>

hoặc:

<code>https://www.tiktok.com/@username</code>

━━━━━━━━━━━━━━━━━━━━
🚀 Sau khi gửi, bot sẽ chạy
module <code>tiktok.py</code>.

🛑 <code>/stop</code> để dừng.
""",
                parse_mode="html"
            )

            return

        # ====================================================
        # YOUTUBE VIDEO
        # ====================================================

        if data == "dl:yt_video":

            set_session(
                bot,
                user_id,

                state="yt_video",

                running=True
            )

            await event.edit(
                "🎬 <b>YOUTUBE VIDEO</b>\n\n"
                "🔗 Gửi link video YouTube.\n\n"
                "🛑 <code>/stop</code> để dừng.",
                parse_mode="html"
            )

            return

        # ====================================================
        # YOUTUBE CHANNEL
        # ====================================================

        if data == "dl:yt_channel":

            set_session(
                bot,
                user_id,

                state="yt_channel",

                running=True
            )

            await event.edit(
                """
╭────────────────────────────╮
│   📺 <b>YOUTUBE PLAYLIST</b>   │
╰────────────────────────────╯

👤 Nhập username:

<code>@MrBeast</code>

hoặc:

<code>MrBeast</code>

Hoặc gửi link channel / playlist.

🛑 <code>/stop</code> để dừng.
""",
                parse_mode="html"
            )

            return

        await event.answer()


    # ========================================================
    # RECEIVE TEXT
    # ========================================================

    @bot.on(
        events.NewMessage()
    )
    async def download_receive(event):

        text = event.raw_text.strip()

        if not text:
            return

        # Không xử lý command
        if text.startswith("/"):
            return

        user_id = event.sender_id

        session = get_session(
            bot,
            user_id
        )

        if not session:
            return

        if session.get(
            "command"
        ) != "download":

            return

        if not session.get(
            "running",
            False
        ):

            return

        state = session.get(
            "state"
        )

        # ====================================================
        # ĐĂNG KÝ TASK HIỆN TẠI
        #
        # /stop sẽ cancel task này.
        # ====================================================

        current_task = asyncio.current_task()

        if current_task:

            track_current_task(
                user_id,
                current_task
            )

        # ====================================================
        # TIKTOK VIDEO
        # ====================================================

        if state == "tt_video":

            if not is_url(text):

                await event.reply(
                    "❌ Hãy gửi link TikTok.",
                    parse_mode="html"
                )

                return

            await process_tiktok_video(
                event,
                text,
                notify_bot
            )

            return

        # ====================================================
        # TIKTOK PLAYLIST / PROFILE
        #
        # GỌI tiktok.py
        # ====================================================

        if state == "tt_playlist":

            await process_tiktok_playlist(
                event,
                text,
                bot,
                notify_bot
            )

            return

        # ====================================================
        # YOUTUBE VIDEO
        # ====================================================

        if state == "yt_video":

            if not is_url(text):

                await event.reply(
                    "❌ Hãy gửi link YouTube.",
                    parse_mode="html"
                )

                return

            await download_one(
                event,
                text,
                notify_bot,
                youtube_options()
            )

            return

        # ====================================================
        # YOUTUBE CHANNEL
        # ====================================================

        if state == "yt_channel":

            await process_youtube_channel(
                event,
                text
            )

            return

        # ====================================================
        # OTHER
        # ====================================================

        if state == "other":

            if not is_url(text):

                await event.reply(
                    "❌ <b>URL không hợp lệ.</b>\n\n"
                    "Hãy gửi link bắt đầu bằng "
                    "<code>https://</code>",
                    parse_mode="html"
                )

                return

            await download_one(
                event,
                text,
                notify_bot,
                base_options()
            )

            return