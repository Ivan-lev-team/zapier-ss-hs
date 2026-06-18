#!/usr/bin/env bash
# Deploy SmartScout scraper to /opt/zapier-ss-hs on Ubuntu.
# Run as root: sudo bash deploy/setup.sh
set -euo pipefail

DEPLOY_DIR="/opt/zapier-ss-hs"
SERVICE_NAME="smartscout-scraper"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing system dependencies"
apt-get update -qq
apt-get install -y python3-pip python3-venv

echo "==> Copying project to $DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR"
rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' \
    "$REPO_DIR/" "$DEPLOY_DIR/"

echo "==> Creating virtualenv and installing Python dependencies"
python3 -m venv "$DEPLOY_DIR/venv"
"$DEPLOY_DIR/venv/bin/pip" install --upgrade pip -q
"$DEPLOY_DIR/venv/bin/pip" install -r "$DEPLOY_DIR/requirements.txt" -q

echo "==> Installing Playwright Chromium browser"
"$DEPLOY_DIR/venv/bin/playwright" install chromium --with-deps

echo "==> Copying .env (if not already present)"
if [ ! -f "$DEPLOY_DIR/.env" ]; then
    cp "$DEPLOY_DIR/.env.example" "$DEPLOY_DIR/.env"
    echo "    *** Edit $DEPLOY_DIR/.env with real credentials before starting ***"
fi

echo "==> Installing systemd service"
cp "$DEPLOY_DIR/deploy/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo ""
echo "==> Done. Service status:"
systemctl status "$SERVICE_NAME" --no-pager
echo ""
echo "Tail logs with: journalctl -u $SERVICE_NAME -f"
