#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FACEBOOK UID / ENTITY IDENTITY RESOLVER
ULTRA FORENSIC EDITION

PUBLIC CONTENT ONLY
HTTP ONLY

NO:
- Facebook Login
- Cookies
- Access Token
- Graph API
- Playwright
- Selenium
- Chromium
- Browser automation

DESIGN:

INPUT
  ↓
URL NORMALIZATION
  ↓
FACEBOOK REDIRECT RESOLUTION
  ↓
CANONICALIZATION
  ↓
ROUTE CLASSIFICATION
  ↓
HTML / META / JSON-LD / SCRIPT EXTRACTION
  ↓
OBJECT EXTRACTION
  ↓
AUTHOR / OWNER / ACTOR / CREATOR DISCOVERY
  ↓
PROFILE DISCOVERY
  ↓
IDENTITY GRAPH
  ↓
USER / PAGE / GROUP SEPARATION
  ↓
MULTI-EVIDENCE CORRELATION
  ↓
CONFLICT DETECTION
  ↓
FINAL USER UID
"""

import asyncio
import contextlib
import html as html_lib
import inspect
import json
import re
import time
import unicodedata

from collections import defaultdict, deque
from dataclasses import dataclass, field
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

import httpx
from telethon import events


from core.task_manager import (
    replace_user_tasks,
    stop_user_tasks,
    track_current_task,
    untrack_current_task,
)


COMMAND_INFO = {
    "command": "getuidfb",
    "description": "Facebook public UID / entity forensic resolver",
    "usage": "/getuidfb [facebook_url]",
    "category": "facebook",
}


# ============================================================
# CONFIG
# ============================================================

MAX_BODY = 6 * 1024 * 1024
MAX_REDIRECTS = 8
MAX_DISCOVERY_URLS = 20
MAX_IDENTITY_HOPS = 3

HTTP_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=18.0,
    write=10.0,
    pool=10.0,
)

HTTP_CONCURRENCY = 4

FB_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
    "mobile.facebook.com",
    "fb.com",
    "www.fb.com",
    "fb.watch",
    "l.facebook.com",
    "lm.facebook.com",
}

FACEBOOK_CONTENT_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
    "mobile.facebook.com",
    "fb.com",
    "www.fb.com",
    "fb.watch",
}

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "igshid",
    "ref",
    "refid",
    "mibextid",
    "mibextid=ATbr",
    "sfnsn",
    "paipv",
}

TRACKING_PREFIXES = (
    "utm_",
    "si_",
)

USER_ROUTE_NAMES = {
    "profile",
    "people",
    "user",
}

NON_USER_ROUTE_NAMES = {
    "pages",
    "groups",
    "events",
    "marketplace",
    "gaming",
}

OBJECT_QUERY_KEYS = {
    "fbid",
    "v",
    "story_fbid",
    "video_id",
    "photo_id",
    "post_id",
    "media_fbid",
    "object_id",
}

NUMERIC_RE = re.compile(r"(?<!\d)\d{5,30}(?!\d)")
PFBID_RE = re.compile(r"\bpfbid[A-Za-z0-9_-]+\b", re.I)

USERNAME_RE = re.compile(
    r"^[A-Za-z0-9.\-_]{2,100}$"
)

UID_KEY_RE = re.compile(
    r"""
    (?:
        user[_-]?id|
        profile[_-]?id|
        person[_-]?id|
        author[_-]?id|
        actor[_-]?id|
        owner[_-]?id
    )
    \s*["':=]+\s*
    ["']?(\d{5,30})
    """,
    re.I | re.X,
)

DEEP_PROFILE_RE = re.compile(
    r"fb://profile/(\d{5,30})",
    re.I,
)

PROFILE_PHP_RE = re.compile(
    r"(?:^|/)profile\.php$",
    re.I,
)

PEOPLE_ROUTE_RE = re.compile(
    r"^/people/[^/]+/(\d{5,30})/?$",
    re.I,
)

USER_ROUTE_RE = re.compile(
    r"^/user/(\d{5,30})/?$",
    re.I,
)

PAGE_ROUTE_RE = re.compile(
    r"^/pages/[^/]+/(\d{5,30})/?$",
    re.I,
)

GROUP_ROUTE_RE = re.compile(
    r"^/groups/(\d{5,30})(?:/.*)?$",
    re.I,
)

EVENT_ROUTE_RE = re.compile(
    r"^/events/(\d{5,30})(?:/.*)?$",
    re.I,
)


# ============================================================
# ENUM-LIKE VALUES
# ============================================================

USER_TYPES = {
    "user",
    "person",
    "profile",
    "human",
}

PAGE_TYPES = {
    "page",
    "organization",
    "localbusiness",
    "brand",
    "business",
}

GROUP_TYPES = {
    "group",
}

EVENT_TYPES = {
    "event",
}

OBJECT_TYPES = {
    "post",
    "video",
    "reel",
    "photo",
    "story",
    "media",
    "comment",
    "attachment",
    "object",
}


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class Evidence:
    uid: str
    kind: str
    strength: float
    source_url: str
    context: str = ""
    independent_key: str = ""
    semantic_type: str = ""
    namespace: str = "USER"
    details: str = ""

    def label(self) -> str:
        return self.kind


@dataclass
class ObjectRef:
    namespace: str
    value: str
    source_url: str
    kind: str = ""
    numeric_id: Optional[str] = None
    context: str = ""


@dataclass
class IdentityEntity:
    uid: str

    username: Optional[str] = None
    name: Optional[str] = None
    profile_url: Optional[str] = None
    avatar: Optional[str] = None

    entity_type: str = "UNKNOWN"

    evidence: List[Evidence] = field(default_factory=list)
    sources: Set[str] = field(default_factory=set)

    score: float = 0.0
    independent_classes: Set[str] = field(default_factory=set)

    conflicts: Set[str] = field(default_factory=set)

    def add_evidence(self, ev: Evidence) -> None:
        self.evidence.append(ev)
        self.sources.add(ev.source_url)

        if ev.independent_key:
            self.independent_classes.add(ev.independent_key)

        self.score += ev.strength

    def best_evidence(self) -> List[Evidence]:
        return sorted(
            self.evidence,
            key=lambda x: x.strength,
            reverse=True,
        )


@dataclass
class PageSnapshot:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    body: str

    canonical_url: Optional[str] = None
    og_url: Optional[str] = None
    og_type: Optional[str] = None
    og_title: Optional[str] = None
    og_description: Optional[str] = None
    og_image: Optional[str] = None

    profile_username: Optional[str] = None
    profile_first_name: Optional[str] = None
    profile_last_name: Optional[str] = None

    json_ld: List[Any] = field(default_factory=list)

    links: List[str] = field(default_factory=list)
    profile_links: List[str] = field(default_factory=list)

    raw_ids: Set[str] = field(default_factory=set)
    deep_profile_ids: Set[str] = field(default_factory=set)

    user_candidates: Dict[str, List[Evidence]] = field(
        default_factory=lambda: defaultdict(list)
    )

    object_refs: List[ObjectRef] = field(default_factory=list)

    blocked: bool = False


@dataclass
class ResolutionResult:
    input_url: str
    final_url: str = ""

    route: str = "UNKNOWN"
    content_type: str = "UNKNOWN"
    publisher_type: str = "UNKNOWN"

    user_candidates: Dict[str, IdentityEntity] = field(
        default_factory=dict
    )

    objects: List[ObjectRef] = field(default_factory=list)

    profile_urls: Set[str] = field(default_factory=set)
    discovered_urls: Set[str] = field(default_factory=set)

    snapshots: List[PageSnapshot] = field(default_factory=list)

    conflicts: List[str] = field(default_factory=list)

    status: str = "UNVERIFIED"
    confidence: float = 0.0

    reason: str = ""


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(value: Any) -> str:
    if value is None:
        return ""

    value = html_lib.unescape(str(value))
    value = value.replace("\\/", "/")
    value = value.replace("\\u002F", "/")
    value = value.replace("\\u003A", ":")
    value = value.replace("\\u003D", "=")
    value = value.replace("\\u0026", "&")
    value = value.replace("\\u0022", '"')
    value = value.replace("\\u0027", "'")

    return re.sub(r"\s+", " ", value).strip()


def normalize_name(value: str) -> str:
    value = clean_text(value)

    value = unicodedata.normalize(
        "NFKD",
        value,
    )

    value = "".join(
        ch for ch in value
        if not unicodedata.combining(ch)
    )

    value = value.casefold()

    value = re.sub(
        r"[^a-z0-9\u0080-\uffff]+",
        "",
        value,
    )

    return value


def valid_numeric_id(value: Any) -> bool:
    if value is None:
        return False

    s = str(value).strip()

    return bool(
        re.fullmatch(r"\d{5,30}", s)
    )


def normalize_numeric(value: Any) -> Optional[str]:
    if value is None:
        return None

    s = str(value).strip()

    if valid_numeric_id(s):
        return s

    return None


def normalize_host(host: str) -> str:
    host = (host or "").lower().strip()

    if host.startswith("www."):
        host = host[4:]

    return host


def is_facebook_host(host: str) -> bool:
    host = normalize_host(host)

    return host in {
        normalize_host(x)
        for x in FB_HOSTS
    }


def is_content_facebook_host(host: str) -> bool:
    host = normalize_host(host)

    return host in {
        normalize_host(x)
        for x in FACEBOOK_CONTENT_HOSTS
    }


def same_facebook_domain(url: str) -> bool:
    try:
        return is_facebook_host(
            urlparse(url).hostname or ""
        )
    except Exception:
        return False


def safe_unquote(value: str) -> str:
    for _ in range(3):
        new = unquote(value)

        if new == value:
            break

        value = new

    return value


def extract_urls(text: str) -> List[str]:
    if not text:
        return []

    pattern = re.compile(
        r"https?://[^\s<>\]\[\"']+",
        re.I,
    )

    return [
        x.rstrip(".,;!?)]}")
        for x in pattern.findall(text)
    ]


def make_absolute(base: str, href: str) -> Optional[str]:
    href = clean_text(href)

    if not href:
        return None

    try:
        value = urljoin(base, href)

        if value.startswith(("http://", "https://")):
            return value

    except Exception:
        pass

    return None


# ============================================================
# URL NORMALIZATION
# ============================================================

def unwrap_facebook_redirect(url: str) -> Optional[str]:
    try:
        p = urlparse(url)

        host = normalize_host(p.hostname or "")

        if host not in {
            "l.facebook.com",
            "lm.facebook.com",
        }:
            return None

        qs = parse_qs(
            p.query,
            keep_blank_values=True,
        )

        target = (
            qs.get("u", [None])[0]
            or qs.get("url", [None])[0]
        )

        if not target:
            return None

        target = safe_unquote(target)

        if same_facebook_domain(target):
            return target

    except Exception:
        pass

    return None


def normalize_facebook_url(url: str) -> str:
    url = clean_text(url)

    if not url:
        return ""

    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    unwrapped = unwrap_facebook_redirect(url)

    if unwrapped:
        url = unwrapped

    try:
        p = urlparse(url)

        host = (p.hostname or "").lower()

        if host == "fb.com":
            host = "www.facebook.com"

        if host == "www.fb.com":
            host = "www.facebook.com"

        query = parse_qs(
            p.query,
            keep_blank_values=True,
        )

        clean_query = {}

        for key, values in query.items():
            key_lower = key.lower()

            if key_lower in TRACKING_PARAMS:
                continue

            if any(
                key_lower.startswith(prefix)
                for prefix in TRACKING_PREFIXES
            ):
                continue

            clean_query[key] = values

        encoded_query = urlencode(
            clean_query,
            doseq=True,
        )

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
                encoded_query,
                "",
            )
        )

    except Exception:
        return url


# ============================================================
# ROUTE CLASSIFIER
# ============================================================

def classify_url(url: str) -> str:
    try:
        p = urlparse(url)

        path = p.path or "/"
        low = path.lower()

        qs = parse_qs(
            p.query,
            keep_blank_values=True,
        )

        host = normalize_host(
            p.hostname or ""
        )

        if host == "fb.watch":
            return "WATCH"

        if PROFILE_PHP_RE.search(path):
            if "id" in qs:
                return "PROFILE"

        if PEOPLE_ROUTE_RE.match(path):
            return "PEOPLE_PROFILE"

        if USER_ROUTE_RE.match(path):
            return "USER_PROFILE"

        if PAGE_ROUTE_RE.match(path):
            return "PAGE"

        if GROUP_ROUTE_RE.match(path):
            return "GROUP"

        if EVENT_ROUTE_RE.match(path):
            return "EVENT"

        if low.startswith("/reel/"):
            if PFBID_RE.search(path):
                return "USER_REEL_OR_REEL"

            return "REEL"

        if low.startswith("/reels/"):
            return "REEL"

        if low.startswith("/watch"):
            return "WATCH"

        if low.startswith("/video"):
            return "VIDEO"

        if low.startswith("/videos/"):
            return "VIDEO"

        if low.startswith("/photo"):
            return "PHOTO"

        if low.startswith("/photos/"):
            return "PHOTO"

        if low.startswith("/story.php"):
            return "STORY"

        if low.startswith("/stories/"):
            return "STORY"

        if low.startswith("/permalink.php"):
            return "POST"

        if low.startswith("/posts/"):
            return "POST"

        if low.startswith("/share/p"):
            return "SHARE_P"

        if low.startswith("/share/r"):
            return "SHARE_R"

        if low.startswith("/share/v"):
            return "SHARE_V"

        if low.startswith("/share/1"):
            return "SHARE_1"

        if low.startswith("/share/"):
            return "SHARE"

        if low.startswith("/p/"):
            return "P_ENTITY"

        if "story_fbid" in qs:
            return "POST"

        if "video_id" in qs or "v" in qs:
            return "VIDEO"

        if "fbid" in qs:
            return "PHOTO_OR_OBJECT"

        if PFBID_RE.search(url):
            return "PFBID_OBJECT"

        if path.count("/") == 1 and path != "/":
            segment = path.strip("/")

            if USERNAME_RE.match(segment):
                return "PROFILE_OR_ENTITY"

        return "UNKNOWN"

    except Exception:
        return "UNKNOWN"


# ============================================================
# HTTP ENGINE
# ============================================================

class FacebookHTTP:
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self.semaphore = asyncio.Semaphore(
            HTTP_CONCURRENCY
        )
        self.cache: Dict[str, Optional[PageSnapshot]] = {}

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            http2=False,
            timeout=HTTP_TIMEOUT,
            follow_redirects=False,
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

    async def fetch(
        self,
        url: str,
    ) -> Optional[PageSnapshot]:

        url = normalize_facebook_url(url)

        if not url:
            return None

        if url in self.cache:
            return self.cache[url]

        async with self.semaphore:
            result = await self._fetch_internal(url)

        self.cache[url] = result

        return result

    async def _fetch_internal(
        self,
        url: str,
    ) -> Optional[PageSnapshot]:

        if not self.client:
            return None

        current = url

        for _ in range(MAX_REDIRECTS + 1):

            try:
                if not same_facebook_domain(current):
                    return None

                response = await self.client.get(
                    current,
                )

            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ):
                return None

            except Exception:
                return None

            status = response.status_code

            location = response.headers.get(
                "location"
            )

            if status in {
                301,
                302,
                303,
                307,
                308,
            } and location:

                nxt = make_absolute(
                    current,
                    location,
                )

                if not nxt:
                    break

                if not same_facebook_domain(nxt):
                    break

                current = normalize_facebook_url(
                    nxt
                )

                continue

            content_type = (
                response.headers.get(
                    "content-type",
                    "application/x-www-form-urlencoded",
                )
                .lower()
            )

            if (
                "text/html" not in content_type
                and "application/xhtml+xml"
                not in content_type
            ):
                return None

            body = await self._read_limited(
                response
            )

            if body is None:
                return None

            snapshot = parse_snapshot(
                requested_url=url,
                final_url=str(response.url),
                status_code=status,
                content_type=content_type,
                body=body,
            )

            return snapshot

        return None

    async def _read_limited(
        self,
        response: httpx.Response,
    ) -> Optional[str]:

        try:
            data = bytearray()

            async for chunk in response.aiter_bytes(
                chunk_size=64 * 1024
            ):
                data.extend(chunk)

                if len(data) > MAX_BODY:
                    break

            return bytes(data[:MAX_BODY]).decode(
                "utf-8",
                errors="ignore",
            )

        except Exception:
            return None


# ============================================================
# HTML EXTRACTION
# ============================================================

def parse_meta_tags(
    body: str,
) -> Dict[str, str]:

    result = {}

    patterns = [
        re.compile(
            r'<meta[^>]+'
            r'(?:property|name)\s*=\s*["\']([^"\']+)["\']'
            r'[^>]+content\s*=\s*["\']([^"\']*)["\']',
            re.I,
        ),
        re.compile(
            r'<meta[^>]+'
            r'content\s*=\s*["\']([^"\']*)["\']'
            r'[^>]+(?:property|name)\s*=\s*["\']([^"\']+)["\']',
            re.I,
        ),
    ]

    for pattern in patterns:

        for match in pattern.finditer(body):

            a = clean_text(match.group(1))
            b = clean_text(match.group(2))

            if pattern is patterns[0]:
                key = a.lower()
                value = b
            else:
                value = a
                key = b.lower()

            result[key] = value

    return result


def parse_canonical(
    body: str,
    base_url: str,
) -> Optional[str]:

    patterns = [
        re.compile(
            r'<link[^>]+rel\s*=\s*["\']canonical["\']'
            r'[^>]+href\s*=\s*["\']([^"\']+)["\']',
            re.I,
        ),
        re.compile(
            r'<link[^>]+href\s*=\s*["\']([^"\']+)["\']'
            r'[^>]+rel\s*=\s*["\']canonical["\']',
            re.I,
        ),
    ]

    for pattern in patterns:

        match = pattern.search(body)

        if match:
            value = make_absolute(
                base_url,
                match.group(1),
            )

            if value and same_facebook_domain(value):
                return normalize_facebook_url(
                    value
                )

    return None


def extract_json_ld(
    body: str,
) -> List[Any]:

    output = []

    for match in re.finditer(
        r'<script[^>]+type\s*=\s*["\']application/ld\+json["\']'
        r'[^>]*>(.*?)</script>',
        body,
        re.I | re.S,
    ):
        raw = match.group(1).strip()

        raw = html_lib.unescape(raw)

        try:
            obj = json.loads(raw)
            output.append(obj)
        except Exception:
            continue

    return output


def recursively_walk(
    obj: Any,
    path: Tuple[str, ...] = (),
):
    yield obj, path

    if isinstance(obj, dict):

        for key, value in obj.items():
            yield from recursively_walk(
                value,
                path + (str(key),),
            )

    elif isinstance(obj, list):

        for index, value in enumerate(obj):
            yield from recursively_walk(
                value,
                path + (str(index),),
            )


def infer_semantic_type(
    obj: Any,
) -> str:

    if not isinstance(obj, dict):
        return ""

    values = []

    for key in (
        "@type",
        "__typename",
        "type",
        "object_type",
        "entity_type",
        "category_type",
    ):
        value = obj.get(key)

        if isinstance(value, str):
            values.append(value)

    for value in values:

        low = value.casefold()

        if low in USER_TYPES:
            return "USER"

        if low in PAGE_TYPES:
            return "PAGE"

        if low in GROUP_TYPES:
            return "GROUP"

        if low in EVENT_TYPES:
            return "EVENT"

        if low in OBJECT_TYPES:
            return "OBJECT"

    return ""


def infer_role_from_path(
    path: Tuple[str, ...],
) -> str:

    joined = ".".join(
        x.casefold()
        for x in path
    )

    if any(
        token in joined
        for token in (
            "author",
            "from",
            "actor",
            "owner",
            "creator",
            "user",
            "person",
            "profile",
        )
    ):
        return "IDENTITY"

    if any(
        token in joined
        for token in (
            "page",
            "group",
            "publisher",
        )
    ):
        return "PUBLISHER"

    return ""


def extract_links(
    body: str,
    base_url: str,
) -> List[str]:

    result = set()

    for match in re.finditer(
        r'<a[^>]+href\s*=\s*["\']([^"\']+)["\']',
        body,
        re.I,
    ):

        href = make_absolute(
            base_url,
            match.group(1),
        )

        if not href:
            continue

        if same_facebook_domain(href):
            result.add(
                normalize_facebook_url(href)
            )

    for raw in extract_urls(body):

        if same_facebook_domain(raw):
            result.add(
                normalize_facebook_url(raw)
            )

    return list(result)


def is_profile_like_url(
    url: str,
) -> bool:

    try:
        p = urlparse(url)
        path = p.path or "/"
        qs = parse_qs(p.query)

        if PROFILE_PHP_RE.search(path):
            return "id" in qs

        if PEOPLE_ROUTE_RE.match(path):
            return True

        if USER_ROUTE_RE.match(path):
            return True

        if path.startswith("/profile.php"):
            return "id" in qs

        if (
            path.count("/") == 1
            and path.strip("/")
            and USERNAME_RE.match(
                path.strip("/")
            )
        ):
            return True

    except Exception:
        pass

    return False


def extract_profile_links(
    links: Iterable[str],
) -> List[str]:

    return [
        x
        for x in links
        if is_profile_like_url(x)
    ]


# ============================================================
# OBJECT EXTRACTION
# ============================================================

def add_object(
    objects: List[ObjectRef],
    namespace: str,
    value: Any,
    source_url: str,
    kind: str = "",
    numeric_id: Optional[str] = None,
    context: str = "",
) -> None:

    if value is None:
        return

    value = clean_text(value)

    if not value:
        return

    if len(value) > 300:
        return

    key = (
        namespace,
        value,
        kind,
    )

    for existing in objects:

        if (
            existing.namespace,
            existing.value,
            existing.kind,
        ) == key:
            return

    objects.append(
        ObjectRef(
            namespace=namespace,
            value=value,
            source_url=source_url,
            kind=kind,
            numeric_id=numeric_id,
            context=context,
        )
    )


def extract_route_objects(
    url: str,
    route: str,
) -> List[ObjectRef]:

    output = []

    try:
        p = urlparse(url)

        path = p.path or ""

        qs = parse_qs(
            p.query,
            keep_blank_values=True,
        )

        for pfbid in PFBID_RE.findall(
            url
        ):

            add_object(
                output,
                "PFBID",
                pfbid,
                url,
                kind=route,
            )

        if route in {
            "PAGE",
            "GROUP",
            "EVENT",
            "P_ENTITY",
        }:

            match = (
                PAGE_ROUTE_RE.match(path)
                or GROUP_ROUTE_RE.match(path)
                or EVENT_ROUTE_RE.match(path)
            )

            if match:

                numeric = match.group(1)

                namespace = {
                    "PAGE": "PAGE",
                    "GROUP": "GROUP",
                    "EVENT": "EVENT",
                }.get(
                    route,
                    "ENTITY",
                )

                add_object(
                    output,
                    namespace,
                    numeric,
                    url,
                    kind=route,
                    numeric_id=numeric,
                )

        for key in OBJECT_QUERY_KEYS:

            values = qs.get(key, [])

            for value in values:

                value = clean_text(value)

                if not value:
                    continue

                namespace = {
                    "fbid": "OBJECT",
                    "v": "VIDEO",
                    "story_fbid": "POST",
                    "video_id": "VIDEO",
                    "photo_id": "PHOTO",
                    "post_id": "POST",
                    "media_fbid": "MEDIA",
                    "object_id": "OBJECT",
                }.get(
                    key,
                    "OBJECT",
                )

                numeric = (
                    value
                    if valid_numeric_id(value)
                    else None
                )

                add_object(
                    output,
                    namespace,
                    value,
                    url,
                    kind=route,
                    numeric_id=numeric,
                    context=key,
                )

        for pattern, namespace, kind in [
            (
                r"/posts/([^/?#]+)",
                "POST",
                "POST",
            ),
            (
                r"/reel/([^/?#]+)",
                "REEL",
                "REEL",
            ),
            (
                r"/videos/([^/?#]+)",
                "VIDEO",
                "VIDEO",
            ),
            (
                r"/photo(?:/|\.php\?fbid=)([^/?#&]+)",
                "PHOTO",
                "PHOTO",
            ),
        ]:

            for match in re.finditer(
                pattern,
                url,
                re.I,
            ):

                value = match.group(1)

                numeric = (
                    value
                    if valid_numeric_id(value)
                    else None
                )

                add_object(
                    output,
                    namespace,
                    value,
                    url,
                    kind=kind,
                    numeric_id=numeric,
                )

    except Exception:
        pass

    return output


def extract_meta_objects(
    snapshot: PageSnapshot,
) -> None:

    body = snapshot.body

    route = classify_url(
        snapshot.final_url
    )

    # pfbid
    for pfbid in PFBID_RE.findall(
        body
    ):

        add_object(
            snapshot.object_refs,
            "PFBID",
            pfbid,
            snapshot.final_url,
            kind=route,
        )

    # explicit object IDs
    object_patterns = [
        (
            r'(?:"|\')post[_-]?id(?:"|\')\s*[:=]\s*["\']?(\d{5,30})',
            "POST",
        ),
        (
            r'(?:"|\')video[_-]?id(?:"|\')\s*[:=]\s*["\']?(\d{5,30})',
            "VIDEO",
        ),
        (
            r'(?:"|\')photo[_-]?id(?:"|\')\s*[:=]\s*["\']?(\d{5,30})',
            "PHOTO",
        ),
        (
            r'(?:"|\')story[_-]?id(?:"|\')\s*[:=]\s*["\']?(\d{5,30})',
            "STORY",
        ),
        (
            r'(?:"|\')media[_-]?fbid(?:"|\')\s*[:=]\s*["\']?(\d{5,30})',
            "MEDIA",
        ),
    ]

    for pattern, namespace in object_patterns:

        for match in re.finditer(
            pattern,
            body,
            re.I,
        ):

            numeric = match.group(1)

            add_object(
                snapshot.object_refs,
                namespace,
                numeric,
                snapshot.final_url,
                kind="EXPLICIT_OBJECT_ID",
                numeric_id=numeric,
            )


# ============================================================
# USER ID EXTRACTION
# ============================================================

def add_user_evidence(
    snapshot: PageSnapshot,
    uid: Any,
    kind: str,
    strength: float,
    independent_key: str,
    context: str = "",
    semantic_type: str = "USER",
    namespace: str = "USER",
    details: str = "",
) -> None:

    uid = normalize_numeric(uid)

    if not uid:
        return

    if namespace != "USER":
        return

    evidence = Evidence(
        uid=uid,
        kind=kind,
        strength=strength,
        source_url=snapshot.final_url,
        context=context,
        independent_key=independent_key,
        semantic_type=semantic_type,
        namespace=namespace,
        details=details,
    )

    snapshot.user_candidates[
        uid
    ].append(evidence)


def extract_deep_profile_ids(
    snapshot: PageSnapshot,
) -> None:

    values = set()

    for value in (
        DEEP_PROFILE_RE.findall(
            snapshot.body
        )
    ):
        values.add(value)

    for value in (
        DEEP_PROFILE_RE.findall(
            snapshot.body.replace(
                "\\/",
                "/",
            )
        )
    ):
        values.add(value)

    for uid in values:

        snapshot.deep_profile_ids.add(
            uid
        )

        add_user_evidence(
            snapshot,
            uid,
            kind="fb://profile",
            strength=98,
            independent_key="DEEPLINK",
            context="fb://profile/<UID>",
        )


def extract_explicit_user_ids(
    snapshot: PageSnapshot,
) -> None:

    body = snapshot.body

    for match in UID_KEY_RE.finditer(
        body
    ):

        uid = match.group(1)

        key_text = match.group(0).casefold()

        if "profile" in key_text:
            strength = 96
            kind = "explicit profile_id"
        elif "person" in key_text:
            strength = 96
            kind = "explicit person_id"
        elif "user" in key_text:
            strength = 96
            kind = "explicit user_id"
        elif "author" in key_text:
            strength = 91
            kind = "explicit author_id"
        elif "actor" in key_text:
            strength = 87
            kind = "explicit actor_id"
        else:
            strength = 84
            kind = "explicit owner_id"

        add_user_evidence(
            snapshot,
            uid,
            kind=kind,
            strength=strength,
            independent_key="EXPLICIT_ID",
            context=key_text,
        )


def extract_json_identity_evidence(
    snapshot: PageSnapshot,
) -> None:

    for root in snapshot.json_ld:

        for obj, path in recursively_walk(
            root
        ):

            if not isinstance(obj, dict):
                continue

            semantic = infer_semantic_type(
                obj
            )

            role = infer_role_from_path(
                path
            )

            # ------------------------------------------------
            # ID
            # ------------------------------------------------

            candidate_ids = []

            for key in (
                "id",
                "uid",
                "user_id",
                "profile_id",
                "person_id",
                "author_id",
                "actor_id",
                "owner_id",
                "creator_id",
            ):

                value = obj.get(key)

                if valid_numeric_id(value):
                    candidate_ids.append(
                        (
                            key,
                            str(value),
                        )
                    )

            # ------------------------------------------------
            # PROFILE URL
            # ------------------------------------------------

            profile_url = None

            for key in (
                "url",
                "sameAs",
                "profile",
                "profileUrl",
            ):

                value = obj.get(key)

                if isinstance(value, str):
                    if same_facebook_domain(value):
                        profile_url = normalize_facebook_url(
                            value
                        )

            # ------------------------------------------------
            # USER SEMANTICS
            # ------------------------------------------------

            user_semantic = (
                semantic == "USER"
                or (
                    role == "IDENTITY"
                    and semantic not in {
                        "PAGE",
                        "GROUP",
                        "EVENT",
                        "OBJECT",
                    }
                )
            )

            if not user_semantic:
                continue

            for key, uid in candidate_ids:

                if semantic == "USER":
                    strength = 94

                elif key in {
                    "user_id",
                    "profile_id",
                    "person_id",
                }:
                    strength = 93

                elif role == "IDENTITY":
                    strength = 87

                else:
                    strength = 80

                add_user_evidence(
                    snapshot,
                    uid,
                    kind=f"JSON {key}",
                    strength=strength,
                    independent_key="JSON_IDENTITY",
                    context=".".join(path),
                    semantic_type=semantic or "USER",
                )

            if profile_url:
                for key, uid in candidate_ids:

                    add_user_evidence(
                        snapshot,
                        uid,
                        kind="JSON identity + profile URL",
                        strength=95,
                        independent_key="PROFILE_CORRELATION",
                        context=profile_url,
                        semantic_type="USER",
                    )


def extract_script_identity_evidence(
    snapshot: PageSnapshot,
) -> None:

    body = snapshot.body

    normalized = body

    replacements = {
        r"\\u0022": '"',
        r"\\u0027": "'",
        r"\\u003A": ":",
        r"\\u003D": "=",
        r"\\u0026": "&",
        r"\\u002F": "/",
        r"\\/": "/",
    }

    for pattern, replacement in replacements.items():
        normalized = re.sub(
            pattern,
            replacement,
            normalized,
            flags=re.I,
        )

    # ---------------------------------------------
    # Explicit user/profile/person IDs
    # ---------------------------------------------

    patterns = [
        (
            r'\buser[_-]?id\b\s*[:=]\s*["\']?(\d{5,30})',
            "script user_id",
            94,
            "SCRIPT_USER_ID",
        ),
        (
            r'\bprofile[_-]?id\b\s*[:=]\s*["\']?(\d{5,30})',
            "script profile_id",
            95,
            "SCRIPT_PROFILE_ID",
        ),
        (
            r'\bperson[_-]?id\b\s*[:=]\s*["\']?(\d{5,30})',
            "script person_id",
            95,
            "SCRIPT_PERSON_ID",
        ),
    ]

    for pattern, kind, strength, independent in patterns:

        for match in re.finditer(
            pattern,
            normalized,
            re.I,
        ):

            uid = match.group(1)

            add_user_evidence(
                snapshot,
                uid,
                kind=kind,
                strength=strength,
                independent_key=independent,
                context=match.group(0),
            )

    # ---------------------------------------------
    # fb://profile
    # ---------------------------------------------

    for uid in DEEP_PROFILE_RE.findall(
        normalized
    ):

        add_user_evidence(
            snapshot,
            uid,
            kind="script fb://profile",
            strength=98,
            independent_key="DEEPLINK",
            context="script",
        )


def extract_profile_route_evidence(
    snapshot: PageSnapshot,
) -> None:

    url = snapshot.final_url

    try:
        p = urlparse(url)

        path = p.path or ""

        qs = parse_qs(
            p.query,
            keep_blank_values=True,
        )

        route = classify_url(
            url
        )

        # profile.php?id=UID
        if (
            PROFILE_PHP_RE.search(path)
            and "id" in qs
        ):

            for uid in qs["id"]:

                if valid_numeric_id(uid):

                    add_user_evidence(
                        snapshot,
                        uid,
                        kind="profile.php?id",
                        strength=91,
                        independent_key="PROFILE_ROUTE",
                        context=url,
                    )

        # /people/name/UID
        match = PEOPLE_ROUTE_RE.match(
            path
        )

        if match:

            uid = match.group(1)

            add_user_evidence(
                snapshot,
                uid,
                kind="/people/name/UID",
                strength=92,
                independent_key="PEOPLE_ROUTE",
                context=url,
            )

        # /user/UID
        match = USER_ROUTE_RE.match(
            path
        )

        if match:

            uid = match.group(1)

            add_user_evidence(
                snapshot,
                uid,
                kind="/user/UID",
                strength=91,
                independent_key="USER_ROUTE",
                context=url,
            )

        # Direct numeric profile route is intentionally
        # NOT automatically accepted.
        if (
            route == "PROFILE_OR_ENTITY"
            and path.count("/") == 1
        ):

            segment = path.strip("/")

            if valid_numeric_id(segment):

                # Weak candidate only.
                # It must be confirmed by page semantics.
                add_user_evidence(
                    snapshot,
                    segment,
                    kind="numeric profile route",
                    strength=58,
                    independent_key="NUMERIC_ROUTE",
                    context=url,
                )

    except Exception:
        pass


def semantic_profile_confirmation(
    snapshot: PageSnapshot,
) -> None:

    meta = parse_meta_tags(
        snapshot.body
    )

    og_type = (
        meta.get("og:type")
        or ""
    ).casefold()

    profile_first = (
        meta.get(
            "profile:first_name"
        )
        or ""
    )

    profile_last = (
        meta.get(
            "profile:last_name"
        )
        or ""
    )

    profile_username = (
        meta.get(
            "profile:username"
        )
        or ""
    )

    is_profile = (
        og_type == "profile"
        or bool(profile_first)
        or bool(profile_last)
        or bool(profile_username)
    )

    if not is_profile:
        return

    for uid, evidence_list in list(
        snapshot.user_candidates.items()
    ):

        for evidence in evidence_list:

            if evidence.kind == "numeric profile route":

                evidence.strength = 88
                evidence.independent_key = (
                    "PROFILE_SEMANTICS"
                )

            elif evidence.kind == "profile.php?id":

                evidence.strength = max(
                    evidence.strength,
                    96,
                )

                evidence.independent_key = (
                    "PROFILE_SEMANTICS"
                )


# ============================================================
# SNAPSHOT PARSER
# ============================================================

def parse_snapshot(
    requested_url: str,
    final_url: str,
    status_code: int,
    content_type: str,
    body: str,
) -> PageSnapshot:

    snapshot = PageSnapshot(
        requested_url=requested_url,
        final_url=normalize_facebook_url(
            final_url
        ),
        status_code=status_code,
        content_type=content_type,
        body=body,
    )

    meta = parse_meta_tags(
        body
    )

    snapshot.canonical_url = parse_canonical(
        body,
        snapshot.final_url,
    )

    snapshot.og_url = (
        meta.get("og:url")
    )

    snapshot.og_type = (
        meta.get("og:type")
    )

    snapshot.og_title = (
        meta.get("og:title")
    )

    snapshot.og_description = (
        meta.get("og:description")
    )

    snapshot.og_image = (
        meta.get("og:image")
    )

    snapshot.profile_username = (
        meta.get(
            "profile:username"
        )
    )

    snapshot.profile_first_name = (
        meta.get(
            "profile:first_name"
        )
    )

    snapshot.profile_last_name = (
        meta.get(
            "profile:last_name"
        )
    )

    snapshot.json_ld = extract_json_ld(
        body
    )

    snapshot.links = extract_links(
        body,
        snapshot.final_url,
    )

    snapshot.profile_links = (
        extract_profile_links(
            snapshot.links
        )
    )

    # Detect login / sparse response
    lower_body = body.casefold()

    snapshot.blocked = (
        status_code in {
            401,
            403,
            429,
            451,
        }
        or (
            "log in to facebook" in lower_body
            and len(body) < 300_000
        )
    )

    # Objects from route
    snapshot.object_refs.extend(
        extract_route_objects(
            snapshot.final_url,
            classify_url(
                snapshot.final_url
            ),
        )
    )

    # Objects from HTML
    extract_meta_objects(
        snapshot
    )

    # Identity extraction
    extract_deep_profile_ids(
        snapshot
    )

    extract_profile_route_evidence(
        snapshot
    )

    extract_explicit_user_ids(
        snapshot
    )

    extract_script_identity_evidence(
        snapshot
    )

    extract_json_identity_evidence(
        snapshot
    )

    semantic_profile_confirmation(
        snapshot
    )

    # Meta profile fields can support route username
    # but never generate a UID by themselves.
    return snapshot


# ============================================================
# IDENTITY GRAPH
# ============================================================

class IdentityGraph:

    def __init__(self):
        self.entities: Dict[
            str,
            IdentityEntity,
        ] = {}

        self.non_user_ids: Dict[
            str,
            Set[str],
        ] = defaultdict(set)

    def add_non_user(
        self,
        namespace: str,
        value: str,
    ) -> None:

        value = clean_text(value)

        if value:
            self.non_user_ids[
                value
            ].add(namespace)

    def add_candidate(
        self,
        uid: str,
        evidence: Evidence,
    ) -> None:

        uid = normalize_numeric(uid)

        if not uid:
            return

        entity = self.entities.get(uid)

        if entity is None:

            entity = IdentityEntity(
                uid=uid
            )

            self.entities[
                uid
            ] = entity

        entity.add_evidence(
            evidence
        )

    def set_metadata(
        self,
        uid: str,
        username: Optional[str] = None,
        name: Optional[str] = None,
        profile_url: Optional[str] = None,
        avatar: Optional[str] = None,
    ) -> None:

        entity = self.entities.get(uid)

        if not entity:
            return

        if username:
            entity.username = (
                entity.username
                or username
            )

        if name:
            entity.name = (
                entity.name
                or name
            )

        if profile_url:
            entity.profile_url = (
                entity.profile_url
                or profile_url
            )

        if avatar:
            entity.avatar = (
                entity.avatar
                or avatar
            )

    def apply_metadata_correlations(
        self,
        snapshot: PageSnapshot,
    ) -> None:

        username = clean_text(
            snapshot.profile_username
            or ""
        )

        name = clean_text(
            " ".join(
                x
                for x in (
                    snapshot.profile_first_name,
                    snapshot.profile_last_name,
                )
                if x
            )
        )

        profile_url = (
            snapshot.canonical_url
            or snapshot.og_url
        )

        for uid in snapshot.user_candidates:

            self.set_metadata(
                uid,
                username=username or None,
                name=name or None,
                profile_url=(
                    profile_url
                    if profile_url
                    and is_profile_like_url(
                        profile_url
                    )
                    else None
                ),
                avatar=snapshot.og_image,
            )

    def correlate_profile_link(
        self,
        source_snapshot: PageSnapshot,
        profile_url: str,
        profile_snapshot: PageSnapshot,
    ) -> None:

        source_username = (
            extract_username_from_url(
                profile_url
            )
        )

        profile_username = (
            profile_snapshot.profile_username
            or source_username
        )

        profile_name = clean_text(
            " ".join(
                x
                for x in (
                    profile_snapshot.profile_first_name,
                    profile_snapshot.profile_last_name,
                )
                if x
            )
        )

        for uid in profile_snapshot.user_candidates:

            entity = self.entities.get(uid)

            if not entity:
                continue

            self.set_metadata(
                uid,
                username=profile_username,
                name=profile_name or None,
                profile_url=profile_snapshot.canonical_url
                or profile_snapshot.og_url
                or profile_url,
                avatar=profile_snapshot.og_image,
            )

            # Profile fetched from a discovered author/profile
            # URL is an independent correlation class.
            add_user_evidence_to_entity(
                entity,
                Evidence(
                    uid=uid,
                    kind="profile page correlation",
                    strength=6,
                    source_url=source_snapshot.final_url,
                    context=profile_url,
                    independent_key="PROFILE_FETCH",
                    semantic_type="USER",
                    namespace="USER",
                    details=(
                        "Content/profile URL resolved "
                        "to same user identity"
                    ),
                ),
            )

    def finalize_scores(
        self,
    ) -> None:

        for entity in self.entities.values():

            # Metadata bonuses
            username = normalize_name(
                entity.username or ""
            )

            if username:
                entity.score += 2

            if entity.profile_url:
                entity.score += 3

            if entity.name:
                entity.score += 1

            # Independent evidence bonus
            independent_count = len(
                entity.independent_classes
            )

            if independent_count >= 2:
                entity.score += 4

            if independent_count >= 3:
                entity.score += 3

            if independent_count >= 4:
                entity.score += 2

            # Conflict penalty
            if entity.conflicts:
                entity.score -= (
                    8 * len(entity.conflicts)
                )

            entity.score = min(
                100.0,
                max(
                    0.0,
                    entity.score,
                ),
            )


def add_user_evidence_to_entity(
    entity: IdentityEntity,
    evidence: Evidence,
) -> None:

    # Avoid duplicated exact evidence.
    for existing in entity.evidence:

        if (
            existing.uid == evidence.uid
            and existing.kind == evidence.kind
            and existing.source_url
            == evidence.source_url
            and existing.context
            == evidence.context
        ):
            return

    entity.add_evidence(
        evidence
    )


def extract_username_from_url(
    url: str,
) -> Optional[str]:

    try:
        p = urlparse(url)

        path = (
            p.path
            or ""
        ).strip("/")

        if not path:
            return None

        parts = path.split("/")

        if len(parts) == 1:

            value = parts[0]

            if USERNAME_RE.match(value):
                if not valid_numeric_id(value):
                    return value

        if (
            len(parts) >= 2
            and parts[0].lower()
            in {
                "people",
            }
        ):
            return parts[1]

    except Exception:
        pass

    return None


# ============================================================
# PROFILE DISCOVERY
# ============================================================

def profile_variants(
    url: str,
) -> List[str]:

    result = []

    try:
        p = urlparse(url)

        path = p.path or "/"

        # Keep original
        if same_facebook_domain(url):
            result.append(
                normalize_facebook_url(
                    url
                )
            )

        # Profile.php stays as is
        if path.lower().startswith(
            "/profile.php"
        ):
            return dedupe_urls(result)

        # Username
        username = extract_username_from_url(
            url
        )

        if not username:
            return dedupe_urls(result)

        for host in (
            "www.facebook.com",
            "m.facebook.com",
            "mbasic.facebook.com",
            "web.facebook.com",
            "touch.facebook.com",
        ):

            result.append(
                f"https://{host}/"
                f"{quote(username)}"
            )

    except Exception:
        pass

    return dedupe_urls(result)


def dedupe_urls(
    urls: Iterable[str],
) -> List[str]:

    seen = set()
    output = []

    for url in urls:

        url = normalize_facebook_url(
            url
        )

        if not url:
            continue

        key = canonical_url_key(
            url
        )

        if key in seen:
            continue

        seen.add(key)
        output.append(url)

    return output


def canonical_url_key(
    url: str,
) -> str:

    try:
        p = urlparse(
            normalize_facebook_url(url)
        )

        host = normalize_host(
            p.hostname or ""
        )

        path = (
            p.path
            or "/"
        ).rstrip("/") or "/"

        qs = parse_qs(
            p.query,
            keep_blank_values=True,
        )

        identity_keys = {}

        for key in (
            "id",
            "story_fbid",
            "fbid",
            "v",
            "post_id",
            "video_id",
            "photo_id",
            "media_fbid",
        ):

            if key in qs:
                identity_keys[
                    key
                ] = tuple(
                    sorted(qs[key])
                )

        return (
            host,
            path,
            tuple(
                sorted(
                    identity_keys.items()
                )
            ),
        ).__repr__()

    except Exception:
        return url


# ============================================================
# SEMANTIC CLASSIFICATION
# ============================================================

def classify_snapshot(
    snapshot: PageSnapshot,
) -> Tuple[str, str, str]:

    route = classify_url(
        snapshot.final_url
    )

    content_type = "UNKNOWN"
    publisher_type = "UNKNOWN"

    meta = parse_meta_tags(
        snapshot.body
    )

    og_type = (
        meta.get("og:type")
        or ""
    ).casefold()

    title = clean_text(
        snapshot.og_title
        or ""
    ).casefold()

    body = snapshot.body.casefold()

    if "video" in og_type:
        content_type = "VIDEO"

    elif route in {
        "REEL",
        "USER_REEL_OR_REEL",
    }:
        content_type = "REEL"

    elif route in {
        "PHOTO",
        "PHOTO_OR_OBJECT",
    }:
        content_type = "PHOTO"

    elif route == "STORY":
        content_type = "STORY"

    elif route in {
        "POST",
        "SHARE_P",
        "PFBID_OBJECT",
    }:
        content_type = "POST"

    elif route in {
        "VIDEO",
        "WATCH",
        "SHARE_V",
    }:
        content_type = "VIDEO"

    elif route == "PAGE":
        content_type = "PAGE"

    elif route == "GROUP":
        content_type = "GROUP"

    elif route == "EVENT":
        content_type = "EVENT"

    elif og_type == "profile":
        content_type = "PROFILE"

    # Publisher
    if route == "PAGE":
        publisher_type = "PAGE"

    elif route == "GROUP":
        publisher_type = "GROUP"

    elif route in {
        "PEOPLE_PROFILE",
        "USER_PROFILE",
        "PROFILE",
    }:
        publisher_type = "USER"

    # textual semantic hints
    if (
        "group" in body
        and publisher_type == "UNKNOWN"
    ):
        publisher_type = "GROUP"

    if (
        "page" in body
        and publisher_type == "UNKNOWN"
        and route in {
            "POST",
            "VIDEO",
            "PHOTO",
            "REEL",
        }
    ):
        # Weak hint only.
        publisher_type = "PAGE"

    # Never classify user solely because title has a name.
    _ = title

    return (
        route,
        content_type,
        publisher_type,
    )


# ============================================================
# ENTITY SEPARATION
# ============================================================

def register_non_user_objects(
    graph: IdentityGraph,
    snapshot: PageSnapshot,
) -> None:

    for obj in snapshot.object_refs:

        if obj.namespace in {
            "PAGE",
            "GROUP",
            "EVENT",
            "POST",
            "VIDEO",
            "PHOTO",
            "STORY",
            "MEDIA",
            "OBJECT",
            "ENTITY",
            "PFBID",
        }:

            if obj.numeric_id:
                graph.add_non_user(
                    obj.namespace,
                    obj.numeric_id,
                )


def reject_object_only_candidates(
    graph: IdentityGraph,
) -> None:

    for uid, entity in graph.entities.items():

        object_namespaces = (
            graph.non_user_ids.get(uid)
            or set()
        )

        if not object_namespaces:
            continue

        strong_user_evidence = any(
            ev.strength >= 90
            and ev.namespace == "USER"
            and ev.semantic_type
            in {
                "",
                "USER",
                "PERSON",
            }
            for ev in entity.evidence
        )

        if not strong_user_evidence:
            entity.score = min(
                entity.score,
                55.0,
            )

            entity.conflicts.update(
                object_namespaces
            )


# ============================================================
# CORRELATION
# ============================================================

def apply_profile_correlations(
    graph: IdentityGraph,
    source_snapshot: PageSnapshot,
    profile_snapshot: PageSnapshot,
    profile_url: str,
) -> None:

    source_username = (
        extract_username_from_url(
            profile_url
        )
    )

    source_name = normalize_name(
        " ".join(
            x
            for x in (
                source_snapshot.profile_first_name,
                source_snapshot.profile_last_name,
            )
            if x
        )
    )

    profile_username = normalize_name(
        profile_snapshot.profile_username
        or source_username
        or ""
    )

    profile_name = normalize_name(
        " ".join(
            x
            for x in (
                profile_snapshot.profile_first_name,
                profile_snapshot.profile_last_name,
            )
            if x
        )
    )

    username_match = (
        bool(
            source_username
            and profile_username
            and normalize_name(
                source_username
            ) == profile_username
        )
    )

    name_match = (
        bool(
            source_name
            and profile_name
            and source_name == profile_name
        )
    )

    for uid in profile_snapshot.user_candidates:

        entity = graph.entities.get(
            uid
        )

        if not entity:
            continue

        if username_match:

            add_user_evidence_to_entity(
                entity,
                Evidence(
                    uid=uid,
                    kind="username correlation",
                    strength=6,
                    source_url=source_snapshot.final_url,
                    context=profile_url,
                    independent_key="USERNAME_CORRELATION",
                    semantic_type="USER",
                    namespace="USER",
                ),
            )

        if name_match:

            add_user_evidence_to_entity(
                entity,
                Evidence(
                    uid=uid,
                    kind="name correlation",
                    strength=2,
                    source_url=source_snapshot.final_url,
                    context=profile_url,
                    independent_key="NAME_CORRELATION",
                    semantic_type="USER",
                    namespace="USER",
                ),
            )

        add_user_evidence_to_entity(
            entity,
            Evidence(
                uid=uid,
                kind="content → profile correlation",
                strength=8,
                source_url=source_snapshot.final_url,
                context=profile_url,
                independent_key="CONTENT_PROFILE",
                semantic_type="USER",
                namespace="USER",
            ),
        )

        entity.profile_url = (
            entity.profile_url
            or profile_snapshot.canonical_url
            or profile_snapshot.og_url
            or profile_url
        )

        entity.username = (
            entity.username
            or profile_snapshot.profile_username
            or source_username
        )

        name = clean_text(
            " ".join(
                x
                for x in (
                    profile_snapshot.profile_first_name,
                    profile_snapshot.profile_last_name,
                )
                if x
            )
        )

        entity.name = (
            entity.name
            or name
        )

        entity.avatar = (
            entity.avatar
            or profile_snapshot.og_image
        )


def correlate_content_author(
    graph: IdentityGraph,
    source_snapshot: PageSnapshot,
) -> None:

    """
    Adds additional weight to identity evidence that appears
    in explicit author/from/actor/creator contexts.
    """

    body = source_snapshot.body

    for uid, evidence_list in list(
        source_snapshot.user_candidates.items()
    ):

        for evidence in evidence_list:

            context = (
                evidence.context
                or ""
            ).casefold()

            kind = (
                evidence.kind
                or ""
            ).casefold()

            author_context = any(
                x in context
                for x in (
                    "author",
                    "from",
                    "actor",
                    "creator",
                    "owner",
                )
            )

            author_kind = any(
                x in kind
                for x in (
                    "author",
                    "actor",
                    "creator",
                    "owner",
                )
            )

            if not (
                author_context
                or author_kind
            ):
                continue

            entity = graph.entities.get(
                uid
            )

            if not entity:
                continue

            add_user_evidence_to_entity(
                entity,
                Evidence(
                    uid=uid,
                    kind="content author correlation",
                    strength=5,
                    source_url=source_snapshot.final_url,
                    context=evidence.context,
                    independent_key="AUTHOR_CONTEXT",
                    semantic_type="USER",
                    namespace="USER",
                ),
            )

    _ = body


# ============================================================
# RESULT EVALUATION
# ============================================================

def evaluate_result(
    result: ResolutionResult,
    graph: IdentityGraph,
) -> None:

    graph.finalize_scores()

    reject_object_only_candidates(
        graph
    )

    candidates = sorted(
        graph.entities.values(),
        key=lambda x: (
            x.score,
            len(x.independent_classes),
            len(x.evidence),
        ),
        reverse=True,
    )

    result.user_candidates = {
        x.uid: x
        for x in candidates
    }

    if not candidates:

        result.status = "UNVERIFIED"
        result.confidence = 0.0
        result.reason = (
            "Không tìm thấy bằng chứng User UID "
            "công khai đủ mạnh."
        )

        return

    best = candidates[0]

    second = (
        candidates[1]
        if len(candidates) > 1
        else None
    )

    independent = len(
        best.independent_classes
    )

    # Strong conflict
    if (
        second
        and best.score >= 88
        and second.score >= 88
        and abs(
            best.score - second.score
        ) <= 7
    ):

        result.status = "CONFLICT"

        result.confidence = round(
            best.score,
            1,
        )

        result.conflicts.append(
            f"{best.uid} vs {second.uid}"
        )

        result.reason = (
            "Có từ hai User candidate mạnh trở lên "
            "nhưng chưa đủ bằng chứng để chọn duy nhất."
        )

        return

    if (
        best.score >= 92
        and independent >= 2
    ):

        result.status = "VERIFIED"

        result.confidence = round(
            best.score,
            1,
        )

        result.reason = (
            "UID được xác nhận bởi nhiều lớp "
            "bằng chứng độc lập."
        )

        return

    if (
        best.score >= 94
        and independent >= 1
        and any(
            ev.strength >= 98
            for ev in best.evidence
        )
    ):

        result.status = "VERIFIED"

        result.confidence = round(
            best.score,
            1,
        )

        result.reason = (
            "Có bằng chứng định danh cực mạnh "
            "và không có xung đột."
        )

        return

    if best.score >= 75:

        result.status = "LIKELY"

        result.confidence = round(
            best.score,
            1,
        )

        result.reason = (
            "Có bằng chứng User UID đáng tin "
            "nhưng chưa đạt ngưỡng xác minh tuyệt đối."
        )

        return

    result.status = "UNVERIFIED"

    result.confidence = round(
        best.score,
        1,
    )

    result.reason = (
        "Có numeric candidate nhưng chưa chứng minh "
        "được đó là User UID."
    )


# ============================================================
# RESOLVER
# ============================================================

class FacebookResolver:

    def __init__(
        self,
        http: FacebookHTTP,
    ):
        self.http = http

    async def resolve(
        self,
        input_url: str,
    ) -> ResolutionResult:

        started = time.monotonic()

        input_url = normalize_facebook_url(
            input_url
        )

        result = ResolutionResult(
            input_url=input_url
        )

        if not input_url:
            result.reason = "URL không hợp lệ."
            return result

        graph = IdentityGraph()

        queue = deque()
        queued = set()
        visited = set()

        queue.append(
            (
                input_url,
                0,
                "INPUT",
            )
        )

        queued.add(
            canonical_url_key(
                input_url
            )
        )

        while (
            queue
            and len(visited)
            < MAX_DISCOVERY_URLS
        ):

            url, hop, reason = queue.popleft()

            key = canonical_url_key(
                url
            )

            if key in visited:
                continue

            visited.add(key)

            snapshot = await self.http.fetch(
                url
            )

            if not snapshot:
                continue

            result.snapshots.append(
                snapshot
            )

            result.discovered_urls.add(
                snapshot.final_url
            )

            if not result.final_url:
                result.final_url = (
                    snapshot.final_url
                )

            route, content_type, publisher_type = (
                classify_snapshot(
                    snapshot
                )
            )

            if result.route == "UNKNOWN":
                result.route = route

            if result.content_type == "UNKNOWN":
                result.content_type = (
                    content_type
                )

            if result.publisher_type == "UNKNOWN":
                result.publisher_type = (
                    publisher_type
                )

            # ---------------------------------------------
            # Canonical
            # ---------------------------------------------

            expansion = []

            if snapshot.canonical_url:
                expansion.append(
                    (
                        snapshot.canonical_url,
                        hop,
                        "CANONICAL",
                    )
                )

            if snapshot.og_url:
                expansion.append(
                    (
                        snapshot.og_url,
                        hop,
                        "OG_URL",
                    )
                )

            # ---------------------------------------------
            # Profile links
            # ---------------------------------------------

            for profile_url in (
                snapshot.profile_links[:8]
            ):

                result.profile_urls.add(
                    profile_url
                )

                expansion.append(
                    (
                        profile_url,
                        min(
                            hop + 1,
                            MAX_IDENTITY_HOPS,
                        ),
                        "PROFILE_LINK",
                    )
                )

            # ---------------------------------------------
            # Same-page URLs
            # ---------------------------------------------

            for discovered in (
                snapshot.links[:20]
            ):

                if not same_facebook_domain(
                    discovered
                ):
                    continue

                route2 = classify_url(
                    discovered
                )

                if route2 in {
                    "PROFILE",
                    "PEOPLE_PROFILE",
                    "USER_PROFILE",
                    "PROFILE_OR_ENTITY",
                }:

                    expansion.append(
                        (
                            discovered,
                            min(
                                hop + 1,
                                MAX_IDENTITY_HOPS,
                            ),
                            "IDENTITY_LINK",
                        )
                    )

            # ---------------------------------------------
            # Graph candidate evidence
            # ---------------------------------------------

            for uid, evidence_list in (
                snapshot.user_candidates.items()
            ):

                for evidence in evidence_list:

                    graph.add_candidate(
                        uid,
                        evidence,
                    )

            graph.apply_metadata_correlations(
                snapshot
            )

            correlate_content_author(
                graph,
                snapshot,
            )

            register_non_user_objects(
                graph,
                snapshot,
            )

            # ---------------------------------------------
            # Objects
            # ---------------------------------------------

            for obj in snapshot.object_refs:

                result.objects.append(
                    obj
                )

            # ---------------------------------------------
            # Profile expansion
            # ---------------------------------------------

            profile_targets = []

            for profile_url in (
                snapshot.profile_links[:6]
            ):
                profile_targets.extend(
                    profile_variants(
                        profile_url
                    )
                )

            # Route username profile
            username = (
                extract_username_from_url(
                    snapshot.final_url
                )
            )

            if username:

                profile_targets.extend(
                    profile_variants(
                        snapshot.final_url
                    )
                )

            for target in dedupe_urls(
                profile_targets
            )[:10]:

                expansion.append(
                    (
                        target,
                        min(
                            hop + 1,
                            MAX_IDENTITY_HOPS,
                        ),
                        "PROFILE_VARIANT",
                    )
                )

            # ---------------------------------------------
            # Schedule expansion
            # ---------------------------------------------

            for target, next_hop, why in expansion:

                if not target:
                    continue

                if not same_facebook_domain(
                    target
                ):
                    continue

                if next_hop > MAX_IDENTITY_HOPS:
                    continue

                target = normalize_facebook_url(
                    target
                )

                target_key = canonical_url_key(
                    target
                )

                if target_key in visited:
                    continue

                if target_key in queued:
                    continue

                if len(queued) >= MAX_DISCOVERY_URLS:
                    break

                queued.add(
                    target_key
                )

                # Prioritize identity routes.
                if why in {
                    "PROFILE_LINK",
                    "IDENTITY_LINK",
                    "PROFILE_VARIANT",
                }:

                    queue.appendleft(
                        (
                            target,
                            next_hop,
                            why,
                        )
                    )

                else:

                    queue.append(
                        (
                            target,
                            next_hop,
                            why,
                        )
                    )

        # ----------------------------------------------------
        # Cross-snapshot profile correlation
        # ----------------------------------------------------

        for source in result.snapshots:

            for profile_url in source.profile_links[:8]:

                normalized = normalize_facebook_url(
                    profile_url
                )

                for profile_snapshot in result.snapshots:

                    if canonical_url_key(
                        profile_snapshot.final_url
                    ) != canonical_url_key(
                        normalized
                    ):
                        continue

                    apply_profile_correlations(
                        graph,
                        source,
                        profile_snapshot,
                        normalized,
                    )

        # ----------------------------------------------------
        # Canonical same-profile correlations
        # ----------------------------------------------------

        for snapshot in result.snapshots:

            for candidate_uid in (
                snapshot.user_candidates
            ):

                entity = graph.entities.get(
                    candidate_uid
                )

                if not entity:
                    continue

                if snapshot.canonical_url:

                    entity.profile_url = (
                        entity.profile_url
                        or snapshot.canonical_url
                    )

                if snapshot.profile_username:

                    entity.username = (
                        entity.username
                        or snapshot.profile_username
                    )

                if (
                    snapshot.profile_first_name
                    or snapshot.profile_last_name
                ):

                    name = clean_text(
                        " ".join(
                            x
                            for x in (
                                snapshot.profile_first_name,
                                snapshot.profile_last_name,
                            )
                            if x
                        )
                    )

                    entity.name = (
                        entity.name
                        or name
                    )

                entity.avatar = (
                    entity.avatar
                    or snapshot.og_image
                )

        # ----------------------------------------------------
        # Remove duplicate objects
        # ----------------------------------------------------

        unique_objects = []

        seen_objects = set()

        for obj in result.objects:

            key = (
                obj.namespace,
                obj.value,
                obj.kind,
            )

            if key in seen_objects:
                continue

            seen_objects.add(key)
            unique_objects.append(
                obj
            )

        result.objects = unique_objects

        # ----------------------------------------------------
        # Evaluate
        # ----------------------------------------------------

        evaluate_result(
            result,
            graph,
        )

        elapsed = (
            time.monotonic()
            - started
        )

        # Final metadata
        result._elapsed = elapsed

        return result


# ============================================================
# OUTPUT FORMAT
# ============================================================

def best_entity(
    result: ResolutionResult,
) -> Optional[IdentityEntity]:

    if not result.user_candidates:
        return None

    return sorted(
        result.user_candidates.values(),
        key=lambda x: (
            x.score,
            len(x.independent_classes),
            len(x.evidence),
        ),
        reverse=True,
    )[0]


def object_values(
    result: ResolutionResult,
    namespace: str,
) -> List[str]:

    return list(dict.fromkeys(
        obj.value
        for obj in result.objects
        if obj.namespace == namespace
    ))


def numeric_object_values(
    result: ResolutionResult,
    namespace: str,
) -> List[str]:

    return list(dict.fromkeys(
        obj.numeric_id
        for obj in result.objects
        if (
            obj.namespace == namespace
            and obj.numeric_id
        )
    ))


def format_entity_evidence(
    entity: IdentityEntity,
    limit: int = 8,
) -> List[str]:

    output = []

    for ev in entity.best_evidence()[:limit]:

        text = ev.kind

        if ev.context:
            text += (
                f" [{ev.context[:100]}]"
            )

        output.append(
            f"• {text} "
            f"(+{ev.strength:.0f})"
        )

    return output


def format_result(
    result: ResolutionResult,
    index: int,
) -> str:

    entity = best_entity(
        result
    )

    lines = []

    lines.append(
        "╭──────────────────────────"
    )

    lines.append(
        f"│ 🔎 FACEBOOK RESOLVER #{index}"
    )

    lines.append(
        "├──────────────────────────"
    )

    lines.append(
        "│ 🔗 INPUT"
    )

    lines.append(
        f"│ Link   : {result.input_url}"
    )

    lines.append(
        f"│ Final  : "
        f"{result.final_url or 'Không xác định'}"
    )

    lines.append(
        "│"
    )

    lines.append(
        "│ 🧭 PHÂN LOẠI"
    )

    lines.append(
        f"│ Route     : {result.route}"
    )

    lines.append(
        f"│ Nội dung  : {result.content_type}"
    )

    lines.append(
        f"│ Publisher : {result.publisher_type}"
    )

    lines.append(
        "│"
    )

    lines.append(
        "│ 👤 TÁC GIẢ / USER"
    )

    if (
        entity
        and result.status in {
            "VERIFIED",
            "LIKELY",
        }
    ):

        lines.append(
            f"│ UID      : {entity.uid}"
        )

        lines.append(
            f"│ Username : "
            f"{entity.username or 'Không có'}"
        )

        lines.append(
            f"│ Tên      : "
            f"{entity.name or 'Không có'}"
        )

        lines.append(
            "│ Entity   : USER"
        )

    else:

        lines.append(
            "│ UID      : ❓ Không xác định"
        )

        lines.append(
            "│ Entity   : Chưa xác minh"
        )

    # --------------------------------------------------------
    # Publisher / non-user
    # --------------------------------------------------------

    page_ids = object_values(
        result,
        "PAGE",
    )

    group_ids = object_values(
        result,
        "GROUP",
    )

    event_ids = object_values(
        result,
        "EVENT",
    )

    if page_ids or group_ids or event_ids:

        lines.append(
            "│"
        )

        lines.append(
            "│ 🏛 NƠI ĐĂNG / ENTITY"
        )

        if page_ids:

            lines.append(
                f"│ PAGE ID  : "
                f"{', '.join(page_ids[:4])}"
            )

            lines.append(
                "│ ⚠️ PAGE ID không phải User UID"
            )

        if group_ids:

            lines.append(
                f"│ GROUP ID : "
                f"{', '.join(group_ids[:4])}"
            )

            lines.append(
                "│ ⚠️ GROUP ID không phải User UID"
            )

        if event_ids:

            lines.append(
                f"│ EVENT ID : "
                f"{', '.join(event_ids[:4])}"
            )

    # --------------------------------------------------------
    # Objects
    # --------------------------------------------------------

    pfbids = object_values(
        result,
        "PFBID",
    )

    post_ids = (
        numeric_object_values(
            result,
            "POST",
        )
        + object_values(
            result,
            "POST",
        )
    )

    video_ids = (
        numeric_object_values(
            result,
            "VIDEO",
        )
        + object_values(
            result,
            "VIDEO",
        )
    )

    photo_ids = (
        numeric_object_values(
            result,
            "PHOTO",
        )
        + object_values(
            result,
            "PHOTO",
        )
    )

    story_ids = (
        numeric_object_values(
            result,
            "STORY",
        )
        + object_values(
            result,
            "STORY",
        )
    )

    media_ids = (
        numeric_object_values(
            result,
            "MEDIA",
        )
        + object_values(
            result,
            "MEDIA",
        )
    )

    if any(
        (
            pfbids,
            post_ids,
            video_ids,
            photo_ids,
            story_ids,
            media_ids,
        )
    ):

        lines.append(
            "│"
        )

        lines.append(
            "│ 📦 OBJECT"
        )

        if pfbids:

            lines.append(
                f"│ PFBID    : "
                f"{', '.join(pfbids[:3])}"
            )

        if post_ids:

            lines.append(
                f"│ Post ID  : "
                f"{', '.join(post_ids[:4])}"
            )

        if video_ids:

            lines.append(
                f"│ Video ID : "
                f"{', '.join(video_ids[:4])}"
            )

        if photo_ids:

            lines.append(
                f"│ Photo ID : "
                f"{', '.join(photo_ids[:4])}"
            )

        if story_ids:

            lines.append(
                f"│ Story ID : "
                f"{', '.join(story_ids[:4])}"
            )

        if media_ids:

            lines.append(
                f"│ Media ID : "
                f"{', '.join(media_ids[:4])}"
            )

    # --------------------------------------------------------
    # Profile
    # --------------------------------------------------------

    if entity and entity.profile_url:

        lines.append(
            "│"
        )

        lines.append(
            "│ 🔗 PROFILE"
        )

        lines.append(
            f"│ {entity.profile_url}"
        )

    # --------------------------------------------------------
    # Evidence
    # --------------------------------------------------------

    if entity:

        lines.append(
            "│"
        )

        lines.append(
            "│ 🔬 EVIDENCE"
        )

        lines.extend(
            "│ " + x
            for x in format_entity_evidence(
                entity
            )
        )

    # --------------------------------------------------------
    # Verification
    # --------------------------------------------------------

    lines.append(
        "│"
    )

    lines.append(
        "│ 🛡 XÁC MINH"
    )

    lines.append(
        f"│ Trạng thái : {result.status}"
    )

    lines.append(
        f"│ Độ tin cậy : "
        f"{result.confidence:.1f}/100"
    )

    if entity:

        lines.append(
            f"│ Evidence   : "
            f"{len(entity.evidence)}"
        )

        lines.append(
            f"│ Independent: "
            f"{len(entity.independent_classes)}"
        )

    lines.append(
        f"│ Reason     : {result.reason}"
    )

    # --------------------------------------------------------
    # Alternatives
    # --------------------------------------------------------

    alternatives = [
        x
        for x in result.user_candidates.values()
        if not entity
        or x.uid != entity.uid
    ]

    if alternatives:

        lines.append(
            "│"
        )

        lines.append(
            "│ ⚠️ CANDIDATES"
        )

        for candidate in sorted(
            alternatives,
            key=lambda x: x.score,
            reverse=True,
        )[:4]:

            lines.append(
                f"│ • {candidate.uid} "
                f"→ {candidate.score:.1f}"
            )

    lines.append(
        "╰──────────────────────────"
    )

    return "\n".join(
        lines
    )


# ============================================================
# TELEGRAM SESSION MANAGER
# ============================================================

_sessions: Dict[
    Tuple[int, int],
    bool,
] = {}

_session_locks: Dict[
    Tuple[int, int],
    asyncio.Lock,
] = {}


def session_key(
    event,
) -> Tuple[int, int]:

    return (
        int(event.chat_id or 0),
        int(event.sender_id or 0),
    )


def get_session_lock(
    key: Tuple[int, int],
) -> asyncio.Lock:

    lock = _session_locks.get(
        key
    )

    if lock is None:

        lock = asyncio.Lock()

        _session_locks[
            key
        ] = lock

    return lock


async def safe_send(
    event,
    text: str,
) -> None:

    try:
        await event.respond(
            text,
            link_preview=False,
        )

    except Exception:
        with contextlib.suppress(
            Exception
        ):
            await event.reply(
                text
            )


# ============================================================
# TASK MANAGER BRIDGE
# ============================================================

async def submit_task(
    user_id: int,
    coroutine,
):
    """
    Compatible with both synchronous and asynchronous
    implementations of replace_user_tasks().
    """

    try:

        result = replace_user_tasks(
            user_id,
            coroutine,
        )

        if inspect.isawaitable(
            result
        ):
            return await result

        return result

    except Exception:

        # If task manager rejects the coroutine,
        # close it to avoid "coroutine was never awaited".
        with contextlib.suppress(
            Exception
        ):
            coroutine.close()

        raise


# ============================================================
# URL INPUT EXTRACTION
# ============================================================

def extract_facebook_urls_from_event(
    event,
) -> List[str]:

    values = []

    raw = (
        getattr(
            event,
            "raw_text",
            None,
        )
        or ""
    )

    values.extend(
        extract_urls(raw)
    )

    # If Telegram message has only a bare URL
    # without scheme.
    for token in raw.split():

        token = token.strip()

        if (
            "facebook.com/" in token.lower()
            or "fb.watch/" in token.lower()
            or token.lower().startswith(
                "www.fb.com/"
            )
        ):

            if not token.startswith(
                "http://"
            ) and not token.startswith(
                "https://"
            ):

                values.append(
                    "https://" + token
                )

    output = []

    for value in values:

        value = normalize_facebook_url(
            value
        )

        if not value:
            continue

        if not same_facebook_domain(
            value
        ):
            continue

        if value not in output:
            output.append(
                value
            )

    return output


# ============================================================
# MAIN WORKER
# ============================================================

async def resolve_and_send(
    event,
    urls: List[str],
) -> None:

    key = session_key(
        event
    )

    lock = get_session_lock(
        key
    )

    async with lock:

        started = time.monotonic()

        await safe_send(
            event,
            (
                "🔎 <b>FACEBOOK FORENSIC</b>\n"
                f"📊 Đang kiểm tra: {len(urls)} link\n"
                "🧠 Multi-source UID correlation\n"
                "⏳ Đang phân tích..."
            ),
        )

        verified = 0
        conflict = 0
        unverified = 0

        outputs = []

        async with FacebookHTTP() as http:

            resolver = FacebookResolver(
                http
            )

            for index, url in enumerate(
                urls,
                1,
            ):

                if not _sessions.get(
                    key,
                    False,
                ):
                    break

                try:

                    result = await resolver.resolve(
                        url
                    )

                    if result.status == "VERIFIED":
                        verified += 1

                    elif result.status == "CONFLICT":
                        conflict += 1

                    else:
                        unverified += 1

                    outputs.append(
                        format_result(
                            result,
                            index,
                        )
                    )

                except asyncio.CancelledError:
                    raise

                except Exception as exc:

                    unverified += 1

                    outputs.append(
                        "\n".join(
                            [
                                "╭──────────────────────────",
                                f"│ 🔎 FACEBOOK RESOLVER #{index}",
                                "├──────────────────────────",
                                f"│ ❌ ERROR: {type(exc).__name__}",
                                f"│ {str(exc)[:300]}",
                                "╰──────────────────────────",
                            ]
                        )
                    )

        elapsed = (
            time.monotonic()
            - started
        )

        header = "\n".join(
            [
                "🔎 <b>FACEBOOK FORENSIC RESULT</b>",
                f"📊 Đã kiểm tra: {len(outputs)}",
                f"✅ Verified: {verified}",
                f"⚠️ Conflict: {conflict}",
                f"❌ Unverified: {unverified}",
                f"⏱ Tổng thời gian: {elapsed:.2f}s",
            ]
        )

        await safe_send(
            event,
            header,
        )

        # Telegram message length safety.
        buffer = ""

        for block in outputs:

            if len(
                buffer
            ) + len(block) + 2 > 3800:

                if buffer:

                    await safe_send(
                        event,
                        buffer,
                    )

                buffer = block

            else:

                if buffer:
                    buffer += "\n\n"

                buffer += block

        if buffer:

            await safe_send(
                event,
                buffer,
            )


# ============================================================
# COMMAND HANDLER
# ============================================================

def register(
    bot,
    notify_bot=None,
):

    @bot.on(
        events.NewMessage(
            pattern=r"^/getuidfb(?:\s+(.+))?$",
        )
    )
    async def getuidfb_command(
        event,
    ):

        key = session_key(
            event
        )

        _sessions[
            key
        ] = True

        raw_argument = (
            event.pattern_match.group(1)
            if event.pattern_match
            else None
        )

        if raw_argument:

            urls = extract_facebook_urls_from_event(
                event
            )

            if not urls:

                urls = []

                for token in (
                    raw_argument.split()
                ):

                    if (
                        "facebook.com/" in token.lower()
                        or "fb.watch/" in token.lower()
                    ):

                        urls.append(
                            normalize_facebook_url(
                                token
                            )
                        )

            if not urls:

                await safe_send(
                    event,
                    (
                        "❌ Không tìm thấy Facebook URL.\n\n"
                        "Ví dụ:\n"
                        "/getuidfb https://facebook.com/..."
                    ),
                )

                return

            worker = resolve_and_send(
                event,
                urls,
            )

            try:
                await submit_task(
                    int(event.sender_id),
                    worker,
                )

            except asyncio.CancelledError:
                pass

            return

        await safe_send(
            event,
            (
                "🔎 <b>FACEBOOK UID FORENSIC</b>\n\n"
                "✅ Session đã bật.\n"
                "📎 Gửi Facebook URL để phân tích.\n\n"
                "Hỗ trợ:\n"
                "• Profile\n"
                "• Post\n"
                "• Reel\n"
                "• Video\n"
                "• Photo\n"
                "• Story\n"
                "• Watch\n"
                "• Share\n"
                "• pfbid\n"
                "• permalink.php\n"
                "• story.php\n"
                "• photo.php\n"
                "• video.php\n"
                "• Page / Group content\n\n"
                "🛡 Không login / cookie / token.\n"
                "🧠 Chỉ trả User UID khi đủ bằng chứng.\n\n"
                "⛔ /stop để dừng."
            ),
        )

    # --------------------------------------------------------
    # Generic URL handler
    # --------------------------------------------------------

    @bot.on(
        events.NewMessage()
    )
    async def getuidfb_message(
        event,
    ):

        text = (
            getattr(
                event,
                "raw_text",
                None,
            )
            or ""
        ).strip()

        if not text:
            return

        # Ignore commands.
        if text.startswith("/"):
            return

        key = session_key(
            event
        )

        if not _sessions.get(
            key,
            False,
        ):
            return

        urls = extract_facebook_urls_from_event(
            event
        )

        if not urls:
            return

        worker = resolve_and_send(
            event,
            urls,
        )

        try:

            await submit_task(
                int(event.sender_id),
                worker,
            )

        except asyncio.CancelledError:
            pass

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    @bot.on(
        events.NewMessage(
            pattern=r"^/stop$",
        )
    )
    async def getuidfb_stop(
        event,
    ):

        key = session_key(
            event
        )

        _sessions[
            key
        ] = False

        user_id = int(
            event.sender_id
        )

        try:

            result = stop_user_tasks(
                user_id
            )

            if inspect.isawaitable(
                result
            ):
                await result

        except Exception:
            pass

        await safe_send(
            event,
            (
                "🛑 <b>FACEBOOK FORENSIC</b>\n"
                "Đã dừng toàn bộ task resolver "
                "của phiên hiện tại."
            ),
        )

    return True


# ============================================================
# OPTIONAL CLEANUP
# ============================================================

async def shutdown_getuidfb():
    _sessions.clear()
    _session_locks.clear()