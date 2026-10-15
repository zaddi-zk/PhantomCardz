# Deploying PhantomCardz on Render

This guide covers running the backend on [Render](https://render.com). The app is a
single FastAPI file (`main.py`) plus a frozen React SPA (`frontend/`). All writable
state (SQLite DB, uploads, logs) is redirected to one configurable `DATA_DIR`.

## 1. Free tier truth — DEMO ONLY

Render's **Free** web services have an **ephemeral filesystem**. Everything written
outside a mounted disk — `market.db`, `uploads/`, `logs/` — is **wiped on every
deploy and every restart**. Free services also **spin down after 15 minutes of
inactivity** and take roughly a minute to wake on the next request.

So on the free tier: every restart re-seeds a fresh database from your environment
variables, uploaded files vanish, and the first request after idle is slow.

**The free tier is for demos and smoke-testing only. Never run a shop that handles
real money on it.** Use it to prove the deploy works, then move to a paid plan with a
persistent disk before going live.

## 2. Production path — Starter + persistent disk

- **Starter plan** (~$7/mo): always-on (no spin-down), always-warm, and it can attach
  a **persistent disk**.
- **Persistent disk**: 1 GB costs about **$0.25/GB/mo**. Mount it at **`/var/data`**.
- The included [`render.yaml`](../render.yaml) Blueprint already declares a Starter
  service with a 1 GB disk mounted at `/var/data` and sets `DATA_DIR=/var/data`, so
  the DB/uploads/logs all live on the disk and survive deploys and restarts.

## 3. Deploy steps

1. **Push the repo to GitHub.** Create a **PRIVATE** repository. `market.db`,
   `market.db-*`, `backups/`, `logs/`, `uploads/` and `.env` are already gitignored,
   so no local data or secrets are committed — but keep the repo private anyway
   because it contains your shop's code. `requirements.txt` and `render.yaml` are
   committed and must remain so.
2. **Render Dashboard → New → Blueprint.**
3. **Pick the repo.** Render reads `render.yaml` and proposes the `phantomcardz`
   web service with its disk.
4. **Fill the `sync: false` environment variables** (Render prompts you for each).
   In particular `ALLOWED_HOSTS` must include:
   - your Render URL host, e.g. `ph-xxxx.onrender.com`
   - `phantomcardz.cc,www.phantomcardz.cc` once you attach the custom domain
     (Settings → Custom Domains; Render provisions free managed TLS automatically).
5. **Deploy.** Render installs `requirements.txt` and starts
   `uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers`.

## 4. First boot on Render

On first boot the database seeds itself **fresh from environment variables** —
the admin account comes from `ADMIN_EMAIL` / `ADMIN_PASSWORD`, and products/defaults
are created on an empty DB.

Your **local `market.db` is intentionally gitignored and is NOT synced** to Render.
To migrate local data, upload `market.db` to the disk via the Render Shell on a paid
plan (e.g. `scp`/`curl` it into `/var/data/market.db`, or paste a dump), then restart
the service. Otherwise, simply start fresh in production.

## 5. Cookies & rate limiting

- `COOKIE_SECURE=true` is **required** behind Render's TLS so the HttpOnly `session`
  cookie is only sent over HTTPS. It is already set in `render.yaml`.
- The start command includes **`--proxy-headers`**, so `request.client.host` becomes
  the **real client IP** from Render's proxy. Without it, every request would look
  like it came from the proxy and the login/register rate-limit wall (HTTP 429) would
  throttle all users together.

## 6. SMTP setup for password-reset emails

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

## 7. Backups on Render

- Because SQLite now lives on the **persistent disk**, Render **automatically
  snapshots the disk daily** with **7-day retention**. This is your baseline safety net.
- For extra/offsite copies you can additionally schedule the existing
  `tools/backup.sh` (which hot-copies `market.db` into `backups/`) using either:
  - a **Background Worker** with a cron schedule that runs `bash tools/backup.sh`, or
  - an external scheduler (cron on a small VM) that triggers the same script.

  Note `backups/` also lives under `DATA_DIR`, so it is covered by the disk and its
  snapshots.
