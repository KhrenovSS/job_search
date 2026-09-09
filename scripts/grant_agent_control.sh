#!/usr/bin/env bash
# One-time step for the OWNER: install a narrow sudoers rule so that this user (and therefore the AI agent
# running under it) can restart hh-scout / hh-scout-bridge and install the unit files without a password.
# Usage:  bash scripts/grant_agent_control.sh      (asks the sudo password once)
# Validates with visudo and rolls back on error — sudo cannot be left broken.  Revoke: sudo rm /etc/sudoers.d/hh-scout
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"; USER_NAME="$(id -un)"
SRC="scripts/sudoers-hh-scout.template"
RENDERED="${REPO}/data/sudoers-hh-scout"
DST="/etc/sudoers.d/hh-scout"

mkdir -p data
echo "==> Рендерю правило для пользователя ${USER_NAME}, репозиторий ${REPO}"
sed -e "s|@USER@|${USER_NAME}|g" -e "s|@REPO@|${REPO}|g" "$SRC" > "$RENDERED"

echo "==> Синтаксис правила (visudo -cf)"
sudo visudo -cf "$RENDERED"

echo "==> Устанавливаю в ${DST} (root:root, 0440)"
sudo install -m 0440 -o root -g root "$RENDERED" "$DST"

echo "==> Полная проверка sudoers (visudo -c)"
if ! sudo visudo -c >/dev/null; then
  echo "ОШИБКА: sudoers невалиден — откатываю ${DST}"
  sudo rm -f "$DST"
  exit 1
fi

echo "==> Проверяю, что команды идут без пароля (sudo -n systemctl daemon-reload)"
if sudo -n /usr/bin/systemctl daemon-reload 2>/dev/null; then
  echo "OK: агент может перезапускать hh-scout без пароля — bash scripts/svc.sh restart | reinstall | status"
else
  echo "ВНИМАНИЕ: правило установлено, но sudo -n не прошёл — проверьте: sudo -l | grep hh-scout"
  exit 1
fi
