# ============================================================
# commands/tiktok.py
#
# TIKTOK SCANNER + TIKWM DOWNLOADER
#
# Tương thích:
#   - Telethon
#   - core.task_manager
#   - commands.register_all()
#
# Không sử dụng:
#   - Playwright
#   - Selenium
#
# Engine:
#   1. TikWM API
#   2. yt-dlp fallback
# ============================================================

import asyncio
import csv
import json
import logging
import os
import random
import re
import sqlite3
import time
import traceback

from datetime import datetime
from urllib.parse import urlparse

import requests
import yt_dlp

from telethon import events

from core.task_manager import (
    replace_user_tasks,
    track_current_task,
    untrack_current_task,
)


# ============================================================
# LOG
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# COMMAND INFO
# ============================================================




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


os.makedirs(
    DATA_DIR,
    exist_ok=True
)

os.makedirs(
    RESULT_DIR,
    exist_ok=True
)

os.makedirs(
    DOWNLOAD_DIR,
    exist_ok=True
)


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


# ============================================================
# DATABASE CONNECTION
# ============================================================

def db_connect():

    conn = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    conn.row_factory = (
        sqlite3.Row
    )

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
# STATE
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
            (
                key,
            )
        ).fetchone()

        if row is None:
            return default

        return row["value"]

    finally:

        conn.close()


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
# SAVE VIDEO
# ============================================================

def save_video(
    info
):

    video_id = (
        info.get("id")
    )

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

                info.get(
                    "title"
                ),

                info.get(
                    "webpage_url"
                ) or info.get(
                    "url"
                ),

                info.get(
                    "uploader"
                ),

                info.get(
                    "uploader_id"
                ),

                info.get(
                    "description"
                ),

                info.get(
                    "upload_date"
                ),

                info.get(
                    "duration"
                ),

                info.get(
                    "view_count"
                ),

                info.get(
                    "like_count"
                ),

                info.get(
                    "comment_count"
                ),

                info.get(
                    "thumbnail"
                ),

                info.get(
                    "extractor"
                ),

                now,

                now,
            )
        )

        conn.commit()

    finally:

        conn.close()


# ============================================================
# COUNT
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
# GET VIDEOS
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
            (
                str(video_id),
            )
        ).fetchone()

        return row

    finally:

        conn.close()


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
# USERNAME
# ============================================================

def clean_username(
    username
):

    if not username:
        return None

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

    return (
        f"https://www.tiktok.com/"
        f"@{username}/video/{video_id}"
    )


# ============================================================
# SESSION
# ============================================================

def create_http_session():

    session = requests.Session()

    session.headers.update(
        TIKWM_HEADERS
    )

    return session


# ============================================================
# RATE LIMIT
# ============================================================

def is_rate_limited(
    response
):

    if response is None:
        return False

    return response.status_code == 429


# ============================================================
# TIKWM API
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
                    "hd": 1,
                },
                timeout=TIKWM_TIMEOUT
            )

            status = (
                response.status_code
            )

            if status == 429:

                delay = min(
                    60,
                    2 ** attempt
                )

                delay += random.uniform(
                    0.5,
                    2.0
                )

                logger.warning(
                    "[TIKWM] 429 - retry %s/%s",
                    attempt,
                    TIKWM_API_RETRIES
                )

                time.sleep(
                    delay
                )

                continue

            if status >= 500:

                delay = min(
                    45,
                    2 ** attempt
                )

                time.sleep(
                    delay
                )

                continue

            response.raise_for_status()

            payload = response.json()

            if not isinstance(
                payload,
                dict
            ):

                raise RuntimeError(
                    "TikWM trả về JSON không hợp lệ."
                )

            code = payload.get(
                "code"
            )

            if code not in (
                0,
                "0",
                None
            ):

                message = (
                    payload.get(
                        "msg"
                    )
                    or
                    payload.get(
                        "message"
                    )
                    or
                    f"code={code}"
                )

                raise RuntimeError(
                    f"TikWM API: {message}"
                )

            data = payload.get(
                "data"
            )

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
                "[TIKWM] attempt %s/%s: %s",
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
        else "TikWM API failed."
    )


# ============================================================
# PARSE TIKWM
# ============================================================

def parse_tikwm_data(
    data
):

    images = data.get(
        "images"
    )

    if isinstance(
        images,
        list
    ) and images:

        images = [
            str(x).strip()
            for x in images
            if x
        ]

        return {
            "type": "photo",
            "images": images,
            "media_url": None,
        }

    media_url = (
        data.get("hdplay")
        or data.get("play")
        or data.get("wmplay")
    )

    if media_url:

        return {
            "type": "video",
            "images": [],
            "media_url": str(
                media_url
            ),
        }

    return {
        "type": "unknown",
        "images": [],
        "media_url": None,
    }


# ============================================================
# FILE NAME
# ============================================================

def video_output_path(
    username,
    video_id
):

    username = clean_username(
        username
    )

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
        "TB",
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
            "[" +
            "#" * width +
            "]"
        )

    ratio = min(
        1,
        current / total
    )

    filled = int(
        width * ratio
    )

    return (
        "["
        + "#" * filled
        + "-" * (
            width - filled
        )
        + "]"
    )


# ============================================================
# STREAM DOWNLOAD
# ============================================================

def download_media(
    session,
    media_url,
    output_path,
    progress_callback=None
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

        try:

            # ----------------------------------------------
            # Nếu part cũ quá trình trước thì xóa.
            # ----------------------------------------------

            if os.path.exists(
                part_path
            ):

                try:
                    os.remove(
                        part_path
                    )
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

                        if not chunk:
                            continue

                        file.write(
                            chunk
                        )

                        downloaded += len(
                            chunk
                        )

                        if progress_callback:

                            progress_callback(
                                downloaded,
                                total
                            )

                # ------------------------------------------
                # Kiểm tra file
                # ------------------------------------------

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
                        "File tải về rỗng."
                    )

                # ------------------------------------------
                # Rename atomic
                # ------------------------------------------

                os.replace(
                    part_path,
                    output_path
                )

                return {
                    "success": True,
                    "path": output_path,
                    "size": size,
                }

        except Exception as e:

            last_error = e

            logger.warning(
                "[MEDIA] attempt %s/%s: %s",
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

                delay = min(
                    30,
                    2 ** attempt
                )

                delay += random.uniform(
                    0.5,
                    2
                )

                time.sleep(
                    delay
                )

    return {
        "success": False,
        "path": None,
        "size": 0,
        "error": str(
            last_error
        )
        if last_error
        else "Download failed."
    }


# ============================================================
# PHOTO FILE
# ============================================================

def photo_file(
    username
):

    return os.path.join(
        RESULT_DIR,
        f"{clean_username(username)}_photo.txt"
    )


# ============================================================
# ERROR FILE
# ============================================================

def error_file(
    username
):

    return os.path.join(
        RESULT_DIR,
        f"{clean_username(username)}_error.txt"
    )


# ============================================================
# READ LOGGED URLS
# ============================================================

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
                    result.add(
                        url
                    )

    except Exception:

        pass

    return result


# ============================================================
# APPEND UNIQUE
# ============================================================

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
                value
                + "\n"
            )

    return True


# ============================================================
# APPEND PHOTOS
# ============================================================

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

        image_url = (
            str(image_url)
            .strip()
        )

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


# ============================================================
# SAVE ERROR
# ============================================================

def save_error(
    username,
    video_url,
    reason
):

    filepath = error_file(
        username
    )

    append_unique(
        filepath,
        video_url,
        reason
    )


# ============================================================
# REMOVE ERROR
# ============================================================

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
                line
                .split(
                    " | ",
                    1
                )[0]
                .strip()
            )

            if value != video_url:

                new_lines.append(
                    line
                )

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
            "[ERROR LOG] remove error failed"
        )


# ============================================================
# YT-DLP PROFILE OPTIONS
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

        if stop_checker and stop_checker():

            raise asyncio.CancelledError

        try:

            options = profile_options()

            with yt_dlp.YoutubeDL(
                options
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

                    if stop_checker and stop_checker():

                        raise asyncio.CancelledError

                    if not entry:

                        continue

                    video_id = entry.get(
                        "id"
                    )

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

                    item = dict(
                        entry
                    )

                    item["webpage_url"] = (
                        entry_url
                    )

                    item.setdefault(
                        "uploader",
                        username
                    )

                    save_video(
                        item
                    )

                    count += 1

                    if progress_callback:

                        progress_callback(
                            count,
                            video_id
                        )

                # ------------------------------------------
                # CHỈ COMPLETE KHI ITERATOR KẾT THÚC BÌNH
                # THƯỜNG
                # ------------------------------------------

                state_set(
                    complete_key,
                    "1"
                )

                return {
                    "success": True,
                    "complete": True,
                    "count": count,
                    "username": username,
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

            time.sleep(
                delay
            )

    return {
        "success": False,
        "complete": False,
        "count": count_videos(),
        "username": username,
        "error": str(
            last_error
        )
        if last_error
        else "Scan failed."
    }


# ============================================================
# DOWNLOAD ONE VIDEO - TIKWM
# ============================================================

def download_one_tikwm(
    session,
    username,
    video_id,
    video_url,
    progress_callback=None
):

    data = tikwm_lookup(
        session,
        video_url
    )

    parsed = parse_tikwm_data(
        data
    )

    media_type = parsed[
        "type"
    ]

    # ========================================================
    # PHOTO
    # ========================================================

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
            "added": added,
        }

    # ========================================================
    # VIDEO
    # ========================================================

    if media_type != "video":

        raise RuntimeError(
            "TikWM không trả về media video."
        )

    media_url = parsed[
        "media_url"
    ]

    output_path = video_output_path(
        username,
        video_id
    )

    # ========================================================
    # FILE ĐÃ TỒN TẠI
    # ========================================================

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
                "existing": True,
            }

    # ========================================================
    # DOWNLOAD
    # ========================================================

    result = download_media(
        session,
        media_url,
        output_path,
        progress_callback
    )

    if not result["success"]:

        set_download_record(
            video_id,
            status="error",
            engine="tikwm",
            media_type="video",
            media_url=media_url,
            file_path=None,
            error=result.get(
                "error"
            )
        )

        raise RuntimeError(
            result.get(
                "error"
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
        "existing": False,
    }


# ============================================================
# YT-DLP FALLBACK
# ============================================================

def download_one_ytdlp(
    username,
    video_id,
    video_url
):

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

    # --------------------------------------------------------
    # Cookie chỉ bật nếu cấu hình TRUE.
    # Không hardcode cookie.
    # --------------------------------------------------------

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
                [
                    video_url
                ]
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
                }

        return {
            "success": False,
            "error":
                "yt-dlp không tạo được file."
        }

    except Exception as e:

        return {
            "success": False,
            "error": str(e)
        }


# ============================================================
# DOWNLOAD ALL
# ============================================================

def download_all_sync(
    username,
    progress_callback=None,
    stop_checker=None
):

    username = clean_username(
        username
    )

    rows = get_all_videos()

    total = len(
        rows
    )

    if total == 0:

        return {
            "success": False,
            "error":
                "Database chưa có video."
        }

    session = create_http_session()

    stats = {
        "total": total,
        "downloaded": 0,
        "photo": 0,
        "error": 0,
        "skipped": 0,
        "fallback": 0,
    }

    try:

        for index, row in enumerate(
            rows,
            start=1
        ):

            if stop_checker and stop_checker():

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

            # ------------------------------------------------
            # Check file
            # ------------------------------------------------

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

                    stats[
                        "skipped"
                    ] += 1

                    set_download_record(
                        video_id,
                        status="downloaded",
                        engine="local",
                        media_type="video",
                        media_url=None,
                        file_path=output_path,
                        error=None
                    )

                    if progress_callback:

                        progress_callback(
                            index,
                            total,
                            video_id,
                            "skip",
                            100,
                            size,
                            size
                        )

                    continue

            # ------------------------------------------------
            # TikWM
            # ------------------------------------------------

            try:

                result = download_one_tikwm(
                    session,
                    username,
                    video_id,
                    video_url,
                    progress_callback=lambda current, total_size:
                        progress_callback(
                            index,
                            total,
                            video_id,
                            "download",
                            (
                                (
                                    current /
                                    total_size
                                ) * 100
                                if total_size
                                else 0
                            ),
                            current,
                            total_size
                        )
                        if progress_callback
                        else None
                )

                if result["type"] == "photo":

                    stats[
                        "photo"
                    ] += 1

                else:

                    stats[
                        "downloaded"
                    ] += 1

                # --------------------------------------------
                # Delay
                # --------------------------------------------

                time.sleep(
                    random.uniform(
                        TIKWM_DELAY_MIN,
                        TIKWM_DELAY_MAX
                    )
                )

                continue

            except asyncio.CancelledError:

                raise

            except Exception as tikwm_error:

                logger.warning(
                    "[TIKWM DOWNLOAD] "
                    "%s -> %s",
                    video_id,
                    tikwm_error
                )

            # ------------------------------------------------
            # YT-DLP FALLBACK
            # ------------------------------------------------

            if USE_YTDLP_FALLBACK:

                fallback = download_one_ytdlp(
                    username,
                    video_id,
                    video_url
                )

                if fallback["success"]:

                    stats[
                        "downloaded"
                    ] += 1

                    stats[
                        "fallback"
                    ] += 1

                    set_download_record(
                        video_id,
                        status="downloaded",
                        engine="ytdlp",
                        media_type="video",
                        media_url=None,
                        file_path=fallback[
                            "path"
                        ],
                        error=None
                    )

                    remove_error(
                        username,
                        video_url
                    )

                    continue

            # ------------------------------------------------
            # ERROR
            # ------------------------------------------------

            stats[
                "error"
            ] += 1

            save_error(
                username,
                video_url,
                "TikWM + yt-dlp failed"
            )

            set_download_record(
                video_id,
                status="error",
                engine="tikwm/ytdlp",
                media_type="unknown",
                media_url=None,
                file_path=None,
                error="Download failed"
            )

    finally:

        session.close()

    return {
        "success": True,
        **stats,
    }


# ============================================================
# FORMAT PROGRESS
# ============================================================

def format_progress(
    index,
    total,
    video_id,
    mode,
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

    if mode == "skip":

        status = (
            "⏭ Đã tồn tại"
        )

    else:

        status = (
            "⬇️ Đang tải"
        )

    return (
        "╭────────────────────────╮\n"
        "│    🎵 <b>TIKTOK</b>       │\n"
        "╰────────────────────────╯\n\n"

        f"📦 <b>{index}/{total}</b>\n"
        f"🆔 <code>{video_id}</code>\n\n"

        f"{status}\n"
        f"{bar} "
        f"<b>{percent:.1f}%</b>\n\n"

        f"💾 "
        f"{format_bytes(current)}"
        " / "
        f"{format_bytes(total_size)}"
    )


# ============================================================
# SCAN WORKER
# ============================================================

async def scan_worker(
    bot,
    event,
    username,
    message
):

    user_id = event.sender_id

    sessions = get_sessions(
        bot
    )

    current_task = (
        asyncio.current_task()
    )

    track_current_task(
        user_id,
        current_task
    )

    try:

        def stopped():

            session = sessions.get(
                user_id
            )

            if not session:

                return True

            if not session.get(
                "running",
                False
            ):

                return True

            return False

        def progress(
            count,
            video_id
        ):

            return

        await message.edit(
            "🔎 <b>Đang quét TikTok...</b>\n\n"
            f"👤 <code>@{username}</code>\n\n"
            "⏳ Đang lấy danh sách video...",
            parse_mode="html"
        )

        result = await asyncio.to_thread(
            scan_profile_sync,
            username,
            progress,
            stopped
        )

        if result.get(
            "complete"
        ):

            total = count_videos()

            await message.edit(
                "╭────────────────────────╮\n"
                "│   🎵 <b>TIKTOK SCANNER</b>  │\n"
                "╰────────────────────────╯\n\n"

                f"👤 <b>@{username}</b>\n\n"

                "📊 <b>KẾT QUẢ</b>\n\n"

                f"🎬 Video: "
                f"<b>{total}</b>\n\n"

                "✅ Scan: <b>COMPLETE</b>\n\n"

                "━━━━━━━━━━━━━━━━━━━━\n\n"

                f"📥 Dùng:\n"
                f"<code>/tiktokdownload {username}</code>",
                parse_mode="html"
            )

        else:

            await message.edit(
                "⚠️ <b>Scan chưa hoàn tất.</b>\n\n"
                f"👤 @{username}\n"
                f"📦 Đã lưu: {count_videos()}\n\n"
                "❌ Profile chưa được đánh dấu "
                "<b>COMPLETE</b>.\n\n"
                f"⚠️ {result.get('error', 'Unknown error')}",
                parse_mode="html"
            )

    except asyncio.CancelledError:

        try:

            await message.edit(
                "🛑 <b>Đã dừng quét TikTok.</b>\n\n"
                f"👤 @{username}\n\n"
                f"💾 Database đã lưu: "
                f"<b>{count_videos()}</b> video.\n\n"
                "Dữ liệu đã lưu vẫn được giữ.",
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
                f"<code>{str(e)[:1000]}</code>",
                parse_mode="html"
            )

        except Exception:
            pass

    finally:

        untrack_current_task(
            user_id,
            current_task
        )


# ============================================================
# DOWNLOAD WORKER
# ============================================================

async def download_worker(
    bot,
    event,
    username,
    message
):

    user_id = event.sender_id

    sessions = get_sessions(
        bot
    )

    current_task = (
        asyncio.current_task()
    )

    track_current_task(
        user_id,
        current_task
    )

    last_edit = 0

    try:

        def stopped():

            session = sessions.get(
                user_id
            )

            if not session:

                return True

            if not session.get(
                "running",
                False
            ):

                return True

            return False

        async def update(
            text
        ):

            nonlocal last_edit

            now = time.monotonic()

            if (
                now - last_edit
                < 1.0
            ):

                return

            last_edit = now

            try:

                await message.edit(
                    text,
                    parse_mode="html"
                )

            except Exception:
                pass

        def progress(
            index,
            total,
            video_id,
            mode,
            percent,
            current,
            total_size
        ):

            text = format_progress(
                index,
                total,
                video_id,
                mode,
                percent,
                current,
                total_size
            )

            try:

                asyncio.run_coroutine_threadsafe(
                    update(text),
                    asyncio.get_running_loop()
                )

            except Exception:

                pass

        total = len(
            get_all_videos()
        )

        await message.edit(
            "╭────────────────────────╮\n"
            "│   🎵 <b>TIKTOK DOWNLOAD</b> │\n"
            "╰────────────────────────╯\n\n"

            f"👤 <b>@{username}</b>\n\n"

            f"📦 Database: "
            f"<b>{total}</b> video\n\n"

            "⚡ Engine: <b>TikWM HD</b>\n"
            "⏳ Đang chuẩn bị...",
            parse_mode="html"
        )

        result = await asyncio.to_thread(
            download_all_sync,
            username,
            progress,
            stopped
        )

        await message.edit(
            "╭────────────────────────╮\n"
            "│   🎵 <b>TIKTOK DOWNLOAD</b> │\n"
            "╰────────────────────────╯\n\n"

            f"👤 <b>@{username}</b>\n\n"

            "📊 <b>KẾT QUẢ</b>\n\n"

            f"📦 Tổng: "
            f"<b>{result['total']}</b>\n"

            f"✅ Tải thành công: "
            f"<b>{result['downloaded']}</b>\n"

            f"⏭ Đã tồn tại: "
            f"<b>{result['skipped']}</b>\n"

            f"🖼 Photo: "
            f"<b>{result['photo']}</b>\n"

            f"🔄 Fallback yt-dlp: "
            f"<b>{result['fallback']}</b>\n"

            f"❌ Lỗi: "
            f"<b>{result['error']}</b>\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "📁 Video:\n"
            f"<code>downloads/{username}/</code>\n\n"

            "🖼 Photo:\n"
            f"<code>results/{username}_photo.txt</code>\n\n"

            "❌ Error:\n"
            f"<code>results/{username}_error.txt</code>",
            parse_mode="html"
        )

    except asyncio.CancelledError:

        try:

            await message.edit(
                "🛑 <b>Đã dừng tải TikTok.</b>\n\n"
                f"👤 @{username}\n\n"
                "💾 Những file đã tải xong "
                "vẫn được giữ lại.\n\n"
                "Chạy lại download sẽ bỏ qua "
                "file đã hoàn thành.",
                parse_mode="html"
            )

        except Exception:
            pass

        raise

    except Exception as e:

        logger.exception(
            "[TIKTOK DOWNLOAD WORKER]"
        )

        try:

            await message.edit(
                "❌ <b>Download TikTok lỗi.</b>\n\n"
                f"<code>{str(e)[:1000]}</code>",
                parse_mode="html"
            )

        except Exception:
            pass

    finally:

        untrack_current_task(
            user_id,
            current_task
        )


# ============================================================
# START SESSION
# ============================================================

async def start_session(
    bot,
    event,
    command,
    username
):

    user_id = event.sender_id

    sessions = get_sessions(
        bot
    )

    # --------------------------------------------------------
    # Dừng task cũ + xóa session cũ
    # --------------------------------------------------------

    await replace_user_tasks(
        user_id
    )

    sessions.pop(
        user_id,
        None
    )

    # --------------------------------------------------------
    # Session mới
    # --------------------------------------------------------

    sessions[user_id] = {

        "command":
            command,

        "username":
            username,

        "running":
            True,

        "processing":
            True,
    }

    message = await event.reply(
        "⏳ <b>Đang khởi động TikTok...</b>",
        parse_mode="html"
    )

    # --------------------------------------------------------
    # Tạo worker
    # --------------------------------------------------------

    if command == "scan":

        coro = scan_worker(
            bot,
            event,
            username,
            message
        )

    else:

        coro = download_worker(
            bot,
            event,
            username,
            message
        )

    await replace_user_tasks(
        user_id,
        coro
    )


# ============================================================
# REGISTER
# ============================================================

def register(
    bot,
    notify_bot
):

    # ========================================================
    # INIT DATABASE
    # ========================================================

    init_tiktok_database()

    sessions = get_sessions(
        bot
    )

    # ========================================================
    # /tiktok
    # ========================================================

    @bot.on(
        events.NewMessage(
            pattern=r"^/tiktok(?:@\w+)?(?:\s+(.+))?$"
        )
    )
    async def tiktok_command(
        event
    ):

        username = (
            event.pattern_match.group(
                1
            )
        )

        if not username:

            # ------------------------------------------------
            # Chờ username
            # ------------------------------------------------

            await replace_user_tasks(
                event.sender_id
            )

            sessions.pop(
                event.sender_id,
                None
            )

            sessions[
                event.sender_id
            ] = {

                "command":
                    "tiktok",

                "running":
                    True,

                "processing":
                    False,
            }

            await event.reply(
                "╭────────────────────────╮\n"
                "│   🎵 <b>TIKTOK SCANNER</b>  │\n"
                "╰────────────────────────╯\n\n"

                "📥 <b>Gửi username TikTok.</b>\n\n"

                "Ví dụ:\n"
                "<code>@nguyenvanloiofficial</code>\n\n"

                "Hoặc:\n"
                "<code>"
                "https://www.tiktok.com/"
                "@nguyenvanloiofficial"
                "</code>\n\n"

                "━━━━━━━━━━━━━━━━━━━━\n\n"

                "⚡ Bot sẽ:\n"
                "• Quét profile\n"
                "• Lưu SQLite\n"
                "• Checkpoint\n"
                "• Không giới hạn video\n"
                "• Retry 429\n\n"

                "🛑 <code>/stop</code> → Dừng",
                parse_mode="html"
            )

            return

        username = clean_username(
            username
        )

        if not username:

            await event.reply(
                "❌ Username TikTok không hợp lệ."
            )

            return

        await start_session(
            bot,
            event,
            "scan",
            username
        )

    # ========================================================
    # NHẬN USERNAME
    # ========================================================

    @bot.on(
        events.NewMessage()
    )
    async def receive_username(
        event
    ):

        user_id = event.sender_id

        text = (
            event.raw_text
            .strip()
        )

        if not text:
            return

        if text.startswith("/"):
            return

        session = sessions.get(
            user_id
        )

        if not session:
            return

        if session.get(
            "command"
        ) != "tiktok":

            return

        if session.get(
            "processing"
        ):

            return

        username = clean_username(
            text
        )

        if not username:

            return

        session[
            "processing"
        ] = True

        await start_session(
            bot,
            event,
            "scan",
            username
        )

    # ========================================================
    # /tiktokdownload
    # ========================================================

    @bot.on(
        events.NewMessage(
            pattern=r"^/tiktokdownload(?:@\w+)?(?:\s+(.+))?$"
        )
    )
    async def tiktok_download(
        event
    ):

        username = (
            event.pattern_match.group(
                1
            )
        )

        if not username:

            await event.reply(
                "╭────────────────────────╮\n"
                "│   📥 <b>TIKTOK DOWNLOAD</b> │\n"
                "╰────────────────────────╯\n\n"

                "Dùng:\n"
                "<code>/tiktokdownload username</code>\n\n"

                "Ví dụ:\n"
                "<code>/tiktokdownload "
                "nguyenvanloiofficial</code>",
                parse_mode="html"
            )

            return

        username = clean_username(
            username
        )

        if not username:

            await event.reply(
                "❌ Username TikTok không hợp lệ."
            )

            return

        await start_session(
            bot,
            event,
            "download",
            username
        )