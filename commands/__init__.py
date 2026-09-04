# ============================================================
# commands/__init__.py
# ============================================================

from commands import (
    start,
    stop,
    download,
    getuidfb,
    cupdien,
    tiktok,
)


def register_all(
    bot,
    notify_bot
):

    modules = [
        start,
        stop,
        download,
        getuidfb,
        cupdien,
        tiktok,
    ]

    for module in modules:

        module.register(
            bot,
            notify_bot
        )

        print(
            f"✅ Loaded command: "
            f"{module.__name__}"
        )