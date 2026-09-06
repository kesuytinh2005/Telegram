#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
============================================================
 FACEBOOK PUBLIC FORENSIC RESOLVER V40
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
    Maximum public-data correlation.

PRINCIPLE:
    NEVER GUESS UID.

PIPELINE:

    INPUT
      ↓
    URL SCANNER
      ↓
    NORMALIZER
      ↓
    URL CLASSIFIER
      ↓
    HTTP FETCH
      ↓
    REDIRECT ANALYSIS
      ↓
    HTML
      ↓
    META
      ↓
    JSON-LD
      ↓
    EMBEDDED JSON
      ↓
    SEMANTIC JSON
      ↓
    URL EVIDENCE
      ↓
    OBJECT ID EXTRACTION
      ↓
    USER ID EXTRACTION
      ↓
    ENTITY CLASSIFICATION
      ↓
    PROFILE DISCOVERY
      ↓
    PROFILE VERIFICATION
      ↓
    CONFLICT DETECTION
      ↓
    IDENTITY CORRELATION
      ↓
    CONFIDENCE
      ↓
    RESULT

IMPORTANT:
    No public UID evidence = UID WITHHELD.
"""

import asyncio
import html
import json
import logging
import re
import time

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
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

REQUEST_TIMEOUT_CONNECT = 5
REQUEST_TIMEOUT_READ = 10

MAX_HTML_BYTES = 12 * 1024 * 1024

MAX_INPUT_URLS = 8
MAX_DISCOVERED_URLS = 60
MAX_PROFILE_CHECKS = 3

MAX_JSON_DEPTH = 18
MAX_JSON_NODES = 30000

MAX_EVIDENCE_PER_SOURCE = 8

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
# TRACKING PARAMETERS
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
}


# ============================================================
# OBJECT ROUTES
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
    "watch",
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
}


# ============================================================
# ID FIELD WEIGHTS
# ============================================================

USER_ID_FIELDS = {
    "user_id": 120,
    "userid": 120,
    "userID": 120,
    "profile_id": 125,
    "profileid": 125,
    "profileID": 125,
    "owner_id": 115,
    "ownerid": 115,
    "ownerID": 115,
    "publisher_id": 112,
    "publisherid": 112,
    "author_id": 110,
    "authorid": 110,
    "creator_id": 110,
    "creatorid": 110,
    "from_id": 108,
    "actor_id": 95,
    "actorid": 95,
    "page_owner_id": 100,
    "entity_id": 72,
}

OBJECT_ID_FIELDS = {
    "post_id": 125,
    "postid": 125,
    "story_fbid": 125,
    "video_id": 125,
    "videoid": 125,
    "reel_id": 125,
    "reelid": 125,
    "photo_id": 125,
    "photoid": 125,
    "media_fbid": 120,
    "album_id": 110,
    "albumid": 110,
    "group_id": 130,
    "groupid": 130,
    "page_id": 130,
    "pageid": 130,
    "event_id": 130,
    "eventid": 130,
}


# ============================================================
# HTTP HEADERS
# ============================================================

# Không có header nào có thể "ép" Facebook trả UID.
# Nhưng bộ header này giúp request gần browser public hơn.

BASE_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;"
        "q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA": (
        '"Chromium";v="140", '
        '"Not=A?Brand";v="24", '
        '"Google Chrome";v="140"'
    ),
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Android"',
}


USER_AGENTS = [
    (
        "Mozilla/5.0 (Linux; Android 15; SM-S938B) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
]


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


@dataclass
class PageSnapshot:
    requested_url: str = ""
    final_url: str = ""

    status_code: int = 0
    content_type: str = ""

    title: str = ""

    meta: Dict[str, str] = field(default_factory=dict)
    links: List[str] = field(default_factory=list)

    json_objects: List[Any] = field(default_factory=list)
    jsonld_objects: List[Any] = field(default_factory=list)

    html_text: str = ""

    redirect_chain: List[str] = field(default_factory=list)

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

    evidence: List[Evidence] = field(default_factory=list)


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

    signals: List[str] = field(default_factory=list)

    notes: List[str] = field(default_factory=list)

    elapsed: float = 0.0

    error: str = ""


# ============================================================
# GLOBAL CACHE
# ============================================================

_CACHE: Dict[str, Tuple[float, ResolveResult]] = {}


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

    value = re.sub(r"\s+", " ", value)

    return value.strip()


def tg_escape(value: Any) -> str:
    return html.escape(clean_text(value), quote=True)


def truncate(value: str, limit: int = 500) -> str:
    value = clean_text(value)

    if len(value) <= limit:
        return value

    return value[: limit - 1].rstrip() + "…"


def is_generic_name(value: str) -> bool:
    value = clean_text(value)

    if not value:
        return True

    lowered = value.lower()

    generic = {
        "facebook",
        "facebook watch",
        "log in",
        "login",
        "watch",
        "video",
        "photos",
        "photo",
        "home",
        "facebook - log in or sign up",
    }

    if lowered in generic:
        return True

    return False


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
        value = re.sub(pattern, "", value, flags=re.I)

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

    return value


# ============================================================
# URL NORMALIZATION
# ============================================================

def normalize_url(url: str) -> str:
    url = clean_text(url)

    if not url:
        return ""

    url = unquote(url)

    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    parsed = urlparse(url)

    scheme = "https"

    host = parsed.netloc.lower().split("@")[-1]

    if ":" in host:
        host = host.split(":")[0]

    path = re.sub(r"/+", "/", parsed.path or "/")

    if not path.startswith("/"):
        path = "/" + path

    query_items = []

    for key, value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):
        key_lower = key.lower()

        if key_lower in TRACKING_PARAMS:
            continue

        if key_lower.startswith("utm_"):
            continue

        query_items.append((key, value))

    query = urlencode(query_items, doseq=True)

    return urlunparse(
        (
            scheme,
            host,
            path,
            "",
            query,
            "",
        )
    )


def clean_url(url: str) -> str:
    normalized = normalize_url(url)

    if not normalized:
        return ""

    parsed = urlparse(normalized)

    host = parsed.netloc.lower()

    if host == "m.facebook.com":
        host = "www.facebook.com"

    if host == "mbasic.facebook.com":
        host = "www.facebook.com"

    if host == "mobile.facebook.com":
        host = "www.facebook.com"

    if host == "web.facebook.com":
        host = "www.facebook.com"

    if host == "touch.facebook.com":
        host = "www.facebook.com"

    path = re.sub(r"/+", "/", parsed.path)

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

        query_items.append((key, value))

    return urlunparse(
        (
            "https",
            host,
            path,
            "",
            urlencode(query_items, doseq=True),
            "",
        )
    )


# ============================================================
# FACEBOOK URL SCANNER
# ============================================================

FACEBOOK_URL_RE = re.compile(
    r"https?://"
    r"(?:"
    r"(?:www|m|mbasic|mobile|web|touch)\.facebook\.com"
    r"|fb\.watch"
    r")"
    r"(?:/[^\s<>\"]*)?",
    re.I,
)


def extract_facebook_urls(text: str) -> List[str]:
    if not text:
        return []

    found = FACEBOOK_URL_RE.findall(text)

    # Also support comma / newline / accidental concatenation.
    for token in re.split(r"[\s,;]+", text):
        token = token.strip()

        if "facebook.com/" in token.lower():
            found.append(token)

    result = []

    seen = set()

    for item in found:
        item = item.rstrip(".,);]}>'\"")

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
# URL CLASSIFICATION
# ============================================================

def classify_url(url: str) -> URLShape:
    normalized = clean_url(url)

    parsed = urlparse(normalized)

    host = parsed.netloc.lower()

    path = parsed.path

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

    shape.query_id = (
        digits(query.get("id"))
        or digits(query.get("user_id"))
        or digits(query.get("profile_id"))
    )

    if "fb.watch" in host:
        shape.kind = "video"
        shape.opaque_token = segments[0] if segments else ""
        return shape

    if not segments:
        shape.kind = "home"
        return shape

    first = segments[0].lower()

    # --------------------------------------------------------
    # profile.php
    # --------------------------------------------------------

    if first == "profile.php":
        shape.kind = "profile"

        if shape.query_id:
            shape.numeric_path_id = shape.query_id

        return shape

    # --------------------------------------------------------
    # photo.php
    # --------------------------------------------------------

    if first == "photo.php":
        shape.kind = "photo"

        shape.photo_id = (
            digits(query.get("fbid"))
            or digits(query.get("photo_id"))
            or digits(query.get("id"))
        )

        return shape

    # --------------------------------------------------------
    # video.php
    # --------------------------------------------------------

    if first == "video.php":
        shape.kind = "video"

        shape.video_id = (
            digits(query.get("v"))
            or digits(query.get("video_id"))
            or digits(query.get("id"))
        )

        return shape

    # --------------------------------------------------------
    # story.php
    # --------------------------------------------------------

    if first == "story.php":
        shape.kind = "story"

        shape.story_id = (
            digits(query.get("story_fbid"))
            or digits(query.get("id"))
        )

        return shape

    # --------------------------------------------------------
    # groups
    # --------------------------------------------------------

    if first == "groups":
        shape.kind = "group"

        if len(segments) >= 2:
            second = segments[1]

            if digits(second):
                shape.group_id = second

            else:
                shape.username = normalize_username(second)

        for seg in segments:
            if digits(seg):
                if not shape.group_id:
                    shape.group_id = seg

        return shape

    # --------------------------------------------------------
    # pages
    # --------------------------------------------------------

    if first == "pages":
        shape.kind = "page"

        if len(segments) >= 2:
            shape.username = normalize_username(segments[1])

        for seg in segments:
            if digits(seg):
                shape.page_id = seg

        return shape

    # --------------------------------------------------------
    # events
    # --------------------------------------------------------

    if first == "events":
        shape.kind = "event"

        for seg in segments:
            if digits(seg):
                shape.event_id = seg

        return shape

    # --------------------------------------------------------
    # numeric first segment
    # --------------------------------------------------------

    if digits(first):
        shape.numeric_path_id = first

        if len(segments) >= 2:
            second = segments[1].lower()

            if second in {
                "videos",
                "video",
            }:
                shape.kind = "video"

                if len(segments) >= 3:
                    shape.video_id = digits(segments[2])

                return shape

            if second in {
                "reel",
                "reels",
            }:
                shape.kind = "reel"

                if len(segments) >= 3:
                    shape.reel_id = digits(segments[2])

                return shape

            if second in {
                "posts",
                "post",
            }:
                shape.kind = "post"

                if len(segments) >= 3:
                    shape.post_id = digits(segments[2])

                return shape

        shape.kind = "numeric_profile_or_object"

        return shape

    # --------------------------------------------------------
    # username + object route
    # --------------------------------------------------------

    first_username = normalize_username(first)

    if first_username:
        shape.username = first_username

    if len(segments) >= 2:
        second = segments[1].lower()

        if second in {"videos", "video"}:
            shape.kind = "video"

            if len(segments) >= 3:
                shape.video_id = digits(segments[2])

            return shape

        if second in {"reel", "reels"}:
            shape.kind = "reel"

            if len(segments) >= 3:
                shape.reel_id = digits(segments[2])

            return shape

        if second in {"posts", "post"}:
            shape.kind = "post"

            if len(segments) >= 3:
                shape.post_id = digits(segments[2])

            return shape

        if second in {"photos", "photo"}:
            shape.kind = "photo"

            if len(segments) >= 3:
                shape.photo_id = digits(segments[2])

            return shape

        if second in {"stories", "story"}:
            shape.kind = "story"

            if len(segments) >= 3:
                shape.story_id = digits(segments[2])

            return shape

    # --------------------------------------------------------
    # plain profile
    # --------------------------------------------------------

    if len(segments) == 1:
        shape.kind = "profile"

        return shape

    shape.kind = "unknown"

    return shape


# ============================================================
# PROFILE URL
# ============================================================

def profile_base_from_url(url: str) -> str:
    shape = classify_url(url)

    if shape.kind in {
        "profile",
    }:
        return clean_url(url)

    if shape.kind in {
        "group",
        "page",
        "event",
    }:
        return ""

    parsed = urlparse(clean_url(url))

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

    username = normalize_username(first)

    if not username:
        return ""

    return (
        "https://www.facebook.com/"
        + username
    )


# ============================================================
# HTML PARSER
# ============================================================

class FacebookHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(
            convert_charrefs=True,
        )

        self.meta: Dict[str, str] = {}

        self.links: List[str] = []

        self.scripts: List[Tuple[str, str]] = []

        self._script_type = ""

        self._script_data: List[str] = []

        self._title_data: List[str] = []

        self.in_title = False

    def handle_starttag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ):
        attrs_dict = dict(attrs)

        tag_lower = tag.lower()

        if tag_lower == "meta":
            key = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or attrs_dict.get("itemprop")
            )

            content = attrs_dict.get("content")

            if key and content:
                self.meta[
                    key.strip().lower()
                ] = clean_text(content)

        elif tag_lower == "a":
            href = attrs_dict.get("href")

            if href:
                self.links.append(href)

        elif tag_lower == "script":
            self._script_type = (
                attrs_dict.get("type")
                or ""
            ).lower()

            self._script_data = []

        elif tag_lower == "title":
            self.in_title = True
            self._title_data = []

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()

        if tag_lower == "script":
            data = "".join(
                self._script_data
            )

            if data.strip():
                self.scripts.append(
                    (
                        self._script_type,
                        data,
                    )
                )

            self._script_type = ""

            self._script_data = []

        elif tag_lower == "title":
            self.in_title = False

    def handle_data(self, data: str):
        if self.in_title:
            self._title_data.append(data)

        if self._script_type:
            self._script_data.append(data)

    @property
    def title(self) -> str:
        return normalize_name(
            "".join(self._title_data)
        )


# ============================================================
# JSON PARSING
# ============================================================

def parse_json_safely(text: str) -> Optional[Any]:
    text = text.strip()

    if not text:
        return None

    try:
        return json.loads(text)

    except Exception:
        pass

    # Facebook sometimes embeds JSON with
    # JS escape sequences.

    cleaned = text.strip()

    cleaned = cleaned.replace(
        "\\/",
        "/",
    )

    try:
        return json.loads(cleaned)

    except Exception:
        return None


def extract_json_objects(
    parser: FacebookHTMLParser,
) -> Tuple[List[Any], List[Any]]:

    json_objects = []

    jsonld_objects = []

    for script_type, data in parser.scripts:

        obj = parse_json_safely(data)

        if obj is not None:

            if (
                "ld+json" in script_type
            ):
                jsonld_objects.append(obj)

            else:
                json_objects.append(obj)

    return json_objects, jsonld_objects


# ============================================================
# GENERIC EMBEDDED JSON SCANNER
# ============================================================

def scan_embedded_json_strings(
    html_text: str,
) -> List[Dict[str, Any]]:

    results = []

    if not html_text:
        return results

    patterns = [
        r'"(user_id|profile_id|owner_id|publisher_id|author_id|creator_id|from_id)"\s*:\s*"?(\d{5,30})',
        r'"(post_id|video_id|reel_id|photo_id|story_fbid|media_fbid)"\s*:\s*"?(\d{5,30})',
        r'"(page_id|group_id|event_id)"\s*:\s*"?(\d{5,30})',
    ]

    for pattern in patterns:

        try:
            matches = re.findall(
                pattern,
                html_text,
                flags=re.I,
            )

        except Exception:
            matches = []

        for key, value in matches:

            results.append(
                {
                    "key": key,
                    "value": value,
                }
            )

            if len(results) >= 500:
                return results

    return results


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

        for index, value in enumerate(obj):

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

    AVATAR_KEYS = {
        "avatar",
        "avatar_url",
        "profile_picture",
        "profile_pic",
        "profilepic",
        "profile_picture_url",
        "profile_pic_url",
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

        evidence: List[Evidence] = []

        def add(
            value: str,
            role: str,
            source: str,
            key: str = "",
            path: str = "",
            neighbor: str = "",
            weight: float = 0,
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
                    url=snapshot.final_url
                    or snapshot.requested_url,
                    weight=weight,
                )
            )

        # ----------------------------------------------------
        # META
        # ----------------------------------------------------

        meta = snapshot.meta

        for key, value in meta.items():

            key_lower = key.lower()

            if key_lower in {
                "og:title",
                "twitter:title",
            }:
                name = normalize_name(value)

                if name:
                    add(
                        name,
                        "NAME",
                        "meta",
                        key,
                        weight=70,
                    )

            elif key_lower in {
                "og:description",
                "twitter:description",
            }:
                value = truncate(
                    value,
                    1000,
                )

                if value:
                    add(
                        value,
                        "DESCRIPTION",
                        "meta",
                        key,
                        weight=55,
                    )

            elif key_lower in {
                "og:image",
                "twitter:image",
            }:
                add(
                    value,
                    "IMAGE",
                    "meta",
                    key,
                    weight=45,
                )

            elif key_lower in {
                "og:url",
                "twitter:url",
            }:
                add(
                    value,
                    "CANONICAL",
                    "meta",
                    key,
                    weight=80,
                )

        # ----------------------------------------------------
        # JSON
        # ----------------------------------------------------

        for obj in snapshot.json_objects:

            for value, path in walk_json(obj):

                if not isinstance(value, dict):
                    continue

                local_items = list(
                    value.items()
                )

                neighbor_text = ""

                for k, v in local_items:

                    if isinstance(v, str):

                        if k.lower() in {
                            "name",
                            "display_name",
                            "full_name",
                            "username",
                            "user_name",
                        }:
                            neighbor_text += (
                                " "
                                + clean_text(v)
                            )

                for key, raw in local_items:

                    key_lower = str(key).lower()

                    value_str = (
                        str(raw)
                        if isinstance(
                            raw,
                            (str, int),
                            )
                        else ""
                    )

                    if not value_str:
                        continue

                    # USER IDS
                    if (
                        key_lower
                        in {
                            x.lower()
                            for x in USER_ID_FIELDS
                        }
                    ):
                        value_id = digits(
                            value_str
                        )

                        if value_id:
                            weight = (
                                USER_ID_FIELDS.get(
                                    key,
                                    USER_ID_FIELDS.get(
                                        key_lower,
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
                                neighbor_text[:300],
                                weight,
                            )

                    # OBJECT IDS
                    if (
                        key_lower
                        in {
                            x.lower()
                            for x in OBJECT_ID_FIELDS
                        }
                    ):
                        value_id = digits(
                            value_str
                        )

                        if value_id:

                            weight = (
                                OBJECT_ID_FIELDS.get(
                                    key,
                                    OBJECT_ID_FIELDS.get(
                                        key_lower,
                                        60,
                                    ),
                                )
                            )

                            add(
                                value_id,
                                "OBJECT_CANDIDATE",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor_text[:300],
                                weight,
                            )

                    # NAME
                    if (
                        key_lower
                        in {
                            x.lower()
                            for x in self.NAME_KEYS
                        }
                    ):
                        name = normalize_name(
                            value_str
                        )

                        if name:
                            add(
                                name,
                                "NAME",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor_text[:300],
                                65,
                            )

                    # USERNAME
                    if (
                        key_lower
                        in {
                            x.lower()
                            for x in self.USERNAME_KEYS
                        }
                    ):
                        username = normalize_username(
                            value_str
                        )

                        if username:
                            add(
                                username,
                                "USERNAME",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor_text[:300],
                                75,
                            )

                    # IMAGE
                    if (
                        key_lower
                        in {
                            x.lower()
                            for x in self.AVATAR_KEYS
                        }
                    ):
                        if (
                            value_str.startswith(
                                "http"
                            )
                        ):
                            add(
                                value_str,
                                "AVATAR",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor_text[:300],
                                60,
                            )

                    # BIO
                    if (
                        key_lower
                        in {
                            x.lower()
                            for x in self.BIO_KEYS
                        }
                    ):
                        description = truncate(
                            value_str,
                            1000,
                        )

                        if description:
                            add(
                                description,
                                "BIO",
                                "embedded_json",
                                str(key),
                                path,
                                neighbor_text[:300],
                                55,
                            )

        # ----------------------------------------------------
        # JSON-LD
        # ----------------------------------------------------

        for obj in snapshot.jsonld_objects:

            for value, path in walk_json(obj):

                if not isinstance(value, dict):
                    continue

                for key, raw in value.items():

                    key_lower = str(key).lower()

                    if isinstance(
                        raw,
                        (str, int),
                    ):
                        value_str = str(raw)

                    elif isinstance(raw, dict):

                        value_str = str(
                            raw.get("url")
                            or raw.get("@id")
                            or raw.get("name")
                            or ""
                        )

                    else:
                        continue

                    if key_lower == "name":

                        name = normalize_name(
                            value_str
                        )

                        if name:
                            add(
                                name,
                                "NAME",
                                "jsonld",
                                str(key),
                                path,
                                weight=80,
                            )

                    elif key_lower in {
                        "url",
                        "@id",
                    }:

                        if (
                            "facebook.com"
                            in value_str.lower()
                        ):
                            add(
                                value_str,
                                "PROFILE_URL",
                                "jsonld",
                                str(key),
                                path,
                                weight=75,
                            )

                    elif key_lower in {
                        "description",
                    }:

                        description = truncate(
                            value_str,
                            1000,
                        )

                        if description:
                            add(
                                description,
                                "BIO",
                                "jsonld",
                                str(key),
                                path,
                                weight=60,
                            )

        # ----------------------------------------------------
        # RAW SCRIPT REGEX
        # ----------------------------------------------------

        for item in scan_embedded_json_strings(
            snapshot.html_text
        ):

            key = item["key"]

            value = item["value"]

            if key.lower() in {
                x.lower()
                for x in USER_ID_FIELDS
            }:
                add(
                    value,
                    "USER_CANDIDATE",
                    "raw_embedded",
                    key,
                    weight=82,
                )

            elif key.lower() in {
                x.lower()
                for x in OBJECT_ID_FIELDS
            }:
                add(
                    value,
                    "OBJECT_CANDIDATE",
                    "raw_embedded",
                    key,
                    weight=82,
                )

        return evidence


# ============================================================
# FETCH ENGINE
# ============================================================

class FetchEngine:

    def __init__(self):
        self._semaphore = asyncio.Semaphore(4)

    def _headers(self) -> Dict[str, str]:

        headers = dict(BASE_HEADERS)

        headers["User-Agent"] = USER_AGENTS[
            int(time.time()) % len(USER_AGENTS)
        ]

        return headers

    def fetch_sync(
        self,
        url: str,
    ) -> PageSnapshot:

        started = time.perf_counter()

        snapshot = PageSnapshot(
            requested_url=url,
        )

        session = requests.Session()

        try:

            response = session.get(
                url,
                headers=self._headers(),
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
                    "",
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

            snapshot.html_text = text

            parser = FacebookHTMLParser()

            try:
                parser.feed(text)

            except Exception as exc:
                logger.debug(
                    "HTML parser error: %s",
                    exc,
                )

            snapshot.meta = parser.meta

            snapshot.links = parser.links[:1000]

            snapshot.title = parser.title

            (
                snapshot.json_objects,
                snapshot.jsonld_objects,
            ) = extract_json_objects(
                parser
            )

            return snapshot

        except requests.RequestException as exc:

            snapshot.error = (
                f"{type(exc).__name__}: {exc}"
            )

            return snapshot

        except Exception as exc:

            snapshot.error = (
                f"{type(exc).__name__}: {exc}"
            )

            return snapshot

        finally:

            snapshot.elapsed = (
                time.perf_counter()
                - started
            )

            try:
                session.close()
            except Exception:
                pass

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
# META / URL EVIDENCE
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

            name = normalize_name(value)

            if name:
                result.append(
                    Evidence(
                        value=name,
                        role="NAME",
                        source="meta",
                        key=key,
                        url=snapshot.final_url,
                        weight=80,
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
                    weight=85,
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
                )
            )

    return result


# ============================================================
# URL EVIDENCE
# ============================================================

def url_evidence(
    snapshot: PageSnapshot,
) -> List[Evidence]:

    result = []

    final_url = snapshot.final_url

    if final_url:

        shape = classify_url(
            final_url
        )

        if shape.username:

            result.append(
                Evidence(
                    value=shape.username,
                    role="USERNAME",
                    source="url",
                    key="path",
                    url=final_url,
                    weight=80,
                )
            )

        if shape.numeric_path_id:

            # VERY IMPORTANT:
            #
            # Numeric first segment of:
            # /123/videos/456
            #
            # is NOT automatically UID.
            #
            result.append(
                Evidence(
                    value=shape.numeric_path_id,
                    role="ROUTE_ENTITY",
                    source="url",
                    key="numeric_path",
                    url=final_url,
                    weight=35,
                )
            )

        for role, value in [
            ("POST_ID", shape.post_id),
            ("VIDEO_ID", shape.video_id),
            ("REEL_ID", shape.reel_id),
            ("PHOTO_ID", shape.photo_id),
            ("STORY_ID", shape.story_id),
            ("GROUP_ID", shape.group_id),
            ("PAGE_ID", shape.page_id),
            ("EVENT_ID", shape.event_id),
        ]:

            if value:

                result.append(
                    Evidence(
                        value=value,
                        role=role,
                        source="url",
                        key=role.lower(),
                        url=final_url,
                        weight=100,
                    )
                )

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

        if key and not result[key]:

            result[key] = digits(
                item.value
            )

    return result


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

    # URL username is useful, but does NOT prove UID.

    shape = classify_url(
        info.profile_url
    )

    if (
        not info.username
        and shape.username
    ):
        info.username = shape.username

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

        text = truncate(
            item.value,
            700,
        )

        if text:
            info.bio = text
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

    # --------------------------------------------------------
    # ENTITY
    # --------------------------------------------------------

    info.entity_type = "USER"

    return info


# ============================================================
# UID CANDIDATE SCORING
# ============================================================

def score_uid_candidates(
    evidence: List[Evidence],
) -> List[Tuple[str, float, List[Evidence]]]:

    grouped: Dict[
        str,
        List[Evidence]
    ] = {}

    for item in evidence:

        value = digits(
            item.value
        )

        if not value:
            continue

        grouped.setdefault(
            value,
            [],
        ).append(item)

    results = []

    for value, items in grouped.items():

        # ----------------------------------------------------
        # Never treat object IDs as UID.
        # ----------------------------------------------------

        object_values = {
            digits(x.value)
            for x in evidence
            if x.role == "OBJECT_CANDIDATE"
        }

        if value in object_values:
            continue

        score = 0.0

        sources = set()

        strong_fields = set()

        for item in items:

            source_key = (
                item.source
            )

            if source_key not in sources:
                score += min(
                    item.weight,
                    120,
                ) * 0.35

            sources.add(
                source_key
            )

            field = item.key.lower()

            if field in {
                "user_id",
                "userid",
                "profile_id",
                "profileid",
                "owner_id",
                "ownerid",
                "creator_id",
                "creatorid",
                "publisher_id",
                "author_id",
                "from_id",
            }:
                strong_fields.add(
                    field
                )

        # Strong semantic field.
        if strong_fields:
            score += 35

        # Multiple independent source families.
        if len(sources) >= 2:
            score += 25

        if len(sources) >= 3:
            score += 15

        # Multiple strong fields.
        if len(strong_fields) >= 2:
            score += 15

        # Cap.
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
) -> Tuple[bool, List[str]]:

    signals = []

    if not expected_uid:
        return False, signals

    # --------------------------------------------------------
    # Explicit UID on profile page
    # --------------------------------------------------------

    profile_candidates = [
        x
        for x in profile_evidence
        if x.role == "USER_CANDIDATE"
    ]

    matching = [
        x
        for x in profile_candidates
        if digits(x.value) == expected_uid
    ]

    if matching:

        signals.append(
            "profile → UID khớp"
        )

        return True, signals

    # --------------------------------------------------------
    # If several strong fields independently agree.
    # --------------------------------------------------------

    distinct_sources = {
        x.source
        for x in profile_candidates
        if digits(x.value) == expected_uid
    }

    if len(distinct_sources) >= 2:

        signals.append(
            "profile → nhiều nguồn UID khớp"
        )

        return True, signals

    return False, signals


# ============================================================
# CONFLICT DETECTOR
# ============================================================

def detect_uid_conflicts(
    candidates: List[
        Tuple[str, float, List[Evidence]]
    ],
) -> List[str]:

    if len(candidates) <= 1:
        return []

    strong = []

    for uid, score, evidence in candidates:

        if score >= 55:
            strong.append(
                uid
            )

    unique = list(
        dict.fromkeys(strong)
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
            "og:url"
        )
        or snapshot.meta.get(
            "twitter:url"
        )
        or snapshot.final_url
        or fallback
    )

    candidate = clean_url(
        candidate
    )

    return candidate


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
# RESULT RESOLVER
# ============================================================

class FacebookResolver:

    def __init__(self):

        self.fetcher = FetchEngine()

        self.semantic = SemanticExtractor()

    async def resolve(
        self,
        url: str,
    ) -> ResolveResult:

        started = time.perf_counter()

        result = ResolveResult(
            input_url=clean_url(url)
        )

        try:

            # ------------------------------------------------
            # CACHE
            # ------------------------------------------------

            cache_key = clean_url(
                url
            ).lower()

            cached = _CACHE.get(
                cache_key
            )

            if cached:

                timestamp, cached_result = cached

                if (
                    time.time()
                    - timestamp
                    < CACHE_TTL
                ):

                    cloned = ResolveResult(
                        **{
                            k: v
                            for k, v
                            in cached_result.__dict__.items()
                        }
                    )

                    cloned.input_url = (
                        clean_url(url)
                    )

                    return cloned

            # ------------------------------------------------
            # CLASSIFY INPUT
            # ------------------------------------------------

            shape = classify_url(
                url
            )

            result.content_type = (
                content_type_from_shape(
                    shape
                )
            )

            result.profile_url = (
                profile_base_from_url(
                    url
                )
            )

            # ------------------------------------------------
            # FETCH
            # ------------------------------------------------

            snapshot = await self.fetcher.fetch(
                clean_url(url)
            )

            # ------------------------------------------------
            # ALWAYS BUILD BASIC RESULT
            # ------------------------------------------------

            result.canonical_url = (
                canonical_url(
                    snapshot,
                    url,
                )
            )

            result.content_url = (
                result.canonical_url
            )

            if snapshot.error:

                result.error = snapshot.error

                result.notes.append(
                    "Facebook không trả được trang public."
                )

            # ------------------------------------------------
            # EVIDENCE
            # ------------------------------------------------

            evidence = []

            evidence.extend(
                meta_evidence(
                    snapshot
                )
            )

            evidence.extend(
                url_evidence(
                    snapshot
                )
            )

            evidence.extend(
                self.semantic.extract(
                    snapshot
                )
            )

            # ------------------------------------------------
            # PROFILE URL
            # ------------------------------------------------

            if not result.profile_url:

                result.profile_url = (
                    profile_base_from_url(
                        result.canonical_url
                    )
                )

            # JSON-LD / links can reveal
            # a more authoritative profile URL.

            discovered_profiles = (
                self.discover_profile_urls(
                    snapshot,
                    evidence,
                )
            )

            if discovered_profiles:

                # Prefer profile URL matching
                # original username.

                if shape.username:

                    matching = [
                        x
                        for x
                        in discovered_profiles
                        if shape.username.lower()
                        in x.lower()
                    ]

                    if matching:
                        result.profile_url = (
                            matching[0]
                        )

                    else:
                        result.profile_url = (
                            discovered_profiles[0]
                        )

                elif not result.profile_url:

                    result.profile_url = (
                        discovered_profiles[0]
                    )

            # ------------------------------------------------
            # METADATA
            # ------------------------------------------------

            self._merge_metadata(
                result,
                shape,
                snapshot,
                evidence,
            )

            # ------------------------------------------------
            # OBJECTS
            # ------------------------------------------------

            objects = extract_object_ids(
                shape,
                evidence,
            )

            result.post_id = objects[
                "post_id"
            ]

            result.video_id = objects[
                "video_id"
            ]

            result.reel_id = objects[
                "reel_id"
            ]

            result.photo_id = objects[
                "photo_id"
            ]

            result.story_id = objects[
                "story_id"
            ]

            result.album_id = objects[
                "album_id"
            ]

            # ------------------------------------------------
            # ENTITY
            # ------------------------------------------------

            result.entity_type = (
                classify_entity(
                    shape,
                    evidence,
                )
            )

            if result.entity_type == "USER":

                result.publisher_type = (
                    "USER"
                )

            elif result.entity_type == "PAGE":

                result.publisher_type = (
                    "PAGE"
                )

            elif result.entity_type == "GROUP":

                result.publisher_type = (
                    "GROUP"
                )

            else:

                result.publisher_type = (
                    "UNKNOWN"
                )

            # ------------------------------------------------
            # UID CANDIDATES
            # ------------------------------------------------

            uid_evidence = [
                x
                for x
                in evidence
                if x.role
                == "USER_CANDIDATE"
            ]

            candidates = score_uid_candidates(
                uid_evidence
            )

            conflicts = (
                detect_uid_conflicts(
                    candidates
                )
            )

            # ------------------------------------------------
            # PROFILE VERIFICATION
            # ------------------------------------------------

            profile_info = ProfileInfo()

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

                if (
                    profile_snapshot.html_text
                ):

                    profile_evidence = []

                    profile_evidence.extend(
                        meta_evidence(
                            profile_snapshot
                        )
                    )

                    profile_evidence.extend(
                        url_evidence(
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

                    # Merge profile metadata.
                    self._merge_profile(
                        result,
                        profile_info,
                    )

                    # ------------------------------------------------
                    # VERIFY TOP UID
                    # ------------------------------------------------

                    if candidates:

                        top_uid = candidates[0][0]

                        verified, signals = (
                            verify_profile_identity(
                                top_uid,
                                profile_info,
                                profile_evidence,
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

                    # ------------------------------------------------
                    # If profile itself has explicit UID
                    # ------------------------------------------------

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

                            if not conflicts:

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

            # ------------------------------------------------
            # MULTI SOURCE VERIFICATION
            # ------------------------------------------------

            if not result.uid and candidates:

                top_uid, score, top_items = (
                    candidates[0]
                )

                source_count = len(
                    {
                        x.source
                        for x in top_items
                    }
                )

                # Need strong evidence from
                # multiple independent source families.

                if (
                    score >= 78
                    and source_count >= 2
                    and not conflicts
                ):

                    result.uid = top_uid

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
                        "UID khớp nhiều nguồn độc lập"
                    )

            # ------------------------------------------------
            # USERNAME / CANONICAL CORRELATION
            # ------------------------------------------------

            if result.username:

                if result.profile_url:

                    result.signals.append(
                        "canonical → username"
                    )

            # ------------------------------------------------
            # CONFLICT
            # ------------------------------------------------

            if conflicts:

                result.verified = False

                result.uid = ""

                result.confidence = 0

                result.notes.append(
                    "Phát hiện nhiều UID cạnh tranh; UID bị khóa để tránh đoán sai."
                )

            # ------------------------------------------------
            # NO UID
            # ------------------------------------------------

            if not result.uid:

                result.verified = False

                result.confidence = 0

                if result.entity_type == "USER":

                    result.notes.append(
                        "Facebook không công khai đủ ID để liên kết username/profile với UID."
                    )

                else:

                    result.notes.append(
                        "Có dữ liệu public nhưng chưa đủ bằng chứng xác định UID."
                    )

            # ------------------------------------------------
            # EVIDENCE SOURCE COUNT
            # ------------------------------------------------

            relevant = [
                x
                for x
                in evidence
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
                    x.source
                    for x in relevant
                }
            )

            # ------------------------------------------------
            # ELAPSED
            # ------------------------------------------------

            result.elapsed = (
                time.perf_counter()
                - started
            )

            # ------------------------------------------------
            # CACHE
            # ------------------------------------------------

            _CACHE[cache_key] = (
                time.time(),
                result,
            )

            # Cleanup cache.
            self.cleanup_cache()

            return result

        except Exception as exc:

            # CRITICAL:
            # Never silently die.

            logger.exception(
                "Resolver error"
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

        names = [
            x.value
            for x in evidence
            if x.role == "NAME"
        ]

        if names and not result.name:

            for name in names:

                name = normalize_name(
                    name
                )

                if name:
                    result.name = name
                    break

        if not result.username:

            usernames = [
                x.value
                for x in evidence
                if x.role == "USERNAME"
            ]

            for username in usernames:

                username = normalize_username(
                    username
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

        if not result.title:

            result.title = (
                content_title(
                    snapshot
                )
            )

        if not result.avatar_url:

            for item in evidence:

                if item.role in {
                    "AVATAR",
                    "IMAGE",
                }:

                    if item.value.startswith(
                        "http"
                    ):

                        result.avatar_url = (
                            item.value
                        )

                        break

        if not result.bio:

            descriptions = [
                x.value
                for x
                in evidence
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

            result.name = profile.name

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

            result.bio = profile.bio

        if profile.entity_type:

            if result.entity_type == "UNKNOWN":

                result.entity_type = (
                    profile.entity_type
                )

    # ========================================================
    # PROFILE DISCOVERY
    # ========================================================

    def discover_profile_urls(
        self,
        snapshot: PageSnapshot,
        evidence: List[Evidence],
    ) -> List[str]:

        found = []

        def add(url: str):

            url = clean_url(
                url
            )

            if not url:
                return

            parsed = urlparse(url)

            if (
                parsed.netloc.lower()
                not in FACEBOOK_HOSTS
            ):
                return

            profile = (
                profile_base_from_url(
                    url
                )
            )

            if not profile:
                return

            if profile not in found:

                found.append(
                    profile
                )

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

                add(
                    absolute
                )

            except Exception:
                continue

            if (
                len(found)
                >= MAX_PROFILE_CHECKS
            ):
                break

        return found[
            :MAX_PROFILE_CHECKS
        ]

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

        uid, score, items = candidates[0]

        value = 35.0

        strong_fields = {
            x.key.lower()
            for x in items
            if x.key
        }

        sources = {
            x.source
            for x in items
        }

        if strong_fields:
            value += 20

        if len(sources) >= 2:
            value += 18

        if len(sources) >= 3:
            value += 8

        if profile_verified:
            value += 17

        if username_match:
            value += 5

        if len(strong_fields) >= 2:
            value += 5

        if conflicts:
            value -= 45

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
            if now - timestamp
            > CACHE_TTL
        ]

        for key in expired:

            _CACHE.pop(
                key,
                None,
            )


# ============================================================
# RESULT FORMAT
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

    # --------------------------------------------------------
    # ENTITY
    # --------------------------------------------------------

    if result.name or result.username or result.uid:

        lines.append(
            "│ 👤 <b>NGƯỜI ĐĂNG</b>"
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

        type_map = {
            "USER": "👤 Cá nhân",
            "PAGE": "📄 Trang",
            "GROUP": "👥 Nhóm",
            "EVENT": "📅 Sự kiện",
            "UNKNOWN": "❔ Chưa xác định",
        }

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
                        280,
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

    # --------------------------------------------------------
    # CONTENT
    # --------------------------------------------------------

    has_content = any(
        [
            result.content_type,
            result.post_id,
            result.video_id,
            result.reel_id,
            result.photo_id,
            result.story_id,
            result.title,
        ]
    )

    if has_content:

        lines.append(
            "│"
        )

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
            ("Post ID", result.post_id),
            ("Video ID", result.video_id),
            ("Reel ID", result.reel_id),
            ("Photo ID", result.photo_id),
            ("Story ID", result.story_id),
            ("Album ID", result.album_id),
        ]

        for label, value in object_rows:

            if value:

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
                        300,
                    )
                )
            )

    # --------------------------------------------------------
    # LINKS
    # --------------------------------------------------------

    lines.append(
        "│"
    )

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

    # --------------------------------------------------------
    # VERIFICATION
    # --------------------------------------------------------

    lines.append(
        "│"
    )

    lines.append(
        "│ 🛡 <b>XÁC MINH</b>"
    )

    if result.verified and result.uid:

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

        for signal in result.signals[:4]:

            lines.append(
                "│ • "
                + tg_escape(
                    signal
                )
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
                        260,
                    )
                )
            )

    # --------------------------------------------------------
    # TIME
    # --------------------------------------------------------

    lines.append(
        "│"
    )

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

        text = text[
            :MAX_OUTPUT_CHARS - 20
        ] + "\n…"

    return text


# ============================================================
# COMBINED OUTPUT
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
# MESSAGE HANDLER
# ============================================================

async def process_urls(
    event,
    urls: List[str],
):

    urls = urls[:MAX_INPUT_URLS]

    if not urls:

        await event.reply(
            "❌ Không tìm thấy URL Facebook hợp lệ."
        )

        return

    # --------------------------------------------------------
    # One resolver for one command.
    # --------------------------------------------------------

    resolver = FacebookResolver()

    # --------------------------------------------------------
    # Parallel but controlled.
    # --------------------------------------------------------

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

    results = await asyncio.gather(
        *[
            one(url)
            for url in urls
        ],
        return_exceptions=False,
    )

    # --------------------------------------------------------
    # Always return something.
    # --------------------------------------------------------

    if not results:

        await event.reply(
            "⚠️ Không có kết quả."
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

    # --------------------------------------------------------
    # ONE TELEGRAM MESSAGE.
    # --------------------------------------------------------

    try:

        await event.reply(
            text,
            parse_mode="html",
            link_preview=False,
        )

    except Exception as exc:

        logger.exception(
            "HTML output failed"
        )

        # Fallback plain text.
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
# FOLLOW-UP INPUT
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

    future = asyncio.get_running_loop().create_future()

    async def watcher(new_event):

        try:

            if new_event.chat_id != chat_id:
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

            if new_sender_id != sender_id:
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

        client.remove_event_handler(
            watcher,
            events.NewMessage,
        )


# ============================================================
# COMMAND HANDLER
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
            "🔎 <b>FACEBOOK RESOLVER</b>\n\n"
            "Gửi link Facebook cần kiểm tra.\n"
            "Tôi sẽ quét public data nhiều lớp "
            "và chỉ trả UID khi đủ bằng chứng.",
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
            next_event.raw_text or ""
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

    # --------------------------------------------------------
    # Optional non-slash form:
    #
    # getuidfb https://facebook.com/...
    #
    # Kept separate to avoid collision with /getuidfb.
    # --------------------------------------------------------

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
        "Facebook Resolver V40 registered"
    )


# ============================================================
# SELF TEST
# ============================================================

def self_test():

    tests = [
        (
            "https://www.facebook.com/nvlnopro/"
        ),
        (
            "https://www.facebook.com/"
            "kim.chi.125900/"
            "videos/4697823150500072/"
        ),
        (
            "https://www.facebook.com/"
            "61592487939720/"
            "videos/1671583863938536/"
        ),
        (
            "https://www.facebook.com/"
            "reel/4697823150500072/"
        ),
        (
            "https://www.facebook.com/"
            "groups/123456789/"
        ),
        (
            "https://www.facebook.com/"
            "profile.php?id=61500000000000"
        ),
    ]

    for url in tests:

        shape = classify_url(
            url
        )

        print(
            "\nURL:",
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