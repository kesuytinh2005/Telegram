#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
======================================================================
 FACEBOOK RESOLVER V32 - PRECISION IDENTITY ENGINE
======================================================================

PUBLIC FACEBOOK CONTENT
HTTP ONLY

NO:
    - Playwright
    - Selenium
    - Chromium
    - Cookies
    - Login
    - Facebook Access Token
    - Graph API Authentication

PRIMARY GOAL
------------
Resolve:

    Facebook Profile
        username
        name
        UID
        avatar
        bio
        profile URL

    Facebook Page
        name
        page ID
        URL

    Facebook Group
        name
        group ID
        URL

    Facebook Content
        post ID
        reel ID
        video ID
        photo ID
        story ID
        title
        publisher identity

IMPORTANT
---------
Never assume:

    route ID == UID
    post ID == UID
    video ID == UID
    reel ID == UID
    pfbid == UID

UID must be correlated with the actual publisher/profile.

======================================================================
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import requests
from telethon import events


# ======================================================================
# LOGGING
# ======================================================================

log = logging.getLogger(
    "getuidfb"
)


# ======================================================================
# CONFIG
# ======================================================================

REQUEST_TIMEOUT = 12

MAX_HTML_BYTES = 14 * 1024 * 1024

MAX_INPUT_URLS = 20

MAX_DISCOVERED_URLS = 100

MAX_PROFILE_CHECKS = 6

MAX_EVIDENCE = 8

FETCH_WORKERS = 4

CACHE_TTL = 300


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
    "touch.facebook.com",
    "free.facebook.com",
}

FB_WATCH_HOSTS = {
    "fb.watch",
}


# ======================================================================
# USER AGENTS
# ======================================================================

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Linux; Android 14) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Mobile Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
]


# ======================================================================
# REGEX
# ======================================================================

NUMERIC_ID_RE = re.compile(
    r"^\d{5,21}$"
)

FACEBOOK_URL_RE = re.compile(
    r"""(?ix)
    (?:
        https?://
    )?
    (?:
        www\.
        |m\.
        |mbasic\.
        |mobile\.
        |web\.
        |touch\.
        |free\.
    )?
    facebook\.com
    /[^\s<>"']*
    |
    (?:
        https?://
    )?
    fb\.watch
    /[^\s<>"']*
    """
)

SEMANTIC_ID_RE = re.compile(
    r"""
    (?P<key>
        user[_\-]?id
        |userid
        |profile[_\-]?id
        |profileid
        |owner[_\-]?id
        |ownerid
        |publisher[_\-]?id
        |publisherid
        |author[_\-]?id
        |authorid
        |creator[_\-]?id
        |creatorid
        |from[_\-]?id
        |fromid
        |actor[_\-]?id
        |actorid
        |page[_\-]?owner[_\-]?id
        |page[_\-]?id
        |group[_\-]?id
        |post[_\-]?id
        |story[_\-]?fbid
        |story[_\-]?id
        |video[_\-]?id
        |reel[_\-]?id
        |photo[_\-]?id
        |media[_\-]?fbid
        |album[_\-]?id
    )
    \s*
    ["':=]+
    \s*
    (?P<value>\d{5,21})
    """,
    re.I | re.X,
)


# ======================================================================
# HELPERS
# ======================================================================

def clean_text(
    value: Any,
) -> str:

    if value is None:
        return ""

    value = html.unescape(
        str(value)
    )

    value = value.replace(
        "\x00",
        " ",
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def truncate(
    value: Any,
    limit: int,
) -> str:

    value = clean_text(
        value
    )

    if len(value) <= limit:
        return value

    return value[:limit - 3] + "..."


def is_numeric_id(
    value: Any,
) -> bool:

    if value is None:
        return False

    return bool(
        NUMERIC_ID_RE.fullmatch(
            str(value).strip()
        )
    )


def normalize_host(
    host: str,
) -> str:

    return (
        host or ""
    ).lower().strip().rstrip(".")


def is_facebook_host(
    host: str,
) -> bool:

    host = normalize_host(
        host
    )

    return (
        host in FACEBOOK_HOSTS
        or host in FB_WATCH_HOSTS
    )


# ======================================================================
# URL NORMALIZATION
# ======================================================================

def normalize_url(
    url: str,
) -> str:

    if not url:
        return ""

    url = clean_text(
        url
    )

    url = url.strip(
        " \t\r\n"
        "\"'<>[]{}"
        ".,!?;:，。！？；：、"
    )

    if not re.match(
        r"^https?://",
        url,
        re.I,
    ):

        lower = url.lower()

        if (
            lower.startswith(
                "facebook.com/"
            )
            or lower.startswith(
                "www.facebook.com/"
            )
            or lower.startswith(
                "m.facebook.com/"
            )
            or lower.startswith(
                "mbasic.facebook.com/"
            )
            or lower.startswith(
                "fb.watch/"
            )
        ):

            url = "https://" + url

        else:
            return ""

    try:

        p = urlparse(
            url
        )

        host = normalize_host(
            p.hostname or ""
        )

        if not is_facebook_host(
            host
        ):
            return ""

        path = re.sub(
            r"/{2,}",
            "/",
            p.path or "/",
        )

        path = path.rstrip(
            " \t\r\n"
        )

        if not path:
            path = "/"

        pairs = parse_qsl(
            p.query,
            keep_blank_values=True,
        )

        keep = []

        remove = {
            "fbclid",
            "refsrc",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "rdid",
            "share_url",
            "__cft__",
            "__tn__",
        }

        for key, value in pairs:

            if key.lower() in remove:
                continue

            keep.append(
                (key, value)
            )

        query = urlencode(
            keep,
            doseq=True,
        )

        return urlunparse(
            (
                "https",
                host,
                path,
                "",
                query,
                "",
            )
        )

    except Exception:

        return ""


# ======================================================================
# URL SCANNER
# ======================================================================

def extract_facebook_urls(
    text: str,
) -> List[str]:

    if not text:
        return []

    text = html.unescape(
        text
    )

    text = text.replace(
        "\\/",
        "/",
    )

    result = []

    seen = set()

    def add(
        value: str,
    ):

        normalized = normalize_url(
            value
        )

        if not normalized:
            return

        if normalized in seen:
            return

        seen.add(
            normalized
        )

        result.append(
            normalized
        )

    for match in FACEBOOK_URL_RE.finditer(
        text
    ):

        raw = match.group(
            0
        )

        raw = raw.rstrip(
            ".,;:!?)]}>'\""
        )

        add(
            raw
        )

        if len(result) >= MAX_INPUT_URLS:
            break

    return result


# ======================================================================
# URL SHAPE
# ======================================================================

@dataclass
class URLShape:

    original: str

    normalized: str

    host: str = ""

    path: str = ""

    query: str = ""

    kind: str = "unknown"

    username: str = ""

    route_entity_id: str = ""

    post_id: str = ""

    video_id: str = ""

    reel_id: str = ""

    photo_id: str = ""

    story_id: str = ""

    group_id: str = ""

    page_id: str = ""

    album_id: str = ""


# ======================================================================
# QUERY VALUE
# ======================================================================

def query_value(
    parsed,
    *names: str,
) -> str:

    pairs = parse_qsl(
        parsed.query,
        keep_blank_values=True,
    )

    lookup = {
        str(k).lower(): v
        for k, v in pairs
    }

    for name in names:

        value = lookup.get(
            name.lower()
        )

        if value:
            return clean_text(
                value
            )

    return ""


# ======================================================================
# RESERVED PROFILE ROUTES
# ======================================================================

RESERVED_ROUTES = {
    "home",
    "watch",
    "reel",
    "reels",
    "videos",
    "video",
    "photos",
    "photo",
    "groups",
    "pages",
    "people",
    "profile.php",
    "photo.php",
    "story.php",
    "permalink.php",
    "share",
    "sharer",
    "plugins",
    "events",
    "marketplace",
    "gaming",
    "messages",
    "login",
    "logout",
    "settings",
    "help",
    "privacy",
    "terms",
}


# ======================================================================
# CLASSIFY URL
# ======================================================================

def classify_url(
    url: str,
) -> URLShape:

    normalized = normalize_url(
        url
    )

    if not normalized:

        return URLShape(
            original=url,
            normalized="",
        )

    parsed = urlparse(
        normalized
    )

    host = normalize_host(
        parsed.hostname or ""
    )

    segments = [
        unquote(x).strip()
        for x in parsed.path.split("/")
        if x.strip()
    ]

    lower = [
        x.lower()
        for x in segments
    ]

    shape = URLShape(
        original=url,
        normalized=normalized,
        host=host,
        path=parsed.path,
        query=parsed.query,
    )

    # --------------------------------------------------------------
    # fb.watch
    # --------------------------------------------------------------

    if host in FB_WATCH_HOSTS:

        shape.kind = "share"

        return shape

    # --------------------------------------------------------------
    # profile.php
    # --------------------------------------------------------------

    if lower[:1] == [
        "profile.php"
    ]:

        uid = query_value(
            parsed,
            "id",
            "uid",
        )

        shape.kind = "profile"

        if is_numeric_id(uid):
            shape.route_entity_id = uid

        return shape

    # --------------------------------------------------------------
    # photo.php
    # --------------------------------------------------------------

    if lower[:1] == [
        "photo.php"
    ]:

        photo = query_value(
            parsed,
            "fbid",
            "photo_id",
            "id",
        )

        if is_numeric_id(photo):
            shape.photo_id = photo

        shape.kind = "photo"

        return shape

    # --------------------------------------------------------------
    # story.php
    # --------------------------------------------------------------

    if lower[:1] == [
        "story.php"
    ]:

        story = query_value(
            parsed,
            "story_fbid",
            "story_id",
            "id",
        )

        if is_numeric_id(story):
            shape.story_id = story

        shape.kind = "story"

        return shape

    # --------------------------------------------------------------
    # watch
    # --------------------------------------------------------------

    if lower[:1] == [
        "watch"
    ]:

        video = query_value(
            parsed,
            "v",
            "video_id",
        )

        if is_numeric_id(video):
            shape.video_id = video

        shape.kind = "video"

        return shape

    # --------------------------------------------------------------
    # reel
    # --------------------------------------------------------------

    if lower[:1] in (
        ["reel"],
        ["reels"],
    ):

        if len(segments) >= 2:

            value = segments[1]

            if is_numeric_id(value):
                shape.reel_id = value

        shape.kind = "reel"

        return shape

    # --------------------------------------------------------------
    # groups
    # --------------------------------------------------------------

    if lower[:1] == [
        "groups"
    ]:

        shape.kind = "group"

        if len(segments) >= 2:

            value = segments[1]

            if is_numeric_id(value):

                shape.group_id = value
                shape.route_entity_id = value

            else:

                shape.username = value

        if "posts" in lower:

            idx = lower.index(
                "posts"
            )

            shape.kind = "group_post"

            if idx + 1 < len(segments):

                value = segments[
                    idx + 1
                ]

                if is_numeric_id(value):
                    shape.post_id = value

        return shape

    # --------------------------------------------------------------
    # pages
    # --------------------------------------------------------------

    if lower[:1] == [
        "pages"
    ]:

        shape.kind = "page"

        if len(segments) >= 2:

            first = segments[1]

            if is_numeric_id(first):

                shape.page_id = first
                shape.route_entity_id = first

            else:

                shape.username = first

        if len(segments) >= 3:

            second = segments[2]

            if is_numeric_id(second):

                shape.page_id = second
                shape.route_entity_id = second

        return shape

    # --------------------------------------------------------------
    # people
    # --------------------------------------------------------------

    if lower[:1] == [
        "people"
    ]:

        shape.kind = "profile"

        if len(segments) >= 2:
            shape.username = segments[1]

        if len(segments) >= 3:

            value = segments[2]

            if is_numeric_id(value):
                shape.route_entity_id = value

        return shape

    # --------------------------------------------------------------
    # generic numeric route
    # --------------------------------------------------------------

    if segments:

        first = segments[0]

        if is_numeric_id(first):

            shape.route_entity_id = first

        elif (
            first.lower()
            not in RESERVED_ROUTES
        ):

            shape.username = first

    # --------------------------------------------------------------
    # CONTENT UNDER USERNAME
    #
    # /username/videos/123
    # /username/reels/123
    # /username/posts/123
    # --------------------------------------------------------------

    for index, part in enumerate(
        lower
    ):

        if part == "posts":

            shape.kind = "post"

            if index + 1 < len(segments):

                value = segments[
                    index + 1
                ]

                if is_numeric_id(value):
                    shape.post_id = value

            return shape

        if part == "videos":

            shape.kind = "video"

            if index + 1 < len(segments):

                value = segments[
                    index + 1
                ]

                if is_numeric_id(value):
                    shape.video_id = value

            return shape

        if part == "reels":

            shape.kind = "reel"

            if index + 1 < len(segments):

                value = segments[
                    index + 1
                ]

                if is_numeric_id(value):
                    shape.reel_id = value

            return shape

        if part == "photos":

            shape.kind = "photo"

            if index + 1 < len(segments):

                value = segments[
                    index + 1
                ]

                if is_numeric_id(value):
                    shape.photo_id = value

            return shape

        if part == "stories":

            shape.kind = "story"

            if index + 1 < len(segments):

                value = segments[
                    index + 1
                ]

                if is_numeric_id(value):
                    shape.story_id = value

            return shape

    # --------------------------------------------------------------
    # PURE USERNAME PROFILE
    #
    # /nvlnopro/
    #
    # THIS IS THE IMPORTANT FIX.
    # --------------------------------------------------------------

    if (
        len(segments) == 1
        and shape.username
    ):

        shape.kind = "profile"

        return shape

    # --------------------------------------------------------------
    # PURE NUMERIC ENTITY
    # --------------------------------------------------------------

    if (
        len(segments) == 1
        and shape.route_entity_id
    ):

        shape.kind = "entity"

        return shape

    shape.kind = "unknown"

    return shape


# ======================================================================
# PROFILE URL
# ======================================================================

def profile_url_from_shape(
    shape: URLShape,
) -> str:

    if shape.username:

        return (
            "https://www.facebook.com/"
            + quote(
                shape.username,
                safe="@._-",
            )
        )

    if shape.route_entity_id:

        return (
            "https://www.facebook.com/"
            "profile.php?id="
            + shape.route_entity_id
        )

    return ""


# ======================================================================
# SNAPSHOT
# ======================================================================

@dataclass
class Snapshot:

    requested_url: str

    final_url: str = ""

    status_code: int = 0

    html: str = ""

    title: str = ""

    canonical: str = ""

    meta: Dict[str, str] = field(
        default_factory=dict
    )

    links: List[str] = field(
        default_factory=list
    )

    jsonld: List[Any] = field(
        default_factory=list
    )

    embedded_json: List[Any] = field(
        default_factory=list
    )

    redirects: List[str] = field(
        default_factory=list
    )

    error: str = ""


# ======================================================================
# HTML PARSER
# ======================================================================

class FacebookHTMLParser(
    HTMLParser
):

    def __init__(
        self,
    ):

        super().__init__(
            convert_charrefs=True
        )

        self.title_parts = []

        self.meta = {}

        self.links = []

        self.jsonld = []

        self.embedded_json = []

        self.in_title = False

        self.in_script = False

        self.script_type = ""

        self.script_buffer = []

    def handle_starttag(
        self,
        tag,
        attrs,
    ):

        tag = tag.lower()

        data = {
            str(k).lower():
            (
                ""
                if v is None
                else str(v)
            )
            for k, v in attrs
        }

        if tag == "title":

            self.in_title = True

        elif tag == "meta":

            key = (
                data.get("property")
                or data.get("name")
                or data.get("itemprop")
                or ""
            ).strip().lower()

            value = (
                data.get("content")
                or ""
            ).strip()

            if key and value:

                self.meta[
                    key
                ] = value

        elif tag == "link":

            href = data.get(
                "href"
            )

            if href:
                self.links.append(
                    href
                )

        elif tag == "script":

            self.in_script = True

            self.script_type = (
                data.get(
                    "type",
                    "",
                )
                .lower()
            )

            self.script_buffer = []

    def handle_endtag(
        self,
        tag,
    ):

        tag = tag.lower()

        if tag == "title":

            self.in_title = False

        elif tag == "script":

            content = "".join(
                self.script_buffer
            ).strip()

            if content:

                self.parse_script(
                    content
                )

            self.in_script = False

            self.script_type = ""

            self.script_buffer = []

    def handle_data(
        self,
        data,
    ):

        if self.in_title:

            self.title_parts.append(
                data
            )

        if self.in_script:

            if len(
                "".join(
                    self.script_buffer
                )
            ) < 4_000_000:

                self.script_buffer.append(
                    data
                )

    def parse_script(
        self,
        content: str,
    ):

        if (
            "ld+json"
            in self.script_type
        ):

            try:

                self.jsonld.append(
                    json.loads(
                        content
                    )
                )

            except Exception:
                pass

            return

        if (
            "application/json"
            in self.script_type
        ):

            try:

                self.embedded_json.append(
                    json.loads(
                        content
                    )
                )

            except Exception:
                pass

            return

        if any(
            x in content.lower()
            for x in (
                "user_id",
                "profile_id",
                "owner_id",
                "creator_id",
                "publisher_id",
                "page_id",
                "group_id",
                "video_id",
                "reel_id",
            )
        ):

            self.embedded_json.extend(
                extract_json_fragments(
                    content
                )
            )

    def get_title(
        self,
    ) -> str:

        return clean_text(
            "".join(
                self.title_parts
            )
        )


# ======================================================================
# JSON FRAGMENT EXTRACTION
# ======================================================================

def extract_json_fragments(
    text: str,
    max_fragments: int = 80,
) -> List[Any]:

    if not text:
        return []

    results = []

    starts = [
        m.start()
        for m in re.finditer(
            r"\{",
            text,
        )
    ]

    for start in starts[:1200]:

        depth = 0

        in_string = False

        escape = False

        for i in range(
            start,
            min(
                len(text),
                start + 1_500_000,
            ),
        ):

            ch = text[i]

            if in_string:

                if escape:

                    escape = False

                elif ch == "\\":

                    escape = True

                elif ch == '"':

                    in_string = False

                continue

            if ch == '"':

                in_string = True

                continue

            if ch == "{":

                depth += 1

            elif ch == "}":

                depth -= 1

                if depth == 0:

                    fragment = text[
                        start:i + 1
                    ]

                    try:

                        results.append(
                            json.loads(
                                fragment
                            )
                        )

                    except Exception:
                        pass

                    break

        if len(results) >= max_fragments:
            break

    return results


# ======================================================================
# PARSE SNAPSHOT
# ======================================================================

def parse_snapshot(
    snapshot: Snapshot,
):

    if not snapshot.html:
        return

    parser = FacebookHTMLParser()

    try:

        parser.feed(
            snapshot.html
        )

        parser.close()

    except Exception as exc:

        snapshot.error = str(
            exc
        )

    snapshot.title = (
        parser.get_title()
    )

    snapshot.meta = (
        parser.meta
    )

    snapshot.links = (
        parser.links
    )

    snapshot.jsonld = (
        parser.jsonld
    )

    snapshot.embedded_json = (
        parser.embedded_json
    )

    for key in (
        "og:url",
        "al:web:url",
        "twitter:url",
    ):

        value = snapshot.meta.get(
            key
        )

        normalized = normalize_url(
            value
        )

        if normalized:

            snapshot.canonical = (
                normalized
            )

            break


# ======================================================================
# EVIDENCE
# ======================================================================

@dataclass
class Evidence:

    value: str

    role: str

    source: str

    weight: float

    key: str = ""

    path: str = ""

    context: str = ""

    url: str = ""

    verified: bool = False

    relation: str = ""


class EvidenceGraph:

    def __init__(
        self,
    ):

        self.items: List[
            Evidence
        ] = []

        self.seen = set()

    def add(
        self,
        evidence: Evidence,
    ):

        if not is_numeric_id(
            evidence.value
        ):
            return

        fp = (
            evidence.value,
            evidence.role,
            evidence.source,
            evidence.key,
            evidence.path,
        )

        if fp in self.seen:
            return

        self.seen.add(
            fp
        )

        self.items.append(
            evidence
        )

    def for_role(
        self,
        role: str,
    ):

        return [
            x
            for x in self.items
            if x.role == role
        ]

    def for_value(
        self,
        value: str,
    ):

        return [
            x
            for x in self.items
            if x.value == value
        ]

    def values(
        self,
        role: str,
    ) -> Set[str]:

        return {
            x.value
            for x in self.items
            if x.role == role
        }


# ======================================================================
# EVIDENCE WEIGHTS
# ======================================================================

USER_WEIGHTS = {
    "user_id": 150,
    "userid": 150,
    "profile_id": 150,
    "profileid": 150,
    "owner_id": 138,
    "ownerid": 138,
    "publisher_id": 135,
    "publisherid": 135,
    "author_id": 132,
    "authorid": 132,
    "creator_id": 135,
    "creatorid": 135,
    "from_id": 132,
    "fromid": 132,
    "actor_id": 125,
    "actorid": 125,
    "page_owner_id": 140,
}


OBJECT_WEIGHTS = {
    "post_id": 150,
    "postid": 150,
    "story_fbid": 150,
    "story_id": 145,
    "video_id": 150,
    "videoid": 150,
    "reel_id": 150,
    "reelid": 150,
    "photo_id": 150,
    "photoid": 150,
    "media_fbid": 145,
    "album_id": 130,
    "albumid": 130,
    "page_id": 155,
    "pageid": 155,
    "group_id": 155,
    "groupid": 155,
}


def normalize_key(
    key: str,
) -> str:

    return (
        str(key or "")
        .lower()
        .strip()
        .replace(
            "-",
            "_",
        )
        .replace(
            " ",
            "_",
        )
    )


def classify_id_key(
    key: str,
) -> Tuple[
    str,
    float,
]:

    key = normalize_key(
        key
    )

    if key in USER_WEIGHTS:

        return (
            "USER_CANDIDATE",
            USER_WEIGHTS[key],
        )

    if key in OBJECT_WEIGHTS:

        role = "OBJECT"

        if key in {
            "post_id",
            "postid",
            "story_fbid",
            "story_id",
        }:
            role = "POST"

        elif key in {
            "video_id",
            "videoid",
        }:
            role = "VIDEO"

        elif key in {
            "reel_id",
            "reelid",
        }:
            role = "REEL"

        elif key in {
            "photo_id",
            "photoid",
        }:
            role = "PHOTO"

        elif key in {
            "page_id",
            "pageid",
        }:
            role = "PAGE"

        elif key in {
            "group_id",
            "groupid",
        }:
            role = "GROUP"

        return (
            role,
            OBJECT_WEIGHTS[key],
        )

    return (
        "",
        0,
    )


# ======================================================================
# ADD EVIDENCE
# ======================================================================

def add_evidence(
    graph: EvidenceGraph,
    value: Any,
    role: str,
    source: str,
    weight: float,
    *,
    key: str = "",
    path: str = "",
    context: str = "",
    url: str = "",
    verified: bool = False,
    relation: str = "",
):

    value = clean_text(
        value
    )

    if not is_numeric_id(
        value
    ):
        return

    graph.add(
        Evidence(
            value=value,
            role=role,
            source=source,
            weight=weight,
            key=key,
            path=path,
            context=truncate(
                context,
                700,
            ),
            url=url,
            verified=verified,
            relation=relation,
        )
    )


# ======================================================================
# STRUCTURAL EVIDENCE
# ======================================================================

def collect_url_evidence(
    graph: EvidenceGraph,
    shape: URLShape,
    url: str,
):

    if shape.route_entity_id:

        role = "ROUTE_ENTITY"

        if shape.kind == "profile":

            role = "USER_CANDIDATE"

            add_evidence(
                graph,
                shape.route_entity_id,
                role,
                "profile_url",
                170,
                key="profile_id",
                url=url,
                verified=True,
            )

        elif shape.kind == "page":

            add_evidence(
                graph,
                shape.route_entity_id,
                "PAGE",
                "page_url",
                170,
                key="page_id",
                url=url,
                verified=True,
            )

        elif shape.kind in {
            "group",
            "group_post",
        }:

            add_evidence(
                graph,
                shape.route_entity_id,
                "GROUP",
                "group_url",
                170,
                key="group_id",
                url=url,
                verified=True,
            )

        else:

            add_evidence(
                graph,
                shape.route_entity_id,
                role,
                "route_entity",
                20,
                url=url,
            )

    if shape.post_id:

        add_evidence(
            graph,
            shape.post_id,
            "POST",
            "url",
            155,
            key="post_id",
            url=url,
        )

    if shape.video_id:

        add_evidence(
            graph,
            shape.video_id,
            "VIDEO",
            "url",
            155,
            key="video_id",
            url=url,
        )

    if shape.reel_id:

        add_evidence(
            graph,
            shape.reel_id,
            "REEL",
            "url",
            155,
            key="reel_id",
            url=url,
        )

    if shape.photo_id:

        add_evidence(
            graph,
            shape.photo_id,
            "PHOTO",
            "url",
            155,
            key="photo_id",
            url=url,
        )

    if shape.story_id:

        add_evidence(
            graph,
            shape.story_id,
            "POST",
            "url",
            155,
            key="story_id",
            url=url,
        )


# ======================================================================
# HTML SEMANTIC IDS
# ======================================================================

def collect_html_ids(
    graph: EvidenceGraph,
    text: str,
    url: str,
    source: str = "html",
):

    if not text:
        return

    for match in SEMANTIC_ID_RE.finditer(
        text
    ):

        key = normalize_key(
            match.group(
                "key"
            )
        )

        value = match.group(
            "value"
        )

        role, weight = classify_id_key(
            key
        )

        if not role:
            continue

        context = text[
            max(
                0,
                match.start() - 300,
            ):
            min(
                len(text),
                match.end() + 600,
            )
        ]

        add_evidence(
            graph,
            value,
            role,
            source,
            weight,
            key=key,
            path=key,
            context=context,
            url=url,
        )


# ======================================================================
# JSON WALK
# ======================================================================

def walk_json(
    value: Any,
    path: str = "",
):

    if isinstance(
        value,
        dict,
    ):

        for key, child in value.items():

            key = str(key)

            child_path = (
                f"{path}.{key}"
                if path
                else key
            )

            yield (
                child_path,
                key,
                child,
                value,
            )

            yield from walk_json(
                child,
                child_path,
            )

    elif isinstance(
        value,
        list,
    ):

        for index, child in enumerate(
            value
        ):

            child_path = (
                f"{path}[{index}]"
            )

            yield (
                child_path,
                str(index),
                child,
                value,
            )

            yield from walk_json(
                child,
                child_path,
            )


# ======================================================================
# JSON IDS
# ======================================================================

def collect_json_ids(
    graph: EvidenceGraph,
    objects: Iterable[Any],
    source: str,
    url: str,
):

    for obj in objects:

        for (
            path,
            key,
            value,
            parent,
        ) in walk_json(
            obj
        ):

            role, weight = classify_id_key(
                key
            )

            if (
                role
                and is_numeric_id(value)
            ):

                add_evidence(
                    graph,
                    value,
                    role,
                    source,
                    weight,
                    key=normalize_key(key),
                    path=path,
                    context=repr(parent),
                    url=url,
                )


# ======================================================================
# META IDS
# ======================================================================

def collect_meta_ids(
    graph: EvidenceGraph,
    snapshot: Snapshot,
):

    for key, value in (
        snapshot.meta.items()
    ):

        role, weight = classify_id_key(
            key
        )

        if (
            role
            and is_numeric_id(value)
        ):

            add_evidence(
                graph,
                value,
                role,
                "meta",
                weight,
                key=normalize_key(key),
                path=key,
                context=f"{key}={value}",
                url=snapshot.final_url,
            )


# ======================================================================
# PROFILE URL DISCOVERY
# ======================================================================

def discover_profile_urls(
    snapshot: Snapshot,
    original_shape: URLShape,
) -> List[str]:

    result = []

    seen = set()

    def add(
        url: str,
    ):

        normalized = normalize_url(
            url
        )

        if not normalized:
            return

        shape = classify_url(
            normalized
        )

        # ----------------------------------------------------------
        # A pure username URL is a profile.
        # ----------------------------------------------------------

        if shape.kind != "profile":
            return

        if normalized in seen:
            return

        seen.add(
            normalized
        )

        result.append(
            normalized
        )

    # Original URL itself.
    if original_shape.kind == "profile":

        add(
            original_shape.normalized
        )

    # Links.
    for link in snapshot.links:

        add(
            urljoin(
                snapshot.final_url,
                link,
            )
        )

        if len(result) >= MAX_PROFILE_CHECKS:
            return result

    # Canonical.
    if snapshot.canonical:

        canonical_shape = (
            classify_url(
                snapshot.canonical
            )
        )

        # Pure profile.
        if canonical_shape.kind == "profile":

            add(
                snapshot.canonical
            )

        # Content belonging to username.
        elif canonical_shape.username:

            add(
                profile_url_from_shape(
                    canonical_shape
                )
            )

    # Metadata URLs.
    for key in (
        "og:url",
        "al:web:url",
        "twitter:url",
    ):

        value = snapshot.meta.get(
            key
        )

        if value:
            add(value)

    return result[:MAX_PROFILE_CHECKS]


# ======================================================================
# PROFILE IDENTITY
# ======================================================================

@dataclass
class ProfileInfo:

    uid: str = ""

    name: str = ""

    username: str = ""

    profile_url: str = ""

    avatar: str = ""

    bio: str = ""

    entity_type: str = "USER"

    verified: bool = False

    score: float = 0


# ======================================================================
# NAME EXTRACTION
# ======================================================================

GENERIC_NAMES = {
    "facebook",
    "meta",
    "home",
    "login",
    "log in",
    "facebook watch",
    "video",
    "videos",
    "reel",
    "reels",
}


def clean_name(
    value: Any,
) -> str:

    value = clean_text(
        value
    )

    if not value:
        return ""

    value = re.sub(
        r"\s*\|\s*Facebook.*$",
        "",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"\s*-\s*Facebook.*$",
        "",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"\s+on Facebook.*$",
        "",
        value,
        flags=re.I,
    )

    value = re.sub(
        r"'s\s+(reel|video|post|photo).*$",
        "",
        value,
        flags=re.I,
    )

    value = value.strip()

    if value.lower() in GENERIC_NAMES:
        return ""

    return truncate(
        value,
        250,
    )


def extract_names_from_json(
    objects: Iterable[Any],
) -> List[str]:

    candidates = []

    keys = {
        "name",
        "full_name",
        "fullname",
        "display_name",
        "short_name",
        "profile_name",
        "user_name",
    }

    for obj in objects:

        for (
            path,
            key,
            value,
            parent,
        ) in walk_json(
            obj
        ):

            if (
                normalize_key(key)
                not in keys
            ):
                continue

            if not isinstance(
                value,
                str,
            ):
                continue

            value = clean_name(
                value
            )

            if not value:
                continue

            path_lower = (
                path.lower()
            )

            score = 10

            if any(
                x in path_lower
                for x in (
                    "profile",
                    "owner",
                    "author",
                    "creator",
                    "publisher",
                    "actor",
                    "user",
                )
            ):

                score += 30

            if normalize_key(key) in {
                "full_name",
                "fullname",
                "display_name",
            }:

                score += 20

            candidates.append(
                (
                    score,
                    value,
                )
            )

    candidates.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    result = []

    seen = set()

    for _, value in candidates:

        key = value.lower()

        if key in seen:
            continue

        seen.add(
            key
        )

        result.append(
            value
        )

        if len(result) >= 10:
            break

    return result


# ======================================================================
# USERNAME EXTRACTION
# ======================================================================

def extract_username_from_json(
    objects: Iterable[Any],
) -> str:

    keys = {
        "username",
        "user_name",
        "vanity",
        "screen_name",
        "handle",
    }

    candidates = []

    for obj in objects:

        for (
            path,
            key,
            value,
            parent,
        ) in walk_json(
            obj
        ):

            if (
                normalize_key(key)
                not in keys
            ):
                continue

            if not isinstance(
                value,
                str,
            ):
                continue

            value = clean_text(
                value
            )

            if not value:
                continue

            path_lower = (
                path.lower()
            )

            score = 10

            if any(
                x in path_lower
                for x in (
                    "profile",
                    "user",
                    "owner",
                    "author",
                    "creator",
                )
            ):

                score += 30

            candidates.append(
                (
                    score,
                    value,
                )
            )

    if not candidates:
        return ""

    candidates.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return candidates[0][1].lstrip(
        "@"
    )


# ======================================================================
# PROFILE METADATA
# ======================================================================

def extract_profile_metadata(
    snapshot: Snapshot,
    shape: URLShape,
) -> ProfileInfo:

    info = ProfileInfo()

    # --------------------------------------------------------------
    # Username from URL.
    # --------------------------------------------------------------

    if shape.username:

        info.username = (
            shape.username
        )

    # --------------------------------------------------------------
    # JSON username.
    # --------------------------------------------------------------

    if not info.username:

        info.username = (
            extract_username_from_json(
                snapshot.embedded_json
            )
            or extract_username_from_json(
                snapshot.jsonld
            )
        )

    # --------------------------------------------------------------
    # Name.
    # --------------------------------------------------------------

    name_candidates = []

    for key in (
        "og:title",
        "twitter:title",
        "title",
    ):

        value = snapshot.meta.get(
            key
        )

        if value:

            value = clean_name(
                value
            )

            if value:

                name_candidates.append(
                    value
                )

    name_candidates.extend(
        extract_names_from_json(
            snapshot.embedded_json
        )
    )

    name_candidates.extend(
        extract_names_from_json(
            snapshot.jsonld
        )
    )

    if name_candidates:

        info.name = name_candidates[0]

    # --------------------------------------------------------------
    # Bio.
    # --------------------------------------------------------------

    for key in (
        "og:description",
        "twitter:description",
        "description",
    ):

        value = snapshot.meta.get(
            key
        )

        if value:

            info.bio = truncate(
                value,
                500,
            )

            break

    # --------------------------------------------------------------
    # Avatar.
    # --------------------------------------------------------------

    for key in (
        "og:image",
        "twitter:image",
        "twitter:image:src",
    ):

        value = snapshot.meta.get(
            key
        )

        if value:

            info.avatar = value.strip()

            break

    # --------------------------------------------------------------
    # Profile URL.
    # --------------------------------------------------------------

    if shape.kind == "profile":

        info.profile_url = (
            profile_url_from_shape(
                shape
            )
        )

    if not info.profile_url:

        canonical_shape = classify_url(
            snapshot.canonical
        )

        if canonical_shape.kind == "profile":

            info.profile_url = (
                profile_url_from_shape(
                    canonical_shape
                )
            )

        elif canonical_shape.username:

            info.profile_url = (
                profile_url_from_shape(
                    canonical_shape
                )
            )

    return info


# ======================================================================
# IDENTITY SCORE
# ======================================================================

def score_uid_candidates(
    graph: EvidenceGraph,
) -> Dict[str, float]:

    scores = {}

    for uid in graph.values(
        "USER_CANDIDATE"
    ):

        items = graph.for_value(
            uid
        )

        score = 0

        sources = set()

        strong_sources = set()

        for item in items:

            sources.add(
                item.source
            )

            contribution = min(
                170,
                item.weight,
            )

            if item.verified:

                contribution += 100

            score += contribution

            if item.weight >= 120:

                strong_sources.add(
                    item.source
                )

        # Independent-source bonus.
        if len(sources) >= 2:
            score += 50

        if len(sources) >= 3:
            score += 45

        if len(strong_sources) >= 2:
            score += 60

        scores[uid] = score

    return scores


# ======================================================================
# CONFLICT
# ======================================================================

def identity_conflict(
    graph: EvidenceGraph,
) -> bool:

    scores = score_uid_candidates(
        graph
    )

    ranked = sorted(
        scores.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    if len(ranked) < 2:
        return False

    top = ranked[0][1]

    for _, score in ranked[1:]:

        if (
            score >= 250
            and score >= top * 0.80
        ):

            return True

    return False


# ======================================================================
# PROFILE VERIFICATION
# ======================================================================

@dataclass
class Verification:

    uid: str

    verified: bool = False

    score: float = 0

    signals: List[str] = field(
        default_factory=list
    )

    info: ProfileInfo = field(
        default_factory=ProfileInfo
    )


async def verify_profile_identity(
    fetcher,
    profile_url: str,
    expected_uid: str,
) -> Verification:

    result = Verification(
        uid=expected_uid
    )

    snapshot = await fetcher.fetch(
        profile_url
    )

    if not snapshot.html:
        return result

    shape = classify_url(
        profile_url
    )

    graph = EvidenceGraph()

    collect_url_evidence(
        graph,
        shape,
        profile_url,
    )

    collect_html_ids(
        graph,
        snapshot.html,
        snapshot.final_url,
        "profile_html",
    )

    collect_meta_ids(
        graph,
        snapshot,
    )

    collect_json_ids(
        graph,
        snapshot.embedded_json,
        "profile_json",
        snapshot.final_url,
    )

    collect_json_ids(
        graph,
        snapshot.jsonld,
        "profile_jsonld",
        snapshot.final_url,
    )

    info = extract_profile_metadata(
        snapshot,
        shape,
    )

    info.profile_url = (
        profile_url_from_shape(
            shape
        )
        or profile_url
    )

    # --------------------------------------------------------------
    # Exact UID evidence.
    # --------------------------------------------------------------

    exact = [
        x
        for x in graph.for_value(
            expected_uid
        )
        if x.role == "USER_CANDIDATE"
    ]

    sources = {
        x.source
        for x in exact
    }

    strong = [
        x
        for x in exact
        if x.weight >= 120
    ]

    if exact:

        result.score += 70

        result.signals.append(
            "uid_found_in_profile"
        )

    if len(sources) >= 2:

        result.score += 80

        result.signals.append(
            "uid_multiple_sources"
        )

    if len(strong) >= 2:

        result.score += 70

        result.signals.append(
            "uid_multiple_strong_sources"
        )

    # --------------------------------------------------------------
    # Explicit profile.php?id.
    # --------------------------------------------------------------

    if (
        shape.kind == "profile"
        and shape.route_entity_id
        == expected_uid
    ):

        result.score += 90

        result.signals.append(
            "profile_url_exact_uid"
        )

    # --------------------------------------------------------------
    # Canonical exact numeric profile.
    # --------------------------------------------------------------

    canonical_shape = classify_url(
        snapshot.canonical
    )

    if (
        canonical_shape.kind == "profile"
        and canonical_shape.route_entity_id
        == expected_uid
    ):

        result.score += 100

        result.signals.append(
            "canonical_exact_uid"
        )

    # --------------------------------------------------------------
    # Identity decision.
    # --------------------------------------------------------------

    if (
        len(sources) >= 2
        and len(strong) >= 1
    ):

        result.verified = True

    elif (
        "profile_url_exact_uid"
        in result.signals
        and len(strong) >= 1
    ):

        result.verified = True

    elif (
        "canonical_exact_uid"
        in result.signals
        and len(strong) >= 1
    ):

        result.verified = True

    if result.verified:

        info.uid = expected_uid
        info.verified = True
        info.score = result.score

        result.info = info

    return result


# ======================================================================
# FETCH ENGINE
# ======================================================================

class FetchEngine:

    def __init__(
        self,
    ):

        self.session = (
            requests.Session()
        )

        self.session.headers.update(
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

    def fetch_sync(
        self,
        url: str,
        user_agent: str,
    ) -> Snapshot:

        result = Snapshot(
            requested_url=url
        )

        try:

            response = self.session.get(
                url,
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
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                stream=True,
            )

            result.status_code = (
                response.status_code
            )

            result.final_url = (
                normalize_url(
                    response.url
                )
                or response.url
            )

            result.redirects = [
                normalize_url(
                    item.url
                )
                or item.url
                for item in response.history
            ]

            chunks = []

            total = 0

            for chunk in response.iter_content(
                chunk_size=65536
            ):

                if not chunk:
                    continue

                remaining = (
                    MAX_HTML_BYTES
                    - total
                )

                if remaining <= 0:
                    break

                chunk = chunk[
                    :remaining
                ]

                chunks.append(
                    chunk
                )

                total += len(
                    chunk
                )

                if total >= MAX_HTML_BYTES:
                    break

            encoding = (
                response.encoding
                or "utf-8"
            )

            response.close()

            raw = b"".join(
                chunks
            )

            try:

                result.html = raw.decode(
                    encoding,
                    errors="replace",
                )

            except Exception:

                result.html = raw.decode(
                    "utf-8",
                    errors="replace",
                )

            parse_snapshot(
                result
            )

            return result

        except Exception as exc:

            result.error = (
                f"{type(exc).__name__}: {exc}"
            )

            return result

    async def fetch(
        self,
        url: str,
    ) -> Snapshot:

        last = Snapshot(
            requested_url=url
        )

        for agent in USER_AGENTS:

            result = await asyncio.to_thread(
                self.fetch_sync,
                url,
                agent,
            )

            last = result

            if result.html:

                return result

        return last


# ======================================================================
# CONTENT
# ======================================================================

@dataclass
class ContentInfo:

    kind: str = ""

    title: str = ""

    post_id: str = ""

    video_id: str = ""

    reel_id: str = ""

    photo_id: str = ""

    story_id: str = ""

    url: str = ""


# ======================================================================
# RESOLVE RESULT
# ======================================================================

@dataclass
class ResolveResult:

    input_url: str

    kind: str = "UNKNOWN"

    status: str = "NOT_VERIFIED"

    confidence: float = 0

    profile: ProfileInfo = field(
        default_factory=ProfileInfo
    )

    content: ContentInfo = field(
        default_factory=ContentInfo
    )

    evidence: List[Evidence] = field(
        default_factory=list
    )

    elapsed: float = 0

    error: str = ""


# ======================================================================
# CONTENT EXTRACTION
# ======================================================================

def clean_content_url(
    url: str,
) -> str:

    normalized = normalize_url(
        url
    )

    if not normalized:
        return ""

    shape = classify_url(
        normalized
    )

    if shape.kind == "reel" and shape.reel_id:

        return (
            "https://www.facebook.com/reel/"
            + shape.reel_id
        )

    if shape.kind == "video" and shape.video_id:

        return (
            "https://www.facebook.com/watch/?v="
            + shape.video_id
        )

    if shape.kind == "photo" and shape.photo_id:

        return (
            "https://www.facebook.com/photo.php?fbid="
            + shape.photo_id
        )

    if shape.kind == "post" and shape.post_id:

        return (
            "https://www.facebook.com/posts/"
            + shape.post_id
        )

    # --------------------------------------------------------------
    # IMPORTANT:
    # Profile URL must remain profile URL.
    # --------------------------------------------------------------

    if shape.kind == "profile":

        return (
            profile_url_from_shape(
                shape
            )
        )

    parsed = urlparse(
        normalized
    )

    return urlunparse(
        (
            "https",
            parsed.netloc,
            parsed.path,
            "",
            "",
            "",
        )
    )


def best_object(
    graph: EvidenceGraph,
    role: str,
) -> str:

    candidates = {}

    for item in graph.for_role(
        role
    ):

        candidates.setdefault(
            item.value,
            0,
        )

        candidates[
            item.value
        ] += item.weight

    if not candidates:
        return ""

    return max(
        candidates.items(),
        key=lambda x: x[1],
    )[0]


def extract_content(
    shape: URLShape,
    snapshot: Snapshot,
    graph: EvidenceGraph,
) -> ContentInfo:

    info = ContentInfo()

    final_shape = classify_url(
        snapshot.final_url
    )

    # --------------------------------------------------------------
    # For pure profile, there is NO content.
    # --------------------------------------------------------------

    if (
        shape.kind == "profile"
        and not shape.post_id
        and not shape.video_id
        and not shape.reel_id
        and not shape.photo_id
        and not shape.story_id
    ):

        return info

    selected = final_shape

    if selected.kind in {
        "profile",
        "unknown",
    }:

        selected = shape

    mapping = {
        "post": "POST",
        "video": "VIDEO",
        "reel": "REEL",
        "photo": "PHOTO",
        "story": "STORY",
        "group_post": "GROUP_POST",
    }

    info.kind = mapping.get(
        selected.kind,
        "",
    )

    info.post_id = (
        selected.post_id
        or best_object(
            graph,
            "POST",
        )
    )

    info.video_id = (
        selected.video_id
        or best_object(
            graph,
            "VIDEO",
        )
    )

    info.reel_id = (
        selected.reel_id
        or best_object(
            graph,
            "REEL",
        )
    )

    info.photo_id = (
        selected.photo_id
        or best_object(
            graph,
            "PHOTO",
        )
    )

    info.title = clean_name(
        snapshot.meta.get(
            "og:title",
            "",
        )
    )

    if not info.title:

        info.title = clean_name(
            snapshot.title
        )

    if selected.kind == "reel":
        info.url = clean_content_url(
            snapshot.final_url
        )

    elif snapshot.canonical:

        info.url = clean_content_url(
            snapshot.canonical
        )

    else:

        info.url = clean_content_url(
            snapshot.final_url
        )

    return info


# ======================================================================
# MAIN RESOLVER
# ======================================================================

class FacebookResolver:

    def __init__(
        self,
    ):

        self.fetcher = (
            FetchEngine()
        )

    async def resolve(
        self,
        input_url: str,
    ) -> ResolveResult:

        started = time.monotonic()

        normalized = normalize_url(
            input_url
        )

        shape = classify_url(
            normalized
        )

        result = ResolveResult(
            input_url=input_url
        )

        if not normalized:

            result.error = (
                "Invalid Facebook URL"
            )

            result.elapsed = (
                time.monotonic()
                - started
            )

            return result

        graph = EvidenceGraph()

        # ----------------------------------------------------------
        # URL
        # ----------------------------------------------------------

        collect_url_evidence(
            graph,
            shape,
            normalized,
        )

        # ----------------------------------------------------------
        # FETCH
        # ----------------------------------------------------------

        snapshot = await self.fetcher.fetch(
            normalized
        )

        if not snapshot.html:

            result.kind = (
                shape.kind.upper()
            )

            result.error = (
                snapshot.error
                or "No public HTML"
            )

            result.evidence = (
                graph.items
            )

            result.elapsed = (
                time.monotonic()
                - started
            )

            return result

        # ----------------------------------------------------------
        # FINAL URL
        # ----------------------------------------------------------

        final_shape = classify_url(
            snapshot.final_url
        )

        collect_url_evidence(
            graph,
            final_shape,
            snapshot.final_url,
        )

        # ----------------------------------------------------------
        # CANONICAL
        # ----------------------------------------------------------

        if snapshot.canonical:

            canonical_shape = (
                classify_url(
                    snapshot.canonical
                )
            )

            collect_url_evidence(
                graph,
                canonical_shape,
                snapshot.canonical,
            )

        # ----------------------------------------------------------
        # HTML
        # ----------------------------------------------------------

        collect_html_ids(
            graph,
            snapshot.html,
            snapshot.final_url,
            "html_semantic",
        )

        # ----------------------------------------------------------
        # META
        # ----------------------------------------------------------

        collect_meta_ids(
            graph,
            snapshot,
        )

        # ----------------------------------------------------------
        # JSON
        # ----------------------------------------------------------

        collect_json_ids(
            graph,
            snapshot.embedded_json,
            "embedded_json",
            snapshot.final_url,
        )

        collect_json_ids(
            graph,
            snapshot.jsonld,
            "jsonld",
            snapshot.final_url,
        )

        # ----------------------------------------------------------
        # Effective shape
        # ----------------------------------------------------------

        effective_shape = final_shape

        if effective_shape.kind == "unknown":

            effective_shape = shape

        # ----------------------------------------------------------
        # PROFILE FLOW
        # ----------------------------------------------------------

        is_profile = (
            shape.kind == "profile"
            or final_shape.kind == "profile"
        )

        profile_urls = (
            discover_profile_urls(
                snapshot,
                shape,
            )
        )

        # ----------------------------------------------------------
        # For username content URL:
        #
        # /nvlnopro/videos/123
        #
        # derive /nvlnopro
        # ----------------------------------------------------------

        if (
            shape.username
            and not profile_urls
        ):

            derived = (
                profile_url_from_shape(
                    shape
                )
            )

            if derived:

                profile_urls.append(
                    derived
                )

        # ----------------------------------------------------------
        # Identity candidates.
        # ----------------------------------------------------------

        scores = score_uid_candidates(
            graph
        )

        profile = ProfileInfo()

        verified_uid = ""

        confidence = 0

        status = "NOT_VERIFIED"

        # ----------------------------------------------------------
        # If URL explicitly has profile numeric ID,
        # verify that exact ID.
        # ----------------------------------------------------------

        if (
            is_profile
            and shape.route_entity_id
        ):

            exact_profile_url = (
                profile_url_from_shape(
                    shape
                )
            )

            verification = (
                await verify_profile_identity(
                    self.fetcher,
                    exact_profile_url,
                    shape.route_entity_id,
                )
            )

            if verification.verified:

                profile = (
                    verification.info
                )

                verified_uid = (
                    shape.route_entity_id
                )

                confidence = min(
                    99.9,
                    94
                    + (
                        len(
                            verification.signals
                        ) * 0.8
                    ),
                )

                status = "VERIFIED"

        # ----------------------------------------------------------
        # Username profile.
        # ----------------------------------------------------------

        if (
            not verified_uid
            and is_profile
            and profile_urls
        ):

            # Fetch candidate profiles.
            for profile_url in profile_urls[
                :MAX_PROFILE_CHECKS
            ]:

                # Candidate IDs must come from the
                # profile page itself.
                profile_snapshot = (
                    await self.fetcher.fetch(
                        profile_url
                    )
                )

                if not profile_snapshot.html:
                    continue

                profile_graph = (
                    EvidenceGraph()
                )

                collect_html_ids(
                    profile_graph,
                    profile_snapshot.html,
                    profile_snapshot.final_url,
                    "profile_html",
                )

                collect_meta_ids(
                    profile_graph,
                    profile_snapshot,
                )

                collect_json_ids(
                    profile_graph,
                    profile_snapshot.embedded_json,
                    "profile_json",
                    profile_snapshot.final_url,
                )

                collect_json_ids(
                    profile_graph,
                    profile_snapshot.jsonld,
                    "profile_jsonld",
                    profile_snapshot.final_url,
                )

                profile_scores = (
                    score_uid_candidates(
                        profile_graph
                    )
                )

                if not profile_scores:
                    continue

                ranked = sorted(
                    profile_scores.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )

                # --------------------------------------------------
                # Do not accept weak candidate.
                # --------------------------------------------------

                candidate_uid = ranked[0][0]

                candidate_items = (
                    profile_graph.for_value(
                        candidate_uid
                    )
                )

                candidate_sources = {
                    x.source
                    for x in candidate_items
                    if x.role
                    == "USER_CANDIDATE"
                }

                strong_count = sum(
                    1
                    for x in candidate_items
                    if (
                        x.role
                        == "USER_CANDIDATE"
                        and x.weight >= 120
                    )
                )

                # High precision rule.
                if not (
                    len(candidate_sources) >= 2
                    and strong_count >= 1
                ):

                    continue

                profile_shape = (
                    classify_url(
                        profile_url
                    )
                )

                info = extract_profile_metadata(
                    profile_snapshot,
                    profile_shape,
                )

                info.uid = candidate_uid

                info.profile_url = (
                    profile_url_from_shape(
                        profile_shape
                    )
                    or profile_url
                )

                info.verified = True

                info.score = (
                    profile_scores[
                        candidate_uid
                    ]
                )

                profile = info

                verified_uid = candidate_uid

                confidence = min(
                    99.5,
                    91
                    + min(
                        8,
                        len(
                            candidate_sources
                        ) * 2.2,
                    ),
                )

                status = "VERIFIED"

                break

        # ----------------------------------------------------------
        # Content publisher.
        # ----------------------------------------------------------

        if (
            not verified_uid
            and not is_profile
            and scores
        ):

            ranked = sorted(
                scores.items(),
                key=lambda x: x[1],
                reverse=True,
            )

            candidate_uid = ranked[0][0]

            candidate_items = (
                graph.for_value(
                    candidate_uid
                )
            )

            sources = {
                x.source
                for x in candidate_items
                if x.role
                == "USER_CANDIDATE"
            }

            strong = [
                x
                for x in candidate_items
                if (
                    x.role
                    == "USER_CANDIDATE"
                    and x.weight >= 120
                )
            ]

            # Need multiple independent signals.
            if (
                len(sources) >= 2
                and strong
                and not identity_conflict(
                    graph
                )
            ):

                for profile_url in profile_urls:

                    verification = (
                        await verify_profile_identity(
                            self.fetcher,
                            profile_url,
                            candidate_uid,
                        )
                    )

                    if verification.verified:

                        profile = (
                            verification.info
                        )

                        verified_uid = (
                            candidate_uid
                        )

                        confidence = min(
                            99.5,
                            93
                            + min(
                                6,
                                len(
                                    verification.signals
                                ),
                            ),
                        )

                        status = "VERIFIED"

                        break

        # ----------------------------------------------------------
        # If identity not verified:
        # DO NOT expose a guessed UID.
        # ----------------------------------------------------------

        if not verified_uid:

            profile = ProfileInfo()

            confidence = 0

            status = "NOT_VERIFIED"

        # ----------------------------------------------------------
        # Content
        # ----------------------------------------------------------

        content = extract_content(
            shape,
            snapshot,
            graph,
        )

        # ----------------------------------------------------------
        # Result
        # ----------------------------------------------------------

        result.kind = (
            effective_shape.kind.upper()
        )

        result.status = status

        result.confidence = confidence

        result.profile = profile

        result.content = content

        result.evidence = sorted(
            graph.items,
            key=lambda x: (
                x.weight,
                x.verified,
            ),
            reverse=True,
        )

        result.elapsed = (
            time.monotonic()
            - started
        )

        return result


# ======================================================================
# TELEGRAM HTML ESCAPE
# ======================================================================

def tg_escape(
    value: Any,
) -> str:

    return (
        str(value or "")
        .replace(
            "&",
            "&amp;",
        )
        .replace(
            "<",
            "&lt;",
        )
        .replace(
            ">",
            "&gt;",
        )
        .replace(
            '"',
            "&quot;",
        )
    )


# ======================================================================
# DISPLAY
# ======================================================================

def entity_label(
    result: ResolveResult,
) -> str:

    if result.kind in {
        "PROFILE",
    }:

        return "👤 Cá nhân"

    if result.kind in {
        "PAGE",
    }:

        return "📄 Trang"

    if result.kind in {
        "GROUP",
        "GROUP_POST",
    }:

        return "👥 Nhóm"

    if result.profile.uid:

        return "👤 Cá nhân"

    return ""


def content_icon(
    kind: str,
) -> str:

    return {
        "REEL": "🎬",
        "VIDEO": "🎥",
        "POST": "📝",
        "PHOTO": "📷",
        "STORY": "⭕",
    }.get(
        kind,
        "📌",
    )


def format_single_result(
    result: ResolveResult,
    index: Optional[int] = None,
) -> str:

    lines = []

    if index is not None:

        lines.append(
            f"<b>#{index}</b>"
        )

    if result.status == "VERIFIED":

        lines.append(
            "🔎 <b>FACEBOOK RESOLVER</b>"
        )

        lines.append(
            "━━━━━━━━━━━━━━━━━━━━"
        )

        # ----------------------------------------------------------
        # Profile
        # ----------------------------------------------------------

        if result.profile.uid:

            lines.append(
                "👤 <b>TÀI KHOẢN</b>"
            )

            if result.profile.name:

                lines.append(
                    "Tên: "
                    f"<b>"
                    f"{tg_escape(result.profile.name)}"
                    f"</b>"
                )

            if result.profile.username:

                lines.append(
                    "Username: "
                    f"<code>@"
                    f"{tg_escape(result.profile.username)}"
                    f"</code>"
                )

            lines.append(
                "UID: "
                f"<code>"
                f"{tg_escape(result.profile.uid)}"
                f"</code>"
            )

            label = entity_label(
                result
            )

            if label:

                lines.append(
                    "Loại: "
                    f"{label}"
                )

            if result.profile.profile_url:

                lines.append(
                    "Profile: "
                    f"<a href=\""
                    f"{tg_escape(result.profile.profile_url)}"
                    f"\">Mở profile</a>"
                )

            # Avatar only when actually available.
            if result.profile.avatar:

                lines.append(
                    "Avatar: "
                    f"<a href=\""
                    f"{tg_escape(result.profile.avatar)}"
                    f"\">Xem ảnh</a>"
                )

        # ----------------------------------------------------------
        # Content
        # ----------------------------------------------------------

        if result.content.kind:

            lines.append("")

            lines.append(
                f"{content_icon(result.content.kind)} "
                f"<b>NỘI DUNG</b>"
            )

            if result.content.title:

                lines.append(
                    "Tiêu đề: "
                    f"{tg_escape(result.content.title)}"
                )

            if result.content.reel_id:

                lines.append(
                    "Reel ID: "
                    f"<code>"
                    f"{result.content.reel_id}"
                    f"</code>"
                )

            if result.content.post_id:

                lines.append(
                    "Post ID: "
                    f"<code>"
                    f"{result.content.post_id}"
                    f"</code>"
                )

            if result.content.video_id:

                lines.append(
                    "Video ID: "
                    f"<code>"
                    f"{result.content.video_id}"
                    f"</code>"
                )

            if result.content.photo_id:

                lines.append(
                    "Photo ID: "
                    f"<code>"
                    f"{result.content.photo_id}"
                    f"</code>"
                )

            if result.content.url:

                lines.append(
                    "Content: "
                    f"<a href=\""
                    f"{tg_escape(result.content.url)}"
                    f"\">Mở nội dung</a>"
                )

        # ----------------------------------------------------------
        # Verification
        # ----------------------------------------------------------

        lines.append("")

        lines.append(
            "🛡 <b>XÁC MINH</b>"
        )

        lines.append(
            "Trạng thái: "
            "<b>✅ VERIFIED</b>"
        )

        lines.append(
            "Độ tin cậy: "
            f"<b>{result.confidence:.1f}%</b>"
        )

        # ----------------------------------------------------------
        # Compact forensic.
        # ----------------------------------------------------------

        identity_evidence = [
            x
            for x in result.evidence
            if (
                x.role
                == "USER_CANDIDATE"
            )
        ]

        if identity_evidence:

            lines.append("")

            lines.append(
                "🔬 <b>XÁC NHẬN</b>"
            )

            seen = set()

            shown = 0

            for item in identity_evidence:

                key = (
                    item.value,
                    item.source,
                )

                if key in seen:
                    continue

                seen.add(
                    key
                )

                lines.append(
                    "• "
                    f"{item.key or item.role}"
                    " → "
                    f"<code>"
                    f"{item.value}"
                    f"</code>"
                )

                shown += 1

                if shown >= 3:
                    break

        lines.append(
            f"⏱ {result.elapsed:.2f}s"
        )

        return "\n".join(
            lines
        )

    # --------------------------------------------------------------
    # NOT VERIFIED
    # --------------------------------------------------------------

    lines.append(
        "🔎 <b>FACEBOOK RESOLVER</b>"
    )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    # Show useful object information even
    # when UID cannot be verified.

    if result.content.kind:

        lines.append(
            f"{content_icon(result.content.kind)} "
            f"<b>{result.content.kind}</b>"
        )

        if result.content.reel_id:

            lines.append(
                "Reel ID: "
                f"<code>"
                f"{result.content.reel_id}"
                f"</code>"
            )

        if result.content.post_id:

            lines.append(
                "Post ID: "
                f"<code>"
                f"{result.content.post_id}"
                f"</code>"
            )

        if result.content.video_id:

            lines.append(
                "Video ID: "
                f"<code>"
                f"{result.content.video_id}"
                f"</code>"
            )

        if result.content.url:

            lines.append(
                "Content: "
                f"<a href=\""
                f"{tg_escape(result.content.url)}"
                f"\">Mở nội dung</a>"
            )

    elif (
        result.kind == "PROFILE"
    ):

        shape = classify_url(
            result.input_url
        )

        username = shape.username

        if username:

            lines.append(
                "👤 Username: "
                f"<code>@"
                f"{tg_escape(username)}"
                f"</code>"
            )

            lines.append(
                "Profile: "
                f"<a href=\""
                f"{tg_escape(profile_url_from_shape(shape))}"
                f"\">Mở profile</a>"
            )

    lines.append("")

    lines.append(
        "🛡 <b>UID: CHƯA XÁC MINH</b>"
    )

    lines.append(
        "Không hiển thị UID khi "
        "chưa đủ bằng chứng."
    )

    lines.append(
        f"⏱ {result.elapsed:.2f}s"
    )

    return "\n".join(
        lines
    )


# ======================================================================
# MULTI RESULT FORMAT
# ======================================================================

def format_results(
    results: List[ResolveResult],
) -> str:

    if not results:
        return "❌ Không có kết quả."

    # One compact message.
    blocks = []

    for index, result in enumerate(
        results,
        start=1,
    ):

        blocks.append(
            format_single_result(
                result,
                index
                if len(results) > 1
                else None,
            )
        )

    output = "\n\n".join(
        blocks
    )

    # Telegram message limit.
    # Prefer one message.
    if len(output) <= 3900:

        return output

    # If too long, compact harder.
    compact_blocks = []

    for index, result in enumerate(
        results,
        start=1,
    ):

        if result.status == "VERIFIED":

            name = (
                tg_escape(
                    result.profile.name
                )
                if result.profile.name
                else "Không rõ"
            )

            username = (
                "@"
                + tg_escape(
                    result.profile.username
                )
                if result.profile.username
                else ""
            )

            compact_blocks.append(
                (
                    f"<b>#{index}</b> "
                    f"👤 <b>{name}</b>\n"
                    f"{username}\n"
                    f"UID: <code>"
                    f"{result.profile.uid}"
                    f"</code>\n"
                    f"✅ VERIFIED "
                    f"{result.confidence:.1f}%"
                )
            )

        else:

            compact_blocks.append(
                (
                    f"<b>#{index}</b> "
                    "⚠️ NOT VERIFIED"
                )
            )

    return "\n\n".join(
        compact_blocks
    )


# ======================================================================
# TELEGRAM WAIT
# ======================================================================

async def wait_for_next_message(
    client,
    chat_id: int,
    timeout: int = 120,
):

    loop = asyncio.get_running_loop()

    future = loop.create_future()

    async def handler(
        event,
    ):

        if event.chat_id != chat_id:
            return

        text = (
            event.raw_text
            or ""
        )

        if not text.strip():
            return

        if text.startswith("/"):
            return

        if not future.done():

            future.set_result(
                text
            )

    client.add_event_handler(
        handler,
        events.NewMessage(
            chats=chat_id
        ),
    )

    try:

        return await asyncio.wait_for(
            future,
            timeout,
        )

    except asyncio.TimeoutError:

        return None

    finally:

        client.remove_event_handler(
            handler
        )


# ======================================================================
# TELEGRAM RESOLVE
# ======================================================================

async def resolve_message(
    event,
    text: str,
):

    urls = extract_facebook_urls(
        text
    )

    if not urls:

        await event.reply(
            "❌ <b>Không tìm thấy link Facebook.</b>",
            parse_mode="html",
            link_preview=False,
        )

        return

    resolver = FacebookResolver()

    # --------------------------------------------------------------
    # Resolve concurrently.
    # --------------------------------------------------------------

    semaphore = asyncio.Semaphore(
        FETCH_WORKERS
    )

    async def worker(
        url: str,
    ):

        async with semaphore:

            try:

                return await resolver.resolve(
                    url
                )

            except Exception as exc:

                log.exception(
                    "getuidfb resolve error"
                )

                return ResolveResult(
                    input_url=url,
                    error=str(
                        exc
                    ),
                )

    results = await asyncio.gather(
        *[
            worker(url)
            for url in urls
        ]
    )

    output = format_results(
        results
    )

    # --------------------------------------------------------------
    # ONE MESSAGE whenever possible.
    # --------------------------------------------------------------

    if len(output) <= 3900:

        await event.reply(
            output,
            parse_mode="html",
            link_preview=False,
        )

        return

    # Only split when absolutely necessary.
    await event.reply(
        output[:3900],
        parse_mode="html",
        link_preview=False,
    )


# ======================================================================
# /getuidfb
# ======================================================================

async def _handle_getuidfb(
    event,
):

    raw = (
        event.raw_text
        or ""
    )

    parts = raw.split(
        maxsplit=1
    )

    if len(parts) > 1:

        text = parts[1].strip()

        if text:

            await resolve_message(
                event,
                text,
            )

            return

    await event.reply(
        "🔎 <b>FACEBOOK RESOLVER</b>\n\n"
        "Gửi link Facebook công khai.",
        parse_mode="html",
        link_preview=False,
    )

    text = await wait_for_next_message(
        event.client,
        event.chat_id,
        120,
    )

    if not text:

        await event.reply(
            "⌛ Hết thời gian chờ.",
            parse_mode="html",
        )

        return

    await resolve_message(
        event,
        text,
    )


# ======================================================================
# REGISTER
# ======================================================================

def register(
    bot,
    notify_bot=None,
):

    bot.add_event_handler(
        _handle_getuidfb,
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?(?:\s+.*)?$"
        ),
    )

    log.info(
        "Registered commands.getuidfb V32"
    )


# ======================================================================
# COMMAND INFO
# ======================================================================

COMMAND_INFO = {
    "command": "getuidfb",
    "description": (
        "Facebook Precision Identity Resolver"
    ),
    "usage": (
        "/getuidfb [Facebook URL]"
    ),
    "category": "Facebook",
    "public_only": True,
    "http_only": True,
    "login_required": False,
    "cookie_required": False,
    "access_token_required": False,
}


# ======================================================================
# SELF TEST
# ======================================================================

def self_test():

    tests = [
        (
            "https://www.facebook.com/nvlnopro/",
            "profile",
            "nvlnopro",
        ),
        (
            "https://www.facebook.com/profile.php?id=61553239356646",
            "profile",
            "",
        ),
        (
            "https://www.facebook.com/reel/4697823150500072/",
            "reel",
            "",
        ),
        (
            "https://www.facebook.com/61592487939720/videos/1671583863938536/",
            "video",
            "",
        ),
    ]

    print(
        "=" * 70
    )

    print(
        "FACEBOOK RESOLVER V32 SELF TEST"
    )

    print(
        "=" * 70
    )

    for url, expected, username in tests:

        shape = classify_url(
            url
        )

        ok = (
            shape.kind == expected
            and (
                not username
                or shape.username
                == username
            )
        )

        print(
            "PASS" if ok else "FAIL",
            "|",
            shape.kind,
            "|",
            shape.username,
            "|",
            shape.route_entity_id,
            "|",
            url,
        )

    print(
        "=" * 70
    )


# ======================================================================
# MAIN
# ======================================================================

if __name__ == "__main__":

    import sys

    if "--self-test" in sys.argv:

        self_test()

    else:

        print(
            "Facebook Resolver V32"
        )

        print(
            "python commands/getuidfb.py --self-test"
        )