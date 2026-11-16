"""Telegram bridge + Auto-Scheduler cho Mánh Mối Marketing Agent.

- Model cố định: nvidia/nemotron-3.5-super-vl:free (OpenRouter API)
- Tự động đăng lên Threads (@n_khac.minh)
- Hẹn giờ tự động đăng mỗi ngày + hẹn giờ 1 lần theo lệnh trên Telegram
- Tự động cân bằng phễu 40% TOFU / 40% MOFU / 20% BOFU dựa trên content-log.csv
"""
import asyncio
import csv
import datetime
import json
import os
import re
import sys
from pathlib import Path

import requests

import threads

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEDULE_FILE = HERE / "schedule.json"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


load_env(HERE / ".env")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
ALLOWED = {int(x) for x in re.findall(r"-?\d+", os.environ.get("ALLOWED_CHAT_IDS", ""))}
MODEL = "nvidia/nemotron-3.5-super-vl:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

if not TOKEN:
    sys.exit("Thiếu TELEGRAM_BOT_TOKEN trong telegram_bot/.env")
if not OPENROUTER_API_KEY:
    sys.exit("Thiếu OPENROUTER_API_KEY trong telegram_bot/.env")

TG = f"https://api.telegram.org/bot{TOKEN}"


def load_schedule() -> dict:
    default = {
        "enabled": True,
        "wait_approval": False,
        "daily_times": ["12:00", "20:00"],
        "last_daily_runs": {},
        "one_off": [],
    }
    if not SCHEDULE_FILE.exists():
        save_schedule(default)
        return default
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return default


def save_schedule(data: dict) -> None:
    SCHEDULE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_next_funnel_tier() -> tuple[str, dict[str, int]]:
    """Tính tỷ lệ TOFU/MOFU/BOFU từ content-log.csv và chọn tầng đang thiếu nhất so với chuẩn 40/40/20."""
    counts = {"TOFU": 0, "MOFU": 0, "BOFU": 0}
    log_path = ROOT / "content-log.csv"
    if log_path.exists():
        with log_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                tier = (row.get("Funnel_Tier") or "").strip().upper()
                if tier in counts:
                    counts[tier] += 1
    total = sum(counts.values())
    if total == 0:
        return "TOFU", counts
    targets = {"TOFU": 0.40, "MOFU": 0.40, "BOFU": 0.20}
    deficits = {t: targets[t] - (counts[t] / total) for t in targets}
    best_tier = max(deficits, key=deficits.get)
    return best_tier, counts


def select_skills_for_prompt(prompt: str) -> list[str]:
    p = prompt.lower()
    if "/tofu" in p:
        return ["marketing-psychology", "copywriting", "copy-editing"]
    if "/mofu" in p:
        return ["copywriting", "copy-editing", "image"]
    if "/bofu" in p:
        return ["offers", "product-marketing", "marketing-psychology", "copy-editing"]
    return ["copywriting", "copy-editing", "marketing-psychology", "offers", "product-marketing", "image"]


def load_skills_text(skill_names: list[str]) -> str:
    blocks = []
    skills_dir = ROOT / ".agents" / "skills"
    for name in skill_names:
        sp = skills_dir / name / "SKILL.md"
        if sp.exists():
            content = sp.read_text(encoding="utf-8")
            blocks.append(f"## SKILL: {name}\n{content[:6000]}")
    return "\n\n".join(blocks)


def read_recent_log() -> str:
    log_path = ROOT / "content-log.csv"
    if not log_path.exists():
        return "Date,Funnel_Tier,Channel,Angle_Used,Opening_Hook"
    lines = log_path.read_text(encoding="utf-8-sig").strip().splitlines()
    header = lines[0] if lines else "Date,Funnel_Tier,Channel,Angle_Used,Opening_Hook"
    tail = lines[-10:] if len(lines) > 1 else []
    return "\n".join([header] + tail)


def append_csv_rows(reply: str) -> tuple[str, int]:
    matches = re.findall(r"```csv_log\s*\n(.*?)```", reply, flags=re.S)
    count = 0
    if matches:
        log_path = ROOT / "content-log.csv"
        existing = log_path.read_text(encoding="utf-8-sig") if log_path.exists() else "Date,Funnel_Tier,Channel,Angle_Used,Opening_Hook\n"
        if not existing.endswith("\n"):
            existing += "\n"
        new_lines = []
        for block in matches:
            for line in block.strip().splitlines():
                line = line.strip()
                if not line or line.lower().startswith("date,"):
                    continue
                new_lines.append(line)
                count += 1
        if new_lines:
            log_path.write_text(existing + "\n".join(new_lines) + "\n", encoding="utf-8")
    cleaned = re.sub(r"```csv_log\s*\n.*?```\s*", "", reply, flags=re.S).strip()
    return cleaned, count


def build_instructions(prompt: str) -> str:
    rule = (ROOT / ".agents" / "rules" / "manh-moi-marketing-agent.md").read_text(encoding="utf-8")
    rule = re.sub(r"^---.*?---\s*", "", rule, count=1, flags=re.S)
    ctx_path = ROOT / ".agents" / "product-marketing.md"
    ctx = ctx_path.read_text(encoding="utf-8") if ctx_path.exists() else ""
    skills_txt = load_skills_text(select_skills_for_prompt(prompt))
    recent_log = read_recent_log()
    today = datetime.date.today().isoformat()
    return (
        rule
        + "\n\n---\n# PRODUCT CONTEXT\n" + ctx
        + "\n\n---\n# LOADED SKILLS (.agents/skills/)\n" + skills_txt
        + "\n\n---\n# RECENT CONTENT LOG (content-log.csv - last 10 rows)\n" + recent_log
        + "\n\n---\n# TELEGRAM & LOGGING MODE\n"
        f"Hôm nay là {today}. Người dùng ra lệnh qua Telegram.\n"
        "- Trả lời ngắn gọn, không dùng bảng markdown, không dùng LaTeX.\n"
        "- Tuyệt đối KHÔNG nhắc tới quét mã QR (người chơi mở túi mù đọc trực tiếp thẻ bài lời khai và hồ sơ được cung cấp).\n"
        "- PHONG CÁCH VIẾT NHƯ NGƯỜI THẬT 100%, TUYỆT ĐỐI KHÔNG SEEDING LỘ LIỄU (BẮT BUỘC):\n"
        "  + Viết như một người trẻ đang kể lại trải nghiệm thật / tâm sự sau buổi đi cà phê với bạn bè, KHÔNG viết theo công thức quảng cáo (Nỗi đau -> Giới thiệu sản phẩm -> Liệt kê tính năng 'luật 5 phút, chơi 2-3 người' -> Kêu gọi).\n"
        "  + Với bài TOFU/MOFU trên Threads: KHÔNG cần nhồi nhét tên thương hiệu hay thông số sản phẩm; hãy kể câu chuyện tự nhiên (ví dụ: ngồi xé túi mù đọc thẻ lời khai cãi nhau với đám bạn...) để người đọc tự tò mò hỏi game gì dưới comment.\n"
        "  + HẠN CHẾ XUỐNG DÒNG / TÁCH DÒNG NHIỀU: Viết liền mạch thành 1 đến tối đa 2 đoạn văn ngắn gọn cho cả bài. Tuyệt đối KHÔNG ngắt mỗi câu một dòng rời rạc. Dùng dấu '...' và 1-2 từ kéo dài chữ cái cuối vừa đủ (haizzz...., ờmmmm..., cayyyy, nản nganggg...).\n"
        "  + GIỚI HẠN ICON: Cả bài CHỈ DÙNG TỐI ĐA 1 EMOJI DUY NHẤT (hoặc chỉ dùng '=))))', ':)))'). Tuyệt đối không rải nhiều icon.\n"
        "- Mỗi bài Threads BẮT BUỘC ngắn gọn dưới 400 ký tự để vừa khít 1 post Threads. Đặt nguyên văn nội dung đăng trong một khối ```threads ... ```; không đưa báo cáo hay ghi chú vào trong khối đó.\n"
        "- Để hệ thống tự động ghi vào content-log.csv, ở cuối câu trả lời (ngay sau khối BÁO CÁO HOÀN THÀNH), hãy xuất thêm một khối ```csv_log ... ``` chứa các dòng mới theo đúng định dạng:\n"
        f'{today},TOFU/MOFU/BOFU,Threads/Facebook,"Angle_Used","Opening_Hook"\n'
        "- Luôn kết thúc phần hiển thị bằng khối báo cáo ở Mục 5."
    )


DRAFTS: dict[int, str] = {}
_counter = [0]
HISTORY: dict[int, list[dict[str, str]]] = {}


def next_draft() -> int:
    _counter[0] += 1
    return _counter[0]


def shrink_for_threads(text: str) -> str:
    text = text.strip()
    if len(text) <= 500:
        return text
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    prompt = (
        f"Rút gọn bài đăng Threads tiếng Việt dưới đây từ {len(text)} ký tự xuống dưới 400 ký tự. "
        "GIỮ NGUYÊN giọng kể chuyện đời thường (không quảng cáo/seeding lộ liễu), viết liền mạch 1-2 đoạn (hạn chế xuống dòng nhiều), dấu '...', từ kéo dài chữ cái cuối vừa phải (haizzz...., ờmmmm...), và TỐI ĐA 1 emoji duy nhất trong cả bài. "
        "Không thêm bất kỳ lời giải thích nào, chỉ trả về đúng nội dung bài đăng:\n\n"
        + text
    )
    try:
        r = requests.post(
            OPENROUTER_URL,
            headers=headers,
            json={"model": MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.4},
            timeout=60,
        )
        data = r.json()
        short = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        short = re.sub(r"^```\w*\s*|\s*```$", "", short).strip()
        if 50 < len(short) <= 500:
            return short
    except Exception:
        pass
    return text


def tg(method: str, **params):
    r = requests.post(f"{TG}/{method}", json=params, timeout=70)
    return r.json()


def send(chat_id: int, text: str) -> None:
    text = text.strip() or "(trống)"
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id, text=text[i : i + 4000])


HELP_TEXT = """📖 DANH SÁCH LỆNH MÁNH MỐI MARKETING BOT

1️⃣ LỆNH TẠO BÀI & TỰ ĐĂNG THREADS:
• /tofu [số lượng / chủ đề]
  Ví dụ: /tofu 1 hoặc /tofu 1 tụ tập bạn bè không bấm điện thoại
• /mofu [số lượng / manh mối]
  Ví dụ: /mofu 1 Threads hoặc /mofu 1 mâu thuẫn lời khai Lê và Mai
• /bofu [số lượng / góc bán]
  Ví dụ: /bofu 1 Threads hoặc /bofu 1 Facebook
• /auto
  Tự động tính tỷ lệ 40% TOFU / 40% MOFU / 20% BOFU từ nhật ký, chọn tầng đang thiếu nhất và viết 1 bài đăng lên Threads ngay.
• /wait (hoặc gắn /wait vào cuối lệnh như: /tofu 1 /wait)
  Chưa đăng vội, lưu thành bản nháp gửi về Telegram kèm ảnh dự kiến để đợi bạn duyệt rồi mới gõ /dang [số] để đăng. (Gõ /wait một mình để Bật/Tắt chế độ luôn đợi duyệt).

2️⃣ LỆNH HẸN GIỜ TỰ ĐỘNG ĐĂNG (SCHEDULER):
• /lich
  Xem trạng thái hẹn giờ, các khung giờ tự đăng hàng ngày và danh sách lịch hẹn 1 lần.
• /lich_ngay 12:00 20:00
  Đặt khung giờ cố định mỗi ngày để bot tự động viết bài (cân bằng phễu 40/40/20) và đăng lên Threads.
• /hen HH:MM [lệnh]
  Hẹn giờ đăng 1 lần vào giờ chỉ định.
  Ví dụ: /hen 21:30 /tofu 1
  Hoặc: /hen 09:00 /mofu 1 Threads
• /hen_bat hoặc /hen_tat
  Bật / tạm tắt chế độ tự động đăng theo lịch hàng ngày.
• /hen_xoa [số thứ tự]
  Xóa 1 lịch hẹn một lần (hoặc /hen_xoa all để xóa hết lịch 1 lần).

3️⃣ LỆNH QUẢN LÝ & KIỂM TRA:
• /anh — Xem danh sách ảnh trong assets/ẢNH MEME THREADS (đang tự động đính kèm khi đăng Threads).
• /dang [số bản nháp] — Đăng lại bản nháp nếu trước đó chưa đăng.
• /log — Xem thống kê số bài TOFU / MOFU / BOFU và 5 bài gần nhất trong content-log.csv.
• /init — Kiểm tra trạng thái hệ thống, model & skills.
• /help — Xem bảng hướng dẫn các lệnh này."""


def format_schedule_status() -> str:
    sched = load_schedule()
    tier, counts = pick_next_funnel_tier()
    total = sum(counts.values()) or 1
    status = "🟢 ĐANG BẬT" if sched.get("enabled", True) else "🔴 ĐANG TẮT"
    daily = ", ".join(sched.get("daily_times", [])) or "(chưa đặt)"
    one_offs = sched.get("one_off", [])
    one_off_lines = []
    for idx, item in enumerate(one_offs, 1):
        one_off_lines.append(f"  {idx}. [{item['run_at']}] -> {item['cmd']}")
    one_off_str = "\n".join(one_off_lines) if one_off_lines else "  (Không có lịch hẹn 1 lần nào)"
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"⏰ TRẠNG THÁI HẸN GIỜ ({status})\n"
        f"- Giờ hệ thống hiện tại: {now_str}\n"
        f"- Khung giờ tự đăng mỗi ngày: {daily}\n"
        f"- Tỷ lệ phễu hiện tại: TOFU {counts['TOFU']} ({counts['TOFU']*100//total}%) | "
        f"MOFU {counts['MOFU']} ({counts['MOFU']*100//total}%) | "
        f"BOFU {counts['BOFU']} ({counts['BOFU']*100//total}%)\n"
        f"- Tầng tự động chọn tiếp theo: {tier}\n\n"
        f"📌 Lịch hẹn 1 lần sắp chạy:\n{one_off_str}\n\n"
        "💡 Gõ /lich_ngay 12:00 20:00 để đổi giờ hàng ngày, hoặc /hen 21:00 /tofu 1 để hẹn 1 lần."
    )


def format_log_summary() -> str:
    tier, counts = pick_next_funnel_tier()
    total = sum(counts.values())
    log_path = ROOT / "content-log.csv"
    lines = log_path.read_text(encoding="utf-8-sig").strip().splitlines() if log_path.exists() else []
    tail = lines[-5:] if len(lines) > 1 else []
    recent_txt = "\n".join(f"• {r}" for r in tail) if tail else "(Chưa có bài nào)"
    return (
        f"📊 THỐNG KÊ CONTENT-LOG.CSV (Tổng: {total} bài)\n"
        f"- TOFU: {counts['TOFU']} bài (Mục tiêu 40%)\n"
        f"- MOFU: {counts['MOFU']} bài (Mục tiêu 40%)\n"
        f"- BOFU: {counts['BOFU']} bài (Mục tiêu 20%)\n"
        f"👉 Gợi ý bài tiếp theo để cân bằng phễu: {tier}\n\n"
        f"📝 5 bài gần nhất:\n{recent_txt}"
    )


def call_openrouter_sync(chat_id: int, prompt: str) -> str:
    p_low = prompt.strip().lower()
    if p_low in ("/init", "khởi tạo hệ thống"):
        skills_dir = ROOT / ".agents" / "skills"
        installed = [d.name for d in skills_dir.iterdir() if d.is_dir()] if skills_dir.exists() else []
        memes = threads.list_meme_images()
        return (
            "✅ HỆ THỐNG MÁNH MỐI AGENT SẴN SÀNG\n"
            f"- Model cố định: {MODEL} (OpenRouter)\n"
            f"- Skills đã nạp ({len(installed)}/6): {', '.join(installed)}\n"
            "- Tài khoản Threads: @n_khac.minh (Tự động đăng: BẬT)\n"
            f"- Kho ảnh Meme Threads (assets/ẢNH MEME THREADS): {len(memes)} ảnh (Tự động đính kèm)\n"
            "- Vụ án 01: TUYỆT TÁC TÀN TRO (Đã khóa bảo vệ Spoiler & bỏ hoàn toàn QR)\n\n"
            "Gõ /help để xem toàn bộ danh sách lệnh & hẹn giờ."
        )

    sys_prompt = build_instructions(prompt)
    hist = HISTORY.get(chat_id, [])[-6:]
    messages = [{"role": "system", "content": sys_prompt}] + hist + [{"role": "user", "content": prompt}]

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://manhmoi.vn",
        "X-Title": "Manh Moi Marketing Agent",
    }
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.7,
    }
    r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=120)
    data = r.json()
    if r.status_code != 200 or "choices" not in data:
        err = data.get("error", {}).get("message") if isinstance(data, dict) else str(data)
        raise RuntimeError(f"OpenRouter ({MODEL}) trả lỗi [{r.status_code}]: {err}")

    msg = data["choices"][0].get("message") or {}
    reply = (msg.get("content") or "").strip()
    if not reply and msg.get("reasoning"):
        reply = msg["reasoning"].strip()

    cleaned_reply, _ = append_csv_rows(reply)
    hist.append({"role": "user", "content": prompt})
    hist.append({"role": "assistant", "content": cleaned_reply})
    HISTORY[chat_id] = hist[-6:]
    return cleaned_reply


async def execute_and_publish(
    chat_id: int,
    prompt: str,
    lock: asyncio.Lock,
    prefix_note: str = "",
    force_wait: bool = False,
) -> None:
    """Chạy lệnh tạo nội dung, gửi về Telegram và tự động đăng (hoặc đợi duyệt nếu có /wait)."""
    has_wait_flag = bool(re.search(r"(?:^|\s)/wait(?:\s|$)", prompt, flags=re.I))
    clean_prompt = re.sub(r"(?:^|\s)/wait(?:\s|$)", " ", prompt, flags=re.I).strip()
    sched = load_schedule()
    wait_mode = force_wait or has_wait_flag or bool(sched.get("wait_approval", False))

    tg("sendChatAction", chat_id=chat_id, action="typing")
    if prefix_note:
        send(chat_id, prefix_note)
    async with lock:
        try:
            reply = await asyncio.to_thread(call_openrouter_sync, chat_id, clean_prompt)
        except Exception as e:
            reply = f"Lỗi agent ({MODEL}): {e}"
    found = re.findall(r"```threads\s*\n(.*?)```", reply, flags=re.S)
    auto_pub = (not wait_mode) and (os.environ.get("AUTO_PUBLISH_THREADS", "").lower() in ("1", "true", "yes"))
    new_ids = []
    for post in found:
        did = next_draft()
        DRAFTS[did] = await asyncio.to_thread(shrink_for_threads, post.strip())
        new_ids.append(did)
    send(chat_id, reply)
    if found:
        if auto_pub and threads.configured():
            for did in new_ids:
                try:
                    pid, img_name = await asyncio.to_thread(threads.publish_text, DRAFTS[did])
                    del DRAFTS[did]
                    img_note = f" kèm ảnh `{img_name}`" if img_name else ""
                    send(chat_id, f"✅ Đã tự động đăng bài #{did}{img_note} lên Threads (id {pid}).")
                except Exception as e:
                    send(chat_id, f"⚠️ Tự đăng bài #{did} lỗi: {e}. Gõ /dang {did} để thử lại.")
        else:
            ids_str = ", ".join(map(str, new_ids))
            send(
                chat_id,
                f"⏸️ CHỜ DUYỆT (/wait): Đã lưu bản nháp #{ids_str} (chưa đăng lên Threads).\n"
                f"👉 Gõ `/dang {new_ids[0]}` để duyệt đăng ngay, hoặc gửi yêu cầu sửa lại!",
            )


async def scheduler_loop(lock: asyncio.Lock) -> None:
    """Vòng lặp kiểm tra lịch hẹn mỗi 20 giây."""
    while True:
        try:
            await asyncio.sleep(20)
            if not ALLOWED:
                continue
            target_chat = next(iter(ALLOWED))
            sched = load_schedule()
            now = datetime.datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            hm_str = now.strftime("%H:%M")
            now_full = now.strftime("%Y-%m-%d %H:%M")

            # 1. Kiểm tra lịch hẹn 1 lần (one_off)
            remaining = []
            due_items = []
            for item in sched.get("one_off", []):
                if item.get("run_at", "") <= now_full:
                    due_items.append(item)
                else:
                    remaining.append(item)
            if due_items:
                sched["one_off"] = remaining
                save_schedule(sched)
                for item in due_items:
                    cmd = item.get("cmd", "/auto")
                    chat_id = item.get("chat_id", target_chat)
                    if cmd.strip().lower() == "/auto":
                        tier, _ = pick_next_funnel_tier()
                        cmd = f"/{tier.lower()} 1 Threads"
                    await execute_and_publish(
                        chat_id,
                        cmd,
                        lock,
                        prefix_note=f"⏰ ĐẾN GIỜ HẸN ({item.get('run_at')}): Đang thực thi `{cmd}`...",
                    )

            # 2. Kiểm tra lịch cố định hàng ngày (daily_times)
            if sched.get("enabled", True):
                last_runs = sched.get("last_daily_runs", {})
                if hm_str in sched.get("daily_times", []) and last_runs.get(hm_str) != today_str:
                    last_runs[hm_str] = today_str
                    sched["last_daily_runs"] = last_runs
                    save_schedule(sched)
                    tier, _ = pick_next_funnel_tier()
                    auto_cmd = f"/{tier.lower()} 1 Threads"
                    await execute_and_publish(
                        target_chat,
                        auto_cmd,
                        lock,
                        prefix_note=f"⏰ LỊCH TỰ ĐỘNG HÀNG NGÀY ({hm_str}): Tự động chọn tầng `{tier}` (chuẩn 40/40/20) và đăng Threads...",
                    )
        except Exception as e:
            print("scheduler error:", e)


async def handle_command(chat_id: int, text: str, lock: asyncio.Lock) -> None:
    t_low = text.lower().strip()

    if t_low in ("/help", "/huongdan", "help"):
        send(chat_id, HELP_TEXT)
        return

    if t_low == "/log":
        send(chat_id, format_log_summary())
        return

    if t_low == "/lich":
        send(chat_id, format_schedule_status())
        return

    if t_low == "/hen_bat":
        sched = load_schedule()
        sched["enabled"] = True
        save_schedule(sched)
        send(chat_id, "🟢 Đã BẬT lịch tự động đăng bài hàng ngày.\n\n" + format_schedule_status())
        return

    if t_low == "/hen_tat":
        sched = load_schedule()
        sched["enabled"] = False
        save_schedule(sched)
        send(chat_id, "🔴 Đã TẠM TẮT lịch tự động đăng bài hàng ngày.\n\n" + format_schedule_status())
        return

    if t_low.startswith("/lich_ngay"):
        times = re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
        if not times:
            send(chat_id, "Dùng: `/lich_ngay 12:00 20:00` (nhập 1 hoặc nhiều khung giờ HH:MM cách nhau bằng dấu cách).")
            return
        formatted = sorted({f"{int(h):02d}:{m}" for h, m in times})
        sched = load_schedule()
        sched["daily_times"] = formatted
        sched["enabled"] = True
        save_schedule(sched)
        send(chat_id, f"✅ Đã cập nhật khung giờ tự đăng mỗi ngày: {', '.join(formatted)}\n\n" + format_schedule_status())
        return

    if t_low.startswith("/hen_xoa"):
        arg = text[len("/hen_xoa"):].strip().lower()
        sched = load_schedule()
        one_offs = sched.get("one_off", [])
        if arg == "all":
            sched["one_off"] = []
            save_schedule(sched)
            send(chat_id, "🗑️ Đã xóa toàn bộ lịch hẹn 1 lần.")
            return
        if arg.isdigit() and 1 <= int(arg) <= len(one_offs):
            removed = one_offs.pop(int(arg) - 1)
            sched["one_off"] = one_offs
            save_schedule(sched)
            send(chat_id, f"🗑️ Đã xóa lịch hẹn: [{removed['run_at']}] -> {removed['cmd']}")
            return
        send(chat_id, "Dùng: `/hen_xoa <số thứ tự>` hoặc `/hen_xoa all`. Gõ `/lich` để xem số thứ tự.")
        return

    if t_low.startswith("/hen"):
        # Hỗ trợ: /hen 21:30 /tofu 1  hoặc  /hen 2026-11-17 09:00 /mofu 1
        m = re.match(r"^/hen\s+(?:(\d{4}-\d{2}-\d{2})\s+)?([01]?\d|2[0-3]):([0-5]\d)(?:\s+(.+))?$", text.strip())
        if not m:
            send(chat_id, "Dùng: `/hen HH:MM [lệnh]`\nVí dụ:\n• `/hen 21:30 /tofu 1`\n• `/hen 14:00 /mofu 1 Threads`\n• `/hen 20:00 /auto`")
            return
        date_part, hh, mm, cmd_part = m.groups()
        now = datetime.datetime.now()
        target_time = datetime.time(int(hh), int(mm))
        if date_part:
            target_date = datetime.date.fromisoformat(date_part)
        else:
            target_date = now.date()
            if datetime.datetime.combine(target_date, target_time) <= now:
                target_date += datetime.timedelta(days=1)
        run_at = f"{target_date.isoformat()} {int(hh):02d}:{mm}"
        cmd = (cmd_part or "/auto").strip()
        sched = load_schedule()
        sched.setdefault("one_off", []).append({"run_at": run_at, "cmd": cmd, "chat_id": chat_id})
        sched["one_off"].sort(key=lambda x: x["run_at"])
        save_schedule(sched)
        send(chat_id, f"✅ Đã đặt lịch hẹn lúc **{run_at}** sẽ tự chạy lệnh: `{cmd}`")
        return

    if t_low in ("/wait", "/wait on", "/wait off"):
        sched = load_schedule()
        if t_low == "/wait on":
            sched["wait_approval"] = True
        elif t_low == "/wait off":
            sched["wait_approval"] = False
        else:
            sched["wait_approval"] = not bool(sched.get("wait_approval", False))
        save_schedule(sched)
        if sched["wait_approval"]:
            send(
                chat_id,
                "⏸️ Đã BẬT chế độ CHỜ DUYỆT (`/wait`): Mọi bài viết tạo ra sẽ lưu nháp và đợi bạn gõ `/dang <số>` mới đăng lên Threads.\n"
                "(Gõ lại `/wait` hoặc `/wait off` khi muốn bật lại tự động đăng thẳng).",
            )
        else:
            send(
                chat_id,
                "▶️ Đã TẮT chế độ chờ duyệt: Bot sẽ tự động đăng thẳng lên Threads.\n"
                "💡 Mẹo: Bạn vẫn có thể thêm `/wait` vào từng lệnh lẻ (ví dụ: `/tofu 1 /wait`) để đợi duyệt riêng bài đó.",
            )
        return

    if t_low in ("/auto", "/auto /wait", "/wait /auto"):
        tier, counts = pick_next_funnel_tier()
        auto_cmd = f"/{tier.lower()} 1 Threads"
        await execute_and_publish(
            chat_id,
            auto_cmd,
            lock,
            prefix_note=f"🎯 Tự động cân bằng phễu (TOFU:{counts['TOFU']} | MOFU:{counts['MOFU']} | BOFU:{counts['BOFU']}) -> Chọn tầng **{tier}**...",
            force_wait=("/wait" in t_low),
        )
        return

    if t_low == "/anh":
        imgs = threads.list_meme_images()
        if not imgs:
            send(chat_id, "Chưa có ảnh nào trong `assets/ẢNH MEME THREADS`.")
            return
        lines = [f"🖼️ KHO ẢNH MEME THREADS ({len(imgs)} ảnh trong `assets/ẢNH MEME THREADS`):"]
        for i, p in enumerate(imgs, 1):
            lines.append(f"  {i}. {p.name}")
        lines.append("\n💡 Bot tự động chọn ảnh phù hợp cảm xúc (không lặp lại ảnh vừa đăng) để đính kèm vào mọi bài Threads.")
        send(chat_id, "\n".join(lines))
        return

    if t_low.startswith("/dang"):
        m = re.match(r"/dang\s+(\d+)", text)
        if not m or int(m.group(1)) not in DRAFTS:
            send(chat_id, "Dùng: /dang <số bản nháp>. Bản nháp hiện có: " + (", ".join(map(str, DRAFTS)) or "chưa có"))
            return
        if not threads.configured():
            send(chat_id, "Chưa cấu hình THREADS_USER_ID / THREADS_ACCESS_TOKEN trong .env.")
            return
        n = int(m.group(1))
        try:
            pid, img_name = await asyncio.to_thread(threads.publish_text, DRAFTS[n])
            del DRAFTS[n]
            img_note = f" kèm ảnh `{img_name}`" if img_name else ""
            send(chat_id, f"✅ Đã đăng lên Threads{img_note} (id {pid}).")
        except Exception as e:
            send(chat_id, f"Đăng lỗi: {e}")
        return

    await execute_and_publish(chat_id, text, lock)


async def main() -> None:
    load_schedule()
    offset = 0
    lock = asyncio.Lock()
    asyncio.create_task(scheduler_loop(lock))
    print(f"Bot + Scheduler đang chạy với model cố định: {MODEL}. Allowed: {ALLOWED or 'CHƯA CẤU HÌNH'}")
    while True:
        try:
            data = await asyncio.to_thread(
                lambda: tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            )
        except Exception as e:
            print("poll error:", e)
            await asyncio.sleep(5)
            continue
        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = (msg.get("chat") or {}).get("id")
            if not text or chat_id is None:
                continue
            text = re.sub(r"^(/\w+)@\w+", r"\1", text)
            if text.startswith("/id") or text.startswith("/start"):
                send(chat_id, f"Chat ID của bạn: {chat_id}\nModel đang chạy: {MODEL}\n\n" + HELP_TEXT)
                continue
            if chat_id not in ALLOWED:
                send(chat_id, "Không có quyền.")
                continue
            await handle_command(chat_id, text, lock)


if __name__ == "__main__":
    asyncio.run(main())
