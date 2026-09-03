# ============================================================
# commands/cupdien.py
# TELEGRAM COMMAND - CÚP ĐIỆN
# ============================================================

from telethon import events, Button

from core.power.api import (
    AREA_CODES,
    get_by_area,
    get_by_customer,
    date_range,
)

from core.power.database import (
    add_subscription,
    get_user_subscriptions,
    remove_subscription,
)

from core.power.session import (
    get_session,
    set_session,
    clear_session,
)

from core.task_manager import replace_user_tasks

COMMAND_INFO = {
    "command": "cupdien",
    "category": "⚡ ĐIỆN LỰC",
    "title": "Lịch cúp điện",

    "description": (
        "Kiểm tra lịch cúp điện theo khu vực hoặc mã khách hàng "
        "và đăng ký theo dõi tự động."
    ),

    "usage": "/cupdien",

    "examples": [
        "/cupdien",
    ],

    "details": [
        "Gửi /cupdien để mở menu cúp điện.",
        "Có thể kiểm tra theo khu vực.",
        "Có thể kiểm tra theo mã khách hàng.",
        "Có thể kiểm tra đồng thời khu vực và mã khách hàng.",
        "Chọn khung giờ để đăng ký theo dõi hằng ngày.",
        "Bot tự động kiểm tra lịch theo thời gian đã chọn.",
        "Nếu có lịch cúp điện mới, bot sẽ tự động gửi thông báo.",
        "Có thể xem danh sách các đăng ký đang theo dõi.",
        "Có thể xóa đăng ký theo dõi bất cứ lúc nào.",
    ],

    "supported": [
        "📍 Theo khu vực",
        "🆔 Theo mã khách hàng",
        "📍 + 🆔 Khu vực và mã khách hàng",
        "⏰ Theo khung giờ",
        "🔔 Tự động thông báo",
        "🗑️ Quản lý đăng ký",
    ],
}
# ============================================================
# ESCAPE HTML
# ============================================================

def esc(value):
    if value is None:
        return ""

    value = str(value)

    return (
        value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ============================================================
# MAIN MENU
# ============================================================

def main_menu():

    return [
        [
            Button.inline(
                "⚡ Kiểm tra ngay",
                b"power:instant"
            )
        ],
        [
            Button.inline(
                "📍 Theo dõi khu vực",
                b"power:type:area"
            ),
            Button.inline(
                "🆔 Theo dõi mã KH",
                b"power:type:customer"
            )
        ],
        [
            Button.inline(
                "📍🆔 Theo dõi cả hai",
                b"power:type:both"
            )
        ],
        [
            Button.inline(
                "📋 Theo dõi của tôi",
                b"power:list"
            )
        ]
    ]


# ============================================================
# START TEXT
# ============================================================

def menu_text():

    return (
        "╭────────────────────╮\n"
        "│  ⚡ <b>QUẢN LÝ CÚP ĐIỆN</b>  │\n"
        "╰────────────────────╯\n\n"

        "Xin chào! 👋\n\n"

        "Bạn có thể sử dụng bot để:\n\n"

        "⚡ <b>Kiểm tra ngay</b>\n"
        "└ Kiểm tra lịch cúp điện hiện tại.\n\n"

        "📍 <b>Theo dõi khu vực</b>\n"
        "└ Tự động kiểm tra lịch theo điện lực.\n\n"

        "🆔 <b>Theo dõi mã khách hàng</b>\n"
        "└ Kiểm tra lịch theo mã KH.\n\n"

        "📍🆔 <b>Theo dõi cả hai</b>\n"
        "└ Kết hợp khu vực + mã KH.\n\n"

        "📋 <b>Theo dõi của tôi</b>\n"
        "└ Xem hoặc xóa đăng ký.\n\n"

        "💡 Chọn chức năng bên dưới để bắt đầu."
    )


# ============================================================
# AREA BUTTONS
# ============================================================

def area_buttons(
    prefix="power:area"
):

    buttons = []

    items = list(
        AREA_CODES.items()
    )

    for i in range(
        0,
        len(items),
        2
    ):

        row = []

        for code, name in items[i:i + 2]:

            short_name = (
                name
                .replace(
                    "Điện lực ",
                    ""
                )
            )

            row.append(
                Button.inline(
                    f"📍 {short_name}",
                    f"{prefix}:{code}".encode()
                )
            )

        buttons.append(row)

    buttons.append(
        [
            Button.inline(
                "🔙 Quay lại",
                b"power:back"
            )
        ]
    )

    return buttons


def area_buttons_instant():

    return area_buttons(
        "power:instantarea"
    )


# ============================================================
# TIME BUTTONS
# ============================================================

def time_buttons(
    prefix
):

    return [
        [
            Button.inline(
                "🌅 06:00",
                f"{prefix}:06:00".encode()
            ),
            Button.inline(
                "🌅 07:00",
                f"{prefix}:07:00".encode()
            ),
            Button.inline(
                "🌅 08:00",
                f"{prefix}:08:00".encode()
            )
        ],
        [
            Button.inline(
                "☀️ 09:00",
                f"{prefix}:09:00".encode()
            ),
            Button.inline(
                "☀️ 10:00",
                f"{prefix}:10:00".encode()
            ),
            Button.inline(
                "☀️ 12:00",
                f"{prefix}:12:00".encode()
            )
        ],
        [
            Button.inline(
                "🌆 18:00",
                f"{prefix}:18:00".encode()
            ),
            Button.inline(
                "🌙 20:00",
                f"{prefix}:20:00".encode()
            ),
            Button.inline(
                "🌙 22:00",
                f"{prefix}:22:00".encode()
            )
        ],
        [
            Button.inline(
                "🔙 Quay lại",
                b"power:back"
            )
        ]
    ]


# ============================================================
# TIME TEXT
# ============================================================

def time_text(
    area_name=None,
    customer=None
):

    text = (
        "╭──────────────────────────╮\n"
        "│  ⏰ <b>CHỌN GIỜ KIỂM TRA</b>  │\n"
        "╰──────────────────────────╯\n\n"
    )

    if area_name:

        text += (
            f"📍 Khu vực: "
            f"<b>{esc(area_name)}</b>\n\n"
        )

    if customer:

        text += (
            f"🆔 Mã KH: "
            f"<code>{esc(customer)}</code>\n\n"
        )

    text += (
        "Chọn thời điểm bot tự động kiểm tra mỗi ngày:\n\n"
        "🔔 Nếu phát hiện lịch mới, bot sẽ gửi thông báo.\n"
        "🔄 Nếu bot khởi động sau giờ kiểm tra, hệ thống "
        "vẫn có thể kiểm tra bù."
    )

    return text


# ============================================================
# FORMAT SCHEDULE
# ============================================================

def format_schedule(
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

    start_time = schedule.get(
        "start_time",
        ""
    )

    start_date = schedule.get(
        "start_date",
        ""
    )

    end_time = schedule.get(
        "end_time",
        ""
    )

    end_date = schedule.get(
        "end_date",
        ""
    )

    cause = schedule.get(
        "cause",
        ""
    )

    text = (
        "⚡ <b>LỊCH CÚP ĐIỆN</b>\n\n"
    )

    if where:

        text += (
            f"📍 <b>Khu vực:</b> "
            f"{esc(where)}\n"
        )

    if start_time or start_date:

        text += (
            f"🕐 <b>Từ:</b> "
            f"{esc(start_time)} "
            f"{esc(start_date)}\n"
        )

    if end_time or end_date:

        text += (
            f"🕐 <b>Đến:</b> "
            f"{esc(end_time)} "
            f"{esc(end_date)}\n"
        )

    if cause:

        text += (
            f"📝 <b>Lý do:</b> "
            f"{esc(cause)}\n"
        )

    if code:

        text += (
            f"🔖 <b>Mã lịch:</b> "
            f"<code>{esc(code)}</code>\n"
        )

    return text


# ============================================================
# FORMAT RESULT
# ============================================================

def format_result(
    result,
    area_name=None,
    customer=None
):

    schedules = result.get(
        "schedules",
        []
    )

    text = (
        "╭──────────────────────────╮\n"
        "│  ⚡ <b>KẾT QUẢ KIỂM TRA</b>  │\n"
        "╰──────────────────────────╯\n\n"
    )

    if area_name:

        text += (
            f"📍 <b>Khu vực:</b> "
            f"{esc(area_name)}\n"
        )

    if customer:

        text += (
            f"🆔 <b>Mã KH:</b> "
            f"<code>{esc(customer)}</code>\n"
        )

    text += "\n"

    if not schedules:

        text += (
            "✅ <b>Không tìm thấy lịch cúp điện.</b>\n\n"
            "Không có dữ liệu trong khoảng thời gian "
            "tra cứu hiện tại."
        )

        return text

    text += (
        f"📋 Tìm thấy "
        f"<b>{len(schedules)}</b> lịch:\n\n"
    )

    for index, schedule in enumerate(
        schedules,
        1
    ):

        text += (
            f"<b>━━ LỊCH #{index} ━━</b>\n"
        )

        if schedule.get("where"):

            text += (
                f"📍 {esc(schedule.get('where'))}\n"
            )

        if (
            schedule.get("start_time")
            or schedule.get("start_date")
        ):

            text += (
                f"🕐 Từ: "
                f"{esc(schedule.get('start_time'))} "
                f"{esc(schedule.get('start_date'))}\n"
            )

        if (
            schedule.get("end_time")
            or schedule.get("end_date")
        ):

            text += (
                f"🕐 Đến: "
                f"{esc(schedule.get('end_time'))} "
                f"{esc(schedule.get('end_date'))}\n"
            )

        if schedule.get("cause"):

            text += (
                f"📝 {esc(schedule.get('cause'))}\n"
            )

        if schedule.get("code"):

            text += (
                f"🔖 <code>"
                f"{esc(schedule.get('code'))}"
                f"</code>\n"
            )

        text += "\n"

    return text


# ============================================================
# BUILD LIST
# ============================================================

def build_list(
    user_id
):

    subscriptions = (
        get_user_subscriptions(
            user_id
        )
    )

    if not subscriptions:

        return (
            "╭──────────────────────────╮\n"
            "│  📋 <b>THEO DÕI CỦA TÔI</b>  │\n"
            "╰──────────────────────────╯\n\n"
            "Bạn chưa có đăng ký nào.",
            [
                [
                    Button.inline(
                        "🔙 Quay lại",
                        b"power:back"
                    )
                ]
            ]
        )

    text = (
        "╭──────────────────────────╮\n"
        "│  📋 <b>THEO DÕI CỦA TÔI</b>  │\n"
        "╰──────────────────────────╯\n\n"
    )

    buttons = []

    for index, sub in enumerate(
        subscriptions,
        1
    ):

        sub_type = sub.get(
            "type",
            ""
        )

        value = sub.get(
            "value",
            ""
        )

        area_name = sub.get(
            "area_name",
            ""
        )

        check_time = sub.get(
            "check_time",
            "07:00"
        )

        if sub_type == "area":

            icon = "📍"
            label = (
                area_name
                or value
            )

        else:

            icon = "🆔"
            label = value

        text += (
            f"<b>#{index}</b> {icon} "
            f"{esc(label)}\n"
            f"⏰ {esc(check_time)} mỗi ngày\n"
        )

        if area_name and sub_type == "customer":

            text += (
                f"📍 {esc(area_name)}\n"
            )

        text += "\n"

        buttons.append(
            [
                Button.inline(
                    f"🗑 Xóa #{index}",
                    f"power:delete:{sub['id']}".encode()
                )
            ]
        )

    buttons.append(
        [
            Button.inline(
                "🔙 Quay lại",
                b"power:back"
            )
        ]
    )

    return text, buttons


# ============================================================
# REGISTER
# ============================================================

def register(
    bot,
    notify_bot
):

    # ========================================================
    # COMMAND
    # ========================================================

    @bot.on(
        events.NewMessage(
            pattern=r"^/cupdien(?:@\w+)?$"
        )
    )
    async def cupdien_start(
        event
    ):

        # Command mới thay thế toàn bộ task nền cũ của user.
        await replace_user_tasks(event.sender_id)

        # Xóa session chờ của command cũ.
        for _attr in ("_dragon_sessions", "_dragon_download_sessions"):
            _sessions = getattr(bot, _attr, None)
            if isinstance(_sessions, dict):
                _sessions.pop(event.sender_id, None)

        try:
            from core.power.session import clear_session
            clear_session(bot, event.sender_id)
        except Exception:
            pass


        clear_session(
            bot,
            event.sender_id
        )

        await event.reply(
            menu_text(),
            buttons=main_menu(),
            parse_mode="html"
        )

    # ========================================================
    # MESSAGE INPUT
    # ========================================================

    @bot.on(
        events.NewMessage()
    )
    async def cupdien_message(
        event
    ):

        if not event.raw_text:

            return

        text = event.raw_text.strip()

        if text.startswith("/"):

            return

        user_id = event.sender_id

        session = get_session(
            bot,
            user_id
        )

        if not session:

            return

        state = session.get(
            "state"
        )

        # ----------------------------------------------------
        # NHẬP MÃ KH
        # ----------------------------------------------------

        if state in (
            "customer_input",
            "instant_customer_input"
        ):

            customer = (
                text
                .strip()
                .upper()
            )

            if len(customer) < 5:

                await event.reply(
                    "❌ Mã khách hàng không hợp lệ.\n\n"
                    "Ví dụ:\n"
                    "<code>PB01050001178</code>",
                    parse_mode="html"
                )

                return

            mode = session.get(
                "mode",
                "customer"
            )

            set_session(
                bot,
                user_id,
                state=(
                    "customer_confirm"
                    if mode == "customer"
                    else "instant_customer_confirm"
                ),
                customer=customer
            )

            area_name = session.get(
                "area_name"
            )

            message = (
                "╭──────────────────────────╮\n"
                "│  🆔 <b>XÁC NHẬN MÃ KH</b>  │\n"
                "╰──────────────────────────╯\n\n"

                f"🆔 Mã KH:\n"
                f"<code>{esc(customer)}</code>\n\n"
            )

            if area_name:

                message += (
                    f"📍 Khu vực: "
                    f"<b>{esc(area_name)}</b>\n\n"
                )

            message += (
                "Nhấn xác nhận để tiếp tục."
            )

            await event.reply(
                message,
                buttons=[
                    [
                        Button.inline(
                            "✅ Xác nhận",
                            f"power:confirmcustomer:{customer}".encode()
                        )
                    ],
                    [
                        Button.inline(
                            "🔙 Quay lại",
                            b"power:back"
                        )
                    ]
                ],
                parse_mode="html"
            )

            return

    # ========================================================
    # CALLBACK
    # ========================================================

    @bot.on(
        events.CallbackQuery(
            pattern=b"power:"
        )
    )
    async def power_callback(
        event
    ):

        user_id = event.sender_id

        try:

            data = event.data.decode(
                "utf-8"
            )

            parts = data.split(
                ":",
                2
            )

            action = (
                parts[1]
                if len(parts) > 1
                else ""
            )

            value = (
                parts[2]
                if len(parts) > 2
                else ""
            )

            # =================================================
            # INSTANT
            # =================================================
                        # =================================================
            # CONFIRM CUSTOMER
            # =================================================

            if action == "confirmcustomer":

                customer = (
                    parts[2]
                    if len(parts) > 2
                    else ""
                )

                customer = customer.strip().upper()

                if not customer:

                    await event.answer(
                        "❌ Mã KH không hợp lệ.",
                        alert=True
                    )

                    return

                session = get_session(
                    bot,
                    user_id
                )

                mode = session.get(
                    "mode",
                    "customer"
                )

                area_code = session.get(
                    "area_code"
                )

                area_name = session.get(
                    "area_name"
                )

                # ------------------------------------------------
                # LƯU MÃ KH VÀO SESSION
                # ------------------------------------------------

                set_session(
                    bot,
                    user_id,
                    customer=customer
                )

                # ------------------------------------------------
                # CUSTOMER ONLY
                # ------------------------------------------------

                if mode == "customer":

                    set_session(
                        bot,
                        user_id,
                        state="time_customer_select",
                        mode="customer",
                        customer=customer
                    )

                    await event.edit(
                        "╭──────────────────────────╮\n"
                        "│  ⏰ <b>CHỌN KHUNG GIỜ</b>  │\n"
                        "╰──────────────────────────╯\n\n"

                        f"🆔 Mã KH: "
                        f"<code>{esc(customer)}</code>\n\n"

                        "Chọn thời gian bot sẽ tự động kiểm tra mỗi ngày.",

                        buttons=time_buttons(
                            "power:timecustomer"
                        ),

                        parse_mode="html"
                    )

                    await event.answer()

                    return

                # ------------------------------------------------
                # BOTH
                # ------------------------------------------------

                if mode == "both":

                    set_session(
                        bot,
                        user_id,
                        state="time_both_select",
                        mode="both",
                        customer=customer
                    )

                    await event.edit(
                        "╭──────────────────────────╮\n"
                        "│  ⏰ <b>CHỌN KHUNG GIỜ</b>  │\n"
                        "╰──────────────────────────╯\n\n"

                        "📍 <b>Khu vực:</b> "
                        f"{esc(area_name or area_code or 'Không xác định')}\n\n"

                        "🆔 <b>Mã KH:</b> "
                        f"<code>{esc(customer)}</code>\n\n"

                        "Chọn thời gian bot sẽ tự động kiểm tra mỗi ngày.",

                        buttons=time_buttons(
                            "power:timeboth"
                        ),

                        parse_mode="html"
                    )

                    await event.answer()

                    return

                # ------------------------------------------------
                # FALLBACK
                # ------------------------------------------------

                set_session(
                    bot,
                    user_id,
                    state="time_customer_select",
                    mode="customer",
                    customer=customer
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  ⏰ <b>CHỌN KHUNG GIỜ</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    f"🆔 Mã KH: "
                    f"<code>{esc(customer)}</code>\n\n"

                    "Chọn thời gian kiểm tra mỗi ngày.",

                    buttons=time_buttons(
                        "power:timecustomer"
                    ),

                    parse_mode="html"
                )

                await event.answer()

                return
            if action == "instant":

                set_session(
                    bot,
                    user_id,
                    state="instant_select",
                    mode="instant"
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  ⚡ <b>KIỂM TRA NGAY</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    "Chọn cách muốn kiểm tra:\n\n"

                    "📍 <b>Theo khu vực</b>\n"
                    "└ Kiểm tra toàn bộ khu vực.\n\n"

                    "🆔 <b>Theo mã KH</b>\n"
                    "└ Kiểm tra theo mã khách hàng.\n\n"

                    "📍🆔 <b>Cả hai</b>\n"
                    "└ Kiểm tra khu vực + mã KH.",

                    buttons=[
                        [
                            Button.inline(
                                "📍 Khu vực",
                                b"power:instant_area"
                            ),
                            Button.inline(
                                "🆔 Mã KH",
                                b"power:instant_customer"
                            )
                        ],
                        [
                            Button.inline(
                                "📍 + 🆔 Cả hai",
                                b"power:instant_both"
                            )
                        ],
                        [
                            Button.inline(
                                "🔙 Quay lại",
                                b"power:back"
                            )
                        ]
                    ],

                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # INSTANT AREA
            # =================================================

            if action == "instant_area":

                set_session(
                    bot,
                    user_id,
                    state="instant_area_select",
                    mode="instant_area"
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  📍 <b>CHỌN KHU VỰC</b>  │\n"
                    "╰──────────────────────────╯\n\n"
                    "Chọn tỉnh / điện lực:",

                    buttons=area_buttons_instant(),

                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # INSTANT AREA SELECT
            # =================================================

            if action == "instantarea":

                area_code = value

                if area_code not in AREA_CODES:

                    await event.answer(
                        "❌ Khu vực không hợp lệ.",
                        alert=True
                    )

                    return

                await event.edit(
                    "⏳ <b>ĐANG KIỂM TRA...</b>\n\n"
                    f"📍 {esc(AREA_CODES[area_code])}\n\n"
                    "Vui lòng chờ...",
                    parse_mode="html"
                )

                try:

                    tu_ngay, den_ngay = date_range()

                    result = await get_by_area(
                        area_code,
                        tu_ngay,
                        den_ngay
                    )

                    text = format_result(
                        result,
                        area_name=AREA_CODES[
                            area_code
                        ]
                    )

                    await event.edit(
                        text,
                        buttons=[
                            [
                                Button.inline(
                                    "🔙 Quay lại",
                                    b"power:instant"
                                )
                            ]
                        ],
                        parse_mode="html"
                    )

                except Exception as e:

                    await event.edit(
                        "❌ <b>Không thể kiểm tra</b>\n\n"
                        f"📍 {esc(AREA_CODES[area_code])}\n\n"
                        f"⚠️ <code>{esc(e)}</code>",
                        parse_mode="html"
                    )

                await event.answer()

                return

            # =================================================
            # INSTANT CUSTOMER
            # =================================================

            if action == "instant_customer":

                set_session(
                    bot,
                    user_id,
                    state="instant_customer_input",
                    mode="instant_customer"
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  🆔 <b>KIỂM TRA MÃ KH</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    "📥 Gửi mã khách hàng cần kiểm tra.\n\n"

                    "Ví dụ:\n"
                    "<code>PB01050001178</code>",

                    buttons=[
                        [
                            Button.inline(
                                "🔙 Quay lại",
                                b"power:instant"
                            )
                        ]
                    ],

                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # INSTANT CUSTOMER CONFIRM
            # =================================================

            if action == "customer_confirm":

                session = get_session(
                    bot,
                    user_id
                )

                customer = session.get(
                    "customer"
                )

                if not customer:

                    await event.answer(
                        "❌ Không tìm thấy mã KH.",
                        alert=True
                    )

                    return

                area_code = session.get(
                    "area_code"
                )

                area_name = session.get(
                    "area_name"
                )

                instant = session.get(
                    "mode"
                ) == "instant_customer"

                if instant:

                    clear_session(
                        bot,
                        user_id
                    )

                    await event.edit(
                        "⏳ <b>ĐANG KIỂM TRA...</b>\n\n"
                        f"🆔 <code>{esc(customer)}</code>",
                        parse_mode="html"
                    )

                    try:

                        tu_ngay, den_ngay = date_range()

                        result = await get_by_customer(
                            customer,
                            tu_ngay,
                            den_ngay
                        )

                        text = format_result(
                            result,
                            area_name=area_name,
                            customer=customer
                        )

                        await event.edit(
                            text,
                            buttons=[
                                [
                                    Button.inline(
                                        "🔙 Quay lại",
                                        b"power:instant"
                                    )
                                ]
                            ],
                            parse_mode="html"
                        )

                    except Exception as e:

                        await event.edit(
                            "❌ <b>Không thể kiểm tra</b>\n\n"
                            f"🆔 <code>{esc(customer)}</code>\n\n"
                            f"⚠️ <code>{esc(e)}</code>",
                            parse_mode="html"
                        )

                    await event.answer()

                    return

            # =================================================
            # INSTANT BOTH
            # =================================================

            if action == "instant_both":

                set_session(
                    bot,
                    user_id,
                    state="instant_area_select",
                    mode="instant_both"
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  📍 <b>BƯỚC 1/2</b>  │\n"
                    "╰──────────────────────────╯\n\n"
                    "Chọn tỉnh / điện lực:",

                    buttons=area_buttons_instant(),

                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # TYPE
            # =================================================

            if action == "type":

                mode = value

                if mode == "area":

                    set_session(
                        bot,
                        user_id,
                        state="area_select",
                        mode="area"
                    )

                    await event.edit(
                        "📍 <b>CHỌN KHU VỰC</b>\n\n"
                        "Chọn tỉnh / điện lực:",
                        buttons=area_buttons(),
                        parse_mode="html"
                    )

                    await event.answer()

                    return

                if mode == "customer":

                    set_session(
                        bot,
                        user_id,
                        state="customer_input",
                        mode="customer"
                    )

                    await event.edit(
                        "🆔 <b>NHẬP MÃ KHÁCH HÀNG</b>\n\n"
                        "Ví dụ:\n"
                        "<code>PB01050001178</code>",
                        buttons=[
                            [
                                Button.inline(
                                    "🔙 Quay lại",
                                    b"power:back"
                                )
                            ]
                        ],
                        parse_mode="html"
                    )

                    await event.answer()

                    return

                if mode == "both":

                    set_session(
                        bot,
                        user_id,
                        state="area_select",
                        mode="both"
                    )

                    await event.edit(
                        "📍 <b>BƯỚC 1/2 — CHỌN KHU VỰC</b>\n\n"
                        "Chọn tỉnh / điện lực:",
                        buttons=area_buttons(),
                        parse_mode="html"
                    )

                    await event.answer()

                    return

            # =================================================
            # AREA
            # =================================================

            if action == "area":

                area_code = value

                if area_code not in AREA_CODES:

                    await event.answer(
                        "❌ Khu vực không hợp lệ.",
                        alert=True
                    )

                    return

                session = get_session(
                    bot,
                    user_id
                )

                mode = session.get(
                    "mode",
                    "area"
                )

                set_session(
                    bot,
                    user_id,
                    area_code=area_code,
                    area_name=AREA_CODES[
                        area_code
                    ]
                )

                if mode == "area":

                    set_session(
                        bot,
                        user_id,
                        state="time_select"
                    )

                    await event.edit(
                        time_text(
                            area_name=AREA_CODES[
                                area_code
                            ]
                        ),
                        buttons=time_buttons(
                            "power:timearea"
                        ),
                        parse_mode="html"
                    )

                    await event.answer()

                    return

            # =================================================
            # CONFIRM AREA
            # =================================================

            if action == "confirmarea":

                area_code = value

                session = get_session(
                    bot,
                    user_id
                )

                mode = session.get(
                    "mode",
                    "area"
                )

                area_name = AREA_CODES.get(
                    area_code
                )

                if not area_name:

                    await event.answer(
                        "❌ Khu vực không hợp lệ.",
                        alert=True
                    )

                    return

                if mode == "area":

                    set_session(
                        bot,
                        user_id,
                        state="time_select",
                        area_code=area_code,
                        area_name=area_name
                    )

                    await event.edit(
                        time_text(
                            area_name=area_name
                        ),
                        buttons=time_buttons(
                            "power:timearea"
                        ),
                        parse_mode="html"
                    )

                    await event.answer()

                    return

                if mode == "both":

                    set_session(
                        bot,
                        user_id,
                        state="customer_input",
                        area_code=area_code,
                        area_name=area_name
                    )

                    await event.edit(
                        "🆔 <b>BƯỚC 2/2 — NHẬP MÃ KH</b>\n\n"
                        f"📍 Khu vực: "
                        f"<b>{esc(area_name)}</b>\n\n"
                        "Gửi mã khách hàng.\n\n"
                        "Ví dụ:\n"
                        "<code>PB01050001178</code>",
                        parse_mode="html"
                    )

                    await event.answer()

                    return

            # =================================================
            # TIME AREA
            # =================================================

            if action == "timearea":

                check_time = value or "07:00"

                session = get_session(
                    bot,
                    user_id
                )

                area_code = session.get(
                    "area_code"
                )

                area_name = session.get(
                    "area_name"
                )

                if not area_code:

                    await event.answer(
                        "❌ Phiên đã hết.",
                        alert=True
                    )

                    return

                add_subscription(
                    user_id=user_id,
                    sub_type="area",
                    value=area_code,
                    area_code=area_code,
                    area_name=area_name,
                    check_time=check_time
                )

                clear_session(
                    bot,
                    user_id
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  ✅ <b>ĐÃ BẬT THEO DÕI</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    f"📍 Khu vực: "
                    f"<b>{esc(area_name)}</b>\n\n"

                    f"⏰ Kiểm tra: "
                    f"<b>{esc(check_time)}</b> mỗi ngày\n\n"

                    "🔔 Khi có lịch mới → bot tự gửi.",
                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # TIME CUSTOMER
            # =================================================

            if action == "timecustomer":

                check_time = value or "07:00"

                session = get_session(
                    bot,
                    user_id
                )

                customer = session.get(
                    "customer"
                )

                if not customer:

                    await event.answer(
                        "❌ Không tìm thấy mã KH.",
                        alert=True
                    )

                    return

                add_subscription(
                    user_id=user_id,
                    sub_type="customer",
                    value=customer,
                    area_code=session.get(
                        "area_code"
                    ),
                    area_name=session.get(
                        "area_name"
                    ),
                    check_time=check_time
                )

                clear_session(
                    bot,
                    user_id
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  ✅ <b>ĐÃ BẬT THEO DÕI</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    f"🆔 Mã KH: "
                    f"<code>{esc(customer)}</code>\n\n"

                    f"⏰ Kiểm tra: "
                    f"<b>{esc(check_time)}</b> mỗi ngày\n\n"

                    "🔔 Khi có lịch mới → bot tự gửi.",
                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # TIME BOTH
            # =================================================

            if action == "timeboth":

                check_time = value or "07:00"

                session = get_session(
                    bot,
                    user_id
                )

                customer = session.get(
                    "customer"
                )

                area_code = session.get(
                    "area_code"
                )

                area_name = session.get(
                    "area_name"
                )

                if not customer or not area_code:

                    await event.answer(
                        "❌ Thiếu thông tin.",
                        alert=True
                    )

                    return

                add_subscription(
                    user_id=user_id,
                    sub_type="customer",
                    value=customer,
                    area_code=area_code,
                    area_name=area_name,
                    check_time=check_time
                )

                add_subscription(
                    user_id=user_id,
                    sub_type="area",
                    value=area_code,
                    area_code=area_code,
                    area_name=area_name,
                    check_time=check_time
                )

                clear_session(
                    bot,
                    user_id
                )

                await event.edit(
                    "╭──────────────────────────╮\n"
                    "│  ✅ <b>ĐÃ BẬT THEO DÕI</b>  │\n"
                    "╰──────────────────────────╯\n\n"

                    f"📍 Khu vực: "
                    f"<b>{esc(area_name)}</b>\n\n"

                    f"🆔 Mã KH: "
                    f"<code>{esc(customer)}</code>\n\n"

                    f"⏰ Kiểm tra: "
                    f"<b>{esc(check_time)}</b> mỗi ngày\n\n"

                    "📍 Theo khu vực: ✅\n"
                    "🆔 Theo mã KH: ✅\n\n"

                    "🔔 Khi có lịch mới → bot tự gửi.",
                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # LIST
            # =================================================

            if action == "list":

                text, buttons = build_list(
                    user_id
                )

                await event.edit(
                    text,
                    buttons=buttons,
                    parse_mode="html"
                )

                await event.answer()

                return

            # =================================================
            # DELETE
            # =================================================

            if action == "delete":

                sub_id = value

                subscriptions = (
                    get_user_subscriptions(
                        user_id
                    )
                )

                target = next(
                    (
                        item
                        for item in subscriptions
                        if str(item.get("id"))
                        == str(sub_id)
                    ),
                    None
                )

                if not target:

                    await event.answer(
                        "❌ Không tìm thấy đăng ký.",
                        alert=True
                    )

                    return

                remove_subscription(
                    sub_id
                )

                await event.answer(
                    "✅ Đã xóa theo dõi."
                )

                text, buttons = build_list(
                    user_id
                )

                await event.edit(
                    text,
                    buttons=buttons,
                    parse_mode="html"
                )

                return

            # =================================================
            # BACK
            # =================================================

            if action == "back":

                clear_session(
                    bot,
                    user_id
                )

                await event.edit(
                    menu_text(),
                    buttons=main_menu(),
                    parse_mode="html"
                )

                await event.answer()

                return

            await event.answer()

        except Exception as e:

            print(
                f"[POWER CALLBACK ERROR] "
                f"{type(e).__name__}: {e}"
            )

            try:

                await event.answer(
                    "❌ Có lỗi xảy ra.",
                    alert=True
                )

            except Exception:

                pass