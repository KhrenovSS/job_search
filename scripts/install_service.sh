#!/usr/bin/env bash
# Installs hh-scout as a systemd service on the host. Requires sudo for the unit file.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  virtualenv -q .venv
fi
.venv/bin/pip install --quiet -r requirements.txt -e .
[ -f .env ] || { cp .env.example .env; echo "Создан .env из шаблона — заполните TG_BOT_TOKEN, TG_OWNER_CHAT_ID, BRIDGE_TOKEN"; }
mkdir -p data
command -v geckodriver >/dev/null || bash scripts/install_geckodriver.sh

# render the unit template for this user and checkout location
REPO="$(pwd)"; USER_NAME="$(id -un)"
sed -e "s|@USER@|${USER_NAME}|g" -e "s|@REPO@|${REPO}|g" -e "s|@HOME@|${HOME}|g" hh-scout.service.template > /tmp/hh-scout.service
sudo install -m 644 /tmp/hh-scout.service /etc/systemd/system/hh-scout.service && rm -f /tmp/hh-scout.service
sudo systemctl daemon-reload
sudo systemctl enable --now hh-scout
sleep 2
systemctl --no-pager --lines=8 status hh-scout || true
