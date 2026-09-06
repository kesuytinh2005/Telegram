#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
============================================================
 FACEBOOK RESOLVER V22 PRECISION / FORENSIC
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
- URL / canonical / OG / JSON-LD / HTML / JS analysis

DESIGN:
- Precision first
- Role separated IDs
- No random numeric ID assignment
- Multi-source UID correlation
- Canonical URL analysis
- Share URL resolution
- Publisher verification
- Conflict detection
- Evidence provenance
- Interactive /getuidfb session
- Multiple URLs
- Concatenated URLs
- Per-user session protection

IMPORTANT:
A numeric Facebook ID found somewhere in HTML is NOT automatically
a USER UID.

The resolver separates:

USER UID
PAGE UID
GROUP ID
EVENT ID
POST ID
VIDEO ID
REEL ID
PHOTO ID
STORY ID
ALBUM ID
ENTITY ID

Opaque pfbid/share tokens are NOT mathematically decoded into UID.
They are correlated with the resolved public object instead.
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
# CONFIG
# ============================================================

REQUEST_TIMEOUT = 12
MAX_HTML_BYTES = 12 * 1024 * 1024
MAX_PROFILE_CHECKS = 3
MAX_DISCOVERED_URLS = 80
MAX_CANDIDATES = 40
SESSION_TIMEOUT = 900

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

TRACKING_PARAMS = {
    "fbclid",
    "mibextid",
    "ref",
    "refid",
    "tn",
    "cft",
}

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
        "Mozilla/5.0 (Linux; Android 13; Mobile; rv:128.0) "
        "Gecko/128.0 Firefox/128.0"
    ),
]


# ============================================================
# SEMANTIC FIELD WEIGHTS
# ============================================================

# Strong user identity fields.
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


# ============================================================
# REGEX
# ============================================================

NUMERIC_ID = r"([0-9]{5,21})"

SEMANTIC_PATTERNS = {
    "user_id": [
        rf'"user_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf"'user_id'\s*:\s*['\"]?{NUMERIC_ID}",
        rf'\buser_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\buserId\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\buserID\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "profile_id": [
        rf'"profile_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bprofile_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bprofileId\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bprofileID\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "owner_id": [
        rf'"owner_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bowner_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bownerId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "publisher_id": [
        rf'"publisher_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bpublisher_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bpublisherId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "author_id": [
        rf'"author_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bauthor_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bauthorId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "actor_id": [
        rf'"actor_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bactor_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bactorId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "entity_id": [
        rf'"entity_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bentity_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bentityId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "page_id": [
        rf'"page_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bpage_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bpageId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "group_id": [
        rf'"group_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bgroup_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bgroupId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "post_id": [
        rf'"post_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bpost_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bpostId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "story_fbid": [
        rf'"story_fbid"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bstory_fbid\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bstoryFbid\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "video_id": [
        rf'"video_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bvideo_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bvideoId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "photo_id": [
        rf'"photo_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bphoto_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bphotoId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "media_fbid": [
        rf'"media_fbid"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\bmedia_fbid\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\bmediaFbid\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
    "album_id": [
        rf'"album_id"\s*:\s*["\']?{NUMERIC_ID}',
        rf'\balbum_id\s*[:=]\s*["\']?{NUMERIC_ID}',
        rf'\balbumId\s*[:=]\s*["\']?{NUMERIC_ID}',
    ],
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
    query: Dict[str, List[str]] = field(default_factory=dict)

    kind: str = "UNKNOWN"

    username: Optional[str] = None

    numeric_path_id: Optional[str] = None
    post_id: Optional[str] = None
    video_id: Optional[str] = None
    reel_id: Optional[str] = None
    photo_id: Optional[str] = None
    story_id: Optional[str] = None
    group_id: Optional[str] = None
    page_id: Optional[str] = None
    album_id: Optional[str] = None

    opaque_token: Optional[str] = None


@dataclass
class Evidence:
    value: str
    role: str
    source: str
    weight: float

    context: str = ""
    url: str = ""

    token: Optional[str] = None
    independent: str = ""

    verified: bool = False

    def key(self):
        return (
            self.value,
            self.role,
            self.source,
            self.context[:120],
        )


@dataclass
class Snapshot:
    requested_url: str

    final_url: str = ""
    status_code: int = 0

    html: str = ""
    title: str = ""

    canonical: Optional[str] = None

    meta: Dict[str, List[str]] = field(default_factory=dict)

    links: List[str] = field(default_factory=list)

    jsonld: List[Any] = field(default_factory=list)

    redirects: List[str] = field(default_factory=list)

    error: Optional[str] = None


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

    title: str = ""

    status: str = "UNVERIFIED"
    confidence: int = 0

    evidence: List[Evidence] = field(default_factory=list)

    notes: List[str] = field(default_factory=list)

    elapsed: float = 0.0


# ============================================================
# HTML PARSER
# ============================================================

class FacebookHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.title_parts: List[str] = []

        self.meta: Dict[str, List[str]] = {}

        self.links: List[str] = []

        self.jsonld: List[Any] = []

        self._in_title = False
        self._script_type = ""
        self._script_parts: List[str] = []

    def handle_starttag(self, tag, attrs):

        attrs_dict = dict(attrs)

        tag = tag.lower()

        if tag == "title":
            self._in_title = True

        elif tag == "meta":
            key = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or attrs_dict.get("itemprop")
            )

            content = attrs_dict.get("content")

            if key and content:
                self.meta.setdefault(
                    key.lower().strip(),
                    [],
                ).append(
                    html_lib.unescape(content.strip())
                )

        elif tag == "link":
            href = attrs_dict.get("href")

            if href:
                rel = (attrs_dict.get("rel") or "").lower()

                if "canonical" in rel:
                    self.meta.setdefault(
                        "__canonical__",
                        [],
                    ).append(href)

                self.links.append(href)

        elif tag == "script":

            self._script_type = (
                attrs_dict.get("type") or ""
            ).lower()

            self._script_parts = []

    def handle_endtag(self, tag):

        tag = tag.lower()

        if tag == "title":
            self._in_title = False

        elif tag == "script":

            if (
                "ld+json" in self._script_type
                and self._script_parts
            ):
                raw = "".join(self._script_parts).strip()

                try:
                    obj = json.loads(raw)
                    self.jsonld.append(obj)
                except Exception:
                    pass

            self._script_type = ""
            self._script_parts = []

    def handle_data(self, data):

        if self._in_title:
            self.title_parts.append(data)

        if self._script_type:
            self._script_parts.append(data)

    @property
    def title(self):
        return re.sub(
            r"\s+",
            " ",
            " ".join(self.title_parts),
        ).strip()


# ============================================================
# URL HELPERS
# ============================================================

def is_fb_host(host: str) -> bool:

    host = host.lower().split(":")[0]

    return (
        host in FB_HOSTS
        or host in FB_SHORT_HOSTS
    )


def normalize_url(url: str) -> str:

    url = html_lib.unescape(
        url.strip()
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
            url = "https://" + url

        elif url.lower().startswith(
            "facebook.com/"
        ):
            url = "https://" + url

    return url


def extract_facebook_urls(text: str) -> List[str]:

    """
    Extract Facebook URLs even when concatenated:

    URL1/URL2

    Example:
    https://facebook.com/share/p/AAA/https://facebook.com/foo/posts/BBB
    """

    if not text:
        return []

    pattern = re.compile(
        r"""
        (?P<url>
            https?://
            (?:
                [A-Za-z0-9-]+\.)?
                facebook\.com
                [^\s<>"'`]+
            |
            https?://
            fb\.watch
            [^\s<>"'`]+
            |
            (?<![A-Za-z0-9])
            (?:www\.)?facebook\.com
            [^\s<>"'`]+
        )
        """,
        re.I | re.X,
    )

    starts = list(
        pattern.finditer(text)
    )

    results = []

    for match in starts:

        raw = match.group("url")

        # Cut if another Facebook URL begins inside
        # the current match.
        nested = re.search(
            r"https?://(?:www\.)?(?:facebook\.com|fb\.watch)",
            raw[8:],
            re.I,
        )

        if nested:
            raw = raw[
                :8 + nested.start()
            ]

        # Clean trailing punctuation.
        raw = raw.rstrip(
            ".,;:!?)]}>\"'"
        )

        if is_valid_facebook_url(raw):
            results.append(
                normalize_url(raw)
            )

    # De-duplicate while preserving order.
    out = []
    seen = set()

    for url in results:

        key = url.lower()

        if key not in seen:
            seen.add(key)
            out.append(url)

    return out


def is_valid_facebook_url(url: str) -> bool:

    try:
        p = urlparse(url)

        return (
            p.scheme in {"http", "https"}
            and is_fb_host(p.netloc)
        )

    except Exception:
        return False


def clean_query(
    query: Dict[str, List[str]]
) -> Dict[str, List[str]]:

    out = {}

    for key, values in query.items():

        if key.lower() in TRACKING_PARAMS:
            continue

        if key.lower().startswith("utm_"):
            continue

        out[key] = values

    return out


# ============================================================
# URL CLASSIFICATION
# ============================================================

def classify_url(url: str) -> URLShape:

    url = normalize_url(url)

    p = urlparse(url)

    host = p.netloc.lower().split(":")[0]

    path = unquote(
        p.path or "/"
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
        x for x in path.split("/")
        if x
    ]

    lower_parts = [
        x.lower()
        for x in parts
    ]

    # --------------------------------------------------------
    # PROFILE
    # --------------------------------------------------------

    if path.lower().startswith(
        "/profile.php"
    ):

        uid = first_query(
            query,
            "id",
        )

        if uid and uid.isdigit():
            shape.kind = "PROFILE"
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

        shape.album_id = extract_album_from_query(
            query
        )

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

        uid = first_query(
            query,
            "id",
        )

        if is_long_numeric(uid):
            shape.numeric_path_id = uid

        return shape

    # --------------------------------------------------------
    # PERMALINK
    # --------------------------------------------------------

    if path.lower().endswith(
        "/permalink.php"
    ):

        shape.kind = "POST"

        shape.post_id = first_query(
            query,
            "story_fbid",
        )

        if not shape.post_id:
            shape.post_id = first_query(
                query,
                "id",
            )

        return shape

    # --------------------------------------------------------
    # WATCH
    # --------------------------------------------------------

    if "watch" in lower_parts:

        shape.kind = "VIDEO"

        shape.video_id = first_query(
            query,
            "v",
        )

        return shape

    # --------------------------------------------------------
    # GROUP
    # --------------------------------------------------------

    if lower_parts and lower_parts[0] == "groups":

        shape.kind = "GROUP"

        if len(parts) >= 2:

            gid = parts[1]

            if gid.isdigit():
                shape.group_id = gid

        if len(parts) >= 4 and (
            lower_parts[2] in {
                "posts",
                "permalink",
            }
        ):
            shape.kind = "GROUP_POST"

            pid = parts[3]

            if is_numeric_id(pid):
                shape.post_id = pid
            else:
                shape.opaque_token = pid

        return shape

    # --------------------------------------------------------
    # PAGE
    # --------------------------------------------------------

    if lower_parts and lower_parts[0] == "pages":

        shape.kind = "PAGE"

        if len(parts) >= 3:
            if parts[2].isdigit():
                shape.page_id = parts[2]

        return shape

    # --------------------------------------------------------
    # PEOPLE
    # --------------------------------------------------------

    if lower_parts and lower_parts[0] == "people":

        shape.kind = "PROFILE"

        if len(parts) >= 3:
            if parts[2].isdigit():
                shape.numeric_path_id = parts[2]

        if len(parts) >= 2:
            shape.username = parts[1]

        return shape

    # --------------------------------------------------------
    # /p/
    # --------------------------------------------------------

    if lower_parts and lower_parts[0] == "p":

        shape.kind = "POST"

        if len(parts) >= 2:

            token = parts[1]

            if is_numeric_id(token):
                shape.post_id = token
            else:
                shape.opaque_token = token

        return shape

    # --------------------------------------------------------
    # /reel/
    # --------------------------------------------------------

    if lower_parts and lower_parts[0] in {
        "reel",
        "reels",
    }:

        shape.kind = "REEL"

        if len(parts) >= 2:

            value = parts[1]

            if is_numeric_id(value):
                shape.reel_id = value
                shape.video_id = value
            else:
                shape.opaque_token = value

        return shape

    # --------------------------------------------------------
    # /video/
    # --------------------------------------------------------

    if lower_parts and lower_parts[0] in {
        "video",
        "videos",
    }:

        shape.kind = "VIDEO"

        if len(parts) >= 2:

            value = parts[-1]

            if is_numeric_id(value):
                shape.video_id = value

        return shape

    # --------------------------------------------------------
    # /photo/
    # --------------------------------------------------------

    if lower_parts and lower_parts[0] in {
        "photo",
        "photos",
    }:

        shape.kind = "PHOTO"

        value = parts[-1]

        if is_numeric_id(value):
            shape.photo_id = value

        return shape

    # --------------------------------------------------------
    # /story/
    # --------------------------------------------------------

    if lower_parts and lower_parts[0] in {
        "story",
        "stories",
    }:

        shape.kind = "STORY"

        value = parts[-1]

        if is_numeric_id(value):
            shape.story_id = value

        return shape

    # --------------------------------------------------------
    # SHARE
    # --------------------------------------------------------

    if len(parts) >= 2 and (
        lower_parts[0] == "share"
    ):

        code = parts[-1]

        if lower_parts[1] == "p":
            shape.kind = "SHARE_POST"

        elif lower_parts[1] == "v":
            shape.kind = "SHARE_VIDEO"

        elif lower_parts[1] == "r":
            shape.kind = "SHARE_REEL"

        elif lower_parts[1] == "s":
            shape.kind = "SHARE_STORY"

        else:
            shape.kind = "SHARE"

        shape.opaque_token = code

        return shape

    # --------------------------------------------------------
    # USERNAME + POSTS / VIDEOS / PHOTOS
    # --------------------------------------------------------

    if parts:

        shape.username = parts[0]

        if len(parts) >= 2:

            second = lower_parts[1]

            if second == "posts":

                shape.kind = "USER_POST"

                value = parts[2] if len(parts) >= 3 else ""

                if is_numeric_id(value):
                    shape.post_id = value
                elif value:
                    shape.opaque_token = value

                return shape

            if second in {
                "videos",
                "video",
            }:

                shape.kind = "VIDEO"

                value = parts[2] if len(parts) >= 3 else ""

                if is_numeric_id(value):
                    shape.video_id = value

                return shape

            if second in {
                "photos",
                "photo",
            }:

                shape.kind = "PHOTO"

                value = parts[2] if len(parts) >= 3 else ""

                if is_numeric_id(value):
                    shape.photo_id = value

                return shape

            if second in {
                "reel",
                "reels",
            }:

                shape.kind = "REEL"

                value = parts[2] if len(parts) >= 3 else ""

                if is_numeric_id(value):
                    shape.reel_id = value
                    shape.video_id = value

                return shape

        # Bare profile/page URL.
        shape.kind = "PROFILE_OR_PAGE"
        return shape

    return shape


# ============================================================
# URL HELPERS
# ============================================================

def first_query(
    query: Dict[str, List[str]],
    key: str,
) -> Optional[str]:

    values = query.get(key)

    if not values:
        return None

    return values[0]


def extract_album_from_query(
    query: Dict[str, List[str]]
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

    return m.group(1) if m else None


def is_numeric_id(value: Optional[str]) -> bool:

    return bool(
        value
        and re.fullmatch(
            r"\d{5,21}",
            value,
        )
    )


def is_long_numeric(
    value: Optional[str]
) -> bool:

    return bool(
        value
        and re.fullmatch(
            r"\d{8,21}",
            value,
        )
    )


# ============================================================
# HTTP
# ============================================================

def fetch_sync(
    url: str,
    user_agent: str,
) -> Snapshot:

    snapshot = Snapshot(
        requested_url=url
    )

    headers = {
        'authority': 'www.facebook.com',
		'accept': '*/*',
		'accept-language': 'vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5',
		'content-type': 'application/x-www-form-urlencoded',
		'dnt': '1',
		'origin': 'https://www.facebook.com',
		'sec-ch-prefers-color-scheme': 'dark',
		'sec-ch-ua': '"Chromium";v="117", "Not;A=Brand";v="8"',
		'sec-ch-ua-full-version-list': '"Chromium";v="117.0.5938.157", "Not;A=Brand";v="8.0.0.0"',
		'sec-ch-ua-mobile': '?0',
		'sec-ch-ua-model': '""',
		'sec-ch-ua-platform': '"Windows"',
		'sec-ch-ua-platform-version': '"15.0.0"',
		'sec-fetch-dest': 'empty',
		'sec-fetch-mode': 'cors',
		'sec-fetch-site': 'same-origin',
		'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
		'x-fb-friendly-name': 'useCometConsentPromptEndOfFlowBatchedMutation',
    }

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
            response.url or url
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

            total += len(chunk)

            if total > MAX_HTML_BYTES:

                remain = (
                    MAX_HTML_BYTES
                    - (total - len(chunk))
                )

                if remain > 0:
                    chunks.append(
                        chunk[:remain]
                    )

                break

            chunks.append(chunk)

        raw = b"".join(chunks)

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

        return snapshot

    except Exception as exc:

        snapshot.error = (
            f"{type(exc).__name__}: {exc}"
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

        if snapshot.status_code in {
            200,
            301,
            302,
            403,
            404,
        }:
            return snapshot

        await asyncio.sleep(
            0.25 * (index + 1)
        )

    return last or Snapshot(
        requested_url=url,
        error="HTTP request failed",
    )


# ============================================================
# PARSE SNAPSHOT
# ============================================================

def parse_snapshot(
    snapshot: Snapshot
) -> Snapshot:

    parser = FacebookHTMLParser()

    try:
        parser.feed(
            snapshot.html
        )
    except Exception:
        pass

    snapshot.title = parser.title

    snapshot.meta = parser.meta

    snapshot.links = parser.links[:]

    snapshot.jsonld = parser.jsonld

    canonical_values = (
        parser.meta.get(
            "__canonical__",
            [],
        )
        + parser.meta.get(
            "og:url",
            [],
        )
    )

    for value in canonical_values:

        candidate = normalize_discovered_url(
            value,
            snapshot.final_url,
        )

        if candidate:
            snapshot.canonical = candidate
            break

    return snapshot


def normalize_discovered_url(
    value: str,
    base_url: str,
) -> Optional[str]:

    if not value:
        return None

    value = html_lib.unescape(
        value.strip()
    )

    if value.startswith("//"):
        value = "https:" + value

    elif value.startswith("/"):
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

    return value


# ============================================================
# JSON WALKER
# ============================================================

def walk_json(
    obj: Any,
    path: str = "",
) -> Iterable[
    Tuple[str, Any]
]:

    yield path, obj

    if isinstance(obj, dict):

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

    elif isinstance(obj, list):

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
# EVIDENCE ENGINE
# ============================================================

def add_evidence(
    evidence: List[Evidence],
    value: Optional[str],
    role: str,
    source: str,
    weight: float,
    context: str = "",
    url: str = "",
    token: Optional[str] = None,
    independent: str = "",
    verified: bool = False,
):

    if not value:
        return

    value = str(value)

    if not value.isdigit():
        return

    if len(value) < 5:
        return

    item = Evidence(
        value=value,
        role=role,
        source=source,
        weight=weight,
        context=context[:1000],
        url=url,
        token=token,
        independent=independent or source,
        verified=verified,
    )

    if item.key() not in {
        x.key()
        for x in evidence
    }:
        evidence.append(item)


def collect_semantic_evidence(
    text: str,
    evidence: List[Evidence],
    source: str,
    base_url: str,
    token: Optional[str] = None,
):

    if not text:
        return

    for field_name, patterns in (
        SEMANTIC_PATTERNS.items()
    ):

        for pattern in patterns:

            try:
                matches = re.finditer(
                    pattern,
                    text,
                    re.I,
                )
            except re.error:
                continue

            for match in matches:

                value = match.group(1)

                start = max(
                    0,
                    match.start() - 180,
                )

                end = min(
                    len(text),
                    match.end() + 180,
                )

                context = text[
                    start:end
                ]

                if (
                    field_name
                    in USER_FIELD_WEIGHTS
                ):

                    weight = (
                        USER_FIELD_WEIGHTS[
                            field_name
                        ]
                    )

                    add_evidence(
                        evidence,
                        value,
                        "USER_CANDIDATE",
                        source,
                        weight,
                        context,
                        base_url,
                        token,
                        source,
                    )

                elif (
                    field_name
                    in OBJECT_FIELD_WEIGHTS
                ):

                    add_evidence(
                        evidence,
                        value,
                        field_name,
                        source,
                        OBJECT_FIELD_WEIGHTS[
                            field_name
                        ],
                        context,
                        base_url,
                        token,
                        source,
                    )


# ============================================================
# NESTED JSON SEMANTIC EXTRACTION
# ============================================================

def collect_json_semantic_evidence(
    json_objects: List[Any],
    evidence: List[Evidence],
    base_url: str,
):

    for obj_index, root in enumerate(
        json_objects
    ):

        for path, value in walk_json(root):

            if not isinstance(
                value,
                (str, int, float),
            ):
                continue

            key = (
                path.rsplit(
                    ".",
                    1,
                )[-1]
                if path
                else ""
            )

            key = re.sub(
                r"\[\d+\]",
                "",
                key,
            ).lower()

            value_str = str(value)

            if not value_str.isdigit():
                continue

            if key in USER_FIELD_WEIGHTS:

                add_evidence(
                    evidence,
                    value_str,
                    "USER_CANDIDATE",
                    f"jsonld:{key}",
                    USER_FIELD_WEIGHTS[key],
                    path,
                    base_url,
                    None,
                    f"json:{obj_index}:{key}",
                )

            elif key in OBJECT_FIELD_WEIGHTS:

                add_evidence(
                    evidence,
                    value_str,
                    key,
                    f"json:{key}",
                    OBJECT_FIELD_WEIGHTS[key],
                    path,
                    base_url,
                    None,
                    f"json:{obj_index}:{key}",
                )


# ============================================================
# PROFILE / AUTHOR URL EXTRACTION
# ============================================================

def extract_profile_urls(
    snapshot: Snapshot,
) -> List[str]:

    results = []

    candidates = []

    candidates.extend(
        snapshot.links
    )

    for key in (
        "og:url",
        "article:author",
        "profile:url",
        "author:url",
    ):
        candidates.extend(
            snapshot.meta.get(
                key,
                [],
            )
        )

    for root in snapshot.jsonld:

        for path, value in walk_json(
            root
        ):

            low = path.lower()

            if any(
                x in low
                for x in (
                    "author",
                    "publisher",
                    "creator",
                    "mainentityofpage",
                )
            ):

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

                        v = value.get(key)

                        if (
                            isinstance(
                                v,
                                str,
                            )
                            and
                            "facebook.com"
                            in v.lower()
                        ):
                            candidates.append(v)

    for value in candidates:

        url = normalize_discovered_url(
            str(value),
            snapshot.final_url,
        )

        if not url:
            continue

        shape = classify_url(
            url
        )

        if shape.kind in {
            "PROFILE",
            "PROFILE_OR_PAGE",
            "PEOPLE",
            "PAGE",
        }:
            results.append(url)

        elif (
            shape.kind == "UNKNOWN"
            and is_probable_profile_url(
                url
            )
        ):
            results.append(url)

    out = []
    seen = set()

    for url in results:

        key = canonical_profile_key(
            url
        )

        if key not in seen:

            seen.add(key)
            out.append(url)

    return out[:MAX_PROFILE_CHECKS]


def is_probable_profile_url(
    url: str
) -> bool:

    p = urlparse(url)

    path = p.path.strip("/")

    if not path:
        return False

    if path.startswith(
        (
            "posts/",
            "reel/",
            "reels/",
            "video/",
            "videos/",
            "photo/",
            "photos/",
            "story/",
            "stories/",
            "groups/",
            "share/",
            "watch",
            "p/",
        )
    ):
        return False

    return True


def canonical_profile_key(
    url: str
) -> str:

    p = urlparse(url)

    return (
        p.netloc.lower()
        + "/"
        + p.path.lower().strip("/")
    )


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
        "title": "",
        "type": "UNKNOWN",
        "verified": False,
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
    )

    result["title"] = snapshot.title

    evidence: List[Evidence] = []

    collect_semantic_evidence(
        snapshot.html,
        evidence,
        "profile_html",
        snapshot.final_url,
    )

    collect_json_semantic_evidence(
        snapshot.jsonld,
        evidence,
        snapshot.final_url,
    )

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
    # Direct profile.php?id=
    # --------------------------------------------------------

    p = urlparse(
        snapshot.final_url
    )

    q = parse_qs(
        p.query
    )

    profile_id = first_query(
        q,
        "id",
    )

    if is_long_numeric(
        profile_id
    ):
        result["uid_hits"].add(
            profile_id
        )

    # --------------------------------------------------------
    # Determine semantic type.
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
        + " "
        + snapshot.html[:300000]
    ).lower()

    if (
        "organization" in combined
        or "page_id" in combined
        or "/pages/" in combined
    ):
        result["type"] = "PAGE"

    elif (
        '"person"' in combined
        or "person" in combined
    ):
        result["type"] = "USER"

    # --------------------------------------------------------
    # Expected UID verification.
    # --------------------------------------------------------

    if expected_uid:

        expected_uid = str(
            expected_uid
        )

        if expected_uid in (
            result["uid_hits"]
        ):
            result["verified"] = True

        elif (
            f"id={expected_uid}"
            in snapshot.final_url
        ):
            result["verified"] = True

    return result


# ============================================================
# BASE64 CONTEXT DECODER
# ============================================================

def decode_possible_base64(
    value: str,
) -> Optional[str]:

    if not value:
        return None

    value = value.strip()

    if len(value) < 12:
        return None

    if not re.fullmatch(
        r"[A-Za-z0-9_-]+",
        value,
    ):
        return None

    try:

        padding = "=" * (
            (-len(value)) % 4
        )

        raw = base64.urlsafe_b64decode(
            value + padding
        )

        text = raw.decode(
            "utf-8",
            errors="ignore",
        )

        if not text:
            return None

        return text[:5000]

    except (
        ValueError,
        binascii.Error,
    ):
        return None


# ============================================================
# DISCOVERED URLS FROM HTML
# ============================================================

def discover_facebook_urls(
    snapshot: Snapshot,
) -> List[str]:

    results = []

    for href in snapshot.links:

        url = normalize_discovered_url(
            href,
            snapshot.final_url,
        )

        if url:
            results.append(url)

    # Search raw HTML / JS too.
    for match in re.finditer(
        r'https?://(?:www\.)?(?:facebook\.com|fb\.watch)[^"\'<>\s]+',
        snapshot.html,
        re.I,
    ):

        value = match.group(0)

        value = value.rstrip(
            ".,;:!?)]}>\"'"
        )

        if is_valid_facebook_url(
            value
        ):
            results.append(
                value
            )

    out = []
    seen = set()

    for url in results:

        key = url.lower()

        if key not in seen:

            seen.add(key)
            out.append(url)

    return out[:MAX_DISCOVERED_URLS]


# ============================================================
# OBJECT EVIDENCE
# ============================================================

def add_structural_evidence(
    shape: URLShape,
    evidence: List[Evidence],
):

    if shape.post_id:
        add_evidence(
            evidence,
            shape.post_id,
            "post_id",
            "url_structure",
            130,
            shape.path,
            shape.normalized,
            shape.opaque_token,
            "url",
        )

    if shape.video_id:
        add_evidence(
            evidence,
            shape.video_id,
            "video_id",
            "url_structure",
            140,
            shape.path,
            shape.normalized,
            shape.opaque_token,
            "url",
        )

    if shape.reel_id:
        add_evidence(
            evidence,
            shape.reel_id,
            "reel_id",
            "url_structure",
            145,
            shape.path,
            shape.normalized,
            shape.opaque_token,
            "url",
        )

    if shape.photo_id:
        add_evidence(
            evidence,
            shape.photo_id,
            "photo_id",
            "url_structure",
            140,
            shape.path,
            shape.normalized,
            shape.opaque_token,
            "url",
        )

    if shape.story_id:
        add_evidence(
            evidence,
            shape.story_id,
            "story_fbid",
            "url_structure",
            140,
            shape.path,
            shape.normalized,
            shape.opaque_token,
            "url",
        )

    if shape.group_id:
        add_evidence(
            evidence,
            shape.group_id,
            "group_id",
            "url_structure",
            160,
            shape.path,
            shape.normalized,
            None,
            "url",
        )

    if shape.page_id:
        add_evidence(
            evidence,
            shape.page_id,
            "page_id",
            "url_structure",
            160,
            shape.path,
            shape.normalized,
            None,
            "url",
        )

    if shape.album_id:
        add_evidence(
            evidence,
            shape.album_id,
            "album_id",
            "url_structure",
            150,
            shape.path,
            shape.normalized,
            None,
            "url",
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

    p = urlparse(url)

    query = parse_qs(
        p.query
    )

    # profile.php?id=
    uid = first_query(
        query,
        "id",
    )

    if (
        p.path.lower().endswith(
            "/profile.php"
        )
        and is_long_numeric(uid)
    ):

        add_evidence(
            evidence,
            uid,
            "USER_CANDIDATE",
            "canonical_profile_query",
            125,
            url,
            url,
            None,
            "canonical_profile_id",
        )

    # photo.php?fbid=X&set=a.Y&id=Z
    if p.path.lower().endswith(
        "/photo.php"
    ):

        photo_id = first_query(
            query,
            "fbid",
        )

        if photo_id:
            add_evidence(
                evidence,
                photo_id,
                "photo_id",
                "canonical_photo",
                170,
                url,
                url,
                None,
                "canonical_photo_id",
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
                160,
                url,
                url,
                None,
                "canonical_album_id",
            )

        owner = first_query(
            query,
            "id",
        )

        if is_long_numeric(owner):

            add_evidence(
                evidence,
                owner,
                "USER_CANDIDATE",
                "photo_owner_query",
                82,
                url,
                url,
                None,
                "photo_owner_id",
            )


# ============================================================
# SCORE USER CANDIDATES
# ============================================================

def candidate_user_scores(
    evidence: List[Evidence],
) -> Dict[str, Dict[str, Any]]:

    scores = {}

    for ev in evidence:

        if ev.role != "USER_CANDIDATE":
            continue

        value = ev.value

        if not is_long_numeric(
            value
        ):
            continue

        row = scores.setdefault(
            value,
            {
                "score": 0.0,
                "sources": set(),
                "roles": set(),
                "contexts": [],
                "verified": False,
                "evidence": [],
            },
        )

        row["score"] += ev.weight

        row["sources"].add(
            ev.source
        )

        row["roles"].add(
            ev.role
        )

        if ev.context:
            row["contexts"].append(
                ev.context
            )

        row["evidence"].append(
            ev
        )

        if ev.verified:
            row["verified"] = True

    # Independent source bonus.
    for value, row in scores.items():

        source_count = len(
            row["sources"]
        )

        role_count = len(
            row["roles"]
        )

        if source_count >= 2:
            row["score"] += 35

        if source_count >= 3:
            row["score"] += 25

        if role_count >= 2:
            row["score"] += 20

        # Same UID appearing repeatedly is useful,
        # but not as strong as independent sources.
        occurrences = len(
            row["evidence"]
        )

        if occurrences >= 2:
            row["score"] += min(
                30,
                occurrences * 5,
            )

    return scores


# ============================================================
# REMOVE ROLE CONFLICTS
# ============================================================

def role_conflicts(
    candidate: str,
    evidence: List[Evidence],
) -> Set[str]:

    roles = set()

    for ev in evidence:

        if ev.value != candidate:
            continue

        if ev.role in {
            "page_id",
            "group_id",
            "album_id",
        }:
            roles.add(
                ev.role
            )

    return roles


# ============================================================
# ENTITY TYPE
# ============================================================

def infer_entity_type(
    shape: URLShape,
    evidence: List[Evidence],
    resolved_url: str,
) -> str:

    url = (
        resolved_url
        or shape.normalized
    )

    low = url.lower()

    # Group always wins for /groups.
    if (
        shape.kind == "GROUP"
        or shape.kind == "GROUP_POST"
        or "/groups/" in low
    ):
        return (
            "GROUP_POST"
            if (
                shape.kind
                == "GROUP_POST"
            )
            else "GROUP"
        )

    # Page explicit.
    if (
        shape.kind == "PAGE"
        or "/pages/" in low
    ):
        return "PAGE"

    # Object types.
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

    if shape.kind in {
        "PHOTO",
    }:
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

    # Canonical path.
    p = urlparse(url)

    parts = [
        x.lower()
        for x in p.path.split("/")
        if x
    ]

    if parts:

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

        if len(parts) >= 2 and (
            parts[1] == "posts"
        ):
            return "POST"

    return "UNKNOWN"


# ============================================================
# PUBLISHER TYPE
# ============================================================

def infer_publisher_type(
    shape: URLShape,
    user_uid: Optional[str],
    page_uid: Optional[str],
    group_id: Optional[str],
    snapshots: List[Snapshot],
) -> str:

    if group_id and (
        shape.kind
        in {
            "GROUP",
            "GROUP_POST",
        }
    ):
        return "GROUP"

    if page_uid:
        return "PAGE"

    if user_uid:
        return "USER"

    # Look for explicit page signals.
    text = " ".join(
        (
            s.title
            + " "
            + " ".join(
                s.meta.get(
                    "og:type",
                    [],
                )
            )
        )
        for s in snapshots
    ).lower()

    if (
        "website" in text
        and "profile" not in text
    ):
        return "PAGE"

    return "UNKNOWN"


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
                "score": 0,
                "count": 0,
                "sources": set(),
            },
        )

        row["score"] += ev.weight

        row["count"] += 1

        row["sources"].add(
            ev.source
        )

    if not candidates:
        return None

    for row in candidates.values():

        if len(
            row["sources"]
        ) >= 2:
            row["score"] += 30

        if row["count"] >= 2:
            row["score"] += 15

    return max(
        candidates.items(),
        key=lambda x: x[1]["score"],
    )[0]


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
    # Structural evidence from input.
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
    # First HTTP request.
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
    # Final URL.
    # --------------------------------------------------------

    result.resolved_url = (
        first.final_url
        or input_url
    )

    # --------------------------------------------------------
    # Canonical.
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

    # --------------------------------------------------------
    # Title.
    # --------------------------------------------------------

    result.title = (
        first.title
        or first.meta.get(
            "og:title",
            [""] ,
        )[0]
        or ""
    )

    # --------------------------------------------------------
    # Semantic HTML.
    # --------------------------------------------------------

    collect_semantic_evidence(
        first.html,
        evidence,
        "html_semantic",
        first.final_url,
        shape.opaque_token,
    )

    # --------------------------------------------------------
    # Meta.
    # --------------------------------------------------------

    for key, values in first.meta.items():

        if key == "__canonical__":
            continue

        for value in values:

            text = str(value)

            collect_semantic_evidence(
                text,
                evidence,
                f"meta:{key}",
                first.final_url,
                shape.opaque_token,
            )

    # --------------------------------------------------------
    # JSON-LD / structured data.
    # --------------------------------------------------------

    collect_json_semantic_evidence(
        first.jsonld,
        evidence,
        first.final_url,
    )

    # --------------------------------------------------------
    # Discover canonical/profile/object links.
    # --------------------------------------------------------

    discovered = (
        discover_facebook_urls(
            first
        )
    )

    # --------------------------------------------------------
    # Redirect target.
    # --------------------------------------------------------

    if (
        first.final_url
        and first.final_url
        != input_url
    ):

        redirected_shape = classify_url(
            first.final_url
        )

        add_structural_evidence(
            redirected_shape,
            evidence,
        )

        extract_canonical_structure(
            first.final_url,
            evidence,
        )

    # --------------------------------------------------------
    # Canonical target.
    # --------------------------------------------------------

    if (
        first.canonical
        and first.canonical
        != first.final_url
    ):

        canonical_shape = classify_url(
            first.canonical
        )

        add_structural_evidence(
            canonical_shape,
            evidence,
        )

    # --------------------------------------------------------
    # Fetch discovered canonical/profile
    # --------------------------------------------------------

    profile_urls = extract_profile_urls(
        first
    )

    # Route username may itself identify publisher.
    if shape.username:

        profile_candidate = (
            "https://www.facebook.com/"
            + quote(
                shape.username,
                safe="@._-",
            )
        )

        if (
            profile_candidate
            not in profile_urls
        ):
            profile_urls.append(
                profile_candidate
            )

    # If canonical is a profile-like URL.
    canonical_shape = classify_url(
        result.canonical_url
    )

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
    # Candidate user IDs BEFORE profile verification.
    # --------------------------------------------------------

    scores = candidate_user_scores(
        evidence
    )

    preliminary = sorted(
        scores.items(),
        key=lambda x: x[1]["score"],
        reverse=True,
    )

    # Verify strongest candidates.
    checked = 0

    for candidate_uid, row in preliminary:

        if checked >= MAX_PROFILE_CHECKS:
            break

        conflicts = role_conflicts(
            candidate_uid,
            evidence,
        )

        # Group/page/album evidence does NOT automatically
        # make it a user. We still may verify independently.
        if (
            "group_id" in conflicts
            and "page_id" not in conflicts
        ):
            continue

        # Try existing discovered profile URLs first.
        urls_to_check = list(
            profile_urls
        )

        # Direct numeric profile endpoint.
        urls_to_check.append(
            "https://www.facebook.com/profile.php?id="
            + candidate_uid
        )

        for profile_url in urls_to_check:

            if checked >= MAX_PROFILE_CHECKS:
                break

            verified = await verify_profile_url(
                profile_url,
                candidate_uid,
            )

            checked += 1

            if verified["verified"]:

                row["verified"] = True

                # Independent profile confirmation.
                add_evidence(
                    evidence,
                    candidate_uid,
                    "USER_CANDIDATE",
                    "profile_verification",
                    155,
                    verified.get(
                        "canonical",
                        "",
                    ),
                    profile_url,
                    None,
                    "verified_profile",
                    True,
                )

                break

    # --------------------------------------------------------
    # Recalculate after verification.
    # --------------------------------------------------------

    scores = candidate_user_scores(
        evidence
    )

    # --------------------------------------------------------
    # Object type.
    # --------------------------------------------------------

    result.object_type = infer_entity_type(
        shape,
        evidence,
        result.canonical_url,
    )

    # --------------------------------------------------------
    # Explicit page/group.
    # --------------------------------------------------------

    result.page_uid = best_object_id(
        evidence,
        "page_id",
    )

    result.group_id = best_object_id(
        evidence,
        "group_id",
    )

    # Never expose page/group IDs as USER UID.
    page_or_group_values = {
        x
        for x in (
            result.page_uid,
            result.group_id,
        )
        if x
    }

    # --------------------------------------------------------
    # Best user candidate.
    # --------------------------------------------------------

    valid_user_candidates = []

    for uid, row in scores.items():

        if uid in page_or_group_values:
            continue

        conflicts = role_conflicts(
            uid,
            evidence,
        )

        # Album ID is never a user ID unless there is
        # independent user verification.
        if (
            "album_id" in conflicts
            and not row["verified"]
        ):
            continue

        valid_user_candidates.append(
            (uid, row)
        )

    valid_user_candidates.sort(
        key=lambda x: x[1]["score"],
        reverse=True,
    )

    if valid_user_candidates:

        top_uid, top_row = (
            valid_user_candidates[0]
        )

        second_score = (
            valid_user_candidates[1][1]["score"]
            if len(valid_user_candidates) > 1
            else 0
        )

        # Conflict if another UID is too close.
        conflict = (
            second_score >=
            top_row["score"] * 0.88
        )

        if (
            top_row["verified"]
            and not conflict
        ):

            result.user_uid = top_uid

        elif (
            not conflict
            and top_row["score"] >= 230
            and len(
                top_row["sources"]
            ) >= 2
        ):

            result.user_uid = top_uid

        elif (
            not conflict
            and top_row["score"] >= 280
            and top_row["verified"]
        ):

            result.user_uid = top_uid

    # --------------------------------------------------------
    # Username.
    # --------------------------------------------------------

    result.username = (
        shape.username
        or extract_username_from_url(
            result.canonical_url
        )
    )

    # --------------------------------------------------------
    # Publisher.
    # --------------------------------------------------------

    result.publisher_type = (
        infer_publisher_type(
            shape,
            result.user_uid,
            result.page_uid,
            result.group_id,
            snapshots,
        )
    )

    # --------------------------------------------------------
    # Object IDs.
    # --------------------------------------------------------

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

    # Reel ID is also the video ID when the route itself
    # explicitly identifies a reel.
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
    # Do not invent object IDs.
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
# FINALIZE
# ============================================================

def finalize_result(
    result: ResolveResult,
    evidence: List[Evidence],
    snapshots: List[Snapshot],
) -> ResolveResult:

    # Deduplicate evidence.
    unique = {}
    for ev in evidence:
        unique[ev.key()] = ev

    result.evidence = list(
        unique.values()
    )

    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------

    user_evidence = [
        x
        for x in result.evidence
        if x.role == "USER_CANDIDATE"
    ]

    if result.user_uid:

        top = [
            x
            for x in user_evidence
            if x.value == result.user_uid
        ]

        sources = {
            x.source
            for x in top
        }

        verified = any(
            x.verified
            for x in top
        )

        score = sum(
            x.weight
            for x in top
        )

        confidence = 55

        if verified:
            confidence += 25

        if len(sources) >= 2:
            confidence += 10

        if len(sources) >= 3:
            confidence += 5

        if score >= 300:
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

        # We still calculate structural confidence,
        # but NEVER convert it into a fake UID.
        object_sources = {
            x.source
            for x in result.evidence
            if x.role in {
                "post_id",
                "video_id",
                "reel_id",
                "photo_id",
                "story_fbid",
                "group_id",
                "page_id",
            }
        }

        result.confidence = min(
            85,
            40 + len(object_sources) * 7,
        )

        result.status = "UNVERIFIED"

        result.notes.append(
            "Không đủ bằng chứng độc lập để xác minh USER UID."
        )

    # --------------------------------------------------------
    # Structural notes
    # --------------------------------------------------------

    if (
        result.input_url
        != result.resolved_url
        and result.resolved_url
    ):
        result.notes.insert(
            0,
            "URL đã được Facebook redirect."
        )

    if result.shape.opaque_token:
        result.notes.append(
            "Opaque token được dùng làm object correlation, "
            "không tự giải mã thành UID."
        )

    if (
        result.publisher_type
        == "GROUP"
        and not result.user_uid
    ):
        result.notes.append(
            "Group ID không được sử dụng làm USER UID."
        )

    if (
        result.page_uid
        and result.user_uid
        and result.page_uid
        == result.user_uid
    ):
        # This is possible only if independent evidence
        # confirms both roles, otherwise remove user UID.
        independent = [
            x
            for x in result.evidence
            if (
                x.value
                == result.user_uid
                and x.verified
            )
        ]

        if not independent:
            result.user_uid = None
            result.status = "UNVERIFIED"

    return result


# ============================================================
# USERNAME EXTRACTION
# ============================================================

def extract_username_from_url(
    url: str,
) -> Optional[str]:

    if not url:
        return None

    p = urlparse(url)

    parts = [
        unquote(x)
        for x in p.path.split("/")
        if x
    ]

    if not parts:
        return None

    first = parts[0]

    if first.lower() in {
        "profile.php",
        "groups",
        "pages",
        "reel",
        "reels",
        "video",
        "videos",
        "photo",
        "photos",
        "story",
        "stories",
        "share",
        "watch",
        "p",
        "permalink.php",
    }:
        return None

    if first.isdigit():
        return None

    return first


# ============================================================
# DISPLAY HELPERS
# ============================================================

def esc(value: Any) -> str:

    if value is None:
        return ""

    return html_lib.escape(
        str(value),
        quote=False,
    )


def display_id(
    label: str,
    value: Optional[str],
) -> Optional[str]:

    if not value:
        return None

    return (
        f"{label}: "
        f"<code>{esc(value)}</code>"
    )


def build_result_text(
    result: ResolveResult,
    index: int,
    total: int,
) -> str:

    lines = []

    lines.append(
        f"🔎 <b>FACEBOOK RESOLVER V22</b> "
        f"[{index}/{total}]"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"🌐 <b>URL TYPE:</b> "
        f"<code>{esc(result.object_type)}</code>"
    )

    lines.append(
        f"📦 <b>OBJECT:</b> "
        f"<code>{esc(result.publisher_type)}</code>"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "👤 <b>PUBLISHER</b>"
    )

    lines.append(
        f"<b>TYPE:</b> "
        f"<code>{esc(result.publisher_type)}</code>"
    )

    if result.username:
        lines.append(
            f"<b>USERNAME:</b> "
            f"<code>@{esc(result.username.lstrip('@'))}</code>"
        )

    if result.user_uid:
        lines.append(
            f"🆔 <b>USER UID:</b> "
            f"<code>{esc(result.user_uid)}</code>"
        )
    else:
        lines.append(
            "🆔 <b>USER UID:</b> "
            "<code>NOT VERIFIED</code>"
        )

    if result.page_uid:
        lines.append(
            f"📄 <b>PAGE UID:</b> "
            f"<code>{esc(result.page_uid)}</code>"
        )

    if result.group_id:
        lines.append(
            f"👥 <b>GROUP ID:</b> "
            f"<code>{esc(result.group_id)}</code>"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "📦 <b>OBJECT IDS</b>"
    )

    # Only display IDs appropriate to the object.
    if result.post_id:
        lines.append(
            f"📝 <b>POST ID:</b> "
            f"<code>{esc(result.post_id)}</code>"
        )

    if result.video_id:
        lines.append(
            f"🎬 <b>VIDEO ID:</b> "
            f"<code>{esc(result.video_id)}</code>"
        )

    if result.reel_id:
        lines.append(
            f"🎞 <b>REEL ID:</b> "
            f"<code>{esc(result.reel_id)}</code>"
        )

    if result.photo_id:
        lines.append(
            f"🖼 <b>PHOTO ID:</b> "
            f"<code>{esc(result.photo_id)}</code>"
        )

    if result.story_id:
        lines.append(
            f"⭕ <b>STORY ID:</b> "
            f"<code>{esc(result.story_id)}</code>"
        )

    if result.album_id:
        lines.append(
            f"🗂 <b>ALBUM ID:</b> "
            f"<code>{esc(result.album_id)}</code>"
        )

    if result.title:
        lines.append(
            "━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            f"📛 <b>TITLE:</b> "
            f"{esc(truncate(result.title, 500))}"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🔐 <b>VERIFICATION</b>"
    )

    lines.append(
        f"<b>STATUS:</b> "
        f"<b>{esc(result.status)}</b>"
    )

    lines.append(
        f"<b>CONFIDENCE:</b> "
        f"<code>{result.confidence}%</code>"
    )

    independent_sources = len({
        (
            ev.independent
            or ev.source
        )
        for ev in result.evidence
        if ev.role == "USER_CANDIDATE"
        or ev.role in {
            "post_id",
            "video_id",
            "reel_id",
            "photo_id",
            "story_fbid",
            "page_id",
            "group_id",
        }
    })

    lines.append(
        f"<b>EVIDENCE:</b> "
        f"<code>{independent_sources}</code> independent sources"
    )

    # UID evidence summary.
    uid_counts = {
        "user_id": 0,
        "profile_id": 0,
        "owner.id": 0,
        "publisher_id": 0,
        "author_id": 0,
        "actor_id": 0,
        "entity_id": 0,
    }

    for ev in result.evidence:

        if ev.role != "USER_CANDIDATE":
            continue

        source = ev.source.lower()

        if "user_id" in source:
            uid_counts["user_id"] += 1

        elif "profile" in source:
            uid_counts["profile_id"] += 1

        elif "owner" in source:
            uid_counts["owner.id"] += 1

        elif "publisher" in source:
            uid_counts["publisher_id"] += 1

        elif "author" in source:
            uid_counts["author_id"] += 1

        elif "actor" in source:
            uid_counts["actor_id"] += 1

        elif "entity" in source:
            uid_counts["entity_id"] += 1

    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🔬 <b>UID EVIDENCE</b>"
    )

    lines.append(
        f"• user_id: {uid_counts['user_id']}"
    )

    lines.append(
        f"• profile_id: {uid_counts['profile_id']}"
    )

    lines.append(
        f"• owner.id: {uid_counts['owner.id']}"
    )

    lines.append(
        f"• publisher_id: {uid_counts['publisher_id']}"
    )

    lines.append(
        f"• author_id: {uid_counts['author_id']}"
    )

    lines.append(
        f"• actor_id: {uid_counts['actor_id']}"
    )

    lines.append(
        f"• entity_id: {uid_counts['entity_id']}"
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

    if result.notes:

        lines.append(
            "━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            "⚠️ <b>NOTES</b>"
        )

        for note in result.notes[:5]:

            lines.append(
                "• "
                + esc(note)
            )

    lines.append(
        f"⏱ <b>TIME:</b> "
        f"<code>{result.elapsed:.2f}s</code>"
    )

    return "\n".join(
        lines
    )


def truncate(
    value: str,
    limit: int,
) -> str:

    value = value or ""

    if len(value) <= limit:
        return value

    return (
        value[:limit - 1]
        + "…"
    )


# ============================================================
# TELEGRAM INTERACTIVE SESSION
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
        int(event.sender_id or 0),
        int(event.chat_id or 0),
    )


async def acquire_session(
    event,
) -> bool:

    key = await session_key(
        event
    )

    async with _SESSION_LOCK:

        if _ACTIVE_SESSIONS.get(
            key
        ):
            return False

        _ACTIVE_SESSIONS[key] = True

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

    loop = asyncio.get_running_loop()

    future = loop.create_future()

    chat_id = source_event.chat_id

    sender_id = source_event.sender_id

    async def waiter(event):

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
            "🔎 <b>FACEBOOK RESOLVER V22</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📥 Gửi <b>URL Facebook</b> cần phân tích.\n\n"
            "Có thể gửi:\n"
            "• 1 URL\n"
            "• nhiều URL\n"
            "• URL cách nhau bằng khoảng trắng\n"
            "• xuống dòng\n"
            "• dấu phẩy\n"
            "• hoặc dính trực tiếp vào nhau\n\n"
            "Ví dụ:\n"
            "<code>"
            "https://facebook.com/share/p/AAA/"
            "https://facebook.com/user/posts/BBB"
            "</code>\n\n"
            "🔁 Sau khi xử lý xong, chỉ cần gửi URL "
            "tiếp theo.\n"
            "⏹ /stop để kết thúc phiên.",
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
            # Do not consume other bot commands.
            # ------------------------------------------------

            if text.startswith("/"):

                command = (
                    text.split(
                        None,
                        1,
                    )[0]
                    .lower()
                    .split("@", 1)[0]
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
                }:

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
                f"<b>{len(urls)}</b> URL Facebook.\n"
                f"🔬 Đang phân tích từng URL...",
                parse_mode="html",
            )

            total = len(urls)

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
                        "Resolver error: %s",
                        exc,
                    )

                    await next_event.respond(
                        "❌ <b>Resolver error</b>\n"
                        f"<code>{esc(type(exc).__name__)}</code>: "
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
                + esc(str(exc)),
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
    Required by commands/__init__.py:

        module.register(bot, notify_bot)

    IMPORTANT:
    Telethon passes ONE event argument to the handler.
    notify_bot is captured by closure.
    """

    async def handler(event):

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
        "Facebook Resolver V22 Precision"
    )


# ============================================================
# MODULE INFO
# ============================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": (
        "Facebook public URL / UID / entity resolver V22"
    ),
    "version": "22.0.0",
    "engine": "HTTP + HTML + JSON-LD + Canonical",
    "login_required": False,
    "cookies_required": False,
    "playwright_required": False,
    "selenium_required": False,
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
    ]

    for text in tests:

        print(
            "\nINPUT:",
            text,
        )

        for url in extract_facebook_urls(
            text
        ):

            print(
                "  URL:",
                url,
            )

            print(
                "  SHAPE:",
                classify_url(url),
            )