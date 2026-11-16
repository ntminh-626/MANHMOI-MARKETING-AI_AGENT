"""Đăng bài (Text + Ảnh từ assets/ẢNH MEME THREADS) lên Threads qua Threads API chính thức (graph.threads.net).

- Hỗ trợ tự động đính kèm ảnh từ thư mục `assets/ẢNH MEME THREADS` (xoay vòng không trùng ảnh gần nhất, hoặc chọn theo cảm xúc nội dung).
- Hỗ trợ tự động tách chuỗi bài (thread reply) nếu bài vượt quá 500 ký tự.
"""
import json
import os
import random
import re
import time
from pathlib import Path

import requests

BASE = "https://graph.threads.net/v1.0"
MAX_LEN = 500
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MEME_DIR = ROOT / "assets" / "ẢNH MEME THREADS"
IMAGE_STATE_FILE = HERE / "used_images.json"


def configured() -> bool:
    return bool(os.environ.get("THREADS_USER_ID") and os.environ.get("THREADS_ACCESS_TOKEN"))


def list_meme_images() -> list[Path]:
    """Lấy danh sách toàn bộ file ảnh trong assets/ẢNH MEME THREADS (và assets/ nếu có)."""
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    files: list[Path] = []
    if MEME_DIR.exists():
        files = [p for p in sorted(MEME_DIR.iterdir()) if p.is_file() and p.suffix.lower() in exts]
    if not files and (ROOT / "assets").exists():
        files = [p for p in sorted((ROOT / "assets").rglob("*")) if p.is_file() and p.suffix.lower() in exts]
    return files


def pick_meme_for_text(text: str, specific_name: str | None = None) -> Path | None:
    """Chọn 1 ảnh meme phù hợp với nội dung bài đăng, ưu tiên không lặp lại các ảnh vừa dùng."""
    files = list_meme_images()
    if not files:
        return None

    if specific_name:
        for f in files:
            if specific_name.lower() in f.name.lower():
                return f

    # Đọc lịch sử ảnh đã dùng để xoay vòng
    used: list[str] = []
    if IMAGE_STATE_FILE.exists():
        try:
            used = json.loads(IMAGE_STATE_FILE.read_text(encoding="utf-8")).get("used", [])
        except Exception:
            used = []

    available = [f for f in files if f.name not in used[-min(len(files) - 1, 5) :]] if len(files) > 1 else files

    # Gợi ý theo từ khóa cảm xúc trong bài
    t_low = text.lower()
    keyword_map = {
        "khoc": ["bad ending", "bắt nhầm", "tiếc", "ức chế", "nản", "cháy", "tiền", "ví", "nhớ lại", "tự thưởng", "hối hận"],
        "gian-doi": ["dối", "mâu thuẫn", "nghi ngờ", "đổ tội", "cãi", "check var", "thề", "hung thủ", "bạn thân"],
        "xin-loi": ["nhầm", "xin lỗi", "oan"],
        "hai-huoc": ["cà phê", "bạn bè", "cười", "tụ tập", "pov"],
        "bua": ["túi mù", "xé pack", "thám tử", "xoắn não"],
    }
    matched = []
    for key, words in keyword_map.items():
        if any(w in t_low for w in words):
            matched.extend([f for f in available if key in f.name.lower()])

    chosen = random.choice(matched) if matched else random.choice(available)

    used.append(chosen.name)
    try:
        IMAGE_STATE_FILE.write_text(json.dumps({"used": used[-20:]}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return chosen


def upload_public_image_url(image_path: Path) -> str:
    """Tải file ảnh local lên host tạm thời để lấy direct HTTPS URL cho Threads Graph API."""
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    headers = {"User-Agent": "Mozilla/5.0"}

    # 1. Thử uguu.se (direct image link)
    try:
        with image_path.open("rb") as f:
            r = requests.post(
                "https://uguu.se/upload",
                headers=headers,
                files={"files[]": (image_path.name, f, mime)},
                timeout=30,
            )
        data = r.json()
        if data.get("success") and data.get("files"):
            return data["files"][0]["url"]
    except Exception:
        pass

    # 2. Dự phòng: 0x0.st / tmpfiles direct link
    try:
        with image_path.open("rb") as f:
            r = requests.post(
                "https://tmpfiles.org/api/v1/upload",
                headers=headers,
                files={"file": (image_path.name, f, mime)},
                timeout=30,
            )
        data = r.json()
        url = data.get("data", {}).get("url", "")
        if url:
            return url.replace("http://", "https://").replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/")
    except Exception as e:
        raise RuntimeError(f"Không thể tạo public URL cho ảnh {image_path.name}: {e}")

    raise RuntimeError(f"Không thể upload ảnh {image_path.name}")


def split_into_chunks(text: str, limit: int = MAX_LEN) -> list[str]:
    """Tách văn bản dài thành các đoạn <= 500 ký tự theo đoạn văn hoặc câu trọn vẹn."""
    text = text.strip()
    if len(text) <= limit:
        return [text]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for p in paragraphs:
        candidate = f"{current}\n\n{p}".strip() if current else p
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
                current = ""
            if len(p) <= limit:
                current = p
            else:
                sentences = re.split(r"(?<=[.?!…])\s+", p)
                for s in sentences:
                    cand_s = f"{current} {s}".strip() if current else s
                    if len(cand_s) <= limit:
                        current = cand_s
                    else:
                        if current:
                            chunks.append(current)
                        current = s[:limit]
    if current:
        chunks.append(current)
    return chunks


def _publish_single(
    uid: str,
    token: str,
    text: str,
    reply_to_id: str | None = None,
    image_url: str | None = None,
) -> str:
    if image_url:
        payload = {
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": text,
            "access_token": token,
        }
    else:
        payload = {"media_type": "TEXT", "text": text, "access_token": token}

    if reply_to_id:
        payload["reply_to_id"] = reply_to_id

    r = requests.post(f"{BASE}/{uid}/threads", data=payload, timeout=30)
    data = r.json()
    if "id" not in data:
        raise RuntimeError(f"Tạo container lỗi: {data}")

    # Ảnh cần thời gian xử lý container trên server Meta lâu hơn text thuần (khoảng 5 giây)
    time.sleep(5 if image_url else 3)

    r = requests.post(
        f"{BASE}/{uid}/threads_publish",
        data={"creation_id": data["id"], "access_token": token},
        timeout=30,
    )
    pub = r.json()
    if "id" not in pub:
        raise RuntimeError(f"Publish lỗi: {pub}")
    return pub["id"]


def publish_text(text: str, with_meme: bool = True, specific_image: str | None = None) -> tuple[str, str | None]:
    """Đăng bài lên Threads kèm ảnh từ `assets/ẢNH MEME THREADS`. Trả về (post_id, tên_ảnh_đã_dùng)."""
    text = text.strip()
    if not text:
        raise ValueError("Bài trống")
    uid = os.environ["THREADS_USER_ID"]
    token = os.environ["THREADS_ACCESS_TOKEN"]

    image_url = None
    used_image_name = None
    if with_meme and os.environ.get("THREADS_ATTACH_MEME", "true").lower() in ("1", "true", "yes"):
        img_path = pick_meme_for_text(text, specific_image)
        if img_path:
            try:
                image_url = upload_public_image_url(img_path)
                used_image_name = img_path.name
            except Exception as e:
                print("Cảnh báo upload ảnh meme, chuyển sang đăng text:", e)

    chunks = split_into_chunks(text, MAX_LEN)
    root_id = None
    prev_id = None
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            time.sleep(3)
        try:
            prev_id = _publish_single(
                uid,
                token,
                chunk,
                reply_to_id=prev_id,
                image_url=image_url if idx == 0 else None,
            )
        except Exception:
            if idx == 0 and image_url:
                # Nếu host ảnh bị chặn đột xuất, tự động fallback đăng text để không rớt bài
                used_image_name = None
                prev_id = _publish_single(uid, token, chunk, reply_to_id=prev_id, image_url=None)
            else:
                raise
        if root_id is None:
            root_id = prev_id
    return (root_id or ""), used_image_name
