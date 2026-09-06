#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
======================================================================
 FB UID / ENTITY RESOLVER V21 MAX ACCURACY
======================================================================
PUBLIC HTTP ONLY
NO:
- Playwright
- Selenium
- Cookie
- Facebook Access Token
- Facebook Login
MỤC TIÊU:
- Precision-first: ưu tiên UID ĐÚNG hơn UID đoán
- Không biến numeric ID ngẫu nhiên trong HTML thành USER UID
- Không biến album ID thành GROUP ID
- Không biến photo/video/post ID thành USER UID
- Không biến PAGE UID / GROUP ID thành USER UID
- Phân biệt:
    USER
    PAGE
    GROUP
    USER_POST
    PAGE_POST
    GROUP_POST
    POST
    REEL
    VIDEO
    PHOTO
    STORY
    PROFILE
    EVENT
    MARKETPLACE
- Resolve:
    share/p
    share/v
    share/r
    pfbid
    posts
    permalink.php
    photo.php
    video.php
    watch
    reel
    reels
    story.php
    stories
    profile.php
    pages
    groups
    people
    vanity profile
- Tách URL dính liền.
- Redirect -> canonical -> OG -> JSON-LD -> HTML -> JS
- Publisher correlation
- Profile verification
- Candidate scoring
- Independent evidence
- Conflict detection
- Role isolation
- Interactive /getuidfb loop
- Compatible:
      module.register(bot, notify_bot)
NGUYÊN TẮC:
    opaque token
        |
        v
    resolved URL
        |
        v
    canonical/object
        |
        v
    publisher candidate
        |
        v
    public profile
        |
        v
    numeric UID
        |
        v
    independent verification
KHÔNG CHỨNG MINH ĐƯỢC:
    => KHÔNG ĐOÁN UID.
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
    "description": "Facebook UID / Entity Resolver V21 MAX ACCURACY",
    "category": "facebook",
}
# ======================================================================
# CONFIG
# ======================================================================
REQUEST_TIMEOUT = 14
PROFILE_VERIFY_TIMEOUT = 9
MAX_HTML_SIZE = 12 * 1024 * 1024
MAX_REDIRECTS = 8
MAX_PROFILE_CHECKS = 6
MAX_DISCOVERED_PROFILE_LINKS = 12
MAX_HTML_CONTEXT = 900
# Không để quá nhiều request Facebook cùng lúc từ một process.
MAX_HTTP_CONCURRENCY = 4
# Candidate phải đạt tối thiểu điểm này mới có thể xem xét.
MIN_UID_SCORE = 55
# Nếu hai candidate quá gần nhau thì không đoán.
UID_CONFLICT_MARGIN = 8
USER_AGENT_LIST = [
    (
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 15; Pixel 8) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
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
# FACEBOOK HOSTS
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
# ======================================================================
# REGEX
# ======================================================================
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
NUMERIC_RE = re.compile(
    r"(?<!\d)\d{5,25}(?!\d)"
)
PFBID_RE = re.compile(
    r"(?i)\bpfbid[A-Za-z0-9_-]+\b"
)
SHARE_RE = re.compile(
    r"(?i)/share/(p|v|r|s)/([^/?#]+)"
)
SEMANTIC_ID_RE = re.compile(
    r"(?i)"
    r"(?:"
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
    r"photo[_-]?id"
    r")"
    r"\s*"
    r"[:=]"
    r"\s*"
    r"[\"']?"
    r"(\d{5,25})"
)
TITLE_RE = re.compile(
    r"<title[^>]*>(.*?)</title>",
    re.I | re.S,
)
# ======================================================================
# DATA CLASSES
# ======================================================================
@dataclass
class ParsedURL:
    original: str
    normalized: str
    host: str = ""
    path: str = ""
    query: dict = field(default_factory=dict)
    url_type: str = "UNKNOWN"
    route_entity: str = "UNKNOWN"
    username: str = ""
    numeric_route_id: str = ""
    opaque_token: str = ""
    post_route_id: str = ""
    video_route_id: str = ""
    photo_route_id: str = ""
    story_route_id: str = ""
    reel_route_id: str = ""
    page_route_id: str = ""
    group_route_id: str = ""
    album_id: str = ""
    is_share: bool = False
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
    redirects: list = field(default_factory=list)
    errors: list = field(default_factory=list)
@dataclass
class Evidence:
    value: str
    role: str
    source: str
    weight: float
    context: str = ""
    object_token: str = ""
    key: str = ""
    independent_key: str = ""
    # Mức độ gắn với URL/object hiện tại.
    locality: float = 1.0
    # Có phải evidence trực tiếp không?
    direct: bool = False
    # Evidence được verify từ profile.
    verified: bool = False
@dataclass
class Candidate:
    value: str
    score: float = 0.0
    sources: list = field(default_factory=list)
    independent_sources: set = field(default_factory=set)
    contexts: list = field(default_factory=list)
    verified: bool = False
    rejected: bool = False
    direct_count: int = 0
    strong_count: int = 0
@dataclass
class ProfileVerification:
    requested_url: str = ""
    uid: str = ""
    username: str = ""
    canonical: str = ""
    final_url: str = ""
    title: str = ""
    entity_kind: str = "UNKNOWN"
    strong: bool = False
    evidence: list = field(default_factory=list)
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
    reel_id: str = ""
    album_id: str = ""
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
                "application/x-www-form-urlencoded"
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
            script_type = (
                self.current_script_attrs
                .get("type", "")
                .lower()
            )
            if (
                script_type
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
# TEXT HELPERS
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
    if len(value) > 25:
        return ""
    return value
def is_probable_numeric_id(value):
    value = normalize_numeric(value)
    if not value:
        return False
    if len(value) < 5 or len(value) > 25:
        return False
    # Timestamp / year-like noise.
    if (
        len(value) >= 10
        and value[:4] in {
            "1900",
            "1990",
            "1991",
            "1992",
            "1993",
            "1994",
            "1995",
            "1996",
            "1997",
            "1998",
            "1999",
            "2000",
            "2001",
            "2002",
            "2003",
            "2004",
            "2005",
            "2006",
            "2007",
            "2008",
            "2009",
            "2010",
            "2011",
            "2012",
            "2013",
            "2014",
            "2015",
            "2016",
            "2017",
            "2018",
            "2019",
            "2020",
            "2021",
            "2022",
            "2023",
            "2024",
            "2025",
            "2026",
        }
    ):
        return False
    return True
def same_numeric(a, b):
    a = normalize_numeric(a)
    b = normalize_numeric(b)
    return bool(
        a
        and b
        and a == b
    )
def host_is_facebook(host):
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
        str(url).strip()
    )
    url = url.strip(
        " \t\r\n<>\"'`"
    )
    if not re.match(
        r"(?i)^https?://",
        url
    ):
        url = "https://" + url
    return url
def safe_parse_url(url):
    try:
        return urlparse(
            normalize_url(url)
        )
    except Exception:
        return None
def normalize_fb_url(url):
    parsed = safe_parse_url(url)
    if not parsed:
        return ""
    if not host_is_facebook(
        parsed.netloc
    ):
        return ""
    scheme = "https"
    host = (
        parsed.netloc
        .lower()
        .split(":")[0]
    )
    # Giữ www cho output ổn định.
    if host == "facebook.com":
        host = "www.facebook.com"
    path = parsed.path or "/"
    return (
        scheme
        + "://"
        + host
        + path
        + (
            "?"
            + parsed.query
            if parsed.query
            else ""
        )
        + (
            "#"
            + parsed.fragment
            if parsed.fragment
            else ""
        )
    )
# ======================================================================
# URL EXTRACTION
# ======================================================================
def extract_facebook_urls(text):
    """
    Tách URL Facebook kể cả:
        URL1 URL2
        URL1
        URL2
        URL1,URL2
        URL1/https://facebook.com/URL2
    Không dùng regex greedily lấy cả dòng.
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
    for index, match in enumerate(
        matches
    ):
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
        # URL không được chứa khoảng trắng.
        cut = re.search(
            r"""[\s<>"'`]+""",
            chunk
        )
        if cut:
            chunk = chunk[
                :cut.start()
            ]
        # Dấu câu cuối URL.
        chunk = chunk.strip()
        chunk = chunk.rstrip(
            ".,;!?)]}>"
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
    output = []
    seen = set()
    for url in results:
        normalized = normalize_fb_url(
            url
        )
        if not normalized:
            continue
        key = normalized.rstrip(
            "/"
        ).lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(
            normalized
        )
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
        )
    host = (
        parsed.netloc
        .lower()
        .split(":")[0]
    )
    path = unquote(
        parsed.path or ""
    )
    path_lower = path.lower()
    raw_qs = parse_qs(
        parsed.query,
        keep_blank_values=True
    )
    query = {}
    for key, values in raw_qs.items():
        key = key.lower()
        if values:
            query[key] = unquote(
                values[-1]
            )
    result = ParsedURL(
        original=url,
        normalized=normalized,
        host=host,
        path=path,
        query=query,
    )
    # ==============================================================
    # profile.php
    # ==============================================================
    if path_lower.rstrip(
        "/"
    ).endswith(
        "/profile.php"
    ):
        result.url_type = "PROFILE"
        result.route_entity = "USER"
        result.numeric_route_id = (
            normalize_numeric(
                query.get("id", "")
            )
        )
        return result
    # ==============================================================
    # pages
    # ==============================================================
    page_match = re.search(
        r"/pages/([^/]+)(?:/(\d+))?",
        path,
        re.I
    )
    if page_match:
        result.route_entity = "PAGE"
        if page_match.group(2):
            result.page_route_id = (
                normalize_numeric(
                    page_match.group(2)
                )
            )
            result.numeric_route_id = (
                result.page_route_id
            )
        result.url_type = "PAGE"
        return result
    # ==============================================================
    # groups
    # ==============================================================
    group_match = re.search(
        r"/groups/([^/?#]+)",
        path,
        re.I
    )
    if group_match:
        token = group_match.group(1)
        result.route_entity = "GROUP"
        if token.isdigit():
            result.group_route_id = (
                normalize_numeric(token)
            )
            result.numeric_route_id = (
                result.group_route_id
            )
        if re.search(
            r"/groups/[^/]+/"
            r"(?:posts?|permalink|"
            r"permalink\.php)",
            path,
            re.I
        ):
            result.url_type = "GROUP_POST"
        elif re.search(
            r"/groups/[^/]+/"
            r"(?:videos?|reels?|"
            r"photos?|media)",
            path,
            re.I
        ):
            result.url_type = "GROUP_MEDIA"
        else:
            result.url_type = "GROUP"
        # Numeric ID cuối route có thể là post.
        parts = [
            x for x in path.split("/")
            if x
        ]
        if (
            result.url_type == "GROUP_POST"
            and len(parts) >= 4
        ):
            last = parts[-1]
            if last.isdigit():
                result.post_route_id = (
                    normalize_numeric(last)
                )
        return result
    # ==============================================================
    # SHARE
    # ==============================================================
    share_match = SHARE_RE.search(
        path
    )
    if share_match:
        kind = (
            share_match.group(1)
            .lower()
        )
        token = share_match.group(2)
        result.is_share = True
        result.opaque_token = token
        if kind == "p":
            result.url_type = "POST"
        elif kind == "v":
            result.url_type = "VIDEO"
        elif kind == "r":
            result.url_type = "REEL"
        else:
            result.url_type = "STORY"
        return result
    # ==============================================================
    # permalink.php
    # ==============================================================
    if path_lower.rstrip(
        "/"
    ).endswith(
        "/permalink.php"
    ):
        result.url_type = "POST"
        story_fbid = normalize_numeric(
            query.get(
                "story_fbid",
                ""
            )
        )
        post_id = normalize_numeric(
            query.get(
                "post_id",
                ""
            )
        )
        result.post_route_id = (
            story_fbid
            or post_id
        )
        if not result.post_route_id:
            result.opaque_token = (
                query.get(
                    "story_fbid",
                    ""
                )
                or query.get(
                    "post_id",
                    ""
                )
            )
        return result
    # ==============================================================
    # photo.php
    # ==============================================================
    if path_lower.rstrip(
        "/"
    ).endswith(
        "/photo.php"
    ):
        result.url_type = "PHOTO"
        fbid = query.get(
            "fbid",
            ""
        )
        if is_probable_numeric_id(
            fbid
        ):
            result.photo_route_id = (
                normalize_numeric(fbid)
            )
        # photo.php?id= is commonly publisher
        # in this route, NOT group.
        owner_id = query.get(
            "id",
            ""
        )
        if is_probable_numeric_id(
            owner_id
        ):
            result.numeric_route_id = (
                normalize_numeric(
                    owner_id
                )
            )
        set_value = query.get(
            "set",
            ""
        )
        album_match = re.search(
            r"(?i)(?:^|[.]|a[.])"
            r"(\d{5,25})",
            set_value
        )
        if album_match:
            album = normalize_numeric(
                album_match.group(1)
            )
            if album:
                result.album_id = album
        if not result.photo_route_id:
            result.opaque_token = (
                fbid
                or query.get(
                    "photo_id",
                    ""
                )
                or ""
            )
        return result
    # ==============================================================
    # story.php
    # ==============================================================
    if path_lower.rstrip(
        "/"
    ).endswith(
        "/story.php"
    ):
        result.url_type = "STORY"
        story_id = query.get(
            "story_fbid",
            ""
        )
        if is_probable_numeric_id(
            story_id
        ):
            result.story_route_id = (
                normalize_numeric(
                    story_id
                )
            )
        else:
            result.opaque_token = (
                story_id
                or query.get(
                    "story_id",
                    ""
                )
                or ""
            )
        return result
    # ==============================================================
    # watch
    # ==============================================================
    if path_lower.rstrip(
        "/"
    ) == "/watch":
        result.url_type = "VIDEO"
        video_id = query.get(
            "v",
            ""
        )
        if is_probable_numeric_id(
            video_id
        ):
            result.video_route_id = (
                normalize_numeric(
                    video_id
                )
            )
        else:
            result.opaque_token = video_id
        return result
    # ==============================================================
    # video.php
    # ==============================================================
    if path_lower.rstrip(
        "/"
    ).endswith(
        "/video.php"
    ):
        result.url_type = "VIDEO"
        video_id = (
            query.get("v", "")
            or query.get("video_id", "")
        )
        if is_probable_numeric_id(
            video_id
        ):
            result.video_route_id = (
                normalize_numeric(
                    video_id
                )
            )
        else:
            result.opaque_token = video_id
        return result
    # ==============================================================
    # reel / reels
    # ==============================================================
    reel_match = re.search(
        r"/(?:reel|reels)/([^/?#]+)",
        path,
        re.I
    )
    if reel_match:
        token = reel_match.group(1)
        result.url_type = "REEL"
        result.reel_route_id = (
            normalize_numeric(token)
        )
        if not result.reel_route_id:
            result.opaque_token = token
        return result
    # ==============================================================
    # videos
    # ==============================================================
    video_match = re.search(
        r"/(?:video|videos)/([^/?#]+)",
        path,
        re.I
    )
    if video_match:
        token = video_match.group(1)
        result.url_type = "VIDEO"
        result.video_route_id = (
            normalize_numeric(token)
        )
        if not result.video_route_id:
            result.opaque_token = token
        # Username trước /videos/
        parts = [
            x for x in path.split("/")
            if x
        ]
        if len(parts) >= 3:
            possible_user = parts[0]
            if re.match(
                r"^[A-Za-z0-9._-]{2,100}$",
                possible_user
            ):
                result.username = (
                    possible_user
                )
        return result
    # ==============================================================
    # photos / photo
    # ==============================================================
    photo_match = re.search(
        r"/(?:photo|photos)/([^/?#]+)",
        path,
        re.I
    )
    if photo_match:
        token = photo_match.group(1)
        result.url_type = "PHOTO"
        result.photo_route_id = (
            normalize_numeric(token)
        )
        if not result.photo_route_id:
            result.opaque_token = token
        parts = [
            x for x in path.split("/")
            if x
        ]
        if len(parts) >= 3:
            possible_user = parts[0]
            if re.match(
                r"^[A-Za-z0-9._-]{2,100}$",
                possible_user
            ):
                result.username = (
                    possible_user
                )
        return result
    # ==============================================================
    # stories
    # ==============================================================
    story_match = re.search(
        r"/stories/([^/?#]+)/([^/?#]+)",
        path,
        re.I
    )
    if story_match:
        result.url_type = "STORY"
        possible_user = story_match.group(1)
        token = story_match.group(2)
        if re.match(
            r"^[A-Za-z0-9._-]{2,100}$",
            possible_user
        ):
            result.username = possible_user
        result.story_route_id = (
            normalize_numeric(token)
        )
        if not result.story_route_id:
            result.opaque_token = token
        return result
    # ==============================================================
    # /posts/
    # ==============================================================
    post_match = re.search(
        r"/posts/([^/?#]+)",
        path,
        re.I
    )
    if post_match:
        result.url_type = "POST"
        token = post_match.group(1)
        result.post_route_id = (
            normalize_numeric(token)
        )
        if not result.post_route_id:
            result.opaque_token = token
        parts = [
            x for x in path.split("/")
            if x
        ]
        if parts:
            possible_user = parts[0]
            if re.match(
                r"^[A-Za-z0-9._-]{2,100}$",
                possible_user
            ):
                result.username = (
                    possible_user
                )
        return result
    # ==============================================================
    # /p/
    # ==============================================================
    p_match = re.search(
        r"/p/([^/?#]+)",
        path,
        re.I
    )
    if p_match:
        result.url_type = "POST"
        token = p_match.group(1)
        result.post_route_id = (
            normalize_numeric(token)
        )
        if not result.post_route_id:
            result.opaque_token = token
        return result
    # ==============================================================
    # people
    # ==============================================================
    if re.search(
        r"/people/",
        path,
        re.I
    ):
        result.url_type = "PROFILE"
        result.route_entity = "USER"
        return result
    # ==============================================================
    # events
    # ==============================================================
    event_match = re.search(
        r"/events/([^/?#]+)",
        path,
        re.I
    )
    if event_match:
        result.url_type = "EVENT"
        token = event_match.group(1)
        if token.isdigit():
            result.numeric_route_id = (
                normalize_numeric(token)
            )
        else:
            result.opaque_token = token
        return result
    # ==============================================================
    # marketplace
    # ==============================================================
    if "/marketplace/" in path_lower:
        result.url_type = "MARKETPLACE"
        return result
    # ==============================================================
    # vanity profile
    # ==============================================================
    parts = [
        x for x in path.split("/")
        if x
    ]
    if parts:
        first = parts[0]
        reserved = {
            "home",
            "watch",
            "groups",
            "pages",
            "marketplace",
            "gaming",
            "events",
            "reels",
            "reel",
            "videos",
            "video",
            "photos",
            "photo",
            "stories",
            "story",
            "settings",
            "login",
            "recover",
            "help",
            "privacy",
            "policies",
            "terms",
            "share",
            "p",
        }
        if (
            first.lower()
            not in reserved
            and re.match(
                r"^[A-Za-z0-9._-]{2,100}$",
                first
            )
        ):
            result.username = first
            result.url_type = "PROFILE"
            result.route_entity = "UNKNOWN"
            return result
    return result
# ======================================================================
# HTML SNAPSHOT PARSER
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
            jsonld.append(
                data
            )
        except Exception:
            # Thử xử lý JSON-LD có HTML entity.
            try:
                data = json.loads(
                    html.unescape(raw)
                )
                jsonld.append(
                    data
                )
            except Exception:
                pass
    canonical = (
        parser.meta.get(
            "og:url",
            ""
        )
        or ""
    )
    return Snapshot(
        requested_url=requested_url,
        final_url=final_url,
        canonical_url=normalize_url(
            canonical
        ),
        status_code=status_code,
        title=title,
        html_text=html_text,
        meta=parser.meta,
        links=parser.links,
        jsonld=jsonld,
        redirects=redirects or [],
    )
# ======================================================================
# JSON-LD
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
def jsonld_objects(snapshot):
    output = []
    for root in snapshot.jsonld:
        for obj in walk_jsonld(root):
            output.append(obj)
    return output
def jsonld_author_objects(snapshot):
    output = []
    for obj in jsonld_objects(snapshot):
        for key in (
            "author",
            "creator",
            "publisher",
        ):
            value = obj.get(key)
            if isinstance(
                value,
                dict
            ):
                output.append(value)
            elif isinstance(
                value,
                list
            ):
                output.extend(
                    x
                    for x in value
                    if isinstance(
                        x,
                        dict
                    )
                )
    return output
# ======================================================================
# JSON-LD ENTITY KIND
# ======================================================================
def infer_jsonld_kind(snapshot):
    kinds = []
    for obj in jsonld_objects(snapshot):
        value = (
            obj.get("@type")
            or obj.get("type")
            or ""
        )
        if isinstance(
            value,
            list
        ):
            kinds.extend(
                str(x).lower()
                for x in value
            )
        else:
            kinds.append(
                str(value).lower()
            )
    joined = " ".join(kinds)
    if "person" in joined:
        return "USER"
    if (
        "organization" in joined
        or "corporation" in joined
        or "localbusiness" in joined
    ):
        return "PAGE"
    return "UNKNOWN"
# ======================================================================
# HTTP FETCHER
# ======================================================================
class HTTPFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.max_redirects = (
            MAX_REDIRECTS
        )
        self.lock = asyncio.Lock()
    def fetch(
        self,
        url,
        timeout=REQUEST_TIMEOUT,
    ):
        errors = []
        for index, ua in enumerate(
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
                if status in {
                    403,
                    429,
                    500,
                    502,
                    503,
                    504,
                }:
                    response.close()
                    if index + 1 < len(
                        USER_AGENT_LIST
                    ):
                        time.sleep(
                            0.3
                            + (
                                index
                                * 0.25
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
                final_url = str(
                    response.url
                )
                history = [
                    str(r.url)
                    for r in response.history
                ]
                encoding = (
                    response.encoding
                    or "utf-8"
                )
                text = content.decode(
                    encoding,
                    errors="replace"
                )
                headers_out = dict(
                    response.headers
                )
                response.close()
                return {
                    "status": status,
                    "url": final_url,
                    "text": text,
                    "history": history,
                    "headers": headers_out,
                    "error": "",
                }
            except Exception as exc:
                errors.append(
                    str(exc)
                )
                if index + 1 < len(
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
# REDIRECT UNWRAP
# ======================================================================
def unwrap_redirect_url(url):
    if not url:
        return url
    current = url
    for _ in range(5):
        parsed = safe_parse_url(
            current
        )
        if not parsed:
            break
        qs = parse_qs(
            parsed.query,
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
            "next",
        ):
            values = qs.get(
                key
            )
            if not values:
                continue
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
        locality=1.0,
        direct=False,
        verified=False,
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
                )[:MAX_HTML_CONTEXT],
                object_token=object_token,
                key=key,
                independent_key=(
                    independent_key
                    or source
                ),
                locality=float(
                    locality
                ),
                direct=bool(
                    direct
                ),
                verified=bool(
                    verified
                ),
            )
        )
    def role(
        self,
        role
    ):
        return [
            x
            for x in self.items
            if x.role == role
        ]
    def count(self):
        return len(
            self.items
        )
# ======================================================================
# SEMANTIC ALIASES
# ======================================================================
SEMANTIC_ALIASES = {
    "userid": "USER",
    "profileid": "USER",
    "ownerid": "OWNER",
    "publisherid": "PUBLISHER",
    "pageid": "PAGE",
    "groupid": "GROUP",
    "actorid": "ACTOR",
    "entityid": "ENTITY",
    "postid": "POST",
    "storyfbid": "STORY",
    "mediafbid": "MEDIA",
    "videoid": "VIDEO",
    "photoid": "PHOTO",
}
# ======================================================================
# SEMANTIC ID EXTRACTION
# ======================================================================
def extract_semantic_ids(
    text,
    store,
    object_token="",
    source="html",
):
    if not text:
        return
    for match in SEMANTIC_ID_RE.finditer(
        text
    ):
        key_raw = (
            match.group(0)
            .split(
                match.group(1)
                if match.lastindex
                else ":"
            )[0]
        )
        # Lấy key từ phần match bằng regex phụ.
        prefix = text[
            max(
                0,
                match.start()
            ):
            match.end()
        ]
        key_match = re.match(
            r"(?i)"
            r"([A-Za-z][A-Za-z0-9_-]*)"
            r"\s*[:=]",
            prefix
        )
        if not key_match:
            continue
        raw_key = (
            key_match.group(1)
            .lower()
            .replace("_", "")
            .replace("-", "")
        )
        role = SEMANTIC_ALIASES.get(
            raw_key
        )
        if not role:
            continue
        value = normalize_numeric(
            match.group(1)
        )
        if not is_probable_numeric_id(
            value
        ):
            continue
        weights = {
            "USER": 24,
            "OWNER": 21,
            "PUBLISHER": 22,
            "PAGE": 22,
            "GROUP": 22,
            "ACTOR": 9,
            "ENTITY": 5,
            "POST": 18,
            "STORY": 18,
            "VIDEO": 18,
            "PHOTO": 18,
            "MEDIA": 12,
        }
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
        # Semantic key trực tiếp mạnh hơn generic context.
        direct = role in {
            "USER",
            "OWNER",
            "PUBLISHER",
            "PAGE",
            "GROUP",
            "POST",
            "STORY",
            "VIDEO",
            "PHOTO",
        }
        store.add(
            value=value,
            role=role,
            source=source,
            weight=weights.get(
                role,
                5
            ),
            context=context,
            object_token=object_token,
            key=raw_key,
            independent_key=(
                source
                + ":"
                + raw_key
            ),
            locality=1.0,
            direct=direct,
        )
# ======================================================================
# SPECIALIZED HTML CONTEXT
#
# Không quét toàn bộ numeric HTML.
# Chỉ tìm các pattern có quan hệ cấu trúc rõ.
# ======================================================================
def extract_special_html_ids(
    text,
    store,
    object_token="",
):
    if not text:
        return
    # --------------------------------------------------------------
    # profile.php?id=
    # --------------------------------------------------------------
    for match in re.finditer(
        r"(?i)"
        r"profile\.php"
        r"[^\"'<>\s]{0,180}?"
        r"(?:[?&])id="
        r"(\d{5,25})",
        text
    ):
        value = normalize_numeric(
            match.group(1)
        )
        if not is_probable_numeric_id(
            value
        ):
            continue
        context = text[
            max(
                0,
                match.start() - 180
            ):
            min(
                len(text),
                match.end() + 180
            )
        ]
        store.add(
            value,
            "USER",
            "profile_php_html",
            26,
            context=context,
            object_token=object_token,
            key="profile.php?id",
            independent_key="profile_php_html",
            locality=1.0,
            direct=True,
        )
    # --------------------------------------------------------------
    # /profile.php?id=
    # --------------------------------------------------------------
    # --------------------------------------------------------------
    # canonical / OG profile query
    # --------------------------------------------------------------
    # Không xử lý generic numeric ở đây.
    # Canonical sẽ được xử lý riêng.
# ======================================================================
# URL EVIDENCE
# ======================================================================
def extract_url_evidence(
    parsed_url,
    store,
):
    # --------------------------------------------------------------
    # Direct profile
    # --------------------------------------------------------------
    if (
        parsed_url.url_type
        == "PROFILE"
        and parsed_url.numeric_route_id
    ):
        store.add(
            parsed_url.numeric_route_id,
            "USER",
            "direct_profile_url",
            100,
            context=parsed_url.normalized,
            independent_key="direct_profile_url",
            locality=1.0,
            direct=True,
            verified=True,
        )
    # --------------------------------------------------------------
    # Group route
    # --------------------------------------------------------------
    if parsed_url.group_route_id:
        store.add(
            parsed_url.group_route_id,
            "GROUP",
            "group_route",
            100,
            context=parsed_url.path,
            independent_key="group_route",
            locality=1.0,
            direct=True,
            verified=True,
        )
    # --------------------------------------------------------------
    # Page route
    # --------------------------------------------------------------
    if parsed_url.page_route_id:
        store.add(
            parsed_url.page_route_id,
            "PAGE",
            "page_route",
            100,
            context=parsed_url.path,
            independent_key="page_route",
            locality=1.0,
            direct=True,
            verified=True,
        )
    # --------------------------------------------------------------
    # Object route IDs
    # --------------------------------------------------------------
    if parsed_url.post_route_id:
        store.add(
            parsed_url.post_route_id,
            "POST",
            "post_route",
            100,
            context=parsed_url.path,
            object_token=(
                parsed_url.opaque_token
            ),
            independent_key="post_route",
            locality=1.0,
            direct=True,
        )
    if parsed_url.video_route_id:
        store.add(
            parsed_url.video_route_id,
            "VIDEO",
            "video_route",
            100,
            context=parsed_url.path,
            object_token=(
                parsed_url.opaque_token
            ),
            independent_key="video_route",
            locality=1.0,
            direct=True,
        )
    if parsed_url.photo_route_id:
        store.add(
            parsed_url.photo_route_id,
            "PHOTO",
            "photo_route",
            100,
            context=parsed_url.path,
            object_token=(
                parsed_url.opaque_token
            ),
            independent_key="photo_route",
            locality=1.0,
            direct=True,
        )
    if parsed_url.story_route_id:
        store.add(
            parsed_url.story_route_id,
            "STORY",
            "story_route",
            100,
            context=parsed_url.path,
            object_token=(
                parsed_url.opaque_token
            ),
            independent_key="story_route",
            locality=1.0,
            direct=True,
        )
    if parsed_url.reel_route_id:
        store.add(
            parsed_url.reel_route_id,
            "REEL",
            "reel_route",
            100,
            context=parsed_url.path,
            object_token=(
                parsed_url.opaque_token
            ),
            independent_key="reel_route",
            locality=1.0,
            direct=True,
        )
    # --------------------------------------------------------------
    # photo.php?id= OWNER candidate
    #
    # Chỉ tạo OWNER evidence.
    # Không trực tiếp tạo GROUP/PAGE.
    # --------------------------------------------------------------
    if (
        parsed_url.url_type
        == "PHOTO"
        and parsed_url.numeric_route_id
    ):
        store.add(
            parsed_url.numeric_route_id,
            "OWNER",
            "photo_php_owner",
            34,
            context=parsed_url.normalized,
            independent_key="photo_php_owner",
            locality=1.0,
            direct=True,
        )
    # --------------------------------------------------------------
    # Opaque
    # --------------------------------------------------------------
    if parsed_url.opaque_token:
        store.add(
            parsed_url.opaque_token,
            "OPAQUE",
            "url_opaque",
            1,
            context=parsed_url.normalized,
            independent_key="url_opaque",
        )
# ======================================================================
# CANONICAL / OG ROLE EXTRACTION
# ======================================================================
def extract_url_role_evidence(
    value,
    store,
    source,
    object_token="",
):
    if not value:
        return
    value = normalize_url(
        value
    )
    parsed = safe_parse_url(
        value
    )
    if not parsed:
        return
    classified = classify_url(
        value
    )
    # --------------------------------------------------------------
    # Profile URL
    # --------------------------------------------------------------
    if (
        classified.url_type
        == "PROFILE"
        and classified.numeric_route_id
    ):
        store.add(
            classified.numeric_route_id,
            "USER",
            source + ":profile",
            38,
            context=value,
            object_token=object_token,
            independent_key=(
                source
                + ":profile"
            ),
            locality=1.0,
            direct=True,
        )
    # --------------------------------------------------------------
    # Group
    # --------------------------------------------------------------
    if classified.group_route_id:
        store.add(
            classified.group_route_id,
            "GROUP",
            source + ":group",
            40,
            context=value,
            object_token=object_token,
            independent_key=(
                source
                + ":group"
            ),
            locality=1.0,
            direct=True,
        )
    # --------------------------------------------------------------
    # Page
    # --------------------------------------------------------------
    if classified.page_route_id:
        store.add(
            classified.page_route_id,
            "PAGE",
            source + ":page",
            40,
            context=value,
            object_token=object_token,
            independent_key=(
                source
                + ":page"
            ),
            locality=1.0,
            direct=True,
        )
# ======================================================================
# JSON-LD EVIDENCE
# ======================================================================
def collect_jsonld_evidence(
    snapshot,
    store,
    object_token="",
):
    for obj in jsonld_objects(
        snapshot
    ):
        obj_type = str(
            obj.get(
                "@type",
                ""
            )
        ).lower()
        # ----------------------------------------------------------
        # Main entity
        # ----------------------------------------------------------
        for key in (
            "mainEntityOfPage",
            "url",
            "@id",
        ):
            value = obj.get(
                key,
                ""
            )
            if isinstance(
                value,
                dict
            ):
                value = (
                    value.get("url")
                    or value.get("@id")
                    or ""
                )
            if isinstance(
                value,
                str
            ):
                extract_url_role_evidence(
                    value,
                    store,
                    "jsonld:" + key,
                    object_token
                )
        # ----------------------------------------------------------
        # Author / creator / publisher
        # ----------------------------------------------------------
        for key in (
            "author",
            "creator",
            "publisher",
        ):
            value = obj.get(
                key
            )
            if isinstance(
                value,
                dict
            ):
                values = [value]
            elif isinstance(
                value,
                list
            ):
                values = [
                    x
                    for x in value
                    if isinstance(
                        x,
                        dict
                    )
                ]
            else:
                values = []
            for person in values:
                name = clean_text(
                    person.get(
                        "name",
                        ""
                    )
                )
                url = person.get(
                    "url",
                    ""
                )
                obj_id = person.get(
                    "@id",
                    ""
                )
                for candidate_url in (
                    url,
                    obj_id,
                ):
                    if not candidate_url:
                        continue
                    candidate_url = str(
                        candidate_url
                    )
                    uid = (
                        extract_numeric_id_from_profile_url(
                            candidate_url
                        )
                    )
                    if uid:
                        store.add(
                            uid,
                            "PUBLISHER",
                            "jsonld_" + key,
                            42,
                            context=(
                                name
                                + " "
                                + candidate_url
                            ),
                            object_token=object_token,
                            independent_key=(
                                "jsonld_"
                                + key
                                + "_url"
                            ),
                            locality=1.0,
                            direct=True,
                        )
                    # Profile route itself.
                    extract_url_role_evidence(
                        candidate_url,
                        store,
                        "jsonld_" + key,
                        object_token
                    )
                # --------------------------------------------------
                # JSON-LD Person strongly supports USER.
                # --------------------------------------------------
                p_type = str(
                    person.get(
                        "@type",
                        ""
                    )
                ).lower()
                if p_type == "person":
                    uid = (
                        extract_numeric_id_from_profile_url(
                            url
                        )
                    )
                    if uid:
                        store.add(
                            uid,
                            "USER",
                            "jsonld_person",
                            48,
                            context=(
                                name
                                + " "
                                + str(url)
                            ),
                            object_token=object_token,
                            independent_key="jsonld_person",
                            locality=1.0,
                            direct=True,
                        )
# ======================================================================
# NUMERIC ID FROM URL
# ======================================================================
def extract_numeric_id_from_profile_url(
    url
):
    if not url:
        return ""
    parsed = safe_parse_url(
        url
    )
    if not parsed:
        return ""
    qs = parse_qs(
        parsed.query,
        keep_blank_values=True
    )
    path = (
        parsed.path
        .lower()
        .rstrip("/")
    )
    if path.endswith(
        "/profile.php"
    ):
        value = qs.get(
            "id",
            [""]
        )[-1]
        if is_probable_numeric_id(
            value
        ):
            return normalize_numeric(
                value
            )
    # profile URL có ?id=
    value = qs.get(
        "id",
        [""]
    )[-1]
    if (
        value
        and is_probable_numeric_id(value)
        and len(path.split("/")) <= 2
    ):
        return normalize_numeric(
            value
        )
    return ""
# ======================================================================
# PROFILE URL DISCOVERY
# ======================================================================
def is_probable_profile_url(
    url
):
    parsed = safe_parse_url(
        url
    )
    if not parsed:
        return False
    if not host_is_facebook(
        parsed.netloc
    ):
        return False
    path = (
        parsed.path
        .strip("/")
    )
    if not path:
        return False
    lower = path.lower()
    # Explicit profile.
    if lower == "profile.php":
        return True
    # Exclude known object routes.
    bad_prefixes = (
        "groups/",
        "pages/",
        "posts/",
        "share/",
        "reel/",
        "reels/",
        "video/",
        "videos/",
        "photo/",
        "photos/",
        "story/",
        "stories/",
        "watch",
        "marketplace/",
        "events/",
        "gaming/",
        "p/",
    )
    if lower.startswith(
        bad_prefixes
    ):
        return False
    parts = [
        x
        for x in path.split("/")
        if x
    ]
    if len(parts) != 1:
        return False
    return bool(
        re.match(
            r"^[A-Za-z0-9._-]{2,100}$",
            parts[0]
        )
    )
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
        "//"
    ):
        value = "https:" + value
    elif value.startswith(
        "/"
    ):
        value = urljoin(
            base_url,
            value
        )
    elif not re.match(
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
    return normalize_url(
        value
    )
def discover_profile_urls(
    parsed_url,
    snapshot,
):
    candidates = []
    # --------------------------------------------------------------
    # Username from route.
    # --------------------------------------------------------------
    if parsed_url.username:
        candidates.extend([
            "https://www.facebook.com/"
            + parsed_url.username,
            "https://m.facebook.com/"
            + parsed_url.username,
        ])
    # --------------------------------------------------------------
    # Canonical / OG
    # --------------------------------------------------------------
    for value in (
        snapshot.canonical_url,
        snapshot.meta.get(
            "og:url",
            ""
        ),
        snapshot.meta.get(
            "profile:url",
            ""
        ),
        snapshot.meta.get(
            "article:author",
            ""
        ),
        snapshot.meta.get(
            "author",
            ""
        ),
    ):
        ref = normalize_profile_reference(
            value,
            snapshot.final_url
        )
        if (
            ref
            and is_probable_profile_url(ref)
        ):
            candidates.append(
                ref
            )
    # --------------------------------------------------------------
    # JSON-LD author
    # --------------------------------------------------------------
    for author in jsonld_author_objects(
        snapshot
    ):
        for key in (
            "url",
            "@id",
        ):
            value = author.get(
                key,
                ""
            )
            ref = normalize_profile_reference(
                value,
                snapshot.final_url
            )
            if (
                ref
                and is_probable_profile_url(ref)
            ):
                candidates.append(
                    ref
                )
    # --------------------------------------------------------------
    # HTML links
    # --------------------------------------------------------------
    for href in snapshot.links:
        ref = normalize_profile_reference(
            href,
            snapshot.final_url
        )
        if not ref:
            continue
        if not is_probable_profile_url(
            ref
        ):
            continue
        candidates.append(
            ref
        )
        if len(candidates) >= (
            MAX_DISCOVERED_PROFILE_LINKS
        ):
            break
    # --------------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------------
    result = []
    seen = set()
    for value in candidates:
        key = value.rstrip(
            "/"
        ).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(
            value
        )
    return result[
        :MAX_PROFILE_CHECKS
    ]
# ======================================================================
# PROFILE USERNAME
# ======================================================================
def extract_profile_username(
    url
):
    parsed = safe_parse_url(
        url
    )
    if not parsed:
        return ""
    path = (
        parsed.path
        .strip("/")
    )
    parts = [
        x
        for x in path.split("/")
        if x
    ]
    if len(parts) != 1:
        return ""
    if parts[0].lower() == "profile.php":
        return ""
    if re.match(
        r"^[A-Za-z0-9._-]{2,100}$",
        parts[0]
    ):
        return parts[0]
    return ""
# ======================================================================
# PROFILE VERIFICATION
# ======================================================================
def verify_profile(
    fetcher,
    profile_url,
):
    result = ProfileVerification(
        requested_url=profile_url
    )
    profile_url = normalize_url(
        profile_url
    )
    if not profile_url:
        return result
    fetched = fetcher.fetch(
        profile_url,
        timeout=PROFILE_VERIFY_TIMEOUT
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
    result.final_url = (
        snapshot.final_url
    )
    result.canonical = (
        snapshot.meta.get(
            "og:url",
            ""
        )
        or snapshot.canonical_url
        or snapshot.final_url
    )
    result.title = (
        snapshot.meta.get(
            "og:title",
            ""
        )
        or snapshot.title
    )
    result.username = (
        extract_profile_username(
            snapshot.final_url
        )
        or extract_profile_username(
            result.canonical
        )
        or extract_profile_username(
            profile_url
        )
    )
    result.entity_kind = (
        infer_jsonld_kind(
            snapshot
        )
    )
    # --------------------------------------------------------------
    # Candidate IDs.
    # --------------------------------------------------------------
    candidates = {}
    def add_candidate(
        uid,
        source,
        score
    ):
        uid = normalize_numeric(
            uid
        )
        if not is_probable_numeric_id(
            uid
        ):
            return
        if uid not in candidates:
            candidates[uid] = {
                "score": 0,
                "sources": [],
            }
        candidates[uid]["score"] += score
        candidates[uid]["sources"].append(
            source
        )
    # Direct profile query.
    direct_uid = (
        extract_numeric_id_from_profile_url(
            snapshot.final_url
        )
    )
    if direct_uid:
        add_candidate(
            direct_uid,
            "final_profile_query",
            100
        )
    canonical_uid = (
        extract_numeric_id_from_profile_url(
            result.canonical
        )
    )
    if canonical_uid:
        add_candidate(
            canonical_uid,
            "canonical_profile_query",
            100
        )
    # Semantic IDs from profile HTML.
    temp = EvidenceStore()
    extract_semantic_ids(
        snapshot.html_text,
        temp,
        source="profile_html"
    )
    for evidence in temp.items:
        if evidence.role in {
            "USER",
            "OWNER",
            "PUBLISHER",
        }:
            add_candidate(
                evidence.value,
                evidence.source,
                evidence.weight
            )
    # Explicit profile.php HTML references.
    extract_special_html_ids(
        snapshot.html_text,
        temp,
        object_token=""
    )
    for evidence in temp.items:
        if evidence.role == "USER":
            add_candidate(
                evidence.value,
                evidence.source,
                evidence.weight
            )
    if not candidates:
        return result
    ranked = sorted(
        candidates.items(),
        key=lambda x: x[1]["score"],
        reverse=True
    )
    best_uid, best_data = ranked[0]
    # --------------------------------------------------------------
    # Username consistency.
    # --------------------------------------------------------------
    requested_username = (
        extract_profile_username(
            profile_url
        )
    )
    username_match = bool(
        requested_username
        and result.username
        and requested_username.lower()
        == result.username.lower()
    )
    if username_match:
        best_data["score"] += 30
        best_data["sources"].append(
            "username_match"
        )
    # --------------------------------------------------------------
    # Canonical consistency.
    # --------------------------------------------------------------
    canonical_username = (
        extract_profile_username(
            result.canonical
        )
    )
    if (
        requested_username
        and canonical_username
        and requested_username.lower()
        == canonical_username.lower()
    ):
        best_data["score"] += 25
        best_data["sources"].append(
            "canonical_username_match"
        )
    # --------------------------------------------------------------
    # JSON-LD Person.
    # --------------------------------------------------------------
    if result.entity_kind == "USER":
        best_data["score"] += 25
        best_data["sources"].append(
            "jsonld_person"
        )
    # --------------------------------------------------------------
    # Determine second candidate.
    # --------------------------------------------------------------
    if len(ranked) >= 2:
        second_uid, second_data = ranked[1]
        if (
            best_uid != second_uid
            and
            abs(
                best_data["score"]
                - second_data["score"]
            ) < UID_CONFLICT_MARGIN
        ):
            # Không ép.
            result.evidence.append(
                (
                    "CONFLICT",
                    best_uid,
                    second_uid,
                )
            )
            return result
    result.uid = best_uid
    result.evidence = [
        (
            best_uid,
            source,
            score
        )
        for source, score in [
            (
                src,
                best_data["score"]
            )
            for src in best_data["sources"]
        ]
    ]
    # --------------------------------------------------------------
    # Strong verification.
    # --------------------------------------------------------------
    score = best_data["score"]
    if (
        username_match
        and score >= 100
    ):
        result.strong = True
    elif (
        result.entity_kind == "USER"
        and score >= 100
    ):
        result.strong = True
    elif score >= 130:
        result.strong = True
    return result
# ======================================================================
# COLLECT OBJECT EVIDENCE
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
    # Semantic keys ONLY.
    # --------------------------------------------------------------
    extract_semantic_ids(
        text,
        store,
        object_token=token,
        source="html_semantic"
    )
    # --------------------------------------------------------------
    # Special structured HTML.
    # --------------------------------------------------------------
    extract_special_html_ids(
        text,
        store,
        object_token=token
    )
    # --------------------------------------------------------------
    # OG / meta.
    # --------------------------------------------------------------
    for key, value in snapshot.meta.items():
        if not value:
            continue
        value = str(value)
        # URL metadata gets URL-aware treatment.
        if key in {
            "og:url",
            "al:ios:url",
            "al:android:url",
            "profile:url",
            "article:author",
        }:
            extract_url_role_evidence(
                value,
                store,
                "meta:" + key,
                token
            )
        # Semantic IDs if explicitly present.
        extract_semantic_ids(
            value,
            store,
            object_token=token,
            source="meta:" + key
        )
    # --------------------------------------------------------------
    # Canonical.
    # --------------------------------------------------------------
    if snapshot.canonical_url:
        extract_url_role_evidence(
            snapshot.canonical_url,
            store,
            "canonical",
            token
        )
    # --------------------------------------------------------------
    # JSON-LD.
    # --------------------------------------------------------------
    collect_jsonld_evidence(
        snapshot,
        store,
        object_token=token
    )
    # --------------------------------------------------------------
    # Links.
    #
    # Chỉ lấy profile.php?id= hoặc explicit
    # profile URL.
    # --------------------------------------------------------------
    for href in snapshot.links:
        if not href:
            continue
        ref = normalize_profile_reference(
            href,
            snapshot.final_url
        )
        if not ref:
            continue
        uid = (
            extract_numeric_id_from_profile_url(
                ref
            )
        )
        if uid:
            store.add(
                uid,
                "PUBLISHER",
                "profile_link",
                40,
                context=ref,
                object_token=token,
                independent_key="profile_link",
                locality=1.0,
                direct=True,
            )
# ======================================================================
# DIRECT OBJECT IDS
# ======================================================================
def choose_direct_object_id(
    parsed_url,
    store,
    role,
):
    # Route ID luôn ưu tiên tuyệt đối.
    if role == "POST":
        if parsed_url.post_route_id:
            return parsed_url.post_route_id
    if role == "VIDEO":
        if parsed_url.video_route_id:
            return parsed_url.video_route_id
    if role == "PHOTO":
        if parsed_url.photo_route_id:
            return parsed_url.photo_route_id
    if role == "STORY":
        if parsed_url.story_route_id:
            return parsed_url.story_route_id
    # --------------------------------------------------------------
    # Fallback semantic role.
    # --------------------------------------------------------------
    scores = {}
    for evidence in store.items:
        if evidence.role != role:
            continue
        value = normalize_numeric(
            evidence.value
        )
        if not is_probable_numeric_id(
            value
        ):
            continue
        # Ưu tiên direct.
        score = evidence.weight
        if evidence.direct:
            score += 25
        score *= evidence.locality
        scores[value] = (
            scores.get(
                value,
                0
            )
            + score
        )
    if not scores:
        return ""
    return max(
        scores,
        key=scores.get
    )
# ======================================================================
# PAGE / GROUP IDs
#
# QUAN TRỌNG:
# Chỉ sử dụng PAGE/GROUP evidence có nguồn explicit.
# Không dùng generic numeric.
# ======================================================================
def collect_page_group_ids(
    parsed_url,
    store,
):
    pages = {}
    groups = {}
    # Route trước.
    if parsed_url.page_route_id:
        pages[
            parsed_url.page_route_id
        ] = 1000
    if parsed_url.group_route_id:
        groups[
            parsed_url.group_route_id
        ] = 1000
    for evidence in store.items:
        if evidence.role == "PAGE":
            value = normalize_numeric(
                evidence.value
            )
            if not value:
                continue
            score = evidence.weight
            if evidence.direct:
                score += 30
            # Meta/canonical page route được phép.
            if (
                evidence.source.startswith(
                    "canonical"
                )
                or evidence.source.startswith(
                    "meta:"
                )
                or evidence.source == "page_route"
            ):
                pages[value] = (
                    pages.get(
                        value,
                        0
                    )
                    + score
                )
        elif evidence.role == "GROUP":
            value = normalize_numeric(
                evidence.value
            )
            if not value:
                continue
            score = evidence.weight
            if evidence.direct:
                score += 30
            if (
                evidence.source.startswith(
                    "canonical"
                )
                or evidence.source.startswith(
                    "meta:"
                )
                or evidence.source == "group_route"
            ):
                groups[value] = (
                    groups.get(
                        value,
                        0
                    )
                    + score
                )
    return pages, groups
# ======================================================================
# USER CANDIDATE SCORING
# ======================================================================
def score_user_candidates(
    store,
    parsed_url,
    profile_verifications,
):
    candidates = {}
    def get_candidate(
        value
    ):
        value = normalize_numeric(
            value
        )
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
    # Role evidence.
    # --------------------------------------------------------------
    for evidence in store.items:
        if evidence.role not in {
            "USER",
            "OWNER",
            "PUBLISHER",
            "ACTOR",
        }:
            continue
        candidate = get_candidate(
            evidence.value
        )
        if not candidate:
            continue
        # Actor yếu.
        weight = evidence.weight
        if evidence.role == "ACTOR":
            weight *= 0.45
        # OWNER chỉ mạnh khi route context là photo/story.
        if evidence.role == "OWNER":
            if parsed_url.url_type not in {
                "PHOTO",
                "STORY",
            }:
                weight *= 0.55
        # Publisher evidence mạnh hơn actor.
        if evidence.role == "PUBLISHER":
            weight *= 1.10
        # Direct evidence.
        if evidence.direct:
            candidate.direct_count += 1
        # Strong source.
        if weight >= 20:
            candidate.strong_count += 1
        candidate.score += (
            weight
            * evidence.locality
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
    # Profile verification.
    # --------------------------------------------------------------
    for verification in profile_verifications:
        uid = normalize_numeric(
            verification.uid
        )
        if not uid:
            continue
        candidate = get_candidate(
            uid
        )
        if not candidate:
            continue
        candidate.score += 60
        candidate.sources.append(
            "profile_verified"
        )
        candidate.independent_sources.add(
            "profile_verified"
        )
        if verification.strong:
            candidate.score += 45
            candidate.verified = True
    # --------------------------------------------------------------
    # Direct profile URL.
    # --------------------------------------------------------------
    if (
        parsed_url.url_type == "PROFILE"
        and parsed_url.numeric_route_id
    ):
        candidate = get_candidate(
            parsed_url.numeric_route_id
        )
        if candidate:
            candidate.score += 100
            candidate.sources.append(
                "direct_profile_route"
            )
            candidate.independent_sources.add(
                "direct_profile_route"
            )
            candidate.verified = True
    # --------------------------------------------------------------
    # Independent source bonus.
    # --------------------------------------------------------------
    for candidate in candidates.values():
        independent = len(
            candidate.independent_sources
        )
        if independent >= 2:
            candidate.score += 12
        if independent >= 3:
            candidate.score += 12
        if independent >= 4:
            candidate.score += 12
        if candidate.direct_count >= 2:
            candidate.score += 10
        if candidate.strong_count >= 2:
            candidate.score += 10
    return candidates
# ======================================================================
# PROFILE CORRELATION
# ======================================================================
def correlate_profile_to_candidates(
    profile_verifications,
    parsed_url,
    store,
):
    for verification in profile_verifications:
        uid = normalize_numeric(
            verification.uid
        )
        if not uid:
            continue
        # Profile username phải khớp route username.
        if (
            parsed_url.username
            and verification.username
            and
            parsed_url.username.lower()
            == verification.username.lower()
        ):
            store.add(
                uid,
                "USER",
                "username_profile_correlation",
                75,
                context=(
                    parsed_url.username
                    + " -> "
                    + verification.username
                ),
                object_token=(
                    parsed_url.opaque_token
                ),
                independent_key=(
                    "username_profile_correlation"
                ),
                locality=1.0,
                direct=True,
                verified=True,
            )
        # Photo owner.
        if (
            parsed_url.url_type == "PHOTO"
            and parsed_url.numeric_route_id
            and same_numeric(
                parsed_url.numeric_route_id,
                uid
            )
        ):
            store.add(
                uid,
                "PUBLISHER",
                "photo_owner_profile_verified",
                85,
                context=(
                    parsed_url.normalized
                ),
                object_token=(
                    parsed_url.opaque_token
                ),
                independent_key=(
                    "photo_owner_profile_verified"
                ),
                locality=1.0,
                direct=True,
                verified=True,
            )
# ======================================================================
# ENTITY INFERENCE
# ======================================================================
def infer_entity(
    parsed_url,
    store,
    page_ids,
    group_ids,
    user_candidates,
    profile_verifications,
):
    # ==============================================================
    # GROUP ROUTE = ABSOLUTE
    # ==============================================================
    if parsed_url.route_entity == "GROUP":
        if parsed_url.url_type == "GROUP_POST":
            return "GROUP_POST"
        if parsed_url.url_type == "GROUP_MEDIA":
            return "GROUP_MEDIA"
        return "GROUP"
    # ==============================================================
    # PAGE ROUTE = ABSOLUTE
    # ==============================================================
    if parsed_url.route_entity == "PAGE":
        if parsed_url.url_type in {
            "POST",
            "VIDEO",
            "REEL",
            "PHOTO",
            "STORY",
        }:
            return "PAGE_POST"
        return "PAGE"
    # ==============================================================
    # Explicit group evidence.
    #
    # Chỉ chấp nhận khi URL/canonical/meta rõ.
    # ==============================================================
    if group_ids:
        if parsed_url.url_type in {
            "POST",
            "VIDEO",
            "REEL",
            "PHOTO",
            "STORY",
        }:
            return "GROUP_POST"
    # ==============================================================
    # Explicit page evidence.
    # ==============================================================
    if page_ids:
        if parsed_url.url_type in {
            "POST",
            "VIDEO",
            "REEL",
            "PHOTO",
            "STORY",
        }:
            return "PAGE_POST"
    # ==============================================================
    # Username route.
    # ==============================================================
    if parsed_url.username:
        if parsed_url.url_type in {
            "POST",
            "VIDEO",
            "REEL",
            "PHOTO",
            "STORY",
        }:
            # Nếu profile verification xác nhận PAGE.
            page_verified = any(
                x.entity_kind == "PAGE"
                for x in profile_verifications
            )
            if page_verified:
                return "PAGE_POST"
            return "USER_POST"
        # Vanity profile.
        if parsed_url.url_type == "PROFILE":
            page_verified = any(
                x.entity_kind == "PAGE"
                for x in profile_verifications
            )
            if page_verified:
                return "PAGE"
            return "USER"
    # ==============================================================
    # Verified user.
    # ==============================================================
    verified_users = [
        c
        for c in user_candidates.values()
        if c.verified
        and not c.rejected
    ]
    if verified_users:
        if parsed_url.url_type in {
            "POST",
            "VIDEO",
            "REEL",
            "PHOTO",
            "STORY",
        }:
            return "USER_POST"
        return "USER"
    # ==============================================================
    # Preserve object type.
    # ==============================================================
    return parsed_url.url_type
# ======================================================================
# SELECT USER UID
# ======================================================================
def select_verified_user(
    candidates,
    entity_type,
):
    usable = [
        c
        for c in candidates.values()
        if not c.rejected
        and c.score >= MIN_UID_SCORE
    ]
    if not usable:
        return None, "NO_CANDIDATE"
    usable.sort(
        key=lambda c: (
            c.verified,
            c.score,
            c.direct_count,
            c.strong_count,
            len(
                c.independent_sources
            ),
        ),
        reverse=True
    )
    best = usable[0]
    # ==============================================================
    # PAGE
    # ==============================================================
    if entity_type.startswith(
        "PAGE"
    ):
        return None, "PAGE_NO_USER"
    # ==============================================================
    # GROUP
    # ==============================================================
    if entity_type.startswith(
        "GROUP"
    ):
        if not best.verified:
            return None, (
                "GROUP_USER_UNVERIFIED"
            )
    # ==============================================================
    # Second candidate conflict.
    # ==============================================================
    if len(usable) >= 2:
        second = usable[1]
        if (
            best.verified
            != second.verified
        ):
            # Verified beats weak candidate.
            if best.verified:
                return best, "OK"
        if (
            best.score
            - second.score
            < UID_CONFLICT_MARGIN
        ):
            return None, "CONFLICT"
    # ==============================================================
    # Need strong verification for uncertain object.
    # ==============================================================
    if (
        entity_type
        in {
            "POST",
            "VIDEO",
            "REEL",
            "PHOTO",
            "STORY",
        }
        and not best.verified
        and best.score < 75
    ):
        return None, "UNVERIFIED"
    return best, "OK"
# ======================================================================
# USERNAME / PUBLISHER NAME
# ======================================================================
def infer_username(
    parsed_url,
    snapshot,
):
    if parsed_url.username:
        return parsed_url.username
    for url in (
        snapshot.canonical_url,
        snapshot.meta.get(
            "og:url",
            ""
        ),
        snapshot.final_url,
    ):
        username = (
            extract_profile_username(
                url
            )
        )
        if username:
            return username
    for href in snapshot.links:
        username = (
            extract_profile_username(
                href
            )
        )
        if username:
            return username
    return ""
def infer_publisher_name(
    snapshot,
):
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
# RESOLVER
# ======================================================================
class FacebookResolver:
    def __init__(self):
        self.fetcher = HTTPFetcher()
        self.semaphore = asyncio.Semaphore(
            MAX_HTTP_CONCURRENCY
        )
    async def fetch_snapshot(
        self,
        url,
    ):
        async with self.semaphore:
            result = await asyncio.to_thread(
                self.fetcher.fetch,
                url
            )
        snapshot = parse_html_snapshot(
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
        )
        if result.get(
            "error"
        ):
            snapshot.errors.append(
                result.get(
                    "error"
                )
            )
        return snapshot, result
    async def verify_profile_async(
        self,
        profile_url,
    ):
        async with self.semaphore:
            return await asyncio.to_thread(
                verify_profile,
                self.fetcher,
                profile_url
            )
    async def resolve(
        self,
        url,
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
        # ==========================================================
        # EVIDENCE STORE
        # ==========================================================
        store = EvidenceStore()
        extract_url_evidence(
            parsed_url,
            store
        )
        # ==========================================================
        # REDIRECT WRAPPER
        # ==========================================================
        unwrapped = unwrap_redirect_url(
            parsed_url.normalized
        )
        if unwrapped != (
            parsed_url.normalized
        ):
            result.warnings.append(
                "URL có redirect wrapper."
            )
        # ==========================================================
        # FETCH ORIGINAL / SHARE
        # ==========================================================
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
        # ==========================================================
        # FINAL CLASSIFICATION
        # ==========================================================
        final_parsed = classify_url(
            result.resolved_url
        )
        # Final URL object route beats share wrapper.
        if final_parsed.url_type != "UNKNOWN":
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
        # ==========================================================
        # CANONICAL
        # ==========================================================
        canonical = (
            snapshot.meta.get(
                "og:url",
                ""
            )
            or snapshot.canonical_url
            or result.resolved_url
        )
        canonical = normalize_url(
            canonical
        )
        result.canonical_url = canonical
        snapshot.canonical_url = canonical
        # ==========================================================
        # COLLECT OBJECT EVIDENCE
        # ==========================================================
        collect_object_ids(
            parsed_url,
            snapshot,
            store
        )
        # Canonical can have better route semantics.
        if canonical:
            canonical_parsed = classify_url(
                canonical
            )
            if canonical_parsed.url_type != "UNKNOWN":
                # Canonical object type authoritative
                # unless original route is explicit group/page.
                if (
                    parsed_url.route_entity
                    not in {
                        "GROUP",
                        "PAGE",
                    }
                ):
                    result.url_type = (
                        canonical_parsed.url_type
                    )
            # Add canonical route evidence.
            extract_url_evidence(
                canonical_parsed,
                store
            )
            # Special photo.php owner.
            if (
                canonical_parsed.url_type
                == "PHOTO"
                and canonical_parsed.numeric_route_id
            ):
                store.add(
                    canonical_parsed.numeric_route_id,
                    "OWNER",
                    "canonical_photo_owner",
                    55,
                    context=canonical,
                    object_token=(
                        parsed_url.opaque_token
                    ),
                    independent_key=(
                        "canonical_photo_owner"
                    ),
                    locality=1.0,
                    direct=True,
                )
        # ==========================================================
        # FINAL ROUTE EVIDENCE
        # ==========================================================
        extract_url_evidence(
            final_parsed,
            store
        )
        # ==========================================================
        # OPAQUE TOKEN
        # ==========================================================
        result.opaque_token = (
            final_parsed.opaque_token
            or parsed_url.opaque_token
            or ""
        )
        if not result.opaque_token:
            match = PFBID_RE.search(
                snapshot.html_text
            )
            if match:
                result.opaque_token = (
                    match.group()
                )
        # ==========================================================
        # DISCOVER PROFILE
        # ==========================================================
        profile_urls = (
            discover_profile_urls(
                parsed_url,
                snapshot
            )
        )
        # Nếu canonical username tốt hơn.
        if canonical:
            canonical_parsed = classify_url(
                canonical
            )
            if (
                canonical_parsed.username
                and not parsed_url.username
            ):
                profile_urls.insert(
                    0,
                    "https://www.facebook.com/"
                    + canonical_parsed.username
                )
        # Dedup.
        dedup_profiles = []
        seen_profiles = set()
        for profile_url in profile_urls:
            key = profile_url.rstrip(
                "/"
            ).lower()
            if key in seen_profiles:
                continue
            seen_profiles.add(key)
            dedup_profiles.append(
                profile_url
            )
        profile_urls = dedup_profiles[
            :MAX_PROFILE_CHECKS
        ]
        # ==========================================================
        # VERIFY PROFILES CONCURRENTLY
        # ==========================================================
        profile_verifications = []
        if profile_urls:
            tasks = [
                self.verify_profile_async(
                    profile_url
                )
                for profile_url in profile_urls
            ]
            try:
                verified_results = (
                    await asyncio.gather(
                        *tasks,
                        return_exceptions=True
                    )
                )
            except Exception:
                verified_results = []
            for verification in verified_results:
                if isinstance(
                    verification,
                    Exception
                ):
                    continue
                if verification.uid:
                    profile_verifications.append(
                        verification
                    )
        # ==========================================================
        # PROFILE CORRELATION
        # ==========================================================
        correlate_profile_to_candidates(
            profile_verifications,
            parsed_url,
            store
        )
        # ==========================================================
        # SCORE USER
        # ==========================================================
        user_candidates = (
            score_user_candidates(
                store,
                parsed_url,
                profile_verifications
            )
        )
        # ==========================================================
        # PAGE / GROUP
        # ==========================================================
        page_ids, group_ids = (
            collect_page_group_ids(
                parsed_url,
                store
            )
        )
        # ==========================================================
        # ENTITY
        # ==========================================================
        entity_type = infer_entity(
            parsed_url,
            store,
            page_ids,
            group_ids,
            user_candidates,
            profile_verifications
        )
        result.entity_type = (
            entity_type
        )
        # ==========================================================
        # PUBLISHER TYPE
        # ==========================================================
        if entity_type.startswith(
            "GROUP"
        ):
            result.publisher_type = "GROUP"
        elif entity_type.startswith(
            "PAGE"
        ):
            result.publisher_type = "PAGE"
        elif entity_type.startswith(
            "USER"
        ):
            result.publisher_type = "USER"
        # ==========================================================
        # GROUP ID
        # ==========================================================
        if group_ids:
            # Route score dominates.
            result.group_id = max(
                group_ids,
                key=group_ids.get
            )
        # ==========================================================
        # PAGE UID
        # ==========================================================
        if page_ids:
            result.page_uid = max(
                page_ids,
                key=page_ids.get
            )
        # ==========================================================
        # PHOTO ALBUM
        # ==========================================================
        if (
            parsed_url.album_id
        ):
            result.album_id = (
                parsed_url.album_id
            )
        # Canonical photo may contain album.
        if (
            not result.album_id
            and canonical
        ):
            canonical_parsed = classify_url(
                canonical
            )
            if canonical_parsed.album_id:
                result.album_id = (
                    canonical_parsed.album_id
                )
        # ==========================================================
        # OBJECT IDs
        # ==========================================================
        if result.url_type in {
            "POST",
            "USER_POST",
            "PAGE_POST",
            "GROUP_POST",
        }:
            result.post_id = (
                choose_direct_object_id(
                    final_parsed,
                    store,
                    "POST"
                )
                or choose_direct_object_id(
                    parsed_url,
                    store,
                    "POST"
                )
            )
        elif result.url_type == "VIDEO":
            result.video_id = (
                choose_direct_object_id(
                    final_parsed,
                    store,
                    "VIDEO"
                )
                or choose_direct_object_id(
                    parsed_url,
                    store,
                    "VIDEO"
                )
            )
        elif result.url_type == "REEL":
            result.reel_id = (
                choose_direct_object_id(
                    final_parsed,
                    store,
                    "REEL"
                )
                or choose_direct_object_id(
                    parsed_url,
                    store,
                    "REEL"
                )
            )
        elif result.url_type == "PHOTO":
            result.photo_id = (
                choose_direct_object_id(
                    final_parsed,
                    store,
                    "PHOTO"
                )
                or choose_direct_object_id(
                    parsed_url,
                    store,
                    "PHOTO"
                )
            )
        elif result.url_type == "STORY":
            result.story_id = (
                choose_direct_object_id(
                    final_parsed,
                    store,
                    "STORY"
                )
                or choose_direct_object_id(
                    parsed_url,
                    store,
                    "STORY"
                )
            )
        # ==========================================================
        # OPAQUE OBJECT DISPLAY
        # ==========================================================
        if result.url_type in {
            "POST",
            "USER_POST",
            "PAGE_POST",
            "GROUP_POST",
        }:
            if (
                not result.post_id
                and result.opaque_token
            ):
                result.post_id = (
                    result.opaque_token
                )
        # ==========================================================
        # USER UID
        # ==========================================================
        # PAGE => tuyệt đối không user UID.
        if entity_type.startswith(
            "PAGE"
        ):
            result.user_uid = ""
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
                    "CONFLICT",
                    "AMBIGUOUS",
                    "GROUP_USER_UNVERIFIED",
                }:
                    result.warnings.append(
                        "Không đủ bằng chứng độc lập để xác minh USER UID."
                    )
        # ==========================================================
        # USERNAME
        # ==========================================================
        result.username = (
            infer_username(
                parsed_url,
                snapshot
            )
        )
        # Canonical username ưu tiên nếu route là share.
        if (
            parsed_url.is_share
            and canonical
        ):
            canonical_username = (
                extract_profile_username(
                    canonical
                )
            )
            if canonical_username:
                result.username = (
                    canonical_username
                )
        # ==========================================================
        # PUBLISHER NAME
        # ==========================================================
        result.publisher_name = (
            infer_publisher_name(
                snapshot
            )
        )
        # ==========================================================
        # CONFIDENCE
        # ==========================================================
        confidence = 0
        if result.user_uid:
            candidate = (
                user_candidates.get(
                    result.user_uid
                )
            )
            if candidate:
                confidence = int(
                    candidate.score
                )
                if candidate.verified:
                    confidence += 15
        # Direct profile.
        if (
            parsed_url.url_type
            == "PROFILE"
            and parsed_url.numeric_route_id
            and result.user_uid
            and same_numeric(
                parsed_url.numeric_route_id,
                result.user_uid
            )
        ):
            confidence = max(
                confidence,
                98
            )
        # Page route.
        if (
            result.page_uid
            and entity_type.startswith(
                "PAGE"
            )
        ):
            confidence = max(
                confidence,
                min(
                    99,
                    int(
                        page_ids.get(
                            result.page_uid,
                            0
                        )
                    )
                )
            )
        # Group route.
        if (
            result.group_id
            and entity_type.startswith(
                "GROUP"
            )
        ):
            confidence = max(
                confidence,
                min(
                    99,
                    int(
                        group_ids.get(
                            result.group_id,
                            0
                        )
                    )
                )
            )
        # ==========================================================
        # Clamp
        # ==========================================================
        result.confidence = min(
            99,
            max(
                0,
                confidence
            )
        )
        # ==========================================================
        # STATUS
        # ==========================================================
        if (
            result.user_uid
            and result.confidence >= 80
        ):
            result.status = "VERIFIED"
        elif (
            result.page_uid
            and entity_type.startswith(
                "PAGE"
            )
        ):
            result.status = "VERIFIED"
        elif (
            result.group_id
            and entity_type.startswith(
                "GROUP"
            )
        ):
            result.status = "VERIFIED"
        elif (
            result.post_id
            or result.video_id
            or result.photo_id
            or result.story_id
            or result.reel_id
            or result.opaque_token
        ):
            result.status = "PARTIAL"
            if not result.user_uid:
                result.warnings.append(
                    "Đã xác định object nhưng chưa xác minh được USER UID."
                )
        else:
            result.status = "UNRESOLVED"
        # ==========================================================
        # Special warning for conflicts.
        # ==========================================================
        uid_values = sorted(
            user_candidates.values(),
            key=lambda x: x.score,
            reverse=True
        )
        if len(uid_values) >= 2:
            a = uid_values[0]
            b = uid_values[1]
            if (
                a.score >= MIN_UID_SCORE
                and b.score >= MIN_UID_SCORE
                and abs(
                    a.score
                    - b.score
                ) < UID_CONFLICT_MARGIN
            ):
                result.warnings.append(
                    "Có nhiều USER UID ứng viên có điểm gần nhau; đã tránh đoán."
                )
        # ==========================================================
        # HTTP warnings.
        # ==========================================================
        if snapshot.status_code:
            if snapshot.status_code == 403:
                result.warnings.append(
                    "Facebook trả HTTP 403; dữ liệu công khai có thể bị giới hạn."
                )
            elif snapshot.status_code == 429:
                result.warnings.append(
                    "Facebook rate-limit HTTP 429."
                )
        if snapshot.redirects:
            result.warnings.append(
                "Facebook đã redirect URL."
            )
        # ==========================================================
        # Evidence
        # ==========================================================
        result.evidence_count = (
            store.count()
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
    # Các URL độc lập, xử lý concurrent.
    tasks = [
        resolver.resolve(url)
        for url in urls
    ]
    results_raw = await asyncio.gather(
        *tasks,
        return_exceptions=True
    )
    results = []
    for url, item in zip(
        urls,
        results_raw
    ):
        if isinstance(
            item,
            Exception
        ):
            results.append(
                ResolveResult(
                    original_url=url,
                    status="ERROR",
                    warnings=[
                        "Resolver error: "
                        + str(item)[:300]
                    ],
                )
            )
        else:
            results.append(
                item
            )
    return results
# ======================================================================
# RESULT LABEL
# ======================================================================
def entity_label(
    result
):
    mapping = {
        "USER":
            "👤 USER",
        "USER_POST":
            "👤 USER POST",
        "PAGE":
            "📄 PAGE",
        "PAGE_POST":
            "📄 PAGE POST",
        "GROUP":
            "👥 GROUP",
        "GROUP_POST":
            "👥 GROUP POST",
        "GROUP_MEDIA":
            "👥 GROUP MEDIA",
        "POST":
            "📝 POST",
        "VIDEO":
            "🎬 VIDEO",
        "REEL":
            "🎬 REEL",
        "PHOTO":
            "🖼 PHOTO",
        "STORY":
            "⭕ STORY",
        "PROFILE":
            "👤 PROFILE",
        "EVENT":
            "📅 EVENT",
        "MARKETPLACE":
            "🛒 MARKETPLACE",
        "UNKNOWN":
            "❓ UNKNOWN",
    }
    return mapping.get(
        result.entity_type,
        mapping.get(
            result.url_type,
            "❓ UNKNOWN"
        )
    )
# ======================================================================
# FORMAT RESULT
# ======================================================================
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
            f"{index}. "
            + entity_label(result)
        )
    else:
        lines.append(
            entity_label(result)
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
            + result.publisher_name[:180]
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
    if result.reel_id:
        lines.append(
            "🎞 REEL ID: "
            + result.reel_id
        )
    if result.photo_id:
        lines.append(
            "🖼 PHOTO ID: "
            + result.photo_id
        )
    if result.album_id:
        lines.append(
            "📁 ALBUM ID: "
            + result.album_id
        )
    if result.story_id:
        lines.append(
            "⭕ STORY ID: "
            + result.story_id
        )
    # --------------------------------------------------------------
    # Opaque token
    # --------------------------------------------------------------
    if (
        result.opaque_token
        and
        result.opaque_token
        != result.post_id
    ):
        lines.append(
            "🔑 OBJECT TOKEN: "
            + result.opaque_token[:300]
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
    # Warning
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
            if not warning:
                continue
            if warning in seen:
                continue
            seen.add(
                warning
            )
            lines.append(
                "• "
                + warning[:320]
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
# TELEGRAM WAIT LOOP
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
# MAIN COMMAND
# ======================================================================
async def _handle_getuidfb(
    event,
    notify_bot=None,
):
    sender_id = event.sender_id
    # --------------------------------------------------------------
    # Duplicate session protection.
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
            "🔎 **FACEBOOK UID V21 MAX ACCURACY**\n\n"
            "Hãy gửi URL Facebook cần kiểm tra.\n\n"
            "Có thể gửi:\n"
            "• 1 URL\n"
            "• nhiều URL\n"
            "• URL cách nhau bằng dấu cách\n"
            "• URL cách nhau bằng xuống dòng\n"
            "• URL bằng dấu phẩy\n"
            "• URL dính liền nhau\n\n"
            "Ví dụ:\n"
            "`https://facebook.com/share/p/AAA/https://facebook.com/user/posts/BBB`\n\n"
            "Bot sẽ tự nhận diện từng URL và xác định:\n"
            "USER / PAGE / GROUP\n"
            "POST / REEL / VIDEO / PHOTO / STORY / PROFILE\n\n"
            "🎯 UID chỉ được trả khi có đủ bằng chứng.\n"
            "⏳ Phiên chờ tối đa 15 phút."
        )
        while True:
            incoming = (
                await wait_for_facebook_message(
                    bot=event.client,
                    event=event,
                    timeout=900,
                )
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
            # Command => thoát session.
            # ------------------------------------------------------
            if text.startswith("/"):
                break
            # ------------------------------------------------------
            # Extract Facebook URLs.
            # ------------------------------------------------------
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
            # Count.
            # ------------------------------------------------------
            try:
                await incoming.reply(
                    "🔗 Đã nhận diện **"
                    + str(len(urls))
                    + " URL Facebook**.\n"
                    "⏳ Đang phân tích với V21 MAX ACCURACY..."
                )
            except Exception:
                pass
            # ------------------------------------------------------
            # Resolve.
            #
            # resolve_facebook_urls giữ nguyên thứ tự.
            # ------------------------------------------------------
            results = (
                await resolve_facebook_urls(
                    urls
                )
            )
            # ------------------------------------------------------
            # Send separately.
            # ------------------------------------------------------
            for index, result in enumerate(
                results,
                1
            ):
                try:
                    output = format_result(
                        result,
                        index=(
                            index
                            if len(urls) > 1
                            else None
                        )
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
                    try:
                        await incoming.reply(
                            "❌ Lỗi xử lý URL #"
                            + str(index)
                            + ":\n"
                            + str(exc)[:1000]
                        )
                    except Exception:
                        pass
            # ------------------------------------------------------
            # Continue loop.
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
    GIỮ NGUYÊN API:
        module.register(bot, notify_bot)
    Telethon chỉ gọi handler(event).
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
# COMPATIBILITY
# ======================================================================
handler = _handle_getuidfb
# ======================================================================
# END
# ======================================================================