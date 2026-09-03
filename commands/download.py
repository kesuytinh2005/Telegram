# ============================================================
# commands/download.py
# DRAGON BOT - DOWNLOAD
# ============================================================

import os
import re
import asyncio
import yt_dlp

from datetime import datetime

from telethon import events, Button
from telethon.tl.types import DocumentAttributeVideo


# ============================================================
# COMMAND INFO
# ============================================================

from core.task_manager import replace_user_tasks, track_current_task

COMMAND_INFO = {
    "command": "download",
    "category": "📥 DOWNLOAD",
    "title": "Tải Video",

    "description": (
        "Tải video từ nhiều nền tảng bằng yt-dlp."
    ),

    "usage": "/download",

    "examples": [
        "/download",
    ],

    "details": [
        "Gửi /download để mở menu tải.",
        "Có thể tải video đơn hoặc profile/playlist/channel.",
        "TikTok profile chỉ cần nhập username.",
        "YouTube channel có thể nhập @username.",
        "Dùng /stop để thoát chế độ download.",
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

    current.update(kwargs)

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
                "📚 Tải profile",
                b"dl:tt_profile"
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
└ 📚 Tải profile bằng username

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

    text = text.replace(
        "https://www.tiktok.com/@",
        ""
    )

    text = text.replace(
        "https://www.tiktok.com/",
        ""
    )

    text = text.replace(
        "http://www.tiktok.com/@",
        ""
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

    size = float(value)

    for unit in (
        "B",
        "KB",
        "MB",
        "GB",
        "TB"
    ):

        if size < 1024:
            return f"{size:.2f} {unit}"

        size /= 1024

    return f"{size:.2f} PB"


def format_duration(value):

    if not value:
        return "❓"

    value = int(value)

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
        "format": "bestvideo+bestaudio/best",

        "merge_output_format": "mp4",

        "quiet": True,

        "no_warnings": True,

        "ignoreerrors": False,

        "retries": 5,

        "fragment_retries": 5,

        "file_access_retries": 5,

        "extractor_retries": 3,

        "socket_timeout": 30,

        "concurrent_fragment_downloads": 1,

        "noplaylist": True,

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

        # TikTok thay đổi response khá thường xuyên
        "extractor_args": {
            "tiktok": {
                "app_name": [
                    "musical_ly"
                ],
            }
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
                    "android",
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

        error_text = str(e)

        await msg.edit(
            "❌ <b>KHÔNG THỂ TẢI VIDEO</b>\n\n"
            f"⚠️ <code>{error_text[:2500]}</code>\n\n"
            "💡 Nếu là TikTok, hãy thử lại sau "
            "vài giây.",
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
# TIKTOK PROFILE
# ============================================================

# ============================================================
# TIKTOK PROFILE
# DOWNLOAD TOÀN BỘ VIDEO PROFILE
# ============================================================

# ============================================================
# TIKTOK PROFILE
# DOWNLOAD -> SEND NGAY -> DELETE
# ============================================================

# ============================================================
# TIKTOK PROFILE
# QUÉT LINK -> TẢI TỪNG VIDEO -> GỬI TELEGRAM -> XÓA FILE
# ============================================================

async def process_tiktok_profile(
    event,
    username,
    bot,
    notify_bot=None
):

    username = clean_username(
        username
    )

    if not username:

        await event.reply(
            "❌ <b>Username không hợp lệ.</b>",
            parse_mode="html"
        )

        return

    user_id = event.sender_id

    profile_url = (
        f"https://www.tiktok.com/@{username}"
    )

    # ========================================================
    # SESSION
    # ========================================================

    session = get_session(
        bot,
        user_id
    )

    if not session:

        return

    # ========================================================
    # MESSAGE
    # ========================================================

    msg = await event.reply(
        "╭────────────────────────────╮\n"
        "│   📚 <b>TIKTOK PROFILE</b>   │\n"
        "╰────────────────────────────╯\n\n"

        f"👤 <b>Username:</b> "
        f"<code>@{username}</code>\n\n"

        "🔎 <b>Đang quét danh sách video...</b>\n"
        "⏳ Vui lòng chờ...",
        parse_mode="html"
    )

    temp_dir = None

    try:

        # ====================================================
        # BƯỚC 1
        # CHỈ LẤY DANH SÁCH VIDEO
        # ====================================================

        scan_options = {
            "extract_flat": True,
            "quiet": True,
        }

        # ====================================================
        # SCAN PROFILE
        # ====================================================

        def scan_profile():

            with yt_dlp.YoutubeDL(
                scan_options
            ) as ydl:

                return ydl.extract_info(
                    profile_url,
                    download=False
                )

        info = await asyncio.to_thread(
            scan_profile
        )

        if not info:

            await msg.edit(
                "❌ <b>KHÔNG THỂ ĐỌC PROFILE</b>\n\n"
                f"👤 <code>@{username}</code>\n\n"
                "⚠️ yt-dlp không lấy được dữ liệu "
                "từ TikTok.",
                parse_mode="html"
            )

            return

        # ====================================================
        # LẤY ENTRIES
        # ====================================================

        entries = info.get(
            "entries"
        ) or []

        # Loại entry rỗng
        entries = [
            entry
            for entry in entries
            if entry
        ]

        if not entries:

            await msg.edit(
                "⚠️ <b>KHÔNG TÌM THẤY VIDEO</b>\n\n"
                f"👤 <code>@{username}</code>\n\n"
                "TikTok không trả về danh sách video.",
                parse_mode="html"
            )

            return

        # ====================================================
        # TÁCH URL VIDEO
        #
        # ƯU TIÊN webpage_url
        # SAU ĐÓ url
        # ====================================================

        video_urls = []

        for entry in entries:

            video_url = (
                entry.get("webpage_url")
                or entry.get("url")
            )

            # ------------------------------------------------
            # Một số trường hợp extract_flat chỉ trả về ID
            # ------------------------------------------------

            if (
                not video_url
                and entry.get("id")
            ):

                video_url = (
                    f"https://www.tiktok.com/@{username}"
                    f"/video/{entry['id']}"
                )

            if not video_url:
                continue

            # ------------------------------------------------
            # Nếu URL không phải HTTP
            # thì bỏ
            # ------------------------------------------------

            if not str(
                video_url
            ).startswith(
                (
                    "http://",
                    "https://"
                )
            ):

                continue

            video_urls.append(
                video_url
            )

        # ====================================================
        # LOẠI URL TRÙNG
        # ====================================================

        video_urls = list(
            dict.fromkeys(
                video_urls
            )
        )

        total = len(
            video_urls
        )

        if total == 0:

            await msg.edit(
                "⚠️ <b>KHÔNG TÁCH ĐƯỢC LINK VIDEO</b>\n\n"
                f"👤 <code>@{username}</code>",
                parse_mode="html"
            )

            return

        # ====================================================
        # TẠO THƯ MỤC TẠM
        # ====================================================

        import tempfile

        temp_dir = tempfile.mkdtemp(
            prefix="dragon_tiktok_"
        )

        # ====================================================
        # THỐNG KÊ
        # ====================================================

        success = 0
        failed = 0
        stopped = False

        await msg.edit(
            "╭────────────────────────────╮\n"
            "│   🚀 <b>BẮT ĐẦU TẢI PROFILE</b>   │\n"
            "╰────────────────────────────╯\n\n"

            f"👤 <b>Username:</b> "
            f"<code>@{username}</code>\n"

            f"🎬 <b>Tổng video:</b> "
            f"<code>{total}</code>\n\n"

            "📥 Tải từng video\n"
            "📤 Gửi ngay lên Telegram\n"
            "🗑 Xóa file tạm sau khi gửi\n\n"

            "🛑 <code>/stop</code> để dừng.",
            parse_mode="html"
        )

        # ====================================================
        # BƯỚC 2
        # TẢI TỪNG VIDEO
        # ====================================================

        for index, video_url in enumerate(
            video_urls,
            1
        ):

            # =================================================
            # KIỂM TRA SESSION
            # =================================================

            session = get_session(
                bot,
                user_id
            )

            if not session:

                stopped = True
                break

            if not session.get(
                "running",
                False
            ):

                stopped = True
                break

            if session.get(
                "command"
            ) != "download":

                stopped = True
                break

            # =================================================
            # THÔNG BÁO VIDEO ĐANG TẢI
            # =================================================

            try:

                await msg.edit(
                    "╭────────────────────────────╮\n"
                    "│   📥 <b>ĐANG TẢI VIDEO</b>   │\n"
                    "╰────────────────────────────╯\n\n"

                    f"👤 <code>@{username}</code>\n"

                    f"🎬 <b>Video:</b> "
                    f"<code>{index}/{total}</code>\n\n"

                    "⏳ Đang tải...\n"
                    "📤 Sau khi tải xong sẽ gửi Telegram.",
                    parse_mode="html"
                )

            except Exception:
                pass

            filepath = None

            # =================================================
            # OPTIONS DOWNLOAD VIDEO
            #
            # THEO ĐÚNG CƠ CHẾ BẠN YÊU CẦU
            # =================================================

            download_options = {

                # Chất lượng tốt nhất mà extractor trả về
                "format": "best",

                # File tạm
                "outtmpl": os.path.join(
                    temp_dir,
                    "%(id)s.%(ext)s"
                ),

                # MP4 nếu cần merge
                "merge_output_format": "mp4",

                # Không cần hiện log
                "quiet": True,

                
                # KHÔNG dùng extract_flat khi tải
                "extract_flat": False,

                # Header TikTok
                

                
            }

            # =================================================
            # DOWNLOAD
            # =================================================

            try:

                def download():

                    with yt_dlp.YoutubeDL() as ydl:
                        return ydl.extract_info(
                            video_url,
                            download=True
                        )

                video_info = await asyncio.to_thread(
                    download
                )

                if not video_info:

                    raise RuntimeError(
                        "yt-dlp không trả về thông tin video."
                    )

                # =================================================
                # TÌM FILE ĐÃ TẢI
                # =================================================

                video_id = (
                    video_info.get("id")
                    or str(index)
                )

                # Tìm file theo ID
                possible_files = []

                for filename in os.listdir(
                    temp_dir
                ):

                    full_path = os.path.join(
                        temp_dir,
                        filename
                    )

                    if not os.path.isfile(
                        full_path
                    ):
                        continue

                    # Ưu tiên file có ID
                    if video_id in filename:

                        possible_files.insert(
                            0,
                            full_path
                        )

                    else:

                        possible_files.append(
                            full_path
                        )

                if not possible_files:

                    raise FileNotFoundError(
                        "Không tìm thấy file video."
                    )

                filepath = possible_files[0]

                # =================================================
                # KIỂM TRA FILE
                # =================================================

                if (
                    not os.path.exists(
                        filepath
                    )
                    or
                    os.path.getsize(
                        filepath
                    ) <= 0
                ):

                    raise RuntimeError(
                        "File video rỗng hoặc không tồn tại."
                    )

                # =================================================
                # CẬP NHẬT TRẠNG THÁI
                # =================================================

                try:

                    await msg.edit(
                        "╭────────────────────────────╮\n"
                        "│   📤 <b>ĐANG GỬI VIDEO</b>   │\n"
                        "╰────────────────────────────╯\n\n"

                        f"👤 <code>@{username}</code>\n"

                        f"🎬 <b>Video:</b> "
                        f"<code>{index}/{total}</code>\n\n"

                        "✅ Tải thành công\n"
                        "📤 Đang gửi lên Telegram...\n"
                        "🗑 Gửi xong sẽ xóa file tạm.",
                        parse_mode="html"
                    )

                except Exception:
                    pass

                # =================================================
                # THÔNG TIN VIDEO
                # =================================================

                duration = int(
                    video_info.get(
                        "duration",
                        0
                    ) or 0
                )

                width = int(
                    video_info.get(
                        "width",
                        0
                    ) or 0
                )

                height = int(
                    video_info.get(
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
                    video_info
                )

                # =================================================
                # GỬI VIDEO NGAY
                # =================================================

                await event.client.send_file(
                    event.chat_id,
                    file=filepath,
                    caption=caption,
                    attributes=attributes,
                    supports_streaming=True,
                    force_document=False,
                    parse_mode="html"
                )

                success += 1

                # =================================================
                # ADMIN NOTIFY
                # =================================================

                if notify_bot:

                    try:

                        user = await event.get_sender()

                        await notify_bot(
                            user,
                            "/download",
                            result=caption,
                            file_path=filepath
                        )

                    except Exception as admin_error:

                        print(
                            f"[ADMIN] "
                            f"{admin_error}"
                        )

            except Exception as video_error:

                failed += 1

                print(
                    f"[TIKTOK VIDEO ERROR] "
                    f"{index}/{total} "
                    f"{video_url} -> "
                    f"{video_error}"
                )

                # =================================================
                # VIDEO LỖI -> BỎ QUA
                # =================================================

                try:

                    await event.reply(
                        "⚠️ <b>BỎ QUA VIDEO LỖI</b>\n\n"
                        f"🎬 <code>{index}/{total}</code>\n"
                        f"⚠️ <code>{str(video_error)[:800]}</code>\n\n"
                        "➡️ Tiếp tục video kế tiếp...",
                        parse_mode="html"
                    )

                except Exception:
                    pass

            finally:

                # =================================================
                # XÓA FILE NGAY SAU MỖI VIDEO
                # =================================================

                if filepath:

                    try:

                        if os.path.exists(
                            filepath
                        ):

                            os.remove(
                                filepath
                            )

                    except Exception as cleanup_error:

                        print(
                            f"[CLEANUP] "
                            f"{cleanup_error}"
                        )

                # =================================================
                # DỌN CÁC FILE PHỤ
                # .part / .ytdl / temp
                # =================================================

                try:

                    for filename in os.listdir(
                        temp_dir
                    ):

                        path = os.path.join(
                            temp_dir,
                            filename
                        )

                        if os.path.isfile(
                            path
                        ):

                            try:
                                os.remove(
                                    path
                                )

                            except Exception:
                                pass

                except Exception:
                    pass

        # ========================================================
        # KẾT THÚC
        # ========================================================

        if stopped:

            await msg.edit(
                "╭────────────────────────────╮\n"
                "│   🛑 <b>ĐÃ DỪNG PROFILE</b>   │\n"
                "╰────────────────────────────╯\n\n"

                f"👤 <code>@{username}</code>\n\n"

                f"📊 Đã tải: "
                f"<b>{success}</b>\n"

                f"⚠️ Lỗi: "
                f"<b>{failed}</b>\n\n"

                "🛑 Người dùng đã dừng quá trình.",
                parse_mode="html"
            )

        else:

            await msg.edit(
                "╭────────────────────────────╮\n"
                "│   ✅ <b>PROFILE HOÀN TẤT</b>   │\n"
                "╰────────────────────────────╯\n\n"

                f"👤 <b>Username:</b> "
                f"<code>@{username}</code>\n\n"

                "━━━━━━━━━━━━━━━━━━━━\n"

                f"🎬 Tổng video: "
                f"<b>{total}</b>\n"

                f"✅ Thành công: "
                f"<b>{success}</b>\n"

                f"⚠️ Video lỗi: "
                f"<b>{failed}</b>\n"

                "━━━━━━━━━━━━━━━━━━━━\n\n"

                "📤 Video được gửi ngay sau khi tải.\n"
                "🗑 File tạm đã được xóa sau mỗi video.",
                parse_mode="html"
            )

    except Exception as e:

        print(
            f"[TIKTOK PROFILE ERROR] {e}"
        )

        try:

            await msg.edit(
                "╭────────────────────────────╮\n"
                "│   ❌ <b>PROFILE ERROR</b>   │\n"
                "╰────────────────────────────╯\n\n"

                f"👤 <code>@{username}</code>\n\n"

                f"⚠️ <code>{str(e)[:2500]}</code>",
                parse_mode="html"
            )

        except Exception:
            pass

    finally:

        # ========================================================
        # XÓA THƯ MỤC TẠM
        # ========================================================

        if temp_dir:

            try:

                import shutil

                shutil.rmtree(
                    temp_dir,
                    ignore_errors=True
                )

            except Exception as cleanup_error:

                print(
                    f"[PROFILE CLEANUP] "
                    f"{cleanup_error}"
                )

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
            x for x in (
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
            f"🎬 Tìm thấy: <b>{len(entries)}</b> video\n\n"
            "📥 Bắt đầu tải...",
            parse_mode="html"
        )

        entries = entries[:20]

        for index, entry in enumerate(
            entries,
            1
        ):

            if not get_session(
                event.client,
                event.sender_id
            ):
                break

            video_id = entry.get(
                "id"
            )

            if not video_id:
                continue

            video_url = (
                f"https://www.youtube.com/watch?v={video_id}"
            )

            await event.reply(
                f"📥 <b>YouTube {index}/{len(entries)}</b>",
                parse_mode="html"
            )

            await download_one(
                event,
                video_url,
                None,
                youtube_options()
            )

    except Exception as e:

        print(
            f"[YOUTUBE CHANNEL ERROR] {e}"
        )

        await msg.edit(
            "❌ <b>Không thể đọc YouTube</b>\n\n"
            f"⚠️ <code>{str(e)[:2500]}</code>",
            parse_mode="html"
        )


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

        if data == "dl:tiktok":

            set_session(
                bot,
                user_id,
                state="tiktok_menu"
            )

            await event.edit(
                """
╭────────────────────────────╮
│       🎵 <b>TIKTOK</b>       │
╰────────────────────────────╯

Chọn chức năng:

🎬 <b>Tải video</b>
└ Gửi link TikTok video

📚 <b>Tải profile</b>
└ Chỉ cần nhập username
└ Ví dụ: <code>@username</code>
""",
                buttons=tiktok_menu(),
                parse_mode="html"
            )

            return

        if data == "dl:youtube":

            set_session(
                bot,
                user_id,
                state="youtube_menu"
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

        if data == "dl:other":

            set_session(
                bot,
                user_id,
                state="other"
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

        if data == "dl:tt_video":

            set_session(
                bot,
                user_id,
                state="tt_video"
            )

            await event.edit(
                "🎬 <b>TIKTOK VIDEO</b>\n\n"
                "🔗 Gửi link TikTok video.\n\n"
                "🛑 <code>/stop</code> để dừng.",
                parse_mode="html"
            )

            return

        if data == "dl:tt_profile":

            set_session(
                bot,
                user_id,
                state="tt_profile"
            )

            await event.edit(
                "📚 <b>TIKTOK PROFILE</b>\n\n"
                "👤 Nhập username.\n\n"
                "Ví dụ:\n"
                "<code>@username</code>\n"
                "hoặc\n"
                "<code>username</code>\n\n"
                "❌ Không cần nhập link.\n\n"
                "🛑 <code>/stop</code> để dừng.",
                parse_mode="html"
            )

            return

        if data == "dl:yt_video":

            set_session(
                bot,
                user_id,
                state="yt_video"
            )

            await event.edit(
                "🎬 <b>YOUTUBE VIDEO</b>\n\n"
                "🔗 Gửi link video YouTube.\n\n"
                "🛑 <code>/stop</code> để dừng.",
                parse_mode="html"
            )

            return

        if data == "dl:yt_channel":

            set_session(
                bot,
                user_id,
                state="yt_channel"
            )

            await event.edit(
                "📺 <b>YOUTUBE PLAYLIST / CHANNEL</b>\n\n"
                "👤 Nhập username:\n"
                "<code>@MrBeast</code>\n\n"
                "Hoặc:\n"
                "<code>MrBeast</code>\n\n"
                "Bạn cũng có thể gửi link channel.\n\n"
                "❌ Không cần link nếu dùng username.\n\n"
                "🛑 <code>/stop</code> để dừng.",
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

        # Task này có thể là vòng tải profile/channel.
        # /stop hoặc command mới sẽ cancel nó.
        track_current_task(user_id)

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
        # TIKTOK PROFILE
        # ====================================================

        if state == "tt_profile":

            await process_tiktok_profile(
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