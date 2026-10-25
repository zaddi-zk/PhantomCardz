# Deploying PhantomCardz on Render

This guide covers running the backend on [Render](https://render.com). The app is a
single FastAPI file (`main.py`) plus a frozen React SPA (`frontend/`). The database
is chosen at runtime by `DATABASE_URL` (see §3): **Supabase PostgreSQL** in
production, or **SQLite** on the persistent disk when `DATABASE_URL` is unset.
Uploads and logs always live under the configurable `DATA_DIR`.

## 1. Free tier truth — DEMO ONLY

Render's **Free** web services have an **ephemeral filesystem**. Everything written
outside a mounted disk — `uploads/`, `logs/`, and a SQLite `market.db` — is **wiped on
every deploy and every restart**. Free services also **spin down after 15 minutes of
inactivity** and take roughly a minute to wake on the next request.

So on the free tier: uploaded files vanish, logs reset, and the first request after
idle is slow. If you use Supabase for the database (§3), your data itself survives
restarts and redeploys — only the on-disk files are ephemeral.

**The free tier is for demos and smoke-testing only. Never run a shop that handles
real money on it.** Use it to prove the deploy works, then move to a paid plan before
going live.

## 2. Production path — Starter + persistent disk

- **Starter plan** (~$7/mo): always-on (no spin-down), always-warm, and it can attach
  a **persistent disk**.
- **Persistent disk**: 1 GB costs about **$0.25/GB/mo**. Mount it at **`/var/data`**.
- The included [`render.yaml`](../render.yaml) Blueprint already declares a Starter
  service with a 1 GB disk mounted at `/var/data` and sets `DATA_DIR=/var/data`, so
  uploads and logs live on the disk and survive deploys and restarts.

## 3. Database — Supabase PostgreSQL (production)

The app picks its database from **`DATABASE_URL`**:

- **set to a `postgres://` / `postgresql://` URL** → PostgreSQL (Supabase);
- **unset / blank** → SQLite at `DATA_DIR/market.db` (local development only).

Use the Supabase **Session pooler** connection string (IPv4-compatible), copied from
*Supabase → Project → Settings → Database → Connection string → Session pooler*:

```
postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

- The app speaks to Postgres through **psycopg 3** with a small pooled connection
  (`psycopg_pool`, max 5) that pre-pings borrowed connections.
- Keep the **`?sslmode=require`** suffix — Supabase requires TLS.
- The schema is **created automatically on first boot** and seeded fresh on an empty
  database (admin account from `ADMIN_EMAIL` / `ADMIN_PASSWORD`, default products).
- `render.yaml` declares `DATABASE_URL` with `sync: false`, so Render prompts you for
  it at deploy time; set the same value locally to run against Supabase.

### Migrating your local SQLite data to Supabase

Run the bundled migrator **once**, from the project root, while `DATABASE_URL` points
at an **empty** Supabase database:

```bash
DATABASE_URL="postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require" \
  python tools/migrate_sqlite_to_postgres.py --source market.db
```

It copies every table in one consistent snapshot, resets the Postgres id sequences,
and **refuses to run into a non-empty database** (so it can never half-overwrite a
live shop). The camelCase columns (`productId`, `comparePrice`, `minQuantity`,
`bannerColor`) are mapped to their lowercase Postgres names automatically.

## 4. Deploy steps

1. **Push the repo to GitHub.** Create a **PRIVATE** repository. `market.db`,
   `market.db-*`, `backups/`, `logs/`, `uploads/` and `.env` are already gitignored,
   so no local data or secrets are committed — but keep the repo private anyway
   because it contains your shop's code. `requirements.txt` and `render.yaml` are
   committed and must remain so.
2. **Create the Supabase project** and copy its **Session pooler** URL (§3).
3. **Render Dashboard → New → Blueprint.**
4. **Pick the repo.** Render reads `render.yaml` and proposes the `phantomcardz`
   web service with its disk.
5. **Fill the `sync: false` environment variables** (Render prompts you for each).
   Paste the Supabase URL into **`DATABASE_URL`**. `ALLOWED_HOSTS` must include:
   - your Render URL host, e.g. `ph-xxxx.onrender.com`
   - `phantomcardz.xyz,www.phantomcardz.xyz` once you attach the custom domain
     (Settings → Custom Domains; Render provisions free managed TLS automatically).
6. **Deploy.** Render installs `requirements.txt` and starts
   `uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers`.
7. **Migrate data** (optional) with the command in §3, then redeploy/restart.

## 5. First boot on Render

- **Postgres (`DATABASE_URL` set):** the schema is created in Supabase and seeded
  fresh if empty. Your local `market.db` is **not** used; copy it over with the
  migrator (§3) if you want your existing users/orders/stock.
- **SQLite (`DATABASE_URL` blank):** the database is created/seeded on the disk at
  `DATA_DIR/market.db`. To move local data, upload it to `/var/data/market.db` via the
  Render Shell on a paid plan, then restart.

## 6. Cookies & rate limiting

- `COOKIE_SECURE=true` is **required** behind Render's TLS so the HttpOnly `session`
  cookie is only sent over HTTPS. It is already set in `render.yaml`.
- The start command includes **`--proxy-headers`**, so `request.client.host` becomes
  the **real client IP** from Render's proxy. Without it, every request would look
  like it came from the proxy and the login/register rate-limit wall (HTTP 429) would
  throttle all users together.

## 7. SMTP setup for password-reset emails

Password-reset codes are **delivered** rather than only printed. Configure an SMTP
sender (a Gmail app-password works, or any provider such as SendGrid/Mailgun/Postmark):

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@yourdomain.com
SMTP_PASSWORD=your-app-password
SMTP_FROM=you@yourdomain.com
RESET_DEV_CODE=false
```

With SMTP configured, `POST /api/password/forgot` emails the code (and sends a copy
to Telegram when the bot is configured). Set **`RESET_DEV_CODE=false` in production**
so the code is never returned in the API response. Locally (no SMTP), leaving
`RESET_DEV_CODE` at its default (`true`) returns a `devCode` for convenience — this
is for development only.

## 8. Real-time crypto payments (NOWPayments)

Out of the box, crypto deposits show a static address and require an admin to
approve them manually. To automate crediting/fulfillment, enable NOWPayments:

1. Create a NOWPayments account and generate an **API key** (Store settings → API keys).
2. Set the **IPN callback URL** to `https://<your-domain>/api/payments/webhook/nowpayments`
   and copy the **IPN secret key**.
3. Set these env vars on Render (see `render.yaml`, all `sync: false`):

```
NOWPAYMENTS_API_KEY=<api key>
NOWPAYMENTS_IPN_SECRET=<ipn secret>
PUBLIC_BASE_URL=https://www.phantomcardz.xyz
# NOWPAYMENTS_API_BASE=https://api-sandbox.nowpayments.io   # sandbox only
```

With a key set, `POST /api/payments/crypto/create` creates a hosted invoice and
returns a live `payAddress`/`payAmount`/`checkoutUrl`. Status is refreshed from
the provider on poll (`GET /api/payments/crypto/{id}/status`) and pushed via the
IPN webhook. A confirmed payment settles **idempotently**: it completes the linked
order (reference `ORD<orderId>-…`) or credits the user's balance exactly once.

## 9. Backups

- **Postgres (Supabase):** Supabase takes **daily backups** (and point-in-time
  recovery on paid plans) — manage/verify these in the Supabase dashboard. The
  `tools/backup.sh` SQLite hot-copy does **not** apply.
- **SQLite (persistent disk):** Render **automatically snapshots the disk daily**
  with **7-day retention**. For extra/offsite copies, schedule `tools/backup.sh`
  (which hot-copies `market.db` into `backups/`) via a Background Worker cron or an
  external scheduler. `backups/` also lives under `DATA_DIR`, so it is covered by
  the disk and its snapshots.
