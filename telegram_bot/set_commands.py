"""Đăng ký menu gợi ý lệnh của Telegram (hiện khi gõ /). Chạy: python telegram_bot/set_commands.py"""
import json
import os
import re
from pathlib import Path

import requests

env = Path(__file__).resolve().parent / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

COMMANDS = [
    ("auto", "Tự chọn tầng thiếu nhất (40/40/20) và đăng"),
    ("pov", "Bài POV 1 câu + 1/2/3 ảnh meme (thêm ý tưởng: /pov ...)"),
    ("video", "Bài POV 1 câu + 1 video meme"),
    ("tofu", "Bài đời thường nhận diện (vd: /tofu cà phê)"),
    ("mofu", "Bài tranh luận lời khai vụ Tuyệt Tác Tàn Tro"),
    ("bofu", "Bài mở đặt trước (150k và Combo 220k)"),
    ("wait", "Bật/tắt chế độ chờ duyệt trước khi đăng"),
    ("dang", "Duyệt đăng bản nháp (vd: /dang 5)"),
    ("doi", "Đổi ảnh/video khác cho bản nháp đang chờ"),
    ("anh", "Xem kho ảnh và video meme"),
    ("status", "Trạng thái hệ thống, tỷ lệ phễu, lịch"),
    ("lich", "Xem/đổi lịch đăng hàng ngày (vd: /lich 12:00 20:00)"),
    ("hen", "Hẹn đăng 1 lần (vd: /hen 21:30 /pov)"),
    ("help", "Bảng hướng dẫn lệnh"),
]
token = os.environ["TELEGRAM_BOT_TOKEN"]
payload = {"commands": [{"command": c, "description": d[:250]} for c, d in COMMANDS]}
r = requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json=payload, timeout=30)
print(r.json())