# ============================================================
# commands/getuidfb.py
# FACEBOOK UID / ENTITY RESOLVER V15 ULTRA - TELEGRAM EDITION
# HTTP only / Public content only
# Compatible with the existing Telethon command/task architecture.
# ============================================================

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import httpx
from telethon import events

from core.task_manager import replace_user_tasks, track_current_task

VERSION = "V15 ULTRA"
DEFAULT_TIMEOUT = 18.0
DEFAULT_MAX_PAGES = 8
DEFAULT_CONCURRENCY = 3
MAX_BODY_BYTES = 12 * 1024 * 1024
SESSION_KEY = "_dragon_sessions"

FB_HOSTS = {
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "mbasic.facebook.com", "web.facebook.com", "touch.facebook.com",
    "fb.watch", "www.fb.watch",
}
REDIRECT_HOSTS = {"l.facebook.com", "lm.facebook.com"}
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "fbclid", "rdid", "share_source", "ref", "refsrc", "mibextid", "__tn__",
}
RESERVED_PATHS = {
    "share", "watch", "stories", "story", "groups", "pages", "reel", "reels",
    "video", "videos", "photo", "photos", "events", "marketplace", "login",
    "logout", "recover", "help", "settings", "privacy", "policies", "plugins",
    "ajax", "api", "search", "gaming", "fundraisers", "business", "businesses",
    "profile.php",
}
ID_RE = re.compile(r"^\d{5,30}$")
PF_BID_RE = re.compile(r"^pfbid[A-Za-z0-9_-]+$", re.I)
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)

HEADERS = {
    'authority': 'www.facebook.com',
	'accept': '*/*',
	'accept-language': 'vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5',
	'content-type': 'application/x-www-form-urlencoded',
	'dnt': '1',
	'origin': 'https://www.facebook.com',
	'sec-ch-prefers-color-scheme': 'dark',
	'sec-ch-ua': '"Chromium";v="117", "Not;A=Brand";v="8"',
	'sec-ch-ua-full-version-list': '"Chromium";v="117.0.5938.157", "Not;A=Brand";v="8.0.0.0"',
	'sec-ch-ua-mobile': '?0',
	'sec-ch-ua-model': '""',
	'sec-ch-ua-platform': '"Windows"',
	'sec-ch-ua-platform-version': '"15.0.0"',
	'sec-fetch-dest': 'empty',
	'sec-fetch-mode': 'cors',
	'sec-fetch-site': 'same-origin',
	'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
	'x-fb-friendly-name': 'useCometConsentPromptEndOfFlowBatchedMutation',
}


# ============================================================
# COMMAND INFO
# ============================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "category": "🔎 FACEBOOK",
    "title": "Facebook UID / Entity V15",
    "description": "Lấy UID và phân tích loại Profile/Page/Group/Post/Reel/Video/Photo/Story từ link Facebook.",
    "usage": "/getuidfb",
    "examples": ["/getuidfb"],
    "details": [
        "Gửi /getuidfb rồi gửi một hoặc nhiều link Facebook.",
        "Không cần xuống dòng giữa các link.",
        "Bot tự động nhận diện URL Facebook.",
        "Sau khi xử lý xong bot tiếp tục chờ link.",
        "Dùng /stop để dừng.",
    ],
    "supported": [
        "Profile", "Page", "Group", "Post", "Group Post", "Page Post",
        "User Post", "Reel", "Video", "Photo", "Story", "Share/p", "Share/v",
        "Share/r", "pfbid",
    ],
}


# ============================================================
# DATA MODELS
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
# BASIC UTILITIES
# ============================================================

def clean_text(value) -> str:
    if value is None:
        return ""
    value = html.unescape(str(value))
    value = value.replace("\\/", "/").replace('\\"', '"')
    return value.strip()


def valid_numeric_id(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value if ID_RE.fullmatch(value) else None


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
    return host[4:] in FB_HOSTS if host.startswith("www.") else host in FB_HOSTS


def is_redirect_wrapper(url: str) -> bool:
    return host_of(url) in REDIRECT_HOSTS


def unwrap_facebook_url(url: str) -> str:
    url = normalize_url(url)
    try:
        p = urlparse(url)
        if p.netloc.lower().split(":")[0] in REDIRECT_HOSTS:
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
    if not value or value.lower() in RESERVED_PATHS:
        return False
    return bool(USERNAME_RE.fullmatch(value))


def unique(items):
    return list(dict.fromkeys(x for x in items if x))


def safe_context(text: str, start: int, end: int, limit: int = 1800) -> str:
    value = text[max(0, start):min(len(text), end)]
    value = value.replace("\n", " ").replace("\r", " ")
    return value[:limit]


def extract_urls(text: str) -> list[str]:
    """Extract multiple Facebook URLs, even when pasted back-to-back."""
    if not text:
        return []
    matches = list(re.finditer(r"https?://", text, re.I))
    if not matches:
        # Also accept facebook.com/... without scheme when useful.
        text = re.sub(r"(?<!\w)(?:www\.)?(?:facebook\.com|fb\.com|fb\.watch)/", lambda m: "https://" + m.group(0), text, flags=re.I)
        matches = list(re.finditer(r"https?://", text, re.I))
    urls = []
    for i, m in enumerate(matches):
        part = text[m.start():(matches[i + 1].start() if i + 1 < len(matches) else len(text))]
        for piece in re.split(r"\s+", part.strip()):
            piece = piece.strip().rstrip(".,!?;:)]}>\"'")
            if not piece:
                continue
            try:
                host = host_of(piece)
            except Exception:
                continue
            if host in FB_HOSTS or host in REDIRECT_HOSTS or host.endswith(".facebook.com") or host.endswith(".fb.watch"):
                if piece not in urls:
                    urls.append(piece)
    return urls


# ============================================================
# FACEBOOK ENCODED ID
# ============================================================

def base64_candidates(token: str):
    token = clean_text(token)
    if not token:
        return []
    variants = [token, token.replace("-", "+").replace("_", "/").replace("*", "/")]
    result = []
    for item in variants:
        item = re.sub(r"\s+", "", item)
        item += "=" * (-len(item) % 4)
        if item:
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
        m = re.fullmatch(r"S:_I(\d{5,30}):(\d{5,30})", decoded)
        if m:
            return EncodedID(token, decoded, True, "_I", m.group(1), m.group(2))
        m = re.fullmatch(r"S:([A-Za-z0-9_*]+):(\d{5,30})", decoded)
        if m:
            return EncodedID(token, decoded, True, m.group(1), None, m.group(2))
        m = re.fullmatch(r"S:([A-Za-z0-9_*]+):(.+)", decoded)
        if m:
            return EncodedID(token, decoded, False, m.group(1))
    return EncodedID(token=token, valid=False)


# ============================================================
# URL PARSER
# ============================================================

class URLParser:
    def __init__(self):
        self.evidence: list[Evidence] = []

    def add(self, field, value, kind, score, source, url="", context="", direct=False, role="", confidence=""):
        value = clean_text(value)
        if not value:
            return
        self.evidence.append(Evidence(field, value, kind, score, source, url, context, direct, role, confidence))

    def _parse_username_object(self, path, url):
        m = re.match(r"^/([^/?#]+)/(posts|videos|photos|reels)/([^/?#]+)", path, re.I)
        if not m:
            return
        username, kind, object_id = m.groups()
        username = normalize_username(username)
        kind = kind.lower()
        if not looks_like_username(username):
            return
        self.add("publisher_username", username, "typed_identity_url", 100, f"/{username}/{kind}", url, direct=True, role="PUBLISHER", confidence="EXACT")
        if kind == "posts" and (ID_RE.fullmatch(object_id) or PF_BID_RE.fullmatch(object_id)):
            self.add("post_id", object_id, "typed_object_url", 100, "username post URL", url, direct=True, role="POST", confidence="EXACT")
        elif kind == "videos" and ID_RE.fullmatch(object_id):
            self.add("video_id", object_id, "typed_object_url", 100, "username video URL", url, direct=True, role="VIDEO", confidence="EXACT")
        elif kind == "photos" and ID_RE.fullmatch(object_id):
            self.add("photo_id", object_id, "typed_object_url", 100, "username photo URL", url, direct=True, role="PHOTO", confidence="EXACT")
        elif kind == "reels" and ID_RE.fullmatch(object_id):
            self.add("reel_id", object_id, "typed_object_url", 100, "username reel URL", url, direct=True, role="REEL", confidence="EXACT")

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

        m = re.fullmatch(r"/profile\.php", lower)
        if m:
            uid = valid_numeric_id(qs.get("id", [None])[0])
            if uid:
                self.add("user_uid", uid, "typed_url", 100, "profile.php?id", url, direct=True, role="USER", confidence="EXACT")

        m = re.fullmatch(r"/people/[^/]+/(\d{5,30})/?", lower)
        if m:
            self.add("user_uid", m.group(1), "typed_url", 100, "people/name/UID", url, direct=True, role="USER", confidence="EXACT")

        m = re.fullmatch(r"/pages/[^/]+/(\d{5,30})/?", lower)
        if m:
            self.add("page_id", m.group(1), "typed_url", 100, "pages/name/ID", url, direct=True, role="PAGE", confidence="EXACT")

        m = re.match(r"^/groups/(\d{5,30})(?:/|$)", lower)
        if m:
            self.add("group_id", m.group(1), "typed_url", 100, "groups/ID", url, direct=True, role="GROUP", confidence="EXACT")

        m = re.match(r"^/groups/(\d{5,30})/(posts|permalink)/([A-Za-z0-9*_-]+)", lower)
        if m:
            self.add("group_id", m.group(1), "typed_url", 100, "group URL", url, direct=True, role="GROUP", confidence="EXACT")
            self.add("post_id", m.group(3), "typed_url", 100, "group post URL", url, direct=True, role="POST", confidence="EXACT")

        m = re.match(r"^/reels?/(\d{5,30})", lower)
        if m:
            self.add("reel_id", m.group(1), "typed_url", 100, "/reel/ID", url, direct=True, role="REEL", confidence="EXACT")

        if lower in {"/watch", "/watch/"}:
            vid = valid_numeric_id(qs.get("v", [None])[0])
            if vid:
                self.add("video_id", vid, "query_parameter", 100, "watch?v", url, direct=True, role="VIDEO", confidence="EXACT")

        if lower == "/video.php":
            vid = valid_numeric_id(qs.get("v", [None])[0])
            if vid:
                self.add("video_id", vid, "query_parameter", 100, "video.php?v", url, direct=True, role="VIDEO", confidence="EXACT")

        if lower == "/photo.php":
            pid = valid_numeric_id(qs.get("fbid", [None])[0])
            if pid:
                self.add("photo_id", pid, "query_parameter", 100, "photo.php?fbid", url, direct=True, role="PHOTO", confidence="EXACT")

        if lower in {"/permalink.php", "/story.php"}:
            story_fbid = qs.get("story_fbid", [None])[0]
            owner_id = valid_numeric_id(qs.get("id", [None])[0])
            if story_fbid:
                if ID_RE.fullmatch(story_fbid):
                    self.add("post_id", story_fbid, "query_parameter", 100, "story_fbid", url, direct=True, role="POST", confidence="EXACT")
                elif PF_BID_RE.fullmatch(story_fbid):
                    self.add("post_id", story_fbid, "opaque_token", 95, "story_fbid", url, direct=True, role="POST_TOKEN", confidence="EXACT")
            if owner_id:
                self.add("publisher_id", owner_id, "query_parameter", 88, "story owner", url, direct=True, role="PUBLISHER_CANDIDATE", confidence="HIGH")

        m = re.match(r"^/stories/(\d{5,30})/([^/?#]+)", path, re.I)
        if m:
            actor_id, token = m.groups()
            self.add("decoded_actor_id", actor_id, "story_path", 100, "stories actor", url, direct=True, role="ACTOR", confidence="EXACT")
            self.add("story_id", token, "story_token", 100, "stories token", url, direct=True, role="STORY_TOKEN", confidence="EXACT")
            decoded = decode_fb_encoded_id(token)
            if decoded:
                if decoded.decoded:
                    self.add("decoded_payload", decoded.decoded, "encoded_id", 100, "Facebook encoded token", url, direct=True, role="ENCODED", confidence="EXACT")
                if decoded.object_id:
                    self.add("decoded_id", decoded.object_id, "encoded_id", 100, "encoded object", url, direct=True, role="OBJECT", confidence="EXACT")
                    self.add("post_id", decoded.object_id, "encoded_id", 98, "story decoded object", url, direct=True, role="POST", confidence="VERY_HIGH")
                if decoded.encoded_type:
                    self.add("decoded_type", decoded.encoded_type, "encoded_id", 100, "encoded type", url, direct=True, role="ENCODED", confidence="EXACT")
                if decoded.actor_id:
                    self.add("decoded_actor_id", decoded.actor_id, "encoded_id", 100, "encoded actor", url, direct=True, role="ACTOR", confidence="EXACT")

        self._parse_username_object(path, url)

        m = re.fullmatch(r"/([^/?#]+)/?", path, re.I)
        if m:
            username = normalize_username(m.group(1))
            if looks_like_username(username):
                self.add("publisher_username", username, "profile_path", 90, "Facebook profile path", url, direct=True, role="PUBLISHER", confidence="HIGH")

        m = re.match(r"^/p/([^/?#]+)", path, re.I)
        if m and PF_BID_RE.fullmatch(m.group(1)):
            self.add("post_id", m.group(1), "opaque_token", 95, "/p/pfbid", url, direct=True, role="POST_TOKEN", confidence="EXACT")

        m = re.match(r"^/share(?:/(p|v|r))?/?([^/?#]+)?", path, re.I)
        if m:
            share_kind, token = m.groups()
            if token:
                self.add("share_token", token, "share_url", 90, "Facebook share URL", url, direct=True, role="SHARE", confidence="HIGH")
            hints = {"p": "SHARE_POST", "v": "SHARE_VIDEO", "r": "SHARE_REEL"}
            self.add("url_type_hint", hints.get((share_kind or "").lower(), "SHARE"), "typed_url", 100, "share URL", url, direct=True)

        media = qs.get("media_fbid", [None])[0]
        if media and (ID_RE.fullmatch(media) or PF_BID_RE.fullmatch(media)):
            self.add("media_fbid", media, "query_parameter", 90, "query:media_fbid", url, direct=True, role="OBJECT", confidence="HIGH")

        candidate = valid_numeric_id(qs.get("id", [None])[0])
        if candidate and lower != "/profile.php":
            self.add("publisher_id", candidate, "generic_query_candidate", 45, "generic ?id", url, role="PUBLISHER_CANDIDATE", confidence="LOW")


# ============================================================
# METADATA / HTML / IDENTITY ENGINES
# ============================================================

class MetadataParser:
    META_RE = re.compile(r"<meta\b[^>]*>", re.I)
    LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
    TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
    SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.I | re.S)
    ATTR_RE = re.compile(r"([:\\w-]+)\\s*=\\s*[\"'](.*?)[\"']", re.I | re.S)

    def parse(self, text: str):
        result = {"title": "", "og_url": "", "og_type": "", "canonical": "", "app_links": [], "deep_links": [], "jsonld": []}
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
                if content.startswith("fb://"):
                    result["deep_links"].append(content)
        for tag in self.LINK_RE.findall(text):
            attrs = {k.lower(): clean_text(v) for k, v in self.ATTR_RE.findall(tag)}
            if "canonical" in attrs.get("rel", "").lower() and attrs.get("href"):
                result["canonical"] = attrs["href"]
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


class HTMLIDEngine:
    PATTERNS = {
        "user_uid": [r'"profile_id"\s*:\s*"?(\d{5,30})"?', r'"user_id"\s*:\s*"?(\d{5,30})"?', r'"userID"\s*:\s*"?(\d{5,30})"?', r'"profileId"\s*:\s*"?(\d{5,30})"?', r'"profile_owner_id"\s*:\s*"?(\d{5,30})"?'],
        "page_id": [r'"page_id"\s*:\s*"?(\d{5,30})"?', r'"pageID"\s*:\s*"?(\d{5,30})"?'],
        "group_id": [r'"group_id"\s*:\s*"?(\d{5,30})"?', r'"groupID"\s*:\s*"?(\d{5,30})"?'],
        "actor_id": [r'"actor_id"\s*:\s*"?(\d{5,30})"?', r'"actorID"\s*:\s*"?(\d{5,30})"?'],
        "author_id": [r'"author_id"\s*:\s*"?(\d{5,30})"?', r'"authorID"\s*:\s*"?(\d{5,30})"?'],
        "owner_id": [r'"owner_id"\s*:\s*"?(\d{5,30})"?', r'"ownerID"\s*:\s*"?(\d{5,30})"?'],
        "publisher_id": [r'"publisher_id"\s*:\s*"?(\d{5,30})"?', r'"publisherID"\s*:\s*"?(\d{5,30})"?'],
        "entity_id": [r'"entity_id"\s*:\s*"?(\d{5,30})"?', r'"entityID"\s*:\s*"?(\d{5,30})"?'],
        "post_id": [r'"post_id"\s*:\s*"?(\d{5,30})"?', r'"story_fbid"\s*:\s*"?(\d{5,30})"?', r'"postID"\s*:\s*"?(\d{5,30})"?'],
        "media_fbid": [r'"media_fbid"\s*:\s*"?(\d{5,30})"?', r'"mediaFbid"\s*:\s*"?(\d{5,30})"?'],
        "video_id": [r'"video_id"\s*:\s*"?(\d{5,30})"?', r'"videoID"\s*:\s*"?(\d{5,30})"?'],
        "reel_id": [r'"reel_id"\s*:\s*"?(\d{5,30})"?', r'"reelID"\s*:\s*"?(\d{5,30})"?'],
        "photo_id": [r'"photo_id"\s*:\s*"?(\d{5,30})"?', r'"photoID"\s*:\s*"?(\d{5,30})"?'],
    }
    URL_PATTERNS = {
        "post_id": [r"/posts/(\d{5,30})", r"/permalink/(\d{5,30})", r"story_fbid[=:/\"]+(\d{5,30})"],
        "video_id": [r"/videos/(\d{5,30})", r"video_id[=:/\"]+(\d{5,30})"],
        "reel_id": [r"/reel/(\d{5,30})", r"reel_id[=:/\"]+(\d{5,30})"],
        "photo_id": [r"/photo(?:\.php)?[^0-9]{0,50}(\d{5,30})", r"photo_id[=:/\"]+(\d{5,30})"],
    }

    def parse(self, text, url):
        result = []
        if not text:
            return result
        for field_name, patterns in self.PATTERNS.items():
            for pattern in patterns:
                for m in re.finditer(pattern, text, re.I):
                    value = m.group(1)
                    role = "USER" if field_name == "user_uid" else ("PAGE" if field_name == "page_id" else ("GROUP" if field_name == "group_id" else "OBJECT"))
                    score = 58 if field_name == "user_uid" else 50 if field_name in {"page_id", "group_id"} else 48
                    result.append(Evidence(field_name, value, "typed_html_identity" if field_name == "user_uid" else "typed_html_object", score, f"HTML:{field_name}", url, safe_context(text, m.start()-900, m.end()+900), False, role, "MEDIUM"))
        for field_name, patterns in self.URL_PATTERNS.items():
            for pattern in patterns:
                for m in re.finditer(pattern, text, re.I):
                    result.append(Evidence(field_name, m.group(1), "embedded_url", 62, f"HTML_URL:{field_name}", url, safe_context(text, m.start()-700, m.end()+700), False, "OBJECT", "MEDIUM"))
        return result


class IdentityEngine:
    ID_KEYS = ("profile_id", "user_id", "userID", "profileId", "profile_owner_id", "profileOwnerID")

    def parse(self, text, url, expected_username=None):
        result = []
        if not text:
            return result
        for key in self.ID_KEYS:
            patterns = [rf'"{re.escape(key)}"\s*:\s*"(\d{{5,30}})"', rf'"{re.escape(key)}"\s*:\s*(\d{{5,30}})']
            for pattern in patterns:
                for m in re.finditer(pattern, text, re.I):
                    score = 84 if key == "profile_id" else 78
                    result.append(Evidence("user_uid", m.group(1), "profile_identity", score, f"IDENTITY:{key}", url, safe_context(text, m.start()-1400, m.end()+1400), True, "USER", "HIGH"))
        username = normalize_username(expected_username or "")
        if not username:
            return result
        username_patterns = [rf'"username"\s*:\s*"{re.escape(username)}"', rf'"vanity"\s*:\s*"{re.escape(username)}"', rf'"vanity_name"\s*:\s*"{re.escape(username)}"', rf'/{re.escape(username)}(?:["\'/?&#]|$)']
        username_hits = []
        for pattern in username_patterns:
            username_hits.extend(re.finditer(pattern, text, re.I))
        if username_hits:
            result.append(Evidence("publisher_username", username, "profile_username", 96, "PROFILE:username", url, f"username={username}", True, "PUBLISHER", "HIGH"))
        for um in username_hits:
            context = safe_context(text, um.start()-3500, um.end()+3500, 7000)
            for key in self.ID_KEYS:
                p = rf'"{re.escape(key)}"\s*:\s*"?(\d{{5,30}})"?'
                for im in re.finditer(p, context, re.I):
                    result.append(Evidence("user_uid", im.group(1), "username_uid_correlation", 92, f"USERNAME:{username}+{key}", url, context[:1800], False, "USER", "VERY_HIGH"))
        return result


class CorrelationEngine:
    ID_KEYS = IdentityEngine.ID_KEYS
    def correlate(self, text, url, expected_username=None):
        result = []
        username = normalize_username(expected_username or "")
        if not text or not username:
            return result
        patterns = [rf'/{re.escape(username)}(?:["\'/?&#]|$)', rf'"username"\s*:\s*"{re.escape(username)}"', rf'"vanity"\s*:\s*"{re.escape(username)}"']
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.I):
                context = safe_context(text, m.start()-2800, m.end()+2800, 5600)
                for key in self.ID_KEYS:
                    p = rf'"{re.escape(key)}"\s*:\s*"?(\d{{5,30}})"?'
                    for im in re.finditer(p, context, re.I):
                        result.append(Evidence("user_uid", im.group(1), "username_local_correlation", 82, f"nearby:{username}+{key}", url, context[:1800], False, "USER", "HIGH"))
        return result


class DeepLinkEngine:
    PATTERNS = [("user_uid", re.compile(r"fb://profile/(\d{5,30})", re.I), "USER"), ("page_id", re.compile(r"fb://page/(\d{5,30})", re.I), "PAGE"), ("group_id", re.compile(r"fb://group/(\d{5,30})", re.I), "GROUP")]
    def parse(self, values, source_url):
        result = []
        for value in values:
            for field_name, pattern, role in self.PATTERNS:
                for m in pattern.finditer(clean_text(value)):
                    result.append(Evidence(field_name, m.group(1), "deep_link", 100, "Facebook App Link", source_url, clean_text(value), True, role, "EXACT"))
        return result


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
                if isinstance(author.get("url"), str):
                    result.append(Evidence("publisher_url", author["url"], "jsonld", 58, "JSON-LD author.url", url, str(author)[:1800], False, "PUBLISHER", "MEDIUM"))
                if author.get("name"):
                    result.append(Evidence("publisher_name", str(author["name"]), "jsonld", 20, "JSON-LD author.name", url, str(author)[:1000], False, "PUBLISHER", "LOW"))
            if isinstance(item.get("url"), str):
                result.append(Evidence("object_url", item["url"], "jsonld", 55, "JSON-LD url", url, role="OBJECT", confidence="MEDIUM"))
        return result


# ============================================================
# HTTP CLIENT
# ============================================================

class HTTPClient:
    def __init__(self, timeout=DEFAULT_TIMEOUT, debug=False):
        self.timeout = timeout
        self.debug = debug
        self.client = None
        self.headers = dict(HEADERS)

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
        if not self.client or not url.startswith(("http://", "https://")):
            return None
        last_error = None
        for attempt in range(3):
            try:
                response = await self.client.get(url)
                if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                raw = response.content[:MAX_BODY_BYTES]
                text = raw.decode(response.encoding or "utf-8", errors="replace")
                meta = MetadataParser().parse(text)
                return PageSnapshot(
                    requested_url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    title=meta["title"], og_url=meta["og_url"], og_type=meta["og_type"], canonical=meta["canonical"],
                    html=text, history=[str(x.url) for x in response.history], app_links=meta["app_links"], deep_links=meta["deep_links"],
                    jsonld=meta["jsonld"], headers={k: response.headers[k] for k in ("content-type", "cache-control", "location", "server") if k in response.headers},
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                await asyncio.sleep(0.6 * (attempt + 1))
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.4 * (attempt + 1))
        if self.debug and last_error:
            print(f"[DEBUG] request failed: {last_error!r}")
        return None


# ============================================================
# RESOLVER
# ============================================================

class FacebookResolver:
    def __init__(self, timeout=DEFAULT_TIMEOUT, max_pages=DEFAULT_MAX_PAGES, concurrency=DEFAULT_CONCURRENCY, debug=False):
        self.timeout = timeout
        self.max_pages = max(1, max_pages)
        self.concurrency = max(1, concurrency)
        self.debug = debug
        self.visited: set[str] = set()
        self.cache: dict[str, PageSnapshot] = {}

    def log(self, *args):
        if self.debug:
            print("[DEBUG]", *args)

    async def fetch_one(self, client, url):
        url = canonicalize_url(url)
        if not url or url in self.visited or len(self.visited) >= self.max_pages:
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
        return snapshot

    def analyze_snapshot(self, snapshot, parser):
        for u in unique([snapshot.final_url, *snapshot.history, snapshot.og_url, snapshot.canonical, *snapshot.app_links, *snapshot.deep_links]):
            if u:
                parser.parse(u)
        parser.evidence.extend(HTMLIDEngine().parse(snapshot.html, snapshot.final_url))
        usernames = {normalize_username(e.value) for e in parser.evidence if e.field == "publisher_username" and e.value}
        try:
            first = urlparse(snapshot.final_url).path.strip("/").split("/")[0]
            if looks_like_username(first):
                usernames.add(first)
        except Exception:
            pass
        identity = IdentityEngine()
        correlation = CorrelationEngine()
        for username in usernames:
            parser.evidence.extend(identity.parse(snapshot.html, snapshot.final_url, username))
            parser.evidence.extend(correlation.correlate(snapshot.html, snapshot.final_url, username))
        parser.evidence.extend(DeepLinkEngine().parse(snapshot.deep_links, snapshot.final_url))
        parser.evidence.extend(JSONLDEngine().parse(snapshot.jsonld, snapshot.final_url))

    def discover_urls(self, snapshot):
        urls = [snapshot.og_url, snapshot.canonical]
        for x in snapshot.app_links + snapshot.deep_links:
            if x and not x.startswith("fb://"):
                urls.append(x)
        for item in snapshot.jsonld:
            author = item.get("author")
            if isinstance(author, dict) and isinstance(author.get("url"), str) and same_fb_host(author["url"]):
                urls.append(author["url"])
            if isinstance(item.get("url"), str) and same_fb_host(item["url"]):
                urls.append(item["url"])
        return unique(canonicalize_url(x) for x in urls if x)

    def get_username(self, parser):
        candidates = [e for e in parser.evidence if e.field == "publisher_username"]
        if not candidates:
            return None
        candidates.sort(key=lambda e: (e.score, int(e.direct), int(e.confidence in {"EXACT", "VERY_HIGH"})), reverse=True)
        return normalize_username(candidates[0].value)

    def derive_profile_url(self, parser):
        username = self.get_username(parser)
        return "https://www.facebook.com/" + quote(username) if username else None

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
        return max(values, key=lambda e: (e.score, int(e.direct), int(e.confidence in {"EXACT", "VERY_HIGH"}))) if values else None

    def classify_url_type(self, evidence, requested_url):
        hints = [e.value for e in evidence if e.field == "url_type_hint"]
        if hints:
            return hints[0]
        try:
            path = unquote(urlparse(requested_url).path).lower()
            qs = parse_qs(urlparse(requested_url).query)
        except Exception:
            return "UNKNOWN"
        if re.match(r"^/groups/\d+/(posts|permalink)/", path): return "GROUP_POST"
        if re.match(r"^/groups/\d+/?$", path): return "GROUP"
        if re.match(r"^/pages/[^/]+/\d+/?$", path): return "PAGE"
        if path == "/profile.php" or re.match(r"^/people/[^/]+/\d+/?$", path): return "PROFILE"
        if re.match(r"^/stories/\d+/", path): return "STORY"
        if re.match(r"^/reels?/\d+", path) or re.match(r"^/[^/]+/reels?/\d+", path): return "REEL"
        if path in {"/watch", "/watch/"} and "v" in qs: return "WATCH_VIDEO"
        if path == "/video.php": return "VIDEO"
        if path == "/photo.php": return "PHOTO"
        m = re.match(r"^/[^/]+/(posts|videos|photos|reels)/", path)
        if m: return {"posts":"USER_POST", "videos":"USER_VIDEO", "photos":"USER_PHOTO", "reels":"USER_REEL"}[m.group(1)]
        if re.match(r"^/p/", path): return "POST"
        if re.match(r"^/share/", path): return "SHARE"
        if re.fullmatch(r"/[^/]+/?", path): return "PROFILE_CANDIDATE"
        return "UNKNOWN"

    def build_result(self, requested_url, parser, snapshots):
        parser.evidence = self.dedupe_evidence(parser.evidence)
        result = Result(requested_url=requested_url)
        chain = [requested_url]
        for snapshot in snapshots:
            chain.extend(snapshot.history)
            if snapshot.final_url: chain.append(snapshot.final_url)
        result.crawl_chain = unique(chain)
        result.resolved_url = snapshots[-1].final_url if snapshots else requested_url
        result.url_type = self.classify_url_type(parser.evidence, requested_url)

        for field_name in ("post_id", "story_id", "video_id", "reel_id", "photo_id", "media_fbid", "decoded_id", "decoded_payload", "decoded_type", "decoded_actor_id", "group_id", "page_id", "publisher_id", "publisher_username", "share_token"):
            ev = self.best(parser.evidence, field_name)
            if ev: setattr(result, field_name, ev.value)

        user_evidence = [e for e in parser.evidence if e.field == "user_uid" and valid_numeric_id(e.value)]
        stats = defaultdict(lambda: {"max":0, "profile":False, "correlation":False, "deep":False, "sources":set(), "direct":False})
        for ev in user_evidence:
            s = stats[ev.value]
            s["max"] = max(s["max"], ev.score)
            s["sources"].add(ev.source)
            s["direct"] |= ev.direct
            if ev.kind == "profile_identity": s["profile"] = True
            if ev.kind in {"username_uid_correlation", "username_local_correlation"}: s["correlation"] = True
            if ev.kind == "deep_link": s["deep"] = True
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

        if result.group_id: result.publisher_type = "GROUP"
        elif result.page_id: result.publisher_type = "PAGE"
        elif result.user_uid: result.publisher_type = "USER"
        elif result.publisher_username: result.publisher_type = "USER_CANDIDATE"
        elif result.publisher_id: result.publisher_type = "UNKNOWN"

        if result.group_id and result.post_id: result.entity_type, result.entity_id = "GROUP_POST", result.post_id
        elif result.page_id and result.post_id: result.entity_type, result.entity_id = "PAGE_POST", result.post_id
        elif result.post_id and result.user_uid: result.entity_type, result.entity_id = "USER_POST", result.post_id
        elif result.post_id and result.publisher_username: result.entity_type, result.entity_id = "USER_POST_UNVERIFIED", result.post_id
        elif result.story_id: result.entity_type, result.entity_id = "STORY", (result.decoded_id or result.story_id)
        elif result.reel_id: result.entity_type, result.entity_id = "REEL", result.reel_id
        elif result.video_id and result.page_id: result.entity_type, result.entity_id = "PAGE_VIDEO", result.video_id
        elif result.video_id and result.user_uid: result.entity_type, result.entity_id = "USER_VIDEO", result.video_id
        elif result.video_id: result.entity_type, result.entity_id = "VIDEO", result.video_id
        elif result.photo_id and result.page_id: result.entity_type, result.entity_id = "PAGE_PHOTO", result.photo_id
        elif result.photo_id and result.user_uid: result.entity_type, result.entity_id = "USER_PHOTO", result.photo_id
        elif result.photo_id: result.entity_type, result.entity_id = "PHOTO", result.photo_id
        elif result.group_id: result.entity_type, result.entity_id = "GROUP", result.group_id
        elif result.page_id: result.entity_type, result.entity_id = "PAGE", result.page_id
        elif result.user_uid: result.entity_type, result.entity_id = "USER", result.user_uid
        elif result.post_id: result.entity_type, result.entity_id = "POST", result.post_id
        elif result.publisher_username: result.entity_type, result.entity_id = "USER_PROFILE", result.publisher_username

        if result.user_uid:
            result.identity_url = "https://www.facebook.com/" + quote(result.publisher_username) if result.publisher_username else f"https://www.facebook.com/profile.php?id={result.user_uid}"
        elif result.publisher_username:
            result.identity_url = "https://www.facebook.com/" + quote(result.publisher_username)
        for snapshot in snapshots:
            if snapshot.title:
                result.title = snapshot.title
                break

        strong_uids = {uid for uid, s in stats.items() if s["profile"] or s["correlation"]}
        if result.publisher_id and result.user_uid and result.publisher_id != result.user_uid and result.publisher_id in strong_uids:
            result.warnings.append("Publisher ID conflicts with verified USER UID.")
            result.user_uid = None
            result.user_verification = "CONFLICT"

        combined = "\n".join((s.title + "\n" + s.html[:200000]).lower() for s in snapshots)
        if any(t in combined for t in ("log in to facebook", "you must log in", "content isn't available", "this content isn't available", "page isn't available", "something went wrong", "checkpoint")):
            result.warnings.append("Facebook returned a login/restriction/unavailable page.")

        confidence = 0
        if result.url_type != "UNKNOWN": confidence += 20
        if result.entity_id: confidence += 20
        if result.resolved_url: confidence += 10
        if result.user_verification == "VERIFIED": confidence += 35
        elif result.user_verification == "AMBIGUOUS": confidence += 8
        elif result.user_verification == "CONFLICT": confidence = 15
        if result.publisher_username: confidence += 8
        if any((result.post_id, result.video_id, result.reel_id, result.photo_id, result.story_id)): confidence += 10
        if any(e.direct and e.confidence == "EXACT" for e in parser.evidence): confidence += 5
        if result.user_verification == "CONFLICT": confidence -= 30
        if result.user_verification == "NOT VERIFIED" and result.entity_type == "USER_POST_UNVERIFIED": confidence = min(confidence, 70)
        result.confidence = max(0, min(100, confidence))

        if result.entity_id and result.user_verification == "VERIFIED": result.status, result.success = "EXACT", True
        elif result.entity_id: result.status, result.success = "PARTIAL", True
        else: result.status, result.success = "UNRESOLVED", False
        result.evidence = sorted(parser.evidence, key=lambda e: (e.field, -e.score, not e.direct))
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
            candidates = self.discover_urls(first)
            profile_url = self.derive_profile_url(parser)
            if profile_url: candidates.insert(0, profile_url)
            candidates.extend(first.history)
            if first.final_url: candidates.append(first.final_url)
            candidates = [canonicalize_url(x) for x in unique(candidates) if x]
            candidates = [x for x in candidates if same_fb_host(x) or is_redirect_wrapper(x)]
            candidates = candidates[:max(0, self.max_pages - 1)]
            semaphore = asyncio.Semaphore(self.concurrency)
            async def worker(u):
                async with semaphore:
                    return await self.fetch_one(client, u)
            if candidates:
                fetched = await asyncio.gather(*(worker(x) for x in candidates), return_exceptions=True)
                for item in fetched:
                    if isinstance(item, PageSnapshot):
                        snapshots.append(item)
                        self.analyze_snapshot(item, parser)
        return self.build_result(original_url, parser, snapshots)


# ============================================================
# TELEGRAM FORMATTING
# ============================================================

def esc(value):
    return html.escape(str(value))


def short_url(url, limit=180):
    url = str(url or "")
    return url if len(url) <= limit else url[:limit-3] + "..."


def label_type(result: Result) -> str:
    names = {
        "PROFILE":"👤 PROFILE", "PROFILE_CANDIDATE":"👤 PROFILE?", "PAGE":"📄 PAGE", "GROUP":"👥 GROUP",
        "GROUP_POST":"👥 GROUP POST", "PAGE_POST":"📄 PAGE POST", "USER_POST":"👤 USER POST",
        "USER_POST_UNVERIFIED":"👤 USER POST", "REEL":"🎬 REEL", "WATCH_VIDEO":"🎥 VIDEO",
        "VIDEO":"🎥 VIDEO", "USER_VIDEO":"👤 USER VIDEO", "PAGE_VIDEO":"📄 PAGE VIDEO",
        "PHOTO":"🖼️ PHOTO", "USER_PHOTO":"👤 USER PHOTO", "PAGE_PHOTO":"📄 PAGE PHOTO",
        "STORY":"⭕ STORY", "POST":"📝 POST", "SHARE_POST":"🔗 SHARE POST", "SHARE_VIDEO":"🔗 SHARE VIDEO",
        "SHARE_REEL":"🔗 SHARE REEL", "SHARE":"🔗 SHARE",
    }
    return names.get(result.url_type, result.url_type or "UNKNOWN")


def format_result(result: Result, index=None) -> str:
    prefix = f"<b>{index}.</b> " if index is not None else ""
    lines = [
        f"{prefix}🔎 <b>FACEBOOK V15 ULTRA</b>",
        f"📌 <b>Loại:</b> {esc(label_type(result))}",
        f"📊 <b>Trạng thái:</b> <code>{esc(result.status)}</code>   🎯 <b>{result.confidence}%</b>",
    ]
    if result.user_uid:
        lines.append(f"🆔 <b>UID:</b> <code>{esc(result.user_uid)}</code>  ✅ VERIFIED")
    elif result.user_verification == "CONFLICT":
        lines.append("🆔 <b>UID:</b> ⚠️ CONFLICT")
    else:
        lines.append("🆔 <b>UID:</b> <i>Không xác minh được</i>")
    if result.publisher_username: lines.append(f"👤 <b>Username:</b> <code>{esc(result.publisher_username)}</code>")
    if result.identity_url: lines.append(f'🌐 <a href="{esc(result.identity_url)}">Identity</a>')
    if result.entity_id: lines.append(f"🔹 <b>Entity:</b> <code>{esc(result.entity_id)}</code> <code>{esc(result.entity_type or '')}</code>")
    if result.group_id: lines.append(f"👥 <b>Group ID:</b> <code>{esc(result.group_id)}</code>")
    if result.page_id: lines.append(f"📄 <b>Page ID:</b> <code>{esc(result.page_id)}</code>")
    if result.post_id: lines.append(f"📝 <b>Post ID:</b> <code>{esc(result.post_id)}</code>")
    if result.story_id: lines.append(f"⭕ <b>Story ID:</b> <code>{esc(result.story_id)}</code>")
    if result.video_id: lines.append(f"🎥 <b>Video ID:</b> <code>{esc(result.video_id)}</code>")
    if result.reel_id: lines.append(f"🎬 <b>Reel ID:</b> <code>{esc(result.reel_id)}</code>")
    if result.photo_id: lines.append(f"🖼️ <b>Photo ID:</b> <code>{esc(result.photo_id)}</code>")
    if result.media_fbid: lines.append(f"🧩 <b>Media FBID:</b> <code>{esc(result.media_fbid)}</code>")
    if result.decoded_id: lines.append(f"🔓 <b>Decoded ID:</b> <code>{esc(result.decoded_id)}</code>")
    if result.decoded_actor_id: lines.append(f"🎭 <b>Actor ID:</b> <code>{esc(result.decoded_actor_id)}</code>")
    if result.share_token: lines.append(f"🔗 <b>Share token:</b> <code>{esc(result.share_token)}</code>")
    if result.title: lines.append(f"🏷️ <b>Title:</b> {esc(result.title[:250])}")
    if result.warnings:
        lines.append("⚠️ <b>Cảnh báo:</b>")
        lines.extend(f"• {esc(w)}" for w in result.warnings[:4])
    lines.append(f'🔗 <a href="{esc(result.requested_url)}">Mở Facebook</a>')
    return "\n".join(lines)


def format_results(results):
    success = sum(bool(x.success) for x in results)
    lines = [
        "╭──────────────────────────╮",
        "│  🔎 <b>FACEBOOK V15 ULTRA</b> │",
        "╰──────────────────────────╯",
        "",
        f"📊 Tổng: <b>{len(results)}</b>   ✅ {success}   ❌ {len(results)-success}",
        "",
    ]
    for i, result in enumerate(results, 1):
        lines.append(format_result(result, i))
        lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.extend(["🔄 <b>GET UID SẴN SÀNG</b>", "📥 Gửi link Facebook tiếp theo.", "🛑 <code>/stop</code> để dừng."])
    return "\n".join(lines)


# ============================================================
# TELEGRAM REGISTER
# ============================================================

def get_sessions(bot):
    if not hasattr(bot, SESSION_KEY):
        setattr(bot, SESSION_KEY, {})
    return getattr(bot, SESSION_KEY)


def register(bot, notify_bot):
    sessions = get_sessions(bot)

    @bot.on(events.NewMessage(pattern=r"^/getuidfb(?:@\w+)?$"))
    async def getuid_start(event):
        user_id = event.sender_id
        await replace_user_tasks(user_id)
        for attr in ("_dragon_sessions", "_dragon_download_sessions"):
            store = getattr(bot, attr, None)
            if isinstance(store, dict):
                store.pop(user_id, None)
        try:
            from core.power.session import clear_session
            clear_session(bot, user_id)
        except Exception:
            pass
        sessions[user_id] = {"command":"getuidfb", "running":True, "processing":False}
        await event.reply(
            "╭─────────────────────╮\n"
            "│  🔎 <b>FACEBOOK V15</b>  │\n"
            "╰─────────────────────╯\n\n"
            "📥 <b>Gửi link Facebook.</b>\n\n"
            "🔹 Profile / Page / Group\n"
            "🔹 Post / Group Post / Page Post\n"
            "🔹 Reel / Video / Photo / Story\n"
            "🔹 share/p, share/v, share/r\n"
            "🔹 pfbid + encoded token\n\n"
            "💡 Có thể gửi nhiều link cùng lúc, không cần xuống dòng.\n"
            "🔄 Xong một batch bot tiếp tục chờ link.\n"
            "🛑 <b>/stop</b> → Dừng",
            parse_mode="html",
        )

    @bot.on(events.NewMessage())
    async def getuid_receive(event):
        user_id = event.sender_id
        text = (event.raw_text or "").strip()
        if not text or text.startswith("/"):
            return
        session = sessions.get(user_id)
        if not session or session.get("command") != "getuidfb" or not session.get("running", False):
            return
        if session.get("processing", False):
            return
        urls = extract_urls(text)
        if not urls:
            # Only answer if this is clearly an active getuid session.
            await event.reply("❌ <b>Không tìm thấy link Facebook.</b>\n\n📥 Hãy gửi URL Facebook hợp lệ.", parse_mode="html")
            return
        session["processing"] = True
        track_current_task(user_id)
        total = len(urls)
        progress = await event.reply(
            "╭─────────────────────╮\n│  🔎 <b>GET FACEBOOK V15</b> │\n╰─────────────────────╯\n\n"
            f"📊 Đã nhận: <b>{total}</b> link\n⚙️ Đang phân tích HTTP...",
            parse_mode="html",
        )
        results = []
        for idx, url in enumerate(urls, 1):
            current = sessions.get(user_id)
            if not current or not current.get("running", False) or current.get("command") != "getuidfb":
                try: await progress.edit("🛑 <b>Đã dừng Get UID.</b>\n\nDùng <code>/getuidfb</code> để bắt đầu lại.", parse_mode="html")
                except Exception: pass
                return
            try:
                await progress.edit(
                    "╭─────────────────────╮\n│  🔎 <b>GET FACEBOOK V15</b> │\n╰─────────────────────╯\n\n"
                    f"📊 Tiến trình: <b>{idx}/{total}</b>\n\n🔗 <code>{esc(short_url(url))}</code>\n\n⏳ Đang phân tích URL + HTML + metadata...",
                    parse_mode="html",
                )
            except Exception:
                pass
            try:
                resolver = FacebookResolver(timeout=DEFAULT_TIMEOUT, max_pages=DEFAULT_MAX_PAGES, concurrency=DEFAULT_CONCURRENCY)
                result = await resolver.resolve(url)
                results.append(result)
                if notify_bot:
                    try:
                        admin_result = (
                            f"UID: {result.user_uid or 'Không xác minh'}\n"
                            f"Type: {result.url_type}\n"
                            f"Status: {result.status}\n"
                            f"Entity: {result.entity_id or 'N/A'}\n"
                            f"Link: {url}"
                        )
                        await notify_bot(await event.get_sender(), "/getuidfb", result=admin_result)
                    except Exception as e:
                        print(f"[UID ADMIN] {e}")
            except Exception as e:
                print(f"[GETUID V15] {e}")
                results.append(Result(requested_url=url, status="ERROR", success=False, warnings=[str(e)]))
        current = sessions.get(user_id)
        if not current or not current.get("running", False):
            return
        try:
            await progress.edit(format_results(results), parse_mode="html", link_preview=False)
        except Exception as e:
            print(f"[RESULT EDIT] {e}")
            try:
                await event.reply(format_results(results), parse_mode="html", link_preview=False)
            except Exception:
                pass
        current["processing"] = False
        current["running"] = True
        current["command"] = "getuidfb"

    return getuid_start, getuid_receive


# ============================================================
# OPTIONAL DIRECT API FOR OTHER BOT MODULES
# ============================================================

async def resolve_facebook_url(url: str, timeout=DEFAULT_TIMEOUT, max_pages=DEFAULT_MAX_PAGES, concurrency=DEFAULT_CONCURRENCY) -> dict:
    resolver = FacebookResolver(timeout=timeout, max_pages=max_pages, concurrency=concurrency)
    result = await resolver.resolve(url)
    return result.to_json()


# ============================================================
# LOCAL SELF TEST
# ============================================================

def self_test():
    passed = total = 0
    def check(name, cond):
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    token = "UzpfSVNDOjQ0ODUxMTU5ODE3NDY3NjY="
    d = decode_fb_encoded_id(token)
    check("Base64 valid", bool(d and d.valid))
    check("Base64 payload", bool(d and d.decoded == "S:ISC:4485115981746766"))
    check("Base64 object", bool(d and d.object_id == "4485115981746766"))

    group_url = "https://www.facebook.com/groups/157327848950286/permalink/1588655489150841/"
    p = URLParser(); p.parse(group_url)
    fields = {(e.field, e.value) for e in p.evidence}
    check("Group ID", ("group_id", "157327848950286") in fields)
    check("Group post ID", ("post_id", "1588655489150841") in fields)
    check("Group post type", FacebookResolver().classify_url_type(p.evidence, group_url) == "GROUP_POST")

    profile_url = "https://www.facebook.com/profile.php?id=100012345678901"
    p = URLParser(); p.parse(profile_url)
    check("Profile UID", any(e.field == "user_uid" and e.value == "100012345678901" for e in p.evidence))
    check("Profile type", FacebookResolver().classify_url_type(p.evidence, profile_url) == "PROFILE")

    post_url = "https://www.facebook.com/dxt2k4/posts/pfbid02LUBuwqqHhfzQbRmdK5LUJBT9KhuGHehSwgW3XRWTkB46ytBw7kxNX8phG837Gsijl"
    p = URLParser(); p.parse(post_url)
    check("Username extracted", any(e.field == "publisher_username" and e.value == "dxt2k4" for e in p.evidence))
    check("pfbid preserved", any(e.field == "post_id" and e.value.lower().startswith("pfbid") for e in p.evidence))
    check("pfbid not fake UID", not any(e.field == "user_uid" and e.value.lower().startswith("pfbid") for e in p.evidence))
    check("USER_POST type", FacebookResolver().classify_url_type(p.evidence, post_url) == "USER_POST")

    for kind, expected in (("p","SHARE_POST"),("v","SHARE_VIDEO"),("r","SHARE_REEL")):
        u = f"https://www.facebook.com/share/{kind}/1AbCdEfGhI/"
        p = URLParser(); p.parse(u)
        check(f"share/{kind}", FacebookResolver().classify_url_type(p.evidence, u) == expected)

    story_url = "https://www.facebook.com/stories/1283170361765988/UzpfSVNDOjQ0ODUxMTU5ODE3NDY3NjY=/?view_single=1"
    p = URLParser(); p.parse(story_url)
    fields = {(e.field,e.value) for e in p.evidence}
    check("Story actor", ("decoded_actor_id","1283170361765988") in fields)
    check("Story decoded object", ("post_id","4485115981746766") in fields)
    check("Story type", FacebookResolver().classify_url_type(p.evidence, story_url) == "STORY")

    actor_only_url = "https://www.facebook.com/stories/123456789012345/opaqueToken"
    p = URLParser(); p.parse(actor_only_url)
    r = FacebookResolver().build_result(actor_only_url, p, [])
    check("Actor is not USER UID", r.user_uid is None)
    opaque = decode_fb_encoded_id("pfbid0ExampleOpaqueToken123")
    check("pfbid opaque", bool(opaque and not opaque.valid))
    check("Invalid token rejected", bool(decode_fb_encoded_id("not-a-valid-facebook-token") and not decode_fb_encoded_id("not-a-valid-facebook-token").valid))

    print(f"\nSelf-test: {passed}/{total} passed.")
    return passed == total


if __name__ == "__main__":
    print(f"Facebook UID / Entity Resolver {VERSION}")
    print("Telegram module: import register(bot, notify_bot) from your main bot.")
    print("Running self-test...")
    raise SystemExit(0 if self_test() else 1)
