import argparse
import asyncio
import base64
import binascii
import html
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from urllib.parse import (
    parse_qs,
    quote,
    unquote,
    urlencode,
    urlparse,
)
import httpx
import requests
from telethon import events

# ============================================================
# COMMAND INFO & TASK MANAGER INTEGRATION
# ============================================================

from core.task_manager import replace_user_tasks, track_current_task

COMMAND_INFO = {
    "command": "getuidfb",
    "category": "🔎 FACEBOOK",
    "title": "Facebook UID",
    "description": (
        "Lấy UID Facebook từ link profile, post, story, "
        "group, reel..."
    ),
    "usage": "/getuidfb",
    "examples": [
        "/getuidfb",
    ],
    "details": [
        "Gửi /getuidfb.",
        "Sau đó gửi một hoặc nhiều link Facebook.",
        "Không cần xuống dòng giữa các link.",
        "Bot tự động nhận diện tất cả URL.",
        "Sau khi xử lý xong bot tiếp tục chờ link.",
        "Dùng /stop để dừng.",
    ],
    "supported": [
        "Profile",
        "Post",
        "Story",
        "Group",
        "Reel",
        "Share link",
    ],
}

# ============================================================
# CONFIG & CONSTANTS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,image/webp,"
        "image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
}

SESSION_KEY = "_dragon_sessions"

VERSION = "V15 ULTRA"
DEFAULT_TIMEOUT = 18.0
DEFAULT_MAX_PAGES = 8
DEFAULT_CONCURRENCY = 3
MAX_BODY_BYTES = 12 * 1024 * 1024

FB_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
    "fb.watch",
    "www.fb.watch",
}

REDIRECT_HOSTS = {
    "l.facebook.com",
    "lm.facebook.com",
}

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "fbclid",
    "rdid",
    "share_source",
    "ref",
    "refsrc",
    "mibextid",
    "tn",
}

RESERVED_PATHS = {
    "share",
    "watch",
    "stories",
    "story",
    "groups",
    "pages",
    "reel",
    "reels",
    "video",
    "videos",
    "photo",
    "photos",
    "events",
    "marketplace",
    "login",
    "logout",
    "recover",
    "help",
    "settings",
    "privacy",
    "policies",
    "plugins",
    "ajax",
    "api",
    "search",
    "gaming",
    "fundraisers",
    "business",
    "businesses",
    "profile.php",
}

ID_RE = re.compile(r"^\d{5,30}$")
PF_BID_RE = re.compile(r"^pfbid[A-Za-z0-9_-]+$", re.I)
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")

# ============================================================
# V15 ULTRA DATA MODELS
# ============================================================

@dataclass
class Evidence:
    field: str
    value: str
    kind: str
    score: int
    source: str
    url: str = ""
    context: str = ""
    direct: bool = False
    role: str = ""
    confidence: str = ""

@dataclass
class PageSnapshot:
    requested_url: str = ""
    final_url: str = ""
    status_code: int = 0
    content_type: str = ""
    title: str = ""
    og_url: str = ""
    og_type: str = ""
    canonical: str = ""
    html: str = ""
    history: list[str] = field(default_factory=list)
    app_links: list[str] = field(default_factory=list)
    deep_links: list[str] = field(default_factory=list)
    jsonld: list[dict] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

@dataclass
class EncodedID:
    token: str
    decoded: str = ""
    valid: bool = False
    encoded_type: str = ""
    actor_id: str | None = None
    object_id: str | None = None

@dataclass
class Result:
    requested_url: str
    resolved_url: str | None = None
    crawl_chain: list[str] = field(default_factory=list)
    status: str = "UNRESOLVED"
    success: bool = False
    url_type: str = "UNKNOWN"
    user_uid: str | None = None
    user_verification: str = "NOT VERIFIED"
    entity_id: str | None = None
    entity_type: str | None = None
    publisher_id: str | None = None
    publisher_type: str | None = None
    publisher_username: str | None = None
    identity_url: str | None = None
    group_id: str | None = None
    page_id: str | None = None
    post_id: str | None = None
    story_id: str | None = None
    video_id: str | None = None
    reel_id: str | None = None
    photo_id: str | None = None
    media_fbid: str | None = None
    decoded_id: str | None = None
    decoded_payload: str | None = None
    decoded_type: str | None = None
    decoded_actor_id: str | None = None
    share_token: str | None = None
    title: str | None = None
    confidence: int = 0
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self):
        return asdict(self)

# ============================================================
# V15 ULTRA BASIC UTILITIES
# ============================================================

def clean_text(value) -> str:
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = value.replace(r"\/", "/")
    value = value.replace(r'\"', '"')
    return value.strip()

def valid_numeric_id(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if ID_RE.fullmatch(value):
        return value
    return None

def normalize_url(url: str) -> str:
    url = clean_text(url)
    if not url:
        return ""
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
        url = "https://" + url
    return url

def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""

def same_fb_host(url: str) -> bool:
    host = host_of(url)
    if host.startswith("www."):
        host = host[4:]
    return host in {
        "facebook.com",
        "m.facebook.com",
        "mbasic.facebook.com",
        "web.facebook.com",
        "touch.facebook.com",
        "fb.watch",
    }

def is_redirect_wrapper(url: str) -> bool:
    return host_of(url) in REDIRECT_HOSTS

def unwrap_facebook_url(url: str) -> str:
    url = normalize_url(url)
    try:
        p = urlparse(url)
        host = p.netloc.lower().split(":")[0]
        if host in REDIRECT_HOSTS:
            qs = parse_qs(p.query)
            for key in ("u", "url", "target"):
                target = qs.get(key, [None])[0]
                if target:
                    return normalize_url(unquote(target))
    except Exception:
        pass
    return url

def strip_tracking(url: str) -> str:
    try:
        p = urlparse(url)
        qs = parse_qs(p.query, keep_blank_values=True)
        for key in list(qs):
            if key.lower() in TRACKING_PARAMS:
                qs.pop(key, None)
        return p._replace(query=urlencode(qs, doseq=True), fragment="").geturl()
    except Exception:
        return url

def canonicalize_url(url: str) -> str:
    return strip_tracking(normalize_url(url))

def normalize_username(value: str) -> str:
    return clean_text(value).lstrip("@").strip()

def looks_like_username(value: str) -> bool:
    value = normalize_username(value)
    if not value:
        return False
    if value.lower() in RESERVED_PATHS:
        return False
    return bool(USERNAME_RE.fullmatch(value))

def unique(items):
    return list(dict.fromkeys(x for x in items if x))

def safe_context(text: str, start: int, end: int, limit: int = 1800) -> str:
    value = text[max(0, start):min(len(text), end)]
    value = value.replace("\n", " ").replace("\r", " ")
    return value[:limit]

# ============================================================
# V15 ULTRA FACEBOOK ENCODED ID
# ============================================================

def base64_candidates(token: str):
    token = clean_text(token)
    if not token:
        return []
    variants = [
        token,
        token.replace("-", "+").replace("_", "/"),
    ]
    result = []
    for item in variants:
        item = re.sub(r"\s+", "", item)
        if not item:
            continue
        item += "=" * (-len(item) % 4)
        result.append(item)
    return unique(result)

def decode_fb_encoded_id(token: str) -> EncodedID | None:
    if not token:
        return None
    token = unquote(str(token)).strip()
    if PF_BID_RE.fullmatch(token):
        return EncodedID(token=token, valid=False)

    for candidate in base64_candidates(token):
        try:
            raw = base64.b64decode(candidate, validate=False)
            decoded = raw.decode("utf-8", errors="strict").strip()
        except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
            continue

        if not decoded.startswith("S:"):
            continue

        m = re.fullmatch(r"S:I(\d{5,30}):(\d{5,30})", decoded)
        if m:
            return EncodedID(
                token=token,
                decoded=decoded,
                valid=True,
                encoded_type="I",
                actor_id=m.group(1),
                object_id=m.group(2),
            )

        m = re.fullmatch(r"S:([A-Za-z0-9]+):(\d{5,30})", decoded)
        if m:
            return EncodedID(
                token=token,
                decoded=decoded,
                valid=True,
                encoded_type=m.group(1),
                object_id=m.group(2),
            )

        m = re.fullmatch(r"S:([A-Za-z0-9]+):(.+)", decoded)
        if m:
            return EncodedID(
                token=token,
                decoded=decoded,
                valid=False,
                encoded_type=m.group(1),
            )

    return EncodedID(token=token, valid=False)

# ============================================================
# V15 ULTRA URL PARSER
# ============================================================

class URLParser:
    def __init__(self):
        self.evidence: list[Evidence] = []

    def add(
        self,
        field,
        value,
        kind,
        score,
        source,
        url="",
        context="",
        direct=False,
        role="",
        confidence="",
    ):
        value = clean_text(value)
        if not value:
            return
        self.evidence.append(
            Evidence(
                field=field,
                value=value,
                kind=kind,
                score=score,
                source=source,
                url=url,
                context=context,
                direct=direct,
                role=role,
                confidence=confidence,
            )
        )

    def parse_username_object(self, path, url):
        m = re.match(r"^/([^/?#]+)/(posts|videos|photos|reels)/([^/?#]+)", path, re.I)
        if not m:
            return
        username, kind, object_id = m.groups()
        username = normalize_username(username)
        kind = kind.lower()
        if not looks_like_username(username):
            return

        self.add(
            "publisher_username",
            username,
            "typed_identity_url",
            100,
            f"/{username}/{kind}",
            url,
            direct=True,
            role="PUBLISHER",
            confidence="EXACT",
        )

        if kind == "posts":
            if ID_RE.fullmatch(object_id) or PF_BID_RE.fullmatch(object_id):
                self.add(
                    "post_id",
                    object_id,
                    "typed_object_url",
                    100,
                    "username post URL",
                    url,
                    direct=True,
                    role="POST",
                    confidence="EXACT",
                )
        elif kind == "videos":
            if ID_RE.fullmatch(object_id):
                self.add(
                    "video_id",
                    object_id,
                    "typed_object_url",
                    100,
                    "username video URL",
                    url,
                    direct=True,
                    role="VIDEO",
                    confidence="EXACT",
                )
        elif kind == "photos":
            if ID_RE.fullmatch(object_id):
                self.add(
                    "photo_id",
                    object_id,
                    "typed_object_url",
                    100,
                    "username photo URL",
                    url,
                    direct=True,
                    role="PHOTO",
                    confidence="EXACT",
                )
        elif kind == "reels":
            if ID_RE.fullmatch(object_id):
                self.add(
                    "reel_id",
                    object_id,
                    "typed_object_url",
                    100,
                    "username reel URL",
                    url,
                    direct=True,
                    role="REEL",
                    confidence="EXACT",
                )

    def parse(self, url: str):
        url = normalize_url(url)
        if not url:
            return
        try:
            p = urlparse(url)
        except Exception:
            return

        path = unquote(p.path or "")
        path = re.sub(r"/+", "/", path)
        if not path.startswith("/"):
            path = "/" + path
        lower = path.lower()
        qs = parse_qs(p.query, keep_blank_values=True)

        if lower == "/profile.php":
            uid = valid_numeric_id(qs.get("id", [None])[0])
            if uid:
                self.add(
                    "user_uid",
                    uid,
                    "typed_url",
                    100,
                    "profile.php?id",
                    url,
                    direct=True,
                    role="USER",
                    confidence="EXACT",
                )

        m = re.fullmatch(r"/people/[^/]+/(\d{5,30})/?", lower)
        if m:
            self.add(
                "user_uid",
                m.group(1),
                "typed_url",
                100,
                "people/name/UID",
                url,
                direct=True,
                role="USER",
                confidence="EXACT",
            )

        m = re.fullmatch(r"/pages/[^/]+/(\d{5,30})/?", lower)
        if m:
            self.add(
                "page_id",
                m.group(1),
                "typed_url",
                100,
                "pages/name/ID",
                url,
                direct=True,
                role="PAGE",
                confidence="EXACT",
            )

        m = re.match(r"^/groups/(\d{5,30})(?:/|$)", lower)
        if m:
            self.add(
                "group_id",
                m.group(1),
                "typed_url",
                100,
                "groups/ID",
                url,
                direct=True,
                role="GROUP",
                confidence="EXACT",
            )

        m = re.match(r"^/groups/(\d{5,30})/(posts|permalink)/([A-Za-z0-9-]+)", lower)
        if m:
            self.add(
                "group_id",
                m.group(1),
                "typed_url",
                100,
                "group URL",
                url,
                direct=True,
                role="GROUP",
                confidence="EXACT",
            )
            self.add(
                "post_id",
                m.group(3),
                "typed_url",
                100,
                "group post URL",
                url,
                direct=True,
                role="POST",
                confidence="EXACT",
            )

        m = re.match(r"^/reel/(\d{5,30})", lower)
        if m:
            self.add(
                "reel_id",
                m.group(1),
                "typed_url",
                100,
                "/reel/ID",
                url,
                direct=True,
                role="REEL",
                confidence="EXACT",
            )

        if lower in ("/watch", "/watch/"):
            vid = valid_numeric_id(qs.get("v", [None])[0])
            if vid:
                self.add(
                    "video_id",
                    vid,
                    "query_parameter",
                    100,
                    "watch?v",
                    url,
                    direct=True,
                    role="VIDEO",
                    confidence="EXACT",
                )

        if lower == "/video.php":
            vid = valid_numeric_id(qs.get("v", [None])[0])
            if vid:
                self.add(
                    "video_id",
                    vid,
                    "query_parameter",
                    100,
                    "video.php?v",
                    url,
                    direct=True,
                    role="VIDEO",
                    confidence="EXACT",
                )

        if lower == "/photo.php":
            pid = valid_numeric_id(qs.get("fbid", [None])[0])
            if pid:
                self.add(
                    "photo_id",
                    pid,
                    "query_parameter",
                    100,
                    "photo.php?fbid",
                    url,
                    direct=True,
                    role="PHOTO",
                    confidence="EXACT",
                )

        if lower in ("/permalink.php", "/story.php"):
            story_fbid = qs.get("story_fbid", [None])[0]
            owner_id = valid_numeric_id(qs.get("id", [None])[0])
            if story_fbid:
                if ID_RE.fullmatch(story_fbid):
                    self.add(
                        "post_id",
                        story_fbid,
                        "query_parameter",
                        100,
                        "story_fbid",
                        url,
                        direct=True,
                        role="POST",
                        confidence="EXACT",
                    )
                elif PF_BID_RE.fullmatch(story_fbid):
                    self.add(
                        "post_id",
                        story_fbid,
                        "opaque_token",
                        95,
                        "story_fbid",
                        url,
                        direct=True,
                        role="POST_TOKEN",
                        confidence="EXACT",
                    )
            if owner_id:
                self.add(
                    "publisher_id",
                    owner_id,
                    "query_parameter",
                    88,
                    "story owner",
                    url,
                    direct=True,
                    role="PUBLISHER_CANDIDATE",
                    confidence="HIGH",
                )

        m = re.match(r"^/stories/(\d{5,30})/([^/?#]+)", path, re.I)
        if m:
            actor_id, token = m.groups()
            self.add(
                "decoded_actor_id",
                actor_id,
                "story_path",
                100,
                "stories actor",
                url,
                direct=True,
                role="ACTOR",
                confidence="EXACT",
            )
            self.add(
                "story_id",
                token,
                "story_token",
                100,
                "stories token",
                url,
                direct=True,
                role="STORY_TOKEN",
                confidence="EXACT",
            )
            decoded = decode_fb_encoded_id(token)
            if decoded:
                if decoded.decoded:
                    self.add(
                        "decoded_payload",
                        decoded.decoded,
                        "encoded_id",
                        100,
                        "Facebook encoded token",
                        url,
                        direct=True,
                        role="ENCODED",
                        confidence="EXACT",
                    )
                if decoded.object_id:
                    self.add(
                        "decoded_id",
                        decoded.object_id,
                        "encoded_id",
                        100,
                        "encoded object",
                        url,
                        direct=True,
                        role="OBJECT",
                        confidence="EXACT",
                    )
                    self.add(
                        "post_id",
                        decoded.object_id,
                        "encoded_id",
                        98,
                        "story decoded object",
                        url,
                        direct=True,
                        role="POST",
                        confidence="VERY_HIGH",
                    )
                if decoded.encoded_type:
                    self.add(
                        "decoded_type",
                        decoded.encoded_type,
                        "encoded_id",
                        100,
                        "encoded type",
                        url,
                        direct=True,
                        role="ENCODED",
                        confidence="EXACT",
                    )
                if decoded.actor_id:
                    self.add(
                        "decoded_actor_id",
                        decoded.actor_id,
                        "encoded_id",
                        100,
                        "encoded actor",
                        url,
                        direct=True,
                        role="ACTOR",
                        confidence="EXACT",
                    )

        self.parse_username_object(path, url)

        m = re.fullmatch(r"/([^/?#]+)/?", path, re.I)
        if m:
            username = normalize_username(m.group(1))
            if looks_like_username(username):
                self.add(
                    "publisher_username",
                    username,
                    "profile_path",
                    90,
                    "Facebook profile path",
                    url,
                    direct=True,
                    role="PUBLISHER",
                    confidence="HIGH",
                )

        m = re.match(r"^/p/([^/?#]+)", path, re.I)
        if m and PF_BID_RE.fullmatch(m.group(1)):
            self.add(
                "post_id",
                m.group(1),
                "opaque_token",
                95,
                "/p/pfbid",
                url,
                direct=True,
                role="POST_TOKEN",
                confidence="EXACT",
            )

        m = re.match(r"^/share(?:/(p|v|r))?/?([^/?#]+)?", path, re.I)
        if m:
            share_kind, token = m.groups()
            share_kind = (share_kind or "").lower()
            if token:
                self.add(
                    "share_token",
                    token,
                    "share_url",
                    90,
                    "Facebook share URL",
                    url,
                    direct=True,
                    role="SHARE",
                    confidence="HIGH",
                )
            if share_kind == "p":
                self.add("url_type_hint", "SHARE_POST", "typed_url", 100, "share/p", url, direct=True)
            elif share_kind == "v":
                self.add("url_type_hint", "SHARE_VIDEO", "typed_url", 100, "share/v", url, direct=True)
            elif share_kind == "r":
                self.add("url_type_hint", "SHARE_REEL", "typed_url", 100, "share/r", url, direct=True)
            else:
                self.add("url_type_hint", "SHARE", "typed_url", 80, "share", url, direct=True)

        for key in ("media_fbid",):
            value = qs.get(key, [None])[0]
            if value and (ID_RE.fullmatch(value) or PF_BID_RE.fullmatch(value)):
                self.add(
                    "media_fbid",
                    value,
                    "query_parameter",
                    90,
                    f"query:{key}",
                    url,
                    direct=True,
                    role="OBJECT",
                    confidence="HIGH",
                )

        candidate = valid_numeric_id(qs.get("id", [None])[0])
        if candidate and lower != "/profile.php":
            self.add(
                "publisher_id",
                candidate,
                "generic_query_candidate",
                45,
                "generic ?id",
                url,
                direct=False,
                role="PUBLISHER_CANDIDATE",
                confidence="LOW",
            )

# ============================================================
# V15 ULTRA METADATA PARSER
# ============================================================

class MetadataParser:
    META_RE = re.compile(r"<meta\b[^>]*>", re.I)
    LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
    TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
    SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.I | re.S)
    ATTR_RE = re.compile(r'([:\w-]+)\s*=\s*["\']([^"\']*)["\']', re.I | re.S)

    def parse(self, text: str):
        result = {
            "title": "",
            "og_url": "",
            "og_type": "",
            "canonical": "",
            "app_links": [],
            "deep_links": [],
            "jsonld": [],
        }
        m = self.TITLE_RE.search(text)
        if m:
            result["title"] = clean_text(m.group(1))

        for tag in self.META_RE.findall(text):
            attrs = {k.lower(): clean_text(v) for k, v in self.ATTR_RE.findall(tag)}
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            content = attrs.get("content", "")
            if not content:
                continue

            if key == "og:url":
                result["og_url"] = content
            elif key == "og:type":
                result["og_type"] = content
            elif key in {"al:ios:url", "al:android:url", "al:web:url"}:
                result["app_links"].append(content)
                if (
                    content.startswith("fb://")
                    or "fb://profile/" in content
                    or "fb://page/" in content
                    or "fb://group/" in content
                ):
                    result["deep_links"].append(content)

        for tag in self.LINK_RE.findall(text):
            attrs = {k.lower(): clean_text(v) for k, v in self.ATTR_RE.findall(tag)}
            rel = attrs.get("rel", "").lower()
            href = attrs.get("href", "")
            if "canonical" in rel and href:
                result["canonical"] = href

        for attrs, body in self.SCRIPT_RE.findall(text):
            attrs_dict = {k.lower(): clean_text(v) for k, v in self.ATTR_RE.findall(attrs)}
            if attrs_dict.get("type", "").lower() != "application/ld+json":
                continue
            try:
                parsed = json.loads(html.unescape(body.strip()))
                if isinstance(parsed, list):
                    result["jsonld"].extend(x for x in parsed if isinstance(x, dict))
                elif isinstance(parsed, dict):
                    result["jsonld"].append(parsed)
            except Exception:
                pass

        return result

# ============================================================
# V15 ULTRA HTML ID ENGINE
# ============================================================

class HTMLIDEngine:
    PATTERNS = {
        "user_uid": [
            r'"profile_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"user_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"userID"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"profileId"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"profile_owner_id"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "page_id": [
            r'"page_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"pageID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "group_id": [
            r'"group_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"groupID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "actor_id": [
            r'"actor_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"actorID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "author_id": [
            r'"author_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"authorID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "owner_id": [
            r'"owner_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"ownerID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "publisher_id": [
            r'"publisher_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"publisherID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "entity_id": [
            r'"entity_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"entityID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "post_id": [
            r'"post_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"story_fbid"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"postID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "media_fbid": [
            r'"media_fbid"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"mediaFbid"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "video_id": [
            r'"video_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"videoID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "reel_id": [
            r'"reel_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"reelID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
        "photo_id": [
            r'"photo_id"\s*:\s*"?(?P<id>\d{5,30})"?',
            r'"photoID"\s*:\s*"?(?P<id>\d{5,30})"?',
        ],
    }

    URL_PATTERNS = {
        "post_id": [
            r"/posts/(\d{5,30})",
            r"/permalink/(\d{5,30})",
            r'story_fbid[=:/\"]+(\d{5,30})',
        ],
        "video_id": [
            r"/videos/(\d{5,30})",
            r'video_id[=:/\"]+(\d{5,30})',
        ],
        "reel_id": [
            r"/reel/(\d{5,30})",
            r'reel_id[=:/\"]+(\d{5,30})',
        ],
        "photo_id": [
            r"/photo(?:.php)?[^0-9]{0,50}(\d{5,30})",
            r'photo_id[=:/\"]+(\d{5,30})',
        ],
    }

    def parse(self, text: str, url: str):
        result = []
        if not text:
            return result

        for field, patterns in self.PATTERNS.items():
            for pattern in patterns:
                for m in re.finditer(pattern, text, re.I):
                    value = m.group("id")
                    if field == "user_uid":
                        score = 58
                        kind = "typed_html_identity"
                        role = "USER"
                    elif field in {"page_id", "group_id"}:
                        score = 50
                        kind = "typed_html_entity"
                        role = "PAGE" if field == "page_id" else "GROUP"
                    else:
                        score = 48
                        kind = "typed_html_object"
                        role = "OBJECT"

                    result.append(
                        Evidence(
                            field=field,
                            value=value,
                            kind=kind,
                            score=score,
                            source=f"HTML:{field}",
                            url=url,
                            context=safe_context(text, m.start() - 900, m.end() + 900),
                            direct=False,
                            role=role,
                            confidence="MEDIUM",
                        )
                    )

        for field, patterns in self.URL_PATTERNS.items():
            for pattern in patterns:
                for m in re.finditer(pattern, text, re.I):
                    result.append(
                        Evidence(
                            field=field,
                            value=m.group(1),
                            kind="embedded_url",
                            score=62,
                            source=f"HTML_URL:{field}",
                            url=url,
                            context=safe_context(text, m.start() - 700, m.end() + 700),
                            direct=False,
                            role="OBJECT",
                            confidence="MEDIUM",
                        )
                    )
        return result

# ============================================================
# V15 ULTRA IDENTITY ENGINE
# ============================================================

class IdentityEngine:
    ID_KEYS = (
        "profile_id",
        "user_id",
        "userID",
        "profileId",
        "profile_owner_id",
        "profileOwnerID",
    )

    def parse(self, text: str, url: str, expected_username: str | None = None):
        result = []
        if not text:
            return result

        for key in self.ID_KEYS:
            patterns = [
                rf'"{re.escape(key)}"\s*:\s*"(\d{{5,30}})"',
                rf'"{re.escape(key)}"\s*:\s*(\d{{5,30}})',
                rf'"{re.escape(key)}"\s*=\s*"(\d{{5,30}})"',
            ]
            for pattern in patterns:
                for m in re.finditer(pattern, text, re.I):
                    score = 84 if key == "profile_id" else 78
                    result.append(
                        Evidence(
                            field="user_uid",
                            value=m.group(1),
                            kind="profile_identity",
                            score=score,
                            source=f"IDENTITY:{key}",
                            url=url,
                            context=safe_context(text, m.start() - 1400, m.end() + 1400),
                            direct=True,
                            role="USER",
                            confidence="HIGH",
                        )
                    )

        username = normalize_username(expected_username or "")
        if not username:
            return result

        username_patterns = [
            rf'"username"\s*:\s*"{re.escape(username)}"',
            rf'"vanity"\s*:\s*"{re.escape(username)}"',
            rf'"vanity_name"\s*:\s*"{re.escape(username)}"',
            rf'/{re.escape(username)}(?:["\'/?&#]|$)',
        ]
        username_hits = []
        for pattern in username_patterns:
            for m in re.finditer(pattern, text, re.I):
                username_hits.append(m)

        if username_hits:
            result.append(
                Evidence(
                    field="publisher_username",
                    value=username,
                    kind="profile_username",
                    score=96,
                    source="PROFILE:username",
                    url=url,
                    context=f"username={username}",
                    direct=True,
                    role="PUBLISHER",
                    confidence="HIGH",
                )
            )

        for um in username_hits:
            context = safe_context(text, um.start() - 3500, um.end() + 3500, 7000)
            for key in self.ID_KEYS:
                pattern = rf'"{re.escape(key)}"\s*:\s*"?(\d{{5,30}})"?'
                for im in re.finditer(pattern, context, re.I):
                    uid = im.group(1).replace("\\", "")
                    result.append(
                        Evidence(
                            field="user_uid",
                            value=uid,
                            kind="username_uid_correlation",
                            score=92,
                            source=f"USERNAME:{username}+{key}",
                            url=url,
                            context=context[:1800],
                            direct=False,
                            role="USER",
                            confidence="VERY_HIGH",
                        )
                    )
        return result

# ============================================================
# V15 ULTRA CORRELATION ENGINE
# ============================================================

class CorrelationEngine:
    ID_KEYS = (
        "profile_id",
        "user_id",
        "userID",
        "profileId",
        "profile_owner_id",
        "profileOwnerID",
    )

    def correlate(self, text: str, url: str, expected_username: str | None = None):
        result = []
        if not text:
            return result

        username = normalize_username(expected_username or "")
        if not username:
            return result

        patterns = [
            rf'/{re.escape(username)}(?:["\'/?&#]|$)',
            rf'"username"\s*:\s*"{re.escape(username)}"',
            rf'"vanity"\s*:\s*"{re.escape(username)}"',
            rf'"vanity_name"\s*:\s*"{re.escape(username)}"',
        ]
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.I):
                context = safe_context(text, m.start() - 2800, m.end() + 2800, 5600)
                for key in self.ID_KEYS:
                    p = rf'"{re.escape(key)}"\s*:\s*"?(\d{{5,30}})"?'
                    for im in re.finditer(p, context, re.I):
                        uid = im.group(1).replace("\\", "")
                        result.append(
                            Evidence(
                                field="user_uid",
                                value=uid,
                                kind="username_local_correlation",
                                score=82,
                                source=f"nearby:{username}+{key}",
                                url=url,
                                context=context[:1800],
                                direct=False,
                                role="USER",
                                confidence="HIGH",
                            )
                        )
        return result

# ============================================================
# V15 ULTRA DEEP LINK ENGINE
# ============================================================

class DeepLinkEngine:
    PATTERNS = [
        ("user_uid", re.compile(r"fb://profile/(\d{5,30})", re.I), "USER"),
        ("page_id", re.compile(r"fb://page/(\d{5,30})", re.I), "PAGE"),
        ("group_id", re.compile(r"fb://group/(\d{5,30})", re.I), "GROUP"),
    ]

    def parse(self, values, source_url):
        result = []
        for value in values:
            value = clean_text(value)
            for field, pattern, role in self.PATTERNS:
                for m in pattern.finditer(value):
                    result.append(
                        Evidence(
                            field=field,
                            value=m.group(1),
                            kind="deep_link",
                            score=100,
                            source="Facebook App Link",
                            url=source_url,
                            context=value,
                            direct=True,
                            role=role,
                            confidence="EXACT",
                        )
                    )
        return result

# ============================================================
# V15 ULTRA JSON-LD ENGINE
# ============================================================

class JSONLDEngine:
    def _walk(self, value, output):
        if isinstance(value, dict):
            output.append(value)
            for v in value.values():
                self._walk(v, output)
        elif isinstance(value, list):
            for v in value:
                self._walk(v, output)

    def parse(self, items, url):
        result = []
        expanded = []
        for item in items:
            self._walk(item, expanded)

        for item in expanded:
            author = item.get("author")
            if isinstance(author, dict):
                author_url = author.get("url")
                if isinstance(author_url, str):
                    result.append(
                        Evidence(
                            field="publisher_url",
                            value=author_url,
                            kind="jsonld",
                            score=58,
                            source="JSON-LD author.url",
                            url=url,
                            context=str(author)[:1800],
                            role="PUBLISHER",
                            confidence="MEDIUM",
                        )
                    )
                if author.get("name"):
                    result.append(
                        Evidence(
                            field="publisher_name",
                            value=str(author["name"]),
                            kind="jsonld",
                            score=20,
                            source="JSON-LD author.name",
                            url=url,
                            context=str(author)[:1000],
                            role="PUBLISHER",
                            confidence="LOW",
                        )
                    )

            object_url = item.get("url")
            if isinstance(object_url, str):
                result.append(
                    Evidence(
                        field="object_url",
                        value=object_url,
                        kind="jsonld",
                        score=55,
                        source="JSON-LD url",
                        url=url,
                        role="OBJECT",
                        confidence="MEDIUM",
                    )
                )
        return result

# ============================================================
# V15 ULTRA NETWORK HTTP CLIENT
# ============================================================

class HTTPClient:
    def __init__(self, timeout=DEFAULT_TIMEOUT, debug=False):
        self.timeout = timeout
        self.debug = debug
        self.client = None
        self.headers = {
            "authority": "www.facebook.com",
            "accept": "*/*",
            "accept-language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
            "content-type": "application/x-www-form-urlencoded",
            "dnt": "1",
            "origin": "https://www.facebook.com",
            "sec-ch-prefers-color-scheme": "dark",
            "sec-ch-ua": '"Chromium";v="117", "Not;A=Brand";v="8"',
            "sec-ch-ua-full-version-list": '"Chromium";v="117.0.5938.157", "Not;A=Brand";v="8.0.0.0"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"15.0.0"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/117.0.0.0 Safari/537.36"
            ),
            "x-fb-friendly-name": "useCometConsentPromptEndOfFlowBatchedMutation",
        }

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            headers=self.headers,
            timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 8.0)),
            follow_redirects=True,
            max_redirects=12,
            http2=False,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.client:
            await self.client.aclose()

    async def get(self, url):
        if not self.client:
            return None
        if not url.startswith(("http://", "https://")):
            return None

        last_error = None
        for attempt in range(3):
            try:
                response = await self.client.get(url)
                if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue

                raw = response.content[:MAX_BODY_BYTES]
                encoding = response.encoding or "utf-8"
                text = raw.decode(encoding, errors="replace")
                meta = MetadataParser().parse(text)

                return PageSnapshot(
                    requested_url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    title=meta["title"],
                    og_url=meta["og_url"],
                    og_type=meta["og_type"],
                    canonical=meta["canonical"],
                    html=text,
                    history=[str(x.url) for x in response.history],
                    app_links=meta["app_links"],
                    deep_links=meta["deep_links"],
                    jsonld=meta["jsonld"],
                    headers={
                        k: response.headers[k]
                        for k in ("content-type", "cache-control", "location", "server")
                        if k in response.headers
                    },
                )
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ) as exc:
                last_error = exc
                await asyncio.sleep(0.6 * (attempt + 1))
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.4 * (attempt + 1))

        if self.debug and last_error:
            print(f"[DEBUG] request failed: {last_error!r}", file=sys.stderr)
        return None

# ============================================================
# V15 ULTRA FACEBOOK RESOLVER
# ============================================================

class FacebookResolver:
    def __init__(
        self,
        timeout=DEFAULT_TIMEOUT,
        max_pages=DEFAULT_MAX_PAGES,
        concurrency=DEFAULT_CONCURRENCY,
        debug=False,
    ):
        self.timeout = timeout
        self.max_pages = max(1, max_pages)
        self.concurrency = max(1, concurrency)
        self.debug = debug
        self.visited: set[str] = set()
        self.cache: dict[str, PageSnapshot] = {}

    def log(self, *args):
        if self.debug:
            print("[DEBUG]", *args, file=sys.stderr)

    async def fetch_one(self, client, url):
        url = canonicalize_url(url)
        if not url:
            return None
        if url in self.visited or len(self.visited) >= self.max_pages:
            return None
        if url in self.cache:
            return self.cache[url]
        if not url.startswith(("http://", "https://")):
            return None

        self.visited.add(url)
        self.log("FETCH", url)
        snapshot = await client.get(url)
        if snapshot:
            self.cache[url] = snapshot
            self.log("STATUS", snapshot.status_code, "FINAL", snapshot.final_url)
        return snapshot

    def analyze_snapshot(self, snapshot, parser):
        urls = unique(
            [
                snapshot.final_url,
                *snapshot.history,
                snapshot.og_url,
                snapshot.canonical,
                *snapshot.app_links,
                *snapshot.deep_links,
            ]
        )
        for u in urls:
            if u:
                parser.parse(u)

        parser.evidence.extend(HTMLIDEngine().parse(snapshot.html, snapshot.final_url))

        usernames = {
            normalize_username(e.value)
            for e in parser.evidence
            if (e.field == "publisher_username") and e.value
        }

        try:
            path = urlparse(snapshot.final_url).path.strip("/")
            if path:
                first = path.split("/")[0]
                if looks_like_username(first):
                    usernames.add(first)
        except Exception:
            pass

        identity = IdentityEngine()
        correlation = CorrelationEngine()
        for username in usernames:
            parser.evidence.extend(
                identity.parse(snapshot.html, snapshot.final_url, username)
            )
            parser.evidence.extend(
                correlation.correlate(snapshot.html, snapshot.final_url, username)
            )

        parser.evidence.extend(
            DeepLinkEngine().parse(snapshot.deep_links, snapshot.final_url)
        )
        parser.evidence.extend(
            JSONLDEngine().parse(snapshot.jsonld, snapshot.final_url)
        )

    def discover_urls(self, snapshot):
        urls = []
        for x in (snapshot.og_url, snapshot.canonical):
            if x:
                urls.append(canonicalize_url(x))
        for x in snapshot.app_links + snapshot.deep_links:
            if not x or x.startswith("fb://"):
                continue
            urls.append(canonicalize_url(x))
        for item in snapshot.jsonld:
            author = item.get("author")
            if isinstance(author, dict):
                author_url = author.get("url")
                if isinstance(author_url, str) and same_fb_host(author_url):
                    urls.append(canonicalize_url(author_url))
            item_url = item.get("url")
            if isinstance(item_url, str) and same_fb_host(item_url):
                urls.append(canonicalize_url(item_url))
        return unique(urls)

    def get_username(self, parser):
        candidates = [e for e in parser.evidence if e.field == "publisher_username"]
        if not candidates:
            return None
        candidates.sort(
            key=lambda e: (
                e.score,
                int(e.direct),
                int(e.confidence in {"EXACT", "VERY_HIGH"}),
            ),
            reverse=True,
        )
        return normalize_username(candidates[0].value)

    def derive_profile_url(self, parser):
        username = self.get_username(parser)
        if not username:
            return None
        return "https://www.facebook.com/" + quote(username)

    @staticmethod
    def dedupe_evidence(evidence):
        best = {}
        for ev in evidence:
            key = (ev.field, ev.value, ev.kind, ev.source)
            old = best.get(key)
            if old is None or (ev.score, ev.direct) > (old.score, old.direct):
                best[key] = ev
        return list(best.values())

    @staticmethod
    def best(evidence, field):
        values = [e for e in evidence if e.field == field]
        if not values:
            return None
        return max(
            values,
            key=lambda e: (
                e.score,
                int(e.direct),
                int(e.confidence in {"EXACT", "VERY_HIGH"}),
            ),
        )

    def classify_url_type(self, evidence, requested_url):
        hints = [e.value for e in evidence if e.field == "url_type_hint"]
        if hints:
            return hints[0]
        try:
            p = urlparse(requested_url)
            path = unquote(p.path).lower()
        except Exception:
            return "UNKNOWN"

        if re.match(r"^/groups/\d+/(posts|permalink)/", path):
            return "GROUP_POST"
        if re.match(r"^/groups/\d+/?$", path):
            return "GROUP"
        if re.match(r"^/pages/[^/]+/\d+/?$", path):
            return "PAGE"
        if path == "/profile.php" or re.match(r"^/people/[^/]+/\d+/?$", path):
            return "PROFILE"
        if re.match(r"^/stories/\d+/", path):
            return "STORY"
        if re.match(r"^/reel/\d+", path) or re.match(r"^/[^/]+/reels/\d+", path):
            return "REEL"
        if path in {"/watch", "/watch/"} and "v" in parse_qs(urlparse(requested_url).query):
            return "WATCH_VIDEO"
        if path == "/video.php":
            return "VIDEO"
        if path == "/photo.php":
            return "PHOTO"

        m = re.match(r"^/[^/]+/(posts|videos|photos|reels)/", path)
        if m:
            return {
                "posts": "USER_POST",
                "videos": "USER_VIDEO",
                "photos": "USER_PHOTO",
                "reels": "USER_REEL",
            }[m.group(1)]

        if re.match(r"^/p/", path):
            return "POST"
        if re.match(r"^/share/", path):
            return "SHARE"
        if re.fullmatch(r"/[^/]+/?", path):
            return "PROFILE_CANDIDATE"

        return "UNKNOWN"

    def build_result(self, requested_url, parser, snapshots):
        parser.evidence = self.dedupe_evidence(parser.evidence)
        result = Result(requested_url=requested_url)

        chain = [requested_url]
        for snapshot in snapshots:
            chain.extend(snapshot.history)
            if snapshot.final_url:
                chain.append(snapshot.final_url)
        result.crawl_chain = unique(chain)

        if snapshots:
            result.resolved_url = snapshots[-1].final_url
        else:
            result.resolved_url = requested_url

        result.url_type = self.classify_url_type(parser.evidence, requested_url)

        for field_name in (
            "post_id",
            "story_id",
            "video_id",
            "reel_id",
            "photo_id",
            "media_fbid",
            "decoded_id",
            "decoded_payload",
            "decoded_type",
            "decoded_actor_id",
            "group_id",
            "page_id",
            "publisher_id",
            "publisher_username",
            "share_token",
        ):
            ev = self.best(parser.evidence, field_name)
            if ev:
                setattr(result, field_name, ev.value)

        user_evidence = [
            e for e in parser.evidence if (e.field == "user_uid") and valid_numeric_id(e.value)
        ]
        stats = defaultdict(
            lambda: {
                "max": 0,
                "profile": False,
                "correlation": False,
                "deep": False,
                "sources": set(),
                "direct": False,
            }
        )
        for ev in user_evidence:
            s = stats[ev.value]
            s["max"] = max(s["max"], ev.score)
            s["sources"].add(ev.source)
            s["direct"] |= ev.direct
            if ev.kind == "profile_identity":
                s["profile"] = True
            if ev.kind in {"username_uid_correlation", "username_local_correlation"}:
                s["correlation"] = True
            if ev.kind == "deep_link":
                s["deep"] = True

        verified = []
        for uid, s in stats.items():
            independent = len(s["sources"])
            if s["profile"] and s["correlation"] and independent >= 2 and s["max"] >= 80:
                verified.append(uid)
            elif s["profile"] and s["max"] >= 92 and s["direct"]:
                verified.append(uid)
            elif s["deep"] and s["correlation"]:
                verified.append(uid)

        verified = sorted(set(verified))
        if len(verified) == 1:
            result.user_uid = verified[0]
            result.user_verification = "VERIFIED"
        elif len(verified) > 1:
            result.user_verification = "AMBIGUOUS"
            result.warnings.append("Multiple strong USER UID candidates detected.")
        else:
            result.user_verification = "NOT VERIFIED"

        if result.group_id:
            result.publisher_type = "GROUP"
        elif result.page_id:
            result.publisher_type = "PAGE"
        elif result.user_uid:
            result.publisher_type = "USER"
        elif result.publisher_username:
            result.publisher_type = "USER_CANDIDATE"
        elif result.publisher_id:
            result.publisher_type = "UNKNOWN"

        if result.group_id and result.post_id:
            result.entity_type = "GROUP_POST"
            result.entity_id = result.post_id
        elif result.page_id and result.post_id:
            result.entity_type = "PAGE_POST"
            result.entity_id = result.post_id
        elif result.post_id and result.user_uid:
            result.entity_type = "USER_POST"
            result.entity_id = result.post_id
        elif result.post_id and result.publisher_username:
            result.entity_type = "USER_POST_UNVERIFIED"
            result.entity_id = result.post_id
        elif result.story_id:
            result.entity_type = "STORY"
            result.entity_id = result.decoded_id or result.story_id
        elif result.reel_id:
            result.entity_type = "REEL"
            result.entity_id = result.reel_id
        elif result.video_id and result.page_id:
            result.entity_type = "PAGE_VIDEO"
            result.entity_id = result.video_id
        elif result.video_id and result.user_uid:
            result.entity_type = "USER_VIDEO"
            result.entity_id = result.video_id
        elif result.video_id:
            result.entity_type = "VIDEO"
            result.entity_id = result.video_id
        elif result.photo_id and result.page_id:
            result.entity_type = "PAGE_PHOTO"
            result.entity_id = result.photo_id
        elif result.photo_id and result.user_uid:
            result.entity_type = "USER_PHOTO"
            result.entity_id = result.photo_id
        elif result.photo_id:
            result.entity_type = "PHOTO"
            result.entity_id = result.photo_id
        elif result.group_id:
            result.entity_type = "GROUP"
            result.entity_id = result.group_id
        elif result.page_id:
            result.entity_type = "PAGE"
            result.entity_id = result.page_id
        elif result.user_uid:
            result.entity_type = "USER"
            result.entity_id = result.user_uid
        elif result.post_id:
            result.entity_type = "POST"
            result.entity_id = result.post_id
        elif result.publisher_username:
            result.entity_type = "USER_PROFILE"
            result.entity_id = result.publisher_username

        if result.user_uid:
            if result.publisher_username:
                result.identity_url = "https://www.facebook.com/" + quote(result.publisher_username)
            else:
                result.identity_url = "https://www.facebook.com/profile.php?id=" + result.user_uid
        elif result.publisher_username:
            result.identity_url = "https://www.facebook.com/" + quote(result.publisher_username)

        for snapshot in snapshots:
            if snapshot.title:
                result.title = snapshot.title
                break

        strong_uids = {
            uid for uid, s in stats.items() if (s["profile"] or s["correlation"])
        }
        if (
            result.publisher_id
            and result.user_uid
            and result.publisher_id != result.user_uid
            and result.publisher_id in strong_uids
        ):
            result.warnings.append("Publisher ID conflicts with verified USER UID.")
            result.user_uid = None
            result.user_verification = "CONFLICT"

        combined = ""
        for snapshot in snapshots:
            combined += (snapshot.title + "\n" + snapshot.html[:200000]).lower()

        restriction_terms = (
            "log in to facebook",
            "you must log in",
            "content isn't available",
            "this content isn't available",
            "page isn't available",
            "something went wrong",
            "checkpoint",
        )
        if any(term in combined for term in restriction_terms):
            result.warnings.append("Facebook returned a login/restriction/unavailable page.")

        confidence = 0
        if result.url_type != "UNKNOWN":
            confidence += 20
        if result.entity_id:
            confidence += 20
        if result.resolved_url:
            confidence += 10

        if result.user_verification == "VERIFIED":
            confidence += 35
        elif result.user_verification == "AMBIGUOUS":
            confidence += 8
        elif result.user_verification == "CONFLICT":
            confidence = 15

        if result.publisher_username:
            confidence += 8
        if any([
            result.post_id,
            result.video_id,
            result.reel_id,
            result.photo_id,
            result.story_id,
        ]):
            confidence += 10

        if any(e.direct and e.confidence == "EXACT" for e in parser.evidence):
            confidence += 5

        if result.user_verification == "CONFLICT":
            confidence -= 30

        if result.user_verification == "NOT VERIFIED" and result.entity_type == "USER_POST_UNVERIFIED":
            confidence = min(confidence, 70)

        result.confidence = max(0, min(100, confidence))

        if result.entity_id and result.user_verification == "VERIFIED":
            result.status = "EXACT"
            result.success = True
        elif result.entity_id:
            result.status = "PARTIAL"
            result.success = True
        else:
            result.status = "UNRESOLVED"
            result.success = False

        result.evidence = sorted(
            parser.evidence, key=lambda e: (e.field, -e.score, not e.direct)
        )
        return result

    async def resolve(self, requested_url):
        original_url = normalize_url(requested_url)
        requested_url = unwrap_facebook_url(original_url)

        parser = URLParser()
        parser.parse(requested_url)

        snapshots = []
        async with HTTPClient(self.timeout, self.debug) as client:
            first = await self.fetch_one(client, requested_url)
            if not first:
                return self.build_result(original_url, parser, snapshots)

            snapshots.append(first)
            self.analyze_snapshot(first, parser)

            profile_url = self.derive_profile_url(parser)
            candidates = self.discover_urls(first)
            if profile_url:
                candidates.insert(0, profile_url)

            candidates.extend(first.history)
            if first.final_url:
                candidates.append(first.final_url)

            candidates = [canonicalize_url(x) for x in unique(candidates) if x]
            candidates = [
                x for x in candidates if (same_fb_host(x) or is_redirect_wrapper(x))
            ]
            candidates = candidates[:max(0, self.max_pages - 1)]

            semaphore = asyncio.Semaphore(self.concurrency)

            async def worker(url):
                async with semaphore:
                    return await self.fetch_one(client, url)

            if candidates:
                fetched = await asyncio.gather(
                    *(worker(x) for x in candidates), return_exceptions=True
                )
                for item in fetched:
                    if isinstance(item, PageSnapshot):
                        snapshots.append(item)
                        self.analyze_snapshot(item, parser)

        return self.build_result(original_url, parser, snapshots)

# ============================================================
# CLI PRINT UTILITIES
# ============================================================

def print_field(label, value):
    if value is None or value == "":
        return
    print(f"{label:<20}: {value}")

def print_result(result, show_evidence=False):
    width = 62
    print()
    print("╭" + "─" * width + "╮")
    title = f"│ FB UID / ENTITY RESOLVER {VERSION}"
    print(title.ljust(width + 1) + "│")
    print("╰" + "─" * width + "╯")
    print_field("REQUESTED URL", result.requested_url)
    print_field("URL TYPE", result.url_type)
    print_field("STATUS", result.status)
    print_field("SUCCESS", result.success)
    print_field("USER UID", result.user_uid or "NOT VERIFIED")
    print_field("USERNAME", result.publisher_username)
    print_field("IDENTITY URL", result.identity_url)
    print_field("ENTITY ID", result.entity_id)
    print_field("GROUP ID", result.group_id)
    print_field("PAGE ID", result.page_id)
    print_field("POST ID", result.post_id)
    print_field("STORY ID", result.story_id)
    print_field("VIDEO ID", result.video_id)
    print_field("REEL ID", result.reel_id)
    print_field("PHOTO ID", result.photo_id)
    print_field("MEDIA FBID", result.media_fbid)
    print_field("ACTOR ID", result.decoded_actor_id)
    print_field("DECODED ID", result.decoded_id)
    print_field("TITLE", result.title)
    print_field("CONFIDENCE", f"{result.confidence}%")

    if result.warnings:
        print("\nWARNINGS")
        for warning in result.warnings:
            print(" !", warning)

    if show_evidence:
        print("\nEVIDENCE")
        for i, ev in enumerate(result.evidence, 1):
            print(f"\n [{i}] {ev.field}={ev.value}")
            print(f" score : {ev.score}")
            print(f" kind : {ev.kind}")
            print(f" source: {ev.source}")
            print(f" direct: {ev.direct}")
            if ev.confidence:
                print(f" conf : {ev.confidence}")
            if ev.role:
                print(f" role : {ev.role}")
            if ev.url:
                print(f" url : {ev.url}")
            if ev.context:
                print(" ctx : " + ev.context[:700])

    if show_evidence and result.crawl_chain:
        print("\nCRAWL CHAIN")
        for step in result.crawl_chain:
            print(" ->", step)

# ============================================================
# TELEGRAM BOT HANDLER HELPERS
# ============================================================

def get_sessions(bot):
    if not hasattr(bot, SESSION_KEY):
        setattr(bot, SESSION_KEY, {})
    return getattr(bot, SESSION_KEY)

def extract_urls(text):
    if not text:
        return []
    matches = list(re.finditer(r"https?://", text, flags=re.IGNORECASE))
    if not matches:
        return []
    urls = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        url = text[start:end].strip().rstrip(".,!?;:)]}>\"'")
        if not url:
            continue
        parts = re.split(r"\s+", url)
        for part in parts:
            part = part.strip().rstrip(".,!?;:)]}>\"'")
            if not part:
                continue
            try:
                parsed = urlparse(part)
                host = parsed.netloc.lower().split(":")[0]
            except Exception:
                continue
            valid = (
                host == "facebook.com"
                or host.endswith(".facebook.com")
                or host == "fb.com"
                or host.endswith(".fb.com")
                or host == "fb.watch"
                or host.endswith(".fb.watch")
            )
            if not valid:
                continue
            if part not in urls:
                urls.append(part)
    return urls

def clean_facebook_uid(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    match = re.search(r"(?<!\d)\d+(?!\d)", value)
    if not match:
        return None
    uid = match.group(0)
    if not uid.isdigit():
        return None
    return uid

def esc(value):
    return html.escape(str(value))

def format_results(results):
    lines = []
    lines.append("╭────────────────────────╮")
    lines.append("│ 🆔 <b>FACEBOOK UID</b> │")
    lines.append("╰────────────────────────╯")
    lines.append("")
    total = len(results)
    success = sum(1 for item in results if item.get("success"))
    failed = total - success
    lines.append(f"📊 Tổng: <b>{total}</b> ✅ {success} ❌ {failed}")
    lines.append("")
    for index, item in enumerate(results, start=1):
        url = esc(item.get("url", ""))
        uid = item.get("uid")
        if uid:
            lines.append(f"<b>{index}.</b> 🆔 <code>{esc(uid)}</code>")
            lines.append(f'🔗 <a href="{url}">Mở Facebook</a>')
            lines.append("✅ <i>Thành công</i>")
        else:
            lines.append(f"<b>{index}.</b> ❌ <b>Không tìm thấy UID</b>")
            lines.append(f'🔗 <a href="{url}">Mở Facebook</a>')
            if item.get("error"):
                error = esc(item.get("error"))[:500]
                lines.append(f"⚠️ <code>{error}</code>")
            else:
                lines.append("⚠️ <i>Facebook không trả về ID.</i>")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔄 <b>GET UID SẴN SÀNG</b>")
    lines.append("📥 Gửi link Facebook tiếp theo.")
    lines.append("💡 Có thể gửi nhiều link cùng lúc.")
    lines.append("🛑 <code>/stop</code> để dừng.")
    return "\n".join(lines)

async def get_uid(event, url, notify_bot=None):
    user = await event.get_sender()
    try:
        resolver = FacebookResolver(timeout=15.0, max_pages=5, concurrency=2)
        v15_res = await resolver.resolve(url)

        target_uid = (
            v15_res.user_uid
            or v15_res.entity_id
            or v15_res.page_id
            or v15_res.group_id
            or v15_res.post_id
            or v15_res.video_id
            or v15_res.reel_id
            or v15_res.photo_id
        )

        clean_uid = clean_facebook_uid(target_uid)

        if clean_uid:
            result = {"url": url, "uid": clean_uid, "success": True}
        else:
            result = {"url": url, "uid": None, "success": False}

        if notify_bot:
            try:
                if clean_uid:
                    admin_result = f"UID: {clean_uid}\nLink: {url}"
                else:
                    admin_result = f"UID: Không tìm thấy\nLink: {url}"
                await notify_bot(user, "/getuidfb", result=admin_result)
            except Exception as e:
                print(f"[UID ADMIN] {e}")

        return result
    except Exception as e:
        print(f"[GETUID ERROR] {e}")
        return {
            "url": url,
            "uid": None,
            "success": False,
            "error": str(e),
        }

# ============================================================
# TELEGRAM REGISTER HANDLER
# ============================================================

def register(bot, notify_bot):
    sessions = get_sessions(bot)

    @bot.on(events.NewMessage(pattern=r"^/getuidfb(?:@\w+)?$"))
    async def getuid_start(event):
        await replace_user_tasks(event.sender_id)
        for _attr in ("_dragon_sessions", "_dragon_download_sessions"):
            _sessions = getattr(bot, _attr, None)
            if isinstance(_sessions, dict):
                _sessions.pop(event.sender_id, None)
        try:
            from core.power.session import clear_session
            clear_session(bot, event.sender_id)
        except Exception:
            pass

        user_id = event.sender_id
        sessions[user_id] = {
            "command": "getuidfb",
            "running": True,
            "processing": False,
        }

        await event.reply(
            "╭─────────────────────╮\n"
            "│ 🔎 <b>FACEBOOK UID</b> │\n"
            "╰─────────────────────╯\n\n"
            "📥 <b>Vui lòng gửi link Facebook.</b>\n\n"
            "🔹 1 link → Get 1\n"
            "🔹 2 link → Get 2\n"
            "🔹 Nhiều link → Get lần lượt\n"
            "🔹 Không cần xuống dòng\n\n"
            "💡 Ví dụ:\n"
            "<code>https://facebook.com/a https://facebook.com/b https://facebook.com/c</code>\n\n"
            "🔄 Sau khi xong bot tiếp tục chờ link.\n"
            "🛑 <b>/stop</b> → Dừng",
            parse_mode="html",
        )

    @bot.on(events.NewMessage())
    async def getuid_receive(event):
        user_id = event.sender_id
        text = event.raw_text.strip()
        if not text or text.startswith("/"):
            return

        session = sessions.get(user_id)
        if not session or session.get("command") != "getuidfb":
            return
        if not session.get("running", False) or session.get("processing", False):
            return

        urls = extract_urls(text)
        if not urls:
            await event.reply(
                "❌ <b>Không tìm thấy link Facebook.</b>\n\n"
                "📥 Gửi một hoặc nhiều link.\n"
                "💡 Không cần xuống dòng.\n\n"
                "🔄 Bot vẫn đang chờ link.",
                parse_mode="html",
            )
            return

        session["processing"] = True
        track_current_task(user_id)
        total = len(urls)

        progress = await event.reply(
            "╭─────────────────────╮\n"
            "│ 🔎 <b>GET FACEBOOK UID</b> │\n"
            "╰─────────────────────╯\n\n"
            f"📊 <b>Đã nhận:</b> {total} link\n"
            "⚙️ <b>Đang xử lý...</b>",
            parse_mode="html",
        )

        results = []
        for index, url in enumerate(urls, start=1):
            session = sessions.get(user_id)
            if not session:
                return
            if not session.get("running", False):
                try:
                    await progress.edit(
                        "🛑 <b>Đã dừng Get UID.</b>\n\n"
                        "Dùng <code>/getuidfb</code> để bắt đầu lại.",
                        parse_mode="html",
                    )
                except Exception:
                    pass
                return

            if session.get("command") != "getuidfb":
                return

            try:
                await progress.edit(
                    "╭─────────────────────╮\n"
                    "│ 🔎 <b>GET FACEBOOK UID</b> │\n"
                    "╰─────────────────────╯\n\n"
                    f"📊 <b>Tiến trình:</b> {index}/{total}\n\n"
                    f"🔗 <code>{esc(url)}</code>\n\n"
                    "⏳ Đang lấy UID...",
                    parse_mode="html",
                )
            except Exception:
                pass

            try:
                result = await get_uid(event, url, notify_bot)
                if result.get("uid"):
                    clean_uid = clean_facebook_uid(result.get("uid"))
                    if clean_uid:
                        result["uid"] = clean_uid
                        result["success"] = True
                    else:
                        result["uid"] = None
                        result["success"] = False
                        result["error"] = "UID trả về không hợp lệ."
                results.append(result)
            except Exception as e:
                print(f"[GETUID LOOP] {e}")
                results.append(
                    {
                        "url": url,
                        "uid": None,
                        "success": False,
                        "error": str(e),
                    }
                )

        session = sessions.get(user_id)
        if not session or not session.get("running", False):
            return

        result_message = format_results(results)
        try:
            await progress.edit(result_message, parse_mode="html")
        except Exception as e:
            print(f"[RESULT EDIT] {e}")

        session["processing"] = False
        session["running"] = True
        session["command"] = "getuidfb"

# ============================================================
# CLI MAIN (CHẠY ĐỘC LẬP / TEST CLI)
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="FB UID / Entity Resolver V15 ULTRA"
    )
    parser.add_argument("urls", nargs="*", help="Facebook URLs to resolve")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--evidence", action="store_true", help="Show all evidence details"
    )
    parser.add_argument("--json", action="store_true", help="Output JSON result")
    parser.add_argument("--debug", action="store_true", help="Show debug logs")
    args = parser.parse_args()

    if not args.urls:
        parser.print_help()
        sys.exit(1)

    async def run():
        resolver = FacebookResolver(
            timeout=args.timeout,
            max_pages=args.max_pages,
            concurrency=args.concurrency,
            debug=args.debug,
        )
        for url in args.urls:
            res = await resolver.resolve(url)
            if args.json:
                print(json.dumps(res.to_json(), indent=2, ensure_ascii=False))
            else:
                print_result(res, show_evidence=args.evidence)

    asyncio.run(run())

if __name__ == "__main__":
    main()
