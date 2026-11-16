"""Script chạy 1 phiên trên GitHub Actions (Cloud miễn phí) để bạn tắt máy tính vẫn điều khiển được bằng điện thoại."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot  # noqa: E402


async def run_for_window(minutes: int = 28) -> None:
    task = asyncio.create_task(bot.main())
    try:
        await asyncio.sleep(minutes * 60)
    finally:
        task.cancel()


if __name__ == "__main__":
    mins = int(os.environ.get("CLOUD_RUN_MINUTES", "28"))
    try:
        asyncio.run(run_for_window(mins))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
