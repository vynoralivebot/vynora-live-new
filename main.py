import os
import json
import hmac
import hashlib
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import Flask, request, jsonify, send_from_directory, g
from flask_socketio import SocketIO, emit, join_room

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vynora_live.db")

app = Flask(__name__, static_folder=".", static_url_path="")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(32))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_1_ID = os.getenv("GROUP_1_ID", "")
GROUP_2_ID = os.getenv("GROUP_2_ID", "")
GROUP_3_ID = os.getenv("GROUP_3_ID", "")
SUPER_ADMINS = {7778606261, 7001825467}

PRICES = {
    1: 20, 3: 50, 6: 100, 10: 160,
    15: 240, 20: 320, 30: 450
}

RECHARGE_PLANS = {
    50: 50, 100: 105, 200: 210, 300: 320,
    500: 550, 1000: 1150, 1500: 1750,
    2000: 2400, 3000: 3600
}

GIFTS = {
    "rose": ("🌹 Rose", 10),
    "heart": ("❤️ Heart", 25),
    "coffee": ("☕ Coffee", 50),
    "diamond": ("💎 Diamond", 100),
    "crown": ("👑 Crown", 250),
    "rocket": ("🚀 Rocket", 500)
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
    conn.executescript("""
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
    """)
    conn.commit()
    conn.close()


def event_log(event_type, message):
    db().execute(
        "INSERT INTO events(event_type,message,created_at) VALUES(?,?,?)",
        (event_type, message, iso_now())
    )
    db().commit()


def telegram_send(chat_id, text, reply_markup=None):
    if not BOT_TOKEN or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.ok
    except requests.RequestException:
        return False


def notify_group(group_id, text):
    return telegram_send(group_id, text)


def validate_telegram_init_data(init_data):
    """Validate Telegram WebApp initData. Returns parsed user or None."""
    if not BOT_TOKEN or not init_data:
        return None

    try:
        pairs = [x.split("=", 1) for x in init_data.split("&") if "=" in x]
        data = dict(pairs)
        received_hash = data.pop("hash", None)
        if not received_hash:
            return None

        check_string = "\n".join(
            f"{k}={data[k]}" for k in sorted(data)
        )
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated = hmac.new(
            secret_key, check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated, received_hash):
            return None

        user_obj = json.loads(data.get("user", "{}"))
        auth_date = int(data.get("auth_date", "0"))
        if datetime.now(timezone.utc).timestamp() - auth_date > 86400:
            return None
        return user_obj
    except Exception:
        return None


def get_current_user():
    tid = request.headers.get("X-Telegram-Id") or request.args.get("telegram_id")
    if not tid:
        return None
    try:
        tid = int(tid)
    except ValueError:
        return None
    return db().execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()


def require_user(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"ok": False, "error": "Authentication required"}), 401
        g.current_user = user
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user or user["telegram_id"] not in SUPER_ADMINS:
            return jsonify({"ok": False, "error": "Admin only"}), 403
        g.current_user = user
        return fn(*args, **kwargs)
    return wrapper


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/config")
def config():
    return jsonify({
        "ok": True,
        "durations": PRICES,
        "recharge_plans": RECHARGE_PLANS,
        "gifts": GIFTS,
        "super_admins": list(SUPER_ADMINS)
    })


@app.route("/api/auth", methods=["POST"])
def auth():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    tg_user = validate_telegram_init_data(init_data)

    # Development fallback. Disable this in production by setting REQUIRE_TELEGRAM_AUTH=1.
    if not tg_user and os.getenv("REQUIRE_TELEGRAM_AUTH", "0") != "1":
        tg_user = {
            "id": int(data.get("telegram_id", 7778606261)),
            "first_name": data.get("name", "Demo User"),
            "username": data.get("username", "demo_user")
        }

    if not tg_user:
        return jsonify({"ok": False, "error": "Invalid Telegram authentication"}), 401

    tid = int(tg_user["id"])
    name = (tg_user.get("first_name", "") + " " + tg_user.get("last_name", "")).strip()
    username = tg_user.get("username", "")

    existing = db().execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
    is_new = existing is None

    if is_new:
        role = "admin" if tid in SUPER_ADMINS else "user"
        db().execute("""
            INSERT INTO users(telegram_id,username,name,country,role,created_at)
            VALUES(?,?,?,?,?,?)
        """, (tid, username, name or username or "Telegram User", "India", role, iso_now()))
        db().commit()
        event_log("new_user", f"New user registered: {name} (@{username}) [{tid}]")
        notify_group(
            GROUP_2_ID,
            f"🆕 <b>New User</b>\nName: {name}\nUsername: @{username}\n"
            f"User ID: {tid}\nCountry: India\nTime: {iso_now()}"
        )
    else:
        db().execute(
            "UPDATE users SET username=?,name=? WHERE telegram_id=?",
            (username, name or existing["name"], tid)
        )
        db().commit()

    user = db().execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
    return jsonify({"ok": True, "user": dict(user), "new_user": is_new})


@app.route("/api/me")
@require_user
def me():
    user = dict(g.current_user)
    host = db().execute("SELECT * FROM hosts WHERE user_id=?", (user["id"],)).fetchone()
    user["host"] = dict(host) if host else None
    return jsonify({"ok": True, "user": user})


@app.route("/api/hosts")
@require_user
def hosts():
    rows = db().execute("""
        SELECT h.*, u.username
        FROM hosts h JOIN users u ON u.id=h.user_id
        WHERE h.approved=1
        ORDER BY h.dummy ASC, h.online DESC, h.rating DESC, h.id DESC
    """).fetchall()
    return jsonify({"ok": True, "hosts": [dict(x) for x in rows]})


@app.route("/api/host/apply", methods=["POST"])
@require_user
def host_apply():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    country = data.get("country", "India").strip()
    photo = data.get("photo_url", "").strip()
    bio = data.get("bio", "").strip()

    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 400

    if country.lower() != "india":
        return jsonify({"ok": False, "error": "Only India-based hosts are currently accepted"}), 400

    existing = db().execute("SELECT * FROM hosts WHERE user_id=?", (g.current_user["id"],)).fetchone()
    if existing:
        return jsonify({"ok": False, "error": "Host application already exists"}), 409

    db().execute("""
        INSERT INTO hosts(user_id,name,photo_url,country,bio,approved,dummy,created_at)
        VALUES(?,?,?,?,?,0,0,?)
    """, (g.current_user["id"], name, photo, country, bio, iso_now()))
    db().commit()

    msg = f"🎙️ <b>New Host Application</b>\nName: {name}\nCountry: {country}\nUser ID: {g.current_user['telegram_id']}\nBio: {bio}"
    event_log("host_application", msg)
    notify_group(GROUP_1_ID, msg + "\n\nUse Admin Dashboard to approve/reject.")
    return jsonify({"ok": True, "message": "Application submitted"})


@app.route("/api/bookings", methods=["POST"])
@require_user
def create_booking():
    data = request.get_json(silent=True) or {}
    host_id = int(data.get("host_id", 0))
    duration = int(data.get("duration", 0))
    booking_date = data.get("date", "")
    booking_time = data.get("time", "")

    if duration not in PRICES:
        return jsonify({"ok": False, "error": "Invalid duration"}), 400
    if not booking_date or not booking_time:
        return jsonify({"ok": False, "error": "Date and time are required"}), 400

    host = db().execute(
        "SELECT * FROM hosts WHERE id=? AND approved=1", (host_id,)
    ).fetchone()
    if not host:
        return jsonify({"ok": False, "error": "Host not available"}), 404

    if host["country"].lower() != "india" or host["dummy"]:
        return jsonify({"ok": False, "error": "Foreign/demo hosts cannot be booked"}), 400

    tokens = PRICES[duration]
    if g.current_user["wallet"] < tokens:
        return jsonify({"ok": False, "error": "Insufficient token balance"}), 400

    # Reserve tokens immediately; refund if an admin later rejects.
    db().execute(
        "UPDATE users SET wallet=wallet-? WHERE id=? AND wallet>=?",
        (tokens, g.current_user["id"], tokens)
    )
    db().execute("""
        INSERT INTO bookings(user_id,host_id,duration,tokens,booking_date,booking_time,created_at)
        VALUES(?,?,?,?,?,?,?)
    """, (g.current_user["id"], host_id, duration, tokens, booking_date, booking_time, iso_now()))
    db().commit()

    booking_id = db().execute("SELECT last_insert_rowid()").fetchone()[0]
    username = g.current_user["username"] or g.current_user["name"]

    msg = (
        f"📅 <b>Booking Request #{booking_id}</b>\n"
        f"Host: {host['name']}\nUser: @{username}\n"
        f"Duration: {duration} min\nDate: {booking_date}\nTime: {booking_time}\n"
        f"Tokens: {tokens}"
    )
    event_log("booking_request", msg)
    notify_group(GROUP_1_ID, msg)
    telegram_send(
        g.current_user["telegram_id"],
        f"✅ आपने {host['name']} को {duration} Minutes के लिए book किया है।\n"
        f"Host confirmation का इंतजार करें।"
    )
    telegram_send(
        db().execute("SELECT telegram_id FROM users WHERE id=?", (host["user_id"],)).fetchone()["telegram_id"],
        f"📥 नई Booking Request\n{g.current_user['name']} ने {duration} Minutes के लिए booking की है।\n"
        f"Date: {booking_date}\nTime: {booking_time}"
    )
    return jsonify({"ok": True, "booking_id": booking_id})


@app.route("/api/bookings")
@require_user
def my_bookings():
    rows = db().execute("""
        SELECT b.*, h.name AS host_name, h.photo_url AS host_photo
        FROM bookings b JOIN hosts h ON h.id=b.host_id
        WHERE b.user_id=?
        ORDER BY b.id DESC
    """, (g.current_user["id"],)).fetchall()
    return jsonify({"ok": True, "bookings": [dict(x) for x in rows]})


@app.route("/api/host/bookings")
@require_user
def host_bookings():
    host = db().execute("SELECT * FROM hosts WHERE user_id=?", (g.current_user["id"],)).fetchone()
    if not host:
        return jsonify({"ok": False, "error": "Host profile not found"}), 404
    rows = db().execute("""
        SELECT b.*, u.name AS user_name, u.username, u.telegram_id
        FROM bookings b JOIN users u ON u.id=b.user_id
        WHERE b.host_id=?
        ORDER BY b.id DESC
    """, (host["id"],)).fetchall()
    return jsonify({"ok": True, "bookings": [dict(x) for x in rows], "host": dict(host)})


@app.route("/api/booking/<int:booking_id>/decision", methods=["POST"])
@require_user
def booking_decision(booking_id):
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")
    host = db().execute("SELECT * FROM hosts WHERE user_id=?", (g.current_user["id"],)).fetchone()
    if not host:
        return jsonify({"ok": False, "error": "Host only"}), 403

    booking = db().execute(
        "SELECT * FROM bookings WHERE id=? AND host_id=?", (booking_id, host["id"])
    ).fetchone()
    if not booking or booking["status"] != "pending":
        return jsonify({"ok": False, "error": "Booking unavailable"}), 404

    if decision not in ("accepted", "rejected"):
        return jsonify({"ok": False, "error": "Invalid decision"}), 400

    db().execute("UPDATE bookings SET status=? WHERE id=?", (decision, booking_id))
    if decision == "rejected":
        db().execute(
            "UPDATE users SET wallet=wallet+? WHERE id=?",
            (booking["tokens"], booking["user_id"])
        )
    db().commit()

    user = db().execute("SELECT * FROM users WHERE id=?", (booking["user_id"],)).fetchone()
    text = f"🎙️ Host {host['name']} ने आपकी booking #{booking_id} को {decision} किया।"
    telegram_send(user["telegram_id"], text)
    event_log("booking_decision", text)
    return jsonify({"ok": True, "status": decision})


@app.route("/api/booking/<int:booking_id>/schedule", methods=["POST"])
@require_user
def schedule_booking(booking_id):
    data = request.get_json(silent=True) or {}
    scheduled_at = data.get("scheduled_at")
    host = db().execute("SELECT * FROM hosts WHERE user_id=?", (g.current_user["id"],)).fetchone()
    if not host:
        return jsonify({"ok": False, "error": "Host only"}), 403

    booking = db().execute(
        "SELECT * FROM bookings WHERE id=? AND host_id=? AND status='accepted'",
        (booking_id, host["id"])
    ).fetchone()
    if not booking or not scheduled_at:
        return jsonify({"ok": False, "error": "Accepted booking and schedule are required"}), 400

    db().execute(
        "UPDATE bookings SET scheduled_at=?,status='scheduled' WHERE id=?",
        (scheduled_at, booking_id)
    )
    db().commit()

    user = db().execute("SELECT * FROM users WHERE id=?", (booking["user_id"],)).fetchone()
    telegram_send(user["telegram_id"], f"📞 {host['name']} आपको {scheduled_at} पर call करेगा।")
    event_log("call_scheduled", f"Booking #{booking_id} scheduled for {scheduled_at}")
    return jsonify({"ok": True})


@app.route("/api/gift", methods=["POST"])
@require_user
def send_gift():
    data = request.get_json(silent=True) or {}
    booking_id = int(data.get("booking_id", 0))
    gift_key = data.get("gift_key", "")
    if gift_key not in GIFTS:
        return jsonify({"ok": False, "error": "Invalid gift"}), 400

    booking = db().execute(
        "SELECT * FROM bookings WHERE id=? AND user_id=?",
        (booking_id, g.current_user["id"])
    ).fetchone()
    if not booking:
        return jsonify({"ok": False, "error": "Booking not found"}), 404

    label, tokens = GIFTS[gift_key]
    if g.current_user["wallet"] < tokens:
        return jsonify({"ok": False, "error": "Insufficient tokens"}), 400

    db().execute("UPDATE users SET wallet=wallet-? WHERE id=?", (tokens, g.current_user["id"]))
    db().execute("UPDATE hosts SET balance=balance+? WHERE id=?", (int(tokens * 0.60), booking["host_id"]))
    db().execute("""
        INSERT INTO gifts(booking_id,user_id,host_id,gift_key,tokens,created_at)
        VALUES(?,?,?,?,?,?)
    """, (booking_id, g.current_user["id"], booking["host_id"], gift_key, tokens, iso_now()))
    db().commit()

    host = db().execute(
        "SELECT h.*,u.telegram_id FROM hosts h JOIN users u ON u.id=h.user_id WHERE h.id=?",
        (booking["host_id"],)
    ).fetchone()
    text = f"🎁 Gift received\nHost: {host['name']}\nGift: {label}\nValue: {tokens} tokens"
    telegram_send(host["telegram_id"], text)
    event_log("gift", text + f"\nBooking: #{booking_id}")
    notify_group(GROUP_3_ID, text + f"\nBooking ID: {booking_id}")
    return jsonify({"ok": True, "gift": label, "tokens": tokens})


@app.route("/api/recharge", methods=["POST"])
@require_user
def recharge():
    data = request.get_json(silent=True) or {}
    amount = int(data.get("amount", 0))
    utr = data.get("utr", "").strip()
    screenshot = data.get("screenshot", "").strip()

    if amount not in RECHARGE_PLANS:
        return jsonify({"ok": False, "error": "Invalid recharge plan"}), 400
    if not utr or not screenshot:
        return jsonify({"ok": False, "error": "UTR and screenshot are compulsory"}), 400

    tokens = RECHARGE_PLANS[amount]
    db().execute("""
        INSERT INTO recharge_requests(user_id,amount,tokens,utr,screenshot,created_at)
        VALUES(?,?,?,?,?,?)
    """, (g.current_user["id"], amount, tokens, utr, screenshot, iso_now()))
    db().commit()
    rid = db().execute("SELECT last_insert_rowid()").fetchone()[0]

    msg = (
        f"💰 <b>Recharge Request #{rid}</b>\n"
        f"User: {g.current_user['name']}\nUser ID: {g.current_user['telegram_id']}\n"
        f"Amount: ₹{amount}\nTokens: {tokens}\nUTR: {utr}\n"
        f"Screenshot: {screenshot}"
    )
    event_log("recharge_request", msg)
    notify_group(GROUP_1_ID, msg)
    return jsonify({"ok": True, "request_id": rid})


@app.route("/api/host/withdraw", methods=["POST"])
@require_user
def withdraw():
    data = request.get_json(silent=True) or {}
    amount = int(data.get("amount", 0))
    details = data.get("payment_details", "").strip()

    host = db().execute("SELECT * FROM hosts WHERE user_id=?", (g.current_user["id"],)).fetchone()
    if not host or not host["approved"]:
        return jsonify({"ok": False, "error": "Approved host only"}), 403
    if amount <= 0 or amount > host["balance"]:
        return jsonify({"ok": False, "error": "Invalid withdrawal amount"}), 400
    if not details:
        return jsonify({"ok": False, "error": "Payment details required"}), 400

    db().execute("UPDATE hosts SET balance=balance-? WHERE id=?", (amount, host["id"]))
    db().execute("""
        INSERT INTO withdrawals(host_id,amount,payment_details,created_at)
        VALUES(?,?,?,?)
    """, (host["id"], amount, details, iso_now()))
    db().commit()

    wid = db().execute("SELECT last_insert_rowid()").fetchone()[0]
    msg = f"💸 Withdrawal #{wid}\nHost: {host['name']}\nAmount: ₹{amount}\nStatus: pending"
    event_log("withdrawal", msg)
    notify_group(GROUP_3_ID, msg)
    return jsonify({"ok": True, "request_id": wid})


@app.route("/api/admin/stats")
@require_admin
def admin_stats():
    q = lambda sql: db().execute(sql).fetchone()[0]
    return jsonify({
        "ok": True,
        "stats": {
            "users": q("SELECT COUNT(*) FROM users"),
            "hosts": q("SELECT COUNT(*) FROM hosts WHERE approved=1 AND dummy=0"),
            "pending_hosts": q("SELECT COUNT(*) FROM hosts WHERE approved=0"),
            "bookings": q("SELECT COUNT(*) FROM bookings"),
            "pending_recharge": q("SELECT COUNT(*) FROM recharge_requests WHERE status='pending'"),
            "withdrawals": q("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")
        }
    })


@app.route("/api/admin/hosts")
@require_admin
def admin_hosts():
    rows = db().execute("""
        SELECT h.*,u.telegram_id,u.username
        FROM hosts h JOIN users u ON u.id=h.user_id
        ORDER BY h.approved ASC,h.id DESC
    """).fetchall()
    return jsonify({"ok": True, "hosts": [dict(x) for x in rows]})


@app.route("/api/admin/host/<int:host_id>", methods=["POST"])
@require_admin
def admin_host_decision(host_id):
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")
    if decision not in ("approve", "reject"):
        return jsonify({"ok": False, "error": "Invalid decision"}), 400

    host = db().execute("""
        SELECT h.*,u.telegram_id FROM hosts h JOIN users u ON u.id=h.user_id
        WHERE h.id=?
    """, (host_id,)).fetchone()
    if not host:
        return jsonify({"ok": False, "error": "Host not found"}), 404

    approved = 1 if decision == "approve" else 0
    db().execute("UPDATE hosts SET approved=? WHERE id=?", (approved, host_id))
    db().commit()

    text = f"🎙️ Host {host['name']} {'approved' if approved else 'rejected'} by Super Admin."
    telegram_send(host["telegram_id"], text)
    event_log("host_approval", text)
    notify_group(GROUP_3_ID, text + f"\nHost ID: {host_id}\nTime: {iso_now()}")
    return jsonify({"ok": True})


@app.route("/api/admin/recharges")
@require_admin
def admin_recharges():
    rows = db().execute("""
        SELECT r.*,u.name,u.username,u.telegram_id
        FROM recharge_requests r JOIN users u ON u.id=r.user_id
        ORDER BY r.id DESC
    """).fetchall()
    return jsonify({"ok": True, "recharges": [dict(x) for x in rows]})


@app.route("/api/admin/recharge/<int:rid>", methods=["POST"])
@require_admin
def admin_recharge_decision(rid):
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")
    if decision not in ("approve", "reject"):
        return jsonify({"ok": False, "error": "Invalid decision"}), 400

    row = db().execute("SELECT * FROM recharge_requests WHERE id=?", (rid,)).fetchone()
    if not row or row["status"] != "pending":
        return jsonify({"ok": False, "error": "Recharge unavailable"}), 404

    db().execute("UPDATE recharge_requests SET status=? WHERE id=?", (decision, rid))
    if decision == "approve":
        db().execute("UPDATE users SET wallet=wallet+? WHERE id=?", (row["tokens"], row["user_id"]))
    # The screenshot is intentionally removed after review. Only basic recharge history remains.
    db().execute("UPDATE recharge_requests SET screenshot='' WHERE id=?", (rid,))
    db().commit()

    user = db().execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    telegram_send(user["telegram_id"], f"💰 Recharge #{rid} {decision}.\nTokens: {row['tokens'] if decision=='approve' else 0}")
    event_log("recharge_decision", f"Recharge #{rid} {decision}")
    return jsonify({"ok": True})


@app.route("/api/admin/events")
@require_admin
def admin_events():
    rows = db().execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify({"ok": True, "events": [dict(x) for x in rows]})


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Optional Telegram webhook endpoint for /start and admin commands."""
    update = request.get_json(silent=True) or {}
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    text = (message.get("text") or "").strip()

    if not user.get("id"):
        return jsonify({"ok": True})

    tid = int(user["id"])
    name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip()
    username = user.get("username", "")

    existing = db().execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
    if not existing:
        role = "admin" if tid in SUPER_ADMINS else "user"
        db().execute("""
            INSERT INTO users(telegram_id,username,name,country,role,created_at)
            VALUES(?,?,?,?,?,?)
        """, (tid, username, name, "India", role, iso_now()))
        db().commit()
        notify_group(
            GROUP_2_ID,
            f"🆕 New User\nName: {name}\nUsername: @{username}\nID: {tid}\nTime: {iso_now()}"
        )

    if text == "/start":
        keyboard = {"inline_keyboard": [[
            {"text": "🚀 Open Vynora Live", "web_app": {"url": os.getenv("WEB_APP_URL", "https://YOUR-DOMAIN.com")}}
        ]]}
        telegram_send(
            chat.get("id", tid),
            "❤️ <b>Welcome to Vynora Live</b>\n\n"
            "Book an India-based Host, schedule a private 1-to-1 call, "
            "send gifts and manage your wallet.",
            keyboard
        )
    elif text.startswith("/") and tid in SUPER_ADMINS:
        telegram_send(tid, "👑 VYNORA LIVE — VERIFIED SUPER ADMIN\n\n"
                           "Admin commands are enabled. Use the Admin Dashboard for full management.")
    return jsonify({"ok": True})


@socketio.on("join_call")
def join_call(data):
    room = str(data.get("booking_id", ""))
    if not room:
        return
    join_room(room)
    emit("peer_joined", {"booking_id": room}, room=room, include_self=False)


@socketio.on("webrtc_signal")
def webrtc_signal(data):
    room = str(data.get("booking_id", ""))
    if room:
        emit("webrtc_signal", data, room=room, include_self=False)


@socketio.on("call_started")
def call_started(data):
    booking_id = int(data.get("booking_id", 0))
    booking = db().execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not booking:
        return

    # Server timestamp is the authoritative start timestamp.
    started = iso_now()
    db().execute(
        "UPDATE bookings SET call_started_at=?,status='in_call' WHERE id=?",
        (started, booking_id)
    )
    db().commit()
    emit("server_timer_started", {"booking_id": booking_id, "started_at": started}, room=str(booking_id))


@socketio.on("call_ended")
def call_ended(data):
    booking_id = int(data.get("booking_id", 0))
    booking = db().execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not booking or not booking["call_started_at"]:
        return

    ended = iso_now()
    db().execute(
        "UPDATE bookings SET call_ended_at=?,status='completed' WHERE id=?",
        (ended, booking_id)
    )
    # 60% of call token revenue goes to the host.
    host_earning = int(booking["tokens"] * 0.60)
    db().execute(
        "UPDATE hosts SET balance=balance+? WHERE id=?",
        (host_earning, booking["host_id"])
    )
    db().commit()

    msg = (
        f"📞 <b>Session Completed</b>\nBooking ID: {booking_id}\n"
        f"Duration: {booking['duration']} min\nTokens: {booking['tokens']}\n"
        f"Host earning: {host_earning}\nStart: {booking['call_started_at']}\nEnd: {ended}"
    )
    event_log("call_completed", msg)
    notify_group(GROUP_3_ID, msg)
    emit("call_completed", {"booking_id": booking_id, "ended_at": ended}, room=str(booking_id))


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "5000"))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)







