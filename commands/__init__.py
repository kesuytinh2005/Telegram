from commands import (
    start,
    stop,
    download,
    getuidfb,
    cupdien,
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