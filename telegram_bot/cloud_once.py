"""Chế độ Cloud one-shot: xử lý ĐÚNG 1 lệnh (hoặc 1 lần kiểm tra lịch) rồi thoát.
Được workflow `manh-moi-event.yml` gọi khi Cloudflare Worker nhận tin nhắn Telegram (webhook) hoặc đến giờ hẹn.
Biến môi trường: EVENT_KIND = cmd | tick ; EVENT_CHAT_ID ; EVENT_TEXT
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot  # noqa: E402


async def run() -> None:
    kind = os.environ.get("EVENT_KIND", "cmd")
    lock = asyncio.Lock()
    if kind == "tick":
        await bot.run_scheduler_once(lock)
        return
    chat_id = int(os.environ["EVENT_CHAT_ID"])
    text = (os.environ.get("EVENT_TEXT") or "").strip()
    if not text:
        return
    if chat_id not in bot.ALLOWED:
        bot.send(chat_id, "Không có quyền.")
        return
    await bot.handle_command(chat_id, text, lock)


if __name__ == "__main__":
    asyncio.run(run())