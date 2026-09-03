#!/usr/bin/env bash
#
# Quick Drain Products — install / update on a bare Ubuntu 22.04/24.04 VPS.
#
#   sudo ./deploy.sh        (run from the clone; safe to run again and again)
#
# Built for a box that already hosts other live sites. It only ever writes
# its own files (/etc/systemd/system/quick-drain.service, the zz-quick-drain
# nginx site, filter.d/quick-drain.conf, jail.d/quick-drain.local,
# /etc/cron.d/quick-drain), never edits another nginx file, never restarts
# anything but quick-drain (nginx and fail2ban are *reloaded*), and never
# touches ports 8002, 8083, 8085, 3001 or 3009 — it binds only PORT (loopback)
# and opens only PUBLIC_PORT in ufw.
#
# Steps: root check -> .env -> apt -> service user -> git pull --ff-only ->
# venv + pip -> CSS + images -> pre-migration DB backup -> compile + template
# check (STOP here on failure, manifest restored) -> migrate -> seed if empty
# -> systemd + nginx (nginx -t before reload) -> ufw -> fail2ban -> cron ->
# permissions -> restart -> verify -> rollback / first-admin hints.
#
set -euo pipefail

APP_DIR_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR_HERE"
# shellcheck source=deploy/lib.sh
. "$APP_DIR_HERE/deploy/lib.sh"

[ "$(id -u)" = "0" ] || die "Run as root:  sudo ./deploy.sh"

# ---------------------------------------------------------------- .env
step "Reading .env"
[ -f .env ] || die "No .env here. cp .env.example .env, fill it in, run again."
load_env "$APP_DIR_HERE/.env"
require_vars APP_DIR DB_PATH MEDIA_DIR BACKUP_DIR BASE_URL SERVER_NAME PORT PUBLIC_PORT SECRET_KEY WORKERS SERVICE_USER
APP_DIR="$(readlink -f -- "$APP_DIR")"
[ "$APP_DIR" = "$APP_DIR_HERE" ] || die "APP_DIR in .env is $APP_DIR but this clone is $APP_DIR_HERE. Make them match."
[ "${#SECRET_KEY}" -ge 32 ] || die "SECRET_KEY must be at least 32 chars:  openssl rand -hex 32"
is_under "$BACKUP_DIR" "$APP_DIR/static" && die "BACKUP_DIR is under static/ — backups would be served to the internet."
is_under "$(dirname "$DB_PATH")" "$APP_DIR/static" && die "DB_PATH is under static/ — the database would be served to the internet."
case "$BASE_URL" in
  https://*) [ "$(printf '%s' "${COOKIE_SECURE:-off}" | tr 'A-Z' 'a-z')" = "on" ] || warn "BASE_URL is https but COOKIE_SECURE is not on — the app will log a CONFIG error and refuse to set secure cookies" ;;
  http://*)  say "plaintext phase (BASE_URL=$BASE_URL): Stripe TEST keys only, lock /admin — see README" ;;
  *) die "BASE_URL must start with http:// or https://" ;;
esac
ok ".env loaded  APP_DIR=$APP_DIR  PORT=$PORT  PUBLIC_PORT=$PUBLIC_PORT  user=$SERVICE_USER"

# ---------------------------------------------------------------- packages
step "System packages"
NEED=""
for p in python3-venv python3-pip nginx sqlite3 curl gettext-base ufw fail2ban git; do
  dpkg -s "$p" >/dev/null 2>&1 || NEED="$NEED $p"
done
if [ -n "$NEED" ]; then
  say "installing:$NEED"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # shellcheck disable=SC2086
  apt-get install -y -qq $NEED >/dev/null
  ok "installed$NEED"
else
  ok "all packages present"
fi

# ---------------------------------------------------------------- service user
step "Service user"
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
  ok "$SERVICE_USER exists"
else
  useradd --system --no-create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  ok "created system user $SERVICE_USER"
fi

# ---------------------------------------------------------------- git
step "Source"
git_() { git -c safe.directory="$APP_DIR" "$@"; }
PREV_COMMIT="$(git_ rev-parse --short HEAD 2>/dev/null || echo "")"
if [ -n "$PREV_COMMIT" ]; then
  say "current commit $PREV_COMMIT (rollback target)"
  if git_ remote get-url origin >/dev/null 2>&1; then
    if git_ pull --ff-only --quiet 2>/dev/null; then
      ok "git pull --ff-only -> $(git_ rev-parse --short HEAD)"
    else
      warn "git pull --ff-only failed (offline, or local commits ahead) — deploying the checkout as-is"
    fi
  else
    say "no git remote; deploying the checkout as-is"
  fi
else
  warn "not a git checkout; rollback by commit is unavailable"
fi

# ---------------------------------------------------------------- python
step "Python environment"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  ok "created .venv"
fi
.venv/bin/pip install -q --upgrade pip >/dev/null 2>&1 || true
.venv/bin/pip install -q -r requirements.txt
ok "requirements installed ($(.venv/bin/python --version 2>&1))"

# ---------------------------------------------------------------- assets
step "Assets"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
PREV_MANIFEST=""
if [ -f static/css/manifest.json ]; then
  cp -p static/css/manifest.json "$SCRATCH/manifest.prev.json"
  PREV_MANIFEST="$SCRATCH/manifest.prev.json"
fi
restore_manifest() {
  if [ -n "$PREV_MANIFEST" ] && [ -f "$PREV_MANIFEST" ]; then
    cp -p "$PREV_MANIFEST" static/css/manifest.json
    warn "restored previous css manifest ($(cat static/css/manifest.json))"
  fi
}
if ./build_css.sh; then
  ok "css built ($(cat static/css/manifest.json))"
else
  restore_manifest
  die "build_css.sh failed. Nothing was restarted; the running service is untouched."
fi
if [ -f scripts/build_images.py ]; then
  if .venv/bin/python scripts/build_images.py; then ok "image renditions built"; else warn "build_images.py failed — existing renditions stay"; fi
else
  say "scripts/build_images.py not present; skipping image renditions"
fi

# ---------------------------------------------------------------- pre-migration backup
step "Database backup (before migrate)"
mkdir -p "$BACKUP_DIR" "$(dirname "$DB_PATH")" "$MEDIA_DIR"
if [ -f "$DB_PATH" ]; then
  PRE="$BACKUP_DIR/pre-deploy-$(date +%Y%m%d-%H%M%S).db"
  db_backup "$DB_PATH" "$PRE" || die "sqlite backup of $DB_PATH failed — not migrating on top of an unbacked database"
  chmod 600 "$PRE"
  ok "copy at $PRE ($(human_size "$PRE"))"
  ls -1t "$BACKUP_DIR"/pre-deploy-*.db 2>/dev/null | tail -n +6 | xargs -r rm -f
else
  say "no database yet at $DB_PATH — first deploy"
fi

# ---------------------------------------------------------------- compile + templates
step "Compile and template check"
if ! .venv/bin/python -m compileall -q app scripts >/dev/null; then
  restore_manifest
  die "Python does not compile. Nothing was restarted; fix the error and run again."
fi
ok "python compiles"
if ! .venv/bin/python scripts/check_templates.py; then
  restore_manifest
  die "A template fails to parse. Nothing was restarted; fix it and run again."
fi
ok "templates parse"

# ---------------------------------------------------------------- migrate + seed
step "Migrate"
.venv/bin/python -c 'from app.db import migrate; migrate()' || die "migration failed — restore from $BACKUP_DIR if the schema is half-applied"
ok "schema up to date"
PRODUCTS="$(.venv/bin/python -c 'from app.db import connect; c = connect(); print(c.execute("SELECT COUNT(*) FROM products").fetchone()[0]); c.close()')"
if [ "$PRODUCTS" = "0" ]; then
  .venv/bin/python scripts/seed.py
  ok "seeded empty catalog"
else
  ok "catalog has $PRODUCTS product(s); seed skipped"
fi

# ---------------------------------------------------------------- systemd
step "systemd unit"
export APP_DIR PORT WORKERS SERVICE_USER PUBLIC_PORT SERVER_NAME
UNIT=/etc/systemd/system/quick-drain.service
envsubst '${APP_DIR} ${PORT} ${WORKERS} ${SERVICE_USER}' < deploy/quick-drain.service > "$SCRATCH/unit"
if ! cmp -s "$SCRATCH/unit" "$UNIT" 2>/dev/null; then install -m 644 "$SCRATCH/unit" "$UNIT"; ok "wrote $UNIT"; else ok "$UNIT unchanged"; fi
# Data outside APP_DIR needs to be writable through ProtectSystem=full.
DROPIN_DIR=/etc/systemd/system/quick-drain.service.d
EXTRA=""
for d in "$(dirname "$DB_PATH")" "$MEDIA_DIR" "$BACKUP_DIR"; do
  is_under "$d" "$APP_DIR" || EXTRA="$EXTRA $(readlink -f -- "$d")"
done
if [ -n "$EXTRA" ]; then
  mkdir -p "$DROPIN_DIR"
  printf '[Service]\nReadWritePaths=%s\n' "${EXTRA# }" > "$DROPIN_DIR/paths.conf"
  ok "drop-in ReadWritePaths:${EXTRA}"
else
  rm -f "$DROPIN_DIR/paths.conf" 2>/dev/null || true
fi
systemctl daemon-reload

# ---------------------------------------------------------------- nginx
step "nginx site (zz-quick-drain only; other sites untouched)"
SITE_AVAIL=/etc/nginx/sites-available/zz-quick-drain
SITE_ENABLED=/etc/nginx/sites-enabled/zz-quick-drain
envsubst '${APP_DIR} ${PORT} ${PUBLIC_PORT} ${SERVER_NAME}' < deploy/nginx.conf > "$SCRATCH/site"
if [ -f "$SITE_AVAIL" ]; then cp -p "$SITE_AVAIL" "$SCRATCH/site.prev"; fi
install -m 644 "$SCRATCH/site" "$SITE_AVAIL"
mkdir -p /etc/nginx/sites-enabled
ln -sfn "$SITE_AVAIL" "$SITE_ENABLED"
if nginx -t >/dev/null 2>&1; then
  systemctl reload nginx
  ok "nginx -t passed; reloaded (listen $PUBLIC_PORT, server_name $SERVER_NAME)"
else
  if [ -f "$SCRATCH/site.prev" ]; then install -m 644 "$SCRATCH/site.prev" "$SITE_AVAIL"; else rm -f "$SITE_ENABLED" "$SITE_AVAIL"; fi
  nginx -t 2>&1 | tail -n 3 | sed 's/^/  /'
  die "nginx -t failed with the new site; previous nginx state restored, nginx NOT reloaded."
fi

# ---------------------------------------------------------------- ufw
step "Firewall"
if have ufw; then
  if ufw status 2>/dev/null | grep -q "^Status: active"; then
    ufw allow "${PUBLIC_PORT}/tcp" >/dev/null && ok "ufw allow ${PUBLIC_PORT}/tcp"
  else
    warn "ufw is installed but inactive — not enabling it from a script (that can lock out SSH). Enable by hand: ufw allow OpenSSH && ufw allow ${PUBLIC_PORT}/tcp && ufw enable"
  fi
else
  warn "ufw missing; port ${PUBLIC_PORT} relies on the provider firewall"
fi

# ---------------------------------------------------------------- fail2ban
step "fail2ban"
install -m 644 deploy/fail2ban/quick-drain.conf /etc/fail2ban/filter.d/quick-drain.conf
mkdir -p /etc/fail2ban/jail.d
envsubst '${PUBLIC_PORT}' < deploy/fail2ban/jail.local > /etc/fail2ban/jail.d/quick-drain.local
chmod 644 /etc/fail2ban/jail.d/quick-drain.local
systemctl enable -q fail2ban 2>/dev/null || true
if systemctl is-active -q fail2ban; then
  fail2ban-client reload >/dev/null 2>&1 && ok "jail quick-drain loaded (maxretry 10 / 15m, ban 1h)" || warn "fail2ban reload failed: fail2ban-client -d | tail"
else
  systemctl start fail2ban && ok "fail2ban started with jail quick-drain" || warn "fail2ban did not start; check: journalctl -u fail2ban"
fi

# ---------------------------------------------------------------- cron
step "cron"
CRON_LOG="$(readlink -f -- "$APP_DIR/..")/quick-drain-cron.log"
touch "$CRON_LOG" && chown "$SERVICE_USER:$SERVICE_USER" "$CRON_LOG" && chmod 664 "$CRON_LOG"
envsubst '${APP_DIR} ${SERVICE_USER}' < deploy/cron.txt > /etc/cron.d/quick-drain
chmod 644 /etc/cron.d/quick-drain
ok "/etc/cron.d/quick-drain (hourly lifecycle, nightly backup, weekly check) -> $CRON_LOG"

# ---------------------------------------------------------------- permissions
step "Permissions"
chown -R "$SERVICE_USER:$SERVICE_USER" "$(dirname "$DB_PATH")" "$MEDIA_DIR" "$BACKUP_DIR"
chmod 750 "$BACKUP_DIR"
chgrp "$SERVICE_USER" .env && chmod 640 .env
if ! runuser -u "$SERVICE_USER" -- test -r "$APP_DIR/app/main.py" -a -x "$APP_DIR/.venv/bin/python"; then
  die "$SERVICE_USER cannot read $APP_DIR. Fix with: chmod o+rx $(dirname "$APP_DIR") $APP_DIR  (and any parent under /root or /home)"
fi
ok "data, media, backups owned by $SERVICE_USER; .env 640"

# ---------------------------------------------------------------- restart
step "Restart quick-drain"
systemctl enable -q quick-drain
systemctl restart quick-drain
sleep 8

# ---------------------------------------------------------------- verify
step "Verify"
HEALTH="$(curl -s --max-time 8 "http://127.0.0.1:${PORT}/healthz" || true)"
if printf '%s' "$HEALTH" | grep -q '"ok": *true'; then
  ok "healthz $HEALTH"
else
  systemctl --no-pager -l status quick-drain 2>&1 | tail -n 12 | sed 's/^/  /'
  journalctl -u quick-drain -n 20 --no-pager 2>/dev/null | sed 's/^/  /'
  die "healthz did not answer on 127.0.0.1:${PORT}. Rollback:  git checkout ${PREV_COMMIT:-<previous>} && ./deploy.sh"
fi
PUB="$(curl -sI --max-time 8 -H "Host: ${SERVER_NAME}" "http://127.0.0.1:${PUBLIC_PORT}/" | head -n1 | tr -d '\r' || true)"
case "$PUB" in
  *" 200"*|*" 301"*|*" 302"*) ok "public port ${PUBLIC_PORT}: $PUB" ;;
  "") warn "nothing answered on 127.0.0.1:${PUBLIC_PORT} — nginx listen? (nginx -T | grep -n ${PUBLIC_PORT})" ;;
  *) warn "public port ${PUBLIC_PORT}: $PUB" ;;
esac

ADMINS="$(.venv/bin/python -c 'from app.db import connect; c = connect(); print(c.execute("SELECT COUNT(*) FROM admin_users WHERE is_active = 1").fetchone()[0]); c.close()' 2>/dev/null || echo "?")"
fix_db_ownership "$DB_PATH"

printf '\n%s\n' "${_B}Done.${_N}"
say "site:      $BASE_URL"
say "logs:      journalctl -u quick-drain -f"
say "rollback:  git checkout ${PREV_COMMIT:-<previous-hash>} && ./deploy.sh"
if [ "$ADMINS" = "0" ]; then
  say "first admin (2FA enrols on first sign-in at $BASE_URL/admin/login):"
  say "           .venv/bin/python scripts/create_admin.py <username> <email>"
else
  say "admins:    $ADMINS active (create/reset: .venv/bin/python scripts/create_admin.py <username> <email>)"
fi
say "check:     ./final_check.sh"
