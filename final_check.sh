#!/usr/bin/env bash
#
# Quick Drain Products — launch / weekly health check.
#
#   ./final_check.sh        PASS / FAIL / WARN per line, exit 1 on any FAIL
#
# Counts real rows, probes the running service, and checks the security
# invariants in .env. A query that errors is a FAIL, never silence: the
# summary line is only "ALL CLEAR" when every check ran and none failed.
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/lib.sh
. "$APP_DIR/deploy/lib.sh"

load_env "$APP_DIR/.env" || die "No .env in $APP_DIR"
export APP_DIR="${APP_DIR}"
require_vars DB_PATH BACKUP_DIR PORT

PY="$APP_DIR/.venv/bin/python"
[ -x "$PY" ] || die "No virtualenv at $APP_DIR/.venv — run ./deploy.sh first"

# Probes from bash (curl), results handed to the python block below.
probe() {  # probe METHOD URL -> http code or 000
  curl -s -o /dev/null -w '%{http_code}' -X "$1" --max-time 8 "$2" 2>/dev/null || printf '000'
}
export QD_WEBHOOK_CODE="$(probe POST "http://127.0.0.1:${PORT}/webhooks/stripe")"
export QD_DOCS_CODE="$(probe GET "http://127.0.0.1:${PORT}/docs")"
export QD_HEALTH_CODE="$(probe GET "http://127.0.0.1:${PORT}/healthz")"
export QD_LATEST_BACKUP="$(ls -1t "$BACKUP_DIR"/quick-drain-[0-9]*.tgz 2>/dev/null | head -n1 || true)"

step "final_check $(date '+%Y-%m-%d %H:%M')  db=$DB_PATH"

set +e
"$PY" - <<'PY'
import os, sqlite3, sys, time

fails = warns = passes = 0

def line(status, msg):
    global fails, warns, passes
    if status == "FAIL": fails += 1
    elif status == "WARN": warns += 1
    else: passes += 1
    print(f"  {status:<4} {msg}")

def env(name, default=""):
    return os.environ.get(name, default).strip()

def flag(name, default="off"):
    return env(name, default).lower() in {"1", "on", "true", "yes"}

def check(name, fn):
    """Run one check; any exception becomes a loud FAIL naming the check."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        line("FAIL", f"{name}: {type(exc).__name__}: {exc}")

db_path = env("DB_PATH")
conn = None
try:
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
except Exception as exc:  # noqa: BLE001
    line("FAIL", f"database open {db_path}: {type(exc).__name__}: {exc}")

def q1(sql, params=()):
    if conn is None:
        raise RuntimeError("no database connection")
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None

# --- counts ---------------------------------------------------------------
def counts():
    n = {}
    for label, table in (("products", "products"), ("variants", "variants"), ("orders", "orders"), ("customers", "customers"), ("admins", "admin_users")):
        n[label] = q1(f"SELECT COUNT(*) FROM {table}")
    line("PASS", "rows: " + ", ".join(f"{k}={v}" for k, v in n.items()))
    if n["products"] == 0:
        line("WARN", "products=0 — catalog is empty (deploy.sh seeds when the table is empty)")
    if n["admins"] == 0:
        line("WARN", "admins=0 — create one: .venv/bin/python scripts/create_admin.py <username> <email>")
check("row counts", counts)

# --- service probes -------------------------------------------------------
def health():
    code = env("QD_HEALTH_CODE")
    if code == "200":
        line("PASS", f"GET /healthz -> {code}")
    else:
        line("FAIL", f"GET /healthz -> {code} (service not answering on 127.0.0.1:{env('PORT')})")
check("healthz", health)

def webhook_route():
    code = env("QD_WEBHOOK_CODE")
    if code in {"400", "503"}:
        line("PASS", f"POST /webhooks/stripe -> {code} (route mounted; {'signature rejected' if code == '400' else 'STRIPE_WEBHOOK_SECRET not set'})")
    elif code in {"404", "405"}:
        line("FAIL", f"POST /webhooks/stripe -> {code}: webhook route missing")
    else:
        line("FAIL", f"POST /webhooks/stripe -> {code}: expected 400 or 503")
check("webhook route", webhook_route)

def docs_hidden():
    code = env("QD_DOCS_CODE")
    if env("ENV").lower() == "production":
        if code == "404":
            line("PASS", f"GET /docs -> 404 with ENV=production")
        else:
            line("FAIL", f"GET /docs -> {code} with ENV=production (expected 404)")
    else:
        line("WARN", f"ENV={env('ENV') or '(unset)'} — not production; /docs -> {code}")
check("docs hidden", docs_hidden)

# --- admin 2FA ------------------------------------------------------------
def admin_2fa():
    if not flag("ADMIN_2FA_REQUIRED", "on"):
        line("FAIL", "ADMIN_2FA_REQUIRED is off")
        return
    if conn is None:
        raise RuntimeError("no database connection")
    rows = conn.execute("SELECT username, totp_enabled FROM admin_users WHERE is_active = 1").fetchall()
    missing = [r["username"] for r in rows if not r["totp_enabled"]]
    if missing:
        line("FAIL", "admin 2FA not enrolled for: " + ", ".join(missing))
    elif not rows:
        line("WARN", "ADMIN_2FA_REQUIRED=on but there are no active admins yet")
    else:
        line("PASS", f"ADMIN_2FA_REQUIRED=on; {len(rows)} active admin(s), all TOTP-enrolled")
check("admin 2FA", admin_2fa)

def rate_limits():
    n = q1("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='rate_limits'")
    if n == 1:
        line("PASS", f"rate_limits table exists ({q1('SELECT COUNT(*) FROM rate_limits')} rows)")
    else:
        line("FAIL", "rate_limits table missing")
check("rate_limits table", rate_limits)

# --- .env invariants ------------------------------------------------------
def stripe_key():
    key = env("STRIPE_SECRET_KEY")
    if not key:
        line("FAIL", "STRIPE_SECRET_KEY empty")
    elif key.startswith("sk_test_"):
        line("PASS", "STRIPE_SECRET_KEY is a test key (sk_test_)")
    elif key.startswith("sk_live_"):
        if flag("STRIPE_LIVE_OK"):
            line("WARN", "STRIPE_SECRET_KEY is LIVE and STRIPE_LIVE_OK=on — real money")
        else:
            line("FAIL", "STRIPE_SECRET_KEY is a live key but STRIPE_LIVE_OK is off (app refuses to boot)")
    else:
        line("FAIL", "STRIPE_SECRET_KEY is neither sk_test_ nor sk_live_")
    if not env("STRIPE_WEBHOOK_SECRET"):
        line("WARN", "STRIPE_WEBHOOK_SECRET empty — paid orders will not be recorded")
check("stripe key", stripe_key)

def cookie_secure():
    base = env("BASE_URL")
    secure = flag("COOKIE_SECURE")
    if base.startswith("https://"):
        line("PASS" if secure else "FAIL", f"BASE_URL is https, COOKIE_SECURE={'on' if secure else 'off'}")
    elif base.startswith("http://"):
        if secure:
            line("FAIL", "BASE_URL is http but COOKIE_SECURE=on — browsers will drop the session cookie")
        else:
            line("WARN", f"BASE_URL={base}: plaintext phase, COOKIE_SECURE=off (test keys only, lock /admin)")
    else:
        line("FAIL", f"BASE_URL={base or '(empty)'} is not a URL")
check("cookie secure", cookie_secure)

def backup_dir():
    bdir = os.path.realpath(env("BACKUP_DIR"))
    static = os.path.realpath(os.path.join(env("APP_DIR"), "static"))
    if bdir == static or bdir.startswith(static + os.sep):
        line("FAIL", f"BACKUP_DIR {bdir} is under static/ — backups would be public")
    else:
        line("PASS", f"BACKUP_DIR {bdir} is outside static/")
check("backup dir", backup_dir)

def backup_age():
    latest = env("QD_LATEST_BACKUP")
    if not latest:
        line("WARN", f"no backups yet in {env('BACKUP_DIR')} (run ./backup.sh; cron does it nightly)")
        return
    age_h = (time.time() - os.path.getmtime(latest)) / 3600
    if age_h < 36:
        line("PASS", f"latest backup {os.path.basename(latest)} is {age_h:.1f}h old")
    else:
        line("WARN", f"latest backup {os.path.basename(latest)} is {age_h:.0f}h old (> 36h)")
check("backup age", backup_age)

def secret_key():
    if len(env("SECRET_KEY")) >= 32:
        line("PASS", "SECRET_KEY set (>= 32 chars)")
    else:
        line("FAIL", "SECRET_KEY missing or shorter than 32 chars")
check("secret key", secret_key)

if conn is not None:
    conn.close()

print()
total = passes + warns + fails
if fails:
    print(f"  RESULT: {fails} FAIL, {warns} WARN, {passes} PASS of {total} — NOT CLEAR")
    sys.exit(1)
print(f"  RESULT: 0 FAIL, {warns} WARN, {passes} PASS of {total} — ALL CLEAR" + (" (with warnings)" if warns else ""))
PY
RC=$?
set -e
fix_db_ownership "$DB_PATH"
[ "$RC" -eq 0 ] || { [ "$RC" -eq 1 ] || printf '  FAIL final_check crashed (exit %s) — treat as NOT CLEAR\n' "$RC"; exit 1; }
