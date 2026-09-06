#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
============================================================
 FACEBOOK UID / ENTITY RESOLVER V23 FORENSIC
============================================================

PUBLIC FACEBOOK CONTENT ONLY
HTTP ONLY

NO:
- Playwright
- Selenium
- Chromium
- Cookies
- Facebook Login
- Facebook Access Token

ENGINE:
- requests
- Telethon
- HTMLParser
- META / OG
- Canonical
- JSON-LD
- application/json
- Embedded JSON
- JavaScript semantic analysis
- Redirect correlation
- Profile verification
- Evidence graph
- Conflict detection

PRECISION FIRST.

IMPORTANT:
A numeric Facebook ID found in HTML/JS is NOT automatically USER UID.

The resolver separates:

USER UID
PAGE ID
GROUP ID
EVENT ID
POST ID
VIDEO ID
REEL ID
PHOTO ID
STORY ID
ALBUM ID
ENTITY / ROUTE ID

Opaque pfbid/share tokens are NEVER mathematically decoded.

A UID is exposed only when sufficient semantic/correlation evidence exists.
Otherwise:

NOT VERIFIED

============================================================
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html as html_lib
import json
import logging
import re
import time

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from urllib.parse import (
    parse_qs,
    quote,
    unquote,
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
# CONFIG
# ============================================================

REQUEST_TIMEOUT = 12

MAX_HTML_BYTES = 12 * 1024 * 1024

MAX_PROFILE_CHECKS = 5

MAX_DISCOVERED_URLS = 100

MAX_JSON_SCRIPTS = 80

MAX_EVIDENCE_CONTEXT = 900

SESSION_TIMEOUT = 900

MAX_URLS_PER_MESSAGE = 30

MAX_CANDIDATES = 50

MIN_UID_LENGTH = 8
MAX_UID_LENGTH = 21


# ============================================================
# FACEBOOK HOSTS
# ============================================================

FB_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "mobile.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
}

FB_SHORT_HOSTS = {
    "fb.watch",
}


# ============================================================
# TRACKING PARAMS
# ============================================================

TRACKING_PARAMS = {
    "fbclid",
    "mibextid",
    "ref",
    "refid",
    "tn",
    "cft",
    "locale",
    "__tn__",
}


# ============================================================
# USER AGENTS
# ============================================================

USER_AGENTS = [
    (
        "Mozilla/5.0 (Linux; Android 14; Mobile) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 14; Mobile; rv:128.0) "
        "Gecko/128.0 Firefox/128.0"
    ),
]


# ============================================================
# SEMANTIC FIELD WEIGHTS
# ============================================================

USER_FIELD_WEIGHTS = {
    "user_id": 120,
    "profile_id": 118,
    "profile.uid": 118,

    "owner_id": 108,
    "owner.id": 108,

    "publisher_id": 108,
    "publisher.id": 108,

    "author_id": 104,
    "author.id": 104,

    "creator_id": 98,
    "creator.id": 98,

    "from.id": 96,
    "from_id": 94,

    "actor_id": 78,
    "actor.id": 78,

    "page_owner_id": 70,

    "entity_id": 35,
    "entity.id": 35,
}


OBJECT_FIELD_WEIGHTS = {
    "post_id": 125,
    "story_fbid": 125,

    "video_id": 125,
    "reel_id": 130,

    "photo_id": 125,
    "media_fbid": 115,

    "album_id": 115,

    "group_id": 145,
    "page_id": 145,
    "event_id": 145,
}


# ============================================================
# FIELD ALIASES
# ============================================================

USER_FIELD_ALIASES = {
    "userid": "user_id",
    "user_id": "user_id",
    "profileid": "profile_id",
    "profile_id": "profile_id",

    "ownerid": "owner_id",
    "owner_id": "owner_id",

    "publisherid": "publisher_id",
    "publisher_id": "publisher_id",

    "authorid": "author_id",
    "author_id": "author_id",

    "creatorid": "creator_id",
    "creator_id": "creator_id",

    "actorid": "actor_id",
    "actor_id": "actor_id",

    "entityid": "entity_id",
    "entity_id": "entity_id",

    "pageownerid": "page_owner_id",
    "page_owner_id": "page_owner_id",
}


OBJECT_FIELD_ALIASES = {
    "postid": "post_id",
    "post_id": "post_id",

    "storyfbid": "story_fbid",
    "story_fbid": "story_fbid",

    "videoid": "video_id",
    "video_id": "video_id",

    "reelid": "reel_id",
    "reel_id": "reel_id",

    "photoid": "photo_id",
    "photo_id": "photo_id",

    "mediafbid": "media_fbid",
    "media_fbid": "media_fbid",

    "albumid": "album_id",
    "album_id": "album_id",

    "groupid": "group_id",
    "group_id": "group_id",

    "pageid": "page_id",
    "page_id": "page_id",

    "eventid": "event_id",
    "event_id": "event_id",
}


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class URLShape:
    original: str

    normalized: str = ""

    host: str = ""

    path: str = ""

    query: Dict[str, List[str]] = field(
        default_factory=dict
    )

    kind: str = "UNKNOWN"

    username: Optional[str] = None

    route_entity_id: Optional[str] = None

    numeric_path_id: Optional[str] = None

    post_id: Optional[str] = None

    video_id: Optional[str] = None

    reel_id: Optional[str] = None

    photo_id: Optional[str] = None

    story_id: Optional[str] = None

    group_id: Optional[str] = None

    page_id: Optional[str] = None

    album_id: Optional[str] = None

    event_id: Optional[str] = None

    opaque_token: Optional[str] = None


@dataclass
class Evidence:
    value: str

    role: str

    source: str

    weight: float

    context: str = ""

    url: str = ""

    path: str = ""

    key_name: str = ""

    token: Optional[str] = None

    independent: str = ""

    verified: bool = False

    entity_hint: str = ""

    def key(self):
        return (
            self.value,
            self.role,
            self.source,
            self.path[:300],
        )


@dataclass
class Snapshot:
    requested_url: str

    final_url: str = ""

    status_code: int = 0

    html: str = ""

    title: str = ""

    canonical: Optional[str] = None

    meta: Dict[str, List[str]] = field(
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

    headers: Dict[str, str] = field(
        default_factory=dict
    )

    error: Optional[str] = None


@dataclass
class CandidateScore:
    value: str

    score: float = 0.0

    sources: Set[str] = field(
        default_factory=set
    )

    independent_sources: Set[str] = field(
        default_factory=set
    )

    fields: Set[str] = field(
        default_factory=set
    )

    evidence: List[Evidence] = field(
        default_factory=list
    )

    verified: bool = False

    conflicts: Set[str] = field(
        default_factory=set
    )

    correlations: List[str] = field(
        default_factory=list
    )


@dataclass
class ResolveResult:
    input_url: str

    shape: URLShape

    resolved_url: str = ""

    canonical_url: str = ""

    object_type: str = "UNKNOWN"

    publisher_type: str = "UNKNOWN"

    username: Optional[str] = None

    user_uid: Optional[str] = None

    page_uid: Optional[str] = None

    group_id: Optional[str] = None

    post_id: Optional[str] = None

    video_id: Optional[str] = None

    reel_id: Optional[str] = None

    photo_id: Optional[str] = None

    story_id: Optional[str] = None

    album_id: Optional[str] = None

    event_id: Optional[str] = None

    route_entity_id: Optional[str] = None

    title: str = ""

    status: str = "NOT_VERIFIED"

    confidence: int = 0

    evidence: List[Evidence] = field(
        default_factory=list
    )

    notes: List[str] = field(
        default_factory=list
    )

    redirects: List[str] = field(
        default_factory=list
    )

    elapsed: float = 0.0


# ============================================================
# HTML PARSER
# ============================================================

class FacebookHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.title_parts: List[str] = []

        self.meta: Dict[str, List[str]] = {}

        self.links: List[str] = []

        self.jsonld: List[Any] = []

        self.embedded_json: List[Any] = []

        self.script_parts: List[str] = []

        self._in_title = False

        self._script_type = ""

        self._script_attrs = {}

    def handle_starttag(
        self,
        tag,
        attrs,
    ):
        tag = tag.lower()

        attrs_dict = dict(attrs)

        if tag == "title":
            self._in_title = True

            return

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

                key = key.lower().strip()

                self.meta.setdefault(
                    key,
                    [],
                ).append(
                    html_lib.unescape(
                        content.strip()
                    )
                )

            return

        if tag == "link":

            href = attrs_dict.get(
                "href"
            )

            if href:

                rel = (
                    attrs_dict.get("rel")
                    or ""
                ).lower()

                if (
                    "canonical"
                    in rel
                ):
                    self.meta.setdefault(
                        "__canonical__",
                        [],
                    ).append(
                        href
                    )

                self.links.append(
                    href
                )

            return

        if tag == "script":

            self._script_type = (
                attrs_dict.get("type")
                or ""
            ).lower().strip()

            self._script_attrs = (
                attrs_dict
            )

            self.script_parts = []

    def handle_endtag(
        self,
        tag,
    ):
        tag = tag.lower()

        if tag == "title":

            self._in_title = False

            return

        if tag != "script":
            return

        raw = "".join(
            self.script_parts
        ).strip()

        if raw:

            if (
                "ld+json"
                in self._script_type
            ):

                parsed = safe_json_loads(
                    raw
                )

                if parsed is not None:
                    self.jsonld.append(
                        parsed
                    )

            else:

                parsed = parse_embedded_json(
                    raw
                )

                if parsed:

                    self.embedded_json.extend(
                        parsed
                    )

        self._script_type = ""

        self._script_attrs = {}

        self.script_parts = []

    def handle_data(
        self,
        data,
    ):
        if self._in_title:
            self.title_parts.append(
                data
            )

        if self._script_type:
            self.script_parts.append(
                data
            )

    @property
    def title(self):

        return re.sub(
            r"\s+",
            " ",
            " ".join(
                self.title_parts
            ),
        ).strip()


# ============================================================
# BASIC HELPERS
# ============================================================

def is_numeric_id(
    value: Optional[str]
) -> bool:

    return bool(
        value
        and re.fullmatch(
            rf"\d{{5,{MAX_UID_LENGTH}}}",
            str(value),
        )
    )


def is_long_numeric(
    value: Optional[str]
) -> bool:

    return bool(
        value
        and re.fullmatch(
            rf"\d{{{MIN_UID_LENGTH},{MAX_UID_LENGTH}}}",
            str(value),
        )
    )


def first_query(
    query,
    key,
):

    values = query.get(
        key
    )

    if not values:
        return None

    return values[0]


def clean_query(
    query,
):

    output = {}

    for key, values in query.items():

        low = key.lower()

        if low in TRACKING_PARAMS:
            continue

        if low.startswith(
            "utm_"
        ):
            continue

        output[key] = values

    return output


def is_fb_host(
    host: str
):

    host = (
        host
        or ""
    ).lower().split(":")[0]

    return (
        host in FB_HOSTS
        or host in FB_SHORT_HOSTS
    )


def is_valid_facebook_url(
    url: str
):

    try:

        p = urlparse(url)

        return (
            p.scheme.lower()
            in {
                "http",
                "https",
            }
            and is_fb_host(
                p.netloc
            )
        )

    except Exception:

        return False


# ============================================================
# URL NORMALIZATION
# ============================================================

def normalize_url(
    url: str
) -> str:

    url = html_lib.unescape(
        str(url or "").strip()
    )

    url = url.strip(
        " \t\r\n<>\"'`()[]{}"
    )

    if not re.match(
        r"^https?://",
        url,
        re.I,
    ):

        if url.lower().startswith(
            "www.facebook.com/"
        ):
            url = (
                "https://"
                + url
            )

        elif url.lower().startswith(
            "facebook.com/"
        ):
            url = (
                "https://"
                + url
            )

    try:

        p = urlparse(
            url
        )

        if not is_fb_host(
            p.netloc
        ):
            return url

        query = clean_query(
            parse_qs(
                p.query,
                keep_blank_values=False,
            )
        )

        return urlunparse(
            (
                p.scheme.lower(),
                p.netloc.lower(),
                re.sub(
                    r"/+",
                    "/",
                    p.path,
                ),
                "",
                urlencode(
                    query,
                    doseq=True,
                ),
                "",
            )
        )

    except Exception:

        return url


# ============================================================
# ROBUST FACEBOOK URL SCANNER
# ============================================================

FACEBOOK_URL_START = re.compile(
    r"""
    https?://
    (?:
        (?:www|m|mbasic|mobile|web|touch)\.
    )?
    facebook\.com
    |
    https?://
    fb\.watch
    |
    (?<![A-Za-z0-9_.-])
    (?:www\.)?
    facebook\.com
    """,
    re.I | re.X,
)


def cut_url_at_boundary(
    raw: str
) -> str:

    if not raw:
        return ""

    raw = raw.strip()

    nested = FACEBOOK_URL_START.search(
        raw,
        1,
    )

    if nested:

        raw = raw[
            :nested.start()
        ]

    raw = raw.rstrip(
        ".,;:!?)]}>\"'"
    )

    return raw


def extract_facebook_urls(
    text: str
) -> List[str]:

    if not text:
        return []

    text = html_lib.unescape(
        text
    )

    matches = list(
        FACEBOOK_URL_START.finditer(
            text
        )
    )

    output = []

    seen = set()

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

        raw = text[
            start:end
        ]

        raw = cut_url_at_boundary(
            raw
        )

        if not raw:
            continue

        url = normalize_url(
            raw
        )

        if not is_valid_facebook_url(
            url
        ):
            continue

        key = url.lower()

        if key in seen:
            continue

        seen.add(key)

        output.append(
            url
        )

        if len(output) >= MAX_URLS_PER_MESSAGE:
            break

    return output


# ============================================================
# URL CLASSIFICATION
# ============================================================

def classify_url(
    url: str
) -> URLShape:

    url = normalize_url(
        url
    )

    p = urlparse(
        url
    )

    host = (
        p.netloc
        .lower()
        .split(":")[0]
    )

    path = unquote(
        p.path
        or "/"
    )

    path = re.sub(
        r"/+",
        "/",
        path,
    )

    query = clean_query(
        parse_qs(
            p.query,
            keep_blank_values=False,
        )
    )

    shape = URLShape(
        original=url,
        normalized=url,
        host=host,
        path=path,
        query=query,
    )

    parts = [
        x
        for x in path.split("/")
        if x
    ]

    lower = [
        x.lower()
        for x in parts
    ]

    # --------------------------------------------------------
    # PROFILE.PHP
    # --------------------------------------------------------

    if path.lower().endswith(
        "/profile.php"
    ):

        uid = first_query(
            query,
            "id",
        )

        shape.kind = "PROFILE"

        if is_long_numeric(
            uid
        ):
            shape.numeric_path_id = uid

        return shape

    # --------------------------------------------------------
    # PHOTO.PHP
    # --------------------------------------------------------

    if path.lower().endswith(
        "/photo.php"
    ):

        shape.kind = "PHOTO"

        shape.photo_id = first_query(
            query,
            "fbid",
        )

        shape.album_id = (
            extract_album_from_query(
                query
            )
        )

        owner = first_query(
            query,
            "id",
        )

        if is_long_numeric(
            owner
        ):
            shape.route_entity_id = owner

        return shape

    # --------------------------------------------------------
    # STORY.PHP
    # --------------------------------------------------------

    if path.lower().endswith(
        "/story.php"
    ):

        shape.kind = "STORY"

        shape.story_id = first_query(
            query,
            "story_fbid",
        )

        owner = first_query(
            query,
            "id",
        )

        if is_long_numeric(
            owner
        ):
            shape.route_entity_id = owner

        return shape

    # --------------------------------------------------------
    # PERMALINK
    # --------------------------------------------------------

    if path.lower().endswith(
        "/permalink.php"
    ):

        shape.kind = "POST"

        shape.post_id = (
            first_query(
                query,
                "story_fbid",
            )
            or
            first_query(
                query,
                "id",
            )
        )

        return shape

    # --------------------------------------------------------
    # WATCH
    # --------------------------------------------------------

    if lower and lower[0] == "watch":

        shape.kind = "VIDEO"

        shape.video_id = first_query(
            query,
            "v",
        )

        return shape

    # --------------------------------------------------------
    # GROUP
    # --------------------------------------------------------

    if lower and lower[0] == "groups":

        if len(parts) >= 2:

            group_value = parts[1]

            if is_numeric_id(
                group_value
            ):

                shape.group_id = (
                    group_value
                )

        shape.kind = "GROUP"

        if (
            len(parts) >= 4
            and lower[2]
            in {
                "posts",
                "permalink",
            }
        ):

            shape.kind = "GROUP_POST"

            pid = parts[3]

            if is_numeric_id(
                pid
            ):
                shape.post_id = pid

            else:
                shape.opaque_token = pid

        return shape

    # --------------------------------------------------------
    # PAGES
    # --------------------------------------------------------

    if lower and lower[0] == "pages":

        shape.kind = "PAGE"

        if len(parts) >= 3:

            if is_numeric_id(
                parts[2]
            ):
                shape.page_id = parts[2]

        elif len(parts) >= 2:

            if is_numeric_id(
                parts[1]
            ):
                shape.page_id = parts[1]

        return shape

    # --------------------------------------------------------
    # PEOPLE
    # --------------------------------------------------------

    if lower and lower[0] == "people":

        shape.kind = "PROFILE"

        if len(parts) >= 2:

            shape.username = parts[1]

        if len(parts) >= 3:

            if is_numeric_id(
                parts[2]
            ):
                shape.numeric_path_id = (
                    parts[2]
                )

        return shape

    # --------------------------------------------------------
    # /p/
    # --------------------------------------------------------

    if lower and lower[0] == "p":

        shape.kind = "POST"

        if len(parts) >= 2:

            value = parts[1]

            if is_numeric_id(
                value
            ):
                shape.post_id = value

            else:
                shape.opaque_token = value

        return shape

    # --------------------------------------------------------
    # /reel/
    # --------------------------------------------------------

    if lower and lower[0] in {
        "reel",
        "reels",
    }:

        shape.kind = "REEL"

        if len(parts) >= 2:

            value = parts[1]

            if is_numeric_id(
                value
            ):

                shape.reel_id = value

                shape.video_id = value

            else:

                shape.opaque_token = value

        return shape

    # --------------------------------------------------------
    # /video/
    # --------------------------------------------------------

    if lower and lower[0] in {
        "video",
        "videos",
    }:

        shape.kind = "VIDEO"

        if len(parts) >= 2:

            value = parts[-1]

            if is_numeric_id(
                value
            ):
                shape.video_id = value

        return shape

    # --------------------------------------------------------
    # /photo/
    # --------------------------------------------------------

    if lower and lower[0] in {
        "photo",
        "photos",
    }:

        shape.kind = "PHOTO"

        value = parts[-1]

        if is_numeric_id(
            value
        ):
            shape.photo_id = value

        return shape

    # --------------------------------------------------------
    # /story/
    # --------------------------------------------------------

    if lower and lower[0] in {
        "story",
        "stories",
    }:

        shape.kind = "STORY"

        value = parts[-1]

        if is_numeric_id(
            value
        ):
            shape.story_id = value

        return shape

    # --------------------------------------------------------
    # SHARE
    # --------------------------------------------------------

    if len(parts) >= 2 and lower[0] == "share":

        subtype = lower[1]

        shape.opaque_token = parts[-1]

        if subtype == "p":
            shape.kind = "SHARE_POST"

        elif subtype == "v":
            shape.kind = "SHARE_VIDEO"

        elif subtype == "r":
            shape.kind = "SHARE_REEL"

        elif subtype == "s":
            shape.kind = "SHARE_STORY"

        else:
            shape.kind = "SHARE"

        return shape

    # --------------------------------------------------------
    # USERNAME / ENTITY ROUTES
    # --------------------------------------------------------

    if parts:

        first = parts[0]

        # CRITICAL:
        # Numeric first path segment is NOT username.
        if is_numeric_id(
            first
        ):

            shape.route_entity_id = first

            shape.numeric_path_id = first

        else:

            shape.username = first

        if len(parts) >= 2:

            second = lower[1]

            if second == "posts":

                shape.kind = "USER_POST"

                value = (
                    parts[2]
                    if len(parts) >= 3
                    else ""
                )

                if is_numeric_id(
                    value
                ):
                    shape.post_id = value

                elif value:
                    shape.opaque_token = value

                return shape

            if second in {
                "videos",
                "video",
            }:

                shape.kind = "VIDEO"

                value = (
                    parts[2]
                    if len(parts) >= 3
                    else ""
                )

                if is_numeric_id(
                    value
                ):
                    shape.video_id = value

                return shape

            if second in {
                "reel",
                "reels",
            }:

                shape.kind = "REEL"

                value = (
                    parts[2]
                    if len(parts) >= 3
                    else ""
                )

                if is_numeric_id(
                    value
                ):

                    shape.reel_id = value

                    shape.video_id = value

                return shape

            if second in {
                "photos",
                "photo",
            }:

                shape.kind = "PHOTO"

                value = (
                    parts[2]
                    if len(parts) >= 3
                    else ""
                )

                if is_numeric_id(
                    value
                ):
                    shape.photo_id = value

                return shape

        if shape.route_entity_id:

            shape.kind = "ENTITY_ROUTE"

        else:

            shape.kind = "PROFILE_OR_PAGE"

        return shape

    return shape


# ============================================================
# ALBUM QUERY
# ============================================================

def extract_album_from_query(
    query
) -> Optional[str]:

    value = first_query(
        query,
        "set",
    )

    if not value:
        return None

    m = re.search(
        r"(?:^|[.=])a[.]([0-9]{5,21})",
        value,
        re.I,
    )

    return (
        m.group(1)
        if m
        else None
    )


# ============================================================
# JSON PARSING
# ============================================================

def safe_json_loads(
    text: str
):

    if not text:
        return None

    try:

        return json.loads(
            text
        )

    except Exception:

        pass

    # Facebook sometimes wraps JSON in HTML entities.
    try:

        decoded = html_lib.unescape(
            text
        )

        return json.loads(
            decoded
        )

    except Exception:

        return None


def parse_embedded_json(
    raw: str
) -> List[Any]:

    if not raw:
        return []

    raw = raw.strip()

    results = []

    # --------------------------------------------------------
    # Direct JSON
    # --------------------------------------------------------

    direct = safe_json_loads(
        raw
    )

    if direct is not None:

        results.append(
            direct
        )

        return results

    # --------------------------------------------------------
    # JSON-ish script scanning
    # --------------------------------------------------------

    candidates = []

    for match in re.finditer(
        r"""
        (?:
            \{[^{}]{1,20000}\}
            |
            \[[^\[\]]{1,20000}\]
        )
        """,
        raw,
        re.X | re.S,
    ):

        candidates.append(
            match.group(0)
        )

        if len(candidates) >= 20:
            break

    for candidate in candidates:

        parsed = safe_json_loads(
            candidate
        )

        if parsed is not None:

            results.append(
                parsed
            )

    return results


# ============================================================
# JSON WALK
# ============================================================

def walk_json(
    obj: Any,
    path: str = "",
) -> Iterable[
    Tuple[str, Any]
]:

    yield path, obj

    if isinstance(
        obj,
        dict,
    ):

        for key, value in obj.items():

            child = (
                f"{path}.{key}"
                if path
                else str(key)
            )

            yield from walk_json(
                value,
                child,
            )

    elif isinstance(
        obj,
        list,
    ):

        for index, value in enumerate(
            obj
        ):

            child = (
                f"{path}[{index}]"
            )

            yield from walk_json(
                value,
                child,
            )


# ============================================================
# PATH NORMALIZATION
# ============================================================

def normalize_json_key(
    key: str
) -> str:

    key = str(
        key or ""
    ).strip().lower()

    key = key.replace(
        "-",
        "_",
    )

    compact = re.sub(
        r"[^a-z0-9_]",
        "",
        key,
    )

    if compact in USER_FIELD_ALIASES:
        return USER_FIELD_ALIASES[
            compact
        ]

    if compact in OBJECT_FIELD_ALIASES:
        return OBJECT_FIELD_ALIASES[
            compact
        ]

    return key


def path_has_identity_context(
    path: str
) -> bool:

    low = path.lower()

    strong = (
        "owner",
        "publisher",
        "author",
        "creator",
        "profile",
        "actor",
        "from",
        "user",
    )

    return any(
        token in low
        for token in strong
    )


# ============================================================
# EVIDENCE
# ============================================================

def add_evidence(
    evidence: List[Evidence],
    value: Optional[str],
    role: str,
    source: str,
    weight: float,
    context: str = "",
    url: str = "",
    path: str = "",
    key_name: str = "",
    token: Optional[str] = None,
    independent: str = "",
    verified: bool = False,
    entity_hint: str = "",
):

    if value is None:
        return

    value = str(
        value
    ).strip()

    if not value.isdigit():
        return

    if not is_numeric_id(
        value
    ):
        return

    item = Evidence(
        value=value,
        role=role,
        source=source,
        weight=weight,
        context=(
            context or ""
        )[
            :MAX_EVIDENCE_CONTEXT
        ],
        url=url or "",
        path=path or "",
        key_name=key_name or "",
        token=token,
        independent=(
            independent
            or source
        ),
        verified=verified,
        entity_hint=(
            entity_hint
            or ""
        ),
    )

    for existing in evidence:

        if existing.key() == item.key():

            # Upgrade verification if later evidence
            # is stronger.
            if verified:
                existing.verified = True

            return

    evidence.append(
        item
    )


# ============================================================
# REGEX SEMANTIC EXTRACTION
# ============================================================

def collect_semantic_evidence(
    text: str,
    evidence: List[Evidence],
    source: str,
    base_url: str,
    token: Optional[str] = None,
):

    if not text:
        return

    # --------------------------------------------------------
    # Generic semantic key/value extraction.
    # --------------------------------------------------------

    pattern = re.compile(
        r"""
        ["']?
        (?P<key>
            user[_-]?id
            |
            profile[_-]?id
            |
            owner[_-]?id
            |
            publisher[_-]?id
            |
            author[_-]?id
            |
            creator[_-]?id
            |
            actor[_-]?id
            |
            entity[_-]?id
            |
            page[_-]?id
            |
            group[_-]?id
            |
            post[_-]?id
            |
            story[_-]?fbid
            |
            video[_-]?id
            |
            reel[_-]?id
            |
            photo[_-]?id
            |
            media[_-]?fbid
            |
            album[_-]?id
            |
            event[_-]?id
        )
        ["']?
        \s*
        (?:
            :
            |
            =
        )
        \s*
        ["']?
        (?P<value>\d{5,21})
        ["']?
        """,
        re.I | re.X,
    )

    for match in pattern.finditer(
        text
    ):

        raw_key = match.group(
            "key"
        )

        value = match.group(
            "value"
        )

        field_name = normalize_json_key(
            raw_key
        )

        start = max(
            0,
            match.start() - 220,
        )

        end = min(
            len(text),
            match.end() + 300,
        )

        context = text[
            start:end
        ]

        if field_name in USER_FIELD_WEIGHTS:

            add_evidence(
                evidence=evidence,
                value=value,
                role="USER_CANDIDATE",
                source=source,
                weight=USER_FIELD_WEIGHTS[
                    field_name
                ],
                context=context,
                url=base_url,
                path="regex:" + raw_key,
                key_name=field_name,
                token=token,
                independent=(
                    source
                    + ":"
                    + field_name
                ),
            )

        elif field_name in OBJECT_FIELD_WEIGHTS:

            add_evidence(
                evidence=evidence,
                value=value,
                role=field_name,
                source=source,
                weight=OBJECT_FIELD_WEIGHTS[
                    field_name
                ],
                context=context,
                url=base_url,
                path="regex:" + raw_key,
                key_name=field_name,
                token=token,
                independent=(
                    source
                    + ":"
                    + field_name
                ),
            )


# ============================================================
# JSON SEMANTIC EXTRACTION
# ============================================================

def collect_json_semantic_evidence(
    json_objects: List[Any],
    evidence: List[Evidence],
    base_url: str,
    source_prefix: str = "embedded_json",
):

    for object_index, root in enumerate(
        json_objects
    ):

        for path, value in walk_json(
            root
        ):

            if not isinstance(
                value,
                (
                    str,
                    int,
                    float,
                ),
            ):
                continue

            value_string = str(
                value
            )

            if not value_string.isdigit():
                continue

            if not is_numeric_id(
                value_string
            ):
                continue

            raw_key = (
                path.rsplit(
                    ".",
                    1,
                )[-1]
                if path
                else ""
            )

            raw_key = re.sub(
                r"\[\d+\]",
                "",
                raw_key,
            )

            field_name = normalize_json_key(
                raw_key
            )

            # ------------------------------------------------
            # USER
            # ------------------------------------------------

            if field_name in USER_FIELD_WEIGHTS:

                path_context = (
                    path
                    + " | "
                    + json_context_for_path(
                        root,
                        path,
                    )
                )

                add_evidence(
                    evidence=evidence,
                    value=value_string,
                    role="USER_CANDIDATE",
                    source=(
                        f"{source_prefix}:"
                        f"{field_name}"
                    ),
                    weight=USER_FIELD_WEIGHTS[
                        field_name
                    ],
                    context=path_context,
                    url=base_url,
                    path=path,
                    key_name=field_name,
                    independent=(
                        f"{source_prefix}:"
                        f"{object_index}:"
                        f"{field_name}"
                    ),
                )

            # ------------------------------------------------
            # OBJECT
            # ------------------------------------------------

            elif field_name in OBJECT_FIELD_WEIGHTS:

                add_evidence(
                    evidence=evidence,
                    value=value_string,
                    role=field_name,
                    source=(
                        f"{source_prefix}:"
                        f"{field_name}"
                    ),
                    weight=OBJECT_FIELD_WEIGHTS[
                        field_name
                    ],
                    context=path,
                    url=base_url,
                    path=path,
                    key_name=field_name,
                    independent=(
                        f"{source_prefix}:"
                        f"{object_index}:"
                        f"{field_name}"
                    ),
                )


def json_context_for_path(
    root: Any,
    path: str,
) -> str:

    # We deliberately return only a compact semantic
    # representation instead of dumping the entire object.

    parts = [
        x
        for x in re.split(
            r"[.\[\]]+",
            path,
        )
        if x
    ]

    if not parts:
        return ""

    return " > ".join(
        parts[-8:]
    )


# ============================================================
# URL DISCOVERY
# ============================================================

def normalize_discovered_url(
    value: str,
    base_url: str,
) -> Optional[str]:

    if not value:
        return None

    value = html_lib.unescape(
        value.strip()
    )

    if value.startswith(
        "//"
    ):
        value = (
            "https:"
            + value
        )

    elif value.startswith(
        "/"
    ):
        value = urljoin(
            base_url,
            value,
        )

    elif not re.match(
        r"^https?://",
        value,
        re.I,
    ):
        return None

    if not is_fb_host(
        urlparse(value).netloc
    ):
        return None

    return normalize_url(
        value
    )


def discover_facebook_urls(
    snapshot: Snapshot,
) -> List[str]:

    results = []

    # HTML <a> links.
    for href in snapshot.links:

        url = normalize_discovered_url(
            href,
            snapshot.final_url,
        )

        if url:
            results.append(
                url
            )

    # Raw HTML / JS.
    for match in FACEBOOK_URL_START.finditer(
        snapshot.html
    ):

        start = match.start()

        tail = snapshot.html[
            start:
            start + 5000
        ]

        # Cut at quote/tag/space.
        tail = re.split(
            r"""["'<>\s]""",
            tail,
            maxsplit=1,
        )[0]

        tail = cut_url_at_boundary(
            tail
        )

        if is_valid_facebook_url(
            tail
        ):
            results.append(
                normalize_url(
                    tail
                )
            )

    output = []

    seen = set()

    for url in results:

        key = url.lower()

        if key in seen:
            continue

        seen.add(key)

        output.append(
            url
        )

        if len(output) >= MAX_DISCOVERED_URLS:
            break

    return output


# ============================================================
# PROFILE URL EXTRACTION
# ============================================================

PROFILE_EXCLUDE = {
    "posts",
    "reel",
    "reels",
    "video",
    "videos",
    "photo",
    "photos",
    "story",
    "stories",
    "groups",
    "pages",
    "share",
    "watch",
    "p",
    "permalink.php",
    "profile.php",
}


def is_probable_profile_url(
    url: str
) -> bool:

    try:

        p = urlparse(
            url
        )

        parts = [
            unquote(x)
            for x in p.path.split("/")
            if x
        ]

        if not parts:
            return False

        first = parts[0].lower()

        if first in PROFILE_EXCLUDE:
            return False

        if first.isdigit():
            return False

        if len(parts) > 3:
            return False

        return True

    except Exception:

        return False


def extract_profile_urls(
    snapshot: Snapshot,
) -> List[str]:

    candidates = []

    candidates.extend(
        snapshot.links
    )

    for key in (
        "article:author",
        "profile:url",
        "author:url",
        "og:see_also",
    ):

        candidates.extend(
            snapshot.meta.get(
                key,
                [],
            )
        )

    # JSON / JSON-LD URLs associated with
    # author / publisher / creator / owner.
    all_json = (
        list(snapshot.jsonld)
        + list(snapshot.embedded_json)
    )

    for root in all_json:

        for path, value in walk_json(
            root
        ):

            low = path.lower()

            if not any(
                token in low
                for token in (
                    "author",
                    "publisher",
                    "creator",
                    "owner",
                    "profile",
                )
            ):
                continue

            if isinstance(
                value,
                str,
            ):

                if (
                    "facebook.com"
                    in value.lower()
                ):
                    candidates.append(
                        value
                    )

            elif isinstance(
                value,
                dict,
            ):

                for key in (
                    "url",
                    "@id",
                ):

                    v = value.get(
                        key
                    )

                    if (
                        isinstance(
                            v,
                            str,
                        )
                        and
                        "facebook.com"
                        in v.lower()
                    ):

                        candidates.append(
                            v
                        )

    results = []

    seen = set()

    for value in candidates:

        url = normalize_discovered_url(
            str(value),
            snapshot.final_url,
        )

        if not url:
            continue

        if not is_probable_profile_url(
            url
        ):
            continue

        key = canonical_profile_key(
            url
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        results.append(
            url
        )

        if len(results) >= MAX_PROFILE_CHECKS:
            break

    return results


def canonical_profile_key(
    url: str
) -> str:

    p = urlparse(
        url
    )

    return (
        p.netloc.lower()
        + "/"
        + p.path.lower().strip("/")
    )


# ============================================================
# STRUCTURAL EVIDENCE
# ============================================================

def add_structural_evidence(
    shape: URLShape,
    evidence: List[Evidence],
):

    if shape.route_entity_id:

        add_evidence(
            evidence,
            shape.route_entity_id,
            "ROUTE_ENTITY",
            "url_structure",
            90,
            context=shape.path,
            url=shape.normalized,
            path="route_entity",
            independent="input_route",
        )

    if shape.numeric_path_id:

        # Only profile.php/people should automatically
        # make numeric path ID a USER candidate.
        if shape.kind in {
            "PROFILE",
        }:

            add_evidence(
                evidence,
                shape.numeric_path_id,
                "USER_CANDIDATE",
                "url_profile_route",
                155,
                context=shape.path,
                url=shape.normalized,
                path="profile.id",
                key_name="profile_id",
                independent="url_profile_route",
            )

    if shape.post_id:

        add_evidence(
            evidence,
            shape.post_id,
            "post_id",
            "url_structure",
            150,
            context=shape.path,
            url=shape.normalized,
            path="post",
            independent="url_post",
        )

    if shape.video_id:

        add_evidence(
            evidence,
            shape.video_id,
            "video_id",
            "url_structure",
            150,
            context=shape.path,
            url=shape.normalized,
            path="video",
            independent="url_video",
        )

    if shape.reel_id:

        add_evidence(
            evidence,
            shape.reel_id,
            "reel_id",
            "url_structure",
            160,
            context=shape.path,
            url=shape.normalized,
            path="reel",
            independent="url_reel",
        )

    if shape.photo_id:

        add_evidence(
            evidence,
            shape.photo_id,
            "photo_id",
            "url_structure",
            150,
            context=shape.path,
            url=shape.normalized,
            path="photo",
            independent="url_photo",
        )

    if shape.story_id:

        add_evidence(
            evidence,
            shape.story_id,
            "story_fbid",
            "url_structure",
            150,
            context=shape.path,
            url=shape.normalized,
            path="story",
            independent="url_story",
        )

    if shape.group_id:

        add_evidence(
            evidence,
            shape.group_id,
            "group_id",
            "url_structure",
            180,
            context=shape.path,
            url=shape.normalized,
            path="group",
            independent="url_group",
        )

    if shape.page_id:

        add_evidence(
            evidence,
            shape.page_id,
            "page_id",
            "url_structure",
            180,
            context=shape.path,
            url=shape.normalized,
            path="page",
            independent="url_page",
        )

    if shape.album_id:

        add_evidence(
            evidence,
            shape.album_id,
            "album_id",
            "url_structure",
            170,
            context=shape.path,
            url=shape.normalized,
            path="album",
            independent="url_album",
        )


# ============================================================
# CANONICAL STRUCTURE
# ============================================================

def extract_canonical_structure(
    url: str,
    evidence: List[Evidence],
):

    if not url:
        return

    shape = classify_url(
        url
    )

    add_structural_evidence(
        shape,
        evidence,
    )

    p = urlparse(
        url
    )

    query = parse_qs(
        p.query
    )

    # profile.php?id=
    if p.path.lower().endswith(
        "/profile.php"
    ):

        uid = first_query(
            query,
            "id",
        )

        if is_long_numeric(
            uid
        ):

            add_evidence(
                evidence,
                uid,
                "USER_CANDIDATE",
                "canonical_profile_query",
                180,
                context=url,
                url=url,
                path="profile.php?id",
                key_name="profile_id",
                independent="canonical_profile",
            )

    # photo.php
    if p.path.lower().endswith(
        "/photo.php"
    ):

        photo_id = first_query(
            query,
            "fbid",
        )

        if is_numeric_id(
            photo_id
        ):

            add_evidence(
                evidence,
                photo_id,
                "photo_id",
                "canonical_photo",
                190,
                context=url,
                url=url,
                path="photo.php?fbid",
                independent="canonical_photo",
            )

        album = extract_album_from_query(
            query
        )

        if album:

            add_evidence(
                evidence,
                album,
                "album_id",
                "canonical_photo",
                180,
                context=url,
                url=url,
                path="photo.php?set",
                independent="canonical_album",
            )

        owner = first_query(
            query,
            "id",
        )

        if is_long_numeric(
            owner
        ):

            add_evidence(
                evidence,
                owner,
                "USER_CANDIDATE",
                "photo_owner_query",
                85,
                context=url,
                url=url,
                path="photo.php?id",
                key_name="owner_id",
                independent="photo_owner_query",
            )


# ============================================================
# OBJECT ID SELECTION
# ============================================================

def best_object_id(
    evidence: List[Evidence],
    role: str,
) -> Optional[str]:

    candidates = {}

    for ev in evidence:

        if ev.role != role:
            continue

        row = candidates.setdefault(
            ev.value,
            {
                "score": 0.0,
                "sources": set(),
                "count": 0,
            },
        )

        row["score"] += ev.weight

        row["sources"].add(
            ev.independent
            or ev.source
        )

        row["count"] += 1

    if not candidates:
        return None

    # Source diversity is useful, but bounded.
    for row in candidates.values():

        source_count = len(
            row["sources"]
        )

        if source_count >= 2:
            row["score"] += 25

        if source_count >= 3:
            row["score"] += 20

        if row["count"] >= 2:
            row["score"] += 10

    return max(
        candidates.items(),
        key=lambda x: x[1]["score"],
    )[0]


# ============================================================
# USER CANDIDATE GRAPH
# ============================================================

def build_user_candidates(
    evidence: List[Evidence],
) -> Dict[str, CandidateScore]:

    candidates = {}

    for ev in evidence:

        if ev.role != "USER_CANDIDATE":
            continue

        if not is_long_numeric(
            ev.value
        ):
            continue

        row = candidates.setdefault(
            ev.value,
            CandidateScore(
                value=ev.value
            ),
        )

        row.evidence.append(
            ev
        )

        row.sources.add(
            ev.source
        )

        row.independent_sources.add(
            ev.independent
            or ev.source
        )

        row.fields.add(
            ev.key_name
            or ev.path
            or ev.source
        )

        # Base weight.
        row.score += ev.weight

        if ev.verified:
            row.verified = True

    # --------------------------------------------------------
    # Diversity normalization
    # --------------------------------------------------------

    for row in candidates.values():

        independent_count = len(
            row.independent_sources
        )

        field_count = len(
            row.fields
        )

        # Independent semantic sources matter.
        if independent_count >= 2:
            row.score += 35

        if independent_count >= 3:
            row.score += 30

        if independent_count >= 4:
            row.score += 25

        # Different semantic fields.
        if field_count >= 2:
            row.score += 20

        if field_count >= 3:
            row.score += 25

        # Verification is not merely another occurrence.
        if row.verified:
            row.score += 100

        # Repetition from same evidence source is capped.
        grouped = {}

        for ev in row.evidence:

            grouped.setdefault(
                ev.source,
                0
            )

            grouped[
                ev.source
            ] += 1

        for source, count in grouped.items():

            if count > 1:

                # We already counted the source once.
                # Add only a small bounded repetition bonus.
                row.score += min(
                    10,
                    count - 1,
                )

    return candidates


# ============================================================
# ROLE CONFLICTS
# ============================================================

def detect_role_conflicts(
    candidate: str,
    evidence: List[Evidence],
) -> Set[str]:

    conflicts = set()

    for ev in evidence:

        if ev.value != candidate:
            continue

        if ev.role in {
            "page_id",
            "group_id",
            "album_id",
            "event_id",
        }:

            conflicts.add(
                ev.role
            )

    return conflicts


# ============================================================
# PROFILE VERIFICATION
# ============================================================

async def verify_profile_url(
    url: str,
    expected_uid: Optional[str] = None,
) -> Dict[str, Any]:

    result = {
        "url": url,
        "uid_hits": set(),
        "canonical": "",
        "final_url": "",
        "title": "",
        "type": "UNKNOWN",
        "verified": False,
        "evidence": [],
    }

    snapshot = await fetch(
        url
    )

    if snapshot.error:
        return result

    snapshot = parse_snapshot(
        snapshot
    )

    result["canonical"] = (
        snapshot.canonical
        or snapshot.final_url
        or url
    )

    result["final_url"] = (
        snapshot.final_url
        or url
    )

    result["title"] = (
        snapshot.title
    )

    evidence = []

    # --------------------------------------------------------
    # Semantic HTML
    # --------------------------------------------------------

    collect_semantic_evidence(
        snapshot.html,
        evidence,
        "profile_html",
        snapshot.final_url,
    )

    # --------------------------------------------------------
    # Embedded JSON
    # --------------------------------------------------------

    collect_json_semantic_evidence(
        snapshot.embedded_json,
        evidence,
        snapshot.final_url,
        "profile_embedded_json",
    )

    # --------------------------------------------------------
    # JSON-LD
    # --------------------------------------------------------

    collect_json_semantic_evidence(
        snapshot.jsonld,
        evidence,
        snapshot.final_url,
        "profile_jsonld",
    )

    # --------------------------------------------------------
    # Direct profile.php
    # --------------------------------------------------------

    p = urlparse(
        snapshot.final_url
    )

    query = parse_qs(
        p.query
    )

    direct_id = first_query(
        query,
        "id",
    )

    if is_long_numeric(
        direct_id
    ):

        add_evidence(
            evidence,
            direct_id,
            "USER_CANDIDATE",
            "profile_final_url",
            200,
            context=snapshot.final_url,
            url=snapshot.final_url,
            path="profile.php?id",
            key_name="profile_id",
            independent="profile_final_url",
            verified=True,
        )

    # --------------------------------------------------------
    # Canonical profile
    # --------------------------------------------------------

    if snapshot.canonical:

        canonical_shape = classify_url(
            snapshot.canonical
        )

        if canonical_shape.kind == "PROFILE":

            uid = (
                canonical_shape.numeric_path_id
            )

            if is_long_numeric(
                uid
            ):

                add_evidence(
                    evidence,
                    uid,
                    "USER_CANDIDATE",
                    "profile_canonical",
                    205,
                    context=snapshot.canonical,
                    url=snapshot.canonical,
                    path="canonical.profile",
                    key_name="profile_id",
                    independent="profile_canonical",
                    verified=True,
                )

    # --------------------------------------------------------
    # Determine explicit type conservatively.
    # --------------------------------------------------------

    combined = (
        snapshot.title
        + " "
        + " ".join(
            snapshot.meta.get(
                "og:type",
                [],
            )
        )
    ).lower()

    if (
        "/pages/"
        in snapshot.final_url.lower()
        or "business" in combined
        or "organization" in combined
    ):

        result["type"] = "PAGE"

    elif (
        "profile"
        in combined
        or '"person"'
        in combined
        or "person"
        in combined
    ):

        result["type"] = "USER"

    # --------------------------------------------------------
    # Candidate UID hits.
    # --------------------------------------------------------

    for ev in evidence:

        if (
            ev.role == "USER_CANDIDATE"
            and is_long_numeric(
                ev.value
            )
        ):

            result["uid_hits"].add(
                ev.value
            )

    # --------------------------------------------------------
    # Expected UID.
    # --------------------------------------------------------

    if expected_uid:

        expected_uid = str(
            expected_uid
        )

        matching = [
            ev
            for ev in evidence
            if (
                ev.role
                == "USER_CANDIDATE"
                and ev.value
                == expected_uid
            )
        ]

        # We require actual profile evidence,
        # not merely arbitrary HTML occurrence.
        if matching:

            strong = [
                ev
                for ev in matching
                if ev.verified
                or ev.key_name
                in {
                    "user_id",
                    "profile_id",
                    "owner_id",
                    "publisher_id",
                    "author_id",
                }
                or "profile"
                in ev.path.lower()
            ]

            if strong:

                result["verified"] = True

    result["evidence"] = evidence

    return result


# ============================================================
# FETCH ENGINE
# ============================================================

def fetch_sync(
    url: str,
    user_agent: str,
) -> Snapshot:

    snapshot = Snapshot(
        requested_url=url
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

    started = time.monotonic()

    try:

        session = requests.Session()

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
            response.url
            or url
        )

        snapshot.headers = dict(
            response.headers
        )

        snapshot.redirects = [
            r.url
            for r in response.history
            if r.url
        ]

        chunks = []

        total = 0

        for chunk in response.iter_content(
            chunk_size=65536
        ):

            if not chunk:
                continue

            total += len(
                chunk
            )

            if total > MAX_HTML_BYTES:

                remain = (
                    MAX_HTML_BYTES
                    - (
                        total
                        - len(chunk)
                    )
                )

                if remain > 0:
                    chunks.append(
                        chunk[:remain]
                    )

                break

            chunks.append(
                chunk
            )

        raw = b"".join(
            chunks
        )

        encoding = (
            response.encoding
            or "utf-8"
        )

        snapshot.html = raw.decode(
            encoding,
            errors="replace",
        )

        log.debug(
            "FB fetch %.2fs %s -> %s",
            time.monotonic()
            - started,
            url,
            snapshot.final_url,
        )

        return snapshot

    except Exception as exc:

        snapshot.error = (
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        return snapshot


async def fetch(
    url: str
) -> Snapshot:

    last = None

    for index, ua in enumerate(
        USER_AGENTS
    ):

        snapshot = await asyncio.to_thread(
            fetch_sync,
            url,
            ua,
        )

        last = snapshot

        # Successful response or meaningful FB response.
        if snapshot.status_code in {
            200,
            301,
            302,
            403,
            404,
        }:

            return snapshot

        await asyncio.sleep(
            0.25
            * (
                index + 1
            )
        )

    return (
        last
        or Snapshot(
            requested_url=url,
            error="HTTP request failed",
        )
    )


# ============================================================
# SNAPSHOT PARSER
# ============================================================

def parse_snapshot(
    snapshot: Snapshot
) -> Snapshot:

    parser = FacebookHTMLParser()

    try:

        parser.feed(
            snapshot.html
        )

    except Exception as exc:

        log.debug(
            "HTML parse error: %s",
            exc,
        )

    snapshot.title = (
        parser.title
    )

    snapshot.meta = (
        parser.meta
    )

    snapshot.links = (
        parser.links[:]
    )

    snapshot.jsonld = (
        parser.jsonld[:]
    )

    snapshot.embedded_json = (
        parser.embedded_json[
            :MAX_JSON_SCRIPTS
        ]
    )

    canonical_values = (
        parser.meta.get(
            "__canonical__",
            [],
        )
        + parser.meta.get(
            "og:url",
            [],
        )
        + parser.meta.get(
            "twitter:url",
            [],
        )
    )

    for value in canonical_values:

        candidate = normalize_discovered_url(
            value,
            snapshot.final_url,
        )

        if candidate:

            snapshot.canonical = (
                candidate
            )

            break

    return snapshot


# ============================================================
# ENTITY TYPE
# ============================================================

def infer_entity_type(
    shape: URLShape,
    resolved_url: str,
) -> str:

    url = (
        resolved_url
        or shape.normalized
        or ""
    )

    low = url.lower()

    if shape.kind == "GROUP_POST":
        return "GROUP_POST"

    if shape.kind == "GROUP":
        return "GROUP"

    if shape.kind == "PAGE":
        return "PAGE"

    if shape.kind in {
        "REEL",
        "SHARE_REEL",
    }:
        return "REEL"

    if shape.kind in {
        "VIDEO",
        "SHARE_VIDEO",
    }:
        return "VIDEO"

    if shape.kind == "PHOTO":
        return "PHOTO"

    if shape.kind in {
        "STORY",
        "SHARE_STORY",
    }:
        return "STORY"

    if shape.kind in {
        "POST",
        "USER_POST",
        "SHARE_POST",
    }:
        return "POST"

    parts = [
        x.lower()
        for x in urlparse(
            url
        ).path.split("/")
        if x
    ]

    if not parts:
        return "UNKNOWN"

    if parts[0] in {
        "reel",
        "reels",
    }:
        return "REEL"

    if parts[0] in {
        "video",
        "videos",
        "watch",
    }:
        return "VIDEO"

    if parts[0] in {
        "photo",
        "photos",
    }:
        return "PHOTO"

    if parts[0] in {
        "story",
        "stories",
    }:
        return "STORY"

    if parts[0] == "groups":
        return "GROUP_POST"

    if len(parts) >= 2:

        if parts[1] == "posts":
            return "POST"

        if parts[1] in {
            "videos",
            "video",
        }:
            return "VIDEO"

        if parts[1] in {
            "reel",
            "reels",
        }:
            return "REEL"

    return "UNKNOWN"


# ============================================================
# PUBLISHER TYPE
# ============================================================

def infer_publisher_type(
    result: ResolveResult,
    snapshots: List[Snapshot],
) -> str:

    if result.group_id:
        return "GROUP"

    if result.page_uid:
        return "PAGE"

    if result.user_uid:
        return "USER"

    # Explicit URL.
    if result.shape.kind in {
        "GROUP",
        "GROUP_POST",
    }:
        return "GROUP"

    if result.shape.kind == "PAGE":
        return "PAGE"

    # Conservative metadata.
    combined = ""

    for snapshot in snapshots:

        combined += " "

        combined += snapshot.title

        combined += " "

        combined += " ".join(
            snapshot.meta.get(
                "og:type",
                [],
            )
        )

    low = combined.lower()

    if "business" in low:
        return "PAGE"

    if "organization" in low:
        return "PAGE"

    return "UNKNOWN"


# ============================================================
# USERNAME
# ============================================================

def extract_username_from_url(
    url: str
) -> Optional[str]:

    if not url:
        return None

    try:

        p = urlparse(
            url
        )

        parts = [
            unquote(x)
            for x in p.path.split("/")
            if x
        ]

        if not parts:
            return None

        first = parts[0]

        if first.lower() in (
            PROFILE_EXCLUDE
            | {
                "profile.php",
            }
        ):
            return None

        if first.isdigit():
            return None

        return first

    except Exception:

        return None


# ============================================================
# PROFILE CORRELATION
# ============================================================

def add_profile_verification(
    evidence: List[Evidence],
    candidate: str,
    verified: Dict[str, Any],
):

    if not verified.get(
        "verified"
    ):
        return

    canonical = (
        verified.get(
            "canonical"
        )
        or verified.get(
            "final_url"
        )
        or verified.get(
            "url"
        )
        or ""
    )

    add_evidence(
        evidence=evidence,
        value=candidate,
        role="USER_CANDIDATE",
        source="profile_verification",
        weight=240,
        context=canonical,
        url=verified.get(
            "url",
            "",
        ),
        path="verified.profile",
        key_name="profile_id",
        independent=(
            "profile_verification"
        ),
        verified=True,
    )


# ============================================================
# CORRELATION ENGINE
# ============================================================

def correlate_route_entity(
    evidence: List[Evidence],
    shape: URLShape,
):

    route_id = (
        shape.route_entity_id
    )

    if not route_id:
        return

    # Route ID must NEVER automatically become UID.
    # We only create a weak relation when another semantic
    # source independently identifies the same value.
    for ev in list(evidence):

        if (
            ev.value == route_id
            and ev.role == "USER_CANDIDATE"
        ):

            add_evidence(
                evidence,
                route_id,
                "USER_CANDIDATE",
                "route_entity_correlation",
                35,
                context=shape.path,
                url=shape.normalized,
                path="route_entity -> semantic_user",
                key_name=ev.key_name,
                independent="route_entity_correlation",
            )


# ============================================================
# RESOLVE ONE URL
# ============================================================

async def resolve_url(
    input_url: str,
) -> ResolveResult:

    started = time.monotonic()

    shape = classify_url(
        input_url
    )

    result = ResolveResult(
        input_url=input_url,
        shape=shape,
    )

    evidence: List[Evidence] = []

    snapshots: List[Snapshot] = []

    # --------------------------------------------------------
    # Input structure
    # --------------------------------------------------------

    add_structural_evidence(
        shape,
        evidence,
    )

    extract_canonical_structure(
        input_url,
        evidence,
    )

    # --------------------------------------------------------
    # First fetch
    # --------------------------------------------------------

    first = await fetch(
        input_url
    )

    if first.error:

        result.status = "HTTP_ERROR"

        result.notes.append(
            first.error
        )

        result.elapsed = (
            time.monotonic()
            - started
        )

        return finalize_result(
            result,
            evidence,
            snapshots,
        )

    first = parse_snapshot(
        first
    )

    snapshots.append(
        first
    )

    # --------------------------------------------------------
    # Redirects
    # --------------------------------------------------------

    result.resolved_url = (
        first.final_url
        or input_url
    )

    result.redirects = (
        first.redirects[:]
    )

    for redirect_url in (
        first.redirects
    ):

        redirect_shape = classify_url(
            redirect_url
        )

        add_structural_evidence(
            redirect_shape,
            evidence,
        )

        extract_canonical_structure(
            redirect_url,
            evidence,
        )

    # Final URL structure.
    final_shape = classify_url(
        result.resolved_url
    )

    add_structural_evidence(
        final_shape,
        evidence,
    )

    extract_canonical_structure(
        result.resolved_url,
        evidence,
    )

    # --------------------------------------------------------
    # Canonical
    # --------------------------------------------------------

    result.canonical_url = (
        first.canonical
        or first.final_url
        or input_url
    )

    extract_canonical_structure(
        result.canonical_url,
        evidence,
    )

    canonical_shape = classify_url(
        result.canonical_url
    )

    add_structural_evidence(
        canonical_shape,
        evidence,
    )

    # --------------------------------------------------------
    # Title
    # --------------------------------------------------------

    result.title = (
        first.title
        or first.meta.get(
            "og:title",
            [""],
        )[0]
        or ""
    )

    # --------------------------------------------------------
    # Semantic HTML
    # --------------------------------------------------------

    collect_semantic_evidence(
        first.html,
        evidence,
        "html_semantic",
        first.final_url,
        shape.opaque_token,
    )

    # --------------------------------------------------------
    # META
    # --------------------------------------------------------

    for key, values in (
        first.meta.items()
    ):

        if key == "__canonical__":
            continue

        for value in values:

            collect_semantic_evidence(
                str(value),
                evidence,
                f"meta:{key}",
                first.final_url,
                shape.opaque_token,
            )

    # --------------------------------------------------------
    # Embedded Facebook JSON
    # --------------------------------------------------------

    collect_json_semantic_evidence(
        first.embedded_json,
        evidence,
        first.final_url,
        "embedded_json",
    )

    # --------------------------------------------------------
    # JSON-LD
    # --------------------------------------------------------

    collect_json_semantic_evidence(
        first.jsonld,
        evidence,
        first.final_url,
        "jsonld",
    )

    # --------------------------------------------------------
    # Route correlation
    # --------------------------------------------------------

    correlate_route_entity(
        evidence,
        shape,
    )

    # --------------------------------------------------------
    # Discover URLs
    # --------------------------------------------------------

    discovered = discover_facebook_urls(
        first
    )

    # --------------------------------------------------------
    # Profile URLs
    # --------------------------------------------------------

    profile_urls = extract_profile_urls(
        first
    )

    # Username route:
    # only use non-numeric username.
    if shape.username:

        candidate = (
            "https://www.facebook.com/"
            + quote(
                shape.username,
                safe="@._-",
            )
        )

        if (
            candidate
            not in profile_urls
        ):

            profile_urls.append(
                candidate
            )

    # Canonical profile.
    if canonical_shape.kind in {
        "PROFILE",
        "PROFILE_OR_PAGE",
    }:

        if (
            result.canonical_url
            not in profile_urls
        ):

            profile_urls.insert(
                0,
                result.canonical_url,
            )

    profile_urls = profile_urls[
        :MAX_PROFILE_CHECKS
    ]

    # --------------------------------------------------------
    # Build initial candidates.
    # --------------------------------------------------------

    candidates = build_user_candidates(
        evidence
    )

    preliminary = sorted(
        candidates.values(),
        key=lambda row: row.score,
        reverse=True,
    )[:MAX_CANDIDATES]

    # --------------------------------------------------------
    # Profile verification.
    #
    # Only verify candidates that already have semantic
    # identity evidence. A random numeric ID is not promoted.
    # --------------------------------------------------------

    checks = 0

    for candidate in preliminary:

        if checks >= MAX_PROFILE_CHECKS:
            break

        conflicts = detect_role_conflicts(
            candidate.value,
            evidence,
        )

        candidate.conflicts.update(
            conflicts
        )

        # Pure page/group/album candidate is not enough.
        if (
            conflicts
            and not (
                "page_id"
                in conflicts
                and "profile_id"
                in candidate.fields
            )
        ):
            continue

        urls_to_check = []

        urls_to_check.extend(
            profile_urls
        )

        # Numeric profile endpoint is useful,
        # but only for an existing semantic candidate.
        urls_to_check.append(
            "https://www.facebook.com/profile.php?id="
            + candidate.value
        )

        checked_candidate = False

        seen_urls = set()

        for profile_url in urls_to_check:

            if checks >= MAX_PROFILE_CHECKS:
                break

            profile_url = normalize_url(
                profile_url
            )

            if profile_url in seen_urls:
                continue

            seen_urls.add(
                profile_url
            )

            verified = await verify_profile_url(
                profile_url,
                candidate.value,
            )

            checks += 1

            if verified.get(
                "verified"
            ):

                candidate.verified = True

                candidate.correlations.append(
                    "profile_verification"
                )

                add_profile_verification(
                    evidence,
                    candidate.value,
                    verified,
                )

                checked_candidate = True

                break

        if checked_candidate:
            break

    # --------------------------------------------------------
    # Rebuild graph after verification.
    # --------------------------------------------------------

    candidates = build_user_candidates(
        evidence
    )

    # --------------------------------------------------------
    # Entity/object IDs
    # --------------------------------------------------------

    result.page_uid = best_object_id(
        evidence,
        "page_id",
    )

    result.group_id = best_object_id(
        evidence,
        "group_id",
    )

    result.post_id = best_object_id(
        evidence,
        "post_id",
    )

    result.video_id = best_object_id(
        evidence,
        "video_id",
    )

    result.reel_id = best_object_id(
        evidence,
        "reel_id",
    )

    result.photo_id = best_object_id(
        evidence,
        "photo_id",
    )

    result.story_id = best_object_id(
        evidence,
        "story_fbid",
    )

    result.album_id = best_object_id(
        evidence,
        "album_id",
    )

    result.event_id = best_object_id(
        evidence,
        "event_id",
    )

    result.route_entity_id = (
        shape.route_entity_id
    )

    # --------------------------------------------------------
    # Entity type
    # --------------------------------------------------------

    result.object_type = infer_entity_type(
        shape,
        result.canonical_url,
    )

    # --------------------------------------------------------
    # Select USER UID
    # --------------------------------------------------------

    page_group_values = {
        value
        for value in (
            result.page_uid,
            result.group_id,
            result.album_id,
            result.event_id,
        )
        if value
    }

    valid = []

    for value, row in candidates.items():

        if value in page_group_values:
            continue

        conflicts = detect_role_conflicts(
            value,
            evidence,
        )

        # A candidate used only as page/group/album
        # cannot become USER.
        if conflicts:

            # Allow only when there is strong independent
            # profile verification.
            if not row.verified:
                continue

        valid.append(
            row
        )

    valid.sort(
        key=lambda x: x.score,
        reverse=True,
    )

    if valid:

        top = valid[0]

        second = (
            valid[1]
            if len(valid) > 1
            else None
        )

        ambiguous = False

        if second:

            # Close candidates are a conflict.
            if (
                second.score
                >= top.score * 0.82
            ):
                ambiguous = True

        # ----------------------------------------------------
        # Strict verification rules
        # ----------------------------------------------------

        if (
            top.verified
            and not ambiguous
        ):

            result.user_uid = (
                top.value
            )

        elif (
            not ambiguous
            and top.score >= 260
            and len(
                top.independent_sources
            ) >= 2
            and len(
                top.fields
            ) >= 2
        ):

            # High-confidence semantic correlation
            # without direct profile verification.
            result.user_uid = (
                top.value
            )

        else:

            result.user_uid = None

    # --------------------------------------------------------
    # Username
    # --------------------------------------------------------

    result.username = (
        shape.username
        or extract_username_from_url(
            result.canonical_url
        )
    )

    # --------------------------------------------------------
    # Publisher
    # --------------------------------------------------------

    result.publisher_type = (
        infer_publisher_type(
            result,
            snapshots,
        )
    )

    # --------------------------------------------------------
    # Reel/video relation
    # --------------------------------------------------------

    if (
        result.object_type == "REEL"
        and not result.reel_id
        and result.video_id
    ):

        result.reel_id = (
            result.video_id
        )

    if (
        result.object_type == "REEL"
        and not result.video_id
        and result.reel_id
    ):

        result.video_id = (
            result.reel_id
        )

    # --------------------------------------------------------
    # Final
    # --------------------------------------------------------

    result.elapsed = (
        time.monotonic()
        - started
    )

    return finalize_result(
        result,
        evidence,
        snapshots,
    )


# ============================================================
# FINAL RESULT
# ============================================================

def finalize_result(
    result: ResolveResult,
    evidence: List[Evidence],
    snapshots: List[Snapshot],
) -> ResolveResult:

    # --------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------

    unique = {}

    for ev in evidence:

        key = ev.key()

        if key not in unique:

            unique[key] = ev

        else:

            if ev.verified:

                unique[key].verified = True

    result.evidence = list(
        unique.values()
    )

    # --------------------------------------------------------
    # USER candidate evidence
    # --------------------------------------------------------

    user_evidence = [
        ev
        for ev in result.evidence
        if ev.role == "USER_CANDIDATE"
    ]

    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------

    if result.user_uid:

        selected = [
            ev
            for ev in user_evidence
            if ev.value
            == result.user_uid
        ]

        independent_sources = {
            ev.independent
            or ev.source
            for ev in selected
        }

        fields = {
            ev.key_name
            or ev.path
            for ev in selected
        }

        verified = any(
            ev.verified
            for ev in selected
        )

        score = sum(
            ev.weight
            for ev in selected
        )

        confidence = 45

        if len(
            independent_sources
        ) >= 2:
            confidence += 15

        if len(
            independent_sources
        ) >= 3:
            confidence += 10

        if len(
            fields
        ) >= 2:
            confidence += 10

        if len(
            fields
        ) >= 3:
            confidence += 5

        if verified:
            confidence += 15

        if score >= 350:
            confidence += 5

        result.confidence = min(
            99,
            confidence,
        )

        result.status = (
            "VERIFIED"
            if verified
            else "LIKELY"
        )

    else:

        # No UID exposed.
        # Structural confidence is deliberately capped.
        object_sources = {
            ev.independent
            or ev.source
            for ev in result.evidence
            if ev.role in {
                "post_id",
                "video_id",
                "reel_id",
                "photo_id",
                "story_fbid",
                "page_id",
                "group_id",
                "album_id",
            }
        }

        result.confidence = min(
            85,
            30
            + len(
                object_sources
            ) * 8,
        )

        result.status = (
            "NOT_VERIFIED"
        )

        result.notes.append(
            "Không đủ bằng chứng độc lập "
            "để xác minh USER UID."
        )

    # --------------------------------------------------------
    # Redirect note
    # --------------------------------------------------------

    if (
        result.input_url
        != result.resolved_url
        and result.resolved_url
    ):

        result.notes.insert(
            0,
            "URL đã được Facebook redirect; "
            "redirect chain được giữ lại làm evidence.",
        )

    # --------------------------------------------------------
    # Opaque token
    # --------------------------------------------------------

    if result.shape.opaque_token:

        result.notes.append(
            "Opaque token/pfbid chỉ được dùng "
            "để correlation với object; "
            "không giải mã toán học thành UID."
        )

    # --------------------------------------------------------
    # Numeric route entity
    # --------------------------------------------------------

    if result.route_entity_id:

        result.notes.append(
            "Numeric route entity không được "
            "tự động coi là USER UID."
        )

    # --------------------------------------------------------
    # Group
    # --------------------------------------------------------

    if (
        result.group_id
        and not result.user_uid
    ):

        result.notes.append(
            "GROUP ID được giữ riêng và "
            "không được chuyển thành USER UID."
        )

    # --------------------------------------------------------
    # Page
    # --------------------------------------------------------

    if (
        result.page_uid
        and not result.user_uid
    ):

        result.notes.append(
            "PAGE ID được giữ riêng và "
            "không được chuyển thành USER UID."
        )

    # --------------------------------------------------------
    # Conflict detection
    # --------------------------------------------------------

    uid_values = {}

    for ev in user_evidence:

        uid_values.setdefault(
            ev.value,
            0,
        )

        uid_values[
            ev.value
        ] += 1

    if len(
        uid_values
    ) > 1:

        ranked = sorted(
            uid_values.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        if (
            len(ranked) >= 2
            and ranked[0][1]
            == ranked[1][1]
        ):

            result.notes.append(
                "IDENTITY CONFLICT: "
                "nhiều USER candidate có mức "
                "evidence tương đương."
            )

            if result.status == "LIKELY":

                result.user_uid = None

                result.status = (
                    "IDENTITY_CONFLICT"
                )

    return result


# ============================================================
# DISPLAY
# ============================================================

def esc(
    value: Any
) -> str:

    if value is None:
        return ""

    return html_lib.escape(
        str(value),
        quote=False,
    )


def truncate(
    value: str,
    limit: int,
) -> str:

    value = value or ""

    if len(value) <= limit:
        return value

    return (
        value[
            :limit - 1
        ]
        + "…"
    )


def evidence_summary(
    result: ResolveResult,
) -> Dict[str, int]:

    counts = {
        "user_id": 0,
        "profile_id": 0,
        "owner": 0,
        "publisher": 0,
        "author": 0,
        "creator": 0,
        "actor": 0,
        "page": 0,
        "group": 0,
        "route": 0,
    }

    for ev in result.evidence:

        key = (
            ev.key_name
            or ev.path
            or ""
        ).lower()

        source = (
            ev.source
            or ""
        ).lower()

        if (
            "user_id"
            in key
            or "user_id"
            in source
        ):
            counts["user_id"] += 1

        elif (
            "profile"
            in key
            or "profile"
            in source
        ):
            counts["profile_id"] += 1

        elif (
            "owner"
            in key
            or "owner"
            in source
        ):
            counts["owner"] += 1

        elif (
            "publisher"
            in key
            or "publisher"
            in source
        ):
            counts["publisher"] += 1

        elif (
            "author"
            in key
            or "author"
            in source
        ):
            counts["author"] += 1

        elif (
            "creator"
            in key
            or "creator"
            in source
        ):
            counts["creator"] += 1

        elif (
            "actor"
            in key
            or "actor"
            in source
        ):
            counts["actor"] += 1

        if ev.role == "page_id":
            counts["page"] += 1

        if ev.role == "group_id":
            counts["group"] += 1

        if ev.role == "ROUTE_ENTITY":
            counts["route"] += 1

    return counts


def build_result_text(
    result: ResolveResult,
    index: int,
    total: int,
) -> str:

    lines = []

    lines.append(
        f"🔎 <b>FACEBOOK RESOLVER V23 FORENSIC</b> "
        f"[{index}/{total}]"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🌐 <b>OBJECT TYPE:</b> "
        f"<code>{esc(result.object_type)}</code>"
    )

    lines.append(
        "📦 <b>PUBLISHER TYPE:</b> "
        f"<code>{esc(result.publisher_type)}</code>"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "👤 <b>PUBLISHER</b>"
    )

    if result.username:

        lines.append(
            "USERNAME: "
            f"<code>@"
            f"{esc(result.username.lstrip('@'))}"
            f"</code>"
        )

    if result.user_uid:

        lines.append(
            "🆔 <b>USER UID:</b> "
            f"<code>{esc(result.user_uid)}</code>"
        )

    else:

        lines.append(
            "🆔 <b>USER UID:</b> "
            "<code>NOT VERIFIED</code>"
        )

    if result.page_uid:

        lines.append(
            "📄 <b>PAGE ID:</b> "
            f"<code>{esc(result.page_uid)}</code>"
        )

    if result.group_id:

        lines.append(
            "👥 <b>GROUP ID:</b> "
            f"<code>{esc(result.group_id)}</code>"
        )

    if result.route_entity_id:

        lines.append(
            "🔢 <b>ROUTE ENTITY:</b> "
            f"<code>{esc(result.route_entity_id)}</code>"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📦 <b>OBJECT IDS</b>"
    )

    if result.post_id:

        lines.append(
            "📝 POST ID: "
            f"<code>{esc(result.post_id)}</code>"
        )

    if result.video_id:

        lines.append(
            "🎬 VIDEO ID: "
            f"<code>{esc(result.video_id)}</code>"
        )

    if result.reel_id:

        lines.append(
            "🎞 REEL ID: "
            f"<code>{esc(result.reel_id)}</code>"
        )

    if result.photo_id:

        lines.append(
            "🖼 PHOTO ID: "
            f"<code>{esc(result.photo_id)}</code>"
        )

    if result.story_id:

        lines.append(
            "⭕ STORY ID: "
            f"<code>{esc(result.story_id)}</code>"
        )

    if result.album_id:

        lines.append(
            "🗂 ALBUM ID: "
            f"<code>{esc(result.album_id)}</code>"
        )

    if result.event_id:

        lines.append(
            "📅 EVENT ID: "
            f"<code>{esc(result.event_id)}</code>"
        )

    if result.title:

        lines.append(
            "━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            "📛 <b>TITLE:</b> "
            + esc(
                truncate(
                    result.title,
                    500,
                )
            )
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🔐 <b>VERIFICATION</b>"
    )

    lines.append(
        "STATUS: "
        f"<b>{esc(result.status)}</b>"
    )

    lines.append(
        "CONFIDENCE: "
        f"<code>{result.confidence}%</code>"
    )

    independent = len({
        ev.independent
        or ev.source
        for ev in result.evidence
        if ev.role
        in {
            "USER_CANDIDATE",
            "post_id",
            "video_id",
            "reel_id",
            "photo_id",
            "story_fbid",
            "page_id",
            "group_id",
            "album_id",
        }
    })

    lines.append(
        "INDEPENDENT EVIDENCE: "
        f"<code>{independent}</code>"
    )

    summary = evidence_summary(
        result
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🔬 <b>UID EVIDENCE</b>"
    )

    lines.append(
        f"• user_id: {summary['user_id']}"
    )

    lines.append(
        f"• profile_id: {summary['profile_id']}"
    )

    lines.append(
        f"• owner: {summary['owner']}"
    )

    lines.append(
        f"• publisher: {summary['publisher']}"
    )

    lines.append(
        f"• author: {summary['author']}"
    )

    lines.append(
        f"• creator: {summary['creator']}"
    )

    lines.append(
        f"• actor: {summary['actor']}"
    )

    lines.append(
        f"• route entity: {summary['route']}"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    if result.resolved_url:

        lines.append(
            "🔗 <b>RESOLVED:</b>"
        )

        lines.append(
            esc(
                result.resolved_url
            )
        )

    if result.canonical_url:

        lines.append(
            "🔗 <b>CANONICAL:</b>"
        )

        lines.append(
            esc(
                result.canonical_url
            )
        )

    if result.redirects:

        lines.append(
            "🔁 <b>REDIRECT CHAIN:</b>"
        )

        for redirect in result.redirects[
            :5
        ]:

            lines.append(
                "• "
                + esc(
                    redirect
                )
            )

    if result.notes:

        lines.append(
            "━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            "⚠️ <b>NOTES</b>"
        )

        seen_notes = set()

        for note in result.notes:

            if note in seen_notes:
                continue

            seen_notes.add(
                note
            )

            lines.append(
                "• "
                + esc(note)
            )

            if len(seen_notes) >= 7:
                break

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"⏱ TIME: "
        f"<code>{result.elapsed:.2f}s</code>"
    )

    return "\n".join(
        lines
    )


# ============================================================
# OPTIONAL FORENSIC EVIDENCE TEXT
# ============================================================

def build_forensic_evidence_text(
    result: ResolveResult,
) -> str:

    lines = []

    lines.append(
        "🔬 <b>FORENSIC EVIDENCE</b>"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    grouped = {}

    for ev in result.evidence:

        grouped.setdefault(
            ev.value,
            [],
        ).append(
            ev
        )

    for value, items in sorted(
        grouped.items(),
        key=lambda x: (
            x[0]
        ),
    ):

        lines.append(
            f"🆔 <code>{esc(value)}</code>"
        )

        for ev in items[:12]:

            verified = (
                " ✓"
                if ev.verified
                else ""
            )

            label = (
                ev.key_name
                or ev.role
            )

            lines.append(
                "• "
                f"{esc(label)}"
                " | "
                f"{esc(ev.source)}"
                f"{verified}"
            )

        lines.append(
            ""
        )

    return "\n".join(
        lines
    )


# ============================================================
# TELEGRAM SESSION
# ============================================================

_ACTIVE_SESSIONS: Dict[
    Tuple[int, int],
    bool,
] = {}

_SESSION_LOCK = asyncio.Lock()


async def session_key(
    event,
) -> Tuple[int, int]:

    return (
        int(
            event.sender_id
            or 0
        ),
        int(
            event.chat_id
            or 0
        ),
    )


async def acquire_session(
    event
) -> bool:

    key = await session_key(
        event
    )

    async with _SESSION_LOCK:

        if _ACTIVE_SESSIONS.get(
            key
        ):

            return False

        _ACTIVE_SESSIONS[
            key
        ] = True

        return True


async def release_session(
    event
):

    key = await session_key(
        event
    )

    async with _SESSION_LOCK:

        _ACTIVE_SESSIONS.pop(
            key,
            None,
        )


async def wait_for_next_message(
    bot,
    source_event,
    timeout: int = SESSION_TIMEOUT,
):

    loop = (
        asyncio.get_running_loop()
    )

    future = loop.create_future()

    chat_id = (
        source_event.chat_id
    )

    sender_id = (
        source_event.sender_id
    )

    async def waiter(
        event
    ):

        if future.done():
            return

        if event.chat_id != chat_id:
            return

        if event.sender_id != sender_id:
            return

        if not event.raw_text:
            return

        future.set_result(
            event
        )

    builder = events.NewMessage(
        chats=chat_id
    )

    bot.add_event_handler(
        waiter,
        builder,
    )

    try:

        return await asyncio.wait_for(
            future,
            timeout=timeout,
        )

    except asyncio.TimeoutError:

        return None

    finally:

        bot.remove_event_handler(
            waiter,
            builder,
        )


# ============================================================
# COMMAND HANDLER
# ============================================================

async def _handle_getuidfb(
    event,
    notify_bot=None,
):

    if not await acquire_session(
        event
    ):

        await event.respond(
            "⚠️ Bạn đang có một phiên "
            "/getuidfb đang chạy."
        )

        return

    try:

        await event.respond(
            "🔎 <b>FACEBOOK RESOLVER V23 FORENSIC</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📥 Gửi URL Facebook công khai cần phân tích.\n\n"
            "Hỗ trợ:\n"
            "• 1 URL\n"
            "• nhiều URL\n"
            "• xuống dòng\n"
            "• khoảng trắng\n"
            "• dấu phẩy\n"
            "• URL dính trực tiếp\n\n"
            "Ví dụ:\n"
            "<code>"
            "https://facebook.com/share/p/AAA/"
            "https://facebook.com/user/posts/pfbidBBB"
            "</code>\n\n"
            "🔬 UID chỉ được xuất khi đủ evidence.\n"
            "⛔ ID ngẫu nhiên không được biến thành UID.\n"
            "⏹ /stop để kết thúc.",
            parse_mode="html",
        )

        while True:

            next_event = (
                await wait_for_next_message(
                    bot=event.client,
                    source_event=event,
                )
            )

            if next_event is None:

                await event.respond(
                    "⏱ Phiên /getuidfb đã hết thời gian chờ.\n"
                    "Gửi /getuidfb để bắt đầu lại."
                )

                break

            text = (
                next_event.raw_text
                or ""
            ).strip()

            # ------------------------------------------------
            # Do not consume normal bot commands.
            # ------------------------------------------------

            if text.startswith("/"):

                command = (
                    text.split(
                        None,
                        1,
                    )[0]
                    .lower()
                    .split(
                        "@",
                        1,
                    )[0]
                )

                if command in {
                    "/stop",
                    "/start",
                    "/download",
                    "/cupdien",
                    "/tiktok",
                    "/gettoken",
                    "/checkliveuid",
                    "/getinfoprofile",
                    "/getuidfb",
                }:

                    if command == "/stop":

                        await next_event.respond(
                            "⏹ Đã kết thúc phiên "
                            "/getuidfb."
                        )

                    break

            urls = extract_facebook_urls(
                text
            )

            if not urls:

                await next_event.respond(
                    "❌ Không tìm thấy URL Facebook hợp lệ.\n"
                    "Hãy gửi link Facebook công khai."
                )

                continue

            await next_event.respond(
                f"🔗 Đã nhận diện "
                f"<b>{len(urls)}</b> URL.\n"
                "🔬 Đang phân tích theo evidence graph...",
                parse_mode="html",
            )

            total = len(
                urls
            )

            success = 0

            for index, url in enumerate(
                urls,
                1,
            ):

                try:

                    result = await resolve_url(
                        url
                    )

                    message = build_result_text(
                        result,
                        index,
                        total,
                    )

                    await next_event.respond(
                        message,
                        parse_mode="html",
                        link_preview=False,
                    )

                    success += 1

                except Exception as exc:

                    log.exception(
                        "Facebook resolver error"
                    )

                    await next_event.respond(
                        "❌ <b>Resolver error</b>\n"
                        f"<code>"
                        f"{esc(type(exc).__name__)}"
                        f"</code>: "
                        f"{esc(str(exc))}",
                        parse_mode="html",
                    )

            await next_event.respond(
                "━━━━━━━━━━━━━━━━━━\n"
                f"✅ Đã xử lý "
                f"<b>{success}/{total}</b> URL.\n"
                "🔁 Gửi URL tiếp theo để tiếp tục."
            )

    finally:

        await release_session(
            event
        )


# ============================================================
# OPTIONAL DIRECT COMMAND
# ============================================================

async def _handle_getuidfb_inline(
    event,
):

    text = (
        event.raw_text
        or ""
    )

    urls = extract_facebook_urls(
        text
    )

    if not urls:
        return

    for index, url in enumerate(
        urls,
        1,
    ):

        try:

            result = await resolve_url(
                url
            )

            await event.respond(
                build_result_text(
                    result,
                    index,
                    len(urls),
                ),
                parse_mode="html",
                link_preview=False,
            )

        except Exception as exc:

            await event.respond(
                "❌ Resolver error: "
                + esc(
                    str(exc)
                ),
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
    Compatible with:

        commands/__init__.py

    Expected:

        module.register(bot, notify_bot)

    Telethon passes one event argument.
    notify_bot is captured by closure.
    """

    async def handler(
        event
    ):

        await _handle_getuidfb(
            event,
            notify_bot,
        )

    bot.add_event_handler(
        handler,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?$"
        ),
    )

    print(
        "   ↳ /getuidfb → "
        "Facebook Resolver V23 Forensic"
    )


# ============================================================
# COMMAND INFO
# ============================================================

COMMAND_INFO = {
    "command": "getuidfb",

    "description": (
        "Facebook public URL / UID / entity "
        "forensic resolver V23"
    ),

    "version": "23.0.0",

    "engine": (
        "HTTP + Redirect + HTML + META + "
        "Canonical + JSON-LD + Embedded JSON + "
        "Evidence Graph"
    ),

    "login_required": False,

    "cookies_required": False,

    "playwright_required": False,

    "selenium_required": False,

    "chromium_required": False,

    "access_token_required": False,

    "precision_first": True,
}


# ============================================================
# SELF TEST
# ============================================================

if __name__ == "__main__":

    tests = [

        (
            "https://www.facebook.com/share/p/AAA/"
            "https://www.facebook.com/dxt2k4/posts/pfbidBBB"
        ),

        (
            "https://www.facebook.com/reel/"
            "1671583863938536/"
        ),

        (
            "https://www.facebook.com/profile.php?id="
            "100022471806332"
        ),

        (
            "https://www.facebook.com/groups/"
            "104374540946868/posts/123456789"
        ),

        (
            "https://www.facebook.com/photo.php?"
            "fbid=1888274892556815"
            "&set=a.104374540946868"
            "&id=100041229667792"
        ),

        (
            "https://www.facebook.com/"
            "61592487939720/videos/"
            "1671583863938536/"
        ),
    ]

    print(
        "\n"
        + "=" * 70
    )

    print(
        "FACEBOOK RESOLVER V23 FORENSIC SELF TEST"
    )

    print(
        "=" * 70
    )

    for text in tests:

        print(
            "\nINPUT:"
        )

        print(
            text
        )

        urls = extract_facebook_urls(
            text
        )

        for url in urls:

            print(
                "\nURL:",
                url,
            )

            print(
                "SHAPE:",
                classify_url(
                    url
                ),
            )