#!/usr/bin/env bash
# One-line first install from a phone. Clones (or updates) the repo into
# /opt/quick-drain, writes a working .env on the first run, then hands off to
# deploy.sh. Safe to run again: an existing .env is never overwritten.
#
#   curl -fsSL https://raw.githubusercontent.com/jackpaypal6969-maker/quickdrainproducts/main/deploy/bootstrap.sh | bash -s -- <host-or-ip> [public-port]
#
# Example (raw-port phase):  ... | bash -s -- 2.24.115.98 8086
set -euo pipefail
HOST="${1:-}"
PUBLIC_PORT="${2:-8086}"
APP_DIR="${APP_DIR:-/opt/quick-drain}"
REPO="${REPO:-https://github.com/jackpaypal6969-maker/quickdrainproducts}"
[ -n "$HOST" ] || { echo "usage: bootstrap.sh <host-or-ip> [public-port]" >&2; exit 2; }
[ "$(id -u)" = "0" ] || { echo "run as root (sudo -i first)" >&2; exit 1; }

command -v git >/dev/null 2>&1 || { export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq git >/dev/null; }

if [ -d "$APP_DIR/.git" ]; then
  echo "==> $APP_DIR exists; pulling latest"
  git -C "$APP_DIR" pull --ff-only || echo "WARN pull failed; deploying the checkout as-is"
else
  echo "==> cloning into $APP_DIR"
  git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "==> writing first .env"
  cp .env.example .env
  case "$HOST" in https://*|http://*) BASE_URL="$HOST"; SERVER_NAME="${HOST#*://}"; SERVER_NAME="${SERVER_NAME%%/*}" ;; *) BASE_URL="http://${HOST}:${PUBLIC_PORT}"; SERVER_NAME="$HOST" ;; esac
  sed -i "s#^APP_DIR=.*#APP_DIR=${APP_DIR}#" .env
  sed -i "s#^DB_PATH=.*#DB_PATH=${APP_DIR}/data/quick-drain.db#" .env
  sed -i "s#^MEDIA_DIR=.*#MEDIA_DIR=${APP_DIR}/media#" .env
  sed -i "s#^BACKUP_DIR=.*#BACKUP_DIR=/var/backups/quick-drain#" .env
  sed -i "s#^BASE_URL=.*#BASE_URL=${BASE_URL}#" .env
  sed -i "s#^SERVER_NAME=.*#SERVER_NAME=${SERVER_NAME}#" .env
  sed -i "s#^PUBLIC_PORT=.*#PUBLIC_PORT=${PUBLIC_PORT}#" .env
  sed -i "s#^EMAIL_FROM=.*#EMAIL_FROM=Quick Drain Products <orders@quickdrainny.com>#" .env
  sed -i "s#^EMAIL_DRY_RUN=.*#EMAIL_DRY_RUN=on#" .env
  sed -i "s#^SECRET_KEY=.*#SECRET_KEY=$(openssl rand -hex 32)#" .env
  chmod 640 .env
  echo "    BASE_URL=${BASE_URL}  SERVER_NAME=${SERVER_NAME}  PUBLIC_PORT=${PUBLIC_PORT}"
else
  echo "==> keeping existing .env"
fi

chmod +x deploy.sh build_css.sh backup.sh handoff.sh final_check.sh
exec ./deploy.sh
