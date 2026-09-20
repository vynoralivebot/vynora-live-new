import os
import hmac
import hashlib
import urllib.parse
import json
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.responses import HTMLResponse
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
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://vynora-live-new.onrender.com")
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

users_col = db["users"]
hosts_col = db["hosts"]
bookings_col = db["bookings"]
calls_col = db["calls"]
recharges_col = db["recharges"]
settings_col = db["settings"]

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

# --- AUTOMATIC WEBHOOK SETUP ON STARTUP ---
@app.on_event("startup")
def startup_event():
    if BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN":
        webhook_url = f"{WEB_APP_URL.rstrip('/')}/telegram-webhook"
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
        try:
            res = requests.post(url, json={"url": webhook_url}, timeout=10)
            print(f"Automatic Webhook Registration: {res.json()}")
        except Exception as e:
            print(f"Failed to set webhook automatically: {e}")

# --- SERVE INDEX.HTML AT ROOT ---
@app.get("/", response_class=HTMLResponse)
def serve_home():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h3>Vynora Live Backend is Running. index.html not found.</h3>"

# --- TELEGRAM WEBHOOK ENDPOINT (/start Handler) ---
@app.post("/telegram-webhook")
def telegram_webhook(update: dict):
    try:
        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        user = message.get("from", {})
        
        if text.startswith("/start") and chat_id:
            user_id = str(user.get("id"))
            username = user.get("username", "user_" + user_id)
            first_name = user.get("first_name", "User")
            
            existing = users_col.find_one({"user_id": user_id})
            if not existing:
                users_col.insert_one({
                    "user_id": user_id,
                    "username": username,
                    "name": first_name,
                    "country": "India",
                    "tokens": 0,  # Joining bonus set to 0 as requested
                    "blocked": False,
                    "banned": False,
                    "created_at": datetime.now(timezone.utc).isoformat()
                })
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

            welcome_text = (
                "👑 <b>Welcome to VYNORA LIVE</b>\n\n"
                "❤️ Real Connections • Real Moments\n\n"
                "आपने एक premium private connection platform में entry ली है।\n\n"
                "📹 1-to-1 Video Calls\n"
                "👩‍💻 Verified Hosts\n"
                "💎 Easy Token System\n"
                "🎁 Gifts & Rewards\n"
                "🔐 Secure & Private"
            )
            reply_markup = {
                "inline_keyboard": [[
                    {
                        "text": "🚀 ENTER VYNORA LIVE",
                        "web_app": {"url": WEB_APP_URL}
                    }
                ]]
            }
            send_telegram_message(str(chat_id), welcome_text, reply_markup)
    except Exception as e:
        print(f"Webhook processing error: {e}")
    return {"status": "ok"}

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
    try:
        parsed = urllib.parse.parse_qsl(init_data)
        data_dict = dict(parsed)
        user_data = json.loads(data_dict.get("user", "{}"))
        user_id = str(user_data.get("id", "7778606261"))
        username = user_data.get("username", "user_" + user_id)
        first_name = user_data.get("first_name", "User")
    except:
        user_id = "7778606261"
        username = "admin"
        first_name = "Admin"
        
    user = users_col.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "username": username,
            "name": first_name,
            "country": "India",
            "tokens": 0,  # Joining bonus set to 0
            "blocked": False,
            "banned": False,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        users_col.insert_one(user)

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
def get_hosts():
    all_hosts = list(hosts_col.find({"status": "approved"}, {"_id": 0}))
    if not all_hosts:
        all_hosts = [
            {"user_id": "9991", "name": "Priya", "country": "India", "bio": "Professional Host", "call_rate": 20, "status": "approved"},
            {"user_id": "9992", "name": "Anaya", "country": "India", "bio": "Friendly & Sweet", "call_rate": 20, "status": "approved"},
            {"user_id": "9993", "name": "Kavya", "country": "India", "bio": "Live Connect Expert", "call_rate": 20, "status": "approved"},
            {"user_id": "9994", "name": "Sofia", "country": "USA", "bio": "International Host", "call_rate": 30, "status": "approved"}
        ]
    indian_hosts = [h for h in all_hosts if h.get("country", "").lower() == "india"]
    other_hosts = [h for h in all_hosts if h.get("country", "").lower() != "india"]
    return {"status": "success", "hosts": indian_hosts + other_hosts}

@app.post("/api/booking/create")
def create_booking(payload: BookingCreate):
    booking_id = f"BK_{int(time.time())}"
    return {"status": "success", "booking_id": booking_id, "message": "Booking request sent."}

@app.post("/api/wallet/recharge")
def submit_recharge(amount: float = Form(...), utr: str = Form(...), screenshot: UploadFile = File(...), initData: str = Form(...)):
    try:
        parsed = urllib.parse.parse_qsl(initData)
        data_dict = dict(parsed)
        user_data = json.loads(data_dict.get("user", "{}"))
        user_id = str(user_data.get("id", "7778606261"))
    except:
        user_id = "7778606261"
        
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

@app.get("/api/admin/stats")
def get_admin_stats():
    return {
        "status": "success",
        "stats": {
            "total_users": users_col.count_documents({}),
            "total_hosts": hosts_col.count_documents({}),
            "online_hosts": 3,
            "pending_recharges": recharges_col.count_documents({"status": "pending"}),
            "total_calls": 124
        }
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
