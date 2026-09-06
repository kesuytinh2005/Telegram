#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
======================================================================
 FB UID / ENTITY RESOLVER V21 MAX ACCURACY
======================================================================

PUBLIC FACEBOOK URL RESOLVER
HTTP ONLY

NO:
    - Playwright
    - Selenium
    - Cookies
    - Facebook Access Token
    - Facebook Login

======================================================================
MỤC TIÊU
======================================================================

1. NHẬN DIỆN ĐÚNG LOẠI URL TRƯỚC KHI TÌM UID

2. Phân biệt:
       USER UID
       PAGE UID
       GROUP ID
       POST ID
       PHOTO ID
       VIDEO ID
       REEL ID
       STORY ID
       ALBUM ID
       EVENT ID
       MARKETPLACE ID

3. Không lấy numeric ID ngẫu nhiên trong HTML làm UID.

4. Không coi:
       pfbid
       share token
       media_fbid
       entity_id
       actor_id
       post_id

   là USER UID nếu chưa chứng minh quan hệ.

5. Canonical URL / redirected URL có độ ưu tiên rất cao.

Ví dụ:

/share/XXXX/
        ↓
photo.php?fbid=PHOTO&id=USER
        ↓
URL TYPE = PHOTO
PHOTO ID = PHOTO
USER UID = USER

6. Hỗ trợ URL dính liền:

https://facebook.com/share/p/AAA/https://facebook.com/user/posts/BBB

=> 2 URL.

7. Hỗ trợ:

https://facebook.com/...
http://facebook.com/...
www.facebook.com/...
facebook.com/...

8. Interactive /getuidfb loop.

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
    "description": "Facebook UID / Entity Resolver V21",
    "category": "facebook",
}


# ======================================================================
# CONFIG
# ======================================================================

TIMEOUT = 15

PROFILE_TIMEOUT = 10

MAX_HTML = 12 * 1024 * 1024

MAX_PROFILE_CHECKS = 6

MAX_LINKS = 300

MAX_JSON_SCAN = 5 * 1024 * 1024


# ======================================================================
# USER AGENTS
# ======================================================================

USER_AGENTS = [
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


# ======================================================================
# FACEBOOK HOSTS
# ======================================================================

FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "mobile.facebook.com",
    "web.facebook.com",
    "fb.watch",
}


# ======================================================================
# URL START DETECTOR
# ======================================================================

URL_START_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"https?://"
    r"(?:www\.)?"
    r"(?:facebook\.com|m\.facebook\.com|"
    r"mbasic\.facebook\.com|mobile\.facebook\.com|"
    r"web\.facebook\.com|fb\.watch)"
    r"|"
    r"(?<![\w./])"
    r"(?:www\.)?"
    r"(?:facebook\.com|m\.facebook\.com|"
    r"mbasic\.facebook\.com|mobile\.facebook\.com|"
    r"web\.facebook\.com|fb\.watch)"
    r")"
)


# ======================================================================
# GENERIC REGEX
# ======================================================================

NUMERIC_ID_RE = re.compile(
    r"(?<!\d)\d{5,25}(?!\d)"
)

PFBID_RE = re.compile(
    r"(?i)\bpfbid[A-Za-z0-9_-]+\b"
)

USERNAME_RE = re.compile(
    r"^[A-Za-z0-9._-]{2,100}$"
)


# ======================================================================
# DATA CLASSES
# ======================================================================

@dataclass
class FBURL:
    original: str
    normalized: str

    host: str = ""
    path: str = ""

    query: dict = field(
        default_factory=dict
    )

    kind: str = "UNKNOWN"

    object_kind: str = "UNKNOWN"

    route_entity: str = "UNKNOWN"

    username: str = ""

    object_id: str = ""

    opaque_token: str = ""

    publisher_hint: str = ""


@dataclass
class Snapshot:
    requested_url: str

    final_url: str = ""

    canonical_url: str = ""

    status: int = 0

    title: str = ""

    html_text: str = ""

    meta: dict = field(
        default_factory=dict
    )

    links: list = field(
        default_factory=list
    )

    jsonld: list = field(
        default_factory=list
    )

    redirect_chain: list = field(
        default_factory=list
    )

    error: str = ""


@dataclass
class Signal:
    value: str

    role: str

    source: str

    weight: float

    context: str = ""

    object_id: str = ""

    independent: str = ""


@dataclass
class UIDCandidate:
    uid: str

    score: float = 0

    sources: set = field(
        default_factory=set
    )

    contexts: list = field(
        default_factory=list
    )

    verified: bool = False


@dataclass
class Result:
    original_url: str

    resolved_url: str = ""

    canonical_url: str = ""

    url_type: str = "UNKNOWN"

    object_type: str = "UNKNOWN"

    entity_type: str = "UNKNOWN"

    publisher_type: str = "UNKNOWN"

    user_uid: str = ""

    page_uid: str = ""

    group_id: str = ""

    post_id: str = ""

    photo_id: str = ""

    video_id: str = ""

    reel_id: str = ""

    story_id: str = ""

    album_id: str = ""

    event_id: str = ""

    marketplace_id: str = ""

    username: str = ""

    publisher_name: str = ""

    publisher_id: str = ""

    opaque_token: str = ""

    title: str = ""

    confidence: int = 0

    status: str = "UNRESOLVED"

    evidence: int = 0

    warnings: list = field(
        default_factory=list
    )

    elapsed: float = 0


# ======================================================================
# HTML PARSER
# ======================================================================

class Parser(HTMLParser):

    def __init__(self):

        super().__init__(
            convert_charrefs=True
        )

        self.meta = {}

        self.links = []

        self.title = []

        self.jsonld_raw = []

        self.in_title = False

        self.in_script = False

        self.script_attrs = {}

        self.script_data = []

    def handle_starttag(
        self,
        tag,
        attrs
    ):

        tag = tag.lower()

        attrs = {
            str(k).lower(): str(v)
            for k, v in attrs
            if v is not None
        }

        if tag == "title":

            self.in_title = True

        elif tag == "meta":

            key = (
                attrs.get("property")
                or attrs.get("name")
                or attrs.get("itemprop")
                or ""
            ).lower()

            value = attrs.get(
                "content",
                ""
            )

            if key:

                self.meta[key] = (
                    html.unescape(
                        value
                    )
                )

        elif tag == "link":

            href = attrs.get(
                "href"
            )

            if href:

                self.links.append(
                    href
                )

        elif tag == "a":

            href = attrs.get(
                "href"
            )

            if href:

                self.links.append(
                    href
                )

        elif tag == "script":

            self.in_script = True

            self.script_attrs = attrs

            self.script_data = []

    def handle_endtag(
        self,
        tag
    ):

        tag = tag.lower()

        if tag == "title":

            self.in_title = False

        elif tag == "script":

            typ = self.script_attrs.get(
                "type",
                ""
            ).lower()

            if typ == "application/ld+json":

                self.jsonld_raw.append(
                    "".join(
                        self.script_data
                    )
                )

            self.in_script = False

            self.script_attrs = {}

            self.script_data = []

    def handle_data(
        self,
        data
    ):

        if self.in_title:

            self.title.append(
                data
            )

        if self.in_script:

            self.script_data.append(
                data
            )


# ======================================================================
# HELPERS
# ======================================================================

def clean(value):

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


def normalize_id(value):

    if value is None:
        return ""

    value = str(
        value
    ).strip()

    if not value.isdigit():
        return ""

    value = value.lstrip("0")

    if len(value) < 5:
        return ""

    if len(value) > 25:
        return ""

    return value


def is_numeric_id(value):

    return bool(
        normalize_id(value)
    )


def facebook_host(host):

    host = (
        host or ""
    ).lower().split(":")[0]

    return (
        host in FACEBOOK_HOSTS
        or host.endswith(
            ".facebook.com"
        )
    )


def normalize_url(url):

    if not url:
        return ""

    url = html.unescape(
        str(url)
    ).strip()

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

        url = (
            "https://"
            + url
        )

    return url


def parse_url(url):

    try:

        return urlparse(
            normalize_url(url)
        )

    except Exception:

        return None


def query_dict(parsed):

    raw = parse_qs(
        parsed.query,
        keep_blank_values=True
    )

    result = {}

    for key, values in raw.items():

        key = key.lower()

        if values:

            result[key] = (
                values[-1]
            )

    return result


# ======================================================================
# URL EXTRACTION
# ======================================================================

def extract_facebook_urls(
    text
):

    """
    Tách URL theo vị trí bắt đầu URL.

    Đây là phần sửa trực tiếp lỗi:

    /share/p/AAA/https://facebook.com/BBB

    thành:

        URL 1
        URL 2
    """

    if not text:
        return []

    text = html.unescape(
        str(text)
    )

    matches = list(
        URL_START_RE.finditer(
            text
        )
    )

    results = []

    for i, match in enumerate(
        matches
    ):

        start = match.start()

        if i + 1 < len(
            matches
        ):

            end = matches[
                i + 1
            ].start()

        else:

            end = len(text)

        chunk = text[
            start:end
        ]

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

        parsed = parse_url(
            chunk
        )

        if not parsed:
            continue

        if not facebook_host(
            parsed.netloc
        ):

            continue

        results.append(
            chunk
        )

    # Deduplicate.
    output = []

    seen = set()

    for url in results:

        key = url.rstrip(
            "/"
        ).lower()

        if key in seen:
            continue

        seen.add(key)

        output.append(
            url
        )

    return output


# ======================================================================
# URL CLASSIFIER
# ======================================================================

def classify_url(
    url
):

    normalized = normalize_url(
        url
    )

    parsed = parse_url(
        normalized
    )

    if not parsed:

        return FBURL(
            original=url,
            normalized=normalized
        )

    host = (
        parsed.netloc
        .lower()
        .split(":")[0]
    )

    path = unquote(
        parsed.path or ""
    )

    path = re.sub(
        r"/+",
        "/",
        path
    )

    path_lower = path.lower()

    query = query_dict(
        parsed
    )

    item = FBURL(
        original=url,
        normalized=normalized,
        host=host,
        path=path,
        query=query,
    )

    # ==============================================================
    # PROFILE.PHP
    # ==============================================================

    if path_lower.rstrip(
        "/"
    ).endswith(
        "/profile.php"
    ):

        item.kind = (
            "PROFILE_ID"
        )

        item.object_kind = (
            "PROFILE"
        )

        item.route_entity = (
            "USER"
        )

        item.object_id = normalize_id(
            query.get("id")
        )

        return item

    # ==============================================================
    # PHOTO.PHP
    #
    # photo.php?fbid=PHOTO&id=USER
    #
    # fbid = PHOTO
    # id   = USER
    # ==============================================================

    if path_lower.rstrip(
        "/"
    ).endswith(
        "/photo.php"
    ):

        if query.get("fbid"):

            item.kind = "PHOTO"

            item.object_kind = (
                "PHOTO"
            )

            item.object_id = normalize_id(
                query.get("fbid")
            )

            if query.get("id"):

                item.publisher_hint = (
                    normalize_id(
                        query.get("id")
                    )
                )

            if query.get("set"):

                item.route_entity = (
                    "ALBUM"
                )

                album = query.get(
                    "set",
                    ""
                )

                # a.123456...
                m = re.search(
                    r"(\d{5,25})",
                    album
                )

                if m:

                    item.publisher_hint = (
                        item.publisher_hint
                    )

            return item

    # ==============================================================
    # STORY.PHP
    # ==============================================================

    if path_lower.rstrip(
        "/"
    ).endswith(
        "/story.php"
    ):

        item.kind = "STORY"

        item.object_kind = (
            "STORY"
        )

        item.object_id = (
            normalize_id(
                query.get(
                    "story_fbid"
                )
            )
            or
            normalize_id(
                query.get("fbid")
            )
        )

        item.publisher_hint = (
            normalize_id(
                query.get("id")
            )
        )

        return item

    # ==============================================================
    # VIDEO.PHP
    # ==============================================================

    if path_lower.rstrip(
        "/"
    ).endswith(
        "/video.php"
    ):

        item.kind = "VIDEO"

        item.object_kind = (
            "VIDEO"
        )

        item.object_id = (
            normalize_id(
                query.get("v")
            )
            or
            normalize_id(
                query.get("video_id")
            )
            or
            normalize_id(
                query.get("fbid")
            )
        )

        item.publisher_hint = (
            normalize_id(
                query.get("id")
            )
        )

        return item

    # ==============================================================
    # WATCH
    # ==============================================================

    if path_lower.rstrip(
        "/"
    ) == "/watch":

        item.kind = "VIDEO"

        item.object_kind = (
            "VIDEO"
        )

        item.object_id = (
            normalize_id(
                query.get("v")
            )
            or query.get("v", "")
        )

        return item

    # ==============================================================
    # SHARE
    # ==============================================================

    share = re.search(
        r"/share/(p|v|r)(?:/([^/?#]+))?",
        path,
        re.I
    )

    if share:

        kind = share.group(
            1
        ).lower()

        token = (
            share.group(2)
            or ""
        )

        item.opaque_token = token

        if kind == "p":

            item.kind = (
                "SHARE_POST"
            )

            item.object_kind = (
                "POST"
            )

        elif kind == "v":

            item.kind = (
                "SHARE_VIDEO"
            )

            item.object_kind = (
                "VIDEO"
            )

        else:

            item.kind = (
                "SHARE_REEL"
            )

            item.object_kind = (
                "REEL"
            )

        return item

    # ==============================================================
    # SHARE WITHOUT /p /v /r
    # /share/XXXX
    # ==============================================================

    generic_share = re.search(
        r"/share/([^/?#]+)",
        path,
        re.I
    )

    if generic_share:

        item.kind = (
            "SHARE"
        )

        item.object_kind = (
            "UNKNOWN"
        )

        item.opaque_token = (
            generic_share.group(1)
        )

        return item

    # ==============================================================
    # GROUP
    # ==============================================================

    group = re.search(
        r"/groups/([^/?#]+)(?:/([^/?#]+))?",
        path,
        re.I
    )

    if group:

        group_token = (
            group.group(1)
        )

        if group_token.isdigit():

            item.route_entity = (
                "GROUP"
            )

            item.publisher_hint = (
                normalize_id(
                    group_token
                )
            )

        tail = (
            group.group(2)
            or ""
        ).lower()

        if "/posts/" in path_lower:

            item.kind = (
                "GROUP_POST"
            )

            item.object_kind = (
                "POST"
            )

        elif (
            "/photo"
            in path_lower
        ):

            item.kind = (
                "GROUP_PHOTO"
            )

            item.object_kind = (
                "PHOTO"
            )

        elif (
            "/video"
            in path_lower
            or "/reel"
            in path_lower
        ):

            item.kind = (
                "GROUP_VIDEO"
            )

            item.object_kind = (
                "VIDEO"
            )

        elif (
            "/permalink/"
            in path_lower
        ):

            item.kind = (
                "GROUP_POST"
            )

            item.object_kind = (
                "POST"
            )

        else:

            item.kind = (
                "GROUP"
            )

            item.object_kind = (
                "GROUP"
            )

        return item

    # ==============================================================
    # PAGE ROUTE
    # /pages/name/123
    # ==============================================================

    page = re.search(
        r"/pages/([^/]+)/(\d+)",
        path,
        re.I
    )

    if page:

        item.route_entity = (
            "PAGE"
        )

        item.kind = (
            "PAGE"
        )

        item.object_kind = (
            "PAGE"
        )

        item.object_id = normalize_id(
            page.group(2)
        )

        item.username = (
            page.group(1)
        )

        return item

    # ==============================================================
    # REEL
    # ==============================================================

    reel = re.search(
        r"/reels?/([^/?#]+)",
        path,
        re.I
    )

    if reel:

        item.kind = "REEL"

        item.object_kind = (
            "REEL"
        )

        item.object_id = normalize_id(
            reel.group(1)
        ) or reel.group(1)

        return item

    # ==============================================================
    # VIDEO
    # ==============================================================

    video = re.search(
        r"/videos?/([^/?#]+)",
        path,
        re.I
    )

    if video:

        item.kind = "VIDEO"

        item.object_kind = (
            "VIDEO"
        )

        item.object_id = normalize_id(
            video.group(1)
        ) or video.group(1)

        return item

    # ==============================================================
    # PHOTO / PHOTOS
    # ==============================================================

    photo = re.search(
        r"/photos?(?:/[^/]+)?/(\d+)",
        path,
        re.I
    )

    if photo:

        item.kind = "PHOTO"

        item.object_kind = (
            "PHOTO"
        )

        item.object_id = normalize_id(
            photo.group(1)
        )

        return item

    # ==============================================================
    # POSTS
    # ==============================================================

    post = re.search(
        r"/posts/([^/?#]+)",
        path,
        re.I
    )

    if post:

        item.kind = "POST"

        item.object_kind = (
            "POST"
        )

        token = post.group(1)

        item.object_id = (
            normalize_id(token)
            or token
        )

        parts = [
            x for x in path.split("/")
            if x
        ]

        if parts:

            first = parts[0]

            if USERNAME_RE.match(
                first
            ):

                item.username = (
                    first
                )

        return item

    # ==============================================================
    # /p/
    # ==============================================================

    p_route = re.search(
        r"/p/([^/?#]+)",
        path,
        re.I
    )

    if p_route:

        item.kind = "POST"

        item.object_kind = (
            "POST"
        )

        item.object_id = (
            normalize_id(
                p_route.group(1)
            )
            or p_route.group(1)
        )

        return item

    # ==============================================================
    # LIVE
    # ==============================================================

    live = re.search(
        r"/(?:live|videos/live)/([^/?#]+)",
        path,
        re.I
    )

    if live:

        item.kind = "LIVE"

        item.object_kind = (
            "LIVE"
        )

        item.object_id = (
            normalize_id(
                live.group(1)
            )
            or live.group(1)
        )

        return item

    # ==============================================================
    # MARKETPLACE
    # ==============================================================

    marketplace = re.search(
        r"/marketplace/item/(\d+)",
        path,
        re.I
    )

    if marketplace:

        item.kind = (
            "MARKETPLACE"
        )

        item.object_kind = (
            "MARKETPLACE"
        )

        item.object_id = normalize_id(
            marketplace.group(1)
        )

        return item

    # ==============================================================
    # EVENT
    # ==============================================================

    event = re.search(
        r"/events/(\d+)",
        path,
        re.I
    )

    if event:

        item.kind = "EVENT"

        item.object_kind = (
            "EVENT"
        )

        item.object_id = normalize_id(
            event.group(1)
        )

        return item

    # ==============================================================
    # USERNAME / PROFILE
    # ==============================================================

    parts = [
        x for x in path.split("/")
        if x
    ]

    if len(parts) == 1:

        username = parts[0]

        if USERNAME_RE.match(
            username
        ):

            if username.lower() not in {
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
                "story",
                "login",
                "share",
            }:

                item.kind = (
                    "PROFILE"
                )

                item.object_kind = (
                    "PROFILE"
                )

                item.username = (
                    username
                )

                item.route_entity = (
                    "USER_OR_PAGE"
                )

                return item

    return item


# ======================================================================
# HTTP FETCHER
# ======================================================================

class Fetcher:

    def __init__(self):

        self.session = requests.Session()

    def fetch(
        self,
        url,
        timeout=TIMEOUT
    ):

        last_error = ""

        for ua in USER_AGENTS:

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

                response = (
                    self.session.get(
                        url,
                        headers=headers,
                        timeout=timeout,
                        allow_redirects=True,
                        stream=True,
                    )
                )

                status = (
                    response.status_code
                )

                if status in {
                    403,
                    429,
                    500,
                    502,
                    503,
                    504,
                }:

                    response.close()

                    time.sleep(
                        0.25
                    )

                    continue

                chunks = []

                total = 0

                for chunk in (
                    response.iter_content(
                        65536
                    )
                ):

                    if not chunk:
                        continue

                    total += len(
                        chunk
                    )

                    if total > MAX_HTML:

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

                text = raw.decode(
                    encoding,
                    errors="replace"
                )

                final_url = str(
                    response.url
                )

                history = [
                    str(x.url)
                    for x in response.history
                ]

                response.close()

                return {
                    "status": status,
                    "url": final_url,
                    "text": text,
                    "history": history,
                    "error": "",
                }

            except Exception as exc:

                last_error = str(
                    exc
                )

        return {
            "status": 0,
            "url": url,
            "text": "",
            "history": [],
            "error": last_error,
        }


# ======================================================================
# SNAPSHOT
# ======================================================================

def parse_snapshot(
    requested,
    fetched
):

    parser = Parser()

    text = fetched.get(
        "text",
        ""
    )

    try:

        parser.feed(
            text
        )

    except Exception:
        pass

    jsonld = []

    for raw in parser.jsonld_raw:

        if len(raw) > MAX_JSON_SCAN:
            continue

        try:

            jsonld.append(
                json.loads(raw)
            )

        except Exception:
            pass

    title = clean(
        " ".join(
            parser.title
        )
    )

    canonical = (
        parser.meta.get(
            "og:url",
            ""
        )
    )

    return Snapshot(
        requested_url=requested,
        final_url=fetched.get(
            "url",
            requested
        ),
        canonical_url=normalize_url(
            canonical
        ) if canonical else "",
        status=fetched.get(
            "status",
            0
        ),
        title=title,
        html_text=text,
        meta=parser.meta,
        links=parser.links[
            :MAX_LINKS
        ],
        jsonld=jsonld,
        redirect_chain=fetched.get(
            "history",
            []
        ),
        error=fetched.get(
            "error",
            ""
        ),
    )


# ======================================================================
# SIGNAL STORE
# ======================================================================

class Signals:

    def __init__(self):

        self.items = []

    def add(
        self,
        value,
        role,
        source,
        weight,
        context="",
        object_id="",
        independent=""
    ):

        value = str(
            value or ""
        ).strip()

        if not value:
            return

        self.items.append(
            Signal(
                value=value,
                role=role,
                source=source,
                weight=float(
                    weight
                ),
                context=clean(
                    context
                )[:1500],
                object_id=object_id,
                independent=(
                    independent
                    or source
                ),
            )
        )


# ======================================================================
# IMPORTANT QUERY SEMANTICS
# ======================================================================

def extract_query_semantics(
    fburl,
    signals
):

    q = fburl.query

    # ==============================================================
    # PHOTO.PHP
    # ==============================================================

    if fburl.kind == "PHOTO":

        # fbid = PHOTO ID
        photo_id = normalize_id(
            q.get("fbid")
        )

        if photo_id:

            signals.add(
                photo_id,
                "PHOTO",
                "photo.php:fbid",
                35,
                context=fburl.normalized,
                object_id=photo_id,
                independent="photo_fbid",
            )

        # id = USER / publisher
        publisher_id = normalize_id(
            q.get("id")
        )

        if publisher_id:

            signals.add(
                publisher_id,
                "PUBLISHER",
                "photo.php:id",
                32,
                context=fburl.normalized,
                object_id=photo_id,
                independent="photo_publisher_id",
            )

            signals.add(
                publisher_id,
                "USER",
                "photo.php:publisher",
                28,
                context=fburl.normalized,
                object_id=photo_id,
                independent="photo_publisher_user",
            )

        # set=a.ALBUM_ID
        set_value = q.get(
            "set",
            ""
        )

        album_match = re.search(
            r"(\d{5,25})",
            set_value
        )

        if album_match:

            album_id = normalize_id(
                album_match.group(1)
            )

            if album_id:

                signals.add(
                    album_id,
                    "ALBUM",
                    "photo.php:set",
                    25,
                    context=fburl.normalized,
                    object_id=photo_id,
                    independent="photo_album",
                )

        return

    # ==============================================================
    # PROFILE
    # ==============================================================

    if fburl.kind == "PROFILE_ID":

        uid = normalize_id(
            q.get("id")
        )

        if uid:

            signals.add(
                uid,
                "USER",
                "profile.php:id",
                50,
                context=fburl.normalized,
                independent="direct_profile_id",
            )

        return

    # ==============================================================
    # STORY
    # ==============================================================

    if fburl.kind == "STORY":

        story_id = normalize_id(
            q.get("story_fbid")
        )

        if story_id:

            signals.add(
                story_id,
                "STORY",
                "story_fbid",
                35,
                context=fburl.normalized,
                independent="story_fbid",
            )

        publisher_id = normalize_id(
            q.get("id")
        )

        if publisher_id:

            signals.add(
                publisher_id,
                "PUBLISHER",
                "story:id",
                28,
                context=fburl.normalized,
                object_id=story_id,
                independent="story_publisher",
            )

        return

    # ==============================================================
    # VIDEO
    # ==============================================================

    if fburl.kind == "VIDEO":

        video_id = (
            normalize_id(
                q.get("v")
            )
            or normalize_id(
                q.get("video_id")
            )
            or normalize_id(
                q.get("fbid")
            )
        )

        if video_id:

            signals.add(
                video_id,
                "VIDEO",
                "video_query",
                35,
                context=fburl.normalized,
                independent="video_query",
            )

        publisher_id = normalize_id(
            q.get("id")
        )

        if publisher_id:

            signals.add(
                publisher_id,
                "PUBLISHER",
                "video:id",
                28,
                context=fburl.normalized,
                object_id=video_id,
                independent="video_publisher",
            )


# ======================================================================
# META SEMANTIC EXTRACTION
# ======================================================================

SEMANTIC_PATTERNS = [
    (
        "user_id",
        "USER",
        22
    ),
    (
        "profile_id",
        "USER",
        22
    ),
    (
        "owner_id",
        "USER",
        20
    ),
    (
        "publisher_id",
        "PUBLISHER",
        20
    ),
    (
        "page_id",
        "PAGE",
        25
    ),
    (
        "group_id",
        "GROUP",
        25
    ),
    (
        "post_id",
        "POST",
        18
    ),
    (
        "video_id",
        "VIDEO",
        18
    ),
    (
        "photo_id",
        "PHOTO",
        18
    ),
    (
        "story_fbid",
        "STORY",
        18
    ),
    (
        "media_fbid",
        "MEDIA",
        10
    ),
    (
        "actor_id",
        "ACTOR",
        7
    ),
]


def extract_semantic_signals(
    text,
    signals,
    object_id=""
):

    if not text:
        return

    for key, role, weight in (
        SEMANTIC_PATTERNS
    ):

        pattern = re.compile(
            rf'(?i)(["\']?{re.escape(key)}["\']?)'
            rf'\s*[:=]\s*'
            rf'["\']?(\d{{5,25}})'
        )

        for match in pattern.finditer(
            text
        ):

            value = normalize_id(
                match.group(2)
            )

            if not value:
                continue

            context = text[
                max(
                    0,
                    match.start() - 300
                ):
                min(
                    len(text),
                    match.end() + 300
                )
            ]

            signals.add(
                value,
                role,
                "semantic:" + key,
                weight,
                context=context,
                object_id=object_id,
                independent=(
                    "semantic:" + key
                ),
            )


# ======================================================================
# CANONICAL URL ANALYSIS
# ======================================================================

def analyze_canonical(
    url,
    signals
):

    if not url:
        return

    fburl = classify_url(
        url
    )

    # URL route itself is strong evidence.
    if fburl.kind == "PHOTO":

        photo_id = normalize_id(
            fburl.query.get(
                "fbid"
            )
        )

        if photo_id:

            signals.add(
                photo_id,
                "PHOTO",
                "canonical_photo",
                45,
                context=url,
                independent="canonical_photo",
            )

        publisher = normalize_id(
            fburl.query.get(
                "id"
            )
        )

        if publisher:

            signals.add(
                publisher,
                "PUBLISHER",
                "canonical_photo_publisher",
                42,
                context=url,
                object_id=photo_id,
                independent="canonical_photo_publisher",
            )

            signals.add(
                publisher,
                "USER",
                "canonical_photo_user",
                38,
                context=url,
                object_id=photo_id,
                independent="canonical_photo_user",
            )

        album = re.search(
            r"(?i)(?:^|[=&])a[.]?(\d{5,25})",
            fburl.query.get(
                "set",
                ""
            )
        )

        if album:

            album_id = normalize_id(
                album.group(1)
            )

            if album_id:

                signals.add(
                    album_id,
                    "ALBUM",
                    "canonical_album",
                    35,
                    context=url,
                    object_id=photo_id,
                    independent="canonical_album",
                )

    elif fburl.kind == "PROFILE_ID":

        uid = normalize_id(
            fburl.query.get(
                "id"
            )
        )

        if uid:

            signals.add(
                uid,
                "USER",
                "canonical_profile",
                50,
                context=url,
                independent="canonical_profile",
            )

    elif fburl.kind in {
        "POST",
        "PAGE_POST",
        "GROUP_POST",
    }:

        if fburl.object_id:

            value = normalize_id(
                fburl.object_id
            )

            if value:

                signals.add(
                    value,
                    "POST",
                    "canonical_post",
                    40,
                    context=url,
                    independent="canonical_post",
                )


# ======================================================================
# JSON-LD
# ======================================================================

def walk_json(
    value
):

    if isinstance(
        value,
        dict
    ):

        yield value

        for child in value.values():

            yield from walk_json(
                child
            )

    elif isinstance(
        value,
        list
    ):

        for child in value:

            yield from walk_json(
                child
            )


def jsonld_signals(
    snapshot,
    signals
):

    for root in snapshot.jsonld:

        for obj in walk_json(
            root
        ):

            typ = clean(
                obj.get(
                    "@type",
                    ""
                )
            ).lower()

            # ------------------------------------------------------
            # author
            # ------------------------------------------------------

            author = obj.get(
                "author"
            )

            authors = []

            if isinstance(
                author,
                dict
            ):

                authors.append(
                    author
                )

            elif isinstance(
                author,
                list
            ):

                authors.extend(
                    x for x in author
                    if isinstance(x, dict)
                )

            for author_obj in authors:

                author_url = clean(
                    author_obj.get(
                        "url",
                        ""
                    )
                )

                author_id = clean(
                    author_obj.get(
                        "@id",
                        ""
                    )
                )

                name = clean(
                    author_obj.get(
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

                    p = parse_url(
                        value
                    )

                    if not p:
                        continue

                    q = query_dict(
                        p
                    )

                    uid = normalize_id(
                        q.get("id")
                    )

                    if uid:

                        signals.add(
                            uid,
                            "USER",
                            "jsonld_author",
                            30,
                            context=(
                                name
                                + " "
                                + value
                            ),
                            independent=(
                                "jsonld_author_url"
                            ),
                        )

            # ------------------------------------------------------
            # mainEntityOfPage
            # ------------------------------------------------------

            main = obj.get(
                "mainEntityOfPage"
            )

            if isinstance(
                main,
                dict
            ):

                main = (
                    main.get(
                        "@id",
                        ""
                    )
                    or main.get(
                        "url",
                        ""
                    )
                )

            if isinstance(
                main,
                str
            ):

                analyze_canonical(
                    main,
                    signals
                )

            # ------------------------------------------------------
            # object URL
            # ------------------------------------------------------

            obj_url = clean(
                obj.get(
                    "url",
                    ""
                )
            )

            if obj_url:

                analyze_canonical(
                    obj_url,
                    signals
                )


# ======================================================================
# HTML LINK ANALYSIS
# ======================================================================

def analyze_links(
    snapshot,
    signals
):

    for href in snapshot.links:

        href = html.unescape(
            str(href)
        )

        if not href:
            continue

        absolute = urljoin(
            snapshot.final_url,
            href
        )

        parsed = parse_url(
            absolute
        )

        if not parsed:
            continue

        if not facebook_host(
            parsed.netloc
        ):
            continue

        fburl = classify_url(
            absolute
        )

        # ----------------------------------------------------------
        # profile.php?id=UID
        # ----------------------------------------------------------

        if fburl.kind == "PROFILE_ID":

            uid = normalize_id(
                fburl.query.get(
                    "id"
                )
            )

            if uid:

                signals.add(
                    uid,
                    "USER",
                    "profile_link",
                    25,
                    context=absolute,
                    independent="profile_link",
                )

        # ----------------------------------------------------------
        # photo.php?fbid=X&id=Y
        # ----------------------------------------------------------

        elif fburl.kind == "PHOTO":

            photo_id = normalize_id(
                fburl.query.get(
                    "fbid"
                )
            )

            publisher_id = normalize_id(
                fburl.query.get(
                    "id"
                )
            )

            if photo_id:

                signals.add(
                    photo_id,
                    "PHOTO",
                    "photo_link",
                    22,
                    context=absolute,
                    independent="photo_link",
                )

            if publisher_id:

                signals.add(
                    publisher_id,
                    "USER",
                    "photo_publisher_link",
                    24,
                    context=absolute,
                    object_id=photo_id,
                    independent="photo_publisher_link",
                )

        # ----------------------------------------------------------
        # Group
        # ----------------------------------------------------------

        elif fburl.route_entity == "GROUP":

            if fburl.publisher_hint:

                signals.add(
                    fburl.publisher_hint,
                    "GROUP",
                    "group_link",
                    30,
                    context=absolute,
                    independent="group_link",
                )


# ======================================================================
# OG ANALYSIS
# ======================================================================

def analyze_meta(
    snapshot,
    signals
):

    for key, value in snapshot.meta.items():

        if not value:
            continue

        key = key.lower()

        # og:url is much more valuable than random HTML numbers.
        if key == "og:url":

            analyze_canonical(
                value,
                signals
            )

        elif key in {
            "article:author",
            "profile:url",
            "author",
        }:

            p = parse_url(
                value
            )

            if p:

                q = query_dict(
                    p
                )

                uid = normalize_id(
                    q.get("id")
                )

                if uid:

                    signals.add(
                        uid,
                        "USER",
                        "meta_author",
                        28,
                        context=value,
                        independent="meta_author",
                    )

        # Only semantic IDs.
        extract_semantic_signals(
            value,
            signals
        )


# ======================================================================
# RAW HTML ANALYSIS
# ======================================================================

def analyze_raw_html(
    snapshot,
    signals,
    object_id=""
):

    text = snapshot.html_text

    # --------------------------------------------------------------
    # Semantic IDs
    # --------------------------------------------------------------

    extract_semantic_signals(
        text,
        signals,
        object_id=object_id
    )

    # --------------------------------------------------------------
    # Strong profile.php?id references
    # --------------------------------------------------------------

    profile_pattern = re.compile(
        r"(?i)"
        r"(?:facebook\.com|"
        r"m\.facebook\.com|"
        r"www\.facebook\.com)"
        r"/profile\.php\?id=(\d{5,25})"
    )

    for match in profile_pattern.finditer(
        text
    ):

        uid = normalize_id(
            match.group(1)
        )

        if uid:

            context = text[
                max(
                    0,
                    match.start() - 250
                ):
                min(
                    len(text),
                    match.end() + 250
                )
            ]

            signals.add(
                uid,
                "USER",
                "html_profile_url",
                34,
                context=context,
                independent="html_profile_url",
            )

    # --------------------------------------------------------------
    # photo.php?fbid=X&id=Y
    # --------------------------------------------------------------

    photo_pattern = re.compile(
        r"(?i)"
        r"photo\.php\?"
        r"[^\"'<>\s]{0,1000}"
    )

    for match in photo_pattern.finditer(
        text
    ):

        chunk = match.group()

        parsed = parse_url(
            "https://www.facebook.com/"
            + chunk
        )

        if not parsed:
            continue

        q = query_dict(
            parsed
        )

        photo_id = normalize_id(
            q.get("fbid")
        )

        user_id = normalize_id(
            q.get("id")
        )

        if photo_id:

            signals.add(
                photo_id,
                "PHOTO",
                "html_photo_fbid",
                28,
                context=chunk,
                independent="html_photo_fbid",
            )

        if user_id:

            signals.add(
                user_id,
                "USER",
                "html_photo_user",
                30,
                context=chunk,
                object_id=photo_id,
                independent="html_photo_user",
            )

    # --------------------------------------------------------------
    # pfbid is ONLY opaque.
    # --------------------------------------------------------------

    for match in PFBID_RE.finditer(
        text
    ):

        token = match.group()

        context = text[
            max(
                0,
                match.start() - 500
            ):
            min(
                len(text),
                match.end() + 500
            )
        ]

        signals.add(
            token,
            "OPAQUE",
            "html_pfbid",
            1,
            context=context,
            independent="html_pfbid",
        )


# ======================================================================
# PROFILE URL DISCOVERY
# ======================================================================

def discover_profiles(
    fburl,
    snapshot
):

    urls = []

    # Input username.
    if fburl.username:

        urls.extend([
            (
                "https://www.facebook.com/"
                + fburl.username
            ),
            (
                "https://m.facebook.com/"
                + fburl.username
            ),
        ])

    # canonical / OG
    for value in (
        snapshot.canonical_url,
        snapshot.meta.get(
            "og:url",
            ""
        ),
    ):

        if not value:
            continue

        p = parse_url(
            value
        )

        if not p:
            continue

        if not facebook_host(
            p.netloc
        ):
            continue

        q = query_dict(
            p
        )

        uid = normalize_id(
            q.get("id")
        )

        if uid:

            urls.append(
                value
            )

        parts = [
            x for x in p.path.split("/")
            if x
        ]

        if len(parts) == 1:

            if USERNAME_RE.match(
                parts[0]
            ):

                urls.append(
                    value
                )

    # HTML links
    for href in snapshot.links:

        absolute = urljoin(
            snapshot.final_url,
            href
        )

        p = parse_url(
            absolute
        )

        if not p:
            continue

        if not facebook_host(
            p.netloc
        ):
            continue

        if (
            "/profile.php"
            in p.path.lower()
        ):

            urls.append(
                absolute
            )

            continue

        parts = [
            x for x in p.path.split("/")
            if x
        ]

        if len(parts) == 1:

            name = parts[0]

            if USERNAME_RE.match(
                name
            ):

                if name.lower() not in {
                    "home",
                    "watch",
                    "groups",
                    "pages",
                    "photos",
                    "videos",
                    "reels",
                    "events",
                    "marketplace",
                }:

                    urls.append(
                        absolute
                    )

    # dedup
    output = []

    seen = set()

    for url in urls:

        key = url.rstrip(
            "/"
        ).lower()

        if key in seen:
            continue

        seen.add(key)

        output.append(
            url
        )

        if len(output) >= (
            MAX_PROFILE_CHECKS
        ):

            break

    return output


# ======================================================================
# VERIFY PROFILE
# ======================================================================

def verify_profile(
    fetcher,
    url
):

    fetched = fetcher.fetch(
        url,
        timeout=PROFILE_TIMEOUT
    )

    if not fetched.get(
        "text"
    ):

        return None

    snapshot = parse_snapshot(
        url,
        fetched
    )

    candidates = {}

    # Direct profile query.
    for target in (
        url,
        snapshot.final_url,
        snapshot.canonical_url,
        snapshot.meta.get(
            "og:url",
            ""
        ),
    ):

        if not target:
            continue

        p = parse_url(
            target
        )

        if not p:
            continue

        q = query_dict(
            p
        )

        uid = normalize_id(
            q.get("id")
        )

        if uid:

            candidates.setdefault(
                uid,
                0
            )

            candidates[
                uid
            ] += 50

    # Semantic USER/PUBLISHER.
    temp = Signals()

    extract_semantic_signals(
        snapshot.html_text,
        temp
    )

    for signal in temp.items:

        if signal.role not in {
            "USER",
            "PUBLISHER",
        }:

            continue

        uid = normalize_id(
            signal.value
        )

        if not uid:
            continue

        candidates.setdefault(
            uid,
            0
        )

        candidates[
            uid
        ] += signal.weight

    if not candidates:

        return None

    uid = max(
        candidates,
        key=candidates.get
    )

    score = candidates[
        uid
    ]

    # Profile URL matching.
    requested_parts = [
        x for x in urlparse(url).path.split("/")
        if x
    ]

    final_parts = [
        x for x in urlparse(
            snapshot.final_url
        ).path.split("/")
        if x
    ]

    strong = False

    if (
        requested_parts
        and final_parts
        and requested_parts[0].lower()
        == final_parts[0].lower()
    ):

        strong = True

    if (
        "/profile.php"
        in snapshot.final_url.lower()
    ):

        strong = True

    return {
        "uid": uid,
        "score": score,
        "strong": strong,
        "final_url": snapshot.final_url,
        "canonical": snapshot.canonical_url,
        "title": snapshot.title,
    }


# ======================================================================
# CANDIDATE BUILDING
# ======================================================================

def build_user_candidates(
    signals,
    profile_verifications
):

    candidates = {}

    for signal in signals.items:

        # ----------------------------------------------------------
        # Only these roles can contribute to USER UID.
        # ----------------------------------------------------------

        if signal.role not in {
            "USER",
            "PUBLISHER",
            "OWNER",
        }:

            continue

        uid = normalize_id(
            signal.value
        )

        if not uid:
            continue

        candidate = candidates.setdefault(
            uid,
            UIDCandidate(
                uid=uid
            )
        )

        candidate.score += (
            signal.weight
        )

        candidate.sources.add(
            signal.independent
            or signal.source
        )

        if signal.context:

            candidate.contexts.append(
                signal.context
            )

    # Profile verification
    for verification in (
        profile_verifications
    ):

        uid = normalize_id(
            verification.get(
                "uid"
            )
        )

        if not uid:
            continue

        candidate = candidates.setdefault(
            uid,
            UIDCandidate(
                uid=uid
            )
        )

        candidate.score += 30

        candidate.sources.add(
            "verified_profile"
        )

        if verification.get(
            "strong"
        ):

            candidate.score += 25

            candidate.verified = True

    return candidates


# ======================================================================
# BEST USER UID
# ======================================================================

def select_user_uid(
    candidates,
    entity_type
):

    if not candidates:

        return "", 0, "NONE"

    ranked = sorted(
        candidates.values(),
        key=lambda x: (
            x.verified,
            x.score,
            len(x.sources),
        ),
        reverse=True
    )

    best = ranked[0]

    # --------------------------------------------------------------
    # Conflict protection
    # --------------------------------------------------------------

    if len(ranked) >= 2:

        second = ranked[1]

        if (
            abs(
                best.score
                - second.score
            ) < 10
            and not best.verified
        ):

            return (
                "",
                0,
                "CONFLICT"
            )

    # --------------------------------------------------------------
    # Group:
    # user UID phải được verify.
    # --------------------------------------------------------------

    if entity_type.startswith(
        "GROUP"
    ):

        if not best.verified:

            return (
                "",
                0,
                "UNVERIFIED_GROUP_PUBLISHER"
            )

    # --------------------------------------------------------------
    # Minimum confidence.
    # --------------------------------------------------------------

    if best.score < 45:

        return (
            "",
            int(best.score),
            "WEAK"
        )

    confidence = min(
        99,
        int(
            best.score
        )
    )

    return (
        best.uid,
        confidence,
        "OK"
    )


# ======================================================================
# ENTITY CLASSIFICATION
# ======================================================================

def determine_entity(
    fburl,
    snapshot,
    signals,
    page_candidates,
    group_candidates
):

    """
    URL STRUCTURE + CANONICAL được ưu tiên hơn
    numeric IDs trong HTML.
    """

    kind = fburl.kind

    # ==============================================================
    # GROUP
    # ==============================================================

    if kind.startswith(
        "GROUP"
    ) or fburl.route_entity == "GROUP":

        if "PHOTO" in kind:

            return "GROUP_PHOTO"

        if "VIDEO" in kind:

            return "GROUP_VIDEO"

        if "POST" in kind:

            return "GROUP_POST"

        return "GROUP"

    # ==============================================================
    # PAGE
    # ==============================================================

    if kind.startswith(
        "PAGE"
    ):

        if "PHOTO" in kind:

            return "PAGE_PHOTO"

        if "VIDEO" in kind:

            return "PAGE_VIDEO"

        if "POST" in kind:

            return "PAGE_POST"

        return "PAGE"

    # ==============================================================
    # PHOTO
    # ==============================================================

    if kind == "PHOTO":

        # photo.php is object PHOTO,
        # publisher separately USER/PAGE.
        return "PHOTO"

    # ==============================================================
    # STORY
    # ==============================================================

    if kind == "STORY":

        return "STORY"

    # ==============================================================
    # VIDEO
    # ==============================================================

    if kind == "VIDEO":

        return "VIDEO"

    # ==============================================================
    # REEL
    # ==============================================================

    if kind == "REEL":

        return "REEL"

    # ==============================================================
    # LIVE
    # ==============================================================

    if kind == "LIVE":

        return "LIVE"

    # ==============================================================
    # PROFILE
    # ==============================================================

    if kind in {
        "PROFILE",
        "PROFILE_ID",
    }:

        return "USER"

    # ==============================================================
    # SHARE
    # ==============================================================

    if kind == "SHARE_POST":

        return "POST"

    if kind == "SHARE_VIDEO":

        return "VIDEO"

    if kind == "SHARE_REEL":

        return "REEL"

    if kind == "SHARE":

        return "UNKNOWN"

    # ==============================================================
    # POST
    # ==============================================================

    if kind == "POST":

        # If canonical route is group
        canonical = (
            snapshot.canonical_url
            or snapshot.final_url
        )

        canonical_fb = classify_url(
            canonical
        )

        if (
            canonical_fb.route_entity
            == "GROUP"
        ):

            return "GROUP_POST"

        if (
            canonical_fb.route_entity
            == "PAGE"
        ):

            return "PAGE_POST"

        return "POST"

    return kind


# ======================================================================
# OBJECT ID ASSIGNMENT
# ======================================================================

def best_signal_value(
    signals,
    role
):

    scores = {}

    for signal in signals.items:

        if signal.role != role:
            continue

        value = str(
            signal.value
        )

        # Object IDs can be opaque.
        scores.setdefault(
            value,
            0
        )

        scores[
            value
        ] += signal.weight

    if not scores:
        return ""

    return max(
        scores,
        key=scores.get
    )


# ======================================================================
# MAIN RESOLVER
# ======================================================================

class Resolver:

    def __init__(self):

        self.fetcher = Fetcher()

    async def resolve(
        self,
        url
    ):

        started = time.perf_counter()

        fburl = classify_url(
            url
        )

        result = Result(
            original_url=url,
            url_type=fburl.kind
        )

        # ----------------------------------------------------------
        # Validate
        # ----------------------------------------------------------

        parsed = parse_url(
            fburl.normalized
        )

        if not parsed or not facebook_host(
            parsed.netloc
        ):

            result.status = (
                "INVALID"
            )

            result.warnings.append(
                "Không phải URL Facebook."
            )

            result.elapsed = (
                time.perf_counter()
                - started
            )

            return result

        # ----------------------------------------------------------
        # Initial signals from URL.
        # ----------------------------------------------------------

        signals = Signals()

        extract_query_semantics(
            fburl,
            signals
        )

        if fburl.opaque_token:

            signals.add(
                fburl.opaque_token,
                "OPAQUE",
                "url_token",
                1,
                context=fburl.normalized,
                independent="url_token",
            )

        if fburl.object_id:

            numeric_object = normalize_id(
                fburl.object_id
            )

            if numeric_object:

                role = {
                    "PHOTO": "PHOTO",
                    "VIDEO": "VIDEO",
                    "REEL": "REEL",
                    "STORY": "STORY",
                    "POST": "POST",
                    "LIVE": "LIVE",
                    "EVENT": "EVENT",
                    "MARKETPLACE": "MARKETPLACE",
                }.get(
                    fburl.object_kind
                )

                if role:

                    signals.add(
                        numeric_object,
                        role,
                        "route_object_id",
                        40,
                        context=fburl.normalized,
                        independent="route_object_id",
                    )

        # ----------------------------------------------------------
        # Fetch
        # ----------------------------------------------------------

        fetched = await asyncio.to_thread(
            self.fetcher.fetch,
            fburl.normalized
        )

        snapshot = parse_snapshot(
            fburl.normalized,
            fetched
        )

        result.resolved_url = (
            snapshot.final_url
            or fburl.normalized
        )

        result.canonical_url = (
            snapshot.canonical_url
            or snapshot.final_url
            or fburl.normalized
        )

        if snapshot.redirect_chain:

            result.warnings.append(
                "URL đã được Facebook redirect."
            )

        if snapshot.error:

            result.warnings.append(
                "HTTP: "
                + snapshot.error[:200]
            )

        # ----------------------------------------------------------
        # IMPORTANT:
        # Reclassify final URL.
        # Canonical type gets priority.
        # ----------------------------------------------------------

        final_fburl = classify_url(
            snapshot.final_url
        )

        canonical_fburl = classify_url(
            snapshot.canonical_url
            or snapshot.final_url
        )

        # canonical photo wins over generic share.
        if canonical_fburl.kind not in {
            "UNKNOWN",
            "SHARE",
            "PROFILE",
        }:

            if (
                fburl.kind.startswith(
                    "SHARE"
                )
                or fburl.kind == "UNKNOWN"
            ):

                fburl.kind = (
                    canonical_fburl.kind
                )

                fburl.object_kind = (
                    canonical_fburl.object_kind
                )

        # ----------------------------------------------------------
        # Analyze canonical FIRST.
        # ----------------------------------------------------------

        analyze_canonical(
            snapshot.final_url,
            signals
        )

        analyze_canonical(
            snapshot.canonical_url,
            signals
        )

        # ----------------------------------------------------------
        # Meta
        # ----------------------------------------------------------

        analyze_meta(
            snapshot,
            signals
        )

        # ----------------------------------------------------------
        # Links
        # ----------------------------------------------------------

        analyze_links(
            snapshot,
            signals
        )

        # ----------------------------------------------------------
        # Raw HTML
        # ----------------------------------------------------------

        analyze_raw_html(
            snapshot,
            signals,
            object_id=fburl.object_id
        )

        # ----------------------------------------------------------
        # JSON-LD
        # ----------------------------------------------------------

        jsonld_signals(
            snapshot,
            signals
        )

        # ----------------------------------------------------------
        # Entity
        # ----------------------------------------------------------

        page_scores = {}

        group_scores = {}

        for signal in signals.items:

            if signal.role == "PAGE":

                page_scores.setdefault(
                    signal.value,
                    0
                )

                page_scores[
                    signal.value
                ] += signal.weight

            elif signal.role == "GROUP":

                group_scores.setdefault(
                    signal.value,
                    0
                )

                group_scores[
                    signal.value
                ] += signal.weight

        entity_type = determine_entity(
            fburl,
            snapshot,
            signals,
            page_scores,
            group_scores
        )

        result.entity_type = (
            entity_type
        )

        result.object_type = (
            fburl.object_kind
        )

        # ----------------------------------------------------------
        # Object IDs
        # ----------------------------------------------------------

        # Re-evaluate after canonical.
        if entity_type in {
            "PHOTO",
            "PAGE_PHOTO",
            "GROUP_PHOTO",
        }:

            result.photo_id = (
                best_signal_value(
                    signals,
                    "PHOTO"
                )
            )

        elif entity_type in {
            "VIDEO",
            "PAGE_VIDEO",
            "GROUP_VIDEO",
        }:

            result.video_id = (
                best_signal_value(
                    signals,
                    "VIDEO"
                )
            )

        elif entity_type == "REEL":

            result.reel_id = (
                best_signal_value(
                    signals,
                    "REEL"
                )
            )

        elif entity_type == "STORY":

            result.story_id = (
                best_signal_value(
                    signals,
                    "STORY"
                )
            )

        elif entity_type == "LIVE":

            result.video_id = (
                best_signal_value(
                    signals,
                    "LIVE"
                )
            )

        elif entity_type in {
            "POST",
            "PAGE_POST",
            "GROUP_POST",
        }:

            result.post_id = (
                best_signal_value(
                    signals,
                    "POST"
                )
            )

        # ----------------------------------------------------------
        # If route object ID is numeric and semantic collector
        # did not find it.
        # ----------------------------------------------------------

        if not result.photo_id and (
            fburl.object_kind == "PHOTO"
        ):

            result.photo_id = normalize_id(
                fburl.object_id
            )

        if not result.video_id and (
            fburl.object_kind == "VIDEO"
        ):

            result.video_id = normalize_id(
                fburl.object_id
            )

        if not result.reel_id and (
            fburl.object_kind == "REEL"
        ):

            result.reel_id = normalize_id(
                fburl.object_id
            )

        if not result.story_id and (
            fburl.object_kind == "STORY"
        ):

            result.story_id = normalize_id(
                fburl.object_id
            )

        if not result.post_id and (
            fburl.object_kind == "POST"
        ):

            result.post_id = (
                normalize_id(
                    fburl.object_id
                )
            )

        # Opaque post token.
        if (
            not result.post_id
            and fburl.opaque_token
            and entity_type in {
                "POST",
                "GROUP_POST",
                "PAGE_POST",
            }
        ):

            result.post_id = (
                fburl.opaque_token
            )

        # ----------------------------------------------------------
        # Album
        # ----------------------------------------------------------

        result.album_id = (
            best_signal_value(
                signals,
                "ALBUM"
            )
        )

        # ----------------------------------------------------------
        # Group
        # ----------------------------------------------------------

        if entity_type.startswith(
            "GROUP"
        ):

            group_scores = {}

            for signal in signals.items:

                if signal.role == "GROUP":

                    group_scores.setdefault(
                        signal.value,
                        0
                    )

                    group_scores[
                        signal.value
                    ] += signal.weight

            if group_scores:

                result.group_id = max(
                    group_scores,
                    key=group_scores.get
                )

            elif fburl.publisher_hint:

                result.group_id = (
                    fburl.publisher_hint
                )

            result.publisher_type = (
                "GROUP"
            )

        # ----------------------------------------------------------
        # Page
        # ----------------------------------------------------------

        if entity_type.startswith(
            "PAGE"
        ):

            page_scores = {}

            for signal in signals.items:

                if signal.role == "PAGE":

                    page_scores.setdefault(
                        signal.value,
                        0
                    )

                    page_scores[
                        signal.value
                    ] += signal.weight

            if page_scores:

                result.page_uid = max(
                    page_scores,
                    key=page_scores.get
                )

            result.publisher_type = (
                "PAGE"
            )

        # ----------------------------------------------------------
        # Publisher hint from photo.php?id=
        # ----------------------------------------------------------

        publisher_hint = (
            fburl.publisher_hint
        )

        if publisher_hint:

            signals.add(
                publisher_hint,
                "USER",
                "url_publisher_hint",
                40,
                context=fburl.normalized,
                object_id=fburl.object_id,
                independent="url_publisher_hint",
            )

        # ----------------------------------------------------------
        # Discover profiles
        # ----------------------------------------------------------

        profile_urls = discover_profiles(
            fburl,
            snapshot
        )

        profile_verifications = []

        for profile_url in profile_urls:

            verification = (
                await asyncio.to_thread(
                    verify_profile,
                    self.fetcher,
                    profile_url
                )
            )

            if verification:

                profile_verifications.append(
                    verification
                )

            if len(
                profile_verifications
            ) >= MAX_PROFILE_CHECKS:

                break

        # ----------------------------------------------------------
        # USER candidates
        # ----------------------------------------------------------

        candidates = (
            build_user_candidates(
                signals,
                profile_verifications
            )
        )

        # ----------------------------------------------------------
        # USER UID
        # ----------------------------------------------------------

        if entity_type not in {
            "PAGE",
            "PAGE_POST",
            "PAGE_PHOTO",
            "PAGE_VIDEO",
        }:

            uid, confidence, reason = (
                select_user_uid(
                    candidates,
                    entity_type
                )
            )

            if uid:

                result.user_uid = uid

                result.publisher_id = uid

                result.publisher_type = (
                    "USER"
                    if not entity_type.startswith(
                        "GROUP"
                    )
                    else "USER"
                )

                result.confidence = (
                    confidence
                )

            else:

                if reason in {
                    "CONFLICT",
                    "WEAK",
                    "UNVERIFIED_GROUP_PUBLISHER",
                }:

                    result.warnings.append(
                        "USER UID chưa đủ bằng chứng độc lập."
                    )

        # ----------------------------------------------------------
        # PHOTO SPECIAL CASE
        #
        # photo.php?fbid=PHOTO&id=USER
        #
        # Đây là trường hợp rất quan trọng.
        # Nếu id=USER xuất hiện ngay trong canonical URL
        # và profile correlation khớp thì ưu tiên nó.
        # ----------------------------------------------------------

        if entity_type in {
            "PHOTO",
            "PAGE_PHOTO",
            "GROUP_PHOTO",
        }:

            canonical_photo = classify_url(
                snapshot.canonical_url
                or snapshot.final_url
            )

            if canonical_photo.kind == "PHOTO":

                direct_user = normalize_id(
                    canonical_photo.query.get(
                        "id"
                    )
                )

                photo = normalize_id(
                    canonical_photo.query.get(
                        "fbid"
                    )
                )

                if direct_user:

                    # Không được lấy fbid làm USER.
                    result.user_uid = (
                        direct_user
                    )

                    result.publisher_id = (
                        direct_user
                    )

                    if entity_type == "PHOTO":

                        result.publisher_type = (
                            "USER"
                        )

                    result.confidence = max(
                        result.confidence,
                        88
                    )

                if photo:

                    result.photo_id = photo

        # ----------------------------------------------------------
        # Username
        # ----------------------------------------------------------

        username = (
            fburl.username
        )

        if not username:

            final_parts = [
                x for x in urlparse(
                    snapshot.final_url
                ).path.split("/")
                if x
            ]

            if final_parts:

                if (
                    len(final_parts) >= 2
                    and final_parts[1].lower()
                    in {
                        "posts",
                        "photos",
                        "photo",
                        "videos",
                        "video",
                        "reels",
                        "reel",
                    }
                ):

                    username = (
                        final_parts[0]
                    )

        result.username = (
            username
        )

        # ----------------------------------------------------------
        # Publisher name
        # ----------------------------------------------------------

        title = clean(
            snapshot.meta.get(
                "og:title",
                ""
            )
            or snapshot.title
        )

        title = re.sub(
            r"\s*(?:\||-)\s*Facebook.*$",
            "",
            title,
            flags=re.I
        )

        result.publisher_name = (
            title[:200]
        )

        result.title = title[:300]

        # ----------------------------------------------------------
        # Opaque token
        # ----------------------------------------------------------

        result.opaque_token = (
            fburl.opaque_token
            or (
                PFBID_RE.search(
                    snapshot.html_text
                ).group()
                if PFBID_RE.search(
                    snapshot.html_text
                )
                else ""
            )
        )

        # ----------------------------------------------------------
        # Confidence based on URL type.
        # ----------------------------------------------------------

        if entity_type == "PHOTO":

            if result.photo_id:

                if result.user_uid:

                    result.confidence = max(
                        result.confidence,
                        90
                    )

                else:

                    result.confidence = max(
                        result.confidence,
                        75
                    )

        elif entity_type == "USER":

            if result.user_uid:

                result.confidence = max(
                    result.confidence,
                    90
                )

        elif entity_type.startswith(
            "GROUP"
        ):

            if result.group_id:

                result.confidence = max(
                    result.confidence,
                    85
                )

        elif entity_type.startswith(
            "PAGE"
        ):

            if result.page_uid:

                result.confidence = max(
                    result.confidence,
                    85
                )

        # ----------------------------------------------------------
        # Status
        # ----------------------------------------------------------

        if (
            result.user_uid
            and result.confidence >= 80
        ):

            result.status = (
                "VERIFIED"
            )

        elif (
            result.page_uid
            and entity_type.startswith(
                "PAGE"
            )
        ):

            result.status = (
                "VERIFIED"
            )

        elif (
            result.group_id
            and entity_type.startswith(
                "GROUP"
            )
        ):

            result.status = (
                "VERIFIED"
            )

        elif (
            result.photo_id
            or result.video_id
            or result.reel_id
            or result.story_id
            or result.post_id
        ):

            result.status = (
                "PARTIAL"
            )

        else:

            result.status = (
                "UNRESOLVED"
            )

        # ----------------------------------------------------------
        # Evidence
        # ----------------------------------------------------------

        result.evidence = len(
            signals.items
        )

        # ----------------------------------------------------------
        # Remove impossible fields
        # ----------------------------------------------------------

        # PHOTO must not show VIDEO/STORY just because
        # those IDs happened to be present in HTML.
        if entity_type in {
            "PHOTO",
            "PAGE_PHOTO",
            "GROUP_PHOTO",
        }:

            result.video_id = ""

            result.story_id = ""

            result.reel_id = ""

            result.post_id = ""

        # VIDEO
        if entity_type in {
            "VIDEO",
            "PAGE_VIDEO",
            "GROUP_VIDEO",
            "REEL",
        }:

            result.photo_id = ""

            result.story_id = ""

        # STORY
        if entity_type == "STORY":

            result.photo_id = ""

            result.video_id = ""

            result.reel_id = ""

        # PAGE
        if entity_type.startswith(
            "PAGE"
        ):

            result.user_uid = ""

            if result.page_uid:

                result.publisher_id = (
                    result.page_uid
                )

        # GROUP
        if entity_type.startswith(
            "GROUP"
        ):

            # group ID is never user UID.
            if result.user_uid == (
                result.group_id
            ):

                result.user_uid = ""

        result.elapsed = (
            time.perf_counter()
            - started
        )

        return result


# ======================================================================
# PUBLIC FUNCTIONS
# ======================================================================

async def resolve_facebook_url(
    url
):

    resolver = Resolver()

    return await resolver.resolve(
        url
    )


async def resolve_facebook_urls(
    urls
):

    resolver = Resolver()

    output = []

    for url in urls:

        try:

            output.append(
                await resolver.resolve(
                    url
                )
            )

        except Exception as exc:

            output.append(
                Result(
                    original_url=url,
                    status="ERROR",
                    warnings=[
                        str(exc)[:500]
                    ]
                )
            )

    return output


# ======================================================================
# DISPLAY
# ======================================================================

TYPE_ICON = {
    "USER": "👤 USER",
    "POST": "📝 POST",
    "PHOTO": "🖼 PHOTO",
    "VIDEO": "🎬 VIDEO",
    "REEL": "🎬 REEL",
    "STORY": "⭕ STORY",
    "LIVE": "🔴 LIVE",
    "GROUP": "👥 GROUP",
    "GROUP_POST": "👥 GROUP POST",
    "GROUP_PHOTO": "👥 GROUP PHOTO",
    "GROUP_VIDEO": "👥 GROUP VIDEO",
    "PAGE": "📄 PAGE",
    "PAGE_POST": "📄 PAGE POST",
    "PAGE_PHOTO": "📄 PAGE PHOTO",
    "PAGE_VIDEO": "📄 PAGE VIDEO",
    "MARKETPLACE": "🛒 MARKETPLACE",
    "EVENT": "📅 EVENT",
    "UNKNOWN": "❓ UNKNOWN",
}


def format_result(
    result,
    index=None
):

    lines = []

    lines.append(
        "🔎 FACEBOOK UID V21 MAX ACCURACY"
    )

    lines.append("")

    if index is not None:

        lines.append(
            f"🔗 URL #{index}"
        )

    lines.append(
        "🏷 URL TYPE: "
        + result.url_type
    )

    lines.append(
        "📦 ENTITY TYPE: "
        + TYPE_ICON.get(
            result.entity_type,
            result.entity_type
        )
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

    if result.publisher_type != "UNKNOWN":

        lines.append(
            "👤 PUBLISHER TYPE: "
            + result.publisher_type
        )

    if result.publisher_id:

        lines.append(
            "🆔 PUBLISHER ID: "
            + result.publisher_id
        )

    if result.username:

        lines.append(
            "📛 USERNAME: @"
            + result.username
        )

    if result.publisher_name:

        lines.append(
            "📌 PUBLISHER: "
            + result.publisher_name
        )

    # --------------------------------------------------------------
    # OBJECT IDs
    # --------------------------------------------------------------

    if result.post_id:

        lines.append(
            "📝 POST ID: "
            + result.post_id
        )

    if result.photo_id:

        lines.append(
            "🖼 PHOTO ID: "
            + result.photo_id
        )

    if result.video_id:

        lines.append(
            "🎬 VIDEO ID: "
            + result.video_id
        )

    if result.reel_id:

        lines.append(
            "🎬 REEL ID: "
            + result.reel_id
        )

    if result.story_id:

        lines.append(
            "⭕ STORY ID: "
            + result.story_id
        )

    if result.album_id:

        lines.append(
            "🗂 ALBUM ID: "
            + result.album_id
        )

    if result.opaque_token:

        lines.append(
            "🔐 OBJECT TOKEN: "
            + result.opaque_token
        )

    lines.append(
        "🎯 CONFIDENCE: "
        + str(
            result.confidence
        )
        + "%"
    )

    lines.append(
        "📊 EVIDENCE: "
        + str(
            result.evidence
        )
        + " signals"
    )

    if result.resolved_url:

        lines.append("")

        lines.append(
            "🔗 RESOLVED:"
        )

        lines.append(
            result.resolved_url[:1000]
        )

    if (
        result.canonical_url
        and
        result.canonical_url
        != result.resolved_url
    ):

        lines.append("")

        lines.append(
            "🔗 CANONICAL:"
        )

        lines.append(
            result.canonical_url[:1000]
        )

    if result.warnings:

        lines.append("")

        lines.append(
            "⚠️ WARNINGS:"
        )

        seen = set()

        for warning in result.warnings:

            warning = clean(
                warning
            )

            if warning in seen:
                continue

            seen.add(
                warning
            )

            lines.append(
                "• "
                + warning[:350]
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
# TELEGRAM SESSION
# ======================================================================

_ACTIVE = {}

_LOCK = asyncio.Lock()


async def wait_message(
    bot,
    event,
    timeout=900
):

    sender_id = (
        event.sender_id
    )

    chat_id = (
        event.chat_id
    )

    loop = (
        asyncio.get_running_loop()
    )

    future = (
        loop.create_future()
    )

    builder = events.NewMessage(
        chats=chat_id
    )

    async def waiter(
        incoming
    ):

        if future.done():
            return

        if incoming.sender_id != (
            sender_id
        ):

            return

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
            timeout
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
# /getuidfb
# ======================================================================

async def _handle_getuidfb(
    event,
    notify_bot=None
):

    user_id = (
        event.sender_id
    )

    async with _LOCK:

        if user_id in _ACTIVE:

            await event.reply(
                "⚠️ Bạn đang có phiên "
                "/getuidfb đang hoạt động.\n"
                "Hãy gửi URL Facebook vào phiên hiện tại."
            )

            return

        _ACTIVE[user_id] = True

    try:

        await event.reply(
            "🔎 **FACEBOOK UID V21 MAX ACCURACY**\n\n"
            "Gửi URL Facebook cần phân tích.\n\n"
            "Hỗ trợ:\n"
            "• Profile\n"
            "• Profile ID\n"
            "• Post\n"
            "• Photo\n"
            "• Video\n"
            "• Reel\n"
            "• Story\n"
            "• Live\n"
            "• Group\n"
            "• Page\n"
            "• Share link\n\n"
            "Có thể gửi nhiều URL cùng lúc.\n"
            "Kể cả URL dính liền nhau.\n\n"
            "Ví dụ:\n"
            "`https://facebook.com/share/p/AAA/https://facebook.com/user/posts/BBB`\n\n"
            "⏳ Phiên chờ 15 phút."
        )

        while True:

            incoming = (
                await wait_message(
                    event.client,
                    event,
                    timeout=900
                )
            )

            if incoming is None:

                await event.reply(
                    "⌛ Phiên /getuidfb đã hết hạn."
                )

                break

            text = (
                incoming.raw_text
                or ""
            ).strip()

            # Command khác -> thoát.
            if text.startswith(
                "/"
            ):

                break

            urls = extract_facebook_urls(
                text
            )

            if not urls:

                await incoming.reply(
                    "❌ Không tìm thấy URL Facebook.\n"
                    "Hãy gửi URL Facebook hợp lệ."
                )

                continue

            await incoming.reply(
                "🔗 Đã nhận diện "
                f"**{len(urls)} URL Facebook**.\n"
                "⏳ Đang phân loại và xác minh..."
            )

            resolver = Resolver()

            for index, url in enumerate(
                urls,
                1
            ):

                try:

                    result = await resolver.resolve(
                        url
                    )

                    output = format_result(
                        result,
                        index if len(urls) > 1 else None
                    )

                    if len(output) > 4000:

                        output = (
                            output[:3900]
                            + "\n..."
                        )

                    await incoming.reply(
                        output
                    )

                except Exception as exc:

                    await incoming.reply(
                        "❌ URL #"
                        + str(index)
                        + " lỗi:\n"
                        + str(exc)[:1000]
                    )

            await incoming.reply(
                "✅ Đã xử lý "
                + str(len(urls))
                + " URL.\n\n"
                "📥 Gửi URL tiếp theo để tiếp tục.\n"
                "Gửi /stop để kết thúc."
            )

    finally:

        async with _LOCK:

            _ACTIVE.pop(
                user_id,
                None
            )


# ======================================================================
# REGISTER
# ======================================================================

def register(
    bot,
    notify_bot=None
):

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
# COMPATIBILITY
# ======================================================================

handler = _handle_getuidfb


# ======================================================================
# END
# ======================================================================