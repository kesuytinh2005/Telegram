from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

COMMANDS = [
    BotCommand(command="start", description="Bắt đầu bot"),
    BotCommand(command="stop", description="Dừng tiến trình"),
    BotCommand(command="download", description="Tải video"),
    BotCommand(command="getuidfb", description="Get Facebook UID"),
]

async def set_commands(bot):
    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=COMMANDS
    ))
    print("✅ Đã set command menu")
