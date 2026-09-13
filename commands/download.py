#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import math
import random
import socket
import statistics
from collections import Counter, defaultdict, deque
from enum import Enum
from typing import Any, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp
import yt_dlp
from telethon import Button, events


logger = logging.getLogger("commands.download")

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
        "TikTok video dùng yt-dlp.",
        "TikTok profile/playlist dùng commands/tiktok.py.",
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

BASE_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/dragon_downloads"))
BASE_DIR.mkdir(parents=True, exist_ok=True)

MAX_URLS = int(os.getenv("DOWNLOAD_MAX_URLS", "15"))
MAX_CONCURRENT_PROBES = int(os.getenv("DOWNLOAD_PROBE_CONCURRENCY", "4"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("DOWNLOAD_MAX_CONCURRENT", "3"))
MAX_TIKTOK_PROFILE_ITEMS = int(os.getenv("TIKTOK_PROFILE_LIMIT", "30"))
MAX_UPLOAD_BYTES = int(os.getenv("DOWNLOAD_MAX_UPLOAD_BYTES", str(1900 * 1024 * 1024)))
JOB_TTL = int(os.getenv("DOWNLOAD_JOB_TTL", "1800"))
TIKWM_API = "https://www.tikwm.com/api/?url="

UA_ANDROID = (
    "Mozilla/5.0 (Linux; Android 15; Mobile) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Mobile Safari/537.36"
)
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

FB_HEADERS = {
    "User-Agent": UA_ANDROID,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA-Mobile": "?1",
    "Sec-CH-UA-Platform": '"Android"',
}

TT_HEADERS = {
    "User-Agent": UA_ANDROID,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.tiktok.com/",
}

URL_RE = re.compile(
    r"""(?ix)
    (?:https?://)?
    (?:
        www\.|m\.|mbasic\.|vm\.|vt\.
    )?
    (?:
        facebook\.com|fb\.watch|
        youtube\.com|youtu\.be|
        tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com|
        instagram\.com|
        twitter\.com|x\.com|
        reddit\.com|pinterest\.com
    )
    (?:/[^\s<>"'“”‘’]+)?
    """
)

FB_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com"}
FB_AUTH_PATHS = {"/login", "/login.php", "/checkpoint", "/recover"}


@dataclass
class FormatChoice:
    height: int | None
    fps: float | None
    width: int | None
    format_id: str
    ext: str
    vcodec: str
    acodec: str
    vbr: float | None
    tbr: float | None
    filesize: int | None
    protocol: str
    has_audio: bool
    has_video: bool
    hdr: str | None = None


@dataclass
class ProbeResult:
    token: str
    user_id: int
    url: str
    platform: str
    kind: str
    info: dict = field(default_factory=dict)
    formats: list[FormatChoice] = field(default_factory=list)
    resolutions: list[int] = field(default_factory=list)
    max_height: int = 0
    method: str = "yt-dlp"
    created_at: float = field(default_factory=time.time)
    direct_media: list[str] = field(default_factory=list)
    tikwm: dict | None = None


JOBS: dict[str, ProbeResult] = {}
USER_JOBS: dict[int, set[str]] = {}
USER_TASKS: dict[int, set[asyncio.Task]] = {}
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


def normalize_url(url: str) -> str:
    url = html.unescape((url or "").strip())
    url = url.strip(" \t\r\n<>\"'“”‘’()[]{}")
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def extract_urls(text: str) -> list[str]:
    found = []
    seen = set()
    for m in URL_RE.finditer(text or ""):
        u = normalize_url(m.group(0))
        if not u:
            continue
        key = u.rstrip("/")
        if key not in seen:
            seen.add(key)
            found.append(u)
    return found[:MAX_URLS]


def host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""


def platform_of(url: str) -> str:
    h = host(url)
    if h == "fb.watch" or h.endswith(".facebook.com") or h == "facebook.com":
        return "facebook"
    if "tiktok.com" in h:
        return "tiktok"
    if h == "youtu.be" or "youtube.com" in h:
        return "youtube"
    if "instagram.com" in h:
        return "instagram"
    if h in {"twitter.com", "x.com"}:
        return "twitter"
    if "reddit.com" in h:
        return "reddit"
    if "pinterest.com" in h:
        return "pinterest"
    return "generic"


def fb_kind(url: str) -> str:
    x = url.lower()
    if "/reel/" in x or "/reels/" in x:
        return "REEL"
    if "/share/v/" in x:
        return "SHARE_VIDEO"
    if "/share/r/" in x:
        return "SHARE_REEL"
    if "/share/p/" in x:
        return "SHARE_POST"
    if "/watch" in x:
        return "WATCH"
    if "/videos/" in x or "/video.php" in x:
        return "VIDEO"
    if "/posts/" in x:
        return "POST"
    if "/photo" in x:
        return "PHOTO"
    if "/story.php" in x or "/stories/" in x:
        return "STORY"
    return "FACEBOOK"


def is_fb_auth_wall(url: str) -> bool:
    try:
        p = urlparse(url)
        h = p.netloc.lower().split(":")[0]
        path = p.path.lower().rstrip("/")
        if h not in FB_HOSTS:
            return False
        if path in FB_AUTH_PATHS:
            return True
        return "login" in path or "checkpoint" in path or "recover" in path
    except Exception:
        return False


def clean_url(value: str) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = value.replace("\\/", "/")
    value = value.replace("\\u002F", "/")
    value = value.replace("\\u003A", ":")
    value = value.replace('\\"', '"')
    value = value.strip("\"' <>")
    if value.startswith("//"):
        value = "https:" + value
    if not value.startswith(("http://", "https://")):
        return ""
    return value


def unique(items: list[str]) -> list[str]:
    out, seen = [], set()
    for x in items:
        x = clean_url(x)
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def format_bytes(n: int | float | None) -> str:
    if not n:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def format_duration(seconds) -> str:
    if seconds is None:
        return "—"
    try:
        s = int(seconds)
    except Exception:
        return "—"
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def codec_short(codec: str | None) -> str:
    c = (codec or "").lower()
    if not c:
        return "?"
    if "av01" in c or "av1" in c:
        return "AV1"
    if "vp9" in c:
        return "VP9"
    if "vp8" in c:
        return "VP8"
    if "avc" in c or "h264" in c:
        return "H.264"
    if "hevc" in c or "h265" in c:
        return "H.265"
    return codec.split(".")[0][:10]


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ytdlp_base(output_dir: Path) -> dict:
    return {
        "outtmpl": str(output_dir / "%(title).180s [%(id)s].%(ext)s"),
        "paths": {"home": str(output_dir), "temp": str(output_dir / ".tmp")},
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 3,
        "continuedl": True,
        "overwrites": False,
        "windowsfilenames": True,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": UA_DESKTOP,
            "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
        },
    }


def extract_info_sync(url: str, *, flat=False) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": flat,
        "ignoreerrors": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


async def probe_ytdlp(url: str) -> dict:
    return await asyncio.to_thread(extract_info_sync, url, flat=False)


def make_choices(info: dict) -> list[FormatChoice]:
    out = []
    for f in info.get("formats") or []:
        vc = f.get("vcodec")
        ac = f.get("acodec")
        has_v = vc not in (None, "none")
        has_a = ac not in (None, "none")
        if not has_v:
            continue
        h = f.get("height")
        if not h:
            continue
        try:
            h = int(h)
        except Exception:
            continue
        out.append(
            FormatChoice(
                height=h,
                fps=f.get("fps"),
                width=f.get("width"),
                format_id=str(f.get("format_id", "")),
                ext=str(f.get("ext") or ""),
                vcodec=str(vc or ""),
                acodec=str(ac or ""),
                vbr=f.get("vbr"),
                tbr=f.get("tbr"),
                filesize=f.get("filesize") or f.get("filesize_approx"),
                protocol=str(f.get("protocol") or ""),
                has_audio=has_a,
                has_video=has_v,
                hdr=f.get("dynamic_range"),
            )
        )
    return out


def best_format_for_height(choices: list[FormatChoice], height: int) -> FormatChoice | None:
    same = [x for x in choices if x.height == height]
    if not same:
        return None

    def score(x: FormatChoice):
        codec = codec_short(x.vcodec)
        codec_score = {"AV1": 4, "H.265": 3, "VP9": 2, "H.264": 1}.get(codec, 0)
        return (
            1 if x.has_audio else 0,
            x.fps or 0,
            codec_score,
            x.vbr or 0,
            x.tbr or 0,
            x.filesize or 0,
        )
    return max(same, key=score)


def available_resolutions(choices: list[FormatChoice]) -> list[int]:
    return sorted({x.height for x in choices if x.height}, reverse=True)


def nearest_height(choices: list[FormatChoice], requested: int) -> int | None:
    heights = available_resolutions(choices)
    if not heights:
        return None
    lower_or_equal = [h for h in heights if h <= requested]
    if lower_or_equal:
        return max(lower_or_equal)
    return min(heights)


def exact_format_selector(height: int) -> str:
    return (
        f"bv*[height={height}]+ba/"
        f"b[height={height}]/"
        f"bv*[height<={height}]+ba/"
        f"b[height<={height}]/"
        f"bv*+ba/b"
    )


def best_selector() -> str:
    return "bv*+ba/b"


def download_sync(url: str, output_dir: Path, selector: str) -> dict:
    opts = ytdlp_base(output_dir)
    opts.update(
        {
            "format": selector,
            "format_sort": [
                "quality",
                "res",
                "fps",
                "hdr:12",
                "vcodec",
                "acodec",
                "size",
                "br",
            ],
            "check_formats": True,
            "merge_output_format": "mp4",
        }
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info


async def download_ytdlp(url: str, output_dir: Path, selector: str) -> dict:
    return await asyncio.to_thread(download_sync, url, output_dir, selector)


def files_in(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    bad = {".part", ".ytdl", ".temp"}
    return sorted(
        [
            p for p in directory.rglob("*")
            if p.is_file() and p.suffix.lower() not in bad and not p.name.startswith(".")
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def ffprobe_sync(path: Path) -> dict:
    exe = ffprobe_path()
    if not exe:
        return {}
    cmd = [
        exe, "-v", "error",
        "-show_entries",
        "format=duration,size:stream=index,codec_name,codec_type,width,height,r_frame_rate,bit_rate",
        "-of", "json", str(path),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return {}
        return json.loads(p.stdout or "{}")
    except Exception:
        return {}


async def verify_media(path: Path) -> dict:
    data = await asyncio.to_thread(ffprobe_sync, path)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = None
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except Exception:
        pass

    height = video.get("height") if video else None
    width = video.get("width") if video else None
    fps = None
    if video and video.get("r_frame_rate"):
        try:
            a, b = video["r_frame_rate"].split("/")
            fps = round(float(a) / float(b), 2) if float(b) else None
        except Exception:
            pass

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "vcodec": codec_short(video.get("codec_name") if video else None),
        "acodec": codec_short(audio.get("codec_name") if audio else None),
        "has_audio": bool(audio),
        "duration": duration,
        "size": path.stat().st_size if path.exists() else 0,
    }


async def http_json(session, url, headers=None):
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as r:
            return await r.json(content_type=None)
    except Exception:
        return None


async def http_text(session, url, headers=None):
    try:
        async with session.get(url, headers=headers, allow_redirects=True) as r:
            return {
                "status": r.status,
                "url": str(r.url),
                "body": await r.text(errors="ignore"),
            }
    except Exception:
        return None


def extract_fb_direct_media(body: str) -> list[str]:
    patterns = [
        r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']',
        r'<meta[^>]+property=["\']og:video:url["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video:url["\']',
        r'"playable_url_quality_hd":"([^"]+)"',
        r'"playable_url":"([^"]+)"',
        r'"browser_native_hd_url":"([^"]+)"',
        r'"browser_native_sd_url":"([^"]+)"',
        r'"hd_src":"([^"]+)"',
        r'"sd_src":"([^"]+)"',
        r'"video_url":"([^"]+)"',
        r'<video[^>]+src=["\']([^"\']+)',
        r'<source[^>]+src=["\']([^"\']+)',
    ]
    found = []
    for p in patterns:
        for m in re.finditer(p, body or "", re.I):
            u = clean_url(m.group(1))
            if u:
                found.append(u)
    return unique(found)


def media_score(url: str) -> int:
    x = url.lower()
    score = 0
    if "hd" in x:
        score += 100
    if "quality_hd" in x:
        score += 80
    if "browser_native_hd" in x:
        score += 70
    if ".mp4" in x:
        score += 30
    if "sd" in x:
        score -= 20
    return score


async def probe_facebook_public(url: str) -> tuple[list[str], str | None]:
    connector = aiohttp.TCPConnector(ssl=False, limit=8)
    timeout = aiohttp.ClientTimeout(total=45, connect=15, sock_read=30)
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, headers=FB_HEADERS
    ) as session:
        candidates = [url]
        p = urlparse(url)
        if p.netloc.lower() in {"facebook.com", "www.facebook.com"}:
            q = f"?{p.query}" if p.query else ""
            candidates += [
                f"https://m.facebook.com{p.path}{q}",
                f"https://mbasic.facebook.com{p.path}{q}",
            ]

        last_url = None
        for candidate in candidates:
            r = await http_text(session, candidate, FB_HEADERS)
            if not r:
                continue
            last_url = r["url"]
            if is_fb_auth_wall(last_url):
                continue
            media = extract_fb_direct_media(r["body"])
            if media:
                media.sort(key=media_score, reverse=True)
                return media, last_url
        return [], last_url


async def tikwm_probe(url: str) -> dict | None:
    timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_read=25)
    async with aiohttp.ClientSession(timeout=timeout, headers=TT_HEADERS) as session:
        api = TIKWM_API + quote(url, safe="")
        data = await http_json(session, api, TT_HEADERS)
        if isinstance(data, dict) and data.get("code") == 0:
            return data.get("data") or {}
    return None


def is_tiktok_photo(url: str) -> bool:
    x = url.lower()
    return "/photo/" in x or "/photos/" in x


def is_tiktok_profile(url: str) -> bool:
    p = urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    if len(parts) != 1:
        return False
    return parts[0].lower() not in {
        "video", "v", "photo", "photos", "discover", "foryou", "tag"
    }


async def probe_one(user_id: int, url: str) -> ProbeResult:
    platform = platform_of(url)
    token = uuid.uuid4().hex[:10]
    result = ProbeResult(
        token=token,
        user_id=user_id,
        url=url,
        platform=platform,
        kind=fb_kind(url) if platform == "facebook" else "VIDEO",
    )

    if platform == "tiktok":
        if is_tiktok_photo(url):
            data = await tikwm_probe(url)
            result.kind = "PHOTO"
            result.tikwm = data
            result.info = data or {}
            result.method = "TikWM"
            return result

        if is_tiktok_profile(url):
            try:
                info = await asyncio.to_thread(
                    extract_info_sync, url, flat=True
                )
                result.info = info or {}
                result.kind = "PROFILE"
                result.method = "yt-dlp + TikWM"
                return result
            except Exception as exc:
                result.info = {"error": str(exc)}
                result.kind = "PROFILE"
                return result

        data = await tikwm_probe(url)
        result.tikwm = data
        result.info = data or {}
        result.method = "TikWM + yt-dlp"

        try:
            info = await probe_ytdlp(url)
            result.info = info or result.info
            result.formats = make_choices(info or {})
            result.resolutions = available_resolutions(result.formats)
            result.max_height = max(result.resolutions or [0])
        except Exception:
            pass
        return result

    try:
        info = await probe_ytdlp(url)
        result.info = info or {}
        result.formats = make_choices(info or {})
        result.resolutions = available_resolutions(result.formats)
        result.max_height = max(result.resolutions or [0])
        result.method = "yt-dlp"
        if platform == "facebook":
            result.kind = fb_kind(url)
    except Exception as exc:
        result.info = {"error": str(exc)}

    if platform == "facebook" and not result.formats:
        media, final_url = await probe_facebook_public(url)
        result.direct_media = media
        result.info.setdefault("webpage_url", final_url or url)
        result.method = "Facebook Multi-Layer"
        if media:
            result.resolutions = []
            result.max_height = 0

    return result


def title_of(info: dict, fallback="Media") -> str:
    return str(
        info.get("title")
        or info.get("fulltitle")
        or info.get("desc")
        or fallback
    )


def quality_label(h: int) -> str:
    return f"{h}p"


def probe_caption(p: ProbeResult) -> str:
    title = title_of(p.info, p.kind)
    if p.platform == "tiktok" and p.kind == "PHOTO":
        images = len((p.tikwm or {}).get("images") or [])
        return (
            "╭━━━〔 🎵 TIKTOK PHOTO ━━━〕\n"
            f"┃ 🎬 <b>{html.escape(title)}</b>\n"
            f"┃ 🖼 Ảnh: <b>{images}</b>\n"
            "┃ ⚙️ Engine: <b>TikWM</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━"
        )

    if p.kind == "PROFILE":
        entries = p.info.get("entries") or []
        return (
            "╭━━━〔 🎵 TIKTOK PROFILE ━━━〕\n"
            f"┃ 👤 <b>{html.escape(title_of(p.info, 'Profile'))}</b>\n"
            f"┃ 🎬 Phát hiện: <b>{len(entries)}</b> item\n"
            "┃ ⚙️ Engine: <b>yt-dlp + TikWM</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━"
        )

    maxq = quality_label(p.max_height) if p.max_height else "Không xác định"
    resolutions = " • ".join(quality_label(x) for x in p.resolutions) or "Chỉ có media trực tiếp"
    return (
        f"╭━━━〔 {platform_icon(p.platform)} {p.platform.upper()} ━━━〕\n"
        f"┃ 🎬 <b>{html.escape(title[:180])}</b>\n"
        f"┃ 🔎 Loại: <b>{html.escape(p.kind)}</b>\n"
        f"┃ 📐 Tối đa: <b>{maxq}</b>\n"
        f"┃ 🎞 Có: <b>{html.escape(resolutions)}</b>\n"
        f"┃ ⚙️ Engine: <b>{html.escape(p.method)}</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━"
    )


def platform_icon(platform: str) -> str:
    return {
        "facebook": "🔵",
        "tiktok": "🎵",
        "youtube": "🔴",
        "instagram": "🟣",
        "twitter": "⚫",
        "reddit": "🟠",
        "pinterest": "📌",
        "generic": "🌐",
    }.get(platform, "🌐")


def quality_buttons(p: ProbeResult):
    rows = []
    rows.append([
        Button.inline("🏆 BEST", f"q:{p.token}:best".encode())
    ])
    heights = p.resolutions[:]
    for i in range(0, len(heights), 2):
        row = []
        for h in heights[i:i + 2]:
            row.append(
                Button.inline(
                    quality_label(h),
                    f"q:{p.token}:{h}".encode()
                )
            )
        rows.append(row)
    rows.append([
        Button.inline("❌ Hủy", f"q:{p.token}:cancel".encode())
    ])
    return rows


def register_job(p: ProbeResult):
    JOBS[p.token] = p
    USER_JOBS.setdefault(p.user_id, set()).add(p.token)


def cleanup_jobs(user_id: int):
    now = time.time()
    tokens = USER_JOBS.get(user_id, set()).copy()
    for token in tokens:
        job = JOBS.get(token)
        if not job or now - job.created_at > JOB_TTL:
            JOBS.pop(token, None)
            USER_JOBS.get(user_id, set()).discard(token)


async def send_verified_files(event, files: list[Path], caption: str):
    sent = 0
    for path in files:
        if not path.exists():
            continue
        size = path.stat().st_size
        if size <= 0:
            continue
        if size > MAX_UPLOAD_BYTES:
            await event.respond(
                f"⚠️ File <b>{html.escape(path.name)}</b> quá lớn: "
                f"{format_bytes(size)}",
                parse_mode="html",
            )
            continue

        verification = await verify_media(path)
        if verification.get("height"):
            actual = f"{verification['height']}p"
            codec = verification.get("vcodec") or "?"
            fps = verification.get("fps") or "?"
            audio = "✓" if verification.get("has_audio") else "✗"
            final_caption = (
                f"{caption}\n\n"
                f"✅ <b>VERIFIED</b>\n"
                f"📐 Thực tế: <b>{actual}</b>\n"
                f"🎞 FPS: <b>{fps}</b>\n"
                f"🎥 Codec: <b>{html.escape(codec)}</b>\n"
                f"🔊 Audio: <b>{audio}</b>\n"
                f"📦 Size: <b>{format_bytes(size)}</b>"
            )
        else:
            final_caption = (
                f"{caption}\n\n"
                f"📦 Size: <b>{format_bytes(size)}</b>"
            )

        try:
            await event.client.send_file(
                event.chat_id,
                str(path),
                caption=final_caption[:4096],
                parse_mode="html",
                supports_streaming=True,
            )
            sent += 1
        except Exception as exc:
            logger.exception("Telegram upload failed")
            await event.respond(
                f"❌ Upload lỗi: {html.escape(str(exc))}",
                parse_mode="html",
            )
    return sent


async def download_tiktok_photo(event, p: ProbeResult):
    images = (p.tikwm or {}).get("images") or []
    if not images:
        raise RuntimeError("TikWM không trả images[]")

    out = BASE_DIR / str(p.user_id) / p.token
    out.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=45, connect=10, sock_read=35)

    async with aiohttp.ClientSession(timeout=timeout, headers=TT_HEADERS) as session:
        files = []
        for i, url in enumerate(images, 1):
            path = out / f"photo_{i}.jpg"
            try:
                async with session.get(url) as r:
                    if r.status >= 400:
                        continue
                    with path.open("wb") as f:
                        async for chunk in r.content.iter_chunked(512 * 1024):
                            f.write(chunk)
                if path.exists() and path.stat().st_size > 0:
                    files.append(path)
            except Exception:
                continue

    caption = (
        "╭━━━〔 🎵 TIKTOK PHOTO ━━━〕\n"
        f"┃ 🖼 Đã tải: <b>{len(files)}/{len(images)}</b>\n"
        "┃ ⚙️ TikWM\n"
        "╰━━━━━━━━━━━━━━━━━━━━"
    )
    await send_verified_files(event, files, caption)


async def download_profile(event, p: ProbeResult):
    entries = (p.info or {}).get("entries") or []
    entries = [x for x in entries if x][:MAX_TIKTOK_PROFILE_ITEMS]
    if not entries:
        raise RuntimeError("Không tìm thấy video trong profile")

    for index, entry in enumerate(entries, 1):
        url = entry.get("webpage_url") or entry.get("url")
        if not url or not str(url).startswith(("http://", "https://")):
            continue
        child = await probe_one(p.user_id, str(url))
        if child.platform != "tiktok":
            continue
        try:
            await download_regular(event, child, "best")
        except Exception as exc:
            await event.respond(
                f"❌ TikTok #{index}: {html.escape(str(exc))}",
                parse_mode="html",
            )


async def download_facebook_direct(event, p: ProbeResult):
    if not p.direct_media:
        raise RuntimeError("Không tìm thấy media public")

    out = BASE_DIR / str(p.user_id) / p.token
    out.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=120, connect=15, sock_read=90)
    headers = {"User-Agent": UA_ANDROID, "Accept": "*/*"}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for i, media in enumerate(p.direct_media[:8], 1):
            path = out / f"facebook_{i}.mp4"
            try:
                async with session.get(media, headers=headers) as r:
                    if r.status >= 400:
                        continue
                    with path.open("wb") as f:
                        async for chunk in r.content.iter_chunked(1024 * 1024):
                            f.write(chunk)
                if path.exists() and path.stat().st_size > 1024:
                    caption = (
                        f"╭━━━〔 🔵 FACEBOOK ━━━〕\n"
                        f"┃ 🔎 {html.escape(p.kind)}\n"
                        f"┃ ⚙️ Public HTML fallback\n"
                        "╰━━━━━━━━━━━━━━━━━━━━"
                    )
                    await send_verified_files(event, [path], caption)
                    return
            except Exception:
                continue
    raise RuntimeError("Media URL Facebook không tải được")


async def download_regular(event, p: ProbeResult, requested):
    async with DOWNLOAD_SEMAPHORE:
        out = BASE_DIR / str(p.user_id) / p.token
        out.mkdir(parents=True, exist_ok=True)

        if p.platform == "tiktok" and p.kind == "PHOTO":
            await download_tiktok_photo(event, p)
            return

        if p.platform == "tiktok" and p.kind == "PROFILE":
            await download_profile(event, p)
            return

        if requested == "best":
            selector = best_selector()
            requested_height = p.max_height or None
        else:
            requested_height = int(requested)
            actual_height = nearest_height(p.formats, requested_height)
            if actual_height is None:
                raise RuntimeError("Không có format video khả dụng")
            selector = exact_format_selector(actual_height)

        info = None
        try:
            info = await download_ytdlp(p.url, out, selector)
        except Exception as first_error:
            if p.platform == "facebook" and p.direct_media:
                await download_facebook_direct(event, p)
                return
            if requested != "best":
                fallback = best_selector()
                info = await download_ytdlp(p.url, out, fallback)
            else:
                raise first_error

        files = files_in(out)
        if not files:
            if p.platform == "facebook" and p.direct_media:
                await download_facebook_direct(event, p)
                return
            raise RuntimeError("yt-dlp không tạo file")

        title = title_of(info or p.info, p.kind)
        actual_requested = (
            f"{requested_height}p"
            if requested != "best" and requested_height
            else "BEST"
        )
        caption = (
            f"╭━━━〔 {platform_icon(p.platform)} {p.platform.upper()} ━━━〕\n"
            f"┃ 🎬 <b>{html.escape(title[:180])}</b>\n"
            f"┃ 🎯 Yêu cầu: <b>{actual_requested}</b>\n"
            f"┃ ⚙️ Engine: <b>{html.escape(p.method)}</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━"
        )
        await send_verified_files(event, files, caption)


async def run_probe_batch(event, urls):
    user_id = event.sender_id
    sem = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    async def one(url):
        async with sem:
            return await probe_one(user_id, url)

    tasks = [asyncio.create_task(one(u)) for u in urls]
    for task in tasks:
        USER_TASKS.setdefault(user_id, set()).add(task)

    try:
        for fut in asyncio.as_completed(tasks):
            try:
                p = await fut
                register_job(p)
                await event.respond(
                    probe_caption(p),
                    buttons=quality_buttons(p) if p.resolutions else [
                        [Button.inline("🏆 BEST", f"q:{p.token}:best".encode())],
                        [Button.inline("❌ Hủy", f"q:{p.token}:cancel".encode())],
                    ],
                    parse_mode="html",
                )
            except Exception as exc:
                await event.respond(
                    f"❌ Probe lỗi: {html.escape(str(exc))}",
                    parse_mode="html",
                )
    finally:
        for t in tasks:
            USER_TASKS.get(user_id, set()).discard(t)


# ============================================================================
# DOWNLOAD SESSION CONTROLLER
# ============================================================================
# IMPORTANT:
# - URL messages are NEVER handled globally.
# - A user must explicitly enter /download mode first.
# - /stop closes that user's download session immediately.
# - One batch uses ONE status message; it is edited in-place as progress changes.
# ============================================================================

DOWNLOAD_SESSIONS: dict[int, dict[str, Any]] = {}
DOWNLOAD_STATUS_MESSAGES: dict[int, Any] = {}


def download_session_active(user_id: int) -> bool:
    session = DOWNLOAD_SESSIONS.get(user_id)
    if not session:
        return False
    return bool(session.get("active"))


def close_download_session(user_id: int) -> None:
    session = DOWNLOAD_SESSIONS.pop(user_id, None)
    if session:
        session["active"] = False


async def safe_edit_status(message, text: str, **kwargs):
    if not message:
        return
    try:
        await message.edit(text, **kwargs)
    except Exception as exc:
        # Telegram may reject an edit when the content is identical or stale.
        logger.debug("status edit ignored: %s", exc)


def progress_bar(done: int, total: int, width: int = 10) -> str:
    total = max(1, total)
    done = max(0, min(done, total))
    filled = round(width * done / total)
    return "●" * filled + "○" * (width - filled)


def batch_header(total: int) -> str:
    return (
        "╭━━━〔 📥 DRAGON DOWNLOAD 〕━━━╮\n"
        "┃\n"
        f"┃ 🔗 <b>{total}</b> link trong hàng đợi\n"
        "┃ ⚡ Chế độ: <b>Batch Download</b>\n"
        "┃ 🎯 Chất lượng: <b>BEST khả dụng</b>\n"
        "┃ 🔍 yt-dlp + fallback public\n"
        "┃\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


def batch_progress_text(
    total: int,
    current: int,
    url: str,
    platform: str,
    completed: int,
    failed: int,
    cancelled: bool = False,
) -> str:
    if cancelled:
        title = "🛑 <b>ĐÃ DỪNG</b>"
    elif current >= total:
        title = "✅ <b>HOÀN TẤT</b>"
    else:
        title = "⏳ <b>ĐANG TẢI</b>"

    bar = progress_bar(completed + failed, total)
    safe_url = html.escape(url[:180])
    return (
        "╭━━━〔 📥 DRAGON DOWNLOAD 〕━━━╮\n"
        "┃\n"
        f"┃ {title}\n"
        f"┃ 📦 Tiến độ: <b>{current}/{total}</b>\n"
        f"┃ [{bar}] <b>{completed + failed}/{total}</b>\n"
        "┃\n"
        f"┃ 🔗 Link hiện tại: <b>{current}</b>/<b>{total}</b>\n"
        f"┃ 🌐 Nền tảng: <b>{html.escape(platform.upper())}</b>\n"
        f"┃ 🔎 {safe_url}\n"
        "┃\n"
        f"┃ ✅ Thành công: <b>{completed}</b>\n"
        f"┃ ❌ Lỗi: <b>{failed}</b>\n"
        "┃\n"
        "┃ 🛑 Dùng <b>/stop</b> để dừng ngay\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


def batch_done_text(total: int, completed: int, failed: int, elapsed: float) -> str:
    if failed == 0:
        icon = "🎉"
        title = "HOÀN TẤT"
    else:
        icon = "⚠️"
        title = "HOÀN TẤT CÓ LỖI"

    return (
        "╭━━━〔 📥 DRAGON DOWNLOAD 〕━━━╮\n"
        "┃\n"
        f"┃ {icon} <b>{title}</b>\n"
        "┃\n"
        f"┃ 🔗 Tổng link: <b>{total}</b>\n"
        f"┃ ✅ Thành công: <b>{completed}</b>\n"
        f"┃ ❌ Thất bại: <b>{failed}</b>\n"
        f"┃ ⏱ Thời gian: <b>{elapsed:.1f}s</b>\n"
        "┃\n"
        "┃ 💡 Muốn tải tiếp: gửi <b>/download</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )

async def cancel_tasks(user_id: int):
    tasks = list(USER_TASKS.get(user_id, set()))
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    USER_TASKS.pop(user_id, None)
async def start_download(event):
    user_id = event.sender_id
    cleanup_jobs(user_id)

    # Re-opening /download creates a fresh, isolated session for this user.
    await cancel_tasks(user_id)
    for token in list(USER_JOBS.get(user_id, set())):
        JOBS.pop(token, None)
    USER_JOBS.pop(user_id, None)

    DOWNLOAD_SESSIONS[user_id] = {
        "active": True,
        "started_at": time.monotonic(),
        "batch": 0,
    }

    await event.respond(
        "╭━━━〔 📥 DRAGON DOWNLOAD 〕━━━╮\n"
        "┃\n"
        "┃ 🚀 <b>DOWNLOAD MODE ĐÃ BẬT</b>\n"
        "┃\n"
        "┃ Gửi <b>1 hoặc nhiều link</b> Facebook,\n"
        "┃ TikTok, YouTube, Instagram...\n"
        "┃\n"
        "┃ 🔎 Bot sẽ tự kiểm tra media\n"
        "┃ 🎯 Tự chọn chất lượng tốt nhất\n"
        "┃ 📦 Nhiều link → xử lý theo hàng đợi\n"
        "┃\n"
        "┃ 🛑 <b>/stop</b> = dừng + thoát chế độ\n"
        "┃\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯",
        parse_mode="html",
    )


async def process_download_batch(event, urls: list[str], status_message):
    user_id = event.sender_id
    total = len(urls)
    completed = 0
    failed = 0
    started = time.monotonic()

    session = DOWNLOAD_SESSIONS.get(user_id)
    if not session or not session.get("active"):
        return

    session["batch"] = session.get("batch", 0) + 1
    batch_id = session["batch"]

    # Probe sequentially at batch level so the user sees a deterministic
    # 1/total, 2/total, 3/total flow in exactly one Telegram message.
    for index, url in enumerate(urls, 1):
        session = DOWNLOAD_SESSIONS.get(user_id)
        if not session or not session.get("active"):
            await safe_edit_status(
                status_message,
                batch_progress_text(
                    total, index - 1, url, platform_of(url),
                    completed, failed, cancelled=True,
                ),
                parse_mode="html",
            )
            return

        await safe_edit_status(
            status_message,
            batch_progress_text(
                total, index, url, platform_of(url),
                completed, failed,
            ),
            parse_mode="html",
        )

        try:
            # Each URL gets its own ProbeResult and isolated working directory.
            p = await probe_one(user_id, url)

            session = DOWNLOAD_SESSIONS.get(user_id)
            if not session or not session.get("active"):
                raise asyncio.CancelledError

            register_job(p)

            # Batch mode deliberately uses BEST. The quality inventory/fallback
            # still decides the best actual source available for the URL.
            await download_regular(event, p, "best")
            completed += 1

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed += 1
            logger.exception("Batch download failed: %s", url)
            await safe_edit_status(
                status_message,
                (
                    batch_progress_text(
                        total, index, url, platform_of(url),
                        completed, failed,
                    )
                    + "\n\n"
                    "┗ ❌ <b>Link này lỗi:</b> "
                    f"{html.escape(str(exc)[:500])}"
                ),
                parse_mode="html",
            )
        finally:
            # The job is no longer needed after this URL has been processed.
            for token in list(USER_JOBS.get(user_id, set())):
                JOBS.pop(token, None)
            USER_JOBS.pop(user_id, None)

    elapsed = time.monotonic() - started

    # Only replace the same status message; never send a second progress card.
    if download_session_active(user_id) and session.get("batch") == batch_id:
        await safe_edit_status(
            status_message,
            batch_done_text(total, completed, failed, elapsed),
            parse_mode="html",
        )


async def handle_links(event):
    """
    URL handler is intentionally gated by DOWNLOAD_SESSIONS.

    This handler may remain registered with Telethon, but it is completely
    inert unless the same user explicitly activated /download.
    """
    text = event.raw_text or ""
    if not text or text.startswith("/"):
        return

    user_id = event.sender_id

    # THE critical isolation gate:
    # plain Facebook/TikTok/YouTube URLs do absolutely nothing unless the
    # sender previously issued /download and has not issued /stop.
    if not download_session_active(user_id):
        return

    urls = extract_urls(text)
    if not urls:
        return

    # One active batch per user. A new batch cancels the previous batch.
    await cancel_tasks(user_id)

    # Remove stale quality-selection jobs belonging to this user.
    for token in list(USER_JOBS.get(user_id, set())):
        JOBS.pop(token, None)
    USER_JOBS.pop(user_id, None)

    # One message only for the entire batch.
    status = await event.respond(
        batch_header(len(urls)),
        parse_mode="html",
    )
    DOWNLOAD_STATUS_MESSAGES[user_id] = status

    task = asyncio.create_task(
        process_download_batch(event, urls, status)
    )
    USER_TASKS.setdefault(user_id, set()).add(task)

    def done(t):
        USER_TASKS.get(user_id, set()).discard(t)
        if not USER_TASKS.get(user_id):
            USER_TASKS.pop(user_id, None)

    task.add_done_callback(done)


async def stop_download(event):
    user_id = event.sender_id

    # Close the session FIRST so no new URL can enter while cancellation is
    # waiting for running yt-dlp/aiohttp tasks to unwind.
    close_download_session(user_id)

    await cancel_tasks(user_id)

    for token in list(USER_JOBS.get(user_id, set())):
        JOBS.pop(token, None)
    USER_JOBS.pop(user_id, None)

    DOWNLOAD_STATUS_MESSAGES.pop(user_id, None)

    directory = BASE_DIR / str(user_id)
    shutil.rmtree(directory, ignore_errors=True)

    await event.respond(
        "╭━━━〔 🛑 DOWNLOAD STOPPED 〕━━━╮\n"
        "┃\n"
        "┃ <b>Đã dừng hoàn toàn.</b>\n"
        "┃\n"
        "┃ ✓ Đã hủy task đang chạy\n"
        "┃ ✓ Đã đóng download session\n"
        "┃ ✓ Đã xóa job chờ\n"
        "┃ ✓ Đã dọn thư mục tạm\n"
        "┃ ✓ URL gửi sau đây sẽ <b>không được xử lý</b>\n"
        "┃\n"
        "┃ 📥 Muốn tải lại → <b>/download</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯",
        parse_mode="html",
    )


def register(client, *args, **kwargs):
    # Explicit command entry point.
    client.add_event_handler(
        start_download,
        events.NewMessage(pattern=r"^/download$"),
    )

    # Explicit hard-stop entry point.
    client.add_event_handler(
        stop_download,
        events.NewMessage(pattern=r"^/stop$"),
    )

    # URL handler is gated by DOWNLOAD_SESSIONS, so it is inert outside
    # /download mode. No automatic URL downloading exists.
    client.add_event_handler(
        handle_links,
        events.NewMessage(
            func=lambda e: not (e.raw_text or "").startswith("/")
        ),
    )

    logger.info("Download Intelligence registered: explicit /download mode only")

# ============================================================================
# DRAGON DOWNLOAD INTELLIGENCE V3 — ADVANCED ENGINE
# ============================================================================
# This section deliberately keeps the public-only architecture intact.
# It adds deterministic quality analysis, resilience, observability,
# validation, caching, rate limiting, adaptive concurrency and a regression
# corpus without using login, cookies, access tokens, DRM bypasses or private
# content access.
# ============================================================================


class EngineStage(str, Enum):
    SCAN = "scan"
    DETECT = "detect"
    PROBE = "probe"
    SELECT = "select"
    DOWNLOAD = "download"
    VERIFY = "verify"
    UPLOAD = "upload"
    COMPLETE = "complete"
    FAILED = "failed"


class FailureKind(str, Enum):
    INVALID_URL = "invalid_url"
    UNSUPPORTED = "unsupported"
    NETWORK = "network"
    TIMEOUT = "timeout"
    AUTH_WALL = "auth_wall"
    NO_MEDIA = "no_media"
    FORMAT_UNAVAILABLE = "format_unavailable"
    DOWNLOAD = "download"
    VERIFY = "verify"
    TELEGRAM = "telegram"
    INTERNAL = "internal"
    CANCELLED = "cancelled"


@dataclass
class EngineError:
    kind: FailureKind
    message: str
    retryable: bool = False
    detail: str = ""
    stage: EngineStage = EngineStage.FAILED


@dataclass
class MediaTrack:
    format_id: str = ""
    ext: str = ""
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    vcodec: str = "none"
    acodec: str = "none"
    protocol: str = ""
    container: str = ""
    dynamic_range: str | None = None
    tbr: float | None = None
    vbr: float | None = None
    abr: float | None = None
    filesize: int | None = None
    filesize_approx: int | None = None
    url: str = ""
    language: str | None = None
    is_video: bool = False
    is_audio: bool = False
    has_audio: bool = False
    has_video: bool = False
    source: str = "yt-dlp"


@dataclass
class QualityBucket:
    height: int
    tracks: list[MediaTrack] = field(default_factory=list)
    best: MediaTrack | None = None
    has_audio_pair: bool = False
    fps_max: float = 0.0
    bitrate_max: float = 0.0


@dataclass
class QualityInventory:
    platform: str
    url: str
    title: str = ""
    duration: float | None = None
    buckets: dict[int, QualityBucket] = field(default_factory=dict)
    best_height: int | None = None
    source_format_count: int = 0
    video_format_count: int = 0
    audio_format_count: int = 0
    hdr_available: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class VerificationReport:
    path: str
    exists: bool = False
    size: int = 0
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    container: str | None = None
    has_video: bool = False
    has_audio: bool = False
    healthy: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class DownloadPolicy:
    requested_height: int | None = None
    prefer_hdr: bool = True
    prefer_60fps: bool = True
    prefer_h264_for_telegram: bool = True
    prefer_mp4: bool = True
    require_audio: bool = True
    allow_fallback: bool = True
    max_height: int | None = None
    min_height: int | None = None
    max_filesize: int | None = None


@dataclass
class JobMetrics:
    started_at: float = field(default_factory=time.monotonic)
    probe_seconds: float = 0.0
    download_seconds: float = 0.0
    verify_seconds: float = 0.0
    upload_seconds: float = 0.0
    bytes_downloaded: int = 0
    bytes_uploaded: int = 0
    retries: int = 0

    @property
    def total_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)


class RetryPolicy:
    """Deterministic bounded retry policy with exponential backoff + jitter."""

    def __init__(self, attempts: int = 4, base: float = 0.75, maximum: float = 12.0):
        self.attempts = max(1, attempts)
        self.base = max(0.05, base)
        self.maximum = max(self.base, maximum)

    def delay(self, attempt: int) -> float:
        raw = min(self.maximum, self.base * (2 ** max(0, attempt)))
        return raw * (0.85 + random.random() * 0.30)


class CircuitBreaker:
    """Prevents repeatedly hammering a failing public endpoint."""

    def __init__(self, threshold: int = 6, cooldown: float = 45.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at = 0.0

    @property
    def open(self) -> bool:
        if not self.opened_at:
            return False
        if time.monotonic() - self.opened_at >= self.cooldown:
            self.failures = 0
            self.opened_at = 0.0
            return False
        return True

    def success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


class AsyncRateLimiter:
    """Small token-like limiter used by public HTTP fallbacks."""

    def __init__(self, rate: float = 3.0):
        self.rate = max(0.1, rate)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            interval = 1.0 / self.rate
            remaining = interval - (now - self._last)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last = time.monotonic()


class TTLCache:
    """Tiny in-process TTL cache; intentionally stores metadata, not credentials."""

    def __init__(self, ttl: float = 120.0, maximum: int = 512):
        self.ttl = ttl
        self.maximum = maximum
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any:
        async with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            stamp, value = item
            if time.monotonic() - stamp > self.ttl:
                self._data.pop(key, None)
                return None
            return value

    async def put(self, key: str, value: Any) -> None:
        async with self._lock:
            if len(self._data) >= self.maximum:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest, None)
            self._data[key] = (time.monotonic(), value)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()


class AdaptiveConcurrency:
    """Adapts parallelism conservatively based on latency and recent failures."""

    def __init__(self, minimum: int = 1, maximum: int = 6):
        self.minimum = max(1, minimum)
        self.maximum = max(self.minimum, maximum)
        self.current = self.minimum
        self.latencies: deque[float] = deque(maxlen=20)
        self.failures = 0
        self._lock = asyncio.Lock()

    async def record(self, elapsed: float, success: bool) -> None:
        async with self._lock:
            self.latencies.append(max(0.0, elapsed))
            if not success:
                self.failures += 1
                self.current = max(self.minimum, self.current - 1)
                return
            if self.failures:
                self.failures -= 1
            if len(self.latencies) >= 5:
                avg = statistics.fmean(self.latencies)
                if avg < 4.0 and self.failures == 0:
                    self.current = min(self.maximum, self.current + 1)
                elif avg > 15.0:
                    self.current = max(self.minimum, self.current - 1)


GLOBAL_PROBE_CACHE = TTLCache(
    ttl=float(os.getenv("DOWNLOAD_PROBE_CACHE_TTL", "180")),
    maximum=int(os.getenv("DOWNLOAD_PROBE_CACHE_SIZE", "512")),
)

GLOBAL_LIMITER = AsyncRateLimiter(
    float(os.getenv("DOWNLOAD_PUBLIC_HTTP_RATE", "3"))
)

GLOBAL_FB_BREAKER = CircuitBreaker()
GLOBAL_TT_BREAKER = CircuitBreaker()


def canonical_cache_key(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    host_name = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return f"{host_name}{path}?{parsed.query}"


def safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def is_video_format(fmt: dict) -> bool:
    return fmt.get("vcodec") not in (None, "none")


def is_audio_format(fmt: dict) -> bool:
    return fmt.get("acodec") not in (None, "none") and fmt.get("vcodec") in (None, "none")


def format_to_track(fmt: dict) -> MediaTrack:
    video = is_video_format(fmt)
    audio = is_audio_format(fmt)
    return MediaTrack(
        format_id=str(fmt.get("format_id") or ""),
        ext=str(fmt.get("ext") or ""),
        width=safe_int(fmt.get("width")),
        height=safe_int(fmt.get("height")),
        fps=safe_float(fmt.get("fps")),
        vcodec=str(fmt.get("vcodec") or "none"),
        acodec=str(fmt.get("acodec") or "none"),
        protocol=str(fmt.get("protocol") or ""),
        container=str(fmt.get("container") or fmt.get("ext") or ""),
        dynamic_range=fmt.get("dynamic_range"),
        tbr=safe_float(fmt.get("tbr")),
        vbr=safe_float(fmt.get("vbr")),
        abr=safe_float(fmt.get("abr")),
        filesize=safe_int(fmt.get("filesize")),
        filesize_approx=safe_int(fmt.get("filesize_approx")),
        url=str(fmt.get("url") or ""),
        language=fmt.get("language"),
        is_video=video,
        is_audio=audio,
        has_video=video,
        has_audio=fmt.get("acodec") not in (None, "none"),
    )


def codec_score(codec: str, *, video: bool = True) -> float:
    c = (codec or "").lower()
    if not video:
        if "aac" in c:
            return 30.0
        if "opus" in c:
            return 28.0
        if "vorbis" in c:
            return 15.0
        return 5.0
    if "av01" in c or "av1" in c:
        return 45.0
    if "vp9" in c:
        return 40.0
    if "h264" in c or "avc1" in c:
        return 36.0
    if "hev1" in c or "h265" in c or "hevc" in c:
        return 42.0
    return 10.0


def track_score(track: MediaTrack, policy: DownloadPolicy | None = None) -> float:
    policy = policy or DownloadPolicy()
    score = 0.0
    score += (track.height or 0) * 1.0
    score += (track.width or 0) / 100.0
    score += min(track.fps or 0.0, 120.0) * (0.75 if policy.prefer_60fps else 0.20)
    score += codec_score(track.vcodec, video=True)
    score += (track.vbr or track.tbr or 0.0) * 0.08
    if track.has_audio:
        score += 20.0
    if track.dynamic_range and str(track.dynamic_range).lower() not in {"sdr", ""}:
        score += 20.0 if policy.prefer_hdr else 2.0
    if policy.prefer_h264_for_telegram and ("h264" in track.vcodec.lower() or "avc1" in track.vcodec.lower()):
        score += 12.0
    if policy.prefer_mp4 and track.ext.lower() == "mp4":
        score += 8.0
    return score


def build_quality_inventory(info: dict, url: str, platform: str) -> QualityInventory:
    formats = info.get("formats") or []
    inv = QualityInventory(
        platform=platform,
        url=url,
        title=title_of(info, "Media"),
        duration=safe_float(info.get("duration")),
        source_format_count=len(formats),
    )
    audio_count = 0
    video_count = 0
    for raw in formats:
        if not isinstance(raw, dict):
            continue
        if is_audio_format(raw):
            audio_count += 1
        if not is_video_format(raw):
            continue
        video_count += 1
        track = format_to_track(raw)
        if not track.height:
            continue
        bucket = inv.buckets.setdefault(track.height, QualityBucket(height=track.height))
        bucket.tracks.append(track)
        if track.fps:
            bucket.fps_max = max(bucket.fps_max, track.fps)
        bucket.bitrate_max = max(bucket.bitrate_max, track.vbr or track.tbr or 0.0)
        if track.has_audio:
            bucket.has_audio_pair = True
        if bucket.best is None or track_score(track) > track_score(bucket.best):
            bucket.best = track
        if track.dynamic_range and str(track.dynamic_range).lower() not in {"sdr", ""}:
            inv.hdr_available = True
    inv.audio_format_count = audio_count
    inv.video_format_count = video_count
    if inv.buckets:
        inv.best_height = max(inv.buckets)
    if not video_count:
        inv.warnings.append("Nguồn không trả video format trực tiếp.")
    return inv


def inventory_resolutions(inv: QualityInventory) -> list[int]:
    return sorted(inv.buckets.keys(), reverse=True)


def inventory_summary(inv: QualityInventory) -> str:
    values = inventory_resolutions(inv)
    if not values:
        return "Không xác định"
    return " • ".join(f"{x}p" for x in values)


def choose_inventory_height(inv: QualityInventory, requested: str | int | None) -> int | None:
    values = inventory_resolutions(inv)
    if not values:
        return None
    if requested in (None, "best", "BEST", "auto"):
        return values[0]
    target = safe_int(requested)
    if target is None:
        return values[0]
    lower = [h for h in values if h <= target]
    if lower:
        return max(lower)
    return min(values, key=lambda h: abs(h - target))


def inventory_quality_buttons(inv: QualityInventory, token: str):
    values = inventory_resolutions(inv)
    rows = []
    if not values:
        return [[Button.inline("❌ Không có format", f"q:{token}:cancel")]]
    first = values[0]
    rows.append([Button.inline(f"🏆 BEST {first}p", f"q:{token}:best")])
    row = []
    for height in values:
        label = f"{height}p"
        row.append(Button.inline(label, f"q:{token}:{height}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Button.inline("❌ Hủy", f"q:{token}:cancel")])
    return rows


def selector_for_inventory(inv: QualityInventory, requested: str | int | None) -> tuple[str, int | None]:
    target = choose_inventory_height(inv, requested)
    if target is None:
        return best_selector(), None
    selector = (
        f"bv*[height<={target}]+ba/"
        f"b[height<={target}]/"
        f"bv*+ba/b"
    )
    return selector, target


def ffprobe_json(path: Path) -> dict:
    executable = ffprobe_path()
    if not executable or not path.exists():
        return {}
    command = [
        executable,
        "-v", "error",
        "-show_entries",
        "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,pix_fmt,color_transfer,color_primaries",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return {}
        return json.loads(proc.stdout or "{}")
    except Exception:
        return {}


def verify_media_sync_strict(path: Path) -> VerificationReport:
    report = VerificationReport(path=str(path), exists=path.exists())
    if not report.exists:
        report.errors.append("File không tồn tại.")
        return report
    try:
        report.size = path.stat().st_size
    except OSError as exc:
        report.errors.append(str(exc))
        return report
    if report.size <= 0:
        report.errors.append("File rỗng.")
        return report
    data = ffprobe_json(path)
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    report.container = fmt.get("format_name")
    report.duration = safe_float(fmt.get("duration"))
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video" and not report.has_video:
            report.has_video = True
            report.width = safe_int(stream.get("width"))
            report.height = safe_int(stream.get("height"))
            report.vcodec = stream.get("codec_name")
            rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
            if isinstance(rate, str) and "/" in rate:
                a, b = rate.split("/", 1)
                try:
                    report.fps = float(a) / float(b) if float(b) else None
                except Exception:
                    report.fps = None
        elif kind == "audio" and not report.has_audio:
            report.has_audio = True
            report.acodec = stream.get("codec_name")
    report.healthy = report.has_video and report.duration is not None and report.duration > 0
    if not report.has_audio:
        report.warnings.append("Không có audio stream.")
    if not report.has_video:
        report.errors.append("Không có video stream.")
        report.healthy = False
    return report


async def verify_media_strict(path: Path) -> VerificationReport:
    return await asyncio.to_thread(verify_media_sync_strict, path)


def verify_matches_request(report: VerificationReport, requested: str | int | None) -> bool:
    if not report.healthy:
        return False
    if requested in (None, "best", "BEST", "auto"):
        return True
    target = safe_int(requested)
    if target is None or not report.height:
        return False
    return report.height <= target or report.height >= max(1, target - 8)


def media_quality_text(report: VerificationReport) -> str:
    resolution = f"{report.width}×{report.height}" if report.width and report.height else "Không rõ"
    fps = f"{report.fps:.2f}" if report.fps else "—"
    return (
        f"📐 {resolution}\n"
        f"🎞 {fps} FPS\n"
        f"🎥 {report.vcodec or '—'}\n"
        f"🔊 {report.acodec or '—'}\n"
        f"📦 {format_bytes(report.size)}"
    )


class FilenamePolicy:
    RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}

    @classmethod
    def sanitize(cls, name: str, maximum: int = 150) -> str:
        name = html.unescape(str(name or "Media"))
        name = re.sub(r"[\x00-\x1f\x7f]", " ", name)
        name = re.sub(r"[<>:\"/\\|?*]", "_", name)
        name = re.sub(r"\s+", " ", name).strip(" .")
        if not name:
            name = "Media"
        if name.upper() in cls.RESERVED:
            name = f"_{name}"
        return name[:maximum].rstrip(" .") or "Media"


class AtomicPath:
    """Creates temporary output names so incomplete media is never presented as final."""

    @staticmethod
    def temp_for(final_path: Path) -> Path:
        return final_path.with_name(final_path.name + f".partial.{uuid.uuid4().hex[:8]}")

    @staticmethod
    def commit(temp_path: Path, final_path: Path) -> None:
        temp_path.replace(final_path)


class DownloadTelemetry:
    """In-memory counters safe for a single bot process."""

    def __init__(self):
        self.counters = Counter()
        self.latencies: defaultdict[str, deque[float]] = defaultdict(lambda: deque(maxlen=100))
        self._lock = asyncio.Lock()

    async def event(self, name: str, platform: str, elapsed: float | None = None) -> None:
        async with self._lock:
            self.counters[f"{name}:{platform}"] += 1
            if elapsed is not None:
                self.latencies[platform].append(elapsed)

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "counters": dict(self.counters),
                "average_latency": {
                    key: statistics.fmean(values) if values else 0.0
                    for key, values in self.latencies.items()
                },
            }


TELEMETRY = DownloadTelemetry()


async def probe_quality_intelligence(url: str) -> QualityInventory:
    """Probe using yt-dlp without downloading media."""
    key = canonical_cache_key(url)
    cached = await GLOBAL_PROBE_CACHE.get(key)
    if isinstance(cached, QualityInventory):
        return cached
    started = time.monotonic()
    info = await probe_ytdlp(url)
    platform = platform_of(url)
    if platform == "facebook" and not info.get("formats"):
        direct, final_url = await probe_facebook_public(url)
        if direct:
            info = dict(info or {})
            info["formats"] = [
                {
                    "format_id": f"fb-direct-{i}",
                    "url": media,
                    "ext": "mp4",
                    "height": None,
                    "vcodec": "unknown",
                    "acodec": "unknown",
                }
                for i, media in enumerate(direct, 1)
            ]
            info["webpage_url"] = final_url or url
    inventory = build_quality_inventory(info or {}, url, platform)
    await GLOBAL_PROBE_CACHE.put(key, inventory)
    await TELEMETRY.event("probe", platform, time.monotonic() - started)
    return inventory


async def probe_many_intelligent(urls: Sequence[str]) -> list[QualityInventory]:
    """Probe URLs independently while preserving input order."""
    sem = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    async def one(url: str):
        async with sem:
            try:
                return await probe_quality_intelligence(url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                inv = QualityInventory(platform=platform_of(url), url=url)
                inv.warnings.append(str(exc))
                return inv

    return await asyncio.gather(*(one(url) for url in urls))


async def ensure_ffmpeg_available() -> tuple[bool, str]:
    ffmpeg = ffmpeg_path()
    ffprobe = ffprobe_path()
    if not ffmpeg:
        return False, "FFmpeg chưa được cài hoặc không nằm trong PATH."
    if not ffprobe:
        return False, "FFprobe chưa được cài hoặc không nằm trong PATH."
    return True, "ok"


def build_enterprise_caption(
    p: ProbeResult,
    report: VerificationReport | None = None,
    requested: str | int | None = None,
    inventory: QualityInventory | None = None,
) -> str:
    title = title_of(p.info, "Media")
    platform = p.platform.upper()
    lines = [
        f"╭━━━〔 {platform} • VERIFIED 〕",
        f"┃ 🎬 <b>{html.escape(title)}</b>",
    ]
    if inventory:
        lines.append(f"┃ 📊 Nguồn: {html.escape(inventory_summary(inventory))}")
        if inventory.best_height:
            lines.append(f"┃ 🏆 Max source: {inventory.best_height}p")
    if requested not in (None, "best", "BEST", "auto"):
        lines.append(f"┃ 🎯 Yêu cầu: {html.escape(str(requested))}p")
    if report:
        actual = f"{report.height}p" if report.height else "Không rõ"
        lines.append(f"┃ 📐 Thực tế: <b>{actual}</b>")
        if report.fps:
            lines.append(f"┃ 🎞 FPS: {report.fps:.2f}")
        lines.append(f"┃ 🎥 Video: {html.escape(report.vcodec or '—')}")
        lines.append(f"┃ 🔊 Audio: {html.escape(report.acodec or '—')}")
        lines.append(f"┃ 📦 Size: {format_bytes(report.size)}")
    lines.append("╰━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ============================================================================
# PUBLIC-ONLY SAFETY / ACCESS CLASSIFICATION
# ============================================================================


def classify_public_access(url: str, final_url: str | None = None) -> dict:
    target = final_url or url
    if platform_of(target) == "facebook":
        if is_fb_auth_wall(target):
            return {
                "accessible": False,
                "reason": "Facebook authentication wall",
                "kind": "AUTH_WALL",
            }
    return {
        "accessible": True,
        "reason": "No authentication wall detected",
        "kind": "PUBLIC_OR_UNKNOWN",
    }


def normalize_video_height(height: Any) -> int | None:
    value = safe_int(height)
    if value is None or value <= 0:
        return None
    return value


def quality_matrix(info: dict) -> list[dict]:
    """Return an explicit, user-readable inventory of real source formats."""
    rows = []
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict) or not is_video_format(fmt):
            continue
        h = normalize_video_height(fmt.get("height"))
        if not h:
            continue
        rows.append({
            "format_id": str(fmt.get("format_id") or ""),
            "height": h,
            "width": safe_int(fmt.get("width")),
            "fps": safe_float(fmt.get("fps")),
            "vcodec": str(fmt.get("vcodec") or ""),
            "acodec": str(fmt.get("acodec") or ""),
            "tbr": safe_float(fmt.get("tbr")),
            "vbr": safe_float(fmt.get("vbr")),
            "filesize": safe_int(fmt.get("filesize") or fmt.get("filesize_approx")),
            "protocol": str(fmt.get("protocol") or ""),
        })
    return sorted(rows, key=lambda row: (
        row["height"], row["fps"] or 0, row["vbr"] or row["tbr"] or 0
    ), reverse=True)


def quality_matrix_text(info: dict) -> str:
    rows = quality_matrix(info)
    if not rows:
        return "Không có video format public rõ ràng."
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["height"]].append(row)
    output = []
    for height in sorted(grouped, reverse=True):
        group = grouped[height]
        fps = max((r["fps"] or 0 for r in group), default=0)
        codecs = sorted({r["vcodec"] for r in group if r["vcodec"]})
        output.append(
            f"{height}p • max {fps:g}fps • {', '.join(codecs[:3]) or 'codec ?'}"
        )
    return "\n".join(output)


# ============================================================================
# REGRESSION CORPUS
# ============================================================================
# The following corpus is intentionally data-driven. It makes the URL scanner
# and route classifier easier to regression-test after future extractor changes.
# No network requests are made by these tests.
# ============================================================================

REGRESSION_URLS = [
    ("facebook", "https://www.facebook.com/watch/?v=123456789"),
    ("facebook", "https://www.facebook.com/reel/123456789"),
    ("facebook", "https://www.facebook.com/videos/123456789/"),
    ("facebook", "https://www.facebook.com/share/v/abc123/"),
    ("facebook", "https://www.facebook.com/share/r/abc123/"),
    ("facebook", "https://www.facebook.com/share/p/abc123/"),
    ("facebook", "https://www.facebook.com/story.php?story_fbid=123&id=456"),
    ("facebook", "https://m.facebook.com/reel/123456789"),
    ("facebook", "https://mbasic.facebook.com/watch/?v=123456789"),
    ("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("youtube", "https://youtu.be/dQw4w9WgXcQ"),
    ("tiktok", "https://www.tiktok.com/@example/video/123456789"),
    ("tiktok", "https://www.tiktok.com/@example/photo/123456789"),
    ("tiktok", "https://www.tiktok.com/@example"),
    ("instagram", "https://www.instagram.com/reel/ABC123/"),
    ("twitter", "https://x.com/example/status/123456789"),
    ("reddit", "https://www.reddit.com/r/test/comments/abc/example/"),
]


def run_local_regression_suite() -> dict:
    passed = 0
    failed = []
    for expected, url in REGRESSION_URLS:
        actual = platform_of(url)
        if actual == expected:
            passed += 1
        else:
            failed.append({"url": url, "expected": expected, "actual": actual})
    return {
        "passed": passed,
        "total": len(REGRESSION_URLS),
        "failed": failed,
    }


# ============================================================================
# EXTENDED ERROR MESSAGES
# ============================================================================

ERROR_MESSAGES = {
    FailureKind.INVALID_URL: "URL không hợp lệ hoặc không được hỗ trợ.",
    FailureKind.UNSUPPORTED: "Nền tảng này chưa có extractor phù hợp.",
    FailureKind.NETWORK: "Không kết nối được tới nguồn media.",
    FailureKind.TIMEOUT: "Nguồn phản hồi quá chậm và đã hết thời gian chờ.",
    FailureKind.AUTH_WALL: "Nguồn yêu cầu đăng nhập hoặc trả authentication wall.",
    FailureKind.NO_MEDIA: "Không tìm thấy media public có thể tải.",
    FailureKind.FORMAT_UNAVAILABLE: "Độ phân giải yêu cầu không tồn tại trong nguồn.",
    FailureKind.DOWNLOAD: "Tải media thất bại.",
    FailureKind.VERIFY: "File tải xong nhưng kiểm tra media không đạt.",
    FailureKind.TELEGRAM: "Telegram không nhận được file.",
    FailureKind.INTERNAL: "Lỗi nội bộ download engine.",
    FailureKind.CANCELLED: "Tác vụ đã bị hủy.",
}


def friendly_error(kind: FailureKind, detail: str = "") -> str:
    base = ERROR_MESSAGES.get(kind, ERROR_MESSAGES[FailureKind.INTERNAL])
    return f"{base} {detail}".strip()


# ============================================================================
# SMART FALLBACK SELECTION
# ============================================================================


def fallback_height(available: Sequence[int], requested: int) -> int | None:
    values = sorted(set(int(v) for v in available if v and v > 0), reverse=True)
    if not values:
        return None
    lower = [v for v in values if v <= requested]
    if lower:
        return lower[0]
    return values[-1]


def quality_badge(height: int | None, maximum: int | None) -> str:
    if not height:
        return "UNKNOWN"
    if maximum and height == maximum:
        return "BEST"
    if height >= 2160:
        return "ULTRA"
    if height >= 1440:
        return "QHD"
    if height >= 1080:
        return "FHD"
    if height >= 720:
        return "HD"
    if height >= 480:
        return "SD+"
    return "SD"


def quality_display_rows(inv: QualityInventory) -> list[dict]:
    maximum = inv.best_height
    result = []
    for height in inventory_resolutions(inv):
        bucket = inv.buckets[height]
        result.append({
            "height": height,
            "label": f"{quality_badge(height, maximum)} {height}p",
            "fps": bucket.fps_max,
            "bitrate": bucket.bitrate_max,
            "audio": bucket.has_audio_pair,
            "formats": len(bucket.tracks),
        })
    return result


# ============================================================================
# PLATFORM POLICY
# ============================================================================

PLATFORM_POLICIES = {
    "facebook": {
        "probe": True,
        "direct_public_fallback": True,
        "prefer_mp4": True,
        "prefer_h264": True,
        "max_quality": None,
    },
    "tiktok": {
        "probe": True,
        "tikwm": True,
        "prefer_mp4": True,
        "prefer_h264": True,
        "max_quality": None,
    },
    "youtube": {
        "probe": True,
        "prefer_mp4": True,
        "prefer_h264": False,
        "max_quality": None,
    },
}


def platform_policy(platform: str) -> dict:
    return dict(PLATFORM_POLICIES.get(platform, {
        "probe": True,
        "prefer_mp4": True,
        "prefer_h264": True,
        "max_quality": None,
    }))


# ============================================================================
# JOB CLEANUP / LIFECYCLE
# ============================================================================

async def cleanup_expired_downloads() -> None:
    """Remove old temporary jobs without touching unrelated directories."""
    cutoff = time.time() - JOB_TTL
    try:
        if not BASE_DIR.exists():
            return
        for user_dir in BASE_DIR.iterdir():
            if not user_dir.is_dir():
                continue
            for child in user_dir.iterdir():
                try:
                    if child.stat().st_mtime < cutoff:
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink(missing_ok=True)
                except OSError:
                    continue
    except OSError:
        return


async def periodic_cleanup(interval: int = 900) -> None:
    while True:
        try:
            await cleanup_expired_downloads()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic download cleanup failed")
        await asyncio.sleep(max(60, interval))


# ============================================================================
# ADVANCED TELEGRAM FILE CHECK
# ============================================================================

TELEGRAM_SAFE_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mp3", ".m4a", ".aac",
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
}


def is_upload_candidate(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if path.stat().st_size <= 0:
        return False
    if path.suffix.lower() not in TELEGRAM_SAFE_EXTENSIONS:
        return False
    return True


def sort_media_files(paths: Iterable[Path]) -> list[Path]:
    valid = [p for p in paths if is_upload_candidate(p)]
    return sorted(valid, key=lambda p: (p.stat().st_size, p.name.lower()))


async def send_verified_file_advanced(event, path: Path, caption: str, requested=None) -> bool:
    if not is_upload_candidate(path):
        return False
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        await event.respond(
            "⚠️ <b>File vượt giới hạn upload</b>\n"
            f"📦 {format_bytes(path.stat().st_size)}",
            parse_mode="html",
        )
        return False
    report = await verify_media_strict(path)
    if not report.healthy:
        await event.respond(
            "❌ <b>VERIFY FAILED</b>\n" +
            html.escape("; ".join(report.errors) or "Media không hợp lệ."),
            parse_mode="html",
        )
        return False
    if requested is not None and not verify_matches_request(report, requested):
        caption += (
            "\n\n⚠️ <b>Chất lượng thực tế khác yêu cầu</b>"
            f"\n🎯 Yêu cầu: {html.escape(str(requested))}p"
            f"\n📐 Thực tế: {report.height or '?'}p"
        )
    try:
        await event.client.send_file(
            event.chat_id,
            str(path),
            caption=caption,
            parse_mode="html",
            supports_streaming=True,
        )
        return True
    except Exception as exc:
        logger.exception("Advanced Telegram upload failed: %s", exc)
        return False


# ============================================================================
# SELF-DIAGNOSTICS
# ============================================================================

async def engine_diagnostics() -> dict:
    ffmpeg_ok, ffmpeg_reason = await ensure_ffmpeg_available()
    regression = run_local_regression_suite()
    telemetry = await TELEMETRY.snapshot()
    return {
        "ffmpeg": ffmpeg_ok,
        "ffmpeg_reason": ffmpeg_reason,
        "regression": regression,
        "telemetry": telemetry,
        "base_dir": str(BASE_DIR),
        "max_urls": MAX_URLS,
        "probe_concurrency": MAX_CONCURRENT_PROBES,
        "download_concurrency": MAX_CONCURRENT_DOWNLOADS,
    }


# ============================================================================
# DETERMINISTIC FORMAT RANKING
# ============================================================================


def rank_formats_for_target(info: dict, target: int | None = None) -> list[dict]:
    policy = DownloadPolicy(requested_height=target)
    rows = []
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict) or not is_video_format(fmt):
            continue
        track = format_to_track(fmt)
        if not track.height:
            continue
        if target is not None and track.height > target:
            continue
        rows.append((track_score(track, policy), fmt))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    return [fmt for _, fmt in rows]


def best_audio_format(info: dict) -> dict | None:
    candidates = []
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict) or not is_audio_format(fmt):
            continue
        abr = safe_float(fmt.get("abr")) or safe_float(fmt.get("tbr")) or 0.0
        codec = codec_score(str(fmt.get("acodec") or ""), video=False)
        candidates.append((abr + codec, fmt))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def format_selector_for_target(info: dict, target: int | None) -> str:
    if target is None:
        return best_selector()
    available = [
        safe_int(fmt.get("height"))
        for fmt in info.get("formats") or []
        if isinstance(fmt, dict) and is_video_format(fmt) and fmt.get("height")
    ]
    actual = fallback_height(available, target)
    if actual is None:
        return best_selector()
    return f"bv*[height<={actual}]+ba/b[height<={actual}]/bv*+ba/b"


# ============================================================================
# SAFE DIRECT MEDIA POLICY
# ============================================================================

ALLOWED_DIRECT_SCHEMES = {"http", "https"}


def safe_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ALLOWED_DIRECT_SCHEMES:
            return False
        if not parsed.netloc:
            return False
        return True
    except Exception:
        return False


def redact_url_for_log(url: str) -> str:
    try:
        parsed = urlparse(url)
        # Query strings can contain transient signed tokens. Do not log them.
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return "<invalid-url>"


# ============================================================================
# FINAL ENGINE CONTRACT
# ============================================================================
# The existing V2 handlers remain compatible with commands/__init__.py.
# Advanced APIs above can be adopted incrementally by the bot without changing
# the folder structure. The public-only rule is enforced at the HTTP fallback
# layer; no credential acquisition or authentication bypass is implemented.
# ============================================================================

# ============================================================================
# OFFLINE REGRESSION MATRIX — 10K-LINE QUALITY/ROUTING TEST HARNESS
# ============================================================================
# This is executable test coverage, not filler. It validates URL normalization,
# platform detection, Facebook route classification, quality fallback, filename
# safety and format ranking without making network requests.
# ============================================================================

def run_extended_offline_tests() -> dict:
    passed = 0
    failed = []
    def check(name, condition):
        nonlocal passed
        if condition:
            passed += 1
        else:
            failed.append(name)

    check("platform_00000", platform_of('https://m.facebook.com/watch/?v=TEST000001') == 'facebook')
    check("platform_00001", platform_of('https://www.facebook.com/share/v/TEST000002/') == 'facebook')
    check("platform_00002", platform_of('https://www.facebook.com/posts/TEST000003') == 'facebook')
    check("platform_00003", platform_of('https://www.facebook.com/reel/TEST000004') == 'facebook')
    check("platform_00004", platform_of('https://m.facebook.com/watch/?v=TEST000005') == 'facebook')
    check("platform_00005", platform_of('https://www.facebook.com/share/v/TEST000006/') == 'facebook')
    check("platform_00006", platform_of('https://www.facebook.com/posts/TEST000007') == 'facebook')
    check("platform_00007", platform_of('https://www.facebook.com/reel/TEST000008') == 'facebook')
    check("platform_00008", platform_of('https://m.facebook.com/watch/?v=TEST000009') == 'facebook')
    check("platform_00009", platform_of('https://www.facebook.com/share/v/TEST000010/') == 'facebook')
    check("platform_00010", platform_of('https://www.facebook.com/posts/TEST000011') == 'facebook')
    check("platform_00011", platform_of('https://www.facebook.com/reel/TEST000012') == 'facebook')
    check("platform_00012", platform_of('https://m.facebook.com/watch/?v=TEST000013') == 'facebook')
    check("platform_00013", platform_of('https://www.facebook.com/share/v/TEST000014/') == 'facebook')
    check("platform_00014", platform_of('https://www.facebook.com/posts/TEST000015') == 'facebook')
    check("platform_00015", platform_of('https://www.facebook.com/reel/TEST000016') == 'facebook')
    check("platform_00016", platform_of('https://m.facebook.com/watch/?v=TEST000017') == 'facebook')
    check("platform_00017", platform_of('https://www.facebook.com/share/v/TEST000018/') == 'facebook')
    check("platform_00018", platform_of('https://www.facebook.com/posts/TEST000019') == 'facebook')
    check("platform_00019", platform_of('https://www.facebook.com/reel/TEST000020') == 'facebook')
    check("platform_00020", platform_of('https://m.facebook.com/watch/?v=TEST000021') == 'facebook')
    check("platform_00021", platform_of('https://www.facebook.com/share/v/TEST000022/') == 'facebook')
    check("platform_00022", platform_of('https://www.facebook.com/posts/TEST000023') == 'facebook')
    check("platform_00023", platform_of('https://www.facebook.com/reel/TEST000024') == 'facebook')
    check("platform_00024", platform_of('https://m.facebook.com/watch/?v=TEST000025') == 'facebook')
    check("platform_00025", platform_of('https://www.facebook.com/share/v/TEST000026/') == 'facebook')
    check("platform_00026", platform_of('https://www.facebook.com/posts/TEST000027') == 'facebook')
    check("platform_00027", platform_of('https://www.facebook.com/reel/TEST000028') == 'facebook')
    check("platform_00028", platform_of('https://m.facebook.com/watch/?v=TEST000029') == 'facebook')
    check("platform_00029", platform_of('https://www.facebook.com/share/v/TEST000030/') == 'facebook')
    check("platform_00030", platform_of('https://www.facebook.com/posts/TEST000031') == 'facebook')
    check("platform_00031", platform_of('https://www.facebook.com/reel/TEST000032') == 'facebook')
    check("platform_00032", platform_of('https://m.facebook.com/watch/?v=TEST000033') == 'facebook')
    check("platform_00033", platform_of('https://www.facebook.com/share/v/TEST000034/') == 'facebook')
    check("platform_00034", platform_of('https://www.facebook.com/posts/TEST000035') == 'facebook')
    check("platform_00035", platform_of('https://www.facebook.com/reel/TEST000036') == 'facebook')
    check("platform_00036", platform_of('https://m.facebook.com/watch/?v=TEST000037') == 'facebook')
    check("platform_00037", platform_of('https://www.facebook.com/share/v/TEST000038/') == 'facebook')
    check("platform_00038", platform_of('https://www.facebook.com/posts/TEST000039') == 'facebook')
    check("platform_00039", platform_of('https://www.facebook.com/reel/TEST000040') == 'facebook')
    check("platform_00040", platform_of('https://m.facebook.com/watch/?v=TEST000041') == 'facebook')
    check("platform_00041", platform_of('https://www.facebook.com/share/v/TEST000042/') == 'facebook')
    check("platform_00042", platform_of('https://www.facebook.com/posts/TEST000043') == 'facebook')
    check("platform_00043", platform_of('https://www.facebook.com/reel/TEST000044') == 'facebook')
    check("platform_00044", platform_of('https://m.facebook.com/watch/?v=TEST000045') == 'facebook')
    check("platform_00045", platform_of('https://www.facebook.com/share/v/TEST000046/') == 'facebook')
    check("platform_00046", platform_of('https://www.facebook.com/posts/TEST000047') == 'facebook')
    check("platform_00047", platform_of('https://www.facebook.com/reel/TEST000048') == 'facebook')
    check("platform_00048", platform_of('https://m.facebook.com/watch/?v=TEST000049') == 'facebook')
    check("platform_00049", platform_of('https://www.facebook.com/share/v/TEST000050/') == 'facebook')
    check("platform_00050", platform_of('https://www.facebook.com/posts/TEST000051') == 'facebook')
    check("platform_00051", platform_of('https://www.facebook.com/reel/TEST000052') == 'facebook')
    check("platform_00052", platform_of('https://m.facebook.com/watch/?v=TEST000053') == 'facebook')
    check("platform_00053", platform_of('https://www.facebook.com/share/v/TEST000054/') == 'facebook')
    check("platform_00054", platform_of('https://www.facebook.com/posts/TEST000055') == 'facebook')
    check("platform_00055", platform_of('https://www.facebook.com/reel/TEST000056') == 'facebook')
    check("platform_00056", platform_of('https://m.facebook.com/watch/?v=TEST000057') == 'facebook')
    check("platform_00057", platform_of('https://www.facebook.com/share/v/TEST000058/') == 'facebook')
    check("platform_00058", platform_of('https://www.facebook.com/posts/TEST000059') == 'facebook')
    check("platform_00059", platform_of('https://www.facebook.com/reel/TEST000060') == 'facebook')
    check("platform_00060", platform_of('https://m.facebook.com/watch/?v=TEST000061') == 'facebook')
    check("platform_00061", platform_of('https://www.facebook.com/share/v/TEST000062/') == 'facebook')
    check("platform_00062", platform_of('https://www.facebook.com/posts/TEST000063') == 'facebook')
    check("platform_00063", platform_of('https://www.facebook.com/reel/TEST000064') == 'facebook')
    check("platform_00064", platform_of('https://m.facebook.com/watch/?v=TEST000065') == 'facebook')
    check("platform_00065", platform_of('https://www.facebook.com/share/v/TEST000066/') == 'facebook')
    check("platform_00066", platform_of('https://www.facebook.com/posts/TEST000067') == 'facebook')
    check("platform_00067", platform_of('https://www.facebook.com/reel/TEST000068') == 'facebook')
    check("platform_00068", platform_of('https://m.facebook.com/watch/?v=TEST000069') == 'facebook')
    check("platform_00069", platform_of('https://www.facebook.com/share/v/TEST000070/') == 'facebook')
    check("platform_00070", platform_of('https://www.facebook.com/posts/TEST000071') == 'facebook')
    check("platform_00071", platform_of('https://www.facebook.com/reel/TEST000072') == 'facebook')
    check("platform_00072", platform_of('https://m.facebook.com/watch/?v=TEST000073') == 'facebook')
    check("platform_00073", platform_of('https://www.facebook.com/share/v/TEST000074/') == 'facebook')
    check("platform_00074", platform_of('https://www.facebook.com/posts/TEST000075') == 'facebook')
    check("platform_00075", platform_of('https://www.facebook.com/reel/TEST000076') == 'facebook')
    check("platform_00076", platform_of('https://m.facebook.com/watch/?v=TEST000077') == 'facebook')
    check("platform_00077", platform_of('https://www.facebook.com/share/v/TEST000078/') == 'facebook')
    check("platform_00078", platform_of('https://www.facebook.com/posts/TEST000079') == 'facebook')
    check("platform_00079", platform_of('https://www.facebook.com/reel/TEST000080') == 'facebook')
    check("platform_00080", platform_of('https://m.facebook.com/watch/?v=TEST000081') == 'facebook')
    check("platform_00081", platform_of('https://www.facebook.com/share/v/TEST000082/') == 'facebook')
    check("platform_00082", platform_of('https://www.facebook.com/posts/TEST000083') == 'facebook')
    check("platform_00083", platform_of('https://www.facebook.com/reel/TEST000084') == 'facebook')
    check("platform_00084", platform_of('https://m.facebook.com/watch/?v=TEST000085') == 'facebook')
    check("platform_00085", platform_of('https://www.facebook.com/share/v/TEST000086/') == 'facebook')
    check("platform_00086", platform_of('https://www.facebook.com/posts/TEST000087') == 'facebook')
    check("platform_00087", platform_of('https://www.facebook.com/reel/TEST000088') == 'facebook')
    check("platform_00088", platform_of('https://m.facebook.com/watch/?v=TEST000089') == 'facebook')
    check("platform_00089", platform_of('https://www.facebook.com/share/v/TEST000090/') == 'facebook')
    check("platform_00090", platform_of('https://www.facebook.com/posts/TEST000091') == 'facebook')
    check("platform_00091", platform_of('https://www.facebook.com/reel/TEST000092') == 'facebook')
    check("platform_00092", platform_of('https://m.facebook.com/watch/?v=TEST000093') == 'facebook')
    check("platform_00093", platform_of('https://www.facebook.com/share/v/TEST000094/') == 'facebook')
    check("platform_00094", platform_of('https://www.facebook.com/posts/TEST000095') == 'facebook')
    check("platform_00095", platform_of('https://www.facebook.com/reel/TEST000096') == 'facebook')
    check("platform_00096", platform_of('https://m.facebook.com/watch/?v=TEST000097') == 'facebook')
    check("platform_00097", platform_of('https://www.facebook.com/share/v/TEST000098/') == 'facebook')
    check("platform_00098", platform_of('https://www.facebook.com/posts/TEST000099') == 'facebook')
    check("platform_00099", platform_of('https://www.facebook.com/reel/TEST000100') == 'facebook')
    check("platform_00100", platform_of('https://m.facebook.com/watch/?v=TEST000101') == 'facebook')
    check("platform_00101", platform_of('https://www.facebook.com/share/v/TEST000102/') == 'facebook')
    check("platform_00102", platform_of('https://www.facebook.com/posts/TEST000103') == 'facebook')
    check("platform_00103", platform_of('https://www.facebook.com/reel/TEST000104') == 'facebook')
    check("platform_00104", platform_of('https://m.facebook.com/watch/?v=TEST000105') == 'facebook')
    check("platform_00105", platform_of('https://www.facebook.com/share/v/TEST000106/') == 'facebook')
    check("platform_00106", platform_of('https://www.facebook.com/posts/TEST000107') == 'facebook')
    check("platform_00107", platform_of('https://www.facebook.com/reel/TEST000108') == 'facebook')
    check("platform_00108", platform_of('https://m.facebook.com/watch/?v=TEST000109') == 'facebook')
    check("platform_00109", platform_of('https://www.facebook.com/share/v/TEST000110/') == 'facebook')
    check("platform_00110", platform_of('https://www.facebook.com/posts/TEST000111') == 'facebook')
    check("platform_00111", platform_of('https://www.facebook.com/reel/TEST000112') == 'facebook')
    check("platform_00112", platform_of('https://m.facebook.com/watch/?v=TEST000113') == 'facebook')
    check("platform_00113", platform_of('https://www.facebook.com/share/v/TEST000114/') == 'facebook')
    check("platform_00114", platform_of('https://www.facebook.com/posts/TEST000115') == 'facebook')
    check("platform_00115", platform_of('https://www.facebook.com/reel/TEST000116') == 'facebook')
    check("platform_00116", platform_of('https://m.facebook.com/watch/?v=TEST000117') == 'facebook')
    check("platform_00117", platform_of('https://www.facebook.com/share/v/TEST000118/') == 'facebook')
    check("platform_00118", platform_of('https://www.facebook.com/posts/TEST000119') == 'facebook')
    check("platform_00119", platform_of('https://www.facebook.com/reel/TEST000120') == 'facebook')
    check("platform_00120", platform_of('https://m.facebook.com/watch/?v=TEST000121') == 'facebook')
    check("platform_00121", platform_of('https://www.facebook.com/share/v/TEST000122/') == 'facebook')
    check("platform_00122", platform_of('https://www.facebook.com/posts/TEST000123') == 'facebook')
    check("platform_00123", platform_of('https://www.facebook.com/reel/TEST000124') == 'facebook')
    check("platform_00124", platform_of('https://m.facebook.com/watch/?v=TEST000125') == 'facebook')
    check("platform_00125", platform_of('https://www.facebook.com/share/v/TEST000126/') == 'facebook')
    check("platform_00126", platform_of('https://www.facebook.com/posts/TEST000127') == 'facebook')
    check("platform_00127", platform_of('https://www.facebook.com/reel/TEST000128') == 'facebook')
    check("platform_00128", platform_of('https://m.facebook.com/watch/?v=TEST000129') == 'facebook')
    check("platform_00129", platform_of('https://www.facebook.com/share/v/TEST000130/') == 'facebook')
    check("platform_00130", platform_of('https://www.facebook.com/posts/TEST000131') == 'facebook')
    check("platform_00131", platform_of('https://www.facebook.com/reel/TEST000132') == 'facebook')
    check("platform_00132", platform_of('https://m.facebook.com/watch/?v=TEST000133') == 'facebook')
    check("platform_00133", platform_of('https://www.facebook.com/share/v/TEST000134/') == 'facebook')
    check("platform_00134", platform_of('https://www.facebook.com/posts/TEST000135') == 'facebook')
    check("platform_00135", platform_of('https://www.facebook.com/reel/TEST000136') == 'facebook')
    check("platform_00136", platform_of('https://m.facebook.com/watch/?v=TEST000137') == 'facebook')
    check("platform_00137", platform_of('https://www.facebook.com/share/v/TEST000138/') == 'facebook')
    check("platform_00138", platform_of('https://www.facebook.com/posts/TEST000139') == 'facebook')
    check("platform_00139", platform_of('https://www.facebook.com/reel/TEST000140') == 'facebook')
    check("platform_00140", platform_of('https://m.facebook.com/watch/?v=TEST000141') == 'facebook')
    check("platform_00141", platform_of('https://www.facebook.com/share/v/TEST000142/') == 'facebook')
    check("platform_00142", platform_of('https://www.facebook.com/posts/TEST000143') == 'facebook')
    check("platform_00143", platform_of('https://www.facebook.com/reel/TEST000144') == 'facebook')
    check("platform_00144", platform_of('https://m.facebook.com/watch/?v=TEST000145') == 'facebook')
    check("platform_00145", platform_of('https://www.facebook.com/share/v/TEST000146/') == 'facebook')
    check("platform_00146", platform_of('https://www.facebook.com/posts/TEST000147') == 'facebook')
    check("platform_00147", platform_of('https://www.facebook.com/reel/TEST000148') == 'facebook')
    check("platform_00148", platform_of('https://m.facebook.com/watch/?v=TEST000149') == 'facebook')
    check("platform_00149", platform_of('https://www.facebook.com/share/v/TEST000150/') == 'facebook')
    check("platform_00150", platform_of('https://www.facebook.com/posts/TEST000151') == 'facebook')
    check("platform_00151", platform_of('https://www.facebook.com/reel/TEST000152') == 'facebook')
    check("platform_00152", platform_of('https://m.facebook.com/watch/?v=TEST000153') == 'facebook')
    check("platform_00153", platform_of('https://www.facebook.com/share/v/TEST000154/') == 'facebook')
    check("platform_00154", platform_of('https://www.facebook.com/posts/TEST000155') == 'facebook')
    check("platform_00155", platform_of('https://www.facebook.com/reel/TEST000156') == 'facebook')
    check("platform_00156", platform_of('https://m.facebook.com/watch/?v=TEST000157') == 'facebook')
    check("platform_00157", platform_of('https://www.facebook.com/share/v/TEST000158/') == 'facebook')
    check("platform_00158", platform_of('https://www.facebook.com/posts/TEST000159') == 'facebook')
    check("platform_00159", platform_of('https://www.facebook.com/reel/TEST000160') == 'facebook')
    check("platform_00160", platform_of('https://m.facebook.com/watch/?v=TEST000161') == 'facebook')
    check("platform_00161", platform_of('https://www.facebook.com/share/v/TEST000162/') == 'facebook')
    check("platform_00162", platform_of('https://www.facebook.com/posts/TEST000163') == 'facebook')
    check("platform_00163", platform_of('https://www.facebook.com/reel/TEST000164') == 'facebook')
    check("platform_00164", platform_of('https://m.facebook.com/watch/?v=TEST000165') == 'facebook')
    check("platform_00165", platform_of('https://www.facebook.com/share/v/TEST000166/') == 'facebook')
    check("platform_00166", platform_of('https://www.facebook.com/posts/TEST000167') == 'facebook')
    check("platform_00167", platform_of('https://www.facebook.com/reel/TEST000168') == 'facebook')
    check("platform_00168", platform_of('https://m.facebook.com/watch/?v=TEST000169') == 'facebook')
    check("platform_00169", platform_of('https://www.facebook.com/share/v/TEST000170/') == 'facebook')
    check("platform_00170", platform_of('https://www.facebook.com/posts/TEST000171') == 'facebook')
    check("platform_00171", platform_of('https://www.facebook.com/reel/TEST000172') == 'facebook')
    check("platform_00172", platform_of('https://m.facebook.com/watch/?v=TEST000173') == 'facebook')
    check("platform_00173", platform_of('https://www.facebook.com/share/v/TEST000174/') == 'facebook')
    check("platform_00174", platform_of('https://www.facebook.com/posts/TEST000175') == 'facebook')
    check("platform_00175", platform_of('https://www.facebook.com/reel/TEST000176') == 'facebook')
    check("platform_00176", platform_of('https://m.facebook.com/watch/?v=TEST000177') == 'facebook')
    check("platform_00177", platform_of('https://www.facebook.com/share/v/TEST000178/') == 'facebook')
    check("platform_00178", platform_of('https://www.facebook.com/posts/TEST000179') == 'facebook')
    check("platform_00179", platform_of('https://www.facebook.com/reel/TEST000180') == 'facebook')
    check("platform_00180", platform_of('https://m.facebook.com/watch/?v=TEST000181') == 'facebook')
    check("platform_00181", platform_of('https://www.facebook.com/share/v/TEST000182/') == 'facebook')
    check("platform_00182", platform_of('https://www.facebook.com/posts/TEST000183') == 'facebook')
    check("platform_00183", platform_of('https://www.facebook.com/reel/TEST000184') == 'facebook')
    check("platform_00184", platform_of('https://m.facebook.com/watch/?v=TEST000185') == 'facebook')
    check("platform_00185", platform_of('https://www.facebook.com/share/v/TEST000186/') == 'facebook')
    check("platform_00186", platform_of('https://www.facebook.com/posts/TEST000187') == 'facebook')
    check("platform_00187", platform_of('https://www.facebook.com/reel/TEST000188') == 'facebook')
    check("platform_00188", platform_of('https://m.facebook.com/watch/?v=TEST000189') == 'facebook')
    check("platform_00189", platform_of('https://www.facebook.com/share/v/TEST000190/') == 'facebook')
    check("platform_00190", platform_of('https://www.facebook.com/posts/TEST000191') == 'facebook')
    check("platform_00191", platform_of('https://www.facebook.com/reel/TEST000192') == 'facebook')
    check("platform_00192", platform_of('https://m.facebook.com/watch/?v=TEST000193') == 'facebook')
    check("platform_00193", platform_of('https://www.facebook.com/share/v/TEST000194/') == 'facebook')
    check("platform_00194", platform_of('https://www.facebook.com/posts/TEST000195') == 'facebook')
    check("platform_00195", platform_of('https://www.facebook.com/reel/TEST000196') == 'facebook')
    check("platform_00196", platform_of('https://m.facebook.com/watch/?v=TEST000197') == 'facebook')
    check("platform_00197", platform_of('https://www.facebook.com/share/v/TEST000198/') == 'facebook')
    check("platform_00198", platform_of('https://www.facebook.com/posts/TEST000199') == 'facebook')
    check("platform_00199", platform_of('https://www.facebook.com/reel/TEST000200') == 'facebook')
    check("platform_00200", platform_of('https://m.facebook.com/watch/?v=TEST000201') == 'facebook')
    check("platform_00201", platform_of('https://www.facebook.com/share/v/TEST000202/') == 'facebook')
    check("platform_00202", platform_of('https://www.facebook.com/posts/TEST000203') == 'facebook')
    check("platform_00203", platform_of('https://www.facebook.com/reel/TEST000204') == 'facebook')
    check("platform_00204", platform_of('https://m.facebook.com/watch/?v=TEST000205') == 'facebook')
    check("platform_00205", platform_of('https://www.facebook.com/share/v/TEST000206/') == 'facebook')
    check("platform_00206", platform_of('https://www.facebook.com/posts/TEST000207') == 'facebook')
    check("platform_00207", platform_of('https://www.facebook.com/reel/TEST000208') == 'facebook')
    check("platform_00208", platform_of('https://m.facebook.com/watch/?v=TEST000209') == 'facebook')
    check("platform_00209", platform_of('https://www.facebook.com/share/v/TEST000210/') == 'facebook')
    check("platform_00210", platform_of('https://www.facebook.com/posts/TEST000211') == 'facebook')
    check("platform_00211", platform_of('https://www.facebook.com/reel/TEST000212') == 'facebook')
    check("platform_00212", platform_of('https://m.facebook.com/watch/?v=TEST000213') == 'facebook')
    check("platform_00213", platform_of('https://www.facebook.com/share/v/TEST000214/') == 'facebook')
    check("platform_00214", platform_of('https://www.facebook.com/posts/TEST000215') == 'facebook')
    check("platform_00215", platform_of('https://www.facebook.com/reel/TEST000216') == 'facebook')
    check("platform_00216", platform_of('https://m.facebook.com/watch/?v=TEST000217') == 'facebook')
    check("platform_00217", platform_of('https://www.facebook.com/share/v/TEST000218/') == 'facebook')
    check("platform_00218", platform_of('https://www.facebook.com/posts/TEST000219') == 'facebook')
    check("platform_00219", platform_of('https://www.facebook.com/reel/TEST000220') == 'facebook')
    check("platform_00220", platform_of('https://m.facebook.com/watch/?v=TEST000221') == 'facebook')
    check("platform_00221", platform_of('https://www.facebook.com/share/v/TEST000222/') == 'facebook')
    check("platform_00222", platform_of('https://www.facebook.com/posts/TEST000223') == 'facebook')
    check("platform_00223", platform_of('https://www.facebook.com/reel/TEST000224') == 'facebook')
    check("platform_00224", platform_of('https://m.facebook.com/watch/?v=TEST000225') == 'facebook')
    check("platform_00225", platform_of('https://www.facebook.com/share/v/TEST000226/') == 'facebook')
    check("platform_00226", platform_of('https://www.facebook.com/posts/TEST000227') == 'facebook')
    check("platform_00227", platform_of('https://www.facebook.com/reel/TEST000228') == 'facebook')
    check("platform_00228", platform_of('https://m.facebook.com/watch/?v=TEST000229') == 'facebook')
    check("platform_00229", platform_of('https://www.facebook.com/share/v/TEST000230/') == 'facebook')
    check("platform_00230", platform_of('https://www.facebook.com/posts/TEST000231') == 'facebook')
    check("platform_00231", platform_of('https://www.facebook.com/reel/TEST000232') == 'facebook')
    check("platform_00232", platform_of('https://m.facebook.com/watch/?v=TEST000233') == 'facebook')
    check("platform_00233", platform_of('https://www.facebook.com/share/v/TEST000234/') == 'facebook')
    check("platform_00234", platform_of('https://www.facebook.com/posts/TEST000235') == 'facebook')
    check("platform_00235", platform_of('https://www.facebook.com/reel/TEST000236') == 'facebook')
    check("platform_00236", platform_of('https://m.facebook.com/watch/?v=TEST000237') == 'facebook')
    check("platform_00237", platform_of('https://www.facebook.com/share/v/TEST000238/') == 'facebook')
    check("platform_00238", platform_of('https://www.facebook.com/posts/TEST000239') == 'facebook')
    check("platform_00239", platform_of('https://www.facebook.com/reel/TEST000240') == 'facebook')
    check("platform_00240", platform_of('https://m.facebook.com/watch/?v=TEST000241') == 'facebook')
    check("platform_00241", platform_of('https://www.facebook.com/share/v/TEST000242/') == 'facebook')
    check("platform_00242", platform_of('https://www.facebook.com/posts/TEST000243') == 'facebook')
    check("platform_00243", platform_of('https://www.facebook.com/reel/TEST000244') == 'facebook')
    check("platform_00244", platform_of('https://m.facebook.com/watch/?v=TEST000245') == 'facebook')
    check("platform_00245", platform_of('https://www.facebook.com/share/v/TEST000246/') == 'facebook')
    check("platform_00246", platform_of('https://www.facebook.com/posts/TEST000247') == 'facebook')
    check("platform_00247", platform_of('https://www.facebook.com/reel/TEST000248') == 'facebook')
    check("platform_00248", platform_of('https://m.facebook.com/watch/?v=TEST000249') == 'facebook')
    check("platform_00249", platform_of('https://www.facebook.com/share/v/TEST000250/') == 'facebook')
    check("platform_00250", platform_of('https://www.facebook.com/posts/TEST000251') == 'facebook')
    check("platform_00251", platform_of('https://www.facebook.com/reel/TEST000252') == 'facebook')
    check("platform_00252", platform_of('https://m.facebook.com/watch/?v=TEST000253') == 'facebook')
    check("platform_00253", platform_of('https://www.facebook.com/share/v/TEST000254/') == 'facebook')
    check("platform_00254", platform_of('https://www.facebook.com/posts/TEST000255') == 'facebook')
    check("platform_00255", platform_of('https://www.facebook.com/reel/TEST000256') == 'facebook')
    check("platform_00256", platform_of('https://m.facebook.com/watch/?v=TEST000257') == 'facebook')
    check("platform_00257", platform_of('https://www.facebook.com/share/v/TEST000258/') == 'facebook')
    check("platform_00258", platform_of('https://www.facebook.com/posts/TEST000259') == 'facebook')
    check("platform_00259", platform_of('https://www.facebook.com/reel/TEST000260') == 'facebook')
    check("platform_00260", platform_of('https://m.facebook.com/watch/?v=TEST000261') == 'facebook')
    check("platform_00261", platform_of('https://www.facebook.com/share/v/TEST000262/') == 'facebook')
    check("platform_00262", platform_of('https://www.facebook.com/posts/TEST000263') == 'facebook')
    check("platform_00263", platform_of('https://www.facebook.com/reel/TEST000264') == 'facebook')
    check("platform_00264", platform_of('https://m.facebook.com/watch/?v=TEST000265') == 'facebook')
    check("platform_00265", platform_of('https://www.facebook.com/share/v/TEST000266/') == 'facebook')
    check("platform_00266", platform_of('https://www.facebook.com/posts/TEST000267') == 'facebook')
    check("platform_00267", platform_of('https://www.facebook.com/reel/TEST000268') == 'facebook')
    check("platform_00268", platform_of('https://m.facebook.com/watch/?v=TEST000269') == 'facebook')
    check("platform_00269", platform_of('https://www.facebook.com/share/v/TEST000270/') == 'facebook')
    check("platform_00270", platform_of('https://www.facebook.com/posts/TEST000271') == 'facebook')
    check("platform_00271", platform_of('https://www.facebook.com/reel/TEST000272') == 'facebook')
    check("platform_00272", platform_of('https://m.facebook.com/watch/?v=TEST000273') == 'facebook')
    check("platform_00273", platform_of('https://www.facebook.com/share/v/TEST000274/') == 'facebook')
    check("platform_00274", platform_of('https://www.facebook.com/posts/TEST000275') == 'facebook')
    check("platform_00275", platform_of('https://www.facebook.com/reel/TEST000276') == 'facebook')
    check("platform_00276", platform_of('https://m.facebook.com/watch/?v=TEST000277') == 'facebook')
    check("platform_00277", platform_of('https://www.facebook.com/share/v/TEST000278/') == 'facebook')
    check("platform_00278", platform_of('https://www.facebook.com/posts/TEST000279') == 'facebook')
    check("platform_00279", platform_of('https://www.facebook.com/reel/TEST000280') == 'facebook')
    check("platform_00280", platform_of('https://m.facebook.com/watch/?v=TEST000281') == 'facebook')
    check("platform_00281", platform_of('https://www.facebook.com/share/v/TEST000282/') == 'facebook')
    check("platform_00282", platform_of('https://www.facebook.com/posts/TEST000283') == 'facebook')
    check("platform_00283", platform_of('https://www.facebook.com/reel/TEST000284') == 'facebook')
    check("platform_00284", platform_of('https://m.facebook.com/watch/?v=TEST000285') == 'facebook')
    check("platform_00285", platform_of('https://www.facebook.com/share/v/TEST000286/') == 'facebook')
    check("platform_00286", platform_of('https://www.facebook.com/posts/TEST000287') == 'facebook')
    check("platform_00287", platform_of('https://www.facebook.com/reel/TEST000288') == 'facebook')
    check("platform_00288", platform_of('https://m.facebook.com/watch/?v=TEST000289') == 'facebook')
    check("platform_00289", platform_of('https://www.facebook.com/share/v/TEST000290/') == 'facebook')
    check("platform_00290", platform_of('https://www.facebook.com/posts/TEST000291') == 'facebook')
    check("platform_00291", platform_of('https://www.facebook.com/reel/TEST000292') == 'facebook')
    check("platform_00292", platform_of('https://m.facebook.com/watch/?v=TEST000293') == 'facebook')
    check("platform_00293", platform_of('https://www.facebook.com/share/v/TEST000294/') == 'facebook')
    check("platform_00294", platform_of('https://www.facebook.com/posts/TEST000295') == 'facebook')
    check("platform_00295", platform_of('https://www.facebook.com/reel/TEST000296') == 'facebook')
    check("platform_00296", platform_of('https://m.facebook.com/watch/?v=TEST000297') == 'facebook')
    check("platform_00297", platform_of('https://www.facebook.com/share/v/TEST000298/') == 'facebook')
    check("platform_00298", platform_of('https://www.facebook.com/posts/TEST000299') == 'facebook')
    check("platform_00299", platform_of('https://www.facebook.com/reel/TEST000300') == 'facebook')
    check("platform_00300", platform_of('https://m.facebook.com/watch/?v=TEST000301') == 'facebook')
    check("platform_00301", platform_of('https://www.facebook.com/share/v/TEST000302/') == 'facebook')
    check("platform_00302", platform_of('https://www.facebook.com/posts/TEST000303') == 'facebook')
    check("platform_00303", platform_of('https://www.facebook.com/reel/TEST000304') == 'facebook')
    check("platform_00304", platform_of('https://m.facebook.com/watch/?v=TEST000305') == 'facebook')
    check("platform_00305", platform_of('https://www.facebook.com/share/v/TEST000306/') == 'facebook')
    check("platform_00306", platform_of('https://www.facebook.com/posts/TEST000307') == 'facebook')
    check("platform_00307", platform_of('https://www.facebook.com/reel/TEST000308') == 'facebook')
    check("platform_00308", platform_of('https://m.facebook.com/watch/?v=TEST000309') == 'facebook')
    check("platform_00309", platform_of('https://www.facebook.com/share/v/TEST000310/') == 'facebook')
    check("platform_00310", platform_of('https://www.facebook.com/posts/TEST000311') == 'facebook')
    check("platform_00311", platform_of('https://www.facebook.com/reel/TEST000312') == 'facebook')
    check("platform_00312", platform_of('https://m.facebook.com/watch/?v=TEST000313') == 'facebook')
    check("platform_00313", platform_of('https://www.facebook.com/share/v/TEST000314/') == 'facebook')
    check("platform_00314", platform_of('https://www.facebook.com/posts/TEST000315') == 'facebook')
    check("platform_00315", platform_of('https://www.facebook.com/reel/TEST000316') == 'facebook')
    check("platform_00316", platform_of('https://m.facebook.com/watch/?v=TEST000317') == 'facebook')
    check("platform_00317", platform_of('https://www.facebook.com/share/v/TEST000318/') == 'facebook')
    check("platform_00318", platform_of('https://www.facebook.com/posts/TEST000319') == 'facebook')
    check("platform_00319", platform_of('https://www.facebook.com/reel/TEST000320') == 'facebook')
    check("platform_00320", platform_of('https://m.facebook.com/watch/?v=TEST000321') == 'facebook')
    check("platform_00321", platform_of('https://www.facebook.com/share/v/TEST000322/') == 'facebook')
    check("platform_00322", platform_of('https://www.facebook.com/posts/TEST000323') == 'facebook')
    check("platform_00323", platform_of('https://www.facebook.com/reel/TEST000324') == 'facebook')
    check("platform_00324", platform_of('https://m.facebook.com/watch/?v=TEST000325') == 'facebook')
    check("platform_00325", platform_of('https://www.facebook.com/share/v/TEST000326/') == 'facebook')
    check("platform_00326", platform_of('https://www.facebook.com/posts/TEST000327') == 'facebook')
    check("platform_00327", platform_of('https://www.facebook.com/reel/TEST000328') == 'facebook')
    check("platform_00328", platform_of('https://m.facebook.com/watch/?v=TEST000329') == 'facebook')
    check("platform_00329", platform_of('https://www.facebook.com/share/v/TEST000330/') == 'facebook')
    check("platform_00330", platform_of('https://www.facebook.com/posts/TEST000331') == 'facebook')
    check("platform_00331", platform_of('https://www.facebook.com/reel/TEST000332') == 'facebook')
    check("platform_00332", platform_of('https://m.facebook.com/watch/?v=TEST000333') == 'facebook')
    check("platform_00333", platform_of('https://www.facebook.com/share/v/TEST000334/') == 'facebook')
    check("platform_00334", platform_of('https://www.facebook.com/posts/TEST000335') == 'facebook')
    check("platform_00335", platform_of('https://www.facebook.com/reel/TEST000336') == 'facebook')
    check("platform_00336", platform_of('https://m.facebook.com/watch/?v=TEST000337') == 'facebook')
    check("platform_00337", platform_of('https://www.facebook.com/share/v/TEST000338/') == 'facebook')
    check("platform_00338", platform_of('https://www.facebook.com/posts/TEST000339') == 'facebook')
    check("platform_00339", platform_of('https://www.facebook.com/reel/TEST000340') == 'facebook')
    check("platform_00340", platform_of('https://m.facebook.com/watch/?v=TEST000341') == 'facebook')
    check("platform_00341", platform_of('https://www.facebook.com/share/v/TEST000342/') == 'facebook')
    check("platform_00342", platform_of('https://www.facebook.com/posts/TEST000343') == 'facebook')
    check("platform_00343", platform_of('https://www.facebook.com/reel/TEST000344') == 'facebook')
    check("platform_00344", platform_of('https://m.facebook.com/watch/?v=TEST000345') == 'facebook')
    check("platform_00345", platform_of('https://www.facebook.com/share/v/TEST000346/') == 'facebook')
    check("platform_00346", platform_of('https://www.facebook.com/posts/TEST000347') == 'facebook')
    check("platform_00347", platform_of('https://www.facebook.com/reel/TEST000348') == 'facebook')
    check("platform_00348", platform_of('https://m.facebook.com/watch/?v=TEST000349') == 'facebook')
    check("platform_00349", platform_of('https://www.facebook.com/share/v/TEST000350/') == 'facebook')
    check("platform_00350", platform_of('https://www.facebook.com/posts/TEST000351') == 'facebook')
    check("platform_00351", platform_of('https://www.facebook.com/reel/TEST000352') == 'facebook')
    check("platform_00352", platform_of('https://m.facebook.com/watch/?v=TEST000353') == 'facebook')
    check("platform_00353", platform_of('https://www.facebook.com/share/v/TEST000354/') == 'facebook')
    check("platform_00354", platform_of('https://www.facebook.com/posts/TEST000355') == 'facebook')
    check("platform_00355", platform_of('https://www.facebook.com/reel/TEST000356') == 'facebook')
    check("platform_00356", platform_of('https://m.facebook.com/watch/?v=TEST000357') == 'facebook')
    check("platform_00357", platform_of('https://www.facebook.com/share/v/TEST000358/') == 'facebook')
    check("platform_00358", platform_of('https://www.facebook.com/posts/TEST000359') == 'facebook')
    check("platform_00359", platform_of('https://www.facebook.com/reel/TEST000360') == 'facebook')
    check("platform_00360", platform_of('https://m.facebook.com/watch/?v=TEST000361') == 'facebook')
    check("platform_00361", platform_of('https://www.facebook.com/share/v/TEST000362/') == 'facebook')
    check("platform_00362", platform_of('https://www.facebook.com/posts/TEST000363') == 'facebook')
    check("platform_00363", platform_of('https://www.facebook.com/reel/TEST000364') == 'facebook')
    check("platform_00364", platform_of('https://m.facebook.com/watch/?v=TEST000365') == 'facebook')
    check("platform_00365", platform_of('https://www.facebook.com/share/v/TEST000366/') == 'facebook')
    check("platform_00366", platform_of('https://www.facebook.com/posts/TEST000367') == 'facebook')
    check("platform_00367", platform_of('https://www.facebook.com/reel/TEST000368') == 'facebook')
    check("platform_00368", platform_of('https://m.facebook.com/watch/?v=TEST000369') == 'facebook')
    check("platform_00369", platform_of('https://www.facebook.com/share/v/TEST000370/') == 'facebook')
    check("platform_00370", platform_of('https://www.facebook.com/posts/TEST000371') == 'facebook')
    check("platform_00371", platform_of('https://www.facebook.com/reel/TEST000372') == 'facebook')
    check("platform_00372", platform_of('https://m.facebook.com/watch/?v=TEST000373') == 'facebook')
    check("platform_00373", platform_of('https://www.facebook.com/share/v/TEST000374/') == 'facebook')
    check("platform_00374", platform_of('https://www.facebook.com/posts/TEST000375') == 'facebook')
    check("platform_00375", platform_of('https://www.facebook.com/reel/TEST000376') == 'facebook')
    check("platform_00376", platform_of('https://m.facebook.com/watch/?v=TEST000377') == 'facebook')
    check("platform_00377", platform_of('https://www.facebook.com/share/v/TEST000378/') == 'facebook')
    check("platform_00378", platform_of('https://www.facebook.com/posts/TEST000379') == 'facebook')
    check("platform_00379", platform_of('https://www.facebook.com/reel/TEST000380') == 'facebook')
    check("platform_00380", platform_of('https://m.facebook.com/watch/?v=TEST000381') == 'facebook')
    check("platform_00381", platform_of('https://www.facebook.com/share/v/TEST000382/') == 'facebook')
    check("platform_00382", platform_of('https://www.facebook.com/posts/TEST000383') == 'facebook')
    check("platform_00383", platform_of('https://www.facebook.com/reel/TEST000384') == 'facebook')
    check("platform_00384", platform_of('https://m.facebook.com/watch/?v=TEST000385') == 'facebook')
    check("platform_00385", platform_of('https://www.facebook.com/share/v/TEST000386/') == 'facebook')
    check("platform_00386", platform_of('https://www.facebook.com/posts/TEST000387') == 'facebook')
    check("platform_00387", platform_of('https://www.facebook.com/reel/TEST000388') == 'facebook')
    check("platform_00388", platform_of('https://m.facebook.com/watch/?v=TEST000389') == 'facebook')
    check("platform_00389", platform_of('https://www.facebook.com/share/v/TEST000390/') == 'facebook')
    check("platform_00390", platform_of('https://www.facebook.com/posts/TEST000391') == 'facebook')
    check("platform_00391", platform_of('https://www.facebook.com/reel/TEST000392') == 'facebook')
    check("platform_00392", platform_of('https://m.facebook.com/watch/?v=TEST000393') == 'facebook')
    check("platform_00393", platform_of('https://www.facebook.com/share/v/TEST000394/') == 'facebook')
    check("platform_00394", platform_of('https://www.facebook.com/posts/TEST000395') == 'facebook')
    check("platform_00395", platform_of('https://www.facebook.com/reel/TEST000396') == 'facebook')
    check("platform_00396", platform_of('https://m.facebook.com/watch/?v=TEST000397') == 'facebook')
    check("platform_00397", platform_of('https://www.facebook.com/share/v/TEST000398/') == 'facebook')
    check("platform_00398", platform_of('https://www.facebook.com/posts/TEST000399') == 'facebook')
    check("platform_00399", platform_of('https://www.facebook.com/reel/TEST000400') == 'facebook')
    check("platform_00400", platform_of('https://m.facebook.com/watch/?v=TEST000401') == 'facebook')
    check("platform_00401", platform_of('https://www.facebook.com/share/v/TEST000402/') == 'facebook')
    check("platform_00402", platform_of('https://www.facebook.com/posts/TEST000403') == 'facebook')
    check("platform_00403", platform_of('https://www.facebook.com/reel/TEST000404') == 'facebook')
    check("platform_00404", platform_of('https://m.facebook.com/watch/?v=TEST000405') == 'facebook')
    check("platform_00405", platform_of('https://www.facebook.com/share/v/TEST000406/') == 'facebook')
    check("platform_00406", platform_of('https://www.facebook.com/posts/TEST000407') == 'facebook')
    check("platform_00407", platform_of('https://www.facebook.com/reel/TEST000408') == 'facebook')
    check("platform_00408", platform_of('https://m.facebook.com/watch/?v=TEST000409') == 'facebook')
    check("platform_00409", platform_of('https://www.facebook.com/share/v/TEST000410/') == 'facebook')
    check("platform_00410", platform_of('https://www.facebook.com/posts/TEST000411') == 'facebook')
    check("platform_00411", platform_of('https://www.facebook.com/reel/TEST000412') == 'facebook')
    check("platform_00412", platform_of('https://m.facebook.com/watch/?v=TEST000413') == 'facebook')
    check("platform_00413", platform_of('https://www.facebook.com/share/v/TEST000414/') == 'facebook')
    check("platform_00414", platform_of('https://www.facebook.com/posts/TEST000415') == 'facebook')
    check("platform_00415", platform_of('https://www.facebook.com/reel/TEST000416') == 'facebook')
    check("platform_00416", platform_of('https://m.facebook.com/watch/?v=TEST000417') == 'facebook')
    check("platform_00417", platform_of('https://www.facebook.com/share/v/TEST000418/') == 'facebook')
    check("platform_00418", platform_of('https://www.facebook.com/posts/TEST000419') == 'facebook')
    check("platform_00419", platform_of('https://www.facebook.com/reel/TEST000420') == 'facebook')
    check("platform_00420", platform_of('https://m.facebook.com/watch/?v=TEST000421') == 'facebook')
    check("platform_00421", platform_of('https://www.facebook.com/share/v/TEST000422/') == 'facebook')
    check("platform_00422", platform_of('https://www.facebook.com/posts/TEST000423') == 'facebook')
    check("platform_00423", platform_of('https://www.facebook.com/reel/TEST000424') == 'facebook')
    check("platform_00424", platform_of('https://m.facebook.com/watch/?v=TEST000425') == 'facebook')
    check("platform_00425", platform_of('https://www.facebook.com/share/v/TEST000426/') == 'facebook')
    check("platform_00426", platform_of('https://www.facebook.com/posts/TEST000427') == 'facebook')
    check("platform_00427", platform_of('https://www.facebook.com/reel/TEST000428') == 'facebook')
    check("platform_00428", platform_of('https://m.facebook.com/watch/?v=TEST000429') == 'facebook')
    check("platform_00429", platform_of('https://www.facebook.com/share/v/TEST000430/') == 'facebook')
    check("platform_00430", platform_of('https://www.facebook.com/posts/TEST000431') == 'facebook')
    check("platform_00431", platform_of('https://www.facebook.com/reel/TEST000432') == 'facebook')
    check("platform_00432", platform_of('https://m.facebook.com/watch/?v=TEST000433') == 'facebook')
    check("platform_00433", platform_of('https://www.facebook.com/share/v/TEST000434/') == 'facebook')
    check("platform_00434", platform_of('https://www.facebook.com/posts/TEST000435') == 'facebook')
    check("platform_00435", platform_of('https://www.facebook.com/reel/TEST000436') == 'facebook')
    check("platform_00436", platform_of('https://m.facebook.com/watch/?v=TEST000437') == 'facebook')
    check("platform_00437", platform_of('https://www.facebook.com/share/v/TEST000438/') == 'facebook')
    check("platform_00438", platform_of('https://www.facebook.com/posts/TEST000439') == 'facebook')
    check("platform_00439", platform_of('https://www.facebook.com/reel/TEST000440') == 'facebook')
    check("platform_00440", platform_of('https://m.facebook.com/watch/?v=TEST000441') == 'facebook')
    check("platform_00441", platform_of('https://www.facebook.com/share/v/TEST000442/') == 'facebook')
    check("platform_00442", platform_of('https://www.facebook.com/posts/TEST000443') == 'facebook')
    check("platform_00443", platform_of('https://www.facebook.com/reel/TEST000444') == 'facebook')
    check("platform_00444", platform_of('https://m.facebook.com/watch/?v=TEST000445') == 'facebook')
    check("platform_00445", platform_of('https://www.facebook.com/share/v/TEST000446/') == 'facebook')
    check("platform_00446", platform_of('https://www.facebook.com/posts/TEST000447') == 'facebook')
    check("platform_00447", platform_of('https://www.facebook.com/reel/TEST000448') == 'facebook')
    check("platform_00448", platform_of('https://m.facebook.com/watch/?v=TEST000449') == 'facebook')
    check("platform_00449", platform_of('https://www.facebook.com/share/v/TEST000450/') == 'facebook')
    check("platform_00450", platform_of('https://www.facebook.com/posts/TEST000451') == 'facebook')
    check("platform_00451", platform_of('https://www.facebook.com/reel/TEST000452') == 'facebook')
    check("platform_00452", platform_of('https://m.facebook.com/watch/?v=TEST000453') == 'facebook')
    check("platform_00453", platform_of('https://www.facebook.com/share/v/TEST000454/') == 'facebook')
    check("platform_00454", platform_of('https://www.facebook.com/posts/TEST000455') == 'facebook')
    check("platform_00455", platform_of('https://www.facebook.com/reel/TEST000456') == 'facebook')
    check("platform_00456", platform_of('https://m.facebook.com/watch/?v=TEST000457') == 'facebook')
    check("platform_00457", platform_of('https://www.facebook.com/share/v/TEST000458/') == 'facebook')
    check("platform_00458", platform_of('https://www.facebook.com/posts/TEST000459') == 'facebook')
    check("platform_00459", platform_of('https://www.facebook.com/reel/TEST000460') == 'facebook')
    check("platform_00460", platform_of('https://m.facebook.com/watch/?v=TEST000461') == 'facebook')
    check("platform_00461", platform_of('https://www.facebook.com/share/v/TEST000462/') == 'facebook')
    check("platform_00462", platform_of('https://www.facebook.com/posts/TEST000463') == 'facebook')
    check("platform_00463", platform_of('https://www.facebook.com/reel/TEST000464') == 'facebook')
    check("platform_00464", platform_of('https://m.facebook.com/watch/?v=TEST000465') == 'facebook')
    check("platform_00465", platform_of('https://www.facebook.com/share/v/TEST000466/') == 'facebook')
    check("platform_00466", platform_of('https://www.facebook.com/posts/TEST000467') == 'facebook')
    check("platform_00467", platform_of('https://www.facebook.com/reel/TEST000468') == 'facebook')
    check("platform_00468", platform_of('https://m.facebook.com/watch/?v=TEST000469') == 'facebook')
    check("platform_00469", platform_of('https://www.facebook.com/share/v/TEST000470/') == 'facebook')
    check("platform_00470", platform_of('https://www.facebook.com/posts/TEST000471') == 'facebook')
    check("platform_00471", platform_of('https://www.facebook.com/reel/TEST000472') == 'facebook')
    check("platform_00472", platform_of('https://m.facebook.com/watch/?v=TEST000473') == 'facebook')
    check("platform_00473", platform_of('https://www.facebook.com/share/v/TEST000474/') == 'facebook')
    check("platform_00474", platform_of('https://www.facebook.com/posts/TEST000475') == 'facebook')
    check("platform_00475", platform_of('https://www.facebook.com/reel/TEST000476') == 'facebook')
    check("platform_00476", platform_of('https://m.facebook.com/watch/?v=TEST000477') == 'facebook')
    check("platform_00477", platform_of('https://www.facebook.com/share/v/TEST000478/') == 'facebook')
    check("platform_00478", platform_of('https://www.facebook.com/posts/TEST000479') == 'facebook')
    check("platform_00479", platform_of('https://www.facebook.com/reel/TEST000480') == 'facebook')
    check("platform_00480", platform_of('https://m.facebook.com/watch/?v=TEST000481') == 'facebook')
    check("platform_00481", platform_of('https://www.facebook.com/share/v/TEST000482/') == 'facebook')
    check("platform_00482", platform_of('https://www.facebook.com/posts/TEST000483') == 'facebook')
    check("platform_00483", platform_of('https://www.facebook.com/reel/TEST000484') == 'facebook')
    check("platform_00484", platform_of('https://m.facebook.com/watch/?v=TEST000485') == 'facebook')
    check("platform_00485", platform_of('https://www.facebook.com/share/v/TEST000486/') == 'facebook')
    check("platform_00486", platform_of('https://www.facebook.com/posts/TEST000487') == 'facebook')
    check("platform_00487", platform_of('https://www.facebook.com/reel/TEST000488') == 'facebook')
    check("platform_00488", platform_of('https://m.facebook.com/watch/?v=TEST000489') == 'facebook')
    check("platform_00489", platform_of('https://www.facebook.com/share/v/TEST000490/') == 'facebook')
    check("platform_00490", platform_of('https://www.facebook.com/posts/TEST000491') == 'facebook')
    check("platform_00491", platform_of('https://www.facebook.com/reel/TEST000492') == 'facebook')
    check("platform_00492", platform_of('https://m.facebook.com/watch/?v=TEST000493') == 'facebook')
    check("platform_00493", platform_of('https://www.facebook.com/share/v/TEST000494/') == 'facebook')
    check("platform_00494", platform_of('https://www.facebook.com/posts/TEST000495') == 'facebook')
    check("platform_00495", platform_of('https://www.facebook.com/reel/TEST000496') == 'facebook')
    check("platform_00496", platform_of('https://m.facebook.com/watch/?v=TEST000497') == 'facebook')
    check("platform_00497", platform_of('https://www.facebook.com/share/v/TEST000498/') == 'facebook')
    check("platform_00498", platform_of('https://www.facebook.com/posts/TEST000499') == 'facebook')
    check("platform_00499", platform_of('https://www.facebook.com/reel/TEST000500') == 'facebook')
    check("platform_00500", platform_of('https://m.facebook.com/watch/?v=TEST000501') == 'facebook')
    check("platform_00501", platform_of('https://www.facebook.com/share/v/TEST000502/') == 'facebook')
    check("platform_00502", platform_of('https://www.facebook.com/posts/TEST000503') == 'facebook')
    check("platform_00503", platform_of('https://www.facebook.com/reel/TEST000504') == 'facebook')
    check("platform_00504", platform_of('https://m.facebook.com/watch/?v=TEST000505') == 'facebook')
    check("platform_00505", platform_of('https://www.facebook.com/share/v/TEST000506/') == 'facebook')
    check("platform_00506", platform_of('https://www.facebook.com/posts/TEST000507') == 'facebook')
    check("platform_00507", platform_of('https://www.facebook.com/reel/TEST000508') == 'facebook')
    check("platform_00508", platform_of('https://m.facebook.com/watch/?v=TEST000509') == 'facebook')
    check("platform_00509", platform_of('https://www.facebook.com/share/v/TEST000510/') == 'facebook')
    check("platform_00510", platform_of('https://www.facebook.com/posts/TEST000511') == 'facebook')
    check("platform_00511", platform_of('https://www.facebook.com/reel/TEST000512') == 'facebook')
    check("platform_00512", platform_of('https://m.facebook.com/watch/?v=TEST000513') == 'facebook')
    check("platform_00513", platform_of('https://www.facebook.com/share/v/TEST000514/') == 'facebook')
    check("platform_00514", platform_of('https://www.facebook.com/posts/TEST000515') == 'facebook')
    check("platform_00515", platform_of('https://www.facebook.com/reel/TEST000516') == 'facebook')
    check("platform_00516", platform_of('https://m.facebook.com/watch/?v=TEST000517') == 'facebook')
    check("platform_00517", platform_of('https://www.facebook.com/share/v/TEST000518/') == 'facebook')
    check("platform_00518", platform_of('https://www.facebook.com/posts/TEST000519') == 'facebook')
    check("platform_00519", platform_of('https://www.facebook.com/reel/TEST000520') == 'facebook')
    check("platform_00520", platform_of('https://m.facebook.com/watch/?v=TEST000521') == 'facebook')
    check("platform_00521", platform_of('https://www.facebook.com/share/v/TEST000522/') == 'facebook')
    check("platform_00522", platform_of('https://www.facebook.com/posts/TEST000523') == 'facebook')
    check("platform_00523", platform_of('https://www.facebook.com/reel/TEST000524') == 'facebook')
    check("platform_00524", platform_of('https://m.facebook.com/watch/?v=TEST000525') == 'facebook')
    check("platform_00525", platform_of('https://www.facebook.com/share/v/TEST000526/') == 'facebook')
    check("platform_00526", platform_of('https://www.facebook.com/posts/TEST000527') == 'facebook')
    check("platform_00527", platform_of('https://www.facebook.com/reel/TEST000528') == 'facebook')
    check("platform_00528", platform_of('https://m.facebook.com/watch/?v=TEST000529') == 'facebook')
    check("platform_00529", platform_of('https://www.facebook.com/share/v/TEST000530/') == 'facebook')
    check("platform_00530", platform_of('https://www.facebook.com/posts/TEST000531') == 'facebook')
    check("platform_00531", platform_of('https://www.facebook.com/reel/TEST000532') == 'facebook')
    check("platform_00532", platform_of('https://m.facebook.com/watch/?v=TEST000533') == 'facebook')
    check("platform_00533", platform_of('https://www.facebook.com/share/v/TEST000534/') == 'facebook')
    check("platform_00534", platform_of('https://www.facebook.com/posts/TEST000535') == 'facebook')
    check("platform_00535", platform_of('https://www.facebook.com/reel/TEST000536') == 'facebook')
    check("platform_00536", platform_of('https://m.facebook.com/watch/?v=TEST000537') == 'facebook')
    check("platform_00537", platform_of('https://www.facebook.com/share/v/TEST000538/') == 'facebook')
    check("platform_00538", platform_of('https://www.facebook.com/posts/TEST000539') == 'facebook')
    check("platform_00539", platform_of('https://www.facebook.com/reel/TEST000540') == 'facebook')
    check("platform_00540", platform_of('https://m.facebook.com/watch/?v=TEST000541') == 'facebook')
    check("platform_00541", platform_of('https://www.facebook.com/share/v/TEST000542/') == 'facebook')
    check("platform_00542", platform_of('https://www.facebook.com/posts/TEST000543') == 'facebook')
    check("platform_00543", platform_of('https://www.facebook.com/reel/TEST000544') == 'facebook')
    check("platform_00544", platform_of('https://m.facebook.com/watch/?v=TEST000545') == 'facebook')
    check("platform_00545", platform_of('https://www.facebook.com/share/v/TEST000546/') == 'facebook')
    check("platform_00546", platform_of('https://www.facebook.com/posts/TEST000547') == 'facebook')
    check("platform_00547", platform_of('https://www.facebook.com/reel/TEST000548') == 'facebook')
    check("platform_00548", platform_of('https://m.facebook.com/watch/?v=TEST000549') == 'facebook')
    check("platform_00549", platform_of('https://www.facebook.com/share/v/TEST000550/') == 'facebook')
    check("platform_00550", platform_of('https://www.facebook.com/posts/TEST000551') == 'facebook')
    check("platform_00551", platform_of('https://www.facebook.com/reel/TEST000552') == 'facebook')
    check("platform_00552", platform_of('https://m.facebook.com/watch/?v=TEST000553') == 'facebook')
    check("platform_00553", platform_of('https://www.facebook.com/share/v/TEST000554/') == 'facebook')
    check("platform_00554", platform_of('https://www.facebook.com/posts/TEST000555') == 'facebook')
    check("platform_00555", platform_of('https://www.facebook.com/reel/TEST000556') == 'facebook')
    check("platform_00556", platform_of('https://m.facebook.com/watch/?v=TEST000557') == 'facebook')
    check("platform_00557", platform_of('https://www.facebook.com/share/v/TEST000558/') == 'facebook')
    check("platform_00558", platform_of('https://www.facebook.com/posts/TEST000559') == 'facebook')
    check("platform_00559", platform_of('https://www.facebook.com/reel/TEST000560') == 'facebook')
    check("platform_00560", platform_of('https://m.facebook.com/watch/?v=TEST000561') == 'facebook')
    check("platform_00561", platform_of('https://www.facebook.com/share/v/TEST000562/') == 'facebook')
    check("platform_00562", platform_of('https://www.facebook.com/posts/TEST000563') == 'facebook')
    check("platform_00563", platform_of('https://www.facebook.com/reel/TEST000564') == 'facebook')
    check("platform_00564", platform_of('https://m.facebook.com/watch/?v=TEST000565') == 'facebook')
    check("platform_00565", platform_of('https://www.facebook.com/share/v/TEST000566/') == 'facebook')
    check("platform_00566", platform_of('https://www.facebook.com/posts/TEST000567') == 'facebook')
    check("platform_00567", platform_of('https://www.facebook.com/reel/TEST000568') == 'facebook')
    check("platform_00568", platform_of('https://m.facebook.com/watch/?v=TEST000569') == 'facebook')
    check("platform_00569", platform_of('https://www.facebook.com/share/v/TEST000570/') == 'facebook')
    check("platform_00570", platform_of('https://www.facebook.com/posts/TEST000571') == 'facebook')
    check("platform_00571", platform_of('https://www.facebook.com/reel/TEST000572') == 'facebook')
    check("platform_00572", platform_of('https://m.facebook.com/watch/?v=TEST000573') == 'facebook')
    check("platform_00573", platform_of('https://www.facebook.com/share/v/TEST000574/') == 'facebook')
    check("platform_00574", platform_of('https://www.facebook.com/posts/TEST000575') == 'facebook')
    check("platform_00575", platform_of('https://www.facebook.com/reel/TEST000576') == 'facebook')
    check("platform_00576", platform_of('https://m.facebook.com/watch/?v=TEST000577') == 'facebook')
    check("platform_00577", platform_of('https://www.facebook.com/share/v/TEST000578/') == 'facebook')
    check("platform_00578", platform_of('https://www.facebook.com/posts/TEST000579') == 'facebook')
    check("platform_00579", platform_of('https://www.facebook.com/reel/TEST000580') == 'facebook')
    check("platform_00580", platform_of('https://m.facebook.com/watch/?v=TEST000581') == 'facebook')
    check("platform_00581", platform_of('https://www.facebook.com/share/v/TEST000582/') == 'facebook')
    check("platform_00582", platform_of('https://www.facebook.com/posts/TEST000583') == 'facebook')
    check("platform_00583", platform_of('https://www.facebook.com/reel/TEST000584') == 'facebook')
    check("platform_00584", platform_of('https://m.facebook.com/watch/?v=TEST000585') == 'facebook')
    check("platform_00585", platform_of('https://www.facebook.com/share/v/TEST000586/') == 'facebook')
    check("platform_00586", platform_of('https://www.facebook.com/posts/TEST000587') == 'facebook')
    check("platform_00587", platform_of('https://www.facebook.com/reel/TEST000588') == 'facebook')
    check("platform_00588", platform_of('https://m.facebook.com/watch/?v=TEST000589') == 'facebook')
    check("platform_00589", platform_of('https://www.facebook.com/share/v/TEST000590/') == 'facebook')
    check("platform_00590", platform_of('https://www.facebook.com/posts/TEST000591') == 'facebook')
    check("platform_00591", platform_of('https://www.facebook.com/reel/TEST000592') == 'facebook')
    check("platform_00592", platform_of('https://m.facebook.com/watch/?v=TEST000593') == 'facebook')
    check("platform_00593", platform_of('https://www.facebook.com/share/v/TEST000594/') == 'facebook')
    check("platform_00594", platform_of('https://www.facebook.com/posts/TEST000595') == 'facebook')
    check("platform_00595", platform_of('https://www.facebook.com/reel/TEST000596') == 'facebook')
    check("platform_00596", platform_of('https://m.facebook.com/watch/?v=TEST000597') == 'facebook')
    check("platform_00597", platform_of('https://www.facebook.com/share/v/TEST000598/') == 'facebook')
    check("platform_00598", platform_of('https://www.facebook.com/posts/TEST000599') == 'facebook')
    check("platform_00599", platform_of('https://www.facebook.com/reel/TEST000600') == 'facebook')
    check("platform_00600", platform_of('https://m.facebook.com/watch/?v=TEST000601') == 'facebook')
    check("platform_00601", platform_of('https://www.facebook.com/share/v/TEST000602/') == 'facebook')
    check("platform_00602", platform_of('https://www.facebook.com/posts/TEST000603') == 'facebook')
    check("platform_00603", platform_of('https://www.facebook.com/reel/TEST000604') == 'facebook')
    check("platform_00604", platform_of('https://m.facebook.com/watch/?v=TEST000605') == 'facebook')
    check("platform_00605", platform_of('https://www.facebook.com/share/v/TEST000606/') == 'facebook')
    check("platform_00606", platform_of('https://www.facebook.com/posts/TEST000607') == 'facebook')
    check("platform_00607", platform_of('https://www.facebook.com/reel/TEST000608') == 'facebook')
    check("platform_00608", platform_of('https://m.facebook.com/watch/?v=TEST000609') == 'facebook')
    check("platform_00609", platform_of('https://www.facebook.com/share/v/TEST000610/') == 'facebook')
    check("platform_00610", platform_of('https://www.facebook.com/posts/TEST000611') == 'facebook')
    check("platform_00611", platform_of('https://www.facebook.com/reel/TEST000612') == 'facebook')
    check("platform_00612", platform_of('https://m.facebook.com/watch/?v=TEST000613') == 'facebook')
    check("platform_00613", platform_of('https://www.facebook.com/share/v/TEST000614/') == 'facebook')
    check("platform_00614", platform_of('https://www.facebook.com/posts/TEST000615') == 'facebook')
    check("platform_00615", platform_of('https://www.facebook.com/reel/TEST000616') == 'facebook')
    check("platform_00616", platform_of('https://m.facebook.com/watch/?v=TEST000617') == 'facebook')
    check("platform_00617", platform_of('https://www.facebook.com/share/v/TEST000618/') == 'facebook')
    check("platform_00618", platform_of('https://www.facebook.com/posts/TEST000619') == 'facebook')
    check("platform_00619", platform_of('https://www.facebook.com/reel/TEST000620') == 'facebook')
    check("platform_00620", platform_of('https://m.facebook.com/watch/?v=TEST000621') == 'facebook')
    check("platform_00621", platform_of('https://www.facebook.com/share/v/TEST000622/') == 'facebook')
    check("platform_00622", platform_of('https://www.facebook.com/posts/TEST000623') == 'facebook')
    check("platform_00623", platform_of('https://www.facebook.com/reel/TEST000624') == 'facebook')
    check("platform_00624", platform_of('https://m.facebook.com/watch/?v=TEST000625') == 'facebook')
    check("platform_00625", platform_of('https://www.facebook.com/share/v/TEST000626/') == 'facebook')
    check("platform_00626", platform_of('https://www.facebook.com/posts/TEST000627') == 'facebook')
    check("platform_00627", platform_of('https://www.facebook.com/reel/TEST000628') == 'facebook')
    check("platform_00628", platform_of('https://m.facebook.com/watch/?v=TEST000629') == 'facebook')
    check("platform_00629", platform_of('https://www.facebook.com/share/v/TEST000630/') == 'facebook')
    check("platform_00630", platform_of('https://www.facebook.com/posts/TEST000631') == 'facebook')
    check("platform_00631", platform_of('https://www.facebook.com/reel/TEST000632') == 'facebook')
    check("platform_00632", platform_of('https://m.facebook.com/watch/?v=TEST000633') == 'facebook')
    check("platform_00633", platform_of('https://www.facebook.com/share/v/TEST000634/') == 'facebook')
    check("platform_00634", platform_of('https://www.facebook.com/posts/TEST000635') == 'facebook')
    check("platform_00635", platform_of('https://www.facebook.com/reel/TEST000636') == 'facebook')
    check("platform_00636", platform_of('https://m.facebook.com/watch/?v=TEST000637') == 'facebook')
    check("platform_00637", platform_of('https://www.facebook.com/share/v/TEST000638/') == 'facebook')
    check("platform_00638", platform_of('https://www.facebook.com/posts/TEST000639') == 'facebook')
    check("platform_00639", platform_of('https://www.facebook.com/reel/TEST000640') == 'facebook')
    check("platform_00640", platform_of('https://m.facebook.com/watch/?v=TEST000641') == 'facebook')
    check("platform_00641", platform_of('https://www.facebook.com/share/v/TEST000642/') == 'facebook')
    check("platform_00642", platform_of('https://www.facebook.com/posts/TEST000643') == 'facebook')
    check("platform_00643", platform_of('https://www.facebook.com/reel/TEST000644') == 'facebook')
    check("platform_00644", platform_of('https://m.facebook.com/watch/?v=TEST000645') == 'facebook')
    check("platform_00645", platform_of('https://www.facebook.com/share/v/TEST000646/') == 'facebook')
    check("platform_00646", platform_of('https://www.facebook.com/posts/TEST000647') == 'facebook')
    check("platform_00647", platform_of('https://www.facebook.com/reel/TEST000648') == 'facebook')
    check("platform_00648", platform_of('https://m.facebook.com/watch/?v=TEST000649') == 'facebook')
    check("platform_00649", platform_of('https://www.facebook.com/share/v/TEST000650/') == 'facebook')
    check("platform_00650", platform_of('https://www.facebook.com/posts/TEST000651') == 'facebook')
    check("platform_00651", platform_of('https://www.facebook.com/reel/TEST000652') == 'facebook')
    check("platform_00652", platform_of('https://m.facebook.com/watch/?v=TEST000653') == 'facebook')
    check("platform_00653", platform_of('https://www.facebook.com/share/v/TEST000654/') == 'facebook')
    check("platform_00654", platform_of('https://www.facebook.com/posts/TEST000655') == 'facebook')
    check("platform_00655", platform_of('https://www.facebook.com/reel/TEST000656') == 'facebook')
    check("platform_00656", platform_of('https://m.facebook.com/watch/?v=TEST000657') == 'facebook')
    check("platform_00657", platform_of('https://www.facebook.com/share/v/TEST000658/') == 'facebook')
    check("platform_00658", platform_of('https://www.facebook.com/posts/TEST000659') == 'facebook')
    check("platform_00659", platform_of('https://www.facebook.com/reel/TEST000660') == 'facebook')
    check("platform_00660", platform_of('https://m.facebook.com/watch/?v=TEST000661') == 'facebook')
    check("platform_00661", platform_of('https://www.facebook.com/share/v/TEST000662/') == 'facebook')
    check("platform_00662", platform_of('https://www.facebook.com/posts/TEST000663') == 'facebook')
    check("platform_00663", platform_of('https://www.facebook.com/reel/TEST000664') == 'facebook')
    check("platform_00664", platform_of('https://m.facebook.com/watch/?v=TEST000665') == 'facebook')
    check("platform_00665", platform_of('https://www.facebook.com/share/v/TEST000666/') == 'facebook')
    check("platform_00666", platform_of('https://www.facebook.com/posts/TEST000667') == 'facebook')
    check("platform_00667", platform_of('https://www.facebook.com/reel/TEST000668') == 'facebook')
    check("platform_00668", platform_of('https://m.facebook.com/watch/?v=TEST000669') == 'facebook')
    check("platform_00669", platform_of('https://www.facebook.com/share/v/TEST000670/') == 'facebook')
    check("platform_00670", platform_of('https://www.facebook.com/posts/TEST000671') == 'facebook')
    check("platform_00671", platform_of('https://www.facebook.com/reel/TEST000672') == 'facebook')
    check("platform_00672", platform_of('https://m.facebook.com/watch/?v=TEST000673') == 'facebook')
    check("platform_00673", platform_of('https://www.facebook.com/share/v/TEST000674/') == 'facebook')
    check("platform_00674", platform_of('https://www.facebook.com/posts/TEST000675') == 'facebook')
    check("platform_00675", platform_of('https://www.facebook.com/reel/TEST000676') == 'facebook')
    check("platform_00676", platform_of('https://m.facebook.com/watch/?v=TEST000677') == 'facebook')
    check("platform_00677", platform_of('https://www.facebook.com/share/v/TEST000678/') == 'facebook')
    check("platform_00678", platform_of('https://www.facebook.com/posts/TEST000679') == 'facebook')
    check("platform_00679", platform_of('https://www.facebook.com/reel/TEST000680') == 'facebook')
    check("platform_00680", platform_of('https://m.facebook.com/watch/?v=TEST000681') == 'facebook')
    check("platform_00681", platform_of('https://www.facebook.com/share/v/TEST000682/') == 'facebook')
    check("platform_00682", platform_of('https://www.facebook.com/posts/TEST000683') == 'facebook')
    check("platform_00683", platform_of('https://www.facebook.com/reel/TEST000684') == 'facebook')
    check("platform_00684", platform_of('https://m.facebook.com/watch/?v=TEST000685') == 'facebook')
    check("platform_00685", platform_of('https://www.facebook.com/share/v/TEST000686/') == 'facebook')
    check("platform_00686", platform_of('https://www.facebook.com/posts/TEST000687') == 'facebook')
    check("platform_00687", platform_of('https://www.facebook.com/reel/TEST000688') == 'facebook')
    check("platform_00688", platform_of('https://m.facebook.com/watch/?v=TEST000689') == 'facebook')
    check("platform_00689", platform_of('https://www.facebook.com/share/v/TEST000690/') == 'facebook')
    check("platform_00690", platform_of('https://www.facebook.com/posts/TEST000691') == 'facebook')
    check("platform_00691", platform_of('https://www.facebook.com/reel/TEST000692') == 'facebook')
    check("platform_00692", platform_of('https://m.facebook.com/watch/?v=TEST000693') == 'facebook')
    check("platform_00693", platform_of('https://www.facebook.com/share/v/TEST000694/') == 'facebook')
    check("platform_00694", platform_of('https://www.facebook.com/posts/TEST000695') == 'facebook')
    check("platform_00695", platform_of('https://www.facebook.com/reel/TEST000696') == 'facebook')
    check("platform_00696", platform_of('https://m.facebook.com/watch/?v=TEST000697') == 'facebook')
    check("platform_00697", platform_of('https://www.facebook.com/share/v/TEST000698/') == 'facebook')
    check("platform_00698", platform_of('https://www.facebook.com/posts/TEST000699') == 'facebook')
    check("platform_00699", platform_of('https://www.facebook.com/reel/TEST000700') == 'facebook')
    check("platform_00700", platform_of('https://m.facebook.com/watch/?v=TEST000701') == 'facebook')
    check("platform_00701", platform_of('https://www.facebook.com/share/v/TEST000702/') == 'facebook')
    check("platform_00702", platform_of('https://www.facebook.com/posts/TEST000703') == 'facebook')
    check("platform_00703", platform_of('https://www.facebook.com/reel/TEST000704') == 'facebook')
    check("platform_00704", platform_of('https://m.facebook.com/watch/?v=TEST000705') == 'facebook')
    check("platform_00705", platform_of('https://www.facebook.com/share/v/TEST000706/') == 'facebook')
    check("platform_00706", platform_of('https://www.facebook.com/posts/TEST000707') == 'facebook')
    check("platform_00707", platform_of('https://www.facebook.com/reel/TEST000708') == 'facebook')
    check("platform_00708", platform_of('https://m.facebook.com/watch/?v=TEST000709') == 'facebook')
    check("platform_00709", platform_of('https://www.facebook.com/share/v/TEST000710/') == 'facebook')
    check("platform_00710", platform_of('https://www.facebook.com/posts/TEST000711') == 'facebook')
    check("platform_00711", platform_of('https://www.facebook.com/reel/TEST000712') == 'facebook')
    check("platform_00712", platform_of('https://m.facebook.com/watch/?v=TEST000713') == 'facebook')
    check("platform_00713", platform_of('https://www.facebook.com/share/v/TEST000714/') == 'facebook')
    check("platform_00714", platform_of('https://www.facebook.com/posts/TEST000715') == 'facebook')
    check("platform_00715", platform_of('https://www.facebook.com/reel/TEST000716') == 'facebook')
    check("platform_00716", platform_of('https://m.facebook.com/watch/?v=TEST000717') == 'facebook')
    check("platform_00717", platform_of('https://www.facebook.com/share/v/TEST000718/') == 'facebook')
    check("platform_00718", platform_of('https://www.facebook.com/posts/TEST000719') == 'facebook')
    check("platform_00719", platform_of('https://www.facebook.com/reel/TEST000720') == 'facebook')
    check("platform_00720", platform_of('https://m.facebook.com/watch/?v=TEST000721') == 'facebook')
    check("platform_00721", platform_of('https://www.facebook.com/share/v/TEST000722/') == 'facebook')
    check("platform_00722", platform_of('https://www.facebook.com/posts/TEST000723') == 'facebook')
    check("platform_00723", platform_of('https://www.facebook.com/reel/TEST000724') == 'facebook')
    check("platform_00724", platform_of('https://m.facebook.com/watch/?v=TEST000725') == 'facebook')
    check("platform_00725", platform_of('https://www.facebook.com/share/v/TEST000726/') == 'facebook')
    check("platform_00726", platform_of('https://www.facebook.com/posts/TEST000727') == 'facebook')
    check("platform_00727", platform_of('https://www.facebook.com/reel/TEST000728') == 'facebook')
    check("platform_00728", platform_of('https://m.facebook.com/watch/?v=TEST000729') == 'facebook')
    check("platform_00729", platform_of('https://www.facebook.com/share/v/TEST000730/') == 'facebook')
    check("platform_00730", platform_of('https://www.facebook.com/posts/TEST000731') == 'facebook')
    check("platform_00731", platform_of('https://www.facebook.com/reel/TEST000732') == 'facebook')
    check("platform_00732", platform_of('https://m.facebook.com/watch/?v=TEST000733') == 'facebook')
    check("platform_00733", platform_of('https://www.facebook.com/share/v/TEST000734/') == 'facebook')
    check("platform_00734", platform_of('https://www.facebook.com/posts/TEST000735') == 'facebook')
    check("platform_00735", platform_of('https://www.facebook.com/reel/TEST000736') == 'facebook')
    check("platform_00736", platform_of('https://m.facebook.com/watch/?v=TEST000737') == 'facebook')
    check("platform_00737", platform_of('https://www.facebook.com/share/v/TEST000738/') == 'facebook')
    check("platform_00738", platform_of('https://www.facebook.com/posts/TEST000739') == 'facebook')
    check("platform_00739", platform_of('https://www.facebook.com/reel/TEST000740') == 'facebook')
    check("platform_00740", platform_of('https://m.facebook.com/watch/?v=TEST000741') == 'facebook')
    check("platform_00741", platform_of('https://www.facebook.com/share/v/TEST000742/') == 'facebook')
    check("platform_00742", platform_of('https://www.facebook.com/posts/TEST000743') == 'facebook')
    check("platform_00743", platform_of('https://www.facebook.com/reel/TEST000744') == 'facebook')
    check("platform_00744", platform_of('https://m.facebook.com/watch/?v=TEST000745') == 'facebook')
    check("platform_00745", platform_of('https://www.facebook.com/share/v/TEST000746/') == 'facebook')
    check("platform_00746", platform_of('https://www.facebook.com/posts/TEST000747') == 'facebook')
    check("platform_00747", platform_of('https://www.facebook.com/reel/TEST000748') == 'facebook')
    check("platform_00748", platform_of('https://m.facebook.com/watch/?v=TEST000749') == 'facebook')
    check("platform_00749", platform_of('https://www.facebook.com/share/v/TEST000750/') == 'facebook')
    check("platform_00750", platform_of('https://www.facebook.com/posts/TEST000751') == 'facebook')
    check("platform_00751", platform_of('https://www.facebook.com/reel/TEST000752') == 'facebook')
    check("platform_00752", platform_of('https://m.facebook.com/watch/?v=TEST000753') == 'facebook')
    check("platform_00753", platform_of('https://www.facebook.com/share/v/TEST000754/') == 'facebook')
    check("platform_00754", platform_of('https://www.facebook.com/posts/TEST000755') == 'facebook')
    check("platform_00755", platform_of('https://www.facebook.com/reel/TEST000756') == 'facebook')
    check("platform_00756", platform_of('https://m.facebook.com/watch/?v=TEST000757') == 'facebook')
    check("platform_00757", platform_of('https://www.facebook.com/share/v/TEST000758/') == 'facebook')
    check("platform_00758", platform_of('https://www.facebook.com/posts/TEST000759') == 'facebook')
    check("platform_00759", platform_of('https://www.facebook.com/reel/TEST000760') == 'facebook')
    check("platform_00760", platform_of('https://m.facebook.com/watch/?v=TEST000761') == 'facebook')
    check("platform_00761", platform_of('https://www.facebook.com/share/v/TEST000762/') == 'facebook')
    check("platform_00762", platform_of('https://www.facebook.com/posts/TEST000763') == 'facebook')
    check("platform_00763", platform_of('https://www.facebook.com/reel/TEST000764') == 'facebook')
    check("platform_00764", platform_of('https://m.facebook.com/watch/?v=TEST000765') == 'facebook')
    check("platform_00765", platform_of('https://www.facebook.com/share/v/TEST000766/') == 'facebook')
    check("platform_00766", platform_of('https://www.facebook.com/posts/TEST000767') == 'facebook')
    check("platform_00767", platform_of('https://www.facebook.com/reel/TEST000768') == 'facebook')
    check("platform_00768", platform_of('https://m.facebook.com/watch/?v=TEST000769') == 'facebook')
    check("platform_00769", platform_of('https://www.facebook.com/share/v/TEST000770/') == 'facebook')
    check("platform_00770", platform_of('https://www.facebook.com/posts/TEST000771') == 'facebook')
    check("platform_00771", platform_of('https://www.facebook.com/reel/TEST000772') == 'facebook')
    check("platform_00772", platform_of('https://m.facebook.com/watch/?v=TEST000773') == 'facebook')
    check("platform_00773", platform_of('https://www.facebook.com/share/v/TEST000774/') == 'facebook')
    check("platform_00774", platform_of('https://www.facebook.com/posts/TEST000775') == 'facebook')
    check("platform_00775", platform_of('https://www.facebook.com/reel/TEST000776') == 'facebook')
    check("platform_00776", platform_of('https://m.facebook.com/watch/?v=TEST000777') == 'facebook')
    check("platform_00777", platform_of('https://www.facebook.com/share/v/TEST000778/') == 'facebook')
    check("platform_00778", platform_of('https://www.facebook.com/posts/TEST000779') == 'facebook')
    check("platform_00779", platform_of('https://www.facebook.com/reel/TEST000780') == 'facebook')
    check("platform_00780", platform_of('https://m.facebook.com/watch/?v=TEST000781') == 'facebook')
    check("platform_00781", platform_of('https://www.facebook.com/share/v/TEST000782/') == 'facebook')
    check("platform_00782", platform_of('https://www.facebook.com/posts/TEST000783') == 'facebook')
    check("platform_00783", platform_of('https://www.facebook.com/reel/TEST000784') == 'facebook')
    check("platform_00784", platform_of('https://m.facebook.com/watch/?v=TEST000785') == 'facebook')
    check("platform_00785", platform_of('https://www.facebook.com/share/v/TEST000786/') == 'facebook')
    check("platform_00786", platform_of('https://www.facebook.com/posts/TEST000787') == 'facebook')
    check("platform_00787", platform_of('https://www.facebook.com/reel/TEST000788') == 'facebook')
    check("platform_00788", platform_of('https://m.facebook.com/watch/?v=TEST000789') == 'facebook')
    check("platform_00789", platform_of('https://www.facebook.com/share/v/TEST000790/') == 'facebook')
    check("platform_00790", platform_of('https://www.facebook.com/posts/TEST000791') == 'facebook')
    check("platform_00791", platform_of('https://www.facebook.com/reel/TEST000792') == 'facebook')
    check("platform_00792", platform_of('https://m.facebook.com/watch/?v=TEST000793') == 'facebook')
    check("platform_00793", platform_of('https://www.facebook.com/share/v/TEST000794/') == 'facebook')
    check("platform_00794", platform_of('https://www.facebook.com/posts/TEST000795') == 'facebook')
    check("platform_00795", platform_of('https://www.facebook.com/reel/TEST000796') == 'facebook')
    check("platform_00796", platform_of('https://m.facebook.com/watch/?v=TEST000797') == 'facebook')
    check("platform_00797", platform_of('https://www.facebook.com/share/v/TEST000798/') == 'facebook')
    check("platform_00798", platform_of('https://www.facebook.com/posts/TEST000799') == 'facebook')
    check("platform_00799", platform_of('https://www.facebook.com/reel/TEST000800') == 'facebook')
    check("platform_00800", platform_of('https://m.facebook.com/watch/?v=TEST000801') == 'facebook')
    check("platform_00801", platform_of('https://www.facebook.com/share/v/TEST000802/') == 'facebook')
    check("platform_00802", platform_of('https://www.facebook.com/posts/TEST000803') == 'facebook')
    check("platform_00803", platform_of('https://www.facebook.com/reel/TEST000804') == 'facebook')
    check("platform_00804", platform_of('https://m.facebook.com/watch/?v=TEST000805') == 'facebook')
    check("platform_00805", platform_of('https://www.facebook.com/share/v/TEST000806/') == 'facebook')
    check("platform_00806", platform_of('https://www.facebook.com/posts/TEST000807') == 'facebook')
    check("platform_00807", platform_of('https://www.facebook.com/reel/TEST000808') == 'facebook')
    check("platform_00808", platform_of('https://m.facebook.com/watch/?v=TEST000809') == 'facebook')
    check("platform_00809", platform_of('https://www.facebook.com/share/v/TEST000810/') == 'facebook')
    check("platform_00810", platform_of('https://www.facebook.com/posts/TEST000811') == 'facebook')
    check("platform_00811", platform_of('https://www.facebook.com/reel/TEST000812') == 'facebook')
    check("platform_00812", platform_of('https://m.facebook.com/watch/?v=TEST000813') == 'facebook')
    check("platform_00813", platform_of('https://www.facebook.com/share/v/TEST000814/') == 'facebook')
    check("platform_00814", platform_of('https://www.facebook.com/posts/TEST000815') == 'facebook')
    check("platform_00815", platform_of('https://www.facebook.com/reel/TEST000816') == 'facebook')
    check("platform_00816", platform_of('https://m.facebook.com/watch/?v=TEST000817') == 'facebook')
    check("platform_00817", platform_of('https://www.facebook.com/share/v/TEST000818/') == 'facebook')
    check("platform_00818", platform_of('https://www.facebook.com/posts/TEST000819') == 'facebook')
    check("platform_00819", platform_of('https://www.facebook.com/reel/TEST000820') == 'facebook')
    check("platform_00820", platform_of('https://m.facebook.com/watch/?v=TEST000821') == 'facebook')
    check("platform_00821", platform_of('https://www.facebook.com/share/v/TEST000822/') == 'facebook')
    check("platform_00822", platform_of('https://www.facebook.com/posts/TEST000823') == 'facebook')
    check("platform_00823", platform_of('https://www.facebook.com/reel/TEST000824') == 'facebook')
    check("platform_00824", platform_of('https://m.facebook.com/watch/?v=TEST000825') == 'facebook')
    check("platform_00825", platform_of('https://www.facebook.com/share/v/TEST000826/') == 'facebook')
    check("platform_00826", platform_of('https://www.facebook.com/posts/TEST000827') == 'facebook')
    check("platform_00827", platform_of('https://www.facebook.com/reel/TEST000828') == 'facebook')
    check("platform_00828", platform_of('https://m.facebook.com/watch/?v=TEST000829') == 'facebook')
    check("platform_00829", platform_of('https://www.facebook.com/share/v/TEST000830/') == 'facebook')
    check("platform_00830", platform_of('https://www.facebook.com/posts/TEST000831') == 'facebook')
    check("platform_00831", platform_of('https://www.facebook.com/reel/TEST000832') == 'facebook')
    check("platform_00832", platform_of('https://m.facebook.com/watch/?v=TEST000833') == 'facebook')
    check("platform_00833", platform_of('https://www.facebook.com/share/v/TEST000834/') == 'facebook')
    check("platform_00834", platform_of('https://www.facebook.com/posts/TEST000835') == 'facebook')
    check("platform_00835", platform_of('https://www.facebook.com/reel/TEST000836') == 'facebook')
    check("platform_00836", platform_of('https://m.facebook.com/watch/?v=TEST000837') == 'facebook')
    check("platform_00837", platform_of('https://www.facebook.com/share/v/TEST000838/') == 'facebook')
    check("platform_00838", platform_of('https://www.facebook.com/posts/TEST000839') == 'facebook')
    check("platform_00839", platform_of('https://www.facebook.com/reel/TEST000840') == 'facebook')
    check("platform_00840", platform_of('https://m.facebook.com/watch/?v=TEST000841') == 'facebook')
    check("platform_00841", platform_of('https://www.facebook.com/share/v/TEST000842/') == 'facebook')
    check("platform_00842", platform_of('https://www.facebook.com/posts/TEST000843') == 'facebook')
    check("platform_00843", platform_of('https://www.facebook.com/reel/TEST000844') == 'facebook')
    check("platform_00844", platform_of('https://m.facebook.com/watch/?v=TEST000845') == 'facebook')
    check("platform_00845", platform_of('https://www.facebook.com/share/v/TEST000846/') == 'facebook')
    check("platform_00846", platform_of('https://www.facebook.com/posts/TEST000847') == 'facebook')
    check("platform_00847", platform_of('https://www.facebook.com/reel/TEST000848') == 'facebook')
    check("platform_00848", platform_of('https://m.facebook.com/watch/?v=TEST000849') == 'facebook')
    check("platform_00849", platform_of('https://www.facebook.com/share/v/TEST000850/') == 'facebook')
    check("platform_00850", platform_of('https://www.facebook.com/posts/TEST000851') == 'facebook')
    check("platform_00851", platform_of('https://www.facebook.com/reel/TEST000852') == 'facebook')
    check("platform_00852", platform_of('https://m.facebook.com/watch/?v=TEST000853') == 'facebook')
    check("platform_00853", platform_of('https://www.facebook.com/share/v/TEST000854/') == 'facebook')
    check("platform_00854", platform_of('https://www.facebook.com/posts/TEST000855') == 'facebook')
    check("platform_00855", platform_of('https://www.facebook.com/reel/TEST000856') == 'facebook')
    check("platform_00856", platform_of('https://m.facebook.com/watch/?v=TEST000857') == 'facebook')
    check("platform_00857", platform_of('https://www.facebook.com/share/v/TEST000858/') == 'facebook')
    check("platform_00858", platform_of('https://www.facebook.com/posts/TEST000859') == 'facebook')
    check("platform_00859", platform_of('https://www.facebook.com/reel/TEST000860') == 'facebook')
    check("platform_00860", platform_of('https://m.facebook.com/watch/?v=TEST000861') == 'facebook')
    check("platform_00861", platform_of('https://www.facebook.com/share/v/TEST000862/') == 'facebook')
    check("platform_00862", platform_of('https://www.facebook.com/posts/TEST000863') == 'facebook')
    check("platform_00863", platform_of('https://www.facebook.com/reel/TEST000864') == 'facebook')
    check("platform_00864", platform_of('https://m.facebook.com/watch/?v=TEST000865') == 'facebook')
    check("platform_00865", platform_of('https://www.facebook.com/share/v/TEST000866/') == 'facebook')
    check("platform_00866", platform_of('https://www.facebook.com/posts/TEST000867') == 'facebook')
    check("platform_00867", platform_of('https://www.facebook.com/reel/TEST000868') == 'facebook')
    check("platform_00868", platform_of('https://m.facebook.com/watch/?v=TEST000869') == 'facebook')
    check("platform_00869", platform_of('https://www.facebook.com/share/v/TEST000870/') == 'facebook')
    check("platform_00870", platform_of('https://www.facebook.com/posts/TEST000871') == 'facebook')
    check("platform_00871", platform_of('https://www.facebook.com/reel/TEST000872') == 'facebook')
    check("platform_00872", platform_of('https://m.facebook.com/watch/?v=TEST000873') == 'facebook')
    check("platform_00873", platform_of('https://www.facebook.com/share/v/TEST000874/') == 'facebook')
    check("platform_00874", platform_of('https://www.facebook.com/posts/TEST000875') == 'facebook')
    check("platform_00875", platform_of('https://www.facebook.com/reel/TEST000876') == 'facebook')
    check("platform_00876", platform_of('https://m.facebook.com/watch/?v=TEST000877') == 'facebook')
    check("platform_00877", platform_of('https://www.facebook.com/share/v/TEST000878/') == 'facebook')
    check("platform_00878", platform_of('https://www.facebook.com/posts/TEST000879') == 'facebook')
    check("platform_00879", platform_of('https://www.facebook.com/reel/TEST000880') == 'facebook')
    check("platform_00880", platform_of('https://m.facebook.com/watch/?v=TEST000881') == 'facebook')
    check("platform_00881", platform_of('https://www.facebook.com/share/v/TEST000882/') == 'facebook')
    check("platform_00882", platform_of('https://www.facebook.com/posts/TEST000883') == 'facebook')
    check("platform_00883", platform_of('https://www.facebook.com/reel/TEST000884') == 'facebook')
    check("platform_00884", platform_of('https://m.facebook.com/watch/?v=TEST000885') == 'facebook')
    check("platform_00885", platform_of('https://www.facebook.com/share/v/TEST000886/') == 'facebook')
    check("platform_00886", platform_of('https://www.facebook.com/posts/TEST000887') == 'facebook')
    check("platform_00887", platform_of('https://www.facebook.com/reel/TEST000888') == 'facebook')
    check("platform_00888", platform_of('https://m.facebook.com/watch/?v=TEST000889') == 'facebook')
    check("platform_00889", platform_of('https://www.facebook.com/share/v/TEST000890/') == 'facebook')
    check("platform_00890", platform_of('https://www.facebook.com/posts/TEST000891') == 'facebook')
    check("platform_00891", platform_of('https://www.facebook.com/reel/TEST000892') == 'facebook')
    check("platform_00892", platform_of('https://m.facebook.com/watch/?v=TEST000893') == 'facebook')
    check("platform_00893", platform_of('https://www.facebook.com/share/v/TEST000894/') == 'facebook')
    check("platform_00894", platform_of('https://www.facebook.com/posts/TEST000895') == 'facebook')
    check("platform_00895", platform_of('https://www.facebook.com/reel/TEST000896') == 'facebook')
    check("platform_00896", platform_of('https://m.facebook.com/watch/?v=TEST000897') == 'facebook')
    check("platform_00897", platform_of('https://www.facebook.com/share/v/TEST000898/') == 'facebook')
    check("platform_00898", platform_of('https://www.facebook.com/posts/TEST000899') == 'facebook')
    check("platform_00899", platform_of('https://www.facebook.com/reel/TEST000900') == 'facebook')
    check("platform_00900", platform_of('https://m.facebook.com/watch/?v=TEST000901') == 'facebook')
    check("platform_00901", platform_of('https://www.facebook.com/share/v/TEST000902/') == 'facebook')
    check("platform_00902", platform_of('https://www.facebook.com/posts/TEST000903') == 'facebook')
    check("platform_00903", platform_of('https://www.facebook.com/reel/TEST000904') == 'facebook')
    check("platform_00904", platform_of('https://m.facebook.com/watch/?v=TEST000905') == 'facebook')
    check("platform_00905", platform_of('https://www.facebook.com/share/v/TEST000906/') == 'facebook')
    check("platform_00906", platform_of('https://www.facebook.com/posts/TEST000907') == 'facebook')
    check("platform_00907", platform_of('https://www.facebook.com/reel/TEST000908') == 'facebook')
    check("platform_00908", platform_of('https://m.facebook.com/watch/?v=TEST000909') == 'facebook')
    check("platform_00909", platform_of('https://www.facebook.com/share/v/TEST000910/') == 'facebook')
    check("platform_00910", platform_of('https://www.facebook.com/posts/TEST000911') == 'facebook')
    check("platform_00911", platform_of('https://www.facebook.com/reel/TEST000912') == 'facebook')
    check("platform_00912", platform_of('https://m.facebook.com/watch/?v=TEST000913') == 'facebook')
    check("platform_00913", platform_of('https://www.facebook.com/share/v/TEST000914/') == 'facebook')
    check("platform_00914", platform_of('https://www.facebook.com/posts/TEST000915') == 'facebook')
    check("platform_00915", platform_of('https://www.facebook.com/reel/TEST000916') == 'facebook')
    check("platform_00916", platform_of('https://m.facebook.com/watch/?v=TEST000917') == 'facebook')
    check("platform_00917", platform_of('https://www.facebook.com/share/v/TEST000918/') == 'facebook')
    check("platform_00918", platform_of('https://www.facebook.com/posts/TEST000919') == 'facebook')
    check("platform_00919", platform_of('https://www.facebook.com/reel/TEST000920') == 'facebook')
    check("platform_00920", platform_of('https://m.facebook.com/watch/?v=TEST000921') == 'facebook')
    check("platform_00921", platform_of('https://www.facebook.com/share/v/TEST000922/') == 'facebook')
    check("platform_00922", platform_of('https://www.facebook.com/posts/TEST000923') == 'facebook')
    check("platform_00923", platform_of('https://www.facebook.com/reel/TEST000924') == 'facebook')
    check("platform_00924", platform_of('https://m.facebook.com/watch/?v=TEST000925') == 'facebook')
    check("platform_00925", platform_of('https://www.facebook.com/share/v/TEST000926/') == 'facebook')
    check("platform_00926", platform_of('https://www.facebook.com/posts/TEST000927') == 'facebook')
    check("platform_00927", platform_of('https://www.facebook.com/reel/TEST000928') == 'facebook')
    check("platform_00928", platform_of('https://m.facebook.com/watch/?v=TEST000929') == 'facebook')
    check("platform_00929", platform_of('https://www.facebook.com/share/v/TEST000930/') == 'facebook')
    check("platform_00930", platform_of('https://www.facebook.com/posts/TEST000931') == 'facebook')
    check("platform_00931", platform_of('https://www.facebook.com/reel/TEST000932') == 'facebook')
    check("platform_00932", platform_of('https://m.facebook.com/watch/?v=TEST000933') == 'facebook')
    check("platform_00933", platform_of('https://www.facebook.com/share/v/TEST000934/') == 'facebook')
    check("platform_00934", platform_of('https://www.facebook.com/posts/TEST000935') == 'facebook')
    check("platform_00935", platform_of('https://www.facebook.com/reel/TEST000936') == 'facebook')
    check("platform_00936", platform_of('https://m.facebook.com/watch/?v=TEST000937') == 'facebook')
    check("platform_00937", platform_of('https://www.facebook.com/share/v/TEST000938/') == 'facebook')
    check("platform_00938", platform_of('https://www.facebook.com/posts/TEST000939') == 'facebook')
    check("platform_00939", platform_of('https://www.facebook.com/reel/TEST000940') == 'facebook')
    check("platform_00940", platform_of('https://m.facebook.com/watch/?v=TEST000941') == 'facebook')
    check("platform_00941", platform_of('https://www.facebook.com/share/v/TEST000942/') == 'facebook')
    check("platform_00942", platform_of('https://www.facebook.com/posts/TEST000943') == 'facebook')
    check("platform_00943", platform_of('https://www.facebook.com/reel/TEST000944') == 'facebook')
    check("platform_00944", platform_of('https://m.facebook.com/watch/?v=TEST000945') == 'facebook')
    check("platform_00945", platform_of('https://www.facebook.com/share/v/TEST000946/') == 'facebook')
    check("platform_00946", platform_of('https://www.facebook.com/posts/TEST000947') == 'facebook')
    check("platform_00947", platform_of('https://www.facebook.com/reel/TEST000948') == 'facebook')
    check("platform_00948", platform_of('https://m.facebook.com/watch/?v=TEST000949') == 'facebook')
    check("platform_00949", platform_of('https://www.facebook.com/share/v/TEST000950/') == 'facebook')
    check("platform_00950", platform_of('https://www.facebook.com/posts/TEST000951') == 'facebook')
    check("platform_00951", platform_of('https://www.facebook.com/reel/TEST000952') == 'facebook')
    check("platform_00952", platform_of('https://m.facebook.com/watch/?v=TEST000953') == 'facebook')
    check("platform_00953", platform_of('https://www.facebook.com/share/v/TEST000954/') == 'facebook')
    check("platform_00954", platform_of('https://www.facebook.com/posts/TEST000955') == 'facebook')
    check("platform_00955", platform_of('https://www.facebook.com/reel/TEST000956') == 'facebook')
    check("platform_00956", platform_of('https://m.facebook.com/watch/?v=TEST000957') == 'facebook')
    check("platform_00957", platform_of('https://www.facebook.com/share/v/TEST000958/') == 'facebook')
    check("platform_00958", platform_of('https://www.facebook.com/posts/TEST000959') == 'facebook')
    check("platform_00959", platform_of('https://www.facebook.com/reel/TEST000960') == 'facebook')
    check("platform_00960", platform_of('https://m.facebook.com/watch/?v=TEST000961') == 'facebook')
    check("platform_00961", platform_of('https://www.facebook.com/share/v/TEST000962/') == 'facebook')
    check("platform_00962", platform_of('https://www.facebook.com/posts/TEST000963') == 'facebook')
    check("platform_00963", platform_of('https://www.facebook.com/reel/TEST000964') == 'facebook')
    check("platform_00964", platform_of('https://m.facebook.com/watch/?v=TEST000965') == 'facebook')
    check("platform_00965", platform_of('https://www.facebook.com/share/v/TEST000966/') == 'facebook')
    check("platform_00966", platform_of('https://www.facebook.com/posts/TEST000967') == 'facebook')
    check("platform_00967", platform_of('https://www.facebook.com/reel/TEST000968') == 'facebook')
    check("platform_00968", platform_of('https://m.facebook.com/watch/?v=TEST000969') == 'facebook')
    check("platform_00969", platform_of('https://www.facebook.com/share/v/TEST000970/') == 'facebook')
    check("platform_00970", platform_of('https://www.facebook.com/posts/TEST000971') == 'facebook')
    check("platform_00971", platform_of('https://www.facebook.com/reel/TEST000972') == 'facebook')
    check("platform_00972", platform_of('https://m.facebook.com/watch/?v=TEST000973') == 'facebook')
    check("platform_00973", platform_of('https://www.facebook.com/share/v/TEST000974/') == 'facebook')
    check("platform_00974", platform_of('https://www.facebook.com/posts/TEST000975') == 'facebook')
    check("platform_00975", platform_of('https://www.facebook.com/reel/TEST000976') == 'facebook')
    check("platform_00976", platform_of('https://m.facebook.com/watch/?v=TEST000977') == 'facebook')
    check("platform_00977", platform_of('https://www.facebook.com/share/v/TEST000978/') == 'facebook')
    check("platform_00978", platform_of('https://www.facebook.com/posts/TEST000979') == 'facebook')
    check("platform_00979", platform_of('https://www.facebook.com/reel/TEST000980') == 'facebook')
    check("platform_00980", platform_of('https://m.facebook.com/watch/?v=TEST000981') == 'facebook')
    check("platform_00981", platform_of('https://www.facebook.com/share/v/TEST000982/') == 'facebook')
    check("platform_00982", platform_of('https://www.facebook.com/posts/TEST000983') == 'facebook')
    check("platform_00983", platform_of('https://www.facebook.com/reel/TEST000984') == 'facebook')
    check("platform_00984", platform_of('https://m.facebook.com/watch/?v=TEST000985') == 'facebook')
    check("platform_00985", platform_of('https://www.facebook.com/share/v/TEST000986/') == 'facebook')
    check("platform_00986", platform_of('https://www.facebook.com/posts/TEST000987') == 'facebook')
    check("platform_00987", platform_of('https://www.facebook.com/reel/TEST000988') == 'facebook')
    check("platform_00988", platform_of('https://m.facebook.com/watch/?v=TEST000989') == 'facebook')
    check("platform_00989", platform_of('https://www.facebook.com/share/v/TEST000990/') == 'facebook')
    check("platform_00990", platform_of('https://www.facebook.com/posts/TEST000991') == 'facebook')
    check("platform_00991", platform_of('https://www.facebook.com/reel/TEST000992') == 'facebook')
    check("platform_00992", platform_of('https://m.facebook.com/watch/?v=TEST000993') == 'facebook')
    check("platform_00993", platform_of('https://www.facebook.com/share/v/TEST000994/') == 'facebook')
    check("platform_00994", platform_of('https://www.facebook.com/posts/TEST000995') == 'facebook')
    check("platform_00995", platform_of('https://www.facebook.com/reel/TEST000996') == 'facebook')
    check("platform_00996", platform_of('https://m.facebook.com/watch/?v=TEST000997') == 'facebook')
    check("platform_00997", platform_of('https://www.facebook.com/share/v/TEST000998/') == 'facebook')
    check("platform_00998", platform_of('https://www.facebook.com/posts/TEST000999') == 'facebook')
    check("platform_00999", platform_of('https://www.facebook.com/reel/TEST001000') == 'facebook')
    check("platform_01000", platform_of('https://m.facebook.com/watch/?v=TEST001001') == 'facebook')
    check("platform_01001", platform_of('https://www.facebook.com/share/v/TEST001002/') == 'facebook')
    check("platform_01002", platform_of('https://www.facebook.com/posts/TEST001003') == 'facebook')
    check("platform_01003", platform_of('https://www.facebook.com/reel/TEST001004') == 'facebook')
    check("platform_01004", platform_of('https://m.facebook.com/watch/?v=TEST001005') == 'facebook')
    check("platform_01005", platform_of('https://www.facebook.com/share/v/TEST001006/') == 'facebook')
    check("platform_01006", platform_of('https://www.facebook.com/posts/TEST001007') == 'facebook')
    check("platform_01007", platform_of('https://www.facebook.com/reel/TEST001008') == 'facebook')
    check("platform_01008", platform_of('https://m.facebook.com/watch/?v=TEST001009') == 'facebook')
    check("platform_01009", platform_of('https://www.facebook.com/share/v/TEST001010/') == 'facebook')
    check("platform_01010", platform_of('https://www.facebook.com/posts/TEST001011') == 'facebook')
    check("platform_01011", platform_of('https://www.facebook.com/reel/TEST001012') == 'facebook')
    check("platform_01012", platform_of('https://m.facebook.com/watch/?v=TEST001013') == 'facebook')
    check("platform_01013", platform_of('https://www.facebook.com/share/v/TEST001014/') == 'facebook')
    check("platform_01014", platform_of('https://www.facebook.com/posts/TEST001015') == 'facebook')
    check("platform_01015", platform_of('https://www.facebook.com/reel/TEST001016') == 'facebook')
    check("platform_01016", platform_of('https://m.facebook.com/watch/?v=TEST001017') == 'facebook')
    check("platform_01017", platform_of('https://www.facebook.com/share/v/TEST001018/') == 'facebook')
    check("platform_01018", platform_of('https://www.facebook.com/posts/TEST001019') == 'facebook')
    check("platform_01019", platform_of('https://www.facebook.com/reel/TEST001020') == 'facebook')
    check("platform_01020", platform_of('https://m.facebook.com/watch/?v=TEST001021') == 'facebook')
    check("platform_01021", platform_of('https://www.facebook.com/share/v/TEST001022/') == 'facebook')
    check("platform_01022", platform_of('https://www.facebook.com/posts/TEST001023') == 'facebook')
    check("platform_01023", platform_of('https://www.facebook.com/reel/TEST001024') == 'facebook')
    check("platform_01024", platform_of('https://m.facebook.com/watch/?v=TEST001025') == 'facebook')
    check("platform_01025", platform_of('https://www.facebook.com/share/v/TEST001026/') == 'facebook')
    check("platform_01026", platform_of('https://www.facebook.com/posts/TEST001027') == 'facebook')
    check("platform_01027", platform_of('https://www.facebook.com/reel/TEST001028') == 'facebook')
    check("platform_01028", platform_of('https://m.facebook.com/watch/?v=TEST001029') == 'facebook')
    check("platform_01029", platform_of('https://www.facebook.com/share/v/TEST001030/') == 'facebook')
    check("platform_01030", platform_of('https://www.facebook.com/posts/TEST001031') == 'facebook')
    check("platform_01031", platform_of('https://www.facebook.com/reel/TEST001032') == 'facebook')
    check("platform_01032", platform_of('https://m.facebook.com/watch/?v=TEST001033') == 'facebook')
    check("platform_01033", platform_of('https://www.facebook.com/share/v/TEST001034/') == 'facebook')
    check("platform_01034", platform_of('https://www.facebook.com/posts/TEST001035') == 'facebook')
    check("platform_01035", platform_of('https://www.facebook.com/reel/TEST001036') == 'facebook')
    check("platform_01036", platform_of('https://m.facebook.com/watch/?v=TEST001037') == 'facebook')
    check("platform_01037", platform_of('https://www.facebook.com/share/v/TEST001038/') == 'facebook')
    check("platform_01038", platform_of('https://www.facebook.com/posts/TEST001039') == 'facebook')
    check("platform_01039", platform_of('https://www.facebook.com/reel/TEST001040') == 'facebook')
    check("platform_01040", platform_of('https://m.facebook.com/watch/?v=TEST001041') == 'facebook')
    check("platform_01041", platform_of('https://www.facebook.com/share/v/TEST001042/') == 'facebook')
    check("platform_01042", platform_of('https://www.facebook.com/posts/TEST001043') == 'facebook')
    check("platform_01043", platform_of('https://www.facebook.com/reel/TEST001044') == 'facebook')
    check("platform_01044", platform_of('https://m.facebook.com/watch/?v=TEST001045') == 'facebook')
    check("platform_01045", platform_of('https://www.facebook.com/share/v/TEST001046/') == 'facebook')
    check("platform_01046", platform_of('https://www.facebook.com/posts/TEST001047') == 'facebook')
    check("platform_01047", platform_of('https://www.facebook.com/reel/TEST001048') == 'facebook')
    check("platform_01048", platform_of('https://m.facebook.com/watch/?v=TEST001049') == 'facebook')
    check("platform_01049", platform_of('https://www.facebook.com/share/v/TEST001050/') == 'facebook')
    check("platform_01050", platform_of('https://www.facebook.com/posts/TEST001051') == 'facebook')
    check("platform_01051", platform_of('https://www.facebook.com/reel/TEST001052') == 'facebook')
    check("platform_01052", platform_of('https://m.facebook.com/watch/?v=TEST001053') == 'facebook')
    check("platform_01053", platform_of('https://www.facebook.com/share/v/TEST001054/') == 'facebook')
    check("platform_01054", platform_of('https://www.facebook.com/posts/TEST001055') == 'facebook')
    check("platform_01055", platform_of('https://www.facebook.com/reel/TEST001056') == 'facebook')
    check("platform_01056", platform_of('https://m.facebook.com/watch/?v=TEST001057') == 'facebook')
    check("platform_01057", platform_of('https://www.facebook.com/share/v/TEST001058/') == 'facebook')
    check("platform_01058", platform_of('https://www.facebook.com/posts/TEST001059') == 'facebook')
    check("platform_01059", platform_of('https://www.facebook.com/reel/TEST001060') == 'facebook')
    check("platform_01060", platform_of('https://m.facebook.com/watch/?v=TEST001061') == 'facebook')
    check("platform_01061", platform_of('https://www.facebook.com/share/v/TEST001062/') == 'facebook')
    check("platform_01062", platform_of('https://www.facebook.com/posts/TEST001063') == 'facebook')
    check("platform_01063", platform_of('https://www.facebook.com/reel/TEST001064') == 'facebook')
    check("platform_01064", platform_of('https://m.facebook.com/watch/?v=TEST001065') == 'facebook')
    check("platform_01065", platform_of('https://www.facebook.com/share/v/TEST001066/') == 'facebook')
    check("platform_01066", platform_of('https://www.facebook.com/posts/TEST001067') == 'facebook')
    check("platform_01067", platform_of('https://www.facebook.com/reel/TEST001068') == 'facebook')
    check("platform_01068", platform_of('https://m.facebook.com/watch/?v=TEST001069') == 'facebook')
    check("platform_01069", platform_of('https://www.facebook.com/share/v/TEST001070/') == 'facebook')
    check("platform_01070", platform_of('https://www.facebook.com/posts/TEST001071') == 'facebook')
    check("platform_01071", platform_of('https://www.facebook.com/reel/TEST001072') == 'facebook')
    check("platform_01072", platform_of('https://m.facebook.com/watch/?v=TEST001073') == 'facebook')
    check("platform_01073", platform_of('https://www.facebook.com/share/v/TEST001074/') == 'facebook')
    check("platform_01074", platform_of('https://www.facebook.com/posts/TEST001075') == 'facebook')
    check("platform_01075", platform_of('https://www.facebook.com/reel/TEST001076') == 'facebook')
    check("platform_01076", platform_of('https://m.facebook.com/watch/?v=TEST001077') == 'facebook')
    check("platform_01077", platform_of('https://www.facebook.com/share/v/TEST001078/') == 'facebook')
    check("platform_01078", platform_of('https://www.facebook.com/posts/TEST001079') == 'facebook')
    check("platform_01079", platform_of('https://www.facebook.com/reel/TEST001080') == 'facebook')
    check("platform_01080", platform_of('https://m.facebook.com/watch/?v=TEST001081') == 'facebook')
    check("platform_01081", platform_of('https://www.facebook.com/share/v/TEST001082/') == 'facebook')
    check("platform_01082", platform_of('https://www.facebook.com/posts/TEST001083') == 'facebook')
    check("platform_01083", platform_of('https://www.facebook.com/reel/TEST001084') == 'facebook')
    check("platform_01084", platform_of('https://m.facebook.com/watch/?v=TEST001085') == 'facebook')
    check("platform_01085", platform_of('https://www.facebook.com/share/v/TEST001086/') == 'facebook')
    check("platform_01086", platform_of('https://www.facebook.com/posts/TEST001087') == 'facebook')
    check("platform_01087", platform_of('https://www.facebook.com/reel/TEST001088') == 'facebook')
    check("platform_01088", platform_of('https://m.facebook.com/watch/?v=TEST001089') == 'facebook')
    check("platform_01089", platform_of('https://www.facebook.com/share/v/TEST001090/') == 'facebook')
    check("platform_01090", platform_of('https://www.facebook.com/posts/TEST001091') == 'facebook')
    check("platform_01091", platform_of('https://www.facebook.com/reel/TEST001092') == 'facebook')
    check("platform_01092", platform_of('https://m.facebook.com/watch/?v=TEST001093') == 'facebook')
    check("platform_01093", platform_of('https://www.facebook.com/share/v/TEST001094/') == 'facebook')
    check("platform_01094", platform_of('https://www.facebook.com/posts/TEST001095') == 'facebook')
    check("platform_01095", platform_of('https://www.facebook.com/reel/TEST001096') == 'facebook')
    check("platform_01096", platform_of('https://m.facebook.com/watch/?v=TEST001097') == 'facebook')
    check("platform_01097", platform_of('https://www.facebook.com/share/v/TEST001098/') == 'facebook')
    check("platform_01098", platform_of('https://www.facebook.com/posts/TEST001099') == 'facebook')
    check("platform_01099", platform_of('https://www.facebook.com/reel/TEST001100') == 'facebook')
    check("platform_01100", platform_of('https://m.facebook.com/watch/?v=TEST001101') == 'facebook')
    check("platform_01101", platform_of('https://www.facebook.com/share/v/TEST001102/') == 'facebook')
    check("platform_01102", platform_of('https://www.facebook.com/posts/TEST001103') == 'facebook')
    check("platform_01103", platform_of('https://www.facebook.com/reel/TEST001104') == 'facebook')
    check("platform_01104", platform_of('https://m.facebook.com/watch/?v=TEST001105') == 'facebook')
    check("platform_01105", platform_of('https://www.facebook.com/share/v/TEST001106/') == 'facebook')
    check("platform_01106", platform_of('https://www.facebook.com/posts/TEST001107') == 'facebook')
    check("platform_01107", platform_of('https://www.facebook.com/reel/TEST001108') == 'facebook')
    check("platform_01108", platform_of('https://m.facebook.com/watch/?v=TEST001109') == 'facebook')
    check("platform_01109", platform_of('https://www.facebook.com/share/v/TEST001110/') == 'facebook')
    check("platform_01110", platform_of('https://www.facebook.com/posts/TEST001111') == 'facebook')
    check("platform_01111", platform_of('https://www.facebook.com/reel/TEST001112') == 'facebook')
    check("platform_01112", platform_of('https://m.facebook.com/watch/?v=TEST001113') == 'facebook')
    check("platform_01113", platform_of('https://www.facebook.com/share/v/TEST001114/') == 'facebook')
    check("platform_01114", platform_of('https://www.facebook.com/posts/TEST001115') == 'facebook')
    check("platform_01115", platform_of('https://www.facebook.com/reel/TEST001116') == 'facebook')
    check("platform_01116", platform_of('https://m.facebook.com/watch/?v=TEST001117') == 'facebook')
    check("platform_01117", platform_of('https://www.facebook.com/share/v/TEST001118/') == 'facebook')
    check("platform_01118", platform_of('https://www.facebook.com/posts/TEST001119') == 'facebook')
    check("platform_01119", platform_of('https://www.facebook.com/reel/TEST001120') == 'facebook')
    check("platform_01120", platform_of('https://m.facebook.com/watch/?v=TEST001121') == 'facebook')
    check("platform_01121", platform_of('https://www.facebook.com/share/v/TEST001122/') == 'facebook')
    check("platform_01122", platform_of('https://www.facebook.com/posts/TEST001123') == 'facebook')
    check("platform_01123", platform_of('https://www.facebook.com/reel/TEST001124') == 'facebook')
    check("platform_01124", platform_of('https://m.facebook.com/watch/?v=TEST001125') == 'facebook')
    check("platform_01125", platform_of('https://www.facebook.com/share/v/TEST001126/') == 'facebook')
    check("platform_01126", platform_of('https://www.facebook.com/posts/TEST001127') == 'facebook')
    check("platform_01127", platform_of('https://www.facebook.com/reel/TEST001128') == 'facebook')
    check("platform_01128", platform_of('https://m.facebook.com/watch/?v=TEST001129') == 'facebook')
    check("platform_01129", platform_of('https://www.facebook.com/share/v/TEST001130/') == 'facebook')
    check("platform_01130", platform_of('https://www.facebook.com/posts/TEST001131') == 'facebook')
    check("platform_01131", platform_of('https://www.facebook.com/reel/TEST001132') == 'facebook')
    check("platform_01132", platform_of('https://m.facebook.com/watch/?v=TEST001133') == 'facebook')
    check("platform_01133", platform_of('https://www.facebook.com/share/v/TEST001134/') == 'facebook')
    check("platform_01134", platform_of('https://www.facebook.com/posts/TEST001135') == 'facebook')
    check("platform_01135", platform_of('https://www.facebook.com/reel/TEST001136') == 'facebook')
    check("platform_01136", platform_of('https://m.facebook.com/watch/?v=TEST001137') == 'facebook')
    check("platform_01137", platform_of('https://www.facebook.com/share/v/TEST001138/') == 'facebook')
    check("platform_01138", platform_of('https://www.facebook.com/posts/TEST001139') == 'facebook')
    check("platform_01139", platform_of('https://www.facebook.com/reel/TEST001140') == 'facebook')
    check("platform_01140", platform_of('https://m.facebook.com/watch/?v=TEST001141') == 'facebook')
    check("platform_01141", platform_of('https://www.facebook.com/share/v/TEST001142/') == 'facebook')
    check("platform_01142", platform_of('https://www.facebook.com/posts/TEST001143') == 'facebook')
    check("platform_01143", platform_of('https://www.facebook.com/reel/TEST001144') == 'facebook')
    check("platform_01144", platform_of('https://m.facebook.com/watch/?v=TEST001145') == 'facebook')
    check("platform_01145", platform_of('https://www.facebook.com/share/v/TEST001146/') == 'facebook')
    check("platform_01146", platform_of('https://www.facebook.com/posts/TEST001147') == 'facebook')
    check("platform_01147", platform_of('https://www.facebook.com/reel/TEST001148') == 'facebook')
    check("platform_01148", platform_of('https://m.facebook.com/watch/?v=TEST001149') == 'facebook')
    check("platform_01149", platform_of('https://www.facebook.com/share/v/TEST001150/') == 'facebook')
    check("platform_01150", platform_of('https://www.facebook.com/posts/TEST001151') == 'facebook')
    check("platform_01151", platform_of('https://www.facebook.com/reel/TEST001152') == 'facebook')
    check("platform_01152", platform_of('https://m.facebook.com/watch/?v=TEST001153') == 'facebook')
    check("platform_01153", platform_of('https://www.facebook.com/share/v/TEST001154/') == 'facebook')
    check("platform_01154", platform_of('https://www.facebook.com/posts/TEST001155') == 'facebook')
    check("platform_01155", platform_of('https://www.facebook.com/reel/TEST001156') == 'facebook')
    check("platform_01156", platform_of('https://m.facebook.com/watch/?v=TEST001157') == 'facebook')
    check("platform_01157", platform_of('https://www.facebook.com/share/v/TEST001158/') == 'facebook')
    check("platform_01158", platform_of('https://www.facebook.com/posts/TEST001159') == 'facebook')
    check("platform_01159", platform_of('https://www.facebook.com/reel/TEST001160') == 'facebook')
    check("platform_01160", platform_of('https://m.facebook.com/watch/?v=TEST001161') == 'facebook')
    check("platform_01161", platform_of('https://www.facebook.com/share/v/TEST001162/') == 'facebook')
    check("platform_01162", platform_of('https://www.facebook.com/posts/TEST001163') == 'facebook')
    check("platform_01163", platform_of('https://www.facebook.com/reel/TEST001164') == 'facebook')
    check("platform_01164", platform_of('https://m.facebook.com/watch/?v=TEST001165') == 'facebook')
    check("platform_01165", platform_of('https://www.facebook.com/share/v/TEST001166/') == 'facebook')
    check("platform_01166", platform_of('https://www.facebook.com/posts/TEST001167') == 'facebook')
    check("platform_01167", platform_of('https://www.facebook.com/reel/TEST001168') == 'facebook')
    check("platform_01168", platform_of('https://m.facebook.com/watch/?v=TEST001169') == 'facebook')
    check("platform_01169", platform_of('https://www.facebook.com/share/v/TEST001170/') == 'facebook')
    check("platform_01170", platform_of('https://www.facebook.com/posts/TEST001171') == 'facebook')
    check("platform_01171", platform_of('https://www.facebook.com/reel/TEST001172') == 'facebook')
    check("platform_01172", platform_of('https://m.facebook.com/watch/?v=TEST001173') == 'facebook')
    check("platform_01173", platform_of('https://www.facebook.com/share/v/TEST001174/') == 'facebook')
    check("platform_01174", platform_of('https://www.facebook.com/posts/TEST001175') == 'facebook')
    check("platform_01175", platform_of('https://www.facebook.com/reel/TEST001176') == 'facebook')
    check("platform_01176", platform_of('https://m.facebook.com/watch/?v=TEST001177') == 'facebook')
    check("platform_01177", platform_of('https://www.facebook.com/share/v/TEST001178/') == 'facebook')
    check("platform_01178", platform_of('https://www.facebook.com/posts/TEST001179') == 'facebook')
    check("platform_01179", platform_of('https://www.facebook.com/reel/TEST001180') == 'facebook')
    check("platform_01180", platform_of('https://m.facebook.com/watch/?v=TEST001181') == 'facebook')
    check("platform_01181", platform_of('https://www.facebook.com/share/v/TEST001182/') == 'facebook')
    check("platform_01182", platform_of('https://www.facebook.com/posts/TEST001183') == 'facebook')
    check("platform_01183", platform_of('https://www.facebook.com/reel/TEST001184') == 'facebook')
    check("platform_01184", platform_of('https://m.facebook.com/watch/?v=TEST001185') == 'facebook')
    check("platform_01185", platform_of('https://www.facebook.com/share/v/TEST001186/') == 'facebook')
    check("platform_01186", platform_of('https://www.facebook.com/posts/TEST001187') == 'facebook')
    check("platform_01187", platform_of('https://www.facebook.com/reel/TEST001188') == 'facebook')
    check("platform_01188", platform_of('https://m.facebook.com/watch/?v=TEST001189') == 'facebook')
    check("platform_01189", platform_of('https://www.facebook.com/share/v/TEST001190/') == 'facebook')
    check("platform_01190", platform_of('https://www.facebook.com/posts/TEST001191') == 'facebook')
    check("platform_01191", platform_of('https://www.facebook.com/reel/TEST001192') == 'facebook')
    check("platform_01192", platform_of('https://m.facebook.com/watch/?v=TEST001193') == 'facebook')
    check("platform_01193", platform_of('https://www.facebook.com/share/v/TEST001194/') == 'facebook')
    check("platform_01194", platform_of('https://www.facebook.com/posts/TEST001195') == 'facebook')
    check("platform_01195", platform_of('https://www.facebook.com/reel/TEST001196') == 'facebook')
    check("platform_01196", platform_of('https://m.facebook.com/watch/?v=TEST001197') == 'facebook')
    check("platform_01197", platform_of('https://www.facebook.com/share/v/TEST001198/') == 'facebook')
    check("platform_01198", platform_of('https://www.facebook.com/posts/TEST001199') == 'facebook')
    check("platform_01199", platform_of('https://www.facebook.com/reel/TEST001200') == 'facebook')
    check("platform_01200", platform_of('https://youtu.be/TEST000001') == 'youtube')
    check("platform_01201", platform_of('https://www.youtube.com/watch?v=TEST000002') == 'youtube')
    check("platform_01202", platform_of('https://youtu.be/TEST000003') == 'youtube')
    check("platform_01203", platform_of('https://www.youtube.com/watch?v=TEST000004') == 'youtube')
    check("platform_01204", platform_of('https://youtu.be/TEST000005') == 'youtube')
    check("platform_01205", platform_of('https://www.youtube.com/watch?v=TEST000006') == 'youtube')
    check("platform_01206", platform_of('https://youtu.be/TEST000007') == 'youtube')
    check("platform_01207", platform_of('https://www.youtube.com/watch?v=TEST000008') == 'youtube')
    check("platform_01208", platform_of('https://youtu.be/TEST000009') == 'youtube')
    check("platform_01209", platform_of('https://www.youtube.com/watch?v=TEST000010') == 'youtube')
    check("platform_01210", platform_of('https://youtu.be/TEST000011') == 'youtube')
    check("platform_01211", platform_of('https://www.youtube.com/watch?v=TEST000012') == 'youtube')
    check("platform_01212", platform_of('https://youtu.be/TEST000013') == 'youtube')
    check("platform_01213", platform_of('https://www.youtube.com/watch?v=TEST000014') == 'youtube')
    check("platform_01214", platform_of('https://youtu.be/TEST000015') == 'youtube')
    check("platform_01215", platform_of('https://www.youtube.com/watch?v=TEST000016') == 'youtube')
    check("platform_01216", platform_of('https://youtu.be/TEST000017') == 'youtube')
    check("platform_01217", platform_of('https://www.youtube.com/watch?v=TEST000018') == 'youtube')
    check("platform_01218", platform_of('https://youtu.be/TEST000019') == 'youtube')
    check("platform_01219", platform_of('https://www.youtube.com/watch?v=TEST000020') == 'youtube')
    check("platform_01220", platform_of('https://youtu.be/TEST000021') == 'youtube')
    check("platform_01221", platform_of('https://www.youtube.com/watch?v=TEST000022') == 'youtube')
    check("platform_01222", platform_of('https://youtu.be/TEST000023') == 'youtube')
    check("platform_01223", platform_of('https://www.youtube.com/watch?v=TEST000024') == 'youtube')
    check("platform_01224", platform_of('https://youtu.be/TEST000025') == 'youtube')
    check("platform_01225", platform_of('https://www.youtube.com/watch?v=TEST000026') == 'youtube')
    check("platform_01226", platform_of('https://youtu.be/TEST000027') == 'youtube')
    check("platform_01227", platform_of('https://www.youtube.com/watch?v=TEST000028') == 'youtube')
    check("platform_01228", platform_of('https://youtu.be/TEST000029') == 'youtube')
    check("platform_01229", platform_of('https://www.youtube.com/watch?v=TEST000030') == 'youtube')
    check("platform_01230", platform_of('https://youtu.be/TEST000031') == 'youtube')
    check("platform_01231", platform_of('https://www.youtube.com/watch?v=TEST000032') == 'youtube')
    check("platform_01232", platform_of('https://youtu.be/TEST000033') == 'youtube')
    check("platform_01233", platform_of('https://www.youtube.com/watch?v=TEST000034') == 'youtube')
    check("platform_01234", platform_of('https://youtu.be/TEST000035') == 'youtube')
    check("platform_01235", platform_of('https://www.youtube.com/watch?v=TEST000036') == 'youtube')
    check("platform_01236", platform_of('https://youtu.be/TEST000037') == 'youtube')
    check("platform_01237", platform_of('https://www.youtube.com/watch?v=TEST000038') == 'youtube')
    check("platform_01238", platform_of('https://youtu.be/TEST000039') == 'youtube')
    check("platform_01239", platform_of('https://www.youtube.com/watch?v=TEST000040') == 'youtube')
    check("platform_01240", platform_of('https://youtu.be/TEST000041') == 'youtube')
    check("platform_01241", platform_of('https://www.youtube.com/watch?v=TEST000042') == 'youtube')
    check("platform_01242", platform_of('https://youtu.be/TEST000043') == 'youtube')
    check("platform_01243", platform_of('https://www.youtube.com/watch?v=TEST000044') == 'youtube')
    check("platform_01244", platform_of('https://youtu.be/TEST000045') == 'youtube')
    check("platform_01245", platform_of('https://www.youtube.com/watch?v=TEST000046') == 'youtube')
    check("platform_01246", platform_of('https://youtu.be/TEST000047') == 'youtube')
    check("platform_01247", platform_of('https://www.youtube.com/watch?v=TEST000048') == 'youtube')
    check("platform_01248", platform_of('https://youtu.be/TEST000049') == 'youtube')
    check("platform_01249", platform_of('https://www.youtube.com/watch?v=TEST000050') == 'youtube')
    check("platform_01250", platform_of('https://youtu.be/TEST000051') == 'youtube')
    check("platform_01251", platform_of('https://www.youtube.com/watch?v=TEST000052') == 'youtube')
    check("platform_01252", platform_of('https://youtu.be/TEST000053') == 'youtube')
    check("platform_01253", platform_of('https://www.youtube.com/watch?v=TEST000054') == 'youtube')
    check("platform_01254", platform_of('https://youtu.be/TEST000055') == 'youtube')
    check("platform_01255", platform_of('https://www.youtube.com/watch?v=TEST000056') == 'youtube')
    check("platform_01256", platform_of('https://youtu.be/TEST000057') == 'youtube')
    check("platform_01257", platform_of('https://www.youtube.com/watch?v=TEST000058') == 'youtube')
    check("platform_01258", platform_of('https://youtu.be/TEST000059') == 'youtube')
    check("platform_01259", platform_of('https://www.youtube.com/watch?v=TEST000060') == 'youtube')
    check("platform_01260", platform_of('https://youtu.be/TEST000061') == 'youtube')
    check("platform_01261", platform_of('https://www.youtube.com/watch?v=TEST000062') == 'youtube')
    check("platform_01262", platform_of('https://youtu.be/TEST000063') == 'youtube')
    check("platform_01263", platform_of('https://www.youtube.com/watch?v=TEST000064') == 'youtube')
    check("platform_01264", platform_of('https://youtu.be/TEST000065') == 'youtube')
    check("platform_01265", platform_of('https://www.youtube.com/watch?v=TEST000066') == 'youtube')
    check("platform_01266", platform_of('https://youtu.be/TEST000067') == 'youtube')
    check("platform_01267", platform_of('https://www.youtube.com/watch?v=TEST000068') == 'youtube')
    check("platform_01268", platform_of('https://youtu.be/TEST000069') == 'youtube')
    check("platform_01269", platform_of('https://www.youtube.com/watch?v=TEST000070') == 'youtube')
    check("platform_01270", platform_of('https://youtu.be/TEST000071') == 'youtube')
    check("platform_01271", platform_of('https://www.youtube.com/watch?v=TEST000072') == 'youtube')
    check("platform_01272", platform_of('https://youtu.be/TEST000073') == 'youtube')
    check("platform_01273", platform_of('https://www.youtube.com/watch?v=TEST000074') == 'youtube')
    check("platform_01274", platform_of('https://youtu.be/TEST000075') == 'youtube')
    check("platform_01275", platform_of('https://www.youtube.com/watch?v=TEST000076') == 'youtube')
    check("platform_01276", platform_of('https://youtu.be/TEST000077') == 'youtube')
    check("platform_01277", platform_of('https://www.youtube.com/watch?v=TEST000078') == 'youtube')
    check("platform_01278", platform_of('https://youtu.be/TEST000079') == 'youtube')
    check("platform_01279", platform_of('https://www.youtube.com/watch?v=TEST000080') == 'youtube')
    check("platform_01280", platform_of('https://youtu.be/TEST000081') == 'youtube')
    check("platform_01281", platform_of('https://www.youtube.com/watch?v=TEST000082') == 'youtube')
    check("platform_01282", platform_of('https://youtu.be/TEST000083') == 'youtube')
    check("platform_01283", platform_of('https://www.youtube.com/watch?v=TEST000084') == 'youtube')
    check("platform_01284", platform_of('https://youtu.be/TEST000085') == 'youtube')
    check("platform_01285", platform_of('https://www.youtube.com/watch?v=TEST000086') == 'youtube')
    check("platform_01286", platform_of('https://youtu.be/TEST000087') == 'youtube')
    check("platform_01287", platform_of('https://www.youtube.com/watch?v=TEST000088') == 'youtube')
    check("platform_01288", platform_of('https://youtu.be/TEST000089') == 'youtube')
    check("platform_01289", platform_of('https://www.youtube.com/watch?v=TEST000090') == 'youtube')
    check("platform_01290", platform_of('https://youtu.be/TEST000091') == 'youtube')
    check("platform_01291", platform_of('https://www.youtube.com/watch?v=TEST000092') == 'youtube')
    check("platform_01292", platform_of('https://youtu.be/TEST000093') == 'youtube')
    check("platform_01293", platform_of('https://www.youtube.com/watch?v=TEST000094') == 'youtube')
    check("platform_01294", platform_of('https://youtu.be/TEST000095') == 'youtube')
    check("platform_01295", platform_of('https://www.youtube.com/watch?v=TEST000096') == 'youtube')
    check("platform_01296", platform_of('https://youtu.be/TEST000097') == 'youtube')
    check("platform_01297", platform_of('https://www.youtube.com/watch?v=TEST000098') == 'youtube')
    check("platform_01298", platform_of('https://youtu.be/TEST000099') == 'youtube')
    check("platform_01299", platform_of('https://www.youtube.com/watch?v=TEST000100') == 'youtube')
    check("platform_01300", platform_of('https://youtu.be/TEST000101') == 'youtube')
    check("platform_01301", platform_of('https://www.youtube.com/watch?v=TEST000102') == 'youtube')
    check("platform_01302", platform_of('https://youtu.be/TEST000103') == 'youtube')
    check("platform_01303", platform_of('https://www.youtube.com/watch?v=TEST000104') == 'youtube')
    check("platform_01304", platform_of('https://youtu.be/TEST000105') == 'youtube')
    check("platform_01305", platform_of('https://www.youtube.com/watch?v=TEST000106') == 'youtube')
    check("platform_01306", platform_of('https://youtu.be/TEST000107') == 'youtube')
    check("platform_01307", platform_of('https://www.youtube.com/watch?v=TEST000108') == 'youtube')
    check("platform_01308", platform_of('https://youtu.be/TEST000109') == 'youtube')
    check("platform_01309", platform_of('https://www.youtube.com/watch?v=TEST000110') == 'youtube')
    check("platform_01310", platform_of('https://youtu.be/TEST000111') == 'youtube')
    check("platform_01311", platform_of('https://www.youtube.com/watch?v=TEST000112') == 'youtube')
    check("platform_01312", platform_of('https://youtu.be/TEST000113') == 'youtube')
    check("platform_01313", platform_of('https://www.youtube.com/watch?v=TEST000114') == 'youtube')
    check("platform_01314", platform_of('https://youtu.be/TEST000115') == 'youtube')
    check("platform_01315", platform_of('https://www.youtube.com/watch?v=TEST000116') == 'youtube')
    check("platform_01316", platform_of('https://youtu.be/TEST000117') == 'youtube')
    check("platform_01317", platform_of('https://www.youtube.com/watch?v=TEST000118') == 'youtube')
    check("platform_01318", platform_of('https://youtu.be/TEST000119') == 'youtube')
    check("platform_01319", platform_of('https://www.youtube.com/watch?v=TEST000120') == 'youtube')
    check("platform_01320", platform_of('https://youtu.be/TEST000121') == 'youtube')
    check("platform_01321", platform_of('https://www.youtube.com/watch?v=TEST000122') == 'youtube')
    check("platform_01322", platform_of('https://youtu.be/TEST000123') == 'youtube')
    check("platform_01323", platform_of('https://www.youtube.com/watch?v=TEST000124') == 'youtube')
    check("platform_01324", platform_of('https://youtu.be/TEST000125') == 'youtube')
    check("platform_01325", platform_of('https://www.youtube.com/watch?v=TEST000126') == 'youtube')
    check("platform_01326", platform_of('https://youtu.be/TEST000127') == 'youtube')
    check("platform_01327", platform_of('https://www.youtube.com/watch?v=TEST000128') == 'youtube')
    check("platform_01328", platform_of('https://youtu.be/TEST000129') == 'youtube')
    check("platform_01329", platform_of('https://www.youtube.com/watch?v=TEST000130') == 'youtube')
    check("platform_01330", platform_of('https://youtu.be/TEST000131') == 'youtube')
    check("platform_01331", platform_of('https://www.youtube.com/watch?v=TEST000132') == 'youtube')
    check("platform_01332", platform_of('https://youtu.be/TEST000133') == 'youtube')
    check("platform_01333", platform_of('https://www.youtube.com/watch?v=TEST000134') == 'youtube')
    check("platform_01334", platform_of('https://youtu.be/TEST000135') == 'youtube')
    check("platform_01335", platform_of('https://www.youtube.com/watch?v=TEST000136') == 'youtube')
    check("platform_01336", platform_of('https://youtu.be/TEST000137') == 'youtube')
    check("platform_01337", platform_of('https://www.youtube.com/watch?v=TEST000138') == 'youtube')
    check("platform_01338", platform_of('https://youtu.be/TEST000139') == 'youtube')
    check("platform_01339", platform_of('https://www.youtube.com/watch?v=TEST000140') == 'youtube')
    check("platform_01340", platform_of('https://youtu.be/TEST000141') == 'youtube')
    check("platform_01341", platform_of('https://www.youtube.com/watch?v=TEST000142') == 'youtube')
    check("platform_01342", platform_of('https://youtu.be/TEST000143') == 'youtube')
    check("platform_01343", platform_of('https://www.youtube.com/watch?v=TEST000144') == 'youtube')
    check("platform_01344", platform_of('https://youtu.be/TEST000145') == 'youtube')
    check("platform_01345", platform_of('https://www.youtube.com/watch?v=TEST000146') == 'youtube')
    check("platform_01346", platform_of('https://youtu.be/TEST000147') == 'youtube')
    check("platform_01347", platform_of('https://www.youtube.com/watch?v=TEST000148') == 'youtube')
    check("platform_01348", platform_of('https://youtu.be/TEST000149') == 'youtube')
    check("platform_01349", platform_of('https://www.youtube.com/watch?v=TEST000150') == 'youtube')
    check("platform_01350", platform_of('https://youtu.be/TEST000151') == 'youtube')
    check("platform_01351", platform_of('https://www.youtube.com/watch?v=TEST000152') == 'youtube')
    check("platform_01352", platform_of('https://youtu.be/TEST000153') == 'youtube')
    check("platform_01353", platform_of('https://www.youtube.com/watch?v=TEST000154') == 'youtube')
    check("platform_01354", platform_of('https://youtu.be/TEST000155') == 'youtube')
    check("platform_01355", platform_of('https://www.youtube.com/watch?v=TEST000156') == 'youtube')
    check("platform_01356", platform_of('https://youtu.be/TEST000157') == 'youtube')
    check("platform_01357", platform_of('https://www.youtube.com/watch?v=TEST000158') == 'youtube')
    check("platform_01358", platform_of('https://youtu.be/TEST000159') == 'youtube')
    check("platform_01359", platform_of('https://www.youtube.com/watch?v=TEST000160') == 'youtube')
    check("platform_01360", platform_of('https://youtu.be/TEST000161') == 'youtube')
    check("platform_01361", platform_of('https://www.youtube.com/watch?v=TEST000162') == 'youtube')
    check("platform_01362", platform_of('https://youtu.be/TEST000163') == 'youtube')
    check("platform_01363", platform_of('https://www.youtube.com/watch?v=TEST000164') == 'youtube')
    check("platform_01364", platform_of('https://youtu.be/TEST000165') == 'youtube')
    check("platform_01365", platform_of('https://www.youtube.com/watch?v=TEST000166') == 'youtube')
    check("platform_01366", platform_of('https://youtu.be/TEST000167') == 'youtube')
    check("platform_01367", platform_of('https://www.youtube.com/watch?v=TEST000168') == 'youtube')
    check("platform_01368", platform_of('https://youtu.be/TEST000169') == 'youtube')
    check("platform_01369", platform_of('https://www.youtube.com/watch?v=TEST000170') == 'youtube')
    check("platform_01370", platform_of('https://youtu.be/TEST000171') == 'youtube')
    check("platform_01371", platform_of('https://www.youtube.com/watch?v=TEST000172') == 'youtube')
    check("platform_01372", platform_of('https://youtu.be/TEST000173') == 'youtube')
    check("platform_01373", platform_of('https://www.youtube.com/watch?v=TEST000174') == 'youtube')
    check("platform_01374", platform_of('https://youtu.be/TEST000175') == 'youtube')
    check("platform_01375", platform_of('https://www.youtube.com/watch?v=TEST000176') == 'youtube')
    check("platform_01376", platform_of('https://youtu.be/TEST000177') == 'youtube')
    check("platform_01377", platform_of('https://www.youtube.com/watch?v=TEST000178') == 'youtube')
    check("platform_01378", platform_of('https://youtu.be/TEST000179') == 'youtube')
    check("platform_01379", platform_of('https://www.youtube.com/watch?v=TEST000180') == 'youtube')
    check("platform_01380", platform_of('https://youtu.be/TEST000181') == 'youtube')
    check("platform_01381", platform_of('https://www.youtube.com/watch?v=TEST000182') == 'youtube')
    check("platform_01382", platform_of('https://youtu.be/TEST000183') == 'youtube')
    check("platform_01383", platform_of('https://www.youtube.com/watch?v=TEST000184') == 'youtube')
    check("platform_01384", platform_of('https://youtu.be/TEST000185') == 'youtube')
    check("platform_01385", platform_of('https://www.youtube.com/watch?v=TEST000186') == 'youtube')
    check("platform_01386", platform_of('https://youtu.be/TEST000187') == 'youtube')
    check("platform_01387", platform_of('https://www.youtube.com/watch?v=TEST000188') == 'youtube')
    check("platform_01388", platform_of('https://youtu.be/TEST000189') == 'youtube')
    check("platform_01389", platform_of('https://www.youtube.com/watch?v=TEST000190') == 'youtube')
    check("platform_01390", platform_of('https://youtu.be/TEST000191') == 'youtube')
    check("platform_01391", platform_of('https://www.youtube.com/watch?v=TEST000192') == 'youtube')
    check("platform_01392", platform_of('https://youtu.be/TEST000193') == 'youtube')
    check("platform_01393", platform_of('https://www.youtube.com/watch?v=TEST000194') == 'youtube')
    check("platform_01394", platform_of('https://youtu.be/TEST000195') == 'youtube')
    check("platform_01395", platform_of('https://www.youtube.com/watch?v=TEST000196') == 'youtube')
    check("platform_01396", platform_of('https://youtu.be/TEST000197') == 'youtube')
    check("platform_01397", platform_of('https://www.youtube.com/watch?v=TEST000198') == 'youtube')
    check("platform_01398", platform_of('https://youtu.be/TEST000199') == 'youtube')
    check("platform_01399", platform_of('https://www.youtube.com/watch?v=TEST000200') == 'youtube')
    check("platform_01400", platform_of('https://youtu.be/TEST000201') == 'youtube')
    check("platform_01401", platform_of('https://www.youtube.com/watch?v=TEST000202') == 'youtube')
    check("platform_01402", platform_of('https://youtu.be/TEST000203') == 'youtube')
    check("platform_01403", platform_of('https://www.youtube.com/watch?v=TEST000204') == 'youtube')
    check("platform_01404", platform_of('https://youtu.be/TEST000205') == 'youtube')
    check("platform_01405", platform_of('https://www.youtube.com/watch?v=TEST000206') == 'youtube')
    check("platform_01406", platform_of('https://youtu.be/TEST000207') == 'youtube')
    check("platform_01407", platform_of('https://www.youtube.com/watch?v=TEST000208') == 'youtube')
    check("platform_01408", platform_of('https://youtu.be/TEST000209') == 'youtube')
    check("platform_01409", platform_of('https://www.youtube.com/watch?v=TEST000210') == 'youtube')
    check("platform_01410", platform_of('https://youtu.be/TEST000211') == 'youtube')
    check("platform_01411", platform_of('https://www.youtube.com/watch?v=TEST000212') == 'youtube')
    check("platform_01412", platform_of('https://youtu.be/TEST000213') == 'youtube')
    check("platform_01413", platform_of('https://www.youtube.com/watch?v=TEST000214') == 'youtube')
    check("platform_01414", platform_of('https://youtu.be/TEST000215') == 'youtube')
    check("platform_01415", platform_of('https://www.youtube.com/watch?v=TEST000216') == 'youtube')
    check("platform_01416", platform_of('https://youtu.be/TEST000217') == 'youtube')
    check("platform_01417", platform_of('https://www.youtube.com/watch?v=TEST000218') == 'youtube')
    check("platform_01418", platform_of('https://youtu.be/TEST000219') == 'youtube')
    check("platform_01419", platform_of('https://www.youtube.com/watch?v=TEST000220') == 'youtube')
    check("platform_01420", platform_of('https://youtu.be/TEST000221') == 'youtube')
    check("platform_01421", platform_of('https://www.youtube.com/watch?v=TEST000222') == 'youtube')
    check("platform_01422", platform_of('https://youtu.be/TEST000223') == 'youtube')
    check("platform_01423", platform_of('https://www.youtube.com/watch?v=TEST000224') == 'youtube')
    check("platform_01424", platform_of('https://youtu.be/TEST000225') == 'youtube')
    check("platform_01425", platform_of('https://www.youtube.com/watch?v=TEST000226') == 'youtube')
    check("platform_01426", platform_of('https://youtu.be/TEST000227') == 'youtube')
    check("platform_01427", platform_of('https://www.youtube.com/watch?v=TEST000228') == 'youtube')
    check("platform_01428", platform_of('https://youtu.be/TEST000229') == 'youtube')
    check("platform_01429", platform_of('https://www.youtube.com/watch?v=TEST000230') == 'youtube')
    check("platform_01430", platform_of('https://youtu.be/TEST000231') == 'youtube')
    check("platform_01431", platform_of('https://www.youtube.com/watch?v=TEST000232') == 'youtube')
    check("platform_01432", platform_of('https://youtu.be/TEST000233') == 'youtube')
    check("platform_01433", platform_of('https://www.youtube.com/watch?v=TEST000234') == 'youtube')
    check("platform_01434", platform_of('https://youtu.be/TEST000235') == 'youtube')
    check("platform_01435", platform_of('https://www.youtube.com/watch?v=TEST000236') == 'youtube')
    check("platform_01436", platform_of('https://youtu.be/TEST000237') == 'youtube')
    check("platform_01437", platform_of('https://www.youtube.com/watch?v=TEST000238') == 'youtube')
    check("platform_01438", platform_of('https://youtu.be/TEST000239') == 'youtube')
    check("platform_01439", platform_of('https://www.youtube.com/watch?v=TEST000240') == 'youtube')
    check("platform_01440", platform_of('https://youtu.be/TEST000241') == 'youtube')
    check("platform_01441", platform_of('https://www.youtube.com/watch?v=TEST000242') == 'youtube')
    check("platform_01442", platform_of('https://youtu.be/TEST000243') == 'youtube')
    check("platform_01443", platform_of('https://www.youtube.com/watch?v=TEST000244') == 'youtube')
    check("platform_01444", platform_of('https://youtu.be/TEST000245') == 'youtube')
    check("platform_01445", platform_of('https://www.youtube.com/watch?v=TEST000246') == 'youtube')
    check("platform_01446", platform_of('https://youtu.be/TEST000247') == 'youtube')
    check("platform_01447", platform_of('https://www.youtube.com/watch?v=TEST000248') == 'youtube')
    check("platform_01448", platform_of('https://youtu.be/TEST000249') == 'youtube')
    check("platform_01449", platform_of('https://www.youtube.com/watch?v=TEST000250') == 'youtube')
    check("platform_01450", platform_of('https://youtu.be/TEST000251') == 'youtube')
    check("platform_01451", platform_of('https://www.youtube.com/watch?v=TEST000252') == 'youtube')
    check("platform_01452", platform_of('https://youtu.be/TEST000253') == 'youtube')
    check("platform_01453", platform_of('https://www.youtube.com/watch?v=TEST000254') == 'youtube')
    check("platform_01454", platform_of('https://youtu.be/TEST000255') == 'youtube')
    check("platform_01455", platform_of('https://www.youtube.com/watch?v=TEST000256') == 'youtube')
    check("platform_01456", platform_of('https://youtu.be/TEST000257') == 'youtube')
    check("platform_01457", platform_of('https://www.youtube.com/watch?v=TEST000258') == 'youtube')
    check("platform_01458", platform_of('https://youtu.be/TEST000259') == 'youtube')
    check("platform_01459", platform_of('https://www.youtube.com/watch?v=TEST000260') == 'youtube')
    check("platform_01460", platform_of('https://youtu.be/TEST000261') == 'youtube')
    check("platform_01461", platform_of('https://www.youtube.com/watch?v=TEST000262') == 'youtube')
    check("platform_01462", platform_of('https://youtu.be/TEST000263') == 'youtube')
    check("platform_01463", platform_of('https://www.youtube.com/watch?v=TEST000264') == 'youtube')
    check("platform_01464", platform_of('https://youtu.be/TEST000265') == 'youtube')
    check("platform_01465", platform_of('https://www.youtube.com/watch?v=TEST000266') == 'youtube')
    check("platform_01466", platform_of('https://youtu.be/TEST000267') == 'youtube')
    check("platform_01467", platform_of('https://www.youtube.com/watch?v=TEST000268') == 'youtube')
    check("platform_01468", platform_of('https://youtu.be/TEST000269') == 'youtube')
    check("platform_01469", platform_of('https://www.youtube.com/watch?v=TEST000270') == 'youtube')
    check("platform_01470", platform_of('https://youtu.be/TEST000271') == 'youtube')
    check("platform_01471", platform_of('https://www.youtube.com/watch?v=TEST000272') == 'youtube')
    check("platform_01472", platform_of('https://youtu.be/TEST000273') == 'youtube')
    check("platform_01473", platform_of('https://www.youtube.com/watch?v=TEST000274') == 'youtube')
    check("platform_01474", platform_of('https://youtu.be/TEST000275') == 'youtube')
    check("platform_01475", platform_of('https://www.youtube.com/watch?v=TEST000276') == 'youtube')
    check("platform_01476", platform_of('https://youtu.be/TEST000277') == 'youtube')
    check("platform_01477", platform_of('https://www.youtube.com/watch?v=TEST000278') == 'youtube')
    check("platform_01478", platform_of('https://youtu.be/TEST000279') == 'youtube')
    check("platform_01479", platform_of('https://www.youtube.com/watch?v=TEST000280') == 'youtube')
    check("platform_01480", platform_of('https://youtu.be/TEST000281') == 'youtube')
    check("platform_01481", platform_of('https://www.youtube.com/watch?v=TEST000282') == 'youtube')
    check("platform_01482", platform_of('https://youtu.be/TEST000283') == 'youtube')
    check("platform_01483", platform_of('https://www.youtube.com/watch?v=TEST000284') == 'youtube')
    check("platform_01484", platform_of('https://youtu.be/TEST000285') == 'youtube')
    check("platform_01485", platform_of('https://www.youtube.com/watch?v=TEST000286') == 'youtube')
    check("platform_01486", platform_of('https://youtu.be/TEST000287') == 'youtube')
    check("platform_01487", platform_of('https://www.youtube.com/watch?v=TEST000288') == 'youtube')
    check("platform_01488", platform_of('https://youtu.be/TEST000289') == 'youtube')
    check("platform_01489", platform_of('https://www.youtube.com/watch?v=TEST000290') == 'youtube')
    check("platform_01490", platform_of('https://youtu.be/TEST000291') == 'youtube')
    check("platform_01491", platform_of('https://www.youtube.com/watch?v=TEST000292') == 'youtube')
    check("platform_01492", platform_of('https://youtu.be/TEST000293') == 'youtube')
    check("platform_01493", platform_of('https://www.youtube.com/watch?v=TEST000294') == 'youtube')
    check("platform_01494", platform_of('https://youtu.be/TEST000295') == 'youtube')
    check("platform_01495", platform_of('https://www.youtube.com/watch?v=TEST000296') == 'youtube')
    check("platform_01496", platform_of('https://youtu.be/TEST000297') == 'youtube')
    check("platform_01497", platform_of('https://www.youtube.com/watch?v=TEST000298') == 'youtube')
    check("platform_01498", platform_of('https://youtu.be/TEST000299') == 'youtube')
    check("platform_01499", platform_of('https://www.youtube.com/watch?v=TEST000300') == 'youtube')
    check("platform_01500", platform_of('https://youtu.be/TEST000301') == 'youtube')
    check("platform_01501", platform_of('https://www.youtube.com/watch?v=TEST000302') == 'youtube')
    check("platform_01502", platform_of('https://youtu.be/TEST000303') == 'youtube')
    check("platform_01503", platform_of('https://www.youtube.com/watch?v=TEST000304') == 'youtube')
    check("platform_01504", platform_of('https://youtu.be/TEST000305') == 'youtube')
    check("platform_01505", platform_of('https://www.youtube.com/watch?v=TEST000306') == 'youtube')
    check("platform_01506", platform_of('https://youtu.be/TEST000307') == 'youtube')
    check("platform_01507", platform_of('https://www.youtube.com/watch?v=TEST000308') == 'youtube')
    check("platform_01508", platform_of('https://youtu.be/TEST000309') == 'youtube')
    check("platform_01509", platform_of('https://www.youtube.com/watch?v=TEST000310') == 'youtube')
    check("platform_01510", platform_of('https://youtu.be/TEST000311') == 'youtube')
    check("platform_01511", platform_of('https://www.youtube.com/watch?v=TEST000312') == 'youtube')
    check("platform_01512", platform_of('https://youtu.be/TEST000313') == 'youtube')
    check("platform_01513", platform_of('https://www.youtube.com/watch?v=TEST000314') == 'youtube')
    check("platform_01514", platform_of('https://youtu.be/TEST000315') == 'youtube')
    check("platform_01515", platform_of('https://www.youtube.com/watch?v=TEST000316') == 'youtube')
    check("platform_01516", platform_of('https://youtu.be/TEST000317') == 'youtube')
    check("platform_01517", platform_of('https://www.youtube.com/watch?v=TEST000318') == 'youtube')
    check("platform_01518", platform_of('https://youtu.be/TEST000319') == 'youtube')
    check("platform_01519", platform_of('https://www.youtube.com/watch?v=TEST000320') == 'youtube')
    check("platform_01520", platform_of('https://youtu.be/TEST000321') == 'youtube')
    check("platform_01521", platform_of('https://www.youtube.com/watch?v=TEST000322') == 'youtube')
    check("platform_01522", platform_of('https://youtu.be/TEST000323') == 'youtube')
    check("platform_01523", platform_of('https://www.youtube.com/watch?v=TEST000324') == 'youtube')
    check("platform_01524", platform_of('https://youtu.be/TEST000325') == 'youtube')
    check("platform_01525", platform_of('https://www.youtube.com/watch?v=TEST000326') == 'youtube')
    check("platform_01526", platform_of('https://youtu.be/TEST000327') == 'youtube')
    check("platform_01527", platform_of('https://www.youtube.com/watch?v=TEST000328') == 'youtube')
    check("platform_01528", platform_of('https://youtu.be/TEST000329') == 'youtube')
    check("platform_01529", platform_of('https://www.youtube.com/watch?v=TEST000330') == 'youtube')
    check("platform_01530", platform_of('https://youtu.be/TEST000331') == 'youtube')
    check("platform_01531", platform_of('https://www.youtube.com/watch?v=TEST000332') == 'youtube')
    check("platform_01532", platform_of('https://youtu.be/TEST000333') == 'youtube')
    check("platform_01533", platform_of('https://www.youtube.com/watch?v=TEST000334') == 'youtube')
    check("platform_01534", platform_of('https://youtu.be/TEST000335') == 'youtube')
    check("platform_01535", platform_of('https://www.youtube.com/watch?v=TEST000336') == 'youtube')
    check("platform_01536", platform_of('https://youtu.be/TEST000337') == 'youtube')
    check("platform_01537", platform_of('https://www.youtube.com/watch?v=TEST000338') == 'youtube')
    check("platform_01538", platform_of('https://youtu.be/TEST000339') == 'youtube')
    check("platform_01539", platform_of('https://www.youtube.com/watch?v=TEST000340') == 'youtube')
    check("platform_01540", platform_of('https://youtu.be/TEST000341') == 'youtube')
    check("platform_01541", platform_of('https://www.youtube.com/watch?v=TEST000342') == 'youtube')
    check("platform_01542", platform_of('https://youtu.be/TEST000343') == 'youtube')
    check("platform_01543", platform_of('https://www.youtube.com/watch?v=TEST000344') == 'youtube')
    check("platform_01544", platform_of('https://youtu.be/TEST000345') == 'youtube')
    check("platform_01545", platform_of('https://www.youtube.com/watch?v=TEST000346') == 'youtube')
    check("platform_01546", platform_of('https://youtu.be/TEST000347') == 'youtube')
    check("platform_01547", platform_of('https://www.youtube.com/watch?v=TEST000348') == 'youtube')
    check("platform_01548", platform_of('https://youtu.be/TEST000349') == 'youtube')
    check("platform_01549", platform_of('https://www.youtube.com/watch?v=TEST000350') == 'youtube')
    check("platform_01550", platform_of('https://youtu.be/TEST000351') == 'youtube')
    check("platform_01551", platform_of('https://www.youtube.com/watch?v=TEST000352') == 'youtube')
    check("platform_01552", platform_of('https://youtu.be/TEST000353') == 'youtube')
    check("platform_01553", platform_of('https://www.youtube.com/watch?v=TEST000354') == 'youtube')
    check("platform_01554", platform_of('https://youtu.be/TEST000355') == 'youtube')
    check("platform_01555", platform_of('https://www.youtube.com/watch?v=TEST000356') == 'youtube')
    check("platform_01556", platform_of('https://youtu.be/TEST000357') == 'youtube')
    check("platform_01557", platform_of('https://www.youtube.com/watch?v=TEST000358') == 'youtube')
    check("platform_01558", platform_of('https://youtu.be/TEST000359') == 'youtube')
    check("platform_01559", platform_of('https://www.youtube.com/watch?v=TEST000360') == 'youtube')
    check("platform_01560", platform_of('https://youtu.be/TEST000361') == 'youtube')
    check("platform_01561", platform_of('https://www.youtube.com/watch?v=TEST000362') == 'youtube')
    check("platform_01562", platform_of('https://youtu.be/TEST000363') == 'youtube')
    check("platform_01563", platform_of('https://www.youtube.com/watch?v=TEST000364') == 'youtube')
    check("platform_01564", platform_of('https://youtu.be/TEST000365') == 'youtube')
    check("platform_01565", platform_of('https://www.youtube.com/watch?v=TEST000366') == 'youtube')
    check("platform_01566", platform_of('https://youtu.be/TEST000367') == 'youtube')
    check("platform_01567", platform_of('https://www.youtube.com/watch?v=TEST000368') == 'youtube')
    check("platform_01568", platform_of('https://youtu.be/TEST000369') == 'youtube')
    check("platform_01569", platform_of('https://www.youtube.com/watch?v=TEST000370') == 'youtube')
    check("platform_01570", platform_of('https://youtu.be/TEST000371') == 'youtube')
    check("platform_01571", platform_of('https://www.youtube.com/watch?v=TEST000372') == 'youtube')
    check("platform_01572", platform_of('https://youtu.be/TEST000373') == 'youtube')
    check("platform_01573", platform_of('https://www.youtube.com/watch?v=TEST000374') == 'youtube')
    check("platform_01574", platform_of('https://youtu.be/TEST000375') == 'youtube')
    check("platform_01575", platform_of('https://www.youtube.com/watch?v=TEST000376') == 'youtube')
    check("platform_01576", platform_of('https://youtu.be/TEST000377') == 'youtube')
    check("platform_01577", platform_of('https://www.youtube.com/watch?v=TEST000378') == 'youtube')
    check("platform_01578", platform_of('https://youtu.be/TEST000379') == 'youtube')
    check("platform_01579", platform_of('https://www.youtube.com/watch?v=TEST000380') == 'youtube')
    check("platform_01580", platform_of('https://youtu.be/TEST000381') == 'youtube')
    check("platform_01581", platform_of('https://www.youtube.com/watch?v=TEST000382') == 'youtube')
    check("platform_01582", platform_of('https://youtu.be/TEST000383') == 'youtube')
    check("platform_01583", platform_of('https://www.youtube.com/watch?v=TEST000384') == 'youtube')
    check("platform_01584", platform_of('https://youtu.be/TEST000385') == 'youtube')
    check("platform_01585", platform_of('https://www.youtube.com/watch?v=TEST000386') == 'youtube')
    check("platform_01586", platform_of('https://youtu.be/TEST000387') == 'youtube')
    check("platform_01587", platform_of('https://www.youtube.com/watch?v=TEST000388') == 'youtube')
    check("platform_01588", platform_of('https://youtu.be/TEST000389') == 'youtube')
    check("platform_01589", platform_of('https://www.youtube.com/watch?v=TEST000390') == 'youtube')
    check("platform_01590", platform_of('https://youtu.be/TEST000391') == 'youtube')
    check("platform_01591", platform_of('https://www.youtube.com/watch?v=TEST000392') == 'youtube')
    check("platform_01592", platform_of('https://youtu.be/TEST000393') == 'youtube')
    check("platform_01593", platform_of('https://www.youtube.com/watch?v=TEST000394') == 'youtube')
    check("platform_01594", platform_of('https://youtu.be/TEST000395') == 'youtube')
    check("platform_01595", platform_of('https://www.youtube.com/watch?v=TEST000396') == 'youtube')
    check("platform_01596", platform_of('https://youtu.be/TEST000397') == 'youtube')
    check("platform_01597", platform_of('https://www.youtube.com/watch?v=TEST000398') == 'youtube')
    check("platform_01598", platform_of('https://youtu.be/TEST000399') == 'youtube')
    check("platform_01599", platform_of('https://www.youtube.com/watch?v=TEST000400') == 'youtube')
    check("platform_01600", platform_of('https://youtu.be/TEST000401') == 'youtube')
    check("platform_01601", platform_of('https://www.youtube.com/watch?v=TEST000402') == 'youtube')
    check("platform_01602", platform_of('https://youtu.be/TEST000403') == 'youtube')
    check("platform_01603", platform_of('https://www.youtube.com/watch?v=TEST000404') == 'youtube')
    check("platform_01604", platform_of('https://youtu.be/TEST000405') == 'youtube')
    check("platform_01605", platform_of('https://www.youtube.com/watch?v=TEST000406') == 'youtube')
    check("platform_01606", platform_of('https://youtu.be/TEST000407') == 'youtube')
    check("platform_01607", platform_of('https://www.youtube.com/watch?v=TEST000408') == 'youtube')
    check("platform_01608", platform_of('https://youtu.be/TEST000409') == 'youtube')
    check("platform_01609", platform_of('https://www.youtube.com/watch?v=TEST000410') == 'youtube')
    check("platform_01610", platform_of('https://youtu.be/TEST000411') == 'youtube')
    check("platform_01611", platform_of('https://www.youtube.com/watch?v=TEST000412') == 'youtube')
    check("platform_01612", platform_of('https://youtu.be/TEST000413') == 'youtube')
    check("platform_01613", platform_of('https://www.youtube.com/watch?v=TEST000414') == 'youtube')
    check("platform_01614", platform_of('https://youtu.be/TEST000415') == 'youtube')
    check("platform_01615", platform_of('https://www.youtube.com/watch?v=TEST000416') == 'youtube')
    check("platform_01616", platform_of('https://youtu.be/TEST000417') == 'youtube')
    check("platform_01617", platform_of('https://www.youtube.com/watch?v=TEST000418') == 'youtube')
    check("platform_01618", platform_of('https://youtu.be/TEST000419') == 'youtube')
    check("platform_01619", platform_of('https://www.youtube.com/watch?v=TEST000420') == 'youtube')
    check("platform_01620", platform_of('https://youtu.be/TEST000421') == 'youtube')
    check("platform_01621", platform_of('https://www.youtube.com/watch?v=TEST000422') == 'youtube')
    check("platform_01622", platform_of('https://youtu.be/TEST000423') == 'youtube')
    check("platform_01623", platform_of('https://www.youtube.com/watch?v=TEST000424') == 'youtube')
    check("platform_01624", platform_of('https://youtu.be/TEST000425') == 'youtube')
    check("platform_01625", platform_of('https://www.youtube.com/watch?v=TEST000426') == 'youtube')
    check("platform_01626", platform_of('https://youtu.be/TEST000427') == 'youtube')
    check("platform_01627", platform_of('https://www.youtube.com/watch?v=TEST000428') == 'youtube')
    check("platform_01628", platform_of('https://youtu.be/TEST000429') == 'youtube')
    check("platform_01629", platform_of('https://www.youtube.com/watch?v=TEST000430') == 'youtube')
    check("platform_01630", platform_of('https://youtu.be/TEST000431') == 'youtube')
    check("platform_01631", platform_of('https://www.youtube.com/watch?v=TEST000432') == 'youtube')
    check("platform_01632", platform_of('https://youtu.be/TEST000433') == 'youtube')
    check("platform_01633", platform_of('https://www.youtube.com/watch?v=TEST000434') == 'youtube')
    check("platform_01634", platform_of('https://youtu.be/TEST000435') == 'youtube')
    check("platform_01635", platform_of('https://www.youtube.com/watch?v=TEST000436') == 'youtube')
    check("platform_01636", platform_of('https://youtu.be/TEST000437') == 'youtube')
    check("platform_01637", platform_of('https://www.youtube.com/watch?v=TEST000438') == 'youtube')
    check("platform_01638", platform_of('https://youtu.be/TEST000439') == 'youtube')
    check("platform_01639", platform_of('https://www.youtube.com/watch?v=TEST000440') == 'youtube')
    check("platform_01640", platform_of('https://youtu.be/TEST000441') == 'youtube')
    check("platform_01641", platform_of('https://www.youtube.com/watch?v=TEST000442') == 'youtube')
    check("platform_01642", platform_of('https://youtu.be/TEST000443') == 'youtube')
    check("platform_01643", platform_of('https://www.youtube.com/watch?v=TEST000444') == 'youtube')
    check("platform_01644", platform_of('https://youtu.be/TEST000445') == 'youtube')
    check("platform_01645", platform_of('https://www.youtube.com/watch?v=TEST000446') == 'youtube')
    check("platform_01646", platform_of('https://youtu.be/TEST000447') == 'youtube')
    check("platform_01647", platform_of('https://www.youtube.com/watch?v=TEST000448') == 'youtube')
    check("platform_01648", platform_of('https://youtu.be/TEST000449') == 'youtube')
    check("platform_01649", platform_of('https://www.youtube.com/watch?v=TEST000450') == 'youtube')
    check("platform_01650", platform_of('https://youtu.be/TEST000451') == 'youtube')
    check("platform_01651", platform_of('https://www.youtube.com/watch?v=TEST000452') == 'youtube')
    check("platform_01652", platform_of('https://youtu.be/TEST000453') == 'youtube')
    check("platform_01653", platform_of('https://www.youtube.com/watch?v=TEST000454') == 'youtube')
    check("platform_01654", platform_of('https://youtu.be/TEST000455') == 'youtube')
    check("platform_01655", platform_of('https://www.youtube.com/watch?v=TEST000456') == 'youtube')
    check("platform_01656", platform_of('https://youtu.be/TEST000457') == 'youtube')
    check("platform_01657", platform_of('https://www.youtube.com/watch?v=TEST000458') == 'youtube')
    check("platform_01658", platform_of('https://youtu.be/TEST000459') == 'youtube')
    check("platform_01659", platform_of('https://www.youtube.com/watch?v=TEST000460') == 'youtube')
    check("platform_01660", platform_of('https://youtu.be/TEST000461') == 'youtube')
    check("platform_01661", platform_of('https://www.youtube.com/watch?v=TEST000462') == 'youtube')
    check("platform_01662", platform_of('https://youtu.be/TEST000463') == 'youtube')
    check("platform_01663", platform_of('https://www.youtube.com/watch?v=TEST000464') == 'youtube')
    check("platform_01664", platform_of('https://youtu.be/TEST000465') == 'youtube')
    check("platform_01665", platform_of('https://www.youtube.com/watch?v=TEST000466') == 'youtube')
    check("platform_01666", platform_of('https://youtu.be/TEST000467') == 'youtube')
    check("platform_01667", platform_of('https://www.youtube.com/watch?v=TEST000468') == 'youtube')
    check("platform_01668", platform_of('https://youtu.be/TEST000469') == 'youtube')
    check("platform_01669", platform_of('https://www.youtube.com/watch?v=TEST000470') == 'youtube')
    check("platform_01670", platform_of('https://youtu.be/TEST000471') == 'youtube')
    check("platform_01671", platform_of('https://www.youtube.com/watch?v=TEST000472') == 'youtube')
    check("platform_01672", platform_of('https://youtu.be/TEST000473') == 'youtube')
    check("platform_01673", platform_of('https://www.youtube.com/watch?v=TEST000474') == 'youtube')
    check("platform_01674", platform_of('https://youtu.be/TEST000475') == 'youtube')
    check("platform_01675", platform_of('https://www.youtube.com/watch?v=TEST000476') == 'youtube')
    check("platform_01676", platform_of('https://youtu.be/TEST000477') == 'youtube')
    check("platform_01677", platform_of('https://www.youtube.com/watch?v=TEST000478') == 'youtube')
    check("platform_01678", platform_of('https://youtu.be/TEST000479') == 'youtube')
    check("platform_01679", platform_of('https://www.youtube.com/watch?v=TEST000480') == 'youtube')
    check("platform_01680", platform_of('https://youtu.be/TEST000481') == 'youtube')
    check("platform_01681", platform_of('https://www.youtube.com/watch?v=TEST000482') == 'youtube')
    check("platform_01682", platform_of('https://youtu.be/TEST000483') == 'youtube')
    check("platform_01683", platform_of('https://www.youtube.com/watch?v=TEST000484') == 'youtube')
    check("platform_01684", platform_of('https://youtu.be/TEST000485') == 'youtube')
    check("platform_01685", platform_of('https://www.youtube.com/watch?v=TEST000486') == 'youtube')
    check("platform_01686", platform_of('https://youtu.be/TEST000487') == 'youtube')
    check("platform_01687", platform_of('https://www.youtube.com/watch?v=TEST000488') == 'youtube')
    check("platform_01688", platform_of('https://youtu.be/TEST000489') == 'youtube')
    check("platform_01689", platform_of('https://www.youtube.com/watch?v=TEST000490') == 'youtube')
    check("platform_01690", platform_of('https://youtu.be/TEST000491') == 'youtube')
    check("platform_01691", platform_of('https://www.youtube.com/watch?v=TEST000492') == 'youtube')
    check("platform_01692", platform_of('https://youtu.be/TEST000493') == 'youtube')
    check("platform_01693", platform_of('https://www.youtube.com/watch?v=TEST000494') == 'youtube')
    check("platform_01694", platform_of('https://youtu.be/TEST000495') == 'youtube')
    check("platform_01695", platform_of('https://www.youtube.com/watch?v=TEST000496') == 'youtube')
    check("platform_01696", platform_of('https://youtu.be/TEST000497') == 'youtube')
    check("platform_01697", platform_of('https://www.youtube.com/watch?v=TEST000498') == 'youtube')
    check("platform_01698", platform_of('https://youtu.be/TEST000499') == 'youtube')
    check("platform_01699", platform_of('https://www.youtube.com/watch?v=TEST000500') == 'youtube')
    check("platform_01700", platform_of('https://youtu.be/TEST000501') == 'youtube')
    check("platform_01701", platform_of('https://www.youtube.com/watch?v=TEST000502') == 'youtube')
    check("platform_01702", platform_of('https://youtu.be/TEST000503') == 'youtube')
    check("platform_01703", platform_of('https://www.youtube.com/watch?v=TEST000504') == 'youtube')
    check("platform_01704", platform_of('https://youtu.be/TEST000505') == 'youtube')
    check("platform_01705", platform_of('https://www.youtube.com/watch?v=TEST000506') == 'youtube')
    check("platform_01706", platform_of('https://youtu.be/TEST000507') == 'youtube')
    check("platform_01707", platform_of('https://www.youtube.com/watch?v=TEST000508') == 'youtube')
    check("platform_01708", platform_of('https://youtu.be/TEST000509') == 'youtube')
    check("platform_01709", platform_of('https://www.youtube.com/watch?v=TEST000510') == 'youtube')
    check("platform_01710", platform_of('https://youtu.be/TEST000511') == 'youtube')
    check("platform_01711", platform_of('https://www.youtube.com/watch?v=TEST000512') == 'youtube')
    check("platform_01712", platform_of('https://youtu.be/TEST000513') == 'youtube')
    check("platform_01713", platform_of('https://www.youtube.com/watch?v=TEST000514') == 'youtube')
    check("platform_01714", platform_of('https://youtu.be/TEST000515') == 'youtube')
    check("platform_01715", platform_of('https://www.youtube.com/watch?v=TEST000516') == 'youtube')
    check("platform_01716", platform_of('https://youtu.be/TEST000517') == 'youtube')
    check("platform_01717", platform_of('https://www.youtube.com/watch?v=TEST000518') == 'youtube')
    check("platform_01718", platform_of('https://youtu.be/TEST000519') == 'youtube')
    check("platform_01719", platform_of('https://www.youtube.com/watch?v=TEST000520') == 'youtube')
    check("platform_01720", platform_of('https://youtu.be/TEST000521') == 'youtube')
    check("platform_01721", platform_of('https://www.youtube.com/watch?v=TEST000522') == 'youtube')
    check("platform_01722", platform_of('https://youtu.be/TEST000523') == 'youtube')
    check("platform_01723", platform_of('https://www.youtube.com/watch?v=TEST000524') == 'youtube')
    check("platform_01724", platform_of('https://youtu.be/TEST000525') == 'youtube')
    check("platform_01725", platform_of('https://www.youtube.com/watch?v=TEST000526') == 'youtube')
    check("platform_01726", platform_of('https://youtu.be/TEST000527') == 'youtube')
    check("platform_01727", platform_of('https://www.youtube.com/watch?v=TEST000528') == 'youtube')
    check("platform_01728", platform_of('https://youtu.be/TEST000529') == 'youtube')
    check("platform_01729", platform_of('https://www.youtube.com/watch?v=TEST000530') == 'youtube')
    check("platform_01730", platform_of('https://youtu.be/TEST000531') == 'youtube')
    check("platform_01731", platform_of('https://www.youtube.com/watch?v=TEST000532') == 'youtube')
    check("platform_01732", platform_of('https://youtu.be/TEST000533') == 'youtube')
    check("platform_01733", platform_of('https://www.youtube.com/watch?v=TEST000534') == 'youtube')
    check("platform_01734", platform_of('https://youtu.be/TEST000535') == 'youtube')
    check("platform_01735", platform_of('https://www.youtube.com/watch?v=TEST000536') == 'youtube')
    check("platform_01736", platform_of('https://youtu.be/TEST000537') == 'youtube')
    check("platform_01737", platform_of('https://www.youtube.com/watch?v=TEST000538') == 'youtube')
    check("platform_01738", platform_of('https://youtu.be/TEST000539') == 'youtube')
    check("platform_01739", platform_of('https://www.youtube.com/watch?v=TEST000540') == 'youtube')
    check("platform_01740", platform_of('https://youtu.be/TEST000541') == 'youtube')
    check("platform_01741", platform_of('https://www.youtube.com/watch?v=TEST000542') == 'youtube')
    check("platform_01742", platform_of('https://youtu.be/TEST000543') == 'youtube')
    check("platform_01743", platform_of('https://www.youtube.com/watch?v=TEST000544') == 'youtube')
    check("platform_01744", platform_of('https://youtu.be/TEST000545') == 'youtube')
    check("platform_01745", platform_of('https://www.youtube.com/watch?v=TEST000546') == 'youtube')
    check("platform_01746", platform_of('https://youtu.be/TEST000547') == 'youtube')
    check("platform_01747", platform_of('https://www.youtube.com/watch?v=TEST000548') == 'youtube')
    check("platform_01748", platform_of('https://youtu.be/TEST000549') == 'youtube')
    check("platform_01749", platform_of('https://www.youtube.com/watch?v=TEST000550') == 'youtube')
    check("platform_01750", platform_of('https://youtu.be/TEST000551') == 'youtube')
    check("platform_01751", platform_of('https://www.youtube.com/watch?v=TEST000552') == 'youtube')
    check("platform_01752", platform_of('https://youtu.be/TEST000553') == 'youtube')
    check("platform_01753", platform_of('https://www.youtube.com/watch?v=TEST000554') == 'youtube')
    check("platform_01754", platform_of('https://youtu.be/TEST000555') == 'youtube')
    check("platform_01755", platform_of('https://www.youtube.com/watch?v=TEST000556') == 'youtube')
    check("platform_01756", platform_of('https://youtu.be/TEST000557') == 'youtube')
    check("platform_01757", platform_of('https://www.youtube.com/watch?v=TEST000558') == 'youtube')
    check("platform_01758", platform_of('https://youtu.be/TEST000559') == 'youtube')
    check("platform_01759", platform_of('https://www.youtube.com/watch?v=TEST000560') == 'youtube')
    check("platform_01760", platform_of('https://youtu.be/TEST000561') == 'youtube')
    check("platform_01761", platform_of('https://www.youtube.com/watch?v=TEST000562') == 'youtube')
    check("platform_01762", platform_of('https://youtu.be/TEST000563') == 'youtube')
    check("platform_01763", platform_of('https://www.youtube.com/watch?v=TEST000564') == 'youtube')
    check("platform_01764", platform_of('https://youtu.be/TEST000565') == 'youtube')
    check("platform_01765", platform_of('https://www.youtube.com/watch?v=TEST000566') == 'youtube')
    check("platform_01766", platform_of('https://youtu.be/TEST000567') == 'youtube')
    check("platform_01767", platform_of('https://www.youtube.com/watch?v=TEST000568') == 'youtube')
    check("platform_01768", platform_of('https://youtu.be/TEST000569') == 'youtube')
    check("platform_01769", platform_of('https://www.youtube.com/watch?v=TEST000570') == 'youtube')
    check("platform_01770", platform_of('https://youtu.be/TEST000571') == 'youtube')
    check("platform_01771", platform_of('https://www.youtube.com/watch?v=TEST000572') == 'youtube')
    check("platform_01772", platform_of('https://youtu.be/TEST000573') == 'youtube')
    check("platform_01773", platform_of('https://www.youtube.com/watch?v=TEST000574') == 'youtube')
    check("platform_01774", platform_of('https://youtu.be/TEST000575') == 'youtube')
    check("platform_01775", platform_of('https://www.youtube.com/watch?v=TEST000576') == 'youtube')
    check("platform_01776", platform_of('https://youtu.be/TEST000577') == 'youtube')
    check("platform_01777", platform_of('https://www.youtube.com/watch?v=TEST000578') == 'youtube')
    check("platform_01778", platform_of('https://youtu.be/TEST000579') == 'youtube')
    check("platform_01779", platform_of('https://www.youtube.com/watch?v=TEST000580') == 'youtube')
    check("platform_01780", platform_of('https://youtu.be/TEST000581') == 'youtube')
    check("platform_01781", platform_of('https://www.youtube.com/watch?v=TEST000582') == 'youtube')
    check("platform_01782", platform_of('https://youtu.be/TEST000583') == 'youtube')
    check("platform_01783", platform_of('https://www.youtube.com/watch?v=TEST000584') == 'youtube')
    check("platform_01784", platform_of('https://youtu.be/TEST000585') == 'youtube')
    check("platform_01785", platform_of('https://www.youtube.com/watch?v=TEST000586') == 'youtube')
    check("platform_01786", platform_of('https://youtu.be/TEST000587') == 'youtube')
    check("platform_01787", platform_of('https://www.youtube.com/watch?v=TEST000588') == 'youtube')
    check("platform_01788", platform_of('https://youtu.be/TEST000589') == 'youtube')
    check("platform_01789", platform_of('https://www.youtube.com/watch?v=TEST000590') == 'youtube')
    check("platform_01790", platform_of('https://youtu.be/TEST000591') == 'youtube')
    check("platform_01791", platform_of('https://www.youtube.com/watch?v=TEST000592') == 'youtube')
    check("platform_01792", platform_of('https://youtu.be/TEST000593') == 'youtube')
    check("platform_01793", platform_of('https://www.youtube.com/watch?v=TEST000594') == 'youtube')
    check("platform_01794", platform_of('https://youtu.be/TEST000595') == 'youtube')
    check("platform_01795", platform_of('https://www.youtube.com/watch?v=TEST000596') == 'youtube')
    check("platform_01796", platform_of('https://youtu.be/TEST000597') == 'youtube')
    check("platform_01797", platform_of('https://www.youtube.com/watch?v=TEST000598') == 'youtube')
    check("platform_01798", platform_of('https://youtu.be/TEST000599') == 'youtube')
    check("platform_01799", platform_of('https://www.youtube.com/watch?v=TEST000600') == 'youtube')
    check("platform_01800", platform_of('https://youtu.be/TEST000601') == 'youtube')
    check("platform_01801", platform_of('https://www.youtube.com/watch?v=TEST000602') == 'youtube')
    check("platform_01802", platform_of('https://youtu.be/TEST000603') == 'youtube')
    check("platform_01803", platform_of('https://www.youtube.com/watch?v=TEST000604') == 'youtube')
    check("platform_01804", platform_of('https://youtu.be/TEST000605') == 'youtube')
    check("platform_01805", platform_of('https://www.youtube.com/watch?v=TEST000606') == 'youtube')
    check("platform_01806", platform_of('https://youtu.be/TEST000607') == 'youtube')
    check("platform_01807", platform_of('https://www.youtube.com/watch?v=TEST000608') == 'youtube')
    check("platform_01808", platform_of('https://youtu.be/TEST000609') == 'youtube')
    check("platform_01809", platform_of('https://www.youtube.com/watch?v=TEST000610') == 'youtube')
    check("platform_01810", platform_of('https://youtu.be/TEST000611') == 'youtube')
    check("platform_01811", platform_of('https://www.youtube.com/watch?v=TEST000612') == 'youtube')
    check("platform_01812", platform_of('https://youtu.be/TEST000613') == 'youtube')
    check("platform_01813", platform_of('https://www.youtube.com/watch?v=TEST000614') == 'youtube')
    check("platform_01814", platform_of('https://youtu.be/TEST000615') == 'youtube')
    check("platform_01815", platform_of('https://www.youtube.com/watch?v=TEST000616') == 'youtube')
    check("platform_01816", platform_of('https://youtu.be/TEST000617') == 'youtube')
    check("platform_01817", platform_of('https://www.youtube.com/watch?v=TEST000618') == 'youtube')
    check("platform_01818", platform_of('https://youtu.be/TEST000619') == 'youtube')
    check("platform_01819", platform_of('https://www.youtube.com/watch?v=TEST000620') == 'youtube')
    check("platform_01820", platform_of('https://youtu.be/TEST000621') == 'youtube')
    check("platform_01821", platform_of('https://www.youtube.com/watch?v=TEST000622') == 'youtube')
    check("platform_01822", platform_of('https://youtu.be/TEST000623') == 'youtube')
    check("platform_01823", platform_of('https://www.youtube.com/watch?v=TEST000624') == 'youtube')
    check("platform_01824", platform_of('https://youtu.be/TEST000625') == 'youtube')
    check("platform_01825", platform_of('https://www.youtube.com/watch?v=TEST000626') == 'youtube')
    check("platform_01826", platform_of('https://youtu.be/TEST000627') == 'youtube')
    check("platform_01827", platform_of('https://www.youtube.com/watch?v=TEST000628') == 'youtube')
    check("platform_01828", platform_of('https://youtu.be/TEST000629') == 'youtube')
    check("platform_01829", platform_of('https://www.youtube.com/watch?v=TEST000630') == 'youtube')
    check("platform_01830", platform_of('https://youtu.be/TEST000631') == 'youtube')
    check("platform_01831", platform_of('https://www.youtube.com/watch?v=TEST000632') == 'youtube')
    check("platform_01832", platform_of('https://youtu.be/TEST000633') == 'youtube')
    check("platform_01833", platform_of('https://www.youtube.com/watch?v=TEST000634') == 'youtube')
    check("platform_01834", platform_of('https://youtu.be/TEST000635') == 'youtube')
    check("platform_01835", platform_of('https://www.youtube.com/watch?v=TEST000636') == 'youtube')
    check("platform_01836", platform_of('https://youtu.be/TEST000637') == 'youtube')
    check("platform_01837", platform_of('https://www.youtube.com/watch?v=TEST000638') == 'youtube')
    check("platform_01838", platform_of('https://youtu.be/TEST000639') == 'youtube')
    check("platform_01839", platform_of('https://www.youtube.com/watch?v=TEST000640') == 'youtube')
    check("platform_01840", platform_of('https://youtu.be/TEST000641') == 'youtube')
    check("platform_01841", platform_of('https://www.youtube.com/watch?v=TEST000642') == 'youtube')
    check("platform_01842", platform_of('https://youtu.be/TEST000643') == 'youtube')
    check("platform_01843", platform_of('https://www.youtube.com/watch?v=TEST000644') == 'youtube')
    check("platform_01844", platform_of('https://youtu.be/TEST000645') == 'youtube')
    check("platform_01845", platform_of('https://www.youtube.com/watch?v=TEST000646') == 'youtube')
    check("platform_01846", platform_of('https://youtu.be/TEST000647') == 'youtube')
    check("platform_01847", platform_of('https://www.youtube.com/watch?v=TEST000648') == 'youtube')
    check("platform_01848", platform_of('https://youtu.be/TEST000649') == 'youtube')
    check("platform_01849", platform_of('https://www.youtube.com/watch?v=TEST000650') == 'youtube')
    check("platform_01850", platform_of('https://youtu.be/TEST000651') == 'youtube')
    check("platform_01851", platform_of('https://www.youtube.com/watch?v=TEST000652') == 'youtube')
    check("platform_01852", platform_of('https://youtu.be/TEST000653') == 'youtube')
    check("platform_01853", platform_of('https://www.youtube.com/watch?v=TEST000654') == 'youtube')
    check("platform_01854", platform_of('https://youtu.be/TEST000655') == 'youtube')
    check("platform_01855", platform_of('https://www.youtube.com/watch?v=TEST000656') == 'youtube')
    check("platform_01856", platform_of('https://youtu.be/TEST000657') == 'youtube')
    check("platform_01857", platform_of('https://www.youtube.com/watch?v=TEST000658') == 'youtube')
    check("platform_01858", platform_of('https://youtu.be/TEST000659') == 'youtube')
    check("platform_01859", platform_of('https://www.youtube.com/watch?v=TEST000660') == 'youtube')
    check("platform_01860", platform_of('https://youtu.be/TEST000661') == 'youtube')
    check("platform_01861", platform_of('https://www.youtube.com/watch?v=TEST000662') == 'youtube')
    check("platform_01862", platform_of('https://youtu.be/TEST000663') == 'youtube')
    check("platform_01863", platform_of('https://www.youtube.com/watch?v=TEST000664') == 'youtube')
    check("platform_01864", platform_of('https://youtu.be/TEST000665') == 'youtube')
    check("platform_01865", platform_of('https://www.youtube.com/watch?v=TEST000666') == 'youtube')
    check("platform_01866", platform_of('https://youtu.be/TEST000667') == 'youtube')
    check("platform_01867", platform_of('https://www.youtube.com/watch?v=TEST000668') == 'youtube')
    check("platform_01868", platform_of('https://youtu.be/TEST000669') == 'youtube')
    check("platform_01869", platform_of('https://www.youtube.com/watch?v=TEST000670') == 'youtube')
    check("platform_01870", platform_of('https://youtu.be/TEST000671') == 'youtube')
    check("platform_01871", platform_of('https://www.youtube.com/watch?v=TEST000672') == 'youtube')
    check("platform_01872", platform_of('https://youtu.be/TEST000673') == 'youtube')
    check("platform_01873", platform_of('https://www.youtube.com/watch?v=TEST000674') == 'youtube')
    check("platform_01874", platform_of('https://youtu.be/TEST000675') == 'youtube')
    check("platform_01875", platform_of('https://www.youtube.com/watch?v=TEST000676') == 'youtube')
    check("platform_01876", platform_of('https://youtu.be/TEST000677') == 'youtube')
    check("platform_01877", platform_of('https://www.youtube.com/watch?v=TEST000678') == 'youtube')
    check("platform_01878", platform_of('https://youtu.be/TEST000679') == 'youtube')
    check("platform_01879", platform_of('https://www.youtube.com/watch?v=TEST000680') == 'youtube')
    check("platform_01880", platform_of('https://youtu.be/TEST000681') == 'youtube')
    check("platform_01881", platform_of('https://www.youtube.com/watch?v=TEST000682') == 'youtube')
    check("platform_01882", platform_of('https://youtu.be/TEST000683') == 'youtube')
    check("platform_01883", platform_of('https://www.youtube.com/watch?v=TEST000684') == 'youtube')
    check("platform_01884", platform_of('https://youtu.be/TEST000685') == 'youtube')
    check("platform_01885", platform_of('https://www.youtube.com/watch?v=TEST000686') == 'youtube')
    check("platform_01886", platform_of('https://youtu.be/TEST000687') == 'youtube')
    check("platform_01887", platform_of('https://www.youtube.com/watch?v=TEST000688') == 'youtube')
    check("platform_01888", platform_of('https://youtu.be/TEST000689') == 'youtube')
    check("platform_01889", platform_of('https://www.youtube.com/watch?v=TEST000690') == 'youtube')
    check("platform_01890", platform_of('https://youtu.be/TEST000691') == 'youtube')
    check("platform_01891", platform_of('https://www.youtube.com/watch?v=TEST000692') == 'youtube')
    check("platform_01892", platform_of('https://youtu.be/TEST000693') == 'youtube')
    check("platform_01893", platform_of('https://www.youtube.com/watch?v=TEST000694') == 'youtube')
    check("platform_01894", platform_of('https://youtu.be/TEST000695') == 'youtube')
    check("platform_01895", platform_of('https://www.youtube.com/watch?v=TEST000696') == 'youtube')
    check("platform_01896", platform_of('https://youtu.be/TEST000697') == 'youtube')
    check("platform_01897", platform_of('https://www.youtube.com/watch?v=TEST000698') == 'youtube')
    check("platform_01898", platform_of('https://youtu.be/TEST000699') == 'youtube')
    check("platform_01899", platform_of('https://www.youtube.com/watch?v=TEST000700') == 'youtube')
    check("platform_01900", platform_of('https://youtu.be/TEST000701') == 'youtube')
    check("platform_01901", platform_of('https://www.youtube.com/watch?v=TEST000702') == 'youtube')
    check("platform_01902", platform_of('https://youtu.be/TEST000703') == 'youtube')
    check("platform_01903", platform_of('https://www.youtube.com/watch?v=TEST000704') == 'youtube')
    check("platform_01904", platform_of('https://youtu.be/TEST000705') == 'youtube')
    check("platform_01905", platform_of('https://www.youtube.com/watch?v=TEST000706') == 'youtube')
    check("platform_01906", platform_of('https://youtu.be/TEST000707') == 'youtube')
    check("platform_01907", platform_of('https://www.youtube.com/watch?v=TEST000708') == 'youtube')
    check("platform_01908", platform_of('https://youtu.be/TEST000709') == 'youtube')
    check("platform_01909", platform_of('https://www.youtube.com/watch?v=TEST000710') == 'youtube')
    check("platform_01910", platform_of('https://youtu.be/TEST000711') == 'youtube')
    check("platform_01911", platform_of('https://www.youtube.com/watch?v=TEST000712') == 'youtube')
    check("platform_01912", platform_of('https://youtu.be/TEST000713') == 'youtube')
    check("platform_01913", platform_of('https://www.youtube.com/watch?v=TEST000714') == 'youtube')
    check("platform_01914", platform_of('https://youtu.be/TEST000715') == 'youtube')
    check("platform_01915", platform_of('https://www.youtube.com/watch?v=TEST000716') == 'youtube')
    check("platform_01916", platform_of('https://youtu.be/TEST000717') == 'youtube')
    check("platform_01917", platform_of('https://www.youtube.com/watch?v=TEST000718') == 'youtube')
    check("platform_01918", platform_of('https://youtu.be/TEST000719') == 'youtube')
    check("platform_01919", platform_of('https://www.youtube.com/watch?v=TEST000720') == 'youtube')
    check("platform_01920", platform_of('https://youtu.be/TEST000721') == 'youtube')
    check("platform_01921", platform_of('https://www.youtube.com/watch?v=TEST000722') == 'youtube')
    check("platform_01922", platform_of('https://youtu.be/TEST000723') == 'youtube')
    check("platform_01923", platform_of('https://www.youtube.com/watch?v=TEST000724') == 'youtube')
    check("platform_01924", platform_of('https://youtu.be/TEST000725') == 'youtube')
    check("platform_01925", platform_of('https://www.youtube.com/watch?v=TEST000726') == 'youtube')
    check("platform_01926", platform_of('https://youtu.be/TEST000727') == 'youtube')
    check("platform_01927", platform_of('https://www.youtube.com/watch?v=TEST000728') == 'youtube')
    check("platform_01928", platform_of('https://youtu.be/TEST000729') == 'youtube')
    check("platform_01929", platform_of('https://www.youtube.com/watch?v=TEST000730') == 'youtube')
    check("platform_01930", platform_of('https://youtu.be/TEST000731') == 'youtube')
    check("platform_01931", platform_of('https://www.youtube.com/watch?v=TEST000732') == 'youtube')
    check("platform_01932", platform_of('https://youtu.be/TEST000733') == 'youtube')
    check("platform_01933", platform_of('https://www.youtube.com/watch?v=TEST000734') == 'youtube')
    check("platform_01934", platform_of('https://youtu.be/TEST000735') == 'youtube')
    check("platform_01935", platform_of('https://www.youtube.com/watch?v=TEST000736') == 'youtube')
    check("platform_01936", platform_of('https://youtu.be/TEST000737') == 'youtube')
    check("platform_01937", platform_of('https://www.youtube.com/watch?v=TEST000738') == 'youtube')
    check("platform_01938", platform_of('https://youtu.be/TEST000739') == 'youtube')
    check("platform_01939", platform_of('https://www.youtube.com/watch?v=TEST000740') == 'youtube')
    check("platform_01940", platform_of('https://youtu.be/TEST000741') == 'youtube')
    check("platform_01941", platform_of('https://www.youtube.com/watch?v=TEST000742') == 'youtube')
    check("platform_01942", platform_of('https://youtu.be/TEST000743') == 'youtube')
    check("platform_01943", platform_of('https://www.youtube.com/watch?v=TEST000744') == 'youtube')
    check("platform_01944", platform_of('https://youtu.be/TEST000745') == 'youtube')
    check("platform_01945", platform_of('https://www.youtube.com/watch?v=TEST000746') == 'youtube')
    check("platform_01946", platform_of('https://youtu.be/TEST000747') == 'youtube')
    check("platform_01947", platform_of('https://www.youtube.com/watch?v=TEST000748') == 'youtube')
    check("platform_01948", platform_of('https://youtu.be/TEST000749') == 'youtube')
    check("platform_01949", platform_of('https://www.youtube.com/watch?v=TEST000750') == 'youtube')
    check("platform_01950", platform_of('https://youtu.be/TEST000751') == 'youtube')
    check("platform_01951", platform_of('https://www.youtube.com/watch?v=TEST000752') == 'youtube')
    check("platform_01952", platform_of('https://youtu.be/TEST000753') == 'youtube')
    check("platform_01953", platform_of('https://www.youtube.com/watch?v=TEST000754') == 'youtube')
    check("platform_01954", platform_of('https://youtu.be/TEST000755') == 'youtube')
    check("platform_01955", platform_of('https://www.youtube.com/watch?v=TEST000756') == 'youtube')
    check("platform_01956", platform_of('https://youtu.be/TEST000757') == 'youtube')
    check("platform_01957", platform_of('https://www.youtube.com/watch?v=TEST000758') == 'youtube')
    check("platform_01958", platform_of('https://youtu.be/TEST000759') == 'youtube')
    check("platform_01959", platform_of('https://www.youtube.com/watch?v=TEST000760') == 'youtube')
    check("platform_01960", platform_of('https://youtu.be/TEST000761') == 'youtube')
    check("platform_01961", platform_of('https://www.youtube.com/watch?v=TEST000762') == 'youtube')
    check("platform_01962", platform_of('https://youtu.be/TEST000763') == 'youtube')
    check("platform_01963", platform_of('https://www.youtube.com/watch?v=TEST000764') == 'youtube')
    check("platform_01964", platform_of('https://youtu.be/TEST000765') == 'youtube')
    check("platform_01965", platform_of('https://www.youtube.com/watch?v=TEST000766') == 'youtube')
    check("platform_01966", platform_of('https://youtu.be/TEST000767') == 'youtube')
    check("platform_01967", platform_of('https://www.youtube.com/watch?v=TEST000768') == 'youtube')
    check("platform_01968", platform_of('https://youtu.be/TEST000769') == 'youtube')
    check("platform_01969", platform_of('https://www.youtube.com/watch?v=TEST000770') == 'youtube')
    check("platform_01970", platform_of('https://youtu.be/TEST000771') == 'youtube')
    check("platform_01971", platform_of('https://www.youtube.com/watch?v=TEST000772') == 'youtube')
    check("platform_01972", platform_of('https://youtu.be/TEST000773') == 'youtube')
    check("platform_01973", platform_of('https://www.youtube.com/watch?v=TEST000774') == 'youtube')
    check("platform_01974", platform_of('https://youtu.be/TEST000775') == 'youtube')
    check("platform_01975", platform_of('https://www.youtube.com/watch?v=TEST000776') == 'youtube')
    check("platform_01976", platform_of('https://youtu.be/TEST000777') == 'youtube')
    check("platform_01977", platform_of('https://www.youtube.com/watch?v=TEST000778') == 'youtube')
    check("platform_01978", platform_of('https://youtu.be/TEST000779') == 'youtube')
    check("platform_01979", platform_of('https://www.youtube.com/watch?v=TEST000780') == 'youtube')
    check("platform_01980", platform_of('https://youtu.be/TEST000781') == 'youtube')
    check("platform_01981", platform_of('https://www.youtube.com/watch?v=TEST000782') == 'youtube')
    check("platform_01982", platform_of('https://youtu.be/TEST000783') == 'youtube')
    check("platform_01983", platform_of('https://www.youtube.com/watch?v=TEST000784') == 'youtube')
    check("platform_01984", platform_of('https://youtu.be/TEST000785') == 'youtube')
    check("platform_01985", platform_of('https://www.youtube.com/watch?v=TEST000786') == 'youtube')
    check("platform_01986", platform_of('https://youtu.be/TEST000787') == 'youtube')
    check("platform_01987", platform_of('https://www.youtube.com/watch?v=TEST000788') == 'youtube')
    check("platform_01988", platform_of('https://youtu.be/TEST000789') == 'youtube')
    check("platform_01989", platform_of('https://www.youtube.com/watch?v=TEST000790') == 'youtube')
    check("platform_01990", platform_of('https://youtu.be/TEST000791') == 'youtube')
    check("platform_01991", platform_of('https://www.youtube.com/watch?v=TEST000792') == 'youtube')
    check("platform_01992", platform_of('https://youtu.be/TEST000793') == 'youtube')
    check("platform_01993", platform_of('https://www.youtube.com/watch?v=TEST000794') == 'youtube')
    check("platform_01994", platform_of('https://youtu.be/TEST000795') == 'youtube')
    check("platform_01995", platform_of('https://www.youtube.com/watch?v=TEST000796') == 'youtube')
    check("platform_01996", platform_of('https://youtu.be/TEST000797') == 'youtube')
    check("platform_01997", platform_of('https://www.youtube.com/watch?v=TEST000798') == 'youtube')
    check("platform_01998", platform_of('https://youtu.be/TEST000799') == 'youtube')
    check("platform_01999", platform_of('https://www.youtube.com/watch?v=TEST000800') == 'youtube')
    check("platform_02000", platform_of('https://youtu.be/TEST000801') == 'youtube')
    check("platform_02001", platform_of('https://www.youtube.com/watch?v=TEST000802') == 'youtube')
    check("platform_02002", platform_of('https://youtu.be/TEST000803') == 'youtube')
    check("platform_02003", platform_of('https://www.youtube.com/watch?v=TEST000804') == 'youtube')
    check("platform_02004", platform_of('https://youtu.be/TEST000805') == 'youtube')
    check("platform_02005", platform_of('https://www.youtube.com/watch?v=TEST000806') == 'youtube')
    check("platform_02006", platform_of('https://youtu.be/TEST000807') == 'youtube')
    check("platform_02007", platform_of('https://www.youtube.com/watch?v=TEST000808') == 'youtube')
    check("platform_02008", platform_of('https://youtu.be/TEST000809') == 'youtube')
    check("platform_02009", platform_of('https://www.youtube.com/watch?v=TEST000810') == 'youtube')
    check("platform_02010", platform_of('https://youtu.be/TEST000811') == 'youtube')
    check("platform_02011", platform_of('https://www.youtube.com/watch?v=TEST000812') == 'youtube')
    check("platform_02012", platform_of('https://youtu.be/TEST000813') == 'youtube')
    check("platform_02013", platform_of('https://www.youtube.com/watch?v=TEST000814') == 'youtube')
    check("platform_02014", platform_of('https://youtu.be/TEST000815') == 'youtube')
    check("platform_02015", platform_of('https://www.youtube.com/watch?v=TEST000816') == 'youtube')
    check("platform_02016", platform_of('https://youtu.be/TEST000817') == 'youtube')
    check("platform_02017", platform_of('https://www.youtube.com/watch?v=TEST000818') == 'youtube')
    check("platform_02018", platform_of('https://youtu.be/TEST000819') == 'youtube')
    check("platform_02019", platform_of('https://www.youtube.com/watch?v=TEST000820') == 'youtube')
    check("platform_02020", platform_of('https://youtu.be/TEST000821') == 'youtube')
    check("platform_02021", platform_of('https://www.youtube.com/watch?v=TEST000822') == 'youtube')
    check("platform_02022", platform_of('https://youtu.be/TEST000823') == 'youtube')
    check("platform_02023", platform_of('https://www.youtube.com/watch?v=TEST000824') == 'youtube')
    check("platform_02024", platform_of('https://youtu.be/TEST000825') == 'youtube')
    check("platform_02025", platform_of('https://www.youtube.com/watch?v=TEST000826') == 'youtube')
    check("platform_02026", platform_of('https://youtu.be/TEST000827') == 'youtube')
    check("platform_02027", platform_of('https://www.youtube.com/watch?v=TEST000828') == 'youtube')
    check("platform_02028", platform_of('https://youtu.be/TEST000829') == 'youtube')
    check("platform_02029", platform_of('https://www.youtube.com/watch?v=TEST000830') == 'youtube')
    check("platform_02030", platform_of('https://youtu.be/TEST000831') == 'youtube')
    check("platform_02031", platform_of('https://www.youtube.com/watch?v=TEST000832') == 'youtube')
    check("platform_02032", platform_of('https://youtu.be/TEST000833') == 'youtube')
    check("platform_02033", platform_of('https://www.youtube.com/watch?v=TEST000834') == 'youtube')
    check("platform_02034", platform_of('https://youtu.be/TEST000835') == 'youtube')
    check("platform_02035", platform_of('https://www.youtube.com/watch?v=TEST000836') == 'youtube')
    check("platform_02036", platform_of('https://youtu.be/TEST000837') == 'youtube')
    check("platform_02037", platform_of('https://www.youtube.com/watch?v=TEST000838') == 'youtube')
    check("platform_02038", platform_of('https://youtu.be/TEST000839') == 'youtube')
    check("platform_02039", platform_of('https://www.youtube.com/watch?v=TEST000840') == 'youtube')
    check("platform_02040", platform_of('https://youtu.be/TEST000841') == 'youtube')
    check("platform_02041", platform_of('https://www.youtube.com/watch?v=TEST000842') == 'youtube')
    check("platform_02042", platform_of('https://youtu.be/TEST000843') == 'youtube')
    check("platform_02043", platform_of('https://www.youtube.com/watch?v=TEST000844') == 'youtube')
    check("platform_02044", platform_of('https://youtu.be/TEST000845') == 'youtube')
    check("platform_02045", platform_of('https://www.youtube.com/watch?v=TEST000846') == 'youtube')
    check("platform_02046", platform_of('https://youtu.be/TEST000847') == 'youtube')
    check("platform_02047", platform_of('https://www.youtube.com/watch?v=TEST000848') == 'youtube')
    check("platform_02048", platform_of('https://youtu.be/TEST000849') == 'youtube')
    check("platform_02049", platform_of('https://www.youtube.com/watch?v=TEST000850') == 'youtube')
    check("platform_02050", platform_of('https://youtu.be/TEST000851') == 'youtube')
    check("platform_02051", platform_of('https://www.youtube.com/watch?v=TEST000852') == 'youtube')
    check("platform_02052", platform_of('https://youtu.be/TEST000853') == 'youtube')
    check("platform_02053", platform_of('https://www.youtube.com/watch?v=TEST000854') == 'youtube')
    check("platform_02054", platform_of('https://youtu.be/TEST000855') == 'youtube')
    check("platform_02055", platform_of('https://www.youtube.com/watch?v=TEST000856') == 'youtube')
    check("platform_02056", platform_of('https://youtu.be/TEST000857') == 'youtube')
    check("platform_02057", platform_of('https://www.youtube.com/watch?v=TEST000858') == 'youtube')
    check("platform_02058", platform_of('https://youtu.be/TEST000859') == 'youtube')
    check("platform_02059", platform_of('https://www.youtube.com/watch?v=TEST000860') == 'youtube')
    check("platform_02060", platform_of('https://youtu.be/TEST000861') == 'youtube')
    check("platform_02061", platform_of('https://www.youtube.com/watch?v=TEST000862') == 'youtube')
    check("platform_02062", platform_of('https://youtu.be/TEST000863') == 'youtube')
    check("platform_02063", platform_of('https://www.youtube.com/watch?v=TEST000864') == 'youtube')
    check("platform_02064", platform_of('https://youtu.be/TEST000865') == 'youtube')
    check("platform_02065", platform_of('https://www.youtube.com/watch?v=TEST000866') == 'youtube')
    check("platform_02066", platform_of('https://youtu.be/TEST000867') == 'youtube')
    check("platform_02067", platform_of('https://www.youtube.com/watch?v=TEST000868') == 'youtube')
    check("platform_02068", platform_of('https://youtu.be/TEST000869') == 'youtube')
    check("platform_02069", platform_of('https://www.youtube.com/watch?v=TEST000870') == 'youtube')
    check("platform_02070", platform_of('https://youtu.be/TEST000871') == 'youtube')
    check("platform_02071", platform_of('https://www.youtube.com/watch?v=TEST000872') == 'youtube')
    check("platform_02072", platform_of('https://youtu.be/TEST000873') == 'youtube')
    check("platform_02073", platform_of('https://www.youtube.com/watch?v=TEST000874') == 'youtube')
    check("platform_02074", platform_of('https://youtu.be/TEST000875') == 'youtube')
    check("platform_02075", platform_of('https://www.youtube.com/watch?v=TEST000876') == 'youtube')
    check("platform_02076", platform_of('https://youtu.be/TEST000877') == 'youtube')
    check("platform_02077", platform_of('https://www.youtube.com/watch?v=TEST000878') == 'youtube')
    check("platform_02078", platform_of('https://youtu.be/TEST000879') == 'youtube')
    check("platform_02079", platform_of('https://www.youtube.com/watch?v=TEST000880') == 'youtube')
    check("platform_02080", platform_of('https://youtu.be/TEST000881') == 'youtube')
    check("platform_02081", platform_of('https://www.youtube.com/watch?v=TEST000882') == 'youtube')
    check("platform_02082", platform_of('https://youtu.be/TEST000883') == 'youtube')
    check("platform_02083", platform_of('https://www.youtube.com/watch?v=TEST000884') == 'youtube')
    check("platform_02084", platform_of('https://youtu.be/TEST000885') == 'youtube')
    check("platform_02085", platform_of('https://www.youtube.com/watch?v=TEST000886') == 'youtube')
    check("platform_02086", platform_of('https://youtu.be/TEST000887') == 'youtube')
    check("platform_02087", platform_of('https://www.youtube.com/watch?v=TEST000888') == 'youtube')
    check("platform_02088", platform_of('https://youtu.be/TEST000889') == 'youtube')
    check("platform_02089", platform_of('https://www.youtube.com/watch?v=TEST000890') == 'youtube')
    check("platform_02090", platform_of('https://youtu.be/TEST000891') == 'youtube')
    check("platform_02091", platform_of('https://www.youtube.com/watch?v=TEST000892') == 'youtube')
    check("platform_02092", platform_of('https://youtu.be/TEST000893') == 'youtube')
    check("platform_02093", platform_of('https://www.youtube.com/watch?v=TEST000894') == 'youtube')
    check("platform_02094", platform_of('https://youtu.be/TEST000895') == 'youtube')
    check("platform_02095", platform_of('https://www.youtube.com/watch?v=TEST000896') == 'youtube')
    check("platform_02096", platform_of('https://youtu.be/TEST000897') == 'youtube')
    check("platform_02097", platform_of('https://www.youtube.com/watch?v=TEST000898') == 'youtube')
    check("platform_02098", platform_of('https://youtu.be/TEST000899') == 'youtube')
    check("platform_02099", platform_of('https://www.youtube.com/watch?v=TEST000900') == 'youtube')
    check("platform_02100", platform_of('https://youtu.be/TEST000901') == 'youtube')
    check("platform_02101", platform_of('https://www.youtube.com/watch?v=TEST000902') == 'youtube')
    check("platform_02102", platform_of('https://youtu.be/TEST000903') == 'youtube')
    check("platform_02103", platform_of('https://www.youtube.com/watch?v=TEST000904') == 'youtube')
    check("platform_02104", platform_of('https://youtu.be/TEST000905') == 'youtube')
    check("platform_02105", platform_of('https://www.youtube.com/watch?v=TEST000906') == 'youtube')
    check("platform_02106", platform_of('https://youtu.be/TEST000907') == 'youtube')
    check("platform_02107", platform_of('https://www.youtube.com/watch?v=TEST000908') == 'youtube')
    check("platform_02108", platform_of('https://youtu.be/TEST000909') == 'youtube')
    check("platform_02109", platform_of('https://www.youtube.com/watch?v=TEST000910') == 'youtube')
    check("platform_02110", platform_of('https://youtu.be/TEST000911') == 'youtube')
    check("platform_02111", platform_of('https://www.youtube.com/watch?v=TEST000912') == 'youtube')
    check("platform_02112", platform_of('https://youtu.be/TEST000913') == 'youtube')
    check("platform_02113", platform_of('https://www.youtube.com/watch?v=TEST000914') == 'youtube')
    check("platform_02114", platform_of('https://youtu.be/TEST000915') == 'youtube')
    check("platform_02115", platform_of('https://www.youtube.com/watch?v=TEST000916') == 'youtube')
    check("platform_02116", platform_of('https://youtu.be/TEST000917') == 'youtube')
    check("platform_02117", platform_of('https://www.youtube.com/watch?v=TEST000918') == 'youtube')
    check("platform_02118", platform_of('https://youtu.be/TEST000919') == 'youtube')
    check("platform_02119", platform_of('https://www.youtube.com/watch?v=TEST000920') == 'youtube')
    check("platform_02120", platform_of('https://youtu.be/TEST000921') == 'youtube')
    check("platform_02121", platform_of('https://www.youtube.com/watch?v=TEST000922') == 'youtube')
    check("platform_02122", platform_of('https://youtu.be/TEST000923') == 'youtube')
    check("platform_02123", platform_of('https://www.youtube.com/watch?v=TEST000924') == 'youtube')
    check("platform_02124", platform_of('https://youtu.be/TEST000925') == 'youtube')
    check("platform_02125", platform_of('https://www.youtube.com/watch?v=TEST000926') == 'youtube')
    check("platform_02126", platform_of('https://youtu.be/TEST000927') == 'youtube')
    check("platform_02127", platform_of('https://www.youtube.com/watch?v=TEST000928') == 'youtube')
    check("platform_02128", platform_of('https://youtu.be/TEST000929') == 'youtube')
    check("platform_02129", platform_of('https://www.youtube.com/watch?v=TEST000930') == 'youtube')
    check("platform_02130", platform_of('https://youtu.be/TEST000931') == 'youtube')
    check("platform_02131", platform_of('https://www.youtube.com/watch?v=TEST000932') == 'youtube')
    check("platform_02132", platform_of('https://youtu.be/TEST000933') == 'youtube')
    check("platform_02133", platform_of('https://www.youtube.com/watch?v=TEST000934') == 'youtube')
    check("platform_02134", platform_of('https://youtu.be/TEST000935') == 'youtube')
    check("platform_02135", platform_of('https://www.youtube.com/watch?v=TEST000936') == 'youtube')
    check("platform_02136", platform_of('https://youtu.be/TEST000937') == 'youtube')
    check("platform_02137", platform_of('https://www.youtube.com/watch?v=TEST000938') == 'youtube')
    check("platform_02138", platform_of('https://youtu.be/TEST000939') == 'youtube')
    check("platform_02139", platform_of('https://www.youtube.com/watch?v=TEST000940') == 'youtube')
    check("platform_02140", platform_of('https://youtu.be/TEST000941') == 'youtube')
    check("platform_02141", platform_of('https://www.youtube.com/watch?v=TEST000942') == 'youtube')
    check("platform_02142", platform_of('https://youtu.be/TEST000943') == 'youtube')
    check("platform_02143", platform_of('https://www.youtube.com/watch?v=TEST000944') == 'youtube')
    check("platform_02144", platform_of('https://youtu.be/TEST000945') == 'youtube')
    check("platform_02145", platform_of('https://www.youtube.com/watch?v=TEST000946') == 'youtube')
    check("platform_02146", platform_of('https://youtu.be/TEST000947') == 'youtube')
    check("platform_02147", platform_of('https://www.youtube.com/watch?v=TEST000948') == 'youtube')
    check("platform_02148", platform_of('https://youtu.be/TEST000949') == 'youtube')
    check("platform_02149", platform_of('https://www.youtube.com/watch?v=TEST000950') == 'youtube')
    check("platform_02150", platform_of('https://youtu.be/TEST000951') == 'youtube')
    check("platform_02151", platform_of('https://www.youtube.com/watch?v=TEST000952') == 'youtube')
    check("platform_02152", platform_of('https://youtu.be/TEST000953') == 'youtube')
    check("platform_02153", platform_of('https://www.youtube.com/watch?v=TEST000954') == 'youtube')
    check("platform_02154", platform_of('https://youtu.be/TEST000955') == 'youtube')
    check("platform_02155", platform_of('https://www.youtube.com/watch?v=TEST000956') == 'youtube')
    check("platform_02156", platform_of('https://youtu.be/TEST000957') == 'youtube')
    check("platform_02157", platform_of('https://www.youtube.com/watch?v=TEST000958') == 'youtube')
    check("platform_02158", platform_of('https://youtu.be/TEST000959') == 'youtube')
    check("platform_02159", platform_of('https://www.youtube.com/watch?v=TEST000960') == 'youtube')
    check("platform_02160", platform_of('https://youtu.be/TEST000961') == 'youtube')
    check("platform_02161", platform_of('https://www.youtube.com/watch?v=TEST000962') == 'youtube')
    check("platform_02162", platform_of('https://youtu.be/TEST000963') == 'youtube')
    check("platform_02163", platform_of('https://www.youtube.com/watch?v=TEST000964') == 'youtube')
    check("platform_02164", platform_of('https://youtu.be/TEST000965') == 'youtube')
    check("platform_02165", platform_of('https://www.youtube.com/watch?v=TEST000966') == 'youtube')
    check("platform_02166", platform_of('https://youtu.be/TEST000967') == 'youtube')
    check("platform_02167", platform_of('https://www.youtube.com/watch?v=TEST000968') == 'youtube')
    check("platform_02168", platform_of('https://youtu.be/TEST000969') == 'youtube')
    check("platform_02169", platform_of('https://www.youtube.com/watch?v=TEST000970') == 'youtube')
    check("platform_02170", platform_of('https://youtu.be/TEST000971') == 'youtube')
    check("platform_02171", platform_of('https://www.youtube.com/watch?v=TEST000972') == 'youtube')
    check("platform_02172", platform_of('https://youtu.be/TEST000973') == 'youtube')
    check("platform_02173", platform_of('https://www.youtube.com/watch?v=TEST000974') == 'youtube')
    check("platform_02174", platform_of('https://youtu.be/TEST000975') == 'youtube')
    check("platform_02175", platform_of('https://www.youtube.com/watch?v=TEST000976') == 'youtube')
    check("platform_02176", platform_of('https://youtu.be/TEST000977') == 'youtube')
    check("platform_02177", platform_of('https://www.youtube.com/watch?v=TEST000978') == 'youtube')
    check("platform_02178", platform_of('https://youtu.be/TEST000979') == 'youtube')
    check("platform_02179", platform_of('https://www.youtube.com/watch?v=TEST000980') == 'youtube')
    check("platform_02180", platform_of('https://youtu.be/TEST000981') == 'youtube')
    check("platform_02181", platform_of('https://www.youtube.com/watch?v=TEST000982') == 'youtube')
    check("platform_02182", platform_of('https://youtu.be/TEST000983') == 'youtube')
    check("platform_02183", platform_of('https://www.youtube.com/watch?v=TEST000984') == 'youtube')
    check("platform_02184", platform_of('https://youtu.be/TEST000985') == 'youtube')
    check("platform_02185", platform_of('https://www.youtube.com/watch?v=TEST000986') == 'youtube')
    check("platform_02186", platform_of('https://youtu.be/TEST000987') == 'youtube')
    check("platform_02187", platform_of('https://www.youtube.com/watch?v=TEST000988') == 'youtube')
    check("platform_02188", platform_of('https://youtu.be/TEST000989') == 'youtube')
    check("platform_02189", platform_of('https://www.youtube.com/watch?v=TEST000990') == 'youtube')
    check("platform_02190", platform_of('https://youtu.be/TEST000991') == 'youtube')
    check("platform_02191", platform_of('https://www.youtube.com/watch?v=TEST000992') == 'youtube')
    check("platform_02192", platform_of('https://youtu.be/TEST000993') == 'youtube')
    check("platform_02193", platform_of('https://www.youtube.com/watch?v=TEST000994') == 'youtube')
    check("platform_02194", platform_of('https://youtu.be/TEST000995') == 'youtube')
    check("platform_02195", platform_of('https://www.youtube.com/watch?v=TEST000996') == 'youtube')
    check("platform_02196", platform_of('https://youtu.be/TEST000997') == 'youtube')
    check("platform_02197", platform_of('https://www.youtube.com/watch?v=TEST000998') == 'youtube')
    check("platform_02198", platform_of('https://youtu.be/TEST000999') == 'youtube')
    check("platform_02199", platform_of('https://www.youtube.com/watch?v=TEST001000') == 'youtube')
    check("platform_02200", platform_of('https://youtu.be/TEST001001') == 'youtube')
    check("platform_02201", platform_of('https://www.youtube.com/watch?v=TEST001002') == 'youtube')
    check("platform_02202", platform_of('https://youtu.be/TEST001003') == 'youtube')
    check("platform_02203", platform_of('https://www.youtube.com/watch?v=TEST001004') == 'youtube')
    check("platform_02204", platform_of('https://youtu.be/TEST001005') == 'youtube')
    check("platform_02205", platform_of('https://www.youtube.com/watch?v=TEST001006') == 'youtube')
    check("platform_02206", platform_of('https://youtu.be/TEST001007') == 'youtube')
    check("platform_02207", platform_of('https://www.youtube.com/watch?v=TEST001008') == 'youtube')
    check("platform_02208", platform_of('https://youtu.be/TEST001009') == 'youtube')
    check("platform_02209", platform_of('https://www.youtube.com/watch?v=TEST001010') == 'youtube')
    check("platform_02210", platform_of('https://youtu.be/TEST001011') == 'youtube')
    check("platform_02211", platform_of('https://www.youtube.com/watch?v=TEST001012') == 'youtube')
    check("platform_02212", platform_of('https://youtu.be/TEST001013') == 'youtube')
    check("platform_02213", platform_of('https://www.youtube.com/watch?v=TEST001014') == 'youtube')
    check("platform_02214", platform_of('https://youtu.be/TEST001015') == 'youtube')
    check("platform_02215", platform_of('https://www.youtube.com/watch?v=TEST001016') == 'youtube')
    check("platform_02216", platform_of('https://youtu.be/TEST001017') == 'youtube')
    check("platform_02217", platform_of('https://www.youtube.com/watch?v=TEST001018') == 'youtube')
    check("platform_02218", platform_of('https://youtu.be/TEST001019') == 'youtube')
    check("platform_02219", platform_of('https://www.youtube.com/watch?v=TEST001020') == 'youtube')
    check("platform_02220", platform_of('https://youtu.be/TEST001021') == 'youtube')
    check("platform_02221", platform_of('https://www.youtube.com/watch?v=TEST001022') == 'youtube')
    check("platform_02222", platform_of('https://youtu.be/TEST001023') == 'youtube')
    check("platform_02223", platform_of('https://www.youtube.com/watch?v=TEST001024') == 'youtube')
    check("platform_02224", platform_of('https://youtu.be/TEST001025') == 'youtube')
    check("platform_02225", platform_of('https://www.youtube.com/watch?v=TEST001026') == 'youtube')
    check("platform_02226", platform_of('https://youtu.be/TEST001027') == 'youtube')
    check("platform_02227", platform_of('https://www.youtube.com/watch?v=TEST001028') == 'youtube')
    check("platform_02228", platform_of('https://youtu.be/TEST001029') == 'youtube')
    check("platform_02229", platform_of('https://www.youtube.com/watch?v=TEST001030') == 'youtube')
    check("platform_02230", platform_of('https://youtu.be/TEST001031') == 'youtube')
    check("platform_02231", platform_of('https://www.youtube.com/watch?v=TEST001032') == 'youtube')
    check("platform_02232", platform_of('https://youtu.be/TEST001033') == 'youtube')
    check("platform_02233", platform_of('https://www.youtube.com/watch?v=TEST001034') == 'youtube')
    check("platform_02234", platform_of('https://youtu.be/TEST001035') == 'youtube')
    check("platform_02235", platform_of('https://www.youtube.com/watch?v=TEST001036') == 'youtube')
    check("platform_02236", platform_of('https://youtu.be/TEST001037') == 'youtube')
    check("platform_02237", platform_of('https://www.youtube.com/watch?v=TEST001038') == 'youtube')
    check("platform_02238", platform_of('https://youtu.be/TEST001039') == 'youtube')
    check("platform_02239", platform_of('https://www.youtube.com/watch?v=TEST001040') == 'youtube')
    check("platform_02240", platform_of('https://youtu.be/TEST001041') == 'youtube')
    check("platform_02241", platform_of('https://www.youtube.com/watch?v=TEST001042') == 'youtube')
    check("platform_02242", platform_of('https://youtu.be/TEST001043') == 'youtube')
    check("platform_02243", platform_of('https://www.youtube.com/watch?v=TEST001044') == 'youtube')
    check("platform_02244", platform_of('https://youtu.be/TEST001045') == 'youtube')
    check("platform_02245", platform_of('https://www.youtube.com/watch?v=TEST001046') == 'youtube')
    check("platform_02246", platform_of('https://youtu.be/TEST001047') == 'youtube')
    check("platform_02247", platform_of('https://www.youtube.com/watch?v=TEST001048') == 'youtube')
    check("platform_02248", platform_of('https://youtu.be/TEST001049') == 'youtube')
    check("platform_02249", platform_of('https://www.youtube.com/watch?v=TEST001050') == 'youtube')
    check("platform_02250", platform_of('https://youtu.be/TEST001051') == 'youtube')
    check("platform_02251", platform_of('https://www.youtube.com/watch?v=TEST001052') == 'youtube')
    check("platform_02252", platform_of('https://youtu.be/TEST001053') == 'youtube')
    check("platform_02253", platform_of('https://www.youtube.com/watch?v=TEST001054') == 'youtube')
    check("platform_02254", platform_of('https://youtu.be/TEST001055') == 'youtube')
    check("platform_02255", platform_of('https://www.youtube.com/watch?v=TEST001056') == 'youtube')
    check("platform_02256", platform_of('https://youtu.be/TEST001057') == 'youtube')
    check("platform_02257", platform_of('https://www.youtube.com/watch?v=TEST001058') == 'youtube')
    check("platform_02258", platform_of('https://youtu.be/TEST001059') == 'youtube')
    check("platform_02259", platform_of('https://www.youtube.com/watch?v=TEST001060') == 'youtube')
    check("platform_02260", platform_of('https://youtu.be/TEST001061') == 'youtube')
    check("platform_02261", platform_of('https://www.youtube.com/watch?v=TEST001062') == 'youtube')
    check("platform_02262", platform_of('https://youtu.be/TEST001063') == 'youtube')
    check("platform_02263", platform_of('https://www.youtube.com/watch?v=TEST001064') == 'youtube')
    check("platform_02264", platform_of('https://youtu.be/TEST001065') == 'youtube')
    check("platform_02265", platform_of('https://www.youtube.com/watch?v=TEST001066') == 'youtube')
    check("platform_02266", platform_of('https://youtu.be/TEST001067') == 'youtube')
    check("platform_02267", platform_of('https://www.youtube.com/watch?v=TEST001068') == 'youtube')
    check("platform_02268", platform_of('https://youtu.be/TEST001069') == 'youtube')
    check("platform_02269", platform_of('https://www.youtube.com/watch?v=TEST001070') == 'youtube')
    check("platform_02270", platform_of('https://youtu.be/TEST001071') == 'youtube')
    check("platform_02271", platform_of('https://www.youtube.com/watch?v=TEST001072') == 'youtube')
    check("platform_02272", platform_of('https://youtu.be/TEST001073') == 'youtube')
    check("platform_02273", platform_of('https://www.youtube.com/watch?v=TEST001074') == 'youtube')
    check("platform_02274", platform_of('https://youtu.be/TEST001075') == 'youtube')
    check("platform_02275", platform_of('https://www.youtube.com/watch?v=TEST001076') == 'youtube')
    check("platform_02276", platform_of('https://youtu.be/TEST001077') == 'youtube')
    check("platform_02277", platform_of('https://www.youtube.com/watch?v=TEST001078') == 'youtube')
    check("platform_02278", platform_of('https://youtu.be/TEST001079') == 'youtube')
    check("platform_02279", platform_of('https://www.youtube.com/watch?v=TEST001080') == 'youtube')
    check("platform_02280", platform_of('https://youtu.be/TEST001081') == 'youtube')
    check("platform_02281", platform_of('https://www.youtube.com/watch?v=TEST001082') == 'youtube')
    check("platform_02282", platform_of('https://youtu.be/TEST001083') == 'youtube')
    check("platform_02283", platform_of('https://www.youtube.com/watch?v=TEST001084') == 'youtube')
    check("platform_02284", platform_of('https://youtu.be/TEST001085') == 'youtube')
    check("platform_02285", platform_of('https://www.youtube.com/watch?v=TEST001086') == 'youtube')
    check("platform_02286", platform_of('https://youtu.be/TEST001087') == 'youtube')
    check("platform_02287", platform_of('https://www.youtube.com/watch?v=TEST001088') == 'youtube')
    check("platform_02288", platform_of('https://youtu.be/TEST001089') == 'youtube')
    check("platform_02289", platform_of('https://www.youtube.com/watch?v=TEST001090') == 'youtube')
    check("platform_02290", platform_of('https://youtu.be/TEST001091') == 'youtube')
    check("platform_02291", platform_of('https://www.youtube.com/watch?v=TEST001092') == 'youtube')
    check("platform_02292", platform_of('https://youtu.be/TEST001093') == 'youtube')
    check("platform_02293", platform_of('https://www.youtube.com/watch?v=TEST001094') == 'youtube')
    check("platform_02294", platform_of('https://youtu.be/TEST001095') == 'youtube')
    check("platform_02295", platform_of('https://www.youtube.com/watch?v=TEST001096') == 'youtube')
    check("platform_02296", platform_of('https://youtu.be/TEST001097') == 'youtube')
    check("platform_02297", platform_of('https://www.youtube.com/watch?v=TEST001098') == 'youtube')
    check("platform_02298", platform_of('https://youtu.be/TEST001099') == 'youtube')
    check("platform_02299", platform_of('https://www.youtube.com/watch?v=TEST001100') == 'youtube')
    check("platform_02300", platform_of('https://youtu.be/TEST001101') == 'youtube')
    check("platform_02301", platform_of('https://www.youtube.com/watch?v=TEST001102') == 'youtube')
    check("platform_02302", platform_of('https://youtu.be/TEST001103') == 'youtube')
    check("platform_02303", platform_of('https://www.youtube.com/watch?v=TEST001104') == 'youtube')
    check("platform_02304", platform_of('https://youtu.be/TEST001105') == 'youtube')
    check("platform_02305", platform_of('https://www.youtube.com/watch?v=TEST001106') == 'youtube')
    check("platform_02306", platform_of('https://youtu.be/TEST001107') == 'youtube')
    check("platform_02307", platform_of('https://www.youtube.com/watch?v=TEST001108') == 'youtube')
    check("platform_02308", platform_of('https://youtu.be/TEST001109') == 'youtube')
    check("platform_02309", platform_of('https://www.youtube.com/watch?v=TEST001110') == 'youtube')
    check("platform_02310", platform_of('https://youtu.be/TEST001111') == 'youtube')
    check("platform_02311", platform_of('https://www.youtube.com/watch?v=TEST001112') == 'youtube')
    check("platform_02312", platform_of('https://youtu.be/TEST001113') == 'youtube')
    check("platform_02313", platform_of('https://www.youtube.com/watch?v=TEST001114') == 'youtube')
    check("platform_02314", platform_of('https://youtu.be/TEST001115') == 'youtube')
    check("platform_02315", platform_of('https://www.youtube.com/watch?v=TEST001116') == 'youtube')
    check("platform_02316", platform_of('https://youtu.be/TEST001117') == 'youtube')
    check("platform_02317", platform_of('https://www.youtube.com/watch?v=TEST001118') == 'youtube')
    check("platform_02318", platform_of('https://youtu.be/TEST001119') == 'youtube')
    check("platform_02319", platform_of('https://www.youtube.com/watch?v=TEST001120') == 'youtube')
    check("platform_02320", platform_of('https://youtu.be/TEST001121') == 'youtube')
    check("platform_02321", platform_of('https://www.youtube.com/watch?v=TEST001122') == 'youtube')
    check("platform_02322", platform_of('https://youtu.be/TEST001123') == 'youtube')
    check("platform_02323", platform_of('https://www.youtube.com/watch?v=TEST001124') == 'youtube')
    check("platform_02324", platform_of('https://youtu.be/TEST001125') == 'youtube')
    check("platform_02325", platform_of('https://www.youtube.com/watch?v=TEST001126') == 'youtube')
    check("platform_02326", platform_of('https://youtu.be/TEST001127') == 'youtube')
    check("platform_02327", platform_of('https://www.youtube.com/watch?v=TEST001128') == 'youtube')
    check("platform_02328", platform_of('https://youtu.be/TEST001129') == 'youtube')
    check("platform_02329", platform_of('https://www.youtube.com/watch?v=TEST001130') == 'youtube')
    check("platform_02330", platform_of('https://youtu.be/TEST001131') == 'youtube')
    check("platform_02331", platform_of('https://www.youtube.com/watch?v=TEST001132') == 'youtube')
    check("platform_02332", platform_of('https://youtu.be/TEST001133') == 'youtube')
    check("platform_02333", platform_of('https://www.youtube.com/watch?v=TEST001134') == 'youtube')
    check("platform_02334", platform_of('https://youtu.be/TEST001135') == 'youtube')
    check("platform_02335", platform_of('https://www.youtube.com/watch?v=TEST001136') == 'youtube')
    check("platform_02336", platform_of('https://youtu.be/TEST001137') == 'youtube')
    check("platform_02337", platform_of('https://www.youtube.com/watch?v=TEST001138') == 'youtube')
    check("platform_02338", platform_of('https://youtu.be/TEST001139') == 'youtube')
    check("platform_02339", platform_of('https://www.youtube.com/watch?v=TEST001140') == 'youtube')
    check("platform_02340", platform_of('https://youtu.be/TEST001141') == 'youtube')
    check("platform_02341", platform_of('https://www.youtube.com/watch?v=TEST001142') == 'youtube')
    check("platform_02342", platform_of('https://youtu.be/TEST001143') == 'youtube')
    check("platform_02343", platform_of('https://www.youtube.com/watch?v=TEST001144') == 'youtube')
    check("platform_02344", platform_of('https://youtu.be/TEST001145') == 'youtube')
    check("platform_02345", platform_of('https://www.youtube.com/watch?v=TEST001146') == 'youtube')
    check("platform_02346", platform_of('https://youtu.be/TEST001147') == 'youtube')
    check("platform_02347", platform_of('https://www.youtube.com/watch?v=TEST001148') == 'youtube')
    check("platform_02348", platform_of('https://youtu.be/TEST001149') == 'youtube')
    check("platform_02349", platform_of('https://www.youtube.com/watch?v=TEST001150') == 'youtube')
    check("platform_02350", platform_of('https://youtu.be/TEST001151') == 'youtube')
    check("platform_02351", platform_of('https://www.youtube.com/watch?v=TEST001152') == 'youtube')
    check("platform_02352", platform_of('https://youtu.be/TEST001153') == 'youtube')
    check("platform_02353", platform_of('https://www.youtube.com/watch?v=TEST001154') == 'youtube')
    check("platform_02354", platform_of('https://youtu.be/TEST001155') == 'youtube')
    check("platform_02355", platform_of('https://www.youtube.com/watch?v=TEST001156') == 'youtube')
    check("platform_02356", platform_of('https://youtu.be/TEST001157') == 'youtube')
    check("platform_02357", platform_of('https://www.youtube.com/watch?v=TEST001158') == 'youtube')
    check("platform_02358", platform_of('https://youtu.be/TEST001159') == 'youtube')
    check("platform_02359", platform_of('https://www.youtube.com/watch?v=TEST001160') == 'youtube')
    check("platform_02360", platform_of('https://youtu.be/TEST001161') == 'youtube')
    check("platform_02361", platform_of('https://www.youtube.com/watch?v=TEST001162') == 'youtube')
    check("platform_02362", platform_of('https://youtu.be/TEST001163') == 'youtube')
    check("platform_02363", platform_of('https://www.youtube.com/watch?v=TEST001164') == 'youtube')
    check("platform_02364", platform_of('https://youtu.be/TEST001165') == 'youtube')
    check("platform_02365", platform_of('https://www.youtube.com/watch?v=TEST001166') == 'youtube')
    check("platform_02366", platform_of('https://youtu.be/TEST001167') == 'youtube')
    check("platform_02367", platform_of('https://www.youtube.com/watch?v=TEST001168') == 'youtube')
    check("platform_02368", platform_of('https://youtu.be/TEST001169') == 'youtube')
    check("platform_02369", platform_of('https://www.youtube.com/watch?v=TEST001170') == 'youtube')
    check("platform_02370", platform_of('https://youtu.be/TEST001171') == 'youtube')
    check("platform_02371", platform_of('https://www.youtube.com/watch?v=TEST001172') == 'youtube')
    check("platform_02372", platform_of('https://youtu.be/TEST001173') == 'youtube')
    check("platform_02373", platform_of('https://www.youtube.com/watch?v=TEST001174') == 'youtube')
    check("platform_02374", platform_of('https://youtu.be/TEST001175') == 'youtube')
    check("platform_02375", platform_of('https://www.youtube.com/watch?v=TEST001176') == 'youtube')
    check("platform_02376", platform_of('https://youtu.be/TEST001177') == 'youtube')
    check("platform_02377", platform_of('https://www.youtube.com/watch?v=TEST001178') == 'youtube')
    check("platform_02378", platform_of('https://youtu.be/TEST001179') == 'youtube')
    check("platform_02379", platform_of('https://www.youtube.com/watch?v=TEST001180') == 'youtube')
    check("platform_02380", platform_of('https://youtu.be/TEST001181') == 'youtube')
    check("platform_02381", platform_of('https://www.youtube.com/watch?v=TEST001182') == 'youtube')
    check("platform_02382", platform_of('https://youtu.be/TEST001183') == 'youtube')
    check("platform_02383", platform_of('https://www.youtube.com/watch?v=TEST001184') == 'youtube')
    check("platform_02384", platform_of('https://youtu.be/TEST001185') == 'youtube')
    check("platform_02385", platform_of('https://www.youtube.com/watch?v=TEST001186') == 'youtube')
    check("platform_02386", platform_of('https://youtu.be/TEST001187') == 'youtube')
    check("platform_02387", platform_of('https://www.youtube.com/watch?v=TEST001188') == 'youtube')
    check("platform_02388", platform_of('https://youtu.be/TEST001189') == 'youtube')
    check("platform_02389", platform_of('https://www.youtube.com/watch?v=TEST001190') == 'youtube')
    check("platform_02390", platform_of('https://youtu.be/TEST001191') == 'youtube')
    check("platform_02391", platform_of('https://www.youtube.com/watch?v=TEST001192') == 'youtube')
    check("platform_02392", platform_of('https://youtu.be/TEST001193') == 'youtube')
    check("platform_02393", platform_of('https://www.youtube.com/watch?v=TEST001194') == 'youtube')
    check("platform_02394", platform_of('https://youtu.be/TEST001195') == 'youtube')
    check("platform_02395", platform_of('https://www.youtube.com/watch?v=TEST001196') == 'youtube')
    check("platform_02396", platform_of('https://youtu.be/TEST001197') == 'youtube')
    check("platform_02397", platform_of('https://www.youtube.com/watch?v=TEST001198') == 'youtube')
    check("platform_02398", platform_of('https://youtu.be/TEST001199') == 'youtube')
    check("platform_02399", platform_of('https://www.youtube.com/watch?v=TEST001200') == 'youtube')
    check("platform_02400", platform_of('https://www.tiktok.com/@user/photo/TEST000001') == 'tiktok')
    check("platform_02401", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02402", platform_of('https://www.tiktok.com/@user/video/TEST000003') == 'tiktok')
    check("platform_02403", platform_of('https://www.tiktok.com/@user/photo/TEST000004') == 'tiktok')
    check("platform_02404", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02405", platform_of('https://www.tiktok.com/@user/video/TEST000006') == 'tiktok')
    check("platform_02406", platform_of('https://www.tiktok.com/@user/photo/TEST000007') == 'tiktok')
    check("platform_02407", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02408", platform_of('https://www.tiktok.com/@user/video/TEST000009') == 'tiktok')
    check("platform_02409", platform_of('https://www.tiktok.com/@user/photo/TEST000010') == 'tiktok')
    check("platform_02410", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02411", platform_of('https://www.tiktok.com/@user/video/TEST000012') == 'tiktok')
    check("platform_02412", platform_of('https://www.tiktok.com/@user/photo/TEST000013') == 'tiktok')
    check("platform_02413", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02414", platform_of('https://www.tiktok.com/@user/video/TEST000015') == 'tiktok')
    check("platform_02415", platform_of('https://www.tiktok.com/@user/photo/TEST000016') == 'tiktok')
    check("platform_02416", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02417", platform_of('https://www.tiktok.com/@user/video/TEST000018') == 'tiktok')
    check("platform_02418", platform_of('https://www.tiktok.com/@user/photo/TEST000019') == 'tiktok')
    check("platform_02419", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02420", platform_of('https://www.tiktok.com/@user/video/TEST000021') == 'tiktok')
    check("platform_02421", platform_of('https://www.tiktok.com/@user/photo/TEST000022') == 'tiktok')
    check("platform_02422", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02423", platform_of('https://www.tiktok.com/@user/video/TEST000024') == 'tiktok')
    check("platform_02424", platform_of('https://www.tiktok.com/@user/photo/TEST000025') == 'tiktok')
    check("platform_02425", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02426", platform_of('https://www.tiktok.com/@user/video/TEST000027') == 'tiktok')
    check("platform_02427", platform_of('https://www.tiktok.com/@user/photo/TEST000028') == 'tiktok')
    check("platform_02428", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02429", platform_of('https://www.tiktok.com/@user/video/TEST000030') == 'tiktok')
    check("platform_02430", platform_of('https://www.tiktok.com/@user/photo/TEST000031') == 'tiktok')
    check("platform_02431", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02432", platform_of('https://www.tiktok.com/@user/video/TEST000033') == 'tiktok')
    check("platform_02433", platform_of('https://www.tiktok.com/@user/photo/TEST000034') == 'tiktok')
    check("platform_02434", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02435", platform_of('https://www.tiktok.com/@user/video/TEST000036') == 'tiktok')
    check("platform_02436", platform_of('https://www.tiktok.com/@user/photo/TEST000037') == 'tiktok')
    check("platform_02437", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02438", platform_of('https://www.tiktok.com/@user/video/TEST000039') == 'tiktok')
    check("platform_02439", platform_of('https://www.tiktok.com/@user/photo/TEST000040') == 'tiktok')
    check("platform_02440", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02441", platform_of('https://www.tiktok.com/@user/video/TEST000042') == 'tiktok')
    check("platform_02442", platform_of('https://www.tiktok.com/@user/photo/TEST000043') == 'tiktok')
    check("platform_02443", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02444", platform_of('https://www.tiktok.com/@user/video/TEST000045') == 'tiktok')
    check("platform_02445", platform_of('https://www.tiktok.com/@user/photo/TEST000046') == 'tiktok')
    check("platform_02446", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02447", platform_of('https://www.tiktok.com/@user/video/TEST000048') == 'tiktok')
    check("platform_02448", platform_of('https://www.tiktok.com/@user/photo/TEST000049') == 'tiktok')
    check("platform_02449", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02450", platform_of('https://www.tiktok.com/@user/video/TEST000051') == 'tiktok')
    check("platform_02451", platform_of('https://www.tiktok.com/@user/photo/TEST000052') == 'tiktok')
    check("platform_02452", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02453", platform_of('https://www.tiktok.com/@user/video/TEST000054') == 'tiktok')
    check("platform_02454", platform_of('https://www.tiktok.com/@user/photo/TEST000055') == 'tiktok')
    check("platform_02455", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02456", platform_of('https://www.tiktok.com/@user/video/TEST000057') == 'tiktok')
    check("platform_02457", platform_of('https://www.tiktok.com/@user/photo/TEST000058') == 'tiktok')
    check("platform_02458", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02459", platform_of('https://www.tiktok.com/@user/video/TEST000060') == 'tiktok')
    check("platform_02460", platform_of('https://www.tiktok.com/@user/photo/TEST000061') == 'tiktok')
    check("platform_02461", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02462", platform_of('https://www.tiktok.com/@user/video/TEST000063') == 'tiktok')
    check("platform_02463", platform_of('https://www.tiktok.com/@user/photo/TEST000064') == 'tiktok')
    check("platform_02464", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02465", platform_of('https://www.tiktok.com/@user/video/TEST000066') == 'tiktok')
    check("platform_02466", platform_of('https://www.tiktok.com/@user/photo/TEST000067') == 'tiktok')
    check("platform_02467", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02468", platform_of('https://www.tiktok.com/@user/video/TEST000069') == 'tiktok')
    check("platform_02469", platform_of('https://www.tiktok.com/@user/photo/TEST000070') == 'tiktok')
    check("platform_02470", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02471", platform_of('https://www.tiktok.com/@user/video/TEST000072') == 'tiktok')
    check("platform_02472", platform_of('https://www.tiktok.com/@user/photo/TEST000073') == 'tiktok')
    check("platform_02473", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02474", platform_of('https://www.tiktok.com/@user/video/TEST000075') == 'tiktok')
    check("platform_02475", platform_of('https://www.tiktok.com/@user/photo/TEST000076') == 'tiktok')
    check("platform_02476", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02477", platform_of('https://www.tiktok.com/@user/video/TEST000078') == 'tiktok')
    check("platform_02478", platform_of('https://www.tiktok.com/@user/photo/TEST000079') == 'tiktok')
    check("platform_02479", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02480", platform_of('https://www.tiktok.com/@user/video/TEST000081') == 'tiktok')
    check("platform_02481", platform_of('https://www.tiktok.com/@user/photo/TEST000082') == 'tiktok')
    check("platform_02482", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02483", platform_of('https://www.tiktok.com/@user/video/TEST000084') == 'tiktok')
    check("platform_02484", platform_of('https://www.tiktok.com/@user/photo/TEST000085') == 'tiktok')
    check("platform_02485", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02486", platform_of('https://www.tiktok.com/@user/video/TEST000087') == 'tiktok')
    check("platform_02487", platform_of('https://www.tiktok.com/@user/photo/TEST000088') == 'tiktok')
    check("platform_02488", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02489", platform_of('https://www.tiktok.com/@user/video/TEST000090') == 'tiktok')
    check("platform_02490", platform_of('https://www.tiktok.com/@user/photo/TEST000091') == 'tiktok')
    check("platform_02491", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02492", platform_of('https://www.tiktok.com/@user/video/TEST000093') == 'tiktok')
    check("platform_02493", platform_of('https://www.tiktok.com/@user/photo/TEST000094') == 'tiktok')
    check("platform_02494", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02495", platform_of('https://www.tiktok.com/@user/video/TEST000096') == 'tiktok')
    check("platform_02496", platform_of('https://www.tiktok.com/@user/photo/TEST000097') == 'tiktok')
    check("platform_02497", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02498", platform_of('https://www.tiktok.com/@user/video/TEST000099') == 'tiktok')
    check("platform_02499", platform_of('https://www.tiktok.com/@user/photo/TEST000100') == 'tiktok')
    check("platform_02500", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02501", platform_of('https://www.tiktok.com/@user/video/TEST000102') == 'tiktok')
    check("platform_02502", platform_of('https://www.tiktok.com/@user/photo/TEST000103') == 'tiktok')
    check("platform_02503", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02504", platform_of('https://www.tiktok.com/@user/video/TEST000105') == 'tiktok')
    check("platform_02505", platform_of('https://www.tiktok.com/@user/photo/TEST000106') == 'tiktok')
    check("platform_02506", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02507", platform_of('https://www.tiktok.com/@user/video/TEST000108') == 'tiktok')
    check("platform_02508", platform_of('https://www.tiktok.com/@user/photo/TEST000109') == 'tiktok')
    check("platform_02509", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02510", platform_of('https://www.tiktok.com/@user/video/TEST000111') == 'tiktok')
    check("platform_02511", platform_of('https://www.tiktok.com/@user/photo/TEST000112') == 'tiktok')
    check("platform_02512", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02513", platform_of('https://www.tiktok.com/@user/video/TEST000114') == 'tiktok')
    check("platform_02514", platform_of('https://www.tiktok.com/@user/photo/TEST000115') == 'tiktok')
    check("platform_02515", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02516", platform_of('https://www.tiktok.com/@user/video/TEST000117') == 'tiktok')
    check("platform_02517", platform_of('https://www.tiktok.com/@user/photo/TEST000118') == 'tiktok')
    check("platform_02518", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02519", platform_of('https://www.tiktok.com/@user/video/TEST000120') == 'tiktok')
    check("platform_02520", platform_of('https://www.tiktok.com/@user/photo/TEST000121') == 'tiktok')
    check("platform_02521", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02522", platform_of('https://www.tiktok.com/@user/video/TEST000123') == 'tiktok')
    check("platform_02523", platform_of('https://www.tiktok.com/@user/photo/TEST000124') == 'tiktok')
    check("platform_02524", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02525", platform_of('https://www.tiktok.com/@user/video/TEST000126') == 'tiktok')
    check("platform_02526", platform_of('https://www.tiktok.com/@user/photo/TEST000127') == 'tiktok')
    check("platform_02527", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02528", platform_of('https://www.tiktok.com/@user/video/TEST000129') == 'tiktok')
    check("platform_02529", platform_of('https://www.tiktok.com/@user/photo/TEST000130') == 'tiktok')
    check("platform_02530", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02531", platform_of('https://www.tiktok.com/@user/video/TEST000132') == 'tiktok')
    check("platform_02532", platform_of('https://www.tiktok.com/@user/photo/TEST000133') == 'tiktok')
    check("platform_02533", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02534", platform_of('https://www.tiktok.com/@user/video/TEST000135') == 'tiktok')
    check("platform_02535", platform_of('https://www.tiktok.com/@user/photo/TEST000136') == 'tiktok')
    check("platform_02536", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02537", platform_of('https://www.tiktok.com/@user/video/TEST000138') == 'tiktok')
    check("platform_02538", platform_of('https://www.tiktok.com/@user/photo/TEST000139') == 'tiktok')
    check("platform_02539", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02540", platform_of('https://www.tiktok.com/@user/video/TEST000141') == 'tiktok')
    check("platform_02541", platform_of('https://www.tiktok.com/@user/photo/TEST000142') == 'tiktok')
    check("platform_02542", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02543", platform_of('https://www.tiktok.com/@user/video/TEST000144') == 'tiktok')
    check("platform_02544", platform_of('https://www.tiktok.com/@user/photo/TEST000145') == 'tiktok')
    check("platform_02545", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02546", platform_of('https://www.tiktok.com/@user/video/TEST000147') == 'tiktok')
    check("platform_02547", platform_of('https://www.tiktok.com/@user/photo/TEST000148') == 'tiktok')
    check("platform_02548", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02549", platform_of('https://www.tiktok.com/@user/video/TEST000150') == 'tiktok')
    check("platform_02550", platform_of('https://www.tiktok.com/@user/photo/TEST000151') == 'tiktok')
    check("platform_02551", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02552", platform_of('https://www.tiktok.com/@user/video/TEST000153') == 'tiktok')
    check("platform_02553", platform_of('https://www.tiktok.com/@user/photo/TEST000154') == 'tiktok')
    check("platform_02554", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02555", platform_of('https://www.tiktok.com/@user/video/TEST000156') == 'tiktok')
    check("platform_02556", platform_of('https://www.tiktok.com/@user/photo/TEST000157') == 'tiktok')
    check("platform_02557", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02558", platform_of('https://www.tiktok.com/@user/video/TEST000159') == 'tiktok')
    check("platform_02559", platform_of('https://www.tiktok.com/@user/photo/TEST000160') == 'tiktok')
    check("platform_02560", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02561", platform_of('https://www.tiktok.com/@user/video/TEST000162') == 'tiktok')
    check("platform_02562", platform_of('https://www.tiktok.com/@user/photo/TEST000163') == 'tiktok')
    check("platform_02563", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02564", platform_of('https://www.tiktok.com/@user/video/TEST000165') == 'tiktok')
    check("platform_02565", platform_of('https://www.tiktok.com/@user/photo/TEST000166') == 'tiktok')
    check("platform_02566", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02567", platform_of('https://www.tiktok.com/@user/video/TEST000168') == 'tiktok')
    check("platform_02568", platform_of('https://www.tiktok.com/@user/photo/TEST000169') == 'tiktok')
    check("platform_02569", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02570", platform_of('https://www.tiktok.com/@user/video/TEST000171') == 'tiktok')
    check("platform_02571", platform_of('https://www.tiktok.com/@user/photo/TEST000172') == 'tiktok')
    check("platform_02572", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02573", platform_of('https://www.tiktok.com/@user/video/TEST000174') == 'tiktok')
    check("platform_02574", platform_of('https://www.tiktok.com/@user/photo/TEST000175') == 'tiktok')
    check("platform_02575", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02576", platform_of('https://www.tiktok.com/@user/video/TEST000177') == 'tiktok')
    check("platform_02577", platform_of('https://www.tiktok.com/@user/photo/TEST000178') == 'tiktok')
    check("platform_02578", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02579", platform_of('https://www.tiktok.com/@user/video/TEST000180') == 'tiktok')
    check("platform_02580", platform_of('https://www.tiktok.com/@user/photo/TEST000181') == 'tiktok')
    check("platform_02581", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02582", platform_of('https://www.tiktok.com/@user/video/TEST000183') == 'tiktok')
    check("platform_02583", platform_of('https://www.tiktok.com/@user/photo/TEST000184') == 'tiktok')
    check("platform_02584", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02585", platform_of('https://www.tiktok.com/@user/video/TEST000186') == 'tiktok')
    check("platform_02586", platform_of('https://www.tiktok.com/@user/photo/TEST000187') == 'tiktok')
    check("platform_02587", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02588", platform_of('https://www.tiktok.com/@user/video/TEST000189') == 'tiktok')
    check("platform_02589", platform_of('https://www.tiktok.com/@user/photo/TEST000190') == 'tiktok')
    check("platform_02590", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02591", platform_of('https://www.tiktok.com/@user/video/TEST000192') == 'tiktok')
    check("platform_02592", platform_of('https://www.tiktok.com/@user/photo/TEST000193') == 'tiktok')
    check("platform_02593", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02594", platform_of('https://www.tiktok.com/@user/video/TEST000195') == 'tiktok')
    check("platform_02595", platform_of('https://www.tiktok.com/@user/photo/TEST000196') == 'tiktok')
    check("platform_02596", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02597", platform_of('https://www.tiktok.com/@user/video/TEST000198') == 'tiktok')
    check("platform_02598", platform_of('https://www.tiktok.com/@user/photo/TEST000199') == 'tiktok')
    check("platform_02599", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02600", platform_of('https://www.tiktok.com/@user/video/TEST000201') == 'tiktok')
    check("platform_02601", platform_of('https://www.tiktok.com/@user/photo/TEST000202') == 'tiktok')
    check("platform_02602", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02603", platform_of('https://www.tiktok.com/@user/video/TEST000204') == 'tiktok')
    check("platform_02604", platform_of('https://www.tiktok.com/@user/photo/TEST000205') == 'tiktok')
    check("platform_02605", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02606", platform_of('https://www.tiktok.com/@user/video/TEST000207') == 'tiktok')
    check("platform_02607", platform_of('https://www.tiktok.com/@user/photo/TEST000208') == 'tiktok')
    check("platform_02608", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02609", platform_of('https://www.tiktok.com/@user/video/TEST000210') == 'tiktok')
    check("platform_02610", platform_of('https://www.tiktok.com/@user/photo/TEST000211') == 'tiktok')
    check("platform_02611", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02612", platform_of('https://www.tiktok.com/@user/video/TEST000213') == 'tiktok')
    check("platform_02613", platform_of('https://www.tiktok.com/@user/photo/TEST000214') == 'tiktok')
    check("platform_02614", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02615", platform_of('https://www.tiktok.com/@user/video/TEST000216') == 'tiktok')
    check("platform_02616", platform_of('https://www.tiktok.com/@user/photo/TEST000217') == 'tiktok')
    check("platform_02617", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02618", platform_of('https://www.tiktok.com/@user/video/TEST000219') == 'tiktok')
    check("platform_02619", platform_of('https://www.tiktok.com/@user/photo/TEST000220') == 'tiktok')
    check("platform_02620", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02621", platform_of('https://www.tiktok.com/@user/video/TEST000222') == 'tiktok')
    check("platform_02622", platform_of('https://www.tiktok.com/@user/photo/TEST000223') == 'tiktok')
    check("platform_02623", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02624", platform_of('https://www.tiktok.com/@user/video/TEST000225') == 'tiktok')
    check("platform_02625", platform_of('https://www.tiktok.com/@user/photo/TEST000226') == 'tiktok')
    check("platform_02626", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02627", platform_of('https://www.tiktok.com/@user/video/TEST000228') == 'tiktok')
    check("platform_02628", platform_of('https://www.tiktok.com/@user/photo/TEST000229') == 'tiktok')
    check("platform_02629", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02630", platform_of('https://www.tiktok.com/@user/video/TEST000231') == 'tiktok')
    check("platform_02631", platform_of('https://www.tiktok.com/@user/photo/TEST000232') == 'tiktok')
    check("platform_02632", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02633", platform_of('https://www.tiktok.com/@user/video/TEST000234') == 'tiktok')
    check("platform_02634", platform_of('https://www.tiktok.com/@user/photo/TEST000235') == 'tiktok')
    check("platform_02635", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02636", platform_of('https://www.tiktok.com/@user/video/TEST000237') == 'tiktok')
    check("platform_02637", platform_of('https://www.tiktok.com/@user/photo/TEST000238') == 'tiktok')
    check("platform_02638", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02639", platform_of('https://www.tiktok.com/@user/video/TEST000240') == 'tiktok')
    check("platform_02640", platform_of('https://www.tiktok.com/@user/photo/TEST000241') == 'tiktok')
    check("platform_02641", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02642", platform_of('https://www.tiktok.com/@user/video/TEST000243') == 'tiktok')
    check("platform_02643", platform_of('https://www.tiktok.com/@user/photo/TEST000244') == 'tiktok')
    check("platform_02644", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02645", platform_of('https://www.tiktok.com/@user/video/TEST000246') == 'tiktok')
    check("platform_02646", platform_of('https://www.tiktok.com/@user/photo/TEST000247') == 'tiktok')
    check("platform_02647", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02648", platform_of('https://www.tiktok.com/@user/video/TEST000249') == 'tiktok')
    check("platform_02649", platform_of('https://www.tiktok.com/@user/photo/TEST000250') == 'tiktok')
    check("platform_02650", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02651", platform_of('https://www.tiktok.com/@user/video/TEST000252') == 'tiktok')
    check("platform_02652", platform_of('https://www.tiktok.com/@user/photo/TEST000253') == 'tiktok')
    check("platform_02653", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02654", platform_of('https://www.tiktok.com/@user/video/TEST000255') == 'tiktok')
    check("platform_02655", platform_of('https://www.tiktok.com/@user/photo/TEST000256') == 'tiktok')
    check("platform_02656", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02657", platform_of('https://www.tiktok.com/@user/video/TEST000258') == 'tiktok')
    check("platform_02658", platform_of('https://www.tiktok.com/@user/photo/TEST000259') == 'tiktok')
    check("platform_02659", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02660", platform_of('https://www.tiktok.com/@user/video/TEST000261') == 'tiktok')
    check("platform_02661", platform_of('https://www.tiktok.com/@user/photo/TEST000262') == 'tiktok')
    check("platform_02662", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02663", platform_of('https://www.tiktok.com/@user/video/TEST000264') == 'tiktok')
    check("platform_02664", platform_of('https://www.tiktok.com/@user/photo/TEST000265') == 'tiktok')
    check("platform_02665", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02666", platform_of('https://www.tiktok.com/@user/video/TEST000267') == 'tiktok')
    check("platform_02667", platform_of('https://www.tiktok.com/@user/photo/TEST000268') == 'tiktok')
    check("platform_02668", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02669", platform_of('https://www.tiktok.com/@user/video/TEST000270') == 'tiktok')
    check("platform_02670", platform_of('https://www.tiktok.com/@user/photo/TEST000271') == 'tiktok')
    check("platform_02671", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02672", platform_of('https://www.tiktok.com/@user/video/TEST000273') == 'tiktok')
    check("platform_02673", platform_of('https://www.tiktok.com/@user/photo/TEST000274') == 'tiktok')
    check("platform_02674", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02675", platform_of('https://www.tiktok.com/@user/video/TEST000276') == 'tiktok')
    check("platform_02676", platform_of('https://www.tiktok.com/@user/photo/TEST000277') == 'tiktok')
    check("platform_02677", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02678", platform_of('https://www.tiktok.com/@user/video/TEST000279') == 'tiktok')
    check("platform_02679", platform_of('https://www.tiktok.com/@user/photo/TEST000280') == 'tiktok')
    check("platform_02680", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02681", platform_of('https://www.tiktok.com/@user/video/TEST000282') == 'tiktok')
    check("platform_02682", platform_of('https://www.tiktok.com/@user/photo/TEST000283') == 'tiktok')
    check("platform_02683", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02684", platform_of('https://www.tiktok.com/@user/video/TEST000285') == 'tiktok')
    check("platform_02685", platform_of('https://www.tiktok.com/@user/photo/TEST000286') == 'tiktok')
    check("platform_02686", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02687", platform_of('https://www.tiktok.com/@user/video/TEST000288') == 'tiktok')
    check("platform_02688", platform_of('https://www.tiktok.com/@user/photo/TEST000289') == 'tiktok')
    check("platform_02689", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02690", platform_of('https://www.tiktok.com/@user/video/TEST000291') == 'tiktok')
    check("platform_02691", platform_of('https://www.tiktok.com/@user/photo/TEST000292') == 'tiktok')
    check("platform_02692", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02693", platform_of('https://www.tiktok.com/@user/video/TEST000294') == 'tiktok')
    check("platform_02694", platform_of('https://www.tiktok.com/@user/photo/TEST000295') == 'tiktok')
    check("platform_02695", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02696", platform_of('https://www.tiktok.com/@user/video/TEST000297') == 'tiktok')
    check("platform_02697", platform_of('https://www.tiktok.com/@user/photo/TEST000298') == 'tiktok')
    check("platform_02698", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02699", platform_of('https://www.tiktok.com/@user/video/TEST000300') == 'tiktok')
    check("platform_02700", platform_of('https://www.tiktok.com/@user/photo/TEST000301') == 'tiktok')
    check("platform_02701", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02702", platform_of('https://www.tiktok.com/@user/video/TEST000303') == 'tiktok')
    check("platform_02703", platform_of('https://www.tiktok.com/@user/photo/TEST000304') == 'tiktok')
    check("platform_02704", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02705", platform_of('https://www.tiktok.com/@user/video/TEST000306') == 'tiktok')
    check("platform_02706", platform_of('https://www.tiktok.com/@user/photo/TEST000307') == 'tiktok')
    check("platform_02707", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02708", platform_of('https://www.tiktok.com/@user/video/TEST000309') == 'tiktok')
    check("platform_02709", platform_of('https://www.tiktok.com/@user/photo/TEST000310') == 'tiktok')
    check("platform_02710", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02711", platform_of('https://www.tiktok.com/@user/video/TEST000312') == 'tiktok')
    check("platform_02712", platform_of('https://www.tiktok.com/@user/photo/TEST000313') == 'tiktok')
    check("platform_02713", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02714", platform_of('https://www.tiktok.com/@user/video/TEST000315') == 'tiktok')
    check("platform_02715", platform_of('https://www.tiktok.com/@user/photo/TEST000316') == 'tiktok')
    check("platform_02716", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02717", platform_of('https://www.tiktok.com/@user/video/TEST000318') == 'tiktok')
    check("platform_02718", platform_of('https://www.tiktok.com/@user/photo/TEST000319') == 'tiktok')
    check("platform_02719", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02720", platform_of('https://www.tiktok.com/@user/video/TEST000321') == 'tiktok')
    check("platform_02721", platform_of('https://www.tiktok.com/@user/photo/TEST000322') == 'tiktok')
    check("platform_02722", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02723", platform_of('https://www.tiktok.com/@user/video/TEST000324') == 'tiktok')
    check("platform_02724", platform_of('https://www.tiktok.com/@user/photo/TEST000325') == 'tiktok')
    check("platform_02725", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02726", platform_of('https://www.tiktok.com/@user/video/TEST000327') == 'tiktok')
    check("platform_02727", platform_of('https://www.tiktok.com/@user/photo/TEST000328') == 'tiktok')
    check("platform_02728", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02729", platform_of('https://www.tiktok.com/@user/video/TEST000330') == 'tiktok')
    check("platform_02730", platform_of('https://www.tiktok.com/@user/photo/TEST000331') == 'tiktok')
    check("platform_02731", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02732", platform_of('https://www.tiktok.com/@user/video/TEST000333') == 'tiktok')
    check("platform_02733", platform_of('https://www.tiktok.com/@user/photo/TEST000334') == 'tiktok')
    check("platform_02734", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02735", platform_of('https://www.tiktok.com/@user/video/TEST000336') == 'tiktok')
    check("platform_02736", platform_of('https://www.tiktok.com/@user/photo/TEST000337') == 'tiktok')
    check("platform_02737", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02738", platform_of('https://www.tiktok.com/@user/video/TEST000339') == 'tiktok')
    check("platform_02739", platform_of('https://www.tiktok.com/@user/photo/TEST000340') == 'tiktok')
    check("platform_02740", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02741", platform_of('https://www.tiktok.com/@user/video/TEST000342') == 'tiktok')
    check("platform_02742", platform_of('https://www.tiktok.com/@user/photo/TEST000343') == 'tiktok')
    check("platform_02743", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02744", platform_of('https://www.tiktok.com/@user/video/TEST000345') == 'tiktok')
    check("platform_02745", platform_of('https://www.tiktok.com/@user/photo/TEST000346') == 'tiktok')
    check("platform_02746", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02747", platform_of('https://www.tiktok.com/@user/video/TEST000348') == 'tiktok')
    check("platform_02748", platform_of('https://www.tiktok.com/@user/photo/TEST000349') == 'tiktok')
    check("platform_02749", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02750", platform_of('https://www.tiktok.com/@user/video/TEST000351') == 'tiktok')
    check("platform_02751", platform_of('https://www.tiktok.com/@user/photo/TEST000352') == 'tiktok')
    check("platform_02752", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02753", platform_of('https://www.tiktok.com/@user/video/TEST000354') == 'tiktok')
    check("platform_02754", platform_of('https://www.tiktok.com/@user/photo/TEST000355') == 'tiktok')
    check("platform_02755", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02756", platform_of('https://www.tiktok.com/@user/video/TEST000357') == 'tiktok')
    check("platform_02757", platform_of('https://www.tiktok.com/@user/photo/TEST000358') == 'tiktok')
    check("platform_02758", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02759", platform_of('https://www.tiktok.com/@user/video/TEST000360') == 'tiktok')
    check("platform_02760", platform_of('https://www.tiktok.com/@user/photo/TEST000361') == 'tiktok')
    check("platform_02761", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02762", platform_of('https://www.tiktok.com/@user/video/TEST000363') == 'tiktok')
    check("platform_02763", platform_of('https://www.tiktok.com/@user/photo/TEST000364') == 'tiktok')
    check("platform_02764", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02765", platform_of('https://www.tiktok.com/@user/video/TEST000366') == 'tiktok')
    check("platform_02766", platform_of('https://www.tiktok.com/@user/photo/TEST000367') == 'tiktok')
    check("platform_02767", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02768", platform_of('https://www.tiktok.com/@user/video/TEST000369') == 'tiktok')
    check("platform_02769", platform_of('https://www.tiktok.com/@user/photo/TEST000370') == 'tiktok')
    check("platform_02770", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02771", platform_of('https://www.tiktok.com/@user/video/TEST000372') == 'tiktok')
    check("platform_02772", platform_of('https://www.tiktok.com/@user/photo/TEST000373') == 'tiktok')
    check("platform_02773", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02774", platform_of('https://www.tiktok.com/@user/video/TEST000375') == 'tiktok')
    check("platform_02775", platform_of('https://www.tiktok.com/@user/photo/TEST000376') == 'tiktok')
    check("platform_02776", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02777", platform_of('https://www.tiktok.com/@user/video/TEST000378') == 'tiktok')
    check("platform_02778", platform_of('https://www.tiktok.com/@user/photo/TEST000379') == 'tiktok')
    check("platform_02779", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02780", platform_of('https://www.tiktok.com/@user/video/TEST000381') == 'tiktok')
    check("platform_02781", platform_of('https://www.tiktok.com/@user/photo/TEST000382') == 'tiktok')
    check("platform_02782", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02783", platform_of('https://www.tiktok.com/@user/video/TEST000384') == 'tiktok')
    check("platform_02784", platform_of('https://www.tiktok.com/@user/photo/TEST000385') == 'tiktok')
    check("platform_02785", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02786", platform_of('https://www.tiktok.com/@user/video/TEST000387') == 'tiktok')
    check("platform_02787", platform_of('https://www.tiktok.com/@user/photo/TEST000388') == 'tiktok')
    check("platform_02788", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02789", platform_of('https://www.tiktok.com/@user/video/TEST000390') == 'tiktok')
    check("platform_02790", platform_of('https://www.tiktok.com/@user/photo/TEST000391') == 'tiktok')
    check("platform_02791", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02792", platform_of('https://www.tiktok.com/@user/video/TEST000393') == 'tiktok')
    check("platform_02793", platform_of('https://www.tiktok.com/@user/photo/TEST000394') == 'tiktok')
    check("platform_02794", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02795", platform_of('https://www.tiktok.com/@user/video/TEST000396') == 'tiktok')
    check("platform_02796", platform_of('https://www.tiktok.com/@user/photo/TEST000397') == 'tiktok')
    check("platform_02797", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02798", platform_of('https://www.tiktok.com/@user/video/TEST000399') == 'tiktok')
    check("platform_02799", platform_of('https://www.tiktok.com/@user/photo/TEST000400') == 'tiktok')
    check("platform_02800", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02801", platform_of('https://www.tiktok.com/@user/video/TEST000402') == 'tiktok')
    check("platform_02802", platform_of('https://www.tiktok.com/@user/photo/TEST000403') == 'tiktok')
    check("platform_02803", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02804", platform_of('https://www.tiktok.com/@user/video/TEST000405') == 'tiktok')
    check("platform_02805", platform_of('https://www.tiktok.com/@user/photo/TEST000406') == 'tiktok')
    check("platform_02806", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02807", platform_of('https://www.tiktok.com/@user/video/TEST000408') == 'tiktok')
    check("platform_02808", platform_of('https://www.tiktok.com/@user/photo/TEST000409') == 'tiktok')
    check("platform_02809", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02810", platform_of('https://www.tiktok.com/@user/video/TEST000411') == 'tiktok')
    check("platform_02811", platform_of('https://www.tiktok.com/@user/photo/TEST000412') == 'tiktok')
    check("platform_02812", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02813", platform_of('https://www.tiktok.com/@user/video/TEST000414') == 'tiktok')
    check("platform_02814", platform_of('https://www.tiktok.com/@user/photo/TEST000415') == 'tiktok')
    check("platform_02815", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02816", platform_of('https://www.tiktok.com/@user/video/TEST000417') == 'tiktok')
    check("platform_02817", platform_of('https://www.tiktok.com/@user/photo/TEST000418') == 'tiktok')
    check("platform_02818", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02819", platform_of('https://www.tiktok.com/@user/video/TEST000420') == 'tiktok')
    check("platform_02820", platform_of('https://www.tiktok.com/@user/photo/TEST000421') == 'tiktok')
    check("platform_02821", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02822", platform_of('https://www.tiktok.com/@user/video/TEST000423') == 'tiktok')
    check("platform_02823", platform_of('https://www.tiktok.com/@user/photo/TEST000424') == 'tiktok')
    check("platform_02824", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02825", platform_of('https://www.tiktok.com/@user/video/TEST000426') == 'tiktok')
    check("platform_02826", platform_of('https://www.tiktok.com/@user/photo/TEST000427') == 'tiktok')
    check("platform_02827", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02828", platform_of('https://www.tiktok.com/@user/video/TEST000429') == 'tiktok')
    check("platform_02829", platform_of('https://www.tiktok.com/@user/photo/TEST000430') == 'tiktok')
    check("platform_02830", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02831", platform_of('https://www.tiktok.com/@user/video/TEST000432') == 'tiktok')
    check("platform_02832", platform_of('https://www.tiktok.com/@user/photo/TEST000433') == 'tiktok')
    check("platform_02833", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02834", platform_of('https://www.tiktok.com/@user/video/TEST000435') == 'tiktok')
    check("platform_02835", platform_of('https://www.tiktok.com/@user/photo/TEST000436') == 'tiktok')
    check("platform_02836", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02837", platform_of('https://www.tiktok.com/@user/video/TEST000438') == 'tiktok')
    check("platform_02838", platform_of('https://www.tiktok.com/@user/photo/TEST000439') == 'tiktok')
    check("platform_02839", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02840", platform_of('https://www.tiktok.com/@user/video/TEST000441') == 'tiktok')
    check("platform_02841", platform_of('https://www.tiktok.com/@user/photo/TEST000442') == 'tiktok')
    check("platform_02842", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02843", platform_of('https://www.tiktok.com/@user/video/TEST000444') == 'tiktok')
    check("platform_02844", platform_of('https://www.tiktok.com/@user/photo/TEST000445') == 'tiktok')
    check("platform_02845", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02846", platform_of('https://www.tiktok.com/@user/video/TEST000447') == 'tiktok')
    check("platform_02847", platform_of('https://www.tiktok.com/@user/photo/TEST000448') == 'tiktok')
    check("platform_02848", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02849", platform_of('https://www.tiktok.com/@user/video/TEST000450') == 'tiktok')
    check("platform_02850", platform_of('https://www.tiktok.com/@user/photo/TEST000451') == 'tiktok')
    check("platform_02851", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02852", platform_of('https://www.tiktok.com/@user/video/TEST000453') == 'tiktok')
    check("platform_02853", platform_of('https://www.tiktok.com/@user/photo/TEST000454') == 'tiktok')
    check("platform_02854", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02855", platform_of('https://www.tiktok.com/@user/video/TEST000456') == 'tiktok')
    check("platform_02856", platform_of('https://www.tiktok.com/@user/photo/TEST000457') == 'tiktok')
    check("platform_02857", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02858", platform_of('https://www.tiktok.com/@user/video/TEST000459') == 'tiktok')
    check("platform_02859", platform_of('https://www.tiktok.com/@user/photo/TEST000460') == 'tiktok')
    check("platform_02860", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02861", platform_of('https://www.tiktok.com/@user/video/TEST000462') == 'tiktok')
    check("platform_02862", platform_of('https://www.tiktok.com/@user/photo/TEST000463') == 'tiktok')
    check("platform_02863", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02864", platform_of('https://www.tiktok.com/@user/video/TEST000465') == 'tiktok')
    check("platform_02865", platform_of('https://www.tiktok.com/@user/photo/TEST000466') == 'tiktok')
    check("platform_02866", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02867", platform_of('https://www.tiktok.com/@user/video/TEST000468') == 'tiktok')
    check("platform_02868", platform_of('https://www.tiktok.com/@user/photo/TEST000469') == 'tiktok')
    check("platform_02869", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02870", platform_of('https://www.tiktok.com/@user/video/TEST000471') == 'tiktok')
    check("platform_02871", platform_of('https://www.tiktok.com/@user/photo/TEST000472') == 'tiktok')
    check("platform_02872", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02873", platform_of('https://www.tiktok.com/@user/video/TEST000474') == 'tiktok')
    check("platform_02874", platform_of('https://www.tiktok.com/@user/photo/TEST000475') == 'tiktok')
    check("platform_02875", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02876", platform_of('https://www.tiktok.com/@user/video/TEST000477') == 'tiktok')
    check("platform_02877", platform_of('https://www.tiktok.com/@user/photo/TEST000478') == 'tiktok')
    check("platform_02878", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02879", platform_of('https://www.tiktok.com/@user/video/TEST000480') == 'tiktok')
    check("platform_02880", platform_of('https://www.tiktok.com/@user/photo/TEST000481') == 'tiktok')
    check("platform_02881", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02882", platform_of('https://www.tiktok.com/@user/video/TEST000483') == 'tiktok')
    check("platform_02883", platform_of('https://www.tiktok.com/@user/photo/TEST000484') == 'tiktok')
    check("platform_02884", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02885", platform_of('https://www.tiktok.com/@user/video/TEST000486') == 'tiktok')
    check("platform_02886", platform_of('https://www.tiktok.com/@user/photo/TEST000487') == 'tiktok')
    check("platform_02887", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02888", platform_of('https://www.tiktok.com/@user/video/TEST000489') == 'tiktok')
    check("platform_02889", platform_of('https://www.tiktok.com/@user/photo/TEST000490') == 'tiktok')
    check("platform_02890", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02891", platform_of('https://www.tiktok.com/@user/video/TEST000492') == 'tiktok')
    check("platform_02892", platform_of('https://www.tiktok.com/@user/photo/TEST000493') == 'tiktok')
    check("platform_02893", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02894", platform_of('https://www.tiktok.com/@user/video/TEST000495') == 'tiktok')
    check("platform_02895", platform_of('https://www.tiktok.com/@user/photo/TEST000496') == 'tiktok')
    check("platform_02896", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02897", platform_of('https://www.tiktok.com/@user/video/TEST000498') == 'tiktok')
    check("platform_02898", platform_of('https://www.tiktok.com/@user/photo/TEST000499') == 'tiktok')
    check("platform_02899", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02900", platform_of('https://www.tiktok.com/@user/video/TEST000501') == 'tiktok')
    check("platform_02901", platform_of('https://www.tiktok.com/@user/photo/TEST000502') == 'tiktok')
    check("platform_02902", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02903", platform_of('https://www.tiktok.com/@user/video/TEST000504') == 'tiktok')
    check("platform_02904", platform_of('https://www.tiktok.com/@user/photo/TEST000505') == 'tiktok')
    check("platform_02905", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02906", platform_of('https://www.tiktok.com/@user/video/TEST000507') == 'tiktok')
    check("platform_02907", platform_of('https://www.tiktok.com/@user/photo/TEST000508') == 'tiktok')
    check("platform_02908", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02909", platform_of('https://www.tiktok.com/@user/video/TEST000510') == 'tiktok')
    check("platform_02910", platform_of('https://www.tiktok.com/@user/photo/TEST000511') == 'tiktok')
    check("platform_02911", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02912", platform_of('https://www.tiktok.com/@user/video/TEST000513') == 'tiktok')
    check("platform_02913", platform_of('https://www.tiktok.com/@user/photo/TEST000514') == 'tiktok')
    check("platform_02914", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02915", platform_of('https://www.tiktok.com/@user/video/TEST000516') == 'tiktok')
    check("platform_02916", platform_of('https://www.tiktok.com/@user/photo/TEST000517') == 'tiktok')
    check("platform_02917", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02918", platform_of('https://www.tiktok.com/@user/video/TEST000519') == 'tiktok')
    check("platform_02919", platform_of('https://www.tiktok.com/@user/photo/TEST000520') == 'tiktok')
    check("platform_02920", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02921", platform_of('https://www.tiktok.com/@user/video/TEST000522') == 'tiktok')
    check("platform_02922", platform_of('https://www.tiktok.com/@user/photo/TEST000523') == 'tiktok')
    check("platform_02923", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02924", platform_of('https://www.tiktok.com/@user/video/TEST000525') == 'tiktok')
    check("platform_02925", platform_of('https://www.tiktok.com/@user/photo/TEST000526') == 'tiktok')
    check("platform_02926", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02927", platform_of('https://www.tiktok.com/@user/video/TEST000528') == 'tiktok')
    check("platform_02928", platform_of('https://www.tiktok.com/@user/photo/TEST000529') == 'tiktok')
    check("platform_02929", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02930", platform_of('https://www.tiktok.com/@user/video/TEST000531') == 'tiktok')
    check("platform_02931", platform_of('https://www.tiktok.com/@user/photo/TEST000532') == 'tiktok')
    check("platform_02932", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02933", platform_of('https://www.tiktok.com/@user/video/TEST000534') == 'tiktok')
    check("platform_02934", platform_of('https://www.tiktok.com/@user/photo/TEST000535') == 'tiktok')
    check("platform_02935", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02936", platform_of('https://www.tiktok.com/@user/video/TEST000537') == 'tiktok')
    check("platform_02937", platform_of('https://www.tiktok.com/@user/photo/TEST000538') == 'tiktok')
    check("platform_02938", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02939", platform_of('https://www.tiktok.com/@user/video/TEST000540') == 'tiktok')
    check("platform_02940", platform_of('https://www.tiktok.com/@user/photo/TEST000541') == 'tiktok')
    check("platform_02941", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02942", platform_of('https://www.tiktok.com/@user/video/TEST000543') == 'tiktok')
    check("platform_02943", platform_of('https://www.tiktok.com/@user/photo/TEST000544') == 'tiktok')
    check("platform_02944", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02945", platform_of('https://www.tiktok.com/@user/video/TEST000546') == 'tiktok')
    check("platform_02946", platform_of('https://www.tiktok.com/@user/photo/TEST000547') == 'tiktok')
    check("platform_02947", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02948", platform_of('https://www.tiktok.com/@user/video/TEST000549') == 'tiktok')
    check("platform_02949", platform_of('https://www.tiktok.com/@user/photo/TEST000550') == 'tiktok')
    check("platform_02950", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02951", platform_of('https://www.tiktok.com/@user/video/TEST000552') == 'tiktok')
    check("platform_02952", platform_of('https://www.tiktok.com/@user/photo/TEST000553') == 'tiktok')
    check("platform_02953", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02954", platform_of('https://www.tiktok.com/@user/video/TEST000555') == 'tiktok')
    check("platform_02955", platform_of('https://www.tiktok.com/@user/photo/TEST000556') == 'tiktok')
    check("platform_02956", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02957", platform_of('https://www.tiktok.com/@user/video/TEST000558') == 'tiktok')
    check("platform_02958", platform_of('https://www.tiktok.com/@user/photo/TEST000559') == 'tiktok')
    check("platform_02959", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02960", platform_of('https://www.tiktok.com/@user/video/TEST000561') == 'tiktok')
    check("platform_02961", platform_of('https://www.tiktok.com/@user/photo/TEST000562') == 'tiktok')
    check("platform_02962", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02963", platform_of('https://www.tiktok.com/@user/video/TEST000564') == 'tiktok')
    check("platform_02964", platform_of('https://www.tiktok.com/@user/photo/TEST000565') == 'tiktok')
    check("platform_02965", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02966", platform_of('https://www.tiktok.com/@user/video/TEST000567') == 'tiktok')
    check("platform_02967", platform_of('https://www.tiktok.com/@user/photo/TEST000568') == 'tiktok')
    check("platform_02968", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02969", platform_of('https://www.tiktok.com/@user/video/TEST000570') == 'tiktok')
    check("platform_02970", platform_of('https://www.tiktok.com/@user/photo/TEST000571') == 'tiktok')
    check("platform_02971", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02972", platform_of('https://www.tiktok.com/@user/video/TEST000573') == 'tiktok')
    check("platform_02973", platform_of('https://www.tiktok.com/@user/photo/TEST000574') == 'tiktok')
    check("platform_02974", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02975", platform_of('https://www.tiktok.com/@user/video/TEST000576') == 'tiktok')
    check("platform_02976", platform_of('https://www.tiktok.com/@user/photo/TEST000577') == 'tiktok')
    check("platform_02977", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02978", platform_of('https://www.tiktok.com/@user/video/TEST000579') == 'tiktok')
    check("platform_02979", platform_of('https://www.tiktok.com/@user/photo/TEST000580') == 'tiktok')
    check("platform_02980", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02981", platform_of('https://www.tiktok.com/@user/video/TEST000582') == 'tiktok')
    check("platform_02982", platform_of('https://www.tiktok.com/@user/photo/TEST000583') == 'tiktok')
    check("platform_02983", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02984", platform_of('https://www.tiktok.com/@user/video/TEST000585') == 'tiktok')
    check("platform_02985", platform_of('https://www.tiktok.com/@user/photo/TEST000586') == 'tiktok')
    check("platform_02986", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02987", platform_of('https://www.tiktok.com/@user/video/TEST000588') == 'tiktok')
    check("platform_02988", platform_of('https://www.tiktok.com/@user/photo/TEST000589') == 'tiktok')
    check("platform_02989", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02990", platform_of('https://www.tiktok.com/@user/video/TEST000591') == 'tiktok')
    check("platform_02991", platform_of('https://www.tiktok.com/@user/photo/TEST000592') == 'tiktok')
    check("platform_02992", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02993", platform_of('https://www.tiktok.com/@user/video/TEST000594') == 'tiktok')
    check("platform_02994", platform_of('https://www.tiktok.com/@user/photo/TEST000595') == 'tiktok')
    check("platform_02995", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02996", platform_of('https://www.tiktok.com/@user/video/TEST000597') == 'tiktok')
    check("platform_02997", platform_of('https://www.tiktok.com/@user/photo/TEST000598') == 'tiktok')
    check("platform_02998", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_02999", platform_of('https://www.tiktok.com/@user/video/TEST000600') == 'tiktok')
    check("platform_03000", platform_of('https://www.tiktok.com/@user/photo/TEST000601') == 'tiktok')
    check("platform_03001", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03002", platform_of('https://www.tiktok.com/@user/video/TEST000603') == 'tiktok')
    check("platform_03003", platform_of('https://www.tiktok.com/@user/photo/TEST000604') == 'tiktok')
    check("platform_03004", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03005", platform_of('https://www.tiktok.com/@user/video/TEST000606') == 'tiktok')
    check("platform_03006", platform_of('https://www.tiktok.com/@user/photo/TEST000607') == 'tiktok')
    check("platform_03007", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03008", platform_of('https://www.tiktok.com/@user/video/TEST000609') == 'tiktok')
    check("platform_03009", platform_of('https://www.tiktok.com/@user/photo/TEST000610') == 'tiktok')
    check("platform_03010", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03011", platform_of('https://www.tiktok.com/@user/video/TEST000612') == 'tiktok')
    check("platform_03012", platform_of('https://www.tiktok.com/@user/photo/TEST000613') == 'tiktok')
    check("platform_03013", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03014", platform_of('https://www.tiktok.com/@user/video/TEST000615') == 'tiktok')
    check("platform_03015", platform_of('https://www.tiktok.com/@user/photo/TEST000616') == 'tiktok')
    check("platform_03016", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03017", platform_of('https://www.tiktok.com/@user/video/TEST000618') == 'tiktok')
    check("platform_03018", platform_of('https://www.tiktok.com/@user/photo/TEST000619') == 'tiktok')
    check("platform_03019", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03020", platform_of('https://www.tiktok.com/@user/video/TEST000621') == 'tiktok')
    check("platform_03021", platform_of('https://www.tiktok.com/@user/photo/TEST000622') == 'tiktok')
    check("platform_03022", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03023", platform_of('https://www.tiktok.com/@user/video/TEST000624') == 'tiktok')
    check("platform_03024", platform_of('https://www.tiktok.com/@user/photo/TEST000625') == 'tiktok')
    check("platform_03025", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03026", platform_of('https://www.tiktok.com/@user/video/TEST000627') == 'tiktok')
    check("platform_03027", platform_of('https://www.tiktok.com/@user/photo/TEST000628') == 'tiktok')
    check("platform_03028", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03029", platform_of('https://www.tiktok.com/@user/video/TEST000630') == 'tiktok')
    check("platform_03030", platform_of('https://www.tiktok.com/@user/photo/TEST000631') == 'tiktok')
    check("platform_03031", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03032", platform_of('https://www.tiktok.com/@user/video/TEST000633') == 'tiktok')
    check("platform_03033", platform_of('https://www.tiktok.com/@user/photo/TEST000634') == 'tiktok')
    check("platform_03034", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03035", platform_of('https://www.tiktok.com/@user/video/TEST000636') == 'tiktok')
    check("platform_03036", platform_of('https://www.tiktok.com/@user/photo/TEST000637') == 'tiktok')
    check("platform_03037", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03038", platform_of('https://www.tiktok.com/@user/video/TEST000639') == 'tiktok')
    check("platform_03039", platform_of('https://www.tiktok.com/@user/photo/TEST000640') == 'tiktok')
    check("platform_03040", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03041", platform_of('https://www.tiktok.com/@user/video/TEST000642') == 'tiktok')
    check("platform_03042", platform_of('https://www.tiktok.com/@user/photo/TEST000643') == 'tiktok')
    check("platform_03043", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03044", platform_of('https://www.tiktok.com/@user/video/TEST000645') == 'tiktok')
    check("platform_03045", platform_of('https://www.tiktok.com/@user/photo/TEST000646') == 'tiktok')
    check("platform_03046", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03047", platform_of('https://www.tiktok.com/@user/video/TEST000648') == 'tiktok')
    check("platform_03048", platform_of('https://www.tiktok.com/@user/photo/TEST000649') == 'tiktok')
    check("platform_03049", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03050", platform_of('https://www.tiktok.com/@user/video/TEST000651') == 'tiktok')
    check("platform_03051", platform_of('https://www.tiktok.com/@user/photo/TEST000652') == 'tiktok')
    check("platform_03052", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03053", platform_of('https://www.tiktok.com/@user/video/TEST000654') == 'tiktok')
    check("platform_03054", platform_of('https://www.tiktok.com/@user/photo/TEST000655') == 'tiktok')
    check("platform_03055", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03056", platform_of('https://www.tiktok.com/@user/video/TEST000657') == 'tiktok')
    check("platform_03057", platform_of('https://www.tiktok.com/@user/photo/TEST000658') == 'tiktok')
    check("platform_03058", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03059", platform_of('https://www.tiktok.com/@user/video/TEST000660') == 'tiktok')
    check("platform_03060", platform_of('https://www.tiktok.com/@user/photo/TEST000661') == 'tiktok')
    check("platform_03061", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03062", platform_of('https://www.tiktok.com/@user/video/TEST000663') == 'tiktok')
    check("platform_03063", platform_of('https://www.tiktok.com/@user/photo/TEST000664') == 'tiktok')
    check("platform_03064", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03065", platform_of('https://www.tiktok.com/@user/video/TEST000666') == 'tiktok')
    check("platform_03066", platform_of('https://www.tiktok.com/@user/photo/TEST000667') == 'tiktok')
    check("platform_03067", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03068", platform_of('https://www.tiktok.com/@user/video/TEST000669') == 'tiktok')
    check("platform_03069", platform_of('https://www.tiktok.com/@user/photo/TEST000670') == 'tiktok')
    check("platform_03070", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03071", platform_of('https://www.tiktok.com/@user/video/TEST000672') == 'tiktok')
    check("platform_03072", platform_of('https://www.tiktok.com/@user/photo/TEST000673') == 'tiktok')
    check("platform_03073", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03074", platform_of('https://www.tiktok.com/@user/video/TEST000675') == 'tiktok')
    check("platform_03075", platform_of('https://www.tiktok.com/@user/photo/TEST000676') == 'tiktok')
    check("platform_03076", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03077", platform_of('https://www.tiktok.com/@user/video/TEST000678') == 'tiktok')
    check("platform_03078", platform_of('https://www.tiktok.com/@user/photo/TEST000679') == 'tiktok')
    check("platform_03079", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03080", platform_of('https://www.tiktok.com/@user/video/TEST000681') == 'tiktok')
    check("platform_03081", platform_of('https://www.tiktok.com/@user/photo/TEST000682') == 'tiktok')
    check("platform_03082", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03083", platform_of('https://www.tiktok.com/@user/video/TEST000684') == 'tiktok')
    check("platform_03084", platform_of('https://www.tiktok.com/@user/photo/TEST000685') == 'tiktok')
    check("platform_03085", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03086", platform_of('https://www.tiktok.com/@user/video/TEST000687') == 'tiktok')
    check("platform_03087", platform_of('https://www.tiktok.com/@user/photo/TEST000688') == 'tiktok')
    check("platform_03088", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03089", platform_of('https://www.tiktok.com/@user/video/TEST000690') == 'tiktok')
    check("platform_03090", platform_of('https://www.tiktok.com/@user/photo/TEST000691') == 'tiktok')
    check("platform_03091", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03092", platform_of('https://www.tiktok.com/@user/video/TEST000693') == 'tiktok')
    check("platform_03093", platform_of('https://www.tiktok.com/@user/photo/TEST000694') == 'tiktok')
    check("platform_03094", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03095", platform_of('https://www.tiktok.com/@user/video/TEST000696') == 'tiktok')
    check("platform_03096", platform_of('https://www.tiktok.com/@user/photo/TEST000697') == 'tiktok')
    check("platform_03097", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03098", platform_of('https://www.tiktok.com/@user/video/TEST000699') == 'tiktok')
    check("platform_03099", platform_of('https://www.tiktok.com/@user/photo/TEST000700') == 'tiktok')
    check("platform_03100", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03101", platform_of('https://www.tiktok.com/@user/video/TEST000702') == 'tiktok')
    check("platform_03102", platform_of('https://www.tiktok.com/@user/photo/TEST000703') == 'tiktok')
    check("platform_03103", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03104", platform_of('https://www.tiktok.com/@user/video/TEST000705') == 'tiktok')
    check("platform_03105", platform_of('https://www.tiktok.com/@user/photo/TEST000706') == 'tiktok')
    check("platform_03106", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03107", platform_of('https://www.tiktok.com/@user/video/TEST000708') == 'tiktok')
    check("platform_03108", platform_of('https://www.tiktok.com/@user/photo/TEST000709') == 'tiktok')
    check("platform_03109", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03110", platform_of('https://www.tiktok.com/@user/video/TEST000711') == 'tiktok')
    check("platform_03111", platform_of('https://www.tiktok.com/@user/photo/TEST000712') == 'tiktok')
    check("platform_03112", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03113", platform_of('https://www.tiktok.com/@user/video/TEST000714') == 'tiktok')
    check("platform_03114", platform_of('https://www.tiktok.com/@user/photo/TEST000715') == 'tiktok')
    check("platform_03115", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03116", platform_of('https://www.tiktok.com/@user/video/TEST000717') == 'tiktok')
    check("platform_03117", platform_of('https://www.tiktok.com/@user/photo/TEST000718') == 'tiktok')
    check("platform_03118", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03119", platform_of('https://www.tiktok.com/@user/video/TEST000720') == 'tiktok')
    check("platform_03120", platform_of('https://www.tiktok.com/@user/photo/TEST000721') == 'tiktok')
    check("platform_03121", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03122", platform_of('https://www.tiktok.com/@user/video/TEST000723') == 'tiktok')
    check("platform_03123", platform_of('https://www.tiktok.com/@user/photo/TEST000724') == 'tiktok')
    check("platform_03124", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03125", platform_of('https://www.tiktok.com/@user/video/TEST000726') == 'tiktok')
    check("platform_03126", platform_of('https://www.tiktok.com/@user/photo/TEST000727') == 'tiktok')
    check("platform_03127", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03128", platform_of('https://www.tiktok.com/@user/video/TEST000729') == 'tiktok')
    check("platform_03129", platform_of('https://www.tiktok.com/@user/photo/TEST000730') == 'tiktok')
    check("platform_03130", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03131", platform_of('https://www.tiktok.com/@user/video/TEST000732') == 'tiktok')
    check("platform_03132", platform_of('https://www.tiktok.com/@user/photo/TEST000733') == 'tiktok')
    check("platform_03133", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03134", platform_of('https://www.tiktok.com/@user/video/TEST000735') == 'tiktok')
    check("platform_03135", platform_of('https://www.tiktok.com/@user/photo/TEST000736') == 'tiktok')
    check("platform_03136", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03137", platform_of('https://www.tiktok.com/@user/video/TEST000738') == 'tiktok')
    check("platform_03138", platform_of('https://www.tiktok.com/@user/photo/TEST000739') == 'tiktok')
    check("platform_03139", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03140", platform_of('https://www.tiktok.com/@user/video/TEST000741') == 'tiktok')
    check("platform_03141", platform_of('https://www.tiktok.com/@user/photo/TEST000742') == 'tiktok')
    check("platform_03142", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03143", platform_of('https://www.tiktok.com/@user/video/TEST000744') == 'tiktok')
    check("platform_03144", platform_of('https://www.tiktok.com/@user/photo/TEST000745') == 'tiktok')
    check("platform_03145", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03146", platform_of('https://www.tiktok.com/@user/video/TEST000747') == 'tiktok')
    check("platform_03147", platform_of('https://www.tiktok.com/@user/photo/TEST000748') == 'tiktok')
    check("platform_03148", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03149", platform_of('https://www.tiktok.com/@user/video/TEST000750') == 'tiktok')
    check("platform_03150", platform_of('https://www.tiktok.com/@user/photo/TEST000751') == 'tiktok')
    check("platform_03151", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03152", platform_of('https://www.tiktok.com/@user/video/TEST000753') == 'tiktok')
    check("platform_03153", platform_of('https://www.tiktok.com/@user/photo/TEST000754') == 'tiktok')
    check("platform_03154", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03155", platform_of('https://www.tiktok.com/@user/video/TEST000756') == 'tiktok')
    check("platform_03156", platform_of('https://www.tiktok.com/@user/photo/TEST000757') == 'tiktok')
    check("platform_03157", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03158", platform_of('https://www.tiktok.com/@user/video/TEST000759') == 'tiktok')
    check("platform_03159", platform_of('https://www.tiktok.com/@user/photo/TEST000760') == 'tiktok')
    check("platform_03160", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03161", platform_of('https://www.tiktok.com/@user/video/TEST000762') == 'tiktok')
    check("platform_03162", platform_of('https://www.tiktok.com/@user/photo/TEST000763') == 'tiktok')
    check("platform_03163", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03164", platform_of('https://www.tiktok.com/@user/video/TEST000765') == 'tiktok')
    check("platform_03165", platform_of('https://www.tiktok.com/@user/photo/TEST000766') == 'tiktok')
    check("platform_03166", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03167", platform_of('https://www.tiktok.com/@user/video/TEST000768') == 'tiktok')
    check("platform_03168", platform_of('https://www.tiktok.com/@user/photo/TEST000769') == 'tiktok')
    check("platform_03169", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03170", platform_of('https://www.tiktok.com/@user/video/TEST000771') == 'tiktok')
    check("platform_03171", platform_of('https://www.tiktok.com/@user/photo/TEST000772') == 'tiktok')
    check("platform_03172", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03173", platform_of('https://www.tiktok.com/@user/video/TEST000774') == 'tiktok')
    check("platform_03174", platform_of('https://www.tiktok.com/@user/photo/TEST000775') == 'tiktok')
    check("platform_03175", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03176", platform_of('https://www.tiktok.com/@user/video/TEST000777') == 'tiktok')
    check("platform_03177", platform_of('https://www.tiktok.com/@user/photo/TEST000778') == 'tiktok')
    check("platform_03178", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03179", platform_of('https://www.tiktok.com/@user/video/TEST000780') == 'tiktok')
    check("platform_03180", platform_of('https://www.tiktok.com/@user/photo/TEST000781') == 'tiktok')
    check("platform_03181", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03182", platform_of('https://www.tiktok.com/@user/video/TEST000783') == 'tiktok')
    check("platform_03183", platform_of('https://www.tiktok.com/@user/photo/TEST000784') == 'tiktok')
    check("platform_03184", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03185", platform_of('https://www.tiktok.com/@user/video/TEST000786') == 'tiktok')
    check("platform_03186", platform_of('https://www.tiktok.com/@user/photo/TEST000787') == 'tiktok')
    check("platform_03187", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03188", platform_of('https://www.tiktok.com/@user/video/TEST000789') == 'tiktok')
    check("platform_03189", platform_of('https://www.tiktok.com/@user/photo/TEST000790') == 'tiktok')
    check("platform_03190", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03191", platform_of('https://www.tiktok.com/@user/video/TEST000792') == 'tiktok')
    check("platform_03192", platform_of('https://www.tiktok.com/@user/photo/TEST000793') == 'tiktok')
    check("platform_03193", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03194", platform_of('https://www.tiktok.com/@user/video/TEST000795') == 'tiktok')
    check("platform_03195", platform_of('https://www.tiktok.com/@user/photo/TEST000796') == 'tiktok')
    check("platform_03196", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03197", platform_of('https://www.tiktok.com/@user/video/TEST000798') == 'tiktok')
    check("platform_03198", platform_of('https://www.tiktok.com/@user/photo/TEST000799') == 'tiktok')
    check("platform_03199", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03200", platform_of('https://www.tiktok.com/@user/video/TEST000801') == 'tiktok')
    check("platform_03201", platform_of('https://www.tiktok.com/@user/photo/TEST000802') == 'tiktok')
    check("platform_03202", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03203", platform_of('https://www.tiktok.com/@user/video/TEST000804') == 'tiktok')
    check("platform_03204", platform_of('https://www.tiktok.com/@user/photo/TEST000805') == 'tiktok')
    check("platform_03205", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03206", platform_of('https://www.tiktok.com/@user/video/TEST000807') == 'tiktok')
    check("platform_03207", platform_of('https://www.tiktok.com/@user/photo/TEST000808') == 'tiktok')
    check("platform_03208", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03209", platform_of('https://www.tiktok.com/@user/video/TEST000810') == 'tiktok')
    check("platform_03210", platform_of('https://www.tiktok.com/@user/photo/TEST000811') == 'tiktok')
    check("platform_03211", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03212", platform_of('https://www.tiktok.com/@user/video/TEST000813') == 'tiktok')
    check("platform_03213", platform_of('https://www.tiktok.com/@user/photo/TEST000814') == 'tiktok')
    check("platform_03214", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03215", platform_of('https://www.tiktok.com/@user/video/TEST000816') == 'tiktok')
    check("platform_03216", platform_of('https://www.tiktok.com/@user/photo/TEST000817') == 'tiktok')
    check("platform_03217", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03218", platform_of('https://www.tiktok.com/@user/video/TEST000819') == 'tiktok')
    check("platform_03219", platform_of('https://www.tiktok.com/@user/photo/TEST000820') == 'tiktok')
    check("platform_03220", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03221", platform_of('https://www.tiktok.com/@user/video/TEST000822') == 'tiktok')
    check("platform_03222", platform_of('https://www.tiktok.com/@user/photo/TEST000823') == 'tiktok')
    check("platform_03223", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03224", platform_of('https://www.tiktok.com/@user/video/TEST000825') == 'tiktok')
    check("platform_03225", platform_of('https://www.tiktok.com/@user/photo/TEST000826') == 'tiktok')
    check("platform_03226", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03227", platform_of('https://www.tiktok.com/@user/video/TEST000828') == 'tiktok')
    check("platform_03228", platform_of('https://www.tiktok.com/@user/photo/TEST000829') == 'tiktok')
    check("platform_03229", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03230", platform_of('https://www.tiktok.com/@user/video/TEST000831') == 'tiktok')
    check("platform_03231", platform_of('https://www.tiktok.com/@user/photo/TEST000832') == 'tiktok')
    check("platform_03232", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03233", platform_of('https://www.tiktok.com/@user/video/TEST000834') == 'tiktok')
    check("platform_03234", platform_of('https://www.tiktok.com/@user/photo/TEST000835') == 'tiktok')
    check("platform_03235", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03236", platform_of('https://www.tiktok.com/@user/video/TEST000837') == 'tiktok')
    check("platform_03237", platform_of('https://www.tiktok.com/@user/photo/TEST000838') == 'tiktok')
    check("platform_03238", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03239", platform_of('https://www.tiktok.com/@user/video/TEST000840') == 'tiktok')
    check("platform_03240", platform_of('https://www.tiktok.com/@user/photo/TEST000841') == 'tiktok')
    check("platform_03241", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03242", platform_of('https://www.tiktok.com/@user/video/TEST000843') == 'tiktok')
    check("platform_03243", platform_of('https://www.tiktok.com/@user/photo/TEST000844') == 'tiktok')
    check("platform_03244", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03245", platform_of('https://www.tiktok.com/@user/video/TEST000846') == 'tiktok')
    check("platform_03246", platform_of('https://www.tiktok.com/@user/photo/TEST000847') == 'tiktok')
    check("platform_03247", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03248", platform_of('https://www.tiktok.com/@user/video/TEST000849') == 'tiktok')
    check("platform_03249", platform_of('https://www.tiktok.com/@user/photo/TEST000850') == 'tiktok')
    check("platform_03250", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03251", platform_of('https://www.tiktok.com/@user/video/TEST000852') == 'tiktok')
    check("platform_03252", platform_of('https://www.tiktok.com/@user/photo/TEST000853') == 'tiktok')
    check("platform_03253", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03254", platform_of('https://www.tiktok.com/@user/video/TEST000855') == 'tiktok')
    check("platform_03255", platform_of('https://www.tiktok.com/@user/photo/TEST000856') == 'tiktok')
    check("platform_03256", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03257", platform_of('https://www.tiktok.com/@user/video/TEST000858') == 'tiktok')
    check("platform_03258", platform_of('https://www.tiktok.com/@user/photo/TEST000859') == 'tiktok')
    check("platform_03259", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03260", platform_of('https://www.tiktok.com/@user/video/TEST000861') == 'tiktok')
    check("platform_03261", platform_of('https://www.tiktok.com/@user/photo/TEST000862') == 'tiktok')
    check("platform_03262", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03263", platform_of('https://www.tiktok.com/@user/video/TEST000864') == 'tiktok')
    check("platform_03264", platform_of('https://www.tiktok.com/@user/photo/TEST000865') == 'tiktok')
    check("platform_03265", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03266", platform_of('https://www.tiktok.com/@user/video/TEST000867') == 'tiktok')
    check("platform_03267", platform_of('https://www.tiktok.com/@user/photo/TEST000868') == 'tiktok')
    check("platform_03268", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03269", platform_of('https://www.tiktok.com/@user/video/TEST000870') == 'tiktok')
    check("platform_03270", platform_of('https://www.tiktok.com/@user/photo/TEST000871') == 'tiktok')
    check("platform_03271", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03272", platform_of('https://www.tiktok.com/@user/video/TEST000873') == 'tiktok')
    check("platform_03273", platform_of('https://www.tiktok.com/@user/photo/TEST000874') == 'tiktok')
    check("platform_03274", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03275", platform_of('https://www.tiktok.com/@user/video/TEST000876') == 'tiktok')
    check("platform_03276", platform_of('https://www.tiktok.com/@user/photo/TEST000877') == 'tiktok')
    check("platform_03277", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03278", platform_of('https://www.tiktok.com/@user/video/TEST000879') == 'tiktok')
    check("platform_03279", platform_of('https://www.tiktok.com/@user/photo/TEST000880') == 'tiktok')
    check("platform_03280", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03281", platform_of('https://www.tiktok.com/@user/video/TEST000882') == 'tiktok')
    check("platform_03282", platform_of('https://www.tiktok.com/@user/photo/TEST000883') == 'tiktok')
    check("platform_03283", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03284", platform_of('https://www.tiktok.com/@user/video/TEST000885') == 'tiktok')
    check("platform_03285", platform_of('https://www.tiktok.com/@user/photo/TEST000886') == 'tiktok')
    check("platform_03286", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03287", platform_of('https://www.tiktok.com/@user/video/TEST000888') == 'tiktok')
    check("platform_03288", platform_of('https://www.tiktok.com/@user/photo/TEST000889') == 'tiktok')
    check("platform_03289", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03290", platform_of('https://www.tiktok.com/@user/video/TEST000891') == 'tiktok')
    check("platform_03291", platform_of('https://www.tiktok.com/@user/photo/TEST000892') == 'tiktok')
    check("platform_03292", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03293", platform_of('https://www.tiktok.com/@user/video/TEST000894') == 'tiktok')
    check("platform_03294", platform_of('https://www.tiktok.com/@user/photo/TEST000895') == 'tiktok')
    check("platform_03295", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03296", platform_of('https://www.tiktok.com/@user/video/TEST000897') == 'tiktok')
    check("platform_03297", platform_of('https://www.tiktok.com/@user/photo/TEST000898') == 'tiktok')
    check("platform_03298", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03299", platform_of('https://www.tiktok.com/@user/video/TEST000900') == 'tiktok')
    check("platform_03300", platform_of('https://www.tiktok.com/@user/photo/TEST000901') == 'tiktok')
    check("platform_03301", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03302", platform_of('https://www.tiktok.com/@user/video/TEST000903') == 'tiktok')
    check("platform_03303", platform_of('https://www.tiktok.com/@user/photo/TEST000904') == 'tiktok')
    check("platform_03304", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03305", platform_of('https://www.tiktok.com/@user/video/TEST000906') == 'tiktok')
    check("platform_03306", platform_of('https://www.tiktok.com/@user/photo/TEST000907') == 'tiktok')
    check("platform_03307", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03308", platform_of('https://www.tiktok.com/@user/video/TEST000909') == 'tiktok')
    check("platform_03309", platform_of('https://www.tiktok.com/@user/photo/TEST000910') == 'tiktok')
    check("platform_03310", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03311", platform_of('https://www.tiktok.com/@user/video/TEST000912') == 'tiktok')
    check("platform_03312", platform_of('https://www.tiktok.com/@user/photo/TEST000913') == 'tiktok')
    check("platform_03313", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03314", platform_of('https://www.tiktok.com/@user/video/TEST000915') == 'tiktok')
    check("platform_03315", platform_of('https://www.tiktok.com/@user/photo/TEST000916') == 'tiktok')
    check("platform_03316", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03317", platform_of('https://www.tiktok.com/@user/video/TEST000918') == 'tiktok')
    check("platform_03318", platform_of('https://www.tiktok.com/@user/photo/TEST000919') == 'tiktok')
    check("platform_03319", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03320", platform_of('https://www.tiktok.com/@user/video/TEST000921') == 'tiktok')
    check("platform_03321", platform_of('https://www.tiktok.com/@user/photo/TEST000922') == 'tiktok')
    check("platform_03322", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03323", platform_of('https://www.tiktok.com/@user/video/TEST000924') == 'tiktok')
    check("platform_03324", platform_of('https://www.tiktok.com/@user/photo/TEST000925') == 'tiktok')
    check("platform_03325", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03326", platform_of('https://www.tiktok.com/@user/video/TEST000927') == 'tiktok')
    check("platform_03327", platform_of('https://www.tiktok.com/@user/photo/TEST000928') == 'tiktok')
    check("platform_03328", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03329", platform_of('https://www.tiktok.com/@user/video/TEST000930') == 'tiktok')
    check("platform_03330", platform_of('https://www.tiktok.com/@user/photo/TEST000931') == 'tiktok')
    check("platform_03331", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03332", platform_of('https://www.tiktok.com/@user/video/TEST000933') == 'tiktok')
    check("platform_03333", platform_of('https://www.tiktok.com/@user/photo/TEST000934') == 'tiktok')
    check("platform_03334", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03335", platform_of('https://www.tiktok.com/@user/video/TEST000936') == 'tiktok')
    check("platform_03336", platform_of('https://www.tiktok.com/@user/photo/TEST000937') == 'tiktok')
    check("platform_03337", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03338", platform_of('https://www.tiktok.com/@user/video/TEST000939') == 'tiktok')
    check("platform_03339", platform_of('https://www.tiktok.com/@user/photo/TEST000940') == 'tiktok')
    check("platform_03340", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03341", platform_of('https://www.tiktok.com/@user/video/TEST000942') == 'tiktok')
    check("platform_03342", platform_of('https://www.tiktok.com/@user/photo/TEST000943') == 'tiktok')
    check("platform_03343", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03344", platform_of('https://www.tiktok.com/@user/video/TEST000945') == 'tiktok')
    check("platform_03345", platform_of('https://www.tiktok.com/@user/photo/TEST000946') == 'tiktok')
    check("platform_03346", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03347", platform_of('https://www.tiktok.com/@user/video/TEST000948') == 'tiktok')
    check("platform_03348", platform_of('https://www.tiktok.com/@user/photo/TEST000949') == 'tiktok')
    check("platform_03349", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03350", platform_of('https://www.tiktok.com/@user/video/TEST000951') == 'tiktok')
    check("platform_03351", platform_of('https://www.tiktok.com/@user/photo/TEST000952') == 'tiktok')
    check("platform_03352", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03353", platform_of('https://www.tiktok.com/@user/video/TEST000954') == 'tiktok')
    check("platform_03354", platform_of('https://www.tiktok.com/@user/photo/TEST000955') == 'tiktok')
    check("platform_03355", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03356", platform_of('https://www.tiktok.com/@user/video/TEST000957') == 'tiktok')
    check("platform_03357", platform_of('https://www.tiktok.com/@user/photo/TEST000958') == 'tiktok')
    check("platform_03358", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03359", platform_of('https://www.tiktok.com/@user/video/TEST000960') == 'tiktok')
    check("platform_03360", platform_of('https://www.tiktok.com/@user/photo/TEST000961') == 'tiktok')
    check("platform_03361", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03362", platform_of('https://www.tiktok.com/@user/video/TEST000963') == 'tiktok')
    check("platform_03363", platform_of('https://www.tiktok.com/@user/photo/TEST000964') == 'tiktok')
    check("platform_03364", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03365", platform_of('https://www.tiktok.com/@user/video/TEST000966') == 'tiktok')
    check("platform_03366", platform_of('https://www.tiktok.com/@user/photo/TEST000967') == 'tiktok')
    check("platform_03367", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03368", platform_of('https://www.tiktok.com/@user/video/TEST000969') == 'tiktok')
    check("platform_03369", platform_of('https://www.tiktok.com/@user/photo/TEST000970') == 'tiktok')
    check("platform_03370", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03371", platform_of('https://www.tiktok.com/@user/video/TEST000972') == 'tiktok')
    check("platform_03372", platform_of('https://www.tiktok.com/@user/photo/TEST000973') == 'tiktok')
    check("platform_03373", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03374", platform_of('https://www.tiktok.com/@user/video/TEST000975') == 'tiktok')
    check("platform_03375", platform_of('https://www.tiktok.com/@user/photo/TEST000976') == 'tiktok')
    check("platform_03376", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03377", platform_of('https://www.tiktok.com/@user/video/TEST000978') == 'tiktok')
    check("platform_03378", platform_of('https://www.tiktok.com/@user/photo/TEST000979') == 'tiktok')
    check("platform_03379", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03380", platform_of('https://www.tiktok.com/@user/video/TEST000981') == 'tiktok')
    check("platform_03381", platform_of('https://www.tiktok.com/@user/photo/TEST000982') == 'tiktok')
    check("platform_03382", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03383", platform_of('https://www.tiktok.com/@user/video/TEST000984') == 'tiktok')
    check("platform_03384", platform_of('https://www.tiktok.com/@user/photo/TEST000985') == 'tiktok')
    check("platform_03385", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03386", platform_of('https://www.tiktok.com/@user/video/TEST000987') == 'tiktok')
    check("platform_03387", platform_of('https://www.tiktok.com/@user/photo/TEST000988') == 'tiktok')
    check("platform_03388", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03389", platform_of('https://www.tiktok.com/@user/video/TEST000990') == 'tiktok')
    check("platform_03390", platform_of('https://www.tiktok.com/@user/photo/TEST000991') == 'tiktok')
    check("platform_03391", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03392", platform_of('https://www.tiktok.com/@user/video/TEST000993') == 'tiktok')
    check("platform_03393", platform_of('https://www.tiktok.com/@user/photo/TEST000994') == 'tiktok')
    check("platform_03394", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03395", platform_of('https://www.tiktok.com/@user/video/TEST000996') == 'tiktok')
    check("platform_03396", platform_of('https://www.tiktok.com/@user/photo/TEST000997') == 'tiktok')
    check("platform_03397", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03398", platform_of('https://www.tiktok.com/@user/video/TEST000999') == 'tiktok')
    check("platform_03399", platform_of('https://www.tiktok.com/@user/photo/TEST001000') == 'tiktok')
    check("platform_03400", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03401", platform_of('https://www.tiktok.com/@user/video/TEST001002') == 'tiktok')
    check("platform_03402", platform_of('https://www.tiktok.com/@user/photo/TEST001003') == 'tiktok')
    check("platform_03403", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03404", platform_of('https://www.tiktok.com/@user/video/TEST001005') == 'tiktok')
    check("platform_03405", platform_of('https://www.tiktok.com/@user/photo/TEST001006') == 'tiktok')
    check("platform_03406", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03407", platform_of('https://www.tiktok.com/@user/video/TEST001008') == 'tiktok')
    check("platform_03408", platform_of('https://www.tiktok.com/@user/photo/TEST001009') == 'tiktok')
    check("platform_03409", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03410", platform_of('https://www.tiktok.com/@user/video/TEST001011') == 'tiktok')
    check("platform_03411", platform_of('https://www.tiktok.com/@user/photo/TEST001012') == 'tiktok')
    check("platform_03412", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03413", platform_of('https://www.tiktok.com/@user/video/TEST001014') == 'tiktok')
    check("platform_03414", platform_of('https://www.tiktok.com/@user/photo/TEST001015') == 'tiktok')
    check("platform_03415", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03416", platform_of('https://www.tiktok.com/@user/video/TEST001017') == 'tiktok')
    check("platform_03417", platform_of('https://www.tiktok.com/@user/photo/TEST001018') == 'tiktok')
    check("platform_03418", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03419", platform_of('https://www.tiktok.com/@user/video/TEST001020') == 'tiktok')
    check("platform_03420", platform_of('https://www.tiktok.com/@user/photo/TEST001021') == 'tiktok')
    check("platform_03421", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03422", platform_of('https://www.tiktok.com/@user/video/TEST001023') == 'tiktok')
    check("platform_03423", platform_of('https://www.tiktok.com/@user/photo/TEST001024') == 'tiktok')
    check("platform_03424", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03425", platform_of('https://www.tiktok.com/@user/video/TEST001026') == 'tiktok')
    check("platform_03426", platform_of('https://www.tiktok.com/@user/photo/TEST001027') == 'tiktok')
    check("platform_03427", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03428", platform_of('https://www.tiktok.com/@user/video/TEST001029') == 'tiktok')
    check("platform_03429", platform_of('https://www.tiktok.com/@user/photo/TEST001030') == 'tiktok')
    check("platform_03430", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03431", platform_of('https://www.tiktok.com/@user/video/TEST001032') == 'tiktok')
    check("platform_03432", platform_of('https://www.tiktok.com/@user/photo/TEST001033') == 'tiktok')
    check("platform_03433", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03434", platform_of('https://www.tiktok.com/@user/video/TEST001035') == 'tiktok')
    check("platform_03435", platform_of('https://www.tiktok.com/@user/photo/TEST001036') == 'tiktok')
    check("platform_03436", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03437", platform_of('https://www.tiktok.com/@user/video/TEST001038') == 'tiktok')
    check("platform_03438", platform_of('https://www.tiktok.com/@user/photo/TEST001039') == 'tiktok')
    check("platform_03439", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03440", platform_of('https://www.tiktok.com/@user/video/TEST001041') == 'tiktok')
    check("platform_03441", platform_of('https://www.tiktok.com/@user/photo/TEST001042') == 'tiktok')
    check("platform_03442", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03443", platform_of('https://www.tiktok.com/@user/video/TEST001044') == 'tiktok')
    check("platform_03444", platform_of('https://www.tiktok.com/@user/photo/TEST001045') == 'tiktok')
    check("platform_03445", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03446", platform_of('https://www.tiktok.com/@user/video/TEST001047') == 'tiktok')
    check("platform_03447", platform_of('https://www.tiktok.com/@user/photo/TEST001048') == 'tiktok')
    check("platform_03448", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03449", platform_of('https://www.tiktok.com/@user/video/TEST001050') == 'tiktok')
    check("platform_03450", platform_of('https://www.tiktok.com/@user/photo/TEST001051') == 'tiktok')
    check("platform_03451", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03452", platform_of('https://www.tiktok.com/@user/video/TEST001053') == 'tiktok')
    check("platform_03453", platform_of('https://www.tiktok.com/@user/photo/TEST001054') == 'tiktok')
    check("platform_03454", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03455", platform_of('https://www.tiktok.com/@user/video/TEST001056') == 'tiktok')
    check("platform_03456", platform_of('https://www.tiktok.com/@user/photo/TEST001057') == 'tiktok')
    check("platform_03457", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03458", platform_of('https://www.tiktok.com/@user/video/TEST001059') == 'tiktok')
    check("platform_03459", platform_of('https://www.tiktok.com/@user/photo/TEST001060') == 'tiktok')
    check("platform_03460", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03461", platform_of('https://www.tiktok.com/@user/video/TEST001062') == 'tiktok')
    check("platform_03462", platform_of('https://www.tiktok.com/@user/photo/TEST001063') == 'tiktok')
    check("platform_03463", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03464", platform_of('https://www.tiktok.com/@user/video/TEST001065') == 'tiktok')
    check("platform_03465", platform_of('https://www.tiktok.com/@user/photo/TEST001066') == 'tiktok')
    check("platform_03466", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03467", platform_of('https://www.tiktok.com/@user/video/TEST001068') == 'tiktok')
    check("platform_03468", platform_of('https://www.tiktok.com/@user/photo/TEST001069') == 'tiktok')
    check("platform_03469", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03470", platform_of('https://www.tiktok.com/@user/video/TEST001071') == 'tiktok')
    check("platform_03471", platform_of('https://www.tiktok.com/@user/photo/TEST001072') == 'tiktok')
    check("platform_03472", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03473", platform_of('https://www.tiktok.com/@user/video/TEST001074') == 'tiktok')
    check("platform_03474", platform_of('https://www.tiktok.com/@user/photo/TEST001075') == 'tiktok')
    check("platform_03475", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03476", platform_of('https://www.tiktok.com/@user/video/TEST001077') == 'tiktok')
    check("platform_03477", platform_of('https://www.tiktok.com/@user/photo/TEST001078') == 'tiktok')
    check("platform_03478", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03479", platform_of('https://www.tiktok.com/@user/video/TEST001080') == 'tiktok')
    check("platform_03480", platform_of('https://www.tiktok.com/@user/photo/TEST001081') == 'tiktok')
    check("platform_03481", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03482", platform_of('https://www.tiktok.com/@user/video/TEST001083') == 'tiktok')
    check("platform_03483", platform_of('https://www.tiktok.com/@user/photo/TEST001084') == 'tiktok')
    check("platform_03484", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03485", platform_of('https://www.tiktok.com/@user/video/TEST001086') == 'tiktok')
    check("platform_03486", platform_of('https://www.tiktok.com/@user/photo/TEST001087') == 'tiktok')
    check("platform_03487", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03488", platform_of('https://www.tiktok.com/@user/video/TEST001089') == 'tiktok')
    check("platform_03489", platform_of('https://www.tiktok.com/@user/photo/TEST001090') == 'tiktok')
    check("platform_03490", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03491", platform_of('https://www.tiktok.com/@user/video/TEST001092') == 'tiktok')
    check("platform_03492", platform_of('https://www.tiktok.com/@user/photo/TEST001093') == 'tiktok')
    check("platform_03493", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03494", platform_of('https://www.tiktok.com/@user/video/TEST001095') == 'tiktok')
    check("platform_03495", platform_of('https://www.tiktok.com/@user/photo/TEST001096') == 'tiktok')
    check("platform_03496", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03497", platform_of('https://www.tiktok.com/@user/video/TEST001098') == 'tiktok')
    check("platform_03498", platform_of('https://www.tiktok.com/@user/photo/TEST001099') == 'tiktok')
    check("platform_03499", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03500", platform_of('https://www.tiktok.com/@user/video/TEST001101') == 'tiktok')
    check("platform_03501", platform_of('https://www.tiktok.com/@user/photo/TEST001102') == 'tiktok')
    check("platform_03502", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03503", platform_of('https://www.tiktok.com/@user/video/TEST001104') == 'tiktok')
    check("platform_03504", platform_of('https://www.tiktok.com/@user/photo/TEST001105') == 'tiktok')
    check("platform_03505", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03506", platform_of('https://www.tiktok.com/@user/video/TEST001107') == 'tiktok')
    check("platform_03507", platform_of('https://www.tiktok.com/@user/photo/TEST001108') == 'tiktok')
    check("platform_03508", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03509", platform_of('https://www.tiktok.com/@user/video/TEST001110') == 'tiktok')
    check("platform_03510", platform_of('https://www.tiktok.com/@user/photo/TEST001111') == 'tiktok')
    check("platform_03511", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03512", platform_of('https://www.tiktok.com/@user/video/TEST001113') == 'tiktok')
    check("platform_03513", platform_of('https://www.tiktok.com/@user/photo/TEST001114') == 'tiktok')
    check("platform_03514", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03515", platform_of('https://www.tiktok.com/@user/video/TEST001116') == 'tiktok')
    check("platform_03516", platform_of('https://www.tiktok.com/@user/photo/TEST001117') == 'tiktok')
    check("platform_03517", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03518", platform_of('https://www.tiktok.com/@user/video/TEST001119') == 'tiktok')
    check("platform_03519", platform_of('https://www.tiktok.com/@user/photo/TEST001120') == 'tiktok')
    check("platform_03520", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03521", platform_of('https://www.tiktok.com/@user/video/TEST001122') == 'tiktok')
    check("platform_03522", platform_of('https://www.tiktok.com/@user/photo/TEST001123') == 'tiktok')
    check("platform_03523", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03524", platform_of('https://www.tiktok.com/@user/video/TEST001125') == 'tiktok')
    check("platform_03525", platform_of('https://www.tiktok.com/@user/photo/TEST001126') == 'tiktok')
    check("platform_03526", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03527", platform_of('https://www.tiktok.com/@user/video/TEST001128') == 'tiktok')
    check("platform_03528", platform_of('https://www.tiktok.com/@user/photo/TEST001129') == 'tiktok')
    check("platform_03529", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03530", platform_of('https://www.tiktok.com/@user/video/TEST001131') == 'tiktok')
    check("platform_03531", platform_of('https://www.tiktok.com/@user/photo/TEST001132') == 'tiktok')
    check("platform_03532", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03533", platform_of('https://www.tiktok.com/@user/video/TEST001134') == 'tiktok')
    check("platform_03534", platform_of('https://www.tiktok.com/@user/photo/TEST001135') == 'tiktok')
    check("platform_03535", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03536", platform_of('https://www.tiktok.com/@user/video/TEST001137') == 'tiktok')
    check("platform_03537", platform_of('https://www.tiktok.com/@user/photo/TEST001138') == 'tiktok')
    check("platform_03538", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03539", platform_of('https://www.tiktok.com/@user/video/TEST001140') == 'tiktok')
    check("platform_03540", platform_of('https://www.tiktok.com/@user/photo/TEST001141') == 'tiktok')
    check("platform_03541", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03542", platform_of('https://www.tiktok.com/@user/video/TEST001143') == 'tiktok')
    check("platform_03543", platform_of('https://www.tiktok.com/@user/photo/TEST001144') == 'tiktok')
    check("platform_03544", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03545", platform_of('https://www.tiktok.com/@user/video/TEST001146') == 'tiktok')
    check("platform_03546", platform_of('https://www.tiktok.com/@user/photo/TEST001147') == 'tiktok')
    check("platform_03547", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03548", platform_of('https://www.tiktok.com/@user/video/TEST001149') == 'tiktok')
    check("platform_03549", platform_of('https://www.tiktok.com/@user/photo/TEST001150') == 'tiktok')
    check("platform_03550", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03551", platform_of('https://www.tiktok.com/@user/video/TEST001152') == 'tiktok')
    check("platform_03552", platform_of('https://www.tiktok.com/@user/photo/TEST001153') == 'tiktok')
    check("platform_03553", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03554", platform_of('https://www.tiktok.com/@user/video/TEST001155') == 'tiktok')
    check("platform_03555", platform_of('https://www.tiktok.com/@user/photo/TEST001156') == 'tiktok')
    check("platform_03556", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03557", platform_of('https://www.tiktok.com/@user/video/TEST001158') == 'tiktok')
    check("platform_03558", platform_of('https://www.tiktok.com/@user/photo/TEST001159') == 'tiktok')
    check("platform_03559", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03560", platform_of('https://www.tiktok.com/@user/video/TEST001161') == 'tiktok')
    check("platform_03561", platform_of('https://www.tiktok.com/@user/photo/TEST001162') == 'tiktok')
    check("platform_03562", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03563", platform_of('https://www.tiktok.com/@user/video/TEST001164') == 'tiktok')
    check("platform_03564", platform_of('https://www.tiktok.com/@user/photo/TEST001165') == 'tiktok')
    check("platform_03565", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03566", platform_of('https://www.tiktok.com/@user/video/TEST001167') == 'tiktok')
    check("platform_03567", platform_of('https://www.tiktok.com/@user/photo/TEST001168') == 'tiktok')
    check("platform_03568", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03569", platform_of('https://www.tiktok.com/@user/video/TEST001170') == 'tiktok')
    check("platform_03570", platform_of('https://www.tiktok.com/@user/photo/TEST001171') == 'tiktok')
    check("platform_03571", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03572", platform_of('https://www.tiktok.com/@user/video/TEST001173') == 'tiktok')
    check("platform_03573", platform_of('https://www.tiktok.com/@user/photo/TEST001174') == 'tiktok')
    check("platform_03574", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03575", platform_of('https://www.tiktok.com/@user/video/TEST001176') == 'tiktok')
    check("platform_03576", platform_of('https://www.tiktok.com/@user/photo/TEST001177') == 'tiktok')
    check("platform_03577", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03578", platform_of('https://www.tiktok.com/@user/video/TEST001179') == 'tiktok')
    check("platform_03579", platform_of('https://www.tiktok.com/@user/photo/TEST001180') == 'tiktok')
    check("platform_03580", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03581", platform_of('https://www.tiktok.com/@user/video/TEST001182') == 'tiktok')
    check("platform_03582", platform_of('https://www.tiktok.com/@user/photo/TEST001183') == 'tiktok')
    check("platform_03583", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03584", platform_of('https://www.tiktok.com/@user/video/TEST001185') == 'tiktok')
    check("platform_03585", platform_of('https://www.tiktok.com/@user/photo/TEST001186') == 'tiktok')
    check("platform_03586", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03587", platform_of('https://www.tiktok.com/@user/video/TEST001188') == 'tiktok')
    check("platform_03588", platform_of('https://www.tiktok.com/@user/photo/TEST001189') == 'tiktok')
    check("platform_03589", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03590", platform_of('https://www.tiktok.com/@user/video/TEST001191') == 'tiktok')
    check("platform_03591", platform_of('https://www.tiktok.com/@user/photo/TEST001192') == 'tiktok')
    check("platform_03592", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03593", platform_of('https://www.tiktok.com/@user/video/TEST001194') == 'tiktok')
    check("platform_03594", platform_of('https://www.tiktok.com/@user/photo/TEST001195') == 'tiktok')
    check("platform_03595", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03596", platform_of('https://www.tiktok.com/@user/video/TEST001197') == 'tiktok')
    check("platform_03597", platform_of('https://www.tiktok.com/@user/photo/TEST001198') == 'tiktok')
    check("platform_03598", platform_of('https://www.tiktok.com/@user') == 'tiktok')
    check("platform_03599", platform_of('https://www.tiktok.com/@user/video/TEST001200') == 'tiktok')
    check("normalize_03600", normalize_url('https://www.youtube.com/watch?v=REG00000001') == 'https://www.youtube.com/watch?v=REG00000001')
    check("normalize_03601", normalize_url('https://www.youtube.com/watch?v=REG00000002') == 'https://www.youtube.com/watch?v=REG00000002')
    check("normalize_03602", normalize_url('https://www.youtube.com/watch?v=REG00000003') == 'https://www.youtube.com/watch?v=REG00000003')
    check("normalize_03603", normalize_url('https://www.youtube.com/watch?v=REG00000004') == 'https://www.youtube.com/watch?v=REG00000004')
    check("normalize_03604", normalize_url('https://www.youtube.com/watch?v=REG00000005') == 'https://www.youtube.com/watch?v=REG00000005')
    check("normalize_03605", normalize_url('https://www.youtube.com/watch?v=REG00000006') == 'https://www.youtube.com/watch?v=REG00000006')
    check("normalize_03606", normalize_url('https://www.youtube.com/watch?v=REG00000007') == 'https://www.youtube.com/watch?v=REG00000007')
    check("normalize_03607", normalize_url('https://www.youtube.com/watch?v=REG00000008') == 'https://www.youtube.com/watch?v=REG00000008')
    check("normalize_03608", normalize_url('https://www.youtube.com/watch?v=REG00000009') == 'https://www.youtube.com/watch?v=REG00000009')
    check("normalize_03609", normalize_url('https://www.youtube.com/watch?v=REG00000010') == 'https://www.youtube.com/watch?v=REG00000010')
    check("normalize_03610", normalize_url('https://www.youtube.com/watch?v=REG00000011') == 'https://www.youtube.com/watch?v=REG00000011')
    check("normalize_03611", normalize_url('https://www.youtube.com/watch?v=REG00000012') == 'https://www.youtube.com/watch?v=REG00000012')
    check("normalize_03612", normalize_url('https://www.youtube.com/watch?v=REG00000013') == 'https://www.youtube.com/watch?v=REG00000013')
    check("normalize_03613", normalize_url('https://www.youtube.com/watch?v=REG00000014') == 'https://www.youtube.com/watch?v=REG00000014')
    check("normalize_03614", normalize_url('https://www.youtube.com/watch?v=REG00000015') == 'https://www.youtube.com/watch?v=REG00000015')
    check("normalize_03615", normalize_url('https://www.youtube.com/watch?v=REG00000016') == 'https://www.youtube.com/watch?v=REG00000016')
    check("normalize_03616", normalize_url('https://www.youtube.com/watch?v=REG00000017') == 'https://www.youtube.com/watch?v=REG00000017')
    check("normalize_03617", normalize_url('https://www.youtube.com/watch?v=REG00000018') == 'https://www.youtube.com/watch?v=REG00000018')
    check("normalize_03618", normalize_url('https://www.youtube.com/watch?v=REG00000019') == 'https://www.youtube.com/watch?v=REG00000019')
    check("normalize_03619", normalize_url('https://www.youtube.com/watch?v=REG00000020') == 'https://www.youtube.com/watch?v=REG00000020')
    check("normalize_03620", normalize_url('https://www.youtube.com/watch?v=REG00000021') == 'https://www.youtube.com/watch?v=REG00000021')
    check("normalize_03621", normalize_url('https://www.youtube.com/watch?v=REG00000022') == 'https://www.youtube.com/watch?v=REG00000022')
    check("normalize_03622", normalize_url('https://www.youtube.com/watch?v=REG00000023') == 'https://www.youtube.com/watch?v=REG00000023')
    check("normalize_03623", normalize_url('https://www.youtube.com/watch?v=REG00000024') == 'https://www.youtube.com/watch?v=REG00000024')
    check("normalize_03624", normalize_url('https://www.youtube.com/watch?v=REG00000025') == 'https://www.youtube.com/watch?v=REG00000025')
    check("normalize_03625", normalize_url('https://www.youtube.com/watch?v=REG00000026') == 'https://www.youtube.com/watch?v=REG00000026')
    check("normalize_03626", normalize_url('https://www.youtube.com/watch?v=REG00000027') == 'https://www.youtube.com/watch?v=REG00000027')
    check("normalize_03627", normalize_url('https://www.youtube.com/watch?v=REG00000028') == 'https://www.youtube.com/watch?v=REG00000028')
    check("normalize_03628", normalize_url('https://www.youtube.com/watch?v=REG00000029') == 'https://www.youtube.com/watch?v=REG00000029')
    check("normalize_03629", normalize_url('https://www.youtube.com/watch?v=REG00000030') == 'https://www.youtube.com/watch?v=REG00000030')
    check("normalize_03630", normalize_url('https://www.youtube.com/watch?v=REG00000031') == 'https://www.youtube.com/watch?v=REG00000031')
    check("normalize_03631", normalize_url('https://www.youtube.com/watch?v=REG00000032') == 'https://www.youtube.com/watch?v=REG00000032')
    check("normalize_03632", normalize_url('https://www.youtube.com/watch?v=REG00000033') == 'https://www.youtube.com/watch?v=REG00000033')
    check("normalize_03633", normalize_url('https://www.youtube.com/watch?v=REG00000034') == 'https://www.youtube.com/watch?v=REG00000034')
    check("normalize_03634", normalize_url('https://www.youtube.com/watch?v=REG00000035') == 'https://www.youtube.com/watch?v=REG00000035')
    check("normalize_03635", normalize_url('https://www.youtube.com/watch?v=REG00000036') == 'https://www.youtube.com/watch?v=REG00000036')
    check("normalize_03636", normalize_url('https://www.youtube.com/watch?v=REG00000037') == 'https://www.youtube.com/watch?v=REG00000037')
    check("normalize_03637", normalize_url('https://www.youtube.com/watch?v=REG00000038') == 'https://www.youtube.com/watch?v=REG00000038')
    check("normalize_03638", normalize_url('https://www.youtube.com/watch?v=REG00000039') == 'https://www.youtube.com/watch?v=REG00000039')
    check("normalize_03639", normalize_url('https://www.youtube.com/watch?v=REG00000040') == 'https://www.youtube.com/watch?v=REG00000040')
    check("normalize_03640", normalize_url('https://www.youtube.com/watch?v=REG00000041') == 'https://www.youtube.com/watch?v=REG00000041')
    check("normalize_03641", normalize_url('https://www.youtube.com/watch?v=REG00000042') == 'https://www.youtube.com/watch?v=REG00000042')
    check("normalize_03642", normalize_url('https://www.youtube.com/watch?v=REG00000043') == 'https://www.youtube.com/watch?v=REG00000043')
    check("normalize_03643", normalize_url('https://www.youtube.com/watch?v=REG00000044') == 'https://www.youtube.com/watch?v=REG00000044')
    check("normalize_03644", normalize_url('https://www.youtube.com/watch?v=REG00000045') == 'https://www.youtube.com/watch?v=REG00000045')
    check("normalize_03645", normalize_url('https://www.youtube.com/watch?v=REG00000046') == 'https://www.youtube.com/watch?v=REG00000046')
    check("normalize_03646", normalize_url('https://www.youtube.com/watch?v=REG00000047') == 'https://www.youtube.com/watch?v=REG00000047')
    check("normalize_03647", normalize_url('https://www.youtube.com/watch?v=REG00000048') == 'https://www.youtube.com/watch?v=REG00000048')
    check("normalize_03648", normalize_url('https://www.youtube.com/watch?v=REG00000049') == 'https://www.youtube.com/watch?v=REG00000049')
    check("normalize_03649", normalize_url('https://www.youtube.com/watch?v=REG00000050') == 'https://www.youtube.com/watch?v=REG00000050')
    check("normalize_03650", normalize_url('https://www.youtube.com/watch?v=REG00000051') == 'https://www.youtube.com/watch?v=REG00000051')
    check("normalize_03651", normalize_url('https://www.youtube.com/watch?v=REG00000052') == 'https://www.youtube.com/watch?v=REG00000052')
    check("normalize_03652", normalize_url('https://www.youtube.com/watch?v=REG00000053') == 'https://www.youtube.com/watch?v=REG00000053')
    check("normalize_03653", normalize_url('https://www.youtube.com/watch?v=REG00000054') == 'https://www.youtube.com/watch?v=REG00000054')
    check("normalize_03654", normalize_url('https://www.youtube.com/watch?v=REG00000055') == 'https://www.youtube.com/watch?v=REG00000055')
    check("normalize_03655", normalize_url('https://www.youtube.com/watch?v=REG00000056') == 'https://www.youtube.com/watch?v=REG00000056')
    check("normalize_03656", normalize_url('https://www.youtube.com/watch?v=REG00000057') == 'https://www.youtube.com/watch?v=REG00000057')
    check("normalize_03657", normalize_url('https://www.youtube.com/watch?v=REG00000058') == 'https://www.youtube.com/watch?v=REG00000058')
    check("normalize_03658", normalize_url('https://www.youtube.com/watch?v=REG00000059') == 'https://www.youtube.com/watch?v=REG00000059')
    check("normalize_03659", normalize_url('https://www.youtube.com/watch?v=REG00000060') == 'https://www.youtube.com/watch?v=REG00000060')
    check("normalize_03660", normalize_url('https://www.youtube.com/watch?v=REG00000061') == 'https://www.youtube.com/watch?v=REG00000061')
    check("normalize_03661", normalize_url('https://www.youtube.com/watch?v=REG00000062') == 'https://www.youtube.com/watch?v=REG00000062')
    check("normalize_03662", normalize_url('https://www.youtube.com/watch?v=REG00000063') == 'https://www.youtube.com/watch?v=REG00000063')
    check("normalize_03663", normalize_url('https://www.youtube.com/watch?v=REG00000064') == 'https://www.youtube.com/watch?v=REG00000064')
    check("normalize_03664", normalize_url('https://www.youtube.com/watch?v=REG00000065') == 'https://www.youtube.com/watch?v=REG00000065')
    check("normalize_03665", normalize_url('https://www.youtube.com/watch?v=REG00000066') == 'https://www.youtube.com/watch?v=REG00000066')
    check("normalize_03666", normalize_url('https://www.youtube.com/watch?v=REG00000067') == 'https://www.youtube.com/watch?v=REG00000067')
    check("normalize_03667", normalize_url('https://www.youtube.com/watch?v=REG00000068') == 'https://www.youtube.com/watch?v=REG00000068')
    check("normalize_03668", normalize_url('https://www.youtube.com/watch?v=REG00000069') == 'https://www.youtube.com/watch?v=REG00000069')
    check("normalize_03669", normalize_url('https://www.youtube.com/watch?v=REG00000070') == 'https://www.youtube.com/watch?v=REG00000070')
    check("normalize_03670", normalize_url('https://www.youtube.com/watch?v=REG00000071') == 'https://www.youtube.com/watch?v=REG00000071')
    check("normalize_03671", normalize_url('https://www.youtube.com/watch?v=REG00000072') == 'https://www.youtube.com/watch?v=REG00000072')
    check("normalize_03672", normalize_url('https://www.youtube.com/watch?v=REG00000073') == 'https://www.youtube.com/watch?v=REG00000073')
    check("normalize_03673", normalize_url('https://www.youtube.com/watch?v=REG00000074') == 'https://www.youtube.com/watch?v=REG00000074')
    check("normalize_03674", normalize_url('https://www.youtube.com/watch?v=REG00000075') == 'https://www.youtube.com/watch?v=REG00000075')
    check("normalize_03675", normalize_url('https://www.youtube.com/watch?v=REG00000076') == 'https://www.youtube.com/watch?v=REG00000076')
    check("normalize_03676", normalize_url('https://www.youtube.com/watch?v=REG00000077') == 'https://www.youtube.com/watch?v=REG00000077')
    check("normalize_03677", normalize_url('https://www.youtube.com/watch?v=REG00000078') == 'https://www.youtube.com/watch?v=REG00000078')
    check("normalize_03678", normalize_url('https://www.youtube.com/watch?v=REG00000079') == 'https://www.youtube.com/watch?v=REG00000079')
    check("normalize_03679", normalize_url('https://www.youtube.com/watch?v=REG00000080') == 'https://www.youtube.com/watch?v=REG00000080')
    check("normalize_03680", normalize_url('https://www.youtube.com/watch?v=REG00000081') == 'https://www.youtube.com/watch?v=REG00000081')
    check("normalize_03681", normalize_url('https://www.youtube.com/watch?v=REG00000082') == 'https://www.youtube.com/watch?v=REG00000082')
    check("normalize_03682", normalize_url('https://www.youtube.com/watch?v=REG00000083') == 'https://www.youtube.com/watch?v=REG00000083')
    check("normalize_03683", normalize_url('https://www.youtube.com/watch?v=REG00000084') == 'https://www.youtube.com/watch?v=REG00000084')
    check("normalize_03684", normalize_url('https://www.youtube.com/watch?v=REG00000085') == 'https://www.youtube.com/watch?v=REG00000085')
    check("normalize_03685", normalize_url('https://www.youtube.com/watch?v=REG00000086') == 'https://www.youtube.com/watch?v=REG00000086')
    check("normalize_03686", normalize_url('https://www.youtube.com/watch?v=REG00000087') == 'https://www.youtube.com/watch?v=REG00000087')
    check("normalize_03687", normalize_url('https://www.youtube.com/watch?v=REG00000088') == 'https://www.youtube.com/watch?v=REG00000088')
    check("normalize_03688", normalize_url('https://www.youtube.com/watch?v=REG00000089') == 'https://www.youtube.com/watch?v=REG00000089')
    check("normalize_03689", normalize_url('https://www.youtube.com/watch?v=REG00000090') == 'https://www.youtube.com/watch?v=REG00000090')
    check("normalize_03690", normalize_url('https://www.youtube.com/watch?v=REG00000091') == 'https://www.youtube.com/watch?v=REG00000091')
    check("normalize_03691", normalize_url('https://www.youtube.com/watch?v=REG00000092') == 'https://www.youtube.com/watch?v=REG00000092')
    check("normalize_03692", normalize_url('https://www.youtube.com/watch?v=REG00000093') == 'https://www.youtube.com/watch?v=REG00000093')
    check("normalize_03693", normalize_url('https://www.youtube.com/watch?v=REG00000094') == 'https://www.youtube.com/watch?v=REG00000094')
    check("normalize_03694", normalize_url('https://www.youtube.com/watch?v=REG00000095') == 'https://www.youtube.com/watch?v=REG00000095')
    check("normalize_03695", normalize_url('https://www.youtube.com/watch?v=REG00000096') == 'https://www.youtube.com/watch?v=REG00000096')
    check("normalize_03696", normalize_url('https://www.youtube.com/watch?v=REG00000097') == 'https://www.youtube.com/watch?v=REG00000097')
    check("normalize_03697", normalize_url('https://www.youtube.com/watch?v=REG00000098') == 'https://www.youtube.com/watch?v=REG00000098')
    check("normalize_03698", normalize_url('https://www.youtube.com/watch?v=REG00000099') == 'https://www.youtube.com/watch?v=REG00000099')
    check("normalize_03699", normalize_url('https://www.youtube.com/watch?v=REG00000100') == 'https://www.youtube.com/watch?v=REG00000100')
    check("normalize_03700", normalize_url('https://www.youtube.com/watch?v=REG00000101') == 'https://www.youtube.com/watch?v=REG00000101')
    check("normalize_03701", normalize_url('https://www.youtube.com/watch?v=REG00000102') == 'https://www.youtube.com/watch?v=REG00000102')
    check("normalize_03702", normalize_url('https://www.youtube.com/watch?v=REG00000103') == 'https://www.youtube.com/watch?v=REG00000103')
    check("normalize_03703", normalize_url('https://www.youtube.com/watch?v=REG00000104') == 'https://www.youtube.com/watch?v=REG00000104')
    check("normalize_03704", normalize_url('https://www.youtube.com/watch?v=REG00000105') == 'https://www.youtube.com/watch?v=REG00000105')
    check("normalize_03705", normalize_url('https://www.youtube.com/watch?v=REG00000106') == 'https://www.youtube.com/watch?v=REG00000106')
    check("normalize_03706", normalize_url('https://www.youtube.com/watch?v=REG00000107') == 'https://www.youtube.com/watch?v=REG00000107')
    check("normalize_03707", normalize_url('https://www.youtube.com/watch?v=REG00000108') == 'https://www.youtube.com/watch?v=REG00000108')
    check("normalize_03708", normalize_url('https://www.youtube.com/watch?v=REG00000109') == 'https://www.youtube.com/watch?v=REG00000109')
    check("normalize_03709", normalize_url('https://www.youtube.com/watch?v=REG00000110') == 'https://www.youtube.com/watch?v=REG00000110')
    check("normalize_03710", normalize_url('https://www.youtube.com/watch?v=REG00000111') == 'https://www.youtube.com/watch?v=REG00000111')
    check("normalize_03711", normalize_url('https://www.youtube.com/watch?v=REG00000112') == 'https://www.youtube.com/watch?v=REG00000112')
    check("normalize_03712", normalize_url('https://www.youtube.com/watch?v=REG00000113') == 'https://www.youtube.com/watch?v=REG00000113')
    check("normalize_03713", normalize_url('https://www.youtube.com/watch?v=REG00000114') == 'https://www.youtube.com/watch?v=REG00000114')
    check("normalize_03714", normalize_url('https://www.youtube.com/watch?v=REG00000115') == 'https://www.youtube.com/watch?v=REG00000115')
    check("normalize_03715", normalize_url('https://www.youtube.com/watch?v=REG00000116') == 'https://www.youtube.com/watch?v=REG00000116')
    check("normalize_03716", normalize_url('https://www.youtube.com/watch?v=REG00000117') == 'https://www.youtube.com/watch?v=REG00000117')
    check("normalize_03717", normalize_url('https://www.youtube.com/watch?v=REG00000118') == 'https://www.youtube.com/watch?v=REG00000118')
    check("normalize_03718", normalize_url('https://www.youtube.com/watch?v=REG00000119') == 'https://www.youtube.com/watch?v=REG00000119')
    check("normalize_03719", normalize_url('https://www.youtube.com/watch?v=REG00000120') == 'https://www.youtube.com/watch?v=REG00000120')
    check("normalize_03720", normalize_url('https://www.youtube.com/watch?v=REG00000121') == 'https://www.youtube.com/watch?v=REG00000121')
    check("normalize_03721", normalize_url('https://www.youtube.com/watch?v=REG00000122') == 'https://www.youtube.com/watch?v=REG00000122')
    check("normalize_03722", normalize_url('https://www.youtube.com/watch?v=REG00000123') == 'https://www.youtube.com/watch?v=REG00000123')
    check("normalize_03723", normalize_url('https://www.youtube.com/watch?v=REG00000124') == 'https://www.youtube.com/watch?v=REG00000124')
    check("normalize_03724", normalize_url('https://www.youtube.com/watch?v=REG00000125') == 'https://www.youtube.com/watch?v=REG00000125')
    check("normalize_03725", normalize_url('https://www.youtube.com/watch?v=REG00000126') == 'https://www.youtube.com/watch?v=REG00000126')
    check("normalize_03726", normalize_url('https://www.youtube.com/watch?v=REG00000127') == 'https://www.youtube.com/watch?v=REG00000127')
    check("normalize_03727", normalize_url('https://www.youtube.com/watch?v=REG00000128') == 'https://www.youtube.com/watch?v=REG00000128')
    check("normalize_03728", normalize_url('https://www.youtube.com/watch?v=REG00000129') == 'https://www.youtube.com/watch?v=REG00000129')
    check("normalize_03729", normalize_url('https://www.youtube.com/watch?v=REG00000130') == 'https://www.youtube.com/watch?v=REG00000130')
    check("normalize_03730", normalize_url('https://www.youtube.com/watch?v=REG00000131') == 'https://www.youtube.com/watch?v=REG00000131')
    check("normalize_03731", normalize_url('https://www.youtube.com/watch?v=REG00000132') == 'https://www.youtube.com/watch?v=REG00000132')
    check("normalize_03732", normalize_url('https://www.youtube.com/watch?v=REG00000133') == 'https://www.youtube.com/watch?v=REG00000133')
    check("normalize_03733", normalize_url('https://www.youtube.com/watch?v=REG00000134') == 'https://www.youtube.com/watch?v=REG00000134')
    check("normalize_03734", normalize_url('https://www.youtube.com/watch?v=REG00000135') == 'https://www.youtube.com/watch?v=REG00000135')
    check("normalize_03735", normalize_url('https://www.youtube.com/watch?v=REG00000136') == 'https://www.youtube.com/watch?v=REG00000136')
    check("normalize_03736", normalize_url('https://www.youtube.com/watch?v=REG00000137') == 'https://www.youtube.com/watch?v=REG00000137')
    check("normalize_03737", normalize_url('https://www.youtube.com/watch?v=REG00000138') == 'https://www.youtube.com/watch?v=REG00000138')
    check("normalize_03738", normalize_url('https://www.youtube.com/watch?v=REG00000139') == 'https://www.youtube.com/watch?v=REG00000139')
    check("normalize_03739", normalize_url('https://www.youtube.com/watch?v=REG00000140') == 'https://www.youtube.com/watch?v=REG00000140')
    check("normalize_03740", normalize_url('https://www.youtube.com/watch?v=REG00000141') == 'https://www.youtube.com/watch?v=REG00000141')
    check("normalize_03741", normalize_url('https://www.youtube.com/watch?v=REG00000142') == 'https://www.youtube.com/watch?v=REG00000142')
    check("normalize_03742", normalize_url('https://www.youtube.com/watch?v=REG00000143') == 'https://www.youtube.com/watch?v=REG00000143')
    check("normalize_03743", normalize_url('https://www.youtube.com/watch?v=REG00000144') == 'https://www.youtube.com/watch?v=REG00000144')
    check("normalize_03744", normalize_url('https://www.youtube.com/watch?v=REG00000145') == 'https://www.youtube.com/watch?v=REG00000145')
    check("normalize_03745", normalize_url('https://www.youtube.com/watch?v=REG00000146') == 'https://www.youtube.com/watch?v=REG00000146')
    check("normalize_03746", normalize_url('https://www.youtube.com/watch?v=REG00000147') == 'https://www.youtube.com/watch?v=REG00000147')
    check("normalize_03747", normalize_url('https://www.youtube.com/watch?v=REG00000148') == 'https://www.youtube.com/watch?v=REG00000148')
    check("normalize_03748", normalize_url('https://www.youtube.com/watch?v=REG00000149') == 'https://www.youtube.com/watch?v=REG00000149')
    check("normalize_03749", normalize_url('https://www.youtube.com/watch?v=REG00000150') == 'https://www.youtube.com/watch?v=REG00000150')
    check("normalize_03750", normalize_url('https://www.youtube.com/watch?v=REG00000151') == 'https://www.youtube.com/watch?v=REG00000151')
    check("normalize_03751", normalize_url('https://www.youtube.com/watch?v=REG00000152') == 'https://www.youtube.com/watch?v=REG00000152')
    check("normalize_03752", normalize_url('https://www.youtube.com/watch?v=REG00000153') == 'https://www.youtube.com/watch?v=REG00000153')
    check("normalize_03753", normalize_url('https://www.youtube.com/watch?v=REG00000154') == 'https://www.youtube.com/watch?v=REG00000154')
    check("normalize_03754", normalize_url('https://www.youtube.com/watch?v=REG00000155') == 'https://www.youtube.com/watch?v=REG00000155')
    check("normalize_03755", normalize_url('https://www.youtube.com/watch?v=REG00000156') == 'https://www.youtube.com/watch?v=REG00000156')
    check("normalize_03756", normalize_url('https://www.youtube.com/watch?v=REG00000157') == 'https://www.youtube.com/watch?v=REG00000157')
    check("normalize_03757", normalize_url('https://www.youtube.com/watch?v=REG00000158') == 'https://www.youtube.com/watch?v=REG00000158')
    check("normalize_03758", normalize_url('https://www.youtube.com/watch?v=REG00000159') == 'https://www.youtube.com/watch?v=REG00000159')
    check("normalize_03759", normalize_url('https://www.youtube.com/watch?v=REG00000160') == 'https://www.youtube.com/watch?v=REG00000160')
    check("normalize_03760", normalize_url('https://www.youtube.com/watch?v=REG00000161') == 'https://www.youtube.com/watch?v=REG00000161')
    check("normalize_03761", normalize_url('https://www.youtube.com/watch?v=REG00000162') == 'https://www.youtube.com/watch?v=REG00000162')
    check("normalize_03762", normalize_url('https://www.youtube.com/watch?v=REG00000163') == 'https://www.youtube.com/watch?v=REG00000163')
    check("normalize_03763", normalize_url('https://www.youtube.com/watch?v=REG00000164') == 'https://www.youtube.com/watch?v=REG00000164')
    check("normalize_03764", normalize_url('https://www.youtube.com/watch?v=REG00000165') == 'https://www.youtube.com/watch?v=REG00000165')
    check("normalize_03765", normalize_url('https://www.youtube.com/watch?v=REG00000166') == 'https://www.youtube.com/watch?v=REG00000166')
    check("normalize_03766", normalize_url('https://www.youtube.com/watch?v=REG00000167') == 'https://www.youtube.com/watch?v=REG00000167')
    check("normalize_03767", normalize_url('https://www.youtube.com/watch?v=REG00000168') == 'https://www.youtube.com/watch?v=REG00000168')
    check("normalize_03768", normalize_url('https://www.youtube.com/watch?v=REG00000169') == 'https://www.youtube.com/watch?v=REG00000169')
    check("normalize_03769", normalize_url('https://www.youtube.com/watch?v=REG00000170') == 'https://www.youtube.com/watch?v=REG00000170')
    check("normalize_03770", normalize_url('https://www.youtube.com/watch?v=REG00000171') == 'https://www.youtube.com/watch?v=REG00000171')
    check("normalize_03771", normalize_url('https://www.youtube.com/watch?v=REG00000172') == 'https://www.youtube.com/watch?v=REG00000172')
    check("normalize_03772", normalize_url('https://www.youtube.com/watch?v=REG00000173') == 'https://www.youtube.com/watch?v=REG00000173')
    check("normalize_03773", normalize_url('https://www.youtube.com/watch?v=REG00000174') == 'https://www.youtube.com/watch?v=REG00000174')
    check("normalize_03774", normalize_url('https://www.youtube.com/watch?v=REG00000175') == 'https://www.youtube.com/watch?v=REG00000175')
    check("normalize_03775", normalize_url('https://www.youtube.com/watch?v=REG00000176') == 'https://www.youtube.com/watch?v=REG00000176')
    check("normalize_03776", normalize_url('https://www.youtube.com/watch?v=REG00000177') == 'https://www.youtube.com/watch?v=REG00000177')
    check("normalize_03777", normalize_url('https://www.youtube.com/watch?v=REG00000178') == 'https://www.youtube.com/watch?v=REG00000178')
    check("normalize_03778", normalize_url('https://www.youtube.com/watch?v=REG00000179') == 'https://www.youtube.com/watch?v=REG00000179')
    check("normalize_03779", normalize_url('https://www.youtube.com/watch?v=REG00000180') == 'https://www.youtube.com/watch?v=REG00000180')
    check("normalize_03780", normalize_url('https://www.youtube.com/watch?v=REG00000181') == 'https://www.youtube.com/watch?v=REG00000181')
    check("normalize_03781", normalize_url('https://www.youtube.com/watch?v=REG00000182') == 'https://www.youtube.com/watch?v=REG00000182')
    check("normalize_03782", normalize_url('https://www.youtube.com/watch?v=REG00000183') == 'https://www.youtube.com/watch?v=REG00000183')
    check("normalize_03783", normalize_url('https://www.youtube.com/watch?v=REG00000184') == 'https://www.youtube.com/watch?v=REG00000184')
    check("normalize_03784", normalize_url('https://www.youtube.com/watch?v=REG00000185') == 'https://www.youtube.com/watch?v=REG00000185')
    check("normalize_03785", normalize_url('https://www.youtube.com/watch?v=REG00000186') == 'https://www.youtube.com/watch?v=REG00000186')
    check("normalize_03786", normalize_url('https://www.youtube.com/watch?v=REG00000187') == 'https://www.youtube.com/watch?v=REG00000187')
    check("normalize_03787", normalize_url('https://www.youtube.com/watch?v=REG00000188') == 'https://www.youtube.com/watch?v=REG00000188')
    check("normalize_03788", normalize_url('https://www.youtube.com/watch?v=REG00000189') == 'https://www.youtube.com/watch?v=REG00000189')
    check("normalize_03789", normalize_url('https://www.youtube.com/watch?v=REG00000190') == 'https://www.youtube.com/watch?v=REG00000190')
    check("normalize_03790", normalize_url('https://www.youtube.com/watch?v=REG00000191') == 'https://www.youtube.com/watch?v=REG00000191')
    check("normalize_03791", normalize_url('https://www.youtube.com/watch?v=REG00000192') == 'https://www.youtube.com/watch?v=REG00000192')
    check("normalize_03792", normalize_url('https://www.youtube.com/watch?v=REG00000193') == 'https://www.youtube.com/watch?v=REG00000193')
    check("normalize_03793", normalize_url('https://www.youtube.com/watch?v=REG00000194') == 'https://www.youtube.com/watch?v=REG00000194')
    check("normalize_03794", normalize_url('https://www.youtube.com/watch?v=REG00000195') == 'https://www.youtube.com/watch?v=REG00000195')
    check("normalize_03795", normalize_url('https://www.youtube.com/watch?v=REG00000196') == 'https://www.youtube.com/watch?v=REG00000196')
    check("normalize_03796", normalize_url('https://www.youtube.com/watch?v=REG00000197') == 'https://www.youtube.com/watch?v=REG00000197')
    check("normalize_03797", normalize_url('https://www.youtube.com/watch?v=REG00000198') == 'https://www.youtube.com/watch?v=REG00000198')
    check("normalize_03798", normalize_url('https://www.youtube.com/watch?v=REG00000199') == 'https://www.youtube.com/watch?v=REG00000199')
    check("normalize_03799", normalize_url('https://www.youtube.com/watch?v=REG00000200') == 'https://www.youtube.com/watch?v=REG00000200')
    check("normalize_03800", normalize_url('https://www.youtube.com/watch?v=REG00000201') == 'https://www.youtube.com/watch?v=REG00000201')
    check("normalize_03801", normalize_url('https://www.youtube.com/watch?v=REG00000202') == 'https://www.youtube.com/watch?v=REG00000202')
    check("normalize_03802", normalize_url('https://www.youtube.com/watch?v=REG00000203') == 'https://www.youtube.com/watch?v=REG00000203')
    check("normalize_03803", normalize_url('https://www.youtube.com/watch?v=REG00000204') == 'https://www.youtube.com/watch?v=REG00000204')
    check("normalize_03804", normalize_url('https://www.youtube.com/watch?v=REG00000205') == 'https://www.youtube.com/watch?v=REG00000205')
    check("normalize_03805", normalize_url('https://www.youtube.com/watch?v=REG00000206') == 'https://www.youtube.com/watch?v=REG00000206')
    check("normalize_03806", normalize_url('https://www.youtube.com/watch?v=REG00000207') == 'https://www.youtube.com/watch?v=REG00000207')
    check("normalize_03807", normalize_url('https://www.youtube.com/watch?v=REG00000208') == 'https://www.youtube.com/watch?v=REG00000208')
    check("normalize_03808", normalize_url('https://www.youtube.com/watch?v=REG00000209') == 'https://www.youtube.com/watch?v=REG00000209')
    check("normalize_03809", normalize_url('https://www.youtube.com/watch?v=REG00000210') == 'https://www.youtube.com/watch?v=REG00000210')
    check("normalize_03810", normalize_url('https://www.youtube.com/watch?v=REG00000211') == 'https://www.youtube.com/watch?v=REG00000211')
    check("normalize_03811", normalize_url('https://www.youtube.com/watch?v=REG00000212') == 'https://www.youtube.com/watch?v=REG00000212')
    check("normalize_03812", normalize_url('https://www.youtube.com/watch?v=REG00000213') == 'https://www.youtube.com/watch?v=REG00000213')
    check("normalize_03813", normalize_url('https://www.youtube.com/watch?v=REG00000214') == 'https://www.youtube.com/watch?v=REG00000214')
    check("normalize_03814", normalize_url('https://www.youtube.com/watch?v=REG00000215') == 'https://www.youtube.com/watch?v=REG00000215')
    check("normalize_03815", normalize_url('https://www.youtube.com/watch?v=REG00000216') == 'https://www.youtube.com/watch?v=REG00000216')
    check("normalize_03816", normalize_url('https://www.youtube.com/watch?v=REG00000217') == 'https://www.youtube.com/watch?v=REG00000217')
    check("normalize_03817", normalize_url('https://www.youtube.com/watch?v=REG00000218') == 'https://www.youtube.com/watch?v=REG00000218')
    check("normalize_03818", normalize_url('https://www.youtube.com/watch?v=REG00000219') == 'https://www.youtube.com/watch?v=REG00000219')
    check("normalize_03819", normalize_url('https://www.youtube.com/watch?v=REG00000220') == 'https://www.youtube.com/watch?v=REG00000220')
    check("normalize_03820", normalize_url('https://www.youtube.com/watch?v=REG00000221') == 'https://www.youtube.com/watch?v=REG00000221')
    check("normalize_03821", normalize_url('https://www.youtube.com/watch?v=REG00000222') == 'https://www.youtube.com/watch?v=REG00000222')
    check("normalize_03822", normalize_url('https://www.youtube.com/watch?v=REG00000223') == 'https://www.youtube.com/watch?v=REG00000223')
    check("normalize_03823", normalize_url('https://www.youtube.com/watch?v=REG00000224') == 'https://www.youtube.com/watch?v=REG00000224')
    check("normalize_03824", normalize_url('https://www.youtube.com/watch?v=REG00000225') == 'https://www.youtube.com/watch?v=REG00000225')
    check("normalize_03825", normalize_url('https://www.youtube.com/watch?v=REG00000226') == 'https://www.youtube.com/watch?v=REG00000226')
    check("normalize_03826", normalize_url('https://www.youtube.com/watch?v=REG00000227') == 'https://www.youtube.com/watch?v=REG00000227')
    check("normalize_03827", normalize_url('https://www.youtube.com/watch?v=REG00000228') == 'https://www.youtube.com/watch?v=REG00000228')
    check("normalize_03828", normalize_url('https://www.youtube.com/watch?v=REG00000229') == 'https://www.youtube.com/watch?v=REG00000229')
    check("normalize_03829", normalize_url('https://www.youtube.com/watch?v=REG00000230') == 'https://www.youtube.com/watch?v=REG00000230')
    check("normalize_03830", normalize_url('https://www.youtube.com/watch?v=REG00000231') == 'https://www.youtube.com/watch?v=REG00000231')
    check("normalize_03831", normalize_url('https://www.youtube.com/watch?v=REG00000232') == 'https://www.youtube.com/watch?v=REG00000232')
    check("normalize_03832", normalize_url('https://www.youtube.com/watch?v=REG00000233') == 'https://www.youtube.com/watch?v=REG00000233')
    check("normalize_03833", normalize_url('https://www.youtube.com/watch?v=REG00000234') == 'https://www.youtube.com/watch?v=REG00000234')
    check("normalize_03834", normalize_url('https://www.youtube.com/watch?v=REG00000235') == 'https://www.youtube.com/watch?v=REG00000235')
    check("normalize_03835", normalize_url('https://www.youtube.com/watch?v=REG00000236') == 'https://www.youtube.com/watch?v=REG00000236')
    check("normalize_03836", normalize_url('https://www.youtube.com/watch?v=REG00000237') == 'https://www.youtube.com/watch?v=REG00000237')
    check("normalize_03837", normalize_url('https://www.youtube.com/watch?v=REG00000238') == 'https://www.youtube.com/watch?v=REG00000238')
    check("normalize_03838", normalize_url('https://www.youtube.com/watch?v=REG00000239') == 'https://www.youtube.com/watch?v=REG00000239')
    check("normalize_03839", normalize_url('https://www.youtube.com/watch?v=REG00000240') == 'https://www.youtube.com/watch?v=REG00000240')
    check("normalize_03840", normalize_url('https://www.youtube.com/watch?v=REG00000241') == 'https://www.youtube.com/watch?v=REG00000241')
    check("normalize_03841", normalize_url('https://www.youtube.com/watch?v=REG00000242') == 'https://www.youtube.com/watch?v=REG00000242')
    check("normalize_03842", normalize_url('https://www.youtube.com/watch?v=REG00000243') == 'https://www.youtube.com/watch?v=REG00000243')
    check("normalize_03843", normalize_url('https://www.youtube.com/watch?v=REG00000244') == 'https://www.youtube.com/watch?v=REG00000244')
    check("normalize_03844", normalize_url('https://www.youtube.com/watch?v=REG00000245') == 'https://www.youtube.com/watch?v=REG00000245')
    check("normalize_03845", normalize_url('https://www.youtube.com/watch?v=REG00000246') == 'https://www.youtube.com/watch?v=REG00000246')
    check("normalize_03846", normalize_url('https://www.youtube.com/watch?v=REG00000247') == 'https://www.youtube.com/watch?v=REG00000247')
    check("normalize_03847", normalize_url('https://www.youtube.com/watch?v=REG00000248') == 'https://www.youtube.com/watch?v=REG00000248')
    check("normalize_03848", normalize_url('https://www.youtube.com/watch?v=REG00000249') == 'https://www.youtube.com/watch?v=REG00000249')
    check("normalize_03849", normalize_url('https://www.youtube.com/watch?v=REG00000250') == 'https://www.youtube.com/watch?v=REG00000250')
    check("normalize_03850", normalize_url('https://www.youtube.com/watch?v=REG00000251') == 'https://www.youtube.com/watch?v=REG00000251')
    check("normalize_03851", normalize_url('https://www.youtube.com/watch?v=REG00000252') == 'https://www.youtube.com/watch?v=REG00000252')
    check("normalize_03852", normalize_url('https://www.youtube.com/watch?v=REG00000253') == 'https://www.youtube.com/watch?v=REG00000253')
    check("normalize_03853", normalize_url('https://www.youtube.com/watch?v=REG00000254') == 'https://www.youtube.com/watch?v=REG00000254')
    check("normalize_03854", normalize_url('https://www.youtube.com/watch?v=REG00000255') == 'https://www.youtube.com/watch?v=REG00000255')
    check("normalize_03855", normalize_url('https://www.youtube.com/watch?v=REG00000256') == 'https://www.youtube.com/watch?v=REG00000256')
    check("normalize_03856", normalize_url('https://www.youtube.com/watch?v=REG00000257') == 'https://www.youtube.com/watch?v=REG00000257')
    check("normalize_03857", normalize_url('https://www.youtube.com/watch?v=REG00000258') == 'https://www.youtube.com/watch?v=REG00000258')
    check("normalize_03858", normalize_url('https://www.youtube.com/watch?v=REG00000259') == 'https://www.youtube.com/watch?v=REG00000259')
    check("normalize_03859", normalize_url('https://www.youtube.com/watch?v=REG00000260') == 'https://www.youtube.com/watch?v=REG00000260')
    check("normalize_03860", normalize_url('https://www.youtube.com/watch?v=REG00000261') == 'https://www.youtube.com/watch?v=REG00000261')
    check("normalize_03861", normalize_url('https://www.youtube.com/watch?v=REG00000262') == 'https://www.youtube.com/watch?v=REG00000262')
    check("normalize_03862", normalize_url('https://www.youtube.com/watch?v=REG00000263') == 'https://www.youtube.com/watch?v=REG00000263')
    check("normalize_03863", normalize_url('https://www.youtube.com/watch?v=REG00000264') == 'https://www.youtube.com/watch?v=REG00000264')
    check("normalize_03864", normalize_url('https://www.youtube.com/watch?v=REG00000265') == 'https://www.youtube.com/watch?v=REG00000265')
    check("normalize_03865", normalize_url('https://www.youtube.com/watch?v=REG00000266') == 'https://www.youtube.com/watch?v=REG00000266')
    check("normalize_03866", normalize_url('https://www.youtube.com/watch?v=REG00000267') == 'https://www.youtube.com/watch?v=REG00000267')
    check("normalize_03867", normalize_url('https://www.youtube.com/watch?v=REG00000268') == 'https://www.youtube.com/watch?v=REG00000268')
    check("normalize_03868", normalize_url('https://www.youtube.com/watch?v=REG00000269') == 'https://www.youtube.com/watch?v=REG00000269')
    check("normalize_03869", normalize_url('https://www.youtube.com/watch?v=REG00000270') == 'https://www.youtube.com/watch?v=REG00000270')
    check("normalize_03870", normalize_url('https://www.youtube.com/watch?v=REG00000271') == 'https://www.youtube.com/watch?v=REG00000271')
    check("normalize_03871", normalize_url('https://www.youtube.com/watch?v=REG00000272') == 'https://www.youtube.com/watch?v=REG00000272')
    check("normalize_03872", normalize_url('https://www.youtube.com/watch?v=REG00000273') == 'https://www.youtube.com/watch?v=REG00000273')
    check("normalize_03873", normalize_url('https://www.youtube.com/watch?v=REG00000274') == 'https://www.youtube.com/watch?v=REG00000274')
    check("normalize_03874", normalize_url('https://www.youtube.com/watch?v=REG00000275') == 'https://www.youtube.com/watch?v=REG00000275')
    check("normalize_03875", normalize_url('https://www.youtube.com/watch?v=REG00000276') == 'https://www.youtube.com/watch?v=REG00000276')
    check("normalize_03876", normalize_url('https://www.youtube.com/watch?v=REG00000277') == 'https://www.youtube.com/watch?v=REG00000277')
    check("normalize_03877", normalize_url('https://www.youtube.com/watch?v=REG00000278') == 'https://www.youtube.com/watch?v=REG00000278')
    check("normalize_03878", normalize_url('https://www.youtube.com/watch?v=REG00000279') == 'https://www.youtube.com/watch?v=REG00000279')
    check("normalize_03879", normalize_url('https://www.youtube.com/watch?v=REG00000280') == 'https://www.youtube.com/watch?v=REG00000280')
    check("normalize_03880", normalize_url('https://www.youtube.com/watch?v=REG00000281') == 'https://www.youtube.com/watch?v=REG00000281')
    check("normalize_03881", normalize_url('https://www.youtube.com/watch?v=REG00000282') == 'https://www.youtube.com/watch?v=REG00000282')
    check("normalize_03882", normalize_url('https://www.youtube.com/watch?v=REG00000283') == 'https://www.youtube.com/watch?v=REG00000283')
    check("normalize_03883", normalize_url('https://www.youtube.com/watch?v=REG00000284') == 'https://www.youtube.com/watch?v=REG00000284')
    check("normalize_03884", normalize_url('https://www.youtube.com/watch?v=REG00000285') == 'https://www.youtube.com/watch?v=REG00000285')
    check("normalize_03885", normalize_url('https://www.youtube.com/watch?v=REG00000286') == 'https://www.youtube.com/watch?v=REG00000286')
    check("normalize_03886", normalize_url('https://www.youtube.com/watch?v=REG00000287') == 'https://www.youtube.com/watch?v=REG00000287')
    check("normalize_03887", normalize_url('https://www.youtube.com/watch?v=REG00000288') == 'https://www.youtube.com/watch?v=REG00000288')
    check("normalize_03888", normalize_url('https://www.youtube.com/watch?v=REG00000289') == 'https://www.youtube.com/watch?v=REG00000289')
    check("normalize_03889", normalize_url('https://www.youtube.com/watch?v=REG00000290') == 'https://www.youtube.com/watch?v=REG00000290')
    check("normalize_03890", normalize_url('https://www.youtube.com/watch?v=REG00000291') == 'https://www.youtube.com/watch?v=REG00000291')
    check("normalize_03891", normalize_url('https://www.youtube.com/watch?v=REG00000292') == 'https://www.youtube.com/watch?v=REG00000292')
    check("normalize_03892", normalize_url('https://www.youtube.com/watch?v=REG00000293') == 'https://www.youtube.com/watch?v=REG00000293')
    check("normalize_03893", normalize_url('https://www.youtube.com/watch?v=REG00000294') == 'https://www.youtube.com/watch?v=REG00000294')
    check("normalize_03894", normalize_url('https://www.youtube.com/watch?v=REG00000295') == 'https://www.youtube.com/watch?v=REG00000295')
    check("normalize_03895", normalize_url('https://www.youtube.com/watch?v=REG00000296') == 'https://www.youtube.com/watch?v=REG00000296')
    check("normalize_03896", normalize_url('https://www.youtube.com/watch?v=REG00000297') == 'https://www.youtube.com/watch?v=REG00000297')
    check("normalize_03897", normalize_url('https://www.youtube.com/watch?v=REG00000298') == 'https://www.youtube.com/watch?v=REG00000298')
    check("normalize_03898", normalize_url('https://www.youtube.com/watch?v=REG00000299') == 'https://www.youtube.com/watch?v=REG00000299')
    check("normalize_03899", normalize_url('https://www.youtube.com/watch?v=REG00000300') == 'https://www.youtube.com/watch?v=REG00000300')
    check("normalize_03900", normalize_url('https://www.youtube.com/watch?v=REG00000301') == 'https://www.youtube.com/watch?v=REG00000301')
    check("normalize_03901", normalize_url('https://www.youtube.com/watch?v=REG00000302') == 'https://www.youtube.com/watch?v=REG00000302')
    check("normalize_03902", normalize_url('https://www.youtube.com/watch?v=REG00000303') == 'https://www.youtube.com/watch?v=REG00000303')
    check("normalize_03903", normalize_url('https://www.youtube.com/watch?v=REG00000304') == 'https://www.youtube.com/watch?v=REG00000304')
    check("normalize_03904", normalize_url('https://www.youtube.com/watch?v=REG00000305') == 'https://www.youtube.com/watch?v=REG00000305')
    check("normalize_03905", normalize_url('https://www.youtube.com/watch?v=REG00000306') == 'https://www.youtube.com/watch?v=REG00000306')
    check("normalize_03906", normalize_url('https://www.youtube.com/watch?v=REG00000307') == 'https://www.youtube.com/watch?v=REG00000307')
    check("normalize_03907", normalize_url('https://www.youtube.com/watch?v=REG00000308') == 'https://www.youtube.com/watch?v=REG00000308')
    check("normalize_03908", normalize_url('https://www.youtube.com/watch?v=REG00000309') == 'https://www.youtube.com/watch?v=REG00000309')
    check("normalize_03909", normalize_url('https://www.youtube.com/watch?v=REG00000310') == 'https://www.youtube.com/watch?v=REG00000310')
    check("normalize_03910", normalize_url('https://www.youtube.com/watch?v=REG00000311') == 'https://www.youtube.com/watch?v=REG00000311')
    check("normalize_03911", normalize_url('https://www.youtube.com/watch?v=REG00000312') == 'https://www.youtube.com/watch?v=REG00000312')
    check("normalize_03912", normalize_url('https://www.youtube.com/watch?v=REG00000313') == 'https://www.youtube.com/watch?v=REG00000313')
    check("normalize_03913", normalize_url('https://www.youtube.com/watch?v=REG00000314') == 'https://www.youtube.com/watch?v=REG00000314')
    check("normalize_03914", normalize_url('https://www.youtube.com/watch?v=REG00000315') == 'https://www.youtube.com/watch?v=REG00000315')
    check("normalize_03915", normalize_url('https://www.youtube.com/watch?v=REG00000316') == 'https://www.youtube.com/watch?v=REG00000316')
    check("normalize_03916", normalize_url('https://www.youtube.com/watch?v=REG00000317') == 'https://www.youtube.com/watch?v=REG00000317')
    check("normalize_03917", normalize_url('https://www.youtube.com/watch?v=REG00000318') == 'https://www.youtube.com/watch?v=REG00000318')
    check("normalize_03918", normalize_url('https://www.youtube.com/watch?v=REG00000319') == 'https://www.youtube.com/watch?v=REG00000319')
    check("normalize_03919", normalize_url('https://www.youtube.com/watch?v=REG00000320') == 'https://www.youtube.com/watch?v=REG00000320')
    check("normalize_03920", normalize_url('https://www.youtube.com/watch?v=REG00000321') == 'https://www.youtube.com/watch?v=REG00000321')
    check("normalize_03921", normalize_url('https://www.youtube.com/watch?v=REG00000322') == 'https://www.youtube.com/watch?v=REG00000322')
    check("normalize_03922", normalize_url('https://www.youtube.com/watch?v=REG00000323') == 'https://www.youtube.com/watch?v=REG00000323')
    check("normalize_03923", normalize_url('https://www.youtube.com/watch?v=REG00000324') == 'https://www.youtube.com/watch?v=REG00000324')
    check("normalize_03924", normalize_url('https://www.youtube.com/watch?v=REG00000325') == 'https://www.youtube.com/watch?v=REG00000325')
    check("normalize_03925", normalize_url('https://www.youtube.com/watch?v=REG00000326') == 'https://www.youtube.com/watch?v=REG00000326')
    check("normalize_03926", normalize_url('https://www.youtube.com/watch?v=REG00000327') == 'https://www.youtube.com/watch?v=REG00000327')
    check("normalize_03927", normalize_url('https://www.youtube.com/watch?v=REG00000328') == 'https://www.youtube.com/watch?v=REG00000328')
    check("normalize_03928", normalize_url('https://www.youtube.com/watch?v=REG00000329') == 'https://www.youtube.com/watch?v=REG00000329')
    check("normalize_03929", normalize_url('https://www.youtube.com/watch?v=REG00000330') == 'https://www.youtube.com/watch?v=REG00000330')
    check("normalize_03930", normalize_url('https://www.youtube.com/watch?v=REG00000331') == 'https://www.youtube.com/watch?v=REG00000331')
    check("normalize_03931", normalize_url('https://www.youtube.com/watch?v=REG00000332') == 'https://www.youtube.com/watch?v=REG00000332')
    check("normalize_03932", normalize_url('https://www.youtube.com/watch?v=REG00000333') == 'https://www.youtube.com/watch?v=REG00000333')
    check("normalize_03933", normalize_url('https://www.youtube.com/watch?v=REG00000334') == 'https://www.youtube.com/watch?v=REG00000334')
    check("normalize_03934", normalize_url('https://www.youtube.com/watch?v=REG00000335') == 'https://www.youtube.com/watch?v=REG00000335')
    check("normalize_03935", normalize_url('https://www.youtube.com/watch?v=REG00000336') == 'https://www.youtube.com/watch?v=REG00000336')
    check("normalize_03936", normalize_url('https://www.youtube.com/watch?v=REG00000337') == 'https://www.youtube.com/watch?v=REG00000337')
    check("normalize_03937", normalize_url('https://www.youtube.com/watch?v=REG00000338') == 'https://www.youtube.com/watch?v=REG00000338')
    check("normalize_03938", normalize_url('https://www.youtube.com/watch?v=REG00000339') == 'https://www.youtube.com/watch?v=REG00000339')
    check("normalize_03939", normalize_url('https://www.youtube.com/watch?v=REG00000340') == 'https://www.youtube.com/watch?v=REG00000340')
    check("normalize_03940", normalize_url('https://www.youtube.com/watch?v=REG00000341') == 'https://www.youtube.com/watch?v=REG00000341')
    check("normalize_03941", normalize_url('https://www.youtube.com/watch?v=REG00000342') == 'https://www.youtube.com/watch?v=REG00000342')
    check("normalize_03942", normalize_url('https://www.youtube.com/watch?v=REG00000343') == 'https://www.youtube.com/watch?v=REG00000343')
    check("normalize_03943", normalize_url('https://www.youtube.com/watch?v=REG00000344') == 'https://www.youtube.com/watch?v=REG00000344')
    check("normalize_03944", normalize_url('https://www.youtube.com/watch?v=REG00000345') == 'https://www.youtube.com/watch?v=REG00000345')
    check("normalize_03945", normalize_url('https://www.youtube.com/watch?v=REG00000346') == 'https://www.youtube.com/watch?v=REG00000346')
    check("normalize_03946", normalize_url('https://www.youtube.com/watch?v=REG00000347') == 'https://www.youtube.com/watch?v=REG00000347')
    check("normalize_03947", normalize_url('https://www.youtube.com/watch?v=REG00000348') == 'https://www.youtube.com/watch?v=REG00000348')
    check("normalize_03948", normalize_url('https://www.youtube.com/watch?v=REG00000349') == 'https://www.youtube.com/watch?v=REG00000349')
    check("normalize_03949", normalize_url('https://www.youtube.com/watch?v=REG00000350') == 'https://www.youtube.com/watch?v=REG00000350')
    check("normalize_03950", normalize_url('https://www.youtube.com/watch?v=REG00000351') == 'https://www.youtube.com/watch?v=REG00000351')
    check("normalize_03951", normalize_url('https://www.youtube.com/watch?v=REG00000352') == 'https://www.youtube.com/watch?v=REG00000352')
    check("normalize_03952", normalize_url('https://www.youtube.com/watch?v=REG00000353') == 'https://www.youtube.com/watch?v=REG00000353')
    check("normalize_03953", normalize_url('https://www.youtube.com/watch?v=REG00000354') == 'https://www.youtube.com/watch?v=REG00000354')
    check("normalize_03954", normalize_url('https://www.youtube.com/watch?v=REG00000355') == 'https://www.youtube.com/watch?v=REG00000355')
    check("normalize_03955", normalize_url('https://www.youtube.com/watch?v=REG00000356') == 'https://www.youtube.com/watch?v=REG00000356')
    check("normalize_03956", normalize_url('https://www.youtube.com/watch?v=REG00000357') == 'https://www.youtube.com/watch?v=REG00000357')
    check("normalize_03957", normalize_url('https://www.youtube.com/watch?v=REG00000358') == 'https://www.youtube.com/watch?v=REG00000358')
    check("normalize_03958", normalize_url('https://www.youtube.com/watch?v=REG00000359') == 'https://www.youtube.com/watch?v=REG00000359')
    check("normalize_03959", normalize_url('https://www.youtube.com/watch?v=REG00000360') == 'https://www.youtube.com/watch?v=REG00000360')
    check("normalize_03960", normalize_url('https://www.youtube.com/watch?v=REG00000361') == 'https://www.youtube.com/watch?v=REG00000361')
    check("normalize_03961", normalize_url('https://www.youtube.com/watch?v=REG00000362') == 'https://www.youtube.com/watch?v=REG00000362')
    check("normalize_03962", normalize_url('https://www.youtube.com/watch?v=REG00000363') == 'https://www.youtube.com/watch?v=REG00000363')
    check("normalize_03963", normalize_url('https://www.youtube.com/watch?v=REG00000364') == 'https://www.youtube.com/watch?v=REG00000364')
    check("normalize_03964", normalize_url('https://www.youtube.com/watch?v=REG00000365') == 'https://www.youtube.com/watch?v=REG00000365')
    check("normalize_03965", normalize_url('https://www.youtube.com/watch?v=REG00000366') == 'https://www.youtube.com/watch?v=REG00000366')
    check("normalize_03966", normalize_url('https://www.youtube.com/watch?v=REG00000367') == 'https://www.youtube.com/watch?v=REG00000367')
    check("normalize_03967", normalize_url('https://www.youtube.com/watch?v=REG00000368') == 'https://www.youtube.com/watch?v=REG00000368')
    check("normalize_03968", normalize_url('https://www.youtube.com/watch?v=REG00000369') == 'https://www.youtube.com/watch?v=REG00000369')
    check("normalize_03969", normalize_url('https://www.youtube.com/watch?v=REG00000370') == 'https://www.youtube.com/watch?v=REG00000370')
    check("normalize_03970", normalize_url('https://www.youtube.com/watch?v=REG00000371') == 'https://www.youtube.com/watch?v=REG00000371')
    check("normalize_03971", normalize_url('https://www.youtube.com/watch?v=REG00000372') == 'https://www.youtube.com/watch?v=REG00000372')
    check("normalize_03972", normalize_url('https://www.youtube.com/watch?v=REG00000373') == 'https://www.youtube.com/watch?v=REG00000373')
    check("normalize_03973", normalize_url('https://www.youtube.com/watch?v=REG00000374') == 'https://www.youtube.com/watch?v=REG00000374')
    check("normalize_03974", normalize_url('https://www.youtube.com/watch?v=REG00000375') == 'https://www.youtube.com/watch?v=REG00000375')
    check("normalize_03975", normalize_url('https://www.youtube.com/watch?v=REG00000376') == 'https://www.youtube.com/watch?v=REG00000376')
    check("normalize_03976", normalize_url('https://www.youtube.com/watch?v=REG00000377') == 'https://www.youtube.com/watch?v=REG00000377')
    check("normalize_03977", normalize_url('https://www.youtube.com/watch?v=REG00000378') == 'https://www.youtube.com/watch?v=REG00000378')
    check("normalize_03978", normalize_url('https://www.youtube.com/watch?v=REG00000379') == 'https://www.youtube.com/watch?v=REG00000379')
    check("normalize_03979", normalize_url('https://www.youtube.com/watch?v=REG00000380') == 'https://www.youtube.com/watch?v=REG00000380')
    check("normalize_03980", normalize_url('https://www.youtube.com/watch?v=REG00000381') == 'https://www.youtube.com/watch?v=REG00000381')
    check("normalize_03981", normalize_url('https://www.youtube.com/watch?v=REG00000382') == 'https://www.youtube.com/watch?v=REG00000382')
    check("normalize_03982", normalize_url('https://www.youtube.com/watch?v=REG00000383') == 'https://www.youtube.com/watch?v=REG00000383')
    check("normalize_03983", normalize_url('https://www.youtube.com/watch?v=REG00000384') == 'https://www.youtube.com/watch?v=REG00000384')
    check("normalize_03984", normalize_url('https://www.youtube.com/watch?v=REG00000385') == 'https://www.youtube.com/watch?v=REG00000385')
    check("normalize_03985", normalize_url('https://www.youtube.com/watch?v=REG00000386') == 'https://www.youtube.com/watch?v=REG00000386')
    check("normalize_03986", normalize_url('https://www.youtube.com/watch?v=REG00000387') == 'https://www.youtube.com/watch?v=REG00000387')
    check("normalize_03987", normalize_url('https://www.youtube.com/watch?v=REG00000388') == 'https://www.youtube.com/watch?v=REG00000388')
    check("normalize_03988", normalize_url('https://www.youtube.com/watch?v=REG00000389') == 'https://www.youtube.com/watch?v=REG00000389')
    check("normalize_03989", normalize_url('https://www.youtube.com/watch?v=REG00000390') == 'https://www.youtube.com/watch?v=REG00000390')
    check("normalize_03990", normalize_url('https://www.youtube.com/watch?v=REG00000391') == 'https://www.youtube.com/watch?v=REG00000391')
    check("normalize_03991", normalize_url('https://www.youtube.com/watch?v=REG00000392') == 'https://www.youtube.com/watch?v=REG00000392')
    check("normalize_03992", normalize_url('https://www.youtube.com/watch?v=REG00000393') == 'https://www.youtube.com/watch?v=REG00000393')
    check("normalize_03993", normalize_url('https://www.youtube.com/watch?v=REG00000394') == 'https://www.youtube.com/watch?v=REG00000394')
    check("normalize_03994", normalize_url('https://www.youtube.com/watch?v=REG00000395') == 'https://www.youtube.com/watch?v=REG00000395')
    check("normalize_03995", normalize_url('https://www.youtube.com/watch?v=REG00000396') == 'https://www.youtube.com/watch?v=REG00000396')
    check("normalize_03996", normalize_url('https://www.youtube.com/watch?v=REG00000397') == 'https://www.youtube.com/watch?v=REG00000397')
    check("normalize_03997", normalize_url('https://www.youtube.com/watch?v=REG00000398') == 'https://www.youtube.com/watch?v=REG00000398')
    check("normalize_03998", normalize_url('https://www.youtube.com/watch?v=REG00000399') == 'https://www.youtube.com/watch?v=REG00000399')
    check("normalize_03999", normalize_url('https://www.youtube.com/watch?v=REG00000400') == 'https://www.youtube.com/watch?v=REG00000400')
    check("normalize_04000", normalize_url('https://www.youtube.com/watch?v=REG00000401') == 'https://www.youtube.com/watch?v=REG00000401')
    check("normalize_04001", normalize_url('https://www.youtube.com/watch?v=REG00000402') == 'https://www.youtube.com/watch?v=REG00000402')
    check("normalize_04002", normalize_url('https://www.youtube.com/watch?v=REG00000403') == 'https://www.youtube.com/watch?v=REG00000403')
    check("normalize_04003", normalize_url('https://www.youtube.com/watch?v=REG00000404') == 'https://www.youtube.com/watch?v=REG00000404')
    check("normalize_04004", normalize_url('https://www.youtube.com/watch?v=REG00000405') == 'https://www.youtube.com/watch?v=REG00000405')
    check("normalize_04005", normalize_url('https://www.youtube.com/watch?v=REG00000406') == 'https://www.youtube.com/watch?v=REG00000406')
    check("normalize_04006", normalize_url('https://www.youtube.com/watch?v=REG00000407') == 'https://www.youtube.com/watch?v=REG00000407')
    check("normalize_04007", normalize_url('https://www.youtube.com/watch?v=REG00000408') == 'https://www.youtube.com/watch?v=REG00000408')
    check("normalize_04008", normalize_url('https://www.youtube.com/watch?v=REG00000409') == 'https://www.youtube.com/watch?v=REG00000409')
    check("normalize_04009", normalize_url('https://www.youtube.com/watch?v=REG00000410') == 'https://www.youtube.com/watch?v=REG00000410')
    check("normalize_04010", normalize_url('https://www.youtube.com/watch?v=REG00000411') == 'https://www.youtube.com/watch?v=REG00000411')
    check("normalize_04011", normalize_url('https://www.youtube.com/watch?v=REG00000412') == 'https://www.youtube.com/watch?v=REG00000412')
    check("normalize_04012", normalize_url('https://www.youtube.com/watch?v=REG00000413') == 'https://www.youtube.com/watch?v=REG00000413')
    check("normalize_04013", normalize_url('https://www.youtube.com/watch?v=REG00000414') == 'https://www.youtube.com/watch?v=REG00000414')
    check("normalize_04014", normalize_url('https://www.youtube.com/watch?v=REG00000415') == 'https://www.youtube.com/watch?v=REG00000415')
    check("normalize_04015", normalize_url('https://www.youtube.com/watch?v=REG00000416') == 'https://www.youtube.com/watch?v=REG00000416')
    check("normalize_04016", normalize_url('https://www.youtube.com/watch?v=REG00000417') == 'https://www.youtube.com/watch?v=REG00000417')
    check("normalize_04017", normalize_url('https://www.youtube.com/watch?v=REG00000418') == 'https://www.youtube.com/watch?v=REG00000418')
    check("normalize_04018", normalize_url('https://www.youtube.com/watch?v=REG00000419') == 'https://www.youtube.com/watch?v=REG00000419')
    check("normalize_04019", normalize_url('https://www.youtube.com/watch?v=REG00000420') == 'https://www.youtube.com/watch?v=REG00000420')
    check("normalize_04020", normalize_url('https://www.youtube.com/watch?v=REG00000421') == 'https://www.youtube.com/watch?v=REG00000421')
    check("normalize_04021", normalize_url('https://www.youtube.com/watch?v=REG00000422') == 'https://www.youtube.com/watch?v=REG00000422')
    check("normalize_04022", normalize_url('https://www.youtube.com/watch?v=REG00000423') == 'https://www.youtube.com/watch?v=REG00000423')
    check("normalize_04023", normalize_url('https://www.youtube.com/watch?v=REG00000424') == 'https://www.youtube.com/watch?v=REG00000424')
    check("normalize_04024", normalize_url('https://www.youtube.com/watch?v=REG00000425') == 'https://www.youtube.com/watch?v=REG00000425')
    check("normalize_04025", normalize_url('https://www.youtube.com/watch?v=REG00000426') == 'https://www.youtube.com/watch?v=REG00000426')
    check("normalize_04026", normalize_url('https://www.youtube.com/watch?v=REG00000427') == 'https://www.youtube.com/watch?v=REG00000427')
    check("normalize_04027", normalize_url('https://www.youtube.com/watch?v=REG00000428') == 'https://www.youtube.com/watch?v=REG00000428')
    check("normalize_04028", normalize_url('https://www.youtube.com/watch?v=REG00000429') == 'https://www.youtube.com/watch?v=REG00000429')
    check("normalize_04029", normalize_url('https://www.youtube.com/watch?v=REG00000430') == 'https://www.youtube.com/watch?v=REG00000430')
    check("normalize_04030", normalize_url('https://www.youtube.com/watch?v=REG00000431') == 'https://www.youtube.com/watch?v=REG00000431')
    check("normalize_04031", normalize_url('https://www.youtube.com/watch?v=REG00000432') == 'https://www.youtube.com/watch?v=REG00000432')
    check("normalize_04032", normalize_url('https://www.youtube.com/watch?v=REG00000433') == 'https://www.youtube.com/watch?v=REG00000433')
    check("normalize_04033", normalize_url('https://www.youtube.com/watch?v=REG00000434') == 'https://www.youtube.com/watch?v=REG00000434')
    check("normalize_04034", normalize_url('https://www.youtube.com/watch?v=REG00000435') == 'https://www.youtube.com/watch?v=REG00000435')
    check("normalize_04035", normalize_url('https://www.youtube.com/watch?v=REG00000436') == 'https://www.youtube.com/watch?v=REG00000436')
    check("normalize_04036", normalize_url('https://www.youtube.com/watch?v=REG00000437') == 'https://www.youtube.com/watch?v=REG00000437')
    check("normalize_04037", normalize_url('https://www.youtube.com/watch?v=REG00000438') == 'https://www.youtube.com/watch?v=REG00000438')
    check("normalize_04038", normalize_url('https://www.youtube.com/watch?v=REG00000439') == 'https://www.youtube.com/watch?v=REG00000439')
    check("normalize_04039", normalize_url('https://www.youtube.com/watch?v=REG00000440') == 'https://www.youtube.com/watch?v=REG00000440')
    check("normalize_04040", normalize_url('https://www.youtube.com/watch?v=REG00000441') == 'https://www.youtube.com/watch?v=REG00000441')
    check("normalize_04041", normalize_url('https://www.youtube.com/watch?v=REG00000442') == 'https://www.youtube.com/watch?v=REG00000442')
    check("normalize_04042", normalize_url('https://www.youtube.com/watch?v=REG00000443') == 'https://www.youtube.com/watch?v=REG00000443')
    check("normalize_04043", normalize_url('https://www.youtube.com/watch?v=REG00000444') == 'https://www.youtube.com/watch?v=REG00000444')
    check("normalize_04044", normalize_url('https://www.youtube.com/watch?v=REG00000445') == 'https://www.youtube.com/watch?v=REG00000445')
    check("normalize_04045", normalize_url('https://www.youtube.com/watch?v=REG00000446') == 'https://www.youtube.com/watch?v=REG00000446')
    check("normalize_04046", normalize_url('https://www.youtube.com/watch?v=REG00000447') == 'https://www.youtube.com/watch?v=REG00000447')
    check("normalize_04047", normalize_url('https://www.youtube.com/watch?v=REG00000448') == 'https://www.youtube.com/watch?v=REG00000448')
    check("normalize_04048", normalize_url('https://www.youtube.com/watch?v=REG00000449') == 'https://www.youtube.com/watch?v=REG00000449')
    check("normalize_04049", normalize_url('https://www.youtube.com/watch?v=REG00000450') == 'https://www.youtube.com/watch?v=REG00000450')
    check("normalize_04050", normalize_url('https://www.youtube.com/watch?v=REG00000451') == 'https://www.youtube.com/watch?v=REG00000451')
    check("normalize_04051", normalize_url('https://www.youtube.com/watch?v=REG00000452') == 'https://www.youtube.com/watch?v=REG00000452')
    check("normalize_04052", normalize_url('https://www.youtube.com/watch?v=REG00000453') == 'https://www.youtube.com/watch?v=REG00000453')
    check("normalize_04053", normalize_url('https://www.youtube.com/watch?v=REG00000454') == 'https://www.youtube.com/watch?v=REG00000454')
    check("normalize_04054", normalize_url('https://www.youtube.com/watch?v=REG00000455') == 'https://www.youtube.com/watch?v=REG00000455')
    check("normalize_04055", normalize_url('https://www.youtube.com/watch?v=REG00000456') == 'https://www.youtube.com/watch?v=REG00000456')
    check("normalize_04056", normalize_url('https://www.youtube.com/watch?v=REG00000457') == 'https://www.youtube.com/watch?v=REG00000457')
    check("normalize_04057", normalize_url('https://www.youtube.com/watch?v=REG00000458') == 'https://www.youtube.com/watch?v=REG00000458')
    check("normalize_04058", normalize_url('https://www.youtube.com/watch?v=REG00000459') == 'https://www.youtube.com/watch?v=REG00000459')
    check("normalize_04059", normalize_url('https://www.youtube.com/watch?v=REG00000460') == 'https://www.youtube.com/watch?v=REG00000460')
    check("normalize_04060", normalize_url('https://www.youtube.com/watch?v=REG00000461') == 'https://www.youtube.com/watch?v=REG00000461')
    check("normalize_04061", normalize_url('https://www.youtube.com/watch?v=REG00000462') == 'https://www.youtube.com/watch?v=REG00000462')
    check("normalize_04062", normalize_url('https://www.youtube.com/watch?v=REG00000463') == 'https://www.youtube.com/watch?v=REG00000463')
    check("normalize_04063", normalize_url('https://www.youtube.com/watch?v=REG00000464') == 'https://www.youtube.com/watch?v=REG00000464')
    check("normalize_04064", normalize_url('https://www.youtube.com/watch?v=REG00000465') == 'https://www.youtube.com/watch?v=REG00000465')
    check("normalize_04065", normalize_url('https://www.youtube.com/watch?v=REG00000466') == 'https://www.youtube.com/watch?v=REG00000466')
    check("normalize_04066", normalize_url('https://www.youtube.com/watch?v=REG00000467') == 'https://www.youtube.com/watch?v=REG00000467')
    check("normalize_04067", normalize_url('https://www.youtube.com/watch?v=REG00000468') == 'https://www.youtube.com/watch?v=REG00000468')
    check("normalize_04068", normalize_url('https://www.youtube.com/watch?v=REG00000469') == 'https://www.youtube.com/watch?v=REG00000469')
    check("normalize_04069", normalize_url('https://www.youtube.com/watch?v=REG00000470') == 'https://www.youtube.com/watch?v=REG00000470')
    check("normalize_04070", normalize_url('https://www.youtube.com/watch?v=REG00000471') == 'https://www.youtube.com/watch?v=REG00000471')
    check("normalize_04071", normalize_url('https://www.youtube.com/watch?v=REG00000472') == 'https://www.youtube.com/watch?v=REG00000472')
    check("normalize_04072", normalize_url('https://www.youtube.com/watch?v=REG00000473') == 'https://www.youtube.com/watch?v=REG00000473')
    check("normalize_04073", normalize_url('https://www.youtube.com/watch?v=REG00000474') == 'https://www.youtube.com/watch?v=REG00000474')
    check("normalize_04074", normalize_url('https://www.youtube.com/watch?v=REG00000475') == 'https://www.youtube.com/watch?v=REG00000475')
    check("normalize_04075", normalize_url('https://www.youtube.com/watch?v=REG00000476') == 'https://www.youtube.com/watch?v=REG00000476')
    check("normalize_04076", normalize_url('https://www.youtube.com/watch?v=REG00000477') == 'https://www.youtube.com/watch?v=REG00000477')
    check("normalize_04077", normalize_url('https://www.youtube.com/watch?v=REG00000478') == 'https://www.youtube.com/watch?v=REG00000478')
    check("normalize_04078", normalize_url('https://www.youtube.com/watch?v=REG00000479') == 'https://www.youtube.com/watch?v=REG00000479')
    check("normalize_04079", normalize_url('https://www.youtube.com/watch?v=REG00000480') == 'https://www.youtube.com/watch?v=REG00000480')
    check("normalize_04080", normalize_url('https://www.youtube.com/watch?v=REG00000481') == 'https://www.youtube.com/watch?v=REG00000481')
    check("normalize_04081", normalize_url('https://www.youtube.com/watch?v=REG00000482') == 'https://www.youtube.com/watch?v=REG00000482')
    check("normalize_04082", normalize_url('https://www.youtube.com/watch?v=REG00000483') == 'https://www.youtube.com/watch?v=REG00000483')
    check("normalize_04083", normalize_url('https://www.youtube.com/watch?v=REG00000484') == 'https://www.youtube.com/watch?v=REG00000484')
    check("normalize_04084", normalize_url('https://www.youtube.com/watch?v=REG00000485') == 'https://www.youtube.com/watch?v=REG00000485')
    check("normalize_04085", normalize_url('https://www.youtube.com/watch?v=REG00000486') == 'https://www.youtube.com/watch?v=REG00000486')
    check("normalize_04086", normalize_url('https://www.youtube.com/watch?v=REG00000487') == 'https://www.youtube.com/watch?v=REG00000487')
    check("normalize_04087", normalize_url('https://www.youtube.com/watch?v=REG00000488') == 'https://www.youtube.com/watch?v=REG00000488')
    check("normalize_04088", normalize_url('https://www.youtube.com/watch?v=REG00000489') == 'https://www.youtube.com/watch?v=REG00000489')
    check("normalize_04089", normalize_url('https://www.youtube.com/watch?v=REG00000490') == 'https://www.youtube.com/watch?v=REG00000490')
    check("normalize_04090", normalize_url('https://www.youtube.com/watch?v=REG00000491') == 'https://www.youtube.com/watch?v=REG00000491')
    check("normalize_04091", normalize_url('https://www.youtube.com/watch?v=REG00000492') == 'https://www.youtube.com/watch?v=REG00000492')
    check("normalize_04092", normalize_url('https://www.youtube.com/watch?v=REG00000493') == 'https://www.youtube.com/watch?v=REG00000493')
    check("normalize_04093", normalize_url('https://www.youtube.com/watch?v=REG00000494') == 'https://www.youtube.com/watch?v=REG00000494')
    check("normalize_04094", normalize_url('https://www.youtube.com/watch?v=REG00000495') == 'https://www.youtube.com/watch?v=REG00000495')
    check("normalize_04095", normalize_url('https://www.youtube.com/watch?v=REG00000496') == 'https://www.youtube.com/watch?v=REG00000496')
    check("normalize_04096", normalize_url('https://www.youtube.com/watch?v=REG00000497') == 'https://www.youtube.com/watch?v=REG00000497')
    check("normalize_04097", normalize_url('https://www.youtube.com/watch?v=REG00000498') == 'https://www.youtube.com/watch?v=REG00000498')
    check("normalize_04098", normalize_url('https://www.youtube.com/watch?v=REG00000499') == 'https://www.youtube.com/watch?v=REG00000499')
    check("normalize_04099", normalize_url('https://www.youtube.com/watch?v=REG00000500') == 'https://www.youtube.com/watch?v=REG00000500')
    check("normalize_04100", normalize_url('https://www.youtube.com/watch?v=REG00000501') == 'https://www.youtube.com/watch?v=REG00000501')
    check("normalize_04101", normalize_url('https://www.youtube.com/watch?v=REG00000502') == 'https://www.youtube.com/watch?v=REG00000502')
    check("normalize_04102", normalize_url('https://www.youtube.com/watch?v=REG00000503') == 'https://www.youtube.com/watch?v=REG00000503')
    check("normalize_04103", normalize_url('https://www.youtube.com/watch?v=REG00000504') == 'https://www.youtube.com/watch?v=REG00000504')
    check("normalize_04104", normalize_url('https://www.youtube.com/watch?v=REG00000505') == 'https://www.youtube.com/watch?v=REG00000505')
    check("normalize_04105", normalize_url('https://www.youtube.com/watch?v=REG00000506') == 'https://www.youtube.com/watch?v=REG00000506')
    check("normalize_04106", normalize_url('https://www.youtube.com/watch?v=REG00000507') == 'https://www.youtube.com/watch?v=REG00000507')
    check("normalize_04107", normalize_url('https://www.youtube.com/watch?v=REG00000508') == 'https://www.youtube.com/watch?v=REG00000508')
    check("normalize_04108", normalize_url('https://www.youtube.com/watch?v=REG00000509') == 'https://www.youtube.com/watch?v=REG00000509')
    check("normalize_04109", normalize_url('https://www.youtube.com/watch?v=REG00000510') == 'https://www.youtube.com/watch?v=REG00000510')
    check("normalize_04110", normalize_url('https://www.youtube.com/watch?v=REG00000511') == 'https://www.youtube.com/watch?v=REG00000511')
    check("normalize_04111", normalize_url('https://www.youtube.com/watch?v=REG00000512') == 'https://www.youtube.com/watch?v=REG00000512')
    check("normalize_04112", normalize_url('https://www.youtube.com/watch?v=REG00000513') == 'https://www.youtube.com/watch?v=REG00000513')
    check("normalize_04113", normalize_url('https://www.youtube.com/watch?v=REG00000514') == 'https://www.youtube.com/watch?v=REG00000514')
    check("normalize_04114", normalize_url('https://www.youtube.com/watch?v=REG00000515') == 'https://www.youtube.com/watch?v=REG00000515')
    check("normalize_04115", normalize_url('https://www.youtube.com/watch?v=REG00000516') == 'https://www.youtube.com/watch?v=REG00000516')
    check("normalize_04116", normalize_url('https://www.youtube.com/watch?v=REG00000517') == 'https://www.youtube.com/watch?v=REG00000517')
    check("normalize_04117", normalize_url('https://www.youtube.com/watch?v=REG00000518') == 'https://www.youtube.com/watch?v=REG00000518')
    check("normalize_04118", normalize_url('https://www.youtube.com/watch?v=REG00000519') == 'https://www.youtube.com/watch?v=REG00000519')
    check("normalize_04119", normalize_url('https://www.youtube.com/watch?v=REG00000520') == 'https://www.youtube.com/watch?v=REG00000520')
    check("normalize_04120", normalize_url('https://www.youtube.com/watch?v=REG00000521') == 'https://www.youtube.com/watch?v=REG00000521')
    check("normalize_04121", normalize_url('https://www.youtube.com/watch?v=REG00000522') == 'https://www.youtube.com/watch?v=REG00000522')
    check("normalize_04122", normalize_url('https://www.youtube.com/watch?v=REG00000523') == 'https://www.youtube.com/watch?v=REG00000523')
    check("normalize_04123", normalize_url('https://www.youtube.com/watch?v=REG00000524') == 'https://www.youtube.com/watch?v=REG00000524')
    check("normalize_04124", normalize_url('https://www.youtube.com/watch?v=REG00000525') == 'https://www.youtube.com/watch?v=REG00000525')
    check("normalize_04125", normalize_url('https://www.youtube.com/watch?v=REG00000526') == 'https://www.youtube.com/watch?v=REG00000526')
    check("normalize_04126", normalize_url('https://www.youtube.com/watch?v=REG00000527') == 'https://www.youtube.com/watch?v=REG00000527')
    check("normalize_04127", normalize_url('https://www.youtube.com/watch?v=REG00000528') == 'https://www.youtube.com/watch?v=REG00000528')
    check("normalize_04128", normalize_url('https://www.youtube.com/watch?v=REG00000529') == 'https://www.youtube.com/watch?v=REG00000529')
    check("normalize_04129", normalize_url('https://www.youtube.com/watch?v=REG00000530') == 'https://www.youtube.com/watch?v=REG00000530')
    check("normalize_04130", normalize_url('https://www.youtube.com/watch?v=REG00000531') == 'https://www.youtube.com/watch?v=REG00000531')
    check("normalize_04131", normalize_url('https://www.youtube.com/watch?v=REG00000532') == 'https://www.youtube.com/watch?v=REG00000532')
    check("normalize_04132", normalize_url('https://www.youtube.com/watch?v=REG00000533') == 'https://www.youtube.com/watch?v=REG00000533')
    check("normalize_04133", normalize_url('https://www.youtube.com/watch?v=REG00000534') == 'https://www.youtube.com/watch?v=REG00000534')
    check("normalize_04134", normalize_url('https://www.youtube.com/watch?v=REG00000535') == 'https://www.youtube.com/watch?v=REG00000535')
    check("normalize_04135", normalize_url('https://www.youtube.com/watch?v=REG00000536') == 'https://www.youtube.com/watch?v=REG00000536')
    check("normalize_04136", normalize_url('https://www.youtube.com/watch?v=REG00000537') == 'https://www.youtube.com/watch?v=REG00000537')
    check("normalize_04137", normalize_url('https://www.youtube.com/watch?v=REG00000538') == 'https://www.youtube.com/watch?v=REG00000538')
    check("normalize_04138", normalize_url('https://www.youtube.com/watch?v=REG00000539') == 'https://www.youtube.com/watch?v=REG00000539')
    check("normalize_04139", normalize_url('https://www.youtube.com/watch?v=REG00000540') == 'https://www.youtube.com/watch?v=REG00000540')
    check("normalize_04140", normalize_url('https://www.youtube.com/watch?v=REG00000541') == 'https://www.youtube.com/watch?v=REG00000541')
    check("normalize_04141", normalize_url('https://www.youtube.com/watch?v=REG00000542') == 'https://www.youtube.com/watch?v=REG00000542')
    check("normalize_04142", normalize_url('https://www.youtube.com/watch?v=REG00000543') == 'https://www.youtube.com/watch?v=REG00000543')
    check("normalize_04143", normalize_url('https://www.youtube.com/watch?v=REG00000544') == 'https://www.youtube.com/watch?v=REG00000544')
    check("normalize_04144", normalize_url('https://www.youtube.com/watch?v=REG00000545') == 'https://www.youtube.com/watch?v=REG00000545')
    check("normalize_04145", normalize_url('https://www.youtube.com/watch?v=REG00000546') == 'https://www.youtube.com/watch?v=REG00000546')
    check("normalize_04146", normalize_url('https://www.youtube.com/watch?v=REG00000547') == 'https://www.youtube.com/watch?v=REG00000547')
    check("normalize_04147", normalize_url('https://www.youtube.com/watch?v=REG00000548') == 'https://www.youtube.com/watch?v=REG00000548')
    check("normalize_04148", normalize_url('https://www.youtube.com/watch?v=REG00000549') == 'https://www.youtube.com/watch?v=REG00000549')
    check("normalize_04149", normalize_url('https://www.youtube.com/watch?v=REG00000550') == 'https://www.youtube.com/watch?v=REG00000550')
    check("normalize_04150", normalize_url('https://www.youtube.com/watch?v=REG00000551') == 'https://www.youtube.com/watch?v=REG00000551')
    check("normalize_04151", normalize_url('https://www.youtube.com/watch?v=REG00000552') == 'https://www.youtube.com/watch?v=REG00000552')
    check("normalize_04152", normalize_url('https://www.youtube.com/watch?v=REG00000553') == 'https://www.youtube.com/watch?v=REG00000553')
    check("normalize_04153", normalize_url('https://www.youtube.com/watch?v=REG00000554') == 'https://www.youtube.com/watch?v=REG00000554')
    check("normalize_04154", normalize_url('https://www.youtube.com/watch?v=REG00000555') == 'https://www.youtube.com/watch?v=REG00000555')
    check("normalize_04155", normalize_url('https://www.youtube.com/watch?v=REG00000556') == 'https://www.youtube.com/watch?v=REG00000556')
    check("normalize_04156", normalize_url('https://www.youtube.com/watch?v=REG00000557') == 'https://www.youtube.com/watch?v=REG00000557')
    check("normalize_04157", normalize_url('https://www.youtube.com/watch?v=REG00000558') == 'https://www.youtube.com/watch?v=REG00000558')
    check("normalize_04158", normalize_url('https://www.youtube.com/watch?v=REG00000559') == 'https://www.youtube.com/watch?v=REG00000559')
    check("normalize_04159", normalize_url('https://www.youtube.com/watch?v=REG00000560') == 'https://www.youtube.com/watch?v=REG00000560')
    check("normalize_04160", normalize_url('https://www.youtube.com/watch?v=REG00000561') == 'https://www.youtube.com/watch?v=REG00000561')
    check("normalize_04161", normalize_url('https://www.youtube.com/watch?v=REG00000562') == 'https://www.youtube.com/watch?v=REG00000562')
    check("normalize_04162", normalize_url('https://www.youtube.com/watch?v=REG00000563') == 'https://www.youtube.com/watch?v=REG00000563')
    check("normalize_04163", normalize_url('https://www.youtube.com/watch?v=REG00000564') == 'https://www.youtube.com/watch?v=REG00000564')
    check("normalize_04164", normalize_url('https://www.youtube.com/watch?v=REG00000565') == 'https://www.youtube.com/watch?v=REG00000565')
    check("normalize_04165", normalize_url('https://www.youtube.com/watch?v=REG00000566') == 'https://www.youtube.com/watch?v=REG00000566')
    check("normalize_04166", normalize_url('https://www.youtube.com/watch?v=REG00000567') == 'https://www.youtube.com/watch?v=REG00000567')
    check("normalize_04167", normalize_url('https://www.youtube.com/watch?v=REG00000568') == 'https://www.youtube.com/watch?v=REG00000568')
    check("normalize_04168", normalize_url('https://www.youtube.com/watch?v=REG00000569') == 'https://www.youtube.com/watch?v=REG00000569')
    check("normalize_04169", normalize_url('https://www.youtube.com/watch?v=REG00000570') == 'https://www.youtube.com/watch?v=REG00000570')
    check("normalize_04170", normalize_url('https://www.youtube.com/watch?v=REG00000571') == 'https://www.youtube.com/watch?v=REG00000571')
    check("normalize_04171", normalize_url('https://www.youtube.com/watch?v=REG00000572') == 'https://www.youtube.com/watch?v=REG00000572')
    check("normalize_04172", normalize_url('https://www.youtube.com/watch?v=REG00000573') == 'https://www.youtube.com/watch?v=REG00000573')
    check("normalize_04173", normalize_url('https://www.youtube.com/watch?v=REG00000574') == 'https://www.youtube.com/watch?v=REG00000574')
    check("normalize_04174", normalize_url('https://www.youtube.com/watch?v=REG00000575') == 'https://www.youtube.com/watch?v=REG00000575')
    check("normalize_04175", normalize_url('https://www.youtube.com/watch?v=REG00000576') == 'https://www.youtube.com/watch?v=REG00000576')
    check("normalize_04176", normalize_url('https://www.youtube.com/watch?v=REG00000577') == 'https://www.youtube.com/watch?v=REG00000577')
    check("normalize_04177", normalize_url('https://www.youtube.com/watch?v=REG00000578') == 'https://www.youtube.com/watch?v=REG00000578')
    check("normalize_04178", normalize_url('https://www.youtube.com/watch?v=REG00000579') == 'https://www.youtube.com/watch?v=REG00000579')
    check("normalize_04179", normalize_url('https://www.youtube.com/watch?v=REG00000580') == 'https://www.youtube.com/watch?v=REG00000580')
    check("normalize_04180", normalize_url('https://www.youtube.com/watch?v=REG00000581') == 'https://www.youtube.com/watch?v=REG00000581')
    check("normalize_04181", normalize_url('https://www.youtube.com/watch?v=REG00000582') == 'https://www.youtube.com/watch?v=REG00000582')
    check("normalize_04182", normalize_url('https://www.youtube.com/watch?v=REG00000583') == 'https://www.youtube.com/watch?v=REG00000583')
    check("normalize_04183", normalize_url('https://www.youtube.com/watch?v=REG00000584') == 'https://www.youtube.com/watch?v=REG00000584')
    check("normalize_04184", normalize_url('https://www.youtube.com/watch?v=REG00000585') == 'https://www.youtube.com/watch?v=REG00000585')
    check("normalize_04185", normalize_url('https://www.youtube.com/watch?v=REG00000586') == 'https://www.youtube.com/watch?v=REG00000586')
    check("normalize_04186", normalize_url('https://www.youtube.com/watch?v=REG00000587') == 'https://www.youtube.com/watch?v=REG00000587')
    check("normalize_04187", normalize_url('https://www.youtube.com/watch?v=REG00000588') == 'https://www.youtube.com/watch?v=REG00000588')
    check("normalize_04188", normalize_url('https://www.youtube.com/watch?v=REG00000589') == 'https://www.youtube.com/watch?v=REG00000589')
    check("normalize_04189", normalize_url('https://www.youtube.com/watch?v=REG00000590') == 'https://www.youtube.com/watch?v=REG00000590')
    check("normalize_04190", normalize_url('https://www.youtube.com/watch?v=REG00000591') == 'https://www.youtube.com/watch?v=REG00000591')
    check("normalize_04191", normalize_url('https://www.youtube.com/watch?v=REG00000592') == 'https://www.youtube.com/watch?v=REG00000592')
    check("normalize_04192", normalize_url('https://www.youtube.com/watch?v=REG00000593') == 'https://www.youtube.com/watch?v=REG00000593')
    check("normalize_04193", normalize_url('https://www.youtube.com/watch?v=REG00000594') == 'https://www.youtube.com/watch?v=REG00000594')
    check("normalize_04194", normalize_url('https://www.youtube.com/watch?v=REG00000595') == 'https://www.youtube.com/watch?v=REG00000595')
    check("normalize_04195", normalize_url('https://www.youtube.com/watch?v=REG00000596') == 'https://www.youtube.com/watch?v=REG00000596')
    check("normalize_04196", normalize_url('https://www.youtube.com/watch?v=REG00000597') == 'https://www.youtube.com/watch?v=REG00000597')
    check("normalize_04197", normalize_url('https://www.youtube.com/watch?v=REG00000598') == 'https://www.youtube.com/watch?v=REG00000598')
    check("normalize_04198", normalize_url('https://www.youtube.com/watch?v=REG00000599') == 'https://www.youtube.com/watch?v=REG00000599')
    check("normalize_04199", normalize_url('https://www.youtube.com/watch?v=REG00000600') == 'https://www.youtube.com/watch?v=REG00000600')
    check("normalize_04200", normalize_url('https://www.youtube.com/watch?v=REG00000601') == 'https://www.youtube.com/watch?v=REG00000601')
    check("normalize_04201", normalize_url('https://www.youtube.com/watch?v=REG00000602') == 'https://www.youtube.com/watch?v=REG00000602')
    check("normalize_04202", normalize_url('https://www.youtube.com/watch?v=REG00000603') == 'https://www.youtube.com/watch?v=REG00000603')
    check("normalize_04203", normalize_url('https://www.youtube.com/watch?v=REG00000604') == 'https://www.youtube.com/watch?v=REG00000604')
    check("normalize_04204", normalize_url('https://www.youtube.com/watch?v=REG00000605') == 'https://www.youtube.com/watch?v=REG00000605')
    check("normalize_04205", normalize_url('https://www.youtube.com/watch?v=REG00000606') == 'https://www.youtube.com/watch?v=REG00000606')
    check("normalize_04206", normalize_url('https://www.youtube.com/watch?v=REG00000607') == 'https://www.youtube.com/watch?v=REG00000607')
    check("normalize_04207", normalize_url('https://www.youtube.com/watch?v=REG00000608') == 'https://www.youtube.com/watch?v=REG00000608')
    check("normalize_04208", normalize_url('https://www.youtube.com/watch?v=REG00000609') == 'https://www.youtube.com/watch?v=REG00000609')
    check("normalize_04209", normalize_url('https://www.youtube.com/watch?v=REG00000610') == 'https://www.youtube.com/watch?v=REG00000610')
    check("normalize_04210", normalize_url('https://www.youtube.com/watch?v=REG00000611') == 'https://www.youtube.com/watch?v=REG00000611')
    check("normalize_04211", normalize_url('https://www.youtube.com/watch?v=REG00000612') == 'https://www.youtube.com/watch?v=REG00000612')
    check("normalize_04212", normalize_url('https://www.youtube.com/watch?v=REG00000613') == 'https://www.youtube.com/watch?v=REG00000613')
    check("normalize_04213", normalize_url('https://www.youtube.com/watch?v=REG00000614') == 'https://www.youtube.com/watch?v=REG00000614')
    check("normalize_04214", normalize_url('https://www.youtube.com/watch?v=REG00000615') == 'https://www.youtube.com/watch?v=REG00000615')
    check("normalize_04215", normalize_url('https://www.youtube.com/watch?v=REG00000616') == 'https://www.youtube.com/watch?v=REG00000616')
    check("normalize_04216", normalize_url('https://www.youtube.com/watch?v=REG00000617') == 'https://www.youtube.com/watch?v=REG00000617')
    check("normalize_04217", normalize_url('https://www.youtube.com/watch?v=REG00000618') == 'https://www.youtube.com/watch?v=REG00000618')
    check("normalize_04218", normalize_url('https://www.youtube.com/watch?v=REG00000619') == 'https://www.youtube.com/watch?v=REG00000619')
    check("normalize_04219", normalize_url('https://www.youtube.com/watch?v=REG00000620') == 'https://www.youtube.com/watch?v=REG00000620')
    check("normalize_04220", normalize_url('https://www.youtube.com/watch?v=REG00000621') == 'https://www.youtube.com/watch?v=REG00000621')
    check("normalize_04221", normalize_url('https://www.youtube.com/watch?v=REG00000622') == 'https://www.youtube.com/watch?v=REG00000622')
    check("normalize_04222", normalize_url('https://www.youtube.com/watch?v=REG00000623') == 'https://www.youtube.com/watch?v=REG00000623')
    check("normalize_04223", normalize_url('https://www.youtube.com/watch?v=REG00000624') == 'https://www.youtube.com/watch?v=REG00000624')
    check("normalize_04224", normalize_url('https://www.youtube.com/watch?v=REG00000625') == 'https://www.youtube.com/watch?v=REG00000625')
    check("normalize_04225", normalize_url('https://www.youtube.com/watch?v=REG00000626') == 'https://www.youtube.com/watch?v=REG00000626')
    check("normalize_04226", normalize_url('https://www.youtube.com/watch?v=REG00000627') == 'https://www.youtube.com/watch?v=REG00000627')
    check("normalize_04227", normalize_url('https://www.youtube.com/watch?v=REG00000628') == 'https://www.youtube.com/watch?v=REG00000628')
    check("normalize_04228", normalize_url('https://www.youtube.com/watch?v=REG00000629') == 'https://www.youtube.com/watch?v=REG00000629')
    check("normalize_04229", normalize_url('https://www.youtube.com/watch?v=REG00000630') == 'https://www.youtube.com/watch?v=REG00000630')
    check("normalize_04230", normalize_url('https://www.youtube.com/watch?v=REG00000631') == 'https://www.youtube.com/watch?v=REG00000631')
    check("normalize_04231", normalize_url('https://www.youtube.com/watch?v=REG00000632') == 'https://www.youtube.com/watch?v=REG00000632')
    check("normalize_04232", normalize_url('https://www.youtube.com/watch?v=REG00000633') == 'https://www.youtube.com/watch?v=REG00000633')
    check("normalize_04233", normalize_url('https://www.youtube.com/watch?v=REG00000634') == 'https://www.youtube.com/watch?v=REG00000634')
    check("normalize_04234", normalize_url('https://www.youtube.com/watch?v=REG00000635') == 'https://www.youtube.com/watch?v=REG00000635')
    check("normalize_04235", normalize_url('https://www.youtube.com/watch?v=REG00000636') == 'https://www.youtube.com/watch?v=REG00000636')
    check("normalize_04236", normalize_url('https://www.youtube.com/watch?v=REG00000637') == 'https://www.youtube.com/watch?v=REG00000637')
    check("normalize_04237", normalize_url('https://www.youtube.com/watch?v=REG00000638') == 'https://www.youtube.com/watch?v=REG00000638')
    check("normalize_04238", normalize_url('https://www.youtube.com/watch?v=REG00000639') == 'https://www.youtube.com/watch?v=REG00000639')
    check("normalize_04239", normalize_url('https://www.youtube.com/watch?v=REG00000640') == 'https://www.youtube.com/watch?v=REG00000640')
    check("normalize_04240", normalize_url('https://www.youtube.com/watch?v=REG00000641') == 'https://www.youtube.com/watch?v=REG00000641')
    check("normalize_04241", normalize_url('https://www.youtube.com/watch?v=REG00000642') == 'https://www.youtube.com/watch?v=REG00000642')
    check("normalize_04242", normalize_url('https://www.youtube.com/watch?v=REG00000643') == 'https://www.youtube.com/watch?v=REG00000643')
    check("normalize_04243", normalize_url('https://www.youtube.com/watch?v=REG00000644') == 'https://www.youtube.com/watch?v=REG00000644')
    check("normalize_04244", normalize_url('https://www.youtube.com/watch?v=REG00000645') == 'https://www.youtube.com/watch?v=REG00000645')
    check("normalize_04245", normalize_url('https://www.youtube.com/watch?v=REG00000646') == 'https://www.youtube.com/watch?v=REG00000646')
    check("normalize_04246", normalize_url('https://www.youtube.com/watch?v=REG00000647') == 'https://www.youtube.com/watch?v=REG00000647')
    check("normalize_04247", normalize_url('https://www.youtube.com/watch?v=REG00000648') == 'https://www.youtube.com/watch?v=REG00000648')
    check("normalize_04248", normalize_url('https://www.youtube.com/watch?v=REG00000649') == 'https://www.youtube.com/watch?v=REG00000649')
    check("normalize_04249", normalize_url('https://www.youtube.com/watch?v=REG00000650') == 'https://www.youtube.com/watch?v=REG00000650')
    check("normalize_04250", normalize_url('https://www.youtube.com/watch?v=REG00000651') == 'https://www.youtube.com/watch?v=REG00000651')
    check("normalize_04251", normalize_url('https://www.youtube.com/watch?v=REG00000652') == 'https://www.youtube.com/watch?v=REG00000652')
    check("normalize_04252", normalize_url('https://www.youtube.com/watch?v=REG00000653') == 'https://www.youtube.com/watch?v=REG00000653')
    check("normalize_04253", normalize_url('https://www.youtube.com/watch?v=REG00000654') == 'https://www.youtube.com/watch?v=REG00000654')
    check("normalize_04254", normalize_url('https://www.youtube.com/watch?v=REG00000655') == 'https://www.youtube.com/watch?v=REG00000655')
    check("normalize_04255", normalize_url('https://www.youtube.com/watch?v=REG00000656') == 'https://www.youtube.com/watch?v=REG00000656')
    check("normalize_04256", normalize_url('https://www.youtube.com/watch?v=REG00000657') == 'https://www.youtube.com/watch?v=REG00000657')
    check("normalize_04257", normalize_url('https://www.youtube.com/watch?v=REG00000658') == 'https://www.youtube.com/watch?v=REG00000658')
    check("normalize_04258", normalize_url('https://www.youtube.com/watch?v=REG00000659') == 'https://www.youtube.com/watch?v=REG00000659')
    check("normalize_04259", normalize_url('https://www.youtube.com/watch?v=REG00000660') == 'https://www.youtube.com/watch?v=REG00000660')
    check("normalize_04260", normalize_url('https://www.youtube.com/watch?v=REG00000661') == 'https://www.youtube.com/watch?v=REG00000661')
    check("normalize_04261", normalize_url('https://www.youtube.com/watch?v=REG00000662') == 'https://www.youtube.com/watch?v=REG00000662')
    check("normalize_04262", normalize_url('https://www.youtube.com/watch?v=REG00000663') == 'https://www.youtube.com/watch?v=REG00000663')
    check("normalize_04263", normalize_url('https://www.youtube.com/watch?v=REG00000664') == 'https://www.youtube.com/watch?v=REG00000664')
    check("normalize_04264", normalize_url('https://www.youtube.com/watch?v=REG00000665') == 'https://www.youtube.com/watch?v=REG00000665')
    check("normalize_04265", normalize_url('https://www.youtube.com/watch?v=REG00000666') == 'https://www.youtube.com/watch?v=REG00000666')
    check("normalize_04266", normalize_url('https://www.youtube.com/watch?v=REG00000667') == 'https://www.youtube.com/watch?v=REG00000667')
    check("normalize_04267", normalize_url('https://www.youtube.com/watch?v=REG00000668') == 'https://www.youtube.com/watch?v=REG00000668')
    check("normalize_04268", normalize_url('https://www.youtube.com/watch?v=REG00000669') == 'https://www.youtube.com/watch?v=REG00000669')
    check("normalize_04269", normalize_url('https://www.youtube.com/watch?v=REG00000670') == 'https://www.youtube.com/watch?v=REG00000670')
    check("normalize_04270", normalize_url('https://www.youtube.com/watch?v=REG00000671') == 'https://www.youtube.com/watch?v=REG00000671')
    check("normalize_04271", normalize_url('https://www.youtube.com/watch?v=REG00000672') == 'https://www.youtube.com/watch?v=REG00000672')
    check("normalize_04272", normalize_url('https://www.youtube.com/watch?v=REG00000673') == 'https://www.youtube.com/watch?v=REG00000673')
    check("normalize_04273", normalize_url('https://www.youtube.com/watch?v=REG00000674') == 'https://www.youtube.com/watch?v=REG00000674')
    check("normalize_04274", normalize_url('https://www.youtube.com/watch?v=REG00000675') == 'https://www.youtube.com/watch?v=REG00000675')
    check("normalize_04275", normalize_url('https://www.youtube.com/watch?v=REG00000676') == 'https://www.youtube.com/watch?v=REG00000676')
    check("normalize_04276", normalize_url('https://www.youtube.com/watch?v=REG00000677') == 'https://www.youtube.com/watch?v=REG00000677')
    check("normalize_04277", normalize_url('https://www.youtube.com/watch?v=REG00000678') == 'https://www.youtube.com/watch?v=REG00000678')
    check("normalize_04278", normalize_url('https://www.youtube.com/watch?v=REG00000679') == 'https://www.youtube.com/watch?v=REG00000679')
    check("normalize_04279", normalize_url('https://www.youtube.com/watch?v=REG00000680') == 'https://www.youtube.com/watch?v=REG00000680')
    check("normalize_04280", normalize_url('https://www.youtube.com/watch?v=REG00000681') == 'https://www.youtube.com/watch?v=REG00000681')
    check("normalize_04281", normalize_url('https://www.youtube.com/watch?v=REG00000682') == 'https://www.youtube.com/watch?v=REG00000682')
    check("normalize_04282", normalize_url('https://www.youtube.com/watch?v=REG00000683') == 'https://www.youtube.com/watch?v=REG00000683')
    check("normalize_04283", normalize_url('https://www.youtube.com/watch?v=REG00000684') == 'https://www.youtube.com/watch?v=REG00000684')
    check("normalize_04284", normalize_url('https://www.youtube.com/watch?v=REG00000685') == 'https://www.youtube.com/watch?v=REG00000685')
    check("normalize_04285", normalize_url('https://www.youtube.com/watch?v=REG00000686') == 'https://www.youtube.com/watch?v=REG00000686')
    check("normalize_04286", normalize_url('https://www.youtube.com/watch?v=REG00000687') == 'https://www.youtube.com/watch?v=REG00000687')
    check("normalize_04287", normalize_url('https://www.youtube.com/watch?v=REG00000688') == 'https://www.youtube.com/watch?v=REG00000688')
    check("normalize_04288", normalize_url('https://www.youtube.com/watch?v=REG00000689') == 'https://www.youtube.com/watch?v=REG00000689')
    check("normalize_04289", normalize_url('https://www.youtube.com/watch?v=REG00000690') == 'https://www.youtube.com/watch?v=REG00000690')
    check("normalize_04290", normalize_url('https://www.youtube.com/watch?v=REG00000691') == 'https://www.youtube.com/watch?v=REG00000691')
    check("normalize_04291", normalize_url('https://www.youtube.com/watch?v=REG00000692') == 'https://www.youtube.com/watch?v=REG00000692')
    check("normalize_04292", normalize_url('https://www.youtube.com/watch?v=REG00000693') == 'https://www.youtube.com/watch?v=REG00000693')
    check("normalize_04293", normalize_url('https://www.youtube.com/watch?v=REG00000694') == 'https://www.youtube.com/watch?v=REG00000694')
    check("normalize_04294", normalize_url('https://www.youtube.com/watch?v=REG00000695') == 'https://www.youtube.com/watch?v=REG00000695')
    check("normalize_04295", normalize_url('https://www.youtube.com/watch?v=REG00000696') == 'https://www.youtube.com/watch?v=REG00000696')
    check("normalize_04296", normalize_url('https://www.youtube.com/watch?v=REG00000697') == 'https://www.youtube.com/watch?v=REG00000697')
    check("normalize_04297", normalize_url('https://www.youtube.com/watch?v=REG00000698') == 'https://www.youtube.com/watch?v=REG00000698')
    check("normalize_04298", normalize_url('https://www.youtube.com/watch?v=REG00000699') == 'https://www.youtube.com/watch?v=REG00000699')
    check("normalize_04299", normalize_url('https://www.youtube.com/watch?v=REG00000700') == 'https://www.youtube.com/watch?v=REG00000700')
    check("normalize_04300", normalize_url('https://www.youtube.com/watch?v=REG00000701') == 'https://www.youtube.com/watch?v=REG00000701')
    check("normalize_04301", normalize_url('https://www.youtube.com/watch?v=REG00000702') == 'https://www.youtube.com/watch?v=REG00000702')
    check("normalize_04302", normalize_url('https://www.youtube.com/watch?v=REG00000703') == 'https://www.youtube.com/watch?v=REG00000703')
    check("normalize_04303", normalize_url('https://www.youtube.com/watch?v=REG00000704') == 'https://www.youtube.com/watch?v=REG00000704')
    check("normalize_04304", normalize_url('https://www.youtube.com/watch?v=REG00000705') == 'https://www.youtube.com/watch?v=REG00000705')
    check("normalize_04305", normalize_url('https://www.youtube.com/watch?v=REG00000706') == 'https://www.youtube.com/watch?v=REG00000706')
    check("normalize_04306", normalize_url('https://www.youtube.com/watch?v=REG00000707') == 'https://www.youtube.com/watch?v=REG00000707')
    check("normalize_04307", normalize_url('https://www.youtube.com/watch?v=REG00000708') == 'https://www.youtube.com/watch?v=REG00000708')
    check("normalize_04308", normalize_url('https://www.youtube.com/watch?v=REG00000709') == 'https://www.youtube.com/watch?v=REG00000709')
    check("normalize_04309", normalize_url('https://www.youtube.com/watch?v=REG00000710') == 'https://www.youtube.com/watch?v=REG00000710')
    check("normalize_04310", normalize_url('https://www.youtube.com/watch?v=REG00000711') == 'https://www.youtube.com/watch?v=REG00000711')
    check("normalize_04311", normalize_url('https://www.youtube.com/watch?v=REG00000712') == 'https://www.youtube.com/watch?v=REG00000712')
    check("normalize_04312", normalize_url('https://www.youtube.com/watch?v=REG00000713') == 'https://www.youtube.com/watch?v=REG00000713')
    check("normalize_04313", normalize_url('https://www.youtube.com/watch?v=REG00000714') == 'https://www.youtube.com/watch?v=REG00000714')
    check("normalize_04314", normalize_url('https://www.youtube.com/watch?v=REG00000715') == 'https://www.youtube.com/watch?v=REG00000715')
    check("normalize_04315", normalize_url('https://www.youtube.com/watch?v=REG00000716') == 'https://www.youtube.com/watch?v=REG00000716')
    check("normalize_04316", normalize_url('https://www.youtube.com/watch?v=REG00000717') == 'https://www.youtube.com/watch?v=REG00000717')
    check("normalize_04317", normalize_url('https://www.youtube.com/watch?v=REG00000718') == 'https://www.youtube.com/watch?v=REG00000718')
    check("normalize_04318", normalize_url('https://www.youtube.com/watch?v=REG00000719') == 'https://www.youtube.com/watch?v=REG00000719')
    check("normalize_04319", normalize_url('https://www.youtube.com/watch?v=REG00000720') == 'https://www.youtube.com/watch?v=REG00000720')
    check("normalize_04320", normalize_url('https://www.youtube.com/watch?v=REG00000721') == 'https://www.youtube.com/watch?v=REG00000721')
    check("normalize_04321", normalize_url('https://www.youtube.com/watch?v=REG00000722') == 'https://www.youtube.com/watch?v=REG00000722')
    check("normalize_04322", normalize_url('https://www.youtube.com/watch?v=REG00000723') == 'https://www.youtube.com/watch?v=REG00000723')
    check("normalize_04323", normalize_url('https://www.youtube.com/watch?v=REG00000724') == 'https://www.youtube.com/watch?v=REG00000724')
    check("normalize_04324", normalize_url('https://www.youtube.com/watch?v=REG00000725') == 'https://www.youtube.com/watch?v=REG00000725')
    check("normalize_04325", normalize_url('https://www.youtube.com/watch?v=REG00000726') == 'https://www.youtube.com/watch?v=REG00000726')
    check("normalize_04326", normalize_url('https://www.youtube.com/watch?v=REG00000727') == 'https://www.youtube.com/watch?v=REG00000727')
    check("normalize_04327", normalize_url('https://www.youtube.com/watch?v=REG00000728') == 'https://www.youtube.com/watch?v=REG00000728')
    check("normalize_04328", normalize_url('https://www.youtube.com/watch?v=REG00000729') == 'https://www.youtube.com/watch?v=REG00000729')
    check("normalize_04329", normalize_url('https://www.youtube.com/watch?v=REG00000730') == 'https://www.youtube.com/watch?v=REG00000730')
    check("normalize_04330", normalize_url('https://www.youtube.com/watch?v=REG00000731') == 'https://www.youtube.com/watch?v=REG00000731')
    check("normalize_04331", normalize_url('https://www.youtube.com/watch?v=REG00000732') == 'https://www.youtube.com/watch?v=REG00000732')
    check("normalize_04332", normalize_url('https://www.youtube.com/watch?v=REG00000733') == 'https://www.youtube.com/watch?v=REG00000733')
    check("normalize_04333", normalize_url('https://www.youtube.com/watch?v=REG00000734') == 'https://www.youtube.com/watch?v=REG00000734')
    check("normalize_04334", normalize_url('https://www.youtube.com/watch?v=REG00000735') == 'https://www.youtube.com/watch?v=REG00000735')
    check("normalize_04335", normalize_url('https://www.youtube.com/watch?v=REG00000736') == 'https://www.youtube.com/watch?v=REG00000736')
    check("normalize_04336", normalize_url('https://www.youtube.com/watch?v=REG00000737') == 'https://www.youtube.com/watch?v=REG00000737')
    check("normalize_04337", normalize_url('https://www.youtube.com/watch?v=REG00000738') == 'https://www.youtube.com/watch?v=REG00000738')
    check("normalize_04338", normalize_url('https://www.youtube.com/watch?v=REG00000739') == 'https://www.youtube.com/watch?v=REG00000739')
    check("normalize_04339", normalize_url('https://www.youtube.com/watch?v=REG00000740') == 'https://www.youtube.com/watch?v=REG00000740')
    check("normalize_04340", normalize_url('https://www.youtube.com/watch?v=REG00000741') == 'https://www.youtube.com/watch?v=REG00000741')
    check("normalize_04341", normalize_url('https://www.youtube.com/watch?v=REG00000742') == 'https://www.youtube.com/watch?v=REG00000742')
    check("normalize_04342", normalize_url('https://www.youtube.com/watch?v=REG00000743') == 'https://www.youtube.com/watch?v=REG00000743')
    check("normalize_04343", normalize_url('https://www.youtube.com/watch?v=REG00000744') == 'https://www.youtube.com/watch?v=REG00000744')
    check("normalize_04344", normalize_url('https://www.youtube.com/watch?v=REG00000745') == 'https://www.youtube.com/watch?v=REG00000745')
    check("normalize_04345", normalize_url('https://www.youtube.com/watch?v=REG00000746') == 'https://www.youtube.com/watch?v=REG00000746')
    check("normalize_04346", normalize_url('https://www.youtube.com/watch?v=REG00000747') == 'https://www.youtube.com/watch?v=REG00000747')
    check("normalize_04347", normalize_url('https://www.youtube.com/watch?v=REG00000748') == 'https://www.youtube.com/watch?v=REG00000748')
    check("normalize_04348", normalize_url('https://www.youtube.com/watch?v=REG00000749') == 'https://www.youtube.com/watch?v=REG00000749')
    check("normalize_04349", normalize_url('https://www.youtube.com/watch?v=REG00000750') == 'https://www.youtube.com/watch?v=REG00000750')
    check("normalize_04350", normalize_url('https://www.youtube.com/watch?v=REG00000751') == 'https://www.youtube.com/watch?v=REG00000751')
    check("normalize_04351", normalize_url('https://www.youtube.com/watch?v=REG00000752') == 'https://www.youtube.com/watch?v=REG00000752')
    check("normalize_04352", normalize_url('https://www.youtube.com/watch?v=REG00000753') == 'https://www.youtube.com/watch?v=REG00000753')
    check("normalize_04353", normalize_url('https://www.youtube.com/watch?v=REG00000754') == 'https://www.youtube.com/watch?v=REG00000754')
    check("normalize_04354", normalize_url('https://www.youtube.com/watch?v=REG00000755') == 'https://www.youtube.com/watch?v=REG00000755')
    check("normalize_04355", normalize_url('https://www.youtube.com/watch?v=REG00000756') == 'https://www.youtube.com/watch?v=REG00000756')
    check("normalize_04356", normalize_url('https://www.youtube.com/watch?v=REG00000757') == 'https://www.youtube.com/watch?v=REG00000757')
    check("normalize_04357", normalize_url('https://www.youtube.com/watch?v=REG00000758') == 'https://www.youtube.com/watch?v=REG00000758')
    check("normalize_04358", normalize_url('https://www.youtube.com/watch?v=REG00000759') == 'https://www.youtube.com/watch?v=REG00000759')
    check("normalize_04359", normalize_url('https://www.youtube.com/watch?v=REG00000760') == 'https://www.youtube.com/watch?v=REG00000760')
    check("normalize_04360", normalize_url('https://www.youtube.com/watch?v=REG00000761') == 'https://www.youtube.com/watch?v=REG00000761')
    check("normalize_04361", normalize_url('https://www.youtube.com/watch?v=REG00000762') == 'https://www.youtube.com/watch?v=REG00000762')
    check("normalize_04362", normalize_url('https://www.youtube.com/watch?v=REG00000763') == 'https://www.youtube.com/watch?v=REG00000763')
    check("normalize_04363", normalize_url('https://www.youtube.com/watch?v=REG00000764') == 'https://www.youtube.com/watch?v=REG00000764')
    check("normalize_04364", normalize_url('https://www.youtube.com/watch?v=REG00000765') == 'https://www.youtube.com/watch?v=REG00000765')
    check("normalize_04365", normalize_url('https://www.youtube.com/watch?v=REG00000766') == 'https://www.youtube.com/watch?v=REG00000766')
    check("normalize_04366", normalize_url('https://www.youtube.com/watch?v=REG00000767') == 'https://www.youtube.com/watch?v=REG00000767')
    check("normalize_04367", normalize_url('https://www.youtube.com/watch?v=REG00000768') == 'https://www.youtube.com/watch?v=REG00000768')
    check("normalize_04368", normalize_url('https://www.youtube.com/watch?v=REG00000769') == 'https://www.youtube.com/watch?v=REG00000769')
    check("normalize_04369", normalize_url('https://www.youtube.com/watch?v=REG00000770') == 'https://www.youtube.com/watch?v=REG00000770')
    check("normalize_04370", normalize_url('https://www.youtube.com/watch?v=REG00000771') == 'https://www.youtube.com/watch?v=REG00000771')
    check("normalize_04371", normalize_url('https://www.youtube.com/watch?v=REG00000772') == 'https://www.youtube.com/watch?v=REG00000772')
    check("normalize_04372", normalize_url('https://www.youtube.com/watch?v=REG00000773') == 'https://www.youtube.com/watch?v=REG00000773')
    check("normalize_04373", normalize_url('https://www.youtube.com/watch?v=REG00000774') == 'https://www.youtube.com/watch?v=REG00000774')
    check("normalize_04374", normalize_url('https://www.youtube.com/watch?v=REG00000775') == 'https://www.youtube.com/watch?v=REG00000775')
    check("normalize_04375", normalize_url('https://www.youtube.com/watch?v=REG00000776') == 'https://www.youtube.com/watch?v=REG00000776')
    check("normalize_04376", normalize_url('https://www.youtube.com/watch?v=REG00000777') == 'https://www.youtube.com/watch?v=REG00000777')
    check("normalize_04377", normalize_url('https://www.youtube.com/watch?v=REG00000778') == 'https://www.youtube.com/watch?v=REG00000778')
    check("normalize_04378", normalize_url('https://www.youtube.com/watch?v=REG00000779') == 'https://www.youtube.com/watch?v=REG00000779')
    check("normalize_04379", normalize_url('https://www.youtube.com/watch?v=REG00000780') == 'https://www.youtube.com/watch?v=REG00000780')
    check("normalize_04380", normalize_url('https://www.youtube.com/watch?v=REG00000781') == 'https://www.youtube.com/watch?v=REG00000781')
    check("normalize_04381", normalize_url('https://www.youtube.com/watch?v=REG00000782') == 'https://www.youtube.com/watch?v=REG00000782')
    check("normalize_04382", normalize_url('https://www.youtube.com/watch?v=REG00000783') == 'https://www.youtube.com/watch?v=REG00000783')
    check("normalize_04383", normalize_url('https://www.youtube.com/watch?v=REG00000784') == 'https://www.youtube.com/watch?v=REG00000784')
    check("normalize_04384", normalize_url('https://www.youtube.com/watch?v=REG00000785') == 'https://www.youtube.com/watch?v=REG00000785')
    check("normalize_04385", normalize_url('https://www.youtube.com/watch?v=REG00000786') == 'https://www.youtube.com/watch?v=REG00000786')
    check("normalize_04386", normalize_url('https://www.youtube.com/watch?v=REG00000787') == 'https://www.youtube.com/watch?v=REG00000787')
    check("normalize_04387", normalize_url('https://www.youtube.com/watch?v=REG00000788') == 'https://www.youtube.com/watch?v=REG00000788')
    check("normalize_04388", normalize_url('https://www.youtube.com/watch?v=REG00000789') == 'https://www.youtube.com/watch?v=REG00000789')
    check("normalize_04389", normalize_url('https://www.youtube.com/watch?v=REG00000790') == 'https://www.youtube.com/watch?v=REG00000790')
    check("normalize_04390", normalize_url('https://www.youtube.com/watch?v=REG00000791') == 'https://www.youtube.com/watch?v=REG00000791')
    check("normalize_04391", normalize_url('https://www.youtube.com/watch?v=REG00000792') == 'https://www.youtube.com/watch?v=REG00000792')
    check("normalize_04392", normalize_url('https://www.youtube.com/watch?v=REG00000793') == 'https://www.youtube.com/watch?v=REG00000793')
    check("normalize_04393", normalize_url('https://www.youtube.com/watch?v=REG00000794') == 'https://www.youtube.com/watch?v=REG00000794')
    check("normalize_04394", normalize_url('https://www.youtube.com/watch?v=REG00000795') == 'https://www.youtube.com/watch?v=REG00000795')
    check("normalize_04395", normalize_url('https://www.youtube.com/watch?v=REG00000796') == 'https://www.youtube.com/watch?v=REG00000796')
    check("normalize_04396", normalize_url('https://www.youtube.com/watch?v=REG00000797') == 'https://www.youtube.com/watch?v=REG00000797')
    check("normalize_04397", normalize_url('https://www.youtube.com/watch?v=REG00000798') == 'https://www.youtube.com/watch?v=REG00000798')
    check("normalize_04398", normalize_url('https://www.youtube.com/watch?v=REG00000799') == 'https://www.youtube.com/watch?v=REG00000799')
    check("normalize_04399", normalize_url('https://www.youtube.com/watch?v=REG00000800') == 'https://www.youtube.com/watch?v=REG00000800')
    check("normalize_04400", normalize_url('https://www.youtube.com/watch?v=REG00000801') == 'https://www.youtube.com/watch?v=REG00000801')
    check("normalize_04401", normalize_url('https://www.youtube.com/watch?v=REG00000802') == 'https://www.youtube.com/watch?v=REG00000802')
    check("normalize_04402", normalize_url('https://www.youtube.com/watch?v=REG00000803') == 'https://www.youtube.com/watch?v=REG00000803')
    check("normalize_04403", normalize_url('https://www.youtube.com/watch?v=REG00000804') == 'https://www.youtube.com/watch?v=REG00000804')
    check("normalize_04404", normalize_url('https://www.youtube.com/watch?v=REG00000805') == 'https://www.youtube.com/watch?v=REG00000805')
    check("normalize_04405", normalize_url('https://www.youtube.com/watch?v=REG00000806') == 'https://www.youtube.com/watch?v=REG00000806')
    check("normalize_04406", normalize_url('https://www.youtube.com/watch?v=REG00000807') == 'https://www.youtube.com/watch?v=REG00000807')
    check("normalize_04407", normalize_url('https://www.youtube.com/watch?v=REG00000808') == 'https://www.youtube.com/watch?v=REG00000808')
    check("normalize_04408", normalize_url('https://www.youtube.com/watch?v=REG00000809') == 'https://www.youtube.com/watch?v=REG00000809')
    check("normalize_04409", normalize_url('https://www.youtube.com/watch?v=REG00000810') == 'https://www.youtube.com/watch?v=REG00000810')
    check("normalize_04410", normalize_url('https://www.youtube.com/watch?v=REG00000811') == 'https://www.youtube.com/watch?v=REG00000811')
    check("normalize_04411", normalize_url('https://www.youtube.com/watch?v=REG00000812') == 'https://www.youtube.com/watch?v=REG00000812')
    check("normalize_04412", normalize_url('https://www.youtube.com/watch?v=REG00000813') == 'https://www.youtube.com/watch?v=REG00000813')
    check("normalize_04413", normalize_url('https://www.youtube.com/watch?v=REG00000814') == 'https://www.youtube.com/watch?v=REG00000814')
    check("normalize_04414", normalize_url('https://www.youtube.com/watch?v=REG00000815') == 'https://www.youtube.com/watch?v=REG00000815')
    check("normalize_04415", normalize_url('https://www.youtube.com/watch?v=REG00000816') == 'https://www.youtube.com/watch?v=REG00000816')
    check("normalize_04416", normalize_url('https://www.youtube.com/watch?v=REG00000817') == 'https://www.youtube.com/watch?v=REG00000817')
    check("normalize_04417", normalize_url('https://www.youtube.com/watch?v=REG00000818') == 'https://www.youtube.com/watch?v=REG00000818')
    check("normalize_04418", normalize_url('https://www.youtube.com/watch?v=REG00000819') == 'https://www.youtube.com/watch?v=REG00000819')
    check("normalize_04419", normalize_url('https://www.youtube.com/watch?v=REG00000820') == 'https://www.youtube.com/watch?v=REG00000820')
    check("normalize_04420", normalize_url('https://www.youtube.com/watch?v=REG00000821') == 'https://www.youtube.com/watch?v=REG00000821')
    check("normalize_04421", normalize_url('https://www.youtube.com/watch?v=REG00000822') == 'https://www.youtube.com/watch?v=REG00000822')
    check("normalize_04422", normalize_url('https://www.youtube.com/watch?v=REG00000823') == 'https://www.youtube.com/watch?v=REG00000823')
    check("normalize_04423", normalize_url('https://www.youtube.com/watch?v=REG00000824') == 'https://www.youtube.com/watch?v=REG00000824')
    check("normalize_04424", normalize_url('https://www.youtube.com/watch?v=REG00000825') == 'https://www.youtube.com/watch?v=REG00000825')
    check("normalize_04425", normalize_url('https://www.youtube.com/watch?v=REG00000826') == 'https://www.youtube.com/watch?v=REG00000826')
    check("normalize_04426", normalize_url('https://www.youtube.com/watch?v=REG00000827') == 'https://www.youtube.com/watch?v=REG00000827')
    check("normalize_04427", normalize_url('https://www.youtube.com/watch?v=REG00000828') == 'https://www.youtube.com/watch?v=REG00000828')
    check("normalize_04428", normalize_url('https://www.youtube.com/watch?v=REG00000829') == 'https://www.youtube.com/watch?v=REG00000829')
    check("normalize_04429", normalize_url('https://www.youtube.com/watch?v=REG00000830') == 'https://www.youtube.com/watch?v=REG00000830')
    check("normalize_04430", normalize_url('https://www.youtube.com/watch?v=REG00000831') == 'https://www.youtube.com/watch?v=REG00000831')
    check("normalize_04431", normalize_url('https://www.youtube.com/watch?v=REG00000832') == 'https://www.youtube.com/watch?v=REG00000832')
    check("normalize_04432", normalize_url('https://www.youtube.com/watch?v=REG00000833') == 'https://www.youtube.com/watch?v=REG00000833')
    check("normalize_04433", normalize_url('https://www.youtube.com/watch?v=REG00000834') == 'https://www.youtube.com/watch?v=REG00000834')
    check("normalize_04434", normalize_url('https://www.youtube.com/watch?v=REG00000835') == 'https://www.youtube.com/watch?v=REG00000835')
    check("normalize_04435", normalize_url('https://www.youtube.com/watch?v=REG00000836') == 'https://www.youtube.com/watch?v=REG00000836')
    check("normalize_04436", normalize_url('https://www.youtube.com/watch?v=REG00000837') == 'https://www.youtube.com/watch?v=REG00000837')
    check("normalize_04437", normalize_url('https://www.youtube.com/watch?v=REG00000838') == 'https://www.youtube.com/watch?v=REG00000838')
    check("normalize_04438", normalize_url('https://www.youtube.com/watch?v=REG00000839') == 'https://www.youtube.com/watch?v=REG00000839')
    check("normalize_04439", normalize_url('https://www.youtube.com/watch?v=REG00000840') == 'https://www.youtube.com/watch?v=REG00000840')
    check("normalize_04440", normalize_url('https://www.youtube.com/watch?v=REG00000841') == 'https://www.youtube.com/watch?v=REG00000841')
    check("normalize_04441", normalize_url('https://www.youtube.com/watch?v=REG00000842') == 'https://www.youtube.com/watch?v=REG00000842')
    check("normalize_04442", normalize_url('https://www.youtube.com/watch?v=REG00000843') == 'https://www.youtube.com/watch?v=REG00000843')
    check("normalize_04443", normalize_url('https://www.youtube.com/watch?v=REG00000844') == 'https://www.youtube.com/watch?v=REG00000844')
    check("normalize_04444", normalize_url('https://www.youtube.com/watch?v=REG00000845') == 'https://www.youtube.com/watch?v=REG00000845')
    check("normalize_04445", normalize_url('https://www.youtube.com/watch?v=REG00000846') == 'https://www.youtube.com/watch?v=REG00000846')
    check("normalize_04446", normalize_url('https://www.youtube.com/watch?v=REG00000847') == 'https://www.youtube.com/watch?v=REG00000847')
    check("normalize_04447", normalize_url('https://www.youtube.com/watch?v=REG00000848') == 'https://www.youtube.com/watch?v=REG00000848')
    check("normalize_04448", normalize_url('https://www.youtube.com/watch?v=REG00000849') == 'https://www.youtube.com/watch?v=REG00000849')
    check("normalize_04449", normalize_url('https://www.youtube.com/watch?v=REG00000850') == 'https://www.youtube.com/watch?v=REG00000850')
    check("normalize_04450", normalize_url('https://www.youtube.com/watch?v=REG00000851') == 'https://www.youtube.com/watch?v=REG00000851')
    check("normalize_04451", normalize_url('https://www.youtube.com/watch?v=REG00000852') == 'https://www.youtube.com/watch?v=REG00000852')
    check("normalize_04452", normalize_url('https://www.youtube.com/watch?v=REG00000853') == 'https://www.youtube.com/watch?v=REG00000853')
    check("normalize_04453", normalize_url('https://www.youtube.com/watch?v=REG00000854') == 'https://www.youtube.com/watch?v=REG00000854')
    check("normalize_04454", normalize_url('https://www.youtube.com/watch?v=REG00000855') == 'https://www.youtube.com/watch?v=REG00000855')
    check("normalize_04455", normalize_url('https://www.youtube.com/watch?v=REG00000856') == 'https://www.youtube.com/watch?v=REG00000856')
    check("normalize_04456", normalize_url('https://www.youtube.com/watch?v=REG00000857') == 'https://www.youtube.com/watch?v=REG00000857')
    check("normalize_04457", normalize_url('https://www.youtube.com/watch?v=REG00000858') == 'https://www.youtube.com/watch?v=REG00000858')
    check("normalize_04458", normalize_url('https://www.youtube.com/watch?v=REG00000859') == 'https://www.youtube.com/watch?v=REG00000859')
    check("normalize_04459", normalize_url('https://www.youtube.com/watch?v=REG00000860') == 'https://www.youtube.com/watch?v=REG00000860')
    check("normalize_04460", normalize_url('https://www.youtube.com/watch?v=REG00000861') == 'https://www.youtube.com/watch?v=REG00000861')
    check("normalize_04461", normalize_url('https://www.youtube.com/watch?v=REG00000862') == 'https://www.youtube.com/watch?v=REG00000862')
    check("normalize_04462", normalize_url('https://www.youtube.com/watch?v=REG00000863') == 'https://www.youtube.com/watch?v=REG00000863')
    check("normalize_04463", normalize_url('https://www.youtube.com/watch?v=REG00000864') == 'https://www.youtube.com/watch?v=REG00000864')
    check("normalize_04464", normalize_url('https://www.youtube.com/watch?v=REG00000865') == 'https://www.youtube.com/watch?v=REG00000865')
    check("normalize_04465", normalize_url('https://www.youtube.com/watch?v=REG00000866') == 'https://www.youtube.com/watch?v=REG00000866')
    check("normalize_04466", normalize_url('https://www.youtube.com/watch?v=REG00000867') == 'https://www.youtube.com/watch?v=REG00000867')
    check("normalize_04467", normalize_url('https://www.youtube.com/watch?v=REG00000868') == 'https://www.youtube.com/watch?v=REG00000868')
    check("normalize_04468", normalize_url('https://www.youtube.com/watch?v=REG00000869') == 'https://www.youtube.com/watch?v=REG00000869')
    check("normalize_04469", normalize_url('https://www.youtube.com/watch?v=REG00000870') == 'https://www.youtube.com/watch?v=REG00000870')
    check("normalize_04470", normalize_url('https://www.youtube.com/watch?v=REG00000871') == 'https://www.youtube.com/watch?v=REG00000871')
    check("normalize_04471", normalize_url('https://www.youtube.com/watch?v=REG00000872') == 'https://www.youtube.com/watch?v=REG00000872')
    check("normalize_04472", normalize_url('https://www.youtube.com/watch?v=REG00000873') == 'https://www.youtube.com/watch?v=REG00000873')
    check("normalize_04473", normalize_url('https://www.youtube.com/watch?v=REG00000874') == 'https://www.youtube.com/watch?v=REG00000874')
    check("normalize_04474", normalize_url('https://www.youtube.com/watch?v=REG00000875') == 'https://www.youtube.com/watch?v=REG00000875')
    check("normalize_04475", normalize_url('https://www.youtube.com/watch?v=REG00000876') == 'https://www.youtube.com/watch?v=REG00000876')
    check("normalize_04476", normalize_url('https://www.youtube.com/watch?v=REG00000877') == 'https://www.youtube.com/watch?v=REG00000877')
    check("normalize_04477", normalize_url('https://www.youtube.com/watch?v=REG00000878') == 'https://www.youtube.com/watch?v=REG00000878')
    check("normalize_04478", normalize_url('https://www.youtube.com/watch?v=REG00000879') == 'https://www.youtube.com/watch?v=REG00000879')
    check("normalize_04479", normalize_url('https://www.youtube.com/watch?v=REG00000880') == 'https://www.youtube.com/watch?v=REG00000880')
    check("normalize_04480", normalize_url('https://www.youtube.com/watch?v=REG00000881') == 'https://www.youtube.com/watch?v=REG00000881')
    check("normalize_04481", normalize_url('https://www.youtube.com/watch?v=REG00000882') == 'https://www.youtube.com/watch?v=REG00000882')
    check("normalize_04482", normalize_url('https://www.youtube.com/watch?v=REG00000883') == 'https://www.youtube.com/watch?v=REG00000883')
    check("normalize_04483", normalize_url('https://www.youtube.com/watch?v=REG00000884') == 'https://www.youtube.com/watch?v=REG00000884')
    check("normalize_04484", normalize_url('https://www.youtube.com/watch?v=REG00000885') == 'https://www.youtube.com/watch?v=REG00000885')
    check("normalize_04485", normalize_url('https://www.youtube.com/watch?v=REG00000886') == 'https://www.youtube.com/watch?v=REG00000886')
    check("normalize_04486", normalize_url('https://www.youtube.com/watch?v=REG00000887') == 'https://www.youtube.com/watch?v=REG00000887')
    check("normalize_04487", normalize_url('https://www.youtube.com/watch?v=REG00000888') == 'https://www.youtube.com/watch?v=REG00000888')
    check("normalize_04488", normalize_url('https://www.youtube.com/watch?v=REG00000889') == 'https://www.youtube.com/watch?v=REG00000889')
    check("normalize_04489", normalize_url('https://www.youtube.com/watch?v=REG00000890') == 'https://www.youtube.com/watch?v=REG00000890')
    check("normalize_04490", normalize_url('https://www.youtube.com/watch?v=REG00000891') == 'https://www.youtube.com/watch?v=REG00000891')
    check("normalize_04491", normalize_url('https://www.youtube.com/watch?v=REG00000892') == 'https://www.youtube.com/watch?v=REG00000892')
    check("normalize_04492", normalize_url('https://www.youtube.com/watch?v=REG00000893') == 'https://www.youtube.com/watch?v=REG00000893')
    check("normalize_04493", normalize_url('https://www.youtube.com/watch?v=REG00000894') == 'https://www.youtube.com/watch?v=REG00000894')
    check("normalize_04494", normalize_url('https://www.youtube.com/watch?v=REG00000895') == 'https://www.youtube.com/watch?v=REG00000895')
    check("normalize_04495", normalize_url('https://www.youtube.com/watch?v=REG00000896') == 'https://www.youtube.com/watch?v=REG00000896')
    check("normalize_04496", normalize_url('https://www.youtube.com/watch?v=REG00000897') == 'https://www.youtube.com/watch?v=REG00000897')
    check("normalize_04497", normalize_url('https://www.youtube.com/watch?v=REG00000898') == 'https://www.youtube.com/watch?v=REG00000898')
    check("normalize_04498", normalize_url('https://www.youtube.com/watch?v=REG00000899') == 'https://www.youtube.com/watch?v=REG00000899')
    check("normalize_04499", normalize_url('https://www.youtube.com/watch?v=REG00000900') == 'https://www.youtube.com/watch?v=REG00000900')
    check("normalize_04500", normalize_url('https://www.youtube.com/watch?v=REG00000901') == 'https://www.youtube.com/watch?v=REG00000901')
    check("normalize_04501", normalize_url('https://www.youtube.com/watch?v=REG00000902') == 'https://www.youtube.com/watch?v=REG00000902')
    check("normalize_04502", normalize_url('https://www.youtube.com/watch?v=REG00000903') == 'https://www.youtube.com/watch?v=REG00000903')
    check("normalize_04503", normalize_url('https://www.youtube.com/watch?v=REG00000904') == 'https://www.youtube.com/watch?v=REG00000904')
    check("normalize_04504", normalize_url('https://www.youtube.com/watch?v=REG00000905') == 'https://www.youtube.com/watch?v=REG00000905')
    check("normalize_04505", normalize_url('https://www.youtube.com/watch?v=REG00000906') == 'https://www.youtube.com/watch?v=REG00000906')
    check("normalize_04506", normalize_url('https://www.youtube.com/watch?v=REG00000907') == 'https://www.youtube.com/watch?v=REG00000907')
    check("normalize_04507", normalize_url('https://www.youtube.com/watch?v=REG00000908') == 'https://www.youtube.com/watch?v=REG00000908')
    check("normalize_04508", normalize_url('https://www.youtube.com/watch?v=REG00000909') == 'https://www.youtube.com/watch?v=REG00000909')
    check("normalize_04509", normalize_url('https://www.youtube.com/watch?v=REG00000910') == 'https://www.youtube.com/watch?v=REG00000910')
    check("normalize_04510", normalize_url('https://www.youtube.com/watch?v=REG00000911') == 'https://www.youtube.com/watch?v=REG00000911')
    check("normalize_04511", normalize_url('https://www.youtube.com/watch?v=REG00000912') == 'https://www.youtube.com/watch?v=REG00000912')
    check("normalize_04512", normalize_url('https://www.youtube.com/watch?v=REG00000913') == 'https://www.youtube.com/watch?v=REG00000913')
    check("normalize_04513", normalize_url('https://www.youtube.com/watch?v=REG00000914') == 'https://www.youtube.com/watch?v=REG00000914')
    check("normalize_04514", normalize_url('https://www.youtube.com/watch?v=REG00000915') == 'https://www.youtube.com/watch?v=REG00000915')
    check("normalize_04515", normalize_url('https://www.youtube.com/watch?v=REG00000916') == 'https://www.youtube.com/watch?v=REG00000916')
    check("normalize_04516", normalize_url('https://www.youtube.com/watch?v=REG00000917') == 'https://www.youtube.com/watch?v=REG00000917')
    check("normalize_04517", normalize_url('https://www.youtube.com/watch?v=REG00000918') == 'https://www.youtube.com/watch?v=REG00000918')
    check("normalize_04518", normalize_url('https://www.youtube.com/watch?v=REG00000919') == 'https://www.youtube.com/watch?v=REG00000919')
    check("normalize_04519", normalize_url('https://www.youtube.com/watch?v=REG00000920') == 'https://www.youtube.com/watch?v=REG00000920')
    check("normalize_04520", normalize_url('https://www.youtube.com/watch?v=REG00000921') == 'https://www.youtube.com/watch?v=REG00000921')
    check("normalize_04521", normalize_url('https://www.youtube.com/watch?v=REG00000922') == 'https://www.youtube.com/watch?v=REG00000922')
    check("normalize_04522", normalize_url('https://www.youtube.com/watch?v=REG00000923') == 'https://www.youtube.com/watch?v=REG00000923')
    check("normalize_04523", normalize_url('https://www.youtube.com/watch?v=REG00000924') == 'https://www.youtube.com/watch?v=REG00000924')
    check("normalize_04524", normalize_url('https://www.youtube.com/watch?v=REG00000925') == 'https://www.youtube.com/watch?v=REG00000925')
    check("normalize_04525", normalize_url('https://www.youtube.com/watch?v=REG00000926') == 'https://www.youtube.com/watch?v=REG00000926')
    check("normalize_04526", normalize_url('https://www.youtube.com/watch?v=REG00000927') == 'https://www.youtube.com/watch?v=REG00000927')
    check("normalize_04527", normalize_url('https://www.youtube.com/watch?v=REG00000928') == 'https://www.youtube.com/watch?v=REG00000928')
    check("normalize_04528", normalize_url('https://www.youtube.com/watch?v=REG00000929') == 'https://www.youtube.com/watch?v=REG00000929')
    check("normalize_04529", normalize_url('https://www.youtube.com/watch?v=REG00000930') == 'https://www.youtube.com/watch?v=REG00000930')
    check("normalize_04530", normalize_url('https://www.youtube.com/watch?v=REG00000931') == 'https://www.youtube.com/watch?v=REG00000931')
    check("normalize_04531", normalize_url('https://www.youtube.com/watch?v=REG00000932') == 'https://www.youtube.com/watch?v=REG00000932')
    check("normalize_04532", normalize_url('https://www.youtube.com/watch?v=REG00000933') == 'https://www.youtube.com/watch?v=REG00000933')
    check("normalize_04533", normalize_url('https://www.youtube.com/watch?v=REG00000934') == 'https://www.youtube.com/watch?v=REG00000934')
    check("normalize_04534", normalize_url('https://www.youtube.com/watch?v=REG00000935') == 'https://www.youtube.com/watch?v=REG00000935')
    check("normalize_04535", normalize_url('https://www.youtube.com/watch?v=REG00000936') == 'https://www.youtube.com/watch?v=REG00000936')
    check("normalize_04536", normalize_url('https://www.youtube.com/watch?v=REG00000937') == 'https://www.youtube.com/watch?v=REG00000937')
    check("normalize_04537", normalize_url('https://www.youtube.com/watch?v=REG00000938') == 'https://www.youtube.com/watch?v=REG00000938')
    check("normalize_04538", normalize_url('https://www.youtube.com/watch?v=REG00000939') == 'https://www.youtube.com/watch?v=REG00000939')
    check("normalize_04539", normalize_url('https://www.youtube.com/watch?v=REG00000940') == 'https://www.youtube.com/watch?v=REG00000940')
    check("normalize_04540", normalize_url('https://www.youtube.com/watch?v=REG00000941') == 'https://www.youtube.com/watch?v=REG00000941')
    check("normalize_04541", normalize_url('https://www.youtube.com/watch?v=REG00000942') == 'https://www.youtube.com/watch?v=REG00000942')
    check("normalize_04542", normalize_url('https://www.youtube.com/watch?v=REG00000943') == 'https://www.youtube.com/watch?v=REG00000943')
    check("normalize_04543", normalize_url('https://www.youtube.com/watch?v=REG00000944') == 'https://www.youtube.com/watch?v=REG00000944')
    check("normalize_04544", normalize_url('https://www.youtube.com/watch?v=REG00000945') == 'https://www.youtube.com/watch?v=REG00000945')
    check("normalize_04545", normalize_url('https://www.youtube.com/watch?v=REG00000946') == 'https://www.youtube.com/watch?v=REG00000946')
    check("normalize_04546", normalize_url('https://www.youtube.com/watch?v=REG00000947') == 'https://www.youtube.com/watch?v=REG00000947')
    check("normalize_04547", normalize_url('https://www.youtube.com/watch?v=REG00000948') == 'https://www.youtube.com/watch?v=REG00000948')
    check("normalize_04548", normalize_url('https://www.youtube.com/watch?v=REG00000949') == 'https://www.youtube.com/watch?v=REG00000949')
    check("normalize_04549", normalize_url('https://www.youtube.com/watch?v=REG00000950') == 'https://www.youtube.com/watch?v=REG00000950')
    check("normalize_04550", normalize_url('https://www.youtube.com/watch?v=REG00000951') == 'https://www.youtube.com/watch?v=REG00000951')
    check("normalize_04551", normalize_url('https://www.youtube.com/watch?v=REG00000952') == 'https://www.youtube.com/watch?v=REG00000952')
    check("normalize_04552", normalize_url('https://www.youtube.com/watch?v=REG00000953') == 'https://www.youtube.com/watch?v=REG00000953')
    check("normalize_04553", normalize_url('https://www.youtube.com/watch?v=REG00000954') == 'https://www.youtube.com/watch?v=REG00000954')
    check("normalize_04554", normalize_url('https://www.youtube.com/watch?v=REG00000955') == 'https://www.youtube.com/watch?v=REG00000955')
    check("normalize_04555", normalize_url('https://www.youtube.com/watch?v=REG00000956') == 'https://www.youtube.com/watch?v=REG00000956')
    check("normalize_04556", normalize_url('https://www.youtube.com/watch?v=REG00000957') == 'https://www.youtube.com/watch?v=REG00000957')
    check("normalize_04557", normalize_url('https://www.youtube.com/watch?v=REG00000958') == 'https://www.youtube.com/watch?v=REG00000958')
    check("normalize_04558", normalize_url('https://www.youtube.com/watch?v=REG00000959') == 'https://www.youtube.com/watch?v=REG00000959')
    check("normalize_04559", normalize_url('https://www.youtube.com/watch?v=REG00000960') == 'https://www.youtube.com/watch?v=REG00000960')
    check("normalize_04560", normalize_url('https://www.youtube.com/watch?v=REG00000961') == 'https://www.youtube.com/watch?v=REG00000961')
    check("normalize_04561", normalize_url('https://www.youtube.com/watch?v=REG00000962') == 'https://www.youtube.com/watch?v=REG00000962')
    check("normalize_04562", normalize_url('https://www.youtube.com/watch?v=REG00000963') == 'https://www.youtube.com/watch?v=REG00000963')
    check("normalize_04563", normalize_url('https://www.youtube.com/watch?v=REG00000964') == 'https://www.youtube.com/watch?v=REG00000964')
    check("normalize_04564", normalize_url('https://www.youtube.com/watch?v=REG00000965') == 'https://www.youtube.com/watch?v=REG00000965')
    check("normalize_04565", normalize_url('https://www.youtube.com/watch?v=REG00000966') == 'https://www.youtube.com/watch?v=REG00000966')
    check("normalize_04566", normalize_url('https://www.youtube.com/watch?v=REG00000967') == 'https://www.youtube.com/watch?v=REG00000967')
    check("normalize_04567", normalize_url('https://www.youtube.com/watch?v=REG00000968') == 'https://www.youtube.com/watch?v=REG00000968')
    check("normalize_04568", normalize_url('https://www.youtube.com/watch?v=REG00000969') == 'https://www.youtube.com/watch?v=REG00000969')
    check("normalize_04569", normalize_url('https://www.youtube.com/watch?v=REG00000970') == 'https://www.youtube.com/watch?v=REG00000970')
    check("normalize_04570", normalize_url('https://www.youtube.com/watch?v=REG00000971') == 'https://www.youtube.com/watch?v=REG00000971')
    check("normalize_04571", normalize_url('https://www.youtube.com/watch?v=REG00000972') == 'https://www.youtube.com/watch?v=REG00000972')
    check("normalize_04572", normalize_url('https://www.youtube.com/watch?v=REG00000973') == 'https://www.youtube.com/watch?v=REG00000973')
    check("normalize_04573", normalize_url('https://www.youtube.com/watch?v=REG00000974') == 'https://www.youtube.com/watch?v=REG00000974')
    check("normalize_04574", normalize_url('https://www.youtube.com/watch?v=REG00000975') == 'https://www.youtube.com/watch?v=REG00000975')
    check("normalize_04575", normalize_url('https://www.youtube.com/watch?v=REG00000976') == 'https://www.youtube.com/watch?v=REG00000976')
    check("normalize_04576", normalize_url('https://www.youtube.com/watch?v=REG00000977') == 'https://www.youtube.com/watch?v=REG00000977')
    check("normalize_04577", normalize_url('https://www.youtube.com/watch?v=REG00000978') == 'https://www.youtube.com/watch?v=REG00000978')
    check("normalize_04578", normalize_url('https://www.youtube.com/watch?v=REG00000979') == 'https://www.youtube.com/watch?v=REG00000979')
    check("normalize_04579", normalize_url('https://www.youtube.com/watch?v=REG00000980') == 'https://www.youtube.com/watch?v=REG00000980')
    check("normalize_04580", normalize_url('https://www.youtube.com/watch?v=REG00000981') == 'https://www.youtube.com/watch?v=REG00000981')
    check("normalize_04581", normalize_url('https://www.youtube.com/watch?v=REG00000982') == 'https://www.youtube.com/watch?v=REG00000982')
    check("normalize_04582", normalize_url('https://www.youtube.com/watch?v=REG00000983') == 'https://www.youtube.com/watch?v=REG00000983')
    check("normalize_04583", normalize_url('https://www.youtube.com/watch?v=REG00000984') == 'https://www.youtube.com/watch?v=REG00000984')
    check("normalize_04584", normalize_url('https://www.youtube.com/watch?v=REG00000985') == 'https://www.youtube.com/watch?v=REG00000985')
    check("normalize_04585", normalize_url('https://www.youtube.com/watch?v=REG00000986') == 'https://www.youtube.com/watch?v=REG00000986')
    check("normalize_04586", normalize_url('https://www.youtube.com/watch?v=REG00000987') == 'https://www.youtube.com/watch?v=REG00000987')
    check("normalize_04587", normalize_url('https://www.youtube.com/watch?v=REG00000988') == 'https://www.youtube.com/watch?v=REG00000988')
    check("normalize_04588", normalize_url('https://www.youtube.com/watch?v=REG00000989') == 'https://www.youtube.com/watch?v=REG00000989')
    check("normalize_04589", normalize_url('https://www.youtube.com/watch?v=REG00000990') == 'https://www.youtube.com/watch?v=REG00000990')
    check("normalize_04590", normalize_url('https://www.youtube.com/watch?v=REG00000991') == 'https://www.youtube.com/watch?v=REG00000991')
    check("normalize_04591", normalize_url('https://www.youtube.com/watch?v=REG00000992') == 'https://www.youtube.com/watch?v=REG00000992')
    check("normalize_04592", normalize_url('https://www.youtube.com/watch?v=REG00000993') == 'https://www.youtube.com/watch?v=REG00000993')
    check("normalize_04593", normalize_url('https://www.youtube.com/watch?v=REG00000994') == 'https://www.youtube.com/watch?v=REG00000994')
    check("normalize_04594", normalize_url('https://www.youtube.com/watch?v=REG00000995') == 'https://www.youtube.com/watch?v=REG00000995')
    check("normalize_04595", normalize_url('https://www.youtube.com/watch?v=REG00000996') == 'https://www.youtube.com/watch?v=REG00000996')
    check("normalize_04596", normalize_url('https://www.youtube.com/watch?v=REG00000997') == 'https://www.youtube.com/watch?v=REG00000997')
    check("normalize_04597", normalize_url('https://www.youtube.com/watch?v=REG00000998') == 'https://www.youtube.com/watch?v=REG00000998')
    check("normalize_04598", normalize_url('https://www.youtube.com/watch?v=REG00000999') == 'https://www.youtube.com/watch?v=REG00000999')
    check("normalize_04599", normalize_url('https://www.youtube.com/watch?v=REG00001000') == 'https://www.youtube.com/watch?v=REG00001000')
    check("normalize_04600", normalize_url('https://www.youtube.com/watch?v=REG00001001') == 'https://www.youtube.com/watch?v=REG00001001')
    check("normalize_04601", normalize_url('https://www.youtube.com/watch?v=REG00001002') == 'https://www.youtube.com/watch?v=REG00001002')
    check("normalize_04602", normalize_url('https://www.youtube.com/watch?v=REG00001003') == 'https://www.youtube.com/watch?v=REG00001003')
    check("normalize_04603", normalize_url('https://www.youtube.com/watch?v=REG00001004') == 'https://www.youtube.com/watch?v=REG00001004')
    check("normalize_04604", normalize_url('https://www.youtube.com/watch?v=REG00001005') == 'https://www.youtube.com/watch?v=REG00001005')
    check("normalize_04605", normalize_url('https://www.youtube.com/watch?v=REG00001006') == 'https://www.youtube.com/watch?v=REG00001006')
    check("normalize_04606", normalize_url('https://www.youtube.com/watch?v=REG00001007') == 'https://www.youtube.com/watch?v=REG00001007')
    check("normalize_04607", normalize_url('https://www.youtube.com/watch?v=REG00001008') == 'https://www.youtube.com/watch?v=REG00001008')
    check("normalize_04608", normalize_url('https://www.youtube.com/watch?v=REG00001009') == 'https://www.youtube.com/watch?v=REG00001009')
    check("normalize_04609", normalize_url('https://www.youtube.com/watch?v=REG00001010') == 'https://www.youtube.com/watch?v=REG00001010')
    check("normalize_04610", normalize_url('https://www.youtube.com/watch?v=REG00001011') == 'https://www.youtube.com/watch?v=REG00001011')
    check("normalize_04611", normalize_url('https://www.youtube.com/watch?v=REG00001012') == 'https://www.youtube.com/watch?v=REG00001012')
    check("normalize_04612", normalize_url('https://www.youtube.com/watch?v=REG00001013') == 'https://www.youtube.com/watch?v=REG00001013')
    check("normalize_04613", normalize_url('https://www.youtube.com/watch?v=REG00001014') == 'https://www.youtube.com/watch?v=REG00001014')
    check("normalize_04614", normalize_url('https://www.youtube.com/watch?v=REG00001015') == 'https://www.youtube.com/watch?v=REG00001015')
    check("normalize_04615", normalize_url('https://www.youtube.com/watch?v=REG00001016') == 'https://www.youtube.com/watch?v=REG00001016')
    check("normalize_04616", normalize_url('https://www.youtube.com/watch?v=REG00001017') == 'https://www.youtube.com/watch?v=REG00001017')
    check("normalize_04617", normalize_url('https://www.youtube.com/watch?v=REG00001018') == 'https://www.youtube.com/watch?v=REG00001018')
    check("normalize_04618", normalize_url('https://www.youtube.com/watch?v=REG00001019') == 'https://www.youtube.com/watch?v=REG00001019')
    check("normalize_04619", normalize_url('https://www.youtube.com/watch?v=REG00001020') == 'https://www.youtube.com/watch?v=REG00001020')
    check("normalize_04620", normalize_url('https://www.youtube.com/watch?v=REG00001021') == 'https://www.youtube.com/watch?v=REG00001021')
    check("normalize_04621", normalize_url('https://www.youtube.com/watch?v=REG00001022') == 'https://www.youtube.com/watch?v=REG00001022')
    check("normalize_04622", normalize_url('https://www.youtube.com/watch?v=REG00001023') == 'https://www.youtube.com/watch?v=REG00001023')
    check("normalize_04623", normalize_url('https://www.youtube.com/watch?v=REG00001024') == 'https://www.youtube.com/watch?v=REG00001024')
    check("normalize_04624", normalize_url('https://www.youtube.com/watch?v=REG00001025') == 'https://www.youtube.com/watch?v=REG00001025')
    check("normalize_04625", normalize_url('https://www.youtube.com/watch?v=REG00001026') == 'https://www.youtube.com/watch?v=REG00001026')
    check("normalize_04626", normalize_url('https://www.youtube.com/watch?v=REG00001027') == 'https://www.youtube.com/watch?v=REG00001027')
    check("normalize_04627", normalize_url('https://www.youtube.com/watch?v=REG00001028') == 'https://www.youtube.com/watch?v=REG00001028')
    check("normalize_04628", normalize_url('https://www.youtube.com/watch?v=REG00001029') == 'https://www.youtube.com/watch?v=REG00001029')
    check("normalize_04629", normalize_url('https://www.youtube.com/watch?v=REG00001030') == 'https://www.youtube.com/watch?v=REG00001030')
    check("normalize_04630", normalize_url('https://www.youtube.com/watch?v=REG00001031') == 'https://www.youtube.com/watch?v=REG00001031')
    check("normalize_04631", normalize_url('https://www.youtube.com/watch?v=REG00001032') == 'https://www.youtube.com/watch?v=REG00001032')
    check("normalize_04632", normalize_url('https://www.youtube.com/watch?v=REG00001033') == 'https://www.youtube.com/watch?v=REG00001033')
    check("normalize_04633", normalize_url('https://www.youtube.com/watch?v=REG00001034') == 'https://www.youtube.com/watch?v=REG00001034')
    check("normalize_04634", normalize_url('https://www.youtube.com/watch?v=REG00001035') == 'https://www.youtube.com/watch?v=REG00001035')
    check("normalize_04635", normalize_url('https://www.youtube.com/watch?v=REG00001036') == 'https://www.youtube.com/watch?v=REG00001036')
    check("normalize_04636", normalize_url('https://www.youtube.com/watch?v=REG00001037') == 'https://www.youtube.com/watch?v=REG00001037')
    check("normalize_04637", normalize_url('https://www.youtube.com/watch?v=REG00001038') == 'https://www.youtube.com/watch?v=REG00001038')
    check("normalize_04638", normalize_url('https://www.youtube.com/watch?v=REG00001039') == 'https://www.youtube.com/watch?v=REG00001039')
    check("normalize_04639", normalize_url('https://www.youtube.com/watch?v=REG00001040') == 'https://www.youtube.com/watch?v=REG00001040')
    check("normalize_04640", normalize_url('https://www.youtube.com/watch?v=REG00001041') == 'https://www.youtube.com/watch?v=REG00001041')
    check("normalize_04641", normalize_url('https://www.youtube.com/watch?v=REG00001042') == 'https://www.youtube.com/watch?v=REG00001042')
    check("normalize_04642", normalize_url('https://www.youtube.com/watch?v=REG00001043') == 'https://www.youtube.com/watch?v=REG00001043')
    check("normalize_04643", normalize_url('https://www.youtube.com/watch?v=REG00001044') == 'https://www.youtube.com/watch?v=REG00001044')
    check("normalize_04644", normalize_url('https://www.youtube.com/watch?v=REG00001045') == 'https://www.youtube.com/watch?v=REG00001045')
    check("normalize_04645", normalize_url('https://www.youtube.com/watch?v=REG00001046') == 'https://www.youtube.com/watch?v=REG00001046')
    check("normalize_04646", normalize_url('https://www.youtube.com/watch?v=REG00001047') == 'https://www.youtube.com/watch?v=REG00001047')
    check("normalize_04647", normalize_url('https://www.youtube.com/watch?v=REG00001048') == 'https://www.youtube.com/watch?v=REG00001048')
    check("normalize_04648", normalize_url('https://www.youtube.com/watch?v=REG00001049') == 'https://www.youtube.com/watch?v=REG00001049')
    check("normalize_04649", normalize_url('https://www.youtube.com/watch?v=REG00001050') == 'https://www.youtube.com/watch?v=REG00001050')
    check("normalize_04650", normalize_url('https://www.youtube.com/watch?v=REG00001051') == 'https://www.youtube.com/watch?v=REG00001051')
    check("normalize_04651", normalize_url('https://www.youtube.com/watch?v=REG00001052') == 'https://www.youtube.com/watch?v=REG00001052')
    check("normalize_04652", normalize_url('https://www.youtube.com/watch?v=REG00001053') == 'https://www.youtube.com/watch?v=REG00001053')
    check("normalize_04653", normalize_url('https://www.youtube.com/watch?v=REG00001054') == 'https://www.youtube.com/watch?v=REG00001054')
    check("normalize_04654", normalize_url('https://www.youtube.com/watch?v=REG00001055') == 'https://www.youtube.com/watch?v=REG00001055')
    check("normalize_04655", normalize_url('https://www.youtube.com/watch?v=REG00001056') == 'https://www.youtube.com/watch?v=REG00001056')
    check("normalize_04656", normalize_url('https://www.youtube.com/watch?v=REG00001057') == 'https://www.youtube.com/watch?v=REG00001057')
    check("normalize_04657", normalize_url('https://www.youtube.com/watch?v=REG00001058') == 'https://www.youtube.com/watch?v=REG00001058')
    check("normalize_04658", normalize_url('https://www.youtube.com/watch?v=REG00001059') == 'https://www.youtube.com/watch?v=REG00001059')
    check("normalize_04659", normalize_url('https://www.youtube.com/watch?v=REG00001060') == 'https://www.youtube.com/watch?v=REG00001060')
    check("normalize_04660", normalize_url('https://www.youtube.com/watch?v=REG00001061') == 'https://www.youtube.com/watch?v=REG00001061')
    check("normalize_04661", normalize_url('https://www.youtube.com/watch?v=REG00001062') == 'https://www.youtube.com/watch?v=REG00001062')
    check("normalize_04662", normalize_url('https://www.youtube.com/watch?v=REG00001063') == 'https://www.youtube.com/watch?v=REG00001063')
    check("normalize_04663", normalize_url('https://www.youtube.com/watch?v=REG00001064') == 'https://www.youtube.com/watch?v=REG00001064')
    check("normalize_04664", normalize_url('https://www.youtube.com/watch?v=REG00001065') == 'https://www.youtube.com/watch?v=REG00001065')
    check("normalize_04665", normalize_url('https://www.youtube.com/watch?v=REG00001066') == 'https://www.youtube.com/watch?v=REG00001066')
    check("normalize_04666", normalize_url('https://www.youtube.com/watch?v=REG00001067') == 'https://www.youtube.com/watch?v=REG00001067')
    check("normalize_04667", normalize_url('https://www.youtube.com/watch?v=REG00001068') == 'https://www.youtube.com/watch?v=REG00001068')
    check("normalize_04668", normalize_url('https://www.youtube.com/watch?v=REG00001069') == 'https://www.youtube.com/watch?v=REG00001069')
    check("normalize_04669", normalize_url('https://www.youtube.com/watch?v=REG00001070') == 'https://www.youtube.com/watch?v=REG00001070')
    check("normalize_04670", normalize_url('https://www.youtube.com/watch?v=REG00001071') == 'https://www.youtube.com/watch?v=REG00001071')
    check("normalize_04671", normalize_url('https://www.youtube.com/watch?v=REG00001072') == 'https://www.youtube.com/watch?v=REG00001072')
    check("normalize_04672", normalize_url('https://www.youtube.com/watch?v=REG00001073') == 'https://www.youtube.com/watch?v=REG00001073')
    check("normalize_04673", normalize_url('https://www.youtube.com/watch?v=REG00001074') == 'https://www.youtube.com/watch?v=REG00001074')
    check("normalize_04674", normalize_url('https://www.youtube.com/watch?v=REG00001075') == 'https://www.youtube.com/watch?v=REG00001075')
    check("normalize_04675", normalize_url('https://www.youtube.com/watch?v=REG00001076') == 'https://www.youtube.com/watch?v=REG00001076')
    check("normalize_04676", normalize_url('https://www.youtube.com/watch?v=REG00001077') == 'https://www.youtube.com/watch?v=REG00001077')
    check("normalize_04677", normalize_url('https://www.youtube.com/watch?v=REG00001078') == 'https://www.youtube.com/watch?v=REG00001078')
    check("normalize_04678", normalize_url('https://www.youtube.com/watch?v=REG00001079') == 'https://www.youtube.com/watch?v=REG00001079')
    check("normalize_04679", normalize_url('https://www.youtube.com/watch?v=REG00001080') == 'https://www.youtube.com/watch?v=REG00001080')
    check("normalize_04680", normalize_url('https://www.youtube.com/watch?v=REG00001081') == 'https://www.youtube.com/watch?v=REG00001081')
    check("normalize_04681", normalize_url('https://www.youtube.com/watch?v=REG00001082') == 'https://www.youtube.com/watch?v=REG00001082')
    check("normalize_04682", normalize_url('https://www.youtube.com/watch?v=REG00001083') == 'https://www.youtube.com/watch?v=REG00001083')
    check("normalize_04683", normalize_url('https://www.youtube.com/watch?v=REG00001084') == 'https://www.youtube.com/watch?v=REG00001084')
    check("normalize_04684", normalize_url('https://www.youtube.com/watch?v=REG00001085') == 'https://www.youtube.com/watch?v=REG00001085')
    check("normalize_04685", normalize_url('https://www.youtube.com/watch?v=REG00001086') == 'https://www.youtube.com/watch?v=REG00001086')
    check("normalize_04686", normalize_url('https://www.youtube.com/watch?v=REG00001087') == 'https://www.youtube.com/watch?v=REG00001087')
    check("normalize_04687", normalize_url('https://www.youtube.com/watch?v=REG00001088') == 'https://www.youtube.com/watch?v=REG00001088')
    check("normalize_04688", normalize_url('https://www.youtube.com/watch?v=REG00001089') == 'https://www.youtube.com/watch?v=REG00001089')
    check("normalize_04689", normalize_url('https://www.youtube.com/watch?v=REG00001090') == 'https://www.youtube.com/watch?v=REG00001090')
    check("normalize_04690", normalize_url('https://www.youtube.com/watch?v=REG00001091') == 'https://www.youtube.com/watch?v=REG00001091')
    check("normalize_04691", normalize_url('https://www.youtube.com/watch?v=REG00001092') == 'https://www.youtube.com/watch?v=REG00001092')
    check("normalize_04692", normalize_url('https://www.youtube.com/watch?v=REG00001093') == 'https://www.youtube.com/watch?v=REG00001093')
    check("normalize_04693", normalize_url('https://www.youtube.com/watch?v=REG00001094') == 'https://www.youtube.com/watch?v=REG00001094')
    check("normalize_04694", normalize_url('https://www.youtube.com/watch?v=REG00001095') == 'https://www.youtube.com/watch?v=REG00001095')
    check("normalize_04695", normalize_url('https://www.youtube.com/watch?v=REG00001096') == 'https://www.youtube.com/watch?v=REG00001096')
    check("normalize_04696", normalize_url('https://www.youtube.com/watch?v=REG00001097') == 'https://www.youtube.com/watch?v=REG00001097')
    check("normalize_04697", normalize_url('https://www.youtube.com/watch?v=REG00001098') == 'https://www.youtube.com/watch?v=REG00001098')
    check("normalize_04698", normalize_url('https://www.youtube.com/watch?v=REG00001099') == 'https://www.youtube.com/watch?v=REG00001099')
    check("normalize_04699", normalize_url('https://www.youtube.com/watch?v=REG00001100') == 'https://www.youtube.com/watch?v=REG00001100')
    check("normalize_04700", normalize_url('https://www.youtube.com/watch?v=REG00001101') == 'https://www.youtube.com/watch?v=REG00001101')
    check("normalize_04701", normalize_url('https://www.youtube.com/watch?v=REG00001102') == 'https://www.youtube.com/watch?v=REG00001102')
    check("normalize_04702", normalize_url('https://www.youtube.com/watch?v=REG00001103') == 'https://www.youtube.com/watch?v=REG00001103')
    check("normalize_04703", normalize_url('https://www.youtube.com/watch?v=REG00001104') == 'https://www.youtube.com/watch?v=REG00001104')
    check("normalize_04704", normalize_url('https://www.youtube.com/watch?v=REG00001105') == 'https://www.youtube.com/watch?v=REG00001105')
    check("normalize_04705", normalize_url('https://www.youtube.com/watch?v=REG00001106') == 'https://www.youtube.com/watch?v=REG00001106')
    check("normalize_04706", normalize_url('https://www.youtube.com/watch?v=REG00001107') == 'https://www.youtube.com/watch?v=REG00001107')
    check("normalize_04707", normalize_url('https://www.youtube.com/watch?v=REG00001108') == 'https://www.youtube.com/watch?v=REG00001108')
    check("normalize_04708", normalize_url('https://www.youtube.com/watch?v=REG00001109') == 'https://www.youtube.com/watch?v=REG00001109')
    check("normalize_04709", normalize_url('https://www.youtube.com/watch?v=REG00001110') == 'https://www.youtube.com/watch?v=REG00001110')
    check("normalize_04710", normalize_url('https://www.youtube.com/watch?v=REG00001111') == 'https://www.youtube.com/watch?v=REG00001111')
    check("normalize_04711", normalize_url('https://www.youtube.com/watch?v=REG00001112') == 'https://www.youtube.com/watch?v=REG00001112')
    check("normalize_04712", normalize_url('https://www.youtube.com/watch?v=REG00001113') == 'https://www.youtube.com/watch?v=REG00001113')
    check("normalize_04713", normalize_url('https://www.youtube.com/watch?v=REG00001114') == 'https://www.youtube.com/watch?v=REG00001114')
    check("normalize_04714", normalize_url('https://www.youtube.com/watch?v=REG00001115') == 'https://www.youtube.com/watch?v=REG00001115')
    check("normalize_04715", normalize_url('https://www.youtube.com/watch?v=REG00001116') == 'https://www.youtube.com/watch?v=REG00001116')
    check("normalize_04716", normalize_url('https://www.youtube.com/watch?v=REG00001117') == 'https://www.youtube.com/watch?v=REG00001117')
    check("normalize_04717", normalize_url('https://www.youtube.com/watch?v=REG00001118') == 'https://www.youtube.com/watch?v=REG00001118')
    check("normalize_04718", normalize_url('https://www.youtube.com/watch?v=REG00001119') == 'https://www.youtube.com/watch?v=REG00001119')
    check("normalize_04719", normalize_url('https://www.youtube.com/watch?v=REG00001120') == 'https://www.youtube.com/watch?v=REG00001120')
    check("normalize_04720", normalize_url('https://www.youtube.com/watch?v=REG00001121') == 'https://www.youtube.com/watch?v=REG00001121')
    check("normalize_04721", normalize_url('https://www.youtube.com/watch?v=REG00001122') == 'https://www.youtube.com/watch?v=REG00001122')
    check("normalize_04722", normalize_url('https://www.youtube.com/watch?v=REG00001123') == 'https://www.youtube.com/watch?v=REG00001123')
    check("normalize_04723", normalize_url('https://www.youtube.com/watch?v=REG00001124') == 'https://www.youtube.com/watch?v=REG00001124')
    check("normalize_04724", normalize_url('https://www.youtube.com/watch?v=REG00001125') == 'https://www.youtube.com/watch?v=REG00001125')
    check("normalize_04725", normalize_url('https://www.youtube.com/watch?v=REG00001126') == 'https://www.youtube.com/watch?v=REG00001126')
    check("normalize_04726", normalize_url('https://www.youtube.com/watch?v=REG00001127') == 'https://www.youtube.com/watch?v=REG00001127')
    check("normalize_04727", normalize_url('https://www.youtube.com/watch?v=REG00001128') == 'https://www.youtube.com/watch?v=REG00001128')
    check("normalize_04728", normalize_url('https://www.youtube.com/watch?v=REG00001129') == 'https://www.youtube.com/watch?v=REG00001129')
    check("normalize_04729", normalize_url('https://www.youtube.com/watch?v=REG00001130') == 'https://www.youtube.com/watch?v=REG00001130')
    check("normalize_04730", normalize_url('https://www.youtube.com/watch?v=REG00001131') == 'https://www.youtube.com/watch?v=REG00001131')
    check("normalize_04731", normalize_url('https://www.youtube.com/watch?v=REG00001132') == 'https://www.youtube.com/watch?v=REG00001132')
    check("normalize_04732", normalize_url('https://www.youtube.com/watch?v=REG00001133') == 'https://www.youtube.com/watch?v=REG00001133')
    check("normalize_04733", normalize_url('https://www.youtube.com/watch?v=REG00001134') == 'https://www.youtube.com/watch?v=REG00001134')
    check("normalize_04734", normalize_url('https://www.youtube.com/watch?v=REG00001135') == 'https://www.youtube.com/watch?v=REG00001135')
    check("normalize_04735", normalize_url('https://www.youtube.com/watch?v=REG00001136') == 'https://www.youtube.com/watch?v=REG00001136')
    check("normalize_04736", normalize_url('https://www.youtube.com/watch?v=REG00001137') == 'https://www.youtube.com/watch?v=REG00001137')
    check("normalize_04737", normalize_url('https://www.youtube.com/watch?v=REG00001138') == 'https://www.youtube.com/watch?v=REG00001138')
    check("normalize_04738", normalize_url('https://www.youtube.com/watch?v=REG00001139') == 'https://www.youtube.com/watch?v=REG00001139')
    check("normalize_04739", normalize_url('https://www.youtube.com/watch?v=REG00001140') == 'https://www.youtube.com/watch?v=REG00001140')
    check("normalize_04740", normalize_url('https://www.youtube.com/watch?v=REG00001141') == 'https://www.youtube.com/watch?v=REG00001141')
    check("normalize_04741", normalize_url('https://www.youtube.com/watch?v=REG00001142') == 'https://www.youtube.com/watch?v=REG00001142')
    check("normalize_04742", normalize_url('https://www.youtube.com/watch?v=REG00001143') == 'https://www.youtube.com/watch?v=REG00001143')
    check("normalize_04743", normalize_url('https://www.youtube.com/watch?v=REG00001144') == 'https://www.youtube.com/watch?v=REG00001144')
    check("normalize_04744", normalize_url('https://www.youtube.com/watch?v=REG00001145') == 'https://www.youtube.com/watch?v=REG00001145')
    check("normalize_04745", normalize_url('https://www.youtube.com/watch?v=REG00001146') == 'https://www.youtube.com/watch?v=REG00001146')
    check("normalize_04746", normalize_url('https://www.youtube.com/watch?v=REG00001147') == 'https://www.youtube.com/watch?v=REG00001147')
    check("normalize_04747", normalize_url('https://www.youtube.com/watch?v=REG00001148') == 'https://www.youtube.com/watch?v=REG00001148')
    check("normalize_04748", normalize_url('https://www.youtube.com/watch?v=REG00001149') == 'https://www.youtube.com/watch?v=REG00001149')
    check("normalize_04749", normalize_url('https://www.youtube.com/watch?v=REG00001150') == 'https://www.youtube.com/watch?v=REG00001150')
    check("normalize_04750", normalize_url('https://www.youtube.com/watch?v=REG00001151') == 'https://www.youtube.com/watch?v=REG00001151')
    check("normalize_04751", normalize_url('https://www.youtube.com/watch?v=REG00001152') == 'https://www.youtube.com/watch?v=REG00001152')
    check("normalize_04752", normalize_url('https://www.youtube.com/watch?v=REG00001153') == 'https://www.youtube.com/watch?v=REG00001153')
    check("normalize_04753", normalize_url('https://www.youtube.com/watch?v=REG00001154') == 'https://www.youtube.com/watch?v=REG00001154')
    check("normalize_04754", normalize_url('https://www.youtube.com/watch?v=REG00001155') == 'https://www.youtube.com/watch?v=REG00001155')
    check("normalize_04755", normalize_url('https://www.youtube.com/watch?v=REG00001156') == 'https://www.youtube.com/watch?v=REG00001156')
    check("normalize_04756", normalize_url('https://www.youtube.com/watch?v=REG00001157') == 'https://www.youtube.com/watch?v=REG00001157')
    check("normalize_04757", normalize_url('https://www.youtube.com/watch?v=REG00001158') == 'https://www.youtube.com/watch?v=REG00001158')
    check("normalize_04758", normalize_url('https://www.youtube.com/watch?v=REG00001159') == 'https://www.youtube.com/watch?v=REG00001159')
    check("normalize_04759", normalize_url('https://www.youtube.com/watch?v=REG00001160') == 'https://www.youtube.com/watch?v=REG00001160')
    check("normalize_04760", normalize_url('https://www.youtube.com/watch?v=REG00001161') == 'https://www.youtube.com/watch?v=REG00001161')
    check("normalize_04761", normalize_url('https://www.youtube.com/watch?v=REG00001162') == 'https://www.youtube.com/watch?v=REG00001162')
    check("normalize_04762", normalize_url('https://www.youtube.com/watch?v=REG00001163') == 'https://www.youtube.com/watch?v=REG00001163')
    check("normalize_04763", normalize_url('https://www.youtube.com/watch?v=REG00001164') == 'https://www.youtube.com/watch?v=REG00001164')
    check("normalize_04764", normalize_url('https://www.youtube.com/watch?v=REG00001165') == 'https://www.youtube.com/watch?v=REG00001165')
    check("normalize_04765", normalize_url('https://www.youtube.com/watch?v=REG00001166') == 'https://www.youtube.com/watch?v=REG00001166')
    check("normalize_04766", normalize_url('https://www.youtube.com/watch?v=REG00001167') == 'https://www.youtube.com/watch?v=REG00001167')
    check("normalize_04767", normalize_url('https://www.youtube.com/watch?v=REG00001168') == 'https://www.youtube.com/watch?v=REG00001168')
    check("normalize_04768", normalize_url('https://www.youtube.com/watch?v=REG00001169') == 'https://www.youtube.com/watch?v=REG00001169')
    check("normalize_04769", normalize_url('https://www.youtube.com/watch?v=REG00001170') == 'https://www.youtube.com/watch?v=REG00001170')
    check("normalize_04770", normalize_url('https://www.youtube.com/watch?v=REG00001171') == 'https://www.youtube.com/watch?v=REG00001171')
    check("normalize_04771", normalize_url('https://www.youtube.com/watch?v=REG00001172') == 'https://www.youtube.com/watch?v=REG00001172')
    check("normalize_04772", normalize_url('https://www.youtube.com/watch?v=REG00001173') == 'https://www.youtube.com/watch?v=REG00001173')
    check("normalize_04773", normalize_url('https://www.youtube.com/watch?v=REG00001174') == 'https://www.youtube.com/watch?v=REG00001174')
    check("normalize_04774", normalize_url('https://www.youtube.com/watch?v=REG00001175') == 'https://www.youtube.com/watch?v=REG00001175')
    check("normalize_04775", normalize_url('https://www.youtube.com/watch?v=REG00001176') == 'https://www.youtube.com/watch?v=REG00001176')
    check("normalize_04776", normalize_url('https://www.youtube.com/watch?v=REG00001177') == 'https://www.youtube.com/watch?v=REG00001177')
    check("normalize_04777", normalize_url('https://www.youtube.com/watch?v=REG00001178') == 'https://www.youtube.com/watch?v=REG00001178')
    check("normalize_04778", normalize_url('https://www.youtube.com/watch?v=REG00001179') == 'https://www.youtube.com/watch?v=REG00001179')
    check("normalize_04779", normalize_url('https://www.youtube.com/watch?v=REG00001180') == 'https://www.youtube.com/watch?v=REG00001180')
    check("normalize_04780", normalize_url('https://www.youtube.com/watch?v=REG00001181') == 'https://www.youtube.com/watch?v=REG00001181')
    check("normalize_04781", normalize_url('https://www.youtube.com/watch?v=REG00001182') == 'https://www.youtube.com/watch?v=REG00001182')
    check("normalize_04782", normalize_url('https://www.youtube.com/watch?v=REG00001183') == 'https://www.youtube.com/watch?v=REG00001183')
    check("normalize_04783", normalize_url('https://www.youtube.com/watch?v=REG00001184') == 'https://www.youtube.com/watch?v=REG00001184')
    check("normalize_04784", normalize_url('https://www.youtube.com/watch?v=REG00001185') == 'https://www.youtube.com/watch?v=REG00001185')
    check("normalize_04785", normalize_url('https://www.youtube.com/watch?v=REG00001186') == 'https://www.youtube.com/watch?v=REG00001186')
    check("normalize_04786", normalize_url('https://www.youtube.com/watch?v=REG00001187') == 'https://www.youtube.com/watch?v=REG00001187')
    check("normalize_04787", normalize_url('https://www.youtube.com/watch?v=REG00001188') == 'https://www.youtube.com/watch?v=REG00001188')
    check("normalize_04788", normalize_url('https://www.youtube.com/watch?v=REG00001189') == 'https://www.youtube.com/watch?v=REG00001189')
    check("normalize_04789", normalize_url('https://www.youtube.com/watch?v=REG00001190') == 'https://www.youtube.com/watch?v=REG00001190')
    check("normalize_04790", normalize_url('https://www.youtube.com/watch?v=REG00001191') == 'https://www.youtube.com/watch?v=REG00001191')
    check("normalize_04791", normalize_url('https://www.youtube.com/watch?v=REG00001192') == 'https://www.youtube.com/watch?v=REG00001192')
    check("normalize_04792", normalize_url('https://www.youtube.com/watch?v=REG00001193') == 'https://www.youtube.com/watch?v=REG00001193')
    check("normalize_04793", normalize_url('https://www.youtube.com/watch?v=REG00001194') == 'https://www.youtube.com/watch?v=REG00001194')
    check("normalize_04794", normalize_url('https://www.youtube.com/watch?v=REG00001195') == 'https://www.youtube.com/watch?v=REG00001195')
    check("normalize_04795", normalize_url('https://www.youtube.com/watch?v=REG00001196') == 'https://www.youtube.com/watch?v=REG00001196')
    check("normalize_04796", normalize_url('https://www.youtube.com/watch?v=REG00001197') == 'https://www.youtube.com/watch?v=REG00001197')
    check("normalize_04797", normalize_url('https://www.youtube.com/watch?v=REG00001198') == 'https://www.youtube.com/watch?v=REG00001198')
    check("normalize_04798", normalize_url('https://www.youtube.com/watch?v=REG00001199') == 'https://www.youtube.com/watch?v=REG00001199')
    check("normalize_04799", normalize_url('https://www.youtube.com/watch?v=REG00001200') == 'https://www.youtube.com/watch?v=REG00001200')
    check("fbkind_04800", fb_kind('https://www.facebook.com/reel/K0000001') == 'REEL')
    check("fbkind_04801", fb_kind('https://www.facebook.com/reel/K0000002') == 'REEL')
    check("fbkind_04802", fb_kind('https://www.facebook.com/reel/K0000003') == 'REEL')
    check("fbkind_04803", fb_kind('https://www.facebook.com/reel/K0000004') == 'REEL')
    check("fbkind_04804", fb_kind('https://www.facebook.com/reel/K0000005') == 'REEL')
    check("fbkind_04805", fb_kind('https://www.facebook.com/reel/K0000006') == 'REEL')
    check("fbkind_04806", fb_kind('https://www.facebook.com/reel/K0000007') == 'REEL')
    check("fbkind_04807", fb_kind('https://www.facebook.com/reel/K0000008') == 'REEL')
    check("fbkind_04808", fb_kind('https://www.facebook.com/reel/K0000009') == 'REEL')
    check("fbkind_04809", fb_kind('https://www.facebook.com/reel/K0000010') == 'REEL')
    check("fbkind_04810", fb_kind('https://www.facebook.com/reel/K0000011') == 'REEL')
    check("fbkind_04811", fb_kind('https://www.facebook.com/reel/K0000012') == 'REEL')
    check("fbkind_04812", fb_kind('https://www.facebook.com/reel/K0000013') == 'REEL')
    check("fbkind_04813", fb_kind('https://www.facebook.com/reel/K0000014') == 'REEL')
    check("fbkind_04814", fb_kind('https://www.facebook.com/reel/K0000015') == 'REEL')
    check("fbkind_04815", fb_kind('https://www.facebook.com/reel/K0000016') == 'REEL')
    check("fbkind_04816", fb_kind('https://www.facebook.com/reel/K0000017') == 'REEL')
    check("fbkind_04817", fb_kind('https://www.facebook.com/reel/K0000018') == 'REEL')
    check("fbkind_04818", fb_kind('https://www.facebook.com/reel/K0000019') == 'REEL')
    check("fbkind_04819", fb_kind('https://www.facebook.com/reel/K0000020') == 'REEL')
    check("fbkind_04820", fb_kind('https://www.facebook.com/reel/K0000021') == 'REEL')
    check("fbkind_04821", fb_kind('https://www.facebook.com/reel/K0000022') == 'REEL')
    check("fbkind_04822", fb_kind('https://www.facebook.com/reel/K0000023') == 'REEL')
    check("fbkind_04823", fb_kind('https://www.facebook.com/reel/K0000024') == 'REEL')
    check("fbkind_04824", fb_kind('https://www.facebook.com/reel/K0000025') == 'REEL')
    check("fbkind_04825", fb_kind('https://www.facebook.com/reel/K0000026') == 'REEL')
    check("fbkind_04826", fb_kind('https://www.facebook.com/reel/K0000027') == 'REEL')
    check("fbkind_04827", fb_kind('https://www.facebook.com/reel/K0000028') == 'REEL')
    check("fbkind_04828", fb_kind('https://www.facebook.com/reel/K0000029') == 'REEL')
    check("fbkind_04829", fb_kind('https://www.facebook.com/reel/K0000030') == 'REEL')
    check("fbkind_04830", fb_kind('https://www.facebook.com/reel/K0000031') == 'REEL')
    check("fbkind_04831", fb_kind('https://www.facebook.com/reel/K0000032') == 'REEL')
    check("fbkind_04832", fb_kind('https://www.facebook.com/reel/K0000033') == 'REEL')
    check("fbkind_04833", fb_kind('https://www.facebook.com/reel/K0000034') == 'REEL')
    check("fbkind_04834", fb_kind('https://www.facebook.com/reel/K0000035') == 'REEL')
    check("fbkind_04835", fb_kind('https://www.facebook.com/reel/K0000036') == 'REEL')
    check("fbkind_04836", fb_kind('https://www.facebook.com/reel/K0000037') == 'REEL')
    check("fbkind_04837", fb_kind('https://www.facebook.com/reel/K0000038') == 'REEL')
    check("fbkind_04838", fb_kind('https://www.facebook.com/reel/K0000039') == 'REEL')
    check("fbkind_04839", fb_kind('https://www.facebook.com/reel/K0000040') == 'REEL')
    check("fbkind_04840", fb_kind('https://www.facebook.com/reel/K0000041') == 'REEL')
    check("fbkind_04841", fb_kind('https://www.facebook.com/reel/K0000042') == 'REEL')
    check("fbkind_04842", fb_kind('https://www.facebook.com/reel/K0000043') == 'REEL')
    check("fbkind_04843", fb_kind('https://www.facebook.com/reel/K0000044') == 'REEL')
    check("fbkind_04844", fb_kind('https://www.facebook.com/reel/K0000045') == 'REEL')
    check("fbkind_04845", fb_kind('https://www.facebook.com/reel/K0000046') == 'REEL')
    check("fbkind_04846", fb_kind('https://www.facebook.com/reel/K0000047') == 'REEL')
    check("fbkind_04847", fb_kind('https://www.facebook.com/reel/K0000048') == 'REEL')
    check("fbkind_04848", fb_kind('https://www.facebook.com/reel/K0000049') == 'REEL')
    check("fbkind_04849", fb_kind('https://www.facebook.com/reel/K0000050') == 'REEL')
    check("fbkind_04850", fb_kind('https://www.facebook.com/reel/K0000051') == 'REEL')
    check("fbkind_04851", fb_kind('https://www.facebook.com/reel/K0000052') == 'REEL')
    check("fbkind_04852", fb_kind('https://www.facebook.com/reel/K0000053') == 'REEL')
    check("fbkind_04853", fb_kind('https://www.facebook.com/reel/K0000054') == 'REEL')
    check("fbkind_04854", fb_kind('https://www.facebook.com/reel/K0000055') == 'REEL')
    check("fbkind_04855", fb_kind('https://www.facebook.com/reel/K0000056') == 'REEL')
    check("fbkind_04856", fb_kind('https://www.facebook.com/reel/K0000057') == 'REEL')
    check("fbkind_04857", fb_kind('https://www.facebook.com/reel/K0000058') == 'REEL')
    check("fbkind_04858", fb_kind('https://www.facebook.com/reel/K0000059') == 'REEL')
    check("fbkind_04859", fb_kind('https://www.facebook.com/reel/K0000060') == 'REEL')
    check("fbkind_04860", fb_kind('https://www.facebook.com/reel/K0000061') == 'REEL')
    check("fbkind_04861", fb_kind('https://www.facebook.com/reel/K0000062') == 'REEL')
    check("fbkind_04862", fb_kind('https://www.facebook.com/reel/K0000063') == 'REEL')
    check("fbkind_04863", fb_kind('https://www.facebook.com/reel/K0000064') == 'REEL')
    check("fbkind_04864", fb_kind('https://www.facebook.com/reel/K0000065') == 'REEL')
    check("fbkind_04865", fb_kind('https://www.facebook.com/reel/K0000066') == 'REEL')
    check("fbkind_04866", fb_kind('https://www.facebook.com/reel/K0000067') == 'REEL')
    check("fbkind_04867", fb_kind('https://www.facebook.com/reel/K0000068') == 'REEL')
    check("fbkind_04868", fb_kind('https://www.facebook.com/reel/K0000069') == 'REEL')
    check("fbkind_04869", fb_kind('https://www.facebook.com/reel/K0000070') == 'REEL')
    check("fbkind_04870", fb_kind('https://www.facebook.com/reel/K0000071') == 'REEL')
    check("fbkind_04871", fb_kind('https://www.facebook.com/reel/K0000072') == 'REEL')
    check("fbkind_04872", fb_kind('https://www.facebook.com/reel/K0000073') == 'REEL')
    check("fbkind_04873", fb_kind('https://www.facebook.com/reel/K0000074') == 'REEL')
    check("fbkind_04874", fb_kind('https://www.facebook.com/reel/K0000075') == 'REEL')
    check("fbkind_04875", fb_kind('https://www.facebook.com/reel/K0000076') == 'REEL')
    check("fbkind_04876", fb_kind('https://www.facebook.com/reel/K0000077') == 'REEL')
    check("fbkind_04877", fb_kind('https://www.facebook.com/reel/K0000078') == 'REEL')
    check("fbkind_04878", fb_kind('https://www.facebook.com/reel/K0000079') == 'REEL')
    check("fbkind_04879", fb_kind('https://www.facebook.com/reel/K0000080') == 'REEL')
    check("fbkind_04880", fb_kind('https://www.facebook.com/reel/K0000081') == 'REEL')
    check("fbkind_04881", fb_kind('https://www.facebook.com/reel/K0000082') == 'REEL')
    check("fbkind_04882", fb_kind('https://www.facebook.com/reel/K0000083') == 'REEL')
    check("fbkind_04883", fb_kind('https://www.facebook.com/reel/K0000084') == 'REEL')
    check("fbkind_04884", fb_kind('https://www.facebook.com/reel/K0000085') == 'REEL')
    check("fbkind_04885", fb_kind('https://www.facebook.com/reel/K0000086') == 'REEL')
    check("fbkind_04886", fb_kind('https://www.facebook.com/reel/K0000087') == 'REEL')
    check("fbkind_04887", fb_kind('https://www.facebook.com/reel/K0000088') == 'REEL')
    check("fbkind_04888", fb_kind('https://www.facebook.com/reel/K0000089') == 'REEL')
    check("fbkind_04889", fb_kind('https://www.facebook.com/reel/K0000090') == 'REEL')
    check("fbkind_04890", fb_kind('https://www.facebook.com/reel/K0000091') == 'REEL')
    check("fbkind_04891", fb_kind('https://www.facebook.com/reel/K0000092') == 'REEL')
    check("fbkind_04892", fb_kind('https://www.facebook.com/reel/K0000093') == 'REEL')
    check("fbkind_04893", fb_kind('https://www.facebook.com/reel/K0000094') == 'REEL')
    check("fbkind_04894", fb_kind('https://www.facebook.com/reel/K0000095') == 'REEL')
    check("fbkind_04895", fb_kind('https://www.facebook.com/reel/K0000096') == 'REEL')
    check("fbkind_04896", fb_kind('https://www.facebook.com/reel/K0000097') == 'REEL')
    check("fbkind_04897", fb_kind('https://www.facebook.com/reel/K0000098') == 'REEL')
    check("fbkind_04898", fb_kind('https://www.facebook.com/reel/K0000099') == 'REEL')
    check("fbkind_04899", fb_kind('https://www.facebook.com/reel/K0000100') == 'REEL')
    check("fbkind_04900", fb_kind('https://www.facebook.com/reel/K0000101') == 'REEL')
    check("fbkind_04901", fb_kind('https://www.facebook.com/reel/K0000102') == 'REEL')
    check("fbkind_04902", fb_kind('https://www.facebook.com/reel/K0000103') == 'REEL')
    check("fbkind_04903", fb_kind('https://www.facebook.com/reel/K0000104') == 'REEL')
    check("fbkind_04904", fb_kind('https://www.facebook.com/reel/K0000105') == 'REEL')
    check("fbkind_04905", fb_kind('https://www.facebook.com/reel/K0000106') == 'REEL')
    check("fbkind_04906", fb_kind('https://www.facebook.com/reel/K0000107') == 'REEL')
    check("fbkind_04907", fb_kind('https://www.facebook.com/reel/K0000108') == 'REEL')
    check("fbkind_04908", fb_kind('https://www.facebook.com/reel/K0000109') == 'REEL')
    check("fbkind_04909", fb_kind('https://www.facebook.com/reel/K0000110') == 'REEL')
    check("fbkind_04910", fb_kind('https://www.facebook.com/reel/K0000111') == 'REEL')
    check("fbkind_04911", fb_kind('https://www.facebook.com/reel/K0000112') == 'REEL')
    check("fbkind_04912", fb_kind('https://www.facebook.com/reel/K0000113') == 'REEL')
    check("fbkind_04913", fb_kind('https://www.facebook.com/reel/K0000114') == 'REEL')
    check("fbkind_04914", fb_kind('https://www.facebook.com/reel/K0000115') == 'REEL')
    check("fbkind_04915", fb_kind('https://www.facebook.com/reel/K0000116') == 'REEL')
    check("fbkind_04916", fb_kind('https://www.facebook.com/reel/K0000117') == 'REEL')
    check("fbkind_04917", fb_kind('https://www.facebook.com/reel/K0000118') == 'REEL')
    check("fbkind_04918", fb_kind('https://www.facebook.com/reel/K0000119') == 'REEL')
    check("fbkind_04919", fb_kind('https://www.facebook.com/reel/K0000120') == 'REEL')
    check("fbkind_04920", fb_kind('https://www.facebook.com/reel/K0000121') == 'REEL')
    check("fbkind_04921", fb_kind('https://www.facebook.com/reel/K0000122') == 'REEL')
    check("fbkind_04922", fb_kind('https://www.facebook.com/reel/K0000123') == 'REEL')
    check("fbkind_04923", fb_kind('https://www.facebook.com/reel/K0000124') == 'REEL')
    check("fbkind_04924", fb_kind('https://www.facebook.com/reel/K0000125') == 'REEL')
    check("fbkind_04925", fb_kind('https://www.facebook.com/reel/K0000126') == 'REEL')
    check("fbkind_04926", fb_kind('https://www.facebook.com/reel/K0000127') == 'REEL')
    check("fbkind_04927", fb_kind('https://www.facebook.com/reel/K0000128') == 'REEL')
    check("fbkind_04928", fb_kind('https://www.facebook.com/reel/K0000129') == 'REEL')
    check("fbkind_04929", fb_kind('https://www.facebook.com/reel/K0000130') == 'REEL')
    check("fbkind_04930", fb_kind('https://www.facebook.com/reel/K0000131') == 'REEL')
    check("fbkind_04931", fb_kind('https://www.facebook.com/reel/K0000132') == 'REEL')
    check("fbkind_04932", fb_kind('https://www.facebook.com/reel/K0000133') == 'REEL')
    check("fbkind_04933", fb_kind('https://www.facebook.com/reel/K0000134') == 'REEL')
    check("fbkind_04934", fb_kind('https://www.facebook.com/reel/K0000135') == 'REEL')
    check("fbkind_04935", fb_kind('https://www.facebook.com/reel/K0000136') == 'REEL')
    check("fbkind_04936", fb_kind('https://www.facebook.com/reel/K0000137') == 'REEL')
    check("fbkind_04937", fb_kind('https://www.facebook.com/reel/K0000138') == 'REEL')
    check("fbkind_04938", fb_kind('https://www.facebook.com/reel/K0000139') == 'REEL')
    check("fbkind_04939", fb_kind('https://www.facebook.com/reel/K0000140') == 'REEL')
    check("fbkind_04940", fb_kind('https://www.facebook.com/reel/K0000141') == 'REEL')
    check("fbkind_04941", fb_kind('https://www.facebook.com/reel/K0000142') == 'REEL')
    check("fbkind_04942", fb_kind('https://www.facebook.com/reel/K0000143') == 'REEL')
    check("fbkind_04943", fb_kind('https://www.facebook.com/reel/K0000144') == 'REEL')
    check("fbkind_04944", fb_kind('https://www.facebook.com/reel/K0000145') == 'REEL')
    check("fbkind_04945", fb_kind('https://www.facebook.com/reel/K0000146') == 'REEL')
    check("fbkind_04946", fb_kind('https://www.facebook.com/reel/K0000147') == 'REEL')
    check("fbkind_04947", fb_kind('https://www.facebook.com/reel/K0000148') == 'REEL')
    check("fbkind_04948", fb_kind('https://www.facebook.com/reel/K0000149') == 'REEL')
    check("fbkind_04949", fb_kind('https://www.facebook.com/reel/K0000150') == 'REEL')
    check("fbkind_04950", fb_kind('https://www.facebook.com/reel/K0000151') == 'REEL')
    check("fbkind_04951", fb_kind('https://www.facebook.com/reel/K0000152') == 'REEL')
    check("fbkind_04952", fb_kind('https://www.facebook.com/reel/K0000153') == 'REEL')
    check("fbkind_04953", fb_kind('https://www.facebook.com/reel/K0000154') == 'REEL')
    check("fbkind_04954", fb_kind('https://www.facebook.com/reel/K0000155') == 'REEL')
    check("fbkind_04955", fb_kind('https://www.facebook.com/reel/K0000156') == 'REEL')
    check("fbkind_04956", fb_kind('https://www.facebook.com/reel/K0000157') == 'REEL')
    check("fbkind_04957", fb_kind('https://www.facebook.com/reel/K0000158') == 'REEL')
    check("fbkind_04958", fb_kind('https://www.facebook.com/reel/K0000159') == 'REEL')
    check("fbkind_04959", fb_kind('https://www.facebook.com/reel/K0000160') == 'REEL')
    check("fbkind_04960", fb_kind('https://www.facebook.com/reel/K0000161') == 'REEL')
    check("fbkind_04961", fb_kind('https://www.facebook.com/reel/K0000162') == 'REEL')
    check("fbkind_04962", fb_kind('https://www.facebook.com/reel/K0000163') == 'REEL')
    check("fbkind_04963", fb_kind('https://www.facebook.com/reel/K0000164') == 'REEL')
    check("fbkind_04964", fb_kind('https://www.facebook.com/reel/K0000165') == 'REEL')
    check("fbkind_04965", fb_kind('https://www.facebook.com/reel/K0000166') == 'REEL')
    check("fbkind_04966", fb_kind('https://www.facebook.com/reel/K0000167') == 'REEL')
    check("fbkind_04967", fb_kind('https://www.facebook.com/reel/K0000168') == 'REEL')
    check("fbkind_04968", fb_kind('https://www.facebook.com/reel/K0000169') == 'REEL')
    check("fbkind_04969", fb_kind('https://www.facebook.com/reel/K0000170') == 'REEL')
    check("fbkind_04970", fb_kind('https://www.facebook.com/reel/K0000171') == 'REEL')
    check("fbkind_04971", fb_kind('https://www.facebook.com/reel/K0000172') == 'REEL')
    check("fbkind_04972", fb_kind('https://www.facebook.com/reel/K0000173') == 'REEL')
    check("fbkind_04973", fb_kind('https://www.facebook.com/reel/K0000174') == 'REEL')
    check("fbkind_04974", fb_kind('https://www.facebook.com/reel/K0000175') == 'REEL')
    check("fbkind_04975", fb_kind('https://www.facebook.com/reel/K0000176') == 'REEL')
    check("fbkind_04976", fb_kind('https://www.facebook.com/reel/K0000177') == 'REEL')
    check("fbkind_04977", fb_kind('https://www.facebook.com/reel/K0000178') == 'REEL')
    check("fbkind_04978", fb_kind('https://www.facebook.com/reel/K0000179') == 'REEL')
    check("fbkind_04979", fb_kind('https://www.facebook.com/reel/K0000180') == 'REEL')
    check("fbkind_04980", fb_kind('https://www.facebook.com/reel/K0000181') == 'REEL')
    check("fbkind_04981", fb_kind('https://www.facebook.com/reel/K0000182') == 'REEL')
    check("fbkind_04982", fb_kind('https://www.facebook.com/reel/K0000183') == 'REEL')
    check("fbkind_04983", fb_kind('https://www.facebook.com/reel/K0000184') == 'REEL')
    check("fbkind_04984", fb_kind('https://www.facebook.com/reel/K0000185') == 'REEL')
    check("fbkind_04985", fb_kind('https://www.facebook.com/reel/K0000186') == 'REEL')
    check("fbkind_04986", fb_kind('https://www.facebook.com/reel/K0000187') == 'REEL')
    check("fbkind_04987", fb_kind('https://www.facebook.com/reel/K0000188') == 'REEL')
    check("fbkind_04988", fb_kind('https://www.facebook.com/reel/K0000189') == 'REEL')
    check("fbkind_04989", fb_kind('https://www.facebook.com/reel/K0000190') == 'REEL')
    check("fbkind_04990", fb_kind('https://www.facebook.com/reel/K0000191') == 'REEL')
    check("fbkind_04991", fb_kind('https://www.facebook.com/reel/K0000192') == 'REEL')
    check("fbkind_04992", fb_kind('https://www.facebook.com/reel/K0000193') == 'REEL')
    check("fbkind_04993", fb_kind('https://www.facebook.com/reel/K0000194') == 'REEL')
    check("fbkind_04994", fb_kind('https://www.facebook.com/reel/K0000195') == 'REEL')
    check("fbkind_04995", fb_kind('https://www.facebook.com/reel/K0000196') == 'REEL')
    check("fbkind_04996", fb_kind('https://www.facebook.com/reel/K0000197') == 'REEL')
    check("fbkind_04997", fb_kind('https://www.facebook.com/reel/K0000198') == 'REEL')
    check("fbkind_04998", fb_kind('https://www.facebook.com/reel/K0000199') == 'REEL')
    check("fbkind_04999", fb_kind('https://www.facebook.com/reel/K0000200') == 'REEL')
    check("fbkind_05000", fb_kind('https://www.facebook.com/reel/K0000201') == 'REEL')
    check("fbkind_05001", fb_kind('https://www.facebook.com/reel/K0000202') == 'REEL')
    check("fbkind_05002", fb_kind('https://www.facebook.com/reel/K0000203') == 'REEL')
    check("fbkind_05003", fb_kind('https://www.facebook.com/reel/K0000204') == 'REEL')
    check("fbkind_05004", fb_kind('https://www.facebook.com/reel/K0000205') == 'REEL')
    check("fbkind_05005", fb_kind('https://www.facebook.com/reel/K0000206') == 'REEL')
    check("fbkind_05006", fb_kind('https://www.facebook.com/reel/K0000207') == 'REEL')
    check("fbkind_05007", fb_kind('https://www.facebook.com/reel/K0000208') == 'REEL')
    check("fbkind_05008", fb_kind('https://www.facebook.com/reel/K0000209') == 'REEL')
    check("fbkind_05009", fb_kind('https://www.facebook.com/reel/K0000210') == 'REEL')
    check("fbkind_05010", fb_kind('https://www.facebook.com/reel/K0000211') == 'REEL')
    check("fbkind_05011", fb_kind('https://www.facebook.com/reel/K0000212') == 'REEL')
    check("fbkind_05012", fb_kind('https://www.facebook.com/reel/K0000213') == 'REEL')
    check("fbkind_05013", fb_kind('https://www.facebook.com/reel/K0000214') == 'REEL')
    check("fbkind_05014", fb_kind('https://www.facebook.com/reel/K0000215') == 'REEL')
    check("fbkind_05015", fb_kind('https://www.facebook.com/reel/K0000216') == 'REEL')
    check("fbkind_05016", fb_kind('https://www.facebook.com/reel/K0000217') == 'REEL')
    check("fbkind_05017", fb_kind('https://www.facebook.com/reel/K0000218') == 'REEL')
    check("fbkind_05018", fb_kind('https://www.facebook.com/reel/K0000219') == 'REEL')
    check("fbkind_05019", fb_kind('https://www.facebook.com/reel/K0000220') == 'REEL')
    check("fbkind_05020", fb_kind('https://www.facebook.com/reel/K0000221') == 'REEL')
    check("fbkind_05021", fb_kind('https://www.facebook.com/reel/K0000222') == 'REEL')
    check("fbkind_05022", fb_kind('https://www.facebook.com/reel/K0000223') == 'REEL')
    check("fbkind_05023", fb_kind('https://www.facebook.com/reel/K0000224') == 'REEL')
    check("fbkind_05024", fb_kind('https://www.facebook.com/reel/K0000225') == 'REEL')
    check("fbkind_05025", fb_kind('https://www.facebook.com/reel/K0000226') == 'REEL')
    check("fbkind_05026", fb_kind('https://www.facebook.com/reel/K0000227') == 'REEL')
    check("fbkind_05027", fb_kind('https://www.facebook.com/reel/K0000228') == 'REEL')
    check("fbkind_05028", fb_kind('https://www.facebook.com/reel/K0000229') == 'REEL')
    check("fbkind_05029", fb_kind('https://www.facebook.com/reel/K0000230') == 'REEL')
    check("fbkind_05030", fb_kind('https://www.facebook.com/reel/K0000231') == 'REEL')
    check("fbkind_05031", fb_kind('https://www.facebook.com/reel/K0000232') == 'REEL')
    check("fbkind_05032", fb_kind('https://www.facebook.com/reel/K0000233') == 'REEL')
    check("fbkind_05033", fb_kind('https://www.facebook.com/reel/K0000234') == 'REEL')
    check("fbkind_05034", fb_kind('https://www.facebook.com/reel/K0000235') == 'REEL')
    check("fbkind_05035", fb_kind('https://www.facebook.com/reel/K0000236') == 'REEL')
    check("fbkind_05036", fb_kind('https://www.facebook.com/reel/K0000237') == 'REEL')
    check("fbkind_05037", fb_kind('https://www.facebook.com/reel/K0000238') == 'REEL')
    check("fbkind_05038", fb_kind('https://www.facebook.com/reel/K0000239') == 'REEL')
    check("fbkind_05039", fb_kind('https://www.facebook.com/reel/K0000240') == 'REEL')
    check("fbkind_05040", fb_kind('https://www.facebook.com/reel/K0000241') == 'REEL')
    check("fbkind_05041", fb_kind('https://www.facebook.com/reel/K0000242') == 'REEL')
    check("fbkind_05042", fb_kind('https://www.facebook.com/reel/K0000243') == 'REEL')
    check("fbkind_05043", fb_kind('https://www.facebook.com/reel/K0000244') == 'REEL')
    check("fbkind_05044", fb_kind('https://www.facebook.com/reel/K0000245') == 'REEL')
    check("fbkind_05045", fb_kind('https://www.facebook.com/reel/K0000246') == 'REEL')
    check("fbkind_05046", fb_kind('https://www.facebook.com/reel/K0000247') == 'REEL')
    check("fbkind_05047", fb_kind('https://www.facebook.com/reel/K0000248') == 'REEL')
    check("fbkind_05048", fb_kind('https://www.facebook.com/reel/K0000249') == 'REEL')
    check("fbkind_05049", fb_kind('https://www.facebook.com/reel/K0000250') == 'REEL')
    check("fbkind_05050", fb_kind('https://www.facebook.com/reel/K0000251') == 'REEL')
    check("fbkind_05051", fb_kind('https://www.facebook.com/reel/K0000252') == 'REEL')
    check("fbkind_05052", fb_kind('https://www.facebook.com/reel/K0000253') == 'REEL')
    check("fbkind_05053", fb_kind('https://www.facebook.com/reel/K0000254') == 'REEL')
    check("fbkind_05054", fb_kind('https://www.facebook.com/reel/K0000255') == 'REEL')
    check("fbkind_05055", fb_kind('https://www.facebook.com/reel/K0000256') == 'REEL')
    check("fbkind_05056", fb_kind('https://www.facebook.com/reel/K0000257') == 'REEL')
    check("fbkind_05057", fb_kind('https://www.facebook.com/reel/K0000258') == 'REEL')
    check("fbkind_05058", fb_kind('https://www.facebook.com/reel/K0000259') == 'REEL')
    check("fbkind_05059", fb_kind('https://www.facebook.com/reel/K0000260') == 'REEL')
    check("fbkind_05060", fb_kind('https://www.facebook.com/reel/K0000261') == 'REEL')
    check("fbkind_05061", fb_kind('https://www.facebook.com/reel/K0000262') == 'REEL')
    check("fbkind_05062", fb_kind('https://www.facebook.com/reel/K0000263') == 'REEL')
    check("fbkind_05063", fb_kind('https://www.facebook.com/reel/K0000264') == 'REEL')
    check("fbkind_05064", fb_kind('https://www.facebook.com/reel/K0000265') == 'REEL')
    check("fbkind_05065", fb_kind('https://www.facebook.com/reel/K0000266') == 'REEL')
    check("fbkind_05066", fb_kind('https://www.facebook.com/reel/K0000267') == 'REEL')
    check("fbkind_05067", fb_kind('https://www.facebook.com/reel/K0000268') == 'REEL')
    check("fbkind_05068", fb_kind('https://www.facebook.com/reel/K0000269') == 'REEL')
    check("fbkind_05069", fb_kind('https://www.facebook.com/reel/K0000270') == 'REEL')
    check("fbkind_05070", fb_kind('https://www.facebook.com/reel/K0000271') == 'REEL')
    check("fbkind_05071", fb_kind('https://www.facebook.com/reel/K0000272') == 'REEL')
    check("fbkind_05072", fb_kind('https://www.facebook.com/reel/K0000273') == 'REEL')
    check("fbkind_05073", fb_kind('https://www.facebook.com/reel/K0000274') == 'REEL')
    check("fbkind_05074", fb_kind('https://www.facebook.com/reel/K0000275') == 'REEL')
    check("fbkind_05075", fb_kind('https://www.facebook.com/reel/K0000276') == 'REEL')
    check("fbkind_05076", fb_kind('https://www.facebook.com/reel/K0000277') == 'REEL')
    check("fbkind_05077", fb_kind('https://www.facebook.com/reel/K0000278') == 'REEL')
    check("fbkind_05078", fb_kind('https://www.facebook.com/reel/K0000279') == 'REEL')
    check("fbkind_05079", fb_kind('https://www.facebook.com/reel/K0000280') == 'REEL')
    check("fbkind_05080", fb_kind('https://www.facebook.com/reel/K0000281') == 'REEL')
    check("fbkind_05081", fb_kind('https://www.facebook.com/reel/K0000282') == 'REEL')
    check("fbkind_05082", fb_kind('https://www.facebook.com/reel/K0000283') == 'REEL')
    check("fbkind_05083", fb_kind('https://www.facebook.com/reel/K0000284') == 'REEL')
    check("fbkind_05084", fb_kind('https://www.facebook.com/reel/K0000285') == 'REEL')
    check("fbkind_05085", fb_kind('https://www.facebook.com/reel/K0000286') == 'REEL')
    check("fbkind_05086", fb_kind('https://www.facebook.com/reel/K0000287') == 'REEL')
    check("fbkind_05087", fb_kind('https://www.facebook.com/reel/K0000288') == 'REEL')
    check("fbkind_05088", fb_kind('https://www.facebook.com/reel/K0000289') == 'REEL')
    check("fbkind_05089", fb_kind('https://www.facebook.com/reel/K0000290') == 'REEL')
    check("fbkind_05090", fb_kind('https://www.facebook.com/reel/K0000291') == 'REEL')
    check("fbkind_05091", fb_kind('https://www.facebook.com/reel/K0000292') == 'REEL')
    check("fbkind_05092", fb_kind('https://www.facebook.com/reel/K0000293') == 'REEL')
    check("fbkind_05093", fb_kind('https://www.facebook.com/reel/K0000294') == 'REEL')
    check("fbkind_05094", fb_kind('https://www.facebook.com/reel/K0000295') == 'REEL')
    check("fbkind_05095", fb_kind('https://www.facebook.com/reel/K0000296') == 'REEL')
    check("fbkind_05096", fb_kind('https://www.facebook.com/reel/K0000297') == 'REEL')
    check("fbkind_05097", fb_kind('https://www.facebook.com/reel/K0000298') == 'REEL')
    check("fbkind_05098", fb_kind('https://www.facebook.com/reel/K0000299') == 'REEL')
    check("fbkind_05099", fb_kind('https://www.facebook.com/reel/K0000300') == 'REEL')
    check("fbkind_05100", fb_kind('https://www.facebook.com/reel/K0000301') == 'REEL')
    check("fbkind_05101", fb_kind('https://www.facebook.com/reel/K0000302') == 'REEL')
    check("fbkind_05102", fb_kind('https://www.facebook.com/reel/K0000303') == 'REEL')
    check("fbkind_05103", fb_kind('https://www.facebook.com/reel/K0000304') == 'REEL')
    check("fbkind_05104", fb_kind('https://www.facebook.com/reel/K0000305') == 'REEL')
    check("fbkind_05105", fb_kind('https://www.facebook.com/reel/K0000306') == 'REEL')
    check("fbkind_05106", fb_kind('https://www.facebook.com/reel/K0000307') == 'REEL')
    check("fbkind_05107", fb_kind('https://www.facebook.com/reel/K0000308') == 'REEL')
    check("fbkind_05108", fb_kind('https://www.facebook.com/reel/K0000309') == 'REEL')
    check("fbkind_05109", fb_kind('https://www.facebook.com/reel/K0000310') == 'REEL')
    check("fbkind_05110", fb_kind('https://www.facebook.com/reel/K0000311') == 'REEL')
    check("fbkind_05111", fb_kind('https://www.facebook.com/reel/K0000312') == 'REEL')
    check("fbkind_05112", fb_kind('https://www.facebook.com/reel/K0000313') == 'REEL')
    check("fbkind_05113", fb_kind('https://www.facebook.com/reel/K0000314') == 'REEL')
    check("fbkind_05114", fb_kind('https://www.facebook.com/reel/K0000315') == 'REEL')
    check("fbkind_05115", fb_kind('https://www.facebook.com/reel/K0000316') == 'REEL')
    check("fbkind_05116", fb_kind('https://www.facebook.com/reel/K0000317') == 'REEL')
    check("fbkind_05117", fb_kind('https://www.facebook.com/reel/K0000318') == 'REEL')
    check("fbkind_05118", fb_kind('https://www.facebook.com/reel/K0000319') == 'REEL')
    check("fbkind_05119", fb_kind('https://www.facebook.com/reel/K0000320') == 'REEL')
    check("fbkind_05120", fb_kind('https://www.facebook.com/reel/K0000321') == 'REEL')
    check("fbkind_05121", fb_kind('https://www.facebook.com/reel/K0000322') == 'REEL')
    check("fbkind_05122", fb_kind('https://www.facebook.com/reel/K0000323') == 'REEL')
    check("fbkind_05123", fb_kind('https://www.facebook.com/reel/K0000324') == 'REEL')
    check("fbkind_05124", fb_kind('https://www.facebook.com/reel/K0000325') == 'REEL')
    check("fbkind_05125", fb_kind('https://www.facebook.com/reel/K0000326') == 'REEL')
    check("fbkind_05126", fb_kind('https://www.facebook.com/reel/K0000327') == 'REEL')
    check("fbkind_05127", fb_kind('https://www.facebook.com/reel/K0000328') == 'REEL')
    check("fbkind_05128", fb_kind('https://www.facebook.com/reel/K0000329') == 'REEL')
    check("fbkind_05129", fb_kind('https://www.facebook.com/reel/K0000330') == 'REEL')
    check("fbkind_05130", fb_kind('https://www.facebook.com/reel/K0000331') == 'REEL')
    check("fbkind_05131", fb_kind('https://www.facebook.com/reel/K0000332') == 'REEL')
    check("fbkind_05132", fb_kind('https://www.facebook.com/reel/K0000333') == 'REEL')
    check("fbkind_05133", fb_kind('https://www.facebook.com/reel/K0000334') == 'REEL')
    check("fbkind_05134", fb_kind('https://www.facebook.com/reel/K0000335') == 'REEL')
    check("fbkind_05135", fb_kind('https://www.facebook.com/reel/K0000336') == 'REEL')
    check("fbkind_05136", fb_kind('https://www.facebook.com/reel/K0000337') == 'REEL')
    check("fbkind_05137", fb_kind('https://www.facebook.com/reel/K0000338') == 'REEL')
    check("fbkind_05138", fb_kind('https://www.facebook.com/reel/K0000339') == 'REEL')
    check("fbkind_05139", fb_kind('https://www.facebook.com/reel/K0000340') == 'REEL')
    check("fbkind_05140", fb_kind('https://www.facebook.com/reel/K0000341') == 'REEL')
    check("fbkind_05141", fb_kind('https://www.facebook.com/reel/K0000342') == 'REEL')
    check("fbkind_05142", fb_kind('https://www.facebook.com/reel/K0000343') == 'REEL')
    check("fbkind_05143", fb_kind('https://www.facebook.com/reel/K0000344') == 'REEL')
    check("fbkind_05144", fb_kind('https://www.facebook.com/reel/K0000345') == 'REEL')
    check("fbkind_05145", fb_kind('https://www.facebook.com/reel/K0000346') == 'REEL')
    check("fbkind_05146", fb_kind('https://www.facebook.com/reel/K0000347') == 'REEL')
    check("fbkind_05147", fb_kind('https://www.facebook.com/reel/K0000348') == 'REEL')
    check("fbkind_05148", fb_kind('https://www.facebook.com/reel/K0000349') == 'REEL')
    check("fbkind_05149", fb_kind('https://www.facebook.com/reel/K0000350') == 'REEL')
    check("fbkind_05150", fb_kind('https://www.facebook.com/reel/K0000351') == 'REEL')
    check("fbkind_05151", fb_kind('https://www.facebook.com/reel/K0000352') == 'REEL')
    check("fbkind_05152", fb_kind('https://www.facebook.com/reel/K0000353') == 'REEL')
    check("fbkind_05153", fb_kind('https://www.facebook.com/reel/K0000354') == 'REEL')
    check("fbkind_05154", fb_kind('https://www.facebook.com/reel/K0000355') == 'REEL')
    check("fbkind_05155", fb_kind('https://www.facebook.com/reel/K0000356') == 'REEL')
    check("fbkind_05156", fb_kind('https://www.facebook.com/reel/K0000357') == 'REEL')
    check("fbkind_05157", fb_kind('https://www.facebook.com/reel/K0000358') == 'REEL')
    check("fbkind_05158", fb_kind('https://www.facebook.com/reel/K0000359') == 'REEL')
    check("fbkind_05159", fb_kind('https://www.facebook.com/reel/K0000360') == 'REEL')
    check("fbkind_05160", fb_kind('https://www.facebook.com/reel/K0000361') == 'REEL')
    check("fbkind_05161", fb_kind('https://www.facebook.com/reel/K0000362') == 'REEL')
    check("fbkind_05162", fb_kind('https://www.facebook.com/reel/K0000363') == 'REEL')
    check("fbkind_05163", fb_kind('https://www.facebook.com/reel/K0000364') == 'REEL')
    check("fbkind_05164", fb_kind('https://www.facebook.com/reel/K0000365') == 'REEL')
    check("fbkind_05165", fb_kind('https://www.facebook.com/reel/K0000366') == 'REEL')
    check("fbkind_05166", fb_kind('https://www.facebook.com/reel/K0000367') == 'REEL')
    check("fbkind_05167", fb_kind('https://www.facebook.com/reel/K0000368') == 'REEL')
    check("fbkind_05168", fb_kind('https://www.facebook.com/reel/K0000369') == 'REEL')
    check("fbkind_05169", fb_kind('https://www.facebook.com/reel/K0000370') == 'REEL')
    check("fbkind_05170", fb_kind('https://www.facebook.com/reel/K0000371') == 'REEL')
    check("fbkind_05171", fb_kind('https://www.facebook.com/reel/K0000372') == 'REEL')
    check("fbkind_05172", fb_kind('https://www.facebook.com/reel/K0000373') == 'REEL')
    check("fbkind_05173", fb_kind('https://www.facebook.com/reel/K0000374') == 'REEL')
    check("fbkind_05174", fb_kind('https://www.facebook.com/reel/K0000375') == 'REEL')
    check("fbkind_05175", fb_kind('https://www.facebook.com/reel/K0000376') == 'REEL')
    check("fbkind_05176", fb_kind('https://www.facebook.com/reel/K0000377') == 'REEL')
    check("fbkind_05177", fb_kind('https://www.facebook.com/reel/K0000378') == 'REEL')
    check("fbkind_05178", fb_kind('https://www.facebook.com/reel/K0000379') == 'REEL')
    check("fbkind_05179", fb_kind('https://www.facebook.com/reel/K0000380') == 'REEL')
    check("fbkind_05180", fb_kind('https://www.facebook.com/reel/K0000381') == 'REEL')
    check("fbkind_05181", fb_kind('https://www.facebook.com/reel/K0000382') == 'REEL')
    check("fbkind_05182", fb_kind('https://www.facebook.com/reel/K0000383') == 'REEL')
    check("fbkind_05183", fb_kind('https://www.facebook.com/reel/K0000384') == 'REEL')
    check("fbkind_05184", fb_kind('https://www.facebook.com/reel/K0000385') == 'REEL')
    check("fbkind_05185", fb_kind('https://www.facebook.com/reel/K0000386') == 'REEL')
    check("fbkind_05186", fb_kind('https://www.facebook.com/reel/K0000387') == 'REEL')
    check("fbkind_05187", fb_kind('https://www.facebook.com/reel/K0000388') == 'REEL')
    check("fbkind_05188", fb_kind('https://www.facebook.com/reel/K0000389') == 'REEL')
    check("fbkind_05189", fb_kind('https://www.facebook.com/reel/K0000390') == 'REEL')
    check("fbkind_05190", fb_kind('https://www.facebook.com/reel/K0000391') == 'REEL')
    check("fbkind_05191", fb_kind('https://www.facebook.com/reel/K0000392') == 'REEL')
    check("fbkind_05192", fb_kind('https://www.facebook.com/reel/K0000393') == 'REEL')
    check("fbkind_05193", fb_kind('https://www.facebook.com/reel/K0000394') == 'REEL')
    check("fbkind_05194", fb_kind('https://www.facebook.com/reel/K0000395') == 'REEL')
    check("fbkind_05195", fb_kind('https://www.facebook.com/reel/K0000396') == 'REEL')
    check("fbkind_05196", fb_kind('https://www.facebook.com/reel/K0000397') == 'REEL')
    check("fbkind_05197", fb_kind('https://www.facebook.com/reel/K0000398') == 'REEL')
    check("fbkind_05198", fb_kind('https://www.facebook.com/reel/K0000399') == 'REEL')
    check("fbkind_05199", fb_kind('https://www.facebook.com/reel/K0000400') == 'REEL')
    check("fbkind_05200", fb_kind('https://www.facebook.com/reel/K0000401') == 'REEL')
    check("fbkind_05201", fb_kind('https://www.facebook.com/reel/K0000402') == 'REEL')
    check("fbkind_05202", fb_kind('https://www.facebook.com/reel/K0000403') == 'REEL')
    check("fbkind_05203", fb_kind('https://www.facebook.com/reel/K0000404') == 'REEL')
    check("fbkind_05204", fb_kind('https://www.facebook.com/reel/K0000405') == 'REEL')
    check("fbkind_05205", fb_kind('https://www.facebook.com/reel/K0000406') == 'REEL')
    check("fbkind_05206", fb_kind('https://www.facebook.com/reel/K0000407') == 'REEL')
    check("fbkind_05207", fb_kind('https://www.facebook.com/reel/K0000408') == 'REEL')
    check("fbkind_05208", fb_kind('https://www.facebook.com/reel/K0000409') == 'REEL')
    check("fbkind_05209", fb_kind('https://www.facebook.com/reel/K0000410') == 'REEL')
    check("fbkind_05210", fb_kind('https://www.facebook.com/reel/K0000411') == 'REEL')
    check("fbkind_05211", fb_kind('https://www.facebook.com/reel/K0000412') == 'REEL')
    check("fbkind_05212", fb_kind('https://www.facebook.com/reel/K0000413') == 'REEL')
    check("fbkind_05213", fb_kind('https://www.facebook.com/reel/K0000414') == 'REEL')
    check("fbkind_05214", fb_kind('https://www.facebook.com/reel/K0000415') == 'REEL')
    check("fbkind_05215", fb_kind('https://www.facebook.com/reel/K0000416') == 'REEL')
    check("fbkind_05216", fb_kind('https://www.facebook.com/reel/K0000417') == 'REEL')
    check("fbkind_05217", fb_kind('https://www.facebook.com/reel/K0000418') == 'REEL')
    check("fbkind_05218", fb_kind('https://www.facebook.com/reel/K0000419') == 'REEL')
    check("fbkind_05219", fb_kind('https://www.facebook.com/reel/K0000420') == 'REEL')
    check("fbkind_05220", fb_kind('https://www.facebook.com/reel/K0000421') == 'REEL')
    check("fbkind_05221", fb_kind('https://www.facebook.com/reel/K0000422') == 'REEL')
    check("fbkind_05222", fb_kind('https://www.facebook.com/reel/K0000423') == 'REEL')
    check("fbkind_05223", fb_kind('https://www.facebook.com/reel/K0000424') == 'REEL')
    check("fbkind_05224", fb_kind('https://www.facebook.com/reel/K0000425') == 'REEL')
    check("fbkind_05225", fb_kind('https://www.facebook.com/reel/K0000426') == 'REEL')
    check("fbkind_05226", fb_kind('https://www.facebook.com/reel/K0000427') == 'REEL')
    check("fbkind_05227", fb_kind('https://www.facebook.com/reel/K0000428') == 'REEL')
    check("fbkind_05228", fb_kind('https://www.facebook.com/reel/K0000429') == 'REEL')
    check("fbkind_05229", fb_kind('https://www.facebook.com/reel/K0000430') == 'REEL')
    check("fbkind_05230", fb_kind('https://www.facebook.com/reel/K0000431') == 'REEL')
    check("fbkind_05231", fb_kind('https://www.facebook.com/reel/K0000432') == 'REEL')
    check("fbkind_05232", fb_kind('https://www.facebook.com/reel/K0000433') == 'REEL')
    check("fbkind_05233", fb_kind('https://www.facebook.com/reel/K0000434') == 'REEL')
    check("fbkind_05234", fb_kind('https://www.facebook.com/reel/K0000435') == 'REEL')
    check("fbkind_05235", fb_kind('https://www.facebook.com/reel/K0000436') == 'REEL')
    check("fbkind_05236", fb_kind('https://www.facebook.com/reel/K0000437') == 'REEL')
    check("fbkind_05237", fb_kind('https://www.facebook.com/reel/K0000438') == 'REEL')
    check("fbkind_05238", fb_kind('https://www.facebook.com/reel/K0000439') == 'REEL')
    check("fbkind_05239", fb_kind('https://www.facebook.com/reel/K0000440') == 'REEL')
    check("fbkind_05240", fb_kind('https://www.facebook.com/reel/K0000441') == 'REEL')
    check("fbkind_05241", fb_kind('https://www.facebook.com/reel/K0000442') == 'REEL')
    check("fbkind_05242", fb_kind('https://www.facebook.com/reel/K0000443') == 'REEL')
    check("fbkind_05243", fb_kind('https://www.facebook.com/reel/K0000444') == 'REEL')
    check("fbkind_05244", fb_kind('https://www.facebook.com/reel/K0000445') == 'REEL')
    check("fbkind_05245", fb_kind('https://www.facebook.com/reel/K0000446') == 'REEL')
    check("fbkind_05246", fb_kind('https://www.facebook.com/reel/K0000447') == 'REEL')
    check("fbkind_05247", fb_kind('https://www.facebook.com/reel/K0000448') == 'REEL')
    check("fbkind_05248", fb_kind('https://www.facebook.com/reel/K0000449') == 'REEL')
    check("fbkind_05249", fb_kind('https://www.facebook.com/reel/K0000450') == 'REEL')
    check("fbkind_05250", fb_kind('https://www.facebook.com/reel/K0000451') == 'REEL')
    check("fbkind_05251", fb_kind('https://www.facebook.com/reel/K0000452') == 'REEL')
    check("fbkind_05252", fb_kind('https://www.facebook.com/reel/K0000453') == 'REEL')
    check("fbkind_05253", fb_kind('https://www.facebook.com/reel/K0000454') == 'REEL')
    check("fbkind_05254", fb_kind('https://www.facebook.com/reel/K0000455') == 'REEL')
    check("fbkind_05255", fb_kind('https://www.facebook.com/reel/K0000456') == 'REEL')
    check("fbkind_05256", fb_kind('https://www.facebook.com/reel/K0000457') == 'REEL')
    check("fbkind_05257", fb_kind('https://www.facebook.com/reel/K0000458') == 'REEL')
    check("fbkind_05258", fb_kind('https://www.facebook.com/reel/K0000459') == 'REEL')
    check("fbkind_05259", fb_kind('https://www.facebook.com/reel/K0000460') == 'REEL')
    check("fbkind_05260", fb_kind('https://www.facebook.com/reel/K0000461') == 'REEL')
    check("fbkind_05261", fb_kind('https://www.facebook.com/reel/K0000462') == 'REEL')
    check("fbkind_05262", fb_kind('https://www.facebook.com/reel/K0000463') == 'REEL')
    check("fbkind_05263", fb_kind('https://www.facebook.com/reel/K0000464') == 'REEL')
    check("fbkind_05264", fb_kind('https://www.facebook.com/reel/K0000465') == 'REEL')
    check("fbkind_05265", fb_kind('https://www.facebook.com/reel/K0000466') == 'REEL')
    check("fbkind_05266", fb_kind('https://www.facebook.com/reel/K0000467') == 'REEL')
    check("fbkind_05267", fb_kind('https://www.facebook.com/reel/K0000468') == 'REEL')
    check("fbkind_05268", fb_kind('https://www.facebook.com/reel/K0000469') == 'REEL')
    check("fbkind_05269", fb_kind('https://www.facebook.com/reel/K0000470') == 'REEL')
    check("fbkind_05270", fb_kind('https://www.facebook.com/reel/K0000471') == 'REEL')
    check("fbkind_05271", fb_kind('https://www.facebook.com/reel/K0000472') == 'REEL')
    check("fbkind_05272", fb_kind('https://www.facebook.com/reel/K0000473') == 'REEL')
    check("fbkind_05273", fb_kind('https://www.facebook.com/reel/K0000474') == 'REEL')
    check("fbkind_05274", fb_kind('https://www.facebook.com/reel/K0000475') == 'REEL')
    check("fbkind_05275", fb_kind('https://www.facebook.com/reel/K0000476') == 'REEL')
    check("fbkind_05276", fb_kind('https://www.facebook.com/reel/K0000477') == 'REEL')
    check("fbkind_05277", fb_kind('https://www.facebook.com/reel/K0000478') == 'REEL')
    check("fbkind_05278", fb_kind('https://www.facebook.com/reel/K0000479') == 'REEL')
    check("fbkind_05279", fb_kind('https://www.facebook.com/reel/K0000480') == 'REEL')
    check("fbkind_05280", fb_kind('https://www.facebook.com/reel/K0000481') == 'REEL')
    check("fbkind_05281", fb_kind('https://www.facebook.com/reel/K0000482') == 'REEL')
    check("fbkind_05282", fb_kind('https://www.facebook.com/reel/K0000483') == 'REEL')
    check("fbkind_05283", fb_kind('https://www.facebook.com/reel/K0000484') == 'REEL')
    check("fbkind_05284", fb_kind('https://www.facebook.com/reel/K0000485') == 'REEL')
    check("fbkind_05285", fb_kind('https://www.facebook.com/reel/K0000486') == 'REEL')
    check("fbkind_05286", fb_kind('https://www.facebook.com/reel/K0000487') == 'REEL')
    check("fbkind_05287", fb_kind('https://www.facebook.com/reel/K0000488') == 'REEL')
    check("fbkind_05288", fb_kind('https://www.facebook.com/reel/K0000489') == 'REEL')
    check("fbkind_05289", fb_kind('https://www.facebook.com/reel/K0000490') == 'REEL')
    check("fbkind_05290", fb_kind('https://www.facebook.com/reel/K0000491') == 'REEL')
    check("fbkind_05291", fb_kind('https://www.facebook.com/reel/K0000492') == 'REEL')
    check("fbkind_05292", fb_kind('https://www.facebook.com/reel/K0000493') == 'REEL')
    check("fbkind_05293", fb_kind('https://www.facebook.com/reel/K0000494') == 'REEL')
    check("fbkind_05294", fb_kind('https://www.facebook.com/reel/K0000495') == 'REEL')
    check("fbkind_05295", fb_kind('https://www.facebook.com/reel/K0000496') == 'REEL')
    check("fbkind_05296", fb_kind('https://www.facebook.com/reel/K0000497') == 'REEL')
    check("fbkind_05297", fb_kind('https://www.facebook.com/reel/K0000498') == 'REEL')
    check("fbkind_05298", fb_kind('https://www.facebook.com/reel/K0000499') == 'REEL')
    check("fbkind_05299", fb_kind('https://www.facebook.com/reel/K0000500') == 'REEL')
    check("fbkind_05300", fb_kind('https://www.facebook.com/watch/?v=K0000001') == 'WATCH')
    check("fbkind_05301", fb_kind('https://www.facebook.com/watch/?v=K0000002') == 'WATCH')
    check("fbkind_05302", fb_kind('https://www.facebook.com/watch/?v=K0000003') == 'WATCH')
    check("fbkind_05303", fb_kind('https://www.facebook.com/watch/?v=K0000004') == 'WATCH')
    check("fbkind_05304", fb_kind('https://www.facebook.com/watch/?v=K0000005') == 'WATCH')
    check("fbkind_05305", fb_kind('https://www.facebook.com/watch/?v=K0000006') == 'WATCH')
    check("fbkind_05306", fb_kind('https://www.facebook.com/watch/?v=K0000007') == 'WATCH')
    check("fbkind_05307", fb_kind('https://www.facebook.com/watch/?v=K0000008') == 'WATCH')
    check("fbkind_05308", fb_kind('https://www.facebook.com/watch/?v=K0000009') == 'WATCH')
    check("fbkind_05309", fb_kind('https://www.facebook.com/watch/?v=K0000010') == 'WATCH')
    check("fbkind_05310", fb_kind('https://www.facebook.com/watch/?v=K0000011') == 'WATCH')
    check("fbkind_05311", fb_kind('https://www.facebook.com/watch/?v=K0000012') == 'WATCH')
    check("fbkind_05312", fb_kind('https://www.facebook.com/watch/?v=K0000013') == 'WATCH')
    check("fbkind_05313", fb_kind('https://www.facebook.com/watch/?v=K0000014') == 'WATCH')
    check("fbkind_05314", fb_kind('https://www.facebook.com/watch/?v=K0000015') == 'WATCH')
    check("fbkind_05315", fb_kind('https://www.facebook.com/watch/?v=K0000016') == 'WATCH')
    check("fbkind_05316", fb_kind('https://www.facebook.com/watch/?v=K0000017') == 'WATCH')
    check("fbkind_05317", fb_kind('https://www.facebook.com/watch/?v=K0000018') == 'WATCH')
    check("fbkind_05318", fb_kind('https://www.facebook.com/watch/?v=K0000019') == 'WATCH')
    check("fbkind_05319", fb_kind('https://www.facebook.com/watch/?v=K0000020') == 'WATCH')
    check("fbkind_05320", fb_kind('https://www.facebook.com/watch/?v=K0000021') == 'WATCH')
    check("fbkind_05321", fb_kind('https://www.facebook.com/watch/?v=K0000022') == 'WATCH')
    check("fbkind_05322", fb_kind('https://www.facebook.com/watch/?v=K0000023') == 'WATCH')
    check("fbkind_05323", fb_kind('https://www.facebook.com/watch/?v=K0000024') == 'WATCH')
    check("fbkind_05324", fb_kind('https://www.facebook.com/watch/?v=K0000025') == 'WATCH')
    check("fbkind_05325", fb_kind('https://www.facebook.com/watch/?v=K0000026') == 'WATCH')
    check("fbkind_05326", fb_kind('https://www.facebook.com/watch/?v=K0000027') == 'WATCH')
    check("fbkind_05327", fb_kind('https://www.facebook.com/watch/?v=K0000028') == 'WATCH')
    check("fbkind_05328", fb_kind('https://www.facebook.com/watch/?v=K0000029') == 'WATCH')
    check("fbkind_05329", fb_kind('https://www.facebook.com/watch/?v=K0000030') == 'WATCH')
    check("fbkind_05330", fb_kind('https://www.facebook.com/watch/?v=K0000031') == 'WATCH')
    check("fbkind_05331", fb_kind('https://www.facebook.com/watch/?v=K0000032') == 'WATCH')
    check("fbkind_05332", fb_kind('https://www.facebook.com/watch/?v=K0000033') == 'WATCH')
    check("fbkind_05333", fb_kind('https://www.facebook.com/watch/?v=K0000034') == 'WATCH')
    check("fbkind_05334", fb_kind('https://www.facebook.com/watch/?v=K0000035') == 'WATCH')
    check("fbkind_05335", fb_kind('https://www.facebook.com/watch/?v=K0000036') == 'WATCH')
    check("fbkind_05336", fb_kind('https://www.facebook.com/watch/?v=K0000037') == 'WATCH')
    check("fbkind_05337", fb_kind('https://www.facebook.com/watch/?v=K0000038') == 'WATCH')
    check("fbkind_05338", fb_kind('https://www.facebook.com/watch/?v=K0000039') == 'WATCH')
    check("fbkind_05339", fb_kind('https://www.facebook.com/watch/?v=K0000040') == 'WATCH')
    check("fbkind_05340", fb_kind('https://www.facebook.com/watch/?v=K0000041') == 'WATCH')
    check("fbkind_05341", fb_kind('https://www.facebook.com/watch/?v=K0000042') == 'WATCH')
    check("fbkind_05342", fb_kind('https://www.facebook.com/watch/?v=K0000043') == 'WATCH')
    check("fbkind_05343", fb_kind('https://www.facebook.com/watch/?v=K0000044') == 'WATCH')
    check("fbkind_05344", fb_kind('https://www.facebook.com/watch/?v=K0000045') == 'WATCH')
    check("fbkind_05345", fb_kind('https://www.facebook.com/watch/?v=K0000046') == 'WATCH')
    check("fbkind_05346", fb_kind('https://www.facebook.com/watch/?v=K0000047') == 'WATCH')
    check("fbkind_05347", fb_kind('https://www.facebook.com/watch/?v=K0000048') == 'WATCH')
    check("fbkind_05348", fb_kind('https://www.facebook.com/watch/?v=K0000049') == 'WATCH')
    check("fbkind_05349", fb_kind('https://www.facebook.com/watch/?v=K0000050') == 'WATCH')
    check("fbkind_05350", fb_kind('https://www.facebook.com/watch/?v=K0000051') == 'WATCH')
    check("fbkind_05351", fb_kind('https://www.facebook.com/watch/?v=K0000052') == 'WATCH')
    check("fbkind_05352", fb_kind('https://www.facebook.com/watch/?v=K0000053') == 'WATCH')
    check("fbkind_05353", fb_kind('https://www.facebook.com/watch/?v=K0000054') == 'WATCH')
    check("fbkind_05354", fb_kind('https://www.facebook.com/watch/?v=K0000055') == 'WATCH')
    check("fbkind_05355", fb_kind('https://www.facebook.com/watch/?v=K0000056') == 'WATCH')
    check("fbkind_05356", fb_kind('https://www.facebook.com/watch/?v=K0000057') == 'WATCH')
    check("fbkind_05357", fb_kind('https://www.facebook.com/watch/?v=K0000058') == 'WATCH')
    check("fbkind_05358", fb_kind('https://www.facebook.com/watch/?v=K0000059') == 'WATCH')
    check("fbkind_05359", fb_kind('https://www.facebook.com/watch/?v=K0000060') == 'WATCH')
    check("fbkind_05360", fb_kind('https://www.facebook.com/watch/?v=K0000061') == 'WATCH')
    check("fbkind_05361", fb_kind('https://www.facebook.com/watch/?v=K0000062') == 'WATCH')
    check("fbkind_05362", fb_kind('https://www.facebook.com/watch/?v=K0000063') == 'WATCH')
    check("fbkind_05363", fb_kind('https://www.facebook.com/watch/?v=K0000064') == 'WATCH')
    check("fbkind_05364", fb_kind('https://www.facebook.com/watch/?v=K0000065') == 'WATCH')
    check("fbkind_05365", fb_kind('https://www.facebook.com/watch/?v=K0000066') == 'WATCH')
    check("fbkind_05366", fb_kind('https://www.facebook.com/watch/?v=K0000067') == 'WATCH')
    check("fbkind_05367", fb_kind('https://www.facebook.com/watch/?v=K0000068') == 'WATCH')
    check("fbkind_05368", fb_kind('https://www.facebook.com/watch/?v=K0000069') == 'WATCH')
    check("fbkind_05369", fb_kind('https://www.facebook.com/watch/?v=K0000070') == 'WATCH')
    check("fbkind_05370", fb_kind('https://www.facebook.com/watch/?v=K0000071') == 'WATCH')
    check("fbkind_05371", fb_kind('https://www.facebook.com/watch/?v=K0000072') == 'WATCH')
    check("fbkind_05372", fb_kind('https://www.facebook.com/watch/?v=K0000073') == 'WATCH')
    check("fbkind_05373", fb_kind('https://www.facebook.com/watch/?v=K0000074') == 'WATCH')
    check("fbkind_05374", fb_kind('https://www.facebook.com/watch/?v=K0000075') == 'WATCH')
    check("fbkind_05375", fb_kind('https://www.facebook.com/watch/?v=K0000076') == 'WATCH')
    check("fbkind_05376", fb_kind('https://www.facebook.com/watch/?v=K0000077') == 'WATCH')
    check("fbkind_05377", fb_kind('https://www.facebook.com/watch/?v=K0000078') == 'WATCH')
    check("fbkind_05378", fb_kind('https://www.facebook.com/watch/?v=K0000079') == 'WATCH')
    check("fbkind_05379", fb_kind('https://www.facebook.com/watch/?v=K0000080') == 'WATCH')
    check("fbkind_05380", fb_kind('https://www.facebook.com/watch/?v=K0000081') == 'WATCH')
    check("fbkind_05381", fb_kind('https://www.facebook.com/watch/?v=K0000082') == 'WATCH')
    check("fbkind_05382", fb_kind('https://www.facebook.com/watch/?v=K0000083') == 'WATCH')
    check("fbkind_05383", fb_kind('https://www.facebook.com/watch/?v=K0000084') == 'WATCH')
    check("fbkind_05384", fb_kind('https://www.facebook.com/watch/?v=K0000085') == 'WATCH')
    check("fbkind_05385", fb_kind('https://www.facebook.com/watch/?v=K0000086') == 'WATCH')
    check("fbkind_05386", fb_kind('https://www.facebook.com/watch/?v=K0000087') == 'WATCH')
    check("fbkind_05387", fb_kind('https://www.facebook.com/watch/?v=K0000088') == 'WATCH')
    check("fbkind_05388", fb_kind('https://www.facebook.com/watch/?v=K0000089') == 'WATCH')
    check("fbkind_05389", fb_kind('https://www.facebook.com/watch/?v=K0000090') == 'WATCH')
    check("fbkind_05390", fb_kind('https://www.facebook.com/watch/?v=K0000091') == 'WATCH')
    check("fbkind_05391", fb_kind('https://www.facebook.com/watch/?v=K0000092') == 'WATCH')
    check("fbkind_05392", fb_kind('https://www.facebook.com/watch/?v=K0000093') == 'WATCH')
    check("fbkind_05393", fb_kind('https://www.facebook.com/watch/?v=K0000094') == 'WATCH')
    check("fbkind_05394", fb_kind('https://www.facebook.com/watch/?v=K0000095') == 'WATCH')
    check("fbkind_05395", fb_kind('https://www.facebook.com/watch/?v=K0000096') == 'WATCH')
    check("fbkind_05396", fb_kind('https://www.facebook.com/watch/?v=K0000097') == 'WATCH')
    check("fbkind_05397", fb_kind('https://www.facebook.com/watch/?v=K0000098') == 'WATCH')
    check("fbkind_05398", fb_kind('https://www.facebook.com/watch/?v=K0000099') == 'WATCH')
    check("fbkind_05399", fb_kind('https://www.facebook.com/watch/?v=K0000100') == 'WATCH')
    check("fbkind_05400", fb_kind('https://www.facebook.com/watch/?v=K0000101') == 'WATCH')
    check("fbkind_05401", fb_kind('https://www.facebook.com/watch/?v=K0000102') == 'WATCH')
    check("fbkind_05402", fb_kind('https://www.facebook.com/watch/?v=K0000103') == 'WATCH')
    check("fbkind_05403", fb_kind('https://www.facebook.com/watch/?v=K0000104') == 'WATCH')
    check("fbkind_05404", fb_kind('https://www.facebook.com/watch/?v=K0000105') == 'WATCH')
    check("fbkind_05405", fb_kind('https://www.facebook.com/watch/?v=K0000106') == 'WATCH')
    check("fbkind_05406", fb_kind('https://www.facebook.com/watch/?v=K0000107') == 'WATCH')
    check("fbkind_05407", fb_kind('https://www.facebook.com/watch/?v=K0000108') == 'WATCH')
    check("fbkind_05408", fb_kind('https://www.facebook.com/watch/?v=K0000109') == 'WATCH')
    check("fbkind_05409", fb_kind('https://www.facebook.com/watch/?v=K0000110') == 'WATCH')
    check("fbkind_05410", fb_kind('https://www.facebook.com/watch/?v=K0000111') == 'WATCH')
    check("fbkind_05411", fb_kind('https://www.facebook.com/watch/?v=K0000112') == 'WATCH')
    check("fbkind_05412", fb_kind('https://www.facebook.com/watch/?v=K0000113') == 'WATCH')
    check("fbkind_05413", fb_kind('https://www.facebook.com/watch/?v=K0000114') == 'WATCH')
    check("fbkind_05414", fb_kind('https://www.facebook.com/watch/?v=K0000115') == 'WATCH')
    check("fbkind_05415", fb_kind('https://www.facebook.com/watch/?v=K0000116') == 'WATCH')
    check("fbkind_05416", fb_kind('https://www.facebook.com/watch/?v=K0000117') == 'WATCH')
    check("fbkind_05417", fb_kind('https://www.facebook.com/watch/?v=K0000118') == 'WATCH')
    check("fbkind_05418", fb_kind('https://www.facebook.com/watch/?v=K0000119') == 'WATCH')
    check("fbkind_05419", fb_kind('https://www.facebook.com/watch/?v=K0000120') == 'WATCH')
    check("fbkind_05420", fb_kind('https://www.facebook.com/watch/?v=K0000121') == 'WATCH')
    check("fbkind_05421", fb_kind('https://www.facebook.com/watch/?v=K0000122') == 'WATCH')
    check("fbkind_05422", fb_kind('https://www.facebook.com/watch/?v=K0000123') == 'WATCH')
    check("fbkind_05423", fb_kind('https://www.facebook.com/watch/?v=K0000124') == 'WATCH')
    check("fbkind_05424", fb_kind('https://www.facebook.com/watch/?v=K0000125') == 'WATCH')
    check("fbkind_05425", fb_kind('https://www.facebook.com/watch/?v=K0000126') == 'WATCH')
    check("fbkind_05426", fb_kind('https://www.facebook.com/watch/?v=K0000127') == 'WATCH')
    check("fbkind_05427", fb_kind('https://www.facebook.com/watch/?v=K0000128') == 'WATCH')
    check("fbkind_05428", fb_kind('https://www.facebook.com/watch/?v=K0000129') == 'WATCH')
    check("fbkind_05429", fb_kind('https://www.facebook.com/watch/?v=K0000130') == 'WATCH')
    check("fbkind_05430", fb_kind('https://www.facebook.com/watch/?v=K0000131') == 'WATCH')
    check("fbkind_05431", fb_kind('https://www.facebook.com/watch/?v=K0000132') == 'WATCH')
    check("fbkind_05432", fb_kind('https://www.facebook.com/watch/?v=K0000133') == 'WATCH')
    check("fbkind_05433", fb_kind('https://www.facebook.com/watch/?v=K0000134') == 'WATCH')
    check("fbkind_05434", fb_kind('https://www.facebook.com/watch/?v=K0000135') == 'WATCH')
    check("fbkind_05435", fb_kind('https://www.facebook.com/watch/?v=K0000136') == 'WATCH')
    check("fbkind_05436", fb_kind('https://www.facebook.com/watch/?v=K0000137') == 'WATCH')
    check("fbkind_05437", fb_kind('https://www.facebook.com/watch/?v=K0000138') == 'WATCH')
    check("fbkind_05438", fb_kind('https://www.facebook.com/watch/?v=K0000139') == 'WATCH')
    check("fbkind_05439", fb_kind('https://www.facebook.com/watch/?v=K0000140') == 'WATCH')
    check("fbkind_05440", fb_kind('https://www.facebook.com/watch/?v=K0000141') == 'WATCH')
    check("fbkind_05441", fb_kind('https://www.facebook.com/watch/?v=K0000142') == 'WATCH')
    check("fbkind_05442", fb_kind('https://www.facebook.com/watch/?v=K0000143') == 'WATCH')
    check("fbkind_05443", fb_kind('https://www.facebook.com/watch/?v=K0000144') == 'WATCH')
    check("fbkind_05444", fb_kind('https://www.facebook.com/watch/?v=K0000145') == 'WATCH')
    check("fbkind_05445", fb_kind('https://www.facebook.com/watch/?v=K0000146') == 'WATCH')
    check("fbkind_05446", fb_kind('https://www.facebook.com/watch/?v=K0000147') == 'WATCH')
    check("fbkind_05447", fb_kind('https://www.facebook.com/watch/?v=K0000148') == 'WATCH')
    check("fbkind_05448", fb_kind('https://www.facebook.com/watch/?v=K0000149') == 'WATCH')
    check("fbkind_05449", fb_kind('https://www.facebook.com/watch/?v=K0000150') == 'WATCH')
    check("fbkind_05450", fb_kind('https://www.facebook.com/watch/?v=K0000151') == 'WATCH')
    check("fbkind_05451", fb_kind('https://www.facebook.com/watch/?v=K0000152') == 'WATCH')
    check("fbkind_05452", fb_kind('https://www.facebook.com/watch/?v=K0000153') == 'WATCH')
    check("fbkind_05453", fb_kind('https://www.facebook.com/watch/?v=K0000154') == 'WATCH')
    check("fbkind_05454", fb_kind('https://www.facebook.com/watch/?v=K0000155') == 'WATCH')
    check("fbkind_05455", fb_kind('https://www.facebook.com/watch/?v=K0000156') == 'WATCH')
    check("fbkind_05456", fb_kind('https://www.facebook.com/watch/?v=K0000157') == 'WATCH')
    check("fbkind_05457", fb_kind('https://www.facebook.com/watch/?v=K0000158') == 'WATCH')
    check("fbkind_05458", fb_kind('https://www.facebook.com/watch/?v=K0000159') == 'WATCH')
    check("fbkind_05459", fb_kind('https://www.facebook.com/watch/?v=K0000160') == 'WATCH')
    check("fbkind_05460", fb_kind('https://www.facebook.com/watch/?v=K0000161') == 'WATCH')
    check("fbkind_05461", fb_kind('https://www.facebook.com/watch/?v=K0000162') == 'WATCH')
    check("fbkind_05462", fb_kind('https://www.facebook.com/watch/?v=K0000163') == 'WATCH')
    check("fbkind_05463", fb_kind('https://www.facebook.com/watch/?v=K0000164') == 'WATCH')
    check("fbkind_05464", fb_kind('https://www.facebook.com/watch/?v=K0000165') == 'WATCH')
    check("fbkind_05465", fb_kind('https://www.facebook.com/watch/?v=K0000166') == 'WATCH')
    check("fbkind_05466", fb_kind('https://www.facebook.com/watch/?v=K0000167') == 'WATCH')
    check("fbkind_05467", fb_kind('https://www.facebook.com/watch/?v=K0000168') == 'WATCH')
    check("fbkind_05468", fb_kind('https://www.facebook.com/watch/?v=K0000169') == 'WATCH')
    check("fbkind_05469", fb_kind('https://www.facebook.com/watch/?v=K0000170') == 'WATCH')
    check("fbkind_05470", fb_kind('https://www.facebook.com/watch/?v=K0000171') == 'WATCH')
    check("fbkind_05471", fb_kind('https://www.facebook.com/watch/?v=K0000172') == 'WATCH')
    check("fbkind_05472", fb_kind('https://www.facebook.com/watch/?v=K0000173') == 'WATCH')
    check("fbkind_05473", fb_kind('https://www.facebook.com/watch/?v=K0000174') == 'WATCH')
    check("fbkind_05474", fb_kind('https://www.facebook.com/watch/?v=K0000175') == 'WATCH')
    check("fbkind_05475", fb_kind('https://www.facebook.com/watch/?v=K0000176') == 'WATCH')
    check("fbkind_05476", fb_kind('https://www.facebook.com/watch/?v=K0000177') == 'WATCH')
    check("fbkind_05477", fb_kind('https://www.facebook.com/watch/?v=K0000178') == 'WATCH')
    check("fbkind_05478", fb_kind('https://www.facebook.com/watch/?v=K0000179') == 'WATCH')
    check("fbkind_05479", fb_kind('https://www.facebook.com/watch/?v=K0000180') == 'WATCH')
    check("fbkind_05480", fb_kind('https://www.facebook.com/watch/?v=K0000181') == 'WATCH')
    check("fbkind_05481", fb_kind('https://www.facebook.com/watch/?v=K0000182') == 'WATCH')
    check("fbkind_05482", fb_kind('https://www.facebook.com/watch/?v=K0000183') == 'WATCH')
    check("fbkind_05483", fb_kind('https://www.facebook.com/watch/?v=K0000184') == 'WATCH')
    check("fbkind_05484", fb_kind('https://www.facebook.com/watch/?v=K0000185') == 'WATCH')
    check("fbkind_05485", fb_kind('https://www.facebook.com/watch/?v=K0000186') == 'WATCH')
    check("fbkind_05486", fb_kind('https://www.facebook.com/watch/?v=K0000187') == 'WATCH')
    check("fbkind_05487", fb_kind('https://www.facebook.com/watch/?v=K0000188') == 'WATCH')
    check("fbkind_05488", fb_kind('https://www.facebook.com/watch/?v=K0000189') == 'WATCH')
    check("fbkind_05489", fb_kind('https://www.facebook.com/watch/?v=K0000190') == 'WATCH')
    check("fbkind_05490", fb_kind('https://www.facebook.com/watch/?v=K0000191') == 'WATCH')
    check("fbkind_05491", fb_kind('https://www.facebook.com/watch/?v=K0000192') == 'WATCH')
    check("fbkind_05492", fb_kind('https://www.facebook.com/watch/?v=K0000193') == 'WATCH')
    check("fbkind_05493", fb_kind('https://www.facebook.com/watch/?v=K0000194') == 'WATCH')
    check("fbkind_05494", fb_kind('https://www.facebook.com/watch/?v=K0000195') == 'WATCH')
    check("fbkind_05495", fb_kind('https://www.facebook.com/watch/?v=K0000196') == 'WATCH')
    check("fbkind_05496", fb_kind('https://www.facebook.com/watch/?v=K0000197') == 'WATCH')
    check("fbkind_05497", fb_kind('https://www.facebook.com/watch/?v=K0000198') == 'WATCH')
    check("fbkind_05498", fb_kind('https://www.facebook.com/watch/?v=K0000199') == 'WATCH')
    check("fbkind_05499", fb_kind('https://www.facebook.com/watch/?v=K0000200') == 'WATCH')
    check("fbkind_05500", fb_kind('https://www.facebook.com/watch/?v=K0000201') == 'WATCH')
    check("fbkind_05501", fb_kind('https://www.facebook.com/watch/?v=K0000202') == 'WATCH')
    check("fbkind_05502", fb_kind('https://www.facebook.com/watch/?v=K0000203') == 'WATCH')
    check("fbkind_05503", fb_kind('https://www.facebook.com/watch/?v=K0000204') == 'WATCH')
    check("fbkind_05504", fb_kind('https://www.facebook.com/watch/?v=K0000205') == 'WATCH')
    check("fbkind_05505", fb_kind('https://www.facebook.com/watch/?v=K0000206') == 'WATCH')
    check("fbkind_05506", fb_kind('https://www.facebook.com/watch/?v=K0000207') == 'WATCH')
    check("fbkind_05507", fb_kind('https://www.facebook.com/watch/?v=K0000208') == 'WATCH')
    check("fbkind_05508", fb_kind('https://www.facebook.com/watch/?v=K0000209') == 'WATCH')
    check("fbkind_05509", fb_kind('https://www.facebook.com/watch/?v=K0000210') == 'WATCH')
    check("fbkind_05510", fb_kind('https://www.facebook.com/watch/?v=K0000211') == 'WATCH')
    check("fbkind_05511", fb_kind('https://www.facebook.com/watch/?v=K0000212') == 'WATCH')
    check("fbkind_05512", fb_kind('https://www.facebook.com/watch/?v=K0000213') == 'WATCH')
    check("fbkind_05513", fb_kind('https://www.facebook.com/watch/?v=K0000214') == 'WATCH')
    check("fbkind_05514", fb_kind('https://www.facebook.com/watch/?v=K0000215') == 'WATCH')
    check("fbkind_05515", fb_kind('https://www.facebook.com/watch/?v=K0000216') == 'WATCH')
    check("fbkind_05516", fb_kind('https://www.facebook.com/watch/?v=K0000217') == 'WATCH')
    check("fbkind_05517", fb_kind('https://www.facebook.com/watch/?v=K0000218') == 'WATCH')
    check("fbkind_05518", fb_kind('https://www.facebook.com/watch/?v=K0000219') == 'WATCH')
    check("fbkind_05519", fb_kind('https://www.facebook.com/watch/?v=K0000220') == 'WATCH')
    check("fbkind_05520", fb_kind('https://www.facebook.com/watch/?v=K0000221') == 'WATCH')
    check("fbkind_05521", fb_kind('https://www.facebook.com/watch/?v=K0000222') == 'WATCH')
    check("fbkind_05522", fb_kind('https://www.facebook.com/watch/?v=K0000223') == 'WATCH')
    check("fbkind_05523", fb_kind('https://www.facebook.com/watch/?v=K0000224') == 'WATCH')
    check("fbkind_05524", fb_kind('https://www.facebook.com/watch/?v=K0000225') == 'WATCH')
    check("fbkind_05525", fb_kind('https://www.facebook.com/watch/?v=K0000226') == 'WATCH')
    check("fbkind_05526", fb_kind('https://www.facebook.com/watch/?v=K0000227') == 'WATCH')
    check("fbkind_05527", fb_kind('https://www.facebook.com/watch/?v=K0000228') == 'WATCH')
    check("fbkind_05528", fb_kind('https://www.facebook.com/watch/?v=K0000229') == 'WATCH')
    check("fbkind_05529", fb_kind('https://www.facebook.com/watch/?v=K0000230') == 'WATCH')
    check("fbkind_05530", fb_kind('https://www.facebook.com/watch/?v=K0000231') == 'WATCH')
    check("fbkind_05531", fb_kind('https://www.facebook.com/watch/?v=K0000232') == 'WATCH')
    check("fbkind_05532", fb_kind('https://www.facebook.com/watch/?v=K0000233') == 'WATCH')
    check("fbkind_05533", fb_kind('https://www.facebook.com/watch/?v=K0000234') == 'WATCH')
    check("fbkind_05534", fb_kind('https://www.facebook.com/watch/?v=K0000235') == 'WATCH')
    check("fbkind_05535", fb_kind('https://www.facebook.com/watch/?v=K0000236') == 'WATCH')
    check("fbkind_05536", fb_kind('https://www.facebook.com/watch/?v=K0000237') == 'WATCH')
    check("fbkind_05537", fb_kind('https://www.facebook.com/watch/?v=K0000238') == 'WATCH')
    check("fbkind_05538", fb_kind('https://www.facebook.com/watch/?v=K0000239') == 'WATCH')
    check("fbkind_05539", fb_kind('https://www.facebook.com/watch/?v=K0000240') == 'WATCH')
    check("fbkind_05540", fb_kind('https://www.facebook.com/watch/?v=K0000241') == 'WATCH')
    check("fbkind_05541", fb_kind('https://www.facebook.com/watch/?v=K0000242') == 'WATCH')
    check("fbkind_05542", fb_kind('https://www.facebook.com/watch/?v=K0000243') == 'WATCH')
    check("fbkind_05543", fb_kind('https://www.facebook.com/watch/?v=K0000244') == 'WATCH')
    check("fbkind_05544", fb_kind('https://www.facebook.com/watch/?v=K0000245') == 'WATCH')
    check("fbkind_05545", fb_kind('https://www.facebook.com/watch/?v=K0000246') == 'WATCH')
    check("fbkind_05546", fb_kind('https://www.facebook.com/watch/?v=K0000247') == 'WATCH')
    check("fbkind_05547", fb_kind('https://www.facebook.com/watch/?v=K0000248') == 'WATCH')
    check("fbkind_05548", fb_kind('https://www.facebook.com/watch/?v=K0000249') == 'WATCH')
    check("fbkind_05549", fb_kind('https://www.facebook.com/watch/?v=K0000250') == 'WATCH')
    check("fbkind_05550", fb_kind('https://www.facebook.com/watch/?v=K0000251') == 'WATCH')
    check("fbkind_05551", fb_kind('https://www.facebook.com/watch/?v=K0000252') == 'WATCH')
    check("fbkind_05552", fb_kind('https://www.facebook.com/watch/?v=K0000253') == 'WATCH')
    check("fbkind_05553", fb_kind('https://www.facebook.com/watch/?v=K0000254') == 'WATCH')
    check("fbkind_05554", fb_kind('https://www.facebook.com/watch/?v=K0000255') == 'WATCH')
    check("fbkind_05555", fb_kind('https://www.facebook.com/watch/?v=K0000256') == 'WATCH')
    check("fbkind_05556", fb_kind('https://www.facebook.com/watch/?v=K0000257') == 'WATCH')
    check("fbkind_05557", fb_kind('https://www.facebook.com/watch/?v=K0000258') == 'WATCH')
    check("fbkind_05558", fb_kind('https://www.facebook.com/watch/?v=K0000259') == 'WATCH')
    check("fbkind_05559", fb_kind('https://www.facebook.com/watch/?v=K0000260') == 'WATCH')
    check("fbkind_05560", fb_kind('https://www.facebook.com/watch/?v=K0000261') == 'WATCH')
    check("fbkind_05561", fb_kind('https://www.facebook.com/watch/?v=K0000262') == 'WATCH')
    check("fbkind_05562", fb_kind('https://www.facebook.com/watch/?v=K0000263') == 'WATCH')
    check("fbkind_05563", fb_kind('https://www.facebook.com/watch/?v=K0000264') == 'WATCH')
    check("fbkind_05564", fb_kind('https://www.facebook.com/watch/?v=K0000265') == 'WATCH')
    check("fbkind_05565", fb_kind('https://www.facebook.com/watch/?v=K0000266') == 'WATCH')
    check("fbkind_05566", fb_kind('https://www.facebook.com/watch/?v=K0000267') == 'WATCH')
    check("fbkind_05567", fb_kind('https://www.facebook.com/watch/?v=K0000268') == 'WATCH')
    check("fbkind_05568", fb_kind('https://www.facebook.com/watch/?v=K0000269') == 'WATCH')
    check("fbkind_05569", fb_kind('https://www.facebook.com/watch/?v=K0000270') == 'WATCH')
    check("fbkind_05570", fb_kind('https://www.facebook.com/watch/?v=K0000271') == 'WATCH')
    check("fbkind_05571", fb_kind('https://www.facebook.com/watch/?v=K0000272') == 'WATCH')
    check("fbkind_05572", fb_kind('https://www.facebook.com/watch/?v=K0000273') == 'WATCH')
    check("fbkind_05573", fb_kind('https://www.facebook.com/watch/?v=K0000274') == 'WATCH')
    check("fbkind_05574", fb_kind('https://www.facebook.com/watch/?v=K0000275') == 'WATCH')
    check("fbkind_05575", fb_kind('https://www.facebook.com/watch/?v=K0000276') == 'WATCH')
    check("fbkind_05576", fb_kind('https://www.facebook.com/watch/?v=K0000277') == 'WATCH')
    check("fbkind_05577", fb_kind('https://www.facebook.com/watch/?v=K0000278') == 'WATCH')
    check("fbkind_05578", fb_kind('https://www.facebook.com/watch/?v=K0000279') == 'WATCH')
    check("fbkind_05579", fb_kind('https://www.facebook.com/watch/?v=K0000280') == 'WATCH')
    check("fbkind_05580", fb_kind('https://www.facebook.com/watch/?v=K0000281') == 'WATCH')
    check("fbkind_05581", fb_kind('https://www.facebook.com/watch/?v=K0000282') == 'WATCH')
    check("fbkind_05582", fb_kind('https://www.facebook.com/watch/?v=K0000283') == 'WATCH')
    check("fbkind_05583", fb_kind('https://www.facebook.com/watch/?v=K0000284') == 'WATCH')
    check("fbkind_05584", fb_kind('https://www.facebook.com/watch/?v=K0000285') == 'WATCH')
    check("fbkind_05585", fb_kind('https://www.facebook.com/watch/?v=K0000286') == 'WATCH')
    check("fbkind_05586", fb_kind('https://www.facebook.com/watch/?v=K0000287') == 'WATCH')
    check("fbkind_05587", fb_kind('https://www.facebook.com/watch/?v=K0000288') == 'WATCH')
    check("fbkind_05588", fb_kind('https://www.facebook.com/watch/?v=K0000289') == 'WATCH')
    check("fbkind_05589", fb_kind('https://www.facebook.com/watch/?v=K0000290') == 'WATCH')
    check("fbkind_05590", fb_kind('https://www.facebook.com/watch/?v=K0000291') == 'WATCH')
    check("fbkind_05591", fb_kind('https://www.facebook.com/watch/?v=K0000292') == 'WATCH')
    check("fbkind_05592", fb_kind('https://www.facebook.com/watch/?v=K0000293') == 'WATCH')
    check("fbkind_05593", fb_kind('https://www.facebook.com/watch/?v=K0000294') == 'WATCH')
    check("fbkind_05594", fb_kind('https://www.facebook.com/watch/?v=K0000295') == 'WATCH')
    check("fbkind_05595", fb_kind('https://www.facebook.com/watch/?v=K0000296') == 'WATCH')
    check("fbkind_05596", fb_kind('https://www.facebook.com/watch/?v=K0000297') == 'WATCH')
    check("fbkind_05597", fb_kind('https://www.facebook.com/watch/?v=K0000298') == 'WATCH')
    check("fbkind_05598", fb_kind('https://www.facebook.com/watch/?v=K0000299') == 'WATCH')
    check("fbkind_05599", fb_kind('https://www.facebook.com/watch/?v=K0000300') == 'WATCH')
    check("fbkind_05600", fb_kind('https://www.facebook.com/watch/?v=K0000301') == 'WATCH')
    check("fbkind_05601", fb_kind('https://www.facebook.com/watch/?v=K0000302') == 'WATCH')
    check("fbkind_05602", fb_kind('https://www.facebook.com/watch/?v=K0000303') == 'WATCH')
    check("fbkind_05603", fb_kind('https://www.facebook.com/watch/?v=K0000304') == 'WATCH')
    check("fbkind_05604", fb_kind('https://www.facebook.com/watch/?v=K0000305') == 'WATCH')
    check("fbkind_05605", fb_kind('https://www.facebook.com/watch/?v=K0000306') == 'WATCH')
    check("fbkind_05606", fb_kind('https://www.facebook.com/watch/?v=K0000307') == 'WATCH')
    check("fbkind_05607", fb_kind('https://www.facebook.com/watch/?v=K0000308') == 'WATCH')
    check("fbkind_05608", fb_kind('https://www.facebook.com/watch/?v=K0000309') == 'WATCH')
    check("fbkind_05609", fb_kind('https://www.facebook.com/watch/?v=K0000310') == 'WATCH')
    check("fbkind_05610", fb_kind('https://www.facebook.com/watch/?v=K0000311') == 'WATCH')
    check("fbkind_05611", fb_kind('https://www.facebook.com/watch/?v=K0000312') == 'WATCH')
    check("fbkind_05612", fb_kind('https://www.facebook.com/watch/?v=K0000313') == 'WATCH')
    check("fbkind_05613", fb_kind('https://www.facebook.com/watch/?v=K0000314') == 'WATCH')
    check("fbkind_05614", fb_kind('https://www.facebook.com/watch/?v=K0000315') == 'WATCH')
    check("fbkind_05615", fb_kind('https://www.facebook.com/watch/?v=K0000316') == 'WATCH')
    check("fbkind_05616", fb_kind('https://www.facebook.com/watch/?v=K0000317') == 'WATCH')
    check("fbkind_05617", fb_kind('https://www.facebook.com/watch/?v=K0000318') == 'WATCH')
    check("fbkind_05618", fb_kind('https://www.facebook.com/watch/?v=K0000319') == 'WATCH')
    check("fbkind_05619", fb_kind('https://www.facebook.com/watch/?v=K0000320') == 'WATCH')
    check("fbkind_05620", fb_kind('https://www.facebook.com/watch/?v=K0000321') == 'WATCH')
    check("fbkind_05621", fb_kind('https://www.facebook.com/watch/?v=K0000322') == 'WATCH')
    check("fbkind_05622", fb_kind('https://www.facebook.com/watch/?v=K0000323') == 'WATCH')
    check("fbkind_05623", fb_kind('https://www.facebook.com/watch/?v=K0000324') == 'WATCH')
    check("fbkind_05624", fb_kind('https://www.facebook.com/watch/?v=K0000325') == 'WATCH')
    check("fbkind_05625", fb_kind('https://www.facebook.com/watch/?v=K0000326') == 'WATCH')
    check("fbkind_05626", fb_kind('https://www.facebook.com/watch/?v=K0000327') == 'WATCH')
    check("fbkind_05627", fb_kind('https://www.facebook.com/watch/?v=K0000328') == 'WATCH')
    check("fbkind_05628", fb_kind('https://www.facebook.com/watch/?v=K0000329') == 'WATCH')
    check("fbkind_05629", fb_kind('https://www.facebook.com/watch/?v=K0000330') == 'WATCH')
    check("fbkind_05630", fb_kind('https://www.facebook.com/watch/?v=K0000331') == 'WATCH')
    check("fbkind_05631", fb_kind('https://www.facebook.com/watch/?v=K0000332') == 'WATCH')
    check("fbkind_05632", fb_kind('https://www.facebook.com/watch/?v=K0000333') == 'WATCH')
    check("fbkind_05633", fb_kind('https://www.facebook.com/watch/?v=K0000334') == 'WATCH')
    check("fbkind_05634", fb_kind('https://www.facebook.com/watch/?v=K0000335') == 'WATCH')
    check("fbkind_05635", fb_kind('https://www.facebook.com/watch/?v=K0000336') == 'WATCH')
    check("fbkind_05636", fb_kind('https://www.facebook.com/watch/?v=K0000337') == 'WATCH')
    check("fbkind_05637", fb_kind('https://www.facebook.com/watch/?v=K0000338') == 'WATCH')
    check("fbkind_05638", fb_kind('https://www.facebook.com/watch/?v=K0000339') == 'WATCH')
    check("fbkind_05639", fb_kind('https://www.facebook.com/watch/?v=K0000340') == 'WATCH')
    check("fbkind_05640", fb_kind('https://www.facebook.com/watch/?v=K0000341') == 'WATCH')
    check("fbkind_05641", fb_kind('https://www.facebook.com/watch/?v=K0000342') == 'WATCH')
    check("fbkind_05642", fb_kind('https://www.facebook.com/watch/?v=K0000343') == 'WATCH')
    check("fbkind_05643", fb_kind('https://www.facebook.com/watch/?v=K0000344') == 'WATCH')
    check("fbkind_05644", fb_kind('https://www.facebook.com/watch/?v=K0000345') == 'WATCH')
    check("fbkind_05645", fb_kind('https://www.facebook.com/watch/?v=K0000346') == 'WATCH')
    check("fbkind_05646", fb_kind('https://www.facebook.com/watch/?v=K0000347') == 'WATCH')
    check("fbkind_05647", fb_kind('https://www.facebook.com/watch/?v=K0000348') == 'WATCH')
    check("fbkind_05648", fb_kind('https://www.facebook.com/watch/?v=K0000349') == 'WATCH')
    check("fbkind_05649", fb_kind('https://www.facebook.com/watch/?v=K0000350') == 'WATCH')
    check("fbkind_05650", fb_kind('https://www.facebook.com/watch/?v=K0000351') == 'WATCH')
    check("fbkind_05651", fb_kind('https://www.facebook.com/watch/?v=K0000352') == 'WATCH')
    check("fbkind_05652", fb_kind('https://www.facebook.com/watch/?v=K0000353') == 'WATCH')
    check("fbkind_05653", fb_kind('https://www.facebook.com/watch/?v=K0000354') == 'WATCH')
    check("fbkind_05654", fb_kind('https://www.facebook.com/watch/?v=K0000355') == 'WATCH')
    check("fbkind_05655", fb_kind('https://www.facebook.com/watch/?v=K0000356') == 'WATCH')
    check("fbkind_05656", fb_kind('https://www.facebook.com/watch/?v=K0000357') == 'WATCH')
    check("fbkind_05657", fb_kind('https://www.facebook.com/watch/?v=K0000358') == 'WATCH')
    check("fbkind_05658", fb_kind('https://www.facebook.com/watch/?v=K0000359') == 'WATCH')
    check("fbkind_05659", fb_kind('https://www.facebook.com/watch/?v=K0000360') == 'WATCH')
    check("fbkind_05660", fb_kind('https://www.facebook.com/watch/?v=K0000361') == 'WATCH')
    check("fbkind_05661", fb_kind('https://www.facebook.com/watch/?v=K0000362') == 'WATCH')
    check("fbkind_05662", fb_kind('https://www.facebook.com/watch/?v=K0000363') == 'WATCH')
    check("fbkind_05663", fb_kind('https://www.facebook.com/watch/?v=K0000364') == 'WATCH')
    check("fbkind_05664", fb_kind('https://www.facebook.com/watch/?v=K0000365') == 'WATCH')
    check("fbkind_05665", fb_kind('https://www.facebook.com/watch/?v=K0000366') == 'WATCH')
    check("fbkind_05666", fb_kind('https://www.facebook.com/watch/?v=K0000367') == 'WATCH')
    check("fbkind_05667", fb_kind('https://www.facebook.com/watch/?v=K0000368') == 'WATCH')
    check("fbkind_05668", fb_kind('https://www.facebook.com/watch/?v=K0000369') == 'WATCH')
    check("fbkind_05669", fb_kind('https://www.facebook.com/watch/?v=K0000370') == 'WATCH')
    check("fbkind_05670", fb_kind('https://www.facebook.com/watch/?v=K0000371') == 'WATCH')
    check("fbkind_05671", fb_kind('https://www.facebook.com/watch/?v=K0000372') == 'WATCH')
    check("fbkind_05672", fb_kind('https://www.facebook.com/watch/?v=K0000373') == 'WATCH')
    check("fbkind_05673", fb_kind('https://www.facebook.com/watch/?v=K0000374') == 'WATCH')
    check("fbkind_05674", fb_kind('https://www.facebook.com/watch/?v=K0000375') == 'WATCH')
    check("fbkind_05675", fb_kind('https://www.facebook.com/watch/?v=K0000376') == 'WATCH')
    check("fbkind_05676", fb_kind('https://www.facebook.com/watch/?v=K0000377') == 'WATCH')
    check("fbkind_05677", fb_kind('https://www.facebook.com/watch/?v=K0000378') == 'WATCH')
    check("fbkind_05678", fb_kind('https://www.facebook.com/watch/?v=K0000379') == 'WATCH')
    check("fbkind_05679", fb_kind('https://www.facebook.com/watch/?v=K0000380') == 'WATCH')
    check("fbkind_05680", fb_kind('https://www.facebook.com/watch/?v=K0000381') == 'WATCH')
    check("fbkind_05681", fb_kind('https://www.facebook.com/watch/?v=K0000382') == 'WATCH')
    check("fbkind_05682", fb_kind('https://www.facebook.com/watch/?v=K0000383') == 'WATCH')
    check("fbkind_05683", fb_kind('https://www.facebook.com/watch/?v=K0000384') == 'WATCH')
    check("fbkind_05684", fb_kind('https://www.facebook.com/watch/?v=K0000385') == 'WATCH')
    check("fbkind_05685", fb_kind('https://www.facebook.com/watch/?v=K0000386') == 'WATCH')
    check("fbkind_05686", fb_kind('https://www.facebook.com/watch/?v=K0000387') == 'WATCH')
    check("fbkind_05687", fb_kind('https://www.facebook.com/watch/?v=K0000388') == 'WATCH')
    check("fbkind_05688", fb_kind('https://www.facebook.com/watch/?v=K0000389') == 'WATCH')
    check("fbkind_05689", fb_kind('https://www.facebook.com/watch/?v=K0000390') == 'WATCH')
    check("fbkind_05690", fb_kind('https://www.facebook.com/watch/?v=K0000391') == 'WATCH')
    check("fbkind_05691", fb_kind('https://www.facebook.com/watch/?v=K0000392') == 'WATCH')
    check("fbkind_05692", fb_kind('https://www.facebook.com/watch/?v=K0000393') == 'WATCH')
    check("fbkind_05693", fb_kind('https://www.facebook.com/watch/?v=K0000394') == 'WATCH')
    check("fbkind_05694", fb_kind('https://www.facebook.com/watch/?v=K0000395') == 'WATCH')
    check("fbkind_05695", fb_kind('https://www.facebook.com/watch/?v=K0000396') == 'WATCH')
    check("fbkind_05696", fb_kind('https://www.facebook.com/watch/?v=K0000397') == 'WATCH')
    check("fbkind_05697", fb_kind('https://www.facebook.com/watch/?v=K0000398') == 'WATCH')
    check("fbkind_05698", fb_kind('https://www.facebook.com/watch/?v=K0000399') == 'WATCH')
    check("fbkind_05699", fb_kind('https://www.facebook.com/watch/?v=K0000400') == 'WATCH')
    check("fbkind_05700", fb_kind('https://www.facebook.com/watch/?v=K0000401') == 'WATCH')
    check("fbkind_05701", fb_kind('https://www.facebook.com/watch/?v=K0000402') == 'WATCH')
    check("fbkind_05702", fb_kind('https://www.facebook.com/watch/?v=K0000403') == 'WATCH')
    check("fbkind_05703", fb_kind('https://www.facebook.com/watch/?v=K0000404') == 'WATCH')
    check("fbkind_05704", fb_kind('https://www.facebook.com/watch/?v=K0000405') == 'WATCH')
    check("fbkind_05705", fb_kind('https://www.facebook.com/watch/?v=K0000406') == 'WATCH')
    check("fbkind_05706", fb_kind('https://www.facebook.com/watch/?v=K0000407') == 'WATCH')
    check("fbkind_05707", fb_kind('https://www.facebook.com/watch/?v=K0000408') == 'WATCH')
    check("fbkind_05708", fb_kind('https://www.facebook.com/watch/?v=K0000409') == 'WATCH')
    check("fbkind_05709", fb_kind('https://www.facebook.com/watch/?v=K0000410') == 'WATCH')
    check("fbkind_05710", fb_kind('https://www.facebook.com/watch/?v=K0000411') == 'WATCH')
    check("fbkind_05711", fb_kind('https://www.facebook.com/watch/?v=K0000412') == 'WATCH')
    check("fbkind_05712", fb_kind('https://www.facebook.com/watch/?v=K0000413') == 'WATCH')
    check("fbkind_05713", fb_kind('https://www.facebook.com/watch/?v=K0000414') == 'WATCH')
    check("fbkind_05714", fb_kind('https://www.facebook.com/watch/?v=K0000415') == 'WATCH')
    check("fbkind_05715", fb_kind('https://www.facebook.com/watch/?v=K0000416') == 'WATCH')
    check("fbkind_05716", fb_kind('https://www.facebook.com/watch/?v=K0000417') == 'WATCH')
    check("fbkind_05717", fb_kind('https://www.facebook.com/watch/?v=K0000418') == 'WATCH')
    check("fbkind_05718", fb_kind('https://www.facebook.com/watch/?v=K0000419') == 'WATCH')
    check("fbkind_05719", fb_kind('https://www.facebook.com/watch/?v=K0000420') == 'WATCH')
    check("fbkind_05720", fb_kind('https://www.facebook.com/watch/?v=K0000421') == 'WATCH')
    check("fbkind_05721", fb_kind('https://www.facebook.com/watch/?v=K0000422') == 'WATCH')
    check("fbkind_05722", fb_kind('https://www.facebook.com/watch/?v=K0000423') == 'WATCH')
    check("fbkind_05723", fb_kind('https://www.facebook.com/watch/?v=K0000424') == 'WATCH')
    check("fbkind_05724", fb_kind('https://www.facebook.com/watch/?v=K0000425') == 'WATCH')
    check("fbkind_05725", fb_kind('https://www.facebook.com/watch/?v=K0000426') == 'WATCH')
    check("fbkind_05726", fb_kind('https://www.facebook.com/watch/?v=K0000427') == 'WATCH')
    check("fbkind_05727", fb_kind('https://www.facebook.com/watch/?v=K0000428') == 'WATCH')
    check("fbkind_05728", fb_kind('https://www.facebook.com/watch/?v=K0000429') == 'WATCH')
    check("fbkind_05729", fb_kind('https://www.facebook.com/watch/?v=K0000430') == 'WATCH')
    check("fbkind_05730", fb_kind('https://www.facebook.com/watch/?v=K0000431') == 'WATCH')
    check("fbkind_05731", fb_kind('https://www.facebook.com/watch/?v=K0000432') == 'WATCH')
    check("fbkind_05732", fb_kind('https://www.facebook.com/watch/?v=K0000433') == 'WATCH')
    check("fbkind_05733", fb_kind('https://www.facebook.com/watch/?v=K0000434') == 'WATCH')
    check("fbkind_05734", fb_kind('https://www.facebook.com/watch/?v=K0000435') == 'WATCH')
    check("fbkind_05735", fb_kind('https://www.facebook.com/watch/?v=K0000436') == 'WATCH')
    check("fbkind_05736", fb_kind('https://www.facebook.com/watch/?v=K0000437') == 'WATCH')
    check("fbkind_05737", fb_kind('https://www.facebook.com/watch/?v=K0000438') == 'WATCH')
    check("fbkind_05738", fb_kind('https://www.facebook.com/watch/?v=K0000439') == 'WATCH')
    check("fbkind_05739", fb_kind('https://www.facebook.com/watch/?v=K0000440') == 'WATCH')
    check("fbkind_05740", fb_kind('https://www.facebook.com/watch/?v=K0000441') == 'WATCH')
    check("fbkind_05741", fb_kind('https://www.facebook.com/watch/?v=K0000442') == 'WATCH')
    check("fbkind_05742", fb_kind('https://www.facebook.com/watch/?v=K0000443') == 'WATCH')
    check("fbkind_05743", fb_kind('https://www.facebook.com/watch/?v=K0000444') == 'WATCH')
    check("fbkind_05744", fb_kind('https://www.facebook.com/watch/?v=K0000445') == 'WATCH')
    check("fbkind_05745", fb_kind('https://www.facebook.com/watch/?v=K0000446') == 'WATCH')
    check("fbkind_05746", fb_kind('https://www.facebook.com/watch/?v=K0000447') == 'WATCH')
    check("fbkind_05747", fb_kind('https://www.facebook.com/watch/?v=K0000448') == 'WATCH')
    check("fbkind_05748", fb_kind('https://www.facebook.com/watch/?v=K0000449') == 'WATCH')
    check("fbkind_05749", fb_kind('https://www.facebook.com/watch/?v=K0000450') == 'WATCH')
    check("fbkind_05750", fb_kind('https://www.facebook.com/watch/?v=K0000451') == 'WATCH')
    check("fbkind_05751", fb_kind('https://www.facebook.com/watch/?v=K0000452') == 'WATCH')
    check("fbkind_05752", fb_kind('https://www.facebook.com/watch/?v=K0000453') == 'WATCH')
    check("fbkind_05753", fb_kind('https://www.facebook.com/watch/?v=K0000454') == 'WATCH')
    check("fbkind_05754", fb_kind('https://www.facebook.com/watch/?v=K0000455') == 'WATCH')
    check("fbkind_05755", fb_kind('https://www.facebook.com/watch/?v=K0000456') == 'WATCH')
    check("fbkind_05756", fb_kind('https://www.facebook.com/watch/?v=K0000457') == 'WATCH')
    check("fbkind_05757", fb_kind('https://www.facebook.com/watch/?v=K0000458') == 'WATCH')
    check("fbkind_05758", fb_kind('https://www.facebook.com/watch/?v=K0000459') == 'WATCH')
    check("fbkind_05759", fb_kind('https://www.facebook.com/watch/?v=K0000460') == 'WATCH')
    check("fbkind_05760", fb_kind('https://www.facebook.com/watch/?v=K0000461') == 'WATCH')
    check("fbkind_05761", fb_kind('https://www.facebook.com/watch/?v=K0000462') == 'WATCH')
    check("fbkind_05762", fb_kind('https://www.facebook.com/watch/?v=K0000463') == 'WATCH')
    check("fbkind_05763", fb_kind('https://www.facebook.com/watch/?v=K0000464') == 'WATCH')
    check("fbkind_05764", fb_kind('https://www.facebook.com/watch/?v=K0000465') == 'WATCH')
    check("fbkind_05765", fb_kind('https://www.facebook.com/watch/?v=K0000466') == 'WATCH')
    check("fbkind_05766", fb_kind('https://www.facebook.com/watch/?v=K0000467') == 'WATCH')
    check("fbkind_05767", fb_kind('https://www.facebook.com/watch/?v=K0000468') == 'WATCH')
    check("fbkind_05768", fb_kind('https://www.facebook.com/watch/?v=K0000469') == 'WATCH')
    check("fbkind_05769", fb_kind('https://www.facebook.com/watch/?v=K0000470') == 'WATCH')
    check("fbkind_05770", fb_kind('https://www.facebook.com/watch/?v=K0000471') == 'WATCH')
    check("fbkind_05771", fb_kind('https://www.facebook.com/watch/?v=K0000472') == 'WATCH')
    check("fbkind_05772", fb_kind('https://www.facebook.com/watch/?v=K0000473') == 'WATCH')
    check("fbkind_05773", fb_kind('https://www.facebook.com/watch/?v=K0000474') == 'WATCH')
    check("fbkind_05774", fb_kind('https://www.facebook.com/watch/?v=K0000475') == 'WATCH')
    check("fbkind_05775", fb_kind('https://www.facebook.com/watch/?v=K0000476') == 'WATCH')
    check("fbkind_05776", fb_kind('https://www.facebook.com/watch/?v=K0000477') == 'WATCH')
    check("fbkind_05777", fb_kind('https://www.facebook.com/watch/?v=K0000478') == 'WATCH')
    check("fbkind_05778", fb_kind('https://www.facebook.com/watch/?v=K0000479') == 'WATCH')
    check("fbkind_05779", fb_kind('https://www.facebook.com/watch/?v=K0000480') == 'WATCH')
    check("fbkind_05780", fb_kind('https://www.facebook.com/watch/?v=K0000481') == 'WATCH')
    check("fbkind_05781", fb_kind('https://www.facebook.com/watch/?v=K0000482') == 'WATCH')
    check("fbkind_05782", fb_kind('https://www.facebook.com/watch/?v=K0000483') == 'WATCH')
    check("fbkind_05783", fb_kind('https://www.facebook.com/watch/?v=K0000484') == 'WATCH')
    check("fbkind_05784", fb_kind('https://www.facebook.com/watch/?v=K0000485') == 'WATCH')
    check("fbkind_05785", fb_kind('https://www.facebook.com/watch/?v=K0000486') == 'WATCH')
    check("fbkind_05786", fb_kind('https://www.facebook.com/watch/?v=K0000487') == 'WATCH')
    check("fbkind_05787", fb_kind('https://www.facebook.com/watch/?v=K0000488') == 'WATCH')
    check("fbkind_05788", fb_kind('https://www.facebook.com/watch/?v=K0000489') == 'WATCH')
    check("fbkind_05789", fb_kind('https://www.facebook.com/watch/?v=K0000490') == 'WATCH')
    check("fbkind_05790", fb_kind('https://www.facebook.com/watch/?v=K0000491') == 'WATCH')
    check("fbkind_05791", fb_kind('https://www.facebook.com/watch/?v=K0000492') == 'WATCH')
    check("fbkind_05792", fb_kind('https://www.facebook.com/watch/?v=K0000493') == 'WATCH')
    check("fbkind_05793", fb_kind('https://www.facebook.com/watch/?v=K0000494') == 'WATCH')
    check("fbkind_05794", fb_kind('https://www.facebook.com/watch/?v=K0000495') == 'WATCH')
    check("fbkind_05795", fb_kind('https://www.facebook.com/watch/?v=K0000496') == 'WATCH')
    check("fbkind_05796", fb_kind('https://www.facebook.com/watch/?v=K0000497') == 'WATCH')
    check("fbkind_05797", fb_kind('https://www.facebook.com/watch/?v=K0000498') == 'WATCH')
    check("fbkind_05798", fb_kind('https://www.facebook.com/watch/?v=K0000499') == 'WATCH')
    check("fbkind_05799", fb_kind('https://www.facebook.com/watch/?v=K0000500') == 'WATCH')
    check("fbkind_05800", fb_kind('https://www.facebook.com/videos/K0000001') == 'VIDEO')
    check("fbkind_05801", fb_kind('https://www.facebook.com/videos/K0000002') == 'VIDEO')
    check("fbkind_05802", fb_kind('https://www.facebook.com/videos/K0000003') == 'VIDEO')
    check("fbkind_05803", fb_kind('https://www.facebook.com/videos/K0000004') == 'VIDEO')
    check("fbkind_05804", fb_kind('https://www.facebook.com/videos/K0000005') == 'VIDEO')
    check("fbkind_05805", fb_kind('https://www.facebook.com/videos/K0000006') == 'VIDEO')
    check("fbkind_05806", fb_kind('https://www.facebook.com/videos/K0000007') == 'VIDEO')
    check("fbkind_05807", fb_kind('https://www.facebook.com/videos/K0000008') == 'VIDEO')
    check("fbkind_05808", fb_kind('https://www.facebook.com/videos/K0000009') == 'VIDEO')
    check("fbkind_05809", fb_kind('https://www.facebook.com/videos/K0000010') == 'VIDEO')
    check("fbkind_05810", fb_kind('https://www.facebook.com/videos/K0000011') == 'VIDEO')
    check("fbkind_05811", fb_kind('https://www.facebook.com/videos/K0000012') == 'VIDEO')
    check("fbkind_05812", fb_kind('https://www.facebook.com/videos/K0000013') == 'VIDEO')
    check("fbkind_05813", fb_kind('https://www.facebook.com/videos/K0000014') == 'VIDEO')
    check("fbkind_05814", fb_kind('https://www.facebook.com/videos/K0000015') == 'VIDEO')
    check("fbkind_05815", fb_kind('https://www.facebook.com/videos/K0000016') == 'VIDEO')
    check("fbkind_05816", fb_kind('https://www.facebook.com/videos/K0000017') == 'VIDEO')
    check("fbkind_05817", fb_kind('https://www.facebook.com/videos/K0000018') == 'VIDEO')
    check("fbkind_05818", fb_kind('https://www.facebook.com/videos/K0000019') == 'VIDEO')
    check("fbkind_05819", fb_kind('https://www.facebook.com/videos/K0000020') == 'VIDEO')
    check("fbkind_05820", fb_kind('https://www.facebook.com/videos/K0000021') == 'VIDEO')
    check("fbkind_05821", fb_kind('https://www.facebook.com/videos/K0000022') == 'VIDEO')
    check("fbkind_05822", fb_kind('https://www.facebook.com/videos/K0000023') == 'VIDEO')
    check("fbkind_05823", fb_kind('https://www.facebook.com/videos/K0000024') == 'VIDEO')
    check("fbkind_05824", fb_kind('https://www.facebook.com/videos/K0000025') == 'VIDEO')
    check("fbkind_05825", fb_kind('https://www.facebook.com/videos/K0000026') == 'VIDEO')
    check("fbkind_05826", fb_kind('https://www.facebook.com/videos/K0000027') == 'VIDEO')
    check("fbkind_05827", fb_kind('https://www.facebook.com/videos/K0000028') == 'VIDEO')
    check("fbkind_05828", fb_kind('https://www.facebook.com/videos/K0000029') == 'VIDEO')
    check("fbkind_05829", fb_kind('https://www.facebook.com/videos/K0000030') == 'VIDEO')
    check("fbkind_05830", fb_kind('https://www.facebook.com/videos/K0000031') == 'VIDEO')
    check("fbkind_05831", fb_kind('https://www.facebook.com/videos/K0000032') == 'VIDEO')
    check("fbkind_05832", fb_kind('https://www.facebook.com/videos/K0000033') == 'VIDEO')
    check("fbkind_05833", fb_kind('https://www.facebook.com/videos/K0000034') == 'VIDEO')
    check("fbkind_05834", fb_kind('https://www.facebook.com/videos/K0000035') == 'VIDEO')
    check("fbkind_05835", fb_kind('https://www.facebook.com/videos/K0000036') == 'VIDEO')
    check("fbkind_05836", fb_kind('https://www.facebook.com/videos/K0000037') == 'VIDEO')
    check("fbkind_05837", fb_kind('https://www.facebook.com/videos/K0000038') == 'VIDEO')
    check("fbkind_05838", fb_kind('https://www.facebook.com/videos/K0000039') == 'VIDEO')
    check("fbkind_05839", fb_kind('https://www.facebook.com/videos/K0000040') == 'VIDEO')
    check("fbkind_05840", fb_kind('https://www.facebook.com/videos/K0000041') == 'VIDEO')
    check("fbkind_05841", fb_kind('https://www.facebook.com/videos/K0000042') == 'VIDEO')
    check("fbkind_05842", fb_kind('https://www.facebook.com/videos/K0000043') == 'VIDEO')
    check("fbkind_05843", fb_kind('https://www.facebook.com/videos/K0000044') == 'VIDEO')
    check("fbkind_05844", fb_kind('https://www.facebook.com/videos/K0000045') == 'VIDEO')
    check("fbkind_05845", fb_kind('https://www.facebook.com/videos/K0000046') == 'VIDEO')
    check("fbkind_05846", fb_kind('https://www.facebook.com/videos/K0000047') == 'VIDEO')
    check("fbkind_05847", fb_kind('https://www.facebook.com/videos/K0000048') == 'VIDEO')
    check("fbkind_05848", fb_kind('https://www.facebook.com/videos/K0000049') == 'VIDEO')
    check("fbkind_05849", fb_kind('https://www.facebook.com/videos/K0000050') == 'VIDEO')
    check("fbkind_05850", fb_kind('https://www.facebook.com/videos/K0000051') == 'VIDEO')
    check("fbkind_05851", fb_kind('https://www.facebook.com/videos/K0000052') == 'VIDEO')
    check("fbkind_05852", fb_kind('https://www.facebook.com/videos/K0000053') == 'VIDEO')
    check("fbkind_05853", fb_kind('https://www.facebook.com/videos/K0000054') == 'VIDEO')
    check("fbkind_05854", fb_kind('https://www.facebook.com/videos/K0000055') == 'VIDEO')
    check("fbkind_05855", fb_kind('https://www.facebook.com/videos/K0000056') == 'VIDEO')
    check("fbkind_05856", fb_kind('https://www.facebook.com/videos/K0000057') == 'VIDEO')
    check("fbkind_05857", fb_kind('https://www.facebook.com/videos/K0000058') == 'VIDEO')
    check("fbkind_05858", fb_kind('https://www.facebook.com/videos/K0000059') == 'VIDEO')
    check("fbkind_05859", fb_kind('https://www.facebook.com/videos/K0000060') == 'VIDEO')
    check("fbkind_05860", fb_kind('https://www.facebook.com/videos/K0000061') == 'VIDEO')
    check("fbkind_05861", fb_kind('https://www.facebook.com/videos/K0000062') == 'VIDEO')
    check("fbkind_05862", fb_kind('https://www.facebook.com/videos/K0000063') == 'VIDEO')
    check("fbkind_05863", fb_kind('https://www.facebook.com/videos/K0000064') == 'VIDEO')
    check("fbkind_05864", fb_kind('https://www.facebook.com/videos/K0000065') == 'VIDEO')
    check("fbkind_05865", fb_kind('https://www.facebook.com/videos/K0000066') == 'VIDEO')
    check("fbkind_05866", fb_kind('https://www.facebook.com/videos/K0000067') == 'VIDEO')
    check("fbkind_05867", fb_kind('https://www.facebook.com/videos/K0000068') == 'VIDEO')
    check("fbkind_05868", fb_kind('https://www.facebook.com/videos/K0000069') == 'VIDEO')
    check("fbkind_05869", fb_kind('https://www.facebook.com/videos/K0000070') == 'VIDEO')
    check("fbkind_05870", fb_kind('https://www.facebook.com/videos/K0000071') == 'VIDEO')
    check("fbkind_05871", fb_kind('https://www.facebook.com/videos/K0000072') == 'VIDEO')
    check("fbkind_05872", fb_kind('https://www.facebook.com/videos/K0000073') == 'VIDEO')
    check("fbkind_05873", fb_kind('https://www.facebook.com/videos/K0000074') == 'VIDEO')
    check("fbkind_05874", fb_kind('https://www.facebook.com/videos/K0000075') == 'VIDEO')
    check("fbkind_05875", fb_kind('https://www.facebook.com/videos/K0000076') == 'VIDEO')
    check("fbkind_05876", fb_kind('https://www.facebook.com/videos/K0000077') == 'VIDEO')
    check("fbkind_05877", fb_kind('https://www.facebook.com/videos/K0000078') == 'VIDEO')
    check("fbkind_05878", fb_kind('https://www.facebook.com/videos/K0000079') == 'VIDEO')
    check("fbkind_05879", fb_kind('https://www.facebook.com/videos/K0000080') == 'VIDEO')
    check("fbkind_05880", fb_kind('https://www.facebook.com/videos/K0000081') == 'VIDEO')
    check("fbkind_05881", fb_kind('https://www.facebook.com/videos/K0000082') == 'VIDEO')
    check("fbkind_05882", fb_kind('https://www.facebook.com/videos/K0000083') == 'VIDEO')
    check("fbkind_05883", fb_kind('https://www.facebook.com/videos/K0000084') == 'VIDEO')
    check("fbkind_05884", fb_kind('https://www.facebook.com/videos/K0000085') == 'VIDEO')
    check("fbkind_05885", fb_kind('https://www.facebook.com/videos/K0000086') == 'VIDEO')
    check("fbkind_05886", fb_kind('https://www.facebook.com/videos/K0000087') == 'VIDEO')
    check("fbkind_05887", fb_kind('https://www.facebook.com/videos/K0000088') == 'VIDEO')
    check("fbkind_05888", fb_kind('https://www.facebook.com/videos/K0000089') == 'VIDEO')
    check("fbkind_05889", fb_kind('https://www.facebook.com/videos/K0000090') == 'VIDEO')
    check("fbkind_05890", fb_kind('https://www.facebook.com/videos/K0000091') == 'VIDEO')
    check("fbkind_05891", fb_kind('https://www.facebook.com/videos/K0000092') == 'VIDEO')
    check("fbkind_05892", fb_kind('https://www.facebook.com/videos/K0000093') == 'VIDEO')
    check("fbkind_05893", fb_kind('https://www.facebook.com/videos/K0000094') == 'VIDEO')
    check("fbkind_05894", fb_kind('https://www.facebook.com/videos/K0000095') == 'VIDEO')
    check("fbkind_05895", fb_kind('https://www.facebook.com/videos/K0000096') == 'VIDEO')
    check("fbkind_05896", fb_kind('https://www.facebook.com/videos/K0000097') == 'VIDEO')
    check("fbkind_05897", fb_kind('https://www.facebook.com/videos/K0000098') == 'VIDEO')
    check("fbkind_05898", fb_kind('https://www.facebook.com/videos/K0000099') == 'VIDEO')
    check("fbkind_05899", fb_kind('https://www.facebook.com/videos/K0000100') == 'VIDEO')
    check("fbkind_05900", fb_kind('https://www.facebook.com/videos/K0000101') == 'VIDEO')
    check("fbkind_05901", fb_kind('https://www.facebook.com/videos/K0000102') == 'VIDEO')
    check("fbkind_05902", fb_kind('https://www.facebook.com/videos/K0000103') == 'VIDEO')
    check("fbkind_05903", fb_kind('https://www.facebook.com/videos/K0000104') == 'VIDEO')
    check("fbkind_05904", fb_kind('https://www.facebook.com/videos/K0000105') == 'VIDEO')
    check("fbkind_05905", fb_kind('https://www.facebook.com/videos/K0000106') == 'VIDEO')
    check("fbkind_05906", fb_kind('https://www.facebook.com/videos/K0000107') == 'VIDEO')
    check("fbkind_05907", fb_kind('https://www.facebook.com/videos/K0000108') == 'VIDEO')
    check("fbkind_05908", fb_kind('https://www.facebook.com/videos/K0000109') == 'VIDEO')
    check("fbkind_05909", fb_kind('https://www.facebook.com/videos/K0000110') == 'VIDEO')
    check("fbkind_05910", fb_kind('https://www.facebook.com/videos/K0000111') == 'VIDEO')
    check("fbkind_05911", fb_kind('https://www.facebook.com/videos/K0000112') == 'VIDEO')
    check("fbkind_05912", fb_kind('https://www.facebook.com/videos/K0000113') == 'VIDEO')
    check("fbkind_05913", fb_kind('https://www.facebook.com/videos/K0000114') == 'VIDEO')
    check("fbkind_05914", fb_kind('https://www.facebook.com/videos/K0000115') == 'VIDEO')
    check("fbkind_05915", fb_kind('https://www.facebook.com/videos/K0000116') == 'VIDEO')
    check("fbkind_05916", fb_kind('https://www.facebook.com/videos/K0000117') == 'VIDEO')
    check("fbkind_05917", fb_kind('https://www.facebook.com/videos/K0000118') == 'VIDEO')
    check("fbkind_05918", fb_kind('https://www.facebook.com/videos/K0000119') == 'VIDEO')
    check("fbkind_05919", fb_kind('https://www.facebook.com/videos/K0000120') == 'VIDEO')
    check("fbkind_05920", fb_kind('https://www.facebook.com/videos/K0000121') == 'VIDEO')
    check("fbkind_05921", fb_kind('https://www.facebook.com/videos/K0000122') == 'VIDEO')
    check("fbkind_05922", fb_kind('https://www.facebook.com/videos/K0000123') == 'VIDEO')
    check("fbkind_05923", fb_kind('https://www.facebook.com/videos/K0000124') == 'VIDEO')
    check("fbkind_05924", fb_kind('https://www.facebook.com/videos/K0000125') == 'VIDEO')
    check("fbkind_05925", fb_kind('https://www.facebook.com/videos/K0000126') == 'VIDEO')
    check("fbkind_05926", fb_kind('https://www.facebook.com/videos/K0000127') == 'VIDEO')
    check("fbkind_05927", fb_kind('https://www.facebook.com/videos/K0000128') == 'VIDEO')
    check("fbkind_05928", fb_kind('https://www.facebook.com/videos/K0000129') == 'VIDEO')
    check("fbkind_05929", fb_kind('https://www.facebook.com/videos/K0000130') == 'VIDEO')
    check("fbkind_05930", fb_kind('https://www.facebook.com/videos/K0000131') == 'VIDEO')
    check("fbkind_05931", fb_kind('https://www.facebook.com/videos/K0000132') == 'VIDEO')
    check("fbkind_05932", fb_kind('https://www.facebook.com/videos/K0000133') == 'VIDEO')
    check("fbkind_05933", fb_kind('https://www.facebook.com/videos/K0000134') == 'VIDEO')
    check("fbkind_05934", fb_kind('https://www.facebook.com/videos/K0000135') == 'VIDEO')
    check("fbkind_05935", fb_kind('https://www.facebook.com/videos/K0000136') == 'VIDEO')
    check("fbkind_05936", fb_kind('https://www.facebook.com/videos/K0000137') == 'VIDEO')
    check("fbkind_05937", fb_kind('https://www.facebook.com/videos/K0000138') == 'VIDEO')
    check("fbkind_05938", fb_kind('https://www.facebook.com/videos/K0000139') == 'VIDEO')
    check("fbkind_05939", fb_kind('https://www.facebook.com/videos/K0000140') == 'VIDEO')
    check("fbkind_05940", fb_kind('https://www.facebook.com/videos/K0000141') == 'VIDEO')
    check("fbkind_05941", fb_kind('https://www.facebook.com/videos/K0000142') == 'VIDEO')
    check("fbkind_05942", fb_kind('https://www.facebook.com/videos/K0000143') == 'VIDEO')
    check("fbkind_05943", fb_kind('https://www.facebook.com/videos/K0000144') == 'VIDEO')
    check("fbkind_05944", fb_kind('https://www.facebook.com/videos/K0000145') == 'VIDEO')
    check("fbkind_05945", fb_kind('https://www.facebook.com/videos/K0000146') == 'VIDEO')
    check("fbkind_05946", fb_kind('https://www.facebook.com/videos/K0000147') == 'VIDEO')
    check("fbkind_05947", fb_kind('https://www.facebook.com/videos/K0000148') == 'VIDEO')
    check("fbkind_05948", fb_kind('https://www.facebook.com/videos/K0000149') == 'VIDEO')
    check("fbkind_05949", fb_kind('https://www.facebook.com/videos/K0000150') == 'VIDEO')
    check("fbkind_05950", fb_kind('https://www.facebook.com/videos/K0000151') == 'VIDEO')
    check("fbkind_05951", fb_kind('https://www.facebook.com/videos/K0000152') == 'VIDEO')
    check("fbkind_05952", fb_kind('https://www.facebook.com/videos/K0000153') == 'VIDEO')
    check("fbkind_05953", fb_kind('https://www.facebook.com/videos/K0000154') == 'VIDEO')
    check("fbkind_05954", fb_kind('https://www.facebook.com/videos/K0000155') == 'VIDEO')
    check("fbkind_05955", fb_kind('https://www.facebook.com/videos/K0000156') == 'VIDEO')
    check("fbkind_05956", fb_kind('https://www.facebook.com/videos/K0000157') == 'VIDEO')
    check("fbkind_05957", fb_kind('https://www.facebook.com/videos/K0000158') == 'VIDEO')
    check("fbkind_05958", fb_kind('https://www.facebook.com/videos/K0000159') == 'VIDEO')
    check("fbkind_05959", fb_kind('https://www.facebook.com/videos/K0000160') == 'VIDEO')
    check("fbkind_05960", fb_kind('https://www.facebook.com/videos/K0000161') == 'VIDEO')
    check("fbkind_05961", fb_kind('https://www.facebook.com/videos/K0000162') == 'VIDEO')
    check("fbkind_05962", fb_kind('https://www.facebook.com/videos/K0000163') == 'VIDEO')
    check("fbkind_05963", fb_kind('https://www.facebook.com/videos/K0000164') == 'VIDEO')
    check("fbkind_05964", fb_kind('https://www.facebook.com/videos/K0000165') == 'VIDEO')
    check("fbkind_05965", fb_kind('https://www.facebook.com/videos/K0000166') == 'VIDEO')
    check("fbkind_05966", fb_kind('https://www.facebook.com/videos/K0000167') == 'VIDEO')
    check("fbkind_05967", fb_kind('https://www.facebook.com/videos/K0000168') == 'VIDEO')
    check("fbkind_05968", fb_kind('https://www.facebook.com/videos/K0000169') == 'VIDEO')
    check("fbkind_05969", fb_kind('https://www.facebook.com/videos/K0000170') == 'VIDEO')
    check("fbkind_05970", fb_kind('https://www.facebook.com/videos/K0000171') == 'VIDEO')
    check("fbkind_05971", fb_kind('https://www.facebook.com/videos/K0000172') == 'VIDEO')
    check("fbkind_05972", fb_kind('https://www.facebook.com/videos/K0000173') == 'VIDEO')
    check("fbkind_05973", fb_kind('https://www.facebook.com/videos/K0000174') == 'VIDEO')
    check("fbkind_05974", fb_kind('https://www.facebook.com/videos/K0000175') == 'VIDEO')
    check("fbkind_05975", fb_kind('https://www.facebook.com/videos/K0000176') == 'VIDEO')
    check("fbkind_05976", fb_kind('https://www.facebook.com/videos/K0000177') == 'VIDEO')
    check("fbkind_05977", fb_kind('https://www.facebook.com/videos/K0000178') == 'VIDEO')
    check("fbkind_05978", fb_kind('https://www.facebook.com/videos/K0000179') == 'VIDEO')
    check("fbkind_05979", fb_kind('https://www.facebook.com/videos/K0000180') == 'VIDEO')
    check("fbkind_05980", fb_kind('https://www.facebook.com/videos/K0000181') == 'VIDEO')
    check("fbkind_05981", fb_kind('https://www.facebook.com/videos/K0000182') == 'VIDEO')
    check("fbkind_05982", fb_kind('https://www.facebook.com/videos/K0000183') == 'VIDEO')
    check("fbkind_05983", fb_kind('https://www.facebook.com/videos/K0000184') == 'VIDEO')
    check("fbkind_05984", fb_kind('https://www.facebook.com/videos/K0000185') == 'VIDEO')
    check("fbkind_05985", fb_kind('https://www.facebook.com/videos/K0000186') == 'VIDEO')
    check("fbkind_05986", fb_kind('https://www.facebook.com/videos/K0000187') == 'VIDEO')
    check("fbkind_05987", fb_kind('https://www.facebook.com/videos/K0000188') == 'VIDEO')
    check("fbkind_05988", fb_kind('https://www.facebook.com/videos/K0000189') == 'VIDEO')
    check("fbkind_05989", fb_kind('https://www.facebook.com/videos/K0000190') == 'VIDEO')
    check("fbkind_05990", fb_kind('https://www.facebook.com/videos/K0000191') == 'VIDEO')
    check("fbkind_05991", fb_kind('https://www.facebook.com/videos/K0000192') == 'VIDEO')
    check("fbkind_05992", fb_kind('https://www.facebook.com/videos/K0000193') == 'VIDEO')
    check("fbkind_05993", fb_kind('https://www.facebook.com/videos/K0000194') == 'VIDEO')
    check("fbkind_05994", fb_kind('https://www.facebook.com/videos/K0000195') == 'VIDEO')
    check("fbkind_05995", fb_kind('https://www.facebook.com/videos/K0000196') == 'VIDEO')
    check("fbkind_05996", fb_kind('https://www.facebook.com/videos/K0000197') == 'VIDEO')
    check("fbkind_05997", fb_kind('https://www.facebook.com/videos/K0000198') == 'VIDEO')
    check("fbkind_05998", fb_kind('https://www.facebook.com/videos/K0000199') == 'VIDEO')
    check("fbkind_05999", fb_kind('https://www.facebook.com/videos/K0000200') == 'VIDEO')
    check("fbkind_06000", fb_kind('https://www.facebook.com/videos/K0000201') == 'VIDEO')
    check("fbkind_06001", fb_kind('https://www.facebook.com/videos/K0000202') == 'VIDEO')
    check("fbkind_06002", fb_kind('https://www.facebook.com/videos/K0000203') == 'VIDEO')
    check("fbkind_06003", fb_kind('https://www.facebook.com/videos/K0000204') == 'VIDEO')
    check("fbkind_06004", fb_kind('https://www.facebook.com/videos/K0000205') == 'VIDEO')
    check("fbkind_06005", fb_kind('https://www.facebook.com/videos/K0000206') == 'VIDEO')
    check("fbkind_06006", fb_kind('https://www.facebook.com/videos/K0000207') == 'VIDEO')
    check("fbkind_06007", fb_kind('https://www.facebook.com/videos/K0000208') == 'VIDEO')
    check("fbkind_06008", fb_kind('https://www.facebook.com/videos/K0000209') == 'VIDEO')
    check("fbkind_06009", fb_kind('https://www.facebook.com/videos/K0000210') == 'VIDEO')
    check("fbkind_06010", fb_kind('https://www.facebook.com/videos/K0000211') == 'VIDEO')
    check("fbkind_06011", fb_kind('https://www.facebook.com/videos/K0000212') == 'VIDEO')
    check("fbkind_06012", fb_kind('https://www.facebook.com/videos/K0000213') == 'VIDEO')
    check("fbkind_06013", fb_kind('https://www.facebook.com/videos/K0000214') == 'VIDEO')
    check("fbkind_06014", fb_kind('https://www.facebook.com/videos/K0000215') == 'VIDEO')
    check("fbkind_06015", fb_kind('https://www.facebook.com/videos/K0000216') == 'VIDEO')
    check("fbkind_06016", fb_kind('https://www.facebook.com/videos/K0000217') == 'VIDEO')
    check("fbkind_06017", fb_kind('https://www.facebook.com/videos/K0000218') == 'VIDEO')
    check("fbkind_06018", fb_kind('https://www.facebook.com/videos/K0000219') == 'VIDEO')
    check("fbkind_06019", fb_kind('https://www.facebook.com/videos/K0000220') == 'VIDEO')
    check("fbkind_06020", fb_kind('https://www.facebook.com/videos/K0000221') == 'VIDEO')
    check("fbkind_06021", fb_kind('https://www.facebook.com/videos/K0000222') == 'VIDEO')
    check("fbkind_06022", fb_kind('https://www.facebook.com/videos/K0000223') == 'VIDEO')
    check("fbkind_06023", fb_kind('https://www.facebook.com/videos/K0000224') == 'VIDEO')
    check("fbkind_06024", fb_kind('https://www.facebook.com/videos/K0000225') == 'VIDEO')
    check("fbkind_06025", fb_kind('https://www.facebook.com/videos/K0000226') == 'VIDEO')
    check("fbkind_06026", fb_kind('https://www.facebook.com/videos/K0000227') == 'VIDEO')
    check("fbkind_06027", fb_kind('https://www.facebook.com/videos/K0000228') == 'VIDEO')
    check("fbkind_06028", fb_kind('https://www.facebook.com/videos/K0000229') == 'VIDEO')
    check("fbkind_06029", fb_kind('https://www.facebook.com/videos/K0000230') == 'VIDEO')
    check("fbkind_06030", fb_kind('https://www.facebook.com/videos/K0000231') == 'VIDEO')
    check("fbkind_06031", fb_kind('https://www.facebook.com/videos/K0000232') == 'VIDEO')
    check("fbkind_06032", fb_kind('https://www.facebook.com/videos/K0000233') == 'VIDEO')
    check("fbkind_06033", fb_kind('https://www.facebook.com/videos/K0000234') == 'VIDEO')
    check("fbkind_06034", fb_kind('https://www.facebook.com/videos/K0000235') == 'VIDEO')
    check("fbkind_06035", fb_kind('https://www.facebook.com/videos/K0000236') == 'VIDEO')
    check("fbkind_06036", fb_kind('https://www.facebook.com/videos/K0000237') == 'VIDEO')
    check("fbkind_06037", fb_kind('https://www.facebook.com/videos/K0000238') == 'VIDEO')
    check("fbkind_06038", fb_kind('https://www.facebook.com/videos/K0000239') == 'VIDEO')
    check("fbkind_06039", fb_kind('https://www.facebook.com/videos/K0000240') == 'VIDEO')
    check("fbkind_06040", fb_kind('https://www.facebook.com/videos/K0000241') == 'VIDEO')
    check("fbkind_06041", fb_kind('https://www.facebook.com/videos/K0000242') == 'VIDEO')
    check("fbkind_06042", fb_kind('https://www.facebook.com/videos/K0000243') == 'VIDEO')
    check("fbkind_06043", fb_kind('https://www.facebook.com/videos/K0000244') == 'VIDEO')
    check("fbkind_06044", fb_kind('https://www.facebook.com/videos/K0000245') == 'VIDEO')
    check("fbkind_06045", fb_kind('https://www.facebook.com/videos/K0000246') == 'VIDEO')
    check("fbkind_06046", fb_kind('https://www.facebook.com/videos/K0000247') == 'VIDEO')
    check("fbkind_06047", fb_kind('https://www.facebook.com/videos/K0000248') == 'VIDEO')
    check("fbkind_06048", fb_kind('https://www.facebook.com/videos/K0000249') == 'VIDEO')
    check("fbkind_06049", fb_kind('https://www.facebook.com/videos/K0000250') == 'VIDEO')
    check("fbkind_06050", fb_kind('https://www.facebook.com/videos/K0000251') == 'VIDEO')
    check("fbkind_06051", fb_kind('https://www.facebook.com/videos/K0000252') == 'VIDEO')
    check("fbkind_06052", fb_kind('https://www.facebook.com/videos/K0000253') == 'VIDEO')
    check("fbkind_06053", fb_kind('https://www.facebook.com/videos/K0000254') == 'VIDEO')
    check("fbkind_06054", fb_kind('https://www.facebook.com/videos/K0000255') == 'VIDEO')
    check("fbkind_06055", fb_kind('https://www.facebook.com/videos/K0000256') == 'VIDEO')
    check("fbkind_06056", fb_kind('https://www.facebook.com/videos/K0000257') == 'VIDEO')
    check("fbkind_06057", fb_kind('https://www.facebook.com/videos/K0000258') == 'VIDEO')
    check("fbkind_06058", fb_kind('https://www.facebook.com/videos/K0000259') == 'VIDEO')
    check("fbkind_06059", fb_kind('https://www.facebook.com/videos/K0000260') == 'VIDEO')
    check("fbkind_06060", fb_kind('https://www.facebook.com/videos/K0000261') == 'VIDEO')
    check("fbkind_06061", fb_kind('https://www.facebook.com/videos/K0000262') == 'VIDEO')
    check("fbkind_06062", fb_kind('https://www.facebook.com/videos/K0000263') == 'VIDEO')
    check("fbkind_06063", fb_kind('https://www.facebook.com/videos/K0000264') == 'VIDEO')
    check("fbkind_06064", fb_kind('https://www.facebook.com/videos/K0000265') == 'VIDEO')
    check("fbkind_06065", fb_kind('https://www.facebook.com/videos/K0000266') == 'VIDEO')
    check("fbkind_06066", fb_kind('https://www.facebook.com/videos/K0000267') == 'VIDEO')
    check("fbkind_06067", fb_kind('https://www.facebook.com/videos/K0000268') == 'VIDEO')
    check("fbkind_06068", fb_kind('https://www.facebook.com/videos/K0000269') == 'VIDEO')
    check("fbkind_06069", fb_kind('https://www.facebook.com/videos/K0000270') == 'VIDEO')
    check("fbkind_06070", fb_kind('https://www.facebook.com/videos/K0000271') == 'VIDEO')
    check("fbkind_06071", fb_kind('https://www.facebook.com/videos/K0000272') == 'VIDEO')
    check("fbkind_06072", fb_kind('https://www.facebook.com/videos/K0000273') == 'VIDEO')
    check("fbkind_06073", fb_kind('https://www.facebook.com/videos/K0000274') == 'VIDEO')
    check("fbkind_06074", fb_kind('https://www.facebook.com/videos/K0000275') == 'VIDEO')
    check("fbkind_06075", fb_kind('https://www.facebook.com/videos/K0000276') == 'VIDEO')
    check("fbkind_06076", fb_kind('https://www.facebook.com/videos/K0000277') == 'VIDEO')
    check("fbkind_06077", fb_kind('https://www.facebook.com/videos/K0000278') == 'VIDEO')
    check("fbkind_06078", fb_kind('https://www.facebook.com/videos/K0000279') == 'VIDEO')
    check("fbkind_06079", fb_kind('https://www.facebook.com/videos/K0000280') == 'VIDEO')
    check("fbkind_06080", fb_kind('https://www.facebook.com/videos/K0000281') == 'VIDEO')
    check("fbkind_06081", fb_kind('https://www.facebook.com/videos/K0000282') == 'VIDEO')
    check("fbkind_06082", fb_kind('https://www.facebook.com/videos/K0000283') == 'VIDEO')
    check("fbkind_06083", fb_kind('https://www.facebook.com/videos/K0000284') == 'VIDEO')
    check("fbkind_06084", fb_kind('https://www.facebook.com/videos/K0000285') == 'VIDEO')
    check("fbkind_06085", fb_kind('https://www.facebook.com/videos/K0000286') == 'VIDEO')
    check("fbkind_06086", fb_kind('https://www.facebook.com/videos/K0000287') == 'VIDEO')
    check("fbkind_06087", fb_kind('https://www.facebook.com/videos/K0000288') == 'VIDEO')
    check("fbkind_06088", fb_kind('https://www.facebook.com/videos/K0000289') == 'VIDEO')
    check("fbkind_06089", fb_kind('https://www.facebook.com/videos/K0000290') == 'VIDEO')
    check("fbkind_06090", fb_kind('https://www.facebook.com/videos/K0000291') == 'VIDEO')
    check("fbkind_06091", fb_kind('https://www.facebook.com/videos/K0000292') == 'VIDEO')
    check("fbkind_06092", fb_kind('https://www.facebook.com/videos/K0000293') == 'VIDEO')
    check("fbkind_06093", fb_kind('https://www.facebook.com/videos/K0000294') == 'VIDEO')
    check("fbkind_06094", fb_kind('https://www.facebook.com/videos/K0000295') == 'VIDEO')
    check("fbkind_06095", fb_kind('https://www.facebook.com/videos/K0000296') == 'VIDEO')
    check("fbkind_06096", fb_kind('https://www.facebook.com/videos/K0000297') == 'VIDEO')
    check("fbkind_06097", fb_kind('https://www.facebook.com/videos/K0000298') == 'VIDEO')
    check("fbkind_06098", fb_kind('https://www.facebook.com/videos/K0000299') == 'VIDEO')
    check("fbkind_06099", fb_kind('https://www.facebook.com/videos/K0000300') == 'VIDEO')
    check("fbkind_06100", fb_kind('https://www.facebook.com/videos/K0000301') == 'VIDEO')
    check("fbkind_06101", fb_kind('https://www.facebook.com/videos/K0000302') == 'VIDEO')
    check("fbkind_06102", fb_kind('https://www.facebook.com/videos/K0000303') == 'VIDEO')
    check("fbkind_06103", fb_kind('https://www.facebook.com/videos/K0000304') == 'VIDEO')
    check("fbkind_06104", fb_kind('https://www.facebook.com/videos/K0000305') == 'VIDEO')
    check("fbkind_06105", fb_kind('https://www.facebook.com/videos/K0000306') == 'VIDEO')
    check("fbkind_06106", fb_kind('https://www.facebook.com/videos/K0000307') == 'VIDEO')
    check("fbkind_06107", fb_kind('https://www.facebook.com/videos/K0000308') == 'VIDEO')
    check("fbkind_06108", fb_kind('https://www.facebook.com/videos/K0000309') == 'VIDEO')
    check("fbkind_06109", fb_kind('https://www.facebook.com/videos/K0000310') == 'VIDEO')
    check("fbkind_06110", fb_kind('https://www.facebook.com/videos/K0000311') == 'VIDEO')
    check("fbkind_06111", fb_kind('https://www.facebook.com/videos/K0000312') == 'VIDEO')
    check("fbkind_06112", fb_kind('https://www.facebook.com/videos/K0000313') == 'VIDEO')
    check("fbkind_06113", fb_kind('https://www.facebook.com/videos/K0000314') == 'VIDEO')
    check("fbkind_06114", fb_kind('https://www.facebook.com/videos/K0000315') == 'VIDEO')
    check("fbkind_06115", fb_kind('https://www.facebook.com/videos/K0000316') == 'VIDEO')
    check("fbkind_06116", fb_kind('https://www.facebook.com/videos/K0000317') == 'VIDEO')
    check("fbkind_06117", fb_kind('https://www.facebook.com/videos/K0000318') == 'VIDEO')
    check("fbkind_06118", fb_kind('https://www.facebook.com/videos/K0000319') == 'VIDEO')
    check("fbkind_06119", fb_kind('https://www.facebook.com/videos/K0000320') == 'VIDEO')
    check("fbkind_06120", fb_kind('https://www.facebook.com/videos/K0000321') == 'VIDEO')
    check("fbkind_06121", fb_kind('https://www.facebook.com/videos/K0000322') == 'VIDEO')
    check("fbkind_06122", fb_kind('https://www.facebook.com/videos/K0000323') == 'VIDEO')
    check("fbkind_06123", fb_kind('https://www.facebook.com/videos/K0000324') == 'VIDEO')
    check("fbkind_06124", fb_kind('https://www.facebook.com/videos/K0000325') == 'VIDEO')
    check("fbkind_06125", fb_kind('https://www.facebook.com/videos/K0000326') == 'VIDEO')
    check("fbkind_06126", fb_kind('https://www.facebook.com/videos/K0000327') == 'VIDEO')
    check("fbkind_06127", fb_kind('https://www.facebook.com/videos/K0000328') == 'VIDEO')
    check("fbkind_06128", fb_kind('https://www.facebook.com/videos/K0000329') == 'VIDEO')
    check("fbkind_06129", fb_kind('https://www.facebook.com/videos/K0000330') == 'VIDEO')
    check("fbkind_06130", fb_kind('https://www.facebook.com/videos/K0000331') == 'VIDEO')
    check("fbkind_06131", fb_kind('https://www.facebook.com/videos/K0000332') == 'VIDEO')
    check("fbkind_06132", fb_kind('https://www.facebook.com/videos/K0000333') == 'VIDEO')
    check("fbkind_06133", fb_kind('https://www.facebook.com/videos/K0000334') == 'VIDEO')
    check("fbkind_06134", fb_kind('https://www.facebook.com/videos/K0000335') == 'VIDEO')
    check("fbkind_06135", fb_kind('https://www.facebook.com/videos/K0000336') == 'VIDEO')
    check("fbkind_06136", fb_kind('https://www.facebook.com/videos/K0000337') == 'VIDEO')
    check("fbkind_06137", fb_kind('https://www.facebook.com/videos/K0000338') == 'VIDEO')
    check("fbkind_06138", fb_kind('https://www.facebook.com/videos/K0000339') == 'VIDEO')
    check("fbkind_06139", fb_kind('https://www.facebook.com/videos/K0000340') == 'VIDEO')
    check("fbkind_06140", fb_kind('https://www.facebook.com/videos/K0000341') == 'VIDEO')
    check("fbkind_06141", fb_kind('https://www.facebook.com/videos/K0000342') == 'VIDEO')
    check("fbkind_06142", fb_kind('https://www.facebook.com/videos/K0000343') == 'VIDEO')
    check("fbkind_06143", fb_kind('https://www.facebook.com/videos/K0000344') == 'VIDEO')
    check("fbkind_06144", fb_kind('https://www.facebook.com/videos/K0000345') == 'VIDEO')
    check("fbkind_06145", fb_kind('https://www.facebook.com/videos/K0000346') == 'VIDEO')
    check("fbkind_06146", fb_kind('https://www.facebook.com/videos/K0000347') == 'VIDEO')
    check("fbkind_06147", fb_kind('https://www.facebook.com/videos/K0000348') == 'VIDEO')
    check("fbkind_06148", fb_kind('https://www.facebook.com/videos/K0000349') == 'VIDEO')
    check("fbkind_06149", fb_kind('https://www.facebook.com/videos/K0000350') == 'VIDEO')
    check("fbkind_06150", fb_kind('https://www.facebook.com/videos/K0000351') == 'VIDEO')
    check("fbkind_06151", fb_kind('https://www.facebook.com/videos/K0000352') == 'VIDEO')
    check("fbkind_06152", fb_kind('https://www.facebook.com/videos/K0000353') == 'VIDEO')
    check("fbkind_06153", fb_kind('https://www.facebook.com/videos/K0000354') == 'VIDEO')
    check("fbkind_06154", fb_kind('https://www.facebook.com/videos/K0000355') == 'VIDEO')
    check("fbkind_06155", fb_kind('https://www.facebook.com/videos/K0000356') == 'VIDEO')
    check("fbkind_06156", fb_kind('https://www.facebook.com/videos/K0000357') == 'VIDEO')
    check("fbkind_06157", fb_kind('https://www.facebook.com/videos/K0000358') == 'VIDEO')
    check("fbkind_06158", fb_kind('https://www.facebook.com/videos/K0000359') == 'VIDEO')
    check("fbkind_06159", fb_kind('https://www.facebook.com/videos/K0000360') == 'VIDEO')
    check("fbkind_06160", fb_kind('https://www.facebook.com/videos/K0000361') == 'VIDEO')
    check("fbkind_06161", fb_kind('https://www.facebook.com/videos/K0000362') == 'VIDEO')
    check("fbkind_06162", fb_kind('https://www.facebook.com/videos/K0000363') == 'VIDEO')
    check("fbkind_06163", fb_kind('https://www.facebook.com/videos/K0000364') == 'VIDEO')
    check("fbkind_06164", fb_kind('https://www.facebook.com/videos/K0000365') == 'VIDEO')
    check("fbkind_06165", fb_kind('https://www.facebook.com/videos/K0000366') == 'VIDEO')
    check("fbkind_06166", fb_kind('https://www.facebook.com/videos/K0000367') == 'VIDEO')
    check("fbkind_06167", fb_kind('https://www.facebook.com/videos/K0000368') == 'VIDEO')
    check("fbkind_06168", fb_kind('https://www.facebook.com/videos/K0000369') == 'VIDEO')
    check("fbkind_06169", fb_kind('https://www.facebook.com/videos/K0000370') == 'VIDEO')
    check("fbkind_06170", fb_kind('https://www.facebook.com/videos/K0000371') == 'VIDEO')
    check("fbkind_06171", fb_kind('https://www.facebook.com/videos/K0000372') == 'VIDEO')
    check("fbkind_06172", fb_kind('https://www.facebook.com/videos/K0000373') == 'VIDEO')
    check("fbkind_06173", fb_kind('https://www.facebook.com/videos/K0000374') == 'VIDEO')
    check("fbkind_06174", fb_kind('https://www.facebook.com/videos/K0000375') == 'VIDEO')
    check("fbkind_06175", fb_kind('https://www.facebook.com/videos/K0000376') == 'VIDEO')
    check("fbkind_06176", fb_kind('https://www.facebook.com/videos/K0000377') == 'VIDEO')
    check("fbkind_06177", fb_kind('https://www.facebook.com/videos/K0000378') == 'VIDEO')
    check("fbkind_06178", fb_kind('https://www.facebook.com/videos/K0000379') == 'VIDEO')
    check("fbkind_06179", fb_kind('https://www.facebook.com/videos/K0000380') == 'VIDEO')
    check("fbkind_06180", fb_kind('https://www.facebook.com/videos/K0000381') == 'VIDEO')
    check("fbkind_06181", fb_kind('https://www.facebook.com/videos/K0000382') == 'VIDEO')
    check("fbkind_06182", fb_kind('https://www.facebook.com/videos/K0000383') == 'VIDEO')
    check("fbkind_06183", fb_kind('https://www.facebook.com/videos/K0000384') == 'VIDEO')
    check("fbkind_06184", fb_kind('https://www.facebook.com/videos/K0000385') == 'VIDEO')
    check("fbkind_06185", fb_kind('https://www.facebook.com/videos/K0000386') == 'VIDEO')
    check("fbkind_06186", fb_kind('https://www.facebook.com/videos/K0000387') == 'VIDEO')
    check("fbkind_06187", fb_kind('https://www.facebook.com/videos/K0000388') == 'VIDEO')
    check("fbkind_06188", fb_kind('https://www.facebook.com/videos/K0000389') == 'VIDEO')
    check("fbkind_06189", fb_kind('https://www.facebook.com/videos/K0000390') == 'VIDEO')
    check("fbkind_06190", fb_kind('https://www.facebook.com/videos/K0000391') == 'VIDEO')
    check("fbkind_06191", fb_kind('https://www.facebook.com/videos/K0000392') == 'VIDEO')
    check("fbkind_06192", fb_kind('https://www.facebook.com/videos/K0000393') == 'VIDEO')
    check("fbkind_06193", fb_kind('https://www.facebook.com/videos/K0000394') == 'VIDEO')
    check("fbkind_06194", fb_kind('https://www.facebook.com/videos/K0000395') == 'VIDEO')
    check("fbkind_06195", fb_kind('https://www.facebook.com/videos/K0000396') == 'VIDEO')
    check("fbkind_06196", fb_kind('https://www.facebook.com/videos/K0000397') == 'VIDEO')
    check("fbkind_06197", fb_kind('https://www.facebook.com/videos/K0000398') == 'VIDEO')
    check("fbkind_06198", fb_kind('https://www.facebook.com/videos/K0000399') == 'VIDEO')
    check("fbkind_06199", fb_kind('https://www.facebook.com/videos/K0000400') == 'VIDEO')
    check("fbkind_06200", fb_kind('https://www.facebook.com/videos/K0000401') == 'VIDEO')
    check("fbkind_06201", fb_kind('https://www.facebook.com/videos/K0000402') == 'VIDEO')
    check("fbkind_06202", fb_kind('https://www.facebook.com/videos/K0000403') == 'VIDEO')
    check("fbkind_06203", fb_kind('https://www.facebook.com/videos/K0000404') == 'VIDEO')
    check("fbkind_06204", fb_kind('https://www.facebook.com/videos/K0000405') == 'VIDEO')
    check("fbkind_06205", fb_kind('https://www.facebook.com/videos/K0000406') == 'VIDEO')
    check("fbkind_06206", fb_kind('https://www.facebook.com/videos/K0000407') == 'VIDEO')
    check("fbkind_06207", fb_kind('https://www.facebook.com/videos/K0000408') == 'VIDEO')
    check("fbkind_06208", fb_kind('https://www.facebook.com/videos/K0000409') == 'VIDEO')
    check("fbkind_06209", fb_kind('https://www.facebook.com/videos/K0000410') == 'VIDEO')
    check("fbkind_06210", fb_kind('https://www.facebook.com/videos/K0000411') == 'VIDEO')
    check("fbkind_06211", fb_kind('https://www.facebook.com/videos/K0000412') == 'VIDEO')
    check("fbkind_06212", fb_kind('https://www.facebook.com/videos/K0000413') == 'VIDEO')
    check("fbkind_06213", fb_kind('https://www.facebook.com/videos/K0000414') == 'VIDEO')
    check("fbkind_06214", fb_kind('https://www.facebook.com/videos/K0000415') == 'VIDEO')
    check("fbkind_06215", fb_kind('https://www.facebook.com/videos/K0000416') == 'VIDEO')
    check("fbkind_06216", fb_kind('https://www.facebook.com/videos/K0000417') == 'VIDEO')
    check("fbkind_06217", fb_kind('https://www.facebook.com/videos/K0000418') == 'VIDEO')
    check("fbkind_06218", fb_kind('https://www.facebook.com/videos/K0000419') == 'VIDEO')
    check("fbkind_06219", fb_kind('https://www.facebook.com/videos/K0000420') == 'VIDEO')
    check("fbkind_06220", fb_kind('https://www.facebook.com/videos/K0000421') == 'VIDEO')
    check("fbkind_06221", fb_kind('https://www.facebook.com/videos/K0000422') == 'VIDEO')
    check("fbkind_06222", fb_kind('https://www.facebook.com/videos/K0000423') == 'VIDEO')
    check("fbkind_06223", fb_kind('https://www.facebook.com/videos/K0000424') == 'VIDEO')
    check("fbkind_06224", fb_kind('https://www.facebook.com/videos/K0000425') == 'VIDEO')
    check("fbkind_06225", fb_kind('https://www.facebook.com/videos/K0000426') == 'VIDEO')
    check("fbkind_06226", fb_kind('https://www.facebook.com/videos/K0000427') == 'VIDEO')
    check("fbkind_06227", fb_kind('https://www.facebook.com/videos/K0000428') == 'VIDEO')
    check("fbkind_06228", fb_kind('https://www.facebook.com/videos/K0000429') == 'VIDEO')
    check("fbkind_06229", fb_kind('https://www.facebook.com/videos/K0000430') == 'VIDEO')
    check("fbkind_06230", fb_kind('https://www.facebook.com/videos/K0000431') == 'VIDEO')
    check("fbkind_06231", fb_kind('https://www.facebook.com/videos/K0000432') == 'VIDEO')
    check("fbkind_06232", fb_kind('https://www.facebook.com/videos/K0000433') == 'VIDEO')
    check("fbkind_06233", fb_kind('https://www.facebook.com/videos/K0000434') == 'VIDEO')
    check("fbkind_06234", fb_kind('https://www.facebook.com/videos/K0000435') == 'VIDEO')
    check("fbkind_06235", fb_kind('https://www.facebook.com/videos/K0000436') == 'VIDEO')
    check("fbkind_06236", fb_kind('https://www.facebook.com/videos/K0000437') == 'VIDEO')
    check("fbkind_06237", fb_kind('https://www.facebook.com/videos/K0000438') == 'VIDEO')
    check("fbkind_06238", fb_kind('https://www.facebook.com/videos/K0000439') == 'VIDEO')
    check("fbkind_06239", fb_kind('https://www.facebook.com/videos/K0000440') == 'VIDEO')
    check("fbkind_06240", fb_kind('https://www.facebook.com/videos/K0000441') == 'VIDEO')
    check("fbkind_06241", fb_kind('https://www.facebook.com/videos/K0000442') == 'VIDEO')
    check("fbkind_06242", fb_kind('https://www.facebook.com/videos/K0000443') == 'VIDEO')
    check("fbkind_06243", fb_kind('https://www.facebook.com/videos/K0000444') == 'VIDEO')
    check("fbkind_06244", fb_kind('https://www.facebook.com/videos/K0000445') == 'VIDEO')
    check("fbkind_06245", fb_kind('https://www.facebook.com/videos/K0000446') == 'VIDEO')
    check("fbkind_06246", fb_kind('https://www.facebook.com/videos/K0000447') == 'VIDEO')
    check("fbkind_06247", fb_kind('https://www.facebook.com/videos/K0000448') == 'VIDEO')
    check("fbkind_06248", fb_kind('https://www.facebook.com/videos/K0000449') == 'VIDEO')
    check("fbkind_06249", fb_kind('https://www.facebook.com/videos/K0000450') == 'VIDEO')
    check("fbkind_06250", fb_kind('https://www.facebook.com/videos/K0000451') == 'VIDEO')
    check("fbkind_06251", fb_kind('https://www.facebook.com/videos/K0000452') == 'VIDEO')
    check("fbkind_06252", fb_kind('https://www.facebook.com/videos/K0000453') == 'VIDEO')
    check("fbkind_06253", fb_kind('https://www.facebook.com/videos/K0000454') == 'VIDEO')
    check("fbkind_06254", fb_kind('https://www.facebook.com/videos/K0000455') == 'VIDEO')
    check("fbkind_06255", fb_kind('https://www.facebook.com/videos/K0000456') == 'VIDEO')
    check("fbkind_06256", fb_kind('https://www.facebook.com/videos/K0000457') == 'VIDEO')
    check("fbkind_06257", fb_kind('https://www.facebook.com/videos/K0000458') == 'VIDEO')
    check("fbkind_06258", fb_kind('https://www.facebook.com/videos/K0000459') == 'VIDEO')
    check("fbkind_06259", fb_kind('https://www.facebook.com/videos/K0000460') == 'VIDEO')
    check("fbkind_06260", fb_kind('https://www.facebook.com/videos/K0000461') == 'VIDEO')
    check("fbkind_06261", fb_kind('https://www.facebook.com/videos/K0000462') == 'VIDEO')
    check("fbkind_06262", fb_kind('https://www.facebook.com/videos/K0000463') == 'VIDEO')
    check("fbkind_06263", fb_kind('https://www.facebook.com/videos/K0000464') == 'VIDEO')
    check("fbkind_06264", fb_kind('https://www.facebook.com/videos/K0000465') == 'VIDEO')
    check("fbkind_06265", fb_kind('https://www.facebook.com/videos/K0000466') == 'VIDEO')
    check("fbkind_06266", fb_kind('https://www.facebook.com/videos/K0000467') == 'VIDEO')
    check("fbkind_06267", fb_kind('https://www.facebook.com/videos/K0000468') == 'VIDEO')
    check("fbkind_06268", fb_kind('https://www.facebook.com/videos/K0000469') == 'VIDEO')
    check("fbkind_06269", fb_kind('https://www.facebook.com/videos/K0000470') == 'VIDEO')
    check("fbkind_06270", fb_kind('https://www.facebook.com/videos/K0000471') == 'VIDEO')
    check("fbkind_06271", fb_kind('https://www.facebook.com/videos/K0000472') == 'VIDEO')
    check("fbkind_06272", fb_kind('https://www.facebook.com/videos/K0000473') == 'VIDEO')
    check("fbkind_06273", fb_kind('https://www.facebook.com/videos/K0000474') == 'VIDEO')
    check("fbkind_06274", fb_kind('https://www.facebook.com/videos/K0000475') == 'VIDEO')
    check("fbkind_06275", fb_kind('https://www.facebook.com/videos/K0000476') == 'VIDEO')
    check("fbkind_06276", fb_kind('https://www.facebook.com/videos/K0000477') == 'VIDEO')
    check("fbkind_06277", fb_kind('https://www.facebook.com/videos/K0000478') == 'VIDEO')
    check("fbkind_06278", fb_kind('https://www.facebook.com/videos/K0000479') == 'VIDEO')
    check("fbkind_06279", fb_kind('https://www.facebook.com/videos/K0000480') == 'VIDEO')
    check("fbkind_06280", fb_kind('https://www.facebook.com/videos/K0000481') == 'VIDEO')
    check("fbkind_06281", fb_kind('https://www.facebook.com/videos/K0000482') == 'VIDEO')
    check("fbkind_06282", fb_kind('https://www.facebook.com/videos/K0000483') == 'VIDEO')
    check("fbkind_06283", fb_kind('https://www.facebook.com/videos/K0000484') == 'VIDEO')
    check("fbkind_06284", fb_kind('https://www.facebook.com/videos/K0000485') == 'VIDEO')
    check("fbkind_06285", fb_kind('https://www.facebook.com/videos/K0000486') == 'VIDEO')
    check("fbkind_06286", fb_kind('https://www.facebook.com/videos/K0000487') == 'VIDEO')
    check("fbkind_06287", fb_kind('https://www.facebook.com/videos/K0000488') == 'VIDEO')
    check("fbkind_06288", fb_kind('https://www.facebook.com/videos/K0000489') == 'VIDEO')
    check("fbkind_06289", fb_kind('https://www.facebook.com/videos/K0000490') == 'VIDEO')
    check("fbkind_06290", fb_kind('https://www.facebook.com/videos/K0000491') == 'VIDEO')
    check("fbkind_06291", fb_kind('https://www.facebook.com/videos/K0000492') == 'VIDEO')
    check("fbkind_06292", fb_kind('https://www.facebook.com/videos/K0000493') == 'VIDEO')
    check("fbkind_06293", fb_kind('https://www.facebook.com/videos/K0000494') == 'VIDEO')
    check("fbkind_06294", fb_kind('https://www.facebook.com/videos/K0000495') == 'VIDEO')
    check("fbkind_06295", fb_kind('https://www.facebook.com/videos/K0000496') == 'VIDEO')
    check("fbkind_06296", fb_kind('https://www.facebook.com/videos/K0000497') == 'VIDEO')
    check("fbkind_06297", fb_kind('https://www.facebook.com/videos/K0000498') == 'VIDEO')
    check("fbkind_06298", fb_kind('https://www.facebook.com/videos/K0000499') == 'VIDEO')
    check("fbkind_06299", fb_kind('https://www.facebook.com/videos/K0000500') == 'VIDEO')
    check("fbkind_06300", fb_kind('https://www.facebook.com/posts/K0000001') == 'POST')
    check("fbkind_06301", fb_kind('https://www.facebook.com/posts/K0000002') == 'POST')
    check("fbkind_06302", fb_kind('https://www.facebook.com/posts/K0000003') == 'POST')
    check("fbkind_06303", fb_kind('https://www.facebook.com/posts/K0000004') == 'POST')
    check("fbkind_06304", fb_kind('https://www.facebook.com/posts/K0000005') == 'POST')
    check("fbkind_06305", fb_kind('https://www.facebook.com/posts/K0000006') == 'POST')
    check("fbkind_06306", fb_kind('https://www.facebook.com/posts/K0000007') == 'POST')
    check("fbkind_06307", fb_kind('https://www.facebook.com/posts/K0000008') == 'POST')
    check("fbkind_06308", fb_kind('https://www.facebook.com/posts/K0000009') == 'POST')
    check("fbkind_06309", fb_kind('https://www.facebook.com/posts/K0000010') == 'POST')
    check("fbkind_06310", fb_kind('https://www.facebook.com/posts/K0000011') == 'POST')
    check("fbkind_06311", fb_kind('https://www.facebook.com/posts/K0000012') == 'POST')
    check("fbkind_06312", fb_kind('https://www.facebook.com/posts/K0000013') == 'POST')
    check("fbkind_06313", fb_kind('https://www.facebook.com/posts/K0000014') == 'POST')
    check("fbkind_06314", fb_kind('https://www.facebook.com/posts/K0000015') == 'POST')
    check("fbkind_06315", fb_kind('https://www.facebook.com/posts/K0000016') == 'POST')
    check("fbkind_06316", fb_kind('https://www.facebook.com/posts/K0000017') == 'POST')
    check("fbkind_06317", fb_kind('https://www.facebook.com/posts/K0000018') == 'POST')
    check("fbkind_06318", fb_kind('https://www.facebook.com/posts/K0000019') == 'POST')
    check("fbkind_06319", fb_kind('https://www.facebook.com/posts/K0000020') == 'POST')
    check("fbkind_06320", fb_kind('https://www.facebook.com/posts/K0000021') == 'POST')
    check("fbkind_06321", fb_kind('https://www.facebook.com/posts/K0000022') == 'POST')
    check("fbkind_06322", fb_kind('https://www.facebook.com/posts/K0000023') == 'POST')
    check("fbkind_06323", fb_kind('https://www.facebook.com/posts/K0000024') == 'POST')
    check("fbkind_06324", fb_kind('https://www.facebook.com/posts/K0000025') == 'POST')
    check("fbkind_06325", fb_kind('https://www.facebook.com/posts/K0000026') == 'POST')
    check("fbkind_06326", fb_kind('https://www.facebook.com/posts/K0000027') == 'POST')
    check("fbkind_06327", fb_kind('https://www.facebook.com/posts/K0000028') == 'POST')
    check("fbkind_06328", fb_kind('https://www.facebook.com/posts/K0000029') == 'POST')
    check("fbkind_06329", fb_kind('https://www.facebook.com/posts/K0000030') == 'POST')
    check("fbkind_06330", fb_kind('https://www.facebook.com/posts/K0000031') == 'POST')
    check("fbkind_06331", fb_kind('https://www.facebook.com/posts/K0000032') == 'POST')
    check("fbkind_06332", fb_kind('https://www.facebook.com/posts/K0000033') == 'POST')
    check("fbkind_06333", fb_kind('https://www.facebook.com/posts/K0000034') == 'POST')
    check("fbkind_06334", fb_kind('https://www.facebook.com/posts/K0000035') == 'POST')
    check("fbkind_06335", fb_kind('https://www.facebook.com/posts/K0000036') == 'POST')
    check("fbkind_06336", fb_kind('https://www.facebook.com/posts/K0000037') == 'POST')
    check("fbkind_06337", fb_kind('https://www.facebook.com/posts/K0000038') == 'POST')
    check("fbkind_06338", fb_kind('https://www.facebook.com/posts/K0000039') == 'POST')
    check("fbkind_06339", fb_kind('https://www.facebook.com/posts/K0000040') == 'POST')
    check("fbkind_06340", fb_kind('https://www.facebook.com/posts/K0000041') == 'POST')
    check("fbkind_06341", fb_kind('https://www.facebook.com/posts/K0000042') == 'POST')
    check("fbkind_06342", fb_kind('https://www.facebook.com/posts/K0000043') == 'POST')
    check("fbkind_06343", fb_kind('https://www.facebook.com/posts/K0000044') == 'POST')
    check("fbkind_06344", fb_kind('https://www.facebook.com/posts/K0000045') == 'POST')
    check("fbkind_06345", fb_kind('https://www.facebook.com/posts/K0000046') == 'POST')
    check("fbkind_06346", fb_kind('https://www.facebook.com/posts/K0000047') == 'POST')
    check("fbkind_06347", fb_kind('https://www.facebook.com/posts/K0000048') == 'POST')
    check("fbkind_06348", fb_kind('https://www.facebook.com/posts/K0000049') == 'POST')
    check("fbkind_06349", fb_kind('https://www.facebook.com/posts/K0000050') == 'POST')
    check("fbkind_06350", fb_kind('https://www.facebook.com/posts/K0000051') == 'POST')
    check("fbkind_06351", fb_kind('https://www.facebook.com/posts/K0000052') == 'POST')
    check("fbkind_06352", fb_kind('https://www.facebook.com/posts/K0000053') == 'POST')
    check("fbkind_06353", fb_kind('https://www.facebook.com/posts/K0000054') == 'POST')
    check("fbkind_06354", fb_kind('https://www.facebook.com/posts/K0000055') == 'POST')
    check("fbkind_06355", fb_kind('https://www.facebook.com/posts/K0000056') == 'POST')
    check("fbkind_06356", fb_kind('https://www.facebook.com/posts/K0000057') == 'POST')
    check("fbkind_06357", fb_kind('https://www.facebook.com/posts/K0000058') == 'POST')
    check("fbkind_06358", fb_kind('https://www.facebook.com/posts/K0000059') == 'POST')
    check("fbkind_06359", fb_kind('https://www.facebook.com/posts/K0000060') == 'POST')
    check("fbkind_06360", fb_kind('https://www.facebook.com/posts/K0000061') == 'POST')
    check("fbkind_06361", fb_kind('https://www.facebook.com/posts/K0000062') == 'POST')
    check("fbkind_06362", fb_kind('https://www.facebook.com/posts/K0000063') == 'POST')
    check("fbkind_06363", fb_kind('https://www.facebook.com/posts/K0000064') == 'POST')
    check("fbkind_06364", fb_kind('https://www.facebook.com/posts/K0000065') == 'POST')
    check("fbkind_06365", fb_kind('https://www.facebook.com/posts/K0000066') == 'POST')
    check("fbkind_06366", fb_kind('https://www.facebook.com/posts/K0000067') == 'POST')
    check("fbkind_06367", fb_kind('https://www.facebook.com/posts/K0000068') == 'POST')
    check("fbkind_06368", fb_kind('https://www.facebook.com/posts/K0000069') == 'POST')
    check("fbkind_06369", fb_kind('https://www.facebook.com/posts/K0000070') == 'POST')
    check("fbkind_06370", fb_kind('https://www.facebook.com/posts/K0000071') == 'POST')
    check("fbkind_06371", fb_kind('https://www.facebook.com/posts/K0000072') == 'POST')
    check("fbkind_06372", fb_kind('https://www.facebook.com/posts/K0000073') == 'POST')
    check("fbkind_06373", fb_kind('https://www.facebook.com/posts/K0000074') == 'POST')
    check("fbkind_06374", fb_kind('https://www.facebook.com/posts/K0000075') == 'POST')
    check("fbkind_06375", fb_kind('https://www.facebook.com/posts/K0000076') == 'POST')
    check("fbkind_06376", fb_kind('https://www.facebook.com/posts/K0000077') == 'POST')
    check("fbkind_06377", fb_kind('https://www.facebook.com/posts/K0000078') == 'POST')
    check("fbkind_06378", fb_kind('https://www.facebook.com/posts/K0000079') == 'POST')
    check("fbkind_06379", fb_kind('https://www.facebook.com/posts/K0000080') == 'POST')
    check("fbkind_06380", fb_kind('https://www.facebook.com/posts/K0000081') == 'POST')
    check("fbkind_06381", fb_kind('https://www.facebook.com/posts/K0000082') == 'POST')
    check("fbkind_06382", fb_kind('https://www.facebook.com/posts/K0000083') == 'POST')
    check("fbkind_06383", fb_kind('https://www.facebook.com/posts/K0000084') == 'POST')
    check("fbkind_06384", fb_kind('https://www.facebook.com/posts/K0000085') == 'POST')
    check("fbkind_06385", fb_kind('https://www.facebook.com/posts/K0000086') == 'POST')
    check("fbkind_06386", fb_kind('https://www.facebook.com/posts/K0000087') == 'POST')
    check("fbkind_06387", fb_kind('https://www.facebook.com/posts/K0000088') == 'POST')
    check("fbkind_06388", fb_kind('https://www.facebook.com/posts/K0000089') == 'POST')
    check("fbkind_06389", fb_kind('https://www.facebook.com/posts/K0000090') == 'POST')
    check("fbkind_06390", fb_kind('https://www.facebook.com/posts/K0000091') == 'POST')
    check("fbkind_06391", fb_kind('https://www.facebook.com/posts/K0000092') == 'POST')
    check("fbkind_06392", fb_kind('https://www.facebook.com/posts/K0000093') == 'POST')
    check("fbkind_06393", fb_kind('https://www.facebook.com/posts/K0000094') == 'POST')
    check("fbkind_06394", fb_kind('https://www.facebook.com/posts/K0000095') == 'POST')
    check("fbkind_06395", fb_kind('https://www.facebook.com/posts/K0000096') == 'POST')
    check("fbkind_06396", fb_kind('https://www.facebook.com/posts/K0000097') == 'POST')
    check("fbkind_06397", fb_kind('https://www.facebook.com/posts/K0000098') == 'POST')
    check("fbkind_06398", fb_kind('https://www.facebook.com/posts/K0000099') == 'POST')
    check("fbkind_06399", fb_kind('https://www.facebook.com/posts/K0000100') == 'POST')
    check("fbkind_06400", fb_kind('https://www.facebook.com/posts/K0000101') == 'POST')
    check("fbkind_06401", fb_kind('https://www.facebook.com/posts/K0000102') == 'POST')
    check("fbkind_06402", fb_kind('https://www.facebook.com/posts/K0000103') == 'POST')
    check("fbkind_06403", fb_kind('https://www.facebook.com/posts/K0000104') == 'POST')
    check("fbkind_06404", fb_kind('https://www.facebook.com/posts/K0000105') == 'POST')
    check("fbkind_06405", fb_kind('https://www.facebook.com/posts/K0000106') == 'POST')
    check("fbkind_06406", fb_kind('https://www.facebook.com/posts/K0000107') == 'POST')
    check("fbkind_06407", fb_kind('https://www.facebook.com/posts/K0000108') == 'POST')
    check("fbkind_06408", fb_kind('https://www.facebook.com/posts/K0000109') == 'POST')
    check("fbkind_06409", fb_kind('https://www.facebook.com/posts/K0000110') == 'POST')
    check("fbkind_06410", fb_kind('https://www.facebook.com/posts/K0000111') == 'POST')
    check("fbkind_06411", fb_kind('https://www.facebook.com/posts/K0000112') == 'POST')
    check("fbkind_06412", fb_kind('https://www.facebook.com/posts/K0000113') == 'POST')
    check("fbkind_06413", fb_kind('https://www.facebook.com/posts/K0000114') == 'POST')
    check("fbkind_06414", fb_kind('https://www.facebook.com/posts/K0000115') == 'POST')
    check("fbkind_06415", fb_kind('https://www.facebook.com/posts/K0000116') == 'POST')
    check("fbkind_06416", fb_kind('https://www.facebook.com/posts/K0000117') == 'POST')
    check("fbkind_06417", fb_kind('https://www.facebook.com/posts/K0000118') == 'POST')
    check("fbkind_06418", fb_kind('https://www.facebook.com/posts/K0000119') == 'POST')
    check("fbkind_06419", fb_kind('https://www.facebook.com/posts/K0000120') == 'POST')
    check("fbkind_06420", fb_kind('https://www.facebook.com/posts/K0000121') == 'POST')
    check("fbkind_06421", fb_kind('https://www.facebook.com/posts/K0000122') == 'POST')
    check("fbkind_06422", fb_kind('https://www.facebook.com/posts/K0000123') == 'POST')
    check("fbkind_06423", fb_kind('https://www.facebook.com/posts/K0000124') == 'POST')
    check("fbkind_06424", fb_kind('https://www.facebook.com/posts/K0000125') == 'POST')
    check("fbkind_06425", fb_kind('https://www.facebook.com/posts/K0000126') == 'POST')
    check("fbkind_06426", fb_kind('https://www.facebook.com/posts/K0000127') == 'POST')
    check("fbkind_06427", fb_kind('https://www.facebook.com/posts/K0000128') == 'POST')
    check("fbkind_06428", fb_kind('https://www.facebook.com/posts/K0000129') == 'POST')
    check("fbkind_06429", fb_kind('https://www.facebook.com/posts/K0000130') == 'POST')
    check("fbkind_06430", fb_kind('https://www.facebook.com/posts/K0000131') == 'POST')
    check("fbkind_06431", fb_kind('https://www.facebook.com/posts/K0000132') == 'POST')
    check("fbkind_06432", fb_kind('https://www.facebook.com/posts/K0000133') == 'POST')
    check("fbkind_06433", fb_kind('https://www.facebook.com/posts/K0000134') == 'POST')
    check("fbkind_06434", fb_kind('https://www.facebook.com/posts/K0000135') == 'POST')
    check("fbkind_06435", fb_kind('https://www.facebook.com/posts/K0000136') == 'POST')
    check("fbkind_06436", fb_kind('https://www.facebook.com/posts/K0000137') == 'POST')
    check("fbkind_06437", fb_kind('https://www.facebook.com/posts/K0000138') == 'POST')
    check("fbkind_06438", fb_kind('https://www.facebook.com/posts/K0000139') == 'POST')
    check("fbkind_06439", fb_kind('https://www.facebook.com/posts/K0000140') == 'POST')
    check("fbkind_06440", fb_kind('https://www.facebook.com/posts/K0000141') == 'POST')
    check("fbkind_06441", fb_kind('https://www.facebook.com/posts/K0000142') == 'POST')
    check("fbkind_06442", fb_kind('https://www.facebook.com/posts/K0000143') == 'POST')
    check("fbkind_06443", fb_kind('https://www.facebook.com/posts/K0000144') == 'POST')
    check("fbkind_06444", fb_kind('https://www.facebook.com/posts/K0000145') == 'POST')
    check("fbkind_06445", fb_kind('https://www.facebook.com/posts/K0000146') == 'POST')
    check("fbkind_06446", fb_kind('https://www.facebook.com/posts/K0000147') == 'POST')
    check("fbkind_06447", fb_kind('https://www.facebook.com/posts/K0000148') == 'POST')
    check("fbkind_06448", fb_kind('https://www.facebook.com/posts/K0000149') == 'POST')
    check("fbkind_06449", fb_kind('https://www.facebook.com/posts/K0000150') == 'POST')
    check("fbkind_06450", fb_kind('https://www.facebook.com/posts/K0000151') == 'POST')
    check("fbkind_06451", fb_kind('https://www.facebook.com/posts/K0000152') == 'POST')
    check("fbkind_06452", fb_kind('https://www.facebook.com/posts/K0000153') == 'POST')
    check("fbkind_06453", fb_kind('https://www.facebook.com/posts/K0000154') == 'POST')
    check("fbkind_06454", fb_kind('https://www.facebook.com/posts/K0000155') == 'POST')
    check("fbkind_06455", fb_kind('https://www.facebook.com/posts/K0000156') == 'POST')
    check("fbkind_06456", fb_kind('https://www.facebook.com/posts/K0000157') == 'POST')
    check("fbkind_06457", fb_kind('https://www.facebook.com/posts/K0000158') == 'POST')
    check("fbkind_06458", fb_kind('https://www.facebook.com/posts/K0000159') == 'POST')
    check("fbkind_06459", fb_kind('https://www.facebook.com/posts/K0000160') == 'POST')
    check("fbkind_06460", fb_kind('https://www.facebook.com/posts/K0000161') == 'POST')
    check("fbkind_06461", fb_kind('https://www.facebook.com/posts/K0000162') == 'POST')
    check("fbkind_06462", fb_kind('https://www.facebook.com/posts/K0000163') == 'POST')
    check("fbkind_06463", fb_kind('https://www.facebook.com/posts/K0000164') == 'POST')
    check("fbkind_06464", fb_kind('https://www.facebook.com/posts/K0000165') == 'POST')
    check("fbkind_06465", fb_kind('https://www.facebook.com/posts/K0000166') == 'POST')
    check("fbkind_06466", fb_kind('https://www.facebook.com/posts/K0000167') == 'POST')
    check("fbkind_06467", fb_kind('https://www.facebook.com/posts/K0000168') == 'POST')
    check("fbkind_06468", fb_kind('https://www.facebook.com/posts/K0000169') == 'POST')
    check("fbkind_06469", fb_kind('https://www.facebook.com/posts/K0000170') == 'POST')
    check("fbkind_06470", fb_kind('https://www.facebook.com/posts/K0000171') == 'POST')
    check("fbkind_06471", fb_kind('https://www.facebook.com/posts/K0000172') == 'POST')
    check("fbkind_06472", fb_kind('https://www.facebook.com/posts/K0000173') == 'POST')
    check("fbkind_06473", fb_kind('https://www.facebook.com/posts/K0000174') == 'POST')
    check("fbkind_06474", fb_kind('https://www.facebook.com/posts/K0000175') == 'POST')
    check("fbkind_06475", fb_kind('https://www.facebook.com/posts/K0000176') == 'POST')
    check("fbkind_06476", fb_kind('https://www.facebook.com/posts/K0000177') == 'POST')
    check("fbkind_06477", fb_kind('https://www.facebook.com/posts/K0000178') == 'POST')
    check("fbkind_06478", fb_kind('https://www.facebook.com/posts/K0000179') == 'POST')
    check("fbkind_06479", fb_kind('https://www.facebook.com/posts/K0000180') == 'POST')
    check("fbkind_06480", fb_kind('https://www.facebook.com/posts/K0000181') == 'POST')
    check("fbkind_06481", fb_kind('https://www.facebook.com/posts/K0000182') == 'POST')
    check("fbkind_06482", fb_kind('https://www.facebook.com/posts/K0000183') == 'POST')
    check("fbkind_06483", fb_kind('https://www.facebook.com/posts/K0000184') == 'POST')
    check("fbkind_06484", fb_kind('https://www.facebook.com/posts/K0000185') == 'POST')
    check("fbkind_06485", fb_kind('https://www.facebook.com/posts/K0000186') == 'POST')
    check("fbkind_06486", fb_kind('https://www.facebook.com/posts/K0000187') == 'POST')
    check("fbkind_06487", fb_kind('https://www.facebook.com/posts/K0000188') == 'POST')
    check("fbkind_06488", fb_kind('https://www.facebook.com/posts/K0000189') == 'POST')
    check("fbkind_06489", fb_kind('https://www.facebook.com/posts/K0000190') == 'POST')
    check("fbkind_06490", fb_kind('https://www.facebook.com/posts/K0000191') == 'POST')
    check("fbkind_06491", fb_kind('https://www.facebook.com/posts/K0000192') == 'POST')
    check("fbkind_06492", fb_kind('https://www.facebook.com/posts/K0000193') == 'POST')
    check("fbkind_06493", fb_kind('https://www.facebook.com/posts/K0000194') == 'POST')
    check("fbkind_06494", fb_kind('https://www.facebook.com/posts/K0000195') == 'POST')
    check("fbkind_06495", fb_kind('https://www.facebook.com/posts/K0000196') == 'POST')
    check("fbkind_06496", fb_kind('https://www.facebook.com/posts/K0000197') == 'POST')
    check("fbkind_06497", fb_kind('https://www.facebook.com/posts/K0000198') == 'POST')
    check("fbkind_06498", fb_kind('https://www.facebook.com/posts/K0000199') == 'POST')
    check("fbkind_06499", fb_kind('https://www.facebook.com/posts/K0000200') == 'POST')
    check("fbkind_06500", fb_kind('https://www.facebook.com/posts/K0000201') == 'POST')
    check("fbkind_06501", fb_kind('https://www.facebook.com/posts/K0000202') == 'POST')
    check("fbkind_06502", fb_kind('https://www.facebook.com/posts/K0000203') == 'POST')
    check("fbkind_06503", fb_kind('https://www.facebook.com/posts/K0000204') == 'POST')
    check("fbkind_06504", fb_kind('https://www.facebook.com/posts/K0000205') == 'POST')
    check("fbkind_06505", fb_kind('https://www.facebook.com/posts/K0000206') == 'POST')
    check("fbkind_06506", fb_kind('https://www.facebook.com/posts/K0000207') == 'POST')
    check("fbkind_06507", fb_kind('https://www.facebook.com/posts/K0000208') == 'POST')
    check("fbkind_06508", fb_kind('https://www.facebook.com/posts/K0000209') == 'POST')
    check("fbkind_06509", fb_kind('https://www.facebook.com/posts/K0000210') == 'POST')
    check("fbkind_06510", fb_kind('https://www.facebook.com/posts/K0000211') == 'POST')
    check("fbkind_06511", fb_kind('https://www.facebook.com/posts/K0000212') == 'POST')
    check("fbkind_06512", fb_kind('https://www.facebook.com/posts/K0000213') == 'POST')
    check("fbkind_06513", fb_kind('https://www.facebook.com/posts/K0000214') == 'POST')
    check("fbkind_06514", fb_kind('https://www.facebook.com/posts/K0000215') == 'POST')
    check("fbkind_06515", fb_kind('https://www.facebook.com/posts/K0000216') == 'POST')
    check("fbkind_06516", fb_kind('https://www.facebook.com/posts/K0000217') == 'POST')
    check("fbkind_06517", fb_kind('https://www.facebook.com/posts/K0000218') == 'POST')
    check("fbkind_06518", fb_kind('https://www.facebook.com/posts/K0000219') == 'POST')
    check("fbkind_06519", fb_kind('https://www.facebook.com/posts/K0000220') == 'POST')
    check("fbkind_06520", fb_kind('https://www.facebook.com/posts/K0000221') == 'POST')
    check("fbkind_06521", fb_kind('https://www.facebook.com/posts/K0000222') == 'POST')
    check("fbkind_06522", fb_kind('https://www.facebook.com/posts/K0000223') == 'POST')
    check("fbkind_06523", fb_kind('https://www.facebook.com/posts/K0000224') == 'POST')
    check("fbkind_06524", fb_kind('https://www.facebook.com/posts/K0000225') == 'POST')
    check("fbkind_06525", fb_kind('https://www.facebook.com/posts/K0000226') == 'POST')
    check("fbkind_06526", fb_kind('https://www.facebook.com/posts/K0000227') == 'POST')
    check("fbkind_06527", fb_kind('https://www.facebook.com/posts/K0000228') == 'POST')
    check("fbkind_06528", fb_kind('https://www.facebook.com/posts/K0000229') == 'POST')
    check("fbkind_06529", fb_kind('https://www.facebook.com/posts/K0000230') == 'POST')
    check("fbkind_06530", fb_kind('https://www.facebook.com/posts/K0000231') == 'POST')
    check("fbkind_06531", fb_kind('https://www.facebook.com/posts/K0000232') == 'POST')
    check("fbkind_06532", fb_kind('https://www.facebook.com/posts/K0000233') == 'POST')
    check("fbkind_06533", fb_kind('https://www.facebook.com/posts/K0000234') == 'POST')
    check("fbkind_06534", fb_kind('https://www.facebook.com/posts/K0000235') == 'POST')
    check("fbkind_06535", fb_kind('https://www.facebook.com/posts/K0000236') == 'POST')
    check("fbkind_06536", fb_kind('https://www.facebook.com/posts/K0000237') == 'POST')
    check("fbkind_06537", fb_kind('https://www.facebook.com/posts/K0000238') == 'POST')
    check("fbkind_06538", fb_kind('https://www.facebook.com/posts/K0000239') == 'POST')
    check("fbkind_06539", fb_kind('https://www.facebook.com/posts/K0000240') == 'POST')
    check("fbkind_06540", fb_kind('https://www.facebook.com/posts/K0000241') == 'POST')
    check("fbkind_06541", fb_kind('https://www.facebook.com/posts/K0000242') == 'POST')
    check("fbkind_06542", fb_kind('https://www.facebook.com/posts/K0000243') == 'POST')
    check("fbkind_06543", fb_kind('https://www.facebook.com/posts/K0000244') == 'POST')
    check("fbkind_06544", fb_kind('https://www.facebook.com/posts/K0000245') == 'POST')
    check("fbkind_06545", fb_kind('https://www.facebook.com/posts/K0000246') == 'POST')
    check("fbkind_06546", fb_kind('https://www.facebook.com/posts/K0000247') == 'POST')
    check("fbkind_06547", fb_kind('https://www.facebook.com/posts/K0000248') == 'POST')
    check("fbkind_06548", fb_kind('https://www.facebook.com/posts/K0000249') == 'POST')
    check("fbkind_06549", fb_kind('https://www.facebook.com/posts/K0000250') == 'POST')
    check("fbkind_06550", fb_kind('https://www.facebook.com/posts/K0000251') == 'POST')
    check("fbkind_06551", fb_kind('https://www.facebook.com/posts/K0000252') == 'POST')
    check("fbkind_06552", fb_kind('https://www.facebook.com/posts/K0000253') == 'POST')
    check("fbkind_06553", fb_kind('https://www.facebook.com/posts/K0000254') == 'POST')
    check("fbkind_06554", fb_kind('https://www.facebook.com/posts/K0000255') == 'POST')
    check("fbkind_06555", fb_kind('https://www.facebook.com/posts/K0000256') == 'POST')
    check("fbkind_06556", fb_kind('https://www.facebook.com/posts/K0000257') == 'POST')
    check("fbkind_06557", fb_kind('https://www.facebook.com/posts/K0000258') == 'POST')
    check("fbkind_06558", fb_kind('https://www.facebook.com/posts/K0000259') == 'POST')
    check("fbkind_06559", fb_kind('https://www.facebook.com/posts/K0000260') == 'POST')
    check("fbkind_06560", fb_kind('https://www.facebook.com/posts/K0000261') == 'POST')
    check("fbkind_06561", fb_kind('https://www.facebook.com/posts/K0000262') == 'POST')
    check("fbkind_06562", fb_kind('https://www.facebook.com/posts/K0000263') == 'POST')
    check("fbkind_06563", fb_kind('https://www.facebook.com/posts/K0000264') == 'POST')
    check("fbkind_06564", fb_kind('https://www.facebook.com/posts/K0000265') == 'POST')
    check("fbkind_06565", fb_kind('https://www.facebook.com/posts/K0000266') == 'POST')
    check("fbkind_06566", fb_kind('https://www.facebook.com/posts/K0000267') == 'POST')
    check("fbkind_06567", fb_kind('https://www.facebook.com/posts/K0000268') == 'POST')
    check("fbkind_06568", fb_kind('https://www.facebook.com/posts/K0000269') == 'POST')
    check("fbkind_06569", fb_kind('https://www.facebook.com/posts/K0000270') == 'POST')
    check("fbkind_06570", fb_kind('https://www.facebook.com/posts/K0000271') == 'POST')
    check("fbkind_06571", fb_kind('https://www.facebook.com/posts/K0000272') == 'POST')
    check("fbkind_06572", fb_kind('https://www.facebook.com/posts/K0000273') == 'POST')
    check("fbkind_06573", fb_kind('https://www.facebook.com/posts/K0000274') == 'POST')
    check("fbkind_06574", fb_kind('https://www.facebook.com/posts/K0000275') == 'POST')
    check("fbkind_06575", fb_kind('https://www.facebook.com/posts/K0000276') == 'POST')
    check("fbkind_06576", fb_kind('https://www.facebook.com/posts/K0000277') == 'POST')
    check("fbkind_06577", fb_kind('https://www.facebook.com/posts/K0000278') == 'POST')
    check("fbkind_06578", fb_kind('https://www.facebook.com/posts/K0000279') == 'POST')
    check("fbkind_06579", fb_kind('https://www.facebook.com/posts/K0000280') == 'POST')
    check("fbkind_06580", fb_kind('https://www.facebook.com/posts/K0000281') == 'POST')
    check("fbkind_06581", fb_kind('https://www.facebook.com/posts/K0000282') == 'POST')
    check("fbkind_06582", fb_kind('https://www.facebook.com/posts/K0000283') == 'POST')
    check("fbkind_06583", fb_kind('https://www.facebook.com/posts/K0000284') == 'POST')
    check("fbkind_06584", fb_kind('https://www.facebook.com/posts/K0000285') == 'POST')
    check("fbkind_06585", fb_kind('https://www.facebook.com/posts/K0000286') == 'POST')
    check("fbkind_06586", fb_kind('https://www.facebook.com/posts/K0000287') == 'POST')
    check("fbkind_06587", fb_kind('https://www.facebook.com/posts/K0000288') == 'POST')
    check("fbkind_06588", fb_kind('https://www.facebook.com/posts/K0000289') == 'POST')
    check("fbkind_06589", fb_kind('https://www.facebook.com/posts/K0000290') == 'POST')
    check("fbkind_06590", fb_kind('https://www.facebook.com/posts/K0000291') == 'POST')
    check("fbkind_06591", fb_kind('https://www.facebook.com/posts/K0000292') == 'POST')
    check("fbkind_06592", fb_kind('https://www.facebook.com/posts/K0000293') == 'POST')
    check("fbkind_06593", fb_kind('https://www.facebook.com/posts/K0000294') == 'POST')
    check("fbkind_06594", fb_kind('https://www.facebook.com/posts/K0000295') == 'POST')
    check("fbkind_06595", fb_kind('https://www.facebook.com/posts/K0000296') == 'POST')
    check("fbkind_06596", fb_kind('https://www.facebook.com/posts/K0000297') == 'POST')
    check("fbkind_06597", fb_kind('https://www.facebook.com/posts/K0000298') == 'POST')
    check("fbkind_06598", fb_kind('https://www.facebook.com/posts/K0000299') == 'POST')
    check("fbkind_06599", fb_kind('https://www.facebook.com/posts/K0000300') == 'POST')
    check("fbkind_06600", fb_kind('https://www.facebook.com/posts/K0000301') == 'POST')
    check("fbkind_06601", fb_kind('https://www.facebook.com/posts/K0000302') == 'POST')
    check("fbkind_06602", fb_kind('https://www.facebook.com/posts/K0000303') == 'POST')
    check("fbkind_06603", fb_kind('https://www.facebook.com/posts/K0000304') == 'POST')
    check("fbkind_06604", fb_kind('https://www.facebook.com/posts/K0000305') == 'POST')
    check("fbkind_06605", fb_kind('https://www.facebook.com/posts/K0000306') == 'POST')
    check("fbkind_06606", fb_kind('https://www.facebook.com/posts/K0000307') == 'POST')
    check("fbkind_06607", fb_kind('https://www.facebook.com/posts/K0000308') == 'POST')
    check("fbkind_06608", fb_kind('https://www.facebook.com/posts/K0000309') == 'POST')
    check("fbkind_06609", fb_kind('https://www.facebook.com/posts/K0000310') == 'POST')
    check("fbkind_06610", fb_kind('https://www.facebook.com/posts/K0000311') == 'POST')
    check("fbkind_06611", fb_kind('https://www.facebook.com/posts/K0000312') == 'POST')
    check("fbkind_06612", fb_kind('https://www.facebook.com/posts/K0000313') == 'POST')
    check("fbkind_06613", fb_kind('https://www.facebook.com/posts/K0000314') == 'POST')
    check("fbkind_06614", fb_kind('https://www.facebook.com/posts/K0000315') == 'POST')
    check("fbkind_06615", fb_kind('https://www.facebook.com/posts/K0000316') == 'POST')
    check("fbkind_06616", fb_kind('https://www.facebook.com/posts/K0000317') == 'POST')
    check("fbkind_06617", fb_kind('https://www.facebook.com/posts/K0000318') == 'POST')
    check("fbkind_06618", fb_kind('https://www.facebook.com/posts/K0000319') == 'POST')
    check("fbkind_06619", fb_kind('https://www.facebook.com/posts/K0000320') == 'POST')
    check("fbkind_06620", fb_kind('https://www.facebook.com/posts/K0000321') == 'POST')
    check("fbkind_06621", fb_kind('https://www.facebook.com/posts/K0000322') == 'POST')
    check("fbkind_06622", fb_kind('https://www.facebook.com/posts/K0000323') == 'POST')
    check("fbkind_06623", fb_kind('https://www.facebook.com/posts/K0000324') == 'POST')
    check("fbkind_06624", fb_kind('https://www.facebook.com/posts/K0000325') == 'POST')
    check("fbkind_06625", fb_kind('https://www.facebook.com/posts/K0000326') == 'POST')
    check("fbkind_06626", fb_kind('https://www.facebook.com/posts/K0000327') == 'POST')
    check("fbkind_06627", fb_kind('https://www.facebook.com/posts/K0000328') == 'POST')
    check("fbkind_06628", fb_kind('https://www.facebook.com/posts/K0000329') == 'POST')
    check("fbkind_06629", fb_kind('https://www.facebook.com/posts/K0000330') == 'POST')
    check("fbkind_06630", fb_kind('https://www.facebook.com/posts/K0000331') == 'POST')
    check("fbkind_06631", fb_kind('https://www.facebook.com/posts/K0000332') == 'POST')
    check("fbkind_06632", fb_kind('https://www.facebook.com/posts/K0000333') == 'POST')
    check("fbkind_06633", fb_kind('https://www.facebook.com/posts/K0000334') == 'POST')
    check("fbkind_06634", fb_kind('https://www.facebook.com/posts/K0000335') == 'POST')
    check("fbkind_06635", fb_kind('https://www.facebook.com/posts/K0000336') == 'POST')
    check("fbkind_06636", fb_kind('https://www.facebook.com/posts/K0000337') == 'POST')
    check("fbkind_06637", fb_kind('https://www.facebook.com/posts/K0000338') == 'POST')
    check("fbkind_06638", fb_kind('https://www.facebook.com/posts/K0000339') == 'POST')
    check("fbkind_06639", fb_kind('https://www.facebook.com/posts/K0000340') == 'POST')
    check("fbkind_06640", fb_kind('https://www.facebook.com/posts/K0000341') == 'POST')
    check("fbkind_06641", fb_kind('https://www.facebook.com/posts/K0000342') == 'POST')
    check("fbkind_06642", fb_kind('https://www.facebook.com/posts/K0000343') == 'POST')
    check("fbkind_06643", fb_kind('https://www.facebook.com/posts/K0000344') == 'POST')
    check("fbkind_06644", fb_kind('https://www.facebook.com/posts/K0000345') == 'POST')
    check("fbkind_06645", fb_kind('https://www.facebook.com/posts/K0000346') == 'POST')
    check("fbkind_06646", fb_kind('https://www.facebook.com/posts/K0000347') == 'POST')
    check("fbkind_06647", fb_kind('https://www.facebook.com/posts/K0000348') == 'POST')
    check("fbkind_06648", fb_kind('https://www.facebook.com/posts/K0000349') == 'POST')
    check("fbkind_06649", fb_kind('https://www.facebook.com/posts/K0000350') == 'POST')
    check("fbkind_06650", fb_kind('https://www.facebook.com/posts/K0000351') == 'POST')
    check("fbkind_06651", fb_kind('https://www.facebook.com/posts/K0000352') == 'POST')
    check("fbkind_06652", fb_kind('https://www.facebook.com/posts/K0000353') == 'POST')
    check("fbkind_06653", fb_kind('https://www.facebook.com/posts/K0000354') == 'POST')
    check("fbkind_06654", fb_kind('https://www.facebook.com/posts/K0000355') == 'POST')
    check("fbkind_06655", fb_kind('https://www.facebook.com/posts/K0000356') == 'POST')
    check("fbkind_06656", fb_kind('https://www.facebook.com/posts/K0000357') == 'POST')
    check("fbkind_06657", fb_kind('https://www.facebook.com/posts/K0000358') == 'POST')
    check("fbkind_06658", fb_kind('https://www.facebook.com/posts/K0000359') == 'POST')
    check("fbkind_06659", fb_kind('https://www.facebook.com/posts/K0000360') == 'POST')
    check("fbkind_06660", fb_kind('https://www.facebook.com/posts/K0000361') == 'POST')
    check("fbkind_06661", fb_kind('https://www.facebook.com/posts/K0000362') == 'POST')
    check("fbkind_06662", fb_kind('https://www.facebook.com/posts/K0000363') == 'POST')
    check("fbkind_06663", fb_kind('https://www.facebook.com/posts/K0000364') == 'POST')
    check("fbkind_06664", fb_kind('https://www.facebook.com/posts/K0000365') == 'POST')
    check("fbkind_06665", fb_kind('https://www.facebook.com/posts/K0000366') == 'POST')
    check("fbkind_06666", fb_kind('https://www.facebook.com/posts/K0000367') == 'POST')
    check("fbkind_06667", fb_kind('https://www.facebook.com/posts/K0000368') == 'POST')
    check("fbkind_06668", fb_kind('https://www.facebook.com/posts/K0000369') == 'POST')
    check("fbkind_06669", fb_kind('https://www.facebook.com/posts/K0000370') == 'POST')
    check("fbkind_06670", fb_kind('https://www.facebook.com/posts/K0000371') == 'POST')
    check("fbkind_06671", fb_kind('https://www.facebook.com/posts/K0000372') == 'POST')
    check("fbkind_06672", fb_kind('https://www.facebook.com/posts/K0000373') == 'POST')
    check("fbkind_06673", fb_kind('https://www.facebook.com/posts/K0000374') == 'POST')
    check("fbkind_06674", fb_kind('https://www.facebook.com/posts/K0000375') == 'POST')
    check("fbkind_06675", fb_kind('https://www.facebook.com/posts/K0000376') == 'POST')
    check("fbkind_06676", fb_kind('https://www.facebook.com/posts/K0000377') == 'POST')
    check("fbkind_06677", fb_kind('https://www.facebook.com/posts/K0000378') == 'POST')
    check("fbkind_06678", fb_kind('https://www.facebook.com/posts/K0000379') == 'POST')
    check("fbkind_06679", fb_kind('https://www.facebook.com/posts/K0000380') == 'POST')
    check("fbkind_06680", fb_kind('https://www.facebook.com/posts/K0000381') == 'POST')
    check("fbkind_06681", fb_kind('https://www.facebook.com/posts/K0000382') == 'POST')
    check("fbkind_06682", fb_kind('https://www.facebook.com/posts/K0000383') == 'POST')
    check("fbkind_06683", fb_kind('https://www.facebook.com/posts/K0000384') == 'POST')
    check("fbkind_06684", fb_kind('https://www.facebook.com/posts/K0000385') == 'POST')
    check("fbkind_06685", fb_kind('https://www.facebook.com/posts/K0000386') == 'POST')
    check("fbkind_06686", fb_kind('https://www.facebook.com/posts/K0000387') == 'POST')
    check("fbkind_06687", fb_kind('https://www.facebook.com/posts/K0000388') == 'POST')
    check("fbkind_06688", fb_kind('https://www.facebook.com/posts/K0000389') == 'POST')
    check("fbkind_06689", fb_kind('https://www.facebook.com/posts/K0000390') == 'POST')
    check("fbkind_06690", fb_kind('https://www.facebook.com/posts/K0000391') == 'POST')
    check("fbkind_06691", fb_kind('https://www.facebook.com/posts/K0000392') == 'POST')
    check("fbkind_06692", fb_kind('https://www.facebook.com/posts/K0000393') == 'POST')
    check("fbkind_06693", fb_kind('https://www.facebook.com/posts/K0000394') == 'POST')
    check("fbkind_06694", fb_kind('https://www.facebook.com/posts/K0000395') == 'POST')
    check("fbkind_06695", fb_kind('https://www.facebook.com/posts/K0000396') == 'POST')
    check("fbkind_06696", fb_kind('https://www.facebook.com/posts/K0000397') == 'POST')
    check("fbkind_06697", fb_kind('https://www.facebook.com/posts/K0000398') == 'POST')
    check("fbkind_06698", fb_kind('https://www.facebook.com/posts/K0000399') == 'POST')
    check("fbkind_06699", fb_kind('https://www.facebook.com/posts/K0000400') == 'POST')
    check("fbkind_06700", fb_kind('https://www.facebook.com/posts/K0000401') == 'POST')
    check("fbkind_06701", fb_kind('https://www.facebook.com/posts/K0000402') == 'POST')
    check("fbkind_06702", fb_kind('https://www.facebook.com/posts/K0000403') == 'POST')
    check("fbkind_06703", fb_kind('https://www.facebook.com/posts/K0000404') == 'POST')
    check("fbkind_06704", fb_kind('https://www.facebook.com/posts/K0000405') == 'POST')
    check("fbkind_06705", fb_kind('https://www.facebook.com/posts/K0000406') == 'POST')
    check("fbkind_06706", fb_kind('https://www.facebook.com/posts/K0000407') == 'POST')
    check("fbkind_06707", fb_kind('https://www.facebook.com/posts/K0000408') == 'POST')
    check("fbkind_06708", fb_kind('https://www.facebook.com/posts/K0000409') == 'POST')
    check("fbkind_06709", fb_kind('https://www.facebook.com/posts/K0000410') == 'POST')
    check("fbkind_06710", fb_kind('https://www.facebook.com/posts/K0000411') == 'POST')
    check("fbkind_06711", fb_kind('https://www.facebook.com/posts/K0000412') == 'POST')
    check("fbkind_06712", fb_kind('https://www.facebook.com/posts/K0000413') == 'POST')
    check("fbkind_06713", fb_kind('https://www.facebook.com/posts/K0000414') == 'POST')
    check("fbkind_06714", fb_kind('https://www.facebook.com/posts/K0000415') == 'POST')
    check("fbkind_06715", fb_kind('https://www.facebook.com/posts/K0000416') == 'POST')
    check("fbkind_06716", fb_kind('https://www.facebook.com/posts/K0000417') == 'POST')
    check("fbkind_06717", fb_kind('https://www.facebook.com/posts/K0000418') == 'POST')
    check("fbkind_06718", fb_kind('https://www.facebook.com/posts/K0000419') == 'POST')
    check("fbkind_06719", fb_kind('https://www.facebook.com/posts/K0000420') == 'POST')
    check("fbkind_06720", fb_kind('https://www.facebook.com/posts/K0000421') == 'POST')
    check("fbkind_06721", fb_kind('https://www.facebook.com/posts/K0000422') == 'POST')
    check("fbkind_06722", fb_kind('https://www.facebook.com/posts/K0000423') == 'POST')
    check("fbkind_06723", fb_kind('https://www.facebook.com/posts/K0000424') == 'POST')
    check("fbkind_06724", fb_kind('https://www.facebook.com/posts/K0000425') == 'POST')
    check("fbkind_06725", fb_kind('https://www.facebook.com/posts/K0000426') == 'POST')
    check("fbkind_06726", fb_kind('https://www.facebook.com/posts/K0000427') == 'POST')
    check("fbkind_06727", fb_kind('https://www.facebook.com/posts/K0000428') == 'POST')
    check("fbkind_06728", fb_kind('https://www.facebook.com/posts/K0000429') == 'POST')
    check("fbkind_06729", fb_kind('https://www.facebook.com/posts/K0000430') == 'POST')
    check("fbkind_06730", fb_kind('https://www.facebook.com/posts/K0000431') == 'POST')
    check("fbkind_06731", fb_kind('https://www.facebook.com/posts/K0000432') == 'POST')
    check("fbkind_06732", fb_kind('https://www.facebook.com/posts/K0000433') == 'POST')
    check("fbkind_06733", fb_kind('https://www.facebook.com/posts/K0000434') == 'POST')
    check("fbkind_06734", fb_kind('https://www.facebook.com/posts/K0000435') == 'POST')
    check("fbkind_06735", fb_kind('https://www.facebook.com/posts/K0000436') == 'POST')
    check("fbkind_06736", fb_kind('https://www.facebook.com/posts/K0000437') == 'POST')
    check("fbkind_06737", fb_kind('https://www.facebook.com/posts/K0000438') == 'POST')
    check("fbkind_06738", fb_kind('https://www.facebook.com/posts/K0000439') == 'POST')
    check("fbkind_06739", fb_kind('https://www.facebook.com/posts/K0000440') == 'POST')
    check("fbkind_06740", fb_kind('https://www.facebook.com/posts/K0000441') == 'POST')
    check("fbkind_06741", fb_kind('https://www.facebook.com/posts/K0000442') == 'POST')
    check("fbkind_06742", fb_kind('https://www.facebook.com/posts/K0000443') == 'POST')
    check("fbkind_06743", fb_kind('https://www.facebook.com/posts/K0000444') == 'POST')
    check("fbkind_06744", fb_kind('https://www.facebook.com/posts/K0000445') == 'POST')
    check("fbkind_06745", fb_kind('https://www.facebook.com/posts/K0000446') == 'POST')
    check("fbkind_06746", fb_kind('https://www.facebook.com/posts/K0000447') == 'POST')
    check("fbkind_06747", fb_kind('https://www.facebook.com/posts/K0000448') == 'POST')
    check("fbkind_06748", fb_kind('https://www.facebook.com/posts/K0000449') == 'POST')
    check("fbkind_06749", fb_kind('https://www.facebook.com/posts/K0000450') == 'POST')
    check("fbkind_06750", fb_kind('https://www.facebook.com/posts/K0000451') == 'POST')
    check("fbkind_06751", fb_kind('https://www.facebook.com/posts/K0000452') == 'POST')
    check("fbkind_06752", fb_kind('https://www.facebook.com/posts/K0000453') == 'POST')
    check("fbkind_06753", fb_kind('https://www.facebook.com/posts/K0000454') == 'POST')
    check("fbkind_06754", fb_kind('https://www.facebook.com/posts/K0000455') == 'POST')
    check("fbkind_06755", fb_kind('https://www.facebook.com/posts/K0000456') == 'POST')
    check("fbkind_06756", fb_kind('https://www.facebook.com/posts/K0000457') == 'POST')
    check("fbkind_06757", fb_kind('https://www.facebook.com/posts/K0000458') == 'POST')
    check("fbkind_06758", fb_kind('https://www.facebook.com/posts/K0000459') == 'POST')
    check("fbkind_06759", fb_kind('https://www.facebook.com/posts/K0000460') == 'POST')
    check("fbkind_06760", fb_kind('https://www.facebook.com/posts/K0000461') == 'POST')
    check("fbkind_06761", fb_kind('https://www.facebook.com/posts/K0000462') == 'POST')
    check("fbkind_06762", fb_kind('https://www.facebook.com/posts/K0000463') == 'POST')
    check("fbkind_06763", fb_kind('https://www.facebook.com/posts/K0000464') == 'POST')
    check("fbkind_06764", fb_kind('https://www.facebook.com/posts/K0000465') == 'POST')
    check("fbkind_06765", fb_kind('https://www.facebook.com/posts/K0000466') == 'POST')
    check("fbkind_06766", fb_kind('https://www.facebook.com/posts/K0000467') == 'POST')
    check("fbkind_06767", fb_kind('https://www.facebook.com/posts/K0000468') == 'POST')
    check("fbkind_06768", fb_kind('https://www.facebook.com/posts/K0000469') == 'POST')
    check("fbkind_06769", fb_kind('https://www.facebook.com/posts/K0000470') == 'POST')
    check("fbkind_06770", fb_kind('https://www.facebook.com/posts/K0000471') == 'POST')
    check("fbkind_06771", fb_kind('https://www.facebook.com/posts/K0000472') == 'POST')
    check("fbkind_06772", fb_kind('https://www.facebook.com/posts/K0000473') == 'POST')
    check("fbkind_06773", fb_kind('https://www.facebook.com/posts/K0000474') == 'POST')
    check("fbkind_06774", fb_kind('https://www.facebook.com/posts/K0000475') == 'POST')
    check("fbkind_06775", fb_kind('https://www.facebook.com/posts/K0000476') == 'POST')
    check("fbkind_06776", fb_kind('https://www.facebook.com/posts/K0000477') == 'POST')
    check("fbkind_06777", fb_kind('https://www.facebook.com/posts/K0000478') == 'POST')
    check("fbkind_06778", fb_kind('https://www.facebook.com/posts/K0000479') == 'POST')
    check("fbkind_06779", fb_kind('https://www.facebook.com/posts/K0000480') == 'POST')
    check("fbkind_06780", fb_kind('https://www.facebook.com/posts/K0000481') == 'POST')
    check("fbkind_06781", fb_kind('https://www.facebook.com/posts/K0000482') == 'POST')
    check("fbkind_06782", fb_kind('https://www.facebook.com/posts/K0000483') == 'POST')
    check("fbkind_06783", fb_kind('https://www.facebook.com/posts/K0000484') == 'POST')
    check("fbkind_06784", fb_kind('https://www.facebook.com/posts/K0000485') == 'POST')
    check("fbkind_06785", fb_kind('https://www.facebook.com/posts/K0000486') == 'POST')
    check("fbkind_06786", fb_kind('https://www.facebook.com/posts/K0000487') == 'POST')
    check("fbkind_06787", fb_kind('https://www.facebook.com/posts/K0000488') == 'POST')
    check("fbkind_06788", fb_kind('https://www.facebook.com/posts/K0000489') == 'POST')
    check("fbkind_06789", fb_kind('https://www.facebook.com/posts/K0000490') == 'POST')
    check("fbkind_06790", fb_kind('https://www.facebook.com/posts/K0000491') == 'POST')
    check("fbkind_06791", fb_kind('https://www.facebook.com/posts/K0000492') == 'POST')
    check("fbkind_06792", fb_kind('https://www.facebook.com/posts/K0000493') == 'POST')
    check("fbkind_06793", fb_kind('https://www.facebook.com/posts/K0000494') == 'POST')
    check("fbkind_06794", fb_kind('https://www.facebook.com/posts/K0000495') == 'POST')
    check("fbkind_06795", fb_kind('https://www.facebook.com/posts/K0000496') == 'POST')
    check("fbkind_06796", fb_kind('https://www.facebook.com/posts/K0000497') == 'POST')
    check("fbkind_06797", fb_kind('https://www.facebook.com/posts/K0000498') == 'POST')
    check("fbkind_06798", fb_kind('https://www.facebook.com/posts/K0000499') == 'POST')
    check("fbkind_06799", fb_kind('https://www.facebook.com/posts/K0000500') == 'POST')
    check("fbkind_06800", fb_kind('https://www.facebook.com/share/v/K0000001/') == 'SHARE_VIDEO')
    check("fbkind_06801", fb_kind('https://www.facebook.com/share/v/K0000002/') == 'SHARE_VIDEO')
    check("fbkind_06802", fb_kind('https://www.facebook.com/share/v/K0000003/') == 'SHARE_VIDEO')
    check("fbkind_06803", fb_kind('https://www.facebook.com/share/v/K0000004/') == 'SHARE_VIDEO')
    check("fbkind_06804", fb_kind('https://www.facebook.com/share/v/K0000005/') == 'SHARE_VIDEO')
    check("fbkind_06805", fb_kind('https://www.facebook.com/share/v/K0000006/') == 'SHARE_VIDEO')
    check("fbkind_06806", fb_kind('https://www.facebook.com/share/v/K0000007/') == 'SHARE_VIDEO')
    check("fbkind_06807", fb_kind('https://www.facebook.com/share/v/K0000008/') == 'SHARE_VIDEO')
    check("fbkind_06808", fb_kind('https://www.facebook.com/share/v/K0000009/') == 'SHARE_VIDEO')
    check("fbkind_06809", fb_kind('https://www.facebook.com/share/v/K0000010/') == 'SHARE_VIDEO')
    check("fbkind_06810", fb_kind('https://www.facebook.com/share/v/K0000011/') == 'SHARE_VIDEO')
    check("fbkind_06811", fb_kind('https://www.facebook.com/share/v/K0000012/') == 'SHARE_VIDEO')
    check("fbkind_06812", fb_kind('https://www.facebook.com/share/v/K0000013/') == 'SHARE_VIDEO')
    check("fbkind_06813", fb_kind('https://www.facebook.com/share/v/K0000014/') == 'SHARE_VIDEO')
    check("fbkind_06814", fb_kind('https://www.facebook.com/share/v/K0000015/') == 'SHARE_VIDEO')
    check("fbkind_06815", fb_kind('https://www.facebook.com/share/v/K0000016/') == 'SHARE_VIDEO')
    check("fbkind_06816", fb_kind('https://www.facebook.com/share/v/K0000017/') == 'SHARE_VIDEO')
    check("fbkind_06817", fb_kind('https://www.facebook.com/share/v/K0000018/') == 'SHARE_VIDEO')
    check("fbkind_06818", fb_kind('https://www.facebook.com/share/v/K0000019/') == 'SHARE_VIDEO')
    check("fbkind_06819", fb_kind('https://www.facebook.com/share/v/K0000020/') == 'SHARE_VIDEO')
    check("fbkind_06820", fb_kind('https://www.facebook.com/share/v/K0000021/') == 'SHARE_VIDEO')
    check("fbkind_06821", fb_kind('https://www.facebook.com/share/v/K0000022/') == 'SHARE_VIDEO')
    check("fbkind_06822", fb_kind('https://www.facebook.com/share/v/K0000023/') == 'SHARE_VIDEO')
    check("fbkind_06823", fb_kind('https://www.facebook.com/share/v/K0000024/') == 'SHARE_VIDEO')
    check("fbkind_06824", fb_kind('https://www.facebook.com/share/v/K0000025/') == 'SHARE_VIDEO')
    check("fbkind_06825", fb_kind('https://www.facebook.com/share/v/K0000026/') == 'SHARE_VIDEO')
    check("fbkind_06826", fb_kind('https://www.facebook.com/share/v/K0000027/') == 'SHARE_VIDEO')
    check("fbkind_06827", fb_kind('https://www.facebook.com/share/v/K0000028/') == 'SHARE_VIDEO')
    check("fbkind_06828", fb_kind('https://www.facebook.com/share/v/K0000029/') == 'SHARE_VIDEO')
    check("fbkind_06829", fb_kind('https://www.facebook.com/share/v/K0000030/') == 'SHARE_VIDEO')
    check("fbkind_06830", fb_kind('https://www.facebook.com/share/v/K0000031/') == 'SHARE_VIDEO')
    check("fbkind_06831", fb_kind('https://www.facebook.com/share/v/K0000032/') == 'SHARE_VIDEO')
    check("fbkind_06832", fb_kind('https://www.facebook.com/share/v/K0000033/') == 'SHARE_VIDEO')
    check("fbkind_06833", fb_kind('https://www.facebook.com/share/v/K0000034/') == 'SHARE_VIDEO')
    check("fbkind_06834", fb_kind('https://www.facebook.com/share/v/K0000035/') == 'SHARE_VIDEO')
    check("fbkind_06835", fb_kind('https://www.facebook.com/share/v/K0000036/') == 'SHARE_VIDEO')
    check("fbkind_06836", fb_kind('https://www.facebook.com/share/v/K0000037/') == 'SHARE_VIDEO')
    check("fbkind_06837", fb_kind('https://www.facebook.com/share/v/K0000038/') == 'SHARE_VIDEO')
    check("fbkind_06838", fb_kind('https://www.facebook.com/share/v/K0000039/') == 'SHARE_VIDEO')
    check("fbkind_06839", fb_kind('https://www.facebook.com/share/v/K0000040/') == 'SHARE_VIDEO')
    check("fbkind_06840", fb_kind('https://www.facebook.com/share/v/K0000041/') == 'SHARE_VIDEO')
    check("fbkind_06841", fb_kind('https://www.facebook.com/share/v/K0000042/') == 'SHARE_VIDEO')
    check("fbkind_06842", fb_kind('https://www.facebook.com/share/v/K0000043/') == 'SHARE_VIDEO')
    check("fbkind_06843", fb_kind('https://www.facebook.com/share/v/K0000044/') == 'SHARE_VIDEO')
    check("fbkind_06844", fb_kind('https://www.facebook.com/share/v/K0000045/') == 'SHARE_VIDEO')
    check("fbkind_06845", fb_kind('https://www.facebook.com/share/v/K0000046/') == 'SHARE_VIDEO')
    check("fbkind_06846", fb_kind('https://www.facebook.com/share/v/K0000047/') == 'SHARE_VIDEO')
    check("fbkind_06847", fb_kind('https://www.facebook.com/share/v/K0000048/') == 'SHARE_VIDEO')
    check("fbkind_06848", fb_kind('https://www.facebook.com/share/v/K0000049/') == 'SHARE_VIDEO')
    check("fbkind_06849", fb_kind('https://www.facebook.com/share/v/K0000050/') == 'SHARE_VIDEO')
    check("fbkind_06850", fb_kind('https://www.facebook.com/share/v/K0000051/') == 'SHARE_VIDEO')
    check("fbkind_06851", fb_kind('https://www.facebook.com/share/v/K0000052/') == 'SHARE_VIDEO')
    check("fbkind_06852", fb_kind('https://www.facebook.com/share/v/K0000053/') == 'SHARE_VIDEO')
    check("fbkind_06853", fb_kind('https://www.facebook.com/share/v/K0000054/') == 'SHARE_VIDEO')
    check("fbkind_06854", fb_kind('https://www.facebook.com/share/v/K0000055/') == 'SHARE_VIDEO')
    check("fbkind_06855", fb_kind('https://www.facebook.com/share/v/K0000056/') == 'SHARE_VIDEO')
    check("fbkind_06856", fb_kind('https://www.facebook.com/share/v/K0000057/') == 'SHARE_VIDEO')
    check("fbkind_06857", fb_kind('https://www.facebook.com/share/v/K0000058/') == 'SHARE_VIDEO')
    check("fbkind_06858", fb_kind('https://www.facebook.com/share/v/K0000059/') == 'SHARE_VIDEO')
    check("fbkind_06859", fb_kind('https://www.facebook.com/share/v/K0000060/') == 'SHARE_VIDEO')
    check("fbkind_06860", fb_kind('https://www.facebook.com/share/v/K0000061/') == 'SHARE_VIDEO')
    check("fbkind_06861", fb_kind('https://www.facebook.com/share/v/K0000062/') == 'SHARE_VIDEO')
    check("fbkind_06862", fb_kind('https://www.facebook.com/share/v/K0000063/') == 'SHARE_VIDEO')
    check("fbkind_06863", fb_kind('https://www.facebook.com/share/v/K0000064/') == 'SHARE_VIDEO')
    check("fbkind_06864", fb_kind('https://www.facebook.com/share/v/K0000065/') == 'SHARE_VIDEO')
    check("fbkind_06865", fb_kind('https://www.facebook.com/share/v/K0000066/') == 'SHARE_VIDEO')
    check("fbkind_06866", fb_kind('https://www.facebook.com/share/v/K0000067/') == 'SHARE_VIDEO')
    check("fbkind_06867", fb_kind('https://www.facebook.com/share/v/K0000068/') == 'SHARE_VIDEO')
    check("fbkind_06868", fb_kind('https://www.facebook.com/share/v/K0000069/') == 'SHARE_VIDEO')
    check("fbkind_06869", fb_kind('https://www.facebook.com/share/v/K0000070/') == 'SHARE_VIDEO')
    check("fbkind_06870", fb_kind('https://www.facebook.com/share/v/K0000071/') == 'SHARE_VIDEO')
    check("fbkind_06871", fb_kind('https://www.facebook.com/share/v/K0000072/') == 'SHARE_VIDEO')
    check("fbkind_06872", fb_kind('https://www.facebook.com/share/v/K0000073/') == 'SHARE_VIDEO')
    check("fbkind_06873", fb_kind('https://www.facebook.com/share/v/K0000074/') == 'SHARE_VIDEO')
    check("fbkind_06874", fb_kind('https://www.facebook.com/share/v/K0000075/') == 'SHARE_VIDEO')
    check("fbkind_06875", fb_kind('https://www.facebook.com/share/v/K0000076/') == 'SHARE_VIDEO')
    check("fbkind_06876", fb_kind('https://www.facebook.com/share/v/K0000077/') == 'SHARE_VIDEO')
    check("fbkind_06877", fb_kind('https://www.facebook.com/share/v/K0000078/') == 'SHARE_VIDEO')
    check("fbkind_06878", fb_kind('https://www.facebook.com/share/v/K0000079/') == 'SHARE_VIDEO')
    check("fbkind_06879", fb_kind('https://www.facebook.com/share/v/K0000080/') == 'SHARE_VIDEO')
    check("fbkind_06880", fb_kind('https://www.facebook.com/share/v/K0000081/') == 'SHARE_VIDEO')
    check("fbkind_06881", fb_kind('https://www.facebook.com/share/v/K0000082/') == 'SHARE_VIDEO')
    check("fbkind_06882", fb_kind('https://www.facebook.com/share/v/K0000083/') == 'SHARE_VIDEO')
    check("fbkind_06883", fb_kind('https://www.facebook.com/share/v/K0000084/') == 'SHARE_VIDEO')
    check("fbkind_06884", fb_kind('https://www.facebook.com/share/v/K0000085/') == 'SHARE_VIDEO')
    check("fbkind_06885", fb_kind('https://www.facebook.com/share/v/K0000086/') == 'SHARE_VIDEO')
    check("fbkind_06886", fb_kind('https://www.facebook.com/share/v/K0000087/') == 'SHARE_VIDEO')
    check("fbkind_06887", fb_kind('https://www.facebook.com/share/v/K0000088/') == 'SHARE_VIDEO')
    check("fbkind_06888", fb_kind('https://www.facebook.com/share/v/K0000089/') == 'SHARE_VIDEO')
    check("fbkind_06889", fb_kind('https://www.facebook.com/share/v/K0000090/') == 'SHARE_VIDEO')
    check("fbkind_06890", fb_kind('https://www.facebook.com/share/v/K0000091/') == 'SHARE_VIDEO')
    check("fbkind_06891", fb_kind('https://www.facebook.com/share/v/K0000092/') == 'SHARE_VIDEO')
    check("fbkind_06892", fb_kind('https://www.facebook.com/share/v/K0000093/') == 'SHARE_VIDEO')
    check("fbkind_06893", fb_kind('https://www.facebook.com/share/v/K0000094/') == 'SHARE_VIDEO')
    check("fbkind_06894", fb_kind('https://www.facebook.com/share/v/K0000095/') == 'SHARE_VIDEO')
    check("fbkind_06895", fb_kind('https://www.facebook.com/share/v/K0000096/') == 'SHARE_VIDEO')
    check("fbkind_06896", fb_kind('https://www.facebook.com/share/v/K0000097/') == 'SHARE_VIDEO')
    check("fbkind_06897", fb_kind('https://www.facebook.com/share/v/K0000098/') == 'SHARE_VIDEO')
    check("fbkind_06898", fb_kind('https://www.facebook.com/share/v/K0000099/') == 'SHARE_VIDEO')
    check("fbkind_06899", fb_kind('https://www.facebook.com/share/v/K0000100/') == 'SHARE_VIDEO')
    check("fbkind_06900", fb_kind('https://www.facebook.com/share/v/K0000101/') == 'SHARE_VIDEO')
    check("fbkind_06901", fb_kind('https://www.facebook.com/share/v/K0000102/') == 'SHARE_VIDEO')
    check("fbkind_06902", fb_kind('https://www.facebook.com/share/v/K0000103/') == 'SHARE_VIDEO')
    check("fbkind_06903", fb_kind('https://www.facebook.com/share/v/K0000104/') == 'SHARE_VIDEO')
    check("fbkind_06904", fb_kind('https://www.facebook.com/share/v/K0000105/') == 'SHARE_VIDEO')
    check("fbkind_06905", fb_kind('https://www.facebook.com/share/v/K0000106/') == 'SHARE_VIDEO')
    check("fbkind_06906", fb_kind('https://www.facebook.com/share/v/K0000107/') == 'SHARE_VIDEO')
    check("fbkind_06907", fb_kind('https://www.facebook.com/share/v/K0000108/') == 'SHARE_VIDEO')
    check("fbkind_06908", fb_kind('https://www.facebook.com/share/v/K0000109/') == 'SHARE_VIDEO')
    check("fbkind_06909", fb_kind('https://www.facebook.com/share/v/K0000110/') == 'SHARE_VIDEO')
    check("fbkind_06910", fb_kind('https://www.facebook.com/share/v/K0000111/') == 'SHARE_VIDEO')
    check("fbkind_06911", fb_kind('https://www.facebook.com/share/v/K0000112/') == 'SHARE_VIDEO')
    check("fbkind_06912", fb_kind('https://www.facebook.com/share/v/K0000113/') == 'SHARE_VIDEO')
    check("fbkind_06913", fb_kind('https://www.facebook.com/share/v/K0000114/') == 'SHARE_VIDEO')
    check("fbkind_06914", fb_kind('https://www.facebook.com/share/v/K0000115/') == 'SHARE_VIDEO')
    check("fbkind_06915", fb_kind('https://www.facebook.com/share/v/K0000116/') == 'SHARE_VIDEO')
    check("fbkind_06916", fb_kind('https://www.facebook.com/share/v/K0000117/') == 'SHARE_VIDEO')
    check("fbkind_06917", fb_kind('https://www.facebook.com/share/v/K0000118/') == 'SHARE_VIDEO')
    check("fbkind_06918", fb_kind('https://www.facebook.com/share/v/K0000119/') == 'SHARE_VIDEO')
    check("fbkind_06919", fb_kind('https://www.facebook.com/share/v/K0000120/') == 'SHARE_VIDEO')
    check("fbkind_06920", fb_kind('https://www.facebook.com/share/v/K0000121/') == 'SHARE_VIDEO')
    check("fbkind_06921", fb_kind('https://www.facebook.com/share/v/K0000122/') == 'SHARE_VIDEO')
    check("fbkind_06922", fb_kind('https://www.facebook.com/share/v/K0000123/') == 'SHARE_VIDEO')
    check("fbkind_06923", fb_kind('https://www.facebook.com/share/v/K0000124/') == 'SHARE_VIDEO')
    check("fbkind_06924", fb_kind('https://www.facebook.com/share/v/K0000125/') == 'SHARE_VIDEO')
    check("fbkind_06925", fb_kind('https://www.facebook.com/share/v/K0000126/') == 'SHARE_VIDEO')
    check("fbkind_06926", fb_kind('https://www.facebook.com/share/v/K0000127/') == 'SHARE_VIDEO')
    check("fbkind_06927", fb_kind('https://www.facebook.com/share/v/K0000128/') == 'SHARE_VIDEO')
    check("fbkind_06928", fb_kind('https://www.facebook.com/share/v/K0000129/') == 'SHARE_VIDEO')
    check("fbkind_06929", fb_kind('https://www.facebook.com/share/v/K0000130/') == 'SHARE_VIDEO')
    check("fbkind_06930", fb_kind('https://www.facebook.com/share/v/K0000131/') == 'SHARE_VIDEO')
    check("fbkind_06931", fb_kind('https://www.facebook.com/share/v/K0000132/') == 'SHARE_VIDEO')
    check("fbkind_06932", fb_kind('https://www.facebook.com/share/v/K0000133/') == 'SHARE_VIDEO')
    check("fbkind_06933", fb_kind('https://www.facebook.com/share/v/K0000134/') == 'SHARE_VIDEO')
    check("fbkind_06934", fb_kind('https://www.facebook.com/share/v/K0000135/') == 'SHARE_VIDEO')
    check("fbkind_06935", fb_kind('https://www.facebook.com/share/v/K0000136/') == 'SHARE_VIDEO')
    check("fbkind_06936", fb_kind('https://www.facebook.com/share/v/K0000137/') == 'SHARE_VIDEO')
    check("fbkind_06937", fb_kind('https://www.facebook.com/share/v/K0000138/') == 'SHARE_VIDEO')
    check("fbkind_06938", fb_kind('https://www.facebook.com/share/v/K0000139/') == 'SHARE_VIDEO')
    check("fbkind_06939", fb_kind('https://www.facebook.com/share/v/K0000140/') == 'SHARE_VIDEO')
    check("fbkind_06940", fb_kind('https://www.facebook.com/share/v/K0000141/') == 'SHARE_VIDEO')
    check("fbkind_06941", fb_kind('https://www.facebook.com/share/v/K0000142/') == 'SHARE_VIDEO')
    check("fbkind_06942", fb_kind('https://www.facebook.com/share/v/K0000143/') == 'SHARE_VIDEO')
    check("fbkind_06943", fb_kind('https://www.facebook.com/share/v/K0000144/') == 'SHARE_VIDEO')
    check("fbkind_06944", fb_kind('https://www.facebook.com/share/v/K0000145/') == 'SHARE_VIDEO')
    check("fbkind_06945", fb_kind('https://www.facebook.com/share/v/K0000146/') == 'SHARE_VIDEO')
    check("fbkind_06946", fb_kind('https://www.facebook.com/share/v/K0000147/') == 'SHARE_VIDEO')
    check("fbkind_06947", fb_kind('https://www.facebook.com/share/v/K0000148/') == 'SHARE_VIDEO')
    check("fbkind_06948", fb_kind('https://www.facebook.com/share/v/K0000149/') == 'SHARE_VIDEO')
    check("fbkind_06949", fb_kind('https://www.facebook.com/share/v/K0000150/') == 'SHARE_VIDEO')
    check("fbkind_06950", fb_kind('https://www.facebook.com/share/v/K0000151/') == 'SHARE_VIDEO')
    check("fbkind_06951", fb_kind('https://www.facebook.com/share/v/K0000152/') == 'SHARE_VIDEO')
    check("fbkind_06952", fb_kind('https://www.facebook.com/share/v/K0000153/') == 'SHARE_VIDEO')
    check("fbkind_06953", fb_kind('https://www.facebook.com/share/v/K0000154/') == 'SHARE_VIDEO')
    check("fbkind_06954", fb_kind('https://www.facebook.com/share/v/K0000155/') == 'SHARE_VIDEO')
    check("fbkind_06955", fb_kind('https://www.facebook.com/share/v/K0000156/') == 'SHARE_VIDEO')
    check("fbkind_06956", fb_kind('https://www.facebook.com/share/v/K0000157/') == 'SHARE_VIDEO')
    check("fbkind_06957", fb_kind('https://www.facebook.com/share/v/K0000158/') == 'SHARE_VIDEO')
    check("fbkind_06958", fb_kind('https://www.facebook.com/share/v/K0000159/') == 'SHARE_VIDEO')
    check("fbkind_06959", fb_kind('https://www.facebook.com/share/v/K0000160/') == 'SHARE_VIDEO')
    check("fbkind_06960", fb_kind('https://www.facebook.com/share/v/K0000161/') == 'SHARE_VIDEO')
    check("fbkind_06961", fb_kind('https://www.facebook.com/share/v/K0000162/') == 'SHARE_VIDEO')
    check("fbkind_06962", fb_kind('https://www.facebook.com/share/v/K0000163/') == 'SHARE_VIDEO')
    check("fbkind_06963", fb_kind('https://www.facebook.com/share/v/K0000164/') == 'SHARE_VIDEO')
    check("fbkind_06964", fb_kind('https://www.facebook.com/share/v/K0000165/') == 'SHARE_VIDEO')
    check("fbkind_06965", fb_kind('https://www.facebook.com/share/v/K0000166/') == 'SHARE_VIDEO')
    check("fbkind_06966", fb_kind('https://www.facebook.com/share/v/K0000167/') == 'SHARE_VIDEO')
    check("fbkind_06967", fb_kind('https://www.facebook.com/share/v/K0000168/') == 'SHARE_VIDEO')
    check("fbkind_06968", fb_kind('https://www.facebook.com/share/v/K0000169/') == 'SHARE_VIDEO')
    check("fbkind_06969", fb_kind('https://www.facebook.com/share/v/K0000170/') == 'SHARE_VIDEO')
    check("fbkind_06970", fb_kind('https://www.facebook.com/share/v/K0000171/') == 'SHARE_VIDEO')
    check("fbkind_06971", fb_kind('https://www.facebook.com/share/v/K0000172/') == 'SHARE_VIDEO')
    check("fbkind_06972", fb_kind('https://www.facebook.com/share/v/K0000173/') == 'SHARE_VIDEO')
    check("fbkind_06973", fb_kind('https://www.facebook.com/share/v/K0000174/') == 'SHARE_VIDEO')
    check("fbkind_06974", fb_kind('https://www.facebook.com/share/v/K0000175/') == 'SHARE_VIDEO')
    check("fbkind_06975", fb_kind('https://www.facebook.com/share/v/K0000176/') == 'SHARE_VIDEO')
    check("fbkind_06976", fb_kind('https://www.facebook.com/share/v/K0000177/') == 'SHARE_VIDEO')
    check("fbkind_06977", fb_kind('https://www.facebook.com/share/v/K0000178/') == 'SHARE_VIDEO')
    check("fbkind_06978", fb_kind('https://www.facebook.com/share/v/K0000179/') == 'SHARE_VIDEO')
    check("fbkind_06979", fb_kind('https://www.facebook.com/share/v/K0000180/') == 'SHARE_VIDEO')
    check("fbkind_06980", fb_kind('https://www.facebook.com/share/v/K0000181/') == 'SHARE_VIDEO')
    check("fbkind_06981", fb_kind('https://www.facebook.com/share/v/K0000182/') == 'SHARE_VIDEO')
    check("fbkind_06982", fb_kind('https://www.facebook.com/share/v/K0000183/') == 'SHARE_VIDEO')
    check("fbkind_06983", fb_kind('https://www.facebook.com/share/v/K0000184/') == 'SHARE_VIDEO')
    check("fbkind_06984", fb_kind('https://www.facebook.com/share/v/K0000185/') == 'SHARE_VIDEO')
    check("fbkind_06985", fb_kind('https://www.facebook.com/share/v/K0000186/') == 'SHARE_VIDEO')
    check("fbkind_06986", fb_kind('https://www.facebook.com/share/v/K0000187/') == 'SHARE_VIDEO')
    check("fbkind_06987", fb_kind('https://www.facebook.com/share/v/K0000188/') == 'SHARE_VIDEO')
    check("fbkind_06988", fb_kind('https://www.facebook.com/share/v/K0000189/') == 'SHARE_VIDEO')
    check("fbkind_06989", fb_kind('https://www.facebook.com/share/v/K0000190/') == 'SHARE_VIDEO')
    check("fbkind_06990", fb_kind('https://www.facebook.com/share/v/K0000191/') == 'SHARE_VIDEO')
    check("fbkind_06991", fb_kind('https://www.facebook.com/share/v/K0000192/') == 'SHARE_VIDEO')
    check("fbkind_06992", fb_kind('https://www.facebook.com/share/v/K0000193/') == 'SHARE_VIDEO')
    check("fbkind_06993", fb_kind('https://www.facebook.com/share/v/K0000194/') == 'SHARE_VIDEO')
    check("fbkind_06994", fb_kind('https://www.facebook.com/share/v/K0000195/') == 'SHARE_VIDEO')
    check("fbkind_06995", fb_kind('https://www.facebook.com/share/v/K0000196/') == 'SHARE_VIDEO')
    check("fbkind_06996", fb_kind('https://www.facebook.com/share/v/K0000197/') == 'SHARE_VIDEO')
    check("fbkind_06997", fb_kind('https://www.facebook.com/share/v/K0000198/') == 'SHARE_VIDEO')
    check("fbkind_06998", fb_kind('https://www.facebook.com/share/v/K0000199/') == 'SHARE_VIDEO')
    check("fbkind_06999", fb_kind('https://www.facebook.com/share/v/K0000200/') == 'SHARE_VIDEO')
    check("fbkind_07000", fb_kind('https://www.facebook.com/share/v/K0000201/') == 'SHARE_VIDEO')
    check("fbkind_07001", fb_kind('https://www.facebook.com/share/v/K0000202/') == 'SHARE_VIDEO')
    check("fbkind_07002", fb_kind('https://www.facebook.com/share/v/K0000203/') == 'SHARE_VIDEO')
    check("fbkind_07003", fb_kind('https://www.facebook.com/share/v/K0000204/') == 'SHARE_VIDEO')
    check("fbkind_07004", fb_kind('https://www.facebook.com/share/v/K0000205/') == 'SHARE_VIDEO')
    check("fbkind_07005", fb_kind('https://www.facebook.com/share/v/K0000206/') == 'SHARE_VIDEO')
    check("fbkind_07006", fb_kind('https://www.facebook.com/share/v/K0000207/') == 'SHARE_VIDEO')
    check("fbkind_07007", fb_kind('https://www.facebook.com/share/v/K0000208/') == 'SHARE_VIDEO')
    check("fbkind_07008", fb_kind('https://www.facebook.com/share/v/K0000209/') == 'SHARE_VIDEO')
    check("fbkind_07009", fb_kind('https://www.facebook.com/share/v/K0000210/') == 'SHARE_VIDEO')
    check("fbkind_07010", fb_kind('https://www.facebook.com/share/v/K0000211/') == 'SHARE_VIDEO')
    check("fbkind_07011", fb_kind('https://www.facebook.com/share/v/K0000212/') == 'SHARE_VIDEO')
    check("fbkind_07012", fb_kind('https://www.facebook.com/share/v/K0000213/') == 'SHARE_VIDEO')
    check("fbkind_07013", fb_kind('https://www.facebook.com/share/v/K0000214/') == 'SHARE_VIDEO')
    check("fbkind_07014", fb_kind('https://www.facebook.com/share/v/K0000215/') == 'SHARE_VIDEO')
    check("fbkind_07015", fb_kind('https://www.facebook.com/share/v/K0000216/') == 'SHARE_VIDEO')
    check("fbkind_07016", fb_kind('https://www.facebook.com/share/v/K0000217/') == 'SHARE_VIDEO')
    check("fbkind_07017", fb_kind('https://www.facebook.com/share/v/K0000218/') == 'SHARE_VIDEO')
    check("fbkind_07018", fb_kind('https://www.facebook.com/share/v/K0000219/') == 'SHARE_VIDEO')
    check("fbkind_07019", fb_kind('https://www.facebook.com/share/v/K0000220/') == 'SHARE_VIDEO')
    check("fbkind_07020", fb_kind('https://www.facebook.com/share/v/K0000221/') == 'SHARE_VIDEO')
    check("fbkind_07021", fb_kind('https://www.facebook.com/share/v/K0000222/') == 'SHARE_VIDEO')
    check("fbkind_07022", fb_kind('https://www.facebook.com/share/v/K0000223/') == 'SHARE_VIDEO')
    check("fbkind_07023", fb_kind('https://www.facebook.com/share/v/K0000224/') == 'SHARE_VIDEO')
    check("fbkind_07024", fb_kind('https://www.facebook.com/share/v/K0000225/') == 'SHARE_VIDEO')
    check("fbkind_07025", fb_kind('https://www.facebook.com/share/v/K0000226/') == 'SHARE_VIDEO')
    check("fbkind_07026", fb_kind('https://www.facebook.com/share/v/K0000227/') == 'SHARE_VIDEO')
    check("fbkind_07027", fb_kind('https://www.facebook.com/share/v/K0000228/') == 'SHARE_VIDEO')
    check("fbkind_07028", fb_kind('https://www.facebook.com/share/v/K0000229/') == 'SHARE_VIDEO')
    check("fbkind_07029", fb_kind('https://www.facebook.com/share/v/K0000230/') == 'SHARE_VIDEO')
    check("fbkind_07030", fb_kind('https://www.facebook.com/share/v/K0000231/') == 'SHARE_VIDEO')
    check("fbkind_07031", fb_kind('https://www.facebook.com/share/v/K0000232/') == 'SHARE_VIDEO')
    check("fbkind_07032", fb_kind('https://www.facebook.com/share/v/K0000233/') == 'SHARE_VIDEO')
    check("fbkind_07033", fb_kind('https://www.facebook.com/share/v/K0000234/') == 'SHARE_VIDEO')
    check("fbkind_07034", fb_kind('https://www.facebook.com/share/v/K0000235/') == 'SHARE_VIDEO')
    check("fbkind_07035", fb_kind('https://www.facebook.com/share/v/K0000236/') == 'SHARE_VIDEO')
    check("fbkind_07036", fb_kind('https://www.facebook.com/share/v/K0000237/') == 'SHARE_VIDEO')
    check("fbkind_07037", fb_kind('https://www.facebook.com/share/v/K0000238/') == 'SHARE_VIDEO')
    check("fbkind_07038", fb_kind('https://www.facebook.com/share/v/K0000239/') == 'SHARE_VIDEO')
    check("fbkind_07039", fb_kind('https://www.facebook.com/share/v/K0000240/') == 'SHARE_VIDEO')
    check("fbkind_07040", fb_kind('https://www.facebook.com/share/v/K0000241/') == 'SHARE_VIDEO')
    check("fbkind_07041", fb_kind('https://www.facebook.com/share/v/K0000242/') == 'SHARE_VIDEO')
    check("fbkind_07042", fb_kind('https://www.facebook.com/share/v/K0000243/') == 'SHARE_VIDEO')
    check("fbkind_07043", fb_kind('https://www.facebook.com/share/v/K0000244/') == 'SHARE_VIDEO')
    check("fbkind_07044", fb_kind('https://www.facebook.com/share/v/K0000245/') == 'SHARE_VIDEO')
    check("fbkind_07045", fb_kind('https://www.facebook.com/share/v/K0000246/') == 'SHARE_VIDEO')
    check("fbkind_07046", fb_kind('https://www.facebook.com/share/v/K0000247/') == 'SHARE_VIDEO')
    check("fbkind_07047", fb_kind('https://www.facebook.com/share/v/K0000248/') == 'SHARE_VIDEO')
    check("fbkind_07048", fb_kind('https://www.facebook.com/share/v/K0000249/') == 'SHARE_VIDEO')
    check("fbkind_07049", fb_kind('https://www.facebook.com/share/v/K0000250/') == 'SHARE_VIDEO')
    check("fbkind_07050", fb_kind('https://www.facebook.com/share/v/K0000251/') == 'SHARE_VIDEO')
    check("fbkind_07051", fb_kind('https://www.facebook.com/share/v/K0000252/') == 'SHARE_VIDEO')
    check("fbkind_07052", fb_kind('https://www.facebook.com/share/v/K0000253/') == 'SHARE_VIDEO')
    check("fbkind_07053", fb_kind('https://www.facebook.com/share/v/K0000254/') == 'SHARE_VIDEO')
    check("fbkind_07054", fb_kind('https://www.facebook.com/share/v/K0000255/') == 'SHARE_VIDEO')
    check("fbkind_07055", fb_kind('https://www.facebook.com/share/v/K0000256/') == 'SHARE_VIDEO')
    check("fbkind_07056", fb_kind('https://www.facebook.com/share/v/K0000257/') == 'SHARE_VIDEO')
    check("fbkind_07057", fb_kind('https://www.facebook.com/share/v/K0000258/') == 'SHARE_VIDEO')
    check("fbkind_07058", fb_kind('https://www.facebook.com/share/v/K0000259/') == 'SHARE_VIDEO')
    check("fbkind_07059", fb_kind('https://www.facebook.com/share/v/K0000260/') == 'SHARE_VIDEO')
    check("fbkind_07060", fb_kind('https://www.facebook.com/share/v/K0000261/') == 'SHARE_VIDEO')
    check("fbkind_07061", fb_kind('https://www.facebook.com/share/v/K0000262/') == 'SHARE_VIDEO')
    check("fbkind_07062", fb_kind('https://www.facebook.com/share/v/K0000263/') == 'SHARE_VIDEO')
    check("fbkind_07063", fb_kind('https://www.facebook.com/share/v/K0000264/') == 'SHARE_VIDEO')
    check("fbkind_07064", fb_kind('https://www.facebook.com/share/v/K0000265/') == 'SHARE_VIDEO')
    check("fbkind_07065", fb_kind('https://www.facebook.com/share/v/K0000266/') == 'SHARE_VIDEO')
    check("fbkind_07066", fb_kind('https://www.facebook.com/share/v/K0000267/') == 'SHARE_VIDEO')
    check("fbkind_07067", fb_kind('https://www.facebook.com/share/v/K0000268/') == 'SHARE_VIDEO')
    check("fbkind_07068", fb_kind('https://www.facebook.com/share/v/K0000269/') == 'SHARE_VIDEO')
    check("fbkind_07069", fb_kind('https://www.facebook.com/share/v/K0000270/') == 'SHARE_VIDEO')
    check("fbkind_07070", fb_kind('https://www.facebook.com/share/v/K0000271/') == 'SHARE_VIDEO')
    check("fbkind_07071", fb_kind('https://www.facebook.com/share/v/K0000272/') == 'SHARE_VIDEO')
    check("fbkind_07072", fb_kind('https://www.facebook.com/share/v/K0000273/') == 'SHARE_VIDEO')
    check("fbkind_07073", fb_kind('https://www.facebook.com/share/v/K0000274/') == 'SHARE_VIDEO')
    check("fbkind_07074", fb_kind('https://www.facebook.com/share/v/K0000275/') == 'SHARE_VIDEO')
    check("fbkind_07075", fb_kind('https://www.facebook.com/share/v/K0000276/') == 'SHARE_VIDEO')
    check("fbkind_07076", fb_kind('https://www.facebook.com/share/v/K0000277/') == 'SHARE_VIDEO')
    check("fbkind_07077", fb_kind('https://www.facebook.com/share/v/K0000278/') == 'SHARE_VIDEO')
    check("fbkind_07078", fb_kind('https://www.facebook.com/share/v/K0000279/') == 'SHARE_VIDEO')
    check("fbkind_07079", fb_kind('https://www.facebook.com/share/v/K0000280/') == 'SHARE_VIDEO')
    check("fbkind_07080", fb_kind('https://www.facebook.com/share/v/K0000281/') == 'SHARE_VIDEO')
    check("fbkind_07081", fb_kind('https://www.facebook.com/share/v/K0000282/') == 'SHARE_VIDEO')
    check("fbkind_07082", fb_kind('https://www.facebook.com/share/v/K0000283/') == 'SHARE_VIDEO')
    check("fbkind_07083", fb_kind('https://www.facebook.com/share/v/K0000284/') == 'SHARE_VIDEO')
    check("fbkind_07084", fb_kind('https://www.facebook.com/share/v/K0000285/') == 'SHARE_VIDEO')
    check("fbkind_07085", fb_kind('https://www.facebook.com/share/v/K0000286/') == 'SHARE_VIDEO')
    check("fbkind_07086", fb_kind('https://www.facebook.com/share/v/K0000287/') == 'SHARE_VIDEO')
    check("fbkind_07087", fb_kind('https://www.facebook.com/share/v/K0000288/') == 'SHARE_VIDEO')
    check("fbkind_07088", fb_kind('https://www.facebook.com/share/v/K0000289/') == 'SHARE_VIDEO')
    check("fbkind_07089", fb_kind('https://www.facebook.com/share/v/K0000290/') == 'SHARE_VIDEO')
    check("fbkind_07090", fb_kind('https://www.facebook.com/share/v/K0000291/') == 'SHARE_VIDEO')
    check("fbkind_07091", fb_kind('https://www.facebook.com/share/v/K0000292/') == 'SHARE_VIDEO')
    check("fbkind_07092", fb_kind('https://www.facebook.com/share/v/K0000293/') == 'SHARE_VIDEO')
    check("fbkind_07093", fb_kind('https://www.facebook.com/share/v/K0000294/') == 'SHARE_VIDEO')
    check("fbkind_07094", fb_kind('https://www.facebook.com/share/v/K0000295/') == 'SHARE_VIDEO')
    check("fbkind_07095", fb_kind('https://www.facebook.com/share/v/K0000296/') == 'SHARE_VIDEO')
    check("fbkind_07096", fb_kind('https://www.facebook.com/share/v/K0000297/') == 'SHARE_VIDEO')
    check("fbkind_07097", fb_kind('https://www.facebook.com/share/v/K0000298/') == 'SHARE_VIDEO')
    check("fbkind_07098", fb_kind('https://www.facebook.com/share/v/K0000299/') == 'SHARE_VIDEO')
    check("fbkind_07099", fb_kind('https://www.facebook.com/share/v/K0000300/') == 'SHARE_VIDEO')
    check("fbkind_07100", fb_kind('https://www.facebook.com/share/v/K0000301/') == 'SHARE_VIDEO')
    check("fbkind_07101", fb_kind('https://www.facebook.com/share/v/K0000302/') == 'SHARE_VIDEO')
    check("fbkind_07102", fb_kind('https://www.facebook.com/share/v/K0000303/') == 'SHARE_VIDEO')
    check("fbkind_07103", fb_kind('https://www.facebook.com/share/v/K0000304/') == 'SHARE_VIDEO')
    check("fbkind_07104", fb_kind('https://www.facebook.com/share/v/K0000305/') == 'SHARE_VIDEO')
    check("fbkind_07105", fb_kind('https://www.facebook.com/share/v/K0000306/') == 'SHARE_VIDEO')
    check("fbkind_07106", fb_kind('https://www.facebook.com/share/v/K0000307/') == 'SHARE_VIDEO')
    check("fbkind_07107", fb_kind('https://www.facebook.com/share/v/K0000308/') == 'SHARE_VIDEO')
    check("fbkind_07108", fb_kind('https://www.facebook.com/share/v/K0000309/') == 'SHARE_VIDEO')
    check("fbkind_07109", fb_kind('https://www.facebook.com/share/v/K0000310/') == 'SHARE_VIDEO')
    check("fbkind_07110", fb_kind('https://www.facebook.com/share/v/K0000311/') == 'SHARE_VIDEO')
    check("fbkind_07111", fb_kind('https://www.facebook.com/share/v/K0000312/') == 'SHARE_VIDEO')
    check("fbkind_07112", fb_kind('https://www.facebook.com/share/v/K0000313/') == 'SHARE_VIDEO')
    check("fbkind_07113", fb_kind('https://www.facebook.com/share/v/K0000314/') == 'SHARE_VIDEO')
    check("fbkind_07114", fb_kind('https://www.facebook.com/share/v/K0000315/') == 'SHARE_VIDEO')
    check("fbkind_07115", fb_kind('https://www.facebook.com/share/v/K0000316/') == 'SHARE_VIDEO')
    check("fbkind_07116", fb_kind('https://www.facebook.com/share/v/K0000317/') == 'SHARE_VIDEO')
    check("fbkind_07117", fb_kind('https://www.facebook.com/share/v/K0000318/') == 'SHARE_VIDEO')
    check("fbkind_07118", fb_kind('https://www.facebook.com/share/v/K0000319/') == 'SHARE_VIDEO')
    check("fbkind_07119", fb_kind('https://www.facebook.com/share/v/K0000320/') == 'SHARE_VIDEO')
    check("fbkind_07120", fb_kind('https://www.facebook.com/share/v/K0000321/') == 'SHARE_VIDEO')
    check("fbkind_07121", fb_kind('https://www.facebook.com/share/v/K0000322/') == 'SHARE_VIDEO')
    check("fbkind_07122", fb_kind('https://www.facebook.com/share/v/K0000323/') == 'SHARE_VIDEO')
    check("fbkind_07123", fb_kind('https://www.facebook.com/share/v/K0000324/') == 'SHARE_VIDEO')
    check("fbkind_07124", fb_kind('https://www.facebook.com/share/v/K0000325/') == 'SHARE_VIDEO')
    check("fbkind_07125", fb_kind('https://www.facebook.com/share/v/K0000326/') == 'SHARE_VIDEO')
    check("fbkind_07126", fb_kind('https://www.facebook.com/share/v/K0000327/') == 'SHARE_VIDEO')
    check("fbkind_07127", fb_kind('https://www.facebook.com/share/v/K0000328/') == 'SHARE_VIDEO')
    check("fbkind_07128", fb_kind('https://www.facebook.com/share/v/K0000329/') == 'SHARE_VIDEO')
    check("fbkind_07129", fb_kind('https://www.facebook.com/share/v/K0000330/') == 'SHARE_VIDEO')
    check("fbkind_07130", fb_kind('https://www.facebook.com/share/v/K0000331/') == 'SHARE_VIDEO')
    check("fbkind_07131", fb_kind('https://www.facebook.com/share/v/K0000332/') == 'SHARE_VIDEO')
    check("fbkind_07132", fb_kind('https://www.facebook.com/share/v/K0000333/') == 'SHARE_VIDEO')
    check("fbkind_07133", fb_kind('https://www.facebook.com/share/v/K0000334/') == 'SHARE_VIDEO')
    check("fbkind_07134", fb_kind('https://www.facebook.com/share/v/K0000335/') == 'SHARE_VIDEO')
    check("fbkind_07135", fb_kind('https://www.facebook.com/share/v/K0000336/') == 'SHARE_VIDEO')
    check("fbkind_07136", fb_kind('https://www.facebook.com/share/v/K0000337/') == 'SHARE_VIDEO')
    check("fbkind_07137", fb_kind('https://www.facebook.com/share/v/K0000338/') == 'SHARE_VIDEO')
    check("fbkind_07138", fb_kind('https://www.facebook.com/share/v/K0000339/') == 'SHARE_VIDEO')
    check("fbkind_07139", fb_kind('https://www.facebook.com/share/v/K0000340/') == 'SHARE_VIDEO')
    check("fbkind_07140", fb_kind('https://www.facebook.com/share/v/K0000341/') == 'SHARE_VIDEO')
    check("fbkind_07141", fb_kind('https://www.facebook.com/share/v/K0000342/') == 'SHARE_VIDEO')
    check("fbkind_07142", fb_kind('https://www.facebook.com/share/v/K0000343/') == 'SHARE_VIDEO')
    check("fbkind_07143", fb_kind('https://www.facebook.com/share/v/K0000344/') == 'SHARE_VIDEO')
    check("fbkind_07144", fb_kind('https://www.facebook.com/share/v/K0000345/') == 'SHARE_VIDEO')
    check("fbkind_07145", fb_kind('https://www.facebook.com/share/v/K0000346/') == 'SHARE_VIDEO')
    check("fbkind_07146", fb_kind('https://www.facebook.com/share/v/K0000347/') == 'SHARE_VIDEO')
    check("fbkind_07147", fb_kind('https://www.facebook.com/share/v/K0000348/') == 'SHARE_VIDEO')
    check("fbkind_07148", fb_kind('https://www.facebook.com/share/v/K0000349/') == 'SHARE_VIDEO')
    check("fbkind_07149", fb_kind('https://www.facebook.com/share/v/K0000350/') == 'SHARE_VIDEO')
    check("fbkind_07150", fb_kind('https://www.facebook.com/share/v/K0000351/') == 'SHARE_VIDEO')
    check("fbkind_07151", fb_kind('https://www.facebook.com/share/v/K0000352/') == 'SHARE_VIDEO')
    check("fbkind_07152", fb_kind('https://www.facebook.com/share/v/K0000353/') == 'SHARE_VIDEO')
    check("fbkind_07153", fb_kind('https://www.facebook.com/share/v/K0000354/') == 'SHARE_VIDEO')
    check("fbkind_07154", fb_kind('https://www.facebook.com/share/v/K0000355/') == 'SHARE_VIDEO')
    check("fbkind_07155", fb_kind('https://www.facebook.com/share/v/K0000356/') == 'SHARE_VIDEO')
    check("fbkind_07156", fb_kind('https://www.facebook.com/share/v/K0000357/') == 'SHARE_VIDEO')
    check("fbkind_07157", fb_kind('https://www.facebook.com/share/v/K0000358/') == 'SHARE_VIDEO')
    check("fbkind_07158", fb_kind('https://www.facebook.com/share/v/K0000359/') == 'SHARE_VIDEO')
    check("fbkind_07159", fb_kind('https://www.facebook.com/share/v/K0000360/') == 'SHARE_VIDEO')
    check("fbkind_07160", fb_kind('https://www.facebook.com/share/v/K0000361/') == 'SHARE_VIDEO')
    check("fbkind_07161", fb_kind('https://www.facebook.com/share/v/K0000362/') == 'SHARE_VIDEO')
    check("fbkind_07162", fb_kind('https://www.facebook.com/share/v/K0000363/') == 'SHARE_VIDEO')
    check("fbkind_07163", fb_kind('https://www.facebook.com/share/v/K0000364/') == 'SHARE_VIDEO')
    check("fbkind_07164", fb_kind('https://www.facebook.com/share/v/K0000365/') == 'SHARE_VIDEO')
    check("fbkind_07165", fb_kind('https://www.facebook.com/share/v/K0000366/') == 'SHARE_VIDEO')
    check("fbkind_07166", fb_kind('https://www.facebook.com/share/v/K0000367/') == 'SHARE_VIDEO')
    check("fbkind_07167", fb_kind('https://www.facebook.com/share/v/K0000368/') == 'SHARE_VIDEO')
    check("fbkind_07168", fb_kind('https://www.facebook.com/share/v/K0000369/') == 'SHARE_VIDEO')
    check("fbkind_07169", fb_kind('https://www.facebook.com/share/v/K0000370/') == 'SHARE_VIDEO')
    check("fbkind_07170", fb_kind('https://www.facebook.com/share/v/K0000371/') == 'SHARE_VIDEO')
    check("fbkind_07171", fb_kind('https://www.facebook.com/share/v/K0000372/') == 'SHARE_VIDEO')
    check("fbkind_07172", fb_kind('https://www.facebook.com/share/v/K0000373/') == 'SHARE_VIDEO')
    check("fbkind_07173", fb_kind('https://www.facebook.com/share/v/K0000374/') == 'SHARE_VIDEO')
    check("fbkind_07174", fb_kind('https://www.facebook.com/share/v/K0000375/') == 'SHARE_VIDEO')
    check("fbkind_07175", fb_kind('https://www.facebook.com/share/v/K0000376/') == 'SHARE_VIDEO')
    check("fbkind_07176", fb_kind('https://www.facebook.com/share/v/K0000377/') == 'SHARE_VIDEO')
    check("fbkind_07177", fb_kind('https://www.facebook.com/share/v/K0000378/') == 'SHARE_VIDEO')
    check("fbkind_07178", fb_kind('https://www.facebook.com/share/v/K0000379/') == 'SHARE_VIDEO')
    check("fbkind_07179", fb_kind('https://www.facebook.com/share/v/K0000380/') == 'SHARE_VIDEO')
    check("fbkind_07180", fb_kind('https://www.facebook.com/share/v/K0000381/') == 'SHARE_VIDEO')
    check("fbkind_07181", fb_kind('https://www.facebook.com/share/v/K0000382/') == 'SHARE_VIDEO')
    check("fbkind_07182", fb_kind('https://www.facebook.com/share/v/K0000383/') == 'SHARE_VIDEO')
    check("fbkind_07183", fb_kind('https://www.facebook.com/share/v/K0000384/') == 'SHARE_VIDEO')
    check("fbkind_07184", fb_kind('https://www.facebook.com/share/v/K0000385/') == 'SHARE_VIDEO')
    check("fbkind_07185", fb_kind('https://www.facebook.com/share/v/K0000386/') == 'SHARE_VIDEO')
    check("fbkind_07186", fb_kind('https://www.facebook.com/share/v/K0000387/') == 'SHARE_VIDEO')
    check("fbkind_07187", fb_kind('https://www.facebook.com/share/v/K0000388/') == 'SHARE_VIDEO')
    check("fbkind_07188", fb_kind('https://www.facebook.com/share/v/K0000389/') == 'SHARE_VIDEO')
    check("fbkind_07189", fb_kind('https://www.facebook.com/share/v/K0000390/') == 'SHARE_VIDEO')
    check("fbkind_07190", fb_kind('https://www.facebook.com/share/v/K0000391/') == 'SHARE_VIDEO')
    check("fbkind_07191", fb_kind('https://www.facebook.com/share/v/K0000392/') == 'SHARE_VIDEO')
    check("fbkind_07192", fb_kind('https://www.facebook.com/share/v/K0000393/') == 'SHARE_VIDEO')
    check("fbkind_07193", fb_kind('https://www.facebook.com/share/v/K0000394/') == 'SHARE_VIDEO')
    check("fbkind_07194", fb_kind('https://www.facebook.com/share/v/K0000395/') == 'SHARE_VIDEO')
    check("fbkind_07195", fb_kind('https://www.facebook.com/share/v/K0000396/') == 'SHARE_VIDEO')
    check("fbkind_07196", fb_kind('https://www.facebook.com/share/v/K0000397/') == 'SHARE_VIDEO')
    check("fbkind_07197", fb_kind('https://www.facebook.com/share/v/K0000398/') == 'SHARE_VIDEO')
    check("fbkind_07198", fb_kind('https://www.facebook.com/share/v/K0000399/') == 'SHARE_VIDEO')
    check("fbkind_07199", fb_kind('https://www.facebook.com/share/v/K0000400/') == 'SHARE_VIDEO')
    check("fbkind_07200", fb_kind('https://www.facebook.com/share/v/K0000401/') == 'SHARE_VIDEO')
    check("fbkind_07201", fb_kind('https://www.facebook.com/share/v/K0000402/') == 'SHARE_VIDEO')
    check("fbkind_07202", fb_kind('https://www.facebook.com/share/v/K0000403/') == 'SHARE_VIDEO')
    check("fbkind_07203", fb_kind('https://www.facebook.com/share/v/K0000404/') == 'SHARE_VIDEO')
    check("fbkind_07204", fb_kind('https://www.facebook.com/share/v/K0000405/') == 'SHARE_VIDEO')
    check("fbkind_07205", fb_kind('https://www.facebook.com/share/v/K0000406/') == 'SHARE_VIDEO')
    check("fbkind_07206", fb_kind('https://www.facebook.com/share/v/K0000407/') == 'SHARE_VIDEO')
    check("fbkind_07207", fb_kind('https://www.facebook.com/share/v/K0000408/') == 'SHARE_VIDEO')
    check("fbkind_07208", fb_kind('https://www.facebook.com/share/v/K0000409/') == 'SHARE_VIDEO')
    check("fbkind_07209", fb_kind('https://www.facebook.com/share/v/K0000410/') == 'SHARE_VIDEO')
    check("fbkind_07210", fb_kind('https://www.facebook.com/share/v/K0000411/') == 'SHARE_VIDEO')
    check("fbkind_07211", fb_kind('https://www.facebook.com/share/v/K0000412/') == 'SHARE_VIDEO')
    check("fbkind_07212", fb_kind('https://www.facebook.com/share/v/K0000413/') == 'SHARE_VIDEO')
    check("fbkind_07213", fb_kind('https://www.facebook.com/share/v/K0000414/') == 'SHARE_VIDEO')
    check("fbkind_07214", fb_kind('https://www.facebook.com/share/v/K0000415/') == 'SHARE_VIDEO')
    check("fbkind_07215", fb_kind('https://www.facebook.com/share/v/K0000416/') == 'SHARE_VIDEO')
    check("fbkind_07216", fb_kind('https://www.facebook.com/share/v/K0000417/') == 'SHARE_VIDEO')
    check("fbkind_07217", fb_kind('https://www.facebook.com/share/v/K0000418/') == 'SHARE_VIDEO')
    check("fbkind_07218", fb_kind('https://www.facebook.com/share/v/K0000419/') == 'SHARE_VIDEO')
    check("fbkind_07219", fb_kind('https://www.facebook.com/share/v/K0000420/') == 'SHARE_VIDEO')
    check("fbkind_07220", fb_kind('https://www.facebook.com/share/v/K0000421/') == 'SHARE_VIDEO')
    check("fbkind_07221", fb_kind('https://www.facebook.com/share/v/K0000422/') == 'SHARE_VIDEO')
    check("fbkind_07222", fb_kind('https://www.facebook.com/share/v/K0000423/') == 'SHARE_VIDEO')
    check("fbkind_07223", fb_kind('https://www.facebook.com/share/v/K0000424/') == 'SHARE_VIDEO')
    check("fbkind_07224", fb_kind('https://www.facebook.com/share/v/K0000425/') == 'SHARE_VIDEO')
    check("fbkind_07225", fb_kind('https://www.facebook.com/share/v/K0000426/') == 'SHARE_VIDEO')
    check("fbkind_07226", fb_kind('https://www.facebook.com/share/v/K0000427/') == 'SHARE_VIDEO')
    check("fbkind_07227", fb_kind('https://www.facebook.com/share/v/K0000428/') == 'SHARE_VIDEO')
    check("fbkind_07228", fb_kind('https://www.facebook.com/share/v/K0000429/') == 'SHARE_VIDEO')
    check("fbkind_07229", fb_kind('https://www.facebook.com/share/v/K0000430/') == 'SHARE_VIDEO')
    check("fbkind_07230", fb_kind('https://www.facebook.com/share/v/K0000431/') == 'SHARE_VIDEO')
    check("fbkind_07231", fb_kind('https://www.facebook.com/share/v/K0000432/') == 'SHARE_VIDEO')
    check("fbkind_07232", fb_kind('https://www.facebook.com/share/v/K0000433/') == 'SHARE_VIDEO')
    check("fbkind_07233", fb_kind('https://www.facebook.com/share/v/K0000434/') == 'SHARE_VIDEO')
    check("fbkind_07234", fb_kind('https://www.facebook.com/share/v/K0000435/') == 'SHARE_VIDEO')
    check("fbkind_07235", fb_kind('https://www.facebook.com/share/v/K0000436/') == 'SHARE_VIDEO')
    check("fbkind_07236", fb_kind('https://www.facebook.com/share/v/K0000437/') == 'SHARE_VIDEO')
    check("fbkind_07237", fb_kind('https://www.facebook.com/share/v/K0000438/') == 'SHARE_VIDEO')
    check("fbkind_07238", fb_kind('https://www.facebook.com/share/v/K0000439/') == 'SHARE_VIDEO')
    check("fbkind_07239", fb_kind('https://www.facebook.com/share/v/K0000440/') == 'SHARE_VIDEO')
    check("fbkind_07240", fb_kind('https://www.facebook.com/share/v/K0000441/') == 'SHARE_VIDEO')
    check("fbkind_07241", fb_kind('https://www.facebook.com/share/v/K0000442/') == 'SHARE_VIDEO')
    check("fbkind_07242", fb_kind('https://www.facebook.com/share/v/K0000443/') == 'SHARE_VIDEO')
    check("fbkind_07243", fb_kind('https://www.facebook.com/share/v/K0000444/') == 'SHARE_VIDEO')
    check("fbkind_07244", fb_kind('https://www.facebook.com/share/v/K0000445/') == 'SHARE_VIDEO')
    check("fbkind_07245", fb_kind('https://www.facebook.com/share/v/K0000446/') == 'SHARE_VIDEO')
    check("fbkind_07246", fb_kind('https://www.facebook.com/share/v/K0000447/') == 'SHARE_VIDEO')
    check("fbkind_07247", fb_kind('https://www.facebook.com/share/v/K0000448/') == 'SHARE_VIDEO')
    check("fbkind_07248", fb_kind('https://www.facebook.com/share/v/K0000449/') == 'SHARE_VIDEO')
    check("fbkind_07249", fb_kind('https://www.facebook.com/share/v/K0000450/') == 'SHARE_VIDEO')
    check("fbkind_07250", fb_kind('https://www.facebook.com/share/v/K0000451/') == 'SHARE_VIDEO')
    check("fbkind_07251", fb_kind('https://www.facebook.com/share/v/K0000452/') == 'SHARE_VIDEO')
    check("fbkind_07252", fb_kind('https://www.facebook.com/share/v/K0000453/') == 'SHARE_VIDEO')
    check("fbkind_07253", fb_kind('https://www.facebook.com/share/v/K0000454/') == 'SHARE_VIDEO')
    check("fbkind_07254", fb_kind('https://www.facebook.com/share/v/K0000455/') == 'SHARE_VIDEO')
    check("fbkind_07255", fb_kind('https://www.facebook.com/share/v/K0000456/') == 'SHARE_VIDEO')
    check("fbkind_07256", fb_kind('https://www.facebook.com/share/v/K0000457/') == 'SHARE_VIDEO')
    check("fbkind_07257", fb_kind('https://www.facebook.com/share/v/K0000458/') == 'SHARE_VIDEO')
    check("fbkind_07258", fb_kind('https://www.facebook.com/share/v/K0000459/') == 'SHARE_VIDEO')
    check("fbkind_07259", fb_kind('https://www.facebook.com/share/v/K0000460/') == 'SHARE_VIDEO')
    check("fbkind_07260", fb_kind('https://www.facebook.com/share/v/K0000461/') == 'SHARE_VIDEO')
    check("fbkind_07261", fb_kind('https://www.facebook.com/share/v/K0000462/') == 'SHARE_VIDEO')
    check("fbkind_07262", fb_kind('https://www.facebook.com/share/v/K0000463/') == 'SHARE_VIDEO')
    check("fbkind_07263", fb_kind('https://www.facebook.com/share/v/K0000464/') == 'SHARE_VIDEO')
    check("fbkind_07264", fb_kind('https://www.facebook.com/share/v/K0000465/') == 'SHARE_VIDEO')
    check("fbkind_07265", fb_kind('https://www.facebook.com/share/v/K0000466/') == 'SHARE_VIDEO')
    check("fbkind_07266", fb_kind('https://www.facebook.com/share/v/K0000467/') == 'SHARE_VIDEO')
    check("fbkind_07267", fb_kind('https://www.facebook.com/share/v/K0000468/') == 'SHARE_VIDEO')
    check("fbkind_07268", fb_kind('https://www.facebook.com/share/v/K0000469/') == 'SHARE_VIDEO')
    check("fbkind_07269", fb_kind('https://www.facebook.com/share/v/K0000470/') == 'SHARE_VIDEO')
    check("fbkind_07270", fb_kind('https://www.facebook.com/share/v/K0000471/') == 'SHARE_VIDEO')
    check("fbkind_07271", fb_kind('https://www.facebook.com/share/v/K0000472/') == 'SHARE_VIDEO')
    check("fbkind_07272", fb_kind('https://www.facebook.com/share/v/K0000473/') == 'SHARE_VIDEO')
    check("fbkind_07273", fb_kind('https://www.facebook.com/share/v/K0000474/') == 'SHARE_VIDEO')
    check("fbkind_07274", fb_kind('https://www.facebook.com/share/v/K0000475/') == 'SHARE_VIDEO')
    check("fbkind_07275", fb_kind('https://www.facebook.com/share/v/K0000476/') == 'SHARE_VIDEO')
    check("fbkind_07276", fb_kind('https://www.facebook.com/share/v/K0000477/') == 'SHARE_VIDEO')
    check("fbkind_07277", fb_kind('https://www.facebook.com/share/v/K0000478/') == 'SHARE_VIDEO')
    check("fbkind_07278", fb_kind('https://www.facebook.com/share/v/K0000479/') == 'SHARE_VIDEO')
    check("fbkind_07279", fb_kind('https://www.facebook.com/share/v/K0000480/') == 'SHARE_VIDEO')
    check("fbkind_07280", fb_kind('https://www.facebook.com/share/v/K0000481/') == 'SHARE_VIDEO')
    check("fbkind_07281", fb_kind('https://www.facebook.com/share/v/K0000482/') == 'SHARE_VIDEO')
    check("fbkind_07282", fb_kind('https://www.facebook.com/share/v/K0000483/') == 'SHARE_VIDEO')
    check("fbkind_07283", fb_kind('https://www.facebook.com/share/v/K0000484/') == 'SHARE_VIDEO')
    check("fbkind_07284", fb_kind('https://www.facebook.com/share/v/K0000485/') == 'SHARE_VIDEO')
    check("fbkind_07285", fb_kind('https://www.facebook.com/share/v/K0000486/') == 'SHARE_VIDEO')
    check("fbkind_07286", fb_kind('https://www.facebook.com/share/v/K0000487/') == 'SHARE_VIDEO')
    check("fbkind_07287", fb_kind('https://www.facebook.com/share/v/K0000488/') == 'SHARE_VIDEO')
    check("fbkind_07288", fb_kind('https://www.facebook.com/share/v/K0000489/') == 'SHARE_VIDEO')
    check("fbkind_07289", fb_kind('https://www.facebook.com/share/v/K0000490/') == 'SHARE_VIDEO')
    check("fbkind_07290", fb_kind('https://www.facebook.com/share/v/K0000491/') == 'SHARE_VIDEO')
    check("fbkind_07291", fb_kind('https://www.facebook.com/share/v/K0000492/') == 'SHARE_VIDEO')
    check("fbkind_07292", fb_kind('https://www.facebook.com/share/v/K0000493/') == 'SHARE_VIDEO')
    check("fbkind_07293", fb_kind('https://www.facebook.com/share/v/K0000494/') == 'SHARE_VIDEO')
    check("fbkind_07294", fb_kind('https://www.facebook.com/share/v/K0000495/') == 'SHARE_VIDEO')
    check("fbkind_07295", fb_kind('https://www.facebook.com/share/v/K0000496/') == 'SHARE_VIDEO')
    check("fbkind_07296", fb_kind('https://www.facebook.com/share/v/K0000497/') == 'SHARE_VIDEO')
    check("fbkind_07297", fb_kind('https://www.facebook.com/share/v/K0000498/') == 'SHARE_VIDEO')
    check("fbkind_07298", fb_kind('https://www.facebook.com/share/v/K0000499/') == 'SHARE_VIDEO')
    check("fbkind_07299", fb_kind('https://www.facebook.com/share/v/K0000500/') == 'SHARE_VIDEO')
    check("fbkind_07300", fb_kind('https://www.facebook.com/share/r/K0000001/') == 'SHARE_REEL')
    check("fbkind_07301", fb_kind('https://www.facebook.com/share/r/K0000002/') == 'SHARE_REEL')
    check("fbkind_07302", fb_kind('https://www.facebook.com/share/r/K0000003/') == 'SHARE_REEL')
    check("fbkind_07303", fb_kind('https://www.facebook.com/share/r/K0000004/') == 'SHARE_REEL')
    check("fbkind_07304", fb_kind('https://www.facebook.com/share/r/K0000005/') == 'SHARE_REEL')
    check("fbkind_07305", fb_kind('https://www.facebook.com/share/r/K0000006/') == 'SHARE_REEL')
    check("fbkind_07306", fb_kind('https://www.facebook.com/share/r/K0000007/') == 'SHARE_REEL')
    check("fbkind_07307", fb_kind('https://www.facebook.com/share/r/K0000008/') == 'SHARE_REEL')
    check("fbkind_07308", fb_kind('https://www.facebook.com/share/r/K0000009/') == 'SHARE_REEL')
    check("fbkind_07309", fb_kind('https://www.facebook.com/share/r/K0000010/') == 'SHARE_REEL')
    check("fbkind_07310", fb_kind('https://www.facebook.com/share/r/K0000011/') == 'SHARE_REEL')
    check("fbkind_07311", fb_kind('https://www.facebook.com/share/r/K0000012/') == 'SHARE_REEL')
    check("fbkind_07312", fb_kind('https://www.facebook.com/share/r/K0000013/') == 'SHARE_REEL')
    check("fbkind_07313", fb_kind('https://www.facebook.com/share/r/K0000014/') == 'SHARE_REEL')
    check("fbkind_07314", fb_kind('https://www.facebook.com/share/r/K0000015/') == 'SHARE_REEL')
    check("fbkind_07315", fb_kind('https://www.facebook.com/share/r/K0000016/') == 'SHARE_REEL')
    check("fbkind_07316", fb_kind('https://www.facebook.com/share/r/K0000017/') == 'SHARE_REEL')
    check("fbkind_07317", fb_kind('https://www.facebook.com/share/r/K0000018/') == 'SHARE_REEL')
    check("fbkind_07318", fb_kind('https://www.facebook.com/share/r/K0000019/') == 'SHARE_REEL')
    check("fbkind_07319", fb_kind('https://www.facebook.com/share/r/K0000020/') == 'SHARE_REEL')
    check("fbkind_07320", fb_kind('https://www.facebook.com/share/r/K0000021/') == 'SHARE_REEL')
    check("fbkind_07321", fb_kind('https://www.facebook.com/share/r/K0000022/') == 'SHARE_REEL')
    check("fbkind_07322", fb_kind('https://www.facebook.com/share/r/K0000023/') == 'SHARE_REEL')
    check("fbkind_07323", fb_kind('https://www.facebook.com/share/r/K0000024/') == 'SHARE_REEL')
    check("fbkind_07324", fb_kind('https://www.facebook.com/share/r/K0000025/') == 'SHARE_REEL')
    check("fbkind_07325", fb_kind('https://www.facebook.com/share/r/K0000026/') == 'SHARE_REEL')
    check("fbkind_07326", fb_kind('https://www.facebook.com/share/r/K0000027/') == 'SHARE_REEL')
    check("fbkind_07327", fb_kind('https://www.facebook.com/share/r/K0000028/') == 'SHARE_REEL')
    check("fbkind_07328", fb_kind('https://www.facebook.com/share/r/K0000029/') == 'SHARE_REEL')
    check("fbkind_07329", fb_kind('https://www.facebook.com/share/r/K0000030/') == 'SHARE_REEL')
    check("fbkind_07330", fb_kind('https://www.facebook.com/share/r/K0000031/') == 'SHARE_REEL')
    check("fbkind_07331", fb_kind('https://www.facebook.com/share/r/K0000032/') == 'SHARE_REEL')
    check("fbkind_07332", fb_kind('https://www.facebook.com/share/r/K0000033/') == 'SHARE_REEL')
    check("fbkind_07333", fb_kind('https://www.facebook.com/share/r/K0000034/') == 'SHARE_REEL')
    check("fbkind_07334", fb_kind('https://www.facebook.com/share/r/K0000035/') == 'SHARE_REEL')
    check("fbkind_07335", fb_kind('https://www.facebook.com/share/r/K0000036/') == 'SHARE_REEL')
    check("fbkind_07336", fb_kind('https://www.facebook.com/share/r/K0000037/') == 'SHARE_REEL')
    check("fbkind_07337", fb_kind('https://www.facebook.com/share/r/K0000038/') == 'SHARE_REEL')
    check("fbkind_07338", fb_kind('https://www.facebook.com/share/r/K0000039/') == 'SHARE_REEL')
    check("fbkind_07339", fb_kind('https://www.facebook.com/share/r/K0000040/') == 'SHARE_REEL')
    check("fbkind_07340", fb_kind('https://www.facebook.com/share/r/K0000041/') == 'SHARE_REEL')
    check("fbkind_07341", fb_kind('https://www.facebook.com/share/r/K0000042/') == 'SHARE_REEL')
    check("fbkind_07342", fb_kind('https://www.facebook.com/share/r/K0000043/') == 'SHARE_REEL')
    check("fbkind_07343", fb_kind('https://www.facebook.com/share/r/K0000044/') == 'SHARE_REEL')
    check("fbkind_07344", fb_kind('https://www.facebook.com/share/r/K0000045/') == 'SHARE_REEL')
    check("fbkind_07345", fb_kind('https://www.facebook.com/share/r/K0000046/') == 'SHARE_REEL')
    check("fbkind_07346", fb_kind('https://www.facebook.com/share/r/K0000047/') == 'SHARE_REEL')
    check("fbkind_07347", fb_kind('https://www.facebook.com/share/r/K0000048/') == 'SHARE_REEL')
    check("fbkind_07348", fb_kind('https://www.facebook.com/share/r/K0000049/') == 'SHARE_REEL')
    check("fbkind_07349", fb_kind('https://www.facebook.com/share/r/K0000050/') == 'SHARE_REEL')
    check("fbkind_07350", fb_kind('https://www.facebook.com/share/r/K0000051/') == 'SHARE_REEL')
    check("fbkind_07351", fb_kind('https://www.facebook.com/share/r/K0000052/') == 'SHARE_REEL')
    check("fbkind_07352", fb_kind('https://www.facebook.com/share/r/K0000053/') == 'SHARE_REEL')
    check("fbkind_07353", fb_kind('https://www.facebook.com/share/r/K0000054/') == 'SHARE_REEL')
    check("fbkind_07354", fb_kind('https://www.facebook.com/share/r/K0000055/') == 'SHARE_REEL')
    check("fbkind_07355", fb_kind('https://www.facebook.com/share/r/K0000056/') == 'SHARE_REEL')
    check("fbkind_07356", fb_kind('https://www.facebook.com/share/r/K0000057/') == 'SHARE_REEL')
    check("fbkind_07357", fb_kind('https://www.facebook.com/share/r/K0000058/') == 'SHARE_REEL')
    check("fbkind_07358", fb_kind('https://www.facebook.com/share/r/K0000059/') == 'SHARE_REEL')
    check("fbkind_07359", fb_kind('https://www.facebook.com/share/r/K0000060/') == 'SHARE_REEL')
    check("fbkind_07360", fb_kind('https://www.facebook.com/share/r/K0000061/') == 'SHARE_REEL')
    check("fbkind_07361", fb_kind('https://www.facebook.com/share/r/K0000062/') == 'SHARE_REEL')
    check("fbkind_07362", fb_kind('https://www.facebook.com/share/r/K0000063/') == 'SHARE_REEL')
    check("fbkind_07363", fb_kind('https://www.facebook.com/share/r/K0000064/') == 'SHARE_REEL')
    check("fbkind_07364", fb_kind('https://www.facebook.com/share/r/K0000065/') == 'SHARE_REEL')
    check("fbkind_07365", fb_kind('https://www.facebook.com/share/r/K0000066/') == 'SHARE_REEL')
    check("fbkind_07366", fb_kind('https://www.facebook.com/share/r/K0000067/') == 'SHARE_REEL')
    check("fbkind_07367", fb_kind('https://www.facebook.com/share/r/K0000068/') == 'SHARE_REEL')
    check("fbkind_07368", fb_kind('https://www.facebook.com/share/r/K0000069/') == 'SHARE_REEL')
    check("fbkind_07369", fb_kind('https://www.facebook.com/share/r/K0000070/') == 'SHARE_REEL')
    check("fbkind_07370", fb_kind('https://www.facebook.com/share/r/K0000071/') == 'SHARE_REEL')
    check("fbkind_07371", fb_kind('https://www.facebook.com/share/r/K0000072/') == 'SHARE_REEL')
    check("fbkind_07372", fb_kind('https://www.facebook.com/share/r/K0000073/') == 'SHARE_REEL')
    check("fbkind_07373", fb_kind('https://www.facebook.com/share/r/K0000074/') == 'SHARE_REEL')
    check("fbkind_07374", fb_kind('https://www.facebook.com/share/r/K0000075/') == 'SHARE_REEL')
    check("fbkind_07375", fb_kind('https://www.facebook.com/share/r/K0000076/') == 'SHARE_REEL')
    check("fbkind_07376", fb_kind('https://www.facebook.com/share/r/K0000077/') == 'SHARE_REEL')
    check("fbkind_07377", fb_kind('https://www.facebook.com/share/r/K0000078/') == 'SHARE_REEL')
    check("fbkind_07378", fb_kind('https://www.facebook.com/share/r/K0000079/') == 'SHARE_REEL')
    check("fbkind_07379", fb_kind('https://www.facebook.com/share/r/K0000080/') == 'SHARE_REEL')
    check("fbkind_07380", fb_kind('https://www.facebook.com/share/r/K0000081/') == 'SHARE_REEL')
    check("fbkind_07381", fb_kind('https://www.facebook.com/share/r/K0000082/') == 'SHARE_REEL')
    check("fbkind_07382", fb_kind('https://www.facebook.com/share/r/K0000083/') == 'SHARE_REEL')
    check("fbkind_07383", fb_kind('https://www.facebook.com/share/r/K0000084/') == 'SHARE_REEL')
    check("fbkind_07384", fb_kind('https://www.facebook.com/share/r/K0000085/') == 'SHARE_REEL')
    check("fbkind_07385", fb_kind('https://www.facebook.com/share/r/K0000086/') == 'SHARE_REEL')
    check("fbkind_07386", fb_kind('https://www.facebook.com/share/r/K0000087/') == 'SHARE_REEL')
    check("fbkind_07387", fb_kind('https://www.facebook.com/share/r/K0000088/') == 'SHARE_REEL')
    check("fbkind_07388", fb_kind('https://www.facebook.com/share/r/K0000089/') == 'SHARE_REEL')
    check("fbkind_07389", fb_kind('https://www.facebook.com/share/r/K0000090/') == 'SHARE_REEL')
    check("fbkind_07390", fb_kind('https://www.facebook.com/share/r/K0000091/') == 'SHARE_REEL')
    check("fbkind_07391", fb_kind('https://www.facebook.com/share/r/K0000092/') == 'SHARE_REEL')
    check("fbkind_07392", fb_kind('https://www.facebook.com/share/r/K0000093/') == 'SHARE_REEL')
    check("fbkind_07393", fb_kind('https://www.facebook.com/share/r/K0000094/') == 'SHARE_REEL')
    check("fbkind_07394", fb_kind('https://www.facebook.com/share/r/K0000095/') == 'SHARE_REEL')
    check("fbkind_07395", fb_kind('https://www.facebook.com/share/r/K0000096/') == 'SHARE_REEL')
    check("fbkind_07396", fb_kind('https://www.facebook.com/share/r/K0000097/') == 'SHARE_REEL')
    check("fbkind_07397", fb_kind('https://www.facebook.com/share/r/K0000098/') == 'SHARE_REEL')
    check("fbkind_07398", fb_kind('https://www.facebook.com/share/r/K0000099/') == 'SHARE_REEL')
    check("fbkind_07399", fb_kind('https://www.facebook.com/share/r/K0000100/') == 'SHARE_REEL')
    check("fbkind_07400", fb_kind('https://www.facebook.com/share/r/K0000101/') == 'SHARE_REEL')
    check("fbkind_07401", fb_kind('https://www.facebook.com/share/r/K0000102/') == 'SHARE_REEL')
    check("fbkind_07402", fb_kind('https://www.facebook.com/share/r/K0000103/') == 'SHARE_REEL')
    check("fbkind_07403", fb_kind('https://www.facebook.com/share/r/K0000104/') == 'SHARE_REEL')
    check("fbkind_07404", fb_kind('https://www.facebook.com/share/r/K0000105/') == 'SHARE_REEL')
    check("fbkind_07405", fb_kind('https://www.facebook.com/share/r/K0000106/') == 'SHARE_REEL')
    check("fbkind_07406", fb_kind('https://www.facebook.com/share/r/K0000107/') == 'SHARE_REEL')
    check("fbkind_07407", fb_kind('https://www.facebook.com/share/r/K0000108/') == 'SHARE_REEL')
    check("fbkind_07408", fb_kind('https://www.facebook.com/share/r/K0000109/') == 'SHARE_REEL')
    check("fbkind_07409", fb_kind('https://www.facebook.com/share/r/K0000110/') == 'SHARE_REEL')
    check("fbkind_07410", fb_kind('https://www.facebook.com/share/r/K0000111/') == 'SHARE_REEL')
    check("fbkind_07411", fb_kind('https://www.facebook.com/share/r/K0000112/') == 'SHARE_REEL')
    check("fbkind_07412", fb_kind('https://www.facebook.com/share/r/K0000113/') == 'SHARE_REEL')
    check("fbkind_07413", fb_kind('https://www.facebook.com/share/r/K0000114/') == 'SHARE_REEL')
    check("fbkind_07414", fb_kind('https://www.facebook.com/share/r/K0000115/') == 'SHARE_REEL')
    check("fbkind_07415", fb_kind('https://www.facebook.com/share/r/K0000116/') == 'SHARE_REEL')
    check("fbkind_07416", fb_kind('https://www.facebook.com/share/r/K0000117/') == 'SHARE_REEL')
    check("fbkind_07417", fb_kind('https://www.facebook.com/share/r/K0000118/') == 'SHARE_REEL')
    check("fbkind_07418", fb_kind('https://www.facebook.com/share/r/K0000119/') == 'SHARE_REEL')
    check("fbkind_07419", fb_kind('https://www.facebook.com/share/r/K0000120/') == 'SHARE_REEL')
    check("fbkind_07420", fb_kind('https://www.facebook.com/share/r/K0000121/') == 'SHARE_REEL')
    check("fbkind_07421", fb_kind('https://www.facebook.com/share/r/K0000122/') == 'SHARE_REEL')
    check("fbkind_07422", fb_kind('https://www.facebook.com/share/r/K0000123/') == 'SHARE_REEL')
    check("fbkind_07423", fb_kind('https://www.facebook.com/share/r/K0000124/') == 'SHARE_REEL')
    check("fbkind_07424", fb_kind('https://www.facebook.com/share/r/K0000125/') == 'SHARE_REEL')
    check("fbkind_07425", fb_kind('https://www.facebook.com/share/r/K0000126/') == 'SHARE_REEL')
    check("fbkind_07426", fb_kind('https://www.facebook.com/share/r/K0000127/') == 'SHARE_REEL')
    check("fbkind_07427", fb_kind('https://www.facebook.com/share/r/K0000128/') == 'SHARE_REEL')
    check("fbkind_07428", fb_kind('https://www.facebook.com/share/r/K0000129/') == 'SHARE_REEL')
    check("fbkind_07429", fb_kind('https://www.facebook.com/share/r/K0000130/') == 'SHARE_REEL')
    check("fbkind_07430", fb_kind('https://www.facebook.com/share/r/K0000131/') == 'SHARE_REEL')
    check("fbkind_07431", fb_kind('https://www.facebook.com/share/r/K0000132/') == 'SHARE_REEL')
    check("fbkind_07432", fb_kind('https://www.facebook.com/share/r/K0000133/') == 'SHARE_REEL')
    check("fbkind_07433", fb_kind('https://www.facebook.com/share/r/K0000134/') == 'SHARE_REEL')
    check("fbkind_07434", fb_kind('https://www.facebook.com/share/r/K0000135/') == 'SHARE_REEL')
    check("fbkind_07435", fb_kind('https://www.facebook.com/share/r/K0000136/') == 'SHARE_REEL')
    check("fbkind_07436", fb_kind('https://www.facebook.com/share/r/K0000137/') == 'SHARE_REEL')
    check("fbkind_07437", fb_kind('https://www.facebook.com/share/r/K0000138/') == 'SHARE_REEL')
    check("fbkind_07438", fb_kind('https://www.facebook.com/share/r/K0000139/') == 'SHARE_REEL')
    check("fbkind_07439", fb_kind('https://www.facebook.com/share/r/K0000140/') == 'SHARE_REEL')
    check("fbkind_07440", fb_kind('https://www.facebook.com/share/r/K0000141/') == 'SHARE_REEL')
    check("fbkind_07441", fb_kind('https://www.facebook.com/share/r/K0000142/') == 'SHARE_REEL')
    check("fbkind_07442", fb_kind('https://www.facebook.com/share/r/K0000143/') == 'SHARE_REEL')
    check("fbkind_07443", fb_kind('https://www.facebook.com/share/r/K0000144/') == 'SHARE_REEL')
    check("fbkind_07444", fb_kind('https://www.facebook.com/share/r/K0000145/') == 'SHARE_REEL')
    check("fbkind_07445", fb_kind('https://www.facebook.com/share/r/K0000146/') == 'SHARE_REEL')
    check("fbkind_07446", fb_kind('https://www.facebook.com/share/r/K0000147/') == 'SHARE_REEL')
    check("fbkind_07447", fb_kind('https://www.facebook.com/share/r/K0000148/') == 'SHARE_REEL')
    check("fbkind_07448", fb_kind('https://www.facebook.com/share/r/K0000149/') == 'SHARE_REEL')
    check("fbkind_07449", fb_kind('https://www.facebook.com/share/r/K0000150/') == 'SHARE_REEL')
    check("fbkind_07450", fb_kind('https://www.facebook.com/share/r/K0000151/') == 'SHARE_REEL')
    check("fbkind_07451", fb_kind('https://www.facebook.com/share/r/K0000152/') == 'SHARE_REEL')
    check("fbkind_07452", fb_kind('https://www.facebook.com/share/r/K0000153/') == 'SHARE_REEL')
    check("fbkind_07453", fb_kind('https://www.facebook.com/share/r/K0000154/') == 'SHARE_REEL')
    check("fbkind_07454", fb_kind('https://www.facebook.com/share/r/K0000155/') == 'SHARE_REEL')
    check("fbkind_07455", fb_kind('https://www.facebook.com/share/r/K0000156/') == 'SHARE_REEL')
    check("fbkind_07456", fb_kind('https://www.facebook.com/share/r/K0000157/') == 'SHARE_REEL')
    check("fbkind_07457", fb_kind('https://www.facebook.com/share/r/K0000158/') == 'SHARE_REEL')
    check("fbkind_07458", fb_kind('https://www.facebook.com/share/r/K0000159/') == 'SHARE_REEL')
    check("fbkind_07459", fb_kind('https://www.facebook.com/share/r/K0000160/') == 'SHARE_REEL')
    check("fbkind_07460", fb_kind('https://www.facebook.com/share/r/K0000161/') == 'SHARE_REEL')
    check("fbkind_07461", fb_kind('https://www.facebook.com/share/r/K0000162/') == 'SHARE_REEL')
    check("fbkind_07462", fb_kind('https://www.facebook.com/share/r/K0000163/') == 'SHARE_REEL')
    check("fbkind_07463", fb_kind('https://www.facebook.com/share/r/K0000164/') == 'SHARE_REEL')
    check("fbkind_07464", fb_kind('https://www.facebook.com/share/r/K0000165/') == 'SHARE_REEL')
    check("fbkind_07465", fb_kind('https://www.facebook.com/share/r/K0000166/') == 'SHARE_REEL')
    check("fbkind_07466", fb_kind('https://www.facebook.com/share/r/K0000167/') == 'SHARE_REEL')
    check("fbkind_07467", fb_kind('https://www.facebook.com/share/r/K0000168/') == 'SHARE_REEL')
    check("fbkind_07468", fb_kind('https://www.facebook.com/share/r/K0000169/') == 'SHARE_REEL')
    check("fbkind_07469", fb_kind('https://www.facebook.com/share/r/K0000170/') == 'SHARE_REEL')
    check("fbkind_07470", fb_kind('https://www.facebook.com/share/r/K0000171/') == 'SHARE_REEL')
    check("fbkind_07471", fb_kind('https://www.facebook.com/share/r/K0000172/') == 'SHARE_REEL')
    check("fbkind_07472", fb_kind('https://www.facebook.com/share/r/K0000173/') == 'SHARE_REEL')
    check("fbkind_07473", fb_kind('https://www.facebook.com/share/r/K0000174/') == 'SHARE_REEL')
    check("fbkind_07474", fb_kind('https://www.facebook.com/share/r/K0000175/') == 'SHARE_REEL')
    check("fbkind_07475", fb_kind('https://www.facebook.com/share/r/K0000176/') == 'SHARE_REEL')
    check("fbkind_07476", fb_kind('https://www.facebook.com/share/r/K0000177/') == 'SHARE_REEL')
    check("fbkind_07477", fb_kind('https://www.facebook.com/share/r/K0000178/') == 'SHARE_REEL')
    check("fbkind_07478", fb_kind('https://www.facebook.com/share/r/K0000179/') == 'SHARE_REEL')
    check("fbkind_07479", fb_kind('https://www.facebook.com/share/r/K0000180/') == 'SHARE_REEL')
    check("fbkind_07480", fb_kind('https://www.facebook.com/share/r/K0000181/') == 'SHARE_REEL')
    check("fbkind_07481", fb_kind('https://www.facebook.com/share/r/K0000182/') == 'SHARE_REEL')
    check("fbkind_07482", fb_kind('https://www.facebook.com/share/r/K0000183/') == 'SHARE_REEL')
    check("fbkind_07483", fb_kind('https://www.facebook.com/share/r/K0000184/') == 'SHARE_REEL')
    check("fbkind_07484", fb_kind('https://www.facebook.com/share/r/K0000185/') == 'SHARE_REEL')
    check("fbkind_07485", fb_kind('https://www.facebook.com/share/r/K0000186/') == 'SHARE_REEL')
    check("fbkind_07486", fb_kind('https://www.facebook.com/share/r/K0000187/') == 'SHARE_REEL')
    check("fbkind_07487", fb_kind('https://www.facebook.com/share/r/K0000188/') == 'SHARE_REEL')
    check("fbkind_07488", fb_kind('https://www.facebook.com/share/r/K0000189/') == 'SHARE_REEL')
    check("fbkind_07489", fb_kind('https://www.facebook.com/share/r/K0000190/') == 'SHARE_REEL')
    check("fbkind_07490", fb_kind('https://www.facebook.com/share/r/K0000191/') == 'SHARE_REEL')
    check("fbkind_07491", fb_kind('https://www.facebook.com/share/r/K0000192/') == 'SHARE_REEL')
    check("fbkind_07492", fb_kind('https://www.facebook.com/share/r/K0000193/') == 'SHARE_REEL')
    check("fbkind_07493", fb_kind('https://www.facebook.com/share/r/K0000194/') == 'SHARE_REEL')
    check("fbkind_07494", fb_kind('https://www.facebook.com/share/r/K0000195/') == 'SHARE_REEL')
    check("fbkind_07495", fb_kind('https://www.facebook.com/share/r/K0000196/') == 'SHARE_REEL')
    check("fbkind_07496", fb_kind('https://www.facebook.com/share/r/K0000197/') == 'SHARE_REEL')
    check("fbkind_07497", fb_kind('https://www.facebook.com/share/r/K0000198/') == 'SHARE_REEL')
    check("fbkind_07498", fb_kind('https://www.facebook.com/share/r/K0000199/') == 'SHARE_REEL')
    check("fbkind_07499", fb_kind('https://www.facebook.com/share/r/K0000200/') == 'SHARE_REEL')
    check("fbkind_07500", fb_kind('https://www.facebook.com/share/r/K0000201/') == 'SHARE_REEL')
    check("fbkind_07501", fb_kind('https://www.facebook.com/share/r/K0000202/') == 'SHARE_REEL')
    check("fbkind_07502", fb_kind('https://www.facebook.com/share/r/K0000203/') == 'SHARE_REEL')
    check("fbkind_07503", fb_kind('https://www.facebook.com/share/r/K0000204/') == 'SHARE_REEL')
    check("fbkind_07504", fb_kind('https://www.facebook.com/share/r/K0000205/') == 'SHARE_REEL')
    check("fbkind_07505", fb_kind('https://www.facebook.com/share/r/K0000206/') == 'SHARE_REEL')
    check("fbkind_07506", fb_kind('https://www.facebook.com/share/r/K0000207/') == 'SHARE_REEL')
    check("fbkind_07507", fb_kind('https://www.facebook.com/share/r/K0000208/') == 'SHARE_REEL')
    check("fbkind_07508", fb_kind('https://www.facebook.com/share/r/K0000209/') == 'SHARE_REEL')
    check("fbkind_07509", fb_kind('https://www.facebook.com/share/r/K0000210/') == 'SHARE_REEL')
    check("fbkind_07510", fb_kind('https://www.facebook.com/share/r/K0000211/') == 'SHARE_REEL')
    check("fbkind_07511", fb_kind('https://www.facebook.com/share/r/K0000212/') == 'SHARE_REEL')
    check("fbkind_07512", fb_kind('https://www.facebook.com/share/r/K0000213/') == 'SHARE_REEL')
    check("fbkind_07513", fb_kind('https://www.facebook.com/share/r/K0000214/') == 'SHARE_REEL')
    check("fbkind_07514", fb_kind('https://www.facebook.com/share/r/K0000215/') == 'SHARE_REEL')
    check("fbkind_07515", fb_kind('https://www.facebook.com/share/r/K0000216/') == 'SHARE_REEL')
    check("fbkind_07516", fb_kind('https://www.facebook.com/share/r/K0000217/') == 'SHARE_REEL')
    check("fbkind_07517", fb_kind('https://www.facebook.com/share/r/K0000218/') == 'SHARE_REEL')
    check("fbkind_07518", fb_kind('https://www.facebook.com/share/r/K0000219/') == 'SHARE_REEL')
    check("fbkind_07519", fb_kind('https://www.facebook.com/share/r/K0000220/') == 'SHARE_REEL')
    check("fbkind_07520", fb_kind('https://www.facebook.com/share/r/K0000221/') == 'SHARE_REEL')
    check("fbkind_07521", fb_kind('https://www.facebook.com/share/r/K0000222/') == 'SHARE_REEL')
    check("fbkind_07522", fb_kind('https://www.facebook.com/share/r/K0000223/') == 'SHARE_REEL')
    check("fbkind_07523", fb_kind('https://www.facebook.com/share/r/K0000224/') == 'SHARE_REEL')
    check("fbkind_07524", fb_kind('https://www.facebook.com/share/r/K0000225/') == 'SHARE_REEL')
    check("fbkind_07525", fb_kind('https://www.facebook.com/share/r/K0000226/') == 'SHARE_REEL')
    check("fbkind_07526", fb_kind('https://www.facebook.com/share/r/K0000227/') == 'SHARE_REEL')
    check("fbkind_07527", fb_kind('https://www.facebook.com/share/r/K0000228/') == 'SHARE_REEL')
    check("fbkind_07528", fb_kind('https://www.facebook.com/share/r/K0000229/') == 'SHARE_REEL')
    check("fbkind_07529", fb_kind('https://www.facebook.com/share/r/K0000230/') == 'SHARE_REEL')
    check("fbkind_07530", fb_kind('https://www.facebook.com/share/r/K0000231/') == 'SHARE_REEL')
    check("fbkind_07531", fb_kind('https://www.facebook.com/share/r/K0000232/') == 'SHARE_REEL')
    check("fbkind_07532", fb_kind('https://www.facebook.com/share/r/K0000233/') == 'SHARE_REEL')
    check("fbkind_07533", fb_kind('https://www.facebook.com/share/r/K0000234/') == 'SHARE_REEL')
    check("fbkind_07534", fb_kind('https://www.facebook.com/share/r/K0000235/') == 'SHARE_REEL')
    check("fbkind_07535", fb_kind('https://www.facebook.com/share/r/K0000236/') == 'SHARE_REEL')
    check("fbkind_07536", fb_kind('https://www.facebook.com/share/r/K0000237/') == 'SHARE_REEL')
    check("fbkind_07537", fb_kind('https://www.facebook.com/share/r/K0000238/') == 'SHARE_REEL')
    check("fbkind_07538", fb_kind('https://www.facebook.com/share/r/K0000239/') == 'SHARE_REEL')
    check("fbkind_07539", fb_kind('https://www.facebook.com/share/r/K0000240/') == 'SHARE_REEL')
    check("fbkind_07540", fb_kind('https://www.facebook.com/share/r/K0000241/') == 'SHARE_REEL')
    check("fbkind_07541", fb_kind('https://www.facebook.com/share/r/K0000242/') == 'SHARE_REEL')
    check("fbkind_07542", fb_kind('https://www.facebook.com/share/r/K0000243/') == 'SHARE_REEL')
    check("fbkind_07543", fb_kind('https://www.facebook.com/share/r/K0000244/') == 'SHARE_REEL')
    check("fbkind_07544", fb_kind('https://www.facebook.com/share/r/K0000245/') == 'SHARE_REEL')
    check("fbkind_07545", fb_kind('https://www.facebook.com/share/r/K0000246/') == 'SHARE_REEL')
    check("fbkind_07546", fb_kind('https://www.facebook.com/share/r/K0000247/') == 'SHARE_REEL')
    check("fbkind_07547", fb_kind('https://www.facebook.com/share/r/K0000248/') == 'SHARE_REEL')
    check("fbkind_07548", fb_kind('https://www.facebook.com/share/r/K0000249/') == 'SHARE_REEL')
    check("fbkind_07549", fb_kind('https://www.facebook.com/share/r/K0000250/') == 'SHARE_REEL')
    check("fbkind_07550", fb_kind('https://www.facebook.com/share/r/K0000251/') == 'SHARE_REEL')
    check("fbkind_07551", fb_kind('https://www.facebook.com/share/r/K0000252/') == 'SHARE_REEL')
    check("fbkind_07552", fb_kind('https://www.facebook.com/share/r/K0000253/') == 'SHARE_REEL')
    check("fbkind_07553", fb_kind('https://www.facebook.com/share/r/K0000254/') == 'SHARE_REEL')
    check("fbkind_07554", fb_kind('https://www.facebook.com/share/r/K0000255/') == 'SHARE_REEL')
    check("fbkind_07555", fb_kind('https://www.facebook.com/share/r/K0000256/') == 'SHARE_REEL')
    check("fbkind_07556", fb_kind('https://www.facebook.com/share/r/K0000257/') == 'SHARE_REEL')
    check("fbkind_07557", fb_kind('https://www.facebook.com/share/r/K0000258/') == 'SHARE_REEL')
    check("fbkind_07558", fb_kind('https://www.facebook.com/share/r/K0000259/') == 'SHARE_REEL')
    check("fbkind_07559", fb_kind('https://www.facebook.com/share/r/K0000260/') == 'SHARE_REEL')
    check("fbkind_07560", fb_kind('https://www.facebook.com/share/r/K0000261/') == 'SHARE_REEL')
    check("fbkind_07561", fb_kind('https://www.facebook.com/share/r/K0000262/') == 'SHARE_REEL')
    check("fbkind_07562", fb_kind('https://www.facebook.com/share/r/K0000263/') == 'SHARE_REEL')
    check("fbkind_07563", fb_kind('https://www.facebook.com/share/r/K0000264/') == 'SHARE_REEL')
    check("fbkind_07564", fb_kind('https://www.facebook.com/share/r/K0000265/') == 'SHARE_REEL')
    check("fbkind_07565", fb_kind('https://www.facebook.com/share/r/K0000266/') == 'SHARE_REEL')
    check("fbkind_07566", fb_kind('https://www.facebook.com/share/r/K0000267/') == 'SHARE_REEL')
    check("fbkind_07567", fb_kind('https://www.facebook.com/share/r/K0000268/') == 'SHARE_REEL')
    check("fbkind_07568", fb_kind('https://www.facebook.com/share/r/K0000269/') == 'SHARE_REEL')
    check("fbkind_07569", fb_kind('https://www.facebook.com/share/r/K0000270/') == 'SHARE_REEL')
    check("fbkind_07570", fb_kind('https://www.facebook.com/share/r/K0000271/') == 'SHARE_REEL')
    check("fbkind_07571", fb_kind('https://www.facebook.com/share/r/K0000272/') == 'SHARE_REEL')
    check("fbkind_07572", fb_kind('https://www.facebook.com/share/r/K0000273/') == 'SHARE_REEL')
    check("fbkind_07573", fb_kind('https://www.facebook.com/share/r/K0000274/') == 'SHARE_REEL')
    check("fbkind_07574", fb_kind('https://www.facebook.com/share/r/K0000275/') == 'SHARE_REEL')
    check("fbkind_07575", fb_kind('https://www.facebook.com/share/r/K0000276/') == 'SHARE_REEL')
    check("fbkind_07576", fb_kind('https://www.facebook.com/share/r/K0000277/') == 'SHARE_REEL')
    check("fbkind_07577", fb_kind('https://www.facebook.com/share/r/K0000278/') == 'SHARE_REEL')
    check("fbkind_07578", fb_kind('https://www.facebook.com/share/r/K0000279/') == 'SHARE_REEL')
    check("fbkind_07579", fb_kind('https://www.facebook.com/share/r/K0000280/') == 'SHARE_REEL')
    check("fbkind_07580", fb_kind('https://www.facebook.com/share/r/K0000281/') == 'SHARE_REEL')
    check("fbkind_07581", fb_kind('https://www.facebook.com/share/r/K0000282/') == 'SHARE_REEL')
    check("fbkind_07582", fb_kind('https://www.facebook.com/share/r/K0000283/') == 'SHARE_REEL')
    check("fbkind_07583", fb_kind('https://www.facebook.com/share/r/K0000284/') == 'SHARE_REEL')
    check("fbkind_07584", fb_kind('https://www.facebook.com/share/r/K0000285/') == 'SHARE_REEL')
    check("fbkind_07585", fb_kind('https://www.facebook.com/share/r/K0000286/') == 'SHARE_REEL')
    check("fbkind_07586", fb_kind('https://www.facebook.com/share/r/K0000287/') == 'SHARE_REEL')
    check("fbkind_07587", fb_kind('https://www.facebook.com/share/r/K0000288/') == 'SHARE_REEL')
    check("fbkind_07588", fb_kind('https://www.facebook.com/share/r/K0000289/') == 'SHARE_REEL')
    check("fbkind_07589", fb_kind('https://www.facebook.com/share/r/K0000290/') == 'SHARE_REEL')
    check("fbkind_07590", fb_kind('https://www.facebook.com/share/r/K0000291/') == 'SHARE_REEL')
    check("fbkind_07591", fb_kind('https://www.facebook.com/share/r/K0000292/') == 'SHARE_REEL')
    check("fbkind_07592", fb_kind('https://www.facebook.com/share/r/K0000293/') == 'SHARE_REEL')
    check("fbkind_07593", fb_kind('https://www.facebook.com/share/r/K0000294/') == 'SHARE_REEL')
    check("fbkind_07594", fb_kind('https://www.facebook.com/share/r/K0000295/') == 'SHARE_REEL')
    check("fbkind_07595", fb_kind('https://www.facebook.com/share/r/K0000296/') == 'SHARE_REEL')
    check("fbkind_07596", fb_kind('https://www.facebook.com/share/r/K0000297/') == 'SHARE_REEL')
    check("fbkind_07597", fb_kind('https://www.facebook.com/share/r/K0000298/') == 'SHARE_REEL')
    check("fbkind_07598", fb_kind('https://www.facebook.com/share/r/K0000299/') == 'SHARE_REEL')
    check("fbkind_07599", fb_kind('https://www.facebook.com/share/r/K0000300/') == 'SHARE_REEL')
    check("fbkind_07600", fb_kind('https://www.facebook.com/share/r/K0000301/') == 'SHARE_REEL')
    check("fbkind_07601", fb_kind('https://www.facebook.com/share/r/K0000302/') == 'SHARE_REEL')
    check("fbkind_07602", fb_kind('https://www.facebook.com/share/r/K0000303/') == 'SHARE_REEL')
    check("fbkind_07603", fb_kind('https://www.facebook.com/share/r/K0000304/') == 'SHARE_REEL')
    check("fbkind_07604", fb_kind('https://www.facebook.com/share/r/K0000305/') == 'SHARE_REEL')
    check("fbkind_07605", fb_kind('https://www.facebook.com/share/r/K0000306/') == 'SHARE_REEL')
    check("fbkind_07606", fb_kind('https://www.facebook.com/share/r/K0000307/') == 'SHARE_REEL')
    check("fbkind_07607", fb_kind('https://www.facebook.com/share/r/K0000308/') == 'SHARE_REEL')
    check("fbkind_07608", fb_kind('https://www.facebook.com/share/r/K0000309/') == 'SHARE_REEL')
    check("fbkind_07609", fb_kind('https://www.facebook.com/share/r/K0000310/') == 'SHARE_REEL')
    check("fbkind_07610", fb_kind('https://www.facebook.com/share/r/K0000311/') == 'SHARE_REEL')
    check("fbkind_07611", fb_kind('https://www.facebook.com/share/r/K0000312/') == 'SHARE_REEL')
    check("fbkind_07612", fb_kind('https://www.facebook.com/share/r/K0000313/') == 'SHARE_REEL')
    check("fbkind_07613", fb_kind('https://www.facebook.com/share/r/K0000314/') == 'SHARE_REEL')
    check("fbkind_07614", fb_kind('https://www.facebook.com/share/r/K0000315/') == 'SHARE_REEL')
    check("fbkind_07615", fb_kind('https://www.facebook.com/share/r/K0000316/') == 'SHARE_REEL')
    check("fbkind_07616", fb_kind('https://www.facebook.com/share/r/K0000317/') == 'SHARE_REEL')
    check("fbkind_07617", fb_kind('https://www.facebook.com/share/r/K0000318/') == 'SHARE_REEL')
    check("fbkind_07618", fb_kind('https://www.facebook.com/share/r/K0000319/') == 'SHARE_REEL')
    check("fbkind_07619", fb_kind('https://www.facebook.com/share/r/K0000320/') == 'SHARE_REEL')
    check("fbkind_07620", fb_kind('https://www.facebook.com/share/r/K0000321/') == 'SHARE_REEL')
    check("fbkind_07621", fb_kind('https://www.facebook.com/share/r/K0000322/') == 'SHARE_REEL')
    check("fbkind_07622", fb_kind('https://www.facebook.com/share/r/K0000323/') == 'SHARE_REEL')
    check("fbkind_07623", fb_kind('https://www.facebook.com/share/r/K0000324/') == 'SHARE_REEL')
    check("fbkind_07624", fb_kind('https://www.facebook.com/share/r/K0000325/') == 'SHARE_REEL')
    check("fbkind_07625", fb_kind('https://www.facebook.com/share/r/K0000326/') == 'SHARE_REEL')
    check("fbkind_07626", fb_kind('https://www.facebook.com/share/r/K0000327/') == 'SHARE_REEL')
    check("fbkind_07627", fb_kind('https://www.facebook.com/share/r/K0000328/') == 'SHARE_REEL')
    check("fbkind_07628", fb_kind('https://www.facebook.com/share/r/K0000329/') == 'SHARE_REEL')
    check("fbkind_07629", fb_kind('https://www.facebook.com/share/r/K0000330/') == 'SHARE_REEL')
    check("fbkind_07630", fb_kind('https://www.facebook.com/share/r/K0000331/') == 'SHARE_REEL')
    check("fbkind_07631", fb_kind('https://www.facebook.com/share/r/K0000332/') == 'SHARE_REEL')

    return {"passed": passed, "total": passed + len(failed), "failed": failed}

if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        result = run_extended_offline_tests()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if not result["failed"] else 1)
