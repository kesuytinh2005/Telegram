# ============================================================
# core/fb_resolver.py
# Facebook UID / Entity Resolver V15 ULTRA
#
# PUBLIC CONTENT ONLY
# HTTP ONLY - no Playwright / Selenium / Cookie / Access Token
# ============================================================

from __future__ import annotations

import base64
import binascii
import html as html_lib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx


VERSION = "V15 ULTRA"
DEFAULT_TIMEOUT = 18.0
DEFAULT_MAX_PAGES = 8
DEFAULT_CONCURRENCY = 3
MAX_BODY_BYTES = 12 * 1024 * 1024

FB_HOSTS = {
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "mbasic.facebook.com", "web.facebook.com", "touch.facebook.com",
    "fb.watch", "www.fb.watch",
}
REDIRECT_HOSTS = {"l.facebook.com", "lm.facebook.com"}

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content",
    "utm_term", "fbclid", "rdid", "share_source", "ref", "refsrc",
    "mibextid", "tn",
}

ID_RE = re.compile(r"^\d{5,30}$")
PF_BID_RE = re.compile(r"^pfbid[A-Za-z0-9_-]+$", re.I)
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")


@dataclass
class Evidence:
    field: str
    value: str
    kind: str
    score: int
    source: str = ""
    url: str = ""
    context: str = ""
    direct: bool = False
    role: str = ""
    confidence: str = ""


@dataclass
class PageSnapshot:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    title: str = ""
    og_url: str = ""
    og_type: str = ""
    canonical: str = ""
    html: str = ""
    history: list[str] = field(default_factory=list)
    app_links: list[str] = field(default_factory=list)
    deep_links: list[str] = field(default_factory=list)
    jsonld: list[dict] = field(default_factory=list)
    headers: dict = field(default_factory=dict)


@dataclass
class EncodedID:
    token: str
    decoded: str | None = None
    valid: bool = False
    encoded_type: str | None = None
    actor_id: str | None = None
    object_id: str | None = None


@dataclass
class Result:
    requested_url: str
    resolved_url: str = ""
    crawl_chain: list[str] = field(default_factory=list)
    status: str = "UNRESOLVED"
    success: bool = False
    url_type: str = "UNKNOWN"

    user_uid: str | None = None
    user_verification: str = "NOT VERIFIED"

    entity_id: str | None = None
    entity_type: str = "UNKNOWN"

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

    actor_id: str | None = None

    decoded_id: str | None = None
    decoded_payload: str | None = None
    decoded_type: str | None = None
    decoded_actor_id: str | None = None
    share_token: str | None = None

    title: str = ""
    confidence: int = 0
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _valid_numeric(v: str | None) -> bool:
    return bool(v and ID_RE.fullmatch(str(v).strip()))


def _clean(v) -> str | None:
    if v is None:
        return None
    v = html_lib.unescape(str(v)).strip().strip("\"'")
    return v or None


def normalize_url(url: str) -> str:
    url = unquote((url or "").strip())
    p = urlparse(url)
    if not p.scheme:
        url = "https://" + url
        p = urlparse(url)

    host = p.netloc.lower().split(":")[0]
    if host in REDIRECT_HOSTS:
        qs = parse_qs(p.query)
        target = qs.get("u") or qs.get("url") or qs.get("next")
        if target:
            return normalize_url(target[0])

    query = []
    for k, vals in parse_qs(p.query, keep_blank_values=True).items():
        if k.lower() in TRACKING_PARAMS:
            continue
        for val in vals:
            query.append((k, val))

    path = re.sub(r"/{2,}", "/", p.path or "/")
    return p._replace(
        scheme="https",
        netloc=host,
        path=path,
        query="&".join(
            f"{k}={v}" for k, v in query
        ),
        fragment=""
    ).geturl()


def decode_fb_encoded_id(token: str) -> EncodedID:
    token = _clean(token) or ""
    out = EncodedID(token=token)

    # pfbid is opaque. Never pretend it is base64 and never invent an ID.
    if PF_BID_RE.fullmatch(token):
        return out

    candidates = [token]
    try:
        candidates.append(unquote(token))
    except Exception:
        pass

    raw = None
    for candidate in candidates:
        try:
            pad = "=" * (-len(candidate) % 4)
            raw = base64.urlsafe_b64decode(candidate + pad)
            break
        except (ValueError, binascii.Error):
            continue

    if raw is None:
        return out

    try:
        text = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return out

    if not text.startswith("S:"):
        return out

    out.decoded = text
    parts = text.split(":")
    if len(parts) == 3 and parts[1] == "_I":
        # S:_I<ACTOR>:<OBJECT>
        m = re.fullmatch(r"_I(\d+)", parts[1] + parts[2]) if False else None
        # Actual Facebook forms are parsed below from the whole payload.
    m = re.fullmatch(r"S:_I(\d+):(\d+)", text)
    if m:
        out.valid = True
        out.encoded_type = "_I"
        out.actor_id = m.group(1)
        out.object_id = m.group(2)
        return out

    m = re.fullmatch(r"S:([A-Za-z0-9_.-]+):(\d+)", text)
    if m:
        out.valid = True
        out.encoded_type = m.group(1)
        out.object_id = m.group(2)
        return out

    return out


class URLParser:
    def parse(self, url: str) -> list[Evidence]:
        e = []
        p = urlparse(normalize_url(url))
        path = [unquote(x) for x in p.path.strip("/").split("/") if x]
        low = [x.lower() for x in path]
        q = parse_qs(p.query)

        def add(field, value, kind, score, direct=True, role=""):
            value = _clean(value)
            if value:
                e.append(Evidence(field, value, kind, score, url=url,
                                  direct=direct, role=role))

        if low[:1] == ["profile.php"]:
            uid = (q.get("id") or [None])[0]
            if _valid_numeric(uid):
                add("user_uid", uid, "url.profile.php", 100, True, "profile")

        if low and low[0] == "people" and len(path) >= 3:
            uid = path[-1]
            if _valid_numeric(uid):
                add("user_uid", uid, "url.people", 100, True, "profile")

        if low and low[0] == "pages" and len(path) >= 3:
            pid = path[-1]
            if _valid_numeric(pid):
                add("page_id", pid, "url.pages", 100)

        if low and low[0] == "groups" and len(path) >= 2:
            gid = path[1]
            if _valid_numeric(gid):
                add("group_id", gid, "url.group", 100)
            if len(path) >= 4 and low[2] in {"posts", "permalink"}:
                oid = path[3]
                if _valid_numeric(oid) or PF_BID_RE.fullmatch(oid):
                    add("post_id", oid, "url.group_post", 98)

        if low and low[0] == "reel" and len(path) >= 2:
            add("reel_id", path[1], "url.reel", 100)

        if low and low[0] == "watch":
            vid = (q.get("v") or [None])[0]
            if _valid_numeric(vid):
                add("video_id", vid, "url.watch", 100)

        if low and low[0] == "video.php":
            vid = (q.get("v") or [None])[0]
            if _valid_numeric(vid):
                add("video_id", vid, "url.video.php", 100)

        if low and low[0] == "photo.php":
            fid = (q.get("fbid") or [None])[0]
            if _valid_numeric(fid):
                add("photo_id", fid, "url.photo.php", 100)

        if low and low[0] in {"permalink.php", "story.php"}:
            sfid = (q.get("story_fbid") or [None])[0]
            if sfid:
                add("post_id", sfid, "url.story_fbid", 98)

        if low and low[0] == "stories" and len(path) >= 3:
            actor = path[1]
            token = path[2]
            if _valid_numeric(actor):
                add("actor_id", actor, "url.story.actor", 96, True, "actor")
            add("share_token", token, "url.story.token", 90)
            dec = decode_fb_encoded_id(token)
            if dec.valid and dec.object_id:
                add("post_id", dec.object_id, "decoded.story", 92, False, "object")
                if dec.actor_id:
                    add("decoded_actor_id", dec.actor_id, "decoded.story", 94, False, "actor")

        if low and low[0] in {"share", "share.php"}:
            if len(path) >= 2:
                token = path[1]
                add("share_token", token, "url.share", 92)
                if low[1] == "p":
                    pass
            if len(path) >= 3 and low[1] in {"p", "v", "r"}:
                token = path[2]
                add("share_token", token, "url.share.token", 96)
                add("share_hint", low[1], "url.share.type", 100)

        # /username/posts/..., /username/videos/..., /username/photos/..., /username/reels/...
        if len(path) >= 3 and USERNAME_RE.fullmatch(path[0]):
            kind = low[1]
            oid = path[2]
            if kind == "posts":
                add("publisher_username", path[0], "url.username", 94, True, "publisher")
                if _valid_numeric(oid) or PF_BID_RE.fullmatch(oid):
                    add("post_id", oid, "url.user_post", 98)
            elif kind in {"videos", "video"}:
                add("publisher_username", path[0], "url.username", 94, True, "publisher")
                if _valid_numeric(oid):
                    add("video_id", oid, "url.user_video", 98)
            elif kind in {"photos", "photo"}:
                add("publisher_username", path[0], "url.username", 94, True, "publisher")
                if _valid_numeric(oid):
                    add("photo_id", oid, "url.user_photo", 98)
            elif kind in {"reels", "reel"}:
                add("publisher_username", path[0], "url.username", 94, True, "publisher")
                add("reel_id", oid, "url.user_reel", 98)

        if len(path) == 1 and USERNAME_RE.fullmatch(path[0]):
            reserved = {
                "home", "watch", "groups", "pages", "reels", "stories",
                "share", "photo", "photos", "video", "videos", "login",
                "settings", "marketplace", "gaming", "events"
            }
            if low[0] not in reserved:
                add("publisher_username", path[0], "url.username", 75, True, "publisher")

        if "media_fbid" in q:
            add("media_fbid", (q["media_fbid"] or [None])[0],
                "url.media_fbid", 96)

        if "id" in q:
            val = (q["id"] or [None])[0]
            if val:
                add("publisher_id", val, "url.query.id", 35, False, "publisher")

        return e


class MetadataParser:
    META_RE = re.compile(
        r'<meta\b[^>]*?(?:property|name)\s*=\s*["\']([^"\']+)["\'][^>]*?'
        r'content\s*=\s*["\'](.*?)["\'][^>]*>',
        re.I | re.S
    )
    LINK_RE = re.compile(
        r'<link\b[^>]*?rel\s*=\s*["\']([^"\']+)["\'][^>]*?'
        r'href\s*=\s*["\'](.*?)["\'][^>]*>',
        re.I | re.S
    )

    def parse(self, html: str, base_url: str):
        meta = {}
        for k, v in self.META_RE.findall(html or ""):
            meta[k.lower().strip()] = html_lib.unescape(v).strip()

        title_m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
        title = re.sub(r"\s+", " ", html_lib.unescape(title_m.group(1))).strip() if title_m else ""

        canonical = ""
        app_links = []
        deep_links = []
        for rel, href in self.LINK_RE.findall(html or ""):
            href = html_lib.unescape(href).strip()
            if "canonical" in rel.lower():
                canonical = urljoin(base_url, href)
            if href.startswith(("fb://", "fb-messenger://")):
                deep_links.append(href)

        for k, v in meta.items():
            if k.startswith("al:") or k.startswith("fb:"):
                if v.startswith(("fb://", "fb-messenger://")):
                    deep_links.append(v)
                elif "url" in k:
                    app_links.append(urljoin(base_url, v))

        jsonld = []
        for raw in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html or "", re.I | re.S
        ):
            try:
                obj = json.loads(html_lib.unescape(raw))
                if isinstance(obj, list):
                    jsonld.extend(x for x in obj if isinstance(x, dict))
                elif isinstance(obj, dict):
                    jsonld.append(obj)
            except Exception:
                pass

        return {
            "title": title,
            "og_url": meta.get("og:url", ""),
            "og_type": meta.get("og:type", ""),
            "canonical": canonical,
            "app_links": list(dict.fromkeys(app_links)),
            "deep_links": list(dict.fromkeys(deep_links)),
            "jsonld": jsonld,
        }


class HTMLIDEngine:
    KEY_MAP = {
        "profile_id": ("user_uid", 84, "html.profile_id", "profile"),
        "user_id": ("user_uid", 78, "html.user_id", "profile"),
        "userID": ("user_uid", 78, "html.userID", "profile"),
        "profileId": ("user_uid", 78, "html.profileId", "profile"),
        "profile_owner_id": ("user_uid", 86, "html.profile_owner_id", "profile"),
        "profileOwnerID": ("user_uid", 86, "html.profileOwnerID", "profile"),
        "page_id": ("page_id", 88, "html.page_id", "page"),
        "pageID": ("page_id", 88, "html.pageID", "page"),
        "group_id": ("group_id", 88, "html.group_id", "group"),
        "groupID": ("group_id", 88, "html.groupID", "group"),
        "post_id": ("post_id", 78, "html.post_id", "object"),
        "story_fbid": ("post_id", 84, "html.story_fbid", "object"),
        "postID": ("post_id", 78, "html.postID", "object"),
        "media_fbid": ("media_fbid", 84, "html.media_fbid", "media"),
        "video_id": ("video_id", 84, "html.video_id", "object"),
        "reel_id": ("reel_id", 84, "html.reel_id", "object"),
        "photo_id": ("photo_id", 84, "html.photo_id", "object"),
        "actor_id": ("actor_id", 60, "html.actor_id", "actor"),
        "author_id": ("author_id", 58, "html.author_id", "actor"),
        "owner_id": ("owner_id", 58, "html.owner_id", "actor"),
        "publisher_id": ("publisher_id", 58, "html.publisher_id", "publisher"),
        "entity_id": ("entity_id", 58, "html.entity_id", "entity"),
    }

    def parse(self, text: str, url: str):
        out = []
        text = text or ""

        for key, (field, score, kind, role) in self.KEY_MAP.items():
            patterns = [
                rf'["\']{re.escape(key)}["\']\s*:\s*["\'](\d{{5,30}}|pfbid[A-Za-z0-9_-]+)["\']',
                rf'["\']{re.escape(key)}["\']\s*:\s*(\d{{5,30}})',
                rf'\b{re.escape(key)}\b\s*=\s*["\'](\d{{5,30}}|pfbid[A-Za-z0-9_-]+)["\']',
            ]
            seen = set()
            for pat in patterns:
                for m in re.finditer(pat, text, re.I):
                    val = m.group(1)
                    if val in seen:
                        continue
                    seen.add(val)
                    out.append(Evidence(
                        field, val, kind, score, url=url,
                        context=text[max(0, m.start()-100):m.end()+100],
                        direct=False, role=role
                    ))

        # Explicit URL forms in HTML/JS.
        patterns = [
            ("post_id", r"/posts/(\d{5,30}|pfbid[A-Za-z0-9_-]+)", 82, "html.url.post"),
            ("post_id", r"/permalink/(\d{5,30}|pfbid[A-Za-z0-9_-]+)", 82, "html.url.permalink"),
            ("video_id", r"/videos/(\d{5,30})", 82, "html.url.video"),
            ("reel_id", r"/reel/(\d{5,30})", 82, "html.url.reel"),
            ("photo_id", r"/photo[^\"'\s/]*(?:/|\?[^\"']*fbid=)(\d{5,30})", 76, "html.url.photo"),
        ]
        for field, pat, score, kind in patterns:
            for m in re.finditer(pat, text, re.I):
                out.append(Evidence(field, m.group(1), kind, score, url=url))

        for m in re.finditer(r'fb://profile/(\d{5,30})', text, re.I):
            out.append(Evidence("user_uid", m.group(1), "deep.profile", 96,
                                url=url, direct=True, role="profile"))
        for m in re.finditer(r'fb://page/(\d{5,30})', text, re.I):
            out.append(Evidence("page_id", m.group(1), "deep.page", 96,
                                url=url, direct=True, role="page"))
        for m in re.finditer(r'fb://group/(\d{5,30})', text, re.I):
            out.append(Evidence("group_id", m.group(1), "deep.group", 96,
                                url=url, direct=True, role="group"))

        return out


class IdentityEngine:
    def correlate(self, evidence: list[Evidence]):
        # Strong explicit profile IDs are promoted only when structurally valid.
        for ev in evidence:
            if ev.field == "user_uid" and ev.role == "profile" and _valid_numeric(ev.value):
                ev.score = max(ev.score, 84)
                ev.confidence = "high"
        # Username + identity ID nearby => stronger correlation.
        usernames = [e.value for e in evidence if e.field == "publisher_username"]
        if usernames:
            for ev in evidence:
                if ev.field in {"actor_id", "author_id", "owner_id", "publisher_id"} and _valid_numeric(ev.value):
                    ev.field = "user_uid"
                    ev.score = max(ev.score, 82)
                    ev.kind = "correlation.username_id"
                    ev.role = "profile"
                    ev.confidence = "medium"
        return evidence


class DeepLinkEngine:
    def parse(self, links: list[str]):
        out = []
        for link in links:
            m = re.fullmatch(r"fb://profile/(\d{5,30})", link, re.I)
            if m:
                out.append(Evidence("user_uid", m.group(1), "deep.profile", 96,
                                    url=link, direct=True, role="profile"))
            m = re.fullmatch(r"fb://page/(\d{5,30})", link, re.I)
            if m:
                out.append(Evidence("page_id", m.group(1), "deep.page", 96,
                                    url=link, direct=True, role="page"))
            m = re.fullmatch(r"fb://group/(\d{5,30})", link, re.I)
            if m:
                out.append(Evidence("group_id", m.group(1), "deep.group", 96,
                                    url=link, direct=True, role="group"))
        return out


class JSONLDEngine:
    def parse(self, docs: list[dict], url: str):
        out = []
        for doc in docs:
            author = doc.get("author")
            if isinstance(author, dict):
                au = author.get("url")
                if au:
                    p = urlparse(str(au))
                    parts = [x for x in p.path.strip("/").split("/") if x]
                    if parts and USERNAME_RE.fullmatch(parts[-1]):
                        out.append(Evidence("publisher_username", parts[-1],
                                            "jsonld.author", 72, url=url, role="publisher"))
                name = author.get("name")
                if name:
                    out.append(Evidence("publisher_name", str(name),
                                        "jsonld.author_name", 50, url=url))
            obj_url = doc.get("url")
            if obj_url:
                m = re.search(r"/posts/(\d{5,30})", str(obj_url), re.I)
                if m:
                    out.append(Evidence("post_id", m.group(1),
                                        "jsonld.object_url", 74, url=url))
        return out


class HTTPClient:
    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1",
        }

    async def get(self, client: httpx.AsyncClient, url: str):
        last = None
        for attempt in range(3):
            try:
                r = await client.get(url, headers=self.headers)
                if r.status_code in {408, 425, 429} or r.status_code >= 500:
                    last = RuntimeError(f"HTTP {r.status_code}")
                    if attempt < 2:
                        await __import__("asyncio").sleep(0.6 * (attempt + 1))
                        continue
                data = r.content[:MAX_BODY_BYTES]
                return r, data
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
                last = exc
                if attempt < 2:
                    await __import__("asyncio").sleep(0.6 * (attempt + 1))
        raise last or RuntimeError("HTTP request failed")


class FacebookResolver:
    def __init__(self, max_pages=DEFAULT_MAX_PAGES, concurrency=DEFAULT_CONCURRENCY,
                 timeout=DEFAULT_TIMEOUT, debug=False):
        self.max_pages = max(1, min(int(max_pages), 20))
        self.concurrency = max(1, min(int(concurrency), 8))
        self.http = HTTPClient(timeout)
        self.debug = debug
        self.url_parser = URLParser()
        self.meta = MetadataParser()
        self.html_engine = HTMLIDEngine()
        self.identity = IdentityEngine()
        self.deep = DeepLinkEngine()
        self.jsonld = JSONLDEngine()

    def _add(self, all_e: list[Evidence], ev: Evidence):
        if not ev.value:
            return
        if not any(
            x.field == ev.field and x.value == ev.value and x.kind == ev.kind
            for x in all_e
        ):
            all_e.append(ev)

    def _classify(self, url: str, e: list[Evidence]):
        p = urlparse(url)
        path = [x.lower() for x in p.path.strip("/").split("/") if x]
        vals = {x.field: [x.value for x in e if x.field == x.field] for x in []}  # no-op
        def has(field): return any(x.field == field for x in e)
        if path[:1] == ["groups"]:
            return "GROUP_POST" if has("post_id") else "GROUP"
        if path[:1] == ["pages"]:
            return "PAGE"
        if path[:1] == ["stories"]:
            return "STORY"
        if path[:1] == ["reel"] or any(x.field == "reel_id" for x in e):
            return "REEL"
        if path[:1] == ["watch"]:
            return "WATCH_VIDEO"
        if path[:1] == ["video.php"]:
            return "VIDEO"
        if path[:1] == ["photo.php"]:
            return "PHOTO"
        if path[:1] == ["share"]:
            hints = [x.value for x in e if x.field == "share_hint"]
            if hints:
                return {"p": "SHARE_POST", "v": "SHARE_VIDEO", "r": "SHARE_REEL"}.get(hints[-1], "SHARE")
            return "SHARE"
        if any(x.field == "publisher_username" for x in e):
            if has("post_id"): return "USER_POST"
            if has("video_id"): return "USER_VIDEO"
            if has("photo_id"): return "USER_PHOTO"
            if has("reel_id"): return "USER_REEL"
            return "PROFILE"
        if has("user_uid") and not any(x.field in {"post_id","video_id","photo_id","reel_id"} for x in e):
            return "PROFILE"
        if has("post_id"): return "POST"
        if has("video_id"): return "VIDEO"
        if has("photo_id"): return "PHOTO"
        return "UNKNOWN"

    def _best(self, e, field):
        cand = [x for x in e if x.field == field]
        if not cand:
            return None
        # numeric IDs preferred over opaque strings when both exist
        cand.sort(key=lambda x: (x.score, _valid_numeric(x.value), x.direct), reverse=True)
        return cand[0].value

    async def resolve(self, requested_url: str) -> Result:
        requested_url = (requested_url or "").strip()
        result = Result(requested_url=requested_url)
        if not requested_url:
            result.warnings.append("URL rỗng.")
            return result

        try:
            first = normalize_url(requested_url)
        except Exception as exc:
            result.warnings.append(f"URL không hợp lệ: {exc}")
            return result

        all_e: list[Evidence] = []
        queue = [first]
        visited = set()
        snapshots = []

        limits = httpx.Limits(
            max_connections=self.concurrency,
            max_keepalive_connections=self.concurrency,
        )
        timeout = httpx.Timeout(self.http.timeout)
        async with httpx.AsyncClient(
            follow_redirects=True, max_redirects=12,
            timeout=timeout, http2=False, limits=limits
        ) as client:
            while queue and len(visited) < self.max_pages:
                url = queue.pop(0)
                url = normalize_url(url)
                if url in visited:
                    continue
                visited.add(url)
                try:
                    r, body = await self.http.get(client, url)
                except Exception as exc:
                    result.warnings.append(f"HTTP lỗi: {type(exc).__name__}")
                    continue

                text = body.decode("utf-8", errors="replace")
                final = normalize_url(str(r.url))
                meta = self.meta.parse(text, final)
                snap = PageSnapshot(
                    requested_url=url, final_url=final,
                    status_code=r.status_code,
                    content_type=r.headers.get("content-type", ""),
                    title=meta["title"], og_url=meta["og_url"],
                    og_type=meta["og_type"], canonical=meta["canonical"],
                    html=text, history=[],
                    app_links=meta["app_links"], deep_links=meta["deep_links"],
                    jsonld=meta["jsonld"], headers=dict(r.headers),
                )
                snapshots.append(snap)

                for ev in self.url_parser.parse(final):
                    self._add(all_e, ev)
                for ev in self.html_engine.parse(text, final):
                    self._add(all_e, ev)
                for ev in self.deep.parse(snap.deep_links):
                    self._add(all_e, ev)
                for ev in self.jsonld.parse(snap.jsonld, final):
                    self._add(all_e, ev)

                # metadata URL evidence / crawl candidates
                for candidate in [snap.og_url, snap.canonical] + snap.app_links:
                    if candidate and candidate.startswith(("http://", "https://")):
                        try:
                            n = normalize_url(candidate)
                            host = urlparse(n).netloc.lower().split(":")[0]
                            if host in FB_HOSTS and n not in visited and n not in queue:
                                queue.append(n)
                        except Exception:
                            pass

                for doc in snap.jsonld:
                    for key in ("url",):
                        candidate = doc.get(key)
                        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                            try:
                                n = normalize_url(candidate)
                                if urlparse(n).netloc in FB_HOSTS and n not in visited and n not in queue:
                                    queue.append(n)
                            except Exception:
                                pass
                    author = doc.get("author")
                    if isinstance(author, dict):
                        candidate = author.get("url")
                        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                            try:
                                n = normalize_url(candidate)
                                if urlparse(n).netloc in FB_HOSTS and n not in visited and n not in queue:
                                    queue.append(n)
                            except Exception:
                                pass

        # Username -> profile page is useful for identity correlation.
        username = self._best(all_e, "publisher_username")
        if username and len(visited) < self.max_pages:
            profile_url = f"https://www.facebook.com/{username}"
            if profile_url not in visited:
                try:
                    limits = httpx.Limits(max_connections=1)
                    async with httpx.AsyncClient(
                        follow_redirects=True, max_redirects=12,
                        timeout=httpx.Timeout(self.http.timeout),
                        http2=False, limits=limits
                    ) as client:
                        r, body = await self.http.get(client, profile_url)
                        final = normalize_url(str(r.url))
                        text = body.decode("utf-8", errors="replace")
                        for ev in self.url_parser.parse(final):
                            self._add(all_e, ev)
                        for ev in self.html_engine.parse(text, final):
                            self._add(all_e, ev)
                        meta = self.meta.parse(text, final)
                        for ev in self.deep.parse(meta["deep_links"]):
                            self._add(all_e, ev)
                except Exception as exc:
                    if self.debug:
                        result.warnings.append(f"Profile correlation lỗi: {type(exc).__name__}")

        all_e = self.identity.correlate(all_e)

        # Derive URL type after correlation.
        result.resolved_url = snapshots[-1].final_url if snapshots else first
        result.crawl_chain = [s.final_url for s in snapshots]
        result.url_type = self._classify(result.resolved_url, all_e)

        result.user_uid = self._best(all_e, "user_uid")
        result.group_id = self._best(all_e, "group_id")
        result.page_id = self._best(all_e, "page_id")
        result.post_id = self._best(all_e, "post_id")
        result.story_id = self._best(all_e, "story_id")
        result.video_id = self._best(all_e, "video_id")
        result.reel_id = self._best(all_e, "reel_id")
        result.photo_id = self._best(all_e, "photo_id")
        result.media_fbid = self._best(all_e, "media_fbid")
        result.actor_id = self._best(all_e, "actor_id") or self._best(all_e, "decoded_actor_id")
        result.publisher_username = username
        result.publisher_id = self._best(all_e, "publisher_id")
        result.share_token = self._best(all_e, "share_token")

        # Decode any explicit share/pfbid token, but NEVER use opaque pfbid as numeric UID.
        token_candidates = []
        for field in ("share_token", "post_id"):
            for ev in all_e:
                if ev.field == field and PF_BID_RE.fullmatch(ev.value):
                    token_candidates.append(ev.value)
        for token in dict.fromkeys(token_candidates):
            dec = decode_fb_encoded_id(token)
            if dec.valid:
                result.decoded_id = dec.object_id
                result.decoded_payload = dec.decoded
                result.decoded_type = dec.encoded_type
                result.decoded_actor_id = dec.actor_id
                if not result.post_id and dec.object_id:
                    result.post_id = dec.object_id

        # Entity ID priority.
        if result.group_id:
            result.entity_id = result.post_id or result.group_id
            result.publisher_type = "GROUP"
        elif result.page_id:
            result.entity_id = (
                result.post_id or result.video_id or result.photo_id
                or result.reel_id or result.page_id
            )
            result.publisher_type = "PAGE"
        else:
            result.entity_id = (
                result.post_id or result.video_id or result.photo_id
                or result.reel_id or result.story_id or result.user_uid
            )
            result.publisher_type = "USER" if result.user_uid else None

        # Strong UID verification rules.
        uid_e = [x for x in all_e if x.field == "user_uid" and _valid_numeric(x.value)]
        distinct_sources = {x.kind for x in uid_e}
        max_score = max((x.score for x in uid_e), default=0)
        has_profile_role = any(x.role == "profile" for x in uid_e)
        direct_profile = any(x.role == "profile" and x.direct for x in uid_e)
        if result.user_uid and (
            (has_profile_role and len(distinct_sources) >= 2 and max_score >= 80)
            or (has_profile_role and max_score >= 92 and direct_profile)
        ):
            result.user_verification = "VERIFIED"
        elif result.user_uid:
            result.user_verification = "UNVERIFIED"
        else:
            result.user_verification = "NOT VERIFIED"

        # Actor/owner IDs alone are not user UID.
        if not has_profile_role and result.user_uid:
            result.user_uid = None
            result.user_verification = "NOT VERIFIED"

        # Type refinement.
        if result.group_id and result.post_id:
            result.entity_type = "GROUP_POST"
        elif result.page_id and result.post_id:
            result.entity_type = "PAGE_POST"
        elif result.page_id and result.video_id:
            result.entity_type = "PAGE_VIDEO"
        elif result.page_id and result.photo_id:
            result.entity_type = "PAGE_PHOTO"
        elif result.user_uid and result.post_id and result.user_verification == "VERIFIED":
            result.entity_type = "USER_POST"
        elif result.post_id:
            result.entity_type = "USER_POST_UNVERIFIED" if result.url_type == "USER_POST" else "POST"
        elif result.reel_id:
            result.entity_type = "REEL"
        elif result.video_id:
            result.entity_type = "USER_VIDEO" if result.url_type == "USER_VIDEO" else "VIDEO"
        elif result.photo_id:
            result.entity_type = "USER_PHOTO" if result.url_type == "USER_PHOTO" else "PHOTO"
        elif result.story_id:
            result.entity_type = "STORY"
        elif result.group_id:
            result.entity_type = "GROUP"
        elif result.page_id:
            result.entity_type = "PAGE"
        elif result.user_uid and result.user_verification == "VERIFIED":
            result.entity_type = "USER"
        elif result.publisher_username:
            result.entity_type = "USER_PROFILE"

        # Identity URL.
        if result.user_uid and result.user_verification == "VERIFIED":
            result.identity_url = f"https://www.facebook.com/profile.php?id={result.user_uid}"
        elif result.publisher_username:
            result.identity_url = f"https://www.facebook.com/{result.publisher_username}"

        # Conflict: publisher_id must not silently overwrite verified UID.
        if result.user_uid and result.publisher_id and _valid_numeric(result.publisher_id):
            if result.publisher_id != result.user_uid:
                result.warnings.append("CONFLICT: publisher_id khác verified user_uid.")
                result.user_uid = None
                result.user_verification = "NOT VERIFIED"

        # Title.
        for s in snapshots:
            if s.title:
                result.title = s.title[:300]
                break

        # Confidence.
        scores = [x.score for x in all_e]
        result.confidence = min(100, max(scores) if scores else 0)
        if result.user_verification == "VERIFIED":
            result.confidence = min(100, max(result.confidence, 90))
        if result.entity_id:
            result.status = "EXACT" if result.user_verification == "VERIFIED" else "PARTIAL"
            result.success = True
        else:
            result.status = "UNRESOLVED"
            result.success = False

        result.evidence = all_e
        if not snapshots:
            result.warnings.append("Không lấy được nội dung Facebook công khai.")
        return result
