#!/usr/bin/env bash
#
# Quick Drain Products — customer handoff bundle.
#
#   ./handoff.sh    -> BACKUP_DIR/quick-drain-handoff-<stamp>.tgz
#
# One archive containing the app checkout (without .venv, node_modules, .tools,
# __pycache__), a checkpointed copy of the database, the media directory and
# the current .env, plus the exact restore sequence for the customer's VPS.
# The .env inside carries the DEVELOPER's secrets: the customer must rotate
# every one of them (see HANDOFF.md, "Secret rotation checklist").
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
. "$APP_DIR/deploy/lib.sh"

load_env "$APP_DIR/.env" || die "No .env in $APP_DIR"
export APP_DIR="${APP_DIR}"
require_vars DB_PATH MEDIA_DIR BACKUP_DIR

STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="quick-drain-handoff-$STAMP"
ARCHIVE="$BACKUP_DIR/$NAME.tgz"

step "Handoff bundle $STAMP"
is_under "$BACKUP_DIR" "$APP_DIR/static" && die "BACKUP_DIR ($BACKUP_DIR) is under static/ — refusing to write a bundle that nginx would serve."
[ -f "$DB_PATH" ] || die "Database not found at DB_PATH=$DB_PATH"
mkdir -p "$BACKUP_DIR"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/$NAME"

# 1) app checkout — source only; deploy.sh rebuilds .venv, CSS and images
tar -C "$(dirname "$APP_DIR")" -cf "$WORK/$NAME/app.tar" \
  --exclude='.venv' --exclude='node_modules' --exclude='.tools' --exclude='__pycache__' \
  --exclude='*.pyc' --exclude='.pytest_cache' --exclude='.env' \
  --exclude='data' --exclude='backups' --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='qa-report*' --exclude='*.tgz' \
  --transform "s|^$(basename "$APP_DIR")|app|" \
  "$(basename "$APP_DIR")"
ok "app checkout $(human_size "$WORK/$NAME/app.tar") (commit $(git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown))"

# 2) checkpointed database
db_backup "$DB_PATH" "$WORK/$NAME/quick-drain.db" || die "sqlite backup or integrity_check failed for $DB_PATH"
ok "database $(human_size "$WORK/$NAME/quick-drain.db") (integrity ok)"

# 3) media + .env
if [ -d "$MEDIA_DIR" ]; then
  tar -C "$(dirname "$MEDIA_DIR")" -cf "$WORK/$NAME/media.tar" --transform "s|^$(basename "$MEDIA_DIR")|media|" "$(basename "$MEDIA_DIR")"
  ok "media $(human_size "$MEDIA_DIR")"
else
  warn "MEDIA_DIR $MEDIA_DIR missing; bundle has no media"
fi
cp -p "$APP_DIR/.env" "$WORK/$NAME/env.developer"
ok ".env captured as env.developer (rotate everything in it)"

cat > "$WORK/$NAME/RESTORE.txt" <<EOF
Quick Drain Products — restore on the customer's VPS (Ubuntu 22.04/24.04, as root)

  1.  mkdir -p /opt && cd /opt
  2.  tar -xzf /path/to/$NAME.tgz
  3.  cd $NAME && tar -xf app.tar && mv app /opt/quick-drain-products
  4.  cd /opt/quick-drain-products
  5.  cp /opt/$NAME/env.developer .env        # then EDIT it:
        APP_DIR=/opt/quick-drain-products
        DB_PATH=/opt/quick-drain-products/data/quick-drain.db
        MEDIA_DIR=/opt/quick-drain-products/media
        BACKUP_DIR=/opt/quick-drain-backups
        BASE_URL / SERVER_NAME / PUBLIC_PORT for the new box
        SECRET_KEY, Stripe, Resend, PostHog, ADMIN_BASIC_AUTH_* -> NEW values (HANDOFF.md checklist)
  6.  mkdir -p data && cp /opt/$NAME/quick-drain.db data/quick-drain.db
  7.  tar -xf /opt/$NAME/media.tar -C /opt/quick-drain-products    # restores media/
  8.  ./deploy.sh
  9.  .venv/bin/python scripts/create_admin.py <username> <email>   # fresh admin, 2FA enrols on first login
 10.  ./final_check.sh
 11.  shred -u /opt/$NAME/env.developer && rm -rf /opt/$NAME
EOF

tar -C "$WORK" -czf "$ARCHIVE" "$NAME"
chmod 600 "$ARCHIVE"
ok "bundle $ARCHIVE ($(human_size "$ARCHIVE"))"

step "Restore sequence (also inside the bundle as RESTORE.txt)"
sed 's/^/  /' "$WORK/$NAME/RESTORE.txt"
printf '\n'
warn "The bundle contains the developer .env. Send it over an encrypted channel and destroy it after the customer has rotated secrets."
