#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
======================================================================
 FACEBOOK UID / ENTITY RESOLVER V16
 TELEGRAMBOT COMMAND MODULE
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

    canonical URL
    OG metadata
    JSON-LD
    HTML / JS ID correlation
    Base64 Facebook encoded ID
    Redirect URL unwrap
    Public URL crawl

IMPORTANT:
    This resolver NEVER treats a GROUP ID as USER UID.

    For:
        /groups/<group>/posts/<post>
        /groups/<group>/permalink/<post>

    the GROUP ID belongs to:
        result.group_id

    The POST ID belongs to:
        result.post_id

    A USER UID is only returned if independent evidence identifies
    an actual USER/PAGE actor.

======================================================================
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

from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional
from urllib.parse import (
    parse_qs,
    quote,
    unquote,
    urljoin,
    urlparse,
    urlunparse,
)

import httpx

from telethon import events


# ======================================================================
# CONFIG
# ======================================================================

VERSION = "V16.0"

DEFAULT_TIMEOUT = 12.0
CONNECT_TIMEOUT = 6.0
READ_TIMEOUT = 12.0

MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 8

MAX_CRAWL_PAGES = 3
MAX_CANDIDATES_PER_FIELD = 20

CONCURRENT_URLS = 5
CONCURRENT_HTTP = 8

SESSION_KEY = "_facebook_uid_v16_sessions"

FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "mobile.facebook.com",
    "web.facebook.com",
    "fb.com",
    "www.fb.com",
}

LOG = logging.getLogger("getuidfb")


# ======================================================================
# HTTP HEADERS
# ======================================================================

HEADERS = {
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


# ======================================================================
# REGEX
# ======================================================================

NUMERIC_ID_RE = re.compile(r"(?<!\d)\d{5,25}(?!\d)")

PFBID_RE = re.compile(
    r"\bpfbid[A-Za-z0-9_-]{8,250}\b",
    re.I,
)

MEDIA_FBID_RE = re.compile(
    r"""
    (?:
        media_fbid
        |
        mediaFBID
        |
        media_fbid:
        |
        media_fbid["']?\s*[:=]\s*["']?
    )
    \s*
    ["']?
    (
        \d{5,30}
        |
        pfbid[A-Za-z0-9_-]{8,250}
    )
    """,
    re.I | re.X,
)

ACTOR_ID_RE = re.compile(
    r"""
    (?:
        actor_id
        |
        actorID
        |
        actor_id["']?\s*[:=]
    )
    \s*
    ["']?
    (\d{5,25})
    """,
    re.I | re.X,
)

PROFILE_ID_RE = re.compile(
    r"""
    (?:
        profile_id
        |
        profileID
        |
        profile_id["']?\s*[:=]
        |
        userID
        |
        user_id
    )
    \s*
    ["']?
    (\d{5,25})
    """,
    re.I | re.X,
)

ENTITY_ID_RE = re.compile(
    r"""
    (?:
        entity_id
        |
        entityID
        |
        object_id
        |
        objectID
    )
    \s*
    ["']?
    [:=]
    \s*
    ["']?
    (\d{5,25})
    """,
    re.I | re.X,
)

GROUP_ID_RE = re.compile(
    r"""
    (?:
        groupID
        |
        group_id
        |
        group_id["']?\s*[:=]
        |
        fb://group/
    )
    \s*
    ["']?
    [:=]?
    \s*
    ["']?
    (\d{5,25})
    """,
    re.I | re.X,
)

PAGE_ID_RE = re.compile(
    r"""
    (?:
        page_id
        |
        pageID
        |
        pageID["']?\s*[:=]
        |
        page_id["']?\s*[:=]
    )
    \s*
    ["']?
    [:=]
    \s*
    ["']?
    (\d{5,25})
    """,
    re.I | re.X,
)

POST_KEY_RE = re.compile(
    r"""
    (?:
        post_id
        |
        postID
        |
        story_fbid
        |
        top_level_post_id
        |
        topLevelPostID
        |
        subscription_target_id
        |
        share_fbid
        |
        mf_story_key
        |
        tl_objid
        |
        throwback_story_fbid
    )
    \s*
    ["']?
    [:=]
    \s*
    ["']?
    (
        \d{5,30}
        |
        pfbid[A-Za-z0-9_-]{8,250}
    )
    """,
    re.I | re.X,
)

CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)',
    re.I,
)

OG_URL_RE = re.compile(
    r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)',
    re.I,
)

OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)',
    re.I,
)

OG_TYPE_RE = re.compile(
    r'<meta[^>]+property=["\']og:type["\'][^>]+content=["\']([^"\']*)',
    re.I,
)

DESCRIPTION_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\']'
    r'[^>]+content=["\']([^"\']*)',
    re.I,
)

USERNAME_PATH_RE = re.compile(
    r"^/([A-Za-z0-9._-]{2,100})(?:/)?$"
)

PROFILE_ID_URL_RE = re.compile(
    r"^/profile\.php$",
    re.I,
)

GROUP_PATH_RE = re.compile(
    r"^/groups/([^/]+)",
    re.I,
)

GROUP_POST_PATH_RE = re.compile(
    r"^/groups/([^/]+)/(?:posts|permalink)/([^/?#]+)",
    re.I,
)

GROUP_POST_PATH_RE_2 = re.compile(
    r"^/groups/([^/]+)/posts/([^/?#]+)",
    re.I,
)

PAGE_POST_PATH_RE = re.compile(
    r"^/([^/]+)/posts/([^/?#]+)",
    re.I,
)

PAGE_PHOTO_PATH_RE = re.compile(
    r"^/([^/]+)/photos/([^/?#]+)",
    re.I,
)

REEL_PATH_RE = re.compile(
    r"^/reel/([^/?#]+)",
    re.I,
)

VIDEO_PATH_RE = re.compile(
    r"^/videos/([^/?#]+)",
    re.I,
)

PHOTO_PATH_RE = re.compile(
    r"^/photo(?:\.php)?/([^/?#]+)",
    re.I,
)

STORY_PATH_RE = re.compile(
    r"^/stories/([^/]+)/([^/?#]+)",
    re.I,
)

SHARE_PATH_RE = re.compile(
    r"^/share/(p|v|r)/([^/?#]+)",
    re.I,
)

POST_PATH_RE = re.compile(
    r"^/([^/]+)/posts/([^/?#]+)",
    re.I,
)

PERMALINK_RE = re.compile(
    r"^/permalink/([^/?#]+)",
    re.I,
)

FB_TOKEN_PREFIXES = (
    "Uzpf",
    "QVFI",
    "AQ",
    "S",
)


# ======================================================================
# DATA MODELS
# ======================================================================

@dataclass
class Evidence:
    field: str
    value: str
    source: str
    weight: float
    context: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecodedToken:
    original: str
    decoded: Optional[str] = None
    object_id: Optional[str] = None
    actor_id: Optional[str] = None
    valid: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class URLContext:
    url: str

    url_type: str = "UNKNOWN"

    is_group: bool = False
    is_page: bool = False
    is_user: bool = False

    is_post: bool = False
    is_reel: bool = False
    is_video: bool = False
    is_photo: bool = False
    is_story: bool = False
    is_share: bool = False

    group_id_from_url: Optional[str] = None
    publisher_username: Optional[str] = None

    post_id_from_url: Optional[str] = None
    reel_id_from_url: Optional[str] = None
    video_id_from_url: Optional[str] = None
    photo_id_from_url: Optional[str] = None
    story_actor_from_url: Optional[str] = None
    story_token_from_url: Optional[str] = None

    share_kind: Optional[str] = None
    share_token: Optional[str] = None

    def entity_context(self) -> str:
        if self.url_type == "GROUP_POST":
            return "GROUP_POST"

        if self.url_type == "PAGE_POST":
            return "PAGE_POST"

        if self.url_type == "USER_POST":
            return "USER_POST"

        if self.url_type == "GROUP":
            return "GROUP"

        if self.url_type == "PAGE":
            return "PAGE"

        if self.url_type == "USER":
            return "USER"

        return self.url_type


@dataclass
class Result:
    requested_url: str

    final_url: Optional[str] = None
    canonical_url: Optional[str] = None

    url_type: str = "UNKNOWN"
    status: str = "NOT_VERIFIED"

    success: bool = False
    confidence: int = 0

    user_uid: Optional[str] = None
    user_verification: str = "NONE"

    page_id: Optional[str] = None
    group_id: Optional[str] = None

    post_id: Optional[str] = None
    reel_id: Optional[str] = None
    video_id: Optional[str] = None
    photo_id: Optional[str] = None
    story_id: Optional[str] = None

    media_fbid: Optional[str] = None
    actor_id: Optional[str] = None
    profile_id: Optional[str] = None
    entity_id: Optional[str] = None

    publisher_username: Optional[str] = None
    title: Optional[str] = None
    og_type: Optional[str] = None

    share_token: Optional[str] = None
    decoded_id: Optional[str] = None
    decoded_actor_id: Optional[str] = None

    conflict: bool = False
    conflicts: list[str] = field(default_factory=list)

    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    pages_crawled: int = 0
    elapsed_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [
            x.to_dict() if isinstance(x, Evidence) else x
            for x in self.evidence
        ]
        return data


# ======================================================================
# BASIC HELPERS
# ======================================================================

def esc(value: Any) -> str:
    if value is None:
        return ""

    return html_lib.escape(str(value), quote=True)


def clean_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    value = html_lib.unescape(value)
    value = re.sub(r"\s+", " ", value)
    value = value.strip()

    return value or None


def normalize_url(url: str) -> str:
    url = html_lib.unescape(url.strip())

    if not url:
        return ""

    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url

    parsed = urlparse(url)

    scheme = "https"

    host = (parsed.hostname or "").lower()

    if host == "fb.com":
        host = "www.facebook.com"

    if host == "www.fb.com":
        host = "www.facebook.com"

    path = parsed.path or "/"

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


def is_facebook_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return (
            host in FACEBOOK_HOSTS
            or host.endswith(".facebook.com")
            or host.endswith(".fb.com")
        )
    except Exception:
        return False


def short_url(url: str, length: int = 90) -> str:
    if len(url) <= length:
        return url

    return url[: length - 3] + "..."


def unique(values: Iterable[str]) -> list[str]:
    result = []
    seen = set()

    for value in values:
        if not value:
            continue

        value = str(value).strip()

        if not value:
            continue

        key = value.lower()

        if key in seen:
            continue

        seen.add(key)
        result.append(value)

    return result


def numeric_id(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    value = str(value).strip()

    if re.fullmatch(r"\d{5,25}", value):
        return value

    return None


def is_numeric_id(value: Optional[str]) -> bool:
    return bool(value and re.fullmatch(r"\d{5,25}", str(value)))


def is_pfbid(value: Optional[str]) -> bool:
    return bool(value and PFBID_RE.fullmatch(str(value)))


def is_probable_id(value: Optional[str]) -> bool:
    if not value:
        return False

    value = str(value).strip()

    return (
        is_numeric_id(value)
        or is_pfbid(value)
    )


# ======================================================================
# URL EXTRACTION FROM TELEGRAM MESSAGE
# ======================================================================

def extract_urls(text: str) -> list[str]:
    if not text:
        return []

    pattern = re.compile(
        r"https?://[^\s<>\[\]()\"']+",
        re.I,
    )

    found = pattern.findall(text)

    cleaned = []

    for url in found:
        url = url.rstrip(".,!?;:，。！？；：")

        if is_facebook_url(url):
            cleaned.append(normalize_url(url))

    return unique(cleaned)


# ======================================================================
# REDIRECT / SHARE UNWRAP
# ======================================================================

def unwrap_redirect_url(url: str) -> str:
    """
    Unwrap common Facebook redirect wrappers without cookies/login.
    """

    current = normalize_url(url)

    for _ in range(5):
        parsed = urlparse(current)
        qs = parse_qs(parsed.query)

        candidates = []

        for key in (
            "u",
            "url",
            "target",
            "redirect",
            "redirect_uri",
            "next",
            "continue",
            "link",
        ):
            candidates.extend(qs.get(key, []))

        changed = False

        for candidate in candidates:
            candidate = unquote(candidate)

            if is_facebook_url(candidate):
                candidate = normalize_url(candidate)

                if candidate != current:
                    current = candidate
                    changed = True
                    break

        if not changed:
            break

    return current


# ======================================================================
# BASE64 FACEBOOK TOKEN DECODER
# ======================================================================

def decode_fb_encoded_id(token: str) -> DecodedToken:
    token = (token or "").strip()

    result = DecodedToken(original=token)

    if not token:
        result.reason = "empty"
        return result

    # pfbid is opaque, NOT a base64 Facebook UID.
    if token.lower().startswith("pfbid"):
        result.reason = "opaque_pfbid"
        return result

    raw = token

    # URL-safe Base64 normalization.
    raw = raw.replace("-", "+").replace("_", "/")

    # Facebook sometimes uses missing padding.
    padding = len(raw) % 4

    if padding:
        raw += "=" * (4 - padding)

    try:
        decoded_bytes = base64.b64decode(
            raw,
            validate=False,
        )
    except (ValueError, binascii.Error):
        result.reason = "base64_decode_failed"
        return result

    try:
        decoded = decoded_bytes.decode(
            "utf-8",
            errors="strict",
        )
    except UnicodeDecodeError:
        result.reason = "not_utf8"
        return result

    decoded = decoded.strip()

    if not decoded:
        result.reason = "empty_payload"
        return result

    result.decoded = decoded

    # Known Facebook structural forms:
    #
    # S:ISC:<object_id>
    # S:<type>:<object_id>
    # <prefix>:<actor>:<object>
    #
    # We intentionally do NOT assume every Base64 payload is a UID.

    parts = decoded.split(":")

    numeric_parts = [
        p for p in parts
        if is_numeric_id(p)
    ]

    if numeric_parts:
        result.object_id = numeric_parts[-1]

    # Common story / object payload:
    #
    # UzpfSVNDOjQ0ODUxMTU5ODE3NDY3NjY=
    # -> S:ISC:4485115981746766
    #
    # This is an object ID, not automatically a USER UID.

    if len(parts) >= 3:
        possible_actor = parts[-2]

        if is_numeric_id(possible_actor):
            result.actor_id = possible_actor

    structural = False

    if decoded.startswith("S:"):
        structural = True

    if decoded.startswith("Uz:"):
        structural = True

    if result.object_id:
        structural = True

    result.valid = structural

    if not structural:
        result.reason = "unknown_payload_structure"
    else:
        result.reason = "valid_structural_token"

    return result


# ======================================================================
# URL CONTEXT PARSER
# ======================================================================

class URLParser:

    def parse(self, url: str) -> URLContext:
        url = normalize_url(url)
        url = unwrap_redirect_url(url)

        context = URLContext(url=url)

        parsed = urlparse(url)

        path = parsed.path or "/"

        path = "/" + path.strip("/")

        if path == "/":
            path = "/"

        qs = parse_qs(parsed.query)

        # --------------------------------------------------------------
        # profile.php?id=<UID>
        # --------------------------------------------------------------

        if PROFILE_ID_URL_RE.match(path):
            profile_id = self._first_numeric(
                qs.get("id", [])
            )

            if profile_id:
                context.url_type = "USER"
                context.is_user = True
                context.post_id_from_url = None

                # Store profile ID through username-independent
                # evidence later.
                context.publisher_username = None

                return context

        # --------------------------------------------------------------
        # /groups/<group>/posts/<post>
        # /groups/<group>/permalink/<post>
        # --------------------------------------------------------------

        m = GROUP_POST_PATH_RE.match(path)

        if not m:
            m = GROUP_POST_PATH_RE_2.match(path)

        if m:
            group_id_or_slug = unquote(m.group(1))
            post_id = unquote(m.group(2))

            context.url_type = "GROUP_POST"
            context.is_group = True
            context.is_post = True

            context.group_id_from_url = (
                numeric_id(group_id_or_slug)
            )

            context.post_id_from_url = post_id

            return context

        # --------------------------------------------------------------
        # /groups/<group>
        # --------------------------------------------------------------

        m = GROUP_PATH_RE.match(path)

        if m:
            group_id_or_slug = unquote(m.group(1))

            context.url_type = "GROUP"
            context.is_group = True

            context.group_id_from_url = (
                numeric_id(group_id_or_slug)
            )

            return context

        # --------------------------------------------------------------
        # /share/p/<token>
        # /share/v/<token>
        # /share/r/<token>
        # --------------------------------------------------------------

        m = SHARE_PATH_RE.match(path)

        if m:
            kind = m.group(1).lower()
            token = unquote(m.group(2))

            context.is_share = True
            context.share_kind = kind
            context.share_token = token

            if kind == "p":
                context.url_type = "SHARE_POST"
                context.is_post = True

            elif kind == "v":
                context.url_type = "SHARE_VIDEO"
                context.is_video = True

            elif kind == "r":
                context.url_type = "SHARE_REEL"
                context.is_reel = True

            return context

        # --------------------------------------------------------------
        # /reel/<id>
        # --------------------------------------------------------------

        m = REEL_PATH_RE.match(path)

        if m:
            context.url_type = "REEL"
            context.is_reel = True
            context.reel_id_from_url = unquote(m.group(1))
            return context

        # --------------------------------------------------------------
        # /videos/<id>
        # --------------------------------------------------------------

        m = VIDEO_PATH_RE.match(path)

        if m:
            context.url_type = "VIDEO"
            context.is_video = True
            context.video_id_from_url = unquote(m.group(1))
            return context

        # --------------------------------------------------------------
        # /photo.php/<id> or /photo/<id>
        # --------------------------------------------------------------

        m = PHOTO_PATH_RE.match(path)

        if m:
            context.url_type = "PHOTO"
            context.is_photo = True
            context.photo_id_from_url = unquote(m.group(1))
            return context

        # --------------------------------------------------------------
        # /stories/<actor>/<story>
        # --------------------------------------------------------------

        m = STORY_PATH_RE.match(path)

        if m:
            context.url_type = "STORY"
            context.is_story = True

            context.story_actor_from_url = numeric_id(
                unquote(m.group(1))
            )

            context.story_token_from_url = unquote(
                m.group(2)
            )

            return context

        # --------------------------------------------------------------
        # /<page>/photos/<photo>
        # --------------------------------------------------------------

        m = PAGE_PHOTO_PATH_RE.match(path)

        if m:
            context.url_type = "PHOTO"
            context.is_photo = True
            context.publisher_username = unquote(m.group(1))
            context.photo_id_from_url = unquote(m.group(2))
            return context

        # --------------------------------------------------------------
        # /<publisher>/posts/<post>
        # --------------------------------------------------------------

        m = POST_PATH_RE.match(path)

        if m:
            publisher = unquote(m.group(1))
            post_id = unquote(m.group(2))

            context.publisher_username = publisher
            context.post_id_from_url = post_id
            context.is_post = True

            # Generic publisher post.
            # We defer PAGE vs USER until evidence tells us.
            context.url_type = "PUBLISHER_POST"

            return context

        # --------------------------------------------------------------
        # /permalink/<id>
        # --------------------------------------------------------------

        m = PERMALINK_RE.match(path)

        if m:
            context.url_type = "POST"
            context.is_post = True
            context.post_id_from_url = unquote(m.group(1))
            return context

        # --------------------------------------------------------------
        # /<username>
        # --------------------------------------------------------------

        m = USERNAME_PATH_RE.match(path)

        if m:
            username = unquote(m.group(1))

            reserved = {
                "home",
                "watch",
                "marketplace",
                "gaming",
                "groups",
                "pages",
                "events",
                "stories",
                "reels",
                "videos",
                "photo",
                "photos",
                "share",
                "profile",
                "login",
                "recover",
                "settings",
                "notifications",
            }

            if username.lower() not in reserved:
                context.publisher_username = username

            # Do not blindly classify username as USER.
            # Facebook username can belong to a PAGE.
            context.url_type = "IDENTITY"

            return context

        return context

    @staticmethod
    def _first_numeric(values: list[str]) -> Optional[str]:
        for value in values:
            value = unquote(str(value))

            if is_numeric_id(value):
                return value

        return None


# ======================================================================
# HTML / METADATA EXTRACTION
# ======================================================================

class EvidenceExtractor:

    def extract(
        self,
        html: str,
        source: str,
        context: URLContext,
    ) -> list[Evidence]:

        evidence: list[Evidence] = []

        if not html:
            return evidence

        # --------------------------------------------------------------
        # Canonical
        # --------------------------------------------------------------

        for match in CANONICAL_RE.finditer(html):
            value = clean_text(match.group(1))

            if value:
                evidence.append(
                    Evidence(
                        field="canonical_url",
                        value=html_lib.unescape(value),
                        source=f"{source}:canonical",
                        weight=28,
                    )
                )

        # --------------------------------------------------------------
        # OpenGraph
        # --------------------------------------------------------------

        for match in OG_URL_RE.finditer(html):
            value = clean_text(match.group(1))

            if value:
                evidence.append(
                    Evidence(
                        field="og_url",
                        value=value,
                        source=f"{source}:og:url",
                        weight=24,
                    )
                )

        for match in OG_TITLE_RE.finditer(html):
            value = clean_text(match.group(1))

            if value:
                evidence.append(
                    Evidence(
                        field="title",
                        value=value,
                        source=f"{source}:og:title",
                        weight=12,
                    )
                )

        for match in OG_TYPE_RE.finditer(html):
            value = clean_text(match.group(1))

            if value:
                evidence.append(
                    Evidence(
                        field="og_type",
                        value=value,
                        source=f"{source}:og:type",
                        weight=10,
                    )
                )

        for match in DESCRIPTION_RE.finditer(html):
            value = clean_text(match.group(1))

            if value:
                evidence.append(
                    Evidence(
                        field="description",
                        value=value,
                        source=f"{source}:description",
                        weight=4,
                    )
                )

        # --------------------------------------------------------------
        # Profile IDs
        # --------------------------------------------------------------

        for match in PROFILE_ID_RE.finditer(html):
            value = match.group(1)

            if is_numeric_id(value):
                evidence.append(
                    Evidence(
                        field="profile_id",
                        value=value,
                        source=f"{source}:profile_id",
                        weight=24,
                    )
                )

        # --------------------------------------------------------------
        # Actor IDs
        # --------------------------------------------------------------

        for match in ACTOR_ID_RE.finditer(html):
            value = match.group(1)

            if is_numeric_id(value):
                evidence.append(
                    Evidence(
                        field="actor_id",
                        value=value,
                        source=f"{source}:actor_id",
                        weight=22,
                    )
                )

        # --------------------------------------------------------------
        # Entity IDs
        # --------------------------------------------------------------

        for match in ENTITY_ID_RE.finditer(html):
            value = match.group(1)

            if is_numeric_id(value):
                evidence.append(
                    Evidence(
                        field="entity_id",
                        value=value,
                        source=f"{source}:entity_id",
                        weight=18,
                    )
                )

        # --------------------------------------------------------------
        # Group IDs
        # --------------------------------------------------------------

        for match in GROUP_ID_RE.finditer(html):
            value = match.group(1)

            if is_numeric_id(value):
                evidence.append(
                    Evidence(
                        field="group_id",
                        value=value,
                        source=f"{source}:group_id",
                        weight=28,
                    )
                )

        # --------------------------------------------------------------
        # Page IDs
        # --------------------------------------------------------------

        for match in PAGE_ID_RE.finditer(html):
            value = match.group(1)

            if is_numeric_id(value):
                evidence.append(
                    Evidence(
                        field="page_id",
                        value=value,
                        source=f"{source}:page_id",
                        weight=26,
                    )
                )

        # --------------------------------------------------------------
        # Post IDs
        # --------------------------------------------------------------

        for match in POST_KEY_RE.finditer(html):
            value = match.group(1)

            if is_probable_id(value):
                evidence.append(
                    Evidence(
                        field="post_id",
                        value=value,
                        source=f"{source}:post_id",
                        weight=20,
                    )
                )

        # --------------------------------------------------------------
        # Media FBID
        # --------------------------------------------------------------

        for match in MEDIA_FBID_RE.finditer(html):
            value = match.group(1)

            if is_probable_id(value):
                evidence.append(
                    Evidence(
                        field="media_fbid",
                        value=value,
                        source=f"{source}:media_fbid",
                        weight=20,
                    )
                )

        # --------------------------------------------------------------
        # pfbid
        # --------------------------------------------------------------

        for match in PFBID_RE.finditer(html):
            value = match.group(0)

            evidence.append(
                Evidence(
                    field="pfbid",
                    value=value,
                    source=f"{source}:pfbid",
                    weight=14,
                )
            )

        # --------------------------------------------------------------
        # Facebook app links
        # --------------------------------------------------------------

        app_patterns = [
            (
                re.compile(
                    r'fb://profile/(\d{5,25})',
                    re.I,
                ),
                "profile_id",
                32,
            ),
            (
                re.compile(
                    r'fb://group/(\d{5,25})',
                    re.I,
                ),
                "group_id",
                34,
            ),
            (
                re.compile(
                    r'fb://page/(\d{5,25})',
                    re.I,
                ),
                "page_id",
                34,
            ),
        ]

        for pattern, field_name, weight in app_patterns:
            for match in pattern.finditer(html):
                value = match.group(1)

                evidence.append(
                    Evidence(
                        field=field_name,
                        value=value,
                        source=f"{source}:app_link",
                        weight=weight,
                    )
                )

        # --------------------------------------------------------------
        # HTML / JS numeric IDs with contextual correlation
        # --------------------------------------------------------------

        evidence.extend(
            self._contextual_numeric_ids(
                html,
                source,
                context,
            )
        )

        # --------------------------------------------------------------
        # JSON-LD
        # --------------------------------------------------------------

        evidence.extend(
            self._json_ld(
                html,
                source,
            )
        )

        # --------------------------------------------------------------
        # Decode Base64 Facebook tokens
        # --------------------------------------------------------------

        evidence.extend(
            self._encoded_tokens(
                html,
                source,
            )
        )

        return self._deduplicate(evidence)

    # ------------------------------------------------------------------
    # Contextual numeric IDs
    # ------------------------------------------------------------------

    def _contextual_numeric_ids(
        self,
        html: str,
        source: str,
        context: URLContext,
    ) -> list[Evidence]:

        result = []

        contexts = [
            (
                r"(?:user|user_id|userID|profile|profile_id)"
                r".{0,100}?(\d{5,25})",
                "profile_id",
                14,
            ),
            (
                r"(?:actor|actor_id|actorID)"
                r".{0,100}?(\d{5,25})",
                "actor_id",
                14,
            ),
            (
                r"(?:group|group_id|groupID)"
                r".{0,100}?(\d{5,25})",
                "group_id",
                15,
            ),
            (
                r"(?:page|page_id|pageID)"
                r".{0,100}?(\d{5,25})",
                "page_id",
                15,
            ),
            (
                r"(?:post|post_id|postID)"
                r".{0,100}?(\d{5,25})",
                "post_id",
                12,
            ),
        ]

        for pattern, field_name, weight in contexts:
            try:
                regex = re.compile(
                    pattern,
                    re.I | re.S,
                )
            except re.error:
                continue

            for match in regex.finditer(html):
                value = match.group(1)

                if not is_numeric_id(value):
                    continue

                result.append(
                    Evidence(
                        field=field_name,
                        value=value,
                        source=f"{source}:contextual_js",
                        weight=weight,
                    )
                )

        return result

    # ------------------------------------------------------------------
    # JSON-LD
    # ------------------------------------------------------------------

    def _json_ld(
        self,
        html: str,
        source: str,
    ) -> list[Evidence]:

        result = []

        pattern = re.compile(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>'
            r'(.*?)'
            r'</script>',
            re.I | re.S,
        )

        for match in pattern.finditer(html):
            raw = html_lib.unescape(match.group(1).strip())

            if not raw:
                continue

            try:
                data = json.loads(raw)
            except Exception:
                continue

            objects = []

            if isinstance(data, dict):
                objects.append(data)

                graph = data.get("@graph")

                if isinstance(graph, list):
                    objects.extend(
                        x for x in graph
                        if isinstance(x, dict)
                    )

            elif isinstance(data, list):
                objects.extend(
                    x for x in data
                    if isinstance(x, dict)
                )

            for obj in objects:
                identifier = obj.get("identifier")

                if identifier is not None:
                    identifier = str(identifier)

                    if is_numeric_id(identifier):
                        result.append(
                            Evidence(
                                field="profile_id",
                                value=identifier,
                                source=f"{source}:jsonld",
                                weight=24,
                            )
                        )

                url_value = obj.get("url")

                if isinstance(url_value, str):
                    parsed = urlparse(url_value)
                    qs = parse_qs(parsed.query)

                    for value in qs.get("id", []):
                        if is_numeric_id(value):
                            result.append(
                                Evidence(
                                    field="profile_id",
                                    value=value,
                                    source=f"{source}:jsonld_url",
                                    weight=20,
                                )
                            )

                author = obj.get("author")

                if isinstance(author, dict):
                    author_id = author.get("identifier")

                    if author_id is not None:
                        author_id = str(author_id)

                        if is_numeric_id(author_id):
                            result.append(
                                Evidence(
                                    field="actor_id",
                                    value=author_id,
                                    source=f"{source}:jsonld_author",
                                    weight=22,
                                )
                            )

                    author_url = author.get("url")

                    if isinstance(author_url, str):
                        parsed = urlparse(author_url)
                        qs = parse_qs(parsed.query)

                        for value in qs.get("id", []):
                            if is_numeric_id(value):
                                result.append(
                                    Evidence(
                                        field="actor_id",
                                        value=value,
                                        source=f"{source}:jsonld_author_url",
                                        weight=20,
                                    )
                                )

        return result

    # ------------------------------------------------------------------
    # Encoded Facebook tokens
    # ------------------------------------------------------------------

    def _encoded_tokens(
        self,
        html: str,
        source: str,
    ) -> list[Evidence]:

        result = []

        candidates = set()

        # Base64-looking strings.
        token_pattern = re.compile(
            r"(?<![A-Za-z0-9_-])"
            r"[A-Za-z0-9_-]{20,160}={0,2}"
            r"(?![A-Za-z0-9_-])"
        )

        for match in token_pattern.finditer(html):
            token = match.group(0)

            if any(token.startswith(prefix) for prefix in FB_TOKEN_PREFIXES):
                candidates.add(token)

        # Explicit encoded values.
        explicit = re.compile(
            r"""
            (?:
                encoded_id
                |
                encoded_token
                |
                story_fbid
                |
                fbid
            )
            \s*
            ["']?
            [:=]
            \s*
            ["']?
            ([A-Za-z0-9_+/=-]{20,180})
            """,
            re.I | re.X,
        )

        for match in explicit.finditer(html):
            candidates.add(match.group(1))

        for token in list(candidates)[:100]:
            decoded = decode_fb_encoded_id(token)

            if not decoded.valid:
                continue

            if decoded.object_id:
                result.append(
                    Evidence(
                        field="decoded_id",
                        value=decoded.object_id,
                        source=f"{source}:base64",
                        weight=18,
                    )
                )

            if decoded.actor_id:
                result.append(
                    Evidence(
                        field="decoded_actor_id",
                        value=decoded.actor_id,
                        source=f"{source}:base64_actor",
                        weight=18,
                    )
                )

        return result

    # ------------------------------------------------------------------
    # Deduplicate
    # ------------------------------------------------------------------

    @staticmethod
    def _deduplicate(
        evidence: list[Evidence],
    ) -> list[Evidence]:

        result = []
        seen = set()

        for item in evidence:
            key = (
                item.field,
                item.value.lower(),
            )

            if key in seen:
                continue

            seen.add(key)
            result.append(item)

        return result


# ======================================================================
# RESOLVER
# ======================================================================

class FacebookResolver:

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_pages: int = MAX_CRAWL_PAGES,
        concurrency: int = CONCURRENT_HTTP,
    ):
        self.timeout = timeout
        self.max_pages = max_pages
        self.concurrency = concurrency

        self._http_sem = asyncio.Semaphore(
            max(1, concurrency)
        )

        self.parser = URLParser()
        self.extractor = EvidenceExtractor()

    # ------------------------------------------------------------------
    # Main
    # ------------------------------------------------------------------

    async def resolve(
        self,
        requested_url: str,
    ) -> Result:

        started = time.monotonic()

        requested_url = normalize_url(
            requested_url
        )

        result = Result(
            requested_url=requested_url,
        )

        if not requested_url:
            result.status = "INVALID_URL"
            result.warnings.append(
                "URL rỗng."
            )
            return result

        if not is_facebook_url(requested_url):
            result.status = "NOT_FACEBOOK"
            result.warnings.append(
                "URL không phải Facebook."
            )
            return result

        requested_url = unwrap_redirect_url(
            requested_url
        )

        context = self.parser.parse(
            requested_url
        )

        result.url_type = context.url_type

        try:
            pages = await self._crawl(
                requested_url
            )

            result.pages_crawled = len(pages)

            all_evidence: list[Evidence] = []

            final_url = requested_url

            for page in pages:
                final_url = page["url"]

                page_context = self.parser.parse(
                    page["url"]
                )

                page_evidence = self.extractor.extract(
                    page["html"],
                    f"page:{len(all_evidence)+1}",
                    page_context,
                )

                all_evidence.extend(
                    page_evidence
                )

                # Canonical from HTTP page.
                canonical = self._extract_canonical(
                    page["html"]
                )

                if canonical:
                    all_evidence.append(
                        Evidence(
                            field="canonical_url",
                            value=canonical,
                            source="canonical",
                            weight=30,
                        )
                    )

            result.final_url = final_url

            # Add URL structural evidence.
            self._add_url_evidence(
                all_evidence,
                requested_url,
                context,
            )

            # Resolve all fields.
            self._resolve_result(
                result,
                context,
                all_evidence,
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            LOG.exception(
                "Facebook resolver failed: %s",
                exc,
            )

            result.status = "ERROR"
            result.warnings.append(
                f"{type(exc).__name__}: {exc}"
            )

        result.elapsed_ms = int(
            (time.monotonic() - started) * 1000
        )

        return result

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    async def _crawl(
        self,
        initial_url: str,
    ) -> list[dict[str, Any]]:

        queue = [
            normalize_url(initial_url)
        ]

        visited = set()
        pages = []

        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(
                timeout=self.timeout,
                connect=CONNECT_TIMEOUT,
                read=READ_TIMEOUT,
                write=self.timeout,
                pool=self.timeout,
            ),
            limits=httpx.Limits(
                max_connections=self.concurrency,
                max_keepalive_connections=self.concurrency,
            ),
            http2=False,
        ) as client:

            while (
                queue
                and len(pages) < self.max_pages
            ):
                current = queue.pop(0)

                current = normalize_url(current)

                if current in visited:
                    continue

                visited.add(current)

                try:
                    page = await self._fetch(
                        client,
                        current,
                    )
                except Exception as exc:
                    LOG.debug(
                        "Fetch failed %s: %s",
                        current,
                        exc,
                    )
                    continue

                if not page:
                    continue

                pages.append(page)

                final_url = page["url"]

                # If Facebook redirected the request,
                # inspect final URL context.
                if (
                    final_url
                    and final_url not in visited
                    and is_facebook_url(final_url)
                ):
                    queue.append(final_url)

                # Canonical can contain the actual public object URL.
                canonical = self._extract_canonical(
                    page["html"]
                )

                if (
                    canonical
                    and is_facebook_url(canonical)
                    and canonical not in visited
                ):
                    queue.append(
                        normalize_url(canonical)
                    )

                # For share URLs, Facebook often redirects to
                # an actual object URL. We intentionally don't crawl
                # arbitrary external links.
                if len(pages) >= self.max_pages:
                    break

        return pages

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> Optional[dict[str, Any]]:

        async with self._http_sem:

            response = await client.get(
                url,
                headers={
                    "Referer": "https://www.facebook.com/",
                },
            )

            content_type = (
                response.headers.get(
                    "content-type",
                    "",
                )
                .lower()
            )

            if (
                "text/html" not in content_type
                and "application/xhtml+xml"
                not in content_type
            ):
                return None

            raw = response.content

            if len(raw) > MAX_HTML_BYTES:
                raw = raw[:MAX_HTML_BYTES]

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

            return {
                "status_code": response.status_code,
                "url": str(response.url),
                "html": text,
                "headers": dict(response.headers),
            }

    # ------------------------------------------------------------------
    # Canonical
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_canonical(
        html: str,
    ) -> Optional[str]:

        match = CANONICAL_RE.search(html)

        if not match:
            match = OG_URL_RE.search(html)

        if not match:
            return None

        value = clean_text(
            match.group(1)
        )

        if not value:
            return None

        return normalize_url(
            value
        )

    # ------------------------------------------------------------------
    # URL evidence
    # ------------------------------------------------------------------

    def _add_url_evidence(
        self,
        evidence: list[Evidence],
        url: str,
        context: URLContext,
    ):

        # --------------------------------------------------------------
        # PROFILE ?id=
        # --------------------------------------------------------------

        parsed = urlparse(url)

        if (
            context.url_type == "USER"
            and parsed.path.lower()
            .rstrip("/")
            .endswith("/profile.php")
        ):
            qs = parse_qs(parsed.query)

            for value in qs.get("id", []):
                if is_numeric_id(value):
                    evidence.append(
                        Evidence(
                            field="profile_id",
                            value=value,
                            source="url:profile.php",
                            weight=45,
                        )
                    )

        # --------------------------------------------------------------
        # GROUP
        # --------------------------------------------------------------

        if context.group_id_from_url:
            evidence.append(
                Evidence(
                    field="group_id",
                    value=context.group_id_from_url,
                    source="url:group",
                    weight=60,
                )
            )

        # --------------------------------------------------------------
        # GROUP POST
        # --------------------------------------------------------------

        if context.url_type == "GROUP_POST":

            if context.post_id_from_url:
                evidence.append(
                    Evidence(
                        field="post_id",
                        value=context.post_id_from_url,
                        source="url:group_post",
                        weight=60,
                        context="GROUP_POST",
                    )
                )

        # --------------------------------------------------------------
        # USER / PAGE publisher
        # --------------------------------------------------------------

        if context.publisher_username:
            evidence.append(
                Evidence(
                    field="publisher_username",
                    value=context.publisher_username,
                    source="url:publisher",
                    weight=48,
                )
            )

        # --------------------------------------------------------------
        # PUBLISHER POST
        # --------------------------------------------------------------

        if (
            context.url_type
            == "PUBLISHER_POST"
            and context.post_id_from_url
        ):
            evidence.append(
                Evidence(
                    field="post_id",
                    value=context.post_id_from_url,
                    source="url:publisher_post",
                    weight=60,
                    context="PUBLISHER_POST",
                )
            )

        # --------------------------------------------------------------
        # Reel
        # --------------------------------------------------------------

        if context.reel_id_from_url:
            evidence.append(
                Evidence(
                    field="reel_id",
                    value=context.reel_id_from_url,
                    source="url:reel",
                    weight=65,
                )
            )

        # --------------------------------------------------------------
        # Video
        # --------------------------------------------------------------

        if context.video_id_from_url:
            evidence.append(
                Evidence(
                    field="video_id",
                    value=context.video_id_from_url,
                    source="url:video",
                    weight=65,
                )
            )

        # --------------------------------------------------------------
        # Photo
        # --------------------------------------------------------------

        if context.photo_id_from_url:
            evidence.append(
                Evidence(
                    field="photo_id",
                    value=context.photo_id_from_url,
                    source="url:photo",
                    weight=65,
                )
            )

        # --------------------------------------------------------------
        # Story
        # --------------------------------------------------------------

        if context.story_actor_from_url:
            evidence.append(
                Evidence(
                    field="actor_id",
                    value=context.story_actor_from_url,
                    source="url:story_actor",
                    weight=70,
                    context="STORY",
                )
            )

        if context.story_token_from_url:
            evidence.append(
                Evidence(
                    field="story_token",
                    value=context.story_token_from_url,
                    source="url:story_token",
                    weight=60,
                    context="STORY",
                )
            )

            decoded = decode_fb_encoded_id(
                context.story_token_from_url
            )

            if decoded.valid:

                if decoded.object_id:
                    evidence.append(
                        Evidence(
                            field="post_id",
                            value=decoded.object_id,
                            source="url:story_base64",
                            weight=65,
                            context="STORY",
                        )
                    )

                if decoded.actor_id:
                    evidence.append(
                        Evidence(
                            field="actor_id",
                            value=decoded.actor_id,
                            source="url:story_base64_actor",
                            weight=65,
                            context="STORY",
                        )
                    )

        # --------------------------------------------------------------
        # Share
        # --------------------------------------------------------------

        if context.share_token:
            evidence.append(
                Evidence(
                    field="share_token",
                    value=context.share_token,
                    source="url:share",
                    weight=65,
                )
            )

            if is_pfbid(context.share_token):
                evidence.append(
                    Evidence(
                        field="post_id",
                        value=context.share_token,
                        source="url:share_pfbid",
                        weight=45,
                    )
                )

    # ------------------------------------------------------------------
    # Resolve result
    # ------------------------------------------------------------------

    def _resolve_result(
        self,
        result: Result,
        context: URLContext,
        evidence: list[Evidence],
    ):

        result.evidence = evidence

        # --------------------------------------------------------------
        # Canonical
        # --------------------------------------------------------------

        canonical_candidates = self._rank(
            evidence,
            "canonical_url",
        )

        if canonical_candidates:
            result.canonical_url = (
                canonical_candidates[0]
            )

        # --------------------------------------------------------------
        # Metadata
        # --------------------------------------------------------------

        titles = self._rank(
            evidence,
            "title",
        )

        if titles:
            result.title = titles[0]

        og_types = self._rank(
            evidence,
            "og_type",
        )

        if og_types:
            result.og_type = og_types[0]

        # --------------------------------------------------------------
        # Entity fields
        # --------------------------------------------------------------

        group_candidates = self._rank(
            evidence,
            "group_id",
        )

        page_candidates = self._rank(
            evidence,
            "page_id",
        )

        profile_candidates = self._rank(
            evidence,
            "profile_id",
        )

        actor_candidates = self._rank(
            evidence,
            "actor_id",
        )

        entity_candidates = self._rank(
            evidence,
            "entity_id",
        )

        post_candidates = self._rank(
            evidence,
            "post_id",
        )

        media_candidates = self._rank(
            evidence,
            "media_fbid",
        )

        reel_candidates = self._rank(
            evidence,
            "reel_id",
        )

        video_candidates = self._rank(
            evidence,
            "video_id",
        )

        photo_candidates = self._rank(
            evidence,
            "photo_id",
        )

        # --------------------------------------------------------------
        # GROUP ID
        # --------------------------------------------------------------

        if group_candidates:
            result.group_id = group_candidates[0]

        # --------------------------------------------------------------
        # PAGE ID
        # --------------------------------------------------------------

        if page_candidates:
            result.page_id = page_candidates[0]

        # --------------------------------------------------------------
        # PROFILE ID
        # --------------------------------------------------------------

        if profile_candidates:
            result.profile_id = profile_candidates[0]

        # --------------------------------------------------------------
        # ACTOR
        # --------------------------------------------------------------

        if actor_candidates:
            result.actor_id = actor_candidates[0]

        # --------------------------------------------------------------
        # POST
        # --------------------------------------------------------------

        if post_candidates:
            result.post_id = post_candidates[0]

        # --------------------------------------------------------------
        # MEDIA
        # --------------------------------------------------------------

        if media_candidates:
            result.media_fbid = media_candidates[0]

        # --------------------------------------------------------------
        # REEL
        # --------------------------------------------------------------

        if reel_candidates:
            result.reel_id = reel_candidates[0]

        # --------------------------------------------------------------
        # VIDEO
        # --------------------------------------------------------------

        if video_candidates:
            result.video_id = video_candidates[0]

        # --------------------------------------------------------------
        # PHOTO
        # --------------------------------------------------------------

        if photo_candidates:
            result.photo_id = photo_candidates[0]

        # --------------------------------------------------------------
        # Publisher
        # --------------------------------------------------------------

        usernames = self._rank(
            evidence,
            "publisher_username",
        )

        if usernames:
            result.publisher_username = (
                usernames[0]
            )

        # --------------------------------------------------------------
        # pfbid
        # --------------------------------------------------------------

        pfbids = self._rank(
            evidence,
            "pfbid",
        )

        if pfbids and not result.post_id:
            result.post_id = pfbids[0]

        # --------------------------------------------------------------
        # Decoded object
        # --------------------------------------------------------------

        decoded_ids = self._rank(
            evidence,
            "decoded_id",
        )

        decoded_actor_ids = self._rank(
            evidence,
            "decoded_actor_id",
        )

        if decoded_ids:
            result.decoded_id = decoded_ids[0]

        if decoded_actor_ids:
            result.decoded_actor_id = (
                decoded_actor_ids[0]
            )

        # --------------------------------------------------------------
        # Share token
        # --------------------------------------------------------------

        share_tokens = self._rank(
            evidence,
            "share_token",
        )

        if share_tokens:
            result.share_token = share_tokens[0]

        # --------------------------------------------------------------
        # Determine URL type from URL + evidence
        # --------------------------------------------------------------

        result.url_type = self._classify(
            context,
            evidence,
        )

        # --------------------------------------------------------------
        # CRITICAL SECURITY / ACCURACY RULE:
        #
        # GROUP and GROUP_POST:
        #
        # Never promote:
        #     group_id
        #
        # to:
        #     user_uid
        #
        # even if group ID has the strongest score.
        # --------------------------------------------------------------

        if result.url_type in {
            "GROUP",
            "GROUP_POST",
        }:

            # USER UID may ONLY come from an actual profile/actor
            # evidence that is independent of the group identity.
            uid = self._verified_user_uid(
                evidence,
                forbidden={
                    result.group_id
                } if result.group_id else set(),
                context=result.url_type,
            )

            if uid:
                result.user_uid = uid
                result.user_verification = "VERIFIED"

            else:
                result.user_uid = None
                result.user_verification = "NOT_APPLICABLE"

                result.warnings.append(
                    "Group ID không được sử dụng làm USER UID."
                )

        # --------------------------------------------------------------
        # PAGE / PAGE POST
        # --------------------------------------------------------------

        elif result.url_type in {
            "PAGE",
            "PAGE_POST",
        }:

            uid = self._verified_page_or_user(
                evidence
            )

            if result.url_type == "PAGE_POST":

                # For PAGE_POST, the page identity belongs in page_id.
                if result.page_id:
                    result.entity_id = result.page_id

                # If no explicit page_id but profile_id/actor_id
                # is strongly supported, it can identify publisher.
                elif uid:
                    result.page_id = uid
                    result.entity_id = uid

            else:
                if result.page_id:
                    result.entity_id = result.page_id

                elif uid:
                    result.page_id = uid
                    result.entity_id = uid

        # --------------------------------------------------------------
        # USER / USER POST
        # --------------------------------------------------------------

        elif result.url_type in {
            "USER",
            "USER_POST",
        }:

            uid = self._verified_user_uid(
                evidence,
                forbidden=set(),
                context=result.url_type,
            )

            if uid:
                result.user_uid = uid
                result.user_verification = "VERIFIED"
                result.entity_id = uid

            else:
                result.user_verification = "NOT_VERIFIED"

        # --------------------------------------------------------------
        # STORY
        # --------------------------------------------------------------

        elif result.url_type == "STORY":

            # Story actor ID is a valid actor candidate,
            # but we only promote it to USER UID if it is supported
            # as a profile/actor rather than simply being a story field.
            uid = self._verified_story_actor(
                evidence
            )

            if uid:
                result.user_uid = uid
                result.user_verification = "VERIFIED"
                result.entity_id = uid
            else:
                result.user_verification = "NOT_VERIFIED"

        # --------------------------------------------------------------
        # Generic media / post
        # --------------------------------------------------------------

        else:

            uid = self._verified_user_uid(
                evidence,
                forbidden={
                    x for x in (
                        result.group_id,
                        result.post_id,
                        result.media_fbid,
                    )
                    if x
                },
                context=result.url_type,
            )

            if uid:
                result.user_uid = uid
                result.user_verification = "VERIFIED"

        # --------------------------------------------------------------
        # Conflict detection
        # --------------------------------------------------------------

        self._detect_conflicts(
            result,
            evidence,
        )

        # --------------------------------------------------------------
        # Confidence
        # --------------------------------------------------------------

        result.confidence = self._confidence(
            result,
            context,
            evidence,
        )

        # --------------------------------------------------------------
        # Status
        # --------------------------------------------------------------

        if result.conflict:
            result.status = "CONFLICT"
            result.success = False

        elif result.user_uid:
            result.status = "VERIFIED"
            result.success = True

        elif result.entity_id:
            result.status = "ENTITY_IDENTIFIED"
            result.success = True

        elif (
            result.post_id
            or result.reel_id
            or result.video_id
            or result.photo_id
            or result.story_id
        ):
            result.status = "OBJECT_IDENTIFIED"
            result.success = True

        else:
            result.status = "NOT_VERIFIED"
            result.success = False

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify(
        self,
        context: URLContext,
        evidence: list[Evidence],
    ) -> str:

        url_type = context.url_type

        # --------------------------------------------------------------
        # Explicit URL structures always win.
        # --------------------------------------------------------------

        if url_type == "GROUP_POST":
            return "GROUP_POST"

        if url_type == "GROUP":
            return "GROUP"

        if url_type == "SHARE_POST":
            return self._classify_share_post(
                evidence
            )

        if url_type == "SHARE_VIDEO":
            return "SHARE_VIDEO"

        if url_type == "SHARE_REEL":
            return "SHARE_REEL"

        if url_type == "REEL":
            return "REEL"

        if url_type == "VIDEO":
            return "VIDEO"

        if url_type == "PHOTO":
            return "PHOTO"

        if url_type == "STORY":
            return "STORY"

        # --------------------------------------------------------------
        # Generic publisher post.
        # --------------------------------------------------------------

        if url_type == "PUBLISHER_POST":

            # If the HTML explicitly identifies a group,
            # this is a group post even if URL was generic.
            groups = self._values(
                evidence,
                "group_id",
            )

            if groups:
                return "GROUP_POST"

            # Explicit page evidence.
            pages = self._values(
                evidence,
                "page_id",
            )

            if pages:
                return "PAGE_POST"

            # Profile/actor evidence.
            profiles = self._values(
                evidence,
                "profile_id",
            )

            actors = self._values(
                evidence,
                "actor_id",
            )

            if profiles or actors:
                return "USER_POST"

            # We know it's a publisher post but don't know
            # whether publisher is USER/PAGE.
            return "POST"

        # --------------------------------------------------------------
        # Identity.
        # --------------------------------------------------------------

        if url_type == "IDENTITY":

            pages = self._values(
                evidence,
                "page_id",
            )

            groups = self._values(
                evidence,
                "group_id",
            )

            profiles = self._values(
                evidence,
                "profile_id",
            )

            if groups:
                return "GROUP"

            if pages:
                return "PAGE"

            if profiles:
                return "USER"

            return "IDENTITY"

        # --------------------------------------------------------------
        # Explicit profile.php
        # --------------------------------------------------------------

        if url_type == "USER":
            return "USER"

        return url_type or "UNKNOWN"

    # ------------------------------------------------------------------
    # Share post
    # ------------------------------------------------------------------

    def _classify_share_post(
        self,
        evidence: list[Evidence],
    ) -> str:

        groups = self._values(
            evidence,
            "group_id",
        )

        pages = self._values(
            evidence,
            "page_id",
        )

        profiles = self._values(
            evidence,
            "profile_id",
        )

        if groups:
            return "GROUP_POST"

        if pages:
            return "PAGE_POST"

        if profiles:
            return "USER_POST"

        return "SHARE_POST"

    # ------------------------------------------------------------------
    # Candidate ranking
    # ------------------------------------------------------------------

    def _rank(
        self,
        evidence: list[Evidence],
        field_name: str,
    ) -> list[str]:

        scores: dict[str, float] = defaultdict(float)
        sources: dict[str, set[str]] = defaultdict(set)

        for item in evidence:

            if item.field != field_name:
                continue

            value = item.value

            if not value:
                continue

            key = value.lower()

            scores[key] += item.weight
            sources[key].add(item.source)

        ranked = sorted(
            scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        return [
            original
            for original in (
                self._restore_value(
                    evidence,
                    field_name,
                    key,
                )
                for key, _ in ranked
            )
            if original
        ]

    @staticmethod
    def _restore_value(
        evidence: list[Evidence],
        field_name: str,
        key: str,
    ) -> Optional[str]:

        for item in evidence:
            if (
                item.field == field_name
                and item.value.lower() == key
            ):
                return item.value

        return None

    @staticmethod
    def _values(
        evidence: list[Evidence],
        field_name: str,
    ) -> list[str]:

        return unique(
            item.value
            for item in evidence
            if item.field == field_name
        )

    # ------------------------------------------------------------------
    # Verified USER UID
    # ------------------------------------------------------------------

    def _verified_user_uid(
        self,
        evidence: list[Evidence],
        forbidden: set[str],
        context: str,
    ) -> Optional[str]:

        candidates = defaultdict(float)
        source_count = defaultdict(set)

        # --------------------------------------------------------------
        # PROFILE IDs
        # --------------------------------------------------------------

        for item in evidence:

            if item.field != "profile_id":
                continue

            value = item.value

            if value in forbidden:
                continue

            if not is_numeric_id(value):
                continue

            candidates[value] += item.weight
            source_count[value].add(
                item.source
            )

        # --------------------------------------------------------------
        # ACTOR IDs
        # --------------------------------------------------------------

        for item in evidence:

            if item.field != "actor_id":
                continue

            value = item.value

            if value in forbidden:
                continue

            if not is_numeric_id(value):
                continue

            # Actor ID alone is weaker than profile ID.
            candidates[value] += (
                item.weight * 0.72
            )

            source_count[value].add(
                item.source
            )

        # --------------------------------------------------------------
        # Story actor.
        # --------------------------------------------------------------

        for item in evidence:

            if (
                item.field == "decoded_actor_id"
                and is_numeric_id(item.value)
            ):

                value = item.value

                if value in forbidden:
                    continue

                candidates[value] += 10
                source_count[value].add(
                    item.source
                )

        if not candidates:
            return None

        ranked = sorted(
            candidates.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        best_id, best_score = ranked[0]

        # Need sufficient evidence.
        #
        # A single weak actor ID should not become USER UID.
        if best_score < 25:
            return None

        # If multiple IDs are close, leave conflict handling to
        # conflict detector rather than guessing.
        if len(ranked) > 1:

            second_score = ranked[1][1]

            if (
                second_score >= best_score * 0.90
                and len(source_count[best_id]) <= 1
            ):
                return None

        return best_id

    # ------------------------------------------------------------------
    # PAGE / USER identity
    # ------------------------------------------------------------------

    def _verified_page_or_user(
        self,
        evidence: list[Evidence],
    ) -> Optional[str]:

        candidates = defaultdict(float)

        for item in evidence:

            if item.field == "page_id":
                candidates[item.value] += (
                    item.weight
                )

        if candidates:
            return max(
                candidates,
                key=candidates.get,
            )

        # fallback only if profile evidence is strong
        profiles = defaultdict(float)

        for item in evidence:

            if item.field == "profile_id":
                profiles[item.value] += (
                    item.weight
                )

        if profiles:
            best = max(
                profiles,
                key=profiles.get,
            )

            if profiles[best] >= 30:
                return best

        return None

    # ------------------------------------------------------------------
    # Story actor
    # ------------------------------------------------------------------

    def _verified_story_actor(
        self,
        evidence: list[Evidence],
    ) -> Optional[str]:

        scores = defaultdict(float)

        for item in evidence:

            if item.field == "actor_id":
                scores[item.value] += (
                    item.weight
                )

            elif item.field == "decoded_actor_id":
                scores[item.value] += 8

            elif item.field == "profile_id":
                scores[item.value] += (
                    item.weight
                )

        if not scores:
            return None

        best = max(
            scores,
            key=scores.get,
        )

        if scores[best] < 25:
            return None

        return best

    # ------------------------------------------------------------------
    # Conflict detection
    # ------------------------------------------------------------------

    def _detect_conflicts(
        self,
        result: Result,
        evidence: list[Evidence],
    ):

        result.conflict = False
        result.conflicts = []

        # --------------------------------------------------------------
        # USER UID conflicts
        # --------------------------------------------------------------

        uid_scores = defaultdict(float)

        for item in evidence:

            if item.field not in {
                "profile_id",
                "actor_id",
                "decoded_actor_id",
            }:
                continue

            if not is_numeric_id(item.value):
                continue

            score = item.weight

            if item.field == "actor_id":
                score *= 0.72

            elif item.field == "decoded_actor_id":
                score *= 0.55

            uid_scores[item.value] += score

        if len(uid_scores) >= 2:

            ranked = sorted(
                uid_scores.items(),
                key=lambda x: x[1],
                reverse=True,
            )

            first_id, first_score = ranked[0]
            second_id, second_score = ranked[1]

            # If two identities have meaningful evidence,
            # don't pretend it's verified.
            if (
                second_score >= 25
                and second_score >= first_score * 0.65
            ):

                result.conflict = True

                result.conflicts.append(
                    "Multiple actor/profile IDs detected: "
                    f"{first_id} vs {second_id}"
                )

        # --------------------------------------------------------------
        # GROUP ID accidentally equals USER UID
        # --------------------------------------------------------------

        if (
            result.group_id
            and result.user_uid
            and result.group_id
            == result.user_uid
        ):

            result.user_uid = None
            result.user_verification = (
                "REJECTED_GROUP_ID"
            )

            result.conflict = True

            result.conflicts.append(
                "Rejected USER UID because it equals GROUP ID."
            )

        # --------------------------------------------------------------
        # pfbid must never become numeric USER UID
        # --------------------------------------------------------------

        if result.user_uid and is_pfbid(
            result.user_uid
        ):

            result.user_uid = None
            result.user_verification = (
                "REJECTED_OPAQUE_TOKEN"
            )

            result.conflict = True

            result.conflicts.append(
                "pfbid is opaque and cannot be USER UID."
            )

    # ------------------------------------------------------------------
    # Confidence
    # ------------------------------------------------------------------

    def _confidence(
        self,
        result: Result,
        context: URLContext,
        evidence: list[Evidence],
    ) -> int:

        score = 0.0

        # --------------------------------------------------------------
        # URL structural evidence
        # --------------------------------------------------------------

        if context.url_type != "UNKNOWN":
            score += 12

        if context.url_type in {
            "GROUP",
            "GROUP_POST",
        }:
            score += 10

        if context.url_type in {
            "USER",
            "USER_POST",
        }:
            score += 8

        if context.url_type in {
            "PAGE",
            "PAGE_POST",
        }:
            score += 8

        # --------------------------------------------------------------
        # Evidence diversity
        # --------------------------------------------------------------

        fields = defaultdict(set)

        for item in evidence:
            fields[item.field].add(
                item.source
            )

        # Profile
        if result.user_uid:

            profile_sources = (
                fields.get("profile_id", set())
            )

            actor_sources = (
                fields.get("actor_id", set())
            )

            if len(profile_sources) >= 2:
                score += 30

            elif profile_sources:
                score += 22

            if len(actor_sources) >= 2:
                score += 18

        # Page
        if result.page_id:
            score += 25

            if len(
                fields.get("page_id", set())
            ) >= 2:
                score += 15

        # Group
        if result.group_id:
            score += 25

            if len(
                fields.get("group_id", set())
            ) >= 2:
                score += 15

        # Post
        if result.post_id:
            score += 20

            if len(
                fields.get("post_id", set())
            ) >= 2:
                score += 15

        # Media
        if (
            result.reel_id
            or result.video_id
            or result.photo_id
            or result.media_fbid
        ):
            score += 12

        # Canonical
        if result.canonical_url:
            score += 10

        # Metadata
        if result.title:
            score += 5

        # JSON-LD
        if any(
            "jsonld" in source
            for sources in fields.values()
            for source in sources
        ):
            score += 10

        # App links
        if any(
            "app_link" in source
            for sources in fields.values()
            for source in sources
        ):
            score += 12

        # Base64 structural decoding
        if result.decoded_id:
            score += 8

        # Conflict penalty
        if result.conflict:
            score -= 40

        # --------------------------------------------------------------
        # Critical rule:
        # GROUP_POST confidence refers to entity/post detection,
        # NOT to a fabricated USER UID.
        # --------------------------------------------------------------

        score = max(
            0,
            min(
                100,
                int(round(score)),
            ),
        )

        return score


# ======================================================================
# TELEGRAM FORMATTING
# ======================================================================

def type_label(result: Result) -> str:

    labels = {
        "USER": "👤 USER",
        "PAGE": "📄 PAGE",
        "GROUP": "👥 GROUP",
        "POST": "📝 POST",
        "USER_POST": "👤📝 USER POST",
        "PAGE_POST": "📄📝 PAGE POST",
        "GROUP_POST": "👥📝 GROUP POST",
        "REEL": "🎬 REEL",
        "VIDEO": "🎥 VIDEO",
        "PHOTO": "🖼️ PHOTO",
        "STORY": "⭕ STORY",
        "SHARE_POST": "🔗 SHARE POST",
        "SHARE_VIDEO": "🔗 SHARE VIDEO",
        "SHARE_REEL": "🔗 SHARE REEL",
        "IDENTITY": "👤 IDENTITY",
        "UNKNOWN": "❓ UNKNOWN",
    }

    return labels.get(
        result.url_type,
        result.url_type,
    )


def confidence_label(
    confidence: int,
) -> str:

    if confidence >= 90:
        return "VERY HIGH"

    if confidence >= 75:
        return "HIGH"

    if confidence >= 55:
        return "MEDIUM"

    if confidence >= 35:
        return "LOW"

    return "VERY LOW"


def format_result(
    result: Result,
    index: Optional[int] = None,
) -> str:

    prefix = (
        f"<b>{index}.</b> "
        if index is not None
        else ""
    )

    lines = [
        "╭──────────────────────────────╮",
        f"│ 🔎 <b>FACEBOOK UID {VERSION}</b>",
        "╰──────────────────────────────╯",
        "",
        f"{prefix}📌 <b>TYPE:</b> "
        f"{esc(type_label(result))}",
        f"📊 <b>STATUS:</b> "
        f"<code>{esc(result.status)}</code>",
        f"🎯 <b>CONFIDENCE:</b> "
        f"<code>{result.confidence}%</code> "
        f"{confidence_label(result.confidence)}",
    ]

    # --------------------------------------------------------------
    # USER UID
    # --------------------------------------------------------------

    if result.user_uid:

        verification = (
            "✅ VERIFIED"
            if result.user_verification
            == "VERIFIED"
            else "⚠️"
        )

        lines.append(
            f"🆔 <b>USER UID:</b> "
            f"<code>{esc(result.user_uid)}</code> "
            f"{verification}"
        )

    else:

        if result.url_type in {
            "GROUP",
            "GROUP_POST",
        }:
            lines.append(
                "🆔 <b>USER UID:</b> "
                "<i>Không lấy Group ID làm UID</i>"
            )

        elif result.conflict:
            lines.append(
                "🆔 <b>USER UID:</b> "
                "⚠️ <b>CONFLICT</b>"
            )

        else:
            lines.append(
                "🆔 <b>USER UID:</b> "
                "<i>Không xác minh được</i>"
            )

    # --------------------------------------------------------------
    # Entity
    # --------------------------------------------------------------

    if result.entity_id:
        lines.append(
            f"🔹 <b>ENTITY ID:</b> "
            f"<code>{esc(result.entity_id)}</code>"
        )

    if result.profile_id:
        lines.append(
            f"👤 <b>PROFILE ID:</b> "
            f"<code>{esc(result.profile_id)}</code>"
        )

    if result.actor_id:
        lines.append(
            f"🎭 <b>ACTOR ID:</b> "
            f"<code>{esc(result.actor_id)}</code>"
        )

    if result.page_id:
        lines.append(
            f"📄 <b>PAGE ID:</b> "
            f"<code>{esc(result.page_id)}</code>"
        )

    if result.group_id:
        lines.append(
            f"👥 <b>GROUP ID:</b> "
            f"<code>{esc(result.group_id)}</code>"
        )

    # --------------------------------------------------------------
    # Object
    # --------------------------------------------------------------

    if result.post_id:
        lines.append(
            f"📝 <b>POST ID:</b> "
            f"<code>{esc(result.post_id)}</code>"
        )

    if result.reel_id:
        lines.append(
            f"🎬 <b>REEL ID:</b> "
            f"<code>{esc(result.reel_id)}</code>"
        )

    if result.video_id:
        lines.append(
            f"🎥 <b>VIDEO ID:</b> "
            f"<code>{esc(result.video_id)}</code>"
        )

    if result.photo_id:
        lines.append(
            f"🖼️ <b>PHOTO ID:</b> "
            f"<code>{esc(result.photo_id)}</code>"
        )

    if result.story_id:
        lines.append(
            f"⭕ <b>STORY ID:</b> "
            f"<code>{esc(result.story_id)}</code>"
        )

    if result.media_fbid:
        lines.append(
            f"🧩 <b>MEDIA FBID:</b> "
            f"<code>{esc(result.media_fbid)}</code>"
        )

    # --------------------------------------------------------------
    # Publisher
    # --------------------------------------------------------------

    if result.publisher_username:
        lines.append(
            f"👤 <b>PUBLISHER:</b> "
            f"<code>{esc(result.publisher_username)}</code>"
        )

    if result.title:
        lines.append(
            f"🏷️ <b>TITLE:</b> "
            f"{esc(result.title[:300])}"
        )

    if result.canonical_url:
        lines.append(
            f"🔗 <b>CANONICAL:</b> "
            f'<a href="{esc(result.canonical_url)}">Open</a>'
        )

    if result.decoded_id:
        lines.append(
            f"🔓 <b>DECODED ID:</b> "
            f"<code>{esc(result.decoded_id)}</code>"
        )

    if result.decoded_actor_id:
        lines.append(
            f"🎭 <b>DECODED ACTOR:</b> "
            f"<code>{esc(result.decoded_actor_id)}</code>"
        )

    if result.share_token:
        lines.append(
            f"🔗 <b>SHARE TOKEN:</b> "
            f"<code>{esc(result.share_token)}</code>"
        )

    # --------------------------------------------------------------
    # Conflict
    # --------------------------------------------------------------

    if result.conflicts:

        lines.append(
            "⚠️ <b>CONFLICT:</b>"
        )

        for item in result.conflicts[:4]:
            lines.append(
                f"• {esc(item)}"
            )

    # --------------------------------------------------------------
    # Warnings
    # --------------------------------------------------------------

    if result.warnings:

        lines.append(
            "⚠️ <b>WARNING:</b>"
        )

        for item in result.warnings[:4]:
            lines.append(
                f"• {esc(item)}"
            )

    lines.extend(
        [
            "",
            f"🌐 <b>PAGES:</b> "
            f"<code>{result.pages_crawled}</code>",
            f"⏱️ <b>TIME:</b> "
            f"<code>{result.elapsed_ms} ms</code>",
            "",
            f'🔗 <a href="{esc(result.requested_url)}">'
            f"Facebook URL</a>",
        ]
    )

    return "\n".join(lines)


def format_results(
    results: list[Result],
) -> str:

    success = sum(
        1
        for result in results
        if result.success
    )

    lines = [
        "╭──────────────────────────────╮",
        f"│ 🔎 <b>FACEBOOK UID {VERSION}</b>",
        "╰──────────────────────────────╯",
        "",
        f"📊 <b>TOTAL:</b> {len(results)}",
        f"✅ <b>SUCCESS:</b> {success}",
        f"❌ <b>FAILED:</b> "
        f"{len(results) - success}",
        "",
    ]

    for index, result in enumerate(
        results,
        1,
    ):

        lines.append(
            format_result(
                result,
                index,
            )
        )

        lines.append(
            "\n━━━━━━━━━━━━━━━━━━━━\n"
        )

    lines.extend(
        [
            "🔄 <b>GET UID READY</b>",
            "📥 Gửi link Facebook tiếp theo.",
            "🛑 <code>/stop</code> để dừng.",
        ]
    )

    return "\n".join(lines)


# ======================================================================
# SESSION
# ======================================================================

def get_sessions(bot) -> dict:
    if not hasattr(
        bot,
        SESSION_KEY,
    ):
        setattr(
            bot,
            SESSION_KEY,
            {},
        )

    return getattr(
        bot,
        SESSION_KEY,
    )


def clear_session(
    bot,
    user_id: int,
):
    sessions = get_sessions(bot)
    sessions.pop(
        user_id,
        None,
    )


# ======================================================================
# OPTIONAL TASK MANAGER COMPATIBILITY
# ======================================================================

async def safe_replace_user_tasks(
    user_id: int,
):
    """
    Compatibility layer.

    If user's bot has:
        core.task_manager.replace_user_tasks

    use it.

    Otherwise do nothing.

    This makes this module compatible with the exact
    structure:

        core/
            users.py
            user_notify.py
            power/

    where task_manager.py may not exist.
    """

    try:

        from core.task_manager import (
            replace_user_tasks,
        )

        result = replace_user_tasks(
            user_id
        )

        if asyncio.iscoroutine(result):
            await result

    except ImportError:
        return

    except Exception as exc:
        LOG.debug(
            "task manager unavailable: %s",
            exc,
        )


async def safe_track_task(
    user_id: int,
):
    try:

        from core.task_manager import (
            track_current_task,
        )

        result = track_current_task(
            user_id
        )

        if asyncio.iscoroutine(result):
            await result

    except ImportError:
        return

    except Exception as exc:
        LOG.debug(
            "task tracking unavailable: %s",
            exc,
        )


# ======================================================================
# ADMIN NOTIFY COMPATIBILITY
# ======================================================================

async def safe_notify(
    notify_bot,
    event,
    result: Result,
):

    if not notify_bot:
        return

    try:

        sender = await event.get_sender()

        admin_result = (
            f"UID: "
            f"{result.user_uid or 'N/A'}\n"
            f"Type: {result.url_type}\n"
            f"Status: {result.status}\n"
            f"Confidence: {result.confidence}%\n"
            f"Entity: {result.entity_id or 'N/A'}\n"
            f"Group: {result.group_id or 'N/A'}\n"
            f"Page: {result.page_id or 'N/A'}\n"
            f"Post: {result.post_id or 'N/A'}\n"
            f"Link: {result.requested_url}"
        )

        if callable(notify_bot):

            value = notify_bot(
                sender,
                "/getuidfb",
                result=admin_result,
            )

            if asyncio.iscoroutine(value):
                await value

            return

        if hasattr(
            notify_bot,
            "send_message",
        ):

            await notify_bot.send_message(
                event.chat_id,
                admin_result,
            )

    except Exception as exc:
        LOG.debug(
            "notify failed: %s",
            exc,
        )


# ======================================================================
# CONCURRENT RESOLUTION
# ======================================================================

async def resolve_many(
    urls: list[str],
    timeout: float = DEFAULT_TIMEOUT,
    max_pages: int = MAX_CRAWL_PAGES,
    concurrency: int = CONCURRENT_URLS,
) -> list[Result]:

    if not urls:
        return []

    semaphore = asyncio.Semaphore(
        max(1, concurrency)
    )

    async def worker(
        url: str,
    ) -> Result:

        async with semaphore:

            resolver = FacebookResolver(
                timeout=timeout,
                max_pages=max_pages,
                concurrency=CONCURRENT_HTTP,
            )

            try:
                return await resolver.resolve(
                    url
                )

            except asyncio.CancelledError:
                raise

            except Exception as exc:

                return Result(
                    requested_url=url,
                    status="ERROR",
                    success=False,
                    warnings=[
                        f"{type(exc).__name__}: {exc}"
                    ],
                )

    return await asyncio.gather(
        *(
            worker(url)
            for url in urls
        )
    )


# ======================================================================
# TELEGRAM REGISTER
# ======================================================================

def register(
    bot,
    notify_bot=None,
):
    """
    REQUIRED API FOR:

        commands/__init__.py

    Example:

        from commands import getuidfb
        getuidfb.register(bot, notify_bot)

    """

    sessions = get_sessions(bot)

    # ==============================================================
    # /getuidfb
    #
    # Supports:
    #
    # /getuidfb
    #
    # /getuidfb https://facebook.com/...
    #
    # /getuidfb https://... https://...
    # ==============================================================

    @bot.on(
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?(?:\s+.*)?$"
        )
    )
    async def getuidfb_command(event):

        user_id = event.sender_id

        text = (
            event.raw_text
            or ""
        ).strip()

        await safe_replace_user_tasks(
            user_id
        )

        # Remove sessions belonging to
        # other interactive commands if they exist.
        for attr in (
            "_dragon_sessions",
            "_dragon_download_sessions",
        ):

            store = getattr(
                bot,
                attr,
                None,
            )

            if isinstance(store, dict):
                store.pop(
                    user_id,
                    None,
                )

        # ----------------------------------------------------------
        # URL directly attached to command
        # ----------------------------------------------------------

        parts = text.split(
            maxsplit=1
        )

        urls = []

        if len(parts) == 2:
            urls = extract_urls(
                parts[1]
            )

        if urls:

            await process_urls(
                bot=bot,
                notify_bot=notify_bot,
                event=event,
                urls=urls,
                sessions=sessions,
            )

            return

        # ----------------------------------------------------------
        # Interactive mode
        # ----------------------------------------------------------

        sessions[user_id] = {
            "command": "getuidfb",
            "running": True,
            "processing": False,
            "started": time.monotonic(),
        }

        await event.reply(
            "╭──────────────────────────────╮\n"
            f"│ 🔎 <b>FACEBOOK UID {VERSION}</b>\n"
            "╰──────────────────────────────╯\n\n"
            "📥 <b>Gửi link Facebook.</b>\n\n"
            "✅ USER / PAGE / GROUP\n"
            "✅ POST / REEL / VIDEO / PHOTO / STORY\n"
            "✅ USER_POST / PAGE_POST / GROUP_POST\n"
            "✅ share/p • share/v • share/r\n"
            "✅ pfbid / media_fbid / actor_id\n"
            "✅ canonical / OG / JSON-LD\n"
            "✅ HTML / JS correlation\n"
            "✅ Facebook Base64 encoded ID\n"
            "✅ Redirect unwrap\n"
            "✅ Public URL crawl\n"
            "✅ Evidence-weighted confidence\n\n"
            "⚠️ <b>GROUP POST:</b> "
            "Group ID sẽ không bao giờ bị dùng làm USER UID.\n\n"
            "💡 Có thể gửi nhiều link cùng lúc.\n"
            "🛑 <code>/stop</code> để dừng.",
            parse_mode="html",
        )

    # ==============================================================
    # RECEIVE URL
    # ==============================================================

    @bot.on(
        events.NewMessage()
    )
    async def getuidfb_receive(event):

        user_id = event.sender_id

        text = (
            event.raw_text
            or ""
        ).strip()

        if not text:
            return

        # Commands are handled elsewhere.
        if text.startswith("/"):
            return

        session = sessions.get(
            user_id
        )

        if not session:
            return

        if (
            session.get("command")
            != "getuidfb"
        ):
            return

        if not session.get(
            "running",
            False,
        ):
            return

        if session.get(
            "processing",
            False,
        ):
            return

        urls = extract_urls(
            text
        )

        if not urls:

            await event.reply(
                "❌ <b>Không tìm thấy Facebook URL.</b>\n\n"
                "Ví dụ:\n"
                "<code>/getuidfb https://www.facebook.com/...</code>",
                parse_mode="html",
            )

            return

        await process_urls(
            bot=bot,
            notify_bot=notify_bot,
            event=event,
            urls=urls,
            sessions=sessions,
        )

    return (
        getuidfb_command,
        getuidfb_receive,
    )


# ======================================================================
# PROCESS TELEGRAM BATCH
# ======================================================================

async def process_urls(
    bot,
    notify_bot,
    event,
    urls: list[str],
    sessions: dict,
):

    user_id = event.sender_id

    session = sessions.get(
        user_id
    )

    # Direct command doesn't necessarily have
    # interactive session.
    if session is None:

        session = {
            "command": "getuidfb",
            "running": True,
            "processing": False,
        }

        sessions[user_id] = session

    if session.get(
        "processing",
        False,
    ):
        return

    session["processing"] = True
    session["running"] = True
    session["command"] = "getuidfb"

    await safe_track_task(
        user_id
    )

    urls = unique(
        normalize_url(url)
        for url in urls
    )

    total = len(urls)

    progress = await event.reply(
        "╭──────────────────────────────╮\n"
        f"│ 🔎 <b>GET UID {VERSION}</b>\n"
        "╰──────────────────────────────╯\n\n"
        f"📊 <b>Đã nhận:</b> {total} link\n"
        "⚡ <b>Mode:</b> HTTP ONLY\n"
        "🌐 <b>Source:</b> PUBLIC CONTENT\n"
        "⏳ Đang phân tích...",
        parse_mode="html",
    )

    try:

        # ----------------------------------------------------------
        # Resolve concurrently.
        #
        # Facebook requests can be slow. Concurrent resolution
        # dramatically reduces total batch latency.
        # ----------------------------------------------------------

        results = await resolve_many(
            urls,
            timeout=DEFAULT_TIMEOUT,
            max_pages=MAX_CRAWL_PAGES,
            concurrency=CONCURRENT_URLS,
        )

        # ----------------------------------------------------------
        # Admin notification concurrently.
        # ----------------------------------------------------------

        if notify_bot:

            await asyncio.gather(
                *(
                    safe_notify(
                        notify_bot,
                        event,
                        result,
                    )
                    for result in results
                ),
                return_exceptions=True,
            )

        # ----------------------------------------------------------
        # Final response.
        # ----------------------------------------------------------

        final_message = format_results(
            results
        )

        try:

            await progress.edit(
                final_message,
                parse_mode="html",
                link_preview=False,
            )

        except Exception:

            await event.reply(
                final_message,
                parse_mode="html",
                link_preview=False,
            )

    except asyncio.CancelledError:

        try:
            await progress.edit(
                "🛑 <b>Đã huỷ Get UID.</b>",
                parse_mode="html",
            )
        except Exception:
            pass

        raise

    except Exception as exc:

        LOG.exception(
            "process_urls failed: %s",
            exc,
        )

        try:

            await progress.edit(
                "❌ <b>Lỗi Get UID</b>\n\n"
                f"<code>{esc(exc)}</code>",
                parse_mode="html",
            )

        except Exception:
            pass

    finally:

        current = sessions.get(
            user_id
        )

        if current:

            current["processing"] = False
            current["running"] = True
            current["command"] = "getuidfb"


# ======================================================================
# /HELP
# ======================================================================

def register_help(
    bot,
):
    """
    Optional helper.

    Only register this if your main bot doesn't already own /help.
    """

    @bot.on(
        events.NewMessage(
            pattern=r"^/help(?:@\w+)?$"
        )
    )
    async def getuidfb_help(event):

        await event.reply(
            "╭──────────────────────────────╮\n"
            f"│ 🔎 <b>FACEBOOK UID {VERSION}</b>\n"
            "╰──────────────────────────────╯\n\n"
            "<b>Cách dùng:</b>\n\n"
            "<code>/getuidfb https://www.facebook.com/...</code>\n\n"
            "Hoặc:\n"
            "<code>/getuidfb</code>\n"
            "sau đó gửi Facebook URL.\n\n"
            "<b>Hỗ trợ:</b>\n"
            "• USER\n"
            "• PAGE\n"
            "• GROUP\n"
            "• POST\n"
            "• USER_POST\n"
            "• PAGE_POST\n"
            "• GROUP_POST\n"
            "• REEL\n"
            "• VIDEO\n"
            "• PHOTO\n"
            "• STORY\n"
            "• share/p\n"
            "• share/v\n"
            "• share/r\n"
            "• pfbid\n"
            "• media_fbid\n"
            "• actor_id\n"
            "• profile_id\n"
            "• entity_id\n\n"
            "<b>Evidence:</b>\n"
            "• URL structure\n"
            "• canonical\n"
            "• OG metadata\n"
            "• JSON-LD\n"
            "• HTML / JS\n"
            "• Facebook app links\n"
            "• Base64 encoded IDs\n"
            "• Redirect URL\n\n"
            "⚠️ GROUP ID không bao giờ được "
            "coi là USER UID.",
            parse_mode="html",
        )

    return getuidfb_help


# ======================================================================
# PUBLIC API
# ======================================================================

async def resolve_facebook_url(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    max_pages: int = MAX_CRAWL_PAGES,
) -> dict[str, Any]:

    resolver = FacebookResolver(
        timeout=timeout,
        max_pages=max_pages,
        concurrency=CONCURRENT_HTTP,
    )

    result = await resolver.resolve(
        url
    )

    return result.to_json()


# ======================================================================
# SELF TEST
# ======================================================================

def self_test() -> bool:

    passed = 0
    total = 0

    def check(
        name: str,
        condition: bool,
    ):

        nonlocal passed, total

        total += 1

        if condition:
            passed += 1

        print(
            f"[{'PASS' if condition else 'FAIL'}] "
            f"{name}"
        )

    parser = URLParser()

    # --------------------------------------------------------------
    # PROFILE
    # --------------------------------------------------------------

    profile = parser.parse(
        "https://www.facebook.com/profile.php?id=100012345678901"
    )

    check(
        "profile.php classification",
        profile.url_type == "USER",
    )

    # --------------------------------------------------------------
    # GROUP POST
    # --------------------------------------------------------------

    group = parser.parse(
        "https://www.facebook.com/groups/"
        "157327848950286/permalink/"
        "1588655489150841/"
    )

    check(
        "group post classification",
        group.url_type == "GROUP_POST",
    )

    check(
        "group ID",
        group.group_id_from_url
        == "157327848950286",
    )

    check(
        "group post ID",
        group.post_id_from_url
        == "1588655489150841",
    )

    # --------------------------------------------------------------
    # USER POST
    # --------------------------------------------------------------

    user_post = parser.parse(
        "https://www.facebook.com/dxt2k4/posts/"
        "123456789012345"
    )

    check(
        "publisher post classification",
        user_post.url_type
        == "PUBLISHER_POST",
    )

    check(
        "publisher username",
        user_post.publisher_username
        == "dxt2k4",
    )

    # --------------------------------------------------------------
    # REEL
    # --------------------------------------------------------------

    reel = parser.parse(
        "https://www.facebook.com/reel/123456789012345"
    )

    check(
        "reel",
        reel.url_type == "REEL",
    )

    # --------------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------------

    video = parser.parse(
        "https://www.facebook.com/videos/123456789012345"
    )

    check(
        "video",
        video.url_type == "VIDEO",
    )

    # --------------------------------------------------------------
    # SHARE
    # --------------------------------------------------------------

    for kind, expected in (
        ("p", "SHARE_POST"),
        ("v", "SHARE_VIDEO"),
        ("r", "SHARE_REEL"),
    ):

        share = parser.parse(
            f"https://www.facebook.com/share/"
            f"{kind}/AbCdEfGh/"
        )

        check(
            f"share/{kind}",
            share.url_type == expected,
        )

    # --------------------------------------------------------------
    # PFBID
    # --------------------------------------------------------------

    pfbid = (
        "pfbid02LUBuwqqHhfzQbRmdK5LUJBT9KhuGHehSwgW3XRWT"
        "BkB46ytBw7kxNX8phG837Gsijl"
    )

    check(
        "pfbid opaque",
        is_pfbid(pfbid),
    )

    decoded_pfbid = decode_fb_encoded_id(
        pfbid
    )

    check(
        "pfbid rejected as base64 UID",
        not decoded_pfbid.valid,
    )

    # --------------------------------------------------------------
    # FACEBOOK BASE64
    # --------------------------------------------------------------

    token = (
        "UzpfSVNDOjQ0ODUxMTU5ODE3NDY3NjY="
    )

    decoded = decode_fb_encoded_id(
        token
    )

    check(
        "base64 Facebook token",
        decoded.valid,
    )

    check(
        "base64 object ID",
        decoded.object_id
        == "4485115981746766",
    )

    # --------------------------------------------------------------
    # GROUP ID NEVER USER UID
    # --------------------------------------------------------------

    resolver = FacebookResolver()

    fake_context = parser.parse(
        "https://www.facebook.com/groups/"
        "157327848950286/permalink/"
        "1588655489150841/"
    )

    evidence = []

    resolver._add_url_evidence(
        evidence,
        fake_context.url,
        fake_context,
    )

    fake_result = Result(
        requested_url=fake_context.url
    )

    resolver._resolve_result(
        fake_result,
        fake_context,
        evidence,
    )

    check(
        "group id not user uid",
        fake_result.user_uid is None,
    )

    check(
        "group id preserved",
        fake_result.group_id
        == "157327848950286",
    )

    check(
        "group post preserved",
        fake_result.post_id
        == "1588655489150841",
    )

    print(
        f"\nSelf-test: {passed}/{total} passed."
    )

    return passed == total


# ======================================================================
# MAIN
# ======================================================================

if __name__ == "__main__":

    print(
        f"Facebook UID / Entity Resolver {VERSION}"
    )

    print(
        "HTTP ONLY / PUBLIC CONTENT ONLY"
    )

    print(
        "No Playwright / Selenium / Cookie / "
        "Access Token / Login"
    )

    print(
        "\nRunning self-test...\n"
    )

    raise SystemExit(
        0
        if self_test()
        else 1
    )