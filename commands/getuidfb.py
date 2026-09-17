"""
FACEBOOK FORENSIC RESOLVER V60
Telegram / Telethon module
PUBLIC HTTP ONLY
NO:
- Playwright
- Selenium
- Chromium
- Facebook Login
- Cookies
- Facebook Access Token
- Graph API authentication
DESIGN PRINCIPLE
----------------
CONTENT TYPE != PUBLISHER TYPE
Ví dụ:
/kim.chi.125900/posts/123/
    CONTENT   = POST
    PUBLISHER = USER
/some-page/posts/123/
    CONTENT   = POST
    PUBLISHER = PAGE
/groups/123/posts/456/
    CONTENT   = GROUP_POST
    PUBLISHER = GROUP
/some-page/reel/999/
    CONTENT   = REEL
    PUBLISHER = PAGE
Không bao giờ:
creator_id => USER
page_id    => USER UID
group_id   => USER UID
Không bao giờ đoán UID khi evidence không đủ.
"""
from __future__ import annotations
import asyncio
import base64
import concurrent.futures
import binascii
import html as html_lib
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
)
from urllib.parse import (
    parse_qs,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)
import requests
from telethon import events
LOGGER = logging.getLogger("commands.getuidfb")
if not LOGGER.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | FBRESOLVER | %(levelname)s | %(message)s",
    )
REQUEST_TIMEOUT = (2.2, 5.5)
MAX_HTML_BYTES = 12 * 1024 * 1024
MAX_INPUT_URLS = 8
MAX_DISCOVERED_URLS = 100
MAX_PROFILE_CHECKS = 12
MAX_SHARE_PROBES = 8
MAX_JSON_DEPTH = 12
MAX_STRING_SCAN = 500_000
MAX_EVIDENCE_PER_SOURCE = 80
MAX_SIGNALS = 5
# Performance / reliability tuning
CONCURRENCY = 20
PROFILE_SWEEP_CONCURRENCY = 20
SHARE_SWEEP_CONCURRENCY = 10
CACHE_TTL = 300
RETRY_COUNT = 1
SESSION_TIMEOUT = 720
# Adaptive probe limits: only expensive fallback work runs after primary evidence fails.
FALLBACK_PROBE_CONCURRENCY = 4
FALLBACK_PROBE_TIMEOUT = (2.0, 5.0)


# Public fallback resolver. It is used only when the primary public-HTTP
# evidence cannot establish a USER UID. No cookies or access tokens are sent.
FALLBACK_API_URL = "https://id.traodoisub.com/api.php"
FALLBACK_API_ENABLED = True
FALLBACK_API_TIMEOUT = (1.8, 4.5)
FALLBACK_API_CACHE_TTL = 300
FALLBACK_API_MAX_BYTES = 512 * 1024
FALLBACK_API_MIN_CONFIDENCE = 84.0
FALLBACK_PROBE_ENABLED = True
FALLBACK_PROBE_PATHS = ("/", "/profile.php?id={id}")
FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "m.facebookcorewwwi.onion",
}
TRACKING_PARAMS = {
    "fbclid",
    "rdid",
    "refsrc",
    "ref",
    "refid",
    "mibextid",
    "cft",
    "tn",
    "eep",
    "so",
    "rv",
    "rdr",
    "acontext",
    "paipv",
    "notif_id",
    "notif_t",
    "notif_type",
    "locale",
}
PRESERVE_PARAMS = {
    "id",
    "v",
    "fbid",
    "story_fbid",
    "set",
    "album_id",
    "photo_id",
    "next",
    "share_url",
}
GENERIC_NAMES = {
    "",
    "facebook",
    "log in",
    "login",
    "watch",
    "video",
    "videos",
    "photos",
    "photo",
    "reels",
    "reel",
    "home",
}
GENERIC_TITLE_RE = re.compile(
    r"^(facebook|log\s*in|login|watch|video|videos|photo|photos|reel|reels)$",
    re.I,
)
USER_AGENTS = [
    (
        "Mozilla/5.0 (Linux; Android 15; SM-S938B) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.7339.101 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
]
FACEBOOK_LOGIN_PATHS = {
    "/login.php",
    "/login/",
    "/login",
    "/checkpoint/",
    "/checkpoint",
    "/recover/",
    "/recover",
}
def is_facebook_auth_wall(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower().rstrip("/")

        if host not in {
            "facebook.com",
            "www.facebook.com",
            "m.facebook.com",
            "mbasic.facebook.com",
        }:
            return False

        if path in FACEBOOK_LOGIN_PATHS:
            return True

        if "login" in path and path.endswith(".php"):
            return True

        return False

    except Exception:
        return False
def make_headers(
    *,
    mobile: bool = False,
    referer: Optional[str] = None,
) -> Dict[str, str]:
    ua = (
        USER_AGENTS[0]
        if mobile
        else random.choice(USER_AGENTS[2:])
    )
    headers = {
        "authority": "www.facebook.com",
        "accept": (
            "text/html,"
            "application/xhtml+xml,"
            "application/xml;q=0.9,"
            "image/avif,"
            "image/webp,"
            "image/apng,"
            "*/*;q=0.8"
        ),
        "accept-language": (
            "vi-VN,vi;q=0.9,"
            "en-US;q=0.8,en;q=0.7"
        ),
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "upgrade-insecure-requests": "1",
        "user-agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/139.0.0.0 "
            "Safari/537.36"
        ),
    }
    if referer:
        headers["Referer"] = referer
    return headers
def clean_text(value: Any) -> str:
    if value is None:
        return ""
    value = str(value)
    value = html_lib.unescape(value)
    value = value.replace("\x00", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()
def truncate(
    value: Any,
    limit: int = 500,
) -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
def tg_escape(value: Any) -> str:
    return html_lib.escape(
        clean_text(value),
        quote=True,
    )
def is_numeric_id(value: Any) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    return bool(
        re.fullmatch(
            r"\d{5,30}",
            s,
        )
    )
def is_opaque_content_id(value: Any) -> bool:
    if value is None:
        return False
    s = clean_text(value)
    if not s:
        return False
    if len(s) < 6 or len(s) > 300:
        return False
    if re.fullmatch(
        r"pfbid[A-Za-z0-9_-]+",
        s,
        re.I,
    ):
        return True
    if re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{5,299}",
        s,
    ):
        return not is_numeric_id(s)
    return False
def is_content_id(value: Any) -> bool:
    return (
        is_numeric_id(value)
        or is_opaque_content_id(value)
    )
def unique_keep_order(
    items: Iterable[str],
) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if not item:
            continue
        key = item.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
def normalize_host(host: str) -> str:
    host = (host or "").lower().strip()
    if host.endswith("."):
        host = host[:-1]
    return host
def is_facebook_host(host: str) -> bool:
    host = normalize_host(host)
    return (
        host in FACEBOOK_HOSTS
        or host.endswith(".facebook.com")
    )
def normalize_facebook_url(url: str) -> str:
    url = unquote(
        (url or "").strip()
    )
    if not re.match(
        r"^https?://",
        url,
        re.I,
    ):
        url = "https://" + url
    p = urlparse(url)
    host = normalize_host(p.netloc)
    if host in {
        "m.facebook.com",
        "web.facebook.com",
    }:
        host = "www.facebook.com"
    path = re.sub(
        r"/+",
        "/",
        p.path or "/",
    )
    if path != "/":
        path = path.rstrip("/")
    query = parse_qs(
        p.query,
        keep_blank_values=True,
    )
    kept = []
    for key, values in query.items():
        lower = key.lower()
        if lower in TRACKING_PARAMS:
            continue
        if lower.startswith("utm_"):
            continue
        if lower in PRESERVE_PARAMS:
            for value in values:
                kept.append(
                    (key, value)
                )
    query_string = urlencode(
        kept,
        doseq=True,
    )
    return urlunparse(
        (
            "https",
            host,
            path or "/",
            "",
            query_string,
            "",
        )
    )
def looks_like_url(value: str) -> bool:
    if not value:
        return False
    return bool(
        re.search(
            r"https?://"
            r"(?:www\.|m\.|mbasic\.)?"
            r"(?:facebook\.com|fb\.watch)"
            r"/\S+",
            value,
            re.I,
        )
    )
def extract_urls(text: str) -> List[str]:
    """Extract Facebook URLs, including concatenated URLs without newlines."""
    if not text:
        return []

    pattern = re.compile(
        r"https?://(?:(?!https?://)[^\s<>\[\]{}])+",
        re.I,
    )

    found = []
    for match in pattern.finditer(text):
        url = match.group(0).strip()
        url = url.rstrip(".,!?;:)]}'\"")
        if looks_like_url(url):
            found.append(url)

    return unique_keep_order(
        [
            normalize_facebook_url(x)
            for x in found
            if x
        ]
    )

def extract_numeric_ids(
    text: str,
) -> List[str]:
    if not text:
        return []
    return unique_keep_order(
        re.findall(
            r"(?<!\d)(\d{5,30})(?!\d)",
            text,
        )
    )
def decode_facebook_opaque_token(token: str) -> Dict[str, str]:
    """Decode known public Facebook opaque/base64 Story tokens."""
    token = clean_text(token).strip().strip("/?#&\"'")
    if not token or len(token) < 8 or len(token) > 500:
        return {}
    compact = re.sub(r"\s+", "", token).replace("-", "+").replace("_", "/")
    compact += "=" * ((4 - len(compact) % 4) % 4)
    try:
        decoded = base64.b64decode(compact, validate=False).decode(
            "utf-8", errors="strict"
        ).strip()
    except (ValueError, UnicodeError, binascii.Error):
        return {}
    if not decoded:
        return {}
    info = {"token": token, "decoded": decoded, "type": "", "id": ""}
    m = re.fullmatch(r"S:_ISC:(\d{5,30})", decoded, re.I)
    if m:
        info["type"] = "STORY"
        info["id"] = m.group(1)
        return info
    m = re.search(r"(?<!\d)(\d{5,30})(?!\d)$", decoded)
    if m:
        info["id"] = m.group(1)
    if decoded[:2].upper() == "S:":
        info["type"] = "STORY"
    return info


def extract_story_token_info(url: str) -> Dict[str, str]:
    """Extract/decode Story tokens from direct or nested public FB URLs."""
    if not url:
        return {}
    queue = [str(url)]
    seen: Set[str] = set()
    while queue and len(seen) < 16:
        raw = html_lib.unescape(unquote(queue.pop(0)))
        if raw in seen:
            continue
        seen.add(raw)
        try:
            parsed = urlparse(raw)
        except Exception:
            parsed = None
        if not parsed:
            continue
        # Raw-text route scan handles index.php?next=<URL> where the nested
        # URL itself contains unescaped "&" parameters and parse_qs truncates it.
        raw_story = re.search(
            r'/stories/([^/?&#\\s]+)/([A-Za-z0-9_-]{10,200}={0,2})(?:[/?&#\\s]|$)',
            raw,
            re.I,
        )
        if raw_story:
            owner = unquote(raw_story.group(1))
            token = raw_story.group(2)
            info = decode_facebook_opaque_token(token)
            if info:
                info["owner_id"] = owner if is_numeric_id(owner) else ""
                info["story_route"] = raw_story.group(0).rstrip("/?#&")
                if info.get("type") == "STORY" and info.get("id"):
                    return info

        query = parse_qs(parsed.query, keep_blank_values=True)
        for key in ("next", "share_url", "url", "target", "redirect"):
            for value in query.get(key, []):
                value = html_lib.unescape(unquote(value))
                if value and value not in seen:
                    queue.append(value)
        parts = [unquote(x) for x in (parsed.path or "").split("/") if x]
        lower = [x.lower() for x in parts]
        if "stories" in lower:
            idx = lower.index("stories")
            tail = parts[idx + 1:]
            owner = tail[0] if tail else ""
            token = tail[1] if len(tail) >= 2 else ""
            if token:
                info = decode_facebook_opaque_token(token)
                if info:
                    info["owner_id"] = owner if is_numeric_id(owner) else ""
                    info["story_route"] = raw
                    if info.get("type") == "STORY" and info.get("id"):
                        return info
        for key in ("story_fbid", "fbid", "story_id", "story_token"):
            for value in query.get(key, []):
                info = decode_facebook_opaque_token(value)
                if info:
                    info["owner_id"] = query.get("id", [""])[0]
                    if info.get("type") == "STORY" and info.get("id"):
                        return info
        for token in re.findall(r"\b[A-Za-z0-9_-]{20,120}={0,2}\b", raw):
            info = decode_facebook_opaque_token(token)
            if info.get("type") == "STORY" and info.get("id"):
                return info
    return {}


def generic_name(value: str) -> bool:
    value = clean_text(value)
    if not value:
        return True
    if GENERIC_TITLE_RE.fullmatch(value):
        return True
    return value.lower() in GENERIC_NAMES
def clean_title(value: str) -> str:
    value = clean_text(value)
    if generic_name(value):
        return ""
    value = re.sub(
        r"\s*[|·-]\s*Facebook\s*$",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"\s+on Facebook\s*$",
        "",
        value,
        flags=re.I,
    )
    return value.strip()
def looks_like_video_title(
    value: str,
) -> bool:
    value = clean_text(value)
    if not value:
        return False
    return (
        "lượt xem" in value.lower()
        or "cảm xúc" in value.lower()
        or "views" in value.lower()
        or "reel" in value.lower()
        or "video" in value.lower()
    )
def extract_content_identifiers(text: str) -> List[str]:
    if not text:
        return []
    found = []
    found.extend(
        re.findall(
            r"(?<!\d)(\d{5,30})(?!\d)",
            text,
        )
    )
    found.extend(
        re.findall(
            r"\bpfbid[A-Za-z0-9_-]{6,299}\b",
            text,
            re.I,
        )
    )
    return unique_keep_order(found)
@dataclass
class URLShape:
    original: str = ""
    normalized: str = ""
    host: str = ""
    path: str = ""
    segments: List[str] = field(
        default_factory=list
    )
    query: Dict[str, List[str]] = field(
        default_factory=dict
    )
    kind: str = "UNKNOWN"
    username: str = ""
    numeric_path_id: str = ""
    post_id: str = ""
    video_id: str = ""
    reel_id: str = ""
    photo_id: str = ""
    story_id: str = ""
    story_token: str = ""
    story_token_decoded: str = ""
    story_token_type: str = ""
    group_id: str = ""
    page_id: str = ""
    album_id: str = ""
    opaque_token: str = ""
    wrapper: bool = False
    route_entity: str = ""
    route_confidence: float = 0.0
class URLParser:
    RESERVED = {
        "watch",
        "reel",
        "reels",
        "video",
        "videos",
        "posts",
        "post",
        "photos",
        "photo",
        "photo.php",
        "video.php",
        "story.php",
        "stories",
        "groups",
        "group",
        "events",
        "event",
        "pages",
        "page",
        "profile.php",
        "permalink.php",
        "login.php",
        "login",
        "logout.php",
        "checkpoint",
        "recover",
        "help",
        "settings",
        "privacy",
        "security",
        "home.php",
        "ajax",
        "plugins",
        "dialog",
        "oauth",
        "share",
        "share.php",
        "share/r",
        "share/p",
        "share/v",
        "share/x",
        "share/b",
        # Common Facebook-owned landing/system routes. These should not be
        # mistaken for vanity USER profiles when a wrapper lands on them.
        "about",
        "meta",
        "business",
        "businesses",
        "company",
        "careers",
        "news",
        "community",
        "developers",
        "marketing",
        "facebook",
        "legal",
    }
    @classmethod
    def parse(
        cls,
        url: str,
    ) -> URLShape:
        normalized = normalize_facebook_url(url)
        p = urlparse(normalized)
        shape = URLShape(
            original=url,
            normalized=normalized,
            host=normalize_host(p.netloc),
            path=p.path,
            query=parse_qs(
                p.query,
                keep_blank_values=True,
            ),
        )
        segments = [
            unquote(x)
            for x in p.path.split("/")
            if x
        ]
        shape.segments = segments
        story_info = extract_story_token_info(url)
        if story_info:
            shape.story_token = story_info.get("token", "")
            shape.story_token_decoded = story_info.get("decoded", "")
            shape.story_token_type = story_info.get("type", "")
            if story_info.get("id") and not shape.story_id:
                shape.story_id = story_info["id"]
            if is_numeric_id(story_info.get("owner_id", "")):
                shape.numeric_path_id = story_info["owner_id"]
                shape.route_entity = "USER"
            if shape.story_token_type == "STORY":
                shape.kind = "STORY"
                shape.route_confidence = max(shape.route_confidence, 99)
        lower = [
            x.lower()
            for x in segments
        ]
        # Authentication/system endpoints are not Facebook publishers.
        blocked_paths = {
            "login.php", "logout.php", "checkpoint", "recover",
            "registration", "reg", "privacy", "security",
        }
        if (
            p.path.lower().strip("/") in blocked_paths
            or any(x in blocked_paths for x in lower)
        ):
            shape.kind = "UNKNOWN"
            shape.route_entity = ""
            shape.route_confidence = 100
            return shape
        if (
            p.path.lower().endswith("/profile.php")
            and "id" in shape.query
        ):
            uid = shape.query["id"][0]
            if is_numeric_id(uid):
                shape.numeric_path_id = uid
                shape.kind = "PROFILE"
                shape.route_entity = "USER"
                shape.route_confidence = 100
            return shape
        if (
            len(lower) >= 2
            and lower[0] == "share"
        ):
            shape.wrapper = True
            shape.kind = "SHARE_WRAPPER"
            shape.route_confidence = 100
            if lower[1] in {
                "r",
                "p",
                "v",
                "x",
                "b",
            }:
                if len(segments) >= 3:
                    shape.opaque_token = segments[2]
            else:
                # Newer Facebook share URLs commonly use /share/<token>/.
                shape.opaque_token = segments[1]
            return shape
        if "pages" in lower:
            idx = lower.index("pages")
            if idx + 1 < len(segments):
                shape.username = segments[idx + 1]
            if idx + 2 < len(segments) and is_numeric_id(segments[idx + 2]):
                shape.page_id = segments[idx + 2]
                shape.numeric_path_id = segments[idx + 2]
            shape.kind = "PAGE"
            shape.route_entity = "PAGE"
            shape.route_confidence = 99
            return shape
        if lower and lower[0] == "watch":
            if "v" in shape.query and is_content_id(shape.query["v"][0]):
                shape.video_id = shape.query["v"][0]
            shape.kind = "VIDEO"
            shape.route_confidence = 99
            cls._derive_route_entity(shape)
            return shape
        if "groups" in lower:
            idx = lower.index("groups")
            if idx + 1 < len(segments):
                candidate = segments[idx + 1]
                if is_numeric_id(candidate):
                    shape.group_id = candidate
                else:
                    shape.username = candidate
            shape.route_entity = "GROUP"
            if "posts" in lower:
                idx = lower.index("posts")
                if idx + 1 < len(segments):
                    candidate = segments[idx + 1]
                    if is_content_id(candidate):
                        shape.post_id = candidate
                    if "groups" in lower:
                        shape.kind = "GROUP_POST"
                    else:
                        shape.kind = "POST"
                    shape.route_confidence = 99
            elif (
                "reel" in lower
                or "reels" in lower
            ):
                shape.kind = "REEL"
            elif (
                "videos" in lower
                or "video" in lower
            ):
                shape.kind = "VIDEO"
            elif (
                "photos" in lower
                or "photo" in lower
            ):
                shape.kind = "PHOTO"
            else:
                shape.kind = "GROUP"
            shape.route_confidence = 98
            return shape
        if "events" in lower:
            idx = lower.index("events")
            if idx + 1 < len(segments):
                candidate = segments[idx + 1]
                if is_numeric_id(candidate):
                    shape.numeric_path_id = candidate
                shape.kind = "EVENT"
                shape.route_entity = "EVENT"
                shape.route_confidence = 98
            return shape
        if (
            "reel" in lower
            or "reels" in lower
        ):
            idx = (
                lower.index("reel")
                if "reel" in lower
                else lower.index("reels")
            )
            if idx + 1 < len(segments):
                candidate = segments[idx + 1]
                if is_content_id(candidate):
                    shape.reel_id = candidate
            shape.kind = "REEL"
            shape.route_confidence = 99
        if (
            "posts" in lower
            or "post" in lower
        ):
            idx = (
                lower.index("posts")
                if "posts" in lower
                else lower.index("post")
            )
            if idx + 1 < len(segments):
                candidate = segments[idx + 1]
                if is_content_id(candidate):
                    shape.post_id = candidate
            if shape.kind == "UNKNOWN":
                shape.kind = "POST"
            shape.route_confidence = max(
                shape.route_confidence,
                99,
            )
        if (
            "videos" in lower
            or "video" in lower
            or p.path.lower().endswith(
                "/video.php"
            )
        ):
            if "v" in shape.query:
                candidate = shape.query["v"][0]
                if is_content_id(candidate):
                    shape.video_id = candidate
            if not shape.video_id:
                idx = (
                    lower.index("videos")
                    if "videos" in lower
                    else (
                        lower.index("video")
                        if "video" in lower
                        else -1
                    )
                )
                if idx >= 0:
                    for candidate in segments[idx + 1:]:
                        if is_content_id(candidate):
                            shape.video_id = candidate
                            break
            if shape.kind == "UNKNOWN":
                shape.kind = "VIDEO"
            shape.route_confidence = max(
                shape.route_confidence,
                98,
            )
        if (
            "photos" in lower
            or "photo" in lower
            or p.path.lower().endswith(
                "/photo.php"
            )
        ):
            for key in (
                "fbid",
                "photo_id",
            ):
                if key in shape.query:
                    candidate = shape.query[key][0]
                    if is_content_id(candidate):
                        shape.photo_id = candidate
                        break
            if not shape.photo_id:
                idx = (
                    lower.index("photos")
                    if "photos" in lower
                    else (
                        lower.index("photo")
                        if "photo" in lower
                        else -1
                    )
                )
                if idx >= 0:
                    for candidate in segments[idx + 1:]:
                        if is_content_id(candidate):
                            shape.photo_id = candidate
                            break
            if shape.kind == "UNKNOWN":
                shape.kind = "PHOTO"
            shape.route_confidence = max(
                shape.route_confidence,
                98,
            )
        if (
            "story.php" in p.path.lower()
            or "stories" in lower
        ):
            # Story content ID and publisher/owner ID are separate.
            for key in (
                "story_fbid",
                "fbid",
            ):
                if key in shape.query:
                    candidate = shape.query[key][0]
                    if is_numeric_id(candidate):
                        shape.story_id = candidate
                        break

            # Classic: /story.php?story_fbid=STORY_ID&id=USER_ID
            # `id` is the story publisher/owner, not the story itself.
            if "id" in shape.query:
                owner_id = shape.query["id"][0]
                if is_numeric_id(owner_id):
                    shape.numeric_path_id = owner_id
                    shape.route_entity = "USER"

            # Newer: /stories/<USER_ID>/<STORY_ID>/ or
            #        /stories/<USERNAME>/<STORY_ID>/
            if "stories" in lower:
                idx = lower.index("stories")
                tail = segments[idx + 1:]
                if tail:
                    first = tail[0]
                    if is_numeric_id(first):
                        shape.numeric_path_id = first
                        shape.route_entity = "USER"
                    elif (
                        first.lower() not in cls.RESERVED
                        and not shape.username
                    ):
                        shape.username = first
                        shape.route_entity = "USER"
                    if len(tail) >= 2:
                        second = tail[1]
                        if is_content_id(second) and not shape.story_id:
                            shape.story_id = second

            shape.kind = "STORY"
            shape.route_confidence = max(
                shape.route_confidence,
                98,
            )
        if (
            "album" in lower
            or "albums" in lower
        ):
            if "album_id" in shape.query:
                candidate = shape.query["album_id"][0]
                if is_numeric_id(candidate):
                    shape.album_id = candidate
            if shape.kind == "UNKNOWN":
                shape.kind = "ALBUM"
            shape.route_confidence = max(
                shape.route_confidence,
                95,
            )
        if shape.kind == "UNKNOWN":
            if not segments:
                shape.kind = "HOME"
            elif len(segments) == 1:
                first = segments[0]
                if first.lower() not in cls.RESERVED:
                    if is_numeric_id(first):
                        shape.numeric_path_id = first
                    else:
                        shape.username = first
                    shape.kind = "PROFILE"
                    shape.route_confidence = 92
        if shape.kind in {
            "POST",
            "REEL",
            "VIDEO",
            "PHOTO",
            "STORY",
            "ALBUM",
        }:
            cls._derive_route_entity(shape)
        return shape
    @classmethod
    def _derive_route_entity(
        cls,
        shape: URLShape,
    ):
        segments = shape.segments
        if not segments:
            return
        lower = [
            x.lower()
            for x in segments
        ]
        if "groups" in lower:
            idx = lower.index("groups")
            if idx + 1 < len(segments):
                entity = segments[idx + 1]
                if is_numeric_id(entity):
                    shape.group_id = entity
                else:
                    shape.username = entity
                shape.route_entity = "GROUP"
            return
        if "pages" in lower:
            idx = lower.index("pages")
            if idx + 1 < len(segments):
                entity = segments[idx + 1]
                if is_numeric_id(entity):
                    shape.page_id = entity
                else:
                    shape.username = entity
                shape.route_entity = "PAGE"
            return
        if is_numeric_id(segments[0]):
            shape.numeric_path_id = segments[0]
            return
        reserved = {
            "watch",
            "reel",
            "reels",
            "video",
            "videos",
            "posts",
            "post",
            "photos",
            "photo",
            "story",
            "stories",
            "events",
            "event",
            "groups",
            "group",
            "pages",
            "page",
            "profile.php",
            "permalink.php",
            "login.php",
            "login",
            "logout.php",
            "checkpoint",
            "recover",
            "help",
            "settings",
            "privacy",
            "security",
            "home.php",
            "ajax",
            "plugins",
            "dialog",
            "oauth",
        }
        first = segments[0]
        if first.lower() in reserved:
            return
        shape.username = first
        shape.route_entity = "USER"
class FBHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )
        self.meta: Dict[str, str] = {}
        self.links: List[str] = []
        self.scripts: List[str] = []
        self._script = False
        self._script_buffer: List[str] = []
        self.title_parts: List[str] = []
        self._title = False
        self.text_parts: List[str] = []
        self.images: List[str] = []
    def handle_starttag(
        self,
        tag: str,
        attrs: List[
            Tuple[str, Optional[str]]
        ],
    ):
        data = {
            k.lower(): v or ""
            for k, v in attrs
        }
        tag = tag.lower()
        if tag == "meta":
            key = (
                data.get("property")
                or data.get("name")
                or data.get("itemprop")
            )
            content = data.get(
                "content",
                "",
            )
            if key and content:
                self.meta[
                    key.lower()
                ] = clean_text(content)
        elif tag == "a":
            href = data.get("href")
            if href:
                self.links.append(href)
        elif tag == "link":
            # Keep canonical/app/profile links. Facebook may expose the
            # resolved public object here even when the visible page is
            # generic. This is metadata discovery only; no auth bypass.
            rel = (data.get("rel") or "").lower()
            href = data.get("href") or ""
            if href and (
                "canonical" in rel
                or "alternate" in rel
                or "shortlink" in rel
            ):
                self.links.append(href)
        elif tag == "img":
            src = (
                data.get("src")
                or data.get("data-src")
                or data.get("data-original")
            )
            if src:
                self.images.append(src)
        elif tag == "script":
            self._script = True
            self._script_buffer = []
        elif tag == "title":
            self._title = True
    def handle_endtag(
        self,
        tag: str,
    ):
        tag = tag.lower()
        if tag == "script":
            if self._script_buffer:
                self.scripts.append(
                    "\n".join(
                        self._script_buffer
                    )
                )
            self._script = False
            self._script_buffer = []
        elif tag == "title":
            self._title = False
    def handle_data(
        self,
        data: str,
    ):
        if self._script:
            current_size = len(
                "".join(
                    self._script_buffer
                )
            )
            if current_size < MAX_STRING_SCAN:
                self._script_buffer.append(
                    data
                )
        elif self._title:
            self.title_parts.append(data)
        else:
            value = clean_text(data)
            if value:
                self.text_parts.append(value)
    @property
    def title(self) -> str:
        return clean_title(
            " ".join(
                self.title_parts
            )
        )
    @property
    def text(self) -> str:
        return truncate(
            " ".join(
                self.text_parts
            ),
            MAX_STRING_SCAN,
        )

@dataclass
class FallbackUIDResult:
    """Result from the optional public ID resolver fallback."""
    ok: bool = False
    uid: str = ""
    name: str = ""
    username: str = ""
    canonical_url: str = ""
    status: str = ""
    error: str = ""
    raw_keys: List[str] = field(default_factory=list)
    elapsed: float = 0.0


class FallbackUIDResolver:
    """
    Isolated adapter for id.traodoisub.com.

    It is treated as an external resolver, not as unquestionable proof.
    Its numeric result is cross-checked against the primary evidence before
    being promoted to a USER UID.
    """

    NAME_KEYS = (
        "name", "full_name", "display_name", "username", "profile_name",
    )
    USERNAME_KEYS = (
        "username", "user_name", "profile_username", "screen_name",
    )
    URL_KEYS = (
        "url", "link", "profile_url", "profile_link", "canonical_url",
    )

    def __init__(
        self,
        endpoint: str = FALLBACK_API_URL,
        timeout: Tuple[float, float] = FALLBACK_API_TIMEOUT,
        ttl: int = FALLBACK_API_CACHE_TTL,
    ):
        self.endpoint = endpoint
        self.timeout = timeout
        self.ttl = ttl
        self._local = threading.local()
        self._cache: Dict[str, Tuple[float, FallbackUIDResult]] = {}
        self._cache_lock = threading.Lock()

    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = True
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=16,
                pool_maxsize=16,
                max_retries=0,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session

    @staticmethod
    def _walk(obj: Any, path: str = "$", depth: int = 0):
        if depth > 8:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                child = f"{path}.{key}"
                yield child, str(key), value
                yield from FallbackUIDResolver._walk(value, child, depth + 1)
        elif isinstance(obj, list):
            for index, value in enumerate(obj[:100]):
                child = f"{path}[{index}]"
                yield child, str(index), value
                yield from FallbackUIDResolver._walk(value, child, depth + 1)

    @staticmethod
    def _first_string(payload: Any, keys: Tuple[str, ...]) -> str:
        wanted = {normalize_key(k) for k in keys}
        for path, key, value in FallbackUIDResolver._walk(payload):
            if normalize_key(key) in wanted and isinstance(value, (str, int)):
                value = clean_text(value)
                if value:
                    return value
        return ""

    @staticmethod
    def _first_numeric_id(payload: Any) -> str:
        # Prefer explicit UID/profile fields.
        for path, key, value in FallbackUIDResolver._walk(payload):
            if not isinstance(value, (str, int)):
                continue
            value = clean_text(value)
            if not is_numeric_id(value):
                continue
            if normalize_key(key) in {
                "uid", "user_id", "userid", "profile_id", "profile_uid",
                "facebook_id", "facebook_uid",
            }:
                return value

        # The known provider format commonly returns {"id": "..."}.
        # Only accept top-level/data.id rather than arbitrary nested IDs.
        for path, key, value in FallbackUIDResolver._walk(payload):
            if (
                normalize_key(key) == "id"
                and path in {"$.id", "$.data.id"}
                and isinstance(value, (str, int))
            ):
                value = clean_text(value)
                if is_numeric_id(value):
                    return value
        return ""

    @staticmethod
    def _success(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return True
        if "error" in payload and payload.get("error"):
            return False
        for key in ("success", "status", "ok"):
            value = payload.get(key)
            if isinstance(value, bool) and value:
                return True
            if isinstance(value, (int, float)) and value in (1, 200):
                return True
            if isinstance(value, str) and value.lower() in {
                "1", "200", "ok", "success", "true"
            }:
                return True
        return True

    def _cache_get(self, key: str) -> Optional[FallbackUIDResult]:
        now = time.time()
        with self._cache_lock:
            item = self._cache.get(key)
            if not item:
                return None
            timestamp, result = item
            if now - timestamp > self.ttl:
                self._cache.pop(key, None)
                return None
            return FallbackUIDResult(**vars(result))

    def _cache_set(self, key: str, result: FallbackUIDResult):
        with self._cache_lock:
            self._cache[key] = (
                time.time(),
                FallbackUIDResult(**vars(result)),
            )

    def resolve(self, url: str) -> FallbackUIDResult:
        started = time.perf_counter()
        normalized = normalize_facebook_url(url)
        cached = self._cache_get(normalized)
        if cached:
            cached.elapsed = time.perf_counter() - started
            return cached

        result = FallbackUIDResult()
        try:
            response = self.session().post(
                self.endpoint,
                data={"link": normalized},
                headers={
                    "Accept": "application/json,text/plain,*/*",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://id.traodoisub.com",
                    "Referer": "https://id.traodoisub.com/",
                    "User-Agent": USER_AGENTS[0],
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=self.timeout,
            )
            result.status = str(response.status_code)
            raw = response.content[:FALLBACK_API_MAX_BYTES]
            body = raw.decode(
                response.encoding or "utf-8",
                errors="ignore",
            ).strip()

            payload = None
            try:
                payload = json.loads(body)
            except Exception:
                match = re.search(r"\{.*\}", body, flags=re.S)
                if match:
                    payload = safe_json_loads(match.group(0))

            if payload is None:
                result.error = "Fallback API không trả JSON hợp lệ."
            elif not self._success(payload):
                result.error = self._first_string(
                    payload, ("error", "message", "msg")
                ) or "Fallback API báo lỗi."
            else:
                result.uid = self._first_numeric_id(payload)
                result.name = self._first_string(payload, self.NAME_KEYS)
                result.username = self._first_string(
                    payload, self.USERNAME_KEYS
                )
                result.canonical_url = self._first_string(
                    payload, self.URL_KEYS
                )
                result.raw_keys = unique_keep_order(
                    [
                        normalize_key(key)
                        for _, key, _ in self._walk(payload)
                        if key
                    ]
                )[:40]
                if result.uid:
                    result.ok = True
                else:
                    result.error = (
                        "Fallback API phản hồi nhưng không có UID số rõ ràng."
                    )
        except (requests.RequestException, OSError) as exc:
            result.error = truncate(str(exc), 250)
        except Exception as exc:
            LOGGER.debug("Fallback resolver error", exc_info=True)
            result.error = truncate(str(exc), 250)

        result.elapsed = time.perf_counter() - started
        self._cache_set(normalized, result)
        return result


@dataclass
class PageSnapshot:
    url: str = ""
    final_url: str = ""
    status: int = 0
    ok: bool = False
    title: str = ""
    meta: Dict[str, str] = field(
        default_factory=dict
    )
    links: List[str] = field(
        default_factory=list
    )
    scripts: List[str] = field(
        default_factory=list
    )
    images: List[str] = field(
        default_factory=list
    )
    text: str = ""
    html: str = ""
    jsonld: List[Any] = field(
        default_factory=list
    )
    json_objects: List[Any] = field(
        default_factory=list
    )
    # URLs visited during an ordinary HTTP redirect chain.  This is useful
    # for public share links whose final response may be a generic/auth page.
    redirect_chain: List[str] = field(
        default_factory=list
    )
    error: str = ""
    limited: bool = False
class HTTPFetcher:
    def __init__(self):
        self.local = threading.local()
    def session(self) -> requests.Session:
        session = getattr(
            self.local,
            "session",
            None,
        )
        if session is None:
            session = requests.Session()
            session.trust_env = True
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=32,
                pool_maxsize=32,
                max_retries=0,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self.local.session = session
        return session
    def fetch(
        self,
        url: str,
        *,
        referer: Optional[str] = None,
    ) -> PageSnapshot:
        snapshot = PageSnapshot(
            url=url,
            final_url=url,
        )
        session = self.session()
        last_error = ""
        for attempt in range(
            RETRY_COUNT + 1
        ):
            try:
                headers = make_headers(
                    mobile=False,
                    referer=referer,
                )
                response = session.get(
                    url,
                    headers=headers,
                    
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True,
                    stream=True,
                )
                snapshot.status = (
                    response.status_code
                )
                snapshot.final_url = (
                    normalize_facebook_url(
                        response.url
                    )
                    if is_facebook_host(
                        urlparse(
                            response.url
                        ).netloc
                    )
                    else response.url
                )
                snapshot.redirect_chain = unique_keep_order(
                    [
                        (
                            normalize_facebook_url(h.url)
                            if is_facebook_host(urlparse(h.url).netloc)
                            else h.url
                        )
                        for h in response.history
                    ]
                    + [snapshot.final_url]
                )
                content_type = (
                    response.headers.get(
                        "Content-Type",
                        "application/x-www-form-urlencoded",
                    )
                    .lower()
                )
                if (
                    "text/html"
                    not in content_type
                    and "application/xhtml+xml"
                    not in content_type
                ):
                    data = response.content[
                        :MAX_HTML_BYTES
                    ]
                    snapshot.html = data.decode(
                        "utf-8",
                        errors="ignore",
                    )
                    snapshot.ok = response.ok
                    return snapshot
                data = bytearray()
                for chunk in response.iter_content(
                    chunk_size=64 * 1024
                ):
                    if not chunk:
                        continue
                    data.extend(chunk)
                    if len(data) >= MAX_HTML_BYTES:
                        snapshot.limited = True
                        break
                text = bytes(data).decode(
                    response.encoding or "utf-8",
                    errors="ignore",
                )
                snapshot.html = text
                parser = FBHTMLParser()
                try:
                    parser.feed(text)
                except Exception:
                    LOGGER.debug(
                        "HTML parser partial failure",
                        exc_info=True,
                    )
                snapshot.title = parser.title
                snapshot.meta = parser.meta
                snapshot.links = (
                    parser.links[
                        :MAX_DISCOVERED_URLS
                    ]
                )
                snapshot.scripts = parser.scripts
                snapshot.images = (
                    parser.images[
                        :MAX_DISCOVERED_URLS
                    ]
                )
                snapshot.text = parser.text
                snapshot.jsonld = (
                    extract_jsonld(text)
                )
                snapshot.json_objects = (
                    extract_embedded_json(text)
                )
                snapshot.ok = response.ok
                if (
                    response.status_code
                    in {429, 500, 502, 503, 504}
                    and attempt < RETRY_COUNT
                ):
                    time.sleep(
                        0.20 * (2 ** attempt)
                        + random.uniform(0.0, 0.12)
                    )
                    continue
                return snapshot
            except (
                requests.RequestException,
                OSError,
            ) as exc:
                last_error = clean_text(exc)
                if attempt < RETRY_COUNT:
                    time.sleep(
                        0.6 * (
                            2 ** attempt
                        )
                    )
                    continue
                break
            except Exception as exc:
                last_error = clean_text(exc)
                LOGGER.debug(
                    "fetch error",
                    exc_info=True,
                )
                break
        snapshot.error = (
            last_error
            or "HTTP request failed"
        )
        snapshot.limited = True
        return snapshot
def safe_json_loads(
    value: str,
) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None
def recursive_objects(
    obj: Any,
    *,
    depth: int = 0,
) -> Iterable[Any]:
    if depth > MAX_JSON_DEPTH:
        return
    yield obj
    if isinstance(obj, dict):
        for value in obj.values():
            yield from recursive_objects(
                value,
                depth=depth + 1,
            )
    elif isinstance(obj, list):
        for value in obj:
            yield from recursive_objects(
                value,
                depth=depth + 1,
            )
def extract_jsonld(
    html: str,
) -> List[Any]:
    results = []
    pattern = re.compile(
        r"<script[^>]+type=[\"']"
        r"application/ld\+json"
        r"[\"'][^>]*>"
        r"(.*?)"
        r"</script>",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(html):
        raw = match.group(1).strip()
        raw = html_lib.unescape(raw)
        parsed = safe_json_loads(raw)
        if parsed is not None:
            results.append(parsed)
    return results[:50]
def extract_balanced_json(
    text: str,
    start: int,
) -> Optional[str]:
    if start >= len(text):
        return None
    opening = text[start]
    if opening not in "{[":
        return None
    closing = (
        "}"
        if opening == "{"
        else "]"
    )
    depth = 0
    in_string = False
    escaped = False
    end_limit = min(
        len(text),
        start + 1_000_000,
    )
    for i in range(
        start,
        end_limit,
    ):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return text[
                    start:i + 1
                ]
    return None
def extract_embedded_json(
    html: str,
) -> List[Any]:
    results = []
    pattern = re.compile(
        r"JSON\.parse\(\s*"
        r"([\"'])(.*?)\1"
        r"\s*\)",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(html):
        raw = match.group(2)
        try:
            parsed_string = bytes(
                raw,
                "utf-8",
            ).decode(
                "unicode_escape"
            )
        except Exception:
            parsed_string = raw
        parsed = safe_json_loads(
            parsed_string
        )
        if parsed is not None:
            results.append(parsed)
    interesting_markers = (
        '"user_id"',
        '"profile_id"',
        '"owner_id"',
        '"author_id"',
        '"creator_id"',
        '"page_id"',
        '"group_id"',
        '"post_id"',
        '"video_id"',
        '"reel_id"',
        '"media_fbid"',
        '"story_fbid"',
    )
    for marker in interesting_markers:
        offset = 0
        while True:
            pos = html.find(
                marker,
                offset,
            )
            if pos < 0:
                break
            start = html.rfind(
                "{",
                max(
                    0,
                    pos - 10_000,
                ),
                pos + 1,
            )
            if start >= 0:
                raw = extract_balanced_json(
                    html,
                    start,
                )
                if raw:
                    parsed = safe_json_loads(
                        raw
                    )
                    if parsed is not None:
                        results.append(parsed)
            if len(results) >= 120:
                return results
            offset = (
                pos
                + len(marker)
            )
    return results[:120]
@dataclass
class Evidence:
    value: str
    role: str
    source: str
    path: str = ""
    key: str = ""
    neighbor: str = ""
    url: str = ""
    weight: float = 0.0
    independent: bool = True
    entity_type: str = ""
USER_FIELD_WEIGHTS = {
    "user_id": 110,
    "profile_id": 108,
    "owner_id": 100,
    "publisher_id": 100,
    "author_id": 96,
    "from.id": 94,
    "from_id": 92,
    "creator_id": 92,
    "page_owner_id": 88,
    "profile.uid": 108,
    "profile.id": 105,
    "owner.id": 100,
    "publisher.id": 100,
    "author.id": 96,
    "creator.id": 92,
    "actor_id": 70,
    "actor.id": 68,
    "entity_id": 48,
    "entity.id": 45,
}
OBJECT_FIELD_WEIGHTS = {
    "post_id": 110,
    "story_fbid": 110,
    "video_id": 110,
    "photo_id": 110,
    "media_fbid": 105,
    "reel_id": 110,
    "album_id": 100,
    "group_id": 120,
    "page_id": 120,
    "event_id": 120,
}
IDENTITY_FIELDS = {
    x.lower(): w
    for x, w in USER_FIELD_WEIGHTS.items()
}
OBJECT_FIELDS = {
    x.lower(): w
    for x, w in OBJECT_FIELD_WEIGHTS.items()
}
def normalize_key(
    key: Any,
) -> str:
    key = str(key)
    key = key.replace(
        "-",
        "_",
    )
    return key.lower().strip()
def key_role(
    key: str,
) -> str:
    k = normalize_key(key)
    if k in IDENTITY_FIELDS:
        return "USER_CANDIDATE"
    if k in OBJECT_FIELDS:
        return "OBJECT_ID"
    if (
        "page" in k
        and "id" in k
    ):
        return "PAGE_ID"
    if (
        "group" in k
        and "id" in k
    ):
        return "GROUP_ID"
    if (
        "event" in k
        and "id" in k
    ):
        return "EVENT_ID"
    return ""
PROFILE_KEYS = {
    "name",
    "username",
    "short_name",
    "display_name",
    "full_name",
    "profile_url",
    "profile_uri",
    "uri",
    "url",
    "avatar",
    "avatar_url",
    "profile_picture",
    "profile_pic",
    "image",
    "description",
    "bio",
}
CONTENT_KEYS = {
    "title",
    "post_id",
    "video_id",
    "reel_id",
    "photo_id",
    "story_fbid",
    "media_fbid",
}
class EvidenceCollector:
    def __init__(self):
        self.items: List[Evidence] = []
        self._dedupe: Set[
            Tuple[str, str, str]
        ] = set()
    def add(
        self,
        value: Any,
        *,
        role: str,
        source: str,
        path: str = "",
        key: str = "",
        neighbor: str = "",
        url: str = "",
        weight: float = 0.0,
        independent: bool = True,
        entity_type: str = "",
    ):
        value = clean_text(value)
        if not value:
            return
        if len(value) > 500:
            value = value[:500]
        dedupe_key = (
            value,
            role,
            source,
        )
        if dedupe_key in self._dedupe:
            return
        self._dedupe.add(
            dedupe_key
        )
        self.items.append(
            Evidence(
                value=value,
                role=role,
                source=source,
                path=path,
                key=key,
                neighbor=neighbor,
                url=url,
                weight=weight,
                independent=independent,
                entity_type=entity_type,
            )
        )
    def values_for(
        self,
        role: str,
    ) -> List[str]:
        return unique_keep_order(
            [
                x.value
                for x in self.items
                if x.role == role
            ]
        )
    def sources_for(
        self,
        value: str,
    ) -> Set[str]:
        return {
            x.source
            for x in self.items
            if x.value == value
        }
    def by_role(
        self,
        role: str,
    ) -> List[Evidence]:
        return [
            x
            for x in self.items
            if x.role == role
        ]
class JSONEvidenceScanner:
    def __init__(
        self,
        collector: EvidenceCollector,
    ):
        self.collector = collector
    def scan(
        self,
        obj: Any,
        *,
        source: str,
        path: str = "$",
        neighbor: str = "",
    ):
        self._scan(
            obj,
            source=source,
            path=path,
            neighbor=neighbor,
            depth=0,
        )
    def _scan(
        self,
        obj: Any,
        *,
        source: str,
        path: str,
        neighbor: str,
        depth: int,
    ):
        if depth > MAX_JSON_DEPTH:
            return
        if isinstance(obj, dict):
            local_strings = {}
            for key, value in obj.items():
                k = normalize_key(key)
                if isinstance(
                    value,
                    (
                        str,
                        int,
                        float,
                    ),
                ):
                    local_strings[k] = str(value)
            local_entity = ""
            if any(
                k in local_strings
                for k in (
                    "group_id",
                )
            ):
                local_entity = "GROUP"
            elif any(
                k in local_strings
                for k in (
                    "page_id",
                    "page_owner_id",
                )
            ):
                local_entity = "PAGE"
            elif any(
                k in local_strings
                for k in (
                    "event_id",
                )
            ):
                local_entity = "EVENT"
            # Many Facebook payloads use a generic `id` inside a user
            # object. Accept it only when the same object has a username/profile
            # identity signal, and let page/group/event semantics block it.
            generic_id = local_strings.get("id", "")
            declared_type = clean_text(
                local_strings.get("__typename", "")
                or local_strings.get("entity_type", "")
                or local_strings.get("type", "")
            ).lower()
            if (
                is_numeric_id(generic_id)
                and not local_entity
                and (
                    declared_type in {"user", "person"}
                    or local_strings.get("username")
                    or local_strings.get("profile_url")
                    or local_strings.get("profile_uri")
                )
            ):
                identity_neighbor = clean_text(
                    " ".join(
                        x for x in (
                            local_strings.get("username", ""),
                            local_strings.get("name", ""),
                            local_strings.get("profile_url", ""),
                        ) if x
                    )
                )
                self.collector.add(
                    generic_id,
                    role="USER_CANDIDATE",
                    source=source,
                    path=path,
                    key="id",
                    neighbor=identity_neighbor,
                    weight=106,
                    entity_type="USER_CANDIDATE",
                )
            for key, value in local_strings.items():
                role = key_role(key)
                if role:
                    weight = IDENTITY_FIELDS.get(
                        key,
                        OBJECT_FIELDS.get(
                            key,
                            30,
                        ),
                    )
                    if role == "USER_CANDIDATE":
                        effective_entity = (
                            local_entity
                            or "USER_CANDIDATE"
                        )
                        self.collector.add(
                            value,
                            role=role,
                            source=source,
                            path=path,
                            key=key,
                            neighbor=neighbor,
                            weight=weight,
                            entity_type=effective_entity,
                        )
                    elif role == "OBJECT_ID":
                        self.collector.add(
                            value,
                            role=role,
                            source=source,
                            path=path,
                            key=key,
                            neighbor=neighbor,
                            weight=weight,
                            entity_type=local_entity,
                        )
                if (
                    "page" in key
                    and key.endswith("id")
                    and is_numeric_id(value)
                ):
                    self.collector.add(
                        value,
                        role="PAGE_ID",
                        source=source,
                        path=path,
                        key=key,
                        neighbor=neighbor,
                        weight=120,
                        entity_type="PAGE",
                    )
                if (
                    "group" in key
                    and key.endswith("id")
                    and is_numeric_id(value)
                ):
                    self.collector.add(
                        value,
                        role="GROUP_ID",
                        source=source,
                        path=path,
                        key=key,
                        neighbor=neighbor,
                        weight=120,
                        entity_type="GROUP",
                    )
                if k in {
                    "name",
                    "display_name",
                    "full_name",
                    "short_name",
                }:
                    name = clean_text(value)
                    if (
                        name
                        and not generic_name(name)
                        and not looks_like_video_title(name)
                    ):
                        self.collector.add(
                            name,
                            role="NAME",
                            source=source,
                            path=path,
                            key=key,
                            neighbor=neighbor,
                            weight=45,
                        )
                elif k == "username":
                    username = clean_text(value)
                    if username:
                        self.collector.add(
                            username.lstrip("@"),
                            role="USERNAME",
                            source=source,
                            path=path,
                            key=key,
                            neighbor=neighbor,
                            weight=55,
                        )
                elif k in {
                    "profile_url",
                    "profile_uri",
                }:
                    self.collector.add(
                        value,
                        role="PROFILE_URL",
                        source=source,
                        path=path,
                        key=key,
                        neighbor=neighbor,
                        weight=70,
                    )
                elif k in {
                    "url",
                    "uri",
                }:
                    if "facebook.com" in value.lower():
                        self.collector.add(
                            value,
                            role="URL",
                            source=source,
                            path=path,
                            key=key,
                            neighbor=neighbor,
                            weight=30,
                        )
                elif k in {
                    "description",
                    "bio",
                }:
                    self.collector.add(
                        truncate(
                            value,
                            1000,
                        ),
                        role="BIO",
                        source=source,
                        path=path,
                        key=key,
                        neighbor=neighbor,
                        weight=30,
                    )
            for key, value in obj.items():
                child_path = (
                    f"{path}.{key}"
                )
                child_neighbor = (
                    local_strings.get(
                        "name",
                        local_strings.get(
                            "display_name",
                            neighbor,
                        ),
                    )
                )
                self._scan(
                    value,
                    source=source,
                    path=child_path,
                    neighbor=child_neighbor,
                    depth=depth + 1,
                )
        elif isinstance(obj, list):
            for index, value in enumerate(
                obj[:500]
            ):
                self._scan(
                    value,
                    source=source,
                    path=f"{path}[{index}]",
                    neighbor=neighbor,
                    depth=depth + 1,
                )
def scan_meta(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    meta = snapshot.meta
    for key, value in meta.items():
        k = key.lower()
        if k in {
            "og:title",
            "twitter:title",
        }:
            title = clean_title(value)
            if title:
                collector.add(
                    title,
                    role="TITLE",
                    source="meta",
                    key=k,
                    weight=60,
                )
        elif k in {
            "og:description",
            "twitter:description",
        }:
            collector.add(
                truncate(
                    value,
                    1200,
                ),
                role="DESCRIPTION",
                source="meta",
                key=k,
                weight=45,
            )
        elif k == "og:url":
            collector.add(
                value,
                role="CANONICAL",
                source="meta",
                key=k,
                weight=95,
            )
        elif k in {
            "og:image",
            "twitter:image",
        }:
            collector.add(
                value,
                role="IMAGE",
                source="meta",
                key=k,
                weight=30,
            )
def scan_jsonld(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    scanner = JSONEvidenceScanner(
        collector
    )
    for index, obj in enumerate(
        snapshot.jsonld
    ):
        scanner.scan(
            obj,
            source=f"jsonld:{index}",
        )
def scan_scripts(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    """
    High-recall Facebook identity/object scanner.
    IMPORTANT:
    - Never treat arbitrary "id" as USER UID.
    - Generic id is classified only from semantic context.
    - creator_id / actor_id / owner_id are candidates,
      not automatically verified UIDs.
    """
    for index, script in enumerate(
        snapshot.scripts[:150]
    ):
        if not script:
            continue
        script = _normalize_embedded_text(script)
        source = f"script:{index}"
        identity_patterns = {
            # Strong direct USER identity fields.
            "user_id": 118,
            "userid": 118,
            "userId": 118,
            "userID": 118,
            "uid": 116,
            "user_uid": 118,
            "userUid": 118,
            "profile_id": 120,
            "profileid": 120,
            "profileId": 120,
            "profileID": 120,
            "profile_uid": 120,
            "profileuid": 120,
            "profileUid": 120,
            "profileUID": 120,
            "profile_owner_id": 118,
            "profileOwnerId": 118,
            "profile_owner_uid": 120,
            "profileOwnerUid": 120,
            # Publisher / author identity fields.
            "publisher_id": 112,
            "publisherid": 112,
            "publisherId": 112,
            "publisherID": 112,
            "author_id": 110,
            "authorid": 110,
            "authorId": 110,
            "authorID": 110,
            "owner_id": 108,
            "ownerid": 108,
            "ownerId": 108,
            "ownerID": 108,
            "from_id": 108,
            "fromid": 108,
            "fromId": 108,
            "fromID": 108,
            "creator_id": 94,
            "creatorid": 94,
            "creatorId": 94,
            "creatorID": 94,
            "actor_id": 92,
            "actorid": 92,
            "actorId": 92,
            "actorID": 92,
            # Nested identity aliases seen in serialized state.
            "profile.uid": 120,
            "profile.id": 116,
            "profile.owner_id": 118,
            "profile.ownerId": 118,
            "owner.id": 110,
            "publisher.id": 112,
            "author.id": 110,
            "from.id": 108,
            "creator.id": 94,
            "actor.id": 92,
            "legacy_id": 92,
            "legacyId": 92,
            "legacyID": 92,
        }
        for key, weight in identity_patterns.items():
            if _uid_negative_key(key):
                continue
            escaped = re.escape(key)
            # Accept JSON, JS object literals and GraphQL-like key/value
            # forms.  The key itself is semantic, so this is much safer than
            # globally harvesting every numeric ``id`` in the document.
            pattern = re.compile(
                rf'(?:["\']{escaped}["\']|(?<![A-Za-z0-9_$]){escaped}(?![A-Za-z0-9_$]))'
                rf'\s*[:=]\s*'
                rf'(?:["\']?)(\d{{5,30}})(?:["\']?)',
                re.I,
            )
            for match in pattern.finditer(script):
                value = match.group(1)
                if not is_numeric_id(value):
                    continue
                start = max(
                    0,
                    match.start() - 500,
                )
                end = min(
                    len(script),
                    match.end() + 500,
                )
                context = script[
                    start:end
                ].lower()
                entity_type = ""
                if (
                    "group_id" in context
                    or "groupid" in context
                    or '"group"' in context
                ):
                    entity_type = "GROUP"
                elif (
                    "page_id" in context
                    or "pageid" in context
                    or '"page"' in context
                ):
                    entity_type = "PAGE"
                elif (
                    "event_id" in context
                    or "eventid" in context
                    or '"event"' in context
                ):
                    entity_type = "EVENT"
                collector.add(
                    value,
                    role="USER_CANDIDATE",
                    source=source,
                    key=key,
                    weight=weight,
                    neighbor=clean_text(
                        context[:500]
                    ),
                    entity_type=(
                        entity_type
                        or "UNKNOWN"
                    ),
                )
        explicit_entity_patterns = {
            "page_id": (
                "PAGE_ID",
                125,
            ),
            "pageid": (
                "PAGE_ID",
                125,
            ),
            "pageId": (
                "PAGE_ID",
                125,
            ),
            "group_id": (
                "GROUP_ID",
                125,
            ),
            "groupid": (
                "GROUP_ID",
                125,
            ),
            "groupId": (
                "GROUP_ID",
                125,
            ),
            "event_id": (
                "EVENT_ID",
                125,
            ),
            "eventid": (
                "EVENT_ID",
                125,
            ),
            "eventId": (
                "EVENT_ID",
                125,
            ),
        }
        for key, (
            role,
            weight,
        ) in explicit_entity_patterns.items():
            pattern = re.compile(
                rf'(?:["\']{re.escape(key)}["\']|(?<![A-Za-z0-9_$]){re.escape(key)}(?![A-Za-z0-9_$]))'
                rf'\s*[:=]\s*'
                rf'(?:["\']?)(\d{{5,30}})(?:["\']?)',
                re.I,
            )
            for match in pattern.finditer(script):
                value = match.group(1)
                if not is_numeric_id(value):
                    continue
                entity = (
                    "PAGE"
                    if role == "PAGE_ID"
                    else (
                        "GROUP"
                        if role == "GROUP_ID"
                        else "EVENT"
                    )
                )
                collector.add(
                    value,
                    role=role,
                    source=source,
                    key=key,
                    weight=weight,
                    entity_type=entity,
                )
        object_patterns = {
            "post_id": 110,
            "postid": 110,
            "postId": 110,
            "video_id": 110,
            "videoid": 110,
            "videoId": 110,
            "reel_id": 110,
            "reelid": 110,
            "reelId": 110,
            "photo_id": 110,
            "photoid": 110,
            "photoId": 110,
            "media_fbid": 108,
            "mediaFbid": 108,
            "story_fbid": 110,
            "storyFbid": 110,
            "album_id": 100,
            "albumid": 100,
            "albumId": 100,
        }
        for key, weight in object_patterns.items():
            pattern = re.compile(
                rf'(?:["\']{re.escape(key)}["\']|(?<![A-Za-z0-9_$]){re.escape(key)}(?![A-Za-z0-9_$]))'
                rf'\s*[:=]\s*'
                rf'(?:["\']?)([A-Za-z0-9._:-]{{5,300}})(?:["\']?)',
                re.I,
            )
            for match in pattern.finditer(script):
                value = match.group(1)
                if not is_content_id(value):
                    continue
                collector.add(
                    value,
                    role="OBJECT_ID",
                    source=source,
                    key=key,
                    weight=weight,
                )
        opaque_pattern = re.compile(
            r"\bpfbid[A-Za-z0-9_-]{6,299}\b",
            re.I,
        )
        for match in opaque_pattern.finditer(script):
            value = match.group(0)
            if not is_opaque_content_id(value):
                continue
            start = max(
                0,
                match.start() - 700,
            )
            end = min(
                len(script),
                match.end() + 700,
            )
            context = clean_text(
                script[start:end]
            )
            collector.add(
                value,
                role="OBJECT_ID",
                source=source,
                key="opaque_content_id",
                weight=115,
                neighbor=context,
            )
        # Generic id is accepted only when the surrounding object explicitly
        # declares itself as a USER. This catches __typename/type/entity_type
        # payloads while preventing arbitrary content IDs from becoming UIDs.
        user_typed = re.compile(
            r"\{[^{}]{0,6000}?(?:__typename|entity_type|entityType|type)\s*[:=]\s*[\"\']?(?:User|USER)[\"\']?"
            r"[^{}]{0,6000}?(?:[\"\']id[\"\']|(?<![A-Za-z0-9_$])id(?![A-Za-z0-9_$]))"
            r"\s*[:=]\s*[\"\']?(\d{5,30})[\"\']?[^{}]{0,6000}\}",
            re.I | re.S,
        )
        for match in user_typed.finditer(script):
            value = match.group(1)
            if not is_numeric_id(value):
                continue
            context = clean_text(match.group(0))
            low = context.lower()
            if any(x in low for x in ("post_id", "comment_id", "group_id", "page_id", "event_id")):
                continue
            collector.add(
                value, role="USER_CANDIDATE", source=source, key="id",
                weight=132, neighbor=context[:2200], entity_type="USER",
            )

        semantic_objects = (
            "user",
            "profile",
            "owner",
            "author",
            "publisher",
            "actor",
            "creator",
            "from",
        )
        for semantic in semantic_objects:
            pattern = re.compile(
                rf'(?:["\']{semantic}["\']|(?<![A-Za-z0-9_$]){semantic}(?![A-Za-z0-9_$]))'
                rf'\s*:\s*\{{'
                rf'.{{0,5000}}?'
                rf'(?:["\']id["\']|(?<![A-Za-z0-9_$])id(?![A-Za-z0-9_$]))'
                rf'\s*:\s*(?:["\']?)(\d{{5,30}})(?:["\']?)',
                re.I | re.S,
            )
            for match in pattern.finditer(script):
                value = match.group(1)
                if not is_numeric_id(value):
                    continue
                weight_map = {
                    "user": 112,
                    "profile": 112,
                    "owner": 102,
                    "author": 100,
                    "publisher": 100,
                    "actor": 82,
                    "creator": 88,
                    "from": 96,
                }
                collector.add(
                    value,
                    role="USER_CANDIDATE",
                    source=source,
                    key=f"{semantic}.id",
                    weight=weight_map.get(
                        semantic,
                        70,
                    ),
                    neighbor=semantic,
                    entity_type="UNKNOWN",
                )
        username_pattern = re.compile(
            r'["\']username["\']'
            r'\s*:\s*["\']'
            r'([^"\']+)'
            r'["\']',
            re.I,
        )
        for match in username_pattern.finditer(script):
            username = clean_text(
                match.group(1)
            ).lstrip("@")
            if username:
                collector.add(
                    username,
                    role="USERNAME",
                    source=source,
                    key="username",
                    weight=65,
                )
        profile_url_pattern = re.compile(
            r'["\'](?:profile_url|profile_uri)["\']'
            r'\s*:\s*["\']'
            r'(https?://[^"\']+)'
            r'["\']',
            re.I,
        )
        for match in profile_url_pattern.finditer(script):
            value = clean_text(
                match.group(1)
            )
            if (
                value
                and is_facebook_host(
                    urlparse(value).netloc
                )
            ):
                collector.add(
                    value,
                    role="PROFILE_URL",
                    source=source,
                    key="profile_url",
                    weight=80,
                )

def scan_html_profile_links(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    """Harvest explicit public profile URLs from raw HTML.

    This is intentionally URL-based: a vanity name is only correlated to a
    numeric UID after the profile URL itself is fetched and the profile
    response exposes a matching numeric identity.
    """
    html = _normalize_embedded_text(snapshot.html or "")
    if not html:
        return

    # Facebook profile URLs may appear escaped inside bootstrap JSON/HTML.
    pattern = re.compile(
        r'https?(?:\\u002F|/){2}'
        r'(?:www\\.|m\\.|mbasic\\.)?facebook\\.com'
        r'(?:\\u002F|/)+'
        r'([^"\'<>\s?&\\]+)',
        re.I,
    )
    for match in pattern.finditer(html):
        username = match.group(1)
        username = username.replace("\\u002F", "/").split("/")[0]
        username = html_lib.unescape(username)
        if not username:
            continue
        parsed = URLParser.parse(
            "https://www.facebook.com/" + username
        )
        if parsed.kind == "PROFILE" and parsed.username:
            collector.add(
                "https://www.facebook.com/" + quote(
                    parsed.username,
                    safe="@.*-",
                ),
                role="PROFILE_URL",
                source="html:profile_link",
                key="profile_url",
                weight=78,
            )

UID_NEGATIVE_KEYS = {
    "post_id", "postid", "comment_id", "commentid", "feedback_id", "feedbackid",
    "reaction_id", "reactionid", "story_fbid", "media_fbid", "video_id", "videoid",
    "reel_id", "reelid", "photo_id", "photoid", "album_id", "albumid",
    "group_id", "groupid", "page_id", "pageid", "event_id", "eventid",
    "ad_id", "adid", "tracking_id", "trackingid", "session_id", "sessionid",
    "thread_id", "threadid", "timestamp", "created_time", "updated_time",
}
UID_STRONG_KEYS = {
    "uid", "user_id", "userid", "user_uid", "useruid", "profile_id", "profileid",
    "profile_uid", "profileuid", "profile_owner_id", "profileownerid",
    "profile_owner_uid", "profileowneruid", "profile.uid", "profile.id",
    "profile_owner.id", "profile_owner.uid", "user.id", "person.id",
    "id@username_route", "id@profile_username", "profile_username_explicit_id", "profile.php?id@username", "id@user", "profile.php?id",
    "data-user-id", "data-profile-id", "data-profile-uid",
}
UID_PUBLISHER_KEYS = {
    "publisher_id", "publisherid", "publisher.id", "author_id", "authorid",
    "author.id", "owner_id", "ownerid", "owner.id", "from_id", "fromid", "from.id",
}

def _normalize_embedded_text(text: str) -> str:
    if not text:
        return ""
    out = html_lib.unescape(text)
    out = out.replace('\\"', '"').replace("\\'", "'").replace('\\/', '/')
    return out

def _uid_negative_key(key: str) -> bool:
    return normalize_key(key) in {normalize_key(x) for x in UID_NEGATIVE_KEYS}


def scan_html_identity(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    """High-recall HTML identity pass with strict semantic guards.

    This complements JSON/script parsing because Facebook frequently leaves
    serialized profile state in raw HTML, escaped HTML attributes, meta tags,
    and bootstrap payloads.  Numeric values are only promoted when they occur
    in a USER/profile semantic pattern; arbitrary numeric IDs are ignored.
    """
    html = snapshot.html or ""
    if not html:
        return

    # Direct identity keys that are safe enough to harvest from HTML.
    direct_patterns = {
        "user_id": 122,
        "userid": 122,
        "userId": 122,
        "userID": 122,
        "profile_id": 124,
        "profileid": 124,
        "profileId": 124,
        "profileID": 124,
        "profile_uid": 124,
        "profileUid": 124,
        "profileUID": 124,
        "profile_owner_id": 122,
        "profileOwnerId": 122,
        "owner_id": 106,
        "ownerId": 106,
        "ownerID": 106,
        "author_id": 108,
        "authorId": 108,
        "authorID": 108,
        "publisher_id": 110,
        "publisherId": 110,
        "publisherID": 110,
        "from_id": 106,
        "fromId": 106,
        "fromID": 106,
        "actor_id": 92,
        "actorId": 92,
        "creator_id": 92,
        "creatorId": 92,
    }
    for key, weight in direct_patterns.items():
        pattern = re.compile(
            rf'(?:["\']{re.escape(key)}["\']|(?<![A-Za-z0-9_$]){re.escape(key)}(?![A-Za-z0-9_$]))'
            rf'\s*[:=]\s*(?:["\']?)(\d{{5,30}})(?:["\']?)',
            re.I,
        )
        for m in pattern.finditer(html):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            a = max(0, m.start() - 900)
            b = min(len(html), m.end() + 900)
            context = clean_text(html[a:b])
            low = context.lower()
            entity_type = "UNKNOWN"
            if any(x in low for x in ("group_id", "groupid", '"group"', "group/")):
                entity_type = "GROUP"
            elif any(x in low for x in ("page_id", "pageid", '"page"', "page/")):
                entity_type = "PAGE"
            elif any(x in low for x in ("event_id", "eventid", '"event"', "event/")):
                entity_type = "EVENT"
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:identity",
                key=key,
                weight=weight,
                neighbor=context[:1200],
                entity_type=entity_type,
            )

    # profile.php?id=... is one of the strongest public profile-ID forms.
    profile_route = re.compile(
        r'(?:profile\.php\?(?:[^"\'<>#&]*&)?id=|profile\.php\?id=|fb://profile/)'
        r'(\d{5,30})',
        re.I,
    )
    for m in profile_route.finditer(html):
        value = m.group(1)
        if is_numeric_id(value):
            a = max(0, m.start() - 500)
            b = min(len(html), m.end() + 700)
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:profile_route",
                key="profile.php?id",
                weight=128,
                neighbor=clean_text(html[a:b]),
                entity_type="USER",
            )

    # Nested semantic objects: {"profile_owner":{"id":"..."}},
    # {"author":{"id":...}}, etc.  The bounded window prevents a random
    # ID thousands of characters away from being associated with a user.
    for semantic, weight in {
        "profile_owner": 124,
        "profileOwner": 124,
        "user": 118,
        "profile": 118,
        "author": 108,
        "publisher": 110,
        "owner": 106,
        "from": 104,
        "actor": 92,
        "creator": 92,
    }.items():
        pattern = re.compile(
            rf'(?:["\']{re.escape(semantic)}["\']|(?<![A-Za-z0-9_$]){re.escape(semantic)}(?![A-Za-z0-9_$]))'
            rf'\s*:\s*\{{[^\{{\}}]{{0,5000}}?'
            rf'(?:["\']id["\']|(?<![A-Za-z0-9_$])id(?![A-Za-z0-9_$]))'
            rf'\s*:\s*(?:["\']?)(\d{{5,30}})(?:["\']?)',
            re.I | re.S,
        )
        for m in pattern.finditer(html):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            a = max(0, m.start() - 700)
            b = min(len(html), m.end() + 700)
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:nested_identity",
                key=f"{semantic}.id",
                weight=weight,
                neighbor=clean_text(html[a:b]),
                entity_type="USER" if semantic in {"user", "profile", "profile_owner", "profileOwner"} else "UNKNOWN",
            )

    # A username/profile URL and UID occurring in the same small HTML window
    # is an important correlation signal, but not a standalone proof.
    username = clean_text(
        getattr(snapshot, "meta", {}).get("profile:username", "")
    ).lstrip("@")
    username_candidates = []
    if username:
        username_candidates.append(username)
    for m in re.finditer(
        r'(?:["\']username["\']|username)\s*[:=]\s*["\']([^"\']{1,120})["\']',
        html,
        re.I,
    ):
        u = clean_text(m.group(1)).lstrip("@")
        if u:
            username_candidates.append(u)
    username_candidates = unique_keep_order(username_candidates)

    if username_candidates:
        for u in username_candidates[:20]:
            if not u:
                continue
            for m in re.finditer(re.escape(u), html, re.I):
                a = max(0, m.start() - 1200)
                b = min(len(html), m.end() + 1200)
                window = html[a:b]
                for im in re.finditer(
                    r'(?:profile(?:_owner)?(?:_id|Id|ID)?|user(?:_id|Id|ID)|owner_id|author_id|publisher_id|from_id)'
                    r'\s*["\']?\s*[:=]\s*["\']?(\d{5,30})',
                    window,
                    re.I,
                ):
                    value = im.group(1)
                    if not is_numeric_id(value):
                        continue
                    collector.add(
                        value,
                        role="USER_CANDIDATE",
                        source="html:username_window",
                        key="username_correlated_id",
                        weight=126,
                        neighbor=clean_text(window[:1800]),
                        entity_type="USER",
                    )


def scan_html_forensic_identity(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    """Second-pass forensic UID extraction for raw public Facebook HTML.

    This pass is intentionally independent from the normal JSON/script scanner.
    It looks for identity-bearing HTML attributes, profile routes, link targets,
    bootstrap variables and compact semantic objects.  It never promotes a
    naked numeric ``id`` to a USER UID.
    """
    html = _normalize_embedded_text(snapshot.html or "")
    if not html:
        return

    # 1) HTML data-* identity attributes.
    data_patterns = {
        "data-user-id": 138,
        "data-userid": 138,
        "data-profile-id": 140,
        "data-profileid": 140,
        "data-profile-uid": 140,
        "data-profile-uid": 140,
        "data-profile-owner-id": 136,
        "data-owner-id": 122,
        "data-author-id": 124,
        "data-publisher-id": 126,
        "data-from-id": 120,
        "data-actor-id": 108,
        "data-creator-id": 106,
    }
    for attr, weight in data_patterns.items():
        pat = re.compile(
            rf'\b{re.escape(attr)}\s*=\s*["\']?(\d{{5,30}})["\']?',
            re.I,
        )
        for m in pat.finditer(html):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            a = max(0, m.start() - 500)
            b = min(len(html), m.end() + 700)
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:data_attribute",
                key=attr,
                weight=weight,
                neighbor=clean_text(html[a:b]),
                entity_type="USER" if "user" in attr or "profile" in attr else "UNKNOWN",
            )

    # 1.5) App-link / deep-link metadata. Older and mobile Facebook
    # responses frequently expose the exact profile object through fb://profile,
    # al:ios:url, al:android:url or referrer_profile_id even when normal JSON
    # identity keys are absent.
    app_link_patterns = [
        (r'fb://profile(?:/|\?id=)(\d{5,30})', "fb://profile", 152),
        (r'(?:al:(?:ios|android|web):url|canonical|og:url)[^>]{0,500}?fb://profile(?:/|\?id=)(\d{5,30})', "app-link:fb-profile", 154),
        (r'(?:referrer_profile_id|referrerProfileId|referrer_profile|profile_id|profileId)["\'&=:\s]+(\d{5,30})', "referrer_profile_id", 150),
        (r'(?:profile\.php\?[^"\'<>#]{0,300}?[?&]id=|[?&]profile_id=)(\d{5,30})', "profile-url-id", 150),
    ]
    for pattern, key, weight in app_link_patterns:
        try:
            rx = re.compile(pattern, re.I | re.S)
        except re.error:
            continue
        for m in rx.finditer(html):
            value = next((g for g in m.groups() if g), "")
            if not value or not is_numeric_id(value):
                continue
            a = max(0, m.start() - 900)
            b = min(len(html), m.end() + 1400)
            local = clean_text(html[a:b])
            low = local.lower()
            # Never use content/entity IDs merely because they occur nearby.
            if key not in {"profile-url-id", "fb://profile", "app-link:fb-profile"} and any(x in low for x in (
                "post_id", "story_fbid", "media_fbid", "video_id", "reel_id",
                "photo_id", "comment_id", "feedback_id", "reaction_id",
                "group_id", "page_id", "event_id",
            )):
                continue
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:app_link_identity",
                key=key,
                weight=weight,
                neighbor=local,
                entity_type="USER",
            )

    # 1.6) Generic profile identity in links/images. A common public-page
    # fallback is an image/photo URL carrying referrer_profile_id.
    for m in re.finditer(
        r'(?:href|src|data-[a-z0-9_-]+)\s*=\s*["\'][^"\']{0,500}?'
        r'(?:referrer_profile_id|profile_id|profileId)=(\d{5,30})[^"\']*["\']',
        html,
        re.I | re.S,
    ):
        value = m.group(1)
        if not is_numeric_id(value):
            continue
        local = clean_text(html[max(0, m.start()-500):min(len(html), m.end()+900)])
        collector.add(
            value,
            role="USER_CANDIDATE",
            source="html:profile_link_identity",
            key="referrer_profile_id",
            weight=148,
            neighbor=local,
            entity_type="USER",
        )

    # 2) Profile/person URL forms embedded in href/src/data attributes.
    route_patterns = [
        (r'(?:/|https?://[^\s"\'<>]+/)profile\.php\?[^"\'<>#]*?\bid=(\d{5,30})', "profile.php?id", 142),
        (r'(?:fb://profile/)(\d{5,30})', "fb://profile", 142),
        (r'/people/[^/"\'<>]{1,180}/(\d{5,30})(?:[/?#"\'<>]|$)', "people/<name>/<id>", 134),
    ]
    for pattern, key, weight in route_patterns:
        for m in re.finditer(pattern, html, re.I):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            a = max(0, m.start() - 700)
            b = min(len(html), m.end() + 900)
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:profile_route_forensic",
                key=key,
                weight=weight,
                neighbor=clean_text(html[a:b]),
                entity_type="USER",
            )

    # 3) Explicit identity key/value pairs, including unquoted HTML/JS keys.
    identity_keys = {
        "uid": 132,
        "user_id": 134,
        "userid": 134,
        "userId": 134,
        "userID": 134,
        "user_uid": 134,
        "profile_id": 136,
        "profileId": 136,
        "profileID": 136,
        "profile_uid": 138,
        "profileUid": 138,
        "profile_owner_id": 136,
        "profileOwnerId": 136,
        "profile_owner_uid": 138,
        "profileOwnerUid": 138,
        "publisher_id": 126,
        "publisherId": 126,
        "author_id": 124,
        "authorId": 124,
        "owner_id": 120,
        "ownerId": 120,
        "from_id": 120,
        "fromId": 120,
        "actor_id": 108,
        "actorId": 108,
        "creator_id": 106,
        "creatorId": 106,
        "legacy_id": 112,
        "legacyId": 112,
    }
    for key, weight in identity_keys.items():
        if _uid_negative_key(key):
            continue
        pat = re.compile(
            rf'(?:["\']{re.escape(key)}["\']|(?<![A-Za-z0-9_$]){re.escape(key)}(?![A-Za-z0-9_$]))'
            rf'\s*[:=]\s*["\']?(\d{{5,30}})["\']?',
            re.I,
        )
        for m in pat.finditer(html):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            a = max(0, m.start() - 650)
            b = min(len(html), m.end() + 850)
            context = clean_text(html[a:b])
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:forensic_key",
                key=key,
                weight=weight,
                neighbor=context,
                entity_type="USER_CANDIDATE",
            )

    # 4) Semantic object forms. Keep the object window small enough to avoid
    # attaching an unrelated ID to a nearby user.
    semantic_specs = {
        "profile_owner": (138, "USER"),
        "profileOwner": (138, "USER"),
        "profile": (132, "USER"),
        "user": (132, "USER"),
        "person": (126, "USER"),
        "author": (122, "UNKNOWN"),
        "publisher": (124, "UNKNOWN"),
        "owner": (118, "UNKNOWN"),
        "from": (116, "UNKNOWN"),
        "actor": (108, "UNKNOWN"),
        "creator": (104, "UNKNOWN"),
    }
    for semantic, (weight, entity_type) in semantic_specs.items():
        pat = re.compile(
            rf'(?:["\']{re.escape(semantic)}["\']|(?<![A-Za-z0-9_$]){re.escape(semantic)}(?![A-Za-z0-9_$]))'
            rf'\s*[:=]\s*\{{[^{{}}]{{0,1800}}?'
            rf'(?:["\'](?:id|uid|user_id|profile_id)["\']|(?:id|uid|user_id|profile_id))'
            rf'\s*[:=]\s*["\']?(\d{{5,30}})["\']?',
            re.I | re.S,
        )
        for m in pat.finditer(html):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            a = max(0, m.start() - 500)
            b = min(len(html), m.end() + 900)
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:semantic_object",
                key=f"{semantic}.id",
                weight=weight,
                neighbor=clean_text(html[a:b]),
                entity_type=entity_type,
            )

    # 5) username/profile URL + UID within a tight neighborhood. This is a
    # correlation signal rather than proof by itself.
    usernames = set(
        clean_text(e.value).lstrip("@").strip()
        for e in collector.by_role("USERNAME")
        if e.value
    )
    for m in re.finditer(
        r'(?:["\'](?:username|profile_name|profile_username)["\']|(?:username|profile_name|profile_username))'
        r'\s*[:=]\s*["\']([^"\'<>]{1,120})["\']',
        html,
        re.I,
    ):
        u = clean_text(m.group(1)).lstrip("@").strip()
        if u and not generic_name(u):
            usernames.add(u)
    shape_username = clean_text(getattr(snapshot, "meta", {}).get("profile:username", "")).lstrip("@")
    if shape_username:
        usernames.add(shape_username)

    for username in list(usernames)[:30]:
        for um in re.finditer(re.escape(username), html, re.I):
            a = max(0, um.start() - 1000)
            b = min(len(html), um.end() + 1000)
            window = html[a:b]
            for im in re.finditer(
                r'(?:user(?:_id|Id|ID)|profile(?:_owner)?(?:_id|Id|ID)|profile_uid|uid|author_id|publisher_id|owner_id|from_id)'
                r'\s*[:=]\s*["\']?(\d{5,30})["\']?',
                window,
                re.I,
            ):
                value = im.group(1)
                if is_numeric_id(value):
                    collector.add(
                        value,
                        role="USER_CANDIDATE",
                        source="html:forensic_username_correlation",
                        key="username+identity",
                        weight=132,
                        neighbor=clean_text(window),
                        entity_type="USER",
                    )

    # 6) Route username + nearby generic id. Public profile payloads often
    # serialize the profile identifier as a generic `id` instead of user_id.
    generic_id = re.compile(
        r'(?:["\']id["\']|(?<![A-Za-z0-9_$])id(?![A-Za-z0-9_$]))'
        r'\s*[:=]\s*["\']?(\d{5,30})["\']?',
        re.I,
    )
    for username in list(usernames)[:40]:
        if not username or len(username) < 2:
            continue
        for um in re.finditer(re.escape(username), html, re.I):
            a = max(0, um.start() - 1400)
            b = min(len(html), um.end() + 1400)
            window = html[a:b]
            low = window.lower()
            if not any(marker in low for marker in (
                "profile", "username", "user", "person", "owner",
                "publisher", "author", "actor", "fbid",
            )):
                continue
            for im in generic_id.finditer(window):
                value = im.group(1)
                if not is_numeric_id(value):
                    continue
                local = window[max(0, im.start()-500):min(len(window), im.end()+700)]
                local_low = local.lower()
                if any(x in local_low for x in (
                    "post_id", "story_fbid", "media_fbid", "video_id",
                    "reel_id", "photo_id", "comment_id", "feedback_id",
                    "reaction_id", "page_id", "group_id", "event_id",
                )):
                    continue
                collector.add(
                    value,
                    role="USER_CANDIDATE",
                    source="html:username_route_correlation",
                    key="id@username_route",
                    weight=128,
                    neighbor=clean_text(local),
                    entity_type="USER",
                )


    # 6) DEEP USERNAME-ANCHORED ID FORENSICS.
    # Facebook frequently moves the profile UID between attributes, links,
    # bootstrap blobs and compact HTML.  When the route already identifies a
    # USER username, inspect a larger local neighborhood, but only promote IDs
    # that have a USER/profile semantic marker or a profile.php?id link.
    deep_key_re = re.compile(
        r'(?:["\']?(?:entity_id|entityID|entityId|profile_id|profileID|profileId|'
        r'user_id|userID|userId|actor_id|actorID|actorId|owner_id|ownerID|ownerId|'
        r'author_id|authorID|authorId|publisher_id|publisherID|publisherId|'
        r'uid|user_uid|profile_uid)["\']?)'
        r'\s*[:=]\s*["\']?(\d{5,30})["\']?',
        re.I,
    )
    data_id_re = re.compile(
        r'(?:data-[a-z0-9_-]*(?:user|profile|actor|owner|author|publisher|entity)[a-z0-9_-]*|'
        r'data-(?:id|fbid))\s*=\s*["\'](\d{5,30})["\']',
        re.I,
    )
    profile_href_re = re.compile(
        r'(?:profile\.php\?[^"\'<>]*?\bid=(\d{5,30})|'
        r'/profile\.php\?[^"\'<>]*?\bid=(\d{5,30}))',
        re.I,
    )
    for username in list(usernames)[:60]:
        if not username or len(username) < 2:
            continue
        for um in re.finditer(re.escape(username), html, re.I):
            a = max(0, um.start() - 3000)
            b = min(len(html), um.end() + 3000)
            window = html[a:b]
            low = window.lower()
            # Require profile/person semantics. This prevents random post IDs
            # elsewhere in a huge HTML document from becoming a UID.
            markers = (
                "profile", "user", "person", "entity", "username",
                "actor", "owner", "author", "publisher", "profile.php",
            )
            if not any(x in low for x in markers):
                continue
            hits = []
            for rx, key in (
                (deep_key_re, "deep_identity_key"),
                (data_id_re, "data-profile-user-id"),
                (profile_href_re, "profile.php?id"),
            ):
                for im in rx.finditer(window):
                    value = next((g for g in im.groups() if g), "")
                    if value and is_numeric_id(value):
                        hits.append((value, key, im.start(), im.end()))
            for value, key, start, end in hits:
                local = window[max(0, start-900):min(len(window), end+1200)]
                local_low = local.lower()
                if any(x in local_low for x in (
                    "post_id", "story_fbid", "media_fbid", "video_id",
                    "reel_id", "photo_id", "comment_id", "feedback_id",
                    "reaction_id", "group_id", "page_id", "event_id",
                )) and key != "profile.php?id":
                    continue
                collector.add(
                    value,
                    role="USER_CANDIDATE",
                    source="html:deep_username_forensics",
                    key=key,
                    weight=148 if key in {"profile.php?id", "data-profile-user-id"} else 142,
                    neighbor=clean_text(local),
                    entity_type="USER",
                )

    # 7) HTML data-* / embedded attributes can expose the UID without a JSON
    # object. Scan globally for explicit profile/user/entity attributes.
    for m in re.finditer(
        r'<[^>]{0,300}?((?:data-(?:user|profile|actor|owner|author|publisher|entity)-id)|'
        r'(?:data-(?:user|profile|actor|owner|author|publisher|entity)-uid)|'
        r'(?:data-(?:userid|profileid|entityid|fbid)))\s*=\s*["\'](\d{5,30})["\']',
        html,
        re.I | re.S,
    ):
        key = m.group(1)
        value = m.group(2)
        if not is_numeric_id(value):
            continue
        tag = clean_text(m.group(0))
        if username := next((u for u in usernames if re.search(re.escape(u), tag, re.I)), ""):
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:data_attribute",
                key="data-user-id",
                weight=146,
                neighbor=tag,
                entity_type="USER",
            )

    # 6) Explicit USER typename/entity_type near a generic id. This catches
    # bootstrap payloads where the only numeric field is simply `id`.
    user_object = re.compile(
        r'(?:(?:__typename|entity_type|entityType|type)\s*[:=]\s*["\']?(?:User|Person)["\']?'
        r'[^{}]{0,1800}?\bid\s*[:=]\s*["\']?(\d{5,30})["\']?)'
        r'|(?:\bid\s*[:=\s]+["\']?(\d{5,30})["\']?[^{}]{0,1800}?'
        r'(?:__typename|entity_type|entityType|type)\s*[:=]\s*["\']?(?:User|Person)["\']?)',
        re.I | re.S,
    )
    for m in user_object.finditer(html):
        value = m.group(1) or m.group(2)
        if not is_numeric_id(value):
            continue
        a = max(0, m.start() - 400)
        b = min(len(html), m.end() + 700)
        collector.add(
            value,
            role="USER_CANDIDATE",
            source="html:typed_user_object",
            key="id@User",
            weight=136,
            neighbor=clean_text(html[a:b]),
            entity_type="USER",
        )


def scan_profile_uid_correlation(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
    username: str,
):
    """Correlate a vanity USER route with a nearby numeric profile identity.

    Facebook profile bootstrap payloads sometimes expose only a generic
    ``id`` field.  We accept that generic id only when the SAME small HTML
    window also contains the requested username plus explicit profile/user
    semantics.  This prevents naked post/page/group ids from becoming a UID.
    """
    html = _normalize_embedded_text(snapshot.html or "")
    wanted = clean_text(username).lstrip("@").strip()
    if not html or not wanted:
        return

    source_id = urlparse(snapshot.final_url or snapshot.url).path or "/"
    source = "html:profile_uid_correlation:" + source_id
    user_re = re.compile(re.escape(wanted), re.I)

    # Strong explicit profile identity forms.
    explicit_patterns = [
        r'(?:profile\.php\?[^"\'<>#]{0,500}?\bid=|fb://profile/)(\d{5,30})',
        r'(?:["\'](?:user_id|userid|userId|userID|profile_id|profileid|profileId|profileID|profile_uid|profileUid|profileUID|profile_owner_id|profileOwnerId)["\']?\s*[:=]\s*["\']?)(\d{5,30})',
    ]
    for raw_pattern in explicit_patterns:
        pattern = re.compile(raw_pattern, re.I)
        for m in pattern.finditer(html):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            a = max(0, m.start() - 3500)
            b = min(len(html), m.end() + 3500)
            window = html[a:b]
            low = window.lower()
            if not user_re.search(window):
                continue
            if not any(
                marker in low
                for marker in (
                    "profile",
                    "user_id",
                    "userid",
                    "profile_id",
                    "profile_uid",
                    "profile_owner",
                    "fb://profile/",
                )
            ):
                continue
            collector.add(
                value,
                role="USER_CANDIDATE",
                source=source,
                key="profile_username_explicit_id",
                weight=150,
                neighbor=clean_text(window[:2400]),
                entity_type="USER",
            )

    # Generic `id` is accepted only with username + profile semantics in the
    # same bounded window. This is the missing case in many modern bootstrap
    # payloads where Facebook no longer labels the profile id as user_id.
    positions = [m.start() for m in user_re.finditer(html)][:40]
    for pos in positions:
        a = max(0, pos - 5000)
        b = min(len(html), pos + 5000)
        window = html[a:b]
        low = window.lower()
        if not any(
            marker in low
            for marker in (
                "profile",
                "user",
                "person",
                "fb://profile/",
                "profile.php?id=",
                "profile_id",
                "user_id",
                "profile_uid",
            )
        ):
            continue
        generic = re.compile(
            r'(?:["\']id["\']|(?<![A-Za-z0-9_$])id(?![A-Za-z0-9_$]))'
            r'\s*[:=]\s*["\']?(\d{5,30})["\']?',
            re.I,
        )
        for m in generic.finditer(window):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            # Reject obvious content/entity semantics surrounding this id.
            around = clean_text(window[max(0, m.start()-700):m.end()+700]).lower()
            if any(
                marker in around
                for marker in (
                    "post_id", "comment_id", "media_fbid", "story_fbid",
                    "video_id", "reel_id", "photo_id", "album_id",
                    "group_id", "page_id", "event_id",
                )
            ):
                continue
            collector.add(
                value,
                role="USER_CANDIDATE",
                source=source,
                key="id@profile_username",
                weight=142,
                neighbor=around[:2400],
                entity_type="USER",
            )

    # Public HTML sometimes contains a profile link and a separate numeric id
    # in the same anchor/container rather than in JSON.
    profile_link_id = re.compile(
        r'(?:href|data-href|data-url)\s*=\s*["\'][^"\']*'
        r'(?:profile\.php\?[^"\']*?id=)(\d{5,30})',
        re.I,
    )
    for m in profile_link_id.finditer(html):
        value = m.group(1)
        if not is_numeric_id(value):
            continue
        a = max(0, m.start() - 2500)
        b = min(len(html), m.end() + 2500)
        window = html[a:b]
        if not user_re.search(window):
            continue
        collector.add(
            value,
            role="USER_CANDIDATE",
            source=source,
            key="profile.php?id@username",
            weight=148,
            neighbor=clean_text(window[:2200]),
            entity_type="USER",
        )

    # LAST-RESORT PROFILE DOCUMENT CORRELATION.
    # Some current Facebook public responses expose a profile bootstrap object
    # as simply {"id":"..."} and omit user_id/profile_id entirely.  On a
    # document whose final URL is the requested vanity profile, a generic id
    # can be accepted only when it is repeated or surrounded by explicit
    # profile/user semantics.  This is deliberately restricted to PROFILE
    # documents and never runs for POST/REEL/PAGE/GROUP routes.
    profile_path = urlparse(snapshot.final_url or snapshot.url).path.lower().strip("/")
    profile_doc = profile_path == wanted.lower() or profile_path.startswith(wanted.lower() + "/")
    canonical = clean_text(snapshot.meta.get("og:url", "")).lower()
    canonical_matches = ("/" + wanted.lower()) in canonical
    if profile_doc or canonical_matches:
        generic_id_re = re.compile(
            r'(?:(?:["\']id["\'])|(?<![A-Za-z0-9_$])id(?![A-Za-z0-9_$]))'
            r'\s*[:=]\s*["\']?(\d{5,30})["\']?',
            re.I,
        )
        counts = {}
        windows = {}
        for m in generic_id_re.finditer(html):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            around = clean_text(
                html[max(0, m.start() - 900):min(len(html), m.end() + 900)]
            )
            low = around.lower()
            if any(
                marker in low
                for marker in (
                    "post_id", "comment_id", "media_fbid", "story_fbid",
                    "video_id", "reel_id", "photo_id", "album_id",
                    "group_id", "page_id", "event_id", "feedback_id",
                )
            ):
                continue
            semantic = any(
                marker in low
                for marker in (
                    "profile", "user", "person", "publisher",
                    "author", "owner", "actor", "fb://profile/",
                )
            )
            counts[value] = counts.get(value, 0) + 1
            if semantic:
                windows[value] = around[:2400]
        for value, count in counts.items():
            if count < 2 and value not in windows:
                continue
            collector.add(
                value,
                role="USER_CANDIDATE",
                source=source,
                key="profile_document_id",
                weight=134 if count >= 2 else 126,
                neighbor=windows.get(value, "profile document: " + wanted),
                entity_type="USER",
            )

class IdentityCorrelation:
    IDENTITY_KEYS = {
        "user",
        "profile",
        "owner",
        "author",
        "publisher",
        "actor",
        "creator",
        "from",
    }
    @staticmethod
    def normalize_username(
        value: str,
    ) -> str:
        return (
            clean_text(value)
            .lstrip("@")
            .lower()
        )
    @classmethod
    def score_candidate(
        cls,
        candidate: str,
        username: str,
        snapshots: List[PageSnapshot],
        collector: EvidenceCollector,
        shape: Optional[URLShape] = None,
    ) -> Tuple[
        float,
        List[str],
    ]:
        if not is_numeric_id(candidate):
            return 0.0, []
        score = 0.0
        signals: List[str] = []
        wanted = cls.normalize_username(
            username
        )
        evidences = [
            e
            for e in collector.by_role(
                "USER_CANDIDATE"
            )
            if e.value == candidate
        ]
        if not evidences:
            return 0.0, []
        object_ids = {
            e.value
            for e in collector.by_role(
                "OBJECT_ID"
            )
        }
        page_ids = {
            e.value
            for e in collector.by_role(
                "PAGE_ID"
            )
        }
        group_ids = {
            e.value
            for e in collector.by_role(
                "GROUP_ID"
            )
        }
        event_ids = {
            e.value
            for e in collector.by_role(
                "EVENT_ID"
            )
        }
        if candidate in (
            object_ids
            | page_ids
            | group_ids
            | event_ids
        ):
            return (
                0.0,
                [
                    "candidate bị loại vì là "
                    "content/entity ID"
                ],
            )
        strongest = max(
            e.weight
            for e in evidences
        )
        if strongest >= 108:
            score += 32
            signals.append(
                "strong profile/user identity field"
            )
        elif strongest >= 100:
            score += 27
            signals.append(
                "owner/author identity field"
            )
        elif strongest >= 88:
            score += 18
            signals.append(
                "creator identity candidate"
            )
        elif strongest >= 70:
            score += 10
        source_set = {
            e.source
            for e in evidences
            if e.independent
        }
        if len(source_set) >= 2:
            score += 18
            signals.append(
                "UID xuất hiện từ nhiều nguồn độc lập"
            )
        if len(source_set) >= 3:
            score += 10
        if any(
            e.entity_type
            in {
                "USER",
                "USER_CANDIDATE",
            }
            for e in evidences
        ):
            score += 18
            signals.append(
                "candidate có USER semantics"
            )
        candidate_context = " ".join(
            clean_text(e.neighbor)
            for e in evidences
        ).lower()
        if wanted:
            username_context_match = False
            for evidence in evidences:
                context = clean_text(
                    evidence.neighbor
                ).lower()
                if not context:
                    continue
                if (
                    wanted in context
                    and any(
                        marker in context
                        for marker in (
                            "user",
                            "profile",
                            "owner",
                            "publisher",
                            "author",
                            "from",
                        )
                    )
                ):
                    username_context_match = True
                    break
            if username_context_match:
                score += 25
                signals.append(
                    "publisher UID ↔ username "
                    "semantic correlation"
                )
        for ps in snapshots:
            html = ps.html or ""
            if not html:
                continue
            candidate_pattern = re.compile(
                rf"(?<!\d)"
                rf"{re.escape(candidate)}"
                rf"(?!\d)"
            )
            if not candidate_pattern.search(html):
                continue
            score += 12
            if "profile" in (
                ps.final_url
                or ps.url
                or ""
            ).lower():
                score += 5
                signals.append(
                    "profile HTML chứa candidate UID"
                )
            if wanted:
                username_found = (
                    wanted
                    in html.lower()
                )
                if username_found:
                    score += 10
                    signals.append(
                        "profile HTML chứa username"
                    )
            canonical = clean_text(
                ps.meta.get(
                    "og:url",
                    "",
                )
            )
            if canonical:
                canonical_lower = (
                    canonical.lower()
                )
                if (
                    wanted
                    and wanted
                    in canonical_lower
                ):
                    score += 20
                    signals.append(
                        "canonical → username khớp"
                    )
            title = clean_text(
                ps.meta.get(
                    "og:title",
                    "",
                )
            ).lower()
            if (
                wanted
                and wanted in title
            ):
                score += 5
                signals.append(
                    "profile title → username khớp"
                )
        if shape and shape.kind in {
            "POST",
            "REEL",
            "VIDEO",
            "PHOTO",
            "STORY",
            "ALBUM",
        }:
            for evidence in evidences:
                context = (
                    evidence.neighbor
                    or ""
                ).lower()
                key = normalize_key(
                    evidence.key
                )
                if key in {
                    "author_id",
                    "author.id",
                    "publisher_id",
                    "publisher.id",
                    "owner_id",
                    "owner.id",
                    "from_id",
                    "from.id",
                    "profile_owner_id",
                    "profileownerid",
                    "profile_owner.uid",
                }:
                    score += 20
                    signals.append(
                        "content → publisher "
                        "identity correlation"
                    )
                    break
                if any(
                    marker in context
                    for marker in (
                        '"author"',
                        '"publisher"',
                        '"owner"',
                        '"from"',
                    )
                ):
                    score += 12
                    signals.append(
                        "content → identity context"
                    )
                    break
        return (
            min(score, 100.0),
            unique_keep_order(signals),
        )
def scan_url_evidence(
    shape: URLShape,
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    if shape.post_id:
        collector.add(
            shape.post_id,
            role="OBJECT_ID",
            source="url_route",
            key="post_id",
            weight=110,
        )
    if shape.reel_id:
        collector.add(
            shape.reel_id,
            role="OBJECT_ID",
            source="url_route",
            key="reel_id",
            weight=110,
        )
    if shape.video_id:
        collector.add(
            shape.video_id,
            role="OBJECT_ID",
            source="url_route",
            key="video_id",
            weight=110,
        )
    if shape.photo_id:
        collector.add(
            shape.photo_id,
            role="OBJECT_ID",
            source="url_route",
            key="photo_id",
            weight=110,
        )
    if shape.story_id:
        collector.add(
            shape.story_id,
            role="OBJECT_ID",
            source="url_route",
            key="story_id",
            weight=110,
        )
    if shape.group_id:
        collector.add(
            shape.group_id,
            role="GROUP_ID",
            source="url_route",
            key="group_id",
            weight=125,
            entity_type="GROUP",
        )
    if shape.page_id:
        collector.add(
            shape.page_id,
            role="PAGE_ID",
            source="url_route",
            key="page_id",
            weight=125,
            entity_type="PAGE",
        )
    if shape.numeric_path_id:
        collector.add(
            shape.numeric_path_id,
            role="ROUTE_NUMERIC_ID",
            source="url_route",
            key="numeric_path_id",
            weight=35,
        )
    if shape.username:
        collector.add(
            shape.username,
            role="USERNAME",
            source="url_route",
            key="username",
            weight=65,
        )
    if snapshot.final_url:
        collector.add(
            snapshot.final_url,
            role="FINAL_URL",
            source="redirect",
            weight=85,
        )
@dataclass
class EntityClassification:
    publisher: str = "UNKNOWN"
    confidence: float = 0.0
    user_uid: str = ""
    page_id: str = ""
    group_id: str = ""
    event_id: str = ""
    author_uid: str = ""
    signals: List[str] = field(
        default_factory=list
    )
class EntityClassifier:
    CONTENT_KINDS = {
        "POST",
        "REEL",
        "VIDEO",
        "PHOTO",
        "STORY",
        "ALBUM",
        "GROUP_POST",
    }
    @staticmethod
    def _candidate_has_entity(
        collector: EvidenceCollector,
        value: str,
        roles: Set[str],
    ) -> bool:
        for evidence in collector.items:
            if evidence.value != value:
                continue
            if evidence.role in roles:
                return True
        return False
    @staticmethod
    def _route_publisher_type(
        shape: URLShape,
    ) -> str:
        """
        HARD ROUTE TYPE.
        Route semantics được ưu tiên hơn
        các ID phụ xuất hiện ngẫu nhiên trong HTML.
        """
        if shape.route_entity == "GROUP":
            return "GROUP"
        if shape.route_entity == "PAGE":
            return "PAGE"
        if shape.kind == "PROFILE":
            return "USER"
        if (
            shape.kind
            in EntityClassifier.CONTENT_KINDS
            and shape.username
        ):
            return "USER"
        return "UNKNOWN"
    def classify(
        self,
        shape: URLShape,
        collector: EvidenceCollector,
        snapshots: List[PageSnapshot],
    ) -> EntityClassification:
        result = EntityClassification()
        page_ids = unique_keep_order(
            collector.values_for(
                "PAGE_ID"
            )
        )
        group_ids = unique_keep_order(
            collector.values_for(
                "GROUP_ID"
            )
        )
        event_ids = unique_keep_order(
            collector.values_for(
                "EVENT_ID"
            )
        )
        user_candidates = unique_keep_order(
            collector.values_for(
                "USER_CANDIDATE"
            )
        )
        route_type = (
            self._route_publisher_type(
                shape
            )
        )
        if route_type == "GROUP":
            result.publisher = "GROUP"
            result.confidence = 100
            if shape.group_id:
                result.group_id = shape.group_id
            elif group_ids:
                result.group_id = group_ids[0]
        elif route_type == "PAGE":
            result.publisher = "PAGE"
            result.confidence = 100
            if shape.page_id:
                result.page_id = shape.page_id
            elif page_ids:
                result.page_id = page_ids[0]
        elif route_type == "USER":
            result.publisher = "USER"
            result.confidence = (
                100
                if shape.kind == "PROFILE"
                else 96
            )
            if (
                shape.kind == "PROFILE"
                and shape.numeric_path_id
                and is_numeric_id(
                    shape.numeric_path_id
                )
            ):
                result.user_uid = (
                    shape.numeric_path_id
                )
        elif event_ids:
            result.publisher = "EVENT"
            result.confidence = 95
            result.event_id = event_ids[0]
        if result.publisher == "UNKNOWN":
            if shape.group_id:
                result.publisher = "GROUP"
                result.confidence = 100
                result.group_id = shape.group_id
            elif shape.page_id:
                result.publisher = "PAGE"
                result.confidence = 100
                result.page_id = shape.page_id
            elif group_ids:
                result.publisher = "GROUP"
                result.confidence = 90
                result.group_id = group_ids[0]
            elif page_ids:
                result.publisher = "PAGE"
                result.confidence = 90
                result.page_id = page_ids[0]
            elif event_ids:
                result.publisher = "EVENT"
                result.confidence = 90
                result.event_id = event_ids[0]
        if result.publisher == "USER":
            if (
                shape.kind == "PROFILE"
                and shape.numeric_path_id
            ):
                result.user_uid = (
                    shape.numeric_path_id
                )
            else:
                ranked = rank_user_candidates(
                    collector,
                    username=shape.username,
                    shape=shape,
                )
                if ranked:
                    result.user_uid = ranked[0][0]
        if result.publisher in {
            "PAGE",
            "GROUP",
        }:
            excluded = set(
                page_ids
                + group_ids
                + event_ids
            )
            ranked = rank_author_candidates(
                collector,
                excluded_ids=excluded,
            )
            if ranked:
                result.author_uid = ranked[0][0]
        if shape.username:
            result.signals.append(
                "route → publisher username"
            )
        if shape.kind in self.CONTENT_KINDS:
            result.signals.append(
                f"route → {shape.kind}"
            )
        if result.publisher == "USER":
            result.signals.append(
                "publisher type → USER"
            )
        elif result.publisher == "PAGE":
            result.signals.append(
                "publisher type → PAGE"
            )
        elif result.publisher == "GROUP":
            result.signals.append(
                "publisher type → GROUP"
            )
        if page_ids:
            result.signals.append(
                "embedded page_id detected"
            )
        if group_ids:
            result.signals.append(
                "embedded group_id detected"
            )
        if user_candidates:
            result.signals.append(
                "identity candidate found"
            )
        result.signals = (
            unique_keep_order(
                result.signals
            )[:MAX_SIGNALS]
        )
        return result
def rank_user_candidates(
    collector: EvidenceCollector,
    username: str = "",
    shape: Optional[URLShape] = None,
) -> List[Tuple[str, float]]:
    scores: Dict[
        str,
        float,
    ] = {}
    evidence_map: Dict[
        str,
        List[Evidence],
    ] = {}
    wanted = (
        clean_text(username)
        .lstrip("@")
        .lower()
    )
    object_ids = {
        e.value
        for e in collector.by_role(
            "OBJECT_ID"
        )
        if is_numeric_id(e.value)
    }
    page_ids = {
        e.value
        for e in collector.by_role(
            "PAGE_ID"
        )
        if is_numeric_id(e.value)
    }
    group_ids = {
        e.value
        for e in collector.by_role(
            "GROUP_ID"
        )
        if is_numeric_id(e.value)
    }
    event_ids = {
        e.value
        for e in collector.by_role(
            "EVENT_ID"
        )
        if is_numeric_id(e.value)
    }
    blocked = (
        object_ids
        | page_ids
        | group_ids
        | event_ids
    )
    route_entity = (
        shape.route_entity
        if shape
        else ""
    )
    for evidence in collector.by_role(
        "USER_CANDIDATE"
    ):
        value = evidence.value
        if not is_numeric_id(value):
            continue
        if value in blocked:
            continue
        if route_entity == "USER":
            if evidence.entity_type in {
                "PAGE",
                "GROUP",
                "EVENT",
            }:
                continue
        scores.setdefault(
            value,
            0.0,
        )
        evidence_map.setdefault(
            value,
            [],
        ).append(evidence)
    ranked = []
    for value, evidences in evidence_map.items():
        score = 0.0
        keys = {
            normalize_key(e.key)
            for e in evidences
        }
        sources = {
            e.source
            for e in evidences
            if e.independent
        }
        contexts = " ".join(
            clean_text(e.neighbor)
            for e in evidences
        ).lower()
        if keys & {
            "user_id",
            "userid",
            "user.uid",
            "uid",
            "user_uid",
            "useruid",
            "user.id",
            "person.id",
            "profile_owner_uid",
            "profileowneruid",
        }:
            score += 68
        if keys & {
            "profile_id",
            "profileid",
            "profile.uid",
            "profile.id",
            "profile_uid",
            "profileuid",
            "profile_owner_id",
            "profileownerid",
            "profile_owner.uid",
            "profile_owner.id",
            "deep_identity_key",
            "data-profile-user-id",
            "profile.php?id",
        }:
            score += 68
        if keys & {
            "publisher_id",
            "publisherid",
            "publisher.id",
        }:
            score += 55
        if keys & {
            "author_id",
            "authorid",
            "author.id",
        }:
            score += 48
        if keys & {
            "owner_id",
            "ownerid",
            "owner.id",
        }:
            score += 45
        if keys & {
            "from_id",
            "fromid",
            "from.id",
        }:
            score += 42
        if keys & {
            "creator_id",
            "creatorid",
            "creator.id",
        }:
            score += 5
        if keys & {
            "actor_id",
            "actorid",
            "actor.id",
        }:
            score += 3
        if len(sources) >= 2:
            score += 18
        if len(sources) >= 3:
            score += 12
        if len(sources) >= 4:
            score += 8
        if any(
            e.entity_type
            in {
                "USER",
                "USER_CANDIDATE",
            }
            for e in evidences
        ):
            score += 15
        username_match = False
        if wanted:
            for evidence in evidences:
                context = clean_text(
                    evidence.neighbor
                ).lower()
                if wanted in context:
                    username_match = True
                    break
        if username_match:
            score += 15
        publisher_key_match = bool(
            keys & {
                "publisher_id",
                "publisherid",
                "publisher.id",
                "author_id",
                "authorid",
                "author.id",
                "owner_id",
                "ownerid",
                "owner.id",
                "from_id",
                "fromid",
                "from.id",
            }
        )
        if (
            shape
            and shape.kind in {
                "POST",
                "REEL",
                "VIDEO",
                "PHOTO",
                "STORY",
            }
            and publisher_key_match
        ):
            score += 30
        if any(
            e.entity_type
            in {
                "PAGE",
                "GROUP",
                "EVENT",
            }
            for e in evidences
        ):
            score -= 100
        if (
            shape
            and shape.username
            and shape.route_entity == "USER"
        ):
            score += 25
        only_weak_identity = (
            bool(keys)
            and keys <= {
                "creator_id",
                "creatorid",
                "creator.id",
                "actor_id",
                "actorid",
                "actor.id",
            }
        )
        if only_weak_identity:
            score -= 30
        # A USER route already identifies the publisher namespace. If the
        # candidate is backed by a strong profile field, prefer it over weak
        # actor/creator candidates from the same page.
        if route_entity == "USER":
            if keys & UID_STRONG_KEYS:
                score += 22
            if keys & UID_PUBLISHER_KEYS:
                score += 14
            if wanted and any(wanted in clean_text(e.neighbor).lower() for e in evidences):
                score += 12
        if score > 0:
            ranked.append(
                (
                    value,
                    score,
                )
            )
    ranked.sort(
        key=lambda x: x[1],
        reverse=True,
    )
    return ranked
def rank_author_candidates(
    collector: EvidenceCollector,
    excluded_ids: Optional[Set[str]] = None,
) -> List[Tuple[str, float]]:
    excluded_ids = (
        excluded_ids or set()
    )
    scores: Dict[
        str,
        float,
    ] = {}
    sources: Dict[
        str,
        Set[str],
    ] = {}
    for evidence in collector.by_role(
        "USER_CANDIDATE"
    ):
        value = evidence.value
        if not is_numeric_id(value):
            continue
        if value in excluded_ids:
            continue
        object_values = {
            x.value
            for x in collector.by_role(
                "OBJECT_ID"
            )
        }
        if value in object_values:
            continue
        score = evidence.weight
        key = normalize_key(
            evidence.key
        )
        if key in {
            "author_id",
            "author.id",
            "publisher_id",
            "publisher.id",
            "owner_id",
            "owner.id",
            "from_id",
            "from.id",
        }:
            score += 30
        elif key in {
            "creator_id",
            "creator.id",
        }:
            score += 10
        elif key in {
            "actor_id",
            "actor.id",
        }:
            score += 5
        scores[value] = (
            scores.get(value, 0)
            + score
        )
        sources.setdefault(
            value,
            set(),
        ).add(
            evidence.source
        )
    ranked = []
    for value, score in scores.items():
        source_count = len(
            sources.get(
                value,
                set(),
            )
        )
        if source_count >= 2:
            score += 20
        if source_count >= 3:
            score += 10
        ranked.append(
            (
                value,
                score,
            )
        )
    ranked.sort(
        key=lambda x: x[1],
        reverse=True,
    )
    return ranked
@dataclass
class ContentClassification:
    content_type: str = "UNKNOWN"
    object_id: str = ""
    post_id: str = ""
    reel_id: str = ""
    video_id: str = ""
    photo_id: str = ""
    story_id: str = ""
    album_id: str = ""
    confidence: float = 0.0
    signals: List[str] = field(
        default_factory=list
    )
class ContentClassifier:
    def classify(
        self,
        shape: URLShape,
        collector: EvidenceCollector,
        snapshot: PageSnapshot,
    ) -> ContentClassification:
        result = ContentClassification()
        if shape.kind == "GROUP_POST":
            result.content_type = "GROUP_POST"
            result.post_id = (
                shape.post_id
                or self._find_object(
                    collector,
                    "post_id",
                )
            )
            result.object_id = (
                result.post_id
            )
            result.confidence = 99
            result.signals.append(
                "route → GROUP_POST"
            )
            return result
        if shape.kind == "POST":
            result.content_type = "POST"
            result.post_id = (
                shape.post_id
                or self._find_object(
                    collector,
                    "post_id",
                )
            )
            result.object_id = (
                result.post_id
            )
            result.confidence = 99
            result.signals.append(
                "route → POST"
            )
            return result
        if shape.kind == "REEL":
            result.content_type = "REEL"
            result.reel_id = (
                shape.reel_id
                or self._find_object(
                    collector,
                    "reel_id",
                )
            )
            result.post_id = (
                self._find_object(
                    collector,
                    "post_id",
                )
            )
            result.object_id = (
                result.reel_id
                or result.post_id
            )
            result.confidence = 99
            result.signals.append(
                "route → REEL"
            )
            return result
        if shape.kind == "VIDEO":
            result.content_type = "VIDEO"
            result.video_id = (
                shape.video_id
                or self._find_object(
                    collector,
                    "video_id",
                )
            )
            result.post_id = (
                self._find_object(
                    collector,
                    "post_id",
                )
            )
            result.object_id = (
                result.video_id
                or result.post_id
            )
            result.confidence = 98
            result.signals.append(
                "route → VIDEO"
            )
            return result
        if shape.kind == "PHOTO":
            result.content_type = "PHOTO"
            result.photo_id = (
                shape.photo_id
                or self._find_object(
                    collector,
                    "photo_id",
                )
            )
            result.post_id = (
                self._find_object(
                    collector,
                    "post_id",
                )
            )
            result.object_id = (
                result.photo_id
                or result.post_id
            )
            result.confidence = 98
            result.signals.append(
                "route → PHOTO"
            )
            return result
        if shape.kind == "STORY":
            result.content_type = "STORY"
            result.story_id = (
                shape.story_id
                or self._find_object(
                    collector,
                    "story_fbid",
                )
            )
            result.object_id = (
                result.story_id
            )
            result.confidence = 98
            result.signals.append(
                "route → STORY"
            )
            return result
        if shape.kind == "ALBUM":
            result.content_type = "ALBUM"
            result.album_id = (
                shape.album_id
                or self._find_object(
                    collector,
                    "album_id",
                )
            )
            result.object_id = (
                result.album_id
            )
            result.confidence = 97
            result.signals.append(
                "route → ALBUM"
            )
            return result
        if shape.kind == "PROFILE":
            result.content_type = "PROFILE"
            result.confidence = 99
            result.signals.append(
                "route → PROFILE"
            )
            return result
        if collector.by_role(
            "OBJECT_ID"
        ):
            evidence = collector.by_role(
                "OBJECT_ID"
            )
            keys = {
                normalize_key(x.key)
                for x in evidence
            }
            if "reel_id" in keys:
                result.content_type = "REEL"
            elif "video_id" in keys:
                result.content_type = "VIDEO"
            elif "photo_id" in keys:
                result.content_type = "PHOTO"
            elif "story_fbid" in keys:
                result.content_type = "STORY"
            elif "post_id" in keys:
                result.content_type = "POST"
            else:
                result.content_type = "CONTENT"
            result.confidence = 70
            result.signals.append(
                "embedded object evidence"
            )
        return result
    @staticmethod
    def _find_object(
        collector: EvidenceCollector,
        key: str,
    ) -> str:
        values = [
            x.value
            for x in collector.by_role(
                "OBJECT_ID"
            )
            if normalize_key(
                x.key
            ) == key
        ]
        return (
            values[0]
            if values
            else ""
        )
@dataclass
class ProfileInfo:
    name: str = ""
    username: str = ""
    profile_url: str = ""
    avatar_url: str = ""
    bio: str = ""
    entity_type: str = "UNKNOWN"
    page_name: str = ""
    group_name: str = ""
    cover_url: str = ""
def extract_profile_info(
    shape: URLShape,
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
    classification: EntityClassification,
) -> ProfileInfo:
    info = ProfileInfo()
    info.entity_type = (
        classification.publisher
    )
    usernames = collector.values_for(
        "USERNAME"
    )
    if shape.username:
        info.username = shape.username
    elif usernames:
        info.username = usernames[0]
    profile_urls = collector.values_for(
        "PROFILE_URL"
    )
    if profile_urls:
        for value in profile_urls:
            if is_facebook_host(
                urlparse(value).netloc
            ):
                info.profile_url = (
                    normalize_facebook_url(
                        value
                    )
                )
                break
    if (
        not info.profile_url
        and info.username
        and classification.publisher
        in {
            "USER",
            "PAGE",
        }
    ):
        info.profile_url = (
            "https://www.facebook.com/"
            + quote(
                info.username,
                safe="@.*-",
            )
        )
    names = collector.values_for(
        "NAME"
    )
    if names:
        for name in names:
            if (
                not generic_name(name)
                and not looks_like_video_title(
                    name
                )
            ):
                info.name = truncate(
                    name,
                    250,
                )
                break
    if not info.name:
        og_title = snapshot.meta.get(
            "og:title",
            "",
        )
        og_title = clean_title(
            og_title
        )
        if (
            og_title
            and not generic_name(
                og_title
            )
            and not looks_like_video_title(
                og_title
            )
        ):
            info.name = truncate(
                og_title,
                250,
            )
    bios = collector.values_for(
        "BIO"
    )
    if bios:
        info.bio = truncate(
            bios[0],
            800,
        )
    else:
        descriptions = collector.values_for(
            "DESCRIPTION"
        )
        if descriptions:
            info.bio = truncate(
                descriptions[0],
                800,
            )
    images = collector.values_for(
        "IMAGE"
    )
    if images:
        info.avatar_url = images[0]
    return info
@dataclass
class VerificationResult:
    verified: bool = False
    uid: str = ""
    confidence: float = 0.0
    sources: int = 0
    signals: List[str] = field(
        default_factory=list
    )
    reason: str = ""
class IdentityVerifier:
    def verify(
        self,
        *,
        shape: URLShape,
        snapshot: PageSnapshot,
        profile_snapshots: List[PageSnapshot],
        collector: EvidenceCollector,
        classification: EntityClassification,
        profile: ProfileInfo,
    ) -> VerificationResult:
        result = VerificationResult()
        if classification.publisher != "USER":
            result.reason = (
                "Publisher không phải USER. "
                "Không trả UID cá nhân."
            )
            return result
        # STORY route can directly identify the publisher even when the
        # story response itself redirects to login.
        if (
            shape.kind == "STORY"
            and shape.route_entity == "USER"
            and shape.numeric_path_id
            and is_numeric_id(shape.numeric_path_id)
        ):
            result.uid = shape.numeric_path_id
            result.verified = True
            result.confidence = 99.0
            result.sources = 1
            result.signals.append(
                "story route → explicit USER publisher UID"
            )
            if shape.story_id:
                result.signals.append(
                    "story_fbid/fbid → separate STORY content ID"
                )
            return result

        if (
            shape.kind == "PROFILE"
            and shape.numeric_path_id
            and is_numeric_id(
                shape.numeric_path_id
            )
        ):
            result.uid = (
                shape.numeric_path_id
            )
            result.verified = True
            result.confidence = 99.5
            result.sources = 1
            result.signals.append(
                "profile.php?id → "
                "explicit USER UID"
            )
            return result
        if (
            shape.kind in {
                "POST",
                "REEL",
                "VIDEO",
                "PHOTO",
                "STORY",
                "ALBUM",
            }
            and not shape.username
        ):
            result.reason = (
                "Content URL không có "
                "publisher username "
                "để correlation."
            )
            return result
        ranked = rank_user_candidates(
            collector,
            username=(
                profile.username
                or shape.username
            ),
            shape=shape,
        )
        if not ranked:
            result.reason = (
                "Không có USER UID candidate "
                "đủ điều kiện."
            )
            return result

        # PROFILE/USER special path:
        # A public profile document can expose the exact object through
        # fb://profile/<ID> (or equivalent app-link metadata) without placing
        # the username next to the ID.  The previous verifier required a
        # username-neighbour correlation and could therefore throw away a
        # perfectly good profile identity.  For a resolved PROFILE route, the
        # fetched document itself is already the correlation anchor.
        if shape.kind == "PROFILE" and shape.route_entity == "USER":
            direct_profile_keys = {
                "fb://profile",
                "app-link:fb-profile",
                "profile.php?id",
                "profile-url-id",
                "referrer_profile_id",
                "profile_username_explicit_id",
                "profile.php?id@username",
                "profile_document_id",
                "data-user-id",
                "data-profile-id",
                "data-profile-uid",
                "user_id",
                "userid",
                "profile_id",
                "profileid",
                "profile_uid",
                "profileuid",
                "uid",
            }
            blocked_entity_ids = {
                e.value
                for role in ("OBJECT_ID", "PAGE_ID", "GROUP_ID", "EVENT_ID")
                for e in collector.by_role(role)
            }
            direct = []
            for candidate, base_score in ranked:
                if candidate in blocked_entity_ids:
                    continue
                evs = [
                    e for e in collector.by_role("USER_CANDIDATE")
                    if e.value == candidate
                ]
                keys = {normalize_key(e.key) for e in evs}
                if not (keys & {normalize_key(k) for k in direct_profile_keys}):
                    continue
                if any(e.entity_type in {"PAGE", "GROUP", "EVENT"} for e in evs):
                    continue

                # Require that the candidate actually occurred in one of the
                # fetched profile documents, not only in the share wrapper.
                occurred_in_profile = False
                username_in_profile = False
                for ps in profile_snapshots or [snapshot]:
                    body = _normalize_embedded_text(ps.html or "")
                    if not body:
                        continue
                    if re.search(rf"(?<!\d){re.escape(candidate)}(?!\d)", body):
                        occurred_in_profile = True
                        if username and username.lower() in body.lower():
                            username_in_profile = True
                        break
                if not occurred_in_profile:
                    continue

                direct_score = max(96.0, min(99.5, float(base_score) + 25.0))
                direct.append((candidate, direct_score, keys, username_in_profile))

            if direct:
                direct.sort(key=lambda x: x[1], reverse=True)
                best = direct[0]
                # If two independent direct profile IDs disagree, do not guess.
                if len(direct) > 1 and direct[1][0] != best[0] and direct[1][1] >= best[1] - 3:
                    result.reason = (
                        "Profile public HTML chứa nhiều identity ID khác nhau; "
                        "không đủ bằng chứng để chọn UID."
                    )
                    return result
                candidate, direct_score, direct_keys, username_match = best
                result.uid = candidate
                result.verified = True
                result.confidence = direct_score
                result.sources = len(collector.sources_for(candidate))
                if "fb://profile" in direct_keys or "app-link:fb-profile" in direct_keys:
                    result.signals.append("public app-link → exact profile object UID")
                elif "profile.php?id" in direct_keys or "profile-url-id" in direct_keys:
                    result.signals.append("public profile URL → explicit USER UID")
                else:
                    result.signals.append("public profile identity field → USER UID")
                if username_match:
                    result.signals.append("profile HTML → username/UID correlation")
                result.signals.append("profile route → USER publisher")
                result.signals = unique_keep_order(result.signals)[:MAX_SIGNALS]
                return result

        correlator = IdentityCorrelation()
        correlated = []
        username = (
            profile.username
            or shape.username
        )
        for candidate, base_score in ranked[:20]:
            correlation_score, signals = (
                correlator.score_candidate(
                    candidate=candidate,
                    username=username,
                    snapshots=profile_snapshots,
                    collector=collector,
                    shape=shape,
                )
            )
            if correlation_score <= 0:
                continue
            final_score = (
                base_score * 0.40
                + correlation_score * 0.60
            )
            correlated.append(
                (
                    candidate,
                    final_score,
                    signals,
                )
            )
        if not correlated:
            result.reason = (
                "Có USER candidate nhưng "
                "không chứng minh được candidate "
                "thuộc publisher."
            )
            return result
        correlated.sort(
            key=lambda x: x[1],
            reverse=True,
        )
        candidate, score, signals = (
            correlated[0]
        )
        if len(correlated) >= 2:
            second_candidate = (
                correlated[1]
            )
            if (
                second_candidate[1]
                >= score * 0.92
            ):
                result.reason = (
                    "Có nhiều USER UID cạnh tranh "
                    "và chưa đủ bằng chứng để "
                    "chọn publisher UID."
                )
                return result
        if shape.kind in {
            "POST",
            "REEL",
            "VIDEO",
            "PHOTO",
            "STORY",
            "ALBUM",
        }:
            evidence = [
                e
                for e in collector.by_role(
                    "USER_CANDIDATE"
                )
                if e.value == candidate
            ]
            keys = {
                normalize_key(e.key)
                for e in evidence
            }
            has_strong_user_field = bool(
                keys
                & {
                    "user_id",
                    "userid",
                    "profile_id",
                    "profileid",
                    "profile.uid",
                    "profile.id",
                    "user.id",
                    "person.id",
                    "profile_uid",
                    "profileuid",
                    "profile_owner_id",
                    "profileownerid",
                    "uid",
                    "id@username_route",
                    "id@profile_username",
                    "profile_username_explicit_id",
                    "profile.php?id@username",
                    "profile_document_id",
                    "id@user",
                    "profile.php?id",
                    "data-user-id",
                    "data-profile-id",
                    "data-profile-uid",
                }
            )
            has_publisher_field = bool(
                keys
                & {
                    "publisher_id",
                    "publisherid",
                    "publisher.id",
                    "author_id",
                    "authorid",
                    "author.id",
                    "owner_id",
                    "ownerid",
                    "owner.id",
                    "from_id",
                    "fromid",
                    "from.id",
                }
            )
            has_username_signal = any(
                (
                    "username"
                    in clean_text(
                        e.neighbor
                    ).lower()
                    or (
                        username
                        and username.lower()
                        in clean_text(
                            e.neighbor
                        ).lower()
                    )
                )
                for e in evidence
                if username
            )
            if any(
                normalize_key(e.key) == "id@username_route"
                for e in evidence
            ):
                has_username_signal = True
            if not (
                has_strong_user_field
                or has_publisher_field
                or has_username_signal
            ):
                result.reason = (
                    "Candidate có UID nhưng "
                    "không có publisher "
                    "identity evidence."
                )
                return result
        if shape.route_entity == "USER":
            evidence = [
                e
                for e in collector.by_role(
                    "USER_CANDIDATE"
                )
                if e.value == candidate
            ]
            keys = {
                normalize_key(e.key)
                for e in evidence
            }
            strong_identity = bool(
                keys
                & {
                    "user_id",
                    "userid",
                    "profile_id",
                    "profileid",
                    "profile.uid",
                    "profile.id",
                    "user.id",
                    "person.id",
                    "publisher_id",
                    "publisherid",
                    "publisher.id",
                    "author_id",
                    "authorid",
                    "author.id",
                    "owner_id",
                    "ownerid",
                    "owner.id",
                    "from_id",
                    "fromid",
                    "from.id",
                    "id",
                    "id@username_route",
                    "id@profile_username",
                    "profile_username_explicit_id",
                    "profile.php?id@username",
                    "profile_document_id",
                    "id@user",
                    "profile.php?id",
                    "data-user-id",
                    "data-profile-id",
                    "data-profile-uid",
                }
            )
            if not strong_identity:
                result.reason = (
                    "Candidate không có USER identity "
                    "field đủ mạnh cho USER route."
                )
                return result
        if score < 78:
            result.reason = (
                "UID candidate chưa đạt "
                "ngưỡng publisher correlation."
            )
            return result
        result.uid = candidate
        result.verified = True
        result.confidence = min(
            99.5,
            score,
        )
        result.sources = len(
            collector.sources_for(
                candidate
            )
        )
        result.signals.extend(
            signals
        )
        result.signals = (
            unique_keep_order(
                result.signals
            )[:MAX_SIGNALS]
        )
        return result
@dataclass
class ResolveResult:
    input_url: str = ""
    canonical_url: str = ""
    content_url: str = ""
    profile_url: str = ""
    entity_type: str = "UNKNOWN"
    publisher_type: str = "UNKNOWN"
    name: str = ""
    username: str = ""
    avatar_url: str = ""
    bio: str = ""
    page_name: str = ""
    group_name: str = ""
    content_type: str = "UNKNOWN"
    title: str = ""
    post_id: str = ""
    video_id: str = ""
    reel_id: str = ""
    photo_id: str = ""
    story_id: str = ""
    story_token: str = ""
    story_token_decoded: str = ""
    story_token_type: str = ""
    album_id: str = ""
    publisher_id: str = ""
    uid: str = ""
    author_uid: str = ""
    verified: bool = False
    confidence: float = 0.0
    evidence_sources: int = 0
    signals: List[str] = field(
        default_factory=list
    )
    notes: List[str] = field(
        default_factory=list
    )
    status: str = "NOT_VERIFIED"
    elapsed: float = 0.0
    http_status: int = 0
    fallback_used: bool = False
    fallback_uid: str = ""
    fallback_confidence: float = 0.0
    fallback_elapsed: float = 0.0
    fallback_stage: str = ""
    fallback_provider: str = ""
    fallback_object_id: str = ""
    fallback_probes: int = 0
class ResultCache:
    def __init__(
        self,
        ttl: int = CACHE_TTL,
    ):
        self.ttl = ttl
        self._data: Dict[
            str,
            Tuple[
                float,
                ResolveResult,
            ],
        ] = {}
        self._lock = threading.Lock()
    def get(
        self,
        key: str,
    ) -> Optional[ResolveResult]:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            timestamp, result = item
            if (
                now - timestamp
                > self.ttl
            ):
                self._data.pop(
                    key,
                    None,
                )
                return None
            return result
    def set(
        self,
        key: str,
        result: ResolveResult,
    ):
        with self._lock:
            self._data[key] = (
                time.time(),
                result,
            )
class FacebookResolver:
    def __init__(self):
        self.fetcher = HTTPFetcher()
        self.cache = ResultCache()
        self.profile_sem = (
            threading.Semaphore(
                MAX_PROFILE_CHECKS
            )
        )
        self.fallback = FallbackUIDResolver()
        self.fetch_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=PROFILE_SWEEP_CONCURRENCY,
            thread_name_prefix="fb-fetch",
        )
    def _fetch_parallel(
        self,
        urls: List[str],
        *,
        referer: Optional[str] = None,
        max_workers: int = PROFILE_SWEEP_CONCURRENCY,
    ) -> List[Tuple[str, PageSnapshot]]:
        urls = unique_keep_order(urls)
        if not urls:
            return []

        workers = max(1, min(max_workers, len(urls)))

        def worker(target: str):
            try:
                return target, self.fetcher.fetch(
                    target,
                    referer=referer,
                )
            except Exception as exc:
                LOGGER.debug(
                    "parallel fetch failed: %s: %r",
                    target,
                    exc,
                    exc_info=True,
                )
                return target, PageSnapshot(
                    url=target,
                    final_url=target,
                    error=truncate(str(exc), 250),
                    limited=True,
                )

        futures = [
            self.fetch_executor.submit(worker, target)
            for target in urls
        ]
        return [future.result() for future in futures]

    def _apply_fallback(
        self,
        *,
        url: str,
        shape: URLShape,
        snapshot: PageSnapshot,
        profile: ProfileInfo,
        entity: EntityClassification,
        verification: VerificationResult,
        collector: EvidenceCollector,
        result: ResolveResult,
    ) -> VerificationResult:
        """Failure-only fallback chain.

        Stage A: ask the public TDS resolver for the numeric target ID.
        Stage B: if that ID is not itself a publisher UID, probe the public
        Facebook representations of that ID and let the normal evidence
        engine identify the publisher.  This avoids the old bug where a post
        ID from a share URL was incorrectly promoted to a USER UID.
        """
        if (
            not FALLBACK_API_ENABLED
            or verification.verified
            or entity.publisher in {"PAGE", "GROUP", "EVENT"}
        ):
            return verification

        try:
            fallback = self.fallback.resolve(url)
        except Exception:
            LOGGER.debug("fallback resolver failed", exc_info=True)
            return verification

        result.fallback_used = bool(fallback.ok or fallback.uid or fallback.error)
        result.fallback_uid = fallback.uid
        result.fallback_elapsed = fallback.elapsed
        result.fallback_provider = "secondary"
        result.fallback_stage = "API"

        if not fallback.ok or not is_numeric_id(fallback.uid):
            if fallback.error:
                result.notes.append(
                    "Secondary resolver error: " + truncate(fallback.error, 180)
                )
            return verification

        candidate = fallback.uid
        result.fallback_object_id = candidate
        blocked = {
            e.value
            for role in ("OBJECT_ID", "PAGE_ID", "GROUP_ID", "EVENT_ID")
            for e in collector.by_role(role)
        }

        # If primary evidence already contains the same ID, the API is an
        # independent confirmation and is the safest possible promotion.
        primary_sources = collector.sources_for(candidate)
        if primary_sources and candidate not in blocked:
            result.fallback_confidence = 97.0
            verification.uid = candidate
            verification.verified = True
            verification.confidence = min(99.5, max(verification.confidence, 97.0))
            verification.sources = len(primary_sources) + 1
            verification.signals.extend([
                "primary public evidence + fallback API agree on UID",
                "fallback API used as independent cross-check",
            ])
            verification.signals = unique_keep_order(verification.signals)[:MAX_SIGNALS]
            return verification

        # Direct profile routes can safely use an explicit fallback ID after
        # the primary verifier has failed, provided it is not an object ID.
        if (shape.route_entity == "USER" or shape.kind == "PROFILE") and candidate not in blocked:
            result.fallback_confidence = FALLBACK_API_MIN_CONFIDENCE
            verification.uid = candidate
            verification.verified = True
            verification.confidence = FALLBACK_API_MIN_CONFIDENCE
            verification.sources = max(1, len(primary_sources)) + 1
            verification.signals.extend([
                "fallback API → explicit numeric profile UID",
                "fallback invoked only after primary public evidence failed",
            ])
            verification.signals = unique_keep_order(verification.signals)[:MAX_SIGNALS]
            return verification

        # Share/content routes are different: the fallback numeric ID may be
        # the post/video/photo itself.  Probe it instead of guessing that it
        # is a person.  This is the important second-stage recovery path.
        if not FALLBACK_PROBE_ENABLED or candidate in blocked:
            result.notes.append(
                "Fallback trả numeric ID nhưng ID đã bị phân loại là object/page/group/event."
            )
            return verification

        probe_urls = [
            f"https://www.facebook.com/{candidate}",
            f"https://www.facebook.com/profile.php?id={candidate}",
            f"https://m.facebook.com/{candidate}",
            f"https://mbasic.facebook.com/{candidate}",
        ]
        probe_urls = unique_keep_order(probe_urls)
        try:
            probe_snapshots = self._fetch_parallel(
                probe_urls,
                referer=url,
                max_workers=FALLBACK_PROBE_CONCURRENCY,
            )
        except Exception:
            LOGGER.debug("fallback identity probes failed", exc_info=True)
            return verification

        result.fallback_probes = len(probe_snapshots)
        result.fallback_stage = "API→PROBE"
        local_collector = collector
        local_snapshots = [snapshot]
        for _, ps in probe_snapshots:
            local_snapshots.append(ps)
            scan_meta(ps, local_collector)
            scan_jsonld(ps, local_collector)
            scan_scripts(ps, local_collector)
            scan_html_identity(ps, local_collector)
            scan_html_forensic_identity(ps, local_collector)
            scan_html_profile_links(ps, local_collector)

        # Re-run the normal verifier with the newly collected evidence.  This
        # keeps fallback behavior aligned with the main scoring rules.
        refreshed_profile = extract_profile_info(
            shape, probe_snapshots[0][1] if probe_snapshots else snapshot,
            local_collector, entity,
        )
        refreshed_entity = EntityClassifier().classify(
            shape,
            local_collector,
            local_snapshots,
        )
        refreshed = IdentityVerifier().verify(
            shape=shape,
            snapshot=snapshot,
            profile_snapshots=local_snapshots,
            collector=local_collector,
            classification=refreshed_entity,
            profile=refreshed_profile,
        )
        if refreshed.verified and refreshed.uid:
            entity.publisher = refreshed_entity.publisher
            entity.confidence = refreshed_entity.confidence
            entity.user_uid = refreshed_entity.user_uid
            entity.page_id = refreshed_entity.page_id
            entity.group_id = refreshed_entity.group_id
            entity.event_id = refreshed_entity.event_id
            entity.author_uid = refreshed_entity.author_uid
            entity.signals = unique_keep_order(
                entity.signals + refreshed_entity.signals
            )[:MAX_SIGNALS]
            result.fallback_confidence = max(86.0, refreshed.confidence)
            refreshed.confidence = min(99.0, max(refreshed.confidence, 86.0))
            refreshed.sources = max(refreshed.sources, 2)
            refreshed.signals.extend([
                "fallback API returned target ID",
                "fallback target re-probed through public Facebook routes",
                "publisher UID re-verified by the normal evidence engine",
            ])
            refreshed.signals = unique_keep_order(refreshed.signals)[:MAX_SIGNALS]
            return refreshed

        result.notes.append(
            "Fallback API lấy được numeric ID nhưng chưa đủ evidence để kết luận đó là UID người đăng."
        )
        return verification

    def resolve(
        self,
        url: str,
    ) -> ResolveResult:
        started = time.perf_counter()
        normalized = normalize_facebook_url(
            url
        )
        cached = self.cache.get(
            normalized
        )
        if cached:
            cached.elapsed = (
                time.perf_counter()
                - started
            )
            return cached
        try:
            result = self._resolve(
                normalized
            )
        except Exception:
            LOGGER.exception(
                "Resolver failure for %s",
                normalized,
            )
            result = ResolveResult(
                input_url=normalized,
                canonical_url=normalized,
                content_url=normalized,
                status="FETCH_LIMITED",
                notes=[
                    "Resolver internal error."
                ],
            )
        result.elapsed = (
            time.perf_counter()
            - started
        )
        self.cache.set(
            normalized,
            result,
        )
        return result
    def _resolve(
        self,
        url: str,
    ) -> ResolveResult:
        shape = URLParser.parse(
            url
        )
        result = ResolveResult(
            input_url=url,
            canonical_url=url,
            content_url=url,
        )
        # ZERO-HTTP fast path: profile.php?id=<numeric UID> is explicit.
        # Skip Facebook fetches entirely for this unambiguous route.
        if (
            shape.kind == "PROFILE"
            and shape.route_entity == "USER"
            and is_numeric_id(shape.numeric_path_id)
        ):
            result.uid = shape.numeric_path_id
            result.verified = True
            result.confidence = 99.9
            result.evidence_sources = 1
            result.status = "VERIFIED_FAST"
            result.content_type = "PROFILE"
            result.signals = ["⚡ explicit numeric profile route"]
            return result
        story_info = extract_story_token_info(url)
        if story_info:
            result.story_token = story_info.get("token", "")
            result.story_token_decoded = story_info.get("decoded", "")
            result.story_token_type = story_info.get("type", "")
            if story_info.get("id"):
                result.story_id = story_info["id"]
            if is_numeric_id(story_info.get("owner_id", "")):
                shape.numeric_path_id = story_info["owner_id"]
                shape.route_entity = "USER"
            if story_info.get("type") == "STORY":
                shape.kind = "STORY"
                shape.story_id = story_info.get("id", "") or shape.story_id
                result.content_type = "STORY"
                result.signals.append("base64 story token → decoded STORY ID")
                result.notes.append(
                    "Decoded Story token từ URL gốc; Story ID được giữ độc lập với UID publisher."
                )
        snapshot = self.fetcher.fetch(
            url
        )
        result.http_status = (
            snapshot.status
        )
        final_url = (
            snapshot.final_url
            or url
        )
        final_shape = URLParser.parse(
            final_url
        )
        if shape.kind == "UNKNOWN" and not shape.wrapper and any(
            x.lower() in {"login.php", "logout.php", "checkpoint", "recover", "registration", "reg", "privacy", "security"}
            for x in shape.segments
        ):
            result.status = "FETCH_LIMITED" if snapshot.limited or snapshot.error else "NOT_VERIFIED"
            result.notes.append("Facebook authentication/system endpoint; không phải profile công khai.")
            return result
        original_shape = URLParser.parse(url)
        share_target_snapshot = None
        if original_shape.wrapper:
            # /share/<opaque-token> is not itself an identity.  Resolve it
            # only through ordinary public HTTP redirects/metadata.  Probe
            # Facebook's public host variants with the same exact path; this
            # is discovery, not an authentication bypass.
            probe_urls = []
            parsed_input = urlparse(url)
            exact_path = parsed_input.path or "/"
            exact_query = parsed_input.query
            for host in (
                parsed_input.netloc,
                "www.facebook.com",
                "m.facebook.com",
                "mbasic.facebook.com",
            ):
                if not host:
                    continue
                probe = urlunparse((
                    "https",
                    host,
                    exact_path,
                    "",
                    exact_query,
                    "",
                ))
                probe_urls.append(
                    normalize_facebook_url(probe)
                )
            probe_urls = unique_keep_order(probe_urls)[:MAX_SHARE_PROBES]

            snapshots = [(url, snapshot)]
            remaining_probes = [
                probe_url for probe_url in probe_urls if probe_url != url
            ]
            snapshots.extend(
                self._fetch_parallel(
                    remaining_probes,
                    referer=url,
                    max_workers=SHARE_SWEEP_CONCURRENCY,
                )
            )

            def public_target_from_snapshot(ps):
                candidates = []

                # Redirect targets are the strongest discovery signal.
                for candidate in ps.redirect_chain:
                    if not candidate:
                        continue
                    parsed = URLParser.parse(candidate)
                    if (
                        parsed.kind in {
                            "POST", "REEL", "VIDEO", "PHOTO",
                            "STORY", "ALBUM", "GROUP_POST",
                            "PROFILE",
                        }
                        and parsed.route_confidence >= 92
                    ):
                        candidates.append(
                            (100.0, candidate, parsed)
                        )

                # og:url / profile:url are weaker than a redirect but still
                # explicit public canonical metadata.
                for key in ("og:url", "profile:url"):
                    candidate = ps.meta.get(key, "")
                    if not candidate or not is_facebook_host(
                        urlparse(candidate).netloc
                    ):
                        continue
                    parsed = URLParser.parse(candidate)
                    if (
                        parsed.kind in {
                            "POST", "REEL", "VIDEO", "PHOTO",
                            "STORY", "ALBUM", "GROUP_POST",
                            "PROFILE",
                        }
                        and parsed.route_confidence >= 92
                    ):
                        candidates.append(
                            (90.0, candidate, parsed)
                        )

                # IMPORTANT: never promote arbitrary links from a share
                # landing page to the share target. Facebook landing pages
                # contain generic STORY/VIDEO/PAGE links (for example Terms,
                # login/help pages). Those links are not evidence that the
                # opaque share token points to that object.
                #
                # Only explicit redirect targets and explicit canonical
                # metadata are allowed to resolve a share token. Identity
                # links remain useful later, after a real target has been
                # established, for author correlation.

                if not candidates:
                    return None
                candidates.sort(
                    key=lambda x: x[0],
                    reverse=True,
                )
                return candidates[0]

            best = None
            for probe_url, ps in snapshots:
                found = public_target_from_snapshot(ps)
                if found is None:
                    continue
                score, target_url, target_shape = found
                if best is None or score > best[0]:
                    best = (
                        score,
                        target_url,
                        target_shape,
                        ps,
                    )

            if best is not None:
                _, target_url, target_shape, source_snapshot = best
                target_snapshot = source_snapshot

                # If the source snapshot itself is not the actual target,
                # fetch the explicit public target once for full evidence.
                if normalize_facebook_url(target_url) != normalize_facebook_url(
                    source_snapshot.final_url or source_snapshot.url
                ):
                    try:
                        target_snapshot = self.fetcher.fetch(
                            target_url,
                            referer=source_snapshot.final_url or url,
                        )
                    except Exception:
                        LOGGER.debug(
                            "share target fetch failed: %s",
                            target_url,
                            exc_info=True,
                        )

                shape = target_shape
                snapshot = target_snapshot
                result.http_status = snapshot.status

                canonical = (
                    snapshot.meta.get("og:url", "")
                    or target_url
                    or snapshot.final_url
                )
                if (
                    canonical
                    and is_facebook_host(
                        urlparse(canonical).netloc
                    )
                ):
                    canonical = normalize_facebook_url(canonical)

                canonical_shape = URLParser.parse(canonical)
                if canonical_shape.kind == "UNKNOWN" and any(
                    part.lower() in {
                        "login.php", "login", "checkpoint", "recover",
                        "registration", "reg", "privacy", "security",
                    }
                    for part in canonical_shape.segments
                ):
                    canonical = ""
                    canonical_shape = URLShape()
                if canonical_shape.kind in {
                    "POST", "REEL", "VIDEO", "PHOTO",
                    "STORY", "ALBUM", "GROUP_POST",
                    "PROFILE",
                }:
                    shape = canonical_shape
                    result.canonical_url = canonical
                    result.content_url = canonical
                else:
                    result.canonical_url = normalize_facebook_url(target_url)
                    result.content_url = result.canonical_url

                if shape.kind == "PROFILE" and shape.route_entity == "USER":
                    result.notes.append(
                        "Share wrapper → public USER profile; tiếp tục quét identity evidence của profile."
                    )
                else:
                    result.notes.append(
                        "Share wrapper đã resolve tới public profile; UID sẽ chỉ VERIFIED nếu profile HTTP trả identity evidence."
                    )

            if shape.kind == "SHARE_WRAPPER":
                result.status = (
                    "FETCH_LIMITED"
                    if any(
                        ps.limited or ps.error
                        for _, ps in snapshots
                    )
                    else "NOT_VERIFIED"
                )
                result.notes.append(
                    "Share token không expose public canonical content "
                    "trong các response HTTP công khai; không suy diễn UID."
                )
                return result
        else:
            shape = final_shape
        canonical = (
            snapshot.meta.get(
                "og:url",
                "",
            )
            or final_url
            or url
        )
        if is_facebook_host(
            urlparse(
                canonical
            ).netloc
        ):
            canonical = (
                normalize_facebook_url(
                    canonical
                )
            )
        result.canonical_url = (
            canonical
        )
        result.content_url = (
            canonical
            if canonical
            else final_url
        )
        canonical_shape = (
            URLParser.parse(
                result.canonical_url
            )
        )
        if (
            canonical_shape.kind
            not in {
                "UNKNOWN",
                "HOME",
                "SHARE_WRAPPER",
            }
        ):
            shape = canonical_shape
        # Preserve exact content identity from the original URL when a
        # redirect/og:url loses the more specific route.
        for attr in (
            "post_id", "reel_id", "video_id",
            "photo_id", "story_id", "album_id",
        ):
            original_value = getattr(original_shape, attr, "")
            if original_value and not getattr(shape, attr, ""):
                setattr(shape, attr, original_value)
                if shape.kind == "UNKNOWN":
                    shape.kind = original_shape.kind
                    shape.route_entity = original_shape.route_entity
        collector = EvidenceCollector()
        scan_url_evidence(
            shape,
            snapshot,
            collector,
        )
        scan_meta(
            snapshot,
            collector,
        )
        scan_jsonld(
            snapshot,
            collector,
        )
        scan_scripts(
            snapshot,
            collector,
        )
        scan_html_identity(
            snapshot,
            collector,
        )
        scan_html_forensic_identity(
            snapshot,
            collector,
        )
        scan_html_profile_links(
            snapshot,
            collector,
        )
        content_classifier = (
            ContentClassifier()
        )
        content = (
            content_classifier.classify(
                shape,
                collector,
                snapshot,
            )
        )
        entity_classifier = (
            EntityClassifier()
        )
        entity = (
            entity_classifier.classify(
                shape,
                collector,
                [snapshot],
            )
        )
        profile = extract_profile_info(
            shape,
            snapshot,
            collector,
            entity,
        )
        profile_urls = (
            self.extract_profile_urls(
                shape,
                snapshot,
                profile,
                entity,
            )
        )

        # Deep public-profile sweep.  A vanity profile can expose its numeric
        # identity only on a different public representation (mobile/basic,
        # about, photos, videos, posts, etc.).  Probe several representations
        # and merge their evidence instead of trusting the first HTML response.
        profile_username = clean_text(
            profile.username or shape.username
        ).lstrip("@").strip()
        if profile_username and (shape.route_entity == "USER" or shape.kind == "PROFILE"):
            encoded_username = quote(profile_username, safe="@.*-")
            # IMPORTANT: profile identity probes must run FIRST.  The previous
            # implementation appended them after arbitrary profile links from
            # the landing HTML, so MAX_PROFILE_CHECKS could be exhausted before
            # the resolver ever fetched the user's own profile.
            deep_profile_variants = []
            # Spread the first probes across Facebook's public renderers.
            # The previous host-first ordering spent almost the whole profile
            # budget on www.facebook.com before m/mbasic were tried.
            suffixes = (
                "",
                "/about",
                "/about_contact_and_basic_info",
                "/posts",
                "/photos",
                "/videos",
                "/reels",
            )
            hosts = (
                "www.facebook.com",
                "m.facebook.com",
                "mbasic.facebook.com",
            )
            for suffix in suffixes:
                for host in hosts:
                    deep_profile_variants.append(
                        f"https://{host}/{encoded_username}{suffix}"
                    )
            for host in hosts:
                deep_profile_variants.extend([
                    f"https://{host}/{encoded_username}?sk=about",
                    f"https://{host}/{encoded_username}?sk=profile",
                    f"https://{host}/{encoded_username}?locale=en_US",
                ])
            # Own-profile probes first, discovered third-party profile links
            # only after them.
            profile_urls = unique_keep_order(
                deep_profile_variants + profile_urls
            )

        profile_snapshots = []
        seen_profile_urls = set()
        profile_targets = []
        for profile_url in profile_urls[:MAX_PROFILE_CHECKS]:
            normalized_profile_url = normalize_facebook_url(profile_url)
            if normalized_profile_url in seen_profile_urls:
                continue
            seen_profile_urls.add(normalized_profile_url)
            if normalized_profile_url == normalize_facebook_url(
                result.content_url
            ):
                profile_snapshots.append(snapshot)
                continue
            profile_targets.append(profile_url)

        fetched_profiles = self._fetch_parallel(
            profile_targets,
            referer=result.content_url,
            max_workers=PROFILE_SWEEP_CONCURRENCY,
        )
        for profile_url, ps in fetched_profiles:
            profile_snapshots.append(ps)
            scan_meta(ps, collector)
            scan_jsonld(ps, collector)
            scan_scripts(ps, collector)
            scan_html_identity(ps, collector)
            scan_html_forensic_identity(ps, collector)
            if profile_username and (
                shape.route_entity == "USER"
                or shape.kind == "PROFILE"
            ):
                scan_profile_uid_correlation(
                    ps,
                    collector,
                    profile_username,
                )
            scan_html_profile_links(ps, collector)
            profile_url_from_meta = ps.meta.get("og:url", "")
            if (
                profile_url_from_meta
                and profile.username
                and profile.username.lower()
                in profile_url_from_meta.lower()
            ):
                collector.add(
                    profile_url_from_meta,
                    role="PROFILE_CANONICAL",
                    source="profile_meta",
                    weight=90,
                )
        # Run the dedicated username↔generic-id correlation over EVERY profile
        # snapshot, including the original target snapshot that may have been
        # reused instead of fetched again.
        if profile_username and (shape.route_entity == "USER" or shape.kind == "PROFILE"):
            for ps in profile_snapshots:
                scan_profile_uid_correlation(
                    ps,
                    collector,
                    profile_username,
                )

        # No separate weak fallback is needed: the deep sweep above already
        # covers mobile/basic/public profile representations and all of the
        # important public profile tabs.

        entity = (
            entity_classifier.classify(
                shape,
                collector,
                profile_snapshots
                or [snapshot],
            )
        )
        profile.entity_type = (
            entity.publisher
        )
        verifier = IdentityVerifier()
        verification = (
            verifier.verify(
                shape=shape,
                snapshot=snapshot,
                profile_snapshots=profile_snapshots,
                collector=collector,
                classification=entity,
                profile=profile,
            )
        )
        if not verification.verified:
            verification = self._apply_fallback(
                url=url,
                shape=shape,
                snapshot=snapshot,
                profile=profile,
                entity=entity,
                verification=verification,
                collector=collector,
                result=result,
            )
        result.entity_type = (
            entity.publisher
        )
        result.publisher_type = (
            entity.publisher
        )
        result.name = profile.name
        result.username = (
            profile.username
        )
        result.profile_url = (
            profile.profile_url
        )
        result.avatar_url = (
            profile.avatar_url
        )
        result.bio = profile.bio
        result.content_type = (
            "STORY"
            if result.story_token_type == "STORY"
            else content.content_type
        )
        result.post_id = (
            content.post_id
        )
        result.video_id = (
            content.video_id
        )
        result.reel_id = (
            content.reel_id
        )
        result.photo_id = (
            content.photo_id
        )
        result.story_id = (
            content.story_id
            or result.story_id
            or shape.story_id
        )
        result.story_token = result.story_token or shape.story_token
        result.story_token_decoded = (
            result.story_token_decoded or shape.story_token_decoded
        )
        result.story_token_type = (
            result.story_token_type or shape.story_token_type
        )
        result.album_id = (
            content.album_id
        )
        result.publisher_id = (
            entity.page_id
            if entity.publisher == "PAGE"
            else (
                entity.group_id
                if entity.publisher == "GROUP"
                else ""
            )
        )
        if (
            entity.publisher == "USER"
            and verification.verified
        ):
            result.uid = (
                verification.uid
            )
        if entity.publisher in {
            "PAGE",
            "GROUP",
        }:
            result.author_uid = (
                entity.author_uid
            )
        result.verified = (
            verification.verified
        )
        result.confidence = (
            verification.confidence
        )
        result.evidence_sources = (
            verification.sources
        )
        result.signals = (
            unique_keep_order(
                content.signals
                + entity.signals
                + verification.signals
            )[:MAX_SIGNALS]
        )
        if result.fallback_used:
            result.signals = unique_keep_order(
                result.signals
                + ["⚡ Secondary resolver engaged"]
            )[:MAX_SIGNALS]
        # Recover content IDs from HTML only as a content fallback; never
        # promote these values to USER UID candidates.
        if result.content_type in {
            "POST", "GROUP_POST", "REEL", "VIDEO", "PHOTO", "STORY", "ALBUM"
        }:
            if result.content_type in {"POST", "GROUP_POST"} and not result.post_id:
                ids = extract_content_identifiers(snapshot.html)
                if ids:
                    result.post_id = ids[0]
                    result.signals.append("content ID recovered from HTML")
            elif result.content_type == "REEL" and not result.reel_id:
                ids = extract_content_identifiers(snapshot.html)
                if ids:
                    result.reel_id = ids[0]
                    result.signals.append("reel ID recovered from HTML")
        result.title = (
            self.extract_content_title(
                snapshot,
                collector,
                profile,
                content,
            )
        )
        if result.verified:
            result.status = (
                "VERIFIED_FALLBACK"
                if result.fallback_used
                else "VERIFIED"
            )
        elif (
            snapshot.limited
            or snapshot.error
        ):
            result.status = (
                "FETCH_LIMITED"
            )
        else:
            result.status = (
                "NOT_VERIFIED"
            )
        if not result.verified:
            if (
                original_shape.wrapper
                and shape.kind == "PROFILE"
                and shape.route_entity == "USER"
                and profile.username
            ):
                result.notes.append(
                    verification.reason
                    or (
                        "Đã xác định share → USER profile @"
                        + profile.username
                        + ", nhưng public HTTP response chưa cung cấp identity evidence đủ mạnh để xác minh numeric UID."
                    )
                )
            else:
                result.notes.append(
                    verification.reason
                    or "UID withheld."
                )
        if generic_name(
            result.name
        ):
            result.name = ""
        if (
            result.publisher_type
            == "GROUP"
        ):
            result.group_name = (
                result.name
            )
        elif (
            result.publisher_type
            == "PAGE"
        ):
            result.page_name = (
                result.name
            )
        return result
    def extract_profile_urls(
        self,
        shape: URLShape,
        snapshot: PageSnapshot,
        profile: ProfileInfo,
        entity: EntityClassification,
    ) -> List[str]:
        candidates = []
        if profile.profile_url:
            candidates.append(
                profile.profile_url
            )
        if shape.username:
            candidates.append(
                "https://www.facebook.com/"
                + quote(
                    shape.username,
                    safe="@.*-",
                )
            )
        for key in (
            "og:url",
            "profile:url",
        ):
            value = snapshot.meta.get(
                key,
                "",
            )
            if (
                value
                and is_facebook_host(
                    urlparse(
                        value
                    ).netloc
                )
            ):
                parsed = URLParser.parse(
                    value
                )
                if (
                    parsed.kind
                    == "PROFILE"
                    and parsed.username
                ):
                    candidates.append(
                        normalize_facebook_url(
                            value
                        )
                    )
        for href in snapshot.links:
            href = urljoin(
                snapshot.final_url
                or snapshot.url,
                href,
            )
            if not is_facebook_host(
                urlparse(
                    href
                ).netloc
            ):
                continue
            parsed = URLParser.parse(
                href
            )
            if (
                parsed.kind
                == "PROFILE"
                and parsed.username
            ):
                candidates.append(
                    normalize_facebook_url(
                        href
                    )
                )
        return unique_keep_order(
            candidates
        )
    @staticmethod
    def extract_content_title(
        snapshot: PageSnapshot,
        collector: EvidenceCollector,
        profile: ProfileInfo,
        content: ContentClassification,
    ) -> str:
        titles = collector.values_for(
            "TITLE"
        )
        for title in titles:
            title = clean_title(
                title
            )
            if (
                title
                and not generic_name(title)
            ):
                return truncate(
                    title,
                    500,
                )
        title = clean_title(
            snapshot.meta.get(
                "og:title",
                "",
            )
        )
        if title:
            return truncate(
                title,
                500,
            )
        return ""
ENTITY_LABELS = {
    "USER": "👤 Cá nhân",
    "PAGE": "📄 Trang",
    "GROUP": "👥 Nhóm",
    "EVENT": "📅 Sự kiện",
    "UNKNOWN": "❔ Chưa xác định",
}
CONTENT_LABELS = {
    "PROFILE": "👤 PROFILE",
    "POST": "📝 POST",
    "GROUP_POST": "👥 GROUP POST",
    "REEL": "🎬 REEL",
    "VIDEO": "🎥 VIDEO",
    "PHOTO": "📷 PHOTO",
    "STORY": "⭕ STORY",
    "ALBUM": "🖼 ALBUM",
    "CONTENT": "📦 CONTENT",
    "UNKNOWN": "❔ UNKNOWN",
}
def safe_href(
    url: str,
) -> str:
    if not url:
        return ""
    return html_lib.escape(
        url,
        quote=True,
    )
def link(
    url: str,
    label: str,
) -> str:
    if not url:
        return ""
    return (
        f'<a href="{safe_href(url)}">'
        f"{tg_escape(label)}"
        f"</a>"
    )
def entity_label(
    value: str,
) -> str:
    return ENTITY_LABELS.get(
        value,
        "❔ Chưa xác định",
    )
def content_label(
    value: str,
) -> str:
    return CONTENT_LABELS.get(
        value,
        value or "❔ UNKNOWN",
    )
def format_id(
    label: str,
    value: str,
) -> str:
    if not value:
        return ""
    return (
        f"│ {tg_escape(label):<10}: "
        f"<code>{tg_escape(value)}</code>"
    )
def format_result(
    index: int,
    result: ResolveResult,
) -> str:
    lines = []
    lines.append(
        "╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮"
    )
    lines.append(
        f"│ 🛰️ <b>FB UID INTELLIGENCE // #{index}</b>"
    )
    lines.append(
        "│ ⚡ <code>PUBLIC-HTTP</code> • <code>PARALLEL</code> • <code>FORENSIC</code>"
    )
    lines.append(
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫"
    )
    publisher = result.publisher_type
    if publisher == "USER":
        lines.append(
            "│ 👤 <b>NGƯỜI ĐĂNG</b>"
        )
        if result.name:
            lines.append(
                "│ Tên       : "
                + tg_escape(
                    result.name
                )
            )
        if result.username:
            lines.append(
                "│ Username  : @"
                + tg_escape(
                    result.username.lstrip("@")
                )
            )
        if result.uid:
            lines.append(
                "│ UID       : "
                f"<code>{tg_escape(result.uid)}</code>"
            )
        else:
            lines.append(
                "│ UID       : "
                "⚠️ Chưa xác minh công khai"
            )
        lines.append(
            "│ Loại      : "
            + entity_label(publisher)
        )
        if result.bio:
            lines.append(
                "│ Bio       : "
                + tg_escape(
                    truncate(
                        result.bio,
                        350,
                    )
                )
            )
        if result.avatar_url:
            lines.append(
                "│ Avatar    : "
                + link(
                    result.avatar_url,
                    "🖼 Mở ảnh",
                )
            )
    elif publisher == "PAGE":
        lines.append(
            "│ 📄 <b>TRANG FACEBOOK</b>"
        )
        if result.name:
            lines.append(
                "│ Tên trang : "
                + tg_escape(
                    result.name
                )
            )
        if result.username:
            lines.append(
                "│ Username  : @"
                + tg_escape(
                    result.username.lstrip("@")
                )
            )
        if result.publisher_id:
            lines.append(
                "│ Page ID   : "
                f"<code>{tg_escape(result.publisher_id)}</code>"
            )
        if result.author_uid:
            lines.append(
                "│ Tác giả   : "
                f"<code>{tg_escape(result.author_uid)}</code>"
            )
        lines.append(
            "│ Loại      : "
            + entity_label(publisher)
        )
        if result.bio:
            lines.append(
                "│ Mô tả     : "
                + tg_escape(
                    truncate(
                        result.bio,
                        350,
                    )
                )
            )
    elif publisher == "GROUP":
        lines.append(
            "│ 👥 <b>NHÓM FACEBOOK</b>"
        )
        if result.name:
            lines.append(
                "│ Tên nhóm  : "
                + tg_escape(
                    result.name
                )
            )
        if result.publisher_id:
            lines.append(
                "│ Group ID  : "
                f"<code>{tg_escape(result.publisher_id)}</code>"
            )
        if result.author_uid:
            lines.append(
                "│ Tác giả   : "
                f"<code>{tg_escape(result.author_uid)}</code>"
            )
        lines.append(
            "│ Loại      : "
            + entity_label(publisher)
        )
    else:
        lines.append(
            "│ 👤 <b>CHỦ THỂ</b>"
        )
        if result.name:
            lines.append(
                "│ Tên       : "
                + tg_escape(
                    result.name
                )
            )
        if result.author_uid:
            lines.append(
                "│ Author UID: "
                f"<code>{tg_escape(result.author_uid)}</code>"
            )
        lines.append(
            "│ Loại      : "
            + entity_label(publisher)
        )
    if result.content_type != "PROFILE":
        lines.append("│")
        lines.append(
            "│ 📦 <b>NỘI DUNG</b>"
        )
        lines.append(
            "│ Type      : "
            + content_label(
                result.content_type
            )
        )
        if result.post_id:
            lines.append(
                "│ Post ID   : "
                f"<code>{tg_escape(result.post_id)}</code>"
            )
        elif result.content_type in {"POST", "GROUP_POST"}:
            lines.append(
                "│ Post ID   : ⚠️ Chưa tìm thấy public"
            )
        if result.reel_id:
            lines.append(
                "│ Reel ID   : "
                f"<code>{tg_escape(result.reel_id)}</code>"
            )
        if result.video_id:
            lines.append(
                "│ Video ID  : "
                f"<code>{tg_escape(result.video_id)}</code>"
            )
        if result.photo_id:
            lines.append(
                "│ Photo ID  : "
                f"<code>{tg_escape(result.photo_id)}</code>"
            )
        if result.story_id:
            lines.append(
                "│ Story ID  : "
                f"<code>{tg_escape(result.story_id)}</code>"
            )
        if result.story_token:
            lines.append(
                "│ Story token: "
                f"<code>{tg_escape(result.story_token)}</code>"
            )
        if result.album_id:
            lines.append(
                "│ Album ID  : "
                f"<code>{tg_escape(result.album_id)}</code>"
            )
        if result.title:
            lines.append(
                "│ Tiêu đề   : "
                + tg_escape(
                    truncate(
                        result.title,
                        450,
                    )
                )
            )
    if publisher == "GROUP":
        lines.append("│")
        lines.append(
            "│ 👥 <b>NGUỒN ĐĂNG</b>"
        )
        lines.append(
            "│ Loại      : 👥 GROUP"
        )
        if result.publisher_id:
            lines.append(
                "│ Group ID  : "
                f"<code>{tg_escape(result.publisher_id)}</code>"
            )
        if result.author_uid:
            lines.append(
                "│ Người đăng: "
                f"<code>{tg_escape(result.author_uid)}</code>"
            )
    elif publisher == "PAGE":
        lines.append("│")
        lines.append(
            "│ 📄 <b>NGUỒN ĐĂNG</b>"
        )
        lines.append(
            "│ Loại      : 📄 PAGE"
        )
        if result.publisher_id:
            lines.append(
                "│ Page ID   : "
                f"<code>{tg_escape(result.publisher_id)}</code>"
            )
        if result.author_uid:
            lines.append(
                "│ Người đăng: "
                f"<code>{tg_escape(result.author_uid)}</code>"
            )
    lines.append("│")
    lines.append(
        "│ 🔗 <b>LIÊN KẾT</b>"
    )
    if result.profile_url:
        label = (
            "Mở trang"
            if publisher not in {
                "PAGE",
                "GROUP",
            }
            else (
                "Mở Page"
                if publisher == "PAGE"
                else "Mở Group"
            )
        )
        lines.append(
            "│ Profile   : "
            + link(
                result.profile_url,
                label,
            )
        )
    if (
        result.content_type != "PROFILE"
        and result.content_url
    ):
        lines.append(
            "│ Content   : "
            + link(
                result.content_url,
                "Mở nội dung",
            )
        )
    lines.append("│")
    lines.append(
        "│ 🔬 <b>DẤU HIỆU</b>"
    )
    signals = (
        result.signals
        or [
            "Không có tín hiệu đủ mạnh."
        ]
    )
    for signal in signals[
        :MAX_SIGNALS
    ]:
        lines.append(
            "│ • "
            + tg_escape(signal)
        )
    lines.append("│")
    lines.append(
        "│ 🛡 <b>XÁC MINH</b>"
    )
    if result.verified:
        lines.append(
            "│ Trạng thái: "
            + (
                "⚡ <b>VERIFIED + FALLBACK</b>"
                if result.fallback_used
                else "✅ <b>VERIFIED</b>"
            )
        )
        lines.append(
            "│ Độ tin cậy: "
            f"<b>{result.confidence:.1f}%</b>"
        )
        if result.fallback_used:
            lines.append(
                "│ Fallback   : "
                + (
                    f"<code>{tg_escape(result.fallback_uid)}</code>"
                    if result.fallback_uid
                    else "đã gọi"
                )
            )
            lines.append(
                "│ Stage      : "
                + tg_escape(result.fallback_stage or "API")
            )
            if result.fallback_object_id:
                lines.append(
                    "│ Target ID  : "
                    f"<code>{tg_escape(result.fallback_object_id)}</code>"
                )
            if result.fallback_probes:
                lines.append(
                    "│ Probes     : "
                    f"{result.fallback_probes} public routes"
                )
            lines.append(
                "│ Fallback t : "
                f"{result.fallback_elapsed:.2f}s"
            )
        lines.append(
            "│ Nguồn     : "
            f"{result.evidence_sources} nguồn"
        )
    else:
        lines.append(
            "│ Trạng thái: "
            "⚠️ <b>"
            + tg_escape(
                result.status
            )
            + "</b>"
        )
        lines.append(
            "│ UID       : "
            "🔒 Đã ẩn vì chưa đủ bằng chứng"
        )
        if result.notes:
            lines.append(
                "│ Lý do     : "
                + tg_escape(
                    truncate(
                        result.notes[0],
                        450,
                    )
                )
            )
    lines.append(
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )
    # Compact operator telemetry: fast to scan in Telegram and useful for
    # spotting when the resolver had to leave the primary path.
    mode = "PRIMARY" if not result.fallback_used else "FAILOVER"
    verdict = "VERIFIED" if result.verified else "UNVERIFIED"
    lines.append(
        f"⚙️ <b>{mode}</b>  •  <b>{verdict}</b>  •  "
        f"{result.elapsed:.2f}s  •  {result.evidence_sources} src"
    )
    return "\n".join(lines)
_resolver = FacebookResolver()
_pending_lock = asyncio.Lock()
_pending_users: Set[
    Tuple[int, int]
] = set()
_pending_sessions = {}
async def wait_for_next_facebook_url(
    bot,
    event,
    timeout: int = SESSION_TIMEOUT,
    stop_event: Optional[asyncio.Event] = None,
):
    """
    Chờ URL Facebook tiếp theo của đúng user + chat.

    Telethon không có bot.wait_for(), nên dùng temporary NewMessage
    handler. stop_event cho phép /stop ngắt vòng lặp ngay lập tức.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    chat_id = event.chat_id
    sender_id = event.sender_id

    async def callback(new_event):
        try:
            if new_event.chat_id != chat_id:
                return
            if new_event.sender_id != sender_id:
                return

            text = (
                new_event.raw_text
                or ""
            ).strip()

            if not text:
                return

            # /stop được xử lý bởi handler /stop riêng. Không coi nó là URL.
            if re.fullmatch(
                r"/stop(?:@\w+)?",
                text,
                flags=re.I,
            ):
                return

            urls = extract_urls(text)
            if not urls:
                return

            if not future.done():
                future.set_result(urls)

        except Exception:
            LOGGER.debug(
                "follow-up callback error: %r",
                new_event,
                exc_info=True,
            )

    handler = events.NewMessage()
    bot.add_event_handler(
        callback,
        handler,
    )

    wait_task = None
    stop_task = None

    try:
        wait_task = asyncio.create_task(
            asyncio.wait_for(
                future,
                timeout=timeout,
            )
        )

        tasks = [wait_task]

        if stop_event is not None:
            stop_task = asyncio.create_task(
                stop_event.wait()
            )
            tasks.append(stop_task)

        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task is not None and stop_task in done:
            return None

        if wait_task in done:
            try:
                return wait_task.result()
            except asyncio.TimeoutError:
                return []

        return []

    finally:
        for task in (
            wait_task,
            stop_task,
        ):
            if task is not None and not task.done():
                task.cancel()

        for task in (
            wait_task,
            stop_task,
        ):
            if task is not None:
                try:
                    await task
                except (
                    asyncio.CancelledError,
                    asyncio.TimeoutError,
                ):
                    pass

        try:
            bot.remove_event_handler(
                callback,
                handler,
            )
        except Exception:
            LOGGER.debug(
                "Unable to remove temporary Telethon handler",
                exc_info=True,
            )


def get_command_args(
    text: str,
) -> str:
    if not text:
        return ""
    m = re.match(
        r"^/getuidfb(?:@\w+)?(?:\s+(.*))?$",
        text.strip(),
        flags=re.I | re.S,
    )
    if not m:
        return ""
    return (
        m.group(1)
        or ""
    ).strip()


async def resolve_async(
    url: str,
) -> ResolveResult:
    return await asyncio.to_thread(
        _resolver.resolve,
        url,
    )


async def resolve_many(
    urls: List[str],
) -> List[ResolveResult]:
    semaphore = asyncio.Semaphore(
        CONCURRENCY
    )

    async def worker(
        url: str,
    ) -> ResolveResult:
        async with semaphore:
            try:
                return await resolve_async(
                    url
                )
            except Exception:
                LOGGER.exception(
                    "Resolver worker failed: %s",
                    url,
                )
                return ResolveResult(
                    input_url=url,
                    canonical_url=url,
                    content_url=url,
                    status="FETCH_LIMITED",
                    notes=[
                        "Không thể hoàn tất "
                        "kiểm tra URL."
                    ],
                )

    tasks = [
        asyncio.create_task(
            worker(url)
        )
        for url in urls
    ]

    return await asyncio.gather(
        *tasks
    )


_resolver = FacebookResolver()
_pending_lock = asyncio.Lock()

# Mỗi (chat_id, sender_id) có đúng 1 phiên getuidfb.
# /stop chỉ dừng phiên của chính user đó, không ảnh hưởng user khác.
_active_sessions: Dict[
    Tuple[int, int],
    Dict[str, Any],
] = {}


async def _cleanup_getuidfb_session(
    session_key: Tuple[int, int],
    task: Optional[asyncio.Task] = None,
):
    async with _pending_lock:
        current = _active_sessions.get(
            session_key
        )
        if not current:
            return

        current_task = current.get("task")

        if (
            task is None
            or current_task is task
            or current_task is None
        ):
            _active_sessions.pop(
                session_key,
                None,
            )


def register(
    bot,
    notify_bot=None,
):
    """
    Telethon command registration.

    /getuidfb
        -> mở phiên tương tác
        -> nhận URL
        -> trả kết quả
        -> tự động chờ URL tiếp theo

    /getuidfb <url>
        -> xử lý URL ngay
        -> sau kết quả vẫn tiếp tục chờ URL tiếp theo

    /stop
        -> dừng vòng lặp getuidfb của đúng user trong đúng chat.
    """

    @bot.on(
        events.NewMessage(
            pattern=r"^/stop(?:@\w+)?$"
        )
    )
    async def getuidfb_stop_handler(
        event,
    ):
        sender_id = (
            event.sender_id
            or 0
        )
        chat_id = (
            event.chat_id
            or sender_id
        )
        session_key = (
            int(chat_id),
            int(sender_id),
        )

        async with _pending_lock:
            session = _active_sessions.get(
                session_key
            )
            if not session:
                return

            session["stopped"] = True
            stop_event = session.get(
                "stop_event"
            )
            task = session.get(
                "task"
            )

            if stop_event is not None:
                stop_event.set()

            if (
                task is not None
                and task is not asyncio.current_task()
                and not task.done()
            ):
                task.cancel()

        try:
            await event.reply(
                (
                    "🛑 <b>GETUIDFB ĐÃ DỪNG</b>\n\n"
                    "Vòng lặp của bạn đã được ngưng.\n"
                    "Dùng <code>/getuidfb</code> để bắt đầu lại."
                ),
                parse_mode="html",
            )
        except Exception:
            LOGGER.debug(
                "Unable to send GETUIDFB stop message",
                exc_info=True,
            )

    @bot.on(
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?(?:\s+.*)?$"
        )
    )
    async def getuidfb_handler(
        event,
    ):
        started = time.perf_counter()
        sender_id = (
            event.sender_id
            or 0
        )
        chat_id = (
            event.chat_id
            or sender_id
        )
        session_key = (
            int(chat_id),
            int(sender_id),
        )

        current_task = asyncio.current_task()

        # Mỗi user chỉ có 1 vòng lặp.
        # Nếu user gửi /getuidfb lần nữa, phiên cũ bị thay thế.
        old_task = None
        async with _pending_lock:
            old_session = _active_sessions.get(
                session_key
            )
            if old_session:
                old_session["stopped"] = True
                old_stop_event = old_session.get(
                    "stop_event"
                )
                if old_stop_event is not None:
                    old_stop_event.set()
                old_task = old_session.get(
                    "task"
                )

            stop_event = asyncio.Event()

            _active_sessions[
                session_key
            ] = {
                "task": current_task,
                "stop_event": stop_event,
                "stopped": False,
                "started": time.time(),
            }

        if (
            old_task is not None
            and old_task is not current_task
            and not old_task.done()
        ):
            old_task.cancel()

        try:
            args = get_command_args(
                event.raw_text
            )

            first_urls = (
                extract_urls(args)
                if args
                else []
            )

            first_round = True

            while not stop_event.is_set():
                urls = first_urls
                first_urls = []

                # Không có URL ngay sau /getuidfb -> yêu cầu URL.
                if not urls:
                    prompt_text = (
                        "🔎 <b>FACEBOOK FORENSIC "
                        "RESOLVER V60</b>\n\n"
                        "📩 Gửi link Facebook cần kiểm tra."
                        "\n\n"
                        "🔁 Sau khi có kết quả, "
                        "bạn có thể gửi link tiếp ngay."
                        "\n"
                        "🛑 Dùng <code>/stop</code> "
                        "để dừng vòng lặp của riêng bạn."
                    )

                    if first_round:
                        prompt_text = (
                            "🔎 <b>FACEBOOK FORENSIC "
                            "RESOLVER V60</b>\n\n"
                            "📩 Hãy gửi link Facebook "
                            "cần kiểm tra."
                            "\n\n"
                            "🔬 Resolver sẽ phân tích:"
                            "\n"
                            "• Redirect / canonical URL"
                            "\n"
                            "• Profile / Page / Group"
                            "\n"
                            "• POST / REEL / VIDEO / "
                            "PHOTO / STORY"
                            "\n"
                            "• HTML / Meta / JSON / "
                            "JSON-LD"
                            "\n"
                            "• UID / Page ID / Group ID"
                            "\n"
                            "• Correlation và conflict"
                            "\n\n"
                            "🔁 Có kết quả xong → "
                            "gửi URL tiếp, không cần /getuidfb."
                            "\n"
                            "🛑 <code>/stop</code> để dừng."
                        )

                    prompt = await event.reply(
                        prompt_text,
                        parse_mode="html",
                    )

                    urls = await wait_for_next_facebook_url(
                        bot,
                        event,
                        SESSION_TIMEOUT,
                        stop_event,
                    )

                    if urls is None:
                        # /stop
                        return

                    if not urls:
                        if prompt:
                            try:
                                await prompt.edit(
                                    (
                                        "⌛ <b>Phiên "
                                        "GETUIDFB hết thời gian chờ.</b>\n\n"
                                        "Dùng <code>/getuidfb</code> "
                                        "để bắt đầu lại."
                                    ),
                                    parse_mode="html",
                                )
                            except Exception:
                                pass
                        return

                first_round = False

                if stop_event.is_set():
                    return

                urls = unique_keep_order(
                    [
                        normalize_facebook_url(x)
                        for x in urls
                        if x
                    ]
                )

                if not urls:
                    continue

                skipped = 0

                if len(urls) > MAX_INPUT_URLS:
                    skipped = (
                        len(urls)
                        - MAX_INPUT_URLS
                    )
                    urls = urls[
                        :MAX_INPUT_URLS
                    ]

                # Mỗi URL có một message riêng. Chạy song song và gửi ngay
                # kết quả của URL nào hoàn thành trước, không dồn report.
                resolve_tasks = {}

                for index, url in enumerate(urls, start=1):
                    if stop_event.is_set():
                        return

                    started_one = time.perf_counter()
                    processing = await event.reply(
                        (
                            "🔬 <b>ĐANG PHÂN TÍCH FACEBOOK</b>\n\n"
                            f"🔎 Resolver #{index}\n"
                            "↪️ Redirect / canonical\n"
                            "🧩 Route classification\n"
                            "📄 HTML / Meta\n"
                            "🧠 JSON / JSON-LD\n"
                            "🎯 Identity correlation\n"
                            "🛡 Strict UID verification"
                        ),
                        parse_mode="html",
                    )

                    task = asyncio.create_task(resolve_async(url))
                    resolve_tasks[task] = (
                        index,
                        url,
                        processing,
                        started_one,
                    )

                stop_wait_task = asyncio.create_task(stop_event.wait())

                try:
                    while resolve_tasks:
                        done, _ = await asyncio.wait(
                            [*resolve_tasks.keys(), stop_wait_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        if stop_wait_task in done or stop_event.is_set():
                            for pending_task in resolve_tasks:
                                if not pending_task.done():
                                    pending_task.cancel()
                            await asyncio.gather(
                                *resolve_tasks.keys(),
                                return_exceptions=True,
                            )
                            return

                        for task in done:
                            if task is stop_wait_task:
                                continue

                            info = resolve_tasks.pop(task, None)
                            if info is None:
                                continue

                            index, url, processing, started_one = info

                            try:
                                result = task.result()
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                LOGGER.exception("Resolver worker failed: %s", url)
                                result = ResolveResult(
                                    input_url=url,
                                    canonical_url=url,
                                    content_url=url,
                                    status="FETCH_LIMITED",
                                    notes=["Không thể hoàn tất kiểm tra URL."],
                                )

                            if stop_event.is_set():
                                return

                            try:
                                block = format_result(index, result)
                            except Exception:
                                LOGGER.exception("format_result failed")
                                block = (
                                    "╭──────────────────────────\n"
                                    f"│ 🔎 <b>FACEBOOK RESOLVER #{index}</b>\n"
                                    "├──────────────────────────\n"
                                    "│ ❌ Không thể định dạng kết quả.\n"
                                    "│ UID đã được bảo vệ, không suy đoán.\n"
                                    "╰──────────────────────────"
                                )

                            verified_count = 1 if result.verified else 0
                            elapsed_one = time.perf_counter() - started_one
                            header = (
                                "🔎 <b>FACEBOOK FORENSIC RESULT</b>\n"
                                "📊 Đã kiểm tra: <b>1</b>\n"
                                f"✅ Verified: <b>{verified_count}</b>\n"
                                f"⏱ Tổng thời gian: <b>{elapsed_one:.2f}s</b>"
                            )
                            final_text = header + "\n\n" + block

                            if len(final_text) <= 3900:
                                await processing.edit(final_text, parse_mode="html")
                            else:
                                await processing.edit(header, parse_mode="html")
                                await event.respond(block, parse_mode="html")

                            if notify_bot:
                                try:
                                    await notify_bot(
                                        event,
                                        f"getuidfb | 1 URL | {verified_count} verified",
                                    )
                                except Exception:
                                    LOGGER.debug("notify_bot failed", exc_info=True)

                finally:
                    if not stop_wait_task.done():
                        stop_wait_task.cancel()
                    try:
                        await stop_wait_task
                    except asyncio.CancelledError:
                        pass

                    if resolve_tasks:
                        for pending_task in resolve_tasks:
                            if not pending_task.done():
                                pending_task.cancel()
                        await asyncio.gather(
                            *resolve_tasks.keys(),
                            return_exceptions=True,
                        )

                if stop_event.is_set():
                    return

                # ====================================================
                # ĐIỂM QUAN TRỌNG:
                # Không return sau khi có kết quả.
                # Quay lại while để nhận URL tiếp theo.
                # ====================================================
                await asyncio.sleep(0)

                

                next_urls = await wait_for_next_facebook_url(
                    bot,
                    event,
                    SESSION_TIMEOUT,
                    stop_event,
                )

                if next_urls is None:
                    return

                if not next_urls:
                    try:
                        await event.reply(
                            (
                                "⌛ <b>Phiên GETUIDFB hết "
                                "thời gian chờ.</b>\n\n"
                                "Dùng <code>/getuidfb</code> "
                                "để bắt đầu lại."
                            ),
                            parse_mode="html",
                        )
                    except Exception:
                        pass
                    return

                first_urls = next_urls

        except asyncio.CancelledError:
            LOGGER.info(
                "GETUIDFB session stopped: %s",
                session_key,
            )
            return

        except Exception as exc:
            LOGGER.exception(
                "GETUIDFB HANDLER ERROR"
            )

            try:
                await event.reply(
                    (
                        "❌ <b>FACEBOOK RESOLVER</b>\n\n"
                        "Đã xảy ra lỗi khi xử lý yêu cầu.\n\n"
                        "🛡 Resolver không suy đoán UID "
                        "khi dữ liệu chưa đủ.\n\n"
                        "<code>"
                        + tg_escape(
                            truncate(
                                str(exc),
                                400,
                            )
                        )
                        + "</code>"
                    ),
                    parse_mode="html",
                )
            except Exception:
                LOGGER.exception(
                    "Unable to send GETUIDFB error"
                )

        finally:
            await _cleanup_getuidfb_session(
                session_key,
                current_task,
            )


COMMAND_INFO = {
    "command": "getuidfb",
    "category": "🔎 FACEBOOK",
    "title": "Facebook UID Resolver",

    "description": (
        "Phân tích liên kết Facebook công khai, "
        "xác minh thông tin chủ thể và hỗ trợ nhận diện "
        "UID, username, loại nội dung cùng dữ liệu liên quan."
    ),

    "usage": "/getuidfb <facebook_url>",

    "examples": [
        "/getuidfb https://www.facebook.com/username",
        "/getuidfb https://facebook.com/profile.php?id=123456789",
        "/getuidfb https://www.facebook.com/reel/123456789",
    ],

    "details": [
        "Gửi URL Facebook công khai cùng lệnh /getuidfb.",
        "Hỗ trợ phân tích profile, page, group và nội dung Facebook.",
        "Nhận diện username, UID và loại liên kết khi dữ liệu công khai cho phép.",
        "Kiểm tra và xác minh kết quả trước khi hiển thị.",
        "Không yêu cầu hoặc xử lý thông tin riêng tư.",
        "Chỉ hoạt động với dữ liệu và liên kết Facebook công khai.",
        "Kết quả có thể không đầy đủ nếu Facebook yêu cầu đăng nhập hoặc giới hạn truy cập.",
    ],

    "supported": [
        "Facebook Profile",
        "Facebook Page",
        "Facebook Group",
        "Facebook Post",
        "Facebook Video",
        "Facebook Reel",
        "Facebook Photo",
        "Facebook Public URL",
    ],
}
if __name__ == "__main__":
    tests = [
        "https://www.facebook.com/kim.chi.125900/",
        "https://www.facebook.com/kim.chi.125900/posts/123456789/",
        "https://www.facebook.com/kim.chi.125900/reel/123456789/",
        "https://www.facebook.com/groups/123456789/posts/987654321/",
        "https://www.facebook.com/share/r/1H1EjsEW7J/",
        "https://www.facebook.com/profile.php?id=61553239356646",
    ]
    for test in tests:
        shape = URLParser.parse(
            test
        )

# -----------------------------------------------------------------------------
# BUILT-IN REGRESSION CORPUS / QA FIXTURES
# -----------------------------------------------------------------------------
# These are executable parser fixtures, not filler. They exercise the URL
# grammar and fallback payload shapes without making network requests.  They
# are kept in-source so deployments can run a deterministic self-test.
REGRESSION_URL_CASES = [
    ('https://www.facebook.com/profile.php?id=100000000000001', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_2', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_3', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_4/posts/100000000000004', 'POST'),
    ('https://www.facebook.com/qa_profile_5/videos/100000000000005', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_6/reel/100000000000006', 'REEL'),
    ('https://www.facebook.com/qa_profile_7/photos/100000000000007', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000008', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000009/posts/200000000000009', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000010', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000011', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000012', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000013&id=615000000000013', 'STORY'),
    ('https://www.facebook.com/share/QaToken000014/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000015/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000016/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000017/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_18/100000000000018', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000019', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_20', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_21', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_22/posts/100000000000022', 'POST'),
    ('https://www.facebook.com/qa_profile_23/videos/100000000000023', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_24/reel/100000000000024', 'REEL'),
    ('https://www.facebook.com/qa_profile_25/photos/100000000000025', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000026', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000027/posts/200000000000027', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000028', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000029', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000030', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000031&id=615000000000031', 'STORY'),
    ('https://www.facebook.com/share/QaToken000032/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000033/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000034/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000035/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_36/100000000000036', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000037', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_38', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_39', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_40/posts/100000000000040', 'POST'),
    ('https://www.facebook.com/qa_profile_41/videos/100000000000041', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_42/reel/100000000000042', 'REEL'),
    ('https://www.facebook.com/qa_profile_43/photos/100000000000043', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000044', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000045/posts/200000000000045', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000046', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000047', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000048', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000049&id=615000000000049', 'STORY'),
    ('https://www.facebook.com/share/QaToken000050/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000051/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000052/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000053/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_54/100000000000054', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000055', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_56', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_57', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_58/posts/100000000000058', 'POST'),
    ('https://www.facebook.com/qa_profile_59/videos/100000000000059', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_60/reel/100000000000060', 'REEL'),
    ('https://www.facebook.com/qa_profile_61/photos/100000000000061', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000062', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000063/posts/200000000000063', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000064', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000065', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000066', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000067&id=615000000000067', 'STORY'),
    ('https://www.facebook.com/share/QaToken000068/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000069/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000070/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000071/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_72/100000000000072', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000073', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_74', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_75', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_76/posts/100000000000076', 'POST'),
    ('https://www.facebook.com/qa_profile_77/videos/100000000000077', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_78/reel/100000000000078', 'REEL'),
    ('https://www.facebook.com/qa_profile_79/photos/100000000000079', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000080', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000081/posts/200000000000081', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000082', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000083', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000084', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000085&id=615000000000085', 'STORY'),
    ('https://www.facebook.com/share/QaToken000086/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000087/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000088/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000089/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_90/100000000000090', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000091', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_92', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_93', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_94/posts/100000000000094', 'POST'),
    ('https://www.facebook.com/qa_profile_95/videos/100000000000095', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_96/reel/100000000000096', 'REEL'),
    ('https://www.facebook.com/qa_profile_97/photos/100000000000097', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000098', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000099/posts/200000000000099', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000100', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000101', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000102', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000103&id=615000000000103', 'STORY'),
    ('https://www.facebook.com/share/QaToken000104/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000105/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000106/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000107/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_108/100000000000108', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000109', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_110', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_111', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_112/posts/100000000000112', 'POST'),
    ('https://www.facebook.com/qa_profile_113/videos/100000000000113', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_114/reel/100000000000114', 'REEL'),
    ('https://www.facebook.com/qa_profile_115/photos/100000000000115', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000116', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000117/posts/200000000000117', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000118', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000119', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000120', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000121&id=615000000000121', 'STORY'),
    ('https://www.facebook.com/share/QaToken000122/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000123/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000124/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000125/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_126/100000000000126', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000127', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_128', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_129', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_130/posts/100000000000130', 'POST'),
    ('https://www.facebook.com/qa_profile_131/videos/100000000000131', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_132/reel/100000000000132', 'REEL'),
    ('https://www.facebook.com/qa_profile_133/photos/100000000000133', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000134', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000135/posts/200000000000135', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000136', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000137', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000138', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000139&id=615000000000139', 'STORY'),
    ('https://www.facebook.com/share/QaToken000140/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000141/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000142/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000143/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_144/100000000000144', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000145', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_146', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_147', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_148/posts/100000000000148', 'POST'),
    ('https://www.facebook.com/qa_profile_149/videos/100000000000149', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_150/reel/100000000000150', 'REEL'),
    ('https://www.facebook.com/qa_profile_151/photos/100000000000151', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000152', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000153/posts/200000000000153', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000154', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000155', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000156', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000157&id=615000000000157', 'STORY'),
    ('https://www.facebook.com/share/QaToken000158/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000159/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000160/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000161/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_162/100000000000162', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000163', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_164', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_165', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_166/posts/100000000000166', 'POST'),
    ('https://www.facebook.com/qa_profile_167/videos/100000000000167', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_168/reel/100000000000168', 'REEL'),
    ('https://www.facebook.com/qa_profile_169/photos/100000000000169', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000170', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000171/posts/200000000000171', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000172', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000173', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000174', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000175&id=615000000000175', 'STORY'),
    ('https://www.facebook.com/share/QaToken000176/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000177/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000178/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000179/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_180/100000000000180', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000181', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_182', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_183', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_184/posts/100000000000184', 'POST'),
    ('https://www.facebook.com/qa_profile_185/videos/100000000000185', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_186/reel/100000000000186', 'REEL'),
    ('https://www.facebook.com/qa_profile_187/photos/100000000000187', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000188', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000189/posts/200000000000189', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000190', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000191', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000192', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000193&id=615000000000193', 'STORY'),
    ('https://www.facebook.com/share/QaToken000194/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000195/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000196/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000197/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_198/100000000000198', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000199', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_200', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_201', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_202/posts/100000000000202', 'POST'),
    ('https://www.facebook.com/qa_profile_203/videos/100000000000203', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_204/reel/100000000000204', 'REEL'),
    ('https://www.facebook.com/qa_profile_205/photos/100000000000205', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000206', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000207/posts/200000000000207', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000208', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000209', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000210', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000211&id=615000000000211', 'STORY'),
    ('https://www.facebook.com/share/QaToken000212/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000213/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000214/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000215/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_216/100000000000216', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000217', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_218', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_219', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_220/posts/100000000000220', 'POST'),
    ('https://www.facebook.com/qa_profile_221/videos/100000000000221', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_222/reel/100000000000222', 'REEL'),
    ('https://www.facebook.com/qa_profile_223/photos/100000000000223', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000224', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000225/posts/200000000000225', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000226', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000227', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000228', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000229&id=615000000000229', 'STORY'),
    ('https://www.facebook.com/share/QaToken000230/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000231/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000232/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000233/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_234/100000000000234', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000235', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_236', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_237', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_238/posts/100000000000238', 'POST'),
    ('https://www.facebook.com/qa_profile_239/videos/100000000000239', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_240/reel/100000000000240', 'REEL'),
    ('https://www.facebook.com/qa_profile_241/photos/100000000000241', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000242', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000243/posts/200000000000243', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000244', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000245', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000246', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000247&id=615000000000247', 'STORY'),
    ('https://www.facebook.com/share/QaToken000248/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000249/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000250/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000251/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_252/100000000000252', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000253', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_254', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_255', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_256/posts/100000000000256', 'POST'),
    ('https://www.facebook.com/qa_profile_257/videos/100000000000257', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_258/reel/100000000000258', 'REEL'),
    ('https://www.facebook.com/qa_profile_259/photos/100000000000259', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000260', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000261/posts/200000000000261', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000262', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000263', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000264', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000265&id=615000000000265', 'STORY'),
    ('https://www.facebook.com/share/QaToken000266/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000267/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000268/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000269/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_270/100000000000270', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000271', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_272', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_273', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_274/posts/100000000000274', 'POST'),
    ('https://www.facebook.com/qa_profile_275/videos/100000000000275', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_276/reel/100000000000276', 'REEL'),
    ('https://www.facebook.com/qa_profile_277/photos/100000000000277', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000278', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000279/posts/200000000000279', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000280', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000281', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000282', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000283&id=615000000000283', 'STORY'),
    ('https://www.facebook.com/share/QaToken000284/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000285/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000286/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000287/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_288/100000000000288', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000289', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_290', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_291', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_292/posts/100000000000292', 'POST'),
    ('https://www.facebook.com/qa_profile_293/videos/100000000000293', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_294/reel/100000000000294', 'REEL'),
    ('https://www.facebook.com/qa_profile_295/photos/100000000000295', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000296', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000297/posts/200000000000297', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000298', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000299', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000300', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000301&id=615000000000301', 'STORY'),
    ('https://www.facebook.com/share/QaToken000302/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000303/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000304/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000305/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_306/100000000000306', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000307', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_308', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_309', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_310/posts/100000000000310', 'POST'),
    ('https://www.facebook.com/qa_profile_311/videos/100000000000311', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_312/reel/100000000000312', 'REEL'),
    ('https://www.facebook.com/qa_profile_313/photos/100000000000313', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000314', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000315/posts/200000000000315', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000316', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000317', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000318', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000319&id=615000000000319', 'STORY'),
    ('https://www.facebook.com/share/QaToken000320/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000321/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000322/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000323/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_324/100000000000324', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000325', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_326', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_327', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_328/posts/100000000000328', 'POST'),
    ('https://www.facebook.com/qa_profile_329/videos/100000000000329', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_330/reel/100000000000330', 'REEL'),
    ('https://www.facebook.com/qa_profile_331/photos/100000000000331', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000332', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000333/posts/200000000000333', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000334', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000335', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000336', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000337&id=615000000000337', 'STORY'),
    ('https://www.facebook.com/share/QaToken000338/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000339/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000340/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000341/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_342/100000000000342', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000343', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_344', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_345', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_346/posts/100000000000346', 'POST'),
    ('https://www.facebook.com/qa_profile_347/videos/100000000000347', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_348/reel/100000000000348', 'REEL'),
    ('https://www.facebook.com/qa_profile_349/photos/100000000000349', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000350', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000351/posts/200000000000351', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000352', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000353', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000354', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000355&id=615000000000355', 'STORY'),
    ('https://www.facebook.com/share/QaToken000356/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000357/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000358/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000359/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_360/100000000000360', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000361', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_362', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_363', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_364/posts/100000000000364', 'POST'),
    ('https://www.facebook.com/qa_profile_365/videos/100000000000365', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_366/reel/100000000000366', 'REEL'),
    ('https://www.facebook.com/qa_profile_367/photos/100000000000367', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000368', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000369/posts/200000000000369', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000370', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000371', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000372', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000373&id=615000000000373', 'STORY'),
    ('https://www.facebook.com/share/QaToken000374/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000375/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000376/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000377/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_378/100000000000378', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000379', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_380', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_381', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_382/posts/100000000000382', 'POST'),
    ('https://www.facebook.com/qa_profile_383/videos/100000000000383', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_384/reel/100000000000384', 'REEL'),
    ('https://www.facebook.com/qa_profile_385/photos/100000000000385', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000386', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000387/posts/200000000000387', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000388', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000389', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000390', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000391&id=615000000000391', 'STORY'),
    ('https://www.facebook.com/share/QaToken000392/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000393/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000394/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000395/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_396/100000000000396', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000397', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_398', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_399', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_400/posts/100000000000400', 'POST'),
    ('https://www.facebook.com/qa_profile_401/videos/100000000000401', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_402/reel/100000000000402', 'REEL'),
    ('https://www.facebook.com/qa_profile_403/photos/100000000000403', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000404', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000405/posts/200000000000405', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000406', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000407', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000408', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000409&id=615000000000409', 'STORY'),
    ('https://www.facebook.com/share/QaToken000410/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000411/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000412/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000413/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_414/100000000000414', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000415', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_416', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_417', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_418/posts/100000000000418', 'POST'),
    ('https://www.facebook.com/qa_profile_419/videos/100000000000419', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_420/reel/100000000000420', 'REEL'),
    ('https://www.facebook.com/qa_profile_421/photos/100000000000421', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000422', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000423/posts/200000000000423', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000424', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000425', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000426', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000427&id=615000000000427', 'STORY'),
    ('https://www.facebook.com/share/QaToken000428/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000429/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000430/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000431/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_432/100000000000432', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000433', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_434', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_435', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_436/posts/100000000000436', 'POST'),
    ('https://www.facebook.com/qa_profile_437/videos/100000000000437', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_438/reel/100000000000438', 'REEL'),
    ('https://www.facebook.com/qa_profile_439/photos/100000000000439', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000440', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000441/posts/200000000000441', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000442', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000443', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000444', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000445&id=615000000000445', 'STORY'),
    ('https://www.facebook.com/share/QaToken000446/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000447/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000448/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000449/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_450/100000000000450', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000451', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_452', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_453', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_454/posts/100000000000454', 'POST'),
    ('https://www.facebook.com/qa_profile_455/videos/100000000000455', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_456/reel/100000000000456', 'REEL'),
    ('https://www.facebook.com/qa_profile_457/photos/100000000000457', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000458', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000459/posts/200000000000459', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000460', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000461', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000462', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000463&id=615000000000463', 'STORY'),
    ('https://www.facebook.com/share/QaToken000464/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000465/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000466/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000467/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_468/100000000000468', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000469', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_470', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_471', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_472/posts/100000000000472', 'POST'),
    ('https://www.facebook.com/qa_profile_473/videos/100000000000473', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_474/reel/100000000000474', 'REEL'),
    ('https://www.facebook.com/qa_profile_475/photos/100000000000475', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000476', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000477/posts/200000000000477', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000478', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000479', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000480', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000481&id=615000000000481', 'STORY'),
    ('https://www.facebook.com/share/QaToken000482/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000483/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000484/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000485/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_486/100000000000486', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000487', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_488', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_489', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_490/posts/100000000000490', 'POST'),
    ('https://www.facebook.com/qa_profile_491/videos/100000000000491', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_492/reel/100000000000492', 'REEL'),
    ('https://www.facebook.com/qa_profile_493/photos/100000000000493', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000494', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000495/posts/200000000000495', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000496', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000497', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000498', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000499&id=615000000000499', 'STORY'),
    ('https://www.facebook.com/share/QaToken000500/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000501/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000502/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000503/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_504/100000000000504', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000505', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_506', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_507', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_508/posts/100000000000508', 'POST'),
    ('https://www.facebook.com/qa_profile_509/videos/100000000000509', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_510/reel/100000000000510', 'REEL'),
    ('https://www.facebook.com/qa_profile_511/photos/100000000000511', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000512', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000513/posts/200000000000513', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000514', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000515', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000516', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000517&id=615000000000517', 'STORY'),
    ('https://www.facebook.com/share/QaToken000518/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000519/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000520/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000521/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_522/100000000000522', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000523', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_524', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_525', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_526/posts/100000000000526', 'POST'),
    ('https://www.facebook.com/qa_profile_527/videos/100000000000527', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_528/reel/100000000000528', 'REEL'),
    ('https://www.facebook.com/qa_profile_529/photos/100000000000529', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000530', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000531/posts/200000000000531', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000532', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000533', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000534', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000535&id=615000000000535', 'STORY'),
    ('https://www.facebook.com/share/QaToken000536/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000537/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000538/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000539/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_540/100000000000540', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000541', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_542', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_543', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_544/posts/100000000000544', 'POST'),
    ('https://www.facebook.com/qa_profile_545/videos/100000000000545', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_546/reel/100000000000546', 'REEL'),
    ('https://www.facebook.com/qa_profile_547/photos/100000000000547', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000548', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000549/posts/200000000000549', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000550', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000551', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000552', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000553&id=615000000000553', 'STORY'),
    ('https://www.facebook.com/share/QaToken000554/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000555/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000556/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000557/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_558/100000000000558', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000559', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_560', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_561', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_562/posts/100000000000562', 'POST'),
    ('https://www.facebook.com/qa_profile_563/videos/100000000000563', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_564/reel/100000000000564', 'REEL'),
    ('https://www.facebook.com/qa_profile_565/photos/100000000000565', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000566', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000567/posts/200000000000567', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000568', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000569', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000570', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000571&id=615000000000571', 'STORY'),
    ('https://www.facebook.com/share/QaToken000572/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000573/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000574/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000575/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_576/100000000000576', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000577', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_578', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_579', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_580/posts/100000000000580', 'POST'),
    ('https://www.facebook.com/qa_profile_581/videos/100000000000581', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_582/reel/100000000000582', 'REEL'),
    ('https://www.facebook.com/qa_profile_583/photos/100000000000583', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000584', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000585/posts/200000000000585', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000586', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000587', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000588', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000589&id=615000000000589', 'STORY'),
    ('https://www.facebook.com/share/QaToken000590/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000591/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000592/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000593/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_594/100000000000594', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000595', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_596', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_597', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_598/posts/100000000000598', 'POST'),
    ('https://www.facebook.com/qa_profile_599/videos/100000000000599', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_600/reel/100000000000600', 'REEL'),
    ('https://www.facebook.com/qa_profile_601/photos/100000000000601', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000602', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000603/posts/200000000000603', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000604', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000605', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000606', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000607&id=615000000000607', 'STORY'),
    ('https://www.facebook.com/share/QaToken000608/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000609/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000610/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000611/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_612/100000000000612', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000613', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_614', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_615', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_616/posts/100000000000616', 'POST'),
    ('https://www.facebook.com/qa_profile_617/videos/100000000000617', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_618/reel/100000000000618', 'REEL'),
    ('https://www.facebook.com/qa_profile_619/photos/100000000000619', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000620', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000621/posts/200000000000621', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000622', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000623', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000624', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000625&id=615000000000625', 'STORY'),
    ('https://www.facebook.com/share/QaToken000626/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000627/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000628/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000629/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_630/100000000000630', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000631', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_632', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_633', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_634/posts/100000000000634', 'POST'),
    ('https://www.facebook.com/qa_profile_635/videos/100000000000635', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_636/reel/100000000000636', 'REEL'),
    ('https://www.facebook.com/qa_profile_637/photos/100000000000637', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000638', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000639/posts/200000000000639', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000640', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000641', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000642', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000643&id=615000000000643', 'STORY'),
    ('https://www.facebook.com/share/QaToken000644/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000645/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000646/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000647/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_648/100000000000648', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000649', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_650', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_651', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_652/posts/100000000000652', 'POST'),
    ('https://www.facebook.com/qa_profile_653/videos/100000000000653', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_654/reel/100000000000654', 'REEL'),
    ('https://www.facebook.com/qa_profile_655/photos/100000000000655', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000656', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000657/posts/200000000000657', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000658', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000659', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000660', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000661&id=615000000000661', 'STORY'),
    ('https://www.facebook.com/share/QaToken000662/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000663/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000664/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000665/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_666/100000000000666', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000667', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_668', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_669', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_670/posts/100000000000670', 'POST'),
    ('https://www.facebook.com/qa_profile_671/videos/100000000000671', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_672/reel/100000000000672', 'REEL'),
    ('https://www.facebook.com/qa_profile_673/photos/100000000000673', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000674', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000675/posts/200000000000675', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000676', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000677', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000678', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000679&id=615000000000679', 'STORY'),
    ('https://www.facebook.com/share/QaToken000680/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000681/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000682/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000683/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_684/100000000000684', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000685', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_686', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_687', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_688/posts/100000000000688', 'POST'),
    ('https://www.facebook.com/qa_profile_689/videos/100000000000689', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_690/reel/100000000000690', 'REEL'),
    ('https://www.facebook.com/qa_profile_691/photos/100000000000691', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000692', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000693/posts/200000000000693', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000694', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000695', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000696', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000697&id=615000000000697', 'STORY'),
    ('https://www.facebook.com/share/QaToken000698/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000699/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000700/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000701/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_702/100000000000702', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000703', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_704', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_705', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_706/posts/100000000000706', 'POST'),
    ('https://www.facebook.com/qa_profile_707/videos/100000000000707', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_708/reel/100000000000708', 'REEL'),
    ('https://www.facebook.com/qa_profile_709/photos/100000000000709', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000710', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000711/posts/200000000000711', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000712', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000713', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000714', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000715&id=615000000000715', 'STORY'),
    ('https://www.facebook.com/share/QaToken000716/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000717/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000718/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000719/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_720/100000000000720', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000721', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_722', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_723', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_724/posts/100000000000724', 'POST'),
    ('https://www.facebook.com/qa_profile_725/videos/100000000000725', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_726/reel/100000000000726', 'REEL'),
    ('https://www.facebook.com/qa_profile_727/photos/100000000000727', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000728', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000729/posts/200000000000729', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000730', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000731', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000732', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000733&id=615000000000733', 'STORY'),
    ('https://www.facebook.com/share/QaToken000734/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000735/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000736/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000737/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_738/100000000000738', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000739', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_740', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_741', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_742/posts/100000000000742', 'POST'),
    ('https://www.facebook.com/qa_profile_743/videos/100000000000743', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_744/reel/100000000000744', 'REEL'),
    ('https://www.facebook.com/qa_profile_745/photos/100000000000745', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000746', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000747/posts/200000000000747', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000748', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000749', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000750', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000751&id=615000000000751', 'STORY'),
    ('https://www.facebook.com/share/QaToken000752/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000753/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000754/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000755/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_756/100000000000756', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000757', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_758', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_759', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_760/posts/100000000000760', 'POST'),
    ('https://www.facebook.com/qa_profile_761/videos/100000000000761', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_762/reel/100000000000762', 'REEL'),
    ('https://www.facebook.com/qa_profile_763/photos/100000000000763', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000764', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000765/posts/200000000000765', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000766', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000767', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000768', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000769&id=615000000000769', 'STORY'),
    ('https://www.facebook.com/share/QaToken000770/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000771/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000772/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000773/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_774/100000000000774', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000775', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_776', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_777', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_778/posts/100000000000778', 'POST'),
    ('https://www.facebook.com/qa_profile_779/videos/100000000000779', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_780/reel/100000000000780', 'REEL'),
    ('https://www.facebook.com/qa_profile_781/photos/100000000000781', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000782', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000783/posts/200000000000783', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000784', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000785', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000786', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000787&id=615000000000787', 'STORY'),
    ('https://www.facebook.com/share/QaToken000788/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000789/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000790/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000791/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_792/100000000000792', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000793', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_794', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_795', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_796/posts/100000000000796', 'POST'),
    ('https://www.facebook.com/qa_profile_797/videos/100000000000797', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_798/reel/100000000000798', 'REEL'),
    ('https://www.facebook.com/qa_profile_799/photos/100000000000799', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000800', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000801/posts/200000000000801', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000802', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000803', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000804', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000805&id=615000000000805', 'STORY'),
    ('https://www.facebook.com/share/QaToken000806/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000807/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000808/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000809/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_810/100000000000810', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000811', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_812', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_813', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_814/posts/100000000000814', 'POST'),
    ('https://www.facebook.com/qa_profile_815/videos/100000000000815', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_816/reel/100000000000816', 'REEL'),
    ('https://www.facebook.com/qa_profile_817/photos/100000000000817', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000818', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000819/posts/200000000000819', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000820', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000821', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000822', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000823&id=615000000000823', 'STORY'),
    ('https://www.facebook.com/share/QaToken000824/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000825/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000826/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000827/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_828/100000000000828', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000829', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_830', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_831', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_832/posts/100000000000832', 'POST'),
    ('https://www.facebook.com/qa_profile_833/videos/100000000000833', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_834/reel/100000000000834', 'REEL'),
    ('https://www.facebook.com/qa_profile_835/photos/100000000000835', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000836', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000837/posts/200000000000837', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000838', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000839', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000840', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000841&id=615000000000841', 'STORY'),
    ('https://www.facebook.com/share/QaToken000842/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000843/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000844/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000845/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_846/100000000000846', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000847', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_848', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_849', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_850/posts/100000000000850', 'POST'),
    ('https://www.facebook.com/qa_profile_851/videos/100000000000851', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_852/reel/100000000000852', 'REEL'),
    ('https://www.facebook.com/qa_profile_853/photos/100000000000853', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000854', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000855/posts/200000000000855', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000856', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000857', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000858', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000859&id=615000000000859', 'STORY'),
    ('https://www.facebook.com/share/QaToken000860/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000861/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000862/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000863/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_864/100000000000864', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000865', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_866', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_867', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_868/posts/100000000000868', 'POST'),
    ('https://www.facebook.com/qa_profile_869/videos/100000000000869', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_870/reel/100000000000870', 'REEL'),
    ('https://www.facebook.com/qa_profile_871/photos/100000000000871', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000872', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000873/posts/200000000000873', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000874', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000875', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000876', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000877&id=615000000000877', 'STORY'),
    ('https://www.facebook.com/share/QaToken000878/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000879/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000880/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000881/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_882/100000000000882', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000883', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_884', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_885', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_886/posts/100000000000886', 'POST'),
    ('https://www.facebook.com/qa_profile_887/videos/100000000000887', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_888/reel/100000000000888', 'REEL'),
    ('https://www.facebook.com/qa_profile_889/photos/100000000000889', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000890', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000891/posts/200000000000891', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000892', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000893', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000894', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000895&id=615000000000895', 'STORY'),
    ('https://www.facebook.com/share/QaToken000896/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000897/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000898/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000899/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_900/100000000000900', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000901', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_902', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_903', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_904/posts/100000000000904', 'POST'),
    ('https://www.facebook.com/qa_profile_905/videos/100000000000905', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_906/reel/100000000000906', 'REEL'),
    ('https://www.facebook.com/qa_profile_907/photos/100000000000907', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000908', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000909/posts/200000000000909', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000910', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000911', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000912', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000913&id=615000000000913', 'STORY'),
    ('https://www.facebook.com/share/QaToken000914/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000915/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000916/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000917/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_918/100000000000918', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000919', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_920', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_921', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_922/posts/100000000000922', 'POST'),
    ('https://www.facebook.com/qa_profile_923/videos/100000000000923', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_924/reel/100000000000924', 'REEL'),
    ('https://www.facebook.com/qa_profile_925/photos/100000000000925', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000926', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000927/posts/200000000000927', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000928', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000929', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000930', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000931&id=615000000000931', 'STORY'),
    ('https://www.facebook.com/share/QaToken000932/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000933/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000934/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000935/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_936/100000000000936', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000937', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_938', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_939', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_940/posts/100000000000940', 'POST'),
    ('https://www.facebook.com/qa_profile_941/videos/100000000000941', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_942/reel/100000000000942', 'REEL'),
    ('https://www.facebook.com/qa_profile_943/photos/100000000000943', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000944', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000945/posts/200000000000945', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000946', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000947', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000948', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000949&id=615000000000949', 'STORY'),
    ('https://www.facebook.com/share/QaToken000950/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000951/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000952/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000953/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_954/100000000000954', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000955', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_956', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_957', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_958/posts/100000000000958', 'POST'),
    ('https://www.facebook.com/qa_profile_959/videos/100000000000959', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_960/reel/100000000000960', 'REEL'),
    ('https://www.facebook.com/qa_profile_961/photos/100000000000961', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000962', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000963/posts/200000000000963', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000964', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000965', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000966', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000967&id=615000000000967', 'STORY'),
    ('https://www.facebook.com/share/QaToken000968/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000969/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000970/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000971/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_972/100000000000972', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000973', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_974', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_975', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_976/posts/100000000000976', 'POST'),
    ('https://www.facebook.com/qa_profile_977/videos/100000000000977', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_978/reel/100000000000978', 'REEL'),
    ('https://www.facebook.com/qa_profile_979/photos/100000000000979', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000980', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000981/posts/200000000000981', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000000982', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000000983', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000000984', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000000985&id=615000000000985', 'STORY'),
    ('https://www.facebook.com/share/QaToken000986/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken000987/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken000988/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000000989/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_990/100000000000990', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000000991', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_992', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_993', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_994/posts/100000000000994', 'POST'),
    ('https://www.facebook.com/qa_profile_995/videos/100000000000995', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_996/reel/100000000000996', 'REEL'),
    ('https://www.facebook.com/qa_profile_997/photos/100000000000997', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000000998', 'GROUP'),
    ('https://www.facebook.com/groups/100000000000999/posts/200000000000999', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001000', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001001', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001002', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001003&id=615000000001003', 'STORY'),
    ('https://www.facebook.com/share/QaToken001004/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001005/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001006/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001007/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1008/100000000001008', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001009', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1010', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1011', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1012/posts/100000000001012', 'POST'),
    ('https://www.facebook.com/qa_profile_1013/videos/100000000001013', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1014/reel/100000000001014', 'REEL'),
    ('https://www.facebook.com/qa_profile_1015/photos/100000000001015', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001016', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001017/posts/200000000001017', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001018', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001019', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001020', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001021&id=615000000001021', 'STORY'),
    ('https://www.facebook.com/share/QaToken001022/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001023/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001024/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001025/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1026/100000000001026', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001027', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1028', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1029', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1030/posts/100000000001030', 'POST'),
    ('https://www.facebook.com/qa_profile_1031/videos/100000000001031', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1032/reel/100000000001032', 'REEL'),
    ('https://www.facebook.com/qa_profile_1033/photos/100000000001033', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001034', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001035/posts/200000000001035', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001036', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001037', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001038', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001039&id=615000000001039', 'STORY'),
    ('https://www.facebook.com/share/QaToken001040/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001041/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001042/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001043/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1044/100000000001044', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001045', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1046', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1047', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1048/posts/100000000001048', 'POST'),
    ('https://www.facebook.com/qa_profile_1049/videos/100000000001049', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1050/reel/100000000001050', 'REEL'),
    ('https://www.facebook.com/qa_profile_1051/photos/100000000001051', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001052', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001053/posts/200000000001053', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001054', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001055', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001056', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001057&id=615000000001057', 'STORY'),
    ('https://www.facebook.com/share/QaToken001058/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001059/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001060/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001061/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1062/100000000001062', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001063', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1064', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1065', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1066/posts/100000000001066', 'POST'),
    ('https://www.facebook.com/qa_profile_1067/videos/100000000001067', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1068/reel/100000000001068', 'REEL'),
    ('https://www.facebook.com/qa_profile_1069/photos/100000000001069', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001070', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001071/posts/200000000001071', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001072', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001073', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001074', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001075&id=615000000001075', 'STORY'),
    ('https://www.facebook.com/share/QaToken001076/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001077/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001078/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001079/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1080/100000000001080', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001081', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1082', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1083', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1084/posts/100000000001084', 'POST'),
    ('https://www.facebook.com/qa_profile_1085/videos/100000000001085', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1086/reel/100000000001086', 'REEL'),
    ('https://www.facebook.com/qa_profile_1087/photos/100000000001087', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001088', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001089/posts/200000000001089', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001090', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001091', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001092', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001093&id=615000000001093', 'STORY'),
    ('https://www.facebook.com/share/QaToken001094/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001095/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001096/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001097/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1098/100000000001098', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001099', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1100', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1101', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1102/posts/100000000001102', 'POST'),
    ('https://www.facebook.com/qa_profile_1103/videos/100000000001103', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1104/reel/100000000001104', 'REEL'),
    ('https://www.facebook.com/qa_profile_1105/photos/100000000001105', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001106', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001107/posts/200000000001107', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001108', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001109', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001110', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001111&id=615000000001111', 'STORY'),
    ('https://www.facebook.com/share/QaToken001112/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001113/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001114/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001115/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1116/100000000001116', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001117', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1118', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1119', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1120/posts/100000000001120', 'POST'),
    ('https://www.facebook.com/qa_profile_1121/videos/100000000001121', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1122/reel/100000000001122', 'REEL'),
    ('https://www.facebook.com/qa_profile_1123/photos/100000000001123', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001124', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001125/posts/200000000001125', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001126', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001127', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001128', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001129&id=615000000001129', 'STORY'),
    ('https://www.facebook.com/share/QaToken001130/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001131/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001132/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001133/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1134/100000000001134', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001135', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1136', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1137', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1138/posts/100000000001138', 'POST'),
    ('https://www.facebook.com/qa_profile_1139/videos/100000000001139', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1140/reel/100000000001140', 'REEL'),
    ('https://www.facebook.com/qa_profile_1141/photos/100000000001141', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001142', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001143/posts/200000000001143', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001144', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001145', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001146', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001147&id=615000000001147', 'STORY'),
    ('https://www.facebook.com/share/QaToken001148/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001149/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001150/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001151/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1152/100000000001152', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001153', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1154', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1155', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1156/posts/100000000001156', 'POST'),
    ('https://www.facebook.com/qa_profile_1157/videos/100000000001157', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1158/reel/100000000001158', 'REEL'),
    ('https://www.facebook.com/qa_profile_1159/photos/100000000001159', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001160', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001161/posts/200000000001161', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001162', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001163', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001164', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001165&id=615000000001165', 'STORY'),
    ('https://www.facebook.com/share/QaToken001166/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001167/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001168/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001169/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1170/100000000001170', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001171', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1172', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1173', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1174/posts/100000000001174', 'POST'),
    ('https://www.facebook.com/qa_profile_1175/videos/100000000001175', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1176/reel/100000000001176', 'REEL'),
    ('https://www.facebook.com/qa_profile_1177/photos/100000000001177', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001178', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001179/posts/200000000001179', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001180', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001181', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001182', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001183&id=615000000001183', 'STORY'),
    ('https://www.facebook.com/share/QaToken001184/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001185/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001186/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001187/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1188/100000000001188', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001189', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1190', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1191', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1192/posts/100000000001192', 'POST'),
    ('https://www.facebook.com/qa_profile_1193/videos/100000000001193', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1194/reel/100000000001194', 'REEL'),
    ('https://www.facebook.com/qa_profile_1195/photos/100000000001195', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001196', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001197/posts/200000000001197', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001198', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001199', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001200', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001201&id=615000000001201', 'STORY'),
    ('https://www.facebook.com/share/QaToken001202/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001203/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001204/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001205/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1206/100000000001206', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001207', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1208', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1209', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1210/posts/100000000001210', 'POST'),
    ('https://www.facebook.com/qa_profile_1211/videos/100000000001211', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1212/reel/100000000001212', 'REEL'),
    ('https://www.facebook.com/qa_profile_1213/photos/100000000001213', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001214', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001215/posts/200000000001215', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001216', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001217', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001218', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001219&id=615000000001219', 'STORY'),
    ('https://www.facebook.com/share/QaToken001220/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001221/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001222/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001223/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1224/100000000001224', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001225', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1226', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1227', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1228/posts/100000000001228', 'POST'),
    ('https://www.facebook.com/qa_profile_1229/videos/100000000001229', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1230/reel/100000000001230', 'REEL'),
    ('https://www.facebook.com/qa_profile_1231/photos/100000000001231', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001232', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001233/posts/200000000001233', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001234', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001235', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001236', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001237&id=615000000001237', 'STORY'),
    ('https://www.facebook.com/share/QaToken001238/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001239/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001240/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001241/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1242/100000000001242', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001243', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1244', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1245', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1246/posts/100000000001246', 'POST'),
    ('https://www.facebook.com/qa_profile_1247/videos/100000000001247', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1248/reel/100000000001248', 'REEL'),
    ('https://www.facebook.com/qa_profile_1249/photos/100000000001249', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001250', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001251/posts/200000000001251', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001252', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001253', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001254', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001255&id=615000000001255', 'STORY'),
    ('https://www.facebook.com/share/QaToken001256/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001257/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001258/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001259/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1260/100000000001260', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001261', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1262', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1263', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1264/posts/100000000001264', 'POST'),
    ('https://www.facebook.com/qa_profile_1265/videos/100000000001265', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1266/reel/100000000001266', 'REEL'),
    ('https://www.facebook.com/qa_profile_1267/photos/100000000001267', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001268', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001269/posts/200000000001269', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001270', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001271', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001272', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001273&id=615000000001273', 'STORY'),
    ('https://www.facebook.com/share/QaToken001274/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001275/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001276/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001277/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1278/100000000001278', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001279', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1280', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1281', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1282/posts/100000000001282', 'POST'),
    ('https://www.facebook.com/qa_profile_1283/videos/100000000001283', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1284/reel/100000000001284', 'REEL'),
    ('https://www.facebook.com/qa_profile_1285/photos/100000000001285', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001286', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001287/posts/200000000001287', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001288', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001289', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001290', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001291&id=615000000001291', 'STORY'),
    ('https://www.facebook.com/share/QaToken001292/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001293/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001294/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001295/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1296/100000000001296', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001297', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1298', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1299', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1300/posts/100000000001300', 'POST'),
    ('https://www.facebook.com/qa_profile_1301/videos/100000000001301', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1302/reel/100000000001302', 'REEL'),
    ('https://www.facebook.com/qa_profile_1303/photos/100000000001303', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001304', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001305/posts/200000000001305', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001306', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001307', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001308', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001309&id=615000000001309', 'STORY'),
    ('https://www.facebook.com/share/QaToken001310/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001311/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001312/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001313/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1314/100000000001314', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001315', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1316', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1317', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1318/posts/100000000001318', 'POST'),
    ('https://www.facebook.com/qa_profile_1319/videos/100000000001319', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1320/reel/100000000001320', 'REEL'),
    ('https://www.facebook.com/qa_profile_1321/photos/100000000001321', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001322', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001323/posts/200000000001323', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001324', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001325', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001326', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001327&id=615000000001327', 'STORY'),
    ('https://www.facebook.com/share/QaToken001328/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001329/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001330/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001331/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1332/100000000001332', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001333', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1334', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1335', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1336/posts/100000000001336', 'POST'),
    ('https://www.facebook.com/qa_profile_1337/videos/100000000001337', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1338/reel/100000000001338', 'REEL'),
    ('https://www.facebook.com/qa_profile_1339/photos/100000000001339', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001340', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001341/posts/200000000001341', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001342', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001343', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001344', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001345&id=615000000001345', 'STORY'),
    ('https://www.facebook.com/share/QaToken001346/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001347/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001348/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001349/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1350/100000000001350', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001351', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1352', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1353', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1354/posts/100000000001354', 'POST'),
    ('https://www.facebook.com/qa_profile_1355/videos/100000000001355', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1356/reel/100000000001356', 'REEL'),
    ('https://www.facebook.com/qa_profile_1357/photos/100000000001357', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001358', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001359/posts/200000000001359', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001360', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001361', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001362', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001363&id=615000000001363', 'STORY'),
    ('https://www.facebook.com/share/QaToken001364/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001365/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001366/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001367/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1368/100000000001368', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001369', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1370', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1371', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1372/posts/100000000001372', 'POST'),
    ('https://www.facebook.com/qa_profile_1373/videos/100000000001373', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1374/reel/100000000001374', 'REEL'),
    ('https://www.facebook.com/qa_profile_1375/photos/100000000001375', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001376', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001377/posts/200000000001377', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001378', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001379', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001380', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001381&id=615000000001381', 'STORY'),
    ('https://www.facebook.com/share/QaToken001382/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001383/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001384/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001385/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1386/100000000001386', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001387', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1388', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1389', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1390/posts/100000000001390', 'POST'),
    ('https://www.facebook.com/qa_profile_1391/videos/100000000001391', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1392/reel/100000000001392', 'REEL'),
    ('https://www.facebook.com/qa_profile_1393/photos/100000000001393', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001394', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001395/posts/200000000001395', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001396', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001397', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001398', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001399&id=615000000001399', 'STORY'),
    ('https://www.facebook.com/share/QaToken001400/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001401/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001402/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001403/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1404/100000000001404', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001405', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1406', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1407', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1408/posts/100000000001408', 'POST'),
    ('https://www.facebook.com/qa_profile_1409/videos/100000000001409', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1410/reel/100000000001410', 'REEL'),
    ('https://www.facebook.com/qa_profile_1411/photos/100000000001411', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001412', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001413/posts/200000000001413', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001414', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001415', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001416', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001417&id=615000000001417', 'STORY'),
    ('https://www.facebook.com/share/QaToken001418/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001419/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001420/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001421/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1422/100000000001422', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001423', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1424', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1425', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1426/posts/100000000001426', 'POST'),
    ('https://www.facebook.com/qa_profile_1427/videos/100000000001427', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1428/reel/100000000001428', 'REEL'),
    ('https://www.facebook.com/qa_profile_1429/photos/100000000001429', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001430', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001431/posts/200000000001431', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001432', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001433', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001434', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001435&id=615000000001435', 'STORY'),
    ('https://www.facebook.com/share/QaToken001436/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001437/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001438/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001439/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1440/100000000001440', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001441', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1442', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1443', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1444/posts/100000000001444', 'POST'),
    ('https://www.facebook.com/qa_profile_1445/videos/100000000001445', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1446/reel/100000000001446', 'REEL'),
    ('https://www.facebook.com/qa_profile_1447/photos/100000000001447', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001448', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001449/posts/200000000001449', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001450', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001451', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001452', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001453&id=615000000001453', 'STORY'),
    ('https://www.facebook.com/share/QaToken001454/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001455/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001456/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001457/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1458/100000000001458', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001459', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1460', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1461', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1462/posts/100000000001462', 'POST'),
    ('https://www.facebook.com/qa_profile_1463/videos/100000000001463', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1464/reel/100000000001464', 'REEL'),
    ('https://www.facebook.com/qa_profile_1465/photos/100000000001465', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001466', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001467/posts/200000000001467', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001468', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001469', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001470', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001471&id=615000000001471', 'STORY'),
    ('https://www.facebook.com/share/QaToken001472/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001473/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001474/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001475/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1476/100000000001476', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001477', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1478', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1479', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1480/posts/100000000001480', 'POST'),
    ('https://www.facebook.com/qa_profile_1481/videos/100000000001481', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1482/reel/100000000001482', 'REEL'),
    ('https://www.facebook.com/qa_profile_1483/photos/100000000001483', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001484', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001485/posts/200000000001485', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001486', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001487', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001488', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001489&id=615000000001489', 'STORY'),
    ('https://www.facebook.com/share/QaToken001490/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001491/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001492/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001493/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1494/100000000001494', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001495', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1496', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1497', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1498/posts/100000000001498', 'POST'),
    ('https://www.facebook.com/qa_profile_1499/videos/100000000001499', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1500/reel/100000000001500', 'REEL'),
    ('https://www.facebook.com/qa_profile_1501/photos/100000000001501', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001502', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001503/posts/200000000001503', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001504', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001505', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001506', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001507&id=615000000001507', 'STORY'),
    ('https://www.facebook.com/share/QaToken001508/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001509/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001510/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001511/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1512/100000000001512', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001513', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1514', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1515', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1516/posts/100000000001516', 'POST'),
    ('https://www.facebook.com/qa_profile_1517/videos/100000000001517', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1518/reel/100000000001518', 'REEL'),
    ('https://www.facebook.com/qa_profile_1519/photos/100000000001519', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001520', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001521/posts/200000000001521', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001522', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001523', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001524', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001525&id=615000000001525', 'STORY'),
    ('https://www.facebook.com/share/QaToken001526/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001527/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001528/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001529/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1530/100000000001530', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001531', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1532', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1533', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1534/posts/100000000001534', 'POST'),
    ('https://www.facebook.com/qa_profile_1535/videos/100000000001535', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1536/reel/100000000001536', 'REEL'),
    ('https://www.facebook.com/qa_profile_1537/photos/100000000001537', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001538', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001539/posts/200000000001539', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001540', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001541', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001542', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001543&id=615000000001543', 'STORY'),
    ('https://www.facebook.com/share/QaToken001544/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001545/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001546/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001547/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1548/100000000001548', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001549', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1550', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1551', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1552/posts/100000000001552', 'POST'),
    ('https://www.facebook.com/qa_profile_1553/videos/100000000001553', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1554/reel/100000000001554', 'REEL'),
    ('https://www.facebook.com/qa_profile_1555/photos/100000000001555', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001556', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001557/posts/200000000001557', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001558', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001559', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001560', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001561&id=615000000001561', 'STORY'),
    ('https://www.facebook.com/share/QaToken001562/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001563/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001564/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001565/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1566/100000000001566', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001567', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1568', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1569', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1570/posts/100000000001570', 'POST'),
    ('https://www.facebook.com/qa_profile_1571/videos/100000000001571', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1572/reel/100000000001572', 'REEL'),
    ('https://www.facebook.com/qa_profile_1573/photos/100000000001573', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001574', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001575/posts/200000000001575', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001576', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001577', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001578', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001579&id=615000000001579', 'STORY'),
    ('https://www.facebook.com/share/QaToken001580/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001581/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001582/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001583/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1584/100000000001584', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001585', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1586', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1587', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1588/posts/100000000001588', 'POST'),
    ('https://www.facebook.com/qa_profile_1589/videos/100000000001589', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1590/reel/100000000001590', 'REEL'),
    ('https://www.facebook.com/qa_profile_1591/photos/100000000001591', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001592', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001593/posts/200000000001593', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001594', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001595', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001596', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001597&id=615000000001597', 'STORY'),
    ('https://www.facebook.com/share/QaToken001598/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001599/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001600/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001601/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1602/100000000001602', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001603', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1604', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1605', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1606/posts/100000000001606', 'POST'),
    ('https://www.facebook.com/qa_profile_1607/videos/100000000001607', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1608/reel/100000000001608', 'REEL'),
    ('https://www.facebook.com/qa_profile_1609/photos/100000000001609', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001610', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001611/posts/200000000001611', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001612', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001613', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001614', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001615&id=615000000001615', 'STORY'),
    ('https://www.facebook.com/share/QaToken001616/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001617/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001618/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001619/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1620/100000000001620', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001621', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1622', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1623', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1624/posts/100000000001624', 'POST'),
    ('https://www.facebook.com/qa_profile_1625/videos/100000000001625', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1626/reel/100000000001626', 'REEL'),
    ('https://www.facebook.com/qa_profile_1627/photos/100000000001627', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001628', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001629/posts/200000000001629', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001630', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001631', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001632', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001633&id=615000000001633', 'STORY'),
    ('https://www.facebook.com/share/QaToken001634/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001635/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001636/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001637/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1638/100000000001638', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001639', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1640', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1641', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1642/posts/100000000001642', 'POST'),
    ('https://www.facebook.com/qa_profile_1643/videos/100000000001643', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1644/reel/100000000001644', 'REEL'),
    ('https://www.facebook.com/qa_profile_1645/photos/100000000001645', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001646', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001647/posts/200000000001647', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001648', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001649', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001650', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001651&id=615000000001651', 'STORY'),
    ('https://www.facebook.com/share/QaToken001652/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001653/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001654/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001655/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1656/100000000001656', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001657', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1658', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1659', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1660/posts/100000000001660', 'POST'),
    ('https://www.facebook.com/qa_profile_1661/videos/100000000001661', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1662/reel/100000000001662', 'REEL'),
    ('https://www.facebook.com/qa_profile_1663/photos/100000000001663', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001664', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001665/posts/200000000001665', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001666', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001667', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001668', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001669&id=615000000001669', 'STORY'),
    ('https://www.facebook.com/share/QaToken001670/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001671/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001672/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001673/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1674/100000000001674', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001675', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1676', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1677', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1678/posts/100000000001678', 'POST'),
    ('https://www.facebook.com/qa_profile_1679/videos/100000000001679', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1680/reel/100000000001680', 'REEL'),
    ('https://www.facebook.com/qa_profile_1681/photos/100000000001681', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001682', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001683/posts/200000000001683', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001684', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001685', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001686', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001687&id=615000000001687', 'STORY'),
    ('https://www.facebook.com/share/QaToken001688/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001689/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001690/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001691/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1692/100000000001692', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001693', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1694', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1695', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1696/posts/100000000001696', 'POST'),
    ('https://www.facebook.com/qa_profile_1697/videos/100000000001697', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1698/reel/100000000001698', 'REEL'),
    ('https://www.facebook.com/qa_profile_1699/photos/100000000001699', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001700', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001701/posts/200000000001701', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001702', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001703', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001704', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001705&id=615000000001705', 'STORY'),
    ('https://www.facebook.com/share/QaToken001706/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001707/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001708/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001709/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1710/100000000001710', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001711', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1712', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1713', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1714/posts/100000000001714', 'POST'),
    ('https://www.facebook.com/qa_profile_1715/videos/100000000001715', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1716/reel/100000000001716', 'REEL'),
    ('https://www.facebook.com/qa_profile_1717/photos/100000000001717', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001718', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001719/posts/200000000001719', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001720', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001721', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001722', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001723&id=615000000001723', 'STORY'),
    ('https://www.facebook.com/share/QaToken001724/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001725/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001726/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001727/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1728/100000000001728', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001729', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1730', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1731', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1732/posts/100000000001732', 'POST'),
    ('https://www.facebook.com/qa_profile_1733/videos/100000000001733', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1734/reel/100000000001734', 'REEL'),
    ('https://www.facebook.com/qa_profile_1735/photos/100000000001735', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001736', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001737/posts/200000000001737', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001738', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001739', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001740', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001741&id=615000000001741', 'STORY'),
    ('https://www.facebook.com/share/QaToken001742/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001743/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001744/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001745/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1746/100000000001746', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001747', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1748', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1749', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1750/posts/100000000001750', 'POST'),
    ('https://www.facebook.com/qa_profile_1751/videos/100000000001751', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1752/reel/100000000001752', 'REEL'),
    ('https://www.facebook.com/qa_profile_1753/photos/100000000001753', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001754', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001755/posts/200000000001755', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001756', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001757', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001758', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001759&id=615000000001759', 'STORY'),
    ('https://www.facebook.com/share/QaToken001760/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001761/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001762/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001763/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1764/100000000001764', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001765', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1766', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1767', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1768/posts/100000000001768', 'POST'),
    ('https://www.facebook.com/qa_profile_1769/videos/100000000001769', 'VIDEO'),
    ('https://www.facebook.com/qa_profile_1770/reel/100000000001770', 'REEL'),
    ('https://www.facebook.com/qa_profile_1771/photos/100000000001771', 'PHOTO'),
    ('https://www.facebook.com/groups/100000000001772', 'GROUP'),
    ('https://www.facebook.com/groups/100000000001773/posts/200000000001773', 'GROUP_POST'),
    ('https://www.facebook.com/events/100000000001774', 'EVENT'),
    ('https://www.facebook.com/watch/?v=100000000001775', 'VIDEO'),
    ('https://www.facebook.com/photo.php?fbid=100000000001776', 'PHOTO'),
    ('https://www.facebook.com/story.php?story_fbid=100000000001777&id=615000000001777', 'STORY'),
    ('https://www.facebook.com/share/QaToken001778/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/p/QaToken001779/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/share/v/QaToken001780/', 'SHARE_WRAPPER'),
    ('https://www.facebook.com/reel/100000000001781/', 'REEL'),
    ('https://www.facebook.com/pages/qa_profile_1782/100000000001782', 'PAGE'),
    ('https://www.facebook.com/profile.php?id=100000000001783', 'PROFILE'),
    ('https://www.facebook.com/qa_profile_1784', 'PROFILE'),
    ('https://m.facebook.com/qa_profile_1785', 'PROFILE'),
]

FALLBACK_PAYLOAD_CASES = [
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000006"}', '100000000000006'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000012"}', '100000000000012'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000018"}', '100000000000018'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000024"}', '100000000000024'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000030"}', '100000000000030'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000036"}', '100000000000036'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000042"}', '100000000000042'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000048"}', '100000000000048'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000054"}', '100000000000054'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000060"}', '100000000000060'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000066"}', '100000000000066'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000072"}', '100000000000072'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000078"}', '100000000000078'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000084"}', '100000000000084'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000090"}', '100000000000090'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000096"}', '100000000000096'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000102"}', '100000000000102'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000108"}', '100000000000108'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000114"}', '100000000000114'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000120"}', '100000000000120'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000126"}', '100000000000126'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000132"}', '100000000000132'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000138"}', '100000000000138'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000144"}', '100000000000144'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000150"}', '100000000000150'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000156"}', '100000000000156'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000162"}', '100000000000162'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000168"}', '100000000000168'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000174"}', '100000000000174'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000180"}', '100000000000180'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000186"}', '100000000000186'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000192"}', '100000000000192'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000198"}', '100000000000198'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000204"}', '100000000000204'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000210"}', '100000000000210'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000216"}', '100000000000216'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000222"}', '100000000000222'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000228"}', '100000000000228'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000234"}', '100000000000234'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000240"}', '100000000000240'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000246"}', '100000000000246'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000252"}', '100000000000252'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000258"}', '100000000000258'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000264"}', '100000000000264'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000270"}', '100000000000270'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000276"}', '100000000000276'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000282"}', '100000000000282'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000288"}', '100000000000288'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000294"}', '100000000000294'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000300"}', '100000000000300'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000306"}', '100000000000306'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000312"}', '100000000000312'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000318"}', '100000000000318'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000324"}', '100000000000324'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000330"}', '100000000000330'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000336"}', '100000000000336'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000342"}', '100000000000342'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000348"}', '100000000000348'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000354"}', '100000000000354'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000360"}', '100000000000360'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000366"}', '100000000000366'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000372"}', '100000000000372'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000378"}', '100000000000378'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000384"}', '100000000000384'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000390"}', '100000000000390'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000396"}', '100000000000396'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000402"}', '100000000000402'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000408"}', '100000000000408'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000414"}', '100000000000414'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000420"}', '100000000000420'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000426"}', '100000000000426'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000432"}', '100000000000432'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000438"}', '100000000000438'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000444"}', '100000000000444'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000450"}', '100000000000450'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000456"}', '100000000000456'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000462"}', '100000000000462'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000468"}', '100000000000468'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000474"}', '100000000000474'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000480"}', '100000000000480'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000486"}', '100000000000486'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000492"}', '100000000000492'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
    ('{"success":true,"data":{"user_id":"100009876543210"}}', '100009876543210'),
    ('{"ok":true,"profile_id":"100001111111111"}', '100001111111111'),
    ('{"error":"not found"}', ''),
    ('{"success":false,"id":"100000000000498"}', '100000000000498'),
    ('{"success":true,"id":"100001234567890"}', '100001234567890'),
    ('{"status":200,"data":{"id":"615123456789"}}', '615123456789'),
]

def run_deterministic_self_test() -> Dict[str, int]:
    """Run parser/fallback regression fixtures without network access."""
    passed = 0
    failed = 0
    for raw_url, expected_kind in REGRESSION_URL_CASES:
        try:
            actual = URLParser.parse(raw_url).kind
            if actual == expected_kind:
                passed += 1
            else:
                failed += 1
                LOGGER.error("URL fixture mismatch: %s != %s", actual, expected_kind)
        except Exception:
            failed += 1
            LOGGER.exception("URL fixture crashed: %s", raw_url)
    for raw_json, expected_uid in FALLBACK_PAYLOAD_CASES:
        try:
            payload = json.loads(raw_json)
            actual = FallbackUIDResolver._first_numeric_id(payload)
            if actual == expected_uid:
                passed += 1
            else:
                failed += 1
                LOGGER.error("Fallback fixture mismatch: %s != %s", actual, expected_uid)
        except Exception:
            failed += 1
            LOGGER.exception("Fallback fixture crashed")
    return {"passed": passed, "failed": failed, "total": passed + failed}

