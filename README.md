# Quick Drain Products

The online store for **Quick Shot**, Quick Drain's monthly drain maintainer
(natural drain enzyme, 4 fl oz / 118 mL, one bottle = one drain-month). It is
the retail arm of the Long Island sewer and drain company at quickdrainny.com:
the store sells the maintainer, the parent site handles the diagnostic visit.

## Stack

- Python 3.11, FastAPI 0.x + Starlette 0.x (pinned below 1.0), Jinja2 (autoescape on)
- SQLite in WAL mode (`app/schema.sql`, `app/db.py` migrations), one connection per request
- Stripe hosted Checkout + webhooks, Stripe Tax; Resend for email (+ Svix-signed webhooks)
- Tailwind v4 built to a hashed stylesheet by `build_css.sh`; vanilla JS in `static/js/app.js`; no bundler at runtime
- uvicorn on 127.0.0.1 behind nginx; systemd; ufw + fail2ban; cron for lifecycle, backups and checks
- Analytics: PostHog (only when `POSTHOG_KEY` is set)

## Quick start (local)

```bash
git clone <repo> quick-drain-products && cd quick-drain-products
cp .env.example .env            # set ENV=development, SECRET_KEY=$(openssl rand -hex 32), Stripe TEST keys
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./build_css.sh                  # downloads the Tailwind standalone CLI on first run
.venv/bin/python scripts/seed.py
.venv/bin/python scripts/create_admin.py admin you@example.com
.venv/bin/uvicorn app.main:app --reload --port 8006
```

Then open http://127.0.0.1:8006 (docs at /docs while `ENV` is not `production`).
`scripts/check_templates.py` parses every template; `pytest` runs the tests.

## Environment variables (`.env.example` — names only, never commit `.env`)

| Variable | Meaning |
|---|---|
| `APP_NAME` | Display name used in titles and emails |
| `ENV` | `production` hides /docs and enforces the config checks; anything else is development |
| `APP_DIR` | Absolute path of this checkout (must equal where deploy.sh runs) |
| `DB_PATH` | Absolute path of the SQLite file (outside static/) |
| `MEDIA_DIR` | Uploads and SDS files (outside static/) |
| `BACKUP_DIR` | Where backup.sh / deploy.sh / handoff.sh write (outside static/) |
| `BACKUP_REMOTE` | Optional off-box copy target: rclone `remote:path` or scp `user@host:/path` |
| `BASE_URL` | Public origin, no trailing slash (`http://<ip>:8086` in phase 1, `https://shop.<domain>` after TLS) |
| `SERVER_NAME` | nginx `server_name` (the VPS IP in phase 1, `shop.<domain>` after DNS) |
| `PORT` | Loopback port uvicorn binds (8006) |
| `PUBLIC_PORT` | Port nginx listens on (8086 in phase 1, 80 once DNS points here) |
| `WORKERS` | uvicorn worker count |
| `LOG_LEVEL` | uvicorn/app log level |
| `TZ` | Timezone for dates shown to staff and customers |
| `SERVICE_USER` | System account the service runs as (deploy.sh creates it) |
| `TAILWIND_VERSION` | Tailwind CLI version pinned by build_css.sh |
| `SECRET_KEY` | Session/CSRF signing key, 32+ chars (`openssl rand -hex 32`); the app refuses to boot without it |
| `COOKIE_SECURE` | `on` marks cookies Secure — required once BASE_URL is https, impossible on plain http |
| `SESSION_DAYS` | Session cookie lifetime |
| `ADMIN_BASIC_AUTH_USER` | Optional HTTP basic auth in front of /admin (phase 1 lock) |
| `ADMIN_BASIC_AUTH_PASSWORD` | Password for the above |
| `ADMIN_ALLOW_IPS` | Optional comma-separated IP allow-list for /admin |
| `ADMIN_2FA` | `full` (password + authenticator + emailed code, default), `authenticator` (no emailed code), or `off` (password only; first-setup convenience, final_check fails while set) |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key (test until told otherwise) |
| `STRIPE_SECRET_KEY` | Stripe secret key; `sk_live_` is refused unless `STRIPE_LIVE_OK=on` |
| `STRIPE_WEBHOOK_SECRET` | Signing secret of the `/webhooks/stripe` endpoint |
| `STRIPE_TAX_ENABLED` | Enables Stripe Tax on Checkout sessions |
| `SUBSCRIPTIONS_ENABLED` | Shows the subscribe-and-save option |
| `STRIPE_LIVE_OK` | Explicit opt-in to live keys |
| `FLAT_SHIPPING_CENTS` | Flat shipping rate |
| `FREE_SHIPPING_THRESHOLD_CENTS` | Cart subtotal that makes shipping free |
| `SHIP_TO_COUNTRIES` | Comma-separated ISO country codes allowed at checkout |
| `RESEND_API_KEY` | Resend API key; empty means every email is a dry run |
| `RESEND_WEBHOOK_SECRET` | Svix signing secret of the `/webhooks/resend` endpoint |
| `EMAIL_FROM` | Sender, e.g. `Quick Drain Products <orders@shop.example.com>` (verified domain) |
| `EMAIL_REPLY_TO` | Reply-to address |
| `CONTACT_INBOX` | Where contact-form notifications go |
| `EMAIL_DRY_RUN` | `on` logs sends instead of calling Resend |
| `PARENT_SITE_URL` | quickdrainny.com cross-links |
| `BOOKING_URL` | "Book a visit" link target |
| `PHONE_DISPLAY` | Phone as shown |
| `PHONE_TEL` | Phone as `tel:` link |
| `POSTHOG_KEY` | PostHog project key (empty disables analytics and the CSP entry) |
| `POSTHOG_HOST` | PostHog ingest host |
| `GOOGLE_SITE_VERIFICATION` | Search Console verification meta content |
| `ABANDONED_CART_HOURS` | Hours before the abandoned-cart email |
| `REORDER_REMINDER_LEAD_DAYS` | Days before the dose runs out to send the reorder reminder |

## Deploy (Ubuntu 22.04/24.04, as root)

```bash
git clone <repo> /opt/quick-drain-products && cd /opt/quick-drain-products
cp .env.example .env && nano .env        # APP_DIR=/opt/quick-drain-products, paths, SECRET_KEY, keys
./deploy.sh
.venv/bin/python scripts/create_admin.py <username> <email>
./final_check.sh
```

`deploy.sh` is idempotent and safe on a box with other live sites: it writes
only its own systemd unit, the `zz-quick-drain` nginx site (sorts last, never
the default server), its own fail2ban filter/jail, and `/etc/cron.d/quick-drain`.
It reloads nginx and fail2ban, restarts only `quick-drain`, and binds only
`PORT` (loopback) and `PUBLIC_PORT`. Every run backs up the database before
migrating, compiles the Python and parses every template before restarting,
and restores the previous CSS manifest if a build fails.

### Updates

```bash
cd /opt/quick-drain-products && git pull && ./deploy.sh
```

### Restart, logs, rollback

```bash
systemctl restart quick-drain
journalctl -u quick-drain -f
git checkout <previous-hash> && ./deploy.sh      # deploy.sh prints the previous hash on every run
```

Pre-migration database copies are in `BACKUP_DIR/pre-deploy-<stamp>.db`
(newest five kept). To roll the data back: stop the service, copy one over
`DB_PATH`, delete `DB_PATH-wal` and `DB_PATH-shm`, start the service.

## Phase 1: the raw-port warning

While the store is reachable as `http://<vps-ip>:8086` there is no TLS.
That means:

- customer passwords, admin passwords, TOTP codes and session cookies cross the wire in plaintext;
- `COOKIE_SECURE` cannot be `on` (browsers would drop the cookie), so `BASE_URL` must stay `http://`;
- Stripe and Resend webhook traffic is unauthenticated in transit (signatures still verify, but the payload is readable).

So in phase 1: **Stripe test keys only**, **no real customer registrations**,
and lock `/admin` with one of:

```
ADMIN_ALLOW_IPS=<your ip>                       # or
ADMIN_BASIC_AUTH_USER=... ADMIN_BASIC_AUTH_PASSWORD=...   # or
ufw insert 1 allow from <your ip> to any port 8086 && ufw deny 8086/tcp
```

(`ufw deny` alone does not block an address if an allow rule sits above it —
insert the specific rule at position 1.)

## TLS

1. DNS: add an **A record**, host `shop` (so the name is `shop.<your-domain>`), value = the VPS IPv4, TTL 300. Wait until `dig +short shop.<your-domain>` returns the IP.
2. In `.env` set `SERVER_NAME=shop.<your-domain>` and `PUBLIC_PORT=80`, run `./deploy.sh` (it opens 80 in ufw; also `ufw allow 443/tcp`).
3. `apt-get install -y certbot python3-certbot-nginx && certbot --nginx -d shop.<your-domain>` — certbot edits only the `zz-quick-drain` site, adds the 443 block and the 80→443 redirect (the shape is shown, commented, at the bottom of `deploy/nginx.conf`).
4. In `.env` set `BASE_URL=https://shop.<your-domain>` and `COOKIE_SECURE=on`, then `systemctl restart quick-drain` (or `./deploy.sh`).
5. `./final_check.sh` must now report `BASE_URL is https, COOKIE_SECURE=on`.

Renewal is automatic (`systemctl list-timers | grep certbot`).

## Stripe

1. Dashboard → Developers → Webhooks → **Add endpoint**: `https://<domain>/webhooks/stripe`.
2. Events: `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, `checkout.session.expired`, `charge.refunded`, `customer.subscription.updated`, `customer.subscription.deleted`, `customer.subscription.created`, `invoice.paid`, `invoice.payment_failed`.
3. Paste the endpoint's signing secret (`whsec_…`) into `STRIPE_WEBHOOK_SECRET`, restart. Until it is set the endpoint answers 503 and paid orders are not recorded.
4. Stripe Tax: Dashboard → Tax → Registrations → add **New York**; keep `STRIPE_TAX_ENABLED=on` (Checkout sessions are created with `automatic_tax`).
5. Local testing: `stripe listen --forward-to http://127.0.0.1:8006/webhooks/stripe` prints a temporary `whsec_` for your local `.env`; `stripe trigger checkout.session.completed` exercises the handler.

The webhook handler is idempotent (`processed_events` table) and returns 500 on
an internal error so Stripe retries.

## Resend

1. Resend → Domains → add the sending domain (e.g. `shop.<your-domain>`) and add the DNS records exactly as listed: the **SPF TXT** (and MX) on the `send` subdomain, the **DKIM** record (`resend._domainkey`, TXT or CNAME as shown), and a **DMARC TXT** at `_dmarc.<your-domain>`: `v=DMARC1; p=quarantine; rua=mailto:dmarc@<your-domain>`.
2. Create an API key → `RESEND_API_KEY`; set `EMAIL_FROM` to an address on the verified domain.
3. Resend → Webhooks → add `https://<domain>/webhooks/resend` for `email.sent`, `email.delivered`, `email.bounced`, `email.complained`; paste the signing secret into `RESEND_WEBHOOK_SECRET`. Bounces and complaints go to `email_suppressions` so the address is never mailed again.
4. `EMAIL_DRY_RUN=off` once the domain is verified.

## PostHog

Project settings → Project API key → `POSTHOG_KEY` (host stays
`https://us.i.posthog.com` unless the project is in the EU). Empty key = no
script tag, no CSP entry.

## Google Search Console (at launch)

Add the property `https://shop.<your-domain>`, choose the **HTML tag** method,
paste the `content="…"` value into `GOOGLE_SITE_VERIFICATION`, restart, verify.
Then Sitemaps → submit `https://shop.<your-domain>/sitemap.xml` (`robots.txt`
already references it).

## Cron

`deploy.sh` installs `deploy/cron.txt` as `/etc/cron.d/quick-drain` (log:
`<APP_DIR>/../quick-drain-cron.log`, never under static/):

```
7 * * * *   quickdrain  cd /opt/quick-drain-products && /opt/quick-drain-products/.venv/bin/python /opt/quick-drain-products/scripts/run_lifecycle.py >> /opt/quick-drain-cron.log 2>&1
15 3 * * *  root        /opt/quick-drain-products/backup.sh >> /opt/quick-drain-cron.log 2>&1
30 6 * * 1  root        /opt/quick-drain-products/final_check.sh >> /opt/quick-drain-cron.log 2>&1
```

## Backups

`./backup.sh` writes `BACKUP_DIR/quick-drain-<stamp>.tgz` containing a
checkpointed, integrity-checked copy of the database, `MEDIA_DIR` and `.env`.
Fourteen local copies are kept. With `BACKUP_REMOTE` set the archive is also
copied off-box (rclone for a configured `remote:path`, scp otherwise; scp needs
a key in root's `~/.ssh`). `./handoff.sh` builds the full transfer bundle
(checkout + DB + media + .env + restore steps). `./final_check.sh` warns when
the newest backup is older than 36 hours.

Restore one archive: stop the service, `tar -xzf` it in a scratch directory,
copy `quick-drain.db` over `DB_PATH` (delete `-wal`/`-shm`), copy `media/` over
`MEDIA_DIR`, start the service.

## Admin sign-in (2FA)

`/admin/login`: password → authenticator app (TOTP; first sign-in shows the
QR code and ten one-time backup codes) → six-digit code emailed to the admin's
address. All three every time; `ADMIN_2FA_REQUIRED=on` is the default and
`final_check.sh` fails if any active admin has not enrolled.

Create or reset an admin (a reset clears 2FA so it re-enrols):

```bash
.venv/bin/python scripts/create_admin.py <username> <email>
```

With `EMAIL_DRY_RUN=on` (or no `RESEND_API_KEY`) the email step is logged to
the journal instead of sent — `journalctl -u quick-drain | grep 'EMAIL DRY RUN'`
shows the recipient, template (`admin_code`) and subject. The log line does
**not** include the code itself and the body is not stored, so the third
factor cannot be completed in dry-run mode: point `RESEND_API_KEY` at a
verified domain before the first admin sign-in.

## Security invariants (implemented in code)

- Sessions are a signed cookie (itsdangerous, `HttpOnly`, `SameSite=Lax`, `Secure` when `COOKIE_SECURE=on`); the app refuses to boot without a 32+ char `SECRET_KEY`.
- Every state-changing form carries a CSRF token; public forms carry a honeypot (`website`) field.
- Security headers on every response: CSP with `script-src 'self'` (plus the PostHog host only when configured), `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`; HSTS once cookies are secure; `Cache-Control: no-store` on `/admin`, `/account`, `/cart`, `/checkout`.
- `/docs`, `/redoc`, `/openapi.json` are disabled when `ENV=production`.
- `/admin` sits behind an optional IP allow-list and/or HTTP basic auth (constant-time compare), then password (argon2id, policy-checked) + TOTP with hashed backup codes + hashed, expiring email code; per-account lockout after failures; every attempt is written to `admin_login_attempts` and sign-ins to `audit_log`.
- Rate limits (SQLite `rate_limits` table) on admin login/2FA/email code, customer login/register/reset, checkout, contact, order lookup, newsletter, back-in-stock, reviews, RMAs and discount codes.
- Stripe: hosted Checkout (no card data touches the server), webhook signature verification, `processed_events` idempotency, live keys refused unless `STRIPE_LIVE_OK=on`.
- Resend webhooks are Svix-signature verified; bounces/complaints suppress the address.
- Jinja autoescape is on everywhere; the `md` filter is the only path for HTML from content; user text is never `|safe`.
- uvicorn binds 127.0.0.1 and trusts proxy headers from 127.0.0.1 only, so rate limits and allow-lists see the real client IP behind nginx.
- Runs as a non-login system user under systemd hardening (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, `ProtectHome=read-only`); `.env` is mode 640; backups and the database live outside `static/` (checked by deploy.sh and final_check.sh).
- fail2ban bans an address after 10 admin sign-in posts in 15 minutes for one hour.

## Ported UI components (`templates/components/`)

| File | Source |
|---|---|
| `booking_cta.html` | Own — cross-sell to the service business |
| `cart_drawer.html` | shadcn/ui "sheet" (side=right), MIT; Radix Dialog behaviour re-implemented in `static/js/app.js` |
| `cart_drawer_body.html` | Own — server-rendered drawer body, re-rendered by `/cart/drawer` |
| `dose_ledger.html` | Own — the signature twelve-month dosing ledger |
| `faq.html` | shadcn/ui "accordion" (Radix Accordion markup + ARIA), MIT |
| `flash_toast.html` | shadcn/ui "sonner"-style toast markup, MIT |
| `footer.html` | Own |
| `header.html` | shadcn/ui "navigation-menu" + "sheet" block layout, MIT |
| `icons.html` | Lucide icon paths (ISC) as an inline SVG macro |
| `mobile_nav.html` | shadcn/ui "sheet" (side=left), MIT |
| `newsletter_modal.html` | shadcn/ui "dialog", MIT |
| `picture.html` | Own — AVIF → WebP → JPEG `<picture>` pipeline |
| `reviews.html` | shadcn/ui "card" + "progress", MIT |
| `variant_selector.html` | shadcn/ui "radio-group" as bordered tiles, MIT |

shadcn/ui is MIT-licensed, so its markup and ARIA patterns were ported freely
(the Radix runtime behaviour is re-implemented in vanilla JS because the site
ships no React). No individual components from 21st.dev were used: licensing
there is set per component by each author and is not uniform, so nothing from
it was copied.

## Deferred

- **Affiliate program**: only the nullable `referral_code` column on carts and orders exists; nothing reads it yet.
- **Interactive calculators** (dose/coverage beyond the static ledger table).
- **Cloudflare** in front of the origin (the VPS IP is currently exposed).

## One-line install from a phone

On a fresh Ubuntu VPS, as root:

```
curl -fsSL https://raw.githubusercontent.com/jackpaypal6969-maker/quickdrainproducts/main/deploy/bootstrap.sh | bash -s -- <vps-ip> 8086
```

It clones into `/opt/quick-drain`, writes a first `.env` (paths, `SECRET_KEY`, `BASE_URL=http://<vps-ip>:8086`, `EMAIL_DRY_RUN=on`), and runs `deploy.sh`. Re-running it pulls the latest code and keeps the existing `.env`. Once a domain and TLS exist, pass the URL instead of the IP: `bash -s -- https://shop.your-domain 443` and set `COOKIE_SECURE=on` in `.env`.

## Subscribe-and-save

Customers can choose **one-time** or **subscribe** on the product page and pick a delivery interval. The admin controls it under **Settings**:

| Setting | Meaning | Default |
|---|---|---|
| `subscription_discount_percent` | Discount off the one-time price for subscribers (site-wide) | 10 |
| `subscription_intervals` | Intervals offered, in months, comma separated | `1,2,3` |
| Variant → *Subscription discount %* | Per-pack override of the site-wide percent (blank = use site-wide) | blank |
| `SUBSCRIPTIONS_ENABLED` (.env) | Master switch; `off` hides the option entirely | `on` |

How it works:

- Checkout runs in Stripe **subscription mode** with recurring `price_data` at the subscriber price and the delivery interval as `interval_count`. Shipping below the free threshold is a recurring line so it is charged every cycle. Stripe Tax applies per invoice.
- The first order is created by `checkout.session.completed` exactly like a one-time purchase, and a row is written to `subscriptions` (lines, interval, shipping, Stripe ids).
- Every later cycle Stripe sends `invoice.paid` with `billing_reason=subscription_cycle`; the webhook creates a **fulfilment order** for the recorded lines (atomic stock decrement, on-hold if short), emails the confirmation, and records the next renewal date. It is idempotent on the event id and on the invoice id.
- `invoice.payment_failed` marks the subscription *past_due*; Stripe's Smart Retries handle dunning. `customer.subscription.updated/deleted` keep status, `cancel_at_period_end` and the renewal date in sync.
- From **Your account** a customer can cancel (takes effect at period end, no further charges), resume before the period ends, or open Stripe's **billing portal** to update the card or address. Enable the portal once in the Stripe dashboard (Settings → Billing → Customer portal) so the button works.
- Renewal orders show in the admin like any other order, tagged `utm_source=subscription`, and count in the reports.
- A cart holds one subscription interval at a time; one-time items can ride along in the same checkout.
