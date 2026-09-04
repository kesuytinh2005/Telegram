# ============================================================
# commands/tiktok.py
#
# TIKTOK SCANNER + TIKWM DOWNLOADER
#
# FLOW:
#
# /download
#    ↓
# TikTok
#    ↓
# 📚 Playlist / Profile
#    ↓
# Nhập username / link
#    ↓
# Scan profile
#    ↓
# [📥 TẢI TẤT CẢ VIDEO]
#    ↓
# Tải video 1
#    ↓
# Gửi video 1 ngay
#    ↓
# Tải video 2
#    ↓
# Gửi video 2 ngay
#    ↓
# ...
#
# Không:
#   - Playwright
#   - Selenium
#   - Port
#   - Server trung gian
#
# Engine:
#   1. yt-dlp -> scan profile
#   2. TikWM -> download media
#   3. yt-dlp -> fallback
#
# Task manager:
#   download.py là nơi quản lý task chính.
#   File này KHÔNG replace_user_tasks().
# ============================================================

import asyncio
import html
import logging
import os
import random
import sqlite3
import time

from datetime import datetime
from urllib.parse import urlparse

import requests
import yt_dlp

from telethon import Button


logger = logging.getLogger(__name__)


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.dirname(
        os.path.abspath(__file__)
    )
)

DATA_DIR = os.path.join(
    BASE_DIR,
    "data"
)

RESULT_DIR = os.path.join(
    BASE_DIR,
    "results"
)

DOWNLOAD_DIR = os.path.join(
    BASE_DIR,
    "downloads"
)

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ============================================================
# DATABASE
# ============================================================

DB_FILE = os.path.join(
    DATA_DIR,
    "tiktok.db"
)


# ============================================================
# TIKWM
# ============================================================

TIKWM_API_URL = (
    "https://www.tikwm.com/api/"
)

TIKWM_API_RETRIES = 5

TIKWM_TIMEOUT = (
    15,
    90
)

TIKWM_DELAY_MIN = 0.8
TIKWM_DELAY_MAX = 1.8


# ============================================================
# MEDIA
# ============================================================

MEDIA_RETRIES = 5

MEDIA_CHUNK_SIZE = (
    512 * 1024
)

MEDIA_TIMEOUT = (
    15,
    120
)


# ============================================================
# SCAN
# ============================================================

SCAN_RETRIES = 8

SCAN_DELAY_MIN = 8
SCAN_DELAY_MAX = 20


# ============================================================
# YT-DLP FALLBACK
# ============================================================

USE_YTDLP_FALLBACK = True

USE_YTDLP_COOKIES = False

YTDLP_COOKIE_FILE = (
    "/sdcard/tiktok_cookies.txt"
)


# ============================================================
# HTTP HEADERS
# ============================================================

TIKWM_HEADERS = {

    "User-Agent":
        "Mozilla/5.0 "
        "(Linux; Android 16; Mobile) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0.0.0 "
        "Mobile Safari/537.36",

    "Accept":
        "application/json,text/plain,*/*",

    "Accept-Language":
        "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",

    "Origin":
        "https://www.tikwm.com",

    "Referer":
        "https://www.tikwm.com/",
}


MEDIA_HEADERS = {

    "User-Agent":
        "Mozilla/5.0 "
        "(Linux; Android 16; Mobile) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0.0.0 "
        "Mobile Safari/537.36",

    "Accept":
        "*/*",

    "Referer":
        "https://www.tikwm.com/",
}


# ============================================================
# SESSION STORAGE
# ============================================================

SESSION_KEY = (
    "_dragon_tiktok_sessions"
)


def get_sessions(bot):

    sessions = getattr(
        bot,
        SESSION_KEY,
        None
    )

    if not isinstance(
        sessions,
        dict
    ):

        sessions = {}

        setattr(
            bot,
            SESSION_KEY,
            sessions
        )

    return sessions


def clear_session(
    bot,
    user_id
):

    sessions = get_sessions(bot)

    sessions.pop(
        user_id,
        None
    )


# ============================================================
# DATABASE CONNECTION
# ============================================================

def db_connect():

    conn = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


# ============================================================
# DATABASE INIT
# ============================================================

def init_tiktok_database():

    conn = db_connect()

    try:

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (

                id TEXT PRIMARY KEY,

                title TEXT,

                url TEXT,

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

                video_id TEXT PRIMARY KEY,

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


# ============================================================
# STATE GET
# ============================================================

def state_get(
    key,
    default=None
):

    conn = db_connect()

    try:

        row = conn.execute(
            """
            SELECT value
            FROM state
            WHERE key = ?
            """,
            (key,)
        ).fetchone()

        if row is None:
            return default

        return row["value"]

    finally:

        conn.close()


# ============================================================
# STATE SET
# ============================================================

def state_set(
    key,
    value
):

    conn = db_connect()

    try:

        conn.execute(
            """
            INSERT INTO state
            (
                key,
                value
            )
            VALUES
            (
                ?,
                ?
            )
            ON CONFLICT(key)
            DO UPDATE SET
                value = excluded.value
            """,
            (
                key,
                str(value)
            )
        )

        conn.commit()

    finally:

        conn.close()


# ============================================================
# USERNAME
# ============================================================

def clean_username(
    username
):

    if not username:
        return None

    username = str(username).strip()

    # --------------------------------------------------------
    # TikTok URL
    # --------------------------------------------------------

    if "tiktok.com/" in username.lower():

        try:

            parsed = urlparse(
                username
            )

            path = (
                parsed.path
                .strip("/")
            )

            if path.startswith("@"):

                username = (
                    path.split("/", 1)[0]
                )

        except Exception:

            pass

    username = (
        username
        .strip()
        .lstrip("@")
        .strip()
    )

    username = (
        username
        .split("?")[0]
        .split("#")[0]
        .strip("/")
    )

    if not username:
        return None

    return username


# ============================================================
# PROFILE URL
# ============================================================

def build_profile_url(
    username
):

    username = clean_username(
        username
    )

    if not username:
        return None

    return (
        "https://www.tiktok.com/@"
        + username
    )


# ============================================================
# VIDEO URL
# ============================================================

def build_video_url(
    username,
    video_id
):

    username = clean_username(
        username
    )

    if not username or not video_id:
        return None

    return (
        f"https://www.tiktok.com/"
        f"@{username}/video/{video_id}"
    )


# ============================================================
# SAVE VIDEO
# ============================================================

def save_video(
    info
):

    video_id = info.get("id")

    if not video_id:
        return

    now = datetime.utcnow().isoformat()

    conn = db_connect()

    try:

        conn.execute(
            """
            INSERT INTO videos
            (
                id,
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
            VALUES
            (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            ON CONFLICT(id)
            DO UPDATE SET

                title = excluded.title,
                url = excluded.url,
                uploader = excluded.uploader,
                uploader_id = excluded.uploader_id,
                description = excluded.description,
                upload_date = excluded.upload_date,
                duration = excluded.duration,
                view_count = excluded.view_count,
                like_count = excluded.like_count,
                comment_count = excluded.comment_count,
                thumbnail = excluded.thumbnail,
                extractor = excluded.extractor,
                last_seen = excluded.last_seen
            """,
            (
                str(video_id),

                info.get("title"),

                info.get("webpage_url")
                or info.get("url"),

                info.get("uploader"),

                info.get("uploader_id"),

                info.get("description"),

                info.get("upload_date"),

                info.get("duration"),

                info.get("view_count"),

                info.get("like_count"),

                info.get("comment_count"),

                info.get("thumbnail"),

                info.get("extractor"),

                now,

                now,
            )
        )

        conn.commit()

    finally:

        conn.close()


# ============================================================
# COUNT ALL
# ============================================================

def count_videos():

    conn = db_connect()

    try:

        row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM videos
            """
        ).fetchone()

        return int(
            row["total"]
        )

    finally:

        conn.close()


# ============================================================
# GET ALL
# ============================================================

def get_all_videos():

    conn = db_connect()

    try:

        rows = conn.execute(
            """
            SELECT *
            FROM videos
            ORDER BY rowid ASC
            """
        ).fetchall()

        return rows

    finally:

        conn.close()


# ============================================================
# GET PROFILE VIDEOS
# ============================================================

def get_videos_by_username(
    username
):

    username = clean_username(
        username
    )

    if not username:
        return []

    conn = db_connect()

    try:

        rows = conn.execute(
            """
            SELECT *
            FROM videos
            WHERE
                LOWER(COALESCE(uploader, '')) = LOWER(?)
                OR
                LOWER(COALESCE(uploader_id, '')) = LOWER(?)
            ORDER BY rowid ASC
            """,
            (
                username,
                username
            )
        ).fetchall()

        return rows

    finally:

        conn.close()


# ============================================================
# DOWNLOAD RECORD
# ============================================================

def get_download_record(
    video_id
):

    conn = db_connect()

    try:

        row = conn.execute(
            """
            SELECT *
            FROM download_records
            WHERE video_id = ?
            """,
            (str(video_id),)
        ).fetchone()

        return row

    finally:

        conn.close()


# ============================================================
# SET DOWNLOAD RECORD
# ============================================================

def set_download_record(
    video_id,
    status,
    engine=None,
    media_type=None,
    media_url=None,
    file_path=None,
    error=None
):

    conn = db_connect()

    try:

        conn.execute(
            """
            INSERT INTO download_records
            (
                video_id,
                status,
                engine,
                media_type,
                media_url,
                file_path,
                error,
                updated_at
            )
            VALUES
            (
                ?, ?, ?, ?, ?,
                ?, ?, ?
            )
            ON CONFLICT(video_id)
            DO UPDATE SET

                status = excluded.status,
                engine = excluded.engine,
                media_type = excluded.media_type,
                media_url = excluded.media_url,
                file_path = excluded.file_path,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                str(video_id),
                status,
                engine,
                media_type,
                media_url,
                file_path,
                error,
                datetime.utcnow().isoformat()
            )
        )

        conn.commit()

    finally:

        conn.close()


# ============================================================
# HTTP SESSION
# ============================================================

def create_http_session():

    session = requests.Session()

    session.headers.update(
        TIKWM_HEADERS
    )

    return session


# ============================================================
# TIKWM LOOKUP
# ============================================================

def tikwm_lookup(
    session,
    video_url
):

    last_error = None

    for attempt in range(
        1,
        TIKWM_API_RETRIES + 1
    ):

        try:

            response = session.post(
                TIKWM_API_URL,
                data={
                    "url": video_url,
                    "hd": 1
                },
                timeout=TIKWM_TIMEOUT
            )

            if response.status_code == 429:

                delay = min(
                    60,
                    2 ** attempt
                )

                delay += random.uniform(
                    0.5,
                    2
                )

                logger.warning(
                    "[TIKWM] 429 attempt=%s",
                    attempt
                )

                time.sleep(delay)

                continue

            if response.status_code >= 500:

                delay = min(
                    45,
                    2 ** attempt
                )

                time.sleep(delay)

                continue

            response.raise_for_status()

            payload = response.json()

            if not isinstance(
                payload,
                dict
            ):

                raise RuntimeError(
                    "TikWM JSON không hợp lệ."
                )

            code = payload.get("code")

            if code not in (
                0,
                "0",
                None
            ):

                raise RuntimeError(
                    payload.get("msg")
                    or
                    payload.get("message")
                    or
                    f"code={code}"
                )

            data = payload.get("data")

            if not isinstance(
                data,
                dict
            ):

                raise RuntimeError(
                    "TikWM không có data."
                )

            return data

        except Exception as e:

            last_error = e

            logger.warning(
                "[TIKWM] attempt=%s/%s error=%s",
                attempt,
                TIKWM_API_RETRIES,
                e
            )

            if attempt < TIKWM_API_RETRIES:

                time.sleep(
                    min(
                        45,
                        2 ** attempt
                    )
                    +
                    random.uniform(
                        0.3,
                        1.5
                    )
                )

    raise RuntimeError(
        str(last_error)
        if last_error
        else
        "TikWM API failed."
    )


# ============================================================
# PARSE TIKWM
# ============================================================

def parse_tikwm_data(
    data
):

    images = data.get("images")

    if (
        isinstance(images, list)
        and images
    ):

        images = [
            str(x).strip()
            for x in images
            if x
        ]

        return {
            "type": "photo",
            "images": images,
            "media_url": None
        }

    media_url = (
        data.get("hdplay")
        or
        data.get("play")
        or
        data.get("wmplay")
    )

    if media_url:

        return {
            "type": "video",
            "images": [],
            "media_url": str(
                media_url
            )
        }

    return {
        "type": "unknown",
        "images": [],
        "media_url": None
    }


# ============================================================
# OUTPUT PATH
# ============================================================

def video_output_path(
    username,
    video_id
):

    username = clean_username(
        username
    ) or "unknown"

    folder = os.path.join(
        DOWNLOAD_DIR,
        username
    )

    os.makedirs(
        folder,
        exist_ok=True
    )

    return os.path.join(
        folder,
        f"{video_id}.mp4"
    )


# ============================================================
# FORMAT BYTES
# ============================================================

def format_bytes(
    value
):

    try:

        value = float(value)

    except Exception:

        return "0 B"

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB"
    ]

    for unit in units:

        if value < 1024:

            return (
                f"{value:.2f} {unit}"
            )

        value /= 1024

    return (
        f"{value:.2f} PB"
    )


# ============================================================
# PROGRESS BAR
# ============================================================

def progress_bar(
    current,
    total,
    width=20
):

    if total <= 0:

        return (
            "["
            + "#"
            * width
            + "]"
        )

    ratio = min(
        1,
        max(
            0,
            current / total
        )
    )

    filled = int(
        width * ratio
    )

    return (
        "["
        + "#"
        * filled
        + "-"
        * (
            width - filled
        )
        + "]"
    )


# ============================================================
# DOWNLOAD MEDIA
# ============================================================

def download_media(
    session,
    media_url,
    output_path,
    progress_callback=None,
    stop_checker=None
):

    part_path = (
        output_path
        + ".part"
    )

    last_error = None

    for attempt in range(
        1,
        MEDIA_RETRIES + 1
    ):

        if (
            stop_checker
            and stop_checker()
        ):

            raise asyncio.CancelledError

        try:

            if os.path.exists(part_path):

                try:
                    os.remove(part_path)
                except Exception:
                    pass

            with session.get(
                media_url,
                headers=MEDIA_HEADERS,
                timeout=MEDIA_TIMEOUT,
                stream=True
            ) as response:

                response.raise_for_status()

                total = int(
                    response.headers.get(
                        "Content-Length",
                        0
                    )
                    or 0
                )

                downloaded = 0

                with open(
                    part_path,
                    "wb"
                ) as file:

                    for chunk in response.iter_content(
                        chunk_size=MEDIA_CHUNK_SIZE
                    ):

                        if (
                            stop_checker
                            and stop_checker()
                        ):

                            raise asyncio.CancelledError

                        if not chunk:
                            continue

                        file.write(chunk)

                        downloaded += len(chunk)

                        if progress_callback:

                            progress_callback(
                                downloaded,
                                total
                            )

                if not os.path.exists(
                    part_path
                ):

                    raise RuntimeError(
                        "Không tạo được file."
                    )

                size = os.path.getsize(
                    part_path
                )

                if size <= 0:

                    raise RuntimeError(
                        "File rỗng."
                    )

                os.replace(
                    part_path,
                    output_path
                )

                return {
                    "success": True,
                    "path": output_path,
                    "size": size
                }

        except asyncio.CancelledError:

            try:

                if os.path.exists(
                    part_path
                ):

                    os.remove(
                        part_path
                    )

            except Exception:
                pass

            raise

        except Exception as e:

            last_error = e

            logger.warning(
                "[MEDIA] attempt=%s/%s error=%s",
                attempt,
                MEDIA_RETRIES,
                e
            )

            try:

                if os.path.exists(
                    part_path
                ):

                    os.remove(
                        part_path
                    )

            except Exception:
                pass

            if attempt < MEDIA_RETRIES:

                time.sleep(
                    min(
                        30,
                        2 ** attempt
                    )
                    +
                    random.uniform(
                        0.5,
                        2
                    )
                )

    return {
        "success": False,
        "path": None,
        "size": 0,
        "error":
            str(last_error)
            if last_error
            else
            "Download failed."
    }


# ============================================================
# PHOTO LOG
# ============================================================

def photo_file(
    username
):

    username = clean_username(
        username
    ) or "unknown"

    return os.path.join(
        RESULT_DIR,
        f"{username}_photo.txt"
    )


# ============================================================
# ERROR LOG
# ============================================================

def error_file(
    username
):

    username = clean_username(
        username
    ) or "unknown"

    return os.path.join(
        RESULT_DIR,
        f"{username}_error.txt"
    )


def read_logged_urls(
    filepath
):

    result = set()

    if not os.path.exists(
        filepath
    ):
        return result

    try:

        with open(
            filepath,
            "r",
            encoding="utf-8"
        ) as file:

            for line in file:

                line = line.strip()

                if not line:
                    continue

                url = (
                    line.split(
                        " | ",
                        1
                    )[0]
                )

                if url:
                    result.add(url)

    except Exception:
        pass

    return result


def append_unique(
    filepath,
    value,
    extra=None
):

    existing = read_logged_urls(
        filepath
    )

    if value in existing:
        return False

    with open(
        filepath,
        "a",
        encoding="utf-8"
    ) as file:

        if extra:

            file.write(
                f"{value} | {extra}\n"
            )

        else:

            file.write(
                value + "\n"
            )

    return True


def save_photo_links(
    username,
    video_url,
    images
):

    filepath = photo_file(
        username
    )

    existing = read_logged_urls(
        filepath
    )

    added = 0

    for image_url in images:

        image_url = str(
            image_url
        ).strip()

        if not image_url:
            continue

        if image_url in existing:
            continue

        with open(
            filepath,
            "a",
            encoding="utf-8"
        ) as file:

            file.write(
                f"{image_url} | "
                f"{video_url}\n"
            )

        existing.add(
            image_url
        )

        added += 1

    return added


def save_error(
    username,
    video_url,
    reason
):

    append_unique(
        error_file(username),
        video_url,
        reason
    )


def remove_error(
    username,
    video_url
):

    filepath = error_file(
        username
    )

    if not os.path.exists(
        filepath
    ):
        return

    try:

        with open(
            filepath,
            "r",
            encoding="utf-8"
        ) as file:

            lines = file.readlines()

        new_lines = []

        for line in lines:

            value = (
                line.split(
                    " | ",
                    1
                )[0]
                .strip()
            )

            if value != video_url:

                new_lines.append(line)

        with open(
            filepath,
            "w",
            encoding="utf-8"
        ) as file:

            file.writelines(
                new_lines
            )

    except Exception:

        logger.exception(
            "[TIKTOK] remove_error failed"
        )


# ============================================================
# YT-DLP PROFILE
# ============================================================

def profile_options():

    return {

        "quiet": True,

        "no_warnings": True,

        "ignoreerrors": False,

        "extract_flat": True,

        "skip_download": True,

        "lazy_playlist": True,

        "retries": 3,

        "fragment_retries": 3,

        "socket_timeout": 40,

        "noplaylist": False,

        "playlistend": None,
    }


# ============================================================
# SCAN PROFILE
# ============================================================

def scan_profile_sync(
    username,
    progress_callback=None,
    stop_checker=None
):

    username = clean_username(
        username
    )

    profile_url = build_profile_url(
        username
    )

    if not profile_url:

        raise RuntimeError(
            "Username TikTok không hợp lệ."
        )

    complete_key = (
        f"tiktok_complete:{username}"
    )

    state_set(
        complete_key,
        "0"
    )

    last_error = None

    for attempt in range(
        1,
        SCAN_RETRIES + 1
    ):

        if (
            stop_checker
            and stop_checker()
        ):

            raise asyncio.CancelledError

        try:

            with yt_dlp.YoutubeDL(
                profile_options()
            ) as ydl:

                info = ydl.extract_info(
                    profile_url,
                    download=False
                )

                if not info:

                    raise RuntimeError(
                        "Không lấy được profile."
                    )

                entries = info.get(
                    "entries"
                )

                if entries is None:

                    raise RuntimeError(
                        "Profile không có entries."
                    )

                count = 0

                for entry in entries:

                    if (
                        stop_checker
                        and stop_checker()
                    ):

                        raise asyncio.CancelledError

                    if not entry:
                        continue

                    video_id = entry.get("id")

                    if not video_id:
                        continue

                    entry_url = (
                        entry.get(
                            "webpage_url"
                        )
                        or
                        build_video_url(
                            username,
                            video_id
                        )
                    )

                    item = dict(entry)

                    item[
                        "webpage_url"
                    ] = entry_url

                    item.setdefault(
                        "uploader",
                        username
                    )

                    item.setdefault(
                        "uploader_id",
                        username
                    )

                    save_video(item)

                    count += 1

                    if progress_callback:

                        progress_callback(
                            count,
                            video_id
                        )

                state_set(
                    complete_key,
                    "1"
                )

                return {
                    "success": True,
                    "complete": True,
                    "count": count,
                    "username": username
                }

        except asyncio.CancelledError:

            raise

        except Exception as e:

            last_error = e

            error_text = str(
                e
            ).lower()

            rate_limited = (
                "429" in error_text
                or
                "too many requests"
                in error_text
                or
                "rate limit"
                in error_text
            )

            logger.warning(
                "[TIKTOK SCAN] "
                "attempt=%s/%s "
                "rate=%s error=%s",
                attempt,
                SCAN_RETRIES,
                rate_limited,
                e
            )

            if attempt >= SCAN_RETRIES:
                break

            if rate_limited:

                delay = random.uniform(
                    30,
                    60
                )

            else:

                delay = random.uniform(
                    SCAN_DELAY_MIN,
                    SCAN_DELAY_MAX
                )

            # Không sleep 20 giây một cục.
            # Cho /stop phản ứng nhanh hơn.
            end_time = (
                time.monotonic()
                + delay
            )

            while (
                time.monotonic()
                < end_time
            ):

                if (
                    stop_checker
                    and stop_checker()
                ):

                    raise asyncio.CancelledError

                time.sleep(0.25)

    return {
        "success": False,
        "complete": False,
        "count": 0,
        "username": username,
        "error":
            str(last_error)
            if last_error
            else
            "Scan failed."
    }


# ============================================================
# TIKWM DOWNLOAD ONE
# ============================================================

def download_one_tikwm(
    session,
    username,
    video_id,
    video_url,
    progress_callback=None,
    stop_checker=None
):

    data = tikwm_lookup(
        session,
        video_url
    )

    parsed = parse_tikwm_data(
        data
    )

    media_type = parsed["type"]

    # --------------------------------------------------------
    # PHOTO
    # --------------------------------------------------------

    if media_type == "photo":

        added = save_photo_links(
            username,
            video_url,
            parsed["images"]
        )

        set_download_record(
            video_id,
            status="photo",
            engine="tikwm",
            media_type="photo",
            media_url=None,
            file_path=None,
            error=None
        )

        return {
            "success": True,
            "type": "photo",
            "added": added
        }

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    if media_type != "video":

        raise RuntimeError(
            "TikWM không trả về video."
        )

    media_url = parsed[
        "media_url"
    ]

    output_path = video_output_path(
        username,
        video_id
    )

    if os.path.exists(
        output_path
    ):

        size = os.path.getsize(
            output_path
        )

        if size > 0:

            set_download_record(
                video_id,
                status="downloaded",
                engine="tikwm",
                media_type="video",
                media_url=media_url,
                file_path=output_path,
                error=None
            )

            return {
                "success": True,
                "type": "video",
                "path": output_path,
                "size": size,
                "existing": True
            }

    result = download_media(
        session,
        media_url,
        output_path,
        progress_callback,
        stop_checker
    )

    if not result["success"]:

        set_download_record(
            video_id,
            status="error",
            engine="tikwm",
            media_type="video",
            media_url=media_url,
            file_path=None,
            error=result.get("error")
        )

        raise RuntimeError(
            result.get(
                "error",
                "Download failed."
            )
        )

    set_download_record(
        video_id,
        status="downloaded",
        engine="tikwm",
        media_type="video",
        media_url=media_url,
        file_path=output_path,
        error=None
    )

    return {
        "success": True,
        "type": "video",
        "path": output_path,
        "size": result["size"],
        "existing": False
    }


# ============================================================
# YT-DLP FALLBACK
# ============================================================

def download_one_ytdlp(
    username,
    video_id,
    video_url,
    stop_checker=None
):

    if (
        stop_checker
        and stop_checker()
    ):

        raise asyncio.CancelledError

    output_path = video_output_path(
        username,
        video_id
    )

    if os.path.exists(
        output_path
    ):

        size = os.path.getsize(
            output_path
        )

        if size > 0:

            return {
                "success": True,
                "path": output_path,
                "size": size,
                "existing": True
            }

    options = {

        "format":
            "bv*+ba/b",

        "merge_output_format":
            "mp4",

        "outtmpl":
            output_path,

        "retries":
            3,

        "fragment_retries":
            3,

        "socket_timeout":
            60,

        "quiet":
            True,

        "no_warnings":
            True,

        "noplaylist":
            True,
    }

    if (
        USE_YTDLP_COOKIES
        and
        os.path.exists(
            YTDLP_COOKIE_FILE
        )
    ):

        options[
            "cookiefile"
        ] = YTDLP_COOKIE_FILE

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            ydl.download(
                [video_url]
            )

        if (
            stop_checker
            and stop_checker()
        ):

            raise asyncio.CancelledError

        if os.path.exists(
            output_path
        ):

            size = os.path.getsize(
                output_path
            )

            if size > 0:

                return {
                    "success": True,
                    "path": output_path,
                    "size": size
                }

        return {
            "success": False,
            "error":
                "yt-dlp không tạo được file."
        }

    except asyncio.CancelledError:

        raise

    except Exception as e:

        return {
            "success": False,
            "error": str(e)
        }


# ============================================================
# DOWNLOAD ONE VIDEO
#
# Hàm này dùng để:
#
#   tải 1 video
#   trả về path
#
# Sau đó asyncio worker sẽ SEND NGAY.
# ============================================================

def download_one_video_sync(
    username,
    row,
    progress_callback=None,
    stop_checker=None
):

    username = clean_username(
        username
    )

    video_id = str(
        row["id"]
    )

    video_url = (
        row["url"]
        or
        build_video_url(
            username,
            video_id
        )
    )

    if not video_url:

        raise RuntimeError(
            "Video không có URL."
        )

    output_path = video_output_path(
        username,
        video_id
    )

    # --------------------------------------------------------
    # File đã có
    # --------------------------------------------------------

    if os.path.exists(
        output_path
    ):

        size = os.path.getsize(
            output_path
        )

        if size > 0:

            return {
                "success": True,
                "type": "video",
                "path": output_path,
                "size": size,
                "existing": True,
                "engine": "local",
                "video_id": video_id,
                "video_url": video_url
            }

    session = create_http_session()

    try:

        # ----------------------------------------------------
        # TIKWM
        # ----------------------------------------------------

        try:

            result = download_one_tikwm(
                session,
                username,
                video_id,
                video_url,
                progress_callback,
                stop_checker
            )

            if result.get("type") == "video":

                result.update({
                    "video_id": video_id,
                    "video_url": video_url,
                    "engine": "tikwm"
                })

                return result

            # Photo thì không gửi bằng send_file video.
            return {
                "success": True,
                "type": "photo",
                "video_id": video_id,
                "video_url": video_url,
                "engine": "tikwm",
                "added": result.get(
                    "added",
                    0
                )
            }

        except asyncio.CancelledError:

            raise

        except Exception as e:

            logger.warning(
                "[TIKTOK] TikWM failed "
                "video=%s error=%s",
                video_id,
                e
            )

        # ----------------------------------------------------
        # YT-DLP FALLBACK
        # ----------------------------------------------------

        if USE_YTDLP_FALLBACK:

            fallback = download_one_ytdlp(
                username,
                video_id,
                video_url,
                stop_checker
            )

            if fallback.get("success"):

                set_download_record(
                    video_id,
                    status="downloaded",
                    engine="ytdlp",
                    media_type="video",
                    media_url=None,
                    file_path=fallback["path"],
                    error=None
                )

                remove_error(
                    username,
                    video_url
                )

                return {
                    "success": True,
                    "type": "video",
                    "path": fallback["path"],
                    "size": fallback["size"],
                    "existing": fallback.get(
                        "existing",
                        False
                    ),
                    "engine": "ytdlp",
                    "video_id": video_id,
                    "video_url": video_url
                }

            raise RuntimeError(
                fallback.get(
                    "error",
                    "TikWM + yt-dlp failed"
                )
            )

        raise RuntimeError(
            "TikWM download failed."
        )

    finally:

        session.close()


# ============================================================
# SEND ONE VIDEO
#
# Video tải xong -> gửi ngay.
# ============================================================

async def send_downloaded_video(
    event,
    path,
    username,
    video_id,
    index,
    total
):

    if not path or not os.path.exists(path):

        raise RuntimeError(
            "Không tìm thấy file video."
        )

    size = os.path.getsize(path)

    if size <= 0:

        raise RuntimeError(
            "File video rỗng."
        )

    title = (
        f"@{username} • "
        f"{index}/{total}"
    )

    caption = (
        "🎵 <b>TIKTOK</b>\n\n"
        f"👤 <b>@{html.escape(username)}</b>\n"
        f"📦 <b>{index}/{total}</b>\n"
        f"🆔 <code>{html.escape(str(video_id))}</code>\n"
        f"💾 <b>{format_bytes(size)}</b>"
    )

    await event.client.send_file(
        event.chat_id,
        path,
        caption=caption,
        parse_mode="html",
        supports_streaming=True
    )


# ============================================================
# FORMAT DOWNLOAD PROGRESS
# ============================================================

def format_download_progress(
    index,
    total,
    video_id,
    percent,
    current,
    total_size
):

    percent = max(
        0,
        min(
            100,
            percent
        )
    )

    bar = progress_bar(
        percent,
        100
    )

    return (
        "╭────────────────────────╮\n"
        "│   🎵 <b>TIKTOK DOWNLOAD</b> │\n"
        "╰────────────────────────╯\n\n"

        f"📦 <b>{index}/{total}</b>\n"
        f"🆔 <code>{html.escape(str(video_id))}</code>\n\n"

        "⬇️ <b>Đang tải video...</b>\n\n"

        f"{bar} <b>{percent:.1f}%</b>\n\n"

        f"💾 {format_bytes(current)}"
        " / "
        f"{format_bytes(total_size)}"
    )


# ============================================================
# SCAN WORKER
#
# KHÔNG replace task.
# KHÔNG tạo task khác.
# ============================================================

async def scan_worker(
    bot,
    event,
    username,
    message
):

    user_id = event.sender_id

    sessions = get_sessions(bot)

    try:

        def stopped():

            session = sessions.get(
                user_id
            )

            if not session:
                return True

            return not session.get(
                "running",
                False
            )

        await message.edit(
            "🔎 <b>Đang quét TikTok...</b>\n\n"
            f"👤 <code>@{html.escape(username)}</code>\n\n"
            "⏳ Đang lấy danh sách video...",
            parse_mode="html"
        )

        result = await asyncio.to_thread(
            scan_profile_sync,
            username,
            None,
            stopped
        )

        if result.get("complete"):

            total = int(
                result.get(
                    "count",
                    0
                )
            )

            # ------------------------------------------------
            # NÚT TẢI TẤT CẢ
            # ------------------------------------------------

            buttons = []

            if total > 0:

                buttons.append([
                    Button.inline(
                        "📥 TẢI TẤT CẢ VIDEO",
                        data=(
                            f"tt_download_all:"
                            f"{username}"
                        ).encode()
                    )
                ])

            buttons.append([
                Button.inline(
                    "🔄 QUÉT LẠI",
                    data=(
                        f"tt_rescan:"
                        f"{username}"
                    ).encode()
                )
            ])

            await message.edit(
                "╭────────────────────────────╮\n"
                "│   ✅ <b>TIKTOK HOÀN TẤT</b>   │\n"
                "╰────────────────────────────╯\n\n"

                f"🔗 <b>@{html.escape(username)}</b>\n\n"

                "📊 <b>KẾT QUẢ</b>\n\n"

                f"🎬 Video mới/lưu: "
                f"<b>{total}</b>\n"

                "✅ Scan: <b>COMPLETE</b>\n\n"

                "━━━━━━━━━━━━━━━━━━━━\n\n"

                "📚 Database đã cập nhật.\n\n"

                "📥 <b>Nhấn nút bên dưới để tải "
                "tất cả video.</b>\n\n"

                "⚡ Video nào tải xong sẽ "
                "được gửi ngay.",
                buttons=buttons,
                parse_mode="html"
            )

        else:

            await message.edit(
                "⚠️ <b>Scan chưa hoàn tất.</b>\n\n"
                f"👤 @{html.escape(username)}\n\n"
                f"📦 Đã lưu: "
                f"<b>{count_videos()}</b> video\n\n"
                f"❌ "
                f"{html.escape(str(result.get('error', 'Unknown error')))}",
                parse_mode="html"
            )

    except asyncio.CancelledError:

        try:

            await message.edit(
                "🛑 <b>Đã dừng quét TikTok.</b>\n\n"
                f"👤 @{html.escape(username)}\n\n"
                f"💾 Database vẫn giữ dữ liệu "
                f"đã scan được.",
                parse_mode="html"
            )

        except Exception:
            pass

        raise

    except Exception as e:

        logger.exception(
            "[TIKTOK SCAN WORKER]"
        )

        try:

            await message.edit(
                "❌ <b>Scan TikTok lỗi.</b>\n\n"
                f"<code>{html.escape(str(e)[:1500])}</code>",
                parse_mode="html"
            )

        except Exception:
            pass

    finally:

        session = sessions.get(
            user_id
        )

        if session:

            session["processing"] = False

            # Không xóa session ở đây.
            # Vì nút TẢI TẤT CẢ cần username.
            #
            # download.py / stop sẽ xử lý
            # session tiếp theo.


# ============================================================
# DOWNLOAD ALL WORKER
#
# QUAN TRỌNG:
#
# Mỗi video:
#
#   DOWNLOAD
#      ↓
#   SEND NGAY
#
# Không đợi tải hết rồi mới gửi.
# ============================================================

async def download_all_worker(
    bot,
    event,
    username,
    message
):

    user_id = event.sender_id

    sessions = get_sessions(bot)

    username = clean_username(
        username
    )

    if not username:

        raise RuntimeError(
            "Username TikTok không hợp lệ."
        )

    rows = get_videos_by_username(
        username
    )

    if not rows:

        raise RuntimeError(
            "Không tìm thấy video của profile "
            f"@{username} trong database."
        )

    total = len(rows)

    sessions[user_id] = {
        "command":
            "tiktok_download",

        "username":
            username,

        "running":
            True,

        "processing":
            True,

        "source":
            "download",

        "notify_bot":
            sessions.get(
                user_id,
                {}
            ).get(
                "notify_bot"
            )
    }

    downloaded = 0
    skipped = 0
    errors = 0
    photos = 0

    last_edit = 0

    try:

        await message.edit(
            "╭────────────────────────╮\n"
            "│   📥 <b>TIKTOK DOWNLOAD</b> │\n"
            "╰────────────────────────╯\n\n"

            f"👤 <b>@{html.escape(username)}</b>\n\n"

            f"📦 Tổng video: <b>{total}</b>\n\n"

            "🚀 Bắt đầu tải...\n\n"

            "⚡ Video tải xong sẽ gửi ngay.\n"
            "🛑 /stop để dừng.",
            parse_mode="html"
        )

        def stopped():

            session = sessions.get(
                user_id
            )

            if not session:
                return True

            return not session.get(
                "running",
                False
            )

        # ----------------------------------------------------
        # LOOP TỪNG VIDEO
        # ----------------------------------------------------

        for index, row in enumerate(
            rows,
            start=1
        ):

            if stopped():

                raise asyncio.CancelledError

            video_id = str(
                row["id"]
            )

            video_url = (
                row["url"]
                or
                build_video_url(
                    username,
                    video_id
                )
            )

            current_progress = 0
            current_size = 0
            current_total = 0

            # ------------------------------------------------
            # MESSAGE PROGRESS
            # ------------------------------------------------

            async def edit_progress(
                percent,
                current,
                total_size
            ):

                nonlocal last_edit

                now = time.monotonic()

                if (
                    percent < 100
                    and
                    now - last_edit < 1.0
                ):

                    return

                last_edit = now

                try:

                    await message.edit(
                        format_download_progress(
                            index,
                            total,
                            video_id,
                            percent,
                            current,
                            total_size
                        ),
                        parse_mode="html"
                    )

                except Exception:

                    pass

            # ------------------------------------------------
            # CALLBACK TỪ THREAD
            # ------------------------------------------------

            loop = asyncio.get_running_loop()

            def progress(
                current,
                total_size
            ):

                nonlocal current_progress
                nonlocal current_size
                nonlocal current_total

                current_size = current

                current_total = (
                    total_size
                )

                if total_size:

                    current_progress = (
                        current
                        /
                        total_size
                        *
                        100
                    )

                else:

                    current_progress = 0

                try:

                    asyncio.run_coroutine_threadsafe(
                        edit_progress(
                            current_progress,
                            current_size,
                            current_total
                        ),
                        loop
                    )

                except Exception:

                    pass

            # ------------------------------------------------
            # DOWNLOAD
            # ------------------------------------------------

            try:

                result = await asyncio.to_thread(
                    download_one_video_sync,
                    username,
                    row,
                    progress,
                    stopped
                )

                if result.get("type") == "photo":

                    photos += 1

                    # Photo không gửi bằng video.
                    # Chỉ ghi log.

                    continue

                path = result.get(
                    "path"
                )

                if not path:

                    raise RuntimeError(
                        "Download không trả về file."
                    )

                if result.get(
                    "existing"
                ):

                    skipped += 1

                else:

                    downloaded += 1

                # ------------------------------------------------
                # QUAN TRỌNG:
                #
                # TẢI XONG -> GỬI NGAY
                # ------------------------------------------------

                await send_downloaded_video(
                    event,
                    path,
                    username,
                    video_id,
                    index,
                    total
                )

                # ------------------------------------------------
                # Sau khi gửi xong mới sang video kế tiếp.
                # ------------------------------------------------

                try:

                    await message.edit(
                        "╭────────────────────────╮\n"
                        "│   📥 <b>TIKTOK DOWNLOAD</b> │\n"
                        "╰────────────────────────╯\n\n"

                        f"👤 <b>@{html.escape(username)}</b>\n\n"

                        f"📦 <b>{index}/{total}</b>\n"
                        f"🆔 <code>{html.escape(video_id)}</code>\n\n"

                        "✅ <b>Đã tải + gửi video.</b>\n\n"

                        f"📊 Đã gửi: "
                        f"<b>{downloaded + skipped}</b>\n"

                        f"⏳ Còn lại: "
                        f"<b>{total - index}</b>\n\n"

                        "🚀 Đang chuyển sang video tiếp theo...",
                        parse_mode="html"
                    )

                except Exception:

                    pass

            except asyncio.CancelledError:

                raise

            except Exception as e:

                errors += 1

                logger.exception(
                    "[TIKTOK DOWNLOAD] "
                    "video=%s failed",
                    video_id
                )

                if video_url:

                    save_error(
                        username,
                        video_url,
                        str(e)
                    )

                try:

                    await message.edit(
                        "⚠️ <b>Video lỗi</b>\n\n"
                        f"📦 {index}/{total}\n"
                        f"🆔 <code>{html.escape(video_id)}</code>\n\n"
                        f"❌ <code>{html.escape(str(e)[:700])}</code>\n\n"
                        "➡️ Bỏ qua và tiếp tục video tiếp theo...",
                        parse_mode="html"
                    )

                except Exception:

                    pass

                # ------------------------------------------------
                # Không dừng cả profile chỉ vì 1 video lỗi.
                # ------------------------------------------------

                continue

        # ====================================================
        # HOÀN TẤT
        # ====================================================

        await message.edit(
            "╭────────────────────────╮\n"
            "│   ✅ <b>TIKTOK HOÀN TẤT</b> │\n"
            "╰────────────────────────╯\n\n"

            f"👤 <b>@{html.escape(username)}</b>\n\n"

            "📊 <b>KẾT QUẢ</b>\n\n"

            f"📦 Tổng: <b>{total}</b>\n"
            f"📤 Đã gửi: <b>{downloaded + skipped}</b>\n"
            f"🖼 Photo: <b>{photos}</b>\n"
            f"❌ Lỗi: <b>{errors}</b>\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "🎉 <b>Tất cả video đã được xử lý.</b>",
            parse_mode="html"
        )

    except asyncio.CancelledError:

        try:

            await message.edit(
                "🛑 <b>ĐÃ DỪNG TẢI TIKTOK</b>\n\n"
                f"👤 @{html.escape(username)}\n\n"

                f"📦 Tổng: <b>{total}</b>\n"
                f"📤 Đã gửi: <b>{downloaded + skipped}</b>\n"
                f"❌ Lỗi: <b>{errors}</b>\n\n"

                "💾 Các video đã tải xong "
                "vẫn được giữ lại.\n\n"

                "▶️ Chạy lại sẽ bỏ qua "
                "những file đã có.",
                parse_mode="html"
            )

        except Exception:

            pass

        raise

    except Exception as e:

        logger.exception(
            "[TIKTOK DOWNLOAD ALL]"
        )

        try:

            await message.edit(
                "❌ <b>Tải TikTok lỗi.</b>\n\n"
                f"<code>{html.escape(str(e)[:1500])}</code>",
                parse_mode="html"
            )

        except Exception:
            pass

    finally:

        session = sessions.get(
            user_id
        )

        if session:

            session["processing"] = False

            session["running"] = False


# ============================================================
# PUBLIC API
#
# download.py gọi hàm này.
#
# KHÔNG replace task ở đây.
# ============================================================

async def process_tiktok_profile(
    bot,
    event,
    username,
    notify_bot=None
):

    username = clean_username(
        username
    )

    if not username:

        await event.reply(
            "❌ <b>Username TikTok không hợp lệ.</b>",
            parse_mode="html"
        )

        return

    user_id = event.sender_id

    sessions = get_sessions(bot)

    sessions[user_id] = {

        "command":
            "tiktok",

        "username":
            username,

        "running":
            True,

        "processing":
            True,

        "source":
            "download",

        "notify_bot":
            notify_bot
    }

    message = await event.reply(
        "⏳ <b>Đang khởi động TikTok...</b>\n\n"
        f"👤 <code>@{html.escape(username)}</code>",
        parse_mode="html"
    )

    # --------------------------------------------------------
    # KHÔNG replace_user_tasks()
    #
    # download.py đã quản lý task.
    # --------------------------------------------------------

    await scan_worker(
        bot,
        event,
        username,
        message
    )


# ============================================================
# PUBLIC API
#
# Dùng khi download.py muốn bắt đầu download.
# ============================================================

async def start_tiktok_download(
    bot,
    event,
    username,
    notify_bot=None,
):

    username = clean_username(
        username
    )

    if not username:

        await event.reply(
            "❌ <b>Username TikTok không hợp lệ.</b>",
            parse_mode="html"
        )

        return

    user_id = event.sender_id

    sessions = get_sessions(bot)

    sessions[user_id] = {

        "command":
            "tiktok_download",

        "username":
            username,

        "running":
            True,

        "processing":
            True,

        "source":
            "download",

        "notify_bot":
            notify_bot
    }

    if message is None:

        message = await event.reply(
            "⏳ <b>Đang chuẩn bị tải TikTok...</b>\n\n"
            f"👤 <code>@{html.escape(username)}</code>",
            parse_mode="html"
        )

    await download_all_worker(
        bot,
        event,
        username,
        message
    )


# ============================================================
# ALIAS
# ============================================================

process_tiktok_download = (
    start_tiktok_download
)


# ============================================================
# BUTTON HANDLER
#
# download.py có thể gọi:
#
#   await tiktok.handle_callback(...)
#
# Hoặc tự xử lý callback.
#
# Hàm này giúp tiktok.py có thể độc lập xử lý
# nút TẢI TẤT CẢ.
# ============================================================

async def handle_callback(
    bot,
    event,
    notify_bot=None
):

    data = event.data

    if isinstance(
        data,
        bytes
    ):

        data = data.decode(
            "utf-8",
            errors="ignore"
        )

    data = str(data)

    # ========================================================
    # TẢI TẤT CẢ
    # ========================================================

    if data.startswith(
        "tt_download_all:"
    ):

        username = data.split(
            ":",
            1
        )[1]

        username = clean_username(
            username
        )

        if not username:

            await event.answer(
                "Username không hợp lệ.",
                alert=True
            )

            return True

        await event.answer(
            "🚀 Bắt đầu tải tất cả video..."
        )

        user_id = event.sender_id

        sessions = get_sessions(
            bot
        )

        # ----------------------------------------------------
        # Không replace task ở đây.
        #
        # Callback phải được download.py đưa vào
        # task manager.
        # ----------------------------------------------------

        sessions[user_id] = {

            "command":
                "tiktok_download",

            "username":
                username,

            "running":
                True,

            "processing":
                True,

            "source":
                "download",

            "notify_bot":
                notify_bot
        }

        message = event.message

        await download_all_worker(
            bot,
            event,
            username,
            message
        )

        return True

    # ========================================================
    # RESCAN
    # ========================================================

    if data.startswith(
        "tt_rescan:"
    ):

        username = data.split(
            ":",
            1
        )[1]

        username = clean_username(
            username
        )

        await event.answer(
            "🔄 Đang quét lại..."
        )

        await process_tiktok_profile(
            bot,
            event,
            username,
            notify_bot
        )

        return True

    return False


# ============================================================
# REGISTER
#
# Không đăng ký:
#   /tiktok
#   /tiktokdownload
#
# Entry point:
#
#   /download
# ============================================================

def register(
    bot,
    notify_bot
):

    init_tiktok_database()

    logger.info(
        "[TIKTOK] Database initialized."
    )

    logger.info(
        "[TIKTOK] Standalone commands disabled."
    )