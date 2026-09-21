import os, time, uuid, threading, logging, base64, mimetypes, hmac, hashlib, json, urllib.parse, contextvars
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
try:
    import qrcode
except Exception:
    qrcode = None
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument
from bson import ObjectId

try:
    from agora_token_builder import RtcTokenBuilder, Role_Publisher
except Exception:
    RtcTokenBuilder = None
    Role_Publisher = 1

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vynora")

BASE = Path(__file__).resolve().parent
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "7778606261,7001825467").split(",") if x.strip().isdigit()}
HOST_SHARE = float(os.getenv("HOST_SHARE", "0.60"))
GROUP_1_ID = os.getenv("GROUP_1_ID", "")
GROUP_2_ID = os.getenv("GROUP_2_ID", "")
GROUP_3_ID = os.getenv("GROUP_3_ID", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", os.getenv("MONGO_URL", ""))
MONGO_DB = os.getenv("MONGO_DB", "vynora_live")
AGORA_APP_ID = os.getenv("AGORA_APP_ID", "")
AGORA_APP_CERTIFICATE = os.getenv("AGORA_APP_CERTIFICATE", "")
UPI_ID = os.getenv("UPI_ID", "vynoralive@slc")
UPI_NAME = os.getenv("UPI_NAME", "Rajnish Kumar")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/VynoraSupport")

BOOKING_PLANS = [
    {"minutes": 1, "tokens": 20, "name": "Demo", "demo": True},
    {"minutes": 2, "tokens": 299, "name": "2 Minutes", "demo": False},
    {"minutes": 5, "tokens": 499, "name": "5 Minutes", "demo": False},
    {"minutes": 10, "tokens": 799, "name": "10 Minutes", "demo": False},
    {"minutes": 30, "tokens": 2499, "name": "30 Minutes", "demo": False},
]

def get_booking_plans():
    if db is None:
        return BOOKING_PLANS
    try:
        row = db["settings"].find_one({"key":"booking_plans"})
        plans = row.get("value") if row else None
        if isinstance(plans, list) and plans:
            return plans
    except Exception:
        pass
    return BOOKING_PLANS


GIFT_PLANS = [
    {"id":"rose","name":"Rose","emoji":"🌹","tokens":10},
    {"id":"heart","name":"Heart","emoji":"❤️","tokens":20},
    {"id":"coffee","name":"Coffee","emoji":"☕","tokens":50},
    {"id":"diamond","name":"Diamond","emoji":"💎","tokens":100},
    {"id":"crown","name":"Crown","emoji":"👑","tokens":250},
    {"id":"rocket","name":"Rocket","emoji":"🚀","tokens":500},
]
RECHARGE_PLANS = [
    {"rupees": 50, "tokens": 50},
    {"rupees": 100, "tokens": 100},
    {"rupees": 200, "tokens": 200},
    {"rupees": 500, "tokens": 500},
    {"rupees": 1000, "tokens": 1000},
    {"rupees": 1500, "tokens": 1500},
    {"rupees": 2000, "tokens": 2000},
    {"rupees": 5000, "tokens": 5000},
]

app = FastAPI(title="Vynora Live 1v1", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

_verified_web_user = contextvars.ContextVar("verified_web_user", default=None)

def clean_json(value):
    """Convert Mongo/Python values into JSON-safe values before FastAPI serializes them."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean_json(v) for v in value]
    return value

def private_host_view(h):
    if not h:
        return None
    return clean_json(h)

def public_host_view(h, u=None):
    u = u or {}
    tid = int(h.get("user_id") or u.get("user_id"))
    raw_country = str(h.get("country") or u.get("country") or "IN").strip().upper()
    country = "IN" if raw_country in {"IN", "INDIA", "🇮🇳"} else raw_country
    return {
        "user_id": tid, "telegram_id": tid,
        "name": h.get("name") or u.get("name", "Host"),
        "username": h.get("username") or u.get("username", ""),
        "photo_url": public_photo_url(tid, u) or h.get("photo_url", ""),
        "online": bool(h.get("online")), "country": country,
        "country_name": h.get("country_name") or ("India" if country == "IN" else country),
        "demo": bool(h.get("demo", False)), "status": h.get("status"),
        "verified": h.get("status") == "approved",
        "verification_label": "Vynora Verified Host" if h.get("status") == "approved" else ""
    }

def verify_telegram_init_data(init_data: str):
    if not BOT_TOKEN or not init_data:
        return None
    try:
        pairs=dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received=pairs.pop("hash", "")
        if not received:
            return None
        data_check="\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret=hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected=hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        auth_date=int(pairs.get("auth_date", "0") or 0)
        if auth_date and now_ts()-auth_date > 86400:
            return None
        user=json.loads(pairs.get("user", "{}"))
        return int(user.get("id")) if user.get("id") else None
    except Exception:
        return None

@app.middleware("http")
async def telegram_webapp_auth(request: Request, call_next):
    path=request.url.path
    if path.startswith("/api/") and path not in ("/api/config","/api/health","/api/telegram/webhook","/api/telegram/webhook-info"):
        verified=verify_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))
        if verified is None:
            return Response(content=json.dumps({"detail":"Valid Telegram Mini App session is required"}), status_code=401, media_type="application/json")
        _verified_web_user.set(verified)
    response=await call_next(request)
    return response

if MONGO_URI:
    mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
    db = mongo[MONGO_DB]
else:
    mongo = None
    db = None


def col(name):
    if db is None:
        raise HTTPException(503, "MongoDB is not configured. Set MONGO_URI.")
    return db[name]


def now_ts():
    return int(time.time())


def esc_html(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def uid(v):
    try: return int(v)
    except Exception: raise HTTPException(400, "Invalid user id")


def user_doc(user_id):
    return col("users").find_one({"user_id": uid(user_id)})


def ensure_user(user_id, name="", username="", country="IN"):
    user_id = uid(user_id)
    existing = col("users").find_one({"user_id": user_id})
    if not existing:
        d = {"user_id": user_id, "name": name or "User", "username": username or "", "tokens": 0, "blocked": False, "country": (country or "IN").upper()[:2],
            "demo_used": False, "demo_used_at": None, "demo_booking_id": None,
            "created_at": now_ts(), "updated_at": now_ts()}
        col("users").insert_one(d)
        return d, True
    updates = {"updated_at": now_ts()}
    if "demo_used" not in existing: updates["demo_used"] = False
    if "demo_used_at" not in existing: updates["demo_used_at"] = None
    if "demo_booking_id" not in existing: updates["demo_booking_id"] = None
    if name: updates["name"] = name
    if username is not None: updates["username"] = username
    col("users").update_one({"user_id": user_id}, {"$set": updates})
    return col("users").find_one({"user_id": user_id}), False


def is_admin(user_id):
    target=uid(user_id)
    verified=_verified_web_user.get()
    if verified is not None and verified != target:
        return False
    return target in ADMIN_IDS


def blocked(user_id):
    d = user_doc(user_id) or {}
    return bool(d.get("blocked"))


def host_doc(host_id):
    return col("hosts").find_one({"user_id": uid(host_id)})


def host_or_404(host_id):
    h = host_doc(host_id)
    if not h or h.get("status") != "approved":
        raise HTTPException(404, "Host not available")
    if blocked(host_id):
        raise HTTPException(403, "Host is blocked")
    return h


def tg_send_photo(chat_id, photo_bytes, filename="payment.jpg", caption="", buttons=None):
    if not BOT_TOKEN or not chat_id or not photo_bytes:
        return None
    try:
        data = {"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML"}
        if buttons:
            data["reply_markup"] = __import__("json").dumps({"inline_keyboard": buttons})
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data=data,
            files={"photo": (filename, photo_bytes, "image/jpeg")},
            timeout=25,
        )
        if not r.ok:
            log.error("Telegram sendPhoto failed chat=%s status=%s body=%s", chat_id, r.status_code, r.text[:500])
            return None
        body = r.json()
        return body.get("result") or None
    except Exception as e:
        log.exception("Telegram sendPhoto exception chat=%s: %s", chat_id, e)
        return None

def tg_delete_message(chat_id, message_id):
    if not BOT_TOKEN or not chat_id or not message_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
            json={"chat_id": str(chat_id), "message_id": int(message_id)},
            timeout=15,
        )
        if not r.ok:
            log.warning("Telegram deleteMessage failed chat=%s message=%s status=%s body=%s", chat_id, message_id, r.status_code, r.text[:500])
        return r.ok
    except Exception as e:
        log.warning("Telegram deleteMessage exception chat=%s message=%s: %s", chat_id, message_id, e)
        return False

def remove_recharge_screenshot(recharge_id, delete_group_message=True):
    r = col("recharges").find_one({"recharge_id": recharge_id})
    if not r:
        return False
    # Remove the large base64 image from MongoDB after the recharge is finalized.
    col("recharges").update_one(
        {"recharge_id": recharge_id},
        {"$unset": {"screenshot_data": "", "screenshot_filename": "", "screenshot_content_type": "", "screenshot_url": ""},
         "$set": {"screenshot_deleted_at": now_ts()}}
    )
    # Also remove the payment screenshot message from Group 1 when possible.
    if delete_group_message:
        for mid in (r.get("telegram_screenshot_message_id"), r.get("telegram_action_message_id")):
            if mid:
                tg_delete_message(GROUP_1_ID, mid)
    return True

def notify_group(chat_id, text, buttons=None):
    if not BOT_TOKEN:
        log.error('Telegram group notify skipped: BOT_TOKEN is missing')
        return False
    if not chat_id:
        log.error('Telegram group notify skipped: group chat ID is missing')
        return False
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=15)
        if not r.ok: log.error('Telegram group notify HTTP %s: %s', r.status_code, r.text[:500])
        return r.ok
    except Exception as e:
        log.warning("telegram group notify failed: %s", e)
        return False


def create_notification(user_id, kind, title, message, data=None):
    try:
        col("notifications").insert_one({"user_id":uid(user_id),"kind":kind,"title":title,"message":message,"data":data or {},"read":False,"created_at":now_ts()})
    except Exception as e:
        log.warning("notification create failed: %s", e)


def notify_user(user_id, text, buttons=None):
    create_notification(user_id, "telegram", "Vynora Live", text, {})
    if not BOT_TOKEN:
        return False
    payload = {"chat_id": uid(user_id), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=15)
        return r.ok
    except Exception as e:
        log.warning("telegram user notify failed: %s", e)
        return False


def notify_new_user(u):
    text = ("🆕 <b>NEW USER</b>\n\n"
            f"Name: {u.get('name','-')}\nUser ID: <code>{u['user_id']}</code>\n"
            f"Username: @{u.get('username','').lstrip('@') or '-'}\n"
            f"Joined: {iso(u.get('created_at', now_ts()))}")
    notify_group(GROUP_2_ID, text)


def notify_request(text, buttons=None):
    notify_group(GROUP_1_ID, text, buttons)


def notify_full(text):
    notify_group(GROUP_3_ID, text)


def profile_photo(user_id):
    d = user_doc(user_id) or {}
    return d.get("photo_url", "")

def public_photo_url(user_id, doc=None):
    d = doc or user_doc(user_id) or {}
    if d.get("photo_data"):
        return f"/api/profile/photo/{int(user_id)}?v={int(d.get('photo_updated_at', 0))}"
    return d.get("photo_url", "")


def require_user(user_id):
    verified=_verified_web_user.get()
    if verified is not None and uid(user_id) != verified:
        raise HTTPException(403, "Telegram user identity mismatch")
    if blocked(user_id):
        raise HTTPException(403, "Your account is blocked")
    return user_doc(user_id) or ensure_user(user_id)[0]


def overlap(host_id, start_ts, end_ts):
    return col("bookings").find_one({
        "host_id": uid(host_id),
        "status": {"$in": ["accepted", "scheduled", "calling"]},
        "scheduled_start": {"$lt": int(end_ts)},
        "scheduled_end": {"$gt": int(start_ts)},
    }) is not None


class StartModel(BaseModel):
    user_id: int
    name: str = ""
    username: str = ""
    country: str = "IN"

class PhotoModel(BaseModel):
    user_id: int
    photo_url: str = ""

class BookingModel(BaseModel):
    user_id: int
    host_id: int
    minutes: int
    requested_start: int

class BookingAction(BaseModel):
    host_id: int
    booking_id: str
    action: str
    scheduled_start: Optional[int] = None

class RechargeModel(BaseModel):
    user_id: int
    rupees: int
    tokens: int
    utr: str = ""
    screenshot_url: str = ""

class TokenModel(BaseModel):
    user_id: int
    amount: int

class BlockModel(BaseModel):
    target_id: int
    reason: str = "Admin action"

class HostApplyModel(BaseModel):
    user_id: int
    name: str
    username: str = ""
    phone: str = ""
    age: Optional[int] = None
    bio: str = ""

class AnnouncementModel(BaseModel):
    user_id: int
    message: str

class ScheduleModel(BaseModel):
    host_id: int
    booking_id: str
    scheduled_start: int

class DirectCallModel(BaseModel):
    admin_id: int
    target_id: int

class CallJoinModel(BaseModel):
    user_id: int
    booking_id: str

class CallEndModel(BaseModel):
    user_id: int
    booking_id: str


@app.get("/")
def root():
    return FileResponse(BASE / "index.html", headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0"})

@app.get("/api/notifications/{user_id}")
def notifications(user_id:int):
    require_user(user_id)
    rows=[]
    for n in col("notifications").find({"user_id":uid(user_id)}).sort("created_at",-1).limit(50):
        n["_id"]=str(n["_id"]); rows.append(n)
    return rows

@app.post("/api/notifications/read/{user_id}")
def notifications_read(user_id:int):
    require_user(user_id); col("notifications").update_many({"user_id":uid(user_id),"read":False},{"$set":{"read":True}}); return {"status":"success"}

@app.get("/api/health")
def health():
    mongo_ok = False
    if mongo:
        try: mongo.admin.command("ping"); mongo_ok = True
        except Exception: pass
    return {"ok": True, "mongo": mongo_ok, "admins": sorted(ADMIN_IDS), "booking_plans": get_booking_plans()}

@app.get("/api/config")
def config():
    return {"booking_plans": get_booking_plans(), "recharge_plans": RECHARGE_PLANS, "gift_plans": GIFT_PLANS, "host_share": HOST_SHARE, "upi_id": UPI_ID, "upi_name": UPI_NAME, "support_url": SUPPORT_URL}

@app.post("/api/start")
def start(data: StartModel):
    verified=_verified_web_user.get()
    if verified is None or verified != uid(data.user_id):
        raise HTTPException(401,"Telegram identity verification failed")
    u, created = ensure_user(data.user_id, data.name, data.username, data.country)
    if created:
        notify_new_user(u)
    return {"status": "success", "new_user": created, "user": {"user_id": u["user_id"], "name": u.get("name"), "username": u.get("username"), "tokens": u.get("tokens", 0), "blocked": u.get("blocked", False), "country": u.get("country", "IN"), "photo_url": public_photo_url(data.user_id, u),
            "demo_used": bool(u.get("demo_used", False))}}

@app.get("/api/user/{user_id}")
def get_user(user_id: int):
    u = require_user(user_id)
    h = host_doc(user_id)
    return {"user": {"user_id": int(u["user_id"]), "name": u.get("name"), "username": u.get("username"), "tokens": int(u.get("tokens",0)), "country": u.get("country", "IN"), "photo_url": public_photo_url(user_id, u),
            "demo_used": bool(u.get("demo_used", False))}, "host": private_host_view(h), "is_admin": is_admin(user_id)}

@app.post("/api/profile/photo")
def set_photo(data: PhotoModel):
    require_user(data.user_id)
    url = data.photo_url.strip()[:1000]
    col("users").update_one(
        {"user_id": uid(data.user_id)},
        {"$set": {"photo_url": url, "photo_updated_at": now_ts(), "updated_at": now_ts()}}
    )
    return {"status": "success", "photo_url": url}

@app.post("/api/profile/photo-upload")
async def upload_profile_photo(
    user_id: Optional[int] = Form(None),
    user_id_query: Optional[int] = Query(None, alias="user_id"),
    file: UploadFile = File(...),
):
    # Accept the Telegram user ID from either multipart FormData or query string.
    # The verified Telegram WebApp ID remains the source of truth in require_user().
    resolved_id = user_id if user_id is not None else user_id_query
    if resolved_id is None:
        verified = _verified_web_user.get()
        if verified is None:
            raise HTTPException(400, "Telegram user ID is required")
        resolved_id = verified
    require_user(resolved_id)
    if not file.content_type or not file.content_type.lower().startswith("image/"):
        raise HTTPException(400, "Please upload an image file")
    raw = await file.read()
    if len(raw) > 900_000:
        raise HTTPException(400, "Photo is too large. Please use an image below 900 KB.")
    if len(raw) < 32:
        raise HTTPException(400, "Invalid or empty image file")
    content_type = file.content_type.split(";", 1)[0].lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        raise HTTPException(400, "Please upload JPG, PNG, WEBP or GIF image")
    encoded = base64.b64encode(raw).decode("ascii")
    data_uri = f"data:{content_type};base64,{encoded}"
    col("users").update_one(
        {"user_id": uid(resolved_id)},
        {"$set": {
            "photo_data": data_uri,
            "photo_url": "",
            "photo_filename": file.filename or "profile.jpg",
            "photo_content_type": content_type,
            "photo_updated_at": now_ts(),
            "updated_at": now_ts()
        }}
    )
    return {"status": "success", "photo_url": public_photo_url(resolved_id)}

@app.get("/api/profile/photo/{user_id}")
def get_profile_photo(user_id: int):
    d = user_doc(user_id) or {}
    data_uri = d.get("photo_data", "")
    if not data_uri.startswith("data:image/"):
        raise HTTPException(404, "Profile photo not found")
    try:
        header, encoded = data_uri.split(",", 1)
        content_type = header.split(";", 1)[0].replace("data:", "") or "image/jpeg"
        raw = base64.b64decode(encoded)
        from fastapi.responses import Response
        return Response(content=raw, media_type=content_type, headers={"Cache-Control": "public, max-age=300"})
    except Exception:
        raise HTTPException(500, "Invalid profile photo")

@app.get("/api/hosts")
def hosts():
    rows = []
    # Real approved India hosts only. Offline hosts remain visible so the
    # customer can see that the host exists; booking is enabled only online.
    docs = list(col("hosts").find({
        "status":"approved",
        "demo":{"$ne":True}
    }).sort("updated_at", -1))
    for h in docs:
        u = user_doc(h["user_id"]) or {}
        country = str(h.get("country") or u.get("country") or "IN").strip().upper()
        if country in {"INDIA", "🇮🇳"}: country = "IN"
        if country != "IN":
            continue
        rows.append(public_host_view(h, u))
    rows.sort(key=lambda x: (not bool(x.get("online")), -int(x.get("user_id",0))))
    return rows

@app.get("/api/host/{host_id}")
def host_profile(host_id: int):
    h = host_or_404(host_id); u = user_doc(host_id) or {}
    return {**public_host_view(h, u), "filter_name": h.get("filter_name","natural")}

@app.post("/api/host/apply")
def host_apply(data: HostApplyModel):
    require_user(data.user_id)
    existing = host_doc(data.user_id)
    d = {"user_id":uid(data.user_id),"name":data.name,"username":data.username,"phone":data.phone,"age":data.age,"bio":data.bio,"status":"pending","online":False,"country":"IN","country_name":"India","total_tokens":0,"available_earnings":0,"created_at":now_ts(),"updated_at":now_ts()}
    if existing and existing.get("status") == "approved": raise HTTPException(400,"Already an approved host")
    col("hosts").update_one({"user_id":uid(data.user_id)}, {"$set":d}, upsert=True)
    text = f"🎙️ <b>HOST APPLICATION</b>\n\nName: {data.name}\nUser ID: <code>{data.user_id}</code>\nUsername: @{data.username.lstrip('@') or '-'}\nPhone: {data.phone or '-'}\nAge: {data.age or '-'}\nBio: {data.bio or '-'}"
    notify_request(text, [[{"text":"✅ Approve Host","callback_data":f"approve_host:{data.user_id}"},{"text":"❌ Reject","callback_data":f"reject_host:{data.user_id}"}]])
    notify_full(text)
    return {"status":"success","message":"Host application submitted"}

class HostFilterModel(BaseModel):
    user_id: int
    filter_name: str = "natural"

FILTERS = {
    "natural": "none",
    "glow": "brightness(1.08) saturate(1.18) contrast(1.04) drop-shadow(0 0 7px rgba(255,210,180,.35))",
    "soft": "brightness(1.06) saturate(1.08) contrast(.98)",
    "warm": "brightness(1.06) saturate(1.12) sepia(.08)",
}

@app.get("/api/host/filter/{host_id}")
def get_host_filter(host_id: int):
    h = host_doc(host_id) or {}
    return {"filter_name": h.get("filter_name", "natural"), "css": FILTERS.get(h.get("filter_name", "natural"), "none")}

@app.post("/api/host/filter")
def set_host_filter(data: HostFilterModel):
    require_user(data.user_id)
    h = host_doc(data.user_id)
    if not h or h.get("status") != "approved":
        raise HTTPException(403, "Approved host access required")
    name = data.filter_name if data.filter_name in FILTERS else "natural"
    col("hosts").update_one({"user_id": uid(data.user_id)}, {"$set": {"filter_name": name, "updated_at": now_ts()}})
    return {"status":"success", "filter_name":name, "css":FILTERS[name]}

@app.get("/api/gifts")
def gifts():
    return GIFT_PLANS

class GiftModel(BaseModel):
    user_id: int
    host_id: int
    gift_id: str
    booking_id: str = ""

@app.post("/api/gift")
def send_gift(data: GiftModel):
    u = require_user(data.user_id)
    h = host_or_404(data.host_id)
    if uid(data.user_id) == uid(data.host_id):
        raise HTTPException(400, "You cannot gift yourself")
    gift = next((g for g in GIFT_PLANS if g["id"] == data.gift_id), None)
    if not gift:
        raise HTTPException(400, "Invalid gift")
    if (u.get("country") or "IN").upper() != (h.get("country") or "IN").upper():
        raise HTTPException(403, "Gifts are available only for India-based host/user pairs")
    cost = int(gift["tokens"])
    changed = col("users").update_one({"user_id":uid(data.user_id),"tokens":{"$gte":cost}}, {"$inc":{"tokens":-cost}})
    if changed.modified_count != 1:
        raise HTTPException(400, "Insufficient tokens")
    host_earned = round(cost * HOST_SHARE, 2)
    gift_id = str(uuid.uuid4())
    col("gifts").insert_one({
        "gift_id":gift_id,"user_id":uid(data.user_id),"host_id":uid(data.host_id),
        "gift_type":gift["id"],"gift_name":gift["name"],"emoji":gift["emoji"],
        "tokens":cost,"host_earned":host_earned,"platform_earned":round(cost*(1-HOST_SHARE),2),
        "booking_id":data.booking_id or "","created_at":now_ts()
    })
    col("hosts").update_one({"user_id":uid(data.host_id)}, {"$inc":{"gift_tokens":host_earned,"total_tokens":host_earned,"available_earnings":host_earned}, "$set":{"updated_at":now_ts()}})
    sender = u.get("name", "User"); host_name = h.get("name", "Host")
    notify_user(data.user_id, f"🎁 {gift['emoji']} <b>{gift['name']} sent!</b>\n{host_name} को {cost} Coins का gift भेजा गया।")
    notify_user(data.host_id, f"🎁 <b>New Gift Received!</b>\n\n{sender} ने आपको {gift['emoji']} {gift['name']} भेजा।\nValue: {cost} Coins\nYour earning: {host_earned:.2f} Coins")
    notify_full(f"🎁 GIFT SENT\nUser: {data.user_id}\nHost: {data.host_id}\nGift: {gift['emoji']} {gift['name']}\nValue: {cost} Coins\nHost earning: {host_earned:.2f}\nBooking: {data.booking_id or '-'}")
    return {"status":"success","gift":gift,"host_earned":host_earned}

@app.get("/api/bookings")
def bookings(user_id: int, role: str = "user"):
    require_user(user_id)
    q = {"user_id":uid(user_id)} if role != "host" else {"host_id":uid(user_id)}
    arr=[]
    for b in col("bookings").find(q).sort("created_at", -1).limit(50):
        b["_id"] = str(b["_id"]); arr.append(b)
    return arr

@app.post("/api/book-slot")
def book_slot(data: BookingModel):
    require_user(data.user_id); h=host_or_404(data.host_id)
    plan=next((p for p in get_booking_plans() if p["minutes"]==int(data.minutes)),None)
    if not plan: raise HTTPException(400,"Invalid duration")
    plan_minutes=int(plan["minutes"])
    plan_tokens=int(plan["tokens"])
    is_demo=bool(plan.get("demo", False))
    start=int(data.requested_start); end=start+plan_minutes*60
    if start < now_ts()-60: raise HTTPException(400,"Please choose a future time")
    u=user_doc(data.user_id)
    norm_country=lambda v: "IN" if str(v or "IN").strip().upper() in {"IN","INDIA","🇮🇳"} else str(v or "IN").strip().upper()
    user_country=norm_country(u.get("country"))
    host_country=norm_country(h.get("country"))
    # Dummy/test hosts are never bookable. Only real approved India hosts can receive bookings.
    if bool(h.get("demo", False)):
        raise HTTPException(403, "This Host is not available for real bookings. Please choose an approved real Host.")
    if host_country != "IN":
        raise HTTPException(403, "Only India-based real Hosts are available for booking.")
    if not bool(h.get("online")):
        raise HTTPException(409, "Host is currently offline. Please choose an online Host or try again later.")
    if host_country != user_country:
        message = f"⚠️ <b>यह Host book नहीं किया जा सकता</b>\n\nयह Host <b>{h.get('country_name', host_country)}</b> से है। अभी केवल <b>India-based Hosts</b> की booking उपलब्ध है।\n\nकृपया India Host चुनें।"
        notify_user(data.user_id, message)
        raise HTTPException(403, "You cannot book a Host from another country")
    if is_demo and bool(u.get("demo_used", False)):
        raise HTTPException(409,"Demo already used. Demo offer is available only once per Telegram User ID.")
    if int(u.get("tokens",0)) < plan_tokens: raise HTTPException(400,"Insufficient tokens")
    # Reserve money immediately; refund on reject/cancel.
    reserved=col("users").update_one({"user_id":uid(data.user_id),"tokens":{"$gte":plan_tokens}}, {"$inc":{"tokens":-plan_tokens}})
    if reserved.modified_count != 1:
        raise HTTPException(400,"Insufficient tokens")
    # Demo is permanently consumed at the first successful demo booking.
    # The Telegram user_id is the permanent identity used for this check.
    if is_demo:
        marked=col("users").update_one(
            {"user_id":uid(data.user_id),"demo_used":{"$ne":True}},
            {"$set":{"demo_used":True,"demo_used_at":now_ts()}}
        )
        if marked.modified_count != 1:
            col("users").update_one({"user_id":uid(data.user_id)},{"$inc":{"tokens":plan_tokens}})
            raise HTTPException(409,"Demo already used. Demo offer is available only once per Telegram User ID.")
    busy=overlap(data.host_id,start,end)
    bid=str(uuid.uuid4())
    doc={"booking_id":bid,"user_id":uid(data.user_id),"host_id":uid(data.host_id),"minutes":plan_minutes,"tokens":plan_tokens,"is_demo":is_demo,"requested_start":start,"scheduled_start":None,"scheduled_end":None,"status":"pending","busy_at_request":busy,"created_at":now_ts(),"updated_at":now_ts(),"call_started_at":None,"call_ended_at":None,"user_joined_at":None,"host_joined_at":None,"earnings_credited":False}
    col("bookings").insert_one(doc)
    if is_demo:
        col("users").update_one({"user_id":uid(data.user_id)},{"$set":{"demo_booking_id":bid,"updated_at":now_ts()}})
    notify_user(data.user_id, f"✅ <b>Booking request submitted</b>\n\nHost: {h.get('name','Host')}\nDuration: {plan['minutes']} min\nCharge: {plan['tokens']} Coins\n\nHost confirmation ka wait karein. Agar Host busy hai to aapko update milega.")
    notify_user(data.host_id, f"📅 <b>New Slot Booking Request</b>\n\nUser: <code>{data.user_id}</code>\nDuration: {plan['minutes']} min\nRequested: {iso(start)}\n\nVynora Live में जाकर Accept या Reject करें.")
    notify_request(f"📅 <b>NEW BOOKING REQUEST</b>\n\nUser: <code>{data.user_id}</code>\nHost: {h.get('name','Host')}\nDuration: {plan['minutes']} min\nCoins: {plan['tokens']}\nRequested: {iso(start)}\nBusy at request: {'YES' if busy else 'NO'}", [[{"text":"✅ Accept","callback_data":f"accept_booking:{bid}"},{"text":"❌ Reject","callback_data":f"reject_booking:{bid}"}]])
    notify_full(f"📅 NEW BOOKING\nBooking: <code>{bid}</code>\nUser: {data.user_id}\nHost: {data.host_id}\nDuration: {plan['minutes']} min\nCoins: {plan['tokens']}\nRequested: {iso(start)}\nBusy: {busy}")
    return {"status":"success","booking_id":bid,"busy":busy,"message":"Booking submitted"}

@app.post("/api/booking/action")
def booking_action(data: BookingAction):
    if not is_admin(data.host_id) and data.action in ("approve","reject"):
        h=host_doc(data.host_id)
        if not h or h.get("status")!="approved": raise HTTPException(403,"Host access required")
    b=col("bookings").find_one({"booking_id":data.booking_id})
    if not b: raise HTTPException(404,"Booking not found")
    action=data.action
    if action=="reject":
        if b["status"] not in ("pending",): raise HTTPException(400,"Booking cannot be rejected now")
        col("bookings").update_one({"booking_id":b["booking_id"]},{"$set":{"status":"rejected","updated_at":now_ts()}})
        col("users").update_one({"user_id":b["user_id"]},{"$inc":{"tokens":b["tokens"]}})
        notify_user(b["user_id"],f"❌ Booking rejected. {b['tokens']} Coins refunded.")
        return {"status":"success"}
    if action=="accept":
        if b["status"]!="pending": raise HTTPException(400,"Booking already processed")
        col("bookings").update_one({"booking_id":b["booking_id"]},{"$set":{"status":"accepted","updated_at":now_ts()}})
        notify_user(b["user_id"],"✅ <b>Host accepted your booking!</b>\n\nAb Host call ka time schedule karega. Schedule hote hi aapko exact time aur JOIN CALL option milega.")
        notify_user(b["host_id"],"✅ Booking accepted. Ab aap call ka time 2/5/10 minutes later ya exact time par schedule kar sakte hain.")
        return {"status":"success","next":"schedule","booking_id":b["booking_id"]}
    raise HTTPException(400,"Unknown action")

@app.post("/api/booking/schedule")
def schedule(data: ScheduleModel):
    b=col("bookings").find_one({"booking_id":data.booking_id,"host_id":uid(data.host_id)})
    if not b or b.get("status") not in ("accepted","scheduled"): raise HTTPException(404,"Booking not available for scheduling")
    if b.get("call_started_at"): raise HTTPException(400,"Call has already started")
    start=int(data.scheduled_start); end=start+int(b["minutes"])*60
    if start < now_ts(): raise HTTPException(400,"Schedule must be in the future")
    if overlap(data.host_id,start,end):
        # Allow the current booking itself but not another booking.
        other=col("bookings").find_one({"booking_id":{"$ne":data.booking_id},"host_id":uid(data.host_id),"status":{"$in":["accepted","scheduled","calling"]},"scheduled_start":{"$lt":end},"scheduled_end":{"$gt":start}})
        if other: raise HTTPException(409,"Host is busy at that time")
    col("bookings").update_one({"booking_id":data.booking_id},{"$set":{"scheduled_start":start,"scheduled_end":end,"status":"scheduled","updated_at":now_ts()}})
    host_name = host_doc(b['host_id']).get('name','Host') if host_doc(b['host_id']) else 'Host'
    notify_user(b["user_id"],f"📞 <b>Call Scheduled</b>\n\nHost: {host_name}\nTime: {iso(start)}\nDuration: {b['minutes']} min\n\nScheduled time par JOIN CALL button milega. Host aapko call karega.")
    notify_user(b["host_id"],f"📞 <b>Call Scheduled</b>\n\nUser: <code>{b['user_id']}</code>\nTime: {iso(start)}\nDuration: {b['minutes']} min\n\nScheduled time par Vynora Live खोलकर JOIN CALL करें.")
    notify_full(f"🕐 BOOKING SCHEDULED\nBooking: <code>{b['booking_id']}</code>\nHost: {b['host_id']}\nUser: {b['user_id']}\nStart: {iso(start)}\nDuration: {b['minutes']} min")
    return {"status":"success","scheduled_start":start,"scheduled_end":end}

@app.post("/api/call/join")
def call_join(data: CallJoinModel):
    require_user(data.user_id)
    b=col("bookings").find_one({"booking_id":data.booking_id})
    if not b: raise HTTPException(404,"Booking not found")
    if uid(data.user_id) not in (b["user_id"],b["host_id"]): raise HTTPException(403,"Not a participant")
    if b.get("status") not in ("scheduled","calling"): raise HTTPException(400,"Call is not scheduled")
    if b.get("scheduled_start") and now_ts() < int(b["scheduled_start"]): raise HTTPException(400,"Call is not ready yet")
    return {"status":"authorized","booking":{k:b.get(k) for k in ["booking_id","minutes","tokens","status","call_started_at","scheduled_end","user_joined_at","host_joined_at"]}}


@app.post("/api/call/connect")
def call_connect(data: CallJoinModel):
    require_user(data.user_id)
    b=col("bookings").find_one({"booking_id":data.booking_id})
    if not b: raise HTTPException(404,"Booking not found")
    if uid(data.user_id) not in (b["user_id"],b["host_id"]): raise HTTPException(403,"Not a participant")
    if b.get("status") not in ("scheduled","calling"): raise HTTPException(400,"Call is not scheduled")
    if b.get("scheduled_start") and now_ts() < int(b["scheduled_start"]): raise HTTPException(400,"Call is not ready yet")
    field="user_joined_at" if uid(data.user_id)==b["user_id"] else "host_joined_at"
    col("bookings").update_one({"booking_id":data.booking_id},{"$set":{field:now_ts(),"updated_at":now_ts()}})
    fresh=col("bookings").find_one({"booking_id":data.booking_id})
    if fresh.get("user_joined_at") and fresh.get("host_joined_at") and not fresh.get("call_started_at"):
        start=now_ts(); end=start+int(fresh["minutes"])*60
        changed=col("bookings").update_one({"booking_id":data.booking_id,"call_started_at":None},{"$set":{"status":"calling","call_started_at":start,"scheduled_end":end,"updated_at":start}})
        if changed.modified_count:
            notify_full(f"📹 <b>CALL STARTED</b>\nBooking: <code>{data.booking_id}</code>\nUser: {b['user_id']}\nHost: {b['host_id']}\nDuration: {b['minutes']} min\nConnected: {iso(start)}")
        fresh=col("bookings").find_one({"booking_id":data.booking_id})
    return {"status":"connected","booking":{k:fresh.get(k) for k in ["booking_id","minutes","tokens","status","call_started_at","scheduled_end","user_joined_at","host_joined_at"]}}


@app.get("/api/call/state/{booking_id}")
def call_state(booking_id:str, user_id:int):
    require_user(user_id)
    b=col("bookings").find_one({"booking_id":booking_id})
    if not b or uid(user_id) not in (b["user_id"],b["host_id"]): raise HTTPException(403,"Not allowed")
    return {"booking":{k:b.get(k) for k in ["booking_id","minutes","tokens","status","call_started_at","scheduled_end","user_joined_at","host_joined_at","actual_seconds","charged_tokens","refunded_tokens"]}}

@app.post("/api/call/end")
def call_end(data: CallEndModel):
    require_user(data.user_id)
    b=col("bookings").find_one({"booking_id":data.booking_id})
    if not b: raise HTTPException(404,"Booking not found")
    if uid(data.user_id) not in (b["user_id"],b["host_id"]): raise HTTPException(403,"Not a participant")
    if b.get("status") in ("completed","rejected","cancelled"): return {"status":"success"}
    if not b.get("call_started_at"):
        # No connected call: refund the full reserved amount exactly once.
        closed=col("bookings").find_one_and_update(
            {"booking_id":data.booking_id,"earnings_credited":{"$ne":True}},
            {"$set":{"status":"completed","call_ended_at":now_ts(),"actual_seconds":0,"charged_tokens":0,"refunded_tokens":b.get("tokens",0),"host_earned":0,"platform_earned":0,"earnings_credited":True,"completion_reason":"cancelled_before_connect"}},
            return_document=ReturnDocument.AFTER
        )
        if closed:
            col("users").update_one({"user_id":b["user_id"]},{"$inc":{"tokens":int(b.get("tokens",0))}})
            notify_full(f"📊 CALL CANCELLED BEFORE CONNECT\nBooking: {b['booking_id']}\nUser: {b['user_id']}\nHost: {b['host_id']}\nRefunded: {b['tokens']} Coins")
        return {"status":"success","actual_seconds":0,"charged_tokens":0,"refunded_tokens":b.get("tokens",0) if closed else 0}
    fresh=finalize_call(data.booking_id, now_ts(), "manual_end")
    return {"status":"success","actual_seconds":fresh.get("actual_seconds",0) if fresh else 0,"charged_tokens":fresh.get("charged_tokens",0) if fresh else 0,"refunded_tokens":fresh.get("refunded_tokens",0) if fresh else 0,"host_earned":fresh.get("host_earned",0) if fresh else 0}

@app.get("/api/call/pending/{user_id}")
def pending_call(user_id:int):
    require_user(user_id)
    now=now_ts()
    b=col("bookings").find_one({
        "$or":[{"user_id":uid(user_id)},{"host_id":uid(user_id)}],
        "status":"scheduled",
        "scheduled_start":{"$lte":now},
        "call_started_at":None
    }, sort=[("scheduled_start",1)])
    if not b:
        return {"active":False}
    b=clean_json(b)
    return {"active":True,"booking":b}


@app.post("/api/call/remind/{booking_id}")
def call_remind(booking_id:str, user_id:int):
    require_user(user_id)
    b=col("bookings").find_one({"booking_id":booking_id})
    if not b or uid(user_id) not in (b["user_id"],b["host_id"]): raise HTTPException(403,"Not allowed")
    if b.get("status") not in ("scheduled","calling"): raise HTTPException(400,"Call is not active")
    notify_user(b["user_id"],"📹 आपका Host अभी available है। Vynora Live खोलकर video call join करें.", [[{"text":"📹 JOIN VIDEO CALL","web_app":{"url":os.getenv("WEB_APP_URL","https://vynora-live-new.onrender.com")}}]])
    return {"status":"success"}


@app.get("/api/agora-token")
def agora_token(channelName: str, uid: int, role: str="publisher"):
    verified=_verified_web_user.get()
    if verified is None or int(uid) != verified:
        raise HTTPException(403,"Telegram identity mismatch")
    if channelName.startswith("booking_"):
        bid=channelName[len("booking_"):]
        b=col("bookings").find_one({"booking_id":bid})
        if not b or verified not in (b.get("user_id"),b.get("host_id")):
            raise HTTPException(403,"Not a booking participant")
    elif channelName.startswith("admin_call_"):
        cid=channelName[len("admin_call_"):]
        d=col("direct_calls").find_one({"call_id":cid})
        if not d or verified not in (d.get("admin_id"),d.get("target_id")):
            raise HTTPException(403,"Not a direct-call participant")
    else:
        raise HTTPException(400,"Invalid call channel")
    if not AGORA_APP_ID or not AGORA_APP_CERTIFICATE or not RtcTokenBuilder:
        raise HTTPException(503,"Agora is not configured")
    expiry=now_ts()+86400
    token=RtcTokenBuilder.buildTokenWithUid(AGORA_APP_ID,AGORA_APP_CERTIFICATE,channelName,int(uid),Role_Publisher,expiry)
    return {"appId":AGORA_APP_ID,"channelName":channelName,"uid":int(uid),"token":token,"expiresAt":expiry}

@app.get("/api/upi-qr")
def upi_qr(amount: float = Query(..., gt=0)):
    if qrcode is None:
        raise HTTPException(503, "QR service unavailable")
    uri = "upi://pay?pa=" + urllib.parse.quote(UPI_ID, safe="@") + "&pn=" + urllib.parse.quote(UPI_NAME) + f"&am={float(amount):.2f}&cu=INR"
    img = qrcode.make(uri)
    import io
    out = io.BytesIO(); img.save(out, format="PNG")
    return Response(content=out.getvalue(), media_type="image/png", headers={"Cache-Control":"no-store"})

@app.post("/api/recharge")
def recharge(data: RechargeModel):
    require_user(data.user_id)
    plan=next((p for p in RECHARGE_PLANS if p["rupees"]==data.rupees and p["tokens"]==data.tokens),None)
    if not plan: raise HTTPException(400,"Invalid recharge plan")
    rid=str(uuid.uuid4())
    col("recharges").insert_one({"recharge_id":rid,"user_id":uid(data.user_id),"rupees":data.rupees,"tokens":data.tokens,"utr":data.utr,"screenshot_url":data.screenshot_url,"status":"pending","created_at":now_ts()})
    text=f"💳 <b>RECHARGE REQUEST</b>\nID: <code>{rid}</code>\nUser: <code>{data.user_id}</code>\n₹{data.rupees} → {data.tokens} Coins\nUTR: {data.utr or '-'}"
    notify_request(text,[[{"text":"✅ Approve","callback_data":f"approve_recharge:{rid}"},{"text":"❌ Reject","callback_data":f"reject_recharge:{rid}"}]])
    notify_full(text)
    return {"status":"success","recharge_id":rid}

@app.post("/api/recharge/submit")
async def recharge_submit(
    user_id: int = Form(...),
    rupees: int = Form(...),
    tokens: int = Form(...),
    utr: str = Form(...),
    screenshot: UploadFile = File(...),
):
    require_user(user_id)
    plan = next((p for p in RECHARGE_PLANS if p["rupees"] == rupees and p["tokens"] == tokens), None)
    if not plan:
        raise HTTPException(400, "Invalid recharge plan")
    utr = (utr or "").strip()[:100]
    if not utr:
        raise HTTPException(400, "UTR / Transaction ID is required")
    if not screenshot or not (screenshot.content_type or "").startswith("image/"):
        raise HTTPException(400, "Payment screenshot image is required")
    photo_bytes = await screenshot.read()
    if not photo_bytes:
        raise HTTPException(400, "Payment screenshot is empty")
    if len(photo_bytes) > 1200000:
        raise HTTPException(400, "Payment screenshot is too large. Please use an image below 1.2 MB.")

    rid = "RCH-" + uuid.uuid4().hex[:10].upper()
    mime = screenshot.content_type or "image/jpeg"
    data_uri = f"data:{mime};base64," + base64.b64encode(photo_bytes).decode("ascii")
    col("recharges").insert_one({
        "recharge_id": rid, "user_id": uid(user_id), "rupees": rupees, "tokens": tokens,
        "utr": utr, "screenshot_data": data_uri,
        "screenshot_filename": screenshot.filename or "payment.jpg",
        "screenshot_content_type": mime, "status": "pending", "created_at": now_ts()
    })
    text = (f"💳 <b>RECHARGE REQUEST</b>\nID: <code>{rid}</code>\n"
            f"User: <code>{uid(user_id)}</code>\n₹{rupees} → {tokens} Coins\n"
            f"UTR: <code>{esc_html(utr)}</code>")
    buttons = [[
        {"text":"✅ Approve", "callback_data":f"approve_recharge:{rid}"},
        {"text":"❌ Reject", "callback_data":f"reject_recharge:{rid}"}
    ]]
    # Group 1 gets the actual payment screenshot with the approve/reject buttons on the same message.
    sent_photo = tg_send_photo(GROUP_1_ID, photo_bytes, screenshot.filename or "payment.jpg", text, buttons)
    if sent_photo and sent_photo.get("message_id"):
        col("recharges").update_one({"recharge_id": rid}, {"$set": {"telegram_screenshot_message_id": sent_photo["message_id"]}})
    else:
        # Keep an action-only fallback if Telegram could not attach the buttons to the photo.
        fallback = tg_send(GROUP_1_ID, f"🧾 <b>Recharge Action</b>\nID: <code>{rid}</code>\n₹{rupees} → {tokens} Coins", buttons) or {}
        if fallback.get("ok") and fallback.get("result", {}).get("message_id"):
            col("recharges").update_one({"recharge_id": rid}, {"$set": {"telegram_action_message_id": fallback["result"]["message_id"]}})
    notify_full(f"RECHARGE REQUEST\nUser: {uid(user_id)}\n₹{rupees} → {tokens} Coins\nUTR: {utr}\nID: {rid}")
    return {"status":"success", "recharge_id":rid}

@app.get("/api/recharge/screenshot/{recharge_id}")
def get_recharge_screenshot(recharge_id: str):
    r = col("recharges").find_one({"recharge_id": recharge_id})
    if not r or not r.get("screenshot_data"):
        raise HTTPException(404, "Screenshot not found")
    raw = r["screenshot_data"]
    try:
        header, encoded = raw.split(",", 1)
        mime = header[5:].split(";", 1)[0] if header.startswith("data:") else "image/jpeg"
        return Response(base64.b64decode(encoded), media_type=mime)
    except Exception:
        raise HTTPException(500, "Invalid screenshot")

@app.post("/api/admin/token")
def admin_token(data: TokenModel):
    if not is_admin(data.user_id): raise HTTPException(403,"Admin only")
    return {"status":"use telegram command"}

@app.post("/api/admin/block")
def admin_block(data: BlockModel):
    if not is_admin(data.target_id): pass
    # target_id is intentionally the target; admin authentication is headerless for API, so UI should not expose this.
    raise HTTPException(400,"Use Telegram admin command for block/unblock")

@app.post("/api/admin/direct-call")
def direct_call(data: DirectCallModel):
    if not is_admin(data.admin_id):
        raise HTTPException(403,"Super Admin only")
    target = require_user(data.target_id)
    if target.get("blocked"):
        raise HTTPException(403, "Target user is blocked")
    cid = str(uuid.uuid4())
    channel = f"admin_call_{cid}"
    d = {
        "call_id":cid,
        "type":"admin_direct",
        "admin_id":uid(data.admin_id),
        "target_id":uid(data.target_id),
        "channel":channel,
        "status":"ringing",
        "created_at":now_ts()
    }
    col("direct_calls").insert_one(d)
    notify_user(
        data.target_id,
        "👑 <b>VYNORA SUPER ADMIN CALL</b>\n\n"
        "Super Admin wants to speak with you immediately.\n"
        "This is a free Admin call — no coins and no time limit.",
        [[{"text":"📞 JOIN ADMIN CALL","web_app":{"url":os.getenv("WEB_APP_URL","https://vynora-live-new.onrender.com")}}]]
    )
    notify_full(f"👑 ADMIN DIRECT CALL\nAdmin: {data.admin_id}\nTarget: {data.target_id}\nCall: <code>{cid}</code>\nFree: YES")
    return {"status":"success","call_id":cid,"channel":channel,"target":target.get("name","User")}

@app.post("/api/direct-call/join/{call_id}")
def join_direct_call(call_id:str, user_id:int):
    require_user(user_id)
    d=col("direct_calls").find_one({"call_id":call_id})
    if not d or uid(user_id) not in (d.get("admin_id"),d.get("target_id")): raise HTTPException(403,"Not allowed")
    col("direct_calls").update_one({"call_id":call_id},{"$set":{"status":"connected","joined_at":now_ts()}})
    return {"status":"success","call":clean_json(d)}


@app.get("/api/direct-call/pending/{target_id}")
def pending_direct_call(target_id:int):
    require_user(target_id)
    d=col("direct_calls").find_one({"target_id":uid(target_id),"status":{"$in":["ringing","connected"]}},sort=[("created_at",-1)])
    if not d: return {"active":False}
    d["_id"]=str(d["_id"])
    return {"active":True,"call":clean_json(d)}

@app.post("/api/direct-call/end/{call_id}")
def end_direct_call(call_id:str, user_id:int):
    require_user(user_id)
    d=col("direct_calls").find_one({"call_id":call_id})
    if not d: raise HTTPException(404,"Direct call not found")
    if uid(user_id) not in (d.get("admin_id"), d.get("target_id")):
        raise HTTPException(403,"Not allowed")
    col("direct_calls").update_one({"call_id":call_id},{"$set":{"status":"ended","ended_at":now_ts()}})
    notify_full(f"📞 ADMIN DIRECT CALL ENDED\nCall: <code>{call_id}</code>\nAdmin: {d.get('admin_id')}\nTarget: {d.get('target_id')}")
    return {"status":"success"}

@app.get("/api/admin/direct-calls/{admin_id}")
def direct_calls(admin_id:int):
    if not is_admin(admin_id): raise HTTPException(403,"Admin only")
    return clean_json(list(col("direct_calls").find({"admin_id":uid(admin_id)}).sort("created_at",-1).limit(20)))

@app.get("/api/admin/overview/{admin_id}")
def admin_overview(admin_id:int):
    if not is_admin(admin_id): raise HTTPException(403,"Admin only")
    t=now_ts(); day=t-86400
    return {
        "users": col("users").count_documents({}),
        "new_users_today": col("users").count_documents({"created_at":{"$gte":day}}),
        "hosts": col("hosts").count_documents({"status":"approved","demo":{"$ne":True}}),
        "online_hosts": col("hosts").count_documents({"status":"approved","demo":{"$ne":True},"online":True}),
        "pending_hosts": col("hosts").count_documents({"status":"pending"}),
        "recharge_requests": col("recharges").count_documents({"status":"pending"}),
        "pending_withdrawals": col("withdrawals").count_documents({"status":"pending"}),
        "total_calls": col("bookings").count_documents({"status":"completed"}),
        "today_calls": col("bookings").count_documents({"status":"completed","call_ended_at":{"$gte":day}}),
        "total_minutes": round(sum(float(x.get("actual_seconds",0))/60 for x in col("bookings").find({"status":"completed"},{"actual_seconds":1})),2),
        "total_recharge": float(sum(float(x.get("rupees",0)) for x in col("recharges").find({"status":"approved"},{"rupees":1}))),
        "platform_earnings": float(sum(float(x.get("platform_earned",0)) for x in col("bookings").find({"status":"completed"},{"platform_earned":1}))),
        "host_earnings": float(sum(float(x.get("host_earned",0)) for x in col("bookings").find({"status":"completed"},{"host_earned":1}))),
    }


@app.post("/api/announcement")
def announcement(data: AnnouncementModel):
    if not is_admin(data.user_id): raise HTTPException(403,"Admin only")
    col("settings").update_one({"key":"announcement"},{"$set":{"key":"announcement","message":data.message,"updated_at":now_ts()}},upsert=True)
    notify_group(GROUP_1_ID,f"📢 <b>ANNOUNCEMENT</b>\n{data.message}")
    notify_group(GROUP_2_ID,f"📢 <b>ANNOUNCEMENT</b>\n{data.message}")
    notify_group(GROUP_3_ID,f"📢 <b>ANNOUNCEMENT</b>\n{data.message}")
    return {"status":"success"}

@app.get("/api/announcement")
def get_announcement():
    return col("settings").find_one({"key":"announcement"},{"_id":0}) or {"message":""}

@app.post("/api/host/online")
def host_online(user_id:int):
    require_user(user_id)
    h = host_or_404(user_id)
    col("hosts").update_one({"user_id":uid(user_id)},{"$set":{"online":True,"updated_at":now_ts()}})
    # Tell users who were waiting because this host was busy.
    pending = col("bookings").find({"host_id":uid(user_id),"status":"pending","busy_at_request":True})
    for b in pending:
        notify_user(
            b["user_id"],
            f"🟢 <b>{h.get('name','Host')} is available again</b>\n\n"
            "Your booking request is still waiting for host confirmation."
        )
    notify_full(f"🟢 HOST AVAILABLE\nHost: {user_id}\nName: {h.get('name','Host')}\nStatus: Online")
    return {"status":"success","online":True}

@app.post("/api/host/offline")
def host_offline(user_id:int):
    require_user(user_id)
    host_or_404(user_id); col("hosts").update_one({"user_id":uid(user_id)},{"$set":{"online":False,"updated_at":now_ts()}}); return {"status":"success","online":False}

@app.get("/api/host/{host_id}/stats")
def host_stats(host_id:int):
    require_user(host_id)
    h=host_or_404(host_id)
    return {"total_tokens":h.get("total_tokens",0),"gift_tokens":h.get("gift_tokens",0),"available_earnings":h.get("available_earnings",0),"total_calls":col("bookings").count_documents({"host_id":uid(host_id),"status":"completed"}),"online":bool(h.get("online")),"filter_name":h.get("filter_name","natural")}

@app.post("/api/withdraw")
def withdraw(user_id:int, amount:float):
    h=host_or_404(user_id)
    if amount < 700: raise HTTPException(400,"Minimum withdrawal is ₹700")
    if amount > float(h.get("available_earnings",0)): raise HTTPException(400,"Insufficient balance")
    wid=str(uuid.uuid4()); col("withdrawals").insert_one({"withdrawal_id":wid,"user_id":uid(user_id),"amount":amount,"status":"pending","created_at":now_ts()})
    col("hosts").update_one({"user_id":uid(user_id)},{"$inc":{"available_earnings":-amount}})
    notify_request(f"💸 <b>WITHDRAWAL REQUEST</b>\nHost: {user_id}\nAmount: ₹{amount}\nID: <code>{wid}</code>", [[{"text":"✅ Paid","callback_data":f"approve_withdraw:{wid}"},{"text":"❌ Reject","callback_data":f"reject_withdraw:{wid}"}]])
    notify_full(f"💸 WITHDRAWAL REQUEST\nHost: {user_id}\nAmount: ₹{amount}\nID: <code>{wid}</code>")
    return {"status":"success","withdrawal_id":wid}

# ---------------- Telegram webhook / admin commands ----------------

def tg(method, payload):
    if not BOT_TOKEN: return None
    try:
        r=requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",json=payload,timeout=25)
        return r.json()
    except Exception as e:
        log.warning("telegram %s failed: %s",method,e); return None


def tg_send(chat_id,text,buttons=None):
    p={"chat_id":chat_id,"text":text,"parse_mode":"HTML"}
    if buttons: p["reply_markup"]={"inline_keyboard":buttons}
    return tg("sendMessage",p)


def admin_command(chat_id, from_id, text):
    if from_id not in ADMIN_IDS: return False
    parts=text.strip().split(maxsplit=2); cmd=parts[0].lower(); args=parts[1:]
    try:
        if cmd in ("/helpadmin","/adminhelp"):
            tg_send(chat_id,"👑 <b>VYNORA ADMIN COMMANDS</b>\n\n/addtoken ID AMOUNT\n/removetoken ID AMOUNT\n/settoken ID AMOUNT\n/approvehost ID\n/rejecthost ID\n/removehost ID\n/addhost ID\n/approveuser ID\n/user ID\n/ban ID [reason]\n/unban ID\n/block ID [reason]\n/unblock ID\n/banhost ID\n/unbanhost ID\n/approverecharge RECHARGE_ID\n/rejectrecharge RECHARGE_ID\n/approvebooking BOOKING_ID\n/rejectbooking BOOKING_ID\n/announce MESSAGE\n/offer MESSAGE\n/clearannouncement\n/callhost HOST_ID\n/setcountry USER_ID IN\n/stats")
        elif cmd in ("/addtoken","/removetoken","/settoken") and len(args)>=2:
            target=int(args[0]); amount=int(args[1]); u=ensure_user(target)[0]
            if cmd=="/addtoken": col("users").update_one({"user_id":target},{"$inc":{"tokens":amount}})
            elif cmd=="/removetoken": col("users").update_one({"user_id":target},{"$inc":{"tokens":-amount}})
            else: col("users").update_one({"user_id":target},{"$set":{"tokens":max(0,amount)}})
            tg_send(chat_id,f"✅ Token updated for <code>{target}</code>")
            notify_full(f"🪙 ADMIN TOKEN ACTION\nAdmin: {from_id}\nUser: {target}\nCommand: {cmd}\nAmount: {amount}")
        elif cmd in ("/approvehost","/addhost") and args:
            target=int(args[0]); ensure_user(target); col("hosts").update_one({"user_id":target},{"$set":{"user_id":target,"telegram_id":target,"status":"approved","verified":True,"verification_label":"Vynora Verified Host","online":False,"updated_at":now_ts()},"$setOnInsert":{"total_tokens":0,"available_earnings":0,"gift_tokens":0,"filter_name":"natural"}},upsert=True); tg_send(chat_id,f"✅ Host approved: <code>{target}</code>"); notify_user(target,"🎙️ आपका Host account approve हो गया है। अब आप bookings receive कर सकते हैं।"); notify_full(f"🎙️ HOST APPROVED\nAdmin: {from_id}\nHost: {target}\nTeam Group: Host approved and ready for monitoring")
        elif cmd in ("/rejecthost","/removehost") and args:
            target=int(args[0]); col("hosts").update_one({"user_id":target},{"$set":{"status":"rejected","online":False}}); tg_send(chat_id,f"❌ Host rejected/removed: <code>{target}</code>"); notify_full(f"❌ HOST REJECTED\nAdmin: {from_id}\nHost: {target}")
        elif cmd in ("/ban","/block","/banhost") and args:
            target=int(args[0]); reason=args[1] if len(args)>1 else "Admin action"; col("users").update_one({"user_id":target},{"$set":{"blocked":True,"block_reason":reason,"blocked_at":now_ts()}},upsert=True); col("hosts").update_one({"user_id":target},{"$set":{"status":"blocked","online":False}}); tg_send(chat_id,f"🚫 Blocked <code>{target}</code>"); notify_user(target,"🚫 आपका account block कर दिया गया है। Support से संपर्क करें।"); notify_full(f"🚫 BLOCKED\nAdmin: {from_id}\nUser: {target}\nReason: {reason}")
        elif cmd in ("/unban","/unblock","/unbanhost") and args:
            target=int(args[0]); col("users").update_one({"user_id":target},{"$set":{"blocked":False,"updated_at":now_ts()}}); h=host_doc(target); 
            if h and h.get("status")=="blocked": col("hosts").update_one({"user_id":target},{"$set":{"status":"approved"}})
            tg_send(chat_id,f"✅ Unblocked <code>{target}</code>"); notify_user(target,"✅ आपका account unblock कर दिया गया है।"); notify_full(f"✅ UNBLOCKED\nAdmin: {from_id}\nUser: {target}")
        elif cmd in ("/approverecharge","/rejectrecharge") and args:
            rid=args[0]; r=col("recharges").find_one({"recharge_id":rid,"status":"pending"})
            if not r: raise ValueError("Recharge request not found or already processed")
            ok=cmd=="/approverecharge"; status="approved" if ok else "rejected"
            col("recharges").update_one({"recharge_id":rid,"status":"pending"},{"$set":{"status":status,"approved_by":from_id,"approved_at":now_ts()}})
            if ok:
                col("users").update_one({"user_id":r["user_id"]},{"$inc":{"tokens":r["tokens"]}}); notify_user(r["user_id"],f"✅ Recharge approved. {r['tokens']} Coins added.")
            else: notify_user(r["user_id"],"❌ Recharge rejected. Contact support.")
            remove_recharge_screenshot(rid)
            tg_send(chat_id,f"Recharge {status}: <code>{rid}</code>\n🗑️ Payment screenshot deleted"); notify_full(f"RECHARGE {status.upper()}\nUser: {r['user_id']}\n₹{r['rupees']} → {r['tokens']} Coins\nAdmin: {from_id}\nScreenshot: DELETED")
        elif cmd in ("/approvebooking","/rejectbooking") and args:
            bid=args[0]; b=col("bookings").find_one({"booking_id":bid})
            if not b: raise ValueError("Booking not found")
            ok=cmd=="/approvebooking"; status="accepted" if ok else "rejected"
            col("bookings").update_one({"booking_id":bid},{"$set":{"status":status,"updated_at":now_ts()}})
            if ok: notify_user(b["user_id"],"✅ Booking accepted. Host/admin will schedule the call.")
            else: col("users").update_one({"user_id":b["user_id"]},{"$inc":{"tokens":b["tokens"]}}); notify_user(b["user_id"],f"❌ Booking rejected. {b['tokens']} Coins refunded.")
            tg_send(chat_id,f"Booking {status}: <code>{bid}</code>"); notify_full(f"BOOKING {status.upper()}\nBooking: {bid}\nUser: {b['user_id']}\nHost: {b['host_id']}\nAdmin: {from_id}")
        elif cmd in ("/approvewithdrawal","/rejectwithdrawal") and args:
            wid=args[0]; w=col("withdrawals").find_one({"withdrawal_id":wid,"status":"pending"})
            if not w: raise ValueError("Withdrawal not found or already processed")
            ok=cmd=="/approvewithdrawal"
            col("withdrawals").update_one({"withdrawal_id":wid,"status":"pending"},{"$set":{"status":"paid" if ok else "rejected","processed_by":from_id,"processed_at":now_ts()}})
            if not ok: col("hosts").update_one({"user_id":w["user_id"]},{"$inc":{"available_earnings":float(w["amount"])}})
            notify_user(w["user_id"], '✅ Withdrawal paid.' if ok else '❌ Withdrawal rejected. Balance returned.')
            notify_full(f"WITHDRAWAL {'PAID' if ok else 'REJECTED'}\nHost: {w['user_id']}\nAmount: ₹{w['amount']}\nAdmin: {from_id}")
            tg_send(chat_id,f"✅ Withdrawal {'paid' if ok else 'rejected'}: <code>{wid}</code>")
        elif cmd=="/setplan" and len(args)>=2:
            minutes=int(args[0]); tokens=int(args[1])
            if minutes not in (1,5,10,15,20,25,30) or tokens<1: raise ValueError("Use: /setplan MINUTES TOKENS")
            plans=get_booking_plans(); found=False
            for x in plans:
                if int(x["minutes"])==minutes: x["tokens"]=tokens; found=True
            if not found: plans.append({"minutes":minutes,"tokens":tokens})
            plans=sorted(plans,key=lambda x:int(x["minutes"]))
            col("settings").update_one({"key":"booking_plans"},{"$set":{"key":"booking_plans","value":plans,"updated_at":now_ts()}},upsert=True)
            tg_send(chat_id,f"✅ Slot plan updated: {minutes} min = {tokens} Coins")
        elif cmd=="/approveuser" and args:
            target=int(args[0]); ensure_user(target); col("users").update_one({"user_id":target},{"$set":{"blocked":False,"updated_at":now_ts()}}); tg_send(chat_id,f"✅ User approved/unblocked: <code>{target}</code>"); notify_user(target,"✅ आपका account active है।")
        elif cmd=="/userhistory" and args:
            target=int(args[0])
            u=user_doc(target) or {}
            bookings=list(col("bookings").find({"user_id":target}).sort("created_at",-1).limit(10))
            recharges=list(col("recharges").find({"user_id":target}).sort("created_at",-1).limit(10))
            gifts=list(col("gifts").find({"user_id":target}).sort("created_at",-1).limit(10))
            calls=list(col("bookings").find({"user_id":target,"call_started_at":{"$ne":None}}).sort("created_at",-1).limit(10))
            btxt="\\n".join(f"• {x.get('booking_id','-')} | {x.get('status','-')} | {x.get('minutes',0)}m | {x.get('tokens',0)} tokens" for x in bookings) or "No bookings"
            rtxt="\\n".join(f"• ₹{x.get('rupees',0)} | {x.get('status','-')} | {x.get('recharge_id','-')}" for x in recharges) or "No recharges"
            gtxt="\\n".join(f"• {x.get('gift_name',x.get('name','Gift'))} | {x.get('tokens',0)} tokens | Host {x.get('host_id','-')}" for x in gifts) or "No gifts"
            ctxt=f"{len(calls)} call records"
            tg_send(chat_id,f"👤 <b>USER HISTORY</b>\\nID: <code>{target}</code>\\nName: {u.get('name','-')}\\nUsername: @{u.get('username','').lstrip('@') or '-'}\\nTokens: {u.get('tokens',0)}\\nBlocked: {u.get('blocked',False)}\\n\\n📅 <b>Bookings</b>\\n{btxt}\\n\\n💰 <b>Recharges</b>\\n{rtxt}\\n\\n🎁 <b>Gifts</b>\\n{gtxt}\\n\\n📹 <b>Calls</b>: {ctxt}")
        elif cmd=="/hosthistory" and args:
            target=int(args[0])
            h=host_doc(target) or {}
            bookings=list(col("bookings").find({"host_id":target}).sort("created_at",-1).limit(10))
            withdrawals=list(col("withdrawals").find({"user_id":target}).sort("created_at",-1).limit(10))
            gifts=list(col("gifts").find({"host_id":target}).sort("created_at",-1).limit(10))
            completed=[x for x in bookings if x.get("status")=="completed"]
            minutes=sum(int(x.get("actual_seconds",0) or 0) for x in completed)/60
            earnings=sum(float(x.get("host_earned",0) or 0) for x in completed)
            btxt="\\n".join(f"• {x.get('booking_id','-')} | {x.get('status','-')} | {x.get('minutes',0)}m | {x.get('host_earned',0)}₹" for x in bookings) or "No bookings"
            wtxt="\\n".join(f"• ₹{x.get('amount',0)} | {x.get('status','-')} | {x.get('withdrawal_id','-')}" for x in withdrawals) or "No withdrawals"
            gtxt="\\n".join(f"• {x.get('gift_name',x.get('name','Gift'))} | {x.get('tokens',0)} tokens | User {x.get('user_id','-')}" for x in gifts) or "No gifts"
            tg_send(chat_id,f"🎙️ <b>HOST HISTORY</b>\\nID: <code>{target}</code>\\nName: {h.get('name','-')}\\nUsername: @{h.get('username','').lstrip('@') or '-'}\\nStatus: {h.get('status','none')}\\nOnline: {h.get('online',False)}\\nAvailable Earnings: ₹{h.get('available_earnings',0)}\\n\\n📅 <b>Bookings</b>\\n{btxt}\\n\\n📹 Completed Calls: {len(completed)}\\n⏱️ Connected Minutes: {round(minutes,2)}\\n💰 Call Earnings: ₹{round(earnings,2)}\\n\\n🎁 <b>Gifts</b>\\n{gtxt}\\n\\n💸 <b>Withdrawals</b>\\n{wtxt}")
        elif cmd=="/user" and args:
            target=int(args[0]); u=user_doc(target) or {}; h=host_doc(target) or {}; tg_send(chat_id,f"👤 <b>USER</b>\nID: <code>{target}</code>\nName: {u.get('name','-')}\nUsername: @{u.get('username','').lstrip('@') or '-'}\nTokens: {u.get('tokens',0)}\nBlocked: {u.get('blocked',False)}\nHost status: {h.get('status','none')}")
        elif cmd=="/setcountry" and len(args)>=2:
            target=int(args[0]); country=args[1].upper()[:2]; ensure_user(target); col("users").update_one({"user_id":target},{"$set":{"country":country,"updated_at":now_ts()}}); col("hosts").update_one({"user_id":target},{"$set":{"country":country,"country_name":country,"updated_at":now_ts()}}); tg_send(chat_id,f"🌍 Country set for <code>{target}</code>: <b>{country}</b>")
        elif cmd=="/offer" and args:
            msg=" ".join(args); col("settings").update_one({"key":"announcement"},{"$set":{"key":"announcement","message":msg,"updated_at":now_ts()}},upsert=True); [notify_group(g,f"🎁 <b>OFFER</b>\n{msg}") for g in (GROUP_1_ID,GROUP_2_ID,GROUP_3_ID) if g]; tg_send(chat_id,"✅ Offer sent")
        elif cmd=="/clearannouncement":
            col("settings").update_one({"key":"announcement"},{"$set":{"message":"","updated_at":now_ts()}},upsert=True); tg_send(chat_id,"✅ Announcement cleared")
        elif cmd in ("/announce","/announcement") and args:
            msg=" ".join(args); col("settings").update_one({"key":"announcement"},{"$set":{"key":"announcement","message":msg,"updated_at":now_ts()}},upsert=True); 
            for gid in (GROUP_1_ID,GROUP_2_ID,GROUP_3_ID): notify_group(gid,f"📢 <b>ANNOUNCEMENT</b>\n{msg}")
            tg_send(chat_id,"✅ Announcement sent")
        elif cmd in ("/call","/calluser","/callhost") and args:
            target=int(args[0]); target_u=ensure_user(target)[0]
            cid=str(uuid.uuid4()); channel=f"admin_call_{cid}"
            col("direct_calls").insert_one({"call_id":cid,"type":"admin_direct","admin_id":from_id,"target_id":target,"channel":channel,"status":"ringing","created_at":now_ts()})
            tg_send(chat_id,f"📞 Calling <code>{target}</code>\nCall: <code>{cid}</code>\n💰 Free • No time limit")
            notify_user(target,"👑 <b>VYNORA SUPER ADMIN CALL</b>\n\nSuper Admin wants to speak with you immediately.\n💰 Free — no coins, no time limit.",[[{"text":"📞 JOIN ADMIN CALL","web_app":{"url":os.getenv("WEB_APP_URL","https://vynora-live-new.onrender.com")}}]])
            notify_full(f"👑 ADMIN DIRECT CALL\nAdmin: {from_id}\nTarget: {target}\nCall: <code>{cid}</code>\nFree: YES")
        elif cmd=="/stats":
            tg_send(chat_id,f"📊 Users: {col('users').count_documents({})}\nHosts: {col('hosts').count_documents({'status':'approved'})}\nPending Hosts: {col('hosts').count_documents({'status':'pending'})}\nBookings: {col('bookings').count_documents({})}\nCompleted Calls: {col('bookings').count_documents({'status':'completed'})}")
        else:
            return False
        return True
    except Exception as e:
        tg_send(chat_id,f"❌ Error: {e}"); return True


def handle_update(upd):
    if "callback_query" in upd:
        cq=upd["callback_query"]; data=cq.get("data",""); from_id=int(cq["from"]["id"]); chat_id=cq["message"]["chat"]["id"]; tg("answerCallbackQuery",{"callback_query_id":cq["id"]})
        try:
            action,key=data.split(":",1)
            if action in ("approve_host","reject_host") and from_id in ADMIN_IDS:
                target=int(key); ensure_user(target)
                status="approved" if action=="approve_host" else "rejected"
                u = user_doc(target) or {}
                col("hosts").update_one({"user_id":target},{"$set":{"user_id":target,"telegram_id":target,"status":status,"verified":status=="approved","verification_label":"Vynora Verified Host" if status=="approved" else "","demo":False,"online":False,"name":u.get("name","Host"),"username":u.get("username",""),"country":"IN","country_name":"India","updated_at":now_ts()},"$setOnInsert":{"total_tokens":0,"available_earnings":0,"gift_tokens":0,"filter_name":"natural"}},upsert=True)
                tg_send(chat_id,f"✅ Host {status}: <code>{target}</code>"); notify_user(target, f"{'🎙️ Host approved' if status=='approved' else '❌ Host application rejected'}."); notify_full(f"🎙️ HOST {status.upper()}\nAdmin: {from_id}\nHost: {target}\nTeam Action: Host approval status updated")
            elif action in ("approve_recharge","reject_recharge") and from_id in ADMIN_IDS:
                status="approved" if action=="approve_recharge" else "rejected"
                r=col("recharges").find_one_and_update(
                    {"recharge_id":key,"status":"pending"},
                    {"$set":{"status":status,"approved_by":from_id,"approved_at":now_ts()}},
                    return_document=ReturnDocument.AFTER
                )
                if not r:
                    tg_send(chat_id,f"⚠️ Recharge <code>{key}</code> already processed.")
                    return
                if status=="approved":
                    col("users").update_one({"user_id":r["user_id"]},{"$inc":{"tokens":r["tokens"]}})
                    notify_user(r["user_id"],f"✅ Recharge approved. {r['tokens']} Coins added.")
                else:
                    notify_user(r["user_id"],"❌ Recharge rejected. Contact support.")
                remove_recharge_screenshot(key)
                tg_send(chat_id,f"Recharge {status}: <code>{key}</code>\n🗑️ Payment screenshot deleted")
                notify_full(f"RECHARGE {status.upper()}\nUser: {r['user_id']}\n₹{r['rupees']} → {r['tokens']} Coins\nAdmin: {from_id}\nScreenshot: DELETED")
            elif action in ("approve_withdraw","reject_withdraw") and from_id in ADMIN_IDS:
                w=col("withdrawals").find_one({"withdrawal_id":key,"status":"pending"})
                if not w: return
                ok=action=="approve_withdraw"
                col("withdrawals").update_one({"withdrawal_id":key,"status":"pending"},{"$set":{"status":"paid" if ok else "rejected","processed_by":from_id,"processed_at":now_ts()}})
                if not ok:
                    col("hosts").update_one({"user_id":w["user_id"]},{"$inc":{"available_earnings":float(w["amount"])}})
                notify_user(w["user_id"], f"{'✅ Withdrawal paid.' if ok else '❌ Withdrawal rejected. Balance returned.'}")
                notify_full(f"💸 WITHDRAWAL {'PAID' if ok else 'REJECTED'}\nHost: {w['user_id']}\nAmount: ₹{w['amount']}\nAdmin: {from_id}")
                tg_send(chat_id,f"Withdrawal {'paid' if ok else 'rejected'}: <code>{key}</code>")
            elif action in ("accept_booking","reject_booking") and from_id in ADMIN_IDS:
                # Admin can process a booking request too.
                b=col("bookings").find_one({"booking_id":key});
                if not b: return
                if action=="reject_booking":
                    col("bookings").update_one({"booking_id":key},{"$set":{"status":"rejected","updated_at":now_ts()}}); col("users").update_one({"user_id":b["user_id"]},{"$inc":{"tokens":b["tokens"]}}); notify_user(b["user_id"],f"❌ Booking rejected. {b['tokens']} Coins refunded.")
                else:
                    col("bookings").update_one({"booking_id":key},{"$set":{"status":"accepted","updated_at":now_ts()}}); notify_user(b["user_id"],"✅ Booking accepted. Host/admin will schedule the call.")
                tg_send(chat_id,f"Booking {action.replace('_',' ')}: <code>{key}</code>")
            elif action=="join_admin_call":
                dc=col("direct_calls").find_one({"call_id":key})
                if not dc or from_id!=dc.get("target_id"): return
                col("direct_calls").update_one({"call_id":key},{"$set":{"status":"connected","joined_at":now_ts()}})
                notify_user(dc["admin_id"],f"📞 User/Host <code>{dc['target_id']}</code> joined your direct call.")
                tg_send(chat_id,"📞 Connected. Please stay on the call.")
        except Exception as e: log.warning("callback error %s",e)
        return
    msg=upd.get("message") or {}; text=msg.get("text",""); from_user=msg.get("from") or {}; from_id=int(from_user.get("id",0)); chat_id=msg.get("chat",{}).get("id")
    if not chat_id: return
    if text.startswith("/start"):
        name=(from_user.get("first_name","")+" "+from_user.get("last_name","")).strip() or "User"; username=from_user.get("username","")
        u,created=ensure_user(from_id,name,username)
        if created: notify_new_user(u)
        tg_send(chat_id, f"🎉 <b>VYNORA LIVE में आपका स्वागत है! 💜</b>\n\nनमस्ते {name} 👋\n\nयहाँ आप अपनी पसंद के Host के साथ\n📅 1-to-1 Video Call Slot Book कर सकते हैं।\n\n✨ Host चुनें → Slot Book करें → Confirmation पाएँ → Call करें\n\n💰 UPI/QR से Recharge करें और Coins से Slot Book करें।\n\n🔐 Secure • Private • 1-to-1 Calling\n\n👇 शुरू करने के लिए नीचे दिए बटन पर क्लिक करें।",[[{"text":"🚀 Open Vynora Live","web_app":{"url":os.getenv("WEB_APP_URL","https://vynora-live-new.onrender.com")}}]])
        return
    # Normal users do not have Telegram commands. They use the Mini App.
    # Super Admin commands remain available below.
    if text.startswith("/") and admin_command(chat_id,from_id,text): return
    if text.startswith("/"):
        tg_send(chat_id, "👋 App खोलने के लिए नीचे <b>Open Vynora Live</b> button का इस्तेमाल करें.", [[{"text":"🚀 Open Vynora Live","web_app":{"url":os.getenv("WEB_APP_URL","https://vynora-live-new.onrender.com")}}]])
        return


def webhook_url():
    base = os.getenv("WEB_APP_URL", "https://vynora-live-new.onrender.com").rstrip("/")
    return base + "/api/telegram/webhook"


def webhook_secret():
    return os.getenv("TELEGRAM_WEBHOOK_SECRET", "vynora_webhook_2026")


def set_telegram_webhook():
    if not BOT_TOKEN:
        log.info("BOT_TOKEN not set; Telegram webhook disabled")
        return False
    url = webhook_url()
    payload = {
        "url": url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
        "max_connections": 20,
        "secret_token": webhook_secret(),
    }
    result = tg("setWebhook", payload) or {}
    if result.get("ok"):
        log.info("Telegram webhook mode ACTIVE: %s", url)
        return True
    log.warning("Telegram setWebhook failed: %s", result)
    return False


@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN is not configured")

    expected = webhook_secret()
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if expected and received != expected:
        raise HTTPException(403, "Invalid Telegram webhook secret")

    try:
        update = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON update")

    try:
        handle_update(update)
    except Exception as e:
        log.exception("telegram webhook update failed: %s", e)
        # Return 200 so Telegram does not repeatedly retry a malformed/failed update.
    return {"ok": True}


@app.get("/api/telegram/webhook-info")
def telegram_webhook_info():
    if not BOT_TOKEN:
        return {"ok": False, "error": "BOT_TOKEN is not configured"}
    return tg("getWebhookInfo", {}) or {"ok": False, "error": "Telegram API unavailable"}


def remove_dummy_hosts():
    """Remove all legacy dummy/demo host records so only real hosts remain."""
    try:
        result = col("hosts").delete_many({"demo":True})
        # Remove the known legacy dummy user accounts as well.
        col("users").delete_many({"user_id":{"$in":[910000001,910000002,910000003]}})
        if result.deleted_count:
            log.info("Removed %s legacy dummy host records", result.deleted_count)
    except Exception as e:
        log.warning("dummy host cleanup: %s", e)


def finalize_call(booking_id, ended_at=None, reason="completed"):
    b=col("bookings").find_one({"booking_id":booking_id})
    if not b: return None
    end=int(ended_at or now_ts())
    start=int(b.get("call_started_at") or end)
    actual=max(0,end-start)
    booked_tokens=int(b.get("tokens",0))
    # Actual connected time is the paid time. Billing is capped at the booked slot.
    if actual <= 0:
        charged=0
    else:
        booked_minutes=max(1, int(b.get("minutes",1)))
        # Charge according to the selected package price, not a hard-coded per-minute rate.
        # Billing is rounded up to the connected minute and capped at the package price.
        per_minute=booked_tokens / booked_minutes
        connected_minutes=max(1, (actual+59)//60)
        charged=min(booked_tokens, int(round(per_minute*connected_minutes)))
    refund=max(0,booked_tokens-charged)
    host_earned=round(charged*HOST_SHARE,2)
    platform_earned=round(charged*(1-HOST_SHARE),2)
    fresh=col("bookings").find_one_and_update(
        {"booking_id":booking_id,"earnings_credited":{"$ne":True}},
        {"$set":{"status":"completed","call_ended_at":end,"actual_seconds":actual,"charged_tokens":charged,"refunded_tokens":refund,"host_earned":host_earned,"platform_earned":platform_earned,"earnings_credited":True,"completion_reason":reason,"updated_at":end}},
        return_document=ReturnDocument.AFTER
    )
    if not fresh: return b
    if refund:
        col("users").update_one({"user_id":b["user_id"]},{"$inc":{"tokens":refund}})
    col("hosts").update_one({"user_id":b["host_id"]},{"$inc":{"total_tokens":host_earned,"available_earnings":host_earned},"$set":{"updated_at":end}})
    report=(f"📊 <b>1v1 CALL COMPLETED</b>\n\nBooking: <code>{b['booking_id']}</code>\nUser: {b['user_id']}\nHost: {b['host_id']}\nBooked: {b['minutes']} min\nActual Connected: {round(actual/60,2)} min\nCharged: {charged} Coins\nRefunded: {refund} Coins\nHost 60%: ₹{host_earned:.2f}\nPlatform 40%: ₹{platform_earned:.2f}\nStart: {iso(start)}\nEnd: {iso(end)}")
    notify_request(report)
    notify_full(report)
    notify_user(b["user_id"],f"🟢 Call completed.\nActual connected time: {round(actual/60,2)} min\nCharged: {charged} Coins\nRefunded: {refund} Coins")
    notify_user(b["host_id"],f"💰 Call completed.\nActual connected time: {round(actual/60,2)} min\nYour earning: ₹{host_earned:.2f}")
    pending=col("bookings").find({"host_id":b["host_id"],"status":"pending","busy_at_request":True})
    host_name=(host_doc(b["host_id"]) or {}).get("name","Host")
    for pb in pending:
        notify_user(pb["user_id"],f"🟢 <b>{host_name} is available again</b>\nYour booking request is still waiting for host confirmation.")
    return fresh


def call_watchdog():
    while True:
        try:
            t=now_ts()
            # Send the actual call-time Telegram reminder once.
            for b in col("bookings").find({"status":"scheduled","scheduled_start":{"$lte":t},"call_started_at":None,"call_reminded_at":{"$exists":False}}):
                col("bookings").update_one({"booking_id":b["booking_id"],"call_reminded_at":{"$exists":False}},{"$set":{"call_reminded_at":t}})
                notify_user(b["user_id"],f"📹 <b>Your Host is ready now</b>\nJoin your {b['minutes']}-minute video call in Vynora Live.", [[{"text":"📹 JOIN VIDEO CALL","web_app":{"url":os.getenv("WEB_APP_URL","https://vynora-live-new.onrender.com")}}]])
                notify_user(b["host_id"],f"📹 <b>Your scheduled call is ready</b>\nUser: <code>{b['user_id']}</code>\nJoin now to start the paid timer when both connect.", [[{"text":"📹 JOIN VIDEO CALL","web_app":{"url":os.getenv("WEB_APP_URL","https://vynora-live-new.onrender.com")}}]])
                notify_full(f"📹 CALL READY\nBooking: {b['booking_id']}\nUser: {b['user_id']}\nHost: {b['host_id']}")
            for b in col("bookings").find({"status":"scheduled","scheduled_start":{"$lte":t}}):
                if t >= int(b.get("scheduled_start",t)) + int(b.get("minutes",1))*60:
                    col("bookings").update_one({"booking_id":b["booking_id"],"status":"scheduled"},{"$set":{"status":"completed","call_ended_at":t,"actual_seconds":0,"charged_tokens":0,"refunded_tokens":b.get("tokens",0),"host_earned":0,"platform_earned":0,"earnings_credited":True,"completion_reason":"no_connect"}})
                    col("users").update_one({"user_id":b["user_id"]},{"$inc":{"tokens":int(b.get("tokens",0))}})
                    notify_full(f"📊 CALL MISSED / NO CONNECT\nBooking: {b['booking_id']}\nUser: {b['user_id']}\nHost: {b['host_id']}\nRefunded: {b['tokens']} Coins")
                    notify_user(b["user_id"],"⏰ Call window ended without both participants connecting. Your reserved Coins were refunded.")
            for b in col("bookings").find({"status":"calling","scheduled_end":{"$lte":t}}):
                finalize_call(b["booking_id"], t, "timer_expired")
        except Exception as e: log.warning("watchdog error: %s",e)
        time.sleep(5)


@app.on_event("startup")
def startup():
    log.info('Vynora startup: BOT_TOKEN=%s, MONGO=%s, GROUP1=%s, GROUP2=%s, GROUP3=%s, AGORA=%s', bool(BOT_TOKEN), bool(MONGO_URI), bool(GROUP_1_ID), bool(GROUP_2_ID), bool(GROUP_3_ID), bool(AGORA_APP_ID and AGORA_APP_CERTIFICATE))
    if db is not None:
        try:
            col("users").create_index("user_id", unique=True)
            col("hosts").create_index("user_id", unique=True)
            col("bookings").create_index("booking_id", unique=True)
            col("recharges").create_index("recharge_id", unique=True)
            col("direct_calls").create_index("call_id", unique=True)
            col("notifications").create_index([("user_id",1),("created_at",-1)])
        except Exception as e: log.warning("index setup: %s",e)
    if db is not None:
        remove_dummy_hosts()
    if BOT_TOKEN:
        # Webhook replaces long polling. Telegram does not allow getUpdates while
        # an outgoing webhook is configured.
        threading.Thread(target=set_telegram_webhook, daemon=True).start()
    if db is not None:
        threading.Thread(target=call_watchdog, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT","8000")))
