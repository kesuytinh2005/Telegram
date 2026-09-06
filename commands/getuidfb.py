#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
============================================================
 FACEBOOK PUBLIC FORENSIC RESOLVER V50
============================================================

PUBLIC HTTP ONLY

NO:
    - Facebook Login
    - Cookies
    - Access Token
    - Playwright
    - Selenium
    - Chromium
    - Graph API

GOAL:
    Maximum public-data UID/entity correlation.

CORE RULE:
    NEVER GUESS UID.

IMPORTANT:
    Facebook does not expose the UID in every public URL.
    Therefore this resolver uses evidence correlation and
    WITHHOLDS UID when the public evidence is insufficient.

PIPELINE:

    INPUT
      ↓
    URL EXTRACTION
      ↓
    URL NORMALIZATION
      ↓
    URL CLASSIFICATION
      ↓
    REDIRECT CHAIN
      ↓
    PUBLIC HTML
      ↓
    META
      ↓
    JSON-LD
      ↓
    SCRIPT DATA
      ↓
    RAW EMBEDDED DATA
      ↓
    URL EVIDENCE
      ↓
    OBJECT IDs
      ↓
    USER ID CANDIDATES
      ↓
    ENTITY CLASSIFICATION
      ↓
    PROFILE DISCOVERY
      ↓
    PROFILE FETCH
      ↓
    PROFILE ↔ USERNAME ↔ UID CORRELATION
      ↓
    CONFLICT DETECTION
      ↓
    STRICT VERIFICATION
      ↓
    RESULT

SUPPORTED FAMILIES:

    /username/
    /profile.php?id=UID

    /username/posts/ID
    /username/post/ID

    /username/videos/ID
    /username/videos/slug/ID
    /username/video/ID

    /username/reel/ID
    /username/reels/ID

    /username/photos/ID
    /username/photos/slug/ID
    /username/photo/ID

    /username/story/ID
    /username/stories/ID

    /reel/ID
    /reel/slug/ID

    /watch/?v=ID
    /video.php?v=ID
    /photo.php?fbid=ID
    /story.php?story_fbid=ID&id=UID

    /groups/ID
    /groups/USERNAME/posts/ID
    /groups/ID/permalink/ID

    /pages/NAME/ID
    /events/ID

    /share/p/...
    /share/r/...
    /share/v/...
    fb.watch/...

============================================================
"""

import asyncio
import copy
import html
import json
import logging
import re
import time

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
    unquote,
)

import requests
from telethon import events


# ============================================================
# CONFIG
# ============================================================

REQUEST_TIMEOUT_CONNECT = 6
REQUEST_TIMEOUT_READ = 12

MAX_HTML_BYTES = 14 * 1024 * 1024
MAX_INPUT_URLS = 8
MAX_DISCOVERED_URLS = 120
MAX_PROFILE_CHECKS = 5

MAX_JSON_DEPTH = 22
MAX_JSON_NODES = 50000

MAX_SCRIPT_CHARS = 2_000_000
MAX_EVIDENCE = 3000

CACHE_TTL = 90
SESSION_TIMEOUT = 900

MAX_OUTPUT_CHARS = 3900


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("getuidfb")

if not logger.handlers:
    logger.addHandler(logging.NullHandler())


# ============================================================
# FACEBOOK HOSTS
# ============================================================

FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "mobile.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
    "fb.watch",
}


# ============================================================
# TRACKING PARAMS
# ============================================================

TRACKING_PARAMS = {
    "fbclid",
    "rdid",
    "share_url",
    "refsrc",
    "mibextid",
    "__cft__",
    "__tn__",
    "sfnsn",
    "notif_id",
    "notif_t",
    "hc_ref",
    "ref",
    "refid",
    "referrer",
    "tracking",
    "source_surface",
    "locale",
    "notif_id",
    "notif_t",
}


# ============================================================
# ROUTES
# ============================================================

OBJECT_ROUTES = {
    "videos",
    "video",
    "reel",
    "reels",
    "posts",
    "post",
    "photos",
    "photo",
    "stories",
    "story",
    "permalink",
    "permalink.php",
    "video.php",
    "photo.php",
    "story.php",
    "share",
    "share.php",
    "watch",
}


RESERVED_ROUTES = OBJECT_ROUTES | {
    "groups",
    "pages",
    "events",
    "marketplace",
    "gaming",
    "login",
    "logout",
    "settings",
    "help",
    "privacy",
    "policies",
    "home",
    "friends",
    "messages",
    "notifications",
    "search",
    "directory",
    "hashtag",
    "profile.php",
    "watch",
}


# ============================================================
# USER ID FIELDS
# ============================================================

USER_ID_FIELDS = {
    "user_id": 125,
    "userid": 125,
    "userID": 125,

    "profile_id": 128,
    "profileid": 128,
    "profileID": 128,

    "owner_id": 122,
    "ownerid": 122,
    "ownerID": 122,

    "publisher_id": 118,
    "publisherid": 118,

    "author_id": 118,
    "authorid": 118,

    "creator_id": 122,
    "creatorid": 122,

    "from_id": 115,
    "fromid": 115,

    "actor_id": 108,
    "actorid": 108,

    "page_owner_id": 108,

    "entity_id": 72,
}


# Nested paths that often contain identity.
STRONG_ID_PATHS = {
    "profile.id": 130,
    "profile.uid": 130,

    "owner.id": 125,
    "owner.uid": 125,

    "author.id": 122,
    "author.uid": 122,

    "creator.id": 122,
    "creator.uid": 122,

    "publisher.id": 118,
    "publisher.uid": 118,

    "from.id": 118,
    "from.uid": 118,

    "actor.id": 112,
    "actor.uid": 112,
}


# ============================================================
# OBJECT FIELDS
# ============================================================

OBJECT_ID_FIELDS = {
    "post_id": 130,
    "postid": 130,

    "story_fbid": 130,

    "video_id": 130,
    "videoid": 130,

    "reel_id": 130,
    "reelid": 130,

    "photo_id": 130,
    "photoid": 130,

    "media_fbid": 128,

    "album_id": 120,
    "albumid": 120,

    "group_id": 140,
    "groupid": 140,

    "page_id": 140,
    "pageid": 140,

    "event_id": 140,
    "eventid": 140,
}


# ============================================================
# HTTP HEADERS
# ============================================================

USER_AGENTS = [
    (
        "Mozilla/5.0 (Linux; Android 15; SM-S938B) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
]


def browser_headers() -> Dict[str, str]:
    

    return {
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


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class Evidence:
    value: str
    role: str
    source: str

    key: str = ""
    path: str = ""
    neighbor: str = ""
    url: str = ""

    weight: float = 0.0

    # evidence family / independence
    family: str = ""

    # whether this evidence came from a direct semantic
    # relationship rather than a loose numeric occurrence
    semantic: bool = False


@dataclass
class URLShape:
    original: str
    normalized: str

    host: str = ""
    path: str = ""

    kind: str = "unknown"

    username: str = ""

    numeric_path_id: str = ""

    post_id: str = ""
    video_id: str = ""
    reel_id: str = ""
    photo_id: str = ""
    story_id: str = ""
    album_id: str = ""

    group_id: str = ""
    page_id: str = ""
    event_id: str = ""

    query_id: str = ""

    opaque_token: str = ""

    # extra query identity
    query_uid: str = ""
    query_object_id: str = ""


@dataclass
class PageSnapshot:
    requested_url: str = ""
    final_url: str = ""

    status_code: int = 0
    content_type: str = ""

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

    json_objects: List[Any] = field(
        default_factory=list
    )

    jsonld_objects: List[Any] = field(
        default_factory=list
    )

    html_text: str = ""

    redirect_chain: List[str] = field(
        default_factory=list
    )

    elapsed: float = 0.0

    error: str = ""


@dataclass
class ProfileInfo:
    name: str = ""
    username: str = ""

    profile_url: str = ""

    uid: str = ""

    avatar_url: str = ""
    cover_url: str = ""

    bio: str = ""

    entity_type: str = "UNKNOWN"

    evidence: List[Evidence] = field(
        default_factory=list
    )


@dataclass
class ResolveResult:
    input_url: str

    canonical_url: str = ""
    content_url: str = ""

    profile_url: str = ""

    entity_type: str = "UNKNOWN"
    publisher_type: str = "UNKNOWN"

    name: str = ""
    username: str = ""

    uid: str = ""

    avatar_url: str = ""
    bio: str = ""

    page_name: str = ""
    group_name: str = ""

    content_type: str = ""

    title: str = ""

    post_id: str = ""
    video_id: str = ""
    reel_id: str = ""
    photo_id: str = ""
    story_id: str = ""
    album_id: str = ""

    verified: bool = False

    confidence: float = 0.0

    evidence_sources: int = 0

    signals: List[str] = field(
        default_factory=list
    )

    notes: List[str] = field(
        default_factory=list
    )

    elapsed: float = 0.0

    error: str = ""


# ============================================================
# CACHE
# ============================================================

_CACHE: Dict[
    str,
    Tuple[float, ResolveResult]
] = {}


# ============================================================
# BASIC HELPERS
# ============================================================

def digits(value: Any) -> str:
    if value is None:
        return ""

    value = str(value).strip()

    if re.fullmatch(r"\d{5,30}", value):
        return value

    return ""


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    value = html.unescape(str(value))

    value = value.replace(
        "\u200b",
        "",
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def tg_escape(value: Any) -> str:
    return html.escape(
        clean_text(value),
        quote=True,
    )


def truncate(
    value: str,
    limit: int = 500,
) -> str:

    value = clean_text(value)

    if len(value) <= limit:
        return value

    return (
        value[: limit - 1].rstrip()
        + "…"
    )


def is_generic_name(value: str) -> bool:
    value = clean_text(value)

    if not value:
        return True

    lowered = value.lower()

    generic = {
        "facebook",
        "facebook watch",
        "facebook login",
        "log in",
        "login",
        "sign up",
        "watch",
        "video",
        "videos",
        "photos",
        "photo",
        "home",
        "facebook - log in or sign up",
    }

    return lowered in generic


def normalize_name(value: str) -> str:
    value = clean_text(value)

    if not value:
        return ""

    patterns = [
        r"\s*\|\s*facebook.*$",
        r"\s*-\s*facebook.*$",
        r"\s+on facebook.*$",
        r"\s*'s reel.*$",
        r"\s*’s reel.*$",
        r"\s*'s video.*$",
        r"\s*’s video.*$",
        r"\s*'s post.*$",
        r"\s*’s post.*$",
        r"\s*'s photo.*$",
        r"\s*’s photo.*$",
    ]

    for pattern in patterns:
        value = re.sub(
            pattern,
            "",
            value,
            flags=re.I,
        )

    value = clean_text(value)

    if is_generic_name(value):
        return ""

    return value


def normalize_username(value: str) -> str:
    value = clean_text(value)

    value = value.lstrip("@").strip("/")

    if not value:
        return ""

    if len(value) > 150:
        return ""

    if re.fullmatch(r"\d+", value):
        return ""

    if value.lower() in RESERVED_ROUTES:
        return ""

    if value.lower().startswith(
        "facebook.com"
    ):
        return ""

    return value


def unique_append(
    items: List[str],
    value: str,
    limit: int = 100,
):
    value = clean_text(value)

    if not value:
        return

    if value not in items:
        items.append(value)

    if len(items) > limit:
        del items[limit:]


# ============================================================
# URL NORMALIZATION
# ============================================================

def normalize_url(url: str) -> str:
    url = clean_text(url)

    if not url:
        return ""

    url = unquote(url)

    if not re.match(
        r"^https?://",
        url,
        re.I,
    ):
        url = "https://" + url

    parsed = urlparse(url)

    host = parsed.netloc.lower()

    if "@" in host:
        host = host.split("@")[-1]

    if ":" in host:
        host = host.split(":")[0]

    path = re.sub(
        r"/+",
        "/",
        parsed.path or "/",
    )

    if not path.startswith("/"):
        path = "/" + path

    query_items = []

    for key, value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):

        kl = key.lower()

        if kl in TRACKING_PARAMS:
            continue

        if kl.startswith("utm_"):
            continue

        query_items.append(
            (
                key,
                value,
            )
        )

    return urlunparse(
        (
            "https",
            host,
            path,
            "",
            urlencode(
                query_items,
                doseq=True,
            ),
            "",
        )
    )


def clean_url(url: str) -> str:
    normalized = normalize_url(url)

    if not normalized:
        return ""

    parsed = urlparse(normalized)

    host = parsed.netloc.lower()

    aliases = {
        "m.facebook.com": "www.facebook.com",
        "mbasic.facebook.com": "www.facebook.com",
        "mobile.facebook.com": "www.facebook.com",
        "web.facebook.com": "www.facebook.com",
        "touch.facebook.com": "www.facebook.com",
    }

    host = aliases.get(
        host,
        host,
    )

    path = re.sub(
        r"/+",
        "/",
        parsed.path or "/",
    )

    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    query_items = []

    for key, value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):

        kl = key.lower()

        if kl in TRACKING_PARAMS:
            continue

        if kl.startswith("utm_"):
            continue

        query_items.append(
            (
                key,
                value,
            )
        )

    return urlunparse(
        (
            "https",
            host,
            path,
            "",
            urlencode(
                query_items,
                doseq=True,
            ),
            "",
        )
    )


# ============================================================
# URL EXTRACTION
# ============================================================

FACEBOOK_URL_RE = re.compile(
    r"https?://"
    r"(?:"
    r"(?:www|m|mbasic|mobile|web|touch)"
    r"\.facebook\.com"
    r"|fb\.watch"
    r")"
    r"(?:/[^\s<>\"]*)?",
    re.I,
)


def extract_facebook_urls(
    text: str,
) -> List[str]:

    if not text:
        return []

    found: List[str] = []

    found.extend(
        FACEBOOK_URL_RE.findall(
            text
        )
    )

    # Handle URLs separated by punctuation.
    for token in re.split(
        r"[\s,;]+",
        text,
    ):

        token = token.strip()

        if (
            "facebook.com/"
            in token.lower()
            or "fb.watch/"
            in token.lower()
        ):
            found.append(token)

    result = []
    seen = set()

    for item in found:

        item = item.strip(
            ".,);]}>'\""
        )

        item = clean_url(item)

        if not item:
            continue

        parsed = urlparse(item)

        host = parsed.netloc.lower()

        if host not in FACEBOOK_HOSTS:
            continue

        key = item.lower().rstrip("/")

        if key in seen:
            continue

        seen.add(key)

        result.append(item)

        if len(result) >= MAX_INPUT_URLS:
            break

    return result


# ============================================================
# NUMERIC SEGMENT HELPERS
# ============================================================

def numeric_segments(
    segments: List[str],
) -> List[str]:

    return [
        x
        for x in segments
        if digits(x)
    ]


def first_numeric_after(
    segments: List[str],
    route_index: int,
) -> str:

    for value in segments[
        route_index + 1:
    ]:

        value = digits(value)

        if value:
            return value

    return ""


# ============================================================
# URL CLASSIFICATION
# ============================================================

def classify_url(
    url: str,
) -> URLShape:

    normalized = clean_url(url)

    parsed = urlparse(normalized)

    host = parsed.netloc.lower()

    path = parsed.path or "/"

    segments = [
        unquote(x).strip()
        for x in path.split("/")
        if x.strip()
    ]

    shape = URLShape(
        original=url,
        normalized=normalized,
        host=host,
        path=path,
    )

    query = dict(
        parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
    )

    q_id = (
        digits(query.get("id"))
        or digits(query.get("user_id"))
        or digits(query.get("profile_id"))
    )

    shape.query_id = q_id

    shape.query_uid = q_id

    shape.query_object_id = (
        digits(query.get("fbid"))
        or digits(query.get("story_fbid"))
        or digits(query.get("media_fbid"))
        or digits(query.get("post_id"))
        or digits(query.get("video_id"))
        or digits(query.get("reel_id"))
        or digits(query.get("photo_id"))
        or digits(query.get("v"))
    )

    # --------------------------------------------------------
    # FB WATCH
    # --------------------------------------------------------

    if host == "fb.watch":

        shape.kind = "video"

        shape.opaque_token = (
            segments[0]
            if segments
            else ""
        )

        return shape

    # --------------------------------------------------------
    # ROOT
    # --------------------------------------------------------

    if not segments:

        shape.kind = "home"

        return shape

    first = segments[0].lower()

    # --------------------------------------------------------
    # PROFILE.PHP
    # --------------------------------------------------------

    if first == "profile.php":

        shape.kind = "profile"

        if q_id:
            shape.numeric_path_id = q_id

        return shape

    # --------------------------------------------------------
    # VIDEO.PHP
    # --------------------------------------------------------

    if first == "video.php":

        shape.kind = "video"

        shape.video_id = (
            digits(query.get("v"))
            or digits(
                query.get("video_id")
            )
            or digits(query.get("id"))
        )

        return shape

    # --------------------------------------------------------
    # PHOTO.PHP
    # --------------------------------------------------------

    if first == "photo.php":

        shape.kind = "photo"

        shape.photo_id = (
            digits(query.get("fbid"))
            or digits(
                query.get("photo_id")
            )
            or digits(query.get("id"))
        )

        return shape

    # --------------------------------------------------------
    # STORY.PHP
    # --------------------------------------------------------

    if first == "story.php":

        shape.kind = "story"

        shape.story_id = (
            digits(
                query.get("story_fbid")
            )
            or digits(
                query.get("id")
            )
        )

        return shape

    # --------------------------------------------------------
    # WATCH
    # --------------------------------------------------------

    if first == "watch":

        shape.kind = "video"

        shape.video_id = (
            digits(query.get("v"))
            or digits(
                query.get("video_id")
            )
        )

        return shape

    # --------------------------------------------------------
    # GROUPS
    # --------------------------------------------------------

    if first == "groups":

        shape.kind = "group"

        if len(segments) >= 2:

            second = segments[1]

            if digits(second):
                shape.group_id = second
            else:
                shape.username = (
                    normalize_username(
                        second
                    )
                )

        # Search all numeric segments.
        nums = numeric_segments(
            segments
        )

        if nums and not shape.group_id:
            shape.group_id = nums[0]

        # group / posts / object
        for i, seg in enumerate(
            segments
        ):

            route = seg.lower()

            if route in {
                "posts",
                "post",
                "permalink",
            }:

                candidate = (
                    first_numeric_after(
                        segments,
                        i,
                    )
                )

                if candidate:
                    shape.post_id = candidate

        return shape

    # --------------------------------------------------------
    # PAGES
    # --------------------------------------------------------

    if first == "pages":

        shape.kind = "page"

        if len(segments) >= 2:

            second = segments[1]

            if not digits(second):
                shape.username = (
                    normalize_username(
                        second
                    )
                )

        nums = numeric_segments(
            segments
        )

        if nums:
            shape.page_id = nums[-1]

        return shape

    # --------------------------------------------------------
    # EVENTS
    # --------------------------------------------------------

    if first == "events":

        shape.kind = "event"

        nums = numeric_segments(
            segments
        )

        if nums:
            shape.event_id = nums[0]

        return shape

    # --------------------------------------------------------
    # NUMERIC FIRST SEGMENT
    #
    # NEVER automatically call it UID.
    # --------------------------------------------------------

    if digits(first):

        shape.numeric_path_id = first

        if len(segments) >= 2:

            route = segments[1].lower()

            if route in {
                "videos",
                "video",
            }:

                shape.kind = "video"

                shape.video_id = (
                    first_numeric_after(
                        segments,
                        1,
                    )
                )

                return shape

            if route in {
                "reel",
                "reels",
            }:

                shape.kind = "reel"

                shape.reel_id = (
                    first_numeric_after(
                        segments,
                        1,
                    )
                )

                return shape

            if route in {
                "posts",
                "post",
            }:

                shape.kind = "post"

                shape.post_id = (
                    first_numeric_after(
                        segments,
                        1,
                    )
                )

                return shape

            if route in {
                "photos",
                "photo",
            }:

                shape.kind = "photo"

                shape.photo_id = (
                    first_numeric_after(
                        segments,
                        1,
                    )
                )

                return shape

        shape.kind = "numeric_profile_or_object"

        return shape

    # --------------------------------------------------------
    # USERNAME + OBJECT ROUTE
    # --------------------------------------------------------

    shape.username = normalize_username(
        first
    )

    if len(segments) >= 2:

        route = segments[1].lower()

        if route in {
            "videos",
            "video",
        }:

            shape.kind = "video"

            shape.video_id = (
                first_numeric_after(
                    segments,
                    1,
                )
            )

            return shape

        if route in {
            "reel",
            "reels",
        }:

            shape.kind = "reel"

            shape.reel_id = (
                first_numeric_after(
                    segments,
                    1,
                )
            )

            return shape

        if route in {
            "posts",
            "post",
        }:

            shape.kind = "post"

            shape.post_id = (
                first_numeric_after(
                    segments,
                    1,
                )
            )

            return shape

        if route in {
            "photos",
            "photo",
        }:

            shape.kind = "photo"

            shape.photo_id = (
                first_numeric_after(
                    segments,
                    1,
                )
            )

            return shape

        if route in {
            "stories",
            "story",
        }:

            shape.kind = "story"

            shape.story_id = (
                first_numeric_after(
                    segments,
                    1,
                )
            )

            return shape

    # --------------------------------------------------------
    # PLAIN PROFILE
    # --------------------------------------------------------

    if len(segments) == 1:

        shape.kind = "profile"

        return shape

    shape.kind = "unknown"

    return shape


# ============================================================
# PROFILE URL
# ============================================================

def profile_base_from_url(
    url: str,
) -> str:

    url = clean_url(url)

    if not url:
        return ""

    shape = classify_url(url)

    if shape.kind == "profile":

        parsed = urlparse(url)

        segments = [
            unquote(x)
            for x in parsed.path.split("/")
            if x.strip()
        ]

        if len(segments) == 1:

            first = segments[0]

            if (
                not digits(first)
                and first.lower()
                not in RESERVED_ROUTES
            ):

                username = normalize_username(
                    first
                )

                if username:
                    return (
                        "https://www.facebook.com/"
                        + username
                    )

        # profile.php?id=UID has no username.
        return ""

    if shape.kind in {
        "group",
        "page",
        "event",
    }:
        return ""

    parsed = urlparse(url)

    segments = [
        unquote(x)
        for x in parsed.path.split("/")
        if x.strip()
    ]

    if not segments:
        return ""

    first = segments[0]

    if digits(first):
        return ""

    if first.lower() in RESERVED_ROUTES:
        return ""

    username = normalize_username(
        first
    )

    if not username:
        return ""

    return (
        "https://www.facebook.com/"
        + username
    )


# ============================================================
# HTML PARSER
# ============================================================

class FacebookHTMLParser(
    HTMLParser
):

    def __init__(self):

        super().__init__(
            convert_charrefs=True
        )

        self.meta = {}

        self.links = []

        self.scripts = []

        self._script_data = []
        self._script_type = ""

        self._title_data = []

        self.in_title = False

    def handle_starttag(
        self,
        tag,
        attrs,
    ):

        attrs_dict = dict(attrs)

        tag = tag.lower()

        if tag == "meta":

            key = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or attrs_dict.get("itemprop")
            )

            content = attrs_dict.get(
                "content"
            )

            if key and content:

                self.meta[
                    key.strip().lower()
                ] = clean_text(content)

        elif tag == "a":

            href = attrs_dict.get(
                "href"
            )

            if href:
                self.links.append(
                    href
                )

        elif tag == "script":

            self._script_type = (
                attrs_dict.get(
                    "type"
                )
                or ""
            ).lower()

            self._script_data = []

        elif tag == "title":

            self.in_title = True

            self._title_data = []

    def handle_endtag(
        self,
        tag,
    ):

        tag = tag.lower()

        if tag == "script":

            data = "".join(
                self._script_data
            )

            if data.strip():

                if len(data) > MAX_SCRIPT_CHARS:
                    data = data[
                        :MAX_SCRIPT_CHARS
                    ]

                self.scripts.append(
                    data
                )

            self._script_type = ""

            self._script_data = []

        elif tag == "title":

            self.in_title = False

    def handle_data(
        self,
        data,
    ):

        if self.in_title:
            self._title_data.append(
                data
            )

        if self._script_type:
            self._script_data.append(
                data
            )

    @property
    def title(self):

        return normalize_name(
            "".join(
                self._title_data
            )
        )


# ============================================================
# JSON PARSER
# ============================================================

def parse_json_safely(
    text: str,
) -> Optional[Any]:

    text = text.strip()

    if not text:
        return None

    try:
        return json.loads(text)
    except Exception:
        pass

    cleaned = (
        text
        .replace("\\/", "/")
        .replace(
            "&quot;",
            '"',
        )
    )

    try:
        return json.loads(
            cleaned
        )
    except Exception:
        return None


def extract_json_objects(
    parser: FacebookHTMLParser,
) -> Tuple[
    List[Any],
    List[Any],
]:

    json_objects = []
    jsonld_objects = []

    for data in parser.scripts:

        obj = parse_json_safely(
            data
        )

        if obj is None:
            continue

        # Detect JSON-LD approximately.
        if (
            '"@context"' in data
            or '"@type"' in data
        ):
            jsonld_objects.append(
                obj
            )
        else:
            json_objects.append(
                obj
            )

    return (
        json_objects,
        jsonld_objects,
    )


# ============================================================
# JSON WALKER
# ============================================================

def walk_json(
    obj: Any,
    path: str = "$",
    depth: int = 0,
    nodes: Optional[List[int]] = None,
):

    if nodes is None:
        nodes = [0]

    if depth > MAX_JSON_DEPTH:
        return

    nodes[0] += 1

    if nodes[0] > MAX_JSON_NODES:
        return

    yield obj, path

    if isinstance(obj, dict):

        for key, value in obj.items():

            next_path = (
                f"{path}.{key}"
            )

            yield from walk_json(
                value,
                next_path,
                depth + 1,
                nodes,
            )

    elif isinstance(obj, list):

        for index, value in enumerate(
            obj
        ):

            next_path = (
                f"{path}[{index}]"
            )

            yield from walk_json(
                value,
                next_path,
                depth + 1,
                nodes,
            )


# ============================================================
# KEY NORMALIZATION
# ============================================================

def normalize_key(
    key: Any,
) -> str:

    return (
        str(key)
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


# ============================================================
# SEMANTIC EXTRACTOR
# ============================================================

class SemanticExtractor:

    NAME_KEYS = {
        "name",
        "display_name",
        "displayname",
        "full_name",
        "fullname",
        "short_name",
        "shortname",
    }

    USERNAME_KEYS = {
        "username",
        "user_name",
        "screen_name",
        "screenname",
        "vanity",
        "handle",
    }

    IMAGE_KEYS = {
        "avatar",
        "avatar_url",
        "profile_picture",
        "profile_pic",
        "profilepic",
        "profile_picture_url",
        "profile_pic_url",
        "image",
    }

    BIO_KEYS = {
        "bio",
        "description",
        "about",
    }

    def extract(
        self,
        snapshot: PageSnapshot,
    ) -> List[Evidence]:

        evidence: List[
            Evidence
        ] = []

        def add(
            value,
            role,
            source,
            key="",
            path="",
            neighbor="",
            weight=0,
            family="",
            semantic=False,
        ):

            value = clean_text(value)

            if not value:
                return

            evidence.append(
                Evidence(
                    value=value,
                    role=role,
                    source=source,
                    key=key,
                    path=path,
                    neighbor=neighbor,
                    url=(
                        snapshot.final_url
                        or snapshot.requested_url
                    ),
                    weight=weight,
                    family=family
                    or source,
                    semantic=semantic,
                )
            )

        # ====================================================
        # META
        # ====================================================

        for key, value in snapshot.meta.items():

            kl = key.lower()

            if kl in {
                "og:title",
                "twitter:title",
            }:

                name = normalize_name(
                    value
                )

                if name:

                    add(
                        name,
                        "NAME",
                        "meta",
                        key,
                        weight=80,
                        family="meta",
                    )

            elif kl in {
                "og:description",
                "twitter:description",
            }:

                add(
                    truncate(
                        value,
                        1000,
                    ),
                    "DESCRIPTION",
                    "meta",
                    key,
                    weight=60,
                    family="meta",
                )

            elif kl in {
                "og:image",
                "twitter:image",
            }:

                add(
                    value,
                    "IMAGE",
                    "meta",
                    key,
                    weight=60,
                    family="meta",
                )

            elif kl in {
                "og:url",
                "twitter:url",
            }:

                add(
                    value,
                    "CANONICAL",
                    "meta",
                    key,
                    weight=90,
                    family="meta",
                    semantic=True,
                )

        # ====================================================
        # PARSED JSON
        # ====================================================

        for obj in snapshot.json_objects:

            for value, path in walk_json(
                obj
            ):

                if not isinstance(
                    value,
                    dict,
                ):
                    continue

                local = list(
                    value.items()
                )

                names = []

                for k, v in local:

                    nk = normalize_key(k)

                    if (
                        nk
                        in self.NAME_KEYS
                        and isinstance(
                            v,
                            str,
                        )
                    ):

                        names.append(
                            clean_text(v)
                        )

                neighbor = " ".join(
                    names
                )[:500]

                for key, raw in local:

                    nk = normalize_key(
                        key
                    )

                    raw_str = ""

                    if isinstance(
                        raw,
                        (
                            str,
                            int,
                            float,
                        ),
                    ):

                        raw_str = str(
                            raw
                        )

                    # ----------------------------------------
                    # USER IDS
                    # ----------------------------------------

                    if (
                        nk
                        in {
                            normalize_key(x)
                            for x
                            in USER_ID_FIELDS
                        }
                    ):

                        value_id = digits(
                            raw_str
                        )

                        if value_id:

                            weight = (
                                USER_ID_FIELDS.get(
                                    key,
                                    USER_ID_FIELDS.get(
                                        nk,
                                        70,
                                    ),
                                )
                            )

                            add(
                                value_id,
                                "USER_CANDIDATE",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor,
                                weight,
                                "json_semantic",
                                True,
                            )

                    # ----------------------------------------
                    # OBJECT IDS
                    # ----------------------------------------

                    if (
                        nk
                        in {
                            normalize_key(x)
                            for x
                            in OBJECT_ID_FIELDS
                        }
                    ):

                        value_id = digits(
                            raw_str
                        )

                        if value_id:

                            weight = (
                                OBJECT_ID_FIELDS.get(
                                    key,
                                    OBJECT_ID_FIELDS.get(
                                        nk,
                                        70,
                                    ),
                                )
                            )

                            add(
                                value_id,
                                "OBJECT_CANDIDATE",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor,
                                weight,
                                "json_object",
                                True,
                            )

                    # ----------------------------------------
                    # NAME
                    # ----------------------------------------

                    if (
                        nk
                        in {
                            normalize_key(x)
                            for x
                            in self.NAME_KEYS
                        }
                    ):

                        name = normalize_name(
                            raw_str
                        )

                        if name:

                            add(
                                name,
                                "NAME",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor,
                                70,
                                "json_semantic",
                                True,
                            )

                    # ----------------------------------------
                    # USERNAME
                    # ----------------------------------------

                    if (
                        nk
                        in {
                            normalize_key(x)
                            for x
                            in self.USERNAME_KEYS
                        }
                    ):

                        username = (
                            normalize_username(
                                raw_str
                            )
                        )

                        if username:

                            add(
                                username,
                                "USERNAME",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor,
                                85,
                                "json_semantic",
                                True,
                            )

                    # ----------------------------------------
                    # IMAGE
                    # ----------------------------------------

                    if (
                        nk
                        in {
                            normalize_key(x)
                            for x
                            in self.IMAGE_KEYS
                        }
                    ):

                        if raw_str.startswith(
                            "http"
                        ):

                            add(
                                raw_str,
                                "AVATAR",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor,
                                65,
                                "json_media",
                            )

                    # ----------------------------------------
                    # BIO
                    # ----------------------------------------

                    if (
                        nk
                        in {
                            normalize_key(x)
                            for x
                            in self.BIO_KEYS
                        }
                    ):

                        if raw_str:

                            add(
                                truncate(
                                    raw_str,
                                    1000,
                                ),
                                "BIO",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor,
                                60,
                                "json_semantic",
                            )

        # ====================================================
        # JSON-LD
        # ====================================================

        for obj in snapshot.jsonld_objects:

            for value, path in walk_json(
                obj
            ):

                if not isinstance(
                    value,
                    dict,
                ):
                    continue

                for key, raw in value.items():

                    nk = normalize_key(
                        key
                    )

                    if isinstance(
                        raw,
                        (
                            str,
                            int,
                            float,
                        ),
                    ):

                        raw_str = str(
                            raw
                        )

                    elif isinstance(
                        raw,
                        dict,
                    ):

                        raw_str = str(
                            raw.get("url")
                            or raw.get("@id")
                            or raw.get("name")
                            or ""
                        )

                    else:
                        continue

                    if nk == "name":

                        name = normalize_name(
                            raw_str
                        )

                        if name:

                            add(
                                name,
                                "NAME",
                                "jsonld",
                                str(key),
                                path,
                                weight=85,
                                family="jsonld",
                            )

                    elif nk in {
                        "url",
                        "@id",
                    }:

                        if (
                            "facebook.com"
                            in raw_str.lower()
                        ):

                            add(
                                raw_str,
                                "PROFILE_URL",
                                "jsonld",
                                str(key),
                                path,
                                weight=85,
                                family="jsonld",
                                semantic=True,
                            )

                    elif nk == "description":

                        add(
                            truncate(
                                raw_str,
                                1000,
                            ),
                            "DESCRIPTION",
                            "jsonld",
                            str(key),
                            path,
                            weight=65,
                            family="jsonld",
                        )

        # ====================================================
        # RAW HTML SEMANTIC SCANNER
        # ====================================================

        evidence.extend(
            scan_raw_html_evidence(
                snapshot
            )
        )

        return evidence


# ============================================================
# RAW HTML EVIDENCE
# ============================================================

def scan_raw_html_evidence(
    snapshot: PageSnapshot,
) -> List[Evidence]:

    result = []

    text = snapshot.html_text

    if not text:
        return result

    url = (
        snapshot.final_url
        or snapshot.requested_url
    )

    # ========================================================
    # ID KEYS
    #
    # Supports:
    #
    # "creator_id":"123"
    # \"creator_id\":\"123\"
    # creator_id: "123"
    # creator_id = "123"
    # ========================================================

    user_keys = (
        "user_id|userid|userID|"
        "profile_id|profileid|profileID|"
        "owner_id|ownerid|ownerID|"
        "publisher_id|publisherid|"
        "author_id|authorid|"
        "creator_id|creatorid|"
        "from_id|fromid|"
        "actor_id|actorid|"
        "page_owner_id"
    )

    object_keys = (
        "post_id|postid|"
        "story_fbid|"
        "video_id|videoid|"
        "reel_id|reelid|"
        "photo_id|photoid|"
        "media_fbid|"
        "album_id|albumid|"
        "group_id|groupid|"
        "page_id|pageid|"
        "event_id|eventid"
    )

    user_pattern = re.compile(
        rf"""(?:"|\\")?
        ({user_keys})
        (?:"|\\")?
        \s*
        (?:[:=]|\\":)
        \s*
        (?:"|\\")?
        (\d{{5,30}})
        (?:"|\\")?
        """,
        re.I | re.X,
    )

    object_pattern = re.compile(
        rf"""(?:"|\\")?
        ({object_keys})
        (?:"|\\")?
        \s*
        (?:[:=]|\\":)
        \s*
        (?:"|\\")?
        (\d{{5,30}})
        (?:"|\\")?
        """,
        re.I | re.X,
    )

    for match in user_pattern.finditer(
        text
    ):

        key = match.group(1)
        value = match.group(2)

        start = max(
            0,
            match.start() - 350,
        )

        end = min(
            len(text),
            match.end() + 350,
        )

        neighbor = clean_text(
            text[start:end]
        )

        result.append(
            Evidence(
                value=value,
                role="USER_CANDIDATE",
                source="raw_html",
                key=key,
                neighbor=truncate(
                    neighbor,
                    600,
                ),
                url=url,
                weight=92,
                family="raw_html",
                semantic=True,
            )
        )

    for match in object_pattern.finditer(
        text
    ):

        key = match.group(1)
        value = match.group(2)

        result.append(
            Evidence(
                value=value,
                role="OBJECT_CANDIDATE",
                source="raw_html",
                key=key,
                url=url,
                weight=95,
                family="raw_html",
                semantic=True,
            )
        )

    # ========================================================
    # NESTED OBJECT:
    #
    # "creator":{"id":"123"}
    # "owner":{"id":"123"}
    # "author":{"id":"123"}
    # "profile":{"id":"123"}
    # ========================================================

    nested_pattern = re.compile(
        r"""
        (?:
            creator|
            owner|
            author|
            publisher|
            profile|
            actor|
            from
        )
        \s*
        [:{]
        .{0,500}?
        (?:
            ["']id["']|
            ["']uid["']|
            \bid\b|
            \buid\b
        )
        \s*
        :
        \s*
        ["']?
        (\d{5,30})
        ["']?
        """,
        re.I | re.X | re.S,
    )

    for match in nested_pattern.finditer(
        text
    ):

        value = match.group(1)

        context = clean_text(
            text[
                max(
                    0,
                    match.start() - 250,
                ):
                min(
                    len(text),
                    match.end() + 250,
                )
            ]
        )

        key = "nested.id"

        lowered = context.lower()

        if "creator" in lowered:
            key = "creator.id"
        elif "owner" in lowered:
            key = "owner.id"
        elif "author" in lowered:
            key = "author.id"
        elif "publisher" in lowered:
            key = "publisher.id"
        elif "profile" in lowered:
            key = "profile.id"
        elif "actor" in lowered:
            key = "actor.id"
        elif "from" in lowered:
            key = "from.id"

        weight = STRONG_ID_PATHS.get(
            key,
            105,
        )

        result.append(
            Evidence(
                value=value,
                role="USER_CANDIDATE",
                source="html_semantic",
                key=key,
                neighbor=truncate(
                    context,
                    600,
                ),
                url=url,
                weight=weight,
                family="html_semantic",
                semantic=True,
            )
        )

    return result


# ============================================================
# FETCH ENGINE
# ============================================================

class FetchEngine:

    def __init__(self):

        self._semaphore = (
            asyncio.Semaphore(4)
        )

    def fetch_sync(
        self,
        url: str,
    ) -> PageSnapshot:

        started = time.perf_counter()

        snapshot = PageSnapshot(
            requested_url=url
        )

        session = requests.Session()

        try:

            response = session.get(
                url,
                headers=browser_headers(),
                timeout=(
                    REQUEST_TIMEOUT_CONNECT,
                    REQUEST_TIMEOUT_READ,
                ),
                allow_redirects=True,
                stream=True,
            )

            snapshot.status_code = (
                response.status_code
            )

            snapshot.content_type = (
                response.headers.get(
                    "content-type",
                    "application/x-www-form-urlencoded",
                )
            )

            snapshot.final_url = (
                response.url
            )

            snapshot.redirect_chain = [
                r.url
                for r in response.history
            ]

            chunks = []

            total = 0

            for chunk in response.iter_content(
                chunk_size=65536
            ):

                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_HTML_BYTES:
                    break

                chunks.append(chunk)

            raw = b"".join(
                chunks
            )

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

            snapshot.html_text = text

            parser = (
                FacebookHTMLParser()
            )

            try:

                parser.feed(text)

            except Exception as exc:

                logger.debug(
                    "HTML parser error: %s",
                    exc,
                )

            snapshot.meta = (
                parser.meta
            )

            snapshot.links = (
                parser.links[:3000]
            )

            snapshot.scripts = (
                parser.scripts
            )

            snapshot.title = (
                parser.title
            )

            (
                snapshot.json_objects,
                snapshot.jsonld_objects,
            ) = extract_json_objects(
                parser
            )

        except requests.RequestException as exc:

            snapshot.error = (
                f"{type(exc).__name__}: {exc}"
            )

        except Exception as exc:

            snapshot.error = (
                f"{type(exc).__name__}: {exc}"
            )

        finally:

            snapshot.elapsed = (
                time.perf_counter()
                - started
            )

            try:
                session.close()
            except Exception:
                pass

        return snapshot

    async def fetch(
        self,
        url: str,
    ) -> PageSnapshot:

        async with self._semaphore:

            return await asyncio.to_thread(
                self.fetch_sync,
                url,
            )


# ============================================================
# URL EVIDENCE
# ============================================================

def url_evidence(
    snapshot: PageSnapshot,
) -> List[Evidence]:

    result = []

    final_url = (
        snapshot.final_url
        or snapshot.requested_url
    )

    shape = classify_url(
        final_url
    )

    # --------------------------------------------------------
    # USERNAME
    # --------------------------------------------------------

    if shape.username:

        result.append(
            Evidence(
                value=shape.username,
                role="USERNAME",
                source="url",
                key="path",
                url=final_url,
                weight=90,
                family="url",
                semantic=True,
            )
        )

    # --------------------------------------------------------
    # profile.php?id=UID
    #
    # Explicit route evidence.
    # --------------------------------------------------------

    if (
        shape.kind == "profile"
        and shape.numeric_path_id
    ):

        result.append(
            Evidence(
                value=shape.numeric_path_id,
                role="PROFILE_ROUTE_UID",
                source="url",
                key="profile.php?id",
                url=final_url,
                weight=125,
                family="url_profile",
                semantic=True,
            )
        )

        result.append(
            Evidence(
                value=shape.numeric_path_id,
                role="USER_CANDIDATE",
                source="url_profile_route",
                key="profile.php?id",
                url=final_url,
                weight=125,
                family="url_profile",
                semantic=True,
            )
        )

    # --------------------------------------------------------
    # story.php?id=UID
    #
    # id parameter commonly identifies publisher/owner.
    # Keep it as author candidate, not generic UID.
    # --------------------------------------------------------

    if (
        shape.kind == "story"
        and shape.query_uid
    ):

        result.append(
            Evidence(
                value=shape.query_uid,
                role="USER_CANDIDATE",
                source="url_story_owner",
                key="story.php?id",
                url=final_url,
                weight=110,
                family="url_story",
                semantic=True,
            )
        )

    # --------------------------------------------------------
    # numeric route entity
    #
    # NOT automatically UID.
    # --------------------------------------------------------

    if shape.numeric_path_id:

        result.append(
            Evidence(
                value=shape.numeric_path_id,
                role="ROUTE_ENTITY",
                source="url",
                key="numeric_path",
                url=final_url,
                weight=35,
                family="url_route",
            )
        )

    # --------------------------------------------------------
    # OBJECTS
    # --------------------------------------------------------

    object_rows = [
        (
            "POST_ID",
            shape.post_id,
        ),
        (
            "VIDEO_ID",
            shape.video_id,
        ),
        (
            "REEL_ID",
            shape.reel_id,
        ),
        (
            "PHOTO_ID",
            shape.photo_id,
        ),
        (
            "STORY_ID",
            shape.story_id,
        ),
        (
            "ALBUM_ID",
            shape.album_id,
        ),
        (
            "GROUP_ID",
            shape.group_id,
        ),
        (
            "PAGE_ID",
            shape.page_id,
        ),
        (
            "EVENT_ID",
            shape.event_id,
        ),
    ]

    for role, value in object_rows:

        if not value:
            continue

        result.append(
            Evidence(
                value=value,
                role=role,
                source="url",
                key=role.lower(),
                url=final_url,
                weight=125,
                family="url_object",
                semantic=True,
            )
        )

    return result


# ============================================================
# META EVIDENCE
# ============================================================

def meta_evidence(
    snapshot: PageSnapshot,
) -> List[Evidence]:

    result = []

    for key, value in snapshot.meta.items():

        if not value:
            continue

        if key in {
            "og:title",
            "twitter:title",
        }:

            name = normalize_name(
                value
            )

            if name:

                result.append(
                    Evidence(
                        value=name,
                        role="NAME",
                        source="meta",
                        key=key,
                        url=snapshot.final_url,
                        weight=80,
                        family="meta",
                    )
                )

        elif key in {
            "og:url",
            "twitter:url",
        }:

            result.append(
                Evidence(
                    value=value,
                    role="CANONICAL",
                    source="meta",
                    key=key,
                    url=snapshot.final_url,
                    weight=90,
                    family="meta",
                    semantic=True,
                )
            )

        elif key in {
            "og:image",
            "twitter:image",
        }:

            result.append(
                Evidence(
                    value=value,
                    role="IMAGE",
                    source="meta",
                    key=key,
                    url=snapshot.final_url,
                    weight=60,
                    family="meta",
                )
            )

        elif key in {
            "og:description",
            "twitter:description",
        }:

            result.append(
                Evidence(
                    value=truncate(
                        value,
                        1000,
                    ),
                    role="DESCRIPTION",
                    source="meta",
                    key=key,
                    url=snapshot.final_url,
                    weight=60,
                    family="meta",
                )
            )

    return result


# ============================================================
# OBJECT EXTRACTION
# ============================================================

def extract_object_ids(
    shape: URLShape,
    evidence: List[Evidence],
) -> Dict[str, str]:

    result = {
        "post_id": shape.post_id,
        "video_id": shape.video_id,
        "reel_id": shape.reel_id,
        "photo_id": shape.photo_id,
        "story_id": shape.story_id,
        "album_id": shape.album_id,
    }

    role_map = {
        "POST_ID": "post_id",
        "VIDEO_ID": "video_id",
        "REEL_ID": "reel_id",
        "PHOTO_ID": "photo_id",
        "STORY_ID": "story_id",
        "ALBUM_ID": "album_id",
    }

    for item in evidence:

        key = role_map.get(
            item.role
        )

        if not key:
            continue

        if result[key]:
            continue

        value = digits(
            item.value
        )

        if value:
            result[key] = value

    return result


# ============================================================
# ENTITY CLASSIFICATION
# ============================================================

def classify_entity(
    shape: URLShape,
    evidence: List[Evidence],
) -> str:

    if shape.kind == "group":
        return "GROUP"

    if shape.kind == "page":
        return "PAGE"

    if shape.kind == "event":
        return "EVENT"

    for item in evidence:

        if item.role == "GROUP_ID":
            return "GROUP"

        if item.role == "PAGE_ID":
            return "PAGE"

        if item.role == "EVENT_ID":
            return "EVENT"

    if shape.kind in {
        "profile",
        "video",
        "reel",
        "post",
        "photo",
        "story",
        "numeric_profile_or_object",
    }:

        return "USER"

    return "UNKNOWN"


# ============================================================
# CONTENT TYPE
# ============================================================

def content_type_from_shape(
    shape: URLShape,
) -> str:

    mapping = {
        "video": "VIDEO",
        "reel": "REEL",
        "post": "POST",
        "photo": "PHOTO",
        "story": "STORY",
    }

    return mapping.get(
        shape.kind,
        "",
    )


# ============================================================
# PROFILE DISCOVERY
# ============================================================

def profile_url_from_candidate(
    url: str,
) -> str:

    url = clean_url(url)

    if not url:
        return ""

    shape = classify_url(
        url
    )

    if shape.kind in {
        "group",
        "page",
        "event",
    }:
        return ""

    return profile_base_from_url(
        url
    )


def discover_profile_urls(
    snapshot: PageSnapshot,
    evidence: List[Evidence],
    expected_username: str = "",
) -> List[str]:

    found = []

    def add(candidate: str):

        candidate = clean_url(
            candidate
        )

        if not candidate:
            return

        parsed = urlparse(
            candidate
        )

        if (
            parsed.netloc.lower()
            not in FACEBOOK_HOSTS
        ):
            return

        profile = (
            profile_url_from_candidate(
                candidate
            )
        )

        if not profile:
            return

        if profile not in found:
            found.append(profile)

    # Canonical
    add(
        snapshot.meta.get(
            "og:url",
            "",
        )
    )

    # JSON-LD / semantic
    for item in evidence:

        if item.role in {
            "PROFILE_URL",
            "CANONICAL",
        }:

            add(
                item.value
            )

    # Links
    for link in snapshot.links:

        try:

            absolute = urljoin(
                snapshot.final_url,
                link,
            )

        except Exception:
            continue

        add(
            absolute
        )

        if len(found) >= MAX_DISCOVERED_URLS:
            break

    # Prefer username matching.
    if expected_username:

        expected = (
            expected_username.lower()
        )

        found.sort(
            key=lambda x: (
                0
                if expected
                in x.lower()
                else 1
            )
        )

    return found[
        :MAX_PROFILE_CHECKS
    ]


# ============================================================
# PROFILE METADATA
# ============================================================

def extract_profile_info(
    snapshot: PageSnapshot,
    evidence: List[Evidence],
    profile_url: str,
) -> ProfileInfo:

    info = ProfileInfo()

    info.profile_url = clean_url(
        profile_url
        or snapshot.final_url
    )

    # --------------------------------------------------------
    # NAME
    # --------------------------------------------------------

    names = [
        x
        for x in evidence
        if x.role == "NAME"
    ]

    names.sort(
        key=lambda x: x.weight,
        reverse=True,
    )

    for item in names:

        candidate = normalize_name(
            item.value
        )

        if candidate:

            info.name = candidate

            break

    # --------------------------------------------------------
    # USERNAME
    # --------------------------------------------------------

    usernames = [
        x
        for x in evidence
        if x.role == "USERNAME"
    ]

    usernames.sort(
        key=lambda x: x.weight,
        reverse=True,
    )

    for item in usernames:

        candidate = normalize_username(
            item.value
        )

        if candidate:

            info.username = candidate

            break

    if not info.username:

        shape = classify_url(
            info.profile_url
        )

        if shape.username:
            info.username = (
                shape.username
            )

    # --------------------------------------------------------
    # AVATAR
    # --------------------------------------------------------

    images = [
        x
        for x in evidence
        if x.role in {
            "AVATAR",
            "IMAGE",
        }
    ]

    images.sort(
        key=lambda x: x.weight,
        reverse=True,
    )

    for item in images:

        if item.value.startswith(
            "http"
        ):

            info.avatar_url = (
                item.value
            )

            break

    # --------------------------------------------------------
    # BIO
    # --------------------------------------------------------

    bios = [
        x
        for x in evidence
        if x.role in {
            "BIO",
            "DESCRIPTION",
        }
    ]

    bios.sort(
        key=lambda x: x.weight,
        reverse=True,
    )

    for item in bios:

        value = truncate(
            item.value,
            700,
        )

        if value:

            info.bio = value

            break

    # --------------------------------------------------------
    # UID
    # --------------------------------------------------------

    candidates = [
        x
        for x in evidence
        if x.role == "USER_CANDIDATE"
    ]

    scored = score_uid_candidates(
        candidates
    )

    if scored:

        info.uid = scored[0][0]

    info.evidence = evidence

    info.entity_type = "USER"

    return info


# ============================================================
# UID CANDIDATE SCORING
# ============================================================

def score_uid_candidates(
    evidence: List[Evidence],
) -> List[
    Tuple[
        str,
        float,
        List[Evidence],
    ]
]:

    grouped: Dict[
        str,
        List[Evidence],
    ] = {}

    object_values = {
        digits(x.value)
        for x in evidence
        if x.role
        in {
            "OBJECT_CANDIDATE",
            "POST_ID",
            "VIDEO_ID",
            "REEL_ID",
            "PHOTO_ID",
            "STORY_ID",
            "ALBUM_ID",
            "GROUP_ID",
            "PAGE_ID",
            "EVENT_ID",
        }
    }

    for item in evidence:

        value = digits(
            item.value
        )

        if not value:
            continue

        if value in object_values:
            continue

        grouped.setdefault(
            value,
            [],
        ).append(item)

    results = []

    for value, items in grouped.items():

        score = 0.0

        families: Set[str] = set()

        semantic_count = 0

        strong_fields: Set[str] = set()

        # ----------------------------------------------------
        # Highest-quality evidence
        # ----------------------------------------------------

        strongest = sorted(
            items,
            key=lambda x: x.weight,
            reverse=True,
        )

        if strongest:

            score += min(
                strongest[0].weight,
                130,
            ) * 0.42

        for item in items:

            family = (
                item.family
                or item.source
            )

            families.add(
                family
            )

            if item.semantic:
                semantic_count += 1

            key = normalize_key(
                item.key
            )

            if key in {
                "user_id",
                "userid",
                "profile_id",
                "profileid",
                "owner_id",
                "ownerid",
                "publisher_id",
                "publisherid",
                "author_id",
                "authorid",
                "creator_id",
                "creatorid",
                "from_id",
                "fromid",
                "actor_id",
                "actorid",
            }:

                strong_fields.add(
                    key
                )

        # ----------------------------------------------------
        # Independent evidence families
        # ----------------------------------------------------

        if len(families) >= 2:
            score += 22

        if len(families) >= 3:
            score += 12

        if len(families) >= 4:
            score += 8

        # ----------------------------------------------------
        # Semantic evidence
        # ----------------------------------------------------

        if semantic_count >= 1:
            score += 12

        if semantic_count >= 2:
            score += 8

        # ----------------------------------------------------
        # Strong field diversity
        # ----------------------------------------------------

        if strong_fields:
            score += 15

        if len(strong_fields) >= 2:
            score += 10

        # ----------------------------------------------------
        # Cap
        # ----------------------------------------------------

        score = min(
            score,
            100,
        )

        results.append(
            (
                value,
                score,
                items,
            )
        )

    results.sort(
        key=lambda x: x[1],
        reverse=True,
    )

    return results


# ============================================================
# PROFILE VERIFICATION
# ============================================================

def verify_profile_identity(
    expected_uid: str,
    profile: ProfileInfo,
    profile_evidence: List[Evidence],
    expected_username: str = "",
) -> Tuple[
    bool,
    List[str],
]:

    signals = []

    if not expected_uid:
        return False, signals

    candidates = [
        x
        for x in profile_evidence
        if x.role == "USER_CANDIDATE"
        and digits(x.value)
        == expected_uid
    ]

    families = {
        x.family or x.source
        for x in candidates
    }

    # --------------------------------------------------------
    # Explicit profile match
    # --------------------------------------------------------

    if candidates:

        if len(families) >= 2:

            signals.append(
                "profile → UID khớp nhiều nguồn"
            )

            return True, signals

        if any(
            x.semantic
            for x in candidates
        ):

            signals.append(
                "profile → UID khớp"
            )

            return True, signals

    # --------------------------------------------------------
    # Username correlation
    # --------------------------------------------------------

    if (
        expected_username
        and profile.username
        and expected_username.lower()
        == profile.username.lower()
        and candidates
    ):

        signals.append(
            "username → profile → UID khớp"
        )

        return True, signals

    return False, signals


# ============================================================
# CONFLICT DETECTOR
# ============================================================

def detect_uid_conflicts(
    candidates: List[
        Tuple[
            str,
            float,
            List[Evidence],
        ]
    ],
) -> List[str]:

    strong = []

    for uid, score, evidence in candidates:

        if score >= 65:

            strong.append(uid)

    unique = list(
        dict.fromkeys(
            strong
        )
    )

    if len(unique) <= 1:
        return []

    return unique


# ============================================================
# CANONICAL URL
# ============================================================

def canonical_url(
    snapshot: PageSnapshot,
    fallback: str,
) -> str:

    candidate = (
        snapshot.meta.get(
            "og:url",
            "",
        )
        or snapshot.meta.get(
            "twitter:url",
            "",
        )
        or snapshot.final_url
        or fallback
    )

    return clean_url(
        candidate
    )


# ============================================================
# CONTENT TITLE
# ============================================================

def content_title(
    snapshot: PageSnapshot,
) -> str:

    candidates = [
        snapshot.meta.get(
            "og:title",
            "",
        ),
        snapshot.meta.get(
            "twitter:title",
            "",
        ),
        snapshot.title,
    ]

    for value in candidates:

        value = normalize_name(
            value
        )

        if value:

            return truncate(
                value,
                500,
            )

    return ""


# ============================================================
# FACEBOOK RESOLVER
# ============================================================

class FacebookResolver:

    def __init__(self):

        self.fetcher = (
            FetchEngine()
        )

        self.semantic = (
            SemanticExtractor()
        )

    async def resolve(
        self,
        url: str,
    ) -> ResolveResult:

        started = time.perf_counter()

        cleaned = clean_url(
            url
        )

        result = ResolveResult(
            input_url=cleaned
        )

        try:

            # =================================================
            # CACHE
            # =================================================

            cache_key = (
                cleaned.lower()
            )

            cached = _CACHE.get(
                cache_key
            )

            if cached:

                timestamp, cached_result = (
                    cached
                )

                if (
                    time.time()
                    - timestamp
                    < CACHE_TTL
                ):

                    cloned = copy.deepcopy(
                        cached_result
                    )

                    cloned.input_url = (
                        cleaned
                    )

                    return cloned

            # =================================================
            # INPUT SHAPE
            # =================================================

            input_shape = classify_url(
                cleaned
            )

            result.content_type = (
                content_type_from_shape(
                    input_shape
                )
            )

            result.profile_url = (
                profile_base_from_url(
                    cleaned
                )
            )

            # =================================================
            # FETCH ORIGINAL
            # =================================================

            snapshot = await self.fetcher.fetch(
                cleaned
            )

            # =================================================
            # CANONICAL
            # =================================================

            result.canonical_url = (
                canonical_url(
                    snapshot,
                    cleaned,
                )
            )

            result.content_url = (
                result.canonical_url
                or cleaned
            )

            # =================================================
            # EVIDENCE
            # =================================================

            evidence: List[
                Evidence
            ] = []

            evidence.extend(
                url_evidence(
                    snapshot
                )
            )

            evidence.extend(
                meta_evidence(
                    snapshot
                )
            )

            evidence.extend(
                self.semantic.extract(
                    snapshot
                )
            )

            # =================================================
            # REDIRECT EVIDENCE
            # =================================================

            for redirect_url in (
                snapshot.redirect_chain
            ):

                redirect_url = clean_url(
                    redirect_url
                )

                if not redirect_url:
                    continue

                redirect_shape = (
                    classify_url(
                        redirect_url
                    )
                )

                if redirect_shape.username:

                    evidence.append(
                        Evidence(
                            value=(
                                redirect_shape.username
                            ),
                            role="USERNAME",
                            source="redirect",
                            key="path",
                            url=redirect_url,
                            weight=82,
                            family="redirect",
                            semantic=True,
                        )
                    )

                for role, value in [
                    (
                        "POST_ID",
                        redirect_shape.post_id,
                    ),
                    (
                        "VIDEO_ID",
                        redirect_shape.video_id,
                    ),
                    (
                        "REEL_ID",
                        redirect_shape.reel_id,
                    ),
                    (
                        "PHOTO_ID",
                        redirect_shape.photo_id,
                    ),
                    (
                        "STORY_ID",
                        redirect_shape.story_id,
                    ),
                ]:

                    if value:

                        evidence.append(
                            Evidence(
                                value=value,
                                role=role,
                                source="redirect",
                                key=role.lower(),
                                url=redirect_url,
                                weight=110,
                                family="redirect",
                                semantic=True,
                            )
                        )

            # =================================================
            # DISCOVER PROFILE
            # =================================================

            if not result.profile_url:

                discovered = (
                    discover_profile_urls(
                        snapshot,
                        evidence,
                        input_shape.username,
                    )
                )

                if discovered:

                    result.profile_url = (
                        discovered[0]
                    )

            else:

                # Ensure canonical matching profile
                discovered = (
                    discover_profile_urls(
                        snapshot,
                        evidence,
                        input_shape.username,
                    )
                )

                if (
                    discovered
                    and input_shape.username
                ):

                    for candidate in discovered:

                        if (
                            input_shape.username.lower()
                            in candidate.lower()
                        ):

                            result.profile_url = (
                                candidate
                            )

                            break

            # =================================================
            # METADATA
            # =================================================

            self._merge_metadata(
                result,
                input_shape,
                snapshot,
                evidence,
            )

            # =================================================
            # OBJECT IDS
            # =================================================

            objects = extract_object_ids(
                input_shape,
                evidence,
            )

            result.post_id = (
                objects["post_id"]
            )

            result.video_id = (
                objects["video_id"]
            )

            result.reel_id = (
                objects["reel_id"]
            )

            result.photo_id = (
                objects["photo_id"]
            )

            result.story_id = (
                objects["story_id"]
            )

            result.album_id = (
                objects["album_id"]
            )

            # =================================================
            # ENTITY
            # =================================================

            result.entity_type = (
                classify_entity(
                    input_shape,
                    evidence,
                )
            )

            result.publisher_type = (
                result.entity_type
            )

            # =================================================
            # UID CANDIDATES
            # =================================================

            uid_evidence = [
                x
                for x in evidence
                if x.role
                == "USER_CANDIDATE"
            ]

            candidates = (
                score_uid_candidates(
                    uid_evidence
                )
            )

            conflicts = (
                detect_uid_conflicts(
                    candidates
                )
            )

            # =================================================
            # PROFILE VERIFICATION
            # =================================================

            if (
                result.profile_url
                and result.entity_type
                in {
                    "USER",
                    "UNKNOWN",
                }
            ):

                profile_snapshot = (
                    await self.fetcher.fetch(
                        result.profile_url
                    )
                )

                if profile_snapshot.html_text:

                    profile_evidence = []

                    profile_evidence.extend(
                        url_evidence(
                            profile_snapshot
                        )
                    )

                    profile_evidence.extend(
                        meta_evidence(
                            profile_snapshot
                        )
                    )

                    profile_evidence.extend(
                        self.semantic.extract(
                            profile_snapshot
                        )
                    )

                    profile_info = (
                        extract_profile_info(
                            profile_snapshot,
                            profile_evidence,
                            result.profile_url,
                        )
                    )

                    self._merge_profile(
                        result,
                        profile_info,
                    )

                    # -----------------------------------------
                    # TOP UID ↔ PROFILE
                    # -----------------------------------------

                    if candidates:

                        top_uid = (
                            candidates[0][0]
                        )

                        verified, signals = (
                            verify_profile_identity(
                                top_uid,
                                profile_info,
                                profile_evidence,
                                result.username,
                            )
                        )

                        if verified:

                            result.uid = (
                                top_uid
                            )

                            result.verified = True

                            result.confidence = (
                                self.calculate_confidence(
                                    candidates,
                                    profile_verified=True,
                                    conflicts=conflicts,
                                    username_match=self.username_match(
                                        result,
                                        profile_info,
                                    ),
                                )
                            )

                            result.signals.extend(
                                signals
                            )

                    # -----------------------------------------
                    # PROFILE DIRECT UID
                    # -----------------------------------------

                    if not result.uid:

                        profile_candidates = (
                            score_uid_candidates(
                                [
                                    x
                                    for x
                                    in profile_evidence
                                    if x.role
                                    == "USER_CANDIDATE"
                                ]
                            )
                        )

                        if profile_candidates:

                            profile_uid = (
                                profile_candidates[0][0]
                            )

                            profile_conflicts = (
                                detect_uid_conflicts(
                                    profile_candidates
                                )
                            )

                            if (
                                not profile_conflicts
                                and profile_uid
                            ):

                                # A UID directly exposed
                                # on a public profile page
                                # is acceptable.
                                result.uid = (
                                    profile_uid
                                )

                                result.verified = (
                                    True
                                )

                                result.confidence = (
                                    self.calculate_confidence(
                                        profile_candidates,
                                        profile_verified=True,
                                        conflicts=[],
                                        username_match=True,
                                    )
                                )

                                result.signals.append(
                                    "profile → UID công khai"
                                )

            # =================================================
            # MULTI-SOURCE FALLBACK
            # =================================================

            if (
                not result.uid
                and candidates
                and not conflicts
            ):

                top_uid, score, items = (
                    candidates[0]
                )

                families = {
                    x.family
                    or x.source
                    for x in items
                }

                semantic_items = [
                    x
                    for x in items
                    if x.semantic
                ]

                # Strict fallback.
                if (
                    score >= 80
                    and len(families) >= 2
                    and len(semantic_items) >= 1
                ):

                    result.uid = (
                        top_uid
                    )

                    result.verified = True

                    result.confidence = (
                        self.calculate_confidence(
                            candidates,
                            profile_verified=False,
                            conflicts=[],
                            username_match=bool(
                                result.username
                            ),
                        )
                    )

                    result.signals.append(
                        "UID khớp nhiều nguồn public"
                    )

            # =================================================
            # USERNAME SIGNAL
            # =================================================

            if (
                result.username
                and result.profile_url
            ):

                result.signals.append(
                    "canonical → username khớp"
                )

            # =================================================
            # CONFLICT LOCK
            # =================================================

            if conflicts:

                result.uid = ""

                result.verified = False

                result.confidence = 0

                result.notes.append(
                    "Phát hiện nhiều UID cạnh tranh; UID bị khóa để tránh nhận sai."
                )

            # =================================================
            # NO UID
            # =================================================

            if not result.uid:

                result.verified = False

                result.confidence = 0

                if snapshot.error:

                    result.notes.append(
                        "Facebook giới hạn hoặc không trả đầy đủ trang public."
                    )

                elif result.entity_type == "USER":

                    result.notes.append(
                        "Không tìm thấy bằng chứng public đủ mạnh để liên kết profile/content với UID."
                    )

                else:

                    result.notes.append(
                        "Có dữ liệu public nhưng chưa đủ bằng chứng xác định UID."
                    )

            # =================================================
            # EVIDENCE COUNT
            # =================================================

            relevant = [
                x
                for x in evidence
                if x.role
                in {
                    "USER_CANDIDATE",
                    "USERNAME",
                    "CANONICAL",
                    "NAME",
                }
            ]

            result.evidence_sources = len(
                {
                    x.family
                    or x.source
                    for x in relevant
                }
            )

            # =================================================
            # ERROR
            # =================================================

            if snapshot.error:

                result.error = (
                    snapshot.error
                )

            # =================================================
            # ELAPSED
            # =================================================

            result.elapsed = (
                time.perf_counter()
                - started
            )

            # =================================================
            # CACHE
            # =================================================

            _CACHE[cache_key] = (
                time.time(),
                copy.deepcopy(
                    result
                ),
            )

            self.cleanup_cache()

            return result

        except Exception as exc:

            logger.exception(
                "Facebook resolver error"
            )

            result.error = (
                f"{type(exc).__name__}: {exc}"
            )

            result.elapsed = (
                time.perf_counter()
                - started
            )

            result.notes.append(
                "Resolver gặp lỗi nội bộ nhưng vẫn trả dữ liệu public đã thu được."
            )

            return result

    # ========================================================
    # METADATA MERGE
    # ========================================================

    def _merge_metadata(
        self,
        result: ResolveResult,
        shape: URLShape,
        snapshot: PageSnapshot,
        evidence: List[Evidence],
    ):

        # NAME
        names = [
            x.value
            for x in evidence
            if x.role == "NAME"
        ]

        for name in names:

            name = normalize_name(
                name
            )

            if name:

                if not result.name:

                    result.name = name

                break

        # USERNAME
        if not result.username:

            usernames = [
                x.value
                for x in evidence
                if x.role == "USERNAME"
            ]

            for username in usernames:

                username = (
                    normalize_username(
                        username
                    )
                )

                if username:

                    result.username = (
                        username
                    )

                    break

        if (
            not result.username
            and shape.username
        ):

            result.username = (
                shape.username
            )

        # TITLE
        if not result.title:

            # Do not call profile title content.
            if shape.kind not in {
                "profile",
                "numeric_profile_or_object",
            }:

                result.title = (
                    content_title(
                        snapshot
                    )
                )

        # IMAGE
        if not result.avatar_url:

            for item in evidence:

                if item.role not in {
                    "AVATAR",
                    "IMAGE",
                }:
                    continue

                if item.value.startswith(
                    "http"
                ):

                    result.avatar_url = (
                        item.value
                    )

                    break

        # BIO
        if not result.bio:

            descriptions = [
                x.value
                for x in evidence
                if x.role
                in {
                    "BIO",
                    "DESCRIPTION",
                }
            ]

            if descriptions:

                result.bio = truncate(
                    descriptions[0],
                    700,
                )

    # ========================================================
    # PROFILE MERGE
    # ========================================================

    def _merge_profile(
        self,
        result: ResolveResult,
        profile: ProfileInfo,
    ):

        if profile.name:
            result.name = (
                profile.name
            )

        if profile.username:
            result.username = (
                profile.username
            )

        if profile.profile_url:
            result.profile_url = (
                profile.profile_url
            )

        if profile.avatar_url:
            result.avatar_url = (
                profile.avatar_url
            )

        if profile.bio:
            result.bio = (
                profile.bio
            )

        if (
            result.entity_type
            == "UNKNOWN"
            and profile.entity_type
        ):

            result.entity_type = (
                profile.entity_type
            )

    # ========================================================
    # USERNAME MATCH
    # ========================================================

    @staticmethod
    def username_match(
        result: ResolveResult,
        profile: ProfileInfo,
    ) -> bool:

        if not result.username:
            return False

        if not profile.username:
            return False

        return (
            result.username.lower()
            == profile.username.lower()
        )

    # ========================================================
    # CONFIDENCE
    # ========================================================

    @staticmethod
    def calculate_confidence(
        candidates,
        profile_verified: bool,
        conflicts: List[str],
        username_match: bool,
    ) -> float:

        if not candidates:
            return 0.0

        uid, score, items = (
            candidates[0]
        )

        families = {
            x.family
            or x.source
            for x in items
        }

        strong_fields = {
            normalize_key(x.key)
            for x in items
            if x.key
        }

        semantic = sum(
            1
            for x in items
            if x.semantic
        )

        value = 45.0

        # Strong raw candidate quality.
        value += min(
            score * 0.20,
            20,
        )

        # Independent families.
        if len(families) >= 2:
            value += 12

        if len(families) >= 3:
            value += 7

        # Profile verification.
        if profile_verified:
            value += 17

        # Username match.
        if username_match:
            value += 5

        # Semantic diversity.
        if semantic >= 2:
            value += 5

        # Strong field diversity.
        if len(strong_fields) >= 2:
            value += 4

        # Conflict.
        if conflicts:
            value -= 60

        return round(
            max(
                0,
                min(
                    value,
                    99.9,
                ),
            ),
            1,
        )

    # ========================================================
    # CACHE
    # ========================================================

    @staticmethod
    def cleanup_cache():

        now = time.time()

        expired = [
            key
            for key, (
                timestamp,
                _,
            ) in _CACHE.items()
            if (
                now - timestamp
                > CACHE_TTL
            )
        ]

        for key in expired:

            _CACHE.pop(
                key,
                None,
            )


# ============================================================
# TELEGRAM LINK
# ============================================================

def html_link(
    label: str,
    url: str,
) -> str:

    if not url:
        return ""

    return (
        '<a href="'
        + html.escape(
            url,
            quote=True,
        )
        + '">'
        + html.escape(
            label,
            quote=False,
        )
        + "</a>"
    )


# ============================================================
# RESULT FORMAT
# ============================================================

def format_result(
    result: ResolveResult,
    index: int,
) -> str:

    lines = []

    lines.append(
        "╭──────────────────────────"
    )

    lines.append(
        f"│ 🔎 <b>FACEBOOK RESOLVER #{index}</b>"
    )

    lines.append(
        "├──────────────────────────"
    )

    type_map = {
        "USER": "👤 Cá nhân",
        "PAGE": "📄 Trang",
        "GROUP": "👥 Nhóm",
        "EVENT": "📅 Sự kiện",
        "UNKNOWN": "❔ Chưa xác định",
    }

    # ========================================================
    # PROFILE / ENTITY
    # ========================================================

    has_identity = any(
        [
            result.name,
            result.username,
            result.uid,
            result.profile_url,
        ]
    )

    if has_identity:

        if result.entity_type == "GROUP":

            lines.append(
                "│ 👥 <b>NHÓM</b>"
            )

        elif result.entity_type == "PAGE":

            lines.append(
                "│ 📄 <b>TRANG</b>"
            )

        elif result.entity_type == "EVENT":

            lines.append(
                "│ 📅 <b>SỰ KIỆN</b>"
            )

        else:

            lines.append(
                "│ 👤 <b>NGƯỜI DÙNG</b>"
            )

        if result.name:

            lines.append(
                "│ Tên      : "
                + tg_escape(
                    result.name
                )
            )

        if result.username:

            lines.append(
                "│ Username : @"
                + tg_escape(
                    result.username
                )
            )

        if result.uid:

            lines.append(
                "│ UID      : <code>"
                + tg_escape(
                    result.uid
                )
                + "</code>"
            )

        else:

            lines.append(
                "│ UID      : ⚠️ <i>Chưa xác minh công khai</i>"
            )

        lines.append(
            "│ Loại     : "
            + type_map.get(
                result.entity_type,
                "❔ Chưa xác định",
            )
        )

        if result.bio:

            lines.append(
                "│ Bio      : "
                + tg_escape(
                    truncate(
                        result.bio,
                        260,
                    )
                )
            )

        if result.avatar_url:

            lines.append(
                "│ Avatar   : "
                + html_link(
                    "🖼 Mở ảnh",
                    result.avatar_url,
                )
            )

    # ========================================================
    # CONTENT
    # ========================================================

    has_content = any(
        [
            result.content_type,
            result.post_id,
            result.video_id,
            result.reel_id,
            result.photo_id,
            result.story_id,
        ]
    )

    # A profile URL must NEVER become "CONTENT".
    if has_content:

        lines.append("│")

        lines.append(
            "│ 🎬 <b>NỘI DUNG</b>"
        )

        if result.content_type:

            lines.append(
                "│ Loại     : "
                + tg_escape(
                    result.content_type
                )
            )

        object_rows = [
            (
                "Post ID",
                result.post_id,
            ),
            (
                "Video ID",
                result.video_id,
            ),
            (
                "Reel ID",
                result.reel_id,
            ),
            (
                "Photo ID",
                result.photo_id,
            ),
            (
                "Story ID",
                result.story_id,
            ),
            (
                "Album ID",
                result.album_id,
            ),
        ]

        for label, value in object_rows:

            if not value:
                continue

            lines.append(
                f"│ {label:<9}: "
                "<code>"
                + tg_escape(value)
                + "</code>"
            )

        if result.title:

            lines.append(
                "│ Tiêu đề  : "
                + tg_escape(
                    truncate(
                        result.title,
                        280,
                    )
                )
            )

    # ========================================================
    # LINKS
    # ========================================================

    lines.append("│")

    lines.append(
        "│ 🔗 <b>LIÊN KẾT</b>"
    )

    if result.profile_url:

        lines.append(
            "│ Profile  : "
            + html_link(
                "Mở trang cá nhân",
                result.profile_url,
            )
        )

    if result.content_url:

        label = (
            "Mở nội dung"
            if has_content
            else "Mở trang"
        )

        lines.append(
            "│ Content  : "
            + html_link(
                label,
                result.content_url,
            )
        )

    # ========================================================
    # FORENSIC
    # ========================================================

    if result.signals:

        lines.append("│")

        lines.append(
            "│ 🔬 <b>DẤU HIỆU</b>"
        )

        # Remove duplicate signals.
        seen = set()

        count = 0

        for signal in result.signals:

            signal = clean_text(
                signal
            )

            if not signal:
                continue

            if signal in seen:
                continue

            seen.add(signal)

            lines.append(
                "│ • "
                + tg_escape(
                    signal
                )
            )

            count += 1

            if count >= 4:
                break

    # ========================================================
    # VERIFICATION
    # ========================================================

    lines.append("│")

    lines.append(
        "│ 🛡 <b>XÁC MINH</b>"
    )

    if (
        result.verified
        and result.uid
    ):

        lines.append(
            "│ Trạng thái : ✅ <b>VERIFIED</b>"
        )

        lines.append(
            "│ Độ tin cậy : <b>"
            + f"{result.confidence:.1f}%"
            + "</b>"
        )

        if result.evidence_sources:

            lines.append(
                "│ Nguồn      : "
                + str(
                    result.evidence_sources
                )
                + " nguồn"
            )

    else:

        lines.append(
            "│ Trạng thái : ⚠️ <b>NOT VERIFIED</b>"
        )

        lines.append(
            "│ UID bị ẩn để tránh nhận sai."
        )

        for note in result.notes[:2]:

            lines.append(
                "│ • "
                + tg_escape(
                    truncate(
                        note,
                        250,
                    )
                )
            )

    # ========================================================
    # ERROR / FETCH
    # ========================================================

    if result.error:

        lines.append(
            "│ ⚠️ <i>Public fetch bị giới hạn.</i>"
        )

    # ========================================================
    # TIME
    # ========================================================

    lines.append("│")

    lines.append(
        "╰──────────────────────────"
    )

    lines.append(
        "⏱ "
        + f"{result.elapsed:.2f}s"
    )

    text = "\n".join(
        lines
    )

    if len(text) > MAX_OUTPUT_CHARS:

        text = (
            text[
                :MAX_OUTPUT_CHARS - 20
            ]
            + "\n…"
        )

    return text


# ============================================================
# RESULTS FORMAT
# ============================================================

def format_results(
    results: List[ResolveResult],
) -> str:

    blocks = []

    for index, result in enumerate(
        results,
        1,
    ):

        blocks.append(
            format_result(
                result,
                index,
            )
        )

    return "\n\n".join(
        blocks
    )


# ============================================================
# PROCESS URLS
# ============================================================

async def process_urls(
    event,
    urls: List[str],
):

    # ========================================================
    # DEDUPE
    # ========================================================

    cleaned_urls = []

    seen = set()

    for url in urls:

        url = clean_url(
            url
        )

        if not url:
            continue

        key = url.lower().rstrip(
            "/"
        )

        if key in seen:
            continue

        seen.add(key)

        cleaned_urls.append(
            url
        )

        if (
            len(cleaned_urls)
            >= MAX_INPUT_URLS
        ):
            break

    urls = cleaned_urls

    if not urls:

        await event.reply(
            "❌ Không tìm thấy URL Facebook hợp lệ."
        )

        return

    # ========================================================
    # RESOLVER
    # ========================================================

    resolver = (
        FacebookResolver()
    )

    async def one(url):

        try:

            return await resolver.resolve(
                url
            )

        except Exception as exc:

            logger.exception(
                "URL resolve failed"
            )

            return ResolveResult(
                input_url=url,
                error=str(exc),
                notes=[
                    "Không thể hoàn tất phân tích URL."
                ],
            )

    # ========================================================
    # PARALLEL
    # ========================================================

    results = await asyncio.gather(
        *[
            one(url)
            for url in urls
        ],
        return_exceptions=False,
    )

    if not results:

        await event.reply(
            "⚠️ Resolver không thu được kết quả."
        )

        return

    text = format_results(
        results
    )

    if not text.strip():

        text = (
            "⚠️ Resolver không thu được "
            "dữ liệu public từ Facebook."
        )

    # ========================================================
    # TELEGRAM
    # ========================================================

    try:

        await event.reply(
            text,
            parse_mode="html",
            link_preview=False,
        )

    except Exception:

        logger.exception(
            "HTML output failed"
        )

        # Strip HTML.
        plain = re.sub(
            r"<[^>]+>",
            "",
            text,
        )

        try:

            await event.reply(
                plain,
                link_preview=False,
            )

        except Exception:

            logger.exception(
                "Plain output failed"
            )


# ============================================================
# WAIT NEXT MESSAGE
# ============================================================

async def wait_for_next_message(
    event,
    timeout: int = SESSION_TIMEOUT,
):

    client = event.client

    sender = await event.get_sender()

    sender_id = (
        getattr(
            sender,
            "id",
            None,
        )
        if sender
        else None
    )

    chat_id = event.chat_id

    if sender_id is None:
        return None

    future = (
        asyncio.get_running_loop()
        .create_future()
    )

    async def watcher(
        new_event,
    ):

        try:

            if (
                new_event.chat_id
                != chat_id
            ):
                return

            new_sender = (
                await new_event.get_sender()
            )

            new_sender_id = (
                getattr(
                    new_sender,
                    "id",
                    None,
                )
                if new_sender
                else None
            )

            if (
                new_sender_id
                != sender_id
            ):
                return

            if not future.done():

                future.set_result(
                    new_event
                )

        except Exception:
            pass

    client.add_event_handler(
        watcher,
        events.NewMessage,
    )

    try:

        return await asyncio.wait_for(
            future,
            timeout=timeout,
        )

    except asyncio.TimeoutError:

        return None

    finally:

        try:

            client.remove_event_handler(
                watcher,
                events.NewMessage,
            )

        except Exception:
            pass


# ============================================================
# COMMAND
# ============================================================

async def _handle_getuidfb(
    event,
    args: str,
):

    args = clean_text(
        args
    )

    urls = extract_facebook_urls(
        args
    )

    if not urls:

        prompt = await event.reply(
            "🔎 <b>FACEBOOK FORENSIC RESOLVER V50</b>\n\n"
            "Gửi link Facebook cần kiểm tra.\n\n"
            "🛡 HTTP public-only\n"
            "🔬 Quét URL + redirect + HTML + JSON + JSON-LD + semantic data\n"
            "🎯 UID chỉ hiển thị khi đủ bằng chứng.",
            parse_mode="html",
            link_preview=False,
        )

        next_event = (
            await wait_for_next_message(
                event
            )
        )

        if next_event is None:

            try:

                await prompt.edit(
                    "⌛ Hết thời gian chờ URL Facebook."
                )

            except Exception:
                pass

            return

        urls = extract_facebook_urls(
            next_event.raw_text
            or ""
        )

        if not urls:

            await next_event.reply(
                "❌ Không tìm thấy URL Facebook hợp lệ."
            )

            return

        await process_urls(
            next_event,
            urls,
        )

        return

    await process_urls(
        event,
        urls,
    )


# ============================================================
# REGISTER
# ============================================================

def register(
    bot,
    notify_bot=None,
):
    """
    Required by:

        commands/__init__.py

    Example:

        module.register(
            bot,
            notify_bot,
        )
    """

    # ========================================================
    # /getuidfb
    # ========================================================

    @bot.on(
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?(?:\s+.*)?$"
        )
    )
    async def getuidfb_command(
        event,
    ):

        try:

            text = (
                event.raw_text
                or ""
            )

            parts = text.split(
                maxsplit=1
            )

            args = (
                parts[1]
                if len(parts) > 1
                else ""
            )

            await _handle_getuidfb(
                event,
                args,
            )

        except Exception:

            logger.exception(
                "getuidfb command crashed"
            )

            try:

                await event.reply(
                    "❌ Resolver gặp lỗi nhưng handler vẫn hoạt động."
                )

            except Exception:
                pass

    # ========================================================
    # getuidfb URL
    # ========================================================

    @bot.on(
        events.NewMessage(
            pattern=r"^getuidfb(?:\s+.+)?$",
            func=lambda e: not (
                e.raw_text or ""
            ).startswith("/"),
        )
    )
    async def getuidfb_text_command(
        event,
    ):

        try:

            text = (
                event.raw_text
                or ""
            )

            parts = text.split(
                maxsplit=1
            )

            args = (
                parts[1]
                if len(parts) > 1
                else ""
            )

            await _handle_getuidfb(
                event,
                args,
            )

        except Exception:

            logger.exception(
                "getuidfb text command crashed"
            )

            try:

                await event.reply(
                    "❌ Resolver gặp lỗi nhưng handler vẫn hoạt động."
                )

            except Exception:
                pass

    logger.info(
        "Facebook Forensic Resolver V50 registered"
    )


# ============================================================
# SELF TEST
# ============================================================

def self_test():

    tests = [

        # profile
        (
            "https://www.facebook.com/nvlnopro/"
        ),

        # username + video
        (
            "https://www.facebook.com/"
            "kim.chi.125900/"
            "videos/4697823150500072/"
        ),

        # username + slug + video
        (
            "https://www.facebook.com/"
            "kim.chi.125900/"
            "videos/"
            "lam-gi-kho-coi-vay/"
            "4697823150500072/"
        ),

        # numeric route
        (
            "https://www.facebook.com/"
            "61592487939720/"
            "videos/"
            "1671583863938536/"
        ),

        # reel
        (
            "https://www.facebook.com/"
            "reel/"
            "4697823150500072/"
        ),

        # reel + slug
        (
            "https://www.facebook.com/"
            "reel/"
            "lam-gi-kho-coi-vay/"
            "4697823150500072/"
        ),

        # post
        (
            "https://www.facebook.com/"
            "username/"
            "posts/"
            "123456789012345/"
        ),

        # photo
        (
            "https://www.facebook.com/"
            "username/"
            "photos/"
            "abc/"
            "123456789012345/"
        ),

        # story
        (
            "https://www.facebook.com/"
            "story.php?"
            "story_fbid=123456789012345"
            "&id=61500000000000"
        ),

        # video.php
        (
            "https://www.facebook.com/"
            "video.php?v=123456789012345"
        ),

        # photo.php
        (
            "https://www.facebook.com/"
            "photo.php?fbid=123456789012345"
        ),

        # profile.php
        (
            "https://www.facebook.com/"
            "profile.php?id=61500000000000"
        ),

        # group
        (
            "https://www.facebook.com/"
            "groups/123456789/"
        ),

        # group post
        (
            "https://www.facebook.com/"
            "groups/123456789/"
            "posts/987654321012345/"
        ),

        # watch
        (
            "https://www.facebook.com/"
            "watch/?v=123456789012345"
        ),
    ]

    for url in tests:

        shape = classify_url(
            url
        )

        print()
        print("=" * 70)
        print(
            "URL:",
            url,
        )
        print(
            "kind:",
            shape.kind,
        )
        print(
            "username:",
            shape.username,
        )
        print(
            "numeric_path_id:",
            shape.numeric_path_id,
        )
        print(
            "post_id:",
            shape.post_id,
        )
        print(
            "video_id:",
            shape.video_id,
        )
        print(
            "reel_id:",
            shape.reel_id,
        )
        print(
            "photo_id:",
            shape.photo_id,
        )
        print(
            "story_id:",
            shape.story_id,
        )
        print(
            "group_id:",
            shape.group_id,
        )
        print(
            "page_id:",
            shape.page_id,
        )
        print(
            "event_id:",
            shape.event_id,
        )
        print(
            "query_uid:",
            shape.query_uid,
        )
        print(
            "profile:",
            profile_base_from_url(
                url
            ),
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "%(levelname)s "
            "%(name)s: "
            "%(message)s"
        ),
    )

    self_test()