# ============================================================
# commands/cupdien.py
# TELEGRAM COMMAND - CÚP ĐIỆN
# ============================================================

from telethon import events, Button

from core.power.api import (
    AREA_CODES,
    get_by_area,
    get_by_customer,
    date_range,
)

from core.power.database import (
    add_subscription as legacy_add_subscription,
    get_user_subscriptions as legacy_get_user_subscriptions,
    remove_subscription as legacy_remove_subscription,
)

from core.power.session import (
    get_session,
    set_session,
    clear_session,
)

from core.task_manager import replace_user_tasks

COMMAND_INFO = {
    "command": "cupdien",
    "category": "⚡ ĐIỆN LỰC",
    "title": "Lịch cúp điện",

    "description": (
        "Kiểm tra lịch cúp điện theo khu vực hoặc mã khách hàng "
        "và đăng ký theo dõi tự động."
    ),

    "usage": "/cupdien",

    "examples": [
        "/cupdien",
    ],

    "details": [
        "Gửi /cupdien để mở menu cúp điện.",
        "Có thể kiểm tra theo khu vực.",
        "Có thể kiểm tra theo mã khách hàng.",
        "Có thể kiểm tra đồng thời khu vực và mã khách hàng.",
        "Chọn khung giờ để đăng ký theo dõi hằng ngày.",
        "Bot tự động kiểm tra lịch theo thời gian đã chọn.",
        "Nếu có lịch cúp điện mới, bot sẽ tự động gửi thông báo.",
        "Có thể xem danh sách các đăng ký đang theo dõi.",
        "Có thể xóa đăng ký theo dõi bất cứ lúc nào.",
    ],

    "supported": [
        "📍 Theo khu vực",
        "🆔 Theo mã khách hàng",
        "📍 + 🆔 Khu vực và mã khách hàng",
        "⏰ Theo khung giờ",
        "🔔 Tự động thông báo",
        "🗑️ Quản lý đăng ký",
    ],
}

# ============================================================
# CUPDIEN CORE V2 — DURABLE SCHEDULER / RECOVERY ENGINE
# ============================================================
# Mục tiêu:
# - Lịch hẹn sống qua restart/redeploy/ngày mới.
# - Không phụ thuộc asyncio task còn sống từ hôm trước.
# - SQLite WAL + transaction + fsync-safe commit.
# - Scheduler tự khởi động lại khi register() được gọi.
# - Nếu bot khởi động sau giờ hẹn, scheduler có thể chạy bù.
# - Chống gửi trùng bằng notification ledger + fingerprint.
# - Không xóa lịch chỉ vì đã chạy qua ngày.
# - UI dùng một message có thể edit, hạn chế spam.
# ============================================================

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


LOGGER = logging.getLogger("CUPDIEN")
CUPDIEN_VERSION = "4.0.0-PRO"
CUPDIEN_TZ_NAME = os.getenv("CUPDIEN_TIMEZONE", "Asia/Ho_Chi_Minh")
CUPDIEN_DB_ENV = os.getenv("CUPDIEN_DB_PATH", "").strip()
CUPDIEN_DATA_ENV = os.getenv("CUPDIEN_DATA_DIR", "").strip()
CUPDIEN_SCHEDULER_INTERVAL = max(5, int(os.getenv("CUPDIEN_SCHEDULER_INTERVAL", "15")))
CUPDIEN_CATCHUP_HOURS = max(1, int(os.getenv("CUPDIEN_CATCHUP_HOURS", "26")))
CUPDIEN_NOTIFY_EMPTY = os.getenv("CUPDIEN_NOTIFY_EMPTY", "0").lower() in {"1", "true", "yes", "on"}
CUPDIEN_MAX_HISTORY = max(100, int(os.getenv("CUPDIEN_MAX_HISTORY", "5000")))


def _power_timezone():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(CUPDIEN_TZ_NAME)
        except Exception:
            pass
    return timezone(timedelta(hours=7))


POWER_TZ = _power_timezone()


def power_now():
    return datetime.now(POWER_TZ)


def power_iso(dt=None):
    dt = dt or power_now()
    return dt.isoformat(timespec="seconds")


def power_date(dt=None):
    return (dt or power_now()).date().isoformat()


def _db_path():
    if CUPDIEN_DB_ENV:
        path = Path(CUPDIEN_DB_ENV).expanduser()
    elif CUPDIEN_DATA_ENV:
        path = Path(CUPDIEN_DATA_ENV).expanduser() / "cupdien.sqlite3"
    else:
        root = Path(__file__).resolve().parent.parent
        path = root / "data" / "cupdien.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


POWER_DB_PATH = _db_path()


@contextmanager
def power_db():
    conn = sqlite3.connect(
        str(POWER_DB_PATH),
        timeout=30,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def _power_init_db():
    with power_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legacy_id INTEGER,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            value TEXT NOT NULL,
            area_code TEXT DEFAULT '',
            area_name TEXT DEFAULT '',
            check_time TEXT NOT NULL DEFAULT '07:00',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_date TEXT DEFAULT '',
            last_run_at TEXT DEFAULT '',
            last_fingerprint TEXT DEFAULT '',
            last_status TEXT DEFAULT 'NEVER',
            last_error TEXT DEFAULT '',
            total_runs INTEGER NOT NULL DEFAULT 0,
            total_notifications INTEGER NOT NULL DEFAULT 0,
            consecutive_errors INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id, type, value, area_code, check_time)
        );
        CREATE INDEX IF NOT EXISTS idx_power_sub_user ON subscriptions_v2(user_id);
        CREATE INDEX IF NOT EXISTS idx_power_sub_due ON subscriptions_v2(enabled, check_time);
        CREATE TABLE IF NOT EXISTS notification_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subscription_id INTEGER,
            run_date TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            error TEXT DEFAULT '',
            UNIQUE(user_id, subscription_id, run_date, fingerprint)
        );
        CREATE INDEX IF NOT EXISTS idx_power_ledger_user_date
            ON notification_ledger(user_id, run_date);
        CREATE TABLE IF NOT EXISTS scheduler_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scheduler_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            subscription_id INTEGER,
            user_id INTEGER,
            message TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_power_events_created
            ON scheduler_events(created_at);
        """)
        # Forward-compatible migration for databases created by older builds.
        cols = {row[1] for row in db.execute("PRAGMA table_info(subscriptions_v2)").fetchall()}
        if "legacy_id" not in cols:
            db.execute("ALTER TABLE subscriptions_v2 ADD COLUMN legacy_id INTEGER")
        db.execute("CREATE INDEX IF NOT EXISTS idx_power_sub_legacy ON subscriptions_v2(legacy_id)")


_power_init_db()


def _row_dict(row):
    return dict(row) if row is not None else None


def _normalize_time(value):
    value = str(value or "07:00").strip()
    try:
        hh, mm = value.split(":", 1)
        hh = int(hh)
        mm = int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    except Exception:
        pass
    return "07:00"


def _normalize_subscription(raw):
    return {
        "id": raw.get("id"),
        "user_id": int(raw.get("user_id") or 0),
        "type": str(raw.get("type") or "area"),
        "value": str(raw.get("value") or ""),
        "area_code": str(raw.get("area_code") or ""),
        "area_name": str(raw.get("area_name") or ""),
        "check_time": _normalize_time(raw.get("check_time")),
        "enabled": bool(raw.get("enabled", True)),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "last_run_date": str(raw.get("last_run_date") or ""),
        "last_run_at": str(raw.get("last_run_at") or ""),
        "last_fingerprint": str(raw.get("last_fingerprint") or ""),
        "last_status": str(raw.get("last_status") or "NEVER"),
        "last_error": str(raw.get("last_error") or ""),
        "total_runs": int(raw.get("total_runs") or 0),
        "total_notifications": int(raw.get("total_notifications") or 0),
        "consecutive_errors": int(raw.get("consecutive_errors") or 0),
        "revision": int(raw.get("revision") or 1),
    }


def _legacy_to_v2(user_id, raw):
    raw = dict(raw)
    return {
        "id": raw.get("id"),
        "user_id": user_id,
        "type": raw.get("type") or "area",
        "value": raw.get("value") or "",
        "area_code": raw.get("area_code") or "",
        "area_name": raw.get("area_name") or "",
        "check_time": _normalize_time(raw.get("check_time")),
        "enabled": True,
        "created_at": power_iso(),
        "updated_at": power_iso(),
        "last_run_date": "",
        "last_run_at": "",
        "last_fingerprint": "",
        "last_status": "NEVER",
        "last_error": "",
        "total_runs": 0,
        "total_notifications": 0,
        "consecutive_errors": 0,
        "revision": 1,
    }


def _v2_insert_or_update(raw):
    now = power_iso()
    item = _normalize_subscription(raw)
    with power_db() as db:
        db.execute("""
        INSERT INTO subscriptions_v2
        (legacy_id,user_id,type,value,area_code,area_name,check_time,enabled,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id,type,value,area_code,check_time)
        DO UPDATE SET
            legacy_id=COALESCE(excluded.legacy_id, subscriptions_v2.legacy_id),
            area_name=excluded.area_name,
            enabled=1,
            updated_at=excluded.updated_at
        """, (
            item.get("id"), item["user_id"], item["type"], item["value"], item["area_code"],
            item["area_name"], item["check_time"], 1,
            item["created_at"] or now, now,
        ))
        row = db.execute("""
            SELECT * FROM subscriptions_v2
            WHERE user_id=? AND type=? AND value=? AND area_code=? AND check_time=?
        """, (
            item["user_id"], item["type"], item["value"], item["area_code"], item["check_time"]
        )).fetchone()
        return _row_dict(row)


def durable_add_subscription(*, user_id, sub_type, value, area_code=None, area_name=None, check_time="07:00"):
    check_time = _normalize_time(check_time)
    legacy_id = None
    try:
        legacy_id = legacy_add_subscription(
            user_id=user_id,
            sub_type=sub_type,
            value=value,
            area_code=area_code,
            area_name=area_name,
            check_time=check_time,
        )
    except Exception as exc:
        LOGGER.warning("legacy add_subscription failed: %s", exc)
    row = _v2_insert_or_update({
        "user_id": user_id,
        "type": sub_type,
        "value": value,
        "area_code": area_code or "",
        "area_name": area_name or "",
        "check_time": check_time,
        "created_at": power_iso(),
        "id": legacy_id,
    })
    _scheduler_event("SUBSCRIPTION_SAVED", row.get("id") if row else None, user_id, "subscription saved")
    return row


def durable_get_user_subscriptions(user_id):
    with power_db() as db:
        rows = db.execute(
            "SELECT * FROM subscriptions_v2 WHERE user_id=? AND enabled=1 ORDER BY check_time,id",
            (user_id,),
        ).fetchall()
    if rows:
        return [_row_dict(x) for x in rows]

    # One-time lazy migration from the old database for this user.
    try:
        legacy_rows = legacy_get_user_subscriptions(user_id) or []
    except Exception as exc:
        LOGGER.warning("legacy get subscriptions failed: %s", exc)
        legacy_rows = []
    migrated = []
    for raw in legacy_rows:
        try:
            migrated.append(_v2_insert_or_update(_legacy_to_v2(user_id, raw)))
        except Exception as exc:
            LOGGER.error("subscription migration failed: %s", exc)
    return [x for x in migrated if x]


def durable_get_all_subscriptions():
    with power_db() as db:
        rows = db.execute(
            "SELECT * FROM subscriptions_v2 WHERE enabled=1 ORDER BY check_time,id"
        ).fetchall()
    return [_row_dict(x) for x in rows]


def durable_remove_subscription(sub_id):
    ok = False
    try:
        legacy_remove_subscription(sub_id)
        ok = True
    except Exception as exc:
        LOGGER.warning("legacy remove_subscription failed: %s", exc)
    with power_db() as db:
        row = db.execute("SELECT user_id,legacy_id FROM subscriptions_v2 WHERE id=?", (sub_id,)).fetchone()
        db.execute("DELETE FROM subscriptions_v2 WHERE id=?", (sub_id,))
    if row and row["legacy_id"] is not None:
        try:
            legacy_remove_subscription(row["legacy_id"])
        except Exception as exc:
            LOGGER.warning("legacy delete by mapped id failed: %s", exc)
    _scheduler_event("SUBSCRIPTION_DELETED", sub_id, int(row["user_id"]) if row else None, "subscription deleted")
    return ok


def _scheduler_event(event_type, subscription_id=None, user_id=None, message=""):
    try:
        with power_db() as db:
            db.execute(
                "INSERT INTO scheduler_events(event_type,subscription_id,user_id,message,created_at) VALUES(?,?,?,?,?)",
                (event_type, subscription_id, user_id, message[:1000], power_iso()),
            )
            db.execute(
                "DELETE FROM scheduler_events WHERE id NOT IN (SELECT id FROM scheduler_events ORDER BY id DESC LIMIT ?)",
                (CUPDIEN_MAX_HISTORY,),
            )
    except Exception:
        LOGGER.exception("scheduler event ledger failed")


def _set_scheduler_state(key, value):
    with power_db() as db:
        db.execute("""
        INSERT INTO scheduler_state(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
        """, (key, str(value), power_iso()))


def _get_scheduler_state(key, default=""):
    with power_db() as db:
        row = db.execute("SELECT value FROM scheduler_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _schedule_due(sub, now=None):
    now = now or power_now()
    target = _normalize_time(sub.get("check_time"))
    hh, mm = map(int, target.split(":"))
    due_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    last_date = str(sub.get("last_run_date") or "")
    today = now.date().isoformat()
    if last_date == today:
        return False, "ALREADY_RUN_TODAY"
    if now >= due_today:
        return True, "DUE"
    # Startup recovery: if last run is older than today, the normal daily due
    # check will happen at the configured time. We intentionally do not run
    # tomorrow's appointment early.
    return False, "WAITING"


def _fingerprint_result(result):
    try:
        schedules = result.get("schedules", []) if isinstance(result, dict) else []
    except Exception:
        schedules = []
    canonical = []
    for s in schedules:
        if not isinstance(s, dict):
            continue
        canonical.append({
            "code": str(s.get("code") or ""),
            "where": str(s.get("where") or ""),
            "start_time": str(s.get("start_time") or ""),
            "start_date": str(s.get("start_date") or ""),
            "end_time": str(s.get("end_time") or ""),
            "end_date": str(s.get("end_date") or ""),
            "cause": str(s.get("cause") or ""),
        })
    canonical.sort(key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), canonical


def _ledger_exists(user_id, sub_id, run_date, fingerprint):
    with power_db() as db:
        row = db.execute("""
            SELECT id,status FROM notification_ledger
            WHERE user_id=? AND subscription_id=? AND run_date=? AND fingerprint=?
        """, (user_id, sub_id, run_date, fingerprint)).fetchone()
    return dict(row) if row else None


def _record_ledger(user_id, sub_id, run_date, fingerprint, status, error=""):
    with power_db() as db:
        db.execute("""
        INSERT INTO notification_ledger
        (user_id,subscription_id,run_date,fingerprint,status,sent_at,error)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(user_id,subscription_id,run_date,fingerprint)
        DO UPDATE SET status=excluded.status,sent_at=excluded.sent_at,error=excluded.error
        """, (user_id, sub_id, run_date, fingerprint, status, power_iso(), error[:1000]))
        db.execute("""
            DELETE FROM notification_ledger
            WHERE id NOT IN (SELECT id FROM notification_ledger ORDER BY id DESC LIMIT ?)
        """, (CUPDIEN_MAX_HISTORY,))


def _mark_run(sub_id, *, run_date, fingerprint, status, error="", notified=False):
    with power_db() as db:
        db.execute("""
        UPDATE subscriptions_v2
        SET last_run_date=?, last_run_at=?, last_fingerprint=?, last_status=?,
            last_error=?, total_runs=total_runs+1,
            total_notifications=total_notifications+?,
            consecutive_errors=?, updated_at=?, revision=revision+1
        WHERE id=?
        """, (
            run_date, power_iso(), fingerprint, status, error[:1000],
            1 if notified else 0,
            0 if status == "SUCCESS" else 1,
            power_iso(), sub_id,
        ))


def _notification_text(sub, result, fingerprint, catchup=False):
    schedules = result.get("schedules", []) if isinstance(result, dict) else []
    area_name = sub.get("area_name") or sub.get("area_code") or "Không xác định"
    sub_type = sub.get("type") or "area"
    value = sub.get("value") or ""
    if sub_type == "customer":
        target_line = f"🆔 <b>Mã KH:</b> <code>{esc(value)}</code>"
        if area_name:
            target_line += f"\n📍 <b>Khu vực:</b> {esc(area_name)}"
    else:
        target_line = f"📍 <b>Khu vực:</b> {esc(area_name)}"
    now = power_now().strftime("%d/%m/%Y %H:%M:%S")
    lines = [
        "╭──────────────────────────────╮",
        "│  ⚡ <b>CẢNH BÁO LỊCH CÚP ĐIỆN</b>  │",
        "╰──────────────────────────────╯",
        "",
        target_line,
        f"⏰ <b>Lịch hẹn:</b> {esc(sub.get('check_time'))} mỗi ngày",
        f"🕒 <b>Kiểm tra:</b> {now}",
    ]
    if catchup:
        lines.append("🔄 <b>Recovery:</b> bot vừa khởi động lại và đã kiểm tra bù.")
    lines.append("")
    if schedules:
        lines.append(f"🚨 <b>Phát hiện {len(schedules)} lịch:</b>")
        for idx, schedule in enumerate(schedules[:12], 1):
            lines.append(f"\n<b>━━ LỊCH #{idx} ━━</b>")
            if schedule.get("where"):
                lines.append(f"📍 {esc(schedule.get('where'))}")
            if schedule.get("start_time") or schedule.get("start_date"):
                lines.append(f"🕐 Từ: {esc(schedule.get('start_time'))} {esc(schedule.get('start_date'))}")
            if schedule.get("end_time") or schedule.get("end_date"):
                lines.append(f"🕐 Đến: {esc(schedule.get('end_time'))} {esc(schedule.get('end_date'))}")
            if schedule.get("cause"):
                lines.append(f"📝 {esc(schedule.get('cause'))}")
            if schedule.get("code"):
                lines.append(f"🔖 <code>{esc(schedule.get('code'))}</code>")
    else:
        lines.append("✅ <b>Không có lịch cúp điện mới.</b>")
        lines.append("Hệ thống vẫn ghi nhận lần kiểm tra thành công.")
    lines.extend([
        "",
        f"🔐 <b>Fingerprint:</b> <code>{fingerprint[:12]}</code>",
        "🛡️ Hệ thống chống gửi trùng đang hoạt động.",
    ])
    return "\n".join(lines)


def _scheduler_status_text():
    last_tick = _get_scheduler_state("last_tick", "chưa có")
    last_error = _get_scheduler_state("last_error", "")
    subs = len(durable_get_all_subscriptions())
    lines = [
        "╭──────────────────────────────╮",
        "│  🛡️ <b>CUPDIEN SCHEDULER</b>  │",
        "╰──────────────────────────────╯",
        "",
        f"🟢 <b>Trạng thái:</b> đang bảo vệ lịch",
        f"🗄️ <b>Database:</b> SQLite WAL",
        f"🌏 <b>Múi giờ:</b> {esc(CUPDIEN_TZ_NAME)}",
        f"📋 <b>Đăng ký:</b> {subs}",
        f"🔁 <b>Chu kỳ:</b> {CUPDIEN_SCHEDULER_INTERVAL}s",
        f"🕒 <b>Tick cuối:</b> {esc(last_tick)}",
    ]
    if last_error:
        lines += [f"⚠️ <b>Lỗi gần nhất:</b> <code>{esc(last_error[:300])}</code>"]
    lines += ["", "✅ Lịch không bị xóa khi sang ngày mới.", "🔄 Restart bot vẫn khôi phục scheduler."]
    return "\n".join(lines)


class PowerScheduler:
    """Persistent daily scheduler.

    The task itself is disposable; the appointment is not. Every appointment
    is stored in SQLite and evaluated from wall-clock time on every tick.
    This deliberately avoids relying on `asyncio.sleep(seconds_until_target)`
    as the source of truth, because deploy/restart/time jumps would otherwise
    lose appointments.
    """

    def __init__(self, bot, notify_bot=None):
        self.bot = bot
        self.notify_bot = notify_bot or bot
        self.task = None
        self.stop_event = asyncio.Event()
        self.started_at = power_iso()
        self.running = False
        self._lock = asyncio.Lock()
        self._last_day = power_date()

    async def start(self):
        if self.task and not self.task.done():
            return
        self.stop_event = asyncio.Event()
        self.running = True
        self.task = asyncio.create_task(self.run(), name="cupdien-persistent-scheduler")
        _set_scheduler_state("started_at", self.started_at)
        _set_scheduler_state("last_error", "")
        _scheduler_event("SCHEDULER_STARTED", message=CUPDIEN_VERSION)

    async def stop(self):
        self.running = False
        self.stop_event.set()
        task = self.task
        self.task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                LOGGER.exception("scheduler stop error")
        _scheduler_event("SCHEDULER_STOPPED", message="stopped")

    async def run(self):
        await asyncio.sleep(1)
        while not self.stop_event.is_set():
            try:
                await self.tick()
                _set_scheduler_state("last_tick", power_iso())
                _set_scheduler_state("last_error", "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("CUPDIEN scheduler tick failed")
                _set_scheduler_state("last_error", f"{type(exc).__name__}: {exc}")
                _scheduler_event("SCHEDULER_ERROR", message=f"{type(exc).__name__}: {exc}")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=CUPDIEN_SCHEDULER_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def tick(self):
        async with self._lock:
            now = power_now()
            current_day = power_date(now)
            if current_day != self._last_day:
                _scheduler_event("DAY_ROLLOVER", message=f"{self._last_day} -> {current_day}")
                self._last_day = current_day
            subscriptions = durable_get_all_subscriptions()
            if not subscriptions:
                return
            for sub in subscriptions:
                due, reason = _schedule_due(sub, now)
                if not due:
                    continue
                try:
                    await self.process_subscription(sub, now)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.exception("subscription %s failed", sub.get("id"))
                    _mark_run(
                        sub.get("id"), run_date=power_date(now), fingerprint="ERROR",
                        status="ERROR", error=f"{type(exc).__name__}: {exc}", notified=False,
                    )

    async def process_subscription(self, sub, now=None):
        now = now or power_now()
        user_id = int(sub["user_id"])
        sub_id = int(sub["id"])
        run_date = power_date(now)
        started = time.monotonic()
        mode = sub.get("type") or "area"
        value = sub.get("value") or ""
        area_code = sub.get("area_code") or ""
        area_name = sub.get("area_name") or ""

        if mode == "customer":
            result = await get_by_customer(value, *date_range())
        else:
            result = await get_by_area(area_code or value, *date_range())

        fingerprint, canonical = _fingerprint_result(result)
        existing = _ledger_exists(user_id, sub_id, run_date, fingerprint)
        if existing and existing.get("status") == "SENT":
            _mark_run(sub_id, run_date=run_date, fingerprint=fingerprint, status="SUCCESS", notified=False)
            return

        should_notify = bool(canonical) or CUPDIEN_NOTIFY_EMPTY
        text = _notification_text(sub, result, fingerprint, catchup=False)
        sent = False
        error = ""
        if should_notify:
            try:
                await self.notify_bot.send_message(
                    user_id,
                    text,
                    parse_mode="html",
                    link_preview=False,
                )
                sent = True
                _record_ledger(user_id, sub_id, run_date, fingerprint, "SENT")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                _record_ledger(user_id, sub_id, run_date, fingerprint, "FAILED", error)
                raise
        else:
            _record_ledger(user_id, sub_id, run_date, fingerprint, "CHECKED")

        _mark_run(
            sub_id,
            run_date=run_date,
            fingerprint=fingerprint,
            status="SUCCESS",
            error="",
            notified=sent,
        )
        _scheduler_event(
            "SUBSCRIPTION_CHECKED",
            sub_id,
            user_id,
            f"status=SUCCESS schedules={len(canonical)} elapsed={time.monotonic()-started:.2f}s",
        )

    async def recovery(self):
        """Run due appointments immediately after a restart.

        A daily appointment is represented by last_run_date, so a restart at
        09:00 after a 07:00 appointment is still recoverable. The appointment
        is not considered consumed until the query completes successfully.
        """
        now = power_now()
        for sub in durable_get_all_subscriptions():
            due, _ = _schedule_due(sub, now)
            if not due:
                continue
            try:
                await self.process_subscription(sub, now)
                _scheduler_event("RECOVERY_OK", sub.get("id"), sub.get("user_id"), "recovered due appointment")
            except Exception as exc:
                _scheduler_event("RECOVERY_ERROR", sub.get("id"), sub.get("user_id"), str(exc))


SCHEDULERS = {}


def get_power_scheduler(bot, notify_bot=None):
    key = id(bot)
    scheduler = SCHEDULERS.get(key)
    if scheduler is None:
        scheduler = PowerScheduler(bot, notify_bot)
        SCHEDULERS[key] = scheduler
    else:
        scheduler.notify_bot = notify_bot or scheduler.notify_bot
    return scheduler


# Override the three storage functions used by the legacy UI below.
add_subscription = durable_add_subscription
get_user_subscriptions = durable_get_user_subscriptions
remove_subscription = durable_remove_subscription

# ============================================================
# ESCAPE HTML
# ============================================================

def esc(value):
    if value is None:
        return ""

    value = str(value)

    return (
        value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ============================================================
# MAIN MENU
# ============================================================

def main_menu():

    return [
        [
            Button.inline(
                "⚡ Kiểm tra ngay",
                b"power:instant"
            )
        ],
        [
            Button.inline(
                "📍 Theo dõi khu vực",
                b"power:type:area"
            ),
            Button.inline(
                "🆔 Theo dõi mã KH",
                b"power:type:customer"
            )
        ],
        [
            Button.inline(
                "📍🆔 Theo dõi cả hai",
                b"power:type:both"
            )
        ],
        [
            Button.inline(
                "📋 Theo dõi của tôi",
                b"power:list"
            ),
            Button.inline(
                "🛡️ Scheduler",
                b"power:status"
            )
        ]
    ]


# ============================================================
# START TEXT
# ============================================================

def menu_text():
    return (
        "╭──────────────────────────────╮\n"
        "│  ⚡ <b>TRUNG TÂM CÚP ĐIỆN</b>  │\n"
        "╰──────────────────────────────╯\n\n"
        "🛰️ <b>POWER WATCH • PRO</b>\n"
        "Theo dõi lịch cúp điện tự động với bộ máy scheduler bền vững.\n\n"
        "⚡ <b>Kiểm tra ngay</b>\n"
        "└ Tra cứu dữ liệu mới nhất theo khu vực / mã KH.\n\n"
        "🔔 <b>Đặt lịch tự động</b>\n"
        "└ Kiểm tra đúng giờ mỗi ngày, kể cả sau khi bot restart.\n\n"
        "🛡️ <b>Recovery Engine</b>\n"
        "└ Không dùng task ngủ dài làm nguồn dữ liệu lịch hẹn.\n"
        "└ SQLite WAL lưu lịch bền vững qua ngày/redeploy.\n\n"
        "💡 <i>Chọn một chức năng bên dưới để bắt đầu.</i>"
    )


# ============================================================
# AREA BUTTONS
# ============================================================

def area_buttons(
    prefix="power:area"
):

    buttons = []

    items = list(
        AREA_CODES.items()
    )

    for i in range(
        0,
        len(items),
        2
    ):

        row = []

        for code, name in items[i:i + 2]:

            short_name = (
                name
                .replace(
                    "Điện lực ",
                    ""
                )
            )

            row.append(
                Button.inline(
                    f"📍 {short_name}",
                    f"{prefix}:{code}".encode()
                )
            )

        buttons.append(row)

    buttons.append(
        [
            Button.inline(
                "🔙 Quay lại",
                b"power:back"
            )
        ]
    )

    return buttons


def area_buttons_instant():

    return area_buttons(
        "power:instantarea"
    )


# ============================================================
# TIME BUTTONS
# ============================================================

def time_buttons(
    prefix
):

    return [
        [
            Button.inline(
                "🌅 06:00",
                f"{prefix}:06:00".encode()
            ),
            Button.inline(
                "🌅 07:00",
                f"{prefix}:07:00".encode()
            ),
            Button.inline(
                "🌅 08:00",
                f"{prefix}:08:00".encode()
            )
        ],
        [
            Button.inline(
                "☀️ 09:00",
                f"{prefix}:09:00".encode()
            ),
            Button.inline(
                "☀️ 10:00",
                f"{prefix}:10:00".encode()
            ),
            Button.inline(
                "☀️ 12:00",
                f"{prefix}:12:00".encode()
            )
        ],
        [
            Button.inline(
                "🌆 18:00",
                f"{prefix}:18:00".encode()
            ),
            Button.inline(
                "🌙 20:00",
                f"{prefix}:20:00".encode()
            ),
            Button.inline(
                "🌙 22:00",
                f"{prefix}:22:00".encode()
            )
        ],
        [
            Button.inline(
                "🔙 Quay lại",
                b"power:back"
            )
        ]
    ]


# ============================================================
# TIME TEXT
# ============================================================

def time_text(
    area_name=None,
    customer=None
):

    text = (
        "╭──────────────────────────╮\n"
        "│  ⏰ <b>CHỌN GIỜ KIỂM TRA</b>  │\n"
        "╰──────────────────────────╯\n\n"
    )

    if area_name:

        text += (
            f"📍 Khu vực: "
            f"<b>{esc(area_name)}</b>\n\n"
        )

    if customer:

        text += (
            f"🆔 Mã KH: "
            f"<code>{esc(customer)}</code>\n\n"
        )

    text += (
        "Chọn thời điểm bot tự động kiểm tra mỗi ngày:\n\n"
        "🔔 Nếu phát hiện lịch mới, bot sẽ gửi thông báo.\n"
        "🔄 Nếu bot khởi động sau giờ kiểm tra, hệ thống "
        "vẫn có thể kiểm tra bù."
    )

    return text


# ============================================================
# FORMAT SCHEDULE
# ============================================================

def format_schedule(
    schedule
):

    code = schedule.get(
        "code",
        ""
    )

    where = schedule.get(
        "where",
        ""
    )

    start_time = schedule.get(
        "start_time",
        ""
    )

    start_date = schedule.get(
        "start_date",
        ""
    )

    end_time = schedule.get(
        "end_time",
        ""
    )

    end_date = schedule.get(
        "end_date",
        ""
    )

    cause = schedule.get(
        "cause",
        ""
    )

    text = (
        "⚡ <b>LỊCH CÚP ĐIỆN</b>\n\n"
    )

    if where:

        text += (
            f"📍 <b>Khu vực:</b> "
            f"{esc(where)}\n"
        )

    if start_time or start_date:

        text += (
            f"🕐 <b>Từ:</b> "
            f"{esc(start_time)} "
            f"{esc(start_date)}\n"
        )

    if end_time or end_date:

        text += (
            f"🕐 <b>Đến:</b> "
            f"{esc(end_time)} "
            f"{esc(end_date)}\n"
        )

    if cause:

        text += (
            f"📝 <b>Lý do:</b> "
            f"{esc(cause)}\n"
        )

    if code:

        text += (
            f"🔖 <b>Mã lịch:</b> "
            f"<code>{esc(code)}</code>\n"
        )

    return text


# ============================================================
# FORMAT RESULT
# ============================================================

def format_result(
    result,
    area_name=None,
    customer=None
):

    schedules = result.get(
        "schedules",
        []
    )

    text = (
        "╭──────────────────────────╮\n"
        "│  ⚡ <b>KẾT QUẢ KIỂM TRA</b>  │\n"
        "╰──────────────────────────╯\n\n"
    )

    if area_name:

        text += (
            f"📍 <b>Khu vực:</b> "
            f"{esc(area_name)}\n"
        )

    if customer:

        text += (
            f"🆔 <b>Mã KH:</b> "
            f"<code>{esc(customer)}</code>\n"
        )

    text += "\n"

    if not schedules:

        text += (
            "✅ <b>Không tìm thấy lịch cúp điện.</b>\n\n"
            "Không có dữ liệu trong khoảng thời gian "
            "tra cứu hiện tại."
        )

        return text

    text += (
        f"📋 Tìm thấy "
        f"<b>{len(schedules)}</b> lịch:\n\n"
    )

    for index, schedule in enumerate(
        schedules,
        1
    ):

        text += (
            f"<b>━━ LỊCH #{index} ━━</b>\n"
        )

        if schedule.get("where"):

            text += (
                f"📍 {esc(schedule.get('where'))}\n"
            )

        if (
            schedule.get("start_time")
            or schedule.get("start_date")
        ):

            text += (
                f"🕐 Từ: "
                f"{esc(schedule.get('start_time'))} "
                f"{esc(schedule.get('start_date'))}\n"
            )

        if (
            schedule.get("end_time")
            or schedule.get("end_date")
        ):

            text += (
                f"🕐 Đến: "
                f"{esc(schedule.get('end_time'))} "
                f"{esc(schedule.get('end_date'))}\n"
            )

        if schedule.get("cause"):

            text += (
                f"📝 {esc(schedule.get('cause'))}\n"
            )

        if schedule.get("code"):

            text += (
                f"🔖 <code>"
                f"{esc(schedule.get('code'))}"
                f"</code>\n"
            )

        text += "\n"

    return text


# ============================================================
# BUILD LIST
# ============================================================

def build_list(user_id):
    subscriptions = get_user_subscriptions(user_id)
    if not subscriptions:
        return (
            "╭──────────────────────────────╮\n"
            "│  📋 <b>LỊCH HẸN CỦA BẠN</b>  │\n"
            "╰──────────────────────────────╯\n\n"
            "📭 <b>Chưa có lịch theo dõi.</b>\n\n"
            "Tạo một lịch mới để hệ thống tự động kiểm tra mỗi ngày.\n\n"
            "🛡️ Scheduler: <b>ACTIVE</b>\n"
            f"🌏 Múi giờ: <code>{esc(CUPDIEN_TZ_NAME)}</code>",
            [[Button.inline("➕ Tạo lịch mới", b"power:type:area")], [Button.inline("🔙 Quay lại", b"power:back")]]
        )

    lines = [
        "╭──────────────────────────────╮",
        "│  📋 <b>LỊCH HẸN CỦA BẠN</b>  │",
        "╰──────────────────────────────╯",
        "",
        f"🛡️ <b>{len(subscriptions)}</b> lịch đang được bảo vệ",
        "💾 Lưu bền vững • 🔄 Restart-safe • ⏰ Daily recovery",
        "",
    ]
    buttons = []
    for index, sub in enumerate(subscriptions, 1):
        sub_type = sub.get("type", "area")
        value = sub.get("value", "")
        area_name = sub.get("area_name", "")
        check_time = sub.get("check_time", "07:00")
        icon = "📍" if sub_type == "area" else "🆔"
        label = area_name or value or "Không xác định"
        status = sub.get("last_status", "NEVER")
        status_icon = "🟢" if status == "SUCCESS" else ("🟠" if status == "NEVER" else "🔴")
        lines += [
            f"<b>━━ #{index} ━━</b>",
            f"{icon} <b>{esc(label)}</b>",
            f"⏰ Hẹn: <code>{esc(check_time)}</code> mỗi ngày",
            f"{status_icon} Trạng thái: <b>{esc(status)}</b>",
            f"🕒 Lần chạy: <code>{esc(sub.get('last_run_at') or 'Chưa chạy')}</code>",
            f"📨 Đã gửi: <b>{int(sub.get('total_notifications') or 0)}</b>",
            "",
        ]
        buttons.append([Button.inline(f"🗑️ Xóa #{index}", f"power:delete:{sub['id']}".encode())])
    buttons += [
        [Button.inline("➕ Thêm lịch", b"power:type:area"), Button.inline("🛡️ Scheduler", b"power:status")],
        [Button.inline("🔙 Quay lại", b"power:back")],
    ]
    return "\n".join(lines), buttons


# ============================================================
# REGISTER
# ============================================================

def register(
    bot,
    notify_bot
):

    # Scheduler is bootstrapped immediately when the command module is loaded.
    # This is the key fix for "it worked yesterday but did not notify today".
    try:
        scheduler = get_power_scheduler(bot, notify_bot)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(scheduler.start(), name="cupdien-scheduler-bootstrap")
        except RuntimeError:
            LOGGER.warning("No running event loop during register; scheduler will start on /cupdien.")
    except Exception:
        LOGGER.exception("Failed to bootstrap CUPDIEN scheduler")

    # ========================================================
    # COMMAND
    # ========================================================

    @bot.on(
        events.NewMessage(
            pattern=r"^/cupdien(?:@\w+)?$"
        )
    )
    async def cupdien_start(
        event
    ):

        # Command mới thay thế toàn bộ task nền cũ của user.
        await replace_user_tasks(event.sender_id)

        # Xóa session chờ của command cũ.
        for _attr in ("_dragon_sessions", "_dragon_download_sessions"):
            _sessions = getattr(bot, _attr, None)
            if isinstance(_sessions, dict):
                _sessions.pop(event.sender_id, None)

        try:
            from core.power.session import clear_session
            clear_session(bot, event.sender_id)
        except Exception:
            pass


        clear_session(
            bot,
            event.sender_id
        )

        scheduler = get_power_scheduler(bot, notify_bot)
        await scheduler.start()
        await scheduler.recovery()

        await event.reply(
            menu_text(),
            buttons=main_menu(),
            parse_mode="html"
        )

    # ========================================================
    # MESSAGE INPUT
    # ========================================================

    @bot.on(
        events.NewMessage()
    )
    async def cupdien_message(
        event
    ):

        if not event.raw_text:

            return

        text = event.raw_text.strip()

        if text.startswith("/"):

            return

        user_id = event.sender_id

        session = get_session(
            bot,
            user_id
        )

        if not session:

            return

        state = session.get(
            "state"
        )

        # ----------------------------------------------------
        # NHẬP MÃ KH
        # ----------------------------------------------------

        if state in (
            "customer_input",
            "instant_customer_input"
        ):

            customer = (
                text
                .strip()
                .upper()
            )

            if len(customer) < 5:

                await event.reply(
                    "❌ Mã khách hàng không hợp lệ.\n\n"
                    "Ví dụ:\n"
                    "<code>PB01050001178</code>",
                    parse_mode="html"
                )

                return

            mode = session.get(
                "mode",
                "customer"
            )

            set_session(
                bot,
                user_id,
                state=(
                    "customer_confirm"
                    if mode == "customer"
                    else "instant_customer_confirm"
                ),
                customer=customer
            )

            area_name = session.get(
                "area_name"
            )

            message = (
                "╭──────────────────────────╮\n"
                "│  🆔 <b>XÁC NHẬN MÃ KH</b>  │\n"
                "╰──────────────────────────╯\n\n"

                f"🆔 Mã KH:\n"
                f"<code>{esc(customer)}</code>\n\n"
            )

            if area_name:

                message += (
                    f"📍 Khu vực: "
                    f"<b>{esc(area_name)}</b>\n\n"
                )

            message += (
                "Nhấn xác nhận để tiếp tục."
            )

            await event.reply(
                message,
                buttons=[
                    [
                        Button.inline(
                            "✅ Xác nhận",
                            f"power:confirmcustomer:{customer}".encode()
                        )
                    ],
                    [
                        Button.inline(
                            "🔙 Quay lại",
                            b"power:back"
                        )
                    ]
                ],
                parse_mode="html"
            )

            return

    # ========================================================
    # CALLBACK
    # ========================================================

    @bot.on(
        events.CallbackQuery(
            pattern=b"power:"
        )
    )
    async def power_callback(
        event
    ):

        user_id = event.sender_id

        try:

            data = event.data.decode(
                "utf-8"
            )

            parts = data.split(
                ":",
                2
            )

            action = (
                parts[1]
                if len(parts) > 1
                else ""
            )

            value = (
                parts[2]
                if len(parts) > 2
                else ""
            )

            # =================================================
            # INSTANT
            # =================================================
                        # =================================================
            # CONFIRM CUSTOMER
            # =================================================

            if action == "confirmcustomer":

                customer = (
                    parts[2]
                    if len(parts) > 2
                    else ""
                )

                customer = customer.strip().upper()

                if not customer:

                    await event.answer(
                        "❌ Mã KH không hợp lệ.",
                        alert=True
                    )

                    return

                session = get_session(
                    bot,
                    user_id
                )

                mode = session.get(
                    "mode",
                    "customer"
                )

                area_code = session.get(
                    "area_code"
                )

                area_name = session.get(
                    "area_name"
                )

                # ------------------------------------------------
                # LƯU MÃ KH VÀO SESSION
                # ------------------------------------------------

                set_session(
                    bot,
                    user_id,
                    customer=customer
                )
                                # ============================================================
                # KIỂM TRA NGAY - KHÔNG BẬT THEO DÕI
                # ============================================================

                if mode in ("instant_customer", "instant_both"):

                    await event.edit(
                        "⏳ <b>ĐANG KIỂM TRA...</b>\n\n"
                        f"🆔 Mã KH: <code>{esc(customer)}</code>\n"
                        + (
                            f"📍 Khu vực: <b>{esc(area_name)}</b>\n"
                            if area_name else ""
                        ),
                        parse_mode="html"
                    )

                    try:

                        tu_ngay, den_ngay = date_range()

                        result = await get_by_customer(
                            customer,
                            tu_ngay,
                            den_ngay
                        )

                        text = format_result(
                            result,
                            area_name=area_name,
                            customer=customer
                        )

                        clear_session(
                            bot,
                            user_id
                        )

                        await event.edit(
                            text,
                            buttons=[
                                [
                                    Button.inline(
                                        "🔙 Quay lại",
                                        b"power:instant"
                                    )
                                ]
                            ],
                            parse_mode="html"
                        )

                    except Exception as e:

                        clear_session(
                            bot,
                            user_id
                        )

                        await event.edit(
                            "❌ <b>Không thể kiểm tra</b>\n\n"
                            f"🆔 Mã KH: <code>{esc(customer)}</code>\n\n"
                            f"⚠️ <code>{esc(e)}</code>",
                            buttons=[
                                [
                                    Button.inline(
                                        "🔙 Quay lại",
                                        b"power:instant"
                                    )
                                ]
                            ],
                            parse_mode="html"
                        )

                    await event.answer()

                    return
                # ------------------------------------------------
                # CUSTOMER ONLY
                # ------------------------------------------------

                if mode == "customer":

                    set_session(
                        bot,
                        user_id,
                        state="time_customer_select",
                        mode="customer",
                        customer=customer
                    )

                    await event.edit(
                        "╭──────────────────────────╮\n"
                        "│  ⏰ <b>CHỌN KHUNG GIỜ</b>  │\n"
                        "╰──────────────────────────╯\n\n"

                        f"🆔 Mã KH: "
                        f"<code>{esc(customer)}</code>\n\n"

                        "Chọn thời gian bot sẽ tự động kiểm tra mỗi ngày.",

                        buttons=time_buttons(
                            "power:timecustomer"
                        ),

                        parse_mode="html"
                    )

                    await event.answer()

                    return

                # ------------------------------------------------
                # BOTH
                # ------------------------------------------------

                if mode == "both":

                    set_session(
                        bot,
                        user_id,
                        state="time_both_select",
                        mode="both",
                        customer=customer
                    )

                    await event.edit(
                        "╭──────────────────────────╮\n"
                        "│  ⏰ <b>CHỌN KHUNG GIỜ</b>  │\n"
                        "╰──────────────────────────╯\n\n"

                        "📍 <b>Khu vực:</b> "
                        f"{esc(area_name or area_code or 'Không xác định')}\n\n"

                        "🆔 <b>Mã KH:</b> "
                        f"<code>{esc(customer)}</code>\n\n"

                        "Chọn thời gian bot sẽ tự động kiểm tra mỗi ngày.",

                        buttons=time_buttons(
                            "power:timeboth"
                        ),

                        parse_mode="html"
                    )

                    await event.answer()

                    return

                # ------------------------------------------------
                # FALLBACK
                # ------------------------------------------------

                set_session(
                    bot,
                    user_id,
                    state="time_customer_select",
                    mode="customer",
                    customer=customer
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  ⏰ <b>CHỌN KHUNG GIỜ</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    f"🆔 Mã KH: "
                    f"<code>{esc(customer)}</code>\n\n"

                    "Chọn thời gian kiểm tra mỗi ngày.",

                    buttons=time_buttons(
                        "power:timecustomer"
                    ),

                    parse_mode="html"
                )

                await event.answer()

                return
            if action == "instant":

                set_session(
                    bot,
                    user_id,
                    state="instant_select",
                    mode="instant"
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  ⚡ <b>KIỂM TRA NGAY</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    "Chọn cách muốn kiểm tra:\n\n"

                    "📍 <b>Theo khu vực</b>\n"
                    "└ Kiểm tra toàn bộ khu vực.\n\n"

                    "🆔 <b>Theo mã KH</b>\n"
                    "└ Kiểm tra theo mã khách hàng.\n\n"

                    "📍🆔 <b>Cả hai</b>\n"
                    "└ Kiểm tra khu vực + mã KH.",

                    buttons=[
                        [
                            Button.inline(
                                "📍 Khu vực",
                                b"power:instant_area"
                            ),
                            Button.inline(
                                "🆔 Mã KH",
                                b"power:instant_customer"
                            )
                        ],
                        [
                            Button.inline(
                                "📍 + 🆔 Cả hai",
                                b"power:instant_both"
                            )
                        ],
                        [
                            Button.inline(
                                "🔙 Quay lại",
                                b"power:back"
                            )
                        ]
                    ],

                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # INSTANT AREA
            # =================================================

            if action == "instant_area":

                set_session(
                    bot,
                    user_id,
                    state="instant_area_select",
                    mode="instant_area"
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  📍 <b>CHỌN KHU VỰC</b>  │\n"
                    "╰──────────────────────────╯\n\n"
                    "Chọn tỉnh / điện lực:",

                    buttons=area_buttons_instant(),

                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # INSTANT AREA SELECT
            # =================================================

            if action == "instantarea":

                area_code = value

                if area_code not in AREA_CODES:

                    await event.answer(
                        "❌ Khu vực không hợp lệ.",
                        alert=True
                    )

                    return

                await event.edit(
                    "⏳ <b>ĐANG KIỂM TRA...</b>\n\n"
                    f"📍 {esc(AREA_CODES[area_code])}\n\n"
                    "Vui lòng chờ...",
                    parse_mode="html"
                )

                try:

                    tu_ngay, den_ngay = date_range()

                    result = await get_by_area(
                        area_code,
                        tu_ngay,
                        den_ngay
                    )

                    text = format_result(
                        result,
                        area_name=AREA_CODES[
                            area_code
                        ]
                    )

                    await event.edit(
                        text,
                        buttons=[
                            [
                                Button.inline(
                                    "🔙 Quay lại",
                                    b"power:instant"
                                )
                            ]
                        ],
                        parse_mode="html"
                    )

                except Exception as e:

                    await event.edit(
                        "❌ <b>Không thể kiểm tra</b>\n\n"
                        f"📍 {esc(AREA_CODES[area_code])}\n\n"
                        f"⚠️ <code>{esc(e)}</code>",
                        parse_mode="html"
                    )

                await event.answer()

                return

            # =================================================
            # INSTANT CUSTOMER
            # =================================================

            if action == "instant_customer":

                set_session(
                    bot,
                    user_id,
                    state="instant_customer_input",
                    mode="instant_customer"
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  🆔 <b>KIỂM TRA MÃ KH</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    "📥 Gửi mã khách hàng cần kiểm tra.\n\n"

                    "Ví dụ:\n"
                    "<code>PB01050001178</code>",

                    buttons=[
                        [
                            Button.inline(
                                "🔙 Quay lại",
                                b"power:instant"
                            )
                        ]
                    ],

                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # INSTANT CUSTOMER CONFIRM
            # =================================================

            if action == "customer_confirm":

                session = get_session(
                    bot,
                    user_id
                )

                customer = session.get(
                    "customer"
                )

                if not customer:

                    await event.answer(
                        "❌ Không tìm thấy mã KH.",
                        alert=True
                    )

                    return

                area_code = session.get(
                    "area_code"
                )

                area_name = session.get(
                    "area_name"
                )

                instant = session.get(
                    "mode"
                ) == "instant_customer"

                

            # =================================================
            # INSTANT BOTH
            # =================================================

            if action == "instant_both":

                set_session(
                    bot,
                    user_id,
                    state="instant_area_select",
                    mode="instant_both"
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  📍 <b>BƯỚC 1/2</b>  │\n"
                    "╰──────────────────────────╯\n\n"
                    "Chọn tỉnh / điện lực:",

                    buttons=area_buttons_instant(),

                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # TYPE
            # =================================================

            if action == "type":

                mode = value

                if mode == "area":

                    set_session(
                        bot,
                        user_id,
                        state="area_select",
                        mode="area"
                    )

                    await event.edit(
                        "📍 <b>CHỌN KHU VỰC</b>\n\n"
                        "Chọn tỉnh / điện lực:",
                        buttons=area_buttons(),
                        parse_mode="html"
                    )

                    await event.answer()

                    return

                if mode == "customer":

                    set_session(
                        bot,
                        user_id,
                        state="customer_input",
                        mode="customer"
                    )

                    await event.edit(
                        "🆔 <b>NHẬP MÃ KHÁCH HÀNG</b>\n\n"
                        "Ví dụ:\n"
                        "<code>PB01050001178</code>",
                        buttons=[
                            [
                                Button.inline(
                                    "🔙 Quay lại",
                                    b"power:back"
                                )
                            ]
                        ],
                        parse_mode="html"
                    )

                    await event.answer()

                    return

                if mode == "both":

                    set_session(
                        bot,
                        user_id,
                        state="area_select",
                        mode="both"
                    )

                    await event.edit(
                        "📍 <b>BƯỚC 1/2 — CHỌN KHU VỰC</b>\n\n"
                        "Chọn tỉnh / điện lực:",
                        buttons=area_buttons(),
                        parse_mode="html"
                    )

                    await event.answer()

                    return

            # =================================================
            # AREA
            # =================================================

            if action == "area":

                area_code = value

                if area_code not in AREA_CODES:

                    await event.answer(
                        "❌ Khu vực không hợp lệ.",
                        alert=True
                    )

                    return

                session = get_session(
                    bot,
                    user_id
                )

                mode = session.get(
                    "mode",
                    "area"
                )

                set_session(
                    bot,
                    user_id,
                    area_code=area_code,
                    area_name=AREA_CODES[
                        area_code
                    ]
                )

                if mode == "area":

                    set_session(
                        bot,
                        user_id,
                        state="time_select"
                    )

                    await event.edit(
                        time_text(
                            area_name=AREA_CODES[
                                area_code
                            ]
                        ),
                        buttons=time_buttons(
                            "power:timearea"
                        ),
                        parse_mode="html"
                    )

                    await event.answer()

                    return

            # =================================================
            # CONFIRM AREA
            # =================================================

            if action == "confirmarea":

                area_code = value

                session = get_session(
                    bot,
                    user_id
                )

                mode = session.get(
                    "mode",
                    "area"
                )

                area_name = AREA_CODES.get(
                    area_code
                )

                if not area_name:

                    await event.answer(
                        "❌ Khu vực không hợp lệ.",
                        alert=True
                    )

                    return

                if mode == "area":

                    set_session(
                        bot,
                        user_id,
                        state="time_select",
                        area_code=area_code,
                        area_name=area_name
                    )

                    await event.edit(
                        time_text(
                            area_name=area_name
                        ),
                        buttons=time_buttons(
                            "power:timearea"
                        ),
                        parse_mode="html"
                    )

                    await event.answer()

                    return

                if mode == "both":

                    set_session(
                        bot,
                        user_id,
                        state="customer_input",
                        area_code=area_code,
                        area_name=area_name
                    )

                    await event.edit(
                        "🆔 <b>BƯỚC 2/2 — NHẬP MÃ KH</b>\n\n"
                        f"📍 Khu vực: "
                        f"<b>{esc(area_name)}</b>\n\n"
                        "Gửi mã khách hàng.\n\n"
                        "Ví dụ:\n"
                        "<code>PB01050001178</code>",
                        parse_mode="html"
                    )

                    await event.answer()

                    return

            # =================================================
            # TIME AREA
            # =================================================

            if action == "timearea":

                check_time = value or "07:00"

                session = get_session(
                    bot,
                    user_id
                )

                area_code = session.get(
                    "area_code"
                )

                area_name = session.get(
                    "area_name"
                )

                if not area_code:

                    await event.answer(
                        "❌ Phiên đã hết.",
                        alert=True
                    )

                    return

                add_subscription(
                    user_id=user_id,
                    sub_type="area",
                    value=area_code,
                    area_code=area_code,
                    area_name=area_name,
                    check_time=check_time
                )

                clear_session(
                    bot,
                    user_id
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  ✅ <b>ĐÃ BẬT THEO DÕI</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    f"📍 Khu vực: "
                    f"<b>{esc(area_name)}</b>\n\n"

                    f"⏰ Kiểm tra: "
                    f"<b>{esc(check_time)}</b> mỗi ngày\n\n"

                    "🔔 Khi có lịch mới → bot tự gửi.",
                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # TIME CUSTOMER
            # =================================================

            if action == "timecustomer":

                check_time = value or "07:00"

                session = get_session(
                    bot,
                    user_id
                )

                customer = session.get(
                    "customer"
                )

                if not customer:

                    await event.answer(
                        "❌ Không tìm thấy mã KH.",
                        alert=True
                    )

                    return

                add_subscription(
                    user_id=user_id,
                    sub_type="customer",
                    value=customer,
                    area_code=session.get(
                        "area_code"
                    ),
                    area_name=session.get(
                        "area_name"
                    ),
                    check_time=check_time
                )

                clear_session(
                    bot,
                    user_id
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  ✅ <b>ĐÃ BẬT THEO DÕI</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    f"🆔 Mã KH: "
                    f"<code>{esc(customer)}</code>\n\n"

                    f"⏰ Kiểm tra: "
                    f"<b>{esc(check_time)}</b> mỗi ngày\n\n"

                    "🔔 Khi có lịch mới → bot tự gửi.",
                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # TIME BOTH
            # =================================================

            if action == "timeboth":

                check_time = value or "07:00"

                session = get_session(
                    bot,
                    user_id
                )

                customer = session.get(
                    "customer"
                )

                area_code = session.get(
                    "area_code"
                )

                area_name = session.get(
                    "area_name"
                )

                if not customer or not area_code:

                    await event.answer(
                        "❌ Thiếu thông tin.",
                        alert=True
                    )

                    return

                add_subscription(
                    user_id=user_id,
                    sub_type="customer",
                    value=customer,
                    area_code=area_code,
                    area_name=area_name,
                    check_time=check_time
                )

                add_subscription(
                    user_id=user_id,
                    sub_type="area",
                    value=area_code,
                    area_code=area_code,
                    area_name=area_name,
                    check_time=check_time
                )

                clear_session(
                    bot,
                    user_id
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  ✅ <b>ĐÃ BẬT THEO DÕI</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    f"📍 Khu vực: "
                    f"<b>{esc(area_name)}</b>\n\n"

                    f"🆔 Mã KH: "
                    f"<code>{esc(customer)}</code>\n\n"

                    f"⏰ Kiểm tra: "
                    f"<b>{esc(check_time)}</b> mỗi ngày\n\n"

                    "📍 Theo khu vực: ✅\n"
                    "🆔 Theo mã KH: ✅\n\n"

                    "🔔 Khi có lịch mới → bot tự gửi.",
                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # SCHEDULER STATUS
            # =================================================

            if action == "status":
                scheduler = get_power_scheduler(bot, notify_bot)
                await scheduler.start()
                await event.edit(
                    _scheduler_status_text(),
                    buttons=[[Button.inline("📋 Lịch của tôi", b"power:list")], [Button.inline("🔙 Quay lại", b"power:back")]],
                    parse_mode="html",
                )
                await event.answer("Scheduler đang hoạt động")
                return

            # =================================================
            # LIST
            # =================================================

            if action == "list":

                text, buttons = build_list(
                    user_id
                )

                await event.edit(
                    text,
                    buttons=buttons,
                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # DELETE
            # =================================================

            if action == "delete":

                sub_id = value

                subscriptions = (
                    get_user_subscriptions(
                        user_id
                    )
                )

                target = next(
                    (
                        item
                        for item in subscriptions
                        if str(item.get("id"))
                        == str(sub_id)
                    ),
                    None
                )

                if not target:

                    await event.answer(
                        "❌ Không tìm thấy đăng ký.",
                        alert=True
                    )

                    return

                remove_subscription(
                    sub_id
                )

                await event.answer(
                    "✅ Đã xóa theo dõi."
                )

                text, buttons = build_list(
                    user_id
                )

                await event.edit(
                    text,
                    buttons=buttons,
                    parse_mode="html"
                )

                return

            # =================================================
            # BACK
            # =================================================

            if action == "back":

                clear_session(
                    bot,
                    user_id
                )

                await event.edit(
                    menu_text(),
                    buttons=main_menu(),
                    parse_mode="html"
                )

                await event.answer()

                return

            await event.answer()

        except Exception as e:

            print(
                f"[POWER CALLBACK ERROR] "
                f"{type(e).__name__}: {e}"
            )

            try:

                await event.answer(
                    "❌ Có lỗi xảy ra.",
                    alert=True
                )

            except Exception:

                pass

# ============================================================
# CUPDIEN HARDENED V5 — PRODUCTION SCHEDULER / HA / RECOVERY
# ============================================================
# This layer supersedes the earlier V4 runtime objects.
# Design goals:
#   * The database is the source of truth, never an asyncio task.
#   * Appointments are NEVER deleted by date rollover.
#   * A restart/redeploy cannot consume an appointment.
#   * A slow API call cannot block unrelated subscriptions forever.
#   * Multiple bot workers cannot intentionally run the same job at once.
#   * A successful Telegram send is recorded with an idempotency key.
#   * Failed attempts remain retryable.
#   * Clock rollover is explicit and timezone-aware.
#   * Corrupt/partial scheduler state is tolerated and repaired.
#   * UI can inspect scheduler health without changing scheduling state.
# ============================================================

import contextlib as _cd_contextlib
import secrets as _cd_secrets
from datetime import datetime as _CDDateTime
from datetime import timedelta as _CDTimedelta
from typing import Iterable as _CDIterable

CUPDIEN_HARDENED_VERSION = "5.0.0-HARDENED"
CUPDIEN_LEASE_SECONDS = max(30, int(os.getenv("CUPDIEN_LEASE_SECONDS", "90")))
CUPDIEN_API_TIMEOUT = max(10, int(os.getenv("CUPDIEN_API_TIMEOUT", "45")))
CUPDIEN_RETRY_LIMIT = max(1, int(os.getenv("CUPDIEN_RETRY_LIMIT", "4")))
CUPDIEN_STALE_AFTER_SECONDS = max(60, int(os.getenv("CUPDIEN_STALE_AFTER_SECONDS", "180")))
CUPDIEN_OWNER = os.getenv("CUPDIEN_INSTANCE_ID", "").strip() or (
    f"{os.getpid()}-{_cd_secrets.token_hex(5)}"
)
_CUPDIEN_V5_SCHEDULERS = {}


def _cd_now():
    return datetime.now(POWER_TZ)


def _cd_date(now=None):
    return (now or _cd_now()).date().isoformat()


def _cd_time(now=None):
    return (now or _cd_now()).strftime("%H:%M")


def _cd_db_init_hardened():
    with power_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS scheduler_leases (
            resource TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS execution_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            run_date TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT DEFAULT '',
            fingerprint TEXT DEFAULT '',
            error TEXT DEFAULT '',
            owner TEXT NOT NULL,
            UNIQUE(subscription_id, run_date, attempt)
        );

        CREATE INDEX IF NOT EXISTS idx_execution_runs_due
            ON execution_runs(subscription_id, run_date, state);

        CREATE TABLE IF NOT EXISTS scheduler_metrics (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scheduler_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            subscription_id INTEGER,
            user_id INTEGER,
            run_date TEXT DEFAULT '',
            owner TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_scheduler_audit_created
            ON scheduler_audit(created_at);
        """)


_cd_db_init_hardened()


def _cd_metric(key, delta=1):
    try:
        with power_db() as db:
            row = db.execute(
                "SELECT value FROM scheduler_metrics WHERE key=?",
                (key,),
            ).fetchone()
            value = int(row["value"]) if row else 0
            value += int(delta)
            db.execute(
                """
                INSERT INTO scheduler_metrics(key,value,updated_at)
                VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, value, power_iso()),
            )
    except Exception:
        LOGGER.exception("metric update failed: %s", key)


def _cd_audit(event_type, subscription_id=None, user_id=None,
              run_date="", detail=""):
    try:
        with power_db() as db:
            db.execute(
                """
                INSERT INTO scheduler_audit(
                    event_type,subscription_id,user_id,run_date,owner,detail,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    str(event_type)[:100],
                    subscription_id,
                    user_id,
                    str(run_date or ""),
                    CUPDIEN_OWNER,
                    str(detail or "")[:2000],
                    power_iso(),
                ),
            )
            db.execute(
                """
                DELETE FROM scheduler_audit
                WHERE id NOT IN (
                    SELECT id FROM scheduler_audit
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (CUPDIEN_MAX_HISTORY,),
            )
    except Exception:
        LOGGER.exception("audit write failed")


def _cd_try_lease(resource, seconds=CUPDIEN_LEASE_SECONDS):
    now = _cd_now()
    expires = now + _CDTimedelta(seconds=seconds)
    with power_db() as db:
        row = db.execute(
            "SELECT owner,expires_at FROM scheduler_leases WHERE resource=?",
            (resource,),
        ).fetchone()
        if row:
            try:
                old_expiry = datetime.fromisoformat(str(row["expires_at"]))
            except Exception:
                old_expiry = now - _CDTimedelta(seconds=1)
            if old_expiry > now and row["owner"] != CUPDIEN_OWNER:
                return False
        db.execute(
            """
            INSERT INTO scheduler_leases(
                resource,owner,acquired_at,expires_at,heartbeat_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(resource) DO UPDATE SET
                owner=excluded.owner,
                acquired_at=excluded.acquired_at,
                expires_at=excluded.expires_at,
                heartbeat_at=excluded.heartbeat_at
            """,
            (resource, CUPDIEN_OWNER, power_iso(now), power_iso(expires), power_iso(now)),
        )
    return True


def _cd_release_lease(resource):
    with power_db() as db:
        db.execute(
            "DELETE FROM scheduler_leases WHERE resource=? AND owner=?",
            (resource, CUPDIEN_OWNER),
        )


def _cd_heartbeat(resource, seconds=CUPDIEN_LEASE_SECONDS):
    now = _cd_now()
    expires = now + _CDTimedelta(seconds=seconds)
    with power_db() as db:
        db.execute(
            """
            UPDATE scheduler_leases
            SET heartbeat_at=?, expires_at=?
            WHERE resource=? AND owner=?
            """,
            (power_iso(now), power_iso(expires), resource, CUPDIEN_OWNER),
        )


def _cd_claim_run(sub, run_date):
    sub_id = int(sub["id"])
    user_id = int(sub["user_id"])
    now = power_iso()
    resource = f"subscription:{sub_id}:{run_date}"
    if not _cd_try_lease(resource):
        return False
    try:
        with power_db() as db:
            existing = db.execute(
                """
                SELECT id,state
                FROM execution_runs
                WHERE subscription_id=? AND run_date=?
                ORDER BY id DESC LIMIT 1
                """,
                (sub_id, run_date),
            ).fetchone()
            if existing and existing["state"] == "SUCCESS":
                _cd_release_lease(resource)
                return False
            attempt = (
                int(
                    db.execute(
                        """
                        SELECT COALESCE(MAX(attempt),0)
                        FROM execution_runs
                        WHERE subscription_id=? AND run_date=?
                        """,
                        (sub_id, run_date),
                    ).fetchone()[0]
                )
                + 1
            )
            db.execute(
                """
                INSERT INTO execution_runs(
                    subscription_id,user_id,run_date,attempt,state,started_at,owner
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    sub_id,
                    user_id,
                    run_date,
                    attempt,
                    "RUNNING",
                    now,
                    CUPDIEN_OWNER,
                ),
            )
        _cd_audit("RUN_CLAIMED", sub_id, user_id, run_date, f"attempt={attempt}")
        _cd_metric("runs_claimed")
        return True
    except Exception:
        _cd_release_lease(resource)
        raise


def _cd_finish_run(sub_id, run_date, state, fingerprint="", error=""):
    with power_db() as db:
        db.execute(
            """
            UPDATE execution_runs
            SET state=?,finished_at=?,fingerprint=?,error=?
            WHERE subscription_id=? AND run_date=? AND owner=? AND state='RUNNING'
            """,
            (
                state,
                power_iso(),
                fingerprint[:128],
                str(error or "")[:2000],
                int(sub_id),
                run_date,
                CUPDIEN_OWNER,
            ),
        )
    _cd_release_lease(f"subscription:{int(sub_id)}:{run_date}")


def _cd_success_exists(sub_id, run_date):
    with power_db() as db:
        row = db.execute(
            """
            SELECT id FROM execution_runs
            WHERE subscription_id=? AND run_date=? AND state='SUCCESS'
            LIMIT 1
            """,
            (int(sub_id), run_date),
        ).fetchone()
    return bool(row)


def _cd_cleanup_stale_runs():
    cutoff = _cd_now() - _CDTimedelta(seconds=CUPDIEN_STALE_AFTER_SECONDS)
    with power_db() as db:
        rows = db.execute(
            """
            SELECT id,subscription_id,run_date
            FROM execution_runs
            WHERE state='RUNNING' AND started_at < ?
            """,
            (power_iso(cutoff),),
        ).fetchall()
        for row in rows:
            db.execute(
                """
                UPDATE execution_runs
                SET state='STALE',finished_at=?,error=?
                WHERE id=? AND state='RUNNING'
                """,
                (
                    power_iso(),
                    "stale worker recovered by watchdog",
                    row["id"],
                ),
            )
    for row in rows:
        _cd_release_lease(
            f"subscription:{int(row['subscription_id'])}:{row['run_date']}"
        )
    if rows:
        _cd_metric("stale_runs_recovered", len(rows))


def _cd_next_due(sub, now=None):
    now = now or _cd_now()
    check_time = _normalize_time(sub.get("check_time"))
    hh, mm = map(int, check_time.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    today = now.date().isoformat()
    last = str(sub.get("last_run_date") or "")
    if last == today:
        return False, target, "DONE_TODAY"
    if now >= target:
        return True, target, "DUE"
    return False, target, "FUTURE"


def _cd_fingerprint(result):
    return _fingerprint_result(result)[0]


def _cd_normalize_result(result):
    if not isinstance(result, dict):
        return {"schedules": []}
    schedules = result.get("schedules")
    if not isinstance(schedules, list):
        schedules = []
    clean = []
    for item in schedules:
        if not isinstance(item, dict):
            continue
        clean.append({
            "code": str(item.get("code") or "").strip(),
            "where": str(item.get("where") or "").strip(),
            "start_time": str(item.get("start_time") or "").strip(),
            "start_date": str(item.get("start_date") or "").strip(),
            "end_time": str(item.get("end_time") or "").strip(),
            "end_date": str(item.get("end_date") or "").strip(),
            "cause": str(item.get("cause") or "").strip(),
        })
    return {"schedules": clean}


async def _cd_query_subscription(sub):
    mode = str(sub.get("type") or "area").lower()
    value = str(sub.get("value") or "").strip()
    area = str(sub.get("area_code") or "").strip()
    if not value and not area:
        raise ValueError("subscription has no target")
    tu, den = date_range()
    if mode == "customer":
        coro = get_by_customer(value, tu, den)
    else:
        coro = get_by_area(area or value, tu, den)
    return await asyncio.wait_for(coro, timeout=CUPDIEN_API_TIMEOUT)


def _cd_should_notify(schedules):
    return bool(schedules) or CUPDIEN_NOTIFY_EMPTY


def _cd_save_subscription(sub_type, value, area_code="", area_name="",
                          check_time="07:00", user_id=0):
    check_time = _normalize_time(check_time)
    value = str(value or "").strip()
    area_code = str(area_code or "").strip()
    area_name = str(area_name or "").strip()
    if not value:
        raise ValueError("empty subscription value")
    now = power_iso()
    with power_db() as db:
        db.execute(
            """
            INSERT INTO subscriptions_v2(
                user_id,type,value,area_code,area_name,check_time,
                enabled,created_at,updated_at,last_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id,type,value,area_code,check_time)
            DO UPDATE SET
                area_name=excluded.area_name,
                enabled=1,
                updated_at=excluded.updated_at
            """,
            (
                int(user_id),
                str(sub_type),
                value,
                area_code,
                area_name,
                check_time,
                1,
                now,
                now,
                "NEVER",
            ),
        )
        row = db.execute(
            """
            SELECT * FROM subscriptions_v2
            WHERE user_id=? AND type=? AND value=?
              AND area_code=? AND check_time=?
            """,
            (int(user_id), str(sub_type), value, area_code, check_time),
        ).fetchone()
    _cd_audit("SUBSCRIPTION_SAVED", row["id"] if row else None, user_id,
              detail=f"{sub_type}:{value}@{check_time}")
    return _row_dict(row)


def _cd_remove_subscription(sub_id, user_id=None):
    with power_db() as db:
        if user_id is None:
            row = db.execute(
                "SELECT * FROM subscriptions_v2 WHERE id=?",
                (int(sub_id),),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM subscriptions_v2 WHERE id=? AND user_id=?",
                (int(sub_id), int(user_id)),
            ).fetchone()
        if not row:
            return False
        db.execute(
            "DELETE FROM subscriptions_v2 WHERE id=?",
            (int(sub_id),),
        )
    # Keep the legacy store synchronized so a compatibility migration can
    # never resurrect an appointment that the user explicitly deleted.
    legacy_id = row["legacy_id"]
    if legacy_id is not None:
        try:
            legacy_remove_subscription(int(legacy_id))
        except Exception as exc:
            LOGGER.warning("legacy deletion sync failed: %s", exc)
    _cd_audit("SUBSCRIPTION_DELETED", sub_id, row["user_id"],
              detail="explicit user deletion")
    return True


def _cd_list_user(user_id):
    with power_db() as db:
        rows = db.execute(
            """
            SELECT * FROM subscriptions_v2
            WHERE user_id=? AND enabled=1
            ORDER BY check_time,id
            """,
            (int(user_id),),
        ).fetchall()
    if rows:
        return [_row_dict(row) for row in rows]

    # Compatibility migration: recover schedules created by the legacy
    # database the first time that user opens the scheduler.
    try:
        legacy_rows = legacy_get_user_subscriptions(int(user_id)) or []
    except Exception as exc:
        LOGGER.warning("legacy compatibility read failed: %s", exc)
        legacy_rows = []
    migrated = []
    for raw in legacy_rows:
        try:
            item = _legacy_to_v2(int(user_id), raw)
            migrated.append(
                _cd_save_subscription(
                    item["type"],
                    item["value"],
                    item["area_code"],
                    item["area_name"],
                    item["check_time"],
                    int(user_id),
                )
            )
        except Exception as exc:
            LOGGER.error("legacy schedule migration failed: %s", exc)
    return [x for x in migrated if x]


def _cd_repair_database():
    with power_db() as db:
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        db.execute("PRAGMA optimize")
        db.execute(
            """
            UPDATE subscriptions_v2
            SET check_time='07:00'
            WHERE check_time IS NULL OR check_time=''
            """
        )
        db.execute(
            """
            UPDATE subscriptions_v2
            SET enabled=1
            WHERE enabled IS NULL
            """
        )
    _cd_metric("database_repairs")


async def _cd_process_one(sub, catchup=False):
    now = _cd_now()
    run_date = _cd_date(now)
    sub_id = int(sub["id"])
    user_id = int(sub["user_id"])
    if _cd_success_exists(sub_id, run_date):
        return "ALREADY_SUCCESS"
    if not _cd_claim_run(sub, run_date):
        return "LOCKED_OR_DONE"

    started = time.monotonic()
    try:
        result = await _cd_query_subscription(sub)
        result = _cd_normalize_result(result)
        fingerprint = _cd_fingerprint(result)
        schedules = result["schedules"]

        existing = _ledger_exists(user_id, sub_id, run_date, fingerprint)
        sent = False

        if existing and existing.get("status") == "SENT":
            sent = False
        elif _cd_should_notify(schedules):
            text = _notification_text(
                sub,
                result,
                fingerprint,
                catchup=catchup,
            )
            await asyncio.wait_for(
                get_power_scheduler.__globals__.get("_CUPDIEN_NOTIFY_BOT", None).send_message(
                    user_id,
                    text,
                    parse_mode="html",
                    link_preview=False,
                ),
                timeout=CUPDIEN_API_TIMEOUT,
            )
            sent = True
            _record_ledger(
                user_id,
                sub_id,
                run_date,
                fingerprint,
                "SENT",
            )
        else:
            _record_ledger(
                user_id,
                sub_id,
                run_date,
                fingerprint,
                "CHECKED",
            )

        _mark_run(
            sub_id,
            run_date=run_date,
            fingerprint=fingerprint,
            status="SUCCESS",
            error="",
            notified=sent,
        )
        _cd_finish_run(sub_id, run_date, "SUCCESS", fingerprint)
        _cd_metric("runs_success")
        _cd_audit(
            "RUN_SUCCESS",
            sub_id,
            user_id,
            run_date,
            f"notify={sent};items={len(schedules)};elapsed={time.monotonic()-started:.2f}s",
        )
        return "SUCCESS"
    except asyncio.CancelledError:
        _cd_finish_run(sub_id, run_date, "CANCELLED", error="worker cancelled")
        _cd_metric("runs_cancelled")
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            _mark_run(
                sub_id,
                run_date=run_date,
                fingerprint="ERROR",
                status="ERROR",
                error=error,
                notified=False,
            )
        finally:
            _cd_finish_run(sub_id, run_date, "ERROR", error=error)
        _cd_metric("runs_error")
        _cd_audit("RUN_ERROR", sub_id, user_id, run_date, error)
        raise


class HardenedPowerScheduler:
    """
    Wall-clock scheduler with durable state.

    It never sleeps until an appointment. It wakes frequently, reads SQLite,
    determines due work from today's date and configured HH:MM, then claims
    each subscription using a database lease. This is resilient to restarts,
    time jumps, deploys and overlapping worker processes.
    """

    def __init__(self, bot, notify_bot=None):
        self.bot = bot
        self.notify_bot = notify_bot or bot
        self.task = None
        self.running = False
        self.stop_event = asyncio.Event()
        self.started_at = power_iso()
        self.instance = CUPDIEN_OWNER
        self.last_tick = ""
        self.last_error = ""
        self.tick_count = 0
        self._tick_lock = asyncio.Lock()

    async def start(self):
        global _CUPDIEN_NOTIFY_BOT
        _CUPDIEN_NOTIFY_BOT = self.notify_bot
        if self.task and not self.task.done():
            return
        self.stop_event = asyncio.Event()
        self.running = True
        self.started_at = power_iso()
        _cd_repair_database()
        _cd_cleanup_stale_runs()
        self.task = asyncio.create_task(
            self.run(),
            name=f"cupdien-hardened-{id(self)}",
        )
        _set_scheduler_state("started_at", self.started_at)
        _set_scheduler_state("instance", self.instance)
        _set_scheduler_state("version", CUPDIEN_HARDENED_VERSION)
        _cd_audit("SCHEDULER_STARTED", detail=self.instance)

    async def stop(self):
        self.running = False
        self.stop_event.set()
        task = self.task
        self.task = None
        if task and not task.done():
            task.cancel()
            with _cd_contextlib.suppress(asyncio.CancelledError):
                await task
        _cd_audit("SCHEDULER_STOPPED", detail=self.instance)

    async def run(self):
        await asyncio.sleep(0.5)
        while not self.stop_event.is_set():
            try:
                await self.tick()
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.exception("hardened scheduler tick failed")
                _set_scheduler_state("last_error", self.last_error)
                _cd_metric("tick_errors")
            self.last_tick = power_iso()
            self.tick_count += 1
            _set_scheduler_state("last_tick", self.last_tick)
            _set_scheduler_state("tick_count", self.tick_count)
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=CUPDIEN_SCHEDULER_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass

    async def tick(self):
        async with self._tick_lock:
            _cd_cleanup_stale_runs()
            now = _cd_now()
            subscriptions = durable_get_all_subscriptions()
            if not subscriptions:
                return
            for sub in subscriptions:
                due, target, reason = _cd_next_due(sub, now)
                if not due:
                    continue
                try:
                    await _cd_process_one(sub, catchup=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One broken subscription must never stop the whole scheduler.
                    LOGGER.exception(
                        "subscription failed: id=%s target=%s",
                        sub.get("id"),
                        target,
                    )

    async def recovery(self):
        """
        Immediate recovery after startup.

        If the bot starts at 10:00 and a daily 07:00 appointment has not been
        successfully consumed today, it is due and will be processed now.
        Tomorrow's appointment is never run early.
        """
        now = _cd_now()
        for sub in durable_get_all_subscriptions():
            due, _, _ = _cd_next_due(sub, now)
            if not due:
                continue
            try:
                await _cd_process_one(sub, catchup=True)
            except Exception:
                LOGGER.exception("startup recovery failed: %s", sub.get("id"))

    def health(self):
        with power_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS n FROM subscriptions_v2 WHERE enabled=1"
            ).fetchone()["n"]
            errors = db.execute(
                """
                SELECT COUNT(*) AS n FROM execution_runs
                WHERE state='ERROR'
                AND started_at >= ?
                """,
                (power_iso(_cd_now() - _CDTimedelta(hours=24)),),
            ).fetchone()["n"]
        return {
            "version": CUPDIEN_HARDENED_VERSION,
            "running": bool(self.running and self.task and not self.task.done()),
            "instance": self.instance,
            "timezone": CUPDIEN_TZ_NAME,
            "now": power_iso(),
            "subscriptions": int(count),
            "errors_24h": int(errors),
            "last_tick": self.last_tick,
            "last_error": self.last_error,
            "tick_count": int(self.tick_count),
        }


async def _cd_global_bootstrap(bot, notify_bot):
    scheduler = get_power_scheduler(bot, notify_bot)
    await scheduler.start()
    # Do not perform a long recovery on the command handler.
    # Startup recovery is isolated in its own task.
    try:
        await scheduler.recovery()
    except Exception:
        LOGGER.exception("global startup recovery failed")


def get_power_scheduler(bot, notify_bot=None):
    global _CUPDIEN_NOTIFY_BOT
    key = id(bot)
    scheduler = _CUPDIEN_V5_SCHEDULERS.get(key)
    if scheduler is None:
        scheduler = HardenedPowerScheduler(bot, notify_bot)
        _CUPDIEN_V5_SCHEDULERS[key] = scheduler
    elif notify_bot is not None:
        scheduler.notify_bot = notify_bot
    _CUPDIEN_NOTIFY_BOT = scheduler.notify_bot
    return scheduler


# Canonical V5 storage. The old storage API remains compatible with the UI.
def add_subscription(*, user_id, sub_type, value, area_code=None,
                     area_name=None, check_time="07:00"):
    return _cd_save_subscription(
        sub_type=sub_type,
        value=value,
        area_code=area_code or "",
        area_name=area_name or "",
        check_time=check_time,
        user_id=user_id,
    )


def get_user_subscriptions(user_id):
    return _cd_list_user(user_id)


def remove_subscription(sub_id):
    return _cd_remove_subscription(sub_id)


def _cd_metrics_snapshot():
    with power_db() as db:
        rows = db.execute(
            "SELECT key,value FROM scheduler_metrics ORDER BY key"
        ).fetchall()
    return {str(row["key"]): int(row["value"]) for row in rows}


def _cd_recent_audit(limit=20):
    with power_db() as db:
        rows = db.execute(
            """
            SELECT event_type,subscription_id,user_id,run_date,
                   owner,detail,created_at
            FROM scheduler_audit
            ORDER BY id DESC LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def _cd_health_text(scheduler):
    h = scheduler.health()
    metrics = _cd_metrics_snapshot()
    icon = "🟢" if h["running"] else "🔴"
    return "\n".join([
        "╭────────────────────────────────╮",
        "│  🛡️ <b>POWER WATCH • HARDENED</b>  │",
        "╰────────────────────────────────╯",
        "",
        f"{icon} <b>Scheduler:</b> {'ACTIVE' if h['running'] else 'STOPPED'}",
        f"⚙️ <b>Engine:</b> <code>{esc(h['version'])}</code>",
        f"🌏 <b>Timezone:</b> <code>{esc(h['timezone'])}</code>",
        f"🕒 <b>Now:</b> <code>{esc(h['now'])}</code>",
        f"📋 <b>Lịch đang bảo vệ:</b> <b>{h['subscriptions']}</b>",
        f"💓 <b>Tick:</b> <b>{h['tick_count']}</b>",
        f"⚠️ <b>Lỗi 24h:</b> <b>{h['errors_24h']}</b>",
        "",
        "🔐 <b>Durability</b>",
        "├ SQLite WAL + FULL sync",
        "├ Database lease chống chạy trùng",
        "├ Idempotency ledger chống gửi trùng",
        "├ Startup recovery",
        "└ Không xóa lịch khi sang ngày",
        "",
        "📊 <b>Runtime metrics</b>",
        f"├ Thành công: <b>{metrics.get('runs_success', 0)}</b>",
        f"├ Lỗi: <b>{metrics.get('runs_error', 0)}</b>",
        f"├ Claim: <b>{metrics.get('runs_claimed', 0)}</b>",
        f"└ Recovery stale: <b>{metrics.get('stale_runs_recovered', 0)}</b>",
        "",
        "🧠 <i>Nguồn sự thật = database, không phải task RAM.</i>",
    ])


def _cd_list_text(user_id):
    rows = get_user_subscriptions(user_id)
    if not rows:
        return (
            "╭────────────────────────────────╮\n"
            "│  📋 <b>LỊCH HẸN ĐƯỢC BẢO VỆ</b>  │\n"
            "╰────────────────────────────────╯\n\n"
            "📭 <b>Chưa có lịch hẹn.</b>\n\n"
            "💾 Lịch mới sẽ được lưu bền vững và không tự mất khi sang ngày."
        ), [[
            Button.inline("➕ Tạo lịch", b"power:type:area"),
            Button.inline("🔙 Quay lại", b"power:back"),
        ]]

    lines = [
        "╭────────────────────────────────╮",
        "│  📋 <b>LỊCH HẸN ĐƯỢC BẢO VỆ</b>  │",
        "╰────────────────────────────────╯",
        "",
        f"🛡️ <b>{len(rows)}</b> lịch • <b>Persistent</b> • <b>Recovery</b>",
        "",
    ]
    buttons = []
    for idx, sub in enumerate(rows, 1):
        typ = "📍 Khu vực" if sub.get("type") == "area" else "🆔 Mã KH"
        label = sub.get("area_name") or sub.get("value") or "Không xác định"
        status = sub.get("last_status") or "NEVER"
        status_icon = {
            "SUCCESS": "🟢",
            "ERROR": "🔴",
            "NEVER": "⚪",
        }.get(status, "🟡")
        lines.extend([
            f"<b>━━ #{idx} • {esc(typ)} ━━</b>",
            f"🎯 <b>{esc(label)}</b>",
            f"⏰ Mỗi ngày: <code>{esc(sub.get('check_time'))}</code>",
            f"{status_icon} {esc(status)}",
            f"🕒 Lần chạy: <code>{esc(sub.get('last_run_at') or 'Chưa chạy')}</code>",
            f"📨 Thông báo: <b>{int(sub.get('total_notifications') or 0)}</b>",
            "",
        ])
        buttons.append([
            Button.inline(
                f"🗑️ Xóa #{idx}",
                f"power:delete:{sub['id']}".encode(),
            )
        ])
    buttons.extend([
        [
            Button.inline("➕ Thêm", b"power:type:area"),
            Button.inline("🛡️ Health", b"power:status"),
        ],
        [Button.inline("🔙 Quay lại", b"power:back")],
    ])
    return "\n".join(lines), buttons


def _cd_menu_text():
    now = _cd_now()
    scheduler = next(iter(_CUPDIEN_V5_SCHEDULERS.values()), None)
    active = bool(scheduler and scheduler.running)
    return "\n".join([
        "╭────────────────────────────────╮",
        "│  ⚡ <b>POWER WATCH • CONTROL</b>  │",
        "╰────────────────────────────────╯",
        "",
        "🚀 <b>Trung tâm theo dõi lịch cúp điện</b>",
        "",
        "⚡ <b>Kiểm tra ngay</b>",
        "└ Tra cứu dữ liệu điện lực theo thời điểm hiện tại.",
        "",
        "🔔 <b>Đặt lịch tự động</b>",
        "└ Scheduler kiểm tra mỗi ngày đúng giờ.",
        "",
        "🛡️ <b>Hệ thống bảo vệ</b>",
        "└ Lịch được lưu vào SQLite, không phụ thuộc RAM.",
        "└ Restart / redeploy không làm mất lịch.",
        "└ Nếu bot khởi động sau giờ hẹn → recovery.",
        "└ Chống chạy trùng và chống gửi trùng.",
        "",
        f"{'🟢' if active else '🔴'} Scheduler: <b>{'ACTIVE' if active else 'OFFLINE'}</b>",
        f"🌏 <code>{esc(CUPDIEN_TZ_NAME)}</code> • 🕒 <code>{now.strftime('%d/%m/%Y %H:%M:%S')}</code>",
    ])


def _cd_patch_ui_globals():
    """
    Rebind UI-facing global functions to the hardened persistent store.
    Kept as a function so the integration is explicit and testable.
    """
    globals()["add_subscription"] = add_subscription
    globals()["get_user_subscriptions"] = get_user_subscriptions
    globals()["remove_subscription"] = remove_subscription
    globals()["menu_text"] = _cd_menu_text
    globals()["build_list"] = _cd_list_text


_cd_patch_ui_globals()


def _cd_start_scheduler_from_register(bot, notify_bot):
    async def runner():
        scheduler = get_power_scheduler(bot, notify_bot)
        await scheduler.start()
        await scheduler.recovery()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(runner(), name="cupdien-hardened-bootstrap")


async def _cd_safe_scheduler_status(bot, notify_bot):
    scheduler = get_power_scheduler(bot, notify_bot)
    await scheduler.start()
    return _cd_health_text(scheduler)


def cupdien_hardened_shutdown(bot):
    scheduler = _CUPDIEN_V5_SCHEDULERS.get(id(bot))
    if scheduler:
        return scheduler.stop()
    return None


def cupdien_hardened_health(bot):
    scheduler = _CUPDIEN_V5_SCHEDULERS.get(id(bot))
    if not scheduler:
        return {
            "running": False,
            "version": CUPDIEN_HARDENED_VERSION,
            "subscriptions": len(durable_get_all_subscriptions()),
        }
    return scheduler.health()


# ============================================================
# V5 REGRESSION TESTS
# ============================================================

def cupdien_selftest_storage():
    _cd_db_init_hardened()
    now = _cd_now()
    assert _cd_date(now) == now.date().isoformat()
    assert _normalize_time("7:5") == "07:05"
    assert _normalize_time("99:99") == "07:00"
    assert _normalize_time("23:59") == "23:59"
    return True


def cupdien_selftest_due_logic():
    base = _CDDateTime(2026, 9, 13, 9, 0, tzinfo=POWER_TZ)
    sub = {"check_time": "07:00", "last_run_date": ""}
    due, _, reason = _cd_next_due(sub, base)
    assert due and reason == "DUE"
    sub["last_run_date"] = "2026-09-13"
    due, _, reason = _cd_next_due(sub, base)
    assert not due and reason == "DONE_TODAY"
    sub["last_run_date"] = ""
    due, _, reason = _cd_next_due(
        {"check_time": "18:00", "last_run_date": ""},
        base,
    )
    assert not due and reason == "FUTURE"
    return True


def cupdien_selftest_rollover():
    before = _CDDateTime(2026, 9, 13, 23, 59, tzinfo=POWER_TZ)
    after = _CDDateTime(2026, 9, 14, 0, 1, tzinfo=POWER_TZ)
    assert _cd_date(before) != _cd_date(after)
    sub = {"check_time": "00:00", "last_run_date": "2026-09-13"}
    due, _, reason = _cd_next_due(sub, after)
    assert due and reason == "DUE"
    return True


def cupdien_selftest_fingerprint():
    a = {"schedules": [{"code": "A", "where": "X"}]}
    b = {"schedules": [{"where": "X", "code": "A"}]}
    assert _cd_fingerprint(a) == _cd_fingerprint(b)
    c = {"schedules": [{"code": "B", "where": "X"}]}
    assert _cd_fingerprint(a) != _cd_fingerprint(c)
    return True


def cupdien_selftest_health():
    health = {
        "version": CUPDIEN_HARDENED_VERSION,
        "running": False,
        "instance": CUPDIEN_OWNER,
        "timezone": CUPDIEN_TZ_NAME,
        "now": power_iso(),
        "subscriptions": 0,
        "errors_24h": 0,
        "last_tick": "",
        "last_error": "",
        "tick_count": 0,
    }
    assert health["version"] == CUPDIEN_HARDENED_VERSION
    assert health["timezone"]
    return True


def cupdien_selftest():
    tests = [
        cupdien_selftest_storage,
        cupdien_selftest_due_logic,
        cupdien_selftest_rollover,
        cupdien_selftest_fingerprint,
        cupdien_selftest_health,
    ]
    passed = 0
    for test in tests:
        test()
        passed += 1
    return passed, len(tests)


# ============================================================
# END OF HARDENED CORE
# The regression corpus below is intentionally large. Each row is a
# deterministic scheduler scenario used by offline validation tooling.
# It does not create background tasks and has no network side effects.
# ============================================================
CUPDIEN_SCHEDULER_REGRESSION_MATRIX = [
    (1, "00:00", True, 0, "DONE_TODAY"),
    (2, "13:01", False, 17, "FUTURE"),
    (3, "02:02", False, 34, "FUTURE"),
    (4, "15:03", True, 51, "DONE_TODAY"),
    (5, "04:04", False, 68, "FUTURE"),
    (6, "17:05", False, 85, "FUTURE"),
    (7, "06:06", True, 102, "DONE_TODAY"),
    (8, "19:07", False, 119, "FUTURE"),
    (9, "08:08", False, 136, "FUTURE"),
    (10, "21:09", True, 153, "DONE_TODAY"),
    (11, "10:10", False, 170, "FUTURE"),
    (12, "23:11", False, 187, "FUTURE"),
    (13, "12:12", True, 204, "DONE_TODAY"),
    (14, "01:13", False, 221, "DUE"),
    (15, "14:14", False, 238, "FUTURE"),
    (16, "03:15", True, 255, "DONE_TODAY"),
    (17, "16:16", False, 272, "FUTURE"),
    (18, "05:17", False, 289, "FUTURE"),
    (19, "18:18", True, 306, "DONE_TODAY"),
    (20, "07:19", False, 323, "FUTURE"),
    (21, "20:20", False, 340, "FUTURE"),
    (22, "09:21", True, 357, "DONE_TODAY"),
    (23, "22:22", False, 374, "FUTURE"),
    (24, "11:23", False, 391, "FUTURE"),
    (25, "00:24", True, 408, "DONE_TODAY"),
    (26, "13:25", False, 425, "FUTURE"),
    (27, "02:26", False, 442, "DUE"),
    (28, "15:27", True, 459, "DONE_TODAY"),
    (29, "04:28", False, 476, "DUE"),
    (30, "17:29", False, 493, "FUTURE"),
    (31, "06:30", True, 510, "DONE_TODAY"),
    (32, "19:31", False, 527, "FUTURE"),
    (33, "08:32", False, 544, "DUE"),
    (34, "21:33", True, 561, "DONE_TODAY"),
    (35, "10:34", False, 578, "FUTURE"),
    (36, "23:35", False, 595, "FUTURE"),
    (37, "12:36", True, 612, "DONE_TODAY"),
    (38, "01:37", False, 629, "DUE"),
    (39, "14:38", False, 646, "FUTURE"),
    (40, "03:39", True, 663, "DONE_TODAY"),
    (41, "16:40", False, 680, "FUTURE"),
    (42, "05:41", False, 697, "DUE"),
    (43, "18:42", True, 714, "DONE_TODAY"),
    (44, "07:43", False, 731, "DUE"),
    (45, "20:44", False, 748, "FUTURE"),
    (46, "09:45", True, 765, "DONE_TODAY"),
    (47, "22:46", False, 782, "FUTURE"),
    (48, "11:47", False, 799, "DUE"),
    (49, "00:48", True, 816, "DONE_TODAY"),
    (50, "13:49", False, 833, "DUE"),
    (51, "02:50", False, 850, "DUE"),
    (52, "15:51", True, 867, "DONE_TODAY"),
    (53, "04:52", False, 884, "DUE"),
    (54, "17:53", False, 901, "FUTURE"),
    (55, "06:54", True, 918, "DONE_TODAY"),
    (56, "19:55", False, 935, "FUTURE"),
    (57, "08:56", False, 952, "DUE"),
    (58, "21:57", True, 969, "DONE_TODAY"),
    (59, "10:58", False, 986, "DUE"),
    (60, "23:59", False, 1003, "FUTURE"),
    (61, "12:00", True, 1020, "DONE_TODAY"),
    (62, "01:01", False, 1037, "DUE"),
    (63, "14:02", False, 1054, "DUE"),
    (64, "03:03", True, 1071, "DONE_TODAY"),
    (65, "16:04", False, 1088, "DUE"),
    (66, "05:05", False, 1105, "DUE"),
    (67, "18:06", True, 1122, "DONE_TODAY"),
    (68, "07:07", False, 1139, "DUE"),
    (69, "20:08", False, 1156, "FUTURE"),
    (70, "09:09", True, 1173, "DONE_TODAY"),
    (71, "22:10", False, 1190, "FUTURE"),
    (72, "11:11", False, 1207, "DUE"),
    (73, "00:12", True, 1224, "DONE_TODAY"),
    (74, "13:13", False, 1241, "DUE"),
    (75, "02:14", False, 1258, "DUE"),
    (76, "15:15", True, 1275, "DONE_TODAY"),
    (77, "04:16", False, 1292, "DUE"),
    (78, "17:17", False, 1309, "DUE"),
    (79, "06:18", True, 1326, "DONE_TODAY"),
    (80, "19:19", False, 1343, "DUE"),
    (81, "08:20", False, 1360, "DUE"),
    (82, "21:21", True, 1377, "DONE_TODAY"),
    (83, "10:22", False, 1394, "DUE"),
    (84, "23:23", False, 1411, "DUE"),
    (85, "12:24", True, 1428, "DONE_TODAY"),
    (86, "01:25", False, 5, "FUTURE"),
    (87, "14:26", False, 22, "FUTURE"),
    (88, "03:27", True, 39, "DONE_TODAY"),
    (89, "16:28", False, 56, "FUTURE"),
    (90, "05:29", False, 73, "FUTURE"),
    (91, "18:30", True, 90, "DONE_TODAY"),
    (92, "07:31", False, 107, "FUTURE"),
    (93, "20:32", False, 124, "FUTURE"),
    (94, "09:33", True, 141, "DONE_TODAY"),
    (95, "22:34", False, 158, "FUTURE"),
    (96, "11:35", False, 175, "FUTURE"),
    (97, "00:36", True, 192, "DONE_TODAY"),
    (98, "13:37", False, 209, "FUTURE"),
    (99, "02:38", False, 226, "DUE"),
    (100, "15:39", True, 243, "DONE_TODAY"),
    (101, "04:40", False, 260, "FUTURE"),
    (102, "17:41", False, 277, "FUTURE"),
    (103, "06:42", True, 294, "DONE_TODAY"),
    (104, "19:43", False, 311, "FUTURE"),
    (105, "08:44", False, 328, "FUTURE"),
    (106, "21:45", True, 345, "DONE_TODAY"),
    (107, "10:46", False, 362, "FUTURE"),
    (108, "23:47", False, 379, "FUTURE"),
    (109, "12:48", True, 396, "DONE_TODAY"),
    (110, "01:49", False, 413, "DUE"),
    (111, "14:50", False, 430, "FUTURE"),
    (112, "03:51", True, 447, "DONE_TODAY"),
    (113, "16:52", False, 464, "FUTURE"),
    (114, "05:53", False, 481, "DUE"),
    (115, "18:54", True, 498, "DONE_TODAY"),
    (116, "07:55", False, 515, "DUE"),
    (117, "20:56", False, 532, "FUTURE"),
    (118, "09:57", True, 549, "DONE_TODAY"),
    (119, "22:58", False, 566, "FUTURE"),
    (120, "11:59", False, 583, "FUTURE"),
    (121, "00:00", True, 600, "DONE_TODAY"),
    (122, "13:01", False, 617, "FUTURE"),
    (123, "02:02", False, 634, "DUE"),
    (124, "15:03", True, 651, "DONE_TODAY"),
    (125, "04:04", False, 668, "DUE"),
    (126, "17:05", False, 685, "FUTURE"),
    (127, "06:06", True, 702, "DONE_TODAY"),
    (128, "19:07", False, 719, "FUTURE"),
    (129, "08:08", False, 736, "DUE"),
    (130, "21:09", True, 753, "DONE_TODAY"),
    (131, "10:10", False, 770, "DUE"),
    (132, "23:11", False, 787, "FUTURE"),
    (133, "12:12", True, 804, "DONE_TODAY"),
    (134, "01:13", False, 821, "DUE"),
    (135, "14:14", False, 838, "FUTURE"),
    (136, "03:15", True, 855, "DONE_TODAY"),
    (137, "16:16", False, 872, "FUTURE"),
    (138, "05:17", False, 889, "DUE"),
    (139, "18:18", True, 906, "DONE_TODAY"),
    (140, "07:19", False, 923, "DUE"),
    (141, "20:20", False, 940, "FUTURE"),
    (142, "09:21", True, 957, "DONE_TODAY"),
    (143, "22:22", False, 974, "FUTURE"),
    (144, "11:23", False, 991, "DUE"),
    (145, "00:24", True, 1008, "DONE_TODAY"),
    (146, "13:25", False, 1025, "DUE"),
    (147, "02:26", False, 1042, "DUE"),
    (148, "15:27", True, 1059, "DONE_TODAY"),
    (149, "04:28", False, 1076, "DUE"),
    (150, "17:29", False, 1093, "DUE"),
    (151, "06:30", True, 1110, "DONE_TODAY"),
    (152, "19:31", False, 1127, "FUTURE"),
    (153, "08:32", False, 1144, "DUE"),
    (154, "21:33", True, 1161, "DONE_TODAY"),
    (155, "10:34", False, 1178, "DUE"),
    (156, "23:35", False, 1195, "FUTURE"),
    (157, "12:36", True, 1212, "DONE_TODAY"),
    (158, "01:37", False, 1229, "DUE"),
    (159, "14:38", False, 1246, "DUE"),
    (160, "03:39", True, 1263, "DONE_TODAY"),
    (161, "16:40", False, 1280, "DUE"),
    (162, "05:41", False, 1297, "DUE"),
    (163, "18:42", True, 1314, "DONE_TODAY"),
    (164, "07:43", False, 1331, "DUE"),
    (165, "20:44", False, 1348, "DUE"),
    (166, "09:45", True, 1365, "DONE_TODAY"),
    (167, "22:46", False, 1382, "DUE"),
    (168, "11:47", False, 1399, "DUE"),
    (169, "00:48", True, 1416, "DONE_TODAY"),
    (170, "13:49", False, 1433, "DUE"),
    (171, "02:50", False, 10, "FUTURE"),
    (172, "15:51", True, 27, "DONE_TODAY"),
    (173, "04:52", False, 44, "FUTURE"),
    (174, "17:53", False, 61, "FUTURE"),
    (175, "06:54", True, 78, "DONE_TODAY"),
    (176, "19:55", False, 95, "FUTURE"),
    (177, "08:56", False, 112, "FUTURE"),
    (178, "21:57", True, 129, "DONE_TODAY"),
    (179, "10:58", False, 146, "FUTURE"),
    (180, "23:59", False, 163, "FUTURE"),
    (181, "12:00", True, 180, "DONE_TODAY"),
    (182, "01:01", False, 197, "DUE"),
    (183, "14:02", False, 214, "FUTURE"),
    (184, "03:03", True, 231, "DONE_TODAY"),
    (185, "16:04", False, 248, "FUTURE"),
    (186, "05:05", False, 265, "FUTURE"),
    (187, "18:06", True, 282, "DONE_TODAY"),
    (188, "07:07", False, 299, "FUTURE"),
    (189, "20:08", False, 316, "FUTURE"),
    (190, "09:09", True, 333, "DONE_TODAY"),
    (191, "22:10", False, 350, "FUTURE"),
    (192, "11:11", False, 367, "FUTURE"),
    (193, "00:12", True, 384, "DONE_TODAY"),
    (194, "13:13", False, 401, "FUTURE"),
    (195, "02:14", False, 418, "DUE"),
    (196, "15:15", True, 435, "DONE_TODAY"),
    (197, "04:16", False, 452, "DUE"),
    (198, "17:17", False, 469, "FUTURE"),
    (199, "06:18", True, 486, "DONE_TODAY"),
    (200, "19:19", False, 503, "FUTURE"),
    (201, "08:20", False, 520, "DUE"),
    (202, "21:21", True, 537, "DONE_TODAY"),
    (203, "10:22", False, 554, "FUTURE"),
    (204, "23:23", False, 571, "FUTURE"),
    (205, "12:24", True, 588, "DONE_TODAY"),
    (206, "01:25", False, 605, "DUE"),
    (207, "14:26", False, 622, "FUTURE"),
    (208, "03:27", True, 639, "DONE_TODAY"),
    (209, "16:28", False, 656, "FUTURE"),
    (210, "05:29", False, 673, "DUE"),
    (211, "18:30", True, 690, "DONE_TODAY"),
    (212, "07:31", False, 707, "DUE"),
    (213, "20:32", False, 724, "FUTURE"),
    (214, "09:33", True, 741, "DONE_TODAY"),
    (215, "22:34", False, 758, "FUTURE"),
    (216, "11:35", False, 775, "DUE"),
    (217, "00:36", True, 792, "DONE_TODAY"),
    (218, "13:37", False, 809, "FUTURE"),
    (219, "02:38", False, 826, "DUE"),
    (220, "15:39", True, 843, "DONE_TODAY"),
    (221, "04:40", False, 860, "DUE"),
    (222, "17:41", False, 877, "FUTURE"),
    (223, "06:42", True, 894, "DONE_TODAY"),
    (224, "19:43", False, 911, "FUTURE"),
    (225, "08:44", False, 928, "DUE"),
    (226, "21:45", True, 945, "DONE_TODAY"),
    (227, "10:46", False, 962, "DUE"),
    (228, "23:47", False, 979, "FUTURE"),
    (229, "12:48", True, 996, "DONE_TODAY"),
    (230, "01:49", False, 1013, "DUE"),
    (231, "14:50", False, 1030, "DUE"),
    (232, "03:51", True, 1047, "DONE_TODAY"),
    (233, "16:52", False, 1064, "DUE"),
    (234, "05:53", False, 1081, "DUE"),
    (235, "18:54", True, 1098, "DONE_TODAY"),
    (236, "07:55", False, 1115, "DUE"),
    (237, "20:56", False, 1132, "FUTURE"),
    (238, "09:57", True, 1149, "DONE_TODAY"),
    (239, "22:58", False, 1166, "FUTURE"),
    (240, "11:59", False, 1183, "DUE"),
    (241, "00:00", True, 1200, "DONE_TODAY"),
    (242, "13:01", False, 1217, "DUE"),
    (243, "02:02", False, 1234, "DUE"),
    (244, "15:03", True, 1251, "DONE_TODAY"),
    (245, "04:04", False, 1268, "DUE"),
    (246, "17:05", False, 1285, "DUE"),
    (247, "06:06", True, 1302, "DONE_TODAY"),
    (248, "19:07", False, 1319, "DUE"),
    (249, "08:08", False, 1336, "DUE"),
    (250, "21:09", True, 1353, "DONE_TODAY"),
    (251, "10:10", False, 1370, "DUE"),
    (252, "23:11", False, 1387, "FUTURE"),
    (253, "12:12", True, 1404, "DONE_TODAY"),
    (254, "01:13", False, 1421, "DUE"),
    (255, "14:14", False, 1438, "DUE"),
    (256, "03:15", True, 15, "DONE_TODAY"),
    (257, "16:16", False, 32, "FUTURE"),
    (258, "05:17", False, 49, "FUTURE"),
    (259, "18:18", True, 66, "DONE_TODAY"),
    (260, "07:19", False, 83, "FUTURE"),
    (261, "20:20", False, 100, "FUTURE"),
    (262, "09:21", True, 117, "DONE_TODAY"),
    (263, "22:22", False, 134, "FUTURE"),
    (264, "11:23", False, 151, "FUTURE"),
    (265, "00:24", True, 168, "DONE_TODAY"),
    (266, "13:25", False, 185, "FUTURE"),
    (267, "02:26", False, 202, "DUE"),
    (268, "15:27", True, 219, "DONE_TODAY"),
    (269, "04:28", False, 236, "FUTURE"),
    (270, "17:29", False, 253, "FUTURE"),
    (271, "06:30", True, 270, "DONE_TODAY"),
    (272, "19:31", False, 287, "FUTURE"),
    (273, "08:32", False, 304, "FUTURE"),
    (274, "21:33", True, 321, "DONE_TODAY"),
    (275, "10:34", False, 338, "FUTURE"),
    (276, "23:35", False, 355, "FUTURE"),
    (277, "12:36", True, 372, "DONE_TODAY"),
    (278, "01:37", False, 389, "DUE"),
    (279, "14:38", False, 406, "FUTURE"),
    (280, "03:39", True, 423, "DONE_TODAY"),
    (281, "16:40", False, 440, "FUTURE"),
    (282, "05:41", False, 457, "DUE"),
    (283, "18:42", True, 474, "DONE_TODAY"),
    (284, "07:43", False, 491, "DUE"),
    (285, "20:44", False, 508, "FUTURE"),
    (286, "09:45", True, 525, "DONE_TODAY"),
    (287, "22:46", False, 542, "FUTURE"),
    (288, "11:47", False, 559, "FUTURE"),
    (289, "00:48", True, 576, "DONE_TODAY"),
    (290, "13:49", False, 593, "FUTURE"),
    (291, "02:50", False, 610, "DUE"),
    (292, "15:51", True, 627, "DONE_TODAY"),
    (293, "04:52", False, 644, "DUE"),
    (294, "17:53", False, 661, "FUTURE"),
    (295, "06:54", True, 678, "DONE_TODAY"),
    (296, "19:55", False, 695, "FUTURE"),
    (297, "08:56", False, 712, "DUE"),
    (298, "21:57", True, 729, "DONE_TODAY"),
    (299, "10:58", False, 746, "DUE"),
    (300, "23:59", False, 763, "FUTURE"),
    (301, "12:00", True, 780, "DONE_TODAY"),
    (302, "01:01", False, 797, "DUE"),
    (303, "14:02", False, 814, "FUTURE"),
    (304, "03:03", True, 831, "DONE_TODAY"),
    (305, "16:04", False, 848, "FUTURE"),
    (306, "05:05", False, 865, "DUE"),
    (307, "18:06", True, 882, "DONE_TODAY"),
    (308, "07:07", False, 899, "DUE"),
    (309, "20:08", False, 916, "FUTURE"),
    (310, "09:09", True, 933, "DONE_TODAY"),
    (311, "22:10", False, 950, "FUTURE"),
    (312, "11:11", False, 967, "DUE"),
    (313, "00:12", True, 984, "DONE_TODAY"),
    (314, "13:13", False, 1001, "DUE"),
    (315, "02:14", False, 1018, "DUE"),
    (316, "15:15", True, 1035, "DONE_TODAY"),
    (317, "04:16", False, 1052, "DUE"),
    (318, "17:17", False, 1069, "DUE"),
    (319, "06:18", True, 1086, "DONE_TODAY"),
    (320, "19:19", False, 1103, "FUTURE"),
    (321, "08:20", False, 1120, "DUE"),
    (322, "21:21", True, 1137, "DONE_TODAY"),
    (323, "10:22", False, 1154, "DUE"),
    (324, "23:23", False, 1171, "FUTURE"),
    (325, "12:24", True, 1188, "DONE_TODAY"),
    (326, "01:25", False, 1205, "DUE"),
    (327, "14:26", False, 1222, "DUE"),
    (328, "03:27", True, 1239, "DONE_TODAY"),
    (329, "16:28", False, 1256, "DUE"),
    (330, "05:29", False, 1273, "DUE"),
    (331, "18:30", True, 1290, "DONE_TODAY"),
    (332, "07:31", False, 1307, "DUE"),
    (333, "20:32", False, 1324, "DUE"),
    (334, "09:33", True, 1341, "DONE_TODAY"),
    (335, "22:34", False, 1358, "DUE"),
    (336, "11:35", False, 1375, "DUE"),
    (337, "00:36", True, 1392, "DONE_TODAY"),
    (338, "13:37", False, 1409, "DUE"),
    (339, "02:38", False, 1426, "DUE"),
    (340, "15:39", True, 3, "DONE_TODAY"),
    (341, "04:40", False, 20, "FUTURE"),
    (342, "17:41", False, 37, "FUTURE"),
    (343, "06:42", True, 54, "DONE_TODAY"),
    (344, "19:43", False, 71, "FUTURE"),
    (345, "08:44", False, 88, "FUTURE"),
    (346, "21:45", True, 105, "DONE_TODAY"),
    (347, "10:46", False, 122, "FUTURE"),
    (348, "23:47", False, 139, "FUTURE"),
    (349, "12:48", True, 156, "DONE_TODAY"),
    (350, "01:49", False, 173, "DUE"),
    (351, "14:50", False, 190, "FUTURE"),
    (352, "03:51", True, 207, "DONE_TODAY"),
    (353, "16:52", False, 224, "FUTURE"),
    (354, "05:53", False, 241, "FUTURE"),
    (355, "18:54", True, 258, "DONE_TODAY"),
    (356, "07:55", False, 275, "FUTURE"),
    (357, "20:56", False, 292, "FUTURE"),
    (358, "09:57", True, 309, "DONE_TODAY"),
    (359, "22:58", False, 326, "FUTURE"),
    (360, "11:59", False, 343, "FUTURE"),
    (361, "00:00", True, 360, "DONE_TODAY"),
    (362, "13:01", False, 377, "FUTURE"),
    (363, "02:02", False, 394, "DUE"),
    (364, "15:03", True, 411, "DONE_TODAY"),
    (365, "04:04", False, 428, "DUE"),
    (366, "17:05", False, 445, "FUTURE"),
    (367, "06:06", True, 462, "DONE_TODAY"),
    (368, "19:07", False, 479, "FUTURE"),
    (369, "08:08", False, 496, "DUE"),
    (370, "21:09", True, 513, "DONE_TODAY"),
    (371, "10:10", False, 530, "FUTURE"),
    (372, "23:11", False, 547, "FUTURE"),
    (373, "12:12", True, 564, "DONE_TODAY"),
    (374, "01:13", False, 581, "DUE"),
    (375, "14:14", False, 598, "FUTURE"),
    (376, "03:15", True, 615, "DONE_TODAY"),
    (377, "16:16", False, 632, "FUTURE"),
    (378, "05:17", False, 649, "DUE"),
    (379, "18:18", True, 666, "DONE_TODAY"),
    (380, "07:19", False, 683, "DUE"),
    (381, "20:20", False, 700, "FUTURE"),
    (382, "09:21", True, 717, "DONE_TODAY"),
    (383, "22:22", False, 734, "FUTURE"),
    (384, "11:23", False, 751, "DUE"),
    (385, "00:24", True, 768, "DONE_TODAY"),
    (386, "13:25", False, 785, "FUTURE"),
    (387, "02:26", False, 802, "DUE"),
    (388, "15:27", True, 819, "DONE_TODAY"),
    (389, "04:28", False, 836, "DUE"),
    (390, "17:29", False, 853, "FUTURE"),
    (391, "06:30", True, 870, "DONE_TODAY"),
    (392, "19:31", False, 887, "FUTURE"),
    (393, "08:32", False, 904, "DUE"),
    (394, "21:33", True, 921, "DONE_TODAY"),
    (395, "10:34", False, 938, "DUE"),
    (396, "23:35", False, 955, "FUTURE"),
    (397, "12:36", True, 972, "DONE_TODAY"),
    (398, "01:37", False, 989, "DUE"),
    (399, "14:38", False, 1006, "DUE"),
    (400, "03:39", True, 1023, "DONE_TODAY"),
    (401, "16:40", False, 1040, "DUE"),
    (402, "05:41", False, 1057, "DUE"),
    (403, "18:42", True, 1074, "DONE_TODAY"),
    (404, "07:43", False, 1091, "DUE"),
    (405, "20:44", False, 1108, "FUTURE"),
    (406, "09:45", True, 1125, "DONE_TODAY"),
    (407, "22:46", False, 1142, "FUTURE"),
    (408, "11:47", False, 1159, "DUE"),
    (409, "00:48", True, 1176, "DONE_TODAY"),
    (410, "13:49", False, 1193, "DUE"),
    (411, "02:50", False, 1210, "DUE"),
    (412, "15:51", True, 1227, "DONE_TODAY"),
    (413, "04:52", False, 1244, "DUE"),
    (414, "17:53", False, 1261, "DUE"),
    (415, "06:54", True, 1278, "DONE_TODAY"),
    (416, "19:55", False, 1295, "DUE"),
    (417, "08:56", False, 1312, "DUE"),
    (418, "21:57", True, 1329, "DONE_TODAY"),
    (419, "10:58", False, 1346, "DUE"),
    (420, "23:59", False, 1363, "FUTURE"),
    (421, "12:00", True, 1380, "DONE_TODAY"),
    (422, "01:01", False, 1397, "DUE"),
    (423, "14:02", False, 1414, "DUE"),
    (424, "03:03", True, 1431, "DONE_TODAY"),
    (425, "16:04", False, 8, "FUTURE"),
    (426, "05:05", False, 25, "FUTURE"),
    (427, "18:06", True, 42, "DONE_TODAY"),
    (428, "07:07", False, 59, "FUTURE"),
    (429, "20:08", False, 76, "FUTURE"),
    (430, "09:09", True, 93, "DONE_TODAY"),
    (431, "22:10", False, 110, "FUTURE"),
    (432, "11:11", False, 127, "FUTURE"),
    (433, "00:12", True, 144, "DONE_TODAY"),
    (434, "13:13", False, 161, "FUTURE"),
    (435, "02:14", False, 178, "DUE"),
    (436, "15:15", True, 195, "DONE_TODAY"),
    (437, "04:16", False, 212, "FUTURE"),
    (438, "17:17", False, 229, "FUTURE"),
    (439, "06:18", True, 246, "DONE_TODAY"),
    (440, "19:19", False, 263, "FUTURE"),
    (441, "08:20", False, 280, "FUTURE"),
    (442, "21:21", True, 297, "DONE_TODAY"),
    (443, "10:22", False, 314, "FUTURE"),
    (444, "23:23", False, 331, "FUTURE"),
    (445, "12:24", True, 348, "DONE_TODAY"),
    (446, "01:25", False, 365, "DUE"),
    (447, "14:26", False, 382, "FUTURE"),
    (448, "03:27", True, 399, "DONE_TODAY"),
    (449, "16:28", False, 416, "FUTURE"),
    (450, "05:29", False, 433, "DUE"),
    (451, "18:30", True, 450, "DONE_TODAY"),
    (452, "07:31", False, 467, "DUE"),
    (453, "20:32", False, 484, "FUTURE"),
    (454, "09:33", True, 501, "DONE_TODAY"),
    (455, "22:34", False, 518, "FUTURE"),
    (456, "11:35", False, 535, "FUTURE"),
    (457, "00:36", True, 552, "DONE_TODAY"),
    (458, "13:37", False, 569, "FUTURE"),
    (459, "02:38", False, 586, "DUE"),
    (460, "15:39", True, 603, "DONE_TODAY"),
    (461, "04:40", False, 620, "DUE"),
    (462, "17:41", False, 637, "FUTURE"),
    (463, "06:42", True, 654, "DONE_TODAY"),
    (464, "19:43", False, 671, "FUTURE"),
    (465, "08:44", False, 688, "DUE"),
    (466, "21:45", True, 705, "DONE_TODAY"),
    (467, "10:46", False, 722, "DUE"),
    (468, "23:47", False, 739, "FUTURE"),
    (469, "12:48", True, 756, "DONE_TODAY"),
    (470, "01:49", False, 773, "DUE"),
    (471, "14:50", False, 790, "FUTURE"),
    (472, "03:51", True, 807, "DONE_TODAY"),
    (473, "16:52", False, 824, "FUTURE"),
    (474, "05:53", False, 841, "DUE"),
    (475, "18:54", True, 858, "DONE_TODAY"),
    (476, "07:55", False, 875, "DUE"),
    (477, "20:56", False, 892, "FUTURE"),
    (478, "09:57", True, 909, "DONE_TODAY"),
    (479, "22:58", False, 926, "FUTURE"),
    (480, "11:59", False, 943, "DUE"),
    (481, "00:00", True, 960, "DONE_TODAY"),
    (482, "13:01", False, 977, "DUE"),
    (483, "02:02", False, 994, "DUE"),
    (484, "15:03", True, 1011, "DONE_TODAY"),
    (485, "04:04", False, 1028, "DUE"),
    (486, "17:05", False, 1045, "DUE"),
    (487, "06:06", True, 1062, "DONE_TODAY"),
    (488, "19:07", False, 1079, "FUTURE"),
    (489, "08:08", False, 1096, "DUE"),
    (490, "21:09", True, 1113, "DONE_TODAY"),
    (491, "10:10", False, 1130, "DUE"),
    (492, "23:11", False, 1147, "FUTURE"),
    (493, "12:12", True, 1164, "DONE_TODAY"),
    (494, "01:13", False, 1181, "DUE"),
    (495, "14:14", False, 1198, "DUE"),
    (496, "03:15", True, 1215, "DONE_TODAY"),
    (497, "16:16", False, 1232, "DUE"),
    (498, "05:17", False, 1249, "DUE"),
    (499, "18:18", True, 1266, "DONE_TODAY"),
    (500, "07:19", False, 1283, "DUE"),
    (501, "20:20", False, 1300, "DUE"),
    (502, "09:21", True, 1317, "DONE_TODAY"),
    (503, "22:22", False, 1334, "FUTURE"),
    (504, "11:23", False, 1351, "DUE"),
    (505, "00:24", True, 1368, "DONE_TODAY"),
    (506, "13:25", False, 1385, "DUE"),
    (507, "02:26", False, 1402, "DUE"),
    (508, "15:27", True, 1419, "DONE_TODAY"),
    (509, "04:28", False, 1436, "DUE"),
    (510, "17:29", False, 13, "FUTURE"),
    (511, "06:30", True, 30, "DONE_TODAY"),
    (512, "19:31", False, 47, "FUTURE"),
    (513, "08:32", False, 64, "FUTURE"),
    (514, "21:33", True, 81, "DONE_TODAY"),
    (515, "10:34", False, 98, "FUTURE"),
    (516, "23:35", False, 115, "FUTURE"),
    (517, "12:36", True, 132, "DONE_TODAY"),
    (518, "01:37", False, 149, "DUE"),
    (519, "14:38", False, 166, "FUTURE"),
    (520, "03:39", True, 183, "DONE_TODAY"),
    (521, "16:40", False, 200, "FUTURE"),
    (522, "05:41", False, 217, "FUTURE"),
    (523, "18:42", True, 234, "DONE_TODAY"),
    (524, "07:43", False, 251, "FUTURE"),
    (525, "20:44", False, 268, "FUTURE"),
    (526, "09:45", True, 285, "DONE_TODAY"),
    (527, "22:46", False, 302, "FUTURE"),
    (528, "11:47", False, 319, "FUTURE"),
    (529, "00:48", True, 336, "DONE_TODAY"),
    (530, "13:49", False, 353, "FUTURE"),
    (531, "02:50", False, 370, "DUE"),
    (532, "15:51", True, 387, "DONE_TODAY"),
    (533, "04:52", False, 404, "DUE"),
    (534, "17:53", False, 421, "FUTURE"),
    (535, "06:54", True, 438, "DONE_TODAY"),
    (536, "19:55", False, 455, "FUTURE"),
    (537, "08:56", False, 472, "FUTURE"),
    (538, "21:57", True, 489, "DONE_TODAY"),
    (539, "10:58", False, 506, "FUTURE"),
    (540, "23:59", False, 523, "FUTURE"),
    (541, "12:00", True, 540, "DONE_TODAY"),
    (542, "01:01", False, 557, "DUE"),
    (543, "14:02", False, 574, "FUTURE"),
    (544, "03:03", True, 591, "DONE_TODAY"),
    (545, "16:04", False, 608, "FUTURE"),
    (546, "05:05", False, 625, "DUE"),
    (547, "18:06", True, 642, "DONE_TODAY"),
    (548, "07:07", False, 659, "DUE"),
    (549, "20:08", False, 676, "FUTURE"),
    (550, "09:09", True, 693, "DONE_TODAY"),
    (551, "22:10", False, 710, "FUTURE"),
    (552, "11:11", False, 727, "DUE"),
    (553, "00:12", True, 744, "DONE_TODAY"),
    (554, "13:13", False, 761, "FUTURE"),
    (555, "02:14", False, 778, "DUE"),
    (556, "15:15", True, 795, "DONE_TODAY"),
    (557, "04:16", False, 812, "DUE"),
    (558, "17:17", False, 829, "FUTURE"),
    (559, "06:18", True, 846, "DONE_TODAY"),
    (560, "19:19", False, 863, "FUTURE"),
    (561, "08:20", False, 880, "DUE"),
    (562, "21:21", True, 897, "DONE_TODAY"),
    (563, "10:22", False, 914, "DUE"),
    (564, "23:23", False, 931, "FUTURE"),
    (565, "12:24", True, 948, "DONE_TODAY"),
    (566, "01:25", False, 965, "DUE"),
    (567, "14:26", False, 982, "DUE"),
    (568, "03:27", True, 999, "DONE_TODAY"),
    (569, "16:28", False, 1016, "DUE"),
    (570, "05:29", False, 1033, "DUE"),
    (571, "18:30", True, 1050, "DONE_TODAY"),
    (572, "07:31", False, 1067, "DUE"),
    (573, "20:32", False, 1084, "FUTURE"),
    (574, "09:33", True, 1101, "DONE_TODAY"),
    (575, "22:34", False, 1118, "FUTURE"),
    (576, "11:35", False, 1135, "DUE"),
    (577, "00:36", True, 1152, "DONE_TODAY"),
    (578, "13:37", False, 1169, "DUE"),
    (579, "02:38", False, 1186, "DUE"),
    (580, "15:39", True, 1203, "DONE_TODAY"),
    (581, "04:40", False, 1220, "DUE"),
    (582, "17:41", False, 1237, "DUE"),
    (583, "06:42", True, 1254, "DONE_TODAY"),
    (584, "19:43", False, 1271, "DUE"),
    (585, "08:44", False, 1288, "DUE"),
    (586, "21:45", True, 1305, "DONE_TODAY"),
    (587, "10:46", False, 1322, "DUE"),
    (588, "23:47", False, 1339, "FUTURE"),
    (589, "12:48", True, 1356, "DONE_TODAY"),
    (590, "01:49", False, 1373, "DUE"),
    (591, "14:50", False, 1390, "DUE"),
    (592, "03:51", True, 1407, "DONE_TODAY"),
    (593, "16:52", False, 1424, "DUE"),
    (594, "05:53", False, 1, "FUTURE"),
    (595, "18:54", True, 18, "DONE_TODAY"),
    (596, "07:55", False, 35, "FUTURE"),
    (597, "20:56", False, 52, "FUTURE"),
    (598, "09:57", True, 69, "DONE_TODAY"),
    (599, "22:58", False, 86, "FUTURE"),
    (600, "11:59", False, 103, "FUTURE"),
    (601, "00:00", True, 120, "DONE_TODAY"),
    (602, "13:01", False, 137, "FUTURE"),
    (603, "02:02", False, 154, "DUE"),
    (604, "15:03", True, 171, "DONE_TODAY"),
    (605, "04:04", False, 188, "FUTURE"),
    (606, "17:05", False, 205, "FUTURE"),
    (607, "06:06", True, 222, "DONE_TODAY"),
    (608, "19:07", False, 239, "FUTURE"),
    (609, "08:08", False, 256, "FUTURE"),
    (610, "21:09", True, 273, "DONE_TODAY"),
    (611, "10:10", False, 290, "FUTURE"),
    (612, "23:11", False, 307, "FUTURE"),
    (613, "12:12", True, 324, "DONE_TODAY"),
    (614, "01:13", False, 341, "DUE"),
    (615, "14:14", False, 358, "FUTURE"),
    (616, "03:15", True, 375, "DONE_TODAY"),
    (617, "16:16", False, 392, "FUTURE"),
    (618, "05:17", False, 409, "DUE"),
    (619, "18:18", True, 426, "DONE_TODAY"),
    (620, "07:19", False, 443, "DUE"),
    (621, "20:20", False, 460, "FUTURE"),
    (622, "09:21", True, 477, "DONE_TODAY"),
    (623, "22:22", False, 494, "FUTURE"),
    (624, "11:23", False, 511, "FUTURE"),
    (625, "00:24", True, 528, "DONE_TODAY"),
    (626, "13:25", False, 545, "FUTURE"),
    (627, "02:26", False, 562, "DUE"),
    (628, "15:27", True, 579, "DONE_TODAY"),
    (629, "04:28", False, 596, "DUE"),
    (630, "17:29", False, 613, "FUTURE"),
    (631, "06:30", True, 630, "DONE_TODAY"),
    (632, "19:31", False, 647, "FUTURE"),
    (633, "08:32", False, 664, "DUE"),
    (634, "21:33", True, 681, "DONE_TODAY"),
    (635, "10:34", False, 698, "DUE"),
    (636, "23:35", False, 715, "FUTURE"),
    (637, "12:36", True, 732, "DONE_TODAY"),
    (638, "01:37", False, 749, "DUE"),
    (639, "14:38", False, 766, "FUTURE"),
    (640, "03:39", True, 783, "DONE_TODAY"),
    (641, "16:40", False, 800, "FUTURE"),
    (642, "05:41", False, 817, "DUE"),
    (643, "18:42", True, 834, "DONE_TODAY"),
    (644, "07:43", False, 851, "DUE"),
    (645, "20:44", False, 868, "FUTURE"),
    (646, "09:45", True, 885, "DONE_TODAY"),
    (647, "22:46", False, 902, "FUTURE"),
    (648, "11:47", False, 919, "DUE"),
    (649, "00:48", True, 936, "DONE_TODAY"),
    (650, "13:49", False, 953, "DUE"),
    (651, "02:50", False, 970, "DUE"),
    (652, "15:51", True, 987, "DONE_TODAY"),
    (653, "04:52", False, 1004, "DUE"),
    (654, "17:53", False, 1021, "FUTURE"),
    (655, "06:54", True, 1038, "DONE_TODAY"),
    (656, "19:55", False, 1055, "FUTURE"),
    (657, "08:56", False, 1072, "DUE"),
    (658, "21:57", True, 1089, "DONE_TODAY"),
    (659, "10:58", False, 1106, "DUE"),
    (660, "23:59", False, 1123, "FUTURE"),
    (661, "12:00", True, 1140, "DONE_TODAY"),
    (662, "01:01", False, 1157, "DUE"),
    (663, "14:02", False, 1174, "DUE"),
    (664, "03:03", True, 1191, "DONE_TODAY"),
    (665, "16:04", False, 1208, "DUE"),
    (666, "05:05", False, 1225, "DUE"),
    (667, "18:06", True, 1242, "DONE_TODAY"),
    (668, "07:07", False, 1259, "DUE"),
    (669, "20:08", False, 1276, "DUE"),
    (670, "09:09", True, 1293, "DONE_TODAY"),
    (671, "22:10", False, 1310, "FUTURE"),
    (672, "11:11", False, 1327, "DUE"),
    (673, "00:12", True, 1344, "DONE_TODAY"),
    (674, "13:13", False, 1361, "DUE"),
    (675, "02:14", False, 1378, "DUE"),
    (676, "15:15", True, 1395, "DONE_TODAY"),
    (677, "04:16", False, 1412, "DUE"),
    (678, "17:17", False, 1429, "DUE"),
    (679, "06:18", True, 6, "DONE_TODAY"),
    (680, "19:19", False, 23, "FUTURE"),
    (681, "08:20", False, 40, "FUTURE"),
    (682, "21:21", True, 57, "DONE_TODAY"),
    (683, "10:22", False, 74, "FUTURE"),
    (684, "23:23", False, 91, "FUTURE"),
    (685, "12:24", True, 108, "DONE_TODAY"),
    (686, "01:25", False, 125, "DUE"),
    (687, "14:26", False, 142, "FUTURE"),
    (688, "03:27", True, 159, "DONE_TODAY"),
    (689, "16:28", False, 176, "FUTURE"),
    (690, "05:29", False, 193, "FUTURE"),
    (691, "18:30", True, 210, "DONE_TODAY"),
    (692, "07:31", False, 227, "FUTURE"),
    (693, "20:32", False, 244, "FUTURE"),
    (694, "09:33", True, 261, "DONE_TODAY"),
    (695, "22:34", False, 278, "FUTURE"),
    (696, "11:35", False, 295, "FUTURE"),
    (697, "00:36", True, 312, "DONE_TODAY"),
    (698, "13:37", False, 329, "FUTURE"),
    (699, "02:38", False, 346, "DUE"),
    (700, "15:39", True, 363, "DONE_TODAY"),
    (701, "04:40", False, 380, "DUE"),
    (702, "17:41", False, 397, "FUTURE"),
    (703, "06:42", True, 414, "DONE_TODAY"),
    (704, "19:43", False, 431, "FUTURE"),
    (705, "08:44", False, 448, "FUTURE"),
    (706, "21:45", True, 465, "DONE_TODAY"),
    (707, "10:46", False, 482, "FUTURE"),
    (708, "23:47", False, 499, "FUTURE"),
    (709, "12:48", True, 516, "DONE_TODAY"),
    (710, "01:49", False, 533, "DUE"),
    (711, "14:50", False, 550, "FUTURE"),
    (712, "03:51", True, 567, "DONE_TODAY"),
    (713, "16:52", False, 584, "FUTURE"),
    (714, "05:53", False, 601, "DUE"),
    (715, "18:54", True, 618, "DONE_TODAY"),
    (716, "07:55", False, 635, "DUE"),
    (717, "20:56", False, 652, "FUTURE"),
    (718, "09:57", True, 669, "DONE_TODAY"),
    (719, "22:58", False, 686, "FUTURE"),
    (720, "11:59", False, 703, "FUTURE"),
    (721, "00:00", True, 720, "DONE_TODAY"),
    (722, "13:01", False, 737, "FUTURE"),
    (723, "02:02", False, 754, "DUE"),
    (724, "15:03", True, 771, "DONE_TODAY"),
    (725, "04:04", False, 788, "DUE"),
    (726, "17:05", False, 805, "FUTURE"),
    (727, "06:06", True, 822, "DONE_TODAY"),
    (728, "19:07", False, 839, "FUTURE"),
    (729, "08:08", False, 856, "DUE"),
    (730, "21:09", True, 873, "DONE_TODAY"),
    (731, "10:10", False, 890, "DUE"),
    (732, "23:11", False, 907, "FUTURE"),
    (733, "12:12", True, 924, "DONE_TODAY"),
    (734, "01:13", False, 941, "DUE"),
    (735, "14:14", False, 958, "DUE"),
    (736, "03:15", True, 975, "DONE_TODAY"),
    (737, "16:16", False, 992, "DUE"),
    (738, "05:17", False, 1009, "DUE"),
    (739, "18:18", True, 1026, "DONE_TODAY"),
    (740, "07:19", False, 1043, "DUE"),
    (741, "20:20", False, 1060, "FUTURE"),
    (742, "09:21", True, 1077, "DONE_TODAY"),
    (743, "22:22", False, 1094, "FUTURE"),
    (744, "11:23", False, 1111, "DUE"),
    (745, "00:24", True, 1128, "DONE_TODAY"),
    (746, "13:25", False, 1145, "DUE"),
    (747, "02:26", False, 1162, "DUE"),
    (748, "15:27", True, 1179, "DONE_TODAY"),
    (749, "04:28", False, 1196, "DUE"),
    (750, "17:29", False, 1213, "DUE"),
    (751, "06:30", True, 1230, "DONE_TODAY"),
    (752, "19:31", False, 1247, "DUE"),
    (753, "08:32", False, 1264, "DUE"),
    (754, "21:33", True, 1281, "DONE_TODAY"),
    (755, "10:34", False, 1298, "DUE"),
    (756, "23:35", False, 1315, "FUTURE"),
    (757, "12:36", True, 1332, "DONE_TODAY"),
    (758, "01:37", False, 1349, "DUE"),
    (759, "14:38", False, 1366, "DUE"),
    (760, "03:39", True, 1383, "DONE_TODAY"),
    (761, "16:40", False, 1400, "DUE"),
    (762, "05:41", False, 1417, "DUE"),
    (763, "18:42", True, 1434, "DONE_TODAY"),
    (764, "07:43", False, 11, "FUTURE"),
    (765, "20:44", False, 28, "FUTURE"),
    (766, "09:45", True, 45, "DONE_TODAY"),
    (767, "22:46", False, 62, "FUTURE"),
    (768, "11:47", False, 79, "FUTURE"),
    (769, "00:48", True, 96, "DONE_TODAY"),
    (770, "13:49", False, 113, "FUTURE"),
    (771, "02:50", False, 130, "FUTURE"),
    (772, "15:51", True, 147, "DONE_TODAY"),
    (773, "04:52", False, 164, "FUTURE"),
    (774, "17:53", False, 181, "FUTURE"),
    (775, "06:54", True, 198, "DONE_TODAY"),
    (776, "19:55", False, 215, "FUTURE"),
    (777, "08:56", False, 232, "FUTURE"),
    (778, "21:57", True, 249, "DONE_TODAY"),
    (779, "10:58", False, 266, "FUTURE"),
    (780, "23:59", False, 283, "FUTURE"),
    (781, "12:00", True, 300, "DONE_TODAY"),
    (782, "01:01", False, 317, "DUE"),
    (783, "14:02", False, 334, "FUTURE"),
    (784, "03:03", True, 351, "DONE_TODAY"),
    (785, "16:04", False, 368, "FUTURE"),
    (786, "05:05", False, 385, "DUE"),
    (787, "18:06", True, 402, "DONE_TODAY"),
    (788, "07:07", False, 419, "FUTURE"),
    (789, "20:08", False, 436, "FUTURE"),
    (790, "09:09", True, 453, "DONE_TODAY"),
    (791, "22:10", False, 470, "FUTURE"),
    (792, "11:11", False, 487, "FUTURE"),
    (793, "00:12", True, 504, "DONE_TODAY"),
    (794, "13:13", False, 521, "FUTURE"),
    (795, "02:14", False, 538, "DUE"),
    (796, "15:15", True, 555, "DONE_TODAY"),
    (797, "04:16", False, 572, "DUE"),
    (798, "17:17", False, 589, "FUTURE"),
    (799, "06:18", True, 606, "DONE_TODAY"),
    (800, "19:19", False, 623, "FUTURE"),
    (801, "08:20", False, 640, "DUE"),
    (802, "21:21", True, 657, "DONE_TODAY"),
    (803, "10:22", False, 674, "DUE"),
    (804, "23:23", False, 691, "FUTURE"),
    (805, "12:24", True, 708, "DONE_TODAY"),
    (806, "01:25", False, 725, "DUE"),
    (807, "14:26", False, 742, "FUTURE"),
    (808, "03:27", True, 759, "DONE_TODAY"),
    (809, "16:28", False, 776, "FUTURE"),
    (810, "05:29", False, 793, "DUE"),
    (811, "18:30", True, 810, "DONE_TODAY"),
    (812, "07:31", False, 827, "DUE"),
    (813, "20:32", False, 844, "FUTURE"),
    (814, "09:33", True, 861, "DONE_TODAY"),
    (815, "22:34", False, 878, "FUTURE"),
    (816, "11:35", False, 895, "DUE"),
    (817, "00:36", True, 912, "DONE_TODAY"),
    (818, "13:37", False, 929, "DUE"),
    (819, "02:38", False, 946, "DUE"),
    (820, "15:39", True, 963, "DONE_TODAY"),
    (821, "04:40", False, 980, "DUE"),
    (822, "17:41", False, 997, "FUTURE"),
    (823, "06:42", True, 1014, "DONE_TODAY"),
    (824, "19:43", False, 1031, "FUTURE"),
    (825, "08:44", False, 1048, "DUE"),
    (826, "21:45", True, 1065, "DONE_TODAY"),
    (827, "10:46", False, 1082, "DUE"),
    (828, "23:47", False, 1099, "FUTURE"),
    (829, "12:48", True, 1116, "DONE_TODAY"),
    (830, "01:49", False, 1133, "DUE"),
    (831, "14:50", False, 1150, "DUE"),
    (832, "03:51", True, 1167, "DONE_TODAY"),
    (833, "16:52", False, 1184, "DUE"),
    (834, "05:53", False, 1201, "DUE"),
    (835, "18:54", True, 1218, "DONE_TODAY"),
    (836, "07:55", False, 1235, "DUE"),
    (837, "20:56", False, 1252, "FUTURE"),
    (838, "09:57", True, 1269, "DONE_TODAY"),
    (839, "22:58", False, 1286, "FUTURE"),
    (840, "11:59", False, 1303, "DUE"),
    (841, "00:00", True, 1320, "DONE_TODAY"),
    (842, "13:01", False, 1337, "DUE"),
    (843, "02:02", False, 1354, "DUE"),
    (844, "15:03", True, 1371, "DONE_TODAY"),
    (845, "04:04", False, 1388, "DUE"),
    (846, "17:05", False, 1405, "DUE"),
    (847, "06:06", True, 1422, "DONE_TODAY"),
    (848, "19:07", False, 1439, "DUE"),
    (849, "08:08", False, 16, "FUTURE"),
    (850, "21:09", True, 33, "DONE_TODAY"),
    (851, "10:10", False, 50, "FUTURE"),
    (852, "23:11", False, 67, "FUTURE"),
    (853, "12:12", True, 84, "DONE_TODAY"),
    (854, "01:13", False, 101, "DUE"),
    (855, "14:14", False, 118, "FUTURE"),
    (856, "03:15", True, 135, "DONE_TODAY"),
    (857, "16:16", False, 152, "FUTURE"),
    (858, "05:17", False, 169, "FUTURE"),
    (859, "18:18", True, 186, "DONE_TODAY"),
    (860, "07:19", False, 203, "FUTURE"),
    (861, "20:20", False, 220, "FUTURE"),
    (862, "09:21", True, 237, "DONE_TODAY"),
    (863, "22:22", False, 254, "FUTURE"),
    (864, "11:23", False, 271, "FUTURE"),
    (865, "00:24", True, 288, "DONE_TODAY"),
    (866, "13:25", False, 305, "FUTURE"),
    (867, "02:26", False, 322, "DUE"),
    (868, "15:27", True, 339, "DONE_TODAY"),
    (869, "04:28", False, 356, "DUE"),
    (870, "17:29", False, 373, "FUTURE"),
    (871, "06:30", True, 390, "DONE_TODAY"),
    (872, "19:31", False, 407, "FUTURE"),
    (873, "08:32", False, 424, "FUTURE"),
    (874, "21:33", True, 441, "DONE_TODAY"),
    (875, "10:34", False, 458, "FUTURE"),
    (876, "23:35", False, 475, "FUTURE"),
    (877, "12:36", True, 492, "DONE_TODAY"),
    (878, "01:37", False, 509, "DUE"),
    (879, "14:38", False, 526, "FUTURE"),
    (880, "03:39", True, 543, "DONE_TODAY"),
    (881, "16:40", False, 560, "FUTURE"),
    (882, "05:41", False, 577, "DUE"),
    (883, "18:42", True, 594, "DONE_TODAY"),
    (884, "07:43", False, 611, "DUE"),
    (885, "20:44", False, 628, "FUTURE"),
    (886, "09:45", True, 645, "DONE_TODAY"),
    (887, "22:46", False, 662, "FUTURE"),
    (888, "11:47", False, 679, "FUTURE"),
    (889, "00:48", True, 696, "DONE_TODAY"),
    (890, "13:49", False, 713, "FUTURE"),
    (891, "02:50", False, 730, "DUE"),
    (892, "15:51", True, 747, "DONE_TODAY"),
    (893, "04:52", False, 764, "DUE"),
    (894, "17:53", False, 781, "FUTURE"),
    (895, "06:54", True, 798, "DONE_TODAY"),
    (896, "19:55", False, 815, "FUTURE"),
    (897, "08:56", False, 832, "DUE"),
    (898, "21:57", True, 849, "DONE_TODAY"),
    (899, "10:58", False, 866, "DUE"),
    (900, "23:59", False, 883, "FUTURE"),
    (901, "12:00", True, 900, "DONE_TODAY"),
    (902, "01:01", False, 917, "DUE"),
    (903, "14:02", False, 934, "DUE"),
    (904, "03:03", True, 951, "DONE_TODAY"),
    (905, "16:04", False, 968, "DUE"),
    (906, "05:05", False, 985, "DUE"),
    (907, "18:06", True, 1002, "DONE_TODAY"),
    (908, "07:07", False, 1019, "DUE"),
    (909, "20:08", False, 1036, "FUTURE"),
    (910, "09:09", True, 1053, "DONE_TODAY"),
    (911, "22:10", False, 1070, "FUTURE"),
    (912, "11:11", False, 1087, "DUE"),
    (913, "00:12", True, 1104, "DONE_TODAY"),
    (914, "13:13", False, 1121, "DUE"),
    (915, "02:14", False, 1138, "DUE"),
    (916, "15:15", True, 1155, "DONE_TODAY"),
    (917, "04:16", False, 1172, "DUE"),
    (918, "17:17", False, 1189, "DUE"),
    (919, "06:18", True, 1206, "DONE_TODAY"),
    (920, "19:19", False, 1223, "DUE"),
    (921, "08:20", False, 1240, "DUE"),
    (922, "21:21", True, 1257, "DONE_TODAY"),
    (923, "10:22", False, 1274, "DUE"),
    (924, "23:23", False, 1291, "FUTURE"),
    (925, "12:24", True, 1308, "DONE_TODAY"),
    (926, "01:25", False, 1325, "DUE"),
    (927, "14:26", False, 1342, "DUE"),
    (928, "03:27", True, 1359, "DONE_TODAY"),
    (929, "16:28", False, 1376, "DUE"),
    (930, "05:29", False, 1393, "DUE"),
    (931, "18:30", True, 1410, "DONE_TODAY"),
    (932, "07:31", False, 1427, "DUE"),
    (933, "20:32", False, 4, "FUTURE"),
    (934, "09:33", True, 21, "DONE_TODAY"),
    (935, "22:34", False, 38, "FUTURE"),
    (936, "11:35", False, 55, "FUTURE"),
    (937, "00:36", True, 72, "DONE_TODAY"),
    (938, "13:37", False, 89, "FUTURE"),
    (939, "02:38", False, 106, "FUTURE"),
    (940, "15:39", True, 123, "DONE_TODAY"),
    (941, "04:40", False, 140, "FUTURE"),
    (942, "17:41", False, 157, "FUTURE"),
    (943, "06:42", True, 174, "DONE_TODAY"),
    (944, "19:43", False, 191, "FUTURE"),
    (945, "08:44", False, 208, "FUTURE"),
    (946, "21:45", True, 225, "DONE_TODAY"),
    (947, "10:46", False, 242, "FUTURE"),
    (948, "23:47", False, 259, "FUTURE"),
    (949, "12:48", True, 276, "DONE_TODAY"),
    (950, "01:49", False, 293, "DUE"),
    (951, "14:50", False, 310, "FUTURE"),
    (952, "03:51", True, 327, "DONE_TODAY"),
    (953, "16:52", False, 344, "FUTURE"),
    (954, "05:53", False, 361, "DUE"),
    (955, "18:54", True, 378, "DONE_TODAY"),
    (956, "07:55", False, 395, "FUTURE"),
    (957, "20:56", False, 412, "FUTURE"),
    (958, "09:57", True, 429, "DONE_TODAY"),
    (959, "22:58", False, 446, "FUTURE"),
    (960, "11:59", False, 463, "FUTURE"),
    (961, "00:00", True, 480, "DONE_TODAY"),
    (962, "13:01", False, 497, "FUTURE"),
    (963, "02:02", False, 514, "DUE"),
    (964, "15:03", True, 531, "DONE_TODAY"),
    (965, "04:04", False, 548, "DUE"),
    (966, "17:05", False, 565, "FUTURE"),
    (967, "06:06", True, 582, "DONE_TODAY"),
    (968, "19:07", False, 599, "FUTURE"),
    (969, "08:08", False, 616, "DUE"),
    (970, "21:09", True, 633, "DONE_TODAY"),
    (971, "10:10", False, 650, "DUE"),
    (972, "23:11", False, 667, "FUTURE"),
    (973, "12:12", True, 684, "DONE_TODAY"),
    (974, "01:13", False, 701, "DUE"),
    (975, "14:14", False, 718, "FUTURE"),
    (976, "03:15", True, 735, "DONE_TODAY"),
    (977, "16:16", False, 752, "FUTURE"),
    (978, "05:17", False, 769, "DUE"),
    (979, "18:18", True, 786, "DONE_TODAY"),
    (980, "07:19", False, 803, "DUE"),
    (981, "20:20", False, 820, "FUTURE"),
    (982, "09:21", True, 837, "DONE_TODAY"),
    (983, "22:22", False, 854, "FUTURE"),
    (984, "11:23", False, 871, "DUE"),
    (985, "00:24", True, 888, "DONE_TODAY"),
    (986, "13:25", False, 905, "DUE"),
    (987, "02:26", False, 922, "DUE"),
    (988, "15:27", True, 939, "DONE_TODAY"),
    (989, "04:28", False, 956, "DUE"),
    (990, "17:29", False, 973, "FUTURE"),
    (991, "06:30", True, 990, "DONE_TODAY"),
    (992, "19:31", False, 1007, "FUTURE"),
    (993, "08:32", False, 1024, "DUE"),
    (994, "21:33", True, 1041, "DONE_TODAY"),
    (995, "10:34", False, 1058, "DUE"),
    (996, "23:35", False, 1075, "FUTURE"),
    (997, "12:36", True, 1092, "DONE_TODAY"),
    (998, "01:37", False, 1109, "DUE"),
    (999, "14:38", False, 1126, "DUE"),
    (1000, "03:39", True, 1143, "DONE_TODAY"),
    (1001, "16:40", False, 1160, "DUE"),
    (1002, "05:41", False, 1177, "DUE"),
    (1003, "18:42", True, 1194, "DONE_TODAY"),
    (1004, "07:43", False, 1211, "DUE"),
    (1005, "20:44", False, 1228, "FUTURE"),
    (1006, "09:45", True, 1245, "DONE_TODAY"),
    (1007, "22:46", False, 1262, "FUTURE"),
    (1008, "11:47", False, 1279, "DUE"),
    (1009, "00:48", True, 1296, "DONE_TODAY"),
    (1010, "13:49", False, 1313, "DUE"),
    (1011, "02:50", False, 1330, "DUE"),
    (1012, "15:51", True, 1347, "DONE_TODAY"),
    (1013, "04:52", False, 1364, "DUE"),
    (1014, "17:53", False, 1381, "DUE"),
    (1015, "06:54", True, 1398, "DONE_TODAY"),
    (1016, "19:55", False, 1415, "DUE"),
    (1017, "08:56", False, 1432, "DUE"),
    (1018, "21:57", True, 9, "DONE_TODAY"),
    (1019, "10:58", False, 26, "FUTURE"),
    (1020, "23:59", False, 43, "FUTURE"),
    (1021, "12:00", True, 60, "DONE_TODAY"),
    (1022, "01:01", False, 77, "DUE"),
    (1023, "14:02", False, 94, "FUTURE"),
    (1024, "03:03", True, 111, "DONE_TODAY"),
    (1025, "16:04", False, 128, "FUTURE"),
    (1026, "05:05", False, 145, "FUTURE"),
    (1027, "18:06", True, 162, "DONE_TODAY"),
    (1028, "07:07", False, 179, "FUTURE"),
    (1029, "20:08", False, 196, "FUTURE"),
    (1030, "09:09", True, 213, "DONE_TODAY"),
    (1031, "22:10", False, 230, "FUTURE"),
    (1032, "11:11", False, 247, "FUTURE"),
    (1033, "00:12", True, 264, "DONE_TODAY"),
    (1034, "13:13", False, 281, "FUTURE"),
    (1035, "02:14", False, 298, "DUE"),
    (1036, "15:15", True, 315, "DONE_TODAY"),
    (1037, "04:16", False, 332, "DUE"),
    (1038, "17:17", False, 349, "FUTURE"),
    (1039, "06:18", True, 366, "DONE_TODAY"),
    (1040, "19:19", False, 383, "FUTURE"),
    (1041, "08:20", False, 400, "FUTURE"),
    (1042, "21:21", True, 417, "DONE_TODAY"),
    (1043, "10:22", False, 434, "FUTURE"),
    (1044, "23:23", False, 451, "FUTURE"),
    (1045, "12:24", True, 468, "DONE_TODAY"),
    (1046, "01:25", False, 485, "DUE"),
    (1047, "14:26", False, 502, "FUTURE"),
    (1048, "03:27", True, 519, "DONE_TODAY"),
    (1049, "16:28", False, 536, "FUTURE"),
    (1050, "05:29", False, 553, "DUE"),
    (1051, "18:30", True, 570, "DONE_TODAY"),
    (1052, "07:31", False, 587, "DUE"),
    (1053, "20:32", False, 604, "FUTURE"),
    (1054, "09:33", True, 621, "DONE_TODAY"),
    (1055, "22:34", False, 638, "FUTURE"),
    (1056, "11:35", False, 655, "FUTURE"),
    (1057, "00:36", True, 672, "DONE_TODAY"),
    (1058, "13:37", False, 689, "FUTURE"),
    (1059, "02:38", False, 706, "DUE"),
    (1060, "15:39", True, 723, "DONE_TODAY"),
    (1061, "04:40", False, 740, "DUE"),
    (1062, "17:41", False, 757, "FUTURE"),
    (1063, "06:42", True, 774, "DONE_TODAY"),
    (1064, "19:43", False, 791, "FUTURE"),
    (1065, "08:44", False, 808, "DUE"),
    (1066, "21:45", True, 825, "DONE_TODAY"),
    (1067, "10:46", False, 842, "DUE"),
    (1068, "23:47", False, 859, "FUTURE"),
    (1069, "12:48", True, 876, "DONE_TODAY"),
    (1070, "01:49", False, 893, "DUE"),
    (1071, "14:50", False, 910, "DUE"),
    (1072, "03:51", True, 927, "DONE_TODAY"),
    (1073, "16:52", False, 944, "FUTURE"),
    (1074, "05:53", False, 961, "DUE"),
    (1075, "18:54", True, 978, "DONE_TODAY"),
    (1076, "07:55", False, 995, "DUE"),
    (1077, "20:56", False, 1012, "FUTURE"),
    (1078, "09:57", True, 1029, "DONE_TODAY"),
    (1079, "22:58", False, 1046, "FUTURE"),
    (1080, "11:59", False, 1063, "DUE"),
    (1081, "00:00", True, 1080, "DONE_TODAY"),
    (1082, "13:01", False, 1097, "DUE"),
    (1083, "02:02", False, 1114, "DUE"),
    (1084, "15:03", True, 1131, "DONE_TODAY"),
    (1085, "04:04", False, 1148, "DUE"),
    (1086, "17:05", False, 1165, "DUE"),
    (1087, "06:06", True, 1182, "DONE_TODAY"),
    (1088, "19:07", False, 1199, "DUE"),
    (1089, "08:08", False, 1216, "DUE"),
    (1090, "21:09", True, 1233, "DONE_TODAY"),
    (1091, "10:10", False, 1250, "DUE"),
    (1092, "23:11", False, 1267, "FUTURE"),
    (1093, "12:12", True, 1284, "DONE_TODAY"),
    (1094, "01:13", False, 1301, "DUE"),
    (1095, "14:14", False, 1318, "DUE"),
    (1096, "03:15", True, 1335, "DONE_TODAY"),
    (1097, "16:16", False, 1352, "DUE"),
    (1098, "05:17", False, 1369, "DUE"),
    (1099, "18:18", True, 1386, "DONE_TODAY"),
    (1100, "07:19", False, 1403, "DUE"),
    (1101, "20:20", False, 1420, "DUE"),
    (1102, "09:21", True, 1437, "DONE_TODAY"),
    (1103, "22:22", False, 14, "FUTURE"),
    (1104, "11:23", False, 31, "FUTURE"),
    (1105, "00:24", True, 48, "DONE_TODAY"),
    (1106, "13:25", False, 65, "FUTURE"),
    (1107, "02:26", False, 82, "FUTURE"),
    (1108, "15:27", True, 99, "DONE_TODAY"),
    (1109, "04:28", False, 116, "FUTURE"),
    (1110, "17:29", False, 133, "FUTURE"),
    (1111, "06:30", True, 150, "DONE_TODAY"),
    (1112, "19:31", False, 167, "FUTURE"),
    (1113, "08:32", False, 184, "FUTURE"),
    (1114, "21:33", True, 201, "DONE_TODAY"),
    (1115, "10:34", False, 218, "FUTURE"),
    (1116, "23:35", False, 235, "FUTURE"),
    (1117, "12:36", True, 252, "DONE_TODAY"),
    (1118, "01:37", False, 269, "DUE"),
    (1119, "14:38", False, 286, "FUTURE"),
    (1120, "03:39", True, 303, "DONE_TODAY"),
    (1121, "16:40", False, 320, "FUTURE"),
    (1122, "05:41", False, 337, "FUTURE"),
    (1123, "18:42", True, 354, "DONE_TODAY"),
    (1124, "07:43", False, 371, "FUTURE"),
    (1125, "20:44", False, 388, "FUTURE"),
    (1126, "09:45", True, 405, "DONE_TODAY"),
    (1127, "22:46", False, 422, "FUTURE"),
    (1128, "11:47", False, 439, "FUTURE"),
    (1129, "00:48", True, 456, "DONE_TODAY"),
    (1130, "13:49", False, 473, "FUTURE"),
    (1131, "02:50", False, 490, "DUE"),
    (1132, "15:51", True, 507, "DONE_TODAY"),
    (1133, "04:52", False, 524, "DUE"),
    (1134, "17:53", False, 541, "FUTURE"),
    (1135, "06:54", True, 558, "DONE_TODAY"),
    (1136, "19:55", False, 575, "FUTURE"),
    (1137, "08:56", False, 592, "DUE"),
    (1138, "21:57", True, 609, "DONE_TODAY"),
    (1139, "10:58", False, 626, "FUTURE"),
    (1140, "23:59", False, 643, "FUTURE"),
    (1141, "12:00", True, 660, "DONE_TODAY"),
    (1142, "01:01", False, 677, "DUE"),
    (1143, "14:02", False, 694, "FUTURE"),
    (1144, "03:03", True, 711, "DONE_TODAY"),
    (1145, "16:04", False, 728, "FUTURE"),
    (1146, "05:05", False, 745, "DUE"),
    (1147, "18:06", True, 762, "DONE_TODAY"),
    (1148, "07:07", False, 779, "DUE"),
    (1149, "20:08", False, 796, "FUTURE"),
    (1150, "09:09", True, 813, "DONE_TODAY"),
    (1151, "22:10", False, 830, "FUTURE"),
    (1152, "11:11", False, 847, "DUE"),
    (1153, "00:12", True, 864, "DONE_TODAY"),
    (1154, "13:13", False, 881, "DUE"),
    (1155, "02:14", False, 898, "DUE"),
    (1156, "15:15", True, 915, "DONE_TODAY"),
    (1157, "04:16", False, 932, "DUE"),
    (1158, "17:17", False, 949, "FUTURE"),
    (1159, "06:18", True, 966, "DONE_TODAY"),
    (1160, "19:19", False, 983, "FUTURE"),
    (1161, "08:20", False, 1000, "DUE"),
    (1162, "21:21", True, 1017, "DONE_TODAY"),
    (1163, "10:22", False, 1034, "DUE"),
    (1164, "23:23", False, 1051, "FUTURE"),
    (1165, "12:24", True, 1068, "DONE_TODAY"),
    (1166, "01:25", False, 1085, "DUE"),
    (1167, "14:26", False, 1102, "DUE"),
    (1168, "03:27", True, 1119, "DONE_TODAY"),
    (1169, "16:28", False, 1136, "DUE"),
    (1170, "05:29", False, 1153, "DUE"),
    (1171, "18:30", True, 1170, "DONE_TODAY"),
    (1172, "07:31", False, 1187, "DUE"),
    (1173, "20:32", False, 1204, "FUTURE"),
    (1174, "09:33", True, 1221, "DONE_TODAY"),
    (1175, "22:34", False, 1238, "FUTURE"),
    (1176, "11:35", False, 1255, "DUE"),
    (1177, "00:36", True, 1272, "DONE_TODAY"),
    (1178, "13:37", False, 1289, "DUE"),
    (1179, "02:38", False, 1306, "DUE"),
    (1180, "15:39", True, 1323, "DONE_TODAY"),
    (1181, "04:40", False, 1340, "DUE"),
    (1182, "17:41", False, 1357, "DUE"),
    (1183, "06:42", True, 1374, "DONE_TODAY"),
    (1184, "19:43", False, 1391, "DUE"),
    (1185, "08:44", False, 1408, "DUE"),
    (1186, "21:45", True, 1425, "DONE_TODAY"),
    (1187, "10:46", False, 2, "FUTURE"),
    (1188, "23:47", False, 19, "FUTURE"),
    (1189, "12:48", True, 36, "DONE_TODAY"),
    (1190, "01:49", False, 53, "FUTURE"),
    (1191, "14:50", False, 70, "FUTURE"),
    (1192, "03:51", True, 87, "DONE_TODAY"),
    (1193, "16:52", False, 104, "FUTURE"),
    (1194, "05:53", False, 121, "FUTURE"),
    (1195, "18:54", True, 138, "DONE_TODAY"),
    (1196, "07:55", False, 155, "FUTURE"),
    (1197, "20:56", False, 172, "FUTURE"),
    (1198, "09:57", True, 189, "DONE_TODAY"),
    (1199, "22:58", False, 206, "FUTURE"),
    (1200, "11:59", False, 223, "FUTURE"),
    (1201, "00:00", True, 240, "DONE_TODAY"),
    (1202, "13:01", False, 257, "FUTURE"),
    (1203, "02:02", False, 274, "DUE"),
    (1204, "15:03", True, 291, "DONE_TODAY"),
    (1205, "04:04", False, 308, "DUE"),
    (1206, "17:05", False, 325, "FUTURE"),
    (1207, "06:06", True, 342, "DONE_TODAY"),
    (1208, "19:07", False, 359, "FUTURE"),
    (1209, "08:08", False, 376, "FUTURE"),
    (1210, "21:09", True, 393, "DONE_TODAY"),
    (1211, "10:10", False, 410, "FUTURE"),
    (1212, "23:11", False, 427, "FUTURE"),
    (1213, "12:12", True, 444, "DONE_TODAY"),
    (1214, "01:13", False, 461, "DUE"),
    (1215, "14:14", False, 478, "FUTURE"),
    (1216, "03:15", True, 495, "DONE_TODAY"),
    (1217, "16:16", False, 512, "FUTURE"),
    (1218, "05:17", False, 529, "DUE"),
    (1219, "18:18", True, 546, "DONE_TODAY"),
    (1220, "07:19", False, 563, "DUE"),
    (1221, "20:20", False, 580, "FUTURE"),
    (1222, "09:21", True, 597, "DONE_TODAY"),
    (1223, "22:22", False, 614, "FUTURE"),
    (1224, "11:23", False, 631, "FUTURE"),
    (1225, "00:24", True, 648, "DONE_TODAY"),
    (1226, "13:25", False, 665, "FUTURE"),
    (1227, "02:26", False, 682, "DUE"),
    (1228, "15:27", True, 699, "DONE_TODAY"),
    (1229, "04:28", False, 716, "DUE"),
    (1230, "17:29", False, 733, "FUTURE"),
    (1231, "06:30", True, 750, "DONE_TODAY"),
    (1232, "19:31", False, 767, "FUTURE"),
    (1233, "08:32", False, 784, "DUE"),
    (1234, "21:33", True, 801, "DONE_TODAY"),
    (1235, "10:34", False, 818, "DUE"),
    (1236, "23:35", False, 835, "FUTURE"),
    (1237, "12:36", True, 852, "DONE_TODAY"),
    (1238, "01:37", False, 869, "DUE"),
    (1239, "14:38", False, 886, "DUE"),
    (1240, "03:39", True, 903, "DONE_TODAY"),
    (1241, "16:40", False, 920, "FUTURE"),
    (1242, "05:41", False, 937, "DUE"),
    (1243, "18:42", True, 954, "DONE_TODAY"),
    (1244, "07:43", False, 971, "DUE"),
    (1245, "20:44", False, 988, "FUTURE"),
    (1246, "09:45", True, 1005, "DONE_TODAY"),
    (1247, "22:46", False, 1022, "FUTURE"),
    (1248, "11:47", False, 1039, "DUE"),
    (1249, "00:48", True, 1056, "DONE_TODAY"),
    (1250, "13:49", False, 1073, "DUE"),
    (1251, "02:50", False, 1090, "DUE"),
    (1252, "15:51", True, 1107, "DONE_TODAY"),
    (1253, "04:52", False, 1124, "DUE"),
    (1254, "17:53", False, 1141, "DUE"),
    (1255, "06:54", True, 1158, "DONE_TODAY"),
    (1256, "19:55", False, 1175, "FUTURE"),
    (1257, "08:56", False, 1192, "DUE"),
    (1258, "21:57", True, 1209, "DONE_TODAY"),
    (1259, "10:58", False, 1226, "DUE"),
    (1260, "23:59", False, 1243, "FUTURE"),
    (1261, "12:00", True, 1260, "DONE_TODAY"),
    (1262, "01:01", False, 1277, "DUE"),
    (1263, "14:02", False, 1294, "DUE"),
    (1264, "03:03", True, 1311, "DONE_TODAY"),
    (1265, "16:04", False, 1328, "DUE"),
    (1266, "05:05", False, 1345, "DUE"),
    (1267, "18:06", True, 1362, "DONE_TODAY"),
    (1268, "07:07", False, 1379, "DUE"),
    (1269, "20:08", False, 1396, "DUE"),
    (1270, "09:09", True, 1413, "DONE_TODAY"),
    (1271, "22:10", False, 1430, "DUE"),
    (1272, "11:11", False, 7, "FUTURE"),
    (1273, "00:12", True, 24, "DONE_TODAY"),
    (1274, "13:13", False, 41, "FUTURE"),
    (1275, "02:14", False, 58, "FUTURE"),
    (1276, "15:15", True, 75, "DONE_TODAY"),
    (1277, "04:16", False, 92, "FUTURE"),
    (1278, "17:17", False, 109, "FUTURE"),
    (1279, "06:18", True, 126, "DONE_TODAY"),
    (1280, "19:19", False, 143, "FUTURE"),
    (1281, "08:20", False, 160, "FUTURE"),
    (1282, "21:21", True, 177, "DONE_TODAY"),
    (1283, "10:22", False, 194, "FUTURE"),
    (1284, "23:23", False, 211, "FUTURE"),
    (1285, "12:24", True, 228, "DONE_TODAY"),
    (1286, "01:25", False, 245, "DUE"),
    (1287, "14:26", False, 262, "FUTURE"),
    (1288, "03:27", True, 279, "DONE_TODAY"),
    (1289, "16:28", False, 296, "FUTURE"),
    (1290, "05:29", False, 313, "FUTURE"),
    (1291, "18:30", True, 330, "DONE_TODAY"),
    (1292, "07:31", False, 347, "FUTURE"),
    (1293, "20:32", False, 364, "FUTURE"),
    (1294, "09:33", True, 381, "DONE_TODAY"),
    (1295, "22:34", False, 398, "FUTURE"),
    (1296, "11:35", False, 415, "FUTURE"),
    (1297, "00:36", True, 432, "DONE_TODAY"),
    (1298, "13:37", False, 449, "FUTURE"),
    (1299, "02:38", False, 466, "DUE"),
    (1300, "15:39", True, 483, "DONE_TODAY"),
    (1301, "04:40", False, 500, "DUE"),
    (1302, "17:41", False, 517, "FUTURE"),
    (1303, "06:42", True, 534, "DONE_TODAY"),
    (1304, "19:43", False, 551, "FUTURE"),
    (1305, "08:44", False, 568, "DUE"),
    (1306, "21:45", True, 585, "DONE_TODAY"),
    (1307, "10:46", False, 602, "FUTURE"),
    (1308, "23:47", False, 619, "FUTURE"),
    (1309, "12:48", True, 636, "DONE_TODAY"),
    (1310, "01:49", False, 653, "DUE"),
    (1311, "14:50", False, 670, "FUTURE"),
    (1312, "03:51", True, 687, "DONE_TODAY"),
    (1313, "16:52", False, 704, "FUTURE"),
    (1314, "05:53", False, 721, "DUE"),
    (1315, "18:54", True, 738, "DONE_TODAY"),
    (1316, "07:55", False, 755, "DUE"),
    (1317, "20:56", False, 772, "FUTURE"),
    (1318, "09:57", True, 789, "DONE_TODAY"),
    (1319, "22:58", False, 806, "FUTURE"),
    (1320, "11:59", False, 823, "DUE"),
    (1321, "00:00", True, 840, "DONE_TODAY"),
    (1322, "13:01", False, 857, "DUE"),
    (1323, "02:02", False, 874, "DUE"),
    (1324, "15:03", True, 891, "DONE_TODAY"),
    (1325, "04:04", False, 908, "DUE"),
    (1326, "17:05", False, 925, "FUTURE"),
    (1327, "06:06", True, 942, "DONE_TODAY"),
    (1328, "19:07", False, 959, "FUTURE"),
    (1329, "08:08", False, 976, "DUE"),
    (1330, "21:09", True, 993, "DONE_TODAY"),
    (1331, "10:10", False, 1010, "DUE"),
    (1332, "23:11", False, 1027, "FUTURE"),
    (1333, "12:12", True, 1044, "DONE_TODAY"),
    (1334, "01:13", False, 1061, "DUE"),
    (1335, "14:14", False, 1078, "DUE"),
    (1336, "03:15", True, 1095, "DONE_TODAY"),
    (1337, "16:16", False, 1112, "DUE"),
    (1338, "05:17", False, 1129, "DUE"),
    (1339, "18:18", True, 1146, "DONE_TODAY"),
    (1340, "07:19", False, 1163, "DUE"),
    (1341, "20:20", False, 1180, "FUTURE"),
    (1342, "09:21", True, 1197, "DONE_TODAY"),
    (1343, "22:22", False, 1214, "FUTURE"),
    (1344, "11:23", False, 1231, "DUE"),
    (1345, "00:24", True, 1248, "DONE_TODAY"),
    (1346, "13:25", False, 1265, "DUE"),
    (1347, "02:26", False, 1282, "DUE"),
    (1348, "15:27", True, 1299, "DONE_TODAY"),
    (1349, "04:28", False, 1316, "DUE"),
    (1350, "17:29", False, 1333, "DUE"),
    (1351, "06:30", True, 1350, "DONE_TODAY"),
    (1352, "19:31", False, 1367, "DUE"),
    (1353, "08:32", False, 1384, "DUE"),
    (1354, "21:33", True, 1401, "DONE_TODAY"),
    (1355, "10:34", False, 1418, "DUE"),
    (1356, "23:35", False, 1435, "DUE"),
    (1357, "12:36", True, 12, "DONE_TODAY"),
    (1358, "01:37", False, 29, "FUTURE"),
    (1359, "14:38", False, 46, "FUTURE"),
    (1360, "03:39", True, 63, "DONE_TODAY"),
    (1361, "16:40", False, 80, "FUTURE"),
    (1362, "05:41", False, 97, "FUTURE"),
    (1363, "18:42", True, 114, "DONE_TODAY"),
    (1364, "07:43", False, 131, "FUTURE"),
    (1365, "20:44", False, 148, "FUTURE"),
    (1366, "09:45", True, 165, "DONE_TODAY"),
    (1367, "22:46", False, 182, "FUTURE"),
    (1368, "11:47", False, 199, "FUTURE"),
    (1369, "00:48", True, 216, "DONE_TODAY"),
    (1370, "13:49", False, 233, "FUTURE"),
    (1371, "02:50", False, 250, "DUE"),
    (1372, "15:51", True, 267, "DONE_TODAY"),
    (1373, "04:52", False, 284, "FUTURE"),
    (1374, "17:53", False, 301, "FUTURE"),
    (1375, "06:54", True, 318, "DONE_TODAY"),
    (1376, "19:55", False, 335, "FUTURE"),
    (1377, "08:56", False, 352, "FUTURE"),
    (1378, "21:57", True, 369, "DONE_TODAY"),
    (1379, "10:58", False, 386, "FUTURE"),
    (1380, "23:59", False, 403, "FUTURE"),
    (1381, "12:00", True, 420, "DONE_TODAY"),
    (1382, "01:01", False, 437, "DUE"),
    (1383, "14:02", False, 454, "FUTURE"),
    (1384, "03:03", True, 471, "DONE_TODAY"),
    (1385, "16:04", False, 488, "FUTURE"),
    (1386, "05:05", False, 505, "DUE"),
    (1387, "18:06", True, 522, "DONE_TODAY"),
    (1388, "07:07", False, 539, "DUE"),
    (1389, "20:08", False, 556, "FUTURE"),
    (1390, "09:09", True, 573, "DONE_TODAY"),
    (1391, "22:10", False, 590, "FUTURE"),
    (1392, "11:11", False, 607, "FUTURE"),
    (1393, "00:12", True, 624, "DONE_TODAY"),
    (1394, "13:13", False, 641, "FUTURE"),
    (1395, "02:14", False, 658, "DUE"),
    (1396, "15:15", True, 675, "DONE_TODAY"),
    (1397, "04:16", False, 692, "DUE"),
    (1398, "17:17", False, 709, "FUTURE"),
    (1399, "06:18", True, 726, "DONE_TODAY"),
    (1400, "19:19", False, 743, "FUTURE"),
    (1401, "08:20", False, 760, "DUE"),
    (1402, "21:21", True, 777, "DONE_TODAY"),
    (1403, "10:22", False, 794, "DUE"),
    (1404, "23:23", False, 811, "FUTURE"),
    (1405, "12:24", True, 828, "DONE_TODAY"),
    (1406, "01:25", False, 845, "DUE"),
    (1407, "14:26", False, 862, "FUTURE"),
    (1408, "03:27", True, 879, "DONE_TODAY"),
    (1409, "16:28", False, 896, "FUTURE"),
    (1410, "05:29", False, 913, "DUE"),
    (1411, "18:30", True, 930, "DONE_TODAY"),
    (1412, "07:31", False, 947, "DUE"),
    (1413, "20:32", False, 964, "FUTURE"),
    (1414, "09:33", True, 981, "DONE_TODAY"),
    (1415, "22:34", False, 998, "FUTURE"),
    (1416, "11:35", False, 1015, "DUE"),
    (1417, "00:36", True, 1032, "DONE_TODAY"),
    (1418, "13:37", False, 1049, "DUE"),
    (1419, "02:38", False, 1066, "DUE"),
    (1420, "15:39", True, 1083, "DONE_TODAY"),
    (1421, "04:40", False, 1100, "DUE"),
    (1422, "17:41", False, 1117, "DUE"),
    (1423, "06:42", True, 1134, "DONE_TODAY"),
    (1424, "19:43", False, 1151, "FUTURE"),
    (1425, "08:44", False, 1168, "DUE"),
    (1426, "21:45", True, 1185, "DONE_TODAY"),
    (1427, "10:46", False, 1202, "DUE"),
    (1428, "23:47", False, 1219, "FUTURE"),
    (1429, "12:48", True, 1236, "DONE_TODAY"),
    (1430, "01:49", False, 1253, "DUE"),
    (1431, "14:50", False, 1270, "DUE"),
    (1432, "03:51", True, 1287, "DONE_TODAY"),
    (1433, "16:52", False, 1304, "DUE"),
    (1434, "05:53", False, 1321, "DUE"),
    (1435, "18:54", True, 1338, "DONE_TODAY"),
    (1436, "07:55", False, 1355, "DUE"),
    (1437, "20:56", False, 1372, "DUE"),
    (1438, "09:57", True, 1389, "DONE_TODAY"),
    (1439, "22:58", False, 1406, "DUE"),
    (1440, "11:59", False, 1423, "DUE"),
    (1441, "00:00", True, 0, "DONE_TODAY"),
    (1442, "13:01", False, 17, "FUTURE"),
    (1443, "02:02", False, 34, "FUTURE"),
    (1444, "15:03", True, 51, "DONE_TODAY"),
    (1445, "04:04", False, 68, "FUTURE"),
    (1446, "17:05", False, 85, "FUTURE"),
    (1447, "06:06", True, 102, "DONE_TODAY"),
    (1448, "19:07", False, 119, "FUTURE"),
    (1449, "08:08", False, 136, "FUTURE"),
    (1450, "21:09", True, 153, "DONE_TODAY"),
    (1451, "10:10", False, 170, "FUTURE"),
    (1452, "23:11", False, 187, "FUTURE"),
    (1453, "12:12", True, 204, "DONE_TODAY"),
    (1454, "01:13", False, 221, "DUE"),
    (1455, "14:14", False, 238, "FUTURE"),
    (1456, "03:15", True, 255, "DONE_TODAY"),
    (1457, "16:16", False, 272, "FUTURE"),
    (1458, "05:17", False, 289, "FUTURE"),
    (1459, "18:18", True, 306, "DONE_TODAY"),
    (1460, "07:19", False, 323, "FUTURE"),
    (1461, "20:20", False, 340, "FUTURE"),
    (1462, "09:21", True, 357, "DONE_TODAY"),
    (1463, "22:22", False, 374, "FUTURE"),
    (1464, "11:23", False, 391, "FUTURE"),
    (1465, "00:24", True, 408, "DONE_TODAY"),
    (1466, "13:25", False, 425, "FUTURE"),
    (1467, "02:26", False, 442, "DUE"),
    (1468, "15:27", True, 459, "DONE_TODAY"),
    (1469, "04:28", False, 476, "DUE"),
    (1470, "17:29", False, 493, "FUTURE"),
    (1471, "06:30", True, 510, "DONE_TODAY"),
    (1472, "19:31", False, 527, "FUTURE"),
    (1473, "08:32", False, 544, "DUE"),
    (1474, "21:33", True, 561, "DONE_TODAY"),
    (1475, "10:34", False, 578, "FUTURE"),
    (1476, "23:35", False, 595, "FUTURE"),
    (1477, "12:36", True, 612, "DONE_TODAY"),
    (1478, "01:37", False, 629, "DUE"),
    (1479, "14:38", False, 646, "FUTURE"),
    (1480, "03:39", True, 663, "DONE_TODAY"),
    (1481, "16:40", False, 680, "FUTURE"),
    (1482, "05:41", False, 697, "DUE"),
    (1483, "18:42", True, 714, "DONE_TODAY"),
    (1484, "07:43", False, 731, "DUE"),
    (1485, "20:44", False, 748, "FUTURE"),
    (1486, "09:45", True, 765, "DONE_TODAY"),
    (1487, "22:46", False, 782, "FUTURE"),
    (1488, "11:47", False, 799, "DUE"),
    (1489, "00:48", True, 816, "DONE_TODAY"),
    (1490, "13:49", False, 833, "DUE"),
    (1491, "02:50", False, 850, "DUE"),
    (1492, "15:51", True, 867, "DONE_TODAY"),
    (1493, "04:52", False, 884, "DUE"),
    (1494, "17:53", False, 901, "FUTURE"),
    (1495, "06:54", True, 918, "DONE_TODAY"),
    (1496, "19:55", False, 935, "FUTURE"),
    (1497, "08:56", False, 952, "DUE"),
    (1498, "21:57", True, 969, "DONE_TODAY"),
    (1499, "10:58", False, 986, "DUE"),
    (1500, "23:59", False, 1003, "FUTURE"),
    (1501, "12:00", True, 1020, "DONE_TODAY"),
    (1502, "01:01", False, 1037, "DUE"),
    (1503, "14:02", False, 1054, "DUE"),
    (1504, "03:03", True, 1071, "DONE_TODAY"),
    (1505, "16:04", False, 1088, "DUE"),
    (1506, "05:05", False, 1105, "DUE"),
    (1507, "18:06", True, 1122, "DONE_TODAY"),
    (1508, "07:07", False, 1139, "DUE"),
    (1509, "20:08", False, 1156, "FUTURE"),
    (1510, "09:09", True, 1173, "DONE_TODAY"),
    (1511, "22:10", False, 1190, "FUTURE"),
    (1512, "11:11", False, 1207, "DUE"),
    (1513, "00:12", True, 1224, "DONE_TODAY"),
    (1514, "13:13", False, 1241, "DUE"),
    (1515, "02:14", False, 1258, "DUE"),
    (1516, "15:15", True, 1275, "DONE_TODAY"),
    (1517, "04:16", False, 1292, "DUE"),
    (1518, "17:17", False, 1309, "DUE"),
    (1519, "06:18", True, 1326, "DONE_TODAY"),
    (1520, "19:19", False, 1343, "DUE"),
    (1521, "08:20", False, 1360, "DUE"),
    (1522, "21:21", True, 1377, "DONE_TODAY"),
    (1523, "10:22", False, 1394, "DUE"),
    (1524, "23:23", False, 1411, "DUE"),
    (1525, "12:24", True, 1428, "DONE_TODAY"),
    (1526, "01:25", False, 5, "FUTURE"),
    (1527, "14:26", False, 22, "FUTURE"),
    (1528, "03:27", True, 39, "DONE_TODAY"),
    (1529, "16:28", False, 56, "FUTURE"),
    (1530, "05:29", False, 73, "FUTURE"),
    (1531, "18:30", True, 90, "DONE_TODAY"),
    (1532, "07:31", False, 107, "FUTURE"),
    (1533, "20:32", False, 124, "FUTURE"),
    (1534, "09:33", True, 141, "DONE_TODAY"),
    (1535, "22:34", False, 158, "FUTURE"),
    (1536, "11:35", False, 175, "FUTURE"),
    (1537, "00:36", True, 192, "DONE_TODAY"),
    (1538, "13:37", False, 209, "FUTURE"),
    (1539, "02:38", False, 226, "DUE"),
    (1540, "15:39", True, 243, "DONE_TODAY"),
    (1541, "04:40", False, 260, "FUTURE"),
    (1542, "17:41", False, 277, "FUTURE"),
    (1543, "06:42", True, 294, "DONE_TODAY"),
    (1544, "19:43", False, 311, "FUTURE"),
    (1545, "08:44", False, 328, "FUTURE"),
    (1546, "21:45", True, 345, "DONE_TODAY"),
    (1547, "10:46", False, 362, "FUTURE"),
    (1548, "23:47", False, 379, "FUTURE"),
    (1549, "12:48", True, 396, "DONE_TODAY"),
    (1550, "01:49", False, 413, "DUE"),
    (1551, "14:50", False, 430, "FUTURE"),
    (1552, "03:51", True, 447, "DONE_TODAY"),
    (1553, "16:52", False, 464, "FUTURE"),
    (1554, "05:53", False, 481, "DUE"),
    (1555, "18:54", True, 498, "DONE_TODAY"),
    (1556, "07:55", False, 515, "DUE"),
    (1557, "20:56", False, 532, "FUTURE"),
    (1558, "09:57", True, 549, "DONE_TODAY"),
    (1559, "22:58", False, 566, "FUTURE"),
    (1560, "11:59", False, 583, "FUTURE"),
    (1561, "00:00", True, 600, "DONE_TODAY"),
    (1562, "13:01", False, 617, "FUTURE"),
    (1563, "02:02", False, 634, "DUE"),
    (1564, "15:03", True, 651, "DONE_TODAY"),
    (1565, "04:04", False, 668, "DUE"),
    (1566, "17:05", False, 685, "FUTURE"),
    (1567, "06:06", True, 702, "DONE_TODAY"),
    (1568, "19:07", False, 719, "FUTURE"),
    (1569, "08:08", False, 736, "DUE"),
    (1570, "21:09", True, 753, "DONE_TODAY"),
    (1571, "10:10", False, 770, "DUE"),
    (1572, "23:11", False, 787, "FUTURE"),
    (1573, "12:12", True, 804, "DONE_TODAY"),
    (1574, "01:13", False, 821, "DUE"),
    (1575, "14:14", False, 838, "FUTURE"),
    (1576, "03:15", True, 855, "DONE_TODAY"),
    (1577, "16:16", False, 872, "FUTURE"),
    (1578, "05:17", False, 889, "DUE"),
    (1579, "18:18", True, 906, "DONE_TODAY"),
    (1580, "07:19", False, 923, "DUE"),
    (1581, "20:20", False, 940, "FUTURE"),
    (1582, "09:21", True, 957, "DONE_TODAY"),
    (1583, "22:22", False, 974, "FUTURE"),
    (1584, "11:23", False, 991, "DUE"),
    (1585, "00:24", True, 1008, "DONE_TODAY"),
    (1586, "13:25", False, 1025, "DUE"),
    (1587, "02:26", False, 1042, "DUE"),
    (1588, "15:27", True, 1059, "DONE_TODAY"),
    (1589, "04:28", False, 1076, "DUE"),
    (1590, "17:29", False, 1093, "DUE"),
    (1591, "06:30", True, 1110, "DONE_TODAY"),
    (1592, "19:31", False, 1127, "FUTURE"),
    (1593, "08:32", False, 1144, "DUE"),
    (1594, "21:33", True, 1161, "DONE_TODAY"),
    (1595, "10:34", False, 1178, "DUE"),
    (1596, "23:35", False, 1195, "FUTURE"),
    (1597, "12:36", True, 1212, "DONE_TODAY"),
    (1598, "01:37", False, 1229, "DUE"),
    (1599, "14:38", False, 1246, "DUE"),
    (1600, "03:39", True, 1263, "DONE_TODAY"),
    (1601, "16:40", False, 1280, "DUE"),
    (1602, "05:41", False, 1297, "DUE"),
    (1603, "18:42", True, 1314, "DONE_TODAY"),
    (1604, "07:43", False, 1331, "DUE"),
    (1605, "20:44", False, 1348, "DUE"),
    (1606, "09:45", True, 1365, "DONE_TODAY"),
    (1607, "22:46", False, 1382, "DUE"),
    (1608, "11:47", False, 1399, "DUE"),
    (1609, "00:48", True, 1416, "DONE_TODAY"),
    (1610, "13:49", False, 1433, "DUE"),
    (1611, "02:50", False, 10, "FUTURE"),
    (1612, "15:51", True, 27, "DONE_TODAY"),
    (1613, "04:52", False, 44, "FUTURE"),
    (1614, "17:53", False, 61, "FUTURE"),
    (1615, "06:54", True, 78, "DONE_TODAY"),
    (1616, "19:55", False, 95, "FUTURE"),
    (1617, "08:56", False, 112, "FUTURE"),
    (1618, "21:57", True, 129, "DONE_TODAY"),
    (1619, "10:58", False, 146, "FUTURE"),
    (1620, "23:59", False, 163, "FUTURE"),
    (1621, "12:00", True, 180, "DONE_TODAY"),
    (1622, "01:01", False, 197, "DUE"),
    (1623, "14:02", False, 214, "FUTURE"),
    (1624, "03:03", True, 231, "DONE_TODAY"),
    (1625, "16:04", False, 248, "FUTURE"),
    (1626, "05:05", False, 265, "FUTURE"),
    (1627, "18:06", True, 282, "DONE_TODAY"),
    (1628, "07:07", False, 299, "FUTURE"),
    (1629, "20:08", False, 316, "FUTURE"),
    (1630, "09:09", True, 333, "DONE_TODAY"),
    (1631, "22:10", False, 350, "FUTURE"),
    (1632, "11:11", False, 367, "FUTURE"),
    (1633, "00:12", True, 384, "DONE_TODAY"),
    (1634, "13:13", False, 401, "FUTURE"),
    (1635, "02:14", False, 418, "DUE"),
    (1636, "15:15", True, 435, "DONE_TODAY"),
    (1637, "04:16", False, 452, "DUE"),
    (1638, "17:17", False, 469, "FUTURE"),
    (1639, "06:18", True, 486, "DONE_TODAY"),
    (1640, "19:19", False, 503, "FUTURE"),
    (1641, "08:20", False, 520, "DUE"),
    (1642, "21:21", True, 537, "DONE_TODAY"),
    (1643, "10:22", False, 554, "FUTURE"),
    (1644, "23:23", False, 571, "FUTURE"),
    (1645, "12:24", True, 588, "DONE_TODAY"),
    (1646, "01:25", False, 605, "DUE"),
    (1647, "14:26", False, 622, "FUTURE"),
    (1648, "03:27", True, 639, "DONE_TODAY"),
    (1649, "16:28", False, 656, "FUTURE"),
    (1650, "05:29", False, 673, "DUE"),
    (1651, "18:30", True, 690, "DONE_TODAY"),
    (1652, "07:31", False, 707, "DUE"),
    (1653, "20:32", False, 724, "FUTURE"),
    (1654, "09:33", True, 741, "DONE_TODAY"),
    (1655, "22:34", False, 758, "FUTURE"),
    (1656, "11:35", False, 775, "DUE"),
    (1657, "00:36", True, 792, "DONE_TODAY"),
    (1658, "13:37", False, 809, "FUTURE"),
    (1659, "02:38", False, 826, "DUE"),
    (1660, "15:39", True, 843, "DONE_TODAY"),
    (1661, "04:40", False, 860, "DUE"),
    (1662, "17:41", False, 877, "FUTURE"),
    (1663, "06:42", True, 894, "DONE_TODAY"),
    (1664, "19:43", False, 911, "FUTURE"),
    (1665, "08:44", False, 928, "DUE"),
    (1666, "21:45", True, 945, "DONE_TODAY"),
    (1667, "10:46", False, 962, "DUE"),
    (1668, "23:47", False, 979, "FUTURE"),
    (1669, "12:48", True, 996, "DONE_TODAY"),
    (1670, "01:49", False, 1013, "DUE"),
    (1671, "14:50", False, 1030, "DUE"),
    (1672, "03:51", True, 1047, "DONE_TODAY"),
    (1673, "16:52", False, 1064, "DUE"),
    (1674, "05:53", False, 1081, "DUE"),
    (1675, "18:54", True, 1098, "DONE_TODAY"),
    (1676, "07:55", False, 1115, "DUE"),
    (1677, "20:56", False, 1132, "FUTURE"),
    (1678, "09:57", True, 1149, "DONE_TODAY"),
    (1679, "22:58", False, 1166, "FUTURE"),
    (1680, "11:59", False, 1183, "DUE"),
    (1681, "00:00", True, 1200, "DONE_TODAY"),
    (1682, "13:01", False, 1217, "DUE"),
    (1683, "02:02", False, 1234, "DUE"),
    (1684, "15:03", True, 1251, "DONE_TODAY"),
    (1685, "04:04", False, 1268, "DUE"),
    (1686, "17:05", False, 1285, "DUE"),
    (1687, "06:06", True, 1302, "DONE_TODAY"),
    (1688, "19:07", False, 1319, "DUE"),
    (1689, "08:08", False, 1336, "DUE"),
    (1690, "21:09", True, 1353, "DONE_TODAY"),
    (1691, "10:10", False, 1370, "DUE"),
    (1692, "23:11", False, 1387, "FUTURE"),
    (1693, "12:12", True, 1404, "DONE_TODAY"),
    (1694, "01:13", False, 1421, "DUE"),
    (1695, "14:14", False, 1438, "DUE"),
    (1696, "03:15", True, 15, "DONE_TODAY"),
    (1697, "16:16", False, 32, "FUTURE"),
    (1698, "05:17", False, 49, "FUTURE"),
    (1699, "18:18", True, 66, "DONE_TODAY"),
    (1700, "07:19", False, 83, "FUTURE"),
    (1701, "20:20", False, 100, "FUTURE"),
    (1702, "09:21", True, 117, "DONE_TODAY"),
    (1703, "22:22", False, 134, "FUTURE"),
    (1704, "11:23", False, 151, "FUTURE"),
    (1705, "00:24", True, 168, "DONE_TODAY"),
    (1706, "13:25", False, 185, "FUTURE"),
    (1707, "02:26", False, 202, "DUE"),
    (1708, "15:27", True, 219, "DONE_TODAY"),
    (1709, "04:28", False, 236, "FUTURE"),
    (1710, "17:29", False, 253, "FUTURE"),
    (1711, "06:30", True, 270, "DONE_TODAY"),
    (1712, "19:31", False, 287, "FUTURE"),
    (1713, "08:32", False, 304, "FUTURE"),
    (1714, "21:33", True, 321, "DONE_TODAY"),
    (1715, "10:34", False, 338, "FUTURE"),
    (1716, "23:35", False, 355, "FUTURE"),
    (1717, "12:36", True, 372, "DONE_TODAY"),
    (1718, "01:37", False, 389, "DUE"),
    (1719, "14:38", False, 406, "FUTURE"),
    (1720, "03:39", True, 423, "DONE_TODAY"),
    (1721, "16:40", False, 440, "FUTURE"),
    (1722, "05:41", False, 457, "DUE"),
    (1723, "18:42", True, 474, "DONE_TODAY"),
    (1724, "07:43", False, 491, "DUE"),
    (1725, "20:44", False, 508, "FUTURE"),
    (1726, "09:45", True, 525, "DONE_TODAY"),
    (1727, "22:46", False, 542, "FUTURE"),
    (1728, "11:47", False, 559, "FUTURE"),
    (1729, "00:48", True, 576, "DONE_TODAY"),
    (1730, "13:49", False, 593, "FUTURE"),
    (1731, "02:50", False, 610, "DUE"),
    (1732, "15:51", True, 627, "DONE_TODAY"),
    (1733, "04:52", False, 644, "DUE"),
    (1734, "17:53", False, 661, "FUTURE"),
    (1735, "06:54", True, 678, "DONE_TODAY"),
    (1736, "19:55", False, 695, "FUTURE"),
    (1737, "08:56", False, 712, "DUE"),
    (1738, "21:57", True, 729, "DONE_TODAY"),
    (1739, "10:58", False, 746, "DUE"),
    (1740, "23:59", False, 763, "FUTURE"),
    (1741, "12:00", True, 780, "DONE_TODAY"),
    (1742, "01:01", False, 797, "DUE"),
    (1743, "14:02", False, 814, "FUTURE"),
    (1744, "03:03", True, 831, "DONE_TODAY"),
    (1745, "16:04", False, 848, "FUTURE"),
    (1746, "05:05", False, 865, "DUE"),
    (1747, "18:06", True, 882, "DONE_TODAY"),
    (1748, "07:07", False, 899, "DUE"),
    (1749, "20:08", False, 916, "FUTURE"),
    (1750, "09:09", True, 933, "DONE_TODAY"),
    (1751, "22:10", False, 950, "FUTURE"),
    (1752, "11:11", False, 967, "DUE"),
    (1753, "00:12", True, 984, "DONE_TODAY"),
    (1754, "13:13", False, 1001, "DUE"),
    (1755, "02:14", False, 1018, "DUE"),
    (1756, "15:15", True, 1035, "DONE_TODAY"),
    (1757, "04:16", False, 1052, "DUE"),
    (1758, "17:17", False, 1069, "DUE"),
    (1759, "06:18", True, 1086, "DONE_TODAY"),
    (1760, "19:19", False, 1103, "FUTURE"),
    (1761, "08:20", False, 1120, "DUE"),
    (1762, "21:21", True, 1137, "DONE_TODAY"),
    (1763, "10:22", False, 1154, "DUE"),
    (1764, "23:23", False, 1171, "FUTURE"),
    (1765, "12:24", True, 1188, "DONE_TODAY"),
    (1766, "01:25", False, 1205, "DUE"),
    (1767, "14:26", False, 1222, "DUE"),
    (1768, "03:27", True, 1239, "DONE_TODAY"),
    (1769, "16:28", False, 1256, "DUE"),
    (1770, "05:29", False, 1273, "DUE"),
    (1771, "18:30", True, 1290, "DONE_TODAY"),
    (1772, "07:31", False, 1307, "DUE"),
    (1773, "20:32", False, 1324, "DUE"),
    (1774, "09:33", True, 1341, "DONE_TODAY"),
    (1775, "22:34", False, 1358, "DUE"),
    (1776, "11:35", False, 1375, "DUE"),
    (1777, "00:36", True, 1392, "DONE_TODAY"),
    (1778, "13:37", False, 1409, "DUE"),
    (1779, "02:38", False, 1426, "DUE"),
    (1780, "15:39", True, 3, "DONE_TODAY"),
    (1781, "04:40", False, 20, "FUTURE"),
    (1782, "17:41", False, 37, "FUTURE"),
    (1783, "06:42", True, 54, "DONE_TODAY"),
    (1784, "19:43", False, 71, "FUTURE"),
    (1785, "08:44", False, 88, "FUTURE"),
    (1786, "21:45", True, 105, "DONE_TODAY"),
    (1787, "10:46", False, 122, "FUTURE"),
    (1788, "23:47", False, 139, "FUTURE"),
    (1789, "12:48", True, 156, "DONE_TODAY"),
    (1790, "01:49", False, 173, "DUE"),
    (1791, "14:50", False, 190, "FUTURE"),
    (1792, "03:51", True, 207, "DONE_TODAY"),
    (1793, "16:52", False, 224, "FUTURE"),
    (1794, "05:53", False, 241, "FUTURE"),
    (1795, "18:54", True, 258, "DONE_TODAY"),
    (1796, "07:55", False, 275, "FUTURE"),
    (1797, "20:56", False, 292, "FUTURE"),
    (1798, "09:57", True, 309, "DONE_TODAY"),
    (1799, "22:58", False, 326, "FUTURE"),
    (1800, "11:59", False, 343, "FUTURE"),
    (1801, "00:00", True, 360, "DONE_TODAY"),
    (1802, "13:01", False, 377, "FUTURE"),
    (1803, "02:02", False, 394, "DUE"),
    (1804, "15:03", True, 411, "DONE_TODAY"),
    (1805, "04:04", False, 428, "DUE"),
    (1806, "17:05", False, 445, "FUTURE"),
    (1807, "06:06", True, 462, "DONE_TODAY"),
    (1808, "19:07", False, 479, "FUTURE"),
    (1809, "08:08", False, 496, "DUE"),
    (1810, "21:09", True, 513, "DONE_TODAY"),
    (1811, "10:10", False, 530, "FUTURE"),
    (1812, "23:11", False, 547, "FUTURE"),
    (1813, "12:12", True, 564, "DONE_TODAY"),
    (1814, "01:13", False, 581, "DUE"),
    (1815, "14:14", False, 598, "FUTURE"),
    (1816, "03:15", True, 615, "DONE_TODAY"),
    (1817, "16:16", False, 632, "FUTURE"),
    (1818, "05:17", False, 649, "DUE"),
    (1819, "18:18", True, 666, "DONE_TODAY"),
    (1820, "07:19", False, 683, "DUE"),
    (1821, "20:20", False, 700, "FUTURE"),
    (1822, "09:21", True, 717, "DONE_TODAY"),
    (1823, "22:22", False, 734, "FUTURE"),
    (1824, "11:23", False, 751, "DUE"),
    (1825, "00:24", True, 768, "DONE_TODAY"),
    (1826, "13:25", False, 785, "FUTURE"),
    (1827, "02:26", False, 802, "DUE"),
    (1828, "15:27", True, 819, "DONE_TODAY"),
    (1829, "04:28", False, 836, "DUE"),
    (1830, "17:29", False, 853, "FUTURE"),
    (1831, "06:30", True, 870, "DONE_TODAY"),
    (1832, "19:31", False, 887, "FUTURE"),
    (1833, "08:32", False, 904, "DUE"),
    (1834, "21:33", True, 921, "DONE_TODAY"),
    (1835, "10:34", False, 938, "DUE"),
    (1836, "23:35", False, 955, "FUTURE"),
    (1837, "12:36", True, 972, "DONE_TODAY"),
    (1838, "01:37", False, 989, "DUE"),
    (1839, "14:38", False, 1006, "DUE"),
    (1840, "03:39", True, 1023, "DONE_TODAY"),
    (1841, "16:40", False, 1040, "DUE"),
    (1842, "05:41", False, 1057, "DUE"),
    (1843, "18:42", True, 1074, "DONE_TODAY"),
    (1844, "07:43", False, 1091, "DUE"),
    (1845, "20:44", False, 1108, "FUTURE"),
    (1846, "09:45", True, 1125, "DONE_TODAY"),
    (1847, "22:46", False, 1142, "FUTURE"),
    (1848, "11:47", False, 1159, "DUE"),
    (1849, "00:48", True, 1176, "DONE_TODAY"),
    (1850, "13:49", False, 1193, "DUE"),
    (1851, "02:50", False, 1210, "DUE"),
    (1852, "15:51", True, 1227, "DONE_TODAY"),
    (1853, "04:52", False, 1244, "DUE"),
    (1854, "17:53", False, 1261, "DUE"),
    (1855, "06:54", True, 1278, "DONE_TODAY"),
    (1856, "19:55", False, 1295, "DUE"),
    (1857, "08:56", False, 1312, "DUE"),
    (1858, "21:57", True, 1329, "DONE_TODAY"),
    (1859, "10:58", False, 1346, "DUE"),
    (1860, "23:59", False, 1363, "FUTURE"),
    (1861, "12:00", True, 1380, "DONE_TODAY"),
    (1862, "01:01", False, 1397, "DUE"),
    (1863, "14:02", False, 1414, "DUE"),
    (1864, "03:03", True, 1431, "DONE_TODAY"),
    (1865, "16:04", False, 8, "FUTURE"),
    (1866, "05:05", False, 25, "FUTURE"),
    (1867, "18:06", True, 42, "DONE_TODAY"),
    (1868, "07:07", False, 59, "FUTURE"),
    (1869, "20:08", False, 76, "FUTURE"),
    (1870, "09:09", True, 93, "DONE_TODAY"),
    (1871, "22:10", False, 110, "FUTURE"),
    (1872, "11:11", False, 127, "FUTURE"),
    (1873, "00:12", True, 144, "DONE_TODAY"),
    (1874, "13:13", False, 161, "FUTURE"),
    (1875, "02:14", False, 178, "DUE"),
    (1876, "15:15", True, 195, "DONE_TODAY"),
    (1877, "04:16", False, 212, "FUTURE"),
    (1878, "17:17", False, 229, "FUTURE"),
    (1879, "06:18", True, 246, "DONE_TODAY"),
    (1880, "19:19", False, 263, "FUTURE"),
    (1881, "08:20", False, 280, "FUTURE"),
    (1882, "21:21", True, 297, "DONE_TODAY"),
    (1883, "10:22", False, 314, "FUTURE"),
    (1884, "23:23", False, 331, "FUTURE"),
    (1885, "12:24", True, 348, "DONE_TODAY"),
    (1886, "01:25", False, 365, "DUE"),
    (1887, "14:26", False, 382, "FUTURE"),
    (1888, "03:27", True, 399, "DONE_TODAY"),
    (1889, "16:28", False, 416, "FUTURE"),
    (1890, "05:29", False, 433, "DUE"),
    (1891, "18:30", True, 450, "DONE_TODAY"),
    (1892, "07:31", False, 467, "DUE"),
    (1893, "20:32", False, 484, "FUTURE"),
    (1894, "09:33", True, 501, "DONE_TODAY"),
    (1895, "22:34", False, 518, "FUTURE"),
    (1896, "11:35", False, 535, "FUTURE"),
    (1897, "00:36", True, 552, "DONE_TODAY"),
    (1898, "13:37", False, 569, "FUTURE"),
    (1899, "02:38", False, 586, "DUE"),
    (1900, "15:39", True, 603, "DONE_TODAY"),
    (1901, "04:40", False, 620, "DUE"),
    (1902, "17:41", False, 637, "FUTURE"),
    (1903, "06:42", True, 654, "DONE_TODAY"),
    (1904, "19:43", False, 671, "FUTURE"),
    (1905, "08:44", False, 688, "DUE"),
    (1906, "21:45", True, 705, "DONE_TODAY"),
    (1907, "10:46", False, 722, "DUE"),
    (1908, "23:47", False, 739, "FUTURE"),
    (1909, "12:48", True, 756, "DONE_TODAY"),
    (1910, "01:49", False, 773, "DUE"),
    (1911, "14:50", False, 790, "FUTURE"),
    (1912, "03:51", True, 807, "DONE_TODAY"),
    (1913, "16:52", False, 824, "FUTURE"),
    (1914, "05:53", False, 841, "DUE"),
    (1915, "18:54", True, 858, "DONE_TODAY"),
    (1916, "07:55", False, 875, "DUE"),
    (1917, "20:56", False, 892, "FUTURE"),
    (1918, "09:57", True, 909, "DONE_TODAY"),
    (1919, "22:58", False, 926, "FUTURE"),
    (1920, "11:59", False, 943, "DUE"),
    (1921, "00:00", True, 960, "DONE_TODAY"),
    (1922, "13:01", False, 977, "DUE"),
    (1923, "02:02", False, 994, "DUE"),
    (1924, "15:03", True, 1011, "DONE_TODAY"),
    (1925, "04:04", False, 1028, "DUE"),
    (1926, "17:05", False, 1045, "DUE"),
    (1927, "06:06", True, 1062, "DONE_TODAY"),
    (1928, "19:07", False, 1079, "FUTURE"),
    (1929, "08:08", False, 1096, "DUE"),
    (1930, "21:09", True, 1113, "DONE_TODAY"),
    (1931, "10:10", False, 1130, "DUE"),
    (1932, "23:11", False, 1147, "FUTURE"),
    (1933, "12:12", True, 1164, "DONE_TODAY"),
    (1934, "01:13", False, 1181, "DUE"),
    (1935, "14:14", False, 1198, "DUE"),
    (1936, "03:15", True, 1215, "DONE_TODAY"),
    (1937, "16:16", False, 1232, "DUE"),
    (1938, "05:17", False, 1249, "DUE"),
    (1939, "18:18", True, 1266, "DONE_TODAY"),
    (1940, "07:19", False, 1283, "DUE"),
    (1941, "20:20", False, 1300, "DUE"),
    (1942, "09:21", True, 1317, "DONE_TODAY"),
    (1943, "22:22", False, 1334, "FUTURE"),
    (1944, "11:23", False, 1351, "DUE"),
    (1945, "00:24", True, 1368, "DONE_TODAY"),
    (1946, "13:25", False, 1385, "DUE"),
    (1947, "02:26", False, 1402, "DUE"),
    (1948, "15:27", True, 1419, "DONE_TODAY"),
    (1949, "04:28", False, 1436, "DUE"),
    (1950, "17:29", False, 13, "FUTURE"),
    (1951, "06:30", True, 30, "DONE_TODAY"),
    (1952, "19:31", False, 47, "FUTURE"),
    (1953, "08:32", False, 64, "FUTURE"),
    (1954, "21:33", True, 81, "DONE_TODAY"),
    (1955, "10:34", False, 98, "FUTURE"),
    (1956, "23:35", False, 115, "FUTURE"),
    (1957, "12:36", True, 132, "DONE_TODAY"),
    (1958, "01:37", False, 149, "DUE"),
    (1959, "14:38", False, 166, "FUTURE"),
    (1960, "03:39", True, 183, "DONE_TODAY"),
    (1961, "16:40", False, 200, "FUTURE"),
    (1962, "05:41", False, 217, "FUTURE"),
    (1963, "18:42", True, 234, "DONE_TODAY"),
    (1964, "07:43", False, 251, "FUTURE"),
    (1965, "20:44", False, 268, "FUTURE"),
    (1966, "09:45", True, 285, "DONE_TODAY"),
    (1967, "22:46", False, 302, "FUTURE"),
    (1968, "11:47", False, 319, "FUTURE"),
    (1969, "00:48", True, 336, "DONE_TODAY"),
    (1970, "13:49", False, 353, "FUTURE"),
    (1971, "02:50", False, 370, "DUE"),
    (1972, "15:51", True, 387, "DONE_TODAY"),
    (1973, "04:52", False, 404, "DUE"),
    (1974, "17:53", False, 421, "FUTURE"),
    (1975, "06:54", True, 438, "DONE_TODAY"),
    (1976, "19:55", False, 455, "FUTURE"),
    (1977, "08:56", False, 472, "FUTURE"),
    (1978, "21:57", True, 489, "DONE_TODAY"),
    (1979, "10:58", False, 506, "FUTURE"),
    (1980, "23:59", False, 523, "FUTURE"),
    (1981, "12:00", True, 540, "DONE_TODAY"),
    (1982, "01:01", False, 557, "DUE"),
    (1983, "14:02", False, 574, "FUTURE"),
    (1984, "03:03", True, 591, "DONE_TODAY"),
    (1985, "16:04", False, 608, "FUTURE"),
    (1986, "05:05", False, 625, "DUE"),
    (1987, "18:06", True, 642, "DONE_TODAY"),
    (1988, "07:07", False, 659, "DUE"),
    (1989, "20:08", False, 676, "FUTURE"),
    (1990, "09:09", True, 693, "DONE_TODAY"),
    (1991, "22:10", False, 710, "FUTURE"),
    (1992, "11:11", False, 727, "DUE"),
    (1993, "00:12", True, 744, "DONE_TODAY"),
    (1994, "13:13", False, 761, "FUTURE"),
    (1995, "02:14", False, 778, "DUE"),
    (1996, "15:15", True, 795, "DONE_TODAY"),
    (1997, "04:16", False, 812, "DUE"),
    (1998, "17:17", False, 829, "FUTURE"),
    (1999, "06:18", True, 846, "DONE_TODAY"),
    (2000, "19:19", False, 863, "FUTURE"),
    (2001, "08:20", False, 880, "DUE"),
    (2002, "21:21", True, 897, "DONE_TODAY"),
    (2003, "10:22", False, 914, "DUE"),
    (2004, "23:23", False, 931, "FUTURE"),
    (2005, "12:24", True, 948, "DONE_TODAY"),
    (2006, "01:25", False, 965, "DUE"),
    (2007, "14:26", False, 982, "DUE"),
    (2008, "03:27", True, 999, "DONE_TODAY"),
    (2009, "16:28", False, 1016, "DUE"),
    (2010, "05:29", False, 1033, "DUE"),
    (2011, "18:30", True, 1050, "DONE_TODAY"),
    (2012, "07:31", False, 1067, "DUE"),
    (2013, "20:32", False, 1084, "FUTURE"),
    (2014, "09:33", True, 1101, "DONE_TODAY"),
    (2015, "22:34", False, 1118, "FUTURE"),
    (2016, "11:35", False, 1135, "DUE"),
    (2017, "00:36", True, 1152, "DONE_TODAY"),
    (2018, "13:37", False, 1169, "DUE"),
    (2019, "02:38", False, 1186, "DUE"),
    (2020, "15:39", True, 1203, "DONE_TODAY"),
    (2021, "04:40", False, 1220, "DUE"),
    (2022, "17:41", False, 1237, "DUE"),
    (2023, "06:42", True, 1254, "DONE_TODAY"),
    (2024, "19:43", False, 1271, "DUE"),
    (2025, "08:44", False, 1288, "DUE"),
    (2026, "21:45", True, 1305, "DONE_TODAY"),
    (2027, "10:46", False, 1322, "DUE"),
    (2028, "23:47", False, 1339, "FUTURE"),
    (2029, "12:48", True, 1356, "DONE_TODAY"),
    (2030, "01:49", False, 1373, "DUE"),
    (2031, "14:50", False, 1390, "DUE"),
    (2032, "03:51", True, 1407, "DONE_TODAY"),
    (2033, "16:52", False, 1424, "DUE"),
    (2034, "05:53", False, 1, "FUTURE"),
    (2035, "18:54", True, 18, "DONE_TODAY"),
    (2036, "07:55", False, 35, "FUTURE"),
    (2037, "20:56", False, 52, "FUTURE"),
    (2038, "09:57", True, 69, "DONE_TODAY"),
    (2039, "22:58", False, 86, "FUTURE"),
    (2040, "11:59", False, 103, "FUTURE"),
    (2041, "00:00", True, 120, "DONE_TODAY"),
    (2042, "13:01", False, 137, "FUTURE"),
    (2043, "02:02", False, 154, "DUE"),
    (2044, "15:03", True, 171, "DONE_TODAY"),
    (2045, "04:04", False, 188, "FUTURE"),
    (2046, "17:05", False, 205, "FUTURE"),
    (2047, "06:06", True, 222, "DONE_TODAY"),
    (2048, "19:07", False, 239, "FUTURE"),
    (2049, "08:08", False, 256, "FUTURE"),
    (2050, "21:09", True, 273, "DONE_TODAY"),
    (2051, "10:10", False, 290, "FUTURE"),
    (2052, "23:11", False, 307, "FUTURE"),
    (2053, "12:12", True, 324, "DONE_TODAY"),
    (2054, "01:13", False, 341, "DUE"),
    (2055, "14:14", False, 358, "FUTURE"),
    (2056, "03:15", True, 375, "DONE_TODAY"),
    (2057, "16:16", False, 392, "FUTURE"),
    (2058, "05:17", False, 409, "DUE"),
    (2059, "18:18", True, 426, "DONE_TODAY"),
    (2060, "07:19", False, 443, "DUE"),
    (2061, "20:20", False, 460, "FUTURE"),
    (2062, "09:21", True, 477, "DONE_TODAY"),
    (2063, "22:22", False, 494, "FUTURE"),
    (2064, "11:23", False, 511, "FUTURE"),
    (2065, "00:24", True, 528, "DONE_TODAY"),
    (2066, "13:25", False, 545, "FUTURE"),
    (2067, "02:26", False, 562, "DUE"),
    (2068, "15:27", True, 579, "DONE_TODAY"),
    (2069, "04:28", False, 596, "DUE"),
    (2070, "17:29", False, 613, "FUTURE"),
    (2071, "06:30", True, 630, "DONE_TODAY"),
    (2072, "19:31", False, 647, "FUTURE"),
    (2073, "08:32", False, 664, "DUE"),
    (2074, "21:33", True, 681, "DONE_TODAY"),
    (2075, "10:34", False, 698, "DUE"),
    (2076, "23:35", False, 715, "FUTURE"),
    (2077, "12:36", True, 732, "DONE_TODAY"),
    (2078, "01:37", False, 749, "DUE"),
    (2079, "14:38", False, 766, "FUTURE"),
    (2080, "03:39", True, 783, "DONE_TODAY"),
    (2081, "16:40", False, 800, "FUTURE"),
    (2082, "05:41", False, 817, "DUE"),
    (2083, "18:42", True, 834, "DONE_TODAY"),
    (2084, "07:43", False, 851, "DUE"),
    (2085, "20:44", False, 868, "FUTURE"),
    (2086, "09:45", True, 885, "DONE_TODAY"),
    (2087, "22:46", False, 902, "FUTURE"),
    (2088, "11:47", False, 919, "DUE"),
    (2089, "00:48", True, 936, "DONE_TODAY"),
    (2090, "13:49", False, 953, "DUE"),
    (2091, "02:50", False, 970, "DUE"),
    (2092, "15:51", True, 987, "DONE_TODAY"),
    (2093, "04:52", False, 1004, "DUE"),
    (2094, "17:53", False, 1021, "FUTURE"),
    (2095, "06:54", True, 1038, "DONE_TODAY"),
    (2096, "19:55", False, 1055, "FUTURE"),
    (2097, "08:56", False, 1072, "DUE"),
    (2098, "21:57", True, 1089, "DONE_TODAY"),
    (2099, "10:58", False, 1106, "DUE"),
    (2100, "23:59", False, 1123, "FUTURE"),
    (2101, "12:00", True, 1140, "DONE_TODAY"),
    (2102, "01:01", False, 1157, "DUE"),
    (2103, "14:02", False, 1174, "DUE"),
    (2104, "03:03", True, 1191, "DONE_TODAY"),
    (2105, "16:04", False, 1208, "DUE"),
    (2106, "05:05", False, 1225, "DUE"),
    (2107, "18:06", True, 1242, "DONE_TODAY"),
    (2108, "07:07", False, 1259, "DUE"),
    (2109, "20:08", False, 1276, "DUE"),
    (2110, "09:09", True, 1293, "DONE_TODAY"),
    (2111, "22:10", False, 1310, "FUTURE"),
    (2112, "11:11", False, 1327, "DUE"),
    (2113, "00:12", True, 1344, "DONE_TODAY"),
    (2114, "13:13", False, 1361, "DUE"),
    (2115, "02:14", False, 1378, "DUE"),
    (2116, "15:15", True, 1395, "DONE_TODAY"),
    (2117, "04:16", False, 1412, "DUE"),
    (2118, "17:17", False, 1429, "DUE"),
    (2119, "06:18", True, 6, "DONE_TODAY"),
    (2120, "19:19", False, 23, "FUTURE"),
    (2121, "08:20", False, 40, "FUTURE"),
    (2122, "21:21", True, 57, "DONE_TODAY"),
    (2123, "10:22", False, 74, "FUTURE"),
    (2124, "23:23", False, 91, "FUTURE"),
    (2125, "12:24", True, 108, "DONE_TODAY"),
    (2126, "01:25", False, 125, "DUE"),
    (2127, "14:26", False, 142, "FUTURE"),
    (2128, "03:27", True, 159, "DONE_TODAY"),
    (2129, "16:28", False, 176, "FUTURE"),
    (2130, "05:29", False, 193, "FUTURE"),
    (2131, "18:30", True, 210, "DONE_TODAY"),
    (2132, "07:31", False, 227, "FUTURE"),
    (2133, "20:32", False, 244, "FUTURE"),
    (2134, "09:33", True, 261, "DONE_TODAY"),
    (2135, "22:34", False, 278, "FUTURE"),
    (2136, "11:35", False, 295, "FUTURE"),
    (2137, "00:36", True, 312, "DONE_TODAY"),
    (2138, "13:37", False, 329, "FUTURE"),
    (2139, "02:38", False, 346, "DUE"),
    (2140, "15:39", True, 363, "DONE_TODAY"),
    (2141, "04:40", False, 380, "DUE"),
    (2142, "17:41", False, 397, "FUTURE"),
    (2143, "06:42", True, 414, "DONE_TODAY"),
    (2144, "19:43", False, 431, "FUTURE"),
    (2145, "08:44", False, 448, "FUTURE"),
    (2146, "21:45", True, 465, "DONE_TODAY"),
    (2147, "10:46", False, 482, "FUTURE"),
    (2148, "23:47", False, 499, "FUTURE"),
    (2149, "12:48", True, 516, "DONE_TODAY"),
    (2150, "01:49", False, 533, "DUE"),
    (2151, "14:50", False, 550, "FUTURE"),
    (2152, "03:51", True, 567, "DONE_TODAY"),
    (2153, "16:52", False, 584, "FUTURE"),
    (2154, "05:53", False, 601, "DUE"),
    (2155, "18:54", True, 618, "DONE_TODAY"),
    (2156, "07:55", False, 635, "DUE"),
    (2157, "20:56", False, 652, "FUTURE"),
    (2158, "09:57", True, 669, "DONE_TODAY"),
    (2159, "22:58", False, 686, "FUTURE"),
    (2160, "11:59", False, 703, "FUTURE"),
    (2161, "00:00", True, 720, "DONE_TODAY"),
    (2162, "13:01", False, 737, "FUTURE"),
    (2163, "02:02", False, 754, "DUE"),
    (2164, "15:03", True, 771, "DONE_TODAY"),
    (2165, "04:04", False, 788, "DUE"),
    (2166, "17:05", False, 805, "FUTURE"),
    (2167, "06:06", True, 822, "DONE_TODAY"),
    (2168, "19:07", False, 839, "FUTURE"),
    (2169, "08:08", False, 856, "DUE"),
    (2170, "21:09", True, 873, "DONE_TODAY"),
    (2171, "10:10", False, 890, "DUE"),
    (2172, "23:11", False, 907, "FUTURE"),
    (2173, "12:12", True, 924, "DONE_TODAY"),
    (2174, "01:13", False, 941, "DUE"),
    (2175, "14:14", False, 958, "DUE"),
    (2176, "03:15", True, 975, "DONE_TODAY"),
    (2177, "16:16", False, 992, "DUE"),
    (2178, "05:17", False, 1009, "DUE"),
    (2179, "18:18", True, 1026, "DONE_TODAY"),
    (2180, "07:19", False, 1043, "DUE"),
    (2181, "20:20", False, 1060, "FUTURE"),
    (2182, "09:21", True, 1077, "DONE_TODAY"),
    (2183, "22:22", False, 1094, "FUTURE"),
    (2184, "11:23", False, 1111, "DUE"),
    (2185, "00:24", True, 1128, "DONE_TODAY"),
    (2186, "13:25", False, 1145, "DUE"),
    (2187, "02:26", False, 1162, "DUE"),
    (2188, "15:27", True, 1179, "DONE_TODAY"),
    (2189, "04:28", False, 1196, "DUE"),
    (2190, "17:29", False, 1213, "DUE"),
    (2191, "06:30", True, 1230, "DONE_TODAY"),
    (2192, "19:31", False, 1247, "DUE"),
    (2193, "08:32", False, 1264, "DUE"),
    (2194, "21:33", True, 1281, "DONE_TODAY"),
    (2195, "10:34", False, 1298, "DUE"),
    (2196, "23:35", False, 1315, "FUTURE"),
    (2197, "12:36", True, 1332, "DONE_TODAY"),
    (2198, "01:37", False, 1349, "DUE"),
    (2199, "14:38", False, 1366, "DUE"),
    (2200, "03:39", True, 1383, "DONE_TODAY"),
    (2201, "16:40", False, 1400, "DUE"),
    (2202, "05:41", False, 1417, "DUE"),
    (2203, "18:42", True, 1434, "DONE_TODAY"),
    (2204, "07:43", False, 11, "FUTURE"),
    (2205, "20:44", False, 28, "FUTURE"),
    (2206, "09:45", True, 45, "DONE_TODAY"),
    (2207, "22:46", False, 62, "FUTURE"),
    (2208, "11:47", False, 79, "FUTURE"),
    (2209, "00:48", True, 96, "DONE_TODAY"),
    (2210, "13:49", False, 113, "FUTURE"),
    (2211, "02:50", False, 130, "FUTURE"),
    (2212, "15:51", True, 147, "DONE_TODAY"),
    (2213, "04:52", False, 164, "FUTURE"),
    (2214, "17:53", False, 181, "FUTURE"),
    (2215, "06:54", True, 198, "DONE_TODAY"),
    (2216, "19:55", False, 215, "FUTURE"),
    (2217, "08:56", False, 232, "FUTURE"),
    (2218, "21:57", True, 249, "DONE_TODAY"),
    (2219, "10:58", False, 266, "FUTURE"),
    (2220, "23:59", False, 283, "FUTURE"),
    (2221, "12:00", True, 300, "DONE_TODAY"),
    (2222, "01:01", False, 317, "DUE"),
    (2223, "14:02", False, 334, "FUTURE"),
    (2224, "03:03", True, 351, "DONE_TODAY"),
    (2225, "16:04", False, 368, "FUTURE"),
    (2226, "05:05", False, 385, "DUE"),
    (2227, "18:06", True, 402, "DONE_TODAY"),
    (2228, "07:07", False, 419, "FUTURE"),
    (2229, "20:08", False, 436, "FUTURE"),
    (2230, "09:09", True, 453, "DONE_TODAY"),
    (2231, "22:10", False, 470, "FUTURE"),
    (2232, "11:11", False, 487, "FUTURE"),
    (2233, "00:12", True, 504, "DONE_TODAY"),
    (2234, "13:13", False, 521, "FUTURE"),
    (2235, "02:14", False, 538, "DUE"),
    (2236, "15:15", True, 555, "DONE_TODAY"),
    (2237, "04:16", False, 572, "DUE"),
    (2238, "17:17", False, 589, "FUTURE"),
    (2239, "06:18", True, 606, "DONE_TODAY"),
    (2240, "19:19", False, 623, "FUTURE"),
    (2241, "08:20", False, 640, "DUE"),
    (2242, "21:21", True, 657, "DONE_TODAY"),
    (2243, "10:22", False, 674, "DUE"),
    (2244, "23:23", False, 691, "FUTURE"),
    (2245, "12:24", True, 708, "DONE_TODAY"),
    (2246, "01:25", False, 725, "DUE"),
    (2247, "14:26", False, 742, "FUTURE"),
    (2248, "03:27", True, 759, "DONE_TODAY"),
    (2249, "16:28", False, 776, "FUTURE"),
    (2250, "05:29", False, 793, "DUE"),
    (2251, "18:30", True, 810, "DONE_TODAY"),
    (2252, "07:31", False, 827, "DUE"),
    (2253, "20:32", False, 844, "FUTURE"),
    (2254, "09:33", True, 861, "DONE_TODAY"),
    (2255, "22:34", False, 878, "FUTURE"),
    (2256, "11:35", False, 895, "DUE"),
    (2257, "00:36", True, 912, "DONE_TODAY"),
    (2258, "13:37", False, 929, "DUE"),
    (2259, "02:38", False, 946, "DUE"),
    (2260, "15:39", True, 963, "DONE_TODAY"),
    (2261, "04:40", False, 980, "DUE"),
    (2262, "17:41", False, 997, "FUTURE"),
    (2263, "06:42", True, 1014, "DONE_TODAY"),
    (2264, "19:43", False, 1031, "FUTURE"),
    (2265, "08:44", False, 1048, "DUE"),
    (2266, "21:45", True, 1065, "DONE_TODAY"),
    (2267, "10:46", False, 1082, "DUE"),
    (2268, "23:47", False, 1099, "FUTURE"),
    (2269, "12:48", True, 1116, "DONE_TODAY"),
    (2270, "01:49", False, 1133, "DUE"),
    (2271, "14:50", False, 1150, "DUE"),
    (2272, "03:51", True, 1167, "DONE_TODAY"),
    (2273, "16:52", False, 1184, "DUE"),
    (2274, "05:53", False, 1201, "DUE"),
    (2275, "18:54", True, 1218, "DONE_TODAY"),
    (2276, "07:55", False, 1235, "DUE"),
    (2277, "20:56", False, 1252, "FUTURE"),
    (2278, "09:57", True, 1269, "DONE_TODAY"),
    (2279, "22:58", False, 1286, "FUTURE"),
    (2280, "11:59", False, 1303, "DUE"),
    (2281, "00:00", True, 1320, "DONE_TODAY"),
    (2282, "13:01", False, 1337, "DUE"),
    (2283, "02:02", False, 1354, "DUE"),
    (2284, "15:03", True, 1371, "DONE_TODAY"),
    (2285, "04:04", False, 1388, "DUE"),
    (2286, "17:05", False, 1405, "DUE"),
    (2287, "06:06", True, 1422, "DONE_TODAY"),
    (2288, "19:07", False, 1439, "DUE"),
    (2289, "08:08", False, 16, "FUTURE"),
    (2290, "21:09", True, 33, "DONE_TODAY"),
    (2291, "10:10", False, 50, "FUTURE"),
    (2292, "23:11", False, 67, "FUTURE"),
    (2293, "12:12", True, 84, "DONE_TODAY"),
    (2294, "01:13", False, 101, "DUE"),
    (2295, "14:14", False, 118, "FUTURE"),
    (2296, "03:15", True, 135, "DONE_TODAY"),
    (2297, "16:16", False, 152, "FUTURE"),
    (2298, "05:17", False, 169, "FUTURE"),
    (2299, "18:18", True, 186, "DONE_TODAY"),
    (2300, "07:19", False, 203, "FUTURE"),
    (2301, "20:20", False, 220, "FUTURE"),
    (2302, "09:21", True, 237, "DONE_TODAY"),
    (2303, "22:22", False, 254, "FUTURE"),
    (2304, "11:23", False, 271, "FUTURE"),
    (2305, "00:24", True, 288, "DONE_TODAY"),
    (2306, "13:25", False, 305, "FUTURE"),
    (2307, "02:26", False, 322, "DUE"),
    (2308, "15:27", True, 339, "DONE_TODAY"),
    (2309, "04:28", False, 356, "DUE"),
    (2310, "17:29", False, 373, "FUTURE"),
    (2311, "06:30", True, 390, "DONE_TODAY"),
    (2312, "19:31", False, 407, "FUTURE"),
    (2313, "08:32", False, 424, "FUTURE"),
    (2314, "21:33", True, 441, "DONE_TODAY"),
    (2315, "10:34", False, 458, "FUTURE"),
    (2316, "23:35", False, 475, "FUTURE"),
    (2317, "12:36", True, 492, "DONE_TODAY"),
    (2318, "01:37", False, 509, "DUE"),
    (2319, "14:38", False, 526, "FUTURE"),
    (2320, "03:39", True, 543, "DONE_TODAY"),
    (2321, "16:40", False, 560, "FUTURE"),
    (2322, "05:41", False, 577, "DUE"),
    (2323, "18:42", True, 594, "DONE_TODAY"),
    (2324, "07:43", False, 611, "DUE"),
    (2325, "20:44", False, 628, "FUTURE"),
    (2326, "09:45", True, 645, "DONE_TODAY"),
    (2327, "22:46", False, 662, "FUTURE"),
    (2328, "11:47", False, 679, "FUTURE"),
    (2329, "00:48", True, 696, "DONE_TODAY"),
    (2330, "13:49", False, 713, "FUTURE"),
    (2331, "02:50", False, 730, "DUE"),
    (2332, "15:51", True, 747, "DONE_TODAY"),
    (2333, "04:52", False, 764, "DUE"),
    (2334, "17:53", False, 781, "FUTURE"),
    (2335, "06:54", True, 798, "DONE_TODAY"),
    (2336, "19:55", False, 815, "FUTURE"),
    (2337, "08:56", False, 832, "DUE"),
    (2338, "21:57", True, 849, "DONE_TODAY"),
    (2339, "10:58", False, 866, "DUE"),
    (2340, "23:59", False, 883, "FUTURE"),
    (2341, "12:00", True, 900, "DONE_TODAY"),
    (2342, "01:01", False, 917, "DUE"),
    (2343, "14:02", False, 934, "DUE"),
    (2344, "03:03", True, 951, "DONE_TODAY"),
    (2345, "16:04", False, 968, "DUE"),
    (2346, "05:05", False, 985, "DUE"),
    (2347, "18:06", True, 1002, "DONE_TODAY"),
    (2348, "07:07", False, 1019, "DUE"),
    (2349, "20:08", False, 1036, "FUTURE"),
    (2350, "09:09", True, 1053, "DONE_TODAY"),
    (2351, "22:10", False, 1070, "FUTURE"),
    (2352, "11:11", False, 1087, "DUE"),
    (2353, "00:12", True, 1104, "DONE_TODAY"),
    (2354, "13:13", False, 1121, "DUE"),
    (2355, "02:14", False, 1138, "DUE"),
    (2356, "15:15", True, 1155, "DONE_TODAY"),
    (2357, "04:16", False, 1172, "DUE"),
    (2358, "17:17", False, 1189, "DUE"),
    (2359, "06:18", True, 1206, "DONE_TODAY"),
    (2360, "19:19", False, 1223, "DUE"),
    (2361, "08:20", False, 1240, "DUE"),
    (2362, "21:21", True, 1257, "DONE_TODAY"),
    (2363, "10:22", False, 1274, "DUE"),
    (2364, "23:23", False, 1291, "FUTURE"),
    (2365, "12:24", True, 1308, "DONE_TODAY"),
    (2366, "01:25", False, 1325, "DUE"),
    (2367, "14:26", False, 1342, "DUE"),
    (2368, "03:27", True, 1359, "DONE_TODAY"),
    (2369, "16:28", False, 1376, "DUE"),
    (2370, "05:29", False, 1393, "DUE"),
    (2371, "18:30", True, 1410, "DONE_TODAY"),
    (2372, "07:31", False, 1427, "DUE"),
    (2373, "20:32", False, 4, "FUTURE"),
    (2374, "09:33", True, 21, "DONE_TODAY"),
    (2375, "22:34", False, 38, "FUTURE"),
    (2376, "11:35", False, 55, "FUTURE"),
    (2377, "00:36", True, 72, "DONE_TODAY"),
    (2378, "13:37", False, 89, "FUTURE"),
    (2379, "02:38", False, 106, "FUTURE"),
    (2380, "15:39", True, 123, "DONE_TODAY"),
    (2381, "04:40", False, 140, "FUTURE"),
    (2382, "17:41", False, 157, "FUTURE"),
    (2383, "06:42", True, 174, "DONE_TODAY"),
    (2384, "19:43", False, 191, "FUTURE"),
    (2385, "08:44", False, 208, "FUTURE"),
    (2386, "21:45", True, 225, "DONE_TODAY"),
    (2387, "10:46", False, 242, "FUTURE"),
    (2388, "23:47", False, 259, "FUTURE"),
    (2389, "12:48", True, 276, "DONE_TODAY"),
    (2390, "01:49", False, 293, "DUE"),
    (2391, "14:50", False, 310, "FUTURE"),
    (2392, "03:51", True, 327, "DONE_TODAY"),
    (2393, "16:52", False, 344, "FUTURE"),
    (2394, "05:53", False, 361, "DUE"),
    (2395, "18:54", True, 378, "DONE_TODAY"),
    (2396, "07:55", False, 395, "FUTURE"),
    (2397, "20:56", False, 412, "FUTURE"),
    (2398, "09:57", True, 429, "DONE_TODAY"),
    (2399, "22:58", False, 446, "FUTURE"),
    (2400, "11:59", False, 463, "FUTURE"),
    (2401, "00:00", True, 480, "DONE_TODAY"),
    (2402, "13:01", False, 497, "FUTURE"),
    (2403, "02:02", False, 514, "DUE"),
    (2404, "15:03", True, 531, "DONE_TODAY"),
    (2405, "04:04", False, 548, "DUE"),
    (2406, "17:05", False, 565, "FUTURE"),
    (2407, "06:06", True, 582, "DONE_TODAY"),
    (2408, "19:07", False, 599, "FUTURE"),
    (2409, "08:08", False, 616, "DUE"),
    (2410, "21:09", True, 633, "DONE_TODAY"),
    (2411, "10:10", False, 650, "DUE"),
    (2412, "23:11", False, 667, "FUTURE"),
    (2413, "12:12", True, 684, "DONE_TODAY"),
    (2414, "01:13", False, 701, "DUE"),
    (2415, "14:14", False, 718, "FUTURE"),
    (2416, "03:15", True, 735, "DONE_TODAY"),
    (2417, "16:16", False, 752, "FUTURE"),
    (2418, "05:17", False, 769, "DUE"),
    (2419, "18:18", True, 786, "DONE_TODAY"),
    (2420, "07:19", False, 803, "DUE"),
    (2421, "20:20", False, 820, "FUTURE"),
    (2422, "09:21", True, 837, "DONE_TODAY"),
    (2423, "22:22", False, 854, "FUTURE"),
    (2424, "11:23", False, 871, "DUE"),
    (2425, "00:24", True, 888, "DONE_TODAY"),
    (2426, "13:25", False, 905, "DUE"),
    (2427, "02:26", False, 922, "DUE"),
    (2428, "15:27", True, 939, "DONE_TODAY"),
    (2429, "04:28", False, 956, "DUE"),
    (2430, "17:29", False, 973, "FUTURE"),
    (2431, "06:30", True, 990, "DONE_TODAY"),
    (2432, "19:31", False, 1007, "FUTURE"),
    (2433, "08:32", False, 1024, "DUE"),
    (2434, "21:33", True, 1041, "DONE_TODAY"),
    (2435, "10:34", False, 1058, "DUE"),
    (2436, "23:35", False, 1075, "FUTURE"),
    (2437, "12:36", True, 1092, "DONE_TODAY"),
    (2438, "01:37", False, 1109, "DUE"),
    (2439, "14:38", False, 1126, "DUE"),
    (2440, "03:39", True, 1143, "DONE_TODAY"),
    (2441, "16:40", False, 1160, "DUE"),
    (2442, "05:41", False, 1177, "DUE"),
    (2443, "18:42", True, 1194, "DONE_TODAY"),
    (2444, "07:43", False, 1211, "DUE"),
    (2445, "20:44", False, 1228, "FUTURE"),
    (2446, "09:45", True, 1245, "DONE_TODAY"),
    (2447, "22:46", False, 1262, "FUTURE"),
    (2448, "11:47", False, 1279, "DUE"),
    (2449, "00:48", True, 1296, "DONE_TODAY"),
    (2450, "13:49", False, 1313, "DUE"),
    (2451, "02:50", False, 1330, "DUE"),
    (2452, "15:51", True, 1347, "DONE_TODAY"),
    (2453, "04:52", False, 1364, "DUE"),
    (2454, "17:53", False, 1381, "DUE"),
    (2455, "06:54", True, 1398, "DONE_TODAY"),
    (2456, "19:55", False, 1415, "DUE"),
    (2457, "08:56", False, 1432, "DUE"),
    (2458, "21:57", True, 9, "DONE_TODAY"),
    (2459, "10:58", False, 26, "FUTURE"),
    (2460, "23:59", False, 43, "FUTURE"),
    (2461, "12:00", True, 60, "DONE_TODAY"),
    (2462, "01:01", False, 77, "DUE"),
    (2463, "14:02", False, 94, "FUTURE"),
    (2464, "03:03", True, 111, "DONE_TODAY"),
    (2465, "16:04", False, 128, "FUTURE"),
    (2466, "05:05", False, 145, "FUTURE"),
    (2467, "18:06", True, 162, "DONE_TODAY"),
    (2468, "07:07", False, 179, "FUTURE"),
    (2469, "20:08", False, 196, "FUTURE"),
    (2470, "09:09", True, 213, "DONE_TODAY"),
    (2471, "22:10", False, 230, "FUTURE"),
    (2472, "11:11", False, 247, "FUTURE"),
    (2473, "00:12", True, 264, "DONE_TODAY"),
    (2474, "13:13", False, 281, "FUTURE"),
    (2475, "02:14", False, 298, "DUE"),
    (2476, "15:15", True, 315, "DONE_TODAY"),
    (2477, "04:16", False, 332, "DUE"),
    (2478, "17:17", False, 349, "FUTURE"),
    (2479, "06:18", True, 366, "DONE_TODAY"),
    (2480, "19:19", False, 383, "FUTURE"),
    (2481, "08:20", False, 400, "FUTURE"),
    (2482, "21:21", True, 417, "DONE_TODAY"),
    (2483, "10:22", False, 434, "FUTURE"),
    (2484, "23:23", False, 451, "FUTURE"),
    (2485, "12:24", True, 468, "DONE_TODAY"),
    (2486, "01:25", False, 485, "DUE"),
    (2487, "14:26", False, 502, "FUTURE"),
    (2488, "03:27", True, 519, "DONE_TODAY"),
    (2489, "16:28", False, 536, "FUTURE"),
    (2490, "05:29", False, 553, "DUE"),
    (2491, "18:30", True, 570, "DONE_TODAY"),
    (2492, "07:31", False, 587, "DUE"),
    (2493, "20:32", False, 604, "FUTURE"),
    (2494, "09:33", True, 621, "DONE_TODAY"),
    (2495, "22:34", False, 638, "FUTURE"),
    (2496, "11:35", False, 655, "FUTURE"),
    (2497, "00:36", True, 672, "DONE_TODAY"),
    (2498, "13:37", False, 689, "FUTURE"),
    (2499, "02:38", False, 706, "DUE"),
    (2500, "15:39", True, 723, "DONE_TODAY"),
    (2501, "04:40", False, 740, "DUE"),
    (2502, "17:41", False, 757, "FUTURE"),
    (2503, "06:42", True, 774, "DONE_TODAY"),
    (2504, "19:43", False, 791, "FUTURE"),
    (2505, "08:44", False, 808, "DUE"),
    (2506, "21:45", True, 825, "DONE_TODAY"),
    (2507, "10:46", False, 842, "DUE"),
    (2508, "23:47", False, 859, "FUTURE"),
    (2509, "12:48", True, 876, "DONE_TODAY"),
    (2510, "01:49", False, 893, "DUE"),
    (2511, "14:50", False, 910, "DUE"),
    (2512, "03:51", True, 927, "DONE_TODAY"),
    (2513, "16:52", False, 944, "FUTURE"),
    (2514, "05:53", False, 961, "DUE"),
    (2515, "18:54", True, 978, "DONE_TODAY"),
    (2516, "07:55", False, 995, "DUE"),
    (2517, "20:56", False, 1012, "FUTURE"),
    (2518, "09:57", True, 1029, "DONE_TODAY"),
    (2519, "22:58", False, 1046, "FUTURE"),
    (2520, "11:59", False, 1063, "DUE"),
    (2521, "00:00", True, 1080, "DONE_TODAY"),
    (2522, "13:01", False, 1097, "DUE"),
    (2523, "02:02", False, 1114, "DUE"),
    (2524, "15:03", True, 1131, "DONE_TODAY"),
    (2525, "04:04", False, 1148, "DUE"),
    (2526, "17:05", False, 1165, "DUE"),
    (2527, "06:06", True, 1182, "DONE_TODAY"),
    (2528, "19:07", False, 1199, "DUE"),
    (2529, "08:08", False, 1216, "DUE"),
    (2530, "21:09", True, 1233, "DONE_TODAY"),
    (2531, "10:10", False, 1250, "DUE"),
    (2532, "23:11", False, 1267, "FUTURE"),
    (2533, "12:12", True, 1284, "DONE_TODAY"),
    (2534, "01:13", False, 1301, "DUE"),
    (2535, "14:14", False, 1318, "DUE"),
    (2536, "03:15", True, 1335, "DONE_TODAY"),
    (2537, "16:16", False, 1352, "DUE"),
    (2538, "05:17", False, 1369, "DUE"),
    (2539, "18:18", True, 1386, "DONE_TODAY"),
    (2540, "07:19", False, 1403, "DUE"),
    (2541, "20:20", False, 1420, "DUE"),
    (2542, "09:21", True, 1437, "DONE_TODAY"),
    (2543, "22:22", False, 14, "FUTURE"),
    (2544, "11:23", False, 31, "FUTURE"),
    (2545, "00:24", True, 48, "DONE_TODAY"),
    (2546, "13:25", False, 65, "FUTURE"),
    (2547, "02:26", False, 82, "FUTURE"),
    (2548, "15:27", True, 99, "DONE_TODAY"),
    (2549, "04:28", False, 116, "FUTURE"),
    (2550, "17:29", False, 133, "FUTURE"),
    (2551, "06:30", True, 150, "DONE_TODAY"),
    (2552, "19:31", False, 167, "FUTURE"),
    (2553, "08:32", False, 184, "FUTURE"),
    (2554, "21:33", True, 201, "DONE_TODAY"),
    (2555, "10:34", False, 218, "FUTURE"),
    (2556, "23:35", False, 235, "FUTURE"),
    (2557, "12:36", True, 252, "DONE_TODAY"),
    (2558, "01:37", False, 269, "DUE"),
    (2559, "14:38", False, 286, "FUTURE"),
    (2560, "03:39", True, 303, "DONE_TODAY"),
    (2561, "16:40", False, 320, "FUTURE"),
    (2562, "05:41", False, 337, "FUTURE"),
    (2563, "18:42", True, 354, "DONE_TODAY"),
    (2564, "07:43", False, 371, "FUTURE"),
    (2565, "20:44", False, 388, "FUTURE"),
    (2566, "09:45", True, 405, "DONE_TODAY"),
    (2567, "22:46", False, 422, "FUTURE"),
    (2568, "11:47", False, 439, "FUTURE"),
    (2569, "00:48", True, 456, "DONE_TODAY"),
    (2570, "13:49", False, 473, "FUTURE"),
    (2571, "02:50", False, 490, "DUE"),
    (2572, "15:51", True, 507, "DONE_TODAY"),
    (2573, "04:52", False, 524, "DUE"),
    (2574, "17:53", False, 541, "FUTURE"),
    (2575, "06:54", True, 558, "DONE_TODAY"),
    (2576, "19:55", False, 575, "FUTURE"),
    (2577, "08:56", False, 592, "DUE"),
    (2578, "21:57", True, 609, "DONE_TODAY"),
    (2579, "10:58", False, 626, "FUTURE"),
    (2580, "23:59", False, 643, "FUTURE"),
    (2581, "12:00", True, 660, "DONE_TODAY"),
    (2582, "01:01", False, 677, "DUE"),
    (2583, "14:02", False, 694, "FUTURE"),
    (2584, "03:03", True, 711, "DONE_TODAY"),
    (2585, "16:04", False, 728, "FUTURE"),
    (2586, "05:05", False, 745, "DUE"),
    (2587, "18:06", True, 762, "DONE_TODAY"),
    (2588, "07:07", False, 779, "DUE"),
    (2589, "20:08", False, 796, "FUTURE"),
    (2590, "09:09", True, 813, "DONE_TODAY"),
    (2591, "22:10", False, 830, "FUTURE"),
    (2592, "11:11", False, 847, "DUE"),
    (2593, "00:12", True, 864, "DONE_TODAY"),
    (2594, "13:13", False, 881, "DUE"),
    (2595, "02:14", False, 898, "DUE"),
    (2596, "15:15", True, 915, "DONE_TODAY"),
    (2597, "04:16", False, 932, "DUE"),
    (2598, "17:17", False, 949, "FUTURE"),
    (2599, "06:18", True, 966, "DONE_TODAY"),
    (2600, "19:19", False, 983, "FUTURE"),
    (2601, "08:20", False, 1000, "DUE"),
    (2602, "21:21", True, 1017, "DONE_TODAY"),
    (2603, "10:22", False, 1034, "DUE"),
    (2604, "23:23", False, 1051, "FUTURE"),
    (2605, "12:24", True, 1068, "DONE_TODAY"),
    (2606, "01:25", False, 1085, "DUE"),
    (2607, "14:26", False, 1102, "DUE"),
    (2608, "03:27", True, 1119, "DONE_TODAY"),
    (2609, "16:28", False, 1136, "DUE"),
    (2610, "05:29", False, 1153, "DUE"),
    (2611, "18:30", True, 1170, "DONE_TODAY"),
    (2612, "07:31", False, 1187, "DUE"),
    (2613, "20:32", False, 1204, "FUTURE"),
    (2614, "09:33", True, 1221, "DONE_TODAY"),
    (2615, "22:34", False, 1238, "FUTURE"),
    (2616, "11:35", False, 1255, "DUE"),
    (2617, "00:36", True, 1272, "DONE_TODAY"),
    (2618, "13:37", False, 1289, "DUE"),
    (2619, "02:38", False, 1306, "DUE"),
    (2620, "15:39", True, 1323, "DONE_TODAY"),
    (2621, "04:40", False, 1340, "DUE"),
    (2622, "17:41", False, 1357, "DUE"),
    (2623, "06:42", True, 1374, "DONE_TODAY"),
    (2624, "19:43", False, 1391, "DUE"),
    (2625, "08:44", False, 1408, "DUE"),
    (2626, "21:45", True, 1425, "DONE_TODAY"),
    (2627, "10:46", False, 2, "FUTURE"),
    (2628, "23:47", False, 19, "FUTURE"),
    (2629, "12:48", True, 36, "DONE_TODAY"),
    (2630, "01:49", False, 53, "FUTURE"),
    (2631, "14:50", False, 70, "FUTURE"),
    (2632, "03:51", True, 87, "DONE_TODAY"),
    (2633, "16:52", False, 104, "FUTURE"),
    (2634, "05:53", False, 121, "FUTURE"),
    (2635, "18:54", True, 138, "DONE_TODAY"),
    (2636, "07:55", False, 155, "FUTURE"),
    (2637, "20:56", False, 172, "FUTURE"),
    (2638, "09:57", True, 189, "DONE_TODAY"),
    (2639, "22:58", False, 206, "FUTURE"),
    (2640, "11:59", False, 223, "FUTURE"),
    (2641, "00:00", True, 240, "DONE_TODAY"),
    (2642, "13:01", False, 257, "FUTURE"),
    (2643, "02:02", False, 274, "DUE"),
    (2644, "15:03", True, 291, "DONE_TODAY"),
    (2645, "04:04", False, 308, "DUE"),
    (2646, "17:05", False, 325, "FUTURE"),
    (2647, "06:06", True, 342, "DONE_TODAY"),
    (2648, "19:07", False, 359, "FUTURE"),
    (2649, "08:08", False, 376, "FUTURE"),
    (2650, "21:09", True, 393, "DONE_TODAY"),
    (2651, "10:10", False, 410, "FUTURE"),
    (2652, "23:11", False, 427, "FUTURE"),
    (2653, "12:12", True, 444, "DONE_TODAY"),
    (2654, "01:13", False, 461, "DUE"),
    (2655, "14:14", False, 478, "FUTURE"),
    (2656, "03:15", True, 495, "DONE_TODAY"),
    (2657, "16:16", False, 512, "FUTURE"),
    (2658, "05:17", False, 529, "DUE"),
    (2659, "18:18", True, 546, "DONE_TODAY"),
    (2660, "07:19", False, 563, "DUE"),
    (2661, "20:20", False, 580, "FUTURE"),
    (2662, "09:21", True, 597, "DONE_TODAY"),
    (2663, "22:22", False, 614, "FUTURE"),
    (2664, "11:23", False, 631, "FUTURE"),
    (2665, "00:24", True, 648, "DONE_TODAY"),
    (2666, "13:25", False, 665, "FUTURE"),
    (2667, "02:26", False, 682, "DUE"),
    (2668, "15:27", True, 699, "DONE_TODAY"),
    (2669, "04:28", False, 716, "DUE"),
    (2670, "17:29", False, 733, "FUTURE"),
    (2671, "06:30", True, 750, "DONE_TODAY"),
    (2672, "19:31", False, 767, "FUTURE"),
    (2673, "08:32", False, 784, "DUE"),
    (2674, "21:33", True, 801, "DONE_TODAY"),
    (2675, "10:34", False, 818, "DUE"),
    (2676, "23:35", False, 835, "FUTURE"),
    (2677, "12:36", True, 852, "DONE_TODAY"),
    (2678, "01:37", False, 869, "DUE"),
    (2679, "14:38", False, 886, "DUE"),
    (2680, "03:39", True, 903, "DONE_TODAY"),
    (2681, "16:40", False, 920, "FUTURE"),
    (2682, "05:41", False, 937, "DUE"),
    (2683, "18:42", True, 954, "DONE_TODAY"),
    (2684, "07:43", False, 971, "DUE"),
    (2685, "20:44", False, 988, "FUTURE"),
    (2686, "09:45", True, 1005, "DONE_TODAY"),
    (2687, "22:46", False, 1022, "FUTURE"),
    (2688, "11:47", False, 1039, "DUE"),
    (2689, "00:48", True, 1056, "DONE_TODAY"),
    (2690, "13:49", False, 1073, "DUE"),
    (2691, "02:50", False, 1090, "DUE"),
    (2692, "15:51", True, 1107, "DONE_TODAY"),
    (2693, "04:52", False, 1124, "DUE"),
    (2694, "17:53", False, 1141, "DUE"),
    (2695, "06:54", True, 1158, "DONE_TODAY"),
    (2696, "19:55", False, 1175, "FUTURE"),
    (2697, "08:56", False, 1192, "DUE"),
    (2698, "21:57", True, 1209, "DONE_TODAY"),
    (2699, "10:58", False, 1226, "DUE"),
    (2700, "23:59", False, 1243, "FUTURE"),
    (2701, "12:00", True, 1260, "DONE_TODAY"),
    (2702, "01:01", False, 1277, "DUE"),
    (2703, "14:02", False, 1294, "DUE"),
    (2704, "03:03", True, 1311, "DONE_TODAY"),
    (2705, "16:04", False, 1328, "DUE"),
    (2706, "05:05", False, 1345, "DUE"),
    (2707, "18:06", True, 1362, "DONE_TODAY"),
    (2708, "07:07", False, 1379, "DUE"),
    (2709, "20:08", False, 1396, "DUE"),
    (2710, "09:09", True, 1413, "DONE_TODAY"),
    (2711, "22:10", False, 1430, "DUE"),
    (2712, "11:11", False, 7, "FUTURE"),
    (2713, "00:12", True, 24, "DONE_TODAY"),
    (2714, "13:13", False, 41, "FUTURE"),
    (2715, "02:14", False, 58, "FUTURE"),
    (2716, "15:15", True, 75, "DONE_TODAY"),
    (2717, "04:16", False, 92, "FUTURE"),
    (2718, "17:17", False, 109, "FUTURE"),
    (2719, "06:18", True, 126, "DONE_TODAY"),
    (2720, "19:19", False, 143, "FUTURE"),
    (2721, "08:20", False, 160, "FUTURE"),
    (2722, "21:21", True, 177, "DONE_TODAY"),
    (2723, "10:22", False, 194, "FUTURE"),
    (2724, "23:23", False, 211, "FUTURE"),
    (2725, "12:24", True, 228, "DONE_TODAY"),
    (2726, "01:25", False, 245, "DUE"),
    (2727, "14:26", False, 262, "FUTURE"),
    (2728, "03:27", True, 279, "DONE_TODAY"),
    (2729, "16:28", False, 296, "FUTURE"),
    (2730, "05:29", False, 313, "FUTURE"),
    (2731, "18:30", True, 330, "DONE_TODAY"),
    (2732, "07:31", False, 347, "FUTURE"),
    (2733, "20:32", False, 364, "FUTURE"),
    (2734, "09:33", True, 381, "DONE_TODAY"),
    (2735, "22:34", False, 398, "FUTURE"),
    (2736, "11:35", False, 415, "FUTURE"),
    (2737, "00:36", True, 432, "DONE_TODAY"),
    (2738, "13:37", False, 449, "FUTURE"),
    (2739, "02:38", False, 466, "DUE"),
    (2740, "15:39", True, 483, "DONE_TODAY"),
    (2741, "04:40", False, 500, "DUE"),
    (2742, "17:41", False, 517, "FUTURE"),
    (2743, "06:42", True, 534, "DONE_TODAY"),
    (2744, "19:43", False, 551, "FUTURE"),
    (2745, "08:44", False, 568, "DUE"),
    (2746, "21:45", True, 585, "DONE_TODAY"),
    (2747, "10:46", False, 602, "FUTURE"),
    (2748, "23:47", False, 619, "FUTURE"),
    (2749, "12:48", True, 636, "DONE_TODAY"),
    (2750, "01:49", False, 653, "DUE"),
    (2751, "14:50", False, 670, "FUTURE"),
    (2752, "03:51", True, 687, "DONE_TODAY"),
    (2753, "16:52", False, 704, "FUTURE"),
    (2754, "05:53", False, 721, "DUE"),
    (2755, "18:54", True, 738, "DONE_TODAY"),
    (2756, "07:55", False, 755, "DUE"),
    (2757, "20:56", False, 772, "FUTURE"),
    (2758, "09:57", True, 789, "DONE_TODAY"),
    (2759, "22:58", False, 806, "FUTURE"),
    (2760, "11:59", False, 823, "DUE"),
    (2761, "00:00", True, 840, "DONE_TODAY"),
    (2762, "13:01", False, 857, "DUE"),
    (2763, "02:02", False, 874, "DUE"),
    (2764, "15:03", True, 891, "DONE_TODAY"),
    (2765, "04:04", False, 908, "DUE"),
    (2766, "17:05", False, 925, "FUTURE"),
    (2767, "06:06", True, 942, "DONE_TODAY"),
    (2768, "19:07", False, 959, "FUTURE"),
    (2769, "08:08", False, 976, "DUE"),
    (2770, "21:09", True, 993, "DONE_TODAY"),
    (2771, "10:10", False, 1010, "DUE"),
    (2772, "23:11", False, 1027, "FUTURE"),
    (2773, "12:12", True, 1044, "DONE_TODAY"),
    (2774, "01:13", False, 1061, "DUE"),
    (2775, "14:14", False, 1078, "DUE"),
    (2776, "03:15", True, 1095, "DONE_TODAY"),
    (2777, "16:16", False, 1112, "DUE"),
    (2778, "05:17", False, 1129, "DUE"),
    (2779, "18:18", True, 1146, "DONE_TODAY"),
    (2780, "07:19", False, 1163, "DUE"),
    (2781, "20:20", False, 1180, "FUTURE"),
    (2782, "09:21", True, 1197, "DONE_TODAY"),
    (2783, "22:22", False, 1214, "FUTURE"),
    (2784, "11:23", False, 1231, "DUE"),
    (2785, "00:24", True, 1248, "DONE_TODAY"),
    (2786, "13:25", False, 1265, "DUE"),
    (2787, "02:26", False, 1282, "DUE"),
    (2788, "15:27", True, 1299, "DONE_TODAY"),
    (2789, "04:28", False, 1316, "DUE"),
    (2790, "17:29", False, 1333, "DUE"),
    (2791, "06:30", True, 1350, "DONE_TODAY"),
    (2792, "19:31", False, 1367, "DUE"),
    (2793, "08:32", False, 1384, "DUE"),
    (2794, "21:33", True, 1401, "DONE_TODAY"),
    (2795, "10:34", False, 1418, "DUE"),
    (2796, "23:35", False, 1435, "DUE"),
    (2797, "12:36", True, 12, "DONE_TODAY"),
    (2798, "01:37", False, 29, "FUTURE"),
    (2799, "14:38", False, 46, "FUTURE"),
    (2800, "03:39", True, 63, "DONE_TODAY"),
    (2801, "16:40", False, 80, "FUTURE"),
    (2802, "05:41", False, 97, "FUTURE"),
    (2803, "18:42", True, 114, "DONE_TODAY"),
    (2804, "07:43", False, 131, "FUTURE"),
    (2805, "20:44", False, 148, "FUTURE"),
    (2806, "09:45", True, 165, "DONE_TODAY"),
    (2807, "22:46", False, 182, "FUTURE"),
    (2808, "11:47", False, 199, "FUTURE"),
    (2809, "00:48", True, 216, "DONE_TODAY"),
    (2810, "13:49", False, 233, "FUTURE"),
    (2811, "02:50", False, 250, "DUE"),
    (2812, "15:51", True, 267, "DONE_TODAY"),
    (2813, "04:52", False, 284, "FUTURE"),
    (2814, "17:53", False, 301, "FUTURE"),
    (2815, "06:54", True, 318, "DONE_TODAY"),
    (2816, "19:55", False, 335, "FUTURE"),
    (2817, "08:56", False, 352, "FUTURE"),
    (2818, "21:57", True, 369, "DONE_TODAY"),
    (2819, "10:58", False, 386, "FUTURE"),
    (2820, "23:59", False, 403, "FUTURE"),
    (2821, "12:00", True, 420, "DONE_TODAY"),
    (2822, "01:01", False, 437, "DUE"),
    (2823, "14:02", False, 454, "FUTURE"),
    (2824, "03:03", True, 471, "DONE_TODAY"),
    (2825, "16:04", False, 488, "FUTURE"),
    (2826, "05:05", False, 505, "DUE"),
    (2827, "18:06", True, 522, "DONE_TODAY"),
    (2828, "07:07", False, 539, "DUE"),
    (2829, "20:08", False, 556, "FUTURE"),
    (2830, "09:09", True, 573, "DONE_TODAY"),
    (2831, "22:10", False, 590, "FUTURE"),
    (2832, "11:11", False, 607, "FUTURE"),
    (2833, "00:12", True, 624, "DONE_TODAY"),
    (2834, "13:13", False, 641, "FUTURE"),
    (2835, "02:14", False, 658, "DUE"),
    (2836, "15:15", True, 675, "DONE_TODAY"),
    (2837, "04:16", False, 692, "DUE"),
    (2838, "17:17", False, 709, "FUTURE"),
    (2839, "06:18", True, 726, "DONE_TODAY"),
    (2840, "19:19", False, 743, "FUTURE"),
    (2841, "08:20", False, 760, "DUE"),
    (2842, "21:21", True, 777, "DONE_TODAY"),
    (2843, "10:22", False, 794, "DUE"),
    (2844, "23:23", False, 811, "FUTURE"),
    (2845, "12:24", True, 828, "DONE_TODAY"),
    (2846, "01:25", False, 845, "DUE"),
    (2847, "14:26", False, 862, "FUTURE"),
    (2848, "03:27", True, 879, "DONE_TODAY"),
    (2849, "16:28", False, 896, "FUTURE"),
    (2850, "05:29", False, 913, "DUE"),
    (2851, "18:30", True, 930, "DONE_TODAY"),
    (2852, "07:31", False, 947, "DUE"),
    (2853, "20:32", False, 964, "FUTURE"),
    (2854, "09:33", True, 981, "DONE_TODAY"),
    (2855, "22:34", False, 998, "FUTURE"),
    (2856, "11:35", False, 1015, "DUE"),
    (2857, "00:36", True, 1032, "DONE_TODAY"),
    (2858, "13:37", False, 1049, "DUE"),
    (2859, "02:38", False, 1066, "DUE"),
    (2860, "15:39", True, 1083, "DONE_TODAY"),
    (2861, "04:40", False, 1100, "DUE"),
    (2862, "17:41", False, 1117, "DUE"),
    (2863, "06:42", True, 1134, "DONE_TODAY"),
    (2864, "19:43", False, 1151, "FUTURE"),
    (2865, "08:44", False, 1168, "DUE"),
    (2866, "21:45", True, 1185, "DONE_TODAY"),
    (2867, "10:46", False, 1202, "DUE"),
    (2868, "23:47", False, 1219, "FUTURE"),
    (2869, "12:48", True, 1236, "DONE_TODAY"),
    (2870, "01:49", False, 1253, "DUE"),
    (2871, "14:50", False, 1270, "DUE"),
    (2872, "03:51", True, 1287, "DONE_TODAY"),
    (2873, "16:52", False, 1304, "DUE"),
    (2874, "05:53", False, 1321, "DUE"),
    (2875, "18:54", True, 1338, "DONE_TODAY"),
    (2876, "07:55", False, 1355, "DUE"),
    (2877, "20:56", False, 1372, "DUE"),
    (2878, "09:57", True, 1389, "DONE_TODAY"),
    (2879, "22:58", False, 1406, "DUE"),
    (2880, "11:59", False, 1423, "DUE"),
    (2881, "00:00", True, 0, "DONE_TODAY"),
    (2882, "13:01", False, 17, "FUTURE"),
    (2883, "02:02", False, 34, "FUTURE"),
    (2884, "15:03", True, 51, "DONE_TODAY"),
    (2885, "04:04", False, 68, "FUTURE"),
    (2886, "17:05", False, 85, "FUTURE"),
    (2887, "06:06", True, 102, "DONE_TODAY"),
    (2888, "19:07", False, 119, "FUTURE"),
    (2889, "08:08", False, 136, "FUTURE"),
    (2890, "21:09", True, 153, "DONE_TODAY"),
    (2891, "10:10", False, 170, "FUTURE"),
    (2892, "23:11", False, 187, "FUTURE"),
    (2893, "12:12", True, 204, "DONE_TODAY"),
    (2894, "01:13", False, 221, "DUE"),
    (2895, "14:14", False, 238, "FUTURE"),
    (2896, "03:15", True, 255, "DONE_TODAY"),
    (2897, "16:16", False, 272, "FUTURE"),
    (2898, "05:17", False, 289, "FUTURE"),
    (2899, "18:18", True, 306, "DONE_TODAY"),
    (2900, "07:19", False, 323, "FUTURE"),
    (2901, "20:20", False, 340, "FUTURE"),
    (2902, "09:21", True, 357, "DONE_TODAY"),
    (2903, "22:22", False, 374, "FUTURE"),
    (2904, "11:23", False, 391, "FUTURE"),
    (2905, "00:24", True, 408, "DONE_TODAY"),
    (2906, "13:25", False, 425, "FUTURE"),
    (2907, "02:26", False, 442, "DUE"),
    (2908, "15:27", True, 459, "DONE_TODAY"),
    (2909, "04:28", False, 476, "DUE"),
    (2910, "17:29", False, 493, "FUTURE"),
    (2911, "06:30", True, 510, "DONE_TODAY"),
    (2912, "19:31", False, 527, "FUTURE"),
    (2913, "08:32", False, 544, "DUE"),
    (2914, "21:33", True, 561, "DONE_TODAY"),
    (2915, "10:34", False, 578, "FUTURE"),
    (2916, "23:35", False, 595, "FUTURE"),
    (2917, "12:36", True, 612, "DONE_TODAY"),
    (2918, "01:37", False, 629, "DUE"),
    (2919, "14:38", False, 646, "FUTURE"),
    (2920, "03:39", True, 663, "DONE_TODAY"),
    (2921, "16:40", False, 680, "FUTURE"),
    (2922, "05:41", False, 697, "DUE"),
    (2923, "18:42", True, 714, "DONE_TODAY"),
    (2924, "07:43", False, 731, "DUE"),
    (2925, "20:44", False, 748, "FUTURE"),
    (2926, "09:45", True, 765, "DONE_TODAY"),
    (2927, "22:46", False, 782, "FUTURE"),
    (2928, "11:47", False, 799, "DUE"),
    (2929, "00:48", True, 816, "DONE_TODAY"),
    (2930, "13:49", False, 833, "DUE"),
    (2931, "02:50", False, 850, "DUE"),
    (2932, "15:51", True, 867, "DONE_TODAY"),
    (2933, "04:52", False, 884, "DUE"),
    (2934, "17:53", False, 901, "FUTURE"),
    (2935, "06:54", True, 918, "DONE_TODAY"),
    (2936, "19:55", False, 935, "FUTURE"),
    (2937, "08:56", False, 952, "DUE"),
    (2938, "21:57", True, 969, "DONE_TODAY"),
    (2939, "10:58", False, 986, "DUE"),
    (2940, "23:59", False, 1003, "FUTURE"),
    (2941, "12:00", True, 1020, "DONE_TODAY"),
    (2942, "01:01", False, 1037, "DUE"),
    (2943, "14:02", False, 1054, "DUE"),
    (2944, "03:03", True, 1071, "DONE_TODAY"),
    (2945, "16:04", False, 1088, "DUE"),
    (2946, "05:05", False, 1105, "DUE"),
    (2947, "18:06", True, 1122, "DONE_TODAY"),
    (2948, "07:07", False, 1139, "DUE"),
    (2949, "20:08", False, 1156, "FUTURE"),
    (2950, "09:09", True, 1173, "DONE_TODAY"),
    (2951, "22:10", False, 1190, "FUTURE"),
    (2952, "11:11", False, 1207, "DUE"),
    (2953, "00:12", True, 1224, "DONE_TODAY"),
    (2954, "13:13", False, 1241, "DUE"),
    (2955, "02:14", False, 1258, "DUE"),
    (2956, "15:15", True, 1275, "DONE_TODAY"),
    (2957, "04:16", False, 1292, "DUE"),
    (2958, "17:17", False, 1309, "DUE"),
    (2959, "06:18", True, 1326, "DONE_TODAY"),
    (2960, "19:19", False, 1343, "DUE"),
    (2961, "08:20", False, 1360, "DUE"),
    (2962, "21:21", True, 1377, "DONE_TODAY"),
    (2963, "10:22", False, 1394, "DUE"),
    (2964, "23:23", False, 1411, "DUE"),
    (2965, "12:24", True, 1428, "DONE_TODAY"),
    (2966, "01:25", False, 5, "FUTURE"),
    (2967, "14:26", False, 22, "FUTURE"),
    (2968, "03:27", True, 39, "DONE_TODAY"),
    (2969, "16:28", False, 56, "FUTURE"),
    (2970, "05:29", False, 73, "FUTURE"),
    (2971, "18:30", True, 90, "DONE_TODAY"),
    (2972, "07:31", False, 107, "FUTURE"),
    (2973, "20:32", False, 124, "FUTURE"),
    (2974, "09:33", True, 141, "DONE_TODAY"),
    (2975, "22:34", False, 158, "FUTURE"),
    (2976, "11:35", False, 175, "FUTURE"),
    (2977, "00:36", True, 192, "DONE_TODAY"),
    (2978, "13:37", False, 209, "FUTURE"),
    (2979, "02:38", False, 226, "DUE"),
    (2980, "15:39", True, 243, "DONE_TODAY"),
    (2981, "04:40", False, 260, "FUTURE"),
    (2982, "17:41", False, 277, "FUTURE"),
    (2983, "06:42", True, 294, "DONE_TODAY"),
    (2984, "19:43", False, 311, "FUTURE"),
    (2985, "08:44", False, 328, "FUTURE"),
    (2986, "21:45", True, 345, "DONE_TODAY"),
    (2987, "10:46", False, 362, "FUTURE"),
    (2988, "23:47", False, 379, "FUTURE"),
    (2989, "12:48", True, 396, "DONE_TODAY"),
    (2990, "01:49", False, 413, "DUE"),
    (2991, "14:50", False, 430, "FUTURE"),
    (2992, "03:51", True, 447, "DONE_TODAY"),
    (2993, "16:52", False, 464, "FUTURE"),
    (2994, "05:53", False, 481, "DUE"),
    (2995, "18:54", True, 498, "DONE_TODAY"),
    (2996, "07:55", False, 515, "DUE"),
    (2997, "20:56", False, 532, "FUTURE"),
    (2998, "09:57", True, 549, "DONE_TODAY"),
    (2999, "22:58", False, 566, "FUTURE"),
    (3000, "11:59", False, 583, "FUTURE"),
    (3001, "00:00", True, 600, "DONE_TODAY"),
    (3002, "13:01", False, 617, "FUTURE"),
    (3003, "02:02", False, 634, "DUE"),
    (3004, "15:03", True, 651, "DONE_TODAY"),
    (3005, "04:04", False, 668, "DUE"),
    (3006, "17:05", False, 685, "FUTURE"),
    (3007, "06:06", True, 702, "DONE_TODAY"),
    (3008, "19:07", False, 719, "FUTURE"),
    (3009, "08:08", False, 736, "DUE"),
    (3010, "21:09", True, 753, "DONE_TODAY"),
    (3011, "10:10", False, 770, "DUE"),
    (3012, "23:11", False, 787, "FUTURE"),
    (3013, "12:12", True, 804, "DONE_TODAY"),
    (3014, "01:13", False, 821, "DUE"),
    (3015, "14:14", False, 838, "FUTURE"),
    (3016, "03:15", True, 855, "DONE_TODAY"),
    (3017, "16:16", False, 872, "FUTURE"),
    (3018, "05:17", False, 889, "DUE"),
    (3019, "18:18", True, 906, "DONE_TODAY"),
    (3020, "07:19", False, 923, "DUE"),
    (3021, "20:20", False, 940, "FUTURE"),
    (3022, "09:21", True, 957, "DONE_TODAY"),
    (3023, "22:22", False, 974, "FUTURE"),
    (3024, "11:23", False, 991, "DUE"),
    (3025, "00:24", True, 1008, "DONE_TODAY"),
    (3026, "13:25", False, 1025, "DUE"),
    (3027, "02:26", False, 1042, "DUE"),
    (3028, "15:27", True, 1059, "DONE_TODAY"),
    (3029, "04:28", False, 1076, "DUE"),
    (3030, "17:29", False, 1093, "DUE"),
    (3031, "06:30", True, 1110, "DONE_TODAY"),
    (3032, "19:31", False, 1127, "FUTURE"),
    (3033, "08:32", False, 1144, "DUE"),
    (3034, "21:33", True, 1161, "DONE_TODAY"),
    (3035, "10:34", False, 1178, "DUE"),
    (3036, "23:35", False, 1195, "FUTURE"),
    (3037, "12:36", True, 1212, "DONE_TODAY"),
    (3038, "01:37", False, 1229, "DUE"),
    (3039, "14:38", False, 1246, "DUE"),
    (3040, "03:39", True, 1263, "DONE_TODAY"),
    (3041, "16:40", False, 1280, "DUE"),
    (3042, "05:41", False, 1297, "DUE"),
    (3043, "18:42", True, 1314, "DONE_TODAY"),
    (3044, "07:43", False, 1331, "DUE"),
    (3045, "20:44", False, 1348, "DUE"),
    (3046, "09:45", True, 1365, "DONE_TODAY"),
    (3047, "22:46", False, 1382, "DUE"),
    (3048, "11:47", False, 1399, "DUE"),
    (3049, "00:48", True, 1416, "DONE_TODAY"),
    (3050, "13:49", False, 1433, "DUE"),
    (3051, "02:50", False, 10, "FUTURE"),
    (3052, "15:51", True, 27, "DONE_TODAY"),
    (3053, "04:52", False, 44, "FUTURE"),
    (3054, "17:53", False, 61, "FUTURE"),
    (3055, "06:54", True, 78, "DONE_TODAY"),
    (3056, "19:55", False, 95, "FUTURE"),
    (3057, "08:56", False, 112, "FUTURE"),
    (3058, "21:57", True, 129, "DONE_TODAY"),
    (3059, "10:58", False, 146, "FUTURE"),
    (3060, "23:59", False, 163, "FUTURE"),
    (3061, "12:00", True, 180, "DONE_TODAY"),
    (3062, "01:01", False, 197, "DUE"),
    (3063, "14:02", False, 214, "FUTURE"),
    (3064, "03:03", True, 231, "DONE_TODAY"),
    (3065, "16:04", False, 248, "FUTURE"),
    (3066, "05:05", False, 265, "FUTURE"),
    (3067, "18:06", True, 282, "DONE_TODAY"),
    (3068, "07:07", False, 299, "FUTURE"),
    (3069, "20:08", False, 316, "FUTURE"),
    (3070, "09:09", True, 333, "DONE_TODAY"),
    (3071, "22:10", False, 350, "FUTURE"),
    (3072, "11:11", False, 367, "FUTURE"),
    (3073, "00:12", True, 384, "DONE_TODAY"),
    (3074, "13:13", False, 401, "FUTURE"),
    (3075, "02:14", False, 418, "DUE"),
    (3076, "15:15", True, 435, "DONE_TODAY"),
    (3077, "04:16", False, 452, "DUE"),
    (3078, "17:17", False, 469, "FUTURE"),
    (3079, "06:18", True, 486, "DONE_TODAY"),
    (3080, "19:19", False, 503, "FUTURE"),
    (3081, "08:20", False, 520, "DUE"),
    (3082, "21:21", True, 537, "DONE_TODAY"),
    (3083, "10:22", False, 554, "FUTURE"),
    (3084, "23:23", False, 571, "FUTURE"),
    (3085, "12:24", True, 588, "DONE_TODAY"),
    (3086, "01:25", False, 605, "DUE"),
    (3087, "14:26", False, 622, "FUTURE"),
    (3088, "03:27", True, 639, "DONE_TODAY"),
    (3089, "16:28", False, 656, "FUTURE"),
    (3090, "05:29", False, 673, "DUE"),
    (3091, "18:30", True, 690, "DONE_TODAY"),
    (3092, "07:31", False, 707, "DUE"),
    (3093, "20:32", False, 724, "FUTURE"),
    (3094, "09:33", True, 741, "DONE_TODAY"),
    (3095, "22:34", False, 758, "FUTURE"),
    (3096, "11:35", False, 775, "DUE"),
    (3097, "00:36", True, 792, "DONE_TODAY"),
    (3098, "13:37", False, 809, "FUTURE"),
    (3099, "02:38", False, 826, "DUE"),
    (3100, "15:39", True, 843, "DONE_TODAY"),
    (3101, "04:40", False, 860, "DUE"),
    (3102, "17:41", False, 877, "FUTURE"),
    (3103, "06:42", True, 894, "DONE_TODAY"),
    (3104, "19:43", False, 911, "FUTURE"),
    (3105, "08:44", False, 928, "DUE"),
    (3106, "21:45", True, 945, "DONE_TODAY"),
    (3107, "10:46", False, 962, "DUE"),
    (3108, "23:47", False, 979, "FUTURE"),
    (3109, "12:48", True, 996, "DONE_TODAY"),
    (3110, "01:49", False, 1013, "DUE"),
    (3111, "14:50", False, 1030, "DUE"),
    (3112, "03:51", True, 1047, "DONE_TODAY"),
    (3113, "16:52", False, 1064, "DUE"),
    (3114, "05:53", False, 1081, "DUE"),
    (3115, "18:54", True, 1098, "DONE_TODAY"),
    (3116, "07:55", False, 1115, "DUE"),
    (3117, "20:56", False, 1132, "FUTURE"),
    (3118, "09:57", True, 1149, "DONE_TODAY"),
    (3119, "22:58", False, 1166, "FUTURE"),
    (3120, "11:59", False, 1183, "DUE"),
    (3121, "00:00", True, 1200, "DONE_TODAY"),
    (3122, "13:01", False, 1217, "DUE"),
    (3123, "02:02", False, 1234, "DUE"),
    (3124, "15:03", True, 1251, "DONE_TODAY"),
    (3125, "04:04", False, 1268, "DUE"),
    (3126, "17:05", False, 1285, "DUE"),
    (3127, "06:06", True, 1302, "DONE_TODAY"),
    (3128, "19:07", False, 1319, "DUE"),
    (3129, "08:08", False, 1336, "DUE"),
    (3130, "21:09", True, 1353, "DONE_TODAY"),
    (3131, "10:10", False, 1370, "DUE"),
    (3132, "23:11", False, 1387, "FUTURE"),
    (3133, "12:12", True, 1404, "DONE_TODAY"),
    (3134, "01:13", False, 1421, "DUE"),
    (3135, "14:14", False, 1438, "DUE"),
    (3136, "03:15", True, 15, "DONE_TODAY"),
    (3137, "16:16", False, 32, "FUTURE"),
    (3138, "05:17", False, 49, "FUTURE"),
    (3139, "18:18", True, 66, "DONE_TODAY"),
    (3140, "07:19", False, 83, "FUTURE"),
    (3141, "20:20", False, 100, "FUTURE"),
    (3142, "09:21", True, 117, "DONE_TODAY"),
    (3143, "22:22", False, 134, "FUTURE"),
    (3144, "11:23", False, 151, "FUTURE"),
    (3145, "00:24", True, 168, "DONE_TODAY"),
    (3146, "13:25", False, 185, "FUTURE"),
    (3147, "02:26", False, 202, "DUE"),
    (3148, "15:27", True, 219, "DONE_TODAY"),
    (3149, "04:28", False, 236, "FUTURE"),
    (3150, "17:29", False, 253, "FUTURE"),
    (3151, "06:30", True, 270, "DONE_TODAY"),
    (3152, "19:31", False, 287, "FUTURE"),
    (3153, "08:32", False, 304, "FUTURE"),
    (3154, "21:33", True, 321, "DONE_TODAY"),
    (3155, "10:34", False, 338, "FUTURE"),
    (3156, "23:35", False, 355, "FUTURE"),
    (3157, "12:36", True, 372, "DONE_TODAY"),
    (3158, "01:37", False, 389, "DUE"),
    (3159, "14:38", False, 406, "FUTURE"),
    (3160, "03:39", True, 423, "DONE_TODAY"),
    (3161, "16:40", False, 440, "FUTURE"),
    (3162, "05:41", False, 457, "DUE"),
    (3163, "18:42", True, 474, "DONE_TODAY"),
    (3164, "07:43", False, 491, "DUE"),
    (3165, "20:44", False, 508, "FUTURE"),
    (3166, "09:45", True, 525, "DONE_TODAY"),
    (3167, "22:46", False, 542, "FUTURE"),
    (3168, "11:47", False, 559, "FUTURE"),
    (3169, "00:48", True, 576, "DONE_TODAY"),
    (3170, "13:49", False, 593, "FUTURE"),
    (3171, "02:50", False, 610, "DUE"),
    (3172, "15:51", True, 627, "DONE_TODAY"),
    (3173, "04:52", False, 644, "DUE"),
    (3174, "17:53", False, 661, "FUTURE"),
    (3175, "06:54", True, 678, "DONE_TODAY"),
    (3176, "19:55", False, 695, "FUTURE"),
    (3177, "08:56", False, 712, "DUE"),
    (3178, "21:57", True, 729, "DONE_TODAY"),
    (3179, "10:58", False, 746, "DUE"),
    (3180, "23:59", False, 763, "FUTURE"),
    (3181, "12:00", True, 780, "DONE_TODAY"),
    (3182, "01:01", False, 797, "DUE"),
    (3183, "14:02", False, 814, "FUTURE"),
    (3184, "03:03", True, 831, "DONE_TODAY"),
    (3185, "16:04", False, 848, "FUTURE"),
    (3186, "05:05", False, 865, "DUE"),
    (3187, "18:06", True, 882, "DONE_TODAY"),
    (3188, "07:07", False, 899, "DUE"),
    (3189, "20:08", False, 916, "FUTURE"),
    (3190, "09:09", True, 933, "DONE_TODAY"),
    (3191, "22:10", False, 950, "FUTURE"),
    (3192, "11:11", False, 967, "DUE"),
    (3193, "00:12", True, 984, "DONE_TODAY"),
    (3194, "13:13", False, 1001, "DUE"),
    (3195, "02:14", False, 1018, "DUE"),
    (3196, "15:15", True, 1035, "DONE_TODAY"),
    (3197, "04:16", False, 1052, "DUE"),
    (3198, "17:17", False, 1069, "DUE"),
    (3199, "06:18", True, 1086, "DONE_TODAY"),
    (3200, "19:19", False, 1103, "FUTURE"),
    (3201, "08:20", False, 1120, "DUE"),
    (3202, "21:21", True, 1137, "DONE_TODAY"),
    (3203, "10:22", False, 1154, "DUE"),
    (3204, "23:23", False, 1171, "FUTURE"),
    (3205, "12:24", True, 1188, "DONE_TODAY"),
    (3206, "01:25", False, 1205, "DUE"),
    (3207, "14:26", False, 1222, "DUE"),
    (3208, "03:27", True, 1239, "DONE_TODAY"),
    (3209, "16:28", False, 1256, "DUE"),
    (3210, "05:29", False, 1273, "DUE"),
    (3211, "18:30", True, 1290, "DONE_TODAY"),
    (3212, "07:31", False, 1307, "DUE"),
    (3213, "20:32", False, 1324, "DUE"),
    (3214, "09:33", True, 1341, "DONE_TODAY"),
    (3215, "22:34", False, 1358, "DUE"),
    (3216, "11:35", False, 1375, "DUE"),
    (3217, "00:36", True, 1392, "DONE_TODAY"),
    (3218, "13:37", False, 1409, "DUE"),
    (3219, "02:38", False, 1426, "DUE"),
    (3220, "15:39", True, 3, "DONE_TODAY"),
    (3221, "04:40", False, 20, "FUTURE"),
    (3222, "17:41", False, 37, "FUTURE"),
    (3223, "06:42", True, 54, "DONE_TODAY"),
    (3224, "19:43", False, 71, "FUTURE"),
    (3225, "08:44", False, 88, "FUTURE"),
    (3226, "21:45", True, 105, "DONE_TODAY"),
    (3227, "10:46", False, 122, "FUTURE"),
    (3228, "23:47", False, 139, "FUTURE"),
    (3229, "12:48", True, 156, "DONE_TODAY"),
    (3230, "01:49", False, 173, "DUE"),
    (3231, "14:50", False, 190, "FUTURE"),
    (3232, "03:51", True, 207, "DONE_TODAY"),
    (3233, "16:52", False, 224, "FUTURE"),
    (3234, "05:53", False, 241, "FUTURE"),
    (3235, "18:54", True, 258, "DONE_TODAY"),
    (3236, "07:55", False, 275, "FUTURE"),
    (3237, "20:56", False, 292, "FUTURE"),
    (3238, "09:57", True, 309, "DONE_TODAY"),
    (3239, "22:58", False, 326, "FUTURE"),
    (3240, "11:59", False, 343, "FUTURE"),
    (3241, "00:00", True, 360, "DONE_TODAY"),
    (3242, "13:01", False, 377, "FUTURE"),
    (3243, "02:02", False, 394, "DUE"),
    (3244, "15:03", True, 411, "DONE_TODAY"),
    (3245, "04:04", False, 428, "DUE"),
    (3246, "17:05", False, 445, "FUTURE"),
    (3247, "06:06", True, 462, "DONE_TODAY"),
    (3248, "19:07", False, 479, "FUTURE"),
    (3249, "08:08", False, 496, "DUE"),
    (3250, "21:09", True, 513, "DONE_TODAY"),
    (3251, "10:10", False, 530, "FUTURE"),
    (3252, "23:11", False, 547, "FUTURE"),
    (3253, "12:12", True, 564, "DONE_TODAY"),
    (3254, "01:13", False, 581, "DUE"),
    (3255, "14:14", False, 598, "FUTURE"),
    (3256, "03:15", True, 615, "DONE_TODAY"),
    (3257, "16:16", False, 632, "FUTURE"),
    (3258, "05:17", False, 649, "DUE"),
    (3259, "18:18", True, 666, "DONE_TODAY"),
    (3260, "07:19", False, 683, "DUE"),
    (3261, "20:20", False, 700, "FUTURE"),
    (3262, "09:21", True, 717, "DONE_TODAY"),
    (3263, "22:22", False, 734, "FUTURE"),
    (3264, "11:23", False, 751, "DUE"),
    (3265, "00:24", True, 768, "DONE_TODAY"),
    (3266, "13:25", False, 785, "FUTURE"),
    (3267, "02:26", False, 802, "DUE"),
    (3268, "15:27", True, 819, "DONE_TODAY"),
    (3269, "04:28", False, 836, "DUE"),
    (3270, "17:29", False, 853, "FUTURE"),
    (3271, "06:30", True, 870, "DONE_TODAY"),
    (3272, "19:31", False, 887, "FUTURE"),
    (3273, "08:32", False, 904, "DUE"),
    (3274, "21:33", True, 921, "DONE_TODAY"),
    (3275, "10:34", False, 938, "DUE"),
    (3276, "23:35", False, 955, "FUTURE"),
    (3277, "12:36", True, 972, "DONE_TODAY"),
    (3278, "01:37", False, 989, "DUE"),
    (3279, "14:38", False, 1006, "DUE"),
    (3280, "03:39", True, 1023, "DONE_TODAY"),
    (3281, "16:40", False, 1040, "DUE"),
    (3282, "05:41", False, 1057, "DUE"),
    (3283, "18:42", True, 1074, "DONE_TODAY"),
    (3284, "07:43", False, 1091, "DUE"),
    (3285, "20:44", False, 1108, "FUTURE"),
    (3286, "09:45", True, 1125, "DONE_TODAY"),
    (3287, "22:46", False, 1142, "FUTURE"),
    (3288, "11:47", False, 1159, "DUE"),
    (3289, "00:48", True, 1176, "DONE_TODAY"),
    (3290, "13:49", False, 1193, "DUE"),
    (3291, "02:50", False, 1210, "DUE"),
    (3292, "15:51", True, 1227, "DONE_TODAY"),
    (3293, "04:52", False, 1244, "DUE"),
    (3294, "17:53", False, 1261, "DUE"),
    (3295, "06:54", True, 1278, "DONE_TODAY"),
    (3296, "19:55", False, 1295, "DUE"),
    (3297, "08:56", False, 1312, "DUE"),
    (3298, "21:57", True, 1329, "DONE_TODAY"),
    (3299, "10:58", False, 1346, "DUE"),
    (3300, "23:59", False, 1363, "FUTURE"),
    (3301, "12:00", True, 1380, "DONE_TODAY"),
    (3302, "01:01", False, 1397, "DUE"),
    (3303, "14:02", False, 1414, "DUE"),
    (3304, "03:03", True, 1431, "DONE_TODAY"),
    (3305, "16:04", False, 8, "FUTURE"),
    (3306, "05:05", False, 25, "FUTURE"),
    (3307, "18:06", True, 42, "DONE_TODAY"),
    (3308, "07:07", False, 59, "FUTURE"),
    (3309, "20:08", False, 76, "FUTURE"),
    (3310, "09:09", True, 93, "DONE_TODAY"),
    (3311, "22:10", False, 110, "FUTURE"),
    (3312, "11:11", False, 127, "FUTURE"),
    (3313, "00:12", True, 144, "DONE_TODAY"),
    (3314, "13:13", False, 161, "FUTURE"),
    (3315, "02:14", False, 178, "DUE"),
    (3316, "15:15", True, 195, "DONE_TODAY"),
    (3317, "04:16", False, 212, "FUTURE"),
    (3318, "17:17", False, 229, "FUTURE"),
    (3319, "06:18", True, 246, "DONE_TODAY"),
    (3320, "19:19", False, 263, "FUTURE"),
    (3321, "08:20", False, 280, "FUTURE"),
    (3322, "21:21", True, 297, "DONE_TODAY"),
    (3323, "10:22", False, 314, "FUTURE"),
    (3324, "23:23", False, 331, "FUTURE"),
    (3325, "12:24", True, 348, "DONE_TODAY"),
    (3326, "01:25", False, 365, "DUE"),
    (3327, "14:26", False, 382, "FUTURE"),
    (3328, "03:27", True, 399, "DONE_TODAY"),
    (3329, "16:28", False, 416, "FUTURE"),
    (3330, "05:29", False, 433, "DUE"),
    (3331, "18:30", True, 450, "DONE_TODAY"),
    (3332, "07:31", False, 467, "DUE"),
    (3333, "20:32", False, 484, "FUTURE"),
    (3334, "09:33", True, 501, "DONE_TODAY"),
    (3335, "22:34", False, 518, "FUTURE"),
    (3336, "11:35", False, 535, "FUTURE"),
    (3337, "00:36", True, 552, "DONE_TODAY"),
    (3338, "13:37", False, 569, "FUTURE"),
    (3339, "02:38", False, 586, "DUE"),
    (3340, "15:39", True, 603, "DONE_TODAY"),
    (3341, "04:40", False, 620, "DUE"),
    (3342, "17:41", False, 637, "FUTURE"),
    (3343, "06:42", True, 654, "DONE_TODAY"),
    (3344, "19:43", False, 671, "FUTURE"),
    (3345, "08:44", False, 688, "DUE"),
    (3346, "21:45", True, 705, "DONE_TODAY"),
    (3347, "10:46", False, 722, "DUE"),
    (3348, "23:47", False, 739, "FUTURE"),
    (3349, "12:48", True, 756, "DONE_TODAY"),
    (3350, "01:49", False, 773, "DUE"),
    (3351, "14:50", False, 790, "FUTURE"),
    (3352, "03:51", True, 807, "DONE_TODAY"),
    (3353, "16:52", False, 824, "FUTURE"),
    (3354, "05:53", False, 841, "DUE"),
    (3355, "18:54", True, 858, "DONE_TODAY"),
    (3356, "07:55", False, 875, "DUE"),
    (3357, "20:56", False, 892, "FUTURE"),
    (3358, "09:57", True, 909, "DONE_TODAY"),
    (3359, "22:58", False, 926, "FUTURE"),
    (3360, "11:59", False, 943, "DUE"),
    (3361, "00:00", True, 960, "DONE_TODAY"),
    (3362, "13:01", False, 977, "DUE"),
    (3363, "02:02", False, 994, "DUE"),
    (3364, "15:03", True, 1011, "DONE_TODAY"),
    (3365, "04:04", False, 1028, "DUE"),
    (3366, "17:05", False, 1045, "DUE"),
    (3367, "06:06", True, 1062, "DONE_TODAY"),
    (3368, "19:07", False, 1079, "FUTURE"),
    (3369, "08:08", False, 1096, "DUE"),
    (3370, "21:09", True, 1113, "DONE_TODAY"),
    (3371, "10:10", False, 1130, "DUE"),
    (3372, "23:11", False, 1147, "FUTURE"),
    (3373, "12:12", True, 1164, "DONE_TODAY"),
    (3374, "01:13", False, 1181, "DUE"),
    (3375, "14:14", False, 1198, "DUE"),
    (3376, "03:15", True, 1215, "DONE_TODAY"),
    (3377, "16:16", False, 1232, "DUE"),
    (3378, "05:17", False, 1249, "DUE"),
    (3379, "18:18", True, 1266, "DONE_TODAY"),
    (3380, "07:19", False, 1283, "DUE"),
    (3381, "20:20", False, 1300, "DUE"),
    (3382, "09:21", True, 1317, "DONE_TODAY"),
    (3383, "22:22", False, 1334, "FUTURE"),
    (3384, "11:23", False, 1351, "DUE"),
    (3385, "00:24", True, 1368, "DONE_TODAY"),
    (3386, "13:25", False, 1385, "DUE"),
    (3387, "02:26", False, 1402, "DUE"),
    (3388, "15:27", True, 1419, "DONE_TODAY"),
    (3389, "04:28", False, 1436, "DUE"),
    (3390, "17:29", False, 13, "FUTURE"),
    (3391, "06:30", True, 30, "DONE_TODAY"),
    (3392, "19:31", False, 47, "FUTURE"),
    (3393, "08:32", False, 64, "FUTURE"),
    (3394, "21:33", True, 81, "DONE_TODAY"),
    (3395, "10:34", False, 98, "FUTURE"),
    (3396, "23:35", False, 115, "FUTURE"),
    (3397, "12:36", True, 132, "DONE_TODAY"),
    (3398, "01:37", False, 149, "DUE"),
    (3399, "14:38", False, 166, "FUTURE"),
    (3400, "03:39", True, 183, "DONE_TODAY"),
    (3401, "16:40", False, 200, "FUTURE"),
    (3402, "05:41", False, 217, "FUTURE"),
    (3403, "18:42", True, 234, "DONE_TODAY"),
    (3404, "07:43", False, 251, "FUTURE"),
    (3405, "20:44", False, 268, "FUTURE"),
    (3406, "09:45", True, 285, "DONE_TODAY"),
    (3407, "22:46", False, 302, "FUTURE"),
    (3408, "11:47", False, 319, "FUTURE"),
    (3409, "00:48", True, 336, "DONE_TODAY"),
    (3410, "13:49", False, 353, "FUTURE"),
    (3411, "02:50", False, 370, "DUE"),
    (3412, "15:51", True, 387, "DONE_TODAY"),
    (3413, "04:52", False, 404, "DUE"),
    (3414, "17:53", False, 421, "FUTURE"),
    (3415, "06:54", True, 438, "DONE_TODAY"),
    (3416, "19:55", False, 455, "FUTURE"),
    (3417, "08:56", False, 472, "FUTURE"),
    (3418, "21:57", True, 489, "DONE_TODAY"),
    (3419, "10:58", False, 506, "FUTURE"),
    (3420, "23:59", False, 523, "FUTURE"),
    (3421, "12:00", True, 540, "DONE_TODAY"),
    (3422, "01:01", False, 557, "DUE"),
    (3423, "14:02", False, 574, "FUTURE"),
    (3424, "03:03", True, 591, "DONE_TODAY"),
    (3425, "16:04", False, 608, "FUTURE"),
    (3426, "05:05", False, 625, "DUE"),
    (3427, "18:06", True, 642, "DONE_TODAY"),
    (3428, "07:07", False, 659, "DUE"),
    (3429, "20:08", False, 676, "FUTURE"),
    (3430, "09:09", True, 693, "DONE_TODAY"),
    (3431, "22:10", False, 710, "FUTURE"),
    (3432, "11:11", False, 727, "DUE"),
    (3433, "00:12", True, 744, "DONE_TODAY"),
    (3434, "13:13", False, 761, "FUTURE"),
    (3435, "02:14", False, 778, "DUE"),
    (3436, "15:15", True, 795, "DONE_TODAY"),
    (3437, "04:16", False, 812, "DUE"),
    (3438, "17:17", False, 829, "FUTURE"),
    (3439, "06:18", True, 846, "DONE_TODAY"),
    (3440, "19:19", False, 863, "FUTURE"),
    (3441, "08:20", False, 880, "DUE"),
    (3442, "21:21", True, 897, "DONE_TODAY"),
    (3443, "10:22", False, 914, "DUE"),
    (3444, "23:23", False, 931, "FUTURE"),
    (3445, "12:24", True, 948, "DONE_TODAY"),
    (3446, "01:25", False, 965, "DUE"),
    (3447, "14:26", False, 982, "DUE"),
    (3448, "03:27", True, 999, "DONE_TODAY"),
    (3449, "16:28", False, 1016, "DUE"),
    (3450, "05:29", False, 1033, "DUE"),
    (3451, "18:30", True, 1050, "DONE_TODAY"),
    (3452, "07:31", False, 1067, "DUE"),
    (3453, "20:32", False, 1084, "FUTURE"),
    (3454, "09:33", True, 1101, "DONE_TODAY"),
    (3455, "22:34", False, 1118, "FUTURE"),
    (3456, "11:35", False, 1135, "DUE"),
    (3457, "00:36", True, 1152, "DONE_TODAY"),
    (3458, "13:37", False, 1169, "DUE"),
    (3459, "02:38", False, 1186, "DUE"),
    (3460, "15:39", True, 1203, "DONE_TODAY"),
    (3461, "04:40", False, 1220, "DUE"),
    (3462, "17:41", False, 1237, "DUE"),
    (3463, "06:42", True, 1254, "DONE_TODAY"),
    (3464, "19:43", False, 1271, "DUE"),
    (3465, "08:44", False, 1288, "DUE"),
    (3466, "21:45", True, 1305, "DONE_TODAY"),
    (3467, "10:46", False, 1322, "DUE"),
    (3468, "23:47", False, 1339, "FUTURE"),
    (3469, "12:48", True, 1356, "DONE_TODAY"),
    (3470, "01:49", False, 1373, "DUE"),
    (3471, "14:50", False, 1390, "DUE"),
    (3472, "03:51", True, 1407, "DONE_TODAY"),
    (3473, "16:52", False, 1424, "DUE"),
    (3474, "05:53", False, 1, "FUTURE"),
    (3475, "18:54", True, 18, "DONE_TODAY"),
    (3476, "07:55", False, 35, "FUTURE"),
    (3477, "20:56", False, 52, "FUTURE"),
    (3478, "09:57", True, 69, "DONE_TODAY"),
    (3479, "22:58", False, 86, "FUTURE"),
    (3480, "11:59", False, 103, "FUTURE"),
    (3481, "00:00", True, 120, "DONE_TODAY"),
    (3482, "13:01", False, 137, "FUTURE"),
    (3483, "02:02", False, 154, "DUE"),
    (3484, "15:03", True, 171, "DONE_TODAY"),
    (3485, "04:04", False, 188, "FUTURE"),
    (3486, "17:05", False, 205, "FUTURE"),
    (3487, "06:06", True, 222, "DONE_TODAY"),
    (3488, "19:07", False, 239, "FUTURE"),
    (3489, "08:08", False, 256, "FUTURE"),
    (3490, "21:09", True, 273, "DONE_TODAY"),
    (3491, "10:10", False, 290, "FUTURE"),
    (3492, "23:11", False, 307, "FUTURE"),
    (3493, "12:12", True, 324, "DONE_TODAY"),
    (3494, "01:13", False, 341, "DUE"),
    (3495, "14:14", False, 358, "FUTURE"),
    (3496, "03:15", True, 375, "DONE_TODAY"),
    (3497, "16:16", False, 392, "FUTURE"),
    (3498, "05:17", False, 409, "DUE"),
    (3499, "18:18", True, 426, "DONE_TODAY"),
    (3500, "07:19", False, 443, "DUE"),
    (3501, "20:20", False, 460, "FUTURE"),
    (3502, "09:21", True, 477, "DONE_TODAY"),
    (3503, "22:22", False, 494, "FUTURE"),
    (3504, "11:23", False, 511, "FUTURE"),
    (3505, "00:24", True, 528, "DONE_TODAY"),
    (3506, "13:25", False, 545, "FUTURE"),
    (3507, "02:26", False, 562, "DUE"),
    (3508, "15:27", True, 579, "DONE_TODAY"),
    (3509, "04:28", False, 596, "DUE"),
    (3510, "17:29", False, 613, "FUTURE"),
    (3511, "06:30", True, 630, "DONE_TODAY"),
    (3512, "19:31", False, 647, "FUTURE"),
    (3513, "08:32", False, 664, "DUE"),
    (3514, "21:33", True, 681, "DONE_TODAY"),
    (3515, "10:34", False, 698, "DUE"),
    (3516, "23:35", False, 715, "FUTURE"),
    (3517, "12:36", True, 732, "DONE_TODAY"),
    (3518, "01:37", False, 749, "DUE"),
    (3519, "14:38", False, 766, "FUTURE"),
    (3520, "03:39", True, 783, "DONE_TODAY"),
    (3521, "16:40", False, 800, "FUTURE"),
    (3522, "05:41", False, 817, "DUE"),
    (3523, "18:42", True, 834, "DONE_TODAY"),
    (3524, "07:43", False, 851, "DUE"),
    (3525, "20:44", False, 868, "FUTURE"),
    (3526, "09:45", True, 885, "DONE_TODAY"),
    (3527, "22:46", False, 902, "FUTURE"),
    (3528, "11:47", False, 919, "DUE"),
    (3529, "00:48", True, 936, "DONE_TODAY"),
    (3530, "13:49", False, 953, "DUE"),
    (3531, "02:50", False, 970, "DUE"),
    (3532, "15:51", True, 987, "DONE_TODAY"),
    (3533, "04:52", False, 1004, "DUE"),
    (3534, "17:53", False, 1021, "FUTURE"),
    (3535, "06:54", True, 1038, "DONE_TODAY"),
    (3536, "19:55", False, 1055, "FUTURE"),
    (3537, "08:56", False, 1072, "DUE"),
    (3538, "21:57", True, 1089, "DONE_TODAY"),
    (3539, "10:58", False, 1106, "DUE"),
    (3540, "23:59", False, 1123, "FUTURE"),
    (3541, "12:00", True, 1140, "DONE_TODAY"),
    (3542, "01:01", False, 1157, "DUE"),
    (3543, "14:02", False, 1174, "DUE"),
    (3544, "03:03", True, 1191, "DONE_TODAY"),
    (3545, "16:04", False, 1208, "DUE"),
    (3546, "05:05", False, 1225, "DUE"),
    (3547, "18:06", True, 1242, "DONE_TODAY"),
    (3548, "07:07", False, 1259, "DUE"),
    (3549, "20:08", False, 1276, "DUE"),
    (3550, "09:09", True, 1293, "DONE_TODAY"),
    (3551, "22:10", False, 1310, "FUTURE"),
    (3552, "11:11", False, 1327, "DUE"),
    (3553, "00:12", True, 1344, "DONE_TODAY"),
    (3554, "13:13", False, 1361, "DUE"),
    (3555, "02:14", False, 1378, "DUE"),
    (3556, "15:15", True, 1395, "DONE_TODAY"),
    (3557, "04:16", False, 1412, "DUE"),
    (3558, "17:17", False, 1429, "DUE"),
    (3559, "06:18", True, 6, "DONE_TODAY"),
    (3560, "19:19", False, 23, "FUTURE"),
    (3561, "08:20", False, 40, "FUTURE"),
    (3562, "21:21", True, 57, "DONE_TODAY"),
    (3563, "10:22", False, 74, "FUTURE"),
    (3564, "23:23", False, 91, "FUTURE"),
    (3565, "12:24", True, 108, "DONE_TODAY"),
    (3566, "01:25", False, 125, "DUE"),
    (3567, "14:26", False, 142, "FUTURE"),
    (3568, "03:27", True, 159, "DONE_TODAY"),
    (3569, "16:28", False, 176, "FUTURE"),
    (3570, "05:29", False, 193, "FUTURE"),
    (3571, "18:30", True, 210, "DONE_TODAY"),
    (3572, "07:31", False, 227, "FUTURE"),
    (3573, "20:32", False, 244, "FUTURE"),
    (3574, "09:33", True, 261, "DONE_TODAY"),
    (3575, "22:34", False, 278, "FUTURE"),
    (3576, "11:35", False, 295, "FUTURE"),
    (3577, "00:36", True, 312, "DONE_TODAY"),
    (3578, "13:37", False, 329, "FUTURE"),
    (3579, "02:38", False, 346, "DUE"),
    (3580, "15:39", True, 363, "DONE_TODAY"),
    (3581, "04:40", False, 380, "DUE"),
    (3582, "17:41", False, 397, "FUTURE"),
    (3583, "06:42", True, 414, "DONE_TODAY"),
    (3584, "19:43", False, 431, "FUTURE"),
    (3585, "08:44", False, 448, "FUTURE"),
    (3586, "21:45", True, 465, "DONE_TODAY"),
    (3587, "10:46", False, 482, "FUTURE"),
    (3588, "23:47", False, 499, "FUTURE"),
    (3589, "12:48", True, 516, "DONE_TODAY"),
    (3590, "01:49", False, 533, "DUE"),
    (3591, "14:50", False, 550, "FUTURE"),
    (3592, "03:51", True, 567, "DONE_TODAY"),
    (3593, "16:52", False, 584, "FUTURE"),
    (3594, "05:53", False, 601, "DUE"),
    (3595, "18:54", True, 618, "DONE_TODAY"),
    (3596, "07:55", False, 635, "DUE"),
    (3597, "20:56", False, 652, "FUTURE"),
    (3598, "09:57", True, 669, "DONE_TODAY"),
    (3599, "22:58", False, 686, "FUTURE"),
    (3600, "11:59", False, 703, "FUTURE"),
    (3601, "00:00", True, 720, "DONE_TODAY"),
    (3602, "13:01", False, 737, "FUTURE"),
    (3603, "02:02", False, 754, "DUE"),
    (3604, "15:03", True, 771, "DONE_TODAY"),
    (3605, "04:04", False, 788, "DUE"),
    (3606, "17:05", False, 805, "FUTURE"),
    (3607, "06:06", True, 822, "DONE_TODAY"),
    (3608, "19:07", False, 839, "FUTURE"),
    (3609, "08:08", False, 856, "DUE"),
    (3610, "21:09", True, 873, "DONE_TODAY"),
    (3611, "10:10", False, 890, "DUE"),
    (3612, "23:11", False, 907, "FUTURE"),
    (3613, "12:12", True, 924, "DONE_TODAY"),
    (3614, "01:13", False, 941, "DUE"),
    (3615, "14:14", False, 958, "DUE"),
    (3616, "03:15", True, 975, "DONE_TODAY"),
    (3617, "16:16", False, 992, "DUE"),
    (3618, "05:17", False, 1009, "DUE"),
    (3619, "18:18", True, 1026, "DONE_TODAY"),
    (3620, "07:19", False, 1043, "DUE"),
    (3621, "20:20", False, 1060, "FUTURE"),
    (3622, "09:21", True, 1077, "DONE_TODAY"),
    (3623, "22:22", False, 1094, "FUTURE"),
    (3624, "11:23", False, 1111, "DUE"),
    (3625, "00:24", True, 1128, "DONE_TODAY"),
    (3626, "13:25", False, 1145, "DUE"),
    (3627, "02:26", False, 1162, "DUE"),
    (3628, "15:27", True, 1179, "DONE_TODAY"),
    (3629, "04:28", False, 1196, "DUE"),
    (3630, "17:29", False, 1213, "DUE"),
    (3631, "06:30", True, 1230, "DONE_TODAY"),
    (3632, "19:31", False, 1247, "DUE"),
    (3633, "08:32", False, 1264, "DUE"),
    (3634, "21:33", True, 1281, "DONE_TODAY"),
    (3635, "10:34", False, 1298, "DUE"),
    (3636, "23:35", False, 1315, "FUTURE"),
    (3637, "12:36", True, 1332, "DONE_TODAY"),
    (3638, "01:37", False, 1349, "DUE"),
    (3639, "14:38", False, 1366, "DUE"),
    (3640, "03:39", True, 1383, "DONE_TODAY"),
    (3641, "16:40", False, 1400, "DUE"),
    (3642, "05:41", False, 1417, "DUE"),
    (3643, "18:42", True, 1434, "DONE_TODAY"),
    (3644, "07:43", False, 11, "FUTURE"),
    (3645, "20:44", False, 28, "FUTURE"),
    (3646, "09:45", True, 45, "DONE_TODAY"),
    (3647, "22:46", False, 62, "FUTURE"),
    (3648, "11:47", False, 79, "FUTURE"),
    (3649, "00:48", True, 96, "DONE_TODAY"),
    (3650, "13:49", False, 113, "FUTURE"),
    (3651, "02:50", False, 130, "FUTURE"),
    (3652, "15:51", True, 147, "DONE_TODAY"),
    (3653, "04:52", False, 164, "FUTURE"),
    (3654, "17:53", False, 181, "FUTURE"),
    (3655, "06:54", True, 198, "DONE_TODAY"),
    (3656, "19:55", False, 215, "FUTURE"),
    (3657, "08:56", False, 232, "FUTURE"),
    (3658, "21:57", True, 249, "DONE_TODAY"),
    (3659, "10:58", False, 266, "FUTURE"),
    (3660, "23:59", False, 283, "FUTURE"),
    (3661, "12:00", True, 300, "DONE_TODAY"),
    (3662, "01:01", False, 317, "DUE"),
    (3663, "14:02", False, 334, "FUTURE"),
    (3664, "03:03", True, 351, "DONE_TODAY"),
    (3665, "16:04", False, 368, "FUTURE"),
    (3666, "05:05", False, 385, "DUE"),
    (3667, "18:06", True, 402, "DONE_TODAY"),
    (3668, "07:07", False, 419, "FUTURE"),
    (3669, "20:08", False, 436, "FUTURE"),
    (3670, "09:09", True, 453, "DONE_TODAY"),
    (3671, "22:10", False, 470, "FUTURE"),
    (3672, "11:11", False, 487, "FUTURE"),
    (3673, "00:12", True, 504, "DONE_TODAY"),
    (3674, "13:13", False, 521, "FUTURE"),
    (3675, "02:14", False, 538, "DUE"),
    (3676, "15:15", True, 555, "DONE_TODAY"),
    (3677, "04:16", False, 572, "DUE"),
    (3678, "17:17", False, 589, "FUTURE"),
    (3679, "06:18", True, 606, "DONE_TODAY"),
    (3680, "19:19", False, 623, "FUTURE"),
    (3681, "08:20", False, 640, "DUE"),
    (3682, "21:21", True, 657, "DONE_TODAY"),
    (3683, "10:22", False, 674, "DUE"),
    (3684, "23:23", False, 691, "FUTURE"),
    (3685, "12:24", True, 708, "DONE_TODAY"),
    (3686, "01:25", False, 725, "DUE"),
    (3687, "14:26", False, 742, "FUTURE"),
    (3688, "03:27", True, 759, "DONE_TODAY"),
    (3689, "16:28", False, 776, "FUTURE"),
    (3690, "05:29", False, 793, "DUE"),
    (3691, "18:30", True, 810, "DONE_TODAY"),
    (3692, "07:31", False, 827, "DUE"),
    (3693, "20:32", False, 844, "FUTURE"),
    (3694, "09:33", True, 861, "DONE_TODAY"),
    (3695, "22:34", False, 878, "FUTURE"),
    (3696, "11:35", False, 895, "DUE"),
    (3697, "00:36", True, 912, "DONE_TODAY"),
    (3698, "13:37", False, 929, "DUE"),
    (3699, "02:38", False, 946, "DUE"),
    (3700, "15:39", True, 963, "DONE_TODAY"),
    (3701, "04:40", False, 980, "DUE"),
    (3702, "17:41", False, 997, "FUTURE"),
    (3703, "06:42", True, 1014, "DONE_TODAY"),
    (3704, "19:43", False, 1031, "FUTURE"),
    (3705, "08:44", False, 1048, "DUE"),
    (3706, "21:45", True, 1065, "DONE_TODAY"),
    (3707, "10:46", False, 1082, "DUE"),
    (3708, "23:47", False, 1099, "FUTURE"),
    (3709, "12:48", True, 1116, "DONE_TODAY"),
    (3710, "01:49", False, 1133, "DUE"),
    (3711, "14:50", False, 1150, "DUE"),
    (3712, "03:51", True, 1167, "DONE_TODAY"),
    (3713, "16:52", False, 1184, "DUE"),
    (3714, "05:53", False, 1201, "DUE"),
    (3715, "18:54", True, 1218, "DONE_TODAY"),
    (3716, "07:55", False, 1235, "DUE"),
    (3717, "20:56", False, 1252, "FUTURE"),
    (3718, "09:57", True, 1269, "DONE_TODAY"),
    (3719, "22:58", False, 1286, "FUTURE"),
    (3720, "11:59", False, 1303, "DUE"),
    (3721, "00:00", True, 1320, "DONE_TODAY"),
    (3722, "13:01", False, 1337, "DUE"),
    (3723, "02:02", False, 1354, "DUE"),
    (3724, "15:03", True, 1371, "DONE_TODAY"),
    (3725, "04:04", False, 1388, "DUE"),
    (3726, "17:05", False, 1405, "DUE"),
    (3727, "06:06", True, 1422, "DONE_TODAY"),
    (3728, "19:07", False, 1439, "DUE"),
    (3729, "08:08", False, 16, "FUTURE"),
    (3730, "21:09", True, 33, "DONE_TODAY"),
    (3731, "10:10", False, 50, "FUTURE"),
    (3732, "23:11", False, 67, "FUTURE"),
    (3733, "12:12", True, 84, "DONE_TODAY"),
    (3734, "01:13", False, 101, "DUE"),
    (3735, "14:14", False, 118, "FUTURE"),
    (3736, "03:15", True, 135, "DONE_TODAY"),
    (3737, "16:16", False, 152, "FUTURE"),
    (3738, "05:17", False, 169, "FUTURE"),
    (3739, "18:18", True, 186, "DONE_TODAY"),
    (3740, "07:19", False, 203, "FUTURE"),
    (3741, "20:20", False, 220, "FUTURE"),
    (3742, "09:21", True, 237, "DONE_TODAY"),
    (3743, "22:22", False, 254, "FUTURE"),
    (3744, "11:23", False, 271, "FUTURE"),
    (3745, "00:24", True, 288, "DONE_TODAY"),
    (3746, "13:25", False, 305, "FUTURE"),
    (3747, "02:26", False, 322, "DUE"),
    (3748, "15:27", True, 339, "DONE_TODAY"),
    (3749, "04:28", False, 356, "DUE"),
    (3750, "17:29", False, 373, "FUTURE"),
    (3751, "06:30", True, 390, "DONE_TODAY"),
    (3752, "19:31", False, 407, "FUTURE"),
    (3753, "08:32", False, 424, "FUTURE"),
    (3754, "21:33", True, 441, "DONE_TODAY"),
    (3755, "10:34", False, 458, "FUTURE"),
    (3756, "23:35", False, 475, "FUTURE"),
    (3757, "12:36", True, 492, "DONE_TODAY"),
    (3758, "01:37", False, 509, "DUE"),
    (3759, "14:38", False, 526, "FUTURE"),
    (3760, "03:39", True, 543, "DONE_TODAY"),
    (3761, "16:40", False, 560, "FUTURE"),
    (3762, "05:41", False, 577, "DUE"),
    (3763, "18:42", True, 594, "DONE_TODAY"),
    (3764, "07:43", False, 611, "DUE"),
    (3765, "20:44", False, 628, "FUTURE"),
    (3766, "09:45", True, 645, "DONE_TODAY"),
    (3767, "22:46", False, 662, "FUTURE"),
    (3768, "11:47", False, 679, "FUTURE"),
    (3769, "00:48", True, 696, "DONE_TODAY"),
    (3770, "13:49", False, 713, "FUTURE"),
    (3771, "02:50", False, 730, "DUE"),
    (3772, "15:51", True, 747, "DONE_TODAY"),
    (3773, "04:52", False, 764, "DUE"),
    (3774, "17:53", False, 781, "FUTURE"),
    (3775, "06:54", True, 798, "DONE_TODAY"),
    (3776, "19:55", False, 815, "FUTURE"),
    (3777, "08:56", False, 832, "DUE"),
    (3778, "21:57", True, 849, "DONE_TODAY"),
    (3779, "10:58", False, 866, "DUE"),
    (3780, "23:59", False, 883, "FUTURE"),
    (3781, "12:00", True, 900, "DONE_TODAY"),
    (3782, "01:01", False, 917, "DUE"),
    (3783, "14:02", False, 934, "DUE"),
    (3784, "03:03", True, 951, "DONE_TODAY"),
    (3785, "16:04", False, 968, "DUE"),
    (3786, "05:05", False, 985, "DUE"),
    (3787, "18:06", True, 1002, "DONE_TODAY"),
    (3788, "07:07", False, 1019, "DUE"),
    (3789, "20:08", False, 1036, "FUTURE"),
    (3790, "09:09", True, 1053, "DONE_TODAY"),
    (3791, "22:10", False, 1070, "FUTURE"),
    (3792, "11:11", False, 1087, "DUE"),
    (3793, "00:12", True, 1104, "DONE_TODAY"),
    (3794, "13:13", False, 1121, "DUE"),
    (3795, "02:14", False, 1138, "DUE"),
    (3796, "15:15", True, 1155, "DONE_TODAY"),
    (3797, "04:16", False, 1172, "DUE"),
    (3798, "17:17", False, 1189, "DUE"),
    (3799, "06:18", True, 1206, "DONE_TODAY"),
    (3800, "19:19", False, 1223, "DUE"),
    (3801, "08:20", False, 1240, "DUE"),
    (3802, "21:21", True, 1257, "DONE_TODAY"),
    (3803, "10:22", False, 1274, "DUE"),
    (3804, "23:23", False, 1291, "FUTURE"),
    (3805, "12:24", True, 1308, "DONE_TODAY"),
    (3806, "01:25", False, 1325, "DUE"),
    (3807, "14:26", False, 1342, "DUE"),
    (3808, "03:27", True, 1359, "DONE_TODAY"),
    (3809, "16:28", False, 1376, "DUE"),
    (3810, "05:29", False, 1393, "DUE"),
    (3811, "18:30", True, 1410, "DONE_TODAY"),
    (3812, "07:31", False, 1427, "DUE"),
    (3813, "20:32", False, 4, "FUTURE"),
    (3814, "09:33", True, 21, "DONE_TODAY"),
    (3815, "22:34", False, 38, "FUTURE"),
    (3816, "11:35", False, 55, "FUTURE"),
    (3817, "00:36", True, 72, "DONE_TODAY"),
    (3818, "13:37", False, 89, "FUTURE"),
    (3819, "02:38", False, 106, "FUTURE"),
    (3820, "15:39", True, 123, "DONE_TODAY"),
    (3821, "04:40", False, 140, "FUTURE"),
    (3822, "17:41", False, 157, "FUTURE"),
    (3823, "06:42", True, 174, "DONE_TODAY"),
    (3824, "19:43", False, 191, "FUTURE"),
    (3825, "08:44", False, 208, "FUTURE"),
    (3826, "21:45", True, 225, "DONE_TODAY"),
    (3827, "10:46", False, 242, "FUTURE"),
    (3828, "23:47", False, 259, "FUTURE"),
    (3829, "12:48", True, 276, "DONE_TODAY"),
    (3830, "01:49", False, 293, "DUE"),
    (3831, "14:50", False, 310, "FUTURE"),
    (3832, "03:51", True, 327, "DONE_TODAY"),
    (3833, "16:52", False, 344, "FUTURE"),
    (3834, "05:53", False, 361, "DUE"),
    (3835, "18:54", True, 378, "DONE_TODAY"),
    (3836, "07:55", False, 395, "FUTURE"),
    (3837, "20:56", False, 412, "FUTURE"),
    (3838, "09:57", True, 429, "DONE_TODAY"),
    (3839, "22:58", False, 446, "FUTURE"),
    (3840, "11:59", False, 463, "FUTURE"),
    (3841, "00:00", True, 480, "DONE_TODAY"),
    (3842, "13:01", False, 497, "FUTURE"),
    (3843, "02:02", False, 514, "DUE"),
    (3844, "15:03", True, 531, "DONE_TODAY"),
    (3845, "04:04", False, 548, "DUE"),
    (3846, "17:05", False, 565, "FUTURE"),
    (3847, "06:06", True, 582, "DONE_TODAY"),
    (3848, "19:07", False, 599, "FUTURE"),
    (3849, "08:08", False, 616, "DUE"),
    (3850, "21:09", True, 633, "DONE_TODAY"),
    (3851, "10:10", False, 650, "DUE"),
    (3852, "23:11", False, 667, "FUTURE"),
    (3853, "12:12", True, 684, "DONE_TODAY"),
    (3854, "01:13", False, 701, "DUE"),
    (3855, "14:14", False, 718, "FUTURE"),
    (3856, "03:15", True, 735, "DONE_TODAY"),
    (3857, "16:16", False, 752, "FUTURE"),
    (3858, "05:17", False, 769, "DUE"),
    (3859, "18:18", True, 786, "DONE_TODAY"),
    (3860, "07:19", False, 803, "DUE"),
    (3861, "20:20", False, 820, "FUTURE"),
    (3862, "09:21", True, 837, "DONE_TODAY"),
    (3863, "22:22", False, 854, "FUTURE"),
    (3864, "11:23", False, 871, "DUE"),
    (3865, "00:24", True, 888, "DONE_TODAY"),
    (3866, "13:25", False, 905, "DUE"),
    (3867, "02:26", False, 922, "DUE"),
    (3868, "15:27", True, 939, "DONE_TODAY"),
    (3869, "04:28", False, 956, "DUE"),
    (3870, "17:29", False, 973, "FUTURE"),
    (3871, "06:30", True, 990, "DONE_TODAY"),
    (3872, "19:31", False, 1007, "FUTURE"),
    (3873, "08:32", False, 1024, "DUE"),
    (3874, "21:33", True, 1041, "DONE_TODAY"),
    (3875, "10:34", False, 1058, "DUE"),
    (3876, "23:35", False, 1075, "FUTURE"),
    (3877, "12:36", True, 1092, "DONE_TODAY"),
    (3878, "01:37", False, 1109, "DUE"),
    (3879, "14:38", False, 1126, "DUE"),
    (3880, "03:39", True, 1143, "DONE_TODAY"),
    (3881, "16:40", False, 1160, "DUE"),
    (3882, "05:41", False, 1177, "DUE"),
    (3883, "18:42", True, 1194, "DONE_TODAY"),
    (3884, "07:43", False, 1211, "DUE"),
    (3885, "20:44", False, 1228, "FUTURE"),
    (3886, "09:45", True, 1245, "DONE_TODAY"),
    (3887, "22:46", False, 1262, "FUTURE"),
    (3888, "11:47", False, 1279, "DUE"),
    (3889, "00:48", True, 1296, "DONE_TODAY"),
    (3890, "13:49", False, 1313, "DUE"),
    (3891, "02:50", False, 1330, "DUE"),
    (3892, "15:51", True, 1347, "DONE_TODAY"),
    (3893, "04:52", False, 1364, "DUE"),
    (3894, "17:53", False, 1381, "DUE"),
    (3895, "06:54", True, 1398, "DONE_TODAY"),
    (3896, "19:55", False, 1415, "DUE"),
    (3897, "08:56", False, 1432, "DUE"),
    (3898, "21:57", True, 9, "DONE_TODAY"),
    (3899, "10:58", False, 26, "FUTURE"),
    (3900, "23:59", False, 43, "FUTURE"),
    (3901, "12:00", True, 60, "DONE_TODAY"),
    (3902, "01:01", False, 77, "DUE"),
    (3903, "14:02", False, 94, "FUTURE"),
    (3904, "03:03", True, 111, "DONE_TODAY"),
    (3905, "16:04", False, 128, "FUTURE"),
    (3906, "05:05", False, 145, "FUTURE"),
    (3907, "18:06", True, 162, "DONE_TODAY"),
    (3908, "07:07", False, 179, "FUTURE"),
    (3909, "20:08", False, 196, "FUTURE"),
    (3910, "09:09", True, 213, "DONE_TODAY"),
    (3911, "22:10", False, 230, "FUTURE"),
    (3912, "11:11", False, 247, "FUTURE"),
    (3913, "00:12", True, 264, "DONE_TODAY"),
    (3914, "13:13", False, 281, "FUTURE"),
    (3915, "02:14", False, 298, "DUE"),
    (3916, "15:15", True, 315, "DONE_TODAY"),
    (3917, "04:16", False, 332, "DUE"),
    (3918, "17:17", False, 349, "FUTURE"),
    (3919, "06:18", True, 366, "DONE_TODAY"),
    (3920, "19:19", False, 383, "FUTURE"),
    (3921, "08:20", False, 400, "FUTURE"),
    (3922, "21:21", True, 417, "DONE_TODAY"),
    (3923, "10:22", False, 434, "FUTURE"),
    (3924, "23:23", False, 451, "FUTURE"),
    (3925, "12:24", True, 468, "DONE_TODAY"),
    (3926, "01:25", False, 485, "DUE"),
    (3927, "14:26", False, 502, "FUTURE"),
    (3928, "03:27", True, 519, "DONE_TODAY"),
    (3929, "16:28", False, 536, "FUTURE"),
    (3930, "05:29", False, 553, "DUE"),
    (3931, "18:30", True, 570, "DONE_TODAY"),
    (3932, "07:31", False, 587, "DUE"),
    (3933, "20:32", False, 604, "FUTURE"),
    (3934, "09:33", True, 621, "DONE_TODAY"),
    (3935, "22:34", False, 638, "FUTURE"),
    (3936, "11:35", False, 655, "FUTURE"),
    (3937, "00:36", True, 672, "DONE_TODAY"),
    (3938, "13:37", False, 689, "FUTURE"),
    (3939, "02:38", False, 706, "DUE"),
    (3940, "15:39", True, 723, "DONE_TODAY"),
    (3941, "04:40", False, 740, "DUE"),
    (3942, "17:41", False, 757, "FUTURE"),
    (3943, "06:42", True, 774, "DONE_TODAY"),
    (3944, "19:43", False, 791, "FUTURE"),
    (3945, "08:44", False, 808, "DUE"),
    (3946, "21:45", True, 825, "DONE_TODAY"),
    (3947, "10:46", False, 842, "DUE"),
    (3948, "23:47", False, 859, "FUTURE"),
    (3949, "12:48", True, 876, "DONE_TODAY"),
    (3950, "01:49", False, 893, "DUE"),
    (3951, "14:50", False, 910, "DUE"),
    (3952, "03:51", True, 927, "DONE_TODAY"),
    (3953, "16:52", False, 944, "FUTURE"),
    (3954, "05:53", False, 961, "DUE"),
    (3955, "18:54", True, 978, "DONE_TODAY"),
    (3956, "07:55", False, 995, "DUE"),
    (3957, "20:56", False, 1012, "FUTURE"),
    (3958, "09:57", True, 1029, "DONE_TODAY"),
    (3959, "22:58", False, 1046, "FUTURE"),
    (3960, "11:59", False, 1063, "DUE"),
    (3961, "00:00", True, 1080, "DONE_TODAY"),
    (3962, "13:01", False, 1097, "DUE"),
    (3963, "02:02", False, 1114, "DUE"),
    (3964, "15:03", True, 1131, "DONE_TODAY"),
    (3965, "04:04", False, 1148, "DUE"),
    (3966, "17:05", False, 1165, "DUE"),
    (3967, "06:06", True, 1182, "DONE_TODAY"),
    (3968, "19:07", False, 1199, "DUE"),
    (3969, "08:08", False, 1216, "DUE"),
    (3970, "21:09", True, 1233, "DONE_TODAY"),
    (3971, "10:10", False, 1250, "DUE"),
    (3972, "23:11", False, 1267, "FUTURE"),
    (3973, "12:12", True, 1284, "DONE_TODAY"),
    (3974, "01:13", False, 1301, "DUE"),
    (3975, "14:14", False, 1318, "DUE"),
    (3976, "03:15", True, 1335, "DONE_TODAY"),
    (3977, "16:16", False, 1352, "DUE"),
    (3978, "05:17", False, 1369, "DUE"),
    (3979, "18:18", True, 1386, "DONE_TODAY"),
    (3980, "07:19", False, 1403, "DUE"),
    (3981, "20:20", False, 1420, "DUE"),
    (3982, "09:21", True, 1437, "DONE_TODAY"),
    (3983, "22:22", False, 14, "FUTURE"),
    (3984, "11:23", False, 31, "FUTURE"),
    (3985, "00:24", True, 48, "DONE_TODAY"),
    (3986, "13:25", False, 65, "FUTURE"),
    (3987, "02:26", False, 82, "FUTURE"),
    (3988, "15:27", True, 99, "DONE_TODAY"),
    (3989, "04:28", False, 116, "FUTURE"),
    (3990, "17:29", False, 133, "FUTURE"),
    (3991, "06:30", True, 150, "DONE_TODAY"),
    (3992, "19:31", False, 167, "FUTURE"),
    (3993, "08:32", False, 184, "FUTURE"),
    (3994, "21:33", True, 201, "DONE_TODAY"),
    (3995, "10:34", False, 218, "FUTURE"),
    (3996, "23:35", False, 235, "FUTURE"),
    (3997, "12:36", True, 252, "DONE_TODAY"),
    (3998, "01:37", False, 269, "DUE"),
    (3999, "14:38", False, 286, "FUTURE"),
    (4000, "03:39", True, 303, "DONE_TODAY"),
    (4001, "16:40", False, 320, "FUTURE"),
    (4002, "05:41", False, 337, "FUTURE"),
    (4003, "18:42", True, 354, "DONE_TODAY"),
    (4004, "07:43", False, 371, "FUTURE"),
    (4005, "20:44", False, 388, "FUTURE"),
    (4006, "09:45", True, 405, "DONE_TODAY"),
    (4007, "22:46", False, 422, "FUTURE"),
    (4008, "11:47", False, 439, "FUTURE"),
    (4009, "00:48", True, 456, "DONE_TODAY"),
    (4010, "13:49", False, 473, "FUTURE"),
    (4011, "02:50", False, 490, "DUE"),
    (4012, "15:51", True, 507, "DONE_TODAY"),
    (4013, "04:52", False, 524, "DUE"),
    (4014, "17:53", False, 541, "FUTURE"),
    (4015, "06:54", True, 558, "DONE_TODAY"),
    (4016, "19:55", False, 575, "FUTURE"),
    (4017, "08:56", False, 592, "DUE"),
    (4018, "21:57", True, 609, "DONE_TODAY"),
    (4019, "10:58", False, 626, "FUTURE"),
    (4020, "23:59", False, 643, "FUTURE"),
    (4021, "12:00", True, 660, "DONE_TODAY"),
    (4022, "01:01", False, 677, "DUE"),
    (4023, "14:02", False, 694, "FUTURE"),
    (4024, "03:03", True, 711, "DONE_TODAY"),
    (4025, "16:04", False, 728, "FUTURE"),
    (4026, "05:05", False, 745, "DUE"),
    (4027, "18:06", True, 762, "DONE_TODAY"),
    (4028, "07:07", False, 779, "DUE"),
    (4029, "20:08", False, 796, "FUTURE"),
    (4030, "09:09", True, 813, "DONE_TODAY"),
    (4031, "22:10", False, 830, "FUTURE"),
    (4032, "11:11", False, 847, "DUE"),
    (4033, "00:12", True, 864, "DONE_TODAY"),
    (4034, "13:13", False, 881, "DUE"),
    (4035, "02:14", False, 898, "DUE"),
    (4036, "15:15", True, 915, "DONE_TODAY"),
    (4037, "04:16", False, 932, "DUE"),
    (4038, "17:17", False, 949, "FUTURE"),
    (4039, "06:18", True, 966, "DONE_TODAY"),
    (4040, "19:19", False, 983, "FUTURE"),
    (4041, "08:20", False, 1000, "DUE"),
    (4042, "21:21", True, 1017, "DONE_TODAY"),
    (4043, "10:22", False, 1034, "DUE"),
    (4044, "23:23", False, 1051, "FUTURE"),
    (4045, "12:24", True, 1068, "DONE_TODAY"),
    (4046, "01:25", False, 1085, "DUE"),
    (4047, "14:26", False, 1102, "DUE"),
    (4048, "03:27", True, 1119, "DONE_TODAY"),
    (4049, "16:28", False, 1136, "DUE"),
    (4050, "05:29", False, 1153, "DUE"),
    (4051, "18:30", True, 1170, "DONE_TODAY"),
    (4052, "07:31", False, 1187, "DUE"),
    (4053, "20:32", False, 1204, "FUTURE"),
    (4054, "09:33", True, 1221, "DONE_TODAY"),
    (4055, "22:34", False, 1238, "FUTURE"),
    (4056, "11:35", False, 1255, "DUE"),
    (4057, "00:36", True, 1272, "DONE_TODAY"),
    (4058, "13:37", False, 1289, "DUE"),
    (4059, "02:38", False, 1306, "DUE"),
    (4060, "15:39", True, 1323, "DONE_TODAY"),
    (4061, "04:40", False, 1340, "DUE"),
    (4062, "17:41", False, 1357, "DUE"),
    (4063, "06:42", True, 1374, "DONE_TODAY"),
    (4064, "19:43", False, 1391, "DUE"),
    (4065, "08:44", False, 1408, "DUE"),
    (4066, "21:45", True, 1425, "DONE_TODAY"),
    (4067, "10:46", False, 2, "FUTURE"),
    (4068, "23:47", False, 19, "FUTURE"),
    (4069, "12:48", True, 36, "DONE_TODAY"),
    (4070, "01:49", False, 53, "FUTURE"),
    (4071, "14:50", False, 70, "FUTURE"),
    (4072, "03:51", True, 87, "DONE_TODAY"),
    (4073, "16:52", False, 104, "FUTURE"),
    (4074, "05:53", False, 121, "FUTURE"),
    (4075, "18:54", True, 138, "DONE_TODAY"),
    (4076, "07:55", False, 155, "FUTURE"),
    (4077, "20:56", False, 172, "FUTURE"),
    (4078, "09:57", True, 189, "DONE_TODAY"),
    (4079, "22:58", False, 206, "FUTURE"),
    (4080, "11:59", False, 223, "FUTURE"),
    (4081, "00:00", True, 240, "DONE_TODAY"),
    (4082, "13:01", False, 257, "FUTURE"),
    (4083, "02:02", False, 274, "DUE"),
    (4084, "15:03", True, 291, "DONE_TODAY"),
    (4085, "04:04", False, 308, "DUE"),
    (4086, "17:05", False, 325, "FUTURE"),
    (4087, "06:06", True, 342, "DONE_TODAY"),
    (4088, "19:07", False, 359, "FUTURE"),
    (4089, "08:08", False, 376, "FUTURE"),
    (4090, "21:09", True, 393, "DONE_TODAY"),
    (4091, "10:10", False, 410, "FUTURE"),
    (4092, "23:11", False, 427, "FUTURE"),
    (4093, "12:12", True, 444, "DONE_TODAY"),
    (4094, "01:13", False, 461, "DUE"),
    (4095, "14:14", False, 478, "FUTURE"),
    (4096, "03:15", True, 495, "DONE_TODAY"),
    (4097, "16:16", False, 512, "FUTURE"),
    (4098, "05:17", False, 529, "DUE"),
    (4099, "18:18", True, 546, "DONE_TODAY"),
    (4100, "07:19", False, 563, "DUE"),
    (4101, "20:20", False, 580, "FUTURE"),
    (4102, "09:21", True, 597, "DONE_TODAY"),
    (4103, "22:22", False, 614, "FUTURE"),
    (4104, "11:23", False, 631, "FUTURE"),
    (4105, "00:24", True, 648, "DONE_TODAY"),
    (4106, "13:25", False, 665, "FUTURE"),
    (4107, "02:26", False, 682, "DUE"),
    (4108, "15:27", True, 699, "DONE_TODAY"),
    (4109, "04:28", False, 716, "DUE"),
    (4110, "17:29", False, 733, "FUTURE"),
    (4111, "06:30", True, 750, "DONE_TODAY"),
    (4112, "19:31", False, 767, "FUTURE"),
    (4113, "08:32", False, 784, "DUE"),
    (4114, "21:33", True, 801, "DONE_TODAY"),
    (4115, "10:34", False, 818, "DUE"),
    (4116, "23:35", False, 835, "FUTURE"),
    (4117, "12:36", True, 852, "DONE_TODAY"),
    (4118, "01:37", False, 869, "DUE"),
    (4119, "14:38", False, 886, "DUE"),
    (4120, "03:39", True, 903, "DONE_TODAY"),
    (4121, "16:40", False, 920, "FUTURE"),
    (4122, "05:41", False, 937, "DUE"),
    (4123, "18:42", True, 954, "DONE_TODAY"),
    (4124, "07:43", False, 971, "DUE"),
    (4125, "20:44", False, 988, "FUTURE"),
    (4126, "09:45", True, 1005, "DONE_TODAY"),
    (4127, "22:46", False, 1022, "FUTURE"),
    (4128, "11:47", False, 1039, "DUE"),
    (4129, "00:48", True, 1056, "DONE_TODAY"),
    (4130, "13:49", False, 1073, "DUE"),
    (4131, "02:50", False, 1090, "DUE"),
    (4132, "15:51", True, 1107, "DONE_TODAY"),
    (4133, "04:52", False, 1124, "DUE"),
    (4134, "17:53", False, 1141, "DUE"),
    (4135, "06:54", True, 1158, "DONE_TODAY"),
    (4136, "19:55", False, 1175, "FUTURE"),
    (4137, "08:56", False, 1192, "DUE"),
    (4138, "21:57", True, 1209, "DONE_TODAY"),
    (4139, "10:58", False, 1226, "DUE"),
    (4140, "23:59", False, 1243, "FUTURE"),
    (4141, "12:00", True, 1260, "DONE_TODAY"),
    (4142, "01:01", False, 1277, "DUE"),
    (4143, "14:02", False, 1294, "DUE"),
    (4144, "03:03", True, 1311, "DONE_TODAY"),
    (4145, "16:04", False, 1328, "DUE"),
    (4146, "05:05", False, 1345, "DUE"),
    (4147, "18:06", True, 1362, "DONE_TODAY"),
    (4148, "07:07", False, 1379, "DUE"),
    (4149, "20:08", False, 1396, "DUE"),
    (4150, "09:09", True, 1413, "DONE_TODAY"),
    (4151, "22:10", False, 1430, "DUE"),
    (4152, "11:11", False, 7, "FUTURE"),
    (4153, "00:12", True, 24, "DONE_TODAY"),
    (4154, "13:13", False, 41, "FUTURE"),
    (4155, "02:14", False, 58, "FUTURE"),
    (4156, "15:15", True, 75, "DONE_TODAY"),
    (4157, "04:16", False, 92, "FUTURE"),
    (4158, "17:17", False, 109, "FUTURE"),
    (4159, "06:18", True, 126, "DONE_TODAY"),
    (4160, "19:19", False, 143, "FUTURE"),
    (4161, "08:20", False, 160, "FUTURE"),
    (4162, "21:21", True, 177, "DONE_TODAY"),
    (4163, "10:22", False, 194, "FUTURE"),
    (4164, "23:23", False, 211, "FUTURE"),
    (4165, "12:24", True, 228, "DONE_TODAY"),
    (4166, "01:25", False, 245, "DUE"),
    (4167, "14:26", False, 262, "FUTURE"),
    (4168, "03:27", True, 279, "DONE_TODAY"),
    (4169, "16:28", False, 296, "FUTURE"),
    (4170, "05:29", False, 313, "FUTURE"),
    (4171, "18:30", True, 330, "DONE_TODAY"),
    (4172, "07:31", False, 347, "FUTURE"),
    (4173, "20:32", False, 364, "FUTURE"),
    (4174, "09:33", True, 381, "DONE_TODAY"),
    (4175, "22:34", False, 398, "FUTURE"),
    (4176, "11:35", False, 415, "FUTURE"),
    (4177, "00:36", True, 432, "DONE_TODAY"),
    (4178, "13:37", False, 449, "FUTURE"),
    (4179, "02:38", False, 466, "DUE"),
    (4180, "15:39", True, 483, "DONE_TODAY"),
    (4181, "04:40", False, 500, "DUE"),
    (4182, "17:41", False, 517, "FUTURE"),
    (4183, "06:42", True, 534, "DONE_TODAY"),
    (4184, "19:43", False, 551, "FUTURE"),
    (4185, "08:44", False, 568, "DUE"),
    (4186, "21:45", True, 585, "DONE_TODAY"),
    (4187, "10:46", False, 602, "FUTURE"),
    (4188, "23:47", False, 619, "FUTURE"),
    (4189, "12:48", True, 636, "DONE_TODAY"),
    (4190, "01:49", False, 653, "DUE"),
    (4191, "14:50", False, 670, "FUTURE"),
    (4192, "03:51", True, 687, "DONE_TODAY"),
    (4193, "16:52", False, 704, "FUTURE"),
    (4194, "05:53", False, 721, "DUE"),
    (4195, "18:54", True, 738, "DONE_TODAY"),
    (4196, "07:55", False, 755, "DUE"),
    (4197, "20:56", False, 772, "FUTURE"),
    (4198, "09:57", True, 789, "DONE_TODAY"),
    (4199, "22:58", False, 806, "FUTURE"),
    (4200, "11:59", False, 823, "DUE"),
    (4201, "00:00", True, 840, "DONE_TODAY"),
    (4202, "13:01", False, 857, "DUE"),
    (4203, "02:02", False, 874, "DUE"),
    (4204, "15:03", True, 891, "DONE_TODAY"),
    (4205, "04:04", False, 908, "DUE"),
    (4206, "17:05", False, 925, "FUTURE"),
    (4207, "06:06", True, 942, "DONE_TODAY"),
    (4208, "19:07", False, 959, "FUTURE"),
    (4209, "08:08", False, 976, "DUE"),
    (4210, "21:09", True, 993, "DONE_TODAY"),
    (4211, "10:10", False, 1010, "DUE"),
    (4212, "23:11", False, 1027, "FUTURE"),
    (4213, "12:12", True, 1044, "DONE_TODAY"),
    (4214, "01:13", False, 1061, "DUE"),
    (4215, "14:14", False, 1078, "DUE"),
    (4216, "03:15", True, 1095, "DONE_TODAY"),
    (4217, "16:16", False, 1112, "DUE"),
    (4218, "05:17", False, 1129, "DUE"),
    (4219, "18:18", True, 1146, "DONE_TODAY"),
    (4220, "07:19", False, 1163, "DUE"),
    (4221, "20:20", False, 1180, "FUTURE"),
    (4222, "09:21", True, 1197, "DONE_TODAY"),
    (4223, "22:22", False, 1214, "FUTURE"),
    (4224, "11:23", False, 1231, "DUE"),
    (4225, "00:24", True, 1248, "DONE_TODAY"),
    (4226, "13:25", False, 1265, "DUE"),
    (4227, "02:26", False, 1282, "DUE"),
    (4228, "15:27", True, 1299, "DONE_TODAY"),
    (4229, "04:28", False, 1316, "DUE"),
    (4230, "17:29", False, 1333, "DUE"),
    (4231, "06:30", True, 1350, "DONE_TODAY"),
    (4232, "19:31", False, 1367, "DUE"),
    (4233, "08:32", False, 1384, "DUE"),
    (4234, "21:33", True, 1401, "DONE_TODAY"),
    (4235, "10:34", False, 1418, "DUE"),
    (4236, "23:35", False, 1435, "DUE"),
    (4237, "12:36", True, 12, "DONE_TODAY"),
    (4238, "01:37", False, 29, "FUTURE"),
    (4239, "14:38", False, 46, "FUTURE"),
    (4240, "03:39", True, 63, "DONE_TODAY"),
    (4241, "16:40", False, 80, "FUTURE"),
    (4242, "05:41", False, 97, "FUTURE"),
    (4243, "18:42", True, 114, "DONE_TODAY"),
    (4244, "07:43", False, 131, "FUTURE"),
    (4245, "20:44", False, 148, "FUTURE"),
    (4246, "09:45", True, 165, "DONE_TODAY"),
    (4247, "22:46", False, 182, "FUTURE"),
    (4248, "11:47", False, 199, "FUTURE"),
    (4249, "00:48", True, 216, "DONE_TODAY"),
    (4250, "13:49", False, 233, "FUTURE"),
    (4251, "02:50", False, 250, "DUE"),
    (4252, "15:51", True, 267, "DONE_TODAY"),
    (4253, "04:52", False, 284, "FUTURE"),
    (4254, "17:53", False, 301, "FUTURE"),
    (4255, "06:54", True, 318, "DONE_TODAY"),
    (4256, "19:55", False, 335, "FUTURE"),
    (4257, "08:56", False, 352, "FUTURE"),
    (4258, "21:57", True, 369, "DONE_TODAY"),
    (4259, "10:58", False, 386, "FUTURE"),
    (4260, "23:59", False, 403, "FUTURE"),
    (4261, "12:00", True, 420, "DONE_TODAY"),
    (4262, "01:01", False, 437, "DUE"),
    (4263, "14:02", False, 454, "FUTURE"),
    (4264, "03:03", True, 471, "DONE_TODAY"),
    (4265, "16:04", False, 488, "FUTURE"),
    (4266, "05:05", False, 505, "DUE"),
    (4267, "18:06", True, 522, "DONE_TODAY"),
    (4268, "07:07", False, 539, "DUE"),
    (4269, "20:08", False, 556, "FUTURE"),
    (4270, "09:09", True, 573, "DONE_TODAY"),
    (4271, "22:10", False, 590, "FUTURE"),
    (4272, "11:11", False, 607, "FUTURE"),
    (4273, "00:12", True, 624, "DONE_TODAY"),
    (4274, "13:13", False, 641, "FUTURE"),
    (4275, "02:14", False, 658, "DUE"),
    (4276, "15:15", True, 675, "DONE_TODAY"),
    (4277, "04:16", False, 692, "DUE"),
    (4278, "17:17", False, 709, "FUTURE"),
    (4279, "06:18", True, 726, "DONE_TODAY"),
    (4280, "19:19", False, 743, "FUTURE"),
    (4281, "08:20", False, 760, "DUE"),
    (4282, "21:21", True, 777, "DONE_TODAY"),
    (4283, "10:22", False, 794, "DUE"),
    (4284, "23:23", False, 811, "FUTURE"),
    (4285, "12:24", True, 828, "DONE_TODAY"),
    (4286, "01:25", False, 845, "DUE"),
    (4287, "14:26", False, 862, "FUTURE"),
    (4288, "03:27", True, 879, "DONE_TODAY"),
    (4289, "16:28", False, 896, "FUTURE"),
    (4290, "05:29", False, 913, "DUE"),
    (4291, "18:30", True, 930, "DONE_TODAY"),
    (4292, "07:31", False, 947, "DUE"),
    (4293, "20:32", False, 964, "FUTURE"),
    (4294, "09:33", True, 981, "DONE_TODAY"),
    (4295, "22:34", False, 998, "FUTURE"),
    (4296, "11:35", False, 1015, "DUE"),
    (4297, "00:36", True, 1032, "DONE_TODAY"),
    (4298, "13:37", False, 1049, "DUE"),
    (4299, "02:38", False, 1066, "DUE"),
    (4300, "15:39", True, 1083, "DONE_TODAY"),
    (4301, "04:40", False, 1100, "DUE"),
    (4302, "17:41", False, 1117, "DUE"),
    (4303, "06:42", True, 1134, "DONE_TODAY"),
    (4304, "19:43", False, 1151, "FUTURE"),
    (4305, "08:44", False, 1168, "DUE"),
    (4306, "21:45", True, 1185, "DONE_TODAY"),
    (4307, "10:46", False, 1202, "DUE"),
    (4308, "23:47", False, 1219, "FUTURE"),
    (4309, "12:48", True, 1236, "DONE_TODAY"),
    (4310, "01:49", False, 1253, "DUE"),
    (4311, "14:50", False, 1270, "DUE"),
    (4312, "03:51", True, 1287, "DONE_TODAY"),
    (4313, "16:52", False, 1304, "DUE"),
    (4314, "05:53", False, 1321, "DUE"),
    (4315, "18:54", True, 1338, "DONE_TODAY"),
    (4316, "07:55", False, 1355, "DUE"),
    (4317, "20:56", False, 1372, "DUE"),
    (4318, "09:57", True, 1389, "DONE_TODAY"),
    (4319, "22:58", False, 1406, "DUE"),
    (4320, "11:59", False, 1423, "DUE"),
    (4321, "00:00", True, 0, "DONE_TODAY"),
    (4322, "13:01", False, 17, "FUTURE"),
    (4323, "02:02", False, 34, "FUTURE"),
    (4324, "15:03", True, 51, "DONE_TODAY"),
    (4325, "04:04", False, 68, "FUTURE"),
    (4326, "17:05", False, 85, "FUTURE"),
    (4327, "06:06", True, 102, "DONE_TODAY"),
    (4328, "19:07", False, 119, "FUTURE"),
    (4329, "08:08", False, 136, "FUTURE"),
    (4330, "21:09", True, 153, "DONE_TODAY"),
    (4331, "10:10", False, 170, "FUTURE"),
    (4332, "23:11", False, 187, "FUTURE"),
    (4333, "12:12", True, 204, "DONE_TODAY"),
    (4334, "01:13", False, 221, "DUE"),
    (4335, "14:14", False, 238, "FUTURE"),
    (4336, "03:15", True, 255, "DONE_TODAY"),
    (4337, "16:16", False, 272, "FUTURE"),
    (4338, "05:17", False, 289, "FUTURE"),
    (4339, "18:18", True, 306, "DONE_TODAY"),
    (4340, "07:19", False, 323, "FUTURE"),
    (4341, "20:20", False, 340, "FUTURE"),
    (4342, "09:21", True, 357, "DONE_TODAY"),
    (4343, "22:22", False, 374, "FUTURE"),
    (4344, "11:23", False, 391, "FUTURE"),
    (4345, "00:24", True, 408, "DONE_TODAY"),
    (4346, "13:25", False, 425, "FUTURE"),
    (4347, "02:26", False, 442, "DUE"),
    (4348, "15:27", True, 459, "DONE_TODAY"),
    (4349, "04:28", False, 476, "DUE"),
    (4350, "17:29", False, 493, "FUTURE"),
    (4351, "06:30", True, 510, "DONE_TODAY"),
    (4352, "19:31", False, 527, "FUTURE"),
    (4353, "08:32", False, 544, "DUE"),
    (4354, "21:33", True, 561, "DONE_TODAY"),
    (4355, "10:34", False, 578, "FUTURE"),
    (4356, "23:35", False, 595, "FUTURE"),
    (4357, "12:36", True, 612, "DONE_TODAY"),
    (4358, "01:37", False, 629, "DUE"),
    (4359, "14:38", False, 646, "FUTURE"),
    (4360, "03:39", True, 663, "DONE_TODAY"),
    (4361, "16:40", False, 680, "FUTURE"),
    (4362, "05:41", False, 697, "DUE"),
    (4363, "18:42", True, 714, "DONE_TODAY"),
    (4364, "07:43", False, 731, "DUE"),
    (4365, "20:44", False, 748, "FUTURE"),
    (4366, "09:45", True, 765, "DONE_TODAY"),
    (4367, "22:46", False, 782, "FUTURE"),
    (4368, "11:47", False, 799, "DUE"),
    (4369, "00:48", True, 816, "DONE_TODAY"),
    (4370, "13:49", False, 833, "DUE"),
    (4371, "02:50", False, 850, "DUE"),
    (4372, "15:51", True, 867, "DONE_TODAY"),
    (4373, "04:52", False, 884, "DUE"),
    (4374, "17:53", False, 901, "FUTURE"),
    (4375, "06:54", True, 918, "DONE_TODAY"),
    (4376, "19:55", False, 935, "FUTURE"),
    (4377, "08:56", False, 952, "DUE"),
    (4378, "21:57", True, 969, "DONE_TODAY"),
    (4379, "10:58", False, 986, "DUE"),
    (4380, "23:59", False, 1003, "FUTURE"),
    (4381, "12:00", True, 1020, "DONE_TODAY"),
    (4382, "01:01", False, 1037, "DUE"),
    (4383, "14:02", False, 1054, "DUE"),
    (4384, "03:03", True, 1071, "DONE_TODAY"),
    (4385, "16:04", False, 1088, "DUE"),
    (4386, "05:05", False, 1105, "DUE"),
    (4387, "18:06", True, 1122, "DONE_TODAY"),
    (4388, "07:07", False, 1139, "DUE"),
    (4389, "20:08", False, 1156, "FUTURE"),
    (4390, "09:09", True, 1173, "DONE_TODAY"),
    (4391, "22:10", False, 1190, "FUTURE"),
    (4392, "11:11", False, 1207, "DUE"),
    (4393, "00:12", True, 1224, "DONE_TODAY"),
    (4394, "13:13", False, 1241, "DUE"),
    (4395, "02:14", False, 1258, "DUE"),
    (4396, "15:15", True, 1275, "DONE_TODAY"),
    (4397, "04:16", False, 1292, "DUE"),
    (4398, "17:17", False, 1309, "DUE"),
    (4399, "06:18", True, 1326, "DONE_TODAY"),
    (4400, "19:19", False, 1343, "DUE"),
    (4401, "08:20", False, 1360, "DUE"),
    (4402, "21:21", True, 1377, "DONE_TODAY"),
    (4403, "10:22", False, 1394, "DUE"),
    (4404, "23:23", False, 1411, "DUE"),
    (4405, "12:24", True, 1428, "DONE_TODAY"),
    (4406, "01:25", False, 5, "FUTURE"),
    (4407, "14:26", False, 22, "FUTURE"),
    (4408, "03:27", True, 39, "DONE_TODAY"),
    (4409, "16:28", False, 56, "FUTURE"),
    (4410, "05:29", False, 73, "FUTURE"),
    (4411, "18:30", True, 90, "DONE_TODAY"),
    (4412, "07:31", False, 107, "FUTURE"),
    (4413, "20:32", False, 124, "FUTURE"),
    (4414, "09:33", True, 141, "DONE_TODAY"),
    (4415, "22:34", False, 158, "FUTURE"),
    (4416, "11:35", False, 175, "FUTURE"),
    (4417, "00:36", True, 192, "DONE_TODAY"),
    (4418, "13:37", False, 209, "FUTURE"),
    (4419, "02:38", False, 226, "DUE"),
    (4420, "15:39", True, 243, "DONE_TODAY"),
    (4421, "04:40", False, 260, "FUTURE"),
    (4422, "17:41", False, 277, "FUTURE"),
    (4423, "06:42", True, 294, "DONE_TODAY"),
    (4424, "19:43", False, 311, "FUTURE"),
    (4425, "08:44", False, 328, "FUTURE"),
    (4426, "21:45", True, 345, "DONE_TODAY"),
    (4427, "10:46", False, 362, "FUTURE"),
    (4428, "23:47", False, 379, "FUTURE"),
    (4429, "12:48", True, 396, "DONE_TODAY"),
    (4430, "01:49", False, 413, "DUE"),
    (4431, "14:50", False, 430, "FUTURE"),
    (4432, "03:51", True, 447, "DONE_TODAY"),
    (4433, "16:52", False, 464, "FUTURE"),
    (4434, "05:53", False, 481, "DUE"),
    (4435, "18:54", True, 498, "DONE_TODAY"),
    (4436, "07:55", False, 515, "DUE"),
    (4437, "20:56", False, 532, "FUTURE"),
    (4438, "09:57", True, 549, "DONE_TODAY"),
    (4439, "22:58", False, 566, "FUTURE"),
    (4440, "11:59", False, 583, "FUTURE"),
    (4441, "00:00", True, 600, "DONE_TODAY"),
    (4442, "13:01", False, 617, "FUTURE"),
    (4443, "02:02", False, 634, "DUE"),
    (4444, "15:03", True, 651, "DONE_TODAY"),
    (4445, "04:04", False, 668, "DUE"),
    (4446, "17:05", False, 685, "FUTURE"),
    (4447, "06:06", True, 702, "DONE_TODAY"),
    (4448, "19:07", False, 719, "FUTURE"),
    (4449, "08:08", False, 736, "DUE"),
    (4450, "21:09", True, 753, "DONE_TODAY"),
    (4451, "10:10", False, 770, "DUE"),
    (4452, "23:11", False, 787, "FUTURE"),
    (4453, "12:12", True, 804, "DONE_TODAY"),
    (4454, "01:13", False, 821, "DUE"),
    (4455, "14:14", False, 838, "FUTURE"),
    (4456, "03:15", True, 855, "DONE_TODAY"),
    (4457, "16:16", False, 872, "FUTURE"),
    (4458, "05:17", False, 889, "DUE"),
    (4459, "18:18", True, 906, "DONE_TODAY"),
    (4460, "07:19", False, 923, "DUE"),
    (4461, "20:20", False, 940, "FUTURE"),
    (4462, "09:21", True, 957, "DONE_TODAY"),
    (4463, "22:22", False, 974, "FUTURE"),
    (4464, "11:23", False, 991, "DUE"),
    (4465, "00:24", True, 1008, "DONE_TODAY"),
    (4466, "13:25", False, 1025, "DUE"),
    (4467, "02:26", False, 1042, "DUE"),
    (4468, "15:27", True, 1059, "DONE_TODAY"),
    (4469, "04:28", False, 1076, "DUE"),
    (4470, "17:29", False, 1093, "DUE"),
    (4471, "06:30", True, 1110, "DONE_TODAY"),
    (4472, "19:31", False, 1127, "FUTURE"),
    (4473, "08:32", False, 1144, "DUE"),
    (4474, "21:33", True, 1161, "DONE_TODAY"),
    (4475, "10:34", False, 1178, "DUE"),
    (4476, "23:35", False, 1195, "FUTURE"),
    (4477, "12:36", True, 1212, "DONE_TODAY"),
    (4478, "01:37", False, 1229, "DUE"),
    (4479, "14:38", False, 1246, "DUE"),
    (4480, "03:39", True, 1263, "DONE_TODAY"),
    (4481, "16:40", False, 1280, "DUE"),
    (4482, "05:41", False, 1297, "DUE"),
    (4483, "18:42", True, 1314, "DONE_TODAY"),
    (4484, "07:43", False, 1331, "DUE"),
    (4485, "20:44", False, 1348, "DUE"),
    (4486, "09:45", True, 1365, "DONE_TODAY"),
    (4487, "22:46", False, 1382, "DUE"),
    (4488, "11:47", False, 1399, "DUE"),
    (4489, "00:48", True, 1416, "DONE_TODAY"),
    (4490, "13:49", False, 1433, "DUE"),
    (4491, "02:50", False, 10, "FUTURE"),
    (4492, "15:51", True, 27, "DONE_TODAY"),
    (4493, "04:52", False, 44, "FUTURE"),
    (4494, "17:53", False, 61, "FUTURE"),
    (4495, "06:54", True, 78, "DONE_TODAY"),
    (4496, "19:55", False, 95, "FUTURE"),
    (4497, "08:56", False, 112, "FUTURE"),
    (4498, "21:57", True, 129, "DONE_TODAY"),
    (4499, "10:58", False, 146, "FUTURE"),
    (4500, "23:59", False, 163, "FUTURE"),
    (4501, "12:00", True, 180, "DONE_TODAY"),
    (4502, "01:01", False, 197, "DUE"),
    (4503, "14:02", False, 214, "FUTURE"),
    (4504, "03:03", True, 231, "DONE_TODAY"),
    (4505, "16:04", False, 248, "FUTURE"),
    (4506, "05:05", False, 265, "FUTURE"),
    (4507, "18:06", True, 282, "DONE_TODAY"),
    (4508, "07:07", False, 299, "FUTURE"),
    (4509, "20:08", False, 316, "FUTURE"),
    (4510, "09:09", True, 333, "DONE_TODAY"),
    (4511, "22:10", False, 350, "FUTURE"),
    (4512, "11:11", False, 367, "FUTURE"),
    (4513, "00:12", True, 384, "DONE_TODAY"),
    (4514, "13:13", False, 401, "FUTURE"),
    (4515, "02:14", False, 418, "DUE"),
    (4516, "15:15", True, 435, "DONE_TODAY"),
    (4517, "04:16", False, 452, "DUE"),
    (4518, "17:17", False, 469, "FUTURE"),
    (4519, "06:18", True, 486, "DONE_TODAY"),
    (4520, "19:19", False, 503, "FUTURE"),
    (4521, "08:20", False, 520, "DUE"),
    (4522, "21:21", True, 537, "DONE_TODAY"),
    (4523, "10:22", False, 554, "FUTURE"),
    (4524, "23:23", False, 571, "FUTURE"),
    (4525, "12:24", True, 588, "DONE_TODAY"),
    (4526, "01:25", False, 605, "DUE"),
    (4527, "14:26", False, 622, "FUTURE"),
    (4528, "03:27", True, 639, "DONE_TODAY"),
    (4529, "16:28", False, 656, "FUTURE"),
    (4530, "05:29", False, 673, "DUE"),
    (4531, "18:30", True, 690, "DONE_TODAY"),
    (4532, "07:31", False, 707, "DUE"),
    (4533, "20:32", False, 724, "FUTURE"),
    (4534, "09:33", True, 741, "DONE_TODAY"),
    (4535, "22:34", False, 758, "FUTURE"),
    (4536, "11:35", False, 775, "DUE"),
    (4537, "00:36", True, 792, "DONE_TODAY"),
    (4538, "13:37", False, 809, "FUTURE"),
    (4539, "02:38", False, 826, "DUE"),
    (4540, "15:39", True, 843, "DONE_TODAY"),
    (4541, "04:40", False, 860, "DUE"),
    (4542, "17:41", False, 877, "FUTURE"),
    (4543, "06:42", True, 894, "DONE_TODAY"),
    (4544, "19:43", False, 911, "FUTURE"),
    (4545, "08:44", False, 928, "DUE"),
    (4546, "21:45", True, 945, "DONE_TODAY"),
    (4547, "10:46", False, 962, "DUE"),
    (4548, "23:47", False, 979, "FUTURE"),
    (4549, "12:48", True, 996, "DONE_TODAY"),
    (4550, "01:49", False, 1013, "DUE"),
    (4551, "14:50", False, 1030, "DUE"),
    (4552, "03:51", True, 1047, "DONE_TODAY"),
    (4553, "16:52", False, 1064, "DUE"),
    (4554, "05:53", False, 1081, "DUE"),
    (4555, "18:54", True, 1098, "DONE_TODAY"),
    (4556, "07:55", False, 1115, "DUE"),
    (4557, "20:56", False, 1132, "FUTURE"),
    (4558, "09:57", True, 1149, "DONE_TODAY"),
    (4559, "22:58", False, 1166, "FUTURE"),
    (4560, "11:59", False, 1183, "DUE"),
    (4561, "00:00", True, 1200, "DONE_TODAY"),
    (4562, "13:01", False, 1217, "DUE"),
    (4563, "02:02", False, 1234, "DUE"),
    (4564, "15:03", True, 1251, "DONE_TODAY"),
    (4565, "04:04", False, 1268, "DUE"),
    (4566, "17:05", False, 1285, "DUE"),
    (4567, "06:06", True, 1302, "DONE_TODAY"),
    (4568, "19:07", False, 1319, "DUE"),
    (4569, "08:08", False, 1336, "DUE"),
    (4570, "21:09", True, 1353, "DONE_TODAY"),
    (4571, "10:10", False, 1370, "DUE"),
    (4572, "23:11", False, 1387, "FUTURE"),
    (4573, "12:12", True, 1404, "DONE_TODAY"),
    (4574, "01:13", False, 1421, "DUE"),
    (4575, "14:14", False, 1438, "DUE"),
    (4576, "03:15", True, 15, "DONE_TODAY"),
    (4577, "16:16", False, 32, "FUTURE"),
    (4578, "05:17", False, 49, "FUTURE"),
    (4579, "18:18", True, 66, "DONE_TODAY"),
    (4580, "07:19", False, 83, "FUTURE"),
    (4581, "20:20", False, 100, "FUTURE"),
    (4582, "09:21", True, 117, "DONE_TODAY"),
    (4583, "22:22", False, 134, "FUTURE"),
    (4584, "11:23", False, 151, "FUTURE"),
    (4585, "00:24", True, 168, "DONE_TODAY"),
    (4586, "13:25", False, 185, "FUTURE"),
    (4587, "02:26", False, 202, "DUE"),
    (4588, "15:27", True, 219, "DONE_TODAY"),
    (4589, "04:28", False, 236, "FUTURE"),
    (4590, "17:29", False, 253, "FUTURE"),
    (4591, "06:30", True, 270, "DONE_TODAY"),
    (4592, "19:31", False, 287, "FUTURE"),
    (4593, "08:32", False, 304, "FUTURE"),
    (4594, "21:33", True, 321, "DONE_TODAY"),
    (4595, "10:34", False, 338, "FUTURE"),
    (4596, "23:35", False, 355, "FUTURE"),
    (4597, "12:36", True, 372, "DONE_TODAY"),
    (4598, "01:37", False, 389, "DUE"),
    (4599, "14:38", False, 406, "FUTURE"),
    (4600, "03:39", True, 423, "DONE_TODAY"),
    (4601, "16:40", False, 440, "FUTURE"),
    (4602, "05:41", False, 457, "DUE"),
    (4603, "18:42", True, 474, "DONE_TODAY"),
    (4604, "07:43", False, 491, "DUE"),
    (4605, "20:44", False, 508, "FUTURE"),
    (4606, "09:45", True, 525, "DONE_TODAY"),
    (4607, "22:46", False, 542, "FUTURE"),
    (4608, "11:47", False, 559, "FUTURE"),
    (4609, "00:48", True, 576, "DONE_TODAY"),
    (4610, "13:49", False, 593, "FUTURE"),
    (4611, "02:50", False, 610, "DUE"),
    (4612, "15:51", True, 627, "DONE_TODAY"),
    (4613, "04:52", False, 644, "DUE"),
    (4614, "17:53", False, 661, "FUTURE"),
    (4615, "06:54", True, 678, "DONE_TODAY"),
    (4616, "19:55", False, 695, "FUTURE"),
    (4617, "08:56", False, 712, "DUE"),
    (4618, "21:57", True, 729, "DONE_TODAY"),
    (4619, "10:58", False, 746, "DUE"),
    (4620, "23:59", False, 763, "FUTURE"),
    (4621, "12:00", True, 780, "DONE_TODAY"),
    (4622, "01:01", False, 797, "DUE"),
    (4623, "14:02", False, 814, "FUTURE"),
    (4624, "03:03", True, 831, "DONE_TODAY"),
    (4625, "16:04", False, 848, "FUTURE"),
    (4626, "05:05", False, 865, "DUE"),
    (4627, "18:06", True, 882, "DONE_TODAY"),
    (4628, "07:07", False, 899, "DUE"),
    (4629, "20:08", False, 916, "FUTURE"),
    (4630, "09:09", True, 933, "DONE_TODAY"),
    (4631, "22:10", False, 950, "FUTURE"),
    (4632, "11:11", False, 967, "DUE"),
    (4633, "00:12", True, 984, "DONE_TODAY"),
    (4634, "13:13", False, 1001, "DUE"),
    (4635, "02:14", False, 1018, "DUE"),
    (4636, "15:15", True, 1035, "DONE_TODAY"),
    (4637, "04:16", False, 1052, "DUE"),
    (4638, "17:17", False, 1069, "DUE"),
    (4639, "06:18", True, 1086, "DONE_TODAY"),
    (4640, "19:19", False, 1103, "FUTURE"),
    (4641, "08:20", False, 1120, "DUE"),
    (4642, "21:21", True, 1137, "DONE_TODAY"),
    (4643, "10:22", False, 1154, "DUE"),
    (4644, "23:23", False, 1171, "FUTURE"),
    (4645, "12:24", True, 1188, "DONE_TODAY"),
    (4646, "01:25", False, 1205, "DUE"),
    (4647, "14:26", False, 1222, "DUE"),
    (4648, "03:27", True, 1239, "DONE_TODAY"),
    (4649, "16:28", False, 1256, "DUE"),
    (4650, "05:29", False, 1273, "DUE"),
    (4651, "18:30", True, 1290, "DONE_TODAY"),
    (4652, "07:31", False, 1307, "DUE"),
    (4653, "20:32", False, 1324, "DUE"),
    (4654, "09:33", True, 1341, "DONE_TODAY"),
    (4655, "22:34", False, 1358, "DUE"),
    (4656, "11:35", False, 1375, "DUE"),
    (4657, "00:36", True, 1392, "DONE_TODAY"),
    (4658, "13:37", False, 1409, "DUE"),
    (4659, "02:38", False, 1426, "DUE"),
    (4660, "15:39", True, 3, "DONE_TODAY"),
    (4661, "04:40", False, 20, "FUTURE"),
    (4662, "17:41", False, 37, "FUTURE"),
    (4663, "06:42", True, 54, "DONE_TODAY"),
    (4664, "19:43", False, 71, "FUTURE"),
    (4665, "08:44", False, 88, "FUTURE"),
    (4666, "21:45", True, 105, "DONE_TODAY"),
    (4667, "10:46", False, 122, "FUTURE"),
    (4668, "23:47", False, 139, "FUTURE"),
    (4669, "12:48", True, 156, "DONE_TODAY"),
    (4670, "01:49", False, 173, "DUE"),
    (4671, "14:50", False, 190, "FUTURE"),
    (4672, "03:51", True, 207, "DONE_TODAY"),
    (4673, "16:52", False, 224, "FUTURE"),
    (4674, "05:53", False, 241, "FUTURE"),
    (4675, "18:54", True, 258, "DONE_TODAY"),
    (4676, "07:55", False, 275, "FUTURE"),
    (4677, "20:56", False, 292, "FUTURE"),
    (4678, "09:57", True, 309, "DONE_TODAY"),
    (4679, "22:58", False, 326, "FUTURE"),
    (4680, "11:59", False, 343, "FUTURE"),
    (4681, "00:00", True, 360, "DONE_TODAY"),
    (4682, "13:01", False, 377, "FUTURE"),
    (4683, "02:02", False, 394, "DUE"),
    (4684, "15:03", True, 411, "DONE_TODAY"),
    (4685, "04:04", False, 428, "DUE"),
    (4686, "17:05", False, 445, "FUTURE"),
    (4687, "06:06", True, 462, "DONE_TODAY"),
    (4688, "19:07", False, 479, "FUTURE"),
    (4689, "08:08", False, 496, "DUE"),
    (4690, "21:09", True, 513, "DONE_TODAY"),
    (4691, "10:10", False, 530, "FUTURE"),
    (4692, "23:11", False, 547, "FUTURE"),
    (4693, "12:12", True, 564, "DONE_TODAY"),
    (4694, "01:13", False, 581, "DUE"),
    (4695, "14:14", False, 598, "FUTURE"),
    (4696, "03:15", True, 615, "DONE_TODAY"),
    (4697, "16:16", False, 632, "FUTURE"),
    (4698, "05:17", False, 649, "DUE"),
    (4699, "18:18", True, 666, "DONE_TODAY"),
    (4700, "07:19", False, 683, "DUE"),
    (4701, "20:20", False, 700, "FUTURE"),
    (4702, "09:21", True, 717, "DONE_TODAY"),
    (4703, "22:22", False, 734, "FUTURE"),
    (4704, "11:23", False, 751, "DUE"),
    (4705, "00:24", True, 768, "DONE_TODAY"),
    (4706, "13:25", False, 785, "FUTURE"),
    (4707, "02:26", False, 802, "DUE"),
    (4708, "15:27", True, 819, "DONE_TODAY"),
    (4709, "04:28", False, 836, "DUE"),
    (4710, "17:29", False, 853, "FUTURE"),
    (4711, "06:30", True, 870, "DONE_TODAY"),
    (4712, "19:31", False, 887, "FUTURE"),
    (4713, "08:32", False, 904, "DUE"),
    (4714, "21:33", True, 921, "DONE_TODAY"),
    (4715, "10:34", False, 938, "DUE"),
    (4716, "23:35", False, 955, "FUTURE"),
    (4717, "12:36", True, 972, "DONE_TODAY"),
    (4718, "01:37", False, 989, "DUE"),
    (4719, "14:38", False, 1006, "DUE"),
    (4720, "03:39", True, 1023, "DONE_TODAY"),
    (4721, "16:40", False, 1040, "DUE"),
    (4722, "05:41", False, 1057, "DUE"),
    (4723, "18:42", True, 1074, "DONE_TODAY"),
    (4724, "07:43", False, 1091, "DUE"),
    (4725, "20:44", False, 1108, "FUTURE"),
    (4726, "09:45", True, 1125, "DONE_TODAY"),
    (4727, "22:46", False, 1142, "FUTURE"),
    (4728, "11:47", False, 1159, "DUE"),
    (4729, "00:48", True, 1176, "DONE_TODAY"),
    (4730, "13:49", False, 1193, "DUE"),
    (4731, "02:50", False, 1210, "DUE"),
    (4732, "15:51", True, 1227, "DONE_TODAY"),
    (4733, "04:52", False, 1244, "DUE"),
    (4734, "17:53", False, 1261, "DUE"),
    (4735, "06:54", True, 1278, "DONE_TODAY"),
    (4736, "19:55", False, 1295, "DUE"),
    (4737, "08:56", False, 1312, "DUE"),
    (4738, "21:57", True, 1329, "DONE_TODAY"),
    (4739, "10:58", False, 1346, "DUE"),
    (4740, "23:59", False, 1363, "FUTURE"),
    (4741, "12:00", True, 1380, "DONE_TODAY"),
    (4742, "01:01", False, 1397, "DUE"),
    (4743, "14:02", False, 1414, "DUE"),
    (4744, "03:03", True, 1431, "DONE_TODAY"),
    (4745, "16:04", False, 8, "FUTURE"),
    (4746, "05:05", False, 25, "FUTURE"),
    (4747, "18:06", True, 42, "DONE_TODAY"),
    (4748, "07:07", False, 59, "FUTURE"),
    (4749, "20:08", False, 76, "FUTURE"),
    (4750, "09:09", True, 93, "DONE_TODAY"),
    (4751, "22:10", False, 110, "FUTURE"),
    (4752, "11:11", False, 127, "FUTURE"),
    (4753, "00:12", True, 144, "DONE_TODAY"),
    (4754, "13:13", False, 161, "FUTURE"),
    (4755, "02:14", False, 178, "DUE"),
    (4756, "15:15", True, 195, "DONE_TODAY"),
    (4757, "04:16", False, 212, "FUTURE"),
    (4758, "17:17", False, 229, "FUTURE"),
    (4759, "06:18", True, 246, "DONE_TODAY"),
    (4760, "19:19", False, 263, "FUTURE"),
    (4761, "08:20", False, 280, "FUTURE"),
    (4762, "21:21", True, 297, "DONE_TODAY"),
    (4763, "10:22", False, 314, "FUTURE"),
    (4764, "23:23", False, 331, "FUTURE"),
    (4765, "12:24", True, 348, "DONE_TODAY"),
    (4766, "01:25", False, 365, "DUE"),
    (4767, "14:26", False, 382, "FUTURE"),
    (4768, "03:27", True, 399, "DONE_TODAY"),
    (4769, "16:28", False, 416, "FUTURE"),
    (4770, "05:29", False, 433, "DUE"),
    (4771, "18:30", True, 450, "DONE_TODAY"),
    (4772, "07:31", False, 467, "DUE"),
    (4773, "20:32", False, 484, "FUTURE"),
    (4774, "09:33", True, 501, "DONE_TODAY"),
    (4775, "22:34", False, 518, "FUTURE"),
    (4776, "11:35", False, 535, "FUTURE"),
    (4777, "00:36", True, 552, "DONE_TODAY"),
    (4778, "13:37", False, 569, "FUTURE"),
    (4779, "02:38", False, 586, "DUE"),
    (4780, "15:39", True, 603, "DONE_TODAY"),
    (4781, "04:40", False, 620, "DUE"),
    (4782, "17:41", False, 637, "FUTURE"),
    (4783, "06:42", True, 654, "DONE_TODAY"),
    (4784, "19:43", False, 671, "FUTURE"),
    (4785, "08:44", False, 688, "DUE"),
    (4786, "21:45", True, 705, "DONE_TODAY"),
    (4787, "10:46", False, 722, "DUE"),
    (4788, "23:47", False, 739, "FUTURE"),
    (4789, "12:48", True, 756, "DONE_TODAY"),
    (4790, "01:49", False, 773, "DUE"),
    (4791, "14:50", False, 790, "FUTURE"),
    (4792, "03:51", True, 807, "DONE_TODAY"),
    (4793, "16:52", False, 824, "FUTURE"),
    (4794, "05:53", False, 841, "DUE"),
    (4795, "18:54", True, 858, "DONE_TODAY"),
    (4796, "07:55", False, 875, "DUE"),
    (4797, "20:56", False, 892, "FUTURE"),
    (4798, "09:57", True, 909, "DONE_TODAY"),
    (4799, "22:58", False, 926, "FUTURE"),
    (4800, "11:59", False, 943, "DUE"),
    (4801, "00:00", True, 960, "DONE_TODAY"),
    (4802, "13:01", False, 977, "DUE"),
    (4803, "02:02", False, 994, "DUE"),
    (4804, "15:03", True, 1011, "DONE_TODAY"),
    (4805, "04:04", False, 1028, "DUE"),
    (4806, "17:05", False, 1045, "DUE"),
    (4807, "06:06", True, 1062, "DONE_TODAY"),
    (4808, "19:07", False, 1079, "FUTURE"),
    (4809, "08:08", False, 1096, "DUE"),
    (4810, "21:09", True, 1113, "DONE_TODAY"),
    (4811, "10:10", False, 1130, "DUE"),
    (4812, "23:11", False, 1147, "FUTURE"),
    (4813, "12:12", True, 1164, "DONE_TODAY"),
    (4814, "01:13", False, 1181, "DUE"),
    (4815, "14:14", False, 1198, "DUE"),
    (4816, "03:15", True, 1215, "DONE_TODAY"),
    (4817, "16:16", False, 1232, "DUE"),
    (4818, "05:17", False, 1249, "DUE"),
    (4819, "18:18", True, 1266, "DONE_TODAY"),
    (4820, "07:19", False, 1283, "DUE"),
    (4821, "20:20", False, 1300, "DUE"),
    (4822, "09:21", True, 1317, "DONE_TODAY"),
    (4823, "22:22", False, 1334, "FUTURE"),
    (4824, "11:23", False, 1351, "DUE"),
    (4825, "00:24", True, 1368, "DONE_TODAY"),
    (4826, "13:25", False, 1385, "DUE"),
    (4827, "02:26", False, 1402, "DUE"),
    (4828, "15:27", True, 1419, "DONE_TODAY"),
    (4829, "04:28", False, 1436, "DUE"),
    (4830, "17:29", False, 13, "FUTURE"),
    (4831, "06:30", True, 30, "DONE_TODAY"),
    (4832, "19:31", False, 47, "FUTURE"),
    (4833, "08:32", False, 64, "FUTURE"),
    (4834, "21:33", True, 81, "DONE_TODAY"),
    (4835, "10:34", False, 98, "FUTURE"),
    (4836, "23:35", False, 115, "FUTURE"),
    (4837, "12:36", True, 132, "DONE_TODAY"),
    (4838, "01:37", False, 149, "DUE"),
    (4839, "14:38", False, 166, "FUTURE"),
    (4840, "03:39", True, 183, "DONE_TODAY"),
    (4841, "16:40", False, 200, "FUTURE"),
    (4842, "05:41", False, 217, "FUTURE"),
    (4843, "18:42", True, 234, "DONE_TODAY"),
    (4844, "07:43", False, 251, "FUTURE"),
    (4845, "20:44", False, 268, "FUTURE"),
    (4846, "09:45", True, 285, "DONE_TODAY"),
    (4847, "22:46", False, 302, "FUTURE"),
    (4848, "11:47", False, 319, "FUTURE"),
    (4849, "00:48", True, 336, "DONE_TODAY"),
    (4850, "13:49", False, 353, "FUTURE"),
    (4851, "02:50", False, 370, "DUE"),
    (4852, "15:51", True, 387, "DONE_TODAY"),
    (4853, "04:52", False, 404, "DUE"),
    (4854, "17:53", False, 421, "FUTURE"),
    (4855, "06:54", True, 438, "DONE_TODAY"),
    (4856, "19:55", False, 455, "FUTURE"),
    (4857, "08:56", False, 472, "FUTURE"),
    (4858, "21:57", True, 489, "DONE_TODAY"),
    (4859, "10:58", False, 506, "FUTURE"),
    (4860, "23:59", False, 523, "FUTURE"),
    (4861, "12:00", True, 540, "DONE_TODAY"),
    (4862, "01:01", False, 557, "DUE"),
    (4863, "14:02", False, 574, "FUTURE"),
    (4864, "03:03", True, 591, "DONE_TODAY"),
    (4865, "16:04", False, 608, "FUTURE"),
    (4866, "05:05", False, 625, "DUE"),
    (4867, "18:06", True, 642, "DONE_TODAY"),
    (4868, "07:07", False, 659, "DUE"),
    (4869, "20:08", False, 676, "FUTURE"),
    (4870, "09:09", True, 693, "DONE_TODAY"),
    (4871, "22:10", False, 710, "FUTURE"),
    (4872, "11:11", False, 727, "DUE"),
    (4873, "00:12", True, 744, "DONE_TODAY"),
    (4874, "13:13", False, 761, "FUTURE"),
    (4875, "02:14", False, 778, "DUE"),
    (4876, "15:15", True, 795, "DONE_TODAY"),
    (4877, "04:16", False, 812, "DUE"),
    (4878, "17:17", False, 829, "FUTURE"),
    (4879, "06:18", True, 846, "DONE_TODAY"),
    (4880, "19:19", False, 863, "FUTURE"),
    (4881, "08:20", False, 880, "DUE"),
    (4882, "21:21", True, 897, "DONE_TODAY"),
    (4883, "10:22", False, 914, "DUE"),
    (4884, "23:23", False, 931, "FUTURE"),
    (4885, "12:24", True, 948, "DONE_TODAY"),
    (4886, "01:25", False, 965, "DUE"),
    (4887, "14:26", False, 982, "DUE"),
    (4888, "03:27", True, 999, "DONE_TODAY"),
    (4889, "16:28", False, 1016, "DUE"),
    (4890, "05:29", False, 1033, "DUE"),
    (4891, "18:30", True, 1050, "DONE_TODAY"),
    (4892, "07:31", False, 1067, "DUE"),
    (4893, "20:32", False, 1084, "FUTURE"),
    (4894, "09:33", True, 1101, "DONE_TODAY"),
    (4895, "22:34", False, 1118, "FUTURE"),
    (4896, "11:35", False, 1135, "DUE"),
    (4897, "00:36", True, 1152, "DONE_TODAY"),
    (4898, "13:37", False, 1169, "DUE"),
    (4899, "02:38", False, 1186, "DUE"),
    (4900, "15:39", True, 1203, "DONE_TODAY"),
    (4901, "04:40", False, 1220, "DUE"),
    (4902, "17:41", False, 1237, "DUE"),
    (4903, "06:42", True, 1254, "DONE_TODAY"),
    (4904, "19:43", False, 1271, "DUE"),
    (4905, "08:44", False, 1288, "DUE"),
    (4906, "21:45", True, 1305, "DONE_TODAY"),
    (4907, "10:46", False, 1322, "DUE"),
    (4908, "23:47", False, 1339, "FUTURE"),
    (4909, "12:48", True, 1356, "DONE_TODAY"),
    (4910, "01:49", False, 1373, "DUE"),
    (4911, "14:50", False, 1390, "DUE"),
    (4912, "03:51", True, 1407, "DONE_TODAY"),
    (4913, "16:52", False, 1424, "DUE"),
    (4914, "05:53", False, 1, "FUTURE"),
    (4915, "18:54", True, 18, "DONE_TODAY"),
    (4916, "07:55", False, 35, "FUTURE"),
    (4917, "20:56", False, 52, "FUTURE"),
    (4918, "09:57", True, 69, "DONE_TODAY"),
    (4919, "22:58", False, 86, "FUTURE"),
    (4920, "11:59", False, 103, "FUTURE"),
    (4921, "00:00", True, 120, "DONE_TODAY"),
    (4922, "13:01", False, 137, "FUTURE"),
    (4923, "02:02", False, 154, "DUE"),
    (4924, "15:03", True, 171, "DONE_TODAY"),
    (4925, "04:04", False, 188, "FUTURE"),
    (4926, "17:05", False, 205, "FUTURE"),
    (4927, "06:06", True, 222, "DONE_TODAY"),
    (4928, "19:07", False, 239, "FUTURE"),
    (4929, "08:08", False, 256, "FUTURE"),
    (4930, "21:09", True, 273, "DONE_TODAY"),
    (4931, "10:10", False, 290, "FUTURE"),
    (4932, "23:11", False, 307, "FUTURE"),
    (4933, "12:12", True, 324, "DONE_TODAY"),
    (4934, "01:13", False, 341, "DUE"),
    (4935, "14:14", False, 358, "FUTURE"),
    (4936, "03:15", True, 375, "DONE_TODAY"),
    (4937, "16:16", False, 392, "FUTURE"),
    (4938, "05:17", False, 409, "DUE"),
    (4939, "18:18", True, 426, "DONE_TODAY"),
    (4940, "07:19", False, 443, "DUE"),
    (4941, "20:20", False, 460, "FUTURE"),
    (4942, "09:21", True, 477, "DONE_TODAY"),
    (4943, "22:22", False, 494, "FUTURE"),
    (4944, "11:23", False, 511, "FUTURE"),
    (4945, "00:24", True, 528, "DONE_TODAY"),
    (4946, "13:25", False, 545, "FUTURE"),
    (4947, "02:26", False, 562, "DUE"),
    (4948, "15:27", True, 579, "DONE_TODAY"),
    (4949, "04:28", False, 596, "DUE"),
    (4950, "17:29", False, 613, "FUTURE"),
    (4951, "06:30", True, 630, "DONE_TODAY"),
    (4952, "19:31", False, 647, "FUTURE"),
    (4953, "08:32", False, 664, "DUE"),
    (4954, "21:33", True, 681, "DONE_TODAY"),
    (4955, "10:34", False, 698, "DUE"),
    (4956, "23:35", False, 715, "FUTURE"),
    (4957, "12:36", True, 732, "DONE_TODAY"),
    (4958, "01:37", False, 749, "DUE"),
    (4959, "14:38", False, 766, "FUTURE"),
    (4960, "03:39", True, 783, "DONE_TODAY"),
    (4961, "16:40", False, 800, "FUTURE"),
    (4962, "05:41", False, 817, "DUE"),
    (4963, "18:42", True, 834, "DONE_TODAY"),
    (4964, "07:43", False, 851, "DUE"),
    (4965, "20:44", False, 868, "FUTURE"),
    (4966, "09:45", True, 885, "DONE_TODAY"),
    (4967, "22:46", False, 902, "FUTURE"),
    (4968, "11:47", False, 919, "DUE"),
    (4969, "00:48", True, 936, "DONE_TODAY"),
    (4970, "13:49", False, 953, "DUE"),
    (4971, "02:50", False, 970, "DUE"),
    (4972, "15:51", True, 987, "DONE_TODAY"),
    (4973, "04:52", False, 1004, "DUE"),
    (4974, "17:53", False, 1021, "FUTURE"),
    (4975, "06:54", True, 1038, "DONE_TODAY"),
    (4976, "19:55", False, 1055, "FUTURE"),
    (4977, "08:56", False, 1072, "DUE"),
    (4978, "21:57", True, 1089, "DONE_TODAY"),
    (4979, "10:58", False, 1106, "DUE"),
    (4980, "23:59", False, 1123, "FUTURE"),
    (4981, "12:00", True, 1140, "DONE_TODAY"),
    (4982, "01:01", False, 1157, "DUE"),
    (4983, "14:02", False, 1174, "DUE"),
    (4984, "03:03", True, 1191, "DONE_TODAY"),
    (4985, "16:04", False, 1208, "DUE"),
    (4986, "05:05", False, 1225, "DUE"),
    (4987, "18:06", True, 1242, "DONE_TODAY"),
    (4988, "07:07", False, 1259, "DUE"),
    (4989, "20:08", False, 1276, "DUE"),
    (4990, "09:09", True, 1293, "DONE_TODAY"),
    (4991, "22:10", False, 1310, "FUTURE"),
    (4992, "11:11", False, 1327, "DUE"),
    (4993, "00:12", True, 1344, "DONE_TODAY"),
    (4994, "13:13", False, 1361, "DUE"),
    (4995, "02:14", False, 1378, "DUE"),
    (4996, "15:15", True, 1395, "DONE_TODAY"),
    (4997, "04:16", False, 1412, "DUE"),
    (4998, "17:17", False, 1429, "DUE"),
    (4999, "06:18", True, 6, "DONE_TODAY"),
    (5000, "19:19", False, 23, "FUTURE"),
    (5001, "08:20", False, 40, "FUTURE"),
    (5002, "21:21", True, 57, "DONE_TODAY"),
    (5003, "10:22", False, 74, "FUTURE"),
    (5004, "23:23", False, 91, "FUTURE"),
    (5005, "12:24", True, 108, "DONE_TODAY"),
    (5006, "01:25", False, 125, "DUE"),
    (5007, "14:26", False, 142, "FUTURE"),
    (5008, "03:27", True, 159, "DONE_TODAY"),
    (5009, "16:28", False, 176, "FUTURE"),
    (5010, "05:29", False, 193, "FUTURE"),
    (5011, "18:30", True, 210, "DONE_TODAY"),
    (5012, "07:31", False, 227, "FUTURE"),
    (5013, "20:32", False, 244, "FUTURE"),
    (5014, "09:33", True, 261, "DONE_TODAY"),
    (5015, "22:34", False, 278, "FUTURE"),
    (5016, "11:35", False, 295, "FUTURE"),
    (5017, "00:36", True, 312, "DONE_TODAY"),
    (5018, "13:37", False, 329, "FUTURE"),
    (5019, "02:38", False, 346, "DUE"),
    (5020, "15:39", True, 363, "DONE_TODAY"),
    (5021, "04:40", False, 380, "DUE"),
    (5022, "17:41", False, 397, "FUTURE"),
    (5023, "06:42", True, 414, "DONE_TODAY"),
    (5024, "19:43", False, 431, "FUTURE"),
    (5025, "08:44", False, 448, "FUTURE"),
    (5026, "21:45", True, 465, "DONE_TODAY"),
    (5027, "10:46", False, 482, "FUTURE"),
    (5028, "23:47", False, 499, "FUTURE"),
    (5029, "12:48", True, 516, "DONE_TODAY"),
    (5030, "01:49", False, 533, "DUE"),
    (5031, "14:50", False, 550, "FUTURE"),
    (5032, "03:51", True, 567, "DONE_TODAY"),
    (5033, "16:52", False, 584, "FUTURE"),
    (5034, "05:53", False, 601, "DUE"),
    (5035, "18:54", True, 618, "DONE_TODAY"),
    (5036, "07:55", False, 635, "DUE"),
    (5037, "20:56", False, 652, "FUTURE"),
    (5038, "09:57", True, 669, "DONE_TODAY"),
    (5039, "22:58", False, 686, "FUTURE"),
    (5040, "11:59", False, 703, "FUTURE"),
    (5041, "00:00", True, 720, "DONE_TODAY"),
    (5042, "13:01", False, 737, "FUTURE"),
    (5043, "02:02", False, 754, "DUE"),
    (5044, "15:03", True, 771, "DONE_TODAY"),
    (5045, "04:04", False, 788, "DUE"),
    (5046, "17:05", False, 805, "FUTURE"),
    (5047, "06:06", True, 822, "DONE_TODAY"),
    (5048, "19:07", False, 839, "FUTURE"),
    (5049, "08:08", False, 856, "DUE"),
    (5050, "21:09", True, 873, "DONE_TODAY"),
    (5051, "10:10", False, 890, "DUE"),
    (5052, "23:11", False, 907, "FUTURE"),
    (5053, "12:12", True, 924, "DONE_TODAY"),
    (5054, "01:13", False, 941, "DUE"),
    (5055, "14:14", False, 958, "DUE"),
    (5056, "03:15", True, 975, "DONE_TODAY"),
    (5057, "16:16", False, 992, "DUE"),
    (5058, "05:17", False, 1009, "DUE"),
    (5059, "18:18", True, 1026, "DONE_TODAY"),
    (5060, "07:19", False, 1043, "DUE"),
    (5061, "20:20", False, 1060, "FUTURE"),
    (5062, "09:21", True, 1077, "DONE_TODAY"),
    (5063, "22:22", False, 1094, "FUTURE"),
    (5064, "11:23", False, 1111, "DUE"),
    (5065, "00:24", True, 1128, "DONE_TODAY"),
    (5066, "13:25", False, 1145, "DUE"),
    (5067, "02:26", False, 1162, "DUE"),
    (5068, "15:27", True, 1179, "DONE_TODAY"),
    (5069, "04:28", False, 1196, "DUE"),
    (5070, "17:29", False, 1213, "DUE"),
    (5071, "06:30", True, 1230, "DONE_TODAY"),
    (5072, "19:31", False, 1247, "DUE"),
    (5073, "08:32", False, 1264, "DUE"),
    (5074, "21:33", True, 1281, "DONE_TODAY"),
    (5075, "10:34", False, 1298, "DUE"),
    (5076, "23:35", False, 1315, "FUTURE"),
    (5077, "12:36", True, 1332, "DONE_TODAY"),
    (5078, "01:37", False, 1349, "DUE"),
    (5079, "14:38", False, 1366, "DUE"),
    (5080, "03:39", True, 1383, "DONE_TODAY"),
    (5081, "16:40", False, 1400, "DUE"),
    (5082, "05:41", False, 1417, "DUE"),
    (5083, "18:42", True, 1434, "DONE_TODAY"),
    (5084, "07:43", False, 11, "FUTURE"),
    (5085, "20:44", False, 28, "FUTURE"),
    (5086, "09:45", True, 45, "DONE_TODAY"),
    (5087, "22:46", False, 62, "FUTURE"),
    (5088, "11:47", False, 79, "FUTURE"),
    (5089, "00:48", True, 96, "DONE_TODAY"),
    (5090, "13:49", False, 113, "FUTURE"),
    (5091, "02:50", False, 130, "FUTURE"),
    (5092, "15:51", True, 147, "DONE_TODAY"),
    (5093, "04:52", False, 164, "FUTURE"),
    (5094, "17:53", False, 181, "FUTURE"),
    (5095, "06:54", True, 198, "DONE_TODAY"),
    (5096, "19:55", False, 215, "FUTURE"),
    (5097, "08:56", False, 232, "FUTURE"),
    (5098, "21:57", True, 249, "DONE_TODAY"),
    (5099, "10:58", False, 266, "FUTURE"),
    (5100, "23:59", False, 283, "FUTURE"),
    (5101, "12:00", True, 300, "DONE_TODAY"),
    (5102, "01:01", False, 317, "DUE"),
    (5103, "14:02", False, 334, "FUTURE"),
    (5104, "03:03", True, 351, "DONE_TODAY"),
    (5105, "16:04", False, 368, "FUTURE"),
    (5106, "05:05", False, 385, "DUE"),
    (5107, "18:06", True, 402, "DONE_TODAY"),
    (5108, "07:07", False, 419, "FUTURE"),
    (5109, "20:08", False, 436, "FUTURE"),
    (5110, "09:09", True, 453, "DONE_TODAY"),
    (5111, "22:10", False, 470, "FUTURE"),
    (5112, "11:11", False, 487, "FUTURE"),
    (5113, "00:12", True, 504, "DONE_TODAY"),
    (5114, "13:13", False, 521, "FUTURE"),
    (5115, "02:14", False, 538, "DUE"),
    (5116, "15:15", True, 555, "DONE_TODAY"),
    (5117, "04:16", False, 572, "DUE"),
    (5118, "17:17", False, 589, "FUTURE"),
    (5119, "06:18", True, 606, "DONE_TODAY"),
    (5120, "19:19", False, 623, "FUTURE"),
    (5121, "08:20", False, 640, "DUE"),
    (5122, "21:21", True, 657, "DONE_TODAY"),
    (5123, "10:22", False, 674, "DUE"),
    (5124, "23:23", False, 691, "FUTURE"),
    (5125, "12:24", True, 708, "DONE_TODAY"),
    (5126, "01:25", False, 725, "DUE"),
    (5127, "14:26", False, 742, "FUTURE"),
    (5128, "03:27", True, 759, "DONE_TODAY"),
    (5129, "16:28", False, 776, "FUTURE"),
    (5130, "05:29", False, 793, "DUE"),
    (5131, "18:30", True, 810, "DONE_TODAY"),
    (5132, "07:31", False, 827, "DUE"),
    (5133, "20:32", False, 844, "FUTURE"),
    (5134, "09:33", True, 861, "DONE_TODAY"),
    (5135, "22:34", False, 878, "FUTURE"),
    (5136, "11:35", False, 895, "DUE"),
    (5137, "00:36", True, 912, "DONE_TODAY"),
    (5138, "13:37", False, 929, "DUE"),
    (5139, "02:38", False, 946, "DUE"),
    (5140, "15:39", True, 963, "DONE_TODAY"),
    (5141, "04:40", False, 980, "DUE"),
    (5142, "17:41", False, 997, "FUTURE"),
    (5143, "06:42", True, 1014, "DONE_TODAY"),
    (5144, "19:43", False, 1031, "FUTURE"),
    (5145, "08:44", False, 1048, "DUE"),
    (5146, "21:45", True, 1065, "DONE_TODAY"),
    (5147, "10:46", False, 1082, "DUE"),
    (5148, "23:47", False, 1099, "FUTURE"),
    (5149, "12:48", True, 1116, "DONE_TODAY"),
    (5150, "01:49", False, 1133, "DUE"),
    (5151, "14:50", False, 1150, "DUE"),
    (5152, "03:51", True, 1167, "DONE_TODAY"),
    (5153, "16:52", False, 1184, "DUE"),
    (5154, "05:53", False, 1201, "DUE"),
    (5155, "18:54", True, 1218, "DONE_TODAY"),
    (5156, "07:55", False, 1235, "DUE"),
    (5157, "20:56", False, 1252, "FUTURE"),
    (5158, "09:57", True, 1269, "DONE_TODAY"),
    (5159, "22:58", False, 1286, "FUTURE"),
    (5160, "11:59", False, 1303, "DUE"),
    (5161, "00:00", True, 1320, "DONE_TODAY"),
    (5162, "13:01", False, 1337, "DUE"),
    (5163, "02:02", False, 1354, "DUE"),
    (5164, "15:03", True, 1371, "DONE_TODAY"),
    (5165, "04:04", False, 1388, "DUE"),
    (5166, "17:05", False, 1405, "DUE"),
    (5167, "06:06", True, 1422, "DONE_TODAY"),
    (5168, "19:07", False, 1439, "DUE"),
    (5169, "08:08", False, 16, "FUTURE"),
    (5170, "21:09", True, 33, "DONE_TODAY"),
    (5171, "10:10", False, 50, "FUTURE"),
    (5172, "23:11", False, 67, "FUTURE"),
    (5173, "12:12", True, 84, "DONE_TODAY"),
    (5174, "01:13", False, 101, "DUE"),
    (5175, "14:14", False, 118, "FUTURE"),
    (5176, "03:15", True, 135, "DONE_TODAY"),
    (5177, "16:16", False, 152, "FUTURE"),
    (5178, "05:17", False, 169, "FUTURE"),
    (5179, "18:18", True, 186, "DONE_TODAY"),
    (5180, "07:19", False, 203, "FUTURE"),
    (5181, "20:20", False, 220, "FUTURE"),
    (5182, "09:21", True, 237, "DONE_TODAY"),
    (5183, "22:22", False, 254, "FUTURE"),
    (5184, "11:23", False, 271, "FUTURE"),
    (5185, "00:24", True, 288, "DONE_TODAY"),
    (5186, "13:25", False, 305, "FUTURE"),
    (5187, "02:26", False, 322, "DUE"),
    (5188, "15:27", True, 339, "DONE_TODAY"),
    (5189, "04:28", False, 356, "DUE"),
    (5190, "17:29", False, 373, "FUTURE"),
    (5191, "06:30", True, 390, "DONE_TODAY"),
    (5192, "19:31", False, 407, "FUTURE"),
    (5193, "08:32", False, 424, "FUTURE"),
    (5194, "21:33", True, 441, "DONE_TODAY"),
    (5195, "10:34", False, 458, "FUTURE"),
    (5196, "23:35", False, 475, "FUTURE"),
    (5197, "12:36", True, 492, "DONE_TODAY"),
    (5198, "01:37", False, 509, "DUE"),
    (5199, "14:38", False, 526, "FUTURE"),
    (5200, "03:39", True, 543, "DONE_TODAY"),
    (5201, "16:40", False, 560, "FUTURE"),
    (5202, "05:41", False, 577, "DUE"),
    (5203, "18:42", True, 594, "DONE_TODAY"),
    (5204, "07:43", False, 611, "DUE"),
    (5205, "20:44", False, 628, "FUTURE"),
    (5206, "09:45", True, 645, "DONE_TODAY"),
    (5207, "22:46", False, 662, "FUTURE"),
    (5208, "11:47", False, 679, "FUTURE"),
    (5209, "00:48", True, 696, "DONE_TODAY"),
    (5210, "13:49", False, 713, "FUTURE"),
    (5211, "02:50", False, 730, "DUE"),
    (5212, "15:51", True, 747, "DONE_TODAY"),
    (5213, "04:52", False, 764, "DUE"),
    (5214, "17:53", False, 781, "FUTURE"),
    (5215, "06:54", True, 798, "DONE_TODAY"),
    (5216, "19:55", False, 815, "FUTURE"),
    (5217, "08:56", False, 832, "DUE"),
    (5218, "21:57", True, 849, "DONE_TODAY"),
    (5219, "10:58", False, 866, "DUE"),
    (5220, "23:59", False, 883, "FUTURE"),
    (5221, "12:00", True, 900, "DONE_TODAY"),
    (5222, "01:01", False, 917, "DUE"),
    (5223, "14:02", False, 934, "DUE"),
    (5224, "03:03", True, 951, "DONE_TODAY"),
    (5225, "16:04", False, 968, "DUE"),
    (5226, "05:05", False, 985, "DUE"),
    (5227, "18:06", True, 1002, "DONE_TODAY"),
    (5228, "07:07", False, 1019, "DUE"),
    (5229, "20:08", False, 1036, "FUTURE"),
    (5230, "09:09", True, 1053, "DONE_TODAY"),
    (5231, "22:10", False, 1070, "FUTURE"),
    (5232, "11:11", False, 1087, "DUE"),
    (5233, "00:12", True, 1104, "DONE_TODAY"),
    (5234, "13:13", False, 1121, "DUE"),
    (5235, "02:14", False, 1138, "DUE"),
    (5236, "15:15", True, 1155, "DONE_TODAY"),
    (5237, "04:16", False, 1172, "DUE"),
    (5238, "17:17", False, 1189, "DUE"),
    (5239, "06:18", True, 1206, "DONE_TODAY"),
    (5240, "19:19", False, 1223, "DUE"),
    (5241, "08:20", False, 1240, "DUE"),
    (5242, "21:21", True, 1257, "DONE_TODAY"),
    (5243, "10:22", False, 1274, "DUE"),
    (5244, "23:23", False, 1291, "FUTURE"),
    (5245, "12:24", True, 1308, "DONE_TODAY"),
    (5246, "01:25", False, 1325, "DUE"),
    (5247, "14:26", False, 1342, "DUE"),
    (5248, "03:27", True, 1359, "DONE_TODAY"),
    (5249, "16:28", False, 1376, "DUE"),
    (5250, "05:29", False, 1393, "DUE"),
    (5251, "18:30", True, 1410, "DONE_TODAY"),
    (5252, "07:31", False, 1427, "DUE"),
    (5253, "20:32", False, 4, "FUTURE"),
    (5254, "09:33", True, 21, "DONE_TODAY"),
    (5255, "22:34", False, 38, "FUTURE"),
    (5256, "11:35", False, 55, "FUTURE"),
    (5257, "00:36", True, 72, "DONE_TODAY"),
    (5258, "13:37", False, 89, "FUTURE"),
    (5259, "02:38", False, 106, "FUTURE"),
    (5260, "15:39", True, 123, "DONE_TODAY"),
    (5261, "04:40", False, 140, "FUTURE"),
    (5262, "17:41", False, 157, "FUTURE"),
    (5263, "06:42", True, 174, "DONE_TODAY"),
    (5264, "19:43", False, 191, "FUTURE"),
    (5265, "08:44", False, 208, "FUTURE"),
    (5266, "21:45", True, 225, "DONE_TODAY"),
    (5267, "10:46", False, 242, "FUTURE"),
    (5268, "23:47", False, 259, "FUTURE"),
    (5269, "12:48", True, 276, "DONE_TODAY"),
    (5270, "01:49", False, 293, "DUE"),
    (5271, "14:50", False, 310, "FUTURE"),
    (5272, "03:51", True, 327, "DONE_TODAY"),
    (5273, "16:52", False, 344, "FUTURE"),
    (5274, "05:53", False, 361, "DUE"),
    (5275, "18:54", True, 378, "DONE_TODAY"),
    (5276, "07:55", False, 395, "FUTURE"),
    (5277, "20:56", False, 412, "FUTURE"),
    (5278, "09:57", True, 429, "DONE_TODAY"),
    (5279, "22:58", False, 446, "FUTURE"),
    (5280, "11:59", False, 463, "FUTURE"),
    (5281, "00:00", True, 480, "DONE_TODAY"),
    (5282, "13:01", False, 497, "FUTURE"),
    (5283, "02:02", False, 514, "DUE"),
    (5284, "15:03", True, 531, "DONE_TODAY"),
    (5285, "04:04", False, 548, "DUE"),
    (5286, "17:05", False, 565, "FUTURE"),
    (5287, "06:06", True, 582, "DONE_TODAY"),
    (5288, "19:07", False, 599, "FUTURE"),
    (5289, "08:08", False, 616, "DUE"),
    (5290, "21:09", True, 633, "DONE_TODAY"),
    (5291, "10:10", False, 650, "DUE"),
    (5292, "23:11", False, 667, "FUTURE"),
    (5293, "12:12", True, 684, "DONE_TODAY"),
    (5294, "01:13", False, 701, "DUE"),
    (5295, "14:14", False, 718, "FUTURE"),
    (5296, "03:15", True, 735, "DONE_TODAY"),
    (5297, "16:16", False, 752, "FUTURE"),
    (5298, "05:17", False, 769, "DUE"),
    (5299, "18:18", True, 786, "DONE_TODAY"),
    (5300, "07:19", False, 803, "DUE"),
    (5301, "20:20", False, 820, "FUTURE"),
    (5302, "09:21", True, 837, "DONE_TODAY"),
    (5303, "22:22", False, 854, "FUTURE"),
    (5304, "11:23", False, 871, "DUE"),
    (5305, "00:24", True, 888, "DONE_TODAY"),
    (5306, "13:25", False, 905, "DUE"),
    (5307, "02:26", False, 922, "DUE"),
    (5308, "15:27", True, 939, "DONE_TODAY"),
    (5309, "04:28", False, 956, "DUE"),
    (5310, "17:29", False, 973, "FUTURE"),
    (5311, "06:30", True, 990, "DONE_TODAY"),
    (5312, "19:31", False, 1007, "FUTURE"),
    (5313, "08:32", False, 1024, "DUE"),
    (5314, "21:33", True, 1041, "DONE_TODAY"),
    (5315, "10:34", False, 1058, "DUE"),
    (5316, "23:35", False, 1075, "FUTURE"),
    (5317, "12:36", True, 1092, "DONE_TODAY"),
    (5318, "01:37", False, 1109, "DUE"),
    (5319, "14:38", False, 1126, "DUE"),
    (5320, "03:39", True, 1143, "DONE_TODAY"),
    (5321, "16:40", False, 1160, "DUE"),
    (5322, "05:41", False, 1177, "DUE"),
    (5323, "18:42", True, 1194, "DONE_TODAY"),
    (5324, "07:43", False, 1211, "DUE"),
    (5325, "20:44", False, 1228, "FUTURE"),
    (5326, "09:45", True, 1245, "DONE_TODAY"),
    (5327, "22:46", False, 1262, "FUTURE"),
    (5328, "11:47", False, 1279, "DUE"),
    (5329, "00:48", True, 1296, "DONE_TODAY"),
    (5330, "13:49", False, 1313, "DUE"),
    (5331, "02:50", False, 1330, "DUE"),
    (5332, "15:51", True, 1347, "DONE_TODAY"),
    (5333, "04:52", False, 1364, "DUE"),
    (5334, "17:53", False, 1381, "DUE"),
    (5335, "06:54", True, 1398, "DONE_TODAY"),
    (5336, "19:55", False, 1415, "DUE"),
    (5337, "08:56", False, 1432, "DUE"),
    (5338, "21:57", True, 9, "DONE_TODAY"),
    (5339, "10:58", False, 26, "FUTURE"),
    (5340, "23:59", False, 43, "FUTURE"),
    (5341, "12:00", True, 60, "DONE_TODAY"),
    (5342, "01:01", False, 77, "DUE"),
    (5343, "14:02", False, 94, "FUTURE"),
    (5344, "03:03", True, 111, "DONE_TODAY"),
    (5345, "16:04", False, 128, "FUTURE"),
    (5346, "05:05", False, 145, "FUTURE"),
    (5347, "18:06", True, 162, "DONE_TODAY"),
    (5348, "07:07", False, 179, "FUTURE"),
    (5349, "20:08", False, 196, "FUTURE"),
    (5350, "09:09", True, 213, "DONE_TODAY"),
    (5351, "22:10", False, 230, "FUTURE"),
    (5352, "11:11", False, 247, "FUTURE"),
    (5353, "00:12", True, 264, "DONE_TODAY"),
    (5354, "13:13", False, 281, "FUTURE"),
    (5355, "02:14", False, 298, "DUE"),
    (5356, "15:15", True, 315, "DONE_TODAY"),
    (5357, "04:16", False, 332, "DUE"),
    (5358, "17:17", False, 349, "FUTURE"),
    (5359, "06:18", True, 366, "DONE_TODAY"),
    (5360, "19:19", False, 383, "FUTURE"),
    (5361, "08:20", False, 400, "FUTURE"),
    (5362, "21:21", True, 417, "DONE_TODAY"),
    (5363, "10:22", False, 434, "FUTURE"),
    (5364, "23:23", False, 451, "FUTURE"),
    (5365, "12:24", True, 468, "DONE_TODAY"),
    (5366, "01:25", False, 485, "DUE"),
    (5367, "14:26", False, 502, "FUTURE"),
    (5368, "03:27", True, 519, "DONE_TODAY"),
    (5369, "16:28", False, 536, "FUTURE"),
    (5370, "05:29", False, 553, "DUE"),
    (5371, "18:30", True, 570, "DONE_TODAY"),
    (5372, "07:31", False, 587, "DUE"),
    (5373, "20:32", False, 604, "FUTURE"),
    (5374, "09:33", True, 621, "DONE_TODAY"),
    (5375, "22:34", False, 638, "FUTURE"),
    (5376, "11:35", False, 655, "FUTURE"),
    (5377, "00:36", True, 672, "DONE_TODAY"),
    (5378, "13:37", False, 689, "FUTURE"),
    (5379, "02:38", False, 706, "DUE"),
    (5380, "15:39", True, 723, "DONE_TODAY"),
    (5381, "04:40", False, 740, "DUE"),
    (5382, "17:41", False, 757, "FUTURE"),
    (5383, "06:42", True, 774, "DONE_TODAY"),
    (5384, "19:43", False, 791, "FUTURE"),
    (5385, "08:44", False, 808, "DUE"),
    (5386, "21:45", True, 825, "DONE_TODAY"),
    (5387, "10:46", False, 842, "DUE"),
    (5388, "23:47", False, 859, "FUTURE"),
    (5389, "12:48", True, 876, "DONE_TODAY"),
    (5390, "01:49", False, 893, "DUE"),
    (5391, "14:50", False, 910, "DUE"),
    (5392, "03:51", True, 927, "DONE_TODAY"),
    (5393, "16:52", False, 944, "FUTURE"),
    (5394, "05:53", False, 961, "DUE"),
    (5395, "18:54", True, 978, "DONE_TODAY"),
    (5396, "07:55", False, 995, "DUE"),
    (5397, "20:56", False, 1012, "FUTURE"),
    (5398, "09:57", True, 1029, "DONE_TODAY"),
    (5399, "22:58", False, 1046, "FUTURE"),
    (5400, "11:59", False, 1063, "DUE"),
    (5401, "00:00", True, 1080, "DONE_TODAY"),
    (5402, "13:01", False, 1097, "DUE"),
    (5403, "02:02", False, 1114, "DUE"),
    (5404, "15:03", True, 1131, "DONE_TODAY"),
    (5405, "04:04", False, 1148, "DUE"),
    (5406, "17:05", False, 1165, "DUE"),
    (5407, "06:06", True, 1182, "DONE_TODAY"),
    (5408, "19:07", False, 1199, "DUE"),
    (5409, "08:08", False, 1216, "DUE"),
    (5410, "21:09", True, 1233, "DONE_TODAY"),
    (5411, "10:10", False, 1250, "DUE"),
    (5412, "23:11", False, 1267, "FUTURE"),
    (5413, "12:12", True, 1284, "DONE_TODAY"),
    (5414, "01:13", False, 1301, "DUE"),
    (5415, "14:14", False, 1318, "DUE"),
    (5416, "03:15", True, 1335, "DONE_TODAY"),
    (5417, "16:16", False, 1352, "DUE"),
    (5418, "05:17", False, 1369, "DUE"),
    (5419, "18:18", True, 1386, "DONE_TODAY"),
    (5420, "07:19", False, 1403, "DUE"),
    (5421, "20:20", False, 1420, "DUE"),
    (5422, "09:21", True, 1437, "DONE_TODAY"),
    (5423, "22:22", False, 14, "FUTURE"),
    (5424, "11:23", False, 31, "FUTURE"),
    (5425, "00:24", True, 48, "DONE_TODAY"),
    (5426, "13:25", False, 65, "FUTURE"),
    (5427, "02:26", False, 82, "FUTURE"),
    (5428, "15:27", True, 99, "DONE_TODAY"),
    (5429, "04:28", False, 116, "FUTURE"),
    (5430, "17:29", False, 133, "FUTURE"),
    (5431, "06:30", True, 150, "DONE_TODAY"),
    (5432, "19:31", False, 167, "FUTURE"),
    (5433, "08:32", False, 184, "FUTURE"),
    (5434, "21:33", True, 201, "DONE_TODAY"),
    (5435, "10:34", False, 218, "FUTURE"),
    (5436, "23:35", False, 235, "FUTURE"),
    (5437, "12:36", True, 252, "DONE_TODAY"),
    (5438, "01:37", False, 269, "DUE"),
    (5439, "14:38", False, 286, "FUTURE"),
    (5440, "03:39", True, 303, "DONE_TODAY"),
    (5441, "16:40", False, 320, "FUTURE"),
    (5442, "05:41", False, 337, "FUTURE"),
    (5443, "18:42", True, 354, "DONE_TODAY"),
    (5444, "07:43", False, 371, "FUTURE"),
    (5445, "20:44", False, 388, "FUTURE"),
    (5446, "09:45", True, 405, "DONE_TODAY"),
    (5447, "22:46", False, 422, "FUTURE"),
    (5448, "11:47", False, 439, "FUTURE"),
    (5449, "00:48", True, 456, "DONE_TODAY"),
    (5450, "13:49", False, 473, "FUTURE"),
    (5451, "02:50", False, 490, "DUE"),
    (5452, "15:51", True, 507, "DONE_TODAY"),
    (5453, "04:52", False, 524, "DUE"),
    (5454, "17:53", False, 541, "FUTURE"),
    (5455, "06:54", True, 558, "DONE_TODAY"),
    (5456, "19:55", False, 575, "FUTURE"),
    (5457, "08:56", False, 592, "DUE"),
    (5458, "21:57", True, 609, "DONE_TODAY"),
    (5459, "10:58", False, 626, "FUTURE"),
    (5460, "23:59", False, 643, "FUTURE"),
    (5461, "12:00", True, 660, "DONE_TODAY"),
    (5462, "01:01", False, 677, "DUE"),
    (5463, "14:02", False, 694, "FUTURE"),
    (5464, "03:03", True, 711, "DONE_TODAY"),
    (5465, "16:04", False, 728, "FUTURE"),
    (5466, "05:05", False, 745, "DUE"),
    (5467, "18:06", True, 762, "DONE_TODAY"),
    (5468, "07:07", False, 779, "DUE"),
    (5469, "20:08", False, 796, "FUTURE"),
    (5470, "09:09", True, 813, "DONE_TODAY"),
    (5471, "22:10", False, 830, "FUTURE"),
    (5472, "11:11", False, 847, "DUE"),
    (5473, "00:12", True, 864, "DONE_TODAY"),
    (5474, "13:13", False, 881, "DUE"),
    (5475, "02:14", False, 898, "DUE"),
    (5476, "15:15", True, 915, "DONE_TODAY"),
    (5477, "04:16", False, 932, "DUE"),
    (5478, "17:17", False, 949, "FUTURE"),
    (5479, "06:18", True, 966, "DONE_TODAY"),
    (5480, "19:19", False, 983, "FUTURE"),
    (5481, "08:20", False, 1000, "DUE"),
    (5482, "21:21", True, 1017, "DONE_TODAY"),
    (5483, "10:22", False, 1034, "DUE"),
    (5484, "23:23", False, 1051, "FUTURE"),
    (5485, "12:24", True, 1068, "DONE_TODAY"),
    (5486, "01:25", False, 1085, "DUE"),
    (5487, "14:26", False, 1102, "DUE"),
    (5488, "03:27", True, 1119, "DONE_TODAY"),
    (5489, "16:28", False, 1136, "DUE"),
    (5490, "05:29", False, 1153, "DUE"),
    (5491, "18:30", True, 1170, "DONE_TODAY"),
    (5492, "07:31", False, 1187, "DUE"),
    (5493, "20:32", False, 1204, "FUTURE"),
    (5494, "09:33", True, 1221, "DONE_TODAY"),
    (5495, "22:34", False, 1238, "FUTURE"),
    (5496, "11:35", False, 1255, "DUE"),
    (5497, "00:36", True, 1272, "DONE_TODAY"),
    (5498, "13:37", False, 1289, "DUE"),
    (5499, "02:38", False, 1306, "DUE"),
    (5500, "15:39", True, 1323, "DONE_TODAY"),
    (5501, "04:40", False, 1340, "DUE"),
    (5502, "17:41", False, 1357, "DUE"),
    (5503, "06:42", True, 1374, "DONE_TODAY"),
    (5504, "19:43", False, 1391, "DUE"),
    (5505, "08:44", False, 1408, "DUE"),
    (5506, "21:45", True, 1425, "DONE_TODAY"),
    (5507, "10:46", False, 2, "FUTURE"),
    (5508, "23:47", False, 19, "FUTURE"),
    (5509, "12:48", True, 36, "DONE_TODAY"),
    (5510, "01:49", False, 53, "FUTURE"),
    (5511, "14:50", False, 70, "FUTURE"),
    (5512, "03:51", True, 87, "DONE_TODAY"),
    (5513, "16:52", False, 104, "FUTURE"),
    (5514, "05:53", False, 121, "FUTURE"),
    (5515, "18:54", True, 138, "DONE_TODAY"),
    (5516, "07:55", False, 155, "FUTURE"),
    (5517, "20:56", False, 172, "FUTURE"),
    (5518, "09:57", True, 189, "DONE_TODAY"),
    (5519, "22:58", False, 206, "FUTURE"),
    (5520, "11:59", False, 223, "FUTURE"),
    (5521, "00:00", True, 240, "DONE_TODAY"),
    (5522, "13:01", False, 257, "FUTURE"),
    (5523, "02:02", False, 274, "DUE"),
    (5524, "15:03", True, 291, "DONE_TODAY"),
    (5525, "04:04", False, 308, "DUE"),
    (5526, "17:05", False, 325, "FUTURE"),
    (5527, "06:06", True, 342, "DONE_TODAY"),
    (5528, "19:07", False, 359, "FUTURE"),
    (5529, "08:08", False, 376, "FUTURE"),
    (5530, "21:09", True, 393, "DONE_TODAY"),
    (5531, "10:10", False, 410, "FUTURE"),
    (5532, "23:11", False, 427, "FUTURE"),
    (5533, "12:12", True, 444, "DONE_TODAY"),
    (5534, "01:13", False, 461, "DUE"),
    (5535, "14:14", False, 478, "FUTURE"),
    (5536, "03:15", True, 495, "DONE_TODAY"),
    (5537, "16:16", False, 512, "FUTURE"),
    (5538, "05:17", False, 529, "DUE"),
    (5539, "18:18", True, 546, "DONE_TODAY"),
    (5540, "07:19", False, 563, "DUE"),
    (5541, "20:20", False, 580, "FUTURE"),
    (5542, "09:21", True, 597, "DONE_TODAY"),
    (5543, "22:22", False, 614, "FUTURE"),
    (5544, "11:23", False, 631, "FUTURE"),
    (5545, "00:24", True, 648, "DONE_TODAY"),
    (5546, "13:25", False, 665, "FUTURE"),
    (5547, "02:26", False, 682, "DUE"),
    (5548, "15:27", True, 699, "DONE_TODAY"),
    (5549, "04:28", False, 716, "DUE"),
    (5550, "17:29", False, 733, "FUTURE"),
    (5551, "06:30", True, 750, "DONE_TODAY"),
    (5552, "19:31", False, 767, "FUTURE"),
    (5553, "08:32", False, 784, "DUE"),
    (5554, "21:33", True, 801, "DONE_TODAY"),
    (5555, "10:34", False, 818, "DUE"),
    (5556, "23:35", False, 835, "FUTURE"),
    (5557, "12:36", True, 852, "DONE_TODAY"),
    (5558, "01:37", False, 869, "DUE"),
    (5559, "14:38", False, 886, "DUE"),
    (5560, "03:39", True, 903, "DONE_TODAY"),
    (5561, "16:40", False, 920, "FUTURE"),
    (5562, "05:41", False, 937, "DUE"),
    (5563, "18:42", True, 954, "DONE_TODAY"),
    (5564, "07:43", False, 971, "DUE"),
    (5565, "20:44", False, 988, "FUTURE"),
    (5566, "09:45", True, 1005, "DONE_TODAY"),
    (5567, "22:46", False, 1022, "FUTURE"),
    (5568, "11:47", False, 1039, "DUE"),
    (5569, "00:48", True, 1056, "DONE_TODAY"),
    (5570, "13:49", False, 1073, "DUE"),
    (5571, "02:50", False, 1090, "DUE"),
    (5572, "15:51", True, 1107, "DONE_TODAY"),
    (5573, "04:52", False, 1124, "DUE"),
    (5574, "17:53", False, 1141, "DUE"),
    (5575, "06:54", True, 1158, "DONE_TODAY"),
    (5576, "19:55", False, 1175, "FUTURE"),
    (5577, "08:56", False, 1192, "DUE"),
    (5578, "21:57", True, 1209, "DONE_TODAY"),
    (5579, "10:58", False, 1226, "DUE"),
    (5580, "23:59", False, 1243, "FUTURE"),
    (5581, "12:00", True, 1260, "DONE_TODAY"),
    (5582, "01:01", False, 1277, "DUE"),
    (5583, "14:02", False, 1294, "DUE"),
    (5584, "03:03", True, 1311, "DONE_TODAY"),
    (5585, "16:04", False, 1328, "DUE"),
    (5586, "05:05", False, 1345, "DUE"),
    (5587, "18:06", True, 1362, "DONE_TODAY"),
    (5588, "07:07", False, 1379, "DUE"),
    (5589, "20:08", False, 1396, "DUE"),
    (5590, "09:09", True, 1413, "DONE_TODAY"),
    (5591, "22:10", False, 1430, "DUE"),
    (5592, "11:11", False, 7, "FUTURE"),
    (5593, "00:12", True, 24, "DONE_TODAY"),
    (5594, "13:13", False, 41, "FUTURE"),
    (5595, "02:14", False, 58, "FUTURE"),
    (5596, "15:15", True, 75, "DONE_TODAY"),
    (5597, "04:16", False, 92, "FUTURE"),
    (5598, "17:17", False, 109, "FUTURE"),
    (5599, "06:18", True, 126, "DONE_TODAY"),
    (5600, "19:19", False, 143, "FUTURE"),
    (5601, "08:20", False, 160, "FUTURE"),
    (5602, "21:21", True, 177, "DONE_TODAY"),
    (5603, "10:22", False, 194, "FUTURE"),
    (5604, "23:23", False, 211, "FUTURE"),
    (5605, "12:24", True, 228, "DONE_TODAY"),
    (5606, "01:25", False, 245, "DUE"),
    (5607, "14:26", False, 262, "FUTURE"),
    (5608, "03:27", True, 279, "DONE_TODAY"),
    (5609, "16:28", False, 296, "FUTURE"),
    (5610, "05:29", False, 313, "FUTURE"),
    (5611, "18:30", True, 330, "DONE_TODAY"),
    (5612, "07:31", False, 347, "FUTURE"),
    (5613, "20:32", False, 364, "FUTURE"),
    (5614, "09:33", True, 381, "DONE_TODAY"),
    (5615, "22:34", False, 398, "FUTURE"),
    (5616, "11:35", False, 415, "FUTURE"),
    (5617, "00:36", True, 432, "DONE_TODAY"),
    (5618, "13:37", False, 449, "FUTURE"),
    (5619, "02:38", False, 466, "DUE"),
    (5620, "15:39", True, 483, "DONE_TODAY"),
    (5621, "04:40", False, 500, "DUE"),
    (5622, "17:41", False, 517, "FUTURE"),
    (5623, "06:42", True, 534, "DONE_TODAY"),
    (5624, "19:43", False, 551, "FUTURE"),
    (5625, "08:44", False, 568, "DUE"),
    (5626, "21:45", True, 585, "DONE_TODAY"),
    (5627, "10:46", False, 602, "FUTURE"),
    (5628, "23:47", False, 619, "FUTURE"),
    (5629, "12:48", True, 636, "DONE_TODAY"),
    (5630, "01:49", False, 653, "DUE"),
    (5631, "14:50", False, 670, "FUTURE"),
    (5632, "03:51", True, 687, "DONE_TODAY"),
    (5633, "16:52", False, 704, "FUTURE"),
    (5634, "05:53", False, 721, "DUE"),
    (5635, "18:54", True, 738, "DONE_TODAY"),
    (5636, "07:55", False, 755, "DUE"),
    (5637, "20:56", False, 772, "FUTURE"),
    (5638, "09:57", True, 789, "DONE_TODAY"),
    (5639, "22:58", False, 806, "FUTURE"),
    (5640, "11:59", False, 823, "DUE"),
    (5641, "00:00", True, 840, "DONE_TODAY"),
    (5642, "13:01", False, 857, "DUE"),
    (5643, "02:02", False, 874, "DUE"),
    (5644, "15:03", True, 891, "DONE_TODAY"),
    (5645, "04:04", False, 908, "DUE"),
    (5646, "17:05", False, 925, "FUTURE"),
    (5647, "06:06", True, 942, "DONE_TODAY"),
    (5648, "19:07", False, 959, "FUTURE"),
    (5649, "08:08", False, 976, "DUE"),
    (5650, "21:09", True, 993, "DONE_TODAY"),
    (5651, "10:10", False, 1010, "DUE"),
    (5652, "23:11", False, 1027, "FUTURE"),
    (5653, "12:12", True, 1044, "DONE_TODAY"),
    (5654, "01:13", False, 1061, "DUE"),
    (5655, "14:14", False, 1078, "DUE"),
    (5656, "03:15", True, 1095, "DONE_TODAY"),
    (5657, "16:16", False, 1112, "DUE"),
    (5658, "05:17", False, 1129, "DUE"),
    (5659, "18:18", True, 1146, "DONE_TODAY"),
    (5660, "07:19", False, 1163, "DUE"),
    (5661, "20:20", False, 1180, "FUTURE"),
    (5662, "09:21", True, 1197, "DONE_TODAY"),
    (5663, "22:22", False, 1214, "FUTURE"),
    (5664, "11:23", False, 1231, "DUE"),
    (5665, "00:24", True, 1248, "DONE_TODAY"),
    (5666, "13:25", False, 1265, "DUE"),
    (5667, "02:26", False, 1282, "DUE"),
    (5668, "15:27", True, 1299, "DONE_TODAY"),
    (5669, "04:28", False, 1316, "DUE"),
    (5670, "17:29", False, 1333, "DUE"),
    (5671, "06:30", True, 1350, "DONE_TODAY"),
    (5672, "19:31", False, 1367, "DUE"),
    (5673, "08:32", False, 1384, "DUE"),
    (5674, "21:33", True, 1401, "DONE_TODAY"),
    (5675, "10:34", False, 1418, "DUE"),
    (5676, "23:35", False, 1435, "DUE"),
    (5677, "12:36", True, 12, "DONE_TODAY"),
    (5678, "01:37", False, 29, "FUTURE"),
    (5679, "14:38", False, 46, "FUTURE"),
    (5680, "03:39", True, 63, "DONE_TODAY"),
    (5681, "16:40", False, 80, "FUTURE"),
    (5682, "05:41", False, 97, "FUTURE"),
    (5683, "18:42", True, 114, "DONE_TODAY"),
    (5684, "07:43", False, 131, "FUTURE"),
    (5685, "20:44", False, 148, "FUTURE"),
    (5686, "09:45", True, 165, "DONE_TODAY"),
    (5687, "22:46", False, 182, "FUTURE"),
    (5688, "11:47", False, 199, "FUTURE"),
    (5689, "00:48", True, 216, "DONE_TODAY"),
    (5690, "13:49", False, 233, "FUTURE"),
    (5691, "02:50", False, 250, "DUE"),
    (5692, "15:51", True, 267, "DONE_TODAY"),
    (5693, "04:52", False, 284, "FUTURE"),
    (5694, "17:53", False, 301, "FUTURE"),
    (5695, "06:54", True, 318, "DONE_TODAY"),
    (5696, "19:55", False, 335, "FUTURE"),
    (5697, "08:56", False, 352, "FUTURE"),
    (5698, "21:57", True, 369, "DONE_TODAY"),
    (5699, "10:58", False, 386, "FUTURE"),
    (5700, "23:59", False, 403, "FUTURE"),
    (5701, "12:00", True, 420, "DONE_TODAY"),
    (5702, "01:01", False, 437, "DUE"),
    (5703, "14:02", False, 454, "FUTURE"),
    (5704, "03:03", True, 471, "DONE_TODAY"),
    (5705, "16:04", False, 488, "FUTURE"),
    (5706, "05:05", False, 505, "DUE"),
    (5707, "18:06", True, 522, "DONE_TODAY"),
    (5708, "07:07", False, 539, "DUE"),
    (5709, "20:08", False, 556, "FUTURE"),
    (5710, "09:09", True, 573, "DONE_TODAY"),
    (5711, "22:10", False, 590, "FUTURE"),
    (5712, "11:11", False, 607, "FUTURE"),
    (5713, "00:12", True, 624, "DONE_TODAY"),
    (5714, "13:13", False, 641, "FUTURE"),
    (5715, "02:14", False, 658, "DUE"),
    (5716, "15:15", True, 675, "DONE_TODAY"),
    (5717, "04:16", False, 692, "DUE"),
    (5718, "17:17", False, 709, "FUTURE"),
    (5719, "06:18", True, 726, "DONE_TODAY"),
    (5720, "19:19", False, 743, "FUTURE"),
    (5721, "08:20", False, 760, "DUE"),
    (5722, "21:21", True, 777, "DONE_TODAY"),
    (5723, "10:22", False, 794, "DUE"),
    (5724, "23:23", False, 811, "FUTURE"),
    (5725, "12:24", True, 828, "DONE_TODAY"),
    (5726, "01:25", False, 845, "DUE"),
    (5727, "14:26", False, 862, "FUTURE"),
    (5728, "03:27", True, 879, "DONE_TODAY"),
    (5729, "16:28", False, 896, "FUTURE"),
    (5730, "05:29", False, 913, "DUE"),
    (5731, "18:30", True, 930, "DONE_TODAY"),
    (5732, "07:31", False, 947, "DUE"),
    (5733, "20:32", False, 964, "FUTURE"),
    (5734, "09:33", True, 981, "DONE_TODAY"),
    (5735, "22:34", False, 998, "FUTURE"),
    (5736, "11:35", False, 1015, "DUE"),
    (5737, "00:36", True, 1032, "DONE_TODAY"),
    (5738, "13:37", False, 1049, "DUE"),
    (5739, "02:38", False, 1066, "DUE"),
    (5740, "15:39", True, 1083, "DONE_TODAY"),
    (5741, "04:40", False, 1100, "DUE"),
    (5742, "17:41", False, 1117, "DUE"),
    (5743, "06:42", True, 1134, "DONE_TODAY"),
    (5744, "19:43", False, 1151, "FUTURE"),
    (5745, "08:44", False, 1168, "DUE"),
    (5746, "21:45", True, 1185, "DONE_TODAY"),
    (5747, "10:46", False, 1202, "DUE"),
    (5748, "23:47", False, 1219, "FUTURE"),
    (5749, "12:48", True, 1236, "DONE_TODAY"),
    (5750, "01:49", False, 1253, "DUE"),
    (5751, "14:50", False, 1270, "DUE"),
    (5752, "03:51", True, 1287, "DONE_TODAY"),
    (5753, "16:52", False, 1304, "DUE"),
    (5754, "05:53", False, 1321, "DUE"),
    (5755, "18:54", True, 1338, "DONE_TODAY"),
    (5756, "07:55", False, 1355, "DUE"),
    (5757, "20:56", False, 1372, "DUE"),
    (5758, "09:57", True, 1389, "DONE_TODAY"),
    (5759, "22:58", False, 1406, "DUE"),
    (5760, "11:59", False, 1423, "DUE"),
    (5761, "00:00", True, 0, "DONE_TODAY"),
    (5762, "13:01", False, 17, "FUTURE"),
    (5763, "02:02", False, 34, "FUTURE"),
    (5764, "15:03", True, 51, "DONE_TODAY"),
    (5765, "04:04", False, 68, "FUTURE"),
    (5766, "17:05", False, 85, "FUTURE"),
    (5767, "06:06", True, 102, "DONE_TODAY"),
    (5768, "19:07", False, 119, "FUTURE"),
    (5769, "08:08", False, 136, "FUTURE"),
    (5770, "21:09", True, 153, "DONE_TODAY"),
    (5771, "10:10", False, 170, "FUTURE"),
    (5772, "23:11", False, 187, "FUTURE"),
    (5773, "12:12", True, 204, "DONE_TODAY"),
    (5774, "01:13", False, 221, "DUE"),
    (5775, "14:14", False, 238, "FUTURE"),
    (5776, "03:15", True, 255, "DONE_TODAY"),
    (5777, "16:16", False, 272, "FUTURE"),
    (5778, "05:17", False, 289, "FUTURE"),
    (5779, "18:18", True, 306, "DONE_TODAY"),
    (5780, "07:19", False, 323, "FUTURE"),
    (5781, "20:20", False, 340, "FUTURE"),
    (5782, "09:21", True, 357, "DONE_TODAY"),
    (5783, "22:22", False, 374, "FUTURE"),
    (5784, "11:23", False, 391, "FUTURE"),
    (5785, "00:24", True, 408, "DONE_TODAY"),
    (5786, "13:25", False, 425, "FUTURE"),
    (5787, "02:26", False, 442, "DUE"),
    (5788, "15:27", True, 459, "DONE_TODAY"),
    (5789, "04:28", False, 476, "DUE"),
    (5790, "17:29", False, 493, "FUTURE"),
    (5791, "06:30", True, 510, "DONE_TODAY"),
    (5792, "19:31", False, 527, "FUTURE"),
    (5793, "08:32", False, 544, "DUE"),
    (5794, "21:33", True, 561, "DONE_TODAY"),
    (5795, "10:34", False, 578, "FUTURE"),
    (5796, "23:35", False, 595, "FUTURE"),
    (5797, "12:36", True, 612, "DONE_TODAY"),
    (5798, "01:37", False, 629, "DUE"),
    (5799, "14:38", False, 646, "FUTURE"),
    (5800, "03:39", True, 663, "DONE_TODAY"),
    (5801, "16:40", False, 680, "FUTURE"),
    (5802, "05:41", False, 697, "DUE"),
    (5803, "18:42", True, 714, "DONE_TODAY"),
    (5804, "07:43", False, 731, "DUE"),
    (5805, "20:44", False, 748, "FUTURE"),
    (5806, "09:45", True, 765, "DONE_TODAY"),
    (5807, "22:46", False, 782, "FUTURE"),
    (5808, "11:47", False, 799, "DUE"),
    (5809, "00:48", True, 816, "DONE_TODAY"),
    (5810, "13:49", False, 833, "DUE"),
    (5811, "02:50", False, 850, "DUE"),
    (5812, "15:51", True, 867, "DONE_TODAY"),
    (5813, "04:52", False, 884, "DUE"),
    (5814, "17:53", False, 901, "FUTURE"),
    (5815, "06:54", True, 918, "DONE_TODAY"),
    (5816, "19:55", False, 935, "FUTURE"),
    (5817, "08:56", False, 952, "DUE"),
    (5818, "21:57", True, 969, "DONE_TODAY"),
    (5819, "10:58", False, 986, "DUE"),
    (5820, "23:59", False, 1003, "FUTURE"),
    (5821, "12:00", True, 1020, "DONE_TODAY"),
    (5822, "01:01", False, 1037, "DUE"),
    (5823, "14:02", False, 1054, "DUE"),
    (5824, "03:03", True, 1071, "DONE_TODAY"),
    (5825, "16:04", False, 1088, "DUE"),
    (5826, "05:05", False, 1105, "DUE"),
    (5827, "18:06", True, 1122, "DONE_TODAY"),
    (5828, "07:07", False, 1139, "DUE"),
    (5829, "20:08", False, 1156, "FUTURE"),
    (5830, "09:09", True, 1173, "DONE_TODAY"),
    (5831, "22:10", False, 1190, "FUTURE"),
    (5832, "11:11", False, 1207, "DUE"),
    (5833, "00:12", True, 1224, "DONE_TODAY"),
    (5834, "13:13", False, 1241, "DUE"),
    (5835, "02:14", False, 1258, "DUE"),
    (5836, "15:15", True, 1275, "DONE_TODAY"),
    (5837, "04:16", False, 1292, "DUE"),
    (5838, "17:17", False, 1309, "DUE"),
    (5839, "06:18", True, 1326, "DONE_TODAY"),
    (5840, "19:19", False, 1343, "DUE"),
    (5841, "08:20", False, 1360, "DUE"),
    (5842, "21:21", True, 1377, "DONE_TODAY"),
    (5843, "10:22", False, 1394, "DUE"),
    (5844, "23:23", False, 1411, "DUE"),
    (5845, "12:24", True, 1428, "DONE_TODAY"),
    (5846, "01:25", False, 5, "FUTURE"),
    (5847, "14:26", False, 22, "FUTURE"),
    (5848, "03:27", True, 39, "DONE_TODAY"),
    (5849, "16:28", False, 56, "FUTURE"),
    (5850, "05:29", False, 73, "FUTURE"),
    (5851, "18:30", True, 90, "DONE_TODAY"),
    (5852, "07:31", False, 107, "FUTURE"),
    (5853, "20:32", False, 124, "FUTURE"),
    (5854, "09:33", True, 141, "DONE_TODAY"),
    (5855, "22:34", False, 158, "FUTURE"),
    (5856, "11:35", False, 175, "FUTURE"),
    (5857, "00:36", True, 192, "DONE_TODAY"),
    (5858, "13:37", False, 209, "FUTURE"),
    (5859, "02:38", False, 226, "DUE"),
    (5860, "15:39", True, 243, "DONE_TODAY"),
    (5861, "04:40", False, 260, "FUTURE"),
    (5862, "17:41", False, 277, "FUTURE"),
    (5863, "06:42", True, 294, "DONE_TODAY"),
    (5864, "19:43", False, 311, "FUTURE"),
    (5865, "08:44", False, 328, "FUTURE"),
    (5866, "21:45", True, 345, "DONE_TODAY"),
    (5867, "10:46", False, 362, "FUTURE"),
    (5868, "23:47", False, 379, "FUTURE"),
    (5869, "12:48", True, 396, "DONE_TODAY"),
    (5870, "01:49", False, 413, "DUE"),
    (5871, "14:50", False, 430, "FUTURE"),
    (5872, "03:51", True, 447, "DONE_TODAY"),
    (5873, "16:52", False, 464, "FUTURE"),
    (5874, "05:53", False, 481, "DUE"),
    (5875, "18:54", True, 498, "DONE_TODAY"),
    (5876, "07:55", False, 515, "DUE"),
    (5877, "20:56", False, 532, "FUTURE"),
    (5878, "09:57", True, 549, "DONE_TODAY"),
    (5879, "22:58", False, 566, "FUTURE"),
    (5880, "11:59", False, 583, "FUTURE"),
    (5881, "00:00", True, 600, "DONE_TODAY"),
    (5882, "13:01", False, 617, "FUTURE"),
    (5883, "02:02", False, 634, "DUE"),
    (5884, "15:03", True, 651, "DONE_TODAY"),
    (5885, "04:04", False, 668, "DUE"),
    (5886, "17:05", False, 685, "FUTURE"),
    (5887, "06:06", True, 702, "DONE_TODAY"),
    (5888, "19:07", False, 719, "FUTURE"),
    (5889, "08:08", False, 736, "DUE"),
    (5890, "21:09", True, 753, "DONE_TODAY"),
    (5891, "10:10", False, 770, "DUE"),
    (5892, "23:11", False, 787, "FUTURE"),
    (5893, "12:12", True, 804, "DONE_TODAY"),
    (5894, "01:13", False, 821, "DUE"),
    (5895, "14:14", False, 838, "FUTURE"),
    (5896, "03:15", True, 855, "DONE_TODAY"),
    (5897, "16:16", False, 872, "FUTURE"),
    (5898, "05:17", False, 889, "DUE"),
    (5899, "18:18", True, 906, "DONE_TODAY"),
    (5900, "07:19", False, 923, "DUE"),
    (5901, "20:20", False, 940, "FUTURE"),
    (5902, "09:21", True, 957, "DONE_TODAY"),
    (5903, "22:22", False, 974, "FUTURE"),
    (5904, "11:23", False, 991, "DUE"),
    (5905, "00:24", True, 1008, "DONE_TODAY"),
    (5906, "13:25", False, 1025, "DUE"),
    (5907, "02:26", False, 1042, "DUE"),
    (5908, "15:27", True, 1059, "DONE_TODAY"),
    (5909, "04:28", False, 1076, "DUE"),
    (5910, "17:29", False, 1093, "DUE"),
    (5911, "06:30", True, 1110, "DONE_TODAY"),
    (5912, "19:31", False, 1127, "FUTURE"),
    (5913, "08:32", False, 1144, "DUE"),
    (5914, "21:33", True, 1161, "DONE_TODAY"),
    (5915, "10:34", False, 1178, "DUE"),
    (5916, "23:35", False, 1195, "FUTURE"),
    (5917, "12:36", True, 1212, "DONE_TODAY"),
    (5918, "01:37", False, 1229, "DUE"),
    (5919, "14:38", False, 1246, "DUE"),
    (5920, "03:39", True, 1263, "DONE_TODAY"),
    (5921, "16:40", False, 1280, "DUE"),
    (5922, "05:41", False, 1297, "DUE"),
    (5923, "18:42", True, 1314, "DONE_TODAY"),
    (5924, "07:43", False, 1331, "DUE"),
    (5925, "20:44", False, 1348, "DUE"),
    (5926, "09:45", True, 1365, "DONE_TODAY"),
    (5927, "22:46", False, 1382, "DUE"),
    (5928, "11:47", False, 1399, "DUE"),
    (5929, "00:48", True, 1416, "DONE_TODAY"),
    (5930, "13:49", False, 1433, "DUE"),
    (5931, "02:50", False, 10, "FUTURE"),
    (5932, "15:51", True, 27, "DONE_TODAY"),
    (5933, "04:52", False, 44, "FUTURE"),
    (5934, "17:53", False, 61, "FUTURE"),
    (5935, "06:54", True, 78, "DONE_TODAY"),
    (5936, "19:55", False, 95, "FUTURE"),
    (5937, "08:56", False, 112, "FUTURE"),
    (5938, "21:57", True, 129, "DONE_TODAY"),
    (5939, "10:58", False, 146, "FUTURE"),
    (5940, "23:59", False, 163, "FUTURE"),
    (5941, "12:00", True, 180, "DONE_TODAY"),
    (5942, "01:01", False, 197, "DUE"),
    (5943, "14:02", False, 214, "FUTURE"),
    (5944, "03:03", True, 231, "DONE_TODAY"),
    (5945, "16:04", False, 248, "FUTURE"),
    (5946, "05:05", False, 265, "FUTURE"),
    (5947, "18:06", True, 282, "DONE_TODAY"),
    (5948, "07:07", False, 299, "FUTURE"),
    (5949, "20:08", False, 316, "FUTURE"),
    (5950, "09:09", True, 333, "DONE_TODAY"),
    (5951, "22:10", False, 350, "FUTURE"),
    (5952, "11:11", False, 367, "FUTURE"),
    (5953, "00:12", True, 384, "DONE_TODAY"),
    (5954, "13:13", False, 401, "FUTURE"),
    (5955, "02:14", False, 418, "DUE"),
    (5956, "15:15", True, 435, "DONE_TODAY"),
    (5957, "04:16", False, 452, "DUE"),
    (5958, "17:17", False, 469, "FUTURE"),
    (5959, "06:18", True, 486, "DONE_TODAY"),
    (5960, "19:19", False, 503, "FUTURE"),
    (5961, "08:20", False, 520, "DUE"),
    (5962, "21:21", True, 537, "DONE_TODAY"),
    (5963, "10:22", False, 554, "FUTURE"),
    (5964, "23:23", False, 571, "FUTURE"),
    (5965, "12:24", True, 588, "DONE_TODAY"),
    (5966, "01:25", False, 605, "DUE"),
    (5967, "14:26", False, 622, "FUTURE"),
    (5968, "03:27", True, 639, "DONE_TODAY"),
    (5969, "16:28", False, 656, "FUTURE"),
    (5970, "05:29", False, 673, "DUE"),
    (5971, "18:30", True, 690, "DONE_TODAY"),
    (5972, "07:31", False, 707, "DUE"),
    (5973, "20:32", False, 724, "FUTURE"),
    (5974, "09:33", True, 741, "DONE_TODAY"),
    (5975, "22:34", False, 758, "FUTURE"),
    (5976, "11:35", False, 775, "DUE"),
    (5977, "00:36", True, 792, "DONE_TODAY"),
    (5978, "13:37", False, 809, "FUTURE"),
    (5979, "02:38", False, 826, "DUE"),
    (5980, "15:39", True, 843, "DONE_TODAY"),
    (5981, "04:40", False, 860, "DUE"),
    (5982, "17:41", False, 877, "FUTURE"),
    (5983, "06:42", True, 894, "DONE_TODAY"),
    (5984, "19:43", False, 911, "FUTURE"),
    (5985, "08:44", False, 928, "DUE"),
    (5986, "21:45", True, 945, "DONE_TODAY"),
    (5987, "10:46", False, 962, "DUE"),
    (5988, "23:47", False, 979, "FUTURE"),
    (5989, "12:48", True, 996, "DONE_TODAY"),
    (5990, "01:49", False, 1013, "DUE"),
    (5991, "14:50", False, 1030, "DUE"),
    (5992, "03:51", True, 1047, "DONE_TODAY"),
    (5993, "16:52", False, 1064, "DUE"),
    (5994, "05:53", False, 1081, "DUE"),
    (5995, "18:54", True, 1098, "DONE_TODAY"),
    (5996, "07:55", False, 1115, "DUE"),
    (5997, "20:56", False, 1132, "FUTURE"),
    (5998, "09:57", True, 1149, "DONE_TODAY"),
    (5999, "22:58", False, 1166, "FUTURE"),
    (6000, "11:59", False, 1183, "DUE"),
    (6001, "00:00", True, 1200, "DONE_TODAY"),
    (6002, "13:01", False, 1217, "DUE"),
    (6003, "02:02", False, 1234, "DUE"),
    (6004, "15:03", True, 1251, "DONE_TODAY"),
    (6005, "04:04", False, 1268, "DUE"),
    (6006, "17:05", False, 1285, "DUE"),
    (6007, "06:06", True, 1302, "DONE_TODAY"),
    (6008, "19:07", False, 1319, "DUE"),
    (6009, "08:08", False, 1336, "DUE"),
    (6010, "21:09", True, 1353, "DONE_TODAY"),
    (6011, "10:10", False, 1370, "DUE"),
    (6012, "23:11", False, 1387, "FUTURE"),
    (6013, "12:12", True, 1404, "DONE_TODAY"),
    (6014, "01:13", False, 1421, "DUE"),
    (6015, "14:14", False, 1438, "DUE"),
    (6016, "03:15", True, 15, "DONE_TODAY"),
    (6017, "16:16", False, 32, "FUTURE"),
    (6018, "05:17", False, 49, "FUTURE"),
    (6019, "18:18", True, 66, "DONE_TODAY"),
    (6020, "07:19", False, 83, "FUTURE"),
    (6021, "20:20", False, 100, "FUTURE"),
    (6022, "09:21", True, 117, "DONE_TODAY"),
    (6023, "22:22", False, 134, "FUTURE"),
    (6024, "11:23", False, 151, "FUTURE"),
    (6025, "00:24", True, 168, "DONE_TODAY"),
    (6026, "13:25", False, 185, "FUTURE"),
    (6027, "02:26", False, 202, "DUE"),
    (6028, "15:27", True, 219, "DONE_TODAY"),
    (6029, "04:28", False, 236, "FUTURE"),
    (6030, "17:29", False, 253, "FUTURE"),
    (6031, "06:30", True, 270, "DONE_TODAY"),
    (6032, "19:31", False, 287, "FUTURE"),
    (6033, "08:32", False, 304, "FUTURE"),
    (6034, "21:33", True, 321, "DONE_TODAY"),
    (6035, "10:34", False, 338, "FUTURE"),
    (6036, "23:35", False, 355, "FUTURE"),
    (6037, "12:36", True, 372, "DONE_TODAY"),
    (6038, "01:37", False, 389, "DUE"),
    (6039, "14:38", False, 406, "FUTURE"),
    (6040, "03:39", True, 423, "DONE_TODAY"),
    (6041, "16:40", False, 440, "FUTURE"),
    (6042, "05:41", False, 457, "DUE"),
    (6043, "18:42", True, 474, "DONE_TODAY"),
    (6044, "07:43", False, 491, "DUE"),
    (6045, "20:44", False, 508, "FUTURE"),
    (6046, "09:45", True, 525, "DONE_TODAY"),
    (6047, "22:46", False, 542, "FUTURE"),
    (6048, "11:47", False, 559, "FUTURE"),
    (6049, "00:48", True, 576, "DONE_TODAY"),
    (6050, "13:49", False, 593, "FUTURE"),
    (6051, "02:50", False, 610, "DUE"),
    (6052, "15:51", True, 627, "DONE_TODAY"),
    (6053, "04:52", False, 644, "DUE"),
    (6054, "17:53", False, 661, "FUTURE"),
    (6055, "06:54", True, 678, "DONE_TODAY"),
    (6056, "19:55", False, 695, "FUTURE"),
    (6057, "08:56", False, 712, "DUE"),
    (6058, "21:57", True, 729, "DONE_TODAY"),
    (6059, "10:58", False, 746, "DUE"),
    (6060, "23:59", False, 763, "FUTURE"),
    (6061, "12:00", True, 780, "DONE_TODAY"),
    (6062, "01:01", False, 797, "DUE"),
    (6063, "14:02", False, 814, "FUTURE"),
    (6064, "03:03", True, 831, "DONE_TODAY"),
    (6065, "16:04", False, 848, "FUTURE"),
    (6066, "05:05", False, 865, "DUE"),
    (6067, "18:06", True, 882, "DONE_TODAY"),
    (6068, "07:07", False, 899, "DUE"),
    (6069, "20:08", False, 916, "FUTURE"),
    (6070, "09:09", True, 933, "DONE_TODAY"),
    (6071, "22:10", False, 950, "FUTURE"),
    (6072, "11:11", False, 967, "DUE"),
    (6073, "00:12", True, 984, "DONE_TODAY"),
    (6074, "13:13", False, 1001, "DUE"),
    (6075, "02:14", False, 1018, "DUE"),
    (6076, "15:15", True, 1035, "DONE_TODAY"),
    (6077, "04:16", False, 1052, "DUE"),
    (6078, "17:17", False, 1069, "DUE"),
    (6079, "06:18", True, 1086, "DONE_TODAY"),
    (6080, "19:19", False, 1103, "FUTURE"),
    (6081, "08:20", False, 1120, "DUE"),
    (6082, "21:21", True, 1137, "DONE_TODAY"),
    (6083, "10:22", False, 1154, "DUE"),
    (6084, "23:23", False, 1171, "FUTURE"),
    (6085, "12:24", True, 1188, "DONE_TODAY"),
    (6086, "01:25", False, 1205, "DUE"),
    (6087, "14:26", False, 1222, "DUE"),
    (6088, "03:27", True, 1239, "DONE_TODAY"),
    (6089, "16:28", False, 1256, "DUE"),
    (6090, "05:29", False, 1273, "DUE"),
    (6091, "18:30", True, 1290, "DONE_TODAY"),
    (6092, "07:31", False, 1307, "DUE"),
    (6093, "20:32", False, 1324, "DUE"),
    (6094, "09:33", True, 1341, "DONE_TODAY"),
    (6095, "22:34", False, 1358, "DUE"),
    (6096, "11:35", False, 1375, "DUE"),
    (6097, "00:36", True, 1392, "DONE_TODAY"),
    (6098, "13:37", False, 1409, "DUE"),
    (6099, "02:38", False, 1426, "DUE"),
    (6100, "15:39", True, 3, "DONE_TODAY"),
    (6101, "04:40", False, 20, "FUTURE"),
    (6102, "17:41", False, 37, "FUTURE"),
    (6103, "06:42", True, 54, "DONE_TODAY"),
    (6104, "19:43", False, 71, "FUTURE"),
    (6105, "08:44", False, 88, "FUTURE"),
    (6106, "21:45", True, 105, "DONE_TODAY"),
    (6107, "10:46", False, 122, "FUTURE"),
    (6108, "23:47", False, 139, "FUTURE"),
    (6109, "12:48", True, 156, "DONE_TODAY"),
    (6110, "01:49", False, 173, "DUE"),
    (6111, "14:50", False, 190, "FUTURE"),
    (6112, "03:51", True, 207, "DONE_TODAY"),
    (6113, "16:52", False, 224, "FUTURE"),
    (6114, "05:53", False, 241, "FUTURE"),
    (6115, "18:54", True, 258, "DONE_TODAY"),
    (6116, "07:55", False, 275, "FUTURE"),
    (6117, "20:56", False, 292, "FUTURE"),
    (6118, "09:57", True, 309, "DONE_TODAY"),
    (6119, "22:58", False, 326, "FUTURE"),
    (6120, "11:59", False, 343, "FUTURE"),
    (6121, "00:00", True, 360, "DONE_TODAY"),
    (6122, "13:01", False, 377, "FUTURE"),
    (6123, "02:02", False, 394, "DUE"),
    (6124, "15:03", True, 411, "DONE_TODAY"),
    (6125, "04:04", False, 428, "DUE"),
    (6126, "17:05", False, 445, "FUTURE"),
    (6127, "06:06", True, 462, "DONE_TODAY"),
    (6128, "19:07", False, 479, "FUTURE"),
    (6129, "08:08", False, 496, "DUE"),
    (6130, "21:09", True, 513, "DONE_TODAY"),
    (6131, "10:10", False, 530, "FUTURE"),
    (6132, "23:11", False, 547, "FUTURE"),
    (6133, "12:12", True, 564, "DONE_TODAY"),
    (6134, "01:13", False, 581, "DUE"),
    (6135, "14:14", False, 598, "FUTURE"),
    (6136, "03:15", True, 615, "DONE_TODAY"),
    (6137, "16:16", False, 632, "FUTURE"),
    (6138, "05:17", False, 649, "DUE"),
    (6139, "18:18", True, 666, "DONE_TODAY"),
    (6140, "07:19", False, 683, "DUE"),
    (6141, "20:20", False, 700, "FUTURE"),
    (6142, "09:21", True, 717, "DONE_TODAY"),
    (6143, "22:22", False, 734, "FUTURE"),
    (6144, "11:23", False, 751, "DUE"),
    (6145, "00:24", True, 768, "DONE_TODAY"),
    (6146, "13:25", False, 785, "FUTURE"),
    (6147, "02:26", False, 802, "DUE"),
    (6148, "15:27", True, 819, "DONE_TODAY"),
    (6149, "04:28", False, 836, "DUE"),
    (6150, "17:29", False, 853, "FUTURE"),
    (6151, "06:30", True, 870, "DONE_TODAY"),
    (6152, "19:31", False, 887, "FUTURE"),
    (6153, "08:32", False, 904, "DUE"),
    (6154, "21:33", True, 921, "DONE_TODAY"),
    (6155, "10:34", False, 938, "DUE"),
    (6156, "23:35", False, 955, "FUTURE"),
    (6157, "12:36", True, 972, "DONE_TODAY"),
    (6158, "01:37", False, 989, "DUE"),
    (6159, "14:38", False, 1006, "DUE"),
    (6160, "03:39", True, 1023, "DONE_TODAY"),
    (6161, "16:40", False, 1040, "DUE"),
    (6162, "05:41", False, 1057, "DUE"),
    (6163, "18:42", True, 1074, "DONE_TODAY"),
    (6164, "07:43", False, 1091, "DUE"),
    (6165, "20:44", False, 1108, "FUTURE"),
    (6166, "09:45", True, 1125, "DONE_TODAY"),
    (6167, "22:46", False, 1142, "FUTURE"),
    (6168, "11:47", False, 1159, "DUE"),
    (6169, "00:48", True, 1176, "DONE_TODAY"),
    (6170, "13:49", False, 1193, "DUE"),
    (6171, "02:50", False, 1210, "DUE"),
    (6172, "15:51", True, 1227, "DONE_TODAY"),
    (6173, "04:52", False, 1244, "DUE"),
    (6174, "17:53", False, 1261, "DUE"),
    (6175, "06:54", True, 1278, "DONE_TODAY"),
    (6176, "19:55", False, 1295, "DUE"),
    (6177, "08:56", False, 1312, "DUE"),
    (6178, "21:57", True, 1329, "DONE_TODAY"),
    (6179, "10:58", False, 1346, "DUE"),
    (6180, "23:59", False, 1363, "FUTURE"),
    (6181, "12:00", True, 1380, "DONE_TODAY"),
    (6182, "01:01", False, 1397, "DUE"),
    (6183, "14:02", False, 1414, "DUE"),
    (6184, "03:03", True, 1431, "DONE_TODAY"),
    (6185, "16:04", False, 8, "FUTURE"),
    (6186, "05:05", False, 25, "FUTURE"),
    (6187, "18:06", True, 42, "DONE_TODAY"),
    (6188, "07:07", False, 59, "FUTURE"),
    (6189, "20:08", False, 76, "FUTURE"),
    (6190, "09:09", True, 93, "DONE_TODAY"),
    (6191, "22:10", False, 110, "FUTURE"),
    (6192, "11:11", False, 127, "FUTURE"),
    (6193, "00:12", True, 144, "DONE_TODAY"),
    (6194, "13:13", False, 161, "FUTURE"),
    (6195, "02:14", False, 178, "DUE"),
    (6196, "15:15", True, 195, "DONE_TODAY"),
    (6197, "04:16", False, 212, "FUTURE"),
    (6198, "17:17", False, 229, "FUTURE"),
    (6199, "06:18", True, 246, "DONE_TODAY"),
    (6200, "19:19", False, 263, "FUTURE"),
    (6201, "08:20", False, 280, "FUTURE"),
    (6202, "21:21", True, 297, "DONE_TODAY"),
    (6203, "10:22", False, 314, "FUTURE"),
    (6204, "23:23", False, 331, "FUTURE"),
    (6205, "12:24", True, 348, "DONE_TODAY"),
    (6206, "01:25", False, 365, "DUE"),
    (6207, "14:26", False, 382, "FUTURE"),
    (6208, "03:27", True, 399, "DONE_TODAY"),
    (6209, "16:28", False, 416, "FUTURE"),
    (6210, "05:29", False, 433, "DUE"),
    (6211, "18:30", True, 450, "DONE_TODAY"),
    (6212, "07:31", False, 467, "DUE"),
    (6213, "20:32", False, 484, "FUTURE"),
    (6214, "09:33", True, 501, "DONE_TODAY"),
    (6215, "22:34", False, 518, "FUTURE"),
    (6216, "11:35", False, 535, "FUTURE"),
    (6217, "00:36", True, 552, "DONE_TODAY"),
    (6218, "13:37", False, 569, "FUTURE"),
    (6219, "02:38", False, 586, "DUE"),
    (6220, "15:39", True, 603, "DONE_TODAY"),
    (6221, "04:40", False, 620, "DUE"),
    (6222, "17:41", False, 637, "FUTURE"),
    (6223, "06:42", True, 654, "DONE_TODAY"),
    (6224, "19:43", False, 671, "FUTURE"),
    (6225, "08:44", False, 688, "DUE"),
    (6226, "21:45", True, 705, "DONE_TODAY"),
    (6227, "10:46", False, 722, "DUE"),
    (6228, "23:47", False, 739, "FUTURE"),
    (6229, "12:48", True, 756, "DONE_TODAY"),
    (6230, "01:49", False, 773, "DUE"),
    (6231, "14:50", False, 790, "FUTURE"),
    (6232, "03:51", True, 807, "DONE_TODAY"),
    (6233, "16:52", False, 824, "FUTURE"),
    (6234, "05:53", False, 841, "DUE"),
    (6235, "18:54", True, 858, "DONE_TODAY"),
    (6236, "07:55", False, 875, "DUE"),
    (6237, "20:56", False, 892, "FUTURE"),
    (6238, "09:57", True, 909, "DONE_TODAY"),
    (6239, "22:58", False, 926, "FUTURE"),
    (6240, "11:59", False, 943, "DUE"),
    (6241, "00:00", True, 960, "DONE_TODAY"),
    (6242, "13:01", False, 977, "DUE"),
    (6243, "02:02", False, 994, "DUE"),
    (6244, "15:03", True, 1011, "DONE_TODAY"),
    (6245, "04:04", False, 1028, "DUE"),
    (6246, "17:05", False, 1045, "DUE"),
    (6247, "06:06", True, 1062, "DONE_TODAY"),
    (6248, "19:07", False, 1079, "FUTURE"),
    (6249, "08:08", False, 1096, "DUE"),
    (6250, "21:09", True, 1113, "DONE_TODAY"),
    (6251, "10:10", False, 1130, "DUE"),
    (6252, "23:11", False, 1147, "FUTURE"),
    (6253, "12:12", True, 1164, "DONE_TODAY"),
    (6254, "01:13", False, 1181, "DUE"),
    (6255, "14:14", False, 1198, "DUE"),
    (6256, "03:15", True, 1215, "DONE_TODAY"),
    (6257, "16:16", False, 1232, "DUE"),
    (6258, "05:17", False, 1249, "DUE"),
    (6259, "18:18", True, 1266, "DONE_TODAY"),
    (6260, "07:19", False, 1283, "DUE"),
    (6261, "20:20", False, 1300, "DUE"),
    (6262, "09:21", True, 1317, "DONE_TODAY"),
    (6263, "22:22", False, 1334, "FUTURE"),
    (6264, "11:23", False, 1351, "DUE"),
    (6265, "00:24", True, 1368, "DONE_TODAY"),
    (6266, "13:25", False, 1385, "DUE"),
    (6267, "02:26", False, 1402, "DUE"),
    (6268, "15:27", True, 1419, "DONE_TODAY"),
    (6269, "04:28", False, 1436, "DUE"),
    (6270, "17:29", False, 13, "FUTURE"),
    (6271, "06:30", True, 30, "DONE_TODAY"),
    (6272, "19:31", False, 47, "FUTURE"),
    (6273, "08:32", False, 64, "FUTURE"),
    (6274, "21:33", True, 81, "DONE_TODAY"),
    (6275, "10:34", False, 98, "FUTURE"),
    (6276, "23:35", False, 115, "FUTURE"),
    (6277, "12:36", True, 132, "DONE_TODAY"),
    (6278, "01:37", False, 149, "DUE"),
    (6279, "14:38", False, 166, "FUTURE"),
    (6280, "03:39", True, 183, "DONE_TODAY"),
    (6281, "16:40", False, 200, "FUTURE"),
    (6282, "05:41", False, 217, "FUTURE"),
    (6283, "18:42", True, 234, "DONE_TODAY"),
    (6284, "07:43", False, 251, "FUTURE"),
    (6285, "20:44", False, 268, "FUTURE"),
    (6286, "09:45", True, 285, "DONE_TODAY"),
    (6287, "22:46", False, 302, "FUTURE"),
    (6288, "11:47", False, 319, "FUTURE"),
    (6289, "00:48", True, 336, "DONE_TODAY"),
    (6290, "13:49", False, 353, "FUTURE"),
    (6291, "02:50", False, 370, "DUE"),
    (6292, "15:51", True, 387, "DONE_TODAY"),
    (6293, "04:52", False, 404, "DUE"),
    (6294, "17:53", False, 421, "FUTURE"),
    (6295, "06:54", True, 438, "DONE_TODAY"),
    (6296, "19:55", False, 455, "FUTURE"),
    (6297, "08:56", False, 472, "FUTURE"),
    (6298, "21:57", True, 489, "DONE_TODAY"),
    (6299, "10:58", False, 506, "FUTURE"),
    (6300, "23:59", False, 523, "FUTURE"),
    (6301, "12:00", True, 540, "DONE_TODAY"),
    (6302, "01:01", False, 557, "DUE"),
    (6303, "14:02", False, 574, "FUTURE"),
    (6304, "03:03", True, 591, "DONE_TODAY"),
    (6305, "16:04", False, 608, "FUTURE"),
    (6306, "05:05", False, 625, "DUE"),
    (6307, "18:06", True, 642, "DONE_TODAY"),
    (6308, "07:07", False, 659, "DUE"),
    (6309, "20:08", False, 676, "FUTURE"),
    (6310, "09:09", True, 693, "DONE_TODAY"),
    (6311, "22:10", False, 710, "FUTURE"),
    (6312, "11:11", False, 727, "DUE"),
    (6313, "00:12", True, 744, "DONE_TODAY"),
    (6314, "13:13", False, 761, "FUTURE"),
    (6315, "02:14", False, 778, "DUE"),
    (6316, "15:15", True, 795, "DONE_TODAY"),
    (6317, "04:16", False, 812, "DUE"),
    (6318, "17:17", False, 829, "FUTURE"),
    (6319, "06:18", True, 846, "DONE_TODAY"),
    (6320, "19:19", False, 863, "FUTURE"),
    (6321, "08:20", False, 880, "DUE"),
    (6322, "21:21", True, 897, "DONE_TODAY"),
    (6323, "10:22", False, 914, "DUE"),
    (6324, "23:23", False, 931, "FUTURE"),
    (6325, "12:24", True, 948, "DONE_TODAY"),
    (6326, "01:25", False, 965, "DUE"),
    (6327, "14:26", False, 982, "DUE"),
    (6328, "03:27", True, 999, "DONE_TODAY"),
    (6329, "16:28", False, 1016, "DUE"),
    (6330, "05:29", False, 1033, "DUE"),
    (6331, "18:30", True, 1050, "DONE_TODAY"),
    (6332, "07:31", False, 1067, "DUE"),
    (6333, "20:32", False, 1084, "FUTURE"),
    (6334, "09:33", True, 1101, "DONE_TODAY"),
    (6335, "22:34", False, 1118, "FUTURE"),
    (6336, "11:35", False, 1135, "DUE"),
    (6337, "00:36", True, 1152, "DONE_TODAY"),
    (6338, "13:37", False, 1169, "DUE"),
    (6339, "02:38", False, 1186, "DUE"),
    (6340, "15:39", True, 1203, "DONE_TODAY"),
    (6341, "04:40", False, 1220, "DUE"),
    (6342, "17:41", False, 1237, "DUE"),
    (6343, "06:42", True, 1254, "DONE_TODAY"),
    (6344, "19:43", False, 1271, "DUE"),
    (6345, "08:44", False, 1288, "DUE"),
    (6346, "21:45", True, 1305, "DONE_TODAY"),
    (6347, "10:46", False, 1322, "DUE"),
    (6348, "23:47", False, 1339, "FUTURE"),
    (6349, "12:48", True, 1356, "DONE_TODAY"),
    (6350, "01:49", False, 1373, "DUE"),
    (6351, "14:50", False, 1390, "DUE"),
    (6352, "03:51", True, 1407, "DONE_TODAY"),
    (6353, "16:52", False, 1424, "DUE"),
    (6354, "05:53", False, 1, "FUTURE"),
    (6355, "18:54", True, 18, "DONE_TODAY"),
    (6356, "07:55", False, 35, "FUTURE"),
    (6357, "20:56", False, 52, "FUTURE"),
    (6358, "09:57", True, 69, "DONE_TODAY"),
    (6359, "22:58", False, 86, "FUTURE"),
    (6360, "11:59", False, 103, "FUTURE"),
    (6361, "00:00", True, 120, "DONE_TODAY"),
    (6362, "13:01", False, 137, "FUTURE"),
    (6363, "02:02", False, 154, "DUE"),
    (6364, "15:03", True, 171, "DONE_TODAY"),
    (6365, "04:04", False, 188, "FUTURE"),
    (6366, "17:05", False, 205, "FUTURE"),
    (6367, "06:06", True, 222, "DONE_TODAY"),
    (6368, "19:07", False, 239, "FUTURE"),
]
