#!/usr/bin/env bash
# One-time (and idempotent) server setup. Run ON THE SERVER from the app dir:
#   sudo bash deploy/install-service.sh
# Installs system packages, builds the venv as the invoking user, installs and
# (re)starts the systemd service. Works on Ubuntu and Raspberry Pi OS.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo: sudo bash $0" >&2
  exit 1
fi

APP_DIR=$(cd "$(dirname "$0")/.." && pwd)
APP_USER=${SUDO_USER:-$(stat -c %U "$APP_DIR")}
echo "==> app dir $APP_DIR, service user $APP_USER"

echo "==> installing system packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv libportaudio2 alsa-utils

echo "==> building venv"
sudo -u "$APP_USER" bash -c "
  set -e
  cd '$APP_DIR'
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e '.[rf]'
"

echo "==> installing systemd unit"
sed -e "s|@USER@|$APP_USER|g" -e "s|@DIR@|$APP_DIR|g" \
  "$APP_DIR/deploy/tf-daemon.service" > /etc/systemd/system/tf-daemon.service
systemctl daemon-reload
systemctl enable tf-daemon
systemctl restart tf-daemon
sleep 3
systemctl --no-pager --lines=15 status tf-daemon || true
