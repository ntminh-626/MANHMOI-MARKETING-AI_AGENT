"""Telegram Bot + Auto-Scheduler cho Mánh Mối Marketing Agent (Phiên bản Tinh Gọn & Chống Xung Đột).

Cấu trúc lệnh đã được tối giản hóa, lưu bản nháp bền vững trên ổ cứng (drafts.json),
khóa chống chạy trùng tiến trình (Single-Instance Lock) và tự động retry khi OpenRouter/Threads bận.
"""
import asyncio
import csv
import datetime
import json
import os
import random
import re
import socket
import sys
import time
from pathlib import Path

import random
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


def load_drafts() -> tuple[dict[str, dict], int]:
    if not DRAFTS_FILE.exists():
        return {}, 0
    try:
        data = json.loads(DRAFTS_FILE.read_text(encoding="utf-8"))
        raw_items = data.get("items", {})
        normalized: dict[str, dict] = {}
        for k, v in raw_items.items():
            if isinstance(v, str):
                normalized[k] = {"text": v, "images": []}
            elif isinstance(v, dict):
                normalized[k] = {"text": v.get("text", ""), "images": v.get("images", []), "reply": v.get("reply", "")}
        return normalized, int(data.get("counter", 0))
    except Exception:
        return {}, 0


def read_draft_reply(draft_id: int | None) -> str:
    """Đọc bình luận mồi (First Self-Reply) gắn với bản nháp (gọi TRƯỚC khi pop)."""
    items, _ = load_drafts()
    if draft_id is None:
        if not items:
            return ""
        draft_id = int(sorted(items.keys(), key=int)[-1])
    return (items.get(str(draft_id), {}) or {}).get("reply", "")


def save_draft_item(post_text: str, image_names: list[str] | None = None, reply_text: str = "", use_video: bool = False) -> int:
    items, counter = load_drafts()
    counter += 1
    if image_names is None and use_video:
        vid = threads.pick_video_for_text(post_text)
        if vid:
            image_names = [vid.name]
    if image_names is None:
        target_cnt = threads.pick_pov_image_count() if threads.is_pov_post(post_text) else 1
        picked = threads.pick_memes_for_text(post_text, count=target_cnt)
        image_names = [p.name for p in picked]
    items[str(counter)] = {"text": post_text, "images": image_names, "reply": reply_text}
    if len(items) > 20:
        oldest = sorted(items.keys(), key=int)[:-20]
        for k in oldest:
            items.pop(k, None)
    DRAFTS_FILE.write_text(json.dumps({"counter": counter, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return counter


def peek_draft_item(draft_id: int | None = None) -> tuple[int | None, str | None, list[Path]]:
    items, _ = load_drafts()
    if not items:
        return None, None, []
    key = str(draft_id) if draft_id is not None else sorted(items.keys(), key=int)[-1]
    if key not in items:
        return None, None, []
    entry = items[key]
    all_memes = {p.name: p for p in threads.list_all_media()}
    paths = [all_memes[name] for name in entry.get("images", []) if name in all_memes]
    return int(key), entry.get("text", ""), paths


def update_draft_images(draft_id: int | None = None) -> tuple[int | None, str | None, list[Path]]:
    """Đổi bộ ảnh Meme khác cho bản nháp đang chờ duyệt."""
    items, counter = load_drafts()
    if not items:
        return None, None, []
    key = str(draft_id) if draft_id is not None else sorted(items.keys(), key=int)[-1]
    if key not in items:
        return None, None, []
    post_text = items[key].get("text", "")
    cur_names = items[key].get("images", [])
    if any(threads.is_video_name(n) for n in cur_names):
        v = threads.pick_video_for_text(post_text, avoid=cur_names)
        picked = [v] if v else []
    else:
        target_cnt = threads.pick_pov_image_count() if threads.is_pov_post(post_text) else 1
        picked = threads.pick_memes_for_text(post_text, count=target_cnt)
    items[key]["images"] = [p.name for p in picked]
    DRAFTS_FILE.write_text(json.dumps({"counter": counter, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(key), post_text, picked


def pop_draft_item(draft_id: int | None = None) -> tuple[int | None, str | None, list[Path]]:
    """Lấy bản nháp kèm danh sách ảnh đã duyệt theo số (hoặc bản nháp mới nhất nếu không truyền số)."""
    items, counter = load_drafts()
    if not items:
        return None, None, []
    key = str(draft_id) if draft_id is not None else sorted(items.keys(), key=int)[-1]
    if key not in items:
        return None, None, []
    entry = items.pop(key)
    DRAFTS_FILE.write_text(json.dumps({"counter": counter, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    all_memes = {p.name: p for p in threads.list_all_media()}
    paths = [all_memes[name] for name in entry.get("images", []) if name in all_memes]
    return int(key), entry.get("text", ""), paths


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


TOPICS_FILE = HERE / "topics.json"
TOPIC_STATE_FILE = HERE / "topic_state.json"


def _load_topics() -> dict:
    try:
        return json.loads(TOPICS_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _pick_fresh(pool_name: str, pool: list[str], state: dict) -> str:
    used = state.get(pool_name, [])
    fresh = [x for x in pool if x not in used[-max(len(pool) - 1, 1):]] or list(pool)
    choice = random.choice(fresh)
    state[pool_name] = (used + [choice])[-len(pool):]
    return choice


def build_topic_seed(prompt: str) -> str:
    """Chọn ngẫu nhiên (có nhớ để không lặp) chủ đề/kiểu mở đầu và gắn vào yêu cầu, ép bài mới đa dạng."""
    low = prompt.lower()
    topics = _load_topics()
    if not topics:
        return ""
    try:
        state = json.loads(TOPIC_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    seed = ""
    # Nếu người dùng tự đưa ý tưởng (vd: /pov đi họp quên tắt mic) -> dùng ĐÚNG ý tưởng đó, bỏ chủ đề ngẫu nhiên
    idea = re.sub(r"theo nhánh pov meme 1 câu cực ngắn mở đầu bằng 'pov:'", " ", low)
    idea = re.sub(r"/(?:tofu|mofu|bofu|pov|video|usevideo|wait|auto)\b|\bthreads\b|\bfacebook\b|(?<!\w)\d+(?!\w)", " ", idea)
    idea = re.sub(r"\s+", " ", idea).strip(" .,:;-")
    if len(idea) >= 4:
        if "/tofu" in low and "pov" in low:
            o = _pick_fresh("pov_openers", topics.get("pov_openers", []), state) if topics.get("pov_openers") else "Pov: T đang ..."
            mention = random.random() < 0.4
            seed = (
                f"Ý TƯỞNG CỦA NGƯỜI DÙNG (BẮT BUỘC dùng làm tình huống chính, KHÔNG thay bằng chủ đề khác, KHÔNG lặp lại bài cũ trong log): \"{idea}\". "
                f"Viết lại thành 1 câu POV hoàn toàn mới, tự nhiên, đời thường. KIỂU MỞ ĐẦU gợi ý: {o}. "
                + ("Được nhắc thoáng 'xé túi mù' đúng 1 lần." if mention else "KHÔNG nhắc túi mù, blind box, thẻ bài, game hay sản phẩm.")
            )
        else:
            seed = f"Ý TƯỞNG CỦA NGƯỜI DÙNG (BẮT BUỘC làm chủ đề chính, viết mới hoàn toàn, không lặp bài cũ): \"{idea}\"."
        try:
            TOPIC_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return seed
    if "/tofu" in low and ("pov" in low):
        t = _pick_fresh("pov_topics", topics.get("pov_topics", []), state) if topics.get("pov_topics") else ""
        o = _pick_fresh("pov_openers", topics.get("pov_openers", []), state) if topics.get("pov_openers") else "Pov: T đang ..."
        mention = random.random() < 0.4
        seed = (
            f"GỢI Ý CHỦ ĐỀ (BẮT BUỘC theo): tình huống đời sống: {t}. KIỂU MỞ ĐẦU: {o}. "
            + ("Được nhắc thoáng 'xé túi mù' đúng 1 lần như chi tiết đời thường." if mention
               else "KHÔNG nhắc túi mù, blind box, thẻ bài, game hay sản phẩm nào trong bài.")
        )
    elif "/tofu" in low:
        t = _pick_fresh("story_topics", topics.get("story_topics", []), state) if topics.get("story_topics") else ""
        seed = f"GỢI Ý CHỦ ĐỀ (BẮT BUỘC theo): kể lại tình huống: {t}. Nhắc thoáng 'xé túi mù' nếu hợp, không nói trinh thám."
    elif "/mofu" in low:
        t = _pick_fresh("mofu_angles", topics.get("mofu_angles", []), state) if topics.get("mofu_angles") else ""
        seed = f"GỢI Ý GÓC TIẾP CẬN (BẮT BUỘC theo): {t}. Chỉ dùng thông tin trong phần ĐƯỢC PHÉP DÙNG."
    elif "/bofu" in low:
        t = _pick_fresh("bofu_angles", topics.get("bofu_angles", []), state) if topics.get("bofu_angles") else ""
        seed = f"GỢI Ý GÓC BÁN (BẮT BUỘC theo): {t}."
    if seed:
        try:
            TOPIC_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return seed



def read_recent_log() -> str:
    log_path = ROOT / "content-log.csv"
    if not log_path.exists():
        return "Date,Funnel_Tier,Channel,Angle_Used,Opening_Hook"
    lines = log_path.read_text(encoding="utf-8-sig").strip().splitlines()
    header = lines[0] if lines else "Date,Funnel_Tier,Channel,Angle_Used,Opening_Hook"
    tail = []
    try:
        for row in csv.reader(lines[-30:] if len(lines) > 1 else []):
            if len(row) >= 5 and row[0] != "Date":
                tail.append(f"{row[0]} | {row[1]} | {row[3][:90]} | {row[4][:90]}")
    except Exception:
        tail = lines[-30:]
    return "\n".join(["Date | Tier | Angle | Opening_Hook"] + tail)


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
        + "\n\n---\n# RECENT CONTENT LOG (content-log.csv - last 30 rows)\n" + recent_log + "\n(QUY TẮC CHỐNG LẶP: KHÔNG dùng lại chủ đề, tình huống, từ khóa chính hay cấu trúc câu mở của bất kỳ dòng nào ở trên. Đặc biệt tránh lặp các mô-típ đã xuất hiện nhiều: lật thẻ, hung thủ, xé túi mù, bạn thân thề dối, cả đám quay mặt nhìn. Luôn đi theo GỢI Ý CHỦ ĐỀ trong yêu cầu.)"
        + "\n\n---\n# TELEGRAM & LOGGING MODE\n"
        f"Hôm nay là {today}. Người dùng ra lệnh qua Telegram.\n"
        "- Trả lời ngắn gọn, không dùng bảng markdown, không dùng LaTeX.\n"
        "- Tuyệt đối KHÔNG nhắc tới quét mã QR (người chơi mở túi mù đọc trực tiếp thẻ bài lời khai và hồ sơ được cung cấp).\n"
        "- PHONG CÁCH VIẾT NHƯ NGƯỜI THẬT 100%, TUYỆT ĐỐI KHÔNG SEEDING LỘ LIỄU (BẮT BUỘC):\n"
        "  + Viết như một người trẻ đang kể lại trải nghiệm thật / tâm sự sau buổi đi cà phê với bạn bè, KHÔNG viết theo công thức quảng cáo.\n"
        "  + HẠN CHẾ XUỐNG DÒNG NHIỀU: Viết liền mạch thành 1 đến tối đa 2 đoạn văn ngắn gọn cho cả bài. Tuyệt đối KHÔNG ngắt mỗi câu một dòng rời rạc.\n"
        "  + GIỚI HẠN ICON: Cả bài CHỈ DÙNG TỐI ĐA 1 EMOJI DUY NHẤT (hoặc chỉ dùng '=))))', ':)))').\n"
        "- QUY TẮC RIÊNG CHO TOFU (/tofu, /pov, /auto noon): nội dung phải RỘNG, đánh vào tình huống đời sống ai cũng đồng cảm (tụ tập bạn bè cắm mặt điện thoại, tiêu tiền tự thưởng, xả stress sau giờ làm, cãi nhau chuyện nhỏ, đứa bạn hay nói dối). Về sản phẩm CHỈ được nhắc thoáng 'xé túi mù' như một chi tiết đời thường. TUYỆT ĐỐI KHÔNG dùng các từ: trinh thám, phá án, hung thủ, vụ án, hồ sơ, thẻ lời khai, board game, Mánh Mối, Bad Ending, True Ending, check var; không nêu tính năng, giá hay luật chơi. Bình luận mồi của bài TOFU cũng giữ giọng chung như vậy.\n"
        "- HAI NHÁNH BÀI TOFU TRÊN THREADS:\n"
        "  + Nhánh A (POV Meme 1 câu - KHI người dùng gõ `/pov` hoặc luân phiên 50% khi gõ `/tofu`): Chỉ viết ĐÚNG 1 CÂU DUY NHẤT (12-28 chữ, không xuống dòng, 0 icon) mở đầu bằng 'Pov:' theo đúng KIỂU MỞ ĐẦU được chỉ định ở GỢI Ý CHỦ ĐỀ (không lặp 'Pov: T đang...' mãi). POV phải là tình huống ĐỜI SỐNG thuần túy (cái ví/tự thưởng, deadline, cà phê cắm mặt điện thoại, nhắn tin bị seen, đứa bạn hứa rồi bùng, đi ngủ muộn...). KHÔNG nhắc sản phẩm: chỉ khoảng 2/5 bài POV được phép có cụm 'xé túi mù' (tối đa 1 lần); 3/5 còn lại KHÔNG có túi mù/blind box/thẻ bài/game gì cả. Tuyệt đối không dùng hung thủ, phá án, lật thẻ, lời khai, vụ án.\n"
        "  + Nhánh B (Tâm sự đời thường 1-2 đoạn): Kể lại một tình huống tụ tập bạn bè / xả stress liền mạch 1-2 đoạn ngắn, hạn chế xuống dòng, 0-1 icon, nhắc thoáng xé túi mù nếu hợp ngữ cảnh.\n"
        "- Mỗi bài Threads BẮT BUỘC ngắn gọn dưới 400 ký tự. Đặt nguyên văn nội dung đăng trong một khối ```threads ... ```; không đưa báo cáo hay ghi chú vào trong khối đó.\n"
        "- QUY TẮC THUẬT TOÁN THREADS 2026 (BẮT BUỘC):\n"
        "  + KHÔNG gắn link ngoài, KHÔNG dùng danh sách hashtag (tối đa 0-1 topic tag, tốt nhất 0), KHÔNG dùng câu mồi tương tác sáo rỗng ('thả tim nếu...', 'tag bạn bè...'). Lồng từ khóa tự nhiên (túi mù phá án, board game trinh thám, thẻ lời khai) vào câu chữ.\n"
        "  + MOFU: kết bằng câu hỏi chọn phe A hay B (Binary Debate) từ lời khai có trong phần ĐƯỢC PHÉP DÙNG, không hé lộ manh mối cấm.\n"
        "  + BOFU: viết như chia sẻ thật, nêu cả 1 nhược điểm vui (Pratfall) và 1 ưu điểm, TUYỆT ĐỐI không gắn link trong bài chính; nhắc 'nhắn mình / link ở bio' nằm ở bình luận mồi.\n"
        "- BÌNH LUẬN MỒI (First Self-Reply, BẮT BUỘC cho mỗi bài Threads): sau khối ```threads```, thêm một khối ```reply ... ``` (dưới 280 ký tự) là bình luận chủ bài tự thả ngay dưới bài để mồi tranh luận: với POV/TOFU là câu bóc phốt tiếp hoặc hỏi xoáy; với MOFU là câu hỏi chọn phe; với BOFU là thông tin đặt trước (Gói Cơ Bản 150.000đ, Combo 220.000đ, nhắn Fanpage hoặc link ở Bio). Cùng quy tắc giọng người thật, tối đa 1 icon.\n"
        "- Thêm một khối ```suggest ... ``` gồm đúng 2 dòng, mỗi dòng là 1 câu gợi ý để chủ kênh trả lời bình luận của khách (mở rộng hội thoại bằng câu hỏi nối dài, không cụt lủn).\n"
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


def send_photo_preview(chat_id: int, image_paths: list[Path], caption: str = "") -> None:
    """Gửi 1 ảnh (sendPhoto) hoặc album 2-3 ảnh (sendMediaGroup) trực tiếp vào Telegram để người dùng xem trước."""
    valid = [p for p in image_paths if p.exists()]
    if not valid:
        if caption:
            send(chat_id, caption)
        return
    vids = [p for p in valid if threads.is_video_name(p.name)]
    if vids:
        v = vids[0]
        try:
            if v.stat().st_size <= 49 * 1024 * 1024:
                with v.open("rb") as f:
                    requests.post(
                        f"{TG}/sendVideo",
                        data={"chat_id": str(chat_id), "caption": caption[:1020]},
                        files={"video": (v.name, f, "video/mp4")},
                        timeout=180,
                    )
                return
            send(chat_id, caption + f"\n\n🎬 Video: {v.name} (quá 50MB nên không xem trước được trên Telegram)")
        except Exception as e:
            print("send video preview error:", e)
            send(chat_id, caption + f"\n\n🎬 Video: {v.name}")
        return
    try:
        if len(valid) == 1:
            with valid[0].open("rb") as f:
                requests.post(
                    f"{TG}/sendPhoto",
                    data={"chat_id": str(chat_id), "caption": caption[:1020]},
                    files={"photo": (valid[0].name, f, "image/jpeg")},
                    timeout=60,
                )
        else:
            media = []
            files = {}
            for idx, p in enumerate(valid):
                key = f"photo{idx}"
                item = {"type": "photo", "media": f"attach://{key}"}
                if idx == 0 and caption:
                    item["caption"] = caption[:1020]
                media.append(item)
                files[key] = (p.name, p.open("rb"), "image/jpeg")
            try:
                requests.post(
                    f"{TG}/sendMediaGroup",
                    data={"chat_id": str(chat_id), "media": json.dumps(media)},
                    files=files,
                    timeout=60,
                )
            finally:
                for _, ftuple in files.items():
                    try:
                        ftuple[1].close()
                    except Exception:
                        pass
    except Exception as e:
        print("send_photo_preview error:", e)
        if caption:
            send(chat_id, caption)


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


def call_openrouter_sync(chat_id: int, prompt: str, temperature: float = 0.7, fresh: bool = False) -> str:
    sys_prompt = build_instructions(prompt)
    hist = [] if fresh else HISTORY.get(chat_id, [])[-4:]
    messages = [{"role": "system", "content": sys_prompt}] + hist + [{"role": "user", "content": prompt}]
    reply = request_openrouter_with_retry(messages, temperature=temperature)
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
• /pov   — Bài 1 câu "Pov: T đang..." + 1/2/3 ảnh Meme (phân bổ ngẫu nhiên 40/40/20).
• /video — Bài POV 1 câu + 1 video Meme (từ assets/VIDEO MEME THREADS, vd: /video /wait).
• /tofu  — Bài nhận diện đời thường (thêm nội dung tùy ý, vd: /tofu cà phê).
• /mofu  — Bài tranh luận lời khai vụ Tuyệt Tác Tàn Tro.
• /bofu  — Bài mở đặt trước (Gói 150k & Combo 220k).

2️⃣ NHÓM DUYỆT BÀI & ĐỔI ẢNH (3 lệnh):
• /wait  — Bật/Tắt chế độ Chờ duyệt kèm gửi ảnh Preview ngay trên Telegram (hoặc gõ: `/pov /wait`).
• /doi   — Đổi bộ ảnh Meme khác cho bản nháp đang chờ duyệt (gửi lại ảnh Preview mới).
• /dang  — Duyệt đăng bản nháp + đúng bộ ảnh đã xem trước lên Threads.

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
    use_video = bool(re.search(r"(?:^|\s)/usevideo(?:\s|$)", clean_prompt, flags=re.I))
    clean_prompt = re.sub(r"(?:^|\s)/usevideo(?:\s|$)", " ", clean_prompt, flags=re.I).strip()
    if use_video and not threads.list_meme_videos():
        send(chat_id, "⚠️ Kho video đang trống (assets/VIDEO MEME THREADS). Bài này sẽ dùng ảnh Meme thay thế.")
        use_video = False
    sched = load_schedule()
    wait_mode = force_wait or has_wait_flag or bool(sched.get("wait_approval", False))

    tg("sendChatAction", chat_id=chat_id, action="typing")
    if prefix_note:
        send(chat_id, prefix_note)
    async with lock:
        try:
            seed = build_topic_seed(clean_prompt)
            gen_prompt = f"{clean_prompt}\n\n{seed}" if seed else clean_prompt
            reply = await asyncio.to_thread(
                call_openrouter_sync, chat_id, gen_prompt, 0.95 if seed else 0.7, bool(seed)
            )
        except Exception as e:
            send(chat_id, f"⚠️ Lỗi gọi model: {e}")
            return

    found = re.findall(r"```threads\s*\n(.*?)```", reply, flags=re.S)
    auto_pub = (not wait_mode) and (os.environ.get("AUTO_PUBLISH_THREADS", "").lower() in ("1", "true", "yes"))
    new_ids = []
    reply_blocks = [r.strip() for r in re.findall(r"```reply\s*\n(.*?)```", reply, flags=re.S)]
    suggest_blocks = re.findall(r"```suggest\s*\n(.*?)```", reply, flags=re.S)
    for idx, post in enumerate(found):
        shrunk = await asyncio.to_thread(shrink_for_threads, post.strip())
        first_reply = reply_blocks[idx] if idx < len(reply_blocks) else ""
        did = save_draft_item(shrunk, reply_text=first_reply, use_video=use_video)
        new_ids.append(did)

    send(chat_id, reply)
    if found:
        if auto_pub and threads.configured():
            for did in new_ids:
                first_reply = read_draft_reply(did)
                _, post_text, pre_imgs = pop_draft_item(did)
                if not post_text:
                    continue
                try:
                    pid, img_name = await asyncio.to_thread(
                        threads.publish_text, post_text, preselected_images=pre_imgs, first_reply=first_reply
                    )
                    img_note = f" kèm ảnh `{img_name}`" if img_name else ""
                    reply_note = " + bình luận mồi" if first_reply else ""
                    send(chat_id, f"✅ Đã tự động đăng bài #{did}{img_note}{reply_note} lên Threads (id {pid}).")
                    tips = "\n".join(f"• {ln.strip()}" for ln in (suggest_blocks[0].strip().splitlines() if suggest_blocks else []) if ln.strip())
                    send(
                        chat_id,
                        "💡 60 PHÚT VÀNG: vào Threads trả lời các bình luận đầu tiên bằng câu hỏi nối dài để kích hoạt +42% tương tác."
                        + (f"\nGợi ý trả lời:\n{tips}" if tips else ""),
                    )
                except Exception as e:
                    re_id = save_draft_item(post_text, [p.name for p in pre_imgs], reply_text=first_reply)
                    send(chat_id, f"⚠️ Tự đăng lỗi: {e}. Đã lưu lại nháp #{re_id}, gõ `/dang` để thử lại.")
        else:
            for did in new_ids:
                _, post_text, pre_imgs = peek_draft_item(did)
                first_reply = read_draft_reply(did)
                img_names_str = ", ".join(p.name for p in pre_imgs) or "(không ảnh)"
                preview_caption = (
                    f"⏸️ XEM TRƯỚC BẢN NHÁP #{did} (Chưa đăng):\n\n"
                    f"{post_text}\n\n"
                    f"💬 Bình luận mồi: {first_reply or '(không có)'}\n"
                    f"🖼️ Ảnh đính kèm ({len(pre_imgs)}): {img_names_str}\n"
                    f"👉 Gõ `/dang` để đăng ngay, hoặc `/doi` để đổi ảnh Meme khác!"
                )
                await asyncio.to_thread(send_photo_preview, chat_id, pre_imgs, preview_caption)


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
                    slot = "/auto noon" if int(hm_str[:2]) < 17 else "/auto night"
                    await handle_command(target_chat, slot, lock, from_scheduler=f"⏰ LỊCH HÀNG NGÀY ({hm_str})")
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
        vids = threads.list_meme_videos()
        vnames = "\n".join(f"  {i}. {p.name}" for i, p in enumerate(vids, 1)) or "  (Trống)"
        send(chat_id, f"🖼️ KHO ẢNH MEME ({len(imgs)} ảnh):\n{names}\n\n🎬 KHO VIDEO ({len(vids)} video):\n{vnames}")
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

    # 3. Nhóm /doi (Đổi ảnh Meme khác cho bản nháp đang chờ duyệt) & /dang (Duyệt đăng)
    if t_low in ("/doi", "/doi_anh") or t_low.startswith("/doi "):
        m = re.match(r"^/doi(?:_anh)?\s+(\d+)$", t_low)
        target_id = int(m.group(1)) if m else None
        did, post_text, new_imgs = update_draft_images(target_id)
        if not post_text:
            send(chat_id, "Hiện chưa có bản nháp nào đang chờ duyệt để đổi ảnh.")
            return
        img_names_str = ", ".join(p.name for p in new_imgs) or "(không ảnh)"
        caption = (
            f"🔄 ĐÃ ĐỔI ẢNH MỚI CHO BẢN NHÁP #{did}:\n\n"
            f"{post_text}\n\n"
            f"🖼️ Ảnh mới ({len(new_imgs)}): {img_names_str}\n"
            f"👉 Ưng ý thì gõ `/dang`, hoặc gõ `/doi` tiếp để chọn bộ ảnh khác!"
        )
        await asyncio.to_thread(send_photo_preview, chat_id, new_imgs, caption)
        return

    if t_low == "/dang" or t_low.startswith("/dang "):
        if not threads.configured():
            send(chat_id, "Chưa cấu hình THREADS_USER_ID / THREADS_ACCESS_TOKEN trong .env.")
            return
        m = re.match(r"^/dang\s+(\d+)$", t_low)
        target_id = int(m.group(1)) if m else None
        first_reply = read_draft_reply(target_id)
        did, post_text, pre_imgs = pop_draft_item(target_id)
        if not post_text:
            items, _ = load_drafts()
            avail = ", ".join(f"#{k}" for k in items.keys()) or "không có"
            send(chat_id, f"Không tìm thấy bản nháp. Các bản nháp hiện có: {avail}")
            return
        tg("sendChatAction", chat_id=chat_id, action="typing")
        try:
            pid, img_name = await asyncio.to_thread(
                threads.publish_text, post_text, preselected_images=pre_imgs, first_reply=first_reply
            )
            img_note = f" kèm ảnh `{img_name}`" if img_name else ""
            reply_note = " + bình luận mồi" if first_reply else ""
            send(chat_id, f"✅ Đã duyệt đăng bản nháp #{did}{img_note}{reply_note} lên Threads (id {pid}).\n💡 60 phút vàng: vào trả lời bình luận đầu tiên bằng câu hỏi nối dài để tăng reach.")
        except Exception as e:
            re_id = save_draft_item(post_text, [p.name for p in pre_imgs], reply_text=first_reply)
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

    if core_low in ("/auto", "/auto noon", "/auto night"):
        tier, counts = pick_next_funnel_tier()
        total = sum(counts.values()) or 1
        targets = {"TOFU": 0.40, "MOFU": 0.40, "BOFU": 0.20}
        if core_low == "/auto noon":
            tier = "TOFU"  # 12:00 trưa: giờ nghỉ trưa công sở -> bài nhẹ, giải trí
        elif core_low == "/auto night":
            # 20:00 tối: Gen Z dừng đọc lâu nhất -> MOFU hoặc BOFU, tầng nào đang thiếu hơn
            tier = max(("MOFU", "BOFU"), key=lambda t: targets[t] - counts[t] / total)
        if tier == "TOFU" and (counts["TOFU"] % 2 == 0):
            expanded = "/tofu 1 Threads theo nhánh POV Meme 1 câu cực ngắn mở đầu bằng 'Pov:'"
        else:
            expanded = f"/{tier.lower()} 1 Threads"
        note = f"{from_scheduler + ' -> ' if from_scheduler else ''}🎯 Tự động cân bằng phễu -> Chọn tầng **{tier}**..."
        await execute_and_publish(chat_id, expanded, lock, prefix_note=note, force_wait=has_wait)
        return

    if core_low == "/pov" or core_low.startswith("/pov ") or core_low.startswith("/tofu pov"):
        extra = re.sub(r"^(/pov|/tofu\s+pov)\s*", "", core_cmd, flags=re.I).strip()
        expanded = f"/tofu 1 Threads theo nhánh POV Meme 1 câu cực ngắn mở đầu bằng 'Pov:' {extra}".strip()
        await execute_and_publish(chat_id, expanded, lock, prefix_note=from_scheduler, force_wait=has_wait)
        return

    if core_low == "/video" or core_low.startswith("/video "):
        extra = re.sub(r"^/video\s*", "", core_cmd, flags=re.I).strip()
        expanded = f"/tofu 1 Threads theo nhánh POV Meme 1 câu cực ngắn mở đầu bằng 'Pov:' /usevideo {extra}".strip()
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
        pending: list[tuple[int, str]] = []
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
            # Báo NGAY đã nhận lệnh (kể cả khi bot vừa thức dậy ở phiên Cloud kế tiếp)
            short = text if len(text) <= 60 else text[:57] + "..."
            delay = max(0, int(time.time()) - int(msg.get("date") or time.time()))
            if delay > 90:
                send(chat_id, f"✅ Đã nhận lệnh: {short}\n⏳ Bạn gửi cách đây {delay // 60} phút, bot vừa thức dậy ở phiên mới nên xử lý trễ. Đang làm ngay...")
            else:
                send(chat_id, f"✅ Đã nhận lệnh: {short}\nĐang xử lý...")
            pending.append((chat_id, text))
        for chat_id, text in pending:
            await handle_command(chat_id, text, lock)


if __name__ == "__main__":
    asyncio.run(main())
