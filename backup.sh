#!/usr/bin/env bash
#
# Quick Drain Products — nightly backup.
#
#   ./backup.sh            checkpointed DB + media + .env -> BACKUP_DIR/quick-drain-<stamp>.tgz
#
# Keeps the newest 14 local archives. If BACKUP_REMOTE is set in .env it also
# copies the new archive off-box: rclone when BACKUP_REMOTE names a configured
# rclone remote (remote:path), otherwise scp (user@host:/path). Never writes
# under static/. Exits non-zero on any failure so cron/journal shows it.
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
. "$APP_DIR/deploy/lib.sh"

load_env "$APP_DIR/.env" || die "No .env in $APP_DIR"
export APP_DIR="${APP_DIR}"
require_vars DB_PATH MEDIA_DIR BACKUP_DIR

STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$BACKUP_DIR/quick-drain-$STAMP.tgz"
KEEP=14

step "Backup $STAMP"
is_under "$BACKUP_DIR" "$APP_DIR/static" && die "BACKUP_DIR ($BACKUP_DIR) is under static/ — backups would be served to the internet."
[ -f "$DB_PATH" ] || die "Database not found at DB_PATH=$DB_PATH"
mkdir -p "$BACKUP_DIR"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1) consistent copy of the live database (WAL-safe), integrity-checked
db_backup "$DB_PATH" "$WORK/quick-drain.db" || die "sqlite backup or integrity_check failed for $DB_PATH"
ok "database copy $(human_size "$WORK/quick-drain.db") (integrity ok)"

# 2) media + .env + db copy -> one archive
TAR_ARGS=(-C "$WORK" quick-drain.db)
if [ -d "$MEDIA_DIR" ]; then
  TAR_ARGS+=(-C "$(dirname "$MEDIA_DIR")" "$(basename "$MEDIA_DIR")")
  ok "media $(human_size "$MEDIA_DIR")"
else
  warn "MEDIA_DIR $MEDIA_DIR does not exist; skipping media"
fi
if [ -f "$APP_DIR/.env" ]; then
  TAR_ARGS+=(-C "$APP_DIR" .env)
fi
TMP_ARCHIVE="$BACKUP_DIR/.quick-drain-$STAMP.partial"
tar -czf "$TMP_ARCHIVE" "${TAR_ARGS[@]}"
mv -f "$TMP_ARCHIVE" "$ARCHIVE"
chmod 600 "$ARCHIVE"
ok "archive $ARCHIVE ($(human_size "$ARCHIVE"))"

# 3) retention: newest $KEEP stay
mapfile -t OLD < <(ls -1t "$BACKUP_DIR"/quick-drain-[0-9]*.tgz 2>/dev/null | tail -n +"$((KEEP + 1))")
if [ "${#OLD[@]}" -gt 0 ]; then
  rm -f -- "${OLD[@]}"
  ok "pruned ${#OLD[@]} old archive(s); $KEEP kept"
fi
say "local archives: $(ls -1 "$BACKUP_DIR"/quick-drain-[0-9]*.tgz 2>/dev/null | wc -l), dir $(human_size "$BACKUP_DIR")"

# 4) off-box copy
if [ -n "${BACKUP_REMOTE:-}" ]; then
  REMOTE_NAME="${BACKUP_REMOTE%%:*}"
  if [[ "$BACKUP_REMOTE" == *:* ]] && have rclone && rclone listremotes 2>/dev/null | grep -qx "$REMOTE_NAME:"; then
    rclone copy --quiet "$ARCHIVE" "$BACKUP_REMOTE" || die "rclone copy to $BACKUP_REMOTE failed"
    ok "copied to rclone remote $BACKUP_REMOTE"
  else
    scp -q -o BatchMode=yes -o ConnectTimeout=20 "$ARCHIVE" "$BACKUP_REMOTE" || die "scp to $BACKUP_REMOTE failed (needs a key in root's ~/.ssh, BatchMode)"
    ok "copied via scp to $BACKUP_REMOTE"
  fi
else
  warn "BACKUP_REMOTE empty — local copies only. A dead disk takes the backups with it."
fi

ok "backup done"
