"""
FACEBOOK FORENSIC RESOLVER V61
Telegram / Telethon module
PUBLIC HTTP ONLY
NO:
- Playwright
- Selenium
- Chromium
- Facebook Login
- Cookies
- Facebook Access Token
- Graph API authentication
DESIGN PRINCIPLE
----------------
CONTENT TYPE != PUBLISHER TYPE
Ví dụ:
/kim.chi.125900/posts/123/
    CONTENT   = POST
    PUBLISHER = USER
/some-page/posts/123/
    CONTENT   = POST
    PUBLISHER = PAGE
/groups/123/posts/456/
    CONTENT   = GROUP_POST
    PUBLISHER = GROUP
/some-page/reel/999/
    CONTENT   = REEL
    PUBLISHER = PAGE
Không bao giờ:
creator_id => USER
page_id    => USER UID
group_id   => USER UID
Không bao giờ đoán UID khi evidence không đủ.
"""
from __future__ import annotations
import asyncio
import base64
import html as html_lib
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
)
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
from telethon import events
LOGGER = logging.getLogger("commands.getuidfb")
if not LOGGER.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | FBRESOLVER | %(levelname)s | %(message)s",
    )
REQUEST_TIMEOUT = (5, 12)
MAX_HTML_BYTES = 12 * 1024 * 1024
MAX_INPUT_URLS = 8
MAX_DISCOVERED_URLS = 100
MAX_PROFILE_CHECKS = 8
MAX_SHARE_PROBES = 8
MAX_OBJECT_PROBES = 24
MAX_OBJECT_DEPTH = 2
MAX_JSON_DEPTH = 12
MAX_STRING_SCAN = 500_000
MAX_EVIDENCE_PER_SOURCE = 80
MAX_SIGNALS = 5
MAX_DEBUG_CANDIDATES = 30
CONCURRENCY = 4
CACHE_TTL = 120
RETRY_COUNT = 2
SESSION_TIMEOUT = 900
FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "m.facebookcorewwwi.onion",
}
TRACKING_PARAMS = {
    "fbclid",
    "rdid",
    "share_url",
    "refsrc",
    "ref",
    "refid",
    "mibextid",
    "cft",
    "tn",
    "eep",
    "so",
    "rv",
    "rdr",
    "acontext",
    "paipv",
    "notif_id",
    "notif_t",
    "notif_type",
    "locale",
}
PRESERVE_PARAMS = {
    "id",
    "v",
    "fbid",
    "story_fbid",
    "set",
    "album_id",
    "photo_id",
}
GENERIC_NAMES = {
    "",
    "facebook",
    "log in",
    "login",
    "watch",
    "video",
    "videos",
    "photos",
    "photo",
    "reels",
    "reel",
    "home",
}
GENERIC_TITLE_RE = re.compile(
    r"^(facebook|log\s*in|login|watch|video|videos|photo|photos|reel|reels)$",
    re.I,
)
USER_AGENTS = [
    (
        "Mozilla/5.0 (Linux; Android 15; SM-S938B) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.7339.101 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
]
def make_headers(
    *,
    mobile: bool = False,
    referer: Optional[str] = None,
) -> Dict[str, str]:
    ua = (
        USER_AGENTS[0]
        if mobile
        else random.choice(USER_AGENTS[2:])
    )
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
    if referer:
        headers["Referer"] = referer
    return headers
def clean_text(value: Any) -> str:
    if value is None:
        return ""
    value = str(value)
    value = html_lib.unescape(value)
    value = value.replace("\x00", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()
def truncate(
    value: Any,
    limit: int = 500,
) -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
def tg_escape(value: Any) -> str:
    return html_lib.escape(
        clean_text(value),
        quote=True,
    )
def is_numeric_id(value: Any) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    return bool(
        re.fullmatch(
            r"\d{5,30}",
            s,
        )
    )
def is_opaque_content_id(value: Any) -> bool:
    if value is None:
        return False
    s = clean_text(value)
    if not s:
        return False
    if len(s) < 6 or len(s) > 300:
        return False
    if re.fullmatch(
        r"pfbid[A-Za-z0-9_-]+",
        s,
        re.I,
    ):
        return True
    if re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{5,299}",
        s,
    ):
        return not is_numeric_id(s)
    return False
def is_content_id(value: Any) -> bool:
    return (
        is_numeric_id(value)
        or is_opaque_content_id(value)
    )
def decode_facebook_story_token(value: str) -> List[str]:
    """Extract safe numeric identifiers from a Facebook story token.

    This is not UID guessing: it only decodes an explicitly supplied story
    token and returns numeric values actually encoded in it.
    """
    out: List[str] = []
    value = unquote(clean_text(value))
    candidates = [value]
    try:
        padded = value + ("=" * (-len(value) % 4))
        raw = base64.b64decode(padded, validate=False).decode("utf-8", "ignore")
        candidates.extend([raw, unquote(raw)])
    except Exception:
        pass
    for text in candidates:
        for n in re.findall(r"(?<!\d)(\d{10,30})(?!\d)", text):
            if n not in out:
                out.append(n)
    return out


def unique_keep_order(
    items: Iterable[str],
) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if not item:
            continue
        key = item.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
def normalize_host(host: str) -> str:
    host = (host or "").lower().strip()
    if host.endswith("."):
        host = host[:-1]
    return host
def is_facebook_host(host: str) -> bool:
    host = normalize_host(host)
    return (
        host in FACEBOOK_HOSTS
        or host.endswith(".facebook.com")
    )
def normalize_facebook_url(url: str) -> str:
    url = unquote(
        (url or "").strip()
    )
    if not re.match(
        r"^https?://",
        url,
        re.I,
    ):
        url = "https://" + url
    p = urlparse(url)
    host = normalize_host(p.netloc)
    if host in {
        "m.facebook.com",
        "web.facebook.com",
    }:
        host = "www.facebook.com"
    path = re.sub(
        r"/+",
        "/",
        p.path or "/",
    )
    if path != "/":
        path = path.rstrip("/")
    query = parse_qs(
        p.query,
        keep_blank_values=True,
    )
    kept = []
    for key, values in query.items():
        lower = key.lower()
        if lower in TRACKING_PARAMS:
            continue
        if lower.startswith("utm_"):
            continue
        if lower in PRESERVE_PARAMS:
            for value in values:
                kept.append(
                    (key, value)
                )
    query_string = urlencode(
        kept,
        doseq=True,
    )
    return urlunparse(
        (
            "https",
            host,
            path or "/",
            "",
            query_string,
            "",
        )
    )
def looks_like_url(value: str) -> bool:
    if not value:
        return False
    return bool(
        re.search(
            r"https?://"
            r"(?:www\.|m\.|mbasic\.)?"
            r"(?:facebook\.com|fb\.watch)"
            r"/\S+",
            value,
            re.I,
        )
    )
def extract_urls(text: str) -> List[str]:
    if not text:
        return []
    pattern = re.compile(
        r"https?://[^\s<>\[\]{}]+",
        re.I,
    )
    found = []
    for match in pattern.findall(text):
        url = match.strip()
        url = url.rstrip(
            ".,!?;:)]}'\""
        )
        if looks_like_url(url):
            found.append(url)
    return unique_keep_order(
        [
            normalize_facebook_url(x)
            for x in found
        ]
    )
def extract_numeric_ids(
    text: str,
) -> List[str]:
    if not text:
        return []
    return unique_keep_order(
        re.findall(
            r"(?<!\d)(\d{5,30})(?!\d)",
            text,
        )
    )
def generic_name(value: str) -> bool:
    value = clean_text(value)
    if not value:
        return True
    if GENERIC_TITLE_RE.fullmatch(value):
        return True
    return value.lower() in GENERIC_NAMES
def clean_title(value: str) -> str:
    value = clean_text(value)
    if generic_name(value):
        return ""
    value = re.sub(
        r"\s*[|·-]\s*Facebook\s*$",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"\s+on Facebook\s*$",
        "",
        value,
        flags=re.I,
    )
    return value.strip()
def looks_like_video_title(
    value: str,
) -> bool:
    value = clean_text(value)
    if not value:
        return False
    return (
        "lượt xem" in value.lower()
        or "cảm xúc" in value.lower()
        or "views" in value.lower()
        or "reel" in value.lower()
        or "video" in value.lower()
    )
def extract_content_identifiers(text: str) -> List[str]:
    if not text:
        return []
    found = []
    found.extend(
        re.findall(
            r"(?<!\d)(\d{5,30})(?!\d)",
            text,
        )
    )
    found.extend(
        re.findall(
            r"\bpfbid[A-Za-z0-9_-]{6,299}\b",
            text,
            re.I,
        )
    )
    return unique_keep_order(found)
@dataclass
class URLShape:
    original: str = ""
    normalized: str = ""
    host: str = ""
    path: str = ""
    segments: List[str] = field(
        default_factory=list
    )
    query: Dict[str, List[str]] = field(
        default_factory=dict
    )
    kind: str = "UNKNOWN"
    username: str = ""
    numeric_path_id: str = ""
    post_id: str = ""
    video_id: str = ""
    reel_id: str = ""
    photo_id: str = ""
    story_id: str = ""
    group_id: str = ""
    page_id: str = ""
    album_id: str = ""
    opaque_token: str = ""
    wrapper: bool = False
    route_entity: str = ""
    route_confidence: float = 0.0
class URLParser:
    RESERVED = {
        "watch",
        "reel",
        "reels",
        "video",
        "videos",
        "posts",
        "post",
        "photos",
        "photo",
        "photo.php",
        "video.php",
        "story.php",
        "stories",
        "groups",
        "group",
        "events",
        "event",
        "pages",
        "page",
        "profile.php",
        "permalink.php",
        "login.php",
        "login",
        "logout.php",
        "checkpoint",
        "recover",
        "help",
        "settings",
        "privacy",
        "security",
        "home.php",
        "ajax",
        "plugins",
        "dialog",
        "oauth",
        "share",
        "share.php",
        "share/r",
        "share/p",
        "share/v",
        "share/x",
        "share/b",
        # Common Facebook-owned landing/system routes. These should not be
        # mistaken for vanity USER profiles when a wrapper lands on them.
        "about",
        "meta",
        "business",
        "businesses",
        "company",
        "careers",
        "news",
        "community",
        "developers",
        "marketing",
        "facebook",
        "legal",
    }
    @classmethod
    def parse(
        cls,
        url: str,
    ) -> URLShape:
        normalized = normalize_facebook_url(url)
        p = urlparse(normalized)
        shape = URLShape(
            original=url,
            normalized=normalized,
            host=normalize_host(p.netloc),
            path=p.path,
            query=parse_qs(
                p.query,
                keep_blank_values=True,
            ),
        )
        segments = [
            unquote(x)
            for x in p.path.split("/")
            if x
        ]
        shape.segments = segments
        lower = [
            x.lower()
            for x in segments
        ]
        # Authentication/system endpoints are not Facebook publishers.
        blocked_paths = {
            "login.php", "logout.php", "checkpoint", "recover",
            "registration", "reg", "privacy", "security",
        }
        if (
            p.path.lower().strip("/") in blocked_paths
            or any(x in blocked_paths for x in lower)
        ):
            shape.kind = "UNKNOWN"
            shape.route_entity = ""
            shape.route_confidence = 100
            return shape
        if (
            p.path.lower().endswith("/profile.php")
            and "id" in shape.query
        ):
            uid = shape.query["id"][0]
            if is_numeric_id(uid):
                shape.numeric_path_id = uid
                shape.kind = "PROFILE"
                shape.route_entity = "USER"
                shape.route_confidence = 100
            return shape
        if (
            len(lower) >= 2
            and lower[0] == "share"
        ):
            shape.wrapper = True
            shape.kind = "SHARE_WRAPPER"
            shape.route_confidence = 100
            if lower[1] in {
                "r",
                "p",
                "v",
                "x",
                "b",
            }:
                if len(segments) >= 3:
                    shape.opaque_token = segments[2]
            else:
                # Newer Facebook share URLs commonly use /share/<token>/.
                shape.opaque_token = segments[1]
            return shape
        if "groups" in lower:
            idx = lower.index("groups")
            if idx + 1 < len(segments):
                candidate = segments[idx + 1]
                if is_numeric_id(candidate):
                    shape.group_id = candidate
                else:
                    shape.username = candidate
            shape.route_entity = "GROUP"
            if "posts" in lower:
                idx = lower.index("posts")
                if idx + 1 < len(segments):
                    candidate = segments[idx + 1]
                    if is_content_id(candidate):
                        shape.post_id = candidate
                    if "groups" in lower:
                        shape.kind = "GROUP_POST"
                    else:
                        shape.kind = "POST"
                    shape.route_confidence = 99
            elif (
                "reel" in lower
                or "reels" in lower
            ):
                shape.kind = "REEL"
            elif (
                "videos" in lower
                or "video" in lower
            ):
                shape.kind = "VIDEO"
            elif (
                "photos" in lower
                or "photo" in lower
            ):
                shape.kind = "PHOTO"
            else:
                shape.kind = "GROUP"
            shape.route_confidence = 98
            return shape
        if "events" in lower:
            idx = lower.index("events")
            if idx + 1 < len(segments):
                candidate = segments[idx + 1]
                if is_numeric_id(candidate):
                    shape.numeric_path_id = candidate
                shape.kind = "EVENT"
                shape.route_entity = "EVENT"
                shape.route_confidence = 98
            return shape
        if (
            "reel" in lower
            or "reels" in lower
        ):
            idx = (
                lower.index("reel")
                if "reel" in lower
                else lower.index("reels")
            )
            if idx + 1 < len(segments):
                candidate = segments[idx + 1]
                if is_content_id(candidate):
                    shape.reel_id = candidate
            shape.kind = "REEL"
            shape.route_confidence = 99
        if (
            "posts" in lower
            or "post" in lower
        ):
            idx = (
                lower.index("posts")
                if "posts" in lower
                else lower.index("post")
            )
            if idx + 1 < len(segments):
                candidate = segments[idx + 1]
                if is_content_id(candidate):
                    shape.post_id = candidate
            if shape.kind == "UNKNOWN":
                shape.kind = "POST"
            shape.route_confidence = max(
                shape.route_confidence,
                99,
            )
        if (
            "videos" in lower
            or "video" in lower
            or p.path.lower().endswith(
                "/video.php"
            )
        ):
            if "v" in shape.query:
                candidate = shape.query["v"][0]
                if is_content_id(candidate):
                    shape.video_id = candidate
            if not shape.video_id:
                idx = (
                    lower.index("videos")
                    if "videos" in lower
                    else (
                        lower.index("video")
                        if "video" in lower
                        else -1
                    )
                )
                if idx >= 0:
                    for candidate in segments[idx + 1:]:
                        if is_content_id(candidate):
                            shape.video_id = candidate
                            break
            if shape.kind == "UNKNOWN":
                shape.kind = "VIDEO"
            shape.route_confidence = max(
                shape.route_confidence,
                98,
            )
        if (
            "photos" in lower
            or "photo" in lower
            or p.path.lower().endswith(
                "/photo.php"
            )
        ):
            for key in (
                "fbid",
                "photo_id",
            ):
                if key in shape.query:
                    candidate = shape.query[key][0]
                    if is_content_id(candidate):
                        shape.photo_id = candidate
                        break
            if not shape.photo_id:
                idx = (
                    lower.index("photos")
                    if "photos" in lower
                    else (
                        lower.index("photo")
                        if "photo" in lower
                        else -1
                    )
                )
                if idx >= 0:
                    for candidate in segments[idx + 1:]:
                        if is_content_id(candidate):
                            shape.photo_id = candidate
                            break
            if shape.kind == "UNKNOWN":
                shape.kind = "PHOTO"
            shape.route_confidence = max(
                shape.route_confidence,
                98,
            )
        if (
            "story.php" in p.path.lower()
            or "stories" in lower
        ):
            # Facebook story URLs can carry TWO important identifiers:
            #   /stories/<owner_numeric_id>/<opaque_story_token>/
            # The first numeric segment is the public owner/profile UID
            # candidate; the second segment may be a base64-ish story object
            # token.  Do not throw either away.
            idx = lower.index("stories") if "stories" in lower else -1
            if idx >= 0 and idx + 1 < len(segments):
                owner_candidate = segments[idx + 1]
                if is_numeric_id(owner_candidate):
                    shape.numeric_path_id = owner_candidate
                    shape.route_entity = "USER"
                    shape.route_confidence = max(shape.route_confidence, 97)
                if idx + 2 < len(segments):
                    story_token = segments[idx + 2]
                    if is_content_id(story_token):
                        shape.story_id = story_token
                    else:
                        # Preserve opaque story tokens such as
                        # UzpfSVNDOjEwODI2OTkxNDA5MzYwNjI= for PASS 2.
                        shape.opaque_token = story_token
            for key in (
                "story_fbid",
                "fbid",
            ):
                if key in shape.query:
                    candidate = shape.query[key][0]
                    if is_content_id(candidate):
                        shape.story_id = candidate
                        break
            shape.kind = "STORY"
            shape.route_confidence = max(
                shape.route_confidence,
                98,
            )
        if (
            "album" in lower
            or "albums" in lower
        ):
            if "album_id" in shape.query:
                candidate = shape.query["album_id"][0]
                if is_numeric_id(candidate):
                    shape.album_id = candidate
            if shape.kind == "UNKNOWN":
                shape.kind = "ALBUM"
            shape.route_confidence = max(
                shape.route_confidence,
                95,
            )
        if shape.kind == "UNKNOWN":
            if not segments:
                shape.kind = "HOME"
            elif len(segments) == 1:
                first = segments[0]
                if first.lower() not in cls.RESERVED:
                    if is_numeric_id(first):
                        shape.numeric_path_id = first
                    else:
                        shape.username = first
                    shape.kind = "PROFILE"
                    shape.route_confidence = 92
        if shape.kind in {
            "POST",
            "REEL",
            "VIDEO",
            "PHOTO",
            "STORY",
            "ALBUM",
        }:
            cls._derive_route_entity(shape)
        return shape
    @classmethod
    def _derive_route_entity(
        cls,
        shape: URLShape,
    ):
        segments = shape.segments
        if not segments:
            return
        lower = [
            x.lower()
            for x in segments
        ]
        if "groups" in lower:
            idx = lower.index("groups")
            if idx + 1 < len(segments):
                entity = segments[idx + 1]
                if is_numeric_id(entity):
                    shape.group_id = entity
                else:
                    shape.username = entity
                shape.route_entity = "GROUP"
            return
        if "pages" in lower:
            idx = lower.index("pages")
            if idx + 1 < len(segments):
                entity = segments[idx + 1]
                if is_numeric_id(entity):
                    shape.page_id = entity
                else:
                    shape.username = entity
                shape.route_entity = "PAGE"
            return
        if is_numeric_id(segments[0]):
            shape.numeric_path_id = segments[0]
            return
        reserved = {
            "watch",
            "reel",
            "reels",
            "video",
            "videos",
            "posts",
            "post",
            "photos",
            "photo",
            "story",
            "stories",
            "events",
            "event",
            "groups",
            "group",
            "pages",
            "page",
            "profile.php",
            "permalink.php",
            "login.php",
            "login",
            "logout.php",
            "checkpoint",
            "recover",
            "help",
            "settings",
            "privacy",
            "security",
            "home.php",
            "ajax",
            "plugins",
            "dialog",
            "oauth",
        }
        first = segments[0]
        if first.lower() in reserved:
            return
        shape.username = first
        shape.route_entity = "USER"
class FBHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(
            convert_charrefs=True
        )
        self.meta: Dict[str, str] = {}
        self.links: List[str] = []
        self.scripts: List[str] = []
        self._script = False
        self._script_buffer: List[str] = []
        self.title_parts: List[str] = []
        self._title = False
        self.text_parts: List[str] = []
        self.images: List[str] = []
    def handle_starttag(
        self,
        tag: str,
        attrs: List[
            Tuple[str, Optional[str]]
        ],
    ):
        data = {
            k.lower(): v or ""
            for k, v in attrs
        }
        tag = tag.lower()
        if tag == "meta":
            key = (
                data.get("property")
                or data.get("name")
                or data.get("itemprop")
            )
            content = data.get(
                "content",
                "",
            )
            if key and content:
                self.meta[
                    key.lower()
                ] = clean_text(content)
        elif tag == "a":
            href = data.get("href")
            if href:
                self.links.append(href)
        elif tag == "link":
            # Keep canonical/app/profile links. Facebook may expose the
            # resolved public object here even when the visible page is
            # generic. This is metadata discovery only; no auth bypass.
            rel = (data.get("rel") or "").lower()
            href = data.get("href") or ""
            if href and (
                "canonical" in rel
                or "alternate" in rel
                or "shortlink" in rel
            ):
                self.links.append(href)
        elif tag == "img":
            src = (
                data.get("src")
                or data.get("data-src")
                or data.get("data-original")
            )
            if src:
                self.images.append(src)
        elif tag == "script":
            self._script = True
            self._script_buffer = []
        elif tag == "title":
            self._title = True
    def handle_endtag(
        self,
        tag: str,
    ):
        tag = tag.lower()
        if tag == "script":
            if self._script_buffer:
                self.scripts.append(
                    "\n".join(
                        self._script_buffer
                    )
                )
            self._script = False
            self._script_buffer = []
        elif tag == "title":
            self._title = False
    def handle_data(
        self,
        data: str,
    ):
        if self._script:
            current_size = len(
                "".join(
                    self._script_buffer
                )
            )
            if current_size < MAX_STRING_SCAN:
                self._script_buffer.append(
                    data
                )
        elif self._title:
            self.title_parts.append(data)
        else:
            value = clean_text(data)
            if value:
                self.text_parts.append(value)
    @property
    def title(self) -> str:
        return clean_title(
            " ".join(
                self.title_parts
            )
        )
    @property
    def text(self) -> str:
        return truncate(
            " ".join(
                self.text_parts
            ),
            MAX_STRING_SCAN,
        )
@dataclass
class PageSnapshot:
    url: str = ""
    final_url: str = ""
    status: int = 0
    ok: bool = False
    title: str = ""
    meta: Dict[str, str] = field(
        default_factory=dict
    )
    links: List[str] = field(
        default_factory=list
    )
    scripts: List[str] = field(
        default_factory=list
    )
    images: List[str] = field(
        default_factory=list
    )
    text: str = ""
    html: str = ""
    jsonld: List[Any] = field(
        default_factory=list
    )
    json_objects: List[Any] = field(
        default_factory=list
    )
    # URLs visited during an ordinary HTTP redirect chain.  This is useful
    # for public share links whose final response may be a generic/auth page.
    redirect_chain: List[str] = field(
        default_factory=list
    )
    error: str = ""
    limited: bool = False
class HTTPFetcher:
    def __init__(self):
        self.local = threading.local()
    def session(self) -> requests.Session:
        session = getattr(
            self.local,
            "session",
            None,
        )
        if session is None:
            session = requests.Session()
            session.trust_env = True
            self.local.session = session
        return session
    def fetch(
        self,
        url: str,
        *,
        referer: Optional[str] = None,
    ) -> PageSnapshot:
        snapshot = PageSnapshot(
            url=url,
            final_url=url,
        )
        session = self.session()
        last_error = ""
        for attempt in range(
            RETRY_COUNT + 1
        ):
            try:
                headers = make_headers(
                    mobile=False,
                    referer=referer,
                )
                response = session.get(
                    url,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True,
                    stream=True,
                )
                snapshot.status = (
                    response.status_code
                )
                snapshot.final_url = (
                    normalize_facebook_url(
                        response.url
                    )
                    if is_facebook_host(
                        urlparse(
                            response.url
                        ).netloc
                    )
                    else response.url
                )
                snapshot.redirect_chain = unique_keep_order(
                    [
                        (
                            normalize_facebook_url(h.url)
                            if is_facebook_host(urlparse(h.url).netloc)
                            else h.url
                        )
                        for h in response.history
                    ]
                    + [snapshot.final_url]
                )
                content_type = (
                    response.headers.get(
                        "Content-Type",
                        "application/x-www-form-urlencoded",
                    )
                    .lower()
                )
                if (
                    "text/html"
                    not in content_type
                    and "application/xhtml+xml"
                    not in content_type
                ):
                    data = response.content[
                        :MAX_HTML_BYTES
                    ]
                    snapshot.html = data.decode(
                        "utf-8",
                        errors="ignore",
                    )
                    snapshot.ok = response.ok
                    return snapshot
                data = bytearray()
                for chunk in response.iter_content(
                    chunk_size=64 * 1024
                ):
                    if not chunk:
                        continue
                    data.extend(chunk)
                    if len(data) >= MAX_HTML_BYTES:
                        snapshot.limited = True
                        break
                text = bytes(data).decode(
                    response.encoding or "utf-8",
                    errors="ignore",
                )
                snapshot.html = text
                parser = FBHTMLParser()
                try:
                    parser.feed(text)
                except Exception:
                    LOGGER.debug(
                        "HTML parser partial failure",
                        exc_info=True,
                    )
                snapshot.title = parser.title
                snapshot.meta = parser.meta
                snapshot.links = (
                    parser.links[
                        :MAX_DISCOVERED_URLS
                    ]
                )
                snapshot.scripts = parser.scripts
                snapshot.images = (
                    parser.images[
                        :MAX_DISCOVERED_URLS
                    ]
                )
                snapshot.text = parser.text
                snapshot.jsonld = (
                    extract_jsonld(text)
                )
                snapshot.json_objects = (
                    extract_embedded_json(text)
                )
                snapshot.ok = response.ok
                if (
                    response.status_code
                    in {
                        429,
                        500,
                        502,
                        503,
                        504,
                    }
                    and attempt < RETRY_COUNT
                ):
                    time.sleep(
                        0.6 * (
                            2 ** attempt
                        )
                    )
                    continue
                return snapshot
            except (
                requests.RequestException,
                OSError,
            ) as exc:
                last_error = clean_text(exc)
                if attempt < RETRY_COUNT:
                    time.sleep(
                        0.6 * (
                            2 ** attempt
                        )
                    )
                    continue
                break
            except Exception as exc:
                last_error = clean_text(exc)
                LOGGER.debug(
                    "fetch error",
                    exc_info=True,
                )
                break
        snapshot.error = (
            last_error
            or "HTTP request failed"
        )
        snapshot.limited = True
        return snapshot
def safe_json_loads(
    value: str,
) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None
def recursive_objects(
    obj: Any,
    *,
    depth: int = 0,
) -> Iterable[Any]:
    if depth > MAX_JSON_DEPTH:
        return
    yield obj
    if isinstance(obj, dict):
        for value in obj.values():
            yield from recursive_objects(
                value,
                depth=depth + 1,
            )
    elif isinstance(obj, list):
        for value in obj:
            yield from recursive_objects(
                value,
                depth=depth + 1,
            )
def extract_jsonld(
    html: str,
) -> List[Any]:
    results = []
    pattern = re.compile(
        r"<script[^>]+type=[\"']"
        r"application/ld\+json"
        r"[\"'][^>]*>"
        r"(.*?)"
        r"</script>",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(html):
        raw = match.group(1).strip()
        raw = html_lib.unescape(raw)
        parsed = safe_json_loads(raw)
        if parsed is not None:
            results.append(parsed)
    return results[:50]
def extract_balanced_json(
    text: str,
    start: int,
) -> Optional[str]:
    if start >= len(text):
        return None
    opening = text[start]
    if opening not in "{[":
        return None
    closing = (
        "}"
        if opening == "{"
        else "]"
    )
    depth = 0
    in_string = False
    escaped = False
    end_limit = min(
        len(text),
        start + 1_000_000,
    )
    for i in range(
        start,
        end_limit,
    ):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return text[
                    start:i + 1
                ]
    return None
def extract_embedded_json(
    html: str,
) -> List[Any]:
    results = []
    pattern = re.compile(
        r"JSON\.parse\(\s*"
        r"([\"'])(.*?)\1"
        r"\s*\)",
        flags=re.I | re.S,
    )
    for match in pattern.finditer(html):
        raw = match.group(2)
        try:
            parsed_string = bytes(
                raw,
                "utf-8",
            ).decode(
                "unicode_escape"
            )
        except Exception:
            parsed_string = raw
        parsed = safe_json_loads(
            parsed_string
        )
        if parsed is not None:
            results.append(parsed)
    interesting_markers = (
        '"user_id"',
        '"profile_id"',
        '"owner_id"',
        '"author_id"',
        '"creator_id"',
        '"page_id"',
        '"group_id"',
        '"post_id"',
        '"video_id"',
        '"reel_id"',
        '"media_fbid"',
        '"story_fbid"',
    )
    for marker in interesting_markers:
        offset = 0
        while True:
            pos = html.find(
                marker,
                offset,
            )
            if pos < 0:
                break
            start = html.rfind(
                "{",
                max(
                    0,
                    pos - 10_000,
                ),
                pos + 1,
            )
            if start >= 0:
                raw = extract_balanced_json(
                    html,
                    start,
                )
                if raw:
                    parsed = safe_json_loads(
                        raw
                    )
                    if parsed is not None:
                        results.append(parsed)
            if len(results) >= 120:
                return results
            offset = (
                pos
                + len(marker)
            )
    return results[:120]
@dataclass
class Evidence:
    value: str
    role: str
    source: str
    path: str = ""
    key: str = ""
    neighbor: str = ""
    url: str = ""
    weight: float = 0.0
    independent: bool = True
    entity_type: str = ""
USER_FIELD_WEIGHTS = {
    "user_id": 110,
    "profile_id": 108,
    "owner_id": 100,
    "publisher_id": 100,
    "author_id": 96,
    "from.id": 94,
    "from_id": 92,
    "creator_id": 92,
    "page_owner_id": 88,
    "profile.uid": 108,
    "profile.id": 105,
    "owner.id": 100,
    "publisher.id": 100,
    "author.id": 96,
    "creator.id": 92,
    "actor_id": 70,
    "actor.id": 68,
    "entity_id": 48,
    "entity.id": 45,
}
OBJECT_FIELD_WEIGHTS = {
    "post_id": 110,
    "story_fbid": 110,
    "video_id": 110,
    "photo_id": 110,
    "media_fbid": 105,
    "reel_id": 110,
    "album_id": 100,
    "group_id": 120,
    "page_id": 120,
    "event_id": 120,
}
IDENTITY_FIELDS = {
    x.lower(): w
    for x, w in USER_FIELD_WEIGHTS.items()
}
OBJECT_FIELDS = {
    x.lower(): w
    for x, w in OBJECT_FIELD_WEIGHTS.items()
}
def normalize_key(
    key: Any,
) -> str:
    key = str(key)
    key = key.replace(
        "-",
        "_",
    )
    return key.lower().strip()
def key_role(
    key: str,
) -> str:
    k = normalize_key(key)
    if k in IDENTITY_FIELDS:
        return "USER_CANDIDATE"
    if k in OBJECT_FIELDS:
        return "OBJECT_ID"
    if (
        "page" in k
        and "id" in k
    ):
        return "PAGE_ID"
    if (
        "group" in k
        and "id" in k
    ):
        return "GROUP_ID"
    if (
        "event" in k
        and "id" in k
    ):
        return "EVENT_ID"
    return ""
PROFILE_KEYS = {
    "name",
    "username",
    "short_name",
    "display_name",
    "full_name",
    "profile_url",
    "profile_uri",
    "uri",
    "url",
    "avatar",
    "avatar_url",
    "profile_picture",
    "profile_pic",
    "image",
    "description",
    "bio",
}
CONTENT_KEYS = {
    "title",
    "post_id",
    "video_id",
    "reel_id",
    "photo_id",
    "story_fbid",
    "media_fbid",
}
class EvidenceCollector:
    def __init__(self):
        self.items: List[Evidence] = []
        self._dedupe: Set[
            Tuple[str, str, str]
        ] = set()
    def add(
        self,
        value: Any,
        *,
        role: str,
        source: str,
        path: str = "",
        key: str = "",
        neighbor: str = "",
        url: str = "",
        weight: float = 0.0,
        independent: bool = True,
        entity_type: str = "",
    ):
        value = clean_text(value)
        if not value:
            return
        if len(value) > 500:
            value = value[:500]
        dedupe_key = (
            value,
            role,
            source,
        )
        if dedupe_key in self._dedupe:
            return
        self._dedupe.add(
            dedupe_key
        )
        self.items.append(
            Evidence(
                value=value,
                role=role,
                source=source,
                path=path,
                key=key,
                neighbor=neighbor,
                url=url,
                weight=weight,
                independent=independent,
                entity_type=entity_type,
            )
        )
    def values_for(
        self,
        role: str,
    ) -> List[str]:
        return unique_keep_order(
            [
                x.value
                for x in self.items
                if x.role == role
            ]
        )
    def sources_for(
        self,
        value: str,
    ) -> Set[str]:
        return {
            x.source
            for x in self.items
            if x.value == value
        }
    def by_role(
        self,
        role: str,
    ) -> List[Evidence]:
        return [
            x
            for x in self.items
            if x.role == role
        ]
class JSONEvidenceScanner:
    def __init__(
        self,
        collector: EvidenceCollector,
    ):
        self.collector = collector
    def scan(
        self,
        obj: Any,
        *,
        source: str,
        path: str = "$",
        neighbor: str = "",
    ):
        self._scan(
            obj,
            source=source,
            path=path,
            neighbor=neighbor,
            depth=0,
        )
    def _scan(
        self,
        obj: Any,
        *,
        source: str,
        path: str,
        neighbor: str,
        depth: int,
    ):
        if depth > MAX_JSON_DEPTH:
            return
        if isinstance(obj, dict):
            local_strings = {}
            for key, value in obj.items():
                k = normalize_key(key)
                if isinstance(
                    value,
                    (
                        str,
                        int,
                        float,
                    ),
                ):
                    local_strings[k] = str(value)
            local_entity = ""
            if any(
                k in local_strings
                for k in (
                    "group_id",
                )
            ):
                local_entity = "GROUP"
            elif any(
                k in local_strings
                for k in (
                    "page_id",
                    "page_owner_id",
                )
            ):
                local_entity = "PAGE"
            elif any(
                k in local_strings
                for k in (
                    "event_id",
                )
            ):
                local_entity = "EVENT"
            # Many Facebook payloads use a generic `id` inside a user
            # object. Accept it only when the same object has a username/profile
            # identity signal, and let page/group/event semantics block it.
            generic_id = local_strings.get("id", "")
            if (
                is_numeric_id(generic_id)
                and not local_entity
                and (
                    local_strings.get("username")
                    or local_strings.get("profile_url")
                    or local_strings.get("profile_uri")
                )
            ):
                identity_neighbor = clean_text(
                    " ".join(
                        x for x in (
                            local_strings.get("username", ""),
                            local_strings.get("name", ""),
                            local_strings.get("profile_url", ""),
                        ) if x
                    )
                )
                self.collector.add(
                    generic_id,
                    role="USER_CANDIDATE",
                    source=source,
                    path=path,
                    key="id",
                    neighbor=identity_neighbor,
                    weight=106,
                    entity_type="USER_CANDIDATE",
                )
            for key, value in local_strings.items():
                role = key_role(key)
                if role:
                    weight = IDENTITY_FIELDS.get(
                        key,
                        OBJECT_FIELDS.get(
                            key,
                            30,
                        ),
                    )
                    if role == "USER_CANDIDATE":
                        effective_entity = (
                            local_entity
                            or "USER_CANDIDATE"
                        )
                        self.collector.add(
                            value,
                            role=role,
                            source=source,
                            path=path,
                            key=key,
                            neighbor=neighbor,
                            weight=weight,
                            entity_type=effective_entity,
                        )
                    elif role == "OBJECT_ID":
                        self.collector.add(
                            value,
                            role=role,
                            source=source,
                            path=path,
                            key=key,
                            neighbor=neighbor,
                            weight=weight,
                            entity_type=local_entity,
                        )
                if (
                    "page" in key
                    and key.endswith("id")
                    and is_numeric_id(value)
                ):
                    self.collector.add(
                        value,
                        role="PAGE_ID",
                        source=source,
                        path=path,
                        key=key,
                        neighbor=neighbor,
                        weight=120,
                        entity_type="PAGE",
                    )
                if (
                    "group" in key
                    and key.endswith("id")
                    and is_numeric_id(value)
                ):
                    self.collector.add(
                        value,
                        role="GROUP_ID",
                        source=source,
                        path=path,
                        key=key,
                        neighbor=neighbor,
                        weight=120,
                        entity_type="GROUP",
                    )
                if k in {
                    "name",
                    "display_name",
                    "full_name",
                    "short_name",
                }:
                    name = clean_text(value)
                    if (
                        name
                        and not generic_name(name)
                        and not looks_like_video_title(name)
                    ):
                        self.collector.add(
                            name,
                            role="NAME",
                            source=source,
                            path=path,
                            key=key,
                            neighbor=neighbor,
                            weight=45,
                        )
                elif k == "username":
                    username = clean_text(value)
                    if username:
                        self.collector.add(
                            username.lstrip("@"),
                            role="USERNAME",
                            source=source,
                            path=path,
                            key=key,
                            neighbor=neighbor,
                            weight=55,
                        )
                elif k in {
                    "profile_url",
                    "profile_uri",
                }:
                    self.collector.add(
                        value,
                        role="PROFILE_URL",
                        source=source,
                        path=path,
                        key=key,
                        neighbor=neighbor,
                        weight=70,
                    )
                elif k in {
                    "url",
                    "uri",
                }:
                    if "facebook.com" in value.lower():
                        self.collector.add(
                            value,
                            role="URL",
                            source=source,
                            path=path,
                            key=key,
                            neighbor=neighbor,
                            weight=30,
                        )
                elif k in {
                    "description",
                    "bio",
                }:
                    self.collector.add(
                        truncate(
                            value,
                            1000,
                        ),
                        role="BIO",
                        source=source,
                        path=path,
                        key=key,
                        neighbor=neighbor,
                        weight=30,
                    )
            for key, value in obj.items():
                child_path = (
                    f"{path}.{key}"
                )
                child_neighbor = (
                    local_strings.get(
                        "name",
                        local_strings.get(
                            "display_name",
                            neighbor,
                        ),
                    )
                )
                self._scan(
                    value,
                    source=source,
                    path=child_path,
                    neighbor=child_neighbor,
                    depth=depth + 1,
                )
        elif isinstance(obj, list):
            for index, value in enumerate(
                obj[:500]
            ):
                self._scan(
                    value,
                    source=source,
                    path=f"{path}[{index}]",
                    neighbor=neighbor,
                    depth=depth + 1,
                )
def scan_meta(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    meta = snapshot.meta
    for key, value in meta.items():
        k = key.lower()
        if k in {
            "og:title",
            "twitter:title",
        }:
            title = clean_title(value)
            if title:
                collector.add(
                    title,
                    role="TITLE",
                    source="meta",
                    key=k,
                    weight=60,
                )
        elif k in {
            "og:description",
            "twitter:description",
        }:
            collector.add(
                truncate(
                    value,
                    1200,
                ),
                role="DESCRIPTION",
                source="meta",
                key=k,
                weight=45,
            )
        elif k == "og:url":
            collector.add(
                value,
                role="CANONICAL",
                source="meta",
                key=k,
                weight=95,
            )
        elif k in {
            "og:image",
            "twitter:image",
        }:
            collector.add(
                value,
                role="IMAGE",
                source="meta",
                key=k,
                weight=30,
            )
def scan_jsonld(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    scanner = JSONEvidenceScanner(
        collector
    )
    for index, obj in enumerate(
        snapshot.jsonld
    ):
        scanner.scan(
            obj,
            source=f"jsonld:{index}",
        )
def scan_scripts(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    """
    High-recall Facebook identity/object scanner.
    IMPORTANT:
    - Never treat arbitrary "id" as USER UID.
    - Generic id is classified only from semantic context.
    - creator_id / actor_id / owner_id are candidates,
      not automatically verified UIDs.
    """
    for index, script in enumerate(
        snapshot.scripts[:150]
    ):
        if not script:
            continue
        source = f"script:{index}"
        identity_patterns = {
            # Strong direct USER identity fields.
            "user_id": 118,
            "userid": 118,
            "userId": 118,
            "userID": 118,
            "uid": 116,
            "user_uid": 118,
            "userUid": 118,
            "profile_id": 120,
            "profileid": 120,
            "profileId": 120,
            "profileID": 120,
            "profile_uid": 120,
            "profileuid": 120,
            "profileUid": 120,
            "profileUID": 120,
            "profile_owner_id": 118,
            "profileOwnerId": 118,
            "profile_owner_uid": 120,
            "profileOwnerUid": 120,
            # Publisher / author identity fields.
            "publisher_id": 112,
            "publisherid": 112,
            "publisherId": 112,
            "publisherID": 112,
            "author_id": 110,
            "authorid": 110,
            "authorId": 110,
            "authorID": 110,
            "owner_id": 108,
            "ownerid": 108,
            "ownerId": 108,
            "ownerID": 108,
            "from_id": 108,
            "fromid": 108,
            "fromId": 108,
            "fromID": 108,
            "creator_id": 94,
            "creatorid": 94,
            "creatorId": 94,
            "creatorID": 94,
            "actor_id": 92,
            "actorid": 92,
            "actorId": 92,
            "actorID": 92,
            # Nested identity aliases seen in serialized state.
            "profile.uid": 120,
            "profile.id": 116,
            "profile.owner_id": 118,
            "profile.ownerId": 118,
            "owner.id": 110,
            "publisher.id": 112,
            "author.id": 110,
            "from.id": 108,
            "creator.id": 94,
            "actor.id": 92,
            "legacy_id": 92,
            "legacyId": 92,
            "legacyID": 92,
        }
        for key, weight in identity_patterns.items():
            escaped = re.escape(key)
            # Accept JSON, JS object literals and GraphQL-like key/value
            # forms.  The key itself is semantic, so this is much safer than
            # globally harvesting every numeric ``id`` in the document.
            pattern = re.compile(
                rf'(?:["\']{escaped}["\']|(?<![A-Za-z0-9_$]){escaped}(?![A-Za-z0-9_$]))'
                rf'\s*[:=]\s*'
                rf'(?:["\']?)(\d{{5,30}})(?:["\']?)',
                re.I,
            )
            for match in pattern.finditer(script):
                value = match.group(1)
                if not is_numeric_id(value):
                    continue
                start = max(
                    0,
                    match.start() - 500,
                )
                end = min(
                    len(script),
                    match.end() + 500,
                )
                context = script[
                    start:end
                ].lower()
                entity_type = ""
                if (
                    "group_id" in context
                    or "groupid" in context
                    or '"group"' in context
                ):
                    entity_type = "GROUP"
                elif (
                    "page_id" in context
                    or "pageid" in context
                    or '"page"' in context
                ):
                    entity_type = "PAGE"
                elif (
                    "event_id" in context
                    or "eventid" in context
                    or '"event"' in context
                ):
                    entity_type = "EVENT"
                collector.add(
                    value,
                    role="USER_CANDIDATE",
                    source=source,
                    key=key,
                    weight=weight,
                    neighbor=clean_text(
                        context[:500]
                    ),
                    entity_type=(
                        entity_type
                        or "UNKNOWN"
                    ),
                )
        explicit_entity_patterns = {
            "page_id": (
                "PAGE_ID",
                125,
            ),
            "pageid": (
                "PAGE_ID",
                125,
            ),
            "pageId": (
                "PAGE_ID",
                125,
            ),
            "group_id": (
                "GROUP_ID",
                125,
            ),
            "groupid": (
                "GROUP_ID",
                125,
            ),
            "groupId": (
                "GROUP_ID",
                125,
            ),
            "event_id": (
                "EVENT_ID",
                125,
            ),
            "eventid": (
                "EVENT_ID",
                125,
            ),
            "eventId": (
                "EVENT_ID",
                125,
            ),
        }
        for key, (
            role,
            weight,
        ) in explicit_entity_patterns.items():
            pattern = re.compile(
                rf'(?:["\']{re.escape(key)}["\']|(?<![A-Za-z0-9_$]){re.escape(key)}(?![A-Za-z0-9_$]))'
                rf'\s*[:=]\s*'
                rf'(?:["\']?)(\d{{5,30}})(?:["\']?)',
                re.I,
            )
            for match in pattern.finditer(script):
                value = match.group(1)
                if not is_numeric_id(value):
                    continue
                entity = (
                    "PAGE"
                    if role == "PAGE_ID"
                    else (
                        "GROUP"
                        if role == "GROUP_ID"
                        else "EVENT"
                    )
                )
                collector.add(
                    value,
                    role=role,
                    source=source,
                    key=key,
                    weight=weight,
                    entity_type=entity,
                )
        object_patterns = {
            "post_id": 110,
            "postid": 110,
            "postId": 110,
            "video_id": 110,
            "videoid": 110,
            "videoId": 110,
            "reel_id": 110,
            "reelid": 110,
            "reelId": 110,
            "photo_id": 110,
            "photoid": 110,
            "photoId": 110,
            "media_fbid": 108,
            "mediaFbid": 108,
            "story_fbid": 110,
            "storyFbid": 110,
            "album_id": 100,
            "albumid": 100,
            "albumId": 100,
        }
        for key, weight in object_patterns.items():
            pattern = re.compile(
                rf'(?:["\']{re.escape(key)}["\']|(?<![A-Za-z0-9_$]){re.escape(key)}(?![A-Za-z0-9_$]))'
                rf'\s*[:=]\s*'
                rf'(?:["\']?)([A-Za-z0-9._:-]{{5,300}})(?:["\']?)',
                re.I,
            )
            for match in pattern.finditer(script):
                value = match.group(1)
                if not is_content_id(value):
                    continue
                collector.add(
                    value,
                    role="OBJECT_ID",
                    source=source,
                    key=key,
                    weight=weight,
                )
        opaque_pattern = re.compile(
            r"\bpfbid[A-Za-z0-9_-]{6,299}\b",
            re.I,
        )
        for match in opaque_pattern.finditer(script):
            value = match.group(0)
            if not is_opaque_content_id(value):
                continue
            start = max(
                0,
                match.start() - 700,
            )
            end = min(
                len(script),
                match.end() + 700,
            )
            context = clean_text(
                script[start:end]
            )
            collector.add(
                value,
                role="OBJECT_ID",
                source=source,
                key="opaque_content_id",
                weight=115,
                neighbor=context,
            )
        semantic_objects = (
            "user",
            "profile",
            "owner",
            "author",
            "publisher",
            "actor",
            "creator",
            "from",
        )
        for semantic in semantic_objects:
            pattern = re.compile(
                rf'(?:["\']{semantic}["\']|(?<![A-Za-z0-9_$]){semantic}(?![A-Za-z0-9_$]))'
                rf'\s*:\s*\{{'
                rf'.{{0,5000}}?'
                rf'(?:["\']id["\']|(?<![A-Za-z0-9_$])id(?![A-Za-z0-9_$]))'
                rf'\s*:\s*(?:["\']?)(\d{{5,30}})(?:["\']?)',
                re.I | re.S,
            )
            for match in pattern.finditer(script):
                value = match.group(1)
                if not is_numeric_id(value):
                    continue
                weight_map = {
                    "user": 112,
                    "profile": 112,
                    "owner": 102,
                    "author": 100,
                    "publisher": 100,
                    "actor": 82,
                    "creator": 88,
                    "from": 96,
                }
                collector.add(
                    value,
                    role="USER_CANDIDATE",
                    source=source,
                    key=f"{semantic}.id",
                    weight=weight_map.get(
                        semantic,
                        70,
                    ),
                    neighbor=semantic,
                    entity_type="UNKNOWN",
                )
        username_pattern = re.compile(
            r'["\']username["\']'
            r'\s*:\s*["\']'
            r'([^"\']+)'
            r'["\']',
            re.I,
        )
        for match in username_pattern.finditer(script):
            username = clean_text(
                match.group(1)
            ).lstrip("@")
            if username:
                collector.add(
                    username,
                    role="USERNAME",
                    source=source,
                    key="username",
                    weight=65,
                )
        profile_url_pattern = re.compile(
            r'["\'](?:profile_url|profile_uri)["\']'
            r'\s*:\s*["\']'
            r'(https?://[^"\']+)'
            r'["\']',
            re.I,
        )
        for match in profile_url_pattern.finditer(script):
            value = clean_text(
                match.group(1)
            )
            if (
                value
                and is_facebook_host(
                    urlparse(value).netloc
                )
            ):
                collector.add(
                    value,
                    role="PROFILE_URL",
                    source=source,
                    key="profile_url",
                    weight=80,
                )

def scan_html_profile_links(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    """Harvest explicit public profile URLs from raw HTML.

    This is intentionally URL-based: a vanity name is only correlated to a
    numeric UID after the profile URL itself is fetched and the profile
    response exposes a matching numeric identity.
    """
    html = snapshot.html or ""
    if not html:
        return

    # Facebook profile URLs may appear escaped inside bootstrap JSON/HTML.
    pattern = re.compile(
        r'https?(?:\\u002F|/){2}'
        r'(?:www\\.|m\\.|mbasic\\.)?facebook\\.com'
        r'(?:\\u002F|/)+'
        r'([^"\'<>\s?&\\]+)',
        re.I,
    )
    for match in pattern.finditer(html):
        username = match.group(1)
        username = username.replace("\\u002F", "/").split("/")[0]
        username = html_lib.unescape(username)
        if not username:
            continue
        parsed = URLParser.parse(
            "https://www.facebook.com/" + username
        )
        if parsed.kind == "PROFILE" and parsed.username:
            collector.add(
                "https://www.facebook.com/" + quote(
                    parsed.username,
                    safe="@.*-",
                ),
                role="PROFILE_URL",
                source="html:profile_link",
                key="profile_url",
                weight=78,
            )

def scan_html_identity(
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    """High-recall HTML identity pass with strict semantic guards.

    This complements JSON/script parsing because Facebook frequently leaves
    serialized profile state in raw HTML, escaped HTML attributes, meta tags,
    and bootstrap payloads.  Numeric values are only promoted when they occur
    in a USER/profile semantic pattern; arbitrary numeric IDs are ignored.
    """
    html = snapshot.html or ""
    if not html:
        return

    # Direct identity keys that are safe enough to harvest from HTML.
    direct_patterns = {
        "user_id": 122,
        "userid": 122,
        "userId": 122,
        "userID": 122,
        "profile_id": 124,
        "profileid": 124,
        "profileId": 124,
        "profileID": 124,
        "profile_uid": 124,
        "profileUid": 124,
        "profileUID": 124,
        "profile_owner_id": 122,
        "profileOwnerId": 122,
        "owner_id": 106,
        "ownerId": 106,
        "ownerID": 106,
        "author_id": 108,
        "authorId": 108,
        "authorID": 108,
        "publisher_id": 110,
        "publisherId": 110,
        "publisherID": 110,
        "from_id": 106,
        "fromId": 106,
        "fromID": 106,
        "actor_id": 92,
        "actorId": 92,
        "creator_id": 92,
        "creatorId": 92,
    }
    for key, weight in direct_patterns.items():
        pattern = re.compile(
            rf'(?:["\']{re.escape(key)}["\']|(?<![A-Za-z0-9_$]){re.escape(key)}(?![A-Za-z0-9_$]))'
            rf'\s*[:=]\s*(?:["\']?)(\d{{5,30}})(?:["\']?)',
            re.I,
        )
        for m in pattern.finditer(html):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            a = max(0, m.start() - 900)
            b = min(len(html), m.end() + 900)
            context = clean_text(html[a:b])
            low = context.lower()
            entity_type = "UNKNOWN"
            if any(x in low for x in ("group_id", "groupid", '"group"', "group/")):
                entity_type = "GROUP"
            elif any(x in low for x in ("page_id", "pageid", '"page"', "page/")):
                entity_type = "PAGE"
            elif any(x in low for x in ("event_id", "eventid", '"event"', "event/")):
                entity_type = "EVENT"
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:identity",
                key=key,
                weight=weight,
                neighbor=context[:1200],
                entity_type=entity_type,
            )

    # profile.php?id=... is one of the strongest public profile-ID forms.
    profile_route = re.compile(
        r'(?:profile\.php\?(?:[^"\'<>#&]*&)?id=|profile\.php\?id=|fb://profile/)'
        r'(\d{5,30})',
        re.I,
    )
    for m in profile_route.finditer(html):
        value = m.group(1)
        if is_numeric_id(value):
            a = max(0, m.start() - 500)
            b = min(len(html), m.end() + 700)
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:profile_route",
                key="profile.php?id",
                weight=128,
                neighbor=clean_text(html[a:b]),
                entity_type="USER",
            )

    # Nested semantic objects: {"profile_owner":{"id":"..."}},
    # {"author":{"id":...}}, etc.  The bounded window prevents a random
    # ID thousands of characters away from being associated with a user.
    for semantic, weight in {
        "profile_owner": 124,
        "profileOwner": 124,
        "user": 118,
        "profile": 118,
        "author": 108,
        "publisher": 110,
        "owner": 106,
        "from": 104,
        "actor": 92,
        "creator": 92,
    }.items():
        pattern = re.compile(
            rf'(?:["\']{re.escape(semantic)}["\']|(?<![A-Za-z0-9_$]){re.escape(semantic)}(?![A-Za-z0-9_$]))'
            rf'\s*:\s*\{{[^\{{\}}]{{0,5000}}?'
            rf'(?:["\']id["\']|(?<![A-Za-z0-9_$])id(?![A-Za-z0-9_$]))'
            rf'\s*:\s*(?:["\']?)(\d{{5,30}})(?:["\']?)',
            re.I | re.S,
        )
        for m in pattern.finditer(html):
            value = m.group(1)
            if not is_numeric_id(value):
                continue
            a = max(0, m.start() - 700)
            b = min(len(html), m.end() + 700)
            collector.add(
                value,
                role="USER_CANDIDATE",
                source="html:nested_identity",
                key=f"{semantic}.id",
                weight=weight,
                neighbor=clean_text(html[a:b]),
                entity_type="USER" if semantic in {"user", "profile", "profile_owner", "profileOwner"} else "UNKNOWN",
            )

    # A username/profile URL and UID occurring in the same small HTML window
    # is an important correlation signal, but not a standalone proof.
    username = clean_text(
        getattr(snapshot, "meta", {}).get("profile:username", "")
    ).lstrip("@")
    username_candidates = []
    if username:
        username_candidates.append(username)
    for m in re.finditer(
        r'(?:["\']username["\']|username)\s*[:=]\s*["\']([^"\']{1,120})["\']',
        html,
        re.I,
    ):
        u = clean_text(m.group(1)).lstrip("@")
        if u:
            username_candidates.append(u)
    username_candidates = unique_keep_order(username_candidates)

    if username_candidates:
        for u in username_candidates[:20]:
            if not u:
                continue
            for m in re.finditer(re.escape(u), html, re.I):
                a = max(0, m.start() - 1200)
                b = min(len(html), m.end() + 1200)
                window = html[a:b]
                for im in re.finditer(
                    r'(?:profile(?:_owner)?(?:_id|Id|ID)?|user(?:_id|Id|ID)|owner_id|author_id|publisher_id|from_id)'
                    r'\s*["\']?\s*[:=]\s*["\']?(\d{5,30})',
                    window,
                    re.I,
                ):
                    value = im.group(1)
                    if not is_numeric_id(value):
                        continue
                    collector.add(
                        value,
                        role="USER_CANDIDATE",
                        source="html:username_window",
                        key="username_correlated_id",
                        weight=126,
                        neighbor=clean_text(window[:1800]),
                        entity_type="USER",
                    )

class IdentityCorrelation:
    IDENTITY_KEYS = {
        "user",
        "profile",
        "owner",
        "author",
        "publisher",
        "actor",
        "creator",
        "from",
    }
    @staticmethod
    def normalize_username(
        value: str,
    ) -> str:
        return (
            clean_text(value)
            .lstrip("@")
            .lower()
        )
    @classmethod
    def score_candidate(
        cls,
        candidate: str,
        username: str,
        snapshots: List[PageSnapshot],
        collector: EvidenceCollector,
        shape: Optional[URLShape] = None,
    ) -> Tuple[
        float,
        List[str],
    ]:
        if not is_numeric_id(candidate):
            return 0.0, []
        score = 0.0
        signals: List[str] = []
        wanted = cls.normalize_username(
            username
        )
        evidences = [
            e
            for e in collector.by_role(
                "USER_CANDIDATE"
            )
            if e.value == candidate
        ]
        if not evidences:
            return 0.0, []
        object_ids = {
            e.value
            for e in collector.by_role(
                "OBJECT_ID"
            )
        }
        page_ids = {
            e.value
            for e in collector.by_role(
                "PAGE_ID"
            )
        }
        group_ids = {
            e.value
            for e in collector.by_role(
                "GROUP_ID"
            )
        }
        event_ids = {
            e.value
            for e in collector.by_role(
                "EVENT_ID"
            )
        }
        if candidate in (
            object_ids
            | page_ids
            | group_ids
            | event_ids
        ):
            return (
                0.0,
                [
                    "candidate bị loại vì là "
                    "content/entity ID"
                ],
            )
        strongest = max(
            e.weight
            for e in evidences
        )
        if strongest >= 108:
            score += 32
            signals.append(
                "strong profile/user identity field"
            )
        elif strongest >= 100:
            score += 27
            signals.append(
                "owner/author identity field"
            )
        elif strongest >= 88:
            score += 18
            signals.append(
                "creator identity candidate"
            )
        elif strongest >= 70:
            score += 10
        source_set = {
            e.source
            for e in evidences
            if e.independent
        }
        if len(source_set) >= 2:
            score += 18
            signals.append(
                "UID xuất hiện từ nhiều nguồn độc lập"
            )
        if len(source_set) >= 3:
            score += 10
        if any(
            e.entity_type
            in {
                "USER",
                "USER_CANDIDATE",
            }
            for e in evidences
        ):
            score += 18
            signals.append(
                "candidate có USER semantics"
            )
        candidate_context = " ".join(
            clean_text(e.neighbor)
            for e in evidences
        ).lower()
        if wanted:
            username_context_match = False
            for evidence in evidences:
                context = clean_text(
                    evidence.neighbor
                ).lower()
                if not context:
                    continue
                if (
                    wanted in context
                    and any(
                        marker in context
                        for marker in (
                            "user",
                            "profile",
                            "owner",
                            "publisher",
                            "author",
                            "from",
                        )
                    )
                ):
                    username_context_match = True
                    break
            if username_context_match:
                score += 25
                signals.append(
                    "publisher UID ↔ username "
                    "semantic correlation"
                )
        for ps in snapshots:
            html = ps.html or ""
            if not html:
                continue
            candidate_pattern = re.compile(
                rf"(?<!\d)"
                rf"{re.escape(candidate)}"
                rf"(?!\d)"
            )
            if not candidate_pattern.search(html):
                continue
            score += 12
            if "profile" in (
                ps.final_url
                or ps.url
                or ""
            ).lower():
                score += 5
                signals.append(
                    "profile HTML chứa candidate UID"
                )
            if wanted:
                username_found = (
                    wanted
                    in html.lower()
                )
                if username_found:
                    score += 10
                    signals.append(
                        "profile HTML chứa username"
                    )
            canonical = clean_text(
                ps.meta.get(
                    "og:url",
                    "",
                )
            )
            if canonical:
                canonical_lower = (
                    canonical.lower()
                )
                if (
                    wanted
                    and wanted
                    in canonical_lower
                ):
                    score += 20
                    signals.append(
                        "canonical → username khớp"
                    )
            title = clean_text(
                ps.meta.get(
                    "og:title",
                    "",
                )
            ).lower()
            if (
                wanted
                and wanted in title
            ):
                score += 5
                signals.append(
                    "profile title → username khớp"
                )
        if shape and shape.kind in {
            "POST",
            "REEL",
            "VIDEO",
            "PHOTO",
            "STORY",
            "ALBUM",
        }:
            for evidence in evidences:
                context = (
                    evidence.neighbor
                    or ""
                ).lower()
                key = normalize_key(
                    evidence.key
                )
                if key in {
                    "author_id",
                    "author.id",
                    "publisher_id",
                    "publisher.id",
                    "owner_id",
                    "owner.id",
                    "from_id",
                    "from.id",
                    "profile_owner_id",
                    "profileownerid",
                    "profile_owner.uid",
                }:
                    score += 20
                    signals.append(
                        "content → publisher "
                        "identity correlation"
                    )
                    break
                if any(
                    marker in context
                    for marker in (
                        '"author"',
                        '"publisher"',
                        '"owner"',
                        '"from"',
                    )
                ):
                    score += 12
                    signals.append(
                        "content → identity context"
                    )
                    break
        return (
            min(score, 100.0),
            unique_keep_order(signals),
        )
def scan_url_evidence(
    shape: URLShape,
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
):
    if shape.post_id:
        collector.add(
            shape.post_id,
            role="OBJECT_ID",
            source="url_route",
            key="post_id",
            weight=110,
        )
    if shape.reel_id:
        collector.add(
            shape.reel_id,
            role="OBJECT_ID",
            source="url_route",
            key="reel_id",
            weight=110,
        )
    if shape.video_id:
        collector.add(
            shape.video_id,
            role="OBJECT_ID",
            source="url_route",
            key="video_id",
            weight=110,
        )
    if shape.photo_id:
        collector.add(
            shape.photo_id,
            role="OBJECT_ID",
            source="url_route",
            key="photo_id",
            weight=110,
        )
    if shape.story_id:
        collector.add(
            shape.story_id,
            role="OBJECT_ID",
            source="url_route",
            key="story_id",
            weight=110,
        )
    if shape.group_id:
        collector.add(
            shape.group_id,
            role="GROUP_ID",
            source="url_route",
            key="group_id",
            weight=125,
            entity_type="GROUP",
        )
    if shape.page_id:
        collector.add(
            shape.page_id,
            role="PAGE_ID",
            source="url_route",
            key="page_id",
            weight=125,
            entity_type="PAGE",
        )
    if shape.numeric_path_id:
        collector.add(
            shape.numeric_path_id,
            role="ROUTE_NUMERIC_ID",
            source="url_route",
            key="numeric_path_id",
            weight=35,
        )
    if shape.username:
        collector.add(
            shape.username,
            role="USERNAME",
            source="url_route",
            key="username",
            weight=65,
        )
    if snapshot.final_url:
        collector.add(
            snapshot.final_url,
            role="FINAL_URL",
            source="redirect",
            weight=85,
        )
@dataclass
class EntityClassification:
    publisher: str = "UNKNOWN"
    confidence: float = 0.0
    user_uid: str = ""
    page_id: str = ""
    group_id: str = ""
    event_id: str = ""
    author_uid: str = ""
    signals: List[str] = field(
        default_factory=list
    )
class EntityClassifier:
    CONTENT_KINDS = {
        "POST",
        "REEL",
        "VIDEO",
        "PHOTO",
        "STORY",
        "ALBUM",
        "GROUP_POST",
    }
    @staticmethod
    def _candidate_has_entity(
        collector: EvidenceCollector,
        value: str,
        roles: Set[str],
    ) -> bool:
        for evidence in collector.items:
            if evidence.value != value:
                continue
            if evidence.role in roles:
                return True
        return False
    @staticmethod
    def _route_publisher_type(
        shape: URLShape,
    ) -> str:
        """
        HARD ROUTE TYPE.
        Route semantics được ưu tiên hơn
        các ID phụ xuất hiện ngẫu nhiên trong HTML.
        """
        if shape.route_entity == "GROUP":
            return "GROUP"
        if shape.route_entity == "PAGE":
            return "PAGE"
        if shape.kind == "PROFILE":
            return "USER"
        if (
            shape.kind
            in EntityClassifier.CONTENT_KINDS
            and (shape.username or (shape.kind == "STORY" and shape.numeric_path_id))
            and shape.route_entity != "PAGE"
            and shape.route_entity != "GROUP"
        ):
            return "USER"
        return "UNKNOWN"
    def classify(
        self,
        shape: URLShape,
        collector: EvidenceCollector,
        snapshots: List[PageSnapshot],
    ) -> EntityClassification:
        result = EntityClassification()
        page_ids = unique_keep_order(
            collector.values_for(
                "PAGE_ID"
            )
        )
        group_ids = unique_keep_order(
            collector.values_for(
                "GROUP_ID"
            )
        )
        event_ids = unique_keep_order(
            collector.values_for(
                "EVENT_ID"
            )
        )
        user_candidates = unique_keep_order(
            collector.values_for(
                "USER_CANDIDATE"
            )
        )
        route_type = (
            self._route_publisher_type(
                shape
            )
        )
        if route_type == "GROUP":
            result.publisher = "GROUP"
            result.confidence = 100
            if shape.group_id:
                result.group_id = shape.group_id
            elif group_ids:
                result.group_id = group_ids[0]
        elif route_type == "PAGE":
            result.publisher = "PAGE"
            result.confidence = 100
            if shape.page_id:
                result.page_id = shape.page_id
            elif page_ids:
                result.page_id = page_ids[0]
        elif route_type == "USER":
            result.publisher = "USER"
            result.confidence = (
                100
                if shape.kind == "PROFILE"
                else 96
            )
            if (
                shape.kind == "PROFILE"
                and shape.numeric_path_id
                and is_numeric_id(
                    shape.numeric_path_id
                )
            ):
                result.user_uid = (
                    shape.numeric_path_id
                )
        elif event_ids:
            result.publisher = "EVENT"
            result.confidence = 95
            result.event_id = event_ids[0]
        if result.publisher == "UNKNOWN":
            if shape.group_id:
                result.publisher = "GROUP"
                result.confidence = 100
                result.group_id = shape.group_id
            elif shape.page_id:
                result.publisher = "PAGE"
                result.confidence = 100
                result.page_id = shape.page_id
            elif group_ids:
                result.publisher = "GROUP"
                result.confidence = 90
                result.group_id = group_ids[0]
            elif page_ids:
                result.publisher = "PAGE"
                result.confidence = 90
                result.page_id = page_ids[0]
            elif event_ids:
                result.publisher = "EVENT"
                result.confidence = 90
                result.event_id = event_ids[0]
        if result.publisher == "USER":
            if (
                shape.kind == "PROFILE"
                and shape.numeric_path_id
            ):
                result.user_uid = (
                    shape.numeric_path_id
                )
            else:
                ranked = rank_user_candidates(
                    collector,
                    username=shape.username,
                    shape=shape,
                )
                if ranked:
                    result.user_uid = ranked[0][0]
        if result.publisher in {
            "PAGE",
            "GROUP",
        }:
            excluded = set(
                page_ids
                + group_ids
                + event_ids
            )
            ranked = rank_author_candidates(
                collector,
                excluded_ids=excluded,
            )
            if ranked:
                result.author_uid = ranked[0][0]
        if shape.username:
            result.signals.append(
                "route → publisher username"
            )
        if shape.kind in self.CONTENT_KINDS:
            result.signals.append(
                f"route → {shape.kind}"
            )
        if result.publisher == "USER":
            result.signals.append(
                "publisher type → USER"
            )
        elif result.publisher == "PAGE":
            result.signals.append(
                "publisher type → PAGE"
            )
        elif result.publisher == "GROUP":
            result.signals.append(
                "publisher type → GROUP"
            )
        if page_ids:
            result.signals.append(
                "embedded page_id detected"
            )
        if group_ids:
            result.signals.append(
                "embedded group_id detected"
            )
        if user_candidates:
            result.signals.append(
                "identity candidate found"
            )
        result.signals = (
            unique_keep_order(
                result.signals
            )[:MAX_SIGNALS]
        )
        return result
def rank_user_candidates(
    collector: EvidenceCollector,
    username: str = "",
    shape: Optional[URLShape] = None,
) -> List[Tuple[str, float]]:
    scores: Dict[
        str,
        float,
    ] = {}
    evidence_map: Dict[
        str,
        List[Evidence],
    ] = {}
    wanted = (
        clean_text(username)
        .lstrip("@")
        .lower()
    )
    object_ids = {
        e.value
        for e in collector.by_role(
            "OBJECT_ID"
        )
        if is_numeric_id(e.value)
    }
    page_ids = {
        e.value
        for e in collector.by_role(
            "PAGE_ID"
        )
        if is_numeric_id(e.value)
    }
    group_ids = {
        e.value
        for e in collector.by_role(
            "GROUP_ID"
        )
        if is_numeric_id(e.value)
    }
    event_ids = {
        e.value
        for e in collector.by_role(
            "EVENT_ID"
        )
        if is_numeric_id(e.value)
    }
    blocked = (
        object_ids
        | page_ids
        | group_ids
        | event_ids
    )
    route_entity = (
        shape.route_entity
        if shape
        else ""
    )
    for evidence in collector.by_role(
        "USER_CANDIDATE"
    ):
        value = evidence.value
        if not is_numeric_id(value):
            continue
        if value in blocked:
            continue
        if route_entity == "USER":
            if evidence.entity_type in {
                "PAGE",
                "GROUP",
                "EVENT",
            }:
                continue
        scores.setdefault(
            value,
            0.0,
        )
        evidence_map.setdefault(
            value,
            [],
        ).append(evidence)
    ranked = []
    for value, evidences in evidence_map.items():
        score = 0.0
        keys = {
            normalize_key(e.key)
            for e in evidences
        }
        sources = {
            e.source
            for e in evidences
            if e.independent
        }
        contexts = " ".join(
            clean_text(e.neighbor)
            for e in evidences
        ).lower()
        if keys & {
            "user_id",
            "userid",
            "user.uid",
            "uid",
            "user_uid",
            "useruid",
        }:
            score += 58
        if keys & {
            "profile_id",
            "profileid",
            "profile.uid",
            "profile.id",
            "profile_uid",
            "profileuid",
            "profile_owner_id",
            "profileownerid",
            "profile_owner.uid",
        }:
            score += 58
        if keys & {
            "publisher_id",
            "publisherid",
            "publisher.id",
        }:
            score += 55
        if keys & {
            "author_id",
            "authorid",
            "author.id",
        }:
            score += 48
        if keys & {
            "owner_id",
            "ownerid",
            "owner.id",
        }:
            score += 45
        if keys & {
            "from_id",
            "fromid",
            "from.id",
        }:
            score += 42
        if keys & {
            "creator_id",
            "creatorid",
            "creator.id",
        }:
            score += 5
        if keys & {
            "actor_id",
            "actorid",
            "actor.id",
        }:
            score += 3
        if len(sources) >= 2:
            score += 18
        if len(sources) >= 3:
            score += 12
        if len(sources) >= 4:
            score += 8
        if any(
            e.entity_type
            in {
                "USER",
                "USER_CANDIDATE",
            }
            for e in evidences
        ):
            score += 15
        username_match = False
        if wanted:
            for evidence in evidences:
                context = clean_text(
                    evidence.neighbor
                ).lower()
                if wanted in context:
                    username_match = True
                    break
        if username_match:
            score += 15
        publisher_key_match = bool(
            keys & {
                "publisher_id",
                "publisherid",
                "publisher.id",
                "author_id",
                "authorid",
                "author.id",
                "owner_id",
                "ownerid",
                "owner.id",
                "from_id",
                "fromid",
                "from.id",
            }
        )
        if (
            shape
            and shape.kind in {
                "POST",
                "REEL",
                "VIDEO",
                "PHOTO",
                "STORY",
            }
            and publisher_key_match
        ):
            score += 30
        if any(
            e.entity_type
            in {
                "PAGE",
                "GROUP",
                "EVENT",
            }
            for e in evidences
        ):
            score -= 100
        if (
            shape
            and shape.username
            and shape.route_entity == "USER"
        ):
            score += 25
        only_weak_identity = (
            bool(keys)
            and keys <= {
                "creator_id",
                "creatorid",
                "creator.id",
                "actor_id",
                "actorid",
                "actor.id",
            }
        )
        if only_weak_identity:
            score -= 30
        if score > 0:
            ranked.append(
                (
                    value,
                    score,
                )
            )
    ranked.sort(
        key=lambda x: x[1],
        reverse=True,
    )
    return ranked
def rank_author_candidates(
    collector: EvidenceCollector,
    excluded_ids: Optional[Set[str]] = None,
) -> List[Tuple[str, float]]:
    excluded_ids = (
        excluded_ids or set()
    )
    scores: Dict[
        str,
        float,
    ] = {}
    sources: Dict[
        str,
        Set[str],
    ] = {}
    for evidence in collector.by_role(
        "USER_CANDIDATE"
    ):
        value = evidence.value
        if not is_numeric_id(value):
            continue
        if value in excluded_ids:
            continue
        object_values = {
            x.value
            for x in collector.by_role(
                "OBJECT_ID"
            )
        }
        if value in object_values:
            continue
        score = evidence.weight
        key = normalize_key(
            evidence.key
        )
        if key in {
            "author_id",
            "author.id",
            "publisher_id",
            "publisher.id",
            "owner_id",
            "owner.id",
            "from_id",
            "from.id",
        }:
            score += 30
        elif key in {
            "creator_id",
            "creator.id",
        }:
            score += 10
        elif key in {
            "actor_id",
            "actor.id",
        }:
            score += 5
        scores[value] = (
            scores.get(value, 0)
            + score
        )
        sources.setdefault(
            value,
            set(),
        ).add(
            evidence.source
        )
    ranked = []
    for value, score in scores.items():
        source_count = len(
            sources.get(
                value,
                set(),
            )
        )
        if source_count >= 2:
            score += 20
        if source_count >= 3:
            score += 10
        ranked.append(
            (
                value,
                score,
            )
        )
    ranked.sort(
        key=lambda x: x[1],
        reverse=True,
    )
    return ranked
@dataclass
class ContentClassification:
    content_type: str = "UNKNOWN"
    object_id: str = ""
    post_id: str = ""
    reel_id: str = ""
    video_id: str = ""
    photo_id: str = ""
    story_id: str = ""
    album_id: str = ""
    confidence: float = 0.0
    signals: List[str] = field(
        default_factory=list
    )
class ContentClassifier:
    def classify(
        self,
        shape: URLShape,
        collector: EvidenceCollector,
        snapshot: PageSnapshot,
    ) -> ContentClassification:
        result = ContentClassification()
        if shape.kind == "GROUP_POST":
            result.content_type = "GROUP_POST"
            result.post_id = (
                shape.post_id
                or self._find_object(
                    collector,
                    "post_id",
                )
            )
            result.object_id = (
                result.post_id
            )
            result.confidence = 99
            result.signals.append(
                "route → GROUP_POST"
            )
            return result
        if shape.kind == "POST":
            result.content_type = "POST"
            result.post_id = (
                shape.post_id
                or self._find_object(
                    collector,
                    "post_id",
                )
            )
            result.object_id = (
                result.post_id
            )
            result.confidence = 99
            result.signals.append(
                "route → POST"
            )
            return result
        if shape.kind == "REEL":
            result.content_type = "REEL"
            result.reel_id = (
                shape.reel_id
                or self._find_object(
                    collector,
                    "reel_id",
                )
            )
            result.post_id = (
                self._find_object(
                    collector,
                    "post_id",
                )
            )
            result.object_id = (
                result.reel_id
                or result.post_id
            )
            result.confidence = 99
            result.signals.append(
                "route → REEL"
            )
            return result
        if shape.kind == "VIDEO":
            result.content_type = "VIDEO"
            result.video_id = (
                shape.video_id
                or self._find_object(
                    collector,
                    "video_id",
                )
            )
            result.post_id = (
                self._find_object(
                    collector,
                    "post_id",
                )
            )
            result.object_id = (
                result.video_id
                or result.post_id
            )
            result.confidence = 98
            result.signals.append(
                "route → VIDEO"
            )
            return result
        if shape.kind == "PHOTO":
            result.content_type = "PHOTO"
            result.photo_id = (
                shape.photo_id
                or self._find_object(
                    collector,
                    "photo_id",
                )
            )
            result.post_id = (
                self._find_object(
                    collector,
                    "post_id",
                )
            )
            result.object_id = (
                result.photo_id
                or result.post_id
            )
            result.confidence = 98
            result.signals.append(
                "route → PHOTO"
            )
            return result
        if shape.kind == "STORY":
            result.content_type = "STORY"
            result.story_id = (
                shape.story_id
                or self._find_object(
                    collector,
                    "story_fbid",
                )
            )
            result.object_id = (
                result.story_id
            )
            result.confidence = 98
            result.signals.append(
                "route → STORY"
            )
            return result
        if shape.kind == "ALBUM":
            result.content_type = "ALBUM"
            result.album_id = (
                shape.album_id
                or self._find_object(
                    collector,
                    "album_id",
                )
            )
            result.object_id = (
                result.album_id
            )
            result.confidence = 97
            result.signals.append(
                "route → ALBUM"
            )
            return result
        if shape.kind == "PROFILE":
            result.content_type = "PROFILE"
            result.confidence = 99
            result.signals.append(
                "route → PROFILE"
            )
            return result
        if collector.by_role(
            "OBJECT_ID"
        ):
            evidence = collector.by_role(
                "OBJECT_ID"
            )
            keys = {
                normalize_key(x.key)
                for x in evidence
            }
            if "reel_id" in keys:
                result.content_type = "REEL"
            elif "video_id" in keys:
                result.content_type = "VIDEO"
            elif "photo_id" in keys:
                result.content_type = "PHOTO"
            elif "story_fbid" in keys:
                result.content_type = "STORY"
            elif "post_id" in keys:
                result.content_type = "POST"
            else:
                result.content_type = "CONTENT"
            result.confidence = 70
            result.signals.append(
                "embedded object evidence"
            )
        return result
    @staticmethod
    def _find_object(
        collector: EvidenceCollector,
        key: str,
    ) -> str:
        values = [
            x.value
            for x in collector.by_role(
                "OBJECT_ID"
            )
            if normalize_key(
                x.key
            ) == key
        ]
        return (
            values[0]
            if values
            else ""
        )
@dataclass
class ProfileInfo:
    name: str = ""
    username: str = ""
    profile_url: str = ""
    avatar_url: str = ""
    bio: str = ""
    entity_type: str = "UNKNOWN"
    page_name: str = ""
    group_name: str = ""
    cover_url: str = ""
def extract_profile_info(
    shape: URLShape,
    snapshot: PageSnapshot,
    collector: EvidenceCollector,
    classification: EntityClassification,
) -> ProfileInfo:
    info = ProfileInfo()
    info.entity_type = (
        classification.publisher
    )
    usernames = collector.values_for(
        "USERNAME"
    )
    if shape.username:
        info.username = shape.username
    elif usernames:
        info.username = usernames[0]
    profile_urls = collector.values_for(
        "PROFILE_URL"
    )
    if profile_urls:
        for value in profile_urls:
            if is_facebook_host(
                urlparse(value).netloc
            ):
                info.profile_url = (
                    normalize_facebook_url(
                        value
                    )
                )
                break
    if (
        not info.profile_url
        and info.username
        and classification.publisher
        in {
            "USER",
            "PAGE",
        }
    ):
        info.profile_url = (
            "https://www.facebook.com/"
            + quote(
                info.username,
                safe="@.*-",
            )
        )
    names = collector.values_for(
        "NAME"
    )
    if names:
        for name in names:
            if (
                not generic_name(name)
                and not looks_like_video_title(
                    name
                )
            ):
                info.name = truncate(
                    name,
                    250,
                )
                break
    if not info.name:
        og_title = snapshot.meta.get(
            "og:title",
            "",
        )
        og_title = clean_title(
            og_title
        )
        if (
            og_title
            and not generic_name(
                og_title
            )
            and not looks_like_video_title(
                og_title
            )
        ):
            info.name = truncate(
                og_title,
                250,
            )
    bios = collector.values_for(
        "BIO"
    )
    if bios:
        info.bio = truncate(
            bios[0],
            800,
        )
    else:
        descriptions = collector.values_for(
            "DESCRIPTION"
        )
        if descriptions:
            info.bio = truncate(
                descriptions[0],
                800,
            )
    images = collector.values_for(
        "IMAGE"
    )
    if images:
        info.avatar_url = images[0]
    return info
@dataclass
class VerificationResult:
    verified: bool = False
    uid: str = ""
    confidence: float = 0.0
    sources: int = 0
    signals: List[str] = field(
        default_factory=list
    )
    reason: str = ""
class IdentityVerifier:
    def verify(
        self,
        *,
        shape: URLShape,
        snapshot: PageSnapshot,
        profile_snapshots: List[PageSnapshot],
        collector: EvidenceCollector,
        classification: EntityClassification,
        profile: ProfileInfo,
    ) -> VerificationResult:
        result = VerificationResult()
        if classification.publisher != "USER":
            result.reason = (
                "Publisher không phải USER. "
                "Không trả UID cá nhân."
            )
            return result
        if (
            shape.kind == "PROFILE"
            and shape.numeric_path_id
            and is_numeric_id(
                shape.numeric_path_id
            )
        ):
            result.uid = (
                shape.numeric_path_id
            )
            result.verified = True
            result.confidence = 99.5
            result.sources = 1
            result.signals.append(
                "profile.php?id → "
                "explicit USER UID"
            )
            return result
        if (
            shape.kind in {
                "POST",
                "REEL",
                "VIDEO",
                "PHOTO",
                "STORY",
                "ALBUM",
            }
            and not shape.username
            and not (shape.kind == "STORY" and shape.numeric_path_id and shape.route_entity == "USER")
        ):
            result.reason = (
                "Content URL không có publisher username "
                "hoặc explicit story owner để correlation."
            )
            return result
        # Explicit /stories/<owner_id>/<token> route: the owner ID is a
        # first-class public identity signal. Only use it when the route is
        # classified as USER and no page/group veto exists.
        if (
            shape.kind == "STORY"
            and shape.route_entity == "USER"
            and is_numeric_id(shape.numeric_path_id)
            and not collector.by_role("PAGE_ID")
            and not collector.by_role("GROUP_ID")
        ):
            result.uid = shape.numeric_path_id
            result.verified = True
            result.confidence = 99.0
            result.sources = 1
            result.signals.append("/stories/<owner_id>/ → explicit USER owner UID")
            return result

        ranked = rank_user_candidates(
            collector,
            username=(
                profile.username
                or shape.username
            ),
            shape=shape,
        )
        if not ranked:
            result.reason = (
                "Không có USER UID candidate "
                "đủ điều kiện."
            )
            return result
        correlator = IdentityCorrelation()
        correlated = []
        username = (
            profile.username
            or shape.username
        )
        for candidate, base_score in ranked[:20]:
            correlation_score, signals = (
                correlator.score_candidate(
                    candidate=candidate,
                    username=username,
                    snapshots=profile_snapshots,
                    collector=collector,
                    shape=shape,
                )
            )
            if correlation_score <= 0:
                continue
            final_score = (
                base_score * 0.40
                + correlation_score * 0.60
            )
            correlated.append(
                (
                    candidate,
                    final_score,
                    signals,
                )
            )
        if not correlated:
            result.reason = (
                "Có USER candidate nhưng "
                "không chứng minh được candidate "
                "thuộc publisher."
            )
            return result
        correlated.sort(
            key=lambda x: x[1],
            reverse=True,
        )
        candidate, score, signals = (
            correlated[0]
        )
        if len(correlated) >= 2:
            second_candidate = (
                correlated[1]
            )
            if (
                second_candidate[1]
                >= score * 0.92
            ):
                result.reason = (
                    "Có nhiều USER UID cạnh tranh "
                    "và chưa đủ bằng chứng để "
                    "chọn publisher UID."
                )
                return result
        if shape.kind in {
            "POST",
            "REEL",
            "VIDEO",
            "PHOTO",
            "STORY",
            "ALBUM",
        }:
            evidence = [
                e
                for e in collector.by_role(
                    "USER_CANDIDATE"
                )
                if e.value == candidate
            ]
            keys = {
                normalize_key(e.key)
                for e in evidence
            }
            has_strong_user_field = bool(
                keys
                & {
                    "user_id",
                    "userid",
                    "profile_id",
                    "profileid",
                    "profile.uid",
                    "profile.id",
                    "profile_uid",
                    "profileuid",
                    "profile_owner_id",
                    "profileownerid",
                    "uid",
                }
            )
            has_publisher_field = bool(
                keys
                & {
                    "publisher_id",
                    "publisherid",
                    "publisher.id",
                    "author_id",
                    "authorid",
                    "author.id",
                    "owner_id",
                    "ownerid",
                    "owner.id",
                    "from_id",
                    "fromid",
                    "from.id",
                }
            )
            has_username_signal = any(
                (
                    "username"
                    in clean_text(
                        e.neighbor
                    ).lower()
                    or (
                        username
                        and username.lower()
                        in clean_text(
                            e.neighbor
                        ).lower()
                    )
                )
                for e in evidence
                if username
            )
            if not (
                has_strong_user_field
                or has_publisher_field
                or has_username_signal
            ):
                result.reason = (
                    "Candidate có UID nhưng "
                    "không có publisher "
                    "identity evidence."
                )
                return result
        if shape.route_entity == "USER":
            evidence = [
                e
                for e in collector.by_role(
                    "USER_CANDIDATE"
                )
                if e.value == candidate
            ]
            keys = {
                normalize_key(e.key)
                for e in evidence
            }
            strong_identity = bool(
                keys
                & {
                    "user_id",
                    "userid",
                    "profile_id",
                    "profileid",
                    "profile.uid",
                    "profile.id",
                    "publisher_id",
                    "publisherid",
                    "publisher.id",
                    "author_id",
                    "authorid",
                    "author.id",
                    "owner_id",
                    "ownerid",
                    "owner.id",
                    "from_id",
                    "fromid",
                    "from.id",
                    "id",
                }
            )
            if not strong_identity:
                result.reason = (
                    "Candidate không có USER identity "
                    "field đủ mạnh cho USER route."
                )
                return result
        if score < 78:
            result.reason = (
                "UID candidate chưa đạt "
                "ngưỡng publisher correlation."
            )
            return result
        result.uid = candidate
        result.verified = True
        result.confidence = min(
            99.5,
            score,
        )
        result.sources = len(
            collector.sources_for(
                candidate
            )
        )
        result.signals.extend(
            signals
        )
        result.signals = (
            unique_keep_order(
                result.signals
            )[:MAX_SIGNALS]
        )
        return result
@dataclass
class ResolveResult:
    input_url: str = ""
    canonical_url: str = ""
    content_url: str = ""
    profile_url: str = ""
    entity_type: str = "UNKNOWN"
    publisher_type: str = "UNKNOWN"
    name: str = ""
    username: str = ""
    avatar_url: str = ""
    bio: str = ""
    page_name: str = ""
    group_name: str = ""
    content_type: str = "UNKNOWN"
    title: str = ""
    post_id: str = ""
    video_id: str = ""
    reel_id: str = ""
    photo_id: str = ""
    story_id: str = ""
    album_id: str = ""
    publisher_id: str = ""
    uid: str = ""
    author_uid: str = ""
    verified: bool = False
    confidence: float = 0.0
    evidence_sources: int = 0
    signals: List[str] = field(
        default_factory=list
    )
    notes: List[str] = field(
        default_factory=list
    )
    status: str = "NOT_VERIFIED"
    elapsed: float = 0.0
    http_status: int = 0
    debug: Dict[str, Any] = field(default_factory=dict)
class ResultCache:
    def __init__(
        self,
        ttl: int = CACHE_TTL,
    ):
        self.ttl = ttl
        self._data: Dict[
            str,
            Tuple[
                float,
                ResolveResult,
            ],
        ] = {}
        self._lock = threading.Lock()
    def get(
        self,
        key: str,
    ) -> Optional[ResolveResult]:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            timestamp, result = item
            if (
                now - timestamp
                > self.ttl
            ):
                self._data.pop(
                    key,
                    None,
                )
                return None
            return result
    def set(
        self,
        key: str,
        result: ResolveResult,
    ):
        with self._lock:
            self._data[key] = (
                time.time(),
                result,
            )
class FacebookResolver:
    def __init__(self):
        self.fetcher = HTTPFetcher()
        self.cache = ResultCache()
        self.profile_sem = (
            threading.Semaphore(
                MAX_PROFILE_CHECKS
            )
        )
    def resolve(
        self,
        url: str,
    ) -> ResolveResult:
        started = time.perf_counter()
        normalized = normalize_facebook_url(
            url
        )
        cached = self.cache.get(
            normalized
        )
        if cached:
            cached.elapsed = (
                time.perf_counter()
                - started
            )
            return cached
        try:
            result = self._resolve(
                normalized
            )
        except Exception:
            LOGGER.exception(
                "Resolver failure for %s",
                normalized,
            )
            result = ResolveResult(
                input_url=normalized,
                canonical_url=normalized,
                content_url=normalized,
                status="FETCH_LIMITED",
                notes=[
                    "Resolver internal error."
                ],
            )
        result.elapsed = (
            time.perf_counter()
            - started
        )
        self.cache.set(
            normalized,
            result,
        )
        return result
    def _resolve(
        self,
        url: str,
    ) -> ResolveResult:
        shape = URLParser.parse(
            url
        )
        result = ResolveResult(
            input_url=url,
            canonical_url=url,
            content_url=url,
        )
        snapshot = self.fetcher.fetch(
            url
        )
        result.http_status = (
            snapshot.status
        )
        final_url = (
            snapshot.final_url
            or url
        )
        final_shape = URLParser.parse(
            final_url
        )
        if shape.kind == "UNKNOWN" and not shape.wrapper and any(
            x.lower() in {"login.php", "logout.php", "checkpoint", "recover", "registration", "reg", "privacy", "security"}
            for x in shape.segments
        ):
            result.status = "FETCH_LIMITED" if snapshot.limited or snapshot.error else "NOT_VERIFIED"
            result.notes.append("Facebook authentication/system endpoint; không phải profile công khai.")
            return result
        original_shape = URLParser.parse(url)
        share_target_snapshot = None
        # Keep every public response from PASS 1 available to mandatory PASS 2.
        # This is critical for /share/<token>: the useful object identifier can
        # appear in a redirect/canonical/embedded representation of a probe,
        # not necessarily in the final generic landing page.
        share_snapshots: List[PageSnapshot] = []
        if original_shape.wrapper:
            # /share/<opaque-token> is not itself an identity.  Resolve it
            # only through ordinary public HTTP redirects/metadata.  Probe
            # Facebook's public host variants with the same exact path; this
            # is discovery, not an authentication bypass.
            probe_urls = []
            parsed_input = urlparse(url)
            exact_path = parsed_input.path or "/"
            exact_query = parsed_input.query
            for host in (
                parsed_input.netloc,
                "www.facebook.com",
                "m.facebook.com",
                "mbasic.facebook.com",
            ):
                if not host:
                    continue
                probe = urlunparse((
                    "https",
                    host,
                    exact_path,
                    "",
                    exact_query,
                    "",
                ))
                probe_urls.append(
                    normalize_facebook_url(probe)
                )
            probe_urls = unique_keep_order(probe_urls)[:MAX_SHARE_PROBES]

            snapshots = [(url, snapshot)]
            for probe_url in probe_urls:
                if probe_url == url:
                    continue
                try:
                    ps = self.fetcher.fetch(
                        probe_url,
                        referer=url,
                    )
                    snapshots.append((probe_url, ps))
                except Exception:
                    LOGGER.debug(
                        "share probe failed: %s",
                        probe_url,
                        exc_info=True,
                    )

            share_snapshots = [ps for _, ps in snapshots]

            share_snapshots = [ps for _, ps in snapshots]

            def public_target_from_snapshot(ps):
                candidates = []

                # Redirect targets are the strongest discovery signal.
                for candidate in ps.redirect_chain:
                    if not candidate:
                        continue
                    parsed = URLParser.parse(candidate)
                    if (
                        parsed.kind in {
                            "POST", "REEL", "VIDEO", "PHOTO",
                            "STORY", "ALBUM", "GROUP_POST",
                            "PROFILE",
                        }
                        and parsed.route_confidence >= 92
                    ):
                        candidates.append(
                            (100.0, candidate, parsed)
                        )

                # og:url / profile:url are weaker than a redirect but still
                # explicit public canonical metadata.
                for key in ("og:url", "profile:url"):
                    candidate = ps.meta.get(key, "")
                    if not candidate or not is_facebook_host(
                        urlparse(candidate).netloc
                    ):
                        continue
                    parsed = URLParser.parse(candidate)
                    if (
                        parsed.kind in {
                            "POST", "REEL", "VIDEO", "PHOTO",
                            "STORY", "ALBUM", "GROUP_POST",
                            "PROFILE",
                        }
                        and parsed.route_confidence >= 92
                    ):
                        candidates.append(
                            (90.0, candidate, parsed)
                        )

                # IMPORTANT: never promote arbitrary links from a share
                # landing page to the share target. Facebook landing pages
                # contain generic STORY/VIDEO/PAGE links (for example Terms,
                # login/help pages). Those links are not evidence that the
                # opaque share token points to that object.
                #
                # Only explicit redirect targets and explicit canonical
                # metadata are allowed to resolve a share token. Identity
                # links remain useful later, after a real target has been
                # established, for author correlation.

                if not candidates:
                    return None
                candidates.sort(
                    key=lambda x: x[0],
                    reverse=True,
                )
                return candidates[0]

            best = None
            for probe_url, ps in snapshots:
                found = public_target_from_snapshot(ps)
                if found is None:
                    continue
                score, target_url, target_shape = found
                if best is None or score > best[0]:
                    best = (
                        score,
                        target_url,
                        target_shape,
                        ps,
                    )

            if best is not None:
                _, target_url, target_shape, source_snapshot = best
                target_snapshot = source_snapshot

                # If the source snapshot itself is not the actual target,
                # fetch the explicit public target once for full evidence.
                if normalize_facebook_url(target_url) != normalize_facebook_url(
                    source_snapshot.final_url or source_snapshot.url
                ):
                    try:
                        target_snapshot = self.fetcher.fetch(
                            target_url,
                            referer=source_snapshot.final_url or url,
                        )
                    except Exception:
                        LOGGER.debug(
                            "share target fetch failed: %s",
                            target_url,
                            exc_info=True,
                        )

                shape = target_shape
                snapshot = target_snapshot
                result.http_status = snapshot.status

                canonical = (
                    snapshot.meta.get("og:url", "")
                    or target_url
                    or snapshot.final_url
                )
                if (
                    canonical
                    and is_facebook_host(
                        urlparse(canonical).netloc
                    )
                ):
                    canonical = normalize_facebook_url(canonical)

                canonical_shape = URLParser.parse(canonical)
                if canonical_shape.kind == "UNKNOWN" and any(
                    part.lower() in {
                        "login.php", "login", "checkpoint", "recover",
                        "registration", "reg", "privacy", "security",
                    }
                    for part in canonical_shape.segments
                ):
                    canonical = ""
                    canonical_shape = URLShape()
                if canonical_shape.kind in {
                    "POST", "REEL", "VIDEO", "PHOTO",
                    "STORY", "ALBUM", "GROUP_POST",
                    "PROFILE",
                }:
                    shape = canonical_shape
                    result.canonical_url = canonical
                    result.content_url = canonical
                else:
                    result.canonical_url = normalize_facebook_url(target_url)
                    result.content_url = result.canonical_url

                result.notes.append(
                    "Share wrapper resolved only from explicit public redirect/canonical metadata."
                )

            if shape.kind == "SHARE_WRAPPER":
                # PASS 1 failed to resolve the wrapper. Do NOT return yet:
                # fixed6 always enters PASS 2 and looks for an explicit object
                # identifier exposed by the responses themselves.
                result.notes.append(
                    "PASS 1: share wrapper chưa resolve được canonical target; chuyển bắt buộc sang PASS 2."
                )
                # Keep the best observed snapshot for evidence scanning, but
                # never treat generic landing-page links as the target.
                best_snapshot = max(
                    snapshots,
                    key=lambda pair: (
                        0 if pair[1].limited or pair[1].error else 1,
                        len(pair[1].html or ""),
                    ),
                )[1]
                snapshot = best_snapshot
                result.http_status = snapshot.status
        else:
            shape = final_shape
        canonical = (
            snapshot.meta.get(
                "og:url",
                "",
            )
            or final_url
            or url
        )
        if is_facebook_host(
            urlparse(
                canonical
            ).netloc
        ):
            canonical = (
                normalize_facebook_url(
                    canonical
                )
            )
        result.canonical_url = (
            canonical
        )
        result.content_url = (
            canonical
            if canonical
            else final_url
        )
        canonical_shape = (
            URLParser.parse(
                result.canonical_url
            )
        )
        if (
            canonical_shape.kind
            not in {
                "UNKNOWN",
                "HOME",
                "SHARE_WRAPPER",
            }
        ):
            shape = canonical_shape
        # Preserve exact content identity from the original URL when a
        # redirect/og:url loses the more specific route.
        for attr in (
            "post_id", "reel_id", "video_id",
            "photo_id", "story_id", "album_id",
            "numeric_path_id", "opaque_token", "username",
        ):
            original_value = getattr(original_shape, attr, "")
            if original_value and not getattr(shape, attr, ""):
                setattr(shape, attr, original_value)
                if shape.kind == "UNKNOWN":
                    shape.kind = original_shape.kind
                    shape.route_entity = original_shape.route_entity
        collector = EvidenceCollector()
        scan_url_evidence(
            shape,
            snapshot,
            collector,
        )
        scan_meta(
            snapshot,
            collector,
        )
        scan_jsonld(
            snapshot,
            collector,
        )
        scan_scripts(
            snapshot,
            collector,
        )
        scan_html_identity(
            snapshot,
            collector,
        )
        scan_html_profile_links(
            snapshot,
            collector,
        )
        content_classifier = (
            ContentClassifier()
        )
        content = (
            content_classifier.classify(
                shape,
                collector,
                snapshot,
            )
        )
        # Never infer STORY/VIDEO/etc. from incidental links on a generic
        # /share/<token> landing page. Only a resolved target or PASS-2 object
        # evidence may establish the content type.
        if original_shape.wrapper and shape.kind == "SHARE_WRAPPER":
            content = ContentClassification()
        # A bare /share/<token> has no content-type evidence merely because
        # the generic Facebook landing page contains links/text mentioning
        # stories, terms, videos, etc.  Do not leak that generic route into
        # the final result.  Once PASS 2 resolves a real object, shape will be
        # upgraded and the later reclassification below can describe it.
        if original_shape.wrapper and shape.kind == "SHARE_WRAPPER":
            content = ContentClassification()
        entity_classifier = (
            EntityClassifier()
        )
        entity = (
            entity_classifier.classify(
                shape,
                collector,
                [snapshot],
            )
        )
        profile = extract_profile_info(
            shape,
            snapshot,
            collector,
            entity,
        )
        profile_urls = (
            self.extract_profile_urls(
                shape,
                snapshot,
                profile,
                entity,
            )
        )
        profile_snapshots = []
        for profile_url in profile_urls[
            :MAX_PROFILE_CHECKS
        ]:
            if (
                profile_url
                == result.content_url
            ):
                profile_snapshots.append(
                    snapshot
                )
                continue
            with self.profile_sem:
                ps = self.fetcher.fetch(
                    profile_url,
                    referer=result.content_url,
                )
            profile_snapshots.append(ps)
            scan_meta(
                ps,
                collector,
            )
            scan_jsonld(
                ps,
                collector,
            )
            scan_scripts(
                ps,
                collector,
            )
            scan_html_identity(
                ps,
                collector,
            )
            scan_html_profile_links(
                ps,
                collector,
            )
            profile_url_from_meta = (
                ps.meta.get(
                    "og:url",
                    "",
                )
            )
            if (
                profile_url_from_meta
                and profile.username
                and profile.username.lower()
                in profile_url_from_meta.lower()
            ):
                collector.add(
                    profile_url_from_meta,
                    role="PROFILE_CANONICAL",
                    source="profile_meta",
                    weight=90,
                )
        # ------------------------------------------------------------------
        # PASS 2 (MANDATORY): OBJECT-ID -> PUBLIC AUTHOR/PROFILE CORRELATION
        # ------------------------------------------------------------------
        # fixed6 deliberately executes this phase after every unsuccessful
        # first-pass resolution. It may have no usable object ID (e.g. a bare
        # opaque share token); in that case it records why it cannot continue.
        phase2_snapshots: List[PageSnapshot] = []
        pass2_ids: List[Tuple[str, str, str]] = []

        def add_pass2_id(kind: str, ident: str, source: str):
            ident = clean_text(ident)
            if not is_content_id(ident):
                return
            key = (kind, ident, source)
            if key not in pass2_ids:
                pass2_ids.append(key)

        # IMPORTANT: PASS 2 also inspects the ORIGINAL INPUT URL.
        # A share permalink can be accompanied by a resolved /stories/... URL
        # containing the owner UID and an opaque story token.  The original
        # URL is stronger evidence than generic landing-page HTML.
        original_input_urls = [original_shape.original, url]
        for original_input_url in original_input_urls:
            try:
                os = URLParser.parse(original_input_url)
            except Exception:
                continue
            if os.numeric_path_id and os.route_entity == "USER":
                add_pass2_id("author", os.numeric_path_id, "original_story_owner")
            if os.story_id:
                add_pass2_id("story", os.story_id, "original_url_story_id")
            if os.opaque_token:
                for decoded_id in decode_facebook_story_token(os.opaque_token):
                    add_pass2_id("story", decoded_id, "decoded_story_token")
                result.debug.setdefault("pass2", {}).setdefault("opaque_tokens", []).append(
                    truncate(os.opaque_token, 120)
                )

        # PASS 2 also follows an explicitly supplied share_url query parameter.
        # Facebook story/shared URLs commonly contain:
        #   share_url=https://www.facebook.com/share/<token>/
        # This is a user-provided/public URL reference, not an auth bypass.
        def harvest_nested_public_urls(seed_url: str):
            seen_local = set()
            queue = [seed_url]
            while queue and len(seen_local) < 12:
                current = queue.pop(0)
                if not current or current in seen_local:
                    continue
                seen_local.add(current)
                try:
                    parsed_current = urlparse(current)
                    qs = parse_qs(parsed_current.query, keep_blank_values=True)
                except Exception:
                    continue
                for key in ("share_url", "url", "target", "redirect_uri"):
                    for value in qs.get(key, []):
                        value = unquote(value)
                        if is_facebook_host(urlparse(value).netloc):
                            try:
                                nested_shape = URLParser.parse(value)
                            except Exception:
                                continue
                            if nested_shape.story_id:
                                add_pass2_id("story", nested_shape.story_id, "nested_share_url")
                            if nested_shape.post_id:
                                add_pass2_id("post", nested_shape.post_id, "nested_share_url")
                            if nested_shape.reel_id:
                                add_pass2_id("reel", nested_shape.reel_id, "nested_share_url")
                            if nested_shape.video_id:
                                add_pass2_id("video", nested_shape.video_id, "nested_share_url")
                            if nested_shape.photo_id:
                                add_pass2_id("photo", nested_shape.photo_id, "nested_share_url")
                            if nested_shape.album_id:
                                add_pass2_id("album", nested_shape.album_id, "nested_share_url")
                            if nested_shape.numeric_path_id and nested_shape.route_entity == "USER":
                                add_pass2_id("author", nested_shape.numeric_path_id, "nested_story_owner")
                            queue.append(value)
                # The current URL itself may be a story route.
                try:
                    cs = URLParser.parse(current)
                    if cs.numeric_path_id and cs.route_entity == "USER":
                        add_pass2_id("author", cs.numeric_path_id, "story_owner_route")
                    if cs.story_id:
                        add_pass2_id("story", cs.story_id, "story_route")
                    if cs.opaque_token:
                        for decoded_id in decode_facebook_story_token(cs.opaque_token):
                            add_pass2_id("story", decoded_id, "nested_decoded_story_token")
                except Exception:
                    pass

        harvest_nested_public_urls(url)

        # The caller may have supplied a canonical/forwarded URL in the query.
        # Inspect the original raw URL too, before normalize_facebook_url strips
        # tracking parameters such as share_url.
        harvest_nested_public_urls(original_shape.original)

        # Explicit story-owner evidence: /stories/<numeric-owner>/<story-token>/
        # carries a public owner identifier in the route. Treat it as USER
        # identity evidence, while still allowing later page/group evidence to
        # veto it.
        story_owner = getattr(original_shape, "numeric_path_id", "")
        # URL-level fallback: /stories/<numeric-owner>/<opaque-token>/ is an
        # explicit owner route even when URLParser cannot classify route_entity
        # because Facebook changed the route grammar. Never depend on the
        # classifier to recover an ID that is literally present in the URL.
        if not story_owner and original_shape.original:
            _m = re.search(r"/stories/(\d{10,30})/", original_shape.original, re.I)
            if _m:
                story_owner = _m.group(1)
                result.debug.setdefault("pass2", {})["explicit_story_owner_regex"] = story_owner
        if story_owner and is_numeric_id(story_owner):
            collector.add(
                story_owner,
                role="USER_CANDIDATE",
                source="original_story_owner_route",
                key="profile_owner_id",
                neighbor="/stories/<owner_id>/<story_id>",
                url=original_shape.original,
                weight=108.0,
                independent=True,
                entity_type="USER",
            )
            result.debug.setdefault("pass2", {})["explicit_story_owner"] = story_owner

        # PASS 2 ID harvesting is deliberately broader than PASS 1, but still
        # evidence-based.  For a share wrapper, inspect every response's
        # redirect/canonical URL and explicit opaque content IDs such as pfbid.
        # Do NOT mine arbitrary numeric strings from generic landing HTML.
        pass2_source_snapshots = [snapshot] + share_snapshots + phase2_snapshots
        pass2_source_snapshots = [
            ps for ps in pass2_source_snapshots if ps is not None
        ]

        def harvest_explicit_object_ids(ps: PageSnapshot):
            urls = list(ps.redirect_chain)
            for key in ("og:url", "profile:url", "canonical"):
                value = ps.meta.get(key, "")
                if value:
                    urls.append(value)
            for candidate_url in urls:
                if not candidate_url or not looks_like_url(candidate_url):
                    continue
                if not is_facebook_host(urlparse(candidate_url).netloc):
                    continue
                cs = URLParser.parse(candidate_url)
                for kind, attr in (
                    ("post", "post_id"), ("story", "story_id"),
                    ("reel", "reel_id"), ("video", "video_id"),
                    ("photo", "photo_id"), ("album", "album_id"),
                ):
                    value = getattr(cs, attr, "")
                    if value:
                        add_pass2_id(kind, value, "redirect_or_canonical")
            # Opaque IDs are useful precisely because they are not numeric.
            # Restrict this extraction to Facebook's recognizable pfbid form;
            # arbitrary alphanumeric tokens in a landing page are not objects.
            for ident in re.findall(
                r"\bpfbid[A-Za-z0-9_-]{6,299}\b",
                ps.html or "",
                re.I,
            ):
                add_pass2_id("post", ident, "explicit_pfbid_html")
            for ident in re.findall(
                r"\bpfbid[A-Za-z0-9_-]{6,299}\b",
                ps.text or "",
                re.I,
            ):
                add_pass2_id("post", ident, "explicit_pfbid_text")

        for ps in pass2_source_snapshots:
            harvest_explicit_object_ids(ps)

        # A share wrapper itself carries an opaque share token.  It is NOT a
        # content ID and is never fed into the UID scorer as one.  We do,
        # however, record the token in debug so the operator can distinguish
        # "no object exposed" from "object exposed but author hidden".
        if original_shape.wrapper and original_shape.opaque_token:
            result.debug.setdefault("pass2", {})["share_token"] = original_shape.opaque_token

        # PASS 2 harvesting: inspect ALL public PASS-1 responses. Redirect and
        # canonical URLs are explicit object evidence; pfbid is the only
        # alphanumeric object form harvested from HTML. Arbitrary numbers or
        # random tokens in generic landing pages are never treated as objects.
        pass2_source_snapshots = [snapshot] + share_snapshots + phase2_snapshots
        pass2_source_snapshots = [ps for ps in pass2_source_snapshots if ps is not None]

        def harvest_explicit_object_ids(ps: PageSnapshot):
            explicit_urls = list(ps.redirect_chain)
            for key in ("og:url", "profile:url", "canonical"):
                value = ps.meta.get(key, "")
                if value:
                    explicit_urls.append(value)

            for candidate_url in explicit_urls:
                if not candidate_url or not is_facebook_host(urlparse(candidate_url).netloc):
                    continue
                cs = URLParser.parse(candidate_url)
                for kind, attr in (
                    ("post", "post_id"), ("story", "story_id"),
                    ("reel", "reel_id"), ("video", "video_id"),
                    ("photo", "photo_id"), ("album", "album_id"),
                ):
                    value = getattr(cs, attr, "")
                    if value:
                        add_pass2_id(kind, value, "redirect_or_canonical")

            # pfbid is an explicit Facebook object identifier form. Unlike a
            # random alphanumeric string, it is safe to feed into object probes.
            for ident in re.findall(
                r"\bpfbid[A-Za-z0-9_-]{6,299}\b",
                ps.html or "",
                re.I,
            ):
                add_pass2_id("post", ident, "explicit_pfbid_html")

            for ident in re.findall(
                r"\bpfbid[A-Za-z0-9_-]{6,299}\b",
                ps.text or "",
                re.I,
            ):
                add_pass2_id("post", ident, "explicit_pfbid_text")

        for ps in pass2_source_snapshots:
            harvest_explicit_object_ids(ps)

        if original_shape.wrapper and original_shape.opaque_token:
            result.debug.setdefault("pass2", {})["share_token"] = original_shape.opaque_token

        # Highest-confidence IDs: URL route first.
        for kind, attr in (
            ("post", "post_id"), ("story", "story_id"),
            ("reel", "reel_id"), ("video", "video_id"),
            ("photo", "photo_id"), ("album", "album_id"),
        ):
            value = getattr(shape, attr, "")
            if value:
                add_pass2_id(kind, value, "url_route")

        # Then only object IDs actually classified by the evidence engine.
        # Generic raw numeric strings are intentionally excluded.
        for ev in collector.items:
            if ev.role not in {"OBJECT_ID", "POST_ID", "STORY_ID", "VIDEO_ID", "REEL_ID", "PHOTO_ID", "ALBUM_ID"}:
                continue
            if not is_content_id(ev.value):
                continue
            key = normalize_key(ev.key)
            if "story" in key:
                kind = "story"
            elif "reel" in key:
                kind = "reel"
            elif "video" in key:
                kind = "video"
            elif "photo" in key or "media" in key:
                kind = "photo"
            elif "album" in key:
                kind = "album"
            else:
                kind = "post"
            add_pass2_id(kind, ev.value, ev.source)

        # Content IDs can be present in explicit canonical metadata. Parse the
        # URL itself, but never mine arbitrary numbers from generic HTML.
        explicit_urls = [
            snapshot.final_url,
            snapshot.meta.get("og:url", ""),
            snapshot.meta.get("profile:url", ""),
        ]
        for candidate_url in explicit_urls:
            if not candidate_url or not is_facebook_host(urlparse(candidate_url).netloc):
                continue
            cs = URLParser.parse(candidate_url)
            for kind, attr in (
                ("post", "post_id"), ("story", "story_id"),
                ("reel", "reel_id"), ("video", "video_id"),
                ("photo", "photo_id"), ("album", "album_id"),
            ):
                value = getattr(cs, attr, "")
                if value:
                    add_pass2_id(kind, value, "explicit_metadata_url")

        if pass2_ids:
            result.notes.append(
                f"PASS 2: found {len(pass2_ids)} explicit content-ID candidate(s)."
            )
            for kind, ident, source in pass2_ids[:MAX_DEBUG_CANDIDATES]:
                result.notes.append(
                    f"PASS2 candidate: {kind}={truncate(ident, 80)} source={source}"
                )

            # Probe representations using the observed shape, plus a shape
            # cloned from each explicit ID where appropriate.
            probe_urls = self.build_object_probe_urls(
                shape, result.content_url or snapshot.final_url or url
            )
            # Direct owner/profile probing for IDs explicitly present in
            # /stories/<numeric-owner>/<story-token>/ URLs.
            author_ids = [i for k, i, _ in pass2_ids if k == "author" and is_numeric_id(i)]
            for author_id in unique_keep_order(author_ids):
                for profile_url in (
                    f"https://www.facebook.com/profile.php?id={author_id}",
                    f"https://m.facebook.com/profile.php?id={author_id}",
                ):
                    try:
                        ps = self.fetcher.fetch(profile_url, referer=result.content_url or url)
                    except Exception:
                        continue
                    phase2_snapshots.append(ps)
                    self.merge_snapshot_evidence(ps, collector)

            for kind, ident, source in pass2_ids:
                temp = URLShape(
                    original=result.content_url or url,
                    normalized=result.content_url or url,
                    host="www.facebook.com",
                    path="/",
                    kind=kind.upper(),
                    username=shape.username,
                    route_entity=shape.route_entity,
                )
                setattr(temp, {
                    "post":"post_id", "story":"story_id", "reel":"reel_id",
                    "video":"video_id", "photo":"photo_id", "album":"album_id",
                }[kind], ident)
                probe_urls.extend(
                    self.build_object_probe_urls(temp, result.content_url or url)
                )
            probe_urls = unique_keep_order(probe_urls)[:MAX_OBJECT_PROBES]

            for probe_url in probe_urls:
                try:
                    ps = self.fetcher.fetch(
                        probe_url,
                        referer=result.content_url or url,
                    )
                except Exception:
                    LOGGER.debug("PASS2 probe failed: %s", probe_url, exc_info=True)
                    continue
                phase2_snapshots.append(ps)
                self.merge_snapshot_evidence(ps, collector)

            # Re-run classification against ALL evidence, including pass2.
            all_public_snapshots = profile_snapshots + phase2_snapshots
            entity = entity_classifier.classify(
                shape, collector, all_public_snapshots or [snapshot]
            )
            # Reclassify content only after PASS 2 object probes have supplied
            # real object evidence.  This prevents generic share-landings from
            # being labeled STORY/VIDEO merely from incidental links.
            content = content_classifier.classify(
                shape,
                collector,
                phase2_snapshots[-1] if phase2_snapshots else snapshot,
            )
            profile = extract_profile_info(
                shape, snapshot, collector, entity
            )

            # Discover and fetch profiles from every useful pass2 snapshot.
            discovered_profiles: List[str] = []
            for ps in phase2_snapshots:
                discovered_profiles.extend(
                    self.extract_profile_urls(shape, ps, profile, entity)
                )
            discovered_profiles.extend(
                self.extract_profile_urls(shape, snapshot, profile, entity)
            )
            discovered_profiles = unique_keep_order(discovered_profiles)
            existing = {
                normalize_facebook_url(x.url) for x in profile_snapshots if x.url
            }
            for profile_url in discovered_profiles[:MAX_PROFILE_CHECKS]:
                if normalize_facebook_url(profile_url) in existing:
                    continue
                try:
                    ps = self.fetcher.fetch(
                        profile_url, referer=result.content_url or url
                    )
                except Exception:
                    continue
                profile_snapshots.append(ps)
                existing.add(normalize_facebook_url(profile_url))
                self.merge_snapshot_evidence(ps, collector)

            result.debug["pass2"] = {
                "executed": True,
                "content_id_candidates": [
                    {"kind": k, "id": truncate(i, 120), "source": src}
                    for k, i, src in pass2_ids[:MAX_DEBUG_CANDIDATES]
                ],
                "probe_urls": probe_urls[:MAX_DEBUG_CANDIDATES],
                "probe_count": len(phase2_snapshots),
                "profile_count": len(profile_snapshots),
            }
        else:
            result.notes.append(
                "PASS 2: executed nhưng không tìm thấy content ID công khai đủ tin cậy; không thể request object cụ thể."
            )
            result.debug["pass2"] = {
                "executed": True,
                "content_id_candidates": [],
                "probe_urls": [],
                "probe_count": 0,
                "profile_count": len(profile_snapshots),
                "source_snapshot_count": len(pass2_source_snapshots),
                "reason": (
                    "No explicit object ID in input route, nested share_url, "
                    "redirect/canonical URLs, or recognizable pfbid evidence. "
                    "Opaque share tokens are not treated as UID guesses."
                ),
            }

        # Final reclassification and verification always use pass2 snapshots.
        entity = (
            entity_classifier.classify(
                shape,
                collector,
                profile_snapshots
                or [snapshot],
            )
        )
        profile.entity_type = (
            entity.publisher
        )
        verifier = IdentityVerifier()
        verification = (
            verifier.verify(
                shape=shape,
                snapshot=snapshot,
                profile_snapshots=(profile_snapshots + phase2_snapshots),
                collector=collector,
                classification=entity,
                profile=profile,
            )
        )
        result.entity_type = (
            entity.publisher
        )
        result.publisher_type = (
            entity.publisher
        )
        result.name = profile.name
        result.username = (
            profile.username
        )
        result.profile_url = (
            profile.profile_url
        )
        result.avatar_url = (
            profile.avatar_url
        )
        result.bio = profile.bio
        result.content_type = (
            content.content_type
        )
        result.post_id = (
            content.post_id
        )
        result.video_id = (
            content.video_id
        )
        result.reel_id = (
            content.reel_id
        )
        result.photo_id = (
            content.photo_id
        )
        result.story_id = (
            content.story_id
        )
        result.album_id = (
            content.album_id
        )
        result.publisher_id = (
            entity.page_id
            if entity.publisher == "PAGE"
            else (
                entity.group_id
                if entity.publisher == "GROUP"
                else ""
            )
        )
        if (
            entity.publisher == "USER"
            and verification.verified
        ):
            result.uid = (
                verification.uid
            )
        if entity.publisher in {
            "PAGE",
            "GROUP",
        }:
            result.author_uid = (
                entity.author_uid
            )
        result.verified = (
            verification.verified
        )
        result.confidence = (
            verification.confidence
        )
        result.evidence_sources = (
            verification.sources
        )
        # Candidate-chain debug: expose scoring/evidence provenance without
        # exposing cookies, headers, tokens, or private data.
        try:
            ranked_debug = rank_user_candidates(
                collector,
                username=(profile.username or shape.username),
                shape=shape,
            )[:MAX_DEBUG_CANDIDATES]
            result.debug["uid_candidates"] = [
                {"uid": uid, "base_score": round(float(score), 2)}
                for uid, score in ranked_debug
            ]
            result.debug.setdefault("candidate_chain", {})["story_owner_from_input"] = getattr(original_shape, "numeric_path_id", "")
            result.debug.setdefault("candidate_chain", {})["nested_share_urls"] = [
                v for v in original_shape.query.get("share_url", [])[:10]
            ]
            result.debug["pass1"] = {
                "input": url,
                "initial_kind": original_shape.kind,
                "canonical": result.canonical_url,
                "content": result.content_url,
                "publisher": entity.publisher,
                "verification_confidence": round(float(verification.confidence), 2),
            }
        except Exception:
            LOGGER.debug("debug scoring failed", exc_info=True)
        result.signals = (
            unique_keep_order(
                content.signals
                + entity.signals
                + verification.signals
            )[:MAX_SIGNALS]
        )
        # Recover content IDs from HTML only as a content fallback; never
        # promote these values to USER UID candidates.
        if result.content_type in {
            "POST", "GROUP_POST", "REEL", "VIDEO", "PHOTO", "STORY", "ALBUM"
        }:
            if result.content_type in {"POST", "GROUP_POST"} and not result.post_id:
                ids = extract_content_identifiers(snapshot.html)
                if ids:
                    result.post_id = ids[0]
                    result.signals.append("content ID recovered from HTML")
            elif result.content_type == "REEL" and not result.reel_id:
                ids = extract_content_identifiers(snapshot.html)
                if ids:
                    result.reel_id = ids[0]
                    result.signals.append("reel ID recovered from HTML")
        result.title = (
            self.extract_content_title(
                snapshot,
                collector,
                profile,
                content,
            )
        )
        if result.verified:
            result.status = "VERIFIED"
        elif (
            snapshot.limited
            or snapshot.error
        ):
            result.status = (
                "FETCH_LIMITED"
            )
        else:
            result.status = (
                "NOT_VERIFIED"
            )
        if not result.verified:
            result.notes.append(
                verification.reason
                or "UID withheld."
            )
        if generic_name(
            result.name
        ):
            result.name = ""
        if (
            result.publisher_type
            == "GROUP"
        ):
            result.group_name = (
                result.name
            )
        elif (
            result.publisher_type
            == "PAGE"
        ):
            result.page_name = (
                result.name
            )
        return result
    def build_object_probe_urls(self, shape: URLShape, base_url: str) -> List[str]:
        """Build public HTTP representations from an already observed content ID.

        This is a bounded second-pass resolver.  It never invents a UID from an
        object ID; it simply asks Facebook's public HTML endpoints whether the
        object exposes an author/profile identity.
        """
        host = "www.facebook.com"
        username = clean_text(shape.username).lstrip("@")
        urls: List[str] = []
        def add(u: str):
            if u and is_facebook_host(urlparse(u).netloc):
                urls.append(normalize_facebook_url(u))

        ids = []
        if shape.post_id: ids.append(("post", shape.post_id))
        if shape.story_id: ids.append(("story", shape.story_id))
        if shape.reel_id: ids.append(("reel", shape.reel_id))
        if shape.video_id: ids.append(("video", shape.video_id))
        if shape.photo_id: ids.append(("photo", shape.photo_id))
        if shape.album_id: ids.append(("album", shape.album_id))

        for kind, ident in ids:
            ident = clean_text(ident)
            if not is_content_id(ident):
                continue
            if kind == "post":
                if username:
                    add(f"https://{host}/{quote(username, safe='@.*-')}/posts/{quote(ident, safe='')}")
                add(f"https://{host}/{quote(ident, safe='')}")
                add(f"https://{host}/permalink.php?story_fbid={quote(ident, safe='')}")
            elif kind == "story":
                add(f"https://{host}/story.php?story_fbid={quote(ident, safe='')}")
                if username:
                    add(f"https://{host}/{quote(username, safe='@.*-')}/posts/{quote(ident, safe='')}")
            elif kind == "reel":
                add(f"https://{host}/reel/{quote(ident, safe='')}")
                add(f"https://{host}/{quote(ident, safe='')}")
            elif kind == "video":
                add(f"https://{host}/watch/?v={quote(ident, safe='')}")
                add(f"https://{host}/videos/{quote(ident, safe='')}")
                add(f"https://{host}/{quote(ident, safe='')}")
            elif kind == "photo":
                add(f"https://{host}/photo/?fbid={quote(ident, safe='')}")
                add(f"https://{host}/{quote(ident, safe='')}")
            elif kind == "album":
                add(f"https://{host}/media/set/?set={quote(ident, safe='')}")
                add(f"https://{host}/{quote(ident, safe='')}")

        # A bare numeric/opaque object identifier can sometimes resolve to a
        # public object/profile page.  It is deliberately fetched as an object,
        # never interpreted as a USER UID without explicit user evidence.
        for ident in extract_content_identifiers(base_url):
            if is_content_id(ident):
                add(f"https://{host}/{quote(ident, safe='')}")
        # Repeat the same object representations on mobile/public hosts.
        # This is still ordinary public HTTP and gives Facebook different
        # rendering paths that may expose metadata absent from www.
        base_urls = list(urls)
        for u in base_urls:
            pu = urlparse(u)
            if pu.netloc == "www.facebook.com":
                for host2 in ("m.facebook.com", "mbasic.facebook.com"):
                    urls.append(urlunparse((
                        pu.scheme, host2, pu.path, pu.params, pu.query, pu.fragment
                    )))
        return unique_keep_order(urls)[:MAX_OBJECT_PROBES]

    @staticmethod
    def merge_snapshot_evidence(snapshot: PageSnapshot, collector: EvidenceCollector):
        scan_meta(snapshot, collector)
        scan_jsonld(snapshot, collector)
        scan_scripts(snapshot, collector)
        scan_html_identity(snapshot, collector)
        scan_html_profile_links(snapshot, collector)

    def extract_profile_urls(
        self,
        shape: URLShape,
        snapshot: PageSnapshot,
        profile: ProfileInfo,
        entity: EntityClassification,
    ) -> List[str]:
        candidates = []
        if profile.profile_url:
            candidates.append(
                profile.profile_url
            )
        if shape.username:
            candidates.append(
                "https://www.facebook.com/"
                + quote(
                    shape.username,
                    safe="@.*-",
                )
            )
        for key in (
            "og:url",
            "profile:url",
        ):
            value = snapshot.meta.get(
                key,
                "",
            )
            if (
                value
                and is_facebook_host(
                    urlparse(
                        value
                    ).netloc
                )
            ):
                parsed = URLParser.parse(
                    value
                )
                if (
                    parsed.kind
                    == "PROFILE"
                    and parsed.username
                ):
                    candidates.append(
                        normalize_facebook_url(
                            value
                        )
                    )
        for href in snapshot.links:
            href = urljoin(
                snapshot.final_url
                or snapshot.url,
                href,
            )
            if not is_facebook_host(
                urlparse(
                    href
                ).netloc
            ):
                continue
            parsed = URLParser.parse(
                href
            )
            if (
                parsed.kind
                == "PROFILE"
                and parsed.username
            ):
                candidates.append(
                    normalize_facebook_url(
                        href
                    )
                )
        return unique_keep_order(
            candidates
        )
    @staticmethod
    def extract_content_title(
        snapshot: PageSnapshot,
        collector: EvidenceCollector,
        profile: ProfileInfo,
        content: ContentClassification,
    ) -> str:
        titles = collector.values_for(
            "TITLE"
        )
        for title in titles:
            title = clean_title(
                title
            )
            if (
                title
                and not generic_name(title)
            ):
                return truncate(
                    title,
                    500,
                )
        title = clean_title(
            snapshot.meta.get(
                "og:title",
                "",
            )
        )
        if title:
            return truncate(
                title,
                500,
            )
        return ""
ENTITY_LABELS = {
    "USER": "👤 Cá nhân",
    "PAGE": "📄 Trang",
    "GROUP": "👥 Nhóm",
    "EVENT": "📅 Sự kiện",
    "UNKNOWN": "❔ Chưa xác định",
}
CONTENT_LABELS = {
    "PROFILE": "👤 PROFILE",
    "POST": "📝 POST",
    "GROUP_POST": "👥 GROUP POST",
    "REEL": "🎬 REEL",
    "VIDEO": "🎥 VIDEO",
    "PHOTO": "📷 PHOTO",
    "STORY": "⭕ STORY",
    "ALBUM": "🖼 ALBUM",
    "CONTENT": "📦 CONTENT",
    "UNKNOWN": "❔ UNKNOWN",
}
def safe_href(
    url: str,
) -> str:
    if not url:
        return ""
    return html_lib.escape(
        url,
        quote=True,
    )
def link(
    url: str,
    label: str,
) -> str:
    if not url:
        return ""
    return (
        f'<a href="{safe_href(url)}">'
        f"{tg_escape(label)}"
        f"</a>"
    )
def entity_label(
    value: str,
) -> str:
    return ENTITY_LABELS.get(
        value,
        "❔ Chưa xác định",
    )
def content_label(
    value: str,
) -> str:
    return CONTENT_LABELS.get(
        value,
        value or "❔ UNKNOWN",
    )
def format_id(
    label: str,
    value: str,
) -> str:
    if not value:
        return ""
    return (
        f"│ {tg_escape(label):<10}: "
        f"<code>{tg_escape(value)}</code>"
    )
def format_result(
    index: int,
    result: ResolveResult,
) -> str:
    lines = []
    lines.append(
        "╭──────────────────────────"
    )
    lines.append(
        f"│ 🔎 <b>FACEBOOK RESOLVER #{index}</b>"
    )
    lines.append(
        "├──────────────────────────"
    )
    publisher = result.publisher_type
    if publisher == "USER":
        lines.append(
            "│ 👤 <b>NGƯỜI ĐĂNG</b>"
        )
        if result.name:
            lines.append(
                "│ Tên       : "
                + tg_escape(
                    result.name
                )
            )
        if result.username:
            lines.append(
                "│ Username  : @"
                + tg_escape(
                    result.username.lstrip("@")
                )
            )
        if result.uid:
            lines.append(
                "│ UID       : "
                f"<code>{tg_escape(result.uid)}</code>"
            )
        else:
            lines.append(
                "│ UID       : "
                "⚠️ Chưa xác minh công khai"
            )
        lines.append(
            "│ Loại      : "
            + entity_label(publisher)
        )
        if result.bio:
            lines.append(
                "│ Bio       : "
                + tg_escape(
                    truncate(
                        result.bio,
                        350,
                    )
                )
            )
        if result.avatar_url:
            lines.append(
                "│ Avatar    : "
                + link(
                    result.avatar_url,
                    "🖼 Mở ảnh",
                )
            )
    elif publisher == "PAGE":
        lines.append(
            "│ 📄 <b>TRANG FACEBOOK</b>"
        )
        if result.name:
            lines.append(
                "│ Tên trang : "
                + tg_escape(
                    result.name
                )
            )
        if result.username:
            lines.append(
                "│ Username  : @"
                + tg_escape(
                    result.username.lstrip("@")
                )
            )
        if result.publisher_id:
            lines.append(
                "│ Page ID   : "
                f"<code>{tg_escape(result.publisher_id)}</code>"
            )
        if result.author_uid:
            lines.append(
                "│ Tác giả   : "
                f"<code>{tg_escape(result.author_uid)}</code>"
            )
        lines.append(
            "│ Loại      : "
            + entity_label(publisher)
        )
        if result.bio:
            lines.append(
                "│ Mô tả     : "
                + tg_escape(
                    truncate(
                        result.bio,
                        350,
                    )
                )
            )
    elif publisher == "GROUP":
        lines.append(
            "│ 👥 <b>NHÓM FACEBOOK</b>"
        )
        if result.name:
            lines.append(
                "│ Tên nhóm  : "
                + tg_escape(
                    result.name
                )
            )
        if result.publisher_id:
            lines.append(
                "│ Group ID  : "
                f"<code>{tg_escape(result.publisher_id)}</code>"
            )
        if result.author_uid:
            lines.append(
                "│ Tác giả   : "
                f"<code>{tg_escape(result.author_uid)}</code>"
            )
        lines.append(
            "│ Loại      : "
            + entity_label(publisher)
        )
    else:
        lines.append(
            "│ 👤 <b>CHỦ THỂ</b>"
        )
        if result.name:
            lines.append(
                "│ Tên       : "
                + tg_escape(
                    result.name
                )
            )
        if result.author_uid:
            lines.append(
                "│ Author UID: "
                f"<code>{tg_escape(result.author_uid)}</code>"
            )
        lines.append(
            "│ Loại      : "
            + entity_label(publisher)
        )
    if result.content_type != "PROFILE":
        lines.append("│")
        lines.append(
            "│ 📦 <b>NỘI DUNG</b>"
        )
        lines.append(
            "│ Type      : "
            + content_label(
                result.content_type
            )
        )
        if result.post_id:
            lines.append(
                "│ Post ID   : "
                f"<code>{tg_escape(result.post_id)}</code>"
            )
        elif result.content_type in {"POST", "GROUP_POST"}:
            lines.append(
                "│ Post ID   : ⚠️ Chưa tìm thấy public"
            )
        if result.reel_id:
            lines.append(
                "│ Reel ID   : "
                f"<code>{tg_escape(result.reel_id)}</code>"
            )
        if result.video_id:
            lines.append(
                "│ Video ID  : "
                f"<code>{tg_escape(result.video_id)}</code>"
            )
        if result.photo_id:
            lines.append(
                "│ Photo ID  : "
                f"<code>{tg_escape(result.photo_id)}</code>"
            )
        if result.story_id:
            lines.append(
                "│ Story ID  : "
                f"<code>{tg_escape(result.story_id)}</code>"
            )
        if result.album_id:
            lines.append(
                "│ Album ID  : "
                f"<code>{tg_escape(result.album_id)}</code>"
            )
        if result.title:
            lines.append(
                "│ Tiêu đề   : "
                + tg_escape(
                    truncate(
                        result.title,
                        450,
                    )
                )
            )
    if publisher == "GROUP":
        lines.append("│")
        lines.append(
            "│ 👥 <b>NGUỒN ĐĂNG</b>"
        )
        lines.append(
            "│ Loại      : 👥 GROUP"
        )
        if result.publisher_id:
            lines.append(
                "│ Group ID  : "
                f"<code>{tg_escape(result.publisher_id)}</code>"
            )
        if result.author_uid:
            lines.append(
                "│ Người đăng: "
                f"<code>{tg_escape(result.author_uid)}</code>"
            )
    elif publisher == "PAGE":
        lines.append("│")
        lines.append(
            "│ 📄 <b>NGUỒN ĐĂNG</b>"
        )
        lines.append(
            "│ Loại      : 📄 PAGE"
        )
        if result.publisher_id:
            lines.append(
                "│ Page ID   : "
                f"<code>{tg_escape(result.publisher_id)}</code>"
            )
        if result.author_uid:
            lines.append(
                "│ Người đăng: "
                f"<code>{tg_escape(result.author_uid)}</code>"
            )
    lines.append("│")
    lines.append(
        "│ 🔗 <b>LIÊN KẾT</b>"
    )
    if result.profile_url:
        label = (
            "Mở trang"
            if publisher not in {
                "PAGE",
                "GROUP",
            }
            else (
                "Mở Page"
                if publisher == "PAGE"
                else "Mở Group"
            )
        )
        lines.append(
            "│ Profile   : "
            + link(
                result.profile_url,
                label,
            )
        )
    if (
        result.content_type != "PROFILE"
        and result.content_url
    ):
        lines.append(
            "│ Content   : "
            + link(
                result.content_url,
                "Mở nội dung",
            )
        )
    lines.append("│")
    lines.append(
        "│ 🔬 <b>DẤU HIỆU</b>"
    )
    signals = (
        result.signals
        or [
            "Không có tín hiệu đủ mạnh."
        ]
    )
    for signal in signals[
        :MAX_SIGNALS
    ]:
        lines.append(
            "│ • "
            + tg_escape(signal)
        )
    lines.append("│")
    lines.append(
        "│ 🛡 <b>XÁC MINH</b>"
    )
    if result.verified:
        lines.append(
            "│ Trạng thái: "
            "✅ <b>VERIFIED</b>"
        )
        lines.append(
            "│ Độ tin cậy: "
            f"<b>{result.confidence:.1f}%</b>"
        )
        lines.append(
            "│ Nguồn     : "
            f"{result.evidence_sources} nguồn"
        )
    else:
        lines.append(
            "│ Trạng thái: "
            "⚠️ <b>"
            + tg_escape(
                result.status
            )
            + "</b>"
        )
        lines.append(
            "│ UID       : "
            "🔒 Đã ẩn vì chưa đủ bằng chứng"
        )
        if result.notes:
            lines.append(
                "│ Lý do     : "
                + tg_escape(
                    truncate(
                        result.notes[0],
                        450,
                    )
                )
            )
    lines.append(
        "╰──────────────────────────"
    )
    lines.append(
        f"⏱ {result.elapsed:.2f}s"
    )
    return "\n".join(lines)
_resolver = FacebookResolver()
_pending_lock = asyncio.Lock()
_pending_users: Set[
    Tuple[int, int]
] = set()
_pending_sessions = {}
async def wait_for_next_facebook_url(
    bot,
    event,
    timeout: int = SESSION_TIMEOUT,
):
    """
    Telethon-compatible replacement for wait_for().
    Không dùng:
    bot.wait_for()
    Vì TelegramClient của Telethon
    không có API này.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    chat_id = event.chat_id
    sender_id = event.sender_id
    session_key = (
        int(chat_id or 0),
        int(sender_id or 0),
    )
    async def callback(new_event):
        try:
            if new_event.chat_id != chat_id:
                return
            if new_event.sender_id != sender_id:
                return
            text = (
                new_event.raw_text
                or ""
            ).strip()
            if not text:
                return
            urls = extract_urls(text)
            if not urls:
                return
            if not future.done():
                future.set_result(
                    urls
                )
        except Exception:
            LOGGER.debug(
                "follow-up callback error: %r",
                new_event,
                exc_info=True,
            )
    handler = events.NewMessage()
    bot.add_event_handler(
        callback,
        handler,
    )
    try:
        return await asyncio.wait_for(
            future,
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return []
    finally:
        try:
            bot.remove_event_handler(
                callback,
                handler,
            )
        except Exception:
            LOGGER.debug(
                "Unable to remove temporary "
                "Telethon handler",
                exc_info=True,
            )
def get_command_args(
    text: str,
) -> str:
    if not text:
        return ""
    m = re.match(
        r"^/getuidfb(?:@\w+)?(?:\s+(.*))?$",
        text.strip(),
        flags=re.I | re.S,
    )
    if not m:
        return ""
    return (
        m.group(1)
        or ""
    ).strip()
async def resolve_async(
    url: str,
) -> ResolveResult:
    return await asyncio.to_thread(
        _resolver.resolve,
        url,
    )
async def resolve_many(
    urls: List[str],
) -> List[ResolveResult]:
    semaphore = asyncio.Semaphore(
        CONCURRENCY
    )
    async def worker(
        url: str,
    ) -> ResolveResult:
        async with semaphore:
            try:
                return await resolve_async(
                    url
                )
            except Exception:
                LOGGER.exception(
                    "Resolver worker failed: %s",
                    url,
                )
                return ResolveResult(
                    input_url=url,
                    canonical_url=url,
                    content_url=url,
                    status="FETCH_LIMITED",
                    notes=[
                        "Không thể hoàn tất "
                        "kiểm tra URL."
                    ],
                )
    tasks = [
        asyncio.create_task(
            worker(url)
        )
        for url in urls
    ]
    return await asyncio.gather(
        *tasks
    )
def register(
    bot,
    notify_bot=None,
):
    """
    Telethon command registration.
    REQUIRED BY:
    commands/__init__.py
    Example:
        module.register(bot, notify_bot)
    """
    @bot.on(
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?(?:\s+.*)?$"
        )
    )
    async def getuidfb_handler(
        event,
    ):
        started = time.perf_counter()
        sender_id = (
            event.sender_id
            or 0
        )
        chat_id = (
            event.chat_id
            or sender_id
        )
        session_key = (
            int(chat_id),
            int(sender_id),
        )
        try:
            args = get_command_args(
                event.raw_text
            )
            if args:
                urls = extract_urls(
                    args
                )
            else:
                async with _pending_lock:
                    if (
                        session_key
                        in _pending_sessions
                    ):
                        return
                    _pending_sessions[
                        session_key
                    ] = time.time()
                prompt = None
                try:
                    prompt = await event.reply(
                        (
                            "🔎 <b>"
                            "FACEBOOK FORENSIC "
                            "RESOLVER V60"
                            "</b>\n\n"
                            "📩 Hãy gửi link "
                            "Facebook cần kiểm tra."
                            "\n\n"
                            "🔬 Resolver sẽ phân tích:"
                            "\n"
                            "• Redirect / canonical URL"
                            "\n"
                            "• Profile / Page / Group"
                            "\n"
                            "• POST / REEL / VIDEO / "
                            "PHOTO / STORY"
                            "\n"
                            "• HTML / Meta / JSON / "
                            "JSON-LD"
                            "\n"
                            "• UID / Page ID / Group ID"
                            "\n"
                            "• Correlation và conflict"
                            "\n\n"
                            "🛡 UID chỉ hiển thị khi "
                            "đủ bằng chứng."
                        ),
                        parse_mode="html",
                    )
                    urls = (
                        await wait_for_next_facebook_url(
                            bot,
                            event,
                            SESSION_TIMEOUT,
                        )
                    )
                    if not urls:
                        if prompt:
                            try:
                                await prompt.edit(
                                    (
                                        "⌛ <b>"
                                        "Hết thời gian chờ."
                                        "</b>\n\n"
                                        "Dùng lại:\n"
                                        "<code>/getuidfb</code>"
                                    ),
                                    parse_mode="html",
                                )
                            except Exception:
                                pass
                        return
                finally:
                    async with _pending_lock:
                        _pending_sessions.pop(
                            session_key,
                            None,
                        )
            if not urls:
                await event.reply(
                    (
                        "⚠️ <b>"
                        "Không tìm thấy link "
                        "Facebook hợp lệ."
                        "</b>\n\n"
                        "Ví dụ:\n"
                        "<code>"
                        "/getuidfb "
                        "https://www.facebook.com/..."
                        "</code>"
                    ),
                    parse_mode="html",
                )
                return
            urls = unique_keep_order(
                [
                    normalize_facebook_url(x)
                    for x in urls
                    if x
                ]
            )
            skipped = 0
            if len(urls) > MAX_INPUT_URLS:
                skipped = (
                    len(urls)
                    - MAX_INPUT_URLS
                )
                urls = urls[
                    :MAX_INPUT_URLS
                ]
            processing = await event.reply(
                (
                    "🔬 <b>"
                    "ĐANG PHÂN TÍCH FACEBOOK"
                    "</b>\n\n"
                    f"🔗 URL: <b>{len(urls)}</b>\n"
                    "↪️ Redirect / canonical\n"
                    "🧩 Route classification\n"
                    "📄 HTML / Meta\n"
                    "🧠 JSON / JSON-LD\n"
                    "🎯 Identity correlation\n"
                    "🛡 Strict UID verification"
                ),
                parse_mode="html",
            )
            results = await resolve_many(
                urls
            )
            blocks = []
            for index, result in enumerate(
                results,
                start=1,
            ):
                try:
                    block = format_result(
                        index,
                        result,
                    )
                except Exception:
                    LOGGER.exception(
                        "format_result failed"
                    )
                    block = (
                        "╭──────────────────────────\n"
                        f"│ 🔎 <b>"
                        f"FACEBOOK RESOLVER #{index}"
                        f"</b>\n"
                        "├──────────────────────────\n"
                        "│ ❌ Không thể định dạng kết quả.\n"
                        "│ UID đã được bảo vệ, không suy đoán.\n"
                        "╰──────────────────────────"
                    )
                blocks.append(block)
            verified_count = sum(
                1
                for result in results
                if result.verified
            )
            header = (
                "🔎 <b>"
                "FACEBOOK FORENSIC RESULT"
                "</b>\n"
                f"📊 Đã kiểm tra: "
                f"<b>{len(results)}</b>\n"
                f"✅ Verified: "
                f"<b>{verified_count}</b>\n"
                f"⏱ Tổng thời gian: "
                f"<b>"
                f"{time.perf_counter() - started:.2f}"
                f"s</b>"
            )
            if skipped:
                header += (
                    "\n⚠️ Bỏ qua: "
                    f"<b>{skipped}</b> "
                    "URL vượt giới hạn."
                )
            final_text = (
                header
                + "\n\n"
                + "\n\n".join(blocks)
            )
            if len(final_text) <= 3900:
                await processing.edit(
                    final_text,
                    parse_mode="html",
                )
            else:
                chunks = []
                current = header
                for block in blocks:
                    candidate = (
                        current
                        + "\n\n"
                        + block
                    )
                    if len(candidate) > 3900:
                        if current.strip():
                            chunks.append(
                                current
                            )
                        current = block
                    else:
                        current = candidate
                if current.strip():
                    chunks.append(
                        current
                    )
                if chunks:
                    await processing.edit(
                        chunks[0],
                        parse_mode="html",
                    )
                    for chunk in chunks[1:]:
                        await event.respond(
                            chunk,
                            parse_mode="html",
                        )
            if notify_bot:
                try:
                    await notify_bot(
                        event,
                        (
                            "getuidfb | "
                            f"{len(results)} URL | "
                            f"{verified_count} verified"
                        ),
                    )
                except Exception:
                    LOGGER.debug(
                        "notify_bot failed",
                        exc_info=True,
                    )
        except Exception as exc:
            LOGGER.exception(
                "GETUIDFB HANDLER ERROR"
            )
            async with _pending_lock:
                _pending_sessions.pop(
                    session_key,
                    None,
                )
            try:
                await event.reply(
                    (
                        "❌ <b>"
                        "FACEBOOK RESOLVER"
                        "</b>\n\n"
                        "Đã xảy ra lỗi khi xử lý "
                        "yêu cầu.\n\n"
                        "🛡 Resolver không suy đoán UID "
                        "khi dữ liệu chưa đủ.\n\n"
                        "<code>"
                        + tg_escape(
                            truncate(
                                str(exc),
                                400,
                            )
                        )
                        + "</code>"
                    ),
                    parse_mode="html",
                )
            except Exception:
                LOGGER.exception(
                    "Unable to send "
                    "GETUIDFB error"
                )
COMMAND_INFO = {
    "command": "getuidfb",
    "description": (
        "Facebook public URL forensic resolver"
    ),
    "usage": (
        "/getuidfb <facebook_url>"
    ),
    "category": "Facebook",
    "public_only": True,
}

if __name__ == "__main__":
    tests = [
        # =========================
        # USER / PROFILE
        # =========================

        "https://www.facebook.com/kim.chi.125900/",
        "https://www.facebook.com/kim.chi.125900",
        "https://www.facebook.com/profile.php?id=61553239356646",
        "https://www.facebook.com/people/Nguyen-Van-Loi/61553239356646/",
        "https://www.facebook.com/people/kim-chi/100012345678901/",

        # =========================
        # USER POSTS
        # =========================

        "https://www.facebook.com/kim.chi.125900/posts/123456789/",
        "https://www.facebook.com/kim.chi.125900/posts/123456789",
        "https://www.facebook.com/kim.chi.125900/posts/pfbid0AbCdEfGhIjKlMnOpQrStUvWxYz/",
        "https://www.facebook.com/kim.chi.125900/posts/pfbid02ABCDEF123456789/",

        # =========================
        # REELS
        # =========================

        "https://www.facebook.com/kim.chi.125900/reel/123456789/",
        "https://www.facebook.com/kim.chi.125900/reels/123456789/",
        "https://www.facebook.com/reel/123456789/",
        "https://www.facebook.com/reel/pfbid02ABCDEF123456789/",
        "https://www.facebook.com/kim.chi.125900/videos/123456789/",

        # =========================
        # VIDEO
        # =========================

        "https://www.facebook.com/kim.chi.125900/videos/123456789/",
        "https://www.facebook.com/watch/?v=123456789",
        "https://www.facebook.com/watch?v=123456789",
        "https://www.facebook.com/video.php?v=123456789",
        "https://www.facebook.com/video.php?id=123456789",

        # =========================
        # PHOTO
        # =========================

        "https://www.facebook.com/photo.php?fbid=123456789",
        "https://www.facebook.com/photo.php?fbid=123456789&id=61553239356646",
        "https://www.facebook.com/kim.chi.125900/photos/a.123456789/987654321/",
        "https://www.facebook.com/photo/?fbid=123456789",

        # =========================
        # GROUP
        # =========================

        "https://www.facebook.com/groups/123456789/",
        "https://www.facebook.com/groups/123456789/posts/987654321/",
        "https://www.facebook.com/groups/123456789/posts/pfbid02ABCDEF123456789/",
        "https://www.facebook.com/groups/testgroup/posts/987654321/",
        "https://www.facebook.com/groups/testgroup/",
        "https://www.facebook.com/groups/123456789/permalink/987654321/",

        # =========================
        # PAGE
        # =========================

        "https://www.facebook.com/testpage/",
        "https://www.facebook.com/testpage/posts/123456789/",
        "https://www.facebook.com/testpage/videos/123456789/",
        "https://www.facebook.com/testpage/reels/123456789/",
        "https://www.facebook.com/pages/Test-Page/123456789/",
        "https://www.facebook.com/pages/category/test/Test-Page-123456789/",

        # =========================
        # STORY
        # =========================

        "https://www.facebook.com/stories/122099490590156326/UzpfSVNDOjEwODI2OTkxNDA5MzYwNjI=",
        "https://www.facebook.com/stories/123456789/",
        "https://www.facebook.com/stories/123456789/ABCDEF123456/",
        "https://www.facebook.com/story.php?story_fbid=123456789&id=61553239356646",
        "https://www.facebook.com/story.php?story_fbid=123456789&id=123456789",

        # =========================
        # SHARE
        # =========================

        "https://www.facebook.com/share/r/1H1EjsEW7J/",
        "https://www.facebook.com/share/p/1CCtt6rTp4/",
        "https://www.facebook.com/share/v/1AbCdEfGhI/",
        "https://www.facebook.com/share/s/1AbCdEfGhI/",
        "https://www.facebook.com/share/19M9rhA5SB/",
        "https://www.facebook.com/share/1AbCdEfGhI/",

        # =========================
        # PERMALINK
        # =========================

        "https://www.facebook.com/permalink.php?story_fbid=123456789&id=61553239356646",
        "https://www.facebook.com/permalink.php?story_fbid=123456789&id=123456789",
        "https://www.facebook.com/permalink/123456789/",

        # =========================
        # USERNAME + CONTENT
        # =========================

        "https://www.facebook.com/kim.chi.125900/posts/123456789?__cft__[0]=abc",
        "https://www.facebook.com/kim.chi.125900/reel/123456789?mibextid=abc",
        "https://www.facebook.com/kim.chi.125900/videos/123456789?mibextid=abc",

        # =========================
        # QUERY PARAMETER VARIANTS
        # =========================

        "https://www.facebook.com/profile.php? id=61553239356646".replace(" ", ""),
        "https://www.facebook.com/photo.php?id=61553239356646&fbid=123456789",
        "https://www.facebook.com/video.php?id=61553239356646&v=123456789",

        # =========================
        # MOBILE / M.FACEBOOK
        # =========================

        "https://m.facebook.com/kim.chi.125900/",
        "https://m.facebook.com/kim.chi.125900/posts/123456789/",
        "https://m.facebook.com/profile.php?id=61553239356646",
        "https://m.facebook.com/groups/123456789/posts/987654321/",

        # =========================
        # WWW / FB VARIANTS
        # =========================

        "https://facebook.com/kim.chi.125900/",
        "https://facebook.com/kim.chi.125900/posts/123456789/",
        "https://facebook.com/groups/123456789/posts/987654321/",
    ]
    for test in tests:
        shape = URLParser.parse(
            test
        )
        print()
        print("=" * 70)
        print(test)
        print("kind           :", shape.kind)
        print("username       :", shape.username)
        print("numeric_id     :", shape.numeric_path_id)
        print("post_id        :", shape.post_id)
        print("reel_id        :", shape.reel_id)
        print("video_id       :", shape.video_id)
        print("photo_id       :", shape.photo_id)
        print("story_id       :", shape.story_id)
        print("group_id       :", shape.group_id)
        print("route_entity   :", shape.route_entity)
        print("wrapper        :", shape.wrapper)