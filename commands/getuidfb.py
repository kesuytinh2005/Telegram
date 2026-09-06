#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
============================================================
 FACEBOOK FORENSIC UID / ENTITY RESOLVER
 MAX PRECISION EDITION
============================================================

PUBLIC CONTENT ONLY
HTTP ONLY

NO:
- Playwright
- Selenium
- Chromium
- Cookies
- Facebook Login
- Facebook Access Token
- Graph API

DESIGN GOALS:
- Maximum UID precision
- Multi-layer public HTML/JSON analysis
- Identity correlation graph
- Profile verification
- Entity separation
- Conflict detection
- Evidence diversity scoring
- Duplicate suppression
- Concurrent but rate-limited fetching
- One consolidated Telegram response
- Telethon compatible

IMPORTANT:
The resolver NEVER guesses a UID from a username or arbitrary
numeric URL segment.

If evidence is insufficient:
    UID = NOT VERIFIED
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
import threading
import time

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
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


# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

REQUEST_CONNECT_TIMEOUT = 5
REQUEST_READ_TIMEOUT = 12

REQUEST_TIMEOUT = (
    REQUEST_CONNECT_TIMEOUT,
    REQUEST_READ_TIMEOUT,
)

MAX_HTML_BYTES = 12 * 1024 * 1024

MAX_INPUT_URLS = 8
MAX_DISCOVERED_URLS = 50
MAX_PROFILE_CHECKS = 2

MAX_SCRIPT_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 30
MAX_EVIDENCE_PER_VALUE = 20

MAX_CONCURRENT_FETCHES = 4

CACHE_TTL = 90

SESSION_TIMEOUT = 900

USER_AGENT_PROFILES = [
    (
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 15; Pixel 8 Pro) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
]


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
    "lm.facebook.com",
    "l.facebook.com",
    "fb.watch",
}


# ============================================================
# RESERVED FACEBOOK ROUTES
# ============================================================

RESERVED_ROUTES = {
    "watch",
    "reel",
    "reels",
    "videos",
    "video",
    "posts",
    "post",
    "photos",
    "photo",
    "stories",
    "story",
    "groups",
    "group",
    "pages",
    "events",
    "event",
    "profile.php",
    "video.php",
    "photo.php",
    "story.php",
    "permalink.php",
    "share",
    "sharer",
    "share.php",
    "dialog",
    "login",
    "checkpoint",
    "recover",
    "help",
    "settings",
    "privacy",
    "marketplace",
    "gaming",
    "messages",
    "notifications",
    "friends",
    "bookmarks",
    "saved",
    "home",
    "search",
    "directory",
    "hashtag",
}


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
}


TRACKING_PARAMS = {
    "fbclid",
    "rdid",
    "share_url",
    "refsrc",
    "mibextid",
    "__cft__",
    "__tn__",
    "__eep__",
    "__xts__",
    "__mref",
    "notif_id",
    "notif_t",
    "locale",
}


TRACKING_PREFIXES = (
    "utm_",
)


# ============================================================
# ID FIELD WEIGHTS
# ============================================================

USER_FIELD_WEIGHTS = {
    "user_id": 125,
    "userid": 125,
    "userID": 125,
    "profile_id": 125,
    "profileid": 125,
    "profileID": 125,

    "owner_id": 115,
    "ownerid": 115,
    "ownerID": 115,

    "author_id": 112,
    "authorid": 112,
    "authorID": 112,

    "creator_id": 112,
    "creatorid": 112,
    "creatorID": 112,

    "publisher_id": 105,
    "publisherid": 105,
    "publisherID": 105,

    "from_id": 105,
    "fromid": 105,

    "actor_id": 95,
    "actorid": 95,

    "entity_id": 70,
    "entityid": 70,

    "profile_uid": 120,
    "profile.uid": 120,
    "profile.id": 115,

    "owner.id": 110,
    "author.id": 108,
    "creator.id": 108,
    "publisher.id": 105,
    "from.id": 108,
    "actor.id": 90,
    "entity.id": 65,
}


OBJECT_FIELD_WEIGHTS = {
    "post_id": 125,
    "postid": 125,
    "postID": 125,

    "story_fbid": 125,
    "story_id": 120,
    "storyid": 120,

    "video_id": 125,
    "videoid": 125,

    "reel_id": 125,
    "reelid": 125,

    "photo_id": 125,
    "photoid": 125,

    "media_fbid": 120,
    "media_id": 110,

    "album_id": 110,
    "albumid": 110,

    "group_id": 135,
    "groupid": 135,

    "page_id": 135,
    "pageid": 135,

    "event_id": 125,
    "eventid": 125,
}


# ============================================================
# METADATA KEYS
# ============================================================

NAME_KEYS = {
    "name",
    "display_name",
    "displayname",
    "full_name",
    "fullname",
    "short_name",
    "shortname",
    "title",
}

USERNAME_KEYS = {
    "username",
    "user_name",
    "screen_name",
    "screenname",
    "vanity",
    "vanity_name",
    "handle",
}

BIO_KEYS = {
    "bio",
    "biography",
    "about",
    "description",
    "short_description",
}

IMAGE_KEYS = {
    "avatar",
    "avatar_url",
    "profile_picture",
    "profile_pic",
    "profile_image",
    "profile_photo",
    "image",
    "image_url",
    "picture",
    "picture_url",
    "photo",
}

URL_KEYS = {
    "profile_url",
    "profileurl",
    "profile_uri",
    "profileuri",
    "canonical_url",
    "canonical",
    "url",
    "uri",
    "link",
}


# ============================================================
# GENERIC / BAD TITLES
# ============================================================

GENERIC_TITLES = {
    "",
    "facebook",
    "facebook watch",
    "watch",
    "video",
    "videos",
    "reel",
    "reels",
    "photo",
    "photos",
    "log in",
    "login",
    "sign up",
    "sign in",
}


# ============================================================
# DATACLASSES
# ============================================================

@dataclass
class Evidence:
    value: str
    key: str
    path: str
    source: str
    url: str
    role: str
    weight: float
    context: str = ""
    neighbor: str = ""
    independent_group: str = ""
    confidence: float = 0.0


@dataclass
class PageSnapshot:
    requested_url: str
    final_url: str = ""
    canonical_url: str = ""

    status_code: int = 0
    content_type: str = ""

    title: str = ""

    meta: Dict[str, str] = field(default_factory=dict)

    links: List[str] = field(default_factory=list)

    scripts: List[Tuple[str, str]] = field(default_factory=list)

    json_objects: List[Any] = field(default_factory=list)

    jsonld_objects: List[Any] = field(default_factory=list)

    raw_html: str = ""

    blocked: bool = False
    fetched: bool = False

    elapsed: float = 0.0


@dataclass
class URLShape:
    original: str

    normalized: str = ""
    clean: str = ""

    host: str = ""
    path: str = ""
    segments: List[str] = field(default_factory=list)

    kind: str = "unknown"

    username: str = ""

    route_entity_id: str = ""

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

    opaque_token: str = ""


@dataclass
class ProfileInfo:
    name: str = ""
    username: str = ""

    profile_url: str = ""

    avatar_url: str = ""
    bio: str = ""

    entity_type: str = "UNKNOWN"

    explicit_ids: Set[str] = field(default_factory=set)

    evidence: List[Evidence] = field(default_factory=list)


@dataclass
class Candidate:
    uid: str

    score: float = 0.0

    evidence: List[Evidence] = field(default_factory=list)

    independent_sources: Set[str] = field(default_factory=set)
    independent_groups: Set[str] = field(default_factory=set)

    profile_match: bool = False
    username_match: bool = False
    canonical_match: bool = False

    conflicts: List[str] = field(default_factory=list)


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

    avatar_url: str = ""
    bio: str = ""

    page_name: str = ""
    group_name: str = ""

    content_type: str = ""
    title: str = ""
    description: str = ""
    thumbnail_url: str = ""

    post_id: str = ""
    video_id: str = ""
    reel_id: str = ""
    photo_id: str = ""
    story_id: str = ""
    album_id: str = ""

    uid: str = ""

    verified: bool = False
    confidence: float = 0.0

    evidence_sources: int = 0

    signals: List[str] = field(default_factory=list)

    notes: List[str] = field(default_factory=list)

    status: str = "NOT_VERIFIED"

    elapsed: float = 0.0


# ============================================================
# TTL CACHE
# ============================================================

class TTLCache:
    def __init__(self, ttl: int = CACHE_TTL):
        self.ttl = ttl
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        now = time.time()

        with self._lock:
            item = self._data.get(key)

            if not item:
                return None

            timestamp, value = item

            if now - timestamp > self.ttl:
                self._data.pop(key, None)
                return None

            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# ============================================================
# URL UTILITIES
# ============================================================

def is_numeric(value: str) -> bool:
    return bool(value and re.fullmatch(r"\d{5,30}", value))


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    value = str(value)

    value = html_lib.unescape(value)

    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_url(url: str) -> str:
    url = clean_text(url)

    if not url:
        return ""

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url

    parsed = urlparse(url)

    scheme = parsed.scheme.lower() or "https"

    host = parsed.netloc.lower()

    if "@" in host:
        host = host.split("@", 1)[-1]

    if ":" in host:
        host = host.split(":", 1)[0]

    path = parsed.path or "/"

    path = re.sub(r"/{2,}", "/", path)

    return urlunparse(
        (
            scheme,
            host,
            path,
            "",
            parsed.query,
            "",
        )
    )


def clean_tracking_url(url: str) -> str:
    normalized = normalize_url(url)

    if not normalized:
        return ""

    parsed = urlparse(normalized)

    query = parse_qs(
        parsed.query,
        keep_blank_values=True,
    )

    kept = {}

    for key, values in query.items():
        lower = key.lower()

        if lower in TRACKING_PARAMS:
            continue

        if any(lower.startswith(prefix) for prefix in TRACKING_PREFIXES):
            continue

        kept[key] = values

    pairs = []

    for key, values in kept.items():
        for value in values:
            pairs.append((key, value))

    new_query = urlencode(
        pairs,
        doseq=True,
    )

    path = re.sub(
        r"/{2,}",
        "/",
        parsed.path or "/",
    )

    if path != "/":
        path = path.rstrip("/")

    return urlunparse(
        (
            "https",
            parsed.netloc.lower(),
            path,
            "",
            new_query,
            "",
        )
    )


def is_facebook_url(url: str) -> bool:
    try:
        host = urlparse(
            normalize_url(url)
        ).netloc.lower()

        return host in FACEBOOK_HOSTS

    except Exception:
        return False


# ============================================================
# URL EXTRACTION
# ============================================================

def extract_facebook_urls(text: str) -> List[str]:
    text = clean_text(text)

    if not text:
        return []

    pattern = re.compile(
        r"https?://"
        r"(?:"
        r"(?:www\.|m\.|mbasic\.|mobile\.|web\.|touch\.)?facebook\.com"
        r"|fb\.watch"
        r")"
        r"[^\s<>\"']+",
        re.I,
    )

    found = pattern.findall(text)

    # Handle cases where URLs are pasted without scheme.
    bare_pattern = re.compile(
        r"(?<!https?://)"
        r"(?:www\.|m\.|mbasic\.|mobile\.|web\.)?facebook\.com"
        r"/[^\s<>\"']+",
        re.I,
    )

    found.extend(
        "https://" + item
        for item in bare_pattern.findall(text)
        if not item.startswith(("http://", "https://"))
    )

    output = []

    seen = set()

    for url in found:
        url = url.rstrip(".,);]}")

        clean = clean_tracking_url(url)

        if not clean:
            continue

        key = clean.lower()

        if key in seen:
            continue

        seen.add(key)

        output.append(clean)

        if len(output) >= MAX_INPUT_URLS:
            break

    return output


# ============================================================
# URL CLASSIFIER
# ============================================================

class URLParser:

    @staticmethod
    def parse(url: str) -> URLShape:
        original = url

        normalized = normalize_url(url)
        clean = clean_tracking_url(normalized)

        parsed = urlparse(clean)

        host = parsed.netloc.lower()

        path = unquote(parsed.path or "/")

        segments = [
            unquote(x).strip()
            for x in path.split("/")
            if x.strip()
        ]

        shape = URLShape(
            original=original,
            normalized=normalized,
            clean=clean,
            host=host,
            path=path,
            segments=segments,
        )

        URLParser._parse_query(
            shape,
            parsed,
        )

        URLParser._parse_path(
            shape,
        )

        return shape

    @staticmethod
    def _parse_query(
        shape: URLShape,
        parsed,
    ) -> None:

        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
        )

        def first(*keys):
            for key in keys:
                values = query.get(key)

                if values:
                    return clean_text(values[0])

            return ""

        shape.post_id = first(
            "post_id",
            "postid",
        ) or shape.post_id

        shape.video_id = first(
            "video_id",
            "videoid",
            "v",
        ) or shape.video_id

        shape.photo_id = first(
            "photo_id",
            "photoid",
        ) or shape.photo_id

        shape.story_id = first(
            "story_fbid",
            "story_id",
        ) or shape.story_id

        shape.album_id = first(
            "album_id",
            "album",
        ) or shape.album_id

        profile_id = first(
            "id",
            "user_id",
            "profile_id",
        )

        if parsed.path.lower().endswith("profile.php"):
            if profile_id:
                shape.route_entity_id = profile_id
                shape.numeric_path_id = profile_id
                shape.kind = "profile"

    @staticmethod
    def _parse_path(
        shape: URLShape,
    ) -> None:

        segments = shape.segments

        if not segments:
            shape.kind = "homepage"
            return

        lower = [
            x.lower()
            for x in segments
        ]

        first = lower[0]

        # ----------------------------------------------------
        # GROUP
        # ----------------------------------------------------

        if first in {"groups", "group"}:

            shape.kind = "group"

            if len(segments) > 1:
                if is_numeric(segments[1]):
                    shape.group_id = segments[1]
                else:
                    shape.username = segments[1]

            if len(segments) > 2:
                for segment in segments[2:]:
                    if is_numeric(segment):
                        shape.group_id = segment

            return

        # ----------------------------------------------------
        # PAGE
        # ----------------------------------------------------

        if first == "pages":

            shape.kind = "page"

            for segment in segments[1:]:
                if is_numeric(segment):
                    shape.page_id = segment
                elif not shape.page_name:
                    shape.username = segment

            return

        # ----------------------------------------------------
        # WATCH
        # ----------------------------------------------------

        if first == "watch":

            shape.kind = "video"

            for segment in segments:
                if is_numeric(segment):
                    shape.video_id = segment

            return

        # ----------------------------------------------------
        # PROFILE / OBJECT ROUTES
        # ----------------------------------------------------

        if first not in RESERVED_ROUTES:

            shape.username = segments[0]

            if len(segments) == 1:

                if is_numeric(shape.username):
                    shape.route_entity_id = shape.username
                    shape.numeric_path_id = shape.username
                    shape.username = ""

                    shape.kind = "numeric_profile"

                else:
                    shape.kind = "profile"

                return

            second = lower[1]

            if second in OBJECT_ROUTES:

                shape.kind = "content"

                # Important:
                # numeric first segment is NOT automatically UID.
                if is_numeric(segments[0]):
                    shape.route_entity_id = segments[0]
                    shape.numeric_path_id = segments[0]
                    shape.username = ""

                object_id = ""

                for segment in segments[2:]:
                    if is_numeric(segment):
                        object_id = segment

                if second in {"reel", "reels"}:
                    shape.reel_id = object_id

                elif second in {"video", "videos"}:
                    shape.video_id = object_id

                elif second in {"post", "posts"}:
                    shape.post_id = object_id

                elif second in {"photo", "photos"}:
                    shape.photo_id = object_id

                elif second in {"story", "stories"}:
                    shape.story_id = object_id

                return

            if any(
                is_numeric(x)
                for x in segments[1:]
            ):
                shape.kind = "content"

                numbers = [
                    x
                    for x in segments[1:]
                    if is_numeric(x)
                ]

                if numbers:
                    shape.post_id = numbers[-1]

                return

        # ----------------------------------------------------
        # DIRECT OBJECT ROUTES
        # ----------------------------------------------------

        if first in {"reel", "reels"}:
            shape.kind = "reel"

            for segment in segments[1:]:
                if is_numeric(segment):
                    shape.reel_id = segment

            return

        if first in {"video", "videos"}:
            shape.kind = "video"

            for segment in segments[1:]:
                if is_numeric(segment):
                    shape.video_id = segment

            return

        if first in {"photo", "photos"}:
            shape.kind = "photo"

            for segment in segments[1:]:
                if is_numeric(segment):
                    shape.photo_id = segment

            return

        if first in {"post", "posts"}:
            shape.kind = "post"

            for segment in segments[1:]:
                if is_numeric(segment):
                    shape.post_id = segment

            return

        if first in {"story", "stories"}:
            shape.kind = "story"

            for segment in segments[1:]:
                if is_numeric(segment):
                    shape.story_id = segment

            return

        shape.kind = "unknown"


# ============================================================
# HTML PARSER
# ============================================================

class FacebookHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.meta: Dict[str, str] = {}

        self.links: List[str] = []

        self.scripts: List[Tuple[str, str]] = []

        self.title = ""

        self._inside_title = False

        self._inside_script = False

        self._script_type = ""

        self._script_buffer: List[str] = []

        self._title_buffer: List[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs,
    ):
        attrs_dict = dict(attrs)

        tag = tag.lower()

        if tag == "meta":

            key = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or attrs_dict.get("itemprop")
                or ""
            ).lower()

            content = attrs_dict.get(
                "content",
                "",
            )

            if key and content:
                self.meta[key] = clean_text(
                    content
                )

        elif tag == "link":

            href = attrs_dict.get(
                "href",
                "",
            )

            if href:
                self.links.append(
                    href
                )

        elif tag == "a":

            href = attrs_dict.get(
                "href",
                "",
            )

            if href:
                self.links.append(
                    href
                )

        elif tag == "title":

            self._inside_title = True
            self._title_buffer = []

        elif tag == "script":

            self._inside_script = True
            self._script_type = (
                attrs_dict.get(
                    "type",
                    "",
                ).lower()
            )

            self._script_buffer = []

    def handle_endtag(
        self,
        tag: str,
    ):

        tag = tag.lower()

        if tag == "title":

            self._inside_title = False

            self.title = clean_text(
                "".join(
                    self._title_buffer
                )
            )

        elif tag == "script":

            if self._inside_script:

                content = "".join(
                    self._script_buffer
                )

                if len(content) <= MAX_SCRIPT_BYTES:

                    self.scripts.append(
                        (
                            self._script_type,
                            content,
                        )
                    )

            self._inside_script = False

            self._script_type = ""

            self._script_buffer = []

    def handle_data(
        self,
        data: str,
    ):

        if self._inside_title:
            self._title_buffer.append(data)

        if self._inside_script:
            self._script_buffer.append(data)


# ============================================================
# JSON HELPERS
# ============================================================

def safe_json_loads(
    text: str,
) -> Optional[Any]:

    text = text.strip()

    if not text:
        return None

    try:
        return json.loads(text)

    except Exception:
        pass

    # Strip common JS wrappers.
    candidates = []

    if text.startswith("<!--"):
        candidates.append(
            text[4:]
            .split("-->", 1)[0]
        )

    if text.startswith(
        "for (;;);"
    ):
        candidates.append(
            text[len("for (;;);"):]
        )

    candidates.append(text)

    for candidate in candidates:

        try:
            return json.loads(
                candidate.strip()
            )
        except Exception:
            continue

    return None


def walk_json(
    value: Any,
    path: str = "$",
    depth: int = 0,
) -> Iterable[Tuple[str, str, Any, str]]:

    if depth > MAX_JSON_DEPTH:
        return

    if isinstance(value, dict):

        for key, child in value.items():

            key_str = str(key)

            child_path = (
                f"{path}.{key_str}"
            )

            yield (
                key_str,
                child_path,
                child,
                path,
            )

            yield from walk_json(
                child,
                child_path,
                depth + 1,
            )

    elif isinstance(value, list):

        for index, child in enumerate(
            value
        ):

            child_path = (
                f"{path}[{index}]"
            )

            yield from walk_json(
                child,
                child_path,
                depth + 1,
            )


# ============================================================
# JSON EXTRACTION
# ============================================================

def extract_json_objects(
    scripts: List[Tuple[str, str]],
) -> Tuple[List[Any], List[Any]]:

    json_objects = []
    jsonld_objects = []

    for script_type, content in scripts:

        if not content:
            continue

        content = content.strip()

        if not content:
            continue

        parsed = safe_json_loads(
            content
        )

        if parsed is not None:

            if (
                "ld+json"
                in script_type
            ):
                jsonld_objects.append(
                    parsed
                )

            else:
                json_objects.append(
                    parsed
                )

            continue

        # Search JSON-looking fragments.
        # Intentionally conservative.
        for match in re.finditer(
            r'(?<![A-Za-z0-9_])'
            r'[\{\[]',
            content,
        ):

            start = match.start()

            fragment = extract_balanced_json(
                content,
                start,
            )

            if not fragment:
                continue

            parsed = safe_json_loads(
                fragment
            )

            if parsed is None:
                continue

            json_objects.append(
                parsed
            )

            if len(json_objects) >= 100:
                break

    return (
        json_objects,
        jsonld_objects,
    )


def extract_balanced_json(
    text: str,
    start: int,
) -> str:

    if start >= len(text):
        return ""

    opener = text[start]

    if opener not in "[{":
        return ""

    closer = (
        "}"
        if opener == "{"
        else "]"
    )

    depth = 0

    in_string = False
    escape = False

    for index in range(
        start,
        min(
            len(text),
            start + MAX_SCRIPT_BYTES,
        ),
    ):

        char = text[index]

        if in_string:

            if escape:
                escape = False

            elif char == "\\":
                escape = True

            elif char == '"':
                in_string = False

            continue

        if char == '"':
            in_string = True
            continue

        if char == opener:
            depth += 1

        elif char == closer:

            depth -= 1

            if depth == 0:
                return text[
                    start:index + 1
                ]

    return ""


# ============================================================
# META UTILITIES
# ============================================================

def get_meta(
    snapshot: PageSnapshot,
    *keys: str,
) -> str:

    for key in keys:

        value = snapshot.meta.get(
            key.lower()
        )

        if value:
            return clean_text(value)

    return ""


def normalize_title(
    title: str,
) -> str:

    title = clean_text(title)

    if not title:
        return ""

    patterns = [
        r"\s*\|\s*Facebook$",
        r"\s*-\s*Facebook$",
        r"\s+on Facebook$",
        r"\s*·\s*Facebook$",
    ]

    for pattern in patterns:
        title = re.sub(
            pattern,
            "",
            title,
            flags=re.I,
        )

    title = re.sub(
        r"\s+(?:'s|’s)\s+(?:reel|video|photo|post)$",
        "",
        title,
        flags=re.I,
    )

    title = re.sub(
        r"^(?:Facebook\s*[-|:]\s*)",
        "",
        title,
        flags=re.I,
    )

    if title.lower() in GENERIC_TITLES:
        return ""

    return title.strip()


def derive_content_title(
    snapshot: PageSnapshot,
) -> str:

    candidates = [
        snapshot.meta.get("og:title", ""),
        snapshot.meta.get("twitter:title", ""),
        snapshot.title,
    ]

    for title in candidates:

        title = normalize_title(title)

        if title:
            return title

    return ""


# ============================================================
# EVIDENCE ENGINE
# ============================================================

class EvidenceEngine:

    def __init__(
        self,
        url: str,
    ):
        self.url = url

        self.items: List[Evidence] = []

        self._seen: Set[
            Tuple[str, str, str, str]
        ] = set()

        self._value_count: Dict[
            str,
            int,
        ] = {}

    def add(
        self,
        value: Any,
        key: str,
        path: str,
        source: str,
        role: str,
        weight: float,
        context: str = "",
        neighbor: str = "",
        independent_group: str = "",
    ) -> None:

        value = clean_text(value)

        if not value:
            return

        if not is_numeric(value):
            return

        identity = (
            value,
            key,
            source,
            path,
        )

        if identity in self._seen:
            return

        count = self._value_count.get(
            value,
            0,
        )

        if count >= MAX_EVIDENCE_PER_VALUE:
            return

        self._seen.add(identity)

        self._value_count[value] = (
            count + 1
        )

        confidence = min(
            1.0,
            max(
                0.0,
                weight / 140.0,
            ),
        )

        self.items.append(
            Evidence(
                value=value,
                key=key,
                path=path,
                source=source,
                url=self.url,
                role=role,
                weight=weight,
                context=clean_text(context)[:500],
                neighbor=clean_text(neighbor)[:250],
                independent_group=(
                    independent_group
                    or source
                ),
                confidence=confidence,
            )
        )

    def scan_json(
        self,
        obj: Any,
        source: str,
        path: str = "$",
    ) -> None:

        for key, child_path, value, parent_path in walk_json(
            obj,
            path,
        ):

            key_lower = key.lower()

            if (
                key_lower in USER_FIELD_WEIGHTS
                or key_lower.replace("-", "_")
                in USER_FIELD_WEIGHTS
            ):

                normalized_key = key_lower.replace(
                    "-",
                    "_",
                )

                weight = USER_FIELD_WEIGHTS.get(
                    key,
                    USER_FIELD_WEIGHTS.get(
                        normalized_key,
                        70,
                    ),
                )

                if isinstance(
                    value,
                    (str, int),
                ):

                    candidate = str(
                        value
                    )

                    if is_numeric(
                        candidate
                    ):

                        self.add(
                            candidate,
                            key,
                            child_path,
                            source,
                            "USER_CANDIDATE",
                            weight,
                            context=(
                                f"{key}={candidate}"
                            ),
                            independent_group=source,
                        )

                elif isinstance(
                    value,
                    dict,
                ):

                    self._scan_identity_dict(
                        value,
                        child_path,
                        source,
                    )

            if (
                key_lower in OBJECT_FIELD_WEIGHTS
                or key_lower.replace("-", "_")
                in OBJECT_FIELD_WEIGHTS
            ):

                normalized_key = key_lower.replace(
                    "-",
                    "_",
                )

                weight = OBJECT_FIELD_WEIGHTS.get(
                    key,
                    OBJECT_FIELD_WEIGHTS.get(
                        normalized_key,
                        70,
                    ),
                )

                if isinstance(
                    value,
                    (str, int),
                ):

                    candidate = str(
                        value
                    )

                    if is_numeric(
                        candidate
                    ):

                        role = (
                            "GROUP_ID"
                            if "group"
                            in normalized_key
                            else
                            "PAGE_ID"
                            if "page"
                            in normalized_key
                            else
                            "OBJECT_ID"
                        )

                        self.add(
                            candidate,
                            key,
                            child_path,
                            source,
                            role,
                            weight,
                            context=(
                                f"{key}={candidate}"
                            ),
                            independent_group=source,
                        )

    def _scan_identity_dict(
        self,
        value: Dict[str, Any],
        path: str,
        source: str,
    ) -> None:

        for key, child in value.items():

            lower = str(key).lower()

            if (
                lower in USER_FIELD_WEIGHTS
                and isinstance(
                    child,
                    (str, int),
                )
            ):

                candidate = str(child)

                if is_numeric(candidate):

                    self.add(
                        candidate,
                        str(key),
                        f"{path}.{key}",
                        source,
                        "USER_CANDIDATE",
                        USER_FIELD_WEIGHTS.get(
                            lower,
                            80,
                        ),
                        context=(
                            f"{key}={candidate}"
                        ),
                        independent_group=source,
                    )

    def scan_semantic_html(
        self,
        html_text: str,
        source: str = "html_semantic",
    ) -> None:

        patterns = [
            (
                r'"(?:user_id|userID|profile_id|profileID)"'
                r'\s*:\s*"?(?P<id>\d{5,30})',
                "profile_id",
                125,
            ),
            (
                r'"(?:creator_id|creatorID)"'
                r'\s*:\s*"?(?P<id>\d{5,30})',
                "creator_id",
                112,
            ),
            (
                r'"(?:owner_id|ownerID)"'
                r'\s*:\s*"?(?P<id>\d{5,30})',
                "owner_id",
                115,
            ),
            (
                r'"(?:author_id|authorID)"'
                r'\s*:\s*"?(?P<id>\d{5,30})',
                "author_id",
                112,
            ),
            (
                r'"(?:publisher_id|publisherID)"'
                r'\s*:\s*"?(?P<id>\d{5,30})',
                "publisher_id",
                105,
            ),
            (
                r'"(?:from_id|fromID)"'
                r'\s*:\s*"?(?P<id>\d{5,30})',
                "from_id",
                105,
            ),
        ]

        for pattern, key, weight in patterns:

            for match in re.finditer(
                pattern,
                html_text,
                re.I,
            ):

                candidate = match.group(
                    "id"
                )

                context_start = max(
                    0,
                    match.start() - 250,
                )

                context_end = min(
                    len(html_text),
                    match.end() + 250,
                )

                context = html_text[
                    context_start:context_end
                ]

                self.add(
                    candidate,
                    key,
                    "html",
                    source,
                    "USER_CANDIDATE",
                    weight,
                    context=context,
                    independent_group=source,
                )


# ============================================================
# FETCH ENGINE
# ============================================================

class FetchEngine:

    def __init__(self):
        self.cache = TTLCache()

        self._semaphore = asyncio.Semaphore(
            MAX_CONCURRENT_FETCHES
        )

    def _headers(
        self,
        url: str,
        referer: str = "",
    ) -> Dict[str, str]:

        agent = USER_AGENT_PROFILES[
            int(time.time() * 1000)
            % len(USER_AGENT_PROFILES)
        ]

        headers = {
            "User-Agent": agent,

            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,image/webp,"
                "image/apng,*/*;"
                "q=0.8"
            ),

            "Accept-Language": (
                "vi-VN,vi;q=0.9,"
                "en-US;q=0.8,en;q=0.7"
            ),

            "Accept-Encoding": (
                "gzip, deflate, br"
            ),

            "Cache-Control": "no-cache",
            "Pragma": "no-cache",

            "Upgrade-Insecure-Requests": "1",

            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": (
                "same-origin"
                if referer
                else "none"
            ),
            "Sec-Fetch-User": "?1",

            "DNT": "1",

            "Connection": "keep-alive",
        }

        if referer:
            headers["Referer"] = referer

        return headers

    def _fetch_sync(
        self,
        url: str,
        referer: str = "",
    ) -> PageSnapshot:

        cache_key = clean_tracking_url(
            url
        )

        cached = self.cache.get(
            cache_key
        )

        if cached is not None:
            return cached

        started = time.perf_counter()

        snapshot = PageSnapshot(
            requested_url=url
        )

        session = requests.Session()

        headers = self._headers(
            url,
            referer,
        )

        try:

            response = session.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                stream=True,
            )

            snapshot.status_code = (
                response.status_code
            )

            snapshot.final_url = (
                clean_tracking_url(
                    response.url
                )
            )

            snapshot.content_type = (
                response.headers.get(
                    "Content-Type",
                    "",
                )
            )

            if (
                response.status_code
                >= 400
            ):
                snapshot.blocked = True
                return snapshot

            content_length = response.headers.get(
                "Content-Length"
            )

            if (
                content_length
                and content_length.isdigit()
                and int(content_length)
                > MAX_HTML_BYTES
            ):
                snapshot.blocked = True
                return snapshot

            chunks = []

            total = 0

            for chunk in response.iter_content(
                chunk_size=64 * 1024
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

            snapshot.raw_html = text

            parser = FacebookHTMLParser()

            try:
                parser.feed(text)

            except Exception:
                pass

            snapshot.meta = parser.meta

            snapshot.links = parser.links[
                :MAX_DISCOVERED_URLS
            ]

            snapshot.scripts = parser.scripts

            snapshot.title = parser.title

            (
                json_objects,
                jsonld_objects,
            ) = extract_json_objects(
                parser.scripts
            )

            snapshot.json_objects = (
                json_objects
            )

            snapshot.jsonld_objects = (
                jsonld_objects
            )

            snapshot.canonical_url = (
                self._canonical(
                    snapshot,
                    url,
                )
            )

            snapshot.blocked = (
                self._looks_blocked(
                    snapshot
                )
            )

            snapshot.fetched = True

        except requests.RequestException as exc:

            log.debug(
                "Facebook fetch failed: %s",
                exc,
            )

        except Exception as exc:

            log.debug(
                "Facebook parser failure: %s",
                exc,
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

        self.cache.set(
            cache_key,
            snapshot,
        )

        return snapshot

    @staticmethod
    def _canonical(
        snapshot: PageSnapshot,
        fallback: str,
    ) -> str:

        canonical = (
            snapshot.meta.get(
                "og:url",
                ""
            )
            or snapshot.meta.get(
                "canonical",
                ""
            )
        )

        if canonical:
            return clean_tracking_url(
                canonical
            )

        return clean_tracking_url(
            snapshot.final_url
            or fallback
        )

    @staticmethod
    def _looks_blocked(
        snapshot: PageSnapshot,
    ) -> bool:

        text = (
            snapshot.raw_html
            or ""
        ).lower()

        title = (
            snapshot.title
            or ""
        ).lower()

        indicators = (
            "checkpoint",
            "temporarily blocked",
            "log into facebook",
            "login to facebook",
            "content isn't available",
            "this content isn't available",
            "you must log in",
        )

        hits = sum(
            1
            for item in indicators
            if item in text
            or item in title
        )

        return hits >= 2

    async def fetch(
        self,
        url: str,
        referer: str = "",
    ) -> PageSnapshot:

        async with self._semaphore:

            return await asyncio.to_thread(
                self._fetch_sync,
                url,
                referer,
            )


# ============================================================
# PROFILE URL DISCOVERY
# ============================================================

def profile_base_from_shape(
    shape: URLShape,
) -> str:

    if shape.kind == "profile":
        if shape.username:
            return (
                "https://www.facebook.com/"
                + quote(
                    shape.username,
                    safe="._-",
                )
            )

    if (
        shape.kind == "content"
        and shape.username
    ):

        return (
            "https://www.facebook.com/"
            + quote(
                shape.username,
                safe="._-",
            )
        )

    return ""


def profile_urls_from_snapshot(
    snapshot: PageSnapshot,
) -> List[str]:

    output = []

    seen = set()

    def add(url: str):

        if not url:
            return

        url = clean_tracking_url(
            url
        )

        if not is_facebook_url(
            url
        ):
            return

        parsed = URLParser.parse(
            url
        )

        if parsed.kind not in {
            "profile",
            "numeric_profile",
        }:
            return

        key = url.lower()

        if key in seen:
            return

        seen.add(key)

        output.append(url)

    canonical = snapshot.canonical_url

    if canonical:
        add(canonical)

    for link in snapshot.links:

        absolute = urljoin(
            snapshot.final_url
            or snapshot.requested_url,
            link,
        )

        add(absolute)

        if len(output) >= MAX_DISCOVERED_URLS:
            break

    return output[:MAX_DISCOVERED_URLS]


# ============================================================
# PROFILE EXTRACTION
# ============================================================

class ProfileResolver:

    def __init__(
        self,
        fetcher: FetchEngine,
    ):
        self.fetcher = fetcher

    async def verify(
        self,
        url: str,
        expected_uid: str = "",
    ) -> ProfileInfo:

        info = ProfileInfo(
            profile_url=clean_tracking_url(
                url
            )
        )

        snapshot = await self.fetcher.fetch(
            url
        )

        if not snapshot.fetched:
            return info

        info.profile_url = (
            snapshot.canonical_url
            or snapshot.final_url
            or url
        )

        info.entity_type = (
            self._classify_profile(
                snapshot
            )
        )

        info.name = self._extract_name(
            snapshot
        )

        info.username = (
            self._extract_username(
                snapshot,
                info.profile_url,
            )
        )

        info.avatar_url = (
            self._extract_avatar(
                snapshot
            )
        )

        info.bio = (
            self._extract_bio(
                snapshot
            )
        )

        engine = EvidenceEngine(
            info.profile_url
        )

        for obj in snapshot.json_objects:

            engine.scan_json(
                obj,
                "profile_embedded_json",
            )

        for obj in snapshot.jsonld_objects:

            engine.scan_json(
                obj,
                "profile_jsonld",
            )

        engine.scan_semantic_html(
            snapshot.raw_html,
            "profile_html_semantic",
        )

        info.evidence = engine.items

        info.explicit_ids = {
            item.value
            for item in engine.items
            if item.role
            == "USER_CANDIDATE"
        }

        return info

    @staticmethod
    def _extract_name(
        snapshot: PageSnapshot,
    ) -> str:

        candidates = [
            snapshot.meta.get(
                "profile:name",
                "",
            ),
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

            value = normalize_title(
                value
            )

            if value:
                return value

        for obj in (
            snapshot.jsonld_objects
            + snapshot.json_objects
        ):

            result = find_json_string(
                obj,
                NAME_KEYS,
            )

            if result:
                result = normalize_title(
                    result
                )

                if result:
                    return result

        return ""

    @staticmethod
    def _extract_username(
        snapshot: PageSnapshot,
        profile_url: str,
    ) -> str:

        for obj in (
            snapshot.jsonld_objects
            + snapshot.json_objects
        ):

            result = find_json_string(
                obj,
                USERNAME_KEYS,
            )

            if result:

                result = clean_username(
                    result
                )

                if result:
                    return result

        shape = URLParser.parse(
            profile_url
        )

        if shape.username:
            return clean_username(
                shape.username
            )

        return ""

    @staticmethod
    def _extract_avatar(
        snapshot: PageSnapshot,
    ) -> str:

        # Only profile snapshot images
        # become avatar.
        return (
            snapshot.meta.get(
                "og:image",
                "",
            )
            or snapshot.meta.get(
                "twitter:image",
                "",
            )
        )

    @staticmethod
    def _extract_bio(
        snapshot: PageSnapshot,
    ) -> str:

        bio = (
            snapshot.meta.get(
                "og:description",
                "",
            )
            or snapshot.meta.get(
                "description",
                "",
            )
        )

        if bio:
            return clean_text(
                bio
            )[:1000]

        for obj in (
            snapshot.json_objects
            + snapshot.jsonld_objects
        ):

            value = find_json_string(
                obj,
                BIO_KEYS,
            )

            if value:
                return clean_text(
                    value
                )[:1000]

        return ""

    @staticmethod
    def _classify_profile(
        snapshot: PageSnapshot,
    ) -> str:

        text = (
            snapshot.raw_html
            or ""
        ).lower()

        if "/groups/" in text:
            return "GROUP"

        if (
            '"page"' in text
            or "page_id" in text
        ):
            return "PAGE"

        return "USER"


# ============================================================
# JSON VALUE FINDERS
# ============================================================

def find_json_string(
    obj: Any,
    keys: Set[str],
    depth: int = 0,
) -> str:

    if depth > MAX_JSON_DEPTH:
        return ""

    if isinstance(obj, dict):

        for key, value in obj.items():

            lower = str(key).lower()

            if lower in {
                x.lower()
                for x in keys
            }:

                if isinstance(
                    value,
                    (str, int),
                ):

                    text = clean_text(
                        value
                    )

                    if text:
                        return text

            result = find_json_string(
                value,
                keys,
                depth + 1,
            )

            if result:
                return result

    elif isinstance(obj, list):

        for value in obj:

            result = find_json_string(
                value,
                keys,
                depth + 1,
            )

            if result:
                return result

    return ""


def clean_username(
    username: str,
) -> str:

    username = clean_text(
        username
    )

    username = username.lstrip("@")

    username = username.rstrip("/")

    if "/" in username:
        username = username.split(
            "/",
            1,
        )[0]

    return username


# ============================================================
# ENTITY CLASSIFIER
# ============================================================

def classify_entity(
    shape: URLShape,
    profile: Optional[ProfileInfo],
) -> str:

    if profile:

        if profile.entity_type in {
            "USER",
            "PAGE",
            "GROUP",
        }:
            return profile.entity_type

    if shape.kind in {
        "group",
    }:
        return "GROUP"

    if shape.kind == "page":
        return "PAGE"

    if shape.kind in {
        "profile",
        "numeric_profile",
    }:
        return "USER"

    return "UNKNOWN"


# ============================================================
# OBJECT EXTRACTION
# ============================================================

def object_ids_from_shape(
    shape: URLShape,
) -> Dict[str, str]:

    return {
        "post_id": shape.post_id,
        "video_id": shape.video_id,
        "reel_id": shape.reel_id,
        "photo_id": shape.photo_id,
        "story_id": shape.story_id,
        "album_id": shape.album_id,
    }


def extract_object_ids(
    snapshot: PageSnapshot,
) -> Dict[str, str]:

    found = {
        "post_id": "",
        "video_id": "",
        "reel_id": "",
        "photo_id": "",
        "story_id": "",
        "album_id": "",
    }

    for obj in (
        snapshot.json_objects
        + snapshot.jsonld_objects
    ):

        for key, path, value, parent in walk_json(
            obj
        ):

            lower = str(key).lower()

            if not isinstance(
                value,
                (str, int),
            ):
                continue

            value = str(value)

            if not is_numeric(value):
                continue

            if (
                lower in {
                    "post_id",
                    "postid",
                    "postid",
                }
                and not found["post_id"]
            ):
                found["post_id"] = value

            elif (
                lower in {
                    "video_id",
                    "videoid",
                }
                and not found["video_id"]
            ):
                found["video_id"] = value

            elif (
                lower in {
                    "reel_id",
                    "reelid",
                }
                and not found["reel_id"]
            ):
                found["reel_id"] = value

            elif (
                lower in {
                    "photo_id",
                    "photoid",
                }
                and not found["photo_id"]
            ):
                found["photo_id"] = value

            elif (
                lower in {
                    "story_id",
                    "story_fbid",
                }
                and not found["story_id"]
            ):
                found["story_id"] = value

            elif (
                lower in {
                    "album_id",
                    "albumid",
                }
                and not found["album_id"]
            ):
                found["album_id"] = value

    return found


# ============================================================
# CONTENT METADATA
# ============================================================

def extract_content_metadata(
    snapshot: PageSnapshot,
) -> Tuple[str, str, str]:

    title = derive_content_title(
        snapshot
    )

    description = (
        snapshot.meta.get(
            "og:description",
            "",
        )
        or snapshot.meta.get(
            "twitter:description",
            "",
        )
    )

    thumbnail = (
        snapshot.meta.get(
            "og:image",
            "",
        )
        or snapshot.meta.get(
            "twitter:image",
            "",
        )
    )

    return (
        title,
        clean_text(description),
        thumbnail,
    )


# ============================================================
# PROFILE MATCHING
# ============================================================

def profile_matches_candidate(
    profile: ProfileInfo,
    candidate: Candidate,
) -> bool:

    if candidate.uid in profile.explicit_ids:
        return True

    for evidence in profile.evidence:

        if (
            evidence.role
            == "USER_CANDIDATE"
            and evidence.value
            == candidate.uid
        ):
            return True

    return False


# ============================================================
# CANDIDATE ENGINE
# ============================================================

class IdentityVerifier:

    def __init__(
        self,
        fetcher: FetchEngine,
        profile_resolver: ProfileResolver,
    ):

        self.fetcher = fetcher

        self.profile_resolver = (
            profile_resolver
        )

    def build_candidates(
        self,
        evidence: List[Evidence],
        object_ids: Set[str],
        route_entity_id: str = "",
    ) -> List[Candidate]:

        grouped: Dict[
            str,
            Candidate,
        ] = {}

        for item in evidence:

            if item.role != "USER_CANDIDATE":
                continue

            value = item.value

            # Never let known object IDs
            # become user UID candidates.
            if value in object_ids:
                continue

            candidate = grouped.get(
                value
            )

            if candidate is None:

                candidate = Candidate(
                    uid=value
                )

                grouped[value] = candidate

            candidate.evidence.append(
                item
            )

            candidate.independent_sources.add(
                item.source
            )

            candidate.independent_groups.add(
                item.independent_group
                or item.source
            )

        candidates = []

        for candidate in grouped.values():

            self._score_candidate(
                candidate,
                route_entity_id,
            )

            candidates.append(
                candidate
            )

        candidates.sort(
            key=lambda x: x.score,
            reverse=True,
        )

        return candidates

    @staticmethod
    def _score_candidate(
        candidate: Candidate,
        route_entity_id: str,
    ) -> None:

        if not candidate.evidence:
            return

        best_by_source: Dict[
            str,
            float,
        ] = {}

        for evidence in candidate.evidence:

            source = (
                evidence.source
            )

            best_by_source[source] = max(
                best_by_source.get(
                    source,
                    0,
                ),
                evidence.weight,
            )

        score = 0.0

        # Source-diverse contribution.
        sorted_weights = sorted(
            best_by_source.values(),
            reverse=True,
        )

        for index, weight in enumerate(
            sorted_weights
        ):

            if index == 0:
                multiplier = 1.0

            elif index == 1:
                multiplier = 0.75

            elif index == 2:
                multiplier = 0.55

            else:
                multiplier = 0.25

            score += (
                weight
                * multiplier
            )

        # Independent source bonus.
        source_count = len(
            candidate.independent_sources
        )

        if source_count >= 2:
            score += 25

        if source_count >= 3:
            score += 15

        if source_count >= 4:
            score += 10

        # Numeric route is weak by itself.
        if (
            route_entity_id
            and candidate.uid
            == route_entity_id
        ):
            score -= 35

        candidate.score = min(
            250,
            max(
                0,
                score,
            ),
        )

    async def verify(
        self,
        candidates: List[Candidate],
        profile_urls: List[str],
        username: str = "",
        canonical_url: str = "",
        entity_type: str = "UNKNOWN",
    ) -> Tuple[
        Optional[Candidate],
        Optional[ProfileInfo],
    ]:

        if entity_type in {
            "PAGE",
            "GROUP",
        }:
            return None, None

        profile_info = None

        # Try only strongest candidate(s).
        for candidate in candidates[:2]:

            # Direct profile verification.
            for profile_url in profile_urls[
                :MAX_PROFILE_CHECKS
            ]:

                info = (
                    await self.profile_resolver.verify(
                        profile_url,
                        candidate.uid,
                    )
                )

                if not info.profile_url:
                    continue

                if info.entity_type in {
                    "PAGE",
                    "GROUP",
                }:
                    continue

                if profile_matches_candidate(
                    info,
                    candidate,
                ):

                    candidate.profile_match = True

                    candidate.score += 55

                    profile_info = info

                    return (
                        candidate,
                        info,
                    )

                # Username/canonical correlation
                # without explicit UID is useful,
                # but NOT sufficient alone.
                if (
                    username
                    and info.username
                    and clean_username(
                        info.username
                    ).lower()
                    == clean_username(
                        username
                    ).lower()
                ):
                    candidate.username_match = True
                    candidate.score += 12

                if (
                    canonical_url
                    and info.profile_url
                ):

                    a = normalize_profile_key(
                        canonical_url
                    )

                    b = normalize_profile_key(
                        info.profile_url
                    )

                    if (
                        a
                        and b
                        and a == b
                    ):
                        candidate.canonical_match = True
                        candidate.score += 15

        # Strong multi-source evidence can verify
        # even if profile endpoint doesn't expose
        # the numeric ID.
        for candidate in candidates:

            if (
                len(
                    candidate.independent_sources
                )
                >= 3
                and candidate.score >= 150
                and not candidate.conflicts
            ):

                return (
                    candidate,
                    profile_info,
                )

        return None, profile_info


def normalize_profile_key(
    url: str,
) -> str:

    shape = URLParser.parse(
        url
    )

    if shape.username:
        return shape.username.lower()

    return clean_tracking_url(
        url
    ).rstrip("/").lower()


# ============================================================
# CONFLICT DETECTOR
# ============================================================

class ConflictDetector:

    @staticmethod
    def detect(
        candidates: List[Candidate],
        object_ids: Set[str],
        entity_type: str,
    ) -> None:

        for candidate in candidates:

            if candidate.uid in object_ids:

                candidate.conflicts.append(
                    "candidate_is_object_id"
                )

            if entity_type in {
                "PAGE",
                "GROUP",
            }:

                candidate.conflicts.append(
                    "non_user_entity"
                )

        strong = [
            c
            for c in candidates
            if c.score >= 100
        ]

        if len(strong) >= 2:

            top = strong[0]

            for other in strong[1:]:

                if (
                    top.score
                    - other.score
                    < 15
                ):

                    top.conflicts.append(
                        "competing_identity"
                    )

                    other.conflicts.append(
                        "competing_identity"
                    )


# ============================================================
# RESULT BUILDER
# ============================================================

class ResultBuilder:

    @staticmethod
    def build(
        shape: URLShape,
        snapshot: PageSnapshot,
        profile: Optional[ProfileInfo],
        candidate: Optional[Candidate],
        title: str,
        description: str,
        thumbnail: str,
        started: float,
        evidence: List[Evidence],
    ) -> ResolveResult:

        result = ResolveResult(
            input_url=shape.clean
            or shape.normalized
            or shape.original
        )

        result.elapsed = (
            time.perf_counter()
            - started
        )

        result.canonical_url = (
            snapshot.canonical_url
            or snapshot.final_url
            or shape.clean
        )

        result.content_url = (
            clean_tracking_url(
                snapshot.final_url
                or shape.clean
            )
        )

        result.entity_type = classify_entity(
            shape,
            profile,
        )

        result.publisher_type = (
            result.entity_type
        )

        if profile:

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

        if not result.profile_url:

            result.profile_url = (
                profile_base_from_shape(
                    shape
                )
            )

        result.title = title

        result.description = (
            description
        )

        result.thumbnail_url = (
            thumbnail
        )

        objects = object_ids_from_shape(
            shape
        )

        # Snapshot IDs have priority when
        # they are actually discovered.
        snapshot_objects = (
            extract_object_ids(
                snapshot
            )
        )

        for key, value in snapshot_objects.items():

            if value and not objects.get(
                key
            ):
                objects[key] = value

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

        if (
            candidate
            and not candidate.conflicts
            and result.entity_type
            not in {"PAGE", "GROUP"}
        ):

            if (
                candidate.profile_match
                or (
                    len(
                        candidate.independent_sources
                    )
                    >= 3
                    and candidate.score >= 150
                )
            ):

                result.uid = candidate.uid

                result.verified = True

                result.status = (
                    "VERIFIED"
                )

                result.confidence = (
                    ResultBuilder.confidence(
                        candidate
                    )
                )

        result.evidence_sources = len({
            x.independent_group
            or x.source
            for x in evidence
            if (
                not candidate
                or x.value
                == candidate.uid
            )
        })

        result.signals = (
            ResultBuilder.signals(
                candidate,
                profile,
            )
        )

        if not result.verified:

            result.status = (
                "NOT_VERIFIED"
            )

            if (
                shape.kind
                in {
                    "profile",
                    "numeric_profile",
                }
            ):
                result.notes.append(
                    "Facebook không cung cấp đủ "
                    "bằng chứng công khai để liên kết "
                    "profile với numeric UID."
                )

            elif snapshot.blocked:

                result.notes.append(
                    "Facebook giới hạn dữ liệu công khai "
                    "trong lần kiểm tra này."
                )

            else:

                result.notes.append(
                    "Chưa có đủ bằng chứng độc lập "
                    "để xác minh UID."
                )

        return result

    @staticmethod
    def confidence(
        candidate: Candidate,
    ) -> float:

        score = candidate.score

        if candidate.profile_match:
            score += 30

        if candidate.username_match:
            score += 5

        if candidate.canonical_match:
            score += 5

        confidence = (
            70
            + score * 0.12
        )

        if len(
            candidate.independent_sources
        ) >= 3:
            confidence += 4

        if len(
            candidate.independent_sources
        ) >= 4:
            confidence += 3

        confidence = min(
            99.7,
            confidence,
        )

        return round(
            confidence,
            1,
        )

    @staticmethod
    def signals(
        candidate: Optional[Candidate],
        profile: Optional[ProfileInfo],
    ) -> List[str]:

        signals = []

        if not candidate:
            return signals

        keys = set()

        for evidence in candidate.evidence:

            key = evidence.key.lower()

            if key in {
                "creator_id",
                "creatorid",
                "creatorid",
            }:
                keys.add(
                    "creator_id"
                )

            elif key in {
                "profile_id",
                "profileid",
                "profileid",
                "user_id",
                "userid",
                "userid",
            }:
                keys.add(
                    "profile/user_id"
                )

            elif key in {
                "owner_id",
                "ownerid",
                "ownerid",
            }:
                keys.add(
                    "owner_id"
                )

            elif key in {
                "author_id",
                "authorid",
                "authorid",
            }:
                keys.add(
                    "author_id"
                )

        for key in sorted(keys):
            signals.append(
                f"{key} → UID"
            )

        if candidate.profile_match:
            signals.append(
                "profile → UID khớp"
            )

        if candidate.username_match:
            signals.append(
                "username → profile khớp"
            )

        if candidate.canonical_match:
            signals.append(
                "canonical → profile khớp"
            )

        if len(
            candidate.independent_sources
        ) >= 2:
            signals.append(
                f"{len(candidate.independent_sources)} nguồn độc lập"
            )

        return signals[:5]


# ============================================================
# MAIN RESOLVER
# ============================================================

class FacebookResolver:

    def __init__(self):

        self.fetcher = FetchEngine()

        self.profile_resolver = (
            ProfileResolver(
                self.fetcher
            )
        )

        self.identity = (
            IdentityVerifier(
                self.fetcher,
                self.profile_resolver,
            )
        )

    async def resolve(
        self,
        url: str,
    ) -> ResolveResult:

        started = time.perf_counter()

        shape = URLParser.parse(
            url
        )

        snapshot = await self.fetcher.fetch(
            shape.clean
            or shape.normalized
        )

        evidence_engine = EvidenceEngine(
            snapshot.final_url
            or shape.clean
        )

        # ----------------------------------------------------
        # LAYER 1: RAW HTML SEMANTIC
        # ----------------------------------------------------

        evidence_engine.scan_semantic_html(
            snapshot.raw_html,
            "html_semantic",
        )

        # ----------------------------------------------------
        # LAYER 2: EMBEDDED JSON
        # ----------------------------------------------------

        for obj in snapshot.json_objects:

            evidence_engine.scan_json(
                obj,
                "embedded_json",
            )

        # ----------------------------------------------------
        # LAYER 3: JSON-LD
        # ----------------------------------------------------

        for obj in snapshot.jsonld_objects:

            evidence_engine.scan_json(
                obj,
                "jsonld",
            )

        # ----------------------------------------------------
        # LAYER 4: META
        # ----------------------------------------------------

        self._scan_meta(
            snapshot,
            evidence_engine,
        )

        # ----------------------------------------------------
        # OBJECT IDs
        # ----------------------------------------------------

        objects = object_ids_from_shape(
            shape
        )

        discovered_objects = (
            extract_object_ids(
                snapshot
            )
        )

        for key, value in discovered_objects.items():

            if value:
                objects[key] = value

        object_id_set = {
            value
            for value in objects.values()
            if value
        }

        # ----------------------------------------------------
        # PROFILE DISCOVERY
        # ----------------------------------------------------

        profile_urls = []

        direct_profile = (
            profile_base_from_shape(
                shape
            )
        )

        if direct_profile:
            profile_urls.append(
                direct_profile
            )

        for candidate_url in (
            profile_urls_from_snapshot(
                snapshot
            )
        ):

            if (
                candidate_url
                not in profile_urls
            ):
                profile_urls.append(
                    candidate_url
                )

            if len(profile_urls) >= MAX_PROFILE_CHECKS:
                break

        # ----------------------------------------------------
        # FIRST PROFILE SNAPSHOT
        # ----------------------------------------------------

        profile = None

        if profile_urls:

            profile = (
                await self.profile_resolver.verify(
                    profile_urls[0]
                )
            )

            # Add profile evidence to graph.
            if profile:

                evidence_engine.items.extend(
                    profile.evidence
                )

        # ----------------------------------------------------
        # ENTITY
        # ----------------------------------------------------

        entity_type = classify_entity(
            shape,
            profile,
        )

        # ----------------------------------------------------
        # CANDIDATES
        # ----------------------------------------------------

        candidates = (
            self.identity.build_candidates(
                evidence_engine.items,
                object_id_set,
                shape.route_entity_id,
            )
        )

        # ----------------------------------------------------
        # CONFLICT DETECTION
        # ----------------------------------------------------

        ConflictDetector.detect(
            candidates,
            object_id_set,
            entity_type,
        )

        # ----------------------------------------------------
        # DEEP PROFILE VERIFICATION
        # ----------------------------------------------------

        verified_candidate = None

        verified_profile = profile

        if candidates:

            verified_candidate, deep_profile = (
                await self.identity.verify(
                    candidates,
                    profile_urls,
                    username=(
                        shape.username
                        or (
                            profile.username
                            if profile
                            else ""
                        )
                    ),
                    canonical_url=(
                        snapshot.canonical_url
                    ),
                    entity_type=entity_type,
                )
            )

            if deep_profile:
                verified_profile = (
                    deep_profile
                )

                # Merge deep profile evidence.
                evidence_engine.items.extend(
                    deep_profile.evidence
                )

        # ----------------------------------------------------
        # CONTENT METADATA
        # ----------------------------------------------------

        title, description, thumbnail = (
            extract_content_metadata(
                snapshot
            )
        )

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        result = ResultBuilder.build(
            shape=shape,
            snapshot=snapshot,
            profile=verified_profile,
            candidate=verified_candidate,
            title=title,
            description=description,
            thumbnail=thumbnail,
            started=started,
            evidence=evidence_engine.items,
        )

        # ----------------------------------------------------
        # ENTITY SAFETY
        # ----------------------------------------------------

        if result.entity_type in {
            "PAGE",
            "GROUP",
        }:

            result.uid = ""

            result.verified = False

            result.status = (
                "NOT_VERIFIED"
            )

            result.notes = [
                "Đây là PAGE/GROUP entity; "
                "ID của entity không được coi là "
                "USER UID."
            ]

        return result

    @staticmethod
    def _scan_meta(
        snapshot: PageSnapshot,
        engine: EvidenceEngine,
    ) -> None:

        # Meta can contain explicit profile IDs
        # in unusual public pages.
        for key, value in snapshot.meta.items():

            lower = key.lower()

            if not value:
                continue

            if lower in {
                "profile:id",
                "profile_id",
                "user:id",
                "user_id",
            }:

                match = re.search(
                    r"\d{5,30}",
                    value,
                )

                if match:

                    engine.add(
                        match.group(),
                        key,
                        "meta",
                        "meta",
                        "USER_CANDIDATE",
                        105,
                        context=value,
                        independent_group="meta",
                    )


# ============================================================
# TELEGRAM FORMATTING
# ============================================================

def tg_escape(
    value: Any,
) -> str:

    return html_lib.escape(
        clean_text(value)
    )


def truncate(
    value: str,
    limit: int,
) -> str:

    value = clean_text(value)

    if len(value) <= limit:
        return value

    return (
        value[:limit - 1]
        + "…"
    )


def entity_label(
    entity_type: str,
) -> str:

    mapping = {
        "USER": "👤 Cá nhân",
        "PAGE": "📄 Trang",
        "GROUP": "👥 Nhóm",
        "EVENT": "📅 Sự kiện",
        "UNKNOWN": "❔ Không xác định",
    }

    return mapping.get(
        entity_type,
        "❔ Không xác định",
    )


def content_label(
    result: ResolveResult,
) -> str:

    if result.reel_id:
        return "REEL"

    if result.video_id:
        return "VIDEO"

    if result.photo_id:
        return "PHOTO"

    if result.story_id:
        return "STORY"

    if result.post_id:
        return "POST"

    if result.album_id:
        return "ALBUM"

    if result.content_type:
        return result.content_type.upper()

    return ""


def format_result(
    result: ResolveResult,
    index: int,
) -> str:

    lines = []

    lines.append(
        "╭──────────────────────────────╮"
    )

    lines.append(
        f"│ 🔎 <b>FACEBOOK RESOLVER</b> "
        f"<code>#{index}</code>"
    )

    lines.append(
        "├──────────────────────────────┤"
    )

    # --------------------------------------------------------
    # PROFILE
    # --------------------------------------------------------

    if (
        result.name
        or result.username
        or result.uid
        or result.profile_url
        or result.entity_type
        != "UNKNOWN"
    ):

        lines.append(
            "│ 👤 <b>NGƯỜI ĐĂNG</b>"
        )

        if result.name:

            lines.append(
                "│ Tên      : "
                + tg_escape(
                    truncate(
                        result.name,
                        120,
                    )
                )
            )

        if result.username:

            lines.append(
                "│ Username : @"
                + tg_escape(
                    truncate(
                        result.username,
                        80,
                    )
                )
            )

        if result.uid:

            lines.append(
                "│ UID      : "
                + tg_escape(
                    result.uid
                )
            )

        else:

            lines.append(
                "│ UID      : "
                "<b>⚠️ CHƯA XÁC MINH</b>"
            )

        lines.append(
            "│ Loại     : "
            + tg_escape(
                entity_label(
                    result.entity_type
                )
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
                "<a href=\""
                + html_lib.escape(
                    result.avatar_url,
                    quote=True,
                )
                + "\">Mở ảnh</a>"
            )

    # --------------------------------------------------------
    # CONTENT
    # --------------------------------------------------------

    if (
        result.post_id
        or result.video_id
        or result.reel_id
        or result.photo_id
        or result.story_id
        or result.title
    ):

        lines.append(
            "│"
        )

        lines.append(
            "│ 🎬 <b>NỘI DUNG</b>"
        )

        ctype = content_label(
            result
        )

        if ctype:

            lines.append(
                "│ Loại     : "
                + tg_escape(
                    ctype
                )
            )

        if result.reel_id:

            lines.append(
                "│ Reel ID  : "
                + tg_escape(
                    result.reel_id
                )
            )

        if result.video_id:

            lines.append(
                "│ Video ID : "
                + tg_escape(
                    result.video_id
                )
            )

        if result.post_id:

            lines.append(
                "│ Post ID  : "
                + tg_escape(
                    result.post_id
                )
            )

        if result.photo_id:

            lines.append(
                "│ Photo ID : "
                + tg_escape(
                    result.photo_id
                )
            )

        if result.story_id:

            lines.append(
                "│ Story ID : "
                + tg_escape(
                    result.story_id
                )
            )

        if result.title:

            lines.append(
                "│ Tiêu đề  : "
                + tg_escape(
                    truncate(
                        result.title,
                        240,
                    )
                )
            )

    # --------------------------------------------------------
    # LINKS
    # --------------------------------------------------------

    if (
        result.profile_url
        or result.content_url
        or result.canonical_url
    ):

        lines.append(
            "│"
        )

        lines.append(
            "│ 🔗 <b>LIÊN KẾT</b>"
        )

        if result.profile_url:

            lines.append(
                "│ Profile  : "
                "<a href=\""
                + html_lib.escape(
                    result.profile_url,
                    quote=True,
                )
                + "\">Mở trang cá nhân</a>"
            )

        if result.content_url:

            lines.append(
                "│ Content  : "
                "<a href=\""
                + html_lib.escape(
                    result.content_url,
                    quote=True,
                )
                + "\">Mở nội dung</a>"
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

    if result.verified:

        lines.append(
            "│ Trạng thái : "
            "<b>✅ VERIFIED</b>"
        )

        lines.append(
            "│ Độ tin cậy : "
            f"<b>{result.confidence:.1f}%</b>"
        )

        if result.evidence_sources:

            lines.append(
                "│ Bằng chứng : "
                f"{result.evidence_sources} nguồn"
            )

    else:

        lines.append(
            "│ Trạng thái : "
            "<b>⚠️ NOT VERIFIED</b>"
        )

        for note in result.notes[:2]:

            lines.append(
                "│ "
                + tg_escape(
                    truncate(
                        note,
                        250,
                    )
                )
            )

    # --------------------------------------------------------
    # FORENSIC SIGNALS
    # --------------------------------------------------------

    if result.verified and result.signals:

        lines.append(
            "│"
        )

        lines.append(
            "│ 🔬 <b>DẤU HIỆU</b>"
        )

        for signal in result.signals[:4]:

            lines.append(
                "│ • "
                + tg_escape(
                    signal
                )
            )

    lines.append(
        "╰──────────────────────────────╯"
    )

    lines.append(
        f"⏱ {result.elapsed:.2f}s"
    )

    return "\n".join(
        lines
    )


def format_all_results(
    results: List[ResolveResult],
) -> str:

    if not results:
        return (
            "⚠️ <b>Không tìm thấy URL Facebook "
            "hợp lệ.</b>"
        )

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
# TELETHON HANDLER
# ============================================================

async def _resolve_urls(
    event,
    urls: List[str],
):

    if not urls:

        await event.reply(
            "🔎 <b>FACEBOOK RESOLVER</b>\n\n"
            "Hãy gửi URL Facebook cần kiểm tra."
            "\n\n"
            "Ví dụ:\n"
            "<code>/getuidfb https://facebook.com/...</code>",
            parse_mode="html",
            link_preview=False,
        )

        return

    urls = urls[
        :MAX_INPUT_URLS
    ]

    resolver = FacebookResolver()

    # --------------------------------------------------------
    # Concurrent resolution
    # --------------------------------------------------------

    tasks = [
        resolver.resolve(
            url
        )
        for url in urls
    ]

    try:

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

    except Exception as exc:

        log.exception(
            "Resolver gather failed: %s",
            exc,
        )

        await event.reply(
            "❌ Không thể hoàn tất quá trình "
            "phân tích.",
        )

        return

    clean_results = []

    for original_url, result in zip(
        urls,
        results,
    ):

        if isinstance(
            result,
            Exception,
        ):

            log.debug(
                "URL resolver exception: %s",
                result,
            )

            fallback = ResolveResult(
                input_url=original_url,
                status="NOT_VERIFIED",
                notes=[
                    "Không thể hoàn tất truy vấn "
                    "công khai cho URL này."
                ],
            )

            clean_results.append(
                fallback
            )

        else:

            clean_results.append(
                result
            )

    # --------------------------------------------------------
    # One consolidated Telegram message
    # --------------------------------------------------------

    output = format_all_results(
        clean_results
    )

    if len(output) > 4000:

        output = output[:3900]

        output += (
            "\n\n⚠️ Một phần kết quả đã được "
            "rút gọn để tránh gửi quá nhiều dữ liệu."
        )

    await event.reply(
        output,
        parse_mode="html",
        link_preview=False,
    )


# ============================================================
# COMMAND PARSER
# ============================================================

def command_arguments(
    event,
) -> str:

    text = (
        getattr(
            event,
            "raw_text",
            "",
        )
        or ""
    )

    match = re.match(
        r"^/\s*getuidfb"
        r"(?:@\w+)?"
        r"(?:\s+(.*))?$",
        text,
        re.I | re.S,
    )

    if not match:
        return ""

    return (
        match.group(1)
        or ""
    ).strip()


# ============================================================
# REGISTER
# ============================================================

def register(
    bot,
    notify_bot=None,
):
    """
    REQUIRED BY:
        commands/__init__.py

    Compatible:
        module.register(bot, notify_bot)
    """

    @bot.on(
        events.NewMessage(
            pattern=r"^/\s*getuidfb(?:@\w+)?(?:\s+.*)?$"
        )
    )
    async def getuidfb_handler(
        event,
    ):

        try:

            arguments = command_arguments(
                event
            )

            urls = extract_facebook_urls(
                arguments
            )

            if not urls:

                await event.reply(
                    "🔎 <b>FACEBOOK RESOLVER</b>\n\n"
                    "Gửi URL Facebook cần kiểm tra.\n\n"
                    "Hệ thống sẽ quét nhiều lớp "
                    "và chỉ trả UID khi đủ bằng chứng.",
                    parse_mode="html",
                    link_preview=False,
                )

                return

            await _resolve_urls(
                event,
                urls,
            )

        except Exception as exc:

            log.exception(
                "getuidfb handler error: %s",
                exc,
            )

            try:

                await event.reply(
                    "❌ Đã xảy ra lỗi trong quá trình "
                    "phân tích Facebook."
                )

            except Exception:
                pass

    return getuidfb_handler


# ============================================================
# OPTIONAL DIRECT API
# ============================================================

async def resolve_facebook_url(
    url: str,
) -> ResolveResult:

    resolver = FacebookResolver()

    return await resolver.resolve(
        url
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
            "videos/"
            "4697823150500072/"
        ),
        (
            "https://www.facebook.com/"
            "61592487939720/"
            "videos/"
            "1671583863938536/"
        ),
        (
            "https://www.facebook.com/"
            "reel/"
            "4697823150500072/"
        ),
        (
            "https://www.facebook.com/"
            "profile.php?id=12345678901234"
        ),
    ]

    for url in tests:

        shape = URLParser.parse(
            url
        )

        print(
            "\nURL:",
            url,
        )

        print(
            " KIND:",
            shape.kind,
        )

        print(
            " USERNAME:",
            shape.username,
        )

        print(
            " ROUTE ENTITY:",
            shape.route_entity_id,
        )

        print(
            " POST:",
            shape.post_id,
        )

        print(
            " VIDEO:",
            shape.video_id,
        )

        print(
            " REEL:",
            shape.reel_id,
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO
    )

    self_test()