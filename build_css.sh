#!/usr/bin/env bash
# Build the Tailwind v4 stylesheet into static/css/app.<hash>.css and write the
# manifest that templates read through asset("css/app.css"). Deterministic,
# idempotent, and run by deploy.sh on every deploy so a 30-day nginx cache can
# never serve a stale sheet: the filename changes whenever the CSS does.
#
#   ./build_css.sh            build once
#   ./build_css.sh --watch    rebuild on change (development)
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
IN="static/css/src/app.css"
OUT_DIR="static/css"
TMP="$OUT_DIR/app.build.css"
TW_VERSION="${TAILWIND_VERSION:-4.3.3}"
mkdir -p "$OUT_DIR" .tools

# 1) standalone binary (no Node needed on the VPS), 2) project node_modules, 3) npx.
resolve_tailwind() {
  if [ -x ".tools/tailwindcss" ]; then echo ".tools/tailwindcss"; return; fi
  if [ -x "node_modules/.bin/tailwindcss" ]; then echo "node_modules/.bin/tailwindcss"; return; fi
  local arch os url
  case "$(uname -m)" in x86_64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) arch="" ;; esac
  case "$(uname -s)" in Linux) os=linux ;; Darwin) os=macos ;; *) os="" ;; esac
  if [ -n "$arch" ] && [ -n "$os" ]; then
    url="https://github.com/tailwindlabs/tailwindcss/releases/download/v${TW_VERSION}/tailwindcss-${os}-${arch}"
    if curl -fsSL --max-time 120 "$url" -o .tools/tailwindcss 2>/dev/null; then chmod +x .tools/tailwindcss; echo ".tools/tailwindcss"; return; fi
  fi
  if command -v npx >/dev/null 2>&1; then echo "npx --yes @tailwindcss/cli@${TW_VERSION}"; return; fi
  echo "ERROR: no Tailwind CLI available (no network for the standalone binary and no node/npx)" >&2
  exit 1
}

TW="$(resolve_tailwind)"
if [ "${1:-}" = "--watch" ]; then
  printf '{"app.css": "app.css"}\n' > "$OUT_DIR/manifest.json"   # asset() serves the live file while watching
  exec $TW -i "$IN" -o "$OUT_DIR/app.css" --watch
fi

$TW -i "$IN" -o "$TMP" --minify
HASH="$(sha256sum "$TMP" | cut -c1-10)"
FINAL="$OUT_DIR/app.$HASH.css"
mv -f "$TMP" "$FINAL"
cp -f "$FINAL" "$OUT_DIR/app.css"   # unhashed copy for local dev + last-resort fallback
printf '{"app.css": "app.%s.css"}\n' "$HASH" > "$OUT_DIR/manifest.json"
# keep only the newest three hashed builds
ls -t "$OUT_DIR"/app.*.css 2>/dev/null | grep -v 'app.build.css' | tail -n +4 | xargs -r rm -f
echo "built $FINAL ($(wc -c < "$FINAL") bytes)"
