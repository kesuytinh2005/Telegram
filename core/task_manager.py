# ============================================================
# CORE - TASK MANAGER
# Quản lý asyncio.Task theo từng Telegram user
# ============================================================

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================
# USER TASK STORAGE
# ============================================================

# {
#     user_id: asyncio.Task
# }
_USER_TASKS: dict[int, asyncio.Task] = {}

# Lock để tránh race-condition khi 2 command đến gần như cùng lúc
_TASK_LOCK = asyncio.Lock()


# ============================================================
# INTERNAL
# ============================================================

def _task_done_callback(user_id: int, task: asyncio.Task):
    """
    Tự động xóa task khỏi manager khi task kết thúc.
    """

    current = _USER_TASKS.get(user_id)

    # Chỉ xóa nếu đây vẫn là task hiện tại của user.
    # Tránh trường hợp task cũ kết thúc sau khi task mới đã được tạo.
    if current is task:
        _USER_TASKS.pop(user_id, None)

    try:
        if task.cancelled():
            logger.info(
                "[TASK MANAGER] User %s task cancelled",
                user_id
            )
            return

        exception = task.exception()

        if exception:
            logger.error(
                "[TASK MANAGER] User %s task crashed: %s",
                user_id,
                exception,
                exc_info=exception,
            )

    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception(
            "[TASK MANAGER] Error checking task of user %s",
            user_id
        )


# ============================================================
# GET CURRENT TASK
# ============================================================

def get_current_task(user_id: int) -> Optional[asyncio.Task]:
    """
    Lấy task hiện tại của user.
    """

    task = _USER_TASKS.get(user_id)

    if task is None:
        return None

    if task.done():
        _USER_TASKS.pop(user_id, None)
        return None

    return task


# ============================================================
# CHECK RUNNING
# ============================================================

def has_running_task(user_id: int) -> bool:
    """
    Kiểm tra user có task đang chạy hay không.
    """

    task = get_current_task(user_id)

    return task is not None and not task.done()


# ============================================================
# STOP ONE USER TASK
# ============================================================

async def stop_user_tasks(user_id: int):
    """
    Hủy toàn bộ task hiện tại của user.

    Đây là hàm quan trọng nhất cho /stop.
    """

    async with _TASK_LOCK:

        task = _USER_TASKS.pop(user_id, None)

        if task is None:
            return

        if task.done():
            return

        logger.info(
            "[TASK MANAGER] Cancelling task of user %s",
            user_id
        )

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(
                "[TASK MANAGER] Task user %s stopped with error: %s",
                user_id,
                e,
            )


# ============================================================
# REPLACE USER TASK
# ============================================================

async def replace_user_tasks(user_id: int, coro=None):
    """
    Hủy task cũ của user.

    Nếu có coro:
        - Hủy task cũ
        - Tạo task mới

    Nếu không có coro:
        - Chỉ hủy task cũ

    Dùng được cho cả:

        await replace_user_tasks(user_id)

    và:

        await replace_user_tasks(user_id, worker())
    """

    async with _TASK_LOCK:

        # ====================================================
        # HỦY TASK CŨ
        # ====================================================

        old_task = _USER_TASKS.pop(user_id, None)

        if old_task is not None and not old_task.done():

            logger.info(
                "[TASK MANAGER] Cancelling old task of user %s",
                user_id
            )

            old_task.cancel()

            try:
                await old_task

            except asyncio.CancelledError:
                pass

            except Exception as e:
                logger.warning(
                    "[TASK MANAGER] Old task %s stopped with error: %s",
                    user_id,
                    e,
                )

        # ====================================================
        # KHÔNG CÓ TASK MỚI
        # ====================================================

        if coro is None:
            logger.info(
                "[TASK MANAGER] User %s: old task stopped",
                user_id
            )

            return None

        # ====================================================
        # TẠO TASK MỚI
        # ====================================================

        task = asyncio.create_task(coro)

        _USER_TASKS[user_id] = task

        task.add_done_callback(
            lambda t: _task_done_callback(user_id, t)
        )

        logger.info(
            "[TASK MANAGER] User %s: new task started",
            user_id
        )

        return task


# ============================================================
# TRACK CURRENT TASK
# ============================================================

def track_current_task(
    user_id: int,
    task: asyncio.Task,
):
    """
    Đăng ký một asyncio.Task đã được tạo từ bên ngoài.

    Dùng khi code hiện tại của bạn đã có:

        task = asyncio.create_task(...)
    """

    old_task = _USER_TASKS.get(user_id)

    if old_task is not None and old_task is not task:
        if not old_task.done():
            old_task.cancel()

    _USER_TASKS[user_id] = task

    task.add_done_callback(
        lambda t: _task_done_callback(user_id, t)
    )

    logger.info(
        "[TASK MANAGER] Tracking task for user %s",
        user_id
    )


# ============================================================
# UNTRACK CURRENT TASK
# ============================================================

def untrack_current_task(
    user_id: int,
    task: Optional[asyncio.Task] = None,
):
    """
    Xóa task khỏi manager.

    Nếu truyền task thì chỉ xóa nếu task đó vẫn là task hiện tại.
    """

    current = _USER_TASKS.get(user_id)

    if current is None:
        return

    if task is not None and current is not task:
        return

    _USER_TASKS.pop(user_id, None)


# ============================================================
# CANCEL ALL USERS
# ============================================================

async def stop_all_tasks():
    """
    Dừng toàn bộ task của tất cả user.

    Dùng khi bot shutdown/restart.
    """

    async with _TASK_LOCK:

        tasks = list(_USER_TASKS.values())

        _USER_TASKS.clear()

        if not tasks:
            return

        logger.info(
            "[TASK MANAGER] Cancelling %s tasks",
            len(tasks)
        )

        for task in tasks:
            if not task.done():
                task.cancel()

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                if not isinstance(result, asyncio.CancelledError):
                    logger.warning(
                        "[TASK MANAGER] Task shutdown error: %s",
                        result,
                    )


# ============================================================
# LIST RUNNING TASKS
# ============================================================

def get_running_users() -> list[int]:
    """
    Trả về danh sách user đang có task chạy.
    """

    users = []

    for user_id, task in list(_USER_TASKS.items()):

        if task.done():
            _USER_TASKS.pop(user_id, None)
            continue

        users.append(user_id)

    return users


# ============================================================
# DEBUG
# ============================================================

def get_task_info(user_id: int):
    """
    Lấy thông tin task hiện tại.
    """

    task = get_current_task(user_id)

    if task is None:
        return None

    return {
        "user_id": user_id,
        "task": task,
        "done": task.done(),
        "cancelled": task.cancelled(),
    }