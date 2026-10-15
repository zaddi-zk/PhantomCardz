#!/usr/bin/env python3
"""PhantomCardz — cards & accounts marketplace backend (FastAPI + SQLite)."""
import os, sqlite3, secrets, random, time, hashlib, string, json
import smtplib
from email.message import EmailMessage
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Depends, Request, UploadFile, File
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.exceptions import HTTPException as StarletteHTTPException

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", APP_ROOT)          # Render: mount path of persistent disk
FRONTEND = os.path.join(APP_ROOT, "frontend")
DB_PATH  = os.path.join(DATA_DIR, "market.db")
UPLOADS  = os.path.join(DATA_DIR, "uploads")
LOGS     = os.path.join(DATA_DIR, "logs")
for _d in (DATA_DIR, UPLOADS, LOGS):
    os.makedirs(_d, exist_ok=True)

def _load_env():
    p = os.path.join(APP_ROOT, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
_load_env()

SITE         = os.getenv("SITE_NAME", "PhantomCardz")
ADMIN_EMAIL  = os.getenv("ADMIN_EMAIL", "admin@phantomcardz.cc")
ADMIN_PASS   = os.getenv("ADMIN_PASSWORD", "Boss123!")
ADMIN_TG     = os.getenv("ADMIN_TELEGRAM", "@phantomadmin")
CASHAPP      = os.getenv("CASHAPP_HANDLE", "$PhantomCardz")
BTC_ADDR     = os.getenv("BTC_ADDRESS", "bc1qphantom0000000000000000000000000000dead")
ETH_ADDR     = os.getenv("ETH_ADDRESS", "0xPhantom000000000000000000000000000dead")
LTC_ADDR     = os.getenv("LTC_ADDRESS", "ltc1qphantom00000000000000000000dead")
DOMAINS      = [d.strip() for d in os.getenv("ALLOWED_HOSTS", "phantomcardz.cc,www.phantomcardz.cc,localhost,127.0.0.1").split(",") if d.strip()]
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
TG_BOT  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
RESET_DEV_CODE = os.getenv("RESET_DEV_CODE", "true").lower() == "true"

security = HTTPBearer(auto_error=False)

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn

def h(pw: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
    return "pbkdf2$" + dk.hex()

def verify_pw(pw: str, salt: str, stored: str) -> bool:
    if stored.startswith("pbkdf2$"):
        return secrets.compare_digest(h(pw, salt), stored)
    return secrets.compare_digest(hashlib.sha256((salt + pw).encode()).hexdigest(), stored)

def now() -> int:
    return int(time.time())

def ts(v) -> Dict[str, int]:
    return {"_seconds": int(v or 0)}

def rand_str(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

def new_username(conn) -> str:
    while True:
        username = f"phan-{secrets.token_hex(4)}"
        if not conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return username

_rl: dict = {}
def _rl_hit(key: str, limit: int, window: int) -> bool:
    t = time.time()
    hits = [x for x in _rl.get(key, []) if t - x < window]
    hits.append(t)
    _rl[key] = hits
    return len(hits) > limit

def audit(event: str, actor: str = "-", detail: str = ""):
    with open(os.path.join(LOGS, "audit.log"), "a") as f:
        f.write(f"{now()} | {event} | {actor} | {detail}\n")

def tg_notify(text: str):
    if not (TG_BOT and TG_CHAT):
        return
    try:
        import urllib.request, urllib.parse as _up, threading
        def _send():
            url = f"https://api.telegram.org/bot{TG_BOT}/sendMessage"
            data = _up.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        pass

def send_reset_email(to_email: str, code: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = f"{SITE} password reset"
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg.set_content(
            f"Your {SITE} reset code is: {code}\n\n"
            f"It expires in 15 minutes.\nIf you did not request this, ignore this email.")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True
    except Exception:
        return False

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    username TEXT,
    password_hash TEXT NOT NULL,
    telegram TEXT,
    balance REAL DEFAULT 0,
    rank TEXT DEFAULT 'user',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS tokens(
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS products(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, description TEXT, image TEXT,
    active INTEGER DEFAULT 1, pinned INTEGER DEFAULT 0,
    owner_id INTEGER DEFAULT 1,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS variants(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    productId INTEGER, name TEXT,
    price REAL, comparePrice REAL,
    minQuantity INTEGER DEFAULT 1,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS stock_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id INTEGER, content TEXT,
    sold INTEGER DEFAULT 0, order_id INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, variant_id INTEGER, quantity INTEGER,
    total REAL, status TEXT DEFAULT 'completed',
    items TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS wallet_codes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE, value REAL, used INTEGER DEFAULT 0,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS deposits(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, method TEXT, amount REAL,
    reference TEXT, status TEXT DEFAULT 'pending',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS discount_codes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE, percent INTEGER, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS card_bases(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, price REAL, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cards(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_id INTEGER, number TEXT, exp_month INTEGER, exp_year INTEGER, cvv TEXT,
    sold INTEGER DEFAULT 0, buyer_id INTEGER,
    checked INTEGER DEFAULT 0, valid INTEGER
);
CREATE TABLE IF NOT EXISTS games_mines(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, mines INTEGER, amount REAL,
    safe_picks INTEGER DEFAULT 0, mines_map TEXT,
    opened TEXT, active INTEGER DEFAULT 1, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS game_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, game TEXT, amount REAL, payout REAL, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS announcements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT, link TEXT, active INTEGER DEFAULT 1,
    bannerColor TEXT DEFAULT '#000000', created_at INTEGER
);
CREATE TABLE IF NOT EXISTS password_resets(
    email TEXT, code TEXT, expires INTEGER, used INTEGER DEFAULT 0,
    PRIMARY KEY(email, code)
);
CREATE TABLE IF NOT EXISTS vouches(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    giver_id INTEGER, token TEXT UNIQUE,
    used_by INTEGER, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS tickets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, subject TEXT, body TEXT,
    status TEXT DEFAULT 'open', created_at INTEGER
);
CREATE TABLE IF NOT EXISTS ticket_replies(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER, admin_id INTEGER, body TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS seller_apps(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE, telegram TEXT, motivation TEXT,
    status TEXT DEFAULT 'pending', created_at INTEGER
);
CREATE TABLE IF NOT EXISTS telegram_claims(
    user_id INTEGER PRIMARY KEY, last_claim INTEGER
);
"""

def luhn_generate(length: int = 16) -> str:
    digits = [random.randint(0, 9) for _ in range(length - 1)]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    digits.append((10 - (total % 10)) % 10)
    return "".join(map(str, digits))

def init_db():
    conn = db()
    conn.executescript(SCHEMA)
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "username" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
    CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
    CREATE INDEX IF NOT EXISTS idx_stock_variant ON stock_items(variant_id, sold);
    CREATE INDEX IF NOT EXISTS idx_cards_base ON cards(base_id, sold);
    CREATE INDEX IF NOT EXISTS idx_cards_buyer ON cards(buyer_id);
    CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(user_id);
    CREATE INDEX IF NOT EXISTS idx_gamehist_user ON game_history(user_id);
    CREATE INDEX IF NOT EXISTS idx_products_active ON products(active, pinned);
    """)
    c = conn.cursor()
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        c.execute("INSERT INTO users(email,password_hash,telegram,balance,rank,created_at) VALUES(?,?,?,?,?,?)",
                  (ADMIN_EMAIL, h(ADMIN_PASS, ADMIN_EMAIL), ADMIN_TG, 1000.0, "admin", now()))
    missing_usernames = c.execute(
        "SELECT id FROM users WHERE username IS NULL OR TRIM(username)=''"
    ).fetchall()
    for row in missing_usernames:
        c.execute("UPDATE users SET username=? WHERE id=?", (new_username(conn), row["id"]))
    if c.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 0:
        c.execute("INSERT INTO announcements(content,link,active,bannerColor,created_at) VALUES(?,?,?,?,?)",
                  (f"Welcome to {SITE} — deposits are open!", None, 1, "#000000", now()))
    if c.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        seeds = [
            ("Hotmail FA", "Login and loot for goodies, easiest way to make a band!", 1, "Hotmails FA (Random)", 5.0, 25),
            ("Outlook FA", "Fresh farmed outlooks, ready for work.", 0, "Outlooks FA (Random)", 30.0, 40),
            ("Combo Lists", "Fresh combo lists for cracking.", 0, "Combos (10k lines)", 10.0, 50),
        ]
        for name, desc, pinned, vname, price, n in seeds:
            c.execute("INSERT INTO products(name,description,image,active,pinned,owner_id,created_at) VALUES(?,?,?,1,?,1,?)",
                      (name, desc, "/api/images/1", pinned, now()))
            pid = c.lastrowid
            c.execute("INSERT INTO variants(productId,name,price,comparePrice,minQuantity,created_at) VALUES(?,?,?,NULL,1,?)",
                      (pid, vname, price, now()))
            vid = c.lastrowid
            for _ in range(n):
                c.execute("INSERT INTO stock_items(variant_id,content,created_at) VALUES(?,?,?)",
                          (vid, f"{rand_str(7)}@hotmail.com:{rand_str(10).capitalize()}1!", now()))
    if c.execute("SELECT COUNT(*) FROM card_bases").fetchone()[0] == 0:
        for name, price in [("US Classic", 5.0), ("Premium Mix", 12.0)]:
            c.execute("INSERT INTO card_bases(name,price,active) VALUES(?,?,1)", (name, price))
            bid = c.lastrowid
            for _ in range(20):
                c.execute("INSERT INTO cards(base_id,number,exp_month,exp_year,cvv) VALUES(?,?,?,?,?)",
                          (bid, luhn_generate(16), random.randint(1, 12),
                           random.randint(2027, 2030), f"{random.randint(100, 999)}"))
    if c.execute("SELECT COUNT(*) FROM discount_codes").fetchone()[0] == 0:
        for code, pct in [("PHANTOM10", 10), ("PHANTOM25", 25)]:
            c.execute("INSERT INTO discount_codes(code,percent,active) VALUES(?,?,1)", (code, pct))
    defaults = {
        "manual_payments": json.dumps({"cashapp": CASHAPP,
            "bitcoin": BTC_ADDR,
            "ethereum": ETH_ADDR,
            "litecoin": LTC_ADDR}),
        "min_deposits": json.dumps({"cashapp": 5, "crypto": 10}),
        "features": json.dumps({"checker": True, "reseller": True, "ranks": True, "logs": True, "cards": True, "plinko": False}),
        "payment_methods": json.dumps({"wallet": True, "cashapp": True, "crypto": True, "stars": False,
                                       "venmo": False, "zelle": False, "chime": True, "applepay": False}),
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    conn.commit()
    conn.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title=f"{SITE} API", version="1.0.0", lifespan=lifespan)

from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(TrustedHostMiddleware, allowed_hosts=DOMAINS + ["testserver"])
app.add_middleware(CORSMiddleware,
    allow_origins=[f"https://{d}" for d in DOMAINS] + [f"http://{d}" for d in DOMAINS] + ["http://localhost:8000"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def maintenance_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") and path not in ("/api/health", "/api/health/", "/api/login", "/api/admin/settings/maintenance"):
        conn = db()
        m = get_setting(conn, "maintenance", "0")
        conn.close()
        if m == "1":
            return JSONResponse({"message": "Maintenance mode, back soon"}, status_code=503)
    return await call_next(request)

@app.exception_handler(ApiError)
async def on_api_error(request, exc: ApiError):
    return JSONResponse({"message": exc.message}, status_code=exc.status)

@app.exception_handler(StarletteHTTPException)
async def spa_or_json_404(request: Request, exc: StarletteHTTPException):
    path = request.url.path
    last = path.rsplit("/", 1)[-1]
    if exc.status_code == 404 and not path.startswith("/api") and not path.startswith("/uploads") and "." not in last:
        return FileResponse(os.path.join(FRONTEND, "index.html"))
    msg = exc.detail if isinstance(exc.detail, str) else "Error"
    return JSONResponse({"message": msg}, status_code=exc.status_code)

def get_current_user(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    if not token:
        token = request.cookies.get("session")
    if not token:
        raise ApiError(401, "No auth")
    conn = db()
    t = conn.execute("SELECT user_id FROM tokens WHERE token=?", (token,)).fetchone()
    if not t:
        conn.close()
        raise ApiError(401, "Invalid token")
    u = conn.execute("SELECT * FROM users WHERE id=?", (t["user_id"],)).fetchone()
    conn.close()
    if not u:
        raise ApiError(401, "Invalid token")
    return dict(u)

def require_admin(user: dict):
    if user.get("rank") != "admin":
        raise ApiError(403, "Admin only")

def create_token(conn, user_id: int) -> str:
    token = secrets.token_hex(32)
    conn.execute("INSERT INTO tokens(token,user_id,created_at) VALUES(?,?,?)", (token, user_id, now()))
    return token

def user_out(u) -> dict:
    return {"id": u["id"], "email": u["email"], "username": u["username"],
            "balance": u["balance"], "rank": u["rank"]}

def get_setting(conn, key: str, fallback="{}"):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else fallback

def set_setting(conn, key: str, value: str):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))

# ================= AUTH =================
class CredsIn(BaseModel):
    email: str
    password: str
    rememberMe: Optional[bool] = None

class RegisterIn(BaseModel):
    email: str
    password: str
    telegram: Optional[str] = None

@app.post("/api/login")
def login(request: Request, data: CredsIn):
    if _rl_hit(f"login:{request.client.host if request.client else 'x'}", 10, 60):
        raise ApiError(429, "Too many attempts, wait a minute")
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE email=?", (data.email,)).fetchone()
    if not u or not verify_pw(data.password, data.email, u["password_hash"]):
        conn.close()
        raise ApiError(401, "Invalid email or password")
    token = create_token(conn, u["id"])
    conn.commit()
    conn.close()
    resp = JSONResponse({"token": token, "user": user_out(u)})
    resp.set_cookie(key="session", value=token, httponly=True, secure=COOKIE_SECURE,
                    samesite="lax", path="/", max_age=7 * 24 * 3600)
    return resp

@app.post("/api/register")
def register(request: Request, data: RegisterIn):
    if _rl_hit(f"reg:{request.client.host if request.client else 'x'}", 5, 3600):
        raise ApiError(429, "Too many registrations, slow down")
    conn = db()
    try:
        username = new_username(conn)
        conn.execute("INSERT INTO users(email,username,password_hash,telegram,balance,rank,created_at) VALUES(?,?,?,?,?,?,?)",
                     (data.email, username, h(data.password, data.email), data.telegram, 0.0, "user", now()))
        u = conn.execute("SELECT * FROM users WHERE email=?", (data.email,)).fetchone()
        token = create_token(conn, u["id"])
        conn.commit()
        audit("register", data.email)
        tg_notify(f"[{SITE}] New user: {data.email}")
        conn.close()
        resp = JSONResponse({"token": token, "user": user_out(u)})
        resp.set_cookie(key="session", value=token, httponly=True, secure=COOKIE_SECURE,
                        samesite="lax", path="/", max_age=7 * 24 * 3600)
        return resp
    except sqlite3.IntegrityError:
        conn.close()
        raise ApiError(400, "Email already exists")

@app.get("/api/user")
def me(user: dict = Depends(get_current_user)):
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (user["id"],)).fetchone()[0]
    spent = conn.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE user_id=? AND status='completed'",
                         (user["id"],)).fetchone()[0]
    conn.close()
    return {"id": user["id"], "email": user["email"], "username": user["username"],
            "balance": user["balance"], "rank": user["rank"],
            "telegram": user["telegram"], "orders": n, "totalSpent": round(spent, 2)}

@app.post("/api/logout")
def logout(user: dict = Depends(get_current_user)):
    conn = db()
    conn.execute("DELETE FROM tokens WHERE user_id=?", (user["id"],))
    conn.commit()
    conn.close()
    resp = JSONResponse({"message": "Logged out", "ok": True})
    resp.delete_cookie("session", path="/")
    return resp

class PasswordIn(BaseModel):
    currentPassword: str
    newPassword: str

@app.post("/api/user/password")
def change_password(data: PasswordIn, user: dict = Depends(get_current_user)):
    conn = db()
    if not verify_pw(data.currentPassword, user["email"], user["password_hash"]):
        conn.close()
        raise ApiError(400, "Wrong password")
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (h(data.newPassword, user["email"]), user["id"]))
    conn.commit()
    conn.close()
    return {"ok": True}

class EmailIn(BaseModel):
    password: str
    newEmail: str

@app.post("/api/user/email")
def change_email(data: EmailIn, user: dict = Depends(get_current_user)):
    conn = db()
    if not verify_pw(data.password, user["email"], user["password_hash"]):
        conn.close()
        raise ApiError(400, "Wrong password")
    conn.execute("UPDATE users SET email=?, password_hash=? WHERE id=?",
                 (data.newEmail, h(data.password, data.newEmail), user["id"]))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/user/rank")
def my_rank(user: dict = Depends(get_current_user)):
    conn = db()
    spent = conn.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE user_id=? AND status='completed'",
                         (user["id"],)).fetchone()[0]
    conn.close()
    if spent >= 5000: r, n, d = "Diamond", "Diamond", 10
    elif spent >= 1000: r, n, d = "Gold", "Gold", 5
    elif spent >= 250: r, n, d = "Silver", "Silver", 2
    else: r, n, d = "user", "Bronze", 0
    return {"rank": r, "name": n, "discount": d, "discountPct": d}

# ================= PUBLIC =================
@app.get("/api/health")
def health():
    return {"ok": True, "service": "api"}

@app.get("/api/announcements")
def announcements():
    conn = db()
    rows = conn.execute("SELECT * FROM announcements WHERE active=1 ORDER BY id DESC").fetchall()
    conn.close()
    return [{"id": r["id"], "content": r["content"], "link": r["link"], "active": bool(r["active"]),
             "createdAt": ts(r["created_at"]), "bannerColor": r["bannerColor"]} for r in rows]

@app.get("/api/products")
def products():
    conn = db()
    rows = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY pinned DESC, id DESC").fetchall()
    out = []
    for r in rows:
        vs = conn.execute("SELECT * FROM variants WHERE productId=?", (r["id"],)).fetchall()
        vv = []
        for v in vs:
            sc = conn.execute("SELECT COUNT(*) FROM stock_items WHERE variant_id=? AND sold=0",
                              (v["id"],)).fetchone()[0]
            vv.append({"id": v["id"], "productId": v["productId"], "name": v["name"], "price": v["price"],
                      "comparePrice": v["comparePrice"], "minQuantity": v["minQuantity"], "stockCount": sc})
        out.append({"id": r["id"], "name": r["name"], "description": r["description"], "image": r["image"],
                    "active": bool(r["active"]), "pinned": bool(r["pinned"]), "createdAt": ts(r["created_at"]),
                    "variants": vv})
    conn.close()
    return out

@app.get("/api/payment-methods")
def payment_methods():
    conn = db()
    data = json.loads(get_setting(conn, "payment_methods"))
    conn.close()
    return data

@app.get("/api/settings/features")
def features():
    conn = db()
    data = json.loads(get_setting(conn, "features"))
    conn.close()
    return data

MANUAL_METHODS = ("cashapp", "venmo", "zelle", "chime", "applepay")
MANUAL_HANDLE_FIELD = {"cashapp": "tag", "venmo": "handle", "zelle": "handle",
                       "chime": "handle", "applepay": "handle"}
MANUAL_SETTING_KEY = {"cashapp": "cashapp-tag", "venmo": "venmo-handle",
                      "zelle": "zelle-handle", "chime": "chime-handle",
                      "applepay": "applepay-handle"}
CRYPTO_ADDRESS_KEY = {"BTC": "bitcoin", "ETH": "ethereum", "LTC": "litecoin", "USDT": "ethereum"}

def _json_setting(conn, key: str, fallback=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not r:
        return fallback
    try:
        return json.loads(r["value"])
    except Exception:
        return r["value"]

def manual_payments_public(conn) -> dict:
    """Nested shape the frozen SPA expects: {method: {enabled, fee, tag|handle}}."""
    pm = _json_setting(conn, "payment_methods", {}) or {}
    legacy = _json_setting(conn, "manual_payments", {}) or {}
    out = {}
    for m in MANUAL_METHODS:
        field = MANUAL_HANDLE_FIELD[m]
        val = _json_setting(conn, MANUAL_SETTING_KEY[m], None)
        if val is None:
            lv = legacy.get(m)
            if isinstance(lv, dict):
                val = lv.get(field) or lv.get("handle") or lv.get("tag") or ""
            else:
                val = lv or ""
        try:
            fee = float(_json_setting(conn, f"{m}-fee", 0) or 0)
        except (TypeError, ValueError):
            fee = 0.0
        out[m] = {"enabled": bool(pm.get(m, False)), "fee": fee, field: val or ""}
    return out

def manual_handle(conn, method: str, fallback: str = "") -> str:
    return manual_payments_public(conn).get(method, {}).get(MANUAL_HANDLE_FIELD[method], fallback) or fallback

def crypto_address(conn, coin: str) -> str:
    mp = _json_setting(conn, "manual_payments", {}) or {}
    key = CRYPTO_ADDRESS_KEY.get((coin or "BTC").upper(), "bitcoin")
    return mp.get(key) or {"bitcoin": BTC_ADDR, "ethereum": ETH_ADDR, "litecoin": LTC_ADDR}.get(key, BTC_ADDR)

def deposit_out(r, email: Optional[str] = None) -> dict:
    """Serialize a deposit row in the shape the frozen SPA expects."""
    method = r["method"] or ""
    usd = float(r["amount"] or 0)
    out = {
        "id": r["id"],
        "type": "crypto" if method.startswith("crypto") else method,
        "method": method,
        "amount": int(round(usd * 100)),
        "amountUsd": usd,
        "paymentId": r["reference"],
        "orderId": r["reference"],
        "reference": r["reference"],
        "status": r["status"],
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(r["created_at"] or 0))),
    }
    if email is not None:
        out["email"] = email
    return out

@app.get("/api/site-settings/manual-payments")
def manual_payments():
    conn = db()
    data = manual_payments_public(conn)
    conn.close()
    return data

@app.get("/api/site-settings/min-deposits")
def min_deposits():
    conn = db()
    data = json.loads(get_setting(conn, "min_deposits"))
    conn.close()
    return data

@app.get("/api/images/{image_id}")
def image(image_id: int):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
           '<rect width="200" height="200" fill="#0d0d14"/>'
           f'<text x="100" y="105" fill="#00ff9d" font-size="40" text-anchor="middle" font-family="monospace">#{image_id}</text></svg>')
    return Response(content=svg, media_type="image/svg+xml")

@app.get("/api/stock")
def stock_totals():
    conn = db()
    p = conn.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
    s = conn.execute("SELECT COUNT(*) FROM stock_items WHERE sold=0").fetchone()[0]
    conn.close()
    return {"products": p, "itemsInStock": s}

# ================= ORDERS (PHASE A) =================
class OrderIn(BaseModel):
    variantId: int
    quantity: int = 1
    code: Optional[str] = None

@app.post("/api/orders")
def place_order(data: OrderIn, user: dict = Depends(get_current_user)):
    if data.quantity < 1 or data.variantId <= 0:
        raise ApiError(400, "Invalid order")
    conn = db()
    v = conn.execute("SELECT * FROM variants WHERE id=?", (data.variantId,)).fetchone()
    if not v:
        conn.close()
        raise ApiError(404, "Not found")
    stock = conn.execute("SELECT * FROM stock_items WHERE variant_id=? AND sold=0 LIMIT ?",
                         (data.variantId, data.quantity)).fetchall()
    if len(stock) < data.quantity:
        conn.close()
        raise ApiError(400, "Insufficient stock")
    total = v["price"] * data.quantity
    if data.code:
        d = conn.execute("SELECT * FROM discount_codes WHERE code=? AND active=1", (data.code,)).fetchone()
        if d:
            total = total * (1 - d["percent"] / 100.0)
    total = round(total, 2)
    claimed = []
    for s in stock:
        cur = conn.execute("UPDATE stock_items SET sold=1, order_id=NULL WHERE id=? AND sold=0", (s["id"],))
        if cur.rowcount == 1:
            claimed.append(s)
    if len(claimed) < data.quantity:
        conn.rollback(); conn.close()
        raise ApiError(400, "Insufficient stock")
    cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                       (total, user["id"], total))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Insufficient balance")
    items = [s["content"] for s in claimed]
    cur = conn.execute(
        "INSERT INTO orders(user_id,variant_id,quantity,total,status,items,created_at) VALUES(?,?,?,?,?,?,?)",
        (user["id"], data.variantId, data.quantity, total, "completed", json.dumps(items), now()))
    oid = cur.lastrowid
    for s in claimed:
        conn.execute("UPDATE stock_items SET order_id=? WHERE id=?", (oid, s["id"]))
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.commit()
    conn.close()
    tg_notify(f"[{SITE}] ORDER #{oid} by {user['email']}: {data.quantity}x variant {data.variantId} = ${total}")
    return {"id": oid, "items": items, "total": total, "newBalance": round(u["balance"], 2)}

@app.get("/api/orders")
def my_orders(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    conn.close()
    return [{"id": r["id"], "variantId": r["variant_id"], "quantity": r["quantity"], "total": r["total"],
             "status": r["status"], "createdAt": ts(r["created_at"]),
             "items": json.loads(r["items"] or "[]")} for r in rows]

@app.get("/api/orders/export")
def export_orders(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT items FROM orders WHERE user_id=? AND status='completed' ORDER BY id DESC",
                        (user["id"],)).fetchall()
    conn.close()
    lines = []
    for r in rows:
        lines.extend(json.loads(r["items"] or "[]"))
    return {"content": "\n".join(lines)}

@app.get("/api/orders/{order_id}")
def one_order(order_id: int, user: dict = Depends(get_current_user)):
    conn = db()
    r = conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user["id"])).fetchone()
    conn.close()
    if not r:
        raise ApiError(404, "Not found")
    return {"id": r["id"], "variantId": r["variant_id"], "quantity": r["quantity"], "total": r["total"],
            "status": r["status"], "createdAt": ts(r["created_at"]),
            "items": json.loads(r["items"] or "[]")}

class DiscountIn(BaseModel):
    code: str

@app.post("/api/discount/validate")
def validate_discount(data: DiscountIn, user: dict = Depends(get_current_user)):
    conn = db()
    d = conn.execute("SELECT * FROM discount_codes WHERE code=? AND active=1", (data.code,)).fetchone()
    conn.close()
    if d:
        return {"valid": True, "percent": d["percent"]}
    return {"valid": False}

# ================= WALLET =================
class RedeemIn(BaseModel):
    code: str

@app.post("/api/wallet/redeem")
def redeem(data: RedeemIn, user: dict = Depends(get_current_user)):
    conn = db()
    r = conn.execute("SELECT * FROM wallet_codes WHERE code=?", (data.code.upper(),)).fetchone()
    if not r:
        conn.close()
        raise ApiError(400, "Invalid code")
    cur = conn.execute("UPDATE wallet_codes SET used=1 WHERE id=? AND used=0", (r["id"],))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Code already redeemed")
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (r["value"], user["id"]))
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.commit()
    conn.close()
    audit("redeem", user["email"], f"${r['value']}")
    return {"newBalance": round(u["balance"], 2), "redeemed": r["value"]}

@app.get("/api/wallet/transactions")
def wallet_txs(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 50", (user["id"],)).fetchall()
    conn.close()
    return [{"id": r["id"], "variant": r["variant_id"], "quantity": r["quantity"], "amount": r["total"],
             "status": r["status"], "createdAt": ts(r["created_at"])} for r in rows]

# ================= DEPOSITS (PHASE B) =================
class CashappIn(BaseModel):
    amount: float
    cashtag: Optional[str] = None

def _deposit_ref() -> str:
    return secrets.token_hex(4).upper()

@app.post("/api/orders/cashapp")
def deposit_cashapp(data: CashappIn, user: dict = Depends(get_current_user)):
    if data.amount <= 0: raise ApiError(400, "Invalid amount")
    conn = db()
    mins = json.loads(get_setting(conn, "min_deposits"))
    if data.amount < mins.get("cashapp", 5):
        conn.close()
        raise ApiError(400, f"Minimum deposit is ${mins.get('cashapp', 5)}")
    handle = manual_handle(conn, "cashapp", CASHAPP)
    ref = _deposit_ref()
    note = f"{SITE.upper()}-{ref}"
    conn.execute("INSERT INTO deposits(user_id,method,amount,reference,status,created_at) VALUES(?,?,?,?,?,?)",
                 (user["id"], "cashapp", data.amount, ref, "pending", now()))
    conn.commit()
    tg_notify(f"[{SITE}] CashApp deposit req {ref} by {user['email']}: ${data.amount}")
    conn.close()
    return {"status": "pending", "reference": ref, "order": {"id": ref}, "paymentNote": note, "cashappTag": handle,
            "instructions": f"Send ${data.amount} via CashApp to {handle} then message {ADMIN_TG} on Telegram with code {ref} and your email {user['email']}"}

class ManualDepositIn(BaseModel):
    amount: float

@app.post("/api/deposits/{method}")
def deposit_manual(method: str, data: ManualDepositIn, user: dict = Depends(get_current_user)):
    if method not in MANUAL_METHODS:
        raise ApiError(404, "Unsupported method")
    if data.amount <= 0:
        raise ApiError(400, "Invalid amount")
    conn = db()
    mins = json.loads(get_setting(conn, "min_deposits"))
    minimum = mins.get(method, 5)
    if data.amount < minimum:
        conn.close()
        raise ApiError(400, f"Minimum deposit for {method} is ${minimum}")
    handle = manual_handle(conn, method, "")
    ref = _deposit_ref()
    note = f"{SITE.upper()}-{ref}"
    conn.execute("INSERT INTO deposits(user_id,method,amount,reference,status,created_at) VALUES(?,?,?,?,?,?)",
                 (user["id"], method, data.amount, ref, "pending", now()))
    conn.commit()
    tg_notify(f"[{SITE}] {method} deposit req {ref} by {user['email']}: ${data.amount}")
    conn.close()
    return {"status": "pending", "reference": ref, "order": {"id": ref}, "paymentNote": note, "handle": handle,
            "instructions": f"Send ${data.amount} via {method} to {handle} and include note {note}"}

class CryptoIn(BaseModel):
    amount: Any = 0
    currency: str = "BTC"
    payCurrency: Optional[str] = None
    purpose: Optional[str] = None

def _crypto_tracking(r) -> dict:
    method = (r["method"] or "crypto:BTC").split(":", 1)
    coin = (method[1] if len(method) > 1 else "BTC").upper()
    conn = db()
    addr = crypto_address(conn, coin)
    conn.close()
    usd = float(r["amount"] or 0)
    created = int(r["created_at"] or now())
    return {
        "paymentId": r["reference"],
        "reference": r["reference"],
        "status": r["status"],
        "payCurrency": coin,
        "currency": "USD",
        "network": coin,
        "payAddress": addr,
        "address": addr,
        "payAmount": f"{usd:.2f}",
        "amount": int(round(usd * 100)),
        "expiresAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created + 3600)),
    }

@app.post("/api/payments/crypto/create")
def deposit_crypto(data: CryptoIn, user: dict = Depends(get_current_user)):
    new_dialect = data.payCurrency is not None or isinstance(data.amount, str)
    try:
        raw = float(data.amount)
    except (TypeError, ValueError):
        raise ApiError(400, "Invalid amount")
    usd = raw / 100.0 if new_dialect else raw
    if usd <= 0:
        raise ApiError(400, "Invalid amount")
    coin = (data.payCurrency or data.currency or "BTC").upper()
    conn = db()
    mins = json.loads(get_setting(conn, "min_deposits"))
    minimum = mins.get("crypto", 10)
    if usd < minimum:
        conn.close()
        raise ApiError(400, f"Minimum deposit is ${minimum}")
    addr = crypto_address(conn, coin)
    ref = secrets.token_hex(8) if new_dialect else _deposit_ref()
    conn.execute("INSERT INTO deposits(user_id,method,amount,reference,status,created_at) VALUES(?,?,?,?,?,?)",
                 (user["id"], f"crypto:{coin}", usd, ref, "pending", now()))
    conn.commit()
    tg_notify(f"[{SITE}] Crypto deposit req {ref} by {user['email']}: ${usd}")
    row = conn.execute("SELECT * FROM deposits WHERE reference=?", (ref,)).fetchone()
    conn.close()
    if new_dialect:
        return _crypto_tracking(row)
    return {"address": addr, "amount": usd, "currency": coin, "reference": ref,
            "status": "pending", "expiresAt": ts(now() + 3600)}

@app.get("/api/payments/crypto/currencies")
def crypto_currencies():
    return {"currencies": list(CRYPTO_ADDRESS_KEY.keys())}

@app.get("/api/payments/crypto/status")
def crypto_status(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    conn.close()
    return [{"id": r["id"], "method": r["method"], "amount": r["amount"], "reference": r["reference"],
             "status": r["status"], "createdAt": ts(r["created_at"])} for r in rows]

def _find_crypto_deposit(conn, pid: str, user_id: int):
    row = None
    if str(pid).isdigit():
        row = conn.execute("SELECT * FROM deposits WHERE id=? AND user_id=?", (int(pid), user_id)).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM deposits WHERE reference=? AND user_id=?", (pid, user_id)).fetchone()
    return row

@app.get("/api/payments/crypto/{pid}")
def crypto_payment_detail(pid: str, user: dict = Depends(get_current_user)):
    conn = db()
    r = _find_crypto_deposit(conn, pid, user["id"])
    conn.close()
    if not r:
        raise ApiError(404, "Not found")
    return _crypto_tracking(r)

@app.get("/api/payments/crypto/{pid}/status")
def crypto_payment_status(pid: str, user: dict = Depends(get_current_user)):
    conn = db()
    r = _find_crypto_deposit(conn, pid, user["id"])
    conn.close()
    if not r:
        raise ApiError(404, "Not found")
    return {"status": r["status"], "reference": r["reference"], "amount": r["amount"],
            "createdAt": ts(r["created_at"])}

@app.get("/api/deposits")
def my_deposits(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    conn.close()
    return [deposit_out(r) for r in rows]

# ================= CARDS + CHECKER (PHASE C) =================
@app.get("/api/card-bases")
def card_bases():
    conn = db()
    rows = conn.execute("SELECT * FROM card_bases WHERE active=1").fetchall()
    out = []
    for r in rows:
        sc = conn.execute("SELECT COUNT(*) FROM cards WHERE base_id=? AND sold=0", (r["id"],)).fetchone()[0]
        out.append({"id": r["id"], "name": r["name"], "price": r["price"], "stockCount": sc})
    conn.close()
    return out

class BuyCardIn(BaseModel):
    baseId: int

@app.post("/api/cards")
def buy_card(data: BuyCardIn, user: dict = Depends(get_current_user)):
    conn = db()
    b = conn.execute("SELECT * FROM card_bases WHERE id=? AND active=1", (data.baseId,)).fetchone()
    if not b:
        conn.close()
        raise ApiError(404, "Not found")
    c = conn.execute("SELECT * FROM cards WHERE base_id=? AND sold=0 ORDER BY RANDOM() LIMIT 1",
                     (data.baseId,)).fetchone()
    if not c:
        conn.close()
        raise ApiError(400, "Out of stock")
    cur = conn.execute("UPDATE cards SET sold=1, buyer_id=? WHERE id=? AND sold=0",
                       (user["id"], c["id"]))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Card just sold, try again")
    cur2 = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                        (b["price"], user["id"], b["price"]))
    if cur2.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Insufficient balance")
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.commit()
    conn.close()
    return {"id": c["id"], "number": c["number"], "expMonth": c["exp_month"],
            "expYear": c["exp_year"], "cvv": c["cvv"], "newBalance": round(u["balance"], 2)}

@app.get("/api/user/cards")
def my_cards(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("""SELECT ca.*, cb.name AS base_name FROM cards ca
                           JOIN card_bases cb ON cb.id=ca.base_id
                           WHERE ca.buyer_id=? ORDER BY ca.id DESC""", (user["id"],)).fetchall()
    conn.close()
    return [{"id": r["id"], "base": r["base_name"], "number": r["number"], "expMonth": r["exp_month"],
             "expYear": r["exp_year"], "cvv": r["cvv"],
             "checked": bool(r["checked"]), "valid": r["valid"]} for r in rows]

class CheckIn(BaseModel):
    cardId: int

@app.post("/api/checker/check")
def check_card(data: CheckIn, user: dict = Depends(get_current_user)):
    conn = db()
    c = conn.execute("SELECT ca.* FROM cards ca WHERE ca.id=? AND ca.buyer_id=?", (data.cardId, user["id"])).fetchone()
    if not c:
        conn.close()
        raise ApiError(404, "Not found")
    valid = random.random() < 0.7
    conn.execute("UPDATE cards SET checked=1, valid=? WHERE id=?", (1 if valid else 0, data.cardId))
    conn.commit()
    conn.close()
    return {"cardId": data.cardId, "valid": valid, "message": "Approved" if valid else "Declined"}

@app.get("/api/checker/config")
def checker_config(user: dict = Depends(get_current_user)):
    return {"enabled": True, "dailyLimit": 50}

# ================= GAMES (PHASE D) =================
class DiceIn(BaseModel):
    amount: float
    guessOver: int

@app.post("/api/games/dice")
def game_dice(data: DiceIn, user: dict = Depends(get_current_user)):
    if data.amount <= 0: raise ApiError(400, "Invalid amount")
    if not (2 <= data.guessOver <= 98):
        raise ApiError(400, "guessOver must be 2-98")
    conn = db()
    cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                       (data.amount, user["id"], data.amount))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Insufficient balance")
    roll = round(random.uniform(0, 100), 2)
    mult = 99.0 / (100 - data.guessOver)
    win = roll > data.guessOver
    payout = round(data.amount * mult, 2) if win else 0.0
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (payout, user["id"]))
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.execute("INSERT INTO game_history(user_id,game,amount,payout,created_at) VALUES(?,?,?,?,?)",
                 (user["id"], "dice", data.amount, payout, now()))
    conn.commit(); conn.close()
    return {"result": "win" if win else "lose", "roll": roll, "multiplier": round(mult, 4),
            "payout": payout, "newBalance": round(u["balance"], 2)}

class SpinIn(BaseModel):
    amount: float

@app.post("/api/games/spin")
def game_spin(data: SpinIn, user: dict = Depends(get_current_user)):
    if data.amount <= 0: raise ApiError(400, "Invalid amount")
    symbols = ["Cherry", "Lemon", "Bell", "Seven", "Diamond"]
    weights = [40, 30, 15, 10, 5]
    pay = {"Cherry": 2, "Lemon": 3, "Bell": 5, "Seven": 10, "Diamond": 25}
    reels = random.choices(symbols, weights=weights, k=3)
    conn = db()
    cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                       (data.amount, user["id"], data.amount))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Insufficient balance")
    payout = 0.0
    if reels[0] == reels[1] == reels[2]:
        payout = round(data.amount * pay[reels[0]] * 0.95, 2)
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (payout, user["id"]))
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.execute("INSERT INTO game_history(user_id,game,amount,payout,created_at) VALUES(?,?,?,?,?)",
                 (user["id"], "spin", data.amount, payout, now()))
    conn.commit(); conn.close()
    return {"result": "win" if payout > 0 else "lose", "reels": reels,
            "payout": payout, "newBalance": round(u["balance"], 2)}

class MinesIn(BaseModel):
    amount: float = 0
    mines: int = 3
    action: str = "start"
    tile: Optional[int] = None
    gameId: Optional[int] = None

@app.post("/api/games/mines")
def game_mines(data: MinesIn, user: dict = Depends(get_current_user)):
    if data.action in ("start",) and data.amount <= 0: raise ApiError(400, "Invalid amount")
    conn = db()
    if data.action == "start":
        if not (1 <= data.mines <= 24):
            conn.close()
            raise ApiError(400, "mines must be 1-24")
        live = conn.execute("SELECT * FROM games_mines WHERE user_id=? AND active=1", (user["id"],)).fetchone()
        if live:
            conn.close()
            raise ApiError(400, "Finish current game first")
        cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                           (data.amount, user["id"], data.amount))
        if cur.rowcount != 1:
            conn.rollback(); conn.close()
            raise ApiError(400, "Insufficient balance")
        mm = random.sample(range(25), data.mines)
        gid = conn.execute("""INSERT INTO games_mines(user_id,mines,amount,safe_picks,mines_map,opened,active,created_at)
                        VALUES(?,?,?,?,?,?,1,?)""",
                      (user["id"], data.mines, data.amount, 0, json.dumps(mm), json.dumps([]), now())).lastrowid
        u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
        conn.commit(); conn.close()
        return {"gameId": gid, "status": "started", "mines": data.mines,
                "newBalance": round(u["balance"], 2)}
    g = conn.execute("SELECT * FROM games_mines WHERE id=? AND user_id=? AND active=1",
                     (data.gameId, user["id"])).fetchone()
    if not g:
        conn.close()
        raise ApiError(404, "Not found")
    mines_map = json.loads(g["mines_map"])
    opened = json.loads(g["opened"])
    if data.action == "pick":
        if data.tile is None or data.tile < 0 or data.tile > 24:
            conn.close()
            raise ApiError(400, "Bad tile")
        if data.tile in opened:
            conn.close()
            raise ApiError(400, "Tile already opened")
        opened.append(data.tile)
        if data.tile in mines_map:
            conn.execute("UPDATE games_mines SET opened=?, active=0 WHERE id=?", (json.dumps(opened), g["id"]))
            conn.execute("INSERT INTO game_history(user_id,game,amount,payout,created_at) VALUES(?,?,?,?,?)",
                         (user["id"], "mines", g["amount"], 0.0, now()))
            u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
            conn.commit(); conn.close()
            return {"hit": True, "done": True, "tile": data.tile, "payout": 0.0,
                    "newBalance": round(u["balance"], 2)}
        safe = g["safe_picks"] + 1
        conn.execute("UPDATE games_mines SET safe_picks=?, opened=? WHERE id=?",
                     (safe, json.dumps(opened), g["id"]))
        mult = round(0.97 * ((25.0 / (25 - g["mines"])) ** safe), 4)
        conn.commit(); conn.close()
        return {"hit": False, "done": False, "tile": data.tile, "safePicks": safe,
                "multiplier": mult, "cashoutValue": round(g["amount"] * mult, 2)}
    if data.action == "cashout":
        safe = g["safe_picks"]
        if safe < 1:
            conn.close()
            raise ApiError(400, "Pick at least one safe tile first")
        cur = conn.execute("UPDATE games_mines SET active=0 WHERE id=? AND active=1", (g["id"],))
        if cur.rowcount != 1:
            conn.rollback(); conn.close()
            raise ApiError(400, "Game already finished")
        mult = 0.97 * ((25.0 / (25 - g["mines"])) ** safe)
        payout = round(g["amount"] * mult, 2)
        conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (payout, user["id"]))
        conn.execute("INSERT INTO game_history(user_id,game,amount,payout,created_at) VALUES(?,?,?,?,?)",
                     (user["id"], "mines", g["amount"], payout, now()))
        u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
        conn.commit(); conn.close()
        return {"result": "win", "multiplier": round(mult, 4), "payout": payout,
                "newBalance": round(u["balance"], 2)}
    conn.close()
    raise ApiError(400, "Bad action")

class PlinkoIn(BaseModel):
    amount: float
    risk: str = "low"

@app.post("/api/games/plinko")
def game_plinko(data: PlinkoIn, user: dict = Depends(get_current_user)):
    if data.amount <= 0: raise ApiError(400, "Invalid amount")
    tables = {
        "low":  [16, 9, 2, 1.4, 1.4, 1.2, 1.1, 1.0, 0.5, 1.0, 1.1, 1.2, 1.4, 1.4, 2, 9, 16],
        "high": [1000, 130, 26, 9, 4, 2, 0.2, 0.2, 0.2, 0.2, 0.2, 2, 4, 9, 26, 130, 1000],
    }
    tbl = tables.get(data.risk, tables["low"])
    conn = db()
    cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                       (data.amount, user["id"], data.amount))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Insufficient balance")
    pos = sum(1 for _ in range(16) if random.random() < 0.5)
    payout = round(data.amount * tbl[pos], 2)
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (payout, user["id"]))
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.execute("INSERT INTO game_history(user_id,game,amount,payout,created_at) VALUES(?,?,?,?,?)",
                 (user["id"], "plinko", data.amount, payout, now()))
    conn.commit(); conn.close()
    return {"bucket": pos, "multiplier": tbl[pos], "payout": payout,
            "newBalance": round(u["balance"], 2)}

@app.get("/api/games/history")
def game_history_list(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM game_history WHERE user_id=? ORDER BY id DESC LIMIT 50",
                        (user["id"],)).fetchall()
    conn.close()
    return [{"game": r["game"], "amount": r["amount"], "payout": r["payout"],
             "createdAt": ts(r["created_at"])} for r in rows]

# ================= ADMIN (PHASE E) =================
@app.get("/api/admin/dashboard")
def admin_dashboard(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    deps = conn.execute("SELECT COUNT(*) FROM deposits").fetchone()[0]
    pend = conn.execute("SELECT COUNT(*) FROM deposits WHERE status='pending'").fetchone()[0]
    rev = conn.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status='completed'").fetchone()[0]
    cards_sold = conn.execute("SELECT COUNT(*) FROM cards WHERE sold=1").fetchone()[0]
    bal = conn.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]
    grev = conn.execute("SELECT COALESCE(SUM(amount-payout),0) FROM game_history").fetchone()[0]
    conn.close()
    return {"users": users, "orders": orders, "deposits": deps, "pendingDeposits": pend,
            "revenue": round(rev, 2), "cardsSold": cards_sold, "balance": round(bal, 2),
            "gamesRevenue": round(grev, 2)}

@app.get("/api/admin/users")
def admin_users(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    out = []
    for r in rows:
        n = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (r["id"],)).fetchone()[0]
        out.append({"id": r["id"], "email": r["email"], "telegram": r["telegram"], "balance": r["balance"],
                    "rank": r["rank"], "orders": n, "createdAt": ts(r["created_at"])})
    conn.close()
    return out

@app.patch("/api/admin/users/{uid}")
def admin_patch_user(uid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if "balance" in payload:
        conn.execute("UPDATE users SET balance=? WHERE id=?", (payload["balance"], uid))
    if "rank" in payload:
        conn.execute("UPDATE users SET rank=? WHERE id=?", (payload["rank"], uid))
    if "telegram" in payload:
        conn.execute("UPDATE users SET telegram=? WHERE id=?", (payload["telegram"], uid))
    r = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.commit(); audit("user_patch", user["email"], f"target={uid} fields={sorted(payload.keys())}"); conn.close()
    if not r:
        raise ApiError(404, "Not found")
    return {"id": r["id"], "email": r["email"], "balance": r["balance"], "rank": r["rank"], "telegram": r["telegram"]}

@app.delete("/api/admin/users/{uid}")
def admin_del_user(uid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
    conn.commit(); audit("user_delete", user["email"], f"target={uid}"); conn.close()
    return {"ok": True}

@app.get("/api/admin/products")
def admin_products(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        vs = conn.execute("SELECT * FROM variants WHERE productId=?", (r["id"],)).fetchall()
        vv = []
        for v in vs:
            sc = conn.execute("SELECT COUNT(*) FROM stock_items WHERE variant_id=? AND sold=0",
                              (v["id"],)).fetchone()[0]
            vv.append({"id": v["id"], "name": v["name"], "price": v["price"], "stockCount": sc})
        out.append({"id": r["id"], "name": r["name"], "description": r["description"],
                    "image": r["image"], "active": bool(r["active"]), "pinned": bool(r["pinned"]),
                    "variants": vv})
    conn.close()
    return out

class ProductIn(BaseModel):
    name: str
    description: str = ""
    image: str = "/api/images/1"
    pinned: bool = False

@app.post("/api/admin/products")
def admin_add_product(data: ProductIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    pid = conn.execute("INSERT INTO products(name,description,image,active,pinned,owner_id,created_at) VALUES(?,?,?,1,?,?,?)",
                 (data.name, data.description, data.image, data.pinned, user["id"], now())).lastrowid
    conn.commit(); conn.close()
    return {"id": pid, "ok": True}

@app.patch("/api/admin/products/{pid}")
def admin_patch_product(pid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if "name" in payload:
        conn.execute("UPDATE products SET name=? WHERE id=?", (payload["name"], pid))
    if "description" in payload:
        conn.execute("UPDATE products SET description=? WHERE id=?", (payload["description"], pid))
    if "active" in payload:
        conn.execute("UPDATE products SET active=? WHERE id=?", (1 if payload["active"] else 0, pid))
    if "pinned" in payload:
        conn.execute("UPDATE products SET pinned=? WHERE id=?", (1 if payload["pinned"] else 0, pid))
    conn.commit(); conn.close()
    return {"ok": True}

@app.delete("/api/admin/products/{pid}")
def admin_del_product(pid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit(); conn.close()
    return {"ok": True}

class VariantIn(BaseModel):
    name: str
    price: float

@app.post("/api/admin/products/{pid}/variants")
def admin_add_variant(pid: int, data: VariantIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    vid = conn.execute("INSERT INTO variants(productId,name,price,comparePrice,minQuantity,created_at) VALUES(?,?,?,NULL,1,?)",
                 (pid, data.name, data.price, now())).lastrowid
    conn.commit(); conn.close()
    return {"id": vid, "ok": True}

class BulkStockIn(BaseModel):
    variantId: int
    contents: str

@app.post("/api/admin/stock/bulk")
def admin_bulk_stock(data: BulkStockIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    lines = [l.strip() for l in data.contents.splitlines() if l.strip()]
    conn = db()
    for l in lines:
        conn.execute("INSERT INTO stock_items(variant_id,content,created_at) VALUES(?,?,?)",
                     (data.variantId, l, now()))
    conn.commit(); conn.close()
    return {"ok": True, "added": len(lines)}

@app.patch("/api/admin/stock/{variant_id}")
def admin_set_stock(variant_id: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    target = int(payload.get("stockCount", 0))
    conn = db()
    cur_unsold = conn.execute("SELECT COUNT(*) FROM stock_items WHERE variant_id=? AND sold=0",
                              (variant_id,)).fetchone()[0]
    if target > cur_unsold:
        for _ in range(target - cur_unsold):
            conn.execute("INSERT INTO stock_items(variant_id,content,created_at) VALUES(?,?,?)",
                         (variant_id, f"{rand_str(7)}:{rand_str(10)}", now()))
    else:
        conn.execute("""DELETE FROM stock_items WHERE id IN
                        (SELECT id FROM stock_items WHERE variant_id=? AND sold=0 LIMIT ?)""",
                      (variant_id, cur_unsold - target))
    conn.commit(); conn.close()
    return {"ok": True, "stockCount": target}

@app.get("/api/admin/deposits")
def admin_deposits(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("""SELECT d.*, u.email FROM deposits d JOIN users u ON u.id=d.user_id
                           ORDER BY d.id DESC""").fetchall()
    conn.close()
    return [deposit_out(r, r["email"]) for r in rows]

@app.post("/api/admin/deposits/{did}/approve")
def approve_deposit(did: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    d = conn.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
    if not d:
        conn.close()
        raise ApiError(404, "Not found")
    cur = conn.execute("UPDATE deposits SET status='approved' WHERE id=? AND status='pending'", (did,))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Not a pending deposit")
    if d["reference"] and d["reference"].startswith("ORD"):
        try:
            oid = int(d["reference"].split("-")[0][3:])
            conn.execute("UPDATE orders SET status='completed' WHERE id=?", (oid,))
        except Exception:
            conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (d["amount"], d["user_id"]))
    else:
        conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (d["amount"], d["user_id"]))
    conn.commit()
    conn.close()
    audit("deposit_approve", user["email"], f"id={did} amount={d['amount']}")
    return {"ok": True}

@app.post("/api/admin/deposits/{did}/reject")
def reject_deposit(did: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    d = conn.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
    if d and d["reference"] and d["reference"].startswith("ORD"):
        try:
            oid = int(d["reference"].split("-")[0][3:])
            conn.execute("UPDATE stock_items SET sold=0, order_id=NULL WHERE sold=1 AND order_id=?", (oid,))
            conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
        except Exception:
            pass
    conn.execute("UPDATE deposits SET status='rejected' WHERE id=?", (did,))
    conn.commit()
    conn.close()
    audit("deposit_reject", user["email"], f"id={did}")
    return {"ok": True}

@app.get("/api/admin/discount-codes")
def admin_discounts(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("SELECT * FROM discount_codes ORDER BY id").fetchall()
    conn.close()
    return [{"id": r["id"], "code": r["code"], "percent": r["percent"], "active": bool(r["active"])} for r in rows]

class DiscountCodeIn(BaseModel):
    code: str
    percent: int

@app.post("/api/admin/discount-codes")
def admin_add_discount(data: DiscountCodeIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("INSERT OR IGNORE INTO discount_codes(code,percent,active) VALUES(?,?,1)",
                 (data.code.upper(), data.percent))
    conn.commit(); conn.close()
    return {"ok": True}

@app.delete("/api/admin/discount-codes/{did}")
def admin_del_discount(did: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("DELETE FROM discount_codes WHERE id=?", (did,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/api/admin/announcements")
def admin_announcements(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("SELECT * FROM announcements ORDER BY id DESC").fetchall()
    conn.close()
    return [{"id": r["id"], "content": r["content"], "link": r["link"], "active": bool(r["active"]),
             "bannerColor": r["bannerColor"], "createdAt": ts(r["created_at"])} for r in rows]

class AnnounceIn(BaseModel):
    content: str
    link: Optional[str] = None
    bannerColor: str = "#000000"

@app.post("/api/admin/announcements")
def admin_add_announcement(data: AnnounceIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("INSERT INTO announcements(content,link,active,bannerColor,created_at) VALUES(?,?,1,?,?)",
                 (data.content, data.link, data.bannerColor, now()))
    conn.commit(); conn.close()
    return {"ok": True}

@app.delete("/api/admin/announcements/{aid}")
def admin_del_announcement(aid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("DELETE FROM announcements WHERE id=?", (aid,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/api/admin/payment-methods")
def admin_get_pm(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    data = json.loads(get_setting(conn, "payment_methods"))
    conn.close()
    return data

@app.post("/api/admin/payment-methods")
def admin_set_pm(payload: Dict[str, bool], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    set_setting(conn, "payment_methods", json.dumps(payload))
    conn.commit(); conn.close()
    return {"ok": True}

@app.post("/api/admin/settings/features")
def admin_set_features(payload: Dict[str, bool], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    set_setting(conn, "features", json.dumps(payload))
    conn.commit(); conn.close()
    return {"ok": True}

@app.post("/api/admin/site-settings")
def admin_site_settings(payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if "manual_payments" in payload:
        set_setting(conn, "manual_payments", json.dumps(payload["manual_payments"]))
    if "min_deposits" in payload:
        set_setting(conn, "min_deposits", json.dumps(payload["min_deposits"]))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/api/admin/integrations/status")
def admin_integrations(user: dict = Depends(get_current_user)):
    require_admin(user)
    return {"telegram": True, "bitcoin": True, "cashapp": True, "authorizeNet": False}

class ConfirmIn(BaseModel):
    confirm: str

@app.post("/api/admin/clear-all-data")
def clear_all(data: ConfirmIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    if data.confirm != "YES":
        raise ApiError(400, "Send confirm=YES")
    conn = db()
    for t in ["orders", "stock_items", "cards", "deposits", "game_history", "games_mines",
              "tokens", "password_resets"]:
        conn.execute(f"DELETE FROM {t}")
    conn.commit(); audit("clear_all", user["email"]); conn.close()
    return {"ok": True}

@app.get("/api/admin/codes")
def admin_codes(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("SELECT * FROM wallet_codes ORDER BY id DESC").fetchall()
    conn.close()
    return [{"id": r["id"], "code": r["code"], "value": r["value"], "used": bool(r["used"])} for r in rows]

class CodeIn(BaseModel):
    code: str
    value: float

@app.post("/api/admin/codes")
def admin_add_code(data: CodeIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("INSERT OR IGNORE INTO wallet_codes(code,value,used,created_at) VALUES(?,?,0,?)",
                 (data.code.upper(), data.value, now()))
    conn.commit(); audit("code_add", user["email"], f"{data.code}=${data.value}"); conn.close()
    return {"ok": True}

@app.delete("/api/admin/codes/{cid}")
def admin_del_code(cid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("DELETE FROM wallet_codes WHERE id=?", (cid,))
    conn.commit(); conn.close()
    return {"ok": True}

# ================= SELLER + VOUCHES (PHASE F) =================
class SellerApplyIn(BaseModel):
    telegram: str
    motivation: Optional[str] = None

@app.post("/api/seller/apply")
def seller_apply(data: SellerApplyIn, user: dict = Depends(get_current_user)):
    conn = db()
    conn.execute("""INSERT INTO seller_apps(user_id,telegram,motivation,status,created_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET telegram=excluded.telegram,
                    motivation=excluded.motivation, status='pending'""",
                 (user["id"], data.telegram, data.motivation, "pending", now()))
    conn.commit(); conn.close()
    return {"status": "pending"}

@app.get("/api/seller/me")
def seller_me(user: dict = Depends(get_current_user)):
    conn = db()
    r = conn.execute("SELECT * FROM seller_apps WHERE user_id=?", (user["id"],)).fetchone()
    n = conn.execute("SELECT COUNT(*) FROM products WHERE owner_id=?", (user["id"],)).fetchone()[0]
    conn.close()
    if not r:
        return {"status": "none", "products": n}
    return {"status": r["status"], "products": n}

@app.post("/api/vouches/create-token")
def vouch_token(user: dict = Depends(get_current_user)):
    token = "VOUCH-" + secrets.token_hex(4).upper()
    conn = db()
    conn.execute("INSERT INTO vouches(giver_id,token,created_at) VALUES(?,?,?)", (user["id"], token, now()))
    conn.commit(); conn.close()
    return {"token": token}

@app.get("/api/vouches")
def my_vouches(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM vouches WHERE giver_id=?", (user["id"],)).fetchall()
    conn.close()
    return [{"token": r["token"], "usedBy": r["used_by"], "createdAt": ts(r["created_at"])} for r in rows]

# ================= SUPPORT + MISC (PHASE G) =================
class TicketIn(BaseModel):
    subject: str
    body: str

@app.post("/api/support")
def create_ticket(data: TicketIn, user: dict = Depends(get_current_user)):
    conn = db()
    tid = conn.execute("INSERT INTO tickets(user_id,subject,body,status,created_at) VALUES(?,?,?,'open',?)",
                 (user["id"], data.subject, data.body, now())).lastrowid
    conn.commit(); tg_notify(f"[{SITE}] Ticket #{tid} from {user['email']}: {data.subject}"); conn.close()
    return {"id": tid, "status": "open"}

@app.get("/api/support")
def my_tickets(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    out = []
    for r in rows:
        reps = conn.execute("SELECT * FROM ticket_replies WHERE ticket_id=? ORDER BY id", (r["id"],)).fetchall()
        out.append({"id": r["id"], "subject": r["subject"], "body": r["body"], "status": r["status"],
                    "createdAt": ts(r["created_at"]),
                    "replies": [{"body": x["body"], "createdAt": ts(x["created_at"])} for x in reps]})
    conn.close()
    return out

@app.get("/api/admin/support")
def admin_tickets(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("""SELECT t.*, u.email FROM tickets t JOIN users u ON u.id=t.user_id
                           ORDER BY t.id DESC""").fetchall()
    conn.close()
    return [{"id": r["id"], "email": r["email"], "subject": r["subject"], "body": r["body"],
             "status": r["status"], "createdAt": ts(r["created_at"])} for r in rows]

class ReplyIn(BaseModel):
    body: str

@app.post("/api/admin/support/{tid}/reply")
def admin_reply(tid: int, data: ReplyIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("INSERT INTO ticket_replies(ticket_id,admin_id,body,created_at) VALUES(?,?,?,?)",
                 (tid, user["id"], data.body, now()))
    conn.commit(); conn.close()
    return {"ok": True}

@app.post("/api/admin/support/{tid}/close")
def admin_close_ticket(tid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("UPDATE tickets SET status='closed' WHERE id=?", (tid,))
    conn.commit(); conn.close()
    return {"ok": True}

class ForgotIn(BaseModel):
    email: str

@app.post("/api/password/forgot")
def password_forgot(data: ForgotIn):
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE email=?", (data.email,)).fetchone()
    sent = False
    if u:
        code = str(random.randint(100000, 999999))
        conn.execute("INSERT OR REPLACE INTO password_resets(email,code,expires,used) VALUES(?,?,?,0)",
                     (data.email, code, now() + 900))
        sent = send_reset_email(data.email, code)
        audit("pw_reset_request", data.email, f"email_sent={sent}")
        tg_notify(f"[{SITE}] Password reset code for {data.email}: {code}")
        conn.commit()
        print(f"[{SITE}] reset code for {data.email}: {code}")
    conn.close()
    resp = {"message": "If the email exists, a reset code was issued"}
    if sent:
        resp["delivered"] = "email"
    elif RESET_DEV_CODE and u:
        resp["devCode"] = code   # local/dev convenience only; set RESET_DEV_CODE=false in production
    return resp

class ResetIn(BaseModel):
    email: str
    code: str
    newPassword: str

@app.post("/api/password/reset")
def password_reset(data: ResetIn):
    conn = db()
    r = conn.execute("SELECT * FROM password_resets WHERE email=? AND code=? AND used=0 AND expires>?",
                     (data.email, data.code, now())).fetchone()
    if not r:
        conn.close()
        raise ApiError(400, "Invalid code")
    conn.execute("UPDATE password_resets SET used=1 WHERE email=? AND code=?", (data.email, data.code))
    conn.execute("UPDATE users SET password_hash=? WHERE email=?",
                 (h(data.newPassword, data.email), data.email))
    conn.execute("DELETE FROM tokens WHERE user_id=(SELECT id FROM users WHERE email=?)", (data.email,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/api/telegram/daily-rewards/link-code")
def tg_code(user: dict = Depends(get_current_user)):
    conn = db()
    r = conn.execute("SELECT * FROM telegram_claims WHERE user_id=?", (user["id"],)).fetchone()
    today = time.strftime("%Y-%m-%d")
    claimed = bool(r and time.strftime("%Y-%m-%d", time.localtime(r["last_claim"])) == today)
    conn.close()
    return {"code": "TG-" + secrets.token_hex(3).upper(), "reward": 0.5, "claimed": claimed}

@app.post("/api/telegram/daily-rewards/link-code")
def tg_claim(user: dict = Depends(get_current_user)):
    conn = db()
    today_start = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))
    cur = conn.execute("UPDATE telegram_claims SET last_claim=? WHERE user_id=? AND last_claim<?",
                       (now(), user["id"], today_start))
    if cur.rowcount == 0:
        ex = conn.execute("SELECT 1 FROM telegram_claims WHERE user_id=?", (user["id"],)).fetchone()
        if ex:
            conn.close()
            raise ApiError(400, "Already claimed today")
        conn.execute("INSERT INTO telegram_claims(user_id,last_claim) VALUES(?,?)", (user["id"], now()))
    conn.execute("UPDATE users SET balance=balance+0.5 WHERE id=?", (user["id"],))
    conn.commit(); conn.close()
    return {"claimed": True, "reward": 0.5}

@app.post("/api/upload")
def upload(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    require_admin(user)
    safe = "".join(ch for ch in file.filename if ch.isalnum() or ch in "._-")
    name = f"{secrets.token_hex(6)}_{safe}"
    with open(os.path.join(UPLOADS, name), "wb") as f:
        f.write(file.file.read())
    return {"url": f"/uploads/{name}"}

# 1. single product (product detail page)
@app.get("/api/products/{pid}")
def product_get(pid: int):
    conn = db()
    r = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not r:
        conn.close()
        raise ApiError(404, "Not found")
    vs = conn.execute("SELECT * FROM variants WHERE productId=?", (pid,)).fetchall()
    vv = []
    for v in vs:
        sc = conn.execute("SELECT COUNT(*) FROM stock_items WHERE variant_id=? AND sold=0", (v["id"],)).fetchone()[0]
        vv.append({"id": v["id"], "productId": v["productId"], "name": v["name"], "price": v["price"],
                  "comparePrice": v["comparePrice"], "minQuantity": v["minQuantity"], "stockCount": sc})
    conn.close()
    return {"id": r["id"], "name": r["name"], "description": r["description"], "image": r["image"],
            "active": bool(r["active"]), "pinned": bool(r["pinned"]), "createdAt": ts(r["created_at"]),
            "variants": vv}

# 2. crypto CHECKOUT — reserves stock, links order to deposit, admin approval completes the order
class CryptoOrderIn(BaseModel):
    variantId: int
    quantity: int = 1
    currency: str = "BTC"

@app.post("/api/orders/crypto")
def order_crypto(data: CryptoOrderIn, user: dict = Depends(get_current_user)):
    if data.quantity < 1:
        raise ApiError(400, "Invalid quantity")
    if data.variantId <= 0:
        raise ApiError(400, "Invalid variant")
    conn = db()
    v = conn.execute("SELECT * FROM variants WHERE id=?", (data.variantId,)).fetchone()
    if not v:
        conn.close()
        raise ApiError(404, "Not found")
    stock = conn.execute("SELECT * FROM stock_items WHERE variant_id=? AND sold=0 LIMIT ?",
                         (data.variantId, data.quantity)).fetchall()
    if len(stock) < data.quantity:
        conn.close()
        raise ApiError(400, "Insufficient stock")
    total = round(v["price"] * data.quantity, 2)
    items = [s["content"] for s in stock]
    cur = conn.execute("INSERT INTO orders(user_id,variant_id,quantity,total,status,items,created_at) VALUES(?,?,?,?,?,?,?)",
                       (user["id"], data.variantId, data.quantity, total, "pending_payment", json.dumps(items), now()))
    oid = cur.lastrowid
    for s in stock:
        conn.execute("UPDATE stock_items SET sold=1, order_id=? WHERE id=?", (oid, s["id"]))
    mp = json.loads(get_setting(conn, "manual_payments"))
    addr = mp.get("bitcoin", "bc1qph")
    if data.currency == "ETH":
        addr = mp.get("ethereum", "0x")
    elif data.currency == "LTC":
        addr = mp.get("litecoin", "ltc1q")
    ref = f"ORD{oid}-{secrets.token_hex(3).upper()}"
    conn.execute("INSERT INTO deposits(user_id,method,amount,reference,status,created_at) VALUES(?,?,?,?,?,?)",
                 (user["id"], f"crypto:{data.currency}", total, ref, "pending", now()))
    conn.commit()
    conn.close()
    tg_notify(f"[{SITE}] CRYPTO ORDER #{oid} pending by {user['email']}: ${total}")
    return {"orderId": oid, "address": addr, "amount": total, "currency": data.currency,
            "reference": ref, "status": "pending"}

# 3. public variants listing
@app.get("/api/variants")
def variants_list():
    conn = db()
    rows = conn.execute("SELECT * FROM variants").fetchall()
    out = []
    for v in rows:
        sc = conn.execute("SELECT COUNT(*) FROM stock_items WHERE variant_id=? AND sold=0", (v["id"],)).fetchone()[0]
        out.append({"id": v["id"], "productId": v["productId"], "name": v["name"], "price": v["price"],
                    "comparePrice": v["comparePrice"], "minQuantity": v["minQuantity"], "stockCount": sc})
    conn.close()
    return out

# 4. user-side deposit management (cancel a pending one)
@app.patch("/api/deposits/{did}")
def user_patch_deposit(did: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    conn = db()
    d = conn.execute("SELECT * FROM deposits WHERE id=? AND user_id=?", (did, user["id"])).fetchone()
    if not d:
        conn.close()
        raise ApiError(404, "Not found")
    status = d["status"]
    if payload.get("status") == "cancelled" and d["status"] == "pending":
        if d["reference"] and d["reference"].startswith("ORD"):
            try:
                oid = int(d["reference"].split("-")[0][3:])
                conn.execute("UPDATE stock_items SET sold=0, order_id=NULL WHERE sold=1 AND order_id=?", (oid,))
                conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
            except Exception:
                pass
        status = "cancelled"
        conn.execute("UPDATE deposits SET status='cancelled' WHERE id=?", (did,))
    conn.commit()
    conn.close()
    return {"ok": True, "status": status}

# 5. user's single card detail
@app.get("/api/cards/{cid}")
def user_card(cid: int, user: dict = Depends(get_current_user)):
    conn = db()
    r = conn.execute("SELECT * FROM cards WHERE id=? AND buyer_id=?", (cid, user["id"])).fetchone()
    conn.close()
    if not r:
        raise ApiError(404, "Not found")
    return {"id": r["id"], "number": r["number"], "expMonth": r["exp_month"], "expYear": r["exp_year"],
            "cvv": r["cvv"], "checked": bool(r["checked"]), "valid": r["valid"]}

@app.get("/api/admin/orders")
def admin_orders(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("""SELECT o.*, u.email FROM orders o JOIN users u ON u.id=o.user_id
                           ORDER BY o.id DESC""").fetchall()
    conn.close()
    return [{"id": r["id"], "email": r["email"], "variantId": r["variant_id"], "quantity": r["quantity"],
             "total": r["total"], "status": r["status"], "createdAt": ts(r["created_at"]),
             "items": json.loads(r["items"] or "[]")} for r in rows]

@app.get("/api/admin/settings/features")
def admin_get_features(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    data = json.loads(get_setting(conn, "features"))
    conn.close()
    return data

@app.get("/api/admin/settings/min-deposits")
def admin_get_min_deposits(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    data = json.loads(get_setting(conn, "min_deposits"))
    conn.close()
    return data

@app.post("/api/admin/settings/min-deposits")
def admin_set_min_deposits(payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if "method" in payload and "min" in payload:
        data = _json_setting(conn, "min_deposits", {}) or {}
        data[payload["method"]] = float(payload["min"])
    else:
        data = payload
    set_setting(conn, "min_deposits", json.dumps(data))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.patch("/api/admin/payment-methods/{name}")
def admin_toggle_pm(name: str, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    data = json.loads(get_setting(conn, "payment_methods"))
    data[name] = bool(payload.get("enabled", not data.get(name, False)))
    set_setting(conn, "payment_methods", json.dumps(data))
    conn.commit()
    conn.close()
    return {"ok": True}

class CardBaseIn(BaseModel):
    name: str
    price: float

@app.post("/api/admin/card-bases")
def admin_add_base(data: CardBaseIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("INSERT INTO card_bases(name,price,active) VALUES(?,?,1)", (data.name, data.price))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.patch("/api/admin/card-bases/{bid}")
def admin_patch_base(bid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if "name" in payload:
        conn.execute("UPDATE card_bases SET name=? WHERE id=?", (payload["name"], bid))
    if "price" in payload:
        conn.execute("UPDATE card_bases SET price=? WHERE id=?", (payload["price"], bid))
    if "active" in payload:
        conn.execute("UPDATE card_bases SET active=? WHERE id=?", (1 if payload["active"] else 0, bid))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/admin/card-bases/{bid}")
def admin_del_base(bid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("DELETE FROM cards WHERE base_id=?", (bid,))
    conn.execute("DELETE FROM card_bases WHERE id=?", (bid,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/admin/cards")
def admin_cards(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("""SELECT ca.*, cb.name AS base_name, bu.email AS buyer FROM cards ca
        JOIN card_bases cb ON cb.id=ca.base_id
        LEFT JOIN users bu ON bu.id=ca.buyer_id
        ORDER BY ca.id DESC LIMIT 500""").fetchall()
    conn.close()
    return [{"id": r["id"], "base": r["base_name"], "number": r["number"], "expMonth": r["exp_month"],
             "expYear": r["exp_year"], "cvv": r["cvv"], "sold": bool(r["sold"]), "buyer": r["buyer"],
             "checked": bool(r["checked"]), "valid": r["valid"]} for r in rows]

@app.patch("/api/admin/variants/{vid}")
def admin_patch_variant(vid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if "name" in payload:
        conn.execute("UPDATE variants SET name=? WHERE id=?", (payload["name"], vid))
    if "price" in payload:
        conn.execute("UPDATE variants SET price=? WHERE id=?", (payload["price"], vid))
    if "minQuantity" in payload:
        conn.execute("UPDATE variants SET minQuantity=? WHERE id=?", (payload["minQuantity"], vid))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.delete("/api/admin/variants/{vid}")
def admin_del_variant(vid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("DELETE FROM stock_items WHERE variant_id=?", (vid,))
    conn.execute("DELETE FROM variants WHERE id=?", (vid,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/admin/announcements/config")
def admin_ann_config(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("SELECT bannerColor FROM announcements ORDER BY id DESC LIMIT 1").fetchall()
    conn.close()
    return {"bannerColor": rows[0]["bannerColor"] if rows else "#000000"}

@app.post("/api/admin/settings/maintenance")
def set_maintenance(payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    set_setting(conn, "maintenance", "1" if payload.get("enabled") else "0")
    conn.commit()
    conn.close()
    return {"ok": True}

# license-file bulk card import (accepts number|mm|yyyy|cvv or number:mm:yyyy:cvv per line)
@app.post("/api/admin/stock/license-file")
async def admin_license_file(baseId: int, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    require_admin(user)
    raw = (await file.read()).decode("utf-8", errors="ignore")
    conn = db()
    b = conn.execute("SELECT id FROM card_bases WHERE id=?", (baseId,)).fetchone()
    if not b:
        conn.close()
        raise ApiError(404, "Not found")
    added = 0
    for line in raw.splitlines():
        parts = [p.strip() for p in line.replace(":", "|").split("|")]
        if len(parts) == 4 and parts[0].isdigit() and len(parts[0]) >= 13:
            try:
                conn.execute("INSERT INTO cards(base_id,number,exp_month,exp_year,cvv) VALUES(?,?,?,?,?)",
                             (baseId, parts[0], int(parts[1]), int(parts[2]), parts[3]))
                added += 1
            except Exception:
                pass
    conn.commit()
    conn.close()
    return {"ok": True, "added": added}

@app.get("/api/worker/card-bases")
def worker_bases(user: dict = Depends(get_current_user)):
    if user["rank"] not in ("admin", "worker"):
        raise ApiError(403, "Worker or admin only")
    conn = db()
    rows = conn.execute("SELECT * FROM card_bases WHERE active=1").fetchall()
    out = []
    for r in rows:
        sc = conn.execute("SELECT COUNT(*) FROM cards WHERE base_id=? AND sold=0", (r["id"],)).fetchone()[0]
        out.append({"id": r["id"], "name": r["name"], "price": r["price"], "stockCount": sc})
    conn.close()
    return out

@app.get("/api/worker/orders")
def worker_orders(user: dict = Depends(get_current_user)):
    if user["rank"] not in ("admin", "worker"):
        raise ApiError(403, "Worker or admin only")
    conn = db()
    rows = conn.execute("""SELECT o.*, u.email FROM orders o JOIN users u ON u.id=o.user_id
                           ORDER BY o.id DESC LIMIT 200""").fetchall()
    conn.close()
    return [{"id": r["id"], "email": r["email"], "variantId": r["variant_id"], "quantity": r["quantity"],
             "status": r["status"], "createdAt": ts(r["created_at"])} for r in rows]

@app.get("/api/admin/settings/{key}")
def admin_get_setting(key: str, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if key in MANUAL_SETTING_KEY.values():
        method = key.rsplit("-", 1)[0]
        field = MANUAL_HANDLE_FIELD.get(method, "handle")
        val = manual_payments_public(conn).get(method, {}).get(field, "")
        conn.close()
        return {field: val}
    if key.endswith("-fee"):
        try:
            fee = float(_json_setting(conn, key, 0) or 0)
        except (TypeError, ValueError):
            fee = 0.0
        conn.close()
        return {"fee": fee}
    conn.close()
    raise ApiError(404, "Unknown setting")

@app.post("/api/admin/settings/{key}")
def admin_set_setting(key: str, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if key in MANUAL_SETTING_KEY.values():
        field = "tag" if key.endswith("-tag") else "handle"
        val = str(payload.get(field) or payload.get("handle") or payload.get("tag") or "")
        set_setting(conn, key, json.dumps(val))
        conn.commit(); conn.close()
        audit("setting_set", user["email"], f"{key}={val}")
        return {"ok": True}
    if key.endswith("-fee"):
        try:
            fee = float(payload.get("fee", 0) or 0)
        except (TypeError, ValueError):
            fee = 0.0
        set_setting(conn, key, json.dumps(fee))
        conn.commit(); conn.close()
        audit("setting_set", user["email"], f"{key}={fee}")
        return {"ok": True}
    conn.close()
    raise ApiError(404, "Unknown setting")

# ================= STATIC (mount LAST) =================
app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="spa")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
