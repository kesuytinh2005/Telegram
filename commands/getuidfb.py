# ============================================================
# commands/getuidfb.py
# ============================================================

import asyncio
import base64
import html
import re
import requests

from urllib.parse import (
    urlparse,
    parse_qs,
    unquote,
    urljoin,
)

from telethon import events


# ============================================================
# COMMAND INFO
# ============================================================

from core.task_manager import replace_user_tasks, track_current_task

COMMAND_INFO = {
    "command": "getuidfb",
    "category": "🔎 FACEBOOK",
    "title": "Facebook UID",

    "description": (
        "Lấy UID / ID Facebook từ profile, page, "
        "post, story, group, reel, video, photo..."
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
        "Page",
        "Post",
        "Story",
        "Group",
        "Reel",
        "Video",
        "Photo",
        "Share link",
        "pfbid",
        "media_fbid",
    ],
}


# ============================================================
# CONFIG
# ============================================================

REQUEST_TIMEOUT = 15

MAX_HTML_SIZE = 12 * 1024 * 1024

MAX_RESULT_TEXT = 3900


# ============================================================
# FACEBOOK HOSTS
# ============================================================

FB_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
    "mobile.facebook.com",

    "fb.com",
    "www.fb.com",

    "fb.watch",
}

FB_REDIRECT_HOSTS = {
    "l.facebook.com",
    "lm.facebook.com",
}

FB_ALL_HOSTS = (
    FB_HOSTS
    | FB_REDIRECT_HOSTS
)


# ============================================================
# HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),

    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,image/webp,"
        "image/apng,*/*;q=0.8"
    ),

    "Accept-Language":
        "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",

    "Cache-Control":
        "no-cache",

    "Pragma":
        "no-cache",

    "Upgrade-Insecure-Requests":
        "1",

    "DNT":
        "1",

    "Sec-Fetch-Site":
        "none",

    "Sec-Fetch-Mode":
        "navigate",

    "Sec-Fetch-User":
        "?1",

    "Sec-Fetch-Dest":
        "document",
}


# ============================================================
# SESSION KEY
# ============================================================

SESSION_KEY = "_dragon_sessions"


# ============================================================
# LẤY SESSION
# ============================================================

def get_sessions(bot):

    if not hasattr(bot, SESSION_KEY):

        setattr(
            bot,
            SESSION_KEY,
            {}
        )

    return getattr(
        bot,
        SESSION_KEY
    )


# ============================================================
# TEXT UTIL
# ============================================================

def clean_text(value):

    if value is None:
        return ""

    value = str(value)

    value = (
        value
        .replace("\x00", " ")
        .replace("\r", " ")
        .replace("\n", " ")
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def unique_list(values):

    result = []

    seen = set()

    for value in values or []:

        if value is None:
            continue

        value = str(value).strip()

        if not value:
            continue

        if value in seen:
            continue

        seen.add(value)

        result.append(value)

    return result


def truncate(value, limit=350):

    value = clean_text(value)

    if len(value) <= limit:
        return value

    return value[:limit - 3] + "..."


def esc(value):

    return html.escape(
        str(value or "")
    )


# ============================================================
# FACEBOOK URL UTIL
# ============================================================

def normalize_host(host):

    host = (
        host or ""
    ).lower().strip()

    if ":" in host:

        host = host.split(
            ":",
            1
        )[0]

    return host


def is_facebook_host(host):

    host = normalize_host(
        host
    )

    return (
        host in FB_ALL_HOSTS
        or host.endswith(".facebook.com")
        or host.endswith(".fb.com")
    )


def strip_url_punctuation(url):

    if not url:
        return ""

    return url.strip().rstrip(
        ".,!?;:)]}>\"'`"
    )


def normalize_url(url):

    url = strip_url_punctuation(
        url
    )

    if not url:
        return ""

    url = unquote(
        url
    )

    url = url.replace(
        " ",
        ""
    )

    return url


def is_http_url(url):

    if not url:
        return False

    try:

        parsed = urlparse(
            url
        )

        return parsed.scheme.lower() in {
            "http",
            "https"
        }

    except Exception:

        return False


# ============================================================
# TÁCH URL FACEBOOK
# ============================================================

def extract_urls(text):
    """
    Parser URL Facebook mạnh hơn code cũ.

    Xử lý được:

    https://facebook.com/a

    https://facebook.com/a https://facebook.com/b

    https://facebook.com/a
    https://facebook.com/b

    https://facebook.com/ahttps://facebook.com/b

    Đồng thời loại bỏ URL không phải Facebook.
    """

    if not text:
        return []

    text = str(text)

    # --------------------------------------------------------
    # Tìm từng URL bắt đầu bằng http:// hoặc https://
    # --------------------------------------------------------

    matches = list(
        re.finditer(
            r"https?://",
            text,
            flags=re.IGNORECASE
        )
    )

    if not matches:
        return []

    urls = []

    for index, match in enumerate(matches):

        start = match.start()

        if index + 1 < len(matches):

            end = matches[
                index + 1
            ].start()

        else:

            end = len(text)

        raw = text[
            start:end
        ]

        # ----------------------------------------------------
        # Một đoạn có thể chứa nhiều URL cách nhau bằng space
        # ----------------------------------------------------

        parts = re.split(
            r"\s+",
            raw.strip()
        )

        for part in parts:

            part = strip_url_punctuation(
                part
            )

            if not part:
                continue

            # ------------------------------------------------
            # Nếu dính dấu ngoặc mở
            # ------------------------------------------------

            part = part.lstrip(
                "([<{\"'"
            )

            if not is_http_url(
                part
            ):
                continue

            try:

                parsed = urlparse(
                    part
                )

                host = normalize_host(
                    parsed.netloc
                )

            except Exception:

                continue

            if not is_facebook_host(
                host
            ):
                continue

            if part not in urls:

                urls.append(
                    part
                )

    return urls


# ============================================================
# URL TYPE
# ============================================================

def classify_url(url):

    try:

        parsed = urlparse(
            url
        )

        path = (
            parsed.path
            or ""
        ).strip("/").lower()

        parts = [
            unquote(x)
            for x in path.split("/")
            if x
        ]

        if not parts:
            return "PROFILE"

        first = parts[0]

        # ----------------------------------------------------
        # PROFILE / PAGE
        # ----------------------------------------------------

        if first in {
            "pages",
            "page",
        }:

            return "PAGE"

        # ----------------------------------------------------
        # GROUP
        # ----------------------------------------------------

        if first in {
            "groups",
            "group",
        }:

            return "GROUP"

        # ----------------------------------------------------
        # POST
        # ----------------------------------------------------

        if first in {
            "p",
            "posts",
            "post",
        }:

            return "POST"

        # ----------------------------------------------------
        # REEL
        # ----------------------------------------------------

        if first in {
            "reel",
            "reels",
        }:

            return "REEL"

        # ----------------------------------------------------
        # VIDEO
        # ----------------------------------------------------

        if first in {
            "video",
            "videos",
        }:

            return "VIDEO"

        # ----------------------------------------------------
        # PHOTO
        # ----------------------------------------------------

        if first in {
            "photo",
            "photos",
        }:

            return "PHOTO"

        # ----------------------------------------------------
        # STORY
        # ----------------------------------------------------

        if first in {
            "story",
            "stories",
        }:

            return "STORY"

        # ----------------------------------------------------
        # SHARE
        # ----------------------------------------------------

        if first in {
            "share",
            "sharer",
        }:

            if len(parts) >= 2:

                second = parts[1]

                if second == "p":
                    return "POST"

                if second == "v":
                    return "VIDEO"

                if second == "r":
                    return "REEL"

            return "SHARE"

        # ----------------------------------------------------
        # WATCH
        # ----------------------------------------------------

        if first == "watch":

            return "VIDEO"

        # ----------------------------------------------------
        # STORY.PHP
        # ----------------------------------------------------

        if first == "story.php":

            return "STORY"

        # ----------------------------------------------------
        # USERNAME CONTENT
        # ----------------------------------------------------

        if len(parts) >= 2:

            second = parts[1]

            if second == "posts":
                return "POST"

            if second == "videos":
                return "VIDEO"

            if second == "reels":
                return "REEL"

            if second == "photos":
                return "PHOTO"

        # ----------------------------------------------------
        # DEFAULT = PROFILE
        # ----------------------------------------------------

        return "PROFILE"

    except Exception:

        return "UNKNOWN"


# ============================================================
# EXTRACT PATH TOKEN
# ============================================================

def path_parts(url):

    try:

        parsed = urlparse(
            url
        )

        return [
            unquote(x)
            for x in parsed.path.strip(
                "/"
            ).split("/")
            if x
        ]

    except Exception:

        return []


# ============================================================
# NUMERIC ID
# ============================================================

def is_numeric_id(value):

    if value is None:
        return False

    value = str(
        value
    ).strip()

    return bool(
        re.fullmatch(
            r"\d{5,30}",
            value
        )
    )


# ============================================================
# PFBID
# ============================================================

def looks_like_pfbid(value):

    if not value:
        return False

    return bool(
        re.fullmatch(
            r"pfbid[A-Za-z0-9_-]+",
            str(value),
            re.IGNORECASE
        )
    )


# ============================================================
# FACEBOOK BASE64 ID
# ============================================================

def decode_fb_encoded_id(token):

    if not token:
        return None

    token = str(
        token
    ).strip()

    candidates = [
        token,
        token.replace(
            "-",
            "+"
        ).replace(
            "_",
            "/"
        ),
    ]

    for candidate in unique_list(
        candidates
    ):

        try:

            padding = (
                "="
                * (
                    -len(candidate)
                    % 4
                )
            )

            decoded = base64.b64decode(
                candidate + padding,
                validate=False
            ).decode(
                "utf-8",
                errors="ignore"
            )

            if not decoded:
                continue

            # ------------------------------------------------
            # Facebook dạng:
            #
            # S:_I123456:987654
            # ------------------------------------------------

            numeric = re.findall(
                r"(?<!\d)\d{5,30}(?!\d)",
                decoded
            )

            if numeric:

                return {
                    "decoded_text": decoded,
                    "decoded_id": numeric[-1],
                }

            return {
                "decoded_text": decoded,
                "decoded_id": None,
            }

        except Exception:

            continue

    return None


# ============================================================
# FIND IDs THEO KEY
# ============================================================

def find_ids(
    html_text,
    keys
):

    if not html_text:
        return []

    results = []

    for key in keys:

        # ----------------------------------------------------
        # key dạng literal
        # ----------------------------------------------------

        if key.endswith("/"):

            pattern = (
                re.escape(key)
                + r"([0-9A-Za-z_-]{3,100})"
            )

        else:

            pattern = (
                r'"'
                + re.escape(key)
                + r'"'
                r"\s*:\s*"
                r'"?'
                r"([^\",}\s]+)"
                r'"?'
            )

        try:

            matches = re.findall(
                pattern,
                html_text,
                flags=re.IGNORECASE
            )

        except Exception:

            continue

        for value in matches:

            value = str(
                value
            ).strip()

            value = value.strip(
                "\"'"
            )

            if not value:
                continue

            if value == "0":
                continue

            if value not in results:

                results.append(
                    value
                )

    return results


# ============================================================
# FIND NUMERIC IDs NEAR KEY
# ============================================================

def find_numeric_near(
    html_text,
    patterns,
    radius=250
):

    results = []

    if not html_text:
        return results

    for pattern in patterns:

        try:

            matches = list(
                re.finditer(
                    pattern,
                    html_text,
                    flags=re.IGNORECASE
                )
            )

        except Exception:

            continue

        for match in matches:

            start = max(
                0,
                match.start() - radius
            )

            end = min(
                len(html_text),
                match.end() + radius
            )

            chunk = html_text[
                start:end
            ]

            numbers = re.findall(
                r"(?<!\d)\d{5,30}(?!\d)",
                chunk
            )

            for number in numbers:

                if number not in results:

                    results.append(
                        number
                    )

    return results


# ============================================================
# META VALUE
# ============================================================

def extract_meta(
    html_text,
    key
):

    if not html_text:
        return None

    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)',
        rf'<meta[^>]+name=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(key)}["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(key)}["\']',
    ]

    for pattern in patterns:

        try:

            match = re.search(
                pattern,
                html_text,
                flags=re.IGNORECASE
            )

        except Exception:

            continue

        if match:

            return clean_text(
                html.unescape(
                    match.group(1)
                )
            )

    return None


# ============================================================
# TITLE
# ============================================================

def extract_title(html_text):

    if not html_text:
        return ""

    match = re.search(
        r"<title[^>]*>(.*?)</title>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL
    )

    if not match:
        return ""

    return clean_text(
        html.unescape(
            re.sub(
                r"<[^>]+>",
                " ",
                match.group(1)
            )
        )
    )


# ============================================================
# CANONICAL
# ============================================================

def extract_canonical(html_text):

    if not html_text:
        return None

    patterns = [
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)',
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            html_text,
            flags=re.IGNORECASE
        )

        if match:

            return html.unescape(
                match.group(1)
            ).strip()

    return None


# ============================================================
# USERNAME
# ============================================================

def extract_username(
    html_text,
    url
):

    candidates = []

    # --------------------------------------------------------
    # profile:username
    # --------------------------------------------------------

    for key in [
        "profile:username",
        "username",
        "userName",
        "vanity",
    ]:

        value = extract_meta(
            html_text,
            key
        )

        if value:

            candidates.append(
                value
            )

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    parts = path_parts(
        url
    )

    if parts:

        first = parts[0]

        reserved = {
            "p",
            "posts",
            "post",
            "reel",
            "reels",
            "video",
            "videos",
            "photo",
            "photos",
            "story",
            "stories",
            "groups",
            "group",
            "pages",
            "page",
            "share",
            "sharer",
            "watch",
            "marketplace",
            "events",
            "gaming",
            "messages",
            "notifications",
        }

        if first.lower() not in reserved:

            if (
                not is_numeric_id(first)
                and not looks_like_pfbid(first)
            ):

                candidates.append(
                    first
                )

    for value in candidates:

        value = str(
            value
        ).strip()

        value = value.lstrip(
            "@"
        )

        if (
            value
            and len(value) <= 200
            and not value.startswith("http")
        ):

            return value

    return None


# ============================================================
# PARSE URL IDs
# ============================================================

def parse_url_ids(url):

    result = {
        "url_type": classify_url(url),

        "username": None,

        "profile_id": None,
        "user_id": None,

        "page_id": None,
        "group_id": None,

        "post_id": None,
        "video_id": None,
        "reel_id": None,
        "photo_id": None,
        "story_id": None,

        "media_fbid": None,
        "actor_id": None,
        "entity_id": None,

        "decoded_id": None,
    }

    try:

        parsed = urlparse(
            url
        )

        query = parse_qs(
            parsed.query
        )

        parts = path_parts(
            url
        )

        lower_parts = [
            x.lower()
            for x in parts
        ]

        # ----------------------------------------------------
        # QUERY IDs
        # ----------------------------------------------------

        def qfirst(*names):

            for name in names:

                values = query.get(
                    name
                )

                if values:

                    value = unquote(
                        values[0]
                    ).strip()

                    if value:
                        return value

            return None

        result["profile_id"] = qfirst(
            "profile_id"
        )

        result["user_id"] = qfirst(
            "user_id"
        )

        result["page_id"] = qfirst(
            "page_id"
        )

        result["group_id"] = qfirst(
            "group_id"
        )

        result["post_id"] = qfirst(
            "post_id",
            "story_fbid",
            "top_level_post_id",
        )

        result["video_id"] = qfirst(
            "video_id"
        )

        result["reel_id"] = qfirst(
            "reel_id"
        )

        result["photo_id"] = qfirst(
            "photo_id"
        )

        result["story_id"] = qfirst(
            "story_id"
        )

        result["media_fbid"] = qfirst(
            "media_fbid"
        )

        result["actor_id"] = qfirst(
            "actor_id"
        )

        result["entity_id"] = qfirst(
            "entity_id"
        )

        # ----------------------------------------------------
        # FBID
        # ----------------------------------------------------

        fbid = qfirst(
            "fbid"
        )

        if fbid:

            if is_numeric_id(fbid):

                result["entity_id"] = fbid

            elif looks_like_pfbid(fbid):

                result["entity_id"] = fbid

        # ----------------------------------------------------
        # GROUP
        # ----------------------------------------------------

        if lower_parts:

            if lower_parts[0] in {
                "groups",
                "group",
            }:

                if len(parts) >= 2:

                    token = parts[1]

                    if (
                        is_numeric_id(token)
                        or token
                    ):

                        result["group_id"] = (
                            token
                        )

        # ----------------------------------------------------
        # PAGE
        # ----------------------------------------------------

        if lower_parts:

            if lower_parts[0] in {
                "pages",
                "page",
            }:

                if len(parts) >= 2:

                    token = parts[1]

                    if token:

                        result["page_id"] = (
                            token
                        )

        # ----------------------------------------------------
        # /p/POST_ID
        # ----------------------------------------------------

        if (
            len(parts) >= 2
            and lower_parts[0] == "p"
        ):

            token = parts[1]

            result["post_id"] = token

        # ----------------------------------------------------
        # /posts/POST_ID
        # ----------------------------------------------------

        if (
            len(parts) >= 2
            and lower_parts[0] in {
                "posts",
                "post",
            }
        ):

            result["post_id"] = parts[1]

        # ----------------------------------------------------
        # /username/posts/ID
        # ----------------------------------------------------

        if (
            len(parts) >= 3
            and lower_parts[1] == "posts"
        ):

            result["username"] = parts[0]

            result["post_id"] = parts[2]

        # ----------------------------------------------------
        # /username/videos/ID
        # ----------------------------------------------------

        if (
            len(parts) >= 3
            and lower_parts[1] == "videos"
        ):

            result["username"] = parts[0]

            result["video_id"] = parts[2]

        # ----------------------------------------------------
        # /username/reels/ID
        # ----------------------------------------------------

        if (
            len(parts) >= 3
            and lower_parts[1] in {
                "reels",
                "reel",
            }
        ):

            result["username"] = parts[0]

            result["reel_id"] = parts[2]

        # ----------------------------------------------------
        # /username/photos/ID
        # ----------------------------------------------------

        if (
            len(parts) >= 3
            and lower_parts[1] in {
                "photos",
                "photo",
            }
        ):

            result["username"] = parts[0]

            result["photo_id"] = parts[2]

        # ----------------------------------------------------
        # /reel/ID
        # ----------------------------------------------------

        if (
            len(parts) >= 2
            and lower_parts[0] in {
                "reel",
                "reels",
            }
        ):

            result["reel_id"] = parts[1]

        # ----------------------------------------------------
        # /video/ID
        # ----------------------------------------------------

        if (
            len(parts) >= 2
            and lower_parts[0] in {
                "video",
                "videos",
            }
        ):

            result["video_id"] = parts[1]

        # ----------------------------------------------------
        # /photo/ID
        # ----------------------------------------------------

        if (
            len(parts) >= 2
            and lower_parts[0] in {
                "photo",
                "photos",
            }
        ):

            result["photo_id"] = parts[1]

        # ----------------------------------------------------
        # /story/ID
        # ----------------------------------------------------

        if (
            len(parts) >= 2
            and lower_parts[0] == "story"
        ):

            result["story_id"] = parts[1]

        # ----------------------------------------------------
        # /stories/ACTOR/STORY
        # ----------------------------------------------------

        if (
            len(parts) >= 3
            and lower_parts[0] == "stories"
        ):

            result["actor_id"] = parts[1]

            result["story_id"] = parts[2]

        # ----------------------------------------------------
        # SHARE/P/TOKEN
        # ----------------------------------------------------

        if (
            len(parts) >= 3
            and lower_parts[0] in {
                "share",
                "sharer",
            }
        ):

            kind = lower_parts[1]

            token = parts[2]

            if kind == "p":

                result["post_id"] = token

            elif kind == "v":

                result["video_id"] = token

            elif kind == "r":

                result["reel_id"] = token

        # ----------------------------------------------------
        # SHARE/TOKEN
        # ----------------------------------------------------

        elif (
            len(parts) >= 2
            and lower_parts[0] == "share"
        ):

            token = parts[1]

            result["entity_id"] = token

        # ----------------------------------------------------
        # WATCH
        # ----------------------------------------------------

        if (
            lower_parts
            and lower_parts[0] == "watch"
        ):

            video_id = qfirst(
                "v",
                "video_id",
            )

            if video_id:

                result["video_id"] = video_id

        # ----------------------------------------------------
        # USERNAME QUERY
        # ----------------------------------------------------

        query_username = qfirst(
            "username",
            "profile",
            "profile_name",
        )

        if query_username:

            result["username"] = (
                query_username
            )

        # ----------------------------------------------------
        # MEDIA FBID
        # ----------------------------------------------------

        if result["media_fbid"]:

            decoded = decode_fb_encoded_id(
                result["media_fbid"]
            )

            if decoded:

                result["decoded_id"] = (
                    decoded.get(
                        "decoded_id"
                    )
                )

        # ----------------------------------------------------
        # USERNAME PROFILE
        # ----------------------------------------------------

        if not result["username"]:

            result["username"] = (
                parse_username_from_url(
                    url
                )
            )

    except Exception:

        pass

    return result


# ============================================================
# USERNAME FROM URL
# ============================================================

def parse_username_from_url(url):

    parts = path_parts(
        url
    )

    if not parts:
        return None

    first = parts[0]

    reserved = {
        "p",
        "posts",
        "post",
        "reel",
        "reels",
        "video",
        "videos",
        "photo",
        "photos",
        "story",
        "stories",
        "groups",
        "group",
        "pages",
        "page",
        "share",
        "sharer",
        "watch",
        "events",
        "marketplace",
        "gaming",
        "messages",
        "notifications",
        "settings",
        "login",
        "logout",
        "help",
    }

    if first.lower() in reserved:
        return None

    if is_numeric_id(first):
        return None

    if looks_like_pfbid(first):
        return None

    return first


# ============================================================
# CONTENT ID EXTRACTION FROM HTML
# ============================================================

def extract_html_ids(
    html_text,
    url_type
):

    result = {
        "user_ids": [],
        "profile_ids": [],
        "page_ids": [],
        "group_ids": [],
        "post_ids": [],
        "video_ids": [],
        "reel_ids": [],
        "photo_ids": [],
        "story_ids": [],
        "media_fbid": [],
        "actor_ids": [],
        "entity_ids": [],
    }

    if not html_text:
        return result

    # --------------------------------------------------------
    # Exact key patterns
    # --------------------------------------------------------

    mapping = {
        "user_ids": [
            "user_id",
            "userID",
            "userId",
        ],

        "profile_ids": [
            "profile_id",
            "profileID",
            "profileId",
        ],

        "page_ids": [
            "page_id",
            "pageID",
            "pageId",
        ],

        "group_ids": [
            "group_id",
            "groupID",
            "groupId",
        ],

        "post_ids": [
            "post_id",
            "postID",
            "postId",
            "story_fbid",
            "top_level_post_id",
            "subscription_target_id",
            "share_fbid",
            "mf_story_key",
            "tl_objid",
            "throwback_story_fbid",
        ],

        "video_ids": [
            "video_id",
            "videoID",
            "videoId",
        ],

        "reel_ids": [
            "reel_id",
            "reelID",
            "reelId",
        ],

        "photo_ids": [
            "photo_id",
            "photoID",
            "photoId",
        ],

        "story_ids": [
            "story_id",
            "storyID",
            "storyId",
        ],

        "media_fbid": [
            "media_fbid",
        ],

        "actor_ids": [
            "actor_id",
            "actorID",
            "actorId",
        ],

        "entity_ids": [
            "entity_id",
            "entityID",
            "entityId",
        ],
    }

    for field_name, keys in mapping.items():

        result[field_name].extend(
            find_ids(
                html_text,
                keys
            )
        )

    # --------------------------------------------------------
    # Deep links
    # --------------------------------------------------------

    deep_patterns = {
        "user_ids": [
            r"fb://profile/",
        ],

        "profile_ids": [
            r"fb://profile/",
        ],

        "page_ids": [
            r"fb://page/",
        ],

        "group_ids": [
            r"fb://group/",
        ],

        "post_ids": [
            r"fb://post/",
        ],

        "entity_ids": [
            r"fb://object/",
        ],
    }

    for field_name, patterns in deep_patterns.items():

        for pattern in patterns:

            try:

                matches = re.findall(
                    re.escape(pattern)
                    + r"([0-9]{5,30})",
                    html_text,
                    flags=re.IGNORECASE
                )

            except Exception:

                continue

            result[field_name].extend(
                matches
            )

    # --------------------------------------------------------
    # URL embedded IDs
    # --------------------------------------------------------

    url_patterns = {
        "post_ids": [
            r"/(?:p|posts|post)/([0-9]{5,30})",
            r"/[^/\s]+/posts/([0-9]{5,30})",
        ],

        "video_ids": [
            r"/(?:video|videos)/([0-9]{5,30})",
            r"/[^/\s]+/videos/([0-9]{5,30})",
        ],

        "reel_ids": [
            r"/(?:reel|reels)/([0-9]{5,30})",
            r"/[^/\s]+/reels?/([0-9]{5,30})",
        ],

        "photo_ids": [
            r"/(?:photo|photos)/([0-9]{5,30})",
            r"/[^/\s]+/photos/([0-9]{5,30})",
        ],

        "story_ids": [
            r"/stories/[^/\s]+/([0-9]{5,30})",
        ],
    }

    for field_name, patterns in url_patterns.items():

        for pattern in patterns:

            try:

                matches = re.findall(
                    pattern,
                    html_text,
                    flags=re.IGNORECASE
                )

            except Exception:

                continue

            result[field_name].extend(
                matches
            )

    # --------------------------------------------------------
    # Clean
    # --------------------------------------------------------

    for key in result:

        result[key] = unique_list(
            result[key]
        )

    return result


# ============================================================
# REQUEST FACEBOOK
# ============================================================

def request_facebook(
    url
):

    last_error = None

    for attempt in range(3):

        try:

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                stream=True,
            )

            chunks = []

            size = 0

            for chunk in response.iter_content(
                chunk_size=65536
            ):

                if not chunk:
                    continue

                size += len(chunk)

                if size > MAX_HTML_SIZE:

                    remaining = (
                        MAX_HTML_SIZE
                        - (
                            size
                            - len(chunk)
                        )
                    )

                    if remaining > 0:

                        chunks.append(
                            chunk[:remaining]
                        )

                    break

                chunks.append(
                    chunk
                )

            content = b"".join(
                chunks
            )

            encoding = (
                response.encoding
                or "utf-8"
            )

            try:

                html_text = content.decode(
                    encoding,
                    errors="ignore"
                )

            except Exception:

                html_text = content.decode(
                    "utf-8",
                    errors="ignore"
                )

            return {
                "success": True,
                "status_code": response.status_code,
                "html": html_text,
                "final_url": response.url,
                "headers": dict(
                    response.headers
                ),
            }

        except (
            requests.Timeout,
            requests.ConnectionError,
        ) as e:

            last_error = e

            # ------------------------------------------------
            # Backoff nhẹ
            # ------------------------------------------------

            if attempt < 2:

                await_time = (
                    0.35
                    * (attempt + 1)
                )

                # Không block event loop
                import time

                time.sleep(
                    await_time
                )

        except Exception as e:

            last_error = e

            break

    return {
        "success": False,
        "status_code": 0,
        "html": "",
        "final_url": url,
        "headers": {},
        "error": str(
            last_error
            or "HTTP request failed"
        ),
    }


# ============================================================
# ASYNC REQUEST
# ============================================================

async def fetch_facebook(
    url
):

    return await asyncio.to_thread(
        request_facebook,
        url
    )


# ============================================================
# REDIRECT UNWRAP
# ============================================================

def unwrap_facebook_redirect(
    url
):

    try:

        parsed = urlparse(
            url
        )

        host = normalize_host(
            parsed.netloc
        )

        if host not in FB_REDIRECT_HOSTS:

            return url

        query = parse_qs(
            parsed.query
        )

        for key in [
            "u",
            "url",
            "target",
            "redirect",
        ]:

            values = query.get(
                key
            )

            if values:

                target = unquote(
                    values[0]
                )

                if target.startswith(
                    "http"
                ):

                    return target

    except Exception:

        pass

    return url


# ============================================================
# STORY ID
# ============================================================

def extract_story_id(
    url,
    html_text=""
):

    # --------------------------------------------------------
    # URL trước
    # --------------------------------------------------------

    try:

        parts = path_parts(
            url
        )

        lower = [
            x.lower()
            for x in parts
        ]

        if (
            len(parts) >= 3
            and lower[0] == "stories"
        ):

            actor = parts[1]

            story = parts[2]

            if is_numeric_id(story):

                return story

            decoded = decode_fb_encoded_id(
                story
            )

            if decoded and decoded.get(
                "decoded_id"
            ):

                return decoded[
                    "decoded_id"
                ]

            # Giữ token nếu Facebook dùng ID
            if story:

                return story

    except Exception:

        pass

    # --------------------------------------------------------
    # HTML
    # --------------------------------------------------------

    if html_text:

        ids = find_ids(
            html_text,
            [
                "story_id",
                "storyID",
                "storyId",
                "story_fbid",
            ]
        )

        for value in ids:

            if value:

                return value

        deep = re.findall(
            r"fb://story/([0-9]{5,30})",
            html_text,
            flags=re.IGNORECASE
        )

        if deep:

            return deep[0]

    return None


# ============================================================
# ENTITY DETECTION
# ============================================================

def detect_entity(
    url_data,
    html_ids,
    final_url,
    html_text
):

    url_type = (
        url_data.get(
            "url_type"
        )
        or "UNKNOWN"
    )

    # --------------------------------------------------------
    # URL type có độ ưu tiên cao hơn actor_id.
    #
    # Đây là điểm sửa lỗi chính:
    #
    # POST không được biến thành USER chỉ vì có actor_id.
    # --------------------------------------------------------

    if url_type == "GROUP":

        return (
            "GROUP",
            first_valid(
                url_data.get("group_id"),
                first_item(
                    html_ids.get(
                        "group_ids"
                    )
                ),
            )
        )

    if url_type == "PAGE":

        return (
            "PAGE",
            first_valid(
                url_data.get("page_id"),
                first_item(
                    html_ids.get(
                        "page_ids"
                    )
                ),
            )
        )

    if url_type == "POST":

        return (
            "POST",
            first_valid(
                url_data.get("post_id"),
                first_item(
                    html_ids.get(
                        "post_ids"
                    )
                ),
                url_data.get("entity_id"),
            )
        )

    if url_type == "REEL":

        return (
            "REEL",
            first_valid(
                url_data.get("reel_id"),
                first_item(
                    html_ids.get(
                        "reel_ids"
                    )
                ),
                url_data.get("entity_id"),
            )
        )

    if url_type == "VIDEO":

        return (
            "VIDEO",
            first_valid(
                url_data.get("video_id"),
                first_item(
                    html_ids.get(
                        "video_ids"
                    )
                ),
                url_data.get("entity_id"),
            )
        )

    if url_type == "PHOTO":

        return (
            "PHOTO",
            first_valid(
                url_data.get("photo_id"),
                first_item(
                    html_ids.get(
                        "photo_ids"
                    )
                ),
                url_data.get("media_fbid"),
                url_data.get("entity_id"),
            )
        )

    if url_type == "STORY":

        return (
            "STORY",
            first_valid(
                url_data.get("story_id"),
                first_item(
                    html_ids.get(
                        "story_ids"
                    )
                ),
            )
        )

    # --------------------------------------------------------
    # PROFILE
    # --------------------------------------------------------

    if url_type == "PROFILE":

        uid = first_valid(
            url_data.get("profile_id"),
            url_data.get("user_id"),
            first_item(
                html_ids.get(
                    "profile_ids"
                )
            ),
            first_item(
                html_ids.get(
                    "user_ids"
                )
            ),
        )

        return (
            "USER",
            uid
        )

    # --------------------------------------------------------
    # SHARE
    # --------------------------------------------------------

    if url_type == "SHARE":

        value = first_valid(
            url_data.get("entity_id"),
            url_data.get("media_fbid"),
        )

        if value:

            return (
                "OBJECT",
                value
            )

    return (
        "UNKNOWN",
        first_valid(
            url_data.get("entity_id")
        )
    )


# ============================================================
# FIRST VALID
# ============================================================

def first_valid(*values):

    for value in values:

        if value is None:
            continue

        value = str(
            value
        ).strip()

        if not value:
            continue

        if value == "0":
            continue

        return value

    return None


def first_item(values):

    if not values:
        return None

    for value in values:

        if value:

            return value

    return None


# ============================================================
# GET UID / ENTITY
#
# Không reply message.
# Chỉ trả result.
# ============================================================

async def get_uid(
    event,
    url,
    notify_bot=None
):

    user = await event.get_sender()

    original_url = (
        url
    )

    url = normalize_url(
        url
    )

    url = unwrap_facebook_redirect(
        url
    )

    result = {
        "url": original_url,

        "resolved_url": url,

        "url_type": "UNKNOWN",

        "entity_type": "UNKNOWN",

        "entity_id": None,

        "uid": None,

        "user_uid": None,

        "page_id": None,

        "group_id": None,

        "post_id": None,

        "video_id": None,

        "reel_id": None,

        "photo_id": None,

        "story_id": None,

        "media_fbid": None,

        "actor_id": None,

        "publisher_id": None,

        "publisher_username": None,

        "title": "",

        "success": False,

        "verified": False,

        "confidence": 0,

    }

    try:

        # ====================================================
        # PARSE URL
        # ====================================================

        url_data = parse_url_ids(
            url
        )

        result["url_type"] = (
            url_data.get(
                "url_type"
            )
        )

        result["page_id"] = (
            url_data.get(
                "page_id"
            )
        )

        result["group_id"] = (
            url_data.get(
                "group_id"
            )
        )

        result["post_id"] = (
            url_data.get(
                "post_id"
            )
        )

        result["video_id"] = (
            url_data.get(
                "video_id"
            )
        )

        result["reel_id"] = (
            url_data.get(
                "reel_id"
            )
        )

        result["photo_id"] = (
            url_data.get(
                "photo_id"
            )
        )

        result["story_id"] = (
            url_data.get(
                "story_id"
            )
        )

        result["media_fbid"] = (
            url_data.get(
                "media_fbid"
            )
        )

        result["actor_id"] = (
            url_data.get(
                "actor_id"
            )
        )

        # ====================================================
        # HTTP
        # ====================================================

        response = await fetch_facebook(
            url
        )

        if not response.get(
            "success"
        ):

            result["error"] = (
                response.get(
                    "error"
                )
                or "Facebook request failed"
            )

            await notify_uid_result(
                notify_bot,
                user,
                result
            )

            return result

        html_text = (
            response.get(
                "html"
            )
            or ""
        )

        final_url = (
            response.get(
                "final_url"
            )
            or url
        )

        status_code = (
            response.get(
                "status_code"
            )
            or 0
        )

        result["resolved_url"] = (
            final_url
        )

        # ====================================================
        # Nếu redirect sang URL mới
        # parse lại URL
        # ====================================================

        final_data = parse_url_ids(
            final_url
        )

        # ----------------------------------------------------
        # Chỉ bổ sung, không ghi đè ID đã có
        # ----------------------------------------------------

        for key in [
            "page_id",
            "group_id",
            "post_id",
            "video_id",
            "reel_id",
            "photo_id",
            "story_id",
            "media_fbid",
            "actor_id",
        ]:

            if not result.get(key):

                result[key] = (
                    final_data.get(
                        key
                    )
                )

        # ====================================================
        # META
        # ====================================================

        result["title"] = (
            extract_meta(
                html_text,
                "og:title"
            )
            or extract_title(
                html_text
            )
        )

        result["publisher_username"] = (
            extract_username(
                html_text,
                final_url
            )
            or final_data.get(
                "username"
            )
        )

        # ====================================================
        # FB PROFILE ID META
        # ====================================================

        fb_profile_id = extract_meta(
            html_text,
            "fb:profile_id"
        )

        if (
            fb_profile_id
            and is_numeric_id(
                fb_profile_id
            )
        ):

            if not result.get(
                "user_uid"
            ):

                result["user_uid"] = (
                    fb_profile_id
                )

        # ====================================================
        # HTML IDs
        # ====================================================

        html_ids = extract_html_ids(
            html_text,
            result["url_type"]
        )

        # ====================================================
        # FILL CONTENT IDs
        # ====================================================

        for field, html_field in [
            (
                "page_id",
                "page_ids"
            ),
            (
                "group_id",
                "group_ids"
            ),
            (
                "post_id",
                "post_ids"
            ),
            (
                "video_id",
                "video_ids"
            ),
            (
                "reel_id",
                "reel_ids"
            ),
            (
                "photo_id",
                "photo_ids"
            ),
            (
                "story_id",
                "story_ids"
            ),
            (
                "media_fbid",
                "media_fbid"
            ),
            (
                "actor_id",
                "actor_ids"
            ),
        ]:

            if result.get(
                field
            ):

                continue

            values = html_ids.get(
                html_field
            )

            if isinstance(
                values,
                list
            ):

                result[field] = (
                    first_item(values)
                )

            else:

                result[field] = (
                    first_valid(values)
                )

        # ====================================================
        # STORY SPECIAL
        # ====================================================

        if result["url_type"] == "STORY":

            story_id = extract_story_id(
                final_url,
                html_text
            )

            if story_id:

                result["story_id"] = (
                    story_id
                )

        # ====================================================
        # ENTITY
        # ====================================================

        entity_type, entity_id = detect_entity(
            url_data,
            html_ids,
            final_url,
            html_text
        )

        result["entity_type"] = (
            entity_type
        )

        result["entity_id"] = (
            entity_id
        )

        # ====================================================
        # ENTITY ID THEO CONTENT
        # ====================================================

        if not result["entity_id"]:

            result["entity_id"] = first_valid(
                result.get("post_id")
                if result["url_type"] == "POST"
                else None,

                result.get("reel_id")
                if result["url_type"] == "REEL"
                else None,

                result.get("video_id")
                if result["url_type"] == "VIDEO"
                else None,

                result.get("photo_id")
                if result["url_type"] == "PHOTO"
                else None,

                result.get("story_id")
                if result["url_type"] == "STORY"
                else None,

                result.get("page_id")
                if result["url_type"] == "PAGE"
                else None,

                result.get("group_id")
                if result["url_type"] == "GROUP"
                else None,
            )

        # ====================================================
        # USER UID
        #
        # CỰC KỲ QUAN TRỌNG:
        #
        # Không dùng actor_id ở content làm USER UID.
        #
        # Chỉ profile/page/group có ID identity riêng mới
        # được xem là UID / publisher identity.
        # ====================================================

        if result["url_type"] == "PROFILE":

            result["user_uid"] = first_valid(
                result.get(
                    "user_uid"
                ),

                final_data.get(
                    "profile_id"
                ),

                final_data.get(
                    "user_id"
                ),

                first_item(
                    html_ids.get(
                        "profile_ids"
                    )
                ),

                first_item(
                    html_ids.get(
                        "user_ids"
                    )
                ),
            )

        elif result["url_type"] == "PAGE":

            result["page_id"] = first_valid(
                result.get(
                    "page_id"
                ),

                first_item(
                    html_ids.get(
                        "page_ids"
                    )
                )
            )

        elif result["url_type"] == "GROUP":

            result["group_id"] = first_valid(
                result.get(
                    "group_id"
                ),

                first_item(
                    html_ids.get(
                        "group_ids"
                    )
                )
            )

        # ====================================================
        # PUBLISHER
        # ====================================================

        if result["url_type"] == "PAGE":

            result["publisher_id"] = (
                result.get(
                    "page_id"
                )
            )

        elif result["url_type"] == "GROUP":

            result["publisher_id"] = (
                result.get(
                    "group_id"
                )
            )

        elif result["url_type"] == "PROFILE":

            result["publisher_id"] = (
                result.get(
                    "user_uid"
                )
            )

        else:

            # ------------------------------------------------
            # Content:
            # actor_id = publisher/author
            #
            # Không biến actor_id thành user_uid.
            # ------------------------------------------------

            result["publisher_id"] = (
                result.get(
                    "actor_id"
                )
                or first_item(
                    html_ids.get(
                        "actor_ids"
                    )
                )
            )

        # ====================================================
        # VERIFICATION
        # ====================================================

        if result["url_type"] == "PROFILE":

            uid = result.get(
                "user_uid"
            )

            if (
                uid
                and is_numeric_id(uid)
            ):

                result["verified"] = True

        elif result["url_type"] == "PAGE":

            if result.get(
                "page_id"
            ):

                result["verified"] = (
                    is_numeric_id(
                        result["page_id"]
                    )
                )

        elif result["url_type"] == "GROUP":

            if result.get(
                "group_id"
            ):

                result["verified"] = (
                    is_numeric_id(
                        result["group_id"]
                    )
                )

        elif result["entity_id"]:

            result["verified"] = (
                is_numeric_id(
                    result["entity_id"]
                )
                or looks_like_pfbid(
                    result["entity_id"]
                )
            )

        # ====================================================
        # SUCCESS
        # ====================================================

        if result["url_type"] == "PROFILE":

            if result.get(
                "user_uid"
            ):

                result["success"] = True

        elif result["url_type"] == "PAGE":

            if result.get(
                "page_id"
            ):

                result["success"] = True

        elif result["url_type"] == "GROUP":

            if result.get(
                "group_id"
            ):

                result["success"] = True

        elif result.get(
            "entity_id"
        ):

            result["success"] = True

        # ====================================================
        # CONFIDENCE
        # ====================================================

        confidence = 0

        if result["success"]:
            confidence += 50

        if result["verified"]:
            confidence += 30

        if result.get(
            "publisher_id"
        ):
            confidence += 10

        if result.get(
            "publisher_username"
        ):
            confidence += 5

        if result.get(
            "title"
        ):
            confidence += 5

        result["confidence"] = min(
            confidence,
            100
        )

        # ====================================================
        # HTTP STATUS
        # ====================================================

        result["http_status"] = (
            status_code
        )

        if status_code in {
            401,
            403,
        }:

            result["warning"] = (
                "Facebook hạn chế truy cập nội dung công khai."
            )

        elif status_code == 404:

            result["warning"] = (
                "Facebook trả về 404."
            )

        elif status_code == 429:

            result["warning"] = (
                "Facebook đang giới hạn request."
            )

        # ====================================================
        # ERROR
        # ====================================================

        if not result["success"]:

            result["error"] = (
                result.get(
                    "warning"
                )
                or "Không tìm thấy ID phù hợp."
            )

        # ====================================================
        # ADMIN
        # ====================================================

        await notify_uid_result(
            notify_bot,
            user,
            result
        )

        return result

    except Exception as e:

        print(
            f"[GETUID ERROR] {e}"
        )

        result["error"] = str(
            e
        )

        await notify_uid_result(
            notify_bot,
            user,
            result
        )

        return result


# ============================================================
# ADMIN NOTIFY
# ============================================================

async def notify_uid_result(
    notify_bot,
    user,
    result
):

    if not notify_bot:
        return

    try:

        entity_type = (
            result.get(
                "entity_type"
            )
            or "UNKNOWN"
        )

        entity_id = (
            result.get(
                "entity_id"
            )
        )

        if result.get(
            "success"
        ):

            admin_result = (
                f"TYPE: {entity_type}\n"
                f"ID: {entity_id}\n"
                f"UID: {result.get('user_uid')}\n"
                f"PAGE: {result.get('page_id')}\n"
                f"GROUP: {result.get('group_id')}\n"
                f"ACTOR: {result.get('actor_id')}\n"
                f"Link: {result.get('url')}"
            )

        else:

            admin_result = (
                f"TYPE: {entity_type}\n"
                f"ID: Không tìm thấy\n"
                f"Link: {result.get('url')}\n"
                f"Error: {result.get('error', '')}"
            )

        await notify_bot(
            user,
            "/getuidfb",
            result=admin_result
        )

    except Exception as e:

        print(
            f"[UID ADMIN] {e}"
        )


# ============================================================
# CLEAN FACEBOOK UID
# ============================================================

def clean_facebook_uid(value):

    if value is None:
        return None

    value = str(
        value
    ).strip()

    if not value:
        return None

    # --------------------------------------------------------
    # Chỉ chấp nhận UID số.
    # --------------------------------------------------------

    if not value.isdigit():

        match = re.fullmatch(
            r"\D*(\d{5,30})\D*",
            value
        )

        if not match:
            return None

        value = match.group(
            1
        )

    if not value.isdigit():
        return None

    if not 5 <= len(value) <= 30:
        return None

    return value


# ============================================================
# FORMAT RESULT
# ============================================================

def format_results(
    results
):

    lines = []

    lines.append(
        "╭────────────────────────╮"
    )

    lines.append(
        "│  🔎 <b>FACEBOOK RESOLVER</b>  │"
    )

    lines.append(
        "╰────────────────────────╯"
    )

    lines.append("")

    total = len(
        results
    )

    success = sum(
        1
        for item in results
        if item.get(
            "success"
        )
    )

    failed = total - success

    lines.append(
        f"📊 Tổng: <b>{total}</b>  "
        f"✅ {success}  ❌ {failed}"
    )

    lines.append("")

    for index, item in enumerate(
        results,
        start=1
    ):

        url = esc(
            item.get(
                "url",
                ""
            )
        )

        entity_type = esc(
            item.get(
                "entity_type",
                "UNKNOWN"
            )
        )

        url_type = esc(
            item.get(
                "url_type",
                "UNKNOWN"
            )
        )

        entity_id = item.get(
            "entity_id"
        )

        user_uid = item.get(
            "user_uid"
        )

        page_id = item.get(
            "page_id"
        )

        group_id = item.get(
            "group_id"
        )

        post_id = item.get(
            "post_id"
        )

        video_id = item.get(
            "video_id"
        )

        reel_id = item.get(
            "reel_id"
        )

        photo_id = item.get(
            "photo_id"
        )

        story_id = item.get(
            "story_id"
        )

        actor_id = item.get(
            "actor_id"
        )

        username = item.get(
            "publisher_username"
        )

        title = truncate(
            item.get(
                "title"
            ),
            250
        )

        lines.append(
            f"<b>{index}. {entity_type}</b>"
        )

        lines.append(
            f"🔗 <a href=\"{url}\">Mở Facebook</a>"
        )

        lines.append(
            f"📌 URL type: <code>{url_type}</code>"
        )

        # ----------------------------------------------------
        # USER
        # ----------------------------------------------------

        if user_uid:

            uid = clean_facebook_uid(
                user_uid
            )

            if uid:

                verified = (
                    "✅ Đã xác minh"
                    if item.get(
                        "verified"
                    )
                    else "⚠️ Chưa xác minh"
                )

                lines.append(
                    f"🆔 <b>USER UID:</b> "
                    f"<code>{esc(uid)}</code>"
                )

                lines.append(
                    f"🔐 {verified}"
                )

        # ----------------------------------------------------
        # ENTITY ID
        # ----------------------------------------------------

        if entity_id:

            lines.append(
                f"🎯 <b>ENTITY ID:</b> "
                f"<code>{esc(entity_id)}</code>"
            )

        # ----------------------------------------------------
        # PAGE
        # ----------------------------------------------------

        if page_id:

            lines.append(
                f"📄 <b>PAGE ID:</b> "
                f"<code>{esc(page_id)}</code>"
            )

        # ----------------------------------------------------
        # GROUP
        # ----------------------------------------------------

        if group_id:

            lines.append(
                f"👥 <b>GROUP ID:</b> "
                f"<code>{esc(group_id)}</code>"
            )

        # ----------------------------------------------------
        # POST
        # ----------------------------------------------------

        if post_id:

            lines.append(
                f"📝 <b>POST ID:</b> "
                f"<code>{esc(post_id)}</code>"
            )

        # ----------------------------------------------------
        # VIDEO
        # ----------------------------------------------------

        if video_id:

            lines.append(
                f"🎬 <b>VIDEO ID:</b> "
                f"<code>{esc(video_id)}</code>"
            )

        # ----------------------------------------------------
        # REEL
        # ----------------------------------------------------

        if reel_id:

            lines.append(
                f"🎞 <b>REEL ID:</b> "
                f"<code>{esc(reel_id)}</code>"
            )

        # ----------------------------------------------------
        # PHOTO
        # ----------------------------------------------------

        if photo_id:

            lines.append(
                f"🖼 <b>PHOTO ID:</b> "
                f"<code>{esc(photo_id)}</code>"
            )

        # ----------------------------------------------------
        # STORY
        # ----------------------------------------------------

        if story_id:

            lines.append(
                f"⭕ <b>STORY ID:</b> "
                f"<code>{esc(story_id)}</code>"
            )

        # ----------------------------------------------------
        # ACTOR
        # ----------------------------------------------------

        if actor_id:

            lines.append(
                f"👤 <b>ACTOR ID:</b> "
                f"<code>{esc(actor_id)}</code>"
            )

        # ----------------------------------------------------
        # USERNAME
        # ----------------------------------------------------

        if username:

            lines.append(
                f"🏷 <b>USERNAME:</b> "
                f"<code>{esc(username)}</code>"
            )

        # ----------------------------------------------------
        # TITLE
        # ----------------------------------------------------

        if title:

            lines.append(
                "📝 <b>TITLE:</b> "
                + esc(title)
            )

        # ----------------------------------------------------
        # CONFIDENCE
        # ----------------------------------------------------

        confidence = item.get(
            "confidence",
            0
        )

        lines.append(
            f"📈 <b>Confidence:</b> "
            f"<code>{confidence}%</code>"
        )

        # ----------------------------------------------------
        # SUCCESS / ERROR
        # ----------------------------------------------------

        if item.get(
            "success"
        ):

            lines.append(
                "✅ <i>Thành công</i>"
            )

        else:

            lines.append(
                "❌ <b>Không tìm thấy ID</b>"
            )

            error = item.get(
                "error"
            )

            if error:

                lines.append(
                    "⚠️ <code>"
                    + esc(
                        truncate(
                            error,
                            400
                        )
                    )
                    + "</code>"
                )

        lines.append("")

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        "🔄 <b>GET UID SẴN SÀNG</b>"
    )

    lines.append(
        "📥 Gửi link Facebook tiếp theo."
    )

    lines.append(
        "💡 Có thể gửi nhiều link cùng lúc."
    )

    lines.append(
        "🛑 <code>/stop</code> để dừng."
    )

    text = "\n".join(
        lines
    )

    # --------------------------------------------------------
    # Telegram giới hạn message.
    # --------------------------------------------------------

    if len(text) <= MAX_RESULT_TEXT:

        return text

    # --------------------------------------------------------
    # Cắt an toàn
    # --------------------------------------------------------

    return (
        text[
            :MAX_RESULT_TEXT - 50
        ]
        + "\n\n"
        "⚠️ <i>Kết quả quá dài, "
        "đã rút gọn.</i>"
    )


# ============================================================
# REGISTER
# ============================================================

def register(
    bot,
    notify_bot
):

    sessions = get_sessions(
        bot
    )

    # ========================================================
    # /getuidfb
    # ========================================================

    @bot.on(
        events.NewMessage(
            pattern=r"^/getuidfb(?:@\w+)?$"
        )
    )
    async def getuid_start(event):

        user_id = event.sender_id

        # ----------------------------------------------------
        # Command mới thay thế toàn bộ task nền cũ
        # ----------------------------------------------------

        try:

            await replace_user_tasks(
                user_id
            )

        except Exception as e:

            print(
                f"[TASK REPLACE] {e}"
            )

        # ----------------------------------------------------
        # Xóa session cũ
        # ----------------------------------------------------

        for _attr in (
            "_dragon_sessions",
            "_dragon_download_sessions",
        ):

            _sessions = getattr(
                bot,
                _attr,
                None
            )

            if isinstance(
                _sessions,
                dict
            ):

                _sessions.pop(
                    user_id,
                    None
                )

        # ----------------------------------------------------
        # Clear power session
        # ----------------------------------------------------

        try:

            from core.power.session import clear_session

            clear_session(
                bot,
                user_id
            )

        except Exception:

            pass

        # ----------------------------------------------------
        # Session RIÊNG user
        # ----------------------------------------------------

        sessions[user_id] = {
            "command": "getuidfb",
            "running": True,
            "processing": False,
        }

        await event.reply(
            "╭─────────────────────╮\n"
            "│  🔎 <b>FACEBOOK UID</b>  │\n"
            "╰─────────────────────╯\n\n"

            "📥 <b>Vui lòng gửi link Facebook.</b>\n\n"

            "🔹 1 link → Get 1\n"
            "🔹 2 link → Get 2\n"
            "🔹 Nhiều link → Get lần lượt\n"
            "🔹 Không cần xuống dòng\n\n"

            "💡 Ví dụ:\n"
            "<code>"
            "https://facebook.com/a "
            "https://facebook.com/b "
            "https://facebook.com/c"
            "</code>\n\n"

            "🔄 Sau khi xong bot tiếp tục chờ link.\n"
            "🛑 <b>/stop</b> → Dừng",

            parse_mode="html"
        )

    # ========================================================
    # NHẬN LINK
    # ========================================================

    @bot.on(
        events.NewMessage()
    )
    async def getuid_receive(event):

        user_id = event.sender_id

        text = (
            event.raw_text
            or ""
        ).strip()

        if not text:
            return

        # ----------------------------------------------------
        # Không bắt command
        # ----------------------------------------------------

        if text.startswith("/"):
            return

        # ----------------------------------------------------
        # Session user
        # ----------------------------------------------------

        session = sessions.get(
            user_id
        )

        if not session:
            return

        # ----------------------------------------------------
        # Không phải getuidfb
        # ----------------------------------------------------

        if session.get(
            "command"
        ) != "getuidfb":

            return

        # ----------------------------------------------------
        # Session đã stop
        # ----------------------------------------------------

        if not session.get(
            "running",
            False
        ):

            return

        # ----------------------------------------------------
        # Đang xử lý
        # ----------------------------------------------------

        if session.get(
            "processing",
            False
        ):

            return

        # ----------------------------------------------------
        # Extract URLs
        # ----------------------------------------------------

        urls = extract_urls(
            text
        )

        if not urls:

            await event.reply(
                "❌ <b>Không tìm thấy link Facebook.</b>\n\n"
                "📥 Gửi một hoặc nhiều link.\n"
                "💡 Không cần xuống dòng.\n\n"
                "🔄 Bot vẫn đang chờ link.",
                parse_mode="html"
            )

            return

        # ----------------------------------------------------
        # Processing
        # ----------------------------------------------------

        session["processing"] = True

        try:

            track_current_task(
                user_id
            )

        except Exception as e:

            print(
                f"[TASK TRACK] {e}"
            )

        total = len(
            urls
        )

        # ----------------------------------------------------
        # Progress message
        # ----------------------------------------------------

        progress = await event.reply(
            "╭─────────────────────╮\n"
            "│  🔎 <b>GET FACEBOOK UID</b>  │\n"
            "╰─────────────────────╯\n\n"

            f"📊 <b>Đã nhận:</b> {total} link\n"
            "⚙️ <b>Đang xử lý...</b>",

            parse_mode="html"
        )

        results = []

        # ====================================================
        # XỬ LÝ TỪNG LINK
        # ====================================================

        for index, url in enumerate(
            urls,
            start=1
        ):

            # ------------------------------------------------
            # Session mới nhất
            # ------------------------------------------------

            session = sessions.get(
                user_id
            )

            if not session:
                return

            # ------------------------------------------------
            # STOP
            # ------------------------------------------------

            if not session.get(
                "running",
                False
            ):

                try:

                    await progress.edit(
                        "🛑 <b>Đã dừng Get UID.</b>\n\n"
                        "Dùng <code>/getuidfb</code> "
                        "để bắt đầu lại.",

                        parse_mode="html"
                    )

                except Exception:

                    pass

                return

            # ------------------------------------------------
            # Command khác thay thế
            # ------------------------------------------------

            if session.get(
                "command"
            ) != "getuidfb":

                return

            # ------------------------------------------------
            # Progress
            # ------------------------------------------------

            try:

                await progress.edit(
                    "╭─────────────────────╮\n"
                    "│  🔎 <b>GET FACEBOOK UID</b>  │\n"
                    "╰─────────────────────╯\n\n"

                    f"📊 <b>Tiến trình:</b> "
                    f"{index}/{total}\n\n"

                    f"🔗 <code>{esc(url)}</code>\n\n"

                    "⏳ Đang phân tích Facebook...",

                    parse_mode="html"
                )

            except Exception:

                pass

            # ------------------------------------------------
            # Get UID / Entity
            # ------------------------------------------------

            try:

                result = await get_uid(
                    event,
                    url,
                    notify_bot
                )

                # ------------------------------------------------
                # Chỉ làm sạch UID thật sự.
                #
                # Không lấy entity_id/content_id đưa vào uid.
                # ------------------------------------------------

                if result.get(
                    "user_uid"
                ):

                    clean_uid = (
                        clean_facebook_uid(
                            result.get(
                                "user_uid"
                            )
                        )
                    )

                    if clean_uid:

                        result["user_uid"] = (
                            clean_uid
                        )

                    else:

                        result["user_uid"] = (
                            None
                        )

                # ------------------------------------------------
                # Compatibility với code cũ:
                #
                # Profile => uid
                # Content => uid = None
                #
                # Như vậy code ngoài nếu đang đọc result["uid"]
                # vẫn hoạt động.
                # ------------------------------------------------

                if (
                    result.get(
                        "entity_type"
                    ) == "USER"
                ):

                    result["uid"] = (
                        result.get(
                            "user_uid"
                        )
                    )

                else:

                    result["uid"] = None

                results.append(
                    result
                )

            except Exception as e:

                print(
                    f"[GETUID LOOP] {e}"
                )

                results.append(
                    {
                        "url": url,
                        "uid": None,
                        "user_uid": None,
                        "success": False,
                        "entity_type": "UNKNOWN",
                        "url_type": "UNKNOWN",
                        "error": str(e),
                    }
                )

        # ====================================================
        # KIỂM TRA SESSION
        # ====================================================

        session = sessions.get(
            user_id
        )

        if not session:
            return

        if not session.get(
            "running",
            False
        ):

            return

        # ====================================================
        # HIỂN THỊ
        # ====================================================

        result_message = format_results(
            results
        )

        try:

            await progress.edit(
                result_message,
                parse_mode="html",
                link_preview=False
            )

        except Exception as e:

            print(
                f"[RESULT EDIT] {e}"
            )

            # ------------------------------------------------
            # Fallback
            # ------------------------------------------------

            try:

                await event.reply(
                    result_message,
                    parse_mode="html",
                    link_preview=False
                )

            except Exception as fallback_error:

                print(
                    f"[RESULT FALLBACK] "
                    f"{fallback_error}"
                )

        # ----------------------------------------------------
        # Cho phép batch mới
        # ----------------------------------------------------

        session["processing"] = False

        session["running"] = True

        session["command"] = "getuidfb"