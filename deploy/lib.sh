# Quick Drain Products — shared helpers for deploy.sh, backup.sh, handoff.sh,
# final_check.sh. Sourced, not executed. Bash 4+.
#
# Output is short on purpose: it is read on a phone.

if [ -t 1 ]; then
  _B=$'\033[1m'; _G=$'\033[32m'; _Y=$'\033[33m'; _R=$'\033[31m'; _N=$'\033[0m'
else
  _B=""; _G=""; _Y=""; _R=""; _N=""
fi

say()  { printf '%s\n' "  $*"; }
ok()   { printf '%s\n' "  ${_G}OK${_N}   $*"; }
warn() { printf '%s\n' "  ${_Y}WARN${_N} $*"; }
step() { printf '\n%s\n' "${_B}==> $*${_N}"; }
die()  { printf '\n%s\n\n' "  ${_R}STOP${_N} $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# load_env FILE — export KEY=VALUE lines. Handles values with spaces and
# parentheses (APP_NAME, PHONE_DISPLAY), strips matching quotes, ignores
# comments and blanks. Never `source`s the file: "(631) 888-6200" would be
# parsed as an array by bash.
load_env() {
  local file="$1" line key val
  [ -f "$file" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ ${#val} -ge 2 && "$val" == \"*\" && "$val" == *\" ]]; then val="${val:1:${#val}-2}"
    elif [[ ${#val} -ge 2 && "$val" == \'*\' && "$val" == *\' ]]; then val="${val:1:${#val}-2}"
    fi
    export "$key=$val"
  done < "$file"
}

# require_vars NAME... — die listing every missing/empty variable at once.
require_vars() {
  local missing="" v
  for v in "$@"; do
    [ -n "${!v:-}" ] || missing="$missing $v"
  done
  [ -z "$missing" ] || die "Missing in .env:$missing"
}

# is_under CHILD PARENT — true when CHILD resolves to PARENT or inside it.
is_under() {
  local child parent
  child="$(readlink -f -- "$1" 2>/dev/null || printf '%s' "$1")"
  parent="$(readlink -f -- "$2" 2>/dev/null || printf '%s' "$2")"
  [ "$child" = "$parent" ] || [[ "$child" == "$parent"/* ]]
}

# db_backup SRC DEST — consistent online copy of a live WAL-mode SQLite file.
# Uses the sqlite3 CLI (.backup) when present, else the venv's sqlite3 module
# (same backup API). Verifies the copy with PRAGMA integrity_check.
db_backup() {
  local src="$1" dest="$2" py="${APP_DIR:-.}/.venv/bin/python"
  [ -f "$src" ] || return 1
  rm -f -- "$dest"
  if have sqlite3; then
    sqlite3 "$src" ".backup '$dest'" || return 1
    [ "$(sqlite3 "$dest" 'PRAGMA integrity_check;')" = "ok" ] || return 1
  else
    [ -x "$py" ] || py="$(command -v python3 || true)"
    [ -n "$py" ] || return 1
    "$py" - "$src" "$dest" <<'PY' || return 1
import sqlite3, sys
src, dest = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src, timeout=30)
d = sqlite3.connect(dest)
with d:
    s.backup(d)
row = d.execute("PRAGMA integrity_check").fetchone()
s.close(); d.close()
sys.exit(0 if row and row[0] == "ok" else 1)
PY
  fi
  fix_db_ownership "$src"
}

# fix_db_ownership DB — a root-run script that opens a WAL database can leave
# DB-wal / DB-shm owned by root, which locks the service user out. Give the
# sidecar files back to whoever owns the database. No-op when not root.
fix_db_ownership() {
  local db="$1" owner f
  [ "$(id -u)" = "0" ] || return 0
  [ -f "$db" ] || return 0
  owner="$(stat -c '%U:%G' -- "$db" 2>/dev/null || true)"
  [ -n "$owner" ] || return 0
  for f in "$db-wal" "$db-shm"; do
    [ -e "$f" ] && chown "$owner" -- "$f" 2>/dev/null || true
  done
  return 0
}

human_size() {
  if [ -e "$1" ]; then du -sh -- "$1" 2>/dev/null | cut -f1; else printf '0'; fi
}
