import os, time, uuid, threading, logging, base64, mimetypes, hmac, hashlib, json, urllib.parse, contextvars
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
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
    {"minutes": 5, "tokens": 100},
    {"minutes": 10, "tokens": 200},
    {"minutes": 15, "tokens": 300},
    {"minutes": 20, "tokens": 400},
    {"minutes": 25, "tokens": 500},
    {"minutes": 30, "tokens": 600},
]


def get_booking_plans():
    if db is None:
        return BOOKING_PLANS
    try:
        row = db["settings"].find_one({"key": "booking_plans"})
        plans = row.get("value") if row else None
        if isinstance(plans, list) and plans:
            return plans
    except Exception:
        pass
    return BOOKING_PLANS


GIFT_PLANS = [
    {"id": "rose", "name": "Rose", "emoji": "🌹", "tokens": 10},
    {"id": "heart", "name": "Heart", "emoji": "❤️", "tokens": 20},
    {"id": "coffee", "name": "Coffee", "emoji": "☕", "tokens": 50},
    {"id": "diamond", "name": "Diamond", "emoji": "💎", "tokens": 100},
    {"id": "crown", "name": "Crown", "emoji": "👑", "tokens": 250},
    {"id": "rocket", "name": "Rocket", "emoji": "🚀", "tokens": 500},
]

RECHARGE_PLANS = [
    {"rupees": 50, "tokens": 50},
    {"rupees": 100, "tokens": 100},
    {"rupees": 200, "tokens": 200},
    {"rupees": 500, "tokens": 500},
    {"rupees": 1000, "tokens": 1000},
    {"rupees": 1500, "tokens": 1500},
    {"rupees": 2000, "tokens": 2000},
]

DUMMY_HOSTS = [
    {
        "user_id": 910000001,
        "name": "Sofia",
        "username": "sofia_demo",
        "country": "US",
        "country_name": "USA",
        "photo_url": "https://randomuser.me/api/portraits/women/44.jpg",
        "bio": "Demo profile",
        "status": "approved",
        "online": True,
    },
    {
        "user_id": 910000002,
        "name": "Emma",
        "username": "emma_demo",
        "country": "GB",
        "country_name": "United Kingdom",
        "photo_url": "https://randomuser.me/api/portraits/women/68.jpg",
        "bio": "Demo profile",
        "status": "approved",
        "online": True,
    },
    {
        "user_id": 910000003,
        "name": "Olivia",
        "username": "olivia_demo",
        "country": "PH",
        "country_name": "Philippines",
        "photo_url": "https://randomuser.me/api/portraits/women/65.jpg",
        "bio": "Demo profile",
        "status": "approved",
        "online": False,
    },
]

app = FastAPI(title="Vynora Live 1v1", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_verified_web_user = contextvars.ContextVar(
    "verified_web_user",
    default=None,
)


def verify_telegram_init_data(init_data: str):
    if not BOT_TOKEN or not init_data:
        return None

    try:
        pairs = dict(
            urllib.parse.parse_qsl(
                init_data,
                keep_blank_values=True,
            )
        )

        received = pairs.pop("hash", "")

        if not received:
            return None

        data_check = "\n".join(
            f"{k}={pairs[k]}"
            for k in sorted(pairs)
        )

        secret = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        expected = hmac.new(
            secret,
            data_check.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(
            expected,
            received,
        ):
            return None

        auth_date = int(
            pairs.get("auth_date", "0") or 0
        )

        if auth_date and now_ts() - auth_date > 86400:
            return None

        user = json.loads(
            pairs.get("user", "{}")
        )

        return (
            int(user.get("id"))
            if user.get("id")
            else None
        )

    except Exception:
        return None


@app.middleware("http")
async def telegram_webapp_auth(
    request: Request,
    call_next,
):
    path = request.url.path

    if (
        path.startswith("/api/")
        and path
        not in (
            "/api/config",
            "/api/health",
            "/api/telegram/webhook",
            "/api/telegram/webhook-info",
        )
    ):
        verified = verify_telegram_init_data(
            request.headers.get(
                "X-Telegram-Init-Data",
                "",
            )
        )

        if verified is None:
            return Response(
                content=json.dumps(
                    {
                        "detail":
                        "Valid Telegram Mini App session is required"
                    }
                ),
                status_code=401,
                media_type="application/json",
            )

        _verified_web_user.set(verified)

    response = await call_next(request)
    return response


if MONGO_URI:
    mongo = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=8000,
    )
    db = mongo[MONGO_DB]
else:
    mongo = None
    db = None


def col(name):
    if db is None:
        raise HTTPException(
            503,
            "MongoDB is not configured. Set MONGO_URI.",
        )

    return db[name]


def now_ts():
    return int(time.time())


def esc_html(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def iso(ts):
    return datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc,
    ).isoformat()


def uid(v):
    try:
        return int(v)
    except Exception:
        raise HTTPException(
            400,
            "Invalid user id",
        )


def user_doc(user_id):
    return col("users").find_one(
        {
            "user_id": uid(user_id)
        }
    )


def ensure_user(
    user_id,
    name="",
    username="",
    country="IN",
):
    user_id = uid(user_id)

    existing = col("users").find_one(
        {
            "user_id": user_id
        }
    )

    if not existing:
        d = {
            "user_id": user_id,
            "name": name or "User",
            "username": username or "",
            "tokens": 0,
            "blocked": False,
            "country": (
                country or "IN"
            ).upper()[:2],
            "created_at": now_ts(),
            "updated_at": now_ts(),
        }

        col("users").insert_one(d)

        return d, True

    updates = {
        "updated_at": now_ts()
    }

    if name:
        updates["name"] = name

    if username is not None:
        updates["username"] = username

    col("users").update_one(
        {
            "user_id": user_id
        },
        {
            "$set": updates
        },
    )

    return (
        col("users").find_one(
            {
                "user_id": user_id
            }
        ),
        False,
    )


def is_admin(user_id):
    target = uid(user_id)

    verified = _verified_web_user.get()

    if (
        verified is not None
        and verified != target
    ):
        return False

    return target in ADMIN_IDS


def blocked(user_id):
    d = user_doc(user_id) or {}
    return bool(d.get("blocked"))


def host_doc(host_id):
    return col("hosts").find_one(
        {
            "user_id": uid(host_id)
        }
    )


def host_or_404(host_id):
    h = host_doc(host_id)

    if not h or h.get("status") != "approved":
        raise HTTPException(
            404,
            "Host not available",
        )

    if blocked(host_id):
        raise HTTPException(
            403,
            "Host is blocked",
        )

    return h


def tg_send_photo(
    chat_id,
    photo_bytes,
    filename="payment.jpg",
    caption="",
    buttons=None,
):
    if (
        not BOT_TOKEN
        or not chat_id
        or not photo_bytes
    ):
        return None

    try:
        data = {
            "chat_id": str(chat_id),
            "caption": caption[:1024],
            "parse_mode": "HTML",
        }

        if buttons:
            data["reply_markup"] = (
                __import__("json").dumps(
                    {
                        "inline_keyboard":
                        buttons
                    }
                )
            )

        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data=data,
            files={
                "photo": (
                    filename,
                    photo_bytes,
                    "image/jpeg",
                )
            },
            timeout=25,
        )

        if not r.ok:
            log.error(
                "Telegram sendPhoto failed chat=%s status=%s body=%s",
                chat_id,
                r.status_code,
                r.text[:500],
            )
            return None

        body = r.json()

        return body.get("result") or None

    except Exception as e:
        log.exception(
            "Telegram sendPhoto exception chat=%s: %s",
            chat_id,
            e,
        )

        return None


def tg_delete_message(
    chat_id,
    message_id,
):
    if (
        not BOT_TOKEN
        or not chat_id
        or not message_id
    ):
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
            json={
                "chat_id": str(chat_id),
                "message_id": int(message_id),
            },
            timeout=15,
        )

        if not r.ok:
            log.warning(
                "Telegram deleteMessage failed chat=%s message=%s status=%s body=%s",
                chat_id,
                message_id,
                r.status_code,
                r.text[:500],
            )

        return r.ok

    except Exception as e:
        log.warning(
            "Telegram deleteMessage exception chat=%s message=%s: %s",
            chat_id,
            message_id,
            e,
        )

        return False


def remove_recharge_screenshot(
    recharge_id,
    delete_group_message=True,
):
    r = col("recharges").find_one(
        {
            "recharge_id": recharge_id
        }
    )

    if not r:
        return False

    col("recharges").update_one(
        {
            "recharge_id": recharge_id
        },
        {
            "$unset": {
                "screenshot_data": "",
                "screenshot_filename": "",
                "screenshot_content_type": "",
                "screenshot_url": "",
            },
            "$set": {
                "screenshot_deleted_at":
                now_ts()
            },
        },
    )

    if delete_group_message:
        for mid in (
            r.get(
                "telegram_screenshot_message_id"
            ),
            r.get(
                "telegram_action_message_id"
            ),
        ):
            if mid:
                tg_delete_message(
                    GROUP_1_ID,
                    mid,
                )

    return True


def notify_group(
    chat_id,
    text,
    buttons=None,
):
    if not BOT_TOKEN:
        log.error(
            "Telegram group notify skipped: BOT_TOKEN is missing"
        )
        return False

    if not chat_id:
        log.error(
            "Telegram group notify skipped: group chat ID is missing"
        )
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": buttons
        }

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=15,
        )

        if not r.ok:
            log.error(
                "Telegram group notify HTTP %s: %s",
                r.status_code,
                r.text[:500],
            )

        return r.ok

    except Exception as e:
        log.warning(
            "telegram group notify failed: %s",
            e,
        )

        return False


def create_notification(
    user_id,
    kind,
    title,
    message,
    data=None,
):
    try:
        col("notifications").insert_one(
            {
                "user_id": uid(user_id),
                "kind": kind,
                "title": title,
                "message": message,
                "data": data or {},
                "read": False,
                "created_at": now_ts(),
            }
        )
    except Exception as e:
        log.warning(
            "notification create failed: %s",
            e,
        )


def notify_user(
    user_id,
    text,
    buttons=None,
):
    create_notification(
        user_id,
        "telegram",
        "Vynora Live",
        text,
        {},
    )

    if not BOT_TOKEN:
        return False

    payload = {
        "chat_id": uid(user_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": buttons
        }

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=15,
        )

        return r.ok

    except Exception as e:
        log.warning(
            "telegram user notify failed: %s",
            e,
        )

        return False


def notify_new_user(u):
    text = (
        "🆕 <b>NEW USER</b>\n\n"
        f"Name: {u.get('name','-')}\n"
        f"User ID: <code>{u['user_id']}</code>\n"
        f"Username: @{u.get('username','').lstrip('@') or '-'}\n"
        f"Joined: {iso(u.get('created_at', now_ts()))}"
    )

    notify_group(
        GROUP_2_ID,
        text,
    )


def notify_request(
    text,
    buttons=None,
):
    notify_group(
        GROUP_1_ID,
        text,
        buttons,
    )


def notify_full(text):
    notify_group(
        GROUP_3_ID,
        text,
    )


def profile_photo(user_id):
    d = user_doc(user_id) or {}
    return d.get(
        "photo_url",
        "",
    )


def public_photo_url(
    user_id,
    doc=None,
):
    d = doc or user_doc(user_id) or {}

    if d.get("photo_data"):
        return (
            f"/api/profile/photo/"
            f"{int(user_id)}"
            f"?v={int(d.get('photo_updated_at', 0))}"
        )

    return d.get(
        "photo_url",
        "",
    )


def require_user(user_id):
    verified = _verified_web_user.get()

    if (
        verified is not None
        and uid(user_id) != verified
    ):
        raise HTTPException(
            403,
            "Telegram user identity mismatch",
        )

    if blocked(user_id):
        raise HTTPException(
            403,
            "Your account is blocked",
        )

    return (
        user_doc(user_id)
        or ensure_user(user_id)[0]
    )


def overlap(
    host_id,
    start_ts,
    end_ts,
):
    return col("bookings").find_one(
        {
            "host_id": uid(host_id),
            "status": {
                "$in": [
                    "accepted",
                    "scheduled",
                    "calling",
                ]
            },
            "start_ts": {
                "$lt": end_ts
            },
            "end_ts": {
                "$gt": start_ts
            },
        }
)
