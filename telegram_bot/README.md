# Telegram bot cho Mánh Mối Agent

## Cài đặt (một lần)
1. Telegram: nhắn `@BotFather` → `/newbot` → lấy **token**.
2. Lấy Gemini API key tại https://aistudio.google.com/apikey.
3. `copy .env.example .env`, điền `TELEGRAM_BOT_TOKEN` và `GEMINI_API_KEY`.
4. `python -m pip install google-antigravity requests`
5. Chạy: `python telegram_bot\bot.py`
6. Nhắn `/id` cho bot, copy Chat ID vào `ALLOWED_CHAT_IDS` trong `.env`, chạy lại bot.

## Dùng
Nhắn trực tiếp: `/tofu 3`, `/mofu 2`, `/bofu 2`, `/init`, hoặc yêu cầu tự do.
Agent tự ghi `content-log.csv`.

## Lưu ý
- Máy chạy bot phải bật thì bot mới trả lời.
- Chỉ Chat ID trong danh sách mới ra lệnh được. Agent bị giới hạn trong thư mục dự án, không chạy lệnh shell.

## Threads (đăng bài)
Luồng: agent soạn bản nháp → bot gửi về Telegram kèm số → bạn gõ `/dang <số>` thì bài mới lên Threads. Không có bài nào tự đăng khi chưa được bạn duyệt.

Lấy `THREADS_USER_ID` và `THREADS_ACCESS_TOKEN`:
1. Vào https://developers.facebook.com → Create App → loại **Business** → thêm sản phẩm **Threads API**.
2. Quyền cần: `threads_basic`, `threads_content_publish`.
3. Mục Roles → thêm tài khoản Threads của bạn làm **Threads Tester**, rồi vào Threads → Settings → Account → Website permissions chấp nhận lời mời.
4. Ở Threads API → User Token Generator, tạo token cho tài khoản, copy **token** và **User ID**.
5. Token ngắn hạn sống khoảng 1 giờ. Đổi sang token dài hạn (60 ngày) bằng endpoint `th_exchange_token`, và gia hạn trước khi hết hạn.
6. Dán vào `.env`, chạy lại bot.

Giới hạn: bài tối đa 500 ký tự. Chỉ đăng text (chưa có ảnh).
