#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
============================================================
 FACEBOOK UID / ENTITY RESOLVER V17 PRECISION
============================================================

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
- Concurrent requests
- /getuidfb <facebook_url>

CORE:
- USER / PAGE / GROUP separation
- USER_POST / PAGE_POST / GROUP_POST
- POST / REEL / VIDEO / PHOTO / STORY
- share/p, share/v, share/r
- pfbid
- media_fbid
- actor_id
- profile_id
- entity_id
- owner_id
- publisher_id
- canonical URL
- OG metadata
- JSON-LD
- HTML / JS correlation
- Base64 / URL-safe Base64 inspection
- Facebook redirect unwrap
- Public URL crawl
- Identity locking
- Evidence correlation
- Conflict detection
- Precision-first scoring

IMPORTANT:
This resolver NEVER guarantees a numeric UID merely because a
number was found in HTML. A numeric value must be correlated with
the correct entity/context before it becomes USER_UID / PAGE_ID /
GROUP_ID.
============================================================
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
    quote,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from telethon import events


# ============================================================
# COMMAND INFO
# ============================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": "Facebook public UID / entity resolver",
    "usage": "/getuidfb <facebook_url>",
    "aliases": ["fbuid", "uidfb"],
}


GETUIDFB_HELP = """
<b>🔎 FACEBOOK UID RESOLVER V17</b>

<b>Cú pháp:</b>
<code>/getuidfb &lt;facebook_url&gt;</code>

Hỗ trợ:
• USER / USER POST
• PAGE / PAGE POST
• GROUP / GROUP POST
• POST / REEL / VIDEO / PHOTO / STORY
• pfbid
• media_fbid
• share/p
• share/v
• share/r
• canonical / OG / JSON-LD
• HTML / JS correlation
• Facebook redirect

<b>Precision-first:</b>
chỉ trả ID khi có bằng chứng đủ mạnh.
"""


# ============================================================
# LOGGING
# ============================================================

LOGGER = logging.getLogger("facebook_uid_v17")


# ============================================================
# CONSTANTS
# ============================================================

FB_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "mobile.facebook.com",
    "web.facebook.com",
    "fb.com",
    "www.fb.com",
    "fb.watch",
    "l.facebook.com",
}

MAX_BODY = 5_500_000
MAX_CRAWL = 4
MAX_DISCOVERED_LINKS = 3
MAX_CONCURRENT = 6

MIN_NUMERIC_ID_LEN = 5
MAX_NUMERIC_ID_LEN = 30

PF_TOKEN_RE = re.compile(
    r"\bpfbid[A-Za-z0-9_-]+\b",
    re.I,
)

NUMERIC_RE = re.compile(
    rf"(?<!\d)(\d{{{MIN_NUMERIC_ID_LEN},{MAX_NUMERIC_ID_LEN}}})(?!\d)"
)

USERNAME_RE = re.compile(
    r"^[A-Za-z0-9._-]{2,100}$"
)

FACEBOOK_URL_RE = re.compile(
    r"""(?ix)
    https?://
    (?:
        [a-z0-9.-]+\.)?
        facebook\.com
        [^\s<>"']*
    |
        https?://(?:www\.)?fb\.com[^\s<>"']*
    |
        https?://fb\.watch/[^\s<>"']*
    """
)

META_RE = re.compile(
    r"""(?is)
    <meta
        [^>]+
        (?:property|name)\s*=\s*["']([^"']+)["']
        [^>]+
        content\s*=\s*["'](.*?)["']
        [^>]*>
    |
    <meta
        [^>]+
        content\s*=\s*["'](.*?)["']
        [^>]+
        (?:property|name)\s*=\s*["']([^"']+)["']
        [^>]*>
    """
)

LINK_RE = re.compile(
    r"""(?is)
    <link
        [^>]*rel\s*=\s*["']([^"']+)["']
        [^>]*href\s*=\s*["'](.*?)["']
        [^>]*>
    |
    <link
        [^>]*href\s*=\s*["'](.*?)["']
        [^>]*rel\s*=\s*["']([^"']+)["']
        [^>]*>
    """
)

SCRIPT_RE = re.compile(
    r"(?is)<script\b[^>]*>(.*?)</script>"
)

TITLE_RE = re.compile(
    r"(?is)<title[^>]*>(.*?)</title>"
)

# Common Facebook identity keys.
IDENTITY_KEYS = (
    "user_id",
    "profile_id",
    "actor_id",
    "page_id",
    "group_id",
    "owner_id",
    "publisher_id",
    "entity_id",
    "story_fbid",
    "post_id",
    "media_fbid",
    "video_id",
    "photo_id",
    "reel_id",
    "id",
)

# Values which frequently contain non-identity numbers.
BAD_NUMERIC_CONTEXT = (
    "timestamp",
    "created_time",
    "updated_time",
    "width",
    "height",
    "duration",
    "offset",
    "count",
    "limit",
    "size",
    "version",
    "revision",
    "tracking",
    "cache",
    "expires",
)


# ============================================================
# ENTITY TYPES
# ============================================================

GROUP_TYPES = {
    "GROUP",
    "GROUP_POST",
}

PAGE_TYPES = {
    "PAGE",
    "PAGE_POST",
    "PAGE_VIDEO",
    "PAGE_PHOTO",
    "PAGE_REEL",
}

USER_TYPES = {
    "USER",
    "USER_POST",
    "USER_VIDEO",
    "USER_PHOTO",
    "USER_REEL",
}

CONTENT_TYPES = {
    "POST",
    "REEL",
    "VIDEO",
    "PHOTO",
    "STORY",
}


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass(slots=True)
class Evidence:
    field: str
    value: str
    source: str
    score: float
    context: str = ""
    direct: bool = False
    kind: str = "generic"
    role: str = ""
    independent_key: str = ""

    def key(self) -> tuple:
        return (
            self.field,
            self.value,
            self.source,
            self.role,
        )


@dataclass(slots=True)
class PageSnapshot:
    url: str
    final_url: str
    status_code: int
    content_type: str
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0


@dataclass(slots=True)
class Candidate:
    field: str
    value: str
    score: float = 0.0
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def evidence_count(self) -> int:
        return len(self.evidence)

    @property
    def independent_sources(self) -> int:
        return len({
            e.independent_key or e.source
            for e in self.evidence
        })


@dataclass(slots=True)
class ResolveResult:
    input_url: str

    status: str = "FAILED"
    url_type: str = "UNKNOWN"
    entity_type: str = "UNKNOWN"

    confidence: int = 0
    confidence_label: str = "LOW"

    canonical_url: Optional[str] = None
    resolved_url: Optional[str] = None

    user_uid: Optional[str] = None
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
    publisher_id: Optional[str] = None

    publisher_username: Optional[str] = None
    share_token: Optional[str] = None

    verification: str = "UNVERIFIED"

    warnings: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    error: Optional[str] = None


# ============================================================
# HELPERS
# ============================================================

def clean_text(value: Any) -> str:
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\\/", "/")
    value = value.replace("\\u002F", "/")
    value = value.replace("\\u003A", ":")
    value = value.replace("\\u0026", "&")

    return re.sub(r"\s+", " ", value).strip()


def valid_numeric_id(value: Any) -> bool:
    if value is None:
        return False

    value = str(value).strip()

    if not value.isdigit():
        return False

    if not (
        MIN_NUMERIC_ID_LEN
        <= len(value)
        <= MAX_NUMERIC_ID_LEN
    ):
        return False

    # Reject obvious tiny test values.
    if len(set(value)) == 1:
        return False

    return True


def valid_opaque_id(value: Any) -> bool:
    if value is None:
        return False

    value = str(value).strip()

    return bool(
        3 <= len(value) <= 300
        and re.fullmatch(r"[A-Za-z0-9_.:-]+", value)
    )


def normalize_host(host: str) -> str:
    return (host or "").lower().split(":")[0]


def is_facebook_host(host: str) -> bool:
    host = normalize_host(host)

    if host in FB_HOSTS:
        return True

    return any(
        host.endswith("." + base)
        for base in (
            "facebook.com",
            "fb.com",
        )
    )


def normalize_url(url: str) -> str:
    url = clean_text(url)

    if not url:
        return ""

    parsed = urlparse(url)

    if not parsed.scheme:
        parsed = urlparse("https://" + url)

    if not is_facebook_host(parsed.netloc):
        return ""

    query = parse_qs(
        parsed.query,
        keep_blank_values=False,
    )

    # Keep only semantically useful Facebook parameters.
    keep = {}

    for key, values in query.items():
        key_l = key.lower()

        if key_l in {
            "id",
            "fbid",
            "story_fbid",
            "v",
            "u",
            "set",
            "substory_index",
        }:
            if values:
                keep[key] = values[-1]

    clean_query = urlencode(keep)

    path = re.sub(
        r"/{2,}",
        "/",
        parsed.path or "/",
    )

    return urlunparse(
        (
            "https",
            normalize_host(parsed.netloc),
            path.rstrip("/") or "/",
            "",
            clean_query,
            "",
        )
    )


def unwrap_redirect(url: str) -> str:
    """
    Unwrap l.facebook.com/l.php?u=...
    and common Facebook redirect wrappers.
    """
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        for key in ("u", "url", "target", "redirect_uri"):
            values = query.get(key)

            if not values:
                continue

            candidate = unquote(values[-1])

            if candidate.startswith(("http://", "https://")):
                if is_facebook_host(urlparse(candidate).netloc):
                    return normalize_url(candidate)

        return normalize_url(url)

    except Exception:
        return normalize_url(url)


def extract_facebook_urls(text: str) -> list[str]:
    if not text:
        return []

    found = []

    for match in FACEBOOK_URL_RE.finditer(text):
        url = match.group(0).rstrip(
            ".,!?;:)]}>\"'"
        )

        url = unwrap_redirect(url)

        if url and url not in found:
            found.append(url)

    return found


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def confidence_label(score: int) -> str:
    if score >= 95:
        return "VERY HIGH"

    if score >= 85:
        return "HIGH"

    if score >= 70:
        return "GOOD"

    if score >= 50:
        return "MEDIUM"

    return "LOW"


# ============================================================
# URL CLASSIFIER
# ============================================================

class FacebookURLClassifier:
    """
    Structural URL classification.

    IMPORTANT:
    Group context has absolute precedence.
    """

    @staticmethod
    def classify(url: str) -> tuple[str, dict[str, str]]:
        parsed = urlparse(url)
        path = unquote(parsed.path or "")
        lower = path.lower()

        query = parse_qs(parsed.query)

        info: dict[str, str] = {}

        # ----------------------------------------------------
        # GROUP
        # ----------------------------------------------------

        m = re.search(
            r"/groups/([^/]+)/(?:posts|permalink|user_posts)/([^/?#]+)",
            lower,
            re.I,
        )

        if m:
            group_token = unquote(m.group(1))
            post_token = unquote(m.group(2))

            if valid_numeric_id(group_token):
                info["group_id"] = group_token

            info["post_id"] = post_token

            return "GROUP_POST", info

        m = re.search(
            r"/groups/([^/?#]+)",
            lower,
            re.I,
        )

        if m:
            group_token = unquote(m.group(1))

            if valid_numeric_id(group_token):
                info["group_id"] = group_token

            return "GROUP", info

        # ----------------------------------------------------
        # STORIES
        # ----------------------------------------------------

        if "/story.php" in lower:
            story_fbid = (
                query.get("story_fbid", [None])[-1]
            )

            owner_id = (
                query.get("id", [None])[-1]
            )

            if story_fbid:
                info["story_id"] = story_fbid

            if valid_numeric_id(owner_id):
                info["owner_id"] = owner_id

            return "STORY", info

        # ----------------------------------------------------
        # SHARE
        # ----------------------------------------------------

        m = re.search(
            r"/share/(p|v|r)/([^/?#]+)",
            lower,
            re.I,
        )

        if m:
            kind = m.group(1).lower()
            token = unquote(m.group(2))

            info["share_token"] = token

            if kind == "p":
                if token.lower().startswith("pfbid"):
                    info["post_id"] = token

                return "SHARE_POST", info

            if kind == "v":
                return "SHARE_VIDEO", info

            if kind == "r":
                return "SHARE_REEL", info

        # ----------------------------------------------------
        # PROFILE.PHP
        # ----------------------------------------------------

        if lower.startswith("/profile.php"):
            profile_id = query.get("id", [None])[-1]

            if valid_numeric_id(profile_id):
                info["profile_id"] = profile_id

            return "PROFILE", info

        # ----------------------------------------------------
        # PAGES
        # ----------------------------------------------------

        if lower.startswith("/pages/"):
            parts = [
                unquote(x)
                for x in path.split("/")
                if x
            ]

            if len(parts) >= 2:
                page_name = parts[1]

                info["publisher_username"] = page_name

            if "/posts/" in lower:
                post = path.split("/posts/", 1)[1]
                post = post.split("/", 1)[0]

                if post:
                    info["post_id"] = unquote(post)

                return "PAGE_POST", info

            if "/videos/" in lower:
                video = path.split("/videos/", 1)[1]
                video = video.split("/", 1)[0]

                if video:
                    info["video_id"] = unquote(video)

                return "PAGE_VIDEO", info

            if "/reels/" in lower:
                reel = path.split("/reels/", 1)[1]
                reel = reel.split("/", 1)[0]

                if reel:
                    info["reel_id"] = unquote(reel)

                return "PAGE_REEL", info

            return "PAGE", info

        # ----------------------------------------------------
        # REELS
        # ----------------------------------------------------

        m = re.search(
            r"/(?:reel|reels)/([^/?#]+)",
            lower,
            re.I,
        )

        if m:
            info["reel_id"] = unquote(m.group(1))
            return "REEL", info

        # ----------------------------------------------------
        # VIDEOS
        # ----------------------------------------------------

        m = re.search(
            r"/videos/(?:[^/]+/)?([^/?#]+)",
            lower,
            re.I,
        )

        if m:
            info["video_id"] = unquote(m.group(1))
            return "VIDEO", info

        if lower.startswith("/watch"):
            video_id = query.get("v", [None])[-1]

            if video_id:
                info["video_id"] = video_id

            return "VIDEO", info

        # ----------------------------------------------------
        # PHOTO
        # ----------------------------------------------------

        if lower.startswith("/photo.php"):
            photo_id = query.get("fbid", [None])[-1]

            if photo_id:
                info["photo_id"] = photo_id

            return "PHOTO", info

        # ----------------------------------------------------
        # /p/
        # ----------------------------------------------------

        m = re.search(
            r"/p/([^/?#]+)",
            lower,
            re.I,
        )

        if m:
            token = unquote(m.group(1))

            info["post_id"] = token

            return "POST", info

        # ----------------------------------------------------
        # USER / PAGE STYLE POST
        # ----------------------------------------------------

        parts = [
            unquote(x)
            for x in path.split("/")
            if x
        ]

        if parts:
            first = parts[0]

            if (
                first.lower()
                not in {
                    "home",
                    "watch",
                    "marketplace",
                    "gaming",
                    "events",
                    "groups",
                    "pages",
                    "reels",
                    "videos",
                    "photo.php",
                    "story.php",
                    "profile.php",
                    "share",
                }
            ):
                if len(parts) >= 3:
                    action = parts[1].lower()
                    token = parts[2]

                    if action == "posts":
                        info["publisher_username"] = first
                        info["post_id"] = token
                        return "AMBIGUOUS_POST", info

                    if action == "videos":
                        info["publisher_username"] = first
                        info["video_id"] = token
                        return "AMBIGUOUS_VIDEO", info

                    if action == "photos":
                        info["publisher_username"] = first
                        info["photo_id"] = token
                        return "AMBIGUOUS_PHOTO", info

                    if action in {"reels", "reel"}:
                        info["publisher_username"] = first
                        info["reel_id"] = token
                        return "AMBIGUOUS_REEL", info

                if len(parts) == 1:
                    info["publisher_username"] = first
                    return "PROFILE_CANDIDATE", info

        return "UNKNOWN", info


# ============================================================
# EVIDENCE STORE
# ============================================================

class EvidenceStore:
    def __init__(self) -> None:
        self.items: list[Evidence] = []
        self._keys: set[tuple] = set()

    def add(
        self,
        field: str,
        value: Any,
        source: str,
        score: float,
        *,
        context: str = "",
        direct: bool = False,
        kind: str = "generic",
        role: str = "",
        independent_key: str = "",
    ) -> None:
        if value is None:
            return

        value = clean_text(value)

        if not value:
            return

        item = Evidence(
            field=field,
            value=value,
            source=source,
            score=score,
            context=context,
            direct=direct,
            kind=kind,
            role=role,
            independent_key=(
                independent_key
                or source
            ),
        )

        key = item.key()

        if key in self._keys:
            return

        self._keys.add(key)
        self.items.append(item)

    def for_field(self, field: str) -> list[Evidence]:
        return [
            x for x in self.items
            if x.field == field
        ]

    def for_value(self, field: str, value: str) -> list[Evidence]:
        return [
            x
            for x in self.items
            if x.field == field
            and x.value == value
        ]

    def candidates(self, field: str) -> list[Candidate]:
        values: dict[str, Candidate] = {}

        for evidence in self.for_field(field):
            candidate = values.setdefault(
                evidence.value,
                Candidate(
                    field=field,
                    value=evidence.value,
                ),
            )

            candidate.evidence.append(evidence)

        for candidate in values.values():
            candidate.score = self._score(candidate)

        return sorted(
            values.values(),
            key=lambda x: x.score,
            reverse=True,
        )

    @staticmethod
    def _score(candidate: Candidate) -> float:
        if not candidate.evidence:
            return 0.0

        score = 0.0

        # Base evidence scores.
        score += sum(
            min(35.0, e.score)
            for e in candidate.evidence
        )

        # Independent-source bonus.
        independent = candidate.independent_sources

        if independent >= 2:
            score += 18

        if independent >= 3:
            score += 15

        if independent >= 4:
            score += 10

        # Direct evidence bonus.
        if any(e.direct for e in candidate.evidence):
            score += 12

        # Multiple evidence of same value.
        if len(candidate.evidence) >= 2:
            score += 8

        return min(100.0, score)


# ============================================================
# HTTP FETCHER
# ============================================================

class HTTPFetcher:
    USER_AGENTS = (
        "Mozilla/5.0 (Linux; Android 13; Mobile) "
        "AppleWebKit/537.36 Chrome/140 Mobile Safari/537.36",

        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 Chrome/140 Safari/537.36",

        "facebookexternalhit/1.1",
    )

    def __init__(
        self,
        *,
        concurrency: int = MAX_CONCURRENT,
    ) -> None:

        if httpx is None:
            raise RuntimeError(
                "Thiếu thư viện httpx. "
                "Cài bằng: pip install httpx"
            )

        self.sem = asyncio.Semaphore(concurrency)

        self.timeout = httpx.Timeout(
            connect=8.0,
            read=14.0,
            write=8.0,
            pool=8.0,
        )

    async def fetch(
        self,
        client: "httpx.AsyncClient",
        url: str,
    ) -> Optional[PageSnapshot]:

        async with self.sem:
            started = time.perf_counter()

            last_error = None

            for attempt in range(2):
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
                    response = await client.get(
                        url,
                        headers=headers,
                        follow_redirects=True,
                    )

                    content_type = (
                        response.headers.get(
                            "content-type",
                            "",
                        )
                    ).lower()

                    if (
                        "text/html" not in content_type
                        and "application/xhtml" not in content_type
                        and not content_type.startswith("text/")
                    ):
                        return PageSnapshot(
                            url=url,
                            final_url=str(response.url),
                            status_code=response.status_code,
                            content_type=content_type,
                            text="",
                            headers=dict(response.headers),
                            elapsed_ms=int(
                                (time.perf_counter() - started)
                                * 1000
                            ),
                        )

                    raw = response.content[:MAX_BODY]

                    text = raw.decode(
                        response.encoding or "utf-8",
                        errors="replace",
                    )

                    return PageSnapshot(
                        url=url,
                        final_url=str(response.url),
                        status_code=response.status_code,
                        content_type=content_type,
                        text=text,
                        headers=dict(response.headers),
                        elapsed_ms=int(
                            (time.perf_counter() - started)
                            * 1000
                        ),
                    )

                except Exception as exc:
                    last_error = exc

                    if attempt == 0:
                        await asyncio.sleep(0.25)

            LOGGER.debug(
                "HTTP failed %s: %r",
                url,
                last_error,
            )

        return None


# ============================================================
# HTML / JSON / JS EXTRACTOR
# ============================================================

class FacebookExtractor:
    def __init__(
        self,
        *,
        url_type: str,
        evidence: EvidenceStore,
    ) -> None:

        self.url_type = url_type
        self.evidence = evidence

    # --------------------------------------------------------
    # META
    # --------------------------------------------------------

    def extract_meta(self, text: str) -> dict[str, str]:
        meta: dict[str, str] = {}

        for match in META_RE.finditer(text):
            a, b, c, d = match.groups()

            if a and b:
                key = clean_text(a).lower()
                value = clean_text(b)
            else:
                key = clean_text(d).lower()
                value = clean_text(c)

            if not key or not value:
                continue

            meta[key] = value

            if key in {
                "og:url",
                "og:title",
                "og:description",
                "og:type",
                "al:ios:url",
                "al:android:url",
                "al:web:url",
            }:
                self.evidence.add(
                    field=key,
                    value=value,
                    source="meta",
                    score=7,
                    kind="metadata",
                    independent_key="meta",
                )

        return meta

    # --------------------------------------------------------
    # LINKS
    # --------------------------------------------------------

    def extract_links(self, text: str) -> list[str]:
        links = []

        for match in LINK_RE.finditer(text):
            a, b, c, d = match.groups()

            if a and b:
                rel = clean_text(a).lower()
                href = clean_text(b)
            else:
                href = clean_text(c)
                rel = clean_text(d).lower()

            if "canonical" not in rel:
                continue

            href = unquote(href)

            if href.startswith(("http://", "https://")):
                href = normalize_url(href)

                if href:
                    links.append(href)

                    self.evidence.add(
                        field="canonical_url",
                        value=href,
                        source="canonical",
                        score=28,
                        direct=True,
                        kind="url",
                        independent_key="canonical",
                    )

        return list(dict.fromkeys(links))

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    def extract_title(self, text: str) -> str:
        m = TITLE_RE.search(text)

        if not m:
            return ""

        title = clean_text(m.group(1))

        if title:
            self.evidence.add(
                field="title",
                value=title,
                source="html_title",
                score=4,
                kind="metadata",
                independent_key="title",
            )

        return title

    # --------------------------------------------------------
    # JSON-LD
    # --------------------------------------------------------

    def extract_jsonld(self, text: str) -> None:
        for script in SCRIPT_RE.findall(text):
            if not re.search(
                r"application/ld\+json",
                script,
                re.I,
            ):
                continue

            body = clean_text(script)

            if not body:
                continue

            try:
                data = json.loads(body)
            except Exception:
                continue

            self._walk_json(
                data,
                source="jsonld",
                depth=0,
            )

    # --------------------------------------------------------
    # GENERIC JSON WALK
    # --------------------------------------------------------

    def _walk_json(
        self,
        value: Any,
        *,
        source: str,
        depth: int,
        parent_key: str = "",
    ) -> None:

        if depth > 15:
            return

        if isinstance(value, dict):
            for key, child in value.items():
                key_l = str(key).lower()

                if isinstance(child, (
                    str,
                    int,
                    float,
                )):
                    self._inspect_key_value(
                        key_l,
                        child,
                        source,
                    )

                elif isinstance(child, (
                    dict,
                    list,
                )):
                    self._walk_json(
                        child,
                        source=source,
                        depth=depth + 1,
                        parent_key=key_l,
                    )

        elif isinstance(value, list):
            for child in value[:1000]:
                self._walk_json(
                    child,
                    source=source,
                    depth=depth + 1,
                    parent_key=parent_key,
                )

    # --------------------------------------------------------
    # KEY / VALUE
    # --------------------------------------------------------

    def _inspect_key_value(
        self,
        key: str,
        value: Any,
        source: str,
    ) -> None:

        value = clean_text(value)

        if not value:
            return

        field_map = {
            "user_id": "user_uid",
            "userid": "user_uid",
            "profile_id": "profile_id",
            "profileid": "profile_id",
            "actor_id": "actor_id",
            "actorid": "actor_id",
            "page_id": "page_id",
            "pageid": "page_id",
            "group_id": "group_id",
            "groupid": "group_id",
            "owner_id": "owner_id",
            "ownerid": "owner_id",
            "publisher_id": "publisher_id",
            "publisherid": "publisher_id",
            "entity_id": "entity_id",
            "entityid": "entity_id",
            "story_fbid": "story_id",
            "storyfbid": "story_id",
            "post_id": "post_id",
            "postid": "post_id",
            "media_fbid": "media_fbid",
            "mediafbid": "media_fbid",
            "video_id": "video_id",
            "videoid": "video_id",
            "photo_id": "photo_id",
            "photoid": "photo_id",
            "reel_id": "reel_id",
            "reelid": "reel_id",
        }

        field = field_map.get(key)

        if not field:
            return

        # Numeric IDs.
        if valid_numeric_id(value):
            score = {
                "user_uid": 12,
                "profile_id": 14,
                "actor_id": 10,
                "page_id": 20,
                "group_id": 30,
                "owner_id": 10,
                "publisher_id": 18,
                "entity_id": 16,
                "post_id": 18,
                "story_id": 18,
                "media_fbid": 18,
                "video_id": 18,
                "photo_id": 18,
                "reel_id": 18,
            }.get(field, 8)

            self.evidence.add(
                field=field,
                value=value,
                source=source,
                score=score,
                context=key,
                kind="json",
                independent_key=source,
            )

            return

        # Opaque IDs are still useful for post/media fields.
        if field in {
            "post_id",
            "story_id",
            "media_fbid",
            "video_id",
            "photo_id",
            "reel_id",
        }:
            if valid_opaque_id(value):
                self.evidence.add(
                    field=field,
                    value=value,
                    source=source,
                    score=14,
                    context=key,
                    kind="opaque",
                    independent_key=source,
                )

    # --------------------------------------------------------
    # HTML / JS PATTERNS
    # --------------------------------------------------------

    def extract_html_js(self, text: str) -> None:
        """
        Extract identity keys from raw HTML / JS.

        We deliberately require the key itself to be close to the
        numeric candidate. This prevents unrelated page numbers
        from becoming UID candidates.
        """

        patterns = {
            "user_uid": (
                r"""
                (?:
                    user_id|
                    userID|
                    userId
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),

            "profile_id": (
                r"""
                (?:
                    profile_id|
                    profileID|
                    profileId
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),

            "actor_id": (
                r"""
                (?:
                    actor_id|
                    actorID|
                    actorId
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),

            "page_id": (
                r"""
                (?:
                    page_id|
                    pageID|
                    pageId
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),

            "group_id": (
                r"""
                (?:
                    group_id|
                    groupID|
                    groupId
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),

            "owner_id": (
                r"""
                (?:
                    owner_id|
                    ownerID|
                    ownerId
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),

            "publisher_id": (
                r"""
                (?:
                    publisher_id|
                    publisherID|
                    publisherId
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),

            "entity_id": (
                r"""
                (?:
                    entity_id|
                    entityID|
                    entityId
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),

            "post_id": (
                r"""
                (?:
                    post_id|
                    postID|
                    postId
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),

            "story_id": (
                r"""
                (?:
                    story_fbid|
                    storyFbid
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),

            "media_fbid": (
                r"""
                (?:
                    media_fbid|
                    mediaFbid
                )
                \s*[:=]\s*
                ["']?(\d{5,30})["']?
                """
            ),
        }

        for field, pattern in patterns.items():
            for match in re.finditer(
                pattern,
                text,
                re.I | re.X,
            ):
                value = match.group(1)

                if not valid_numeric_id(value):
                    continue

                context_start = max(
                    0,
                    match.start() - 300,
                )

                context_end = min(
                    len(text),
                    match.end() + 300,
                )

                context = text[
                    context_start:context_end
                ]

                self.evidence.add(
                    field=field,
                    value=value,
                    source="html_js_key",
                    score={
                        "group_id": 24,
                        "page_id": 18,
                        "publisher_id": 16,
                        "post_id": 17,
                        "media_fbid": 17,
                        "story_id": 17,
                        "entity_id": 14,
                        "profile_id": 12,
                        "user_uid": 10,
                        "actor_id": 9,
                        "owner_id": 9,
                    }.get(field, 8),
                    context=clean_text(context[:500]),
                    kind="html_js",
                    independent_key="html_js_key",
                )

        # ----------------------------------------------------
        # pfbid
        # ----------------------------------------------------

        for token in PF_TOKEN_RE.findall(text):
            self.evidence.add(
                field="post_id",
                value=token,
                source="pfbid",
                score=22,
                direct=False,
                kind="opaque",
                independent_key="pfbid",
            )

        # ----------------------------------------------------
        # Numeric values inside known URL patterns.
        # ----------------------------------------------------

        for match in re.finditer(
            r"/(?:posts|permalink|videos|reels|photo)/(\d{5,30})",
            text,
            re.I,
        ):
            value = match.group(1)

            self.evidence.add(
                field="entity_id",
                value=value,
                source="embedded_url",
                score=10,
                kind="url",
                independent_key="embedded_url",
            )

    # --------------------------------------------------------
    # APP / DEEP LINKS
    # --------------------------------------------------------

    def extract_app_links(self, text: str) -> list[str]:
        candidates = []

        for match in re.finditer(
            r"""(?is)
            (?:
                https?://
                (?:www\.)?facebook\.com/
                [^\s"'<>]+
            )
            """,
            text,
        ):
            raw = clean_text(match.group(0))
            normalized = normalize_url(raw)

            if normalized:
                candidates.append(normalized)

        return list(
            dict.fromkeys(candidates)
        )

    # --------------------------------------------------------
    # BASE64
    # --------------------------------------------------------

    def inspect_base64_candidates(self, text: str) -> None:
        """
        Conservative Base64 decoder.

        NEVER promotes an arbitrary decoded number to UID.
        Only records decoded numeric values if they occur in
        an identity-shaped payload.
        """

        tokens = set()

        # Long alphanumeric tokens only.
        for token in re.findall(
            r"(?<![A-Za-z0-9+/=_-])"
            r"[A-Za-z0-9+/_=-]{16,180}"
            r"(?![A-Za-z0-9+/=_-])",
            text,
        ):
            tokens.add(token)

        for token in list(tokens)[:1500]:
            decoded = self._decode_base64(token)

            if not decoded:
                continue

            low = decoded.lower()

            identity_marker = any(
                marker in low
                for marker in (
                    "actor_id",
                    "profile_id",
                    "user_id",
                    "page_id",
                    "group_id",
                    "story_fbid",
                    "media_fbid",
                    "post_id",
                    "entity_id",
                    "owner_id",
                )
            )

            if not identity_marker:
                continue

            for number in NUMERIC_RE.findall(decoded):
                if not valid_numeric_id(number):
                    continue

                field = self._field_from_decoded_context(
                    decoded
                )

                if not field:
                    continue

                self.evidence.add(
                    field=field,
                    value=number,
                    source="base64_identity",
                    score=8,
                    context=clean_text(
                        decoded[:700]
                    ),
                    kind="base64",
                    independent_key="base64_identity",
                )

    @staticmethod
    def _decode_base64(token: str) -> str:
        variants = []

        variants.append(token)

        variants.append(
            token.replace("-", "+")
            .replace("_", "/")
        )

        for candidate in variants:
            candidate = re.sub(
                r"[^A-Za-z0-9+/=]",
                "",
                candidate,
            )

            padding = (
                "="
                * ((4 - len(candidate) % 4) % 4)
            )

            try:
                raw = base64.b64decode(
                    candidate + padding,
                    validate=False,
                )

                if not raw:
                    continue

                decoded = raw.decode(
                    "utf-8",
                    errors="ignore",
                )

                if decoded:
                    return decoded

            except (
                ValueError,
                binascii.Error,
            ):
                continue

        return ""

    @staticmethod
    def _field_from_decoded_context(
        decoded: str,
    ) -> Optional[str]:

        low = decoded.lower()

        mapping = (
            ("group_id", "group_id"),
            ("page_id", "page_id"),
            ("profile_id", "profile_id"),
            ("user_id", "user_uid"),
            ("actor_id", "actor_id"),
            ("publisher_id", "publisher_id"),
            ("entity_id", "entity_id"),
            ("story_fbid", "story_id"),
            ("media_fbid", "media_fbid"),
            ("post_id", "post_id"),
            ("owner_id", "owner_id"),
        )

        for marker, field in mapping:
            if marker in low:
                return field

        return None


# ============================================================
# CORRELATION ENGINE
# ============================================================

class IdentityCorrelationEngine:
    """
    Precision-first entity resolver.

    The central rule:

        URL CONTEXT > EXPLICIT METADATA > CORRELATED IDS
        > generic actor/profile candidates > random numbers
    """

    def __init__(
        self,
        *,
        url_type: str,
        url_info: dict[str, str],
        evidence: EvidenceStore,
    ) -> None:

        self.url_type = url_type
        self.url_info = url_info
        self.evidence = evidence

    # --------------------------------------------------------
    # MAIN
    # --------------------------------------------------------

    def resolve(
        self,
        result: ResolveResult,
    ) -> ResolveResult:

        result.url_type = self.url_type

        # ----------------------------------------------------
        # Structural IDs
        # ----------------------------------------------------

        self._apply_structural_ids(result)

        # ----------------------------------------------------
        # Context lock
        # ----------------------------------------------------

        if self.url_type in GROUP_TYPES:
            result.entity_type = (
                "GROUP_POST"
                if self.url_type == "GROUP_POST"
                else "GROUP"
            )

        elif self.url_type in PAGE_TYPES:
            result.entity_type = "PAGE"

        elif self.url_type in USER_TYPES:
            result.entity_type = "USER"

        elif self.url_type in {
            "STORY",
            "REEL",
            "VIDEO",
            "PHOTO",
            "POST",
            "SHARE_POST",
            "SHARE_VIDEO",
            "SHARE_REEL",
        }:
            result.entity_type = self.url_type

        else:
            result.entity_type = "UNKNOWN"

        # ----------------------------------------------------
        # Identity resolution
        # ----------------------------------------------------

        if result.entity_type in GROUP_TYPES:
            self._resolve_group(result)

        elif result.entity_type == "PAGE":
            self._resolve_page(result)

        elif result.entity_type == "USER":
            self._resolve_user(result)

        else:
            self._resolve_generic(result)

        # ----------------------------------------------------
        # Publisher
        # ----------------------------------------------------

        self._resolve_publisher(result)

        # ----------------------------------------------------
        # Object IDs
        # ----------------------------------------------------

        self._resolve_object_ids(result)

        # ----------------------------------------------------
        # Canonical
        # ----------------------------------------------------

        self._resolve_canonical(result)

        # ----------------------------------------------------
        # Final confidence
        # ----------------------------------------------------

        self._calculate_confidence(result)

        return result

    # --------------------------------------------------------
    # STRUCTURAL
    # --------------------------------------------------------

    def _apply_structural_ids(
        self,
        result: ResolveResult,
    ) -> None:

        for field_name, value in self.url_info.items():

            if field_name == "publisher_username":
                result.publisher_username = value

            elif field_name == "group_id":
                if valid_numeric_id(value):
                    result.group_id = value

            elif field_name == "post_id":
                if valid_opaque_id(value):
                    result.post_id = value

            elif field_name == "profile_id":
                if valid_numeric_id(value):
                    result.profile_id = value

            elif field_name == "video_id":
                if valid_opaque_id(value):
                    result.video_id = value

            elif field_name == "photo_id":
                if valid_opaque_id(value):
                    result.photo_id = value

            elif field_name == "reel_id":
                if valid_opaque_id(value):
                    result.reel_id = value

            elif field_name == "story_id":
                if valid_opaque_id(value):
                    result.story_id = value

            elif field_name == "share_token":
                result.share_token = value

    # --------------------------------------------------------
    # GROUP
    # --------------------------------------------------------

    def _resolve_group(
        self,
        result: ResolveResult,
    ) -> None:

        # Structural group_id gets highest priority.
        if not result.group_id:
            candidate = self._best(
                "group_id",
                minimum=40,
            )

            if candidate:
                result.group_id = candidate.value

        # Post ID.
        if not result.post_id:
            candidate = self._best(
                "post_id",
                minimum=45,
            )

            if candidate:
                result.post_id = candidate.value

        # CRITICAL LOCK:
        # never derive user_uid/page_id from group context.
        result.user_uid = None
        result.page_id = None

        # Actor/profile can still be internally useful, but
        # intentionally NOT exposed unless explicitly needed.
        result.actor_id = None
        result.profile_id = None

        if result.group_id:
            result.status = "OBJECT_IDENTIFIED"

        if (
            self.url_type == "GROUP_POST"
            and result.post_id
            and result.group_id
        ):
            result.verification = "VERIFIED"

    # --------------------------------------------------------
    # PAGE
    # --------------------------------------------------------

    def _resolve_page(
        self,
        result: ResolveResult,
    ) -> None:

        # Direct page_id evidence.
        candidate = self._best(
            "page_id",
            minimum=42,
        )

        if candidate:
            result.page_id = candidate.value

        # Publisher ID can become page_id only with
        # page-specific corroboration.
        if not result.page_id:
            publisher = self._best(
                "publisher_id",
                minimum=55,
            )

            if publisher and (
                publisher.independent_sources >= 2
                or publisher.evidence_count >= 3
            ):
                result.page_id = publisher.value

        # Never use generic actor/profile as page UID unless
        # page context has independent confirmation.
        if not result.page_id:
            actor = self._best(
                "actor_id",
                minimum=75,
            )

            if actor and self._has_page_correlation(
                actor.value
            ):
                result.page_id = actor.value

        result.user_uid = None

        if result.page_id:
            result.status = "OBJECT_IDENTIFIED"
            result.verification = "VERIFIED"

    def _has_page_correlation(
        self,
        value: str,
    ) -> bool:

        checks = (
            self.evidence.for_value(
                "page_id",
                value,
            ),
            self.evidence.for_value(
                "publisher_id",
                value,
            ),
        )

        sources = set()

        for group in checks:
            for item in group:
                sources.add(
                    item.independent_key
                    or item.source
                )

        return len(sources) >= 2

    # --------------------------------------------------------
    # USER
    # --------------------------------------------------------

    def _resolve_user(
        self,
        result: ResolveResult,
    ) -> None:

        candidates = []

        for field in (
            "user_uid",
            "profile_id",
            "publisher_id",
            "owner_id",
            "actor_id",
        ):
            for candidate in self.evidence.candidates(
                field
            ):
                candidates.append(
                    (field, candidate)
                )

        candidates.sort(
            key=lambda x: x[1].score,
            reverse=True,
        )

        # Strict candidate acceptance.
        for field, candidate in candidates:
            if candidate.score < 65:
                continue

            if candidate.independent_sources < 2:
                # A profile_id from explicit profile URL can
                # still be accepted.
                if not (
                    field == "profile_id"
                    and self.url_type
                    in {
                        "PROFILE",
                        "PROFILE_CANDIDATE",
                    }
                ):
                    continue

            if self._is_conflicted_user_candidate(
                candidate.value
            ):
                continue

            result.user_uid = candidate.value
            break

        if result.user_uid:
            result.status = "OBJECT_IDENTIFIED"
            result.verification = "VERIFIED"

    def _is_conflicted_user_candidate(
        self,
        value: str,
    ) -> bool:

        # A value appearing strongly as group/page identity
        # cannot simultaneously become USER_UID.
        if self.evidence.for_value(
            "group_id",
            value,
        ):
            return True

        page_evidence = self.evidence.for_value(
            "page_id",
            value,
        )

        if page_evidence:
            return True

        return False

    # --------------------------------------------------------
    # GENERIC
    # --------------------------------------------------------

    def _resolve_generic(
        self,
        result: ResolveResult,
    ) -> None:

        # Group URL can NEVER fall here.
        if self.url_type in GROUP_TYPES:
            return

        # Object IDs.
        if not result.post_id:
            candidate = self._best(
                "post_id",
                minimum=40,
            )

            if candidate:
                result.post_id = candidate.value

        # Do not promote actor/profile automatically.
        # Generic URLs need stronger evidence.
        user = self._best(
            "user_uid",
            minimum=75,
        )

        if (
            user
            and user.independent_sources >= 2
            and not self._is_conflicted_user_candidate(
                user.value
            )
        ):
            result.user_uid = user.value

        page = self._best(
            "page_id",
            minimum=60,
        )

        if page:
            result.page_id = page.value

    # --------------------------------------------------------
    # PUBLISHER
    # --------------------------------------------------------

    def _resolve_publisher(
        self,
        result: ResolveResult,
    ) -> None:

        if not result.publisher_id:

            if result.entity_type == "GROUP_POST":
                # Never use group_id as publisher_id.
                candidate = self._best(
                    "publisher_id",
                    minimum=70,
                )

            elif result.entity_type == "PAGE":
                candidate = self._best(
                    "page_id",
                    minimum=50,
                )

            elif result.entity_type == "USER":
                candidate = self._best(
                    "user_uid",
                    minimum=65,
                )

            else:
                candidate = None

            if candidate:
                result.publisher_id = candidate.value

    # --------------------------------------------------------
    # OBJECT IDS
    # --------------------------------------------------------

    def _resolve_object_ids(
        self,
        result: ResolveResult,
    ) -> None:

        if not result.post_id:
            candidate = self._best(
                "post_id",
                minimum=40,
            )

            if candidate:
                result.post_id = candidate.value

        if not result.media_fbid:
            candidate = self._best(
                "media_fbid",
                minimum=45,
            )

            if candidate:
                result.media_fbid = candidate.value

        if not result.video_id:
            candidate = self._best(
                "video_id",
                minimum=40,
            )

            if candidate:
                result.video_id = candidate.value

        if not result.photo_id:
            candidate = self._best(
                "photo_id",
                minimum=40,
            )

            if candidate:
                result.photo_id = candidate.value

        if not result.reel_id:
            candidate = self._best(
                "reel_id",
                minimum=40,
            )

            if candidate:
                result.reel_id = candidate.value

        if not result.story_id:
            candidate = self._best(
                "story_id",
                minimum=40,
            )

            if candidate:
                result.story_id = candidate.value

        # entity_id is deliberately only used when no more
        # specific object ID exists.
        if not result.entity_id:
            candidate = self._best(
                "entity_id",
                minimum=65,
            )

            if candidate:
                result.entity_id = candidate.value

    # --------------------------------------------------------
    # CANONICAL
    # --------------------------------------------------------

    def _resolve_canonical(
        self,
        result: ResolveResult,
    ) -> None:

        candidates = self.evidence.candidates(
            "canonical_url"
        )

        if candidates:
            result.canonical_url = (
                candidates[0].value
            )

    # --------------------------------------------------------
    # BEST CANDIDATE
    # --------------------------------------------------------

    def _best(
        self,
        field: str,
        *,
        minimum: float = 0,
    ) -> Optional[Candidate]:

        candidates = self.evidence.candidates(
            field
        )

        for candidate in candidates:
            if candidate.score >= minimum:
                return candidate

        return None

    # --------------------------------------------------------
    # CONFIDENCE
    # --------------------------------------------------------

    def _calculate_confidence(
        self,
        result: ResolveResult,
    ) -> None:

        score = 0

        # ----------------------------------------------------
        # Structural certainty
        # ----------------------------------------------------

        if self.url_type in GROUP_TYPES:
            score += 45

            if result.group_id:
                score += 25

            if result.post_id:
                score += 15

        elif self.url_type in PAGE_TYPES:
            score += 35

            if result.page_id:
                score += 40

            if result.post_id:
                score += 10

        elif self.url_type in USER_TYPES:
            score += 35

            if result.user_uid:
                score += 40

        else:
            if result.post_id:
                score += 20

            if result.user_uid:
                score += 35

            if result.page_id:
                score += 35

        # ----------------------------------------------------
        # Evidence quality
        # ----------------------------------------------------

        important_values = (
            result.user_uid,
            result.page_id,
            result.group_id,
            result.post_id,
        )

        for value in important_values:
            if not value:
                continue

            relevant = [
                e
                for e in self.evidence.items
                if e.value == value
            ]

            independent = len({
                e.independent_key
                for e in relevant
            })

            if independent >= 2:
                score += 8

            if independent >= 3:
                score += 8

        # ----------------------------------------------------
        # Verification
        # ----------------------------------------------------

        if result.verification == "VERIFIED":
            score += 8

        result.confidence = min(
            99,
            max(0, score),
        )

        result.confidence_label = (
            confidence_label(
                result.confidence
            )
        )

        if (
            result.status == "FAILED"
            and (
                result.group_id
                or result.page_id
                or result.user_uid
                or result.post_id
                or result.reel_id
                or result.video_id
                or result.photo_id
                or result.story_id
            )
        ):
            result.status = "OBJECT_IDENTIFIED"


# ============================================================
# MAIN RESOLVER
# ============================================================

class FacebookResolver:
    def __init__(
        self,
        *,
        max_concurrent: int = MAX_CONCURRENT,
    ) -> None:

        self.fetcher = HTTPFetcher(
            concurrency=max_concurrent
        )

    async def resolve(
        self,
        url: str,
    ) -> ResolveResult:

        started = time.perf_counter()

        normalized = normalize_url(url)

        result = ResolveResult(
            input_url=normalized or url
        )

        if not normalized:
            result.status = "FAILED"
            result.error = (
                "URL không phải Facebook public URL"
            )
            return result

        normalized = unwrap_redirect(normalized)

        url_type, url_info = (
            FacebookURLClassifier.classify(
                normalized
            )
        )

        result.url_type = url_type

        evidence = EvidenceStore()

        # Structural evidence.
        for field_name, value in url_info.items():

            if field_name == "group_id":
                evidence.add(
                    "group_id",
                    value,
                    "url_structure",
                    45,
                    direct=True,
                    kind="url",
                    independent_key="url_structure",
                )

            elif field_name == "profile_id":
                evidence.add(
                    "profile_id",
                    value,
                    "url_structure",
                    42,
                    direct=True,
                    kind="url",
                    independent_key="url_structure",
                )

            elif field_name == "post_id":
                evidence.add(
                    "post_id",
                    value,
                    "url_structure",
                    38,
                    direct=True,
                    kind="url",
                    independent_key="url_structure",
                )

            elif field_name == "publisher_username":
                evidence.add(
                    "publisher_username",
                    value,
                    "url_structure",
                    20,
                    direct=True,
                    kind="url",
                    independent_key="url_structure",
                )

            elif field_name == "share_token":
                evidence.add(
                    "share_token",
                    value,
                    "url_structure",
                    38,
                    direct=True,
                    kind="url",
                    independent_key="url_structure",
                )

        result.resolved_url = normalized

        # ----------------------------------------------------
        # HTTP crawl
        # ----------------------------------------------------

        snapshots: list[PageSnapshot] = []

        if httpx is not None:

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
                    timeout=self.fetcher.timeout,
                    headers=headers,
                    limits=httpx.Limits(
                        max_connections=10,
                        max_keepalive_connections=5,
                    ),
                    http2=False,
                ) as client:

                    first = await self.fetcher.fetch(
                        client,
                        normalized,
                    )

                    if first:
                        snapshots.append(first)

                        result.resolved_url = (
                            normalize_url(
                                first.final_url
                            )
                            or first.final_url
                        )

                    # ------------------------------------------------
                    # Canonical discovery.
                    # ------------------------------------------------

                    discovered = []

                    for snapshot in snapshots:
                        extractor = FacebookExtractor(
                            url_type=url_type,
                            evidence=evidence,
                        )

                        canonical = (
                            extractor.extract_links(
                                snapshot.text
                            )
                        )

                        extractor.extract_meta(
                            snapshot.text
                        )

                        extractor.extract_title(
                            snapshot.text
                        )

                        extractor.extract_jsonld(
                            snapshot.text
                        )

                        extractor.extract_html_js(
                            snapshot.text
                        )

                        extractor.inspect_base64_candidates(
                            snapshot.text
                        )

                        app_links = (
                            extractor.extract_app_links(
                                snapshot.text
                            )
                        )

                        discovered.extend(canonical)
                        discovered.extend(app_links)

                    # ------------------------------------------------
                    # Fetch canonical first.
                    # ------------------------------------------------

                    crawl_targets = []

                    for candidate in discovered:
                        candidate = normalize_url(
                            candidate
                        )

                        if not candidate:
                            continue

                        if candidate == normalized:
                            continue

                        if candidate == result.resolved_url:
                            continue

                        if candidate not in crawl_targets:
                            crawl_targets.append(
                                candidate
                            )

                    # Keep crawl bounded.
                    crawl_targets = crawl_targets[
                        :MAX_DISCOVERED_LINKS
                    ]

                    if crawl_targets:

                        fetched = await asyncio.gather(
                            *[
                                self.fetcher.fetch(
                                    client,
                                    candidate,
                                )
                                for candidate in crawl_targets
                            ],
                            return_exceptions=True,
                        )

                        for item in fetched:
                            if isinstance(
                                item,
                                PageSnapshot,
                            ):
                                snapshots.append(item)

            except Exception as exc:
                LOGGER.debug(
                    "Resolver HTTP error: %r",
                    exc,
                )

        # ----------------------------------------------------
        # Parse all snapshots.
        # ----------------------------------------------------

        for snapshot in snapshots:

            extractor = FacebookExtractor(
                url_type=url_type,
                evidence=evidence,
            )

            if snapshot.final_url:
                final = normalize_url(
                    snapshot.final_url
                )

                if final:
                    evidence.add(
                        "canonical_url",
                        final,
                        "redirect_final",
                        25,
                        direct=True,
                        kind="url",
                        independent_key="redirect_final",
                    )

            meta = extractor.extract_meta(
                snapshot.text
            )

            extractor.extract_links(
                snapshot.text
            )

            extractor.extract_title(
                snapshot.text
            )

            extractor.extract_jsonld(
                snapshot.text
            )

            extractor.extract_html_js(
                snapshot.text
            )

            extractor.inspect_base64_candidates(
                snapshot.text
            )

            # OG URL gets special correlation value.
            og_url = meta.get("og:url")

            if og_url:
                og_url = normalize_url(
                    unquote(og_url)
                )

                if og_url:
                    evidence.add(
                        "canonical_url",
                        og_url,
                        "og:url",
                        30,
                        direct=True,
                        kind="metadata",
                        independent_key="og:url",
                    )

        # ----------------------------------------------------
        # Canonical URL classification can refine ambiguous
        # user/page posts.
        # ----------------------------------------------------

        canonical_candidates = (
            evidence.candidates(
                "canonical_url"
            )
        )

        refined_type = url_type
        refined_info = dict(url_info)

        for candidate in canonical_candidates[:3]:
            candidate_url = candidate.value

            ctype, cinfo = (
                FacebookURLClassifier.classify(
                    candidate_url
                )
            )

            if ctype in GROUP_TYPES:
                refined_type = ctype
                refined_info.update(cinfo)
                break

            if ctype in PAGE_TYPES:
                refined_type = ctype
                refined_info.update(cinfo)
                break

            if (
                ctype in USER_TYPES
                and refined_type
                in {
                    "AMBIGUOUS_POST",
                    "AMBIGUOUS_VIDEO",
                    "AMBIGUOUS_PHOTO",
                    "AMBIGUOUS_REEL",
                }
            ):
                refined_type = ctype
                refined_info.update(cinfo)

        # ----------------------------------------------------
        # Correlate
        # ----------------------------------------------------

        engine = IdentityCorrelationEngine(
            url_type=refined_type,
            url_info=refined_info,
            evidence=evidence,
        )

        result = engine.resolve(result)

        # Keep evidence internally, but output formatter decides
        # what is visible.
        result.evidence = sorted(
            evidence.items,
            key=lambda x: x.score,
            reverse=True,
        )

        if not result.status:
            result.status = "FAILED"

        # ----------------------------------------------------
        # Hard safety rules
        # ----------------------------------------------------

        if result.entity_type in GROUP_TYPES:
            result.user_uid = None
            result.page_id = None

        if (
            result.entity_type == "PAGE"
            and result.page_id
        ):
            result.user_uid = None

        # Never expose random actor/profile values as UID.
        if (
            result.user_uid
            and result.group_id
            and result.entity_type in GROUP_TYPES
        ):
            result.user_uid = None

        elapsed = int(
            (time.perf_counter() - started) * 1000
        )

        # Internal warning only.
        if (
            result.status == "FAILED"
            and not result.error
        ):
            result.error = (
                "Không tìm thấy bằng chứng public đủ mạnh"
            )

        LOGGER.debug(
            "Resolved %s in %sms: %s",
            normalized,
            elapsed,
            result.status,
        )

        return result

    async def resolve_many(
        self,
        urls: Iterable[str],
    ) -> list[ResolveResult]:

        unique = []

        for url in urls:
            normalized = normalize_url(url)

            if normalized and normalized not in unique:
                unique.append(normalized)

        if not unique:
            return []

        return await asyncio.gather(
            *[
                self.resolve(url)
                for url in unique
            ]
        )


# ============================================================
# TELEGRAM FORMATTER
# ============================================================

def esc(value: Any) -> str:
    return html.escape(
        str(value),
        quote=False,
    )


def format_result(
    result: ResolveResult,
    index: int,
) -> str:

    lines = [
        f"<b>{index}.</b> "
        f"{type_icon(result.entity_type)} "
        f"<b>{esc(display_type(result.entity_type))}</b>"
    ]

    # --------------------------------------------------------
    # Status
    # --------------------------------------------------------

    if result.status:
        lines.append(
            f"📊 <b>STATUS:</b> "
            f"{esc(result.status)}"
        )

    # --------------------------------------------------------
    # USER
    # --------------------------------------------------------

    if result.user_uid:
        lines.append(
            f"🆔 <b>USER UID:</b> "
            f"<code>{esc(result.user_uid)}</code>"
        )

    # --------------------------------------------------------
    # PAGE
    # --------------------------------------------------------

    if result.page_id:
        lines.append(
            f"📄 <b>PAGE UID:</b> "
            f"<code>{esc(result.page_id)}</code>"
        )

    # --------------------------------------------------------
    # GROUP
    # --------------------------------------------------------

    if result.group_id:
        lines.append(
            f"👥 <b>GROUP ID:</b> "
            f"<code>{esc(result.group_id)}</code>"
        )

    # --------------------------------------------------------
    # OBJECT
    # --------------------------------------------------------

    if result.post_id:
        lines.append(
            f"📝 <b>POST ID:</b> "
            f"<code>{esc(result.post_id)}</code>"
        )

    if result.reel_id:
        lines.append(
            f"🎞 <b>REEL ID:</b> "
            f"<code>{esc(result.reel_id)}</code>"
        )

    if result.video_id:
        lines.append(
            f"🎬 <b>VIDEO ID:</b> "
            f"<code>{esc(result.video_id)}</code>"
        )

    if result.photo_id:
        lines.append(
            f"📷 <b>PHOTO ID:</b> "
            f"<code>{esc(result.photo_id)}</code>"
        )

    if result.story_id:
        lines.append(
            f"📖 <b>STORY ID:</b> "
            f"<code>{esc(result.story_id)}</code>"
        )

    if result.media_fbid:
        lines.append(
            f"🖼 <b>MEDIA FBID:</b> "
            f"<code>{esc(result.media_fbid)}</code>"
        )

    # --------------------------------------------------------
    # Publisher
    # --------------------------------------------------------

    if result.publisher_username:
        lines.append(
            f"👤 <b>PUBLISHER:</b> "
            f"<code>{esc(result.publisher_username)}</code>"
        )

    # --------------------------------------------------------
    # Share token
    # --------------------------------------------------------

    if result.share_token:
        lines.append(
            f"🔗 <b>SHARE TOKEN:</b> "
            f"<code>{esc(result.share_token)}</code>"
        )

    # --------------------------------------------------------
    # Canonical
    # --------------------------------------------------------

    if result.canonical_url:
        lines.append(
            f"🌐 <b>CANONICAL:</b> "
            f'<a href="{esc(result.canonical_url)}">'
            f"{esc(result.canonical_url)}"
            f"</a>"
        )

    # --------------------------------------------------------
    # Verification
    # --------------------------------------------------------

    if result.confidence:
        lines.append(
            f"🎯 <b>CONFIDENCE:</b> "
            f"{result.confidence}% "
            f"{esc(result.confidence_label)}"
        )

    if result.verification:
        lines.append(
            f"🔐 <b>VERIFICATION:</b> "
            f"{esc(result.verification)}"
        )

    # --------------------------------------------------------
    # Warnings ONLY when real warnings exist.
    # --------------------------------------------------------

    if result.warnings:
        lines.append(
            "⚠️ <b>WARNING:</b> "
            + " • ".join(
                esc(x)
                for x in result.warnings
            )
        )

    # --------------------------------------------------------
    # Error ONLY when failed.
    # --------------------------------------------------------

    if (
        result.status == "FAILED"
        and result.error
    ):
        lines.append(
            f"❌ <b>ERROR:</b> "
            f"{esc(result.error)}"
        )

    return "\n".join(lines)


def display_type(entity_type: str) -> str:
    mapping = {
        "USER": "USER",
        "USER_POST": "USER POST",
        "USER_VIDEO": "USER VIDEO",
        "USER_PHOTO": "USER PHOTO",
        "USER_REEL": "USER REEL",

        "PAGE": "PAGE",
        "PAGE_POST": "PAGE POST",
        "PAGE_VIDEO": "PAGE VIDEO",
        "PAGE_PHOTO": "PAGE PHOTO",
        "PAGE_REEL": "PAGE REEL",

        "GROUP": "GROUP",
        "GROUP_POST": "GROUP POST",

        "POST": "POST",
        "REEL": "REEL",
        "VIDEO": "VIDEO",
        "PHOTO": "PHOTO",
        "STORY": "STORY",

        "SHARE_POST": "SHARE POST",
        "SHARE_VIDEO": "SHARE VIDEO",
        "SHARE_REEL": "SHARE REEL",
    }

    return mapping.get(
        entity_type,
        entity_type or "UNKNOWN",
    )


def type_icon(entity_type: str) -> str:
    if entity_type in GROUP_TYPES:
        return "👥"

    if entity_type == "PAGE":
        return "📄"

    if entity_type == "USER":
        return "👤"

    if "VIDEO" in entity_type:
        return "🎬"

    if "REEL" in entity_type:
        return "🎞"

    if "PHOTO" in entity_type:
        return "📷"

    if "STORY" in entity_type:
        return "📖"

    return "📝"


# ============================================================
# TELEGRAM HANDLER
# ============================================================

async def _reply_help(event) -> None:
    await event.respond(
        GETUIDFB_HELP,
        parse_mode="html",
    )


def register(
    client,
    *args,
    **kwargs,
):
    """
    Compatible with command loader:

        module.register(client, ...)

    Additional positional/keyword arguments are intentionally
    accepted so this module remains compatible with different
    bot architectures.

    /start and /help are NOT registered here because they should
    normally belong to commands/start.py. This avoids duplicate
    Telegram handlers.
    """

    resolver = FacebookResolver()

    @client.on(
        events.NewMessage(
            pattern=r"(?i)^/getuidfb(?:@\w+)?(?:\s+[\s\S]*)?$"
        )
    )
    async def getuidfb_handler(event):

        text = event.raw_text or ""

        # Remove command itself.
        payload = re.sub(
            r"(?i)^/getuidfb(?:@\w+)?",
            "",
            text,
            count=1,
        ).strip()

        urls = extract_facebook_urls(payload)

        if not urls:
            await event.respond(
                GETUIDFB_HELP,
                parse_mode="html",
            )
            return

        # ----------------------------------------------------
        # Initial response.
        # ----------------------------------------------------

        if len(urls) == 1:
            progress_text = (
                "🔎 <b>FACEBOOK UID V17</b>\n"
                "⏳ Đang phân tích public data..."
            )
        else:
            progress_text = (
                "🔎 <b>FACEBOOK UID V17</b>\n"
                f"📊 Đang xử lý <b>{len(urls)}</b> URL..."
            )

        progress = await event.respond(
            progress_text,
            parse_mode="html",
        )

        try:
            results = await resolver.resolve_many(
                urls
            )

        except Exception as exc:
            LOGGER.exception(
                "getuidfb failed"
            )

            await progress.edit(
                "❌ <b>Resolver error:</b> "
                f"<code>{esc(exc)}</code>",
                parse_mode="html",
            )
            return

        # ----------------------------------------------------
        # Build minimal output.
        # ----------------------------------------------------

        successful = [
            x
            for x in results
            if x.status != "FAILED"
        ]

        failed = [
            x
            for x in results
            if x.status == "FAILED"
        ]

        blocks = [
            "<b>╭──────────────────────────────╮</b>",
            "│ 🔎 <b>FACEBOOK UID V17</b>",
            "<b>╰──────────────────────────────╯</b>",
        ]

        blocks.append(
            f"📊 <b>TOTAL:</b> {len(results)}"
        )

        blocks.append(
            f"✅ <b>SUCCESS:</b> {len(successful)}"
        )

        blocks.append(
            f"❌ <b>FAILED:</b> {len(failed)}"
        )

        blocks.append("")

        for index, result in enumerate(
            results,
            start=1,
        ):
            blocks.append(
                format_result(
                    result,
                    index,
                )
            )

            if index != len(results):
                blocks.append(
                    "━━━━━━━━━━━━━━━━━━━━"
                )

        await progress.edit(
            "\n".join(blocks),
            parse_mode="html",
            link_preview=False,
        )

    # Expose resolver for other command modules/tests.
    client.facebook_uid_resolver = resolver

    return resolver


# ============================================================
# OPTIONAL DIRECT API
# ============================================================

async def resolve_facebook_url(
    url: str,
) -> ResolveResult:
    """
    Programmatic API for other modules.
    """

    resolver = FacebookResolver()

    return await resolver.resolve(
        url
    )


async def resolve_facebook_urls(
    urls: Iterable[str],
) -> list[ResolveResult]:
    """
    Concurrent programmatic API.
    """

    resolver = FacebookResolver()

    return await resolver.resolve_many(
        urls
    )


# ============================================================
# SELF TEST
# ============================================================

if __name__ == "__main__":
    import sys

    async def _main():
        if len(sys.argv) < 2:
            print(
                "Usage: python getuidfb.py "
                "<facebook_url>"
            )
            return

        resolver = FacebookResolver()

        result = await resolver.resolve(
            sys.argv[1]
        )

        print(
            json.dumps(
                {
                    "status": result.status,
                    "url_type": result.url_type,
                    "entity_type": result.entity_type,
                    "user_uid": result.user_uid,
                    "page_id": result.page_id,
                    "group_id": result.group_id,
                    "post_id": result.post_id,
                    "reel_id": result.reel_id,
                    "video_id": result.video_id,
                    "photo_id": result.photo_id,
                    "story_id": result.story_id,
                    "media_fbid": result.media_fbid,
                    "publisher_username":
                        result.publisher_username,
                    "canonical_url":
                        result.canonical_url,
                    "confidence":
                        result.confidence,
                    "verification":
                        result.verification,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    asyncio.run(_main())

