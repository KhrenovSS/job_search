#!/usr/bin/env bash
# Разбор последнего подхода: что ушло владельцу, с письмами или без, и как отработали стадии.
# Только чтение: БД открывается на чтение, браузер не трогается — можно запускать даже во время сбора.
# Использование: bash go_hh.sh [ЧЧ:ММ | YYYY-MM-DD]   (по умолчанию — журнал за сегодня)
set -uo pipefail
cd "$(dirname "$0")"
DB="data/hh_scout.db"
SINCE="${1:-today}"
q() { sqlite3 -column -header "$DB" "$1"; }

# Канал лида человеческими словами: по нему видно, откуда пришла компания (v9.13).
CHANNEL="case v.search_pass
           when 'panel' then 'щитовик' when 'design' then 'бюро' when 'owen_si' then 'ОВЕН'
           when 'profi' then 'profi' when 'similar' then 'похожие' else 'вакансия hh' end"

echo "=== Сейчас: $(date '+%d.%m %H:%M') ==="
bash scripts/svc.sh status 2>/dev/null | tail -3

echo
echo "=== Прогоны за сегодня ==="
q "select id, time(started_at,'+3 hours') нач, time(finished_at,'+3 hours') кон, status, trigger,
          page_loads страниц, collected новых, prefiltered описаний, evaluated оценено,
          substr(coalesce(error,''),1,40) ошибка
     from runs where started_at >= date('now','start of day') order by id;"

echo
echo "=== Что ушло за сутки (главное: есть ли письмо) ==="
q "select strftime('%d.%m %H:%M', d.sent_at, '+3 hours') ушло, $CHANNEL канал,
          substr(coalesce(v.employer,'—'),1,20) компания,
          substr(case when v.lead_kind='company' then '' else coalesce(v.title,'') end,1,24) вакансия,
          e.total балл,
          case when e.floor=1 then 'мин' else '' end минимум,
          case when c.text is null then 'НЕТ ПИСЬМА' else length(c.text)||' зн.' end письмо
     from digest_items di
     join digests d on d.id = di.digest_id
     join vacancies v on v.id = di.vacancy_id
     left join evaluations e on e.vacancy_id = v.id
     left join cover_letters c on c.vacancy_id = v.id
    where d.sent_at >= datetime('now','-24 hours')
    order by d.sent_at, e.total desc;"

echo
echo "=== Отправки за сутки (мгновенные и дайджест) ==="
q "select id, strftime('%d.%m %H:%M', sent_at, '+3 hours') время, items_count лидов,
          coalesce(note,'дайджест 12:00') вид
     from digests where sent_at >= datetime('now','-24 hours') order by id;"

echo
echo "=== Очередь, каталог и что ждёт открытия ==="
q "select (select count(*) from vacancies v join evaluations e on e.vacancy_id=v.id
            where v.status='evaluated' and (e.total >= 50 or e.floor = 1))            'в очереди',
         (select count(*) from cover_letters where created_at >= date('now','start of day')) 'писем сегодня',
         (select count(*) from digest_items di join digests d on d.id=di.digest_id
            where d.sent_at >= datetime('now','-24 hours'))                            'лидов за сутки',
         (select count(*) from vacancies where site='owen' and status='new')           'каталог ждёт',
         (select count(*) from vacancies where site='owen' and status='sent')          'каталог ушло',
         (select count(*) from vacancies where lead_kind='company' and site='hh' and status='to_fetch') 'компаний к открытию',
         (select count(*) from vacancies where lead_kind='vacancy' and status='to_fetch')               'вакансий к открытию';"

echo
echo "=== Журнал подхода (стадии, письма, отправка) ==="
journalctl -u hh-scout --since "$SINCE" --no-pager 2>/dev/null \
  | grep -E "Прогон #|План сбора|Серия |Пауза |Отклики: синхрон|Префильтр|Триаж:|Описания:|Каталог ОВЕН|допущено|Оценка:|Письма:|Письмо для|Редактор поправил|отправлено сразу|Дневной минимум|Добрано|Дайджест #|Сбор завершён|avito" \
  | cut -c1-200 | tail -40

echo
echo "=== Предупреждения и ошибки ==="
journalctl -u hh-scout --since "$SINCE" --no-pager -p warning 2>/dev/null | cut -c1-200 | tail -15 \
  || echo "чисто"
