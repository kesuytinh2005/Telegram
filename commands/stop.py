# ============================================================
# commands/stop.py
# ============================================================
#
# USER KILL SWITCH
#
# IMPORTANT:
# - Chi dung task/session cua user hien tai.
# - Khong quet asyncio.all_tasks().
# - Khong xoa global state.
# - Khong unregister handler/module.
# - Khong cleanup registry cua module cupdien.
#
# getuidfb co session registry rieng (_active_sessions).
# File nay chi goi "stop hook" neu module getuidfb cung cap.
# No khong xoa registry cua getuidfb truc tiep.
# ============================================================

import asyncio
import importlib
import inspect
import time

from telethon import events

from core.task_manager import stop_user_tasks


COMMAND_INFO = {
    "command": "stop",
    "category": "SYSTEM",
    "title": "Dung tien trinh",
    "description": (
        "Dung cac tien trinh cua user hien tai va hien thi "
        "trang thai thuc te."
    ),
    "usage": "/stop",
    "examples": ["/stop"],
    "details": [
        "Tu dong dung task duoc task_manager quan ly.",
        "Gui lenh dung cho getuidfb neu dang co vong lap.",
        "Chi xu ly user hien tai.",
        "Khong quet va huy task cua module khac.",
        "Khong unregister module.",
        "Khong xoa global state.",
        "Khong can thiep module cupdien.",
    ],
}


# ============================================================
# SESSION REGISTRIES OWNED BY COMMANDS THAT /stop MAY CLEAN
# ============================================================
#
# Day la cac session command cu. Khong dung registry generic.
#

ALLOWED_SESSION_ATTRS = (
    "_dragon_sessions",
    "_dragon_download_sessions",
    "_getuidfb_sessions",
    "_uid_sessions",
    "_uidfb_sessions",
    "_download_sessions",
)


# ============================================================
# SAFE HELPERS
# ============================================================

def _user_keys(user_id):
    return (
        user_id,
        str(user_id),
    )


def _get_dict(bot, attr_name):
    if attr_name not in ALLOWED_SESSION_ATTRS:
        return None

    try:
        value = getattr(bot, attr_name, None)
    except Exception:
        return None

    if not isinstance(value, dict):
        return None

    return value


def _remove_allowed_sessions(bot, user_id):
    removed = 0

    for attr_name in ALLOWED_SESSION_ATTRS:
        registry = _get_dict(bot, attr_name)

        if registry is None:
            continue

        for key in _user_keys(user_id):
            try:
                if key in registry:
                    registry.pop(key, None)
                    removed += 1
            except Exception:
                continue

    return removed


def _count_allowed_sessions(bot, user_id):
    count = 0

    for attr_name in ALLOWED_SESSION_ATTRS:
        registry = _get_dict(bot, attr_name)

        if registry is None:
            continue

        for key in _user_keys(user_id):
            try:
                if key in registry:
                    count += 1
                    break
            except Exception:
                continue

    return count


# ============================================================
# GETUIDFB STOP ADAPTER
# ============================================================
#
# getuidfb hien tai co registry:
#
#     _active_sessions[(chat_id, user_id)]
#
# va moi session co:
#
#     task
#     stop_event
#
# /stop can dung vong lap nay.
#
# Ta KHONG xoa _active_sessions tu file nay.
# Ta chi goi mot hook dung session neu module co hook.
#
# ============================================================

async def _stop_getuidfb(bot, user_id):
    """
    Gui stop signal den getuidfb neu module co stop hook.

    Tra ve:
        {
            "found": bool,
            "cancelled": int,
            "signalled": int,
            "hook": bool,
        }
    """

    result = {
        "found": False,
        "cancelled": 0,
        "signalled": 0,
        "hook": False,
    }

    module_candidates = (
        "getuidfb_v64_TURBO",
        "getuidfb",
        "commands.getuidfb",
    )

    module = None

    for module_name in module_candidates:
        try:
            module = importlib.import_module(module_name)
            break
        except Exception:
            continue

    if module is None:
        return result

    # --------------------------------------------------------
    # Preferred interface:
    #
    #     async def stop_user_sessions(user_id)
    #
    # --------------------------------------------------------

    hook = getattr(
        module,
        "stop_user_sessions",
        None,
    )

    if callable(hook):
        try:
            value = hook(user_id)

            if inspect.isawaitable(value):
                value = await value

            result["hook"] = True

            if isinstance(value, dict):
                result.update(
                    {
                        key: value[key]
                        for key in (
                            "found",
                            "cancelled",
                            "signalled",
                        )
                        if key in value
                    }
                )

            return result

        except asyncio.CancelledError:
            raise

        except Exception:
            return result

    # --------------------------------------------------------
    # Compatibility fallback.
    #
    # Chi doc registry de gui stop_event/cancel task.
    # Khong pop/xoa registry.
    # --------------------------------------------------------

    sessions = getattr(
        module,
        "_active_sessions",
        None,
    )

    if not isinstance(sessions, dict):
        return result

    matched = []

    for key, session in list(sessions.items()):
        if not isinstance(session, dict):
            continue

        try:
            session_user_id = int(key[1])
        except Exception:
            continue

        try:
            target_user_id = int(user_id)
        except Exception:
            target_user_id = user_id

        if session_user_id != target_user_id:
            continue

        matched.append(session)

    if not matched:
        return result

    result["found"] = True

    current = asyncio.current_task()

    for session in matched:
        stop_event = session.get("stop_event")
        task = session.get("task")

        if stop_event is not None:
            try:
                stop_event.set()
                result["signalled"] += 1
            except Exception:
                pass

        if (
            isinstance(task, asyncio.Task)
            and task is not current
            and not task.done()
        ):
            try:
                task.cancel()
                result["cancelled"] += 1
            except Exception:
                pass

    return result


# ============================================================
# TASK MANAGER
# ============================================================

async def _stop_task_manager(user_id):
    try:
        value = stop_user_tasks(user_id)

        if inspect.isawaitable(value):
            await value

        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


# ============================================================
# POWER SESSION
# ============================================================

async def _clear_power_session(bot, user_id):
    try:
        from core.power.session import clear_session

        value = clear_session(
            bot,
            user_id,
        )

        if inspect.isawaitable(value):
            await value

        return True

    except Exception:
        return False


# ============================================================
# EXECUTION
# ============================================================

async def _execute_stop(bot, user_id):
    started = time.perf_counter()

    result = {
        "task_manager": False,
        "getuidfb_found": False,
        "getuidfb_signalled": 0,
        "getuidfb_cancelled": 0,
        "power_session": False,
        "sessions_removed": 0,
        "sessions_remaining": 0,
        "elapsed_ms": 0.0,
    }

    # 1. Stop officially managed tasks.
    result["task_manager"] = await _stop_task_manager(
        user_id
    )

    # 2. Stop getuidfb's own active loop.
    getuidfb_result = await _stop_getuidfb(
        bot,
        user_id,
    )

    result["getuidfb_found"] = getuidfb_result.get(
        "found",
        False,
    )

    result["getuidfb_signalled"] = getuidfb_result.get(
        "signalled",
        0,
    )

    result["getuidfb_cancelled"] = getuidfb_result.get(
        "cancelled",
        0,
    )

    # 3. Clear only the user's power session.
    result["power_session"] = await _clear_power_session(
        bot,
        user_id,
    )

    # 4. Clean only explicitly allowed command sessions.
    result["sessions_removed"] = _remove_allowed_sessions(
        bot,
        user_id,
    )

    await asyncio.sleep(0)

    result["sessions_remaining"] = _count_allowed_sessions(
        bot,
        user_id,
    )

    result["elapsed_ms"] = (
        time.perf_counter() - started
    ) * 1000.0

    return result


# ============================================================
# UI
# ============================================================

def _render_report(user_id, result):
    task_ok = result["task_manager"]
    getuidfb_found = result["getuidfb_found"]
    getuidfb_cancelled = result["getuidfb_cancelled"]
    getuidfb_signalled = result["getuidfb_signalled"]

    power_ok = result["power_session"]
    removed = result["sessions_removed"]
    remaining = result["sessions_remaining"]
    elapsed = result["elapsed_ms"]

    if (
        remaining == 0
        and (
            task_ok
            or getuidfb_found
            or power_ok
            or removed == 0
        )
    ):
        status = "DA DUNG"
        icon = "OK"
        detail = "Khong con session command duoc quan ly."
    else:
        status = "DANG DUNG"
        icon = "WAIT"
        detail = (
            "Lenh dung da duoc gui. Mot so task co the "
            "can them mot nhip de ket thuc."
        )

    if getuidfb_found:
        getuidfb_line = (
            f"<code>[OK] Vong lap GetUIDFB: "
            f"signal={getuidfb_signalled}, "
            f"cancel={getuidfb_cancelled}</code>"
        )
    else:
        getuidfb_line = (
            "<code>[--] Vong lap GetUIDFB: khong hoat dong</code>"
        )

    return (
        "<b>USER KILL SWITCH</b>\n"
        "<i>Bo dieu khien dung tien trinh cua ban</i>\n"
        "\n"
        "<b>TRANG THAI</b>\n"
        f"<code>[{icon}] {status}</code>\n"
        f"{detail}\n"
        "\n"
        "<b>TIEN TRINH</b>\n"
        f"<code>[{'OK' if task_ok else '--'}] Task Manager</code>\n"
        f"{getuidfb_line}\n"
        f"<code>[{'OK' if power_ok else '--'}] Power session</code>\n"
        "\n"
        "<b>PHIEN CUA BAN</b>\n"
        f"<code>Da dong    : {removed}</code>\n"
        f"<code>Con lai    : {remaining}</code>\n"
        f"<code>Thoi gian  : {elapsed:.1f} ms</code>\n"
        "\n"
        "<b>BAO VE</b>\n"
        "<code>[OK] Chi xu ly user hien tai</code>\n"
        "<code>[OK] Module da dang ky duoc bao ve</code>\n"
        "<code>[OK] Global state khong bi xoa</code>\n"
        "<code>[OK] User khac khong bi anh huong</code>\n"
        "\n"
        "He thong san sang. "
        "Dung <code>/getuidfb</code> de bat dau lai."
    )


# ============================================================
# REGISTER
# ============================================================

def register(bot, notify_bot):

    @bot.on(
        events.NewMessage(
            pattern=r"^/stop(?:@\w+)?$"
        )
    )
    async def stop(event):

        user_id = event.sender_id or 0

        try:
            message = await event.reply(
                "<b>USER KILL SWITCH</b>\n"
                "\n"
                "<code>Dang dung tien trinh cua ban...</code>",
                parse_mode="html",
            )
        except Exception:
            message = None

        result = await _execute_stop(
            bot,
            user_id,
        )

        report = _render_report(
            user_id,
            result,
        )

        if message is not None:
            try:
                await message.edit(
                    report,
                    parse_mode="html",
                )
                return
            except Exception:
                pass

        try:
            await event.reply(
                report,
                parse_mode="html",
            )
        except Exception:
            pass
