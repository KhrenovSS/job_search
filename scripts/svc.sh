#!/usr/bin/env bash
# Service control for hh-scout, usable by the AI agent without a password once the owner has run
# scripts/grant_agent_control.sh (sudoers rule). Refuses to stop/restart while a crawl is running
# (one Marionette session — CLAUDE.md rule 4) unless --force is given.
# Usage: bash scripts/svc.sh status | logs [N] | incidents [YYYY-MM-DD] | start | stop | restart | reinstall | bridge-restart   [--force]
#   incidents — everything needed for a post-mortem of one day: warnings/errors and tracebacks from journald (hh-scout and
#   the bridge), the day's runs from the DB, Telegram alerts sent (kv alert:*), failed units. Read-only.
set -euo pipefail
cd "$(dirname "$0")/.."
DB="data/hh_scout.db"
CMD="${1:-status}"; shift || true
FORCE=0; N=40; DAY=""
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) DAY="$a" ;;
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
  # probe with a harmless command that IS in the sudoers rule (`true` is not; `sudo -l` says yes even when a password is needed)
  if ! sudo -n /usr/bin/systemctl daemon-reload 2>/dev/null; then
    echo "ОШИБКА: sudo без пароля не разрешён. Владелец: bash scripts/grant_agent_control.sh" >&2
    exit 4
  fi
}

show_status() {
  systemctl status hh-scout hh-scout-bridge --no-pager --lines=0 2>&1 | grep -E '^● |Active:' || true
  printf 'мост /health: %s\n' "$(curl -s --max-time 3 http://127.0.0.1:8766/health || echo 'не отвечает')"
  if crawl_running; then echo "сбор идёт: да"; else echo "сбор идёт: нет"; fi
}

show_incidents() {
  local day="${DAY:-$(date +%F)}" since until
  since="$day 00:00:00"; until="$day 23:59:59"
  echo "=== Разбор дня $day (журналы персистентны: /var/log/journal) ==="
  # the services log to stdout, so journald marks every line "info": filter by the level word in the text, not by -p
  echo "--- hh-scout: WARNING / ERROR / CRITICAL"
  journalctl -u hh-scout --since "$since" --until "$until" --no-pager -o short-iso 2>/dev/null \
    | grep -E ' (WARNING|ERROR|CRITICAL) ' | cut -c1-220 || true
  echo "--- hh-scout: исключения (Traceback, до 120 строк)"
  journalctl -u hh-scout --since "$since" --until "$until" --no-pager -o short-iso 2>/dev/null | grep -A14 'Traceback' | head -120 || true
  echo "--- hh-scout-bridge: WARNING / ERROR"
  journalctl -u hh-scout-bridge --since "$since" --until "$until" --no-pager -o short-iso 2>/dev/null \
    | grep -E 'WARNING|ERROR|Traceback' | cut -c1-220 || true
  echo "--- старты/остановки сервиса за день"
  journalctl -u hh-scout --since "$since" --until "$until" --no-pager -o short-iso 2>/dev/null \
    | grep -E 'HH-Scout запущен|HH-Scout остановлен|Планировщик запущен|SIGTERM' | cut -c1-160 || true
  echo "--- тревоги из журнала за день (health: Тревога <ключ>: текст)"
  journalctl -u hh-scout --since "$since" --until "$until" --no-pager -o short-iso 2>/dev/null \
    | grep -F 'hh_scout.health: Тревога' | sed -E 's/^([^ ]+) .*Тревога /\1  /' | cut -c1-200 || true
  echo "--- прогоны за день (время в БД — UTC; день считается по Москве)"
  if [ -f "$DB" ] && command -v sqlite3 >/dev/null; then
    sqlite3 -column -header "$DB" "select id, status, trigger, page_loads pages, collected, evaluated,
        substr(replace(started_at,'T',' '),12,5) start_utc, substr(replace(finished_at,'T',' '),12,5) end_utc,
        substr(coalesce(error,''),1,70) err
      from runs
      where replace(started_at,'T',' ') >= datetime('$day','-3 hours') and replace(started_at,'T',' ') < datetime('$day','+1 day','-3 hours')
      order by id;" 2>/dev/null || true
    echo "--- тревоги в kv alert:<ключ>:<дата> (сторож хранит только текущий день; прошлые дни — см. журнал выше)"
    sqlite3 -column "$DB" "select substr(key,7,length(key)-17) alert, substr(value,12,5) at_msk from kv where key like 'alert:%:$day' order by value;" 2>/dev/null || true
    echo "--- загрузок за день / лимит"
    sqlite3 -column "$DB" "select (select coalesce(sum(page_loads),0) from runs where replace(started_at,'T',' ') >= datetime('$day','-3 hours') and replace(started_at,'T',' ') < datetime('$day','+1 day','-3 hours')) loads, (select value from kv where key='daily_cap:$day') cap;" 2>/dev/null || true
  else
    echo "БД $DB недоступна или нет sqlite3"
  fi
  echo "--- systemd: юниты в состоянии failed"
  systemctl --failed --no-pager --no-legend 2>/dev/null | grep -E 'hh-scout' || echo "нет"
  printf 'hh-scout: %s · hh-scout-bridge: %s\n' "$(systemctl is-active hh-scout 2>/dev/null)" "$(systemctl is-active hh-scout-bridge 2>/dev/null)"
}

after_restart() {
  sleep 3
  printf 'hh-scout: %s\n' "$(systemctl is-active hh-scout || true)"
  journalctl -u hh-scout -n 8 --no-pager 2>/dev/null | grep -v apscheduler || true
}

case "$CMD" in
  status) show_status ;;
  logs) journalctl -u hh-scout -n "$N" --no-pager ;;
  incidents) show_incidents ;;
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
  -h|--help|help) sed -n '2,7p' "$0" ;;
  *) echo "Неизвестная команда: $CMD (status|logs [N]|incidents [YYYY-MM-DD]|start|stop|restart|reinstall|bridge-restart [--force])" >&2; exit 2 ;;
esac
