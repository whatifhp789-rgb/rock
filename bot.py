"""
Aether Bot — Telegram sales bot backed by the same Lovable Cloud database
that powers the Aether Control web panel.

Run:
    pip install requests
    export TELEGRAM_BOT_TOKEN="123:abc"
    export SUPABASE_URL="https://<project-ref>.supabase.co"
    export SUPABASE_SERVICE_ROLE_KEY="<service role key>"
    python bot.py

No SQLite: every read/write goes to the shared cloud tables
(bot_settings, plans, payments, bot_media), so anything you change in the
web panel is live in the bot instantly, and every order the bot creates
shows up in the panel.
"""

import os
import time
import html
import traceback
from typing import Any, Dict, List, Optional

import requests

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
PANEL_URL = os.environ.get("PANEL_URL", "").strip()  # https://your-app.lovable.app

if not BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
if not SUPABASE_URL or not SERVICE_KEY:
    raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
REST = f"{SUPABASE_URL}/rest/v1"
DB_HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
}

session = requests.Session()

# chat_id -> id of the order currently being paid for
awaiting: Dict[str, int] = {}


# --------------------------------------------------------------------------
# Database helpers (Supabase REST / PostgREST)
# --------------------------------------------------------------------------

def db_get(table: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    r = session.get(f"{REST}/{table}", headers=DB_HEADERS, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def db_insert(table: str, row: Dict[str, Any]) -> Dict[str, Any]:
    headers = dict(DB_HEADERS)
    headers["Prefer"] = "return=representation"
    r = session.post(f"{REST}/{table}", headers=headers, json=row, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data[0] if isinstance(data, list) and data else {}


def db_update(table: str, match: Dict[str, str], patch: Dict[str, Any]) -> None:
    r = session.patch(f"{REST}/{table}", headers=DB_HEADERS, params=match, json=patch, timeout=20)
    r.raise_for_status()


def get_setting(key: str, fallback: str = "") -> str:
    rows = db_get("bot_settings", {"key": f"eq.{key}", "select": "value", "limit": "1"})
    return rows[0]["value"] if rows else fallback


def get_plans() -> List[Dict[str, Any]]:
    return db_get("plans", {"active": "eq.true", "order": "position.asc", "select": "*"})


def get_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    rows = db_get("plans", {"id": f"eq.{plan_id}", "select": "*", "limit": "1"})
    return rows[0] if rows else None


def get_media(scope: str) -> List[Dict[str, Any]]:
    return db_get("bot_media", {"scope": f"eq.{scope}", "order": "position.asc", "select": "*"})


def render(template: str, order: Any, plan: str) -> str:
    return (template or "").replace("{order}", str(order)).replace("{plan}", plan)


# --------------------------------------------------------------------------
# Telegram helpers
# --------------------------------------------------------------------------

def tg(method: str, **payload: Any) -> Dict[str, Any]:
    try:
        r = session.post(f"{API}/{method}", json=payload, timeout=30)
        return r.json()
    except Exception:
        traceback.print_exc()
        return {"ok": False}


def send(chat_id: Any, text: str, keyboard: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    return tg("sendMessage", **payload)


def send_media(chat_id: Any, item: Dict[str, Any], caption: str = "") -> None:
    if item["kind"] == "video":
        tg("sendVideo", chat_id=chat_id, video=item["file_id"], caption=caption, parse_mode="HTML")
    else:
        tg("sendPhoto", chat_id=chat_id, photo=item["file_id"], caption=caption, parse_mode="HTML")


def main_menu() -> Dict[str, Any]:
    rows = [[{"text": f"💎 {p['label']} — ₹{int(float(p['price']))}", "callback_data": f"plan:{p['id']}"}]
            for p in get_plans()]
    rows.append([
        {"text": "📖 How to use", "callback_data": "howto"},
        {"text": "🆘 Report", "callback_data": "report"},
    ])
    if PANEL_URL:
        rows.append([{"text": "🛠 Admin panel", "web_app": {"url": PANEL_URL}}])
    return {"inline_keyboard": rows}


def back_menu() -> Dict[str, Any]:
    return {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "menu"}]]}


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

def send_welcome(chat_id: Any) -> None:
    for item in get_media("welcome"):
        send_media(chat_id, item)
    send(chat_id, get_setting("welcome_text", "Welcome!"), main_menu())


def handle_start(msg: Dict[str, Any]) -> None:
    chat_id = str(msg["chat"]["id"])
    awaiting.pop(chat_id, None)
    send_welcome(chat_id)


def handle_plan(chat_id: str, user: Dict[str, Any], plan_id: str) -> None:
    plan = get_plan(plan_id)
    if not plan:
        send(chat_id, "That plan is no longer available.", main_menu())
        return

    order = db_insert("payments", {
        "chat_id": chat_id,
        "username": user.get("username") or "",
        "full_name": " ".join(filter(None, [user.get("first_name"), user.get("last_name")])),
        "plan_id": plan["id"],
        "plan_label": plan["label"],
        "price": plan["price"],
        "status": "selected",
    })
    awaiting[chat_id] = order["id"]

    caption = plan["reply_text"] or f"Pay ₹{int(float(plan['price']))} and send the payment screenshot here."
    qr = plan["qr_photo"] or get_setting("qr_photo")
    if qr:
        tg("sendPhoto", chat_id=chat_id, photo=qr, caption=caption, parse_mode="HTML")
    else:
        send(chat_id, caption)
    send(chat_id, "📸 Now send the payment screenshot in this chat.", back_menu())


def handle_screenshot(msg: Dict[str, Any]) -> None:
    chat_id = str(msg["chat"]["id"])
    order_id = awaiting.get(chat_id)
    if not order_id:
        send(chat_id, "Please pick a plan first.", main_menu())
        return

    file_id = msg["photo"][-1]["file_id"]
    caption = (msg.get("caption") or "").strip()
    db_update("payments", {"id": f"eq.{order_id}"}, {
        "photo_file_id": file_id,
        "utr": caption,
        "status": "pending",
    })
    awaiting.pop(chat_id, None)

    rows = db_get("payments", {"id": f"eq.{order_id}", "select": "*", "limit": "1"})
    order = rows[0] if rows else {"plan_label": "", "price": 0}
    send(chat_id, render(get_setting("submitted_text", "Received! Please wait for approval."),
                         order_id, order.get("plan_label", "")))

    admin_id = get_setting("admin_chat_id")
    if admin_id:
        user = msg.get("from", {})
        who = f"@{user.get('username')}" if user.get("username") else html.escape(user.get("first_name", "user"))
        tg("sendPhoto", chat_id=admin_id, photo=file_id, parse_mode="HTML",
           caption=(f"🧾 <b>Order #{order_id}</b>\n{order.get('plan_label','')} — ₹{int(float(order.get('price') or 0))}\n"
                    f"From: {who} (<code>{chat_id}</code>)\nUTR: {html.escape(caption) or '—'}"),
           reply_markup={"inline_keyboard": [[
               {"text": "✅ Approve", "callback_data": f"ok:{order_id}"},
               {"text": "❌ Reject", "callback_data": f"no:{order_id}"},
           ]]})


def decide(order_id: int, approve: bool, admin_chat: Any) -> None:
    rows = db_get("payments", {"id": f"eq.{order_id}", "select": "*", "limit": "1"})
    if not rows:
        send(admin_chat, "Order not found.")
        return
    order = rows[0]
    if order["status"] not in ("pending", "selected"):
        send(admin_chat, f"Order #{order_id} is already {order['status']}.")
        return

    db_update("payments", {"id": f"eq.{order_id}"}, {
        "status": "approved" if approve else "declined",
        "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    link = ""
    if approve:
        plan = get_plan(order["plan_id"]) if order.get("plan_id") else None
        link = (plan or {}).get("access_link") or get_setting("access_link")

    body = render(get_setting("approved_text" if approve else "declined_text"),
                  order_id, order.get("plan_label", ""))
    if approve and link:
        body = f"{body}\n{link}"
    send(order["chat_id"], body)
    send(admin_chat, f"{'✅ Approved' if approve else '❌ Rejected'} order #{order_id}.")


def handle_callback(cb: Dict[str, Any]) -> None:
    data = cb.get("data") or ""
    chat_id = str(cb["message"]["chat"]["id"])
    tg("answerCallbackQuery", callback_query_id=cb["id"])

    if data == "menu":
        send_welcome(chat_id)
    elif data == "howto":
        for item in get_media("howto"):
            send_media(chat_id, item)
        send(chat_id, get_setting("howto_text", "Guide coming soon."), back_menu())
    elif data == "report":
        send(chat_id, get_setting("report_text", "Describe your issue and we'll get back to you."), back_menu())
    elif data.startswith("plan:"):
        handle_plan(chat_id, cb.get("from", {}), data.split(":", 1)[1])
    elif data.startswith(("ok:", "no:")):
        if chat_id != get_setting("admin_chat_id"):
            return
        decide(int(data.split(":", 1)[1]), data.startswith("ok:"), chat_id)


def handle_update(update: Dict[str, Any]) -> None:
    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg or "chat" not in msg:
        return

    if "photo" in msg:
        handle_screenshot(msg)
        return

    text = (msg.get("text") or "").strip()
    chat_id = str(msg["chat"]["id"])

    if text.startswith("/start"):
        handle_start(msg)
    elif text.startswith("/id"):
        send(chat_id, f"Your chat id: <code>{chat_id}</code>")
    elif text.startswith("/panel"):
        send(chat_id, "Open the control panel:", {"inline_keyboard": [[
            {"text": "🛠 Aether Control", "web_app": {"url": PANEL_URL}}]]} if PANEL_URL else None)
    else:
        send(chat_id, "Use the menu below 👇", main_menu())


# --------------------------------------------------------------------------
# Long polling loop
# --------------------------------------------------------------------------

def main() -> None:
    tg("deleteWebhook", drop_pending_updates=False)
    print("Aether bot is running…")
    offset = 0
    while True:
        try:
            r = session.get(f"{API}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=60)
            payload = r.json()
            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception:
                    traceback.print_exc()
        except requests.exceptions.RequestException:
            time.sleep(3)
        except Exception:
            traceback.print_exc()
            time.sleep(3)


if __name__ == "__main__":
    main()
