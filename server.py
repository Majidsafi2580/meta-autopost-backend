import os
import json
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

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
USER_SETTINGS_FILE = DATA_DIR / "user_settings.json"
POSTS_LOG_FILE = DATA_DIR / "posts_log.json"
SCHEDULED_POSTS_FILE = DATA_DIR / "scheduled_posts.json"

SCOPES = ["pages_show_list", "pages_read_engagement", "pages_manage_posts", "business_management"]

TIMEZONES = {
    "🇩🇿 المغرب العربي": "Africa/Algiers",
    "🇲🇦 المغرب": "Africa/Casablanca",
    "🇹🇳 تونس": "Africa/Tunis",
    "🇪🇬 مصر": "Africa/Cairo",
    "🇸🇦 السعودية": "Asia/Riyadh",
    "🇦🇪 الإمارات": "Asia/Dubai",
    "🇺🇸 شرق أمريكا": "America/New_York",
    "🇺🇸 وسط أمريكا": "America/Chicago",
    "🇺🇸 غرب أمريكا": "America/Los_Angeles",
}

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


def short_id(length=8):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def require_config():
    missing = []
    for key, value in {"META_APP_ID": APP_ID, "META_APP_SECRET": APP_SECRET, "META_REDIRECT_URI": REDIRECT_URI}.items():
        if not value:
            missing.append(key)
    return missing


def meta_error_message(err):
    try:
        data = json.loads(str(err))
        e = data.get("error", {})
        return e.get("message") or str(err)
    except Exception:
        return str(err)


def graph_get(path, params):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{path.lstrip('/')}"
    r = requests.get(url, params=params, timeout=60)
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
    return data


def graph_post(path, data=None, params=None):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{path.lstrip('/')}"
    r = requests.post(url, data=data or {}, params=params or {}, timeout=120)
    out = r.json()
    if r.status_code >= 400:
        raise RuntimeError(json.dumps(out, ensure_ascii=False))
    return out


def tg_api(method, payload):
    if not BOT_TOKEN:
        return None
    try:
        return requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=30).json()
    except Exception:
        return None


def get_telegram_file_url(file_id):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    file_info = tg_api("getFile", {"file_id": file_id})
    if not file_info or not file_info.get("ok"):
        raise RuntimeError(f"Failed to get Telegram file: {file_info}")
    file_path = file_info["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


def main_keyboard():
    return {"keyboard": [
        [{"text": "➕ ربط حساب Meta"}, {"text": "📄 حساباتي"}],
        [{"text": "🔄 تحديث الحسابات"}, {"text": "🎯 اختيار وجهة النشر"}],
        [{"text": "🌍 اختيار التوقيت"}, {"text": "📊 حالة الربط"}],
        [{"text": "📝 نشر نص"}, {"text": "🖼 نشر صورة"}],
        [{"text": "🎬 نشر فيديو"}, {"text": "⏰ جدولة منشور"}],
        [{"text": "📋 المنشورات المجدولة"}, {"text": "🗑 حذف منشور مجدول"}],
        [{"text": "❌ إلغاء"}],
    ], "resize_keyboard": True}


def send_telegram(chat_id, text, keyboard=True):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = main_keyboard()
    return tg_api("sendMessage", payload)


def is_admin_allowed(chat_id):
    return True if not ADMIN_CHAT_ID else str(chat_id) == str(ADMIN_CHAT_ID)


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


def get_user_settings(telegram_id):
    settings = load_json(USER_SETTINGS_FILE, {})
    tid = str(telegram_id)
    if tid not in settings:
        settings[tid] = {"selected_pages": [], "platforms": ["facebook"], "timezone": "Africa/Algiers"}
        save_json(USER_SETTINGS_FILE, settings)
    return settings[tid]


def update_user_settings(telegram_id, updates):
    settings = load_json(USER_SETTINGS_FILE, {})
    tid = str(telegram_id)
    current = get_user_settings(tid)
    current.update(updates)
    settings[tid] = current
    save_json(USER_SETTINGS_FILE, settings)
    return current


def get_pages_for_user(telegram_id):
    acc = find_account_for_telegram(telegram_id)
    return acc.get("pages", []) if acc else []


def log_post(entry):
    logs = load_json(POSTS_LOG_FILE, [])
    entry["logged_at"] = now_utc_iso()
    logs.append(entry)
    save_json(POSTS_LOG_FILE, logs[-500:])


def pages_summary(telegram_id):
    acc = find_account_for_telegram(telegram_id)
    if not acc:
        return "❌ لا يوجد حساب Meta مربوط."
    pages = acc.get("pages", [])
    settings = get_user_settings(telegram_id)
    selected = set(str(x) for x in settings.get("selected_pages", []))
    platforms = ", ".join(settings.get("platforms", ["facebook"]))
    lines = [
        f"✅ Meta: {acc.get('meta_user', {}).get('name', 'Unknown')}",
        f"📄 عدد الصفحات: {len(pages)}",
        f"🌐 المنصات المختارة: {platforms}",
        f"🕒 التوقيت: {settings.get('timezone', 'Africa/Algiers')}", "", "الصفحات:"]
    for i, p in enumerate(pages, 1):
        mark = "✅" if (not selected and i == 1) or str(p.get("id")) in selected else "⬜"
        ig = p.get("instagram_business_account")
        ig_txt = f"IG: @{ig.get('username')}" if ig else "IG: غير مربوط"
        lines.append(f"{mark} {i}. {p.get('name')} | token: {'✅' if p.get('access_token') else '❌'} | {ig_txt}")
    return "\n".join(lines)


def publish_to_facebook_page(page, post_type, text=None, file_url=None):
    page_id, token = page["id"], page["access_token"]
    if post_type == "text":
        if not text:
            raise RuntimeError("النص فارغ.")
        return graph_post(f"/{page_id}/feed", data={"message": text, "access_token": token})
    if post_type == "photo":
        return graph_post(f"/{page_id}/photos", data={"url": file_url, "caption": text or "", "access_token": token})
    if post_type == "video":
        return graph_post(f"/{page_id}/videos", data={"file_url": file_url, "description": text or "", "access_token": token})
    raise RuntimeError(f"نوع غير مدعوم: {post_type}")


def publish_to_instagram(page, post_type, text=None, file_url=None):
    ig = page.get("instagram_business_account")
    if not ig:
        raise RuntimeError("Instagram غير مربوط بهذه الصفحة.")
    ig_id, token = ig["id"], page["access_token"]
    if post_type == "text":
        raise RuntimeError("Instagram لا يدعم منشور نص فقط.")
    if post_type == "photo":
        c = graph_post(f"/{ig_id}/media", data={"image_url": file_url, "caption": text or "", "access_token": token})
    elif post_type == "video":
        c = graph_post(f"/{ig_id}/media", data={"media_type": "REELS", "video_url": file_url, "caption": text or "", "access_token": token})
    else:
        raise RuntimeError(f"نوع غير مدعوم: {post_type}")
    p = graph_post(f"/{ig_id}/media_publish", data={"creation_id": c["id"], "access_token": token})
    return {"container": c, "publish": p}


def publish_content(telegram_id, post_type, text=None, file_url=None, selected_page_ids=None, platforms=None):
    pages = get_pages_for_user(telegram_id)
    if not pages:
        raise RuntimeError("لا توجد صفحات. أعد ربط Meta.")
    settings = get_user_settings(telegram_id)
    selected_page_ids = selected_page_ids or settings.get("selected_pages") or [pages[0]["id"]]
    platforms = platforms or settings.get("platforms") or ["facebook"]
    page_map = {str(p.get("id")): p for p in pages}
    chosen = [page_map[str(pid)] for pid in selected_page_ids if str(pid) in page_map] or [pages[0]]
    results = []
    for page in chosen:
        for platform in platforms:
            item = {"platform": platform, "page_id": page.get("id"), "page_name": page.get("name"), "success": False}
            try:
                if platform == "facebook":
                    result = publish_to_facebook_page(page, post_type, text=text, file_url=file_url)
                    item.update({"success": True, "post_id": result.get("id"), "raw": result})
                elif platform == "instagram":
                    result = publish_to_instagram(page, post_type, text=text, file_url=file_url)
                    item.update({"success": True, "post_id": result.get("publish", {}).get("id"), "raw": result})
                else:
                    item["error"] = f"منصة غير مدعومة: {platform}"
            except Exception as e:
                item["error"] = meta_error_message(e)
            results.append(item)
            log_post({"telegram_id": str(telegram_id), "type": post_type, "platform": platform, "page_id": page.get("id"), "page_name": page.get("name"), "success": item.get("success"), "post_id": item.get("post_id"), "error": item.get("error")})
    return results


def format_publish_results(results):
    ok = [r for r in results if r.get("success")]
    fail = [r for r in results if not r.get("success")]
    lines = [f"✅ نجح: {len(ok)}", f"❌ فشل: {len(fail)}"]
    for r in results:
        mark = "✅" if r.get("success") else "❌"
        post_id = f" | ID: {r.get('post_id')}" if r.get("post_id") else ""
        err = f" | {r.get('error')}" if r.get("error") else ""
        lines.append(f"{mark} {r.get('platform')} - {r.get('page_name')}{post_id}{err}")
    return "\n".join(lines)


@app.get("/")
def home():
    return jsonify({"ok": True, "name": "Meta AutoPost Full Telegram Bot", "missing_env": require_config(), "telegram_ready": bool(BOT_TOKEN)})

@app.get("/privacy")
def privacy():
    return render_template_string("<h1>Privacy Policy</h1><p>This app connects authorized Facebook Pages and Instagram accounts for publishing automation. We do not ask for passwords.</p>")

@app.get("/terms")
def terms():
    return render_template_string("<h1>Terms of Service</h1><p>Users are responsible for their content and must follow Meta rules.</p>")

@app.route("/data-deletion", methods=["GET", "POST"])
def data_deletion():
    if request.method == "GET":
        return render_template_string("<h1>User Data Deletion</h1><p>Send a deletion request to the app admin.</p>")
    code = secrets.token_hex(8)
    return jsonify({"url": f"{BASE_URL or request.host_url.rstrip('/')}/data-deletion/status/{code}", "confirmation_code": code})

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
    states[state] = {"telegram_id": str(telegram_id), "created_at": now_utc_iso()}
    save_json(STATE_FILE, states)
    params = {"client_id": APP_ID, "redirect_uri": REDIRECT_URI, "state": state, "scope": ",".join(SCOPES), "response_type": "code"}
    return redirect(f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth?{urlencode(params)}")

@app.get("/auth/meta/callback")
def meta_callback():
    if request.args.get("error"):
        return jsonify({"ok": False, "error": request.args.get("error"), "details": request.args.to_dict()}), 400
    code, state = request.args.get("code"), request.args.get("state")
    states = load_json(STATE_FILE, {})
    state_data = states.pop(state, None)
    save_json(STATE_FILE, states)
    if not code or not state_data:
        return jsonify({"ok": False, "error": "Invalid or expired state"}), 400
    try:
        token_data = graph_get("/oauth/access_token", {"client_id": APP_ID, "client_secret": APP_SECRET, "redirect_uri": REDIRECT_URI, "code": code})
        long_data = graph_get("/oauth/access_token", {"grant_type": "fb_exchange_token", "client_id": APP_ID, "client_secret": APP_SECRET, "fb_exchange_token": token_data["access_token"]})
        user_token = long_data["access_token"]
        me = graph_get("/me", {"access_token": user_token, "fields": "id,name"})
        pages = graph_get("/me/accounts", {"access_token": user_token, "fields": "id,name,access_token,instagram_business_account{id,username,name,profile_picture_url}"}).get("data", [])
        telegram_id = str(state_data["telegram_id"])
        accounts = [a for a in load_json(ACCOUNTS_FILE, []) if str(a.get("telegram_id")) != telegram_id]
        accounts.append({"telegram_id": telegram_id, "meta_user": me, "user_access_token": user_token, "pages": pages, "connected_at": now_utc_iso()})
        save_json(ACCOUNTS_FILE, accounts)
        if pages:
            s = get_user_settings(telegram_id)
            if not s.get("selected_pages"):
                update_user_settings(telegram_id, {"selected_pages": [pages[0]["id"]]})
        if telegram_id.isdigit():
            send_telegram(telegram_id, f"✅ تم ربط Meta بنجاح\nالحساب: {me.get('name')}\nعدد الصفحات: {len(pages)}")
        return render_template_string("<h1>✅ Account connected</h1><p>Meta account: {{name}}</p><p>Pages found: {{count}}</p>", name=me.get("name"), count=len(pages))
    except Exception as e:
        return jsonify({"ok": False, "error": meta_error_message(e)}), 500

@app.get("/accounts")
def accounts():
    safe = []
    for a in load_json(ACCOUNTS_FILE, []):
        safe.append({"telegram_id": a.get("telegram_id"), "meta_user": a.get("meta_user"), "connected_at": a.get("connected_at"), "pages": [{"id": p.get("id"), "name": p.get("name"), "instagram_business_account": p.get("instagram_business_account"), "has_page_token": bool(p.get("access_token"))} for p in a.get("pages", [])]})
    return jsonify({"ok": True, "accounts": safe})

@app.post("/post/facebook")
def post_facebook():
    body = request.get_json(silent=True) or {}
    results = publish_content(body.get("telegram_id", "admin"), "text", text=body.get("message", "Test post"), platforms=["facebook"])
    return jsonify({"ok": True, "results": results})

@app.get("/telegram/set-webhook")
def telegram_set_webhook():
    if not BOT_TOKEN:
        return jsonify({"ok": False, "error": "TELEGRAM_BOT_TOKEN missing"}), 500
    webhook_url = f"{BASE_URL}/telegram/webhook"
    return jsonify({"ok": True, "webhook_url": webhook_url, "telegram": tg_api("setWebhook", {"url": webhook_url})})

@app.get("/telegram/delete-webhook")
def telegram_delete_webhook():
    return jsonify({"ok": True, "telegram": tg_api("deleteWebhook", {})})


def parse_local_datetime_to_utc(text_time, tz_name):
    local_tz = ZoneInfo(tz_name)
    dt_local = datetime.strptime(text_time.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    return dt_local.isoformat(), dt_local.astimezone(timezone.utc).isoformat()

@app.get("/cron/publish-scheduled")
def cron_publish_scheduled():
    posts = load_json(SCHEDULED_POSTS_FILE, [])
    now = datetime.now(timezone.utc)
    processed, changed = [], False
    for post in posts:
        if post.get("status") != "pending":
            continue
        try:
            scheduled_utc = datetime.fromisoformat(post["scheduled_utc"])
            if scheduled_utc.tzinfo is None:
                scheduled_utc = scheduled_utc.replace(tzinfo=timezone.utc)
            if scheduled_utc > now:
                continue
            results = publish_content(post["telegram_id"], post["type"], text=post.get("text") or post.get("caption") or "", file_url=post.get("file_url"), selected_page_ids=post.get("selected_pages"), platforms=post.get("platforms"))
            post["results"] = results
            success_count = len([r for r in results if r.get("success")])
            post["status"] = "published" if success_count == len(results) and results else ("partial" if success_count else "failed")
            post["published_at"] = now_utc_iso()
            processed.append({"id": post.get("id"), "status": post.get("status")})
            changed = True
            if str(post.get("telegram_id", "")).isdigit():
                send_telegram(post["telegram_id"], f"⏰ تم تنفيذ منشور مجدول\nID: {post.get('id')}\n{format_publish_results(results)}")
        except Exception as e:
            post.update({"status": "failed", "error": meta_error_message(e), "failed_at": now_utc_iso()})
            processed.append({"id": post.get("id"), "status": "failed", "error": post["error"]})
            changed = True
    if changed:
        save_json(SCHEDULED_POSTS_FILE, posts)
    return jsonify({"ok": True, "processed": processed})


def set_state(chat_id, data):
    states = load_json(BOT_STATES_FILE, {})
    states[str(chat_id)] = data
    save_json(BOT_STATES_FILE, states)


def get_state(chat_id):
    return load_json(BOT_STATES_FILE, {}).get(str(chat_id))


def clear_state(chat_id):
    states = load_json(BOT_STATES_FILE, {})
    states.pop(str(chat_id), None)
    save_json(BOT_STATES_FILE, states)


def handle_content_for_state(chat_id, message, text, state):
    action = state.get("action")
    if action in ["await_text_now", "await_photo_now", "await_video_now"]:
        post_type = action.replace("await_", "").replace("_now", "")
        file_url = None
        if post_type == "photo":
            photos = message.get("photo") or []
            if not photos:
                raise RuntimeError("ابعث صورة، ماشي نص فقط.")
            file_url = get_telegram_file_url(photos[-1]["file_id"])
        elif post_type == "video":
            video = message.get("video")
            if not video:
                raise RuntimeError("ابعث فيديو، ماشي نص فقط.")
            file_url = get_telegram_file_url(video["file_id"])
        elif not text:
            raise RuntimeError("اكتب نص المنشور.")
        results = publish_content(chat_id, post_type, text=text, file_url=file_url)
        clear_state(chat_id)
        send_telegram(chat_id, "تمت محاولة النشر:\n" + format_publish_results(results))
        return True

    if action == "await_schedule_type":
        mapping = {"نص": "text", "صورة": "photo", "فيديو": "video", "1": "text", "2": "photo", "3": "video"}
        post_type = mapping.get(text)
        if not post_type:
            send_telegram(chat_id, "اختار: نص / صورة / فيديو")
            return True
        set_state(chat_id, {"action": f"await_schedule_content_{post_type}", "type": post_type})
        send_telegram(chat_id, "أرسل المحتوى المجدول الآن: نص، أو صورة مع caption، أو فيديو مع caption.")
        return True

    if action and action.startswith("await_schedule_content_"):
        post_type, file_url = state.get("type"), None
        if post_type == "photo":
            photos = message.get("photo") or []
            if not photos:
                raise RuntimeError("ابعث صورة للجدولة.")
            file_url = get_telegram_file_url(photos[-1]["file_id"])
        elif post_type == "video":
            video = message.get("video")
            if not video:
                raise RuntimeError("ابعث فيديو للجدولة.")
            file_url = get_telegram_file_url(video["file_id"])
        elif post_type == "text" and not text:
            raise RuntimeError("اكتب نص المنشور.")
        state.update({"action": "await_schedule_time", "text": text, "file_url": file_url})
        set_state(chat_id, state)
        send_telegram(chat_id, f"اكتب وقت النشر بهذا الشكل:\nYYYY-MM-DD HH:MM\nالتوقيت الحالي: {get_user_settings(chat_id).get('timezone')}")
        return True

    if action == "await_schedule_time":
        settings = get_user_settings(chat_id)
        local_iso, utc_iso = parse_local_datetime_to_utc(text, settings.get("timezone", "Africa/Algiers"))
        pages = get_pages_for_user(chat_id)
        post = {"id": short_id(), "telegram_id": str(chat_id), "type": state["type"], "platforms": settings.get("platforms", ["facebook"]), "selected_pages": settings.get("selected_pages") or ([pages[0]["id"]] if pages else []), "text": state.get("text") or "", "caption": state.get("text") or "", "file_url": state.get("file_url"), "timezone": settings.get("timezone"), "scheduled_local": text, "scheduled_local_iso": local_iso, "scheduled_utc": utc_iso, "status": "pending", "created_at": now_utc_iso(), "results": []}
        posts = load_json(SCHEDULED_POSTS_FILE, [])
        posts.append(post)
        save_json(SCHEDULED_POSTS_FILE, posts)
        clear_state(chat_id)
        send_telegram(chat_id, f"✅ تم حفظ الجدولة\nID: {post['id']}\nالوقت: {text}\nTimezone: {settings.get('timezone')}")
        return True

    if action == "await_delete_scheduled":
        posts = load_json(SCHEDULED_POSTS_FILE, [])
        found = False
        for p in posts:
            if p.get("id") == text:
                found = True
                if p.get("status") == "pending":
                    p.update({"status": "cancelled", "cancelled_at": now_utc_iso()})
                    save_json(SCHEDULED_POSTS_FILE, posts)
                    send_telegram(chat_id, f"✅ تم إلغاء المنشور: {text}")
                else:
                    send_telegram(chat_id, f"لا يمكن إلغاؤه، الحالة: {p.get('status')}")
                break
        if not found:
            send_telegram(chat_id, "لم أجد منشور بهذا ID.")
        clear_state(chat_id)
        return True
    return False

@app.post("/telegram/webhook")
def telegram_webhook():
    chat_id = None
    try:
        update = request.get_json(silent=True) or {}
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or message.get("caption") or "").strip()
        if not chat_id:
            return jsonify({"ok": True})
        if not is_admin_allowed(chat_id):
            send_telegram(chat_id, "❌ هذا البوت خاص، غير مسموح لك باستعماله.", keyboard=False)
            return jsonify({"ok": True})
        state = get_state(chat_id)

        if text in ["/start", "start", "ابدأ"]:
            clear_state(chat_id)
            send_telegram(chat_id, "مرحبا 👋\nهذا بوت AutoPost DZ للنشر والجدولة على Facebook/Instagram.")
        elif text == "❌ إلغاء":
            clear_state(chat_id); send_telegram(chat_id, "تم الإلغاء.")
        elif text == "➕ ربط حساب Meta":
            send_telegram(chat_id, f"اضغط الرابط واربط حساب Meta:\n{BASE_URL}/auth/meta/start?telegram_id={chat_id}")
        elif text == "📄 حساباتي":
            send_telegram(chat_id, pages_summary(chat_id))
        elif text == "🔄 تحديث الحسابات":
            acc = find_account_for_telegram(chat_id)
            if not acc:
                send_telegram(chat_id, "❌ لا يوجد حساب مربوط.")
            else:
                pages = graph_get("/me/accounts", {"access_token": acc["user_access_token"], "fields": "id,name,access_token,instagram_business_account{id,username,name,profile_picture_url}"}).get("data", [])
                accounts = load_json(ACCOUNTS_FILE, [])
                for a in accounts:
                    if str(a.get("telegram_id")) == str(chat_id):
                        a["pages"] = pages; a["updated_at"] = now_utc_iso()
                save_json(ACCOUNTS_FILE, accounts)
                send_telegram(chat_id, f"✅ تم تحديث الحسابات\nعدد الصفحات: {len(pages)}")
        elif text == "🎯 اختيار وجهة النشر":
            pages = get_pages_for_user(chat_id)
            if not pages: send_telegram(chat_id, "لا توجد صفحات. أعد ربط Meta.")
            else:
                update_user_settings(chat_id, {"selected_pages": [p["id"] for p in pages], "platforms": ["facebook"]})
                send_telegram(chat_id, "✅ تم اختيار كل الصفحات للنشر على Facebook حالياً.\nInstagram نفعله بعد حل الربط.")
        elif text == "🌍 اختيار التوقيت":
            set_state(chat_id, {"action": "await_timezone"})
            send_telegram(chat_id, "اكتب التوقيت مثل Africa/Algiers\n\n" + "\n".join([f"- {k}: {v}" for k, v in TIMEZONES.items()]))
        elif state and state.get("action") == "await_timezone":
            if text not in set(TIMEZONES.values()): send_telegram(chat_id, "توقيت غير صحيح. مثال: Africa/Algiers")
            else:
                update_user_settings(chat_id, {"timezone": text}); clear_state(chat_id); send_telegram(chat_id, f"✅ تم اختيار التوقيت: {text}")
        elif text == "📊 حالة الربط":
            acc, settings = find_account_for_telegram(chat_id), get_user_settings(chat_id)
            logs = [l for l in load_json(POSTS_LOG_FILE, []) if str(l.get("telegram_id")) == str(chat_id)]
            lines = [f"Telegram Bot Token: {'✅' if BOT_TOKEN else '❌'}", f"Meta Env Vars: {'✅' if not require_config() else '❌'}", f"الحساب مربوط: {'✅' if acc else '❌'}", f"عدد الصفحات: {len(acc.get('pages', [])) if acc else 0}", f"الصفحات المختارة: {len(settings.get('selected_pages', []))}", f"المنصات: {', '.join(settings.get('platforms', []))}", f"Timezone: {settings.get('timezone')}"]
            if logs:
                last = logs[-1]; lines.append(f"آخر نشر: {'✅' if last.get('success') else '❌'} {last.get('platform')} {last.get('page_name')}")
                if last.get("error"): lines.append(f"آخر خطأ: {last.get('error')}")
            send_telegram(chat_id, "\n".join(lines))
        elif text == "📝 نشر نص":
            set_state(chat_id, {"action": "await_text_now"}); send_telegram(chat_id, "اكتب نص المنشور الآن.")
        elif text == "🖼 نشر صورة":
            set_state(chat_id, {"action": "await_photo_now"}); send_telegram(chat_id, "ابعث الصورة الآن، ومعاها caption إذا حبيت.")
        elif text == "🎬 نشر فيديو":
            set_state(chat_id, {"action": "await_video_now"}); send_telegram(chat_id, "ابعث الفيديو الآن، ومعاه caption إذا حبيت.")
        elif text == "⏰ جدولة منشور":
            set_state(chat_id, {"action": "await_schedule_type"}); send_telegram(chat_id, "اختار نوع المنشور للجدولة:\nنص\nصورة\nفيديو")
        elif text == "📋 المنشورات المجدولة":
            posts = [p for p in load_json(SCHEDULED_POSTS_FILE, []) if str(p.get("telegram_id")) == str(chat_id)]
            if not posts: send_telegram(chat_id, "لا توجد منشورات مجدولة.")
            else: send_telegram(chat_id, "آخر المنشورات المجدولة:\n" + "\n".join([f"ID: {p.get('id')} | {p.get('type')} | {p.get('scheduled_local')} | {p.get('timezone')} | {p.get('status')}" for p in posts[-10:]]))
        elif text == "🗑 حذف منشور مجدول":
            set_state(chat_id, {"action": "await_delete_scheduled"}); send_telegram(chat_id, "أرسل ID تاع المنشور المجدول لإلغائه.")
        elif state:
            handle_content_for_state(chat_id, message, text, state)
        else:
            send_telegram(chat_id, "اختار من الأزرار تحت 👇")
    except Exception as e:
        if chat_id:
            send_telegram(chat_id, f"❌ خطأ:\n{meta_error_message(e)}")
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
