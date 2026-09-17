# ============================================================
# core/power/monitor.py
# POWER OUTAGE AUTO MONITOR
# ============================================================

import asyncio
from datetime import datetime

from core.power.api import (
    check_subscription,
)

from core.power.database import (
    get_all_enabled_subscriptions,
    update_subscription_check,
    get_last_check,
    notification_exists,
    mark_notification_sent,
    save_power_check,
)


# ============================================================
# CONFIG
# ============================================================

CHECK_INTERVAL = 30

DEFAULT_CHECK_TIME = "07:00"


# ============================================================
# TIME
# ============================================================

def now():

    return datetime.now()


def today_string():

    return now().strftime(
        "%Y-%m-%d"
    )


def current_time_string():

    return now().strftime(
        "%H:%M"
    )


def should_check(
    subscription
):

    check_time = subscription.get(
        "check_time",
        DEFAULT_CHECK_TIME
    )

    check_time = str(
        check_time
    )[:5]

    current = current_time_string()

    today = today_string()

    last = get_last_check(
        subscription["id"]
    )

    if not last:
        return True

    last_date = last.get(
        "last_check_date"
    )

    # --------------------------------------------------------
    # Chưa check hôm nay
    # --------------------------------------------------------

    if last_date != today:
        return current >= check_time

    return False


# ============================================================
# NOTIFICATION KEY
# ============================================================

def make_notification_key(
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

    start_date = schedule.get(
        "start_date",
        ""
    )

    start_time = schedule.get(
        "start_time",
        ""
    )

    end_date = schedule.get(
        "end_date",
        ""
    )

    end_time = schedule.get(
        "end_time",
        ""
    )

    return "|".join([
        str(code),
        str(where),
        str(start_date),
        str(start_time),
        str(end_date),
        str(end_time),
    ])


# ============================================================
# FORMAT
# ============================================================

def format_schedule(
    schedule
):

    code = schedule.get(
        "code"
    )

    where = schedule.get(
        "where"
    )

    start_time = schedule.get(
        "start_time"
    )

    start_date = schedule.get(
        "start_date"
    )

    end_time = schedule.get(
        "end_time"
    )

    end_date = schedule.get(
        "end_date"
    )

    cause = schedule.get(
        "cause"
    )

    text = ""

    text += "⚡ <b>LỊCH CÚP ĐIỆN MỚI</b>\n\n"

    if where:
        text += (
            f"📍 <b>Khu vực:</b> "
            f"{where}\n"
        )

    if start_time and start_date:

        text += (
            f"🕐 <b>Từ:</b> "
            f"{start_time} {start_date}\n"
        )

    if end_time and end_date:

        text += (
            f"🕐 <b>Đến:</b> "
            f"{end_time} {end_date}\n"
        )

    if cause:

        text += (
            f"📝 <b>Lý do:</b> "
            f"{cause}\n"
        )

    if code:

        text += (
            f"🔖 <b>Mã lịch:</b> "
            f"<code>{code}</code>\n"
        )

    return text


# ============================================================
# CHECK ONE
# ============================================================

async def check_one(
    bot,
    subscription
):

    sub_id = subscription["id"]

    user_id = subscription["user_id"]

    check_date = today_string()

    check_time = current_time_string()

    try:

        result = await check_subscription(
            subscription
        )

        schedules = result.get(
            "schedules",
            []
        )

        save_power_check(
            subscription_id=sub_id,
            check_date=check_date,
            check_time=check_time,
            status="ok",
            schedule_count=len(
                schedules
            )
        )

        # ----------------------------------------------------
        # Gửi lịch mới
        # ----------------------------------------------------

        for schedule in schedules:

            key = make_notification_key(
                schedule
            )

            if not key:
                continue

            if notification_exists(
                sub_id,
                key
            ):
                continue

            message = format_schedule(
                schedule
            )

            try:

                await bot.send_message(
                    user_id,
                    message,
                    parse_mode="html"
                )

                mark_notification_sent(
                    sub_id,
                    key
                )

            except Exception as send_error:

                print(
                    f"[POWER SEND ERROR] "
                    f"#{sub_id}: "
                    f"{send_error}"
                )

        update_subscription_check(
            sub_id,
            check_date=check_date,
            check_time=check_time
        )

        print(
            f"[POWER] Checked "
            f"{subscription.get('type')} "
            f"{subscription.get('value')} "
            f"-> {len(schedules)} schedule(s)"
        )

        return True

    except Exception as e:

        print(
            f"[POWER CHECK ERROR] "
            f"#{sub_id}: {e}"
        )

        save_power_check(
            subscription_id=sub_id,
            check_date=check_date,
            check_time=check_time,
            status="error",
            schedule_count=0
        )

        return False


# ============================================================
# CHECK ALL
# ============================================================

async def check_all(
    bot
):

    subscriptions = (
        get_all_enabled_subscriptions()
    )

    print(
        f"[POWER] Active subscriptions: "
        f"{len(subscriptions)}"
    )

    for index, subscription in enumerate(
        subscriptions,
        start=1
    ):

        try:

            if not should_check(
                subscription
            ):
                continue

            print(
                f"[POWER] Checking "
                f"subscription #{index} "
                f"({subscription.get('type')}: "
                f"{subscription.get('value')})"
            )

            await check_one(
                bot,
                subscription
            )

        except Exception as e:

            print(
                f"[POWER CHECK ERROR] "
                f"#{index}: {e}"
            )


# ============================================================
# MONITOR LOOP
# ============================================================

async def power_monitor_loop(
    bot
):

    print(
        "[POWER] Monitor loop started"
    )

    while True:

        try:

            await check_all(
                bot
            )

        except asyncio.CancelledError:

            print(
                "[POWER] Monitor stopped"
            )

            raise

        except Exception as e:

            print(
                f"[POWER MONITOR ERROR] {e}"
            )

        await asyncio.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# START
# ============================================================

def start_power_monitor(
    bot
):

    return asyncio.create_task(
        power_monitor_loop(
            bot
        )
    )