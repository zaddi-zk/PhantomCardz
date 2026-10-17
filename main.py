#!/usr/bin/env python3
"""PhantomCardz — cards & accounts marketplace backend (FastAPI + SQLite)."""
import os, re, secrets, random, time, hashlib, string, json, threading
import smtplib
from datetime import datetime
from email.message import EmailMessage
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Depends, Request, UploadFile, File
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.exceptions import HTTPException as StarletteHTTPException
from database import INTEGRITY_ERRORS, connect_db, insert_id

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
SOL_ADDR     = os.getenv("SOL_ADDRESS", "PhantomSoL1111111111111111111111111111111111")
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
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
VOUCH_TG_URL = os.getenv("TELEGRAM_VOUCH_URL", "https://t.me/+3-lMkt-idutkOTIx")
TELEGRAM_CHANNEL_URL = os.getenv("TELEGRAM_CHANNEL_URL", "").strip() or "https://t.me/phantomCardz"

try:
    import payments as npp   # NOWPayments integration (optional)
except Exception:            # pragma: no cover - optional dependency
    npp = None

security = HTTPBearer(auto_error=False)

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message

def db():
    return connect_db(DB_PATH)

# Maintenance flag cache: avoids a settings round-trip on every API request.
_maint = {"value": "0", "expires": 0.0}

PASSWORD_SALT = os.getenv("PASSWORD_SALT", "PhantomCardz-pbkdf2-static-v1")

def h(pw: str, salt: str = "") -> str:
    """Hash a password with a stable application salt (email no longer used).

    The salt used to be the user's email, which made an email change
    impossible without the plaintext password. A global salt keeps password
    verification stable across email changes; legacy email-salted hashes
    still verify and are upgraded on next login.
    """
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), PASSWORD_SALT.encode(), 200_000)
    return "pbkdf2$" + dk.hex()

def _legacy_hash(pw: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), (salt or "").encode(), 200_000)
    return "pbkdf2$" + dk.hex()

def verify_pw(pw: str, salt: str, stored: str) -> bool:
    if stored.startswith("pbkdf2$"):
        if secrets.compare_digest(h(pw), stored):
            return True
        return bool(salt) and secrets.compare_digest(_legacy_hash(pw, salt), stored)
    return secrets.compare_digest(hashlib.sha256(((salt or "") + pw).encode()).hexdigest(), stored)

def now() -> int:
    return int(time.time())

def ts(v) -> str:
    """Serialize an epoch-seconds timestamp as an ISO-8601 UTC string.

    The frozen SPA dates everything with ``new Date(value)``, which only
    understands ISO strings (or epoch milliseconds as numbers) — not the
    ``{_seconds}`` wrapper an earlier port emitted, so every ``createdAt``
    rendered as "—". ISO keeps it parseable everywhere.
    """
    try:
        secs = int(v or 0)
    except (TypeError, ValueError):
        return ""
    if secs <= 0:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))

def to_cents(v) -> int:
    """Dollars (as sent by some legacy client inputs) -> integer cents."""
    try:
        return int(round(float(v) * 100))
    except (TypeError, ValueError):
        return 0

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
CREATE TABLE IF NOT EXISTS order_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER, variant_id INTEGER, quantity INTEGER,
    price REAL, contents TEXT
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
    used_by INTEGER, order_id TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS tickets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, subject TEXT, body TEXT,
    order_id TEXT, image_url TEXT,
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
CREATE TABLE IF NOT EXISTS telegram_link_codes(
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at INTEGER,
    used INTEGER DEFAULT 0
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

INDEX_SCHEMA = """
    CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
    CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
    CREATE INDEX IF NOT EXISTS idx_stock_variant ON stock_items(variant_id, sold);
    CREATE INDEX IF NOT EXISTS idx_cards_base ON cards(base_id, sold);
    CREATE INDEX IF NOT EXISTS idx_cards_buyer ON cards(buyer_id);
    CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(user_id);
    CREATE INDEX IF NOT EXISTS idx_gamehist_user ON game_history(user_id);
    CREATE INDEX IF NOT EXISTS idx_products_active ON products(active, pinned);
"""

def _has_col(conn, table, col):
    return col.lower() in {c.lower() for c in conn.table_columns(table)}

def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.executescript(INDEX_SCHEMA)
    if not _has_col(conn, "users", "username"):
        conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
    if not _has_col(conn, "users", "is_banned"):
        conn.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    if not _has_col(conn, "card_bases", "refundable"):
        conn.execute("ALTER TABLE card_bases ADD COLUMN refundable INTEGER DEFAULT 0")
    if not _has_col(conn, "card_bases", "hrPercent"):
        conn.execute("ALTER TABLE card_bases ADD COLUMN hrPercent REAL DEFAULT 0")
    # Payment-provider tracking (NOWPayments). Added lazily for existing DBs.
    for _col, _ddl in (("provider", "TEXT"), ("provider_id", "TEXT"),
                       ("pay_currency", "TEXT"), ("pay_amount", "TEXT"),
                       ("pay_address", "TEXT"), ("payin_extra_id", "TEXT"),
                       ("checkout_url", "TEXT"), ("meta", "TEXT"),
                       ("updated_at", "INTEGER"), ("settled_at", "INTEGER")):
        if not _has_col(conn, "deposits", _col):
            conn.execute(f"ALTER TABLE deposits ADD COLUMN {_col} {_ddl}")
    # SPA order model: human order id, payment metadata and delivery payload.
    for _col, _ddl in (("order_id", "TEXT"), ("payment_method", "TEXT DEFAULT ''"),
                       ("payment_note", "TEXT DEFAULT ''"), ("paid_amount", "REAL DEFAULT 0"),
                       ("delivery_content", "TEXT DEFAULT ''")):
        if not _has_col(conn, "orders", _col):
            conn.execute(f"ALTER TABLE orders ADD COLUMN {_col} {_ddl}")
    for _col, _ddl in (("item_type", "TEXT DEFAULT 'product'"), ("card_id", "INTEGER")):
        if not _has_col(conn, "order_items", _col):
            conn.execute(f"ALTER TABLE order_items ADD COLUMN {_col} {_ddl}")
    # SPA discount-code model (type/value/min order/usage/expiry).
    for _col, _ddl in (("type", "TEXT DEFAULT 'percent'"), ("value", "REAL DEFAULT 0"),
                       ("min_order", "REAL DEFAULT 0"), ("max_uses", "INTEGER"),
                       ("used_count", "INTEGER DEFAULT 0"), ("expires_at", "INTEGER")):
        if not _has_col(conn, "discount_codes", _col):
            conn.execute(f"ALTER TABLE discount_codes ADD COLUMN {_col} {_ddl}")
    conn.execute("UPDATE discount_codes SET value=percent WHERE (value IS NULL OR value=0) AND percent IS NOT NULL")
    conn.execute("UPDATE orders SET order_id='ORD-'||id WHERE order_id IS NULL OR order_id=''")
    if not _has_col(conn, "vouches", "order_id"):
        conn.execute("ALTER TABLE vouches ADD COLUMN order_id TEXT")
    for _col, _ddl in (("order_id", "TEXT"), ("image_url", "TEXT")):
        if not _has_col(conn, "tickets", _col):
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {_col} {_ddl}")
    if not _has_col(conn, "users", "telegram_id"):
        conn.execute("ALTER TABLE users ADD COLUMN telegram_id TEXT")
    # Per-card free text the SPA parses for city/state/zip (and delivers to the buyer).
    if not _has_col(conn, "cards", "extras"):
        conn.execute("ALTER TABLE cards ADD COLUMN extras TEXT DEFAULT ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")

MONEY_COLUMNS = [
    ("users", "balance"),
    ("variants", "price"),
    ("variants", "comparePrice"),
    ("orders", "total"),
    ("wallet_codes", "value"),
    ("deposits", "amount"),
    ("card_bases", "price"),
    ("games_mines", "amount"),
    ("game_history", "amount"),
    ("game_history", "payout"),
]

def migrate_money_to_cents(conn):
    """One-time conversion of stored money from dollars to integer cents.

    The web client renders all money as ``value / 100``; the original port
    wrote dollar amounts, leaving every price/balance 100x too small. Guarded
    by the ``money_unit`` setting so it runs exactly once per database.
    """
    if get_setting(conn, "money_unit", "") == "cents":
        return
    for table, col in MONEY_COLUMNS:
        try:
            conn.execute(f"UPDATE {table} SET {col}=ROUND({col}*100) WHERE {col} IS NOT NULL")
        except Exception as e:
            audit("money_migrate_skip", "-", f"{table}.{col}: {e}")
    set_setting(conn, "money_unit", "cents")
    conn.commit()
    audit("money_migrate", "-", "money unit migrated to cents")

def init_db():
    conn = db()
    ensure_schema(conn)
    migrate_money_to_cents(conn)
    c = conn.cursor()
    # Guarantee the configured admin account exists and can reach /admin.
    admin_row = c.execute("SELECT id, rank FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
    if admin_row is None:
        c.execute("INSERT INTO users(email,password_hash,telegram,balance,rank,created_at) VALUES(?,?,?,?,?,?)",
                  (ADMIN_EMAIL, h(ADMIN_PASS, ADMIN_EMAIL), ADMIN_TG, 100000.0, "admin", now()))
    elif admin_row["rank"] != "admin":
        c.execute("UPDATE users SET rank='admin' WHERE id=?", (admin_row["id"],))
        audit("admin_promote", ADMIN_EMAIL, "startup rank repair")
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
            ("Hotmail FA", "Login and loot for goodies, easiest way to make a band!", 1, "Hotmails FA (Random)", 500.0, 25),
            ("Outlook FA", "Fresh farmed outlooks, ready for work.", 0, "Outlooks FA (Random)", 3000.0, 40),
            ("Combo Lists", "Fresh combo lists for cracking.", 0, "Combos (10k lines)", 1000.0, 50),
        ]
        for name, desc, pinned, vname, price, n in seeds:
            pid = insert_id(c, "INSERT INTO products(name,description,image,active,pinned,owner_id,created_at) VALUES(?,?,?,1,?,1,?)",
                            (name, desc, "/api/images/1", pinned, now()))
            vid = insert_id(c, "INSERT INTO variants(productId,name,price,comparePrice,minQuantity,created_at) VALUES(?,?,?,NULL,1,?)",
                            (pid, vname, price, now()))
            for _ in range(n):
                c.execute("INSERT INTO stock_items(variant_id,content,created_at) VALUES(?,?,?)",
                          (vid, f"{rand_str(7)}@hotmail.com:{rand_str(10).capitalize()}1!", now()))
    if c.execute("SELECT COUNT(*) FROM card_bases").fetchone()[0] == 0:
        for name, price in [("US Classic", 500.0), ("Premium Mix", 1200.0)]:
            bid = insert_id(c, "INSERT INTO card_bases(name,price,active) VALUES(?,?,1)", (name, price))
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
        "features": json.dumps({"checker": True, "reseller": True, "ranks": True, "logs": True, "cards": True, "plinko": True}),
        "payment_methods": json.dumps({"wallet": True, "cashapp": True, "crypto": True, "stars": False,
                                       "venmo": False, "zelle": False, "chime": True, "applepay": False}),
    }
    for k, v in defaults.items():
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING", (k, v))
    # One-time: the games feature was historically seeded off. Flip Plinko on so
    # the game unlocks; admins can still turn it back off from the panel.
    if get_setting(conn, "features_plinko_default", "") != "1":
        try:
            feats = json.loads(get_setting(conn, "features", "{}")) or {}
        except Exception:
            feats = {}
        feats["plinko"] = True
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  ("features", json.dumps(feats)))
        set_setting(conn, "features_plinko_default", "1")
    conn.commit()
    conn.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        start_telegram_poller()
    except Exception as e:
        audit("telegram_poller_error", "-", str(e))
    yield

app = FastAPI(title=f"{SITE} API", version="1.0.0", lifespan=lifespan)

from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(TrustedHostMiddleware, allowed_hosts=DOMAINS + ["testserver", "*.onrender.com"])
app.add_middleware(CORSMiddleware,
    allow_origins=[f"https://{d}" for d in DOMAINS] + [f"http://{d}" for d in DOMAINS] + ["http://localhost:8000"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def maintenance_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") and path not in ("/api/health", "/api/health/", "/api/login", "/api/admin/settings/maintenance"):
        if time.time() >= _maint["expires"]:
            conn = db()
            _maint["value"] = get_setting(conn, "maintenance", "0")
            conn.close()
            _maint["expires"] = time.time() + 2.0
        if _maint["value"] == "1":
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
    u = conn.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",
                     (token,)).fetchone()
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

def role_flags(u) -> dict:
    # The web client expects {role, isWorker, isBanned}; the DB stores rank.
    rank = u["rank"]
    banned = u.get("is_banned") if hasattr(u, "get") else None
    return {"role": rank, "isWorker": rank == "worker", "isBanned": bool(banned)}

def user_out(u) -> dict:
    tg = u.get("telegram") if hasattr(u, "get") else None
    return {"id": u["id"], "email": u["email"], "username": u["username"],
            "balance": u["balance"], "protectedBalance": 0,
            "rank": u["rank"], "telegramUsername": (tg.lstrip("@") if tg else None),
            "telegramId": (u.get("telegram_id") if hasattr(u, "get") else None),
            "lastDailySpin": None, "createdAt": ts(u["created_at"]),
            **role_flags(u)}

def get_setting(conn, key: str, fallback="{}"):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else fallback

def set_setting(conn, key: str, value: str):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
    if key == "maintenance":
        _maint["expires"] = 0.0

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
    if u.get("is_banned"):
        conn.close()
        raise ApiError(403, "Account suspended")
    if not secrets.compare_digest(u["password_hash"], h(data.password)):
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (h(data.password), u["id"]))
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
    except INTEGRITY_ERRORS:
        conn.close()
        raise ApiError(400, "Email already exists")

@app.get("/api/user")
def me(user: dict = Depends(get_current_user)):
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (user["id"],)).fetchone()[0]
    spent = conn.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE user_id=? AND status='completed'",
                         (user["id"],)).fetchone()[0]
    conn.close()
    return {**user_out(user), "telegram": user["telegram"], "orders": n,
            "totalSpent": round(spent, 2)}

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
@app.patch("/api/user/password")
def change_password(data: PasswordIn, user: dict = Depends(get_current_user)):
    if len(data.newPassword or "") < 6:
        raise ApiError(400, "New password must be at least 6 characters")
    conn = db()
    if not verify_pw(data.currentPassword, user["email"], user["password_hash"]):
        conn.close()
        raise ApiError(400, "Wrong password")
    if data.currentPassword == data.newPassword:
        conn.close()
        raise ApiError(400, "New password must differ from the current password")
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (h(data.newPassword, user["email"]), user["id"]))
    conn.commit()
    conn.close()
    audit("change_password", user["email"], "password updated")
    return {"ok": True}

class EmailIn(BaseModel):
    password: Optional[str] = None
    newEmail: Optional[str] = None
    email: Optional[str] = None

@app.post("/api/user/email")
@app.patch("/api/user/email")
def change_email(data: EmailIn, user: dict = Depends(get_current_user)):
    new_email = (data.newEmail or data.email or "").strip().lower()
    if "@" not in new_email or "." not in new_email.rsplit("@", 1)[-1] or " " in new_email:
        raise ApiError(400, "Enter a valid email address")
    conn = db()
    if data.password is not None and not verify_pw(data.password, user["email"], user["password_hash"]):
        conn.close()
        raise ApiError(400, "Wrong password")
    taken = conn.execute("SELECT id FROM users WHERE LOWER(email)=? AND id<>?",
                         (new_email, user["id"])).fetchone()
    if taken:
        conn.close()
        raise ApiError(400, "That email is already in use")
    try:
        if data.password is not None:
            conn.execute("UPDATE users SET email=?, password_hash=? WHERE id=?",
                         (new_email, h(data.password), user["id"]))
        else:
            conn.execute("UPDATE users SET email=? WHERE id=?", (new_email, user["id"]))
        conn.commit()
    except Exception:
        conn.rollback(); conn.close()
        raise ApiError(400, "That email is already in use")
    conn.close()
    audit("change_email", user["email"], new_email)
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
@app.get("/")
def read_root(request: Request):
    # Uptime monitors get JSON; real browsers keep getting the SPA.
    if "text/html" in request.headers.get("accept", ""):
        return FileResponse(os.path.join(FRONTEND, "index.html"),
                            headers={"Cache-Control": "no-cache"})
    return {"status": "ok", "message": "PhantomCardz API is running"}

@app.get("/api/health")
def health():
    return {"ok": True, "service": "api"}

@app.get("/api/public-config")
def public_config():
    return {"siteName": SITE, "telegramChannelUrl": TELEGRAM_CHANNEL_URL}

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
    vs = conn.execute("""
        SELECT v.*, (SELECT COUNT(*) FROM stock_items s WHERE s.variant_id=v.id AND s.sold=0) AS stock_count
        FROM variants v JOIN products p ON p.id=v.productId
        WHERE p.active=1 ORDER BY v.id
    """).fetchall()
    by_product = {}
    for v in vs:
        by_product.setdefault(v["productId"], []).append(
            {"id": v["id"], "productId": v["productId"], "name": v["name"], "price": v["price"],
             "comparePrice": v["comparePrice"], "minQuantity": v["minQuantity"], "stockCount": v["stock_count"]})
    out = [{"id": r["id"], "name": r["name"], "description": r["description"], "image": r["image"],
            "active": bool(r["active"]), "pinned": bool(r["pinned"]), "createdAt": ts(r["created_at"]),
            "variants": by_product.get(r["id"], [])} for r in rows]
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
CRYPTO_ADDRESS_KEY = {"BTC": "bitcoin", "ETH": "ethereum", "LTC": "litecoin",
                      "SOL": "solana", "USDT": "ethereum"}
# Only these tickers are offered for crypto deposits (payout wallets exist for
# exactly these); any provider currency outside this set is filtered out.
SUPPORTED_CRYPTO = ["BTC", "LTC", "ETH", "SOL"]

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
    defaults = {"bitcoin": BTC_ADDR, "ethereum": ETH_ADDR, "litecoin": LTC_ADDR,
                "solana": SOL_ADDR}
    return mp.get(key) or defaults.get(key, BTC_ADDR)

def _deposit_client_status(s: str) -> str:
    """Map an internal deposit status to the vocabulary the SPA renders."""
    return {"approved": "completed", "completed": "completed",
            "rejected": "failed", "cancelled": "failed", "failed": "failed",
            "expired": "expired", "underpaid": "underpaid",
            "pending": "pending", "waiting_payment": "waiting_payment"}.get((s or "pending").lower(), s)

def deposit_out(r, email: Optional[str] = None, username: Optional[str] = None) -> dict:
    """Serialize a deposit row in the shape the frozen SPA expects."""
    method = r["method"] or ""
    cents = int(round(float(r["amount"] or 0)))
    out = {
        "id": r["id"],
        "type": "crypto" if method.startswith("crypto") else method,
        "method": method,
        "amount": cents,
        "amountUsd": round(cents / 100.0, 2),
        "paymentId": r["reference"],
        "orderId": r["reference"],
        "reference": r["reference"],
        "status": _deposit_client_status(r["status"]),
        "rawStatus": r["status"],
        "paymentNote": r.get("payin_extra_id") or None,
        "checkoutUrl": r.get("checkout_url"),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(r["created_at"] or 0))),
    }
    if email is not None:
        out["email"] = email
    if username is not None:
        out["username"] = username
    return out

def _order_status(s: str) -> str:
    """Map an internal order status to the frozen SPA's status enum."""
    return {"completed": "fulfilled", "paid": "fulfilled", "fulfilled": "fulfilled",
            "pending_payment": "waiting_payment", "unpaid": "waiting_payment",
            "pending": "pending", "waiting_payment": "waiting_payment",
            "delivering": "delivering", "cancelled": "refunded",
            "refunded": "refunded", "replaced": "replaced"}.get((s or "pending").lower(), s or "pending")

def _order_id_str(o) -> str:
    oid = None
    try:
        oid = o["order_id"]
    except Exception:
        oid = None
    return oid or f"ORD-{o['id']}"

def _parse_items(raw):
    try:
        d = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(d, list):
        return []
    return [x for x in d if x]

def _variant_row(conn, vid, cache):
    if not vid:
        return None
    if vid in cache:
        return cache[vid]
    v = conn.execute("SELECT v.*, p.name AS product_name FROM variants v "
                     "LEFT JOIN products p ON p.id=v.productId WHERE v.id=?", (vid,)).fetchone()
    cache[vid] = v
    return v

def order_items_out(conn, oid: int, fallback_items=None, cache=None) -> list:
    """Serialize order_items joined with their variant/product/card (SPA shape)."""
    cache = cache if cache is not None else {}
    rows = conn.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (oid,)).fetchall()
    items = []
    for oi in rows:
        v = _variant_row(conn, oi["variant_id"], cache)
        card_id = oi.get("card_id")
        item = {
            "id": oi["id"],
            "orderId": oid,
            "variantId": oi["variant_id"],
            "stockItemId": None,
            "cardId": card_id,
            "itemType": (oi.get("item_type") or "product"),
            "price": oi["price"],
            "quantity": oi["quantity"],
            "productName": (v.get("product_name") if v else None),
            "variant": ({"id": v["id"], "name": v["name"], "price": v["price"],
                         "productId": v["productId"]} if v else None),
            "variantName": (v["name"] if v else None),
            "card": None,
            "contents": [],
        }
        if card_id:
            c = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
            if c:
                item["card"] = card_out(c)
        try:
            item["contents"] = json.loads(oi["contents"] or "[]")
        except Exception:
            item["contents"] = []
        items.append(item)
    if not items and fallback_items:
        # Legacy rows stored only a flat list of content strings in orders.items.
        for i, content in enumerate(fallback_items):
            if isinstance(content, dict):
                items.append({**content, "orderId": oid})
            else:
                items.append({"id": i + 1, "orderId": oid, "variantId": None, "cardId": None,
                              "itemType": "product", "price": None, "quantity": 1,
                              "productName": "Product", "variant": None, "variantName": None,
                              "card": None, "contents": [str(content)]})
    return items

def _content_map_key(it) -> str:
    if it.get("itemType") == "card":
        return f"card-{it.get('cardId') or it.get('id')}"
    return str(it.get("variantId") or it.get("id"))

def _delivery_content(o, items) -> str:
    dc = {}
    for it in items:
        contents = it.get("contents") or []
        if contents:
            dc[_content_map_key(it)] = "\n".join(str(x) for x in contents)
    if dc:
        return json.dumps(dc)
    try:
        return o["delivery_content"] or ""
    except Exception:
        return ""

def order_out(conn, o, with_items: bool = True, with_user: bool = False) -> dict:
    """Serialize an order row into the exact shape the frozen SPA reads."""
    fallback = _parse_items(o.get("items"))
    items = order_items_out(conn, o["id"], fallback_items=fallback) if with_items else []
    paid = o.get("paid_amount")
    out = {
        "id": o["id"],
        "orderId": _order_id_str(o),
        "userId": o["user_id"],
        "status": _order_status(o["status"]),
        "rawStatus": o["status"],
        "total": o["total"],
        "paidAmount": (paid if paid else o["total"]),
        "paymentMethod": (o.get("payment_method") or ""),
        "paymentNote": (o.get("payment_note") or ""),
        "deliveryContent": _delivery_content(o, items) if with_items else (o.get("delivery_content") or ""),
        "createdAt": ts(o["created_at"]),
        "items": items,
    }
    if o.get("variant_id") is not None:
        out["variantId"] = o["variant_id"]
    if o.get("quantity") is not None:
        out["quantity"] = o["quantity"]
    if with_user:
        u = conn.execute("SELECT id, username, email, telegram FROM users WHERE id=?", (o["user_id"],)).fetchone()
        if u:
            out["user"] = {"id": u["id"], "username": u["username"], "email": u["email"],
                           "telegramUsername": u["telegram"], "telegram": u["telegram"]}
            out["email"] = u["email"]
    return out

def discount_out(r) -> dict:
    """Serialize a discount_codes row in the frozen SPA's shape."""
    dtype = r.get("type") or "percent"
    value = r.get("value")
    if value in (None, 0) and r.get("percent") is not None:
        value = r["percent"]
    expires = r.get("expires_at")
    return {
        "id": r["id"], "code": r["code"], "type": dtype, "value": value or 0,
        "percent": r["percent"], "minOrder": r.get("min_order") or 0,
        "maxUses": r.get("max_uses"), "usedCount": r.get("used_count") or 0,
        "isActive": bool(r["active"]), "active": bool(r["active"]),
        "expiresAt": ts(expires) if expires else None,
    }

def _discount_off(total: float, d) -> float:
    """Return the money (cents) taken off ``total`` for discount row ``d``."""
    if d is None:
        return 0.0
    if (d.get("type") or "percent") == "fixed":
        return min(float(total), float(d.get("value") or 0))
    return float(total) * float(d.get("percent") or 0) / 100.0

def _active_discount(conn, code=None, discount_id=None):
    if discount_id:
        d = conn.execute("SELECT * FROM discount_codes WHERE id=? AND active=1", (discount_id,)).fetchone()
    elif code:
        d = conn.execute("SELECT * FROM discount_codes WHERE code=? AND active=1", (code,)).fetchone()
    else:
        d = None
    if not d:
        return None
    exp = d.get("expires_at")
    if exp and int(exp) < int(time.time()):
        return None
    mx = d.get("max_uses")
    if mx is not None and (d.get("used_count") or 0) >= mx:
        return None
    return d

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
    row = conn.execute("""
        SELECT (SELECT COUNT(*) FROM products WHERE active=1) AS products,
               (SELECT COUNT(*) FROM stock_items WHERE sold=0) AS items
    """).fetchone()
    conn.close()
    return {"products": row["products"], "itemsInStock": row["items"]}

# ================= ORDERS (PHASE A) =================
class OrderIn(BaseModel):
    variantId: Optional[int] = None
    quantity: int = 1
    code: Optional[str] = None
    items: Optional[List[Dict[str, Any]]] = None
    cardIds: Optional[List[int]] = None
    discountCodeId: Optional[int] = None

@app.post("/api/orders")
def place_order(data: OrderIn, user: dict = Depends(get_current_user)):
    """Instant balance checkout: debit the wallet, release the products.

    Accepts both the legacy ``{variantId, quantity}`` shape and the SPA's
    ``{items:[{variantId, quantity}], cardIds:[]}`` shape. Everything runs in a
    single transaction with the balance guard, so a product can never be
    released without the funds being taken (and vice versa).
    """
    raw_items = []
    if data.items:
        for it in data.items:
            try:
                raw_items.append((int(it.get("variantId")), max(1, int(it.get("quantity", 1)))))
            except (TypeError, ValueError, AttributeError):
                raise ApiError(400, "Invalid item")
    elif data.variantId:
        raw_items.append((int(data.variantId), max(1, int(data.quantity or 1))))
    card_ids = [int(c) for c in (data.cardIds or [])]
    if not raw_items and not card_ids:
        raise ApiError(400, "Invalid order")
    conn = db()
    try:
        total = 0.0
        reserved = []
        for vid, qty in raw_items:
            if qty < 1 or vid <= 0:
                raise ApiError(400, "Invalid order")
            v = conn.execute("SELECT * FROM variants WHERE id=?", (vid,)).fetchone()
            if not v:
                raise ApiError(404, "Variant not found")
            q = "SELECT * FROM stock_items WHERE variant_id=? AND sold=0 LIMIT ?"
            if conn.is_postgres:
                q += " FOR UPDATE SKIP LOCKED"
            rows = conn.execute(q, (v["id"], qty)).fetchall()
            if len(rows) < qty:
                raise ApiError(400, "Insufficient stock")
            total += float(v["price"]) * qty
            reserved.append((v, qty, rows))
        card_rows = []
        for cid in card_ids:
            c = conn.execute("SELECT ca.*, cb.name AS base_name, cb.price AS base_price, cb.hrPercent AS base_hr "
                             "FROM cards ca JOIN card_bases cb ON cb.id=ca.base_id "
                             "WHERE ca.id=? AND ca.sold=0 AND cb.active=1", (cid,)).fetchone()
            if not c:
                raise ApiError(400, "Card out of stock")
            total += float(c["base_price"])
            card_rows.append(c)
        disc = _active_discount(conn, code=data.code, discount_id=data.discountCodeId)
        if disc:
            total -= _discount_off(total, disc)
        total = round(total, 2)
        if disc:
            conn.execute("UPDATE discount_codes SET used_count=COALESCE(used_count,0)+1 WHERE id=?", (disc["id"],))
        # Debit first, guarded by balance>=total, so the wallet can't go negative.
        cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                           (total, user["id"], total))
        if cur.rowcount != 1:
            raise ApiError(400, "Insufficient balance")
        first_vid = reserved[0][0]["id"] if reserved else None
        qty_total = sum(q for _, q, _ in reserved)
        items = []
        oid = insert_id(conn, "INSERT INTO orders(user_id,variant_id,quantity,total,status,items,payment_method,paid_amount,created_at) "
                              "VALUES(?,?,?,?,?,?,?,?,?)",
                        (user["id"], first_vid, qty_total, total, "completed", "[]", "Balance", total, now()))
        conn.execute("UPDATE orders SET order_id=? WHERE id=?", (f"ORD-{oid}", oid))
        for v, qty, rows in reserved:
            picked = []
            for s in rows:
                c2 = conn.execute("UPDATE stock_items SET sold=1, order_id=? WHERE id=? AND sold=0", (oid, s["id"]))
                if c2.rowcount == 1:
                    picked.append(s)
            if len(picked) < qty:
                raise ApiError(400, "Insufficient stock")
            piece = [s["content"] for s in picked]
            items.extend(piece)
            conn.execute("INSERT INTO order_items(order_id,variant_id,quantity,price,contents,item_type) VALUES(?,?,?,?,?,?)",
                         (oid, v["id"], qty, v["price"], json.dumps(piece), "product"))
        for c in card_rows:
            c2 = conn.execute("UPDATE cards SET sold=1, buyer_id=? WHERE id=? AND sold=0", (user["id"], c["id"]))
            if c2.rowcount != 1:
                raise ApiError(400, "Card just sold, try again")
            items.append(c["number"])
            conn.execute("INSERT INTO order_items(order_id,variant_id,quantity,price,contents,item_type,card_id) VALUES(?,?,?,?,?,?,?)",
                         (oid, None, 1, c["base_price"], json.dumps([c["number"]]), "card", c["id"]))
        conn.execute("UPDATE orders SET items=? WHERE id=?", (json.dumps(items), oid))
        u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
        conn.commit()
    except ApiError:
        conn.rollback(); conn.close(); raise
    except Exception:
        conn.rollback(); conn.close(); raise
    conn.close()
    tg_notify(f"[{SITE}] ORDER #{oid} by {user['email']}: {qty_total or len(card_rows)} item(s) = ${total / 100:.2f}")
    return {"id": oid, "items": items, "total": total,
            "newBalance": int(round(u["balance"])), "status": "completed"}

@app.get("/api/orders")
def my_orders(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    out = [order_out(conn, r) for r in rows]
    conn.close()
    return out

@app.get("/api/orders/export")
def export_orders(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    lines = []
    for r in rows:
        if _order_status(r["status"]) not in ("fulfilled", "delivering", "replaced"):
            continue
        for it in order_items_out(conn, r["id"], fallback_items=_parse_items(r.get("items"))):
            lines.extend(str(c) for c in (it.get("contents") or []))
    conn.close()
    return {"content": "\n".join(lines)}

@app.get("/api/orders/{order_id}")
def one_order(order_id: int, user: dict = Depends(get_current_user)):
    conn = db()
    r = conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user["id"])).fetchone()
    if not r:
        conn.close()
        raise ApiError(404, "Not found")
    out = order_out(conn, r)
    conn.close()
    return out

class DiscountIn(BaseModel):
    code: str

@app.post("/api/discount/validate")
def validate_discount(data: DiscountIn, user: dict = Depends(get_current_user)):
    conn = db()
    d = _active_discount(conn, code=data.code)
    conn.close()
    if d:
        return {"valid": True, "percent": d["percent"], "type": d.get("type") or "percent",
                "value": d.get("value") if d.get("value") not in (None, 0) else d["percent"],
                "minOrder": d.get("min_order") or 0, "id": d["id"]}
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
    return {"newBalance": int(round(u["balance"])), "amountAdded": r["value"], "redeemed": r["value"]}

@app.get("/api/wallet/transactions")
def wallet_txs(user: dict = Depends(get_current_user)):
    conn = db()
    txs = []
    for r in conn.execute("SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall():
        method = r["method"] or ""
        is_crypto = method.startswith("crypto")
        txs.append({
            "id": f"dep-{r['id']}",
            "userId": user["id"],
            "type": "deposit" if is_crypto else "manual_deposit",
            "amount": int(round(float(r["amount"] or 0))),
            "description": ("Crypto deposit" if is_crypto else f"{method.capitalize()} deposit") + f" · {r['reference']}",
            "paymentMethod": ("Crypto" if is_crypto else method.capitalize()),
            "orderId": r["reference"],
            "status": _deposit_client_status(r["status"]),
            "createdAt": ts(r["created_at"]),
        })
    for r in conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall():
        txs.append({
            "id": f"ord-{r['id']}",
            "userId": user["id"],
            "type": "purchase",
            "amount": -int(round(float(r["total"] or 0))),
            "description": f"Order {_order_id_str(r)}",
            "paymentMethod": r.get("payment_method") or "Balance",
            "orderId": _order_id_str(r),
            "status": _order_status(r["status"]),
            "createdAt": ts(r["created_at"]),
        })
    conn.close()
    txs.sort(key=lambda t: t["createdAt"], reverse=True)
    return txs

# ================= DEPOSITS (PHASE B) =================
class CartItem(BaseModel):
    variantId: int
    quantity: int = 1

class CashappIn(BaseModel):
    amount: float = 0
    cashtag: Optional[str] = None
    items: Optional[List[CartItem]] = None
    discountCodeId: Optional[int] = None

def _deposit_ref() -> str:
    return secrets.token_hex(4).upper()

def _cart_order(conn, items, user, discount_id=None, payment_method="", payment_note="", status="pending_payment"):
    """Reserve stock for a multi-item cart and create a pending order.

    Returns (order_id, total_cents, delivered_contents)."""
    reserved = []
    total = 0.0
    for it in items:
        if it.quantity < 1:
            raise ApiError(400, "Invalid quantity")
        v = conn.execute("SELECT * FROM variants WHERE id=?", (it.variantId,)).fetchone()
        if not v:
            raise ApiError(404, "Variant not found")
        q = "SELECT * FROM stock_items WHERE variant_id=? AND sold=0 LIMIT ?"
        if conn.is_postgres:
            q += " FOR UPDATE SKIP LOCKED"
        rows = conn.execute(q, (v["id"], it.quantity)).fetchall()
        if len(rows) < it.quantity:
            raise ApiError(400, "Insufficient stock")
        total += float(v["price"]) * it.quantity
        reserved.append((v, it.quantity, rows))
    disc = _active_discount(conn, discount_id=discount_id)
    if disc:
        total -= _discount_off(total, disc)
    total = round(total, 2)
    if disc:
        conn.execute("UPDATE discount_codes SET used_count=COALESCE(used_count,0)+1 WHERE id=?", (disc["id"],))
    first_vid = reserved[0][0]["id"] if reserved else None
    qty_total = sum(q for _, q, _ in reserved)
    oid = insert_id(conn, "INSERT INTO orders(user_id,variant_id,quantity,total,status,items,payment_method,payment_note,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                    (user["id"], first_vid, qty_total, total, status, "[]",
                     payment_method, payment_note, now()))
    conn.execute("UPDATE orders SET order_id=? WHERE id=?", (f"ORD-{oid}", oid))
    contents = []
    for v, qty, rows in reserved:
        picked = []
        for s in rows:
            cur = conn.execute("UPDATE stock_items SET sold=1, order_id=? WHERE id=? AND sold=0", (oid, s["id"]))
            if cur.rowcount == 1:
                picked.append(s)
        if len(picked) < qty:
            conn.rollback()
            raise ApiError(400, "Insufficient stock")
        piece = [s["content"] for s in picked]
        contents.extend(piece)
        conn.execute("INSERT INTO order_items(order_id,variant_id,quantity,price,contents) VALUES(?,?,?,?,?)",
                     (oid, v["id"], qty, v["price"], json.dumps(piece)))
    conn.execute("UPDATE orders SET items=? WHERE id=?", (json.dumps(contents), oid))
    return oid, total, contents

@app.post("/api/orders/cashapp")
def deposit_cashapp(data: CashappIn, user: dict = Depends(get_current_user)):
    conn = db()
    if data.items:
        try:
            oid, total, _ = _cart_order(conn, data.items, user, data.discountCodeId, payment_method="CashApp", status="pending")
        except ApiError:
            conn.rollback(); conn.close(); raise
        ref = f"ORD{oid}-{secrets.token_hex(3).upper()}"
        handle = manual_handle(conn, "cashapp", CASHAPP)
        conn.execute("INSERT INTO deposits(user_id,method,amount,reference,status,created_at) VALUES(?,?,?,?,?,?)",
                     (user["id"], "cashapp", total, ref, "pending", now()))
        note = f"{SITE.upper()}-{ref}"
        conn.execute("UPDATE orders SET payment_note=? WHERE id=?", (note, oid))
        conn.commit(); conn.close()
        tg_notify(f"[{SITE}] CashApp order #{oid} by {user['email']}: ${total / 100:.2f}")
        return {"orderId": oid, "id": oid, "reference": ref, "amount": total, "total": total,
                "status": "pending", "paymentNote": note, "cashappTag": handle,
                "instructions": f"Send ${total / 100:.2f} via CashApp to {handle} then message {ADMIN_TG} on Telegram with code {ref} and your email {user['email']}"}
    if data.amount <= 0: raise ApiError(400, "Invalid amount")
    mins = json.loads(get_setting(conn, "min_deposits"))
    if data.amount < mins.get("cashapp", 5):
        conn.close()
        raise ApiError(400, f"Minimum deposit is ${mins.get('cashapp', 5)}")
    handle = manual_handle(conn, "cashapp", CASHAPP)
    ref = _deposit_ref()
    note = f"{SITE.upper()}-{ref}"
    conn.execute("INSERT INTO deposits(user_id,method,amount,reference,status,payin_extra_id,created_at) VALUES(?,?,?,?,?,?,?)",
                 (user["id"], "cashapp", to_cents(data.amount), ref, "pending", note, now()))
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
    conn.execute("INSERT INTO deposits(user_id,method,amount,reference,status,payin_extra_id,created_at) VALUES(?,?,?,?,?,?,?)",
                 (user["id"], method, to_cents(data.amount), ref, "pending", note, now()))
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
    orderId: Optional[int] = None

def _linked_order_id(ref: str):
    """Order id encoded in a deposit reference like ``ORD42-AB12CD``."""
    if ref and ref.startswith("ORD"):
        try:
            return int(ref.split("-")[0][3:])
        except Exception:
            return None
    return None

def _client_status(s: str) -> str:
    """Map an internal deposit status to the value the SPA's poller expects."""
    return {"approved": "completed", "rejected": "failed", "cancelled": "failed",
            "failed": "failed", "expired": "expired", "underpaid": "underpaid",
            "pending": "pending"}.get((s or "pending").lower(), s)

def _ipn_url() -> str:
    base = PUBLIC_BASE_URL
    if not base:
        host = DOMAINS[0] if DOMAINS else "localhost"
        scheme = "http" if host in ("localhost", "127.0.0.1") else "https"
        base = f"{scheme}://{host}"
    return base + "/api/payments/webhook/nowpayments"

def settle_deposit(conn, d, actor: str = "system") -> bool:
    """Idempotently mark a pending deposit as paid and release its order/credit.

    Safe to call from a webhook, a status poll, or an admin action at the same
    time: the state transition is guarded by ``status='pending'``.
    """
    cur = conn.execute("UPDATE deposits SET status='approved', settled_at=?, updated_at=? "
                       "WHERE id=? AND status='pending'", (now(), now(), d["id"]))
    if cur.rowcount != 1:
        return False
    oid = _linked_order_id(d["reference"] or "")
    if oid:
        # Products were already reserved at checkout; completing releases them.
        conn.execute("UPDATE orders SET status='completed' "
                     "WHERE id=? AND status IN ('pending_payment','pending','unpaid')", (oid,))
    else:
        conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (d["amount"], d["user_id"]))
    audit("deposit_settle", actor, f"id={d['id']} amount={d['amount']} ref={d['reference']}")
    return True

def _provider_payment(conn, ref: str, cents: int, coin: str, user, purpose: str) -> Optional[dict]:
    """Create a NOWPayments invoice for a pending deposit row and persist its data."""
    if not (npp and npp.enabled()):
        return None
    email = user.get("email") if isinstance(user, dict) else "-"
    try:
        info = npp.create_payment(cents / 100.0, "usd", coin, ref,
                                  f"{SITE} {purpose}", _ipn_url())
    except Exception as e:
        audit("crypto_provider_error", email or "-", f"{ref}: {e}")
        return None
    conn.execute(
        "UPDATE deposits SET provider='nowpayments', provider_id=?, pay_currency=?, pay_amount=?, "
        "pay_address=?, payin_extra_id=?, checkout_url=?, meta=?, updated_at=? WHERE reference=?",
        (str(info.get("payment_id") or ""), (info.get("pay_currency") or coin).upper(),
         str(info.get("pay_amount") or ""), info.get("pay_address"), info.get("payin_extra_id"),
         info.get("invoice_url") or info.get("checkout_url"), json.dumps(info), now(), ref))
    return info

def _apply_provider_status(conn, d, np_status: str, info: Optional[dict] = None) -> str:
    """Persist a provider status and settle / fail the deposit accordingly."""
    fields = {"updated_at": now()}
    if info:
        if info.get("pay_address"):
            fields["pay_address"] = info["pay_address"]
        if info.get("pay_amount") is not None:
            fields["pay_amount"] = str(info["pay_amount"])
        if info.get("payin_extra_id"):
            fields["payin_extra_id"] = info["payin_extra_id"]
        if info.get("pay_currency"):
            fields["pay_currency"] = str(info["pay_currency"]).upper()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE deposits SET {sets} WHERE id=?", tuple(fields.values()) + (d["id"],))
    mapped = npp.map_status(np_status) if npp else "pending"
    if mapped == "completed":
        settle_deposit(conn, d, actor="nowpayments")
        return "completed"
    if mapped == "failed":
        oid = _linked_order_id(d["reference"] or "")
        if oid:
            conn.execute("UPDATE stock_items SET sold=0, order_id=NULL WHERE order_id=?", (oid,))
            conn.execute("UPDATE orders SET status='cancelled' "
                         "WHERE id=? AND status IN ('pending_payment','pending','unpaid')", (oid,))
        conn.execute("UPDATE deposits SET status='failed' WHERE id=? AND status='pending'", (d["id"],))
        return "failed"
    return _client_status(d["status"])

def _refresh_crypto_deposit(conn, d):
    """Ask the provider for the latest state of a pending, provider-backed deposit."""
    if not (npp and npp.enabled() and d.get("provider_id")):
        return d
    if d["status"] != "pending":
        return d
    try:
        info = npp.get_payment(d["provider_id"])
    except Exception:
        return d
    _apply_provider_status(conn, d, (info.get("payment_status") or "").lower(), info)
    conn.commit()
    return conn.execute("SELECT * FROM deposits WHERE id=?", (d["id"],)).fetchone()

def _crypto_tracking(r) -> dict:
    method = (r["method"] or "crypto:BTC").split(":", 1)
    coin = (method[1] if len(method) > 1 else "BTC").upper()
    pay_currency = (r.get("pay_currency") or coin).upper()
    address = r.get("pay_address")
    if not address:
        conn = db()
        address = crypto_address(conn, pay_currency)
        conn.close()
    cents = int(round(float(r["amount"] or 0)))
    created = int(r["created_at"] or now())
    pay_amount = r.get("pay_amount") or f"{cents / 100.0:.2f}"
    oid = _linked_order_id(r["reference"] or "")
    return {
        "paymentId": r["reference"],
        "reference": r["reference"],
        "status": _client_status(r["status"]),
        "payCurrency": pay_currency,
        "currency": "USD",
        "network": pay_currency,
        "payAddress": address,
        "address": address,
        "payAmount": str(pay_amount),
        "amount": cents,
        "payinExtraId": r.get("payin_extra_id"),
        "checkoutUrl": r.get("checkout_url"),
        "provider": r.get("provider") or "manual",
        "orderId": oid if oid else r["reference"],
        "expiresAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created + 3600)),
    }

@app.post("/api/payments/crypto/create")
def deposit_crypto(data: CryptoIn, user: dict = Depends(get_current_user)):
    new_dialect = data.payCurrency is not None or isinstance(data.amount, str)
    try:
        raw = float(data.amount)
    except (TypeError, ValueError):
        raise ApiError(400, "Invalid amount")
    cents = int(round(raw)) if new_dialect else to_cents(raw)
    usd = cents / 100.0
    if cents <= 0:
        raise ApiError(400, "Invalid amount")
    coin = (data.payCurrency or data.currency or "BTC").upper()
    conn = db()
    purpose = (data.purpose or "deposit").lower()
    if data.orderId:
        purpose = "order"
    if purpose != "order":
        mins = json.loads(get_setting(conn, "min_deposits"))
        minimum = mins.get("crypto", 10)
        if usd < minimum:
            conn.close()
            raise ApiError(400, f"Minimum deposit is ${minimum}")
    addr = crypto_address(conn, coin)
    if data.orderId:
        ref = f"ORD{int(data.orderId)}-{secrets.token_hex(3).upper()}"
        conn.execute("UPDATE orders SET payment_method=? WHERE id=? AND (payment_method IS NULL OR payment_method='')",
                     (f"Crypto ({coin})", int(data.orderId)))
    else:
        ref = secrets.token_hex(8) if new_dialect else _deposit_ref()
    conn.execute("INSERT INTO deposits(user_id,method,amount,reference,status,created_at) VALUES(?,?,?,?,?,?)",
                 (user["id"], f"crypto:{coin}", cents, ref, "pending", now()))
    conn.commit()
    _provider_payment(conn, ref, cents, coin, user, purpose)
    conn.commit()
    tg_notify(f"[{SITE}] Crypto {'order' if purpose == 'order' else 'deposit'} req {ref} by {user['email']}: ${usd}")
    row = conn.execute("SELECT * FROM deposits WHERE reference=?", (ref,)).fetchone()
    conn.close()
    if new_dialect:
        return _crypto_tracking(row)
    return {"address": row.get("pay_address") or addr, "amount": cents, "currency": coin, "reference": ref,
            "status": "pending", "expiresAt": ts(now() + 3600)}

def _supported_currencies(provider_currencies):
    """Restrict the provider list to the tickers we actually have wallets for."""
    have = {str(c).upper() for c in (provider_currencies or [])}
    allowed = [c for c in SUPPORTED_CRYPTO if c in have]
    return allowed or list(SUPPORTED_CRYPTO)

@app.get("/api/payments/crypto/currencies")
def crypto_currencies():
    if npp and npp.enabled():
        cached = _np_currencies_cache.get("value")
        if cached and _np_currencies_cache.get("expires", 0) > time.time():
            return {"currencies": cached}
        try:
            currencies = npp.get_currencies()
            if currencies:
                allowed = _supported_currencies(currencies)
                _np_currencies_cache["value"] = allowed
                _np_currencies_cache["expires"] = time.time() + 600
                return {"currencies": allowed}
        except Exception:
            pass
    return {"currencies": list(SUPPORTED_CRYPTO)}

_np_currencies_cache: Dict[str, Any] = {}

@app.get("/api/payments/crypto/status")
def crypto_status(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    out = []
    for r in rows:
        r = _refresh_crypto_deposit(conn, r)
        out.append({"id": r["id"], "method": r["method"], "amount": r["amount"], "reference": r["reference"],
                    "status": _client_status(r["status"]), "createdAt": ts(r["created_at"])})
    conn.close()
    return out

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
    if not r:
        conn.close()
        raise ApiError(404, "Not found")
    r = _refresh_crypto_deposit(conn, r)
    conn.close()
    return _crypto_tracking(r)

@app.get("/api/payments/crypto/{pid}/status")
def crypto_payment_status(pid: str, user: dict = Depends(get_current_user)):
    conn = db()
    r = _find_crypto_deposit(conn, pid, user["id"])
    if not r:
        conn.close()
        raise ApiError(404, "Not found")
    r = _refresh_crypto_deposit(conn, r)
    conn.close()
    oid = _linked_order_id(r["reference"] or "")
    return {"status": _client_status(r["status"]), "reference": r["reference"], "amount": r["amount"],
            "orderId": oid if oid else r["reference"], "createdAt": ts(r["created_at"])}

@app.post("/api/payments/webhook/nowpayments")
async def nowpayments_webhook(request: Request):
    """IPN callback. Verifies the HMAC signature, then settles idempotently."""
    raw = await request.body()
    sig = request.headers.get("x-nowpayments-sig", "")
    if not (npp and npp.enabled() and npp.verify_ipn(raw, sig)):
        raise ApiError(401, "Invalid signature")
    try:
        data = json.loads(raw.decode() or "{}")
    except Exception:
        raise ApiError(400, "Invalid payload")
    pid = str(data.get("payment_id") or "")
    status = (data.get("payment_status") or "").lower()
    conn = db()
    d = conn.execute("SELECT * FROM deposits WHERE provider_id=?", (pid,)).fetchone() if pid else None
    if not d:
        conn.close()
        return {"ok": True, "ignored": True}
    _apply_provider_status(conn, d, status, data)
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/deposits")
def my_deposits(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    conn.close()
    return [deposit_out(r) for r in rows]

# ================= CARDS + CHECKER (PHASE C) =================
def _digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def bin_data(number: str) -> dict:
    d = _digits(number)
    brand = {"4": "Visa", "5": "Mastercard", "2": "Mastercard",
             "3": "American Express", "6": "Discover"}.get(d[:1], "Visa")
    return {"brand": brand, "scheme": brand, "type": "debit", "level": "classic",
            "bank": "Synchrony Bank", "country": "United States", "countryCode": "US"}

def card_out(r, base_name=None, base_price=None, base_hr=None) -> dict:
    """Serialize a card row in the shape the frozen SPA expects (plus legacy keys)."""
    num = r["number"] or ""
    d = _digits(num)
    mm = int(r["exp_month"] or 0)
    yy = int(r["exp_year"] or 0)
    masked = (d[:6] + "•" * 6 + d[-4:]) if len(d) >= 10 else num
    return {
        "id": r["id"],
        "cardNumber": num,
        "maskedCard": masked,
        "expiry": (f"{mm:02d}/{str(yy)[-2:]}" if mm else ""),
        "cvv": r["cvv"],
        "country": "US",
        "extras": ((r.get("extras") if hasattr(r, "get") else None) or ""),
        "price": base_price if base_price is not None else None,
        "hrPercent": base_hr if base_hr else 80,
        "isFirstHand": True,
        "isSold": bool(r["sold"]),
        "baseId": r["base_id"],
        "baseName": base_name,
        "binData": bin_data(num),
        "number": num,
        "expMonth": r["exp_month"],
        "expYear": r["exp_year"],
        "checked": bool(r["checked"]),
        "valid": r["valid"],
    }

@app.get("/api/card-bases")
def card_bases():
    conn = db()
    rows = conn.execute("""
        SELECT b.*, (SELECT COUNT(*) FROM cards c WHERE c.base_id=b.id AND c.sold=0) AS stock_count
        FROM card_bases b WHERE b.active=1
    """).fetchall()
    out = [{"id": r["id"], "name": r["name"], "price": r["price"], "stockCount": r["stock_count"],
            "refundable": bool(r["refundable"]), "hrPercent": r["hrPercent"] or 80} for r in rows]
    conn.close()
    return out

@app.get("/api/cards")
def list_cards(baseId: Optional[int] = None, user: dict = Depends(get_current_user)):
    conn = db()
    sql = """SELECT ca.*, cb.name AS base_name, cb.price AS base_price, cb.hrPercent AS base_hr
             FROM cards ca JOIN card_bases cb ON cb.id=ca.base_id
             WHERE ca.sold=0 AND cb.active=1"""
    params = []
    if baseId:
        sql += " AND ca.base_id=?"
        params.append(baseId)
    sql += " ORDER BY ca.id DESC LIMIT 1000"
    rows = conn.execute(sql, tuple(params)).fetchall()
    conn.close()
    return [card_out(r, r["base_name"], r["base_price"], r["base_hr"]) for r in rows]

class BuyCardIn(BaseModel):
    baseId: Optional[int] = None
    extras: Optional[str] = None
    price: Optional[float] = None

def _parse_card_line(line: str):
    """Parse one pasted "full item" line into card fields.

    Mirrors the frozen SPA's own tokenizer (``iJ``/``aJ``): split on
    ``[|:,\\s]+``; token 0 is the number, token 1 an ``MM/YY`` or ``MM/YYYY``
    expiry, token 2 the CVV. Everything else is preserved verbatim in
    ``extras`` (city/state/zip live there and are parsed client-side).
    """
    raw = (line or "").strip()
    if not raw:
        return None
    parts = [p for p in re.split(r"[|:,\s]+", raw) if p]
    if len(parts) < 3:
        return None
    number = "".join(ch for ch in parts[0] if ch.isdigit())
    if len(number) < 12:
        return None
    m = re.match(r"^(\d{1,2})[/-](\d{2}|\d{4})$", parts[1])
    if not m:
        return None
    month = int(m.group(1))
    year = int(m.group(2))
    if year < 100:
        year += 2000
    if not (1 <= month <= 12):
        return None
    cvv = "".join(ch for ch in parts[2] if ch.isdigit()) or parts[2]
    return {"number": number, "month": month, "year": year, "cvv": cvv, "extras": raw}

def _buy_card_random(base_id: int, user: dict):
    """Legacy: buy one random unsold card from a base using wallet balance."""
    conn = db()
    b = conn.execute("SELECT * FROM card_bases WHERE id=? AND active=1", (base_id,)).fetchone()
    if not b:
        conn.close()
        raise ApiError(404, "Not found")
    c = conn.execute("SELECT * FROM cards WHERE base_id=? AND sold=0 ORDER BY RANDOM() LIMIT 1",
                     (base_id,)).fetchone()
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
    oid = insert_id(conn, "INSERT INTO orders(user_id,variant_id,quantity,total,status,items,payment_method,paid_amount,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                    (user["id"], None, 1, b["price"], "completed", "[]", "Balance", b["price"], now()))
    conn.execute("UPDATE orders SET order_id=? WHERE id=?", (f"CARD-{oid}", oid))
    conn.execute("INSERT INTO order_items(order_id,variant_id,quantity,price,contents,item_type,card_id) VALUES(?,?,?,?,?,?,?)",
                 (oid, None, 1, b["price"], json.dumps([c["number"]]), "card", c["id"]))
    conn.execute("UPDATE orders SET items=? WHERE id=?", (json.dumps([c["number"]]), oid))
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.commit()
    conn.close()
    out = card_out(c, b["name"], b["price"], b["hrPercent"])
    out["newBalance"] = int(round(u["balance"]))
    return out

@app.post("/api/cards")
def buy_or_add_card(data: BuyCardIn, user: dict = Depends(get_current_user)):
    """Add card stock (staff) or buy a random card (legacy).

    The frozen SPA reuses ``POST /api/cards`` from the admin panel to add stock
    (``{extras, price, baseId}``) while buying a specific card goes through
    ``POST /api/cards/{id}/purchase``. Branch on the payload so both keep
    working; the ``{baseId}``-only shape stays the legacy random buy.
    """
    if data.extras is not None or data.price is not None:
        _require_staff(user)
        if not data.baseId:
            raise ApiError(400, "Base is required")
        if not (data.extras or "").strip():
            raise ApiError(400, "Full item is required")
        conn = db()
        b = conn.execute("SELECT id FROM card_bases WHERE id=? AND active=1", (data.baseId,)).fetchone()
        if not b:
            conn.close()
            raise ApiError(404, "Base not found")
        added = skipped = 0
        for line in (data.extras or "").splitlines():
            parsed = _parse_card_line(line)
            if not parsed:
                if line.strip():
                    skipped += 1
                continue
            conn.execute("INSERT INTO cards(base_id,number,exp_month,exp_year,cvv,extras) VALUES(?,?,?,?,?,?)",
                         (data.baseId, parsed["number"], parsed["month"], parsed["year"],
                          parsed["cvv"], parsed["extras"]))
            added += 1
        conn.commit(); conn.close()
        audit("card_add", user["email"], f"base={data.baseId} added={added} skipped={skipped}")
        return {"ok": True, "added": added, "addedCount": added,
                "skipped": skipped, "skippedCount": skipped, "count": added}
    if not data.baseId:
        raise ApiError(400, "Base is required")
    return _buy_card_random(data.baseId, user)

@app.get("/api/user/cards")
def my_cards(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("""SELECT ca.*, cb.name AS base_name, cb.price AS base_price, cb.hrPercent AS base_hr
                           FROM cards ca JOIN card_bases cb ON cb.id=ca.base_id
                           WHERE ca.buyer_id=? ORDER BY ca.id DESC""", (user["id"],)).fetchall()
    conn.close()
    out = []
    for r in rows:
        o = card_out(r, r["base_name"], r["base_price"], r["base_hr"])
        o["base"] = r["base_name"]
        out.append(o)
    return out

class CheckIn(BaseModel):
    cardId: Optional[int] = None
    cards: Optional[List[dict]] = None

@app.post("/api/checker/check")
def check_card(data: CheckIn, user: dict = Depends(get_current_user)):
    conn = db()
    if data.cards is not None:
        results = []
        for card in data.cards:
            desc = card.get("dataDescriptor") or card.get("cardNumber") or ""
            approved = random.random() < 0.7
            results.append({
                "status": "approved" if approved else "declined",
                "code": "1" if approved else "2",
                "message": "Approved" if approved else "Declined",
                "dataDescriptor": desc,
            })
        conn.close()
        return {"results": results}
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
    """Config for the SPA's Authorize.net Accept.js card checker."""
    return {
        "enabled": True,
        "dailyLimit": 50,
        "scriptUrl": os.getenv("AUTHORIZE_NET_SCRIPT_URL", "https://jstest.authorize.net/v1/Accept.js"),
        "apiLoginId": os.getenv("AUTHORIZE_NET_API_LOGIN_ID", ""),
        "clientKey": os.getenv("AUTHORIZE_NET_CLIENT_KEY", ""),
    }

# ================= GAMES (PHASE D) =================
class DiceIn(BaseModel):
    amount: float = 0
    betAmount: Optional[float] = None
    guessOver: Optional[int] = 50

    def bet(self) -> float:
        return float(self.betAmount if self.betAmount is not None else self.amount)

@app.post("/api/games/dice")
def game_dice(data: DiceIn, user: dict = Depends(get_current_user)):
    amount = data.bet()
    if amount <= 0: raise ApiError(400, "Invalid amount")
    if not (2 <= data.guessOver <= 98):
        raise ApiError(400, "guessOver must be 2-98")
    conn = db()
    cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                       (amount, user["id"], amount))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Insufficient balance")
    roll = round(random.uniform(0, 100), 2)
    mult = 99.0 / (100 - data.guessOver)
    win = roll > data.guessOver
    payout = round(amount * mult, 2) if win else 0.0
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (payout, user["id"]))
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.execute("INSERT INTO game_history(user_id,game,amount,payout,created_at) VALUES(?,?,?,?,?)",
                 (user["id"], "dice", amount, payout, now()))
    conn.commit(); conn.close()
    return {"result": "win" if win else "lose", "won": win, "roll": roll, "multiplier": round(mult, 4),
            "payout": payout, "newBalance": int(round(u["balance"]))}

class SpinIn(BaseModel):
    amount: float = 0
    betAmount: Optional[float] = None

    def bet(self) -> float:
        return float(self.betAmount if self.betAmount is not None else self.amount)

@app.post("/api/games/spin")
def game_spin(data: SpinIn, user: dict = Depends(get_current_user)):
    amount = data.bet()
    if amount <= 0: raise ApiError(400, "Invalid amount")
    symbols = ["Cherry", "Lemon", "Bell", "Seven", "Diamond"]
    weights = [40, 30, 15, 10, 5]
    pay = {"Cherry": 2, "Lemon": 3, "Bell": 5, "Seven": 10, "Diamond": 25}
    reels = random.choices(symbols, weights=weights, k=3)
    conn = db()
    cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                       (amount, user["id"], amount))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Insufficient balance")
    payout = 0.0
    if reels[0] == reels[1] == reels[2]:
        payout = round(amount * pay[reels[0]] * 0.95, 2)
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (payout, user["id"]))
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.execute("INSERT INTO game_history(user_id,game,amount,payout,created_at) VALUES(?,?,?,?,?)",
                 (user["id"], "spin", amount, payout, now()))
    conn.commit(); conn.close()
    return {"result": "win" if payout > 0 else "lose", "won": payout > 0, "reels": reels,
            "reward": payout, "payout": payout, "newBalance": int(round(u["balance"]))}

class MinesIn(BaseModel):
    amount: float = 0
    betAmount: Optional[float] = None
    mines: int = 3
    difficulty: Optional[str] = None
    action: str = "start"
    tile: Optional[int] = None
    gameId: Optional[int] = None

    def bet(self) -> float:
        return float(self.betAmount if self.betAmount is not None else self.amount)

    def mine_count(self) -> int:
        if self.difficulty:
            return {"simple": 3, "extreme": 10, "impossible": 20}.get(self.difficulty, self.mines)
        return self.mines

@app.post("/api/games/mines")
def game_mines(data: MinesIn, user: dict = Depends(get_current_user)):
    amount = data.bet()
    if data.action in ("start",) and amount <= 0: raise ApiError(400, "Invalid amount")
    conn = db()
    if data.action == "start":
        mines = data.mine_count()
        if not (1 <= mines <= 24):
            conn.close()
            raise ApiError(400, "mines must be 1-24")
        live = conn.execute("SELECT * FROM games_mines WHERE user_id=? AND active=1", (user["id"],)).fetchone()
        if live:
            conn.close()
            raise ApiError(400, "Finish current game first")
        cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                           (amount, user["id"], amount))
        if cur.rowcount != 1:
            conn.rollback(); conn.close()
            raise ApiError(400, "Insufficient balance")
        mm = random.sample(range(25), mines)
        gid = insert_id(conn, """INSERT INTO games_mines(user_id,mines,amount,safe_picks,mines_map,opened,active,created_at)
                VALUES(?,?,?,?,?,?,1,?)""",
                (user["id"], mines, amount, 0, json.dumps(mm), json.dumps([]), now()))
        u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
        conn.commit(); conn.close()
        return {"gameId": gid, "status": "started", "mines": mines,
                "newBalance": int(round(u["balance"]))}
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
                    "newBalance": int(round(u["balance"]))}
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
                "newBalance": int(round(u["balance"]))}
    conn.close()
    raise ApiError(400, "Bad action")

class PlinkoIn(BaseModel):
    amount: float = 0
    betAmount: Optional[float] = None
    risk: str = "low"

    def bet(self) -> int:
        return int(round(float(self.betAmount if self.betAmount is not None else self.amount)))

# 8 fair rows -> 9 slots. Matches the frozen client's payout table (house edge ~3.8%).
PLINKO_TABLE = [3, 2.2, 1.3, 0.9, 0.45, 0.9, 1.3, 2.2, 3]

@app.post("/api/games/plinko")
def game_plinko(data: PlinkoIn, user: dict = Depends(get_current_user)):
    bet = data.bet()
    if bet <= 0: raise ApiError(400, "Invalid amount")
    path = [random.randint(0, 1) for _ in range(8)]
    slot = sum(path)
    mult = PLINKO_TABLE[slot]
    conn = db()
    cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                       (bet, user["id"], bet))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Insufficient balance")
    payout = int(round(bet * mult))
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (payout, user["id"]))
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.execute("INSERT INTO game_history(user_id,game,amount,payout,created_at) VALUES(?,?,?,?,?)",
                 (user["id"], "plinko", bet, payout, now()))
    conn.commit(); conn.close()
    return {"path": path, "slot": slot, "bucket": slot, "multiplier": mult, "payout": payout,
            "profit": payout - bet, "won": payout > bet, "newBalance": int(round(u["balance"]))}

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
    row = conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM users)                                   AS users,
          (SELECT COUNT(*) FROM orders)                                  AS orders,
          (SELECT COUNT(*) FROM deposits)                                AS deposits,
          (SELECT COUNT(*) FROM deposits WHERE status='pending')         AS pending,
          (SELECT COALESCE(SUM(total),0) FROM orders WHERE status='completed') AS revenue,
          (SELECT COUNT(*) FROM cards WHERE sold=1)                      AS cards_sold,
          (SELECT COALESCE(SUM(balance),0) FROM users)                   AS balance,
          (SELECT COALESCE(SUM(amount-payout),0) FROM game_history)      AS games_revenue,
          (SELECT COUNT(*) FROM stock_items WHERE sold=0)                AS items_in_stock,
          (SELECT COUNT(*) FROM stock_items WHERE sold=1)                AS items_sold,
          (SELECT COALESCE(SUM(v.price),0) FROM stock_items s JOIN variants v ON v.id=s.variant_id WHERE s.sold=0) AS stock_worth
    """).fetchone()
    conn.close()
    return {"users": row["users"], "orders": row["orders"], "deposits": row["deposits"],
            "pendingDeposits": row["pending"], "revenue": int(round(row["revenue"])),
            "cardsSold": row["cards_sold"], "balance": int(round(row["balance"])),
            "gamesRevenue": int(round(row["games_revenue"])),
            "totalUsers": row["users"], "totalSales": int(round(row["revenue"])),
            "storeBalance": int(round(row["balance"])), "itemsInStock": row["items_in_stock"],
            "itemsSold": row["items_sold"], "stockWorth": int(round(row["stock_worth"]))}

@app.get("/api/admin/users")
def admin_users(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("""
        SELECT u.*, COUNT(o.id) AS order_count
        FROM users u LEFT JOIN orders o ON o.user_id=u.id
        GROUP BY u.id ORDER BY u.id
    """).fetchall()
    out = [{"id": r["id"], "email": r["email"], "username": r["username"], "telegram": r["telegram"],
            "balance": r["balance"], "rank": r["rank"], "orders": r["order_count"],
            "createdAt": ts(r["created_at"]), **role_flags(r)}
           for r in rows]
    conn.close()
    return out

@app.patch("/api/admin/users/{uid}")
def admin_patch_user(uid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if "balance" in payload:
        conn.execute("UPDATE users SET balance=? WHERE id=?", (to_cents(payload["balance"]), uid))
    if "rank" in payload:
        conn.execute("UPDATE users SET rank=? WHERE id=?", (payload["rank"], uid))
    if "telegram" in payload:
        conn.execute("UPDATE users SET telegram=? WHERE id=?", (payload["telegram"], uid))
    if "role" in payload and payload["role"] in ("user", "admin", "worker"):
        conn.execute("UPDATE users SET rank=? WHERE id=?", (payload["role"], uid))
    if "isWorker" in payload:
        r0 = conn.execute("SELECT rank FROM users WHERE id=?", (uid,)).fetchone()
        if r0 and r0["rank"] != "admin":
            conn.execute("UPDATE users SET rank=? WHERE id=?", ("worker" if payload["isWorker"] else "user", uid))
    if "isBanned" in payload:
        conn.execute("UPDATE users SET is_banned=? WHERE id=?", (1 if payload["isBanned"] else 0, uid))
    r = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.commit(); audit("user_patch", user["email"], f"target={uid} fields={sorted(payload.keys())}"); conn.close()
    if not r:
        raise ApiError(404, "Not found")
    return {"id": r["id"], "email": r["email"], "username": r["username"], "balance": r["balance"],
            "rank": r["rank"], "telegram": r["telegram"], **role_flags(r)}

# The web client addresses user management via dedicated action routes; keep
# these aliases so the admin panel works against this backend.
def _admin_target(conn, uid: int):
    r = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        conn.close()
        raise ApiError(404, "Not found")
    return r

@app.post("/api/admin/users/{uid}/set-role")
def admin_set_role(uid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    role = payload.get("role")
    if role not in ("user", "admin", "worker"):
        raise ApiError(400, "Invalid role")
    conn = db(); _admin_target(conn, uid)
    conn.execute("UPDATE users SET rank=? WHERE id=?", (role, uid))
    conn.commit(); audit("user_set_role", user["email"], f"target={uid} role={role}"); conn.close()
    return {"ok": True, "role": role, "isWorker": role == "worker"}

@app.post("/api/admin/users/{uid}/set-worker")
def admin_set_worker(uid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db(); r = _admin_target(conn, uid)
    is_worker = bool(payload.get("isWorker"))
    if r["rank"] != "admin":
        conn.execute("UPDATE users SET rank=? WHERE id=?", ("worker" if is_worker else "user", uid))
    conn.commit(); audit("user_set_worker", user["email"], f"target={uid} isWorker={is_worker}"); conn.close()
    return {"ok": True, "isWorker": is_worker}

@app.post("/api/admin/users/{uid}/set-balance")
def admin_set_balance(uid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    raw = payload.get("balance", payload.get("amount"))
    try:
        balance = to_cents(raw)
    except (TypeError, ValueError):
        raise ApiError(400, "Invalid balance")
    conn = db(); _admin_target(conn, uid)
    conn.execute("UPDATE users SET balance=? WHERE id=?", (balance, uid))
    conn.commit(); audit("user_set_balance", user["email"], f"target={uid} balance={balance}"); conn.close()
    return {"ok": True, "balance": balance}

@app.post("/api/admin/users/{uid}/balance")
def admin_set_balance_alias(uid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    raw = payload.get("amount", payload.get("balance"))
    try:
        balance = int(round(float(raw)))
    except (TypeError, ValueError):
        raise ApiError(400, "Invalid balance")
    conn = db(); _admin_target(conn, uid)
    conn.execute("UPDATE users SET balance=? WHERE id=?", (balance, uid))
    conn.commit(); audit("user_set_balance", user["email"], f"target={uid} balance={balance}"); conn.close()
    return {"ok": True, "balance": balance}

@app.post("/api/admin/users/{uid}/password")
@app.patch("/api/admin/users/{uid}/password")
def admin_set_password(uid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    new_password = payload.get("newPassword") or payload.get("password")
    if not new_password:
        raise ApiError(400, "Password required")
    conn = db(); r = _admin_target(conn, uid)
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (h(new_password, r["email"]), uid))
    conn.commit(); audit("user_set_password", user["email"], f"target={uid}"); conn.close()
    return {"ok": True}

@app.post("/api/admin/users/{uid}/ban")
def admin_ban_user(uid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db(); _admin_target(conn, uid)
    conn.execute("UPDATE users SET is_banned=1 WHERE id=?", (uid,))
    conn.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
    conn.commit(); audit("user_ban", user["email"], f"target={uid}"); conn.close()
    return {"ok": True, "isBanned": True}

@app.post("/api/admin/users/{uid}/unban")
def admin_unban_user(uid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db(); _admin_target(conn, uid)
    conn.execute("UPDATE users SET is_banned=0 WHERE id=?", (uid,))
    conn.commit(); audit("user_unban", user["email"], f"target={uid}"); conn.close()
    return {"ok": True, "isBanned": False}

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
    vs = conn.execute("""
        SELECT v.*, (SELECT COUNT(*) FROM stock_items s WHERE s.variant_id=v.id AND s.sold=0) AS stock_count
        FROM variants v ORDER BY v.id
    """).fetchall()
    by_product = {}
    for v in vs:
        by_product.setdefault(v["productId"], []).append(
            {"id": v["id"], "name": v["name"], "price": v["price"], "stockCount": v["stock_count"]})
    out = [{"id": r["id"], "name": r["name"], "description": r["description"],
            "image": r["image"], "active": bool(r["active"]), "pinned": bool(r["pinned"]),
            "variants": by_product.get(r["id"], [])} for r in rows]
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
    pid = insert_id(conn, "INSERT INTO products(name,description,image,active,pinned,owner_id,created_at) VALUES(?,?,?,1,?,?,?)",
                    (data.name, data.description, data.image, data.pinned, user["id"], now()))
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
    vid = insert_id(conn, "INSERT INTO variants(productId,name,price,comparePrice,minQuantity,created_at) VALUES(?,?,?,NULL,1,?)",
                    (pid, data.name, data.price, now()))
    conn.commit(); conn.close()
    return {"id": vid, "ok": True}

class BulkStockIn(BaseModel):
    variantId: int
    contents: Optional[str] = None
    rawContent: Optional[str] = None

@app.post("/api/admin/stock/bulk")
def admin_bulk_stock(data: BulkStockIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    raw = data.rawContent if data.rawContent is not None else (data.contents or "")
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    conn = db()
    added = 0
    for l in lines:
        conn.execute("INSERT INTO stock_items(variant_id,content,created_at) VALUES(?,?,?)",
                     (data.variantId, l, now()))
        added += 1
    conn.commit(); conn.close()
    return {"ok": True, "added": added, "addedCount": added, "skipped": 0, "skippedCount": 0}

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
    rows = conn.execute("""SELECT d.*, u.email, u.username FROM deposits d JOIN users u ON u.id=d.user_id
                           ORDER BY d.id DESC""").fetchall()
    conn.close()
    return [deposit_out(r, r["email"], r["username"]) for r in rows]

@app.post("/api/admin/deposits/{did}/approve")
def approve_deposit(did: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    d = conn.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
    if not d:
        conn.close()
        raise ApiError(404, "Not found")
    if not settle_deposit(conn, d, actor=user["email"]):
        conn.rollback(); conn.close()
        raise ApiError(400, "Not a pending deposit")
    conn.commit()
    conn.close()
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
    return [discount_out(r) for r in rows]

class DiscountCodeIn(BaseModel):
    code: str
    percent: Optional[int] = None
    type: Optional[str] = "percent"
    value: Optional[Any] = None
    minOrder: Optional[float] = 0
    maxUses: Optional[int] = None
    expiresAt: Optional[str] = None

def _parse_dt(v) -> Optional[int]:
    """Parse an ISO / datetime-local string (or epoch) into epoch seconds."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        s = str(v).replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        try:
            return int(datetime.strptime(str(v), "%Y-%m-%dT%H:%M").timestamp())
        except Exception:
            return None

@app.post("/api/admin/discount-codes")
def admin_add_discount(data: DiscountCodeIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    dtype = (data.type or "percent").lower()
    if data.value is not None:
        value = float(data.value)
    elif data.percent is not None:
        value = float(data.percent)
    else:
        value = 0.0
    percent = int(value) if dtype == "percent" else (data.percent or 0)
    conn.execute("INSERT INTO discount_codes(code,percent,type,value,min_order,max_uses,used_count,expires_at,active) "
                 "VALUES(?,?,?,?,?,?,0,?,1) ON CONFLICT(code) DO NOTHING",
                 (data.code.upper(), percent, dtype, value, float(data.minOrder or 0),
                  data.maxUses, _parse_dt(data.expiresAt)))
    conn.commit(); conn.close()
    return {"ok": True, "type": dtype, "value": value}

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
    return {"ok": True, "success": True}

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
    return {"telegram": True, "bitcoin": True, "cashapp": True, "authorizeNet": False,
            "TELEGRAM_BOT_TOKEN": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
            "NOWPAYMENTS_API_KEY": bool(npp and npp.enabled()),
            "AUTHORIZE_NET_API_LOGIN_ID": bool(os.getenv("AUTHORIZE_NET_API_LOGIN_ID"))}

class ConfirmIn(BaseModel):
    confirm: Optional[str] = None

@app.post("/api/admin/clear-all-data")
def clear_all(user: dict = Depends(get_current_user), data: Optional[ConfirmIn] = None):
    require_admin(user)
    if data is not None and data.confirm is not None and data.confirm != "YES":
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
    return [{"id": r["id"], "code": r["code"], "value": r["value"], "amount": r["value"],
             "used": bool(r["used"]), "isUsed": bool(r["used"]), "createdAt": ts(r["created_at"])}
            for r in rows]

class CodeIn(BaseModel):
    code: Optional[str] = None
    value: Optional[float] = None
    amount: Optional[float] = None
    count: Optional[int] = None

@app.post("/api/admin/codes")
def admin_add_code(data: CodeIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if data.amount is not None:
        # SPA generateCodes: create N random wallet codes each worth `amount` cents.
        n = max(1, min(100, int(data.count or 1)))
        codes = []
        for _ in range(n):
            code = "PC" + secrets.token_hex(5).upper()
            conn.execute("INSERT INTO wallet_codes(code,value,used,created_at) VALUES(?,?,0,?) "
                         "ON CONFLICT(code) DO NOTHING", (code, data.amount, now()))
            codes.append(code)
        conn.commit(); audit("code_bulk", user["email"], f"{n}x{data.amount}"); conn.close()
        return {"codes": codes}
    if not data.code or data.value is None:
        conn.close()
        raise ApiError(400, "code+value or amount+count required")
    conn.execute("INSERT INTO wallet_codes(code,value,used,created_at) VALUES(?,?,0,?) ON CONFLICT(code) DO NOTHING",
                 (data.code.upper(), data.value, now()))
    conn.commit(); audit("code_add", user["email"], f"{data.code}=${data.value}"); conn.close()
    return {"ok": True, "codes": [data.code.upper()]}

@app.delete("/api/admin/codes/{cid}")
def admin_del_code(cid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("DELETE FROM wallet_codes WHERE id=?", (cid,))
    conn.commit(); conn.close()
    return {"ok": True}

# ================= SELLER + VOUCHES (PHASE F) =================
class SellerApplyIn(BaseModel):
    telegram: Optional[str] = None
    motivation: Optional[str] = None
    note: Optional[str] = None

@app.post("/api/seller/apply")
def seller_apply(data: SellerApplyIn, user: dict = Depends(get_current_user)):
    conn = db()
    telegram = data.telegram or user.get("telegram") or ""
    motivation = data.motivation or data.note
    conn.execute("""INSERT INTO seller_apps(user_id,telegram,motivation,status,created_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET telegram=excluded.telegram,
                    motivation=excluded.motivation, status='pending'""",
                 (user["id"], telegram, motivation, "pending", now()))
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
def vouch_token(payload: Optional[Dict[str, Any]] = None, user: dict = Depends(get_current_user)):
    token = "VOUCH-" + secrets.token_hex(4).upper()
    conn = db()
    conn.execute("INSERT INTO vouches(giver_id,token,order_id,created_at) VALUES(?,?,?,?)",
                 (user["id"], token, (payload or {}).get("orderId"), now()))
    conn.commit(); conn.close()
    return {"token": token, "telegramUrl": VOUCH_TG_URL}

@app.get("/api/vouches")
def my_vouches(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM vouches WHERE giver_id=?", (user["id"],)).fetchall()
    conn.close()
    return [{"token": r["token"], "usedBy": r["used_by"], "createdAt": ts(r["created_at"])} for r in rows]

# ================= SUPPORT + MISC (PHASE G) =================
class TicketIn(BaseModel):
    subject: str
    body: Optional[str] = None
    description: Optional[str] = None
    orderId: Optional[str] = None
    imageUrl: Optional[str] = None

@app.post("/api/support")
def create_ticket(data: TicketIn, user: dict = Depends(get_current_user)):
    body = data.body or data.description or ""
    conn = db()
    tid = insert_id(conn, "INSERT INTO tickets(user_id,subject,body,order_id,image_url,status,created_at) "
                          "VALUES(?,?,?,?,?,'open',?)",
                    (user["id"], data.subject, body, data.orderId, data.imageUrl, now()))
    conn.commit(); tg_notify(f"[{SITE}] Ticket #{tid} from {user['email']}: {data.subject}"); conn.close()
    return {"id": tid, "status": "open"}

@app.get("/api/support")
def my_tickets(user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    reps = conn.execute("""
        SELECT r.* FROM ticket_replies r JOIN tickets t ON t.id=r.ticket_id
        WHERE t.user_id=? ORDER BY r.id
    """, (user["id"],)).fetchall()
    by_ticket = {}
    for x in reps:
        by_ticket.setdefault(x["ticket_id"], []).append({"body": x["body"], "createdAt": ts(x["created_at"])})
    out = [{"id": r["id"], "subject": r["subject"], "body": r["body"], "description": r["body"],
            "orderId": r.get("order_id"), "imageUrl": r.get("image_url"), "status": r["status"],
            "createdAt": ts(r["created_at"]), "replies": by_ticket.get(r["id"], [])} for r in rows]
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
        conn.execute("INSERT INTO password_resets(email,code,expires,used) VALUES(?,?,?,0) "
                 "ON CONFLICT(email,code) DO UPDATE SET expires=excluded.expires,used=0",
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

# ================= TELEGRAM ACCOUNT LINKING =================
LINK_CODE_TTL = 900  # one-time code validity, seconds

def tg_api(method: str, payload: Optional[dict] = None, timeout: int = 15):
    """Call the Telegram Bot API. Returns parsed JSON, or None on failure."""
    if not TG_BOT:
        return None
    import urllib.request
    url = f"https://api.telegram.org/bot{TG_BOT}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as e:
        audit("telegram_api_error", method, str(e))
        return None

def tg_send(chat_id, text: str):
    return tg_api("sendMessage", {"chat_id": chat_id, "text": text,
                                  "disable_web_page_preview": True})

def _tg_link_user(chat_id, from_user, code: str):
    """Handle ``/link CODE`` from a private chat: verify the code and link."""
    first = (from_user.get("first_name") or "").strip()
    last = (from_user.get("last_name") or "").strip()
    handle = from_user.get("username") or ""
    tg_uid = str(from_user.get("id") or "")
    name_ok = "phantomcardz" in (first + last).lower().replace(" ", "") \
        or "phantomcardz" in (handle or "").lower()
    if not name_ok:
        tg_send(chat_id, "Please add “PhantomCardz” to your Telegram first or last name "
                         "(or username), then send the command again.")
        return
    conn = db()
    row = conn.execute("SELECT * FROM telegram_link_codes WHERE code=?", (code,)).fetchone()
    if not row or row["used"]:
        conn.close()
        tg_send(chat_id, "That code is invalid or already used. Generate a new one on your "
                         "Account page at phantomcardz.cc/account.")
        return
    if now() - int(row["created_at"] or 0) > LINK_CODE_TTL:
        conn.close()
        tg_send(chat_id, "That code has expired. Generate a fresh one on your Account page.")
        return
    taken = conn.execute("SELECT id FROM users WHERE telegram_id=? AND id<>?",
                         (tg_uid, row["user_id"])).fetchone()
    if taken:
        conn.close()
        tg_send(chat_id, "This Telegram account is already linked to another PhantomCardz account.")
        return
    conn.execute("UPDATE users SET telegram=?, telegram_id=? WHERE id=?",
                 (handle or first or "linked", tg_uid, row["user_id"]))
    conn.execute("UPDATE telegram_link_codes SET used=1 WHERE code=?", (code,))
    conn.commit()
    uname = conn.execute("SELECT username FROM users WHERE id=?", (row["user_id"],)).fetchone()
    conn.close()
    linked_as = uname["username"] if uname else "your account"
    tg_send(chat_id, f"✅ Linked! This Telegram account is now connected to PhantomCardz ({linked_as}). "
                     "Eligible reward payouts will be sent here.")
    audit("telegram_link", handle or tg_uid, f"user_id={row['user_id']}")

def process_telegram_update(update: dict):
    """Handle a single Telegram update (only ``/link`` and ``/start`` matter)."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    low = text.lower()
    if chat.get("type") != "private":
        if low.startswith("/link"):
            tg_send(chat_id, "For your security, send /link CODE to me in a private chat.")
        return
    parts = text.split()
    if low.startswith("/link"):
        code = parts[1].strip().upper() if len(parts) > 1 else ""
        if not code:
            tg_send(chat_id, "Usage: /link CODE — generate a code on your Account page first.")
            return
        _tg_link_user(chat_id, msg.get("from") or {}, code)
    elif low.startswith("/start"):
        tg_send(chat_id, f"Welcome to {SITE}! To link your account, generate a one-time code on "
                         "the Account page, then send /link CODE here in this private chat.")

_tg_poller_started = {"flag": False}

def _tg_poll_loop():
    tg_api("deleteWebhook", {"drop_pending_updates": False})
    offset = None
    while True:
        params = {"timeout": 30, "allowed_updates": json.dumps(["message", "edited_message"])}
        if offset is not None:
            params["offset"] = offset
        res = tg_api("getUpdates", params, timeout=40)
        if not (res and res.get("ok")):
            time.sleep(3)
            continue
        for upd in res.get("result", []):
            offset = upd.get("update_id", 0) + 1
            try:
                process_telegram_update(upd)
            except Exception as e:
                audit("telegram_update_error", "-", str(e))

def start_telegram_poller():
    if not TG_BOT or _tg_poller_started["flag"]:
        return
    _tg_poller_started["flag"] = True
    threading.Thread(target=_tg_poll_loop, daemon=True, name="tg-poller").start()

def _issue_link_code(conn, uid: int) -> str:
    """Return the user's active one-time code, minting a fresh one if needed."""
    row = conn.execute("SELECT code,created_at FROM telegram_link_codes "
                       "WHERE user_id=? AND used=0 ORDER BY created_at DESC LIMIT 1",
                       (uid,)).fetchone()
    if row and now() - int(row["created_at"] or 0) <= LINK_CODE_TTL:
        return row["code"]
    conn.execute("DELETE FROM telegram_link_codes WHERE user_id=? AND used=0", (uid,))
    code = secrets.token_hex(3).upper()
    conn.execute("INSERT INTO telegram_link_codes(code,user_id,created_at,used) VALUES(?,?,?,0)",
                 (code, uid, now()))
    conn.commit()
    return code

@app.get("/api/telegram/daily-rewards/link-code")
def tg_link_status(user: dict = Depends(get_current_user)):
    conn = db()
    r = conn.execute("SELECT telegram, telegram_id FROM users WHERE id=?", (user["id"],)).fetchone()
    if r and r["telegram_id"]:
        conn.close()
        return {"code": "", "linked": True,
                "telegramUsername": (r["telegram"] or "").lstrip("@") or None}
    code = _issue_link_code(conn, user["id"])
    conn.close()
    return {"code": code, "linked": False, "telegramUsername": None, "expiresIn": LINK_CODE_TTL}

@app.post("/api/telegram/daily-rewards/link-code")
@app.post("/api/telegram/link-code")
def tg_link_code(user: dict = Depends(get_current_user)):
    conn = db()
    code = _issue_link_code(conn, user["id"])
    conn.close()
    audit("telegram_link_code", user["email"], code)
    return {"code": code, "expiresIn": LINK_CODE_TTL}

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
    vs = conn.execute("""
        SELECT v.*, (SELECT COUNT(*) FROM stock_items s WHERE s.variant_id=v.id AND s.sold=0) AS stock_count
        FROM variants v WHERE v.productId=? ORDER BY v.id
    """, (pid,)).fetchall()
    vv = [{"id": v["id"], "productId": v["productId"], "name": v["name"], "price": v["price"],
           "comparePrice": v["comparePrice"], "minQuantity": v["minQuantity"], "stockCount": v["stock_count"]}
          for v in vs]
    conn.close()
    return {"id": r["id"], "name": r["name"], "description": r["description"], "image": r["image"],
            "active": bool(r["active"]), "pinned": bool(r["pinned"]), "createdAt": ts(r["created_at"]),
            "variants": vv}

# 2. crypto CHECKOUT — reserves stock, links order to deposit, admin approval completes the order
class CryptoOrderIn(BaseModel):
    variantId: Optional[int] = None
    quantity: int = 1
    currency: str = "BTC"
    items: Optional[List[CartItem]] = None
    discountCodeId: Optional[int] = None
    payCurrency: Optional[str] = None

@app.post("/api/orders/crypto")
def order_crypto(data: CryptoOrderIn, user: dict = Depends(get_current_user)):
    coin = (data.payCurrency or data.currency or "BTC").upper()
    conn = db()
    if data.items:
        try:
            oid, total, _ = _cart_order(conn, data.items, user, data.discountCodeId, payment_method=f"Crypto ({coin})")
        except ApiError:
            conn.rollback(); conn.close(); raise
        ref = f"ORD{oid}-{secrets.token_hex(3).upper()}"
        conn.execute("INSERT INTO deposits(user_id,method,amount,reference,status,created_at) VALUES(?,?,?,?,?,?)",
                     (user["id"], f"crypto:{coin}", total, ref, "pending", now()))
        conn.commit()
        _provider_payment(conn, ref, total, coin, user, "order")
        conn.commit()
        row = conn.execute("SELECT * FROM deposits WHERE reference=?", (ref,)).fetchone()
        conn.close()
        tg_notify(f"[{SITE}] CRYPTO ORDER #{oid} pending by {user['email']}: ${total / 100:.2f}")
        track = _crypto_tracking(row)
        return {"orderId": oid, "id": oid, "reference": ref, "amount": total, "total": total,
                "currency": coin, "payCurrency": coin, "address": track["payAddress"],
                "payAddress": track["payAddress"], "payAmount": track["payAmount"],
                "checkoutUrl": track.get("checkoutUrl"), "status": "pending",
                "paymentId": ref}
    if data.quantity < 1:
        raise ApiError(400, "Invalid quantity")
    if not data.variantId or data.variantId <= 0:
        raise ApiError(400, "Invalid variant")
    v = conn.execute("SELECT * FROM variants WHERE id=?", (data.variantId,)).fetchone()
    if not v:
        conn.close()
        raise ApiError(404, "Not found")
    stock_query = "SELECT * FROM stock_items WHERE variant_id=? AND sold=0 LIMIT ?"
    if conn.is_postgres:
        stock_query += " FOR UPDATE SKIP LOCKED"
    stock = conn.execute(stock_query, (data.variantId, data.quantity)).fetchall()
    if len(stock) < data.quantity:
        conn.close()
        raise ApiError(400, "Insufficient stock")
    total = round(v["price"] * data.quantity, 2)
    items = [s["content"] for s in stock]
    oid = insert_id(conn, "INSERT INTO orders(user_id,variant_id,quantity,total,status,items,payment_method,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?)",
                    (user["id"], data.variantId, data.quantity, total, "pending_payment", json.dumps(items),
                     f"Crypto ({coin})", now()))
    conn.execute("UPDATE orders SET order_id=? WHERE id=?", (f"ORD-{oid}", oid))
    conn.execute("INSERT INTO order_items(order_id,variant_id,quantity,price,contents,item_type) VALUES(?,?,?,?,?,?)",
                 (oid, data.variantId, data.quantity, v["price"], json.dumps(items), "product"))
    for s in stock:
        conn.execute("UPDATE stock_items SET sold=1, order_id=? WHERE id=?", (oid, s["id"]))
    ref = f"ORD{oid}-{secrets.token_hex(3).upper()}"
    conn.execute("INSERT INTO deposits(user_id,method,amount,reference,status,created_at) VALUES(?,?,?,?,?,?)",
                 (user["id"], f"crypto:{coin}", total, ref, "pending", now()))
    conn.commit()
    _provider_payment(conn, ref, total, coin, user, "order")
    conn.commit()
    row = conn.execute("SELECT * FROM deposits WHERE reference=?", (ref,)).fetchone()
    conn.close()
    tg_notify(f"[{SITE}] CRYPTO ORDER #{oid} pending by {user['email']}: ${total}")
    track = _crypto_tracking(row)
    return {"orderId": oid, "id": oid, "reference": ref, "amount": total, "total": total,
            "currency": coin, "payCurrency": coin, "address": track["payAddress"],
            "payAddress": track["payAddress"], "payAmount": track["payAmount"],
            "checkoutUrl": track.get("checkoutUrl"), "status": "pending", "paymentId": ref}

# 3. public variants listing
@app.get("/api/variants")
def variants_list():
    conn = db()
    rows = conn.execute("""
        SELECT v.*, (SELECT COUNT(*) FROM stock_items s WHERE s.variant_id=v.id AND s.sold=0) AS stock_count
        FROM variants v
    """).fetchall()
    out = [{"id": v["id"], "productId": v["productId"], "name": v["name"], "price": v["price"],
            "comparePrice": v["comparePrice"], "minQuantity": v["minQuantity"], "stockCount": v["stock_count"]}
           for v in rows]
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
    r = conn.execute("""SELECT ca.*, cb.name AS base_name, cb.price AS base_price, cb.hrPercent AS base_hr
                        FROM cards ca JOIN card_bases cb ON cb.id=ca.base_id
                        WHERE ca.id=? AND ca.buyer_id=?""", (cid, user["id"])).fetchone()
    conn.close()
    if not r:
        raise ApiError(404, "Not found")
    return card_out(r, r["base_name"], r["base_price"], r["base_hr"])

@app.get("/api/admin/orders")
def admin_orders(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("""SELECT o.*, u.email FROM orders o JOIN users u ON u.id=o.user_id
                           ORDER BY o.id DESC""").fetchall()
    out = [order_out(conn, r, with_user=True) for r in rows]
    conn.close()
    return out

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
    price: float = 0
    refundable: bool = False
    hrPercent: float = 80

@app.get("/api/admin/card-bases")
def admin_list_bases(user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("""
        SELECT b.*,
               (SELECT COUNT(*) FROM cards c WHERE c.base_id=b.id AND c.sold=0) AS stock_count,
               (SELECT COUNT(*) FROM cards c WHERE c.base_id=b.id)              AS card_count,
               (SELECT COUNT(*) FROM cards c WHERE c.base_id=b.id AND c.sold=1) AS sold_count
        FROM card_bases b ORDER BY b.id DESC
    """).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"], "price": r["price"], "active": bool(r["active"]),
             "refundable": bool(r["refundable"]), "hrPercent": r["hrPercent"],
             "stockCount": r["stock_count"], "cardCount": r["card_count"], "soldCount": r["sold_count"]}
            for r in rows]

@app.post("/api/admin/card-bases")
def admin_add_base(data: CardBaseIn, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    bid = insert_id(conn, "INSERT INTO card_bases(name,price,active,refundable,hrPercent) VALUES(?,?,1,?,?)",
                    (data.name, data.price, 1 if data.refundable else 0, data.hrPercent))
    conn.commit()
    conn.close()
    return {"ok": True, "id": bid}

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
    if "refundable" in payload:
        conn.execute("UPDATE card_bases SET refundable=? WHERE id=?", (1 if payload["refundable"] else 0, bid))
    if "hrPercent" in payload:
        conn.execute("UPDATE card_bases SET hrPercent=? WHERE id=?", (payload["hrPercent"], bid))
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
    rows = conn.execute("""SELECT ca.*, cb.name AS base_name, cb.price AS base_price, cb.hrPercent AS base_hr,
        bu.email AS buyer FROM cards ca
        JOIN card_bases cb ON cb.id=ca.base_id
        LEFT JOIN users bu ON bu.id=ca.buyer_id
        ORDER BY ca.id DESC LIMIT 500""").fetchall()
    conn.close()
    out = []
    for r in rows:
        o = card_out(r, r["base_name"], r["base_price"], r["base_hr"])
        o["base"] = r["base_name"]; o["sold"] = bool(r["sold"]); o["buyer"] = r["buyer"]
        out.append(o)
    return out

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

# license-file bulk import. The frozen client posts text/plain to
# ?variantId=... (stock lines); the legacy admin flow posts a file to
# ?baseId=... (card rows). Support both, and return added/skipped counts.
@app.post("/api/admin/stock/license-file")
async def admin_license_file(request: Request, variantId: Optional[int] = None,
                             baseId: Optional[int] = None,
                             file: Optional[UploadFile] = File(None),
                             user: dict = Depends(get_current_user)):
    require_admin(user)
    if file is not None:
        raw = (await file.read()).decode("utf-8", errors="ignore")
    else:
        raw = (await request.body()).decode("utf-8", errors="ignore")
    conn = db()
    added = 0
    skipped = 0
    if variantId is not None:
        v = conn.execute("SELECT id FROM variants WHERE id=?", (variantId,)).fetchone()
        if not v:
            conn.close()
            raise ApiError(404, "Not found")
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            conn.execute("INSERT INTO stock_items(variant_id,content,created_at) VALUES(?,?,?)",
                         (variantId, line, now()))
            added += 1
    else:
        if baseId is None:
            conn.close()
            raise ApiError(400, "variantId or baseId required")
        b = conn.execute("SELECT id FROM card_bases WHERE id=?", (baseId,)).fetchone()
        if not b:
            conn.close()
            raise ApiError(404, "Not found")
        for line in raw.splitlines():
            parts = [p.strip() for p in line.replace(":", "|").split("|")]
            if len(parts) == 4 and parts[0].isdigit() and len(parts[0]) >= 13:
                try:
                    conn.execute("INSERT INTO cards(base_id,number,exp_month,exp_year,cvv) VALUES(?,?,?,?,?)",
                                 (baseId, parts[0], int(parts[1]), int(parts[2]), parts[3]))
                    added += 1
                except Exception:
                    skipped += 1
            elif line.strip():
                skipped += 1
    conn.commit()
    conn.close()
    return {"ok": True, "added": added, "addedCount": added, "skipped": skipped, "skippedCount": skipped}

@app.get("/api/worker/card-bases")
def worker_bases(user: dict = Depends(get_current_user)):
    if user["rank"] not in ("admin", "worker"):
        raise ApiError(403, "Worker or admin only")
    conn = db()
    rows = conn.execute("""
        SELECT b.*, (SELECT COUNT(*) FROM cards c WHERE c.base_id=b.id AND c.sold=0) AS stock_count
        FROM card_bases b WHERE b.active=1
    """).fetchall()
    out = [{"id": r["id"], "name": r["name"], "price": r["price"], "stockCount": r["stock_count"],
            "refundable": bool(r["refundable"]), "hrPercent": r["hrPercent"]} for r in rows]
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

# ================= SPA COMPAT LAYER =================
# Routes the frozen React client calls that the original port did not expose.

def _require_staff(user: dict):
    if user["rank"] not in ("admin", "worker"):
        raise ApiError(403, "Worker or admin only")

class NewProductIn(BaseModel):
    name: str
    description: str = ""
    image: str = "/api/images/1"
    active: bool = True
    pinned: bool = False

@app.post("/api/products")
def create_product(data: NewProductIn, user: dict = Depends(get_current_user)):
    _require_staff(user)
    conn = db()
    pid = insert_id(conn, "INSERT INTO products(name,description,image,active,pinned,owner_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (data.name, data.description, data.image, 1 if data.active else 0, 1 if data.pinned else 0, user["id"], now()))
    conn.commit(); conn.close()
    return {"id": pid, "ok": True}

class NewVariantIn(BaseModel):
    productId: int
    name: str
    price: float
    minQuantity: int = 1

@app.post("/api/variants")
def create_variant(data: NewVariantIn, user: dict = Depends(get_current_user)):
    _require_staff(user)
    conn = db()
    p = conn.execute("SELECT id FROM products WHERE id=?", (data.productId,)).fetchone()
    if not p:
        conn.close()
        raise ApiError(404, "Product not found")
    vid = insert_id(conn, "INSERT INTO variants(productId,name,price,comparePrice,minQuantity,created_at) VALUES(?,?,?,NULL,?,?)",
                    (data.productId, data.name, data.price, data.minQuantity, now()))
    conn.commit(); conn.close()
    return {"id": vid, "ok": True}

class WorkerBaseIn(BaseModel):
    name: str
    price: float = 0
    refundable: bool = False

@app.post("/api/worker/card-bases")
def worker_add_base(data: WorkerBaseIn, user: dict = Depends(get_current_user)):
    _require_staff(user)
    conn = db()
    bid = insert_id(conn, "INSERT INTO card_bases(name,price,active,refundable,hrPercent) VALUES(?,?,1,?,80)",
                    (data.name, data.price, 1 if data.refundable else 0))
    conn.commit(); conn.close()
    return {"id": bid, "ok": True}

# ---- cards ----
class PurchaseCardIn(BaseModel):
    quantity: int = 1

@app.post("/api/cards/{cid}/purchase")
def purchase_card(cid: int, data: Optional[PurchaseCardIn] = None, user: dict = Depends(get_current_user)):
    conn = db()
    c = conn.execute("SELECT ca.*, cb.price AS base_price, cb.name AS base_name, cb.hrPercent AS base_hr "
                     "FROM cards ca JOIN card_bases cb ON cb.id=ca.base_id "
                     "WHERE ca.id=? AND ca.sold=0 AND cb.active=1", (cid,)).fetchone()
    if not c:
        conn.close()
        raise ApiError(400, "Out of stock")
    cur = conn.execute("UPDATE cards SET sold=1, buyer_id=? WHERE id=? AND sold=0", (user["id"], cid))
    if cur.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Card just sold, try again")
    cur2 = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                        (c["base_price"], user["id"], c["base_price"]))
    if cur2.rowcount != 1:
        conn.rollback(); conn.close()
        raise ApiError(400, "Insufficient balance")
    oid = insert_id(conn, "INSERT INTO orders(user_id,variant_id,quantity,total,status,items,payment_method,paid_amount,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                    (user["id"], None, 1, c["base_price"], "completed", "[]", "Balance", c["base_price"], now()))
    conn.execute("UPDATE orders SET order_id=? WHERE id=?", (f"CARD-{oid}", oid))
    conn.execute("INSERT INTO order_items(order_id,variant_id,quantity,price,contents,item_type,card_id) VALUES(?,?,?,?,?,?,?)",
                 (oid, None, 1, c["base_price"], json.dumps([c["number"]]), "card", cid))
    conn.execute("UPDATE orders SET items=? WHERE id=?", (json.dumps([c["number"]]), oid))
    u = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    conn.commit(); conn.close()
    out = card_out(c, c["base_name"], c["base_price"], c["base_hr"])
    out["newBalance"] = int(round(u["balance"]))
    return out

@app.get("/api/admin/card-bases/{bid}/cards")
def admin_base_cards(bid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("""SELECT ca.*, cb.name AS base_name, cb.price AS base_price, cb.hrPercent AS base_hr,
        bu.email AS buyer FROM cards ca
        JOIN card_bases cb ON cb.id=ca.base_id
        LEFT JOIN users bu ON bu.id=ca.buyer_id WHERE ca.base_id=? ORDER BY ca.id DESC LIMIT 1000""",
        (bid,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        o = card_out(r, r["base_name"], r["base_price"], r["base_hr"])
        o["sold"] = bool(r["sold"]); o["buyer"] = r["buyer"]
        out.append(o)
    return out

@app.delete("/api/admin/cards/{cid}")
def admin_del_card(cid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("DELETE FROM cards WHERE id=?", (cid,))
    conn.commit(); conn.close()
    return {"ok": True}

# ---- stock ----
@app.get("/api/admin/stock/{variant_id}")
def admin_variant_stock(variant_id: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    rows = conn.execute("""SELECT s.*, (CASE WHEN s.sold=1 AND o.status IN ('pending_payment','pending','unpaid','waiting_payment') THEN 1 ELSE 0 END) AS reserved
        FROM stock_items s LEFT JOIN orders o ON o.id=s.order_id
        WHERE s.variant_id=? ORDER BY s.id DESC LIMIT 1000""", (variant_id,)).fetchall()
    conn.close()
    return [{"id": r["id"], "content": r["content"], "isSold": bool(r["sold"]),
             "isReserved": bool(r["reserved"]), "orderId": r["order_id"]} for r in rows]

@app.delete("/api/admin/stock/{sid}")
def admin_del_stock(sid: int, user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    conn.execute("DELETE FROM stock_items WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return {"ok": True}

class StockAddIn(BaseModel):
    variantId: int
    rawContent: str

@app.post("/api/stock")
def stock_add(data: StockAddIn, user: dict = Depends(get_current_user)):
    _require_staff(user)
    lines = [l.strip() for l in (data.rawContent or "").splitlines() if l.strip()]
    conn = db()
    added = 0
    for l in lines:
        conn.execute("INSERT INTO stock_items(variant_id,content,created_at) VALUES(?,?,?)",
                     (data.variantId, l, now()))
        added += 1
    conn.commit(); conn.close()
    return {"ok": True, "addedCount": added, "added": added}

# ---- order actions ----
def _admin_order(conn, oid: int):
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        conn.close()
        raise ApiError(404, "Not found")
    return o

@app.post("/api/admin/orders/{oid}/deliver")
def admin_order_deliver(oid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db(); o = _admin_order(conn, oid)
    extra = payload.get("deliveryContent") or payload.get("items")
    if isinstance(extra, dict):
        conn.execute("UPDATE orders SET delivery_content=?, status='completed' WHERE id=?",
                     (json.dumps(extra), oid))
    elif extra:
        items = json.loads(o["items"] or "[]")
        if isinstance(extra, list):
            items.extend(str(x) for x in extra)
        else:
            items.append(str(extra))
        conn.execute("UPDATE orders SET items=?, status='completed' WHERE id=?", (json.dumps(items), oid))
    else:
        conn.execute("UPDATE orders SET status='completed' WHERE id=?", (oid,))
    conn.commit(); conn.close()
    audit("order_deliver", user["email"], f"id={oid}")
    return {"ok": True, "status": "fulfilled"}

@app.post("/api/admin/orders/{oid}/cashapp-fulfill")
def admin_order_cashapp_fulfill(oid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db(); _admin_order(conn, oid)
    conn.execute("UPDATE orders SET status='completed' WHERE id=?", (oid,))
    conn.commit(); conn.close()
    audit("order_cashapp_fulfill", user["email"], f"id={oid}")
    return {"ok": True, "status": "completed"}

@app.post("/api/admin/orders/{oid}/mark-unpaid")
def admin_order_mark_unpaid(oid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db(); _admin_order(conn, oid)
    conn.execute("UPDATE orders SET status='unpaid' WHERE id=?", (oid,))
    conn.commit(); conn.close()
    audit("order_mark_unpaid", user["email"], f"id={oid}")
    return {"ok": True, "status": "unpaid"}

@app.post("/api/admin/orders/{oid}/refund")
def admin_order_refund(oid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db(); o = _admin_order(conn, oid)
    conn.execute("UPDATE stock_items SET sold=0, order_id=NULL WHERE order_id=?", (oid,))
    conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (o["total"], o["user_id"]))
    conn.execute("UPDATE orders SET status='refunded' WHERE id=?", (oid,))
    conn.commit(); conn.close()
    audit("order_refund", user["email"], f"id={oid} amount={o['total']}")
    return {"ok": True, "status": "refunded", "refunded": o["total"]}

@app.post("/api/admin/orders/{oid}/replace")
def admin_order_replace(oid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        conn.close()
        raise ApiError(404, "Not found")
    ois = conn.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()
    conn.execute("UPDATE stock_items SET sold=0, order_id=NULL WHERE order_id=?", (oid,))
    contents = []
    if ois:
        for oi in ois:
            rows = conn.execute("SELECT * FROM stock_items WHERE variant_id=? AND sold=0 LIMIT ?",
                                (oi["variant_id"], oi["quantity"])).fetchall()
            if len(rows) < oi["quantity"]:
                conn.rollback(); conn.close()
                raise ApiError(400, "Insufficient stock to replace")
            piece = []
            for s in rows:
                conn.execute("UPDATE stock_items SET sold=1, order_id=? WHERE id=?", (oid, s["id"]))
                piece.append(s["content"])
            contents.extend(piece)
            conn.execute("UPDATE order_items SET contents=? WHERE id=?", (json.dumps(piece), oi["id"]))
    else:
        rows = conn.execute("SELECT * FROM stock_items WHERE variant_id=? AND sold=0 LIMIT ?",
                            (o["variant_id"], o["quantity"])).fetchall()
        if len(rows) < o["quantity"]:
            conn.rollback(); conn.close()
            raise ApiError(400, "Insufficient stock to replace")
        for s in rows:
            conn.execute("UPDATE stock_items SET sold=1, order_id=? WHERE id=?", (oid, s["id"]))
            contents.append(s["content"])
    conn.execute("UPDATE orders SET items=?, status='completed' WHERE id=?", (json.dumps(contents), oid))
    conn.commit(); conn.close()
    audit("order_replace", user["email"], f"id={oid}")
    return {"ok": True, "status": "completed", "items": contents}

@app.post("/api/orders/{oid}/live-check")
def order_live_check(oid: int, user: dict = Depends(get_current_user)):
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, user["id"])).fetchone()
    if not o:
        conn.close()
        raise ApiError(404, "Not found")
    d = conn.execute("SELECT * FROM deposits WHERE reference LIKE ? ORDER BY id DESC LIMIT 1",
                     (f"ORD{oid}-%",)).fetchone()
    if d and d["status"] == "pending":
        d = _refresh_crypto_deposit(conn, d)
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    conn.close()
    mapped = _order_status(o["status"])
    return {"id": o["id"], "orderId": _order_id_str(o), "status": mapped, "rawStatus": o["status"],
            "paid": mapped in ("fulfilled", "delivering", "replaced"),
            "paymentStatus": _client_status(d["status"]) if d else None}

# ---- support ----
@app.patch("/api/admin/support/{tid}")
def admin_patch_ticket(tid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    action = (payload.get("action") or "").lower()
    message = payload.get("message") or payload.get("body")
    if message:
        conn.execute("INSERT INTO ticket_replies(ticket_id,admin_id,body,created_at) VALUES(?,?,?,?)",
                     (tid, user["id"], str(message), now()))
    if action in ("close", "closed", "resolve", "resolved") or payload.get("status") == "closed":
        conn.execute("UPDATE tickets SET status='closed' WHERE id=?", (tid,))
    elif action in ("open", "reopen"):
        conn.execute("UPDATE tickets SET status='open' WHERE id=?", (tid,))
    conn.commit(); conn.close()
    return {"ok": True}

# ---- announcements ----
@app.patch("/api/admin/announcements/config")
def admin_patch_ann_config(payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    color = payload.get("bannerColor") or payload.get("color") or "#000000"
    latest = conn.execute("SELECT id FROM announcements ORDER BY id DESC LIMIT 1").fetchone()
    if latest:
        conn.execute("UPDATE announcements SET bannerColor=? WHERE id=?", (color, latest["id"]))
    else:
        conn.execute("INSERT INTO announcements(content,link,active,bannerColor,created_at) VALUES('',NULL,0,?,?)",
                     (color, now()))
    conn.commit(); conn.close()
    return {"ok": True, "bannerColor": color}

@app.patch("/api/admin/announcements/{aid}")
def admin_patch_announcement(aid: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if "active" in payload:
        conn.execute("UPDATE announcements SET active=? WHERE id=?", (1 if payload["active"] else 0, aid))
    if "content" in payload:
        conn.execute("UPDATE announcements SET content=? WHERE id=?", (payload["content"], aid))
    if "bannerColor" in payload:
        conn.execute("UPDATE announcements SET bannerColor=? WHERE id=?", (payload["bannerColor"], aid))
    conn.commit(); conn.close()
    return {"ok": True}

# ---- discounts ----
@app.patch("/api/admin/discount-codes/{did}")
def admin_patch_discount(did: int, payload: Dict[str, Any], user: dict = Depends(get_current_user)):
    require_admin(user)
    conn = db()
    if "isActive" in payload:
        conn.execute("UPDATE discount_codes SET active=? WHERE id=?", (1 if payload["isActive"] else 0, did))
    if "active" in payload:
        conn.execute("UPDATE discount_codes SET active=? WHERE id=?", (1 if payload["active"] else 0, did))
    if "percent" in payload:
        conn.execute("UPDATE discount_codes SET percent=? WHERE id=?", (payload["percent"], did))
    if "type" in payload:
        conn.execute("UPDATE discount_codes SET type=? WHERE id=?", (payload["type"], did))
    if "value" in payload:
        conn.execute("UPDATE discount_codes SET value=? WHERE id=?", (payload["value"], did))
    if "minOrder" in payload:
        conn.execute("UPDATE discount_codes SET min_order=? WHERE id=?", (payload["minOrder"], did))
    if "maxUses" in payload:
        conn.execute("UPDATE discount_codes SET max_uses=? WHERE id=?", (payload["maxUses"], did))
    if "expiresAt" in payload:
        conn.execute("UPDATE discount_codes SET expires_at=? WHERE id=?", (_parse_dt(payload["expiresAt"]), did))
    conn.commit(); conn.close()
    return {"ok": True}

# ================= STATIC (mount LAST) =================
app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="spa")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
