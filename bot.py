"""
Telegram payment bot — single file Python version (media + dashboard edition).

Fixes in this version
---------------------
1. No more 3–4 duplicate replies:
   * single-instance lock (bot.lock) — a second copy of the script refuses to run
   * processed update_id table — the same update is never handled twice
   * media-group (album) dedupe — an album of screenshots = ONE request, one reply
   * getUpdates offset is confirmed before handling
2. Every request now gets its own random 4-digit Order ID (e.g. #4821).
3. The text sent after the QR always starts with the ✅ tick logo.

Setup
-----
  pip install requests
  python bot.py            (first run asks for the bot token and your chat id)
"""

import atexit
import json
import os
import random
import sqlite3
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot.db")
LOCK_PATH = os.path.join(BASE_DIR, "bot.lock")
API = "https://api.telegram.org"

DEFAULTS = {
    "bot_token": "8920151470:AAGN1bMKSksTrd37vcYqF7Hw92rTZQ_bMuk",
    "admin_chat_id": "8657077884",
    "welcome_text": "Welcome! Choose an option below.",
    "welcome_photo": "",          # legacy single photo (still supported)
    "access_link": "",
    # {order} and {plan} are replaced automatically in these three texts
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

MEDIA_LIMIT = 10  # telegram album limit


def new_order_code():
    """Random 4-digit order id, unique among existing payments."""
    with db() as c:
        for _ in range(50):
            code = str(random.randint(1000, 9999))
            row = c.execute(
                "SELECT 1 FROM payments WHERE order_code = ?", (code,)
            ).fetchone()
            if not row:
                return code
    return str(random.randint(1000, 9999))


def tick(text):
    """Make sure the message starts with the ✅ tick logo."""
    text = (text or "").lstrip()
    return text if text.startswith("✅") else "✅ " + text


def render(template, order="", plan=""):
    """Fill {order} / {plan} placeholders without crashing on other braces."""
    return (template or "").replace("{order}", str(order)).replace("{plan}", str(plan))


def notify(chat_id, text_key, order="", plan="", extra=""):
    """Send the admin-configured photo + text for a status message."""
    body = render(get(text_key), order, plan) + (("\n" + extra) if extra else "")
    photo = get(text_key.replace("_text", "_photo"))
    if photo:
        return send_photo(chat_id, photo, body)
    return send(chat_id, body)


# --------------------------------------------------------------------------- db
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
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
                   order_code TEXT NOT NULL DEFAULT '',
                   chat_id TEXT NOT NULL,
                   username TEXT NOT NULL DEFAULT '',
                   full_name TEXT NOT NULL DEFAULT '',
                   plan_label TEXT NOT NULL DEFAULT '',
                   price REAL NOT NULL DEFAULT 0,
                   photo_file_id TEXT NOT NULL DEFAULT '',
                   media_group_id TEXT NOT NULL DEFAULT '',
                   status TEXT NOT NULL DEFAULT 'selected',
                   created_at REAL NOT NULL DEFAULT 0)"""
        )
        # older databases: add the new columns if they are missing
        cols = {r["name"] for r in c.execute("PRAGMA table_info(payments)").fetchall()}
        if "order_code" not in cols:
            c.execute("ALTER TABLE payments ADD COLUMN order_code TEXT NOT NULL DEFAULT ''")
        if "media_group_id" not in cols:
            c.execute("ALTER TABLE payments ADD COLUMN media_group_id TEXT NOT NULL DEFAULT ''")
        # media library: scope = 'welcome' or 'plan:<plan_id>'
        c.execute(
            """CREATE TABLE IF NOT EXISTS media (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   scope TEXT NOT NULL,
                   kind TEXT NOT NULL,              -- 'photo' | 'video'
                   file_id TEXT NOT NULL,
                   position INTEGER NOT NULL DEFAULT 0)"""
        )
        c.execute("CREATE TABLE IF NOT EXISTS state (chat_id TEXT PRIMARY KEY, step TEXT NOT NULL)")
        # every update_id we already handled -> guarantees no duplicate replies
        c.execute(
            """CREATE TABLE IF NOT EXISTS seen_updates (
                   update_id INTEGER PRIMARY KEY,
                   created_at REAL NOT NULL DEFAULT 0)"""
        )
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


def already_handled(update_id):
    """True if this update_id was processed before (duplicate delivery)."""
    with db() as c:
        try:
            c.execute(
                "INSERT INTO seen_updates (update_id, created_at) VALUES (?, ?)",
                (int(update_id), time.time()),
            )
        except sqlite3.IntegrityError:
            return True
        c.execute("DELETE FROM seen_updates WHERE created_at < ?", (time.time() - 86400,))
    return False


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
        c.execute(
            "INSERT INTO media (scope, kind, file_id, position) VALUES (?, ?, ?, ?)",
            (scope, kind, file_id, n + 1),
        )
    return True


def media_clear(scope):
    with db() as c:
        c.execute("DELETE FROM media WHERE scope = ?", (scope,))


def set_step(chat_id, step):
    with db() as c:
        if step:
            c.execute(
                "INSERT INTO state (chat_id, step) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET step = excluded.step",
                (str(chat_id), step),
            )
        else:
            c.execute("DELETE FROM state WHERE chat_id = ?", (str(chat_id),))


def get_step(chat_id):
    with db() as c:
        row = c.execute("SELECT step FROM state WHERE chat_id = ?", (str(chat_id),)).fetchone()
    return row["step"] if row else ""


def stats():
    with db() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(price), 0) AS total "
            "FROM payments GROUP BY status"
        ).fetchall()
        users = c.execute("SELECT COUNT(DISTINCT chat_id) AS n FROM payments").fetchone()["n"]
    by = {r["status"]: {"n": r["n"], "total": r["total"]} for r in rows}
    return {
        "users": users,
        "pending": by.get("pending", {}).get("n", 0),
        "approved": by.get("approved", {}).get("n", 0),
        "declined": by.get("declined", {}).get("n", 0),
        "selected": by.get("selected", {}).get("n", 0),
        "revenue": by.get("approved", {}).get("total", 0),
    }


def all_chat_ids():
    with db() as c:
        return [r["chat_id"] for r in c.execute(
            "SELECT DISTINCT chat_id FROM payments"
        ).fetchall()]


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
    """Send 1..10 photos/videos. Album when >1 (caption on the first item)."""
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


def start_keyboard():
    rows = [[{"text": f"{p['label']} — ₹{int(p['price'])}", "callback_data": f"plan:{p['id']}"}]
            for p in plans()]
    rows.append([
        {"text": "📘 How to use", "callback_data": "howto"},
        {"text": "🚨 Report an Issue", "callback_data": "report"},
    ])
    return {"inline_keyboard": rows}


def pay_keyboard(pid):
    return {
        "inline_keyboard": [
            [{"text": "✅ I have paid", "callback_data": f"paid:{pid}"}],
            [{"text": "❌ Cancel", "callback_data": "cancel"}],
        ]
    }


def report_keyboard():
    return {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "report_cancel"}]]}


def review_keyboard(payment_id):
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"pay_ok:{payment_id}"},
            {"text": "❌ Decline", "callback_data": f"pay_no:{payment_id}"},
        ]]
    }


def dashboard_text():
    s = stats()
    with db() as c:
        rows = c.execute(
            "SELECT * FROM payments WHERE status = 'pending' ORDER BY id DESC LIMIT 5"
        ).fetchall()
    lines = [
        "📊 <b>Dashboard</b>",
        "",
        f"👥 Customers: <b>{s['users']}</b>",
        f"🧾 Pending review: <b>{s['pending']}</b>",
        f"✅ Approved: <b>{s['approved']}</b>",
        f"❌ Declined: <b>{s['declined']}</b>",
        f"🕒 Waiting for screenshot: <b>{s['selected']}</b>",
        f"💰 Approved revenue: <b>₹{int(s['revenue'])}</b>",
    ]
    if rows:
        lines += ["", "<b>Latest pending</b>"]
        for r in rows:
            who = "@" + r["username"] if r["username"] else (r["full_name"] or r["chat_id"])
            code = r["order_code"] or r["id"]
            lines.append(f"• #{code} {who} — {r['plan_label']} ₹{int(r['price'])}")
    return "\n".join(lines)


def dashboard_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🧾 Review payments", "callback_data": "pay:list"},
             {"text": "🔄 Refresh", "callback_data": "dash"}],
            [{"text": "💰 Plans & QR", "callback_data": "plans:list"},
             {"text": "🎞 Welcome media", "callback_data": "media:welcome"}],
            [{"text": "💬 Welcome text", "callback_data": "set:welcome_text"},
             {"text": "🔗 Access link", "callback_data": "set:access_link"}],
            [{"text": "✅ Approved text", "callback_data": "set:approved_text"},
             {"text": "❌ Declined text", "callback_data": "set:declined_text"}],
            [{"text": "🖼 Submitted photo", "callback_data": "set:submitted_photo"},
             {"text": "📝 Submitted text", "callback_data": "set:submitted_text"}],
            [{"text": "🖼 Approved photo", "callback_data": "set:approved_photo"},
             {"text": "🖼 Declined photo", "callback_data": "set:declined_photo"}],
            [{"text": "📘 How-to video", "callback_data": "media:howto"},
             {"text": "📘 How-to text", "callback_data": "set:howto_text"}],
            [{"text": "🚨 Report text", "callback_data": "set:report_text"},
             {"text": "📷 QR (all plans)", "callback_data": "set:qr_photo"}],
            [{"text": "📢 Broadcast", "callback_data": "bcast"}],
        ]
    }


def media_keyboard(scope):
    items = media_list(scope)
    return {
        "inline_keyboard": [
            [{"text": f"➕ Add photo/video ({len(items)}/{MEDIA_LIMIT})",
              "callback_data": f"madd:{scope}"}],
            [{"text": "🗑 Remove all", "callback_data": f"mclr:{scope}"}],
            [{"text": "⬅️ Dashboard", "callback_data": "dash"}],
        ]
    }


def plans_keyboard():
    rows = [[{"text": f"{p['label']} — ₹{int(p['price'])}", "callback_data": f"pedit:{p['id']}"}]
            for p in plans(active_only=False)]
    rows.append([{"text": "➕ Add plan", "callback_data": "pnew"}])
    rows.append([{"text": "⬅️ Dashboard", "callback_data": "dash"}])
    return {"inline_keyboard": rows}


def plan_keyboard(pid):
    return {
        "inline_keyboard": [
            [{"text": "✏️ Label", "callback_data": f"pset:label:{pid}"},
             {"text": "💵 Price", "callback_data": f"pset:price:{pid}"}],
            [{"text": "📝 Reply text", "callback_data": f"pset:reply_text:{pid}"},
             {"text": "📷 QR (only this plan)", "callback_data": f"pset:qr_photo:{pid}"}],
            [{"text": "🎞 Plan videos", "callback_data": f"media:plan:{pid}"}],
            [{"text": "🗑 Delete plan", "callback_data": f"pdel:{pid}"}],
            [{"text": "⬅️ Plans", "callback_data": "plans:list"}],
        ]
    }


def is_admin(chat_id):
    return str(chat_id) == str(get("admin_chat_id"))


def extract_media(msg):
    """Return ('photo'|'video', file_id) or (None, '')."""
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
        c.execute(
            "UPDATE payments SET status = ? WHERE id = ?",
            ("approved" if approve else "declined", payment_id),
        )

    code = row["order_code"] or row["id"]
    if approve:
        link = get("access_link")
        notify(row["chat_id"], "approved_text", code, row["plan_label"],
               extra=link or "(link not set yet)")
    else:
        notify(row["chat_id"], "declined_text", code, row["plan_label"])
    return "Approved & link sent" if approve else "Declined"


# ----------------------------------------------------------------------- handlers
def handle_message(msg):
    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return
    frm = msg.get("from", {}) or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    kind, file_id = extract_media(msg)
    group_id = str(msg.get("media_group_id") or "")

    step = get_step(chat_id)

    # ---- customer is reporting an issue ----
    if step == "report" and not (text.lower().startswith("/")):
        if not text and not file_id:
            return send(chat_id, "Please describe your issue or send a screenshot.",
                        report_keyboard())
        set_step(chat_id, "")
        who = "@" + frm["username"] if frm.get("username") else frm.get("first_name", str(chat_id))
        header = (f"🚨 <b>Issue reported</b>\nFrom: {who} (<code>{chat_id}</code>)"
                  + (f"\n\n{text}" if text else ""))
        admin = get("admin_chat_id")
        if admin:
            if file_id and kind == "photo":
                send_photo(admin, file_id, header)
            elif file_id:
                call("sendVideo", chat_id=admin, video=file_id,
                     caption=header[:1024], parse_mode="HTML")
            else:
                send(admin, header)
        return send(chat_id, "✅ Thanks! Your issue has been sent to the admin. "
                             "We'll get back to you as soon as possible.",
                    start_keyboard())

    # ---- admin is filling in a value ----
    if step and is_admin(chat_id):
        parts = step.split(":")

        if parts[0] == "madd":
            scope = ":".join(parts[1:])
            if not file_id:
                return send(chat_id, "Send a photo or a video, please.")
            ok = media_add(scope, kind, file_id)
            if not ok:
                set_step(chat_id, "")
                return send(chat_id, f"Limit reached ({MEDIA_LIMIT}).", media_keyboard(scope))
            n = len(media_list(scope))
            if group_id:
                # album: stay quiet for the extra items, confirm once at the end
                return None
            return send(
                chat_id,
                f"Added ✅ ({n}/{MEDIA_LIMIT}). Send another one, or tap Done.",
                {"inline_keyboard": [[{"text": "✔️ Done", "callback_data": f"mdone:{scope}"}]]},
            )

        if parts[0] == "bcast":
            set_step(chat_id, "")
            if not text and not file_id:
                return send(chat_id, "Send text, a photo or a video to broadcast.")
            targets = all_chat_ids()
            sent = failed = 0
            for cid in targets:
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
                c.execute(
                    "INSERT INTO plans (label, price, reply_text, position) VALUES (?, ?, ?, ?)",
                    (text or "New plan", 0, "Pay on the QR above and send the screenshot here.", 99),
                )
            set_step(chat_id, "")
            return send(chat_id, "Plan added ✅ Now set its price, QR and videos.", plans_keyboard())

    # ---- payment screenshot from a customer ----
    if file_id and kind == "photo" and not is_admin(chat_id):
        with db() as c:
            # album of screenshots -> only the first photo creates the request
            if group_id:
                dup = c.execute(
                    "SELECT 1 FROM payments WHERE chat_id = ? AND media_group_id = ?",
                    (str(chat_id), group_id),
                ).fetchone()
                if dup:
                    return None
            sel = c.execute(
                "SELECT * FROM payments WHERE chat_id = ? AND status = 'selected' "
                "ORDER BY id DESC LIMIT 1",
                (str(chat_id),),
            ).fetchone()
            if sel:
                pid = sel["id"]
                c.execute(
                    "UPDATE payments SET photo_file_id = ?, media_group_id = ?, "
                    "status = 'pending' WHERE id = ?",
                    (file_id, group_id, pid),
                )
                label, price, code = sel["plan_label"], sel["price"], sel["order_code"]
            else:
                code = new_order_code()
                cur = c.execute(
                    "INSERT INTO payments (order_code, chat_id, username, full_name, plan_label, "
                    "price, photo_file_id, media_group_id, status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,'pending',?)",
                    (code, str(chat_id), frm.get("username", ""), frm.get("first_name", ""),
                     "Unknown plan", 0, file_id, group_id, time.time()),
                )
                pid, label, price = cur.lastrowid, "Unknown plan", 0

        code = code or str(pid)
        notify(chat_id, "submitted_text", code, label)
        who = "@" + frm["username"] if frm.get("username") else frm.get("first_name", str(chat_id))
        if get("admin_chat_id"):
            send_photo(
                get("admin_chat_id"),
                file_id,
                f"🧾 <b>Payment for review</b>\n🆔 Order #{code}\n{label} — ₹{int(price)}\n"
                f"From: {who} (<code>{chat_id}</code>)",
                review_keyboard(pid),
            )
        return

    # ---- UTR / transaction id from a customer waiting to pay ----
    low = text.lower()
    if text and not low.startswith("/") and not is_admin(chat_id):
        with db() as c:
            sel = c.execute(
                "SELECT * FROM payments WHERE chat_id = ? AND status = 'selected' "
                "ORDER BY id DESC LIMIT 1",
                (str(chat_id),),
            ).fetchone()
            if sel:
                c.execute("UPDATE payments SET status = 'pending' WHERE id = ?", (sel["id"],))
        if sel:
            code = sel["order_code"] or sel["id"]
            notify(chat_id, "submitted_text", code, sel["plan_label"])
            who = "@" + frm["username"] if frm.get("username") else frm.get("first_name", str(chat_id))
            if get("admin_chat_id"):
                send(get("admin_chat_id"),
                     f"🧾 <b>Payment for review (UTR)</b>\n🆔 Order #{code}\n"
                     f"{sel['plan_label']} — ₹{int(sel['price'])}\nUTR: <code>{text}</code>\n"
                     f"From: {who} (<code>{chat_id}</code>)",
                     review_keyboard(sel["id"]))
            return

    # ---- commands ----
    if low.startswith("/start"):
        items = welcome_media()
        if items:
            send_media(chat_id, items, get("welcome_text"))
            return send(chat_id, "Choose an option below 👇", start_keyboard())
        return send(chat_id, get("welcome_text"), start_keyboard())

    if low.startswith("/admin") or low.startswith("/dashboard"):
        if is_admin(chat_id):
            return send(chat_id, dashboard_text(), dashboard_keyboard())
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

    # customer picked a plan
    if data.startswith("plan:"):
        answer()
        set_step(chat_id, "")
        pid = data.split(":")[1]
        with db() as c:
            p = c.execute("SELECT * FROM plans WHERE id = ?", (pid,)).fetchone()
        if not p:
            return
        # 1) plan videos / photos first
        items = media_list(f"plan:{pid}")
        if items:
            send_media(chat_id, items, f"<b>{p['label']}</b> — ₹{int(p['price'])}")
        # 2) then the QR with the "I have paid" / "Cancel" buttons under it
        code = new_order_code()
        base = p["reply_text"] or f"{p['label']} — ₹{int(p['price'])}\nScan the QR to pay."
        caption = tick(base) + f"\n\n🆔 Order #{code}"
        with db() as c:
            c.execute("DELETE FROM payments WHERE chat_id = ? AND status = 'selected'", (str(chat_id),))
            c.execute(
                "INSERT INTO payments (order_code, chat_id, username, full_name, plan_label, "
                "price, status, created_at) VALUES (?,?,?,?,?,?, 'selected', ?)",
                (code, str(chat_id), frm.get("username", ""), frm.get("first_name", ""),
                 p["label"], p["price"], time.time()),
            )
        qr = p["qr_photo"] or get("qr_photo")
        if not qr:
            return send(chat_id, "QR is not set yet. Please contact support.")
        send_photo(chat_id, qr, caption, pay_keyboard(pid))
        return

    if data.startswith("paid:"):
        answer("Send screenshot or UTR")
        return send(chat_id, "✅ Great!\n\n📸 Please send your payment screenshot, or\n"
                             "📝 Type your UTR / Transaction ID\n\n"
                             "We'll verify and activate your plan within 30 minutes.")

    if data == "cancel":
        answer("Cancelled")
        with db() as c:
            c.execute("DELETE FROM payments WHERE chat_id = ? AND status = 'selected'", (str(chat_id),))
        return send(chat_id, "Cancelled. Choose a plan whenever you're ready 👇", start_keyboard())

    # everything below is admin-only
    if not is_admin(frm.get("id")):
        return answer("Only the admin can do this.")

    if data == "bcast":
        set_step(chat_id, "bcast")
        answer()
        return send(chat_id, f"📢 Send the message (text, photo or video) to broadcast to all "
                             f"{len(all_chat_ids())} users.",
                    {"inline_keyboard": [[{"text": "⬅️ Cancel", "callback_data": "dash"}]]})

    if data == "dash":
        answer()
        set_step(chat_id, "")
        return send(chat_id, dashboard_text(), dashboard_keyboard())

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
        scope = data.split(":", 1)[1]          # 'welcome' or 'plan:<id>'
        answer()
        items = media_list(scope)
        title = "Welcome media" if scope == "welcome" else "Plan media"
        if items:
            send_media(chat_id, items, f"<b>{title}</b> — current {len(items)} item(s)")
        return send(chat_id, f"{title}: {len(items)}/{MEDIA_LIMIT} items.", media_keyboard(scope))

    if data.startswith("madd:"):
        scope = data.split(":", 1)[1]
        set_step(chat_id, f"madd:{scope}")
        answer()
        return send(chat_id, "Send photos/videos one by one (up to "
                             f"{MEDIA_LIMIT}). Tap Done when finished.",
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
        prompt = "Send the new photo." if key in PHOTO_KEYS else (
            "Send the new text. You can use {order} and {plan} placeholders."
            if key in ("submitted_text", "approved_text", "declined_text")
            else "Send the new value.")
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
            rows = c.execute(
                "SELECT * FROM payments WHERE status = 'pending' ORDER BY id DESC LIMIT 10"
            ).fetchall()
        if not rows:
            return send(chat_id, "No pending payments.", dashboard_keyboard())
        for r in rows:
            who = "@" + r["username"] if r["username"] else r["full_name"] or r["chat_id"]
            code = r["order_code"] or r["id"]
            send_photo(
                chat_id,
                r["photo_file_id"],
                f"🧾 Order #{code} — {r['plan_label']} ₹{int(r['price'])}\n"
                f"From: {who} (<code>{r['chat_id']}</code>)",
                review_keyboard(r["id"]),
            )
        return


# ------------------------------------------------------------------ single copy
def acquire_lock():
    """Refuse to start when another copy of the bot is already polling."""
    if os.path.exists(LOCK_PATH):
        try:
            old_pid = int(open(LOCK_PATH).read().strip() or 0)
        except ValueError:
            old_pid = 0
        alive = False
        if old_pid:
            try:
                os.kill(old_pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive:
            sys.exit(
                f"Another bot instance is already running (pid {old_pid}).\n"
                "Stop it first — two copies polling at once cause duplicate replies."
            )
        os.remove(LOCK_PATH)
    with open(LOCK_PATH, "w") as fh:
        fh.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(LOCK_PATH) and os.remove(LOCK_PATH))


# --------------------------------------------------------------------------- main
def first_run_setup():
    if not get("bot_token"):
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            token = input("Bot token from @BotFather: ").strip()
        put("bot_token", token)
    if not get("admin_chat_id"):
        print("Send /admin to your bot from your own account, then paste the chat id it replies "
              "with. Leave empty to skip for now.")
        admin_id = input("Your chat id: ").strip()
        if admin_id:
            put("admin_chat_id", admin_id)


def main():
    init_db()
    first_run_setup()
    if not get("bot_token"):
        sys.exit("No bot token configured.")

    acquire_lock()

    # long polling mode; drop the backlog so old updates aren't replayed
    call("deleteWebhook", drop_pending_updates=True)
    me = call("getMe")
    if not me.get("ok"):
        sys.exit("Invalid bot token.")
    print(f"Bot @{me['result'].get('username')} running. Press Ctrl+C to stop.")

    offset = 0
    while True:
        try:
            res = requests.get(
                f"{API}/bot{get('bot_token')}/getUpdates",
                params={"timeout": 50, "offset": offset,
                        "allowed_updates": json.dumps(["message", "callback_query"])},
                timeout=70,
            ).json()
            updates = res.get("result", [])
            if updates:
                # confirm the whole batch first so Telegram never resends it
                offset = updates[-1]["update_id"] + 1
                try:
                    requests.get(
                        f"{API}/bot{get('bot_token')}/getUpdates",
                        params={"timeout": 0, "offset": offset}, timeout=20,
                    )
                except Exception:
                    pass
            for upd in updates:
                if already_handled(upd["update_id"]):
                    continue
                try:
                    if "message" in upd:
                        handle_message(upd["message"])
                    elif "callback_query" in upd:
                        handle_callback(upd["callback_query"])
                except Exception as exc:  # keep the bot alive on any single failure
                    print("[error]", exc)
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except Exception as exc:
            print("[poll error]", exc)
            time.sleep(3)


if __name__ == "__main__":
    main()
