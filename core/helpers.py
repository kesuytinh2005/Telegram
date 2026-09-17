from html import escape


def get_user_name(user):
    if user.first_name:
        name = user.first_name

        if user.last_name:
            name += f" {user.last_name}"

        return escape(name)

    return "Không có tên"


def get_username(user):
    if user.username:
        return f"@{escape(user.username)}"

    return "Không có username"


def command_usage(info):
    return escape(
        info.get(
            "usage",
            f"/{info.get('command', '')}"
        )
    )