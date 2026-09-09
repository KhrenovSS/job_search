#!/usr/bin/env bash
# Send one Telegram message to the owner without Python — used by systemd OnFailure= when hh-scout itself is down.
# Reads TG_BOT_TOKEN and TG_OWNER_CHAT_ID from the environment (EnvironmentFile=.env in the unit) or from ./.env.
set -u
cd "$(dirname "$0")/.." || exit 0
if [ -z "${TG_BOT_TOKEN:-}" ] || [ -z "${TG_OWNER_CHAT_ID:-}" ]; then
  [ -f .env ] && { TG_BOT_TOKEN=$(grep -E '^TG_BOT_TOKEN=' .env | cut -d= -f2-); TG_OWNER_CHAT_ID=$(grep -E '^TG_OWNER_CHAT_ID=' .env | cut -d= -f2-); }
fi
[ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_OWNER_CHAT_ID:-}" ] || { echo "tg_alert: нет TG_BOT_TOKEN/TG_OWNER_CHAT_ID" >&2; exit 0; }
TEXT="${*:-🚨 hh-scout: сервис упал и не смог перезапуститься. Смотрите: journalctl -u hh-scout -n 50}"
curl -sS -m 15 -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TG_OWNER_CHAT_ID}" --data-urlencode "text=${TEXT}" >/dev/null || echo "tg_alert: отправка не удалась" >&2
exit 0
