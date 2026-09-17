from __future__ import annotations

import asyncio
import html
import re
from math import ceil

from telethon import Button, events

from core.fbmonitor import (
    STATUS_DIE,
    STATUS_LIVE,
    STATUS_UNKNOWN,
    UID_RE,
    TIME_RE,
    FacebookMonitorService,
    get_service,
)

PAGE_SIZE = 8
_PENDING: dict[int, str] = {}

COMMAND_INFO = {
    "command": "fbwatch",
    "category": "🛰️ FACEBOOK UID",
    "title": "Theo dõi UID Facebook",

    "description": (
        "Kiểm tra trạng thái LIVE/DIE của Facebook UID "
        "và tự động theo dõi thay đổi trạng thái trong nền."
    ),

    "usage": "/fbwatch",

    "examples": [
        "/fbwatch",
        "/fbadd 1000123456789",
        "/fblist",
        "/fbcheck 1",
        "/fbremove 1",
    ],

    "details": [
        "Gửi /fbwatch để mở bảng điều khiển Facebook UID Monitor.",
        "Thêm UID Facebook vào danh sách theo dõi.",
        "Kiểm tra LIVE/DIE thủ công bất cứ lúc nào.",
        "Tự động gọi Facebook Graph API để cập nhật trạng thái.",
        "Khi UID LIVE → DIE sẽ tự động thông báo.",
        "Khi UID DIE → LIVE sẽ tự động thông báo.",
        "Có thể thiết lập chu kỳ kiểm tra riêng cho từng UID.",
        "Có thể thiết lập ngày và giờ gửi báo cáo định kỳ.",
        "Monitor chạy nền độc lập và không chặn các command khác.",
        "Có thể theo dõi nhiều UID cùng lúc.",
        "Dữ liệu UID và cấu hình được lưu trong database.",
        "API timeout/lỗi mạng được xử lý riêng và không kết luận DIE giả.",
        "Trạng thái LIVE/DIE được phân tích từ dữ liệu API, không dựa vào URL ảnh.",
    ],

    "supported": [
        "Facebook UID",
        "LIVE",
        "DIE",
        "UNKNOWN",
        "Theo dõi nền",
        "Thông báo thay đổi trạng thái",
        "Báo cáo định kỳ",
        "SQLite Database",
        "Facebook Graph API",
    ],
}
def _status(status):
    return {
        STATUS_LIVE: "🟢 LIVE",
        STATUS_DIE: "🔴 DIE",
        STATUS_UNKNOWN: "🟡 UNKNOWN",
        None: "⚪ CHƯA CHECK",
    }.get(status, "⚪ CHƯA CHECK")


def _escape(v):
    return html.escape(str(v), quote=True)


def _home(service: FacebookMonitorService, user_id: int):
    rows = service.db.list_user(user_id)
    s = service.db.stats(user_id)
    monitor = "🟢 ĐANG CHẠY NỀN" if service._started else "🟡 ĐANG KHỞI ĐỘNG"
    return (
        "╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "│   <b>🛰 FACEBOOK UID MONITOR</b>   │\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"{monitor}\n"
        "Không chặn các command khác của bot.\n\n"
        "<b>📊 TỔNG QUAN</b>\n"
        f"├ 🟢 LIVE: <b>{s['live']}</b>\n"
        f"├ 🔴 DIE: <b>{s['die']}</b>\n"
        f"├ 🟡 UNKNOWN: <b>{s['unknown']}</b>\n"
        f"└ 📡 Theo dõi: <b>{s['enabled']}/{s['total']}</b>\n\n"
        "<i>Scheduler riêng • connection pool • retry/backoff • không dùng URL để kết luận LIVE/DIE.</i>"
    )


def _menu():
    return [
        [Button.inline("➕ Thêm UID", b"fbw:add"), Button.inline("📋 Danh sách", b"fbw:list:0")],
        [Button.inline("⚡ Check tất cả", b"fbw:checkall"), Button.inline("🔔 Thông báo", b"fbw:notify")],
        [Button.inline("⏱ Chu kỳ", b"fbw:interval"), Button.inline("📅 Lịch", b"fbw:schedule")],
        [Button.inline("🧹 Quản lý", b"fbw:manage:0"), Button.inline("📘 Hướng dẫn", b"fbw:help")],
    ]


def _back():
    return [[Button.inline("🏠 Trang chủ", b"fbw:home")]]


def _list_view(rows, page=0):
    total_pages = max(1, ceil(len(rows) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    if not chunk:
        text = "╭━━━ <b>📋 DANH SÁCH UID</b> ━━━╮\n\nChưa có UID nào.\n╰━━━━━━━━━━━━━━━━━━━━╯"
    else:
        lines = ["╭━━━ <b>📋 DANH SÁCH UID</b> ━━━╮", ""]
        for r in chunk:
            state = _status(r.get("last_status"))
            on = "🟢 ON" if r.get("enabled") else "⚫ OFF"
            interval = max(1, int(r.get("interval_seconds") or 300) // 60)
            last = r.get("last_checked_at") or "Chưa check"
            latency = r.get("last_latency_ms") or 0
            lines += [
                f"<b>#{r['id']}</b> · <code>{_escape(r['uid'])}</code>",
                f"{state} · {on} · ⏱ {interval}m · ⚡ {latency}ms",
                f"🕒 {_escape(last)}",
                "",
            ]
        lines.append(f"<i>Trang {page + 1}/{total_pages} · {len(rows)} UID</i>")
        text = "\n".join(lines)
    buttons = []
    for r in chunk:
        buttons.append([Button.inline(f"🔎 #{r['id']} • {r['uid']}", f"fbw:detail:{r['id']}")])
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️", f"fbw:list:{page-1}"))
    nav.append(Button.inline(f"{page+1}/{total_pages}", b"fbw:noop"))
    if page < total_pages - 1:
        nav.append(Button.inline("➡️", f"fbw:list:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([Button.inline("➕ Thêm", b"fbw:add"), Button.inline("🏠", b"fbw:home")])
    return text, buttons


def _detail(r):
    status = _status(r.get("last_status"))
    interval = max(1, int(r.get("interval_seconds") or 300) // 60)
    daily = r.get("daily_times") or "[]"
    try:
        times = __import__("json").loads(daily)
    except Exception:
        times = []
    days = r.get("weekdays") or "[0,1,2,3,4,5,6]"
    try:
        days = __import__("json").loads(days)
    except Exception:
        days = list(range(7))
    day_names = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]
    day_text = ", ".join(day_names[int(x)] for x in days if str(x).isdigit() and 0 <= int(x) <= 6)
    return (
        "╭━━━ <b>🔎 CHI TIẾT UID</b> ━━━╮\n\n"
        f"🆔 <code>{_escape(r['uid'])}</code>\n"
        f"📊 Trạng thái: <b>{status}</b>\n"
        f"📡 Theo dõi: <b>{'ON' if r.get('enabled') else 'OFF'}</b>\n"
        f"⏱ Chu kỳ: <b>{interval} phút</b>\n"
        f"🔔 Đổi trạng thái: <b>{'ON' if r.get('notify_transition') else 'OFF'}</b>\n"
        f"📅 Báo cáo: <b>{'ON' if r.get('notify_daily') else 'OFF'}</b>\n"
        f"🕐 Giờ: <b>{', '.join(times) if times else 'Không đặt'}</b>\n"
        f"📆 Ngày: <b>{day_text or 'Không đặt'}</b>\n"
        f"⚡ API: <b>{r.get('last_latency_ms') or 0}ms</b>\n"
        f"🕒 Check cuối: <b>{_escape(r.get('last_checked_at') or 'Chưa check')}</b>\n"
        f"🧠 {_escape(r.get('last_reason') or 'Chưa có dữ liệu')}\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯"
    )


def register(bot, notify_bot=None):
    service = get_service(bot)

    async def bootstrap():
        try:
            await service.start()
        except Exception:
            import logging
            logging.getLogger("commands.fbmonitor").exception("FB monitor bootstrap failed")

    try:
        asyncio.get_running_loop().create_task(bootstrap(), name="fbmonitor-bootstrap")
    except RuntimeError:
        pass

    @bot.on(events.NewMessage(pattern=r"^/fbwatch(?:@\w+)?$"))
    async def fbwatch(event):
        await service.start()
        await event.reply(_home(service, event.sender_id), buttons=_menu(), parse_mode="html", link_preview=False)

    @bot.on(events.NewMessage(pattern=r"^/fbadd(?:@\w+)?(?:\s+.*)?$"))
    async def fbadd(event):
        await service.start()
        parts = event.raw_text.split(maxsplit=1)
        uid = parts[1].strip().split()[0] if len(parts) > 1 else ""
        if not UID_RE.fullmatch(uid):
            _PENDING[event.sender_id] = "add"
            await event.reply(
                "╭━━━ <b>➕ THÊM UID FACEBOOK</b> ━━━╮\n\n"
                "Gửi <b>UID dạng số</b> ở tin nhắn tiếp theo.\n"
                "Ví dụ: <code>1000123456789</code>\n\n"
                "⏱ Không làm ảnh hưởng các command khác.",
                buttons=_back(), parse_mode="html")
            return
        await _add_and_reply(event, service, uid)

    @bot.on(events.NewMessage(pattern=r"^/fblist(?:@\w+)?$"))
    async def fblist(event):
        await service.start()
        text, buttons = _list_view(service.db.list_user(event.sender_id), 0)
        await event.reply(text, buttons=buttons, parse_mode="html", link_preview=False)

    @bot.on(events.NewMessage(pattern=r"^/fbcheck(?:@\w+)?\s+(\d+)$"))
    async def fbcheck(event):
        await service.start()
        result = await service.check_now(event.sender_id, int(event.pattern_match.group(1)))
        await _check_reply(event, result)

    @bot.on(events.NewMessage(pattern=r"^/fbremove(?:@\w+)?\s+(\d+)$"))
    async def fbremove(event):
        ok = service.db.remove(event.sender_id, int(event.pattern_match.group(1)))
        await event.reply("✅ Đã xóa UID." if ok else "❌ Không tìm thấy UID.", buttons=_back(), parse_mode="html")
        service.wake()

    @bot.on(events.NewMessage(pattern=r"^/fbset(?:@\w+)?\s+(\d+)\s+(\w+)\s+(.+)$"))
    async def fbset(event):
        wid = int(event.pattern_match.group(1)); key = event.pattern_match.group(2).lower(); value = event.pattern_match.group(3).strip()
        fields = {}
        try:
            if key == "interval":
                fields["interval_seconds"] = max(30, min(int(value) * 60, 7 * 24 * 3600))
            elif key == "daily":
                times = [x.strip() for x in value.split(",") if TIME_RE.fullmatch(x.strip())]
                if not times: raise ValueError
                fields["daily_times"] = __import__("json").dumps(sorted(set(times)))
            elif key == "days":
                days = sorted(set(int(x.strip()) for x in value.split(",")))
                if not days or any(x < 0 or x > 6 for x in days): raise ValueError
                fields["weekdays"] = __import__("json").dumps(days)
            elif key in {"enabled", "transition", "daily_notify"}:
                if value.lower() not in {"on", "off", "1", "0", "true", "false"}: raise ValueError
                val = 1 if value.lower() in {"on", "1", "true"} else 0
                fields[{"enabled":"enabled", "transition":"notify_transition", "daily_notify":"notify_daily"}[key]] = val
            else: raise ValueError
        except (ValueError, TypeError):
            await event.reply("❌ Giá trị không hợp lệ. Dùng /fbhelp để xem cú pháp.", buttons=_back())
            return
        ok = service.db.update(event.sender_id, wid, **fields)
        await event.reply("✅ Đã cập nhật cấu hình." if ok else "❌ Không tìm thấy UID.", buttons=_back(), parse_mode="html")
        service.wake()

    @bot.on(events.NewMessage(pattern=r"^/fbhelp(?:@\w+)?$"))
    async def fbhelp(event):
        await event.reply(_help_text(), buttons=_back(), parse_mode="html", link_preview=False)

    @bot.on(events.NewMessage())
    async def pending_uid(event):
        if not event.raw_text or event.raw_text.startswith("/"):
            return
        if _PENDING.get(event.sender_id) != "add":
            return
        uid = event.raw_text.strip().split()[0]
        if not UID_RE.fullmatch(uid):
            await event.reply("❌ UID không hợp lệ. Cần 5–30 chữ số.")
            return
        _PENDING.pop(event.sender_id, None)
        await _add_and_reply(event, service, uid)

    # One dedicated callback router.  It ACKs immediately, never lets an
    # exception escape silently, and always gives the user a usable screen.
    @bot.on(events.CallbackQuery(pattern=re.compile(rb"^fbw:")))
    async def fb_buttons(event):
        try:
            await service.start()

            raw = event.data or b""
            action = raw.decode("utf-8", errors="replace")
            if not action.startswith("fbw:"):
                await event.answer("Nút không hợp lệ.", alert=True)
                return
            action = action[4:]

            # ACK first: Telegram clients otherwise show the spinning button.
            try:
                await event.answer()
            except Exception:
                pass

            async def edit(text, buttons=None):
                try:
                    return await event.edit(
                        text,
                        buttons=buttons,
                        parse_mode="html",
                        link_preview=False,
                    )
                except Exception:
                    # Some Telethon versions/contexts can reject an edit with
                    # link_preview. Retry with the minimal edit signature.
                    return await event.edit(
                        text,
                        buttons=buttons,
                        parse_mode="html",
                    )

            user_id = int(event.sender_id)

            if action in {"home", "menu"}:
                await edit(_home(service, user_id), _menu())
                return

            if action == "noop":
                return

            if action.startswith("list:"):
                try:
                    page = max(0, int(action.split(":", 1)[1]))
                except (ValueError, IndexError):
                    page = 0
                text, buttons = _list_view(service.db.list_user(user_id), page)
                await edit(text, buttons)
                return

            if action == "add":
                _PENDING[user_id] = "add"
                await edit(
                    "╭━━━ <b>➕ THÊM UID FACEBOOK</b> ━━━╮\n\n"
                    "Gửi UID Facebook dạng số ở tin nhắn tiếp theo.\n"
                    "Ví dụ: <code>1000123456789</code>\n\n"
                    "💡 Có thể bấm <b>Hủy</b> để quay lại.",
                    [[Button.inline("✖️ Hủy", b"fbw:home")]],
                )
                return

            if action == "checkall":
                rows = service.db.list_user(user_id)
                if not rows:
                    await edit(
                        "╭━━━ <b>⚡ CHECK TẤT CẢ</b> ━━━╮\n\n"
                        "Chưa có UID nào để kiểm tra.",
                        _back(),
                    )
                    return

                await edit(
                    f"╭━━━ <b>⚡ CHECK TẤT CẢ</b> ━━━╮\n\n"
                    f"Đã xếp <b>{len(rows)}</b> UID vào hàng kiểm tra.\n\n"
                    "🚀 Các request chạy song song có giới hạn.\n"
                    "🤖 Bot vẫn nhận command bình thường.",
                    [[Button.inline("📋 Xem danh sách", b"fbw:list:0")],
                     [Button.inline("🏠 Trang chủ", b"fbw:home")]],
                )
                task = asyncio.create_task(
                    _check_all(service, user_id, rows),
                    name=f"fb-checkall-{user_id}",
                )
                def _checkall_done(t):
                    if t.cancelled():
                        return
                    try:
                        t.exception()
                    except Exception:
                        pass
                task.add_done_callback(_checkall_done)
                return

            if action == "notify":
                await edit(
                    "╭━━━ <b>🔔 THÔNG BÁO</b> ━━━╮\n\n"
                    "Chọn một UID trong danh sách để bật/tắt:\n"
                    "• 🚨 LIVE ↔ DIE\n"
                    "• 📅 Báo cáo định kỳ\n\n"
                    "Thiết lập được lưu vĩnh viễn trong SQLite.",
                    [[Button.inline("📋 Chọn UID", b"fbw:list:0")],
                     [Button.inline("🏠 Trang chủ", b"fbw:home")]],
                )
                return

            if action == "interval":
                await edit(
                    "╭━━━ <b>⏱ CHU KỲ CHECK</b> ━━━╮\n\n"
                    "<code>/fbset ID interval 1</code>  → mỗi 1 phút\n"
                    "<code>/fbset ID interval 5</code>  → mỗi 5 phút\n"
                    "<code>/fbset ID interval 30</code> → mỗi 30 phút\n\n"
                    "⚙️ Mỗi UID có chu kỳ riêng.\n"
                    "🛡️ Tối thiểu 30 giây để tránh spam API.",
                    [[Button.inline("📋 Chọn UID", b"fbw:list:0")],
                     [Button.inline("🏠 Trang chủ", b"fbw:home")]],
                )
                return

            if action == "schedule":
                await edit(
                    "╭━━━ <b>📅 LỊCH TỰ ĐỘNG</b> ━━━╮\n\n"
                    "<code>/fbset ID daily 08:00,12:00,20:00</code>\n"
                    "<code>/fbset ID daily_notify on</code>\n"
                    "<code>/fbset ID days 0,1,2,3,4,5,6</code>\n\n"
                    "0=T2 · 1=T3 · 2=T4 · 3=T5\n"
                    "4=T6 · 5=T7 · 6=CN\n\n"
                    "🕐 Dùng giờ hệ thống của máy chạy bot.",
                    [[Button.inline("📋 Chọn UID", b"fbw:list:0")],
                     [Button.inline("🏠 Trang chủ", b"fbw:home")]],
                )
                return

            if action == "help":
                await edit(_help_text(), _back())
                return

            if action.startswith("manage:"):
                try:
                    page = max(0, int(action.split(":", 1)[1]))
                except (ValueError, IndexError):
                    page = 0
                text, buttons = _list_view(service.db.list_user(user_id), page)
                await edit(text, buttons)
                return

            if action.startswith("detail:"):
                try:
                    wid = int(action.split(":", 1)[1])
                except (ValueError, IndexError):
                    await event.answer("ID UID không hợp lệ.", alert=True)
                    return
                row = service.db.get(user_id, wid)
                if not row:
                    await event.answer("UID không tồn tại hoặc đã bị xóa.", alert=True)
                    return

                buttons = [
                    [Button.inline("⚡ Check ngay", f"fbw:checkid:{wid}"),
                     Button.inline(
                         "🟢 Đang ON" if row.get("enabled") else "⚫ Đang OFF",
                         f"fbw:toggle:{wid}",
                     )],
                    [Button.inline(
                         "🔔 Tắt báo đổi" if row.get("notify_transition") else "🔔 Bật báo đổi",
                         f"fbw:transition:{wid}",
                     ),
                     Button.inline(
                         "📅 Tắt báo cáo" if row.get("notify_daily") else "📅 Bật báo cáo",
                         f"fbw:daily:{wid}",
                     )],
                    [Button.inline("📜 Lịch sử", f"fbw:history:{wid}"),
                     Button.inline("🗑 Xóa", f"fbw:removeconfirm:{wid}")],
                    [Button.inline("⬅️ Danh sách", b"fbw:list:0"),
                     Button.inline("🏠 Trang chủ", b"fbw:home")],
                ]
                await edit(_detail(row), buttons)
                return

            if action.startswith("checkid:"):
                try:
                    wid = int(action.split(":", 1)[1])
                except (ValueError, IndexError):
                    await event.answer("ID không hợp lệ.", alert=True)
                    return
                await edit(
                    "⏳ <b>Đang kiểm tra UID...</b>\n\n"
                    "Đang gọi Facebook API, vui lòng chờ.",
                    [[Button.inline("✖️ Hủy màn hình", f"fbw:detail:{wid}")]],
                )
                result = await service.check_now(user_id, wid)
                if not result:
                    await event.answer("UID không tồn tại.", alert=True)
                    return
                await _edit_check(event, wid, result)
                return

            if action.startswith("toggle:"):
                wid = int(action.split(":", 1)[1])
                row = service.db.get(user_id, wid)
                if not row:
                    await event.answer("UID không tồn tại.", alert=True)
                    return
                service.db.update(user_id, wid, enabled=0 if row.get("enabled") else 1)
                service.wake()
                row = service.db.get(user_id, wid)
                buttons = [
                    [Button.inline("⚡ Check ngay", f"fbw:checkid:{wid}")],
                    [Button.inline("⬅️ Chi tiết", f"fbw:detail:{wid}")],
                ]
                await edit(_detail(row), buttons)
                return

            if action.startswith("transition:"):
                wid = int(action.split(":", 1)[1])
                row = service.db.get(user_id, wid)
                if not row:
                    await event.answer("UID không tồn tại.", alert=True)
                    return
                service.db.update(
                    user_id, wid,
                    notify_transition=0 if row.get("notify_transition") else 1,
                )
                row = service.db.get(user_id, wid)
                await edit(
                    _detail(row),
                    [[Button.inline("⬅️ Chi tiết", f"fbw:detail:{wid}")],
                     [Button.inline("🏠 Trang chủ", b"fbw:home")]],
                )
                return

            if action.startswith("daily:"):
                wid = int(action.split(":", 1)[1])
                row = service.db.get(user_id, wid)
                if not row:
                    await event.answer("UID không tồn tại.", alert=True)
                    return
                service.db.update(
                    user_id, wid,
                    notify_daily=0 if row.get("notify_daily") else 1,
                )
                row = service.db.get(user_id, wid)
                await edit(
                    _detail(row),
                    [[Button.inline("⬅️ Chi tiết", f"fbw:detail:{wid}")],
                     [Button.inline("🏠 Trang chủ", b"fbw:home")]],
                )
                return

            if action.startswith("history:"):
                wid = int(action.split(":", 1)[1])
                row = service.db.get(user_id, wid)
                if not row:
                    await event.answer("UID không tồn tại.", alert=True)
                    return
                events_ = service.db.recent_events(user_id, wid, 8)
                if not events_:
                    history = "Chưa có lần chuyển trạng thái nào."
                else:
                    lines = ["<b>📜 LỊCH SỬ TRẠNG THÁI</b>", ""]
                    for ev in events_:
                        old = ev.get("old_status") or "—"
                        new = ev.get("new_status") or "?"
                        old = old.upper()
                        new = new.upper()
                        icon = "🟢" if new == "LIVE" else "🔴" if new == "DIE" else "🟡"
                        lines.append(
                            f"{icon} <b>{old} → {new}</b> · "
                            f"{_escape(ev.get('checked_at') or '')}"
                        )
                        if ev.get("reason"):
                            lines.append(f"└ {_escape(ev['reason'])}")
                    history = "\n".join(lines)
                await edit(
                    f"╭━━━ <b>📜 LỊCH SỬ</b> ━━━╮\n\n"
                    f"🆔 <code>{_escape(row['uid'])}</code>\n\n"
                    f"{history}\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━╯",
                    [[Button.inline("⬅️ Chi tiết", f"fbw:detail:{wid}")],
                     [Button.inline("🏠 Trang chủ", b"fbw:home")]],
                )
                return

            if action.startswith("removeconfirm:"):
                wid = int(action.split(":", 1)[1])
                row = service.db.get(user_id, wid)
                if not row:
                    await event.answer("UID không tồn tại.", alert=True)
                    return
                await edit(
                    "╭━━━ <b>⚠️ XÁC NHẬN XÓA</b> ━━━╮\n\n"
                    f"Bạn chắc chắn muốn xóa:\n"
                    f"🆔 <code>{_escape(row['uid'])}</code>\n\n"
                    "Lịch sử của UID cũng sẽ bị xóa.",
                    [[Button.inline("✅ Xóa UID", f"fbw:removeid:{wid}"),
                      Button.inline("❌ Hủy", f"fbw:detail:{wid}")]],
                )
                return

            if action.startswith("removeid:"):
                wid = int(action.split(":", 1)[1])
                ok = service.db.remove(user_id, wid)
                service.wake()
                await edit(
                    "✅ <b>Đã xóa UID khỏi monitor.</b>" if ok
                    else "❌ UID không tồn tại.",
                    [[Button.inline("📋 Danh sách", b"fbw:list:0")],
                     [Button.inline("🏠 Trang chủ", b"fbw:home")]],
                )
                return

            await event.answer("Chức năng này chưa được hỗ trợ.", alert=True)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never leave a callback looking like a dead button.
            import logging
            logging.getLogger("commands.fbmonitor").exception(
                "Callback failed: %s", exc
            )
            try:
                await event.answer("Có lỗi khi xử lý nút. Đã ghi log để kiểm tra.", alert=True)
            except Exception:
                pass


async def _add_and_reply(event, service, uid):
    row = service.db.add(event.sender_id, uid)
    result = await service.check_now(event.sender_id, row["id"])
    await event.reply(
        "╭━━━ <b>✅ ĐÃ THÊM UID</b> ━━━╮\n"
        f"🆔 <code>{_escape(uid)}</code>\n"
        f"📊 <b>{_status(result.status)}</b>\n"
        f"⚡ API: <b>{result.latency_ms}ms</b>\n"
        f"🔎 {_escape(result.reason)}\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🛰 <b>Đã đưa vào monitor nền.</b>",
        buttons=_back(), parse_mode="html", link_preview=False)
    service.wake()


async def _check_all(service, user_id, rows):
    # Each check is independent and bounded by the checker's semaphore.
    async def one(r):
        try:
            return await service.check_now(user_id, int(r["id"]))
        except Exception:
            return None
    await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)


async def _check_reply(event, result):
    if not result:
        await event.reply("❌ Không tìm thấy UID.", buttons=_back()); return
    await event.reply(
        "╭━━━ <b>⚡ KẾT QUẢ CHECK</b> ━━━╮\n"
        f"🆔 <code>{_escape(result.uid)}</code>\n"
        f"📊 <b>{_status(result.status)}</b>\n"
        f"⚡ API: <b>{result.latency_ms}ms</b> · 🔁 {result.attempts} lần\n"
        f"🌐 HTTP: <b>{result.http_status or '-'}</b>\n"
        f"🔎 {_escape(result.reason)}\n"
        f"🕒 {result.checked_at}\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯", buttons=_back(), parse_mode="html")


async def _edit_check(event, wid, result):
    if not result:
        await event.answer("Không tìm thấy UID", alert=True); return
    await event.edit(
        "╭━━━ <b>⚡ CHECK NGAY</b> ━━━╮\n"
        f"🆔 <code>{_escape(result.uid)}</code>\n"
        f"📊 <b>{_status(result.status)}</b>\n"
        f"⚡ API: <b>{result.latency_ms}ms</b>\n"
        f"🔎 {_escape(result.reason)}\n"
        f"🕒 {result.checked_at}\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯",
        buttons=[[Button.inline("🔎 Chi tiết", f"fbw:detail:{wid}")], [Button.inline("🏠", b"fbw:home")]], parse_mode="html")


def _help_text():
    return (
        "╭━━━ <b>📘 FACEBOOK UID MONITOR</b> ━━━╮\n\n"
        "<b>➕ Thêm UID</b>\n<code>/fbadd 1000123456789</code>\n\n"
        "<b>📋 Danh sách</b>\n<code>/fblist</code>\n\n"
        "<b>⚡ Check</b>\n<code>/fbcheck ID</code>\n\n"
        "<b>⏱ Chu kỳ</b>\n<code>/fbset ID interval 5</code>\n\n"
        "<b>🔔 Báo đổi LIVE ↔ DIE</b>\n<code>/fbset ID transition on</code>\n\n"
        "<b>📅 Báo cáo theo giờ</b>\n<code>/fbset ID daily 08:00,12:00,20:00</code>\n"
        "<code>/fbset ID daily_notify on</code>\n\n"
        "<b>📆 Ngày</b>\n<code>/fbset ID days 0,1,2,3,4,5,6</code>\n\n"
        "<b>🛡 Chống treo</b>\n"
        "• aiohttp connection pool\n"
        "• timeout ngắn + retry/backoff\n"
        "• giới hạn concurrency\n"
        "• không tạo duplicate job\n"
        "• UNKNOWN khi API/network lỗi\n"
        "• scheduler chạy nền độc lập\n"
        "• /stop không dừng monitor này\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )
