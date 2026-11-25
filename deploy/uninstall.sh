#!/usr/bin/env bash
# Quick Drain Products — remove the storefront from this server.
#
#   curl -fsSL https://raw.githubusercontent.com/jackpaypal6969-maker/quickdrainproducts/main/deploy/uninstall.sh | sudo bash
#   (or, from the checkout:  sudo ./deploy/uninstall.sh)
#
# Removes only what deploy.sh / bootstrap.sh created, by name:
#   /opt/quick-drain (the app, its database, uploads, .env)
#   /etc/systemd/system/quick-drain.service (+ .d drop-ins)
#   /etc/nginx/sites-{available,enabled}/zz-quick-drain      (nginx is reloaded, not restarted)
#   /etc/cron.d/quick-drain and /opt/quick-drain-cron.log
#   /etc/fail2ban/filter.d/quick-drain.conf, /etc/fail2ban/jail.d/quick-drain.local
#   the ufw rule for PUBLIC_PORT (default 8086) — no other port is touched
#   /var/backups/quick-drain and the quickdrain system user
# Nothing else on the server is read, stopped or changed. Packages (nginx,
# fail2ban, python3) stay installed because other sites may use them.
#
# Before deleting, one archive of .env + database + uploads is written to
# /root/quick-drain-final-<date>.tar.gz so an accidental run is recoverable.
# Delete that file too if you want nothing left:  rm /root/quick-drain-final-*.tar.gz
set -uo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

APP_DIR="${APP_DIR:-/opt/quick-drain}"
SERVICE_USER=quickdrain
PUBLIC_PORT=8086
PORT=8006
BACKUP_DIR=/var/backups/quick-drain
DB_PATH="$APP_DIR/data/quick-drain.db"
MEDIA_DIR="$APP_DIR/media"
if [ -f "$APP_DIR/.env" ]; then
  # read only the keys we need; never source a .env (it may contain arbitrary shell)
  getenv() { sed -n "s/^$1=//p" "$APP_DIR/.env" | tail -n 1 | tr -d '"'"'" | tr -d '[:space:]'; }
  v="$(getenv SERVICE_USER)"; [ -n "$v" ] && SERVICE_USER="$v"
  v="$(getenv PUBLIC_PORT)";  [ -n "$v" ] && PUBLIC_PORT="$v"
  v="$(getenv PORT)";         [ -n "$v" ] && PORT="$v"
  v="$(getenv BACKUP_DIR)";   [ -n "$v" ] && BACKUP_DIR="$v"
  v="$(getenv DB_PATH)";      [ -n "$v" ] && DB_PATH="$v"
  v="$(getenv MEDIA_DIR)";    [ -n "$v" ] && MEDIA_DIR="$v"
fi
case "$SERVICE_USER" in root|""|*[!a-z0-9_-]*) SERVICE_USER=quickdrain ;; esac
case "$BACKUP_DIR" in /var/backups/quick-drain|/var/backups/quick-drain/*|"$APP_DIR"/*) ;; *) BACKUP_DIR="" ;; esac  # never rm a dir we did not create

say() { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

step "Final archive (safety net)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="/root/quick-drain-final-${STAMP}.tar.gz"
if [ -d "$APP_DIR" ]; then
  systemctl stop quick-drain 2>/dev/null || true   # quiesce the database first
  tmp="$(mktemp -d)"
  [ -f "$APP_DIR/.env" ] && cp "$APP_DIR/.env" "$tmp/env"
  if [ -f "$DB_PATH" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$DB_PATH" ".backup '$tmp/quick-drain.db'" 2>/dev/null || cp "$DB_PATH" "$tmp/quick-drain.db"
    else
      cp "$DB_PATH" "$tmp/quick-drain.db"
    fi
  fi
  for d in uploads sds; do [ -d "$MEDIA_DIR/$d" ] && cp -a "$MEDIA_DIR/$d" "$tmp/$d"; done
  if [ -n "$(ls -A "$tmp" 2>/dev/null)" ]; then
    tar -C "$tmp" -czf "$ARCHIVE" . && chmod 600 "$ARCHIVE" && say "saved $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
  else
    say "nothing to archive"
  fi
  rm -rf "$tmp"
else
  say "$APP_DIR not found; removing any leftover system files"
fi

step "Service"
if [ -f /etc/systemd/system/quick-drain.service ] || systemctl list-unit-files 2>/dev/null | grep -q '^quick-drain\.service'; then
  systemctl disable --now quick-drain 2>/dev/null || true
  rm -f /etc/systemd/system/quick-drain.service
  rm -rf /etc/systemd/system/quick-drain.service.d
  systemctl daemon-reload
  systemctl reset-failed quick-drain 2>/dev/null || true
  say "quick-drain.service removed"
else
  say "no service installed"
fi

step "nginx"
if [ -e /etc/nginx/sites-enabled/zz-quick-drain ] || [ -e /etc/nginx/sites-available/zz-quick-drain ]; then
  rm -f /etc/nginx/sites-enabled/zz-quick-drain /etc/nginx/sites-available/zz-quick-drain
  if systemctl is-active -q nginx 2>/dev/null; then
    if nginx -t >/dev/null 2>&1; then systemctl reload nginx && say "site removed, nginx reloaded (other sites untouched)"
    else say "site removed, but nginx -t fails on the remaining config; nginx NOT reloaded. Run: nginx -t"; fi
  else
    say "site removed (nginx not running)"
  fi
else
  say "no nginx site installed"
fi

step "cron"
rm -f /etc/cron.d/quick-drain "$(readlink -f -- "$APP_DIR/..")/quick-drain-cron.log" 2>/dev/null
say "cron entries and log removed"

step "fail2ban"
if [ -e /etc/fail2ban/jail.d/quick-drain.local ] || [ -e /etc/fail2ban/filter.d/quick-drain.conf ]; then
  rm -f /etc/fail2ban/jail.d/quick-drain.local /etc/fail2ban/filter.d/quick-drain.conf
  if systemctl is-active -q fail2ban 2>/dev/null; then fail2ban-client reload >/dev/null 2>&1 && say "jail removed, fail2ban reloaded" || say "jail files removed; fail2ban reload failed (check: fail2ban-client -d | tail)"; else say "jail files removed"; fi
else
  say "no jail installed"
fi

step "Firewall"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  if ufw status | grep -qE "^${PUBLIC_PORT}/tcp +ALLOW"; then
    ufw delete allow "${PUBLIC_PORT}/tcp" >/dev/null && say "ufw rule for ${PUBLIC_PORT}/tcp removed (no other rule touched)"
  else
    say "no ufw rule for ${PUBLIC_PORT}/tcp"
  fi
else
  say "ufw inactive or absent; nothing to do"
fi

step "Files"
[ -d "$APP_DIR" ] && rm -rf "$APP_DIR" && say "removed $APP_DIR"
[ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ] && rm -rf "$BACKUP_DIR" && say "removed $BACKUP_DIR"
true

step "Service user"
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
  pkill -u "$SERVICE_USER" 2>/dev/null || true
  userdel "$SERVICE_USER" 2>/dev/null && say "removed user $SERVICE_USER" || say "could not remove user $SERVICE_USER (userdel $SERVICE_USER)"
else
  say "no user $SERVICE_USER"
fi

step "Check"
left=0
for f in /etc/systemd/system/quick-drain.service /etc/nginx/sites-enabled/zz-quick-drain /etc/cron.d/quick-drain /etc/fail2ban/jail.d/quick-drain.local "$APP_DIR"; do
  [ -e "$f" ] && { say "STILL PRESENT: $f"; left=1; }
done
for p in "$PORT" "$PUBLIC_PORT"; do
  if ss -ltn 2>/dev/null | grep -qE ":${p}\b"; then say "port $p is still listening (another program owns it; not touched)"; fi
done
if ls /etc/letsencrypt/live/ >/dev/null 2>&1 && [ -n "$(ls -A /etc/letsencrypt/live 2>/dev/null)" ]; then
  say "TLS certificates in /etc/letsencrypt were left alone. If one was issued for this store: certbot delete --cert-name <its-domain>"
fi
[ "$left" -eq 0 ] && say "Quick Drain Products is gone from this server." || say "some items remain; see above."
[ -f "$ARCHIVE" ] && say "Final copy kept at $ARCHIVE (delete with: rm $ARCHIVE)"
exit 0
