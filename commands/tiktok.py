# -*- coding: utf-8 -*-

"""
TIKTOK MODULE
============================================================
Luồng:

/download
    -> TikTok
        -> 📚 Playlist / Profile
            -> nhập username/link
                -> scan profile
                    -> 📥 TẢI TẤT CẢ VIDEO
                        -> tải video 1 -> gửi ngay
                        -> tải video 2 -> gửi ngay
                        -> ...
                        
Không cần:
- Playwright
- Selenium
- port trung gian
- server trung gian

Engine:
1. TikWM
2. yt-dlp fallback

Task:
- 1 task/user
- task mới thay task cũ
- /stop hủy task hiện tại
============================================================
"""

import asyncio
import csv
import json
import logging
import os
import re
import sqlite3
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import requests
import yt_dlp

from telethon import Button

from core.task_manager import (
    track_current_task,
    untrack_current_task,
)


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
RESULT_DIR = BASE_DIR / "results"
DOWNLOAD_DIR = BASE_DIR / "downloads"

DATA_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = DATA_DIR / "tiktok.db"

TIKWM_API = "https://www.tikwm.com/api/"

TIKWM_RETRIES = 5
TIKWM_TIMEOUT = (15, 90)

MEDIA_RETRIES = 5
MEDIA_TIMEOUT = (15, 120)

SCAN_RETRIES = 8

MAX_FILENAME = 120

LOG_FILE = DATA_DIR / "tiktok.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("tiktok")


# ============================================================
# SESSION
# ============================================================

_dragon_tiktok_sessions = {}


def get_sessions(bot):
    """
    Lưu session theo bot instance.
    """
    key = id(bot)

    if key not in _dragon_tiktok_sessions:
        _dragon_tiktok_sessions[key] = {}

    return _dragon_tiktok_sessions[key]


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        str(DB_FILE),
        timeout=30,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row
    return conn


def init_tiktok_database():
    conn = db_connect()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                url TEXT UNIQUE,
                uploader TEXT,
                uploader_id TEXT,
                description TEXT,
                upload_date TEXT,
                duration INTEGER,
                view_count INTEGER,
                like_count INTEGER,
                comment_count INTEGER,
                thumbnail TEXT,
                extractor TEXT,
                first_seen TEXT,
                last_seen TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS download_records (
                video_id INTEGER PRIMARY KEY,
                status TEXT,
                engine TEXT,
                media_type TEXT,
                media_url TEXT,
                file_path TEXT,
                error TEXT,
                updated_at TEXT
            )
            """
        )

        conn.commit()

    finally:
        conn.close()


init_tiktok_database()


# ============================================================
# STATE
# ============================================================

def get_state(key, default=None):
    conn = db_connect()

    try:
        row = conn.execute(
            "SELECT value FROM state WHERE key = ?",
            (key,),
        ).fetchone()

        if not row:
            return default

        return row["value"]

    finally:
        conn.close()


def set_state(key, value):
    conn = db_connect()

    try:
        conn.execute(
            """
            INSERT INTO state(key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# USERNAME
# ============================================================

def clean_username(value):
    if not value:
        return ""

    value = str(value).strip()

    value = value.replace(
        "https://www.tiktok.com/@",
        "",
    )

    value = value.replace(
        "https://www.tiktok.com/",
        "",
    )

    value = value.replace(
        "http://www.tiktok.com/@",
        "",
    )

    value = value.replace(
        "http://www.tiktok.com/",
        "",
    )

    value = value.split("?")[0]
    value = value.split("#")[0]
    value = value.strip("/")

    if value.startswith("@"):
        value = value[1:]

    value = value.strip()

    match = re.match(
        r"([A-Za-z0-9._-]+)",
        value,
    )

    if match:
        return match.group(1)

    return ""


def profile_url(username):
    username = clean_username(username)

    return f"https://www.tiktok.com/@{quote(username)}"


# ============================================================
# FORMAT
# ============================================================

def safe_filename(name):
    if not name:
        name = "tiktok_video"

    name = str(name)

    name = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        name,
    )

    name = re.sub(
        r"\s+",
        " ",
        name,
    ).strip()

    if not name:
        name = "tiktok_video"

    return name[:MAX_FILENAME]


def format_number(value):
    try:
        return f"{int(value):,}".replace(",", ".")
    except Exception:
        return "0"


def format_bytes(size):
    try:
        size = float(size)
    except Exception:
        return "0 B"

    units = [
        "B",
        "KB",
        "MB",
        "GB",
    ]

    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"

        size /= 1024

    return f"{size:.1f} TB"


# ============================================================
# DATABASE VIDEO
# ============================================================

def save_video(info):
    url = info.get("url")

    if not url:
        return None

    now = datetime.now().isoformat()

    conn = db_connect()

    try:
        conn.execute(
            """
            INSERT INTO videos (
                title,
                url,
                uploader,
                uploader_id,
                description,
                upload_date,
                duration,
                view_count,
                like_count,
                comment_count,
                thumbnail,
                extractor,
                first_seen,
                last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url)
            DO UPDATE SET
                title = excluded.title,
                uploader = excluded.uploader,
                uploader_id = excluded.uploader_id,
                description = excluded.description,
                duration = excluded.duration,
                view_count = excluded.view_count,
                like_count = excluded.like_count,
                comment_count = excluded.comment_count,
                thumbnail = excluded.thumbnail,
                extractor = excluded.extractor,
                last_seen = excluded.last_seen
            """,
            (
                info.get("title", ""),
                url,
                info.get("uploader", ""),
                info.get("uploader_id", ""),
                info.get("description", ""),
                info.get("upload_date", ""),
                info.get("duration", 0) or 0,
                info.get("view_count", 0) or 0,
                info.get("like_count", 0) or 0,
                info.get("comment_count", 0) or 0,
                info.get("thumbnail", ""),
                info.get("extractor", "tiktok"),
                now,
                now,
            ),
        )

        conn.commit()

        row = conn.execute(
            "SELECT id FROM videos WHERE url = ?",
            (url,),
        ).fetchone()

        return row["id"] if row else None

    finally:
        conn.close()


def get_profile_videos(username):
    """
    Chỉ lấy video của profile vừa scan.
    Không lấy toàn bộ DB.
    """

    username = clean_username(username)

    conn = db_connect()

    try:
        rows = conn.execute(
            """
            SELECT *
            FROM videos
            WHERE LOWER(uploader) = LOWER(?)
            ORDER BY id ASC
            """,
            (username,),
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


def get_video_by_url(url):
    conn = db_connect()

    try:
        row = conn.execute(
            "SELECT * FROM videos WHERE url = ?",
            (url,),
        ).fetchone()

        return dict(row) if row else None

    finally:
        conn.close()


def count_profile_videos(username):
    return len(get_profile_videos(username))


# ============================================================
# HTTP
# ============================================================

def create_http_session():
    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Linux; Android 10; K) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Mobile Safari/537.36"
            ),
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
    )

    return session


# ============================================================
# TIKWM
# ============================================================

def tikwm_lookup(video_url):
    """
    Lấy media URL từ TikWM.
    """

    session = create_http_session()

    payload = {
        "url": video_url,
        "hd": 1,
    }

    last_error = None

    try:

        for attempt in range(1, TIKWM_RETRIES + 1):

            try:
                response = session.post(
                    TIKWM_API,
                    data=payload,
                    timeout=TIKWM_TIMEOUT,
                )

                response.raise_for_status()

                data = response.json()

                if not isinstance(data, dict):
                    raise RuntimeError(
                        "TikWM response không hợp lệ"
                    )

                if data.get("code") not in (0, None):
                    raise RuntimeError(
                        str(data.get("msg") or "TikWM error")
                    )

                result = data.get("data") or {}

                # ----------------------------
                # VIDEO
                # ----------------------------

                for key in (
                    "hdplay",
                    "play",
                    "wmplay",
                ):
                    value = result.get(key)

                    if isinstance(value, str) and value.startswith(
                        ("http://", "https://")
                    ):
                        return {
                            "type": "video",
                            "url": value,
                            "engine": "tikwm",
                        }

                # ----------------------------
                # PHOTO
                # ----------------------------

                images = result.get("images")

                if isinstance(images, list):

                    for image in images:

                        if isinstance(image, str):
                            if image.startswith(
                                ("http://", "https://")
                            ):
                                return {
                                    "type": "photo",
                                    "url": image,
                                    "engine": "tikwm",
                                }

                raise RuntimeError(
                    "TikWM không tìm thấy media"
                )

            except Exception as exc:
                last_error = exc

                logger.warning(
                    "TikWM attempt %s/%s: %s",
                    attempt,
                    TIKWM_RETRIES,
                    exc,
                )

                if attempt < TIKWM_RETRIES:
                    time.sleep(
                        min(2 * attempt, 8)
                    )

    finally:
        session.close()

    raise RuntimeError(
        f"TikWM failed: {last_error}"
    )


# ============================================================
# YT-DLP
# ============================================================

def ytdlp_get_media(video_url):
    """
    Fallback lấy direct media bằng yt-dlp.
    """

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": (
            "bv*+ba/"
            "b"
        ),
    }

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            video_url,
            download=False,
        )

    if not info:
        raise RuntimeError(
            "yt-dlp không trả về dữ liệu"
        )

    url = info.get("url")

    if url:
        return {
            "type": "video",
            "url": url,
            "engine": "yt-dlp",
            "title": info.get("title") or "TikTok",
        }

    formats = info.get("formats") or []

    candidates = []

    for fmt in formats:

        direct = fmt.get("url")

        if not direct:
            continue

        height = fmt.get("height") or 0
        ext = fmt.get("ext") or ""

        candidates.append(
            (
                height,
                ext,
                direct,
            )
        )

    if not candidates:
        raise RuntimeError(
            "Không tìm thấy direct media"
        )

    candidates.sort(
        key=lambda x: x[0]
    )

    best = candidates[-1]

    return {
        "type": "video",
        "url": best[2],
        "engine": "yt-dlp",
        "title": info.get("title") or "TikTok",
    }


# ============================================================
# GET MEDIA
# ============================================================

def get_media_url(video_url):
    """
    TikWM trước.
    yt-dlp sau.
    """

    try:
        return tikwm_lookup(video_url)

    except Exception as tikwm_error:

        logger.warning(
            "TikWM failed, fallback yt-dlp: %s",
            tikwm_error,
        )

        try:
            return ytdlp_get_media(video_url)

        except Exception as ytdlp_error:

            raise RuntimeError(
                "TikWM + yt-dlp đều thất bại\n"
                f"TikWM: {tikwm_error}\n"
                f"yt-dlp: {ytdlp_error}"
            )


# ============================================================
# DOWNLOAD MEDIA
# ============================================================

def download_media(
    media_url,
    output_path,
    stop_checker=None,
):
    """
    Download trực tiếp CDN.

    Không dùng port trung gian.
    """

    output_path = Path(output_path)

    part_path = Path(
        str(output_path) + ".part"
    )

    session = create_http_session()

    last_error = None

    try:

        for attempt in range(
            1,
            MEDIA_RETRIES + 1,
        ):

            try:

                with session.get(
                    media_url,
                    stream=True,
                    timeout=MEDIA_TIMEOUT,
                    allow_redirects=True,
                ) as response:

                    response.raise_for_status()

                    total = int(
                        response.headers.get(
                            "content-length",
                            0,
                        )
                        or 0
                    )

                    downloaded = 0

                    with open(
                        part_path,
                        "wb",
                    ) as file:

                        for chunk in response.iter_content(
                            chunk_size=512 * 1024
                        ):

                            if stop_checker:
                                if stop_checker():
                                    raise asyncio.CancelledError()

                            if not chunk:
                                continue

                            file.write(chunk)

                            downloaded += len(chunk)

                os.replace(
                    part_path,
                    output_path,
                )

                return {
                    "path": str(output_path),
                    "size": downloaded or total,
                }

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                last_error = exc

                logger.warning(
                    "Download attempt %s/%s failed: %s",
                    attempt,
                    MEDIA_RETRIES,
                    exc,
                )

                try:
                    if part_path.exists():
                        part_path.unlink()
                except Exception:
                    pass

                if attempt < MEDIA_RETRIES:
                    time.sleep(
                        min(attempt * 2, 10)
                    )

    finally:
        session.close()

    raise RuntimeError(
        f"Download failed: {last_error}"
    )


# ============================================================
# SCAN PROFILE
# ============================================================

def scan_profile_sync(
    username,
    progress_callback=None,
    stop_checker=None,
):
    """
    Scan profile bằng yt-dlp.

    Chỉ lưu video TikTok.
    """

    username = clean_username(username)

    if not username:
        raise ValueError(
            "Username TikTok không hợp lệ"
        )

    url = profile_url(username)

    set_state(
        f"tiktok_complete:{username}",
        "0",
    )

    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "lazy_playlist": True,
        "playlistend": None,
        "ignoreerrors": True,
    }

    last_error = None

    for attempt in range(
        1,
        SCAN_RETRIES + 1,
    ):

        try:

            if stop_checker and stop_checker():
                raise asyncio.CancelledError()

            if progress_callback:
                progress_callback(
                    f"🔎 Đang quét TikTok...\n"
                    f"👤 @{username}\n"
                    f"🔄 Lần thử: {attempt}/{SCAN_RETRIES}"
                )

            with yt_dlp.YoutubeDL(options) as ydl:

                info = ydl.extract_info(
                    url,
                    download=False,
                )

            if not info:
                raise RuntimeError(
                    "TikTok không trả về dữ liệu"
                )

            entries = info.get("entries")

            if entries is None:
                entries = [info]

            count = 0

            for entry in entries:

                if stop_checker and stop_checker():
                    raise asyncio.CancelledError()

                if not entry:
                    continue

                video_url = (
                    entry.get("webpage_url")
                    or entry.get("url")
                )

                if not video_url:
                    continue

                if not str(video_url).startswith(
                    "http"
                ):
                    continue

                # Một số entry extract_flat trả URL ID.
                if "/video/" not in video_url:
                    video_id = (
                        entry.get("id")
                        or ""
                    )

                    if video_id:
                        video_url = (
                            f"https://www.tiktok.com/"
                            f"@{username}/video/{video_id}"
                        )
                    else:
                        continue

                save_video(
                    {
                        "title": entry.get(
                            "title"
                        ) or "TikTok",
                        "url": video_url,
                        "uploader": username,
                        "uploader_id": (
                            entry.get("uploader_id")
                            or username
                        ),
                        "description": entry.get(
                            "description"
                        ) or "",
                        "upload_date": entry.get(
                            "upload_date"
                        ) or "",
                        "duration": entry.get(
                            "duration"
                        ) or 0,
                        "view_count": entry.get(
                            "view_count"
                        ) or 0,
                        "like_count": entry.get(
                            "like_count"
                        ) or 0,
                        "comment_count": entry.get(
                            "comment_count"
                        ) or 0,
                        "thumbnail": entry.get(
                            "thumbnail"
                        ) or "",
                        "extractor": "tiktok",
                    }
                )

                count += 1

                if progress_callback:
                    progress_callback(
                        f"🔎 Đang quét TikTok...\n"
                        f"👤 @{username}\n\n"
                        f"🎬 Đã tìm thấy: {count}"
                    )

            set_state(
                f"tiktok_complete:{username}",
                "1",
            )

            return count

        except asyncio.CancelledError:
            raise

        except Exception as exc:

            last_error = exc

            logger.error(
                "Scan attempt %s failed: %s",
                attempt,
                exc,
            )

            if attempt < SCAN_RETRIES:

                if progress_callback:
                    progress_callback(
                        f"⚠️ TikTok đang giới hạn truy cập.\n"
                        f"🔄 Thử lại {attempt + 1}/{SCAN_RETRIES}..."
                    )

                time.sleep(
                    min(
                        8 * attempt,
                        30,
                    )
                )

    raise RuntimeError(
        f"Không thể scan TikTok: {last_error}"
    )


# ============================================================
# SCAN WORKER
# ============================================================

async def scan_worker(
    bot,
    event,
    username,
    message,
):
    user_id = event.sender_id

    sessions = get_sessions(bot)

    current_task = asyncio.current_task()

    track_current_task(
        user_id,
        current_task,
    )

    try:

        def stopped():
            session = sessions.get(user_id)

            if not session:
                return True

            return not session.get(
                "running",
                False,
            )

        async def update_message(text):
            try:
                await message.edit(text)
            except Exception:
                pass

        def progress(text):
            try:
                loop.call_soon_threadsafe(
                    asyncio.create_task,
                    update_message(text),
                )
            except Exception:
                pass

        loop = asyncio.get_running_loop()

        await update_message(
            "╭────────────────────────────╮\n"
            "│   🔎 TIKTOK SCANNER        │\n"
            "╰────────────────────────────╯\n\n"
            f"👤 @{username}\n\n"
            "⏳ Đang quét profile..."
        )

        count = await asyncio.to_thread(
            scan_profile_sync,
            username,
            progress,
            stopped,
        )

        if stopped():
            return

        videos = get_profile_videos(
            username
        )

        count = len(videos)

        await update_message(
            "╭────────────────────────────╮\n"
            "│    ✅ TIKTOK HOÀN TẤT      │\n"
            "╰────────────────────────────╯\n\n"
            f"🔗 @{username}\n"
            f"📊 Tìm thấy: **{count} video**\n"
            "✅ Scan: **COMPLETE**\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📚 Database đã cập nhật.\n"
            "📥 Bấm nút bên dưới để tải tất cả."
        )

        try:
            await message.edit(
                "╭────────────────────────────╮\n"
                "│    ✅ TIKTOK HOÀN TẤT      │\n"
                "╰────────────────────────────╯\n\n"
                f"🔗 @{username}\n"
                f"📊 Tìm thấy: **{count} video**\n"
                "✅ Scan: **COMPLETE**\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📚 Database đã cập nhật.\n"
                "📥 Bấm nút bên dưới để tải tất cả.",
                buttons=[
                    [
                        Button.inline(
                            "📥 TẢI TẤT CẢ VIDEO",
                            data=(
                                "tt:download_all:"
                                + username
                            ).encode(),
                        )
                    ],
                    [
                        Button.inline(
                            "❌ Đóng",
                            data=b"tt:close",
                        )
                    ],
                ],
            )

        except Exception as exc:
            logger.error(
                "Cannot add download button: %s",
                exc,
            )

    except asyncio.CancelledError:

        try:
            await message.edit(
                "🛑 **TIKTOK ĐÃ DỪNG**\n\n"
                f"👤 @{username}\n"
                "Đã hủy tác vụ hiện tại."
            )
        except Exception:
            pass

        raise

    except Exception as exc:

        logger.error(
            "scan_worker error:\n%s",
            traceback.format_exc(),
        )

        try:
            await message.edit(
                "❌ **TIKTOK SCAN LỖI**\n\n"
                f"👤 @{username}\n\n"
                f"⚠️ `{exc}`"
            )
        except Exception:
            pass

    finally:

        session = sessions.get(user_id)

        if session:
            session["running"] = False

        untrack_current_task(
            user_id,
            current_task,
        )


# ============================================================
# DOWNLOAD ONE
# ============================================================

def download_one_sync(
    video,
    username,
    index,
    total,
    stop_checker=None,
):
    """
    Tải một video.
    """

    if stop_checker and stop_checker():
        raise asyncio.CancelledError()

    video_url = video.get("url")

    if not video_url:
        raise RuntimeError(
            "Video không có URL"
        )

    title = (
        video.get("title")
        or f"tiktok_{index}"
    )

    title = safe_filename(title)

    user_dir = (
        DOWNLOAD_DIR
        / "tiktok"
        / safe_filename(username)
    )

    user_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    output = (
        user_dir
        / f"{index:04d}_{title}.mp4"
    )

    # Tránh file cũ.
    if output.exists():
        try:
            output.unlink()
        except Exception:
            pass

    media = get_media_url(
        video_url
    )

    if stop_checker and stop_checker():
        raise asyncio.CancelledError()

    result = download_media(
        media["url"],
        output,
        stop_checker,
    )

    return {
        "path": result["path"],
        "size": result["size"],
        "engine": media.get(
            "engine",
            "unknown",
        ),
        "media_type": media.get(
            "type",
            "video",
        ),
        "title": title,
    }


# ============================================================
# DOWNLOAD ALL
# ============================================================

async def download_all_for_profile(
    bot,
    event,
    username,
):
    """
    Tải tuần tự từng video.

    Video nào xong:
        -> gửi ngay video đó.

    Không đợi toàn bộ hoàn thành.
    """

    username = clean_username(username)

    if not username:
        await event.reply(
            "❌ Username TikTok không hợp lệ."
        )
        return

    user_id = event.sender_id

    sessions = get_sessions(bot)

    session = sessions.setdefault(
        user_id,
        {},
    )

    session["running"] = True
    session["type"] = "tiktok_download"
    session["username"] = username

    current_task = asyncio.current_task()

    track_current_task(
        user_id,
        current_task,
    )

    status_message = None

    try:

        videos = get_profile_videos(
            username
        )

        if not videos:

            await event.reply(
                "❌ Không có video nào trong profile.\n\n"
                f"👤 @{username}\n"
                "Hãy scan profile trước."
            )

            return

        total = len(videos)

        status_message = await event.reply(
            "╭────────────────────────────╮\n"
            "│    📥 TIKTOK DOWNLOAD      │\n"
            "╰────────────────────────────╯\n\n"
            f"👤 @{username}\n"
            f"📊 Tổng video: {total}\n\n"
            "⏳ Chuẩn bị tải..."
        )

        success = 0
        failed = 0

        for index, video in enumerate(
            videos,
            start=1,
        ):

            # Kiểm tra /stop NGAY trước mỗi video.
            if not session.get(
                "running",
                False,
            ):
                raise asyncio.CancelledError()

            try:

                title = (
                    video.get("title")
                    or f"Video {index}"
                )

                try:
                    await status_message.edit(
                        "╭────────────────────────────╮\n"
                        "│    📥 TIKTOK DOWNLOAD      │\n"
                        "╰────────────────────────────╯\n\n"
                        f"👤 @{username}\n"
                        f"📊 {index}/{total}\n\n"
                        f"⏳ Đang tải:\n"
                        f"🎬 {title[:100]}"
                    )
                except Exception:
                    pass

                def stopped():
                    current = sessions.get(
                        user_id
                    )

                    if not current:
                        return True

                    return not current.get(
                        "running",
                        False,
                    )

                result = await asyncio.to_thread(
                    download_one_sync,
                    video,
                    username,
                    index,
                    total,
                    stopped,
                )

                if stopped():
                    raise asyncio.CancelledError()

                file_path = result["path"]

                # ========================================
                # GỬI NGAY VIDEO VỪA TẢI XONG
                # ========================================

                caption = (
                    f"🎬 **TikTok**\n\n"
                    f"👤 @{username}\n"
                    f"📊 {index}/{total}\n"
                    f"📝 {title[:500]}"
                )

                await bot.send_file(
                    event.chat_id,
                    file_path,
                    caption=caption,
                    supports_streaming=True,
                )

                success += 1

                # ========================================
                # XÓA FILE SAU KHI GỬI
                # ========================================

                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except Exception as exc:
                    logger.warning(
                        "Cannot remove %s: %s",
                        file_path,
                        exc,
                    )

                try:
                    await status_message.edit(
                        "╭────────────────────────────╮\n"
                        "│    📥 TIKTOK DOWNLOAD      │\n"
                        "╰────────────────────────────╯\n\n"
                        f"👤 @{username}\n"
                        f"📊 {index}/{total}\n\n"
                        f"✅ Đã gửi: {success}\n"
                        f"❌ Lỗi: {failed}\n\n"
                        "⏳ Đang xử lý video tiếp theo..."
                    )
                except Exception:
                    pass

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                failed += 1

                logger.error(
                    "Download video %s failed:\n%s",
                    index,
                    traceback.format_exc(),
                )

                # Ghi lỗi nhưng KHÔNG dừng toàn bộ.
                try:
                    await event.reply(
                        f"⚠️ Video {index}/{total} lỗi\n"
                        f"🎬 {title[:100]}\n"
                        f"❌ {str(exc)[:500]}\n\n"
                        "➡️ Chuyển sang video tiếp theo."
                    )
                except Exception:
                    pass

        try:
            await status_message.edit(
                "╭────────────────────────────╮\n"
                "│    ✅ TIKTOK HOÀN TẤT      │\n"
                "╰────────────────────────────╯\n\n"
                f"👤 @{username}\n"
                f"📊 Tổng: {total}\n"
                f"✅ Đã gửi: {success}\n"
                f"❌ Lỗi: {failed}\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📥 Các video đã được gửi trực tiếp."
            )
        except Exception:
            pass

    except asyncio.CancelledError:

        try:
            if status_message:
                await status_message.edit(
                    "🛑 **TIKTOK ĐÃ DỪNG**\n\n"
                    f"👤 @{username}\n"
                    "Tác vụ tải đã được hủy."
                )
        except Exception:
            pass

        raise

    except Exception as exc:

        logger.error(
            "download_all_for_profile error:\n%s",
            traceback.format_exc(),
        )

        try:
            if status_message:
                await status_message.edit(
                    "❌ **TIKTOK DOWNLOAD LỖI**\n\n"
                    f"👤 @{username}\n\n"
                    f"⚠️ `{exc}`"
                )
        except Exception:
            pass

    finally:

        session = sessions.get(
            user_id
        )

        if session:
            session["running"] = False

        untrack_current_task(
            user_id,
            current_task,
        )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def handle_callback(
    bot,
    event,
):
    """
    Được gọi từ commands/download.py
    """

    data = event.data

    if isinstance(data, bytes):
        data = data.decode(
            "utf-8",
            errors="ignore",
        )

    if data == "tt:close":

        try:
            await event.delete()
        except Exception:
            pass

        return True

    if not data.startswith(
        "tt:download_all:"
    ):
        return False

    username = data.split(
        "tt:download_all:",
        1,
    )[1]

    username = clean_username(
        username
    )

    if not username:
        await event.answer(
            "❌ Username không hợp lệ",
            alert=True,
        )
        return True

    await event.answer(
        "📥 Bắt đầu tải từng video...",
        alert=False,
    )

    # Nút được bấm trực tiếp.
    # Task hiện tại của user sẽ được download.py
    # thay thế bằng task này.
    from core.task_manager import replace_user_tasks

    user_id = event.sender_id

    coro = download_all_for_profile(
        bot,
        event,
        username,
    )

    await replace_user_tasks(
        user_id,
        coro,
    )

    return True


# ============================================================
# PUBLIC API
# ============================================================

async def process_tiktok_profile(
    bot,
    event,
    username,
    notify_bot=None,
):
    """
    PUBLIC API được commands/download.py gọi.

    QUAN TRỌNG:
    Không gọi replace_user_tasks() ở đây.

    download.py đã quản lý task rồi.
    """

    username = clean_username(
        username
    )

    if not username:
        await event.reply(
            "❌ Username TikTok không hợp lệ.\n\n"
            "Ví dụ:\n"
            "@nguyenvanloiofficial"
        )
        return

    user_id = event.sender_id

    sessions = get_sessions(bot)

    sessions[user_id] = {
        "running": True,
        "type": "tiktok_scan",
        "username": username,
    }

    message = await event.reply(
        "╭────────────────────────────╮\n"
        "│   🔎 TIKTOK SCANNER        │\n"
        "╰────────────────────────────╯\n\n"
        f"👤 @{username}\n\n"
        "⏳ Đang quét profile..."
    )

    await scan_worker(
        bot,
        event,
        username,
        message,
    )


# ============================================================
# COMPATIBILITY API
# ============================================================

async def download_profile(
    bot,
    event,
    username,
    notify_bot=None,
):
    return await process_tiktok_profile(
        bot,
        event,
        username,
        notify_bot,
    )


async def download_playlist(
    bot,
    event,
    username,
    notify_bot=None,
):
    return await process_tiktok_profile(
        bot,
        event,
        username,
        notify_bot,
    )


async def run(
    bot,
    event,
    username=None,
    text=None,
    url=None,
    profile=None,
    link=None,
    notify_bot=None,
):
    value = (
        username
        or text
        or url
        or profile
        or link
        or ""
    )

    return await process_tiktok_profile(
        bot,
        event,
        value,
        notify_bot,
    )


# ============================================================
# REGISTER
# ============================================================

def register(
    bot,
    notify_bot=None,
):
    """
    Không đăng ký /tiktok nữa.

    TikTok chỉ đi qua:

        /download
            -> TikTok
                -> Playlist/Profile
    """

    init_tiktok_database()

    logger.info(
        "TikTok module initialized"
    )