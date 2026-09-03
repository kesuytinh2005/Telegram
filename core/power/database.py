# ============================================================
# core/power/database.py
# DATABASE THEO DÕI LỊCH CÚP ĐIỆN
# ============================================================

import os
import sqlite3
import uuid
from datetime import datetime


BASE_DIR = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.abspath(__file__)
        )
    )
)

DATA_DIR = os.path.join(
    BASE_DIR,
    "data"
)

DB_FILE = os.path.join(
    DATA_DIR,
    "power.db"
)


# ============================================================
# CONNECTION
# ============================================================

def get_connection():

    os.makedirs(
        DATA_DIR,
        exist_ok=True
    )

    conn = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


# ============================================================
# INIT
# ============================================================

def init_database():

    conn = get_connection()

    try:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (

                id TEXT PRIMARY KEY,

                user_id INTEGER NOT NULL,

                type TEXT NOT NULL,

                value TEXT NOT NULL,

                area_code TEXT,

                area_name TEXT,

                check_time TEXT NOT NULL DEFAULT '07:00',

                enabled INTEGER NOT NULL DEFAULT 1,

                last_check TEXT,

                last_check_date TEXT,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL,

                UNIQUE(
                    user_id,
                    type,
                    value
                )
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                subscription_id TEXT NOT NULL,

                notification_key TEXT NOT NULL,

                sent_at TEXT NOT NULL,

                UNIQUE(
                    subscription_id,
                    notification_key
                )
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS power_checks (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                subscription_id TEXT NOT NULL,

                check_date TEXT NOT NULL,

                check_time TEXT NOT NULL,

                status TEXT,

                schedule_count INTEGER DEFAULT 0,

                checked_at TEXT NOT NULL,

                UNIQUE(
                    subscription_id,
                    check_date
                )
            )
        """)

        conn.commit()

    finally:

        conn.close()


# ============================================================
# HELPERS
# ============================================================

def _row_to_dict(row):

    if row is None:
        return None

    return dict(row)


def _rows_to_dict(rows):

    return [
        dict(row)
        for row in rows
    ]


def _now():

    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ============================================================
# ADD
# ============================================================

def add_subscription(
    user_id,
    sub_type,
    value,
    area_code=None,
    area_name=None,
    check_time="07:00"
):

    init_database()

    sub_type = str(
        sub_type
    ).strip().lower()

    value = str(
        value
    ).strip().upper()

    check_time = str(
        check_time
    ).strip()

    now = _now()

    conn = get_connection()

    try:

        existing = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE user_id = ?
              AND type = ?
              AND value = ?
            """,
            (
                user_id,
                sub_type,
                value
            )
        ).fetchone()

        if existing:

            conn.execute(
                """
                UPDATE subscriptions

                SET area_code = ?,
                    area_name = ?,
                    check_time = ?,
                    enabled = 1,
                    updated_at = ?

                WHERE id = ?
                """,
                (
                    area_code,
                    area_name,
                    check_time,
                    now,
                    existing["id"]
                )
            )

            conn.commit()

            row = conn.execute(
                """
                SELECT *
                FROM subscriptions
                WHERE id = ?
                """,
                (
                    existing["id"],
                )
            ).fetchone()

            return _row_to_dict(row)

        sub_id = uuid.uuid4().hex

        conn.execute(
            """
            INSERT INTO subscriptions (
                id,
                user_id,
                type,
                value,
                area_code,
                area_name,
                check_time,
                enabled,
                created_at,
                updated_at
            )

            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                sub_id,
                user_id,
                sub_type,
                value,
                area_code,
                area_name,
                check_time,
                now,
                now
            )
        )

        conn.commit()

        row = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE id = ?
            """,
            (
                sub_id,
            )
        ).fetchone()

        return _row_to_dict(row)

    finally:

        conn.close()


# ============================================================
# UPDATE
# ============================================================

def update_subscription(
    subscription_id,
    **fields
):

    if not fields:
        return False

    allowed = {
        "area_code",
        "area_name",
        "check_time",
        "enabled"
    }

    updates = []
    values = []

    for key, value in fields.items():

        if key not in allowed:
            continue

        updates.append(
            f"{key} = ?"
        )

        if key == "enabled":
            value = 1 if value else 0

        values.append(
            value
        )

    if not updates:
        return False

    updates.append(
        "updated_at = ?"
    )

    values.append(
        _now()
    )

    values.append(
        subscription_id
    )

    conn = get_connection()

    try:

        cursor = conn.execute(
            f"""
            UPDATE subscriptions

            SET {", ".join(updates)}

            WHERE id = ?
            """,
            values
        )

        conn.commit()

        return cursor.rowcount > 0

    finally:

        conn.close()


# ============================================================
# ENABLE / DISABLE
# ============================================================

def set_subscription_enabled(
    subscription_id,
    enabled=True
):

    return update_subscription(
        subscription_id,
        enabled=enabled
    )


# ============================================================
# REMOVE
# ============================================================

def remove_subscription(
    subscription_id
):

    conn = get_connection()

    try:

        conn.execute(
            """
            DELETE FROM notifications
            WHERE subscription_id = ?
            """,
            (
                subscription_id,
            )
        )

        conn.execute(
            """
            DELETE FROM power_checks
            WHERE subscription_id = ?
            """,
            (
                subscription_id,
            )
        )

        cursor = conn.execute(
            """
            DELETE FROM subscriptions
            WHERE id = ?
            """,
            (
                subscription_id,
            )
        )

        conn.commit()

        return cursor.rowcount > 0

    finally:

        conn.close()


# ============================================================
# USER SUBSCRIPTIONS
# ============================================================

def get_user_subscriptions(
    user_id
):

    init_database()

    conn = get_connection()

    try:

        rows = conn.execute(
            """
            SELECT *
            FROM subscriptions

            WHERE user_id = ?

            ORDER BY
                created_at ASC
            """,
            (
                user_id,
            )
        ).fetchall()

        return _rows_to_dict(
            rows
        )

    finally:

        conn.close()


# ============================================================
# ENABLED SUBSCRIPTIONS
# ============================================================

def get_all_enabled_subscriptions():

    init_database()

    conn = get_connection()

    try:

        rows = conn.execute(
            """
            SELECT *
            FROM subscriptions

            WHERE enabled = 1

            ORDER BY id
            """
        ).fetchall()

        return _rows_to_dict(
            rows
        )

    finally:

        conn.close()


# Alias để tương thích code cũ
def get_enabled_subscriptions():

    return get_all_enabled_subscriptions()


# ============================================================
# LAST CHECK
# ============================================================

def update_last_check(
    subscription_id,
    check_date=None,
    check_time=None
):

    now = _now()

    conn = get_connection()

    try:

        conn.execute(
            """
            UPDATE subscriptions

            SET last_check = ?,
                last_check_date = ?,
                updated_at = ?

            WHERE id = ?
            """,
            (
                check_time or now,
                check_date,
                now,
                subscription_id
            )
        )

        conn.commit()

    finally:

        conn.close()


def get_last_check(
    subscription_id
):

    conn = get_connection()

    try:

        row = conn.execute(
            """
            SELECT
                last_check,
                last_check_date
            FROM subscriptions
            WHERE id = ?
            """,
            (
                subscription_id,
            )
        ).fetchone()

        if not row:
            return None

        return {
            "last_check": row["last_check"],
            "last_check_date": row["last_check_date"]
        }

    finally:

        conn.close()


# ============================================================
# UPDATE CHECK
# ============================================================

def update_subscription_check(
    subscription_id,
    check_date=None,
    check_time=None
):

    update_last_check(
        subscription_id,
        check_date=check_date,
        check_time=check_time
    )


# ============================================================
# NOTIFICATION
# ============================================================

def notification_exists(
    subscription_id,
    notification_key
):

    conn = get_connection()

    try:

        row = conn.execute(
            """
            SELECT 1
            FROM notifications

            WHERE subscription_id = ?
              AND notification_key = ?

            LIMIT 1
            """,
            (
                subscription_id,
                notification_key
            )
        ).fetchone()

        return row is not None

    finally:

        conn.close()


def mark_notification_sent(
    subscription_id,
    notification_key
):

    conn = get_connection()

    try:

        conn.execute(
            """
            INSERT OR IGNORE INTO notifications (
                subscription_id,
                notification_key,
                sent_at
            )

            VALUES (?, ?, ?)
            """,
            (
                subscription_id,
                notification_key,
                _now()
            )
        )

        conn.commit()

    finally:

        conn.close()


# ============================================================
# POWER CHECK
# ============================================================

def save_power_check(
    subscription_id,
    check_date,
    check_time,
    status="ok",
    schedule_count=0
):

    conn = get_connection()

    try:

        conn.execute(
            """
            INSERT OR REPLACE INTO power_checks (
                subscription_id,
                check_date,
                check_time,
                status,
                schedule_count,
                checked_at
            )

            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                subscription_id,
                check_date,
                check_time,
                status,
                schedule_count,
                _now()
            )
        )

        conn.commit()

    finally:

        conn.close()


# ============================================================
# GET CHECK
# ============================================================

def get_power_check(
    subscription_id,
    check_date
):

    conn = get_connection()

    try:

        row = conn.execute(
            """
            SELECT *
            FROM power_checks

            WHERE subscription_id = ?
              AND check_date = ?
            """,
            (
                subscription_id,
                check_date
            )
        ).fetchone()

        return _row_to_dict(
            row
        )

    finally:

        conn.close()