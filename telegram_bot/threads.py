"""Đăng bài (Text + 1 ảnh hoặc Carousel 2-3 ảnh từ assets/ẢNH MEME THREADS) lên Threads qua Threads API (graph.threads.net).

- Với bài POV (mở đầu bằng `Pov:` / `POV:` hoặc bài ngắn 1 câu): tự động chọn 2-3 ảnh liên quan tới nội dung và đăng dạng Carousel trượt ngang (giống hệt Viral Thread mẫu).
- Với bài thường: đính kèm 1 ảnh meme phù hợp (hoặc 2-3 ảnh nếu yêu cầu).
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


def is_pov_post(text: str) -> bool:
    """Nhận diện bài nhánh POV Meme (bắt đầu bằng Pov: hoặc chứa Pov: ở dòng đầu)."""
    t = text.strip().lower()
    return t.startswith("pov:") or t.startswith("pov ") or "\npov:" in t


def pick_memes_for_text(text: str, count: int = 1, specific_name: str | None = None) -> list[Path]:
    """Chọn `count` ảnh meme (1 hoặc 2-3 ảnh khác nhau) liên quan tới nội dung bài đăng."""
    files = list_meme_images()
    if not files:
        return []

    count = max(1, min(count, len(files)))

    if specific_name:
        for f in files:
            if specific_name.lower() in f.name.lower():
                return [f]

    used: list[str] = []
    if IMAGE_STATE_FILE.exists():
        try:
            used = json.loads(IMAGE_STATE_FILE.read_text(encoding="utf-8")).get("used", [])
        except Exception:
            used = []

    recent_cutoff = min(max(len(files) - count, 1), 4)
    available = [f for f in files if f.name not in used[-recent_cutoff:]]
    if len(available) < count:
        available = list(files)

    t_low = text.lower()
    keyword_map = {
        "khoc": ["bad ending", "bắt nhầm", "tiếc", "ức chế", "nản", "cháy", "tiền", "ví", "nhớ lại", "tự thưởng", "hối hận", "khóc"],
        "gian-doi": ["dối", "mâu thuẫn", "nghi ngờ", "đổ tội", "cãi", "check var", "thề", "hung thủ", "bạn thân", "lật mặt", "phản bội"],
        "xin-loi": ["nhầm", "xin lỗi", "oan", "lỡ"],
        "bieu-cam": ["nhìn", "ánh mắt", "khi", "pov", "điện thoại", "im lặng", "ngồi"],
        "hai-huoc": ["cà phê", "bạn bè", "cười", "tụ tập", "vui", "hài"],
        "bua": ["túi mù", "xé", "thám tử", "xoắn não", "hồ sơ", "lời khai"],
    }

    scored: list[tuple[int, float, Path]] = []
    for f in available:
        fname = f.name.lower()
        score = 0
        for key, words in keyword_map.items():
            if key in fname and any(w in t_low for w in words):
                score += 2
        if f.name not in used[-5:]:
            score += 1
        scored.append((score, random.random(), f))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    chosen = [item[2] for item in scored[:count]]

    for c in chosen:
        used.append(c.name)
    try:
        IMAGE_STATE_FILE.write_text(json.dumps({"used": used[-25:]}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return chosen


def pick_meme_for_text(text: str, specific_name: str | None = None) -> Path | None:
    res = pick_memes_for_text(text, count=1, specific_name=specific_name)
    return res[0] if res else None


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

    # 2. Dự phòng: tmpfiles direct link
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
    image_urls: list[str] | None = None,
) -> str:
    urls = [u for u in (image_urls or []) if u]

    if len(urls) >= 2:
        # Đăng dạng CAROUSEL (2-3 ảnh trượt ngang trên cùng 1 bài Threads)
        child_ids: list[str] = []
        for u in urls:
            r_item = requests.post(
                f"{BASE}/{uid}/threads",
                data={
                    "media_type": "IMAGE",
                    "image_url": u,
                    "is_carousel_item": "true",
                    "access_token": token,
                },
                timeout=30,
            )
            d_item = r_item.json()
            if "id" in d_item:
                child_ids.append(d_item["id"])
        if len(child_ids) >= 2:
            time.sleep(4)
            payload = {
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "text": text,
                "access_token": token,
            }
            if reply_to_id:
                payload["reply_to_id"] = reply_to_id
            r_car = requests.post(f"{BASE}/{uid}/threads", data=payload, timeout=30)
            d_car = r_car.json()
            if "id" not in d_car:
                raise RuntimeError(f"Tạo CAROUSEL container lỗi: {d_car}")
            time.sleep(5)
            r_pub = requests.post(
                f"{BASE}/{uid}/threads_publish",
                data={"creation_id": d_car["id"], "access_token": token},
                timeout=30,
            )
            pub = r_pub.json()
            if "id" not in pub:
                raise RuntimeError(f"Publish CAROUSEL lỗi: {pub}")
            return pub["id"]

    # Đăng 1 ảnh hoặc chỉ Text
    if len(urls) == 1:
        payload = {
            "media_type": "IMAGE",
            "image_url": urls[0],
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

    time.sleep(5 if urls else 3)

    r = requests.post(
        f"{BASE}/{uid}/threads_publish",
        data={"creation_id": data["id"], "access_token": token},
        timeout=30,
    )
    pub = r.json()
    if "id" not in pub:
        raise RuntimeError(f"Publish lỗi: {pub}")
    return pub["id"]


def publish_text(
    text: str,
    with_meme: bool = True,
    specific_image: str | None = None,
    image_count: int | None = None,
    preselected_images: list[Path] | None = None,
    first_reply: str | None = None,
) -> tuple[str, str | None]:
    """Đăng bài lên Threads kèm ảnh từ `assets/ẢNH MEME THREADS`.
    - Nếu truyền `preselected_images` (đã duyệt trước qua /wait trên Telegram), đăng chính xác các ảnh đó.
    - Nếu là bài POV (hoặc image_count >= 2): tự động chọn 2-3 ảnh liên quan và đăng dạng Carousel.
    Trả về (post_id, chuỗi_tên_ảnh_đã_dùng).
    """
    text = text.strip()
    if not text:
        raise ValueError("Bài trống")
    uid = os.environ["THREADS_USER_ID"]
    token = os.environ["THREADS_ACCESS_TOKEN"]

    image_urls: list[str] = []
    used_image_names: list[str] = []
    if with_meme and os.environ.get("THREADS_ATTACH_MEME", "true").lower() in ("1", "true", "yes"):
        if preselected_images is not None:
            img_paths = [p for p in preselected_images if p.exists()]
        else:
            if image_count is None:
                target_count = random.choice([2, 3]) if is_pov_post(text) else 1
            else:
                target_count = image_count
            img_paths = pick_memes_for_text(text, count=target_count, specific_name=specific_image)

        for img_path in img_paths:
            try:
                u = upload_public_image_url(img_path)
                image_urls.append(u)
                used_image_names.append(img_path.name)
            except Exception as e:
                print("Cảnh báo upload ảnh meme:", e)

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
                image_urls=image_urls if idx == 0 else None,
            )
        except Exception:
            if idx == 0 and image_urls:
                used_image_names = []
                prev_id = _publish_single(uid, token, chunk, reply_to_id=prev_id, image_urls=None)
            else:
                raise
        if root_id is None:
            root_id = prev_id
    # Bình luận mồi đầu tiên (First Self-Reply) để kích hoạt Reply Effect trong 60 phút đầu
    reply_text = (first_reply or "").strip()
    if reply_text and root_id:
        try:
            time.sleep(5)
            _publish_single(uid, token, reply_text[:MAX_LEN], reply_to_id=root_id, image_urls=None)
        except Exception as e:
            print("Cảnh báo: không đăng được bình luận mồi:", e)

    joined_names = ", ".join(used_image_names) if used_image_names else None
    return (root_id or ""), joined_names
