#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
======================================================================
 FACEBOOK UID / ENTITY RESOLVER V30 FORENSIC PRECISION
======================================================================

PUBLIC FACEBOOK CONTENT ONLY
HTTP ONLY

NO:
    - Playwright
    - Selenium
    - Chromium
    - Cookies
    - Facebook login
    - Facebook access token
    - Graph API authentication
    - UID guessing

DESIGN GOAL
-----------
Maximize UID accuracy while minimizing false-positive UID assignments.

CORE RULE
---------
    OBJECT ID != USER UID

A numeric value found in:
    /123456789/videos/987654321/
is NOT automatically a user UID.

The resolver builds an evidence graph and only promotes a candidate
to VERIFIED UID when sufficiently independent identity evidence exists.

SUPPORTED
---------
    facebook.com/<username>
    facebook.com/profile.php?id=<uid>
    facebook.com/<username>/posts/<post_id>
    facebook.com/<username>/videos/<video_id>
    facebook.com/<username>/reels/<reel_id>
    facebook.com/<username>/photos/<photo_id>
    facebook.com/<username>/story.php?story_fbid=...
    facebook.com/groups/<group>/posts/<post_id>
    facebook.com/pages/<page>/<page_id>
    facebook.com/<numeric>/videos/<video_id>
    facebook.com/reel/<id>
    facebook.com/watch/?v=<id>
    facebook.com/photo.php?fbid=<id>
    facebook.com/permalink.php?story_fbid=...
    facebook.com/share/...
    facebook.com/p/<opaque>
    fb.watch/...

FEATURES
--------
    - Robust URL scanner
    - Concatenated Facebook URL detection
    - Redirect resolution
    - Canonical resolution
    - Meta extraction
    - JSON-LD extraction
    - application/json extraction
    - Embedded JSON extraction
    - Semantic ID extraction
    - URL structural extraction
    - Object/entity separation
    - Profile correlation
    - Identity verification
    - Conflict detection
    - Evidence graph
    - Source diversity scoring
    - Same-source inflation protection
    - Telegram Telethon integration
    - register(bot, notify_bot=None)
    - self-test mode

PRINCIPLE
---------
    No independent correlation => NOT VERIFIED
======================================================================
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
import time

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import requests
from telethon import events


# ======================================================================
# LOGGING
# ======================================================================

log = logging.getLogger("facebook_resolver")


# ======================================================================
# CONFIG
# ======================================================================

REQUEST_TIMEOUT = 12
MAX_HTML_BYTES = 12 * 1024 * 1024

MAX_DISCOVERED_URLS = 120
MAX_CANDIDATES = 80
MAX_PROFILE_CHECKS = 5

SESSION_TIMEOUT = 900

MAX_INPUT_URLS = 20

# Do not hammer Facebook.
FETCH_DELAY = 0.15

# Maximum amount of raw embedded JSON to inspect.
MAX_JSON_SCRIPT_BYTES = 3 * 1024 * 1024


# ======================================================================
# FACEBOOK HOSTS
# ======================================================================

FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "mobile.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
    "free.facebook.com",
    "developers.facebook.com",
}

FB_WATCH_HOSTS = {
    "fb.watch",
}


# ======================================================================
# USER AGENT
# ======================================================================

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 14; Mobile) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Mobile Safari/537.36"
    ),
]


# ======================================================================
# REGEX
# ======================================================================

NUMERIC_ID_RE = re.compile(r"^\d{5,21}$")

LONG_NUMERIC_RE = re.compile(r"^\d{8,21}$")

PF_BID_RE = re.compile(
    r"\bpfbid[A-Za-z0-9_-]+\b",
    re.I,
)

FACEBOOK_URL_RE = re.compile(
    r"""(?ix)
    (?:
        https?://
    )?
    (?:
        www\.
        |m\.
        |mbasic\.
        |mobile\.
        |web\.
        |touch\.
        |free\.
    )?
    facebook\.com
    (?:
        /[^\s<>"']*
    )
    |
    (?:
        https?://
    )?
    fb\.watch
    /[^\s<>"']*
    """
)


# ======================================================================
# SEMANTIC FIELD WEIGHTS
# ======================================================================

USER_FIELD_WEIGHTS = {
    "user_id": 120,
    "userid": 120,
    "user_id_str": 120,

    "profile_id": 120,
    "profileid": 120,

    "owner_id": 110,
    "ownerid": 110,

    "publisher_id": 108,
    "publisherid": 108,

    "author_id": 105,
    "authorid": 105,

    "creator_id": 102,
    "creatorid": 102,

    "from_id": 102,
    "fromid": 102,

    "actor_id": 95,
    "actorid": 95,

    "page_owner_id": 100,

    "profile.uid": 120,
    "profile.id": 120,

    "owner.id": 110,
    "publisher.id": 108,
    "author.id": 105,
    "creator.id": 102,

    "from.id": 105,
    "actor.id": 95,

    "mainentityofpage.id": 92,
}


OBJECT_FIELD_WEIGHTS = {
    "post_id": 120,
    "postid": 120,

    "story_fbid": 120,
    "storyfbid": 120,

    "video_id": 120,
    "videoid": 120,

    "reel_id": 120,
    "reelid": 120,

    "photo_id": 118,
    "photoid": 118,

    "media_fbid": 112,
    "mediafbid": 112,

    "album_id": 105,
    "albumid": 105,

    "group_id": 125,
    "groupid": 125,

    "page_id": 125,
    "pageid": 125,

    "event_id": 120,
    "eventid": 120,
}


# ======================================================================
# URL / ID HELPERS
# ======================================================================

def is_numeric_id(value: Any) -> bool:
    if value is None:
        return False

    value = str(value).strip()

    return bool(NUMERIC_ID_RE.fullmatch(value))


def is_long_numeric(value: Any) -> bool:
    if value is None:
        return False

    value = str(value).strip()

    return bool(LONG_NUMERIC_RE.fullmatch(value))


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    value = html_lib.unescape(str(value))
    value = value.replace("\x00", " ")

    return value.strip()


def compact_text(value: Any, limit: int = 500) -> str:
    value = clean_text(value)
    value = re.sub(r"\s+", " ", value)

    if len(value) > limit:
        return value[:limit] + "..."

    return value


def normalize_host(host: str) -> str:
    return (host or "").lower().strip().rstrip(".")


def is_facebook_host(host: str) -> bool:
    host = normalize_host(host)

    return (
        host in FACEBOOK_HOSTS
        or host in FB_WATCH_HOSTS
    )


def is_facebook_url(url: str) -> bool:
    try:
        parsed = urlparse(url)

        return (
            parsed.scheme in {"http", "https"}
            and is_facebook_host(parsed.hostname or "")
        )

    except Exception:
        return False


def normalize_url(url: str) -> str:
    """
    Normalize a Facebook URL without destroying query semantics.
    """

    if not url:
        return ""

    url = clean_text(url)

    # Telegram/user input often contains surrounding punctuation.
    url = url.strip(
        " \t\r\n"
        "\"'<>[]{}"
        "，。！？；：、"
    )

    if not re.match(r"^https?://", url, re.I):
        if url.lower().startswith("fb.watch/"):
            url = "https://" + url
        elif url.lower().startswith("facebook.com/"):
            url = "https://" + url
        elif url.lower().startswith("www.facebook.com/"):
            url = "https://" + url
        elif url.lower().startswith("m.facebook.com/"):
            url = "https://" + url
        else:
            return ""

    try:
        p = urlparse(url)

        if not p.hostname:
            return ""

        host = normalize_host(p.hostname)

        if not is_facebook_host(host):
            return ""

        path = re.sub(r"/{2,}", "/", p.path or "/")

        # Remove trailing punctuation but preserve actual route slash.
        path = path.rstrip(" \t\r\n")

        while path.endswith((".", ",", ";", ":", "!", "?", ")")):
            path = path[:-1]

        if not path:
            path = "/"

        # Preserve query but remove common tracking parameters.
        query_pairs = parse_qsl(
            p.query,
            keep_blank_values=True,
        )

        filtered = []

        for key, value in query_pairs:
            lk = key.lower()

            if lk in {
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "utm_term",
                "utm_content",
                "fbclid",
                "refsrc",
            }:
                continue

            filtered.append((key, value))

        query = urlencode(
            filtered,
            doseq=True,
        )

        return urlunparse(
            (
                "https",
                host,
                path,
                "",
                query,
                "",
            )
        )

    except Exception:
        return ""


# ======================================================================
# URL SCANNER
# ======================================================================

def extract_facebook_urls(text: str) -> List[str]:
    """
    Robust scanner.

    Handles:
        URL URL
        URL,URL
        URLURL
        Telegram text around URL
        escaped HTML
        www.facebook.com
        facebook.com
        m.facebook.com
        fb.watch
    """

    if not text:
        return []

    text = html_lib.unescape(text)

    # Decode common escaped separators.
    text = text.replace("\\/", "/")

    results: List[str] = []
    seen: Set[str] = set()

    # Pass 1: regex.
    for match in FACEBOOK_URL_RE.finditer(text):

        raw = match.group(0)

        raw = raw.strip(
            " \t\r\n"
            "\"'<>[]{}"
        )

        # Remove punctuation generated by prose.
        raw = raw.rstrip(
            ".,;:!?)]}>'\""
        )

        normalized = normalize_url(raw)

        if not normalized:
            continue

        if normalized in seen:
            continue

        seen.add(normalized)
        results.append(normalized)

        if len(results) >= MAX_INPUT_URLS:
            return results

    # Pass 2:
    # Search for known Facebook hostname boundaries even if regex
    # encountered unusual concatenated input.
    lower = text.lower()

    host_patterns = [
        "https://www.facebook.com/",
        "https://facebook.com/",
        "http://www.facebook.com/",
        "http://facebook.com/",
        "www.facebook.com/",
        "facebook.com/",
        "https://m.facebook.com/",
        "m.facebook.com/",
        "https://mbasic.facebook.com/",
        "mbasic.facebook.com/",
        "https://fb.watch/",
        "fb.watch/",
    ]

    for marker in host_patterns:
        start = 0

        while True:
            idx = lower.find(marker.lower(), start)

            if idx < 0:
                break

            tail = text[idx:]

            # Stop at whitespace / HTML boundary.
            candidate = re.split(
                r"""[\s<>"']""",
                tail,
                maxsplit=1,
            )[0]

            candidate = candidate.rstrip(
                ".,;:!?)]}>'\""
            )

            normalized = normalize_url(candidate)

            if normalized and normalized not in seen:
                seen.add(normalized)
                results.append(normalized)

            start = idx + len(marker)

            if len(results) >= MAX_INPUT_URLS:
                break

        if len(results) >= MAX_INPUT_URLS:
            break

    return results


# ======================================================================
# URL SHAPE
# ======================================================================

@dataclass
class URLShape:
    original: str
    normalized: str

    host: str = ""
    path: str = ""
    query: str = ""

    kind: str = "unknown"

    username: str = ""

    # IMPORTANT:
    # route_entity_id is NOT automatically a UID.
    route_entity_id: str = ""

    numeric_path_id: str = ""

    post_id: str = ""
    video_id: str = ""
    reel_id: str = ""
    photo_id: str = ""
    story_id: str = ""

    group_id: str = ""
    page_id: str = ""
    album_id: str = ""

    opaque_token: str = ""


# ======================================================================
# URL CLASSIFIER
# ======================================================================

def query_value(
    parsed,
    *names: str,
) -> str:
    values = dict(
        parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
    )

    lower_map = {
        str(k).lower(): v
        for k, v in values.items()
    }

    for name in names:
        value = lower_map.get(name.lower())

        if value:
            return clean_text(value)

    return ""


def classify_url(url: str) -> URLShape:
    normalized = normalize_url(url)

    if not normalized:
        return URLShape(
            original=url,
            normalized="",
        )

    p = urlparse(normalized)

    host = normalize_host(p.hostname or "")

    segments = [
        unquote(x).strip()
        for x in p.path.split("/")
        if x.strip()
    ]

    lower_segments = [
        x.lower()
        for x in segments
    ]

    shape = URLShape(
        original=url,
        normalized=normalized,
        host=host,
        path=p.path,
        query=p.query,
    )

    # --------------------------------------------------------------
    # fb.watch
    # --------------------------------------------------------------

    if host in FB_WATCH_HOSTS:
        shape.kind = "share"
        return shape

    # --------------------------------------------------------------
    # profile.php
    # --------------------------------------------------------------

    if lower_segments[:1] == ["profile.php"]:
        uid = query_value(
            p,
            "id",
        )

        if is_numeric_id(uid):
            shape.kind = "profile"
            shape.username = ""
            shape.numeric_path_id = uid
            shape.route_entity_id = uid

        return shape

    # --------------------------------------------------------------
    # photo.php
    # --------------------------------------------------------------

    if lower_segments[:1] == ["photo.php"]:

        fbid = query_value(
            p,
            "fbid",
            "photo_id",
            "id",
        )

        album_id = query_value(
            p,
            "set",
            "album_id",
        )

        if is_numeric_id(fbid):
            shape.photo_id = fbid

        if is_numeric_id(album_id):
            shape.album_id = album_id

        shape.kind = "photo"

        return shape

    # --------------------------------------------------------------
    # story.php
    # --------------------------------------------------------------

    if lower_segments[:1] == ["story.php"]:

        story_id = query_value(
            p,
            "story_fbid",
            "story_id",
            "id",
        )

        if is_numeric_id(story_id):
            shape.story_id = story_id

        shape.kind = "story"

        return shape

    # --------------------------------------------------------------
    # permalink.php
    # --------------------------------------------------------------

    if lower_segments[:1] == ["permalink.php"]:

        post_id = query_value(
            p,
            "story_fbid",
            "fbid",
            "id",
        )

        if is_numeric_id(post_id):
            shape.post_id = post_id

        shape.kind = "permalink"

        return shape

    # --------------------------------------------------------------
    # WATCH
    # --------------------------------------------------------------

    if lower_segments[:1] == ["watch"]:

        video_id = query_value(
            p,
            "v",
            "video_id",
        )

        if is_numeric_id(video_id):
            shape.video_id = video_id

        shape.kind = "watch"

        return shape

    # --------------------------------------------------------------
    # REEL
    # --------------------------------------------------------------

    if lower_segments[:1] == ["reel"]:

        if len(segments) >= 2:
            candidate = segments[1]

            if is_numeric_id(candidate):
                shape.reel_id = candidate

        shape.kind = "reel"

        return shape

    # --------------------------------------------------------------
    # REELS
    # --------------------------------------------------------------

    if lower_segments[:1] == ["reels"]:

        if len(segments) >= 2:
            candidate = segments[1]

            if is_numeric_id(candidate):
                shape.reel_id = candidate

        shape.kind = "reel"

        return shape

    # --------------------------------------------------------------
    # GROUPS
    # --------------------------------------------------------------

    if lower_segments[:1] == ["groups"]:

        if len(segments) >= 2:
            group = segments[1]

            if is_numeric_id(group):
                shape.group_id = group
                shape.route_entity_id = group

            else:
                shape.username = group

        if "posts" in lower_segments:
            idx = lower_segments.index("posts")

            if idx + 1 < len(segments):
                candidate = segments[idx + 1]

                if is_numeric_id(candidate):
                    shape.post_id = candidate

            shape.kind = "group_post"

        else:
            shape.kind = "group"

        return shape

    # --------------------------------------------------------------
    # PAGES
    # --------------------------------------------------------------

    if lower_segments[:1] == ["pages"]:

        shape.kind = "page"

        if len(segments) >= 2:
            first = segments[1]

            if is_numeric_id(first):
                shape.page_id = first
                shape.route_entity_id = first
            else:
                shape.username = first

        if len(segments) >= 3:
            second = segments[2]

            if is_numeric_id(second):
                shape.page_id = second
                shape.route_entity_id = second

        return shape

    # --------------------------------------------------------------
    # PEOPLE
    # --------------------------------------------------------------

    if lower_segments[:1] == ["people"]:

        shape.kind = "profile"

        if len(segments) >= 2:
            shape.username = segments[1]

        if len(segments) >= 3:
            candidate = segments[2]

            if is_numeric_id(candidate):
                shape.route_entity_id = candidate

        return shape

    # --------------------------------------------------------------
    # SHARE
    # --------------------------------------------------------------

    if lower_segments[:1] == ["share"]:
        shape.kind = "share"

        return shape

    # --------------------------------------------------------------
    # P/<opaque>
    # --------------------------------------------------------------

    if lower_segments[:1] == ["p"]:

        shape.kind = "post"

        if len(segments) >= 2:
            shape.opaque_token = segments[1]

        return shape

    # --------------------------------------------------------------
    # Generic route
    # --------------------------------------------------------------

    if not segments:
        shape.kind = "home"
        return shape

    first = segments[0]

    # Numeric first route segment.
    # NEVER automatically call this a UID.
    if is_numeric_id(first):
        shape.route_entity_id = first

    else:
        shape.username = first

    # Find object route.
    for i, segment in enumerate(lower_segments):

        if segment == "posts":
            shape.kind = "post"

            if i + 1 < len(segments):
                candidate = segments[i + 1]

                if is_numeric_id(candidate):
                    shape.post_id = candidate

            break

        if segment == "videos":
            shape.kind = "video"

            if i + 1 < len(segments):
                candidate = segments[i + 1]

                if is_numeric_id(candidate):
                    shape.video_id = candidate

            break

        if segment == "reels":
            shape.kind = "reel"

            if i + 1 < len(segments):
                candidate = segments[i + 1]

                if is_numeric_id(candidate):
                    shape.reel_id = candidate

            break

        if segment == "photos":
            shape.kind = "photo"

            if i + 1 < len(segments):
                candidate = segments[i + 1]

                if is_numeric_id(candidate):
                    shape.photo_id = candidate

            break

        if segment == "stories":
            shape.kind = "story"

            if i + 1 < len(segments):
                candidate = segments[i + 1]

                if is_numeric_id(candidate):
                    shape.story_id = candidate

            break

    else:
        if shape.username:
            shape.kind = "profile"

        elif shape.route_entity_id:
            shape.kind = "entity"

        else:
            shape.kind = "unknown"

    return shape


# ======================================================================
# EVIDENCE
# ======================================================================

@dataclass
class Evidence:
    value: str

    role: str

    source: str

    weight: float

    context: str = ""

    url: str = ""

    token: str = ""

    independent: bool = True

    verified: bool = False

    path: str = ""

    key: str = ""

    neighbor: str = ""

    confidence: float = 0.0

    def fingerprint(self) -> Tuple:
        return (
            self.value,
            self.role,
            self.source,
            self.path,
            self.key,
        )


# ======================================================================
# SNAPSHOT
# ======================================================================

@dataclass
class Snapshot:
    requested_url: str

    final_url: str = ""

    status_code: int = 0

    html: str = ""

    title: str = ""

    canonical: str = ""

    meta: Dict[str, str] = field(
        default_factory=dict
    )

    links: List[str] = field(
        default_factory=list
    )

    jsonld: List[Any] = field(
        default_factory=list
    )

    embedded_json: List[Any] = field(
        default_factory=list
    )

    redirects: List[str] = field(
        default_factory=list
    )

    error: str = ""


# ======================================================================
# HTML PARSER
# ======================================================================

class FacebookHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.title_parts: List[str] = []

        self.meta: Dict[str, str] = {}

        self.links: List[str] = []

        self.jsonld: List[Any] = []

        self.embedded_json: List[Any] = []

        self._title_depth = 0

        self._script_type = ""

        self._script_buffer: List[str] = []

        self._inside_script = False

        self._script_attrs: Dict[str, str] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs,
    ):
        tag = tag.lower()

        attr_map = {
            str(k).lower(): (
                "" if v is None else str(v)
            )
            for k, v in attrs
        }

        if tag == "title":
            self._title_depth += 1

        if tag == "meta":

            key = (
                attr_map.get("property")
                or attr_map.get("name")
                or attr_map.get("itemprop")
                or attr_map.get("data-name")
                or ""
            ).strip().lower()

            value = (
                attr_map.get("content")
                or ""
            ).strip()

            if key and value:
                self.meta[key] = value

        elif tag == "link":

            href = attr_map.get("href")

            if href:
                self.links.append(href)

        elif tag == "script":

            self._inside_script = True

            self._script_attrs = attr_map

            self._script_type = (
                attr_map.get("type", "")
                .lower()
                .strip()
            )

            self._script_buffer = []

    def handle_endtag(self, tag: str):

        tag = tag.lower()

        if tag == "title":

            if self._title_depth:
                self._title_depth -= 1

        elif tag == "script":

            content = "".join(
                self._script_buffer
            ).strip()

            if content:
                self._parse_script(
                    content
                )

            self._inside_script = False

            self._script_buffer = []

            self._script_type = ""

            self._script_attrs = {}

    def handle_data(self, data: str):

        if self._title_depth:
            self.title_parts.append(data)

        if self._inside_script:
            if sum(
                len(x)
                for x in self._script_buffer
            ) < MAX_JSON_SCRIPT_BYTES:
                self._script_buffer.append(data)

    def _parse_script(
        self,
        content: str,
    ):
        script_type = self._script_type

        if (
            script_type == "application/ld+json"
            or "ld+json" in script_type
        ):
            try:
                obj = json.loads(
                    content
                )

                self.jsonld.append(obj)

            except Exception:
                # Some JSON-LD is malformed.
                pass

            return

        if (
            "application/json" in script_type
            or script_type == "application/json"
        ):
            try:
                obj = json.loads(
                    content
                )

                self.embedded_json.append(obj)

            except Exception:
                pass

            return

        # Facebook frequently embeds JSON-like state in ordinary
        # script tags. We do not blindly eval JavaScript.
        # Instead search for complete JSON objects around obvious
        # semantic fields.
        if any(
            field in content
            for field in (
                '"user_id"',
                '"profile_id"',
                '"owner_id"',
                '"publisher_id"',
                '"author_id"',
                '"page_id"',
                '"group_id"',
                '"video_id"',
                '"post_id"',
            )
        ):
            extracted = extract_json_fragments(
                content
            )

            self.embedded_json.extend(
                extracted
            )

    def get_title(self) -> str:
        return compact_text(
            "".join(
                self.title_parts
            ),
            500,
        )


# ======================================================================
# JSON FRAGMENT EXTRACTION
# ======================================================================

def extract_json_fragments(
    text: str,
    max_fragments: int = 40,
) -> List[Any]:
    """
    Best-effort extraction of balanced JSON objects.

    No eval().
    No JavaScript execution.
    """

    if not text:
        return []

    results = []

    starts = []

    for m in re.finditer(
        r"\{",
        text,
    ):
        starts.append(m.start())

        if len(starts) > 500:
            break

    for start in starts:

        depth = 0

        in_string = False
        escape = False

        for i in range(
            start,
            min(
                len(text),
                start + 1_000_000,
            ),
        ):

            ch = text[i]

            if in_string:

                if escape:
                    escape = False

                elif ch == "\\":
                    escape = True

                elif ch == '"':
                    in_string = False

                continue

            if ch == '"':
                in_string = True
                continue

            if ch == "{":
                depth += 1

            elif ch == "}":
                depth -= 1

                if depth == 0:

                    candidate = text[
                        start:i + 1
                    ]

                    try:
                        obj = json.loads(
                            candidate
                        )

                        results.append(obj)

                    except Exception:
                        pass

                    break

        if len(results) >= max_fragments:
            break

    return results


# ======================================================================
# SNAPSHOT PARSING
# ======================================================================

def parse_snapshot(
    snapshot: Snapshot,
):
    if not snapshot.html:
        return

    parser = FacebookHTMLParser()

    try:
        parser.feed(
            snapshot.html
        )
        parser.close()

    except Exception as exc:
        snapshot.error = (
            f"HTML parser: {exc}"
        )

    snapshot.title = parser.get_title()

    snapshot.meta = parser.meta

    snapshot.links = parser.links

    snapshot.jsonld = parser.jsonld

    snapshot.embedded_json = (
        parser.embedded_json
    )

    canonical_candidates = []

    for key, value in snapshot.meta.items():

        if key in {
            "og:url",
            "al:web:url",
            "twitter:url",
            "canonical",
        }:
            canonical_candidates.append(
                value
            )

    for candidate in canonical_candidates:

        normalized = normalize_url(
            candidate
        )

        if normalized:
            snapshot.canonical = normalized
            break


# ======================================================================
# JSON WALKER
# ======================================================================

def walk_json(
    value: Any,
    path: str = "",
    parent: Any = None,
) -> Iterable[
    Tuple[
        str,
        Any,
        str,
        Any,
    ]
]:

    if isinstance(value, dict):

        for key, child in value.items():

            key_str = str(key)

            child_path = (
                f"{path}.{key_str}"
                if path
                else key_str
            )

            yield (
                child_path,
                child,
                key_str,
                value,
            )

            yield from walk_json(
                child,
                child_path,
                value,
            )

    elif isinstance(value, list):

        for index, child in enumerate(value):

            child_path = (
                f"{path}[{index}]"
            )

            yield (
                child_path,
                child,
                str(index),
                value,
            )

            yield from walk_json(
                child,
                child_path,
                value,
            )


# ======================================================================
# SEMANTIC KEY NORMALIZATION
# ======================================================================

def normalize_key(
    key: str,
) -> str:

    key = str(key or "").strip().lower()

    key = key.replace(
        "-",
        "_",
    )

    key = key.replace(
        " ",
        "_",
    )

    return key


def field_weight(
    key: str,
) -> Tuple[str, float]:
    """
    Returns:
        role, weight
    """

    normalized = normalize_key(
        key
    )

    # Direct keys.
    if normalized in USER_FIELD_WEIGHTS:
        return (
            "USER_CANDIDATE",
            USER_FIELD_WEIGHTS[
                normalized
            ],
        )

    if normalized in OBJECT_FIELD_WEIGHTS:
        return (
            "OBJECT",
            OBJECT_FIELD_WEIGHTS[
                normalized
            ],
        )

    # Dot paths.
    compact = normalized.replace(
        "_",
        ".",
    )

    if compact in USER_FIELD_WEIGHTS:
        return (
            "USER_CANDIDATE",
            USER_FIELD_WEIGHTS[
                compact
            ],
        )

    if compact in OBJECT_FIELD_WEIGHTS:
        return (
            "OBJECT",
            OBJECT_FIELD_WEIGHTS[
                compact
            ],
        )

    return (
        "",
        0.0,
    )


# ======================================================================
# EVIDENCE GRAPH
# ======================================================================

class EvidenceGraph:

    def __init__(self):
        self.items: List[Evidence] = []

        self._seen: Set[
            Tuple
        ] = set()

    def add(
        self,
        evidence: Evidence,
    ):

        if not evidence.value:
            return

        if not is_numeric_id(
            evidence.value
        ):
            return

        fp = evidence.fingerprint()

        if fp in self._seen:
            return

        self._seen.add(fp)

        self.items.append(
            evidence
        )

    def values(
        self,
        role: Optional[str] = None,
    ) -> Set[str]:

        result = set()

        for item in self.items:

            if role is not None:
                if item.role != role:
                    continue

            result.add(
                item.value
            )

        return result

    def for_value(
        self,
        value: str,
    ) -> List[Evidence]:

        return [
            item
            for item in self.items
            if item.value == value
        ]

    def strongest(
        self,
        value: str,
    ) -> Optional[Evidence]:

        items = self.for_value(
            value
        )

        if not items:
            return None

        return max(
            items,
            key=lambda x: x.weight,
        )

    def source_diversity(
        self,
        value: str,
    ) -> int:

        return len({
            item.source
            for item in self.for_value(
                value
            )
        })

    def independent_sources(
        self,
        value: str,
    ) -> Set[str]:

        return {
            item.source
            for item in self.for_value(
                value
            )
            if item.independent
        }


# ======================================================================
# ADD EVIDENCE
# ======================================================================

def add_evidence(
    graph: EvidenceGraph,
    value: Any,
    role: str,
    source: str,
    weight: float,
    context: str = "",
    url: str = "",
    token: str = "",
    independent: bool = True,
    verified: bool = False,
    path: str = "",
    key: str = "",
    neighbor: str = "",
    confidence: float = 0.0,
):

    value = clean_text(value)

    if not is_numeric_id(value):
        return

    graph.add(
        Evidence(
            value=value,
            role=role,
            source=source,
            weight=float(weight),
            context=compact_text(
                context,
                800,
            ),
            url=url,
            token=token,
            independent=independent,
            verified=verified,
            path=path,
            key=key,
            neighbor=neighbor,
            confidence=confidence,
        )
    )


# ======================================================================
# URL STRUCTURAL EVIDENCE
# ======================================================================

def add_structural_evidence(
    graph: EvidenceGraph,
    shape: URLShape,
    url: str,
):

    # --------------------------------------------------------------
    # Explicit profile.php?id=
    # --------------------------------------------------------------

    if (
        shape.kind == "profile"
        and shape.numeric_path_id
    ):

        add_evidence(
            graph,
            shape.numeric_path_id,
            "USER_CANDIDATE",
            "url_profile_id",
            145,
            "Explicit profile.php?id= UID",
            url,
            independent=True,
            verified=False,
            path="query.id",
            key="id",
        )

    # --------------------------------------------------------------
    # group ID
    # --------------------------------------------------------------

    if shape.group_id:

        add_evidence(
            graph,
            shape.group_id,
            "GROUP",
            "url_group_id",
            145,
            "Explicit group route ID",
            url,
            independent=True,
        )

    # --------------------------------------------------------------
    # page ID
    # --------------------------------------------------------------

    if shape.page_id:

        add_evidence(
            graph,
            shape.page_id,
            "PAGE",
            "url_page_id",
            145,
            "Explicit page route ID",
            url,
            independent=True,
        )

    # --------------------------------------------------------------
    # Route entity ID
    # --------------------------------------------------------------
    #
    # IMPORTANT:
    # It is deliberately weak.
    # It is NEVER treated as verified user UID.
    #

    if shape.route_entity_id:

        add_evidence(
            graph,
            shape.route_entity_id,
            "ROUTE_ENTITY",
            "url_route_entity",
            20,
            (
                "Numeric route entity; "
                "NOT automatically user UID"
            ),
            url,
            independent=False,
        )

    # --------------------------------------------------------------
    # Object IDs
    # --------------------------------------------------------------

    if shape.post_id:

        add_evidence(
            graph,
            shape.post_id,
            "POST",
            "url_post_id",
            140,
            "Post object ID",
            url,
            independent=True,
        )

    if shape.video_id:

        add_evidence(
            graph,
            shape.video_id,
            "VIDEO",
            "url_video_id",
            140,
            "Video object ID",
            url,
            independent=True,
        )

    if shape.reel_id:

        add_evidence(
            graph,
            shape.reel_id,
            "REEL",
            "url_reel_id",
            140,
            "Reel object ID",
            url,
            independent=True,
        )

    if shape.photo_id:

        add_evidence(
            graph,
            shape.photo_id,
            "PHOTO",
            "url_photo_id",
            140,
            "Photo object ID",
            url,
            independent=True,
        )

    if shape.story_id:

        add_evidence(
            graph,
            shape.story_id,
            "STORY",
            "url_story_id",
            140,
            "Story object ID",
            url,
            independent=True,
        )

    if shape.album_id:

        add_evidence(
            graph,
            shape.album_id,
            "ALBUM",
            "url_album_id",
            125,
            "Album ID",
            url,
            independent=True,
        )


# ======================================================================
# SEMANTIC HTML EXTRACTION
# ======================================================================

SEMANTIC_REGEX = re.compile(
    r"""
    (?P<key>
        user[_\-]?id
        |userid
        |profile[_\-]?id
        |profileid
        |owner[_\-]?id
        |ownerid
        |publisher[_\-]?id
        |publisherid
        |author[_\-]?id
        |authorid
        |creator[_\-]?id
        |creatorid
        |from[_\-]?id
        |fromid
        |actor[_\-]?id
        |actorid
        |page[_\-]?owner[_\-]?id
        |page[_\-]?id
        |group[_\-]?id
        |post[_\-]?id
        |story[_\-]?fbid
        |video[_\-]?id
        |reel[_\-]?id
        |photo[_\-]?id
        |media[_\-]?fbid
        |album[_\-]?id
    )
    \s*
    ["':=]+
    \s*
    (?P<value>\d{5,21})
    """,
    re.I | re.X,
)


def collect_semantic_evidence(
    graph: EvidenceGraph,
    text: str,
    source: str,
    url: str,
):

    if not text:
        return

    # Avoid huge pathological scanning.
    if len(text) > MAX_HTML_BYTES:
        text = text[
            :MAX_HTML_BYTES
        ]

    for match in SEMANTIC_REGEX.finditer(
        text
    ):

        key = normalize_key(
            match.group("key")
        )

        value = match.group(
            "value"
        )

        role, weight = field_weight(
            key
        )

        if not role:
            continue

        start = max(
            0,
            match.start() - 180,
        )

        end = min(
            len(text),
            match.end() + 300,
        )

        context = text[
            start:end
        ]

        add_evidence(
            graph,
            value,
            role,
            source,
            weight,
            context=context,
            url=url,
            independent=True,
            path=key,
            key=key,
        )


# ======================================================================
# JSON SEMANTIC EXTRACTION
# ======================================================================

def collect_json_semantic_evidence(
    graph: EvidenceGraph,
    objects: Iterable[Any],
    source: str,
    url: str,
):

    for obj in objects:

        for path, value, key, parent in walk_json(
            obj
        ):

            normalized_key = normalize_key(
                key
            )

            role, weight = field_weight(
                normalized_key
            )

            if (
                role
                and is_numeric_id(value)
            ):

                parent_text = compact_text(
                    repr(parent),
                    1000,
                )

                add_evidence(
                    graph,
                    value,
                    role,
                    source,
                    weight + 5,
                    context=parent_text,
                    url=url,
                    independent=True,
                    path=path,
                    key=normalized_key,
                )


# ======================================================================
# JSON-LD ID EXTRACTION
# ======================================================================

def collect_jsonld_identity_evidence(
    graph: EvidenceGraph,
    objects: Iterable[Any],
    url: str,
):

    for obj in objects:

        for path, value, key, parent in walk_json(
            obj
        ):

            key_lower = normalize_key(
                key
            )

            if not isinstance(
                value,
                str,
            ):
                continue

            # JSON-LD commonly contains URLs.
            if value.startswith(
                "http://"
            ) or value.startswith(
                "https://"
            ):

                normalized = normalize_url(
                    value
                )

                if normalized:

                    shape = classify_url(
                        normalized
                    )

                    if (
                        shape.kind == "profile"
                        and shape.numeric_path_id
                    ):

                        add_evidence(
                            graph,
                            shape.numeric_path_id,
                            "USER_CANDIDATE",
                            "jsonld_profile_url",
                            125,
                            context=compact_text(
                                repr(parent),
                                900,
                            ),
                            url=url,
                            independent=True,
                            path=path,
                            key=key_lower,
                        )

            # Direct @id / identifier / sameAs numeric ID.
            if key_lower in {
                "@id",
                "identifier",
                "id",
            }:

                if is_numeric_id(value):

                    add_evidence(
                        graph,
                        value,
                        "USER_CANDIDATE",
                        "jsonld_identifier",
                        95,
                        context=compact_text(
                            repr(parent),
                            900,
                        ),
                        url=url,
                        independent=True,
                        path=path,
                        key=key_lower,
                    )


# ======================================================================
# META EVIDENCE
# ======================================================================

def collect_meta_evidence(
    graph: EvidenceGraph,
    snapshot: Snapshot,
):

    for key, value in snapshot.meta.items():

        key_lower = normalize_key(
            key
        )

        # Meta itself can contain profile URL.
        normalized = normalize_url(
            value
        )

        if normalized:

            shape = classify_url(
                normalized
            )

            if (
                shape.kind == "profile"
                and shape.numeric_path_id
            ):

                add_evidence(
                    graph,
                    shape.numeric_path_id,
                    "USER_CANDIDATE",
                    "meta_profile_url",
                    135,
                    context=f"{key}: {value}",
                    url=snapshot.final_url,
                    independent=True,
                    path=key_lower,
                    key=key_lower,
                )

        # Some metadata directly exposes IDs.
        role, weight = field_weight(
            key_lower
        )

        if (
            role
            and is_numeric_id(value)
        ):

            add_evidence(
                graph,
                value,
                role,
                "meta",
                weight,
                context=f"{key}: {value}",
                url=snapshot.final_url,
                independent=True,
                path=key_lower,
                key=key_lower,
            )


# ======================================================================
# DISCOVER FACEBOOK URLS FROM PAGE
# ======================================================================

def discover_facebook_urls(
    snapshot: Snapshot,
) -> List[str]:

    results = []
    seen = set()

    def add(url: str):

        normalized = normalize_url(
            url
        )

        if not normalized:
            return

        if normalized in seen:
            return

        seen.add(normalized)

        results.append(normalized)

    for link in snapshot.links:

        add(
            urljoin(
                snapshot.final_url
                or snapshot.requested_url,
                link,
            )
        )

        if len(results) >= MAX_DISCOVERED_URLS:
            return results

    for key, value in snapshot.meta.items():

        if (
            "facebook" in value.lower()
            or "fb.watch" in value.lower()
        ):
            add(value)

    for match in FACEBOOK_URL_RE.finditer(
        snapshot.html
    ):

        add(
            match.group(0)
        )

        if len(results) >= MAX_DISCOVERED_URLS:
            break

    return results


# ======================================================================
# HTTP FETCH ENGINE
# ======================================================================

class FetchEngine:

    def __init__(self):
        self.session = requests.Session()

        self.session.headers.update(
            {
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
        )

    def fetch_sync(
        self,
        url: str,
        user_agent: str,
    ) -> Snapshot:

        snapshot = Snapshot(
            requested_url=url
        )

        try:

            headers = {
                "User-Agent": user_agent,
            }

            response = self.session.get(
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
                normalize_url(
                    response.url
                )
                or response.url
            )

            snapshot.redirects = [
                normalize_url(
                    h.url
                ) or h.url
                for h in response.history
            ]

            chunks = []

            total = 0

            content_type = (
                response.headers.get(
                    "Content-Type",
                    "application/x-www-form-urlencoded",
                )
                .lower()
            )

            # We only need HTML/text for resolver.
            if (
                "text/html" not in content_type
                and "application/xhtml" not in content_type
                and not content_type.startswith(
                    "text/"
                )
            ):
                # Still read a limited amount because Facebook
                # can occasionally omit correct Content-Type.
                pass

            for chunk in response.iter_content(
                chunk_size=65536,
                decode_unicode=False,
            ):

                if not chunk:
                    continue

                remaining = (
                    MAX_HTML_BYTES - total
                )

                if remaining <= 0:
                    break

                chunk = chunk[
                    :remaining
                ]

                chunks.append(chunk)

                total += len(chunk)

                if total >= MAX_HTML_BYTES:
                    break

            response.close()

            raw = b"".join(
                chunks
            )

            encoding = (
                response.encoding
                or "utf-8"
            )

            try:
                snapshot.html = raw.decode(
                    encoding,
                    errors="replace",
                )

            except Exception:
                snapshot.html = raw.decode(
                    "utf-8",
                    errors="replace",
                )

            parse_snapshot(
                snapshot
            )

            return snapshot

        except Exception as exc:

            snapshot.error = (
                f"{type(exc).__name__}: {exc}"
            )

            return snapshot

    async def fetch(
        self,
        url: str,
    ) -> Snapshot:

        last = Snapshot(
            requested_url=url
        )

        for user_agent in USER_AGENTS:

            result = await asyncio.to_thread(
                self.fetch_sync,
                url,
                user_agent,
            )

            last = result

            if (
                result.status_code
                and result.html
            ):
                return result

            await asyncio.sleep(
                FETCH_DELAY
            )

        return last


# ======================================================================
# PROFILE URL EXTRACTION
# ======================================================================

def extract_profile_urls(
    snapshot: Snapshot,
) -> List[str]:

    results = []
    seen = set()

    def add(value: str):

        normalized = normalize_url(
            value
        )

        if not normalized:
            return

        shape = classify_url(
            normalized
        )

        if shape.kind != "profile":
            return

        if normalized in seen:
            return

        seen.add(normalized)
        results.append(normalized)

    for link in snapshot.links:

        add(
            urljoin(
                snapshot.final_url,
                link,
            )
        )

        if len(results) >= MAX_PROFILE_CHECKS:
            return results

    for key in (
        "og:url",
        "al:web:url",
        "twitter:url",
        "canonical",
    ):

        value = snapshot.meta.get(
            key
        )

        if value:
            add(value)

    for obj in snapshot.jsonld:

        for path, value, key, parent in walk_json(
            obj
        ):

            if not isinstance(
                value,
                str,
            ):
                continue

            normalized = normalize_url(
                value
            )

            if normalized:
                add(normalized)

            if len(results) >= MAX_PROFILE_CHECKS:
                return results

    return results


# ======================================================================
# PROFILE VERIFICATION
# ======================================================================

@dataclass
class ProfileVerification:
    uid: str

    profile_url: str

    verified: bool = False

    score: float = 0.0

    signals: List[str] = field(
        default_factory=list
    )

    snapshot: Optional[
        Snapshot
    ] = None


async def verify_profile_url(
    fetcher: FetchEngine,
    profile_url: str,
    expected_uid: str,
) -> ProfileVerification:

    result = ProfileVerification(
        uid=expected_uid,
        profile_url=profile_url,
    )

    if not is_numeric_id(
        expected_uid
    ):
        return result

    snapshot = await fetcher.fetch(
        profile_url
    )

    result.snapshot = snapshot

    if not snapshot.html:
        return result

    shape = classify_url(
        profile_url
    )

    # --------------------------------------------------------------
    # Signal 1: explicit input URL ID
    # --------------------------------------------------------------

    if (
        shape.kind == "profile"
        and shape.numeric_path_id
        == expected_uid
    ):
        result.score += 60
        result.signals.append(
            "profile_url_exact_id"
        )

    # --------------------------------------------------------------
    # Signal 2: final URL profile.php?id=
    # --------------------------------------------------------------

    final_shape = classify_url(
        snapshot.final_url
    )

    if (
        final_shape.kind == "profile"
        and final_shape.numeric_path_id
        == expected_uid
    ):
        result.score += 70
        result.signals.append(
            "final_url_exact_profile_id"
        )

    # --------------------------------------------------------------
    # Signal 3: canonical exact profile URL
    # --------------------------------------------------------------

    if snapshot.canonical:

        canonical_shape = classify_url(
            snapshot.canonical
        )

        if (
            canonical_shape.kind == "profile"
            and (
                canonical_shape.numeric_path_id
                == expected_uid
            )
        ):
            result.score += 80
            result.signals.append(
                "canonical_exact_profile_id"
            )

    # --------------------------------------------------------------
    # Signal 4: explicit semantic identity
    # --------------------------------------------------------------

    graph = EvidenceGraph()

    collect_semantic_evidence(
        graph,
        snapshot.html,
        "profile_html_semantic",
        snapshot.final_url,
    )

    collect_meta_evidence(
        graph,
        snapshot,
    )

    collect_json_semantic_evidence(
        graph,
        snapshot.embedded_json,
        "profile_embedded_json",
        snapshot.final_url,
    )

    collect_json_semantic_evidence(
        graph,
        snapshot.jsonld,
        "profile_jsonld",
        snapshot.final_url,
    )

    collect_jsonld_identity_evidence(
        graph,
        snapshot.jsonld,
        snapshot.final_url,
    )

    exact_identity_items = [
        x
        for x in graph.for_value(
            expected_uid
        )
        if x.role == "USER_CANDIDATE"
    ]

    source_names = {
        x.source
        for x in exact_identity_items
    }

    if exact_identity_items:
        result.score += min(
            70,
            sum(
                min(
                    30,
                    x.weight * 0.25
                )
                for x in exact_identity_items
            ),
        )

        result.signals.append(
            "explicit_identity_field"
        )

    # --------------------------------------------------------------
    # Signal 5: identity source diversity
    # --------------------------------------------------------------

    if len(source_names) >= 2:
        result.score += 35
        result.signals.append(
            "multiple_identity_sources"
        )

    elif len(source_names) == 1:
        result.score += 10

    # --------------------------------------------------------------
    # CRITICAL:
    # Do NOT mark verified just because the number appears somewhere.
    # --------------------------------------------------------------

    strong_profile_signals = {
        "profile_url_exact_id",
        "final_url_exact_profile_id",
        "canonical_exact_profile_id",
    }

    strong_count = len(
        strong_profile_signals.intersection(
            result.signals
        )
    )

    if strong_count >= 1 and (
        "explicit_identity_field"
        in result.signals
    ):
        result.verified = True

    if (
        "canonical_exact_profile_id"
        in result.signals
        and (
            "final_url_exact_profile_id"
            in result.signals
            or "multiple_identity_sources"
            in result.signals
        )
    ):
        result.verified = True

    return result


# ======================================================================
# CANDIDATE SCORING
# ======================================================================

def candidate_user_scores(
    graph: EvidenceGraph,
) -> Dict[str, float]:

    scores: Dict[str, float] = {}

    for value in graph.values(
        "USER_CANDIDATE"
    ):

        items = graph.for_value(
            value
        )

        # ----------------------------------------------------------
        # Group by source.
        # Same source must not inflate indefinitely.
        # ----------------------------------------------------------

        by_source: Dict[
            str,
            List[Evidence]
        ] = {}

        for item in items:

            if item.role != "USER_CANDIDATE":
                continue

            by_source.setdefault(
                item.source,
                [],
            ).append(item)

        score = 0.0

        for source, source_items in by_source.items():

            strongest = max(
                source_items,
                key=lambda x: x.weight,
            )

            # Source cap.
            contribution = min(
                strongest.weight,
                150,
            )

            # Multiple independent paths from the same source
            # give only a small bonus.
            if len(source_items) >= 2:
                contribution += min(
                    12,
                    (len(source_items) - 1) * 4,
                )

            score += contribution

        # ----------------------------------------------------------
        # Diversity bonus.
        # ----------------------------------------------------------

        sources = set(
            by_source
        )

        if len(sources) >= 2:
            score += 35

        if len(sources) >= 3:
            score += 35

        if len(sources) >= 4:
            score += 25

        # ----------------------------------------------------------
        # Verification bonus.
        # ----------------------------------------------------------

        if any(
            x.verified
            for x in items
        ):
            score += 150

        scores[value] = score

    return scores


# ======================================================================
# CONFLICT DETECTOR
# ======================================================================

@dataclass
class ConflictReport:
    conflicting_users: List[str] = field(
        default_factory=list
    )

    conflicting_pages: List[str] = field(
        default_factory=list
    )

    conflicting_groups: List[str] = field(
        default_factory=list
    )

    conflict: bool = False

    notes: List[str] = field(
        default_factory=list
    )


def detect_conflicts(
    graph: EvidenceGraph,
) -> ConflictReport:

    report = ConflictReport()

    user_scores = candidate_user_scores(
        graph
    )

    ranked_users = sorted(
        user_scores.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    strong_users = [
        uid
        for uid, score in ranked_users
        if score >= 180
    ]

    if len(strong_users) > 1:

        top_score = user_scores[
            strong_users[0]
        ]

        for uid in strong_users[1:]:

            score = user_scores[
                uid
            ]

            # Two similarly strong user identities
            # are a real conflict.
            if score >= top_score * 0.72:

                report.conflicting_users = (
                    strong_users
                )

                report.conflict = True

                report.notes.append(
                    "Multiple strong user identities"
                )

                break

    pages = graph.values(
        "PAGE"
    )

    groups = graph.values(
        "GROUP"
    )

    if len(pages) > 1:

        report.conflicting_pages = list(
            pages
        )

    if len(groups) > 1:

        report.conflicting_groups = list(
            groups
        )

    return report


# ======================================================================
# OBJECT ID SELECTION
# ======================================================================

def best_object_id(
    graph: EvidenceGraph,
    role: str,
) -> str:

    values = graph.values(
        role
    )

    if not values:
        return ""

    best_value = ""
    best_score = -1.0

    for value in values:

        items = graph.for_value(
            value
        )

        score = 0.0

        sources = set()

        for item in items:

            if item.role != role:
                continue

            sources.add(
                item.source
            )

            score += min(
                item.weight,
                150,
            )

        score += (
            min(
                4,
                len(sources),
            )
            * 20
        )

        if score > best_score:
            best_score = score
            best_value = value

    return best_value


# ======================================================================
# ENTITY CLASSIFICATION
# ======================================================================

def infer_entity_type(
    shape: URLShape,
) -> str:

    mapping = {
        "profile": "USER_OR_PROFILE",
        "group": "GROUP",
        "group_post": "GROUP_POST",
        "page": "PAGE",
        "post": "POST",
        "video": "VIDEO",
        "reel": "REEL",
        "photo": "PHOTO",
        "story": "STORY",
        "watch": "VIDEO",
        "permalink": "POST",
        "share": "SHARE",
        "entity": "ENTITY",
    }

    return mapping.get(
        shape.kind,
        "UNKNOWN",
    )


def infer_publisher_type(
    graph: EvidenceGraph,
    shape: URLShape,
) -> str:

    if shape.group_id:
        return "GROUP"

    if shape.page_id:
        return "PAGE"

    if graph.values(
        "GROUP"
    ):
        return "GROUP"

    if graph.values(
        "PAGE"
    ):
        return "PAGE"

    if graph.values(
        "USER_CANDIDATE"
    ):
        return "USER"

    return "UNKNOWN"


# ======================================================================
# RESULT
# ======================================================================

@dataclass
class ResolveResult:

    input_url: str

    shape: URLShape

    resolved_url: str = ""

    canonical_url: str = ""

    object_type: str = "UNKNOWN"

    publisher_type: str = "UNKNOWN"

    username: str = ""

    user_uid: str = ""

    page_uid: str = ""

    group_id: str = ""

    post_id: str = ""

    video_id: str = ""

    reel_id: str = ""

    photo_id: str = ""

    story_id: str = ""

    album_id: str = ""

    title: str = ""

    status: str = "NOT_VERIFIED"

    confidence: float = 0.0

    evidence: List[Evidence] = field(
        default_factory=list
    )

    notes: List[str] = field(
        default_factory=list
    )

    elapsed: float = 0.0


# ======================================================================
# IDENTITY VERIFIER
# ======================================================================

class IdentityVerifier:

    @staticmethod
    def verify_user(
        graph: EvidenceGraph,
        profile_verifications:
        List[ProfileVerification],
        conflict: ConflictReport,
    ) -> Tuple[str, float, List[str]]:

        notes = []

        scores = candidate_user_scores(
            graph
        )

        if not scores:
            return (
                "",
                0.0,
                [
                    "No user identity candidate"
                ],
            )

        ranked = sorted(
            scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        best_uid, best_score = ranked[0]

        # ----------------------------------------------------------
        # Hard conflict.
        # ----------------------------------------------------------

        if conflict.conflict:

            notes.append(
                "Strong user identity conflict"
            )

            return (
                "",
                0.0,
                notes,
            )

        # ----------------------------------------------------------
        # Profile verification is strongest.
        # ----------------------------------------------------------

        verified_profiles = [
            x
            for x in profile_verifications
            if x.verified
            and x.uid == best_uid
        ]

        if verified_profiles:

            notes.append(
                "Profile identity independently verified"
            )

            confidence = min(
                99.9,
                92.0
                + min(
                    7.0,
                    len(
                        verified_profiles
                    ) * 2,
                ),
            )

            return (
                best_uid,
                confidence,
                notes,
            )

        # ----------------------------------------------------------
        # Explicit profile.php?id=<uid>
        # plus another independent identity source.
        # ----------------------------------------------------------

        items = graph.for_value(
            best_uid
        )

        explicit_profile = any(
            x.source
            == "url_profile_id"
            for x in items
        )

        independent_sources = {
            x.source
            for x in items
            if x.independent
        }

        if (
            explicit_profile
            and len(independent_sources) >= 2
        ):

            notes.append(
                "Explicit profile ID corroborated "
                "by independent source"
            )

            return (
                best_uid,
                min(
                    98.0,
                    88.0
                    + len(
                        independent_sources
                    ) * 2,
                ),
                notes,
            )

        # ----------------------------------------------------------
        # Strong semantic identity from multiple sources.
        # ----------------------------------------------------------

        strong_semantic_sources = {
            x.source
            for x in items
            if (
                x.weight >= 100
                and x.independent
                and x.role
                == "USER_CANDIDATE"
            )
        }

        if (
            len(strong_semantic_sources)
            >= 3
        ):

            notes.append(
                "Three or more independent "
                "strong identity sources"
            )

            return (
                best_uid,
                min(
                    96.0,
                    82.0
                    + len(
                        strong_semantic_sources
                    ) * 3,
                ),
                notes,
            )

        # ----------------------------------------------------------
        # Score alone is NEVER enough.
        # ----------------------------------------------------------

        notes.append(
            "Candidate exists but identity "
            "correlation is insufficient"
        )

        return (
            "",
            0.0,
            notes,
        )


# ======================================================================
# MAIN RESOLVER
# ======================================================================

class FacebookResolver:

    def __init__(self):

        self.fetcher = FetchEngine()

    async def resolve(
        self,
        input_url: str,
    ) -> ResolveResult:

        started = time.monotonic()

        normalized = normalize_url(
            input_url
        )

        shape = classify_url(
            normalized
        )

        result = ResolveResult(
            input_url=input_url,
            shape=shape,
        )

        if not normalized:

            result.notes.append(
                "Invalid Facebook URL"
            )

            return result

        graph = EvidenceGraph()

        # ----------------------------------------------------------
        # Step 1: URL structural evidence
        # ----------------------------------------------------------

        add_structural_evidence(
            graph,
            shape,
            normalized,
        )

        # ----------------------------------------------------------
        # Step 2: Fetch
        # ----------------------------------------------------------

        snapshot = await self.fetcher.fetch(
            normalized
        )

        result.resolved_url = (
            snapshot.final_url
            or normalized
        )

        result.canonical_url = (
            snapshot.canonical
            or ""
        )

        result.title = snapshot.title

        if snapshot.error:

            result.notes.append(
                snapshot.error
            )

        if not snapshot.html:

            result.notes.append(
                "No public HTML returned"
            )

            result.evidence = graph.items

            result.elapsed = (
                time.monotonic()
                - started
            )

            return result

        # ----------------------------------------------------------
        # Step 3: Final URL structural evidence
        # ----------------------------------------------------------

        final_shape = classify_url(
            snapshot.final_url
        )

        add_structural_evidence(
            graph,
            final_shape,
            snapshot.final_url,
        )

        # ----------------------------------------------------------
        # Step 4: Canonical structural evidence
        # ----------------------------------------------------------

        if snapshot.canonical:

            canonical_shape = classify_url(
                snapshot.canonical
            )

            add_structural_evidence(
                graph,
                canonical_shape,
                snapshot.canonical,
            )

        # ----------------------------------------------------------
        # Step 5: Meta
        # ----------------------------------------------------------

        collect_meta_evidence(
            graph,
            snapshot,
        )

        # ----------------------------------------------------------
        # Step 6: HTML semantic fields
        # ----------------------------------------------------------

        collect_semantic_evidence(
            graph,
            snapshot.html,
            "html_semantic",
            snapshot.final_url,
        )

        # ----------------------------------------------------------
        # Step 7: Embedded JSON
        # ----------------------------------------------------------

        collect_json_semantic_evidence(
            graph,
            snapshot.embedded_json,
            "embedded_json",
            snapshot.final_url,
        )

        # ----------------------------------------------------------
        # Step 8: JSON-LD
        # ----------------------------------------------------------

        collect_json_semantic_evidence(
            graph,
            snapshot.jsonld,
            "jsonld",
            snapshot.final_url,
        )

        collect_jsonld_identity_evidence(
            graph,
            snapshot.jsonld,
            snapshot.final_url,
        )

        # ----------------------------------------------------------
        # Step 9: Discover all Facebook URLs
        # ----------------------------------------------------------

        discovered = (
            discover_facebook_urls(
                snapshot
            )
        )

        # ----------------------------------------------------------
        # Step 10: Analyze discovered URLs.
        # ----------------------------------------------------------

        for discovered_url in discovered:

            discovered_shape = classify_url(
                discovered_url
            )

            add_structural_evidence(
                graph,
                discovered_shape,
                discovered_url,
            )

        # ----------------------------------------------------------
        # Step 11: Redirect evidence
        # ----------------------------------------------------------

        for redirect_url in snapshot.redirects:

            redirect_shape = classify_url(
                redirect_url
            )

            add_structural_evidence(
                graph,
                redirect_shape,
                redirect_url,
            )

        # ----------------------------------------------------------
        # Step 12: Profile discovery
        # ----------------------------------------------------------

        profile_urls = extract_profile_urls(
            snapshot
        )

        # Also inspect discovered Facebook URLs.
        for discovered_url in discovered:

            discovered_shape = classify_url(
                discovered_url
            )

            if (
                discovered_shape.kind
                == "profile"
            ):

                if (
                    discovered_url
                    not in profile_urls
                ):
                    profile_urls.append(
                        discovered_url
                    )

                if (
                    len(profile_urls)
                    >= MAX_PROFILE_CHECKS
                ):
                    break

        # ----------------------------------------------------------
        # Step 13: Determine candidates.
        # ----------------------------------------------------------

        scores = candidate_user_scores(
            graph
        )

        ranked = sorted(
            scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        profile_verifications = []

        # Verify only the strongest plausible candidates.
        candidate_uids = [
            uid
            for uid, score in ranked[:MAX_PROFILE_CHECKS]
            if score >= 80
        ]

        # ----------------------------------------------------------
        # Profile URL candidates.
        # ----------------------------------------------------------

        profile_uid_pairs = []

        for profile_url in profile_urls:

            profile_shape = classify_url(
                profile_url
            )

            if (
                profile_shape.kind
                == "profile"
                and profile_shape.numeric_path_id
            ):

                profile_uid_pairs.append(
                    (
                        profile_url,
                        profile_shape.numeric_path_id,
                    )
                )

        # Add explicit candidates to verification pool.
        for _, uid in profile_uid_pairs:

            if uid not in candidate_uids:

                candidate_uids.append(
                    uid
                )

            if (
                len(candidate_uids)
                >= MAX_PROFILE_CHECKS
            ):
                break

        # ----------------------------------------------------------
        # Verify profile URLs first.
        # ----------------------------------------------------------

        checked = set()

        for profile_url, uid in profile_uid_pairs:

            key = (
                profile_url,
                uid,
            )

            if key in checked:
                continue

            checked.add(key)

            verification = (
                await verify_profile_url(
                    self.fetcher,
                    profile_url,
                    uid,
                )
            )

            profile_verifications.append(
                verification
            )

        # ----------------------------------------------------------
        # If candidate has no discovered profile URL,
        # try canonical profile.php?id=<candidate>.
        #
        # This is still public HTTP only.
        # ----------------------------------------------------------

        for uid in candidate_uids:

            if (
                len(
                    profile_verifications
                )
                >= MAX_PROFILE_CHECKS
            ):
                break

            if any(
                x.uid == uid
                for x in profile_verifications
            ):
                continue

            profile_url = (
                "https://www.facebook.com/"
                f"profile.php?id={quote(uid)}"
            )

            verification = (
                await verify_profile_url(
                    self.fetcher,
                    profile_url,
                    uid,
                )
            )

            profile_verifications.append(
                verification
            )

        # ----------------------------------------------------------
        # Step 14: Conflict detection
        # ----------------------------------------------------------

        conflict = detect_conflicts(
            graph
        )

        # ----------------------------------------------------------
        # Step 15: Identity verification
        # ----------------------------------------------------------

        (
            verified_uid,
            confidence,
            identity_notes,
        ) = IdentityVerifier.verify_user(
            graph,
            profile_verifications,
            conflict,
        )

        result.user_uid = verified_uid

        result.confidence = confidence

        result.notes.extend(
            identity_notes
        )

        # ----------------------------------------------------------
        # Step 16: Entity/object IDs
        # ----------------------------------------------------------

        result.page_uid = (
            best_object_id(
                graph,
                "PAGE",
            )
        )

        result.group_id = (
            best_object_id(
                graph,
                "GROUP",
            )
        )

        result.post_id = (
            best_object_id(
                graph,
                "POST",
            )
        )

        result.video_id = (
            best_object_id(
                graph,
                "VIDEO",
            )
        )

        result.reel_id = (
            best_object_id(
                graph,
                "REEL",
            )
        )

        result.photo_id = (
            best_object_id(
                graph,
                "PHOTO",
            )
        )

        result.story_id = (
            best_object_id(
                graph,
                "STORY",
            )
        )

        result.album_id = (
            best_object_id(
                graph,
                "ALBUM",
            )
        )

        # ----------------------------------------------------------
        # Step 17: Entity classification
        # ----------------------------------------------------------

        result.object_type = (
            infer_entity_type(
                final_shape
                if final_shape.kind
                != "unknown"
                else shape
            )
        )

        result.publisher_type = (
            infer_publisher_type(
                graph,
                final_shape,
            )
        )

        result.username = (
            final_shape.username
            or shape.username
        )

        # ----------------------------------------------------------
        # Step 18: Final status
        # ----------------------------------------------------------

        if verified_uid:

            result.status = "VERIFIED"

        else:

            result.status = "NOT_VERIFIED"

            # Very important:
            # Never leak a weak candidate into user_uid.
            result.user_uid = ""

            result.notes.append(
                "UID withheld because public evidence "
                "does not independently verify identity"
            )

        # ----------------------------------------------------------
        # Evidence
        # ----------------------------------------------------------

        result.evidence = sorted(
            graph.items,
            key=lambda x: (
                x.weight,
                x.confidence,
            ),
            reverse=True,
        )

        result.elapsed = (
            time.monotonic()
            - started
        )

        return result


# ======================================================================
# TELEGRAM HTML ESCAPE
# ======================================================================

def tg_escape(
    value: Any,
) -> str:

    value = "" if value is None else str(
        value
    )

    return (
        value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def truncate(
    value: Any,
    limit: int,
) -> str:

    value = str(
        value or ""
    )

    if len(value) <= limit:
        return value

    return (
        value[:limit - 3]
        + "..."
    )


# ======================================================================
# RESULT FORMATTER
# ======================================================================

def format_result(
    result: ResolveResult,
) -> str:

    lines = []

    status_icon = (
        "✅"
        if result.status == "VERIFIED"
        else "⚠️"
    )

    lines.append(
        f"{status_icon} "
        "<b>FACEBOOK FORENSIC RESOLVER</b>"
    )

    lines.append("")

    lines.append(
        "<b>STATUS:</b> "
        f"{tg_escape(result.status)}"
    )

    lines.append(
        "<b>CONFIDENCE:</b> "
        f"{result.confidence:.1f}%"
    )

    lines.append(
        "<b>TYPE:</b> "
        f"{tg_escape(result.object_type)}"
    )

    lines.append(
        "<b>PUBLISHER:</b> "
        f"{tg_escape(result.publisher_type)}"
    )

    lines.append("")

    if result.user_uid:

        lines.append(
            "<b>USER UID:</b> "
            f"<code>{tg_escape(result.user_uid)}</code>"
        )

    else:

        lines.append(
            "<b>USER UID:</b> "
            "<code>NOT VERIFIED</code>"
        )

    if result.username:

        lines.append(
            "<b>USERNAME:</b> "
            f"{tg_escape(result.username)}"
        )

    if result.page_uid:

        lines.append(
            "<b>PAGE ID:</b> "
            f"<code>{tg_escape(result.page_uid)}</code>"
        )

    if result.group_id:

        lines.append(
            "<b>GROUP ID:</b> "
            f"<code>{tg_escape(result.group_id)}</code>"
        )

    if result.post_id:

        lines.append(
            "<b>POST ID:</b> "
            f"<code>{tg_escape(result.post_id)}</code>"
        )

    if result.video_id:

        lines.append(
            "<b>VIDEO ID:</b> "
            f"<code>{tg_escape(result.video_id)}</code>"
        )

    if result.reel_id:

        lines.append(
            "<b>REEL ID:</b> "
            f"<code>{tg_escape(result.reel_id)}</code>"
        )

    if result.photo_id:

        lines.append(
            "<b>PHOTO ID:</b> "
            f"<code>{tg_escape(result.photo_id)}</code>"
        )

    if result.story_id:

        lines.append(
            "<b>STORY ID:</b> "
            f"<code>{tg_escape(result.story_id)}</code>"
        )

    if result.album_id:

        lines.append(
            "<b>ALBUM ID:</b> "
            f"<code>{tg_escape(result.album_id)}</code>"
        )

    if result.title:

        lines.append("")

        lines.append(
            "<b>TITLE:</b> "
            f"{tg_escape(truncate(result.title, 350))}"
        )

    if result.resolved_url:

        lines.append("")

        lines.append(
            "<b>RESOLVED:</b> "
            f"<code>{tg_escape(truncate(result.resolved_url, 500))}</code>"
        )

    if result.canonical_url:

        lines.append(
            "<b>CANONICAL:</b> "
            f"<code>{tg_escape(truncate(result.canonical_url, 500))}</code>"
        )

    if result.notes:

        lines.append("")

        lines.append(
            "<b>FORENSIC NOTES:</b>"
        )

        unique_notes = []

        for note in result.notes:

            if note not in unique_notes:
                unique_notes.append(note)

        for note in unique_notes[:8]:

            lines.append(
                "• "
                + tg_escape(
                    truncate(
                        note,
                        350,
                    )
                )
            )

    lines.append("")

    lines.append(
        "<b>TIME:</b> "
        f"{result.elapsed:.2f}s"
    )

    return "\n".join(lines)


# ======================================================================
# EVIDENCE DETAIL
# ======================================================================

def format_evidence(
    result: ResolveResult,
    limit: int = 12,
) -> str:

    lines = [
        "",
        "<b>TOP EVIDENCE</b>",
    ]

    count = 0

    for item in result.evidence:

        if count >= limit:
            break

        if item.role == "ROUTE_ENTITY":
            continue

        lines.append(
            "• "
            f"<code>{tg_escape(item.value)}</code>"
            " — "
            f"{tg_escape(item.role)}"
            " — "
            f"{tg_escape(item.source)}"
            f" ({item.weight:.0f})"
        )

        if item.path:

            lines.append(
                "  PATH: "
                f"<code>{tg_escape(truncate(item.path, 160))}</code>"
            )

        count += 1

    return "\n".join(lines)


# ======================================================================
# TELEGRAM SESSION
# ======================================================================

_ACTIVE_SESSIONS: Dict[
    int,
    float,
] = {}


async def wait_for_next_message(
    client,
    chat_id: int,
    timeout: int = 120,
):

    loop = asyncio.get_running_loop()

    future = loop.create_future()

    async def handler(event):

        if event.chat_id != chat_id:
            return

        text = event.raw_text or ""

        if not text.strip():
            return

        if text.startswith("/"):
            return

        if not future.done():
            future.set_result(
                text
            )

    client.add_event_handler(
        handler,
        events.NewMessage(
            chats=chat_id
        ),
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
            handler,
            events.NewMessage(
                chats=chat_id
            ),
        )


# ======================================================================
# COMMAND HANDLER
# ======================================================================

async def _resolve_text(
    event,
    text: str,
):

    urls = extract_facebook_urls(
        text
    )

    if not urls:

        await event.reply(
            "❌ <b>Không tìm thấy URL Facebook hợp lệ.</b>\n\n"
            "Hãy gửi link Facebook công khai.",
            parse_mode="html",
        )

        return

    if len(urls) > MAX_INPUT_URLS:
        urls = urls[
            :MAX_INPUT_URLS
        ]

    resolver = FacebookResolver()

    messages = []

    for index, url in enumerate(
        urls,
        start=1,
    ):

        try:

            result = await resolver.resolve(
                url
            )

            block = format_result(
                result
            )

            # Include evidence only when useful.
            if result.status == "VERIFIED":
                block += format_evidence(
                    result
                )

            if len(urls) > 1:

                block = (
                    f"<b>#{index}</b>\n"
                    + block
                )

            messages.append(
                block
            )

        except Exception as exc:

            log.exception(
                "Facebook resolver error"
            )

            messages.append(
                f"<b>#{index}</b>\n"
                "❌ Resolver error: "
                f"<code>{tg_escape(exc)}</code>"
            )

    output = "\n\n"
    output += "\n\n".join(
        messages
    )

    # Telegram message limit.
    if len(output) > 4000:

        for i in range(
            0,
            len(output),
            3900,
        ):

            await event.reply(
                output[i:i + 3900],
                parse_mode="html",
            )

    else:

        await event.reply(
            output,
            parse_mode="html",
        )


async def _handle_getuidfb(
    event,
):

    sender_id = event.sender_id

    now = time.time()

    _ACTIVE_SESSIONS[
        sender_id
    ] = now

    try:

        existing = (
            event.raw_text
            or ""
        )

        parts = existing.split(
            maxsplit=1
        )

        if len(parts) > 1:

            text = parts[1].strip()

            if text:

                await _resolve_text(
                    event,
                    text,
                )

                return

        await event.reply(
            "🔎 <b>FACEBOOK UID FORENSIC</b>\n\n"
            "Gửi link Facebook công khai.\n"
            "Có thể gửi nhiều link trong một tin nhắn.\n\n"
            "Ví dụ:\n"
            "<code>facebook.com/...</code>\n\n"
            "⏱ Timeout: 120 giây",
            parse_mode="html",
        )

        text = await wait_for_next_message(
            event.client,
            event.chat_id,
            timeout=120,
        )

        if not text:

            await event.reply(
                "⌛ Hết thời gian chờ.",
                parse_mode="html",
            )

            return

        await _resolve_text(
            event,
            text,
        )

    finally:

        _ACTIVE_SESSIONS.pop(
            sender_id,
            None,
        )


async def _handle_getuidfb_inline(
    event,
):

    text = (
        event.raw_text
        or ""
    )

    parts = text.split(
        maxsplit=1
    )

    if len(parts) < 2:

        await event.reply(
            "Dùng:\n"
            "<code>/getuidfb URL</code>",
            parse_mode="html",
        )

        return

    await _resolve_text(
        event,
        parts[1],
    )


# ======================================================================
# REGISTER
# ======================================================================

def register(
    bot,
    notify_bot=None,
):

    """
    REQUIRED BY:
        commands.__init__.py

    Expected loader:
        module.register(bot, notify_bot)
    """

    bot.add_event_handler(
        _handle_getuidfb_inline,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?\s+.+$"
        ),
    )

    bot.add_event_handler(
        _handle_getuidfb,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?$"
        ),
    )

    log.info(
        "Registered /getuidfb V30 FORENSIC"
    )


# ======================================================================
# COMMAND INFO
# ======================================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": (
        "Facebook public URL forensic UID/entity resolver"
    ),
    "usage": (
        "/getuidfb [Facebook URL]"
    ),
    "category": "Facebook",
    "public_only": True,
    "http_only": True,
    "login_required": False,
    "cookie_required": False,
    "access_token_required": False,
}


# ======================================================================
# SELF TEST
# ======================================================================

def self_test():

    tests = [
        (
            "https://www.facebook.com/profile.php?id=123456789012345",
            "profile",
            "123456789012345",
        ),
        (
            "https://www.facebook.com/example/videos/1234567890123456/",
            "video",
            "1234567890123456",
        ),
        (
            "https://www.facebook.com/61592487939720/videos/1671583863938536/",
            "video",
            "1671583863938536",
        ),
        (
            "https://www.facebook.com/groups/123456789012345/posts/987654321012345/",
            "group_post",
            "987654321012345",
        ),
        (
            "https://www.facebook.com/reel/1234567890123456/",
            "reel",
            "1234567890123456",
        ),
        (
            "https://www.facebook.com/photo.php?fbid=1234567890123456",
            "photo",
            "1234567890123456",
        ),
    ]

    print(
        "=" * 70
    )

    print(
        "FACEBOOK RESOLVER V30 SELF TEST"
    )

    print(
        "=" * 70
    )

    for url, expected_kind, expected_id in tests:

        normalized = normalize_url(
            url
        )

        shape = classify_url(
            normalized
        )

        object_ids = [
            shape.post_id,
            shape.video_id,
            shape.reel_id,
            shape.photo_id,
            shape.story_id,
        ]

        found = next(
            (
                x
                for x in object_ids
                if x
            ),
            "",
        )

        passed = (
            shape.kind
            == expected_kind
            and found
            == expected_id
        )

        print(
            (
                "PASS"
                if passed
                else "FAIL"
            ),
            "|",
            expected_kind,
            "|",
            url,
        )

        print(
            "   kind:",
            shape.kind,
        )

        print(
            "   route_entity_id:",
            shape.route_entity_id,
        )

        print(
            "   object_id:",
            found,
        )

    # URL scanner test.
    concatenated = (
        "abc "
        "https://www.facebook.com/reel/1234567890123456/"
        "https://www.facebook.com/profile.php?id=123456789012345"
        " xyz"
    )

    urls = extract_facebook_urls(
        concatenated
    )

    print()

    print(
        "URL SCANNER:",
        len(urls),
        "URLs"
    )

    for url in urls:
        print(
            " -",
            url,
        )

    print()

    # Evidence graph test.
    graph = EvidenceGraph()

    add_evidence(
        graph,
        "123456789012345",
        "USER_CANDIDATE",
        "url_profile_id",
        145,
    )

    add_evidence(
        graph,
        "123456789012345",
        "USER_CANDIDATE",
        "embedded_json",
        120,
    )

    add_evidence(
        graph,
        "123456789012345",
        "USER_CANDIDATE",
        "jsonld",
        110,
    )

    scores = candidate_user_scores(
        graph
    )

    print(
        "EVIDENCE SCORE:",
        scores,
    )

    print(
        "=" * 70
    )


# ======================================================================
# CLI
# ======================================================================

if __name__ == "__main__":

    import sys

    if (
        "--self-test"
        in sys.argv
    ):

        self_test()

    else:

        print(
            "Facebook Resolver V30"
        )

        print(
            "Telegram integration module."
        )

        print(
            "Run:"
        )

        print(
            "  python commands/getuidfb.py --self-test"
        )