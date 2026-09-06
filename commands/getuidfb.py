#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
====================================================================
 FACEBOOK UID / ENTITY RESOLVER V21 ULTRA
 NUMERIC-FIRST / STRUCTURE-FIRST
 TELEGRAM / TELETHON
====================================================================

PUBLIC CONTENT ONLY
HTTP ONLY

NO:
    Playwright
    Selenium
    Chromium
    Cookie
    Facebook Access Token
    Facebook Login

CORE PRINCIPLE
==============

1. URL STRUCTURE FIRST
2. NUMERIC FACEBOOK IDs SECOND
3. PROFILE / PUBLISHER CORRELATION THIRD
4. pfbid / opaque token LAST

IMPORTANT
=========

A pfbid token is NOT a USER UID.

A group_id is NEVER automatically user_uid.

A page_id is NEVER automatically user_uid.

Generic numeric IDs are NEVER automatically user_uid.

The resolver tries to establish:

    URL
      ↓
    OBJECT
      ↓
    PUBLISHER
      ↓
    PROFILE
      ↓
    NUMERIC USER UID

If that chain cannot be established with enough evidence,
USER UID is omitted instead of guessed.

TELEGRAM
========

/getuidfb

Then send:

    URL

or:

    URL1
    URL2
    URL3

or even:

    URL1 URL2 URL3

or malformed concatenated input:

    https://facebook.com/share/p/ABC/https://facebook.com/user/posts/pfbid...

The URL extractor separates every actual URL independently.

The session remains active until:

    /stop

====================================================================
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import json
import logging
import re
import time

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from urllib.parse import (
    parse_qs,
    unquote,
    urljoin,
    urlparse,
)

import httpx
from telethon import events


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# COMMAND INFO
# ============================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": "Facebook UID / Entity Resolver V21 Ultra",
    "usage": "/getuidfb",
    "category": "Facebook",
}


# ============================================================
# CONFIG
# ============================================================

MAX_URLS_PER_MESSAGE = 100

MAX_HTML_CHARS = 5_000_000
MAX_RESPONSE_BYTES = 15_000_000

HTTP_CONCURRENCY = 6

REQUEST_TIMEOUT = 22.0
PROFILE_TIMEOUT = 18.0

INTERACTIVE_TIMEOUT = 1800

MAX_OBJECT_SCAN = 15000
MAX_PROFILE_CRAWLS = 8


# ============================================================
# FACEBOOK HOSTS
# ============================================================

FB_MAIN_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
}

FB_WATCH_HOSTS = {
    "fb.watch",
}


# ============================================================
# RESERVED ROUTES
# ============================================================

FB_RESERVED_ROUTES = {
    "home",
    "watch",
    "reel",
    "reels",
    "video",
    "videos",
    "photo",
    "photos",
    "story",
    "stories",
    "share",
    "groups",
    "pages",
    "profile.php",
    "photo.php",
    "story.php",
    "permalink.php",
    "posts",
    "events",
    "marketplace",
    "gaming",
    "login",
    "recover",
    "help",
    "privacy",
    "terms",
    "settings",
    "messages",
    "notifications",
    "friends",
    "search",
    "hashtag",
    "plugins",
    "dialog",
    "ajax",
    "business",
    "watch",
}


# ============================================================
# USER AGENTS
# ============================================================

USER_AGENT_POOL = (
    (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.5 Mobile/15E148 Safari/604.1"
    ),
)


# ============================================================
# REGEX
# ============================================================

NUMERIC_ID_RE = re.compile(
    r"(?<!\d)(\d{5,25})(?!\d)"
)

PFBID_RE = re.compile(
    r"\bpfbid[A-Za-z0-9_-]{5,300}\b",
    re.IGNORECASE,
)

HTTP_URL_START_RE = re.compile(
    r"https?://",
    re.IGNORECASE,
)

FB_HOST_START_RE = re.compile(
    r"(?ix)"
    r"(?:https?://)?"
    r"(?:"
    r"(?:www\.|m\.|mbasic\.|web\.)?facebook\.com"
    r"|"
    r"fb\.watch"
    r")"
    r"/"
)


# ============================================================
# BASIC HELPERS
# ============================================================

def clean_text(value: Any) -> str:

    if value is None:
        return ""

    return html.unescape(
        str(value)
    ).strip()


def normalize_space(value: Any) -> str:

    return re.sub(
        r"\s+",
        " ",
        clean_text(value),
    ).strip()


def tg_escape(value: Any) -> str:

    return html.escape(
        clean_text(value),
        quote=False,
    )


def numeric_id(value: Any) -> Optional[str]:

    if value is None:
        return None

    value = clean_text(value)

    if re.fullmatch(
        r"\d{5,25}",
        value,
    ):
        return value

    return None


def is_numeric_id(value: Any) -> bool:

    return (
        numeric_id(value)
        is not None
    )


def unique_preserve(
    values: Iterable[Any],
) -> list[str]:

    output = []
    seen = set()

    for value in values:

        value = clean_text(value)

        if not value:
            continue

        key = value.lower()

        if key in seen:
            continue

        seen.add(key)
        output.append(value)

    return output


def truncate(
    value: Any,
    limit: int = 300,
) -> str:

    value = clean_text(value)

    if len(value) <= limit:
        return value

    return value[:limit - 1] + "…"


# ============================================================
# HOST HELPERS
# ============================================================

def host_of(url: str) -> str:

    try:

        return (
            urlparse(
                url
            )
            .netloc
            .lower()
            .split("@")[-1]
            .split(":")[0]
        )

    except Exception:

        return ""


def is_facebook_host(
    host: str,
) -> bool:

    host = (
        host
        .lower()
        .strip()
    )

    if host in FB_MAIN_HOSTS:
        return True

    if host.endswith(
        ".facebook.com"
    ):
        return True

    if host in FB_WATCH_HOSTS:
        return True

    if host.endswith(
        ".fb.watch"
    ):
        return True

    return False


def is_facebook_url(
    url: str,
) -> bool:

    return is_facebook_host(
        host_of(url)
    )


# ============================================================
# URL NORMALIZATION
# ============================================================

def normalize_input_url(
    raw: str,
) -> str:

    raw = clean_text(raw)

    raw = raw.strip(
        " \t\r\n<>[](){}'\".,;"
    )

    if not raw:
        return ""

    if raw.startswith("//"):
        raw = "https:" + raw

    if not re.match(
        r"^https?://",
        raw,
        re.IGNORECASE,
    ):
        raw = "https://" + raw

    return raw


# ============================================================
# IMPORTANT:
# ROBUST URL EXTRACTION
# ============================================================

def extract_raw_url_candidates(
    text: str,
) -> list[str]:

    """
    Tách URL bằng vị trí START của từng http(s)://.

    Ví dụ:

    https://facebook.com/share/p/ABC/https://facebook.com/user/123

    sẽ thành:

    https://facebook.com/share/p/ABC/
    https://facebook.com/user/123

    Không dùng regex greedy kiểu:
        https://[^\s]+

    vì nó sẽ nuốt luôn URL thứ hai.
    """

    if not text:
        return []

    matches = list(
        HTTP_URL_START_RE.finditer(
            text
        )
    )

    candidates = []

    # --------------------------------------------------------
    # URL có scheme
    # --------------------------------------------------------

    for index, match in enumerate(
        matches
    ):

        start = match.start()

        if index + 1 < len(matches):

            end = matches[
                index + 1
            ].start()

        else:

            end = len(text)

        candidate = text[
            start:end
        ]

        candidate = candidate.strip(
            " \t\r\n<>[](){}'\""
        )

        # Remove punctuation only.
        candidate = candidate.rstrip(
            ".,;)]}"
        )

        if candidate:
            candidates.append(
                candidate
            )

    # --------------------------------------------------------
    # Bare facebook.com
    # --------------------------------------------------------

    for match in FB_HOST_START_RE.finditer(
        text
    ):

        start = match.start()

        # Ignore if this position is already inside an
        # http URL.
        if start > 0:

            prefix = text[
                max(0, start - 8):start
            ]

            if re.search(
                r"https?://$",
                prefix,
                re.IGNORECASE,
            ):
                continue

        next_match = FB_HOST_START_RE.search(
            text,
            match.end(),
        )

        if next_match:

            end = next_match.start()

        else:

            end = len(text)

        candidate = text[
            start:end
        ].strip(
            " \t\r\n<>[](){}'\""
        )

        candidate = candidate.rstrip(
            ".,;)]}"
        )

        if candidate:
            candidates.append(
                candidate
            )

    return candidates


def extract_facebook_urls(
    text: str,
) -> list[str]:

    """
    Public Telegram URL extractor.

    Important:
        URL1URL2
        URL1 URL2
        URL1
        URL2

    đều được tách độc lập.

    URL bắt đầu bằng https:// mới được ưu tiên
    khi chuỗi URL bị dính vào nhau.
    """

    if not text:
        return []

    raw_candidates = (
        extract_raw_url_candidates(
            text
        )
    )

    output = []
    seen = set()

    for raw in raw_candidates:

        raw = normalize_input_url(
            raw
        )

        if not raw:
            continue

        # ----------------------------------------------------
        # Nếu candidate vẫn chứa một URL thứ hai,
        # tiếp tục tách đệ quy.
        # ----------------------------------------------------

        nested = list(
            HTTP_URL_START_RE.finditer(
                raw[8:]
            )
        )

        if nested:

            split_positions = [
                8 + m.start()
                for m in nested
            ]

            pieces = []
            last = 0

            for position in split_positions:

                pieces.append(
                    raw[last:position]
                )

                last = position

            pieces.append(
                raw[last:]
            )

        else:

            pieces = [raw]

        for piece in pieces:

            piece = normalize_input_url(
                piece
            )

            if not piece:
                continue

            # ------------------------------------------------
            # Stop at accidental quote/bracket.
            # ------------------------------------------------

            piece = piece.rstrip(
                " \t\r\n<>[](){}'\".,;"
            )

            host = host_of(piece)

            if not is_facebook_host(
                host
            ):
                continue

            key = piece.lower()

            if key in seen:
                continue

            seen.add(key)
            output.append(piece)

            if len(output) >= MAX_URLS_PER_MESSAGE:
                return output

    return output


# ============================================================
# QUERY
# ============================================================

def first_query_value(
    query: dict[str, list[str]],
    keys: Iterable[str],
) -> Optional[str]:

    for key in keys:

        values = query.get(
            key
        )

        if not values:
            continue

        for value in values:

            value = clean_text(
                value
            )

            if value:
                return value

    return None


# ============================================================
# PARSED URL
# ============================================================

@dataclass
class ParsedURL:

    original: str

    normalized: str = ""

    host: str = ""
    path: str = ""

    url_type: str = "UNKNOWN"

    entity_type: str = "UNKNOWN"

    username: Optional[str] = None

    object_id: Optional[str] = None

    opaque_id: Optional[str] = None

    profile_id: Optional[str] = None

    page_id: Optional[str] = None

    group_id: Optional[str] = None

    post_id: Optional[str] = None

    video_id: Optional[str] = None

    photo_id: Optional[str] = None

    story_id: Optional[str] = None


# ============================================================
# URL CLASSIFIER
# ============================================================

def classify_facebook_url(
    raw_url: str,
) -> ParsedURL:

    normalized = normalize_input_url(
        raw_url
    )

    parsed = urlparse(
        normalized
    )

    host = host_of(
        normalized
    )

    path = parsed.path or "/"

    segments = [
        unquote(x).strip()
        for x in path.split("/")
        if x.strip()
    ]

    lower = [
        x.lower()
        for x in segments
    ]

    query = parse_qs(
        parsed.query
    )

    result = ParsedURL(
        original=raw_url,
        normalized=normalized,
        host=host,
        path=path,
    )

    # ========================================================
    # PROFILE.PHP
    # ========================================================

    if lower and lower[0] == "profile.php":

        uid = numeric_id(
            first_query_value(
                query,
                (
                    "id",
                    "uid",
                    "user_id",
                    "profile_id",
                ),
            )
        )

        result.url_type = "PROFILE"
        result.entity_type = "USER"
        result.profile_id = uid
        result.object_id = uid

        return result

    # ========================================================
    # PHOTO.PHP
    # ========================================================

    if lower and lower[0] == "photo.php":

        photo = first_query_value(
            query,
            (
                "fbid",
                "photo_id",
                "photoid",
            ),
        )

        owner = numeric_id(
            first_query_value(
                query,
                (
                    "id",
                    "owner_id",
                    "profile_id",
                ),
            )
        )

        result.url_type = "PHOTO"
        result.entity_type = "PHOTO"

        if photo:

            if is_numeric_id(photo):

                result.photo_id = (
                    numeric_id(photo)
                )

                result.object_id = (
                    result.photo_id
                )

            else:

                result.photo_id = photo
                result.object_id = photo
                result.opaque_id = photo

        result.profile_id = owner

        return result

    # ========================================================
    # STORY.PHP
    # ========================================================

    if lower and lower[0] == "story.php":

        story = first_query_value(
            query,
            (
                "story_fbid",
                "story_id",
                "fbid",
            ),
        )

        owner = numeric_id(
            first_query_value(
                query,
                (
                    "id",
                    "owner_id",
                    "profile_id",
                ),
            )
        )

        result.url_type = "STORY"
        result.entity_type = "STORY"

        if story:

            result.story_id = (
                numeric_id(story)
                or story
            )

            result.object_id = (
                result.story_id
            )

            if not is_numeric_id(story):
                result.opaque_id = story

        result.profile_id = owner

        return result

    # ========================================================
    # WATCH
    # ========================================================

    if lower and lower[0] == "watch":

        video = first_query_value(
            query,
            (
                "v",
                "video_id",
                "videoid",
            ),
        )

        result.url_type = "VIDEO"
        result.entity_type = "VIDEO"

        if video:

            result.video_id = (
                numeric_id(video)
                or video
            )

            result.object_id = (
                result.video_id
            )

            if not is_numeric_id(video):
                result.opaque_id = video

        return result

    # ========================================================
    # SHARE
    # ========================================================

    if len(lower) >= 3 and lower[0] == "share":

        kind = lower[1]
        token = segments[2]

        result.object_id = token

        if token.lower().startswith(
            "pfbid"
        ):
            result.opaque_id = token

        elif is_numeric_id(token):

            if kind == "p":
                result.post_id = token

            elif kind == "v":
                result.video_id = token

            elif kind == "r":
                result.video_id = token

        if kind == "p":

            result.url_type = "POST"
            result.entity_type = "POST"

        elif kind == "v":

            result.url_type = "VIDEO"
            result.entity_type = "VIDEO"

        elif kind == "r":

            result.url_type = "REEL"
            result.entity_type = "VIDEO"

        else:

            result.url_type = "SHARE"
            result.entity_type = "UNKNOWN"

        return result

    # ========================================================
    # /p/OBJECT
    # ========================================================

    if len(lower) >= 2 and lower[0] == "p":

        token = segments[1]

        result.url_type = "POST"
        result.entity_type = "POST"

        result.object_id = token

        if is_numeric_id(token):

            result.post_id = token

        else:

            result.opaque_id = token

        return result

    # ========================================================
    # GROUP
    # ========================================================

    if lower and lower[0] == "groups":

        result.entity_type = "GROUP"

        if len(segments) >= 2:

            group = segments[1]

            if is_numeric_id(group):

                result.group_id = (
                    numeric_id(group)
                )

        if len(lower) >= 3:

            action = lower[2]

            if action in {
                "posts",
                "post",
                "permalink",
            }:

                result.url_type = "GROUP_POST"

                if len(segments) >= 4:

                    token = segments[3]

                    result.object_id = token

                    if is_numeric_id(token):

                        result.post_id = token

                    else:

                        result.opaque_id = token

                return result

        result.url_type = "GROUP"

        return result

    # ========================================================
    # PAGE
    # ========================================================

    if lower and lower[0] == "pages":

        result.entity_type = "PAGE"

        if len(segments) >= 2:

            result.username = segments[1]

        for segment in segments[2:5]:

            if is_numeric_id(segment):

                result.page_id = (
                    numeric_id(segment)
                )

                break

        # Facebook page post:
        #
        # /pages/name/PAGE_ID/posts/POST_ID
        # /pages/name/PAGE_ID/post/POST_ID
        # /pages/name/PAGE_ID/permalink/POST_ID

        if len(lower) >= 4:

            action = lower[3]

            if action in {
                "posts",
                "post",
                "permalink",
            }:

                result.url_type = "PAGE_POST"

                if len(segments) >= 5:

                    token = segments[4]

                    result.object_id = token

                    if is_numeric_id(token):

                        result.post_id = token

                    else:

                        result.opaque_id = token

                return result

        result.url_type = "PAGE"

        return result

    # ========================================================
    # REEL
    # ========================================================

    if lower and lower[0] in {
        "reel",
        "reels",
    }:

        result.url_type = "REEL"
        result.entity_type = "VIDEO"

        if len(segments) >= 2:

            token = segments[1]

            result.object_id = token

            if is_numeric_id(token):

                result.video_id = token

            else:

                result.opaque_id = token

        return result

    # ========================================================
    # VIDEO
    # ========================================================

    if lower and lower[0] in {
        "video",
        "videos",
    }:

        result.url_type = "VIDEO"
        result.entity_type = "VIDEO"

        if len(segments) >= 2:

            token = segments[1]

            result.object_id = token

            if is_numeric_id(token):

                result.video_id = token

            else:

                result.opaque_id = token

        return result

    # ========================================================
    # STORY
    # ========================================================

    if lower and lower[0] in {
        "story",
        "stories",
    }:

        result.url_type = "STORY"
        result.entity_type = "STORY"

        if len(segments) >= 2:

            token = segments[1]

            result.object_id = token

            if is_numeric_id(token):

                result.story_id = token

            else:

                result.opaque_id = token

        return result

    # ========================================================
    # USERNAME ROUTES
    # ========================================================

    if segments:

        first = segments[0]

        if (
            first.lower()
            not in FB_RESERVED_ROUTES
            and not is_numeric_id(first)
        ):

            result.username = first

            if len(lower) >= 2:

                action = lower[1]

                # ------------------------------------------------
                # USER POST
                # ------------------------------------------------

                if action in {
                    "posts",
                    "post",
                }:

                    result.url_type = "USER_POST"
                    result.entity_type = "USER"

                    if len(segments) >= 3:

                        token = segments[2]

                        result.object_id = token

                        if is_numeric_id(token):

                            result.post_id = token

                        else:

                            result.opaque_id = token

                    return result

                # ------------------------------------------------
                # USER REEL
                # ------------------------------------------------

                if action in {
                    "reel",
                    "reels",
                }:

                    result.url_type = "REEL"
                    result.entity_type = "USER"

                    if len(segments) >= 3:

                        token = segments[2]

                        result.object_id = token

                        if is_numeric_id(token):

                            result.video_id = token

                        else:

                            result.opaque_id = token

                    return result

                # ------------------------------------------------
                # USER VIDEO
                # ------------------------------------------------

                if action in {
                    "video",
                    "videos",
                }:

                    result.url_type = "VIDEO"
                    result.entity_type = "USER"

                    if len(segments) >= 3:

                        token = segments[2]

                        result.object_id = token

                        if is_numeric_id(token):

                            result.video_id = token

                        else:

                            result.opaque_id = token

                    return result

                # ------------------------------------------------
                # USER PHOTO
                # ------------------------------------------------

                if action in {
                    "photo",
                    "photos",
                }:

                    result.url_type = "PHOTO"
                    result.entity_type = "USER"

                    if len(segments) >= 3:

                        token = segments[2]

                        result.object_id = token

                        if is_numeric_id(token):

                            result.photo_id = token

                        else:

                            result.opaque_id = token

                    return result

                # ------------------------------------------------
                # USER STORY
                # ------------------------------------------------

                if action in {
                    "story",
                    "stories",
                }:

                    result.url_type = "STORY"
                    result.entity_type = "USER"

                    if len(segments) >= 3:

                        token = segments[2]

                        result.object_id = token

                        if is_numeric_id(token):

                            result.story_id = token

                        else:

                            result.opaque_id = token

                    return result

            # ----------------------------------------------------
            # Plain profile
            # ----------------------------------------------------

            result.url_type = "PROFILE"
            result.entity_type = "USER"

            return result

    return result


# ============================================================
# SNAPSHOT
# ============================================================

@dataclass
class Snapshot:

    requested_url: str

    final_url: str = ""

    status_code: int = 0

    html_text: str = ""

    title: str = ""

    metas: dict[str, str] = field(
        default_factory=dict
    )

    canonical: str = ""

    links: list[str] = field(
        default_factory=list
    )

    json_ld: list[Any] = field(
        default_factory=list
    )


# ============================================================
# EVIDENCE
# ============================================================

@dataclass
class Evidence:

    value: str

    role: str

    source: str

    score: float

    context: str = ""

    object_token: str = ""

    independent: bool = True


class EvidenceStore:

    def __init__(self):

        self.items: list[Evidence] = []

    def add(
        self,
        value: Any,
        role: str,
        source: str,
        score: float,
        context: str = "",
        object_token: str = "",
        independent: bool = True,
    ):

        value = clean_text(value)

        if not value:
            return

        self.items.append(
            Evidence(
                value=value,
                role=role,
                source=source,
                score=score,
                context=truncate(
                    context,
                    700,
                ),
                object_token=object_token,
                independent=independent,
            )
        )

    def values(
        self,
        role: Optional[str] = None,
    ) -> list[str]:

        values = []

        for item in self.items:

            if role and item.role != role:
                continue

            values.append(
                item.value
            )

        return unique_preserve(
            values
        )

    def for_value(
        self,
        value: str,
    ) -> list[Evidence]:

        return [
            item
            for item in self.items
            if item.value == value
        ]

    def count(self) -> int:
        return len(self.items)


# ============================================================
# RESULT
# ============================================================

@dataclass
class ResolveResult:

    original_url: str

    final_url: str = ""

    url_type: str = "UNKNOWN"

    entity_type: str = "UNKNOWN"

    status: str = "NOT_RESOLVED"

    confidence: int = 0

    user_uid: Optional[str] = None

    page_uid: Optional[str] = None

    group_id: Optional[str] = None

    post_id: Optional[str] = None

    video_id: Optional[str] = None

    photo_id: Optional[str] = None

    story_id: Optional[str] = None

    publisher_id: Optional[str] = None

    publisher_type: Optional[str] = None

    username: Optional[str] = None

    title: Optional[str] = None

    opaque_id: Optional[str] = None

    verified: bool = False

    evidence_count: int = 0


# ============================================================
# HTTP
# ============================================================

class PublicHTTP:

    def __init__(self):

        self.semaphore = asyncio.Semaphore(
            HTTP_CONCURRENCY
        )

    async def fetch(
        self,
        url: str,
        timeout: float = REQUEST_TIMEOUT,
    ) -> Optional[Snapshot]:

        url = normalize_input_url(
            url
        )

        if not url:
            return None

        async with self.semaphore:

            last_error = None

            for attempt in range(3):

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

                try:

                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            timeout
                        ),
                        follow_redirects=True,
                        http2=False,
                        headers=headers,
                    ) as client:

                        response = await client.get(
                            url
                        )

                    body = response.content

                    if len(body) > MAX_RESPONSE_BYTES:

                        body = body[
                            :MAX_RESPONSE_BYTES
                        ]

                    content_type = (
                        response.headers.get(
                            "content-type",
                            "application/x-www-form-urlencoded",
                        )
                        .lower()
                    )

                    if (
                        "text/html"
                        not in content_type
                        and not body.lstrip().startswith(
                            b"<"
                        )
                    ):

                        return Snapshot(
                            requested_url=url,
                            final_url=str(
                                response.url
                            ),
                            status_code=response.status_code,
                        )

                    text = body.decode(
                        "utf-8",
                        errors="ignore",
                    )

                    if len(text) > MAX_HTML_CHARS:

                        text = text[
                            :MAX_HTML_CHARS
                        ]

                    return parse_snapshot(
                        requested_url=url,
                        final_url=str(
                            response.url
                        ),
                        status_code=response.status_code,
                        text=text,
                    )

                except Exception as exc:

                    last_error = exc

                    if attempt < 2:

                        await asyncio.sleep(
                            0.6 * (
                                attempt + 1
                            )
                        )

            if last_error:

                logger.debug(
                    "HTTP error %s: %s",
                    url,
                    last_error,
                )

        return None


# ============================================================
# HTML PARSING
# ============================================================

ATTR_RE = re.compile(
    r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)

META_TAG_RE = re.compile(
    r"<meta\b[^>]*>",
    re.IGNORECASE,
)

LINK_TAG_RE = re.compile(
    r"<link\b[^>]*>",
    re.IGNORECASE,
)

TITLE_RE = re.compile(
    r"<title\b[^>]*>(.*?)</title>",
    re.IGNORECASE | re.DOTALL,
)

ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))",
    re.IGNORECASE,
)

JSONLD_RE = re.compile(
    r"""
    <script
        \b[^>]*type\s*=\s*
        ["']application/ld\+json["']
        [^>]*>
        (.*?)
    </script>
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


def parse_attributes(
    tag: str,
) -> dict[str, str]:

    output = {}

    for match in ATTR_RE.finditer(
        tag
    ):

        key = match.group(1).lower()

        value = (
            match.group(2)
            or match.group(3)
            or match.group(4)
            or ""
        )

        output[key] = html.unescape(
            value
        )

    return output


def extract_meta(
    text: str,
) -> dict[str, str]:

    output = {}

    for match in META_TAG_RE.finditer(
        text
    ):

        attrs = parse_attributes(
            match.group(0)
        )

        key = (
            attrs.get("property")
            or attrs.get("name")
            or attrs.get("itemprop")
        )

        value = attrs.get(
            "content"
        )

        if key and value:

            output[
                key.lower()
            ] = clean_text(value)

    return output


def extract_canonical(
    text: str,
    base_url: str,
) -> str:

    for match in LINK_TAG_RE.finditer(
        text
    ):

        attrs = parse_attributes(
            match.group(0)
        )

        rel = attrs.get(
            "rel",
            "",
        ).lower()

        href = attrs.get(
            "href",
            "",
        )

        if (
            href
            and "canonical" in rel
        ):

            return urljoin(
                base_url,
                href,
            )

    return ""


def extract_links(
    text: str,
    base_url: str,
) -> list[str]:

    result = []

    for match in ANCHOR_RE.finditer(
        text
    ):

        href = (
            match.group(1)
            or match.group(2)
            or match.group(3)
            or ""
        )

        if not href:
            continue

        href = html.unescape(
            href
        )

        absolute = urljoin(
            base_url,
            href,
        )

        if is_facebook_url(
            absolute
        ):

            result.append(
                absolute
            )

    # Also raw absolute Facebook URLs.
    for match in re.finditer(
        r"https?://[^\s\"'<>]+",
        text,
        re.IGNORECASE,
    ):

        url = match.group(0).rstrip(
            ".,;)]}"
        )

        if is_facebook_url(url):

            result.append(
                url
            )

    return unique_preserve(
        result
    )[:500]


def extract_title(
    text: str,
    metas: dict[str, str],
) -> str:

    match = TITLE_RE.search(
        text
    )

    if match:

        title = normalize_space(
            match.group(1)
        )

        if title:
            return title

    return (
        metas.get("og:title")
        or metas.get("twitter:title")
        or ""
    )


def extract_json_ld(
    text: str,
) -> list[Any]:

    output = []

    for match in JSONLD_RE.finditer(
        text
    ):

        raw = (
            match.group(1)
            .strip()
        )

        if not raw:
            continue

        try:

            output.append(
                json.loads(raw)
            )

        except Exception:
            continue

    return output


def parse_snapshot(
    requested_url: str,
    final_url: str,
    status_code: int,
    text: str,
) -> Snapshot:

    metas = extract_meta(
        text
    )

    return Snapshot(
        requested_url=requested_url,
        final_url=final_url,
        status_code=status_code,
        html_text=text,
        title=extract_title(
            text,
            metas,
        ),
        metas=metas,
        canonical=extract_canonical(
            text,
            final_url,
        ),
        links=extract_links(
            text,
            final_url,
        ),
        json_ld=extract_json_ld(
            text
        ),
    )


# ============================================================
# JSON WALK
# ============================================================

def walk_json(
    value: Any,
) -> Iterable[dict[str, Any]]:

    if isinstance(
        value,
        dict,
    ):

        yield value

        for child in value.values():

            yield from walk_json(
                child
            )

    elif isinstance(
        value,
        list,
    ):

        for child in value:

            yield from walk_json(
                child
            )


# ============================================================
# ID KEY MAP
# ============================================================

ID_KEY_ROLES = {

    "user_id": "user",
    "userid": "user",
    "userId": "user",
    "userID": "user",

    "profile_id": "profile",
    "profileId": "profile",
    "profileID": "profile",

    "owner_id": "owner",
    "ownerId": "owner",
    "ownerID": "owner",

    "publisher_id": "publisher",
    "publisherId": "publisher",
    "publisherID": "publisher",

    "actor_id": "actor",
    "actorId": "actor",
    "actorID": "actor",

    "page_id": "page",
    "pageId": "page",
    "pageID": "page",

    "group_id": "group",
    "groupId": "group",
    "groupID": "group",

    "entity_id": "entity",
    "entityId": "entity",
    "entityID": "entity",

    "post_id": "post",
    "postId": "post",
    "postID": "post",

    "video_id": "video",
    "videoId": "video",
    "videoID": "video",

    "photo_id": "photo",
    "photoId": "photo",
    "photoID": "photo",

    "story_fbid": "story",
    "story_id": "story",
    "storyId": "story",

    "media_fbid": "media",
    "mediaFbid": "media",

    "fbid": "fbid",
}


# ============================================================
# EXPLICIT ID EXTRACTION
# ============================================================

def extract_explicit_ids(
    text: str,
    store: EvidenceStore,
    object_token: str = "",
):

    if not text:
        return

    keys = "|".join(
        re.escape(k)
        for k in ID_KEY_ROLES
    )

    pattern = re.compile(
        rf"""
        ["']?
        (?P<key>{keys})
        ["']?
        \s*
        :
        \s*
        ["']?
        (?P<value>\d{{5,25}})
        ["']?
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    score_map = {

        "user": 18,
        "profile": 20,
        "owner": 16,
        "publisher": 16,

        "page": 17,
        "group": 17,

        "post": 15,
        "video": 15,
        "photo": 13,
        "story": 13,

        "entity": 9,
        "media": 8,
        "fbid": 7,
        "actor": 8,
    }

    for match in pattern.finditer(
        text
    ):

        key = match.group(
            "key"
        )

        value = match.group(
            "value"
        )

        role = ID_KEY_ROLES.get(
            key,
            "generic",
        )

        start = max(
            0,
            match.start() - 300,
        )

        end = min(
            len(text),
            match.end() + 300,
        )

        context = text[
            start:end
        ]

        store.add(
            value=value,
            role=role,
            source=f"explicit:{key}",
            score=score_map.get(
                role,
                2,
            ),
            context=context,
            object_token=object_token,
        )


# ============================================================
# DATA ATTRIBUTES
# ============================================================

def extract_data_ids(
    text: str,
    store: EvidenceStore,
    object_token: str = "",
):

    pattern = re.compile(
        r"""
        data-
        (?P<key>
            user-id|
            profile-id|
            owner-id|
            publisher-id|
            actor-id|
            page-id|
            group-id|
            entity-id|
            post-id|
            video-id|
            photo-id|
            story-id|
            media-fbid
        )
        \s*=\s*
        ["']
        (?P<value>\d{5,25})
        ["']
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    for match in pattern.finditer(
        text
    ):

        key = match.group(
            "key"
        ).lower()

        value = match.group(
            "value"
        )

        role_map = {
            "user-id": "user",
            "profile-id": "profile",
            "owner-id": "owner",
            "publisher-id": "publisher",
            "actor-id": "actor",
            "page-id": "page",
            "group-id": "group",
            "entity-id": "entity",
            "post-id": "post",
            "video-id": "video",
            "photo-id": "photo",
            "story-id": "story",
            "media-fbid": "media",
        }

        role = role_map.get(
            key,
            "generic",
        )

        store.add(
            value=value,
            role=role,
            source=f"data:{key}",
            score=10,
            context=match.group(0),
            object_token=object_token,
        )


# ============================================================
# PROFILE URL
# ============================================================

def profile_from_url(
    url: str,
) -> tuple[
    Optional[str],
    Optional[str],
    Optional[str],
]:

    try:

        url = normalize_input_url(
            url
        )

        parsed = urlparse(
            url
        )

        segments = [
            unquote(x)
            for x in parsed.path.split("/")
            if x.strip()
        ]

        lower = [
            x.lower()
            for x in segments
        ]

        query = parse_qs(
            parsed.query
        )

        # ----------------------------------------------------
        # /profile.php?id=UID
        # ----------------------------------------------------

        if (
            lower
            and lower[0] == "profile.php"
        ):

            uid = numeric_id(
                first_query_value(
                    query,
                    (
                        "id",
                        "uid",
                        "user_id",
                        "profile_id",
                    ),
                )
            )

            return (
                None,
                uid,
                "USER",
            )

        # ----------------------------------------------------
        # /pages/name/PAGE_ID
        # ----------------------------------------------------

        if (
            lower
            and lower[0] == "pages"
        ):

            page_id = None

            for segment in segments[2:5]:

                if is_numeric_id(segment):

                    page_id = segment
                    break

            return (
                None,
                page_id,
                "PAGE",
            )

        # ----------------------------------------------------
        # /groups/GROUP_ID
        # ----------------------------------------------------

        if (
            lower
            and lower[0] == "groups"
        ):

            group_id = None

            if len(segments) >= 2:

                group_id = numeric_id(
                    segments[1]
                )

            return (
                None,
                group_id,
                "GROUP",
            )

        # ----------------------------------------------------
        # /username
        # ----------------------------------------------------

        if segments:

            first = segments[0]

            if (
                first.lower()
                not in FB_RESERVED_ROUTES
                and not is_numeric_id(first)
            ):

                return (
                    first,
                    None,
                    "USER_OR_PAGE",
                )

    except Exception:
        pass

    return (
        None,
        None,
        None,
    )


# ============================================================
# PROFILE EVIDENCE
# ============================================================

def extract_profile_evidence(
    snapshot: Snapshot,
    store: EvidenceStore,
):

    for url in snapshot.links:

        username, profile_id, entity = (
            profile_from_url(
                url
            )
        )

        if username:

            store.add(
                value=username,
                role="username",
                source="profile:url",
                score=10,
                context=url,
            )

        if profile_id:

            role = (
                "page"
                if entity == "PAGE"
                else
                "group"
                if entity == "GROUP"
                else
                "profile"
            )

            store.add(
                value=profile_id,
                role=role,
                source="profile:url:id",
                score=20,
                context=url,
            )

    # Raw URLs outside anchors.
    for match in re.finditer(
        r"https?://[^\s\"'<>]+",
        snapshot.html_text,
        re.IGNORECASE,
    ):

        url = match.group(0).rstrip(
            ".,;)]}"
        )

        if not is_facebook_url(
            url
        ):
            continue

        username, profile_id, entity = (
            profile_from_url(
                url
            )
        )

        if username:

            store.add(
                value=username,
                role="username",
                source="raw-profile:url",
                score=7,
                context=url,
            )

        if profile_id:

            role = (
                "page"
                if entity == "PAGE"
                else
                "group"
                if entity == "GROUP"
                else
                "profile"
            )

            store.add(
                value=profile_id,
                role=role,
                source="raw-profile:id",
                score=16,
                context=url,
            )


# ============================================================
# META EVIDENCE
# ============================================================

def extract_meta_evidence(
    snapshot: Snapshot,
    store: EvidenceStore,
):

    for key, value in snapshot.metas.items():

        value = clean_text(
            value
        )

        if not value:
            continue

        key = key.lower()

        if key in {
            "og:url",
            "twitter:url",
            "og:see_also",
        }:

            username, profile_id, entity = (
                profile_from_url(
                    value
                )
            )

            if username:

                store.add(
                    value=username,
                    role="username",
                    source=f"meta:{key}",
                    score=11,
                    context=value,
                )

            if profile_id:

                role = (
                    "page"
                    if entity == "PAGE"
                    else
                    "group"
                    if entity == "GROUP"
                    else
                    "profile"
                )

                store.add(
                    value=profile_id,
                    role=role,
                    source=f"meta:{key}:id",
                    score=20,
                    context=value,
                )

        elif key in {
            "og:title",
            "twitter:title",
        }:

            store.add(
                value=value,
                role="title",
                source=f"meta:{key}",
                score=3,
                context=value,
            )


# ============================================================
# JSON-LD EVIDENCE
# ============================================================

def extract_jsonld_evidence(
    snapshot: Snapshot,
    store: EvidenceStore,
):

    for data in snapshot.json_ld:

        for obj in walk_json(
            data
        ):

            for key, value in obj.items():

                key_lower = (
                    str(key).lower()
                )

                if key_lower == "author":

                    authors = (
                        value
                        if isinstance(
                            value,
                            list,
                        )
                        else [value]
                    )

                    for author in authors:

                        if not isinstance(
                            author,
                            dict,
                        ):
                            continue

                        name = clean_text(
                            author.get(
                                "name"
                            )
                        )

                        url = clean_text(
                            author.get(
                                "url"
                            )
                        )

                        if name:

                            store.add(
                                value=name,
                                role="publisher_name",
                                source="jsonld:author:name",
                                score=7,
                                context=name,
                            )

                        if url:

                            username, profile_id, entity = (
                                profile_from_url(
                                    url
                                )
                            )

                            if username:

                                store.add(
                                    value=username,
                                    role="username",
                                    source="jsonld:author:url",
                                    score=12,
                                    context=url,
                                )

                            if profile_id:

                                role = (
                                    "page"
                                    if entity == "PAGE"
                                    else
                                    "profile"
                                )

                                store.add(
                                    value=profile_id,
                                    role=role,
                                    source="jsonld:author:id",
                                    score=21,
                                    context=url,
                                )

                elif key_lower == "url":

                    url = clean_text(
                        value
                    )

                    if not is_facebook_url(
                        url
                    ):
                        continue

                    username, profile_id, entity = (
                        profile_from_url(
                            url
                        )
                    )

                    if username:

                        store.add(
                            value=username,
                            role="username",
                            source="jsonld:url",
                            score=8,
                            context=url,
                        )

                    if profile_id:

                        role = (
                            "page"
                            if entity == "PAGE"
                            else
                            "group"
                            if entity == "GROUP"
                            else
                            "profile"
                        )

                        store.add(
                            value=profile_id,
                            role=role,
                            source="jsonld:url:id",
                            score=18,
                            context=url,
                        )


# ============================================================
# PFBID / OPAQUE CORRELATION
# ============================================================

def decode_opaque_base64(
    token: str,
) -> list[str]:

    """
    Very conservative.

    pfbid is NOT assumed to be a normal Base64 UID.

    Decoding is only auxiliary evidence.
    """

    token = clean_text(
        token
    )

    if len(token) < 8:
        return []

    candidates = [
        token,
        token.replace(
            "-",
            "+",
        ).replace(
            "_",
            "/",
        ),
    ]

    output = []

    for value in candidates:

        try:

            padding = "=" * (
                (-len(value)) % 4
            )

            raw = base64.b64decode(
                value + padding,
                validate=False,
            )

            decoded = raw.decode(
                "utf-8",
                errors="ignore",
            )

            decoded = clean_text(
                decoded
            )

            if decoded:
                output.append(
                    decoded
                )

        except (
            ValueError,
            binascii.Error,
        ):
            continue

    return unique_preserve(
        output
    )


def correlate_opaque_token(
    text: str,
    token: str,
    store: EvidenceStore,
):

    if not text or not token:
        return

    text_lower = text.lower()
    token_lower = token.lower()

    positions = []

    start = 0

    while True:

        pos = text_lower.find(
            token_lower,
            start,
        )

        if pos < 0:
            break

        positions.append(
            pos
        )

        start = (
            pos
            + len(token)
        )

        if len(positions) >= 20:
            break

    for position in positions:

        left = max(
            0,
            position - MAX_OBJECT_SCAN,
        )

        right = min(
            len(text),
            position
            + len(token)
            + MAX_OBJECT_SCAN,
        )

        window = text[
            left:right
        ]

        # -----------------------------------------------
        # Explicit numeric IDs in same object context.
        # -----------------------------------------------

        extract_explicit_ids(
            window,
            store,
            object_token=token,
        )

        extract_data_ids(
            window,
            store,
            object_token=token,
        )

        # -----------------------------------------------
        # Profile URLs in same context.
        # -----------------------------------------------

        for match in re.finditer(
            r"https?://[^\s\"'<>]+",
            window,
            re.IGNORECASE,
        ):

            url = match.group(0).rstrip(
                ".,;)]}"
            )

            if not is_facebook_url(
                url
            ):
                continue

            username, profile_id, entity = (
                profile_from_url(
                    url
                )
            )

            if username:

                store.add(
                    value=username,
                    role="username",
                    source="opaque:profile-url",
                    score=13,
                    context=url,
                    object_token=token,
                )

            if profile_id:

                role = (
                    "page"
                    if entity == "PAGE"
                    else
                    "group"
                    if entity == "GROUP"
                    else
                    "profile"
                )

                store.add(
                    value=profile_id,
                    role=role,
                    source="opaque:profile-id",
                    score=23,
                    context=url,
                    object_token=token,
                )

        # -----------------------------------------------
        # Conservative decoded fragments.
        # -----------------------------------------------

        for decoded in decode_opaque_base64(
            token
        ):

            for number in NUMERIC_ID_RE.findall(
                decoded
            ):

                store.add(
                    value=number,
                    role="decoded",
                    source="opaque:decoded",
                    score=1,
                    context=decoded,
                    object_token=token,
                    independent=False,
                )


# ============================================================
# OBJECT TOKENS
# ============================================================

def extract_opaque_tokens(
    text: str,
) -> list[str]:

    return unique_preserve(
        PFBID_RE.findall(
            text
        )
    )


# ============================================================
# RESULT OBJECT SELECTION
# ============================================================

def best_numeric_role(
    store: EvidenceStore,
    role: str,
) -> Optional[str]:

    scores: dict[str, float] = {}

    for item in store.items:

        if item.role != role:
            continue

        value = numeric_id(
            item.value
        )

        if not value:
            continue

        scores.setdefault(
            value,
            0,
        )

        scores[value] += (
            item.score
        )

    if not scores:
        return None

    return max(
        scores,
        key=scores.get,
    )


def best_username(
    store: EvidenceStore,
) -> Optional[str]:

    scores: dict[str, float] = {}

    for item in store.items:

        if item.role != "username":
            continue

        value = clean_text(
            item.value
        )

        if not value:
            continue

        if (
            value.lower()
            in FB_RESERVED_ROUTES
        ):
            continue

        scores.setdefault(
            value,
            0,
        )

        scores[value] += (
            item.score
        )

    if not scores:
        return None

    return max(
        scores,
        key=scores.get,
    )


# ============================================================
# UID CANDIDATES
# ============================================================

def build_uid_candidates(
    store: EvidenceStore,
) -> dict[str, dict[str, Any]]:

    candidates = {}

    allowed = {
        "user",
        "profile",
        "owner",
        "publisher",
        "actor",
        "decoded",
    }

    for item in store.items:

        if item.role not in allowed:
            continue

        value = numeric_id(
            item.value
        )

        if not value:
            continue

        candidate = candidates.setdefault(
            value,
            {
                "score": 0.0,
                "roles": set(),
                "sources": set(),
                "contexts": [],
                "strong": 0,
            },
        )

        candidate["score"] += (
            item.score
        )

        candidate["roles"].add(
            item.role
        )

        candidate["sources"].add(
            item.source
        )

        if item.context:

            candidate["contexts"].append(
                item.context
            )

        if item.role in {
            "user",
            "profile",
            "owner",
            "publisher",
        }:

            candidate["strong"] += 1

    return candidates


# ============================================================
# RESOLVER
# ============================================================

class FacebookResolver:

    def __init__(self):

        self.http = PublicHTTP()

    async def resolve(
        self,
        url: str,
    ) -> ResolveResult:

        parsed = classify_facebook_url(
            url
        )

        result = ResolveResult(
            original_url=url,
            final_url=parsed.normalized,
            url_type=parsed.url_type,
            entity_type=parsed.entity_type,
            opaque_id=parsed.opaque_id,
        )

        store = EvidenceStore()

        # ====================================================
        # URL STRUCTURAL EVIDENCE
        # ====================================================

        if parsed.profile_id:

            store.add(
                parsed.profile_id,
                "profile",
                "url:profile_id",
                40,
                parsed.normalized,
            )

        if parsed.page_id:

            store.add(
                parsed.page_id,
                "page",
                "url:page_id",
                40,
                parsed.normalized,
            )

        if parsed.group_id:

            store.add(
                parsed.group_id,
                "group",
                "url:group_id",
                40,
                parsed.normalized,
            )

        if parsed.post_id:

            store.add(
                parsed.post_id,
                "post",
                "url:post_id",
                40,
                parsed.normalized,
            )

        if parsed.video_id:

            store.add(
                parsed.video_id,
                "video",
                "url:video_id",
                40,
                parsed.normalized,
            )

        if parsed.photo_id:

            store.add(
                parsed.photo_id,
                "photo",
                "url:photo_id",
                40,
                parsed.normalized,
            )

        if parsed.story_id:

            store.add(
                parsed.story_id,
                "story",
                "url:story_id",
                40,
                parsed.normalized,
            )

        if parsed.username:

            store.add(
                parsed.username,
                "username",
                "url:username",
                30,
                parsed.normalized,
            )

        # ====================================================
        # FIRST FETCH
        # ====================================================

        snapshot = await self.http.fetch(
            parsed.normalized
        )

        if snapshot is None:

            result.status = "NOT_RESOLVED"

            return result

        result.final_url = (
            snapshot.final_url
            or parsed.normalized
        )

        # ====================================================
        # FINAL REDIRECT CLASSIFICATION
        # ====================================================

        final_parsed = classify_facebook_url(
            result.final_url
        )

        # Original structural type has priority.
        #
        # A USER_POST redirecting to a profile must not
        # suddenly become PROFILE.
        #
        if parsed.url_type in {
            "UNKNOWN",
            "SHARE",
        }:

            if final_parsed.url_type != "UNKNOWN":

                result.url_type = (
                    final_parsed.url_type
                )

        # ====================================================
        # OBJECT TOKEN
        # ====================================================

        object_token = (
            parsed.opaque_id
            or parsed.object_id
            or ""
        )

        if (
            not object_token
            and final_parsed.opaque_id
        ):

            object_token = (
                final_parsed.opaque_id
            )

        result.opaque_id = (
            object_token
            or None
        )

        # ====================================================
        # PAGE / HTML EVIDENCE
        # ====================================================

        extract_meta_evidence(
            snapshot,
            store,
        )

        extract_profile_evidence(
            snapshot,
            store,
        )

        extract_jsonld_evidence(
            snapshot,
            store,
        )

        extract_explicit_ids(
            snapshot.html_text,
            store,
            object_token=object_token,
        )

        extract_data_ids(
            snapshot.html_text,
            store,
            object_token=object_token,
        )

        # ====================================================
        # PFBID CORRELATION
        # ====================================================

        tokens = unique_preserve(
            [
                object_token,
                *extract_opaque_tokens(
                    snapshot.html_text
                ),
            ]
        )

        for token in tokens[:30]:

            correlate_opaque_token(
                snapshot.html_text,
                token,
                store,
            )

        # ====================================================
        # PROFILE CRAWL
        # ====================================================

        crawl_urls = []

        if snapshot.canonical:

            crawl_urls.append(
                snapshot.canonical
            )

        if parsed.username:

            crawl_urls.append(
                "https://www.facebook.com/"
                + parsed.username
            )

        discovered_usernames = [
            x
            for x in store.values(
                "username"
            )
            if (
                x.lower()
                not in FB_RESERVED_ROUTES
            )
            and " " not in x
        ]

        for username in discovered_usernames:

            crawl_urls.append(
                "https://www.facebook.com/"
                + username
            )

        crawl_urls = unique_preserve(
            crawl_urls
        )[:MAX_PROFILE_CRAWLS]

        # ====================================================
        # FETCH PROFILES
        # ====================================================

        if crawl_urls:

            fetched = await asyncio.gather(
                *[
                    self.http.fetch(
                        u,
                        timeout=PROFILE_TIMEOUT,
                    )
                    for u in crawl_urls
                ],
                return_exceptions=True,
            )

            for profile_snapshot in fetched:

                if not isinstance(
                    profile_snapshot,
                    Snapshot,
                ):
                    continue

                extract_meta_evidence(
                    profile_snapshot,
                    store,
                )

                extract_profile_evidence(
                    profile_snapshot,
                    store,
                )

                extract_jsonld_evidence(
                    profile_snapshot,
                    store,
                )

                extract_explicit_ids(
                    profile_snapshot.html_text,
                    store,
                )

                extract_data_ids(
                    profile_snapshot.html_text,
                    store,
                )

        # ====================================================
        # ENTITY
        # ====================================================

        result.entity_type = (
            self.infer_entity(
                parsed,
                store,
            )
        )

        # ====================================================
        # OBJECT IDs
        # ====================================================

        self.collect_objects(
            parsed,
            store,
            result,
        )

        # ====================================================
        # PUBLISHER
        # ====================================================

        publisher_type, publisher_id = (
            self.infer_publisher(
                parsed,
                result.entity_type,
                store,
            )
        )

        result.publisher_type = (
            publisher_type
        )

        result.publisher_id = (
            publisher_id
        )

        # ====================================================
        # USERNAME
        # ====================================================

        result.username = (
            parsed.username
            or best_username(store)
        )

        # ====================================================
        # TITLE
        # ====================================================

        if snapshot.title:

            generic_titles = {
                "facebook",
                "facebook - log in or sign up",
                "log in or sign up",
            }

            if (
                snapshot.title.lower()
                not in generic_titles
            ):

                result.title = (
                    snapshot.title
                )

        # ====================================================
        # PAGE ID
        # ====================================================

        if result.entity_type == "PAGE":

            result.page_uid = (
                parsed.page_id
                or best_numeric_role(
                    store,
                    "page",
                )
            )

        # ====================================================
        # GROUP ID
        # ====================================================

        if result.entity_type == "GROUP":

            result.group_id = (
                parsed.group_id
                or best_numeric_role(
                    store,
                    "group",
                )
            )

        # ====================================================
        # USER UID
        # ====================================================

        result.user_uid = (
            self.select_user_uid(
                parsed=parsed,
                entity_type=result.entity_type,
                publisher_id=publisher_id,
                store=store,
                object_token=object_token,
            )
        )

        # ====================================================
        # CONFIDENCE
        # ====================================================

        result.evidence_count = (
            store.count()
        )

        result.confidence = (
            self.calculate_confidence(
                result,
                store,
            )
        )

        result.verified = (
            result.confidence >= 85
            and (
                result.user_uid
                or result.page_uid
                or result.group_id
                or result.post_id
                or result.video_id
                or result.photo_id
                or result.story_id
            )
            is not None
        )

        if result.verified:

            result.status = "VERIFIED"

        elif result.confidence >= 55:

            result.status = "RESOLVED"

        else:

            result.status = "NOT_RESOLVED"

        return result

    # ========================================================
    # ENTITY
    # ========================================================

    def infer_entity(
        self,
        parsed: ParsedURL,
        store: EvidenceStore,
    ) -> str:

        # Structural URL always wins for groups/pages.

        if parsed.url_type in {
            "GROUP",
            "GROUP_POST",
        }:

            return "GROUP"

        if parsed.url_type in {
            "PAGE",
            "PAGE_POST",
        }:

            return "PAGE"

        # Strong group evidence.
        if store.values("group"):

            if parsed.url_type not in {
                "PROFILE",
                "USER_POST",
            }:

                return "GROUP"

        # Strong page evidence.
        if store.values("page"):

            if parsed.url_type not in {
                "PROFILE",
            }:

                return "PAGE"

        if parsed.url_type in {
            "PROFILE",
            "USER_POST",
        }:

            return "USER"

        # For generic object URLs, publisher evidence decides.

        if store.values("page"):

            return "PAGE"

        if store.values("group"):

            return "GROUP"

        if store.values(
            "user"
        ) or store.values(
            "profile"
        ):

            return "USER"

        return (
            parsed.entity_type
            if parsed.entity_type
            else "UNKNOWN"
        )

    # ========================================================
    # OBJECTS
    # ========================================================

    def collect_objects(
        self,
        parsed: ParsedURL,
        store: EvidenceStore,
        result: ResolveResult,
    ):

        # URL IDs first.

        if parsed.post_id:

            result.post_id = (
                parsed.post_id
            )

        if parsed.video_id:

            result.video_id = (
                parsed.video_id
            )

        if parsed.photo_id:

            result.photo_id = (
                parsed.photo_id
            )

        if parsed.story_id:

            result.story_id = (
                parsed.story_id
            )

        # HTML explicit numeric IDs second.

        if not result.post_id:

            result.post_id = (
                best_numeric_role(
                    store,
                    "post",
                )
            )

        if not result.video_id:

            result.video_id = (
                best_numeric_role(
                    store,
                    "video",
                )
            )

        if not result.photo_id:

            result.photo_id = (
                best_numeric_role(
                    store,
                    "photo",
                )
            )

        if not result.story_id:

            result.story_id = (
                best_numeric_role(
                    store,
                    "story",
                )
            )

        # Opaque token is ONLY fallback for object display.

        if (
            not result.post_id
            and parsed.opaque_id
            and parsed.url_type
            in {
                "POST",
                "USER_POST",
                "PAGE_POST",
                "GROUP_POST",
            }
        ):

            result.post_id = (
                parsed.opaque_id
            )

        if (
            not result.video_id
            and parsed.opaque_id
            and parsed.url_type
            in {
                "VIDEO",
                "REEL",
            }
        ):

            result.video_id = (
                parsed.opaque_id
            )

    # ========================================================
    # PUBLISHER
    # ========================================================

    def infer_publisher(
        self,
        parsed: ParsedURL,
        entity_type: str,
        store: EvidenceStore,
    ) -> tuple[
        Optional[str],
        Optional[str],
    ]:

        # ----------------------------------------------------
        # GROUP
        # ----------------------------------------------------

        if entity_type == "GROUP":

            # group ID is NOT publisher user ID.

            publisher = (
                best_numeric_role(
                    store,
                    "user",
                )
                or best_numeric_role(
                    store,
                    "profile",
                )
                or best_numeric_role(
                    store,
                    "owner",
                )
                or best_numeric_role(
                    store,
                    "publisher",
                )
            )

            if publisher:

                return (
                    "USER",
                    publisher,
                )

            return (
                None,
                None,
            )

        # ----------------------------------------------------
        # PAGE
        # ----------------------------------------------------

        if entity_type == "PAGE":

            page_id = (
                parsed.page_id
                or best_numeric_role(
                    store,
                    "page",
                )
            )

            if page_id:

                return (
                    "PAGE",
                    page_id,
                )

            return (
                None,
                None,
            )

        # ----------------------------------------------------
        # USER
        # ----------------------------------------------------

        if entity_type == "USER":

            publisher = (
                best_numeric_role(
                    store,
                    "user",
                )
                or best_numeric_role(
                    store,
                    "profile",
                )
                or best_numeric_role(
                    store,
                    "owner",
                )
                or best_numeric_role(
                    store,
                    "publisher",
                )
            )

            if publisher:

                return (
                    "USER",
                    publisher,
                )

        return (
            None,
            None,
        )

    # ========================================================
    # USER UID
    # ========================================================

    def select_user_uid(
        self,
        parsed: ParsedURL,
        entity_type: str,
        publisher_id: Optional[str],
        store: EvidenceStore,
        object_token: str,
    ) -> Optional[str]:

        # ====================================================
        # HARD SAFETY:
        # PAGE/GROUP object does not automatically produce UID.
        # ====================================================

        if entity_type == "PAGE":

            # Only return a user UID if explicit independent
            # user evidence exists.
            pass

        if entity_type == "GROUP":

            # Same rule.
            pass

        # ====================================================
        # BUILD CANDIDATES
        # ====================================================

        candidates = (
            build_uid_candidates(
                store
            )
        )

        if not candidates:
            return None

        # ====================================================
        # EXCLUDE PAGE / GROUP IDs
        # ====================================================

        page_ids = set(
            store.values(
                "page"
            )
        )

        group_ids = set(
            store.values(
                "group"
            )
        )

        for value in list(
            candidates
        ):

            candidate = candidates[
                value
            ]

            roles = candidate[
                "roles"
            ]

            strong_user_role = bool(
                roles
                & {
                    "user",
                    "profile",
                    "owner",
                    "publisher",
                }
            )

            if (
                value in page_ids
                and not strong_user_role
            ):

                del candidates[
                    value
                ]

                continue

            if (
                value in group_ids
                and not strong_user_role
            ):

                del candidates[
                    value
                ]

                continue

        if not candidates:
            return None

        # ====================================================
        # SCORE
        # ====================================================

        for value, candidate in candidates.items():

            roles = candidate[
                "roles"
            ]

            sources = candidate[
                "sources"
            ]

            # Independent source bonus.
            candidate["score"] += min(
                len(sources) * 5,
                30,
            )

            # Strong explicit user/profile evidence.
            if "user" in roles:
                candidate["score"] += 20

            if "profile" in roles:
                candidate["score"] += 22

            if "owner" in roles:
                candidate["score"] += 16

            if "publisher" in roles:
                candidate["score"] += 16

            # actor_id alone is weak.
            if (
                roles == {"actor"}
            ):

                candidate["score"] -= 15

            # decoded Base64 alone is extremely weak.
            if (
                roles == {"decoded"}
            ):

                candidate["score"] -= 25

            # Publisher equality bonus.
            if (
                publisher_id
                and value
                == publisher_id
            ):

                candidate["score"] += 20

        ranked = sorted(
            candidates.items(),
            key=lambda x: x[1]["score"],
            reverse=True,
        )

        if not ranked:
            return None

        best_value, best = ranked[0]

        # ====================================================
        # AMBIGUITY PROTECTION
        # ====================================================

        if len(ranked) >= 2:

            second_value, second = ranked[1]

            if (
                best["score"]
                - second["score"]
                < 10
            ):

                return None

        # ====================================================
        # REQUIRE REAL USER EVIDENCE
        # ====================================================

        strong_roles = (
            best["roles"]
            & {
                "user",
                "profile",
                "owner",
                "publisher",
            }
        )

        if not strong_roles:

            return None

        # ====================================================
        # THRESHOLD
        # ====================================================

        if best["score"] < 45:

            return None

        # ====================================================
        # USER ROUTES CAN ACCEPT LOWER THRESHOLD
        # ====================================================

        if parsed.url_type in {
            "PROFILE",
            "USER_POST",
        }:

            if best["score"] >= 40:

                return best_value

        # ====================================================
        # GROUP/PAGE POSTS REQUIRE HIGH CONFIDENCE
        # ====================================================

        if parsed.url_type in {
            "GROUP_POST",
            "PAGE_POST",
        }:

            if best["score"] >= 55:

                return best_value

            return None

        # Generic object.
        if best["score"] >= 55:

            return best_value

        return None

    # ========================================================
    # CONFIDENCE
    # ========================================================

    def calculate_confidence(
        self,
        result: ResolveResult,
        store: EvidenceStore,
    ) -> int:

        score = 0

        # URL resolved.
        if result.final_url:

            score += 10

        # Object structure.
        if result.post_id:

            score += 15

        if result.video_id:

            score += 15

        if result.photo_id:

            score += 13

        if result.story_id:

            score += 13

        # Entity.
        if result.page_uid:

            score += 20

        if result.group_id:

            score += 20

        # Publisher.
        if result.publisher_id:

            score += 15

        # Username.
        if result.username:

            score += 10

        # User UID.
        if result.user_uid:

            score += 30

        # Independent sources.
        sources = {
            item.source
            for item in store.items
            if item.independent
        }

        score += min(
            len(sources) * 2,
            15,
        )

        # Strong user evidence.
        if result.user_uid:

            strong = 0

            for item in store.items:

                if (
                    item.value
                    == result.user_uid
                    and item.role
                    in {
                        "user",
                        "profile",
                        "owner",
                        "publisher",
                    }
                ):

                    strong += 1

            score += min(
                strong * 5,
                15,
            )

        return max(
            0,
            min(
                99,
                score,
            ),
        )


# ============================================================
# PUBLIC RESOLVER API
# ============================================================

async def resolve_facebook_url(
    url: str,
) -> ResolveResult:

    resolver = FacebookResolver()

    return await resolver.resolve(
        url
    )


async def resolve_facebook_urls(
    urls: list[str],
) -> list[ResolveResult]:

    resolver = FacebookResolver()

    results = await asyncio.gather(
        *[
            resolver.resolve(url)
            for url in urls
        ],
        return_exceptions=True,
    )

    output = []

    for index, result in enumerate(
        results
    ):

        if isinstance(
            result,
            ResolveResult,
        ):

            output.append(
                result
            )

        else:

            logger.error(
                "Resolver error #%s: %r",
                index + 1,
                result,
            )

            output.append(
                ResolveResult(
                    original_url=urls[index],
                    status="NOT_RESOLVED",
                )
            )

    return output


# ============================================================
# DISPLAY LABEL
# ============================================================

def display_entity_label(
    result: ResolveResult,
) -> str:

    if result.entity_type == "USER":

        if result.url_type in {
            "USER_POST",
            "POST",
        }:

            return "👤 USER POST"

        if result.url_type == "REEL":

            return "🎬 USER REEL"

        if result.url_type == "VIDEO":

            return "🎬 USER VIDEO"

        if result.url_type == "PHOTO":

            return "🖼 USER PHOTO"

        if result.url_type == "STORY":

            return "⭕ USER STORY"

        return "👤 USER"

    if result.entity_type == "PAGE":

        if result.url_type == "PAGE_POST":

            return "📄 PAGE POST"

        return "📄 PAGE"

    if result.entity_type == "GROUP":

        if result.url_type == "GROUP_POST":

            return "👥 GROUP POST"

        return "👥 GROUP"

    if result.url_type == "REEL":

        return "🎬 REEL"

    if result.url_type == "VIDEO":

        return "🎬 VIDEO"

    if result.url_type == "PHOTO":

        return "🖼 PHOTO"

    if result.url_type == "STORY":

        return "⭕ STORY"

    if result.url_type == "POST":

        return "📝 POST"

    return "🔎 FACEBOOK OBJECT"


# ============================================================
# RESULT FORMAT
# ============================================================

def format_result(
    result: ResolveResult,
    index: int = 1,
) -> str:

    lines = []

    lines.append(
        f"<b>{index}. "
        f"{tg_escape(display_entity_label(result))}"
        f"</b>"
    )

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    if result.status == "VERIFIED":

        lines.append(
            "📊 <b>STATUS:</b> VERIFIED"
        )

    elif result.status == "RESOLVED":

        lines.append(
            "📊 <b>STATUS:</b> RESOLVED"
        )

    else:

        lines.append(
            "📊 <b>STATUS:</b> NOT RESOLVED"
        )

    # --------------------------------------------------------
    # USER UID
    # --------------------------------------------------------

    if result.user_uid:

        lines.append(
            "🆔 <b>USER UID:</b> "
            f"<code>{tg_escape(result.user_uid)}</code>"
        )

    # --------------------------------------------------------
    # PAGE UID
    # --------------------------------------------------------

    if result.page_uid:

        lines.append(
            "📄 <b>PAGE UID:</b> "
            f"<code>{tg_escape(result.page_uid)}</code>"
        )

    # --------------------------------------------------------
    # GROUP
    # --------------------------------------------------------

    if result.group_id:

        lines.append(
            "👥 <b>GROUP ID:</b> "
            f"<code>{tg_escape(result.group_id)}</code>"
        )

    # --------------------------------------------------------
    # POST
    # --------------------------------------------------------

    if result.post_id:

        lines.append(
            "📝 <b>POST ID:</b> "
            f"<code>{tg_escape(result.post_id)}</code>"
        )

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    if result.video_id:

        lines.append(
            "🎬 <b>VIDEO ID:</b> "
            f"<code>{tg_escape(result.video_id)}</code>"
        )

    # --------------------------------------------------------
    # PHOTO
    # --------------------------------------------------------

    if result.photo_id:

        lines.append(
            "🖼 <b>PHOTO ID:</b> "
            f"<code>{tg_escape(result.photo_id)}</code>"
        )

    # --------------------------------------------------------
    # STORY
    # --------------------------------------------------------

    if result.story_id:

        lines.append(
            "⭕ <b>STORY ID:</b> "
            f"<code>{tg_escape(result.story_id)}</code>"
        )

    # --------------------------------------------------------
    # PUBLISHER
    # --------------------------------------------------------

    if result.publisher_id:

        if (
            not result.user_uid
            or result.publisher_id
            != result.user_uid
        ):

            if (
                result.publisher_type
                == "PAGE"
            ):

                label = "PAGE ID"

            else:

                label = "PUBLISHER ID"

            lines.append(
                f"👤 <b>{label}:</b> "
                f"<code>{tg_escape(result.publisher_id)}</code>"
            )

    # --------------------------------------------------------
    # PUBLISHER USERNAME
    # --------------------------------------------------------

    if result.username:

        lines.append(
            "👤 <b>PUBLISHER:</b> "
            f"{tg_escape(result.username)}"
        )

    # --------------------------------------------------------
    # CONFIDENCE
    # --------------------------------------------------------

    lines.append(
        "🎯 <b>CONFIDENCE:</b> "
        f"{result.confidence}%"
    )

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    lines.append(
        "🔗 <b>URL:</b> "
        f'<a href="{html.escape(result.original_url, quote=True)}">'
        f"{tg_escape(truncate(result.original_url, 700))}"
        "</a>"
    )

    return "\n".join(
        lines
    )


# ============================================================
# INTERACTIVE WAIT
# ============================================================

async def wait_for_user_message(
    bot,
    user_id: int,
    chat_id: int,
    timeout: int = INTERACTIVE_TIMEOUT,
):

    loop = asyncio.get_running_loop()

    future = loop.create_future()

    async def temporary_handler(
        event,
    ):

        if event.sender_id != user_id:
            return

        if event.chat_id != chat_id:
            return

        if not future.done():

            future.set_result(
                event
            )

    event_builder = events.NewMessage(
        chats=chat_id
    )

    bot.add_event_handler(
        temporary_handler,
        event_builder,
    )

    try:

        return await asyncio.wait_for(
            future,
            timeout=timeout,
        )

    finally:

        try:

            bot.remove_event_handler(
                temporary_handler,
                event_builder,
            )

        except Exception:

            try:
                bot.remove_event_handler(
                    temporary_handler
                )
            except Exception:
                pass


# ============================================================
# MAIN TELEGRAM HANDLER
# ============================================================

async def _handle_getuidfb(
    event,
):

    user_id = event.sender_id
    chat_id = event.chat_id

    if (
        user_id is None
        or chat_id is None
    ):

        return

    # ========================================================
    # START SESSION
    # ========================================================

    await event.respond(
        "🔎 <b>FACEBOOK UID / ENTITY RESOLVER V21</b>\n\n"
        "📎 Gửi URL Facebook để phân tích.\n\n"
        "Có thể gửi:\n"
        "• 1 URL\n"
        "• nhiều URL cùng tin nhắn\n"
        "• URL cách nhau bằng dấu cách\n"
        "• URL cách nhau bằng xuống dòng\n"
        "• nhiều URL bị dính liền nhau\n\n"
        "🤖 Bot sẽ tự tách từng URL, đếm số lượng "
        "và xử lý từng link riêng.\n\n"
        "🛑 <code>/stop</code> để thoát.",
        parse_mode="html",
    )

    # ========================================================
    # CONTINUOUS LOOP
    # ========================================================

    while True:

        try:

            incoming = await wait_for_user_message(
                bot=event.client,
                user_id=user_id,
                chat_id=chat_id,
            )

        except asyncio.TimeoutError:

            await event.respond(
                "⏱ <b>GETUIDFB</b>\n"
                "Phiên nhập URL đã hết thời gian chờ.",
                parse_mode="html",
            )

            return

        except asyncio.CancelledError:

            return

        except Exception as exc:

            logger.exception(
                "GETUIDFB input error"
            )

            try:

                await event.respond(
                    "❌ GETUIDFB ERROR\n"
                    f"<code>{tg_escape(str(exc))}</code>",
                    parse_mode="html",
                )

            except Exception:
                pass

            return

        text = (
            incoming.raw_text
            or ""
        ).strip()

        if not text:
            continue

        # ====================================================
        # STOP
        # ====================================================

        if re.fullmatch(
            r"/stop(?:@\w+)?",
            text,
            re.IGNORECASE,
        ):

            await incoming.respond(
                "🛑 <b>GETUIDFB</b>\n"
                "Đã thoát chế độ nhập URL.",
                parse_mode="html",
            )

            return

        # ====================================================
        # DON'T START ANOTHER SESSION
        # ====================================================

        if re.fullmatch(
            r"/getuidfb(?:@\w+)?",
            text,
            re.IGNORECASE,
        ):

            await incoming.respond(
                "ℹ️ <b>GETUIDFB đang hoạt động.</b>\n\n"
                "Chỉ cần gửi URL Facebook.",
                parse_mode="html",
            )

            continue

        # ====================================================
        # EXTRACT URLS
        # ====================================================

        urls = extract_facebook_urls(
            text
        )

        if not urls:

            await incoming.respond(
                "❌ Không tìm thấy URL Facebook.\n\n"
                "Ví dụ:\n"
                "<code>https://www.facebook.com/username/posts/123456</code>",
                parse_mode="html",
            )

            continue

        # ====================================================
        # COUNT
        # ====================================================

        count = len(urls)

        await incoming.respond(
            f"🔎 <b>ĐÃ NHẬN {count} URL</b>\n"
            "⚙️ Đang phân tích từng URL...",
            parse_mode="html",
        )

        # ====================================================
        # RESOLVE
        # ====================================================

        results = await resolve_facebook_urls(
            urls
        )

        # ====================================================
        # SEND EACH RESULT SEPARATELY
        # ========================================================

        for index, result in enumerate(
            results,
            1,
        ):

            try:

                await incoming.respond(
                    format_result(
                        result,
                        index=index,
                    ),
                    parse_mode="html",
                    link_preview=False,
                )

            except Exception:

                try:

                    fallback = (
                        f"{index}. "
                        f"{display_entity_label(result)}\n"
                        f"STATUS: {result.status}\n"
                        f"USER UID: "
                        f"{result.user_uid or 'N/A'}\n"
                        f"PAGE UID: "
                        f"{result.page_uid or 'N/A'}\n"
                        f"GROUP ID: "
                        f"{result.group_id or 'N/A'}\n"
                        f"POST ID: "
                        f"{result.post_id or 'N/A'}\n"
                        f"VIDEO ID: "
                        f"{result.video_id or 'N/A'}\n"
                        f"CONFIDENCE: "
                        f"{result.confidence}%\n"
                        f"URL: "
                        f"{result.original_url}"
                    )

                    await incoming.respond(
                        fallback,
                        link_preview=False,
                    )

                except Exception:

                    logger.exception(
                        "Failed to send result #%s",
                        index,
                    )

        # ====================================================
        # CONTINUE
        # ====================================================

        await incoming.respond(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📎 <b>GETUIDFB ĐANG CHỜ URL TIẾP</b>\n"
            "Gửi URL khác để phân tích tiếp.\n"
            "🛑 <code>/stop</code> để thoát.",
            parse_mode="html",
        )


# ============================================================
# REGISTER
# ============================================================

def register(
    bot,
    notify_bot=None,
):
    """
    EXACTLY compatible with:

        module.register(
            bot,
            notify_bot
        )

    No callback wrapper.
    No callback(event).
    Telethon automatically supplies event.
    """

    bot.add_event_handler(
        _handle_getuidfb,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?$",
        ),
    )

    logger.info(
        "✅ Registered /getuidfb V21"
    )


# ============================================================
# COMPATIBILITY ALIAS
# ============================================================

handler = _handle_getuidfb