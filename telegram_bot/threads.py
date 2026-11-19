"""Đăng bài (Text + 1 ảnh hoặc Carousel 2-3 ảnh từ assets/ẢNH MEME THREADS) lên Threads qua Threads API (graph.threads.net).

- Với bài POV (mở đầu bằng `Pov:` / `POV:` hoặc bài ngắn 1 câu): tự động chọn 1-3 ảnh (phân bổ 40/40/20) liên quan tới nội dung và đăng dạng Carousel trượt ngang (giống hệt Viral Thread mẫu).
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
VIDEO_DIR = ROOT / "assets" / "VIDEO MEME THREADS"
VIDEO_EXTS = {".mp4", ".mov"}


def pick_topic_tag(text: str) -> str | None:
    """Chọn 1 topic tag (Threads chỉ cho 1 tag/bài) khớp nội dung nhất; ưu tiên tag đang hot, tránh lặp tag vừa dùng."""
    if os.environ.get("THREADS_TOPIC_TAG", "false").lower() not in ("1", "true", "yes"):
        return None
    try:
        cfg = json.loads((HERE / "topics.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    fixed = str(cfg.get("fixed_tag", "")).strip()
    if fixed:
        return fixed[:50]  # luôn dùng 1 tag cố định (đặt "fixed_tag" = "" trong topics.json để quay lại tự chọn theo nội dung)
    pool = cfg.get("topic_tags", [])
    hot = {h.lower() for h in cfg.get("hot_tags", [])}
    t = text.lower()
    state_f = HERE / "topic_state.json"
    try:
        state = json.loads(state_f.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    recent = state.get("used_tags", [])[-4:]
    scored = []
    for item in pool:
        tag = str(item.get("tag", "")).strip()
        if not tag or "." in tag or "&" in tag:
            continue
        sc = sum(1 for k in item.get("keywords", []) if k.lower() in t)
        if sc <= 0:
            continue
        if tag.lower() in hot:
            sc += 1
        if tag in recent:
            sc -= 2
        scored.append((sc, random.random(), tag))
    if not scored:
        fallback = [h for h in cfg.get("hot_tags", []) if h not in recent]
        tag = random.choice(fallback) if fallback else None
    else:
        scored.sort(reverse=True)
        tag = scored[0][2]
    if tag:
        try:
            state["used_tags"] = (state.get("used_tags", []) + [tag])[-12:]
            state_f.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return tag[:50] if tag else None


def is_video_name(name: str) -> bool:
    return Path(str(name).split("?")[0]).suffix.lower() in VIDEO_EXTS


def list_meme_videos() -> list[Path]:
    """Lấy danh sách video (.mp4/.mov) trong assets/VIDEO MEME THREADS."""
    if not VIDEO_DIR.exists():
        return []
    return [p for p in sorted(VIDEO_DIR.rglob("*")) if p.is_file() and p.suffix.lower() in VIDEO_EXTS]


def list_all_media() -> list[Path]:
    return list_meme_images() + list_meme_videos()


def pick_video_for_text(text: str = "", avoid: list[str] | None = None) -> Path | None:
    """Chọn ngẫu nhiên 1 video, tránh video vừa dùng / video đang nằm trong nháp (avoid)."""
    vids = list_meme_videos()
    if not vids:
        return None
    used: list[str] = []
    if IMAGE_STATE_FILE.exists():
        try:
            used = json.loads(IMAGE_STATE_FILE.read_text(encoding="utf-8")).get("used_videos", [])
        except Exception:
            used = []
    blocked = set(avoid or []) | set(used[-max(len(vids) - 1, 0):][-3:])
    pool = [v for v in vids if v.name not in blocked] or vids
    chosen = random.choice(pool)
    try:
        data = {}
        if IMAGE_STATE_FILE.exists():
            data = json.loads(IMAGE_STATE_FILE.read_text(encoding="utf-8"))
        data["used_videos"] = (used + [chosen.name])[-10:]
        IMAGE_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return chosen


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


def pick_pov_image_count() -> int:
    """Phân bổ số ảnh cho bài POV: 1 ảnh 40% / 2 ảnh 40% / 3 ảnh 20%."""
    return random.choices([1, 2, 3], weights=[40, 40, 20], k=1)[0]


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
        _st = {}
        try:
            _st = json.loads(IMAGE_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _st = {}
        _st["used"] = used[-25:]
        IMAGE_STATE_FILE.write_text(json.dumps(_st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return chosen


def pick_meme_for_text(text: str, specific_name: str | None = None) -> Path | None:
    res = pick_memes_for_text(text, count=1, specific_name=specific_name)
    return res[0] if res else None


def upload_public_image_url(image_path: Path) -> str:
    """Tải file ảnh local lên host tạm thời để lấy direct HTTPS URL cho Threads Graph API."""
    ext = image_path.suffix.lower()
    mime = {".png": "image/png", ".mp4": "video/mp4", ".mov": "video/quicktime", ".webp": "image/webp"}.get(ext, "image/jpeg")
    tmo = 180 if ext in VIDEO_EXTS else 30
    headers = {"User-Agent": "Mozilla/5.0"}

    # 0. Chạy trên GitHub Actions (repo public): dùng thẳng link raw.githubusercontent.com (miễn phí, ổn định)
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        try:
            from urllib.parse import quote
            rel = image_path.resolve().relative_to(Path(__file__).resolve().parent.parent).as_posix()
            branch = os.environ.get("GITHUB_REF_NAME", "main")
            url = f"https://raw.githubusercontent.com/{repo}/{branch}/{quote(rel)}"
            if requests.head(url, headers=headers, timeout=15, allow_redirects=True).status_code == 200:
                return url
        except Exception:
            pass

    # 1. Thử uguu.se (direct image link)
    try:
        with image_path.open("rb") as f:
            r = requests.post(
                "https://uguu.se/upload",
                headers=headers,
                files={"files[]": (image_path.name, f, mime)},
                timeout=tmo,
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
                timeout=tmo,
            )
        data = r.json()
        url = data.get("data", {}).get("url", "")
        if url:
            return url.replace("http://", "https://").replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/")
    except Exception as e:
        raise RuntimeError(f"Không thể tạo public URL cho ảnh {image_path.name}: {e}")

    raise RuntimeError(f"Không thể upload ảnh {image_path.name}")


upload_public_media_url = upload_public_image_url


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


def _is_video_url(u: str) -> bool:
    return Path(u.split("?")[0]).suffix.lower() in VIDEO_EXTS


def _wait_container_ready(token: str, container_id: str, max_wait: int = 300) -> None:
    """Video cần Threads xử lý xong (status FINISHED) trước khi publish."""
    start = time.time()
    last = None
    while time.time() - start < max_wait:
        try:
            r = requests.get(
                f"{BASE}/{container_id}",
                params={"fields": "status,error_message", "access_token": token},
                timeout=30,
            ).json()
        except Exception as e:
            last = str(e)
            time.sleep(5)
            continue
        st = r.get("status")
        if st in ("FINISHED", "PUBLISHED"):
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Threads xử lý media lỗi: {r}")
        last = r
        time.sleep(6)
    raise RuntimeError(f"Quá thời gian chờ Threads xử lý video: {last}")


def _create_container(uid: str, token: str, payload: dict, what: str) -> str:
    payload = dict(payload, access_token=token)
    d = requests.post(f"{BASE}/{uid}/threads", data=payload, timeout=60).json()
    if "id" not in d:
        raise RuntimeError(f"Tạo {what} container lỗi: {d}")
    return d["id"]


def _publish_container(uid: str, token: str, creation_id: str, what: str) -> str:
    pub = requests.post(
        f"{BASE}/{uid}/threads_publish",
        data={"creation_id": creation_id, "access_token": token},
        timeout=60,
    ).json()
    if "id" not in pub:
        raise RuntimeError(f"Publish {what} lỗi: {pub}")
    return pub["id"]


def _publish_single(
    uid: str,
    token: str,
    text: str,
    reply_to_id: str | None = None,
    image_urls: list[str] | None = None,
    topic_tag: str | None = None,
) -> str:
    """Đăng 1 bài: Text / 1 ảnh / 1 video / Carousel 2+ media (ảnh và video trộn được)."""
    urls = [u for u in (image_urls or []) if u]

    if len(urls) >= 2:
        child_ids: list[str] = []
        for u in urls:
            vid = _is_video_url(u)
            payload = {"media_type": "VIDEO" if vid else "IMAGE", "is_carousel_item": "true"}
            payload["video_url" if vid else "image_url"] = u
            cid = _create_container(uid, token, payload, "carousel item")
            if vid:
                _wait_container_ready(token, cid)
            child_ids.append(cid)
        time.sleep(4)
        payload = {"media_type": "CAROUSEL", "children": ",".join(child_ids), "text": text}
        if topic_tag:
            payload["topic_tag"] = topic_tag
        if reply_to_id:
            payload["reply_to_id"] = reply_to_id
        car = _create_container(uid, token, payload, "CAROUSEL")
        time.sleep(5)
        return _publish_container(uid, token, car, "CAROUSEL")

    if len(urls) == 1 and _is_video_url(urls[0]):
        payload = {"media_type": "VIDEO", "video_url": urls[0], "text": text}
    elif len(urls) == 1:
        payload = {"media_type": "IMAGE", "image_url": urls[0], "text": text}
    else:
        payload = {"media_type": "TEXT", "text": text}
    if reply_to_id:
        payload["reply_to_id"] = reply_to_id
    if topic_tag:
        payload["topic_tag"] = topic_tag

    cid = _create_container(uid, token, payload, payload["media_type"])
    if payload["media_type"] == "VIDEO":
        _wait_container_ready(token, cid)
    else:
        time.sleep(5 if urls else 3)
    return _publish_container(uid, token, cid, payload["media_type"])


def publish_text(
    text: str,
    with_meme: bool = True,
    specific_image: str | None = None,
    image_count: int | None = None,
    preselected_images: list[Path] | None = None,
    first_reply: str | None = None,
    topic_tag: str | None = None,
) -> tuple[str, str | None]:
    """Đăng bài lên Threads kèm ảnh từ `assets/ẢNH MEME THREADS`.
    - Nếu truyền `preselected_images` (đã duyệt trước qua /wait trên Telegram), đăng chính xác các ảnh đó.
    - Nếu là bài POV (hoặc image_count >= 2): tự động chọn 1-3 ảnh (phân bổ 40/40/20) liên quan và đăng dạng Carousel.
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
                target_count = pick_pov_image_count() if is_pov_post(text) else 1
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

    if topic_tag is None:
        topic_tag = pick_topic_tag(text)
    chunks = split_into_chunks(text, MAX_LEN)
    root_id = None
    prev_id = None
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            time.sleep(3)
        if idx == 0:
            attempts = [(image_urls, topic_tag)]
            if topic_tag:
                attempts.append((image_urls, None))
            if image_urls:
                attempts.append((None, None))
        else:
            attempts = [(None, None)]
        last_err: Exception | None = None
        for k, (urls_try, tag_try) in enumerate(attempts):
            try:
                prev_id = _publish_single(
                    uid, token, chunk, reply_to_id=prev_id, image_urls=urls_try, topic_tag=tag_try
                )
                if idx == 0 and image_urls and not urls_try:
                    used_image_names = []
                if idx == 0 and topic_tag and not tag_try:
                    topic_tag = None
                last_err = None
                break
            except Exception as e:
                last_err = e
        if last_err is not None:
            raise last_err
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
