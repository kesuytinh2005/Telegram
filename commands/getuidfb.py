# ============================================================
# commands/getuidfb.py
# FACEBOOK UID / ENTITY RESOLVER V15 ULTRA
# ============================================================

import asyncio
import html
import re

from telethon import events

from core.fb_resolver import (
    FacebookResolver,
    DEFAULT_TIMEOUT,
    DEFAULT_MAX_PAGES,
    DEFAULT_CONCURRENCY,
)
from core.task_manager import replace_user_tasks, track_current_task


COMMAND_INFO = {
    "command": "getuidfb",
    "category": "🔎 FACEBOOK",
    "title": "Facebook UID V15 ULTRA",
    "description": (
        "Phân tích link Facebook công khai bằng resolver V15 ULTRA. "
        "Nhận diện Profile, Page, Group, Post, Reel, Video, Photo, Story và Share."
    ),
    "usage": "/getuidfb",
    "examples": ["/getuidfb"],
    "details": [
        "Gửi /getuidfb rồi gửi một hoặc nhiều link Facebook.",
        "Có thể gửi nhiều URL trong cùng một tin nhắn.",
        "V15 ULTRA dùng HTTP, không dùng Playwright/Selenium/Cookie/Access Token.",
        "pfbid được giữ nguyên nếu không có bằng chứng giải mã hợp lệ.",
        "UID chỉ được đánh dấu VERIFIED khi có bằng chứng nhận diện đủ mạnh.",
        "Dùng /stop để dừng.",
    ],
    "supported": [
        "Profile", "Page", "Post", "Group", "Group Post",
        "Story", "Reel", "Video", "Photo", "Share",
    ],
}

SESSION_KEY = "_dragon_sessions"

FB_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "touch.facebook.com",
    "fb.watch",
    "www.fb.watch",
    "fb.com",
    "www.fb.com",
}


def get_sessions(bot):
    sessions = getattr(bot, SESSION_KEY, None)
    if not isinstance(sessions, dict):
        sessions = {}
        setattr(bot, SESSION_KEY, sessions)
    return sessions


def extract_urls(text):
    """
    Tách URL Facebook kể cả trường hợp:
        url1 url2
        url1
        url2
        url1https://facebook.com/url2
    """
    if not text:
        return []

    matches = list(re.finditer(r"https?://", text, re.I))
    if not matches:
        return []

    urls = []

    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()

        for part in re.split(r"\s+", chunk):
            part = part.strip().rstrip(".,!?;:)]}>\"'")
            if not part:
                continue

            try:
                from urllib.parse import urlparse
                p = urlparse(part)
                host = p.netloc.lower().split(":")[0]
            except Exception:
                continue

            if host not in FB_HOSTS and not host.endswith(".facebook.com"):
                continue

            if part not in urls:
                urls.append(part)

    return urls


def esc(value):
    return html.escape(str(value or ""))


def short_url(url, limit=170):
    url = str(url or "")
    return url if len(url) <= limit else url[:limit - 3] + "..."


def value_or_none(value):
    if value is None or str(value).strip() == "":
        return "—"
    return str(value)


def format_item(item, index):
    url = esc(short_url(item.requested_url))
    uid = item.user_uid
    uid_status = item.user_verification

    lines = [
        f"<b>#{index}</b>  <b>{esc(item.entity_type)}</b>",
        f"🔗 <a href=\"{url}\">Mở link</a>",
        f"🏷️ <b>URL type:</b> <code>{esc(item.url_type)}</code>",
        f"📌 <b>Status:</b> <code>{esc(item.status)}</code>",
    ]

    if uid:
        icon = "✅" if uid_status == "VERIFIED" else "⚠️"
        lines.append(
            f"{icon} <b>USER UID:</b> <code>{esc(uid)}</code> "
            f"(<i>{esc(uid_status)}</i>)"
        )
    else:
        lines.append("🆔 <b>USER UID:</b> <code>NOT VERIFIED</code>")

    fields = [
        ("👤 USERNAME", item.publisher_username),
        ("🏢 PAGE ID", item.page_id),
        ("👥 GROUP ID", item.group_id),
        ("📝 POST ID", item.post_id),
        ("🎬 REEL ID", item.reel_id),
        ("🎥 VIDEO ID", item.video_id),
        ("🖼️ PHOTO ID", item.photo_id),
        ("📖 STORY ID", item.story_id),
        ("📦 MEDIA FBID", item.media_fbid),
        ("🎭 ACTOR ID", item.actor_id),
        ("🔓 DECODED ID", item.decoded_id),
    ]

    for label, value in fields:
        if value:
            lines.append(f"{label}: <code>{esc(value)}</code>")

    if item.decoded_payload:
        lines.append(
            f"🧩 <b>Decoded:</b> <code>{esc(item.decoded_payload)}</code>"
        )

    if item.identity_url:
        lines.append(
            f"🪪 <a href=\"{esc(item.identity_url)}\">Identity URL</a>"
        )

    if item.title:
        lines.append(f"📄 <b>Title:</b> {esc(item.title[:220])}")

    lines.append(f"📈 <b>Confidence:</b> <code>{item.confidence}/100</code>")

    if item.warnings:
        warning = " | ".join(item.warnings[:3])
        lines.append(f"⚠️ {esc(warning[:500])}")

    return "\n".join(lines)


def format_results(results, elapsed):
    total = len(results)
    exact = sum(1 for x in results if x.status == "EXACT")
    partial = sum(1 for x in results if x.status == "PARTIAL")
    unresolved = total - exact - partial
    verified = sum(1 for x in results if x.user_verification == "VERIFIED")

    lines = [
        "╭────────────────────────────────────╮",
        "│   🔎 <b>FACEBOOK UID V15 ULTRA</b>   │",
        "╰────────────────────────────────────╯",
        "",
        (
            f"📊 <b>{total}</b> link  •  "
            f"✅ Exact: <b>{exact}</b>  •  "
            f"🟡 Partial: <b>{partial}</b>  •  "
            f"❌ Unresolved: <b>{unresolved}</b>"
        ),
        f"🆔 <b>Verified UID:</b> {verified}",
        f"⏱️ <b>Thời gian:</b> {elapsed:.2f}s",
        "",
    ]

    for i, item in enumerate(results, 1):
        lines.append(format_item(item, i))
        lines.append("\n━━━━━━━━━━━━━━━━━━━━\n")

    lines.extend([
        "🔄 <b>GET UID SẴN SÀNG</b>",
        "📥 Gửi link Facebook tiếp theo.",
        "💡 Có thể gửi nhiều link cùng lúc.",
        "🛑 <code>/stop</code> để dừng.",
    ])

    return "\n".join(lines)


async def notify_admin(notify_bot, user, results):
    if not notify_bot:
        return

    try:
        verified = [
            r.user_uid for r in results
            if r.user_uid and r.user_verification == "VERIFIED"
        ]
        entity_types = sorted(set(
            r.entity_type for r in results if r.entity_type
        ))

        message = (
            "╭─────────────────────╮\n"
            "│  🔎 <b>GETUIDFB V15</b>  │\n"
            "╰─────────────────────╯\n\n"
            f"📊 Links: {len(results)}\n"
            f"🆔 Verified UID: {', '.join(verified) if verified else 'Không có'}\n"
            f"🏷️ Entity: {', '.join(entity_types) if entity_types else 'UNKNOWN'}"
        )

        await notify_bot(
            user,
            "/getuidfb",
            result=message,
        )
    except Exception as e:
        print(f"[UID ADMIN] {e}")


async def resolve_one(url, resolver):
    return await resolver.resolve(url)


def register(bot, notify_bot):
    sessions = get_sessions(bot)

    @bot.on(events.NewMessage(pattern=r"^/getuidfb(?:@\w+)?$"))
    async def getuid_start(event):
        user_id = event.sender_id

        # Dừng task/command cũ của user trước khi vào getuidfb.
        await replace_user_tasks(user_id)

        for attr in ("_dragon_sessions", "_dragon_download_sessions"):
            old = getattr(bot, attr, None)
            if isinstance(old, dict):
                old.pop(user_id, None)

        try:
            from core.power.session import clear_session
            clear_session(bot, user_id)
        except Exception:
            pass

        sessions[user_id] = {
            "command": "getuidfb",
            "running": True,
            "processing": False,
        }

        await event.reply(
            "╭────────────────────────────╮\n"
            "│  🔎 <b>FACEBOOK UID V15 ULTRA</b>  │\n"
            "╰────────────────────────────╯\n\n"
            "📥 <b>Gửi link Facebook để phân tích.</b>\n\n"
            "🔹 Profile / Page / Group\n"
            "🔹 Post / Group Post / Page Post\n"
            "🔹 Reel / Video / Photo / Story\n"
            "🔹 Share / pfbid\n\n"
            "⚙️ <b>Public HTTP only</b>\n"
            "🚫 Không Cookie • Không Access Token • Không Playwright\n\n"
            "💡 Có thể gửi nhiều link cùng lúc.\n"
            "🛑 <code>/stop</code> → Dừng",
            parse_mode="html",
        )

    @bot.on(events.NewMessage())
    async def getuid_receive(event):
        user_id = event.sender_id
        text = (event.raw_text or "").strip()

        if not text or text.startswith("/"):
            return

        session = sessions.get(user_id)
        if not session or session.get("command") != "getuidfb":
            return
        if not session.get("running", False):
            return
        if session.get("processing", False):
            await event.reply(
                "⏳ <b>Đang xử lý batch trước.</b>\n"
                "🛑 Dùng <code>/stop</code> nếu muốn dừng.",
                parse_mode="html",
            )
            return

        urls = extract_urls(text)
        if not urls:
            await event.reply(
                "❌ <b>Không tìm thấy link Facebook hợp lệ.</b>\n\n"
                "📥 Hãy gửi URL Facebook public.",
                parse_mode="html",
            )
            return

        session["processing"] = True
        track_current_task(user_id)

        progress = await event.reply(
            "╭────────────────────────╮\n"
            "│  🔎 <b>V15 ULTRA SCANNER</b>  │\n"
            "╰────────────────────────╯\n\n"
            f"📊 Đã nhận: <b>{len(urls)}</b> link\n"
            "⚙️ Đang khởi tạo resolver...",
            parse_mode="html",
        )

        results = []
        started = asyncio.get_running_loop().time()

        resolver = FacebookResolver(
            max_pages=DEFAULT_MAX_PAGES,
            concurrency=DEFAULT_CONCURRENCY,
            timeout=DEFAULT_TIMEOUT,
        )

        try:
            for index, url in enumerate(urls, 1):
                session = sessions.get(user_id)
                if not session or not session.get("running", False):
                    try:
                        await progress.edit(
                            "🛑 <b>Đã dừng Get UID.</b>\n\n"
                            "Dùng <code>/getuidfb</code> để bắt đầu lại.",
                            parse_mode="html",
                        )
                    except Exception:
                        pass
                    return

                try:
                    await progress.edit(
                        "╭────────────────────────╮\n"
                        "│  🔎 <b>V15 ULTRA SCANNER</b>  │\n"
                        "╰────────────────────────╯\n\n"
                        f"📊 <b>Tiến trình:</b> {index}/{len(urls)}\n"
                        f"🔗 <code>{esc(short_url(url))}</code>\n\n"
                        "⏳ Đang phân tích HTML / metadata / ID / deep-link...",
                        parse_mode="html",
                    )
                except Exception:
                    pass

                try:
                    result = await resolve_one(url, resolver)
                    results.append(result)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[V15 RESOLVE] {e}")
                    # Import Result only when an unexpected resolver failure occurs.
                    from core.fb_resolver import Result
                    results.append(Result(
                        requested_url=url,
                        status="UNRESOLVED",
                        success=False,
                        warnings=[str(e)],
                    ))

            elapsed = asyncio.get_running_loop().time() - started

            session = sessions.get(user_id)
            if not session or not session.get("running", False):
                return

            message = format_results(results, elapsed)

            # Telegram message limit ~4096 chars. Split safely if necessary.
            if len(message) <= 3900:
                await progress.edit(message, parse_mode="html")
            else:
                # Compact fallback: send each result in chunks, then keep progress as ready.
                await progress.edit(
                    "╭────────────────────────╮\n"
                    "│  🔎 <b>V15 ULTRA COMPLETE</b>  │\n"
                    "╰────────────────────────╯\n\n"
                    f"📊 Đã xử lý <b>{len(results)}</b> link.\n"
                    f"🆔 Verified UID: <b>{sum(1 for r in results if r.user_verification == 'VERIFIED')}</b>",
                    parse_mode="html",
                )

                for i, result in enumerate(results, 1):
                    chunk = format_item(result, i)
                    if len(chunk) > 3900:
                        chunk = chunk[:3850] + "\n…"
                    await event.reply(chunk, parse_mode="html")

                await event.reply(
                    "🔄 <b>GET UID SẴN SÀNG</b>\n"
                    "📥 Gửi link Facebook tiếp theo.\n"
                    "🛑 <code>/stop</code> để dừng.",
                    parse_mode="html",
                )

            try:
                user = await event.get_sender()
                await notify_admin(notify_bot, user, results)
            except Exception as e:
                print(f"[UID NOTIFY] {e}")

        except asyncio.CancelledError:
            raise
        finally:
            current = sessions.get(user_id)
            if current and current.get("command") == "getuidfb":
                current["processing"] = False
                current["running"] = True


__all__ = [
    "COMMAND_INFO",
    "register",
    "extract_urls",
]
