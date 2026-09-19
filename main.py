import os
import hmac
import hashlib
import urllib.parse
import json
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pymongo import MongoClient
import requests

# Load Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
ADMIN_IDS = [str(x).strip() for x in os.getenv("ADMIN_IDS", "7778606261,7001825467").split(",")]
GROUP_1_ID = os.getenv("GROUP_1_ID", "-1001234567891")
GROUP_2_ID = os.getenv("GROUP_2_ID", "-1001234567892")
GROUP_3_ID = os.getenv("GROUP_3_ID", "-1001234567893")
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://your-render-app.onrender.com")
AGORA_APP_ID = os.getenv("AGORA_APP_ID", "YOUR_AGORA_APP_ID")
AGORA_APP_CERTIFICATE = os.getenv("AGORA_APP_CERTIFICATE", "YOUR_AGORA_APP_CERTIFICATE")
HOST_SHARE = float(os.getenv("HOST_SHARE", "0.6"))
UPI_ID = os.getenv("UPI_ID", "vynoralive@slc")

app = FastAPI(title="Vynora Live Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MongoDB Connection
client = MongoClient(MONGO_URI)
db = client["vynora_live"]

# Collections
users_col = db["users"]
hosts_col = db["hosts"]
bookings_col = db["bookings"]
calls_col = db["calls"]
transactions_col = db["transactions"]
recharges_col = db["recharges"]
withdrawals_col = db["withdrawals"]
notifications_col = db["notifications"]
announcements_col = db["announcements"]
settings_col = db["settings"]
gifts_col = db["gifts"]
direct_calls_col = db["direct_calls"]

# Ensure default settings safely
try:
    if settings_col.count_documents({}) == 0:
        settings_col.insert_one({
            "token_rate_inr": 1,
            "call_rates": {
                "1": 20, "5": 100, "10": 200, "15": 300, "20": 400, "25": 500, "30": 600
            },
            "host_share": 0.6,
            "platform_share": 0.4
        })
except Exception as e:
    print(f"Settings init error: {e}")

# --- TELEGRAM HELPER FUNCTIONS ---
def send_telegram_message(chat_id: str, text: str, reply_markup: Optional[dict] = None):
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram send error: {e}")

def send_telegram_photo(chat_id: str, photo_bytes, caption: str, reply_markup: Optional[dict] = None):
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        files = {"photo": ("screenshot.jpg", photo_bytes)}
        requests.post(url, data=data, files=files, timeout=10)
    except Exception as e:
        print(f"Telegram photo send error: {e}")

def validate_telegram_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram initData")
    try:
        parsed = urllib.parse.parse_qsl(init_data)
        data_dict = dict(parsed)
        received_hash = data_dict.pop("hash", "")
        
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_dict.items()))
        
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash != received_hash:
            if BOT_TOKEN != "YOUR_BOT_TOKEN":
                raise HTTPException(status_code=401, detail="Invalid Telegram signature")
                
        user_data = json.loads(data_dict.get("user", "{}"))
        return user_data
    except Exception as e:
        if BOT_TOKEN == "YOUR_BOT_TOKEN":
            return {"id": 7778606261, "first_name": "Admin", "username": "admin"}
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")

# --- PYDANTIC MODELS ---
class BookingCreate(BaseModel):
    host_id: str
    date: str
    time: str
    duration_minutes: int

# --- API ROUTES ---
@app.post("/api/auth/verify")
def verify_auth(payload: dict):
    init_data = payload.get("initData", "")
    tg_user = validate_telegram_init_data(init_data)
    user_id = str(tg_user.get("id"))
    username = tg_user.get("username", "user_" + user_id)
    first_name = tg_user.get("first_name", "User")
    
    user = users_col.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "username": username,
            "name": first_name,
            "country": "India",
            "tokens": 50,
            "blocked": False,
            "banned": False,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        users_col.insert_one(user)
        reg_text = (
            f"👤 <b>New User Joined</b>\n\n"
            f"Name: {first_name}\n"
            f"Username: @{username}\n"
            f"Telegram ID: {user_id}\n"
            f"Country: India\n"
            f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S')}"
        )
        send_telegram_message(GROUP_2_ID, reg_text)

    is_admin = user_id in ADMIN_IDS
    host_record = hosts_col.find_one({"user_id": user_id})
    is_host = bool(host_record and host_record.get("status") == "approved")
    
    return {
        "status": "success",
        "user_id": user_id,
        "name": user.get("name"),
        "username": user.get("username"),
        "tokens": user.get("tokens", 0),
        "role": "admin" if is_admin else ("host" if is_host else "user"),
        "is_admin": is_admin,
        "is_host": is_host
    }

@app.get("/api/hosts")
def get_hosts(initData: str = Header(...)):
    validate_telegram_init_data(initData)
    all_hosts = list(hosts_col.find({"status": "approved"}, {"_id": 0}))
    indian_hosts = [h for h in all_hosts if h.get("country", "").lower() == "india"]
    other_hosts = [h for h in all_hosts if h.get("country", "").lower() != "india"]
    return {"status": "success", "hosts": indian_hosts + other_hosts}

@app.post("/api/booking/create")
def create_booking(payload: BookingCreate, initData: str = Header(...)):
    tg_user = validate_telegram_init_data(initData)
    user_id = str(tg_user.get("id"))
    
    user = users_col.find_one({"user_id": user_id})
    if not user or user.get("blocked") or user.get("banned"):
        raise HTTPException(status_code=403, detail="User blocked or not found")
        
    host = hosts_col.find_one({"user_id": payload.host_id, "status": "approved"})
    if not host or host.get("blocked") or host.get("banned"):
        raise HTTPException(status_code=404, detail="Host not available")
        
    if host.get("country", "").lower() != "india" and user.get("country", "India").lower() == "india":
        raise HTTPException(status_code=400, detail="⚠️ यह Host book नहीं किया जा सकता। यह दूसरे country का Host है। अभी केवल India-based Hosts available हैं।")
        
    settings = settings_col.find_one({}) or {}
    call_rates = settings.get("call_rates", {"1": 20, "5": 100, "10": 200, "15": 300, "20": 400, "25": 500, "30": 600})
    duration_str = str(payload.duration_minutes)
    token_cost = call_rates.get(duration_str, payload.duration_minutes * 20)
    
    if user.get("tokens", 0) < token_cost:
        raise HTTPException(status_code=400, detail="Insufficient tokens. Please recharge.")
        
    users_col.update_one({"user_id": user_id}, {"$inc": {"tokens": -token_cost}})
    
    booking_id = f"BK_{int(time.time())}_{user_id[-4:]}"
    booking = {
        "booking_id": booking_id,
        "user_id": user_id,
        "user_name": user.get("name"),
        "host_id": payload.host_id,
        "host_name": host.get("name"),
        "date": payload.date,
        "time": payload.time,
        "duration_minutes": payload.duration_minutes,
        "token_cost": token_cost,
        "status": "pending",
        "user_joined": False,
        "host_joined": False,
        "call_started_at": None,
        "call_ended_at": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    bookings_col.insert_one(booking)
    
    send_telegram_message(GROUP_3_ID, f"📅 <b>NEW BOOKING</b>\nUser: {user.get('name')}\nHost: {host.get('name')}\nDuration: {payload.duration_minutes}m\nTokens: {token_cost}")
    send_telegram_message(GROUP_1_ID, f"🔔 <b>New Booking Request</b>\nBooking ID: {booking_id}")
    
    return {"status": "success", "booking_id": booking_id, "message": "Booking request sent to host."}

@app.post("/api/call/join")
def join_call(payload: dict, initData: str = Header(...)):
    tg_user = validate_telegram_init_data(initData)
    user_id = str(tg_user.get("id"))
    booking_id = payload.get("booking_id")
    
    booking = bookings_col.find_one({"booking_id": booking_id})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
        
    is_user = booking["user_id"] == user_id
    is_host = booking["host_id"] == user_id
    is_admin = user_id in ADMIN_IDS
    
    if not (is_user or is_host or is_admin):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    update_field = {}
    now_str = datetime.now(timezone.utc).isoformat()
    
    if is_user:
        update_field["user_joined"] = True
        update_field["user_joined_at"] = now_str
    elif is_host:
        update_field["host_joined"] = True
        update_field["host_joined_at"] = now_str
        
    bookings_col.update_one({"booking_id": booking_id}, {"$set": update_field})
    
    updated_booking = bookings_col.find_one({"booking_id": booking_id})
    timer_started = False
    if updated_booking.get("user_joined") and updated_booking.get("host_joined") and not updated_booking.get("call_started_at"):
        bookings_col.update_one({"booking_id": booking_id}, {"$set": {"call_started_at": now_str, "status": "active"}})
        timer_started = True
        send_telegram_message(GROUP_3_ID, f"📹 <b>CALL STARTED</b>\nBooking: {booking_id}")
        
    return {
        "status": "success",
        "agora_app_id": AGORA_APP_ID,
        "channel_name": booking_id,
        "timer_started": timer_started,
        "duration_minutes": booking["duration_minutes"]
    }

@app.post("/api/call/end")
def end_call(payload: dict, initData: str = Header(...)):
    validate_telegram_init_data(initData)
    booking_id = payload.get("booking_id")
    
    booking = bookings_col.find_one({"booking_id": booking_id})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
        
    now_str = datetime.now(timezone.utc).isoformat()
    if booking.get("status") == "completed":
        return {"status": "already_ended"}
        
    bookings_col.update_one({"booking_id": booking_id}, {"$set": {"status": "completed", "call_ended_at": now_str}})
    
    token_cost = booking["token_cost"]
    host_earning = token_cost * HOST_SHARE
    platform_earning = token_cost * (1 - HOST_SHARE)
    
    hosts_col.update_one({"user_id": booking["host_id"]}, {"$inc": {"earnings": host_earning, "available_balance": host_earning}})
    
    calls_col.insert_one({
        "booking_id": booking_id,
        "user_id": booking["user_id"],
        "host_id": booking["host_id"],
        "duration_booked": booking["duration_minutes"],
        "tokens_used": token_cost,
        "host_earning": host_earning,
        "platform_earning": platform_earning,
        "call_start": booking.get("call_started_at"),
        "call_end": now_str,
        "status": "completed"
    })
    
    send_telegram_message(GROUP_3_ID, f"🏁 <b>CALL COMPLETED</b>\nBooking: {booking_id}\nTokens: {token_cost}")
    return {"status": "success", "message": "Call ended successfully."}

@app.post("/api/wallet/recharge")
def submit_recharge(amount: float = Form(...), utr: str = Form(...), screenshot: UploadFile = File(...), initData: str = Form(...)):
    tg_user = validate_telegram_init_data(initData)
    user_id = str(tg_user.get("id"))
    
    file_bytes = screenshot.file.read()
    recharge_id = f"REC_{int(time.time())}_{user_id[-4:]}"
    
    recharges_col.insert_one({
        "recharge_id": recharge_id,
        "user_id": user_id,
        "amount": amount,
        "utr": utr,
        "screenshot_bytes": file_bytes,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    
    send_telegram_photo(GROUP_1_ID, file_bytes, f"💳 <b>New Recharge Request</b>\nID: {recharge_id}\nAmount: ₹{amount}\nUTR: {utr}")
    send_telegram_message(GROUP_3_ID, f"💳 <b>Recharge Submitted</b> | User: {user_id} | ₹{amount}")
    
    return {"status": "success", "message": "Recharge request submitted successfully."}

@app.get("/api/user/data")
def get_user_data(initData: str = Header(...)):
    tg_user = validate_telegram_init_data(initData)
    user_id = str(tg_user.get("id"))
    user = users_col.find_one({"user_id": user_id}, {"_id": 0, "screenshot_bytes": 0})
    bookings = list(bookings_col.find({"user_id": user_id}, {"_id": 0}))
    return {"status": "success", "user": user, "bookings": bookings}

@app.get("/api/admin/stats")
def get_admin_stats(initData: str = Header(...)):
    tg_user = validate_telegram_init_data(initData)
    if str(tg_user.get("id")) not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
        
    return {
        "status": "success",
        "stats": {
            "total_users": users_col.count_documents({}),
            "total_hosts": hosts_col.count_documents({}),
            "online_hosts": hosts_col.count_documents({"online": True}),
            "pending_recharges": recharges_col.count_documents({"status": "pending"}),
            "total_calls": calls_col.count_documents({})
        }
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
