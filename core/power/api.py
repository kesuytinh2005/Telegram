# ============================================================
# core/power/api.py
# EVNSPC POWER OUTAGE API
# ============================================================

import asyncio
import html
import re

from datetime import date, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from config import (
    EVNSPC_API_URL,
    EVNSPC_REFERER,
    USER_AGENT,
    API_TIMEOUT,
    LOOKAHEAD_DAYS,
)


# ============================================================
# HEADERS
# ============================================================

HEADERS = {
    "Host": "cskh.evnspc.vn",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": USER_AGENT,
    "Accept": "text/html, */*; q=0.01",
    "Referer": EVNSPC_REFERER,
    "Accept-Language": (
        "vi-VN,vi;q=0.9,"
        "en-US;q=0.8,en;q=0.7"
    ),
}


# ============================================================
# KHU VỰC
# MÃ CHỈ DÙNG NỘI BỘ
# ============================================================

AREA_CODES = {
    "PB0101": "Điện lực Đồng Nai",
    "PB0301": "Điện lực Lâm Đồng",
    "PB0501": "Điện lực Tây Ninh",
    "PB0701": "Điện lực Đồng Tháp",
    "PB1001": "Điện lực Vĩnh Long",
    "PB1101": "Điện lực Cần Thơ",
    "PB1201": "Điện lực An Giang",
    "PB1401": "Điện lực Cà Mau",
}


# ============================================================
# CLEAN
# ============================================================

def clean_text(value):

    if not value:
        return ""

    value = html.unescape(
        str(value)
    )

    value = value.replace(
        "\xa0",
        " "
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


# ============================================================
# PARSE TIME
# ============================================================

def parse_time(text):

    text = clean_text(text)

    pattern = re.compile(
        r"từ\s+"
        r"(\d{1,2}:\d{2}:\d{2})\s+ngày\s+"
        r"(\d{1,2}/\d{1,2}/\d{4})\s+đến\s+"
        r"(\d{1,2}:\d{2}:\d{2})\s+ngày\s+"
        r"(\d{1,2}/\d{1,2}/\d{4})",
        re.IGNORECASE,
    )

    match = pattern.search(text)

    if not match:
        return {
            "start_time": "",
            "start_date": "",
            "end_time": "",
            "end_date": "",
            "raw": text,
        }

    return {
        "start_time": match.group(1),
        "start_date": match.group(2),
        "end_time": match.group(3),
        "end_date": match.group(4),
        "raw": text,
    }


# ============================================================
# CUSTOMER INFO
# ============================================================

def parse_customer_info(content):

    soup = BeautifulSoup(
        content,
        "html.parser"
    )

    customer = ""
    address = ""

    notification = soup.select_one(
        ".notification"
    )

    if notification:

        for span in notification.select(
            ".ttl span"
        ):

            text = clean_text(
                span.get_text(
                    " ",
                    strip=True
                )
            )

            lower = text.lower()

            if lower.startswith(
                "khách hàng:"
            ):

                customer = text.split(
                    ":",
                    1
                )[1].strip()

            elif lower.startswith(
                "địa chỉ:"
            ):

                address = text.split(
                    ":",
                    1
                )[1].strip()

    return {
        "customer": customer,
        "address": address,
    }


# ============================================================
# SCHEDULE PARSER
# ============================================================

def parse_schedule_entries(content):

    soup = BeautifulSoup(
        content,
        "html.parser"
    )

    results = []

    entries = soup.select(
        ".notification .entry"
    )

    if not entries:
        entries = soup.select(
            ".entry"
        )

    for entry in entries:

        code = ""

        code_el = entry.select_one(
            ".code"
        )

        if code_el:

            code_text = clean_text(
                code_el.get_text(
                    " ",
                    strip=True
                )
            )

            match = re.search(
                r"MÃ LỊCH\s*:\s*(.+)",
                code_text,
                re.IGNORECASE
            )

            if match:
                code = clean_text(
                    match.group(1)
                )

        # ----------------------------------------------------
        # TIME
        # ----------------------------------------------------

        time_text = ""

        time_el = entry.select_one(
            ".time"
        )

        if time_el:

            time_text = clean_text(
                time_el.get_text(
                    " ",
                    strip=True
                )
            )

        # ----------------------------------------------------
        # CAUSE
        # ----------------------------------------------------

        cause = ""

        cause_el = entry.select_one(
            ".cause"
        )

        if cause_el:

            cause_text = clean_text(
                cause_el.get_text(
                    " ",
                    strip=True
                )
            )

            match = re.search(
                r"LÝ DO NGỪNG CUNG CẤP ĐIỆN\s*:\s*(.*)",
                cause_text,
                re.IGNORECASE
            )

            if match:
                cause = clean_text(
                    match.group(1)
                )
            else:
                cause = cause_text

        # ----------------------------------------------------
        # AREA
        # ----------------------------------------------------

        where = ""

        area_el = (
            entry.select_one(".area")
            or entry.select_one(".location")
            or entry.select_one(".address")
        )

        if area_el:

            where = clean_text(
                area_el.get_text(
                    " ",
                    strip=True
                )
            )

        # ----------------------------------------------------
        # FALLBACK
        # ----------------------------------------------------

        if not where:

            text = clean_text(
                entry.get_text(
                    " ",
                    strip=True
                )
            )

            for unwanted in (
                code,
                time_text,
                cause,
            ):

                if unwanted:
                    text = text.replace(
                        unwanted,
                        ""
                    )

            text = re.sub(
                r"MÃ LỊCH\s*:",
                "",
                text,
                flags=re.I
            )

            text = re.sub(
                r"THỜI GIAN\s*:",
                "",
                text,
                flags=re.I
            )

            text = re.sub(
                r"LÝ DO NGỪNG CUNG CẤP ĐIỆN\s*:",
                "",
                text,
                flags=re.I
            )

            where = clean_text(
                text
            )

        parsed = parse_time(
            time_text
        )

        results.append({
            "code": code,
            "where": where,
            "start_time": parsed["start_time"],
            "start_date": parsed["start_date"],
            "end_time": parsed["end_time"],
            "end_date": parsed["end_date"],
            "time_raw": parsed["raw"],
            "cause": cause,
        })

    return results


# ============================================================
# HTTP REQUEST
# ============================================================

async def _request(params):

    last_error = None

    for attempt in range(3):

        try:

            async with httpx.AsyncClient(
                headers=HEADERS,
                timeout=API_TIMEOUT,
                follow_redirects=True,
                http2=False,
            ) as client:

                response = await client.get(
                    EVNSPC_API_URL,
                    params=params
                )

                response.raise_for_status()

                return response.text

        except Exception as exc:

            last_error = exc

            print(
                f"[POWER API] Attempt "
                f"{attempt + 1}/3 lỗi: {exc}"
            )

            if attempt < 2:

                await asyncio.sleep(
                    2 * (attempt + 1)
                )

    raise RuntimeError(
        f"EVNSPC API lỗi: {last_error}"
    )


# ============================================================
# BY AREA
# ============================================================

async def get_by_area(
    ma_don_vi: str,
    tu_ngay: str,
    den_ngay: str,
):

    ma_don_vi = (
        ma_don_vi
        .strip()
        .upper()
    )

    params = {
        "madvi": ma_don_vi,
        "tuNgay": tu_ngay,
        "denNgay": den_ngay,
        "ChucNang": "MaDonVi",
    }

    content = await _request(
        params
    )

    schedules = parse_schedule_entries(
        content
    )

    return {
        "type": "area",
        "code": ma_don_vi,
        "name": AREA_CODES.get(
            ma_don_vi,
            ma_don_vi
        ),
        "customer": "",
        "address": "",
        "schedules": schedules,
    }


# ============================================================
# BY CUSTOMER
# ============================================================

async def get_by_customer(
    ma_kh: str,
    tu_ngay: str,
    den_ngay: str,
):

    ma_kh = (
        ma_kh
        .strip()
        .upper()
    )

    params = {
        "tuNgay": tu_ngay,
        "denNgay": den_ngay,
        "maKH": ma_kh,
        "ChucNang": "MaKhachHang",
    }

    content = await _request(
        params
    )

    info = parse_customer_info(
        content
    )

    schedules = parse_schedule_entries(
        content
    )

    return {
        "type": "customer",
        "code": ma_kh,
        "name": info["customer"],
        "customer": info["customer"],
        "address": info["address"],
        "schedules": schedules,
    }


# ============================================================
# DATE RANGE
# ============================================================

def date_range(
    days: Optional[int] = None
):

    if days is None:
        days = LOOKAHEAD_DAYS

    today = date.today()

    end = today + timedelta(
        days=days
    )

    return (
        today.strftime("%d-%m-%Y"),
        end.strftime("%d-%m-%Y"),
    )


# ============================================================
# CHECK SUBSCRIPTION
# ============================================================

async def check_subscription(
    subscription
):

    tu_ngay, den_ngay = date_range()

    sub_type = subscription.get(
        "type"
    )

    value = subscription.get(
        "value"
    )

    if sub_type == "customer":

        return await get_by_customer(
            value,
            tu_ngay,
            den_ngay
        )

    if sub_type == "area":

        return await get_by_area(
            value,
            tu_ngay,
            den_ngay
        )

    raise ValueError(
        "Subscription không hợp lệ"
    )



# ============================================================
# POWER SCHEDULE - HÀM DÙNG CHUNG
# ============================================================

async def get_power_schedule(
    area_code=None,
    customer=None,
    days=None,
):
    """
    Hàm dùng chung cho:
    - Check ngay theo khu vực
    - Check ngay theo mã KH
    - Check ngay cả hai
    - Monitor theo khung giờ
    """

    tu_ngay, den_ngay = date_range(
        days
    )

    # --------------------------------------------------------
    # CHECK THEO MÃ KH
    # --------------------------------------------------------

    if customer:

        result = await get_by_customer(
            customer,
            tu_ngay,
            den_ngay
        )

        return result.get(
            "schedules",
            []
        )

    # --------------------------------------------------------
    # CHECK THEO KHU VỰC
    # --------------------------------------------------------

    if area_code:

        result = await get_by_area(
            area_code,
            tu_ngay,
            den_ngay
        )

        return result.get(
            "schedules",
            []
        )

    raise ValueError(
        "Thiếu area_code hoặc customer"
    )

