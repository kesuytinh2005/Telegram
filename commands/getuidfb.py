#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
====================================================================
 FACEBOOK UID / ENTITY RESOLVER V19 ULTRA
 TELEGRAM / TELETHON
====================================================================

PUBLIC HTTP ONLY

SUPPORTED:
    USER
    PAGE
    GROUP

    PROFILE
    USER_POST
    PAGE_POST
    GROUP_POST

    POST
    REEL
    VIDEO
    PHOTO
    STORY

    /share/p/
    /share/v/
    /share/r/
    /p/pfbid...
    /profile.php?id=
    /photo.php?fbid=
    /story.php?story_fbid=
    /watch?v=
    /groups/<id>/posts/<id>
    /pages/<name>/<id>/posts/<id>
    /username/posts/<id>
    /username/reels/<id>
    /username/videos/<id>

UID RECOVERY:
    numeric UID
    profile_id
    user_id
    owner_id
    publisher_id
    actor_id
    page_id
    group_id
    entity_id
    media_fbid
    video_id
    post_id

OPAQUE OBJECT:
    pfbid...

CORRELATION:
    pfbid
       ↓
    object/post
       ↓
    publisher/owner/actor
       ↓
    public profile
       ↓
    numeric UID
       ↓
    verification

NO:
    Playwright
    Selenium
    Cookies
    Facebook Access Token
    Facebook Login
====================================================================
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
from typing import Any, Iterable, Optional
from urllib.parse import (
    parse_qs,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import httpx
from telethon import events


# ====================================================================
# LOGGING
# ====================================================================

logger = logging.getLogger(__name__)


# ====================================================================
# COMMAND INFO
# ====================================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": "Facebook UID / Entity Resolver V19 ULTRA",
    "usage": "/getuidfb <facebook_url>",
}


# ====================================================================
# LIMITS
# ====================================================================

MAX_HTML_CHARS = 4_000_000
MAX_RESPONSE_BYTES = 12_000_000

MAX_CRAWL_PAGES = 7
MAX_PROFILE_PAGES = 3
MAX_RELATED_LINKS = 25

MAX_URLS_PER_REQUEST = 12

HTTP_CONCURRENCY = 6

HTTP_TIMEOUT = 18.0
HTTP_RETRIES = 2

MAX_EVIDENCE_CONTEXT = 900

MIN_NUMERIC_ID_LENGTH = 5
MAX_NUMERIC_ID_LENGTH = 25


# ====================================================================
# FACEBOOK HOSTS
# ====================================================================

FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "mobile.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
    "developers.facebook.com",
    "fb.com",
    "www.fb.com",
}


# ====================================================================
# RESERVED ROUTES
# ====================================================================

FB_RESERVED = {
    "home",
    "watch",
    "groups",
    "pages",
    "profile.php",
    "photo.php",
    "story.php",
    "reel",
    "reels",
    "videos",
    "video",
    "photos",
    "photo",
    "stories",
    "story",
    "marketplace",
    "gaming",
    "events",
    "search",
    "notifications",
    "messages",
    "friends",
    "settings",
    "login",
    "recover",
    "help",
    "privacy",
    "share",
    "p",
    "permalink",
}


# ====================================================================
# REGEX
# ====================================================================

NUMERIC_ID_RE = re.compile(
    rf"\b\d{{{MIN_NUMERIC_ID_LENGTH},{MAX_NUMERIC_ID_LENGTH}}}\b"
)

PF_BID_RE = re.compile(
    r"\bpfbid[A-Za-z0-9_-]+\b",
    re.I,
)

AQ_ID_RE = re.compile(
    r"\bAQ[A-Za-z0-9_-]{10,}\b",
    re.I,
)

URL_RE = re.compile(
    r"https?://[^\s<>\"]+",
    re.I,
)


# ====================================================================
# DATA STRUCTURES
# ====================================================================

@dataclass
class ParsedURL:
    original: str

    normalized: str = ""
    final: str = ""

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

    share_type: Optional[str] = None

    query: dict[str, list[str]] = field(
        default_factory=dict
    )


@dataclass
class Snapshot:
    requested_url: str

    final_url: str = ""

    status_code: int = 0

    content_type: str = ""

    text: str = ""

    headers: dict[str, str] = field(
        default_factory=dict
    )

    elapsed: float = 0.0

    error: Optional[str] = None


@dataclass
class Evidence:
    value: str

    role: str

    source: str

    score: float

    context: str = ""

    independent: bool = True

    url: str = ""

    relation: str = ""


@dataclass
class Candidate:
    value: str

    role: str

    evidence: list[Evidence] = field(
        default_factory=list
    )

    @property
    def score(self) -> float:

        if not self.evidence:
            return 0.0

        base = max(
            e.score
            for e in self.evidence
        )

        sources = {
            e.source
            for e in self.evidence
            if e.independent
        }

        bonus = min(
            18,
            max(0, len(sources) - 1) * 6,
        )

        return min(
            100.0,
            base + bonus,
        )

    @property
    def independent_sources(self) -> int:

        return len({
            e.source
            for e in self.evidence
            if e.independent
        })

    @property
    def best_evidence(self) -> Optional[Evidence]:

        if not self.evidence:
            return None

        return max(
            self.evidence,
            key=lambda x: x.score,
        )


@dataclass
class ResolveResult:
    original_url: str

    resolved_url: str = ""

    url_type: str = "UNKNOWN"

    entity_type: str = "UNKNOWN"

    confidence: float = 0.0

    verified: bool = False

    username: Optional[str] = None

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

    error: Optional[str] = None

    evidence_count: int = 0


# ====================================================================
# BASIC HELPERS
# ====================================================================

def is_numeric_id(
    value: Optional[str],
) -> bool:

    if not value:
        return False

    return bool(
        re.fullmatch(
            rf"\d{{{MIN_NUMERIC_ID_LENGTH},{MAX_NUMERIC_ID_LENGTH}}}",
            value.strip(),
        )
    )


def is_opaque_id(
    value: Optional[str],
) -> bool:

    if not value:
        return False

    value = value.strip()

    return bool(
        PF_BID_RE.fullmatch(value)
        or AQ_ID_RE.fullmatch(value)
    )


def first_query(
    query: dict[str, list[str]],
    key: str,
) -> Optional[str]:

    values = query.get(key)

    if not values:
        return None

    value = values[0]

    if not value:
        return None

    return unquote(
        value
    )


def truncate(
    value: str,
    size: int,
) -> str:

    if not value:
        return ""

    value = value.strip()

    if len(value) <= size:
        return value

    return value[: size - 1] + "…"


def tg_escape(
    value: Any,
) -> str:

    return html.escape(
        str(value or ""),
        quote=False,
    )


def clean_url(
    url: str,
) -> str:

    if not url:
        return ""

    url = html.unescape(
        url.strip()
    )

    url = url.strip(
        "\"'<>[]()"
    )

    url = url.replace(
        "\\/",
        "/",
    )

    return url


def normalize_host(
    host: str,
) -> str:

    return (
        host
        .lower()
        .split(":")[0]
        .strip()
    )


def is_facebook_host(
    host: str,
) -> bool:

    host = normalize_host(
        host
    )

    return (
        host in FACEBOOK_HOSTS
        or host.endswith(
            ".facebook.com"
        )
        or host.endswith(
            ".fb.com"
        )
    )


def is_facebook_url(
    url: str,
) -> bool:

    try:
        return is_facebook_host(
            urlparse(url).netloc
        )
    except Exception:
        return False


# ====================================================================
# REDIRECT / WRAPPER UNWRAPPER
# ====================================================================

def unwrap_redirect(
    url: str,
) -> str:

    url = clean_url(
        url
    )

    if not url:
        return url

    try:
        parsed = urlparse(
            url
        )

        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
        )

        for key in (
            "u",
            "url",
            "target",
            "redirect",
            "redirect_uri",
            "dest",
            "destination",
        ):

            value = first_query(
                query,
                key,
            )

            if (
                value
                and
                value.startswith(
                    ("http://", "https://")
                )
            ):

                return value

    except Exception:
        pass

    return url


# ====================================================================
# PATH TOKENS
# ====================================================================

def path_tokens(
    path: str,
) -> list[str]:

    path = unquote(
        path or ""
    )

    path = path.replace(
        "\\/",
        "/",
    )

    return [
        unquote(x)
        for x in path.split("/")
        if x
    ]


# ====================================================================
# ADVANCED FACEBOOK URL CLASSIFIER
# ====================================================================

def classify_facebook_url(
    url: str,
) -> ParsedURL:

    original = url

    url = unwrap_redirect(
        clean_url(url)
    )

    parsed = urlparse(
        url
    )

    host = normalize_host(
        parsed.netloc
    )

    tokens = path_tokens(
        parsed.path
    )

    lower = [
        x.lower()
        for x in tokens
    ]

    query = parse_qs(
        parsed.query,
        keep_blank_values=True,
    )

    result = ParsedURL(
        original=original,
        normalized=url,
        final=url,
        host=host,
        path=parsed.path,
        query=query,
    )

    # ================================================================
    # FB.WATCH
    # ================================================================

    if host.endswith(
        "fb.watch"
    ):

        result.url_type = "VIDEO"
        result.entity_type = "VIDEO"

        if tokens:
            result.object_id = tokens[0]

        return result

    # ================================================================
    # PROFILE.PHP
    # ================================================================

    if (
        lower
        and
        lower[0] == "profile.php"
    ):

        uid = first_query(
            query,
            "id",
        )

        result.url_type = "PROFILE"
        result.entity_type = "USER"

        if is_numeric_id(uid):
            result.profile_id = uid
            result.object_id = uid

        return result

    # ================================================================
    # PHOTO.PHP
    # ================================================================

    if (
        lower
        and
        lower[0] == "photo.php"
    ):

        fbid = first_query(
            query,
            "fbid",
        )

        result.url_type = "PHOTO"
        result.entity_type = "PHOTO"
        result.object_id = fbid

        if is_opaque_id(fbid):
            result.opaque_id = fbid

        return result

    # ================================================================
    # STORY.PHP
    # ================================================================

    if (
        lower
        and
        lower[0] == "story.php"
    ):

        story_fbid = first_query(
            query,
            "story_fbid",
        )

        result.url_type = "STORY"
        result.entity_type = "STORY"
        result.object_id = story_fbid

        if is_opaque_id(story_fbid):
            result.opaque_id = story_fbid

        story_owner = first_query(
            query,
            "id",
        )

        if is_numeric_id(
            story_owner
        ):
            result.profile_id = story_owner

        return result

    # ================================================================
    # WATCH
    # ================================================================

    if (
        lower
        and
        lower[0] == "watch"
    ):

        video_id = first_query(
            query,
            "v",
        )

        result.url_type = "WATCH"
        result.entity_type = "VIDEO"
        result.object_id = video_id

        if is_opaque_id(video_id):
            result.opaque_id = video_id

        return result

    # ================================================================
    # SHARE
    # ================================================================

    if (
        len(lower) >= 2
        and
        lower[0] == "share"
    ):

        share_type = lower[1]

        token = (
            tokens[2]
            if len(tokens) >= 3
            else None
        )

        result.share_type = share_type

        if share_type == "p":

            result.url_type = "SHARE_POST"
            result.entity_type = "POST"

        elif share_type == "v":

            result.url_type = "SHARE_VIDEO"
            result.entity_type = "VIDEO"

        elif share_type == "r":

            result.url_type = "SHARE_REEL"
            result.entity_type = "VIDEO"

        else:

            result.url_type = "UNKNOWN"

        result.object_id = token

        if is_opaque_id(token):
            result.opaque_id = token

        return result

    # ================================================================
    # /P/PFBID
    # ================================================================

    if (
        lower
        and
        lower[0] == "p"
    ):

        token = (
            tokens[1]
            if len(tokens) >= 2
            else None
        )

        result.url_type = "POST"
        result.entity_type = "POST"
        result.object_id = token

        if is_opaque_id(token):
            result.opaque_id = token

        return result

    # ================================================================
    # /REEL
    # ================================================================

    if (
        lower
        and
        lower[0] in {
            "reel",
            "reels",
        }
    ):

        token = (
            tokens[1]
            if len(tokens) >= 2
            else None
        )

        result.url_type = "REEL"
        result.entity_type = "VIDEO"
        result.object_id = token

        if is_opaque_id(token):
            result.opaque_id = token

        return result

    # ================================================================
    # /VIDEOS
    # ================================================================

    if (
        lower
        and
        lower[0] in {
            "video",
            "videos",
        }
    ):

        token = (
            tokens[1]
            if len(tokens) >= 2
            else None
        )

        result.url_type = "VIDEO"
        result.entity_type = "VIDEO"
        result.object_id = token

        if is_opaque_id(token):
            result.opaque_id = token

        return result

    # ================================================================
    # /GROUPS
    # ================================================================

    if (
        lower
        and
        lower[0] == "groups"
    ):

        result.entity_type = "GROUP"

        group_id = (
            tokens[1]
            if len(tokens) >= 2
            else None
        )

        if is_numeric_id(group_id):
            result.group_id = group_id

        if len(lower) >= 4:

            action = lower[2]
            object_id = tokens[3]

            if action in {
                "posts",
                "post",
                "permalink",
            }:

                result.url_type = "GROUP_POST"
                result.object_id = object_id

                if is_opaque_id(object_id):
                    result.opaque_id = object_id

                return result

            if action in {
                "reels",
                "reel",
            }:

                result.url_type = "REEL"
                result.entity_type = "GROUP"
                result.object_id = object_id

                return result

            if action in {
                "videos",
                "video",
            }:

                result.url_type = "VIDEO"
                result.entity_type = "GROUP"
                result.object_id = object_id

                return result

        result.url_type = "GROUP"

        return result

    # ================================================================
    # /PAGES/<NAME>/<ID>
    # ================================================================

    if (
        lower
        and
        lower[0] == "pages"
    ):

        result.entity_type = "PAGE"

        if len(tokens) >= 3:

            page_id = tokens[2]

            if is_numeric_id(page_id):
                result.page_id = page_id

        if len(lower) >= 5:

            action = lower[3]
            object_id = tokens[4]

            if action in {
                "posts",
                "post",
            }:

                result.url_type = "PAGE_POST"
                result.object_id = object_id

                if is_opaque_id(object_id):
                    result.opaque_id = object_id

                return result

            if action in {
                "reels",
                "reel",
            }:

                result.url_type = "REEL"
                result.entity_type = "PAGE"
                result.object_id = object_id

                return result

            if action in {
                "videos",
                "video",
            }:

                result.url_type = "VIDEO"
                result.entity_type = "PAGE"
                result.object_id = object_id

                return result

            if action in {
                "photos",
                "photo",
            }:

                result.url_type = "PHOTO"
                result.entity_type = "PAGE"
                result.object_id = object_id

                return result

        result.url_type = "PAGE"

        return result

    # ================================================================
    # USERNAME / USER ROUTE
    # ================================================================

    if tokens:

        first = tokens[0]

        if (
            first.lower()
            not in FB_RESERVED
            and
            re.fullmatch(
                r"[A-Za-z0-9._-]{2,100}",
                first,
            )
        ):

            result.username = first

            # --------------------------------------------------------
            # /username/posts/id
            # --------------------------------------------------------

            if len(lower) >= 3:

                action = lower[1]
                object_id = tokens[2]

                if action in {
                    "posts",
                    "post",
                }:

                    result.url_type = "USER_POST"
                    result.entity_type = "USER"
                    result.object_id = object_id

                    if is_opaque_id(object_id):
                        result.opaque_id = object_id

                    return result

                # ----------------------------------------------------
                # REEL
                # ----------------------------------------------------

                if action in {
                    "reel",
                    "reels",
                }:

                    result.url_type = "REEL"
                    result.entity_type = "USER"
                    result.object_id = object_id

                    if is_opaque_id(object_id):
                        result.opaque_id = object_id

                    return result

                # ----------------------------------------------------
                # VIDEO
                # ----------------------------------------------------

                if action in {
                    "video",
                    "videos",
                }:

                    result.url_type = "VIDEO"
                    result.entity_type = "USER"
                    result.object_id = object_id

                    if is_opaque_id(object_id):
                        result.opaque_id = object_id

                    return result

                # ----------------------------------------------------
                # PHOTO
                # ----------------------------------------------------

                if action in {
                    "photo",
                    "photos",
                }:

                    result.url_type = "PHOTO"
                    result.entity_type = "USER"
                    result.object_id = object_id

                    if is_opaque_id(object_id):
                        result.opaque_id = object_id

                    return result

                # ----------------------------------------------------
                # STORY
                # ----------------------------------------------------

                if action in {
                    "story",
                    "stories",
                }:

                    result.url_type = "STORY"
                    result.entity_type = "USER"
                    result.object_id = object_id

                    if is_opaque_id(object_id):
                        result.opaque_id = object_id

                    return result

            # --------------------------------------------------------
            # SINGLE USERNAME
            # --------------------------------------------------------

            result.url_type = "PROFILE"
            result.entity_type = "USER"

            return result

    # ================================================================
    # QUERY FALLBACK
    # ================================================================

    fbid = first_query(
        query,
        "fbid",
    )

    if fbid:

        result.url_type = "PHOTO"
        result.entity_type = "PHOTO"
        result.object_id = fbid

        if is_opaque_id(fbid):
            result.opaque_id = fbid

        return result

    video = first_query(
        query,
        "v",
    )

    if video:

        result.url_type = "VIDEO"
        result.entity_type = "VIDEO"
        result.object_id = video

        return result

    story = first_query(
        query,
        "story_fbid",
    )

    if story:

        result.url_type = "STORY"
        result.entity_type = "STORY"
        result.object_id = story

        return result

    return result


# Alias
parse_url = classify_facebook_url


# ====================================================================
# HTML META EXTRACTION
# ====================================================================

def extract_meta(
    text: str,
) -> dict[str, list[str]]:

    result: dict[str, list[str]] = {}

    if not text:
        return result

    pattern = re.compile(
        r"<meta\b([^>]+)>",
        re.I | re.S,
    )

    for match in pattern.finditer(text):

        attrs = match.group(1)

        name_match = re.search(
            r"""
            \b(?:property|name|itemprop)
            \s*=\s*
            ["']([^"']+)["']
            """,
            attrs,
            re.I | re.X,
        )

        content_match = re.search(
            r"""
            \bcontent
            \s*=\s*
            ["'](.*?)["']
            """,
            attrs,
            re.I | re.S | re.X,
        )

        if not (
            name_match
            and
            content_match
        ):
            continue

        key = (
            name_match
            .group(1)
            .strip()
            .lower()
        )

        value = html.unescape(
            content_match
            .group(1)
            .strip()
        )

        if not value:
            continue

        result.setdefault(
            key,
            [],
        ).append(value)

    return result


# ====================================================================
# LINK EXTRACTION
# ====================================================================

def extract_links(
    text: str,
    base_url: str,
) -> list[str]:

    results = []
    seen = set()

    pattern = re.compile(
        r"<a\b([^>]+)>",
        re.I | re.S,
    )

    for match in pattern.finditer(text):

        attrs = match.group(1)

        href_match = re.search(
            r"""
            \bhref
            \s*=\s*
            ["'](.*?)["']
            """,
            attrs,
            re.I | re.S | re.X,
        )

        if not href_match:
            continue

        href = html.unescape(
            href_match.group(1)
        ).strip()

        if not href:
            continue

        absolute = urljoin(
            base_url,
            href,
        )

        absolute = clean_url(
            absolute
        )

        if not absolute.startswith(
            ("http://", "https://")
        ):
            continue

        if not is_facebook_url(
            absolute
        ):
            continue

        if absolute in seen:
            continue

        seen.add(
            absolute
        )

        results.append(
            absolute
        )

        if len(results) >= MAX_RELATED_LINKS:
            break

    return results


# ====================================================================
# CANONICAL
# ====================================================================

def extract_canonical(
    text: str,
    base_url: str,
) -> Optional[str]:

    pattern = re.compile(
        r"<link\b([^>]+)>",
        re.I | re.S,
    )

    for match in pattern.finditer(text):

        attrs = match.group(1)

        rel = re.search(
            r"""
            \brel
            \s*=\s*
            ["'](.*?)["']
            """,
            attrs,
            re.I | re.S | re.X,
        )

        href = re.search(
            r"""
            \bhref
            \s*=\s*
            ["'](.*?)["']
            """,
            attrs,
            re.I | re.S | re.X,
        )

        if not (
            rel
            and
            href
        ):
            continue

        rel_value = rel.group(1).lower()

        if "canonical" not in rel_value:
            continue

        return clean_url(
            urljoin(
                base_url,
                html.unescape(
                    href.group(1)
                ),
            )
        )

    return None


# ====================================================================
# TITLE
# ====================================================================

def extract_title(
    text: str,
) -> Optional[str]:

    match = re.search(
        r"<title[^>]*>(.*?)</title>",
        text,
        re.I | re.S,
    )

    if not match:
        return None

    value = re.sub(
        r"\s+",
        " ",
        html.unescape(
            match.group(1)
        ),
    ).strip()

    return truncate(
        value,
        350,
    )


# ====================================================================
# JSON-LD EXTRACTION
# ====================================================================

def extract_jsonld(
    text: str,
) -> list[Any]:

    result = []

    pattern = re.compile(
        r"""
        <script
        [^>]*?
        type\s*=\s*
        ["']application/ld\+json["']
        [^>]*>
        (.*?)
        </script>
        """,
        re.I | re.S | re.X,
    )

    for match in pattern.finditer(text):

        raw = html.unescape(
            match.group(1)
        ).strip()

        if not raw:
            continue

        try:

            data = json.loads(
                raw
            )

            result.append(
                data
            )

        except Exception:
            continue

    return result


# ====================================================================
# TEXT NORMALIZATION
# ====================================================================

def normalize_text(
    value: str,
) -> str:

    value = html.unescape(
        value or ""
    )

    value = value.replace(
        "\\/",
        "/",
    )

    return value


# ====================================================================
# EVIDENCE STORE
# ====================================================================

class EvidenceStore:

    def __init__(self):

        self.items: list[Evidence] = []

        self.opaque_ids: set[str] = set()

    def add(
        self,
        value: Optional[str],
        role: str,
        source: str,
        score: float,
        context: str = "",
        independent: bool = True,
        url: str = "",
        relation: str = "",
    ) -> None:

        if not value:
            return

        value = str(
            value
        ).strip()

        if not value:
            return

        if len(value) > 500:
            return

        self.items.append(
            Evidence(
                value=value,
                role=role,
                source=source,
                score=score,
                context=truncate(
                    context,
                    MAX_EVIDENCE_CONTEXT,
                ),
                independent=independent,
                url=url,
                relation=relation,
            )
        )

    def add_opaque(
        self,
        value: str,
    ) -> None:

        if value:
            self.opaque_ids.add(
                value
            )

    def candidates(
        self,
        role: str,
    ) -> list[Candidate]:

        grouped: dict[str, Candidate] = {}

        for evidence in self.items:

            if evidence.role != role:
                continue

            key = evidence.value

            if key not in grouped:

                grouped[key] = Candidate(
                    value=key,
                    role=role,
                )

            grouped[key].evidence.append(
                evidence
            )

        return sorted(
            grouped.values(),
            key=lambda x: (
                x.score,
                x.independent_sources,
            ),
            reverse=True,
        )

    def candidates_for(
        self,
        *roles: str,
    ) -> list[Candidate]:

        role_set = set(
            roles
        )

        grouped: dict[
            tuple[str, str],
            Candidate,
        ] = {}

        for evidence in self.items:

            if evidence.role not in role_set:
                continue

            key = (
                evidence.value,
                evidence.role,
            )

            if key not in grouped:

                grouped[key] = Candidate(
                    value=evidence.value,
                    role=evidence.role,
                )

            grouped[key].evidence.append(
                evidence
            )

        return sorted(
            grouped.values(),
            key=lambda x: (
                x.score,
                x.independent_sources,
            ),
            reverse=True,
        )

    @property
    def count(self) -> int:

        return len(self.items)


# ====================================================================
# HTTP CLIENT
# ====================================================================

class PublicHTTP:

    def __init__(
        self,
    ):

        self.semaphore = asyncio.Semaphore(
            HTTP_CONCURRENCY
        )

        self.client: Optional[
            httpx.AsyncClient
        ] = None

    async def __aenter__(
        self,
    ):

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

        self.client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(
                HTTP_TIMEOUT
            ),
            follow_redirects=True,
            http2=True,
        )

        return self

    async def __aexit__(
        self,
        exc_type,
        exc,
        tb,
    ):

        if self.client:

            await self.client.aclose()

    async def get(
        self,
        url: str,
    ) -> Snapshot:

        if not self.client:

            raise RuntimeError(
                "HTTP client not initialized"
            )

        async with self.semaphore:

            last_error = None

            for attempt in range(
                HTTP_RETRIES + 1
            ):

                started = time.monotonic()

                try:

                    response = await self.client.get(
                        url
                    )

                    elapsed = (
                        time.monotonic()
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
                        errors="replace",
                    )

                    text = text[
                        :MAX_HTML_CHARS
                    ]

                    return Snapshot(
                        requested_url=url,
                        final_url=str(
                            response.url
                        ),
                        status_code=response.status_code,
                        content_type=content_type,
                        text=text,
                        headers=dict(
                            response.headers
                        ),
                        elapsed=elapsed,
                    )

                except Exception as exc:

                    last_error = str(
                        exc
                    )

                    if attempt < HTTP_RETRIES:

                        await asyncio.sleep(
                            0.6 * (
                                attempt + 1
                            )
                        )

            return Snapshot(
                requested_url=url,
                error=last_error
                or "HTTP request failed",
            )


# ====================================================================
# EXTRACTOR
# ====================================================================

class FacebookExtractor:

    ID_PATTERNS = [

        (
            "user_id",
            "user",
            97,
        ),

        (
            "userID",
            "user",
            97,
        ),

        (
            "profile_id",
            "profile",
            97,
        ),

        (
            "profileID",
            "profile",
            97,
        ),

        (
            "owner_id",
            "owner",
            91,
        ),

        (
            "ownerID",
            "owner",
            91,
        ),

        (
            "publisher_id",
            "publisher",
            94,
        ),

        (
            "publisherID",
            "publisher",
            94,
        ),

        (
            "page_id",
            "page",
            94,
        ),

        (
            "pageID",
            "page",
            94,
        ),

        (
            "group_id",
            "group",
            94,
        ),

        (
            "groupID",
            "group",
            94,
        ),

        (
            "actor_id",
            "actor",
            58,
        ),

        (
            "actorID",
            "actor",
            58,
        ),

        (
            "entity_id",
            "entity",
            78,
        ),

        (
            "entityID",
            "entity",
            78,
        ),

        (
            "media_fbid",
            "media",
            87,
        ),

        (
            "mediaFBID",
            "media",
            87,
        ),

        (
            "post_id",
            "post",
            86,
        ),

        (
            "postID",
            "post",
            86,
        ),

        (
            "story_fbid",
            "story",
            84,
        ),

        (
            "storyFBID",
            "story",
            84,
        ),

        (
            "video_id",
            "video",
            82,
        ),

        (
            "videoID",
            "video",
            82,
        ),
    ]

    def __init__(
        self,
        store: EvidenceStore,
    ):

        self.store = store

    # ==================================================================
    # MAIN
    # ==================================================================

    def extract(
        self,
        snapshot: Snapshot,
        parsed: ParsedURL,
    ) -> None:

        text = normalize_text(
            snapshot.text
        )

        if not text:
            return

        url = (
            snapshot.final_url
            or snapshot.requested_url
        )

        self.extract_url(
            parsed,
            url,
        )

        self.extract_meta_data(
            text,
            url,
        )

        self.extract_jsonld_data(
            text,
            url,
        )

        self.extract_explicit_ids(
            text,
            url,
        )

        self.extract_opaque_correlations(
            text,
            url,
        )

        self.extract_profile_links(
            text,
            url,
        )

    # ==================================================================
    # URL
    # ==================================================================

    def extract_url(
        self,
        parsed: ParsedURL,
        url: str,
    ) -> None:

        if parsed.profile_id:

            self.store.add(
                parsed.profile_id,
                "profile",
                "url:profile_id",
                100,
                url,
                True,
                url,
            )

        if parsed.page_id:

            self.store.add(
                parsed.page_id,
                "page",
                "url:page_id",
                100,
                url,
                True,
                url,
            )

        if parsed.group_id:

            self.store.add(
                parsed.group_id,
                "group",
                "url:group_id",
                100,
                url,
                True,
                url,
            )

        if parsed.object_id:

            role = "object"

            if parsed.url_type in {
                "USER_POST",
                "PAGE_POST",
                "GROUP_POST",
                "POST",
                "SHARE_POST",
            }:
                role = "post"

            elif parsed.url_type in {
                "VIDEO",
                "WATCH",
            }:
                role = "video"

            elif parsed.url_type == "REEL":
                role = "video"

            elif parsed.url_type == "PHOTO":
                role = "photo"

            elif parsed.url_type == "STORY":
                role = "story"

            self.store.add(
                parsed.object_id,
                role,
                "url:object",
                90,
                url,
                True,
                url,
            )

        if parsed.opaque_id:

            self.store.add_opaque(
                parsed.opaque_id
            )

    # ==================================================================
    # META
    # ==================================================================

    def extract_meta_data(
        self,
        text: str,
        url: str,
    ) -> None:

        meta = extract_meta(
            text
        )

        identity_keys = {
            "user_id": (
                "user",
                97,
            ),
            "profile_id": (
                "profile",
                97,
            ),
            "owner_id": (
                "owner",
                91,
            ),
            "publisher_id": (
                "publisher",
                94,
            ),
            "page_id": (
                "page",
                94,
            ),
            "group_id": (
                "group",
                94,
            ),
            "actor_id": (
                "actor",
                58,
            ),
            "entity_id": (
                "entity",
                78,
            ),
            "media_fbid": (
                "media",
                87,
            ),
            "post_id": (
                "post",
                86,
            ),
            "video_id": (
                "video",
                82,
            ),
        }

        for key, values in meta.items():

            if key in identity_keys:

                role, score = identity_keys[
                    key
                ]

                for value in values:

                    if is_numeric_id(value):

                        self.store.add(
                            value,
                            role,
                            "meta:" + key,
                            score,
                            value,
                            True,
                            url,
                        )

        # --------------------------------------------------------------
        # OG URL
        # --------------------------------------------------------------

        for key in (
            "og:url",
            "al:web:url",
            "al:ios:url",
            "al:android:url",
        ):

            for value in meta.get(
                key,
                [],
            ):

                if not value.startswith(
                    "http"
                ):
                    continue

                parsed = parse_url(
                    value
                )

                self.extract_url(
                    parsed,
                    value,
                )

        # --------------------------------------------------------------
        # ARTICLE AUTHOR
        # --------------------------------------------------------------

        for key in (
            "article:author",
            "author",
            "profile",
        ):

            for value in meta.get(
                key,
                [],
            ):

                parsed = parse_url(
                    value
                )

                if parsed.username:

                    self.store.add(
                        parsed.username,
                        "username",
                        "meta:author_url",
                        82,
                        value,
                        True,
                        url,
                    )

    # ==================================================================
    # JSON-LD
    # ==================================================================

    def extract_jsonld_data(
        self,
        text: str,
        url: str,
    ) -> None:

        blocks = extract_jsonld(
            text
        )

        for block in blocks:

            self.walk_jsonld(
                block,
                url,
            )

    def walk_jsonld(
        self,
        data: Any,
        url: str,
    ) -> None:

        if isinstance(
            data,
            list,
        ):

            for item in data:

                self.walk_jsonld(
                    item,
                    url,
                )

            return

        if not isinstance(
            data,
            dict,
        ):
            return

        # --------------------------------------------------------------
        # DIRECT IDENTIFIERS
        # --------------------------------------------------------------

        for key in (
            "identifier",
            "id",
            "@id",
            "user_id",
            "profile_id",
            "page_id",
            "group_id",
        ):

            value = data.get(
                key
            )

            if isinstance(
                value,
                str,
            ) and is_numeric_id(value):

                role = "entity"

                if key in {
                    "user_id",
                    "profile_id",
                }:
                    role = "user"

                elif key == "page_id":
                    role = "page"

                elif key == "group_id":
                    role = "group"

                self.store.add(
                    value,
                    role,
                    "jsonld:" + key,
                    88,
                    str(data),
                    True,
                    url,
                )

        # --------------------------------------------------------------
        # AUTHOR
        # --------------------------------------------------------------

        for key in (
            "author",
            "creator",
            "publisher",
        ):

            value = data.get(
                key
            )

            self.extract_jsonld_person(
                value,
                key,
                url,
            )

        # --------------------------------------------------------------
        # MAIN ENTITY
        # --------------------------------------------------------------

        for key in (
            "mainEntity",
            "mainEntityOfPage",
            "about",
            "subjectOf",
        ):

            value = data.get(
                key
            )

            self.extract_jsonld_url(
                value,
                url,
            )

        # --------------------------------------------------------------
        # RECURSE
        # --------------------------------------------------------------

        for value in data.values():

            if isinstance(
                value,
                (dict, list),
            ):

                self.walk_jsonld(
                    value,
                    url,
                )

    def extract_jsonld_person(
        self,
        value: Any,
        source_name: str,
        url: str,
    ) -> None:

        if isinstance(
            value,
            list,
        ):

            for item in value:

                self.extract_jsonld_person(
                    item,
                    source_name,
                    url,
                )

            return

        if not isinstance(
            value,
            dict,
        ):
            return

        identifier = value.get(
            "identifier"
        )

        if (
            isinstance(
                identifier,
                str,
            )
            and
            is_numeric_id(identifier)
        ):

            self.store.add(
                identifier,
                "user",
                "jsonld:" + source_name,
                91,
                str(value),
                True,
                url,
            )

        person_id = value.get(
            "id"
        )

        if (
            isinstance(
                person_id,
                str,
            )
            and
            is_numeric_id(person_id)
        ):

            self.store.add(
                person_id,
                "user",
                "jsonld:" + source_name,
                87,
                str(value),
                True,
                url,
            )

        person_url = value.get(
            "url"
        )

        if isinstance(
            person_url,
            str,
        ):

            parsed = parse_url(
                person_url
            )

            if parsed.username:

                self.store.add(
                    parsed.username,
                    "username",
                    "jsonld:" + source_name,
                    86,
                    person_url,
                    True,
                    url,
                )

            self.extract_url(
                parsed,
                person_url,
            )

    def extract_jsonld_url(
        self,
        value: Any,
        url: str,
    ) -> None:

        if isinstance(
            value,
            str,
        ):

            if value.startswith(
                "http"
            ):

                parsed = parse_url(
                    value
                )

                self.extract_url(
                    parsed,
                    value,
                )

            return

        if isinstance(
            value,
            dict,
        ):

            self.extract_jsonld_url(
                value.get("url"),
                url,
            )

    # ==================================================================
    # EXPLICIT ID EXTRACTION
    # ==================================================================

    def extract_explicit_ids(
        self,
        text: str,
        url: str,
    ) -> None:

        for key, role, score in self.ID_PATTERNS:

            patterns = [
                rf'["\']{re.escape(key)}["\']\s*:\s*["\'](\d{{{MIN_NUMERIC_ID_LENGTH},{MAX_NUMERIC_ID_LENGTH}}})["\']',
                rf'\b{re.escape(key)}\s*[:=]\s*["\']?(\d{{{MIN_NUMERIC_ID_LENGTH},{MAX_NUMERIC_ID_LENGTH}}})',
            ]

            for pattern in patterns:

                for match in re.finditer(
                    pattern,
                    text,
                    re.I,
                ):

                    value = match.group(1)

                    self.store.add(
                        value,
                        role,
                        "embedded:" + key,
                        score,
                        self.local_context(
                            text,
                            match.start(),
                        ),
                        True,
                        url,
                    )

    # ==================================================================
    # OPAQUE CORRELATION
    # ==================================================================

    def extract_opaque_correlations(
        self,
        text: str,
        url: str,
    ) -> None:

        opaque_ids = list(
            dict.fromkeys(
                PF_BID_RE.findall(
                    text
                )
                +
                AQ_ID_RE.findall(
                    text
                )
            )
        )

        for opaque in opaque_ids:

            self.store.add_opaque(
                opaque
            )

            for match in re.finditer(
                re.escape(opaque),
                text,
                re.I,
            ):

                start = max(
                    0,
                    match.start() - 9000,
                )

                end = min(
                    len(text),
                    match.end() + 9000,
                )

                window = text[
                    start:end
                ]

                self.correlate_opaque_window(
                    opaque,
                    window,
                    url,
                )

    def correlate_opaque_window(
        self,
        opaque: str,
        window: str,
        url: str,
    ) -> None:

        patterns = [

            (
                r"""
                ["']?
                user[_-]?id
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "user",
                96,
                "opaque:user_id",
            ),

            (
                r"""
                ["']?
                profile[_-]?id
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "profile",
                96,
                "opaque:profile_id",
            ),

            (
                r"""
                ["']?
                owner[_-]?id
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "owner",
                90,
                "opaque:owner_id",
            ),

            (
                r"""
                ["']?
                publisher[_-]?id
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "publisher",
                93,
                "opaque:publisher_id",
            ),

            (
                r"""
                ["']?
                actor[_-]?id
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "actor",
                58,
                "opaque:actor_id",
            ),

            (
                r"""
                ["']?
                page[_-]?id
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "page",
                94,
                "opaque:page_id",
            ),

            (
                r"""
                ["']?
                group[_-]?id
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "group",
                94,
                "opaque:group_id",
            ),

            (
                r"""
                ["']?
                media[_-]?fbid
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "media",
                88,
                "opaque:media_fbid",
            ),

            (
                r"""
                ["']?
                entity[_-]?id
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "entity",
                78,
                "opaque:entity_id",
            ),

            (
                r"""
                ["']?
                post[_-]?id
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "post",
                86,
                "opaque:post_id",
            ),

            (
                r"""
                ["']?
                video[_-]?id
                ["']?
                \s*[:=]\s*
                ["']?
                (\d{5,25})
                """,
                "video",
                82,
                "opaque:video_id",
            ),
        ]

        for (
            pattern,
            role,
            score,
            source,
        ) in patterns:

            for match in re.finditer(
                pattern,
                window,
                re.I | re.X,
            ):

                value = match.group(1)

                self.store.add(
                    value,
                    role,
                    source,
                    score,
                    (
                        opaque
                        + "\n"
                        + self.local_context(
                            window,
                            match.start(),
                        )
                    ),
                    True,
                    url,
                    relation=(
                        opaque
                        + "->"
                        + role
                    ),
                )

        # --------------------------------------------------------------
        # SAME OBJECT JSON
        # --------------------------------------------------------------

        json_patterns = [

            r"""
            \{
            [^{}]{0,3500}?
            ["']id["']
            \s*:\s*
            ["'](\d{5,25})["']
            [^{}]{0,3500}?
            \}
            """,

            r"""
            \{
            [^{}]{0,3500}?
            ["'](?:pk|fbid|object_id)["']
            \s*:\s*
            ["'](\d{5,25})["']
            [^{}]{0,3500}?
            \}
            """,
        ]

        for pattern in json_patterns:

            for match in re.finditer(
                pattern,
                window,
                re.I | re.X,
            ):

                value = match.group(1)

                self.store.add(
                    value,
                    "generic",
                    "opaque:object",
                    30,
                    match.group(0),
                    False,
                    url,
                    relation=(
                        opaque
                        + "->generic"
                    ),
                )

    # ==================================================================
    # PROFILE LINKS
    # ==================================================================

    def extract_profile_links(
        self,
        text: str,
        url: str,
    ) -> None:

        for link in extract_links(
            text,
            url,
        ):

            parsed = parse_url(
                link
            )

            if parsed.username:

                self.store.add(
                    parsed.username,
                    "username",
                    "public:profile_link",
                    84,
                    link,
                    True,
                    url,
                )

            if parsed.profile_id:

                self.store.add(
                    parsed.profile_id,
                    "profile",
                    "public:profile_link",
                    94,
                    link,
                    True,
                    url,
                )

    # ==================================================================
    # CONTEXT
    # ==================================================================

    @staticmethod
    def local_context(
        text: str,
        position: int,
        radius: int = 1200,
    ) -> str:

        start = max(
            0,
            position - radius,
        )

        end = min(
            len(text),
            position + radius,
        )

        return text[
            start:end
        ]


# ====================================================================
# USERNAME EXTRACTION FROM HTML
# ====================================================================

def extract_public_usernames(
    text: str,
    base_url: str,
) -> list[str]:

    usernames = []

    seen = set()

    for link in extract_links(
        text,
        base_url,
    ):

        parsed = parse_url(
            link
        )

        if not parsed.username:
            continue

        username = parsed.username

        if username.lower() in FB_RESERVED:
            continue

        if username.lower() in seen:
            continue

        seen.add(
            username.lower()
        )

        usernames.append(
            username
        )

    return usernames


# ====================================================================
# PROFILE TARGET BUILDER
# ====================================================================

def build_profile_targets(
    parsed: ParsedURL,
    store: EvidenceStore,
) -> list[str]:

    targets = []

    seen = set()

    def add(
        url: str,
    ):

        url = clean_url(
            url
        )

        if not is_facebook_url(
            url
        ):
            return

        if url in seen:
            return

        seen.add(
            url
        )

        targets.append(
            url
        )

    # --------------------------------------------------------------
    # URL USERNAME
    # --------------------------------------------------------------

    if parsed.username:

        username = quote(
            parsed.username,
            safe="._-",
        )

        add(
            "https://www.facebook.com/"
            + username
        )

        add(
            "https://m.facebook.com/"
            + username
        )

    # --------------------------------------------------------------
    # USER CANDIDATES
    # --------------------------------------------------------------

    for candidate in (
        store.candidates_for(
            "user",
            "profile",
            "owner",
            "publisher",
        )[:8]
    ):

        value = candidate.value

        if not is_numeric_id(
            value
        ):
            continue

        add(
            "https://www.facebook.com/"
            "profile.php?id="
            + value
        )

    return targets[
        :MAX_PROFILE_PAGES
    ]


# ====================================================================
# PROFILE VERIFICATION
# ====================================================================

async def verify_user_candidate(
    http: PublicHTTP,
    uid: str,
    username: Optional[str],
) -> tuple[bool, Optional[str]]:

    if not is_numeric_id(
        uid
    ):
        return False, None

    urls = [
        (
            "https://www.facebook.com/"
            "profile.php?id="
            + uid
        ),
        (
            "https://m.facebook.com/"
            "profile.php?id="
            + uid
        ),
    ]

    for url in urls:

        snapshot = await http.get(
            url
        )

        if not snapshot.text:
            continue

        text = snapshot.text

        # ----------------------------------------------------------
        # DIRECT ID
        # ----------------------------------------------------------

        direct_patterns = [
            rf'"user_id"\s*:\s*"{re.escape(uid)}"',
            rf'"userID"\s*:\s*"{re.escape(uid)}"',
            rf'"profile_id"\s*:\s*"{re.escape(uid)}"',
            rf'"profileID"\s*:\s*"{re.escape(uid)}"',
        ]

        if any(
            re.search(
                pattern,
                text,
                re.I,
            )
            for pattern in direct_patterns
        ):

            return True, snapshot.final_url

        # ----------------------------------------------------------
        # CANONICAL USERNAME
        # ----------------------------------------------------------

        canonical = extract_canonical(
            text,
            snapshot.final_url
            or url,
        )

        if canonical:

            cp = parse_url(
                canonical
            )

            if (
                username
                and
                cp.username
                and
                cp.username.lower()
                == username.lower()
            ):

                return True, canonical

        # ----------------------------------------------------------
        # PUBLIC LINKS
        # ----------------------------------------------------------

        if username:

            target = username.lower()

            for link in extract_links(
                text,
                snapshot.final_url
                or url,
            ):

                lp = parse_url(
                    link
                )

                if (
                    lp.username
                    and
                    lp.username.lower()
                    == target
                ):

                    return True, link

    return False, None


# ====================================================================
# UID SELECTION
# ====================================================================

def select_user_candidate(
    store: EvidenceStore,
    parsed: ParsedURL,
) -> Optional[Candidate]:

    candidates = store.candidates_for(
        "user",
        "profile",
        "owner",
        "publisher",
    )

    # --------------------------------------------------------------
    # NEVER USE GROUP ID AS USER UID
    # --------------------------------------------------------------

    group_ids = {
        c.value
        for c in store.candidates(
            "group"
        )
    }

    # --------------------------------------------------------------
    # NEVER USE PAGE ID AS USER UID
    # --------------------------------------------------------------

    page_ids = {
        c.value
        for c in store.candidates(
            "page"
        )
    }

    filtered = []

    for candidate in candidates:

        value = candidate.value

        if not is_numeric_id(
            value
        ):
            continue

        if value in group_ids:
            continue

        if value in page_ids:
            continue

        filtered.append(
            candidate
        )

    if not filtered:
        return None

    # --------------------------------------------------------------
    # URL PROFILE ID IS EXTREMELY STRONG
    # --------------------------------------------------------------

    if parsed.profile_id:

        for candidate in filtered:

            if (
                candidate.value
                == parsed.profile_id
            ):

                return candidate

    # --------------------------------------------------------------
    # SCORE
    # --------------------------------------------------------------

    filtered.sort(
        key=lambda c: (
            c.score,
            c.independent_sources,
        ),
        reverse=True,
    )

    top = filtered[0]

    # --------------------------------------------------------------
    # CONFLICT PROTECTION
    # --------------------------------------------------------------

    if len(filtered) >= 2:

        second = filtered[1]

        if (
            top.value
            != second.value
            and
            top.score < 85
            and
            second.score >= 80
        ):
            return None

    return top


# ====================================================================
# CONFIDENCE
# ====================================================================

def calculate_confidence(
    candidate: Optional[Candidate],
    verified: bool,
    parsed: ParsedURL,
) -> float:

    if not candidate:

        return 0.0

    score = candidate.score

    if verified:

        score += 8

    if parsed.profile_id:

        score += 7

    if parsed.username:

        if any(
            e.role in {
                "user",
                "profile",
                "owner",
                "publisher",
            }
            for e in candidate.evidence
        ):
            score += 3

    return min(
        100.0,
        score,
    )


# ====================================================================
# ENTITY INFERENCE
# ====================================================================

def infer_entity_type(
    parsed: ParsedURL,
    store: EvidenceStore,
) -> str:

    # --------------------------------------------------------------
    # GROUP HAS ABSOLUTE PRIORITY
    # --------------------------------------------------------------

    if parsed.url_type == "GROUP":
        return "GROUP"

    if parsed.url_type == "GROUP_POST":
        return "GROUP"

    if parsed.group_id:
        return "GROUP"

    # --------------------------------------------------------------
    # PAGE
    # --------------------------------------------------------------

    if parsed.url_type == "PAGE":
        return "PAGE"

    if parsed.url_type == "PAGE_POST":
        return "PAGE"

    if parsed.page_id:
        return "PAGE"

    # --------------------------------------------------------------
    # USER
    # --------------------------------------------------------------

    if parsed.url_type in {
        "PROFILE",
        "USER_POST",
    }:

        return "USER"

    # --------------------------------------------------------------
    # STRONG PAGE EVIDENCE
    # --------------------------------------------------------------

    if store.candidates(
        "page"
    ):

        if parsed.username is None:

            return "PAGE"

    return parsed.entity_type or "UNKNOWN"


# ====================================================================
# OBJECT ID COLLECTION
# ====================================================================

def collect_objects(
    store: EvidenceStore,
    parsed: ParsedURL,
) -> dict[str, Optional[str]]:

    result = {
        "post_id": None,
        "video_id": None,
        "photo_id": None,
        "story_id": None,
        "media_fbid": None,
        "entity_id": None,
    }

    if (
        parsed.object_id
        and
        is_numeric_id(
            parsed.object_id
        )
    ):

        if parsed.url_type in {
            "USER_POST",
            "PAGE_POST",
            "GROUP_POST",
            "POST",
            "SHARE_POST",
        }:

            result["post_id"] = (
                parsed.object_id
            )

        elif parsed.url_type in {
            "VIDEO",
            "WATCH",
            "SHARE_VIDEO",
        }:

            result["video_id"] = (
                parsed.object_id
            )

        elif parsed.url_type == "REEL":

            result["video_id"] = (
                parsed.object_id
            )

        elif parsed.url_type == "PHOTO":

            result["photo_id"] = (
                parsed.object_id
            )

        elif parsed.url_type == "STORY":

            result["story_id"] = (
                parsed.object_id
            )

    # --------------------------------------------------------------
    # STORE CANDIDATES
    # --------------------------------------------------------------

    for role, key in (
        ("post", "post_id"),
        ("video", "video_id"),
        ("photo", "photo_id"),
        ("story", "story_id"),
        ("media", "media_fbid"),
        ("entity", "entity_id"),
    ):

        candidates = store.candidates(
            role
        )

        if candidates:

            result[key] = (
                candidates[0].value
            )

    return result


# ====================================================================
# PUBLISHER NAME
# ====================================================================

def infer_publisher_name(
    store: EvidenceStore,
    parsed: ParsedURL,
) -> Optional[str]:

    usernames = store.candidates(
        "username"
    )

    if parsed.username:

        return parsed.username

    if usernames:

        return usernames[0].value

    return None


# ====================================================================
# MAIN RESOLVER
# ====================================================================

class FacebookResolver:

    async def resolve(
        self,
        url: str,
    ) -> ResolveResult:

        original = clean_url(
            url
        )

        parsed = parse_url(
            original
        )

        if not is_facebook_url(
            parsed.normalized
        ) and not parsed.host.endswith(
            "fb.watch"
        ):

            return ResolveResult(
                original_url=original,
                url_type="UNKNOWN",
                error="Không phải Facebook URL",
            )

        store = EvidenceStore()

        extractor = FacebookExtractor(
            store
        )

        visited: set[str] = set()

        snapshots: list[Snapshot] = []

        async with PublicHTTP() as http:

            # ========================================================
            # ROUND 1
            # ========================================================

            first_urls = [
                parsed.normalized
            ]

            if parsed.normalized != original:

                first_urls.append(
                    original
                )

            for target in first_urls:

                if target in visited:
                    continue

                visited.add(
                    target
                )

                snapshot = await http.get(
                    target
                )

                snapshots.append(
                    snapshot
                )

                if snapshot.text:

                    final_parsed = parse_url(
                        snapshot.final_url
                        or target
                    )

                    parsed = self.merge_parsed(
                        parsed,
                        final_parsed,
                    )

                    extractor.extract(
                        snapshot,
                        parsed,
                    )

            # ========================================================
            # ROUND 2
            # CANONICAL / OG / REDIRECT
            # ========================================================

            targets = []

            for snapshot in snapshots:

                base = (
                    snapshot.final_url
                    or snapshot.requested_url
                )

                canonical = extract_canonical(
                    snapshot.text,
                    base,
                )

                if canonical:

                    targets.append(
                        canonical
                    )

                meta = extract_meta(
                    snapshot.text
                )

                for key in (
                    "og:url",
                    "al:web:url",
                    "al:ios:url",
                    "al:android:url",
                ):

                    targets.extend(
                        meta.get(
                            key,
                            [],
                        )
                    )

            targets = self.unique_urls(
                targets
            )

            targets = targets[
                :MAX_CRAWL_PAGES
            ]

            if targets:

                extra = await asyncio.gather(
                    *[
                        http.get(
                            target
                        )
                        for target in targets
                        if target not in visited
                    ],
                    return_exceptions=True,
                )

                for item in extra:

                    if isinstance(
                        item,
                        Exception,
                    ):
                        continue

                    if not isinstance(
                        item,
                        Snapshot,
                    ):
                        continue

                    target = (
                        item.final_url
                        or item.requested_url
                    )

                    if target in visited:
                        continue

                    visited.add(
                        target
                    )

                    snapshots.append(
                        item
                    )

                    p = parse_url(
                        target
                    )

                    parsed = self.merge_parsed(
                        parsed,
                        p,
                    )

                    extractor.extract(
                        item,
                        p,
                    )

            # ========================================================
            # ROUND 3
            # PROFILE RECOVERY
            # ========================================================

            profile_targets = (
                build_profile_targets(
                    parsed,
                    store,
                )
            )

            profile_targets = [
                x
                for x in profile_targets
                if x not in visited
            ][
                :MAX_PROFILE_PAGES
            ]

            if profile_targets:

                profile_results = await asyncio.gather(
                    *[
                        http.get(
                            target
                        )
                        for target in profile_targets
                    ],
                    return_exceptions=True,
                )

                for item in profile_results:

                    if isinstance(
                        item,
                        Exception,
                    ):
                        continue

                    if not isinstance(
                        item,
                        Snapshot,
                    ):
                        continue

                    target = (
                        item.final_url
                        or item.requested_url
                    )

                    if target in visited:
                        continue

                    visited.add(
                        target
                    )

                    snapshots.append(
                        item
                    )

                    p = parse_url(
                        target
                    )

                    extractor.extract(
                        item,
                        p,
                    )

            # ========================================================
            # UID SELECTION
            # ========================================================

            user_candidate = (
                select_user_candidate(
                    store,
                    parsed,
                )
            )

            verified = False
            verified_url = None

            if user_candidate:

                verified, verified_url = (
                    await verify_user_candidate(
                        http,
                        user_candidate.value,
                        parsed.username,
                    )
                )

            # ========================================================
            # ENTITY
            # ========================================================

            entity = infer_entity_type(
                parsed,
                store,
            )

            # ========================================================
            # PAGE ID
            # ========================================================

            page_candidate = None

            page_candidates = store.candidates(
                "page"
            )

            if page_candidates:

                page_candidate = (
                    page_candidates[0]
                )

            # ========================================================
            # GROUP ID
            # ========================================================

            group_candidate = None

            group_candidates = store.candidates(
                "group"
            )

            if group_candidates:

                group_candidate = (
                    group_candidates[0]
                )

            # ========================================================
            # OBJECTS
            # ========================================================

            objects = collect_objects(
                store,
                parsed,
            )

            # ========================================================
            # TITLE
            # ========================================================

            title = None

            for snapshot in snapshots:

                candidate_title = extract_title(
                    snapshot.text
                )

                if candidate_title:

                    title = candidate_title

                    break

            # ========================================================
            # USER UID POLICY
            # ========================================================

            user_uid = None

            if entity == "USER":

                if (
                    user_candidate
                    and
                    verified
                ):

                    user_uid = (
                        user_candidate.value
                    )

            elif entity == "GROUP":

                # NEVER group ID => user UID
                user_uid = None

            elif entity == "PAGE":

                # NEVER page ID => user UID
                user_uid = None

            else:

                if (
                    user_candidate
                    and
                    verified
                ):

                    user_uid = (
                        user_candidate.value
                    )

            # ========================================================
            # PUBLISHER
            # ========================================================

            publisher = infer_publisher_name(
                store,
                parsed,
            )

            publisher_id = None

            if user_uid:

                publisher_id = user_uid

            elif entity == "PAGE":

                if page_candidate:

                    publisher_id = (
                        page_candidate.value
                    )

            # ========================================================
            # CONFIDENCE
            # ========================================================

            confidence = calculate_confidence(
                user_candidate,
                verified,
                parsed,
            )

            if entity == "GROUP":

                if group_candidate:

                    confidence = max(
                        confidence,
                        min(
                            100,
                            group_candidate.score,
                        ),
                    )

            if entity == "PAGE":

                if page_candidate:

                    confidence = max(
                        confidence,
                        min(
                            100,
                            page_candidate.score,
                        ),
                    )

            # ========================================================
            # VERIFIED STATUS
            # ========================================================

            final_verified = False

            if entity == "USER":

                final_verified = bool(
                    user_uid
                    and
                    verified
                )

            elif entity == "PAGE":

                final_verified = bool(
                    page_candidate
                    and
                    page_candidate.score >= 90
                )

            elif entity == "GROUP":

                final_verified = bool(
                    group_candidate
                    and
                    group_candidate.score >= 90
                )

            elif user_uid:

                final_verified = verified

            return ResolveResult(
                original_url=original,
                resolved_url=(
                    verified_url
                    or
                    (
                        snapshots[0].final_url
                        if snapshots
                        else parsed.normalized
                    )
                ),
                url_type=self.final_url_type(
                    parsed,
                    entity,
                    store,
                ),
                entity_type=entity,
                confidence=round(
                    confidence,
                    1,
                ),
                verified=final_verified,
                username=parsed.username,
                user_uid=user_uid,
                page_id=(
                    page_candidate.value
                    if page_candidate
                    else parsed.page_id
                ),
                group_id=(
                    group_candidate.value
                    if group_candidate
                    else parsed.group_id
                ),
                post_id=objects[
                    "post_id"
                ],
                video_id=objects[
                    "video_id"
                ],
                photo_id=objects[
                    "photo_id"
                ],
                story_id=objects[
                    "story_id"
                ],
                media_fbid=objects[
                    "media_fbid"
                ],
                entity_id=objects[
                    "entity_id"
                ],
                publisher=publisher,
                publisher_id=publisher_id,
                title=title,
                evidence_count=store.count,
            )

    # ==================================================================
    # TYPE REFINEMENT
    # ==================================================================

    @staticmethod
    def final_url_type(
        parsed: ParsedURL,
        entity: str,
        store: EvidenceStore,
    ) -> str:

        current = parsed.url_type

        if current == "UNKNOWN":

            if store.candidates(
                "post"
            ):
                current = "POST"

        # --------------------------------------------------------------
        # STRICT POST TYPES
        # --------------------------------------------------------------

        if current in {
            "POST",
            "SHARE_POST",
        }:

            if entity == "GROUP":
                return "GROUP_POST"

            if entity == "PAGE":
                return "PAGE_POST"

            if entity == "USER":
                return "USER_POST"

        if current == "USER_POST":
            return "USER_POST"

        if current == "PAGE_POST":
            return "PAGE_POST"

        if current == "GROUP_POST":
            return "GROUP_POST"

        return current

    # ==================================================================
    # MERGE PARSED URL
    # ==================================================================

    @staticmethod
    def merge_parsed(
        a: ParsedURL,
        b: ParsedURL,
    ) -> ParsedURL:

        # Prefer the more specific classification
        type_priority = {
            "UNKNOWN": 0,
            "PROFILE": 20,
            "PAGE": 20,
            "GROUP": 20,
            "POST": 30,
            "USER_POST": 50,
            "PAGE_POST": 50,
            "GROUP_POST": 50,
            "PHOTO": 45,
            "STORY": 45,
            "VIDEO": 45,
            "WATCH": 45,
            "REEL": 45,
            "SHARE_POST": 40,
            "SHARE_VIDEO": 40,
            "SHARE_REEL": 40,
        }

        if (
            type_priority.get(
                b.url_type,
                0,
            )
            >
            type_priority.get(
                a.url_type,
                0,
            )
        ):

            a.url_type = b.url_type

        if b.entity_type != "UNKNOWN":

            if a.entity_type == "UNKNOWN":

                a.entity_type = (
                    b.entity_type
                )

        for attr in (
            "username",
            "object_id",
            "opaque_id",
            "profile_id",
            "page_id",
            "group_id",
            "share_type",
        ):

            value = getattr(
                b,
                attr,
                None,
            )

            if value:

                setattr(
                    a,
                    attr,
                    value,
                )

        return a

    # ==================================================================
    # UNIQUE URLS
    # ==================================================================

    @staticmethod
    def unique_urls(
        urls: Iterable[str],
    ) -> list[str]:

        result = []

        seen = set()

        for url in urls:

            if not url:
                continue

            url = clean_url(
                url
            )

            if not url:
                continue

            if url in seen:
                continue

            seen.add(
                url
            )

            result.append(
                url
            )

        return result


# ====================================================================
# PUBLIC API
# ====================================================================

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

    return await asyncio.gather(
        *[
            resolver.resolve(url)
            for url in urls
        ]
    )


# ====================================================================
# URL EXTRACTION FROM TELEGRAM MESSAGE
# ====================================================================

def extract_facebook_urls(
    text: str,
) -> list[str]:

    if not text:
        return []

    candidates = URL_RE.findall(
        text
    )

    result = []

    seen = set()

    for url in candidates:

        url = clean_url(
            url
        )

        if not url:
            continue

        try:

            parsed = urlparse(
                url
            )

        except Exception:

            continue

        host = normalize_host(
            parsed.netloc
        )

        if not (
            is_facebook_host(host)
            or
            host.endswith("fb.watch")
        ):

            continue

        # ----------------------------------------------------------
        # Remove punctuation accidentally attached to URL
        # ----------------------------------------------------------

        url = url.rstrip(
            ".,;!?)]}>\"'"
        )

        if url in seen:
            continue

        seen.add(
            url
        )

        result.append(
            url
        )

        if len(result) >= MAX_URLS_PER_REQUEST:

            break

    return result


# ====================================================================
# OUTPUT HELPERS
# ====================================================================

TYPE_LABELS = {
    "PROFILE": "👤 USER PROFILE",
    "USER_POST": "👤 USER POST",
    "PAGE": "📄 PAGE",
    "PAGE_POST": "📄 PAGE POST",
    "GROUP": "👥 GROUP",
    "GROUP_POST": "👥 GROUP POST",
    "POST": "📝 POST",
    "REEL": "🎬 REEL",
    "VIDEO": "🎥 VIDEO",
    "PHOTO": "🖼 PHOTO",
    "STORY": "⭕ STORY",
    "WATCH": "🎥 VIDEO",
    "SHARE_POST": "📝 SHARED POST",
    "SHARE_VIDEO": "🎥 SHARED VIDEO",
    "SHARE_REEL": "🎬 SHARED REEL",
    "UNKNOWN": "❓ UNKNOWN",
}


def result_status(
    result: ResolveResult,
) -> str:

    if result.verified:

        return "VERIFIED"

    if (
        result.confidence >= 80
    ):

        return "HIGH CONFIDENCE"

    if (
        result.confidence >= 55
    ):

        return "PARTIAL"

    return "UNRESOLVED"


def format_result(
    result: ResolveResult,
    index: int = 1,
) -> str:

    lines = []

    lines.append(
        "🔎 <b>FACEBOOK UID V19 ULTRA</b>"
    )

    lines.append("")

    lines.append(
        f"<b>{index}. "
        f"{tg_escape(TYPE_LABELS.get(result.url_type, result.url_type))}"
        f"</b>"
    )

    lines.append("")

    lines.append(
        "📊 <b>STATUS:</b> "
        f"{tg_escape(result_status(result))}"
    )

    # ================================================================
    # USER UID
    # ================================================================

    if result.user_uid:

        lines.append(
            "🆔 <b>USER UID:</b> "
            f"<code>{tg_escape(result.user_uid)}</code>"
        )

    # ================================================================
    # PAGE
    # ================================================================

    if result.page_id:

        if result.entity_type == "PAGE":

            lines.append(
                "📄 <b>PAGE UID:</b> "
                f"<code>{tg_escape(result.page_id)}</code>"
            )

    # ================================================================
    # GROUP
    # ================================================================

    if result.group_id:

        lines.append(
            "👥 <b>GROUP ID:</b> "
            f"<code>{tg_escape(result.group_id)}</code>"
        )

    # ================================================================
    # POST
    # ================================================================

    if result.post_id:

        lines.append(
            "📝 <b>POST ID:</b> "
            f"<code>{tg_escape(result.post_id)}</code>"
        )

    # ================================================================
    # VIDEO
    # ================================================================

    if result.video_id:

        lines.append(
            "🎥 <b>VIDEO ID:</b> "
            f"<code>{tg_escape(result.video_id)}</code>"
        )

    # ================================================================
    # PHOTO
    # ================================================================

    if result.photo_id:

        lines.append(
            "🖼 <b>PHOTO ID:</b> "
            f"<code>{tg_escape(result.photo_id)}</code>"
        )

    # ================================================================
    # STORY
    # ================================================================

    if result.story_id:

        lines.append(
            "⭕ <b>STORY ID:</b> "
            f"<code>{tg_escape(result.story_id)}</code>"
        )

    # ================================================================
    # MEDIA FBID
    # ================================================================

    if result.media_fbid:

        lines.append(
            "🎞 <b>MEDIA FBID:</b> "
            f"<code>{tg_escape(result.media_fbid)}</code>"
        )

    # ================================================================
    # ENTITY ID
    # ================================================================

    if result.entity_id:

        lines.append(
            "🔗 <b>ENTITY ID:</b> "
            f"<code>{tg_escape(result.entity_id)}</code>"
        )

    # ================================================================
    # PUBLISHER
    # ================================================================

    if result.publisher:

        lines.append(
            "👤 <b>PUBLISHER:</b> "
            f"{tg_escape(result.publisher)}"
        )

    if (
        result.publisher_id
        and
        result.publisher_id != result.user_uid
    ):

        lines.append(
            "🆔 <b>PUBLISHER ID:</b> "
            f"<code>{tg_escape(result.publisher_id)}</code>"
        )

    # ================================================================
    # USERNAME
    # ================================================================

    if (
        result.username
        and
        result.publisher != result.username
    ):

        lines.append(
            "🔖 <b>USERNAME:</b> "
            f"@{tg_escape(result.username)}"
        )

    # ================================================================
    # TITLE
    # ================================================================

    if result.title:

        lines.append(
            "📝 <b>TITLE:</b> "
            f"{tg_escape(result.title)}"
        )

    # ================================================================
    # CONFIDENCE
    # ================================================================

    lines.append(
        "🎯 <b>CONFIDENCE:</b> "
        f"{result.confidence:.0f}%"
    )

    # ================================================================
    # RESOLVED URL
    # ================================================================

    if result.resolved_url:

        lines.append(
            "🔗 <b>RESOLVED:</b> "
            f'<a href="{html.escape(result.resolved_url, quote=True)}">'
            f"Facebook</a>"
        )

    return "\n".join(
        lines
    )


# ====================================================================
# COMPACT MULTI RESULT OUTPUT
# ====================================================================

def format_all_results(
    results: list[ResolveResult],
) -> str:

    if not results:

        return (
            "❌ <b>Không tìm thấy Facebook URL.</b>"
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


# ====================================================================
# TELEGRAM INTERACTIVE WAIT
# ====================================================================

async def wait_for_url_message(
    client,
    event,
    timeout: int = 120,
) -> Optional[str]:

    loop = asyncio.get_running_loop()

    future = loop.create_future()

    builder = events.NewMessage(
        chats=event.chat_id,
        from_users=event.sender_id,
    )

    async def callback(
        incoming,
    ):

        if future.done():
            return

        raw = (
            incoming.raw_text
            or ""
        ).strip()

        if raw:

            future.set_result(
                raw
            )

    client.add_event_handler(
        callback,
        builder,
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
            callback,
            builder,
        )


# ====================================================================
# TELEGRAM HANDLER
# ====================================================================

async def _handle_getuidfb(
    client,
    event,
) -> None:

    try:

        raw = (
            event.raw_text
            or ""
        ).strip()

        # ------------------------------------------------------------
        # /getuidfb URL
        # ------------------------------------------------------------

        command_match = re.match(
            r"^/getuidfb(?:@\w+)?(?:\s+([\s\S]+))?$",
            raw,
            re.I,
        )

        argument = (
            command_match.group(1).strip()
            if command_match
            and
            command_match.group(1)
            else ""
        )

        if argument:

            urls = extract_facebook_urls(
                argument
            )

        else:

            prompt = await event.respond(
                "🔎 <b>FACEBOOK UID RESOLVER</b>\n\n"
                "Gửi link Facebook cần phân tích.\n"
                "Có thể gửi nhiều link cùng lúc.\n\n"
                "⏳ Timeout: 120 giây.",
                parse_mode="html",
                link_preview=False,
            )

            incoming = (
                await wait_for_url_message(
                    client,
                    event,
                    timeout=120,
                )
            )

            if not incoming:

                await prompt.edit(
                    "⌛ <b>Đã hết thời gian chờ.</b>",
                    parse_mode="html",
                )

                return

            urls = extract_facebook_urls(
                incoming
            )

            if not urls:

                await prompt.edit(
                    "❌ <b>Không tìm thấy Facebook URL.</b>",
                    parse_mode="html",
                )

                return

        if not urls:

            await event.respond(
                "❌ <b>Không tìm thấy Facebook URL hợp lệ.</b>",
                parse_mode="html",
            )

            return

        status_message = await event.respond(
            (
                "🔎 <b>FACEBOOK UID V19 ULTRA</b>\n\n"
                f"📥 Đang phân tích <b>{len(urls)}</b> URL...\n"
                "🧠 URL Intelligence\n"
                "🔗 Object Correlation\n"
                "👤 Publisher Recovery\n"
                "🔐 UID Verification"
            ),
            parse_mode="html",
            link_preview=False,
        )

        started = time.monotonic()

        results = await resolve_facebook_urls(
            urls
        )

        elapsed = (
            time.monotonic()
            - started
        )

        output = format_all_results(
            results
        )

        # ------------------------------------------------------------
        # Footer
        # ------------------------------------------------------------

        output += (
            "\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"⏱ <b>{elapsed:.1f}s</b>"
        )

        # ------------------------------------------------------------
        # Telegram message size
        # ------------------------------------------------------------

        if len(output) > 3900:

            chunks = []

            current = ""

            for result in results:

                block = format_result(
                    result,
                    results.index(result) + 1,
                )

                if (
                    len(current)
                    + len(block)
                    + 2
                    > 3900
                ):

                    chunks.append(
                        current
                    )

                    current = block

                else:

                    if current:
                        current += "\n\n"

                    current += block

            if current:

                chunks.append(
                    current
                )

            await status_message.delete()

            for chunk in chunks:

                await event.respond(
                    chunk,
                    parse_mode="html",
                    link_preview=False,
                )

        else:

            await status_message.edit(
                output,
                parse_mode="html",
                link_preview=False,
            )

    except Exception as exc:

        logger.exception(
            "getuidfb failed"
        )

        try:

            await event.respond(
                (
                    "❌ <b>Resolver error</b>\n\n"
                    f"<code>{tg_escape(str(exc))}</code>"
                ),
                parse_mode="html",
            )

        except Exception:

            pass


# ====================================================================
# REGISTER
# ====================================================================

def register(
    client,
    *args,
    **kwargs,
):
    """
    Compatible with:

        module.register(client)

    or:

        module.register(client, notify_bot)

    or:

        module.register(bot=client, ...)
    """

    client.add_event_handler(
        _handle_getuidfb,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?(?:\s+[\s\S]+)?$"
        ),
    )

    logger.info(
        "Registered /getuidfb V19 ULTRA"
    )

    return True


# ====================================================================
# OPTIONAL DIRECT HANDLER ALIAS
# ====================================================================

handler = _handle_getuidfb


# ====================================================================
# END
# ====================================================================