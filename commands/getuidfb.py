#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
======================================================================
 FB UID / ENTITY RESOLVER V20 ULTRA
======================================================================

Public HTTP only
- NO Playwright
- NO Selenium
- NO Cookie
- NO Access Token
- NO Facebook login

Mục tiêu:
- Ưu tiên USER UID numeric có bằng chứng mạnh
- Không coi pfbid / share token là USER UID
- Tách USER / PAGE / GROUP
- Tách USER_POST / PAGE_POST / GROUP_POST
- POST / REEL / VIDEO / PHOTO / STORY / PROFILE
- Hỗ trợ:
    /share/p/...
    /share/v/...
    /share/r/...
    /pfbid...
    /posts/...
    /reel/...
    /video/...
    /watch?v=...
    /photo/...
    /photos/...
    /story.php?story_fbid=...
    /profile.php?id=...
- Tự tách nhiều URL kể cả URL dính liền:
    https://facebook.com/share/p/xxx/https://facebook.com/user/posts/yyy
- Redirect -> canonical -> OG -> JSON-LD -> HTML/JS
- Numeric semantic ID extraction
- Profile correlation
- Publisher correlation
- Conflict detection
- Evidence scoring
- Interactive /getuidfb loop
- Mỗi URL xử lý độc lập
- Giữ nguyên module command structure

Nguyên tắc quan trọng:

    pfbid/share token
            ↓
       resolved URL
            ↓
        object/post
            ↓
        publisher
            ↓
       public profile
            ↓
       numeric UID
            ↓
       independent verification

Không thể chứng minh UID:
    => KHÔNG đoán UID.
======================================================================
"""

import asyncio
import html
import json
import re
import time

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import (
    urlparse,
    parse_qs,
    unquote,
    urljoin,
)

import requests
from telethon import events


# ======================================================================
# COMMAND INFO
# ======================================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": "Facebook UID / Entity Resolver V20 ULTRA",
    "category": "facebook",
}


# ======================================================================
# CONFIG
# ======================================================================

REQUEST_TIMEOUT = 15

MAX_HTML_SIZE = 12 * 1024 * 1024

MAX_REDIRECTS = 8

PROFILE_VERIFY_TIMEOUT = 10

MAX_PROFILE_CHECKS = 5

USER_AGENT_LIST = [
    (
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
]

COMMON_HEADERS = {
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

FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "mobile.facebook.com",
    "fb.watch",
}

FACEBOOK_URL_START_RE = re.compile(
    r"(?i)"
    r"(?:https?://"
    r"(?:www\.)?"
    r"(?:facebook\.com|m\.facebook\.com|"
    r"mbasic\.facebook\.com|web\.facebook\.com|"
    r"mobile\.facebook\.com|fb\.watch)"
    r"|"
    r"(?<![\w./])"
    r"(?:www\.)?"
    r"(?:facebook\.com|m\.facebook\.com|"
    r"mbasic\.facebook\.com|web\.facebook\.com|"
    r"mobile\.facebook\.com|fb\.watch)"
    r")"
)

NUMERIC_RE = re.compile(r"(?<!\d)\d{5,25}(?!\d)")

PFBID_RE = re.compile(
    r"(?i)\bpfbid[A-Za-z0-9_-]+\b"
)

SHARE_TOKEN_RE = re.compile(
    r"(?i)/share/(?:p|v|r)/([A-Za-z0-9_-]+)"
)

POST_PATH_RE = re.compile(
    r"(?i)/posts/(?:pfbid)?([A-Za-z0-9_-]+)"
)

USERNAME_RE = re.compile(
    r"^[A-Za-z0-9._-]{2,100}$"
)

SEMANTIC_KEY_RE = re.compile(
    r"(?i)"
    r"\b("
    r"user[_-]?id|"
    r"profile[_-]?id|"
    r"owner[_-]?id|"
    r"publisher[_-]?id|"
    r"page[_-]?id|"
    r"group[_-]?id|"
    r"actor[_-]?id|"
    r"entity[_-]?id|"
    r"post[_-]?id|"
    r"story[_-]?fbid|"
    r"media[_-]?fbid|"
    r"video[_-]?id|"
    r"photo[_-]?id|"
    r"fbid"
    r")"
    r"\s*[:=]\s*[\"']?(\d{5,25})"
)

META_RE = re.compile(
    r"<meta\b([^>]*?)>",
    re.I | re.S,
)

ATTR_RE = re.compile(
    r"""([:\w-]+)\s*=\s*["'](.*?)["']""",
    re.I | re.S,
)

TITLE_RE = re.compile(
    r"<title[^>]*>(.*?)</title>",
    re.I | re.S,
)

LINK_RE = re.compile(
    r"<link\b([^>]*?)>",
    re.I | re.S,
)

ANCHOR_RE = re.compile(
    r"<a\b([^>]*?)>",
    re.I | re.S,
)

SCRIPT_RE = re.compile(
    r"<script\b([^>]*)>(.*?)</script>",
    re.I | re.S,
)

HTML_TAG_RE = re.compile(r"<[^>]+>")


# ======================================================================
# DATA CLASSES
# ======================================================================

@dataclass
class ParsedURL:
    original: str
    normalized: str
    host: str
    path: str
    query: dict = field(default_factory=dict)

    url_type: str = "UNKNOWN"
    route_entity: str = "UNKNOWN"

    username: str = ""
    numeric_route_id: str = ""

    opaque_token: str = ""

    is_share: bool = False
    is_redirect_wrapper: bool = False


@dataclass
class Snapshot:
    requested_url: str

    final_url: str = ""
    canonical_url: str = ""

    status_code: int = 0
    title: str = ""

    html_text: str = ""

    meta: dict = field(default_factory=dict)

    links: list = field(default_factory=list)

    jsonld: list = field(default_factory=list)

    errors: list = field(default_factory=list)

    redirects: list = field(default_factory=list)


@dataclass
class Evidence:
    value: str
    role: str
    source: str
    weight: float

    context: str = ""

    object_token: str = ""
    key: str = ""

    verified: bool = False
    independent_key: str = ""


@dataclass
class Candidate:
    value: str

    score: float = 0.0

    sources: list = field(default_factory=list)

    independent_sources: set = field(default_factory=set)

    contexts: list = field(default_factory=list)

    verified: bool = False

    rejected: bool = False


@dataclass
class ResolveResult:
    original_url: str

    normalized_url: str = ""
    resolved_url: str = ""
    canonical_url: str = ""

    url_type: str = "UNKNOWN"
    entity_type: str = "UNKNOWN"
    publisher_type: str = "UNKNOWN"

    user_uid: str = ""
    page_uid: str = ""
    group_id: str = ""

    publisher_id: str = ""
    publisher_name: str = ""
    username: str = ""

    post_id: str = ""
    video_id: str = ""
    photo_id: str = ""
    story_id: str = ""

    opaque_token: str = ""

    title: str = ""

    confidence: int = 0

    status: str = "UNRESOLVED"

    warnings: list = field(default_factory=list)

    evidence_count: int = 0

    elapsed: float = 0.0


# ======================================================================
# HTML PARSER
# ======================================================================

class FacebookHTMLParser(HTMLParser):

    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )

        self.meta = {}

        self.links = []

        self.jsonld_raw = []

        self.title_parts = []

        self.in_title = False

        self.in_script = False

        self.current_script_attrs = {}

        self.current_script_parts = []

    def handle_starttag(self, tag, attrs):

        tag = tag.lower()

        attrs_dict = {
            str(k).lower(): str(v)
            for k, v in attrs
            if v is not None
        }

        if tag == "title":
            self.in_title = True

        elif tag == "meta":

            key = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or attrs_dict.get("itemprop")
                or ""
            ).lower()

            content = attrs_dict.get(
                "content",
                ""
            )

            if key:
                self.meta[key] = html.unescape(
                    content
                )

        elif tag == "link":

            href = attrs_dict.get("href")

            if href:
                self.links.append(href)

        elif tag == "a":

            href = attrs_dict.get("href")

            if href:
                self.links.append(href)

        elif tag == "script":

            self.in_script = True

            self.current_script_attrs = attrs_dict

            self.current_script_parts = []

    def handle_endtag(self, tag):

        tag = tag.lower()

        if tag == "title":

            self.in_title = False

        elif tag == "script":

            if (
                self.current_script_attrs.get(
                    "type",
                    ""
                ).lower()
                == "application/ld+json"
            ):

                self.jsonld_raw.append(
                    "".join(
                        self.current_script_parts
                    )
                )

            self.in_script = False

            self.current_script_attrs = {}

            self.current_script_parts = []

    def handle_data(self, data):

        if self.in_title:
            self.title_parts.append(data)

        if self.in_script:
            self.current_script_parts.append(data)


# ======================================================================
# GENERAL HELPERS
# ======================================================================

def clean_text(value):

    if value is None:
        return ""

    value = html.unescape(
        str(value)
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def normalize_numeric(value):

    if value is None:
        return ""

    value = str(value).strip()

    if not value.isdigit():
        return ""

    value = value.lstrip("0")

    if len(value) < 5:
        return ""

    return value


def is_probable_numeric_id(value):

    value = normalize_numeric(value)

    if not value:
        return False

    if len(value) < 5:
        return False

    if len(value) > 25:
        return False

    # Các số kiểu timestamp rất dễ gây false positive.
    if value.startswith(
        (
            "1900",
            "2000",
            "2020",
            "2021",
            "2022",
            "2023",
            "2024",
            "2025",
            "2026",
        )
    ) and len(value) >= 10:

        return False

    return True


def same_numeric(a, b):

    a = normalize_numeric(a)
    b = normalize_numeric(b)

    return bool(a and b and a == b)


def host_is_facebook(host):

    host = (
        host or ""
    ).lower().split(":")[0]

    return (
        host in FACEBOOK_HOSTS
        or host.endswith(".facebook.com")
    )


def normalize_url(url):

    if not url:
        return ""

    url = html.unescape(
        url.strip()
    )

    url = url.strip(
        " \t\r\n<>\"'`"
    )

    url = url.rstrip(
        ".,;!?)]}"
    )

    if not re.match(
        r"(?i)^https?://",
        url
    ):

        url = "https://" + url

    return url


def safe_parse_url(url):

    try:

        parsed = urlparse(
            normalize_url(url)
        )

        return parsed

    except Exception:

        return None


# ======================================================================
# FACEBOOK URL EXTRACTION
# ======================================================================

def extract_facebook_urls(text):

    """
    Cực kỳ quan trọng:

    Input:
        https://facebook.com/share/p/AAA/https://facebook.com/user/posts/BBB

    Output:
        [
            https://facebook.com/share/p/AAA,
            https://facebook.com/user/posts/BBB
        ]

    Không dùng regex kiểu "https://... hết dòng"
    vì sẽ nuốt URL kế tiếp.
    """

    if not text:
        return []

    text = html.unescape(
        str(text)
    )

    matches = list(
        FACEBOOK_URL_START_RE.finditer(
            text
        )
    )

    if not matches:
        return []

    results = []

    for index, match in enumerate(matches):

        start = match.start()

        if index + 1 < len(matches):

            end = matches[
                index + 1
            ].start()

        else:

            end = len(text)

        chunk = text[
            start:end
        ]

        # Cắt tại ký tự phân cách thông thường.
        cut = re.search(
            r"""[\s<>"'`]+""",
            chunk
        )

        if cut:

            chunk = chunk[
                :cut.start()
            ]

        chunk = chunk.strip()

        chunk = chunk.rstrip(
            ".,;!?)]}"
        )

        if not chunk:
            continue

        chunk = normalize_url(
            chunk
        )

        parsed = safe_parse_url(
            chunk
        )

        if not parsed:
            continue

        if not host_is_facebook(
            parsed.netloc
        ):
            continue

        results.append(
            chunk
        )

    # Deduplicate nhưng giữ thứ tự.
    output = []

    seen = set()

    for url in results:

        key = url.rstrip("/").lower()

        if key in seen:
            continue

        seen.add(key)

        output.append(url)

    return output


# ======================================================================
# URL CLASSIFICATION
# ======================================================================

def classify_url(url):

    normalized = normalize_url(
        url
    )

    parsed = safe_parse_url(
        normalized
    )

    if not parsed:

        return ParsedURL(
            original=url,
            normalized=normalized,
            host="",
            path="",
        )

    host = (
        parsed.netloc
        .lower()
        .split(":")[0]
    )

    path = (
        unquote(
            parsed.path or ""
        )
        .strip()
    )

    path_lower = path.lower()

    query_raw = parse_qs(
        parsed.query,
        keep_blank_values=True
    )

    query = {}

    for key, values in query_raw.items():

        if values:

            query[key.lower()] = values[-1]

    result = ParsedURL(
        original=url,
        normalized=normalized,
        host=host,
        path=path,
        query=query,
    )

    # --------------------------------------------------------------
    # PROFILE
    # --------------------------------------------------------------

    if path_lower.rstrip(
        "/"
    ).endswith(
        "/profile.php"
    ):

        result.url_type = "PROFILE"

        result.route_entity = "USER"

        result.numeric_route_id = (
            normalize_numeric(
                query.get("id")
            )
        )

        return result

    # --------------------------------------------------------------
    # GROUP
    # --------------------------------------------------------------

    group_match = re.search(
        r"/groups/([^/?#]+)",
        path,
        re.I
    )

    if group_match:

        result.route_entity = "GROUP"

        token = group_match.group(1)

        if token.isdigit():

            result.numeric_route_id = (
                normalize_numeric(token)
            )

        if re.search(
            r"/groups/[^/]+/(?:posts?|permalink|permalink\.php)",
            path,
            re.I
        ):

            result.url_type = "GROUP_POST"

        elif re.search(
            r"/groups/[^/]+/(?:videos?|reels?|photos?|media)",
            path,
            re.I
        ):

            result.url_type = "GROUP_MEDIA"

        else:

            result.url_type = "GROUP"

        return result

    # --------------------------------------------------------------
    # SHARE
    # --------------------------------------------------------------

    share_match = re.search(
        r"/share/(p|v|r)/([^/?#]+)",
        path,
        re.I
    )

    if share_match:

        share_kind = (
            share_match.group(1)
            .lower()
        )

        token = share_match.group(2)

        result.is_share = True

        result.opaque_token = token

        if share_kind == "p":

            result.url_type = "POST"

        elif share_kind == "v":

            result.url_type = "VIDEO"

        else:

            result.url_type = "REEL"

        return result

    # --------------------------------------------------------------
    # WATCH
    # --------------------------------------------------------------

    if (
        path_lower.rstrip("/")
        == "/watch"
    ):

        result.url_type = "VIDEO"

        result.opaque_token = (
            query.get("v", "")
        )

        return result

    # --------------------------------------------------------------
    # PHOTO.PHP
    # --------------------------------------------------------------

    if path_lower.rstrip(
        "/"
    ).endswith(
        "/photo.php"
    ):

        result.url_type = "PHOTO"

        result.opaque_token = (
            query.get("fbid", "")
            or query.get("set", "")
        )

        return result

    # --------------------------------------------------------------
    # STORY.PHP
    # --------------------------------------------------------------

    if path_lower.rstrip(
        "/"
    ).endswith(
        "/story.php"
    ):

        result.url_type = "STORY"

        result.opaque_token = (
            query.get("story_fbid", "")
            or query.get("id", "")
        )

        return result

    # --------------------------------------------------------------
    # REEL
    # --------------------------------------------------------------

    if re.search(
        r"/(?:reel|reels)/",
        path,
        re.I
    ):

        result.url_type = "REEL"

        result.opaque_token = (
            path.rstrip("/").split("/")[-1]
        )

        return result

    # --------------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------------

    if re.search(
        r"/(?:video|videos)/",
        path,
        re.I
    ):

        result.url_type = "VIDEO"

        result.opaque_token = (
            path.rstrip("/").split("/")[-1]
        )

        return result

    # --------------------------------------------------------------
    # PHOTO
    # --------------------------------------------------------------

    if re.search(
        r"/(?:photo|photos)/",
        path,
        re.I
    ):

        result.url_type = "PHOTO"

        result.opaque_token = (
            path.rstrip("/").split("/")[-1]
        )

        return result

    # --------------------------------------------------------------
    # POSTS
    # --------------------------------------------------------------

    if re.search(
        r"/posts/",
        path,
        re.I
    ):

        result.url_type = "POST"

        result.opaque_token = (
            path.rstrip("/").split("/")[-1]
        )

        parts = [
            p for p in path.split("/")
            if p
        ]

        if parts:

            possible_user = parts[0]

            if USERNAME_RE.match(
                possible_user
            ):

                result.username = (
                    possible_user
                )

        return result

    # --------------------------------------------------------------
    # /p/
    # --------------------------------------------------------------

    if re.search(
        r"/p/",
        path,
        re.I
    ):

        result.url_type = "POST"

        result.opaque_token = (
            path.rstrip("/").split("/")[-1]
        )

        return result

    # --------------------------------------------------------------
    # PAGE ROUTE
    # --------------------------------------------------------------

    page_match = re.search(
        r"/pages/([^/]+)/(\d+)",
        path,
        re.I
    )

    if page_match:

        result.route_entity = "PAGE"

        result.numeric_route_id = (
            normalize_numeric(
                page_match.group(2)
            )
        )

        result.url_type = "PAGE"

        return result

    # --------------------------------------------------------------
    # USERNAME ROUTE
    # --------------------------------------------------------------

    parts = [
        p for p in path.split("/")
        if p
    ]

    if parts:

        first = parts[0]

        if USERNAME_RE.match(first):

            if first.lower() not in {
                "home",
                "watch",
                "groups",
                "pages",
                "marketplace",
                "gaming",
                "events",
                "reels",
                "videos",
                "photos",
                "photo",
                "stories",
            }:

                result.username = first

                result.route_entity = "UNKNOWN"

                result.url_type = "PROFILE"

                return result

    result.url_type = "UNKNOWN"

    return result


# ======================================================================
# META / JSON-LD
# ======================================================================

def parse_html_snapshot(
    requested_url,
    status_code,
    final_url,
    html_text,
    redirects=None,
):

    parser = FacebookHTMLParser()

    try:

        parser.feed(
            html_text
        )

    except Exception:
        pass

    title = clean_text(
        " ".join(
            parser.title_parts
        )
    )

    jsonld = []

    for raw in parser.jsonld_raw:

        raw = raw.strip()

        if not raw:
            continue

        try:

            data = json.loads(
                raw
            )

            jsonld.append(data)

        except Exception:

            # Facebook đôi khi chứa JSON-LD
            # không chuẩn hoàn toàn.
            continue

    return Snapshot(
        requested_url=requested_url,
        final_url=final_url,
        status_code=status_code,
        title=title,
        html_text=html_text,
        meta=parser.meta,
        links=parser.links,
        jsonld=jsonld,
        redirects=redirects or [],
    )


# ======================================================================
# JSON-LD WALKER
# ======================================================================

def walk_jsonld(value):

    if isinstance(
        value,
        dict
    ):

        yield value

        for child in value.values():

            yield from walk_jsonld(
                child
            )

    elif isinstance(
        value,
        list
    ):

        for child in value:

            yield from walk_jsonld(
                child
            )


def jsonld_author_objects(
    snapshot
):

    output = []

    for root in snapshot.jsonld:

        for obj in walk_jsonld(root):

            author = obj.get(
                "author"
            )

            if isinstance(
                author,
                dict
            ):

                output.append(
                    author
                )

            elif isinstance(
                author,
                list
            ):

                output.extend(
                    x for x in author
                    if isinstance(x, dict)
                )

            publisher = obj.get(
                "publisher"
            )

            if isinstance(
                publisher,
                dict
            ):

                output.append(
                    publisher
                )

    return output


# ======================================================================
# HTTP FETCHER
# ======================================================================

class HTTPFetcher:

    def __init__(self):

        self.session = requests.Session()

        self.session.max_redirects = (
            MAX_REDIRECTS
        )

    def fetch(
        self,
        url,
        timeout=REQUEST_TIMEOUT,
    ):

        errors = []

        for ua_index, ua in enumerate(
            USER_AGENT_LIST
        ):

            headers = dict(
                COMMON_HEADERS
            )

            headers["User-Agent"] = ua

            try:

                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=True,
                    stream=True,
                )

                status = (
                    response.status_code
                )

                # Retry rate limit / server errors.
                if status in {
                    403,
                    429,
                    500,
                    502,
                    503,
                    504,
                }:

                    response.close()

                    if ua_index + 1 < len(
                        USER_AGENT_LIST
                    ):

                        time.sleep(
                            0.35 + (
                                ua_index * 0.25
                            )
                        )

                        continue

                chunks = []

                total = 0

                for chunk in response.iter_content(
                    chunk_size=65536
                ):

                    if not chunk:
                        continue

                    total += len(chunk)

                    if total > MAX_HTML_SIZE:

                        break

                    chunks.append(chunk)

                content = b"".join(
                    chunks
                )

                response.close()

                encoding = (
                    response.encoding
                    or "utf-8"
                )

                text = content.decode(
                    encoding,
                    errors="replace"
                )

                return {
                    "status": status,
                    "url": response.url,
                    "text": text,
                    "history": [
                        r.url
                        for r in response.history
                    ],
                    "headers": dict(
                        response.headers
                    ),
                    "error": "",
                }

            except Exception as exc:

                errors.append(
                    str(exc)
                )

                if ua_index + 1 < len(
                    USER_AGENT_LIST
                ):

                    time.sleep(
                        0.25
                    )

        return {
            "status": 0,
            "url": url,
            "text": "",
            "history": [],
            "headers": {},
            "error": (
                errors[-1]
                if errors
                else "HTTP error"
            ),
        }


# ======================================================================
# REDIRECT / WRAPPER UNWRAP
# ======================================================================

def unwrap_redirect_url(url):

    if not url:
        return url

    current = url

    for _ in range(4):

        parsed = safe_parse_url(
            current
        )

        if not parsed:
            break

        query = parsed.query

        qs = parse_qs(
            query,
            keep_blank_values=True
        )

        target = ""

        for key in (
            "u",
            "url",
            "target",
            "redirect",
            "redirect_url",
            "dest",
            "destination",
        ):

            values = qs.get(
                key
            )

            if values:

                candidate = unquote(
                    values[-1]
                )

                candidate_parsed = (
                    safe_parse_url(
                        candidate
                    )
                )

                if candidate_parsed:

                    target = candidate

                    break

        if not target:
            break

        if target == current:
            break

        current = target

    return current


# ======================================================================
# EVIDENCE STORE
# ======================================================================

class EvidenceStore:

    def __init__(self):

        self.items = []

    def add(
        self,
        value,
        role,
        source,
        weight,
        context="",
        object_token="",
        key="",
        independent_key="",
    ):

        value = str(
            value or ""
        ).strip()

        if not value:
            return

        self.items.append(
            Evidence(
                value=value,
                role=role,
                source=source,
                weight=float(weight),
                context=clean_text(
                    context
                )[:1200],
                object_token=object_token,
                key=key,
                independent_key=(
                    independent_key
                    or source
                ),
            )
        )

    def values_for_role(
        self,
        role
    ):

        return [
            x for x in self.items
            if x.role == role
        ]

    def count(self):

        return len(
            self.items
        )


# ======================================================================
# SEMANTIC ID EXTRACTION
# ======================================================================

SEMANTIC_ALIASES = {
    "user_id": "USER",
    "userid": "USER",
    "user_id": "USER",

    "profile_id": "USER",
    "profileid": "USER",

    "owner_id": "USER",
    "ownerid": "USER",

    "publisher_id": "PUBLISHER",
    "publisherid": "PUBLISHER",

    "page_id": "PAGE",
    "pageid": "PAGE",

    "group_id": "GROUP",
    "groupid": "GROUP",

    "actor_id": "ACTOR",
    "actorid": "ACTOR",

    "entity_id": "ENTITY",
    "entityid": "ENTITY",

    "post_id": "POST",
    "postid": "POST",

    "story_fbid": "STORY",
    "storyfbid": "STORY",

    "media_fbid": "MEDIA",
    "mediafbid": "MEDIA",

    "video_id": "VIDEO",
    "videoid": "VIDEO",

    "photo_id": "PHOTO",
    "photoid": "PHOTO",

    "fbid": "FBID",
}


def extract_semantic_ids(
    text,
    store,
    object_token="",
    source="html",
):

    if not text:
        return

    # --------------------------------------------------------------
    # key = numeric
    # --------------------------------------------------------------

    for match in SEMANTIC_KEY_RE.finditer(
        text
    ):

        raw_key = (
            match.group(1)
            .lower()
            .replace("-", "")
            .replace("_", "")
        )

        value = normalize_numeric(
            match.group(2)
        )

        if not is_probable_numeric_id(
            value
        ):
            continue

        role = SEMANTIC_ALIASES.get(
            raw_key,
            ""
        )

        if not role:
            continue

        # Weight theo độ tin cậy của key.
        weights = {
            "USER": 10,
            "PUBLISHER": 9,
            "PAGE": 10,
            "GROUP": 10,
            "POST": 8,
            "STORY": 8,
            "MEDIA": 6,
            "VIDEO": 8,
            "PHOTO": 8,
            "ACTOR": 4,
            "ENTITY": 3,
            "FBID": 2,
        }

        context = text[
            max(
                0,
                match.start() - 220
            ):
            min(
                len(text),
                match.end() + 220
            )
        ]

        store.add(
            value=value,
            role=role,
            source=source,
            weight=weights.get(
                role,
                2
            ),
            context=context,
            object_token=object_token,
            key=raw_key,
            independent_key=(
                source + ":" + raw_key
            ),
        )


def extract_numeric_context(
    text,
    store,
    object_token="",
    source="html",
):

    if not text:
        return

    # Chỉ lấy numeric ID khi gần các semantic marker.
    markers = [
        "user",
        "profile",
        "owner",
        "publisher",
        "actor",
        "page",
        "group",
        "post",
        "story",
        "video",
        "photo",
        "fbid",
    ]

    lower = text.lower()

    for marker in markers:

        start = 0

        while True:

            pos = lower.find(
                marker,
                start
            )

            if pos < 0:
                break

            window_start = max(
                0,
                pos - 180
            )

            window_end = min(
                len(text),
                pos + 300
            )

            window = text[
                window_start:window_end
            ]

            for number_match in (
                NUMERIC_RE.finditer(
                    window
                )
            ):

                value = normalize_numeric(
                    number_match.group()
                )

                if not is_probable_numeric_id(
                    value
                ):
                    continue

                marker_role = (
                    "USER"
                    if marker in {
                        "user",
                        "profile",
                        "owner",
                        "publisher",
                    }
                    else (
                        "PAGE"
                        if marker == "page"
                        else (
                            "GROUP"
                            if marker == "group"
                            else (
                                "POST"
                                if marker == "post"
                                else (
                                    "VIDEO"
                                    if marker == "video"
                                    else (
                                        "PHOTO"
                                        if marker == "photo"
                                        else (
                                            "STORY"
                                            if marker == "story"
                                            else "CONTEXT"
                                        )
                                    )
                                )
                            )
                        )
                    )
                )

                weight = {
                    "USER": 5,
                    "PAGE": 5,
                    "GROUP": 5,
                    "POST": 4,
                    "VIDEO": 4,
                    "PHOTO": 4,
                    "STORY": 4,
                    "CONTEXT": 1,
                }.get(
                    marker_role,
                    1
                )

                store.add(
                    value=value,
                    role=marker_role,
                    source=source + "_context",
                    weight=weight,
                    context=window,
                    object_token=object_token,
                    independent_key=(
                        source + ":context:" + marker
                    ),
                )

            start = pos + len(marker)


# ======================================================================
# URL BASED EVIDENCE
# ======================================================================

def extract_url_evidence(
    parsed_url,
    store
):

    # --------------------------------------------------------------
    # profile.php?id=UID
    # --------------------------------------------------------------

    if (
        parsed_url.url_type
        == "PROFILE"
        and parsed_url.numeric_route_id
    ):

        store.add(
            parsed_url.numeric_route_id,
            "USER",
            "profile_query",
            18,
            context=parsed_url.normalized,
            independent_key="profile_query_id",
        )

    # --------------------------------------------------------------
    # GROUP
    # --------------------------------------------------------------

    if (
        parsed_url.route_entity
        == "GROUP"
        and parsed_url.numeric_route_id
    ):

        store.add(
            parsed_url.numeric_route_id,
            "GROUP",
            "group_route",
            18,
            context=parsed_url.path,
            independent_key="group_route_id",
        )

    # --------------------------------------------------------------
    # pfbid / share token
    # --------------------------------------------------------------

    if parsed_url.opaque_token:

        token = parsed_url.opaque_token

        if PFBID_RE.fullmatch(
            token
        ):

            store.add(
                token,
                "OPAQUE",
                "url_pfbid",
                1,
                context=parsed_url.normalized,
                independent_key="url_pfbid",
            )

        elif len(token) >= 5:

            store.add(
                token,
                "OPAQUE",
                "url_token",
                1,
                context=parsed_url.normalized,
                independent_key="url_token",
            )


# ======================================================================
# PROFILE URL DISCOVERY
# ======================================================================

def normalize_profile_reference(
    value,
    base_url=""
):

    if not value:
        return ""

    value = html.unescape(
        str(value).strip()
    )

    if value.startswith(
        ("//", "/")
    ):

        value = urljoin(
            base_url,
            value
        )

    if not re.match(
        r"(?i)^https?://",
        value
    ):

        return ""

    parsed = safe_parse_url(
        value
    )

    if not parsed:
        return ""

    if not host_is_facebook(
        parsed.netloc
    ):
        return ""

    return value


def discover_profile_urls(
    parsed_url,
    snapshot
):

    profiles = []

    # --------------------------------------------------------------
    # Input username
    # --------------------------------------------------------------

    if parsed_url.username:

        profiles.extend([
            "https://www.facebook.com/"
            + parsed_url.username,
            "https://m.facebook.com/"
            + parsed_url.username,
        ])

    # --------------------------------------------------------------
    # Canonical / OG
    # --------------------------------------------------------------

    for candidate in [
        snapshot.canonical_url,
        snapshot.meta.get("og:url", ""),
        snapshot.meta.get(
            "al:ios:url",
            ""
        ),
        snapshot.meta.get(
            "al:android:url",
            ""
        ),
    ]:

        candidate = normalize_profile_reference(
            candidate,
            snapshot.final_url
        )

        if candidate:
            profiles.append(
                candidate
            )

    # --------------------------------------------------------------
    # article:author
    # --------------------------------------------------------------

    for key in (
        "article:author",
        "author",
        "profile:url",
    ):

        value = snapshot.meta.get(
            key,
            ""
        )

        if value:

            candidate = normalize_profile_reference(
                value,
                snapshot.final_url
            )

            if candidate:

                profiles.append(
                    candidate
                )

    # --------------------------------------------------------------
    # HTML links
    # --------------------------------------------------------------

    for href in snapshot.links:

        href = normalize_profile_reference(
            href,
            snapshot.final_url
        )

        if not href:
            continue

        parsed = safe_parse_url(
            href
        )

        if not parsed:
            continue

        p = parsed.path.lower()

        if (
            "/profile.php" in p
            or "/groups/" in p
            or "/pages/" in p
            or "/posts/" in p
            or "/reel/" in p
            or "/video/" in p
            or "/photo/" in p
            or "/photos/" in p
            or "/watch" in p
        ):

            continue

        parts = [
            x for x in parsed.path.split("/")
            if x
        ]

        if len(parts) == 1:

            name = parts[0]

            if USERNAME_RE.match(
                name
            ):

                profiles.append(
                    href
                )

    # --------------------------------------------------------------
    # Dedup
    # --------------------------------------------------------------

    result = []

    seen = set()

    for value in profiles:

        key = value.rstrip(
            "/"
        ).lower()

        if key in seen:
            continue

        seen.add(key)

        result.append(
            value
        )

    return result[:MAX_PROFILE_CHECKS]


# ======================================================================
# PROFILE ID EXTRACTION
# ======================================================================

def extract_profile_id_from_url(
    url
):

    parsed = safe_parse_url(
        url
    )

    if not parsed:
        return ""

    qs = parse_qs(
        parsed.query,
        keep_blank_values=True
    )

    # profile.php?id=
    if (
        parsed.path.lower()
        .rstrip("/")
        .endswith("/profile.php")
    ):

        value = (
            qs.get("id", [""])[-1]
        )

        if is_probable_numeric_id(
            value
        ):

            return normalize_numeric(
                value
            )

    # ?id= on facebook profile-like URL
    value = (
        qs.get("id", [""])[-1]
    )

    if is_probable_numeric_id(
        value
    ):

        return normalize_numeric(
            value
        )

    return ""


def extract_profile_username(
    url
):

    parsed = safe_parse_url(
        url
    )

    if not parsed:
        return ""

    path_parts = [
        x for x in parsed.path.split("/")
        if x
    ]

    if len(path_parts) != 1:
        return ""

    username = path_parts[0]

    if USERNAME_RE.match(
        username
    ):

        return username

    return ""


# ======================================================================
# PROFILE VERIFICATION
# ======================================================================

def verify_profile(
    fetcher,
    profile_url,
):

    result = {
        "uid": "",
        "username": "",
        "canonical": "",
        "title": "",
        "status": 0,
        "strong": False,
        "evidence": [],
    }

    profile_url = normalize_url(
        profile_url
    )

    if not profile_url:
        return result

    fetched = fetcher.fetch(
        profile_url,
        timeout=PROFILE_VERIFY_TIMEOUT,
    )

    result["status"] = fetched.get(
        "status",
        0
    )

    if not fetched.get(
        "text"
    ):

        return result

    snapshot = parse_html_snapshot(
        requested_url=profile_url,
        status_code=fetched.get(
            "status",
            0
        ),
        final_url=fetched.get(
            "url",
            profile_url
        ),
        html_text=fetched.get(
            "text",
            ""
        ),
        redirects=fetched.get(
            "history",
            []
        ),
    )

    final_url = snapshot.final_url

    result["canonical"] = (
        snapshot.meta.get(
            "og:url",
            ""
        )
        or snapshot.final_url
    )

    result["title"] = snapshot.title

    # --------------------------------------------------------------
    # Direct semantic IDs
    # --------------------------------------------------------------

    temp = EvidenceStore()

    extract_semantic_ids(
        snapshot.html_text,
        temp,
        source="profile_verify"
    )

    # profile query
    uid = extract_profile_id_from_url(
        final_url
    )

    if uid:

        result["uid"] = uid

        result["evidence"].append(
            (
                uid,
                "final_profile_query",
                20
            )
        )

    # canonical query
    canonical_uid = extract_profile_id_from_url(
        result["canonical"]
    )

    if canonical_uid:

        if not result["uid"]:

            result["uid"] = canonical_uid

        if same_numeric(
            result["uid"],
            canonical_uid
        ):

            result["strong"] = True

        result["evidence"].append(
            (
                canonical_uid,
                "canonical_profile_query",
                18
            )
        )

    # semantic USER / PROFILE evidence
    user_candidates = [
        e for e in temp.items
        if e.role in {
            "USER",
            "PUBLISHER",
        }
    ]

    if user_candidates:

        best = sorted(
            user_candidates,
            key=lambda x: x.weight,
            reverse=True
        )[0]

        if not result["uid"]:

            result["uid"] = (
                best.value
            )

        if same_numeric(
            result["uid"],
            best.value
        ):

            result["strong"] = True

        result["evidence"].append(
            (
                best.value,
                best.source,
                best.weight
            )
        )

    result["username"] = (
        extract_profile_username(
            final_url
        )
        or extract_profile_username(
            result["canonical"]
        )
        or extract_profile_username(
            profile_url
        )
    )

    # --------------------------------------------------------------
    # If final URL has a clear username and OG URL matches,
    # this is strong profile correlation.
    # --------------------------------------------------------------

    requested_username = (
        extract_profile_username(
            profile_url
        )
    )

    if (
        requested_username
        and result["username"]
        and requested_username.lower()
        == result["username"].lower()
    ):

        result["strong"] = True

    return result


# ======================================================================
# CANDIDATE SCORING
# ======================================================================

def score_user_candidates(
    store,
    parsed_url,
    profile_verifications,
):

    candidates = {}

    def get(value):

        value = normalize_numeric(
            value
        )

        if not value:
            return None

        if not is_probable_numeric_id(
            value
        ):
            return None

        if value not in candidates:

            candidates[value] = Candidate(
                value=value
            )

        return candidates[value]

    # --------------------------------------------------------------
    # First pass
    # --------------------------------------------------------------

    for evidence in store.items:

        if evidence.role not in {
            "USER",
            "PUBLISHER",
            "ACTOR",
        }:

            continue

        candidate = get(
            evidence.value
        )

        if not candidate:
            continue

        candidate.score += (
            evidence.weight
        )

        candidate.sources.append(
            evidence.source
        )

        candidate.independent_sources.add(
            evidence.independent_key
            or evidence.source
        )

        if evidence.context:

            candidate.contexts.append(
                evidence.context
            )

    # --------------------------------------------------------------
    # Profile verification
    # --------------------------------------------------------------

    for profile in profile_verifications:

        uid = normalize_numeric(
            profile.get("uid", "")
        )

        if not uid:
            continue

        candidate = get(
            uid
        )

        if not candidate:
            continue

        candidate.score += 20

        candidate.sources.append(
            "verified_profile"
        )

        candidate.independent_sources.add(
            "verified_profile"
        )

        if profile.get(
            "strong"
        ):

            candidate.score += 15

            candidate.verified = True

    # --------------------------------------------------------------
    # Input profile.php?id=
    # --------------------------------------------------------------

    if (
        parsed_url.url_type
        == "PROFILE"
        and parsed_url.numeric_route_id
    ):

        candidate = get(
            parsed_url.numeric_route_id
        )

        if candidate:

            candidate.score += 25

            candidate.sources.append(
                "direct_profile_url"
            )

            candidate.independent_sources.add(
                "direct_profile_url"
            )

            candidate.verified = True

    # --------------------------------------------------------------
    # Candidate with many independent signals
    # --------------------------------------------------------------

    for candidate in candidates.values():

        independent_count = len(
            candidate.independent_sources
        )

        if independent_count >= 2:

            candidate.score += 8

        if independent_count >= 3:

            candidate.score += 8

        if independent_count >= 4:

            candidate.score += 8

    return candidates


# ======================================================================
# OBJECT ID COLLECTION
# ======================================================================

def collect_object_ids(
    parsed_url,
    snapshot,
    store,
):

    token = (
        parsed_url.opaque_token
    )

    text = snapshot.html_text

    # --------------------------------------------------------------
    # Semantic IDs
    # --------------------------------------------------------------

    extract_semantic_ids(
        text,
        store,
        object_token=token,
        source="html_semantic"
    )

    # --------------------------------------------------------------
    # Contextual IDs
    # --------------------------------------------------------------

    extract_numeric_context(
        text,
        store,
        object_token=token,
        source="html"
    )

    # --------------------------------------------------------------
    # OG metadata
    # --------------------------------------------------------------

    for key, value in snapshot.meta.items():

        if not value:
            continue

        extract_semantic_ids(
            value,
            store,
            object_token=token,
            source="meta:" + key
        )

        # og:url may contain numeric ID.
        if key == "og:url":

            numeric_ids = NUMERIC_RE.findall(
                value
            )

            for numeric in numeric_ids:

                numeric = normalize_numeric(
                    numeric
                )

                if is_probable_numeric_id(
                    numeric
                ):

                    store.add(
                        numeric,
                        "URL_NUMERIC",
                        "og_url",
                        2,
                        context=value,
                        object_token=token,
                        independent_key="og_url_numeric",
                    )

    # --------------------------------------------------------------
    # Canonical
    # --------------------------------------------------------------

    if snapshot.canonical_url:

        numeric_ids = NUMERIC_RE.findall(
            snapshot.canonical_url
        )

        for numeric in numeric_ids:

            numeric = normalize_numeric(
                numeric
            )

            if is_probable_numeric_id(
                numeric
            ):

                store.add(
                    numeric,
                    "URL_NUMERIC",
                    "canonical_url",
                    4,
                    context=snapshot.canonical_url,
                    object_token=token,
                    independent_key="canonical_numeric",
                )

    # --------------------------------------------------------------
    # JSON-LD
    # --------------------------------------------------------------

    for author in jsonld_author_objects(
        snapshot
    ):

        author_url = author.get(
            "url",
            ""
        )

        author_id = author.get(
            "@id",
            ""
        )

        author_name = clean_text(
            author.get(
                "name",
                ""
            )
        )

        for value in (
            author_url,
            author_id,
        ):

            if not value:
                continue

            value = str(
                value
            )

            uid = extract_profile_id_from_url(
                value
            )

            if uid:

                store.add(
                    uid,
                    "PUBLISHER",
                    "jsonld_author_url",
                    13,
                    context=(
                        author_name
                        + " "
                        + value
                    ),
                    object_token=token,
                    independent_key=(
                        "jsonld_author_url"
                    ),
                )

            extract_semantic_ids(
                value,
                store,
                object_token=token,
                source="jsonld_author"
            )

    # --------------------------------------------------------------
    # Raw links
    # --------------------------------------------------------------

    for href in snapshot.links:

        if not href:
            continue

        href = html.unescape(
            href
        )

        uid = extract_profile_id_from_url(
            href
        )

        if uid:

            store.add(
                uid,
                "USER",
                "profile_link",
                11,
                context=href,
                object_token=token,
                independent_key="profile_link",
            )

    # --------------------------------------------------------------
    # Important:
    # Không lấy toàn bộ numeric ID trong HTML
    # làm UID.
    # --------------------------------------------------------------

    return


# ======================================================================
# PAGE / GROUP CANDIDATES
# ======================================================================

def collect_page_group_ids(
    store
):

    pages = {}

    groups = {}

    for evidence in store.items:

        if evidence.role == "PAGE":

            pages.setdefault(
                evidence.value,
                0
            )

            pages[
                evidence.value
            ] += evidence.weight

        elif evidence.role == "GROUP":

            groups.setdefault(
                evidence.value,
                0
            )

            groups[
                evidence.value
            ] += evidence.weight

    return pages, groups


# ======================================================================
# POST / VIDEO / PHOTO / STORY IDs
# ======================================================================

def choose_object_id(
    store,
    role,
):

    scores = {}

    for evidence in store.items:

        if evidence.role != role:
            continue

        value = evidence.value

        # Numeric only for object ID.
        # Opaque token xử lý riêng.
        if not value.isdigit():
            continue

        scores.setdefault(
            value,
            0
        )

        scores[value] += (
            evidence.weight
        )

    if not scores:
        return ""

    return max(
        scores,
        key=scores.get
    )


# ======================================================================
# PUBLISHER / ENTITY INFERENCE
# ======================================================================

def infer_entity(
    parsed_url,
    store,
    page_ids,
    group_ids,
    user_candidates,
):

    # --------------------------------------------------------------
    # GROUP route = authoritative
    # --------------------------------------------------------------

    if (
        parsed_url.route_entity
        == "GROUP"
    ):

        return (
            "GROUP_POST"
            if "POST" in parsed_url.url_type
            else "GROUP"
        )

    # --------------------------------------------------------------
    # Explicit GROUP evidence
    # --------------------------------------------------------------

    if group_ids:

        if parsed_url.url_type in {
            "POST",
            "VIDEO",
            "REEL",
            "PHOTO",
            "STORY",
            "UNKNOWN",
        }:

            return "GROUP_POST"

    # --------------------------------------------------------------
    # Explicit PAGE route / PAGE evidence
    # --------------------------------------------------------------

    if (
        parsed_url.route_entity
        == "PAGE"
    ):

        return "PAGE_POST"

    if page_ids:

        if parsed_url.url_type in {
            "POST",
            "VIDEO",
            "REEL",
            "PHOTO",
            "STORY",
        }:

            return "PAGE_POST"

    # --------------------------------------------------------------
    # User route
    # --------------------------------------------------------------

    if parsed_url.username:

        return (
            "USER_POST"
            if parsed_url.url_type
            in {
                "POST",
                "VIDEO",
                "REEL",
                "PHOTO",
                "STORY",
            }
            else "USER"
        )

    # --------------------------------------------------------------
    # Strong user candidate
    # --------------------------------------------------------------

    verified_users = [
        c for c in user_candidates.values()
        if c.verified
        and not c.rejected
    ]

    if verified_users:

        return (
            "USER_POST"
            if parsed_url.url_type
            in {
                "POST",
                "VIDEO",
                "REEL",
                "PHOTO",
                "STORY",
            }
            else "USER"
        )

    # --------------------------------------------------------------
    # Generic fallback
    # --------------------------------------------------------------

    if parsed_url.url_type in {
        "POST",
        "VIDEO",
        "REEL",
        "PHOTO",
        "STORY",
    }:

        return "POST"

    return parsed_url.url_type


# ======================================================================
# PUBLISHER NAME / USERNAME
# ======================================================================

def infer_username(
    parsed_url,
    snapshot,
):

    if parsed_url.username:

        return parsed_url.username

    # canonical
    for url in [
        snapshot.canonical_url,
        snapshot.meta.get(
            "og:url",
            ""
        ),
        snapshot.final_url,
    ]:

        username = extract_profile_username(
            url
        )

        if username:

            return username

    # Links
    for href in snapshot.links:

        username = extract_profile_username(
            href
        )

        if username:

            return username

    return ""


def infer_publisher_name(
    snapshot
):

    # OG title is often "Name | Facebook".
    title = clean_text(
        snapshot.meta.get(
            "og:title",
            ""
        )
    )

    if title:

        title = re.sub(
            r"\s*\|\s*Facebook.*$",
            "",
            title,
            flags=re.I
        )

        title = re.sub(
            r"\s*-\s*Facebook.*$",
            "",
            title,
            flags=re.I
        )

        if title.strip():

            return title.strip()

    # JSON-LD
    for author in jsonld_author_objects(
        snapshot
    ):

        name = clean_text(
            author.get(
                "name",
                ""
            )
        )

        if name:

            return name

    return ""


# ======================================================================
# CONFLICT DETECTION
# ======================================================================

def select_verified_user(
    candidates,
    entity_type,
):

    usable = [
        c for c in candidates.values()
        if not c.rejected
    ]

    if not usable:
        return None, "NO_CANDIDATE"

    usable.sort(
        key=lambda c: (
            c.verified,
            c.score,
            len(c.independent_sources),
        ),
        reverse=True
    )

    best = usable[0]

    if len(usable) == 1:

        return best, "OK"

    second = usable[1]

    # Nếu 2 candidate có score gần nhau
    # => không đoán.
    if (
        best.score < 35
        and (
            best.score
            - second.score
        ) < 12
    ):

        return None, "AMBIGUOUS"

    if (
        best.score
        - second.score
        < 8
        and
        len(best.independent_sources)
        <= len(second.independent_sources) + 1
    ):

        return None, "CONFLICT"

    # PAGE/GROUP tuyệt đối không được
    # trở thành USER UID.
    if entity_type.startswith(
        "GROUP"
    ):

        if not best.verified:

            return None, "GROUP_USER_UNVERIFIED"

    return best, "OK"


# ======================================================================
# MAIN RESOLVER
# ======================================================================

class FacebookResolver:

    def __init__(self):

        self.fetcher = HTTPFetcher()

    async def fetch_snapshot(
        self,
        url
    ):

        result = await asyncio.to_thread(
            self.fetcher.fetch,
            url
        )

        return parse_html_snapshot(
            requested_url=url,
            status_code=result.get(
                "status",
                0
            ),
            final_url=result.get(
                "url",
                url
            ),
            html_text=result.get(
                "text",
                ""
            ),
            redirects=result.get(
                "history",
                []
            ),
        ), result

    async def resolve(
        self,
        url
    ):

        started = time.perf_counter()

        parsed_url = classify_url(
            url
        )

        result = ResolveResult(
            original_url=url,
            normalized_url=(
                parsed_url.normalized
            ),
            url_type=(
                parsed_url.url_type
            ),
        )

        if not host_is_facebook(
            parsed_url.host
        ):

            result.status = "INVALID"

            result.warnings.append(
                "Không phải URL Facebook."
            )

            result.elapsed = (
                time.perf_counter()
                - started
            )

            return result

        # ----------------------------------------------------------
        # URL evidence
        # ----------------------------------------------------------

        store = EvidenceStore()

        extract_url_evidence(
            parsed_url,
            store
        )

        # ----------------------------------------------------------
        # Unwrap redirect
        # ----------------------------------------------------------

        unwrapped = unwrap_redirect_url(
            parsed_url.normalized
        )

        if unwrapped != (
            parsed_url.normalized
        ):

            result.warnings.append(
                "URL có redirect wrapper."
            )

        # ----------------------------------------------------------
        # First HTTP request
        # ----------------------------------------------------------

        snapshot, fetch_info = (
            await self.fetch_snapshot(
                unwrapped
            )
        )

        if fetch_info.get(
            "error"
        ):

            result.warnings.append(
                "HTTP: "
                + str(
                    fetch_info.get(
                        "error"
                    )
                )[:180]
            )

        result.resolved_url = (
            snapshot.final_url
            or unwrapped
        )

        result.canonical_url = (
            snapshot.meta.get(
                "og:url",
                ""
            )
            or snapshot.canonical_url
            or result.resolved_url
        )

        # ----------------------------------------------------------
        # Redirect evidence
        # ----------------------------------------------------------

        if snapshot.redirects:

            result.warnings.append(
                "Facebook đã redirect URL."
            )

        # ----------------------------------------------------------
        # Parse canonical
        # ----------------------------------------------------------

        canonical = (
            snapshot.meta.get(
                "og:url",
                ""
            )
            or ""
        )

        if canonical:

            canonical = normalize_url(
                canonical
            )

            snapshot.canonical_url = (
                canonical
            )

        # ----------------------------------------------------------
        # Collect object IDs
        # ----------------------------------------------------------

        collect_object_ids(
            parsed_url,
            snapshot,
            store
        )

        # ----------------------------------------------------------
        # Detect opaque token from final URL
        # ----------------------------------------------------------

        final_parsed = classify_url(
            result.resolved_url
        )

        if (
            final_parsed.opaque_token
        ):

            result.opaque_token = (
                final_parsed.opaque_token
            )

        elif parsed_url.opaque_token:

            result.opaque_token = (
                parsed_url.opaque_token
            )

        # pfbid anywhere
        if not result.opaque_token:

            pfbid_match = PFBID_RE.search(
                snapshot.html_text
            )

            if pfbid_match:

                result.opaque_token = (
                    pfbid_match.group()
                )

        # ----------------------------------------------------------
        # If final URL route gives better type
        # ----------------------------------------------------------

        if (
            final_parsed.url_type
            not in {
                "UNKNOWN",
                "PROFILE",
            }
        ):

            # Don't blindly replace GROUP/PAGE semantics.
            if (
                parsed_url.route_entity
                not in {
                    "GROUP",
                    "PAGE",
                }
            ):

                result.url_type = (
                    final_parsed.url_type
                )

        # ----------------------------------------------------------
        # Page / Group
        # ----------------------------------------------------------

        page_ids, group_ids = (
            collect_page_group_ids(
                store
            )
        )

        # Direct route group ID
        if (
            parsed_url.route_entity
            == "GROUP"
            and parsed_url.numeric_route_id
        ):

            group_ids.setdefault(
                parsed_url.numeric_route_id,
                0
            )

            group_ids[
                parsed_url.numeric_route_id
            ] += 25

        # ----------------------------------------------------------
        # Discover public profiles
        # ----------------------------------------------------------

        profile_urls = discover_profile_urls(
            parsed_url,
            snapshot
        )

        profile_verifications = []

        for profile_url in profile_urls:

            verification = await asyncio.to_thread(
                verify_profile,
                self.fetcher,
                profile_url
            )

            if verification.get(
                "uid"
            ):

                profile_verifications.append(
                    verification
                )

            # Small limit
            if len(
                profile_verifications
            ) >= MAX_PROFILE_CHECKS:

                break

        # ----------------------------------------------------------
        # User candidate scoring
        # ----------------------------------------------------------

        user_candidates = (
            score_user_candidates(
                store,
                parsed_url,
                profile_verifications
            )
        )

        # ----------------------------------------------------------
        # Entity
        # ----------------------------------------------------------

        entity_type = infer_entity(
            parsed_url,
            store,
            page_ids,
            group_ids,
            user_candidates
        )

        result.entity_type = (
            entity_type
        )

        # ----------------------------------------------------------
        # Publisher type
        # ----------------------------------------------------------

        if entity_type.startswith(
            "GROUP"
        ):

            result.publisher_type = (
                "GROUP"
            )

        elif entity_type.startswith(
            "PAGE"
        ):

            result.publisher_type = (
                "PAGE"
            )

        elif entity_type.startswith(
            "USER"
        ):

            result.publisher_type = (
                "USER"
            )

        # ----------------------------------------------------------
        # GROUP ID
        # ----------------------------------------------------------

        if group_ids:

            result.group_id = max(
                group_ids,
                key=group_ids.get
            )

        # ----------------------------------------------------------
        # PAGE UID
        # ----------------------------------------------------------

        if page_ids:

            result.page_uid = max(
                page_ids,
                key=page_ids.get
            )

        # ----------------------------------------------------------
        # Object IDs
        # ----------------------------------------------------------

        result.post_id = (
            choose_object_id(
                store,
                "POST"
            )
        )

        result.video_id = (
            choose_object_id(
                store,
                "VIDEO"
            )
        )

        result.photo_id = (
            choose_object_id(
                store,
                "PHOTO"
            )
        )

        result.story_id = (
            choose_object_id(
                store,
                "STORY"
            )
        )

        # ----------------------------------------------------------
        # If no numeric post ID:
        # Keep opaque token visible.
        # ----------------------------------------------------------

        if (
            not result.post_id
            and result.opaque_token
            and result.url_type
            in {
                "POST",
                "VIDEO",
                "REEL",
                "PHOTO",
                "STORY",
                "GROUP_POST",
            }
        ):

            result.post_id = (
                result.opaque_token
            )

        # ----------------------------------------------------------
        # USER UID
        # ----------------------------------------------------------

        # For PAGE:
        # page UID must NEVER be copied to user_uid.
        if entity_type.startswith(
            "PAGE"
        ):

            result.user_uid = ""

        elif entity_type.startswith(
            "GROUP"
        ):

            # Group ID không phải USER UID.
            selected, reason = (
                select_verified_user(
                    user_candidates,
                    entity_type
                )
            )

            if selected:

                result.user_uid = (
                    selected.value
                )

                result.publisher_id = (
                    selected.value
                )

            else:

                result.user_uid = ""

                if reason in {
                    "AMBIGUOUS",
                    "CONFLICT",
                    "GROUP_USER_UNVERIFIED",
                }:

                    result.warnings.append(
                        "Không đủ bằng chứng độc lập để xác minh USER UID."
                    )

        else:

            selected, reason = (
                select_verified_user(
                    user_candidates,
                    entity_type
                )
            )

            if selected:

                result.user_uid = (
                    selected.value
                )

                result.publisher_id = (
                    selected.value
                )

            else:

                if reason in {
                    "AMBIGUOUS",
                    "CONFLICT",
                }:

                    result.warnings.append(
                        "Phát hiện nhiều UID có bằng chứng xung đột."
                    )

        # ----------------------------------------------------------
        # USERNAME
        # ----------------------------------------------------------

        result.username = (
            infer_username(
                parsed_url,
                snapshot
            )
        )

        # ----------------------------------------------------------
        # Publisher name
        # ----------------------------------------------------------

        result.publisher_name = (
            infer_publisher_name(
                snapshot
            )
        )

        # ----------------------------------------------------------
        # Confidence
        # ----------------------------------------------------------

        confidence = 0

        if result.user_uid:

            candidate = user_candidates.get(
                result.user_uid
            )

            if candidate:

                confidence = min(
                    99,
                    int(
                        candidate.score
                    )
                )

                if candidate.verified:

                    confidence = min(
                        99,
                        confidence + 8
                    )

        # Direct page
        if (
            result.page_uid
            and entity_type.startswith(
                "PAGE"
            )
        ):

            page_score = page_ids.get(
                result.page_uid,
                0
            )

            confidence = max(
                confidence,
                min(
                    99,
                    int(page_score)
                )
            )

        # Direct group
        if (
            result.group_id
            and entity_type.startswith(
                "GROUP"
            )
        ):

            group_score = group_ids.get(
                result.group_id,
                0
            )

            confidence = max(
                confidence,
                min(
                    99,
                    int(group_score)
                )
            )

        # Direct profile
        if (
            parsed_url.url_type
            == "PROFILE"
            and result.user_uid
        ):

            confidence = max(
                confidence,
                95
            )

        # ----------------------------------------------------------
        # Status
        # ----------------------------------------------------------

        if (
            result.user_uid
            and confidence >= 70
        ):

            result.status = "VERIFIED"

        elif (
            result.page_uid
            and entity_type.startswith(
                "PAGE"
            )
            and confidence >= 60
        ):

            result.status = "VERIFIED"

        elif (
            result.group_id
            and entity_type.startswith(
                "GROUP"
            )
            and confidence >= 60
        ):

            result.status = "VERIFIED"

        elif (
            result.post_id
            or result.opaque_token
        ):

            result.status = "PARTIAL"

            result.warnings.append(
                "Object xác định được nhưng USER UID chưa đủ bằng chứng."
            )

        else:

            result.status = "UNRESOLVED"

        # ----------------------------------------------------------
        # Evidence count
        # ----------------------------------------------------------

        result.evidence_count = (
            store.count()
        )

        result.confidence = min(
            99,
            max(
                0,
                confidence
            )
        )

        result.title = (
            snapshot.meta.get(
                "og:title",
                ""
            )
            or snapshot.title
        )

        result.elapsed = (
            time.perf_counter()
            - started
        )

        return result


# ======================================================================
# PUBLIC API
# ======================================================================

async def resolve_facebook_url(
    url
):

    resolver = FacebookResolver()

    return await resolver.resolve(
        url
    )


async def resolve_facebook_urls(
    urls
):

    resolver = FacebookResolver()

    results = []

    for url in urls:

        try:

            result = await resolver.resolve(
                url
            )

        except Exception as exc:

            result = ResolveResult(
                original_url=url,
                status="ERROR",
                warnings=[
                    "Resolver error: "
                    + str(exc)[:200]
                ],
            )

        results.append(
            result
        )

    return results


# ======================================================================
# FORMAT RESULT
# ======================================================================

def entity_label(
    result
):

    mapping = {
        "USER": "👤 USER",
        "USER_POST": "👤 USER POST",
        "PAGE": "📄 PAGE",
        "PAGE_POST": "📄 PAGE POST",
        "GROUP": "👥 GROUP",
        "GROUP_POST": "👥 GROUP POST",
        "GROUP_MEDIA": "👥 GROUP MEDIA",
        "POST": "📝 POST",
        "VIDEO": "🎬 VIDEO",
        "REEL": "🎬 REEL",
        "PHOTO": "🖼 PHOTO",
        "STORY": "⭕ STORY",
        "PROFILE": "👤 PROFILE",
        "UNKNOWN": "❓ UNKNOWN",
    }

    return mapping.get(
        result.entity_type,
        mapping.get(
            result.url_type,
            "❓ UNKNOWN"
        )
    )


def format_result(
    result,
    index=None
):

    prefix = ""

    if index is not None:

        prefix = (
            f"{index}. "
        )

    lines = []

    lines.append(
        "🔎 FACEBOOK UID V20 ULTRA"
    )

    lines.append("")

    lines.append(
        prefix
        + entity_label(result)
    )

    lines.append(
        "📊 STATUS: "
        + result.status
    )

    # --------------------------------------------------------------
    # USER
    # --------------------------------------------------------------

    if result.user_uid:

        lines.append(
            "🆔 USER UID: "
            + result.user_uid
        )

    # --------------------------------------------------------------
    # PAGE
    # --------------------------------------------------------------

    if result.page_uid:

        lines.append(
            "📄 PAGE UID: "
            + result.page_uid
        )

    # --------------------------------------------------------------
    # GROUP
    # --------------------------------------------------------------

    if result.group_id:

        lines.append(
            "👥 GROUP ID: "
            + result.group_id
        )

    # --------------------------------------------------------------
    # Publisher
    # --------------------------------------------------------------

    if result.publisher_id:

        lines.append(
            "👤 PUBLISHER ID: "
            + result.publisher_id
        )

    if result.publisher_type != "UNKNOWN":

        lines.append(
            "🏷 PUBLISHER TYPE: "
            + result.publisher_type
        )

    if result.username:

        lines.append(
            "📛 USERNAME: @"
            + result.username
        )

    if result.publisher_name:

        lines.append(
            "📌 PUBLISHER: "
            + result.publisher_name[:150]
        )

    # --------------------------------------------------------------
    # Object
    # --------------------------------------------------------------

    if result.post_id:

        lines.append(
            "📝 POST ID: "
            + result.post_id
        )

    if result.video_id:

        lines.append(
            "🎬 VIDEO ID: "
            + result.video_id
        )

    if result.photo_id:

        lines.append(
            "🖼 PHOTO ID: "
            + result.photo_id
        )

    if result.story_id:

        lines.append(
            "⭕ STORY ID: "
            + result.story_id
        )

    # --------------------------------------------------------------
    # Confidence
    # --------------------------------------------------------------

    lines.append(
        "🎯 CONFIDENCE: "
        + str(
            result.confidence
        )
        + "%"
    )

    if result.evidence_count:

        lines.append(
            "📊 EVIDENCE: "
            + str(
                result.evidence_count
            )
            + " signals"
        )

    # --------------------------------------------------------------
    # URL
    # --------------------------------------------------------------

    if result.resolved_url:

        lines.append(
            "🔗 RESOLVED:"
        )

        lines.append(
            result.resolved_url[:900]
        )

    if (
        result.canonical_url
        and
        result.canonical_url
        != result.resolved_url
    ):

        lines.append(
            "🔗 CANONICAL:"
        )

        lines.append(
            result.canonical_url[:900]
        )

    # --------------------------------------------------------------
    # Warnings
    # --------------------------------------------------------------

    if result.warnings:

        lines.append("")

        lines.append(
            "⚠️ WARNINGS:"
        )

        seen = set()

        for warning in result.warnings:

            warning = clean_text(
                warning
            )

            if warning in seen:
                continue

            seen.add(warning)

            lines.append(
                "• "
                + warning[:300]
            )

    lines.append("")

    lines.append(
        "⏱ TIME: "
        + f"{result.elapsed:.2f}s"
    )

    return "\n".join(
        lines
    )


# ======================================================================
# TELEGRAM URL WAIT LOOP
# ======================================================================

_ACTIVE_SESSIONS = {}

_SESSION_LOCK = asyncio.Lock()


async def wait_for_facebook_message(
    bot,
    event,
    timeout=900,
):

    sender_id = event.sender_id

    chat_id = event.chat_id

    loop = asyncio.get_running_loop()

    future = loop.create_future()

    builder = events.NewMessage(
        chats=chat_id
    )

    async def waiter(
        incoming
    ):

        if future.done():
            return

        if incoming.sender_id != sender_id:
            return

        # Không lấy message của bot.
        if getattr(
            incoming,
            "out",
            False
        ):

            return

        future.set_result(
            incoming
        )

    bot.add_event_handler(
        waiter,
        builder
    )

    try:

        return await asyncio.wait_for(
            future,
            timeout=timeout
        )

    except asyncio.TimeoutError:

        return None

    finally:

        try:

            bot.remove_event_handler(
                waiter,
                builder
            )

        except Exception:

            pass


# ======================================================================
# MAIN COMMAND HANDLER
# ======================================================================

async def _handle_getuidfb(
    event,
    notify_bot=None,
):

    sender_id = event.sender_id

    # --------------------------------------------------------------
    # Prevent duplicate session
    # --------------------------------------------------------------

    async with _SESSION_LOCK:

        if sender_id in _ACTIVE_SESSIONS:

            try:

                await event.reply(
                    "⚠️ Bạn đang có một phiên "
                    "/getuidfb đang chờ URL.\n\n"
                    "Hãy gửi URL Facebook vào phiên hiện tại."
                )

            except Exception:
                pass

            return

        _ACTIVE_SESSIONS[
            sender_id
        ] = True

    try:

        await event.reply(
            "🔎 **FACEBOOK UID V20 ULTRA**\n\n"
            "Hãy gửi URL Facebook cần kiểm tra.\n\n"
            "Có thể gửi:\n"
            "• 1 URL\n"
            "• nhiều URL\n"
            "• URL cách nhau bằng dấu cách / xuống dòng\n"
            "• URL dính liền nhau\n\n"
            "Ví dụ:\n"
            "`https://facebook.com/share/p/AAA/https://facebook.com/user/posts/BBB`\n\n"
            "Bot sẽ tự tách thành từng URL và xử lý riêng.\n\n"
            "⏳ Phiên chờ tối đa 15 phút."
        )

        total_processed = 0

        while True:

            incoming = await wait_for_facebook_message(
                bot=event.client,
                event=event,
                timeout=900,
            )

            if incoming is None:

                try:

                    await event.reply(
                        "⌛ Phiên /getuidfb đã hết thời gian chờ.\n"
                        "Gửi /getuidfb để bắt đầu lại."
                    )

                except Exception:
                    pass

                break

            text = (
                incoming.raw_text
                or ""
            ).strip()

            # ------------------------------------------------------
            # Commands => thoát loop để command khác xử lý
            # ------------------------------------------------------

            if text.startswith("/"):

                break

            urls = extract_facebook_urls(
                text
            )

            if not urls:

                try:

                    await incoming.reply(
                        "❌ Không tìm thấy URL Facebook hợp lệ.\n\n"
                        "Hãy gửi URL Facebook, ví dụ:\n"
                        "https://www.facebook.com/username/posts/123"
                    )

                except Exception:
                    pass

                continue

            # ------------------------------------------------------
            # Report URL count
            # ------------------------------------------------------

            if len(urls) > 1:

                try:

                    await incoming.reply(
                        f"🔗 Đã nhận diện **{len(urls)} URL Facebook**.\n"
                        "⏳ Đang xử lý lần lượt..."
                    )

                except Exception:
                    pass

            else:

                try:

                    await incoming.reply(
                        "🔗 Đã nhận diện **1 URL Facebook**.\n"
                        "⏳ Đang phân tích..."
                    )

                except Exception:
                    pass

            # ------------------------------------------------------
            # Process sequentially to preserve order
            # ------------------------------------------------------

            for index, url in enumerate(
                urls,
                1
            ):

                try:

                    result = await resolve_facebook_url(
                        url
                    )

                    output = format_result(
                        result,
                        index=(
                            index
                            if len(urls) > 1
                            else None
                        )
                    )

                    # Telegram giới hạn message.
                    if len(output) > 4000:

                        output = output[
                            :3900
                        ] + "\n..."

                    await incoming.reply(
                        output
                    )

                except Exception as exc:

                    await incoming.reply(
                        "❌ Lỗi xử lý URL #"
                        + str(index)
                        + ":\n"
                        + str(exc)[:1000]
                    )

                total_processed += 1

            # ------------------------------------------------------
            # Continue loop
            # ------------------------------------------------------

            try:

                await incoming.reply(
                    "✅ Đã xử lý "
                    + str(len(urls))
                    + " URL.\n\n"
                    "📥 Gửi URL Facebook tiếp theo "
                    "để tiếp tục.\n"
                    "Hoặc gửi /stop để kết thúc."
                )

            except Exception:
                pass

    finally:

        async with _SESSION_LOCK:

            _ACTIVE_SESSIONS.pop(
                sender_id,
                None
            )


# ======================================================================
# TELETHON REGISTER
# ======================================================================

def register(
    bot,
    notify_bot=None,
):

    """
    Quan trọng:

    Telethon handler chỉ nhận event.

    notify_bot được giữ trong closure để tránh lỗi:

        TypeError:
        _handle_getuidfb() missing 1 required positional argument: 'event'
    """

    async def handler(
        event
    ):

        await _handle_getuidfb(
            event,
            notify_bot
        )

    bot.add_event_handler(
        handler,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?$"
        )
    )


# ======================================================================
# COMPATIBILITY ALIAS
# ======================================================================

handler = _handle_getuidfb


# ======================================================================
# END
# ======================================================================