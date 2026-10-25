# PhantomCardz — Deployment (phantomcardz.xyz)

Target: single Ubuntu/Debian host running the FastAPI app (`main.py`) behind nginx,
managed by systemd, with HTTPS via Let's Encrypt.

## 0. Environment (do this FIRST)

Production **must** set `COOKIE_SECURE=true` and roll `.env` **before the first
start**, and `ALLOWED_HOSTS` **must contain the domain** (TrustedHost rejects
everything else):

```bash
cp .env.example .env
# edit .env:
#   COOKIE_SECURE=true
#   ALLOWED_HOSTS=phantomcardz.xyz,www.phantomcardz.xyz,localhost,127.0.0.1
#   ADMIN_PASSWORD=<strong-secret>   # only seeds a FRESH market.db
```

`COOKIE_SECURE=true` requires HTTPS (the `Secure` cookie is dropped over plain HTTP),
so complete the nginx/certbot steps in the same session. On an **existing**
`market.db`, `ADMIN_PASSWORD` in `.env` is ignored — rotate via
`POST /api/user/password` instead.

## 1. DNS

Point the domain at the server's public IP:

| Type  | Name            | Value         |
|-------|-----------------|---------------|
| A     | phantomcardz.xyz | `<SERVER_IP>` |
| CNAME | www             | phantomcardz.xyz |

Verify: `dig +short phantomcardz.xyz` and `dig +short www.phantomcardz.xyz`.

## 2. systemd service

Create `/etc/systemd/system/phantomcardz.service`:

```ini
[Unit]
Description=PhantomCardz API
After=network.target

[Service]
User=milo
WorkingDirectory=/home/milo/PhantomCardz
ExecStart=/home/milo/PhantomCardz/venv/bin/python main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now phantomcardz
```

Check: `systemctl status phantomcardz` and `journalctl -u phantomcardz -f`.

## 3. nginx reverse proxy

Create `/etc/nginx/sites-available/phantomcardz`:

```nginx
server {
    listen 80;
    server_name phantomcardz.xyz www.phantomcardz.xyz;
    client_max_body_size 10M;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Enable and reload:

```bash
sudo ln -s /etc/nginx/sites-available/phantomcardz /etc/nginx/sites-enabled/ \
  && sudo nginx -t && sudo systemctl reload nginx
```

## 4. HTTPS (Let's Encrypt)

```bash
sudo apt install certbot python3-certbot-nginx \
  && sudo certbot --nginx -d phantomcardz.xyz -d www.phantomcardz.xyz
```

Certbot installs the certificate, rewrites the nginx server block for TLS, and sets up
auto-renewal (`systemctl status certbot.timer`). Test renewal with
`sudo certbot renew --dry-run`.

## Notes

- The app listens on `0.0.0.0:8000` and stores state in `market.db` (SQLite) plus `uploads/`.
  Back up both.
- `market.db` is created/seeded on first startup if missing.
- Only nginx (80/443) should be exposed publicly; port 8000 stays bound to localhost in
  production via firewall or `proxy_pass http://127.0.0.1:8000`.
