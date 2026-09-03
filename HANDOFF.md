# Quick Drain Products — handoff

Everything the customer's side needs to run the store on their own VPS and
their own accounts. Read README.md for the full reference; this page is the
checklist.

## 1. DNS

| Type | Host | Value | TTL |
|---|---|---|---|
| A | `shop` (→ `shop.<your-domain>`) | the VPS IPv4 | 300 |
| TXT / MX | as listed on Resend → Domains (SPF on the `send` subdomain) | from Resend | 300 |
| TXT or CNAME | `resend._domainkey` (DKIM) | from Resend | 300 |
| TXT | `_dmarc` | `v=DMARC1; p=quarantine; rua=mailto:dmarc@<your-domain>` | 300 |

Check: `dig +short shop.<your-domain>` returns the VPS IP.

## 2. TLS

```bash
# in .env: SERVER_NAME=shop.<your-domain>  PUBLIC_PORT=80
./deploy.sh && ufw allow 443/tcp
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d shop.<your-domain>
# in .env: BASE_URL=https://shop.<your-domain>  COOKIE_SECURE=on
systemctl restart quick-drain && ./final_check.sh
```

## 3. `.env` values to fill on the customer box

Paths and identity: `APP_DIR`, `DB_PATH`, `MEDIA_DIR`, `BACKUP_DIR`, `BACKUP_REMOTE`,
`BASE_URL`, `SERVER_NAME`, `PORT`, `PUBLIC_PORT`, `WORKERS`, `SERVICE_USER`, `ENV=production`.

Secrets — all NEW, none copied from the developer's `.env` (see section 8):
`SECRET_KEY`, `COOKIE_SECURE`, `ADMIN_BASIC_AUTH_USER` / `ADMIN_BASIC_AUTH_PASSWORD` (or
`ADMIN_ALLOW_IPS`), `STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
`STRIPE_LIVE_OK`, `RESEND_API_KEY`, `RESEND_WEBHOOK_SECRET`, `EMAIL_FROM`, `EMAIL_REPLY_TO`,
`CONTACT_INBOX`, `POSTHOG_KEY`, `GOOGLE_SITE_VERIFICATION`.

Business settings to confirm: `FLAT_SHIPPING_CENTS`, `FREE_SHIPPING_THRESHOLD_CENTS`,
`SHIP_TO_COUNTRIES`, `ABANDONED_CART_HOURS`, `REORDER_REMINDER_LEAD_DAYS`, `PHONE_*`, `BOOKING_URL`.

## 4. Cron

Installed by `deploy.sh` as `/etc/cron.d/quick-drain` from `deploy/cron.txt`
(log at `<APP_DIR>/../quick-drain-cron.log`):

```
7 * * * *   <SERVICE_USER>  cd <APP_DIR> && <APP_DIR>/.venv/bin/python <APP_DIR>/scripts/run_lifecycle.py >> <APP_DIR>/../quick-drain-cron.log 2>&1
15 3 * * *  root            <APP_DIR>/backup.sh >> <APP_DIR>/../quick-drain-cron.log 2>&1
30 6 * * 1  root            <APP_DIR>/final_check.sh >> <APP_DIR>/../quick-drain-cron.log 2>&1
```

## 5. First admin

```bash
cd <APP_DIR>
.venv/bin/python scripts/create_admin.py <username> <email>
```

Sign in at `<BASE_URL>/admin/login`: password → authenticator QR (save the ten
backup codes) → emailed code. The emailed step needs a working
`RESEND_API_KEY`; it cannot be completed in dry-run mode.

## 6. Restore sequence (what `./handoff.sh` prints; also `RESTORE.txt` inside the bundle)

```bash
mkdir -p /opt && cd /opt
tar -xzf /path/to/quick-drain-handoff-<stamp>.tgz
cd quick-drain-handoff-<stamp> && tar -xf app.tar && mv app /opt/quick-drain-products
cd /opt/quick-drain-products
cp /opt/quick-drain-handoff-<stamp>/env.developer .env     # then edit: paths for this box + ALL new secrets
mkdir -p data && cp /opt/quick-drain-handoff-<stamp>/quick-drain.db data/quick-drain.db
tar -xf /opt/quick-drain-handoff-<stamp>/media.tar -C /opt/quick-drain-products
./deploy.sh
.venv/bin/python scripts/create_admin.py <username> <email>
./final_check.sh
shred -u /opt/quick-drain-handoff-<stamp>/env.developer && rm -rf /opt/quick-drain-handoff-<stamp>
```

## 7. Rollback

```bash
cd <APP_DIR> && git checkout <previous-hash> && ./deploy.sh    # deploy.sh prints the hash each run
# data: systemctl stop quick-drain; cp <BACKUP_DIR>/pre-deploy-<stamp>.db <DB_PATH>; rm -f <DB_PATH>-wal <DB_PATH>-shm; systemctl start quick-drain
```

## 8. SECRET ROTATION CHECKLIST

The handoff bundle carries the developer's `.env`. Nothing in it may survive
the transfer.

- [ ] `SECRET_KEY` regenerated (`openssl rand -hex 32`) — every existing session and password-reset link becomes invalid, which is the point
- [ ] Admin accounts recreated with new passwords (`scripts/create_admin.py`) and 2FA re-enrolled; developer admin rows deactivated or deleted
- [ ] `RESEND_API_KEY` and `RESEND_WEBHOOK_SECRET` regenerated on the customer's own Resend account; sending domain verified there; webhook endpoint recreated there
- [ ] `STRIPE_PUBLISHABLE_KEY` / `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` replaced with the customer's own Stripe account and the webhook endpoint recreated in that account — the store must not still route money into the developer's account after transfer; confirm with a test checkout that the payment lands in the customer's Dashboard
- [ ] `POSTHOG_KEY` replaced with a key from the customer's PostHog project
- [ ] `ADMIN_BASIC_AUTH_USER` / `ADMIN_BASIC_AUTH_PASSWORD` changed or removed (and `ADMIN_ALLOW_IPS` set to the customer's addresses)
- [ ] Old backups containing the old `.env` destroyed on both sides (`BACKUP_DIR`, `BACKUP_REMOTE`, the handoff bundle, any developer copies); `./backup.sh` run once after rotation so the newest archive holds only new secrets
- [ ] `./final_check.sh` run after rotation: 0 FAIL

## 9. Open items

- **Cloudflare not configured** — the origin IP is exposed; put the domain behind Cloudflare (proxied A record, SSL mode Full (strict), restrict ports 80/443 to Cloudflare IPs) when ready.
- **SDS not yet uploaded** — the product page keeps the "published when uploaded" wording until the safety data sheet (PDF) is uploaded on the admin product form (Documents → SDS); active ingredients stay blank until then.
- **Real product photography pending** — `static/img/products/` holds placeholder renders; replace them with real photos through the admin product images (then `./deploy.sh`, which runs `scripts/build_images.py` to regenerate the AVIF/WebP/JPEG renditions).
- **Prices to confirm** — seeded prices are placeholders (flag `prices_are_placeholders` in settings, shown as a notice in admin); editing any variant price in admin clears the flag automatically.
- Affiliate program, interactive calculators: deferred (README).
