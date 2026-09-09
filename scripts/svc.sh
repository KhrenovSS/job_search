#!/usr/bin/env bash
# Service control for hh-scout, usable by the AI agent without a password once the owner has run
# scripts/grant_agent_control.sh (sudoers rule). Refuses to stop/restart while a crawl is running
# (one Marionette session — CLAUDE.md rule 4) unless --force is given.
# Usage: bash scripts/svc.sh status | logs [N] | start | stop | restart | reinstall | bridge-restart   [--force]
set -euo pipefail
cd "$(dirname "$0")/.."
DB="data/hh_scout.db"
CMD="${1:-status}"; shift || true
FORCE=0; N=40
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    ''|*[!0-9]*) echo "Неизвестный аргумент: $a" >&2; exit 2 ;;
    *) N="$a" ;;
  esac
done

crawl_running() {
  # manual CLI with the browser, or a scheduled sitting inside the service (runs.status='running')
  if pgrep -f 'hh_scout.(pipeline|browser)' >/dev/null 2>&1; then return 0; fi
  if [ -f "$DB" ] && command -v sqlite3 >/dev/null; then
    local n
    n="$(sqlite3 "$DB" "select count(*) from runs where status='running';" 2>/dev/null || echo 0)"
    [ "${n:-0}" -gt 0 ] && return 0
  fi
  return 1
}

guard() {
  if crawl_running; then
    if [ "$FORCE" = 1 ]; then
      echo "ВНИМАНИЕ: идёт сбор, продолжаю из-за --force"
    else
      echo "ОТКАЗ: идёт сбор (прогон running или процесс с браузером). Дождитесь конца подхода (/status → «сбор идёт: нет») или добавьте --force." >&2
      exit 3
    fi
  fi
}

need_sudo() {
  if ! sudo -n true 2>/dev/null; then
    echo "ОШИБКА: sudo без пароля не разрешён. Владелец: bash scripts/grant_agent_control.sh" >&2
    exit 4
  fi
}

show_status() {
  systemctl status hh-scout hh-scout-bridge --no-pager --lines=0 2>&1 | grep -E '^● |Active:' || true
  printf 'мост /health: %s\n' "$(curl -s --max-time 3 http://127.0.0.1:8766/health || echo 'не отвечает')"
  if crawl_running; then echo "сбор идёт: да"; else echo "сбор идёт: нет"; fi
}

after_restart() {
  sleep 3
  printf 'hh-scout: %s\n' "$(systemctl is-active hh-scout || true)"
  journalctl -u hh-scout -n 8 --no-pager 2>/dev/null | grep -v apscheduler || true
}

case "$CMD" in
  status) show_status ;;
  logs) journalctl -u hh-scout -n "$N" --no-pager ;;
  start) need_sudo; sudo -n /usr/bin/systemctl start hh-scout; after_restart ;;
  stop) guard; need_sudo; sudo -n /usr/bin/systemctl stop hh-scout; printf 'hh-scout: %s\n' "$(systemctl is-active hh-scout || true)" ;;
  restart)
    guard; need_sudo
    sudo -n /usr/bin/systemctl reset-failed hh-scout 2>/dev/null || true
    sudo -n /usr/bin/systemctl restart hh-scout; after_restart ;;
  reinstall)
    guard; need_sudo
    bash scripts/install_service.sh
    sudo -n /usr/bin/systemctl restart hh-scout; after_restart ;;
  bridge-restart)
    need_sudo; sudo -n /usr/bin/systemctl restart hh-scout-bridge; sleep 2
    printf 'мост /health: %s\n' "$(curl -s --max-time 5 http://127.0.0.1:8766/health || echo 'не отвечает')" ;;
  -h|--help|help) sed -n '2,5p' "$0" ;;
  *) echo "Неизвестная команда: $CMD (status|logs [N]|start|stop|restart|reinstall|bridge-restart [--force])" >&2; exit 2 ;;
esac
