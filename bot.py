"""
Telegram payment bot — single file Python version (media + MINI APP dashboard edition).

What changed vs your old file
-----------------------------
  * /admin now sends a "🚀 LAUNCH DASHBOARD" web_app button that opens the
    Aether Control mini app right inside Telegram (like in your video).
  * A small local HTTP API server runs in a background thread so the mini app
    can read stats/plans/payments and write changes back into bot.db.
  * Every API request is verified with Telegram's initData HMAC signature, so
    only YOUR admin account can use the panel.
  * The old inline-button dashboard is still there as a fallback.

Setup
-----
  pip install requests
  Set the three values in DEFAULTS below (or via /admin -> old menu):
      bot_token       -> from @BotFather
      admin_chat_id   -> your Telegram user id
      webapp_url      -> your published Lovable app URL
      api_public_url  -> the public https URL that points at THIS script's API
                         (e.g. ngrok:  ngrok http 8099  -> https://xxx.ngrok-free.app)
  python bot.py

Because the bot runs on your PC, Telegram needs an https URL to reach the API.
Easiest: install ngrok, run `ngrok http 8099`, and paste the https URL into
api_public_url below.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")
API = "https://api.telegram.org"

DEFAULTS = {
    "bot_token": "8959356690:AAFbJp2MCgqAzeFESxFuyopl7c026cVcROk",
    "admin_chat_id": "7431786238",
    # ---- mini app ----
    "webapp_url": "",        # e.g. https://your-app.lovable.app
    "api_public_url": "",    # e.g. https://xxxx.ngrok-free.app
    "api_port": "8099",
    # ---- messages ----
    "welcome_text": "Welcome! Choose an option below.",
    "welcome_photo": "",
    "qr_photo": "",
    "access_link": "",
    "submitted_text": "✅ Request Submitted!\n\n🆔 Order #{order}\n⏳ Your plan will be activated after verification.\nYou'll receive a notification once approved.",
    "submitted_photo": "",
    "approved_text": "✅ Payment Approved!\n\n🆔 Order #{order} — Plan: {plan}\nHere is your access link:",
    "approved_photo": "",
    "declined_text": "❌ Payment Not Verified\n\n🆔 Order #{order} — Plan: {plan}\nYour payment could not be verified.\n\nPlease contact support if you believe this is an error.",
    "declined_photo": "",
    "howto_text": "📘 How to use\n\nWatch the video above, pick your plan, pay on the QR, then tap \"✅ I have paid\" and send the payment screenshot here.",
    "report_text": "🚨 Report an Issue\n\nPlease describe your issue or send a screenshot.\nWe'll get back to you as soon as possible.",
}

PHOTO_KEYS = ("welcome_photo", "qr_photo", "submitted_photo", "approved_photo", "declined_photo")
MEDIA_LIMIT = 10


def render(template, order="", plan=""):
    return (template or "").replace("{order}", str(order)).replace("{plan}", str(plan))


def notify(chat_id, text_key, order="", plan="", extra=""):
    body = render(get(text_key), order, plan) + (("\n" + extra) if extra else "")
    photo = get(text_key.replace("_text", "_photo"))
    if photo:
        return send_photo(chat_id, photo, body)
    return send(chat_id, body)


# --------------------------------------------------------------------------- db
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS plans (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   label TEXT NOT NULL,
                   price REAL NOT NULL,
                   reply_text TEXT NOT NULL DEFAULT '',
                   qr_photo TEXT NOT NULL DEFAULT '',
                   position INTEGER NOT NULL DEFAULT 0,
                   active INTEGER NOT NULL DEFAULT 1)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS payments (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   chat_id TEXT NOT NULL,
                   username TEXT NOT NULL DEFAULT '',
                   full_name TEXT NOT NULL DEFAULT '',
                   plan_label TEXT NOT NULL DEFAULT '',
                   price REAL NOT NULL DEFAULT 0,
                   photo_file_id TEXT NOT NULL DEFAULT '',
                   status TEXT NOT NULL DEFAULT 'selected',
                   created_at REAL NOT NULL DEFAULT 0)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS media (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   scope TEXT NOT NULL,
                   kind TEXT NOT NULL,
                   file_id TEXT NOT NULL,
                   position INTEGER NOT NULL DEFAULT 0)"""
        )
        c.execute("CREATE TABLE IF NOT EXISTS state (chat_id TEXT PRIMARY KEY, step TEXT NOT NULL)")
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        if not c.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"]:
            c.executemany(
                "INSERT INTO plans (label, price, reply_text, position) VALUES (?, ?, ?, ?)",
                [
                    ("Basic Plan", 49, "Pay ₹49 on the QR above and send the payment screenshot here.", 1),
                    ("Premium Plan", 99, "Pay ₹99 on the QR above and send the payment screenshot here.", 2),
                ],
            )


def get(key):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS.get(key, "")


def put(key, value):
    with db() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def plans(active_only=True):
    q = "SELECT * FROM plans" + (" WHERE active = 1" if active_only else "") + " ORDER BY position, id"
    with db() as c:
        return [dict(r) for r in c.execute(q).fetchall()]


def media_list(scope):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM media WHERE scope = ? ORDER BY position, id", (scope,)
        ).fetchall()]


def media_add(scope, kind, file_id):
    with db() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM media WHERE scope = ?", (scope,)).fetchone()["n"]
        if n >= MEDIA_LIMIT:
            return False
        c.execute("INSERT INTO media (scope, kind, file_id, position) VALUES (?, ?, ?, ?)",
                  (scope, kind, file_id, n + 1))
    return True


def media_clear(scope):
    with db() as c:
        c.execute("DELETE FROM media WHERE scope = ?", (scope,))


def set_step(chat_id, step):
    with db() as c:
        if step:
            c.execute("INSERT INTO state (chat_id, step) VALUES (?, ?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET step = excluded.step",
                      (str(chat_id), step))
        else:
            c.execute("DELETE FROM state WHERE chat_id = ?", (str(chat_id),))


def get_step(chat_id):
    with db() as c:
        row = c.execute("SELECT step FROM state WHERE chat_id = ?", (str(chat_id),)).fetchone()
    return row["step"] if row else ""


def stats():
    day_start = time.time() - 86400
    with db() as c:
        rows = c.execute("SELECT status, COUNT(*) AS n, COALESCE(SUM(price), 0) AS total "
                         "FROM payments GROUP BY status").fetchall()
        users = c.execute("SELECT COUNT(DISTINCT chat_id) AS n FROM payments").fetchone()["n"]
        today_users = c.execute("SELECT COUNT(DISTINCT chat_id) AS n FROM payments "
                                "WHERE created_at >= ?", (day_start,)).fetchone()["n"]
        today_rev = c.execute("SELECT COALESCE(SUM(price), 0) AS t FROM payments "
                              "WHERE status = 'approved' AND created_at >= ?",
                              (day_start,)).fetchone()["t"]
    by = {r["status"]: {"n": r["n"], "total": r["total"]} for r in rows}
    return {
        "users": users,
        "today_users": today_users,
        "pending": by.get("pending", {}).get("n", 0),
        "approved": by.get("approved", {}).get("n", 0),
        "declined": by.get("declined", {}).get("n", 0),
        "selected": by.get("selected", {}).get("n", 0),
        "revenue": by.get("approved", {}).get("total", 0),
        "today_revenue": today_rev,
    }


def all_chat_ids():
    with db() as c:
        return [r["chat_id"] for r in c.execute("SELECT DISTINCT chat_id FROM payments").fetchall()]


# ---------------------------------------------------------------------- telegram
def call(method, **payload):
    token = get("bot_token")
    res = requests.post(f"{API}/bot{token}/{method}", json=payload, timeout=90)
    data = res.json()
    if not data.get("ok"):
        print(f"[telegram] {method} failed: {data.get('description')}")
    return data


def send(chat_id, text, keyboard=None):
    args = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        args["reply_markup"] = keyboard
    return call("sendMessage", **args)


def send_photo(chat_id, file_id, caption="", keyboard=None):
    if not file_id:
        return send(chat_id, caption, keyboard) if caption else None
    args = {"chat_id": chat_id, "photo": file_id, "caption": caption[:1024], "parse_mode": "HTML"}
    if keyboard:
        args["reply_markup"] = keyboard
    return call("sendPhoto", **args)


def send_media(chat_id, items, caption=""):
    items = items[:MEDIA_LIMIT]
    if not items:
        return None
    if len(items) == 1:
        it = items[0]
        method = "sendPhoto" if it["kind"] == "photo" else "sendVideo"
        args = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
        args["photo" if it["kind"] == "photo" else "video"] = it["file_id"]
        return call(method, **args)
    group = []
    for i, it in enumerate(items):
        entry = {"type": it["kind"], "media": it["file_id"]}
        if i == 0 and caption:
            entry["caption"] = caption[:1024]
            entry["parse_mode"] = "HTML"
        group.append(entry)
    return call("sendMediaGroup", chat_id=chat_id, media=group)


def welcome_media():
    items = media_list("welcome")
    if not items and get("welcome_photo"):
        items = [{"kind": "photo", "file_id": get("welcome_photo")}]
    return items


# ------------------------------------------------------------------- keyboards
def start_keyboard():
    rows = [[{"text": f"{p['label']} — ₹{int(p['price'])}", "callback_data": f"plan:{p['id']}"}]
            for p in plans()]
    rows.append([
        {"text": "📘 How to use", "callback_data": "howto"},
        {"text": "🚨 Report an Issue", "callback_data": "report"},
    ])
    return {"inline_keyboard": rows}


def pay_keyboard(pid):
    return {"inline_keyboard": [
        [{"text": "✅ I have paid", "callback_data": f"paid:{pid}"}],
        [{"text": "❌ Cancel", "callback_data": "cancel"}],
    ]}


def report_keyboard():
    return {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "report_cancel"}]]}


def review_keyboard(payment_id):
    return {"inline_keyboard": [[
        {"text": "✅ APPROVE", "callback_data": f"pay_ok:{payment_id}"},
        {"text": "❌ REJECT", "callback_data": f"pay_no:{payment_id}"},
    ]]}


def webapp_full_url():
    """Mini app URL with the bot's public API address appended."""
    base = (get("webapp_url") or "").rstrip("/")
    api_url = (get("api_public_url") or "").rstrip("/")
    if not base:
        return ""
    if api_url:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}api={urllib.parse.quote(api_url, safe='')}"
    return base


def dashboard_keyboard():
    """Mini app launcher + the classic menu as a fallback."""
    rows = []
    url = webapp_full_url()
    if url.startswith("https://"):
        rows.append([{"text": "🚀 LAUNCH DASHBOARD", "web_app": {"url": url}}])
    rows += [
        [{"text": "🧾 Review payments", "callback_data": "pay:list"},
         {"text": "🔄 Refresh", "callback_data": "dash"}],
        [{"text": "💰 Plans & QR", "callback_data": "plans:list"},
         {"text": "🎞 Welcome media", "callback_data": "media:welcome"}],
        [{"text": "💬 Welcome text", "callback_data": "set:welcome_text"},
         {"text": "🔗 Access link", "callback_data": "set:access_link"}],
        [{"text": "📘 How-to video", "callback_data": "media:howto"},
         {"text": "📷 QR (all plans)", "callback_data": "set:qr_photo"}],
        [{"text": "🌐 Mini app URL", "callback_data": "set:webapp_url"},
         {"text": "🔌 API URL", "callback_data": "set:api_public_url"}],
        [{"text": "📢 Broadcast", "callback_data": "bcast"}],
    ]
    return {"inline_keyboard": rows}


def admin_intro_text():
    s = stats()
    lines = [
        "⚡ <b>AETHER CONTROL</b> ⚡",
        "",
        f"🌶 Total Users: <b>{s['users']}</b>",
        f"💰 Revenue: <b>₹{int(s['revenue'])}</b>",
        f"🧾 Pending: <b>{s['pending']}</b>",
        "",
        "<i>Tap the button below to open the Dark Mode Web Dashboard!</i>",
    ]
    if not webapp_full_url().startswith("https://"):
        lines += ["", "⚠️ Set <b>Mini app URL</b> and <b>API URL</b> first (buttons below)."]
    return "\n".join(lines)


def media_keyboard(scope):
    items = media_list(scope)
    return {"inline_keyboard": [
        [{"text": f"➕ Add photo/video ({len(items)}/{MEDIA_LIMIT})", "callback_data": f"madd:{scope}"}],
        [{"text": "🗑 Remove all", "callback_data": f"mclr:{scope}"}],
        [{"text": "⬅️ Dashboard", "callback_data": "dash"}],
    ]}


def plans_keyboard():
    rows = [[{"text": f"{p['label']} — ₹{int(p['price'])}", "callback_data": f"pedit:{p['id']}"}]
            for p in plans(active_only=False)]
    rows.append([{"text": "➕ Add plan", "callback_data": "pnew"}])
    rows.append([{"text": "⬅️ Dashboard", "callback_data": "dash"}])
    return {"inline_keyboard": rows}


def plan_keyboard(pid):
    return {"inline_keyboard": [
        [{"text": "✏️ Label", "callback_data": f"pset:label:{pid}"},
         {"text": "💵 Price", "callback_data": f"pset:price:{pid}"}],
        [{"text": "📝 Reply text", "callback_data": f"pset:reply_text:{pid}"},
         {"text": "📷 QR (only this plan)", "callback_data": f"pset:qr_photo:{pid}"}],
        [{"text": "🎞 Plan videos", "callback_data": f"media:plan:{pid}"}],
        [{"text": "🗑 Delete plan", "callback_data": f"pdel:{pid}"}],
        [{"text": "⬅️ Plans", "callback_data": "plans:list"}],
    ]}


def is_admin(chat_id):
    return str(chat_id) == str(get("admin_chat_id"))


def extract_media(msg):
    if msg.get("photo"):
        return "photo", msg["photo"][-1]["file_id"]
    if msg.get("video"):
        return "video", msg["video"]["file_id"]
    if msg.get("animation"):
        return "video", msg["animation"]["file_id"]
    doc = msg.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("video/"):
        return "video", doc["file_id"]
    if str(doc.get("mime_type", "")).startswith("image/"):
        return "photo", doc["file_id"]
    return None, ""


# ------------------------------------------------------------------- decisioning
def decide(payment_id, approve):
    with db() as c:
        row = c.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if not row:
            return "Payment not found"
        if row["status"] != "pending":
            return f"Already {row['status']}"
        c.execute("UPDATE payments SET status = ? WHERE id = ?",
                  ("approved" if approve else "declined", payment_id))
    if approve:
        link = get("access_link")
        notify(row["chat_id"], "approved_text", row["id"], row["plan_label"],
               extra=link or "(link not set yet)")
    else:
        notify(row["chat_id"], "declined_text", row["id"], row["plan_label"])
    return "Approved & link sent" if approve else "Declined"


def broadcast_all(text, file_id="", kind=""):
    sent = failed = 0
    for cid in all_chat_ids():
        try:
            if file_id and kind == "photo":
                ok = send_photo(cid, file_id, text)
            elif file_id:
                ok = call("sendVideo", chat_id=cid, video=file_id,
                          caption=text[:1024], parse_mode="HTML")
            else:
                ok = send(cid, text)
            if ok and ok.get("ok"):
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.05)
    return sent, failed


# ==========================================================================
#                        MINI APP  ·  LOCAL HTTP API
# ==========================================================================
def check_init_data(init_data):
    """Verify Telegram WebApp initData and return the admin's user id, or None."""
    token = get("bot_token")
    if not init_data or not token:
        return None
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", "")
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        user = json.loads(pairs.get("user", "{}"))
        uid = str(user.get("id", ""))
        return uid if is_admin(uid) else None
    except Exception:
        return None


def api_state():
    scopes = ["welcome", "howto"] + [f"plan:{p['id']}" for p in plans(active_only=False)]
    media = {}
    for sc in scopes:
        items = media_list(sc)
        if items:
            media[sc] = [{"id": i["id"], "kind": i["kind"], "file_id": i["file_id"]} for i in items]
    with db() as c:
        pending = [dict(r) for r in c.execute(
            "SELECT * FROM payments WHERE status = 'pending' ORDER BY id DESC LIMIT 50"
        ).fetchall()]
        settings = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings").fetchall()}
    settings.pop("bot_token", None)
    plan_rows = plans(active_only=False)
    for p in plan_rows:
        p["media_count"] = len(media_list(f"plan:{p['id']}"))
    return {
        "bot_username": get("bot_username"),
        "stats": stats(),
        "plans": plan_rows,
        "pending": pending,
        "settings": settings,
        "media": media,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _text(self, msg, code=400):
        raw = msg.encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        return check_init_data(self.headers.get("X-Init-Data", ""))

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/ping":
            return self._json({"ok": True})
        if not self._auth():
            return self._text("Unauthorized — open this panel from your bot's /admin button.", 401)

        if parsed.path == "/api/state":
            return self._json(api_state())

        if parsed.path == "/api/photo":
            file_id = urllib.parse.parse_qs(parsed.query).get("file_id", [""])[0]
            if not file_id:
                return self._text("missing file_id")
            info = call("getFile", file_id=file_id)
            if not info.get("ok"):
                return self._text("file not found", 404)
            path = info["result"]["file_path"]
            blob = requests.get(f"{API}/file/bot{get('bot_token')}/{path}", timeout=60).content
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            return self.wfile.write(blob)

        return self._text("Not found", 404)

    def do_POST(self):
        if not self._auth():
            return self._text("Unauthorized — open this panel from your bot's /admin button.", 401)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._text("Bad JSON")
        path = urllib.parse.urlparse(self.path).path

        try:
            if path == "/api/plan/save":
                pid = body.get("id")
                if pid:
                    fields, values = [], []
                    for k in ("label", "price", "reply_text", "qr_photo", "active"):
                        if k in body:
                            fields.append(f"{k} = ?")
                            values.append(body[k])
                    if fields:
                        values.append(pid)
                        with db() as c:
                            c.execute(f"UPDATE plans SET {', '.join(fields)} WHERE id = ?", values)
                else:
                    with db() as c:
                        c.execute("INSERT INTO plans (label, price, reply_text, position) "
                                  "VALUES (?, ?, ?, ?)",
                                  (body.get("label", "New plan"), float(body.get("price", 0)),
                                   body.get("reply_text", "Pay on the QR above and send the "
                                                          "screenshot here."), 99))
                return self._json({"ok": True})

            if path == "/api/plan/delete":
                pid = body.get("id")
                with db() as c:
                    c.execute("DELETE FROM plans WHERE id = ?", (pid,))
                media_clear(f"plan:{pid}")
                return self._json({"ok": True})

            if path == "/api/payment/decide":
                msg = decide(body.get("id"), bool(body.get("approve")))
                return self._json({"ok": True, "message": msg})

            if path == "/api/setting":
                key = body.get("key", "")
                if key in ("bot_token", "admin_chat_id") or key not in DEFAULTS:
                    return self._text("This setting cannot be changed from the panel.", 403)
                put(key, body.get("value", ""))
                return self._json({"ok": True})

            if path == "/api/media/clear":
                media_clear(body.get("scope", ""))
                return self._json({"ok": True})

            if path == "/api/broadcast":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._text("Empty message")
                sent, failed = broadcast_all(text)
                return self._json({"sent": sent, "failed": failed})
        except Exception as exc:
            return self._text(f"Server error: {exc}", 500)

        return self._text("Not found", 404)


def start_api_server():
    port = int(get("api_port") or 8099)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Mini app API listening on http://0.0.0.0:{port}")
    print("Expose it with:  ngrok http", port)


# ----------------------------------------------------------------------- handlers
def handle_message(msg):
    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return
    frm = msg.get("from", {}) or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    kind, file_id = extract_media(msg)
    step = get_step(chat_id)

    if step == "report" and not text.lower().startswith("/"):
        if not text and not file_id:
            return send(chat_id, "Please describe your issue or send a screenshot.", report_keyboard())
        set_step(chat_id, "")
        who = "@" + frm["username"] if frm.get("username") else frm.get("first_name", str(chat_id))
        header = (f"🚨 <b>Issue reported</b>\nFrom: {who} (<code>{chat_id}</code>)"
                  + (f"\n\n{text}" if text else ""))
        admin = get("admin_chat_id")
        if admin:
            if file_id and kind == "photo":
                send_photo(admin, file_id, header)
            elif file_id:
                call("sendVideo", chat_id=admin, video=file_id, caption=header[:1024],
                     parse_mode="HTML")
            else:
                send(admin, header)
        return send(chat_id, "✅ Thanks! Your issue has been sent to the admin.", start_keyboard())

    if step and is_admin(chat_id):
        parts = step.split(":")

        if parts[0] == "madd":
            scope = ":".join(parts[1:])
            if not file_id:
                return send(chat_id, "Send a photo or a video, please.")
            if not media_add(scope, kind, file_id):
                set_step(chat_id, "")
                return send(chat_id, f"Limit reached ({MEDIA_LIMIT}).", media_keyboard(scope))
            n = len(media_list(scope))
            return send(chat_id, f"Added ✅ ({n}/{MEDIA_LIMIT}). Send another one, or tap Done.",
                        {"inline_keyboard": [[{"text": "✔️ Done", "callback_data": f"mdone:{scope}"}]]})

        if parts[0] == "bcast":
            set_step(chat_id, "")
            if not text and not file_id:
                return send(chat_id, "Send text, a photo or a video to broadcast.")
            sent, failed = broadcast_all(text, file_id, kind or "")
            return send(chat_id, f"📢 Broadcast done.\nSent: <b>{sent}</b>\nFailed: <b>{failed}</b>",
                        dashboard_keyboard())

        if parts[0] == "set":
            key = parts[1]
            if key in PHOTO_KEYS:
                if not file_id or kind != "photo":
                    return send(chat_id, "Send a photo, please.")
                put(key, file_id)
            else:
                put(key, text)
            set_step(chat_id, "")
            return send(chat_id, "Saved ✅", dashboard_keyboard())

        if parts[0] == "pset":
            field, pid = parts[1], parts[2]
            value = file_id if field == "qr_photo" else text
            if field == "qr_photo" and (not value or kind != "photo"):
                return send(chat_id, "Send the QR photo, please.")
            if field == "price":
                try:
                    value = float(text)
                except ValueError:
                    return send(chat_id, "Send a number, e.g. 49")
            with db() as c:
                c.execute(f"UPDATE plans SET {field} = ? WHERE id = ?", (value, pid))
            set_step(chat_id, "")
            return send(chat_id, "Plan updated ✅", plans_keyboard())

        if parts[0] == "pnew":
            with db() as c:
                c.execute("INSERT INTO plans (label, price, reply_text, position) VALUES (?,?,?,?)",
                          (text or "New plan", 0,
                           "Pay on the QR above and send the screenshot here.", 99))
            set_step(chat_id, "")
            return send(chat_id, "Plan added ✅", plans_keyboard())

    if file_id and kind == "photo" and not is_admin(chat_id):
        with db() as c:
            sel = c.execute("SELECT * FROM payments WHERE chat_id = ? AND status = 'selected' "
                            "ORDER BY id DESC LIMIT 1", (str(chat_id),)).fetchone()
            if sel:
                pid = sel["id"]
                c.execute("UPDATE payments SET photo_file_id = ?, status = 'pending' WHERE id = ?",
                          (file_id, pid))
                label, price = sel["plan_label"], sel["price"]
            else:
                cur = c.execute(
                    "INSERT INTO payments (chat_id, username, full_name, plan_label, price, "
                    "photo_file_id, status, created_at) VALUES (?,?,?,?,?,?,'pending',?)",
                    (str(chat_id), frm.get("username", ""), frm.get("first_name", ""),
                     "Unknown plan", 0, file_id, time.time()))
                pid, label, price = cur.lastrowid, "Unknown plan", 0

        notify(chat_id, "submitted_text", pid, label)
        who = "@" + frm["username"] if frm.get("username") else frm.get("first_name", str(chat_id))
        if get("admin_chat_id"):
            send_photo(get("admin_chat_id"), file_id,
                       f"🔔 <b>NEW ORDER</b> 🥵\n👤 {who}\n🆔 <code>{chat_id}</code>\n"
                       f"🎒 {label}\n💰 ₹{int(price)}",
                       review_keyboard(pid))
        return

    low = text.lower()
    if text and not low.startswith("/") and not is_admin(chat_id):
        with db() as c:
            sel = c.execute("SELECT * FROM payments WHERE chat_id = ? AND status = 'selected' "
                            "ORDER BY id DESC LIMIT 1", (str(chat_id),)).fetchone()
            if sel:
                c.execute("UPDATE payments SET status = 'pending' WHERE id = ?", (sel["id"],))
        if sel:
            notify(chat_id, "submitted_text", sel["id"], sel["plan_label"])
            who = "@" + frm["username"] if frm.get("username") else frm.get("first_name", str(chat_id))
            if get("admin_chat_id"):
                send(get("admin_chat_id"),
                     f"🔔 <b>NEW ORDER (UTR)</b>\n👤 {who}\n🆔 <code>{chat_id}</code>\n"
                     f"🎒 {sel['plan_label']} — ₹{int(sel['price'])}\nUTR: <code>{text}</code>",
                     review_keyboard(sel["id"]))
            return

    if low.startswith("/start"):
        items = welcome_media()
        if items:
            send_media(chat_id, items, get("welcome_text"))
            return send(chat_id, "Choose an option below 👇", start_keyboard())
        return send(chat_id, get("welcome_text"), start_keyboard())

    if low.startswith("/admin") or low.startswith("/dashboard"):
        if is_admin(chat_id):
            return send(chat_id, admin_intro_text(), dashboard_keyboard())
        return send(chat_id, f"Your chat id is <code>{chat_id}</code>.")

    send(chat_id, "Send /start to see the available plans.")


def handle_callback(cq):
    cq_id = cq["id"]
    data = cq.get("data") or ""
    frm = cq.get("from", {}) or {}
    chat_id = cq.get("message", {}).get("chat", {}).get("id")

    def answer(text=""):
        call("answerCallbackQuery", callback_query_id=cq_id, text=text)

    if data == "howto":
        answer()
        items = media_list("howto")
        if items:
            send_media(chat_id, items, get("howto_text"))
        else:
            send(chat_id, get("howto_text"))
        return send(chat_id, "Ready? Pick a plan 👇", start_keyboard())

    if data == "report":
        answer()
        set_step(chat_id, "report")
        return send(chat_id, get("report_text"), report_keyboard())

    if data == "report_cancel":
        answer("Cancelled")
        set_step(chat_id, "")
        return send(chat_id, "Cancelled. Choose an option below 👇", start_keyboard())

    if data.startswith("plan:"):
        answer()
        set_step(chat_id, "")
        pid = data.split(":")[1]
        with db() as c:
            p = c.execute("SELECT * FROM plans WHERE id = ?", (pid,)).fetchone()
        if not p:
            return
        items = media_list(f"plan:{pid}")
        if items:
            send_media(chat_id, items, f"<b>{p['label']}</b> — ₹{int(p['price'])}")
        caption = p["reply_text"] or f"{p['label']} — ₹{int(p['price'])}\nScan the QR to pay."
        with db() as c:
            c.execute("DELETE FROM payments WHERE chat_id = ? AND status = 'selected'",
                      (str(chat_id),))
            c.execute("INSERT INTO payments (chat_id, username, full_name, plan_label, price, "
                      "status, created_at) VALUES (?,?,?,?,?, 'selected', ?)",
                      (str(chat_id), frm.get("username", ""), frm.get("first_name", ""),
                       p["label"], p["price"], time.time()))
        qr = p["qr_photo"] or get("qr_photo")
        if not qr:
            return send(chat_id, "QR is not set yet. Please contact support.")
        return send_photo(chat_id, qr, caption, pay_keyboard(pid))

    if data.startswith("paid:"):
        answer("Send screenshot or UTR")
        return send(chat_id, "✅ Great!\n\n📸 Please send your payment screenshot, or\n"
                             "📝 Type your UTR / Transaction ID\n\n"
                             "We'll verify and activate your plan within 30 minutes.")

    if data == "cancel":
        answer("Cancelled")
        with db() as c:
            c.execute("DELETE FROM payments WHERE chat_id = ? AND status = 'selected'",
                      (str(chat_id),))
        return send(chat_id, "Cancelled. Choose a plan whenever you're ready 👇", start_keyboard())

    if not is_admin(frm.get("id")):
        return answer("Only the admin can do this.")

    if data == "bcast":
        set_step(chat_id, "bcast")
        answer()
        return send(chat_id, f"📢 Send the message to broadcast to all {len(all_chat_ids())} users.",
                    {"inline_keyboard": [[{"text": "⬅️ Cancel", "callback_data": "dash"}]]})

    if data == "dash":
        answer()
        set_step(chat_id, "")
        return send(chat_id, admin_intro_text(), dashboard_keyboard())

    if data.startswith("pay_ok:") or data.startswith("pay_no:"):
        result = decide(data.split(":")[1], data.startswith("pay_ok:"))
        answer(result)
        msg = cq.get("message", {})
        if msg.get("message_id"):
            mark = "✅ APPROVED" if data.startswith("pay_ok:") else "❌ DECLINED"
            if msg.get("caption") is not None:
                call("editMessageCaption", chat_id=chat_id, message_id=msg["message_id"],
                     caption=f"{msg['caption']}\n\n<b>{mark}</b>", parse_mode="HTML")
            else:
                call("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                     text=f"{msg.get('text', '')}\n\n<b>{mark}</b>", parse_mode="HTML")
        return

    if data.startswith("media:"):
        scope = data.split(":", 1)[1]
        answer()
        items = media_list(scope)
        title = "Welcome media" if scope == "welcome" else "Media"
        if items:
            send_media(chat_id, items, f"<b>{title}</b> — current {len(items)} item(s)")
        return send(chat_id, f"{title}: {len(items)}/{MEDIA_LIMIT} items.", media_keyboard(scope))

    if data.startswith("madd:"):
        scope = data.split(":", 1)[1]
        set_step(chat_id, f"madd:{scope}")
        answer()
        return send(chat_id, f"Send photos/videos one by one (up to {MEDIA_LIMIT}).",
                    {"inline_keyboard": [[{"text": "✔️ Done", "callback_data": f"mdone:{scope}"}]]})

    if data.startswith("mdone:"):
        scope = data.split(":", 1)[1]
        set_step(chat_id, "")
        answer("Done")
        return send(chat_id, "Media saved ✅", media_keyboard(scope))

    if data.startswith("mclr:"):
        scope = data.split(":", 1)[1]
        media_clear(scope)
        answer("Removed")
        return send(chat_id, "All media removed.", media_keyboard(scope))

    if data.startswith("set:"):
        key = data.split(":")[1]
        set_step(chat_id, f"set:{key}")
        answer()
        if key == "qr_photo":
            return send(chat_id, "Send the QR photo once — it will be used for ALL plans.")
        if key == "webapp_url":
            return send(chat_id, "Send your published mini app URL (https://...).\n"
                                 f"Current: <code>{get(key) or '(empty)'}</code>")
        if key == "api_public_url":
            return send(chat_id, "Send the public https URL of this bot's API "
                                 "(e.g. your ngrok URL).\n"
                                 f"Current: <code>{get(key) or '(empty)'}</code>")
        prompt = "Send the new photo." if key in PHOTO_KEYS else "Send the new value."
        return send(chat_id, f"{prompt}\nCurrent: <code>{get(key) or '(empty)'}</code>")

    if data == "plans:list":
        answer()
        return send(chat_id, "Your plans:", plans_keyboard())

    if data == "pnew":
        set_step(chat_id, "pnew")
        answer()
        return send(chat_id, "Send the new plan's button label.")

    if data.startswith("pedit:"):
        answer()
        return send(chat_id, "What do you want to change?", plan_keyboard(data.split(":")[1]))

    if data.startswith("pset:"):
        _, field, pid = data.split(":")
        set_step(chat_id, f"pset:{field}:{pid}")
        answer()
        return send(chat_id, "Send the QR photo." if field == "qr_photo" else "Send the new value.")

    if data.startswith("pdel:"):
        pid = data.split(":")[1]
        with db() as c:
            c.execute("DELETE FROM plans WHERE id = ?", (pid,))
        media_clear(f"plan:{pid}")
        answer("Deleted")
        return send(chat_id, "Plan deleted.", plans_keyboard())

    if data == "pay:list":
        answer()
        with db() as c:
            rows = c.execute("SELECT * FROM payments WHERE status = 'pending' "
                             "ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            return send(chat_id, "No pending payments.", dashboard_keyboard())
        for r in rows:
            who = "@" + r["username"] if r["username"] else r["full_name"] or r["chat_id"]
            send_photo(chat_id, r["photo_file_id"],
                       f"🧾 {r['plan_label']} — ₹{int(r['price'])}\n"
                       f"From: {who} (<code>{r['chat_id']}</code>)",
                       review_keyboard(r["id"]))
        return


# --------------------------------------------------------------------------- main
def first_run_setup():
    if not get("bot_token"):
        put("bot_token", input("Bot token from @BotFather: ").strip())
    if not get("admin_chat_id"):
        admin_id = input("Your Telegram chat id: ").strip()
        if admin_id:
            put("admin_chat_id", admin_id)
    if not get("webapp_url"):
        url = input("Mini app URL (published Lovable app, blank to skip): ").strip()
        if url:
            put("webapp_url", url)
    if not get("api_public_url"):
        url = input("Public https URL for this bot's API (ngrok, blank to skip): ").strip()
        if url:
            put("api_public_url", url)


def main():
    init_db()
    first_run_setup()
    if not get("bot_token"):
        sys.exit("No bot token configured.")

    call("deleteWebhook", drop_pending_updates=False)
    me = call("getMe")
    if not me.get("ok"):
        sys.exit("Invalid bot token.")
    put("bot_username", me["result"].get("username", ""))
    print(f"Bot @{me['result'].get('username')} running. Press Ctrl+C to stop.")

    start_api_server()

    offset = 0
    while True:
        try:
            res = requests.get(
                f"{API}/bot{get('bot_token')}/getUpdates",
                params={"timeout": 50, "offset": offset,
                        "allowed_updates": json.dumps(["message", "callback_query"])},
                timeout=70,
            ).json()
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if "message" in upd:
                        handle_message(upd["message"])
                    elif "callback_query" in upd:
                        handle_callback(upd["callback_query"])
                except Exception as exc:
                    print("[error]", exc)
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except Exception as exc:
            print("[poll error]", exc)
            time.sleep(3)


if __name__ == "__main__":
    main()
