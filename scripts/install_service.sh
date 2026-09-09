#!/usr/bin/env bash
# Installs hh-scout as a systemd service on the host. Requires sudo for the unit files
# (passwordless after scripts/grant_agent_control.sh). Does not restart a running service — use scripts/svc.sh restart|reinstall.
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
for unit in hh-scout hh-scout-alert; do
  # rendered copies stay in data/ (gitignored); these exact paths are what scripts/sudoers-hh-scout.template allows
  sed -e "s|@USER@|${USER_NAME}|g" -e "s|@REPO@|${REPO}|g" -e "s|@HOME@|${HOME}|g" "${unit}.service.template" > "${REPO}/data/${unit}.service"
  sudo install -m 644 "${REPO}/data/${unit}.service" "/etc/systemd/system/${unit}.service"
done
sudo systemctl daemon-reload
sudo systemctl enable --now hh-scout
sleep 2
systemctl --no-pager --lines=8 status hh-scout || true
