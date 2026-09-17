# ============================================================
# DROP-IN HOOK FOR getuidfb
# ============================================================
#
# Dat ham nay trong module getuidfb (gan _active_sessions).
# /stop.py se tu dong goi ham nay neu import duoc module.
#
# Khong unregister handler.
# Khong xoa session registry tai day.
# Chi signal/cancel session cua user.
# ============================================================

async def stop_user_sessions(user_id):
    result = {
        "found": False,
        "signalled": 0,
        "cancelled": 0,
    }

    try:
        target_user_id = int(user_id)
    except Exception:
        target_user_id = user_id

    current_task = asyncio.current_task()

    async with _pending_lock:
        matched = []

        for session_key, session in _active_sessions.items():
            try:
                session_user_id = int(session_key[1])
            except Exception:
                continue

            if session_user_id != target_user_id:
                continue

            if not isinstance(session, dict):
                continue

            matched.append(session)

        if not matched:
            return result

        result["found"] = True

        for session in matched:
            session["stopped"] = True

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
                and task is not current_task
                and not task.done()
            ):
                try:
                    task.cancel()
                    result["cancelled"] += 1
                except Exception:
                    pass

    return result
