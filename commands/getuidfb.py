#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
======================================================================
 FB UID / ENTITY RESOLVER V18 ULTRA
======================================================================

HTTP ONLY
PUBLIC CONTENT ONLY

NO:
    - Playwright
    - Selenium
    - Cookie
    - Facebook Access Token
    - Facebook Login

TELEGRAM:
    - Telethon
    - Async
    - Concurrent HTTP requests
    - Direct URL:
          /getuidfb https://facebook.com/...
    - Interactive:
          /getuidfb
          -> send URL
    - Multiple URLs in one message

SUPPORTED:
    USER
    PAGE
    GROUP

    POST
    REEL
    VIDEO
    PHOTO
    STORY

    USER_POST
    PAGE_POST
    GROUP_POST

    share/p
    share/v
    share/r

    pfbid
    media_fbid
    actor_id
    profile_id
    entity_id
    user_id
    owner_id
    publisher_id

    canonical URL
    OG metadata
    JSON-LD
    HTML / JS ID correlation
    Base64 Facebook encoded data
    Redirect URL unwrap
    Public URL crawl

IMPORTANT:
    GROUP ID NEVER becomes USER UID.
    PAGE ID NEVER becomes USER UID.

The resolver tries to recover numeric IDs from opaque Facebook
objects by correlating PUBLIC evidence.

It does NOT claim that every pfbid can mathematically be decoded.
Instead it performs multi-source public correlation.
======================================================================
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
import time

from dataclasses import dataclass, field
from typing import Any, Optional, Iterable

from urllib.parse import (
    urlparse,
    urlunparse,
    parse_qs,
    urlencode,
    urljoin,
    unquote,
    quote,
)

import httpx

from telethon import events


# ======================================================================
# COMMAND INFO
# ======================================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": "Facebook public UID / entity resolver",
    "usage": "/getuidfb <facebook_url>",
    "interactive": True,
}


# ======================================================================
# CONFIGURATION
# ======================================================================

HTTP_TIMEOUT = 14.0

MAX_RETRIES = 2

MAX_CONCURRENT_REQUESTS = 6

MAX_INITIAL_PAGES = 6

MAX_PROFILE_PAGES = 3

MAX_RELATED_LINKS = 12

MAX_RESPONSE_BYTES = 5_000_000

MAX_HTML_CONTEXT = 7000

TELEGRAM_MESSAGE_LIMIT = 3900


# ======================================================================
# LOGGING
# ======================================================================

log = logging.getLogger(__name__)


# ======================================================================
# FACEBOOK HOSTS
# ======================================================================

FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "mobile.facebook.com",
    "touch.facebook.com",
    "web.facebook.com",
    "lm.facebook.com",
    "l.facebook.com",
    "fb.watch",
    "www.fb.watch",
}


# ======================================================================
# REGEX
# ======================================================================

PF_BID_RE = re.compile(
    r"\bpfbid[A-Za-z0-9_-]{8,}\b",
    re.I,
)

MEDIA_F_BID_RE = re.compile(
    r"""
    (?:
        media[_-]?fbid
        |
        mediaFbid
    )
    \s*
    [:=]
    \s*
    ["']?
    (\d{5,25})
    """,
    re.I | re.X,
)

NUMERIC_ID_RE = re.compile(
    r"(?<!\d)(\d{5,25})(?!\d)"
)

FB_URL_RE = re.compile(
    r"""
    (?:
        https?://
    )?
    (?:
        (?:www|m|mbasic|mobile|touch|web)\.facebook\.com
        |
        (?:www\.)?fb\.watch
    )
    /[^\s<>"'`]+
    """,
    re.I | re.X,
)

TAG_RE = re.compile(
    r"(?is)<([a-zA-Z][\w:-]*)\b([^>]*)>"
)

ATTR_RE = re.compile(
    r"""
    (?is)
    ([\w:-]+)
    \s*=\s*
    (?:
        "([^"]*)"
        |
        '([^']*)'
        |
        ([^\s"'=<>`]+)
    )
    """,
    re.X,
)

JSON_LD_RE = re.compile(
    r"""
    (?is)
    <script
    [^>]*?
    type\s*=\s*["']application/ld\+json["']
    [^>]*>
    (.*?)
    </script>
    """,
    re.X,
)

KEY_VALUE_RE = re.compile(
    r"""
    (?is)
    ["']
    (
        user[_-]?id
        |
        profile[_-]?id
        |
        owner[_-]?id
        |
        publisher[_-]?id
        |
        actor[_-]?id
        |
        page[_-]?id
        |
        group[_-]?id
        |
        entity[_-]?id
        |
        media[_-]?fbid
        |
        story[_-]?fbid
        |
        post[_-]?id
        |
        video[_-]?id
        |
        photo[_-]?id
    )
    ["']
    \s*
    [:=]
    \s*
    ["']?
    (
        \d{5,25}
    )
    """,
    re.I | re.X,
)

UNQUOTED_KEY_VALUE_RE = re.compile(
    r"""
    (?is)
    \b
    (
        user_id
        |
        profile_id
        |
        owner_id
        |
        publisher_id
        |
        actor_id
        |
        page_id
        |
        group_id
        |
        entity_id
        |
        media_fbid
        |
        story_fbid
        |
        post_id
        |
        video_id
        |
        photo_id
    )
    \b
    \s*
    [:=]
    \s*
    ["']?
    (
        \d{5,25}
    )
    """,
    re.I | re.X,
)

GENERIC_ID_RE = re.compile(
    r"""
    ["']
    id
    ["']
    \s*
    [:=]
    \s*
    ["']
    (\d{5,25})
    ["']
    """,
    re.I | re.X,
)


# ======================================================================
# ID KEY → ROLE
# ======================================================================

ID_ROLE_MAP = {
    "user_id": "user",
    "userid": "user",

    "profile_id": "profile",
    "profileid": "profile",

    "owner_id": "owner",
    "ownerid": "owner",

    "publisher_id": "publisher",
    "publisherid": "publisher",

    "actor_id": "actor",
    "actorid": "actor",

    "page_id": "page",
    "pageid": "page",

    "group_id": "group",
    "groupid": "group",

    "entity_id": "entity",
    "entityid": "entity",

    "media_fbid": "media",

    "story_fbid": "story",
    "storyfbid": "story",

    "post_id": "post",
    "postid": "post",

    "video_id": "video",
    "videoid": "video",

    "photo_id": "photo",
    "photoid": "photo",
}


# ======================================================================
# DATACLASSES
# ======================================================================

@dataclass
class Evidence:
    value: str
    role: str
    source: str
    score: float

    context: str = ""

    independent_source: str = ""

    url: str = ""

    relation: str = ""


@dataclass
class Candidate:
    value: str
    role: str

    evidence: list[Evidence] = field(default_factory=list)

    def add(self, evidence: Evidence) -> None:
        self.evidence.append(evidence)

    @property
    def independent_sources(self) -> int:
        values = set()

        for item in self.evidence:
            values.add(
                item.independent_source
                or item.source
            )

        return len(values)

    @property
    def best_score(self) -> float:
        if not self.evidence:
            return 0

        return max(
            item.score
            for item in self.evidence
        )

    @property
    def score(self) -> float:
        if not self.evidence:
            return 0

        by_source = {}

        for item in self.evidence:
            source = (
                item.independent_source
                or item.source
            )

            old = by_source.get(source, 0)

            if item.score > old:
                by_source[source] = item.score

        total = sum(by_source.values())

        source_count = len(by_source)

        if source_count > 1:
            total += min(
                20,
                (source_count - 1) * 4,
            )

        return min(
            100,
            total,
        )


@dataclass
class Snapshot:
    requested_url: str

    final_url: str

    status_code: int

    content_type: str

    text: str

    elapsed: float

    headers: dict[str, str] = field(
        default_factory=dict
    )

    links: list[str] = field(
        default_factory=list
    )

    error: Optional[str] = None


@dataclass
class ParsedURL:
    original: str

    normalized: str

    final: str = ""

    host: str = ""

    path: str = ""

    url_type: str = "UNKNOWN"

    entity_type: str = "UNKNOWN"

    username: Optional[str] = None

    object_id: Optional[str] = None

    group_id: Optional[str] = None

    page_id: Optional[str] = None

    opaque_id: Optional[str] = None

    share_type: Optional[str] = None


@dataclass
class ResolveResult:
    original_url: str

    resolved_url: str

    url_type: str

    entity_type: str

    status: str

    confidence: int

    user_uid: Optional[str] = None

    page_id: Optional[str] = None

    group_id: Optional[str] = None

    post_id: Optional[str] = None

    video_id: Optional[str] = None

    photo_id: Optional[str] = None

    story_id: Optional[str] = None

    media_fbid: Optional[str] = None

    entity_id: Optional[str] = None

    publisher: Optional[str] = None

    publisher_id: Optional[str] = None

    title: Optional[str] = None

    evidence_count: int = 0

    error: Optional[str] = None


# ======================================================================
# BASIC URL HELPERS
# ======================================================================

def is_facebook_url(url: str) -> bool:
    try:
        host = (
            urlparse(url)
            .netloc
            .lower()
            .split("@")[-1]
            .split(":")[0]
        )

        return (
            host in FACEBOOK_HOSTS
            or host.endswith(".facebook.com")
        )

    except Exception:
        return False


def clean_url(url: str) -> str:
    if not url:
        return ""

    url = html.unescape(
        url.strip()
    )

    url = url.strip(
        "<>[](){}\"'"
    )

    if not re.match(
        r"^https?://",
        url,
        re.I,
    ):
        url = "https://" + url

    p = urlparse(url)

    host = (
        p.netloc
        .lower()
        .split("@")[-1]
    )

    if ":" in host:
        host = host.split(":")[0]

    allowed_query = {
        "id",
        "v",
        "fbid",
        "story_fbid",
        "type",
        "set",
        "substory_index",
        "mibextid",
        "sfnsn",
    }

    query = parse_qs(
        p.query,
        keep_blank_values=True,
    )

    filtered = {}

    for key, values in query.items():
        if key.lower() in allowed_query:
            filtered[key] = values

    path = re.sub(
        r"/{2,}",
        "/",
        p.path or "/",
    )

    if path != "/":
        path = path.rstrip("/")

    return urlunparse(
        (
            "https",
            host,
            path,
            "",
            urlencode(
                filtered,
                doseq=True,
            ),
            "",
        )
    )


def unwrap_redirect(url: str) -> str:
    current = clean_url(url)

    for _ in range(4):
        try:
            parsed = urlparse(current)
            query = parse_qs(
                parsed.query,
                keep_blank_values=True,
            )
        except Exception:
            break

        target = None

        for key in (
            "u",
            "url",
            "target",
            "dest",
            "destination",
            "redirect",
        ):
            values = query.get(key)

            if not values:
                continue

            candidate = unquote(
                values[0]
            )

            if is_facebook_url(candidate):
                target = clean_url(candidate)
                break

        if not target:
            break

        if target == current:
            break

        current = target

    return current


# ======================================================================
# URL PARSER
# ======================================================================

def parse_url(url: str) -> ParsedURL:
    original = url

    normalized = unwrap_redirect(
        clean_url(url)
    )

    parsed = urlparse(
        normalized
    )

    host = parsed.netloc.lower()

    parts = [
        unquote(x)
        for x in parsed.path.split("/")
        if x
    ]

    lower = [
        x.lower()
        for x in parts
    ]

    query = parse_qs(
        parsed.query
    )

    result = ParsedURL(
        original=original,
        normalized=normalized,
        host=host,
        path=parsed.path,
    )

    # --------------------------------------------------------------
    # FB WATCH
    # --------------------------------------------------------------

    if "fb.watch" in host:
        result.url_type = "VIDEO"

        if parts:
            result.object_id = parts[0]

        return result

    # --------------------------------------------------------------
    # PROFILE.PHP
    # --------------------------------------------------------------

    if lower and lower[0] == "profile.php":
        uid = (
            query.get("id")
            or [None]
        )[0]

        result.url_type = "PROFILE"
        result.entity_type = "USER"

        if uid and uid.isdigit():
            result.object_id = uid

        return result

    # --------------------------------------------------------------
    # PHOTO.PHP
    # --------------------------------------------------------------

    if lower and lower[0] == "photo.php":
        result.url_type = "PHOTO"

        result.object_id = (
            query.get("fbid")
            or [None]
        )[0]

        return result

    # --------------------------------------------------------------
    # STORY.PHP
    # --------------------------------------------------------------

    if lower and lower[0] == "story.php":
        result.url_type = "STORY"

        result.object_id = (
            query.get("story_fbid")
            or [None]
        )[0]

        return result

    # --------------------------------------------------------------
    # PAGES
    # --------------------------------------------------------------

    if lower and lower[0] == "pages":

        result.entity_type = "PAGE"

        if (
            len(parts) >= 3
            and parts[2].isdigit()
        ):
            result.page_id = parts[2]
            result.object_id = parts[2]

        if (
            len(parts) >= 5
            and lower[3] in {
                "posts",
                "videos",
                "photos",
                "reels",
            }
        ):

            action = lower[3]

            result.url_type = {
                "posts": "PAGE_POST",
                "videos": "VIDEO",
                "photos": "PHOTO",
                "reels": "REEL",
            }.get(
                action,
                "PAGE",
            )

            result.object_id = parts[4]

        else:
            result.url_type = "PAGE"

        return result

    # --------------------------------------------------------------
    # GROUP
    # --------------------------------------------------------------

    if lower and lower[0] == "groups":

        result.entity_type = "GROUP"

        if (
            len(parts) >= 2
            and parts[1].isdigit()
        ):
            result.group_id = parts[1]

        if (
            len(parts) >= 4
            and lower[2] in {
                "posts",
                "permalink",
            }
        ):

            result.url_type = "GROUP_POST"

            result.object_id = parts[3]

        else:
            result.url_type = "GROUP"

        return result

    # --------------------------------------------------------------
    # SHARE
    # --------------------------------------------------------------

    if (
        len(parts) >= 2
        and lower[0] == "share"
    ):

        share = lower[1]

        result.share_type = share

        result.url_type = {
            "p": "POST",
            "v": "VIDEO",
            "r": "REEL",
        }.get(
            share,
            "SHARE",
        )

        if len(parts) >= 3:
            result.object_id = parts[2]
            result.opaque_id = parts[2]

        return result

    # --------------------------------------------------------------
    # REEL
    # --------------------------------------------------------------

    if lower and lower[0] in {
        "reel",
        "reels",
    }:

        result.url_type = "REEL"

        if len(parts) >= 2:
            result.object_id = parts[1]

        return result

    # --------------------------------------------------------------
    # VIDEOS
    # --------------------------------------------------------------

    if lower and lower[0] == "videos":

        result.url_type = "VIDEO"

        if len(parts) >= 2:
            result.object_id = parts[1]

        return result

    # --------------------------------------------------------------
    # WATCH
    # --------------------------------------------------------------

    if lower and lower[0] == "watch":

        result.url_type = "VIDEO"

        result.object_id = (
            query.get("v")
            or [None]
        )[0]

        return result

    # --------------------------------------------------------------
    # /P/<TOKEN>
    # --------------------------------------------------------------

    if lower and lower[0] == "p":

        result.url_type = "POST"

        if len(parts) >= 2:
            result.object_id = parts[1]
            result.opaque_id = parts[1]

        return result

    # --------------------------------------------------------------
    # GENERIC USERNAME CONTENT
    # --------------------------------------------------------------

    if parts:

        first = parts[0]

        reserved = {
            "home",
            "watch",
            "marketplace",
            "gaming",
            "events",
            "groups",
            "pages",
            "reel",
            "reels",
            "videos",
            "photos",
            "stories",
            "story",
            "photo",
            "search",
            "notifications",
            "messages",
            "friends",
            "settings",
            "login",
            "recover",
            "help",
            "privacy",
        }

        if (
            re.match(
                r"^[A-Za-z0-9._-]{2,100}$",
                first,
            )
            and first.lower() not in reserved
        ):
            result.username = first

        if len(parts) >= 3:

            action = lower[1]
            object_id = parts[2]

            if action == "posts":
                result.url_type = "USER_POST"
                result.object_id = object_id

            elif action == "reels":
                result.url_type = "REEL"
                result.object_id = object_id

            elif action == "videos":
                result.url_type = "VIDEO"
                result.object_id = object_id

            elif action == "photos":
                result.url_type = "PHOTO"
                result.object_id = object_id

            elif action in {
                "story",
                "stories",
            }:
                result.url_type = "STORY"
                result.object_id = object_id

            else:
                result.url_type = "PROFILE"

        elif len(parts) == 1:

            result.url_type = "PROFILE"

    # --------------------------------------------------------------
    # PFBID DETECTION
    # --------------------------------------------------------------

    match = PF_BID_RE.search(
        normalized
    )

    if match:

        result.opaque_id = match.group(0)

        if result.url_type == "UNKNOWN":
            result.url_type = "POST"

    return result


# ======================================================================
# HTML ATTRIBUTE PARSER
# ======================================================================

def parse_attrs(raw: str) -> dict[str, str]:

    result = {}

    for match in ATTR_RE.finditer(raw):

        key = match.group(1).lower()

        value = (
            match.group(2)
            or match.group(3)
            or match.group(4)
            or ""
        )

        result[key] = html.unescape(
            value
        )

    return result


def iter_tags(
    text: str,
    wanted: Optional[set[str]] = None,
):

    for match in TAG_RE.finditer(text):

        tag = match.group(1).lower()

        if wanted is not None:
            if tag not in wanted:
                continue

        attrs = parse_attrs(
            match.group(2)
        )

        yield (
            tag,
            attrs,
            match.group(0),
        )


# ======================================================================
# META
# ======================================================================

def extract_meta(
    text: str,
) -> dict[str, list[str]]:

    result = {}

    for _, attrs, _ in iter_tags(
        text,
        {"meta"},
    ):

        key = (
            attrs.get("property")
            or attrs.get("name")
            or attrs.get("itemprop")
        )

        value = attrs.get(
            "content"
        )

        if not key or not value:
            continue

        key = key.lower()

        result.setdefault(
            key,
            [],
        ).append(
            value
        )

    return result


# ======================================================================
# CANONICAL
# ======================================================================

def extract_canonical(
    text: str,
    base_url: str,
) -> Optional[str]:

    for _, attrs, _ in iter_tags(
        text,
        {"link"},
    ):

        rel = attrs.get(
            "rel",
            "",
        ).lower()

        href = attrs.get(
            "href"
        )

        if not href:
            continue

        if "canonical" not in rel:
            continue

        full = urljoin(
            base_url,
            href,
        )

        if is_facebook_url(full):
            return clean_url(full)

    return None


# ======================================================================
# LINKS
# ======================================================================

def extract_links(
    text: str,
    base_url: str,
) -> list[str]:

    result = []

    seen = set()

    for _, attrs, _ in iter_tags(
        text,
        {"a", "link"},
    ):

        href = attrs.get(
            "href"
        )

        if not href:
            continue

        href = html.unescape(
            href
        )

        if href.startswith(
            (
                "javascript:",
                "mailto:",
                "tel:",
                "#",
            )
        ):
            continue

        full = urljoin(
            base_url,
            href,
        )

        if not is_facebook_url(
            full
        ):
            continue

        full = clean_url(
            full
        )

        if full not in seen:

            seen.add(full)
            result.append(full)

    # URLs embedded in scripts.
    for match in FB_URL_RE.finditer(
        text
    ):

        candidate = match.group(0)

        if not candidate.lower().startswith(
            "http"
        ):
            candidate = (
                "https://"
                + candidate
            )

        candidate = clean_url(
            candidate
        )

        if (
            is_facebook_url(candidate)
            and candidate not in seen
        ):

            seen.add(candidate)
            result.append(candidate)

    return result[
        :MAX_RELATED_LINKS * 3
    ]


# ======================================================================
# JSON-LD
# ======================================================================

def walk_json(
    value: Any,
) -> Iterable[Any]:

    yield value

    if isinstance(
        value,
        dict,
    ):

        for item in value.values():
            yield from walk_json(
                item
            )

    elif isinstance(
        value,
        list,
    ):

        for item in value:
            yield from walk_json(
                item
            )


def parse_json_ld(
    text: str,
) -> list[Any]:

    result = []

    for raw in JSON_LD_RE.findall(
        text
    ):

        raw = html.unescape(
            raw
        ).strip()

        if not raw:
            continue

        try:

            result.append(
                json.loads(raw)
            )

            continue

        except Exception:
            pass

        # Conservative fallback.
        chunks = re.findall(
            r"(?s)\{.*?\}",
            raw,
        )

        for chunk in chunks:

            try:
                result.append(
                    json.loads(chunk)
                )
            except Exception:
                pass

    return result


# ======================================================================
# EVIDENCE STORE
# ======================================================================

class EvidenceStore:

    def __init__(self):

        self.candidates = {}

        self.signals = []

        self.opaque_ids = set()

        self.usernames = set()

    # ------------------------------------------------------------------

    def add(
        self,
        value: Any,
        role: str,
        source: str,
        score: float,
        context: str = "",
        independent_source: str = "",
        url: str = "",
        relation: str = "",
    ) -> None:

        if value is None:
            return

        value = str(value).strip()

        if not value:
            return

        if role not in {
            "user",
            "profile",
            "owner",
            "publisher",
            "page",
            "group",
            "post",
            "video",
            "photo",
            "story",
            "media",
            "entity",
            "actor",
            "generic",
        }:
            role = "generic"

        evidence = Evidence(
            value=value,
            role=role,
            source=source,
            score=score,
            context=context[
                :MAX_HTML_CONTEXT
            ],
            independent_source=(
                independent_source
                or source
            ),
            url=url,
            relation=relation,
        )

        self.signals.append(
            evidence
        )

        key = (
            role,
            value,
        )

        candidate = self.candidates.get(
            key
        )

        if candidate is None:

            candidate = Candidate(
                value=value,
                role=role,
            )

            self.candidates[key] = (
                candidate
            )

        candidate.add(
            evidence
        )

    # ------------------------------------------------------------------

    def add_username(
        self,
        username: str,
    ) -> None:

        username = (
            username
            .strip()
            .lstrip("@")
        )

        if re.match(
            r"^[A-Za-z0-9._-]{2,100}$",
            username,
        ):

            self.usernames.add(
                username
            )

    # ------------------------------------------------------------------

    def add_opaque(
        self,
        value: str,
    ) -> None:

        if value:
            self.opaque_ids.add(
                value
            )

    # ------------------------------------------------------------------

    def candidates_for(
        self,
        *roles: str,
    ) -> list[Candidate]:

        result = [
            candidate
            for candidate
            in self.candidates.values()
            if candidate.role in roles
        ]

        return sorted(
            result,
            key=lambda x: x.score,
            reverse=True,
        )

    # ------------------------------------------------------------------

    @property
    def count(self) -> int:
        return len(
            self.signals
        )


# ======================================================================
# HTTP ENGINE
# ======================================================================

class HTTPClient:

    def __init__(
        self,
        timeout: float = HTTP_TIMEOUT,
    ):

        self.timeout = timeout

        self.sem = asyncio.Semaphore(
            MAX_CONCURRENT_REQUESTS
        )

        self.client: Optional[
            httpx.AsyncClient
        ] = None

    # ------------------------------------------------------------------

    async def __aenter__(self):

        self.client = (
            httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.timeout,
                    connect=8.0,
                ),
                follow_redirects=True,
                headers={
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
                },
                limits=httpx.Limits(
                    max_connections=(
                        MAX_CONCURRENT_REQUESTS
                        + 2
                    ),
                    max_keepalive_connections=(
                        MAX_CONCURRENT_REQUESTS
                    ),
                ),
            )
        )

        return self

    # ------------------------------------------------------------------

    async def __aexit__(
        self,
        exc_type,
        exc,
        tb,
    ):

        if self.client:
            await self.client.aclose()

    # ------------------------------------------------------------------

    async def get(
        self,
        url: str,
    ) -> Snapshot:

        if self.client is None:
            raise RuntimeError(
                "HTTPClient is not initialized"
            )

        last_error = None

        for attempt in range(
            MAX_RETRIES + 1
        ):

            started = (
                time.perf_counter()
            )

            try:

                async with self.sem:

                    response = (
                        await self.client.get(
                            url
                        )
                    )

                elapsed = (
                    time.perf_counter()
                    - started
                )

                content_type = (
                    response.headers.get(
                        "content-type",
                        "application/x-www-form-urlencoded",
                    )
                )

                raw = response.content

                if len(raw) > MAX_RESPONSE_BYTES:
                    raw = raw[
                        :MAX_RESPONSE_BYTES
                    ]

                encoding = (
                    response.encoding
                    or "utf-8"
                )

                text = raw.decode(
                    encoding,
                    "replace",
                )

                return Snapshot(
                    requested_url=url,
                    final_url=str(
                        response.url
                    ),
                    status_code=(
                        response.status_code
                    ),
                    content_type=content_type,
                    text=text,
                    elapsed=elapsed,
                    headers=dict(
                        response.headers
                    ),
                    links=extract_links(
                        text,
                        str(response.url),
                    ),
                )

            except Exception as exc:

                last_error = str(exc)

                if attempt < MAX_RETRIES:

                    await asyncio.sleep(
                        0.35
                        * (attempt + 1)
                    )

        return Snapshot(
            requested_url=url,
            final_url=url,
            status_code=0,
            content_type="",
            text="",
            elapsed=0,
            error=(
                last_error
                or "HTTP request failed"
            ),
        )


# ======================================================================
# EXTRACTOR
# ======================================================================

class Extractor:

    def __init__(
        self,
        store: EvidenceStore,
    ):

        self.store = store

    # ------------------------------------------------------------------

    def extract(
        self,
        snapshot: Snapshot,
        parsed: ParsedURL,
    ) -> None:

        text = snapshot.text

        if not text:
            return

        source = (
            "html:"
            + urlparse(
                snapshot.final_url
            ).netloc.lower()
        )

        self.extract_meta(
            text,
            source,
            snapshot.final_url,
        )

        self.extract_jsonld(
            text,
            source,
            snapshot.final_url,
        )

        self.extract_key_values(
            text,
            source,
            snapshot.final_url,
        )

        self.extract_context_ids(
            text,
            source,
            snapshot.final_url,
        )

        self.extract_pfbid(
            text,
            source,
            snapshot.final_url,
        )

        self.extract_base64(
            text,
            source,
            snapshot.final_url,
        )

        self.extract_publisher(
            text,
            source,
            snapshot.final_url,
            parsed,
        )

    # ------------------------------------------------------------------

    def extract_meta(
        self,
        text: str,
        source: str,
        url: str,
    ) -> None:

        meta = extract_meta(
            text
        )

        for key, values in meta.items():

            for value in values:

                if key in {
                    "og:url",
                    "al:ios:url",
                    "al:android:url",
                    "al:web:url",
                    "og:see_also",
                }:

                    if is_facebook_url(
                        value
                    ):

                        self.extract_url_identity(
                            value,
                            key,
                            source,
                            url,
                        )

                if key in {
                    "user_id",
                    "user:id",
                    "profile_id",
                    "owner_id",
                    "publisher_id",
                    "page_id",
                    "group_id",
                    "actor_id",
                    "entity_id",
                }:

                    role = self.role_from_key(
                        key
                    )

                    for numeric in (
                        NUMERIC_ID_RE.findall(
                            value
                        )
                    ):

                        score = {
                            "user": 94,
                            "profile": 94,
                            "owner": 84,
                            "publisher": 88,
                            "page": 92,
                            "group": 92,
                            "actor": 52,
                            "entity": 66,
                        }.get(
                            role,
                            30,
                        )

                        self.store.add(
                            numeric,
                            role,
                            f"meta:{key}",
                            score,
                            key,
                            f"meta:{key}",
                            url,
                        )

    # ------------------------------------------------------------------

    def extract_jsonld(
        self,
        text: str,
        source: str,
        url: str,
    ) -> None:

        objects = parse_json_ld(
            text
        )

        for root in objects:

            for node in walk_json(
                root
            ):

                if not isinstance(
                    node,
                    dict,
                ):
                    continue

                node_type = str(
                    node.get(
                        "@type",
                        "",
                    )
                ).lower()

                # ------------------------------------------------------
                # AUTHOR / CREATOR / PUBLISHER
                # ------------------------------------------------------

                for key in (
                    "author",
                    "creator",
                    "publisher",
                    "copyrightHolder",
                ):

                    author = node.get(
                        key
                    )

                    if not isinstance(
                        author,
                        list,
                    ):
                        author = [
                            author
                        ]

                    for person in author:

                        if not isinstance(
                            person,
                            dict,
                        ):
                            continue

                        author_url = (
                            person.get("url")
                            or person.get("@id")
                        )

                        author_name = (
                            person.get("name")
                        )

                        identifier = (
                            person.get(
                                "identifier"
                            )
                        )

                        if author_name:

                            self.store.add_username(
                                str(
                                    author_name
                                )
                            )

                        if author_url:

                            if is_facebook_url(
                                str(author_url)
                            ):

                                self.extract_url_identity(
                                    str(author_url),
                                    "jsonld_author",
                                    source,
                                    url,
                                )

                        if identifier:

                            for numeric in (
                                NUMERIC_ID_RE.findall(
                                    str(identifier)
                                )
                            ):

                                self.store.add(
                                    numeric,
                                    "publisher",
                                    "jsonld_author_id",
                                    82,
                                    "JSON-LD author identifier",
                                    "jsonld_author_id",
                                    url,
                                )

                # ------------------------------------------------------
                # MAIN ENTITY URL
                # ------------------------------------------------------

                for key in (
                    "url",
                    "mainEntityOfPage",
                ):

                    value = node.get(
                        key
                    )

                    if isinstance(
                        value,
                        dict,
                    ):

                        value = (
                            value.get("@id")
                            or value.get("url")
                        )

                    if not value:
                        continue

                    value = str(
                        value
                    )

                    if is_facebook_url(
                        value
                    ):

                        self.extract_url_identity(
                            value,
                            f"jsonld:{key}",
                            source,
                            url,
                        )

                # ------------------------------------------------------
                # IDENTIFIER
                # ------------------------------------------------------

                identifier = node.get(
                    "identifier"
                )

                if identifier:

                    for numeric in (
                        NUMERIC_ID_RE.findall(
                            str(identifier)
                        )
                    ):

                        if (
                            "person"
                            in node_type
                            or "profile"
                            in node_type
                        ):

                            self.store.add(
                                numeric,
                                "profile",
                                "jsonld_identifier",
                                74,
                                node_type,
                                "jsonld_identifier",
                                url,
                            )

    # ------------------------------------------------------------------

    def extract_key_values(
        self,
        text: str,
        source: str,
        url: str,
    ) -> None:

        matches = list(
            KEY_VALUE_RE.finditer(
                text
            )
        )

        matches += list(
            UNQUOTED_KEY_VALUE_RE.finditer(
                text
            )
        )

        for match in matches:

            key = (
                match.group(1)
                .lower()
                .replace("-", "_")
            )

            value = match.group(
                2
            )

            role = self.role_from_key(
                key
            )

            score = {
                "user": 96,
                "profile": 94,
                "owner": 86,
                "publisher": 90,
                "page": 93,
                "group": 93,
                "post": 80,
                "video": 74,
                "photo": 74,
                "story": 74,
                "media": 82,
                "entity": 68,
                "actor": 54,
            }.get(
                role,
                25,
            )

            context = text[
                max(
                    0,
                    match.start()
                    - 900,
                ):
                min(
                    len(text),
                    match.end()
                    + 900,
                )
            ]

            self.store.add(
                value,
                role,
                f"embedded:{key}",
                score,
                context,
                f"embedded:{key}",
                url,
            )

    # ------------------------------------------------------------------

    def extract_context_ids(
        self,
        text: str,
        source: str,
        url: str,
    ) -> None:

        patterns = [
            (
                r'"userID"\s*:\s*"(\d{5,25})"',
                "user",
                94,
            ),
            (
                r'"userId"\s*:\s*"(\d{5,25})"',
                "user",
                94,
            ),
            (
                r'"profileID"\s*:\s*"(\d{5,25})"',
                "profile",
                94,
            ),
            (
                r'"profileId"\s*:\s*"(\d{5,25})"',
                "profile",
                94,
            ),
            (
                r'"ownerID"\s*:\s*"(\d{5,25})"',
                "owner",
                86,
            ),
            (
                r'"ownerId"\s*:\s*"(\d{5,25})"',
                "owner",
                86,
            ),
            (
                r'"publisherID"\s*:\s*"(\d{5,25})"',
                "publisher",
                90,
            ),
            (
                r'"publisherId"\s*:\s*"(\d{5,25})"',
                "publisher",
                90,
            ),
            (
                r'"pageID"\s*:\s*"(\d{5,25})"',
                "page",
                92,
            ),
            (
                r'"pageId"\s*:\s*"(\d{5,25})"',
                "page",
                92,
            ),
            (
                r'"groupID"\s*:\s*"(\d{5,25})"',
                "group",
                92,
            ),
            (
                r'"groupId"\s*:\s*"(\d{5,25})"',
                "group",
                92,
            ),
        ]

        for pattern, role, score in patterns:

            for match in re.finditer(
                pattern,
                text,
                re.I,
            ):

                context = text[
                    max(
                        0,
                        match.start()
                        - 1000,
                    ):
                    min(
                        len(text),
                        match.end()
                        + 1000,
                    )
                ]

                self.store.add(
                    match.group(1),
                    role,
                    "context_pattern",
                    score,
                    context,
                    f"context:{role}",
                    url,
                )

    # ------------------------------------------------------------------

    def extract_pfbid(
        self,
        text: str,
        source: str,
        url: str,
    ) -> None:

        pfbids = list(
            dict.fromkeys(
                PF_BID_RE.findall(
                    text
                )
            )
        )

        for pfbid in pfbids:

            self.store.add_opaque(
                pfbid
            )

            for match in re.finditer(
                re.escape(pfbid),
                text,
                re.I,
            ):

                start = max(
                    0,
                    match.start()
                    - 5000,
                )

                end = min(
                    len(text),
                    match.end()
                    + 5000,
                )

                window = text[
                    start:end
                ]

                # ------------------------------------------------------
                # Strong contextual keys
                # ------------------------------------------------------

                for key_match in (
                    KEY_VALUE_RE.finditer(
                        window
                    )
                ):

                    key = (
                        key_match.group(1)
                        .lower()
                        .replace("-", "_")
                    )

                    value = key_match.group(
                        2
                    )

                    role = self.role_from_key(
                        key
                    )

                    score = {
                        "user": 89,
                        "profile": 88,
                        "owner": 82,
                        "publisher": 86,
                        "page": 82,
                        "group": 82,
                        "post": 78,
                        "video": 72,
                        "photo": 72,
                        "story": 72,
                        "media": 76,
                        "entity": 62,
                        "actor": 42,
                    }.get(
                        role,
                        20,
                    )

                    self.store.add(
                        value,
                        role,
                        "pfbid_correlation",
                        score,
                        (
                            pfbid
                            + " :: "
                            + window[:3000]
                        ),
                        "pfbid:" + pfbid,
                        url,
                        relation="pfbid",
                    )

                # ------------------------------------------------------
                # Nearby media_fbid
                # ------------------------------------------------------

                for media_match in (
                    MEDIA_F_BID_RE.finditer(
                        window
                    )
                ):

                    self.store.add(
                        media_match.group(1),
                        "media",
                        "pfbid_media_correlation",
                        80,
                        pfbid,
                        "pfbid_media",
                        url,
                        relation="pfbid->media",
                    )

    # ------------------------------------------------------------------

    def extract_base64(
        self,
        text: str,
        source: str,
        url: str,
    ) -> None:

        # Conservative token scan.
        tokens = re.findall(
            r"""
            (?<![A-Za-z0-9+/=_-])
            [A-Za-z0-9_-]{16,100}
            ={0,2}
            (?![A-Za-z0-9+/=_-])
            """,
            text,
            re.X,
        )

        for token in tokens[:300]:

            if token.lower().startswith(
                "pfbid"
            ):
                continue

            decoded = None

            variants = [
                token,
                token.replace(
                    "-",
                    "+",
                ).replace(
                    "_",
                    "/",
                ),
            ]

            for variant in variants:

                try:

                    padded = (
                        variant
                        + "="
                        * (
                            -len(variant)
                            % 4
                        )
                    )

                    raw = base64.b64decode(
                        padded,
                        validate=False,
                    )

                    candidate = raw.decode(
                        "utf-8",
                        "ignore",
                    )

                    if (
                        candidate
                        and re.search(
                            r"\d{5,25}",
                            candidate,
                        )
                    ):

                        decoded = candidate
                        break

                except Exception:
                    continue

            if not decoded:
                continue

            if not re.search(
                r"""
                user
                |
                profile
                |
                owner
                |
                publisher
                |
                page
                |
                group
                |
                actor
                |
                entity
                |
                fbid
                |
                post
                """,
                decoded,
                re.I | re.X,
            ):
                continue

            for numeric in (
                NUMERIC_ID_RE.findall(
                    decoded
                )
            ):

                self.store.add(
                    numeric,
                    "generic",
                    "base64_context",
                    28,
                    decoded[:1000],
                    "base64_context",
                    url,
                )

    # ------------------------------------------------------------------

    def extract_publisher(
        self,
        text: str,
        source: str,
        url: str,
        parsed: ParsedURL,
    ) -> None:

        if parsed.username:

            self.store.add_username(
                parsed.username
            )

        canonical = extract_canonical(
            text,
            url,
        )

        if canonical:

            self.extract_url_identity(
                canonical,
                "canonical",
                source,
                url,
            )

        meta = extract_meta(
            text
        )

        for key in (
            "og:url",
            "al:ios:url",
            "al:android:url",
            "al:web:url",
        ):

            for value in meta.get(
                key,
                [],
            ):

                if is_facebook_url(
                    value
                ):

                    self.extract_url_identity(
                        value,
                        key,
                        source,
                        url,
                    )

        # Public profile links.
        for link in extract_links(
            text,
            url,
        ):

            link_parsed = parse_url(
                link
            )

            if (
                link_parsed.url_type
                == "PROFILE"
            ):

                if link_parsed.username:

                    self.store.add_username(
                        link_parsed.username
                    )

                if (
                    link_parsed.object_id
                    and link_parsed.object_id.isdigit()
                ):

                    self.store.add(
                        link_parsed.object_id,
                        "profile",
                        "public_profile_link",
                        96,
                        link,
                        "public_profile_link",
                        url,
                    )

    # ------------------------------------------------------------------

    def extract_url_identity(
        self,
        url: str,
        source: str,
        parent_url: str,
        root_url: str,
    ) -> None:

        parsed = parse_url(
            url
        )

        if parsed.username:

            self.store.add_username(
                parsed.username
            )

        if (
            parsed.object_id
            and parsed.url_type
            == "PROFILE"
            and parsed.object_id.isdigit()
        ):

            self.store.add(
                parsed.object_id,
                "profile",
                source,
                97,
                url,
                source,
                root_url,
            )

        if (
            parsed.page_id
            and parsed.page_id.isdigit()
        ):

            self.store.add(
                parsed.page_id,
                "page",
                source,
                96,
                url,
                source,
                root_url,
            )

        if (
            parsed.group_id
            and parsed.group_id.isdigit()
        ):

            self.store.add(
                parsed.group_id,
                "group",
                source,
                96,
                url,
                source,
                root_url,
            )

    # ------------------------------------------------------------------

    @staticmethod
    def role_from_key(
        key: str,
    ) -> str:

        key = (
            key
            .lower()
            .replace("-", "_")
        )

        return ID_ROLE_MAP.get(
            key,
            "generic",
        )


# ======================================================================
# CORRELATION ENGINE
# ======================================================================

class Correlator:

    def __init__(
        self,
        store: EvidenceStore,
        parsed: ParsedURL,
        snapshots: list[Snapshot],
    ):

        self.store = store

        self.parsed = parsed

        self.snapshots = snapshots

    # ------------------------------------------------------------------

    def best(
        self,
        *roles: str,
    ) -> Optional[Candidate]:

        candidates = (
            self.store.candidates_for(
                *roles
            )
        )

        if not candidates:
            return None

        return candidates[0]

    # ------------------------------------------------------------------

    def user_candidates(
        self,
    ) -> list[Candidate]:

        candidates = (
            self.store.candidates_for(
                "user",
                "profile",
                "owner",
                "publisher",
            )
        )

        result = []

        group_ids = {
            item.value
            for item
            in self.store.candidates_for(
                "group"
            )
        }

        page_ids = {
            item.value
            for item
            in self.store.candidates_for(
                "page"
            )
        }

        for candidate in candidates:

            value = candidate.value

            # ----------------------------------------------------------
            # HARD GROUP LOCK
            # ----------------------------------------------------------

            if value in group_ids:
                continue

            # ----------------------------------------------------------
            # HARD PAGE LOCK
            # ----------------------------------------------------------

            if (
                value in page_ids
                and candidate.role
                not in {
                    "user",
                }
            ):
                continue

            if candidate.score < 65:
                continue

            # ----------------------------------------------------------
            # Direct identity evidence
            # ----------------------------------------------------------

            direct = False

            for evidence in (
                candidate.evidence
            ):

                if evidence.source in {
                    "url:profile",
                    "canonical",
                    "public_profile_link",
                    "jsonld_author",
                    "jsonld:mainEntityOfPage",
                    "meta:profile_id",
                    "meta:user_id",
                    "embedded:profile_id",
                    "embedded:user_id",
                    "context_pattern",
                }:

                    direct = True
                    break

            # ----------------------------------------------------------
            # Strong independent correlation
            # ----------------------------------------------------------

            independent = (
                candidate.independent_sources
            )

            if direct:

                result.append(
                    candidate
                )

                continue

            if (
                independent >= 2
                and candidate.score >= 75
            ):

                result.append(
                    candidate
                )

        return sorted(
            result,
            key=lambda x: x.score,
            reverse=True,
        )

    # ------------------------------------------------------------------

    def best_user(
        self,
    ) -> Optional[Candidate]:

        candidates = self.user_candidates()

        if not candidates:
            return None

        # --------------------------------------------------------------
        # USER URL STRUCTURE
        # --------------------------------------------------------------

        if self.parsed.url_type in {
            "USER_POST",
            "PROFILE",
        }:

            # Prefer candidates with profile/user evidence.
            preferred = [
                c
                for c in candidates
                if c.role
                in {
                    "user",
                    "profile",
                }
            ]

            if preferred:
                return preferred[0]

        # --------------------------------------------------------------
        # PUBLISHER RECOVERY
        # --------------------------------------------------------------

        publisher = [
            c
            for c in candidates
            if c.role
            in {
                "publisher",
                "owner",
            }
        ]

        if publisher:
            return publisher[0]

        return candidates[0]

    # ------------------------------------------------------------------

    def entity_type(
        self,
        user: Optional[Candidate],
    ) -> str:

        # GROUP HAS ABSOLUTE PRIORITY.
        if (
            self.parsed.url_type
            == "GROUP_POST"
            or self.parsed.group_id
        ):
            return "GROUP"

        # PAGE STRUCTURE.
        if (
            self.parsed.url_type
            == "PAGE_POST"
            or self.parsed.page_id
        ):
            return "PAGE"

        # Strong page evidence.
        page = self.best(
            "page"
        )

        if page and not user:
            return "PAGE"

        # User evidence.
        if user:
            return "USER"

        if self.parsed.url_type == "PROFILE":
            return "USER"

        return "UNKNOWN"

    # ------------------------------------------------------------------

    def display_type(
        self,
        entity: str,
    ) -> str:

        if self.parsed.url_type == "GROUP_POST":
            return "GROUP_POST"

        if self.parsed.url_type == "PAGE_POST":
            return "PAGE_POST"

        if self.parsed.url_type == "USER_POST":
            return "USER_POST"

        if entity == "GROUP":
            return "GROUP"

        if entity == "PAGE":
            return "PAGE"

        if entity == "USER":

            if self.parsed.url_type == "PROFILE":
                return "USER"

            return self.parsed.url_type

        return self.parsed.url_type

    # ------------------------------------------------------------------

    def object_ids(
        self,
    ) -> dict[str, Optional[str]]:

        def get(
            *roles: str,
        ) -> Optional[str]:

            candidate = self.best(
                *roles
            )

            if candidate:
                return candidate.value

            return None

        result = {
            "post_id": None,
            "video_id": None,
            "photo_id": None,
            "story_id": None,
            "media_fbid": get(
                "media"
            ),
            "entity_id": get(
                "entity"
            ),
        }

        if self.parsed.url_type in {
            "POST",
            "USER_POST",
            "PAGE_POST",
            "GROUP_POST",
        }:

            result["post_id"] = (
                self.parsed.object_id
                or get("post")
            )

        else:

            result["post_id"] = get(
                "post"
            )

        if self.parsed.url_type in {
            "VIDEO",
            "REEL",
        }:

            result["video_id"] = (
                self.parsed.object_id
                or get("video")
            )

        else:

            result["video_id"] = get(
                "video"
            )

        if self.parsed.url_type == "PHOTO":

            result["photo_id"] = (
                self.parsed.object_id
                or get("photo")
            )

        else:

            result["photo_id"] = get(
                "photo"
            )

        if self.parsed.url_type == "STORY":

            result["story_id"] = (
                self.parsed.object_id
                or get("story")
            )

        else:

            result["story_id"] = get(
                "story"
            )

        return result

    # ------------------------------------------------------------------

    def confidence(
        self,
        user: Optional[Candidate],
        entity: str,
    ) -> int:

        score = 35.0

        if user:

            score = max(
                score,
                user.score,
            )

        if entity == "PAGE":

            page = self.best(
                "page"
            )

            if page:
                score = max(
                    score,
                    page.score,
                )

        if entity == "GROUP":

            group = self.best(
                "group"
            )

            if group:
                score = max(
                    score,
                    group.score,
                )

        # Structural classification.
        if self.parsed.url_type in {
            "USER_POST",
            "PAGE_POST",
            "GROUP_POST",
            "PROFILE",
        }:

            score += 4

        # Independent evidence.
        if user:

            if user.independent_sources >= 3:
                score += 6

            elif user.independent_sources >= 2:
                score += 3

        # Conflict detection.
        candidates = self.user_candidates()

        if len(candidates) >= 2:

            first = candidates[0]

            second = candidates[1]

            if (
                first.value
                != second.value
                and
                second.score
                >= first.score - 10
            ):

                score -= 10

        return max(
            0,
            min(
                100,
                round(score),
            ),
        )

    # ------------------------------------------------------------------

    def status(
        self,
        user: Optional[Candidate],
        entity: str,
        confidence: int,
    ) -> str:

        if entity == "GROUP":

            if self.best(
                "group"
            ):
                return "VERIFIED"

            return "RESOLVED"

        if entity == "PAGE":

            if self.best(
                "page"
            ):
                return "VERIFIED"

            return "RESOLVED"

        if user:

            if confidence >= 80:
                return "VERIFIED"

            return "RESOLVED"

        objects = self.object_ids()

        if (
            objects["post_id"]
            or objects["video_id"]
            or objects["photo_id"]
            or objects["story_id"]
        ):

            return "OBJECT_IDENTIFIED"

        return "NOT_RESOLVED"


# ======================================================================
# MAIN RESOLVER
# ======================================================================

class FacebookResolver:

    def __init__(
        self,
        timeout: float = HTTP_TIMEOUT,
    ):

        self.timeout = timeout

    # ------------------------------------------------------------------

    async def resolve(
        self,
        url: str,
    ) -> ResolveResult:

        original = url

        parsed = parse_url(
            url
        )

        if not is_facebook_url(
            parsed.normalized
        ):

            return ResolveResult(
                original_url=original,
                resolved_url=(
                    parsed.normalized
                ),
                url_type=parsed.url_type,
                entity_type="UNKNOWN",
                status="NOT_RESOLVED",
                confidence=0,
                error=(
                    "Unsupported Facebook URL"
                ),
            )

        store = EvidenceStore()

        self.seed_url(
            store,
            parsed,
        )

        snapshots = []

        async with HTTPClient(
            self.timeout
        ) as http:

            # ----------------------------------------------------------
            # FIRST REQUEST
            # ----------------------------------------------------------

            first = await http.get(
                parsed.normalized
            )

            snapshots.append(
                first
            )

            if first.final_url:

                parsed.final = clean_url(
                    first.final_url
                )

            else:

                parsed.final = (
                    parsed.normalized
                )

            # ----------------------------------------------------------
            # REPARSE REDIRECT
            # ----------------------------------------------------------

            final_parsed = parse_url(
                parsed.final
            )

            self.merge_parsed(
                parsed,
                final_parsed,
            )

            self.seed_url(
                store,
                final_parsed,
            )

            # ----------------------------------------------------------
            # EXTRACT FIRST PAGE
            # ----------------------------------------------------------

            Extractor(
                store
            ).extract(
                first,
                parsed,
            )

            # ----------------------------------------------------------
            # BUILD RELATED TARGETS
            # ----------------------------------------------------------

            targets = (
                self.build_targets(
                    parsed,
                    first,
                    store,
                )
            )

            targets = [
                x
                for x in targets
                if x not in {
                    parsed.normalized,
                    parsed.final,
                    first.final_url,
                }
            ]

            targets = targets[
                :MAX_INITIAL_PAGES - 1
            ]

            # ----------------------------------------------------------
            # CONCURRENT CRAWL
            # ----------------------------------------------------------

            if targets:

                results = await asyncio.gather(
                    *(
                        http.get(
                            target
                        )
                        for target in targets
                    ),
                    return_exceptions=True,
                )

                for item in results:

                    if not isinstance(
                        item,
                        Snapshot,
                    ):
                        continue

                    snapshots.append(
                        item
                    )

                    Extractor(
                        store
                    ).extract(
                        item,
                        parsed,
                    )

            # ----------------------------------------------------------
            # PROFILE DISCOVERY
            # ----------------------------------------------------------

            profile_targets = (
                self.build_profile_targets(
                    parsed,
                    store,
                    snapshots,
                )
            )

            profile_targets = profile_targets[
                :MAX_PROFILE_PAGES
            ]

            if profile_targets:

                profile_results = await asyncio.gather(
                    *(
                        http.get(
                            target
                        )
                        for target
                        in profile_targets
                    ),
                    return_exceptions=True,
                )

                for item in profile_results:

                    if not isinstance(
                        item,
                        Snapshot,
                    ):
                        continue

                    snapshots.append(
                        item
                    )

                    Extractor(
                        store
                    ).extract(
                        item,
                        parsed,
                    )

        # --------------------------------------------------------------
        # FINAL CORRELATION
        # --------------------------------------------------------------

        correlator = Correlator(
            store,
            parsed,
            snapshots,
        )

        user = correlator.best_user()

        entity = correlator.entity_type(
            user
        )

        display_type = (
            correlator.display_type(
                entity
            )
        )

        confidence = (
            correlator.confidence(
                user,
                entity,
            )
        )

        status = (
            correlator.status(
                user,
                entity,
                confidence,
            )
        )

        objects = (
            correlator.object_ids()
        )

        # --------------------------------------------------------------
        # PAGE
        # --------------------------------------------------------------

        page_candidate = correlator.best(
            "page"
        )

        page_id = (
            parsed.page_id
            or (
                page_candidate.value
                if page_candidate
                else None
            )
        )

        # --------------------------------------------------------------
        # GROUP
        # --------------------------------------------------------------

        group_candidate = correlator.best(
            "group"
        )

        group_id = (
            parsed.group_id
            or (
                group_candidate.value
                if group_candidate
                else None
            )
        )

        # --------------------------------------------------------------
        # USER UID
        # --------------------------------------------------------------

        user_uid = (
            user.value
            if user
            else None
        )

        # --------------------------------------------------------------
        # HARD SAFETY LOCKS
        # --------------------------------------------------------------

        if user_uid == group_id:
            user_uid = None

        if user_uid == page_id:
            user_uid = None

        if entity == "GROUP":

            # Group ID can never be user UID.
            if user_uid == group_id:
                user_uid = None

        if entity == "PAGE":

            # Page ID is page identity.
            user_uid = None

        # --------------------------------------------------------------
        # PUBLISHER
        # --------------------------------------------------------------

        publisher = (
            parsed.username
            or self.find_publisher(
                parsed,
                snapshots,
            )
        )

        publisher_id = (
            user_uid
            if entity == "USER"
            else None
        )

        if entity == "GROUP":
            publisher_id = (
                user_uid
                if user_uid
                else None
            )

        if entity == "PAGE":
            publisher_id = page_id

        # --------------------------------------------------------------
        # TITLE
        # --------------------------------------------------------------

        title = self.find_title(
            snapshots
        )

        return ResolveResult(
            original_url=original,
            resolved_url=(
                parsed.final
                or parsed.normalized
            ),
            url_type=display_type,
            entity_type=entity,
            status=status,
            confidence=confidence,
            user_uid=user_uid,
            page_id=page_id,
            group_id=group_id,
            post_id=objects["post_id"],
            video_id=objects["video_id"],
            photo_id=objects["photo_id"],
            story_id=objects["story_id"],
            media_fbid=objects["media_fbid"],
            entity_id=objects["entity_id"],
            publisher=publisher,
            publisher_id=publisher_id,
            title=title,
            evidence_count=store.count,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def seed_url(
        store: EvidenceStore,
        parsed: ParsedURL,
    ) -> None:

        if parsed.username:

            store.add_username(
                parsed.username
            )

        if (
            parsed.group_id
            and parsed.group_id.isdigit()
        ):

            store.add(
                parsed.group_id,
                "group",
                "url:group",
                100,
                parsed.normalized,
                "url:group",
                parsed.normalized,
            )

        if (
            parsed.page_id
            and parsed.page_id.isdigit()
        ):

            store.add(
                parsed.page_id,
                "page",
                "url:page",
                100,
                parsed.normalized,
                "url:page",
                parsed.normalized,
            )

        if (
            parsed.url_type
            == "PROFILE"
            and parsed.object_id
            and parsed.object_id.isdigit()
        ):

            store.add(
                parsed.object_id,
                "profile",
                "url:profile",
                100,
                parsed.normalized,
                "url:profile",
                parsed.normalized,
            )

        if parsed.object_id:

            role = {
                "POST": "post",
                "USER_POST": "post",
                "PAGE_POST": "post",
                "GROUP_POST": "post",
                "VIDEO": "video",
                "REEL": "video",
                "PHOTO": "photo",
                "STORY": "story",
            }.get(
                parsed.url_type
            )

            if role:

                store.add(
                    parsed.object_id,
                    role,
                    f"url:{role}",
                    96,
                    parsed.normalized,
                    f"url:{role}",
                    parsed.normalized,
                )

        if parsed.opaque_id:

            store.add_opaque(
                parsed.opaque_id
            )

    # ------------------------------------------------------------------

    @staticmethod
    def merge_parsed(
        destination: ParsedURL,
        source: ParsedURL,
    ) -> None:

        # Do not overwrite strong original fields with empty values.
        for field_name in (
            "final",
            "host",
            "path",
            "url_type",
            "entity_type",
            "username",
            "object_id",
            "group_id",
            "page_id",
            "opaque_id",
            "share_type",
        ):

            value = getattr(
                source,
                field_name,
                None,
            )

            if value:
                setattr(
                    destination,
                    field_name,
                    value,
                )

    # ------------------------------------------------------------------

    @staticmethod
    def build_targets(
        parsed: ParsedURL,
        snapshot: Snapshot,
        store: EvidenceStore,
    ) -> list[str]:

        targets = []

        seen = set()

        def add(
            value: Optional[str],
        ):

            if not value:
                return

            value = clean_url(
                value
            )

            if not is_facebook_url(
                value
            ):
                return

            if value in seen:
                return

            seen.add(value)

            targets.append(
                value
            )

        # Actual redirect.
        add(
            snapshot.final_url
        )

        # Canonical.
        canonical = extract_canonical(
            snapshot.text,
            snapshot.final_url,
        )

        add(
            canonical
        )

        # OG/app URLs.
        meta = extract_meta(
            snapshot.text
        )

        for key in (
            "og:url",
            "al:ios:url",
            "al:android:url",
            "al:web:url",
        ):

            for value in meta.get(
                key,
                [],
            ):

                if is_facebook_url(
                    value
                ):

                    add(
                        value
                    )

        # Relevant public links.
        links = extract_links(
            snapshot.text,
            snapshot.final_url,
        )

        def relevance(
            link: str,
        ) -> int:

            parsed_link = parse_url(
                link
            )

            score = 0

            if (
                parsed.username
                and parsed_link.username
                and
                parsed.username.lower()
                ==
                parsed_link.username.lower()
            ):
                score += 70

            if (
                parsed_link.url_type
                == "PROFILE"
            ):
                score += 55

            if (
                parsed_link.url_type
                in {
                    "USER_POST",
                    "PAGE_POST",
                    "GROUP_POST",
                }
            ):
                score += 20

            return score

        for link in sorted(
            links,
            key=relevance,
            reverse=True,
        ):

            add(
                link
            )

            if len(targets) >= MAX_RELATED_LINKS:
                break

        # Always attempt publisher profile.
        if parsed.username:

            username = quote(
                parsed.username,
                safe="._-",
            )

            add(
                "https://www.facebook.com/"
                + username
            )

        return targets

    # ------------------------------------------------------------------

    @staticmethod
    def build_profile_targets(
        parsed: ParsedURL,
        store: EvidenceStore,
        snapshots: list[Snapshot],
    ) -> list[str]:

        targets = []

        seen = set()

        def add(
            value: Optional[str],
        ):

            if not value:
                return

            if not is_facebook_url(
                value
            ):
                return

            value = clean_url(
                value
            )

            if value in seen:
                return

            seen.add(value)

            targets.append(
                value
            )

        usernames = set(
            store.usernames
        )

        if parsed.username:
            usernames.add(
                parsed.username
            )

        # Search all snapshots for profile URLs.
        for snapshot in snapshots:

            canonical = extract_canonical(
                snapshot.text,
                snapshot.final_url,
            )

            if canonical:

                p = parse_url(
                    canonical
                )

                if (
                    p.url_type
                    == "PROFILE"
                ):

                    add(
                        canonical
                    )

                    if p.username:
                        usernames.add(
                            p.username
                        )

            meta = extract_meta(
                snapshot.text
            )

            for key in (
                "og:url",
                "al:ios:url",
                "al:android:url",
                "al:web:url",
            ):

                for value in meta.get(
                    key,
                    [],
                ):

                    if not is_facebook_url(
                        value
                    ):
                        continue

                    p = parse_url(
                        value
                    )

                    if (
                        p.url_type
                        == "PROFILE"
                    ):

                        add(
                            value
                        )

                        if p.username:
                            usernames.add(
                                p.username
                            )

            for link in extract_links(
                snapshot.text,
                snapshot.final_url,
            ):

                p = parse_url(
                    link
                )

                if (
                    p.url_type
                    == "PROFILE"
                ):

                    add(
                        link
                    )

                    if p.username:
                        usernames.add(
                            p.username
                        )

        # Synthesize profile URLs.
        for username in list(
            usernames
        )[:6]:

            add(
                "https://www.facebook.com/"
                + quote(
                    username,
                    safe="._-",
                )
            )

        # Direct profile ID.
        for candidate in (
            store.candidates_for(
                "profile",
                "user",
            )[:4]
        ):

            if candidate.value.isdigit():

                add(
                    "https://www.facebook.com/"
                    "profile.php?id="
                    + candidate.value
                )

        return targets

    # ------------------------------------------------------------------

    @staticmethod
    def find_publisher(
        parsed: ParsedURL,
        snapshots: list[Snapshot],
    ) -> Optional[str]:

        if parsed.username:
            return parsed.username

        for snapshot in snapshots:

            meta = extract_meta(
                snapshot.text
            )

            for key in (
                "author",
                "article:author",
            ):

                values = meta.get(
                    key,
                    [],
                )

                if values:

                    value = re.sub(
                        r"\s+",
                        " ",
                        values[0],
                    ).strip()

                    if value:
                        return value[:200]

            for root in parse_json_ld(
                snapshot.text
            ):

                for node in walk_json(
                    root
                ):

                    if not isinstance(
                        node,
                        dict,
                    ):
                        continue

                    author = node.get(
                        "author"
                    )

                    if isinstance(
                        author,
                        dict,
                    ):

                        name = author.get(
                            "name"
                        )

                        if name:
                            return str(
                                name
                            )[:200]

                    elif isinstance(
                        author,
                        str,
                    ):

                        return author[:200]

        return None

    # ------------------------------------------------------------------

    @staticmethod
    def find_title(
        snapshots: list[Snapshot],
    ) -> Optional[str]:

        for snapshot in snapshots:

            match = re.search(
                r"(?is)"
                r"<title[^>]*>"
                r"(.*?)"
                r"</title>",
                snapshot.text,
            )

            if not match:
                continue

            value = re.sub(
                r"\s+",
                " ",
                html.unescape(
                    match.group(1)
                ),
            ).strip()

            if not value:
                continue

            if value.lower() in {
                "facebook",
                "facebook - log in or sign up",
                "log in",
            }:
                continue

            return value[:500]

        return None


# ======================================================================
# FORMATTER
# ======================================================================

def escape_html(
    value: Any,
) -> str:

    if value is None:
        return ""

    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def type_icon(
    value: str,
) -> str:

    return {
        "USER": "👤",
        "USER_POST": "👤📝",
        "PAGE": "📄",
        "PAGE_POST": "📄📝",
        "GROUP": "👥",
        "GROUP_POST": "👥📝",
        "POST": "📝",
        "REEL": "🎞",
        "VIDEO": "🎬",
        "PHOTO": "🖼",
        "STORY": "📖",
        "SHARE": "🔗",
        "UNKNOWN": "❓",
    }.get(
        value,
        "🔎",
    )


def format_result(
    result: ResolveResult,
    index: int = 1,
) -> str:

    lines = []

    lines.append(
        "🔎 <b>FACEBOOK UID V18 ULTRA</b>"
    )

    lines.append("")

    lines.append(
        f"<b>{index}.</b> "
        f"{type_icon(result.url_type)} "
        f"<b>{escape_html(result.url_type.replace('_', ' '))}</b>"
    )

    lines.append("")

    lines.append(
        f"📊 <b>STATUS:</b> "
        f"{escape_html(result.status)}"
    )

    # --------------------------------------------------------------
    # USER UID
    # --------------------------------------------------------------

    if result.user_uid:

        lines.append(
            "🆔 <b>USER UID:</b> "
            f"<code>{escape_html(result.user_uid)}</code>"
        )

    # --------------------------------------------------------------
    # PAGE
    # --------------------------------------------------------------

    if result.page_id:

        lines.append(
            "📄 <b>PAGE UID:</b> "
            f"<code>{escape_html(result.page_id)}</code>"
        )

    # --------------------------------------------------------------
    # GROUP
    # --------------------------------------------------------------

    if result.group_id:

        lines.append(
            "👥 <b>GROUP ID:</b> "
            f"<code>{escape_html(result.group_id)}</code>"
        )

    # --------------------------------------------------------------
    # POST
    # --------------------------------------------------------------

    if result.post_id:

        lines.append(
            "📝 <b>POST ID:</b> "
            f"<code>{escape_html(result.post_id)}</code>"
        )

    # --------------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------------

    if result.video_id:

        lines.append(
            "🎬 <b>VIDEO ID:</b> "
            f"<code>{escape_html(result.video_id)}</code>"
        )

    # --------------------------------------------------------------
    # PHOTO
    # --------------------------------------------------------------

    if result.photo_id:

        lines.append(
            "🖼 <b>PHOTO ID:</b> "
            f"<code>{escape_html(result.photo_id)}</code>"
        )

    # --------------------------------------------------------------
    # STORY
    # --------------------------------------------------------------

    if result.story_id:

        lines.append(
            "📖 <b>STORY ID:</b> "
            f"<code>{escape_html(result.story_id)}</code>"
        )

    # --------------------------------------------------------------
    # MEDIA FBID
    # --------------------------------------------------------------

    if result.media_fbid:

        lines.append(
            "🧩 <b>MEDIA FBID:</b> "
            f"<code>{escape_html(result.media_fbid)}</code>"
        )

    # --------------------------------------------------------------
    # ENTITY
    # --------------------------------------------------------------

    if result.entity_id:

        lines.append(
            "🔗 <b>ENTITY ID:</b> "
            f"<code>{escape_html(result.entity_id)}</code>"
        )

    # --------------------------------------------------------------
    # PUBLISHER
    # --------------------------------------------------------------

    if result.publisher:

        lines.append(
            "👤 <b>PUBLISHER:</b> "
            f"{escape_html(result.publisher)}"
        )

    # --------------------------------------------------------------
    # PUBLISHER ID
    # --------------------------------------------------------------

    if (
        result.publisher_id
        and result.publisher_id
        not in {
            result.group_id,
            result.page_id,
        }
    ):

        if (
            not result.user_uid
            or result.publisher_id
            != result.user_uid
        ):

            lines.append(
                "🆔 <b>PUBLISHER ID:</b> "
                f"<code>"
                f"{escape_html(result.publisher_id)}"
                f"</code>"
            )

    # --------------------------------------------------------------
    # TITLE
    # --------------------------------------------------------------

    if result.title:

        lines.append(
            "📝 <b>TITLE:</b> "
            f"{escape_html(result.title)}"
        )

    lines.append("")

    lines.append(
        f"🎯 <b>CONFIDENCE:</b> "
        f"{result.confidence}%"
    )

    # --------------------------------------------------------------
    # SOURCE
    # --------------------------------------------------------------

    if result.resolved_url:

        safe_url = (
            escape_html(
                result.resolved_url
            )
        )

        lines.append("")

        lines.append(
            f'🔗 <a href="{safe_url}">'
            f"Facebook URL"
            f"</a>"
        )

    if result.error:

        lines.append("")

        lines.append(
            "⚠️ <b>ERROR:</b> "
            f"{escape_html(result.error)}"
        )

    return "\n".join(
        lines
    )


# ======================================================================
# URL INPUT
# ======================================================================

def extract_urls(
    text: str,
) -> list[str]:

    result = []

    seen = set()

    # Explicit URLs.
    for match in re.findall(
        r"https?://[^\s<>'\"`]+",
        text,
        re.I,
    ):

        value = match.rstrip(
            ".,;!?)]}"
        )

        if is_facebook_url(
            value
        ):

            value = clean_url(
                value
            )

            if value not in seen:

                seen.add(
                    value
                )

                result.append(
                    value
                )

    # Facebook URL without scheme.
    for match in FB_URL_RE.finditer(
        text
    ):

        value = match.group(0)

        value = value.rstrip(
            ".,;!?)]}"
        )

        if not value.lower().startswith(
            "http"
        ):

            value = (
                "https://"
                + value
            )

        if is_facebook_url(
            value
        ):

            value = clean_url(
                value
            )

            if value not in seen:

                seen.add(
                    value
                )

                result.append(
                    value
                )

    return result


# ======================================================================
# PUBLIC API
# ======================================================================

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
        *(
            resolver.resolve(
                url
            )
            for url in urls
        ),
        return_exceptions=True,
    )

    output = []

    for url, result in zip(
        urls,
        results,
    ):

        if isinstance(
            result,
            ResolveResult,
        ):

            output.append(
                result
            )

        else:

            output.append(
                ResolveResult(
                    original_url=url,
                    resolved_url=url,
                    url_type="UNKNOWN",
                    entity_type="UNKNOWN",
                    status="NOT_RESOLVED",
                    confidence=0,
                    error=str(result),
                )
            )

    return output


# ======================================================================
# TELEGRAM REGISTRATION
# ======================================================================

def register(
    client,
    *args,
    **kwargs,
):

    @client.on(
        events.NewMessage(
            pattern=r"^/getuidfb(?:\s+([\s\S]+))?$"
        )
    )
    async def getuidfb_handler(
        event,
    ):

        raw = (
            event.pattern_match
            .group(1)
            or ""
        ).strip()

        # --------------------------------------------------------------
        # INTERACTIVE MODE
        # --------------------------------------------------------------

        if not raw:

            prompt = await event.respond(
                "🔎 <b>FACEBOOK UID RESOLVER</b>\n\n"
                "📥 Gửi link Facebook cần kiểm tra.\n"
                "• Có thể gửi nhiều link.\n"
                "• Chỉ xử lý nội dung công khai.\n"
                "• Không cần Facebook Login.",
                parse_mode="html",
            )

            try:

                response = (
                    await client.wait_for(
                        events.NewMessage(
                            chats=event.chat_id,
                            from_users=event.sender_id,
                        ),
                        timeout=120,
                    )
                )

                raw = (
                    response.raw_text
                    or ""
                ).strip()

            except Exception:

                try:

                    await prompt.edit(
                        "⌛ <b>Hết thời gian chờ.</b>\n"
                        "Gửi lại <code>/getuidfb</code> "
                        "để tiếp tục.",
                        parse_mode="html",
                    )

                except Exception:
                    pass

                return

            try:
                await prompt.delete()
            except Exception:
                pass

        # --------------------------------------------------------------
        # EXTRACT URLS
        # --------------------------------------------------------------

        urls = extract_urls(
            raw
        )

        if not urls:

            await event.respond(
                "❌ Không tìm thấy Facebook URL hợp lệ.",
                parse_mode="html",
            )

            return

        # --------------------------------------------------------------
        # PROCESS
        # --------------------------------------------------------------

        status_message = (
            await event.respond(
                "🔎 Đang phân tích "
                f"<b>{len(urls)}</b> Facebook URL...",
                parse_mode="html",
            )
        )

        results = await resolve_facebook_urls(
            urls
        )

        chunks = []

        for index, result in enumerate(
            results,
            1,
        ):

            chunks.append(
                format_result(
                    result,
                    index,
                )
            )

        combined = (
            "\n\n"
            "━━━━━━━━━━━━━━━━━━━━"
            "\n\n"
        ).join(
            chunks
        )

        # --------------------------------------------------------------
        # SINGLE MESSAGE
        # --------------------------------------------------------------

        if len(combined) <= TELEGRAM_MESSAGE_LIMIT:

            try:

                await status_message.edit(
                    combined,
                    parse_mode="html",
                    link_preview=False,
                )

            except Exception:

                try:
                    await event.respond(
                        combined,
                        parse_mode="html",
                        link_preview=False,
                    )
                except Exception:
                    pass

            return

        # --------------------------------------------------------------
        # SPLIT MESSAGES
        # --------------------------------------------------------------

        try:
            await status_message.delete()
        except Exception:
            pass

        current = ""

        for chunk in chunks:

            piece = (
                chunk
                + "\n\n"
                + "━━━━━━━━━━━━━━━━━━━━"
                + "\n\n"
            )

            if (
                current
                and
                len(current)
                + len(piece)
                > TELEGRAM_MESSAGE_LIMIT
            ):

                try:

                    await event.respond(
                        current.rstrip(),
                        parse_mode="html",
                        link_preview=False,
                    )

                except Exception:
                    pass

                current = ""

            current += piece

        if current.strip():

            try:

                await event.respond(
                    current.rstrip(),
                    parse_mode="html",
                    link_preview=False,
                )

            except Exception:
                pass

    return getuidfb_handler


# ======================================================================
# CLI TEST
# ======================================================================

async def cli() -> None:

    import sys

    text = " ".join(
        sys.argv[1:]
    )

    urls = extract_urls(
        text
    )

    if not urls:

        print(
            "Usage:"
        )

        print(
            "python getuidfb.py "
            "<facebook_url>"
        )

        return

    results = (
        await resolve_facebook_urls(
            urls
        )
    )

    for result in results:

        print("=" * 80)

        print(
            "TYPE       :",
            result.url_type,
        )

        print(
            "ENTITY     :",
            result.entity_type,
        )

        print(
            "STATUS     :",
            result.status,
        )

        print(
            "CONFIDENCE :",
            result.confidence,
        )

        print(
            "USER UID   :",
            result.user_uid,
        )

        print(
            "PAGE UID   :",
            result.page_id,
        )

        print(
            "GROUP ID   :",
            result.group_id,
        )

        print(
            "POST ID    :",
            result.post_id,
        )

        print(
            "VIDEO ID   :",
            result.video_id,
        )

        print(
            "PHOTO ID   :",
            result.photo_id,
        )

        print(
            "STORY ID   :",
            result.story_id,
        )

        print(
            "MEDIA FBID :",
            result.media_fbid,
        )

        print(
            "ENTITY ID  :",
            result.entity_id,
        )

        print(
            "PUBLISHER  :",
            result.publisher,
        )

        print(
            "PUBLISHER ID:",
            result.publisher_id,
        )

        print(
            "EVIDENCE   :",
            result.evidence_count,
        )

        print(
            "RESOLVED   :",
            result.resolved_url,
        )


# ======================================================================
# MAIN
# ======================================================================

if __name__ == "__main__":

    asyncio.run(
        cli()
    )