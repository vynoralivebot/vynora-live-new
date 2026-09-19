import os
import json
import hmac
import hashlib
import sqlite3
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import requests
try:
    import qrcode
except ImportError:
    qrcode = None
from flask import Flask, request, jsonify, send_from_directory, g, send_file
from flask_socketio import SocketIO, emit, join_room

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vynora_live.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder=".", static_url_path="")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(32))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://vynora-live-new.onrender.com").strip().rstrip("/")
GROUP_1_ID = os.getenv("GROUP_1_ID", "").strip()
GROUP_2_ID = os.getenv("GROUP_2_ID", "").strip()
GROUP_3_ID = os.getenv("GROUP_3_ID", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip()
SUPER_ADMINS = {7778606261, 7001825467}

PRICES = {1:20, 3:50, 6:100, 10:160, 15:240, 20:320, 30:450}
RECHARGE_PLANS = {50:50, 100:105, 200:210, 300:320, 500:550, 1000:1150, 1500:1750, 2000:2400, 3000:3600}
GIFTS = {
    "rose": ("🌹 Rose", 10),
    "heart": ("❤️ Heart", 25),
    "coffee": ("☕ Coffee", 50),
    "diamond": ("💎 Diamond", 100),
    "crown": ("👑 Crown", 250),
    "rocket": ("🚀 Rocket", 500),
}
FILTERS = ["Natural", "Glow ✨", "Soft", "Warm"]
ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp"}
MAX_UPLOAD = 8 * 1024 * 1024
CALL_PEERS = {}  # booking/admin room -> set of Socket.IO session IDs


def now():
    return datetime.now(timezone.utc).isoformat()


def conn():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    c = g.pop("db", None)
    if c:
        c.close()


def init_db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER UNIQUE NOT NULL,
      username TEXT DEFAULT '', name TEXT DEFAULT '', photo_url TEXT DEFAULT '',
      country TEXT DEFAULT 'India', wallet INTEGER DEFAULT 0,
      role TEXT DEFAULT 'user', status TEXT DEFAULT 'active', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hosts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE NOT NULL,
      name TEXT NOT NULL, photo_url TEXT DEFAULT '', country TEXT DEFAULT 'India',
      bio TEXT DEFAULT '', rating REAL DEFAULT 5, online INTEGER DEFAULT 0,
      approved INTEGER DEFAULT 0, dummy INTEGER DEFAULT 0, filter_name TEXT DEFAULT 'Natural',
      balance INTEGER DEFAULT 0, created_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS bookings(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, host_id INTEGER NOT NULL,
      duration INTEGER NOT NULL, tokens INTEGER NOT NULL, booking_date TEXT NOT NULL,
      booking_time TEXT NOT NULL, status TEXT DEFAULT 'pending', scheduled_at TEXT DEFAULT '',
      call_started_at TEXT DEFAULT '', call_ended_at TEXT DEFAULT '', created_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(host_id) REFERENCES hosts(id)
    );
    CREATE TABLE IF NOT EXISTS gifts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      host_id INTEGER NOT NULL, gift_key TEXT NOT NULL, tokens INTEGER NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS recharge_requests(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
      tokens INTEGER NOT NULL, utr TEXT NOT NULL, screenshot_path TEXT DEFAULT '',
      status TEXT DEFAULT 'pending', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS withdrawals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, host_id INTEGER NOT NULL, amount INTEGER NOT NULL,
      payment_details TEXT NOT NULL, status TEXT DEFAULT 'pending', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS notifications(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL,
      kind TEXT DEFAULT 'info', is_read INTEGER DEFAULT 0, created_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT ''
    );
    """)
    c.executemany("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", [
        ("banner_text", "❤️ Welcome to Vynora Live"),
        ("banner_image", ""),
        ("withdrawals_per_day", "1"),
        ("host_share_percent", "60"),
    ])
    for col, ddl in [
        ("gift_earnings", "ALTER TABLE hosts ADD COLUMN gift_earnings INTEGER DEFAULT 0"),
        ("approved_at", "ALTER TABLE hosts ADD COLUMN approved_at TEXT DEFAULT ''"),
    ]:
        try: c.execute(ddl)
        except sqlite3.OperationalError: pass
    c.commit()
    c.close()


def setting(key, default=""):
    row=conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def set_setting(key, value):
    conn().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,str(value)))
    conn().commit()

def notify_user(user_id, title, message, kind="info", telegram=True):
    c=conn(); c.execute("INSERT INTO notifications(user_id,title,message,kind,created_at) VALUES(?,?,?,?,?)", (user_id,title,message,kind,now())); c.commit()
    if telegram:
        u=c.execute("SELECT telegram_id FROM users WHERE id=?",(user_id,)).fetchone()
        if u: tg_send(u["telegram_id"], f"<b>{title}</b>\n{message}")


def log_event(kind, msg):
    c = conn()
    c.execute("INSERT INTO events(event_type,message,created_at) VALUES(?,?,?)", (kind, msg, now()))
    c.commit()


def tg_request(method, payload=None, files=None):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN not configured"}
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                           data=payload, files=files, timeout=20)
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


def tg_send(chat_id, text, markup=None):
    if not chat_id:
        return False
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if markup:
        payload["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    result = tg_request("sendMessage", payload)
    if not result.get("ok"):
        log_event("telegram_error", f"sendMessage {chat_id}: {result.get('description')}")
    return bool(result.get("ok"))


def tg_send_photo(chat_id, path, caption):
    if not chat_id or not path or not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        result = tg_request("sendPhoto", {"chat_id": chat_id, "caption": caption}, {"photo": f})
    if not result.get("ok"):
        log_event("telegram_error", f"sendPhoto {chat_id}: {result.get('description')}")
    return bool(result.get("ok"))


def notify_group(group_id, text, photo_path=None):
    if not group_id:
        log_event("telegram_config_error", "Group ID is empty; notification not sent.")
        return False
    if photo_path and os.path.exists(photo_path):
        return tg_send_photo(group_id, photo_path, text)
    return tg_send(group_id, text)


def set_webhook():
    if not BOT_TOKEN:
        print("BOT_TOKEN missing; Telegram webhook not configured.")
        return
    webhook = f"{WEB_APP_URL}/api/telegram/webhook"
    result = tg_request("setWebhook", {"url": webhook, "allowed_updates": json.dumps(["message","callback_query"])})
    print("Telegram webhook:", result)
    if not result.get("ok"):
        log_event("telegram_error", f"setWebhook: {result.get('description')}")


def validate_init_data(init_data):
    if not BOT_TOKEN or not init_data:
        return None
    try:
        vals = dict(parse_qsl(init_data, keep_blank_values=True))
        received = vals.pop("hash", None)
        if not received:
            return None
        check = "\n".join(f"{k}={vals[k]}" for k in sorted(vals))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        auth_date = int(vals.get("auth_date", "0"))
        if datetime.now(timezone.utc).timestamp() - auth_date > 86400:
            return None
        return json.loads(vals.get("user", "{}"))
    except Exception:
        return None


def current_user():
    tid = request.headers.get("X-Telegram-Id") or request.args.get("telegram_id")
    try:
        tid = int(tid)
    except (ValueError, TypeError):
        return None
    return conn().execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()


def require_user(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return jsonify(ok=False, error="Authentication required"), 401
        if u["status"] != "active":
            return jsonify(ok=False, error="Account blocked"), 403
        g.user = u
        return fn(*a, **kw)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u or u["telegram_id"] not in SUPER_ADMINS:
            return jsonify(ok=False, error="Super Admin only"), 403
        g.user = u
        return fn(*a, **kw)
    return wrapper


def upload_image(file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError("Image is required")
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_IMAGE_EXT:
        raise ValueError("Only JPG, JPEG, PNG and WEBP images are allowed")
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_UPLOAD:
        raise ValueError("Image must be 8 MB or smaller")
    name = secrets.token_hex(16) + "." + ext
    path = os.path.join(UPLOAD_DIR, name)
    file_storage.save(path)
    return f"/uploads/{name}", path


@app.route("/")
def index():
    response = send_from_directory(BASE_DIR, "index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/uploads/<name>")
def uploaded(name):
    safe = os.path.basename(name)
    path = os.path.join(UPLOAD_DIR, safe)
    if not os.path.exists(path):
        return jsonify(ok=False, error="File not found"), 404
    return send_file(path)


@app.route("/api/health")
def health():
    return jsonify(ok=True, service="Vynora Live", status="online", time=now())


@app.route("/api/config")
def config():
    return jsonify(ok=True, durations=PRICES, recharge_plans=RECHARGE_PLANS,
                   gifts={k: {"name": v[0], "tokens": v[1]} for k,v in GIFTS.items()},
                   filters=FILTERS, upi_id="vynoralive@slc", upi_name="Rajnish Kumar",
                   support_username=SUPPORT_USERNAME, admins=list(SUPER_ADMINS))



@app.route("/api/upi-qr")
def upi_qr():
    if qrcode is None:
        return jsonify(ok=False, error="QR library unavailable"), 503
    amount = request.args.get("amount", "").strip()
    upi = f"upi://pay?pa=vynoralive@slc&pn=Rajnish%20Kumar&cu=INR"
    if amount:
        upi += f"&am={amount}"
    img = qrcode.make(upi)
    import io
    out = io.BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return send_file(out, mimetype="image/png")

@app.route("/api/auth", methods=["POST"])
def auth():
    data = request.get_json(silent=True) or {}
    tg_user = validate_init_data(data.get("initData", ""))
    if not tg_user and os.getenv("REQUIRE_TELEGRAM_AUTH", "0") != "1":
        tg_user = {"id": int(data.get("telegram_id", 7778606261)),
                   "first_name": data.get("name", "Demo User"),
                   "username": data.get("username", "demo_user")}
    if not tg_user:
        return jsonify(ok=False, error="Invalid Telegram authentication"), 401
    tid = int(tg_user["id"])
    name = (tg_user.get("first_name","") + " " + tg_user.get("last_name","")).strip() or "Telegram User"
    username = tg_user.get("username","")
    c = conn()
    row = c.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
    new = row is None
    if new:
        role = "admin" if tid in SUPER_ADMINS else "user"
        c.execute("""INSERT INTO users(telegram_id,username,name,role,created_at)
                     VALUES(?,?,?,?,?)""", (tid, username, name, role, now()))
        c.commit()
        log_event("new_user", f"{name} @{username} [{tid}]")
        notify_group(GROUP_2_ID, f"🆕 <b>New User Registration</b>\nName: {name}\nUsername: @{username}\nUser ID: {tid}\nCountry: India\nTime: {now()}")
    else:
        c.execute("UPDATE users SET username=?,name=? WHERE telegram_id=?", (username,name,tid)); c.commit()
    row = c.execute("SELECT * FROM users WHERE telegram_id=?", (tid,)).fetchone()
    return jsonify(ok=True, new_user=new, user=dict(row))


@app.route("/api/me")
@require_user
def me():
    u = dict(g.user)
    h = conn().execute("SELECT * FROM hosts WHERE user_id=?", (g.user["id"],)).fetchone()
    u["host"] = dict(h) if h else None
    return jsonify(ok=True, user=u)


@app.route("/api/profile", methods=["POST"])
@require_user
def profile():
    name = request.form.get("name", g.user["name"]).strip()
    country = request.form.get("country", g.user["country"]).strip() or "India"
    file = request.files.get("photo")
    photo_url = g.user["photo_url"]
    if file:
        try:
            photo_url, _ = upload_image(file)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
    conn().execute("UPDATE users SET name=?,country=?,photo_url=? WHERE id=?", (name,country,photo_url,g.user["id"]))
    conn().commit()
    return jsonify(ok=True)


@app.route("/api/host/apply", methods=["POST"])
@require_user
def host_apply():
    name = request.form.get("name","").strip()
    country = request.form.get("country","India").strip()
    bio = request.form.get("bio","").strip()
    file = request.files.get("photo")
    if not name or not file:
        return jsonify(ok=False,error="Name and photo are required"),400
    if country.lower() != "india":
        return jsonify(ok=False,error="Only India-based hosts are accepted"),400
    if conn().execute("SELECT 1 FROM hosts WHERE user_id=?", (g.user["id"],)).fetchone():
        return jsonify(ok=False,error="Host application already exists"),409
    try:
        photo_url, path = upload_image(file)
    except ValueError as e:
        return jsonify(ok=False,error=str(e)),400
    c=conn()
    c.execute("""INSERT INTO hosts(user_id,name,photo_url,country,bio,approved,dummy,created_at)
                 VALUES(?,?,?,?,?,0,0,?)""",(g.user["id"],name,photo_url,country,bio,now()))
    c.commit()
    msg=f"🎙️ <b>NEW HOST APPLICATION</b>\nName: {name}\nCountry: {country}\nUser ID: {g.user['telegram_id']}\nBio: {bio}"
    log_event("host_application",msg); notify_group(GROUP_1_ID,msg,path)
    return jsonify(ok=True)


@app.route("/api/host/profile", methods=["POST"])
@require_user
def host_profile():
    h=conn().execute("SELECT * FROM hosts WHERE user_id=?", (g.user["id"],)).fetchone()
    if not h: return jsonify(ok=False,error="Host profile not found"),404
    name=request.form.get("name",h["name"]).strip()
    bio=request.form.get("bio",h["bio"]).strip()
    filter_name=request.form.get("filter_name",h["filter_name"])
    if filter_name not in FILTERS: filter_name="Natural"
    photo_url=h["photo_url"]; file=request.files.get("photo")
    if file:
        try: photo_url,_=upload_image(file)
        except ValueError as e: return jsonify(ok=False,error=str(e)),400
    c=conn(); c.execute("UPDATE hosts SET name=?,bio=?,filter_name=?,photo_url=? WHERE id=?",(name,bio,filter_name,photo_url,h["id"])); c.commit()
    return jsonify(ok=True)


@app.route("/api/host/status", methods=["POST"])
@require_user
def host_status():
    h=conn().execute("SELECT * FROM hosts WHERE user_id=? AND approved=1",(g.user["id"],)).fetchone()
    if not h: return jsonify(ok=False,error="Approved host only"),403
    online=1 if (request.get_json(silent=True) or {}).get("online") else 0
    conn().execute("UPDATE hosts SET online=? WHERE id=?",(online,h["id"])); conn().commit()
    return jsonify(ok=True,online=bool(online))


@app.route("/api/hosts")
@require_user
def hosts():
    rows=conn().execute("""SELECT h.*,u.username FROM hosts h JOIN users u ON u.id=h.user_id
                           WHERE h.approved=1 ORDER BY h.dummy ASC,h.online DESC,h.rating DESC,h.id DESC""").fetchall()
    return jsonify(ok=True, real_hosts=[dict(x) for x in rows if not x["dummy"] and x["country"].lower()=="india"],
                   dummy_hosts=[dict(x) for x in rows if x["dummy"] or x["country"].lower()!="india"])


@app.route("/api/bookings", methods=["POST"])
@require_user
def create_booking():
    d=request.get_json(silent=True) or {}
    try: hid=int(d.get("host_id")); duration=int(d.get("duration"))
    except: return jsonify(ok=False,error="Invalid host/duration"),400
    date=d.get("date","").strip(); time=d.get("time","").strip()
    if duration not in PRICES or not date or not time: return jsonify(ok=False,error="Duration, date and time required"),400
    h=conn().execute("SELECT * FROM hosts WHERE id=? AND approved=1",(hid,)).fetchone()
    if not h: return jsonify(ok=False,error="Host not available"),404
    if h["dummy"] or h["country"].lower()!="india": return jsonify(ok=False,error="Foreign/demo host cannot be booked"),400
    tokens=PRICES[duration]
    c=conn()
    if c.execute("SELECT wallet FROM users WHERE id=?",(g.user["id"],)).fetchone()["wallet"]<tokens:
        return jsonify(ok=False,error="Insufficient tokens"),400
    c.execute("UPDATE users SET wallet=wallet-? WHERE id=?",(tokens,g.user["id"]))
    c.execute("""INSERT INTO bookings(user_id,host_id,duration,tokens,booking_date,booking_time,created_at)
                 VALUES(?,?,?,?,?,?,?)""",(g.user["id"],hid,duration,tokens,date,time,now()))
    c.commit(); bid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
    msg=f"📅 <b>BOOKING REQUEST #{bid}</b>\nHost: {h['name']}\nUser: {g.user['name']} (@{g.user['username']})\nDuration: {duration} min\nDate: {date}\nTime: {time}\nTokens: {tokens}"
    log_event("booking",msg); notify_group(GROUP_1_ID,msg)
    hu=c.execute("SELECT telegram_id FROM users WHERE id=?",(h["user_id"],)).fetchone()
    if hu: tg_send(hu["telegram_id"],f"📥 New booking request #{bid}\n{duration} minutes\n{date} {time}")
    tg_send(g.user["telegram_id"],f"✅ आपने {h['name']} को {duration} Minutes के लिए book किया है। Host confirmation का इंतजार करें।")
    return jsonify(ok=True,booking_id=bid)


@app.route("/api/bookings")
@require_user
def my_bookings():
    rows=conn().execute("""SELECT b.*,h.name host_name,h.photo_url host_photo FROM bookings b
                           JOIN hosts h ON h.id=b.host_id WHERE b.user_id=? ORDER BY b.id DESC""",(g.user["id"],)).fetchall()
    return jsonify(ok=True,bookings=[dict(x) for x in rows])


@app.route("/api/host/bookings")
@require_user
def host_bookings():
    h=conn().execute("SELECT * FROM hosts WHERE user_id=?",(g.user["id"],)).fetchone()
    if not h:return jsonify(ok=False,error="Host profile not found"),404
    rows=conn().execute("""SELECT b.*,u.name user_name,u.username,u.telegram_id FROM bookings b
                           JOIN users u ON u.id=b.user_id WHERE b.host_id=? ORDER BY b.id DESC""",(h["id"],)).fetchall()
    return jsonify(ok=True,bookings=[dict(x) for x in rows],host=dict(h))


@app.route("/api/booking/<int:bid>/decision", methods=["POST"])
@require_user
def booking_decision(bid):
    d=request.get_json(silent=True) or {}; decision=d.get("decision")
    h=conn().execute("SELECT * FROM hosts WHERE user_id=?",(g.user["id"],)).fetchone()
    if not h:return jsonify(ok=False,error="Host only"),403
    b=conn().execute("SELECT * FROM bookings WHERE id=? AND host_id=?",(bid,h["id"])).fetchone()
    if not b or b["status"]!="pending":return jsonify(ok=False,error="Booking unavailable"),404
    if decision not in ("accepted","rejected"):return jsonify(ok=False,error="Invalid decision"),400
    c=conn(); c.execute("UPDATE bookings SET status=? WHERE id=?",(decision,bid))
    if decision=="rejected": c.execute("UPDATE users SET wallet=wallet+? WHERE id=?",(b["tokens"],b["user_id"]))
    c.commit()
    u=c.execute("SELECT * FROM users WHERE id=?",(b["user_id"],)).fetchone()
    if u: notify_user(u["id"],"🎙️ Booking Update",f"Host {h['name']} ने आपकी booking #{bid} को {decision} किया.","booking")
    log_event("booking_decision",f"Booking #{bid} {decision} by host {h['name']}")
    return jsonify(ok=True)


@app.route("/api/booking/<int:bid>/schedule", methods=["POST"])
@require_user
def schedule(bid):
    d=request.get_json(silent=True) or {}; scheduled=d.get("scheduled_at","").strip()
    h=conn().execute("SELECT * FROM hosts WHERE user_id=?",(g.user["id"],)).fetchone()
    if not h:return jsonify(ok=False,error="Host only"),403
    b=conn().execute("SELECT * FROM bookings WHERE id=? AND host_id=? AND status='accepted'",(bid,h["id"])).fetchone()
    if not b or not scheduled:return jsonify(ok=False,error="Accepted booking and time required"),400
    c=conn(); c.execute("UPDATE bookings SET scheduled_at=?,status='scheduled' WHERE id=?",(scheduled,bid)); c.commit()
    u=c.execute("SELECT * FROM users WHERE id=?",(b["user_id"],)).fetchone()
    if u: notify_user(u["id"],"📞 Call Scheduled",f"{h['name']} आपको {scheduled} पर call करेगा.","call")
    return jsonify(ok=True)


@app.route("/api/gift", methods=["POST"])
@require_user
def gift():
    d=request.get_json(silent=True) or {}
    try: bid=int(d.get("booking_id")); key=d.get("gift_key")
    except: return jsonify(ok=False,error="Invalid request"),400
    if key not in GIFTS:return jsonify(ok=False,error="Invalid gift"),400
    b=conn().execute("SELECT * FROM bookings WHERE id=? AND user_id=?",(bid,g.user["id"])).fetchone()
    if not b:return jsonify(ok=False,error="Booking not found"),404
    label,tokens=GIFTS[key]; c=conn(); bal=c.execute("SELECT wallet FROM users WHERE id=?",(g.user["id"],)).fetchone()["wallet"]
    if bal<tokens:return jsonify(ok=False,error="Insufficient tokens"),400
    host_earning=int(tokens*.60)
    c.execute("UPDATE users SET wallet=wallet-? WHERE id=?",(tokens,g.user["id"]))
    c.execute("UPDATE hosts SET balance=balance+?, gift_earnings=COALESCE(gift_earnings,0)+? WHERE id=?",(host_earning,host_earning,b["host_id"]))
    c.execute("INSERT INTO gifts(booking_id,user_id,host_id,gift_key,tokens,created_at) VALUES(?,?,?,?,?,?)",(bid,g.user["id"],b["host_id"],key,tokens,now()))
    c.commit()
    h=c.execute("SELECT h.*,u.telegram_id FROM hosts h JOIN users u ON u.id=h.user_id WHERE h.id=?",(b["host_id"],)).fetchone()
    msg=f"🎁 <b>Gift Received</b>\nHost: {h['name']}\nGift: {label}\nTokens: {tokens}\nHost earning: {host_earning}\nBooking ID: {bid}"
    tg_send(h["telegram_id"],msg); notify_user(g.user["id"],"🎁 Gift Sent",f"{label} भेजा गया. {tokens} tokens कटे.","gift"); notify_group(GROUP_3_ID,msg); log_event("gift",msg)
    return jsonify(ok=True)


@app.route("/api/recharge", methods=["POST"])
@require_user
def recharge():
    try: amount=int(request.form.get("amount","0"))
    except: amount=0
    utr=request.form.get("utr","").strip(); file=request.files.get("screenshot")
    if amount not in RECHARGE_PLANS:return jsonify(ok=False,error="Invalid recharge plan"),400
    if not utr or not file:return jsonify(ok=False,error="UTR and payment screenshot are required"),400
    try: url,path=upload_image(file)
    except ValueError as e:return jsonify(ok=False,error=str(e)),400
    tokens=RECHARGE_PLANS[amount]; c=conn()
    c.execute("""INSERT INTO recharge_requests(user_id,amount,tokens,utr,screenshot_path,created_at)
                 VALUES(?,?,?,?,?,?)""",(g.user["id"],amount,tokens,utr,path,now())); c.commit()
    rid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
    msg=f"💰 <b>RECHARGE REQUEST #{rid}</b>\nUser: {g.user['name']}\nID: {g.user['telegram_id']}\nAmount: ₹{amount}\nTokens: {tokens}\nUTR: {utr}"
    log_event("recharge",msg); notify_group(GROUP_1_ID,msg,path)
    notify_user(g.user["id"],"📤 Recharge Submitted",f"Recharge #{rid} submitted. Admin approval का इंतजार करें.","recharge")
    return jsonify(ok=True,request_id=rid)


@app.route("/api/host/withdraw", methods=["POST"])
@require_user
def withdraw():
    d=request.get_json(silent=True) or {}
    try: amount=int(d.get("amount",0))
    except: amount=0
    details=str(d.get("payment_details","" )).strip()
    h=conn().execute("SELECT * FROM hosts WHERE user_id=? AND approved=1",(g.user["id"],)).fetchone()
    if not h:return jsonify(ok=False,error="Approved host only"),403
    if amount<=0 or amount>h["balance"] or not details:return jsonify(ok=False,error="Invalid amount/payment details"),400
    from zoneinfo import ZoneInfo
    day=datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    exists=conn().execute("SELECT 1 FROM withdrawals WHERE host_id=? AND substr(created_at,1,10)=?",(h["id"],day)).fetchone()
    if exists:return jsonify(ok=False,error="आज 1 withdrawal already लगा चुके हैं. एक दिन में केवल 1 withdrawal allowed है."),400
    c=conn(); c.execute("UPDATE hosts SET balance=balance-? WHERE id=?",(amount,h["id"]))
    c.execute("INSERT INTO withdrawals(host_id,amount,payment_details,created_at) VALUES(?,?,?,?)",(h["id"],amount,details,now())); c.commit()
    wid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
    msg=f"💸 <b>WITHDRAWAL #{wid}</b>\nHost: {h['name']}\nAmount: ₹{amount}\nStatus: pending"
    log_event("withdrawal",msg); notify_group(GROUP_3_ID,msg)
    notify_user(g.user["id"],"💸 Withdrawal Submitted",f"₹{amount} withdrawal request #{wid} submitted. Admin approval का इंतजार करें.","finance")
    return jsonify(ok=True,request_id=wid)


@app.route("/api/notifications")
@require_user
def notifications():
    rows=conn().execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",(g.user["id"],)).fetchall()
    unread=conn().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",(g.user["id"],)).fetchone()[0]
    return jsonify(ok=True,notifications=[dict(x) for x in rows],unread=unread)

@app.route("/api/notifications/read", methods=["POST"])
@require_user
def notifications_read():
    conn().execute("UPDATE notifications SET is_read=1 WHERE user_id=?",(g.user["id"],)); conn().commit()
    return jsonify(ok=True)

@app.route("/api/help")
@require_user
def help_center():
    role="host" if conn().execute("SELECT 1 FROM hosts WHERE user_id=? AND approved=1",(g.user["id"],)).fetchone() else "user"
    common=[
      {"title":"📅 Booking कैसे करें?","text":"Real Hosts में host चुनें → Book → duration → date/time → Confirm Booking. Host accept करने के बाद call schedule होगा."},
      {"title":"💰 Recharge कैसे करें?","text":"Recharge plan चुनें → UPI QR से payment करें → UTR डालें → payment screenshot सीधे phone से upload करें → Submit. Admin approval के बाद tokens wallet में आएँगे."},
      {"title":"📞 Call कैसे join करें?","text":"Scheduled time पर incoming call आएगी. Accept दबाएँ. User और Host दोनों connect होने के बाद ही server timer शुरू होगा."},
      {"title":"🎁 Gift कैसे भेजें?","text":"Active call में gift चुनें. Gift tokens wallet से कटेंगे और host earning में जुड़ेंगे."},
    ]
    host_rules=[
      {"title":"🎙️ Host Rule","text":"केवल approved India-based host booking ले सकता है. Online/Offline status सही रखें."},
      {"title":"💸 Withdrawal Rule","text":"एक Host एक calendar day में केवल 1 withdrawal request लगा सकता है. Pending request रहते दूसरा withdrawal भी नहीं लगाया जा सकता."},
      {"title":"💰 Earnings","text":"Call revenue का 60% host earning में credit होगा. Gift earning अलग दिखाई जाएगी."},
      {"title":"📷 Profile & Filters","text":"Profile photo सीधे upload करें. Natural, Glow, Soft या Warm filter save करें; अगली call में वही preference रहेगी."},
    ]
    return jsonify(ok=True,role=role,common=common,host_rules=host_rules if role=="host" else [])

@app.route("/api/banner")
def banner():
    return jsonify(ok=True,text=setting("banner_text","❤️ Welcome to Vynora Live"),image=setting("banner_image",""))


# ---------------- ADMIN ----------------

@app.route("/api/admin/direct-call", methods=["POST"])
@require_admin
def admin_direct_call():
    d=request.get_json(silent=True) or {}
    try: uid=int(d.get("user_id"))
    except: return jsonify(ok=False,error="User ID required"),400
    u=conn().execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
    if not u:return jsonify(ok=False,error="User not found"),404
    room=f"admin_{g.user['telegram_id']}_{uid}_{secrets.token_hex(4)}"
    markup={"inline_keyboard":[[{"text":"📞 Join Admin Call","web_app":{"url":f"{WEB_APP_URL}/?admin_call={room}"}}]]}
    sent=tg_send(u["telegram_id"],"📞 <b>VYNORA LIVE Admin wants to speak with you immediately.</b>\nThis is a free Admin direct call.",markup)
    socketio.emit("admin_incoming_call",{"room":room,"title":"VYNORA LIVE Admin","free":True},room=f"user_{uid}")
    msg=f"📞 Admin direct call to {u['name']} (user #{uid})"
    log_event("admin_direct_call",msg); notify_group(GROUP_3_ID,msg)
    return jsonify(ok=True,sent=sent,room=room)


@app.route("/api/admin/bookings")
@require_admin
def admin_bookings():
    rows=conn().execute("""SELECT b.*,h.name host_name,u.name user_name,u.username
                           FROM bookings b JOIN hosts h ON h.id=b.host_id JOIN users u ON u.id=b.user_id
                           ORDER BY b.id DESC LIMIT 500""").fetchall()
    return jsonify(ok=True,bookings=[dict(x) for x in rows])


@app.route("/api/admin/daily-stats")
@require_admin
def admin_daily_stats():
    from zoneinfo import ZoneInfo
    day=datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    c=conn()
    def n(sql,args=()): return c.execute(sql,args).fetchone()[0]
    return jsonify(ok=True,date=day,stats={
      "new_users_today":n("SELECT COUNT(*) FROM users WHERE substr(created_at,1,10)=?",(day,)),
      "recharges_today":n("SELECT COUNT(*) FROM recharge_requests WHERE substr(created_at,1,10)=? AND status='approved'",(day,)),
      "recharge_amount_today":n("SELECT COALESCE(SUM(amount),0) FROM recharge_requests WHERE substr(created_at,1,10)=? AND status='approved'",(day,)),
      "hosts_approved_today":n("SELECT COUNT(*) FROM hosts WHERE substr(COALESCE(approved_at,created_at),1,10)=? AND approved=1 AND dummy=0",(day,)),
      "host_earnings_today":n("SELECT COALESCE(SUM(CAST(tokens*0.60 AS INTEGER)),0) FROM bookings WHERE substr(call_ended_at,1,10)=? AND status='completed'",(day)),
      "calls_completed_today":n("SELECT COUNT(*) FROM bookings WHERE substr(call_ended_at,1,10)=? AND status='completed'",(day)),
      "gifts_today":n("SELECT COALESCE(SUM(tokens),0) FROM gifts WHERE substr(created_at,1,10)=?",(day)),
      "withdrawals_today":n("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE substr(created_at,1,10)=?",(day)),
    })

@app.route("/api/admin/banner", methods=["POST"])
@require_admin
def admin_banner():
    text=request.form.get("text","").strip()
    file=request.files.get("image")
    if not text and not file:return jsonify(ok=False,error="Banner text or image required"),400
    image=setting("banner_image","")
    if file:
        try: image,_=upload_image(file)
        except ValueError as e:return jsonify(ok=False,error=str(e)),400
    set_setting("banner_text",text or setting("banner_text","")); set_setting("banner_image",image)
    msg=f"📢 <b>VYNORA LIVE BANNER</b>\n{text}" if text else "📢 <b>VYNORA LIVE BANNER UPDATED</b>"
    notify_group(GROUP_1_ID,msg); notify_group(GROUP_2_ID,msg); notify_group(GROUP_3_ID,msg)
    log_event("banner_update",msg)
    return jsonify(ok=True,text=setting("banner_text"),image=image)

@app.route("/api/admin/settings")
@require_admin
def admin_settings():
    return jsonify(ok=True,settings={
        "upi_id":"vynoralive@slc","upi_name":"Rajnish Kumar",
        "host_share_percent":int(setting("host_share_percent","60")),
        "withdrawals_per_day":int(setting("withdrawals_per_day","1")),
        "banner_text":setting("banner_text",""),
        "banner_image":setting("banner_image",""),"group_1_configured":bool(GROUP_1_ID),
        "group_2_configured":bool(GROUP_2_ID),"group_3_configured":bool(GROUP_3_ID),
        "webhook":"configured" if BOT_TOKEN else "missing BOT_TOKEN"
    })


@app.route("/api/admin/stats")
@require_admin
def admin_stats():
    c=conn()
    def n(sql): return c.execute(sql).fetchone()[0]
    from zoneinfo import ZoneInfo
    day=datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    return jsonify(ok=True,stats={
      "users":n("SELECT COUNT(*) FROM users"),
      "hosts":n("SELECT COUNT(*) FROM hosts WHERE approved=1 AND dummy=0"),
      "pending_hosts":n("SELECT COUNT(*) FROM hosts WHERE approved=0 AND dummy=0"),
      "bookings":n("SELECT COUNT(*) FROM bookings"),
      "recharges":n("SELECT COUNT(*) FROM recharge_requests WHERE status='pending'"),
      "withdrawals":n("SELECT COUNT(*) FROM withdrawals WHERE status='pending'"),
      "calls":n("SELECT COUNT(*) FROM bookings WHERE status='completed'"),
      "today_users":n("SELECT COUNT(*) FROM users WHERE substr(created_at,1,10)=?",(day,)),
      "today_recharge_count":n("SELECT COUNT(*) FROM recharge_requests WHERE substr(created_at,1,10)=? AND status='approved'",(day,)),
      "today_recharge_amount":n("SELECT COALESCE(SUM(amount),0) FROM recharge_requests WHERE substr(created_at,1,10)=? AND status='approved'",(day,)),
      "today_hosts_approved":n("SELECT COUNT(*) FROM hosts WHERE substr(COALESCE(approved_at,created_at),1,10)=? AND approved=1 AND dummy=0",(day,)),
      "today_host_earnings":n("SELECT COALESCE(SUM(CAST(tokens*0.60 AS INTEGER)),0) FROM bookings WHERE substr(call_ended_at,1,10)=? AND status='completed'",(day))
    })


@app.route("/api/admin/hosts")
@require_admin
def admin_hosts():
    rows=conn().execute("""SELECT h.*,u.telegram_id,u.username FROM hosts h JOIN users u ON u.id=h.user_id ORDER BY h.approved ASC,h.id DESC""").fetchall()
    return jsonify(ok=True,hosts=[dict(x) for x in rows])


@app.route("/api/admin/host/<int:hid>", methods=["POST"])
@require_admin
def admin_host(hid):
    d=request.get_json(silent=True) or {}; decision=d.get("decision")
    if decision not in ("approve","reject"):return jsonify(ok=False,error="Invalid decision"),400
    h=conn().execute("SELECT h.*,u.telegram_id FROM hosts h JOIN users u ON u.id=h.user_id WHERE h.id=?",(hid,)).fetchone()
    if not h:return jsonify(ok=False,error="Host not found"),404
    approved=1 if decision=="approve" else 0
    approved_at=now() if approved else ""
    conn().execute("UPDATE hosts SET approved=?,approved_at=? WHERE id=?",(approved,approved_at,hid)); conn().commit()
    msg=f"🎙️ <b>HOST {'APPROVED' if approved else 'REJECTED'}</b>\nHost: {h['name']}\nHost ID: {hid}\nAdmin: {g.user['telegram_id']}"
    tg_send(h["telegram_id"],msg); notify_group(GROUP_3_ID,msg); log_event("host_approval",msg)
    return jsonify(ok=True)


@app.route("/api/admin/recharges")
@require_admin
def admin_recharges():
    rows=conn().execute("""SELECT r.*,u.name,u.username,u.telegram_id FROM recharge_requests r JOIN users u ON u.id=r.user_id ORDER BY r.id DESC""").fetchall()
    return jsonify(ok=True,recharges=[dict(x) for x in rows])


@app.route("/api/admin/recharge/<int:rid>", methods=["POST"])
@require_admin
def admin_recharge(rid):
    d=request.get_json(silent=True) or {}; decision=d.get("decision")
    if decision not in ("approve","reject"):return jsonify(ok=False,error="Invalid decision"),400
    c=conn(); r=c.execute("SELECT * FROM recharge_requests WHERE id=?",(rid,)).fetchone()
    if not r or r["status"]!="pending":return jsonify(ok=False,error="Request unavailable"),404
    c.execute("UPDATE recharge_requests SET status=? WHERE id=?",(decision,rid))
    if decision=="approve": c.execute("UPDATE users SET wallet=wallet+? WHERE id=?",(r["tokens"],r["user_id"]))
    path=r["screenshot_path"]
    c.execute("UPDATE recharge_requests SET screenshot_path='' WHERE id=?",(rid,)); c.commit()
    if path and os.path.exists(path):
        try: os.remove(path)
        except OSError: pass
    u=c.execute("SELECT * FROM users WHERE id=?",(r["user_id"],)).fetchone()
    if u: notify_user(u["id"],"💰 Recharge Approved" if decision=="approve" else "❌ Recharge Rejected",f"Recharge #{rid}. Tokens: {r['tokens'] if decision=='approve' else 0}","recharge")
    msg=f"💰 Recharge #{rid} {decision}\nAmount: ₹{r['amount']}\nTokens: {r['tokens']}"
    notify_group(GROUP_3_ID,msg); log_event("recharge_decision",msg)
    return jsonify(ok=True)


@app.route("/api/admin/withdrawals")
@require_admin
def admin_withdrawals():
    rows=conn().execute("""SELECT w.*,h.name host_name,u.telegram_id FROM withdrawals w JOIN hosts h ON h.id=w.host_id JOIN users u ON u.id=h.user_id ORDER BY w.id DESC""").fetchall()
    return jsonify(ok=True,withdrawals=[dict(x) for x in rows])


@app.route("/api/admin/withdrawal/<int:wid>", methods=["POST"])
@require_admin
def admin_withdrawal(wid):
    d=request.get_json(silent=True) or {}; decision=d.get("decision")
    if decision not in ("approve","reject"):return jsonify(ok=False,error="Invalid decision"),400
    c=conn(); w=c.execute("SELECT * FROM withdrawals WHERE id=?",(wid,)).fetchone()
    if not w or w["status"]!="pending":return jsonify(ok=False,error="Unavailable"),404
    c.execute("UPDATE withdrawals SET status=? WHERE id=?",(decision,wid))
    if decision=="reject": c.execute("UPDATE hosts SET balance=balance+? WHERE id=?",(w["amount"],w["host_id"]))
    c.commit(); return jsonify(ok=True)


@app.route("/api/admin/users")
@require_admin
def admin_users():
    rows=conn().execute("SELECT * FROM users ORDER BY id DESC LIMIT 500").fetchall()
    return jsonify(ok=True,users=[dict(x) for x in rows])


@app.route("/api/admin/user/<int:uid>/status", methods=["POST"])
@require_admin
def admin_user_status(uid):
    d=request.get_json(silent=True) or {}; status=d.get("status")
    if status not in ("active","blocked","banned"):return jsonify(ok=False,error="Invalid status"),400
    conn().execute("UPDATE users SET status=? WHERE id=?",(status,uid)); conn().commit()
    return jsonify(ok=True)


@app.route("/api/admin/user/<int:uid>/tokens", methods=["POST"])
@require_admin
def admin_tokens(uid):
    d=request.get_json(silent=True) or {}
    try: amount=int(d.get("amount",0))
    except: amount=0
    mode=d.get("mode","add")
    if mode not in ("add","remove","set"):return jsonify(ok=False,error="Invalid mode"),400
    c=conn()
    if mode=="add": c.execute("UPDATE users SET wallet=wallet+? WHERE id=?",(amount,uid))
    elif mode=="remove": c.execute("UPDATE users SET wallet=MAX(wallet-?,0) WHERE id=?",(amount,uid))
    else: c.execute("UPDATE users SET wallet=MAX(?,0) WHERE id=?",(amount,uid))
    c.commit(); return jsonify(ok=True)


@app.route("/api/admin/announce", methods=["POST"])
@require_admin
def announce():
    text=(request.get_json(silent=True) or {}).get("text","").strip()
    if not text:return jsonify(ok=False,error="Message required"),400
    results=[notify_group(x,f"📢 <b>VYNORA LIVE ANNOUNCEMENT</b>\n\n{text}") for x in (GROUP_1_ID,GROUP_2_ID,GROUP_3_ID)]
    c=conn(); users=c.execute("SELECT id FROM users WHERE status='active'").fetchall()
    for u in users: notify_user(u["id"],"📢 Vynora Live Announcement",text,"announcement",telegram=False)
    log_event("announcement",text)
    return jsonify(ok=True,results=results,users_notified=len(users))


@app.route("/api/admin/group-test", methods=["POST"])
@require_admin
def group_test():
    text="🧪 <b>VYNORA LIVE TEST</b>\nGroup notification test successful.\nTime: "+now()
    results={"group1":notify_group(GROUP_1_ID,text),"group2":notify_group(GROUP_2_ID,text),"group3":notify_group(GROUP_3_ID,text)}
    return jsonify(ok=True,results=results)

@app.route("/api/admin/events")
@require_admin
def admin_events():
    rows=conn().execute("SELECT * FROM events ORDER BY id DESC LIMIT 200").fetchall()
    return jsonify(ok=True,events=[dict(x) for x in rows])



@app.route("/api/admin/host/add", methods=["POST"])
@require_admin
def admin_add_host():
    name=request.form.get("name","").strip()
    country=request.form.get("country","India").strip()
    bio=request.form.get("bio","").strip()
    dummy=1 if request.form.get("dummy")=="1" else 0
    file=request.files.get("photo")
    if not name:return jsonify(ok=False,error="Name required"),400
    if not dummy and country.lower()!="india":return jsonify(ok=False,error="Real host must be India"),400
    photo=""
    if file:
        try: photo,_=upload_image(file)
        except ValueError as e:return jsonify(ok=False,error=str(e)),400
    # Create an internal user record for dummy hosts if necessary.
    c=conn()
    fake_tid=-int(secrets.randbelow(900000000)+100000000)
    c.execute("INSERT INTO users(telegram_id,name,country,role,created_at) VALUES(?,?,?,?,?)",
              (fake_tid,name,country,"host",now()))
    uid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("""INSERT INTO hosts(user_id,name,photo_url,country,bio,approved,dummy,created_at)
                 VALUES(?,?,?,?,?,?,?,?)""",(uid,name,photo,country,bio,1,dummy,now()))
    c.commit()
    return jsonify(ok=True)


@app.route("/api/admin/host/<int:hid>/remove", methods=["POST"])
@require_admin
def admin_remove_host(hid):
    c=conn(); h=c.execute("SELECT user_id FROM hosts WHERE id=?",(hid,)).fetchone()
    if not h:return jsonify(ok=False,error="Host not found"),404
    c.execute("DELETE FROM hosts WHERE id=?",(hid,))
    c.execute("DELETE FROM users WHERE id=? AND telegram_id<0",(h["user_id"],))
    c.commit(); return jsonify(ok=True)


# ---------------- TELEGRAM WEBHOOK: BOTH PATHS ----------------
@app.route("/api/telegram/webhook", methods=["POST"])
@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    update=request.get_json(silent=True) or {}
    m=update.get("message") or {}
    u=m.get("from") or {}
    chat=m.get("chat") or {}
    text=(m.get("text") or "").strip()
    if not u.get("id"):return jsonify(ok=True)
    tid=int(u["id"]); name=(u.get("first_name","")+" "+u.get("last_name","")).strip() or "Telegram User"; username=u.get("username","")
    c=conn(); row=c.execute("SELECT * FROM users WHERE telegram_id=?",(tid,)).fetchone()
    if not row:
        role="admin" if tid in SUPER_ADMINS else "user"
        c.execute("INSERT INTO users(telegram_id,username,name,role,created_at) VALUES(?,?,?,?,?)",(tid,username,name,role,now())); c.commit()
        notify_group(GROUP_2_ID,f"🆕 <b>NEW USER</b>\nName: {name}\nUsername: @{username}\nUser ID: {tid}\nCountry: India\nTime: {now()}")
        log_event("new_user",f"Telegram registration {tid}")
    if text=="/start":
        markup={"inline_keyboard":[[{"text":"🚀 Open Vynora Live","web_app":{"url":WEB_APP_URL}}]]}
        tg_send(chat.get("id",tid),
                "❤️ <b>Welcome to Vynora Live</b>\n\n"
                "🇮🇳 India-based Real Hosts\n📅 Slot Booking\n📞 Scheduled 1-to-1 Calls\n🎁 Gifts\n💰 Wallet & Recharge\n\n"
                "नीचे button से Vynora Live खोलें।",markup)
    elif tid in SUPER_ADMINS and text=="/helpadmin":
        tg_send(tid,"👑 <b>VYNORA LIVE — VERIFIED SUPER ADMIN</b>\n\n/helpadmin\n/approvehost [host_id]\n/rejecthost [host_id]\n/addhost\n/removehost [host_id]\n/ban [user_id]\n/unban [user_id]\n/block [user_id]\n/unblock [user_id]\n/user [telegram_id]\n/addtoken [user_id] [amount]\n/removetoken [user_id] [amount]\n/settoken [user_id] [amount]\n/approverecharge [id]\n/rejectrecharge [id]\n/announce <text>\n/offer <text>\n/banner <text>\n/clearannouncement\n/callhost [user_id]\n/setcountry [user_id] [country]\n/stats\n/group_test")
    elif tid in SUPER_ADMINS and text=="/stats":
        c=conn(); from zoneinfo import ZoneInfo; day=datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        vals={
          "users":c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
          "hosts":c.execute("SELECT COUNT(*) FROM hosts WHERE approved=1 AND dummy=0").fetchone()[0],
          "today_users":c.execute("SELECT COUNT(*) FROM users WHERE substr(created_at,1,10)=?",(day,)).fetchone()[0],
          "today_hosts":c.execute("SELECT COUNT(*) FROM hosts WHERE substr(COALESCE(approved_at,created_at),1,10)=? AND approved=1 AND dummy=0",(day,)).fetchone()[0],
          "today_recharge":c.execute("SELECT COALESCE(SUM(amount),0) FROM recharge_requests WHERE substr(created_at,1,10)=? AND status='approved'",(day,)).fetchone()[0],
          "today_host_earn":c.execute("SELECT COALESCE(SUM(CAST(tokens*0.60 AS INTEGER)),0) FROM bookings WHERE substr(call_ended_at,1,10)=? AND status='completed'",(day,)).fetchone()[0],
        }
        tg_send(tid,f"📊 <b>Vynora Live Stats — {day}</b>\nTotal users: {vals['users']}\nTotal approved hosts: {vals['hosts']}\nNew users today: {vals['today_users']}\nHosts approved today: {vals['today_hosts']}\nRecharge today: ₹{vals['today_recharge']}\nHost earnings today: {vals['today_host_earn']}")
    elif tid in SUPER_ADMINS and text=="/group_test":
        res=[notify_group(gid,"🧪 <b>VYNORA LIVE GROUP TEST</b>\nGroup notification system is working.") for gid in (GROUP_1_ID,GROUP_2_ID,GROUP_3_ID)]
        tg_send(tid,f"Group test: G1={'OK' if res[0] else 'FAIL'} · G2={'OK' if res[1] else 'FAIL'} · G3={'OK' if res[2] else 'FAIL'}")
    elif tid in SUPER_ADMINS and text.startswith("/approvehost "):
        try: hid=int(text.split()[1])
        except: hid=0
        h=conn().execute("SELECT h.*,u.telegram_id FROM hosts h JOIN users u ON u.id=h.user_id WHERE h.id=?",(hid,)).fetchone()
        if h:
            conn().execute("UPDATE hosts SET approved=1,approved_at=? WHERE id=?",(now(),hid)); conn().commit()
            msg=f"🎙️ <b>HOST APPROVED</b>\nHost: {h['name']}\nHost ID: {hid}\nAdmin: {tid}\nTime: {now()}"
            tg_send(h['telegram_id'],msg); notify_group(GROUP_3_ID,msg); tg_send(tid,"✅ Host approved")
        else: tg_send(tid,"❌ Host not found")
    elif tid in SUPER_ADMINS and text.startswith("/rejecthost "):
        try: hid=int(text.split()[1])
        except: hid=0
        h=conn().execute("SELECT h.*,u.telegram_id FROM hosts h JOIN users u ON u.id=h.user_id WHERE h.id=?",(hid,)).fetchone()
        if h:
            conn().execute("UPDATE hosts SET approved=0,approved_at='' WHERE id=?",(hid,)); conn().commit()
            msg=f"❌ <b>HOST REJECTED</b>\nHost: {h['name']}\nHost ID: {hid}\nAdmin: {tid}"
            tg_send(h['telegram_id'],msg); notify_group(GROUP_3_ID,msg); tg_send(tid,"✅ Host rejected")
        else: tg_send(tid,"❌ Host not found")
    elif tid in SUPER_ADMINS and text.startswith(("/ban ","/block ","/unban ","/unblock ")):
        parts=text.split(); cmd=parts[0]; target=parts[1] if len(parts)>1 else ""
        try: uid=int(target)
        except: uid=0
        status="blocked" if cmd in ("/ban","/block") else "active"
        c=conn(); row=c.execute("SELECT * FROM users WHERE telegram_id=? OR id=?",(uid,uid)).fetchone()
        if row:
            c.execute("UPDATE users SET status=? WHERE id=?",(status,row['id'])); c.commit(); tg_send(tid,f"✅ {cmd} applied to user #{row['id']}")
        else: tg_send(tid,"❌ User not found")
    elif tid in SUPER_ADMINS and text.startswith(("/addtoken ","/removetoken ","/settoken ")):
        parts=text.split()
        if len(parts)<3: tg_send(tid,"Usage: /addtoken user_id amount");
        else:
            try: uid=int(parts[1]); amount=max(0,int(parts[2]))
            except: uid=0; amount=0
            mode=parts[0][1:]
            c=conn(); row=c.execute("SELECT * FROM users WHERE telegram_id=? OR id=?",(uid,uid)).fetchone()
            if row:
                if mode=="addtoken": c.execute("UPDATE users SET wallet=wallet+? WHERE id=?",(amount,row['id']))
                elif mode=="removetoken": c.execute("UPDATE users SET wallet=MAX(wallet-?,0) WHERE id=?",(amount,row['id']))
                else: c.execute("UPDATE users SET wallet=? WHERE id=?",(amount,row['id']))
                c.commit(); tg_send(tid,"✅ Token balance updated")
            else: tg_send(tid,"❌ User not found")
    elif tid in SUPER_ADMINS and text.startswith(("/approverecharge ","/rejectrecharge ")):
        parts=text.split()
        try: rid=int(parts[1])
        except: rid=0
        decision="approve" if parts[0]=="/approverecharge" else "reject"
        c=conn(); r=c.execute("SELECT * FROM recharge_requests WHERE id=? AND status='pending'",(rid,)).fetchone()
        if r:
            c.execute("UPDATE recharge_requests SET status=?,screenshot_path='' WHERE id=?",(decision,rid))
            if decision=="approve": c.execute("UPDATE users SET wallet=wallet+? WHERE id=?",(r['tokens'],r['user_id']))
            c.commit(); path=r['screenshot_path']
            if path and os.path.exists(path):
                try: os.remove(path)
                except OSError: pass
            notify_user(r['user_id'],"💰 Recharge Approved" if decision=="approve" else "❌ Recharge Rejected",f"Recharge #{rid} processed. Tokens: {r['tokens'] if decision=='approve' else 0}","recharge")
            notify_group(GROUP_3_ID,f"💰 Recharge #{rid} {decision}\nAmount: ₹{r['amount']}\nTokens: {r['tokens']}")
            tg_send(tid,"✅ Recharge processed")
        else: tg_send(tid,"❌ Recharge not found/pending")
    elif tid in SUPER_ADMINS and text.startswith("/user "):
        try: target=int(text.split()[1])
        except: target=0
        urow=conn().execute("SELECT * FROM users WHERE telegram_id=? OR id=?",(target,target)).fetchone()
        tg_send(tid, (f"👤 <b>User</b>\nName: {urow['name']}\nUsername: @{urow['username']}\nTelegram ID: {urow['telegram_id']}\nWallet: {urow['wallet']}\nStatus: {urow['status']}" if urow else "❌ User not found"))
    elif tid in SUPER_ADMINS and text.startswith("/setcountry "):
        parts=text.split(maxsplit=2)
        if len(parts)<3: tg_send(tid,"Usage: /setcountry user_id country")
        else:
            try: target=int(parts[1])
            except: target=0
            urow=conn().execute("SELECT * FROM users WHERE telegram_id=? OR id=?",(target,target)).fetchone()
            if urow:
                conn().execute("UPDATE users SET country=? WHERE id=?",(parts[2].strip(),urow['id'])); conn().commit(); tg_send(tid,"✅ Country updated")
            else: tg_send(tid,"❌ User not found")
    elif tid in SUPER_ADMINS and text.startswith("/announce "):
        body=text[len("/announce "):].strip()
        if body:
            for gid in (GROUP_1_ID,GROUP_2_ID,GROUP_3_ID): notify_group(gid,f"📢 <b>VYNORA LIVE ANNOUNCEMENT</b>\n\n{body}")
            tg_send(tid,"✅ Announcement sent to all configured groups.")
    elif tid in SUPER_ADMINS and text.startswith("/offer "):
        body=text[len("/offer "):].strip()
        for gid in (GROUP_1_ID,GROUP_2_ID,GROUP_3_ID): notify_group(gid,f"🎁 <b>VYNORA LIVE OFFER</b>\n\n{body}")
        tg_send(tid,"✅ Offer sent to all configured groups.")
    elif tid in SUPER_ADMINS and text.startswith("/banner "):
        body=text[len("/banner "):].strip()
        if body:
            set_setting("banner_text",body)
            tg_send(tid,"✅ Home banner text updated.")
    elif tid in SUPER_ADMINS and text=="/clearannouncement":
        set_setting("banner_text",""); set_setting("banner_image",""); tg_send(tid,"✅ Home banner cleared.")
    return jsonify(ok=True)


# ---------------- WEBRTC SIGNALING ----------------
@socketio.on("join_call")
def ws_join(data):
    data=data or {}
    room=str(data.get("booking_id","") or "")
    if data.get("user_room"): join_room(str(data["user_room"]))
    if data.get("host_room"): join_room(str(data["host_room"]))
    if not room:
        return
    join_room(room)
    peers=CALL_PEERS.setdefault(room,set())
    peers.add(request.sid)
    # Only the already-connected peer creates the WebRTC offer.
    if len(peers) >= 2:
        emit("peer_joined",{"booking_id":room},room=room,include_self=False)
        # Start the authoritative timer only after two participants are connected.
        try:
            bid=int(room)
            b=conn().execute("SELECT * FROM bookings WHERE id=?",(bid,)).fetchone()
            if b and not b["call_started_at"] and b["status"] in ("scheduled","ringing","accepted","in_call"):
                started=now()
                conn().execute("UPDATE bookings SET call_started_at=?,status='in_call' WHERE id=? AND call_started_at=''",(started,bid))
                conn().commit()
                emit("server_timer_started",{"booking_id":bid,"started_at":started,"duration":b["duration"]},room=room)
        except (ValueError,TypeError):
            # Admin/free calls use non-numeric rooms and have no booking timer.
            emit("call_ready",{"room":room},room=room)


@socketio.on("webrtc_signal")
def ws_signal(data):
    room=str((data or {}).get("booking_id",""))
    if room: emit("webrtc_signal",data,room=room,include_self=False)


@socketio.on("call_started")
def ws_started(data):
    # Kept for backwards compatibility with older clients. The current client
    # starts the timer from ws_join only after both participants are connected.
    return

@socketio.on("disconnect")
def ws_disconnect():
    for room, peers in list(CALL_PEERS.items()):
        peers.discard(request.sid)
        if not peers:
            CALL_PEERS.pop(room, None)


@socketio.on("call_ended")
def ws_ended(data):
    try: bid=int(data.get("booking_id"))
    except: return
    c=conn(); b=c.execute("SELECT * FROM bookings WHERE id=?",(bid,)).fetchone()
    if not b or not b["call_started_at"] or b["status"]=="completed":return
    ended=now(); earning=int(b["tokens"]*.60)
    c.execute("UPDATE bookings SET call_ended_at=?,status='completed' WHERE id=?",(ended,bid))
    c.execute("UPDATE hosts SET balance=balance+? WHERE id=?",(earning,b["host_id"])); c.commit()
    h=c.execute("SELECT name FROM hosts WHERE id=?",(b["host_id"],)).fetchone()
    u=c.execute("SELECT telegram_id,name FROM users WHERE id=?",(b["user_id"],)).fetchone()
    msg=f"📞 <b>SESSION COMPLETED</b>\nHost: {h['name'] if h else ''}\nUser: {u['name'] if u else ''}\nBooking ID: {bid}\nDuration: {b['duration']} min\nStart: {b['call_started_at']}\nEnd: {ended}\nTokens: {b['tokens']}\nHost earning: {earning}\nStatus: completed"
    log_event("call_completed",msg); notify_group(GROUP_3_ID,msg)
    if u: tg_send(u["telegram_id"],f"📞 Call completed.\nBooking #{bid}\nDuration: {b['duration']} min")
    emit("call_completed",{"booking_id":bid,"ended_at":ended},room=str(bid))



def parse_schedule(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def finish_booking(booking_id, reason="duration_complete"):
    with app.app_context():
        c=conn()
        b=c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()






