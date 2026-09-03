# ============================================================
# main.py
# ============================================================

import asyncio

from rich.console import Console
from rich.panel import Panel

from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    NOTIFY_BOT_TOKEN,
)

from login import login_telegram
from core.client import create_clients
from core.commands import set_commands
from commands import register_all

from core.power.database import init_database
from core.power.monitor import start_power_monitor


console = Console()


# ============================================================
# MAIN
# ============================================================

async def main():

    # ========================================================
    # CONFIG CHECK
    # ========================================================

    if not API_ID:

        console.print(
            "[red]❌ Chưa cấu hình TG_API_ID[/red]"
        )

        return

    if not API_HASH:

        console.print(
            "[red]❌ Chưa cấu hình TG_API_HASH[/red]"
        )

        return

    if not BOT_TOKEN:

        console.print(
            "[red]❌ Chưa cấu hình TG_BOT_TOKEN[/red]"
        )

        return

    if not NOTIFY_BOT_TOKEN:

        console.print(
            "[red]❌ Chưa cấu hình TG_NOTIFY_BOT_TOKEN[/red]"
        )

        return

    # ========================================================
    # DATABASE
    # ========================================================

    console.print(
        Panel.fit(
            "STEP 0\n"
            "🗄 Initializing Database",
            title="DATABASE"
        )
    )

    init_database()

    console.print(
        "[green]✅ Database ready[/green]"
    )

    # ========================================================
    # LOGIN
    # ========================================================

    console.print(
        Panel.fit(
            "STEP 1\n"
            "🔐 Telegram User Login",
            title="BOOT"
        )
    )

    user_client = await login_telegram()

    # ========================================================
    # BOT
    # ========================================================

    console.print(
        Panel.fit(
            "STEP 2\n"
            "🤖 Starting Telegram Bot",
            title="BOT"
        )
    )

    bot, notify_bot = create_clients()

    await bot.start(
        bot_token=BOT_TOKEN
    )

    await notify_bot.start(
        bot_token=NOTIFY_BOT_TOKEN
    )

    # ========================================================
    # COMMANDS
    # ========================================================

    register_all(
        bot,
        notify_bot
    )

    await set_commands(
        bot
    )

    # ========================================================
    # POWER MONITOR
    # ========================================================

    power_monitor_task = start_power_monitor(
        bot
    )

    console.print(
        "[green]⚡ Power monitor: STARTED[/green]"
    )

    # ========================================================
    # BOT INFO
    # ========================================================

    bot_me = await bot.get_me()

    console.print(
        Panel.fit(
            f"[bold green]🚀 BOT ĐÃ START[/bold green]\n\n"
            f"🤖 Bot: @{bot_me.username}\n"
            f"🆔 ID: {bot_me.id}\n\n"
            "📦 Modules: loaded\n"
            "⌨ Commands: loaded\n"
            "⚡ Power monitor: ON\n"
            "🔐 User session: OK",
            title="SUCCESS",
            style="green"
        )
    )

    # ========================================================
    # RUN
    # ========================================================

    try:

        await bot.run_until_disconnected()

    finally:

        # ----------------------------------------------------
        # STOP MONITOR
        # ----------------------------------------------------

        if power_monitor_task:

            power_monitor_task.cancel()

            try:

                await power_monitor_task

            except asyncio.CancelledError:

                pass

        # ----------------------------------------------------
        # DISCONNECT
        # ----------------------------------------------------

        await bot.disconnect()

        await notify_bot.disconnect()

        await user_client.disconnect()


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        console.print(
            "\n[yellow]🛑 Bot đã được dừng.[/yellow]"
        )