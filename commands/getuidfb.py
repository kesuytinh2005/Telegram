#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================
 FACEBOOK UID / ENTITY RESOLVER V21
 ACCURACY + STABILITY EDITION
============================================================
PUBLIC HTTP ONLY
NO:
- Playwright
- Selenium
- Facebook Login
- Facebook Cookie
- Facebook Access Token
DESIGN:
- Broad evidence collection
- Role-aware ID extraction
- UID candidate correlation
- Profile verification
- Canonical / OG / JSON-LD parsing
- Redirect following
- Share URL resolution
- pfbid correlation
- actor_id / entity_id retained as evidence
- No blind numeric-ID assignment
- Concatenated Facebook URL splitting
- Multi URL processing
- Telethon-compatible register()
- Per-user interactive session
- Task-manager compatible
IMPORTANT:
Accuracy > Recall.
If Facebook does not expose enough evidence to prove a UID,
the resolver returns NOT VERIFIED instead of inventing one.
============================================================
"""
from __future__ import annotations
import asyncio
import base64
import binascii
import html as html_lib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
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
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telethon import events
# ============================================================
# TASK MANAGER
# ============================================================
try:
    from core.task_manager import (
        replace_user_tasks,
        track_current_task,
    )
except Exception:
    replace_user_tasks = None
    track_current_task = None
# ============================================================
# COMMAND INFO
# ============================================================
COMMAND_INFO = {
    "command": "getuidfb",
    "description": "Resolve Facebook UID / Page / Group / Post / Reel / Video / Photo / Story",
    "usage": "/getuidfb",
    "category": "facebook",
}
# ============================================================
# LOGGING
# ============================================================
logger = logging.getLogger(__name__)
# ============================================================
# CONFIG
# ============================================================
REQUEST_TIMEOUT = (6, 14)
MAX_HTML_BYTES = 12 * 1024 * 1024
MAX_DISCOVERED_LINKS = 120
MAX_PROFILE_CHECKS = 3
MAX_CANDIDATES = 80
SESSION_TIMEOUT = 15 * 60
MAX_INPUT_URLS = 30
# ============================================================
# USER AGENTS
# ============================================================
USER_AGENTS = [
    (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 13; SM-S918B) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.5 Safari/605.1.15"
    ),
]
# ============================================================
# FACEBOOK HOSTS
# ============================================================
FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "mobile.facebook.com",
    "touch.facebook.com",
    "fb.watch",
    "www.fb.watch",
}
# ============================================================
# COMMAND EXIT WORDS
# ============================================================
COMMAND_PREFIXES = {
    "/start",
    "/stop",
    "/download",
    "/getuidfb",
    "/timbaihat",
    "/gettoken",
    "/checkliveuid",
    "/getinfoprofile",
    "/uid",
    "/theodoi",
    "/cupdien",
    "/tiktok",
}
# ============================================================
# SEMANTIC KEY WEIGHTS
# ============================================================
UID_KEY_WEIGHTS = {
    "user_id": 110,
    "userid": 108,
    "profile_id": 108,
    "profileid": 106,
    "owner_id": 100,
    "ownerid": 98,
    "publisher_id": 100,
    "publisherid": 98,
    "author_id": 98,
    "authorid": 96,
    "from_id": 94,
    "fromid": 92,
    "actor_id": 78,
    "actorid": 76,
    "entity_id": 62,
    "entityid": 60,
    "target_id": 55,
    "targetid": 53,
}
OBJECT_KEY_WEIGHTS = {
    "post_id": 100,
    "postid": 100,
    "story_fbid": 100,
    "storyfbid": 100,
    "media_fbid": 96,
    "mediafbid": 96,
    "video_id": 100,
    "videoid": 100,
    "photo_id": 100,
    "photoid": 100,
    "reel_id": 100,
    "reelid": 100,
    "fbid": 82,
}
PAGE_KEY_WEIGHTS = {
    "page_id": 110,
    "pageid": 108,
}
GROUP_KEY_WEIGHTS = {
    "group_id": 115,
    "groupid": 112,
}
# ============================================================
# DATACLASSES
# ============================================================
@dataclass
class URLShape:
    original: str
    normalized: str
    host: str = ""
    path: str = ""
    query: Dict[str, List[str]] = field(default_factory=dict)
    kind: str = "UNKNOWN"
    username: Optional[str] = None
    post_token: Optional[str] = None
    share_token: Optional[str] = None
    route_id: Optional[str] = None
    route_ids: List[str] = field(default_factory=list)
@dataclass
class Evidence:
    value: str
    role: str
    source: str
    weight: float
    context: str = ""
    key: Optional[str] = None
    url: Optional[str] = None
    token: Optional[str] = None
    independent: str = ""
    verified: bool = False
@dataclass
class Snapshot:
    requested_url: str
    final_url: str
    status_code: int
    html: str
    title: str = ""
    canonical: Optional[str] = None
    og: Dict[str, str] = field(default_factory=dict)
    meta: Dict[str, str] = field(default_factory=dict)
    links: List[str] = field(default_factory=list)
    jsonld: List[Any] = field(default_factory=list)
    redirects: List[str] = field(default_factory=list)
@dataclass
class Candidate:
    value: str
    score: float = 0.0
    evidences: List[Evidence] = field(default_factory=list)
    roles: Set[str] = field(default_factory=set)
    sources: Set[str] = field(default_factory=set)
    independent: Set[str] = field(default_factory=set)
    verified: bool = False
    conflict: bool = False
    username_match: bool = False
    profile_match: bool = False
    page_conflict: bool = False
    group_conflict: bool = False
@dataclass
class ResolveResult:
    input_url: str
    resolved_url: str
    canonical_url: Optional[str]
    url_type: str
    entity_type: str
    publisher_type: Optional[str]
    username: Optional[str]
    user_uid: Optional[str]
    page_uid: Optional[str]
    group_id: Optional[str]
    post_id: Optional[str]
    reel_id: Optional[str]
    video_id: Optional[str]
    photo_id: Optional[str]
    story_id: Optional[str]
    album_id: Optional[str]
    publisher_id: Optional[str]
    title: Optional[str]
    status: str
    confidence: int
    evidence_count: int
    warnings: List[str] = field(default_factory=list)
    evidence_summary: List[str] = field(default_factory=list)
    elapsed: float = 0.0
# ============================================================
# HELPERS
# ============================================================
def clean_text(value: Any) -> str:
    if value is None:
        return ""
    value = html_lib.unescape(str(value))
    value = value.replace("\\/", "/")
    return value.strip()
def clean_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = clean_text(value)
    value = value.strip("\"' ")
    if not value:
        return None
    if not re.fullmatch(r"\d{1,25}", value):
        return None
    return value
def plausible_uid(value: Optional[str]) -> bool:
    """
    UID candidate must be reasonably long.
    This deliberately rejects things such as:
        22130
        12345
        7682435
    because generic HTML frequently contains short numbers.
    """
    if not value:
        return False
    if not value.isdigit():
        return False
    if len(value) < 8:
        return False
    if len(value) > 21:
        return False
    if len(set(value)) == 1:
        return False
    return True
def plausible_object_id(value: Optional[str]) -> bool:
    if not value:
        return False
    if not value.isdigit():
        return False
    if len(value) < 5:
        return False
    if len(value) > 25:
        return False
    return True
def normalize_username(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = unquote(value).strip().strip("/")
    if value.startswith("@"):
        value = value[1:]
    if not value:
        return None
    return value.lower()
# ============================================================
# URL
# ============================================================
def is_facebook_host(host: str) -> bool:
    host = (host or "").lower().split(":")[0]
    return (
        host in FACEBOOK_HOSTS
        or host.endswith(".facebook.com")
        or host.endswith(".fb.watch")
    )
def normalize_url(url: str) -> str:
    url = html_lib.unescape(url.strip())
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("www.facebook.com"):
        url = "https://" + url
    elif url.startswith("facebook.com"):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = "www." + host[4:]
    path = re.sub(r"/{2,}", "/", parsed.path)
    return urlunparse(
        (
            parsed.scheme.lower(),
            host,
            path,
            "",
            parsed.query,
            "",
        )
    )
# ============================================================
# URL EXTRACTION
# ============================================================
URL_START_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"(?:m|mbasic|web|mobile|touch)\.facebook\.com"
    r"|facebook\.com"
    r"|(?:www\.)?fb\.watch"
    r")"
    r"(?=/|$|\?)",
    re.I,
)
BARE_FB_RE = re.compile(
    r"(?<![\w@])"
    r"(?:www\.)?facebook\.com"
    r"(?=/|$|\?)"
    r"[^\s<>\[\]{}\"'`]+",
    re.I,
)
def trim_url_punctuation(value: str) -> str:
    value = value.strip()
    while value and value[-1] in ".,;:!?)]}>\"'":
        value = value[:-1]
    return value
def extract_facebook_urls(text: str) -> List[str]:
    """
    Robust extraction.
    Important:
        /share/p/ABC/https://facebook.com/XYZ
    becomes 2 URLs.
    """
    if not text:
        return []
    text = html_lib.unescape(text)
    found: List[Tuple[int, str]] = []
    starts = list(URL_START_RE.finditer(text))
    for index, match in enumerate(starts):
        start = match.start()
        if index + 1 < len(starts):
            end = starts[index + 1].start()
        else:
            end = len(text)
        chunk = text[start:end]
        chunk = re.split(
            r"[\s<>\[\]{}\"'`]",
            chunk,
            maxsplit=1,
        )[0]
        chunk = trim_url_punctuation(chunk)
        if chunk:
            found.append((start, chunk))
    # Bare facebook.com URLs
    for match in BARE_FB_RE.finditer(text):
        value = trim_url_punctuation(match.group(0))
        # Don't duplicate URLs already captured
        found.append((match.start(), value))
    found.sort(key=lambda x: x[0])
    result = []
    seen = set()
    for _, url in found:
        normalized = normalize_url(url)
        if not is_facebook_host(urlparse(normalized).netloc):
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
        if len(result) >= MAX_INPUT_URLS:
            break
    return result
# ============================================================
# URL CLASSIFICATION
# ============================================================
def parse_shape(url: str) -> URLShape:
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    host = parsed.netloc.lower()
    path = unquote(parsed.path or "/")
    path = re.sub(r"/+", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    query = parse_qs(
        parsed.query,
        keep_blank_values=True,
    )
    segments = [
        unquote(x)
        for x in path.split("/")
        if x
    ]
    lower_segments = [x.lower() for x in segments]
    shape = URLShape(
        original=url,
        normalized=normalized,
        host=host,
        path=path,
        query=query,
    )
    # --------------------------------------------------------
    # PROFILE.PHP
    # --------------------------------------------------------
    if lower_segments and lower_segments[-1] == "profile.php":
        if query.get("id"):
            shape.kind = "PROFILE"
            shape.route_id = query["id"][0]
            return shape
    # --------------------------------------------------------
    # PHOTO.PHP
    # --------------------------------------------------------
    if lower_segments and lower_segments[-1] == "photo.php":
        if query.get("fbid"):
            shape.kind = "PHOTO"
            shape.route_id = query["fbid"][0]
            if query.get("id"):
                shape.username = query["id"][0]
            return shape
    # --------------------------------------------------------
    # STORY.PHP
    # --------------------------------------------------------
    if lower_segments and lower_segments[-1] == "story.php":
        if query.get("story_fbid"):
            shape.kind = "STORY"
            shape.route_id = query["story_fbid"][0]
            return shape
    # --------------------------------------------------------
    # WATCH
    # --------------------------------------------------------
    if "watch" in lower_segments:
        if query.get("v"):
            shape.kind = "VIDEO"
            shape.route_id = query["v"][0]
            return shape
    # --------------------------------------------------------
    # PERMALINK
    # --------------------------------------------------------
    if lower_segments and lower_segments[-1] == "permalink.php":
        if query.get("story_fbid"):
            shape.kind = "POST"
            shape.post_token = query["story_fbid"][0]
            return shape
    # --------------------------------------------------------
    # SHARE
    # --------------------------------------------------------
    for i, segment in enumerate(lower_segments):
        if segment == "share" and i + 2 < len(segments):
            share_type = lower_segments[i + 1]
            token = segments[i + 2]
            shape.share_token = token
            if share_type == "p":
                shape.kind = "POST"
            elif share_type == "v":
                shape.kind = "VIDEO"
            elif share_type == "r":
                shape.kind = "REEL"
            elif share_type == "s":
                shape.kind = "STORY"
            else:
                shape.kind = "SHARE"
            return shape
    # --------------------------------------------------------
    # GROUPS
    # --------------------------------------------------------
    if "groups" in lower_segments:
        i = lower_segments.index("groups")
        if i + 1 < len(segments):
            gid = segments[i + 1]
            shape.route_id = gid
            shape.route_ids.append(gid)
            if i + 2 < len(segments):
                next_seg = lower_segments[i + 2]
                if next_seg in {
                    "posts",
                    "permalink",
                    "photos",
                    "videos",
                    "reels",
                }:
                    shape.kind = "GROUP_POST"
                else:
                    shape.kind = "GROUP"
            else:
                shape.kind = "GROUP"
            return shape
    # --------------------------------------------------------
    # PAGES
    # --------------------------------------------------------
    if "pages" in lower_segments:
        i = lower_segments.index("pages")
        if i + 2 < len(segments):
            page_id = segments[i + 2]
            shape.route_id = page_id
            shape.route_ids.append(page_id)
            if i + 3 < len(segments):
                shape.kind = "PAGE_POST"
            else:
                shape.kind = "PAGE"
            return shape
    # --------------------------------------------------------
    # REEL
    # --------------------------------------------------------
    if "reel" in lower_segments:
        i = lower_segments.index("reel")
        if i + 1 < len(segments):
            shape.kind = "REEL"
            shape.route_id = segments[i + 1]
            return shape
    if "reels" in lower_segments:
        i = lower_segments.index("reels")
        if i + 1 < len(segments):
            shape.kind = "REEL"
            shape.route_id = segments[i + 1]
            return shape
    # --------------------------------------------------------
    # VIDEO / VIDEOS
    # --------------------------------------------------------
    for marker in ("video", "videos"):
        if marker in lower_segments:
            i = lower_segments.index(marker)
            if i + 1 < len(segments):
                shape.kind = "VIDEO"
                shape.route_id = segments[i + 1]
                if i > 0:
                    shape.username = normalize_username(
                        segments[i - 1]
                    )
                return shape
    # --------------------------------------------------------
    # PHOTOS
    # --------------------------------------------------------
    if "photos" in lower_segments:
        i = lower_segments.index("photos")
        if i + 1 < len(segments):
            shape.kind = "PHOTO"
            shape.route_id = segments[-1]
            if i > 0:
                shape.username = normalize_username(
                    segments[i - 1]
                )
            return shape
    # --------------------------------------------------------
    # STORIES
    # --------------------------------------------------------
    if "stories" in lower_segments:
        i = lower_segments.index("stories")
        if i + 1 < len(segments):
            shape.kind = "STORY"
            shape.route_ids = segments[i + 1:]
            shape.route_id = segments[-1]
            return shape
    # --------------------------------------------------------
    # P / POSTS
    # --------------------------------------------------------
    if "posts" in lower_segments:
        i = lower_segments.index("posts")
        if i + 1 < len(segments):
            shape.kind = "POST"
            shape.post_token = segments[i + 1]
            if i > 0:
                shape.username = normalize_username(
                    segments[i - 1]
                )
            return shape
    # --------------------------------------------------------
    # /p/<token>
    # --------------------------------------------------------
    if "p" in lower_segments:
        i = lower_segments.index("p")
        if i + 1 < len(segments):
            shape.kind = "POST"
            shape.post_token = segments[i + 1]
            return shape
    # --------------------------------------------------------
    # /photo/<id>
    # --------------------------------------------------------
    if "photo" in lower_segments:
        i = lower_segments.index("photo")
        if i + 1 < len(segments):
            shape.kind = "PHOTO"
            shape.route_id = segments[i + 1]
            return shape
    # --------------------------------------------------------
    # /username
    # --------------------------------------------------------
    if segments:
        first = segments[0]
        reserved = {
            "home",
            "login",
            "watch",
            "marketplace",
            "events",
            "gaming",
            "groups",
            "pages",
            "people",
            "public",
            "photo.php",
            "profile.php",
            "story.php",
            "permalink.php",
            "share",
            "reel",
            "reels",
            "videos",
            "photos",
            "stories",
        }
        if first.lower() not in reserved:
            shape.kind = "PROFILE"
            shape.username = normalize_username(first)
            return shape
    return shape
# ============================================================
# HTML PARSER
# ============================================================
class FBHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )
        self.title_parts: List[str] = []
        self.meta: Dict[str, str] = {}
        self.links: List[str] = []
        self.jsonld: List[Any] = []
        self._inside_title = False
        self._inside_jsonld = False
        self._jsonld_parts: List[str] = []
    def handle_starttag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ):
        attrs_dict = {
            str(k).lower(): v
            for k, v in attrs
        }
        tag = tag.lower()
        if tag == "title":
            self._inside_title = True
        elif tag == "meta":
            key = (
                attrs_dict.get("property")
                or attrs_dict.get("name")
                or attrs_dict.get("itemprop")
            )
            content = attrs_dict.get("content")
            if key and content:
                self.meta[
                    clean_text(key).lower()
                ] = clean_text(content)
        elif tag == "link":
            rel = clean_text(
                attrs_dict.get("rel")
            ).lower()
            href = attrs_dict.get("href")
            if href and "canonical" in rel:
                self.meta["canonical"] = clean_text(href)
            elif href:
                self.links.append(
                    clean_text(href)
                )
        elif tag == "a":
            href = attrs_dict.get("href")
            if href:
                self.links.append(
                    clean_text(href)
                )
        elif (
            tag == "script"
            and clean_text(
                attrs_dict.get("type")
            ).lower()
            == "application/ld+json"
        ):
            self._inside_jsonld = True
            self._jsonld_parts = []
    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag == "title":
            self._inside_title = False
        elif tag == "script" and self._inside_jsonld:
            self._inside_jsonld = False
            raw = "".join(
                self._jsonld_parts
            ).strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    self.jsonld.append(parsed)
                except Exception:
                    pass
            self._jsonld_parts = []
    def handle_data(self, data: str):
        if self._inside_title:
            self.title_parts.append(data)
        if self._inside_jsonld:
            self._jsonld_parts.append(data)
    @property
    def title(self) -> str:
        return clean_text(
            " ".join(
                self.title_parts
            )
        )
# ============================================================
# JSON WALKER
# ============================================================
def walk_json(
    obj: Any,
) -> Iterable[Tuple[str, Any, str]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_str = str(key)
            yield key_str, value, key_str
            yield from walk_json(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_json(item)
# ============================================================
# JSON VALUE
# ============================================================
def extract_numeric_from_value(
    value: Any,
) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return None
    if isinstance(value, str):
        match = re.fullmatch(
            r"\s*(\d{1,25})\s*",
            value,
        )
        if match:
            return match.group(1)
    return None
# ============================================================
# REQUEST SESSION
# ============================================================
_thread_local = threading.local()
def get_session() -> requests.Session:
    session = getattr(
        _thread_local,
        "session",
        None,
    )
    if session is not None:
        return session
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(
            ["GET", "HEAD"]
        ),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=8,
        pool_maxsize=8,
    )
    session.mount(
        "https://",
        adapter,
    )
    session.mount(
        "http://",
        adapter,
    )
    session.headers.update(
        {
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
    )
    _thread_local.session = session
    return session
# ============================================================
# HTTP FETCH
# ============================================================
def fetch_html_sync(
    url: str,
    user_agent: str,
) -> Snapshot:
    session = get_session()
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
    response = session.get(
        url,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
        stream=True,
    )
    content = bytearray()
    try:
        for chunk in response.iter_content(
            chunk_size=64 * 1024
        ):
            if not chunk:
                continue
            remaining = (
                MAX_HTML_BYTES
                - len(content)
            )
            if remaining <= 0:
                break
            content.extend(
                chunk[:remaining]
            )
            if len(content) >= MAX_HTML_BYTES:
                break
    finally:
        response.close()
    encoding = (
        response.encoding
        or "utf-8"
    )
    try:
        text = bytes(content).decode(
            encoding,
            errors="replace",
        )
    except Exception:
        text = bytes(content).decode(
            "utf-8",
            errors="replace",
        )
    parser = FBHTMLParser()
    try:
        parser.feed(text)
    except Exception:
        pass
    final_url = normalize_url(
        response.url
    )
    canonical = parser.meta.get(
        "canonical"
    )
    if canonical:
        canonical = normalize_url(
            urljoin(
                final_url,
                canonical,
            )
        )
    og = {}
    for key, value in parser.meta.items():
        if key.startswith("og:"):
            og[key] = value
    redirects = []
    for item in response.history:
        redirects.append(
            normalize_url(
                item.url
            )
        )
    redirects.append(final_url)
    return Snapshot(
        requested_url=url,
        final_url=final_url,
        status_code=response.status_code,
        html=text,
        title=parser.title,
        canonical=canonical,
        og=og,
        meta=parser.meta,
        links=parser.links[
            :MAX_DISCOVERED_LINKS
        ],
        jsonld=parser.jsonld,
        redirects=redirects,
    )
async def fetch_html(
    url: str,
) -> Snapshot:
    last_error = None
    for index, ua in enumerate(
        USER_AGENTS
    ):
        try:
            snapshot = await asyncio.to_thread(
                fetch_html_sync,
                url,
                ua,
            )
            if (
                snapshot.status_code
                not in {403, 429}
            ):
                return snapshot
            last_error = RuntimeError(
                f"HTTP {snapshot.status_code}"
            )
        except Exception as exc:
            last_error = exc
        if index + 1 < len(USER_AGENTS):
            await asyncio.sleep(
                0.25 * (index + 1)
            )
    if last_error:
        raise last_error
    raise RuntimeError(
        "Facebook request failed"
    )
# ============================================================
# SEMANTIC REGEX
# ============================================================
SEMANTIC_PATTERNS: Dict[str, List[re.Pattern]] = {}
def compile_patterns():
    aliases = {
        "user_id": [
            "user_id",
            "userId",
            "userID",
        ],
        "profile_id": [
            "profile_id",
            "profileId",
            "profileID",
        ],
        "owner_id": [
            "owner_id",
            "ownerId",
            "ownerID",
        ],
        "publisher_id": [
            "publisher_id",
            "publisherId",
            "publisherID",
        ],
        "author_id": [
            "author_id",
            "authorId",
            "authorID",
        ],
        "actor_id": [
            "actor_id",
            "actorId",
            "actorID",
        ],
        "entity_id": [
            "entity_id",
            "entityId",
            "entityID",
        ],
        "target_id": [
            "target_id",
            "targetId",
            "targetID",
        ],
        "page_id": [
            "page_id",
            "pageId",
            "pageID",
        ],
        "group_id": [
            "group_id",
            "groupId",
            "groupID",
        ],
        "post_id": [
            "post_id",
            "postId",
            "postID",
        ],
        "story_fbid": [
            "story_fbid",
            "storyFbid",
        ],
        "media_fbid": [
            "media_fbid",
            "mediaFbid",
        ],
        "video_id": [
            "video_id",
            "videoId",
            "videoID",
        ],
        "photo_id": [
            "photo_id",
            "photoId",
            "photoID",
        ],
        "reel_id": [
            "reel_id",
            "reelId",
            "reelID",
        ],
        "fbid": [
            "fbid",
        ],
    }
    for semantic, names in aliases.items():
        patterns = []
        for name in names:
            patterns.append(
                re.compile(
                    rf"""
                    (?:
                        ["']{re.escape(name)}["']
                        |
                        \b{re.escape(name)}\b
                    )
                    \s*
                    (?:
                        :
                        |
                        =
                    )
                    \s*
                    ["']?
                    (\d{{1,25}})
                    ["']?
                    """,
                    re.I | re.X,
                )
            )
        SEMANTIC_PATTERNS[
            semantic
        ] = patterns
compile_patterns()
# ============================================================
# NESTED JSON SEMANTIC EVIDENCE
# ============================================================
def extract_json_evidence(
    jsonld: List[Any],
    source: str,
    url: str,
) -> List[Evidence]:
    evidences = []
    def recurse(
        obj: Any,
        parent_keys: List[str],
    ):
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_lower = (
                    str(key)
                    .lower()
                    .replace("-", "_")
                )
                numeric = (
                    extract_numeric_from_value(
                        value
                    )
                )
                if numeric:
                    if key_lower in UID_KEY_WEIGHTS:
                        evidences.append(
                            Evidence(
                                value=numeric,
                                role="USER_CANDIDATE",
                                source=source,
                                weight=UID_KEY_WEIGHTS[
                                    key_lower
                                ],
                                context=(
                                    "/".join(
                                        parent_keys
                                        + [str(key)]
                                    )
                                ),
                                key=key_lower,
                                url=url,
                                independent=source,
                            )
                        )
                    elif key_lower in PAGE_KEY_WEIGHTS:
                        evidences.append(
                            Evidence(
                                value=numeric,
                                role="PAGE_ID",
                                source=source,
                                weight=PAGE_KEY_WEIGHTS[
                                    key_lower
                                ],
                                context=(
                                    "/".join(
                                        parent_keys
                                        + [str(key)]
                                    )
                                ),
                                key=key_lower,
                                url=url,
                                independent=source,
                            )
                        )
                    elif key_lower in GROUP_KEY_WEIGHTS:
                        evidences.append(
                            Evidence(
                                value=numeric,
                                role="GROUP_ID",
                                source=source,
                                weight=GROUP_KEY_WEIGHTS[
                                    key_lower
                                ],
                                context=(
                                    "/".join(
                                        parent_keys
                                        + [str(key)]
                                    )
                                ),
                                key=key_lower,
                                url=url,
                                independent=source,
                            )
                        )
                    elif key_lower in OBJECT_KEY_WEIGHTS:
                        evidences.append(
                            Evidence(
                                value=numeric,
                                role=key_lower.upper(),
                                source=source,
                                weight=OBJECT_KEY_WEIGHTS[
                                    key_lower
                                ],
                                context=(
                                    "/".join(
                                        parent_keys
                                        + [str(key)]
                                    )
                                ),
                                key=key_lower,
                                url=url,
                                independent=source,
                            )
                        )
                recurse(
                    value,
                    parent_keys + [str(key)],
                )
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                recurse(
                    value,
                    parent_keys + [str(index)],
                )
    for item in jsonld:
        recurse(item, [])
    return evidences
# ============================================================
# RAW HTML SEMANTIC EVIDENCE
# ============================================================
def extract_raw_semantic_evidence(
    text: str,
    source: str,
    url: str,
    token: Optional[str] = None,
) -> List[Evidence]:
    evidences = []
    if not text:
        return evidences
    for semantic, patterns in SEMANTIC_PATTERNS.items():
        for pattern in patterns:
            for match in pattern.finditer(text):
                value = clean_id(
                    match.group(1)
                )
                if not value:
                    continue
                start = max(
                    0,
                    match.start() - 180,
                )
                end = min(
                    len(text),
                    match.end() + 180,
                )
                context = text[
                    start:end
                ]
                if semantic in UID_KEY_WEIGHTS:
                    evidences.append(
                        Evidence(
                            value=value,
                            role="USER_CANDIDATE",
                            source=source,
                            weight=UID_KEY_WEIGHTS[
                                semantic
                            ],
                            context=context,
                            key=semantic,
                            url=url,
                            token=token,
                            independent=source,
                        )
                    )
                elif semantic in PAGE_KEY_WEIGHTS:
                    evidences.append(
                        Evidence(
                            value=value,
                            role="PAGE_ID",
                            source=source,
                            weight=PAGE_KEY_WEIGHTS[
                                semantic
                            ],
                            context=context,
                            key=semantic,
                            url=url,
                            token=token,
                            independent=source,
                        )
                    )
                elif semantic in GROUP_KEY_WEIGHTS:
                    evidences.append(
                        Evidence(
                            value=value,
                            role="GROUP_ID",
                            source=source,
                            weight=GROUP_KEY_WEIGHTS[
                                semantic
                            ],
                            context=context,
                            key=semantic,
                            url=url,
                            token=token,
                            independent=source,
                        )
                    )
                elif semantic in OBJECT_KEY_WEIGHTS:
                    evidences.append(
                        Evidence(
                            value=value,
                            role=semantic.upper(),
                            source=source,
                            weight=OBJECT_KEY_WEIGHTS[
                                semantic
                            ],
                            context=context,
                            key=semantic,
                            url=url,
                            token=token,
                            independent=source,
                        )
                    )
    return evidences
# ============================================================
# SPECIAL HTML PATTERNS
# ============================================================
SPECIAL_PATTERNS = [
    (
        "from.id",
        re.compile(
            r"""
            ["']from["']
            \s*:\s*
            \{
            [^{}]{0,1000}?
            ["']id["']
            \s*:\s*
            ["']?(\d{5,25})
            """,
            re.I | re.X,
        ),
        "USER_CANDIDATE",
        88,
    ),
    (
        "author.id",
        re.compile(
            r"""
            ["']author["']
            \s*:\s*
            \{
            [^{}]{0,1000}?
            ["']id["']
            \s*:\s*
            ["']?(\d{5,25})
            """,
            re.I | re.X,
        ),
        "USER_CANDIDATE",
        88,
    ),
    (
        "owner.id",
        re.compile(
            r"""
            ["']owner["']
            \s*:\s*
            \{
            [^{}]{0,1000}?
            ["']id["']
            \s*:\s*
            ["']?(\d{5,25})
            """,
            re.I | re.X,
        ),
        "USER_CANDIDATE",
        88,
    ),
    (
        "publisher.id",
        re.compile(
            r"""
            ["']publisher["']
            \s*:\s*
            \{
            [^{}]{0,1000}?
            ["']id["']
            \s*:\s*
            ["']?(\d{5,25})
            """,
            re.I | re.X,
        ),
        "USER_CANDIDATE",
        86,
    ),
    (
        "data-user-id",
        re.compile(
            r"""
            data-(?:user|profile)-id
            \s*=\s*
            ["'](\d{8,21})["']
            """,
            re.I | re.X,
        ),
        "USER_CANDIDATE",
        94,
    ),
]
def extract_special_evidence(
    text: str,
    source: str,
    url: str,
    token: Optional[str] = None,
) -> List[Evidence]:
    result = []
    for (
        name,
        pattern,
        role,
        weight,
    ) in SPECIAL_PATTERNS:
        for match in pattern.finditer(text):
            value = clean_id(
                match.group(1)
            )
            if not value:
                continue
            result.append(
                Evidence(
                    value=value,
                    role=role,
                    source=source,
                    weight=weight,
                    context=text[
                        max(
                            0,
                            match.start() - 150,
                        ):
                        min(
                            len(text),
                            match.end() + 150,
                        )
                    ],
                    key=name,
                    url=url,
                    token=token,
                    independent=source,
                )
            )
    return result
# ============================================================
# URL EVIDENCE
# ============================================================
def add_url_evidence(
    shape: URLShape,
    source: str,
) -> List[Evidence]:
    result = []
    query = shape.query
    # --------------------------------------------------------
    # profile.php?id=
    # --------------------------------------------------------
    if shape.kind == "PROFILE":
        if query.get("id"):
            uid = clean_id(
                query["id"][0]
            )
            if plausible_uid(uid):
                result.append(
                    Evidence(
                        value=uid,
                        role="USER_CANDIDATE",
                        source=source,
                        weight=120,
                        key="profile.php?id",
                        url=shape.normalized,
                        independent="profile-query",
                    )
                )
    # --------------------------------------------------------
    # photo.php
    # --------------------------------------------------------
    if shape.kind == "PHOTO":
        if query.get("fbid"):
            photo_id = clean_id(
                query["fbid"][0]
            )
            if plausible_object_id(photo_id):
                result.append(
                    Evidence(
                        value=photo_id,
                        role="PHOTO_ID",
                        source=source,
                        weight=125,
                        key="photo.php?fbid",
                        url=shape.normalized,
                        independent="photo-query",
                    )
                )
        # IMPORTANT:
        # id= on photo.php is publisher/profile context,
        # NOT group ID and NOT photo ID.
        if query.get("id"):
            publisher = clean_id(
                query["id"][0]
            )
            if plausible_uid(publisher):
                result.append(
                    Evidence(
                        value=publisher,
                        role="USER_CANDIDATE",
                        source=source,
                        weight=90,
                        key="photo.php?id",
                        url=shape.normalized,
                        independent="photo-publisher-query",
                    )
                )
        if query.get("set"):
            set_value = query["set"][0]
            match = re.search(
                r"(?:^|[.]|album[_-]?id[=.])(\d{5,25})",
                set_value,
                re.I,
            )
            if match:
                album_id = match.group(1)
                result.append(
                    Evidence(
                        value=album_id,
                        role="ALBUM_ID",
                        source=source,
                        weight=100,
                        key="photo.php?set",
                        url=shape.normalized,
                        independent="album-query",
                    )
                )
    # --------------------------------------------------------
    # story.php
    # --------------------------------------------------------
    if shape.kind == "STORY":
        if query.get("story_fbid"):
            story_id = clean_id(
                query["story_fbid"][0]
            )
            if plausible_object_id(story_id):
                result.append(
                    Evidence(
                        value=story_id,
                        role="STORY_ID",
                        source=source,
                        weight=125,
                        key="story_fbid",
                        url=shape.normalized,
                        independent="story-query",
                    )
                )
        if query.get("id"):
            uid = clean_id(
                query["id"][0]
            )
            if plausible_uid(uid):
                result.append(
                    Evidence(
                        value=uid,
                        role="USER_CANDIDATE",
                        source=source,
                        weight=85,
                        key="story.php?id",
                        url=shape.normalized,
                        independent="story-publisher-query",
                    )
                )
    # --------------------------------------------------------
    # watch?v=
    # --------------------------------------------------------
    if shape.kind == "VIDEO":
        if query.get("v"):
            video_id = clean_id(
                query["v"][0]
            )
            if plausible_object_id(video_id):
                result.append(
                    Evidence(
                        value=video_id,
                        role="VIDEO_ID",
                        source=source,
                        weight=125,
                        key="watch?v",
                        url=shape.normalized,
                        independent="video-query",
                    )
                )
    # --------------------------------------------------------
    # permalink
    # --------------------------------------------------------
    if query.get("story_fbid"):
        post_id = clean_id(
            query["story_fbid"][0]
        )
        if plausible_object_id(post_id):
            result.append(
                Evidence(
                    value=post_id,
                    role="POST_ID",
                    source=source,
                    weight=120,
                    key="story_fbid",
                    url=shape.normalized,
                    independent="post-query",
                )
            )
    # --------------------------------------------------------
    # generic query media
    # --------------------------------------------------------
    for key, role, weight in (
        ("video_id", "VIDEO_ID", 120),
        ("photo_id", "PHOTO_ID", 120),
        ("media_fbid", "MEDIA_FBID", 110),
        ("post_id", "POST_ID", 110),
    ):
        if query.get(key):
            value = clean_id(
                query[key][0]
            )
            if plausible_object_id(value):
                result.append(
                    Evidence(
                        value=value,
                        role=role,
                        source=source,
                        weight=weight,
                        key=key,
                        url=shape.normalized,
                        independent=f"query-{key}",
                    )
                )
    return result
# ============================================================
# FACEBOOK URL EVIDENCE
# ============================================================
def extract_url_evidence(
    url: str,
    source: str,
) -> List[Evidence]:
    shape = parse_shape(url)
    result = add_url_evidence(
        shape,
        source,
    )
    segments = [
        x
        for x in shape.path.split("/")
        if x
    ]
    lower = [
        x.lower()
        for x in segments
    ]
    # --------------------------------------------------------
    # groups
    # --------------------------------------------------------
    if "groups" in lower:
        i = lower.index("groups")
        if i + 1 < len(segments):
            gid = clean_id(
                segments[i + 1]
            )
            if plausible_object_id(gid):
                result.append(
                    Evidence(
                        value=gid,
                        role="GROUP_ID",
                        source=source,
                        weight=130,
                        key="groups-route",
                        url=url,
                        independent="group-route",
                    )
                )
    # --------------------------------------------------------
    # pages
    # --------------------------------------------------------
    if "pages" in lower:
        i = lower.index("pages")
        if i + 2 < len(segments):
            page_id = clean_id(
                segments[i + 2]
            )
            if plausible_object_id(page_id):
                result.append(
                    Evidence(
                        value=page_id,
                        role="PAGE_ID",
                        source=source,
                        weight=135,
                        key="pages-route",
                        url=url,
                        independent="page-route",
                    )
                )
    # --------------------------------------------------------
    # reel
    # --------------------------------------------------------
    for marker in (
        "reel",
        "reels",
    ):
        if marker in lower:
            i = lower.index(marker)
            if i + 1 < len(segments):
                value = clean_id(
                    segments[i + 1]
                )
                if plausible_object_id(value):
                    result.append(
                        Evidence(
                            value=value,
                            role="REEL_ID",
                            source=source,
                            weight=130,
                            key=f"{marker}-route",
                            url=url,
                            independent="reel-route",
                        )
                    )
    # --------------------------------------------------------
    # posts
    # --------------------------------------------------------
    if "posts" in lower:
        i = lower.index("posts")
        if i + 1 < len(segments):
            value = segments[i + 1]
            if value.startswith(
                "pfbid"
            ):
                result.append(
                    Evidence(
                        value=value,
                        role="POST_TOKEN",
                        source=source,
                        weight=120,
                        key="pfbid",
                        url=url,
                        token=value,
                        independent="post-token-route",
                    )
                )
            else:
                numeric = clean_id(value)
                if plausible_object_id(
                    numeric
                ):
                    result.append(
                        Evidence(
                            value=numeric,
                            role="POST_ID",
                            source=source,
                            weight=135,
                            key="posts-route",
                            url=url,
                            independent="post-route",
                        )
                    )
    # --------------------------------------------------------
    # p/
    # --------------------------------------------------------
    if "p" in lower:
        i = lower.index("p")
        if i + 1 < len(segments):
            value = segments[i + 1]
            if value.startswith(
                "pfbid"
            ):
                result.append(
                    Evidence(
                        value=value,
                        role="POST_TOKEN",
                        source=source,
                        weight=120,
                        key="p-pfbid",
                        url=url,
                        token=value,
                        independent="p-route",
                    )
                )
            else:
                numeric = clean_id(value)
                if plausible_object_id(
                    numeric
                ):
                    result.append(
                        Evidence(
                            value=numeric,
                            role="POST_ID",
                            source=source,
                            weight=125,
                            key="p-route",
                            url=url,
                            independent="p-route",
                        )
                    )
    # --------------------------------------------------------
    # video
    # --------------------------------------------------------
    for marker in (
        "video",
        "videos",
    ):
        if marker in lower:
            i = lower.index(marker)
            if i + 1 < len(segments):
                numeric = clean_id(
                    segments[i + 1]
                )
                if plausible_object_id(
                    numeric
                ):
                    result.append(
                        Evidence(
                            value=numeric,
                            role="VIDEO_ID",
                            source=source,
                            weight=130,
                            key=f"{marker}-route",
                            url=url,
                            independent="video-route",
                        )
                    )
    # --------------------------------------------------------
    # photos
    # --------------------------------------------------------
    if "photos" in lower:
        i = lower.index("photos")
        if i + 1 < len(segments):
            numeric = clean_id(
                segments[-1]
            )
            if plausible_object_id(
                numeric
            ):
                result.append(
                    Evidence(
                        value=numeric,
                        role="PHOTO_ID",
                        source=source,
                        weight=125,
                        key="photos-route",
                        url=url,
                        independent="photo-route",
                    )
                )
    # --------------------------------------------------------
    # stories
    # --------------------------------------------------------
    if "stories" in lower:
        numeric = clean_id(
            segments[-1]
        )
        if plausible_object_id(
            numeric
        ):
            result.append(
                Evidence(
                    value=numeric,
                    role="STORY_ID",
                    source=source,
                    weight=125,
                    key="stories-route",
                    url=url,
                    independent="story-route",
                )
            )
    return result
# ============================================================
# JSON-LD PROFILE EVIDENCE
# ============================================================
def extract_jsonld_profile_evidence(
    jsonld: List[Any],
    source: str,
    url: str,
) -> List[Evidence]:
    result = []
    def inspect(
        obj: Any,
        parent: str = "",
    ):
        if isinstance(obj, dict):
            obj_type = str(
                obj.get("@type", "")
            ).lower()
            # ------------------------------------------------
            # Person
            # ------------------------------------------------
            if "person" in obj_type:
                candidate_url = (
                    obj.get("url")
                    or obj.get("@id")
                )
                if isinstance(
                    candidate_url,
                    str,
                ):
                    parsed = urlparse(
                        candidate_url
                    )
                    if (
                        is_facebook_host(
                            parsed.netloc
                        )
                    ):
                        shape = parse_shape(
                            normalize_url(
                                candidate_url
                            )
                        )
                        if shape.kind == "PROFILE":
                            if (
                                shape.query.get("id")
                            ):
                                uid = clean_id(
                                    shape.query["id"][0]
                                )
                                if plausible_uid(uid):
                                    result.append(
                                        Evidence(
                                            value=uid,
                                            role="USER_CANDIDATE",
                                            source=source,
                                            weight=105,
                                            key="jsonld-person-url",
                                            url=url,
                                            independent="jsonld-person-url",
                                        )
                                    )
            # ------------------------------------------------
            # author
            # ------------------------------------------------
            author = obj.get("author")
            if isinstance(author, dict):
                author_url = (
                    author.get("url")
                    or author.get("@id")
                )
                if isinstance(
                    author_url,
                    str,
                ):
                    parsed = urlparse(
                        author_url
                    )
                    if is_facebook_host(
                        parsed.netloc
                    ):
                        author_shape = parse_shape(
                            normalize_url(
                                author_url
                            )
                        )
                        if (
                            author_shape.kind
                            == "PROFILE"
                        ):
                            if (
                                author_shape.query.get(
                                    "id"
                                )
                            ):
                                uid = clean_id(
                                    author_shape.query[
                                        "id"
                                    ][0]
                                )
                                if plausible_uid(uid):
                                    result.append(
                                        Evidence(
                                            value=uid,
                                            role="USER_CANDIDATE",
                                            source=source,
                                            weight=110,
                                            key="jsonld-author-url",
                                            url=url,
                                            independent="jsonld-author-url",
                                        )
                                    )
            # recurse
            for key, value in obj.items():
                inspect(
                    value,
                    f"{parent}/{key}",
                )
        elif isinstance(obj, list):
            for item in obj:
                inspect(
                    item,
                    parent,
                )
    for item in jsonld:
        inspect(item)
    return result
# ============================================================
# PROFILE URL DISCOVERY
# ============================================================
def discover_profile_urls(
    snapshot: Snapshot,
    base_url: str,
) -> List[str]:
    candidates: List[str] = []
    def add(value: Optional[str]):
        if not value:
            return
        value = clean_text(value)
        value = urljoin(
            base_url,
            value,
        )
        value = normalize_url(
            value
        )
        parsed = urlparse(value)
        if not is_facebook_host(
            parsed.netloc
        ):
            return
        shape = parse_shape(value)
        if shape.kind == "PROFILE":
            if value not in candidates:
                candidates.append(value)
    add(snapshot.canonical)
    add(
        snapshot.og.get(
            "og:url"
        )
    )
    for key in (
        "article:author",
        "author",
    ):
        add(
            snapshot.meta.get(key)
        )
    for link in snapshot.links:
        add(link)
        if len(candidates) >= 20:
            break
    # JSON-LD
    def scan_json(obj: Any):
        if isinstance(obj, dict):
            for key in (
                "url",
                "@id",
            ):
                value = obj.get(key)
                if isinstance(
                    value,
                    str,
                ):
                    add(value)
            author = obj.get(
                "author"
            )
            if isinstance(
                author,
                dict,
            ):
                add(
                    author.get("url")
                )
                add(
                    author.get("@id")
                )
            for value in obj.values():
                scan_json(value)
        elif isinstance(obj, list):
            for item in obj:
                scan_json(item)
    for item in snapshot.jsonld:
        scan_json(item)
    return candidates[:20]
# ============================================================
# PROFILE PAGE CLASSIFICATION
# ============================================================
def classify_profile_snapshot(
    snapshot: Snapshot,
    requested_url: str,
) -> Tuple[
    Optional[str],
    Optional[str],
    Optional[str],
    List[Evidence],
]:
    evidences: List[Evidence] = []
    all_text = (
        snapshot.html
        + "\n"
        + snapshot.title
        + "\n"
        + "\n".join(
            snapshot.meta.values()
        )
    )
    evidences.extend(
        extract_raw_semantic_evidence(
            all_text,
            "profile-html",
            snapshot.final_url,
        )
    )
    evidences.extend(
        extract_special_evidence(
            all_text,
            "profile-special",
            snapshot.final_url,
        )
    )
    evidences.extend(
        extract_json_evidence(
            snapshot.jsonld,
            "profile-jsonld",
            snapshot.final_url,
        )
    )
    # --------------------------------------------------------
    # Determine explicit page/group evidence
    # --------------------------------------------------------
    page_ids = [
        e.value
        for e in evidences
        if e.role == "PAGE_ID"
    ]
    group_ids = [
        e.value
        for e in evidences
        if e.role == "GROUP_ID"
    ]
    user_ids = [
        e.value
        for e in evidences
        if e.role == "USER_CANDIDATE"
        and plausible_uid(e.value)
    ]
    page_id = (
        max(
            set(page_ids),
            key=page_ids.count,
        )
        if page_ids
        else None
    )
    group_id = (
        max(
            set(group_ids),
            key=group_ids.count,
        )
        if group_ids
        else None
    )
    uid = None
    if user_ids:
        counts = {}
        for value in user_ids:
            counts[value] = (
                counts.get(value, 0)
                + 1
            )
        uid = max(
            counts,
            key=counts.get,
        )
    publisher_type = None
    if group_id:
        publisher_type = "GROUP"
    elif page_id:
        publisher_type = "PAGE"
    elif uid:
        publisher_type = "USER"
    return (
        uid,
        page_id,
        publisher_type,
        evidences,
    )
# ============================================================
# CANDIDATE BUILD
# ============================================================
def build_candidates(
    evidences: List[Evidence],
) -> Dict[str, Candidate]:
    candidates: Dict[
        str,
        Candidate,
    ] = {}
    for evidence in evidences:
        if (
            evidence.role
            != "USER_CANDIDATE"
        ):
            continue
        value = evidence.value
        if not plausible_uid(value):
            continue
        candidate = candidates.get(
            value
        )
        if candidate is None:
            candidate = Candidate(
                value=value
            )
            candidates[value] = candidate
        candidate.evidences.append(
            evidence
        )
        candidate.roles.add(
            evidence.role
        )
        candidate.sources.add(
            evidence.source
        )
        if evidence.independent:
            candidate.independent.add(
                evidence.independent
            )
        candidate.score += (
            evidence.weight
        )
    return candidates
# ============================================================
# PROFILE VERIFICATION
# ============================================================
async def verify_candidate(
    candidate: Candidate,
    profile_urls: List[str],
) -> Candidate:
    if not profile_urls:
        return candidate
    # --------------------------------------------------------
    # Limit network verification
    # --------------------------------------------------------
    urls = profile_urls[
        :MAX_PROFILE_CHECKS
    ]
    for profile_url in urls:
        try:
            snapshot = await fetch_html(
                profile_url
            )
        except Exception:
            continue
        candidate_text = (
            snapshot.html
            + "\n"
            + snapshot.title
            + "\n"
            + "\n".join(
                snapshot.meta.values()
            )
        )
        # Exact UID appears in strong profile context
        if re.search(
            rf"""
            (?:
                user_id|
                profile_id|
                owner_id|
                publisher_id|
                author_id|
                actor_id|
                data-user-id
            )
            \D{{0,100}}
            {re.escape(candidate.value)}
            """,
            candidate_text,
            re.I | re.X,
        ):
            candidate.verified = True
            candidate.profile_match = True
            candidate.score += 55
            candidate.independent.add(
                "profile-page"
            )
        # profile.php?id=UID
        parsed = urlparse(
            snapshot.final_url
        )
        query = parse_qs(
            parsed.query
        )
        if query.get("id"):
            profile_id = clean_id(
                query["id"][0]
            )
            if (
                profile_id
                == candidate.value
            ):
                candidate.verified = True
                candidate.profile_match = True
                candidate.score += 70
                candidate.independent.add(
                    "profile-query"
                )
        # canonical
        if snapshot.canonical:
            cshape = parse_shape(
                snapshot.canonical
            )
            if (
                cshape.kind == "PROFILE"
                and cshape.query.get("id")
            ):
                profile_id = clean_id(
                    cshape.query["id"][0]
                )
                if (
                    profile_id
                    == candidate.value
                ):
                    candidate.verified = True
                    candidate.profile_match = True
                    candidate.score += 75
                    candidate.independent.add(
                        "profile-canonical"
                    )
        # OG URL
        og_url = snapshot.og.get(
            "og:url"
        )
        if og_url:
            og_url = normalize_url(
                urljoin(
                    snapshot.final_url,
                    og_url,
                )
            )
            og_shape = parse_shape(
                og_url
            )
            if (
                og_shape.kind == "PROFILE"
                and og_shape.query.get("id")
            ):
                profile_id = clean_id(
                    og_shape.query["id"][0]
                )
                if (
                    profile_id
                    == candidate.value
                ):
                    candidate.verified = True
                    candidate.profile_match = True
                    candidate.score += 65
                    candidate.independent.add(
                        "og-profile"
                    )
        # Stop once strongly verified
        if (
            candidate.verified
            and len(
                candidate.independent
            ) >= 2
        ):
            break
    return candidate
# ============================================================
# CANDIDATE CORRELATION
# ============================================================
def score_candidate_context(
    candidate: Candidate,
    snapshot: Snapshot,
    shape: URLShape,
) -> Candidate:
    text = snapshot.html
    # --------------------------------------------------------
    # Route username correlation
    # --------------------------------------------------------
    if shape.username:
        username = re.escape(
            shape.username
        )
        profile_patterns = [
            rf"/{username}(?:[/?\"'])",
            rf'"{username}"',
            rf"@{username}\b",
        ]
        for pattern in profile_patterns:
            if re.search(
                pattern,
                text,
                re.I,
            ):
                candidate.username_match = True
                candidate.score += 18
                candidate.independent.add(
                    "route-username"
                )
                break
    # --------------------------------------------------------
    # Candidate appears near object token
    # --------------------------------------------------------
    tokens = []
    if shape.post_token:
        tokens.append(
            shape.post_token
        )
    if shape.share_token:
        tokens.append(
            shape.share_token
        )
    for token in tokens:
        if not token:
            continue
        position = text.find(token)
        if position < 0:
            continue
        start = max(
            0,
            position - 2500,
        )
        end = min(
            len(text),
            position + 2500,
        )
        context = text[
            start:end
        ]
        if candidate.value in context:
            candidate.score += 20
            candidate.independent.add(
                "object-context"
            )
    return candidate
# ============================================================
# CONFLICT DETECTION
# ============================================================
def detect_candidate_conflicts(
    candidates: Dict[str, Candidate],
    page_ids: Set[str],
    group_ids: Set[str],
) -> None:
    for candidate in candidates.values():
        if candidate.value in page_ids:
            candidate.page_conflict = True
            candidate.score -= 85
        if candidate.value in group_ids:
            candidate.group_conflict = True
            candidate.score -= 100
    ordered = sorted(
        candidates.values(),
        key=lambda x: x.score,
        reverse=True,
    )
    if len(ordered) < 2:
        return
    top = ordered[0]
    second = ordered[1]
    if (
        top.score > 0
        and second.score > 0
        and abs(
            top.score
            - second.score
        )
        < 35
    ):
        top.conflict = True
        second.conflict = True
# ============================================================
# OBJECT SELECTION
# ============================================================
def select_object_id(
    evidences: List[Evidence],
    role: str,
) -> Optional[str]:
    items = [
        e
        for e in evidences
        if e.role == role
        and plausible_object_id(
            e.value
        )
    ]
    if not items:
        return None
    groups: Dict[str, List[Evidence]] = {}
    for item in items:
        groups.setdefault(
            item.value,
            [],
        ).append(item)
    scored = []
    for value, values in groups.items():
        score = sum(
            x.weight
            for x in values
        )
        independent = len(
            {
                x.independent
                for x in values
                if x.independent
            }
        )
        score += independent * 20
        scored.append(
            (
                score,
                value,
            )
        )
    scored.sort(
        reverse=True
    )
    return scored[0][1]
# ============================================================
# ENTITY TYPE
# ============================================================
def determine_entity_type(
    shape: URLShape,
    page_id: Optional[str],
    group_id: Optional[str],
    publisher_type: Optional[str],
) -> Tuple[str, str]:
    kind = shape.kind
    # Group has absolute priority
    if group_id:
        if kind in {
            "POST",
            "GROUP_POST",
        }:
            return (
                "GROUP_POST",
                "GROUP",
            )
        return (
            "GROUP",
            "GROUP",
        )
    # Page
    if page_id:
        if kind in {
            "POST",
            "PAGE_POST",
        }:
            return (
                "PAGE_POST",
                "PAGE",
            )
        return (
            "PAGE",
            "PAGE",
        )
    # Media
    if kind == "REEL":
        return "REEL", publisher_type or "UNKNOWN"
    if kind == "VIDEO":
        return "VIDEO", publisher_type or "UNKNOWN"
    if kind == "PHOTO":
        return "PHOTO", publisher_type or "UNKNOWN"
    if kind == "STORY":
        return "STORY", publisher_type or "UNKNOWN"
    if kind == "PROFILE":
        return "PROFILE", publisher_type or "USER"
    if kind == "POST":
        if publisher_type == "PAGE":
            return "PAGE_POST", "PAGE"
        if publisher_type == "GROUP":
            return "GROUP_POST", "GROUP"
        return "USER_POST", "USER"
    return (
        kind,
        publisher_type or "UNKNOWN",
    )
# ============================================================
# CONFIDENCE
# ============================================================
def calculate_confidence(
    candidate: Optional[Candidate],
    status: str,
) -> int:
    if not candidate:
        return 0
    score = candidate.score
    independent = len(
        candidate.independent
    )
    sources = len(
        candidate.sources
    )
    confidence = 0
    if score >= 330:
        confidence = 99
    elif score >= 280:
        confidence = 97
    elif score >= 230:
        confidence = 95
    elif score >= 190:
        confidence = 92
    elif score >= 155:
        confidence = 88
    elif score >= 125:
        confidence = 82
    elif score >= 100:
        confidence = 75
    elif score >= 80:
        confidence = 68
    else:
        confidence = 55
    confidence += min(
        independent * 2,
        6,
    )
    confidence += min(
        max(sources - 1, 0),
        3,
    )
    if candidate.conflict:
        confidence -= 18
    if candidate.page_conflict:
        confidence -= 20
    if candidate.group_conflict:
        confidence -= 25
    if status == "VERIFIED":
        confidence = max(
            confidence,
            92,
        )
    elif status == "LIKELY":
        confidence = min(
            confidence,
            89,
        )
    else:
        confidence = min(
            confidence,
            69,
        )
    return max(
        0,
        min(
            99,
            int(confidence),
        ),
    )
# ============================================================
# STATUS
# ============================================================
def determine_status(
    candidate: Optional[Candidate],
) -> str:
    if not candidate:
        return "UNVERIFIED"
    if candidate.conflict:
        return "CONFLICT"
    if candidate.verified:
        return "VERIFIED"
    strong = sum(
        1
        for e in candidate.evidences
        if e.weight >= 90
    )
    independent = len(
        candidate.independent
    )
    if (
        candidate.score >= 145
        and strong >= 2
        and independent >= 2
    ):
        return "LIKELY"
    return "UNVERIFIED"
# ============================================================
# EVIDENCE SUMMARY
# ============================================================
def evidence_summary(
    evidences: List[Evidence],
) -> List[str]:
    result = []
    seen = set()
    priority = sorted(
        evidences,
        key=lambda e: e.weight,
        reverse=True,
    )
    for evidence in priority:
        if evidence.role == "USER_CANDIDATE":
            label = evidence.key or (
                evidence.source
            )
            if label in seen:
                continue
            seen.add(label)
            result.append(
                f"{label}: {evidence.value}"
            )
            if len(result) >= 10:
                break
    return result
# ============================================================
# BASE64 CONTEXT DECODER
# ============================================================
def decode_base64_candidate(
    value: str,
) -> Optional[str]:
    if not value:
        return None
    if len(value) < 12:
        return None
    if not re.fullmatch(
        r"[A-Za-z0-9_\-+/=]+",
        value,
    ):
        return None
    try:
        raw = value.replace(
            "-",
            "+",
        ).replace(
            "_",
            "/",
        )
        raw += "=" * (
            (-len(raw)) % 4
        )
        decoded = base64.b64decode(
            raw,
            validate=False,
        )
        text = decoded.decode(
            "utf-8",
            errors="ignore",
        )
        if not text:
            return None
        # Conservative:
        # only accept if Facebook-ish content exists
        if any(
            marker in text.lower()
            for marker in (
                "facebook",
                "user_id",
                "profile_id",
                "actor_id",
                "owner_id",
                "pfbid",
                "fbid",
            )
        ):
            return text
    except (
        ValueError,
        binascii.Error,
        UnicodeError,
    ):
        pass
    return None
# ============================================================
# MAIN RESOLVER
# ============================================================
async def resolve_facebook_url(
    input_url: str,
) -> ResolveResult:
    started = time.perf_counter()
    input_url = normalize_url(
        input_url
    )
    initial_shape = parse_shape(
        input_url
    )
    warnings: List[str] = []
    all_evidence: List[Evidence] = []
    # --------------------------------------------------------
    # Initial URL evidence
    # --------------------------------------------------------
    all_evidence.extend(
        extract_url_evidence(
            input_url,
            "input-url",
        )
    )
    # --------------------------------------------------------
    # Fetch
    # --------------------------------------------------------
    try:
        snapshot = await fetch_html(
            input_url
        )
    except Exception as exc:
        elapsed = (
            time.perf_counter()
            - started
        )
        return ResolveResult(
            input_url=input_url,
            resolved_url=input_url,
            canonical_url=None,
            url_type=initial_shape.kind,
            entity_type=initial_shape.kind,
            publisher_type=None,
            username=initial_shape.username,
            user_uid=None,
            page_uid=None,
            group_id=None,
            post_id=None,
            reel_id=None,
            video_id=None,
            photo_id=None,
            story_id=None,
            album_id=None,
            publisher_id=None,
            title=None,
            status="ERROR",
            confidence=0,
            evidence_count=0,
            warnings=[
                f"HTTP error: {type(exc).__name__}: {exc}"
            ],
            evidence_summary=[],
            elapsed=elapsed,
        )
    # --------------------------------------------------------
    # Redirect
    # --------------------------------------------------------
    if (
        snapshot.final_url
        != input_url
    ):
        warnings.append(
            "URL đã được Facebook redirect."
        )
    final_shape = parse_shape(
        snapshot.final_url
    )
    # --------------------------------------------------------
    # Canonical
    # --------------------------------------------------------
    canonical = snapshot.canonical
    if canonical:
        cshape = parse_shape(
            canonical
        )
        if (
            cshape.kind != "UNKNOWN"
            or final_shape.kind == "UNKNOWN"
        ):
            final_shape = cshape
        all_evidence.extend(
            extract_url_evidence(
                canonical,
                "canonical-url",
            )
        )
    # --------------------------------------------------------
    # OG URL
    # --------------------------------------------------------
    og_url = snapshot.og.get(
        "og:url"
    )
    if og_url:
        og_url = normalize_url(
            urljoin(
                snapshot.final_url,
                og_url,
            )
        )
        all_evidence.extend(
            extract_url_evidence(
                og_url,
                "og-url",
            )
        )
        og_shape = parse_shape(
            og_url
        )
        if (
            og_shape.kind != "UNKNOWN"
        ):
            final_shape = og_shape
    # --------------------------------------------------------
    # HTML semantic
    # --------------------------------------------------------
    html_text = snapshot.html
    all_evidence.extend(
        extract_raw_semantic_evidence(
            html_text,
            "html-semantic",
            snapshot.final_url,
            token=(
                initial_shape.post_token
                or initial_shape.share_token
            ),
        )
    )
    all_evidence.extend(
        extract_special_evidence(
            html_text,
            "html-special",
            snapshot.final_url,
            token=(
                initial_shape.post_token
                or initial_shape.share_token
            ),
        )
    )
    # --------------------------------------------------------
    # JSON-LD
    # --------------------------------------------------------
    all_evidence.extend(
        extract_json_evidence(
            snapshot.jsonld,
            "jsonld-semantic",
            snapshot.final_url,
        )
    )
    all_evidence.extend(
        extract_jsonld_profile_evidence(
            snapshot.jsonld,
            "jsonld-profile",
            snapshot.final_url,
        )
    )
    # --------------------------------------------------------
    # Discovered Facebook URLs
    # --------------------------------------------------------
    discovered_urls = []
    for link in snapshot.links:
        absolute = normalize_url(
            urljoin(
                snapshot.final_url,
                link,
            )
        )
        parsed = urlparse(
            absolute
        )
        if not is_facebook_host(
            parsed.netloc
        ):
            continue
        discovered_urls.append(
            absolute
        )
        if len(
            discovered_urls
        ) >= MAX_DISCOVERED_LINKS:
            break
    # OG + canonical
    if canonical:
        discovered_urls.append(
            canonical
        )
    if og_url:
        discovered_urls.append(
            og_url
        )
    # --------------------------------------------------------
    # URL evidence from discovered links
    # --------------------------------------------------------
    seen_urls = set()
    for discovered in discovered_urls:
        if discovered in seen_urls:
            continue
        seen_urls.add(
            discovered
        )
        all_evidence.extend(
            extract_url_evidence(
                discovered,
                "discovered-url",
            )
        )
    # --------------------------------------------------------
    # Opaque token
    # --------------------------------------------------------
    object_token = (
        initial_shape.post_token
        or initial_shape.share_token
        or final_shape.post_token
        or final_shape.share_token
    )
    if object_token:
        # Keep pfbid/share token as object evidence,
        # never as UID.
        all_evidence.append(
            Evidence(
                value=object_token,
                role="OBJECT_TOKEN",
                source="url-token",
                weight=100,
                key="object-token",
                url=snapshot.final_url,
                token=object_token,
                independent="object-token",
            )
        )
    # --------------------------------------------------------
    # Profile URLs
    # --------------------------------------------------------
    profile_urls = discover_profile_urls(
        snapshot,
        snapshot.final_url,
    )
    # --------------------------------------------------------
    # PAGE/GROUP
    # --------------------------------------------------------
    page_ids = {
        e.value
        for e in all_evidence
        if e.role == "PAGE_ID"
    }
    group_ids = {
        e.value
        for e in all_evidence
        if e.role == "GROUP_ID"
    }
    page_uid = None
    if page_ids:
        # Prefer highest evidence score
        page_uid = select_object_id(
            all_evidence,
            "PAGE_ID",
        )
    group_id = None
    if group_ids:
        group_id = select_object_id(
            all_evidence,
            "GROUP_ID",
        )
    # --------------------------------------------------------
    # OBJECT IDs
    # --------------------------------------------------------
    post_id = select_object_id(
        all_evidence,
        "POST_ID",
    )
    if not post_id:
        # POST token can still be output
        token_items = [
            e.value
            for e in all_evidence
            if e.role == "POST_TOKEN"
        ]
        if token_items:
            post_id = token_items[0]
    reel_id = select_object_id(
        all_evidence,
        "REEL_ID",
    )
    video_id = select_object_id(
        all_evidence,
        "VIDEO_ID",
    )
    photo_id = select_object_id(
        all_evidence,
        "PHOTO_ID",
    )
    story_id = select_object_id(
        all_evidence,
        "STORY_ID",
    )
    album_id = select_object_id(
        all_evidence,
        "ALBUM_ID",
    )
    # --------------------------------------------------------
    # MEDIA_FBID
    # --------------------------------------------------------
    if not photo_id:
        media_items = [
            e
            for e in all_evidence
            if e.role == "MEDIA_FBID"
        ]
        if media_items:
            media_items.sort(
                key=lambda x: x.weight,
                reverse=True,
            )
            if final_shape.kind == "PHOTO":
                photo_id = (
                    media_items[0].value
                )
    # --------------------------------------------------------
    # Candidate users
    # --------------------------------------------------------
    candidates = build_candidates(
        all_evidence
    )
    # --------------------------------------------------------
    # Context scoring
    # --------------------------------------------------------
    for candidate in candidates.values():
        score_candidate_context(
            candidate,
            snapshot,
            final_shape,
        )
    # --------------------------------------------------------
    # Remove IDs explicitly known as page/group
    # --------------------------------------------------------
    detect_candidate_conflicts(
        candidates,
        page_ids,
        group_ids,
    )
    # --------------------------------------------------------
    # Profile verification
    # --------------------------------------------------------
    ordered_candidates = sorted(
        candidates.values(),
        key=lambda x: x.score,
        reverse=True,
    )
    for candidate in ordered_candidates[
        :MAX_PROFILE_CHECKS
    ]:
        await verify_candidate(
            candidate,
            profile_urls,
        )
    # Re-sort after verification
    ordered_candidates = sorted(
        candidates.values(),
        key=lambda x: x.score,
        reverse=True,
    )
    best_candidate = (
        ordered_candidates[0]
        if ordered_candidates
        else None
    )
    # --------------------------------------------------------
    # Publisher
    # --------------------------------------------------------
    publisher_type = None
    if group_id:
        publisher_type = "GROUP"
    elif page_uid:
        publisher_type = "PAGE"
    elif best_candidate:
        publisher_type = "USER"
    # --------------------------------------------------------
    # Username
    # --------------------------------------------------------
    username = (
        final_shape.username
        or initial_shape.username
    )
    # Search discovered profile URL
    if not username:
        for profile_url in profile_urls:
            profile_shape = parse_shape(
                profile_url
            )
            if profile_shape.username:
                username = (
                    profile_shape.username
                )
                break
    # --------------------------------------------------------
    # Publisher UID
    # --------------------------------------------------------
    user_uid = None
    status = "UNVERIFIED"
    if (
        best_candidate
        and publisher_type == "USER"
    ):
        status = determine_status(
            best_candidate
        )
        # Strict rule:
        # Only expose UID as verified/likely.
        if status in {
            "VERIFIED",
            "LIKELY",
        }:
            user_uid = (
                best_candidate.value
            )
    # --------------------------------------------------------
    # Publisher ID
    # --------------------------------------------------------
    publisher_id = None
    if publisher_type == "USER":
        publisher_id = user_uid
    elif publisher_type == "PAGE":
        publisher_id = page_uid
    elif publisher_type == "GROUP":
        publisher_id = group_id
    # --------------------------------------------------------
    # Entity type
    # --------------------------------------------------------
    url_type, entity_type = (
        determine_entity_type(
            final_shape,
            page_uid,
            group_id,
            publisher_type,
        )
    )
    # --------------------------------------------------------
    # Special publisher correction
    # --------------------------------------------------------
    if (
        url_type == "GROUP_POST"
        and best_candidate
        and user_uid
    ):
        publisher_type = "USER"
        publisher_id = user_uid
    # --------------------------------------------------------
    # Group post:
    # group ID never becomes user UID
    # --------------------------------------------------------
    if group_id:
        if (
            user_uid
            and user_uid == group_id
        ):
            user_uid = None
            publisher_id = None
            warnings.append(
                "UID candidate trùng GROUP ID nên đã loại."
            )
    # --------------------------------------------------------
    # Page:
    # page UID never becomes user UID
    # --------------------------------------------------------
    if page_uid:
        if (
            user_uid
            and user_uid == page_uid
        ):
            user_uid = None
            if publisher_type == "USER":
                publisher_type = "PAGE"
            publisher_id = page_uid
            status = "UNVERIFIED"
            warnings.append(
                "UID candidate trùng PAGE ID nên đã loại."
            )
    # --------------------------------------------------------
    # If object is explicitly Page, user UID is independent
    # only.
    # --------------------------------------------------------
    if (
        page_uid
        and not (
            best_candidate
            and best_candidate.value
            != page_uid
            and best_candidate.verified
        )
    ):
        if final_shape.kind in {
            "PAGE",
            "PAGE_POST",
        }:
            user_uid = None
            if publisher_type == "USER":
                publisher_type = "PAGE"
            publisher_id = page_uid
    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------
    confidence = calculate_confidence(
        best_candidate,
        status,
    )
    # --------------------------------------------------------
    # Evidence count
    # --------------------------------------------------------
    independent_evidence = {
        (
            e.value,
            e.role,
            e.independent,
        )
        for e in all_evidence
        if e.independent
    }
    evidence_count = len(
        independent_evidence
    )
    # --------------------------------------------------------
    # Warnings
    # --------------------------------------------------------
    if not user_uid:
        if final_shape.kind in {
            "POST",
            "USER_POST",
            "REEL",
            "VIDEO",
            "PHOTO",
            "STORY",
        }:
            warnings.append(
                "Không đủ bằng chứng độc lập để xác minh USER UID."
            )
    if object_token:
        warnings.append(
            "Opaque token/pfbid không được tự giải mã thành UID."
        )
    # --------------------------------------------------------
    # Elapsed
    # --------------------------------------------------------
    elapsed = (
        time.perf_counter()
        - started
    )
    # --------------------------------------------------------
    # Title
    # --------------------------------------------------------
    title = (
        snapshot.og.get(
            "og:title"
        )
        or snapshot.title
        or None
    )
    return ResolveResult(
        input_url=input_url,
        resolved_url=snapshot.final_url,
        canonical_url=canonical,
        url_type=url_type,
        entity_type=entity_type,
        publisher_type=publisher_type,
        username=username,
        user_uid=user_uid,
        page_uid=page_uid,
        group_id=group_id,
        post_id=post_id,
        reel_id=reel_id,
        video_id=video_id,
        photo_id=photo_id,
        story_id=story_id,
        album_id=album_id,
        publisher_id=publisher_id,
        title=title,
        status=status,
        confidence=confidence,
        evidence_count=evidence_count,
        warnings=list(
            dict.fromkeys(warnings)
        ),
        evidence_summary=evidence_summary(
            all_evidence
        ),
        elapsed=elapsed,
    )
# ============================================================
# TELEGRAM ESCAPE
# ============================================================
def tg_escape(value: Any) -> str:
    value = clean_text(value)
    # HTML parse mode
    return (
        value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
def truncate(
    value: Any,
    maximum: int = 350,
) -> str:
    value = clean_text(value)
    if len(value) <= maximum:
        return value
    return (
        value[: maximum - 3]
        + "..."
    )
# ============================================================
# RESULT FORMATTER
# ============================================================
def format_result(
    result: ResolveResult,
    index: Optional[int] = None,
    total: Optional[int] = None,
) -> str:
    lines = []
    if index is not None and total is not None:
        lines.append(
            f"🔎 <b>FACEBOOK RESOLVER V21</b> "
            f"[{index}/{total}]"
        )
    else:
        lines.append(
            "🔎 <b>FACEBOOK RESOLVER V21</b>"
        )
    lines.append("")
    lines.append(
        f"🌐 <b>URL TYPE:</b> "
        f"{tg_escape(result.url_type)}"
    )
    lines.append(
        f"📦 <b>OBJECT:</b> "
        f"{tg_escape(result.entity_type)}"
    )
    # --------------------------------------------------------
    # Publisher
    # --------------------------------------------------------
    if (
        result.publisher_type
        or result.username
        or result.publisher_id
    ):
        lines.append("")
        lines.append(
            "━━━━━━━━━━━━━━━━━━"
        )
        lines.append(
            "👤 <b>PUBLISHER</b>"
        )
        if result.publisher_type:
            lines.append(
                f"TYPE: "
                f"{tg_escape(result.publisher_type)}"
            )
        if result.username:
            lines.append(
                f"USERNAME: "
                f"@{tg_escape(result.username)}"
            )
        if result.publisher_id:
            if (
                result.publisher_type
                == "PAGE"
            ):
                lines.append(
                    f"PAGE UID: "
                    f"<code>{tg_escape(result.publisher_id)}</code>"
                )
            elif (
                result.publisher_type
                == "GROUP"
            ):
                lines.append(
                    f"GROUP ID: "
                    f"<code>{tg_escape(result.publisher_id)}</code>"
                )
            else:
                lines.append(
                    f"PUBLISHER UID: "
                    f"<code>{tg_escape(result.publisher_id)}</code>"
                )
    # --------------------------------------------------------
    # USER UID
    # --------------------------------------------------------
    if result.user_uid:
        lines.append(
            f"🆔 <b>USER UID:</b> "
            f"<code>{tg_escape(result.user_uid)}</code>"
        )
    elif result.entity_type not in {
        "PAGE",
        "GROUP",
    }:
        lines.append(
            "🆔 <b>USER UID:</b> "
            "<code>NOT VERIFIED</code>"
        )
    # --------------------------------------------------------
    # PAGE
    # --------------------------------------------------------
    if result.page_uid:
        lines.append(
            f"📄 <b>PAGE UID:</b> "
            f"<code>{tg_escape(result.page_uid)}</code>"
        )
    # --------------------------------------------------------
    # GROUP
    # --------------------------------------------------------
    if result.group_id:
        lines.append(
            f"👥 <b>GROUP ID:</b> "
            f"<code>{tg_escape(result.group_id)}</code>"
        )
    # --------------------------------------------------------
    # OBJECT
    # --------------------------------------------------------
    if any(
        (
            result.post_id,
            result.reel_id,
            result.video_id,
            result.photo_id,
            result.story_id,
            result.album_id,
        )
    ):
        lines.append("")
        lines.append(
            "━━━━━━━━━━━━━━━━━━"
        )
        lines.append(
            "📦 <b>OBJECT IDs</b>"
        )
    if result.post_id:
        lines.append(
            f"📝 POST ID: "
            f"<code>{tg_escape(result.post_id)}</code>"
        )
    if result.reel_id:
        lines.append(
            f"🎞 REEL ID: "
            f"<code>{tg_escape(result.reel_id)}</code>"
        )
    if result.video_id:
        lines.append(
            f"🎬 VIDEO ID: "
            f"<code>{tg_escape(result.video_id)}</code>"
        )
    if result.photo_id:
        lines.append(
            f"🖼 PHOTO ID: "
            f"<code>{tg_escape(result.photo_id)}</code>"
        )
    if result.story_id:
        lines.append(
            f"⭕ STORY ID: "
            f"<code>{tg_escape(result.story_id)}</code>"
        )
    if result.album_id:
        lines.append(
            f"💿 ALBUM ID: "
            f"<code>{tg_escape(result.album_id)}</code>"
        )
    # --------------------------------------------------------
    # Title
    # --------------------------------------------------------
    if result.title:
        lines.append("")
        lines.append(
            f"📛 <b>TITLE:</b> "
            f"{tg_escape(truncate(result.title, 350))}"
        )
    # --------------------------------------------------------
    # Verification
    # --------------------------------------------------------
    lines.append("")
    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )
    lines.append(
        "🔐 <b>VERIFICATION</b>"
    )
    lines.append(
        f"STATUS: "
        f"<b>{tg_escape(result.status)}</b>"
    )
    lines.append(
        f"CONFIDENCE: "
        f"<b>{result.confidence}%</b>"
    )
    lines.append(
        f"EVIDENCE: "
        f"{result.evidence_count} independent signals"
    )
    # --------------------------------------------------------
    # Evidence
    # --------------------------------------------------------
    if result.evidence_summary:
        lines.append("")
        lines.append(
            "🔬 <b>UID EVIDENCE</b>"
        )
        for item in result.evidence_summary[:8]:
            lines.append(
                f"• {tg_escape(item)}"
            )
    # --------------------------------------------------------
    # URLs
    # --------------------------------------------------------
    lines.append("")
    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )
    lines.append(
        "🔗 <b>RESOLVED:</b>"
    )
    lines.append(
        f"<code>{tg_escape(result.resolved_url)}</code>"
    )
    if result.canonical_url:
        lines.append(
            "🔗 <b>CANONICAL:</b>"
        )
        lines.append(
            f"<code>{tg_escape(result.canonical_url)}</code>"
        )
    # --------------------------------------------------------
    # Warnings
    # --------------------------------------------------------
    if result.warnings:
        lines.append("")
        lines.append(
            "⚠️ <b>NOTES</b>"
        )
        for warning in result.warnings[:5]:
            lines.append(
                f"• {tg_escape(warning)}"
            )
    lines.append("")
    lines.append(
        f"⏱ TIME: {result.elapsed:.2f}s"
    )
    return "\n".join(lines)
# ============================================================
# SESSION MANAGEMENT
# ============================================================
_ACTIVE_SESSIONS: Dict[
    Tuple[int, int],
    asyncio.Future,
] = {}
_SESSION_LOCK = asyncio.Lock()
async def wait_for_next_message(
    bot,
    event,
    timeout: int = SESSION_TIMEOUT,
):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    key = (
        int(event.chat_id or 0),
        int(event.sender_id or 0),
    )
    async with _SESSION_LOCK:
        old = _ACTIVE_SESSIONS.get(
            key
        )
        if old and not old.done():
            old.cancel()
        _ACTIVE_SESSIONS[key] = future
    async def waiter(new_event):
        try:
            if (
                int(new_event.chat_id or 0)
                != int(event.chat_id or 0)
            ):
                return
            if (
                int(new_event.sender_id or 0)
                != int(event.sender_id or 0)
            ):
                return
            if future.done():
                return
            future.set_result(
                new_event
            )
        except Exception:
            pass
    builder = events.NewMessage(
        chats=event.chat_id
    )
    bot.add_event_handler(
        waiter,
        builder,
    )
    try:
        return await asyncio.wait_for(
            future,
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return None
    except asyncio.CancelledError:
        return None
    finally:
        bot.remove_event_handler(
            waiter,
            builder,
        )
        async with _SESSION_LOCK:
            current = _ACTIVE_SESSIONS.get(
                key
            )
            if current is future:
                _ACTIVE_SESSIONS.pop(
                    key,
                    None,
                )
# ============================================================
# COMMAND DETECTION
# ============================================================
def is_other_command(
    text: str,
) -> bool:
    text = (
        text or ""
    ).strip().lower()
    if not text.startswith("/"):
        return False
    command = (
        text.split(
            None,
            1,
        )[0]
        .split("@", 1)[0]
    )
    return command in COMMAND_PREFIXES
# ============================================================
# TASK MANAGER SAFE HELPERS
# ============================================================
def safe_track_task(
    user_id: Optional[int],
    task_name: str,
):
    try:
        if track_current_task:
            track_current_task(
                user_id,
                task_name,
            )
    except TypeError:
        try:
            track_current_task(
                user_id=user_id,
                task_name=task_name,
            )
        except Exception:
            pass
    except Exception:
        pass
def safe_replace_tasks(
    user_id: Optional[int],
    tasks: Any,
):
    try:
        if replace_user_tasks:
            replace_user_tasks(
                user_id,
                tasks,
            )
    except TypeError:
        try:
            replace_user_tasks(
                user_id=user_id,
                tasks=tasks,
            )
        except Exception:
            pass
    except Exception:
        pass
# ============================================================
# PROCESS ONE MESSAGE
# ============================================================
async def process_urls(
    event,
    urls: List[str],
):
    total = len(urls)
    if total <= 0:
        return
    safe_track_task(
        event.sender_id,
        f"getuidfb: {total} URL",
    )
    for index, url in enumerate(
        urls,
        start=1,
    ):
        try:
            result = await resolve_facebook_url(
                url
            )
            text = format_result(
                result,
                index=index,
                total=total,
            )
            await event.respond(
                text,
                parse_mode="html",
                link_preview=False,
            )
        except Exception as exc:
            logger.exception(
                "getuidfb resolver error"
            )
            await event.respond(
                (
                    f"❌ <b>URL {index}/{total} "
                    f"FAILED</b>\n\n"
                    f"<code>{tg_escape(url)}</code>\n\n"
                    f"ERROR: "
                    f"{tg_escape(type(exc).__name__)}: "
                    f"{tg_escape(str(exc))}"
                ),
                parse_mode="html",
                link_preview=False,
            )
    safe_track_task(
        event.sender_id,
        "getuidfb: completed",
    )
# ============================================================
# MAIN /getuidfb HANDLER
# ============================================================
async def _handle_getuidfb(
    event,
):
    try:
        await event.respond(
            (
                "🔎 <b>FACEBOOK UID RESOLVER V21</b>\n\n"
                "📎 Gửi <b>link Facebook</b> cần kiểm tra.\n\n"
                "Hỗ trợ:\n"
                "• Profile\n"
                "• Post\n"
                "• Reel\n"
                "• Video\n"
                "• Photo\n"
                "• Story\n"
                "• Page\n"
                "• Group\n"
                "• /share/p/\n"
                "• /share/v/\n"
                "• /share/r/\n"
                "• pfbid...\n\n"
                "💡 Có thể gửi nhiều URL cùng lúc.\n"
                "💡 URL dính liền nhau cũng tự tách.\n\n"
                "⏳ Sau khi xử lý xong, bot tiếp tục "
                "chờ URL mới.\n\n"
                "🛑 Gửi /stop hoặc command khác để thoát."
            ),
            parse_mode="html",
            link_preview=False,
        )
    except Exception:
        return
    # --------------------------------------------------------
    # Interactive loop
    # --------------------------------------------------------
    while True:
        next_event = await wait_for_next_message(
            event.client,
            event,
            timeout=SESSION_TIMEOUT,
        )
        if next_event is None:
            try:
                await event.respond(
                    (
                        "⏱ <b>GETUIDFB TIMEOUT</b>\n\n"
                        "Phiên chờ URL đã hết hạn.\n"
                        "Gửi <code>/getuidfb</code> để tạo phiên mới."
                    ),
                    parse_mode="html",
                    link_preview=False,
                )
            except Exception:
                pass
            break
        text = (
            next_event.raw_text
            or ""
        ).strip()
        # ----------------------------------------------------
        # Don't swallow other commands
        # ----------------------------------------------------
        if is_other_command(
            text
        ):
            break
        # ----------------------------------------------------
        # Extract all FB URLs
        # ----------------------------------------------------
        urls = extract_facebook_urls(
            text
        )
        if not urls:
            try:
                await next_event.respond(
                    (
                        "❌ Không tìm thấy URL Facebook hợp lệ.\n\n"
                        "Hãy gửi link Facebook, ví dụ:\n"
                        "<code>https://www.facebook.com/...</code>"
                    ),
                    parse_mode="html",
                    link_preview=False,
                )
            except Exception:
                pass
            continue
        # ----------------------------------------------------
        # Multiple URL notification
        # ----------------------------------------------------
        if len(urls) > 1:
            try:
                await next_event.respond(
                    (
                        f"🔗 Đã nhận diện "
                        f"<b>{len(urls)}</b> URL Facebook.\n"
                        "⏳ Đang xử lý lần lượt..."
                    ),
                    parse_mode="html",
                    link_preview=False,
                )
            except Exception:
                pass
        # ----------------------------------------------------
        # Process
        # ----------------------------------------------------
        await process_urls(
            next_event,
            urls,
        )
        # ----------------------------------------------------
        # Continue loop
        # ----------------------------------------------------
        try:
            await next_event.respond(
                (
                    f"✅ Đã xử lý "
                    f"<b>{len(urls)}</b> URL.\n\n"
                    "📎 Gửi URL Facebook tiếp theo."
                ),
                parse_mode="html",
                link_preview=False,
            )
        except Exception:
            pass
# ============================================================
# REGISTER
# ============================================================
def register(
    bot,
    notify_bot=None,
):
    """
    Compatible with:
        module.register(bot, notify_bot)
    """
    bot.add_event_handler(
        _handle_getuidfb,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?$"
        ),
    )
    logger.info(
        "Loaded command: getuidfb"
    )
# ============================================================
# OPTIONAL DIRECT API
# ============================================================
async def resolve(
    url: str,
) -> ResolveResult:
    """
    Programmatic API.
    Example:
        result = await resolve(
            "https://www.facebook.com/..."
        )
    """
    return await resolve_facebook_url(
        url
    )
# ============================================================
# CLI TEST
# ============================================================
if __name__ == "__main__":
    import sys
    async def _cli():
        if len(sys.argv) < 2:
            print(
                "Usage: python getuidfb.py <facebook_url>"
            )
            return
        urls = extract_facebook_urls(
            " ".join(
                sys.argv[1:]
            )
        )
        if not urls:
            print(
                "No Facebook URL found."
            )
            return
        for url in urls:
            result = await resolve_facebook_url(
                url
            )
            print(
                format_result(
                    result
                )
            )
            print(
                "\n" + "=" * 70 + "\n"
            )
    asyncio.run(
        _cli()
    )