# ============================================================
# core/power/session.py
# POWER SESSION
# ============================================================

_SESSIONS = {}


def get_session(
    bot,
    user_id
):
    bot_key = id(bot)

    sessions = _SESSIONS.setdefault(
        bot_key,
        {}
    )

    return sessions.get(
        user_id,
        {}
    )


def set_session(
    bot,
    user_id,
    **kwargs
):
    bot_key = id(bot)

    sessions = _SESSIONS.setdefault(
        bot_key,
        {}
    )

    current = sessions.setdefault(
        user_id,
        {}
    )

    current.update(
        kwargs
    )

    return current


def clear_session(
    bot,
    user_id
):
    bot_key = id(bot)

    sessions = _SESSIONS.get(
        bot_key
    )

    if not sessions:
        return

    sessions.pop(
        user_id,
        None
    )


def has_session(
    bot,
    user_id
):
    bot_key = id(bot)

    sessions = _SESSIONS.get(
        bot_key,
        {}
    )

    return user_id in sessions


def clear_all_sessions(bot):
    _SESSIONS.pop(id(bot), None)
