#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
============================================================
 FB UID / ENTITY RESOLVER V15 ULTRA - TELETHON BOT
============================================================

HTTP ONLY
PUBLIC CONTENT ONLY

NO:
- Playwright
- Selenium
- Cookie
- Facebook Access Token
- Facebook Login

TELEGRAM ENGINE:
- Telethon
- Async
- Concurrent requests
- Direct Facebook URL
- /start
- /help
- /getuidfb <facebook_url>

FEATURES:
- UID verification
- USER / PAGE / GROUP separation
- POST / REEL / VIDEO / PHOTO / STORY
- GROUP_POST / PAGE_POST / USER_POST
- share/p, share/v, share/r
- pfbid
- media_fbid
- actor_id
- profile_id
- entity_id
- canonical URL
- OG metadata
- JSON-LD
- HTML/JS ID correlation
- Base64 Facebook encoded ID
- Redirect URL unwrap
- Crawl public Facebook URLs

============================================================
REQUIREMENTS
============================================================

pip install telethon httpx

ENV:

export API_ID=12345678
export API_HASH="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export BOT_TOKEN="123456:xxxxxxxxxxxxxxxx"

RUN:

python uid.py

============================================================
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import getpass
import html
import json
import os
import re
import time

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from urllib.parse import (
    parse_qs,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlparse,
)

import httpx

from telethon import events
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
# VERSION
# ============================================================

VERSION = "V15 ULTRA"


# ============================================================
# CONFIG
# ============================================================

DEFAULT_TIMEOUT = 18.0
DEFAULT_MAX_PAGES = 8
DEFAULT_CONCURRENCY = 3

# Số request resolver chạy đồng thời toàn bot.
# Tăng lên 5-10 nếu server/network chịu được.
GLOBAL_CONCURRENCY = 6

MAX_BODY_BYTES = 12 * 1024 * 1024
MAX_TELEGRAM_MESSAGE = 3900


SESSION_KEY = "_dragon_sessions"
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
# FACEBOOK HOSTS
# ============================================================

FB_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
    "mobile.facebook.com",
}

FB_REDIRECT_HOSTS = {
    "l.facebook.com",
    "lm.facebook.com",
}

FB_ALL_HOSTS = FB_HOSTS | FB_REDIRECT_HOSTS


# ============================================================
# TRACKING PARAMETERS
# ============================================================

TRACKING_PARAMS = {
    "fbclid",
    "__tn__",
    "__cft__",
    "hc_ref",
    "refid",
    "notif_id",
    "notif_t",
    "notif_type",
    "locale",
    "paipv",
}


# ============================================================
# RESERVED PATHS
# ============================================================

RESERVED_PATHS = {
    "home",
    "watch",
    "reel",
    "reels",
    "video",
    "videos",
    "photo",
    "photos",
    "posts",
    "post",
    "story",
    "stories",
    "share",
    "sharer",
    "permalink",
    "groups",
    "group",
    "pages",
    "page",
    "events",
    "marketplace",
    "gaming",
    "messages",
    "notifications",
    "settings",
    "login",
    "logout",
    "recover",
    "privacy",
    "help",
    "policies",
    "business",
    "ads",
}


# ============================================================
# REGEX
# ============================================================

NUMERIC_ID_RE = re.compile(
    r"^\d{5,30}$"
)

PF_BID_RE = re.compile(
    r"^pfbid[A-Za-z0-9_-]+$",
    re.IGNORECASE,
)

URL_RE = re.compile(
    r"https?://[^\s<>'\"]+",
    re.IGNORECASE,
)

FB_URL_RE = re.compile(
    r"https?://(?:"
    r"(?:www\.|m\.|mbasic\.|web\.|touch\.|mobile\.)?facebook\.com"
    r"|(?:l|lm)\.facebook\.com"
    r"|fb\.watch"
    r")/[^\s<>'\"]+",
    re.IGNORECASE,
)


# ============================================================
# UTILITIES
# ============================================================

def clean_text(value: Any) -> str:
    if value is None:
        return ""

    if not isinstance(value, str):
        value = str(value)

    value = html.unescape(value)
    value = value.replace("\x00", "")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def unique_list(items: list[str]) -> list[str]:
    result = []
    seen = set()

    for item in items:
        item = clean_text(item)

        if not item:
            continue

        key = item.lower()

        if key in seen:
            continue

        seen.add(key)
        result.append(item)

    return result


def truncate(
    value: str,
    length: int = 500,
) -> str:

    value = clean_text(value)

    if len(value) <= length:
        return value

    return value[:length - 3] + "..."


def is_numeric_id(value: str) -> bool:
    return bool(
        NUMERIC_ID_RE.fullmatch(
            clean_text(value)
        )
    )


def looks_like_pfbid(value: str) -> bool:
    return bool(
        PF_BID_RE.fullmatch(
            clean_text(value)
        )
    )


def normalize_host(host: str) -> str:
    host = (host or "").lower().strip()

    if host.endswith("."):
        host = host[:-1]

    return host


def same_fb_host(url: str) -> bool:
    try:
        host = normalize_host(
            urlparse(url).hostname or ""
        )

        return host in FB_HOSTS

    except Exception:
        return False


def is_fb_host_or_redirect(url: str) -> bool:
    try:
        host = normalize_host(
            urlparse(url).hostname or ""
        )

        return host in FB_ALL_HOSTS

    except Exception:
        return False


def is_http_url(url: str) -> bool:
    try:
        return urlparse(url).scheme.lower() in {
            "http",
            "https",
        }

    except Exception:
        return False


def normalize_url(url: str) -> str:
    url = clean_text(url)

    url = url.strip(
        " \t\r\n<>\"'"
    )

    if not url:
        return ""

    if not re.match(
        r"^https?://",
        url,
        re.IGNORECASE,
    ):
        url = "https://" + url

    parsed = urlparse(url)

    scheme = parsed.scheme.lower()

    host = normalize_host(
        parsed.hostname or ""
    )

    if not host:
        return url

    path = parsed.path or "/"

    if path != "/":
        path = re.sub(
            r"/{2,}",
            "/",
            path,
        )

    query = parse_qs(
        parsed.query,
        keep_blank_values=True,
    )

    clean_query = {}

    for key, values in query.items():

        if key.lower() in TRACKING_PARAMS:
            continue

        clean_query[key] = values

    query_string = urlencode(
        clean_query,
        doseq=True,
    )

    return (
        f"{scheme}://{host}{path}"
        + (
            f"?{query_string}"
            if query_string
            else ""
        )
    )


def strip_url_punctuation(url: str) -> str:
    return url.rstrip(
        ".,;:!?)]}>\"'"
    )


def extract_url(text: str) -> Optional[str]:

    if not text:
        return None

    match = FB_URL_RE.search(text)

    if match:
        return strip_url_punctuation(
            match.group(0)
        )

    match = URL_RE.search(text)

    if match:

        candidate = strip_url_punctuation(
            match.group(0)
        )

        try:
            host = normalize_host(
                urlparse(candidate).hostname
                or ""
            )

            if host in FB_ALL_HOSTS:
                return candidate

        except Exception:
            pass

    # facebook.com/username
    # www.facebook.com/username

    match = re.search(
        r"(?<!\w)"
        r"(?:www\.)?facebook\.com/[^\s<>'\"]+",
        text,
        re.IGNORECASE,
    )

    if match:
        return (
            "https://"
            + strip_url_punctuation(
                match.group(0)
            )
        )

    return None


def shorten_id(
    value: str,
    length: int = 60,
) -> str:

    value = clean_text(value)

    if len(value) <= length:
        return value

    return value[:length - 3] + "..."


# ============================================================
# BASE64 FACEBOOK ID
# ============================================================

def base64_candidates(
    token: str,
) -> list[str]:

    token = clean_text(token)

    if not token:
        return []

    variants = [
        token,
        token.replace(
            "-",
            "+",
        ).replace(
            "_",
            "/",
        ),
        token.replace(
            "-",
            "+",
        ).replace(
            "*",
            "/",
        ),
    ]

    result = []

    for item in variants:

        item = item.strip()

        if not item:
            continue

        padding = len(item) % 4

        if padding:
            item += "=" * (
                4 - padding
            )

        if item not in result:
            result.append(item)

    return result


def decode_fb_encoded_id(
    token: str,
) -> dict[str, str]:

    token = clean_text(token)

    if not token:
        return {}

    result = {}

    for candidate in base64_candidates(
        token
    ):

        try:
            raw = base64.b64decode(
                candidate,
                validate=False,
            )

        except (
            ValueError,
            binascii.Error,
        ):
            continue

        if not raw:
            continue

        text = raw.decode(
            "utf-8",
            errors="ignore",
        )

        text = clean_text(text)

        match = re.search(
            r"S:_I(\d{5,30}):(\d{5,30})",
            text,
        )

        if match:

            result["actor_id"] = (
                match.group(1)
            )

            result["object_id"] = (
                match.group(2)
            )

            result["decoded_text"] = text

            return result

        matches = re.findall(
            r"(?<!\d)(\d{5,30})(?!\d)",
            text,
        )

        if matches:

            result["decoded_id"] = (
                matches[-1]
            )

            result["decoded_text"] = text

            return result

    return result


# ============================================================
# EVIDENCE
# ============================================================

@dataclass
class Evidence:

    field: str
    value: str
    source: str
    score: int = 0
    context: str = ""

    def key(self) -> tuple:
        return (
            self.field,
            self.value,
            self.source,
        )


# ============================================================
# RESULT
# ============================================================

@dataclass
class Result:

    input_url: str = ""
    resolved_url: str = ""
    url_type: str = "UNKNOWN"
    status: str = "UNKNOWN"
    success: bool = False

    user_uid: Optional[str] = None
    publisher_id: Optional[str] = None
    publisher_username: Optional[str] = None

    page_id: Optional[str] = None
    group_id: Optional[str] = None

    post_id: Optional[str] = None
    video_id: Optional[str] = None
    reel_id: Optional[str] = None
    photo_id: Optional[str] = None
    story_id: Optional[str] = None

    media_fbid: Optional[str] = None
    actor_id: Optional[str] = None
    decoded_id: Optional[str] = None
    entity_id: Optional[str] = None

    entity_type: str = "UNKNOWN"
    publisher_type: str = "UNKNOWN"

    user_uid_verified: bool = False

    title: str = ""

    confidence: int = 0

    warnings: list[str] = field(
        default_factory=list
    )

    evidence: list[Evidence] = field(
        default_factory=list
    )

    crawl_chain: list[str] = field(
        default_factory=list
    )


# ============================================================
# URL PARSER
# ============================================================

class URLParser:

    def __init__(self):
        self.evidence = []

    def add(
        self,
        field: str,
        value: str,
        source: str,
        score: int = 0,
        context: str = "",
    ):

        value = clean_text(value)

        if not value:
            return

        self.evidence.append(
            Evidence(
                field=field,
                value=value,
                source=source,
                score=score,
                context=context,
            )
        )

    def parse(
        self,
        url: str,
    ):

        try:
            parsed = urlparse(url)

        except Exception:
            return

        path = unquote(
            parsed.path or ""
        )

        path_lower = path.lower()

        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
        )

        url_type = "PROFILE"

        if "/posts/" in path_lower:
            url_type = "POST"

        elif "/reels/" in path_lower:
            url_type = "REEL"

        elif "/reel/" in path_lower:
            url_type = "REEL"

        elif "/videos/" in path_lower:
            url_type = "VIDEO"

        elif "/photos/" in path_lower:
            url_type = "PHOTO"

        elif "/stories/" in path_lower:
            url_type = "STORY"

        elif path_lower.startswith("/p/"):
            url_type = "POST"

        elif "/share/p/" in path_lower:
            url_type = "POST"

        elif "/share/v/" in path_lower:
            url_type = "VIDEO"

        elif "/share/r/" in path_lower:
            url_type = "REEL"

        elif path_lower.startswith("/share/"):
            url_type = "SHARE"

        elif "/groups/" in path_lower:
            url_type = "GROUP"

        elif "/watch/" in path_lower:
            url_type = "VIDEO"

        self.add(
            "url_type_hint",
            url_type,
            "url",
            80,
        )

        # ====================================================
        # USERNAME / POSTS / VIDEOS / REELS / PHOTOS
        # ====================================================

        match = re.match(
            r"^/([^/?#]+)/"
            r"(posts|videos|reels|photos|reel)"
            r"/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if match:

            username = match.group(1)
            content_type = match.group(2).lower()
            object_id = match.group(3)

            if (
                username.lower()
                not in RESERVED_PATHS
            ):
                self.add(
                    "publisher_username",
                    username,
                    "url",
                    95,
                )

            if content_type == "posts":

                self.add(
                    "post_id",
                    object_id,
                    "url",
                    100,
                )

            elif content_type == "videos":

                self.add(
                    "video_id",
                    object_id,
                    "url",
                    100,
                )

            elif content_type in {
                "reels",
                "reel",
            }:

                self.add(
                    "reel_id",
                    object_id,
                    "url",
                    100,
                )

            elif content_type == "photos":

                self.add(
                    "photo_id",
                    object_id,
                    "url",
                    100,
                )

        # ====================================================
        # /p/ID
        # ====================================================

        match = re.match(
            r"^/p/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if match:

            self.add(
                "post_id",
                match.group(1),
                "url",
                100,
            )

        # ====================================================
        # SHARE
        # ====================================================

        match = re.match(
            r"^/share/"
            r"(p|v|r)"
            r"/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if match:

            share_type = match.group(1).lower()
            token = match.group(2)

            if share_type == "p":

                self.add(
                    "post_id",
                    token,
                    "share_url",
                    95,
                )

            elif share_type == "v":

                self.add(
                    "video_id",
                    token,
                    "share_url",
                    95,
                )

            elif share_type == "r":

                self.add(
                    "reel_id",
                    token,
                    "share_url",
                    95,
                )

        # ====================================================
        # GENERIC SHARE
        # ====================================================

        match = re.match(
            r"^/share/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if match:

            token = match.group(1)

            if token.lower() not in {
                "p",
                "v",
                "r",
            }:

                self.add(
                    "share_token",
                    token,
                    "share_url",
                    70,
                )

        # ====================================================
        # STORIES
        # ====================================================

        match = re.match(
            r"^/stories/"
            r"([^/?#]+)/"
            r"([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if match:

            actor = match.group(1)
            story = match.group(2)

            if is_numeric_id(actor):

                self.add(
                    "actor_id",
                    actor,
                    "story_url",
                    95,
                )

            else:

                self.add(
                    "publisher_username",
                    actor,
                    "story_url",
                    90,
                )

            self.add(
                "story_id",
                story,
                "story_url",
                100,
            )

        # ====================================================
        # GROUP
        # ====================================================

        match = re.match(
            r"^/groups/"
            r"([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if match:

            group_token = match.group(1)

            if is_numeric_id(group_token):

                self.add(
                    "group_id",
                    group_token,
                    "group_url",
                    100,
                )

            else:

                self.add(
                    "group_username",
                    group_token,
                    "group_url",
                    90,
                )

        # ====================================================
        # QUERY IDs
        # ====================================================

        for key in (
            "id",
            "profile_id",
            "user_id",
            "actor_id",
            "entity_id",
            "page_id",
            "group_id",
            "media_fbid",
            "post_id",
            "video_id",
            "reel_id",
            "photo_id",
            "story_id",
        ):

            for value in query.get(
                key,
                [],
            ):

                if not value:
                    continue

                self.add(
                    key,
                    value,
                    f"query:{key}",
                    90,
                )

        # ====================================================
        # USERNAME QUERY
        # ====================================================

        for key in (
            "username",
            "profile",
            "profile_name",
        ):

            for value in query.get(
                key,
                [],
            ):

                if value:

                    self.add(
                        "publisher_username",
                        value,
                        f"query:{key}",
                        80,
                    )

        # ====================================================
        # MEDIA FBID
        # ====================================================

        for value in query.get(
            "media_fbid",
            [],
        ):

            if not value:
                continue

            self.add(
                "media_fbid",
                value,
                "query:media_fbid",
                100,
            )

            decoded = decode_fb_encoded_id(
                value
            )

            if decoded.get(
                "actor_id"
            ):

                self.add(
                    "actor_id",
                    decoded["actor_id"],
                    "media_fbid_decode",
                    90,
                )

            if decoded.get(
                "object_id"
            ):

                self.add(
                    "entity_id",
                    decoded["object_id"],
                    "media_fbid_decode",
                    85,
                )

            if decoded.get(
                "decoded_id"
            ):

                self.add(
                    "decoded_id",
                    decoded["decoded_id"],
                    "media_fbid_decode",
                    80,
                )

        # ====================================================
        # FBID
        # ====================================================

        for value in query.get(
            "fbid",
            [],
        ):

            if value:

                self.add(
                    "entity_id",
                    value,
                    "query:fbid",
                    90,
                )


# ============================================================
# HTML ATTRIBUTES
# ============================================================

ATTR_RE = re.compile(
    r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)


def parse_html_attrs(
    tag: str,
) -> dict[str, str]:

    attrs = {}

    for match in ATTR_RE.finditer(tag):

        key = match.group(1).lower()

        value = (
            match.group(2)
            if match.group(2) is not None
            else match.group(3)
        )

        attrs[key] = html.unescape(
            value or ""
        )

    return attrs


# ============================================================
# HTML REGEX
# ============================================================

META_RE = re.compile(
    r"<meta\b[^>]*>",
    re.IGNORECASE,
)

LINK_RE = re.compile(
    r"<link\b[^>]*>",
    re.IGNORECASE,
)

TITLE_RE = re.compile(
    r"<title\b[^>]*>"
    r"(.*?)"
    r"</title\s*>",
    re.IGNORECASE | re.DOTALL,
)

SCRIPT_RE = re.compile(
    r"<script\b([^>]*)>"
    r"(.*?)"
    r"</script\s*>",
    re.IGNORECASE | re.DOTALL,
)


# ============================================================
# METADATA PARSER
# ============================================================

class MetadataParser:

    def __init__(self):

        self.evidence = []
        self.title = ""
        self.canonical = ""
        self.og = {}
        self.app = {}
        self.jsonld = []

    def add(
        self,
        field,
        value,
        source,
        score=0,
        context="",
    ):

        value = clean_text(value)

        if not value:
            return

        self.evidence.append(
            Evidence(
                field=field,
                value=value,
                source=source,
                score=score,
                context=context,
            )
        )

    def parse(
        self,
        html_text: str,
    ):

        if not html_text:
            return

        # ====================================================
        # TITLE
        # ====================================================

        title_match = TITLE_RE.search(
            html_text
        )

        if title_match:

            self.title = clean_text(
                title_match.group(1)
            )

            if self.title:

                self.add(
                    "title",
                    self.title,
                    "html:title",
                    70,
                )

        # ====================================================
        # META
        # ====================================================

        for tag_match in META_RE.finditer(
            html_text
        ):

            tag = tag_match.group(0)

            attrs = parse_html_attrs(tag)

            key = (
                attrs.get("property")
                or attrs.get("name")
                or attrs.get("itemprop")
                or ""
            ).lower()

            value = attrs.get(
                "content",
                "",
            )

            if not key or not value:
                continue

            if key.startswith("og:"):

                self.og[key] = value

                self.add(
                    key,
                    value,
                    "meta",
                    70,
                )

            elif key.startswith("al:"):

                self.app[key] = value

                self.add(
                    key,
                    value,
                    "meta",
                    70,
                )

            elif key in {
                "profile:username",
                "profile:first_name",
                "profile:last_name",
                "fb:profile_id",
                "fb:app_id",
                "article:author",
            }:

                self.add(
                    key,
                    value,
                    "meta",
                    75,
                )

        # ====================================================
        # CANONICAL
        # ====================================================

        for tag_match in LINK_RE.finditer(
            html_text
        ):

            tag = tag_match.group(0)

            attrs = parse_html_attrs(tag)

            rel = attrs.get(
                "rel",
                "",
            ).lower()

            href = attrs.get(
                "href",
                "",
            )

            if (
                "canonical" in rel
                and href
            ):

                self.canonical = href

                self.add(
                    "canonical_url",
                    href,
                    "link:canonical",
                    95,
                )

        # ====================================================
        # JSON-LD
        # ====================================================

        for match in SCRIPT_RE.finditer(
            html_text
        ):

            attrs_raw = match.group(1) or ""
            body = match.group(2) or ""

            attrs = parse_html_attrs(
                "<script "
                + attrs_raw
                + ">"
            )

            script_type = attrs.get(
                "type",
                "",
            ).lower()

            if "ld+json" not in script_type:
                continue

            body = body.strip()

            if not body:
                continue

            try:

                data = json.loads(body)

                self.jsonld.append(data)

            except Exception:
                continue


# ============================================================
# HTML ID ENGINE
# ============================================================

class HTMLIDEngine:

    PATTERNS = {

        "profile_id": [
            re.compile(
                r'"profile_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
            re.compile(
                r'"profileID"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "user_id": [
            re.compile(
                r'"user_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
            re.compile(
                r'"userID"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "actor_id": [
            re.compile(
                r'"actor_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
            re.compile(
                r'"actorID"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "page_id": [
            re.compile(
                r'"page_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "group_id": [
            re.compile(
                r'"group_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "entity_id": [
            re.compile(
                r'"entity_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
            re.compile(
                r'"entityID"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "post_id": [
            re.compile(
                r'"post_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
            re.compile(
                r'"postID"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "video_id": [
            re.compile(
                r'"video_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
            re.compile(
                r'"videoID"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "reel_id": [
            re.compile(
                r'"reel_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
            re.compile(
                r'"reelID"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "photo_id": [
            re.compile(
                r'"photo_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "story_id": [
            re.compile(
                r'"story_id"\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            ),
        ],

        "media_fbid": [
            re.compile(
                r'"media_fbid"\s*:\s*"?(?P<id>\d{5,50})"?',
                re.IGNORECASE,
            ),
        ],
    }

    URL_PATTERNS = [

        re.compile(
            r"facebook\.com/"
            r"(?P<username>[A-Za-z0-9._-]{1,100})"
            r"/posts/"
            r"(?P<id>\d{5,30})",
            re.IGNORECASE,
        ),

        re.compile(
            r"facebook\.com/"
            r"(?P<username>[A-Za-z0-9._-]{1,100})"
            r"/videos/"
            r"(?P<id>\d{5,30})",
            re.IGNORECASE,
        ),

        re.compile(
            r"facebook\.com/"
            r"(?P<username>[A-Za-z0-9._-]{1,100})"
            r"/reels/"
            r"(?P<id>\d{5,30})",
            re.IGNORECASE,
        ),

        re.compile(
            r"facebook\.com/"
            r"(?P<username>[A-Za-z0-9._-]{1,100})"
            r"/photos/"
            r"(?P<id>\d{5,30})",
            re.IGNORECASE,
        ),

        re.compile(
            r"/share/[pvr]/"
            r"(?P<token>pfbid[A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
    ]

    def parse(
        self,
        text: str,
    ) -> list[Evidence]:

        evidence = []

        if not text:
            return evidence

        # ====================================================
        # KEY VALUE
        # ====================================================

        for field_name, patterns in self.PATTERNS.items():

            for pattern in patterns:

                for match in pattern.finditer(
                    text
                ):

                    value = (
                        match.groupdict()
                        .get("id")
                    )

                    if value:

                        evidence.append(
                            Evidence(
                                field=field_name,
                                value=value,
                                source="html_js",
                                score=85,
                                context=truncate(
                                    match.group(0),
                                    180,
                                ),
                            )
                        )

        # ====================================================
        # URL REFERENCES
        # ====================================================

        for pattern in self.URL_PATTERNS:

            for match in pattern.finditer(
                text
            ):

                data = match.groupdict()

                username = data.get(
                    "username"
                )

                object_id = data.get(
                    "id"
                )

                token = data.get(
                    "token"
                )

                if username:

                    evidence.append(
                        Evidence(
                            field="publisher_username",
                            value=username,
                            source="html_url",
                            score=80,
                            context=truncate(
                                match.group(0),
                                180,
                            ),
                        )
                    )

                if object_id:

                    evidence.append(
                        Evidence(
                            field="entity_id",
                            value=object_id,
                            source="html_url",
                            score=75,
                            context=truncate(
                                match.group(0),
                                180,
                            ),
                        )
                    )

                if token:

                    evidence.append(
                        Evidence(
                            field="entity_id",
                            value=token,
                            source="html_url",
                            score=75,
                            context=truncate(
                                match.group(0),
                                180,
                            ),
                        )
                    )

        return evidence


# ============================================================
# IDENTITY ENGINE
# ============================================================

class IdentityEngine:

    ID_KEYS = (
        "profile_id",
        "user_id",
        "actor_id",
        "page_id",
        "group_id",
        "entity_id",
    )

    USERNAME_PATTERNS = [

        re.compile(
            r'"username"\s*:\s*"([^"]{1,100})"',
            re.IGNORECASE,
        ),

        re.compile(
            r'"userName"\s*:\s*"([^"]{1,100})"',
            re.IGNORECASE,
        ),

        re.compile(
            r'"vanity"\s*:\s*"([^"]{1,100})"',
            re.IGNORECASE,
        ),
    ]

    def parse(
        self,
        text: str,
    ) -> list[Evidence]:

        evidence = []

        if not text:
            return evidence

        for pattern in self.USERNAME_PATTERNS:

            for match in pattern.finditer(
                text
            ):

                username = clean_text(
                    match.group(1)
                )

                if (
                    username
                    and username.lower()
                    not in RESERVED_PATHS
                ):

                    evidence.append(
                        Evidence(
                            field="publisher_username",
                            value=username,
                            source="identity",
                            score=65,
                            context=truncate(
                                match.group(0),
                                180,
                            ),
                        )
                    )

        for key in self.ID_KEYS:

            pattern = re.compile(
                rf'"{re.escape(key)}"'
                r'\s*:\s*"?(?P<id>\d{5,30})"?',
                re.IGNORECASE,
            )

            for match in pattern.finditer(
                text
            ):

                evidence.append(
                    Evidence(
                        field=key,
                        value=match.group("id"),
                        source="identity",
                        score=88,
                        context=truncate(
                            match.group(0),
                            180,
                        ),
                    )
                )

        return evidence


# ============================================================
# CORRELATION ENGINE
# ============================================================

class CorrelationEngine:

    def parse(
        self,
        text: str,
    ) -> list[Evidence]:

        evidence = []

        if not text:
            return evidence

        patterns = [

            (
                "publisher_id",
                re.compile(
                    r'"(?:owner|author|publisher|actor)"'
                    r'\s*:\s*\{'
                    r'[^{}]{0,1000}?'
                    r'"(?:id|pk)"'
                    r'\s*:\s*"?(?P<id>\d{5,30})"?',
                    re.IGNORECASE,
                ),
            ),

            (
                "actor_id",
                re.compile(
                    r'"actor"'
                    r'\s*:\s*\{'
                    r'[^{}]{0,1000}?'
                    r'"id"'
                    r'\s*:\s*"?(?P<id>\d{5,30})"?',
                    re.IGNORECASE,
                ),
            ),

            (
                "entity_id",
                re.compile(
                    r'"entity"'
                    r'\s*:\s*\{'
                    r'[^{}]{0,1000}?'
                    r'"id"'
                    r'\s*:\s*"?(?P<id>\d{5,30})"?',
                    re.IGNORECASE,
                ),
            ),
        ]

        for field_name, pattern in patterns:

            for match in pattern.finditer(
                text
            ):

                value = (
                    match.groupdict()
                    .get("id")
                )

                if value:

                    evidence.append(
                        Evidence(
                            field=field_name,
                            value=value,
                            source="correlation",
                            score=80,
                            context=truncate(
                                match.group(0),
                                300,
                            ),
                        )
                    )

        for match in re.finditer(
            r'"id"\s*:\s*"(\d{5,30})"',
            text,
            re.IGNORECASE,
        ):

            evidence.append(
                Evidence(
                    field="generic_id",
                    value=match.group(1),
                    source="correlation",
                    score=35,
                    context=truncate(
                        match.group(0),
                        100,
                    ),
                )
            )

        return evidence


# ============================================================
# DEEP LINK ENGINE
# ============================================================

class DeepLinkEngine:

    def parse(
        self,
        text: str,
    ) -> list[Evidence]:

        evidence = []

        if not text:
            return evidence

        patterns = [

            (
                "user_uid",
                re.compile(
                    r"fb://profile/"
                    r"(?P<id>\d{5,30})",
                    re.IGNORECASE,
                ),
            ),

            (
                "page_id",
                re.compile(
                    r"fb://page/"
                    r"(?P<id>\d{5,30})",
                    re.IGNORECASE,
                ),
            ),

            (
                "group_id",
                re.compile(
                    r"fb://group/"
                    r"(?P<id>\d{5,30})",
                    re.IGNORECASE,
                ),
            ),

            (
                "entity_id",
                re.compile(
                    r"fb://(?:post|object)/"
                    r"(?P<id>\d{5,30})",
                    re.IGNORECASE,
                ),
            ),
        ]

        for field_name, pattern in patterns:

            for match in pattern.finditer(
                text
            ):

                value = match.group("id")

                evidence.append(
                    Evidence(
                        field=field_name,
                        value=value,
                        source="deep_link",
                        score=90,
                        context=match.group(0),
                    )
                )

        return evidence


# ============================================================
# JSON LD ENGINE
# ============================================================

class JSONLDEngine:

    def walk(
        self,
        obj: Any,
        path: str = "",
    ) -> list[Evidence]:

        evidence = []

        if isinstance(obj, dict):

            for key, value in obj.items():

                current = (
                    f"{path}.{key}"
                    if path
                    else str(key)
                )

                key_lower = str(key).lower()

                if isinstance(
                    value,
                    (
                        str,
                        int,
                        float,
                    ),
                ):

                    value_string = clean_text(
                        value
                    )

                    if (
                        key_lower in {
                            "identifier",
                            "accountid",
                            "profileid",
                            "userid",
                            "actorid",
                            "pageid",
                            "groupid",
                        }
                        and is_numeric_id(
                            value_string
                        )
                    ):

                        evidence.append(
                            Evidence(
                                field="jsonld_id",
                                value=value_string,
                                source="jsonld",
                                score=70,
                                context=current,
                            )
                        )

                    if key_lower in {
                        "name",
                        "headline",
                    }:

                        if value_string:

                            evidence.append(
                                Evidence(
                                    field="title",
                                    value=value_string,
                                    source="jsonld",
                                    score=50,
                                    context=current,
                                )
                            )

                evidence.extend(
                    self.walk(
                        value,
                        current,
                    )
                )

        elif isinstance(obj, list):

            for index, item in enumerate(
                obj
            ):

                evidence.extend(
                    self.walk(
                        item,
                        f"{path}[{index}]",
                    )
                )

        return evidence


# ============================================================
# SNAPSHOT
# ============================================================

@dataclass
class Snapshot:

    requested_url: str
    final_url: str
    status_code: int
    html: str
    headers: dict[str, str]

    history: list[str] = field(
        default_factory=list
    )


# ============================================================
# HTTP CLIENT
# ============================================================

class HTTPClient:

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
    ):

        self.timeout = timeout
        self.client = None

        self.headers = {

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
                "en-US;q=0.8,"
                "en;q=0.7"
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

    async def __aenter__(self):

        self.client = httpx.AsyncClient(

            timeout=httpx.Timeout(
                self.timeout,
                connect=10.0,
            ),

            follow_redirects=True,

            headers=self.headers,

            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )

        return self

    async def __aexit__(
        self,
        exc_type,
        exc,
        tb,
    ):

        if self.client:
            await self.client.aclose()

        self.client = None

    async def get(
        self,
        url: str,
    ) -> Optional[Snapshot]:

        if not self.client:

            raise RuntimeError(
                "HTTPClient chưa được mở"
            )

        retries = 3

        for attempt in range(
            retries
        ):

            try:

                response = await self.client.get(
                    url
                )

                raw = response.content

                if len(raw) > MAX_BODY_BYTES:
                    raw = raw[
                        :MAX_BODY_BYTES
                    ]

                encoding = (
                    response.encoding
                    or "utf-8"
                )

                try:

                    text = raw.decode(
                        encoding,
                        errors="replace",
                    )

                except Exception:

                    text = raw.decode(
                        "utf-8",
                        errors="replace",
                    )

                history = [
                    str(item.url)
                    for item in response.history
                ]

                return Snapshot(
                    requested_url=url,
                    final_url=str(
                        response.url
                    ),
                    status_code=response.status_code,
                    html=text,
                    headers=dict(
                        response.headers
                    ),
                    history=history,
                )

            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
            ):

                if attempt >= retries - 1:
                    return None

                await asyncio.sleep(
                    0.5 * (
                        attempt + 1
                    )
                )

            except Exception:

                return None

        return None


# ============================================================
# FACEBOOK RESOLVER
# ============================================================

class FacebookResolver:

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_pages: int = DEFAULT_MAX_PAGES,
        concurrency: int = DEFAULT_CONCURRENCY,
    ):

        self.timeout = timeout
        self.max_pages = max_pages
        self.concurrency = concurrency

        self.visited = set()

        self.cache = {}

        self.all_evidence = []

        self.snapshots = []

        self.semaphore = asyncio.Semaphore(
            concurrency
        )

    # ========================================================
    # EVIDENCE
    # ========================================================

    def add_evidence(
        self,
        evidence,
    ):

        self.all_evidence.extend(
            evidence
        )

    # ========================================================
    # UNWRAP
    # ========================================================

    def unwrap_url(
        self,
        url: str,
    ) -> str:

        current = normalize_url(url)

        for _ in range(3):

            if not current:
                break

            try:

                parsed = urlparse(
                    current
                )

                host = normalize_host(
                    parsed.hostname or ""
                )

                if host not in FB_REDIRECT_HOSTS:
                    break

                query = parse_qs(
                    parsed.query,
                    keep_blank_values=True,
                )

                target = None

                for key in (
                    "u",
                    "url",
                    "target",
                    "redirect",
                ):

                    values = query.get(
                        key,
                        [],
                    )

                    if values:

                        target = values[0]
                        break

                if not target:
                    break

                target = unquote(target)

                if not is_http_url(
                    target
                ):
                    break

                current = normalize_url(
                    target
                )

            except Exception:

                break

        return current

    # ========================================================
    # FETCH
    # ========================================================

    async def fetch_one(
        self,
        client: HTTPClient,
        url: str,
    ) -> Optional[Snapshot]:

        url = normalize_url(url)

        if not url:
            return None

        if not is_http_url(url):
            return None

        if url in self.visited:
            return self.cache.get(url)

        # Atomic enough because event loop doesn't switch
        # between these simple operations.

        if len(self.visited) >= self.max_pages:
            return None

        self.visited.add(url)

        async with self.semaphore:

            snapshot = await client.get(
                url
            )

        self.cache[url] = snapshot

        if snapshot:
            self.snapshots.append(
                snapshot
            )

        return snapshot

    # ========================================================
    # ANALYZE
    # ========================================================

    def analyze_snapshot(
        self,
        snapshot: Snapshot,
    ):

        # ----------------------------------------------------
        # FINAL URL
        # ----------------------------------------------------

        parser = URLParser()

        parser.parse(
            snapshot.final_url
        )

        self.add_evidence(
            parser.evidence
        )

        # ----------------------------------------------------
        # REDIRECT HISTORY
        # ----------------------------------------------------

        for history_url in snapshot.history:

            parser = URLParser()

            parser.parse(
                history_url
            )

            self.add_evidence(
                parser.evidence
            )

        # ----------------------------------------------------
        # METADATA
        # ----------------------------------------------------

        metadata = MetadataParser()

        metadata.parse(
            snapshot.html
        )

        self.add_evidence(
            metadata.evidence
        )

        # ----------------------------------------------------
        # CANONICAL
        # ----------------------------------------------------

        if metadata.canonical:

            parser = URLParser()

            parser.parse(
                metadata.canonical
            )

            self.add_evidence(
                parser.evidence
            )

        # ----------------------------------------------------
        # HTML ID
        # ----------------------------------------------------

        self.add_evidence(
            HTMLIDEngine().parse(
                snapshot.html
            )
        )

        # ----------------------------------------------------
        # IDENTITY
        # ----------------------------------------------------

        self.add_evidence(
            IdentityEngine().parse(
                snapshot.html
            )
        )

        # ----------------------------------------------------
        # CORRELATION
        # ----------------------------------------------------

        self.add_evidence(
            CorrelationEngine().parse(
                snapshot.html
            )
        )

        # ----------------------------------------------------
        # DEEP LINK
        # ----------------------------------------------------

        self.add_evidence(
            DeepLinkEngine().parse(
                snapshot.html
            )
        )

        # ----------------------------------------------------
        # JSON LD
        # ----------------------------------------------------

        jsonld_engine = JSONLDEngine()

        for data in metadata.jsonld:

            self.add_evidence(
                jsonld_engine.walk(data)
            )

    # ========================================================
    # DISCOVER URLS
    # ========================================================

    def discover_urls(
        self,
        snapshot: Snapshot,
    ) -> list[str]:

        urls = []

        for match in URL_RE.finditer(
            snapshot.html
        ):

            candidate = strip_url_punctuation(
                match.group(0)
            )

            candidate = html.unescape(
                candidate
            )

            try:

                parsed = urlparse(
                    candidate
                )

                host = normalize_host(
                    parsed.hostname or ""
                )

                if host in FB_ALL_HOSTS:

                    normalized = normalize_url(
                        candidate
                    )

                    if normalized:
                        urls.append(
                            normalized
                        )

            except Exception:
                continue

        relative_pattern = re.compile(
            r'(?:"|\')'
            r"(/(?:"
            r"p|share|reel|reels|videos|"
            r"photos|posts|stories|groups"
            r")/"
            r"[^\"'\s<>]+)"
            r'(?:"|\')',
            re.IGNORECASE,
        )

        for match in relative_pattern.finditer(
            snapshot.html
        ):

            relative = match.group(1)

            absolute = urljoin(
                snapshot.final_url,
                relative,
            )

            absolute = normalize_url(
                absolute
            )

            if absolute:
                urls.append(
                    absolute
                )

        return unique_list(
            urls
        )

    # ========================================================
    # PROFILE URL
    # ========================================================

    def derive_profile_url(
        self,
        evidence,
    ) -> Optional[str]:

        usernames = []

        for item in evidence:

            if (
                item.field
                != "publisher_username"
            ):
                continue

            username = clean_text(
                item.value
            )

            if (
                username
                and username.lower()
                not in RESERVED_PATHS
            ):

                usernames.append(
                    username
                )

        usernames = unique_list(
            usernames
        )

        if not usernames:
            return None

        username = usernames[0]

        return (
            "https://www.facebook.com/"
            + quote(
                username,
                safe="._-",
            )
        )

    # ========================================================
    # BEST VALUE
    # ========================================================

    def best_value(
        self,
        field_name: str,
    ) -> Optional[str]:

        candidates = [
            item
            for item in self.all_evidence
            if item.field == field_name
        ]

        if not candidates:
            return None

        scores = defaultdict(int)

        source_bonus = {
            "url": 30,
            "share_url": 25,
            "query": 20,
            "html_js": 15,
            "identity": 15,
            "correlation": 10,
            "meta": 8,
            "jsonld": 5,
        }

        for item in candidates:

            bonus = 0

            for source, value in source_bonus.items():

                if item.source.startswith(
                    source
                ):

                    bonus = value
                    break

            scores[item.value] += (
                item.score
                + bonus
            )

        return max(
            scores,
            key=scores.get,
        )

    # ========================================================
    # URL TYPE
    # ========================================================

    def determine_url_type(
        self,
        url: str,
    ) -> str:

        path = (
            urlparse(url).path
            or ""
        ).lower()

        if "/stories/" in path:
            return "STORY"

        if "/reels/" in path:
            return "REEL"

        if "/reel/" in path:
            return "REEL"

        if "/videos/" in path:
            return "VIDEO"

        if "/photos/" in path:
            return "PHOTO"

        if "/posts/" in path:
            return "POST"

        if path.startswith("/p/"):
            return "POST"

        if "/share/p/" in path:
            return "POST"

        if "/share/v/" in path:
            return "VIDEO"

        if "/share/r/" in path:
            return "REEL"

        if "/groups/" in path:
            return "GROUP"

        return "PROFILE"

    # ========================================================
    # BUILD RESULT
    # ========================================================

    def build_result(
        self,
        input_url: str,
    ) -> Result:

        result = Result()

        result.input_url = input_url

        # Chọn snapshot phù hợp nhất:
        # snapshot cuối cùng có final URL ưu tiên.

        if self.snapshots:

            result.resolved_url = (
                self.snapshots[0].final_url
            )

        else:

            result.resolved_url = input_url

        result.url_type = (
            self.determine_url_type(
                result.resolved_url
            )
        )

        # ----------------------------------------------------
        # DEDUP
        # ----------------------------------------------------

        unique = {}

        for item in self.all_evidence:

            key = (
                item.field,
                item.value,
            )

            previous = unique.get(key)

            if (
                previous is None
                or item.score > previous.score
            ):

                unique[key] = item

        result.evidence = list(
            unique.values()
        )

        # ----------------------------------------------------
        # MAIN IDS
        # ----------------------------------------------------

        result.publisher_id = (
            self.best_value(
                "publisher_id"
            )
            or self.best_value(
                "profile_id"
            )
        )

        result.user_uid = (
            self.best_value(
                "user_id"
            )
            or self.best_value(
                "profile_id"
            )
            or self.best_value(
                "publisher_id"
            )
        )

        result.page_id = (
            self.best_value(
                "page_id"
            )
        )

        result.group_id = (
            self.best_value(
                "group_id"
            )
        )

        result.post_id = (
            self.best_value(
                "post_id"
            )
        )

        result.video_id = (
            self.best_value(
                "video_id"
            )
        )

        result.reel_id = (
            self.best_value(
                "reel_id"
            )
        )

        result.photo_id = (
            self.best_value(
                "photo_id"
            )
        )

        result.story_id = (
            self.best_value(
                "story_id"
            )
        )

        result.media_fbid = (
            self.best_value(
                "media_fbid"
            )
        )

        result.actor_id = (
            self.best_value(
                "actor_id"
            )
        )

        result.decoded_id = (
            self.best_value(
                "decoded_id"
            )
            or self.best_value(
                "jsonld_id"
            )
        )

        result.entity_id = (
            self.best_value(
                "entity_id"
            )
        )

        result.publisher_username = (
            self.best_value(
                "publisher_username"
            )
        )

        result.title = (
            self.best_value(
                "title"
            )
            or ""
        )

        # ----------------------------------------------------
        # ENTITY FALLBACK
        # ----------------------------------------------------

        if not result.entity_id:

            for value in (
                result.post_id,
                result.video_id,
                result.reel_id,
                result.photo_id,
                result.story_id,
            ):

                if value:

                    result.entity_id = value
                    break

        # ----------------------------------------------------
        # MEDIA FBID DECODE
        # ----------------------------------------------------

        if result.media_fbid:

            decoded = decode_fb_encoded_id(
                result.media_fbid
            )

            if decoded.get(
                "actor_id"
            ):

                result.actor_id = (
                    decoded["actor_id"]
                )

            if (
                decoded.get("object_id")
                and not result.entity_id
            ):

                result.entity_id = (
                    decoded["object_id"]
                )

            if decoded.get(
                "decoded_id"
            ):

                result.decoded_id = (
                    decoded["decoded_id"]
                )

        # ----------------------------------------------------
        # PUBLISHER TYPE
        # ----------------------------------------------------

        if result.group_id:

            result.publisher_type = "GROUP"

        elif result.page_id:

            result.publisher_type = "PAGE"

        elif result.publisher_id:

            result.publisher_type = "USER"

        elif result.user_uid:

            result.publisher_type = "USER"

        # ----------------------------------------------------
        # ENTITY TYPE
        # ----------------------------------------------------

        if result.group_id:

            result.entity_type = "GROUP"

        elif result.page_id:

            result.entity_type = "PAGE"

        elif result.post_id:

            result.entity_type = "POST"

        elif result.reel_id:

            result.entity_type = "REEL"

        elif result.video_id:

            result.entity_type = "VIDEO"

        elif result.photo_id:

            result.entity_type = "PHOTO"

        elif result.story_id:

            result.entity_type = "STORY"

        elif result.user_uid:

            result.entity_type = "USER"

        # ----------------------------------------------------
        # UID VERIFICATION
        # ----------------------------------------------------

        uid_evidence = [
            item
            for item in result.evidence
            if item.field in {
                "user_id",
                "profile_id",
                "publisher_id",
                "actor_id",
            }
            and is_numeric_id(
                item.value
            )
        ]

        if result.user_uid:

            independent_sources = {
                item.source
                for item in uid_evidence
                if item.value
                == result.user_uid
            }

            result.user_uid_verified = (
                len(independent_sources)
                >= 2
            )

        # ----------------------------------------------------
        # CONFIDENCE
        # ----------------------------------------------------

        confidence = 0

        if result.user_uid:
            confidence += 30

        if result.user_uid_verified:
            confidence += 25

        if result.publisher_username:
            confidence += 10

        if result.entity_id:
            confidence += 10

        if any(
            (
                result.post_id,
                result.video_id,
                result.reel_id,
                result.photo_id,
                result.story_id,
            )
        ):
            confidence += 10

        if self.snapshots:
            confidence += 10

        if any(
            item.source in {
                "url",
                "share_url",
            }
            for item in result.evidence
        ):
            confidence += 5

        result.confidence = min(
            confidence,
            100,
        )

        # ----------------------------------------------------
        # STATUS
        # ----------------------------------------------------

        if not self.snapshots:

            result.status = (
                "HTTP_FETCH_FAILED"
            )

            result.success = False

        elif result.user_uid:

            result.status = "RESOLVED"
            result.success = True

        elif result.entity_id:

            result.status = (
                "ENTITY_RESOLVED"
            )

            result.success = True

        else:

            result.status = (
                "NO_PUBLIC_ID_FOUND"
            )

            result.success = False

        # ----------------------------------------------------
        # WARNINGS
        # ----------------------------------------------------

        for snapshot in self.snapshots:

            if snapshot.status_code in {
                401,
                403,
            }:

                result.warnings.append(
                    "Facebook giới hạn truy cập "
                    "hoặc yêu cầu xác thực."
                )

            elif snapshot.status_code == 404:

                result.warnings.append(
                    "URL trả về HTTP 404."
                )

            elif snapshot.status_code == 429:

                result.warnings.append(
                    "Facebook rate-limit request."
                )

        if not result.user_uid:

            result.warnings.append(
                "Không tìm thấy UID người dùng "
                "đủ bằng chứng trong nội dung công khai."
            )

        elif not result.user_uid_verified:

            result.warnings.append(
                "UID tìm được nhưng chưa đạt "
                "mức xác minh đa nguồn."
            )

        if result.resolved_url != input_url:

            result.warnings.append(
                "URL đã được Facebook redirect "
                "sang URL khác."
            )

        result.warnings = unique_list(
            result.warnings
        )

        # ----------------------------------------------------
        # CRAWL CHAIN
        # ----------------------------------------------------

        result.crawl_chain = unique_list(
            [
                snapshot.final_url
                for snapshot in self.snapshots
            ]
        )

        return result

    # ========================================================
    # RESOLVE
    # ========================================================

    async def resolve(
        self,
        url: str,
    ) -> Result:

        url = normalize_url(url)

        if not url:

            return Result(
                input_url="",
                status="INVALID_URL",
                success=False,
            )

        if not is_fb_host_or_redirect(
            url
        ):

            return Result(
                input_url=url,
                resolved_url=url,
                url_type="NON_FACEBOOK",
                status="INVALID_FACEBOOK_URL",
                success=False,
                warnings=[
                    "URL không thuộc Facebook."
                ],
            )

        original_url = url

        url = self.unwrap_url(
            url
        )

        # ====================================================
        # INITIAL URL
        # ====================================================

        parser = URLParser()

        parser.parse(
            url
        )

        self.add_evidence(
            parser.evidence
        )

        async with HTTPClient(
            timeout=self.timeout
        ) as client:

            first = await self.fetch_one(
                client,
                url,
            )

            if not first:

                return self.build_result(
                    original_url
                )

            self.analyze_snapshot(
                first
            )

            # =================================================
            # PROFILE
            # =================================================

            profile_url = (
                self.derive_profile_url(
                    self.all_evidence
                )
            )

            # =================================================
            # DISCOVER
            # =================================================

            candidates = self.discover_urls(
                first
            )

            if profile_url:

                candidates.insert(
                    0,
                    profile_url,
                )

            candidates = unique_list(
                candidates
            )

            candidates = [
                item
                for item in candidates
                if item
                != first.final_url
            ]

            available = max(
                0,
                self.max_pages
                - len(self.visited),
            )

            candidates = candidates[
                :available
            ]

            # =================================================
            # CONCURRENT FETCH
            # =================================================

            if candidates:

                tasks = [
                    self.fetch_one(
                        client,
                        candidate,
                    )
                    for candidate in candidates
                ]

                snapshots = await asyncio.gather(
                    *tasks,
                    return_exceptions=True,
                )

                for snapshot in snapshots:

                    if isinstance(
                        snapshot,
                        Snapshot,
                    ):

                        self.analyze_snapshot(
                            snapshot
                        )

        return self.build_result(
            original_url
        )


# ============================================================
# TELEGRAM FORMAT
# ============================================================

def tg_escape(
    value: Any,
) -> str:

    return html.escape(
        clean_text(value),
        quote=False,
    )


def field_line(
    label: str,
    value: Any,
) -> Optional[str]:

    value = clean_text(value)

    if not value:
        return None

    return (
        f"<b>{tg_escape(label)}</b>: "
        f"<code>"
        f"{tg_escape(shorten_id(value))}"
        f"</code>"
    )


def format_result(
    result: Result,
    elapsed: float,
) -> str:

    lines = []

    lines.append(
        f"<b>🔎 FB UID / ENTITY RESOLVER "
        f"{VERSION}</b>"
    )

    lines.append("")

    status_icon = (
        "✅"
        if result.success
        else "⚠️"
    )

    lines.append(
        f"{status_icon} "
        f"<b>STATUS:</b> "
        f"<code>"
        f"{tg_escape(result.status)}"
        f"</code>"
    )

    lines.append(
        f"🎯 <b>CONFIDENCE:</b> "
        f"<code>"
        f"{result.confidence}%"
        f"</code>"
    )

    lines.append(
        f"⏱ <b>TIME:</b> "
        f"<code>"
        f"{elapsed:.2f}s"
        f"</code>"
    )

    lines.append("")

    # ========================================================
    # UID
    # ========================================================

    if result.user_uid:

        verification = (
            "VERIFIED"
            if result.user_uid_verified
            else "FOUND / NOT FULLY VERIFIED"
        )

        lines.append(
            "👤 <b>USER UID:</b> "
            f"<code>"
            f"{tg_escape(result.user_uid)}"
            f"</code>"
        )

        lines.append(
            "🛡 <b>VERIFICATION:</b> "
            f"<code>"
            f"{verification}"
            f"</code>"
        )

    else:

        lines.append(
            "👤 <b>USER UID:</b> "
            "<code>NOT FOUND</code>"
        )

    # ========================================================
    # TYPES
    # ========================================================

    lines.append("")

    lines.append(
        "🌐 <b>URL TYPE:</b> "
        f"<code>"
        f"{tg_escape(result.url_type)}"
        f"</code>"
    )

    lines.append(
        "🏷 <b>ENTITY TYPE:</b> "
        f"<code>"
        f"{tg_escape(result.entity_type)}"
        f"</code>"
    )

    lines.append(
        "👥 <b>PUBLISHER TYPE:</b> "
        f"<code>"
        f"{tg_escape(result.publisher_type)}"
        f"</code>"
    )

    # ========================================================
    # USERNAME
    # ========================================================

    if result.publisher_username:

        lines.append(
            "📛 <b>USERNAME:</b> "
            f"<code>@"
            f"{tg_escape(result.publisher_username)}"
            f"</code>"
        )

    if result.publisher_id:

        lines.append(
            "🆔 <b>PUBLISHER ID:</b> "
            f"<code>"
            f"{tg_escape(result.publisher_id)}"
            f"</code>"
        )

    # ========================================================
    # PAGE / GROUP
    # ========================================================

    if result.page_id:

        lines.append(
            "📄 <b>PAGE ID:</b> "
            f"<code>"
            f"{tg_escape(result.page_id)}"
            f"</code>"
        )

    if result.group_id:

        lines.append(
            "👥 <b>GROUP ID:</b> "
            f"<code>"
            f"{tg_escape(result.group_id)}"
            f"</code>"
        )

    # ========================================================
    # CONTENT
    # ========================================================

    content_fields = [

        (
            "POST ID",
            result.post_id,
        ),

        (
            "VIDEO ID",
            result.video_id,
        ),

        (
            "REEL ID",
            result.reel_id,
        ),

        (
            "PHOTO ID",
            result.photo_id,
        ),

        (
            "STORY ID",
            result.story_id,
        ),

        (
            "MEDIA FBID",
            result.media_fbid,
        ),

        (
            "ACTOR ID",
            result.actor_id,
        ),

        (
            "ENTITY ID",
            result.entity_id,
        ),

        (
            "DECODED ID",
            result.decoded_id,
        ),
    ]

    for label, value in content_fields:

        line = field_line(
            label,
            value,
        )

        if line:
            lines.append(line)

    # ========================================================
    # TITLE
    # ========================================================

    if result.title:

        title = tg_escape(
            truncate(
                result.title,
                350,
            )
        )

        lines.append("")

        lines.append(
            "📝 <b>TITLE:</b> "
            + title
        )

    # ========================================================
    # RESOLVED URL
    # ========================================================

    if result.resolved_url:

        resolved = tg_escape(
            truncate(
                result.resolved_url,
                700,
            )
        )

        lines.append("")

        lines.append(
            "🔗 <b>RESOLVED:</b>\n"
            "<code>"
            + resolved
            + "</code>"
        )

    # ========================================================
    # WARNINGS
    # ========================================================

    if result.warnings:

        lines.append("")

        lines.append(
            "⚠️ <b>WARNINGS:</b>"
        )

        for warning in result.warnings[:5]:

            lines.append(
                "• "
                + tg_escape(
                    truncate(
                        warning,
                        250,
                    )
                )
            )

    # ========================================================
    # EVIDENCE
    # ========================================================

    lines.append("")

    lines.append(
        "📊 <b>EVIDENCE:</b> "
        f"<code>"
        f"{len(result.evidence)}"
        f"</code> signals"
    )

    lines.append(
        "📡 <b>PAGES:</b> "
        f"<code>"
        f"{len(result.crawl_chain)}"
        f"</code>"
    )

    output = "\n".join(
        lines
    )

    if len(output) > MAX_TELEGRAM_MESSAGE:

        output = output[
            :MAX_TELEGRAM_MESSAGE - 30
        ]

        output += "\n\n<i>...</i>"

    return output


# ============================================================
# TELEGRAM TEXT
# ============================================================

START_TEXT = """
<b>🔎 FB UID / ENTITY RESOLVER V15 ULTRA</b>

Bot phân tích URL Facebook bằng HTTP public.

<b>Không sử dụng:</b>
• Playwright
• Selenium
• Cookie
• Access Token
• Facebook Login

<b>Lệnh:</b>

<code>/getuidfb URL</code>

Ví dụ:

<code>/getuidfb https://www.facebook.com/username</code>

Bạn cũng có thể gửi trực tiếp URL Facebook.

<b>Hỗ trợ:</b>
• Profile / User
• Page
• Group
• Post
• Reel
• Video
• Photo
• Story
• share/p
• share/v
• share/r
• pfbid
• media_fbid
• actor_id
• profile_id
• entity_id
• UID verification

<b>Engine:</b>
<code>Telethon + HTTPX Async</code>
"""


# ============================================================
# GLOBAL TELEGRAM STATE
# ============================================================

client: Optional[
    TelegramClient
] = None

resolve_semaphore = asyncio.Semaphore(
    GLOBAL_CONCURRENCY
)

active_tasks: set[asyncio.Task] = set()


# ============================================================
# SAFE TASK
# ============================================================

def create_background_task(
    coro,
) -> asyncio.Task:

    task = asyncio.create_task(
        coro
    )

    active_tasks.add(task)

    task.add_done_callback(
        active_tasks.discard
    )

    return task


# ============================================================
# PROCESS FACEBOOK
# ============================================================

async def process_facebook_event(
    event,
    url: str,
):

    url = strip_url_punctuation(
        clean_text(url)
    )

    if not url:
        return

    normalized = normalize_url(
        url
    )

    if not is_http_url(
        normalized
    ):

        await event.respond(
            "❌ URL không hợp lệ."
        )

        return

    if not is_fb_host_or_redirect(
        normalized
    ):

        await event.respond(
            "❌ Đây không phải URL Facebook."
        )

        return

    processing_message = (
        await event.respond(
            "⏳ <b>Đang phân tích Facebook...</b>\n\n"
            "HTTP public resolver đang thu thập "
            "các bằng chứng UID/entity.",
            parse_mode="html",
            link_preview=False,
        )
    )

    started = time.perf_counter()

    try:

        # ====================================================
        # GLOBAL CONCURRENCY
        # ====================================================

        async with resolve_semaphore:

            resolver = FacebookResolver(
                timeout=DEFAULT_TIMEOUT,
                max_pages=DEFAULT_MAX_PAGES,
                concurrency=DEFAULT_CONCURRENCY,
            )

            result = await resolver.resolve(
                normalized
            )

        elapsed = (
            time.perf_counter()
            - started
        )

        output = format_result(
            result,
            elapsed,
        )

        # ====================================================
        # BUTTONS
        # ====================================================

        buttons = []

        if (
            result.user_uid
            and is_numeric_id(
                result.user_uid
            )
        ):

            profile_url = (
                "https://www.facebook.com/"
                + result.user_uid
            )

            buttons.append(
                [
                    Button.url(
                        "👤 Mở Profile",
                        profile_url,
                    )
                ]
            )

        if result.publisher_username:

            username_url = (
                "https://www.facebook.com/"
                + quote(
                    result.publisher_username,
                    safe="._-",
                )
            )

            buttons.append(
                [
                    Button.url(
                        "📛 Mở Username",
                        username_url,
                    )
                ]
            )

        if (
            result.resolved_url
            and is_http_url(
                result.resolved_url
            )
        ):

            buttons.append(
                [
                    Button.url(
                        "🔗 Mở URL",
                        result.resolved_url,
                    )
                ]
            )

        # ====================================================
        # EDIT RESULT
        # ====================================================

        await processing_message.edit(
            output,
            parse_mode="html",
            link_preview=False,
            buttons=buttons or None,
        )

    except FloodWaitError as exc:

        await processing_message.edit(
            (
                "⚠️ <b>Telegram FloodWait</b>\n\n"
                f"Vui lòng thử lại sau "
                f"<code>{exc.seconds}s</code>."
            ),
            parse_mode="html",
        )

    except asyncio.CancelledError:

        try:
            await processing_message.edit(
                "❌ Request đã bị huỷ."
            )
        except Exception:
            pass

        raise

    except Exception as exc:

        elapsed = (
            time.perf_counter()
            - started
        )

        error_text = (
            "❌ <b>Resolver Error</b>\n\n"
            "<b>Error:</b> "
            f"<code>"
            f"{tg_escape(str(exc)[:1000])}"
            f"</code>\n"
            "<b>Time:</b> "
            f"<code>{elapsed:.2f}s</code>"
        )

        try:

            await processing_message.edit(
                error_text,
                parse_mode="html",
            )

        except Exception:

            try:

                await event.respond(
                    error_text,
                    parse_mode="html",
                )

            except Exception:
                pass


# ============================================================
# START
# ============================================================

async def handle_start(
    event,
):

    await event.respond(
        START_TEXT,
        parse_mode="html",
        link_preview=False,
    )


# ============================================================
# HELP
# ============================================================

async def handle_help(
    event,
):

    await event.respond(
        START_TEXT,
        parse_mode="html",
        link_preview=False,
    )


# ============================================================
# GETUIDFB
# ============================================================

async def handle_getuidfb(
    event,
):

    text = (
        event.raw_text
        or ""
    ).strip()

    # Remove command.

    match = re.match(
        r"^/getuidfb(?:@\w+)?"
        r"(?:\s+(.+))?$",
        text,
        re.IGNORECASE | re.DOTALL,
    )

    if not match:

        return

    argument = (
        match.group(1)
        or ""
    ).strip()

    if not argument:

        await event.respond(
            "❗ <b>Cú pháp:</b>\n\n"
            "<code>/getuidfb "
            "https://www.facebook.com/username"
            "</code>",
            parse_mode="html",
        )

        return

    url = extract_url(
        argument
    )

    if not url:
        url = argument

    create_background_task(
        process_facebook_event(
            event,
            url,
        )
    )


# ============================================================
# DIRECT URL
# ============================================================

async def handle_direct_url(
    event,
):

    text = (
        event.raw_text
        or ""
    ).strip()

    if not text:
        return

    # Ignore commands.

    if text.startswith("/"):
        return

    url = extract_url(
        text
    )

    if not url:
        return

    create_background_task(
        process_facebook_event(
            event,
            url,
        )
    )


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================

async def universal_message_handler(
    event,
):

    if not event.is_private and not event.is_group:
        return

    text = (
        event.raw_text
        or ""
    ).strip()

    if not text:
        return

    # ========================================================
    # START
    # ========================================================

    if re.fullmatch(
        r"/start(?:@\w+)?",
        text,
        re.IGNORECASE,
    ):

        await handle_start(
            event
        )

        return

    # ========================================================
    # HELP
    # ========================================================

    if re.fullmatch(
        r"/help(?:@\w+)?",
        text,
        re.IGNORECASE,
    ):

        await handle_help(
            event
        )

        return

    # ========================================================
    # GETUIDFB
    # ========================================================

    if re.match(
        r"^/getuidfb(?:@\w+)?(?:\s|$)",
        text,
        re.IGNORECASE,
    ):

        await handle_getuidfb(
            event
        )

        return

    # ========================================================
    # OTHER COMMAND
    # ========================================================

    if text.startswith("/"):
        return

    # ========================================================
    # DIRECT FACEBOOK URL
    # ========================================================

    await handle_direct_url(
        event
    )


# ============================================================
# API CONFIG
# ============================================================

def get_api_id() -> int:

    value = os.getenv(
        API_ID_ENV,
        "",
    ).strip()

    if value:

        try:
            return int(value)

        except ValueError:

            print(
                "❌ API_ID phải là số."
            )

    print("")
    print(
        "Chưa tìm thấy API_ID."
    )

    while True:

        value = input(
            "API_ID: "
        ).strip()

        try:

            return int(value)

        except ValueError:

            print(
                "❌ API_ID phải là số."
            )


def get_api_hash() -> str:

    value = os.getenv(
        API_HASH_ENV,
        "",
    ).strip()

    if value:
        return value

    print("")
    print(
        "Chưa tìm thấy API_HASH."
    )

    return input(
        "API_HASH: "
    ).strip()


def get_bot_token() -> str:

    value = os.getenv(
        BOT_TOKEN_ENV,
        "",
    ).strip()

    if value:
        return value

    print("")
    print(
        "Chưa tìm thấy BOT_TOKEN."
    )

    try:

        return getpass.getpass(
            "BOT TOKEN: "
        ).strip()

    except Exception:

        return input(
            "BOT TOKEN: "
        ).strip()


# ============================================================
# TELETHON ERROR LOG
# ============================================================

async def telegram_error_logger(
    event,
    error,
):

    print(
        "[TELETHON ERROR]",
        repr(error),
    )


# ============================================================
# MAIN
# ============================================================

