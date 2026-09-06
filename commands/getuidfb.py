#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
======================================================================
 FACEBOOK IDENTITY + CONTENT RESOLVER V31
======================================================================

PUBLIC FACEBOOK CONTENT ONLY
HTTP ONLY

NO:
    Playwright
    Selenium
    Chromium
    Cookie
    Facebook Login
    Facebook Access Token
    Graph API Authentication

GOAL
----
Resolve public Facebook URLs into:

    USER
        name
        username
        UID
        profile URL
        avatar
        bio

    PAGE
        name
        page ID
        URL

    GROUP
        name
        group ID
        URL

    CONTENT
        post ID
        video ID
        reel ID
        photo ID
        story ID
        title
        canonical URL

SECURITY / ACCURACY
-------------------
Never assume:

    route entity ID == user UID
    object ID == user UID
    pfbid == user UID

UID is returned only after identity correlation.

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

log = logging.getLogger(
    "facebook_identity_resolver"
)


# ======================================================================
# CONFIG
# ======================================================================

REQUEST_TIMEOUT = 12

MAX_HTML_BYTES = 12 * 1024 * 1024

MAX_INPUT_URLS = 20

MAX_DISCOVERED_URLS = 120

MAX_PROFILE_CHECKS = 5

MAX_EVIDENCE_DISPLAY = 10

PROFILE_VERIFY_THRESHOLD = 70

FETCH_DELAY = 0.15


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
}

FB_WATCH_HOSTS = {
    "fb.watch",
}


# ======================================================================
# USER AGENTS
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

NUMERIC_ID_RE = re.compile(
    r"^\d{5,21}$"
)

LONG_NUMERIC_RE = re.compile(
    r"^\d{8,21}$"
)

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
    /[^\s<>"']*
    |
    (?:
        https?://
    )?
    fb\.watch
    /[^\s<>"']*
    """
)


# ======================================================================
# SEMANTIC WEIGHTS
# ======================================================================

USER_FIELD_WEIGHTS = {
    "user_id": 125,
    "userid": 125,

    "profile_id": 125,
    "profileid": 125,

    "owner_id": 115,
    "ownerid": 115,

    "publisher_id": 112,
    "publisherid": 112,

    "author_id": 110,
    "authorid": 110,

    "creator_id": 112,
    "creatorid": 112,

    "from_id": 110,
    "fromid": 110,

    "actor_id": 100,
    "actorid": 100,

    "page_owner_id": 105,

    "profile.uid": 125,
    "profile.id": 125,

    "owner.id": 115,
    "publisher.id": 112,
    "author.id": 110,
    "creator.id": 112,

    "from.id": 110,
    "actor.id": 100,
}


OBJECT_FIELD_WEIGHTS = {
    "post_id": 125,
    "postid": 125,

    "story_fbid": 125,
    "storyfbid": 125,

    "video_id": 125,
    "videoid": 125,

    "reel_id": 125,
    "reelid": 125,

    "photo_id": 122,
    "photoid": 122,

    "media_fbid": 115,
    "mediafbid": 115,

    "album_id": 108,
    "albumid": 108,

    "group_id": 130,
    "groupid": 130,

    "page_id": 130,
    "pageid": 130,
}


# ======================================================================
# BASIC HELPERS
# ======================================================================

def clean_text(
    value: Any,
) -> str:

    if value is None:
        return ""

    value = html_lib.unescape(
        str(value)
    )

    value = value.replace(
        "\x00",
        " ",
    )

    return value.strip()


def compact_text(
    value: Any,
    limit: int = 500,
) -> str:

    value = clean_text(
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    if len(value) > limit:
        return value[:limit - 3] + "..."

    return value


def is_numeric_id(
    value: Any,
) -> bool:

    if value is None:
        return False

    return bool(
        NUMERIC_ID_RE.fullmatch(
            str(value).strip()
        )
    )


def is_long_numeric(
    value: Any,
) -> bool:

    if value is None:
        return False

    return bool(
        LONG_NUMERIC_RE.fullmatch(
            str(value).strip()
        )
    )


def normalize_host(
    host: str,
) -> str:

    return (
        host
        or ""
    ).lower().strip().rstrip(".")


def is_facebook_host(
    host: str,
) -> bool:

    host = normalize_host(
        host
    )

    return (
        host in FACEBOOK_HOSTS
        or host in FB_WATCH_HOSTS
    )


# ======================================================================
# URL NORMALIZATION
# ======================================================================

def normalize_url(
    url: str,
) -> str:

    if not url:
        return ""

    url = clean_text(
        url
    )

    url = url.strip(
        " \t\r\n"
        "\"'<>[]{}"
        "，。！？；：、"
    )

    if not re.match(
        r"^https?://",
        url,
        re.I,
    ):

        lower = url.lower()

        if (
            lower.startswith(
                "facebook.com/"
            )
            or lower.startswith(
                "www.facebook.com/"
            )
            or lower.startswith(
                "m.facebook.com/"
            )
            or lower.startswith(
                "mbasic.facebook.com/"
            )
            or lower.startswith(
                "fb.watch/"
            )
        ):
            url = "https://" + url

        else:
            return ""

    try:

        p = urlparse(
            url
        )

        host = normalize_host(
            p.hostname or ""
        )

        if not is_facebook_host(
            host
        ):
            return ""

        path = re.sub(
            r"/{2,}",
            "/",
            p.path or "/",
        )

        path = path.rstrip(
            " \t\r\n"
        )

        while path.endswith(
            (".", ",", ";", ":", "!", "?", ")")
        ):
            path = path[:-1]

        if not path:
            path = "/"

        pairs = parse_qsl(
            p.query,
            keep_blank_values=True,
        )

        filtered = []

        for key, value in pairs:

            if key.lower() in {
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "utm_term",
                "utm_content",
                "fbclid",
                "refsrc",
            }:
                continue

            filtered.append(
                (key, value)
            )

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
# FACEBOOK URL SCANNER
# ======================================================================

def extract_facebook_urls(
    text: str,
) -> List[str]:

    if not text:
        return []

    text = html_lib.unescape(
        text
    )

    text = text.replace(
        "\\/",
        "/",
    )

    results = []

    seen = set()

    def add(
        raw: str,
    ):

        normalized = normalize_url(
            raw
        )

        if not normalized:
            return

        if normalized in seen:
            return

        seen.add(
            normalized
        )

        results.append(
            normalized
        )

    for match in FACEBOOK_URL_RE.finditer(
        text
    ):

        raw = match.group(
            0
        )

        raw = raw.rstrip(
            ".,;:!?)]}>'\""
        )

        add(
            raw
        )

        if len(results) >= MAX_INPUT_URLS:
            break

    # Secondary scan for concatenated URLs.
    if len(results) < MAX_INPUT_URLS:

        lower = text.lower()

        markers = [
            "https://facebook.com/",
            "https://www.facebook.com/",
            "https://m.facebook.com/",
            "http://facebook.com/",
            "http://www.facebook.com/",
            "facebook.com/",
            "www.facebook.com/",
            "m.facebook.com/",
            "mbasic.facebook.com/",
            "https://fb.watch/",
            "fb.watch/",
        ]

        for marker in markers:

            start = 0

            while True:

                idx = lower.find(
                    marker.lower(),
                    start,
                )

                if idx < 0:
                    break

                candidate = re.split(
                    r"""[\s<>"']""",
                    text[idx:],
                    maxsplit=1,
                )[0]

                candidate = candidate.rstrip(
                    ".,;:!?)]}>'\""
                )

                add(
                    candidate
                )

                start = (
                    idx
                    + len(marker)
                )

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

    route_entity_id: str = ""

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
# QUERY
# ======================================================================

def query_value(
    parsed,
    *names: str,
) -> str:

    pairs = parse_qsl(
        parsed.query,
        keep_blank_values=True,
    )

    mapping = {
        str(k).lower(): v
        for k, v in pairs
    }

    for name in names:

        value = mapping.get(
            name.lower()
        )

        if value:
            return clean_text(
                value
            )

    return ""


# ======================================================================
# CLASSIFY URL
# ======================================================================

def classify_url(
    url: str,
) -> URLShape:

    normalized = normalize_url(
        url
    )

    if not normalized:

        return URLShape(
            original=url,
            normalized="",
        )

    parsed = urlparse(
        normalized
    )

    host = normalize_host(
        parsed.hostname or ""
    )

    segments = [
        unquote(
            x
        ).strip()
        for x in parsed.path.split("/")
        if x.strip()
    ]

    lower = [
        x.lower()
        for x in segments
    ]

    shape = URLShape(
        original=url,
        normalized=normalized,
        host=host,
        path=parsed.path,
        query=parsed.query,
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

    if lower[:1] == [
        "profile.php"
    ]:

        uid = query_value(
            parsed,
            "id",
        )

        if is_numeric_id(uid):

            shape.kind = "profile"

            shape.route_entity_id = uid

        return shape

    # --------------------------------------------------------------
    # photo.php
    # --------------------------------------------------------------

    if lower[:1] == [
        "photo.php"
    ]:

        photo = query_value(
            parsed,
            "fbid",
            "photo_id",
            "id",
        )

        album = query_value(
            parsed,
            "set",
            "album_id",
        )

        if is_numeric_id(photo):
            shape.photo_id = photo

        if is_numeric_id(album):
            shape.album_id = album

        shape.kind = "photo"

        return shape

    # --------------------------------------------------------------
    # story.php
    # --------------------------------------------------------------

    if lower[:1] == [
        "story.php"
    ]:

        story = query_value(
            parsed,
            "story_fbid",
            "story_id",
            "id",
        )

        if is_numeric_id(story):
            shape.story_id = story

        shape.kind = "story"

        return shape

    # --------------------------------------------------------------
    # permalink.php
    # --------------------------------------------------------------

    if lower[:1] == [
        "permalink.php"
    ]:

        post = query_value(
            parsed,
            "story_fbid",
            "fbid",
            "id",
        )

        if is_numeric_id(post):
            shape.post_id = post

        shape.kind = "permalink"

        return shape

    # --------------------------------------------------------------
    # watch
    # --------------------------------------------------------------

    if lower[:1] == [
        "watch"
    ]:

        video = query_value(
            parsed,
            "v",
            "video_id",
        )

        if is_numeric_id(video):
            shape.video_id = video

        shape.kind = "video"

        return shape

    # --------------------------------------------------------------
    # reel
    # --------------------------------------------------------------

    if lower[:1] == [
        "reel"
    ]:

        if len(segments) >= 2:

            value = segments[1]

            if is_numeric_id(value):
                shape.reel_id = value

        shape.kind = "reel"

        return shape

    # --------------------------------------------------------------
    # reels
    # --------------------------------------------------------------

    if lower[:1] == [
        "reels"
    ]:

        if len(segments) >= 2:

            value = segments[1]

            if is_numeric_id(value):
                shape.reel_id = value

        shape.kind = "reel"

        return shape

    # --------------------------------------------------------------
    # groups
    # --------------------------------------------------------------

    if lower[:1] == [
        "groups"
    ]:

        if len(segments) >= 2:

            value = segments[1]

            if is_numeric_id(value):

                shape.group_id = value

                shape.route_entity_id = value

            else:

                shape.username = value

        if "posts" in lower:

            idx = lower.index(
                "posts"
            )

            if idx + 1 < len(segments):

                value = segments[
                    idx + 1
                ]

                if is_numeric_id(value):
                    shape.post_id = value

            shape.kind = "group_post"

        else:

            shape.kind = "group"

        return shape

    # --------------------------------------------------------------
    # pages
    # --------------------------------------------------------------

    if lower[:1] == [
        "pages"
    ]:

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
    # people
    # --------------------------------------------------------------

    if lower[:1] == [
        "people"
    ]:

        shape.kind = "profile"

        if len(segments) >= 2:
            shape.username = segments[1]

        if len(segments) >= 3:

            value = segments[2]

            if is_numeric_id(value):
                shape.route_entity_id = value

        return shape

    # --------------------------------------------------------------
    # share
    # --------------------------------------------------------------

    if lower[:1] == [
        "share"
    ]:

        shape.kind = "share"

        return shape

    # --------------------------------------------------------------
    # /p/
    # --------------------------------------------------------------

    if lower[:1] == [
        "p"
    ]:

        shape.kind = "post"

        if len(segments) >= 2:
            shape.opaque_token = segments[1]

        return shape

    # --------------------------------------------------------------
    # generic
    # --------------------------------------------------------------

    if not segments:

        shape.kind = "home"

        return shape

    first = segments[0]

    if is_numeric_id(first):

        # NEVER promote directly to UID.
        shape.route_entity_id = first

    else:

        shape.username = first

    for index, part in enumerate(
        lower
    ):

        if part == "posts":

            shape.kind = "post"

            if index + 1 < len(
                segments
            ):

                value = segments[
                    index + 1
                ]

                if is_numeric_id(value):
                    shape.post_id = value

            break

        if part == "videos":

            shape.kind = "video"

            if index + 1 < len(
                segments
            ):

                value = segments[
                    index + 1
                ]

                if is_numeric_id(value):
                    shape.video_id = value

            break

        if part == "reels":

            shape.kind = "reel"

            if index + 1 < len(
                segments
            ):

                value = segments[
                    index + 1
                ]

                if is_numeric_id(value):
                    shape.reel_id = value

            break

        if part == "photos":

            shape.kind = "photo"

            if index + 1 < len(
                segments
            ):

                value = segments[
                    index + 1
                ]

                if is_numeric_id(value):
                    shape.photo_id = value

            break

        if part == "stories":

            shape.kind = "story"

            if index + 1 < len(
                segments
            ):

                value = segments[
                    index + 1
                ]

                if is_numeric_id(value):
                    shape.story_id = value

            break

    else:

        if shape.username:
            shape.kind = "profile"

        elif shape.route_entity_id:
            shape.kind = "entity"

    return shape


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

class FacebookHTMLParser(
    HTMLParser
):

    def __init__(
        self,
    ):

        super().__init__(
            convert_charrefs=True
        )

        self.title_parts = []

        self.meta = {}

        self.links = []

        self.jsonld = []

        self.embedded_json = []

        self._title = False

        self._script = False

        self._script_type = ""

        self._script_buffer = []

    def handle_starttag(
        self,
        tag,
        attrs,
    ):

        tag = tag.lower()

        amap = {
            str(k).lower():
            (
                ""
                if v is None
                else str(v)
            )
            for k, v in attrs
        }

        if tag == "title":

            self._title = True

        elif tag == "meta":

            key = (
                amap.get("property")
                or amap.get("name")
                or amap.get("itemprop")
                or amap.get("data-name")
                or ""
            ).strip().lower()

            value = (
                amap.get("content")
                or ""
            ).strip()

            if key and value:

                self.meta[
                    key
                ] = value

        elif tag == "link":

            href = amap.get(
                "href"
            )

            if href:
                self.links.append(
                    href
                )

        elif tag == "script":

            self._script = True

            self._script_type = (
                amap.get(
                    "type",
                    "",
                )
                .lower()
                .strip()
            )

            self._script_buffer = []

    def handle_endtag(
        self,
        tag,
    ):

        tag = tag.lower()

        if tag == "title":

            self._title = False

        elif tag == "script":

            content = "".join(
                self._script_buffer
            ).strip()

            if content:

                self.parse_script(
                    content
                )

            self._script = False

            self._script_type = ""

            self._script_buffer = []

    def handle_data(
        self,
        data,
    ):

        if self._title:

            self.title_parts.append(
                data
            )

        if self._script:

            if sum(
                len(x)
                for x in self._script_buffer
            ) < 3 * 1024 * 1024:

                self._script_buffer.append(
                    data
                )

    def parse_script(
        self,
        content,
    ):

        if (
            "ld+json"
            in self._script_type
        ):

            try:

                self.jsonld.append(
                    json.loads(
                        content
                    )
                )

            except Exception:
                pass

            return

        if (
            "application/json"
            in self._script_type
        ):

            try:

                self.embedded_json.append(
                    json.loads(
                        content
                    )
                )

            except Exception:
                pass

            return

        semantic_fields = (
            '"user_id"',
            '"profile_id"',
            '"owner_id"',
            '"publisher_id"',
            '"author_id"',
            '"creator_id"',
            '"page_id"',
            '"group_id"',
            '"post_id"',
            '"video_id"',
            '"reel_id"',
        )

        if any(
            x in content
            for x in semantic_fields
        ):

            self.embedded_json.extend(
                extract_json_fragments(
                    content
                )
            )

    def title(
        self,
    ) -> str:

        return compact_text(
            "".join(
                self.title_parts
            ),
            500,
        )


# ======================================================================
# JSON FRAGMENTS
# ======================================================================

def extract_json_fragments(
    text: str,
    max_fragments: int = 50,
) -> List[Any]:

    if not text:
        return []

    results = []

    starts = [
        m.start()
        for m in re.finditer(
            r"\{",
            text
        )
    ]

    for start in starts[:700]:

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

                        results.append(
                            json.loads(
                                candidate
                            )
                        )

                    except Exception:
                        pass

                    break

        if len(results) >= max_fragments:
            break

    return results


# ======================================================================
# PARSE SNAPSHOT
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

    snapshot.title = (
        parser.title()
    )

    snapshot.meta = (
        parser.meta
    )

    snapshot.links = (
        parser.links
    )

    snapshot.jsonld = (
        parser.jsonld
    )

    snapshot.embedded_json = (
        parser.embedded_json
    )

    canonical_candidates = []

    for key, value in (
        snapshot.meta.items()
    ):

        if key in {
            "og:url",
            "al:web:url",
            "twitter:url",
            "canonical",
        }:

            canonical_candidates.append(
                value
            )

    for value in canonical_candidates:

        normalized = normalize_url(
            value
        )

        if normalized:

            snapshot.canonical = (
                normalized
            )

            break


# ======================================================================
# JSON WALK
# ======================================================================

def walk_json(
    value: Any,
    path: str = "",
    parent: Any = None,
):

    if isinstance(
        value,
        dict,
    ):

        for key, child in value.items():

            key = str(key)

            child_path = (
                f"{path}.{key}"
                if path
                else key
            )

            yield (
                child_path,
                child,
                key,
                value,
            )

            yield from walk_json(
                child,
                child_path,
                value,
            )

    elif isinstance(
        value,
        list,
    ):

        for index, child in enumerate(
            value
        ):

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
# KEY WEIGHT
# ======================================================================

def normalize_key(
    key: str,
) -> str:

    return (
        str(key or "")
        .lower()
        .strip()
        .replace(
            "-",
            "_",
        )
        .replace(
            " ",
            "_",
        )
    )


def field_weight(
    key: str,
) -> Tuple[str, float]:

    normalized = normalize_key(
        key
    )

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
        0,
    )


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

    path: str = ""

    key: str = ""

    neighbor: str = ""

    independent: bool = True

    verified: bool = False

    def fingerprint(
        self,
    ):

        return (
            self.value,
            self.role,
            self.source,
            self.path,
            self.key,
        )


class EvidenceGraph:

    def __init__(
        self,
    ):

        self.items = []

        self.seen = set()

    def add(
        self,
        item: Evidence,
    ):

        if not is_numeric_id(
            item.value
        ):
            return

        fp = item.fingerprint()

        if fp in self.seen:
            return

        self.seen.add(
            fp
        )

        self.items.append(
            item
        )

    def values(
        self,
        role=None,
    ) -> Set[str]:

        return {
            x.value
            for x in self.items
            if role is None
            or x.role == role
        }

    def for_value(
        self,
        value,
    ):

        return [
            x
            for x in self.items
            if x.value == value
        ]


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
    path: str = "",
    key: str = "",
    neighbor: str = "",
    independent: bool = True,
    verified: bool = False,
):

    value = clean_text(
        value
    )

    if not is_numeric_id(
        value
    ):
        return

    graph.add(
        Evidence(
            value=value,
            role=role,
            source=source,
            weight=weight,
            context=compact_text(
                context,
                900,
            ),
            url=url,
            path=path,
            key=key,
            neighbor=neighbor,
            independent=independent,
            verified=verified,
        )
    )


# ======================================================================
# STRUCTURAL EVIDENCE
# ======================================================================

def add_structural_evidence(
    graph: EvidenceGraph,
    shape: URLShape,
    url: str,
):

    if (
        shape.kind == "profile"
        and shape.route_entity_id
    ):

        add_evidence(
            graph,
            shape.route_entity_id,
            "USER_CANDIDATE",
            "url_profile_id",
            150,
            "Explicit profile.php?id",
            url,
            path="query.id",
            key="id",
        )

    if shape.group_id:

        add_evidence(
            graph,
            shape.group_id,
            "GROUP",
            "url_group_id",
            150,
            "Explicit group ID",
            url,
        )

    if shape.page_id:

        add_evidence(
            graph,
            shape.page_id,
            "PAGE",
            "url_page_id",
            150,
            "Explicit page ID",
            url,
        )

    if shape.route_entity_id:

        add_evidence(
            graph,
            shape.route_entity_id,
            "ROUTE_ENTITY",
            "url_route_entity",
            15,
            "Route entity only",
            url,
            independent=False,
        )

    if shape.post_id:

        add_evidence(
            graph,
            shape.post_id,
            "POST",
            "url_post_id",
            140,
            "Post object ID",
            url,
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
        )


# ======================================================================
# HTML SEMANTIC
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

    for match in SEMANTIC_REGEX.finditer(
        text[:MAX_HTML_BYTES]
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

        context = text[
            max(
                0,
                match.start() - 180,
            ):
            min(
                len(text),
                match.end() + 320,
            )
        ]

        add_evidence(
            graph,
            value,
            role,
            source,
            weight,
            context=context,
            url=url,
            path=key,
            key=key,
        )


# ======================================================================
# JSON EVIDENCE
# ======================================================================

def collect_json_semantic_evidence(
    graph: EvidenceGraph,
    objects: Iterable[Any],
    source: str,
    url: str,
):

    for obj in objects:

        for (
            path,
            value,
            key,
            parent,
        ) in walk_json(
            obj
        ):

            normalized = normalize_key(
                key
            )

            role, weight = field_weight(
                normalized
            )

            if (
                role
                and is_numeric_id(value)
            ):

                add_evidence(
                    graph,
                    value,
                    role,
                    source,
                    weight,
                    context=compact_text(
                        repr(parent),
                        1000,
                    ),
                    url=url,
                    path=path,
                    key=normalized,
                )


# ======================================================================
# JSON-LD
# ======================================================================

def collect_jsonld_evidence(
    graph: EvidenceGraph,
    objects: Iterable[Any],
    url: str,
):

    for obj in objects:

        for (
            path,
            value,
            key,
            parent,
        ) in walk_json(
            obj
        ):

            key_lower = normalize_key(
                key
            )

            # Direct identity IDs.
            if (
                key_lower in {
                    "id",
                    "identifier",
                    "user_id",
                    "profile_id",
                    "owner_id",
                    "creator_id",
                    "author_id",
                }
                and is_numeric_id(value)
            ):

                role, weight = field_weight(
                    key_lower
                )

                if not role:
                    role = (
                        "USER_CANDIDATE"
                    )
                    weight = 90

                add_evidence(
                    graph,
                    value,
                    role,
                    "jsonld_identity",
                    weight,
                    context=compact_text(
                        repr(parent),
                        900,
                    ),
                    url=url,
                    path=path,
                    key=key_lower,
                )

            if isinstance(
                value,
                str,
            ):

                normalized_url = (
                    normalize_url(
                        value
                    )
                )

                if normalized_url:

                    child = classify_url(
                        normalized_url
                    )

                    if (
                        child.kind
                        == "profile"
                        and child.route_entity_id
                    ):

                        add_evidence(
                            graph,
                            child.route_entity_id,
                            "USER_CANDIDATE",
                            "jsonld_profile_url",
                            130,
                            context=compact_text(
                                repr(parent),
                                900,
                            ),
                            url=url,
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
                key=key_lower,
                path=key_lower,
            )

        normalized = normalize_url(
            value
        )

        if normalized:

            child = classify_url(
                normalized
            )

            if (
                child.kind
                == "profile"
                and child.route_entity_id
            ):

                add_evidence(
                    graph,
                    child.route_entity_id,
                    "USER_CANDIDATE",
                    "meta_profile_url",
                    140,
                    context=f"{key}: {value}",
                    url=snapshot.final_url,
                    key=key_lower,
                    path=key_lower,
                )


# ======================================================================
# DISCOVER FACEBOOK URLS
# ======================================================================

def discover_facebook_urls(
    snapshot: Snapshot,
) -> List[str]:

    results = []

    seen = set()

    base = (
        snapshot.final_url
        or snapshot.requested_url
    )

    def add(
        value: str,
    ):

        normalized = normalize_url(
            value
        )

        if not normalized:
            return

        if normalized in seen:
            return

        seen.add(
            normalized
        )

        results.append(
            normalized
        )

    for link in snapshot.links:

        add(
            urljoin(
                base,
                link,
            )
        )

        if len(results) >= MAX_DISCOVERED_URLS:
            return results

    for value in snapshot.meta.values():

        if (
            "facebook.com"
            in value.lower()
            or "fb.watch"
            in value.lower()
        ):

            add(value)

    for match in FACEBOOK_URL_RE.finditer(
        snapshot.html
    ):

        add(
            match.group(
                0
            )
        )

        if len(results) >= MAX_DISCOVERED_URLS:
            break

    return results


# ======================================================================
# HTTP ENGINE
# ======================================================================

class FetchEngine:

    def __init__(
        self,
    ):

        self.session = (
            requests.Session()
        )

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

            response = self.session.get(
                url,
                headers={
                    "User-Agent":
                    user_agent,
                },
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
                    x.url
                )
                or x.url
                for x in response.history
            ]

            chunks = []

            total = 0

            for chunk in response.iter_content(
                chunk_size=65536,
                decode_unicode=False,
            ):

                if not chunk:
                    continue

                remaining = (
                    MAX_HTML_BYTES
                    - total
                )

                if remaining <= 0:
                    break

                chunk = chunk[
                    :remaining
                ]

                chunks.append(
                    chunk
                )

                total += len(
                    chunk
                )

                if total >= MAX_HTML_BYTES:
                    break

            encoding = (
                response.encoding
                or "utf-8"
            )

            response.close()

            raw = b"".join(
                chunks
            )

            try:

                snapshot.html = (
                    raw.decode(
                        encoding,
                        errors="replace",
                    )
                )

            except Exception:

                snapshot.html = (
                    raw.decode(
                        "utf-8",
                        errors="replace",
                    )
                )

            parse_snapshot(
                snapshot
            )

            return snapshot

        except Exception as exc:

            snapshot.error = (
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            return snapshot

    async def fetch(
        self,
        url: str,
    ) -> Snapshot:

        last = Snapshot(
            requested_url=url
        )

        for agent in USER_AGENTS:

            result = await asyncio.to_thread(
                self.fetch_sync,
                url,
                agent,
            )

            last = result

            if (
                result.html
                and result.status_code
            ):

                return result

            await asyncio.sleep(
                FETCH_DELAY
            )

        return last


# ======================================================================
# PROFILE INFORMATION
# ======================================================================

@dataclass
class ProfileInfo:

    uid: str = ""

    name: str = ""

    username: str = ""

    profile_url: str = ""

    avatar: str = ""

    bio: str = ""

    entity_type: str = "USER"

    verified: bool = False

    sources: Set[str] = field(
        default_factory=set
    )


# ======================================================================
# CONTENT INFORMATION
# ======================================================================

@dataclass
class ContentInfo:

    content_type: str = ""

    post_id: str = ""

    video_id: str = ""

    reel_id: str = ""

    photo_id: str = ""

    story_id: str = ""

    album_id: str = ""

    title: str = ""

    canonical_url: str = ""

    publisher_uid: str = ""


# ======================================================================
# RESOLVE RESULT
# ======================================================================

@dataclass
class ResolveResult:

    input_url: str

    shape: URLShape

    status: str = "NOT_VERIFIED"

    confidence: float = 0.0

    entity_type: str = "UNKNOWN"

    publisher_type: str = "UNKNOWN"

    profile: ProfileInfo = field(
        default_factory=ProfileInfo
    )

    content: ContentInfo = field(
        default_factory=ContentInfo
    )

    resolved_url: str = ""

    canonical_url: str = ""

    evidence: List[Evidence] = field(
        default_factory=list
    )

    notes: List[str] = field(
        default_factory=list
    )

    elapsed: float = 0.0


# ======================================================================
# PROFILE URL EXTRACTION
# ======================================================================

def extract_profile_urls(
    snapshot: Snapshot,
) -> List[str]:

    results = []

    seen = set()

    def add(
        value: str,
    ):

        normalized = normalize_url(
            value
        )

        if not normalized:
            return

        child = classify_url(
            normalized
        )

        if child.kind != "profile":
            return

        if normalized in seen:
            return

        seen.add(
            normalized
        )

        results.append(
            normalized
        )

    for link in snapshot.links:

        add(
            urljoin(
                snapshot.final_url,
                link,
            )
        )

        if len(results) >= MAX_PROFILE_CHECKS:
            break

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

        for (
            _,
            value,
            _key,
            _parent,
        ) in walk_json(
            obj
        ):

            if isinstance(
                value,
                str,
            ):

                add(value)

                if (
                    len(results)
                    >= MAX_PROFILE_CHECKS
                ):
                    return results

    return results


# ======================================================================
# PROFILE NAME / USERNAME EXTRACTION
# ======================================================================

def extract_profile_identity(
    snapshot: Snapshot,
    expected_uid: str = "",
) -> ProfileInfo:

    info = ProfileInfo()

    graph = EvidenceGraph()

    collect_semantic_evidence(
        graph,
        snapshot.html,
        "profile_html",
        snapshot.final_url,
    )

    collect_meta_evidence(
        graph,
        snapshot,
    )

    collect_json_semantic_evidence(
        graph,
        snapshot.embedded_json,
        "profile_json",
        snapshot.final_url,
    )

    collect_jsonld_evidence(
        graph,
        snapshot.jsonld,
        snapshot.final_url,
    )

    # --------------------------------------------------------------
    # UID
    # --------------------------------------------------------------

    if expected_uid:

        if any(
            x.value == expected_uid
            and x.role
            == "USER_CANDIDATE"
            for x in graph.items
        ):

            info.uid = expected_uid

    # --------------------------------------------------------------
    # Name candidates
    # --------------------------------------------------------------

    name_keys = [
        "og:title",
        "twitter:title",
        "title",
        "profile:name",
        "author",
        "og:site_name",
    ]

    for key in name_keys:

        value = snapshot.meta.get(
            key
        )

        if value:

            value = compact_text(
                value,
                250,
            )

            if (
                value
                and value.lower()
                not in {
                    "facebook",
                    "meta",
                }
            ):

                info.name = value

                break

    # --------------------------------------------------------------
    # Description / bio
    # --------------------------------------------------------------

    for key in (
        "og:description",
        "description",
        "twitter:description",
    ):

        value = snapshot.meta.get(
            key
        )

        if value:

            info.bio = compact_text(
                value,
                500,
            )

            break

    # --------------------------------------------------------------
    # Profile URL
    # --------------------------------------------------------------

    profile_urls = (
        extract_profile_urls(
            snapshot
        )
    )

    if profile_urls:

        info.profile_url = (
            clean_profile_url(
                profile_urls[0]
            )
        )

        shape = classify_url(
            profile_urls[0]
        )

        if shape.username:

            info.username = (
                shape.username
            )

        if (
            not info.uid
            and shape.route_entity_id
        ):

            info.uid = (
                shape.route_entity_id
            )

    # --------------------------------------------------------------
    # Canonical username
    # --------------------------------------------------------------

    if not info.username:

        candidate = (
            clean_profile_url(
                snapshot.canonical
            )
        )

        child = classify_url(
            candidate
        )

        if child.username:

            info.username = (
                child.username
            )

    # --------------------------------------------------------------
    # Avatar
    # --------------------------------------------------------------

    for key in (
        "og:image",
        "twitter:image",
        "twitter:image:src",
    ):

        value = snapshot.meta.get(
            key
        )

        if value:

            info.avatar = value.strip()

            break

    return info


# ======================================================================
# CLEAN PROFILE URL
# ======================================================================

def clean_profile_url(
    url: str,
) -> str:

    normalized = normalize_url(
        url
    )

    if not normalized:
        return ""

    shape = classify_url(
        normalized
    )

    if (
        shape.kind == "profile"
        and shape.username
    ):

        return (
            "https://www.facebook.com/"
            + quote(
                shape.username,
                safe="@._-",
            )
        )

    if (
        shape.kind == "profile"
        and shape.route_entity_id
    ):

        return (
            "https://www.facebook.com/"
            "profile.php?id="
            + quote(
                shape.route_entity_id
            )
        )

    return normalized


# ======================================================================
# CLEAN CONTENT URL
# ======================================================================

def clean_content_url(
    url: str,
) -> str:

    normalized = normalize_url(
        url
    )

    if not normalized:
        return ""

    parsed = urlparse(
        normalized
    )

    shape = classify_url(
        normalized
    )

    if shape.kind == "reel" and shape.reel_id:

        return (
            "https://www.facebook.com/"
            "reel/"
            f"{shape.reel_id}"
        )

    if shape.kind == "video" and shape.video_id:

        return (
            "https://www.facebook.com/"
            "watch/?v="
            f"{shape.video_id}"
        )

    if shape.kind == "photo" and shape.photo_id:

        return (
            "https://www.facebook.com/"
            "photo.php?fbid="
            f"{shape.photo_id}"
        )

    if shape.kind == "post" and shape.post_id:

        return (
            "https://www.facebook.com/"
            "posts/"
            f"{shape.post_id}"
        )

    # Fallback: remove query parameters.
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            "",
            "",
        )
    )


# ======================================================================
# PROFILE VERIFIER
# ======================================================================

@dataclass
class ProfileVerification:

    uid: str

    profile_url: str

    score: float = 0

    verified: bool = False

    signals: List[str] = field(
        default_factory=list
    )

    snapshot: Optional[
        Snapshot
    ] = None

    info: ProfileInfo = field(
        default_factory=ProfileInfo
    )


async def verify_profile(
    fetcher: FetchEngine,
    profile_url: str,
    uid: str,
) -> ProfileVerification:

    result = ProfileVerification(
        uid=uid,
        profile_url=profile_url,
    )

    snapshot = await fetcher.fetch(
        profile_url
    )

    result.snapshot = snapshot

    if not snapshot.html:

        return result

    info = extract_profile_identity(
        snapshot,
        uid,
    )

    result.info = info

    shape = classify_url(
        profile_url
    )

    # --------------------------------------------------------------
    # Explicit profile URL.
    # --------------------------------------------------------------

    if (
        shape.kind == "profile"
        and shape.route_entity_id == uid
    ):

        result.score += 55

        result.signals.append(
            "profile_url_exact_uid"
        )

    # --------------------------------------------------------------
    # Final URL.
    # --------------------------------------------------------------

    final_shape = classify_url(
        snapshot.final_url
    )

    if (
        final_shape.kind == "profile"
        and final_shape.route_entity_id == uid
    ):

        result.score += 65

        result.signals.append(
            "final_url_exact_uid"
        )

    # --------------------------------------------------------------
    # Canonical.
    # --------------------------------------------------------------

    canonical = (
        clean_profile_url(
            snapshot.canonical
        )
    )

    canonical_shape = classify_url(
        canonical
    )

    if (
        canonical_shape.kind == "profile"
        and canonical_shape.route_entity_id == uid
    ):

        result.score += 80

        result.signals.append(
            "canonical_exact_uid"
        )

    # --------------------------------------------------------------
    # Explicit semantic identity.
    # --------------------------------------------------------------

    graph = EvidenceGraph()

    collect_semantic_evidence(
        graph,
        snapshot.html,
        "profile_semantic",
        snapshot.final_url,
    )

    collect_json_semantic_evidence(
        graph,
        snapshot.embedded_json,
        "profile_json",
        snapshot.final_url,
    )

    collect_jsonld_evidence(
        graph,
        snapshot.jsonld,
        snapshot.final_url,
    )

    identity_items = [
        x
        for x in graph.for_value(
            uid
        )
        if x.role
        == "USER_CANDIDATE"
    ]

    identity_sources = {
        x.source
        for x in identity_items
    }

    if identity_items:

        result.score += 40

        result.signals.append(
            "explicit_identity_field"
        )

    if len(identity_sources) >= 2:

        result.score += 30

        result.signals.append(
            "multiple_identity_sources"
        )

    # --------------------------------------------------------------
    # Final verification.
    # --------------------------------------------------------------

    strong_url = any(
        x in result.signals
        for x in (
            "canonical_exact_uid",
            "final_url_exact_uid",
        )
    )

    semantic = (
        "explicit_identity_field"
        in result.signals
    )

    multi = (
        "multiple_identity_sources"
        in result.signals
    )

    if (
        strong_url
        and semantic
    ):

        result.verified = True

    elif (
        strong_url
        and multi
    ):

        result.verified = True

    elif (
        result.score >= 150
        and semantic
        and multi
    ):

        result.verified = True

    if result.verified:

        result.info.verified = True

    return result


# ======================================================================
# USER SCORING
# ======================================================================

def candidate_user_scores(
    graph: EvidenceGraph,
) -> Dict[str, float]:

    scores = {}

    for uid in graph.values(
        "USER_CANDIDATE"
    ):

        items = graph.for_value(
            uid
        )

        by_source = {}

        for item in items:

            if item.role != "USER_CANDIDATE":
                continue

            by_source.setdefault(
                item.source,
                [],
            ).append(
                item
            )

        score = 0

        for source, source_items in (
            by_source.items()
        ):

            strongest = max(
                source_items,
                key=lambda x: x.weight,
            )

            contribution = min(
                150,
                strongest.weight,
            )

            # Same source gets only tiny bonus.
            if len(source_items) > 1:

                contribution += min(
                    12,
                    (len(source_items) - 1)
                    * 3,
                )

            score += contribution

        source_count = len(
            by_source
        )

        if source_count >= 2:
            score += 30

        if source_count >= 3:
            score += 30

        if source_count >= 4:
            score += 20

        if any(
            x.verified
            for x in items
        ):

            score += 150

        scores[uid] = score

    return scores


# ======================================================================
# CONFLICT DETECTION
# ======================================================================

def detect_user_conflict(
    graph: EvidenceGraph,
) -> Tuple[
    bool,
    List[str],
]:

    scores = candidate_user_scores(
        graph
    )

    ranked = sorted(
        scores.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    if len(ranked) < 2:

        return (
            False,
            [],
        )

    top_uid, top_score = ranked[0]

    conflicts = []

    for uid, score in ranked[1:]:

        if (
            score >= 180
            and score >= top_score * 0.72
        ):

            conflicts.append(
                uid
            )

    if conflicts:

        return (
            True,
            [top_uid] + conflicts,
        )

    return (
        False,
        [],
    )


# ======================================================================
# ENTITY TYPE
# ======================================================================

def infer_entity_type(
    shape: URLShape,
) -> str:

    mapping = {
        "profile": "USER",
        "group": "GROUP",
        "group_post": "GROUP_POST",
        "page": "PAGE",
        "post": "POST",
        "video": "VIDEO",
        "reel": "REEL",
        "photo": "PHOTO",
        "story": "STORY",
        "permalink": "POST",
        "share": "SHARE",
    }

    return mapping.get(
        shape.kind,
        "UNKNOWN",
    )


def infer_publisher_type(
    shape: URLShape,
    graph: EvidenceGraph,
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
# CONTENT EXTRACTION
# ======================================================================

def extract_content_info(
    shape: URLShape,
    snapshot: Snapshot,
    graph: EvidenceGraph,
) -> ContentInfo:

    content = ContentInfo()

    final_shape = classify_url(
        snapshot.final_url
    )

    # Prefer final route.
    chosen = final_shape

    if chosen.kind == "unknown":

        chosen = shape

    content.content_type = (
        infer_entity_type(
            chosen
        )
    )

    content.post_id = (
        best_value(
            graph,
            "POST",
        )
    )

    content.video_id = (
        best_value(
            graph,
            "VIDEO",
        )
    )

    content.reel_id = (
        best_value(
            graph,
            "REEL",
        )
    )

    content.photo_id = (
        best_value(
            graph,
            "PHOTO",
        )
    )

    content.story_id = (
        best_value(
            graph,
            "STORY",
        )
    )

    content.album_id = (
        best_value(
            graph,
            "ALBUM",
        )
    )

    # Route IDs should win if available.
    if chosen.post_id:
        content.post_id = chosen.post_id

    if chosen.video_id:
        content.video_id = chosen.video_id

    if chosen.reel_id:
        content.reel_id = chosen.reel_id

    if chosen.photo_id:
        content.photo_id = chosen.photo_id

    if chosen.story_id:
        content.story_id = chosen.story_id

    if chosen.album_id:
        content.album_id = chosen.album_id

    content.title = (
        extract_clean_title(
            snapshot
        )
    )

    content.canonical_url = (
        clean_content_url(
            snapshot.canonical
        )
        if snapshot.canonical
        else clean_content_url(
            snapshot.final_url
        )
    )

    return content


# ======================================================================
# TITLE
# ======================================================================

def extract_clean_title(
    snapshot: Snapshot,
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

        value = compact_text(
            value,
            350,
        )

        if not value:
            continue

        if value.lower() in {
            "facebook",
            "meta",
        }:
            continue

        return value

    return ""


# ======================================================================
# BEST OBJECT VALUE
# ======================================================================

def best_value(
    graph: EvidenceGraph,
    role: str,
) -> str:

    values = graph.values(
        role
    )

    if not values:
        return ""

    best = ""

    best_score = -1

    for value in values:

        items = [
            x
            for x in graph.for_value(
                value
            )
            if x.role == role
        ]

        sources = {
            x.source
            for x in items
        }

        score = sum(
            min(
                150,
                x.weight,
            )
            for x in items
        )

        score += len(
            sources
        ) * 20

        if score > best_score:

            best_score = score

            best = value

    return best


# ======================================================================
# IDENTITY VERIFICATION
# ======================================================================

async def resolve_identity(
    fetcher: FetchEngine,
    graph: EvidenceGraph,
    snapshot: Snapshot,
    shape: URLShape,
) -> Tuple[
    ProfileInfo,
    float,
    str,
    List[str],
]:

    notes = []

    profile_urls = (
        extract_profile_urls(
            snapshot
        )
    )

    scores = candidate_user_scores(
        graph
    )

    ranked = sorted(
        scores.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    if not ranked:

        return (
            ProfileInfo(),
            0,
            "NOT_VERIFIED",
            [
                "No user identity candidate"
            ],
        )

    conflict, conflict_uids = (
        detect_user_conflict(
            graph
        )
    )

    if conflict:

        notes.append(
            "Multiple strong user identities conflict"
        )

        return (
            ProfileInfo(),
            0,
            "NOT_VERIFIED",
            notes,
        )

    best_uid = ranked[0][0]

    # --------------------------------------------------------------
    # Verify discovered profile URLs.
    # --------------------------------------------------------------

    verifications = []

    for profile_url in profile_urls:

        child = classify_url(
            profile_url
        )

        uid = child.route_entity_id

        if not uid:
            continue

        verification = (
            await verify_profile(
                fetcher,
                profile_url,
                uid,
            )
        )

        verifications.append(
            verification
        )

        if verification.verified:

            if uid == best_uid:

                info = (
                    verification.info
                )

                if not info.uid:
                    info.uid = uid

                return (
                    info,
                    min(
                        99.9,
                        94
                        + min(
                            5,
                            len(
                                verification.signals
                            ),
                        ),
                    ),
                    "VERIFIED",
                    [
                        "Profile identity independently verified",
                        *verification.signals,
                    ],
                )

    # --------------------------------------------------------------
    # Explicit profile candidate.
    # --------------------------------------------------------------

    if (
        shape.kind == "profile"
        and shape.route_entity_id
    ):

        uid = shape.route_entity_id

        profile_url = (
            "https://www.facebook.com/"
            "profile.php?id="
            f"{uid}"
        )

        verification = (
            await verify_profile(
                fetcher,
                profile_url,
                uid,
            )
        )

        if verification.verified:

            return (
                verification.info,
                min(
                    99.0,
                    92
                    + min(
                        7,
                        len(
                            verification.signals
                        ),
                    ),
                ),
                "VERIFIED",
                [
                    "Explicit profile ID verified",
                    *verification.signals,
                ],
            )

    # --------------------------------------------------------------
    # No profile verification.
    # --------------------------------------------------------------

    notes.append(
        "User candidate found but profile correlation "
        "is insufficient"
    )

    return (
        ProfileInfo(),
        0,
        "NOT_VERIFIED",
        notes,
    )


# ======================================================================
# MAIN RESOLVER
# ======================================================================

class FacebookResolver:

    def __init__(
        self,
    ):

        self.fetcher = (
            FetchEngine()
        )

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
        # URL evidence
        # ----------------------------------------------------------

        add_structural_evidence(
            graph,
            shape,
            normalized,
        )

        # ----------------------------------------------------------
        # Fetch
        # ----------------------------------------------------------

        snapshot = await self.fetcher.fetch(
            normalized
        )

        result.resolved_url = (
            snapshot.final_url
            or normalized
        )

        result.canonical_url = (
            clean_content_url(
                snapshot.canonical
            )
            if snapshot.canonical
            else ""
        )

        if not snapshot.html:

            result.notes.append(
                "Facebook did not return usable public HTML"
            )

            result.evidence = (
                graph.items
            )

            result.elapsed = (
                time.monotonic()
                - started
            )

            return result

        # ----------------------------------------------------------
        # Final URL
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
        # Canonical
        # ----------------------------------------------------------

        if snapshot.canonical:

            canonical_shape = (
                classify_url(
                    snapshot.canonical
                )
            )

            add_structural_evidence(
                graph,
                canonical_shape,
                snapshot.canonical,
            )

        # ----------------------------------------------------------
        # Redirects
        # ----------------------------------------------------------

        for redirect in snapshot.redirects:

            redirect_shape = (
                classify_url(
                    redirect
                )
            )

            add_structural_evidence(
                graph,
                redirect_shape,
                redirect,
            )

        # ----------------------------------------------------------
        # Metadata
        # ----------------------------------------------------------

        collect_meta_evidence(
            graph,
            snapshot,
        )

        # ----------------------------------------------------------
        # HTML
        # ----------------------------------------------------------

        collect_semantic_evidence(
            graph,
            snapshot.html,
            "html_semantic",
            snapshot.final_url,
        )

        # ----------------------------------------------------------
        # Embedded JSON
        # ----------------------------------------------------------

        collect_json_semantic_evidence(
            graph,
            snapshot.embedded_json,
            "embedded_json",
            snapshot.final_url,
        )

        # ----------------------------------------------------------
        # JSON-LD
        # ----------------------------------------------------------

        collect_json_semantic_evidence(
            graph,
            snapshot.jsonld,
            "jsonld",
            snapshot.final_url,
        )

        collect_jsonld_evidence(
            graph,
            snapshot.jsonld,
            snapshot.final_url,
        )

        # ----------------------------------------------------------
        # Discover all Facebook URLs.
        # ----------------------------------------------------------

        discovered = (
            discover_facebook_urls(
                snapshot
            )
        )

        for discovered_url in discovered:

            child = classify_url(
                discovered_url
            )

            add_structural_evidence(
                graph,
                child,
                discovered_url,
            )

        # ----------------------------------------------------------
        # Identity
        # ----------------------------------------------------------

        (
            profile,
            confidence,
            status,
            identity_notes,
        ) = await resolve_identity(
            self.fetcher,
            graph,
            snapshot,
            shape,
        )

        result.profile = profile

        result.confidence = confidence

        result.status = status

        result.notes.extend(
            identity_notes
        )

        # ----------------------------------------------------------
        # Content
        # ----------------------------------------------------------

        result.content = (
            extract_content_info(
                shape,
                snapshot,
                graph,
            )
        )

        # ----------------------------------------------------------
        # Entity
        # ----------------------------------------------------------

        result.entity_type = (
            infer_entity_type(
                final_shape
                if final_shape.kind
                != "unknown"
                else shape
            )
        )

        result.publisher_type = (
            infer_publisher_type(
                final_shape,
                graph,
            )
        )

        # If profile is verified,
        # publisher is USER unless content says PAGE/GROUP.
        if (
            result.status
            == "VERIFIED"
            and result.publisher_type
            == "UNKNOWN"
        ):

            result.publisher_type = "USER"

        # ----------------------------------------------------------
        # Clean URLs
        # ----------------------------------------------------------

        result.resolved_url = (
            clean_content_url(
                snapshot.final_url
            )
        )

        if snapshot.canonical:

            result.canonical_url = (
                clean_content_url(
                    snapshot.canonical
                )
            )

        # ----------------------------------------------------------
        # Evidence
        # ----------------------------------------------------------

        result.evidence = sorted(
            graph.items,
            key=lambda x: x.weight,
            reverse=True,
        )

        # ----------------------------------------------------------
        # Final
        # ----------------------------------------------------------

        if (
            result.status
            != "VERIFIED"
        ):

            result.profile = (
                ProfileInfo()
            )

            result.confidence = 0

            result.notes.append(
                "UID withheld: insufficient identity correlation"
            )

        result.elapsed = (
            time.monotonic()
            - started
        )

        return result


# ======================================================================
# TELEGRAM ESCAPE
# ======================================================================

def tg_escape(
    value: Any,
) -> str:

    value = (
        ""
        if value is None
        else str(value)
    )

    return (
        value
        .replace(
            "&",
            "&amp;",
        )
        .replace(
            "<",
            "&lt;",
        )
        .replace(
            ">",
            "&gt;",
        )
        .replace(
            '"',
            "&quot;",
        )
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

    return value[
        :limit - 3
    ] + "..."


# ======================================================================
# UI HELPERS
# ======================================================================

def content_icon(
    content_type: str,
) -> str:

    mapping = {
        "REEL": "🎬",
        "VIDEO": "🎥",
        "POST": "📝",
        "PHOTO": "📷",
        "STORY": "⭕",
        "GROUP": "👥",
        "GROUP_POST": "👥",
        "PAGE": "📄",
        "USER": "👤",
    }

    return mapping.get(
        content_type,
        "📌",
    )


def entity_icon(
    entity_type: str,
) -> str:

    return {
        "USER": "👤",
        "PAGE": "📄",
        "GROUP": "👥",
    }.get(
        entity_type,
        "📌",
    )


def format_profile_block(
    result: ResolveResult,
) -> List[str]:

    profile = result.profile

    lines = []

    if not profile.uid:
        return lines

    lines.append(
        "👤 <b>NGƯỜI ĐĂNG</b>"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    if profile.name:

        lines.append(
            "Tên: "
            f"<b>{tg_escape(profile.name)}</b>"
        )

    if profile.username:

        lines.append(
            "Username: "
            f"<code>@{tg_escape(profile.username)}</code>"
        )

    lines.append(
        "UID: "
        f"<code>{tg_escape(profile.uid)}</code>"
    )

    lines.append(
        "Loại: "
        f"{entity_icon(profile.entity_type)} "
        f"{tg_escape(profile.entity_type)}"
    )

    if profile.profile_url:

        lines.append(
            "Profile: "
            f"<a href=\"{tg_escape(profile.profile_url)}\">"
            "Mở profile"
            "</a>"
        )

    if profile.bio:

        lines.append("")

        lines.append(
            "📝 "
            f"{tg_escape(truncate(profile.bio, 350))}"
        )

    return lines


def format_content_block(
    result: ResolveResult,
) -> List[str]:

    content = result.content

    lines = []

    has_content = any(
        [
            content.post_id,
            content.video_id,
            content.reel_id,
            content.photo_id,
            content.story_id,
        ]
    )

    if not has_content:
        return lines

    icon = content_icon(
        content.content_type
    )

    lines.append(
        f"{icon} <b>NỘI DUNG</b>"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    if content.content_type:

        lines.append(
            "Loại: "
            f"<b>{tg_escape(content.content_type)}</b>"
        )

    if content.title:

        lines.append(
            "Tiêu đề: "
            f"{tg_escape(truncate(content.title, 300))}"
        )

    if content.post_id:

        lines.append(
            "Post ID: "
            f"<code>{tg_escape(content.post_id)}</code>"
        )

    if content.video_id:

        lines.append(
            "Video ID: "
            f"<code>{tg_escape(content.video_id)}</code>"
        )

    if content.reel_id:

        lines.append(
            "Reel ID: "
            f"<code>{tg_escape(content.reel_id)}</code>"
        )

    if content.photo_id:

        lines.append(
            "Photo ID: "
            f"<code>{tg_escape(content.photo_id)}</code>"
        )

    if content.story_id:

        lines.append(
            "Story ID: "
            f"<code>{tg_escape(content.story_id)}</code>"
        )

    return lines


def format_links_block(
    result: ResolveResult,
) -> List[str]:

    lines = []

    if not (
        result.profile.profile_url
        or result.canonical_url
    ):
        return lines

    lines.append(
        "🔗 <b>LIÊN KẾT</b>"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    if result.profile.profile_url:

        lines.append(
            "Profile: "
            f"<a href=\""
            f"{tg_escape(result.profile.profile_url)}"
            f"\">Facebook Profile</a>"
        )

    if result.canonical_url:

        lines.append(
            "Content: "
            f"<a href=\""
            f"{tg_escape(result.canonical_url)}"
            f"\">Facebook Content</a>"
        )

    return lines


def format_verification_block(
    result: ResolveResult,
) -> List[str]:

    lines = []

    if result.status == "VERIFIED":

        lines.append(
            "🛡 <b>XÁC MINH</b>"
        )

        lines.append(
            "━━━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            "Trạng thái: "
            "<b>✅ VERIFIED</b>"
        )

        lines.append(
            "Độ tin cậy: "
            f"<b>{result.confidence:.1f}%</b>"
        )

        sources = set()

        for item in result.evidence:

            if (
                item.role
                == "USER_CANDIDATE"
            ):

                sources.add(
                    item.source
                )

        if sources:

            lines.append(
                "Nguồn: "
                f"<b>{len(sources)} nguồn</b>"
            )

        lines.append(
            "✓ Identity correlation"
        )

        lines.append(
            "✓ Profile verification"
        )

    else:

        lines.append(
            "🛡 <b>XÁC MINH</b>"
        )

        lines.append(
            "━━━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            "Trạng thái: "
            "<b>⚠️ NOT VERIFIED</b>"
        )

        lines.append(
            "UID chưa được hiển thị vì "
            "chưa đủ bằng chứng độc lập."
        )

    return lines


def format_forensic_block(
    result: ResolveResult,
) -> List[str]:

    lines = []

    useful = []

    seen = set()

    for item in result.evidence:

        if item.role in {
            "ROUTE_ENTITY",
            "OBJECT",
        }:
            continue

        key = (
            item.value,
            item.role,
            item.source,
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        useful.append(
            item
        )

        if len(useful) >= MAX_EVIDENCE_DISPLAY:
            break

    if not useful:
        return lines

    lines.append(
        "🔬 <b>FORENSIC</b>"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    for item in useful:

        label = {
            "USER_CANDIDATE":
                "🆔 Identity",
            "POST":
                "📝 Post",
            "VIDEO":
                "🎥 Video",
            "REEL":
                "🎬 Reel",
            "PHOTO":
                "📷 Photo",
            "STORY":
                "⭕ Story",
            "PAGE":
                "📄 Page",
            "GROUP":
                "👥 Group",
        }.get(
            item.role,
            item.role,
        )

        lines.append(
            f"{label}: "
            f"<code>{tg_escape(item.value)}</code>"
        )

        if item.key:

            lines.append(
                "  "
                f"<i>{tg_escape(item.key)}</i>"
                f" · {tg_escape(item.source)}"
            )

    return lines


# ======================================================================
# MAIN UI
# ======================================================================

def format_result(
    result: ResolveResult,
    index: Optional[int] = None,
) -> str:

    lines = []

    if index is not None:

        lines.append(
            f"<b>#{index}</b>"
        )

    if result.status == "VERIFIED":

        lines.append(
            "🔎 <b>FACEBOOK IDENTITY RESOLVER</b>"
        )

    else:

        lines.append(
            "🔎 <b>FACEBOOK RESOLVER</b>"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.extend(
        format_profile_block(
            result
        )
    )

    if result.profile.uid:

        lines.append("")

    lines.extend(
        format_content_block(
            result
        )
    )

    if result.content.post_id or (
        result.content.reel_id
    ):

        lines.append("")

    lines.extend(
        format_links_block(
            result
        )
    )

    if (
        result.profile.profile_url
        or result.canonical_url
    ):

        lines.append("")

    lines.extend(
        format_verification_block(
            result
        )
    )

    # --------------------------------------------------------------
    # Notes
    # --------------------------------------------------------------

    if (
        result.status != "VERIFIED"
        and result.notes
    ):

        lines.append("")

        lines.append(
            "ℹ️ "
            f"{tg_escape(truncate(result.notes[-1], 350))}"
        )

    # --------------------------------------------------------------
    # Forensic
    # --------------------------------------------------------------

    if result.status == "VERIFIED":

        lines.append("")

        lines.extend(
            format_forensic_block(
                result
            )
        )

    lines.append("")

    lines.append(
        "⏱ "
        f"{result.elapsed:.2f}s"
    )

    return "\n".join(
        lines
    )


# ======================================================================
# WAIT FOR URL
# ======================================================================

_ACTIVE_SESSIONS = {}


async def wait_for_next_message(
    client,
    chat_id: int,
    timeout: int = 120,
):

    loop = asyncio.get_running_loop()

    future = loop.create_future()

    async def handler(
        event,
    ):

        if event.chat_id != chat_id:
            return

        text = (
            event.raw_text
            or ""
        )

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
            timeout,
        )

    except asyncio.TimeoutError:

        return None

    finally:

        client.remove_event_handler(
            handler
        )


# ======================================================================
# RESOLVE TELEGRAM TEXT
# ======================================================================

async def resolve_text(
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

    urls = urls[
        :MAX_INPUT_URLS
    ]

    resolver = FacebookResolver()

    results = []

    for index, url in enumerate(
        urls,
        start=1,
    ):

        try:

            result = await resolver.resolve(
                url
            )

            results.append(
                format_result(
                    result,
                    index
                    if len(urls) > 1
                    else None,
                )
            )

        except Exception as exc:

            log.exception(
                "Facebook resolver error"
            )

            results.append(
                (
                    f"❌ <b>#{index}</b>\n"
                    "Resolver error: "
                    f"<code>{tg_escape(exc)}</code>"
                )
            )

    output = "\n\n".join(
        results
    )

    # Telegram safe limit.
    if len(output) <= 3900:

        await event.reply(
            output,
            parse_mode="html",
            link_preview=False,
        )

        return

    # Split without destroying HTML blocks.
    chunks = []

    current = ""

    for block in results:

        if (
            len(current)
            + len(block)
            + 2
            <= 3900
        ):

            if current:
                current += "\n\n"

            current += block

        else:

            if current:
                chunks.append(
                    current
                )

            current = block

    if current:
        chunks.append(
            current
        )

    for chunk in chunks:

        await event.reply(
            chunk[:3900],
            parse_mode="html",
            link_preview=False,
        )


# ======================================================================
# /getuidfb
# ======================================================================

async def _handle_getuidfb(
    event,
):

    sender_id = (
        event.sender_id
    )

    _ACTIVE_SESSIONS[
        sender_id
    ] = time.time()

    try:

        raw = (
            event.raw_text
            or ""
        )

        parts = raw.split(
            maxsplit=1
        )

        if len(parts) > 1:

            text = parts[1].strip()

            if text:

                await resolve_text(
                    event,
                    text,
                )

                return

        await event.reply(
            "🔎 <b>FACEBOOK IDENTITY RESOLVER</b>\n\n"
            "Gửi link Facebook công khai.\n"
            "Có thể gửi nhiều link cùng lúc.\n\n"
            "Ví dụ:\n"
            "<code>/getuidfb https://facebook.com/...</code>\n\n"
            "Hoặc gửi link ở tin nhắn tiếp theo.",
            parse_mode="html",
        )

        text = await wait_for_next_message(
            event.client,
            event.chat_id,
            120,
        )

        if not text:

            await event.reply(
                "⌛ Hết thời gian chờ.",
                parse_mode="html",
            )

            return

        await resolve_text(
            event,
            text,
        )

    finally:

        _ACTIVE_SESSIONS.pop(
            sender_id,
            None,
        )


# ======================================================================
# REGISTER
# ======================================================================

def register(
    bot,
    notify_bot=None,
):

    bot.add_event_handler(
        _handle_getuidfb,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?(?:\s+.*)?$"
        ),
    )

    log.info(
        "Registered /getuidfb V31"
    )


# ======================================================================
# COMMAND INFO
# ======================================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": (
        "Facebook Identity + Content Resolver"
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

    cases = [
        (
            "https://www.facebook.com/profile.php?id=61553239356646",
            "profile",
            "",
            "61553239356646",
        ),
        (
            "https://www.facebook.com/reel/4697823150500072/",
            "reel",
            "4697823150500072",
            "",
        ),
        (
            "https://www.facebook.com/61592487939720/videos/1671583863938536/",
            "video",
            "1671583863938536",
            "61592487939720",
        ),
        (
            "https://www.facebook.com/groups/123456789012345/posts/987654321012345/",
            "group_post",
            "987654321012345",
            "123456789012345",
        ),
        (
            "https://www.facebook.com/photo.php?fbid=1234567890123456",
            "photo",
            "1234567890123456",
            "",
        ),
    ]

    print(
        "=" * 75
    )

    print(
        "FACEBOOK IDENTITY RESOLVER V31"
    )

    print(
        "=" * 75
    )

    for (
        url,
        expected_kind,
        expected_object,
        expected_route,
    ) in cases:

        shape = classify_url(
            url
        )

        objects = [
            shape.post_id,
            shape.video_id,
            shape.reel_id,
            shape.photo_id,
            shape.story_id,
        ]

        found = next(
            (
                x
                for x in objects
                if x
            ),
            "",
        )

        ok = (
            shape.kind
            == expected_kind
            and found
            == expected_object
            and shape.route_entity_id
            == expected_route
        )

        print(
            "PASS"
            if ok
            else "FAIL",
            "|",
            shape.kind,
            "|",
            url,
        )

        print(
            "  route_entity_id =",
            shape.route_entity_id,
        )

        print(
            "  object_id        =",
            found,
        )

    print()

    concatenated = (
        "https://www.facebook.com/reel/4697823150500072/"
        "https://www.facebook.com/profile.php?id=61553239356646"
    )

    urls = extract_facebook_urls(
        concatenated
    )

    print(
        "SCANNER:",
        len(urls),
        "URLs"
    )

    for url in urls:

        print(
            " ",
            url,
        )

    print(
        "=" * 75
    )


# ======================================================================
# CLI
# ======================================================================

if __name__ == "__main__":

    import sys

    if "--self-test" in sys.argv:

        self_test()

    else:

        print(
            "Facebook Identity + Content Resolver V31"
        )

        print(
            "Run:"
        )

        print(
            "python commands/getuidfb.py --self-test"
        )