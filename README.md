# PhantomCardz

An online **cards & accounts marketplace**: buyers purchase stocked accounts and
card bases, manage a wallet, play provably-random games (dice, spin, mines,
plinko), and check purchased cards. Admins manage products, stock, deposits,
users, discount codes, announcements, and support tickets.

The project is a **FastAPI + SQLite backend** (`main.py`) serving a **frozen React
SPA** (`frontend/`). The SPA authenticates with **HttpOnly session cookies**; the
API also accepts **`Authorization: Bearer <token>`** for programmatic clients, so
it speaks both dialects.

## Requirements

- Python 3 (the `venv/` directory already contains the app's dependencies)
- `curl` (for the smoke test)

## Start / stop

```bash
tools/start.sh     # (re)start the API on http://0.0.0.0:8000, logs to market.log
tools/stop.sh      # stop the API
tools/smoke.sh     # run the auth + regression checks against the running server
tools/backup.sh    # hot-backup market.db -> backups/market-<stamp>.db.gz (keeps last 14)
```

`market.db` is created and seeded on first run if missing.

## Admin login

```
email:    admin@phantomcardz.cc
password: Boss123!
```

The SPA logs in with the same credentials; the login response sets an HttpOnly
`session` cookie.

## Configuration (environment variables)

Settings are read from a `.env` file at the project root (loaded at startup; real
process environment variables take precedence). Copy `.env.example` to `.env` and
edit. `.env` is git-ignored; `.env.example` is committed.

| Key              | Effect                                                        | Default                                                          |
|------------------|---------------------------------------------------------------|------------------------------------------------------------------|
| `SITE_NAME`      | Brand name in titles/announcements                            | `PhantomCardz`                                                   |
| `ADMIN_EMAIL`    | Admin account seeded on a **fresh** DB                         | `admin@phantomcardz.cc`                                          |
| `ADMIN_PASSWORD` | Admin password seeded on a **fresh** DB                        | `Boss123!`                                                       |
| `ADMIN_TELEGRAM` | Telegram handle shown in deposit instructions                 | `@phantomadmin`                                                  |
| `CASHAPP_HANDLE` | Default CashApp handle in `manual_payments` defaults          | `$PhantomCardz`                                                  |
| `BTC_ADDRESS`    | Default BTC address in `manual_payments` defaults             | `bc1qphantom…dead`                                               |
| `ETH_ADDRESS`    | Default ETH address in `manual_payments` defaults             | `0xPhantom…dead`                                                 |
| `LTC_ADDRESS`    | Default LTC address in `manual_payments` defaults             | `ltc1qphantom…dead`                                              |
| `ALLOWED_HOSTS`  | TrustedHost + CORS allow-list (comma-separated)               | `phantomcardz.cc,www.phantomcardz.cc,localhost,127.0.0.1`        |
| `COOKIE_SECURE`  | Set `session` cookie `Secure` flag (requires HTTPS)           | `false`                                                          |

**These values only apply to fresh tables/defaults.** On an existing `market.db`,
changing `ADMIN_PASSWORD` in `.env` has no effect — rotate the admin password via
`POST /api/user/password` (Bearer/cookie authed). To (re)seed from `.env`, delete
`market.db` with `.env` in place and start the server.

### Rotating `.env`

1. Edit `.env` (e.g. new payment addresses).
2. Restart: `tools/stop.sh && tools/start.sh`.
3. Payment addresses / minimums can also be changed at runtime without a restart
   via `POST /api/admin/site-settings` (admin only).

## Backups

```bash
tools/backup.sh
# -> backups/market-<YYYYmmdd-HHMMSS>.db.gz  (uses sqlite3 ".backup", safe while running)
```

Keeps the 14 most recent archives. Back up `uploads/` separately if you allow uploads.

## Folder map

```
/home/milo/PhantomCardz/
├── main.py            # FastAPI backend — all routes, SQLite schema, cookie+token auth
├── market.db          # SQLite database (auto-created/seeded) — git-ignored
├── market.log         # runtime log (git-ignored)
├── .env               # local config (git-ignored); see .env.example
├── .env.example       # documented config template (committed)
├── README.md          # this file
├── docs/
│   └── DEPLOY.md      # DNS, systemd, nginx, HTTPS deployment guide
├── tools/
│   ├── start.sh       # start the server
│   ├── stop.sh        # stop the server
│   ├── smoke.sh       # auth + regression smoke test (PASS/FAIL per check)
│   └── backup.sh      # sqlite3 hot backup -> backups/*.db.gz
├── backups/           # generated DB backups (git-ignored)
├── logs/              # audit.log (git-ignored)
├── frontend/          # frozen React SPA (index.html + assets/) — DO NOT EDIT
├── uploads/           # user-uploaded files, served at /uploads (git-ignored)
└── venv/              # Python virtual environment (git-ignored)
```

## API surface (high level)

- **Auth**: `POST /api/login`, `POST /api/register`, `POST /api/logout`, `GET /api/user`,
  `GET /api/user/rank` (includes `discountPct`)
- **Catalog**: `GET /api/products`, `GET /api/stock`, `GET /api/card-bases`
- **Orders / wallet**: `POST /api/orders`, `GET /api/orders`, `GET /api/orders/export`,
  `POST /api/wallet/redeem`, `GET /api/wallet/transactions`
- **Deposits**: `POST /api/orders/cashapp`, `POST /api/payments/crypto/create`,
  `GET /api/payments/crypto/{id}/status`, `GET /api/payments/crypto/currencies`
- **Cards / checker**: `POST /api/cards`, `GET /api/user/cards`, `POST /api/checker/check`
- **Games**: `POST /api/games/{dice,spin,mines,plinko}`, `GET /api/games/history`
- **Admin**: `/api/admin/*` (dashboard, users, products, variants, stock, deposits,
  discount codes, announcements, payment methods, support, `POST /api/admin/site-settings`)

## Production hardening

- **Auth**: dual dialect — `Authorization: Bearer <token>` or HttpOnly `session` cookie.
- **Passwords**: PBKDF2-HMAC-SHA256 (200k iterations). Legacy SHA-256 hashes still
  verify and are upgraded on the next password change.
- **Rate limiting**: login ≤10/60s and register ≤5/h per client IP.
- **Host/CORS**: locked to `ALLOWED_HOSTS`; cross-origin credentials enabled for those hosts.
- **SPA routing**: deep links (`/login`, `/products`, …) return `index.html` on refresh;
  `/api/*` and `/uploads/*` 404s stay JSON.
- **Audit trail**: admin/register/clear actions appended to `logs/audit.log`.
- **Indexes**: hot lookup paths indexed in `init_db`.

## Deployment

See [`docs/DEPLOY.md`](docs/DEPLOY.md) for DNS, systemd unit, nginx reverse proxy,
and Let's Encrypt HTTPS setup for `phantomcardz.cc`.