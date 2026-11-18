# Bot Telegram phản hồi trong vài giây - miễn phí (Cloudflare Worker + GitHub Actions)

Luồng: bạn nhắn Telegram -> Cloudflare Worker trả lời "Đã nhận lệnh" ngay -> gọi GitHub Actions xử lý lệnh (~1-2 phút) -> bot gửi kết quả về Telegram.
Hẹn giờ đăng (12:00 / 20:00 hoặc `/hen`): Worker kiểm tra mỗi 5 phút và gọi Actions khi đến giờ.

## Làm một lần (toàn bộ trên điện thoại/trình duyệt, không cần cài gì)

### 1. Tạo GitHub token (cho Worker gọi Actions)
GitHub -> Settings -> Developer settings -> Personal access tokens -> **Fine-grained tokens** -> Generate new token
- Repository access: **Only select repositories** -> chọn repo này
- Permissions -> Repository permissions -> **Actions: Read and write**
- Copy token (chỉ hiện 1 lần).

### 2. Tạo Worker trên Cloudflare (đăng ký miễn phí, không cần thẻ)
1. dash.cloudflare.com -> **Workers & Pages** -> Create -> Create Worker -> Deploy (tên bất kỳ, vd `manhmoi-bot`).
2. Bấm **Edit code**, xoá code mẫu, dán toàn bộ file `cloudflare/worker.js`, bấm **Deploy**.
3. Settings -> **Variables and secrets** -> thêm:
   | Tên | Loại | Giá trị |
   |---|---|---|
   | `TELEGRAM_BOT_TOKEN` | Secret | token bot Telegram |
   | `GH_TOKEN` | Secret | token ở bước 1 |
   | `WEBHOOK_SECRET` | Secret | một chuỗi ngẫu nhiên bất kỳ (chữ + số, vd `mm7Xk29QpLz41`) |
   | `ALLOWED_CHAT_IDS` | Text | Chat ID của bạn |
   | `GH_REPO` | Text | `ntminh-626/MANHMOI-MARKETING-AI_AGENT` |
4. Settings -> **Trigger Events** (Triggers) -> **Cron Triggers** -> Add -> `*/5 * * * *`.
5. Copy địa chỉ Worker (dạng `https://manhmoi-bot.<tên>.workers.dev`).

### 3. Nối Telegram với Worker
Mở trình duyệt, dán (thay 3 chỗ trong `<>`):
```
https://api.telegram.org/bot<TOKEN_BOT>/setWebhook?url=<ĐỊA_CHỈ_WORKER>&secret_token=<WEBHOOK_SECRET>
```
Thấy `"ok":true` là xong.

### 4. Dọn bên cũ
- **Tắt bot chạy trên máy** (webhook và `python bot.py` không chạy cùng lúc được).
- Repo -> Settings -> Secrets: giữ nguyên `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS`, `OPENROUTER_API_KEY`, `THREADS_USER_ID`, `THREADS_ACCESS_TOKEN`.

### 5. Thử
Nhắn `/status` cho bot: nhận "Đã nhận lệnh" ngay, vài chục giây sau có kết quả.

## Quay lại chạy bot trên máy
Mở `https://api.telegram.org/bot<TOKEN_BOT>/deleteWebhook` rồi chạy `python telegram_bot\bot.py`.

## Giới hạn miễn phí
- Cloudflare Workers Free: 100.000 yêu cầu/ngày, đủ dùng cả đời cho bot cá nhân.
- GitHub Actions: repo public không giới hạn phút.
- Nếu hai lệnh gửi sát nhau (vài giây), hai phiên chạy song song; trạng thái được gộp tự động nhưng hiếm khi có thể lệch số thứ tự nháp.