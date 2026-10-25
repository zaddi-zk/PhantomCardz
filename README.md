# PhantomCardz

An online **cards & accounts marketplace**: buyers purchase stocked accounts and
card bases, manage a wallet, play provably-random games (dice, spin, mines,
plinko), and check purchased cards. Admins manage products, stock, deposits,
users, discount codes, announcements, and support tickets.

The project is a **FastAPI backend** (`main.py`) serving a **frozen React SPA**
(`frontend/`). It stores data in **SQLite** by default and switches to **PostgreSQL
(Supabase)** when `DATABASE_URL` is set. The SPA authenticates with **HttpOnly
session cookies**; the API also accepts **`Authorization: Bearer <token>`** for
programmatic clients, so it speaks both dialects.

## Requirements

- Python 3 (the `venv/` directory already contains the app's dependencies)
- `curl` (for the smoke test)

## Start / stop

```bash
tools/start.sh     # (re)start the API on http://0.0.0.0:8000, logs to market.log
tools/stop.sh      # stop the API
tools/smoke.sh     # auth + regression smoke test (PASS/FAIL per check)
tools/fullcheck.sh # full endpoint walker + money-integrity glitch probes
tools/backup.sh    # hot-backup market.db -> backups/market-<stamp>.db.gz (keeps last 14)
```

`market.db` is created and seeded on first run if missing (SQLite mode).

## Database — SQLite or Supabase PostgreSQL

The database is selected by the **`DATABASE_URL`** environment variable:

- **unset** → SQLite at `DATA_DIR/market.db` (default; ideal for local dev);
- **`postgres://` / `postgresql://` URL** → PostgreSQL. Point it at the Supabase
  **Session pooler** URI (IPv4-compatible), kept secret, e.g.
  `postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require`.

The schema auto-creates and seeds on an empty database. To copy an existing
`market.db` into an empty Supabase database **once**:

```bash
DATABASE_URL="postgresql://...?...sslmode=require" \
  python tools/migrate_sqlite_to_postgres.py --source market.db
```

The migrator maps the app's camelCase columns to their lowercase Postgres names,
resets the id sequences, and refuses to run into a non-empty database. See
[`docs/DEPLOY-RENDER.md`](docs/DEPLOY-RENDER.md) for the full Supabase + Render setup.

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
| `DATABASE_URL`   | SQLite when blank; PostgreSQL (Supabase) when a `postgres://` URL | *(empty → `market.db`)*                                      |
| `SITE_NAME`      | Brand name in titles/announcements                            | `PhantomCardz`                                                   |
| `ADMIN_EMAIL`    | Admin account seeded on a **fresh** DB                         | `admin@phantomcardz.cc`                                          |
| `ADMIN_PASSWORD` | Admin password seeded on a **fresh** DB                        | `Boss123!`                                                       |
| `ADMIN_TELEGRAM` | Telegram handle shown in deposit instructions                 | `@phantomadmin`                                                  |
| `CASHAPP_HANDLE` | Default CashApp handle in `manual_payments` defaults          | `$PhantomCardz`                                                  |
| `BTC_ADDRESS`    | Default BTC address in `manual_payments` defaults             | `bc1qphantom…dead`                                               |
| `ETH_ADDRESS`    | Default ETH address in `manual_payments` defaults             | `0xPhantom…dead`                                                 |
| `LTC_ADDRESS`    | Default LTC address in `manual_payments` defaults             | `ltc1qphantom…dead`                                              |
| `SOL_ADDRESS`    | Default SOL address in `manual_payments` defaults             | `PhantomSoL1111…`                                                |
| `NOWPAYMENTS_API_KEY` | Enables real-time crypto via NOWPayments (blank = static addresses + manual approval) | *(empty)*        |
| `NOWPAYMENTS_IPN_SECRET` | HMAC secret that verifies NOWPayments IPN callbacks      | *(empty)*                                                        |
| `NOWPAYMENTS_API_BASE` | Override API host (sandbox: `https://api-sandbox.nowpayments.io`) | `https://api.nowpayments.io`                            |
| `PUBLIC_BASE_URL` | Public origin used for the NOWPayments IPN callback URL      | *(falls back to `ALLOWED_HOSTS[0]`)*                             |
| `ALLOWED_HOSTS`  | TrustedHost + CORS allow-list (comma-separated)               | `phantomcardz.xyz,www.phantomcardz.xyz,localhost,127.0.0.1`      |
| `COOKIE_SECURE`  | Set `session` cookie `Secure` flag (requires HTTPS)           | `false`                                                          |
| `TELEGRAM_BOT_TOKEN` | Bot token for order/deposit/ticket notifications (blank = off) | *(empty)*                                                    |
| `TELEGRAM_CHAT_ID`  | Telegram chat id that receives notifications                  | *(empty)*                                                        |
| `TELEGRAM_CHANNEL_URL` | Telegram channel shown in the signup/logout welcome prompt | `https://t.me/phantomCardz` (used when unset or blank) |
| `TELEGRAM_VOUCH_URL` | Where the "Vouch" button sends users (channel/invite/bot link) | `https://t.me/+3-lMkt-idutkOTIx`                              |
| `PASSWORD_SALT`  | Static password-hash salt; keep stable (changing it invalidates passwords) | `PhantomCardz-pbkdf2-static-v1`                       |

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
├── main.py            # FastAPI backend — all routes, schema, cookie+token auth
├── database.py        # SQLite/PostgreSQL compatibility layer (DATABASE_URL switch)
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
│   ├── fullcheck.sh   # full endpoint walker + money-integrity probes
│   ├── migrate_sqlite_to_postgres.py  # one-shot market.db -> Supabase copy
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
- **Orders / wallet**: `POST /api/orders` (instant balance checkout — accepts
  `{variantId, quantity}` or the SPA shape `{items:[{variantId, quantity}], cardIds}`),
  `GET /api/orders`, `GET /api/orders/export`, `POST /api/orders/{id}/live-check`,
  `POST /api/wallet/redeem`, `GET /api/wallet/transactions`
- **Deposits**: `POST /api/orders/cashapp`, `POST /api/orders/crypto`,
  `POST /api/payments/crypto/create`, `GET /api/payments/crypto/{id}`,
  `GET /api/payments/crypto/{id}/status`, `GET /api/payments/crypto/status`,
  `GET /api/payments/crypto/currencies`, `POST /api/payments/webhook/nowpayments`
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

## Admin Panel

Log in as the admin (see below) and open **`/admin`** in the SPA. The panel exposes
dashboard, users (rank/balance/telegram), products, variants, stock, deposits
(approve/reject), orders, card bases, cards, wallet codes, discount codes,
announcements, payment methods, site settings (addresses/minimums), maintenance
mode, and support tickets.

## Telegram notifications (optional)

Order/deposit/ticket/register events are pushed to Telegram when configured:

1. Talk to **@BotFather** on Telegram → `/newbot` → copy the bot token.
2. Message your new bot once (so it can DM you), then get your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (`result[].message.chat.id`).
3. Put both into `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```
4. Restart: `tools/stop.sh && tools/start.sh`.

Notifications are fire-and-forget (never block or fail a request). Without the keys,
`tg_notify` is a no-op.

## Restocking workflows

- **Cards (card bases)**: Admin Panel → card bases → add base (`POST /api/admin/card-bases`
  `{name, price}`), then bulk-import inventory via **license-file**:
  `curl -F "file=@lic.txt" ".../api/admin/stock/license-file?baseId=<id>"` where the
  file has one card per line, `number|mm|yyyy|cvv` or `number:mm:yyyy:cvv`.
- **Products/accounts**: Admin Panel → products → create product
  (`POST /api/admin/products`), add variants (`POST /api/admin/products/{id}/variants`
  `{name, price}`), then bulk-load stock lines
  (`POST /api/admin/stock/bulk` `{variantId, contents}` — one item per line, e.g.
  `email:password`).

## Money-integrity protections

- **Negative-amount gates**: deposits, games (dice/spin/plinko/mines-start), orders
  and crypto-checkout reject `amount <= 0` / invalid `variantId` with 400.
- **Atomic mutations**: every money path enforces its precondition in the SQL UPDATE
  itself and checks `rowcount == 1`, rolling back otherwise —
  deposit approval (`WHERE status='pending'`), wallet-code redemption
  (`WHERE used=0`), card purchase (`WHERE sold=0` + balance check),
  order stock claims (`WHERE sold=0` per item), mines cashout
  (`WHERE active=1`), and the Telegram daily claim (`WHERE last_claim < today`).
  Double-approve / double-redeem / double-cashout return 400 and change nothing.
- **Audited retries**: money-relevant admin actions (deposit approve/reject, code add,
  user patch/delete, redeem, register, clear-all) append to `logs/audit.log`, so a
  failed or repeated attempt can be traced after the fact.

## Deployment

See [`docs/DEPLOY.md`](docs/DEPLOY.md) for DNS, systemd unit, nginx reverse proxy,
and Let's Encrypt HTTPS setup for `phantomcardz.xyz`.

## Running on Render

See [`docs/DEPLOY-RENDER.md`](docs/DEPLOY-RENDER.md) for the Render Blueprint
(`render.yaml`), the persistent-disk `DATA_DIR` layout, SMTP reset-email setup, and
backups. On Render the start command is `uvicorn main:app`; local development still
uses `tools/start.sh`.