"""Telegram Bot + Auto-Scheduler cho Mánh Mối Marketing Agent (Phiên bản Tinh Gọn & Chống Xung Đột).

Cấu trúc lệnh đã được tối giản hóa, lưu bản nháp bền vững trên ổ cứng (drafts.json),
khóa chống chạy trùng tiến trình (Single-Instance Lock) và tự động retry khi OpenRouter/Threads bận.
"""
import asyncio
import csv
import datetime
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

import requests

import threads

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEDULE_FILE = HERE / "schedule.json"
DRAFTS_FILE = HERE / "drafts.json"
SINGLETON_PORT = 47891  # Cổng nội bộ chống chạy 2 bot cùng lúc trên máy


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


# ==============================================================================
# 1. QUẢN LÝ TRẠNG THÁI BỀN VỮNG (SCHEDULE & DRAFTS) - KHÔNG MẤT DỮ LIỆU KHI RESTART
# ==============================================================================
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


def load_drafts() -> tuple[dict[str, str], int]:
    if not DRAFTS_FILE.exists():
        return {}, 0
    try:
        data = json.loads(DRAFTS_FILE.read_text(encoding="utf-8"))
        return data.get("items", {}), int(data.get("counter", 0))
    except Exception:
        return {}, 0


def save_draft_item(post_text: str) -> int:
    items, counter = load_drafts()
    counter += 1
    items[str(counter)] = post_text
    # Chỉ giữ tối đa 20 bản nháp gần nhất để gọn file
    if len(items) > 20:
        oldest = sorted(items.keys(), key=int)[:-20]
        for k in oldest:
            items.pop(k, None)
    DRAFTS_FILE.write_text(json.dumps({"counter": counter, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return counter


def pop_draft_item(draft_id: int | None = None) -> tuple[int | None, str | None]:
    """Lấy bản nháp theo số (hoặc bản nháp mới nhất nếu không truyền số)."""
    items, counter = load_drafts()
    if not items:
        return None, None
    key = str(draft_id) if draft_id is not None else sorted(items.keys(), key=int)[-1]
    if key not in items:
        return None, None
    text = items.pop(key)
    DRAFTS_FILE.write_text(json.dumps({"counter": counter, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(key), text


# ==============================================================================
# 2. CÂN BẰNG PHỄU & XỬ LÝ SKILLS / LOG
# ==============================================================================
def pick_next_funnel_tier() -> tuple[str, dict[str, int]]:
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
    return max(deficits, key=deficits.get), counts


def select_skills_for_prompt(prompt: str) -> list[str]:
    p = prompt.lower()
    if "/tofu" in p or "/pov" in p:
        return ["marketing-psychology", "copywriting", "copy-editing"]
    if "/mofu" in p:
        return ["copywriting", "copy-editing", "image"]
    if "/bofu" in p:
        return ["offers", "product-marketing", "marketing-psychology", "copy-editing"]
    return ["copywriting", "copy-editing", "marketing-psychology", "offers", "product-marketing"]


def load_skills_text(skill_names: list[str]) -> str:
    blocks = []
    skills_dir = ROOT / ".agents" / "skills"
    for name in skill_names:
        sp = skills_dir / name / "SKILL.md"
        if sp.exists():
            blocks.append(f"## SKILL: {name}\n{sp.read_text(encoding='utf-8')[:5500]}")
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
        "  + Viết như một người trẻ đang kể lại trải nghiệm thật / tâm sự sau buổi đi cà phê với bạn bè, KHÔNG viết theo công thức quảng cáo.\n"
        "  + HẠN CHẾ XUỐNG DÒNG NHIỀU: Viết liền mạch thành 1 đến tối đa 2 đoạn văn ngắn gọn cho cả bài. Tuyệt đối KHÔNG ngắt mỗi câu một dòng rời rạc.\n"
        "  + GIỚI HẠN ICON: Cả bài CHỈ DÙNG TỐI ĐA 1 EMOJI DUY NHẤT (hoặc chỉ dùng '=))))', ':)))').\n"
        "- HAI NHÁNH BÀI TOFU TRÊN THREADS:\n"
        "  + Nhánh A (POV Meme 1 câu - KHI người dùng gõ `/pov` hoặc luân phiên 50% khi gõ `/tofu`): Chỉ viết ĐÚNG 1 CÂU DUY NHẤT (12-28 chữ, không xuống dòng, 0 icon) mở đầu bằng 'Pov: T đang...' hoặc 'Pov: Khi t...'.\n"
        "  + Nhánh B (Tâm sự đời thường 1-2 đoạn): Kể lại câu chuyện đi cà phê với bạn bè / xé túi mù đọc thẻ lời khai liền mạch 1-2 đoạn ngắn, hạn chế xuống dòng, 0-1 icon.\n"
        "- Mỗi bài Threads BẮT BUỘC ngắn gọn dưới 400 ký tự. Đặt nguyên văn nội dung đăng trong một khối ```threads ... ```; không đưa báo cáo hay ghi chú vào trong khối đó.\n"
        "- Để hệ thống tự động ghi vào content-log.csv, ở cuối câu trả lời (ngay sau khối BÁO CÁO HOÀN THÀNH), hãy xuất thêm một khối ```csv_log ... ``` chứa các dòng mới theo đúng định dạng:\n"
        f'{today},TOFU/MOFU/BOFU,Threads/Facebook,"Angle_Used","Opening_Hook"\n'
        "- Luôn kết thúc phần hiển thị bằng khối báo cáo ở Mục 5."
    )


# ==============================================================================
# 3. GỌI MODEL OPENROUTER (CÓ AUTO-RETRY KHI BẬN) & TELEGRAM API
# ==============================================================================
HISTORY: dict[int, list[dict[str, str]]] = {}


def tg(method: str, **params):
    try:
        r = requests.post(f"{TG}/{method}", json=params, timeout=65)
        return r.json()
    except Exception as e:
        print(f"tg({method}) error:", e)
        return {}


def send(chat_id: int, text: str) -> None:
    text = text.strip() or "(trống)"
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id, text=text[i : i + 4000])


def request_openrouter_with_retry(messages: list[dict[str, str]], temperature: float = 0.7) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://manhmoi.vn",
        "X-Title": "Manh Moi Marketing Agent",
    }
    payload = {"model": MODEL, "messages": messages, "temperature": temperature}
    last_err = ""
    for attempt in range(3):
        try:
            r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=120)
            data = r.json()
            if r.status_code == 200 and "choices" in data:
                msg = data["choices"][0].get("message") or {}
                reply = (msg.get("content") or "").strip()
                if not reply and msg.get("reasoning"):
                    reply = msg["reasoning"].strip()
                if reply:
                    return reply
            last_err = data.get("error", {}).get("message") if isinstance(data, dict) else str(data)
        except Exception as e:
            last_err = str(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"OpenRouter ({MODEL}) bận sau 3 lần thử: {last_err}")


def shrink_for_threads(text: str) -> str:
    text = text.strip()
    if len(text) <= 500:
        return text
    prompt = (
        f"Rút gọn bài đăng Threads tiếng Việt dưới đây từ {len(text)} ký tự xuống dưới 400 ký tự. "
        "GIỮ NGUYÊN giọng kể chuyện đời thường, viết liền mạch 1-2 đoạn (hạn chế xuống dòng), và TỐI ĐA 1 emoji. "
        "Chỉ trả về đúng nội dung bài đăng:\n\n" + text
    )
    try:
        short = request_openrouter_with_retry([{"role": "user", "content": prompt}], temperature=0.4)
        short = re.sub(r"^```\w*\s*|\s*```$", "", short).strip()
        if 30 < len(short) <= 500:
            return short
    except Exception:
        pass
    return text


def call_openrouter_sync(chat_id: int, prompt: str) -> str:
    sys_prompt = build_instructions(prompt)
    hist = HISTORY.get(chat_id, [])[-4:]
    messages = [{"role": "system", "content": sys_prompt}] + hist + [{"role": "user", "content": prompt}]
    reply = request_openrouter_with_retry(messages, temperature=0.7)
    cleaned_reply, _ = append_csv_rows(reply)
    hist.append({"role": "user", "content": prompt})
    hist.append({"role": "assistant", "content": cleaned_reply})
    HISTORY[chat_id] = hist[-4:]
    return cleaned_reply


# ==============================================================================
# 4. BẢNG LỆNH TỐI GIẢN (GỌN GÀNG - KHÔNG XUNG ĐỘT)
# ==============================================================================
HELP_TEXT = """📖 BẢNG ĐIỀU KHIỂN MÁNH MỐI BOT (TỐI GIẢN)

1️⃣ NHÓM TẠO BÀI & ĐĂNG THREADS (5 lệnh chính):
• /auto  — Tự chọn tầng thiếu nhất (40/40/20) & đăng Threads.
• /pov   — Bài 1 câu "Pov: T đang..." + 2-3 ảnh Meme trượt ngang.
• /tofu  — Bài nhận diện đời thường (thêm nội dung tùy ý, vd: /tofu cà phê).
• /mofu  — Bài tranh luận lời khai vụ Tuyệt Tác Tàn Tro.
• /bofu  — Bài mở đặt trước (Gói 150k & Combo 220k).

2️⃣ NHÓM DUYỆT BÀI (2 lệnh):
• /wait  — Bật/Tắt chế độ Chờ duyệt (hoặc gõ kèm: `/pov /wait`).
• /dang  — Duyệt đăng bản nháp mới nhất lên Threads (hoặc `/dang <số>`).

3️⃣ NHÓM HẸN GIỜ & KIỂM TRA (3 lệnh):
• /lich  — Xem lịch hẹn (Đổi giờ: `/lich 12:00 20:00` | Bật/tắt: `/lich on` hoặc `/lich off`).
• /hen   — Hẹn 1 lần (Vd: `/hen 21:30 /pov` | Xóa: `/hen xoa`).
• /status — Xem toàn bộ trạng thái hệ thống, kho ảnh Meme & nhật ký bài."""


def format_status() -> str:
    sched = load_schedule()
    tier, counts = pick_next_funnel_tier()
    total = sum(counts.values())
    memes = threads.list_meme_images()
    drafts, _ = load_drafts()
    sched_state = "🟢 BẬT" if sched.get("enabled", True) else "🔴 TẮT"
    wait_state = "⏸️ CHỜ DUYỆT (/wait)" if sched.get("wait_approval", False) else "▶️ TỰ ĐĂNG THẲNG"
    daily = ", ".join(sched.get("daily_times", [])) or "(chưa đặt)"
    one_offs = sched.get("one_off", [])
    one_off_txt = "\n".join(f"  {i}. [{x['run_at']}] -> {x['cmd']}" for i, x in enumerate(one_offs, 1)) or "  (Trống)"

    return (
        f"📊 TRẠNG THÁI HỆ THỐNG MÁNH MỐI\n"
        f"• Model: `{MODEL}`\n"
        f"• Threads: `@n_khac.minh` ({wait_state})\n"
        f"• Kho ảnh Meme: {len(memes)} ảnh | Bản nháp chờ: {len(drafts)}\n\n"
        f"📈 TỶ LỆ PHỄU (Tổng {total} bài):\n"
        f"• TOFU: {counts['TOFU']} | MOFU: {counts['MOFU']} | BOFU: {counts['BOFU']}\n"
        f"👉 Bài tự động tiếp theo: **{tier}**\n\n"
        f"⏰ LỊCH HẸN HÀNG NGÀY ({sched_state}): {daily}\n"
        f"📌 Lịch hẹn 1 lần:\n{one_off_txt}"
    )


async def execute_and_publish(
    chat_id: int,
    prompt: str,
    lock: asyncio.Lock,
    prefix_note: str = "",
    force_wait: bool = False,
) -> None:
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
            send(chat_id, f"⚠️ Lỗi gọi model: {e}")
            return

    found = re.findall(r"```threads\s*\n(.*?)```", reply, flags=re.S)
    auto_pub = (not wait_mode) and (os.environ.get("AUTO_PUBLISH_THREADS", "").lower() in ("1", "true", "yes"))
    new_ids = []
    for post in found:
        shrunk = await asyncio.to_thread(shrink_for_threads, post.strip())
        did = save_draft_item(shrunk)
        new_ids.append(did)

    send(chat_id, reply)
    if found:
        if auto_pub and threads.configured():
            for did in new_ids:
                _, post_text = pop_draft_item(did)
                if not post_text:
                    continue
                try:
                    pid, img_name = await asyncio.to_thread(threads.publish_text, post_text)
                    img_note = f" kèm ảnh `{img_name}`" if img_name else ""
                    send(chat_id, f"✅ Đã tự động đăng bài #{did}{img_note} lên Threads (id {pid}).")
                except Exception as e:
                    re_id = save_draft_item(post_text)
                    send(chat_id, f"⚠️ Tự đăng lỗi: {e}. Đã lưu lại nháp #{re_id}, gõ `/dang` để thử lại.")
        else:
            ids_str = ", ".join(f"#{i}" for i in new_ids)
            send(
                chat_id,
                f"⏸️ CHỜ DUYỆT: Đã lưu bản nháp {ids_str} (chưa đăng lên Threads).\n"
                f"👉 Gõ `/dang` để đăng ngay bản nháp mới nhất!",
            )


async def scheduler_loop(lock: asyncio.Lock) -> None:
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

            # 1. Lịch hẹn 1 lần
            remaining, due_items = [], []
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
                    await handle_command(chat_id, cmd, lock, from_scheduler=f"⏰ ĐẾN GIỜ HẸN ({item.get('run_at')})")

            # 2. Lịch cố định hàng ngày
            if sched.get("enabled", True):
                last_runs = sched.get("last_daily_runs", {})
                if hm_str in sched.get("daily_times", []) and last_runs.get(hm_str) != today_str:
                    last_runs[hm_str] = today_str
                    sched["last_daily_runs"] = last_runs
                    save_schedule(sched)
                    await handle_command(target_chat, "/auto", lock, from_scheduler=f"⏰ LỊCH HÀNG NGÀY ({hm_str})")
        except Exception as e:
            print("scheduler error:", e)


async def handle_command(chat_id: int, text: str, lock: asyncio.Lock, from_scheduler: str = "") -> None:
    t_clean = text.strip()
    t_low = t_clean.lower()

    # 1. Nhóm trợ giúp & trạng thái (gộp gọn /status, /init, /log, /anh, /lich xem)
    if t_low in ("/help", "/huongdan", "help"):
        send(chat_id, HELP_TEXT)
        return

    if t_low in ("/status", "/init", "/log", "khởi tạo hệ thống"):
        send(chat_id, format_status())
        return

    if t_low == "/anh":
        imgs = threads.list_meme_images()
        names = "\n".join(f"  {i}. {p.name}" for i, p in enumerate(imgs, 1)) or "  (Trống)"
        send(chat_id, f"🖼️ KHO ẢNH MEME ({len(imgs)} ảnh):\n{names}")
        return

    # 2. Nhóm /wait (Bật/tắt chế độ chờ duyệt)
    if t_low in ("/wait", "/wait on", "/wait off"):
        sched = load_schedule()
        if t_low == "/wait on":
            sched["wait_approval"] = True
        elif t_low == "/wait off":
            sched["wait_approval"] = False
        else:
            sched["wait_approval"] = not bool(sched.get("wait_approval", False))
        save_schedule(sched)
        state_msg = (
            "⏸️ ĐANG BẬT CHỜ DUYỆT: Mọi bài tạo ra sẽ lưu nháp đợi bạn gõ `/dang` mới đăng."
            if sched["wait_approval"]
            else "▶️ ĐANG BẬT TỰ ĐỘNG ĐĂNG: Viết xong tự đăng thẳng lên Threads (vẫn có thể thêm `/wait` ở cuối lệnh lẻ)."
        )
        send(chat_id, state_msg)
        return

    # 3. Nhóm /dang (Gõ `/dang` không cần nhớ số -> tự đăng bản nháp mới nhất)
    if t_low == "/dang" or t_low.startswith("/dang "):
        if not threads.configured():
            send(chat_id, "Chưa cấu hình THREADS_USER_ID / THREADS_ACCESS_TOKEN trong .env.")
            return
        m = re.match(r"^/dang\s+(\d+)$", t_low)
        target_id = int(m.group(1)) if m else None
        did, post_text = pop_draft_item(target_id)
        if not post_text:
            items, _ = load_drafts()
            avail = ", ".join(f"#{k}" for k in items.keys()) or "không có"
            send(chat_id, f"Không tìm thấy bản nháp. Các bản nháp hiện có: {avail}")
            return
        tg("sendChatAction", chat_id=chat_id, action="typing")
        try:
            pid, img_name = await asyncio.to_thread(threads.publish_text, post_text)
            img_note = f" kèm ảnh `{img_name}`" if img_name else ""
            send(chat_id, f"✅ Đã duyệt đăng bản nháp #{did}{img_note} lên Threads (id {pid}).")
        except Exception as e:
            re_id = save_draft_item(post_text)
            send(chat_id, f"⚠️ Đăng lỗi: {e}. Đã giữ lại bản nháp #{re_id}.")
        return

    # 4. Nhóm /lich (Tích hợp xem lịch, đổi giờ hàng ngày, bật/tắt vào chung 1 lệnh `/lich`)
    if t_low == "/lich" or t_low.startswith("/lich ") or t_low.startswith("/lich_ngay") or t_low in ("/hen_bat", "/hen_tat"):
        sched = load_schedule()
        if t_low in ("/lich on", "/hen_bat"):
            sched["enabled"] = True
            save_schedule(sched)
            send(chat_id, "🟢 Đã BẬT lịch tự động hàng ngày.\n\n" + format_status())
            return
        if t_low in ("/lich off", "/hen_tat"):
            sched["enabled"] = False
            save_schedule(sched)
            send(chat_id, "🔴 Đã TẮT lịch tự động hàng ngày.\n\n" + format_status())
            return
        times = re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t_clean)
        if times:
            formatted = sorted({f"{int(h):02d}:{m}" for h, m in times})
            sched["daily_times"] = formatted
            sched["enabled"] = True
            save_schedule(sched)
            send(chat_id, f"✅ Đã đặt giờ tự đăng hàng ngày: {', '.join(formatted)}\n\n" + format_status())
            return
        send(chat_id, format_status())
        return

    # 5. Nhóm /hen (Hẹn 1 lần & xóa lịch hẹn gộp chung vào `/hen`)
    if t_low.startswith("/hen"):
        arg = re.sub(r"^/hen(?:_xoa)?\s*", "", t_clean, flags=re.I).strip()
        sched = load_schedule()
        if arg.lower().startswith("xoa") or t_low.startswith("/hen_xoa"):
            sub = re.sub(r"^xoa\s*", "", arg, flags=re.I).strip().lower()
            one_offs = sched.get("one_off", [])
            if sub in ("", "all"):
                sched["one_off"] = []
                save_schedule(sched)
                send(chat_id, "🗑️ Đã xóa toàn bộ lịch hẹn 1 lần.")
                return
            if sub.isdigit() and 1 <= int(sub) <= len(one_offs):
                removed = one_offs.pop(int(sub) - 1)
                sched["one_off"] = one_offs
                save_schedule(sched)
                send(chat_id, f"🗑️ Đã xóa lịch hẹn: [{removed['run_at']}] -> {removed['cmd']}")
                return
        m = re.match(r"^(?:(\d{4}-\d{2}-\d{2})\s+)?([01]?\d|2[0-3]):([0-5]\d)(?:\s+(.+))?$", arg)
        if not m:
            send(chat_id, "Dùng: `/hen HH:MM [lệnh]` (Vd: `/hen 21:30 /pov`)\nHoặc xóa lịch: `/hen xoa`")
            return
        date_part, hh, mm, cmd_part = m.groups()
        now = datetime.datetime.now()
        target_time = datetime.time(int(hh), int(mm))
        target_date = datetime.date.fromisoformat(date_part) if date_part else now.date()
        if not date_part and datetime.datetime.combine(target_date, target_time) <= now:
            target_date += datetime.timedelta(days=1)
        run_at = f"{target_date.isoformat()} {int(hh):02d}:{mm}"
        cmd = (cmd_part or "/auto").strip()
        sched.setdefault("one_off", []).append({"run_at": run_at, "cmd": cmd, "chat_id": chat_id})
        sched["one_off"].sort(key=lambda x: x["run_at"])
        save_schedule(sched)
        send(chat_id, f"✅ Đã hẹn lúc **{run_at}** tự chạy: `{cmd}`")
        return

    # 6. Nhóm lệnh tạo nội dung chuẩn hóa (/auto, /pov, /tofu, /mofu, /bofu)
    has_wait = bool(re.search(r"(?:^|\s)/wait(?:\s|$)", t_clean, flags=re.I))
    core_cmd = re.sub(r"(?:^|\s)/wait(?:\s|$)", " ", t_clean, flags=re.I).strip()
    core_low = core_cmd.lower()

    if core_low == "/auto":
        tier, counts = pick_next_funnel_tier()
        if tier == "TOFU" and (counts["TOFU"] % 2 == 0):
            expanded = "/tofu 1 Threads theo nhánh POV Meme 1 câu cực ngắn mở đầu bằng 'Pov: T đang...'"
        else:
            expanded = f"/{tier.lower()} 1 Threads"
        note = f"{from_scheduler + ' -> ' if from_scheduler else ''}🎯 Tự động cân bằng phễu -> Chọn tầng **{tier}**..."
        await execute_and_publish(chat_id, expanded, lock, prefix_note=note, force_wait=has_wait)
        return

    if core_low == "/pov" or core_low.startswith("/pov ") or core_low.startswith("/tofu pov"):
        extra = re.sub(r"^(/pov|/tofu\s+pov)\s*", "", core_cmd, flags=re.I).strip()
        expanded = f"/tofu 1 Threads theo nhánh POV Meme 1 câu cực ngắn mở đầu bằng 'Pov: T đang...' {extra}".strip()
        await execute_and_publish(chat_id, expanded, lock, prefix_note=from_scheduler, force_wait=has_wait)
        return

    if core_low in ("/tofu", "/mofu", "/bofu"):
        expanded = f"{core_low} 1 Threads"
        await execute_and_publish(chat_id, expanded, lock, prefix_note=from_scheduler, force_wait=has_wait)
        return

    await execute_and_publish(chat_id, core_cmd, lock, prefix_note=from_scheduler, force_wait=has_wait)


def acquire_singleton_socket() -> socket.socket | None:
    """Đảm bảo trên cùng 1 máy chỉ có đúng 1 tiến trình bot chạy để không bao giờ bị xung đột getUpdates (409 Conflict)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


async def main() -> None:
    singleton = acquire_singleton_socket()
    if singleton is None and not os.environ.get("GITHUB_ACTIONS"):
        print("Một tiến trình bot khác đang chạy sẵn trên máy (cổng khóa 47891). Thoát để tránh xung đột.")
        return

    load_schedule()
    offset = 0
    lock = asyncio.Lock()
    asyncio.create_task(scheduler_loop(lock))
    print(f"Bot + Scheduler (Tinh Gọn) đang chạy với model: {MODEL}. Allowed: {ALLOWED or 'CHƯA CẤU HÌNH'}")
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
                send(chat_id, f"Chat ID của bạn: {chat_id}\nModel: {MODEL}\n\n" + HELP_TEXT)
                continue
            if chat_id not in ALLOWED:
                send(chat_id, "Không có quyền.")
                continue
            await handle_command(chat_id, text, lock)


if __name__ == "__main__":
    asyncio.run(main())
