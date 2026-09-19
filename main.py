import os
import json
import hmac
import hashlib
import sqlite3
import secrets
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, request, jsonify, send_from_directory, g
from flask_socketio import SocketIO, emit, join_room

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vynora_live.db")

app = Flask(__name__, static_folder=".", static_url_path="")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(32))

# Socket.IO is used for WebRTC signaling and server-side call events.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_1_ID = os.getenv("GROUP_1_ID", "").strip()
GROUP_2_ID = os.getenv("GROUP_2_ID", "").strip()
GROUP_3_ID = os.getenv("GROUP_3_ID", "").strip()

# Final Master Requirement — two Super Admins
SUPER_ADMINS = {7778606261, 7001825467}

PRICES = {
    1: 20,
    3: 50,
    6: 100,
    10: 160,
    15: 240,
    20: 320,
    30: 450,
}

RECHARGE_PLANS = {
    50: 50,
    100: 105,
    200: 210,
    300: 320,
    500: 550,
    1000: 1150,
    1500: 1750,
    2000: 2400,
    3000: 3600,
}

GIFTS = {
    "rose": ("🌹 Rose", 10),
    "heart": ("❤️ Heart", 25),
    "coffee": ("☕ Coffee", 50),
    "diamond": ("💎 Diamond", 100),
    "crown": ("👑 Crown", 250),
    "rocket": ("🚀 Rocket", 500),
}


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT DEFAULT '',
            name TEXT DEFAULT '',
            photo_url TEXT DEFAULT '',
            country TEXT DEFAULT 'India',
            wallet INTEGER DEFAULT 0,
            role TEXT DEFAULT 'user',
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            name TEXT NOT NULL,
            photo_url TEXT DEFAULT '',
            country TEXT DEFAULT 'India',
            bio TEXT DEFAULT '',
            rating REAL DEFAULT 5.0,
            price_note TEXT DEFAULT '',
            online INTEGER DEFAULT 0,
            approved INTEGER DEFAULT 0,
            dummy INTEGER DEFAULT 0,
            filter_name TEXT DEFAULT 'Natural',
            balance INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            host_id INTEGER NOT NULL,
            duration INTEGER NOT NULL,
            tokens INTEGER NOT NULL,
            booking_date TEXT NOT NULL,
            booking_time TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            scheduled_at TEXT,
            call_started_at TEXT,
            call_ended_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(host_id) REFERENCES hosts(id)
        );

        CREATE TABLE IF NOT EXISTS gifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            host_id INTEGER NOT NULL,
            gift_key TEXT NOT NULL,
            tokens INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recharge_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            tokens INTEGER NOT NULL,
            utr TEXT NOT NULL,
            screenshot TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            payment_details TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def event_log(event_type, message):
    db().execute(
        "INSERT INTO events(event_type,message,created_at) VALUES(?,?,?)",
        (event_type, message, iso_now()),
    )
    db().commit()


def telegram_send(chat_id, text, reply_markup=None):
    if not BOT_TOKEN or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }

    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        response = requests.post(url, json=payload, timeout=15)
        return response.ok
    except requests.RequestException:
        return False


def set_telegram_webhook():
    """
    Automatically register the correct webhook after every deployment.

    We intentionally use /api/telegram/webhook because your existing
    Telegram bot was already configured to send updates there.
    """
    if not BOT_TOKEN:
        print("WARNING: BOT_TOKEN is not configured. Telegram webhook not set.")
        return

    public_url = os.getenv(
        "WEBHOOK_URL",
        "https://vynora-live-new.onrender.com/api/telegram/webhook",
    ).rstrip("/")

    if not public_url.startswith("http"):
        print("WARNING: WEBHOOK_URL is invalid. Telegram webhook not set.")
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json={"url": public_url},
            timeout=15,
        )
        print("Telegram webhook:", response.text)
    except requests.RequestException as exc:
        print("Telegram webhook setup failed:", exc)


def notify_group(group_id, text):
    return telegram_send(group_id, text)


def validate_telegram_init_data(init_data):
    """
    Validate Telegram WebApp initData.
    Returns Telegram user object or None.
    """
    if not BOT_TOKEN or not init_data:
        return None

    try:
        pairs = [
            x.split("=", 1)
            for x in init_data.split("&")
            if "=" in x
        ]
        data = dict(pairs)
        received_hash = data.pop("hash", None)

        if not received_hash:
            return None

        check_string = "\n".join(
            f"{key}={data[key]}"
            for key in sorted(data)
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        calculated = hmac.new(
            secret_key,
            check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calculated, received_hash):
            return None

        user_obj = json.loads(data.get("user", "{}"))
        auth_date = int(data.get("auth_date", "0"))

        # WebApp authentication validity: 24 hours.
        if datetime.now(timezone.utc).timestamp() - auth_date > 86400:
            return None

        return user_obj

    except Exception:
        return None


def get_current_user():
    telegram_id = (
        request.headers.get("X-Telegram-Id")
        or request.args.get("telegram_id")
    )

    if not telegram_id:
        return None

    try:
        telegram_id = int(telegram_id)
    except (ValueError, TypeError):
        return None

    return db().execute(
        "SELECT * FROM users WHERE telegram_id=?",
        (telegram_id,),
    ).fetchone()


def require_user(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()

        if not user:
            return jsonify({
                "ok": False,
                "error": "Authentication required",
            }), 401

        if user["status"] != "active":
            return jsonify({
                "ok": False,
                "error": "Your account is blocked.",
            }), 403

        g.current_user = user
        return fn(*args, **kwargs)

    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()

        if not user:
            return jsonify({
                "ok": False,
                "error": "Authentication required",
            }), 401

        if user["telegram_id"] not in SUPER_ADMINS:
            return jsonify({
                "ok": False,
                "error": "Admin only",
            }), 403

        g.current_user = user
        return fn(*args, **kwargs)

    return wrapper


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "Vynora Live",
        "status": "online",
        "time": iso_now(),
    })


@app.route("/api/config")
def config():
    return jsonify({
        "ok": True,
        "durations": PRICES,
        "recharge_plans": RECHARGE_PLANS,
        "gifts": GIFTS,
        "super_admins": list(SUPER_ADMINS),
        "upi_id": "vynoralive@slc",
        "upi_name": "Rajnish Kumar",
    })


@app.route("/api/auth", methods=["POST"])
def auth():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    telegram_user = validate_telegram_init_data(init_data)

    # Development fallback.
    # IMPORTANT: set REQUIRE_TELEGRAM_AUTH=1 in Render production.
    if (
        not telegram_user
        and os.getenv("REQUIRE_TELEGRAM_AUTH", "0") != "1"
    ):
        telegram_user = {
            "id": int(data.get("telegram_id", 7778606261)),
            "first_name": data.get("name", "Demo User"),
            "username": data.get("username", "demo_user"),
        }

    if not telegram_user:
        return jsonify({
            "ok": False,
            "error": "Invalid Telegram authentication",
        }), 401

    telegram_id = int(telegram_user["id"])
    name = (
        f"{telegram_user.get('first_name', '')} "
        f"{telegram_user.get('last_name', '')}"
    ).strip()
    username = telegram_user.get("username", "")

    existing = db().execute(
        "SELECT * FROM users WHERE telegram_id=?",
        (telegram_id,),
    ).fetchone()

    is_new = existing is None

    if is_new:
        role = "admin" if telegram_id in SUPER_ADMINS else "user"

        db().execute(
            """
            INSERT INTO users
            (telegram_id,username,name,country,role,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                telegram_id,
                username,
                name or username or "Telegram User",
                "India",
                role,
                iso_now(),
            ),
        )
        db().commit()

        event_log(
            "new_user",
            f"New user registered: {name} (@{username}) [{telegram_id}]",
        )

        notify_group(
            GROUP_2_ID,
            f"🆕 <b>New User</b>\n"
            f"Name: {name}\n"
            f"Username: @{username}\n"
            f"User ID: {telegram_id}\n"
            f"Country: India\n"
            f"Time: {iso_now()}",
        )

    else:
        db().execute(
            """
            UPDATE users
            SET username=?,name=?
            WHERE telegram_id=?
            """,
            (
                username,
                name or existing["name"],
                telegram_id,
            ),
        )
        db().commit()

    user = db().execute(
        "SELECT * FROM users WHERE telegram_id=?",
        (telegram_id,),
    ).fetchone()

    return jsonify({
        "ok": True,
        "user": dict(user),
        "new_user": is_new,
    })


@app.route("/api/me")
@require_user
def me():
    user = dict(g.current_user)

    host = db().execute(
        "SELECT * FROM hosts WHERE user_id=?",
        (user["id"],),
    ).fetchone()

    user["host"] = dict(host) if host else None

    return jsonify({
        "ok": True,
        "user": user,
    })


@app.route("/api/hosts")
@require_user
def hosts():
    rows = db().execute(
        """
        SELECT h.*, u.username
        FROM hosts h
        JOIN users u ON u.id=h.user_id
        WHERE h.approved=1
        ORDER BY h.dummy ASC,
                 h.online DESC,
                 h.rating DESC,
                 h.id DESC
        """
    ).fetchall()

    return jsonify({
        "ok": True,
        "hosts": [dict(row) for row in rows],
    })


@app.route("/api/host/apply", methods=["POST"])
@require_user
def host_apply():
    data = request.get_json(silent=True) or {}

    name = data.get("name", "").strip()
    country = data.get("country", "India").strip()
    photo = data.get("photo_url", "").strip()
    bio = data.get("bio", "").strip()

    if not name:
        return jsonify({
            "ok": False,
            "error": "Name is required",
        }), 400

    if country.lower() != "india":
        return jsonify({
            "ok": False,
            "error": "Only India-based hosts are currently accepted",
        }), 400

    existing = db().execute(
        "SELECT * FROM hosts WHERE user_id=?",
        (g.current_user["id"],),
    ).fetchone()

    if existing:
        return jsonify({
            "ok": False,
            "error": "Host application already exists",
        }), 409

    db().execute(
        """
        INSERT INTO hosts
        (user_id,name,photo_url,country,bio,approved,dummy,created_at)
        VALUES(?,?,?,?,?,0,0,?)
        """,
        (
            g.current_user["id"],
            name,
            photo,
            country,
            bio,
            iso_now(),
        ),
    )
    db().commit()

    message = (
        f"🎙️ <b>New Host Application</b>\n"
        f"Name: {name}\n"
        f"Country: {country}\n"
        f"User ID: {g.current_user['telegram_id']}\n"
        f"Bio: {bio}"
    )

    event_log("host_application", message)
    notify_group(
        GROUP_1_ID,
        message + "\n\nUse Admin Dashboard to approve/reject.",
    )

    return jsonify({
        "ok": True,
        "message": "Application submitted",
    })


@app.route("/api/bookings", methods=["POST"])
@require_user
def create_booking():
    data = request.get_json(silent=True) or {}

    try:
        host_id = int(data.get("host_id", 0))
        duration = int(data.get("duration", 0))
    except (ValueError, TypeError):
        return jsonify({
            "ok": False,
            "error": "Invalid host or duration",
        }), 400

    booking_date = data.get("date", "").strip()
    booking_time = data.get("time", "").strip()

    if duration not in PRICES:
        return jsonify({
            "ok": False,
            "error": "Invalid duration",
        }), 400

    if not booking_date or not booking_time:
        return jsonify({
            "ok": False,
            "error": "Date and time are required",
        }), 400

    host = db().execute(
        """
        SELECT * FROM hosts
        WHERE id=? AND approved=1
        """,
        (host_id,),
    ).fetchone()

    if not host:
        return jsonify({
            "ok": False,
            "error": "Host not available",
        }), 404

    # Frontend + backend country restriction.
    if host["country"].lower() != "india" or host["dummy"]:
        return jsonify({
            "ok": False,
            "error": "Foreign/demo hosts cannot be booked",
        }), 400

    tokens = PRICES[duration]

    if g.current_user["wallet"] < tokens:
        return jsonify({
            "ok": False,
            "error": "Insufficient token balance",
        }), 400

    db().execute(
        """
        UPDATE users
        SET wallet=wallet-?
        WHERE id=? AND wallet>=?
        """,
        (
            tokens,
            g.current_user["id"],
            tokens,
        ),
    )

    db().execute(
        """
        INSERT INTO bookings
        (user_id,host_id,duration,tokens,
         booking_date,booking_time,created_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            g.current_user["id"],
            host_id,
            duration,
            tokens,
            booking_date,
            booking_time,
            iso_now(),
        ),
    )

    db().commit()

    booking_id = db().execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    username = (
        g.current_user["username"]
        or g.current_user["name"]
    )

    message = (
        f"📅 <b>Booking Request #{booking_id}</b>\n"
        f"Host: {host['name']}\n"
        f"User: @{username}\n"
        f"Duration: {duration} min\n"
        f"Date: {booking_date}\n"
        f"Time: {booking_time}\n"
        f"Tokens: {tokens}"
    )

    event_log("booking_request", message)
    notify_group(GROUP_1_ID, message)

    telegram_send(
        g.current_user["telegram_id"],
        f"✅ आपने {host['name']} को {duration} Minutes के लिए "
        f"book किया है।\nHost confirmation का इंतजार करें।",
    )

    host_user = db().execute(
        "SELECT telegram_id FROM users WHERE id=?",
        (host["user_id"],),
    ).fetchone()

    if host_user:
        telegram_send(
            host_user["telegram_id"],
            f"📥 नई Booking Request\n"
            f"{g.current_user['name']} ने {duration} Minutes के लिए "
            f"booking की है।\nDate: {booking_date}\nTime: {booking_time}",
        )

    return jsonify({
        "ok": True,
        "booking_id": booking_id,
    })


@app.route("/api/bookings")
@require_user
def my_bookings():
    rows = db().execute(
        """
        SELECT b.*, h.name AS host_name,
               h.photo_url AS host_photo
        FROM bookings b
        JOIN hosts h ON h.id=b.host_id
        WHERE b.user_id=?
        ORDER BY b.id DESC
        """,
        (g.current_user["id"],),
    ).fetchall()

    return jsonify({
        "ok": True,
        "bookings": [dict(row) for row in rows],
    })


@app.route("/api/host/bookings")
@require_user
def host_bookings():
    host = db().execute(
        "SELECT * FROM hosts WHERE user_id=?",
        (g.current_user["id"],),
    ).fetchone()

    if not host:
        return jsonify({
            "ok": False,
            "error": "Host profile not found",
        }), 404

    rows = db().execute(
        """
        SELECT b.*, u.name AS user_name,
               u.username, u.telegram_id
        FROM bookings b
        JOIN users u ON u.id=b.user_id
        WHERE b.host_id=?
        ORDER BY b.id DESC
        """,
        (host["id"],),
    ).fetchall()

    return jsonify({
        "ok": True,
        "bookings": [dict(row) for row in rows],
        "host": dict(host),
    })


@app.route("/api/booking/<int:booking_id>/decision", methods=["POST"])
@require_user
def booking_decision(booking_id):
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")

    host = db().execute(
        "SELECT * FROM hosts WHERE user_id=?",
        (g.current_user["id"],),
    ).fetchone()

    if not host:
        return jsonify({
            "ok": False,
            "error": "Host only",
        }), 403

    booking = db().execute(
        """
        SELECT * FROM bookings
        WHERE id=? AND host_id=?
        """,
        (
            booking_id,
            host["id"],
        ),
    ).fetchone()

    if not booking or booking["status"] != "pending":
        return jsonify({
            "ok": False,
            "error": "Booking unavailable",
        }), 404

    if decision not in ("accepted", "rejected"):
        return jsonify({
            "ok": False,
            "error": "Invalid decision",
        }), 400

    db().execute(
        "UPDATE bookings SET status=? WHERE id=?",
        (
            decision,
            booking_id,
        ),
    )

    # Rejected booking refunds reserved tokens.
    if decision == "rejected":
        db().execute(
            """
            UPDATE users
            SET wallet=wallet+?
            WHERE id=?
            """,
            (
                booking["tokens"],
                booking["user_id"],
            ),
        )

    db().commit()

    user = db().execute(
        "SELECT * FROM users WHERE id=?",
        (booking["user_id"],),
    ).fetchone()

    message = (
        f"🎙️ Host {host['name']} ने आपकी booking "
        f"#{booking_id} को {decision} किया।"
    )

    if user:
        telegram_send(user["telegram_id"], message)

    event_log("booking_decision", message)

    return jsonify({
        "ok": True,
        "status": decision,
    })


@app.route("/api/booking/<int:booking_id>/schedule", methods=["POST"])
@require_user
def schedule_booking(booking_id):
    data = request.get_json(silent=True) or {}
    scheduled_at = data.get("scheduled_at", "").strip()

    host = db().execute(
        "SELECT * FROM hosts WHERE user_id=?",
        (g.current_user["id"],),
    ).fetchone()

    if not host:
        return jsonify({
            "ok": False,
            "error": "Host only",
        }), 403

    booking = db().execute(
        """
        SELECT * FROM bookings
        WHERE id=? AND host_id=? AND status='accepted'
        """,
        (
            booking_id,
            host["id"],
        ),
    ).fetchone()

    if not booking or not scheduled_at:
        return jsonify({
            "ok": False,
            "error": "Accepted booking and schedule are required",
        }), 400

    db().execute(
        """
        UPDATE bookings
        SET scheduled_at=?,status='scheduled'
        WHERE id=?
        """,
        (
            scheduled_at,
            booking_id,
        ),
    )
    db().commit()

    user = db().execute(
        "SELECT * FROM users WHERE id=?",
        (booking["user_id"],),
    ).fetchone()

    if user:
        telegram_send(
            user["telegram_id"],
            f"📞 {host['name']} आपको {scheduled_at} पर call करेगा।",
        )

    event_log(
        "call_scheduled",
        f"Booking #{booking_id} scheduled for {scheduled_at}",
    )

    return jsonify({"ok": True})


@app.route("/api/gift", methods=["POST"])
@require_user
def send_gift():
    data = request.get_json(silent=True) or {}

    try:
        booking_id = int(data.get("booking_id", 0))
    except (ValueError, TypeError):
        return jsonify({
            "ok": False,
            "error": "Invalid booking",
        }), 400

    gift_key = data.get("gift_key", "")

    if gift_key not in GIFTS:
        return jsonify({
            "ok": False,
            "error": "Invalid gift",
        }), 400

    booking = db().execute(
        """
        SELECT * FROM bookings
        WHERE id=? AND user_id=?
        """,
        (
            booking_id,
            g.current_user["id"],
        ),
    ).fetchone()

    if not booking:
        return jsonify({
            "ok": False,
            "error": "Booking not found",
        }), 404

    label, tokens = GIFTS[gift_key]

    if g.current_user["wallet"] < tokens:
        return jsonify({
            "ok": False,
            "error": "Insufficient tokens",
        }), 400

    db().execute(
        "UPDATE users SET wallet=wallet-? WHERE id=?",
        (
            tokens,
            g.current_user["id"],
        ),
    )

    # Host gets 60% of gift value.
    host_earning = int(tokens * 0.60)

    db().execute(
        "UPDATE hosts SET balance=balance+? WHERE id=?",
        (
            host_earning,
            booking["host_id"],
        ),
    )

    db().execute(
        """
        INSERT INTO gifts
        (booking_id,user_id,host_id,gift_key,tokens,created_at)
        VALUES(?,?,?,?,?,?)
        """,
        (
            booking_id,
            g.current_user["id"],
            booking["host_id"],
            gift_key,
            tokens,
            iso_now(),
        ),
    )

    db().commit()

    host = db().execute(
        """
        SELECT h.*,u.telegram_id
        FROM hosts h
        JOIN users u ON u.id=h.user_id
        WHERE h.id=?
        """,
        (booking["host_id"],),
    ).fetchone()

    message = (
        f"🎁 Gift received\n"
        f"Host: {host['name']}\n"
        f"Gift: {label}\n"
        f"Value: {tokens} tokens\n"
        f"Host earning: {host_earning}"
    )

    telegram_send(host["telegram_id"], message)

    event_log(
        "gift",
        message + f"\nBooking: #{booking_id}",
    )

    notify_group(
        GROUP_3_ID,
        message + f"\nBooking ID: {booking_id}",
    )

    return jsonify({
        "ok": True,
        "gift": label,
        "tokens": tokens,
    })


@app.route("/api/recharge", methods=["POST"])
@require_user
def recharge():
    data = request.get_json(silent=True) or {}

    try:
        amount = int(data.get("amount", 0))
    except (ValueError, TypeError):
        amount = 0

    utr = data.get("utr", "").strip()
    screenshot = data.get("screenshot", "").strip()

    if amount not in RECHARGE_PLANS:
        return jsonify({
            "ok": False,
            "error": "Invalid recharge plan",
        }), 400

    if not utr or not screenshot:
        return jsonify({
            "ok": False,
            "error": "UTR and screenshot are compulsory",
        }), 400

    tokens = RECHARGE_PLANS[amount]

    db().execute(
        """
        INSERT INTO recharge_requests
        (user_id,amount,tokens,utr,screenshot,created_at)
        VALUES(?,?,?,?,?,?)
        """,
        (
            g.current_user["id"],
            amount,
            tokens,
            utr,
            screenshot,
            iso_now(),
        ),
    )
    db().commit()

    request_id = db().execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    message = (
        f"💰 <b>Recharge Request #{request_id}</b>\n"
        f"User: {g.current_user['name']}\n"
        f"User ID: {g.current_user['telegram_id']}\n"
        f"Amount: ₹{amount}\n"
        f"Tokens: {tokens}\n"
        f"UTR: {utr}\n"
        f"Screenshot: {screenshot}"
    )

    event_log("recharge_request", message)
    notify_group(GROUP_1_ID, message)

    return jsonify({
        "ok": True,
        "request_id": request_id,
    })


@app.route("/api/host/withdraw", methods=["POST"])
@require_user
def withdraw():
    data = request.get_json(silent=True) or {}

    try:
        amount = int(data.get("amount", 0))
    except (ValueError, TypeError):
        amount = 0

    payment_details = data.get(
        "payment_details",
        "",
    ).strip()

    host = db().execute(
        "SELECT * FROM hosts WHERE user_id=?",
        (g.current_user["id"],),
    ).fetchone()

    if not host or not host["approved"]:
        return jsonify({
            "ok": False,
            "error": "Approved host only",
        }), 403

    if amount <= 0 or amount > host["balance"]:
        return jsonify({
            "ok": False,
            "error": "Invalid withdrawal amount",
        }), 400

    if not payment_details:
        return jsonify({
            "ok": False,
            "error": "Payment details required",
        }), 400

    db().execute(
        "UPDATE hosts SET balance=balance-? WHERE id=?",
        (
            amount,
            host["id"],
        ),
    )

    db().execute(
        """
        INSERT INTO withdrawals
        (host_id,amount,payment_details,created_at)
        VALUES(?,?,?,?)
        """,
        (
            host["id"],
            amount,
            payment_details,
            iso_now(),
        ),
    )

    db().commit()

    withdrawal_id = db().execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    message = (
        f"💸 Withdrawal #{withdrawal_id}\n"
        f"Host: {host['name']}\n"
        f"Amount: ₹{amount}\n"
        f"Status: pending"
    )

    event_log("withdrawal", message)
    notify_group(GROUP_3_ID, message)

    return jsonify({
        "ok": True,
        "request_id": withdrawal_id,
    })


@app.route("/api/admin/stats")
@require_admin
def admin_stats():
    def count(sql):
        return db().execute(sql).fetchone()[0]

    return jsonify({
        "ok": True,
        "stats": {
            "users": count("SELECT COUNT(*) FROM users"),
            "hosts": count(
                "SELECT COUNT(*) FROM hosts "
                "WHERE approved=1 AND dummy=0"
            ),
            "pending_hosts": count(
                "SELECT COUNT(*) FROM hosts WHERE approved=0"
            ),
            "bookings": count(
                "SELECT COUNT(*) FROM bookings"
            ),
            "pending_recharge": count(
                "SELECT COUNT(*) FROM recharge_requests "
                "WHERE status='pending'"
            ),
            "withdrawals": count(
                "SELECT COUNT(*) FROM withdrawals "
                "WHERE status='pending'"
            ),
        },
    })


@app.route("/api/admin/hosts")
@require_admin
def admin_hosts():
    rows = db().execute(
        """
        SELECT h.*,u.telegram_id,u.username
        FROM hosts h
        JOIN users u ON u.id=h.user_id
        ORDER BY h.approved ASC,h.id DESC
        """
    ).fetchall()

    return jsonify({
        "ok": True,
        "hosts": [dict(row) for row in rows],
    })


@app.route("/api/admin/host/<int:host_id>", methods=["POST"])
@require_admin
def admin_host_decision(host_id):
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")

    if decision not in ("approve", "reject"):
        return jsonify({
            "ok": False,
            "error": "Invalid decision",
        }), 400

    host = db().execute(
        """
        SELECT h.*,u.telegram_id
        FROM hosts h
        JOIN users u ON u.id=h.user_id
        WHERE h.id=?
        """,
        (host_id,),
    ).fetchone()

    if not host:
        return jsonify({
            "ok": False,
            "error": "Host not found",
        }), 404

    approved = 1 if decision == "approve" else 0

    db().execute(
        "UPDATE hosts SET approved=? WHERE id=?",
        (
            approved,
            host_id,
        ),
    )
    db().commit()

    message = (
        f"🎙️ Host {host['name']} "
        f"{'approved' if approved else 'rejected'} "
        f"by Super Admin."
    )

    telegram_send(
        host["telegram_id"],
        message,
    )

    event_log(
        "host_approval",
        message,
    )

    notify_group(
        GROUP_3_ID,
        message
        + f"\nHost ID: {host_id}\nTime: {iso_now()}",
    )

    return jsonify({"ok": True})


@app.route("/api/admin/recharges")
@require_admin
def admin_recharges():
    rows = db().execute(
        """
        SELECT r.*,u.name,u.username,u.telegram_id
        FROM recharge_requests r
        JOIN users u ON u.id=r.user_id
        ORDER BY r.id DESC
        """
    ).fetchall()

    return jsonify({
        "ok": True,
        "recharges": [dict(row) for row in rows],
    })


@app.route("/api/admin/recharge/<int:request_id>", methods=["POST"])
@require_admin
def admin_recharge_decision(request_id):
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")

    if decision not in ("approve", "reject"):
        return jsonify({
            "ok": False,
            "error": "Invalid decision",
        }), 400

    row = db().execute(
        "SELECT * FROM recharge_requests WHERE id=?",
        (request_id,),
    ).fetchone()

    if not row or row["status"] != "pending":
        return jsonify({
            "ok": False,
            "error": "Recharge unavailable",
        }), 404

    db().execute(
        """
        UPDATE recharge_requests
        SET status=?
        WHERE id=?
        """,
        (
            decision,
            request_id,
        ),
    )

    if decision == "approve":
        db().execute(
            """
            UPDATE users
            SET wallet=wallet+?
            WHERE id=?
            """,
            (
                row["tokens"],
                row["user_id"],
            ),
        )

    # Keep recharge history but remove heavy screenshot data after review.
    db().execute(
        """
        UPDATE recharge_requests
        SET screenshot=''
        WHERE id=?
        """,
        (request_id,),
    )

    db().commit()

    user = db().execute(
        "SELECT * FROM users WHERE id=?",
        (row["user_id"],),
    ).fetchone()

    if user:
        token_message = (
            str(row["tokens"])
            if decision == "approve"
            else "0"
        )

        telegram_send(
            user["telegram_id"],
            f"💰 Recharge #{request_id} {decision}.\n"
            f"Tokens: {token_message}",
        )

    event_log(
        "recharge_decision",
        f"Recharge #{request_id} {decision}",
    )

    return jsonify({"ok": True})


@app.route("/api/admin/events")
@require_admin
def admin_events():
    rows = db().execute(
        """
        SELECT * FROM events
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()

    return jsonify({
        "ok": True,
        "events": [dict(row) for row in rows],
    })


# -------------------------------------------------------------------
# TELEGRAM WEBHOOK
# IMPORTANT:
# We support BOTH URLs so your existing Telegram configuration keeps
# working:
#   /api/telegram/webhook
#   /telegram/webhook
# -------------------------------------------------------------------

@app.route("/api/telegram/webhook", methods=["POST"])
@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    telegram_user = message.get("from") or {}
    text = (message.get("text") or "").strip()

    if not telegram_user.get("id"):
        return jsonify({"ok": True})

    telegram_id = int(telegram_user["id"])

    name = (
        f"{telegram_user.get('first_name', '')} "
        f"{telegram_user.get('last_name', '')}"
    ).strip()

    username = telegram_user.get("username", "")

    existing = db().execute(
        "SELECT * FROM users WHERE telegram_id=?",
        (telegram_id,),
    ).fetchone()

    if not existing:
        role = (
            "admin"
            if telegram_id in SUPER_ADMINS
            else "user"
        )

        db().execute(
            """
            INSERT INTO users
            (telegram_id,username,name,country,role,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                telegram_id,
                username,
                name or username or "Telegram User",
                "India",
                role,
                iso_now(),
            ),
        )
        db().commit()

        notify_group(
            GROUP_2_ID,
            f"🆕 <b>New User</b>\n"
            f"Name: {name}\n"
            f"Username: @{username}\n"
            f"ID: {telegram_id}\n"
            f"Time: {iso_now()}",
        )

        event_log(
            "new_user",
            f"Telegram /start registration: {telegram_id}",
        )

    # Final /start welcome message.
    if text == "/start":
        web_app_url = os.getenv(
            "WEB_APP_URL",
            "https://vynora-live-new.onrender.com",
        ).strip()

        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "🚀 Open Vynora Live",
                    "web_app": {
                        "url": web_app_url
                    },
                }
            ]]
        }

        telegram_send(
            chat.get("id", telegram_id),
            "❤️ <b>Welcome to Vynora Live</b>\n\n"
            "Private 1-to-1 Host Calling Platform.\n\n"
            "🇮🇳 India-based Real Hosts\n"
            "📅 Slot Booking\n"
            "📞 Scheduled Video Calls\n"
            "🎁 Gifts\n"
            "💰 Wallet & Recharge\n\n"
            "नीचे दिए button से Vynora Live खोलें।",
            keyboard,
        )

    # Admin commands remain separate.
    elif text.startswith("/") and telegram_id in SUPER_ADMINS:
        command = text.split()[0].lower()

        if command == "/helpadmin":
            telegram_send(
                telegram_id,
                "👑 <b>VYNORA LIVE — VERIFIED SUPER ADMIN</b>\n\n"
                "/helpadmin\n"
                "/approvehost\n"
                "/rejecthost\n"
                "/addhost\n"
                "/removehost\n"
                "/ban\n"
                "/unban\n"
                "/block\n"
                "/unblock\n"
                "/user\n"
                "/addtoken\n"
                "/removetoken\n"
                "/settoken\n"
                "/approverecharge\n"
                "/rejectrecharge\n"
                "/announce\n"
                "/offer\n"
                "/stats",
            )
        else:
            telegram_send(
                telegram_id,
                "👑 VYNORA LIVE — VERIFIED SUPER ADMIN\n\n"
                f"Command received: {text}\n\n"
                "Full management के लिए Admin Dashboard इस्तेमाल करें.",
            )

    return jsonify({"ok": True})


# -------------------------------------------------------------------
# WEBRTC / CALL SIGNALING
# -------------------------------------------------------------------

@socketio.on("join_call")
def join_call(data):
    booking_id = str(data.get("booking_id", ""))

    if not booking_id:
        return

    join_room(booking_id)

    emit(
        "peer_joined",
        {"booking_id": booking_id},
        room=booking_id,
        include_self=False,
    )


@socketio.on("webrtc_signal")
def webrtc_signal(data):
    booking_id = str(data.get("booking_id", ""))

    if booking_id:
        emit(
            "webrtc_signal",
            data,
            room=booking_id,
            include_self=False,
        )


@socketio.on("call_started")
def call_started(data):
    try:
        booking_id = int(data.get("booking_id", 0))
    except (ValueError, TypeError):
        return

    booking = db().execute(
        "SELECT * FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()

    if not booking:
        return

    # SERVER-AUTHORITATIVE start time.
    started = iso_now()

    db().execute(
        """
        UPDATE bookings
        SET call_started_at=?,status='in_call'
        WHERE id=?
        """,
        (
            started,
            booking_id,
        ),
    )
    db().commit()

    emit(
        "server_timer_started",
        {
            "booking_id": booking_id,
            "started_at": started,
        },
        room=str(booking_id),
    )


@socketio.on("call_ended")
def call_ended(data):
    try:
        booking_id = int(data.get("booking_id", 0))
    except (ValueError, TypeError):
        return

    booking = db().execute(
        "SELECT * FROM bookings WHERE id=?",
        (booking_id,),
    ).fetchone()

    if not booking or not booking["call_started_at"]:
        return

    # Prevent duplicate completion/earning.
    if booking["status"] == "completed":
        return

    ended = iso_now()

    db().execute(
        """
        UPDATE bookings
        SET call_ended_at=?,status='completed'
        WHERE id=?
        """,
        (
            ended,
            booking_id,
        ),
    )

    # Call revenue: 60% to host.
    host_earning = int(booking["tokens"] * 0.60)

    db().execute(
        """
        UPDATE hosts
        SET balance=balance+?
        WHERE id=?
        """,
        (
            host_earning,
            booking["host_id"],
        ),
    )

    db().commit()

    message = (
        f"📞 <b>Session Completed</b>\n"
        f"Booking ID: {booking_id}\n"
        f"Duration: {booking['duration']} min\n"
        f"Tokens: {booking['tokens']}\n"
        f"Host earning: {host_earning}\n"
        f"Start: {booking['call_started_at']}\n"
        f"End: {ended}\n"
        f"Status: completed"
    )

    event_log(
        "call_completed",
        message,
    )

    # Group 3 compulsory call-completion log.
    notify_group(
        GROUP_3_ID,
        message,
    )

    emit(
        "call_completed",
        {
            "booking_id": booking_id,
            "ended_at": ended,
        },
        room=str(booking_id),
    )


if __name__ == "__main__":
    init_db()

    # Automatically fix/register Telegram webhook on every deployment.
    set_telegram_webhook()

    port = int(os.getenv("PORT", "5000"))

    print("==============================================")
    print("❤️ VYNORA LIVE SERVER STARTED")
    print("Port:", port)
    print("Webhook:", "/api/telegram/webhook")
    print("Alternative webhook:", "/telegram/webhook")
    print("==============================================")

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        allow_unsafe_werkzeug=True,
    )







