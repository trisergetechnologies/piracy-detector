#!/usr/bin/env bash
# Install / update Piracy Detector on a Hostinger (or any Ubuntu) VPS
# alongside existing Node/Mongo/Next apps.
#
# Usage (on the VPS as root or sudo):
#   export DOMAIN=piracy.yourdomain.com
#   export REPO_URL=https://github.com/trisergetechnologies/piracy-detector.git
#   bash deploy/setup.sh
#
# Ports chosen to avoid clashing with a typical Next app on :3000:
#   API  127.0.0.1:5001
#   UI   127.0.0.1:3001

set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/piracy-detector}"
REPO_URL="${REPO_URL:-https://github.com/trisergetechnologies/piracy-detector.git}"
BRANCH="${BRANCH:-main}"
DOMAIN="${DOMAIN:-piracy.YOURDOMAIN.com}"
APP_USER="${APP_USER:-www-data}"
API_PORT="${API_PORT:-5001}"
UI_PORT="${UI_PORT:-3001}"

echo "==> App dir: $APP_DIR"
echo "==> Domain : $DOMAIN"
echo "==> Ports  : API=$API_PORT UI=$UI_PORT"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
  git curl build-essential \
  python3 python3-venv python3-pip \
  ffmpeg nginx

# Node 20 if missing / too old
if ! command -v node >/dev/null 2>&1 || [[ "$(node -v | sed 's/v//;s/\..*//')" -lt 18 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi

# Mongo: reuse existing mongod if present
if ! command -v mongod >/dev/null 2>&1 && ! systemctl is-active --quiet mongod 2>/dev/null; then
  echo "WARNING: MongoDB not detected. Install it or set MONGODB_URI in .env to Atlas/remote."
fi

mkdir -p "$(dirname "$APP_DIR")"
if [[ -d "$APP_DIR/.git" ]]; then
  echo "==> Updating existing clone"
  git -C "$APP_DIR" fetch origin
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
else
  echo "==> Cloning repo"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

if [[ ! -f .env ]]; then
  cp .env.example .env
  cat >> .env <<EOF

# Filled by deploy/setup.sh
NEXT_PUBLIC_API_URL=http://127.0.0.1:${API_PORT}
EOF
  echo "==> Created $APP_DIR/.env — EDIT THIS FILE with real API keys before use"
else
  echo "==> Keeping existing .env"
fi

# Patch ports into .env if missing
grep -q '^NEXT_PUBLIC_API_URL=' .env || echo "NEXT_PUBLIC_API_URL=http://127.0.0.1:${API_PORT}" >> .env

echo "==> Python venv + deps"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "==> Dashboard build"
cd dashboard
npm ci
NEXT_PUBLIC_API_URL="http://127.0.0.1:${API_PORT}" npm run build
cd ..

echo "==> Permissions"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Systemd units"
# Rewrite ports in unit files if customized
sed "s/5001/${API_PORT}/g" deploy/piracy-api.service > /etc/systemd/system/piracy-api.service
sed -e "s/3001/${UI_PORT}/g" -e "s/5001/${API_PORT}/g" deploy/piracy-ui.service > /etc/systemd/system/piracy-ui.service
# Fix WorkingDirectory paths if APP_DIR differs
sed -i "s|/var/www/piracy-detector|${APP_DIR}|g" /etc/systemd/system/piracy-api.service
sed -i "s|/var/www/piracy-detector|${APP_DIR}|g" /etc/systemd/system/piracy-ui.service

systemctl daemon-reload
systemctl enable piracy-api piracy-ui
systemctl restart piracy-api
systemctl restart piracy-ui

echo "==> Nginx site"
NGINX_SITE=/etc/nginx/sites-available/piracy-detector
sed -e "s/piracy.YOURDOMAIN.com/${DOMAIN}/g" \
    -e "s/3001/${UI_PORT}/g" \
    -e "s/5001/${API_PORT}/g" \
    deploy/nginx-piracy.conf > "$NGINX_SITE"
ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/piracy-detector
nginx -t
systemctl reload nginx

echo ""
echo "============================================================"
echo " Deployed."
echo "  API health : curl -s http://127.0.0.1:${API_PORT}/health"
echo "  UI local   : curl -sI http://127.0.0.1:${UI_PORT}"
echo "  Public     : http://${DOMAIN}  (add DNS A record → this VPS)"
echo ""
echo " NEXT STEPS:"
echo "  1. nano ${APP_DIR}/.env   # set YOUTUBE_API_KEYS + OPENAI_API_KEY"
echo "  2. systemctl restart piracy-api"
echo "  3. certbot --nginx -d ${DOMAIN}"
echo "============================================================"
