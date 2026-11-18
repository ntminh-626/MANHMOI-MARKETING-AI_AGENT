// Cloudflare Worker (gói miễn phí) - cầu nối Telegram <-> GitHub Actions.
// 1) Telegram webhook: nhận tin nhắn -> trả lời "Đã nhận lệnh" NGAY -> gọi GitHub Actions xử lý lệnh.
// 2) Cron Trigger (mỗi 5 phút): nếu có bài hẹn giờ đến hạn -> gọi GitHub Actions chạy lịch.
//
// Biến cần đặt (Settings -> Variables and secrets):
//   TELEGRAM_BOT_TOKEN (secret), GH_TOKEN (secret), WEBHOOK_SECRET (secret),
//   ALLOWED_CHAT_IDS (text, cách nhau bằng dấu phẩy), GH_REPO (text, vd ntminh-626/MANHMOI-MARKETING-AI_AGENT)
//   GH_BRANCH (text, tuỳ chọn, mặc định main), GH_WORKFLOW (tuỳ chọn, mặc định manh-moi-event.yml)

const TZ_OFFSET_MIN = 7 * 60; // Giờ Việt Nam

async function tgSend(env, chatId, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}

async function dispatch(env, inputs) {
  const wf = env.GH_WORKFLOW || "manh-moi-event.yml";
  const res = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${wf}/dispatches`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.GH_TOKEN}`,
      accept: "application/vnd.github+json",
      "user-agent": "manh-moi-worker",
      "x-github-api-version": "2022-11-28",
    },
    body: JSON.stringify({ ref: env.GH_BRANCH || "main", inputs }),
  });
  if (!res.ok) throw new Error(`GitHub dispatch ${res.status}: ${await res.text()}`);
}

function allowedSet(env) {
  return new Set((env.ALLOWED_CHAT_IDS || "").match(/-?\d+/g) || []);
}

async function handleUpdate(env, update) {
  const msg = update.message;
  const text = (msg?.text || "").trim();
  const chatId = msg?.chat?.id;
  if (!text || chatId === undefined) return;

  if (/^\/(id|start)\b/.test(text)) {
    await tgSend(env, chatId, `Chat ID của bạn: ${chatId}\n(Dán số này vào ALLOWED_CHAT_IDS)`);
    return;
  }
  if (!allowedSet(env).has(String(chatId))) {
    await tgSend(env, chatId, "Không có quyền.");
    return;
  }
  const short = text.length > 60 ? text.slice(0, 57) + "..." : text;
  await tgSend(env, chatId, `✅ Đã nhận lệnh: ${short}\nĐang xử lý, khoảng 1-2 phút sẽ có kết quả...`);
  try {
    await dispatch(env, { kind: "cmd", chat_id: String(chatId), text });
  } catch (e) {
    await tgSend(env, chatId, `⚠️ Không gọi được GitHub Actions: ${String(e).slice(0, 300)}`);
  }
}

function vnNow() {
  return new Date(Date.now() + TZ_OFFSET_MIN * 60000);
}
const pad = (n) => String(n).padStart(2, "0");

async function checkSchedule(env) {
  const branch = env.GH_BRANCH || "main";
  const url = `https://raw.githubusercontent.com/${env.GH_REPO}/${branch}/telegram_bot/schedule.json?t=${Date.now()}`;
  const res = await fetch(url, { headers: { "cache-control": "no-cache" } });
  if (!res.ok) return;
  const sched = await res.json();
  const now = vnNow();
  const today = `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-${pad(now.getUTCDate())}`;
  const nowFull = `${today} ${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}`;

  let due = (sched.one_off || []).some((x) => (x.run_at || "") <= nowFull);
  if (!due && sched.enabled !== false) {
    const last = sched.last_daily_runs || {};
    for (const hm of sched.daily_times || []) {
      const [h, m] = hm.split(":").map(Number);
      const late = now.getUTCHours() * 60 + now.getUTCMinutes() - (h * 60 + m);
      if (late >= 0 && late <= 90 && last[hm] !== today) {
        due = true;
        break;
      }
    }
  }
  if (due) await dispatch(env, { kind: "tick", chat_id: "", text: "" });
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("Manh Moi bot OK");
    if (request.headers.get("x-telegram-bot-api-secret-token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }
    ctx.waitUntil(handleUpdate(env, update));
    return new Response("ok"); // trả lời Telegram ngay để không bị gửi lại
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(checkSchedule(env).catch((e) => console.log("schedule error", String(e))));
  },
};