"""portal.py -- login (User/Admin), vehicles, entry/exit, availability by vehicle type."""
import os, sqlite3, functools
from datetime import datetime, timedelta
import random
import secrets
from flask import Blueprint, jsonify, request, session, render_template, redirect
from werkzeug.security import generate_password_hash, check_password_hash
import predict as pred_module

bp = Blueprint("portal", __name__)
DB = os.path.join(os.path.dirname(__file__), "portal.db")
# Share of each lot's capacity assigned to each vehicle zone (edit as needed)
ZONE_SHARE = {"Car": 0.70, "SUV": 0.20, "Bike": 0.10}


def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE,
            pw TEXT, role TEXT, full_name TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS vehicles(id INTEGER PRIMARY KEY, user_id INT, plate TEXT UNIQUE,
            vtype TEXT, make_model TEXT, color TEXT);
        CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, vehicle_id INT, user_id INT,
            lot TEXT, entry_time TEXT, exit_time TEXT);
        CREATE TABLE IF NOT EXISTS complaints(id INTEGER PRIMARY KEY, user_id INT, lot TEXT, category TEXT,
            plate TEXT, message TEXT, status TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, user_id INT, vehicle_id INT, lot TEXT, plate TEXT,
            vtype TEXT, make_model TEXT, color TEXT, book_date TEXT, book_time TEXT, amount REAL, payment_method TEXT,
            payment_id TEXT, status TEXT, created TEXT, decided_at TEXT);""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        for col in ("phone", "email", "lot"):
            if col not in cols: c.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        if not c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            c.execute("INSERT INTO users(username,pw,role,full_name,created) VALUES(?,?,?,?,?)",
                      (os.environ.get("ADMIN_ID", "ADMIN001"), generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123")),
                       "admin", "Administrator", datetime.now().isoformat()))


def login_required(role=None):
    def deco(f):
        @functools.wraps(f)
        def w(*a, **k):
            if "uid" not in session: return jsonify(error="Login required"), 401
            if role and session.get("role") != role: return jsonify(error="Forbidden"), 403
            return f(*a, **k)
        return w
    return deco


@bp.route("/login")
def login_page():
    return redirect("/") if "uid" in session else render_template("login.html")


@bp.route("/portal")
def page():
    return render_template("portal.html") if "uid" in session else redirect("/login")


@bp.post("/api/register")
def register():
    d = request.json or {}
    u, p = (d.get("username") or "").strip(), d.get("password") or ""
    name, phone = (d.get("full_name") or "").strip(), (d.get("phone") or "").strip()
    plate, vt = (d.get("plate") or "").strip().upper(), d.get("vtype")
    if len(u) < 3 or len(p) < 6: return jsonify(error="Username >=3 and password >=6 chars"), 400
    if not name or not phone: return jsonify(error="Full name and phone are required"), 400
    if not plate or vt not in ZONE_SHARE: return jsonify(error="Vehicle plate and type (Car/Bike/SUV) are required"), 400
    try:
        with db() as c:
            cur = c.execute("INSERT INTO users(username,pw,role,full_name,phone,email,created) VALUES(?,?,?,?,?,?,?)",
                            (u, generate_password_hash(p), "user", name, phone, d.get("email", ""), datetime.now().isoformat()))
            c.execute("INSERT INTO vehicles(user_id,plate,vtype,make_model,color) VALUES(?,?,?,?,?)",
                      (cur.lastrowid, plate, vt, d.get("make_model", ""), d.get("color", "")))
    except sqlite3.IntegrityError:
        return jsonify(error="Username or vehicle plate already registered"), 409
    return jsonify(ok=True)


@bp.post("/api/login")
def login():
    d = request.json or {}
    with db() as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (d.get("username", ""),)).fetchone()
    if not r or not check_password_hash(r["pw"], d.get("password", "")):
        return jsonify(error="Invalid credentials"), 401
    if d.get("role") and d["role"] != r["role"]:
        return jsonify(error=f"This is not a{'n admin' if d['role']=='admin' else ' user'} account. Use the {r['role'].title()} login tab."), 403
    if r["role"] == "admin":  # require & save admin details after successful login
        lot, name = (d.get("lot") or "").strip(), (d.get("full_name") or "").strip()
        phone, email = (d.get("phone") or "").strip(), (d.get("email") or "").strip()
        if lot not in pred_module.LOT_ID_MAP or not name or not phone or not email:
            return jsonify(error="Parking lot, name, phone and email are required"), 400
        with db() as c:
            c.execute("UPDATE users SET full_name=?, phone=?, email=?, lot=? WHERE id=?", (name, phone, email, lot, r["id"]))
        session.update(lot=lot); r = dict(r, full_name=name)
    session.update(uid=r["id"], role=r["role"], name=r["full_name"])
    return jsonify(role=r["role"], name=r["full_name"])


@bp.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=True)


@bp.get("/api/me")
def me():
    if "uid" not in session:
        return jsonify(error="no"), 401
    with db() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    r = dict(r) if r else {}
    return jsonify(role=session.get("role"), name=session.get("name"), username=r.get("username"),
                   phone=r.get("phone"), email=r.get("email"), lot=r.get("lot"), created=r.get("created"))


@bp.get("/api/availability")
@login_required()
def availability():
    """Per-lot availability split by vehicle type, from real historical occupancy
    (dataset) + vehicles currently parked via this app."""
    now = datetime.now(); out = []
    with db() as c:
        live = {(r["lot"], r["vtype"]): r["n"] for r in c.execute(
            "SELECT s.lot, v.vtype, COUNT(*) n FROM sessions s JOIN vehicles v ON v.id=s.vehicle_id "
            "WHERE s.exit_time IS NULL GROUP BY s.lot, v.vtype")}
    for lot in pred_module.LOT_ID_MAP:
        p = pred_module.predict_availability(lot, now)
        occ = p["predicted_occupied_pct"] / 100
        types = {}
        for t, sh in ZONE_SHARE.items():
            cap = int(round(p["capacity"] * sh))
            types[t] = dict(capacity=cap, available=int(round(cap * (1 - occ))),
                            parked_via_app=live.get((lot, t), 0))
        out.append(dict(lot=lot, capacity=p["capacity"], occupied_pct=p["predicted_occupied_pct"],
                        available=p["estimated_available_spaces"], status=p["status"], types=types))
    return jsonify(out)


@bp.route("/api/vehicles", methods=["GET", "POST"])
@login_required("user")
def vehicles():
    with db() as c:
        if request.method == "POST":
            d = request.json or {}
            plate = (d.get("plate") or "").strip().upper()
            if d.get("vtype") not in ZONE_SHARE or not plate:
                return jsonify(error="Plate and type (Car/Bike/SUV) required"), 400
            try:
                c.execute("INSERT INTO vehicles(user_id,plate,vtype,make_model,color) VALUES(?,?,?,?,?)",
                          (session["uid"], plate, d["vtype"], d.get("make_model", ""), d.get("color", "")))
            except sqlite3.IntegrityError:
                return jsonify(error="Plate already registered"), 409
            return jsonify(ok=True)
        return jsonify([dict(r) for r in c.execute("SELECT * FROM vehicles WHERE user_id=?", (session["uid"],))])


@bp.post("/api/entry")
@login_required("user")
def entry():
    d = request.json or {}
    with db() as c:
        v = c.execute("SELECT * FROM vehicles WHERE id=? AND user_id=?", (d.get("vehicle_id"), session["uid"])).fetchone()
        if not v or d.get("lot") not in pred_module.LOT_ID_MAP: return jsonify(error="Invalid vehicle or lot"), 400
        if c.execute("SELECT 1 FROM sessions WHERE vehicle_id=? AND exit_time IS NULL", (v["id"],)).fetchone():
            return jsonify(error="Vehicle already parked"), 409
        c.execute("INSERT INTO sessions(vehicle_id,user_id,lot,entry_time) VALUES(?,?,?,?)",
                  (v["id"], session["uid"], d["lot"], datetime.now().isoformat(timespec="seconds")))
    return jsonify(ok=True)


@bp.get("/api/my_active")
@login_required("user")
def my_active():
    """The user's currently parked vehicles, so the Profile page can offer Exit Parking with a live fee estimate."""
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT s.id, s.lot, s.entry_time, v.id AS vehicle_id, v.plate, v.vtype FROM sessions s "
            "JOIN vehicles v ON v.id=s.vehicle_id WHERE s.user_id=? AND s.exit_time IS NULL", (session["uid"],))]
    now = datetime.now()
    for r in rows:
        hrs = (now - datetime.fromisoformat(r["entry_time"])).total_seconds() / 3600
        r["hours_so_far"] = round(hrs, 2)
        r["fee_so_far"] = round(hrs * _RATE.get(r["vtype"], 30))
        r["overstay"] = hrs > OVERSTAY_HOURS
    return jsonify(rows)


@bp.post("/api/exit")
@login_required("user")
def exit_():
    vid = (request.json or {}).get("vehicle_id")
    now = datetime.now()
    with db() as c:
        row = c.execute("SELECT s.*, v.vtype FROM sessions s JOIN vehicles v ON v.id=s.vehicle_id "
                        "WHERE s.vehicle_id=? AND s.user_id=? AND s.exit_time IS NULL", (vid, session["uid"])).fetchone()
        if not row:
            return jsonify(error="Vehicle not parked"), 400
        exit_iso = now.isoformat(timespec="seconds")
        c.execute("UPDATE sessions SET exit_time=? WHERE id=?", (exit_iso, row["id"]))
    hrs = (now - datetime.fromisoformat(row["entry_time"])).total_seconds() / 3600
    fee = round(hrs * _RATE.get(row["vtype"], 30))
    # parking space is released automatically: the vehicle now has exit_time set, so it moves to Exited History
    return jsonify(ok=True, entry_time=row["entry_time"], exit_time=exit_iso,
                   duration_hours=round(hrs, 2), fee=fee, overstay=hrs > OVERSTAY_HOURS)


_Q = ("SELECT s.id, s.lot, s.entry_time, s.exit_time, v.plate, v.vtype, v.make_model, v.color, "
      "u.username, u.full_name, u.phone FROM sessions s JOIN vehicles v ON v.id=s.vehicle_id "
      "JOIN users u ON u.id=s.user_id ")

OVERSTAY_HOURS = 4  # grace period before a still-parked vehicle counts as overstaying
_RATE = {"Bike": 20, "SUV": 70, "Van": 90, "Pickup": 90, "Bus": 150, "Truck": 150, "Bicycle": 10}


def _tag_overstay(rows, now):
    """Mark still-parked rows that have exceeded the grace period; add extra hours/fee owed."""
    for r in rows:
        extra = (now - datetime.fromisoformat(r["entry_time"])).total_seconds() / 3600 - OVERSTAY_HOURS
        r["overstay"] = (not r["exit_time"]) and extra > 0
        r["overstay_hours"] = round(extra, 1) if r["overstay"] else 0
        r["overstay_fee"] = round(r["overstay_hours"] * _RATE.get(r.get("vtype"), 30)) if r["overstay"] else 0
    return rows



@bp.get("/api/my_history")
@login_required("user")
def my_history():
    with db() as c:
        rows = [dict(r) for r in c.execute(_Q + "WHERE s.user_id=? ORDER BY s.entry_time DESC", (session["uid"],))]
    return jsonify(_tag_overstay(rows, datetime.now()))


def _admin_lot():
    lot = session.get("lot")
    if not lot:
        with db() as c:
            r = c.execute("SELECT lot FROM users WHERE id=?", (session["uid"],)).fetchone()
        lot = r["lot"] if r else None
    return lot


_FN = "Aarav Priya Rahul Ananya Vikram Sneha Arjun Kavya Rohan Meera Ishaan Neha Karan Divya Aditya Pooja Sanjay Riya Nikhil Shamika".split()
_LN = "Sharma Reddy Patel Nair Singh Gupta Rao Iyer Khan Mehta Verma Das Joshi Kumar Naidu Bose Shah Pillai Menon Yadav".split()
_LT = "ABCDEFGHJKLMNOPRSTUVWXY"


def _sample_rows(lot, n_parked, now):
    """Deterministic sample vehicles (stable within the hour) sized to the predicted occupancy."""
    rnd = random.Random(f"{lot}{now:%Y%m%d%H}")
    base = now.replace(minute=0, second=0, microsecond=0)
    types = ["Car"] * 70 + ["SUV"] * 20 + ["Bike"] * 10
    iso = lambda d: d.isoformat(timespec="seconds")
    rows = []
    for i in range(n_parked + min(300, n_parked // 5)):
        exited = i >= n_parked
        fn, ln = rnd.choice(_FN), rnd.choice(_LN)
        plate = f"{rnd.choice(_LT)}{rnd.choice(_LT)}{rnd.randint(10, 99)} {rnd.choice(_LT)}{rnd.choice(_LT)}{rnd.choice(_LT)}"
        vt, ex = rnd.choice(types), None
        if exited:
            ent = base - timedelta(minutes=rnd.randint(120, 720))
            ex = min(ent + timedelta(minutes=rnd.randint(20, 300)), base - timedelta(minutes=1))
            if n_parked and rnd.random() < 0.4:  # returning vehicle: earlier visit before its current stay
                src = rows[rnd.randrange(n_parked)]
                ex = min(ex, datetime.fromisoformat(src["entry_time"]) - timedelta(minutes=5))
                ent = min(ent, ex - timedelta(minutes=20))
                fn, ln = src["full_name"].split(" ", 1); plate, vt = src["plate"], src["vtype"]
        else:
            ent = base - timedelta(minutes=rnd.randint(0, 600), seconds=rnd.randint(0, 59))
        rows.append(dict(id=-(i + 1), lot=lot, entry_time=iso(ent), exit_time=iso(ex) if ex else None, plate=plate,
                         vtype=vt, make_model="", color="", username=f"{fn}{ln}".lower(),
                         full_name=f"{fn} {ln}", phone="", sample=True))
    return rows


@bp.get("/api/admin/overview")
@login_required("admin")
def admin_overview():
    lot, now = _admin_lot(), datetime.now()  # admin only sees their own selected lot
    with db() as c:
        real = [dict(r) for r in c.execute(_Q + "WHERE s.lot=? ORDER BY s.entry_time DESC LIMIT 500", (lot,))]
    p = pred_module.predict_availability(lot, now)
    cap = p["capacity"]; occ = cap - p["estimated_available_spaces"]
    real_parked = sum(1 for r in real if not r["exit_time"])
    log = sorted(_tag_overstay(real + _sample_rows(lot, max(0, occ - real_parked), now), now), key=lambda r: r["entry_time"], reverse=True)
    occ = max(occ, real_parked)
    overstay_n = sum(1 for r in log if r["overstay"])
    with db() as c:
        bk = [dict(r) for r in c.execute("SELECT b.*, u.full_name FROM bookings b JOIN users u ON u.id=b.user_id "
                                         "WHERE b.lot=? ORDER BY b.book_date DESC, b.book_time DESC LIMIT 200", (lot,))]
    return jsonify(lot=lot, capacity=cap, occupied=occ, available=cap - occ, occupied_pct=p["predicted_occupied_pct"],
                   status=p["status"], log=log, bookings=bk, overstay_n=overstay_n)


@bp.post("/api/admin/force_exit")
@login_required("admin")
def force_exit():
    sid = (request.json or {}).get("session_id")
    with db() as c:
        n = c.execute("UPDATE sessions SET exit_time=? WHERE id=? AND lot=? AND exit_time IS NULL",
                      (datetime.now().isoformat(timespec="seconds"), sid, _admin_lot())).rowcount
    return jsonify(ok=True) if n else (jsonify(error="Session not active"), 400)


@bp.route("/profile")
def profile_page():
    return render_template("profile.html") if "uid" in session else redirect("/login")


@bp.route("/api/complaints", methods=["GET", "POST"])
@login_required()
def complaints():
    uid, admin = session["uid"], session.get("role") == "admin"
    with db() as c:
        if request.method == "POST":
            d = request.get_json(silent=True) or {}
            lot = _admin_lot() if admin else (d.get("lot") or "").strip()
            msg = (d.get("message") or "").strip()
            if lot not in pred_module.LOT_ID_MAP or not msg:
                return jsonify(error="Parking lot and message are required"), 400
            c.execute("INSERT INTO complaints(user_id,lot,category,plate,message,status,created) VALUES(?,?,?,?,?,?,?)",
                      (uid, lot, (d.get("category") or "Other")[:40], (d.get("plate") or "").strip().upper()[:20],
                       msg[:1000], "Open", datetime.now().isoformat(timespec="seconds")))
            return jsonify(ok=True)
        mine = [dict(r) for r in c.execute("SELECT * FROM complaints WHERE user_id=? ORDER BY id DESC", (uid,))]
        lotc = [dict(r) for r in c.execute(
            "SELECT c.*, u.full_name FROM complaints c JOIN users u ON u.id=c.user_id "
            "WHERE c.lot=? AND c.user_id<>? ORDER BY c.id DESC", (_admin_lot(), uid))] if admin else []
    return jsonify(mine=mine, lot=lotc)


@bp.post("/api/complaints/resolve")
@login_required("admin")
def resolve_complaint():
    cid = (request.get_json(silent=True) or {}).get("id")
    with db() as c:
        c.execute("UPDATE complaints SET status='Resolved' WHERE id=? AND lot=?", (cid, _admin_lot()))
    return jsonify(ok=True)


def _slot(c, lot, date, time):
    """Predicted free spaces for lot at date/time minus spaces already booked in that hour."""
    try:
        dt = datetime.fromisoformat(f"{date}T{time}")
    except ValueError:
        raise ValueError("Invalid date or time")
    if lot not in pred_module.LOT_ID_MAP:
        raise ValueError("Select a valid parking lot")
    p = pred_module.predict_availability(lot, dt)
    # only ADMIN-APPROVED bookings reduce availability; pending ones don't yet
    n = c.execute("SELECT COUNT(*) FROM bookings WHERE lot=? AND book_date=? AND substr(book_time,1,2)=? AND status='Approved'",
                  (lot, date, time[:2])).fetchone()[0]
    return dt, p, max(0, p["estimated_available_spaces"] - n)


@bp.get("/api/book/availability")
@login_required("user")
def book_availability():
    a = request.args
    try:
        with db() as c:
            _, p, free = _slot(c, a.get("lot", ""), a.get("date", ""), a.get("time", ""))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(capacity=p["capacity"], available=free, status=p["status"], occupied_pct=p["predicted_occupied_pct"])


@bp.route("/api/bookings", methods=["GET", "POST"])
@login_required("user")
def bookings():
    uid = session["uid"]
    with db() as c:
        if request.method == "POST":
            d = request.get_json(silent=True) or {}
            v = c.execute("SELECT * FROM vehicles WHERE id=? AND user_id=?", (d.get("vehicle_id"), uid)).fetchone()
            if not v:
                return jsonify(error="Select one of your vehicles"), 400
            date, time, lot = d.get("date", ""), d.get("time", ""), d.get("lot", "")
            try:
                dt, p, free = _slot(c, lot, date, time)
            except ValueError as e:
                return jsonify(error=str(e)), 400
            if dt < datetime.now().replace(minute=0, second=0, microsecond=0):
                return jsonify(error="Choose a current or future time"), 400
            if c.execute("SELECT 1 FROM bookings WHERE plate=? AND book_date=? AND substr(book_time,1,2)=? AND status IN ('Pending','Approved')",
                         (v["plate"], date, time[:2])).fetchone():
                return jsonify(error="This vehicle already has a booking for that hour"), 400
            if free < 1:
                return jsonify(error="No spaces available for that time"), 400
            method = (d.get("payment_method") or "Card").strip()[:20]
            rate = {"Bike": 20, "SUV": 70, "Van": 90, "Pickup": 90, "Bus": 150, "Truck": 150, "Bicycle": 10}.get(v["vtype"], 50)
            pay_id = "PAY" + secrets.token_hex(5).upper()
            now = datetime.now().isoformat(timespec="seconds")
            # simulate a payment gateway call: verify before saving anything; no row/receipt if it fails
            paid_ok = random.random() < 0.9
            if not paid_ok:
                return jsonify(error="Payment failed. Please try again.", payment_status="Failed"), 402
            # payment verified as Paid; booking now waits for admin approval
            cur = c.execute("INSERT INTO bookings(user_id,vehicle_id,lot,plate,vtype,make_model,color,book_date,book_time,"
                            "amount,payment_method,payment_id,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (uid, v["id"], lot, v["plate"], v["vtype"], v["make_model"], v["color"], date, time[:5],
                             rate, method, pay_id, "Pending", now))
            row = dict(c.execute("SELECT * FROM bookings WHERE id=?", (cur.lastrowid,)).fetchone())
            return jsonify(ok=True, booking=row, payment_status="Paid", spaces_left=free)
        return jsonify([dict(r) for r in c.execute("SELECT * FROM bookings WHERE user_id=? ORDER BY book_date DESC, book_time DESC", (uid,))])


@bp.post("/api/bookings/cancel")
@login_required("user")
def cancel_booking():
    bid = (request.get_json(silent=True) or {}).get("id")
    with db() as c:
        c.execute("UPDATE bookings SET status='Cancelled' WHERE id=? AND user_id=? AND status IN ('Pending','Approved')",
                  (bid, session["uid"]))
    return jsonify(ok=True)


@bp.post("/api/bookings/decide")
@login_required("admin")
def decide_booking():
    d = request.get_json(silent=True) or {}
    bid, action = d.get("id"), d.get("action")
    if action not in ("Approved", "Rejected"):
        return jsonify(error="Invalid action"), 400
    with db() as c:
        b = c.execute("SELECT * FROM bookings WHERE id=? AND lot=? AND status='Pending'", (bid, _admin_lot())).fetchone()
        if not b:
            return jsonify(error="Booking not found or already decided"), 404
        if action == "Approved":
            p = pred_module.predict_availability(b["lot"], datetime.fromisoformat(f'{b["book_date"]}T{b["book_time"]}'))
            n = c.execute("SELECT COUNT(*) FROM bookings WHERE lot=? AND book_date=? AND substr(book_time,1,2)=? AND status='Approved'",
                         (b["lot"], b["book_date"], b["book_time"][:2])).fetchone()[0]
            if n >= p["estimated_available_spaces"]:
                return jsonify(error="No spaces left for that hour anymore"), 400
        c.execute("UPDATE bookings SET status=?, decided_at=? WHERE id=?",
                  (action, datetime.now().isoformat(timespec="seconds"), bid))
    return jsonify(ok=True)
