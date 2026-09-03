#!/usr/bin/env bash
# Mobile QA gate: audits the money pages at 390px with a real Chromium and keeps
# the screenshots under docs/qa/. Exit 1 on any CRITICAL finding (horizontal
# scroll, missing viewport meta). Requires node + the dev devDependencies
# (npm install) and a running server (default http://127.0.0.1:8006).
#
#   ./scripts/mobile_qa.sh [base_url]
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${1:-http://127.0.0.1:8006}"
OUT="$APP_DIR/qa-report"
SKILL="$(ls -d ~/.claude/skills/synced/*/mobile-qa ~/.claude/skills/mobile-qa "$APP_DIR"/.claude/skills/mobile-qa 2>/dev/null | head -1 || true)"
AUDIT="${MOBILE_QA_AUDIT:-$SKILL/scripts/audit.js}"
[ -f "$AUDIT" ] || { echo "audit.js not found; set MOBILE_QA_AUDIT=/path/to/audit.js" >&2; exit 2; }
mkdir -p "$OUT" "$APP_DIR/docs/qa"
fail=0
for page in "/" "/products/quick-shot" "/cart" "/checkout/success?session_id=demo"; do
  name="$(echo "$page" | sed -E 's#[/?=]+#-#g; s#^-##; s#-$##')"; name="${name:-home}"
  case "$page" in /checkout/success*) name="checkout-pending" ;; esac
  echo "== $page"
  if NODE_PATH="$APP_DIR/node_modules" node "$AUDIT" "$BASE$page" --out "$OUT/$name" | grep -E "CRITICAL|SERIOUS|MODERATE|HTTP" ; then :; fi
  if grep -q "CRITICAL" "$OUT/$name/report.md"; then fail=1; fi
done
# The audit's full-page capture does not scroll, so reveal sections look blank in
# its PNGs; the docs/qa set is captured with a scroll-through pass instead.
NODE_PATH="$APP_DIR/node_modules" node "$APP_DIR/scripts/qa_screens.js" "$BASE" "$APP_DIR/docs/qa"
echo
[ "$fail" = "0" ] && echo "MOBILE QA: no critical findings" || { echo "MOBILE QA: CRITICAL findings — see $OUT"; exit 1; }
