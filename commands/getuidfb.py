#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
====================================================================
 FACEBOOK UID / ENTITY RESOLVER V22 PRECISION
 TELEGRAM BOT - TELETHON
====================================================================

HTTP ONLY
PUBLIC CONTENT ONLY

KHÔNG:
- Playwright
- Selenium
- Cookie Facebook
- Facebook Access Token
- Facebook Login

CORE:
- USER / PAGE / GROUP
- USER_POST / PAGE_POST / GROUP_POST
- POST / REEL / VIDEO / PHOTO / STORY
- /share/p/...
- /share/v/...
- /share/r/...
- /share/s/...
- /pfbid...
- media_fbid
- actor_id
- profile_id
- user_id
- owner_id
- publisher_id
- author_id
- canonical URL
- OG metadata
- JSON-LD
- HTML / JS correlation
- redirect unwrap
- public profile verification
- publisher verification
- conflict detection
- role-clean ID assignment
- concatenated Facebook URL extraction

TELETHON:
- register(bot, notify_bot=None)
- /getuidfb
- interactive session
- multiple URLs/message
- URL concatenation detection
- continues waiting for next URL
- /stop /cancel exits resolver session
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

from collections import defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Optional

from urllib.parse import (
    parse_qs,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlparse,
)

import requests

from telethon import events


# ====================================================================
# VERSION
# ====================================================================

VERSION = "V22 PRECISION"


# ====================================================================
# LOGGING
# ====================================================================

logger = logging.getLogger(__name__)


# ====================================================================
# CONFIG
# ====================================================================

DEFAULT_TIMEOUT = 12.0
DEFAULT_MAX_PAGES = 8
DEFAULT_CONCURRENCY = 3

MAX_BODY_BYTES = 12 * 1024 * 1024
MAX_TELEGRAM_MESSAGE = 3900

PROFILE_VERIFY_LIMIT = 3
PROFILE_VERIFY_TIMEOUT = 9.0

SESSION_TIMEOUT = 15 * 60


# ====================================================================
# COMMAND INFO
# ====================================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": "Facebook UID / Entity Resolver V22 Precision",
    "usage": "/getuidfb",
    "category": "Facebook",
}


# ====================================================================
# FACEBOOK HOSTS
# ====================================================================

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


# ====================================================================
# TRACKING PARAMETERS
# ====================================================================

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
    "rdid",
}


# ====================================================================
# RESERVED FACEBOOK PATHS
# ====================================================================

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
    "event",
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
    "plugins",
    "directory",
    "hashtag",
    "search",
    "public",
    "people",
    "profile.php",
    "photo.php",
    "story.php",
    "video.php",
}


# ====================================================================
# REGEX
# ====================================================================

NUMERIC_ID_RE = re.compile(
    r"^\d{5,30}$"
)

PF_BID_RE = re.compile(
    r"^pfbid[A-Za-z0-9_-]+$",
    re.IGNORECASE,
)

FB_DOMAIN_START_RE = re.compile(
    r"""
    (?ix)
    (?:
        https?://
    )?
    (?:
        (?:www|m|mbasic|web|touch|mobile)\.facebook\.com
        |
        (?:l|lm)\.facebook\.com
        |
        facebook\.com
        |
        fb\.watch
    )
    """,
)

URL_START_RE = re.compile(
    r"""
    (?ix)
    (?:
        https?://
    )?
    (?:
        (?:www|m|mbasic|web|touch|mobile)\.facebook\.com
        |
        (?:l|lm)\.facebook\.com
        |
        facebook\.com
        |
        fb\.watch
    )
    """
)

NUMERIC_RE = re.compile(
    r"(?<!\d)\d{5,30}(?!\d)"
)


# ====================================================================
# UTILITIES
# ====================================================================

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


def truncate(value: str, length: int = 500) -> str:
    value = clean_text(value)

    if len(value) <= length:
        return value

    return value[: length - 3] + "..."


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


def is_http_url(url: str) -> bool:
    try:
        return urlparse(url).scheme.lower() in {
            "http",
            "https",
        }
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


def is_fb_host(url: str) -> bool:
    try:
        host = normalize_host(
            urlparse(url).hostname or ""
        )

        return host in FB_HOSTS

    except Exception:
        return False


def strip_url_punctuation(url: str) -> str:
    return url.rstrip(
        ".,;:!?)]}>\"'"
    )


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

    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    scheme = parsed.scheme.lower()

    host = normalize_host(
        parsed.hostname or ""
    )

    if not host:
        return ""

    path = parsed.path or "/"

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


# ====================================================================
# FACEBOOK URL EXTRACTION
# ====================================================================

def extract_facebook_urls(text: str) -> list[str]:
    """
    Extract ALL Facebook URLs.

    Handles:

        URL URL

        URL
        URL

        URL, URL

    and importantly:

        https://facebook.com/share/p/AAA/https://facebook.com/user/posts/pfbidBBB

    The second URL is detected from its own URL-start position.
    """

    if not text:
        return []

    text = html.unescape(text)

    matches = list(
        URL_START_RE.finditer(text)
    )

    results = []

    for index, match in enumerate(matches):

        start = match.start()

        if index + 1 < len(matches):
            end = matches[index + 1].start()
        else:
            end = len(text)

        segment = text[start:end]

        segment = segment.strip()

        segment = strip_url_punctuation(
            segment
        )

        # Remove accidental separator before next content.
        segment = segment.strip(
            " \t\r\n,;<>\"'"
        )

        if not segment:
            continue

        # A concatenated URL can contain trailing slash
        # before the next URL. That is intentionally preserved.
        normalized = normalize_url(segment)

        if not normalized:
            continue

        if not is_fb_host_or_redirect(
            normalized
        ):
            continue

        results.append(normalized)

    # Also inspect encoded nested URLs.
    extra = []

    for url in results:
        try:
            parsed = urlparse(url)
            query = parse_qs(
                parsed.query,
                keep_blank_values=True,
            )

            for key in (
                "u",
                "url",
                "target",
                "redirect",
                "share_url",
            ):
                for value in query.get(
                    key,
                    [],
                ):
                    value = unquote(
                        value
                    )

                    nested = extract_facebook_urls(
                        value
                    )

                    extra.extend(
                        nested
                    )

        except Exception:
            continue

    return unique_list(
        results + extra
    )


# ====================================================================
# BASE64 / FACEBOOK ENCODED ID
# ====================================================================

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
    """
    Conservative decoder.

    IMPORTANT:
    A decoded number is NOT automatically treated as USER UID.
    """

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

        decoded = raw.decode(
            "utf-8",
            errors="ignore",
        )

        decoded = clean_text(
            decoded
        )

        if not decoded:
            continue

        match = re.search(
            r"S:_I(\d{5,30}):(\d{5,30})",
            decoded,
        )

        if match:
            result["actor_id"] = (
                match.group(1)
            )

            result["object_id"] = (
                match.group(2)
            )

            result["decoded_text"] = decoded

            return result

        numbers = re.findall(
            r"(?<!\d)(\d{5,30})(?!\d)",
            decoded,
        )

        if numbers:

            result["decoded_id"] = (
                numbers[-1]
            )

            result["decoded_text"] = decoded

            return result

    return result


# ====================================================================
# EVIDENCE
# ====================================================================

@dataclass
class Evidence:
    field: str
    value: str
    source: str
    score: int = 0
    context: str = ""
    role: str = ""
    token: str = ""
    independent_group: str = ""

    def key(self) -> tuple:
        return (
            self.field,
            self.value,
            self.source,
            self.role,
            self.token,
        )


# ====================================================================
# URL SHAPE
# ====================================================================

@dataclass
class URLShape:
    url: str = ""
    host: str = ""
    path: str = ""
    path_lower: str = ""

    url_type: str = "UNKNOWN"

    username: Optional[str] = None

    group_id: Optional[str] = None
    page_id: Optional[str] = None

    post_id: Optional[str] = None
    video_id: Optional[str] = None
    reel_id: Optional[str] = None
    photo_id: Optional[str] = None
    story_id: Optional[str] = None
    album_id: Optional[str] = None

    share_token: Optional[str] = None
    object_token: Optional[str] = None

    profile_id: Optional[str] = None
    publisher_id: Optional[str] = None


# ====================================================================
# URL SHAPE PARSER
# ====================================================================

class URLShapeParser:

    def parse(
        self,
        url: str,
    ) -> URLShape:

        shape = URLShape(
            url=url
        )

        try:
            parsed = urlparse(url)
        except Exception:
            return shape

        shape.host = normalize_host(
            parsed.hostname or ""
        )

        shape.path = unquote(
            parsed.path or "/"
        )

        shape.path_lower = (
            shape.path.lower()
        )

        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
        )

        path = shape.path
        lower = shape.path_lower

        # ------------------------------------------------------------
        # GROUP
        # ------------------------------------------------------------

        group_match = re.match(
            r"^/groups/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if group_match:

            group_token = group_match.group(
                1
            )

            if is_numeric_id(
                group_token
            ):
                shape.group_id = group_token

            if re.search(
                r"/posts/([^/?#]+)",
                lower,
                re.IGNORECASE,
            ):
                post_match = re.search(
                    r"/posts/([^/?#]+)",
                    path,
                    re.IGNORECASE,
                )

                if post_match:
                    shape.post_id = (
                        post_match.group(1)
                    )

                shape.url_type = (
                    "GROUP_POST"
                )

            else:
                shape.url_type = "GROUP"

            return shape

        # ------------------------------------------------------------
        # PAGE
        # ------------------------------------------------------------

        page_match = re.match(
            r"^/pages/([^/?#]+)/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if page_match:

            possible_page_id = (
                page_match.group(2)
            )

            if is_numeric_id(
                possible_page_id
            ):
                shape.page_id = (
                    possible_page_id
                )

            shape.url_type = "PAGE"

            return shape

        # ------------------------------------------------------------
        # PROFILE.PHP
        # ------------------------------------------------------------

        if lower.startswith(
            "/profile.php"
        ):

            for key in (
                "id",
                "profile_id",
                "user_id",
            ):

                values = query.get(
                    key,
                    [],
                )

                if values:
                    value = values[0]

                    if is_numeric_id(value):
                        shape.profile_id = (
                            value
                        )
                        shape.url_type = (
                            "PROFILE"
                        )
                        return shape

        # ------------------------------------------------------------
        # PHOTO.PHP
        # ------------------------------------------------------------

        if lower.startswith(
            "/photo.php"
        ):

            values = query.get(
                "fbid",
                [],
            )

            if values:
                shape.photo_id = values[0]

            values = query.get(
                "id",
                [],
            )

            if values and is_numeric_id(
                values[0]
            ):
                shape.publisher_id = (
                    values[0]
                )

            values = query.get(
                "set",
                [],
            )

            if values:

                set_value = values[0]

                match = re.search(
                    r"(?:^|[.:])a?\.?(\d{5,30})",
                    set_value,
                    re.IGNORECASE,
                )

                if match:
                    shape.album_id = (
                        match.group(1)
                    )

            shape.url_type = "PHOTO"

            return shape

        # ------------------------------------------------------------
        # STORY.PHP
        # ------------------------------------------------------------

        if lower.startswith(
            "/story.php"
        ):

            values = query.get(
                "story_fbid",
                [],
            )

            if values:
                shape.story_id = values[0]

            values = query.get(
                "id",
                [],
            )

            if values and is_numeric_id(
                values[0]
            ):
                shape.publisher_id = (
                    values[0]
                )

            shape.url_type = "STORY"

            return shape

        # ------------------------------------------------------------
        # WATCH
        # ------------------------------------------------------------

        if lower.startswith(
            "/watch"
        ):

            values = query.get(
                "v",
                [],
            )

            if values:
                shape.video_id = values[0]

            shape.url_type = "VIDEO"

            return shape

        # ------------------------------------------------------------
        # VIDEO.PHP
        # ------------------------------------------------------------

        if lower.startswith(
            "/video.php"
        ):

            values = query.get(
                "v",
                [],
            )

            if values:
                shape.video_id = values[0]

            shape.url_type = "VIDEO"

            return shape

        # ------------------------------------------------------------
        # SHARE
        # ------------------------------------------------------------

        share_match = re.match(
            r"^/share/(p|v|r|s)/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if share_match:

            share_type = (
                share_match.group(1).lower()
            )

            token = (
                share_match.group(2)
            )

            shape.share_token = token
            shape.object_token = token

            if share_type == "p":
                shape.url_type = "POST"

            elif share_type == "v":
                shape.url_type = "VIDEO"

            elif share_type == "r":
                shape.url_type = "REEL"

            elif share_type == "s":
                shape.url_type = "STORY"

            return shape

        # ------------------------------------------------------------
        # REEL
        # ------------------------------------------------------------

        reel_match = re.match(
            r"^/(?:reel|reels)/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if reel_match:

            value = reel_match.group(
                1
            )

            shape.reel_id = value
            shape.video_id = value
            shape.url_type = "REEL"

            return shape

        # ------------------------------------------------------------
        # STORY ROUTE
        # ------------------------------------------------------------

        story_match = re.match(
            r"^/stories/([^/?#]+)/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if story_match:

            actor = story_match.group(
                1
            )

            story = story_match.group(
                2
            )

            shape.story_id = story

            if is_numeric_id(actor):
                shape.publisher_id = actor
            else:
                shape.username = actor

            shape.url_type = "STORY"

            return shape

        # ------------------------------------------------------------
        # USERNAME CONTENT ROUTES
        # ------------------------------------------------------------

        content_match = re.match(
            r"^/([^/?#]+)/(posts|videos|reels|reel|photos)/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if content_match:

            username = (
                content_match.group(1)
            )

            content_type = (
                content_match.group(2).lower()
            )

            object_id = (
                content_match.group(3)
            )

            if username.lower() not in (
                RESERVED_PATHS
            ):

                shape.username = username

            if content_type == "posts":

                shape.post_id = object_id
                shape.url_type = "POST"

            elif content_type == "videos":

                shape.video_id = object_id
                shape.url_type = "VIDEO"

            elif content_type in {
                "reels",
                "reel",
            }:

                shape.reel_id = object_id
                shape.video_id = object_id
                shape.url_type = "REEL"

            elif content_type == "photos":

                shape.photo_id = object_id
                shape.url_type = "PHOTO"

            return shape

        # ------------------------------------------------------------
        # /p/TOKEN
        # ------------------------------------------------------------

        p_match = re.match(
            r"^/p/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if p_match:

            token = p_match.group(1)

            shape.post_id = token
            shape.object_token = token
            shape.url_type = "POST"

            return shape

        # ------------------------------------------------------------
        # /posts/TOKEN
        # ------------------------------------------------------------

        posts_match = re.match(
            r"^/posts/([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if posts_match:

            token = posts_match.group(1)

            shape.post_id = token
            shape.object_token = token
            shape.url_type = "POST"

            return shape

        # ------------------------------------------------------------
        # /photos/...
        # ------------------------------------------------------------

        photos_match = re.match(
            r"^/photos/(?:[^/]+/)?([^/?#]+)",
            path,
            re.IGNORECASE,
        )

        if photos_match:

            value = photos_match.group(1)

            if value.lower() not in RESERVED_PATHS:
                shape.photo_id = value

            shape.url_type = "PHOTO"

            return shape

        # ------------------------------------------------------------
        # QUERY IDS
        # ------------------------------------------------------------

        mapping = {
            "post_id": "post_id",
            "video_id": "video_id",
            "reel_id": "reel_id",
            "photo_id": "photo_id",
            "story_id": "story_id",
            "page_id": "page_id",
            "group_id": "group_id",
            "profile_id": "profile_id",
            "user_id": "profile_id",
            "publisher_id": "publisher_id",
        }

        for key, target in mapping.items():

            for value in query.get(
                key,
                [],
            ):

                if not value:
                    continue

                if target in {
                    "profile_id",
                    "publisher_id",
                    "page_id",
                    "group_id",
                } and not is_numeric_id(
                    value
                ):
                    continue

                setattr(
                    shape,
                    target,
                    value,
                )

        # ------------------------------------------------------------
        # Generic profile-like path
        # ------------------------------------------------------------

        parts = [
            x
            for x in path.split("/")
            if x
        ]

        if (
            len(parts) == 1
            and parts[0].lower()
            not in RESERVED_PATHS
        ):

            if is_numeric_id(
                parts[0]
            ):
                shape.publisher_id = (
                    parts[0]
                )
            else:
                shape.username = parts[0]

            shape.url_type = "PROFILE"

        return shape


# ====================================================================
# HTML PARSER
# ====================================================================

class FacebookHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.title = ""
        self.canonical = ""

        self.meta = {}
        self.links = []

        self.jsonld = []

        self._inside_title = False
        self._title_buffer = []

        self._inside_jsonld = False
        self._jsonld_buffer = []

    def handle_starttag(
        self,
        tag,
        attrs,
    ):

        tag_lower = tag.lower()

        attrs_dict = {
            str(k).lower(): (
                v or ""
            )
            for k, v in attrs
        }

        if tag_lower == "title":

            self._inside_title = True
            self._title_buffer = []

        elif tag_lower == "meta":

            key = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or attrs_dict.get("itemprop")
                or ""
            ).lower()

            value = (
                attrs_dict.get("content")
                or ""
            )

            if key and value:
                self.meta[key] = (
                    html.unescape(value)
                )

        elif tag_lower == "link":

            rel = attrs_dict.get(
                "rel",
                ""
            ).lower()

            href = attrs_dict.get(
                "href",
                ""
            )

            if (
                "canonical" in rel
                and href
            ):
                self.canonical = html.unescape(
                    href
                )

        elif tag_lower == "a":

            href = attrs_dict.get(
                "href",
                ""
            )

            if href:
                self.links.append(
                    html.unescape(href)
                )

        elif tag_lower == "script":

            script_type = attrs_dict.get(
                "type",
                ""
            ).lower()

            if "ld+json" in script_type:

                self._inside_jsonld = True
                self._jsonld_buffer = []

    def handle_endtag(
        self,
        tag,
    ):

        tag_lower = tag.lower()

        if (
            tag_lower == "title"
            and self._inside_title
        ):

            self._inside_title = False

            self.title = clean_text(
                "".join(
                    self._title_buffer
                )
            )

        elif (
            tag_lower == "script"
            and self._inside_jsonld
        ):

            self._inside_jsonld = False

            raw = "".join(
                self._jsonld_buffer
            ).strip()

            if raw:

                try:
                    data = json.loads(raw)
                    self.jsonld.append(data)
                except Exception:
                    pass

    def handle_data(
        self,
        data,
    ):

        if self._inside_title:
            self._title_buffer.append(
                data
            )

        if self._inside_jsonld:
            self._jsonld_buffer.append(
                data
            )


# ====================================================================
# JSON WALK
# ====================================================================

def walk_json(
    obj: Any,
    path: str = "",
):
    if isinstance(obj, dict):

        yield obj, path

        for key, value in obj.items():

            current = (
                f"{path}.{key}"
                if path
                else str(key)
            )

            yield from walk_json(
                value,
                current,
            )

    elif isinstance(obj, list):

        for index, item in enumerate(
            obj
        ):

            yield from walk_json(
                item,
                f"{path}[{index}]",
            )


# ====================================================================
# SNAPSHOT
# ====================================================================

@dataclass
class Snapshot:
    requested_url: str = ""
    final_url: str = ""
    status_code: int = 0

    html: str = ""

    headers: dict[str, str] = field(
        default_factory=dict
    )

    history: list[str] = field(
        default_factory=list
    )

    title: str = ""
    canonical: str = ""

    og: dict[str, str] = field(
        default_factory=dict
    )

    links: list[str] = field(
        default_factory=list
    )

    jsonld: list[Any] = field(
        default_factory=list
    )


# ====================================================================
# HTTP CLIENT
# ====================================================================

class HTTPClient:

    USER_AGENTS = [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Linux; Android 14) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/138.0.0.0 Mobile Safari/537.36"
        ),
    ]

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
    ):

        self.timeout = timeout

        self.session = requests.Session()

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def _headers(
        self,
        user_agent: str,
    ):

        return {
            "User-Agent": user_agent,
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,"
                "image/webp,"
                "image/apng,"
                "*/*;q=0.8"
            ),
            "Accept-Language": (
                "vi-VN,vi;q=0.9,"
                "en-US;q=0.8,"
                "en;q=0.7"
            ),
            "Accept-Encoding": (
                "gzip, deflate"
            ),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }

    def _get_sync(
        self,
        url: str,
        user_agent: str,
    ) -> Optional[Snapshot]:

        try:

            response = self.session.get(
                url,
                headers=self._headers(
                    user_agent
                ),
                timeout=(
                    self.timeout,
                    self.timeout,
                ),
                allow_redirects=True,
                stream=True,
            )

            chunks = []
            total = 0

            for chunk in response.iter_content(
                chunk_size=64 * 1024
            ):

                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_BODY_BYTES:

                    remaining = (
                        MAX_BODY_BYTES
                        - (
                            total
                            - len(chunk)
                        )
                    )

                    if remaining > 0:
                        chunks.append(
                            chunk[:remaining]
                        )

                    break

                chunks.append(chunk)

            raw = b"".join(chunks)

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
                status_code=(
                    response.status_code
                ),
                html=text,
                headers={
                    str(k): str(v)
                    for k, v in response.headers.items()
                },
                history=history,
            )

        except requests.RequestException:
            return None

        except Exception:
            logger.exception(
                "Facebook HTTP error"
            )
            return None

    async def get(
        self,
        url: str,
    ) -> Optional[Snapshot]:

        for index, user_agent in enumerate(
            self.USER_AGENTS
        ):

            snapshot = await asyncio.to_thread(
                self._get_sync,
                url,
                user_agent,
            )

            if snapshot is None:

                if index + 1 < len(
                    self.USER_AGENTS
                ):
                    await asyncio.sleep(
                        0.35 * (index + 1)
                    )

                continue

            if snapshot.status_code in {
                403,
                429,
                500,
                502,
                503,
                504,
            }:

                if index + 1 < len(
                    self.USER_AGENTS
                ):

                    await asyncio.sleep(
                        0.45 * (index + 1)
                    )

                    continue

            return snapshot

        return None


# ====================================================================
# RESOLVE RESULT
# ====================================================================

@dataclass
class ResolveResult:

    input_url: str = ""
    resolved_url: str = ""
    canonical_url: str = ""

    url_type: str = "UNKNOWN"

    entity_type: str = "UNKNOWN"
    publisher_type: str = "UNKNOWN"

    status: str = "UNKNOWN"
    success: bool = False

    user_uid: Optional[str] = None
    page_uid: Optional[str] = None
    group_id: Optional[str] = None

    publisher_id: Optional[str] = None
    publisher_username: Optional[str] = None
    publisher_name: Optional[str] = None

    post_id: Optional[str] = None
    video_id: Optional[str] = None
    reel_id: Optional[str] = None
    photo_id: Optional[str] = None
    story_id: Optional[str] = None
    album_id: Optional[str] = None

    media_fbid: Optional[str] = None
    actor_id: Optional[str] = None
    entity_id: Optional[str] = None
    decoded_id: Optional[str] = None

    title: str = ""
    description: str = ""

    user_uid_verified: bool = False

    confidence: int = 0

    evidence: list[Evidence] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )

    crawl_chain: list[str] = field(
        default_factory=list
    )


# ====================================================================
# FACEBOOK RESOLVER
# ====================================================================

class FacebookResolver:

    USER_ID_KEYS = {
        "user_id": 95,
        "profile_id": 100,
        "owner_id": 92,
        "publisher_id": 92,
        "author_id": 90,
    }

    PAGE_ID_KEYS = {
        "page_id": 100,
    }

    GROUP_ID_KEYS = {
        "group_id": 100,
    }

    OBJECT_ID_KEYS = {
        "post_id",
        "video_id",
        "reel_id",
        "photo_id",
        "story_id",
        "media_fbid",
        "entity_id",
    }

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_pages: int = DEFAULT_MAX_PAGES,
        concurrency: int = DEFAULT_CONCURRENCY,
    ):

        self.timeout = timeout
        self.max_pages = max_pages

        self.visited: set[str] = set()

        self.cache: dict[
            str,
            Optional[Snapshot],
        ] = {}

        self.snapshots: list[
            Snapshot
        ] = []

        self.evidence: list[
            Evidence
        ] = []

        self.semaphore = asyncio.Semaphore(
            concurrency
        )

        self.shape_parser = URLShapeParser()

    # =================================================================
    # EVIDENCE
    # =================================================================

    def add(
        self,
        field_name: str,
        value: Any,
        source: str,
        score: int,
        context: str = "",
        role: str = "",
        token: str = "",
        independent_group: str = "",
    ):

        value = clean_text(value)

        if not value:
            return

        self.evidence.append(
            Evidence(
                field=field_name,
                value=value,
                source=source,
                score=score,
                context=truncate(
                    context,
                    350,
                ),
                role=role,
                token=token,
                independent_group=(
                    independent_group
                    or source
                ),
            )
        )

    # =================================================================
    # ADD URL SHAPE EVIDENCE
    # =================================================================

    def analyze_url(
        self,
        url: str,
        source: str = "url",
    ):

        shape = self.shape_parser.parse(
            url
        )

        if shape.url_type:
            self.add(
                "url_type",
                shape.url_type,
                source,
                100,
            )

        if shape.username:
            self.add(
                "publisher_username",
                shape.username,
                source,
                95,
                role="publisher",
            )

        if shape.publisher_id:
            self.add(
                "publisher_id",
                shape.publisher_id,
                source,
                88,
                role="publisher",
            )

        if shape.profile_id:
            self.add(
                "profile_id",
                shape.profile_id,
                source,
                100,
                role="user",
            )

        if shape.page_id:
            self.add(
                "page_id",
                shape.page_id,
                source,
                100,
                role="page",
            )

        if shape.group_id:
            self.add(
                "group_id",
                shape.group_id,
                source,
                100,
                role="group",
            )

        if shape.post_id:
            self.add(
                "post_id",
                shape.post_id,
                source,
                100,
                role="object",
                token=(
                    shape.object_token
                    or ""
                ),
            )

        if shape.video_id:
            self.add(
                "video_id",
                shape.video_id,
                source,
                100,
                role="object",
            )

        if shape.reel_id:
            self.add(
                "reel_id",
                shape.reel_id,
                source,
                100,
                role="object",
            )

        if shape.photo_id:
            self.add(
                "photo_id",
                shape.photo_id,
                source,
                100,
                role="object",
            )

        if shape.story_id:
            self.add(
                "story_id",
                shape.story_id,
                source,
                100,
                role="object",
            )

        if shape.album_id:
            self.add(
                "album_id",
                shape.album_id,
                source,
                100,
                role="album",
            )

        if shape.share_token:
            self.add(
                "object_token",
                shape.share_token,
                source,
                95,
                role="opaque",
                token=shape.share_token,
            )

        return shape

    # =================================================================
    # PARSE SNAPSHOT
    # =================================================================

    def parse_snapshot(
        self,
        snapshot: Snapshot,
    ):

        parser = FacebookHTMLParser()

        try:
            parser.feed(
                snapshot.html
            )
        except Exception:
            logger.exception(
                "HTML parser error"
            )

        snapshot.title = (
            parser.title
        )

        snapshot.canonical = (
            parser.canonical
        )

        snapshot.links = (
            parser.links
        )

        snapshot.jsonld = (
            parser.jsonld
        )

        for key, value in (
            parser.meta.items()
        ):

            if key.startswith("og:"):
                snapshot.og[key] = value

        # ------------------------------------------------------------
        # URL
        # ------------------------------------------------------------

        self.analyze_url(
            snapshot.final_url,
            "final_url",
        )

        for history_url in (
            snapshot.history
        ):

            self.analyze_url(
                history_url,
                "redirect",
            )

        if snapshot.canonical:

            canonical = urljoin(
                snapshot.final_url,
                snapshot.canonical,
            )

            canonical = normalize_url(
                canonical
            )

            if canonical:

                self.analyze_url(
                    canonical,
                    "canonical",
                )

        # ------------------------------------------------------------
        # META
        # ------------------------------------------------------------

        if snapshot.title:

            self.add(
                "title",
                snapshot.title,
                "html:title",
                70,
            )

        for key, value in (
            snapshot.og.items()
        ):

            self.add(
                key,
                value,
                "og",
                75,
            )

        # ------------------------------------------------------------
        # ARTICLE AUTHOR
        # ------------------------------------------------------------

        article_author = (
            parser.meta.get(
                "article:author"
            )
        )

        if article_author:

            self.add(
                "author_url",
                article_author,
                "meta:author",
                90,
                role="user",
            )

            self.analyze_profile_url(
                article_author,
                "meta:author",
            )

        # ------------------------------------------------------------
        # JSON-LD
        # ------------------------------------------------------------

        for data in (
            snapshot.jsonld
        ):

            self.parse_jsonld(
                data
            )

        # ------------------------------------------------------------
        # HTML / JS
        # ------------------------------------------------------------

        self.parse_semantic_html(
            snapshot.html
        )

        # ------------------------------------------------------------
        # LINKS
        # ------------------------------------------------------------

        for link in (
            snapshot.links
        ):

            absolute = urljoin(
                snapshot.final_url,
                link,
            )

            absolute = normalize_url(
                absolute
            )

            if not absolute:
                continue

            if not is_fb_host_or_redirect(
                absolute
            ):
                continue

            self.analyze_profile_url(
                absolute,
                "html:link",
            )

            self.analyze_url(
                absolute,
                "html:url",
            )

    # =================================================================
    # PROFILE URL ANALYSIS
    # =================================================================

    def analyze_profile_url(
        self,
        url: str,
        source: str,
    ):

        if not url:
            return

        url = html.unescape(
            clean_text(url)
        )

        if not is_http_url(url):

            # Sometimes article:author contains
            # relative Facebook path.
            if url.startswith("/"):
                url = urljoin(
                    "https://www.facebook.com",
                    url,
                )
            else:
                return

        url = normalize_url(
            url
        )

        if not url:
            return

        if not is_fb_host_or_redirect(
            url
        ):
            return

        shape = self.shape_parser.parse(
            url
        )

        if shape.profile_id:

            self.add(
                "profile_id",
                shape.profile_id,
                source,
                100,
                role="user",
                independent_group=source,
            )

        if shape.username:

            self.add(
                "publisher_username",
                shape.username,
                source,
                90,
                role="publisher",
            )

        if shape.publisher_id:

            self.add(
                "publisher_id",
                shape.publisher_id,
                source,
                85,
                role="publisher",
            )

        if shape.page_id:

            self.add(
                "page_id",
                shape.page_id,
                source,
                100,
                role="page",
            )

        if shape.group_id:

            self.add(
                "group_id",
                shape.group_id,
                source,
                100,
                role="group",
            )

    # =================================================================
    # JSON-LD
    # =================================================================

    def parse_jsonld(
        self,
        data: Any,
    ):

        for obj, path in walk_json(data):

            if not isinstance(
                obj,
                dict,
            ):
                continue

            object_type = obj.get(
                "@type"
            )

            if isinstance(
                object_type,
                list,
            ):
                types = [
                    clean_text(x).lower()
                    for x in object_type
                ]
            else:
                types = [
                    clean_text(
                        object_type
                    ).lower()
                ]

            # --------------------------------------------------------
            # Name/title
            # --------------------------------------------------------

            for key in (
                "name",
                "headline",
                "caption",
            ):

                value = obj.get(key)

                if value:

                    self.add(
                        "title",
                        value,
                        "jsonld",
                        55,
                        context=path,
                        independent_group=(
                            "jsonld:title"
                        ),
                    )

            # --------------------------------------------------------
            # identifier
            # --------------------------------------------------------

            identifier = obj.get(
                "identifier"
            )

            if (
                identifier is not None
                and is_numeric_id(
                    clean_text(identifier)
                )
            ):

                identifier = clean_text(
                    identifier
                )

                if any(
                    x in types
                    for x in (
                        "person",
                        "user",
                    )
                ):

                    self.add(
                        "profile_id",
                        identifier,
                        "jsonld:identifier",
                        82,
                        role="user",
                        independent_group=(
                            "jsonld:person"
                        ),
                    )

                elif any(
                    x in types
                    for x in (
                        "organization",
                        "corporation",
                        "localbusiness",
                        "brand",
                    )
                ):

                    self.add(
                        "page_id",
                        identifier,
                        "jsonld:identifier",
                        82,
                        role="page",
                        independent_group=(
                            "jsonld:organization"
                        ),
                    )

                else:

                    self.add(
                        "entity_id",
                        identifier,
                        "jsonld:identifier",
                        45,
                        role="entity",
                    )

            # --------------------------------------------------------
            # URL
            # --------------------------------------------------------

            url_value = obj.get(
                "url"
            )

            if isinstance(
                url_value,
                str,
            ):

                self.analyze_profile_url(
                    url_value,
                    "jsonld:url",
                )

            # --------------------------------------------------------
            # author
            # --------------------------------------------------------

            author = obj.get(
                "author"
            )

            self.parse_jsonld_person(
                author,
                "jsonld:author",
            )

            # --------------------------------------------------------
            # creator
            # --------------------------------------------------------

            creator = obj.get(
                "creator"
            )

            self.parse_jsonld_person(
                creator,
                "jsonld:creator",
            )

            # --------------------------------------------------------
            # publisher
            # --------------------------------------------------------

            publisher = obj.get(
                "publisher"
            )

            self.parse_jsonld_person(
                publisher,
                "jsonld:publisher",
            )

            # --------------------------------------------------------
            # mainEntityOfPage
            # --------------------------------------------------------

            main_entity = obj.get(
                "mainEntityOfPage"
            )

            if isinstance(
                main_entity,
                dict,
            ):

                main_url = main_entity.get(
                    "url"
                )

                if isinstance(
                    main_url,
                    str,
                ):
                    self.analyze_url(
                        normalize_url(
                            main_url
                        ),
                        "jsonld:mainEntity",
                    )

    def parse_jsonld_person(
        self,
        value: Any,
        source: str,
    ):

        if isinstance(
            value,
            list,
        ):

            for item in value:
                self.parse_jsonld_person(
                    item,
                    source,
                )

            return

        if isinstance(
            value,
            str,
        ):

            self.analyze_profile_url(
                value,
                source,
            )

            return

        if not isinstance(
            value,
            dict,
        ):
            return

        object_type = clean_text(
            value.get("@type", "")
        ).lower()

        identifier = clean_text(
            value.get(
                "identifier",
                ""
            )
        )

        url_value = clean_text(
            value.get(
                "url",
                ""
            )
        )

        name = clean_text(
            value.get(
                "name",
                ""
            )
        )

        if name:

            self.add(
                "publisher_name",
                name,
                source,
                75,
                role="publisher",
                independent_group=source,
            )

        if url_value:

            self.analyze_profile_url(
                url_value,
                source,
            )

        if (
            identifier
            and is_numeric_id(identifier)
        ):

            if object_type in {
                "person",
                "user",
            }:

                self.add(
                    "profile_id",
                    identifier,
                    source,
                    90,
                    role="user",
                    independent_group=source,
                )

            elif object_type in {
                "organization",
                "corporation",
                "localbusiness",
                "brand",
            }:

                self.add(
                    "page_id",
                    identifier,
                    source,
                    90,
                    role="page",
                    independent_group=source,
                )

    # =================================================================
    # SEMANTIC HTML / JS
    # =================================================================

    def parse_semantic_html(
        self,
        text: str,
    ):

        if not text:
            return

        # ------------------------------------------------------------
        # Strong identity keys
        # ------------------------------------------------------------

        patterns = {
            "profile_id": [
                r'"profile_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"profileID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"profileId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "user_id": [
                r'"user_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"userID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"userId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "owner_id": [
                r'"owner_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"ownerID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"ownerId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "publisher_id": [
                r'"publisher_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"publisherID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"publisherId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "author_id": [
                r'"author_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"authorID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"authorId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "page_id": [
                r'"page_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"pageID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"pageId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "group_id": [
                r'"group_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"groupID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"groupId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "actor_id": [
                r'"actor_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"actorID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"actorId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "post_id": [
                r'"post_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"postID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"postId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "video_id": [
                r'"video_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"videoID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"videoId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "reel_id": [
                r'"reel_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"reelID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"reelId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "photo_id": [
                r'"photo_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"photoID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"photoId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "story_id": [
                r'"story_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"storyID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"storyId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
            "media_fbid": [
                r'"media_fbid"\s*:\s*"?(?P<id>\d{5,50})',
                r'"mediaFbid"\s*:\s*"?(?P<id>\d{5,50})',
            ],
            "entity_id": [
                r'"entity_id"\s*:\s*"?(?P<id>\d{5,30})',
                r'"entityID"\s*:\s*"?(?P<id>\d{5,30})',
                r'"entityId"\s*:\s*"?(?P<id>\d{5,30})',
            ],
        }

        for field_name, regexes in (
            patterns.items()
        ):

            for pattern_text in regexes:

                try:
                    pattern = re.compile(
                        pattern_text,
                        re.IGNORECASE,
                    )
                except re.error:
                    continue

                for match in pattern.finditer(
                    text
                ):

                    value = (
                        match.groupdict()
                        .get("id")
                    )

                    if not value:
                        continue

                    context = text[
                        max(
                            0,
                            match.start() - 300,
                        ):
                        min(
                            len(text),
                            match.end() + 300,
                        )
                    ]

                    score = 80

                    if field_name == "profile_id":
                        score = 100

                    elif field_name == "user_id":
                        score = 96

                    elif field_name in {
                        "owner_id",
                        "publisher_id",
                        "author_id",
                    }:
                        score = 90

                    elif field_name == "actor_id":
                        score = 65

                    elif field_name == "page_id":
                        score = 100

                    elif field_name == "group_id":
                        score = 100

                    self.add(
                        field_name,
                        value,
                        "html_js",
                        score,
                        context=context,
                        role=(
                            "user"
                            if field_name
                            in {
                                "profile_id",
                                "user_id",
                                "owner_id",
                                "publisher_id",
                                "author_id",
                            }
                            else "object"
                        ),
                        independent_group=(
                            f"html_js:{field_name}"
                        ),
                    )

        # ------------------------------------------------------------
        # Nested from/author/owner/publisher
        # ------------------------------------------------------------

        nested_patterns = [
            (
                "user",
                r'"(?:from|author|owner|publisher)"'
                r'\s*:\s*\{'
                r'[^{}]{0,1800}?'
                r'"id"\s*:\s*"?(?P<id>\d{5,30})',
                84,
            ),
            (
                "actor",
                r'"actor"'
                r'\s*:\s*\{'
                r'[^{}]{0,1200}?'
                r'"id"\s*:\s*"?(?P<id>\d{5,30})',
                65,
            ),
        ]

        for role, pattern_text, score in (
            nested_patterns
        ):

            try:
                pattern = re.compile(
                    pattern_text,
                    re.IGNORECASE,
                )
            except re.error:
                continue

            for match in pattern.finditer(
                text
            ):

                value = match.group(
                    "id"
                )

                context = text[
                    max(
                        0,
                        match.start() - 200,
                    ):
                    min(
                        len(text),
                        match.end() + 200,
                    )
                ]

                self.add(
                    (
                        "publisher_id"
                        if role == "user"
                        else "actor_id"
                    ),
                    value,
                    "nested_object",
                    score,
                    context=context,
                    role=role,
                    independent_group=(
                        "nested:" + role
                    ),
                )

        # ------------------------------------------------------------
        # Username
        # ------------------------------------------------------------

        username_patterns = [
            r'"username"\s*:\s*"([^"]{1,100})"',
            r'"userName"\s*:\s*"([^"]{1,100})"',
            r'"vanity"\s*:\s*"([^"]{1,100})"',
        ]

        for pattern_text in (
            username_patterns
        ):

            try:
                pattern = re.compile(
                    pattern_text,
                    re.IGNORECASE,
                )
            except re.error:
                continue

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

                    self.add(
                        "publisher_username",
                        username,
                        "identity",
                        65,
                        independent_group=(
                            "identity:username"
                        ),
                    )

        # ------------------------------------------------------------
        # Facebook URLs inside HTML/JS
        # ------------------------------------------------------------

        for url in extract_facebook_urls(
            text
        ):

            self.analyze_url(
                url,
                "html:url",
            )

        # ------------------------------------------------------------
        # Deep links
        # ------------------------------------------------------------

        deep_patterns = [
            (
                "user_uid",
                r"fb://profile/(?P<id>\d{5,30})",
                90,
            ),
            (
                "page_id",
                r"fb://page/(?P<id>\d{5,30})",
                90,
            ),
            (
                "group_id",
                r"fb://group/(?P<id>\d{5,30})",
                90,
            ),
            (
                "entity_id",
                r"fb://(?:post|object)/(?P<id>\d{5,30})",
                85,
            ),
        ]

        for field_name, pattern_text, score in (
            deep_patterns
        ):

            try:
                pattern = re.compile(
                    pattern_text,
                    re.IGNORECASE,
                )
            except re.error:
                continue

            for match in pattern.finditer(
                text
            ):

                value = match.group(
                    "id"
                )

                self.add(
                    field_name,
                    value,
                    "deep_link",
                    score,
                    role=(
                        "user"
                        if field_name
                        == "user_uid"
                        else field_name
                    ),
                    independent_group=(
                        "deep_link"
                    ),
                )

    # =================================================================
    # DISCOVER FACEBOOK URLS
    # =================================================================

    def discover_urls(
        self,
        snapshot: Snapshot,
    ) -> list[str]:

        found = []

        # Absolute URLs.
        found.extend(
            extract_facebook_urls(
                snapshot.html
            )
        )

        # Links.
        for link in (
            snapshot.links
        ):

            absolute = urljoin(
                snapshot.final_url,
                html.unescape(link),
            )

            absolute = normalize_url(
                absolute
            )

            if (
                absolute
                and is_fb_host_or_redirect(
                    absolute
                )
            ):
                found.append(
                    absolute
                )

        return unique_list(
            found
        )

    # =================================================================
    # FETCH
    # =================================================================

    async def fetch_one(
        self,
        client: HTTPClient,
        url: str,
    ) -> Optional[Snapshot]:

        url = normalize_url(url)

        if not url:
            return None

        if url in self.visited:
            return self.cache.get(
                url
            )

        if len(self.visited) >= (
            self.max_pages
        ):
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

    # =================================================================
    # CANDIDATE SCORING
    # =================================================================

    def candidate_scores(
        self,
        field_names: set[str],
    ) -> dict[str, int]:

        scores = defaultdict(int)

        for item in self.evidence:

            if item.field not in field_names:
                continue

            if not is_numeric_id(
                item.value
            ):
                continue

            scores[
                item.value
            ] += item.score

        # Independent source bonus.
        for value in list(scores):

            groups = {
                item.independent_group
                for item in self.evidence
                if (
                    item.field in field_names
                    and item.value == value
                    and item.independent_group
                )
            }

            scores[value] += min(
                len(groups) * 12,
                48,
            )

        return dict(scores)

    # =================================================================
    # BEST VALUE
    # =================================================================

    def best_value(
        self,
        field_names: set[str],
        numeric_only: bool = False,
    ) -> Optional[str]:

        scores = defaultdict(int)

        for item in self.evidence:

            if item.field not in field_names:
                continue

            if (
                numeric_only
                and not is_numeric_id(
                    item.value
                )
            ):
                continue

            scores[
                item.value
            ] += item.score

        if not scores:
            return None

        return max(
            scores,
            key=scores.get,
        )

    # =================================================================
    # PROFILE VERIFICATION
    # =================================================================

    async def verify_user_candidate(
        self,
        client: HTTPClient,
        uid: str,
        username: Optional[str] = None,
    ) -> dict[str, Any]:

        result = {
            "verified": False,
            "type": "UNKNOWN",
            "uid": uid,
            "name": "",
            "username": username,
            "evidence": [],
        }

        urls = []

        if username:

            username = clean_text(
                username
            )

            if (
                username
                and username.lower()
                not in RESERVED_PATHS
            ):

                urls.append(
                    "https://www.facebook.com/"
                    + quote(
                        username,
                        safe="._-",
                    )
                )

        urls.extend([
            (
                "https://www.facebook.com/"
                + uid
            ),
            (
                "https://www.facebook.com/profile.php"
                "?id="
                + uid
            ),
        ])

        urls = unique_list(
            urls
        )[
            :PROFILE_VERIFY_LIMIT
        ]

        for profile_url in urls:

            snapshot = await self.fetch_one(
                client,
                profile_url,
            )

            if not snapshot:
                continue

            parser = FacebookHTMLParser()

            try:
                parser.feed(
                    snapshot.html
                )
            except Exception:
                continue

            meta = parser.meta

            canonical = normalize_url(
                urljoin(
                    snapshot.final_url,
                    parser.canonical
                    or "",
                )
            )

            title = clean_text(
                parser.title
            )

            if title:
                result["name"] = title

            # --------------------------------------------------------
            # Parse identity evidence from profile page.
            # --------------------------------------------------------

            before = len(
                self.evidence
            )

            self.parse_snapshot(
                snapshot
            )

            profile_evidence = (
                self.evidence[before:]
            )

            uid_matches = [
                item
                for item in profile_evidence
                if (
                    item.value == uid
                    and item.field in {
                        "profile_id",
                        "user_id",
                        "owner_id",
                        "publisher_id",
                        "author_id",
                    }
                )
            ]

            page_matches = [
                item
                for item in profile_evidence
                if (
                    item.value == uid
                    and item.field
                    == "page_id"
                )
            ]

            group_matches = [
                item
                for item in profile_evidence
                if (
                    item.value == uid
                    and item.field
                    == "group_id"
                )
            ]

            # --------------------------------------------------------
            # JSON-LD entity type.
            # --------------------------------------------------------

            detected_type = "UNKNOWN"

            for data in parser.jsonld:

                for obj, _path in walk_json(
                    data
                ):

                    if not isinstance(
                        obj,
                        dict,
                    ):
                        continue

                    object_type = obj.get(
                        "@type"
                    )

                    if isinstance(
                        object_type,
                        list,
                    ):
                        types = {
                            clean_text(x).lower()
                            for x in object_type
                        }
                    else:
                        types = {
                            clean_text(
                                object_type
                            ).lower()
                        }

                    if (
                        "person" in types
                        or "user" in types
                    ):
                        detected_type = (
                            "USER"
                        )

                    if (
                        "organization"
                        in types
                        or "corporation"
                        in types
                        or "localbusiness"
                        in types
                        or "brand"
                        in types
                    ):
                        detected_type = (
                            "PAGE"
                        )

            # --------------------------------------------------------
            # Canonical / OG profile correlation.
            # --------------------------------------------------------

            canonical_has_uid = (
                uid in canonical
                if canonical
                else False
            )

            og_url = clean_text(
                meta.get(
                    "og:url",
                    "",
                )
            )

            og_has_uid = (
                uid in og_url
                if og_url
                else False
            )

            username_match = False

            if username:

                username_lower = (
                    username.lower()
                )

                for candidate_url in (
                    canonical,
                    og_url,
                ):

                    if not candidate_url:
                        continue

                    path = (
                        urlparse(
                            candidate_url
                        ).path
                        .strip("/")
                        .lower()
                    )

                    if path.startswith(
                        username_lower
                    ):
                        username_match = True

            # --------------------------------------------------------
            # Decide entity type.
            # --------------------------------------------------------

            if group_matches:

                detected_type = "GROUP"

            elif page_matches:

                detected_type = "PAGE"

            elif (
                detected_type == "UNKNOWN"
                and (
                    uid_matches
                    or canonical_has_uid
                    or og_has_uid
                )
            ):

                detected_type = "USER"

            # --------------------------------------------------------
            # Verification.
            # --------------------------------------------------------

            strong = (
                len(uid_matches) > 0
            )

            profile_correlation = (
                canonical_has_uid
                or og_has_uid
                or username_match
            )

            if (
                detected_type == "USER"
                and (
                    strong
                    or profile_correlation
                )
            ):

                result["verified"] = True
                result["type"] = "USER"

                result["evidence"].extend(
                    [
                        "profile_identity"
                    ]
                )

                if canonical_has_uid:
                    result["evidence"].append(
                        "canonical"
                    )

                if og_has_uid:
                    result["evidence"].append(
                        "og:url"
                    )

                if username_match:
                    result["evidence"].append(
                        "username"
                    )

                return result

            if detected_type == "PAGE":

                result["type"] = "PAGE"

                result["evidence"].append(
                    "page_identity"
                )

                return result

            if detected_type == "GROUP":

                result["type"] = "GROUP"

                result["evidence"].append(
                    "group_identity"
                )

                return result

        return result

    # =================================================================
    # PUBLISHER TYPE
    # =================================================================

    def infer_publisher_type(
        self,
        shape: URLShape,
        user_uid: Optional[str],
        page_uid: Optional[str],
        group_id: Optional[str],
        verified_type: str,
    ) -> str:

        if group_id:
            return "GROUP"

        if page_uid:
            return "PAGE"

        if verified_type in {
            "USER",
            "PAGE",
            "GROUP",
        }:
            return verified_type

        if shape.url_type == "GROUP_POST":
            return "GROUP"

        if shape.url_type == "PAGE":
            return "PAGE"

        if user_uid:
            return "USER"

        return "UNKNOWN"

    # =================================================================
    # ENTITY TYPE
    # =================================================================

    def infer_entity_type(
        self,
        shape: URLShape,
        result: ResolveResult,
    ) -> str:

        if shape.url_type in {
            "GROUP_POST",
            "GROUP",
        }:
            return "GROUP"

        if shape.url_type == "PAGE":
            return "PAGE"

        if shape.url_type == "REEL":
            return "REEL"

        if shape.url_type == "VIDEO":
            return "VIDEO"

        if shape.url_type == "PHOTO":
            return "PHOTO"

        if shape.url_type == "STORY":
            return "STORY"

        if shape.url_type == "POST":
            return "POST"

        if result.publisher_type == "USER":
            return "USER"

        if result.publisher_type == "PAGE":
            return "PAGE"

        if result.publisher_type == "GROUP":
            return "GROUP"

        return "UNKNOWN"

    # =================================================================
    # BUILD RESULT
    # =================================================================

    def build_result(
        self,
        input_url: str,
    ) -> ResolveResult:

        result = ResolveResult()

        result.input_url = input_url

        if self.snapshots:

            first = self.snapshots[0]

            result.resolved_url = (
                first.final_url
            )

        else:

            result.resolved_url = (
                input_url
            )

        # ------------------------------------------------------------
        # Deduplicate evidence.
        # ------------------------------------------------------------

        unique = {}

        for item in self.evidence:

            key = (
                item.field,
                item.value,
                item.role,
                item.token,
            )

            old = unique.get(
                key
            )

            if (
                old is None
                or item.score > old.score
            ):
                unique[key] = item

        result.evidence = list(
            unique.values()
        )

        # ------------------------------------------------------------
        # Input/final/canonical shape.
        # ------------------------------------------------------------

        shape = self.shape_parser.parse(
            result.resolved_url
        )

        result.url_type = (
            shape.url_type
        )

        canonical_candidates = [
            item.value
            for item in result.evidence
            if item.field
            == "canonical_url"
        ]

        if canonical_candidates:
            result.canonical_url = (
                canonical_candidates[0]
            )

        # ------------------------------------------------------------
        # Object IDs from URL shape.
        # ------------------------------------------------------------

        if shape.post_id:
            result.post_id = (
                shape.post_id
            )

        if shape.video_id:
            result.video_id = (
                shape.video_id
            )

        if shape.reel_id:
            result.reel_id = (
                shape.reel_id
            )

        if shape.photo_id:
            result.photo_id = (
                shape.photo_id
            )

        if shape.story_id:
            result.story_id = (
                shape.story_id
            )

        if shape.album_id:
            result.album_id = (
                shape.album_id
            )

        if shape.group_id:
            result.group_id = (
                shape.group_id
            )

        if shape.page_id:
            result.page_uid = (
                shape.page_id
            )

        if shape.username:
            result.publisher_username = (
                shape.username
            )

        if shape.publisher_id:
            # Only candidate here.
            # NEVER immediately make it USER UID.
            result.publisher_id = (
                shape.publisher_id
            )

        # ------------------------------------------------------------
        # Evidence object IDs.
        # ------------------------------------------------------------

        if not result.post_id:
            result.post_id = (
                self.best_value(
                    {"post_id"}
                )
            )

        if not result.video_id:
            result.video_id = (
                self.best_value(
                    {"video_id"}
                )
            )

        if not result.reel_id:
            result.reel_id = (
                self.best_value(
                    {"reel_id"}
                )
            )

        if not result.photo_id:
            result.photo_id = (
                self.best_value(
                    {"photo_id"}
                )
            )

        if not result.story_id:
            result.story_id = (
                self.best_value(
                    {"story_id"}
                )
            )

        if not result.album_id:
            result.album_id = (
                self.best_value(
                    {"album_id"}
                )
            )

        result.media_fbid = (
            self.best_value(
                {"media_fbid"}
            )
        )

        result.actor_id = (
            self.best_value(
                {"actor_id"},
                numeric_only=True,
            )
        )

        result.entity_id = (
            self.best_value(
                {"entity_id"},
                numeric_only=True,
            )
        )

        # ------------------------------------------------------------
        # Title / publisher name.
        # ------------------------------------------------------------

        result.title = (
            self.best_value(
                {"title"}
            )
            or ""
        )

        result.publisher_name = (
            self.best_value(
                {"publisher_name"}
            )
        )

        if not result.publisher_username:

            result.publisher_username = (
                self.best_value(
                    {"publisher_username"}
                )
            )

        # ------------------------------------------------------------
        # Page / Group role.
        # ------------------------------------------------------------

        page_candidate = self.best_value(
            {"page_id"},
            numeric_only=True,
        )

        group_candidate = self.best_value(
            {"group_id"},
            numeric_only=True,
        )

        if (
            result.url_type
            in {
                "PAGE",
            }
            and page_candidate
        ):
            result.page_uid = (
                page_candidate
            )

        if (
            result.url_type
            in {
                "GROUP",
                "GROUP_POST",
            }
            and group_candidate
        ):
            result.group_id = (
                group_candidate
            )

        # ------------------------------------------------------------
        # USER UID candidates.
        # ------------------------------------------------------------

        uid_scores = defaultdict(int)

        uid_fields = {
            "profile_id",
            "user_id",
            "owner_id",
            "publisher_id",
            "author_id",
        }

        for item in result.evidence:

            if item.field not in uid_fields:
                continue

            if not is_numeric_id(
                item.value
            ):
                continue

            # A publisher ID is only a candidate.
            uid_scores[
                item.value
            ] += item.score

        # Profile URL evidence gets strong bonus.
        for item in result.evidence:

            if item.field != "profile_id":
                continue

            if not is_numeric_id(
                item.value
            ):
                continue

            uid_scores[
                item.value
            ] += 30

        # Independent evidence bonus.
        for uid in list(uid_scores):

            groups = {
                item.independent_group
                for item in result.evidence
                if (
                    item.value == uid
                    and item.field
                    in uid_fields
                    and item.independent_group
                )
            }

            uid_scores[
                uid
            ] += min(
                len(groups) * 15,
                60,
            )

        # ------------------------------------------------------------
        # Prevent known page/group IDs from becoming USER UID.
        # ------------------------------------------------------------

        blocked_uids = set()

        for item in result.evidence:

            if (
                item.field
                in {
                    "page_id",
                    "group_id",
                }
                and is_numeric_id(
                    item.value
                )
            ):

                blocked_uids.add(
                    item.value
                )

        # Only block if there is no stronger user evidence.
        for uid in list(
            uid_scores
        ):

            if uid not in blocked_uids:
                continue

            strong_user = any(
                item.value == uid
                and item.field
                in {
                    "profile_id",
                    "user_id",
                }
                and item.score >= 95
                for item in result.evidence
            )

            if not strong_user:
                uid_scores.pop(
                    uid,
                    None,
                )

        # ------------------------------------------------------------
        # Select candidate.
        # ------------------------------------------------------------

        best_uid = None

        if uid_scores:

            ranked = sorted(
                uid_scores.items(),
                key=lambda x: x[1],
                reverse=True,
            )

            best_uid = ranked[0][0]

            # Conflict detection.
            if len(ranked) >= 2:

                first_score = ranked[0][1]
                second_score = ranked[1][1]

                if (
                    first_score >= 100
                    and second_score >= 100
                    and (
                        first_score
                        - second_score
                    ) <= 12
                ):

                    result.status = (
                        "CONFLICT"
                    )

                    result.warnings.append(
                        "Có nhiều UID ứng viên "
                        "có mức bằng chứng gần nhau."
                    )

                    best_uid = None

        # ------------------------------------------------------------
        # Assign preliminary UID.
        # ------------------------------------------------------------

        if best_uid:

            result.user_uid = (
                best_uid
            )

        # ------------------------------------------------------------
        # Publisher ID should be role-aware.
        # ------------------------------------------------------------

        if result.user_uid:

            result.publisher_id = (
                result.user_uid
            )

        elif result.page_uid:

            result.publisher_id = (
                result.page_uid
            )

        elif result.group_id:

            result.publisher_id = (
                result.group_id
            )

        # ------------------------------------------------------------
        # Decode media_fbid conservatively.
        # ------------------------------------------------------------

        if result.media_fbid:

            decoded = (
                decode_fb_encoded_id(
                    result.media_fbid
                )
            )

            if decoded.get(
                "actor_id"
            ):

                result.actor_id = (
                    decoded[
                        "actor_id"
                    ]
                )

            if decoded.get(
                "object_id"
            ):

                if not result.entity_id:
                    result.entity_id = (
                        decoded[
                            "object_id"
                        ]
                    )

            if decoded.get(
                "decoded_id"
            ):

                result.decoded_id = (
                    decoded[
                        "decoded_id"
                    ]
                )

        # ------------------------------------------------------------
        # Entity role.
        # ------------------------------------------------------------

        verified_type = "UNKNOWN"

        result.publisher_type = (
            self.infer_publisher_type(
                shape,
                result.user_uid,
                result.page_uid,
                result.group_id,
                verified_type,
            )
        )

        result.entity_type = (
            self.infer_entity_type(
                shape,
                result,
            )
        )

        # ------------------------------------------------------------
        # IMPORTANT:
        # Do not show album ID as group ID.
        # Do not show photo ID as page ID.
        # Do not show entity ID as UID.
        # ------------------------------------------------------------

        if (
            result.url_type
            not in {
                "PAGE",
            }
            and shape.page_id is None
            and result.publisher_type
            != "PAGE"
        ):
            # If page evidence exists but object isn't
            # actually page-like, keep only strong page evidence.
            if not any(
                item.field == "page_id"
                and item.score >= 100
                for item in result.evidence
            ):
                result.page_uid = None

        if (
            result.url_type
            not in {
                "GROUP",
                "GROUP_POST",
            }
        ):
            if not any(
                item.field == "group_id"
                and item.score >= 100
                for item in result.evidence
            ):
                result.group_id = None

        # Album ID is always separate.
        # ------------------------------------------------------------

        # ============================================================
        # STATUS BEFORE PROFILE VERIFICATION
        # ============================================================

        if not self.snapshots:

            result.status = (
                "HTTP_FETCH_FAILED"
            )
            result.success = False

        elif result.user_uid:

            result.status = (
                "LIKELY"
            )
            result.success = True

        elif (
            result.page_uid
            or result.group_id
            or result.post_id
            or result.video_id
            or result.reel_id
            or result.photo_id
            or result.story_id
            or result.entity_id
        ):

            result.status = (
                "ENTITY_RESOLVED"
            )
            result.success = True

        else:

            result.status = (
                "NO_PUBLIC_ID_FOUND"
            )
            result.success = False

        # ============================================================
        # CONFIDENCE
        # ============================================================

        confidence = 0

        if result.user_uid:
            confidence += 35

        if result.page_uid:
            confidence += 25

        if result.group_id:
            confidence += 25

        if result.post_id:
            confidence += 10

        if result.video_id:
            confidence += 10

        if result.reel_id:
            confidence += 10

        if result.photo_id:
            confidence += 10

        if result.story_id:
            confidence += 10

        if result.publisher_username:
            confidence += 8

        if result.canonical_url:
            confidence += 8

        if self.snapshots:
            confidence += 8

        result.confidence = min(
            confidence,
            100,
        )

        # ============================================================
        # WARNINGS
        # ============================================================

        for snapshot in (
            self.snapshots
        ):

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

        if (
            result.user_uid
            and not result.user_uid_verified
        ):

            result.warnings.append(
                "UID mới chỉ là ứng viên; "
                "chưa xác minh profile đa nguồn."
            )

        if not result.user_uid:

            result.warnings.append(
                "Không có USER UID đủ bằng chứng "
                "để xác minh."
            )

        if (
            result.resolved_url
            and normalize_url(
                result.input_url
            )
            != normalize_url(
                result.resolved_url
            )
        ):

            result.warnings.append(
                "URL đã được Facebook redirect."
            )

        result.warnings = unique_list(
            result.warnings
        )

        result.crawl_chain = unique_list(
            [
                x.final_url
                for x in self.snapshots
                if x.final_url
            ]
        )

        return result

    # =================================================================
    # FINAL VERIFICATION
    # =================================================================

    async def final_verification(
        self,
        client: HTTPClient,
        result: ResolveResult,
    ):

        if not result.user_uid:
            return

        verify = (
            await self.verify_user_candidate(
                client,
                result.user_uid,
                result.publisher_username,
            )
        )

        if verify["type"] == "PAGE":

            # Candidate is actually a page.
            if result.page_uid is None:
                result.page_uid = (
                    result.user_uid
                )

            result.user_uid = None
            result.user_uid_verified = (
                False
            )

            result.publisher_type = "PAGE"

            result.status = (
                "ENTITY_RESOLVED"
            )

            result.warnings.append(
                "ID ứng viên được xác định là "
                "PAGE, không phải USER UID."
            )

            return

        if verify["type"] == "GROUP":

            result.user_uid = None
            result.user_uid_verified = (
                False
            )

            result.publisher_type = "GROUP"

            result.status = (
                "ENTITY_RESOLVED"
            )

            result.warnings.append(
                "ID ứng viên thuộc GROUP, "
                "không phải USER UID."
            )

            return

        if verify["verified"]:

            result.user_uid_verified = (
                True
            )

            result.publisher_type = (
                "USER"
            )

            result.publisher_id = (
                result.user_uid
            )

            result.status = (
                "VERIFIED"
            )

            result.confidence = max(
                result.confidence,
                96,
            )

            if verify.get(
                "name"
            ):

                result.publisher_name = (
                    verify["name"]
                )

        else:

            # Keep candidate only as likely.
            result.status = (
                "LIKELY"
            )

            result.confidence = min(
                result.confidence,
                79,
            )

    # =================================================================
    # RESOLVE
    # =================================================================

    async def resolve(
        self,
        url: str,
    ) -> ResolveResult:

        original_url = normalize_url(
            url
        )

        if not original_url:

            return ResolveResult(
                input_url=url,
                status="INVALID_URL",
                success=False,
            )

        if not is_fb_host_or_redirect(
            original_url
        ):

            return ResolveResult(
                input_url=original_url,
                resolved_url=original_url,
                url_type="NON_FACEBOOK",
                status="INVALID_FACEBOOK_URL",
                success=False,
                warnings=[
                    "URL không thuộc Facebook."
                ],
            )

        # ------------------------------------------------------------
        # Initial URL
        # ------------------------------------------------------------

        self.analyze_url(
            original_url,
            "input_url",
        )

        client = HTTPClient(
            timeout=self.timeout
        )

        try:

            # --------------------------------------------------------
            # First request
            # --------------------------------------------------------

            first = await self.fetch_one(
                client,
                original_url,
            )

            if not first:

                return self.build_result(
                    original_url
                )

            self.parse_snapshot(
                first
            )

            # --------------------------------------------------------
            # Canonical URL
            # --------------------------------------------------------

            candidate_urls = (
                self.discover_urls(
                    first
                )
            )

            # Canonical has very high priority.
            if first.canonical:

                canonical = normalize_url(
                    urljoin(
                        first.final_url,
                        first.canonical,
                    )
                )

                if canonical:
                    candidate_urls.insert(
                        0,
                        canonical,
                    )

            # Final URL.
            if first.final_url:

                candidate_urls.insert(
                    0,
                    normalize_url(
                        first.final_url
                    ),
                )

            candidate_urls = unique_list(
                candidate_urls
            )

            candidate_urls = [
                x
                for x in candidate_urls
                if x
                and normalize_url(x)
                not in self.visited
            ]

            candidate_urls = candidate_urls[
                : max(
                    0,
                    self.max_pages
                    - len(
                        self.visited
                    ),
                )
            ]

            # --------------------------------------------------------
            # Crawl sequentially in priority order.
            #
            # This is intentionally not an uncontrolled parallel
            # crawl because identity correlation is more important
            # than brute-force page count.
            # --------------------------------------------------------

            for candidate in (
                candidate_urls
            ):

                if len(
                    self.visited
                ) >= self.max_pages:
                    break

                snapshot = await self.fetch_one(
                    client,
                    candidate,
                )

                if not snapshot:
                    continue

                self.parse_snapshot(
                    snapshot
                )

                # Newly discovered canonical URL.
                new_urls = (
                    self.discover_urls(
                        snapshot
                    )
                )

                for new_url in (
                    new_urls
                ):

                    if (
                        new_url
                        not in self.visited
                        and len(
                            self.visited
                        ) < self.max_pages
                    ):

                        # One-hop priority for exact canonical/object
                        # URLs.
                        new_snapshot = (
                            await self.fetch_one(
                                client,
                                new_url,
                            )
                        )

                        if new_snapshot:

                            self.parse_snapshot(
                                new_snapshot
                            )

            # --------------------------------------------------------
            # Build preliminary result.
            # --------------------------------------------------------

            result = self.build_result(
                original_url
            )

            # --------------------------------------------------------
            # User verification.
            # --------------------------------------------------------

            await self.final_verification(
                client,
                result,
            )

            # --------------------------------------------------------
            # Recalculate warnings.
            # --------------------------------------------------------

            result.warnings = unique_list(
                result.warnings
            )

            return result

        finally:

            client.close()


# ====================================================================
# TELEGRAM HTML ESCAPE
# ====================================================================

def tg_escape(
    value: Any,
) -> str:

    return html.escape(
        clean_text(value),
        quote=False,
    )


# ====================================================================
# FORMAT ID
# ====================================================================

def format_id(
    value: Optional[str],
) -> str:

    if not value:
        return "NOT FOUND"

    return (
        "<code>"
        + tg_escape(
            truncate(
                value,
                100,
            )
        )
        + "</code>"
    )


# ====================================================================
# FORMAT RESULT
# ====================================================================

def format_result(
    result: ResolveResult,
    elapsed: float,
) -> str:

    lines = []

    lines.append(
        "<b>🔎 FACEBOOK UID / ENTITY RESOLVER "
        f"{VERSION}</b>"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    status_icon = (
        "✅"
        if result.success
        else "⚠️"
    )

    lines.append(
        f"{status_icon} <b>STATUS:</b> "
        f"<code>{tg_escape(result.status)}</code>"
    )

    lines.append(
        f"🎯 <b>CONFIDENCE:</b> "
        f"<code>{result.confidence}%</code>"
    )

    lines.append(
        f"⏱ <b>TIME:</b> "
        f"<code>{elapsed:.2f}s</code>"
    )

    # ---------------------------------------------------------------
    # URL
    # ---------------------------------------------------------------

    lines.append("")
    lines.append(
        "<b>🌐 URL / ENTITY</b>"
    )

    lines.append(
        f"URL TYPE: "
        f"<code>{tg_escape(result.url_type)}</code>"
    )

    lines.append(
        f"ENTITY TYPE: "
        f"<code>{tg_escape(result.entity_type)}</code>"
    )

    lines.append(
        f"PUBLISHER TYPE: "
        f"<code>{tg_escape(result.publisher_type)}</code>"
    )

    # ---------------------------------------------------------------
    # Publisher
    # ---------------------------------------------------------------

    lines.append("")
    lines.append(
        "<b>👤 PUBLISHER</b>"
    )

    if result.publisher_name:
        lines.append(
            "DISPLAY NAME: "
            + tg_escape(
                truncate(
                    result.publisher_name,
                    300,
                )
            )
        )

    if result.publisher_username:

        lines.append(
            "USERNAME: "
            f"<code>@"
            f"{tg_escape(result.publisher_username)}"
            f"</code>"
        )

    if result.publisher_type == "USER":

        lines.append(
            "USER UID: "
            + format_id(
                result.user_uid
            )
        )

        lines.append(
            "VERIFICATION: "
            + (
                "<code>VERIFIED</code>"
                if result.user_uid_verified
                else "<code>NOT VERIFIED</code>"
            )
        )

    elif result.publisher_type == "PAGE":

        lines.append(
            "PAGE UID: "
            + format_id(
                result.page_uid
            )
        )

    elif result.publisher_type == "GROUP":

        lines.append(
            "GROUP ID: "
            + format_id(
                result.group_id
            )
        )

    else:

        if result.user_uid:

            lines.append(
                "USER UID CANDIDATE: "
                + format_id(
                    result.user_uid
                )
            )

            lines.append(
                "VERIFICATION: "
                "<code>NOT VERIFIED</code>"
            )

        if result.publisher_id:

            lines.append(
                "PUBLISHER ID: "
                + format_id(
                    result.publisher_id
                )
            )

    # ---------------------------------------------------------------
    # Role-clean IDs
    # ---------------------------------------------------------------

    lines.append("")
    lines.append(
        "<b>📦 OBJECT IDS</b>"
    )

    if result.post_id:
        lines.append(
            "📝 POST ID: "
            + format_id(
                result.post_id
            )
        )

    if result.video_id:
        lines.append(
            "🎬 VIDEO ID: "
            + format_id(
                result.video_id
            )
        )

    if result.reel_id:
        lines.append(
            "🎞 REEL ID: "
            + format_id(
                result.reel_id
            )
        )

    if result.photo_id:
        lines.append(
            "🖼 PHOTO ID: "
            + format_id(
                result.photo_id
            )
        )

    if result.story_id:
        lines.append(
            "⭕ STORY ID: "
            + format_id(
                result.story_id
            )
        )

    if result.album_id:
        lines.append(
            "💿 ALBUM ID: "
            + format_id(
                result.album_id
            )
        )

    if result.media_fbid:
        lines.append(
            "🔐 MEDIA FBID: "
            + format_id(
                result.media_fbid
            )
        )

    if result.actor_id:
        lines.append(
            "🎭 ACTOR ID: "
            + format_id(
                result.actor_id
            )
        )

    if result.entity_id:
        lines.append(
            "🎯 ENTITY ID: "
            + format_id(
                result.entity_id
            )
        )

    if result.decoded_id:
        lines.append(
            "🔓 DECODED ID: "
            + format_id(
                result.decoded_id
            )
        )

    if result.page_uid and (
        result.publisher_type != "PAGE"
    ):
        lines.append(
            "📄 PAGE UID: "
            + format_id(
                result.page_uid
            )
        )

    if result.group_id and (
        result.publisher_type != "GROUP"
    ):
        lines.append(
            "👥 GROUP ID: "
            + format_id(
                result.group_id
            )
        )

    # ---------------------------------------------------------------
    # Content
    # ---------------------------------------------------------------

    if result.title:

        lines.append("")
        lines.append(
            "<b>📝 CONTENT</b>"
        )

        lines.append(
            "TITLE: "
            + tg_escape(
                truncate(
                    result.title,
                    500,
                )
            )
        )

    # ---------------------------------------------------------------
    # URLs
    # ---------------------------------------------------------------

    lines.append("")
    lines.append(
        "<b>🔗 URLS</b>"
    )

    lines.append(
        "SOURCE:\n"
        f"<code>{tg_escape(truncate(result.input_url, 700))}</code>"
    )

    if result.resolved_url:

        lines.append(
            "RESOLVED:\n"
            f"<code>{tg_escape(truncate(result.resolved_url, 700))}</code>"
        )

    if result.canonical_url:

        lines.append(
            "CANONICAL:\n"
            f"<code>{tg_escape(truncate(result.canonical_url, 700))}</code>"
        )

    # ---------------------------------------------------------------
    # Verification
    # ---------------------------------------------------------------

    lines.append("")
    lines.append(
        "<b>🔐 VERIFICATION</b>"
    )

    independent = {
        item.independent_group
        for item in result.evidence
        if item.independent_group
    }

    lines.append(
        f"EVIDENCE: "
        f"<code>{len(result.evidence)}</code>"
    )

    lines.append(
        f"INDEPENDENT GROUPS: "
        f"<code>{len(independent)}</code>"
    )

    lines.append(
        f"PAGES: "
        f"<code>{len(result.crawl_chain)}</code>"
    )

    # ---------------------------------------------------------------
    # Warnings
    # ---------------------------------------------------------------

    if result.warnings:

        lines.append("")
        lines.append(
            "<b>⚠️ NOTES</b>"
        )

        for warning in (
            result.warnings[:6]
        ):

            lines.append(
                "• "
                + tg_escape(
                    truncate(
                        warning,
                        280,
                    )
                )
            )

    output = "\n".join(
        lines
    )

    if len(output) > MAX_TELEGRAM_MESSAGE:

        output = (
            output[
                :MAX_TELEGRAM_MESSAGE - 80
            ]
            + "\n\n"
            "⚠️ <i>Output đã được rút gọn "
            "để không vượt giới hạn Telegram.</i>"
        )

    return output


# ====================================================================
# TELEGRAM SESSION MANAGER
# ====================================================================

@dataclass
class ResolverSession:

    chat_id: int
    sender_id: int

    queue: asyncio.Queue = field(
        default_factory=asyncio.Queue
    )

    started_at: float = field(
        default_factory=time.monotonic
    )

    active: bool = True


_ACTIVE_SESSIONS: dict[
    tuple[int, int],
    ResolverSession,
] = {}

_SESSION_LOCK = asyncio.Lock()


# ====================================================================
# SESSION KEY
# ====================================================================

def session_key(
    event,
) -> tuple[int, int]:

    return (
        int(event.chat_id or 0),
        int(event.sender_id or 0),
    )


# ====================================================================
# COMMAND DETECTION
# ====================================================================

KNOWN_EXIT_COMMANDS = {
    "/stop",
    "/cancel",
    "/start",
    "/help",
}


def is_exit_command(
    text: str,
) -> bool:

    text = clean_text(
        text
    )

    if not text.startswith("/"):
        return False

    command = (
        text.split(
            None,
            1,
        )[0]
        .lower()
    )

    command = command.split(
        "@",
        1,
    )[0]

    return command in KNOWN_EXIT_COMMANDS


# ====================================================================
# PROCESS ONE URL
# ====================================================================

async def process_one_url(
    event,
    url: str,
):

    started = time.perf_counter()

    processing = await event.respond(
        "⏳ <b>Đang phân tích Facebook...</b>\n"
        "HTTP public resolver đang thu thập "
        "và đối chiếu bằng chứng.",
        parse_mode="html",
        link_preview=False,
    )

    try:

        resolver = FacebookResolver(
            timeout=DEFAULT_TIMEOUT,
            max_pages=DEFAULT_MAX_PAGES,
            concurrency=DEFAULT_CONCURRENCY,
        )

        result = await resolver.resolve(
            url
        )

        elapsed = (
            time.perf_counter()
            - started
        )

        output = format_result(
            result,
            elapsed,
        )

        await processing.edit(
            output,
            parse_mode="html",
            link_preview=False,
        )

    except Exception as exc:

        elapsed = (
            time.perf_counter()
            - started
        )

        logger.exception(
            "getuidfb resolver error"
        )

        error_text = (
            "❌ <b>RESOLVER ERROR</b>\n\n"
            "ERROR:\n"
            f"<code>{tg_escape(str(exc)[:1500])}</code>\n\n"
            f"TIME: <code>{elapsed:.2f}s</code>"
        )

        try:

            await processing.edit(
                error_text,
                parse_mode="html",
            )

        except Exception:

            await event.respond(
                error_text,
                parse_mode="html",
            )


# ====================================================================
# INPUT ROUTER
# ====================================================================

async def _resolver_input_router(
    event,
):

    if not event.is_private and (
        event.chat_id is None
    ):
        return

    if not event.incoming:
        return

    key = session_key(
        event
    )

    session = _ACTIVE_SESSIONS.get(
        key
    )

    if session is None:
        return

    text = clean_text(
        event.raw_text or ""
    )

    if not text:
        return

    # Commands are sent to the session so
    # getuidfb can terminate cleanly.
    if text.startswith("/"):

        await session.queue.put(
            (
                "command",
                text,
            )
        )

        return

    urls = extract_facebook_urls(
        text
    )

    if urls:

        await session.queue.put(
            (
                "urls",
                urls,
            )
        )

    else:

        await session.queue.put(
            (
                "invalid",
                text,
            )
        )


# ====================================================================
# GETUIDFB COMMAND
# ====================================================================

async def _handle_getuidfb(
    event,
    notify_bot=None,
):

    key = session_key(
        event
    )

    # ---------------------------------------------------------------
    # Prevent duplicate sessions.
    # ---------------------------------------------------------------

    async with _SESSION_LOCK:

        if key in _ACTIVE_SESSIONS:

            await event.respond(
                "⚠️ <b>/getuidfb đang hoạt động.</b>\n\n"
                "Hãy gửi URL Facebook vào phiên hiện tại.\n"
                "Dùng <code>/stop</code> để kết thúc.",
                parse_mode="html",
            )

            return

        session = ResolverSession(
            chat_id=key[0],
            sender_id=key[1],
        )

        _ACTIVE_SESSIONS[
            key
        ] = session

    try:

        await event.respond(
            "<b>🔎 FACEBOOK UID / ENTITY RESOLVER</b>\n\n"
            "Hãy gửi <b>URL Facebook</b>.\n\n"
            "Có thể gửi:\n"
            "• 1 URL\n"
            "• nhiều URL\n"
            "• URL xuống dòng\n"
            "• URL cách nhau bằng khoảng trắng\n"
            "• URL dính liền nhau\n\n"
            "Ví dụ:\n"
            "<code>https://facebook.com/share/p/AAA/"
            "https://facebook.com/user/posts/pfbidBBB</code>\n\n"
            "Bot sẽ tự tách từng URL và xử lý "
            "theo đúng thứ tự.\n\n"
            "Gửi <code>/stop</code> để kết thúc.",
            parse_mode="html",
            link_preview=False,
        )

        while session.active:

            # -------------------------------------------------------
            # Session timeout
            # -------------------------------------------------------

            try:

                item_type, payload = (
                    await asyncio.wait_for(
                        session.queue.get(),
                        timeout=SESSION_TIMEOUT,
                    )
                )

            except asyncio.TimeoutError:

                await event.respond(
                    "⏱ <b>Phiên /getuidfb đã hết thời gian.</b>\n"
                    "Gửi <code>/getuidfb</code> để tạo phiên mới.",
                    parse_mode="html",
                )

                break

            # -------------------------------------------------------
            # Commands
            # -------------------------------------------------------

            if item_type == "command":

                command = (
                    clean_text(
                        payload
                    )
                    .split(
                        None,
                        1,
                    )[0]
                    .lower()
                )

                command = command.split(
                    "@",
                    1,
                )[0]

                if command in {
                    "/stop",
                    "/cancel",
                    "/start",
                    "/help",
                }:

                    session.active = False

                    await event.respond(
                        "🛑 <b>Đã kết thúc phiên "
                        "getuidfb.</b>",
                        parse_mode="html",
                    )

                    break

                # Unknown command:
                # don't try to interpret as Facebook URL.
                continue

            # -------------------------------------------------------
            # Invalid input
            # -------------------------------------------------------

            if item_type == "invalid":

                await event.respond(
                    "❌ Không tìm thấy URL Facebook "
                    "trong tin nhắn.\n"
                    "Hãy gửi URL Facebook hợp lệ.",
                    parse_mode="html",
                )

                continue

            # -------------------------------------------------------
            # URLs
            # -------------------------------------------------------

            if item_type == "urls":

                urls = payload

                if not urls:
                    continue

                await event.respond(
                    f"🔗 <b>Đã nhận diện "
                    f"{len(urls)} URL Facebook.</b>",
                    parse_mode="html",
                )

                for index, url in enumerate(
                    urls,
                    start=1,
                ):

                    # Small progress marker only for
                    # multiple URLs.
                    if len(urls) > 1:

                        await event.respond(
                            f"🔎 <b>URL {index}/{len(urls)}</b>\n"
                            f"<code>{tg_escape(truncate(url, 700))}</code>",
                            parse_mode="html",
                            link_preview=False,
                        )

                    await process_one_url(
                        event,
                        url,
                    )

                await event.respond(
                    f"✅ <b>Đã xử lý xong "
                    f"{len(urls)} URL.</b>\n"
                    "Gửi URL tiếp theo để tiếp tục.",
                    parse_mode="html",
                )

    finally:

        session.active = False

        async with _SESSION_LOCK:

            current = _ACTIVE_SESSIONS.get(
                key
            )

            if current is session:

                _ACTIVE_SESSIONS.pop(
                    key,
                    None,
                )


# ====================================================================
# REGISTER
# ====================================================================

def register(
    bot,
    notify_bot=None,
):

    # ---------------------------------------------------------------
    # Main command.
    #
    # Important:
    # The actual Telethon callback receives ONLY event.
    # ---------------------------------------------------------------

    async def getuidfb_handler(
        event,
    ):

        await _handle_getuidfb(
            event,
            notify_bot,
        )

    bot.add_event_handler(
        getuidfb_handler,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?$",
            incoming=True,
        ),
    )

    # ---------------------------------------------------------------
    # Session input router.
    # ---------------------------------------------------------------

    bot.add_event_handler(
        _resolver_input_router,
        events.NewMessage(
            incoming=True,
        ),
    )

    print(
        f"✅ Registered /getuidfb {VERSION}"
    )


# ====================================================================
# OPTIONAL ALIAS
# ====================================================================

async def getuidfb(
    event,
):

    await _handle_getuidfb(
        event
    )