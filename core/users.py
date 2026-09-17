# ============================================================
# core/users.py
# ============================================================

import os
import json
from datetime import datetime


USERS_FILE = "data/users.json"


# ============================================================
# INIT
# ============================================================

def _ensure_file():

    folder = os.path.dirname(USERS_FILE)

    if folder:
        os.makedirs(folder, exist_ok=True)

    if not os.path.exists(USERS_FILE):

        with open(
            USERS_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                {},
                f,
                ensure_ascii=False,
                indent=2
            )


# ============================================================
# LOAD
# ============================================================

def load_users():

    _ensure_file()

    try:

        with open(
            USERS_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception as e:

        print(f"[USERS LOAD] {e}")

    return {}


# ============================================================
# SAVE
# ============================================================

def save_users(users):

    _ensure_file()

    temp_file = USERS_FILE + ".tmp"

    try:

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                users,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temp_file,
            USERS_FILE
        )

    except Exception as e:

        print(f"[USERS SAVE] {e}")

        try:

            if os.path.exists(temp_file):
                os.remove(temp_file)

        except Exception:
            pass


# ============================================================
# FORMAT USER
# ============================================================

def get_user_info(user):

    user_id = getattr(
        user,
        "id",
        None
    )

    username = getattr(
        user,
        "username",
        None
    )

    first_name = getattr(
        user,
        "first_name",
        None
    )

    last_name = getattr(
        user,
        "last_name",
        None
    )

    full_name = " ".join(
        x
        for x in [
            first_name,
            last_name
        ]
        if x
    ).strip()

    if not full_name:
        full_name = "Không có tên"

    return {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "name": full_name,
    }


# ============================================================
# REGISTER USER
# ============================================================

def register_user(user):

    users = load_users()

    info = get_user_info(user)

    user_id = info["user_id"]

    if not user_id:
        return False, info

    key = str(user_id)

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # --------------------------------------------------------
    # USER MỚI
    # --------------------------------------------------------

    if key not in users:

        users[key] = {
            **info,
            "first_seen": now,
            "last_seen": now,
        }

        save_users(users)

        return True, users[key]

    # --------------------------------------------------------
    # USER CŨ
    # --------------------------------------------------------

    users[key].update({
        "username": info["username"],
        "first_name": info["first_name"],
        "last_name": info["last_name"],
        "name": info["name"],
        "last_seen": now,
    })

    save_users(users)

    return False, users[key]


# ============================================================
# COUNT USER
# ============================================================

def get_user_count():

    users = load_users()

    return len(users)


# ============================================================
# GET ALL USERS
# ============================================================

def get_users():

    return load_users()