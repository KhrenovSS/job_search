#!/usr/bin/env bash
# Разбор последнего подхода: пришли ли лиды с письмами и как отработали стадии.
# Только чтение: БД открывается на чтение, браузер не трогается — можно запускать даже во время сбора.
# Использование: bash go_hh.sh [ЧЧ:ММ | YYYY-MM-DD]   (по умолчанию — журнал за сегодня)
set -uo pipefail
cd "$(dirname "$0")"
DB="data/hh_scout.db"
SINCE="${1:-today}"
q() { sqlite3 -column -header "$DB" "$1"; }

echo "=== Сейчас: $(date '+%d.%m %H:%M') ==="
bash scripts/svc.sh status 2>/dev/null | tail -3

echo
echo "=== Прогоны за сегодня ==="
q "select id, time(started_at,'+3 hours') нач, time(finished_at,'+3 hours') кон, status, trigger,
          page_loads страниц, collected новых, prefiltered описаний, evaluated оценено,
          substr(coalesce(error,''),1,40) ошибка
     from runs where started_at >= date('now','start of day') order by id;"

echo
echo "=== Отправки за сутки (мгновенные и дайджест) ==="
q "select id, strftime('%d.%m %H:%M', sent_at, '+3 hours') время, items_count лидов, coalesce(note,'дайджест 12:00') вид
     from digests where sent_at >= datetime('now','-24 hours') order by id;"

echo
echo "=== Лиды 50-59: письмо и судьба ==="
q "select v.hh_id, substr(v.title,1,30) вакансия, substr(v.employer,1,18) компания, e.total балл,
          case v.status when 'sent' then 'отправлен' when 'evaluated' then 'в очереди' else v.status end состояние,
          case when e.floor=1 then 'минимум' else 'порог' end как,
          case when c.text is null then 'нет' else length(c.text)||' зн.' end письмо
     from vacancies v join evaluations e on e.vacancy_id = v.id
     left join cover_letters c on c.vacancy_id = v.id
    where e.total between 50 and 59 and v.status in ('evaluated','sent')
    order by e.total desc;"

echo
echo "=== Итог суток ==="
q "select (select count(*) from digest_items di join digests d on d.id=di.digest_id
            where d.sent_at >= datetime('now','-24 hours')) 'лидов за сутки',
         (select count(*) from cover_letters where created_at >= date('now','start of day')) 'писем сегодня',
         (select count(*) from vacancies v join evaluations e on e.vacancy_id=v.id
           where v.status='evaluated' and (e.total >= 50 or e.floor = 1)) 'ждут в очереди';"

echo
echo "=== Журнал подхода (стадии, письма, отправка) ==="
journalctl -u hh-scout --since "$SINCE" --no-pager 2>/dev/null \
  | grep -E "Прогон #|План сбора|Серия |Пауза |Отклики: синхрон|Триаж:|Описания:|Оценка:|Письма:|Письмо для|Редактор поправил|отправлено сразу|Дневной минимум|Дайджест #|Сбор завершён" \
  | cut -c1-200 | tail -40

echo
echo "=== Предупреждения и ошибки ==="
journalctl -u hh-scout --since "$SINCE" --no-pager -p warning 2>/dev/null | cut -c1-200 | tail -15 \
  || echo "чисто"
