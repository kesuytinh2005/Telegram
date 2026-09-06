#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
====================================================================
 FACEBOOK UID / ENTITY RESOLVER V20 ULTRA
 TELEGRAM / TELETHON
====================================================================

PUBLIC CONTENT ONLY
HTTP ONLY

NO:
    - Playwright
    - Selenium
    - Chromium
    - Cookies
    - Facebook Access Token
    - Facebook Login

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

    pfbid
    media_fbid
    actor_id
    profile_id
    entity_id
    owner_id
    publisher_id
    page_id
    group_id
    post_id
    video_id
    story_fbid

TELEGRAM:
    /getuidfb

After /getuidfb:
    send one URL
    send many URLs
    send URLs separated by spaces
    send URLs separated by newlines

The bot keeps the resolver session open and waits for
the next URL until the user exits it.
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

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from urllib.parse import (
    parse_qs,
    unquote,
    urljoin,
    urlparse,
)

import httpx
from telethon import events


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# COMMAND INFO
# ============================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": "Facebook UID / Entity Resolver V20 Ultra",
    "usage": "/getuidfb",
    "category": "Facebook",
}


# ============================================================
# CONSTANTS
# ============================================================

MAX_URLS_PER_MESSAGE = 50
MAX_HTML_CHARS = 4_000_000
MAX_RESPONSE_BYTES = 12_000_000

REQUEST_TIMEOUT = 20.0
PROFILE_TIMEOUT = 15.0

INTERACTIVE_TIMEOUT = 900

MAX_PFBID_WINDOW = 12000

FB_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "m.facebookcorewwwi.onion",
}

FB_SHARE_HOSTS = {
    "fb.watch",
}

FB_RESERVED_ROUTES = {
    "home",
    "watch",
    "reel",
    "reels",
    "video",
    "videos",
    "photo",
    "photos",
    "story",
    "stories",
    "share",
    "groups",
    "pages",
    "profile.php",
    "photo.php",
    "story.php",
    "permalink.php",
    "posts",
    "events",
    "marketplace",
    "gaming",
    "login",
    "recover",
    "help",
    "privacy",
    "terms",
    "settings",
    "messages",
    "notifications",
    "friends",
    "search",
    "hashtag",
    "plugins",
    "dialog",
    "ajax",
    "business",
}

USER_AGENT_POOL = (
    (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.5 Mobile/15E148 Safari/604.1"
    ),
)


# ============================================================
# REGEX
# ============================================================

NUMERIC_ID_RE = re.compile(
    r"(?<!\d)(\d{5,25})(?!\d)"
)

PFBID_RE = re.compile(
    r"\bpfbid[A-Za-z0-9_-]{6,300}\b",
    re.IGNORECASE,
)

MEDIA_FBID_RE = re.compile(
    r"\bmedia_fbid[_:=\"'\s]+([A-Za-z0-9_-]{5,300})",
    re.IGNORECASE,
)

FB_URL_RE = re.compile(
    r"""(?ix)
    (?:
        https?://
    )?
    (?:
        (?:www\.|m\.|mbasic\.|web\.)?facebook\.com
        |
        fb\.watch
    )
    /
    [^\s<>"'`]+
    """
)

ALL_URL_RE = re.compile(
    r"""https?://[^\s<>"'`]+""",
    re.IGNORECASE,
)


# ============================================================
# HELPERS
# ============================================================

def clean_text(value: Any) -> str:
    if value is None:
        return ""

    return html.unescape(
        str(value)
    ).strip()


def normalize_space(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        clean_text(value),
    ).strip()


def tg_escape(value: Any) -> str:
    """
    Telegram HTML escaping.
    """

    return html.escape(
        clean_text(value),
        quote=False,
    )


def truncate(
    value: Any,
    limit: int = 300,
) -> str:

    value = clean_text(value)

    if len(value) <= limit:
        return value

    return value[: max(0, limit - 1)] + "…"


def numeric_id(value: Any) -> Optional[str]:
    """
    Chỉ chấp nhận ID số hợp lệ.
    """

    if value is None:
        return None

    value = clean_text(value)

    match = re.fullmatch(
        r"\d{5,25}",
        value,
    )

    if not match:
        return None

    return value


def unique_preserve(
    values: Iterable[Any],
) -> list[str]:

    result = []
    seen = set()

    for value in values:

        value = clean_text(value)

        if not value:
            continue

        key = value.lower()

        if key in seen:
            continue

        seen.add(key)
        result.append(value)

    return result


def host_of(url: str) -> str:
    try:
        return (
            urlparse(url).netloc
            .lower()
            .split("@")[-1]
            .split(":")[0]
        )
    except Exception:
        return ""


def is_facebook_host(host: str) -> bool:

    host = host.lower().strip()

    return (
        host in FB_HOSTS
        or host.endswith(".facebook.com")
        or host in FB_SHARE_HOSTS
        or host.endswith(".fb.watch")
    )


def is_numeric(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"\d{5,25}",
            value or "",
        )
    )


# ============================================================
# URL NORMALIZATION
# ============================================================

def normalize_input_url(raw: str) -> str:
    raw = clean_text(raw)

    raw = raw.strip(
        " \t\r\n<>[](){}'\".,;"
    )

    if not raw:
        return ""

    if raw.startswith("//"):
        raw = "https:" + raw

    if not re.match(
        r"^https?://",
        raw,
        re.IGNORECASE,
    ):
        raw = "https://" + raw

    return raw


def canonicalize_url(url: str) -> str:

    url = normalize_input_url(url)

    if not url:
        return ""

    try:
        parsed = urlparse(url)

        scheme = "https"

        host = parsed.netloc.lower()

        if host.startswith("www."):
            host = host[4:]

        path = re.sub(
            r"/{2,}",
            "/",
            parsed.path or "/",
        )

        if path != "/":
            path = path.rstrip("/")

        return (
            f"{scheme}://{host}"
            f"{path}"
        )

    except Exception:
        return url


# ============================================================
# URL DATA
# ============================================================

@dataclass
class ParsedURL:

    original: str

    normalized: str = ""

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

    post_id: Optional[str] = None
    video_id: Optional[str] = None
    photo_id: Optional[str] = None
    story_id: Optional[str] = None

    share_type: Optional[str] = None


# ============================================================
# URL CLASSIFIER
# ============================================================

def classify_facebook_url(
    raw_url: str,
) -> ParsedURL:

    original = raw_url

    normalized = normalize_input_url(
        raw_url
    )

    parsed = urlparse(normalized)

    host = (
        parsed.netloc
        .lower()
        .split(":")[0]
    )

    path = parsed.path or "/"

    segments = [
        unquote(x).strip()
        for x in path.split("/")
        if x.strip()
    ]

    lower = [
        x.lower()
        for x in segments
    ]

    query = parse_qs(
        parsed.query
    )

    result = ParsedURL(
        original=original,
        normalized=normalized,
        host=host,
        path=path,
    )

    # --------------------------------------------------------
    # PROFILE.PHP
    # --------------------------------------------------------

    if lower and lower[0] == "profile.php":

        uid = first_query_value(
            query,
            (
                "id",
                "profile_id",
                "user_id",
            ),
        )

        uid = numeric_id(uid)

        result.url_type = "PROFILE"
        result.entity_type = "USER"
        result.profile_id = uid
        result.object_id = uid

        return result

    # --------------------------------------------------------
    # PHOTO.PHP
    # --------------------------------------------------------

    if lower and lower[0] == "photo.php":

        fbid = first_query_value(
            query,
            (
                "fbid",
                "photo_id",
                "photoid",
            ),
        )

        fbid = clean_text(fbid)

        result.url_type = "PHOTO"
        result.entity_type = "PHOTO"

        if fbid:
            result.photo_id = (
                numeric_id(fbid)
                or fbid
            )

            if not is_numeric(fbid):
                result.opaque_id = fbid

        owner = first_query_value(
            query,
            (
                "id",
                "owner_id",
            ),
        )

        owner = numeric_id(owner)

        if owner:
            result.profile_id = owner

        return result

    # --------------------------------------------------------
    # STORY.PHP
    # --------------------------------------------------------

    if lower and lower[0] == "story.php":

        story = first_query_value(
            query,
            (
                "story_fbid",
                "story_id",
                "fbid",
            ),
        )

        owner = first_query_value(
            query,
            (
                "id",
                "owner_id",
                "profile_id",
            ),
        )

        result.url_type = "STORY"
        result.entity_type = "STORY"

        if story:
            result.story_id = (
                numeric_id(story)
                or story
            )

            if not is_numeric(story):
                result.opaque_id = story

        result.profile_id = numeric_id(
            owner
        )

        return result

    # --------------------------------------------------------
    # WATCH
    # --------------------------------------------------------

    if lower and lower[0] == "watch":

        video = first_query_value(
            query,
            (
                "v",
                "video_id",
            ),
        )

        result.url_type = "VIDEO"
        result.entity_type = "VIDEO"

        if video:
            result.video_id = (
                numeric_id(video)
                or video
            )

            result.object_id = (
                result.video_id
            )

            if not is_numeric(video):
                result.opaque_id = video

        return result

    # --------------------------------------------------------
    # SHARE
    # --------------------------------------------------------

    if len(lower) >= 2 and lower[0] == "share":

        share_kind = lower[1]

        token = (
            segments[2]
            if len(segments) >= 3
            else None
        )

        result.share_type = share_kind
        result.opaque_id = token

        if share_kind == "p":
            result.url_type = "POST"
            result.entity_type = "POST"

        elif share_kind == "v":
            result.url_type = "VIDEO"
            result.entity_type = "VIDEO"

        elif share_kind == "r":
            result.url_type = "REEL"
            result.entity_type = "VIDEO"

        else:
            result.url_type = "SHARE"
            result.entity_type = "UNKNOWN"

        result.object_id = token

        return result

    # --------------------------------------------------------
    # P / PFBID
    # --------------------------------------------------------

    if len(lower) >= 2 and lower[0] == "p":

        token = segments[1]

        result.url_type = "POST"
        result.entity_type = "POST"
        result.object_id = token

        if token.lower().startswith("pfbid"):
            result.opaque_id = token

        elif is_numeric(token):
            result.post_id = token

        return result

    # --------------------------------------------------------
    # GROUPS
    # --------------------------------------------------------

    if lower and lower[0] == "groups":

        result.entity_type = "GROUP"

        group_identifier = (
            segments[1]
            if len(segments) >= 2
            else None
        )

        group_numeric = numeric_id(
            group_identifier
        )

        if group_numeric:
            result.group_id = group_numeric

        if len(lower) >= 3:

            action = lower[2]

            if action in {
                "posts",
                "post",
                "permalink",
            }:

                result.url_type = "GROUP_POST"

                if len(segments) >= 4:

                    token = segments[3]

                    result.object_id = token

                    if token.lower().startswith(
                        "pfbid"
                    ):
                        result.opaque_id = token

                    elif is_numeric(token):
                        result.post_id = token

                return result

        result.url_type = "GROUP"

        return result

    # --------------------------------------------------------
    # PAGES
    # --------------------------------------------------------

    if lower and lower[0] == "pages":

        result.entity_type = "PAGE"

        page_name = (
            segments[1]
            if len(segments) >= 2
            else None
        )

        if page_name:
            result.username = page_name

        page_numeric = None

        for segment in segments[2:4]:

            if is_numeric(segment):
                page_numeric = segment
                break

        if page_numeric:
            result.page_id = page_numeric

        if len(lower) >= 4:

            action = lower[3]

            if action in {
                "posts",
                "post",
                "permalink",
            }:

                result.url_type = "PAGE_POST"

                if len(segments) >= 5:

                    token = segments[4]

                    result.object_id = token

                    if token.lower().startswith(
                        "pfbid"
                    ):
                        result.opaque_id = token

                    elif is_numeric(token):
                        result.post_id = token

                return result

        result.url_type = "PAGE"

        return result

    # --------------------------------------------------------
    # REELS
    # --------------------------------------------------------

    if lower and lower[0] in {
        "reel",
        "reels",
    }:

        token = (
            segments[1]
            if len(segments) >= 2
            else None
        )

        result.url_type = "REEL"
        result.entity_type = "VIDEO"
        result.object_id = token

        if token:
            if token.lower().startswith("pfbid"):
                result.opaque_id = token
            elif is_numeric(token):
                result.video_id = token

        return result

    # --------------------------------------------------------
    # VIDEOS
    # --------------------------------------------------------

    if lower and lower[0] in {
        "video",
        "videos",
    }:

        token = (
            segments[1]
            if len(segments) >= 2
            else None
        )

        result.url_type = "VIDEO"
        result.entity_type = "VIDEO"
        result.object_id = token

        if token:
            if token.lower().startswith("pfbid"):
                result.opaque_id = token
            elif is_numeric(token):
                result.video_id = token

        return result

    # --------------------------------------------------------
    # STORY / STORIES
    # --------------------------------------------------------

    if lower and lower[0] in {
        "story",
        "stories",
    }:

        token = (
            segments[1]
            if len(segments) >= 2
            else None
        )

        result.url_type = "STORY"
        result.entity_type = "STORY"
        result.object_id = token

        if token:
            if token.lower().startswith("pfbid"):
                result.opaque_id = token
            elif is_numeric(token):
                result.story_id = token

        return result

    # --------------------------------------------------------
    # USERNAME ROUTES
    # --------------------------------------------------------

    if segments:

        first = segments[0]

        if (
            first.lower()
            not in FB_RESERVED_ROUTES
        ):

            result.username = first

            if len(lower) >= 2:

                action = lower[1]

                if action == "posts":

                    result.url_type = "USER_POST"
                    result.entity_type = "USER"

                    if len(segments) >= 3:

                        token = segments[2]

                        result.object_id = token

                        if token.lower().startswith(
                            "pfbid"
                        ):
                            result.opaque_id = token

                        elif is_numeric(token):
                            result.post_id = token

                    return result

                if action in {
                    "reels",
                    "reel",
                }:

                    result.url_type = "REEL"
                    result.entity_type = "USER"

                    if len(segments) >= 3:

                        token = segments[2]
                        result.object_id = token

                        if token.lower().startswith(
                            "pfbid"
                        ):
                            result.opaque_id = token

                        elif is_numeric(token):
                            result.video_id = token

                    return result

                if action in {
                    "videos",
                    "video",
                }:

                    result.url_type = "VIDEO"
                    result.entity_type = "USER"

                    if len(segments) >= 3:

                        token = segments[2]
                        result.object_id = token

                        if token.lower().startswith(
                            "pfbid"
                        ):
                            result.opaque_id = token

                        elif is_numeric(token):
                            result.video_id = token

                    return result

                if action in {
                    "photos",
                    "photo",
                }:

                    result.url_type = "PHOTO"
                    result.entity_type = "USER"

                    if len(segments) >= 3:

                        token = segments[2]
                        result.object_id = token

                        if token.lower().startswith(
                            "pfbid"
                        ):
                            result.opaque_id = token

                        elif is_numeric(token):
                            result.photo_id = token

                    return result

                if action in {
                    "story",
                    "stories",
                }:

                    result.url_type = "STORY"
                    result.entity_type = "USER"

                    if len(segments) >= 3:

                        token = segments[2]
                        result.object_id = token

                        if token.lower().startswith(
                            "pfbid"
                        ):
                            result.opaque_id = token

                        elif is_numeric(token):
                            result.story_id = token

                    return result

            # plain profile
            result.url_type = "PROFILE"
            result.entity_type = "USER"

            return result

    return result


# ============================================================
# QUERY HELPERS
# ============================================================

def first_query_value(
    query: dict[str, list[str]],
    keys: Iterable[str],
) -> Optional[str]:

    for key in keys:

        values = query.get(key)

        if not values:
            continue

        for value in values:

            value = clean_text(value)

            if value:
                return value

    return None


# ============================================================
# TELEGRAM URL EXTRACTION
# ============================================================

def extract_facebook_urls(
    text: str,
) -> list[str]:

    if not text:
        return []

    candidates = []

    # --------------------------------------------------------
    # HTTP URLs
    # --------------------------------------------------------

    for match in ALL_URL_RE.finditer(text):

        raw = match.group(0)

        raw = raw.strip(
            " \t\r\n<>[](){}'\".,;"
        )

        if not raw:
            continue

        host = host_of(raw)

        if is_facebook_host(host):
            candidates.append(raw)

    # --------------------------------------------------------
    # Bare facebook.com / fb.watch
    # --------------------------------------------------------

    for match in FB_URL_RE.finditer(text):

        raw = match.group(0)

        raw = raw.strip(
            " \t\r\n<>[](){}'\".,;"
        )

        normalized = normalize_input_url(
            raw
        )

        if normalized:
            candidates.append(
                normalized
            )

    # --------------------------------------------------------
    # Remove duplicates
    # --------------------------------------------------------

    result = []

    seen = set()

    for url in candidates:

        url = normalize_input_url(url)

        if not url:
            continue

        key = url.lower()

        if key in seen:
            continue

        seen.add(key)
        result.append(url)

    return result[:MAX_URLS_PER_MESSAGE]


# ============================================================
# SNAPSHOT
# ============================================================

@dataclass
class Snapshot:

    requested_url: str

    final_url: str = ""

    status_code: int = 0

    html: str = ""

    title: str = ""

    metas: dict[str, str] = field(
        default_factory=dict
    )

    canonical: str = ""

    links: list[str] = field(
        default_factory=list
    )

    json_ld: list[Any] = field(
        default_factory=list
    )

    redirect_chain: list[str] = field(
        default_factory=list
    )


# ============================================================
# EVIDENCE
# ============================================================

@dataclass
class Evidence:

    value: str

    role: str

    source: str

    score: float

    context: str = ""

    object_token: str = ""

    independent: bool = True


# ============================================================
# CANDIDATE
# ============================================================

@dataclass
class Candidate:

    value: str

    role: str

    score: float = 0.0

    evidence: list[Evidence] = field(
        default_factory=list
    )

    independent_sources: set[str] = field(
        default_factory=set
    )

    def add(
        self,
        evidence: Evidence,
    ):

        self.evidence.append(
            evidence
        )

        self.score += evidence.score

        if evidence.independent:
            self.independent_sources.add(
                evidence.source
            )


# ============================================================
# RESOLVE RESULT
# ============================================================

@dataclass
class ResolveResult:

    original_url: str

    final_url: str = ""

    url_type: str = "UNKNOWN"
    entity_type: str = "UNKNOWN"

    status: str = "NOT_RESOLVED"

    confidence: int = 0

    user_uid: Optional[str] = None
    page_uid: Optional[str] = None
    group_id: Optional[str] = None

    post_id: Optional[str] = None
    video_id: Optional[str] = None
    photo_id: Optional[str] = None
    story_id: Optional[str] = None

    publisher_id: Optional[str] = None
    publisher_type: Optional[str] = None
    username: Optional[str] = None
    title: Optional[str] = None

    opaque_id: Optional[str] = None

    verified: bool = False

    evidence_count: int = 0


# ============================================================
# EVIDENCE STORE
# ============================================================

class EvidenceStore:

    def __init__(self):

        self.items: list[Evidence] = []

        self.by_value: dict[
            str,
            list[Evidence],
        ] = {}

    def add(
        self,
        value: Any,
        role: str,
        source: str,
        score: float,
        context: str = "",
        object_token: str = "",
        independent: bool = True,
    ):

        value = clean_text(value)

        if not value:
            return

        evidence = Evidence(
            value=value,
            role=role,
            source=source,
            score=score,
            context=truncate(
                context,
                500,
            ),
            object_token=object_token,
            independent=independent,
        )

        self.items.append(evidence)

        self.by_value.setdefault(
            value,
            [],
        ).append(evidence)

    def values(
        self,
        role: Optional[str] = None,
    ) -> list[str]:

        result = []

        for item in self.items:

            if role and item.role != role:
                continue

            result.append(item.value)

        return unique_preserve(result)

    def evidence_for(
        self,
        value: str,
    ) -> list[Evidence]:

        return self.by_value.get(
            value,
            [],
        )

    def count(self) -> int:
        return len(self.items)


# ============================================================
# HTTP CLIENT
# ============================================================

class PublicHTTP:

    def __init__(self):

        self.semaphore = asyncio.Semaphore(
            5
        )

    async def fetch(
        self,
        url: str,
        timeout: float = REQUEST_TIMEOUT,
    ) -> Optional[Snapshot]:

        url = normalize_input_url(url)

        if not url:
            return None

        last_error = None

        async with self.semaphore:

            for attempt in range(3):

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

                try:

                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            timeout
                        ),
                        follow_redirects=True,
                        http2=False,
                        headers=headers,
                    ) as client:

                        response = await client.get(
                            url
                        )

                    content_type = (
                        response.headers.get(
                            "content-type",
                            "application/x-www-form-urlencoded",
                        )
                        .lower()
                    )

                    body = response.content

                    if len(body) > MAX_RESPONSE_BYTES:
                        body = body[
                            :MAX_RESPONSE_BYTES
                        ]

                    if (
                        "text/html"
                        not in content_type
                        and not body.lstrip().startswith(
                            b"<"
                        )
                    ):
                        return Snapshot(
                            requested_url=url,
                            final_url=str(
                                response.url
                            ),
                            status_code=response.status_code,
                            html="",
                        )

                    text = body.decode(
                        "utf-8",
                        errors="ignore",
                    )

                    if len(text) > MAX_HTML_CHARS:
                        text = text[
                            :MAX_HTML_CHARS
                        ]

                    snapshot = parse_snapshot(
                        url,
                        str(response.url),
                        response.status_code,
                        text,
                    )

                    return snapshot

                except Exception as exc:

                    last_error = exc

                    if attempt < 2:
                        await asyncio.sleep(
                            0.7 * (attempt + 1)
                        )

        if last_error:
            logger.debug(
                "Facebook HTTP error %s: %s",
                url,
                last_error,
            )

        return None


# ============================================================
# SNAPSHOT PARSER
# ============================================================

def parse_snapshot(
    requested_url: str,
    final_url: str,
    status_code: int,
    text: str,
) -> Snapshot:

    metas = extract_meta(
        text
    )

    canonical = extract_canonical(
        text,
        final_url,
    )

    links = extract_links(
        text,
        final_url,
    )

    json_ld = extract_json_ld(
        text
    )

    title = extract_title(
        text,
        metas,
    )

    return Snapshot(
        requested_url=requested_url,
        final_url=final_url,
        status_code=status_code,
        html=text,
        title=title,
        metas=metas,
        canonical=canonical,
        links=links,
        json_ld=json_ld,
    )


# ============================================================
# META PARSER
# ============================================================

META_TAG_RE = re.compile(
    r"<meta\b[^>]*>",
    re.IGNORECASE,
)

ATTR_RE = re.compile(
    r"""([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)


def parse_attributes(
    tag: str,
) -> dict[str, str]:

    result = {}

    for match in ATTR_RE.finditer(tag):

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


def extract_meta(
    text: str,
) -> dict[str, str]:

    result = {}

    for tag_match in META_TAG_RE.finditer(
        text
    ):

        attrs = parse_attributes(
            tag_match.group(0)
        )

        key = (
            attrs.get("property")
            or attrs.get("name")
            or attrs.get("itemprop")
        )

        value = attrs.get("content")

        if key and value:
            result[
                key.lower()
            ] = clean_text(value)

    return result


# ============================================================
# CANONICAL
# ============================================================

LINK_TAG_RE = re.compile(
    r"<link\b[^>]*>",
    re.IGNORECASE,
)


def extract_canonical(
    text: str,
    base_url: str,
) -> str:

    for tag_match in LINK_TAG_RE.finditer(
        text
    ):

        attrs = parse_attributes(
            tag_match.group(0)
        )

        rel = attrs.get(
            "rel",
            "",
        ).lower()

        href = attrs.get(
            "href",
            "",
        )

        if (
            href
            and "canonical" in rel
        ):

            return urljoin(
                base_url,
                html.unescape(href),
            )

    return ""


# ============================================================
# LINKS
# ============================================================

ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))",
    re.IGNORECASE,
)


def extract_links(
    text: str,
    base_url: str,
) -> list[str]:

    result = []

    for match in ANCHOR_RE.finditer(
        text
    ):

        href = (
            match.group(1)
            or match.group(2)
            or match.group(3)
            or ""
        )

        href = html.unescape(
            href
        )

        if not href:
            continue

        absolute = urljoin(
            base_url,
            href,
        )

        if is_facebook_host(
            host_of(absolute)
        ):
            result.append(
                absolute
            )

    # --------------------------------------------------------
    # Also scan raw HTML / JS URLs
    # --------------------------------------------------------

    for match in ALL_URL_RE.finditer(
        text
    ):

        url = match.group(0).rstrip(
            ".,;)]}"
        )

        if is_facebook_host(
            host_of(url)
        ):
            result.append(url)

    return unique_preserve(
        result
    )[:300]


# ============================================================
# TITLE
# ============================================================

TITLE_RE = re.compile(
    r"<title\b[^>]*>(.*?)</title>",
    re.IGNORECASE | re.DOTALL,
)


def extract_title(
    text: str,
    metas: dict[str, str],
) -> str:

    match = TITLE_RE.search(
        text
    )

    if match:

        title = normalize_space(
            match.group(1)
        )

        if title:
            return title

    return (
        metas.get("og:title")
        or metas.get("twitter:title")
        or ""
    )


# ============================================================
# JSON-LD
# ============================================================

JSON_LD_RE = re.compile(
    r'<script\b[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def extract_json_ld(
    text: str,
) -> list[Any]:

    result = []

    for match in JSON_LD_RE.finditer(
        text
    ):

        raw = match.group(1).strip()

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


# ============================================================
# JSON WALKER
# ============================================================

def walk_json(
    value: Any,
) -> Iterable[dict[str, Any]]:

    if isinstance(
        value,
        dict,
    ):

        yield value

        for child in value.values():

            yield from walk_json(
                child
            )

    elif isinstance(
        value,
        list,
    ):

        for child in value:

            yield from walk_json(
                child
            )


# ============================================================
# ID KEY MAP
# ============================================================

ID_KEYS = {
    "user_id": "user",
    "userid": "user",
    "userId": "user",
    "profile_id": "profile",
    "profileId": "profile",
    "profileID": "profile",
    "owner_id": "owner",
    "ownerId": "owner",
    "publisher_id": "publisher",
    "publisherId": "publisher",
    "actor_id": "actor",
    "actorId": "actor",
    "page_id": "page",
    "pageId": "page",
    "group_id": "group",
    "groupId": "group",
    "entity_id": "entity",
    "entityId": "entity",
    "post_id": "post",
    "postId": "post",
    "video_id": "video",
    "videoId": "video",
    "photo_id": "photo",
    "photoId": "photo",
    "story_fbid": "story",
    "storyId": "story",
    "story_id": "story",
    "media_fbid": "media",
    "mediaFbid": "media",
    "fbid": "fbid",
}


# ============================================================
# EXPLICIT ID EXTRACTION
# ============================================================

def extract_explicit_ids(
    text: str,
    store: EvidenceStore,
    object_token: str = "",
):

    if not text:
        return

    # --------------------------------------------------------
    # JSON / JS key:value
    # --------------------------------------------------------

    key_pattern = "|".join(
        re.escape(key)
        for key in ID_KEYS
    )

    pattern = re.compile(
        rf"""
        ["']?
        (?P<key>{key_pattern})
        ["']?
        \s*
        :
        \s*
        ["']?
        (?P<value>\d{{5,25}})
        ["']?
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    for match in pattern.finditer(
        text
    ):

        key = match.group(
            "key"
        )

        value = match.group(
            "value"
        )

        role = ID_KEYS.get(
            key,
            "generic",
        )

        score = {
            "user": 11,
            "profile": 12,
            "owner": 10,
            "publisher": 10,
            "page": 10,
            "group": 10,
            "actor": 6,
            "entity": 7,
            "post": 9,
            "video": 9,
            "photo": 8,
            "story": 8,
            "media": 7,
            "fbid": 6,
        }.get(
            role,
            4,
        )

        context_start = max(
            0,
            match.start() - 250,
        )

        context_end = min(
            len(text),
            match.end() + 250,
        )

        context = text[
            context_start:context_end
        ]

        store.add(
            value=value,
            role=role,
            source=f"explicit:{key}",
            score=score,
            context=context,
            object_token=object_token,
        )

    # --------------------------------------------------------
    # data-* attributes
    # --------------------------------------------------------

    data_pattern = re.compile(
        r"""
        data-
        (?P<key>
            user-id|
            profile-id|
            owner-id|
            publisher-id|
            actor-id|
            page-id|
            group-id|
            entity-id|
            post-id|
            video-id|
            photo-id|
            story-id|
            media-fbid
        )
        \s*=\s*
        ["']
        (?P<value>\d{5,25})
        ["']
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    for match in data_pattern.finditer(
        text
    ):

        key = match.group(
            "key"
        ).lower()

        value = match.group(
            "value"
        )

        role = key.replace(
            "-id",
            "",
        )

        if role == "media-fbid":
            role = "media"

        store.add(
            value=value,
            role=role,
            source=f"data:{key}",
            score=8,
            context=match.group(0),
            object_token=object_token,
        )


# ============================================================
# GENERIC NUMERIC IDS
# ============================================================

def extract_generic_numeric_ids(
    text: str,
    store: EvidenceStore,
    object_token: str = "",
):

    if not text:
        return

    # Deliberately low score.
    # Generic IDs NEVER directly become user_uid.

    for match in NUMERIC_ID_RE.finditer(
        text
    ):

        value = match.group(1)

        start = max(
            0,
            match.start() - 150,
        )

        end = min(
            len(text),
            match.end() + 150,
        )

        context = text[
            start:end
        ]

        store.add(
            value=value,
            role="generic",
            source="generic:numeric",
            score=0.5,
            context=context,
            object_token=object_token,
            independent=False,
        )


# ============================================================
# PROFILE URL EXTRACTION
# ============================================================

def profile_from_url(
    url: str,
) -> tuple[
    Optional[str],
    Optional[str],
]:

    try:

        parsed = urlparse(
            normalize_input_url(url)
        )

        path = [
            unquote(x)
            for x in parsed.path.split("/")
            if x.strip()
        ]

        lower = [
            x.lower()
            for x in path
        ]

        query = parse_qs(
            parsed.query
        )

        # profile.php?id=
        if lower and lower[0] == "profile.php":

            uid = numeric_id(
                first_query_value(
                    query,
                    (
                        "id",
                        "profile_id",
                        "user_id",
                    ),
                )
            )

            return (
                None,
                uid,
            )

        # /pages/name/123
        if (
            lower
            and lower[0] == "pages"
        ):

            page_id = None

            for segment in path[2:4]:

                if is_numeric(segment):
                    page_id = segment
                    break

            return (
                None,
                page_id,
            )

        # /username
        if path:

            first = path[0]

            if (
                first.lower()
                not in FB_RESERVED_ROUTES
                and not is_numeric(first)
            ):

                return (
                    first,
                    None,
                )

    except Exception:
        pass

    return (
        None,
        None,
    )


# ============================================================
# PFBID LOCAL CORRELATION
# ============================================================

def extract_opaque_tokens(
    text: str,
) -> list[str]:

    tokens = []

    tokens.extend(
        PFBID_RE.findall(
            text
        )
    )

    for match in MEDIA_FBID_RE.finditer(
        text
    ):

        tokens.append(
            match.group(1)
        )

    return unique_preserve(
        tokens
    )


def decode_base64_candidate(
    token: str,
) -> list[str]:

    """
    Conservative Base64 decoder.

    This does NOT assume that pfbid itself is mathematically
    decodable into a UID.

    It only tries common Base64 / URL-safe forms and returns
    readable decoded fragments for correlation.
    """

    token = clean_text(token)

    if len(token) < 8:
        return []

    variants = [
        token,
        token.replace("-", "+").replace("_", "/"),
    ]

    result = []

    for variant in variants:

        try:

            padding = "=" * (
                (-len(variant)) % 4
            )

            raw = base64.b64decode(
                variant + padding,
                validate=False,
            )

            if not raw:
                continue

            decoded = raw.decode(
                "utf-8",
                errors="ignore",
            )

            decoded = clean_text(
                decoded
            )

            if decoded:
                result.append(
                    decoded
                )

        except (
            ValueError,
            binascii.Error,
        ):
            continue

    return unique_preserve(
        result
    )


def correlate_opaque_window(
    window: str,
    token: str,
    store: EvidenceStore,
):

    # --------------------------------------------------------
    # Explicit IDs around same opaque token
    # --------------------------------------------------------

    extract_explicit_ids(
        window,
        store,
        object_token=token,
    )

    # --------------------------------------------------------
    # Profile URLs around same token
    # --------------------------------------------------------

    for match in ALL_URL_RE.finditer(
        window
    ):

        url = match.group(0)

        if not is_facebook_host(
            host_of(url)
        ):
            continue

        username, profile_id = profile_from_url(
            url
        )

        if username:

            store.add(
                value=username,
                role="username",
                source="opaque:profile_url",
                score=8,
                context=url,
                object_token=token,
            )

        if profile_id:

            store.add(
                value=profile_id,
                role="profile",
                source="opaque:profile_id_url",
                score=13,
                context=url,
                object_token=token,
            )

    # --------------------------------------------------------
    # JSON-LD-ish author fields in local window
    # --------------------------------------------------------

    author_url_pattern = re.compile(
        r'"(?:url|profile_url|profileUrl|link)"\s*:\s*'
        r'"([^"]*facebook\.com[^"]+)"',
        re.IGNORECASE,
    )

    for match in author_url_pattern.finditer(
        window
    ):

        url = match.group(1)

        username, profile_id = profile_from_url(
            url
        )

        if username:

            store.add(
                value=username,
                role="username",
                source="opaque:author_url",
                score=7,
                context=url,
                object_token=token,
            )

        if profile_id:

            store.add(
                value=profile_id,
                role="profile",
                source="opaque:author_profile_id",
                score=13,
                context=url,
                object_token=token,
            )

    # --------------------------------------------------------
    # Conservative Base64 inspection
    # --------------------------------------------------------

    decoded_fragments = (
        decode_base64_candidate(token)
    )

    for decoded in decoded_fragments:

        for number in NUMERIC_ID_RE.findall(
            decoded
        ):

            store.add(
                value=number,
                role="decoded",
                source="opaque:base64",
                score=2,
                context=decoded,
                object_token=token,
                independent=False,
            )


def extract_opaque_correlations(
    text: str,
    store: EvidenceStore,
    tokens: Iterable[str],
):

    if not text:
        return

    for token in tokens:

        token_lower = token.lower()

        # ----------------------------------------------------
        # Search exact token locations
        # ----------------------------------------------------

        positions = []

        start = 0

        text_lower = text.lower()

        while True:

            position = text_lower.find(
                token_lower,
                start,
            )

            if position < 0:
                break

            positions.append(
                position
            )

            start = position + len(
                token
            )

            if len(positions) >= 15:
                break

        # ----------------------------------------------------
        # If not found, scan beginning/end conservatively
        # ----------------------------------------------------

        if not positions:
            positions = [0]

        for position in positions:

            left = max(
                0,
                position - MAX_PFBID_WINDOW,
            )

            right = min(
                len(text),
                position
                + len(token)
                + MAX_PFBID_WINDOW,
            )

            window = text[
                left:right
            ]

            correlate_opaque_window(
                window,
                token,
                store,
            )


# ============================================================
# JSON-LD ID EXTRACTION
# ============================================================

def extract_jsonld_evidence(
    snapshot: Snapshot,
    store: EvidenceStore,
):

    for data in snapshot.json_ld:

        for obj in walk_json(data):

            for key, value in obj.items():

                key_lower = str(
                    key
                ).lower()

                # ------------------------------------------------
                # author
                # ------------------------------------------------

                if key_lower == "author":

                    author_objects = (
                        value
                        if isinstance(
                            value,
                            list,
                        )
                        else [value]
                    )

                    for author in author_objects:

                        if isinstance(
                            author,
                            dict,
                        ):

                            author_url = clean_text(
                                author.get("url")
                            )

                            author_name = clean_text(
                                author.get("name")
                            )

                            if author_url:

                                username, profile_id = (
                                    profile_from_url(
                                        author_url
                                    )
                                )

                                if username:

                                    store.add(
                                        value=username,
                                        role="username",
                                        source="jsonld:author_url",
                                        score=10,
                                        context=author_url,
                                    )

                                if profile_id:

                                    store.add(
                                        value=profile_id,
                                        role="profile",
                                        source="jsonld:author_profile_id",
                                        score=14,
                                        context=author_url,
                                    )

                            if author_name:

                                store.add(
                                    value=author_name,
                                    role="publisher_name",
                                    source="jsonld:author_name",
                                    score=5,
                                    context=author_name,
                                )

                # ------------------------------------------------
                # url
                # ------------------------------------------------

                if key_lower == "url":

                    url = clean_text(
                        value
                    )

                    if (
                        url
                        and is_facebook_host(
                            host_of(url)
                        )
                    ):

                        username, profile_id = (
                            profile_from_url(
                                url
                            )
                        )

                        if username:

                            store.add(
                                value=username,
                                role="username",
                                source="jsonld:url",
                                score=7,
                                context=url,
                            )

                        if profile_id:

                            store.add(
                                value=profile_id,
                                role="profile",
                                source="jsonld:profile_id",
                                score=12,
                                context=url,
                            )


# ============================================================
# META EVIDENCE
# ============================================================

def extract_meta_evidence(
    snapshot: Snapshot,
    store: EvidenceStore,
):

    metas = snapshot.metas

    for key, value in metas.items():

        value = clean_text(value)

        if not value:
            continue

        key_lower = key.lower()

        # --------------------------------------------------------
        # OG URL
        # --------------------------------------------------------

        if key_lower in {
            "og:url",
            "twitter:url",
        }:

            username, profile_id = (
                profile_from_url(
                    value
                )
            )

            if username:

                store.add(
                    value=username,
                    role="username",
                    source=f"meta:{key_lower}",
                    score=8,
                    context=value,
                )

            if profile_id:

                store.add(
                    value=profile_id,
                    role="profile",
                    source=f"meta:{key_lower}:id",
                    score=13,
                    context=value,
                )

        # --------------------------------------------------------
        # title / description
        # --------------------------------------------------------

        elif key_lower in {
            "og:title",
            "twitter:title",
        }:

            store.add(
                value=value,
                role="title",
                source=f"meta:{key_lower}",
                score=3,
                context=value,
            )


# ============================================================
# LINK EVIDENCE
# ============================================================

def extract_link_evidence(
    snapshot: Snapshot,
    store: EvidenceStore,
):

    for url in snapshot.links:

        username, profile_id = (
            profile_from_url(
                url
            )
        )

        if username:

            store.add(
                value=username,
                role="username",
                source="link:profile",
                score=5,
                context=url,
            )

        if profile_id:

            store.add(
                value=profile_id,
                role="profile",
                source="link:profile_id",
                score=11,
                context=url,
            )


# ============================================================
# ID GRAPH
# ============================================================

class IDGraph:

    """
    Lightweight relationship graph.

    opaque
        ↓
    post/video/story/media
        ↓
    publisher/owner/actor
        ↓
    profile
        ↓
    numeric UID
    """

    def __init__(
        self,
        store: EvidenceStore,
    ):

        self.store = store

    def role_values(
        self,
        role: str,
        object_token: str = "",
    ) -> list[str]:

        values = []

        for item in self.store.items:

            if item.role != role:
                continue

            if (
                object_token
                and item.object_token
                and item.object_token.lower()
                != object_token.lower()
            ):
                continue

            values.append(
                item.value
            )

        return unique_preserve(
            values
        )

    def best_profile(
        self,
        object_token: str = "",
    ) -> Optional[str]:

        candidates = {}

        for item in self.store.items:

            if item.role not in {
                "user",
                "profile",
                "owner",
                "publisher",
            }:
                continue

            if (
                object_token
                and item.object_token
                and item.object_token.lower()
                != object_token.lower()
            ):
                continue

            value = numeric_id(
                item.value
            )

            if not value:
                continue

            candidates.setdefault(
                value,
                0.0,
            )

            candidates[value] += (
                item.score
            )

        if not candidates:
            return None

        return max(
            candidates,
            key=candidates.get,
        )


# ============================================================
# RESOLVER
# ============================================================

class FacebookResolver:

    def __init__(self):

        self.http = PublicHTTP()

    async def resolve(
        self,
        url: str,
    ) -> ResolveResult:

        started = time.monotonic()

        parsed = classify_facebook_url(
            url
        )

        result = ResolveResult(
            original_url=url,
            final_url=parsed.normalized,
            url_type=parsed.url_type,
            entity_type=parsed.entity_type,
            opaque_id=parsed.opaque_id,
        )

        store = EvidenceStore()

        # ----------------------------------------------------
        # URL evidence
        # ----------------------------------------------------

        if parsed.profile_id:

            store.add(
                value=parsed.profile_id,
                role="profile",
                source="url:profile_id",
                score=20,
                context=parsed.normalized,
            )

        if parsed.page_id:

            store.add(
                value=parsed.page_id,
                role="page",
                source="url:page_id",
                score=20,
                context=parsed.normalized,
            )

        if parsed.group_id:

            store.add(
                value=parsed.group_id,
                role="group",
                source="url:group_id",
                score=20,
                context=parsed.normalized,
            )

        if parsed.post_id:

            store.add(
                value=parsed.post_id,
                role="post",
                source="url:post_id",
                score=20,
                context=parsed.normalized,
            )

        if parsed.video_id:

            store.add(
                value=parsed.video_id,
                role="video",
                source="url:video_id",
                score=20,
                context=parsed.normalized,
            )

        if parsed.photo_id:

            store.add(
                value=parsed.photo_id,
                role="photo",
                source="url:photo_id",
                score=20,
                context=parsed.normalized,
            )

        if parsed.story_id:

            store.add(
                value=parsed.story_id,
                role="story",
                source="url:story_id",
                score=20,
                context=parsed.normalized,
            )

        if parsed.username:

            store.add(
                value=parsed.username,
                role="username",
                source="url:username",
                score=15,
                context=parsed.normalized,
            )

        # ----------------------------------------------------
        # First fetch
        # ----------------------------------------------------

        snapshot = await self.http.fetch(
            parsed.normalized
        )

        if snapshot is None:

            result.status = "NOT_RESOLVED"
            result.confidence = 0

            return result

        result.final_url = (
            snapshot.final_url
            or parsed.normalized
        )

        # ----------------------------------------------------
        # Re-classify final URL
        #
        # IMPORTANT:
        # Structural URL type from original URL remains
        # authoritative for POST/GROUP_POST/PAGE_POST.
        # ----------------------------------------------------

        final_parsed = classify_facebook_url(
            snapshot.final_url
        )

        if (
            parsed.url_type
            in {
                "UNKNOWN",
                "PROFILE",
                "SHARE",
            }
        ):

            if final_parsed.url_type != "UNKNOWN":

                result.url_type = (
                    final_parsed.url_type
                )

        # ----------------------------------------------------
        # Object token
        # ----------------------------------------------------

        object_token = (
            parsed.opaque_id
            or parsed.object_id
            or ""
        )

        if (
            not object_token
            and final_parsed.opaque_id
        ):
            object_token = (
                final_parsed.opaque_id
            )

        if object_token:

            result.opaque_id = object_token

        # ----------------------------------------------------
        # Extract evidence
        # ----------------------------------------------------

        extract_meta_evidence(
            snapshot,
            store,
        )

        extract_link_evidence(
            snapshot,
            store,
        )

        extract_jsonld_evidence(
            snapshot,
            store,
        )

        extract_explicit_ids(
            snapshot.html,
            store,
            object_token=object_token,
        )

        # Generic IDs are deliberately weak.
        extract_generic_numeric_ids(
            snapshot.html,
            store,
            object_token=object_token,
        )

        # ----------------------------------------------------
        # Opaque correlation
        # ----------------------------------------------------

        opaque_tokens = unique_preserve(
            [
                object_token,
                *extract_opaque_tokens(
                    snapshot.html
                ),
            ]
        )

        if opaque_tokens:

            extract_opaque_correlations(
                snapshot.html,
                store,
                opaque_tokens,
            )

        # ----------------------------------------------------
        # Crawl canonical / profile target
        # ----------------------------------------------------

        crawl_urls = []

        if snapshot.canonical:
            crawl_urls.append(
                snapshot.canonical
            )

        # Profile URL from original route
        if parsed.username:

            profile_url = (
                f"https://www.facebook.com/"
                f"{parsed.username}"
            )

            crawl_urls.append(
                profile_url
            )

        # Profile URL discovered from links
        usernames = store.values(
            "username"
        )

        for username in usernames[:8]:

            if (
                username.lower()
                in FB_RESERVED_ROUTES
            ):
                continue

            if (
                " "
                in username
            ):
                continue

            crawl_urls.append(
                f"https://www.facebook.com/"
                f"{username}"
            )

        crawl_urls = unique_preserve(
            crawl_urls
        )[:5]

        # ----------------------------------------------------
        # Crawl profiles concurrently
        # ----------------------------------------------------

        profile_snapshots = []

        if crawl_urls:

            tasks = [
                self.http.fetch(
                    u,
                    timeout=PROFILE_TIMEOUT,
                )
                for u in crawl_urls
            ]

            fetched = await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

            for item in fetched:

                if isinstance(
                    item,
                    Snapshot,
                ):
                    profile_snapshots.append(
                        item
                    )

        # ----------------------------------------------------
        # Process profile pages
        # ----------------------------------------------------

        for profile_snapshot in profile_snapshots:

            extract_meta_evidence(
                profile_snapshot,
                store,
            )

            extract_link_evidence(
                profile_snapshot,
                store,
            )

            extract_jsonld_evidence(
                profile_snapshot,
                store,
            )

            extract_explicit_ids(
                profile_snapshot.html,
                store,
            )

        # ----------------------------------------------------
        # Infer entity
        # ----------------------------------------------------

        entity_type = self.infer_entity(
            parsed,
            final_parsed,
            store,
            snapshot,
        )

        result.entity_type = entity_type

        # ----------------------------------------------------
        # Collect object IDs
        # ----------------------------------------------------

        self.collect_objects(
            parsed,
            final_parsed,
            store,
            result,
        )

        # ----------------------------------------------------
        # Publisher
        # ----------------------------------------------------

        publisher_type, publisher_id = (
            self.infer_publisher(
                parsed,
                entity_type,
                store,
            )
        )

        result.publisher_type = (
            publisher_type
        )

        result.publisher_id = (
            publisher_id
        )

        # ----------------------------------------------------
        # Username
        # ----------------------------------------------------

        result.username = (
            parsed.username
            or self.best_username(
                store
            )
        )

        # ----------------------------------------------------
        # Title
        # ----------------------------------------------------

        result.title = (
            snapshot.title
            or self.best_title(store)
        )

        # ----------------------------------------------------
        # UID selection
        # ----------------------------------------------------

        result.user_uid = (
            await self.select_user_uid(
                parsed=parsed,
                entity_type=entity_type,
                publisher_id=publisher_id,
                store=store,
                object_token=object_token,
                requested_url=parsed.normalized,
            )
        )

        # ----------------------------------------------------
        # Page UID
        # ----------------------------------------------------

        if entity_type == "PAGE":

            result.page_uid = (
                self.select_best_role_id(
                    store,
                    "page",
                )
                or parsed.page_id
            )

        # ----------------------------------------------------
        # Group ID
        # ----------------------------------------------------

        if entity_type == "GROUP":

            result.group_id = (
                self.select_best_role_id(
                    store,
                    "group",
                )
                or parsed.group_id
            )

        # ----------------------------------------------------
        # Confidence
        # ----------------------------------------------------

        result.evidence_count = (
            store.count()
        )

        result.confidence = (
            self.calculate_confidence(
                result,
                store,
            )
        )

        result.verified = (
            result.confidence >= 85
            and (
                bool(result.user_uid)
                or bool(result.page_uid)
                or bool(result.group_id)
                or bool(result.post_id)
                or bool(result.video_id)
                or bool(result.photo_id)
                or bool(result.story_id)
            )
        )

        if result.verified:

            result.status = "VERIFIED"

        elif result.confidence >= 55:

            result.status = "RESOLVED"

        else:

            result.status = "NOT_RESOLVED"

        elapsed = time.monotonic() - started

        logger.debug(
            "Resolved %s in %.2fs",
            parsed.normalized,
            elapsed,
        )

        return result

    # ========================================================
    # ENTITY INFERENCE
    # ========================================================

    def infer_entity(
        self,
        parsed: ParsedURL,
        final_parsed: ParsedURL,
        store: EvidenceStore,
        snapshot: Snapshot,
    ) -> str:

        # Structural route wins.
        if parsed.url_type == "GROUP_POST":
            return "GROUP"

        if parsed.url_type == "PAGE_POST":
            return "PAGE"

        if parsed.url_type == "GROUP":
            return "GROUP"

        if parsed.url_type == "PAGE":
            return "PAGE"

        # Strong explicit group evidence.
        if store.values("group"):
            if parsed.url_type not in {
                "PROFILE",
                "USER_POST",
            }:
                return "GROUP"

        # Strong page evidence.
        if store.values("page"):
            if parsed.url_type not in {
                "PROFILE",
            }:
                return "PAGE"

        # Username routes are initially USER.
        if parsed.url_type in {
            "USER_POST",
            "PROFILE",
        }:
            return "USER"

        # Media object without publisher.
        if parsed.url_type in {
            "POST",
            "REEL",
            "VIDEO",
            "PHOTO",
            "STORY",
        }:

            if store.values("group"):
                return "GROUP"

            if store.values("page"):
                return "PAGE"

            return "UNKNOWN"

        return (
            parsed.entity_type
            if parsed.entity_type
            else "UNKNOWN"
        )

    # ========================================================
    # OBJECT COLLECTION
    # ========================================================

    def collect_objects(
        self,
        parsed: ParsedURL,
        final_parsed: ParsedURL,
        store: EvidenceStore,
        result: ResolveResult,
    ):

        # ----------------------------------------------------
        # Original URL IDs are authoritative.
        # ----------------------------------------------------

        if parsed.post_id:
            result.post_id = parsed.post_id

        if parsed.video_id:
            result.video_id = parsed.video_id

        if parsed.photo_id:
            result.photo_id = parsed.photo_id

        if parsed.story_id:
            result.story_id = parsed.story_id

        # ----------------------------------------------------
        # Find best evidence IDs.
        # ----------------------------------------------------

        if not result.post_id:

            result.post_id = (
                self.best_role_value(
                    store,
                    "post",
                )
            )

        if not result.video_id:

            result.video_id = (
                self.best_role_value(
                    store,
                    "video",
                )
            )

        if not result.photo_id:

            result.photo_id = (
                self.best_role_value(
                    store,
                    "photo",
                )
            )

        if not result.story_id:

            result.story_id = (
                self.best_role_value(
                    store,
                    "story",
                )
            )

        # ----------------------------------------------------
        # pfbid fallback
        # ----------------------------------------------------

        if (
            not result.post_id
            and parsed.opaque_id
            and parsed.url_type
            in {
                "POST",
                "USER_POST",
                "PAGE_POST",
                "GROUP_POST",
            }
        ):

            result.post_id = (
                parsed.opaque_id
            )

        if (
            not result.video_id
            and parsed.opaque_id
            and parsed.url_type
            in {
                "VIDEO",
                "REEL",
            }
        ):

            result.video_id = (
                parsed.opaque_id
            )

    # ========================================================
    # PUBLISHER
    # ========================================================

    def infer_publisher(
        self,
        parsed: ParsedURL,
        entity_type: str,
        store: EvidenceStore,
    ) -> tuple[
        Optional[str],
        Optional[str],
    ]:

        # ----------------------------------------------------
        # Group
        # ----------------------------------------------------

        if entity_type == "GROUP":

            publisher = (
                self.best_role_value(
                    store,
                    "publisher",
                )
                or self.best_role_value(
                    store,
                    "owner",
                )
                or self.best_role_value(
                    store,
                    "actor",
                )
                or self.best_role_value(
                    store,
                    "user",
                )
                or self.best_role_value(
                    store,
                    "profile",
                )
            )

            if publisher:
                return (
                    "USER",
                    publisher,
                )

            return (
                None,
                parsed.group_id,
            )

        # ----------------------------------------------------
        # Page
        # ----------------------------------------------------

        if entity_type == "PAGE":

            page_id = (
                parsed.page_id
                or self.best_role_value(
                    store,
                    "page",
                )
            )

            if page_id:
                return (
                    "PAGE",
                    page_id,
                )

        # ----------------------------------------------------
        # User
        # ----------------------------------------------------

        if entity_type == "USER":

            publisher = (
                self.best_role_value(
                    store,
                    "user",
                )
                or self.best_role_value(
                    store,
                    "profile",
                )
                or self.best_role_value(
                    store,
                    "owner",
                )
                or self.best_role_value(
                    store,
                    "publisher",
                )
            )

            if publisher:
                return (
                    "USER",
                    publisher,
                )

        return (
            None,
            None,
        )

    # ========================================================
    # USER UID
    # ========================================================

    async def select_user_uid(
        self,
        parsed: ParsedURL,
        entity_type: str,
        publisher_id: Optional[str],
        store: EvidenceStore,
        object_token: str,
        requested_url: str,
    ) -> Optional[str]:

        # ----------------------------------------------------
        # For explicit USER profile routes:
        # profile_id is strongest.
        # ----------------------------------------------------

        if (
            entity_type == "USER"
            and parsed.profile_id
        ):

            return parsed.profile_id

        candidates: dict[
            str,
            Candidate,
        ] = {}

        # ----------------------------------------------------
        # Collect strong user-like roles
        # ----------------------------------------------------

        allowed_roles = {
            "user",
            "profile",
            "owner",
            "publisher",
        }

        for item in store.items:

            if item.role not in allowed_roles:
                continue

            value = numeric_id(
                item.value
            )

            if not value:
                continue

            candidate = candidates.get(
                value
            )

            if candidate is None:

                candidate = Candidate(
                    value=value,
                    role=item.role,
                )

                candidates[value] = candidate

            candidate.add(
                item
            )

        # ----------------------------------------------------
        # Actor ID is weaker.
        # ----------------------------------------------------

        for item in store.items:

            if item.role != "actor":
                continue

            value = numeric_id(
                item.value
            )

            if not value:
                continue

            candidate = candidates.get(
                value
            )

            if candidate is None:

                candidate = Candidate(
                    value=value,
                    role="actor",
                )

                candidates[value] = candidate

            candidate.add(
                item
            )

        if not candidates:
            return None

        # ----------------------------------------------------
        # Never blindly use group/page IDs.
        # ----------------------------------------------------

        group_ids = set(
            store.values("group")
        )

        page_ids = set(
            store.values("page")
        )

        # Remove only when the ID has clear exclusive
        # group/page evidence and no independent user evidence.
        for value in list(
            candidates
        ):

            candidate = candidates[
                value
            ]

            sources = (
                candidate.independent_sources
            )

            strong_user_source = any(
                src.startswith(
                    (
                        "explicit:user_id",
                        "explicit:profile",
                        "explicit:owner",
                        "explicit:publisher",
                        "url:profile_id",
                        "opaque:profile_id",
                        "jsonld:author_profile_id",
                    )
                )
                for src in sources
            )

            if (
                value in group_ids
                and not strong_user_source
            ):

                del candidates[
                    value
                ]

                continue

            if (
                value in page_ids
                and not strong_user_source
            ):

                del candidates[
                    value
                ]

        if not candidates:
            return None

        # ----------------------------------------------------
        # Score with independent source bonus.
        # ----------------------------------------------------

        for candidate in candidates.values():

            independent_count = len(
                candidate.independent_sources
            )

            candidate.score += (
                min(
                    independent_count,
                    5,
                )
                * 4
            )

            if candidate.role in {
                "profile",
                "user",
            }:
                candidate.score += 8

            if candidate.role == "actor":
                candidate.score -= 5

            if (
                publisher_id
                and candidate.value
                == publisher_id
            ):
                candidate.score += 12

        ranked = sorted(
            candidates.values(),
            key=lambda x: x.score,
            reverse=True,
        )

        if not ranked:
            return None

        best = ranked[0]

        # ----------------------------------------------------
        # Ambiguity protection.
        # ----------------------------------------------------

        if len(ranked) >= 2:

            second = ranked[1]

            if (
                best.score < 30
                and (
                    best.score
                    - second.score
                    < 8
                )
            ):
                return None

        # ----------------------------------------------------
        # Minimum evidence.
        # ----------------------------------------------------

        if best.score < 18:

            return None

        # ----------------------------------------------------
        # Explicit USER route can accept a strong candidate.
        # ----------------------------------------------------

        if parsed.url_type in {
            "USER_POST",
            "PROFILE",
        }:

            if best.score >= 18:
                return best.value

        # ----------------------------------------------------
        # Generic POST requires stronger evidence.
        # ----------------------------------------------------

        if best.score >= 30:
            return best.value

        return None

    # ========================================================
    # BEST ROLE VALUE
    # ========================================================

    def best_role_value(
        self,
        store: EvidenceStore,
        role: str,
    ) -> Optional[str]:

        scores: dict[str, float] = {}

        for item in store.items:

            if item.role != role:
                continue

            value = clean_text(
                item.value
            )

            if not value:
                continue

            scores.setdefault(
                value,
                0,
            )

            scores[value] += (
                item.score
            )

        if not scores:
            return None

        return max(
            scores,
            key=scores.get,
        )

    def best_role_value_numeric(
        self,
        store: EvidenceStore,
        role: str,
    ) -> Optional[str]:

        value = self.best_role_value(
            store,
            role,
        )

        return numeric_id(
            value
        )

    def select_best_role_id(
        self,
        store: EvidenceStore,
        role: str,
    ) -> Optional[str]:

        return (
            self.best_role_value_numeric(
                store,
                role,
            )
        )

    # ========================================================
    # USERNAME
    # ========================================================

    def best_username(
        self,
        store: EvidenceStore,
    ) -> Optional[str]:

        scores = {}

        for item in store.items:

            if item.role != "username":
                continue

            value = clean_text(
                item.value
            )

            if not value:
                continue

            if (
                value.lower()
                in FB_RESERVED_ROUTES
            ):
                continue

            scores.setdefault(
                value,
                0,
            )

            scores[value] += (
                item.score
            )

        if not scores:
            return None

        return max(
            scores,
            key=scores.get,
        )

    # ========================================================
    # TITLE
    # ========================================================

    def best_title(
        self,
        store: EvidenceStore,
    ) -> Optional[str]:

        value = self.best_role_value(
            store,
            "title",
        )

        return (
            truncate(
                value,
                250,
            )
            if value
            else None
        )

    # ========================================================
    # CONFIDENCE
    # ========================================================

    def calculate_confidence(
        self,
        result: ResolveResult,
        store: EvidenceStore,
    ) -> int:

        score = 0

        if result.final_url:
            score += 8

        if result.post_id:
            score += 12

        if result.video_id:
            score += 12

        if result.photo_id:
            score += 10

        if result.story_id:
            score += 10

        if result.group_id:
            score += 18

        if result.page_uid:
            score += 18

        if result.username:
            score += 8

        if result.user_uid:
            score += 30

        # Independent evidence.
        independent_sources = set()

        for item in store.items:

            if item.independent:
                independent_sources.add(
                    item.source
                )

        score += min(
            len(independent_sources) * 2,
            12,
        )

        # Strong user evidence.
        if result.user_uid:

            strong_sources = 0

            for item in store.items:

                if (
                    item.value
                    == result.user_uid
                    and item.role
                    in {
                        "user",
                        "profile",
                        "owner",
                        "publisher",
                    }
                ):

                    strong_sources += 1

            score += min(
                strong_sources * 4,
                12,
            )

        # Cap.
        return max(
            0,
            min(
                99,
                score,
            ),
        )


# ============================================================
# PUBLIC API
# ============================================================

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

    tasks = [
        resolver.resolve(
            url
        )
        for url in urls
    ]

    results = await asyncio.gather(
        *tasks,
        return_exceptions=True,
    )

    output = []

    for index, result in enumerate(
        results
    ):

        if isinstance(
            result,
            ResolveResult,
        ):
            output.append(
                result
            )
            continue

        logger.exception(
            "Resolver error #%s: %s",
            index + 1,
            result,
        )

        output.append(
            ResolveResult(
                original_url=urls[index],
                status="NOT_RESOLVED",
            )
        )

    return output


# ============================================================
# DISPLAY LABELS
# ============================================================

def display_entity_label(
    result: ResolveResult,
) -> str:

    if result.entity_type == "USER":

        if result.url_type in {
            "USER_POST",
            "POST",
        }:
            return "👤 USER POST"

        return "👤 USER"

    if result.entity_type == "PAGE":

        if result.url_type == "PAGE_POST":
            return "📄 PAGE POST"

        return "📄 PAGE"

    if result.entity_type == "GROUP":

        if result.url_type == "GROUP_POST":
            return "👥 GROUP POST"

        return "👥 GROUP"

    if result.url_type == "REEL":
        return "🎬 REEL"

    if result.url_type == "VIDEO":
        return "🎬 VIDEO"

    if result.url_type == "PHOTO":
        return "🖼 PHOTO"

    if result.url_type == "STORY":
        return "⭕ STORY"

    if result.url_type == "POST":
        return "📝 POST"

    return "🔎 FACEBOOK OBJECT"


# ============================================================
# RESULT FORMAT
# ============================================================

def format_result(
    result: ResolveResult,
    index: int = 1,
) -> str:

    lines = []

    lines.append(
        f"<b>{index}. "
        f"{tg_escape(display_entity_label(result))}"
        f"</b>"
    )

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    if result.status == "VERIFIED":

        lines.append(
            "📊 <b>STATUS:</b> VERIFIED"
        )

    elif result.status == "RESOLVED":

        lines.append(
            "📊 <b>STATUS:</b> RESOLVED"
        )

    else:

        lines.append(
            "📊 <b>STATUS:</b> NOT RESOLVED"
        )

    # --------------------------------------------------------
    # USER
    # --------------------------------------------------------

    if result.user_uid:

        lines.append(
            "🆔 <b>USER UID:</b> "
            f"<code>{tg_escape(result.user_uid)}</code>"
        )

    # --------------------------------------------------------
    # PAGE
    # --------------------------------------------------------

    if result.page_uid:

        lines.append(
            "📄 <b>PAGE UID:</b> "
            f"<code>{tg_escape(result.page_uid)}</code>"
        )

    # --------------------------------------------------------
    # GROUP
    # --------------------------------------------------------

    if result.group_id:

        lines.append(
            "👥 <b>GROUP ID:</b> "
            f"<code>{tg_escape(result.group_id)}</code>"
        )

    # --------------------------------------------------------
    # POST
    # --------------------------------------------------------

    if result.post_id:

        lines.append(
            "📝 <b>POST ID:</b> "
            f"<code>{tg_escape(result.post_id)}</code>"
        )

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    if result.video_id:

        lines.append(
            "🎬 <b>VIDEO ID:</b> "
            f"<code>{tg_escape(result.video_id)}</code>"
        )

    # --------------------------------------------------------
    # PHOTO
    # --------------------------------------------------------

    if result.photo_id:

        lines.append(
            "🖼 <b>PHOTO ID:</b> "
            f"<code>{tg_escape(result.photo_id)}</code>"
        )

    # --------------------------------------------------------
    # STORY
    # --------------------------------------------------------

    if result.story_id:

        lines.append(
            "⭕ <b>STORY ID:</b> "
            f"<code>{tg_escape(result.story_id)}</code>"
        )

    # --------------------------------------------------------
    # PUBLISHER
    # --------------------------------------------------------

    if result.publisher_id:

        # Do not duplicate USER UID.
        if (
            not result.user_uid
            or result.publisher_id
            != result.user_uid
        ):

            label = (
                "PAGE ID"
                if result.publisher_type == "PAGE"
                else "PUBLISHER ID"
            )

            lines.append(
                f"👤 <b>{label}:</b> "
                f"<code>{tg_escape(result.publisher_id)}</code>"
            )

    # --------------------------------------------------------
    # USERNAME
    # --------------------------------------------------------

    if result.username:

        lines.append(
            "👤 <b>PUBLISHER:</b> "
            f"{tg_escape(result.username)}"
        )

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    if result.title:

        # Avoid generic Facebook title.
        title_lower = (
            result.title.lower()
        )

        if title_lower not in {
            "facebook",
            "facebook - log in or sign up",
            "log in or sign up",
        }:

            lines.append(
                "📝 <b>TITLE:</b> "
                f"{tg_escape(truncate(result.title, 300))}"
            )

    # --------------------------------------------------------
    # CONFIDENCE
    # --------------------------------------------------------

    lines.append(
        "🎯 <b>CONFIDENCE:</b> "
        f"{result.confidence}%"
    )

    # --------------------------------------------------------
    # ORIGINAL URL
    # --------------------------------------------------------

    lines.append(
        "🔗 <b>URL:</b> "
        f'<a href="{html.escape(result.original_url, quote=True)}">'
        f"{tg_escape(truncate(result.original_url, 500))}"
        "</a>"
    )

    return "\n".join(
        lines
    )


# ============================================================
# INTERACTIVE SESSION
# ============================================================

async def wait_for_user_message(
    bot,
    user_id: int,
    chat_id: int,
    timeout: int = INTERACTIVE_TIMEOUT,
):

    loop = asyncio.get_running_loop()

    future = loop.create_future()

    async def temporary_handler(
        event,
    ):

        if event.sender_id != user_id:
            return

        if event.chat_id != chat_id:
            return

        if not future.done():

            future.set_result(
                event
            )

    bot.add_event_handler(
        temporary_handler,
        events.NewMessage(
            chats=chat_id
        ),
    )

    try:

        return await asyncio.wait_for(
            future,
            timeout=timeout,
        )

    finally:

        try:

            bot.remove_event_handler(
                temporary_handler,
                events.NewMessage(
                    chats=chat_id
                ),
            )

        except Exception:

            try:
                bot.remove_event_handler(
                    temporary_handler
                )
            except Exception:
                pass


# ============================================================
# INTERACTIVE COMMAND HANDLER
# ============================================================

async def _handle_getuidfb(
    event,
):
    """
    /getuidfb

    Opens an interactive URL input session.

    User can then send:
        URL 1

    then:
        URL 2

    or:
        URL 1
        URL 2
        URL 3

    The session remains active.
    """

    user_id = event.sender_id
    chat_id = event.chat_id

    if user_id is None or chat_id is None:
        return

    # --------------------------------------------------------
    # Initial message
    # --------------------------------------------------------

    await event.respond(
        "🔎 <b>FACEBOOK UID / ENTITY RESOLVER V20</b>\n\n"
        "📎 Gửi link Facebook cần phân tích.\n\n"
        "• Có thể gửi 1 link.\n"
        "• Có thể gửi nhiều link cùng lúc.\n"
        "• Không cần gõ lại /getuidfb.\n"
        "• URL có thể cách nhau bằng dấu cách hoặc xuống dòng.\n"
        "• Bot sẽ đếm chính xác số URL rồi xử lý từng link riêng.\n\n"
        "🛑 Gõ <code>/stop</code> để thoát chế độ nhập.",
        parse_mode="html",
    )

    # --------------------------------------------------------
    # Continuous input loop
    # --------------------------------------------------------

    while True:

        try:

            incoming = (
                await wait_for_user_message(
                    bot=event.client,
                    user_id=user_id,
                    chat_id=chat_id,
                    timeout=INTERACTIVE_TIMEOUT,
                )
            )

        except asyncio.TimeoutError:

            await event.respond(
                "⏱ <b>GETUIDFB</b>\n"
                "Phiên nhập URL đã hết thời gian chờ.",
                parse_mode="html",
            )

            return

        except asyncio.CancelledError:

            return

        except Exception as exc:

            logger.exception(
                "Interactive GETUIDFB error"
            )

            await event.respond(
                "❌ GETUIDFB ERROR\n"
                f"<code>{tg_escape(str(exc))}</code>",
                parse_mode="html",
            )

            return

        text = (
            incoming.raw_text
            or ""
        ).strip()

        if not text:
            continue

        # ----------------------------------------------------
        # Exit commands
        # ----------------------------------------------------

        if re.fullmatch(
            r"/stop(?:@\w+)?",
            text,
            re.IGNORECASE,
        ):

            await incoming.respond(
                "🛑 <b>GETUIDFB</b>\n"
                "Đã thoát chế độ nhập URL.",
                parse_mode="html",
            )

            return

        if re.fullmatch(
            r"/getuidfb(?:@\w+)?",
            text,
            re.IGNORECASE,
        ):

            await incoming.respond(
                "ℹ️ GETUIDFB đang hoạt động.\n"
                "Chỉ cần gửi URL Facebook, không cần gõ lại lệnh.",
                parse_mode="html",
            )

            continue

        # ----------------------------------------------------
        # Extract URLs
        # ----------------------------------------------------

        urls = extract_facebook_urls(
            text
        )

        if not urls:

            await incoming.respond(
                "❌ Không tìm thấy URL Facebook hợp lệ.\n\n"
                "Hãy gửi dạng:\n"
                "<code>https://www.facebook.com/...</code>",
                parse_mode="html",
            )

            continue

        # ----------------------------------------------------
        # Exact URL count
        # ----------------------------------------------------

        count = len(urls)

        await incoming.respond(
            f"🔎 <b>ĐÃ NHẬN {count} URL</b>\n"
            "⚙️ Đang phân tích từng URL...",
            parse_mode="html",
        )

        # ----------------------------------------------------
        # Resolve ALL URLs concurrently
        # ----------------------------------------------------

        results = await resolve_facebook_urls(
            urls
        )

        # ----------------------------------------------------
        # Send ONE result per URL
        # ----------------------------------------------------

        for index, result in enumerate(
            results,
            1,
        ):

            try:

                await incoming.respond(
                    format_result(
                        result,
                        index=index,
                    ),
                    parse_mode="html",
                    link_preview=False,
                )

            except Exception:

                # Fallback without HTML if malformed
                # content causes Telegram parse failure.
                try:

                    fallback = (
                        f"{index}. "
                        f"{display_entity_label(result)}\n"
                        f"STATUS: {result.status}\n"
                        f"USER UID: {result.user_uid or 'N/A'}\n"
                        f"PAGE UID: {result.page_uid or 'N/A'}\n"
                        f"GROUP ID: {result.group_id or 'N/A'}\n"
                        f"POST ID: {result.post_id or 'N/A'}\n"
                        f"VIDEO ID: {result.video_id or 'N/A'}\n"
                        f"CONFIDENCE: {result.confidence}%\n"
                        f"URL: {result.original_url}"
                    )

                    await incoming.respond(
                        fallback,
                        link_preview=False,
                    )

                except Exception:
                    logger.exception(
                        "Could not send GETUIDFB result"
                    )

        # ----------------------------------------------------
        # Continue waiting
        # ----------------------------------------------------

        await incoming.respond(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📎 <b>GETUIDFB ĐANG CHỜ URL TIẾP THEO</b>\n"
            "Gửi link khác để phân tích tiếp.\n"
            "🛑 <code>/stop</code> để thoát.",
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
    IMPORTANT:

    Compatible with:

        module.register(
            bot,
            notify_bot
        )

    Telethon itself supplies `event`.

    There is NO:
        callback(event)

    here.
    """

    bot.add_event_handler(
        _handle_getuidfb,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?$",
        ),
    )

    logger.info(
        "Registered /getuidfb"
    )


# ============================================================
# COMPATIBILITY ALIAS
# ============================================================

handler = _handle_getuidfb