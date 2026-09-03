# ============================================================
# core/power/checker.py
# POWER CHECK ENGINE
# ============================================================

from core.power.api import (
    get_by_area,
    get_by_customer,
    AREA_CODES,
    date_range,
)


# ============================================================
# FORMAT RESULT
# ============================================================

def format_power_result(
    data,
    area_name=None,
    customer=None,
):
    if not data:
        text = (
            "╭──────────────────────────╮\n"
            "│  ⚡ <b>KẾT QUẢ KIỂM TRA</b>  │\n"
            "╰──────────────────────────╯\n\n"
        )

        if area_name:
            text += (
                f"📍 <b>Khu vực:</b> "
                f"{area_name}\n"
            )

        if customer:
            text += (
                f"🆔 <b>Mã KH:</b> "
                f"<code>{customer}</code>\n"
            )

        text += (
            "\n"
            "✅ Không tìm thấy lịch cúp điện "
            "trong khoảng thời gian tra cứu."
        )

        return text

    text = (
        "╭──────────────────────────╮\n"
        "│  ⚡ <b>LỊCH CÚP ĐIỆN</b>  │\n"
        "╰──────────────────────────╯\n\n"
    )

    if area_name:
        text += (
            f"📍 <b>Khu vực:</b> "
            f"{area_name}\n"
        )

    if customer:
        text += (
            f"🆔 <b>Mã KH:</b> "
            f"<code>{customer}</code>\n"
        )

    text += "\n"

    for i, item in enumerate(data, 1):

        if not isinstance(item, dict):
            continue

        code = (
            item.get("code")
            or ""
        )

        where = (
            item.get("where")
            or ""
        )

        start_time = (
            item.get("start_time")
            or ""
        )

        start_date = (
            item.get("start_date")
            or ""
        )

        end_time = (
            item.get("end_time")
            or ""
        )

        end_date = (
            item.get("end_date")
            or ""
        )

        cause = (
            item.get("cause")
            or ""
        )

        text += (
            f"⚡ <b>Lịch #{i}</b>\n"
        )

        if code:
            text += (
                f"🆔 Mã lịch: "
                f"<code>{code}</code>\n"
            )

        if start_date:
            if end_date and end_date != start_date:
                text += (
                    f"📅 Ngày: "
                    f"<b>{start_date}"
                    f" → {end_date}</b>\n"
                )
            else:
                text += (
                    f"📅 Ngày: "
                    f"<b>{start_date}</b>\n"
                )

        if start_time or end_time:

            if start_time and end_time:
                time_text = (
                    f"{start_time} - {end_time}"
                )

            elif start_time:
                time_text = start_time

            else:
                time_text = end_time

            text += (
                f"⏰ Thời gian: "
                f"<b>{time_text}</b>\n"
            )

        if where:
            text += (
                f"📍 Khu vực: "
                f"{where}\n"
            )

        if cause:
            text += (
                f"📌 Lý do: "
                f"{cause}\n"
            )

        text += "\n"

    return text.rstrip()


# ============================================================
# CHECK AREA
# ============================================================

async def check_area(
    area_code,
    area_name=None,
):
    """
    Kiểm tra lịch cúp điện theo khu vực.

    Dùng chung cho:
    - Kiểm tra ngay
    - Theo dõi định kỳ
    """

    area_code = (
        str(area_code)
        .strip()
        .upper()
    )

    if not area_name:
        area_name = AREA_CODES.get(
            area_code,
            area_code
        )

    try:

        tu_ngay, den_ngay = date_range()

        result = await get_by_area(
            ma_don_vi=area_code,
            tu_ngay=tu_ngay,
            den_ngay=den_ngay,
        )

        schedules = []

        if isinstance(result, dict):

            schedules = result.get(
                "schedules",
                []
            )

        elif isinstance(result, list):

            schedules = result

        return {
            "success": True,
            "area_code": area_code,
            "area_name": area_name,
            "customer": None,
            "data": schedules,
            "text": format_power_result(
                schedules,
                area_name=area_name,
            ),
        }

    except Exception as e:

        print(
            "[POWER CHECK AREA ERROR] "
            f"{area_code}: "
            f"{type(e).__name__}: {e}"
        )

        return {
            "success": False,
            "area_code": area_code,
            "area_name": area_name,
            "customer": None,
            "data": [],
            "text": (
                "❌ <b>KHÔNG THỂ KIỂM TRA</b>\n\n"
                f"📍 Khu vực: "
                f"<b>{area_name}</b>\n\n"
                f"⚠️ Lỗi: "
                f"<code>{e}</code>"
            ),
        }


# ============================================================
# CHECK CUSTOMER
# ============================================================

async def check_customer(
    customer,
    area_code=None,
    area_name=None,
):
    """
    Kiểm tra lịch cúp điện theo mã khách hàng.

    Dùng chung cho:
    - Kiểm tra ngay
    - Theo dõi định kỳ
    """

    customer = (
        str(customer)
        .strip()
        .upper()
    )

    try:

        tu_ngay, den_ngay = date_range()

        result = await get_by_customer(
            ma_kh=customer,
            tu_ngay=tu_ngay,
            den_ngay=den_ngay,
        )

        schedules = []

        if isinstance(result, dict):

            schedules = result.get(
                "schedules",
                []
            )

            if not area_name:

                area_name = result.get(
                    "name"
                )

        elif isinstance(result, list):

            schedules = result

        return {
            "success": True,
            "area_code": area_code,
            "area_name": area_name,
            "customer": customer,
            "data": schedules,
            "text": format_power_result(
                schedules,
                area_name=area_name,
                customer=customer,
            ),
        }

    except Exception as e:

        print(
            "[POWER CHECK CUSTOMER ERROR] "
            f"{customer}: "
            f"{type(e).__name__}: {e}"
        )

        return {
            "success": False,
            "area_code": area_code,
            "area_name": area_name,
            "customer": customer,
            "data": [],
            "text": (
                "❌ <b>KHÔNG THỂ KIỂM TRA</b>\n\n"
                f"🆔 Mã KH: "
                f"<code>{customer}</code>\n\n"
                f"⚠️ Lỗi: "
                f"<code>{e}</code>"
            ),
        }


# ============================================================
# CHECK SUBSCRIPTION
# ============================================================

async def check_subscription(
    subscription
):
    """
    Kiểm tra subscription.

    Hỗ trợ:
    - area
    - customer
    """

    if not isinstance(
        subscription,
        dict
    ):
        raise ValueError(
            "Subscription không hợp lệ"
        )

    sub_type = (
        subscription.get("type")
        or subscription.get("sub_type")
        or ""
    )

    value = (
        subscription.get("value")
        or ""
    )

    area_code = (
        subscription.get("area_code")
        or None
    )

    area_name = (
        subscription.get("area_name")
        or None
    )

    if not value:
        raise ValueError(
            "Subscription thiếu value"
        )

    if sub_type == "area":

        return await check_area(
            area_code=value,
            area_name=area_name,
        )

    if sub_type == "customer":

        return await check_customer(
            customer=value,
            area_code=area_code,
            area_name=area_name,
        )

    raise ValueError(
        f"Subscription không hợp lệ: "
        f"{sub_type}"
    )