# ============================================================
# login.py
# ============================================================

import os

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from config import API_ID, API_HASH, USER_SESSION

console = Console()


def convert_phone(number: str) -> str:
    number = number.strip()

    if number.startswith("0"):
        return "+84" + number[1:]

    if number.startswith("+84"):
        return number

    if number.startswith("84"):
        return "+" + number

    return number


async def login_telegram():
    """
    Đăng nhập Telegram User.

    Nếu session đã tồn tại và còn hợp lệ:
        -> tự động sử dụng
        -> không hỏi SĐT / OTP

    Nếu chưa có session:
        -> yêu cầu đăng nhập tương tác
    """

    console.print()

    console.print(
        Panel.fit(
            "[bold cyan]🔐 TELEGRAM LOGIN[/bold cyan]\n"
            "[white]Telegram User Session[/white]",
            title="BOT SYSTEM"
        )
    )

    # --------------------------------------------------------
    # SESSION PATH
    # --------------------------------------------------------

    session_path = USER_SESSION

    console.print(
        f"[dim]Session: {session_path}.session[/dim]"
    )

    client = TelegramClient(
        session_path,
        API_ID,
        API_HASH
    )

    await client.connect()

    # --------------------------------------------------------
    # SESSION ĐÃ ĐĂNG NHẬP
    # --------------------------------------------------------

    if await client.is_user_authorized():

        me = await client.get_me()

        console.print(
            Panel.fit(
                f"[bold green]✔ SESSION HỢP LỆ[/bold green]\n\n"
                f"👤 Tên: {me.first_name or 'Unknown'}\n"
                f"🆔 ID: {me.id}\n\n"
                f"💾 Session: {session_path}.session\n\n"
                "[green]Không cần đăng nhập lại.[/green]",
                title="🎉 TELEGRAM USER",
                style="green"
            )
        )

        return client

    # --------------------------------------------------------
    # CHƯA CÓ SESSION
    # --------------------------------------------------------

    console.print(
        Panel.fit(
            Text(
                "VUI LÒNG NHẬP SỐ ĐIỆN THOẠI TELEGRAM",
                style="bold yellow"
            ),
            subtitle="Ví dụ: +84987654321"
        )
    )

    phone = convert_phone(
        console.input(
            "[cyan]📱 Nhập số điện thoại: [/cyan]"
        )
    )

    console.print(
        "\n[yellow]📨 Đang gửi mã đăng nhập...[/yellow]"
    )

    await client.send_code_request(phone)

    console.print(
        Panel.fit(
            "[bold]Mã đăng nhập đã được gửi.[/bold]\n\n"
            "• Kiểm tra Telegram\n"
            "• Hoặc SMS nếu Telegram yêu cầu\n"
            "• Nhập mã không có dấu '-'",

            title="📨 OTP",
            style="blue"
        )
    )

    code = console.input(
        "[yellow]🔑 Nhập mã OTP: [/yellow]"
    ).strip()

    try:

        await client.sign_in(
            phone=phone,
            code=code
        )

    except SessionPasswordNeededError:

        console.print(
            Panel.fit(
                "[red]Tài khoản đang bật mật khẩu 2 lớp (2FA).[/red]",
                title="🔐 2FA"
            )
        )

        password = console.input(
            "[cyan]🔑 Nhập mật khẩu 2FA: [/cyan]"
        )

        await client.sign_in(
            password=password
        )

    # --------------------------------------------------------
    # LOGIN SUCCESS
    # --------------------------------------------------------

    me = await client.get_me()

    console.print(
        Panel.fit(
            f"[bold green]🎉 ĐĂNG NHẬP THÀNH CÔNG![/bold green]\n\n"
            f"👤 Tài khoản: {me.first_name or 'Unknown'}\n"
            f"🆔 ID: {me.id}\n\n"
            f"💾 Session đã lưu:\n"
            f"{session_path}.session\n\n"
            "[yellow]Lần sau không cần đăng nhập lại.[/yellow]",

            title="✔ HOÀN TẤT",
            style="green"
        )
    )

    return client