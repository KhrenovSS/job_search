#!/usr/bin/env bash
# Разбор последних подходов: что ушло владельцу, с письмами или без, и как отработали стадии.
# Только чтение: БД открывается на чтение, браузер не трогается — можно запускать даже во время сбора.
# Использование: bash go_hh.sh [ЧЧ:ММ | YYYY-MM-DD]   (по умолчанию — журнал за сегодня)
set -uo pipefail
cd "$(dirname "$0")"
DB="data/hh_scout.db"
SINCE="${1:-today}"
q() { sqlite3 -column -header "$DB" "$1"; }
# journalctl без подсказки «you are currently not seeing messages from other users» в каждом вызове.
jr() { journalctl -q -u hh-scout --since "$SINCE" --no-pager 2>/dev/null; }
# Порог — из .env хозяйства (решение №52: 50), а не зашит в скрипт: владелец может его поменять.
THRESHOLD="$(grep -E '^SCORE_THRESHOLD=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -dc '0-9')"
THRESHOLD="${THRESHOLD:-50}"

# Границы времени строятся через strftime с «T», как хранится в базе (`2026-09-22T01:42:55+00:00`): у datetime()
# разделитель — пробел, а ' ' < 'T', и сравнение строк втягивало лишние часы (за сутки насчитывалось 283 страницы
# вместо 86). DAY — местная полночь моментом UTC: `date('now','start of day')` это полночь UTC, то есть 03:00 МСК,
# и с ночными подходами (v9.15) такой фильтр терял всё, что случилось между полуночью и тремя часами.
DAY="strftime('%Y-%m-%dT%H:%M:%S','now','+3 hours','start of day','-3 hours')"
H24="strftime('%Y-%m-%dT%H:%M:%S','now','-24 hours')"
# Канал лида человеческими словами: по нему видно, откуда пришла компания (v9.13, v9.15).
CHANNEL="case v.search_pass
           when 'panel' then 'щитовик' when 'design' then 'бюро' when 'owen_si' then 'ОВЕН'
           when 'plant' then 'эксплуатант' when 'profi' then 'profi' when 'similar' then 'похожие'
           else 'вакансия hh' end"
# Очередь — то же, что видит `repo.lead_queue`: выше порога или добрано дневным минимумом.
IN_QUEUE="v.status='evaluated' and (e.total >= $THRESHOLD or e.floor = 1)"

echo "=== Сейчас: $(date '+%d.%m %H:%M') ==="
echo "код: $(git log -1 --format='%h %ad %s' --date=format:'%d.%m %H:%M' 2>/dev/null | cut -c1-110)"
echo "порог: $THRESHOLD · незакоммичено: $(git status --porcelain 2>/dev/null | wc -l) файл(ов)"
bash scripts/svc.sh status 2>/dev/null | grep -v '^● '
q "select coalesce((select strftime('%d.%m %H:%M', substr(value,1,19)) from kv where key='next_crawl_at'),
                    'не назначен') 'следующий подход',
         (select coalesce(sum(page_loads),0) from runs where started_at >= $DAY) || ' / ' ||
         coalesce((select value from kv where key = 'daily_cap:' || date('now','+3 hours')), '?') 'страниц за день';"

echo
echo "=== Сутки одной строкой ==="
q "select (select count(*) from runs where started_at >= $H24) подходов,
         (select count(*) from digest_items di join digests d on d.id=di.digest_id
            where d.sent_at >= $H24)                              лидов,
         (select count(*) from cover_letters where created_at >= $DAY)                   'писем сегодня',
         (select count(*) from vacancies v join evaluations e on e.vacancy_id=v.id where $IN_QUEUE) 'в очереди',
         (select count(*) from vacancies v join evaluations e on e.vacancy_id=v.id
            left join cover_letters c on c.vacancy_id=v.id
            where $IN_QUEUE and c.text is null)                                          'из них без письма';"

echo
echo "=== Прогоны за сутки ==="
q "select id, strftime('%d.%m %H:%M', started_at,'+3 hours') нач, time(finished_at,'+3 hours') кон, status, trigger,
          page_loads страниц, collected новых, prefiltered описаний, evaluated оценено,
          substr(coalesce(error,''),1,40) ошибка
     from runs where started_at >= $H24 order by id;"

echo
echo "=== Отправки за сутки (мгновенные и дайджест) ==="
q "select id, strftime('%d.%m %H:%M', sent_at, '+3 hours') время, items_count лидов,
          coalesce(note,'дайджест 12:00') вид
     from digests where sent_at >= $H24 order by id;"

echo
echo "=== Что ушло за сутки: по каналам ==="
q "select $CHANNEL канал, count(*) лидов, round(avg(e.total)) 'средний балл',
          sum(case when c.text is null then 1 else 0 end) 'без письма',
          sum(case when e.floor=1 then 1 else 0 end) 'ниже порога'
     from digest_items di
     join digests d on d.id = di.digest_id
     join vacancies v on v.id = di.vacancy_id
     left join evaluations e on e.vacancy_id = v.id
     left join cover_letters c on c.vacancy_id = v.id
    where d.sent_at >= $H24
    group by 1 order by 2 desc;"

# Лид без письма — это сбой: карточка без готового письма отправляться не должна (v9.14, v9.15).
echo
echo "=== Ушло БЕЗ письма (должно быть пусто) ==="
q "select strftime('%d.%m %H:%M', d.sent_at, '+3 hours') ушло, $CHANNEL канал,
          substr(coalesce(v.employer,'—'),1,24) компания, v.hh_id, di.letter_message_id 'id письма'
     from digest_items di
     join digests d on d.id = di.digest_id
     join vacancies v on v.id = di.vacancy_id
     left join cover_letters c on c.vacancy_id = v.id
    where d.sent_at >= $H24 and c.text is null
    order by d.sent_at;"

echo
echo "=== Последние 25 лидов (главное: есть ли письмо) ==="
q "select ушло, канал, компания, вакансия, балл, минимум, письмо from (
     select strftime('%d.%m %H:%M', d.sent_at, '+3 hours') ушло, $CHANNEL канал,
            substr(coalesce(v.employer,'—'),1,20) компания,
            substr(case when v.lead_kind='company' then '' else coalesce(v.title,'') end,1,24) вакансия,
            e.total балл,
            case when e.floor=1 then 'мин' else '' end минимум,
            case when c.text is null then 'НЕТ ПИСЬМА' else length(c.text)||' зн.' end письмо,
            d.sent_at ord, e.total tot
       from digest_items di
       join digests d on d.id = di.digest_id
       join vacancies v on v.id = di.vacancy_id
       left join evaluations e on e.vacancy_id = v.id
       left join cover_letters c on c.vacancy_id = v.id
      where d.sent_at >= $H24
      order by d.sent_at desc, e.total desc limit 25)
   order by ord, tot desc;"

echo
echo "=== Очередь, каналы компаний и что ждёт открытия ==="
q "select (select count(*) from vacancies where site='owen' and status='new')             'ОВЕН ждёт',
         (select count(*) from vacancies where site='owen' and status='sent')             'ОВЕН ушло',
         (select count(*) from vacancies where skip_reason='plant_pool')                  'эксплуатанты в пуле',
         (select count(*) from vacancies where search_pass='plant' and status='to_fetch') 'эксплуатанты к открытию',
         (select count(*) from vacancies where search_pass='plant' and status='sent')     'эксплуатанты ушло',
         (select count(*) from vacancies where lead_kind='company' and site='hh' and status='to_fetch') 'компаний к открытию',
         (select count(*) from vacancies where lead_kind='vacancy' and status='to_fetch')               'вакансий к открытию';"

echo
echo "=== Журнал подхода (стадии, письма, отправка) ==="
# Построчные «Письмо для … — правим и переписываем» и «Редактор поправил» сюда не берутся: при шести подходах
# по 30 писем они вытесняли из хвоста сами стадии, а длина каждого письма и так видна в таблице лидов.
# Неудачи писем («Письмо для … отклонено») остаются.
jr | grep -E "Прогон #|План сбора|Серия |Пауза |Отклики: синхрон|Префильтр|Триаж:|Описания:|Каталог ОВЕН|Эксплуатанты|допущено|Оценка:|Письма:|Бюджет времени|отклонено|прежним правилам|отправлено сразу|Дневной минимум|Добрано|Дайджест #|Карточка .* отозвана|Сбор завершён|avito" \
  | cut -c1-200 | tail -60

echo
echo "=== Предупреждения и ошибки ==="
# Сервис пишет логи в stdout, и journald ставит всем строкам приоритет info — `journalctl -p warning` всегда пуст.
# Поэтому уровень берём из текста строки. Сетевые повторы aiogram (Telegram не ответил, повтор через N с) — шум
# самовосстановления: одной строкой-счётчиком, а не пятнадцатью строками хвоста.
WARN="$(jr | grep -E " (WARNING|ERROR|CRITICAL) |Traceback")"
if [ -z "$WARN" ]; then
  echo "чисто"
else
  NET="$(grep -cE "aiogram.dispatcher: (Failed to fetch updates|Sleep for)" <<<"$WARN")"
  [ "$NET" -gt 0 ] && echo "сетевые повторы Telegram (aiogram): $NET строк"
  echo "по уровням и источникам:"
  grep -vE "aiogram.dispatcher: (Failed to fetch updates|Sleep for)" <<<"$WARN" \
    | grep -oE " (WARNING|ERROR|CRITICAL) +[a-z_.]+" | sort | uniq -c | sort -rn | head -10
  echo "последние:"
  grep -vE "aiogram.dispatcher: (Failed to fetch updates|Sleep for|Received SIGTERM)" <<<"$WARN" | cut -c1-200 | tail -15
fi
