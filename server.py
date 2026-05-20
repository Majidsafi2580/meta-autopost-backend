import os
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, request, redirect, jsonify, render_template_string

load_dotenv()

APP_ID = os.getenv("META_APP_ID", "").strip()
APP_SECRET = os.getenv("META_APP_SECRET", "").strip()
REDIRECT_URI = os.getenv("META_REDIRECT_URI", "").strip()
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v25.0")
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)

ACCOUNTS_FILE = DATA_DIR / "accounts.json"
STATE_FILE = DATA_DIR / "states.json"
BOT_STATES_FILE = DATA_DIR / "bot_states.json"

SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "business_management",
]

app = Flask(__name__)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def require_config():
    missing = []
    for key, value in {
        "META_APP_ID": APP_ID,
        "META_APP_SECRET": APP_SECRET,
        "META_REDIRECT_URI": REDIRECT_URI,
    }.items():
        if not value:
            missing.append(key)
    return missing


def graph_get(path, params):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{path.lstrip('/')}"
    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
    return data


def graph_post(path, data=None, params=None):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{path.lstrip('/')}"
    r = requests.post(url, data=data or {}, params=params or {}, timeout=60)
    out = r.json()
    if r.status_code >= 400:
        raise RuntimeError(json.dumps(out, ensure_ascii=False))
    return out


def tg_api(method, payload):
    if not BOT_TOKEN:
        return None
    try:
        return requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload,
            timeout=15,
        ).json()
    except Exception:
        return None


def send_telegram(chat_id, text, keyboard=True):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = {
            "keyboard": [
                [{"text": "➕ ربط حساب Meta"}, {"text": "📄 حساباتي"}],
                [{"text": "📝 نشر في Facebook"}, {"text": "❌ إلغاء"}],
            ],
            "resize_keyboard": True,
        }
    return tg_api("sendMessage", payload)


def notify_admin(text):
    if BOT_TOKEN and ADMIN_CHAT_ID:
        send_telegram(ADMIN_CHAT_ID, text, keyboard=False)


def find_account_for_telegram(telegram_id):
    accounts = load_json(ACCOUNTS_FILE, [])
    telegram_id = str(telegram_id)
    for acc in reversed(accounts):
        if str(acc.get("telegram_id")) == telegram_id:
            return acc
    for acc in reversed(accounts):
        if str(acc.get("telegram_id")) == "admin":
            return acc
    return accounts[-1] if accounts else None


def get_first_page_for_telegram(telegram_id):
    acc = find_account_for_telegram(telegram_id)
    if not acc:
        raise RuntimeError("ما كاين حتى حساب Meta مربوط. اضغط ➕ ربط حساب Meta أولاً.")
    pages = acc.get("pages", [])
    if not pages:
        raise RuntimeError("الحساب مربوط، لكن ما لقيناش Pages. عاود الربط وتأكد أنك اخترت الصفحة.")
    page = pages[0]
    if not page.get("access_token"):
        raise RuntimeError("ما لقيناش Page access token. عاود الربط بالصلاحيات الكاملة.")
    return page


@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "name": "Meta AutoPost Backend + Telegram",
        "routes": [
            "/privacy", "/terms", "/data-deletion",
            "/auth/meta/start?telegram_id=YOUR_TELEGRAM_ID",
            "/auth/meta/callback", "/accounts", "/post/facebook",
            "/telegram/webhook", "/telegram/set-webhook",
        ],
        "missing_env": require_config(),
        "telegram_ready": bool(BOT_TOKEN),
    })


@app.get("/privacy")
def privacy():
    return render_template_string("""
    <h1>Privacy Policy</h1>
    <p>This app is used to connect Facebook Pages and Instagram professional accounts for content publishing automation.</p>
    <p>We only store the access token and account/page identifiers needed to publish content selected by the account owner.</p>
    <p>We do not sell user data. We do not ask for Facebook or Instagram passwords.</p>
    <p>To request deletion, visit <a href="/data-deletion">/data-deletion</a>.</p>
    """)


@app.get("/terms")
def terms():
    return render_template_string("""
    <h1>Terms of Service</h1>
    <p>This tool helps authorized users schedule and publish their own content to Facebook Pages and Instagram professional accounts.</p>
    <p>Users are responsible for the content they publish and must follow Meta platform rules and local laws.</p>
    <p>The app may stop working if Meta permissions, access tokens, or API policies change.</p>
    """)


@app.route("/data-deletion", methods=["GET", "POST"])
def data_deletion():
    if request.method == "GET":
        return render_template_string("""
        <h1>User Data Deletion</h1>
        <p>Send a deletion request to the app admin with your Facebook user ID or Telegram ID.</p>
        <p>Endpoint status: active.</p>
        """)
    confirmation_code = secrets.token_hex(8)
    return jsonify({
        "url": f"{BASE_URL or request.host_url.rstrip('/')}/data-deletion/status/{confirmation_code}",
        "confirmation_code": confirmation_code,
    })


@app.get("/data-deletion/status/<code>")
def data_deletion_status(code):
    return jsonify({"status": "received", "confirmation_code": code})


@app.get("/auth/meta/start")
def meta_start():
    missing = require_config()
    if missing:
        return jsonify({"ok": False, "error": "Missing env vars", "missing": missing}), 500

    telegram_id = request.args.get("telegram_id", "admin")
    state = secrets.token_urlsafe(24)
    states = load_json(STATE_FILE, {})
    states[state] = {"telegram_id": str(telegram_id), "created_at": datetime.now(timezone.utc).isoformat()}
    save_json(STATE_FILE, states)

    params = {
        "client_id": APP_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": ",".join(SCOPES),
        "response_type": "code",
    }
    return redirect(f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth?{urlencode(params)}")


@app.get("/auth/meta/callback")
def meta_callback():
    error = request.args.get("error")
    if error:
        return jsonify({"ok": False, "error": error, "details": request.args.to_dict()}), 400

    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return jsonify({"ok": False, "error": "Missing code or state"}), 400

    states = load_json(STATE_FILE, {})
    state_data = states.pop(state, None)
    save_json(STATE_FILE, states)
    if not state_data:
        return jsonify({"ok": False, "error": "Invalid or expired state"}), 400

    try:
        token_data = graph_get("/oauth/access_token", {
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        })
        short_token = token_data["access_token"]
        long_data = graph_get("/oauth/access_token", {
            "grant_type": "fb_exchange_token",
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "fb_exchange_token": short_token,
        })
        user_token = long_data["access_token"]
        me = graph_get("/me", {"access_token": user_token, "fields": "id,name"})
        pages = graph_get("/me/accounts", {
            "access_token": user_token,
            "fields": "id,name,access_token,instagram_business_account{id,username,name,profile_picture_url}",
        }).get("data", [])

        accounts = load_json(ACCOUNTS_FILE, [])
        record = {
            "telegram_id": str(state_data["telegram_id"]),
            "meta_user": me,
            "user_access_token": user_token,
            "pages": pages,
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }
        accounts.append(record)
        save_json(ACCOUNTS_FILE, accounts)

        notify_admin(f"✅ Meta connected: {me.get('name')} | Pages: {len(pages)}")
        if str(state_data["telegram_id"]).isdigit():
            send_telegram(state_data["telegram_id"], f"✅ تم ربط Meta بنجاح\nالحساب: {me.get('name')}\nعدد الصفحات: {len(pages)}")

        return render_template_string("""
        <h1>✅ Account connected</h1>
        <p>Meta account: {{ name }}</p>
        <p>Pages found: {{ pages_count }}</p>
        <p>You can close this page and return to Telegram.</p>
        """, name=me.get("name"), pages_count=len(pages))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/accounts")
def accounts():
    data = load_json(ACCOUNTS_FILE, [])
    safe = []
    for account in data:
        item = {
            "telegram_id": account.get("telegram_id"),
            "meta_user": account.get("meta_user"),
            "connected_at": account.get("connected_at"),
            "pages": [],
        }
        for p in account.get("pages", []):
            item["pages"].append({
                "id": p.get("id"),
                "name": p.get("name"),
                "instagram_business_account": p.get("instagram_business_account"),
                "has_page_token": bool(p.get("access_token")),
            })
        safe.append(item)
    return jsonify({"ok": True, "accounts": safe})


@app.post("/post/facebook")
def post_facebook():
    body = request.get_json(silent=True) or {}
    message = body.get("message", "Test post from AutoPost DZ")
    telegram_id = body.get("telegram_id", "admin")
    page = get_first_page_for_telegram(telegram_id)
    result = graph_post(f"/{page['id']}/feed", data={"message": message, "access_token": page["access_token"]})
    return jsonify({"ok": True, "result": result})


@app.get("/telegram/set-webhook")
def telegram_set_webhook():
    if not BOT_TOKEN:
        return jsonify({"ok": False, "error": "TELEGRAM_BOT_TOKEN missing"}), 500
    webhook_url = f"{BASE_URL}/telegram/webhook"
    result = tg_api("setWebhook", {"url": webhook_url})
    return jsonify({"ok": True, "webhook_url": webhook_url, "telegram": result})


@app.post("/telegram/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    if not chat_id:
        return jsonify({"ok": True})

    bot_states = load_json(BOT_STATES_FILE, {})
    current_state = bot_states.get(str(chat_id))

    if text in ["/start", "start", "ابدأ"]:
        send_telegram(chat_id, "مرحبا 👋\nهذا بوت AutoPost DZ.\nاربط حساب Meta ثم تقدر تنشر في Facebook Page مباشرة.")
    elif text == "➕ ربط حساب Meta":
        send_telegram(chat_id, f"اضغط الرابط واربط حساب Meta:\n{BASE_URL}/auth/meta/start?telegram_id={chat_id}")
    elif text == "📄 حساباتي":
        acc = find_account_for_telegram(chat_id)
        if not acc:
            send_telegram(chat_id, "ما كاين حتى حساب مربوط. اضغط ➕ ربط حساب Meta.")
        else:
            pages = acc.get("pages", [])
            lines = [f"✅ Meta: {acc.get('meta_user', {}).get('name', 'Unknown')}", f"عدد الصفحات: {len(pages)}"]
            for i, p in enumerate(pages, 1):
                lines.append(f"{i}. {p.get('name')} | token: {'✅' if p.get('access_token') else '❌'}")
            send_telegram(chat_id, "\n".join(lines))
    elif text == "📝 نشر في Facebook":
        bot_states[str(chat_id)] = {"action": "await_fb_message"}
        save_json(BOT_STATES_FILE, bot_states)
        send_telegram(chat_id, "اكتب نص المنشور الآن، وأنا ننشرو في Facebook Page.")
    elif text == "❌ إلغاء":
        bot_states.pop(str(chat_id), None)
        save_json(BOT_STATES_FILE, bot_states)
        send_telegram(chat_id, "تم الإلغاء.")
    elif current_state and current_state.get("action") == "await_fb_message":
        try:
            page = get_first_page_for_telegram(chat_id)
            result = graph_post(f"/{page['id']}/feed", data={"message": text, "access_token": page["access_token"]})
            bot_states.pop(str(chat_id), None)
            save_json(BOT_STATES_FILE, bot_states)
            send_telegram(chat_id, f"✅ تم النشر في الصفحة\nPost ID: {result.get('id')}")
        except Exception as e:
            send_telegram(chat_id, f"❌ فشل النشر:\n{e}")
    else:
        send_telegram(chat_id, "اختار من الأزرار تحت 👇")
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)