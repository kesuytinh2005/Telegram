#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
================================================================
 FACEBOOK UID / ENTITY RESOLVER V17.5 UID-FIRST
================================================================

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
- Concurrent HTTP
- Direct Facebook URL
- /getuidfb <facebook_url>

GOAL:
- Tối đa khả năng tìm NUMERIC UID chính của chủ thể URL.
- Giữ nguyên kiểu tích hợp command.register(...)
- Không biến Group ID thành User UID.
- Không bỏ qua pfbid / media_fbid.
- Không phụ thuộc task_manager / database.

ENGINE:
1. URL structural analysis
2. Redirect unwrap
3. Canonical discovery
4. OG metadata
5. JSON-LD
6. Embedded JSON
7. HTML / JS identity extraction
8. Publisher extraction
9. Publisher profile crawling
10. ID correlation
11. Entity separation
12. UID verification
13. Minimal Telegram output
================================================================
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
from typing import Any, Optional, Iterable
from urllib.parse import (
    parse_qs,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

try:
    import httpx
except ImportError:
    httpx = None

from telethon import events


# ==============================================================
# COMMAND INFO
# ==============================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": "Facebook UID / Entity Resolver",
    "usage": "/getuidfb <facebook_url>",
    "aliases": ["uidfb", "fbuid"],
}


HELP_TEXT = """
<b>🔎 FACEBOOK UID RESOLVER V17.5</b>

<b>Sử dụng:</b>
<code>/getuidfb &lt;facebook_url&gt;</code>

Hỗ trợ:
• USER / USER POST
• PAGE / PAGE POST
• GROUP / GROUP POST
• POST / REEL / VIDEO / PHOTO / STORY
• pfbid
• media_fbid
• actor_id / profile_id / owner_id
• canonical / OG / JSON-LD
• HTML / JS
• Facebook redirect

<b>UID được ưu tiên tìm từ publisher/chủ thể chính.</b>
"""


LOGGER = logging.getLogger("getuidfb.v17")


# ==============================================================
# CONSTANTS
# ==============================================================

FACEBOOK_HOSTS = {
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

MAX_HTML = 7_000_000
MAX_CRAWL = 5
MAX_PROFILE_CRAWL = 2
MAX_CONCURRENT = 6

NUMERIC_ID_RE = re.compile(
    r"(?<!\d)(\d{5,30})(?!\d)"
)

PFID_RE = re.compile(
    r"\bpfbid[A-Za-z0-9_-]+\b",
    re.I,
)

FB_URL_RE = re.compile(
    r"""(?ix)
    https?://
    (?:
        (?:[a-z0-9-]+\.)?facebook\.com
        |
        (?:www\.)?fb\.com
        |
        fb\.watch
    )
    [^\s<>"']*
    """
)

USERNAME_RE = re.compile(
    r"^[A-Za-z0-9._-]{2,100}$"
)


# ==============================================================
# ENTITY
# ==============================================================

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


# ==============================================================
# DATA
# ==============================================================

@dataclass
class Evidence:
    field: str
    value: str
    source: str
    score: float
    context: str = ""
    direct: bool = False
    role: str = ""
    independent: str = ""


@dataclass
class Candidate:
    field: str
    value: str
    evidence: list[Evidence] = field(
        default_factory=list
    )

    @property
    def score(self) -> float:
        total = 0.0

        total += sum(
            min(35.0, e.score)
            for e in self.evidence
        )

        independent = len({
            e.independent or e.source
            for e in self.evidence
        })

        if independent >= 2:
            total += 15

        if independent >= 3:
            total += 15

        if independent >= 4:
            total += 10

        if any(e.direct for e in self.evidence):
            total += 10

        if len(self.evidence) >= 2:
            total += 7

        return min(100.0, total)


@dataclass
class Snapshot:
    requested_url: str
    final_url: str
    status: int
    text: str
    headers: dict[str, str] = field(
        default_factory=dict
    )


@dataclass
class Result:
    input_url: str

    status: str = "FAILED"
    url_type: str = "UNKNOWN"
    entity_type: str = "UNKNOWN"

    confidence: int = 0
    verification: str = "UNVERIFIED"

    user_uid: Optional[str] = None
    page_id: Optional[str] = None
    group_id: Optional[str] = None

    post_id: Optional[str] = None
    reel_id: Optional[str] = None
    video_id: Optional[str] = None
    photo_id: Optional[str] = None
    story_id: Optional[str] = None
    media_fbid: Optional[str] = None

    publisher: Optional[str] = None
    publisher_id: Optional[str] = None

    canonical_url: Optional[str] = None
    resolved_url: Optional[str] = None

    share_token: Optional[str] = None

    warnings: list[str] = field(
        default_factory=list
    )

    error: Optional[str] = None

    evidence: list[Evidence] = field(
        default_factory=list
    )


# ==============================================================
# UTIL
# ==============================================================

def clean(value: Any) -> str:
    if value is None:
        return ""

    value = html.unescape(str(value))
    value = value.replace("\\/", "/")
    value = value.replace("\\u002F", "/")
    value = value.replace("\\u003A", ":")
    value = value.replace("\\u0026", "&")

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def numeric(value: Any) -> bool:
    if value is None:
        return False

    value = str(value).strip()

    if not value.isdigit():
        return False

    if not 5 <= len(value) <= 30:
        return False

    # Avoid obvious dummy values.
    if len(set(value)) == 1:
        return False

    return True


def opaque(value: Any) -> bool:
    if value is None:
        return False

    value = str(value).strip()

    return bool(
        3 <= len(value) <= 300
        and re.fullmatch(
            r"[A-Za-z0-9_.:-]+",
            value,
        )
    )


def host(url: str) -> str:
    return (
        urlparse(url)
        .netloc
        .lower()
        .split(":")[0]
    )


def is_fb(url: str) -> bool:
    h = host(url)

    return (
        h in FACEBOOK_HOSTS
        or h.endswith(".facebook.com")
        or h.endswith(".fb.com")
    )


def normalize(url: str) -> str:
    url = clean(url)

    if not url:
        return ""

    if not url.startswith(
        ("http://", "https://")
    ):
        url = "https://" + url

    parsed = urlparse(url)

    if not is_fb(url):
        return ""

    query = parse_qs(
        parsed.query,
        keep_blank_values=False,
    )

    useful = {}

    for key, values in query.items():

        if key.lower() in {
            "id",
            "fbid",
            "story_fbid",
            "v",
            "u",
            "set",
            "substory_index",
        } and values:
            useful[key] = values[-1]

    return urlunparse(
        (
            "https",
            host(url),
            re.sub(
                r"/{2,}",
                "/",
                parsed.path or "/",
            ).rstrip("/") or "/",
            "",
            urlencode(useful),
            "",
        )
    )


def unwrap(url: str) -> str:
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)

        for key in (
            "u",
            "url",
            "target",
            "redirect_uri",
        ):
            values = qs.get(key)

            if not values:
                continue

            candidate = unquote(
                values[-1]
            )

            if candidate.startswith(
                ("http://", "https://")
            ) and is_fb(candidate):
                return normalize(candidate)

    except Exception:
        pass

    return normalize(url)


def extract_urls(text: str) -> list[str]:
    result = []

    for match in FB_URL_RE.finditer(
        text or ""
    ):
        url = match.group(0).rstrip(
            ".,!?;:)]}>\"'"
        )

        url = unwrap(url)

        if url and url not in result:
            result.append(url)

    return result


# ==============================================================
# EVIDENCE STORE
# ==============================================================

class EvidenceStore:

    def __init__(self):
        self.items: list[Evidence] = []
        self._keys = set()

    def add(
        self,
        field: str,
        value: Any,
        source: str,
        score: float,
        *,
        context: str = "",
        direct: bool = False,
        role: str = "",
        independent: str = "",
    ):
        value = clean(value)

        if not value:
            return

        item = Evidence(
            field=field,
            value=value,
            source=source,
            score=score,
            context=context,
            direct=direct,
            role=role,
            independent=(
                independent
                or source
            ),
        )

        key = (
            item.field,
            item.value,
            item.source,
            item.role,
        )

        if key in self._keys:
            return

        self._keys.add(key)
        self.items.append(item)

    def candidates(
        self,
        field: str,
    ) -> list[Candidate]:

        grouped: dict[str, Candidate] = {}

        for item in self.items:

            if item.field != field:
                continue

            grouped.setdefault(
                item.value,
                Candidate(
                    field=field,
                    value=item.value,
                ),
            ).evidence.append(item)

        return sorted(
            grouped.values(),
            key=lambda x: x.score,
            reverse=True,
        )

    def has(
        self,
        field: str,
        value: str,
    ) -> bool:

        return any(
            e.field == field
            and e.value == value
            for e in self.items
        )


# ==============================================================
# URL ANALYSIS
# ==============================================================

class URLParser:

    @staticmethod
    def parse(
        url: str,
    ) -> tuple[str, dict[str, str]]:

        p = urlparse(url)
        path = unquote(p.path or "")
        low = path.lower()

        query = parse_qs(p.query)
        info = {}

        # ------------------------------------------------------
        # GROUP POST — absolute priority
        # ------------------------------------------------------

        m = re.search(
            r"/groups/([^/]+)"
            r"/(?:posts|permalink|user_posts)"
            r"/([^/?#]+)",
            low,
            re.I,
        )

        if m:

            group = unquote(m.group(1))
            post = unquote(m.group(2))

            if numeric(group):
                info["group_id"] = group

            if opaque(post):
                info["post_id"] = post

            return "GROUP_POST", info

        # ------------------------------------------------------
        # GROUP
        # ------------------------------------------------------

        m = re.search(
            r"/groups/([^/?#]+)",
            low,
            re.I,
        )

        if m:

            group = unquote(m.group(1))

            if numeric(group):
                info["group_id"] = group

            return "GROUP", info

        # ------------------------------------------------------
        # STORY
        # ------------------------------------------------------

        if low.startswith("/story.php"):

            story = query.get(
                "story_fbid",
                [None],
            )[-1]

            owner = query.get(
                "id",
                [None],
            )[-1]

            if story:
                info["story_id"] = story

            if numeric(owner):
                info["owner_id"] = owner

            return "STORY", info

        # ------------------------------------------------------
        # SHARE
        # ------------------------------------------------------

        m = re.search(
            r"/share/(p|v|r)/([^/?#]+)",
            low,
            re.I,
        )

        if m:

            mode = m.group(1).lower()
            token = unquote(m.group(2))

            info["share_token"] = token

            if mode == "p":
                if token.lower().startswith("pfbid"):
                    info["post_id"] = token

                return "SHARE_POST", info

            if mode == "v":
                return "SHARE_VIDEO", info

            return "SHARE_REEL", info

        # ------------------------------------------------------
        # PROFILE.PHP
        # ------------------------------------------------------

        if low.startswith("/profile.php"):

            profile = query.get(
                "id",
                [None],
            )[-1]

            if numeric(profile):
                info["profile_id"] = profile

            return "USER", info

        # ------------------------------------------------------
        # PAGES
        # ------------------------------------------------------

        if low.startswith("/pages/"):

            parts = [
                unquote(x)
                for x in path.split("/")
                if x
            ]

            if len(parts) >= 2:
                info["publisher"] = parts[1]

            if "/posts/" in low:

                post = path.split(
                    "/posts/",
                    1,
                )[1].split("/", 1)[0]

                if post:
                    info["post_id"] = post

                return "PAGE_POST", info

            if "/videos/" in low:

                video = path.split(
                    "/videos/",
                    1,
                )[1].split("/", 1)[0]

                if video:
                    info["video_id"] = video

                return "PAGE_VIDEO", info

            if "/reels/" in low:

                reel = path.split(
                    "/reels/",
                    1,
                )[1].split("/", 1)[0]

                if reel:
                    info["reel_id"] = reel

                return "PAGE_REEL", info

            return "PAGE", info

        # ------------------------------------------------------
        # REEL
        # ------------------------------------------------------

        m = re.search(
            r"/(?:reel|reels)/([^/?#]+)",
            low,
            re.I,
        )

        if m:

            info["reel_id"] = unquote(
                m.group(1)
            )

            return "REEL", info

        # ------------------------------------------------------
        # WATCH
        # ------------------------------------------------------

        if low.startswith("/watch"):

            video = query.get(
                "v",
                [None],
            )[-1]

            if video:
                info["video_id"] = video

            return "VIDEO", info

        # ------------------------------------------------------
        # VIDEOS
        # ------------------------------------------------------

        m = re.search(
            r"/videos/(?:[^/]+/)?([^/?#]+)",
            low,
            re.I,
        )

        if m:

            info["video_id"] = unquote(
                m.group(1)
            )

            return "VIDEO", info

        # ------------------------------------------------------
        # PHOTO
        # ------------------------------------------------------

        if low.startswith("/photo.php"):

            photo = query.get(
                "fbid",
                [None],
            )[-1]

            if photo:
                info["photo_id"] = photo

            return "PHOTO", info

        # ------------------------------------------------------
        # /p/
        # ------------------------------------------------------

        m = re.search(
            r"/p/([^/?#]+)",
            low,
            re.I,
        )

        if m:

            info["post_id"] = unquote(
                m.group(1)
            )

            return "POST", info

        # ------------------------------------------------------
        # USER/PAGE STYLE CONTENT
        # ------------------------------------------------------

        parts = [
            unquote(x)
            for x in path.split("/")
            if x
        ]

        if parts:

            username = parts[0]

            ignored = {
                "home",
                "watch",
                "groups",
                "pages",
                "videos",
                "reels",
                "gaming",
                "events",
                "marketplace",
                "profile.php",
                "photo.php",
                "story.php",
                "share",
            }

            if username.lower() not in ignored:

                if len(parts) >= 3:

                    action = parts[1].lower()
                    obj = parts[2]

                    info["publisher"] = username

                    if action == "posts":
                        info["post_id"] = obj
                        return (
                            "AMBIGUOUS_POST",
                            info,
                        )

                    if action == "videos":
                        info["video_id"] = obj
                        return (
                            "AMBIGUOUS_VIDEO",
                            info,
                        )

                    if action == "photos":
                        info["photo_id"] = obj
                        return (
                            "AMBIGUOUS_PHOTO",
                            info,
                        )

                    if action in {
                        "reel",
                        "reels",
                    }:
                        info["reel_id"] = obj
                        return (
                            "AMBIGUOUS_REEL",
                            info,
                        )

                if len(parts) == 1:
                    info["publisher"] = username
                    return (
                        "PROFILE_CANDIDATE",
                        info,
                    )

        return "UNKNOWN", info


# ==============================================================
# HTTP
# ==============================================================

class HTTP:

    def __init__(
        self,
        concurrency: int = MAX_CONCURRENT,
    ):

        if httpx is None:
            raise RuntimeError(
                "Cần cài httpx: "
                "pip install httpx"
            )

        self.sem = asyncio.Semaphore(
            concurrency
        )

        self.timeout = httpx.Timeout(
            connect=8,
            read=15,
            write=8,
            pool=8,
        )

    async def get(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> Optional[Snapshot]:

        async with self.sem:

            try:

                response = await client.get(
                    url,
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
                )

                content_type = (
                    response.headers
                    .get(
                        "content-type",
                        "",
                    )
                    .lower()
                )

                if (
                    "html" not in content_type
                    and "text/" not in content_type
                ):
                    return None

                raw = response.content[
                    :MAX_HTML
                ]

                text = raw.decode(
                    response.encoding
                    or "utf-8",
                    errors="replace",
                )

                return Snapshot(
                    requested_url=url,
                    final_url=str(
                        response.url
                    ),
                    status=response.status_code,
                    text=text,
                    headers=dict(
                        response.headers
                    ),
                )

            except Exception as exc:

                LOGGER.debug(
                    "GET %s failed: %r",
                    url,
                    exc,
                )

                return None


# ==============================================================
# EXTRACTOR
# ==============================================================

class Extractor:

    META_RE = re.compile(
        r"""(?is)
        <meta
        [^>]+
        (?:
            property|name
        )
        \s*=\s*["']([^"']+)["']
        [^>]+
        content\s*=\s*["'](.*?)["']
        [^>]*>
        |
        <meta
        [^>]+
        content\s*=\s*["'](.*?)["']
        [^>]+
        (?:
            property|name
        )
        \s*=\s*["']([^"']+)["']
        [^>]*>
        """
    )

    LINK_RE = re.compile(
        r"""(?is)
        <link
        [^>]*rel\s*=\s*["']([^"']+)["']
        [^>]*href\s*=\s*["'](.*?)["']
        [^>]*>
        """
    )

    def __init__(
        self,
        store: EvidenceStore,
    ):
        self.store = store

    # ----------------------------------------------------------
    # META
    # ----------------------------------------------------------

    def meta(
        self,
        text: str,
    ) -> dict[str, str]:

        result = {}

        for m in self.META_RE.finditer(text):

            a, b, c, d = m.groups()

            if a and b:
                key = clean(a).lower()
                value = clean(b)
            else:
                key = clean(d).lower()
                value = clean(c)

            if not key or not value:
                continue

            result[key] = value

            if key in {
                "og:url",
                "og:title",
                "og:description",
                "og:type",
                "al:web:url",
                "al:ios:url",
                "al:android:url",
            }:

                self.store.add(
                    key,
                    value,
                    "meta",
                    8,
                    independent="meta",
                )

        return result

    # ----------------------------------------------------------
    # CANONICAL
    # ----------------------------------------------------------

    def canonical(
        self,
        text: str,
    ) -> list[str]:

        result = []

        for m in self.LINK_RE.finditer(text):

            rel = clean(
                m.group(1)
            ).lower()

            href = clean(
                m.group(2)
            )

            if "canonical" not in rel:
                continue

            href = normalize(
                unquote(href)
            )

            if not href:
                continue

            result.append(href)

            self.store.add(
                "canonical_url",
                href,
                "canonical",
                32,
                direct=True,
                independent="canonical",
            )

        return result

    # ----------------------------------------------------------
    # JSON-LD
    # ----------------------------------------------------------

    def jsonld(
        self,
        text: str,
    ):

        scripts = re.findall(
            r"""(?is)
            <script[^>]+
            type=["']application/ld\+json["']
            [^>]*>
            (.*?)
            </script>
            """,
            text,
        )

        for body in scripts:

            try:
                data = json.loads(
                    html.unescape(body)
                )

            except Exception:
                continue

            self.walk_json(
                data,
                source="jsonld",
            )

    # ----------------------------------------------------------
    # JSON WALK
    # ----------------------------------------------------------

    def walk_json(
        self,
        obj: Any,
        *,
        source: str,
        depth: int = 0,
    ):

        if depth > 18:
            return

        if isinstance(obj, dict):

            for key, value in obj.items():

                key_l = str(
                    key
                ).lower()

                if isinstance(
                    value,
                    (
                        str,
                        int,
                        float,
                    ),
                ):
                    self.key_value(
                        key_l,
                        value,
                        source,
                    )

                elif isinstance(
                    value,
                    (dict, list),
                ):
                    self.walk_json(
                        value,
                        source=source,
                        depth=depth + 1,
                    )

        elif isinstance(obj, list):

            for value in obj[:1500]:
                self.walk_json(
                    value,
                    source=source,
                    depth=depth + 1,
                )

    # ----------------------------------------------------------
    # KEY VALUE
    # ----------------------------------------------------------

    def key_value(
        self,
        key: str,
        value: Any,
        source: str,
    ):

        value = clean(value)

        mapping = {
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
            "post_id": "post_id",
            "postid": "post_id",
            "story_fbid": "story_id",
            "storyfbid": "story_id",
            "media_fbid": "media_fbid",
            "mediafbid": "media_fbid",
            "video_id": "video_id",
            "videoid": "video_id",
            "photo_id": "photo_id",
            "photoid": "photo_id",
            "reel_id": "reel_id",
            "reelid": "reel_id",
        }

        field = mapping.get(key)

        if not field:
            return

        if numeric(value):

            score = {
                "group_id": 35,
                "page_id": 28,
                "profile_id": 24,
                "publisher_id": 24,
                "post_id": 24,
                "story_id": 22,
                "media_fbid": 22,
                "video_id": 22,
                "photo_id": 22,
                "reel_id": 22,
                "user_uid": 20,
                "actor_id": 15,
                "owner_id": 18,
                "entity_id": 16,
            }.get(field, 10)

            self.store.add(
                field,
                value,
                source,
                score,
                context=key,
                independent=source,
            )

        elif field in {
            "post_id",
            "story_id",
            "media_fbid",
            "video_id",
            "photo_id",
            "reel_id",
        } and opaque(value):

            self.store.add(
                field,
                value,
                source,
                18,
                context=key,
                independent=source,
            )

    # ----------------------------------------------------------
    # HTML / JS ID EXTRACTION
    # ----------------------------------------------------------

    def html_ids(
        self,
        text: str,
    ):

        patterns = {

            "user_uid": (
                r"(?:user_id|userID|userId)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),

            "profile_id": (
                r"(?:profile_id|profileID|profileId)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),

            "actor_id": (
                r"(?:actor_id|actorID|actorId)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),

            "page_id": (
                r"(?:page_id|pageID|pageId)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),

            "group_id": (
                r"(?:group_id|groupID|groupId)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),

            "owner_id": (
                r"(?:owner_id|ownerID|ownerId)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),

            "publisher_id": (
                r"(?:publisher_id|publisherID|publisherId)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),

            "entity_id": (
                r"(?:entity_id|entityID|entityId)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),

            "post_id": (
                r"(?:post_id|postID|postId)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),

            "story_id": (
                r"(?:story_fbid|storyFbid)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),

            "media_fbid": (
                r"(?:media_fbid|mediaFbid)"
                r"\s*[:=]\s*"
                r"""["']?(\d{5,30})"""
            ),
        }

        for field, pattern in patterns.items():

            try:
                matches = re.finditer(
                    pattern,
                    text,
                    re.I,
                )

            except re.error:
                continue

            for m in matches:

                value = m.group(1)

                if not numeric(value):
                    continue

                context = clean(
                    text[
                        max(
                            0,
                            m.start() - 500,
                        ):
                        min(
                            len(text),
                            m.end() + 500,
                        )
                    ]
                )

                self.store.add(
                    field,
                    value,
                    "html_js",
                    {
                        "group_id": 32,
                        "page_id": 27,
                        "profile_id": 24,
                        "publisher_id": 24,
                        "post_id": 23,
                        "story_id": 22,
                        "media_fbid": 22,
                        "user_uid": 21,
                        "owner_id": 19,
                        "actor_id": 16,
                        "entity_id": 16,
                    }.get(field, 10),
                    context=context,
                    independent="html_js",
                )

        # pfbid
        for token in PFID_RE.findall(text):

            self.store.add(
                "post_id",
                token,
                "pfbid",
                30,
                independent="pfbid",
            )

    # ----------------------------------------------------------
    # BASE64
    # ----------------------------------------------------------

    def base64_ids(
        self,
        text: str,
    ):

        candidates = set(
            re.findall(
                r"(?<![A-Za-z0-9+/=_-])"
                r"[A-Za-z0-9+/_=-]{20,180}"
                r"(?![A-Za-z0-9+/=_-])",
                text,
            )
        )

        for token in list(candidates)[
            :1200
        ]:

            decoded = self.decode64(
                token
            )

            if not decoded:
                continue

            low = decoded.lower()

            markers = (
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
            )

            if not any(
                marker in low
                for marker in markers
            ):
                continue

            field = None

            if "group_id" in low:
                field = "group_id"

            elif "page_id" in low:
                field = "page_id"

            elif "profile_id" in low:
                field = "profile_id"

            elif "user_id" in low:
                field = "user_uid"

            elif "publisher_id" in low:
                field = "publisher_id"

            elif "actor_id" in low:
                field = "actor_id"

            elif "owner_id" in low:
                field = "owner_id"

            elif "post_id" in low:
                field = "post_id"

            elif "media_fbid" in low:
                field = "media_fbid"

            if not field:
                continue

            for value in NUMERIC_ID_RE.findall(
                decoded
            ):

                self.store.add(
                    field,
                    value,
                    "base64",
                    8,
                    context=clean(
                        decoded[:800]
                    ),
                    independent="base64",
                )

    @staticmethod
    def decode64(
        token: str,
    ) -> str:

        token = token.replace(
            "-",
            "+",
        ).replace(
            "_",
            "/",
        )

        token = re.sub(
            r"[^A-Za-z0-9+/=]",
            "",
            token,
        )

        token += "=" * (
            (4 - len(token) % 4) % 4
        )

        try:

            raw = base64.b64decode(
                token,
                validate=False,
            )

            return raw.decode(
                "utf-8",
                errors="ignore",
            )

        except (
            ValueError,
            binascii.Error,
        ):
            return ""


# ==============================================================
# CORRELATION
# ==============================================================

class Correlator:

    def __init__(
        self,
        url_type: str,
        url_info: dict[str, str],
        store: EvidenceStore,
    ):

        self.url_type = url_type
        self.url_info = url_info
        self.store = store

    # ----------------------------------------------------------
    # MAIN
    # ----------------------------------------------------------

    def build(
        self,
        result: Result,
    ) -> Result:

        result.url_type = self.url_type

        self.structural(
            result
        )

        self.entity_type(
            result
        )

        # ======================================================
        # GROUP LOCK
        # ======================================================

        if result.entity_type in GROUP_TYPES:

            self.group(
                result
            )

            # Absolute protection.
            result.user_uid = None
            result.page_id = None
            result.publisher_id = (
                None
                if result.publisher_id
                == result.group_id
                else result.publisher_id
            )

        # ======================================================
        # PAGE
        # ======================================================

        elif result.entity_type == "PAGE":

            self.page(
                result
            )

            result.user_uid = None

        # ======================================================
        # USER
        # ======================================================

        elif result.entity_type == "USER":

            self.user(
                result
            )

        # ======================================================
        # GENERIC
        # ======================================================

        else:

            self.generic(
                result
            )

        self.objects(
            result
        )

        self.publisher(
            result
        )

        self.canonical(
            result
        )

        self.finalize(
            result
        )

        return result

    # ----------------------------------------------------------
    # STRUCTURAL
    # ----------------------------------------------------------

    def structural(
        self,
        result: Result,
    ):

        for key, value in self.url_info.items():

            if key == "group_id":
                result.group_id = value

            elif key == "post_id":
                result.post_id = value

            elif key == "publisher":
                result.publisher = value

            elif key == "profile_id":
                result.user_uid = value

            elif key == "video_id":
                result.video_id = value

            elif key == "photo_id":
                result.photo_id = value

            elif key == "reel_id":
                result.reel_id = value

            elif key == "story_id":
                result.story_id = value

            elif key == "share_token":
                result.share_token = value

    # ----------------------------------------------------------
    # ENTITY TYPE
    # ----------------------------------------------------------

    def entity_type(
        self,
        result: Result,
    ):

        if self.url_type in GROUP_TYPES:
            result.entity_type = (
                "GROUP_POST"
                if self.url_type
                == "GROUP_POST"
                else "GROUP"
            )
            return

        if self.url_type in PAGE_TYPES:
            result.entity_type = "PAGE"
            return

        if self.url_type in USER_TYPES:
            result.entity_type = "USER"
            return

        if self.url_type.startswith(
            "AMBIGUOUS_"
        ):
            result.entity_type = "UNKNOWN"
            return

        result.entity_type = self.url_type

    # ----------------------------------------------------------
    # GROUP
    # ----------------------------------------------------------

    def group(
        self,
        result: Result,
    ):

        if not result.group_id:

            candidate = self.best(
                "group_id",
                55,
            )

            if candidate:
                result.group_id = (
                    candidate.value
                )

        if not result.post_id:

            candidate = self.best(
                "post_id",
                45,
            )

            if candidate:
                result.post_id = (
                    candidate.value
                )

        if result.group_id:
            result.status = (
                "OBJECT_IDENTIFIED"
            )

        if (
            result.group_id
            and result.post_id
        ):
            result.verification = (
                "VERIFIED"
            )

    # ----------------------------------------------------------
    # PAGE
    # ----------------------------------------------------------

    def page(
        self,
        result: Result,
    ):

        candidate = self.best(
            "page_id",
            48,
        )

        if candidate:

            result.page_id = (
                candidate.value
            )

        if not result.page_id:

            candidate = self.best(
                "publisher_id",
                65,
            )

            if candidate:

                # Must have page evidence.
                if self.page_correlated(
                    candidate.value
                ):
                    result.page_id = (
                        candidate.value
                    )

        if result.page_id:

            result.status = (
                "OBJECT_IDENTIFIED"
            )

            result.verification = (
                "VERIFIED"
            )

    # ----------------------------------------------------------
    # USER
    # ----------------------------------------------------------

    def user(
        self,
        result: Result,
    ):

        fields = (
            "user_uid",
            "profile_id",
            "publisher_id",
            "owner_id",
            "actor_id",
        )

        ranked = []

        for field in fields:

            for candidate in self.store.candidates(
                field
            ):

                if self.conflicts(
                    candidate.value
                ):
                    continue

                ranked.append(
                    (
                        candidate.score,
                        field,
                        candidate,
                    )
                )

        ranked.sort(
            reverse=True,
            key=lambda x: x[0],
        )

        for score, field, candidate in ranked:

            if score < 60:
                continue

            # Explicit profile_id from profile URL
            # is already very strong.
            if (
                field == "profile_id"
                and self.url_type
                == "USER"
            ):
                result.user_uid = (
                    candidate.value
                )
                break

            # Normal publisher UID needs
            # independent confirmation.
            if candidate.score >= 70:

                if (
                    candidate.independent_sources
                    >= 2
                ):
                    result.user_uid = (
                        candidate.value
                    )
                    break

        if result.user_uid:

            result.status = (
                "OBJECT_IDENTIFIED"
            )

            result.verification = (
                "VERIFIED"
            )

    # ----------------------------------------------------------
    # GENERIC
    # ----------------------------------------------------------

    def generic(
        self,
        result: Result,
    ):

        # Try publisher first.
        for field in (
            "user_uid",
            "profile_id",
            "publisher_id",
            "owner_id",
        ):

            candidate = self.best(
                field,
                65,
            )

            if not candidate:
                continue

            if self.conflicts(
                candidate.value
            ):
                continue

            if (
                candidate.independent_sources
                >= 2
            ):
                result.user_uid = (
                    candidate.value
                )
                break

        if result.user_uid:
            result.status = (
                "OBJECT_IDENTIFIED"
            )

    # ----------------------------------------------------------
    # OBJECTS
    # ----------------------------------------------------------

    def objects(
        self,
        result: Result,
    ):

        mappings = (
            ("post_id", "post_id"),
            ("reel_id", "reel_id"),
            ("video_id", "video_id"),
            ("photo_id", "photo_id"),
            ("story_id", "story_id"),
            ("media_fbid", "media_fbid"),
        )

        for target, field_name in mappings:

            if getattr(
                result,
                target,
            ):
                continue

            candidate = self.best(
                field_name,
                40,
            )

            if candidate:

                setattr(
                    result,
                    target,
                    candidate.value,
                )

    # ----------------------------------------------------------
    # PUBLISHER
    # ----------------------------------------------------------

    def publisher(
        self,
        result: Result,
    ):

        if result.publisher:
            return

        candidate = self.best(
            "publisher",
            15,
        )

        if candidate:
            result.publisher = (
                candidate.value
            )

    # ----------------------------------------------------------
    # CANONICAL
    # ----------------------------------------------------------

    def canonical(
        self,
        result: Result,
    ):

        candidate = self.best(
            "canonical_url",
            30,
        )

        if candidate:
            result.canonical_url = (
                candidate.value
            )

    # ----------------------------------------------------------
    # PAGE CORRELATION
    # ----------------------------------------------------------

    def page_correlated(
        self,
        value: str,
    ) -> bool:

        sources = set()

        for field in (
            "page_id",
            "publisher_id",
            "actor_id",
            "owner_id",
        ):

            for evidence in (
                self.store.items
            ):

                if (
                    evidence.field
                    == field
                    and evidence.value
                    == value
                ):

                    sources.add(
                        evidence.independent
                        or evidence.source
                    )

        return len(sources) >= 2

    # ----------------------------------------------------------
    # CONFLICT
    # ----------------------------------------------------------

    def conflicts(
        self,
        value: str,
    ) -> bool:

        # Group ID always wins its own namespace.
        if self.store.has(
            "group_id",
            value,
        ):
            return True

        # A strongly established Page ID should
        # not become a User UID.
        page_candidates = (
            self.store.candidates(
                "page_id"
            )
        )

        for candidate in page_candidates:

            if (
                candidate.value == value
                and candidate.score >= 65
            ):
                return True

        return False

    # ----------------------------------------------------------
    # BEST
    # ----------------------------------------------------------

    def best(
        self,
        field: str,
        minimum: float,
    ) -> Optional[Candidate]:

        candidates = self.store.candidates(
            field
        )

        for candidate in candidates:

            if candidate.score >= minimum:
                return candidate

        return None

    # ----------------------------------------------------------
    # FINAL
    # ----------------------------------------------------------

    def finalize(
        self,
        result: Result,
    ):

        # ------------------------------------------------------
        # Hard namespace protection.
        # ------------------------------------------------------

        if result.entity_type in GROUP_TYPES:

            result.user_uid = None
            result.page_id = None

        if result.entity_type == "PAGE":

            result.user_uid = None

        # ------------------------------------------------------
        # Confidence.
        # ------------------------------------------------------

        score = 0

        if result.group_id:
            score += 35

        if result.page_id:
            score += 45

        if result.user_uid:
            score += 45

        if result.post_id:
            score += 15

        if result.publisher:
            score += 5

        if result.canonical_url:
            score += 5

        if result.verification == "VERIFIED":
            score += 10

        result.confidence = min(
            99,
            score,
        )

        if result.confidence >= 95:
            label = "VERY HIGH"
        elif result.confidence >= 85:
            label = "HIGH"
        elif result.confidence >= 70:
            label = "GOOD"
        elif result.confidence >= 50:
            label = "MEDIUM"
        else:
            label = "LOW"

        # Stored dynamically for formatter.
        result._confidence_label = label

        if (
            result.status == "FAILED"
            and (
                result.user_uid
                or result.page_id
                or result.group_id
                or result.post_id
                or result.video_id
                or result.reel_id
                or result.photo_id
                or result.story_id
            )
        ):
            result.status = (
                "OBJECT_IDENTIFIED"
            )


# ==============================================================
# RESOLVER
# ==============================================================

class FacebookResolver:

    def __init__(
        self,
        concurrency: int = MAX_CONCURRENT,
    ):

        self.http = HTTP(
            concurrency
        )

    async def resolve(
        self,
        url: str,
    ) -> Result:

        original = normalize(
            unwrap(url)
        )

        result = Result(
            input_url=original or url
        )

        if not original:

            result.error = (
                "URL Facebook không hợp lệ"
            )

            return result

        url_type, url_info = (
            URLParser.parse(
                original
            )
        )

        store = EvidenceStore()

        # ------------------------------------------------------
        # Structural evidence
        # ------------------------------------------------------

        for field_name, value in (
            url_info.items()
        ):

            scores = {
                "group_id": 50,
                "post_id": 40,
                "profile_id": 50,
                "publisher": 25,
                "video_id": 35,
                "photo_id": 35,
                "reel_id": 35,
                "story_id": 35,
                "share_token": 30,
            }

            store.add(
                field_name,
                value,
                "url_structure",
                scores.get(
                    field_name,
                    20,
                ),
                direct=True,
                independent="url_structure",
            )

        # Publisher isn't originally a dedicated
        # extractor field in all paths.
        if "publisher" in url_info:
            store.add(
                "publisher",
                url_info["publisher"],
                "url_structure",
                25,
                direct=True,
                independent="url_structure",
            )

        result.resolved_url = original

        # ------------------------------------------------------
        # HTTP crawl
        # ------------------------------------------------------

        snapshots: list[Snapshot] = []

        try:

            async with httpx.AsyncClient(
                timeout=self.http.timeout,
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=10,
                    max_keepalive_connections=5,
                ),
                http2=False,
            ) as client:

                first = await self.http.get(
                    client,
                    original,
                )

                if first:
                    snapshots.append(first)

                    result.resolved_url = (
                        normalize(
                            first.final_url
                        )
                        or first.final_url
                    )

                # ------------------------------------------------
                # First-pass extraction.
                # ------------------------------------------------

                discovered = []

                for snap in snapshots:

                    extractor = Extractor(
                        store
                    )

                    meta = extractor.meta(
                        snap.text
                    )

                    discovered.extend(
                        extractor.canonical(
                            snap.text
                        )
                    )

                    extractor.jsonld(
                        snap.text
                    )

                    extractor.html_ids(
                        snap.text
                    )

                    extractor.base64_ids(
                        snap.text
                    )

                    # OG URL.
                    og = meta.get(
                        "og:url"
                    )

                    if og:

                        og = normalize(
                            unquote(og)
                        )

                        if og:
                            store.add(
                                "canonical_url",
                                og,
                                "og:url",
                                35,
                                direct=True,
                                independent="og:url",
                            )

                            discovered.append(
                                og
                            )

                    # Search Facebook URLs in page.
                    for found in extract_urls(
                        snap.text
                    ):

                        if found != original:
                            discovered.append(
                                found
                            )

                # ------------------------------------------------
                # Canonical / publisher candidates.
                # ------------------------------------------------

                targets = []

                for candidate in discovered:

                    candidate = normalize(
                        candidate
                    )

                    if not candidate:
                        continue

                    if candidate == original:
                        continue

                    if candidate in targets:
                        continue

                    targets.append(
                        candidate
                    )

                # First crawl canonical/object URLs.
                targets = targets[
                    :MAX_CRAWL
                ]

                if targets:

                    fetched = await asyncio.gather(
                        *[
                            self.http.get(
                                client,
                                target,
                            )
                            for target in targets
                        ],
                        return_exceptions=True,
                    )

                    for item in fetched:

                        if isinstance(
                            item,
                            Snapshot,
                        ):
                            snapshots.append(
                                item
                            )

                # ------------------------------------------------
                # SECONDARY publisher discovery.
                #
                # This is the important part for:
                #
                # /username/posts/pfbid...
                #
                # We search the returned HTML for a
                # profile/page URL and crawl it.
                # ------------------------------------------------

                profile_targets = []

                for snap in snapshots:

                    found_urls = (
                        extract_urls(
                            snap.text
                        )
                    )

                    for candidate in found_urls:

                        ctype, cinfo = (
                            URLParser.parse(
                                candidate
                            )
                        )

                        if ctype in {
                            "PROFILE_CANDIDATE",
                            "PROFILE",
                            "USER",
                            "PAGE",
                        }:

                            if candidate not in (
                                profile_targets
                            ):
                                profile_targets.append(
                                    candidate
                                )

                profile_targets = (
                    profile_targets[
                        :MAX_PROFILE_CRAWL
                    ]
                )

                if profile_targets:

                    fetched = await asyncio.gather(
                        *[
                            self.http.get(
                                client,
                                target,
                            )
                            for target in profile_targets
                        ],
                        return_exceptions=True,
                    )

                    for item in fetched:

                        if isinstance(
                            item,
                            Snapshot,
                        ):
                            snapshots.append(
                                item
                            )

        except Exception as exc:

            LOGGER.debug(
                "Resolver error: %r",
                exc,
            )

        # ------------------------------------------------------
        # Parse ALL snapshots again.
        # ------------------------------------------------------

        for snap in snapshots:

            extractor = Extractor(
                store
            )

            meta = extractor.meta(
                snap.text
            )

            extractor.canonical(
                snap.text
            )

            extractor.jsonld(
                snap.text
            )

            extractor.html_ids(
                snap.text
            )

            extractor.base64_ids(
                snap.text
            )

            # --------------------------------------------------
            # Profile URL / username correlations.
            # --------------------------------------------------

            for found in extract_urls(
                snap.text
            ):

                ctype, cinfo = (
                    URLParser.parse(
                        found
                    )
                )

                publisher = cinfo.get(
                    "publisher"
                )

                if publisher and USERNAME_RE.match(
                    publisher
                ):

                    store.add(
                        "publisher",
                        publisher,
                        "embedded_profile_url",
                        12,
                        independent=(
                            "embedded_profile_url"
                        ),
                    )

                profile_id = cinfo.get(
                    "profile_id"
                )

                if numeric(profile_id):

                    store.add(
                        "profile_id",
                        profile_id,
                        "embedded_profile_url",
                        35,
                        direct=True,
                        independent=(
                            "embedded_profile_url"
                        ),
                    )

        # ------------------------------------------------------
        # Determine canonical.
        # ------------------------------------------------------

        canonical_candidates = (
            store.candidates(
                "canonical_url"
            )
        )

        # Refine ambiguous URL based on canonical.
        final_type = url_type
        final_info = dict(
            url_info
        )

        for candidate in canonical_candidates:

            ctype, cinfo = (
                URLParser.parse(
                    candidate.value
                )
            )

            if ctype in GROUP_TYPES:

                final_type = ctype
                final_info.update(
                    cinfo
                )
                break

            if ctype in PAGE_TYPES:

                final_type = ctype
                final_info.update(
                    cinfo
                )
                break

            if (
                ctype in USER_TYPES
                and final_type.startswith(
                    "AMBIGUOUS_"
                )
            ):

                final_type = ctype
                final_info.update(
                    cinfo
                )

                break

        # ------------------------------------------------------
        # Correlation.
        # ------------------------------------------------------

        result = Correlator(
            final_type,
            final_info,
            store,
        ).build(
            result
        )

        # ------------------------------------------------------
        # Evidence retained internally.
        # ------------------------------------------------------

        result.evidence = sorted(
            store.items,
            key=lambda e: e.score,
            reverse=True,
        )

        # ------------------------------------------------------
        # Final error.
        # ------------------------------------------------------

        if (
            result.status == "FAILED"
            and not result.error
        ):
            result.error = (
                "Không tìm thấy ID public đủ mạnh"
            )

        return result

    async def resolve_many(
        self,
        urls: Iterable[str],
    ) -> list[Result]:

        unique = []

        for url in urls:

            normalized = normalize(
                unwrap(url)
            )

            if (
                normalized
                and normalized not in unique
            ):
                unique.append(
                    normalized
                )

        if not unique:
            return []

        return await asyncio.gather(
            *[
                self.resolve(url)
                for url in unique
            ]
        )


# ==============================================================
# OUTPUT
# ==============================================================

def esc(value: Any) -> str:
    return html.escape(
        str(value),
        quote=False,
    )


def icon(entity: str) -> str:

    if entity in GROUP_TYPES:
        return "👥"

    if entity == "PAGE":
        return "📄"

    if entity == "USER":
        return "👤"

    if "VIDEO" in entity:
        return "🎬"

    if "REEL" in entity:
        return "🎞"

    if "PHOTO" in entity:
        return "📷"

    if "STORY" in entity:
        return "📖"

    return "📝"


def pretty_type(
    entity: str,
) -> str:

    return {
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
    }.get(
        entity,
        entity or "UNKNOWN",
    )


def format_result(
    result: Result,
    index: int,
) -> str:

    lines = [
        f"<b>{index}.</b> "
        f"{icon(result.entity_type)} "
        f"<b>{esc(pretty_type(result.entity_type))}</b>"
    ]

    # ----------------------------------------------------------
    # USER UID
    # ----------------------------------------------------------

    if result.user_uid:

        lines.append(
            "🆔 <b>USER UID:</b> "
            f"<code>{esc(result.user_uid)}</code>"
        )

    # ----------------------------------------------------------
    # PAGE UID
    # ----------------------------------------------------------

    if result.page_id:

        lines.append(
            "📄 <b>PAGE UID:</b> "
            f"<code>{esc(result.page_id)}</code>"
        )

    # ----------------------------------------------------------
    # GROUP
    # ----------------------------------------------------------

    if result.group_id:

        lines.append(
            "👥 <b>GROUP ID:</b> "
            f"<code>{esc(result.group_id)}</code>"
        )

    # ----------------------------------------------------------
    # POST
    # ----------------------------------------------------------

    if result.post_id:

        lines.append(
            "📝 <b>POST ID:</b> "
            f"<code>{esc(result.post_id)}</code>"
        )

    # ----------------------------------------------------------
    # MEDIA
    # ----------------------------------------------------------

    if result.reel_id:

        lines.append(
            "🎞 <b>REEL ID:</b> "
            f"<code>{esc(result.reel_id)}</code>"
        )

    if result.video_id:

        lines.append(
            "🎬 <b>VIDEO ID:</b> "
            f"<code>{esc(result.video_id)}</code>"
        )

    if result.photo_id:

        lines.append(
            "📷 <b>PHOTO ID:</b> "
            f"<code>{esc(result.photo_id)}</code>"
        )

    if result.story_id:

        lines.append(
            "📖 <b>STORY ID:</b> "
            f"<code>{esc(result.story_id)}</code>"
        )

    if result.media_fbid:

        lines.append(
            "🖼 <b>MEDIA FBID:</b> "
            f"<code>{esc(result.media_fbid)}</code>"
        )

    # ----------------------------------------------------------
    # PUBLISHER
    # ----------------------------------------------------------

    if result.publisher:

        lines.append(
            "👤 <b>PUBLISHER:</b> "
            f"<code>{esc(result.publisher)}</code>"
        )

    # ----------------------------------------------------------
    # CANONICAL
    # ----------------------------------------------------------

    if result.canonical_url:

        lines.append(
            "🌐 <b>CANONICAL:</b> "
            f'<a href="{esc(result.canonical_url)}">'
            f"{esc(result.canonical_url)}"
            "</a>"
        )

    # ----------------------------------------------------------
    # STATUS
    # ----------------------------------------------------------

    if result.status:

        lines.append(
            "📊 <b>STATUS:</b> "
            f"{esc(result.status)}"
        )

    # ----------------------------------------------------------
    # CONFIDENCE
    # ----------------------------------------------------------

    if result.confidence:

        label = getattr(
            result,
            "_confidence_label",
            "",
        )

        lines.append(
            "🎯 <b>CONFIDENCE:</b> "
            f"{result.confidence}%"
            + (
                f" {esc(label)}"
                if label
                else ""
            )
        )

    # ----------------------------------------------------------
    # WARNING ONLY IF REAL
    # ----------------------------------------------------------

    if result.warnings:

        lines.append(
            "⚠️ <b>WARNING:</b> "
            + " • ".join(
                esc(x)
                for x in result.warnings
            )
        )

    # ----------------------------------------------------------
    # ERROR ONLY IF FAILED
    # ----------------------------------------------------------

    if (
        result.status == "FAILED"
        and result.error
    ):

        lines.append(
            "❌ <b>ERROR:</b> "
            f"{esc(result.error)}"
        )

    return "\n".join(lines)


# ==============================================================
# TELEGRAM REGISTER
# ==============================================================

def register(
    client,
    *args,
    **kwargs,
):

    """
    Compatible:

        module.register(client)

    or:

        module.register(client, notify_bot)

    or other existing loader signatures.
    """

    resolver = FacebookResolver()

    @client.on(
        events.NewMessage(
            pattern=r"(?i)^/getuidfb(?:@\w+)?(?:\s+[\s\S]*)?$"
        )
    )
    async def getuidfb_handler(
        event,
    ):

        text = event.raw_text or ""

        payload = re.sub(
            r"(?i)^/getuidfb(?:@\w+)?",
            "",
            text,
            count=1,
        ).strip()

        urls = extract_urls(
            payload
        )

        if not urls:

            await event.respond(
                HELP_TEXT,
                parse_mode="html",
            )

            return

        progress = await event.respond(
            "🔎 <b>FACEBOOK UID V17.5</b>\n"
            "⏳ Đang phân tích publisher "
            "+ numeric UID...",
            parse_mode="html",
        )

        started = time.perf_counter()

        try:

            results = await resolver.resolve_many(
                urls
            )

        except Exception as exc:

            LOGGER.exception(
                "getuidfb exception"
            )

            await progress.edit(
                "❌ <b>Resolver Error</b>\n"
                f"<code>{esc(exc)}</code>",
                parse_mode="html",
            )

            return

        elapsed = int(
            (
                time.perf_counter()
                - started
            ) * 1000
        )

        success = sum(
            x.status != "FAILED"
            for x in results
        )

        failed = (
            len(results)
            - success
        )

        blocks = [
            "<b>╭──────────────────────────────╮</b>",
            "│ 🔎 <b>FACEBOOK UID V17.5</b>",
            "<b>╰──────────────────────────────╯</b>",
            f"📊 <b>TOTAL:</b> {len(results)}",
            f"✅ <b>SUCCESS:</b> {success}",
            f"❌ <b>FAILED:</b> {failed}",
            "",
        ]

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

            if index < len(results):
                blocks.append(
                    "━━━━━━━━━━━━━━━━━━━━"
                )

        blocks.extend(
            [
                "",
                f"⏱️ <b>TIME:</b> {elapsed} ms",
            ]
        )

        await progress.edit(
            "\n".join(blocks),
            parse_mode="html",
            link_preview=False,
        )

    # Allow other modules/tests to access
    # the same resolver instance.
    try:
        client.facebook_uid_resolver = (
            resolver
        )
    except Exception:
        pass

    return resolver


# ==============================================================
# PROGRAMMATIC API
# ==============================================================

async def resolve_facebook_url(
    url: str,
) -> Result:

    resolver = FacebookResolver()

    return await resolver.resolve(
        url
    )


async def resolve_facebook_urls(
    urls: Iterable[str],
) -> list[Result]:

    resolver = FacebookResolver()

    return await resolver.resolve_many(
        urls
    )


# ==============================================================
# CLI TEST
# ==============================================================

if __name__ == "__main__":

    import sys

    async def main():

        if len(sys.argv) < 2:

            print(
                "Usage:\n"
                "python getuidfb.py "
                "<facebook_url>"
            )

            return

        resolver = FacebookResolver()

        result = await resolver.resolve(
            sys.argv[1]
        )

        output = {
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
            "publisher": result.publisher,
            "canonical_url":
                result.canonical_url,
            "confidence":
                result.confidence,
            "verification":
                result.verification,
        }

        print(
            json.dumps(
                output,
                ensure_ascii=False,
                indent=2,
            )
        )

    asyncio.run(
        main()
    )