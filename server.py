import os
import json
import hmac
import base64
import hashlib
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

SCOPES = [
    "pages_show_list",

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


def send_telegram(text):
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass


@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "name": "Meta AutoPost Backend",
        "routes": [
            "/privacy",
            "/terms",
            "/data-deletion",
            "/auth/meta/start?telegram_id=YOUR_TELEGRAM_ID",
            "/auth/meta/callback",
            "/accounts",
            "/post/facebook",
            "/post/instagram/photo",
        ],
        "missing_env": require_config(),
    })


@app.get("/privacy")
def privacy():
    html = """
    <h1>Privacy Policy</h1>
    <p>This app is used to connect Facebook Pages and Instagram professional accounts for content publishing automation.</p>
    <p>We only store the access token and account/page identifiers needed to publish content selected by the account owner.</p>
    <p>We do not sell user data. We do not ask for Facebook or Instagram passwords.</p>
    <p>To request deletion, visit <a href="/data-deletion">/data-deletion</a>.</p>
    """
    return render_template_string(html)


@app.get("/terms")
def terms():
    html = """
    <h1>Terms of Service</h1>
    <p>This tool helps authorized users schedule and publish their own content to Facebook Pages and Instagram professional accounts.</p>
    <p>Users are responsible for the content they publish and must follow Meta platform rules and local laws.</p>
    <p>The app may stop working if Meta permissions, access tokens, or API policies change.</p>
    """
    return render_template_string(html)


@app.route("/data-deletion", methods=["GET", "POST"])
def data_deletion():
    # Meta can send signed_request here. For testing, this route also supports a simple GET page.
    if request.method == "GET":
        return render_template_string("""
        <h1>User Data Deletion</h1>
        <p>Send a deletion request to the app admin with your Facebook user ID or Telegram ID.</p>
        <p>Endpoint status: active.</p>
        """)

    signed_request = request.form.get("signed_request", "")
    confirmation_code = secrets.token_hex(8)

    # Minimal valid response format for Meta deletion callback.
    # You can later improve it by decoding signed_request and deleting the exact user.
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
    states[state] = {
        "telegram_id": telegram_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(STATE_FILE, states)

    params = {
        "client_id": APP_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": ",".join(SCOPES),
        "response_type": "code",
    }
    url = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth?{urlencode(params)}"
    return redirect(url)


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

        me = graph_get("/me", {
            "access_token": user_token,
            "fields": "id,name",
        })

        pages = graph_get("/me/accounts", {
            "access_token": user_token,
            "fields": "id,name,access_token,instagram_business_account{id,username,name,profile_picture_url}",
        }).get("data", [])

        accounts = load_json(ACCOUNTS_FILE, [])
        record = {
            "telegram_id": state_data["telegram_id"],
            "meta_user": me,
            "user_access_token": user_token,
            "pages": pages,
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }
        accounts.append(record)
        save_json(ACCOUNTS_FILE, accounts)

        send_telegram(f"✅ Meta connected: {me.get('name')} | Pages: {len(pages)}")

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


def get_first_page():
    data = load_json(ACCOUNTS_FILE, [])
    if not data:
        raise RuntimeError("No connected Meta account. Open /auth/meta/start first.")
    pages = data[-1].get("pages", [])
    if not pages:
        raise RuntimeError("No Facebook pages found for this account.")
    return pages[0]


@app.post("/post/facebook")
def post_facebook():
    body = request.get_json(silent=True) or {}
    message = body.get("message", "Test post from AutoPost DZ backend")
    page = get_first_page()
    page_id = page["id"]
    page_token = page["access_token"]

    result = graph_post(f"/{page_id}/feed", data={
        "message": message,
        "access_token": page_token,
    })
    return jsonify({"ok": True, "result": result})


@app.post("/post/instagram/photo")
def post_instagram_photo():
    body = request.get_json(silent=True) or {}
    image_url = body.get("image_url")
    caption = body.get("caption", "")
    if not image_url:
        return jsonify({"ok": False, "error": "image_url is required"}), 400

    page = get_first_page()
    ig = page.get("instagram_business_account")
    if not ig:
        return jsonify({"ok": False, "error": "No Instagram business account linked to this page"}), 400

    ig_id = ig["id"]
    page_token = page["access_token"]

    container = graph_post(f"/{ig_id}/media", data={
        "image_url": image_url,
        "caption": caption,
        "access_token": page_token,
    })

    publish = graph_post(f"/{ig_id}/media_publish", data={
        "creation_id": container["id"],
        "access_token": page_token,
    })

    return jsonify({"ok": True, "container": container, "publish": publish})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
