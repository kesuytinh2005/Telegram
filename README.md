# TelegramBot Modular

Bot Telegram Telethon, tách module để dễ thêm lệnh.

## 1. Cài Termux

```bash
pkg update -y
pkg install python ffmpeg -y
pip install -r requirements.txt
```

## 2. Cấu hình

Copy `.env.example` thành `.env`:

```bash
cp .env.example .env
nano .env
```

Điền API ID, API HASH, bot token và notify token mới.

## 3. Chạy

```bash
python main.py
```

Lần đầu chương trình sẽ yêu cầu:
- Số điện thoại Telegram
- OTP
- Mật khẩu 2FA nếu có

Session được lưu trong `BOT/mysession.session`.

Lần sau chạy lại sẽ tự nhận session và không hỏi OTP nếu session còn hợp lệ.

## 4. Thêm lệnh

Tạo file mới:

`commands/test.py`

```python
from telethon import events

def register(bot, notify_bot):
    @bot.on(events.NewMessage(pattern=r"^/test$"))
    async def test(event):
        await event.reply("OK")
```

Sau đó thêm:

```python
from commands import test
```

và `test` vào danh sách `modules` trong `commands/__init__.py`.

## Lưu ý bảo mật

Không commit hoặc chia sẻ:
- `.env`
- `BOT/*.session`
- bot token
- API hash

Nếu token đã bị lộ, hãy revoke/regenerate token trước khi dùng bot.
