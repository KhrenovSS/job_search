#!/usr/bin/env bash
# Installs the hh-scout Claude bridge as a systemd service on the host.
# Run from the repo root or bridge/: bash bridge/install.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  if command -v virtualenv >/dev/null; then virtualenv -q .venv; else python3 -m venv .venv; fi
fi
.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env.bridge ]; then
  cp .env.bridge.example .env.bridge
  TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  sed -i "s|^HH_BRIDGE_TOKEN=.*|HH_BRIDGE_TOKEN=${TOKEN}|" .env.bridge
  chmod 600 .env.bridge
  echo "Created .env.bridge with a fresh token. Copy it to BRIDGE_TOKEN in the project's .env:"
  echo "  ${TOKEN}"
fi

command -v claude >/dev/null || { echo "claude CLI not found in PATH"; exit 1; }

# render the unit template for this user and checkout location
BRIDGE_DIR="$(pwd)"; REPO="$(dirname "$BRIDGE_DIR")"; USER_NAME="$(id -un)"
sed -e "s|@USER@|${USER_NAME}|g" -e "s|@REPO@|${REPO}|g" -e "s|@HOME@|${HOME}|g" hh-scout-bridge.service.template > /tmp/hh-scout-bridge.service
sudo install -m 644 /tmp/hh-scout-bridge.service /etc/systemd/system/hh-scout-bridge.service && rm -f /tmp/hh-scout-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now hh-scout-bridge
sleep 1
systemctl --no-pager --lines=5 status hh-scout-bridge || true
PORT=$(grep -E '^BRIDGE_PORT=' .env.bridge | cut -d= -f2)
curl -fsS "http://127.0.0.1:${PORT:-8766}/health" && echo
