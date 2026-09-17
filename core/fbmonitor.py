from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import aiohttp

try:
    from config import DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.getcwd(), "data")

LOGGER = logging.getLogger("core.fbmonitor")

DB_FILE = os.path.join(DATA_DIR, "facebook_monitor.db")
FACEBOOK_PICTURE_URL = "https://graph.facebook.com/{uid}/picture?redirect=false"

# Tuned for a Telegram bot: short timeouts, connection reuse, bounded concurrency,
# and retries only where retrying is useful. The monitor never blocks Telethon's loop.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=9, connect=3, sock_connect=3, sock_read=6)
MAX_CONCURRENCY = 16
CONNECTOR_LIMIT = 32
CACHE_TTL = 8
SCHEDULER_TICK = 2
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 7 * 24 * 3600
MAX_RETRIES = 2

STATUS_LIVE = "live"
STATUS_DIE = "die"
STATUS_UNKNOWN = "unknown"

UID_RE = re.compile(r"^\d{5,30}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class CheckResult:
    uid: str
    status: str
    reason: str
    checked_at: str
    http_status: int | None = None
    raw: dict[str, Any] | None = None
    latency_ms: int = 0
    attempts: int = 1


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json_load(value: Any, default: Any):
    try:
        parsed = json.loads(value)
        return parsed
    except Exception:
        return default


class FacebookMonitorDB:
    """Small WAL SQLite store. All operations are short and transaction scoped."""

    def __init__(self, path: str = DB_FILE):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.init()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self):
        conn = self.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS fb_watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    uid TEXT NOT NULL,
                    label TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    interval_seconds INTEGER NOT NULL DEFAULT 300,
                    daily_times TEXT NOT NULL DEFAULT '[]',
                    weekdays TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
                    notify_transition INTEGER NOT NULL DEFAULT 1,
                    notify_daily INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT,
                    last_reason TEXT,
                    last_checked_at TEXT,
                    last_latency_ms INTEGER NOT NULL DEFAULT 0,
                    last_http_status INTEGER,
                    last_daily_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, uid)
                );
                CREATE INDEX IF NOT EXISTS idx_fb_watches_enabled ON fb_watches(enabled);
                CREATE INDEX IF NOT EXISTS idx_fb_watches_user ON fb_watches(user_id);
                CREATE TABLE IF NOT EXISTS fb_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_id INTEGER NOT NULL,
                    old_status TEXT,
                    new_status TEXT NOT NULL,
                    reason TEXT,
                    checked_at TEXT NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(watch_id) REFERENCES fb_watches(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_fb_events_watch ON fb_events(watch_id, id DESC);
                """
            )
            # Safe migrations for installations made by older module versions.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(fb_watches)").fetchall()}
            if "last_latency_ms" not in cols:
                conn.execute("ALTER TABLE fb_watches ADD COLUMN last_latency_ms INTEGER NOT NULL DEFAULT 0")
            if "last_http_status" not in cols:
                conn.execute("ALTER TABLE fb_watches ADD COLUMN last_http_status INTEGER")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    def add(self, user_id: int, uid: str, label: str | None = None):
        uid = str(uid).strip()
        now = _now()
        conn = self.connect()
        try:
            conn.execute(
                """
                INSERT INTO fb_watches(user_id, uid, label, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, uid) DO UPDATE SET
                    label=COALESCE(excluded.label, fb_watches.label),
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (int(user_id), uid, label, now, now),
            )
            conn.commit()
            return self._row(conn.execute("SELECT * FROM fb_watches WHERE user_id=? AND uid=?", (int(user_id), uid)).fetchone())
        finally:
            conn.close()

    def remove(self, user_id: int, watch_id: int) -> bool:
        conn = self.connect()
        try:
            cur = conn.execute("DELETE FROM fb_watches WHERE id=? AND user_id=?", (int(watch_id), int(user_id)))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def list_user(self, user_id: int, limit: int = 500):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM fb_watches WHERE user_id=? ORDER BY id DESC LIMIT ?", (int(user_id), int(limit))).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    def get(self, user_id: int, watch_id: int):
        conn = self.connect()
        try:
            return self._row(conn.execute("SELECT * FROM fb_watches WHERE id=? AND user_id=?", (int(watch_id), int(user_id))).fetchone())
        finally:
            conn.close()

    def all_enabled(self):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM fb_watches WHERE enabled=1 ORDER BY id").fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    def stats(self, user_id: int):
        rows = self.list_user(user_id)
        live = sum(r.get("last_status") == STATUS_LIVE for r in rows)
        die = sum(r.get("last_status") == STATUS_DIE for r in rows)
        unknown = sum(r.get("last_status") not in (STATUS_LIVE, STATUS_DIE) for r in rows)
        enabled = sum(bool(r.get("enabled")) for r in rows)
        return {"total": len(rows), "live": live, "die": die, "unknown": unknown, "enabled": enabled}

    def update(self, user_id: int, watch_id: int, **fields):
        allowed = {
            "label", "enabled", "interval_seconds", "daily_times", "weekdays",
            "notify_transition", "notify_daily", "last_status", "last_reason",
            "last_checked_at", "last_latency_ms", "last_http_status", "last_daily_key",
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return False
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [int(watch_id), int(user_id)]
        conn = self.connect()
        try:
            cur = conn.execute(f"UPDATE fb_watches SET {assignments} WHERE id=? AND user_id=?", values)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def record_event(self, watch_id: int, old_status: str | None, new_status: str, reason: str, checked_at: str, notified: bool = False):
        conn = self.connect()
        try:
            conn.execute("INSERT INTO fb_events(watch_id, old_status, new_status, reason, checked_at, notified) VALUES (?, ?, ?, ?, ?, ?)", (watch_id, old_status, new_status, reason, checked_at, int(notified)))
            conn.commit()
        finally:
            conn.close()

    def recent_events(self, user_id: int, watch_id: int, limit: int = 10):
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT e.* FROM fb_events e JOIN fb_watches w ON w.id=e.watch_id
                   WHERE w.id=? AND w.user_id=? ORDER BY e.id DESC LIMIT ?""",
                (int(watch_id), int(user_id), int(limit)),
            ).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()


class FacebookChecker:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._session_lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, CheckResult]] = {}
        self._cache_lock = asyncio.Lock()

    async def start(self):
        async with self._session_lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(
                    limit=CONNECTOR_LIMIT,
                    limit_per_host=CONNECTOR_LIMIT,
                    ttl_dns_cache=300,
                    enable_cleanup_closed=True,
                )
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=REQUEST_TIMEOUT,
                    headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 TelegramBot-FBMonitor/2.0"},
                    raise_for_status=False,
                )

    async def close(self):
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
        async with self._cache_lock:
            self._cache.clear()

    @staticmethod
    def classify(uid: str, payload: Any) -> tuple[str, str]:
        """Classify only from response structure. `url` is intentionally ignored."""
        if not isinstance(payload, dict):
            return STATUS_UNKNOWN, "API không trả JSON object"
        data = payload.get("data")
        if not isinstance(data, dict):
            return STATUS_UNKNOWN, "Thiếu trường data"
        height = data.get("height")
        width = data.get("width")
        valid_h = isinstance(height, (int, float)) and not isinstance(height, bool) and height > 0
        valid_w = isinstance(width, (int, float)) and not isinstance(width, bool) and width > 0
        if valid_h and valid_w:
            return STATUS_LIVE, f"image dimensions {int(width)}x{int(height)}"
        # Per the user's API contract: a data object without valid dimensions is DIE.
        return STATUS_DIE, "API không có image dimensions hợp lệ"

    async def _get_cached(self, uid: str) -> CheckResult | None:
        async with self._cache_lock:
            item = self._cache.get(uid)
            if item and time.monotonic() - item[0] <= CACHE_TTL:
                return item[1]
            if item:
                self._cache.pop(uid, None)
        return None

    async def _put_cache(self, uid: str, result: CheckResult):
        async with self._cache_lock:
            self._cache[uid] = (time.monotonic(), result)
            # Cheap bounded cleanup.
            if len(self._cache) > 5000:
                cutoff = time.monotonic() - CACHE_TTL
                self._cache = {k: v for k, v in self._cache.items() if v[0] >= cutoff}

    async def check(self, uid: str, force: bool = False) -> CheckResult:
        uid = str(uid).strip()
        if not UID_RE.fullmatch(uid):
            return CheckResult(uid, STATUS_UNKNOWN, "UID phải gồm 5–30 chữ số", _now())

        if not force:
            cached = await self._get_cached(uid)
            if cached:
                return cached

        await self.start()
        assert self._session is not None
        url = FACEBOOK_PICTURE_URL.format(uid=uid)
        started = time.monotonic()
        last_reason = "Unknown error"
        attempts = 0

        async with self._sem:
            for attempt in range(1, MAX_RETRIES + 2):
                attempts = attempt
                try:
                    async with self._session.get(url, allow_redirects=False) as resp:
                        http_status = resp.status
                        text = await resp.text(errors="replace")
                        try:
                            payload = json.loads(text)
                        except json.JSONDecodeError:
                            payload = None

                        # Retry transient server/rate-limit responses only.
                        if http_status == 429 or 500 <= http_status <= 599:
                            retry_after = 0.0
                            if http_status == 429:
                                try:
                                    retry_after = min(float(resp.headers.get("Retry-After", "0")), 3.0)
                                except ValueError:
                                    retry_after = 0.0
                            if attempt <= MAX_RETRIES:
                                await asyncio.sleep(max(retry_after, 0.15 * (2 ** (attempt - 1)) + random.random() * 0.15))
                                continue
                            result = CheckResult(uid, STATUS_UNKNOWN, f"Facebook API HTTP {http_status}", _now(), http_status, payload if isinstance(payload, dict) else {}, int((time.monotonic() - started) * 1000), attempts)
                        elif not isinstance(payload, dict):
                            result = CheckResult(uid, STATUS_UNKNOWN, f"HTTP {http_status}: response không phải JSON", _now(), http_status, {}, int((time.monotonic() - started) * 1000), attempts)
                        else:
                            status, reason = self.classify(uid, payload)
                            result = CheckResult(uid, status, reason, _now(), http_status, payload, int((time.monotonic() - started) * 1000), attempts)
                        await self._put_cache(uid, result)
                        return result
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    last_reason = type(exc).__name__
                    if attempt <= MAX_RETRIES:
                        await asyncio.sleep(0.15 * (2 ** (attempt - 1)) + random.random() * 0.15)
                        continue
                    result = CheckResult(uid, STATUS_UNKNOWN, f"Lỗi mạng: {last_reason}", _now(), None, {}, int((time.monotonic() - started) * 1000), attempts)
                    await self._put_cache(uid, result)
                    return result
                except Exception as exc:
                    LOGGER.exception("FB check failed for %s", uid)
                    result = CheckResult(uid, STATUS_UNKNOWN, f"Lỗi request: {type(exc).__name__}", _now(), None, {}, int((time.monotonic() - started) * 1000), attempts)
                    await self._put_cache(uid, result)
                    return result

        result = CheckResult(uid, STATUS_UNKNOWN, last_reason, _now(), None, {}, int((time.monotonic() - started) * 1000), attempts)
        await self._put_cache(uid, result)
        return result


class FacebookMonitorService:
    """Persistent background monitor. `/stop` from another bot subsystem cannot cancel this task."""

    def __init__(self, bot):
        self.bot = bot
        self.db = FacebookMonitorDB()
        self.checker = FacebookChecker()
        self._task: asyncio.Task | None = None
        self._started = False
        self._wake = asyncio.Event()
        self._closing = False
        self._running_ids: set[int] = set()
        self._running_lock = asyncio.Lock()
        self._jobs: set[asyncio.Task] = set()

    async def start(self):
        if self._started and self._task and not self._task.done():
            return
        self._closing = False
        self._started = True
        await self.checker.start()
        self._task = asyncio.create_task(self._run(), name="fbmonitor-supervisor")
        LOGGER.info("Facebook UID monitor started")

    async def close(self):
        self._closing = True
        self._wake.set()
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for job in list(self._jobs):
            job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)
        self._jobs.clear()
        self._running_ids.clear()
        await self.checker.close()
        self._started = False

    def wake(self):
        self._wake.set()

    @staticmethod
    def _due(row, now: datetime):
        last = row.get("last_checked_at")
        interval = max(MIN_INTERVAL_SECONDS, min(int(row.get("interval_seconds") or 300), MAX_INTERVAL_SECONDS))
        interval_due = True
        if last:
            try:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                interval_due = (now - last_dt).total_seconds() >= interval
            except ValueError:
                interval_due = True

        weekdays = _json_load(row.get("weekdays"), list(range(7)))
        try:
            weekday_ok = now.weekday() in {int(x) for x in weekdays}
        except Exception:
            weekday_ok = True

        daily_times = _json_load(row.get("daily_times"), [])
        current_hm = now.strftime("%H:%M")
        daily_key = now.strftime("%Y-%m-%d %H:%M")
        daily_due = weekday_ok and current_hm in {str(x)[:5] for x in daily_times} and row.get("last_daily_key") != daily_key
        return interval_due and weekday_ok, daily_due, daily_key

    async def _claim(self, watch_id: int) -> bool:
        async with self._running_lock:
            if watch_id in self._running_ids:
                return False
            self._running_ids.add(watch_id)
            return True

    async def _release(self, watch_id: int):
        async with self._running_lock:
            self._running_ids.discard(watch_id)

    async def _run(self):
        while not self._closing:
            try:
                now = datetime.now()
                rows = self.db.all_enabled()
                for row in rows:
                    interval_due, daily_due, daily_key = self._due(row, now)
                    if not (interval_due or daily_due):
                        continue
                    watch_id = int(row["id"])
                    if not await self._claim(watch_id):
                        continue
                    task = asyncio.create_task(self._process(row, daily_due, daily_key), name=f"fbmonitor-check-{watch_id}")
                    self._jobs.add(task)
                    task.add_done_callback(self._jobs.discard)

                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=SCHEDULER_TICK)
                    self._wake.clear()
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Facebook monitor supervisor error")
                await asyncio.sleep(1)

    async def _process(self, row, daily_due: bool, daily_key: str):
        watch_id = int(row["id"])
        try:
            result = await self.checker.check(str(row["uid"]))
            old = row.get("last_status")
            updates = dict(
                last_status=result.status,
                last_reason=result.reason,
                last_checked_at=result.checked_at,
                last_latency_ms=result.latency_ms,
                last_http_status=result.http_status,
            )
            if daily_due:
                updates["last_daily_key"] = daily_key
            self.db.update(int(row["user_id"]), watch_id, **updates)

            if old in (STATUS_LIVE, STATUS_DIE) and result.status in (STATUS_LIVE, STATUS_DIE) and old != result.status and int(row.get("notify_transition", 1)):
                self.db.record_event(watch_id, old, result.status, result.reason, result.checked_at)
                await self._notify_transition(row, old, result)

            if daily_due and int(row.get("notify_daily", 0)):
                await self._notify_daily(row, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("FB monitor job failed for watch %s", watch_id)
        finally:
            await self._release(watch_id)

    async def _send(self, user_id: int, text: str):
        try:
            await self.bot.send_message(int(user_id), text, parse_mode="html", link_preview=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Unable to notify Telegram user %s", user_id)

    async def _notify_transition(self, row, old: str, result: CheckResult):
        label = "LIVE" if result.status == STATUS_LIVE else "DIE"
        old_label = "LIVE" if old == STATUS_LIVE else "DIE"
        emoji = "🟢" if result.status == STATUS_LIVE else "🔴"
        name = html.escape(str(row.get("label") or "Facebook UID"))
        text = (
            "╭━━━ <b>🚨 TRẠNG THÁI THAY ĐỔI</b> ━━━╮\n"
            f"🆔 <code>{html.escape(str(row['uid']))}</code>\n"
            f"🏷 <b>{name}</b>\n\n"
            f"📌 Trước: <b>{old_label}</b>\n"
            f"{emoji} Hiện tại: <b>{label}</b>\n"
            f"⚡ Độ trễ API: <b>{result.latency_ms}ms</b>\n"
            f"🔎 {html.escape(result.reason)}\n"
            f"🕒 {result.checked_at}\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
        await self._send(int(row["user_id"]), text)

    async def _notify_daily(self, row, result: CheckResult):
        status = {STATUS_LIVE: "🟢 LIVE", STATUS_DIE: "🔴 DIE", STATUS_UNKNOWN: "🟡 UNKNOWN"}[result.status]
        text = (
            "╭━━━ <b>📅 BÁO CÁO ĐỊNH KỲ</b> ━━━╮\n"
            f"🆔 <code>{html.escape(str(row['uid']))}</code>\n"
            f"📊 Trạng thái: <b>{status}</b>\n"
            f"⚡ API: <b>{result.latency_ms}ms</b>\n"
            f"🔎 {html.escape(result.reason)}\n"
            f"🕒 {result.checked_at}\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
        await self._send(int(row["user_id"]), text)

    async def check_now(self, user_id: int, watch_id: int) -> CheckResult | None:
        row = self.db.get(user_id, watch_id)
        if not row:
            return None
        result = await self.checker.check(str(row["uid"]), force=True)
        old = row.get("last_status")
        self.db.update(user_id, watch_id, last_status=result.status, last_reason=result.reason, last_checked_at=result.checked_at, last_latency_ms=result.latency_ms, last_http_status=result.http_status)
        if old in (STATUS_LIVE, STATUS_DIE) and result.status in (STATUS_LIVE, STATUS_DIE) and old != result.status:
            self.db.record_event(watch_id, old, result.status, result.reason, result.checked_at, False)
        return result


_SERVICES: dict[int, FacebookMonitorService] = {}


def get_service(bot) -> FacebookMonitorService:
    key = id(bot)
    service = _SERVICES.get(key)
    if service is None:
        service = FacebookMonitorService(bot)
        _SERVICES[key] = service
    return service
