import os, time, uuid, threading, logging, base64, mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument

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
    {"minutes": 1, "tokens": 20},
    {"minutes": 3, "tokens": 50},
    {"minutes": 6, "tokens": 100},
    {"minutes": 10, "tokens": 160},
    {"minutes": 15, "tokens": 240},
    {"minutes": 20, "tokens": 320},
    {"minutes": 30, "tokens": 450},
]
RECHARGE_PLANS = [
    {"rupees": 50, "tokens": 50}, {"rupees": 100, "tokens": 105},
    {"rupees": 200, "tokens": 210}, {"rupees": 300, "tokens": 320},
    {"rupees": 500, "tokens": 550}, {"rupees": 1000, "tokens": 1150},
    {"rupees": 1500, "tokens": 1750}, {"rupees": 2000, "tokens": 2400},
]

app = FastAPI(title="Vynora Live 1v1", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

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


def iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def uid(v):
    try: return int(v)
    except Exception: raise HTTPException(400, "Invalid user id")


def user_doc(user_id):
    return col("users").find_one({"user_id": uid(user_id)})


def ensure_user(user_id, name="", username=""):
    user_id = uid(user_id)
    existing = col("users").find_one({"user_id": user_id})
    if not existing:
        d = {"user_id": user_id, "name": name or "User", "username": username or "", "tokens": 0, "blocked": False, "created_at": now_ts(), "updated_at": now_ts()}
        col("users").insert_one(d)
        return d, True
    updates = {"updated_at": now_ts()}
    if name: updates["name"] = name
    if username is not None: updates["username"] = username
    col("users").update_one({"user_id": user_id}, {"$set": updates})
    return col("users").find_one({"user_id": user_id}), False


def is_admin(user_id):
    return uid(user_id) in ADMIN_IDS


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


def notify_group(chat_id, text, buttons=None):
    if not BOT_TOKEN or not chat_id:
        return False
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=15)
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
    return FileResponse(BASE / "index.html")

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
    return {"ok": True, "mongo": mongo_ok, "admins": sorted(ADMIN_IDS), "booking_plans": BOOKING_PLANS}

@app.get("/api/config")
def config():
    return {"booking_plans": BOOKING_PLANS, "recharge_plans": RECHARGE_PLANS, "host_share": HOST_SHARE, "upi_id": UPI_ID, "upi_name": UPI_NAME, "support_url": SUPPORT_URL}

@app.post("/api/start")
def start(data: StartModel):
    u, created = ensure_user(data.user_id, data.name, data.username)
    if created:
        notify_new_user(u)
    return {"status": "success", "new_user": created, "user": {"user_id": u["user_id"], "name": u.get("name"), "username": u.get("username"), "tokens": u.get("tokens", 0), "blocked": u.get("blocked", False)}}

@app.get("/api/user/{user_id}")
def get_user(user_id: int):
    u = require_user(user_id)
    h = host_doc(user_id)
    return {"user": {"user_id": u["user_id"], "name": u.get("name"), "username": u.get("username"), "tokens": u.get("tokens",0), "photo_url": public_photo_url(user_id, u)}, "host": h or None, "is_admin": is_admin(user_id)}

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
async def upload_profile_photo(user_id: int, file: UploadFile = File(...)):
    require_user(user_id)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Please upload an image file")
    raw = await file.read()
    if len(raw) > 900_000:
        raise HTTPException(400, "Photo is too large. Please use an image below 900 KB.")
    encoded = base64.b64encode(raw).decode("ascii")
    data_uri = f"data:{file.content_type};base64,{encoded}"
    col("users").update_one(
        {"user_id": uid(user_id)},
        {"$set": {
            "photo_data": data_uri,
            "photo_url": "",
            "photo_filename": file.filename or "profile.jpg",
            "photo_content_type": file.content_type,
            "photo_updated_at": now_ts(),
            "updated_at": now_ts()
        }}
    )
    return {"status": "success", "photo_url": public_photo_url(user_id)}

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
    for h in col("hosts").find({"status":"approved"}).sort("updated_at", -1):
        u = user_doc(h["user_id"]) or {}
        rows.append({"user_id": h["user_id"], "name": h.get("name") or u.get("name","Host"), "username": h.get("username") or u.get("username",""), "photo_url": public_photo_url(h["user_id"], u) or h.get("photo_url",""), "online": bool(h.get("online")), "total_tokens": int(h.get("total_tokens",0)), "available_earnings": float(h.get("available_earnings",0)), "status": h.get("status")})
    return rows

@app.get("/api/host/{host_id}")
def host_profile(host_id: int):
    h = host_or_404(host_id); u = user_doc(host_id) or {}
    return {"user_id":host_id,"name":h.get("name") or u.get("name","Host"),"username":h.get("username") or u.get("username",""),"photo_url":public_photo_url(host_id, u) or h.get("photo_url",""),"online":bool(h.get("online")),"total_tokens":int(h.get("total_tokens",0)),"available_earnings":float(h.get("available_earnings",0)),"status":h.get("status")}

@app.post("/api/host/apply")
def host_apply(data: HostApplyModel):
    require_user(data.user_id)
    existing = host_doc(data.user_id)
    d = {"user_id":uid(data.user_id),"name":data.name,"username":data.username,"phone":data.phone,"age":data.age,"bio":data.bio,"status":"pending","online":False,"total_tokens":0,"available_earnings":0,"created_at":now_ts(),"updated_at":now_ts()}
    if existing and existing.get("status") == "approved": raise HTTPException(400,"Already an approved host")
    col("hosts").update_one({"user_id":uid(data.user_id)}, {"$set":d}, upsert=True)
    text = f"🎙️ <b>HOST APPLICATION</b>\n\nName: {data.name}\nUser ID: <code>{data.user_id}</code>\nUsername: @{data.username.lstrip('@') or '-'}\nPhone: {data.phone or '-'}\nAge: {data.age or '-'}\nBio: {data.bio or '-'}"
    notify_request(text, [[{"text":"✅ Approve Host","callback_data":f"approve_host:{data.user_id}"},{"text":"❌ Reject","callback_data":f"reject_host:{data.user_id}"}]])
    notify_full(text)
    return {"status":"success","message":"Host application submitted"}

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
    plan=next((p for p in BOOKING_PLANS if p["minutes"]==int(data.minutes)),None)
    if not plan: raise HTTPException(400,"Invalid duration")
    start=int(data.requested_start); end=start+plan["minutes"]*60
    if start < now_ts()-60: raise HTTPException(400,"Please choose a future time")
    u=user_doc(data.user_id)
    if int(u.get("tokens",0)) < plan["tokens"]: raise HTTPException(400,"Insufficient tokens")
    # Reserve money immediately; refund on reject/cancel.
    col("users").update_one({"user_id":uid(data.user_id),"tokens":{"$gte":plan["tokens"]}}, {"$inc":{"tokens":-plan["tokens"]}})
    busy=overlap(data.host_id,start,end)
    bid=str(uuid.uuid4())
    doc={"booking_id":bid,"user_id":uid(data.user_id),"host_id":uid(data.host_id),"minutes":plan["minutes"],"tokens":plan["tokens"],"requested_start":start,"scheduled_start":None,"scheduled_end":None,"status":"pending","busy_at_request":busy,"created_at":now_ts(),"updated_at":now_ts(),"call_started_at":None,"call_ended_at":None,"user_joined_at":None,"host_joined_at":None,"earnings_credited":False}
    col("bookings").insert_one(doc)
    notify_user(data.user_id, f"✅ <b>Booking request submitted</b>\n\nHost: {h.get('name','Host')}\nDuration: {plan['minutes']} min\nCharge: {plan['tokens']} Coins\n\nHost confirmation ka wait karein. Agar Host busy hai to aapko update milega.")
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
        notify_user(b["user_id"],"✅ Host accepted your request. Ab Host call ka time schedule karegi.")
        return {"status":"success","next":"schedule","booking_id":b["booking_id"]}
    raise HTTPException(400,"Unknown action")

@app.post("/api/booking/schedule")
def schedule(data: ScheduleModel):
    b=col("bookings").find_one({"booking_id":data.booking_id,"host_id":uid(data.host_id)})
    if not b or b.get("status")!="accepted": raise HTTPException(404,"Accepted booking not found")
    start=int(data.scheduled_start); end=start+int(b["minutes"])*60
    if start < now_ts(): raise HTTPException(400,"Schedule must be in the future")
    if overlap(data.host_id,start,end):
        # Allow the current booking itself but not another booking.
        other=col("bookings").find_one({"booking_id":{"$ne":data.booking_id},"host_id":uid(data.host_id),"status":{"$in":["accepted","scheduled","calling"]},"scheduled_start":{"$lt":end},"scheduled_end":{"$gt":start}})
        if other: raise HTTPException(409,"Host is busy at that time")
    col("bookings").update_one({"booking_id":data.booking_id},{"$set":{"scheduled_start":start,"scheduled_end":end,"status":"scheduled","updated_at":now_ts()}})
    notify_user(b["user_id"],f"📞 <b>Call Scheduled</b>\n\nHost: {host_doc(b['host_id']).get('name','Host')}\nTime: {iso(start)}\nDuration: {b['minutes']} min\n\nScheduled time par JOIN CALL button milega.")
    notify_full(f"🕐 BOOKING SCHEDULED\nBooking: <code>{b['booking_id']}</code>\nHost: {b['host_id']}\nUser: {b['user_id']}\nStart: {iso(start)}\nDuration: {b['minutes']} min")
    return {"status":"success","scheduled_start":start,"scheduled_end":end}

@app.post("/api/call/join")
def call_join(data: CallJoinModel):
    b=col("bookings").find_one({"booking_id":data.booking_id})
    if not b: raise HTTPException(404,"Booking not found")
    if uid(data.user_id) not in (b["user_id"],b["host_id"]): raise HTTPException(403,"Not a participant")
    if b.get("status") not in ("scheduled","calling"): raise HTTPException(400,"Call is not scheduled")
    field="user_joined_at" if uid(data.user_id)==b["user_id"] else "host_joined_at"
    update={field:now_ts(),"updated_at":now_ts()}
    # First participant does not start timer. Timer starts when both fields exist.
    col("bookings").update_one({"booking_id":data.booking_id},{"$set":update})
    fresh=col("bookings").find_one({"booking_id":data.booking_id})
    if fresh.get("user_joined_at") and fresh.get("host_joined_at") and not fresh.get("call_started_at"):
        start=now_ts(); end=start+int(fresh["minutes"])*60
        col("bookings").update_one({"booking_id":data.booking_id},{"$set":{"status":"calling","call_started_at":start,"scheduled_end":end,"updated_at":start}})
        fresh=col("bookings").find_one({"booking_id":data.booking_id})
    return {"status":"success","booking":{k:fresh.get(k) for k in ["booking_id","minutes","tokens","status","call_started_at","scheduled_end","user_joined_at","host_joined_at"]}}

@app.post("/api/call/end")
def call_end(data: CallEndModel):
    b=col("bookings").find_one({"booking_id":data.booking_id})
    if not b: raise HTTPException(404,"Booking not found")
    if uid(data.user_id) not in (b["user_id"],b["host_id"]): raise HTTPException(403,"Not a participant")
    if b.get("status") in ("completed","rejected","cancelled"): return {"status":"success"}
    end=now_ts(); start=int(b.get("call_started_at") or end); actual=max(0,end-start)
    col("bookings").update_one({"booking_id":data.booking_id},{"$set":{"status":"completed","call_ended_at":end,"actual_seconds":actual,"updated_at":end}})
    # Only once credit host.
    fresh=col("bookings").find_one_and_update({"booking_id":data.booking_id,"earnings_credited":{"$ne":True}},{"$set":{"earnings_credited":True},"$inc":{"host_earned":b["tokens"]*HOST_SHARE,"platform_earned":b["tokens"]*(1-HOST_SHARE)}} ,return_document=ReturnDocument.AFTER)
    if fresh:
        col("hosts").update_one({"user_id":b["host_id"]},{"$inc":{"total_tokens":b["tokens"]*HOST_SHARE,"available_earnings":b["tokens"]*HOST_SHARE},"$set":{"online":False,"updated_at":end}})
    mins=round(actual/60,2)
    report=(f"📊 <b>1v1 CALL COMPLETED</b>\n\nBooking: <code>{b['booking_id']}</code>\nUser: {b['user_id']}\nHost: {b['host_id']}\nBooked: {b['minutes']} min\nActual: {mins} min\nCharge: {b['tokens']} Coins\nHost 60%: {b['tokens']*HOST_SHARE:.2f}\nPlatform: {b['tokens']*(1-HOST_SHARE):.2f}\nStart: {iso(start)}\nEnd: {iso(end)}")
    notify_request(report)
    notify_full(report)
    notify_user(b["user_id"],"🟢 Call completed. Host ab available hai; aap dobara booking kar sakte hain.")
    return {"status":"success","actual_seconds":actual,"host_earned":b["tokens"]*HOST_SHARE}

@app.get("/api/agora-token")
def agora_token(channelName: str, uid: int, role: str="publisher"):
    if not AGORA_APP_ID or not AGORA_APP_CERTIFICATE or not RtcTokenBuilder:
        raise HTTPException(503,"Agora is not configured")
    expiry=now_ts()+3600
    token=RtcTokenBuilder.buildTokenWithUid(AGORA_APP_ID,AGORA_APP_CERTIFICATE,channelName,int(uid),Role_Publisher,expiry)
    return {"appId":AGORA_APP_ID,"channelName":channelName,"uid":int(uid),"token":token,"expiresAt":expiry}

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

@app.get("/api/direct-call/pending/{target_id}")
def pending_direct_call(target_id:int):
    require_user(target_id)
    d=col("direct_calls").find_one({"target_id":uid(target_id),"status":{"$in":["ringing","connected"]}},sort=[("created_at",-1)])
    if not d: return {"active":False}
    d["_id"]=str(d["_id"])
    return {"active":True,"call":d}

@app.post("/api/direct-call/end/{call_id}")
def end_direct_call(call_id:str, user_id:int):
    if not is_admin(user_id):
        d=col("direct_calls").find_one({"call_id":call_id})
        if not d or d.get("host_id")!=uid(user_id): raise HTTPException(403,"Not allowed")
    col("direct_calls").update_one({"call_id":call_id},{"$set":{"status":"ended","ended_at":now_ts()}})
    return {"status":"success"}

@app.get("/api/admin/direct-calls/{admin_id}")
def direct_calls(admin_id:int):
    if not is_admin(admin_id): raise HTTPException(403,"Admin only")
    return list(col("direct_calls").find({"admin_id":uid(admin_id)}).sort("created_at",-1).limit(20))

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
    host_or_404(user_id); col("hosts").update_one({"user_id":uid(user_id)},{"$set":{"online":False,"updated_at":now_ts()}}); return {"status":"success","online":False}

@app.get("/api/host/{host_id}/stats")
def host_stats(host_id:int):
    h=host_or_404(host_id)
    return {"total_tokens":h.get("total_tokens",0),"available_earnings":h.get("available_earnings",0),"total_calls":col("bookings").count_documents({"host_id":uid(host_id),"status":"completed"}),"online":bool(h.get("online"))}

@app.post("/api/withdraw")
def withdraw(user_id:int, amount:float):
    h=host_or_404(user_id)
    if amount < 700: raise HTTPException(400,"Minimum withdrawal is ₹700")
    if amount > float(h.get("available_earnings",0)): raise HTTPException(400,"Insufficient balance")
    wid=str(uuid.uuid4()); col("withdrawals").insert_one({"withdrawal_id":wid,"user_id":uid(user_id),"amount":amount,"status":"pending","created_at":now_ts()})
    col("hosts").update_one({"user_id":uid(user_id)},{"$inc":{"available_earnings":-amount}})
    notify_request(f"💸 WITHDRAWAL REQUEST\nHost: {user_id}\nAmount: ₹{amount}\nID: <code>{wid}</code>")
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
            tg_send(chat_id,"👑 <b>VYNORA ADMIN COMMANDS</b>\n\n/addtoken ID AMOUNT\n/removetoken ID AMOUNT\n/settoken ID AMOUNT\n/approvehost ID\n/rejecthost ID\n/addhost ID\n/approveuser ID\n/user ID\n/ban ID [reason]\n/unban ID\n/block ID [reason]\n/unblock ID\n/approverecharge RECHARGE_ID\n/rejectrecharge RECHARGE_ID\n/approvebooking BOOKING_ID\n/rejectbooking BOOKING_ID\n/announce MESSAGE\n/offer MESSAGE\n/clearannouncement\n/callhost HOST_ID\n/stats")
        elif cmd in ("/addtoken","/removetoken","/settoken") and len(args)>=2:
            target=int(args[0]); amount=int(args[1]); u=ensure_user(target)[0]
            if cmd=="/addtoken": col("users").update_one({"user_id":target},{"$inc":{"tokens":amount}})
            elif cmd=="/removetoken": col("users").update_one({"user_id":target},{"$inc":{"tokens":-amount}})
            else: col("users").update_one({"user_id":target},{"$set":{"tokens":max(0,amount)}})
            tg_send(chat_id,f"✅ Token updated for <code>{target}</code>")
            notify_full(f"🪙 ADMIN TOKEN ACTION\nAdmin: {from_id}\nUser: {target}\nCommand: {cmd}\nAmount: {amount}")
        elif cmd in ("/approvehost","/addhost") and args:
            target=int(args[0]); ensure_user(target); col("hosts").update_one({"user_id":target},{"$set":{"user_id":target,"status":"approved","online":False,"updated_at":now_ts()},"$setOnInsert":{"total_tokens":0,"available_earnings":0}},upsert=True); tg_send(chat_id,f"✅ Host approved: <code>{target}</code>"); notify_user(target,"🎙️ आपका Host account approve हो गया है। अब आप bookings receive कर सकते हैं।"); notify_full(f"✅ HOST APPROVED\nAdmin: {from_id}\nHost: {target}")
        elif cmd in ("/rejecthost","/removehost") and args:
            target=int(args[0]); col("hosts").update_one({"user_id":target},{"$set":{"status":"rejected","online":False}}); tg_send(chat_id,f"❌ Host rejected/removed: <code>{target}</code>"); notify_full(f"❌ HOST REJECTED\nAdmin: {from_id}\nHost: {target}")
        elif cmd in ("/ban","/block") and args:
            target=int(args[0]); reason=args[1] if len(args)>1 else "Admin action"; col("users").update_one({"user_id":target},{"$set":{"blocked":True,"block_reason":reason,"blocked_at":now_ts()}},upsert=True); col("hosts").update_one({"user_id":target},{"$set":{"status":"blocked","online":False}}); tg_send(chat_id,f"🚫 Blocked <code>{target}</code>"); notify_user(target,"🚫 आपका account block कर दिया गया है। Support से संपर्क करें।"); notify_full(f"🚫 BLOCKED\nAdmin: {from_id}\nUser: {target}\nReason: {reason}")
        elif cmd in ("/unban","/unblock") and args:
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
            tg_send(chat_id,f"Recharge {status}: <code>{rid}</code>"); notify_full(f"RECHARGE {status.upper()}\nUser: {r['user_id']}\n₹{r['rupees']} → {r['tokens']} Coins\nAdmin: {from_id}")
        elif cmd in ("/approvebooking","/rejectbooking") and args:
            bid=args[0]; b=col("bookings").find_one({"booking_id":bid})
            if not b: raise ValueError("Booking not found")
            ok=cmd=="/approvebooking"; status="accepted" if ok else "rejected"
            col("bookings").update_one({"booking_id":bid},{"$set":{"status":status,"updated_at":now_ts()}})
            if ok: notify_user(b["user_id"],"✅ Booking accepted. Host/admin will schedule the call.")
            else: col("users").update_one({"user_id":b["user_id"]},{"$inc":{"tokens":b["tokens"]}}); notify_user(b["user_id"],f"❌ Booking rejected. {b['tokens']} Coins refunded.")
            tg_send(chat_id,f"Booking {status}: <code>{bid}</code>"); notify_full(f"BOOKING {status.upper()}\nBooking: {bid}\nUser: {b['user_id']}\nHost: {b['host_id']}\nAdmin: {from_id}")
        elif cmd=="/approveuser" and args:
            target=int(args[0]); ensure_user(target); col("users").update_one({"user_id":target},{"$set":{"blocked":False,"updated_at":now_ts()}}); tg_send(chat_id,f"✅ User approved/unblocked: <code>{target}</code>"); notify_user(target,"✅ आपका account active है।")
        elif cmd=="/user" and args:
            target=int(args[0]); u=user_doc(target) or {}; h=host_doc(target) or {}; tg_send(chat_id,f"👤 <b>USER</b>\nID: <code>{target}</code>\nName: {u.get('name','-')}\nUsername: @{u.get('username','').lstrip('@') or '-'}\nTokens: {u.get('tokens',0)}\nBlocked: {u.get('blocked',False)}\nHost status: {h.get('status','none')}")
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
                col("hosts").update_one({"user_id":target},{"$set":{"user_id":target,"status":status,"online":False,"updated_at":now_ts()},"$setOnInsert":{"total_tokens":0,"available_earnings":0}},upsert=True)
                tg_send(chat_id,f"✅ Host {status}: <code>{target}</code>"); notify_user(target, f"{'🎙️ Host approved' if status=='approved' else '❌ Host application rejected'}."); notify_full(f"HOST {status.upper()}\nAdmin: {from_id}\nHost: {target}")
            elif action in ("approve_recharge","reject_recharge") and from_id in ADMIN_IDS:
                r=col("recharges").find_one({"recharge_id":key})
                if not r: return
                status="approved" if action=="approve_recharge" else "rejected"
                col("recharges").update_one({"recharge_id":key,"status":"pending"},{"$set":{"status":status,"approved_by":from_id,"approved_at":now_ts()}})
                if status=="approved": col("users").update_one({"user_id":r["user_id"]},{"$inc":{"tokens":r["tokens"]}}); notify_user(r["user_id"],f"✅ Recharge approved. {r['tokens']} Coins added.")
                else: notify_user(r["user_id"],"❌ Recharge rejected. Contact support.")
                tg_send(chat_id,f"Recharge {status}: <code>{key}</code>"); notify_full(f"RECHARGE {status.upper()}\nUser: {r['user_id']}\n₹{r['rupees']} → {r['tokens']} Coins\nAdmin: {from_id}")
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


def call_watchdog():
    while True:
        try:
            t=now_ts()
            for b in col("bookings").find({"status":"calling","scheduled_end":{"$lte":t}}):
                col("bookings").update_one({"booking_id":b["booking_id"],"status":"calling"},{"$set":{"status":"completed","call_ended_at":t,"actual_seconds":max(0,t-int(b.get("call_started_at") or t))}})
                # Credit once and notify through the same completion path.
                fresh=col("bookings").find_one_and_update({"booking_id":b["booking_id"],"earnings_credited":{"$ne":True}},{"$set":{"earnings_credited":True},"$inc":{"host_earned":b["tokens"]*HOST_SHARE,"platform_earned":b["tokens"]*(1-HOST_SHARE)}},return_document=ReturnDocument.AFTER)
                if fresh: col("hosts").update_one({"user_id":b["host_id"]},{"$inc":{"total_tokens":b["tokens"]*HOST_SHARE,"available_earnings":b["tokens"]*HOST_SHARE},"$set":{"online":False}})
                report=f"📊 AUTO COMPLETED CALL\nBooking: <code>{b['booking_id']}</code>\nUser: {b['user_id']}\nHost: {b['host_id']}\nCharge: {b['tokens']} Coins\nHost 60%: {b['tokens']*HOST_SHARE:.2f}"
                notify_request(report); notify_full(report); notify_user(b["user_id"],"🟢 आपका call पूरा हो गया। Host अब available है, आप फिर booking कर सकते हैं।")
        except Exception as e: log.warning("watchdog error: %s",e)
        time.sleep(5)


@app.on_event("startup")
def startup():
    if db is not None:
        try:
            col("users").create_index("user_id", unique=True)
            col("hosts").create_index("user_id", unique=True)
            col("bookings").create_index("booking_id", unique=True)
            col("recharges").create_index("recharge_id", unique=True)
            col("direct_calls").create_index("call_id", unique=True)
            col("notifications").create_index([("user_id",1),("created_at",-1)])
        except Exception as e: log.warning("index setup: %s",e)
    if BOT_TOKEN:
        # Webhook replaces long polling. Telegram does not allow getUpdates while
        # an outgoing webhook is configured.
        threading.Thread(target=set_telegram_webhook, daemon=True).start()
    if db is not None:
        threading.Thread(target=call_watchdog, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT","8000")))







