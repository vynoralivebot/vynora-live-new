
import os
import hmac
import hashlib
import sqlite3
import json
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import requests
from flask import Flask, request, jsonify, send_from_directory, g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vynora_stage1.db")

app = Flask(__name__, static_folder=".", static_url_path="")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://vynora-live-new.onrender.com").strip().rstrip("/")
REQUIRE_TELEGRAM_AUTH = os.getenv("REQUIRE_TELEGRAM_AUTH", "1").strip() != "0"

SUPER_ADMINS = {7778606261, 7001825467}


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    if not hasattr(g, "db"):
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error):
    c = getattr(g, "db", None)
    if c:
        c.close()


def init_db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        username TEXT DEFAULT '',
        name TEXT DEFAULT '',
        photo_url TEXT DEFAULT '',
        country TEXT DEFAULT 'India',
        wallet INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT DEFAULT '',
        photo_url TEXT DEFAULT '',
        country TEXT DEFAULT 'India',
        bio TEXT DEFAULT '',
        online INTEGER DEFAULT 0,
        approved INTEGER DEFAULT 1,
        dummy INTEGER DEFAULT 0,
        rate_1m INTEGER DEFAULT 20,
        created_at TEXT NOT NULL
    );
    """)
    c.commit()

    # No foreign/dummy demo hosts in Stage 1.
    # Keep only India hosts visible to users.
    c.execute("UPDATE hosts SET approved=1 WHERE country='India' AND dummy=0")
    c.commit()
    c.close()


def validate_init_data(init_data: str):
    """Validate Telegram Mini App initData using BOT_TOKEN."""
    if not init_data or not BOT_TOKEN:
        return None

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None

        data_check = "\n".join(
            f"{k}={pairs[k]}" for k in sorted(pairs.keys())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated = hmac.new(
            secret_key,
            data_check.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated, received_hash):
            return None

        user_raw = pairs.get("user", "{}")
        return json.loads(user_raw)
    except Exception:
        return None


def telegram_user():
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        init_data = request.args.get("initData", "")

    user = validate_init_data(init_data)

    if not user:
        if REQUIRE_TELEGRAM_AUTH:
            return None
        # Development-only fallback when REQUIRE_TELEGRAM_AUTH=0
        return {
            "id": 0,
            "first_name": "Test",
            "last_name": "User",
            "username": "testuser"
        }

    return user


def current_user():
    tg = telegram_user()
    if not tg:
        return None

    tid = int(tg["id"])
    username = tg.get("username", "")
    name = " ".join(
        x for x in [tg.get("first_name", ""), tg.get("last_name", "")]
        if x
    ).strip() or "Telegram User"

    c = db()
    row = c.execute(
        "SELECT * FROM users WHERE telegram_id=?",
        (tid,)
    ).fetchone()

    if not row:
        c.execute("""
            INSERT INTO users
            (telegram_id, username, name, country, created_at, updated_at)
            VALUES (?, ?, ?, 'India', ?, ?)
        """, (tid, username, name, now(), now()))
        c.commit()
        row = c.execute(
            "SELECT * FROM users WHERE telegram_id=?",
            (tid,)
        ).fetchone()
    else:
        c.execute("""
            UPDATE users
            SET username=?, name=?, updated_at=?
            WHERE telegram_id=?
        """, (username, name, now(), tid))
        c.commit()
        row = c.execute(
            "SELECT * FROM users WHERE telegram_id=?",
            (tid,)
        ).fetchone()

    return row


def require_user():
    user = current_user()
    if not user:
        return None, (jsonify(ok=False, error="Telegram authentication required"), 401)
    if user["status"] != "active":
        return None, (jsonify(ok=False, error="Your account is not active"), 403)
    return user, None


def bot_send(chat_id, text, reply_markup=None):
    if not BOT_TOKEN:
        return False
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10
        )
        return bool(r.ok and r.json().get("ok"))
    except Exception:
        return False


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/health")
def health():
    return jsonify(ok=True, service="vynora-live-stage1")


@app.get("/api/me")
def api_me():
    user, error = require_user()
    if error:
        return error

    return jsonify(
        ok=True,
        user={
            "id": user["id"],
            "telegram_id": user["telegram_id"],
            "username": user["username"],
            "name": user["name"],
            "photo_url": user["photo_url"],
            "country": user["country"],
            "wallet": user["wallet"],
        }
    )


@app.get("/api/hosts")
def api_hosts():
    user, error = require_user()
    if error:
        return error

    rows = db().execute("""
        SELECT id, name, username, photo_url, country, bio, online, rate_1m
        FROM hosts
        WHERE approved=1 AND dummy=0 AND LOWER(country)='india'
        ORDER BY online DESC, id DESC
    """).fetchall()

    hosts = []
    for h in rows:
        hosts.append({
            "id": h["id"],
            "name": h["name"],
            "username": h["username"],
            "photo_url": h["photo_url"],
            "country": h["country"],
            "bio": h["bio"],
            "online": bool(h["online"]),
            "rate_1m": h["rate_1m"],
        })

    return jsonify(ok=True, hosts=hosts)


@app.get("/api/host/<int:host_id>")
def api_host(host_id):
    user, error = require_user()
    if error:
        return error

    row = db().execute("""
        SELECT id, name, username, photo_url, country, bio, online, rate_1m
        FROM hosts
        WHERE id=? AND approved=1 AND dummy=0 AND LOWER(country)='india'
    """, (host_id,)).fetchone()

    if not row:
        return jsonify(ok=False, error="Host not found"), 404

    return jsonify(ok=True, host=dict(row, online=bool(row["online"])))


@app.post("/api/admin/host")
def admin_add_host():
    tg = telegram_user()
    if not tg or int(tg["id"]) not in SUPER_ADMINS:
        return jsonify(ok=False, error="Admin authentication required"), 403

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    country = str(data.get("country", "India")).strip() or "India"

    if not name:
        return jsonify(ok=False, error="Host name required"), 400
    if country.lower() != "india":
        return jsonify(ok=False, error="Only India-based real hosts are allowed in Stage 1"), 400

    c = db()
    cur = c.execute("""
        INSERT INTO hosts
        (name, username, photo_url, country, bio, online, approved, dummy, rate_1m, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
    """, (
        name,
        str(data.get("username", "")).strip(),
        str(data.get("photo_url", "")).strip(),
        country,
        str(data.get("bio", "")).strip(),
        1 if data.get("online") else 0,
        int(data.get("rate_1m", 20)),
        now()
    ))
    c.commit()
    return jsonify(ok=True, host_id=cur.lastrowid)


@app.post("/api/admin/host/<int:host_id>/online")
def admin_host_online(host_id):
    tg = telegram_user()
    if not tg or int(tg["id"]) not in SUPER_ADMINS:
        return jsonify(ok=False, error="Admin authentication required"), 403

    data = request.get_json(silent=True) or {}
    online = 1 if data.get("online") else 0
    cur = db().execute(
        "UPDATE hosts SET online=? WHERE id=? AND dummy=0 AND LOWER(country)='india'",
        (online, host_id)
    )
    db().commit()
    if cur.rowcount == 0:
        return jsonify(ok=False, error="Host not found"), 404
    return jsonify(ok=True)


@app.post("/api/telegram/webhook")
@app.post("/telegram/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message") or {}
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()

    if not sender.get("id"):
        return jsonify(ok=True)

    tid = int(sender["id"])
    name = " ".join(
        x for x in [sender.get("first_name", ""), sender.get("last_name", "")]
        if x
    ).strip() or "Telegram User"
    username = sender.get("username", "")

    c = db()
    existing = c.execute(
        "SELECT id FROM users WHERE telegram_id=?",
        (tid,)
    ).fetchone()

    if not existing:
        c.execute("""
            INSERT INTO users
            (telegram_id, username, name, country, created_at, updated_at)
            VALUES (?, ?, ?, 'India', ?, ?)
        """, (tid, username, name, now(), now()))
        c.commit()

    if text == "/start":
        markup = {
            "inline_keyboard": [[
                {
                    "text": "🚀 Open Vynora Live",
                    "web_app": {"url": WEB_APP_URL}
                }
            ]]
        }
        bot_send(
            chat.get("id", tid),
            "❤️ <b>Welcome to Vynora Live</b>\n\n"
            "🇮🇳 India-based Real Hosts\n"
            "📅 Slot Booking\n"
            "📞 1-to-1 Calls\n"
            "🎁 Gifts\n"
            "💰 Wallet & Recharge\n\n"
            "नीचे button दबाकर Vynora Live खोलें।",
            markup
        )

    return jsonify(ok=True)


def set_webhook():
    if not BOT_TOKEN:
        print("BOT_TOKEN missing; webhook not configured.")
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
        r = requests.post(
            url,
            json={"url": f"{WEB_APP_URL}/api/telegram/webhook"},
            timeout=15
        )
        print("Telegram webhook:", r.json())
    except Exception as e:
        print("Webhook setup warning:", e)


if __name__ == "__main__":
    init_db()
    set_webhook()
    port = int(os.getenv("PORT", "10000"))
    print(f"❤️ VYNORA LIVE STAGE 1 STARTED on {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
ify(ok=False,error="Message required"),400
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
        if not b or not b["call_started_at"] or b["status"]=="completed":
            return False
        ended=now()
        earning=int(b["tokens"]*0.60)






