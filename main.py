import os
import time
import hmac
import hashlib
import urllib.parse
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, Request, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorClient
import requests

# ==========================================
# CONFIGURATION & ENVIRONMENT
# ==========================================
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "vynora_live")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")

AGORA_APP_ID = os.getenv("AGORA_APP_ID", "YOUR_AGORA_APP_ID")
AGORA_APP_CERTIFICATE = os.getenv("AGORA_APP_CERTIFICATE", "YOUR_AGORA_APP_CERTIFICATE")

SUPER_ADMIN_IDS = [7778606261, 7001825467]

GROUP_1_ID = os.getenv("GROUP_1_ID", "-1000000000001")  # Admin/Requests
GROUP_2_ID = os.getenv("GROUP_2_ID", "-1000000000002")  # User Registration
GROUP_3_ID = os.getenv("GROUP_3_ID", "-1000000000003")  # Team/Operations

app = FastAPI(title="Vynora Live API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]

# ==========================================
# PRICING & CONSTANTS
# ==========================================
BOOKING_PRICES = {
    1: 20,
    5: 100,
    10: 200,
    15: 300,
    20: 400,
    25: 500,
    30: 600
}

RECHARGE_PLANS = [50, 100, 200, 500, 1000, 1500, 2000]

# ==========================================
# TELEGRAM NOTIFICATION HELPER
# ==========================================
def send_telegram_message(chat_id: str, text: str):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN":
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"Telegram Notification Error: {e}")

# ==========================================
# AUTHENTICATION & SECURITY
# ==========================================
def verify_telegram_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Unauthorized: Missing initData")
    try:
        parsed = urllib.parse.parse_qsl(init_data)
        data_dict = dict(parsed)
        if "hash" not in data_dict:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid initData format")
        
        received_hash = data_dict.pop("hash")
        sorted_keys = sorted(data_dict.keys())
        data_check_string = "\n".join([f"{k}={v}" for k in sorted_keys])
        
        secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        import json
        user_info = json.loads(data_dict.get("user", "{}"))
        return user_info
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")

async def get_current_user(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data")
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram Auth Header")
    user_data = verify_telegram_init_data(init_data)
    telegram_id = user_data.get("id")
    if not telegram_id:
        raise HTTPException(status_code=401, detail="Invalid user ID in auth data")
    
    user = await db.users.find_one({"telegram_id": telegram_id})
    if not user:
        user = {
            "telegram_id": telegram_id,
            "name": f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip(),
            "username": user_data.get("username", ""),
            "country": "India",
            "tokens": 50,
            "role": "user",
            "created_at": datetime.now(timezone.utc)
        }
        await db.users.insert_one(user)
        send_telegram_message(GROUP_2_ID, f"👤 <b>New User Registration</b>\nName: {user['name']}\nUsername: @{user['username']}\nID: {telegram_id}\nCountry: India")
    return user

async def get_current_admin(request: Request):
    user = await get_current_user(request)
    if user["telegram_id"] not in SUPER_ADMIN_IDS and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Access denied. Admins only.")
    return user

# ==========================================
# PYDANTIC MODELS
# ==========================================
class BookingRequest(BaseModel):
    host_id: str
    duration_minutes: int
    scheduled_time: Optional[str] = None

class RechargeRequest(BaseModel):
    amount: int
    utr: str
    screenshot_file_id: Optional[str] = None

class GiftRequest(BaseModel):
    gift_name: str
    tokens: int
    host_id: str

class HostStatusUpdate(BaseModel):
    is_online: bool

# ==========================================
# USER & HOST ROUTES
# ==========================================
@app.get("/api/me")
async def get_me(user: dict = Depends(get_current_user)):
    user["_id"] = str(user["_id"])
    user["is_admin"] = user["telegram_id"] in SUPER_ADMIN_IDS
    return user

@app.get("/api/hosts")
async def get_hosts(user: dict = Depends(get_current_user)):
    hosts_cursor = db.hosts.find({"status": "approved"})
    hosts = []
    async for host in hosts_cursor:
        host["_id"] = str(host["_id"])
        hosts.append(host)
    hosts.sort(key=lambda x: 0 if x.get("country", "India").lower() == "india" else 1)
    return hosts

@app.post("/api/hosts/status")
async def update_host_status(payload: HostStatusUpdate, user: dict = Depends(get_current_user)):
    host = await db.hosts.find_one({"telegram_id": user["telegram_id"]})
    if not host:
        raise HTTPException(status_code=404, detail="Host profile not found")
    
    await db.hosts.update_one(
        {"telegram_id": user["telegram_id"]},
        {"$set": {"is_online": payload.is_online}}
    )
    return {"status": "success", "is_online": payload.is_online}

@app.post("/api/bookings")
async def create_booking(payload: BookingRequest, user: dict = Depends(get_current_user)):
    if payload.duration_minutes not in BOOKING_PRICES:
        raise HTTPException(status_code=400, detail="Invalid call duration")
    
    from bson import ObjectId
    try:
        host = await db.hosts.find_one({"_id": ObjectId(payload.host_id)})
    except Exception:
        host = await db.hosts.find_one({"_id": payload.host_id})
        
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
        
    if not host.get("is_online", False):
        raise HTTPException(status_code=400, detail="Host is currently offline. Cannot book immediate call.")
        
    user_country = user.get("country", "India").lower()
    host_country = host.get("country", "India").lower()
    if user_country == "india" and host_country != "india":
        raise HTTPException(status_code=400, detail="यह Host book नहीं किया जा सकता। यह दूसरे country का Host है। अभी केवल India-based Hosts available हैं।")

    cost = BOOKING_PRICES[payload.duration_minutes]
    if user.get("tokens", 0) < cost:
        raise HTTPException(status_code=400, detail="Insufficient token balance. Please recharge.")

    res = await db.users.update_one(
        {"_id": user["_id"], "tokens": {"$gte": cost}},
        {"$inc": {"tokens": -cost}}
    )
    if res.modified_count == 0:
        raise HTTPException(status_code=400, detail="Token deduction failed.")

    booking = {
        "user_id": str(user["_id"]),
        "user_name": user["name"],
        "user_telegram_id": user["telegram_id"],
        "host_id": str(host["_id"]),
        "host_name": host["name"],
        "host_telegram_id": host["telegram_id"],
        "duration_minutes": payload.duration_minutes,
        "token_cost": cost,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
        "user_joined_at": None,
        "host_joined_at": None,
        "call_started_at": None,
        "call_ended_at": None,
        "actual_duration_seconds": 0
    }
    
    result = await db.bookings.insert_one(booking)
    booking_id = str(result.inserted_id)

    send_telegram_message(
        str(host["telegram_id"]),
        f"📹 <b>New Booking Request!</b>\nUser: {user['name']}\nDuration: {payload.duration_minutes} Mins\nCost: {cost} Tokens\nBooking ID: {booking_id}"
    )
    send_telegram_message(
        GROUP_3_ID,
        f"📋 <b>Booking Created</b>\nUser: {user['name']}\nHost: {host['name']}\nDuration: {payload.duration_minutes}m\nCost: {cost} Tokens\nID: {booking_id}"
    )

    return {"status": "success", "booking_id": booking_id, "message": "Booking created successfully"}

@app.get("/api/bookings")
async def get_bookings(user: dict = Depends(get_current_user)):
    query = {}
    if user["telegram_id"] in SUPER_ADMIN_IDS:
        pass
    elif user.get("role") == "host":
        host = await db.hosts.find_one({"telegram_id": user["telegram_id"]})
        if host:
            query = {"host_id": str(host["_id"])}
        else:
            query = {"user_id": str(user["_id"])}
    else:
        query = {"user_id": str(user["_id"])}

    cursor = db.bookings.find(query).sort("created_at", -1).limit(50)
    bookings = []
    async for b in cursor:
        b["_id"] = str(b["_id"])
        bookings.append(b)
    return bookings

@app.post("/api/bookings/{booking_id}/action")
async def booking_action(booking_id: str, action: str, user: dict = Depends(get_current_user)):
    from bson import ObjectId
    booking = await db.bookings.find_one({"_id": ObjectId(booking_id)})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    host = await db.hosts.find_one({"_id": ObjectId(booking["host_id"])})
    if not host or host["telegram_id"] != user["telegram_id"]:
        if user["telegram_id"] not in SUPER_ADMIN_IDS:
            raise HTTPException(status_code=403, detail="Unauthorized")

    if action == "accept":
        await db.bookings.update_one({"_id": ObjectId(booking_id)}, {"$set": {"status": "accepted"}})
        send_telegram_message(booking["user_telegram_id"], f"✅ Aapki booking {booking['host_name']} ke sath accept ho gayi hai!")
        return {"status": "success", "message": "Booking accepted"}
    elif action == "reject":
        cost = booking["token_cost"]
        await db.users.update_one({"_id": ObjectId(booking["user_id"])}, {"$inc": {"tokens": cost}})
        await db.bookings.update_one({"_id": ObjectId(booking_id)}, {"$set": {"status": "rejected"}})
        send_telegram_message(booking["user_telegram_id"], f"❌ Aapki booking reject ho gayi hai. {cost} tokens refund kar diye gaye hain.")
        return {"status": "success", "message": "Booking rejected and refunded"}
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

@app.post("/api/call/join")
async def join_call(booking_id: str, user: dict = Depends(get_current_user)):
    from bson import ObjectId
    booking = await db.bookings.find_one({"_id": ObjectId(booking_id)})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    is_host = user["telegram_id"] == booking.get("host_telegram_id")
    is_user = str(user["_id"]) == booking.get("user_id")

    if not is_host and not is_user and user["telegram_id"] not in SUPER_ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Unauthorized for this call")

    now = datetime.now(timezone.utc)
    update_data = {}
    if is_host and not booking.get("host_joined_at"):
        update_data["host_joined_at"] = now
    elif is_user and not booking.get("user_joined_at"):
        update_data["user_joined_at"] = now

    updated_booking = await db.bookings.find_one_and_update(
        {"_id": ObjectId(booking_id)},
        {"$set": update_data},
        return_document=True
    )

    if updated_booking.get("user_joined_at") and updated_booking.get("host_joined_at") and not updated_booking.get("call_started_at"):
        await db.bookings.update_one(
            {"_id": ObjectId(booking_id)},
            {"$set": {"call_started_at": now, "status": "ongoing"}}
        )

    channel_name = f"vynora_{booking_id}"
    uid = user["telegram_id"] % 1000000

    return {
        "status": "success",
        "channel_name": channel_name,
        "token": None,
        "uid": uid,
        "app_id": AGORA_APP_ID
    }

@app.post("/api/call/end")
async def end_call(booking_id: str, user: dict = Depends(get_current_user)):
    from bson import ObjectId
    booking = await db.bookings.find_one({"_id": ObjectId(booking_id)})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.get("status") == "completed":
        return {"status": "success", "message": "Call already finalized"}

    now = datetime.now(timezone.utc)
    call_started = booking.get("call_started_at")
    
    actual_seconds = 0
    if call_started:
        if call_started.tzinfo is None:
            call_started = call_started.replace(tzinfo=timezone.utc)
        actual_seconds = int((now - call_started).total_seconds())

    booked_minutes = booking["duration_minutes"]
    booked_seconds = booked_minutes * 60

    total_paid_tokens = booking["token_cost"]
    tokens_to_charge = total_paid_tokens
    refund_tokens = 0

    if actual_seconds < booked_seconds and actual_seconds > 0:
        per_second_cost = total_paid_tokens / booked_seconds
        tokens_to_charge = int(actual_seconds * per_second_cost)
        if tokens_to_charge < 1:
            tokens_to_charge = 0
        refund_tokens = total_paid_tokens - tokens_to_charge

    host_earning = int(tokens_to_charge * 0.60)
    platform_earning = tokens_to_charge - host_earning

    await db.bookings.update_one(
        {"_id": ObjectId(booking_id), "status": {"$ne": "completed"}},
        {
            "$set": {
                "status": "completed",
                "call_ended_at": now,
                "actual_duration_seconds": actual_seconds,
                "tokens_charged": tokens_to_charge,
                "tokens_refunded": refund_tokens,
                "host_earning": host_earning,
                "platform_earning": platform_earning
            }
        }
    )

    if refund_tokens > 0:
        await db.users.update_one({"_id": ObjectId(booking["user_id"])}, {"$inc": {"tokens": refund_tokens}})

    host = await db.hosts.find_one({"_id": ObjectId(booking["host_id"])})
    if host:
        await db.hosts.update_one(
            {"_id": host["_id"]},
            {"$inc": {"tokens_earned": host_earning, "available_balance": host_earning}}
        )

    send_telegram_message(
        GROUP_3_ID,
        f"📴 <b>Call Completed</b>\nBooking ID: {booking_id}\nDuration: {actual_seconds}s / {booked_minutes}m\nTokens Charged: {tokens_to_charge}\nHost Earning: {host_earning} (60%)"
    )

    return {
        "status": "success",
        "actual_duration_seconds": actual_seconds,
        "tokens_charged": tokens_to_charge,
        "refund_tokens": refund_tokens
    }

@app.post("/api/recharge")
async def submit_recharge(payload: RechargeRequest, user: dict = Depends(get_current_user)):
    if payload.amount not in RECHARGE_PLANS:
        raise HTTPException(status_code=400, detail="Invalid recharge plan amount")

    recharge_doc = {
        "user_id": str(user["_id"]),
        "user_name": user["name"],
        "user_telegram_id": user["telegram_id"],
        "amount": payload.amount,
        "utr": payload.utr,
        "screenshot": payload.screenshot_file_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc)
    }
    result = await db.recharges.insert_one(recharge_doc)
    recharge_id = str(result.inserted_id)

    send_telegram_message(
        GROUP_1_ID,
        f"💳 <b>New Recharge Request</b>\nUser: {user['name']} (ID: {user['telegram_id']})\nAmount: ₹{payload.amount}\nUTR: {payload.utr}\nID: {recharge_id}"
    )

    return {"status": "success", "recharge_id": recharge_id, "message": "Recharge submitted successfully"}

@app.post("/api/gifts")
async def send_gift(payload: GiftRequest, user: dict = Depends(get_current_user)):
    if user.get("tokens", 0) < payload.tokens:
        raise HTTPException(status_code=400, detail="Insufficient tokens for gift")

    from bson import ObjectId
    host = await db.hosts.find_one({"_id": ObjectId(payload.host_id)})
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")

    await db.users.update_one({"_id": user["_id"]}, {"$inc": {"tokens": -payload.tokens}})
    
    gift_earning = int(payload.tokens * 0.60)
    await db.hosts.update_one({"_id": host["_id"]}, {"$inc": {"tokens_earned": gift_earning, "available_balance": gift_earning}})

    gift_txn = {
        "user_id": str(user["_id"]),
        "host_id": str(host["_id"]),
        "gift_name": payload.gift_name,
        "tokens": payload.tokens,
        "created_at": datetime.now(timezone.utc)
    }
    await db.transactions.insert_one(gift_txn)

    send_telegram_message(GROUP_3_ID, f"🎁 <b>Gift Sent</b>\nUser {user['name']} sent {payload.gift_name} ({payload.tokens} tokens) to Host {host['name']}")

    return {"status": "success", "message": "Gift sent successfully"}

@app.get("/api/transactions")
async def get_transactions(user: dict = Depends(get_current_user)):
    cursor = db.transactions.find({"user_id": str(user["_id"])}).sort("created_at", -1).limit(30)
    txns = []
    async for t in cursor:
        t["_id"] = str(t["_id"])
        txns.append(t)
    return txns

@app.post("/api/admin/recharge/{recharge_id}/approve")
async def approve_recharge(recharge_id: str, admin: dict = Depends(get_current_admin)):
    from bson import ObjectId
    recharge = await db.recharges.find_one({"_id": ObjectId(recharge_id), "status": "pending"})
    if not recharge:
        raise HTTPException(status_code=404, detail="Pending recharge not found or already processed")

    amount = recharge["amount"]
    await db.recharges.update_one({"_id": ObjectId(recharge_id)}, {"$set": {"status": "approved"}})
    await db.users.update_one({"_id": ObjectId(recharge["user_id"])}, {"$inc": {"tokens": amount}})

    send_telegram_message(recharge["user_telegram_id"], f"🎉 Aapka recharge ₹{amount} ({amount} Tokens) approve ho gaya hai!")
    return {"status": "success", "message": f"Approved {amount} tokens"}

@app.get("/api/admin/stats")
async def admin_stats(admin: dict = Depends(get_current_admin)):
    total_users = await db.users.count_documents({})
    total_hosts = await db.hosts.count_documents({})
    total_bookings = await db.bookings.count_documents({})
    return {
        "total_users": total_users,
        "total_hosts": total_hosts,
        "total_bookings": total_bookings
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
