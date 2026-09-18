# Схема БД (SQLite)

Файл `DB_PATH` (по умолчанию `data/hh_scout.db`, WAL). Миграции — список функций в `db.py`, версия через
`PRAGMA user_version`; применённые не редактировать, только добавлять: `_m001_initial` (таблицы),
`_m002_triage_columns` (`vacancies.triage_priority`, `triage_note`), `_m003_lead_scoring` (`evaluations.role_score`,
`lead_score`, `company_kind`, `pitch_hint`), `_m004_cover_letters`, `_m005_lead_actions` (`lead_actions`,
`digest_items.letter_message_id`), `_m006_site`, `_m007_employer_id`, `_m008_accept_temporary`
(`vacancies.accept_temporary`, `civil_law_contracts`), `_m009_employers`, `_m010_negotiation_state`
(`vacancies.negotiation_state`, `negotiation_seen_at`). Время — TEXT ISO-8601 UTC.
Весь SQL — в `src/hh_scout/pipeline/repo.py`.

| Таблица | Назначение |
|---|---|
| `areas_cache` | регионы России из открытого `api.hh.ru/areas` (обновляется раз в 30 дней) |
| `vacancies` | все увиденные вакансии; `hh_id` UNIQUE = «уже видели» |
| `evaluations` | одна оценка на вакансию (UNIQUE `vacancy_id`); при переоценке строка пересоздаётся |
| `cover_letters` | текст отклика (UNIQUE `vacancy_id`), `model_note` |
| `digests`, `digest_items` | отправленные подборки; `tg_message_id` карточки, `letter_message_id` письма (для сворачивания) |
| `lead_actions` | действия владельца по отправленному лиду: `action` liked / disliked / responded / auto_responded / deferred / closed_stale, `reason`, `created_at`. Лид открыт, пока нет responded/auto_responded/disliked/closed_stale |
| `feedback` | 👍/👎: `value` ±1, `reason` salary/format/stack/agency/NULL |
| `runs` | прогоны: `trigger` schedule/manual, `status` running/ok/failed, метрики collected/prefiltered/evaluated/bridge_calls/page_loads, `error`; колонка `sent` не используется |
| `kv` | флаги, см. ниже |

## `vacancies.status` — 9 значений
| Статус | Кто ставит | Смысл |
|---|---|---|
| `new` | collector | карточка собрана, правила не применялись |
| `triage` | prefilter | прошла правила, ждёт триаж ИИ |
| `to_fetch` | triage | ИИ велел открыть страницу; `triage_priority` 1–3 |
| `prefiltered` | details | страница загружена, `raw_json` заполнен, ждёт оценку |
| `evaluated` | evaluator | оценена, строка в `evaluations`, кандидат в дайджест |
| `sent` | digest | отправлена в дайджесте (или помечена при первом старте сервиса — `kv.preview_marked`); открыт/закрыт лид — по `lead_actions` |
| `rejected` | digest | была `evaluated`, `total < score_threshold` на момент дайджеста |
| `skipped` | prefilter/collector/triage/details/dedup/digest | отсеяна; всегда с `skip_reason` |
| `evaluation_failed` | evaluator (ИИ дважды вернул невалидный ответ / пропустил hh_id) или details (страница без `vacancyView`) | терминальная ошибка, не повторяется |

Лиды выше порога, не влезшие в `DIGEST_MAX_ITEMS` (10), остаются `evaluated` до следующего дайджеста.

## `vacancies.skip_reason`
`applied` (владелец уже откликался) · `archived` · `stopword:<слово>` · `no_engineering_title`
(нет инженерного слова в названии — самый частый) · `triage` (ИИ: не открывать) · `invalid_ai_answer` /
`missing_in_ai_answer` (оценка) · `no_vacancy_view` (страница без данных) · `low_priority_expired` (приоритет 3 триажа не открыт за `LOW_PRIORITY_TTL_DAYS`) · `queue_expired` (v9.1: лид простоял в очереди дольше `QUEUE_TTL_DAYS` — до него так и не дошла суточная норма) ·
`duplicate_employer:<hh_id>` (v8: у компании уже есть лид `<hh_id>` — отправленный за последние `EMPLOYER_REPEAT_DAYS` или ждущий
дайджест; ставится на любом статусе от `triage` до `evaluated`, см. `pipeline/dedup.py`). **Единственная обратимая причина** (v8.7): если `<hh_id>` так и не стал лидом (отклонён, сорвалась оценка или оценён ниже порога) и у компании лида
не осталось, `dedup.revive_orphans` в начале каждого прогона возвращает дубль в `to_fetch` (триаж ИИ уже пройден) или `triage`. ·
`employer_responded:<hh_id>` (v9.3: в компанию уже откликались — сам владелец на hh.ru (`applied=1`) или кнопкой «✅ Написал»
(`lead_actions.responded`/`auto_responded`) — не позже `EMPLOYER_REPEAT_DAYS` назад; письмо уходит кадровику всей организации,
второе на тот же стол не нужно. Причина **необратимая**, в отличие от `duplicate_employer:`: `revive_orphans` такие строки
не воскрешает). `set_status` в не-skip переходах обнуляет `skip_reason`.

## Прочие поля `vacancies`
- `work_format`: remote / hybrid / office / field / unknown (приоритет remote > hybrid > office > field).
- `employment`: full / part (в т.ч. SIDE_JOB) / project / fly_in_fly_out / unknown.
- `salary_raw`: исходный объект `compensation` hh (JSON); `salary_from`, `salary_to`: рубли net (gross×0.87) **только для
  месячных RUR**; валюта/почасовые хранятся как есть (пометка `Salary.note` в БД не хранится, вычисляется из `salary_raw`).
- `site` (v7, `_m006`): `hh` (по умолчанию) / `profi`. Заказы profi.ru: `hh_id = 'profi:<номер заказа>'` — глобальный `UNIQUE`
  остаётся, коллизий с hh нет; `employer` = имя клиента, `employment = project`, `raw_json = {description, budget, when, client,
  posted, work_format, city, site}`, `salary_raw = {"profi_budget": "до 5000 ₽", from, to, …}`; статус сразу `prefiltered`.
- `source`: `search:<idx>` / `similar_to_resume` / `negotiations` / `profi`; `search_pass`: regional / remote / project / **gph**
  (v8.2, поиск с фильтром hh `accept_temporary=true`) / similar / negotiations / profi.
- `accept_temporary`, `civil_law_contracts` (v8.2, `_m008`) — **что о форме оформления говорит сам hh.ru**, а не ИИ по тексту:
  `accept_temporary` 0/1 — отметка «Оформление по ГПХ или по совместительству» (`acceptTemporary`);
  `civil_law_contracts` — JSON-список форм помимо ТК РФ (`INDIVIDUAL_ENTREPRENEUR` — ИП, `SELF_EMPLOYED` — самозанятый,
  `INDIVIDUAL_PERSON` — физлицо) или NULL. Пишутся из карточки поиска и со страницы вакансии (`accept_temporary` через
  `MAX`, список через `COALESCE` — флаг, увиденный однажды, не теряется). **Бэкфилла нет**: до v8.2 поля не сохранялись,
  старые строки остаются 0/NULL и наполняются при новом сборе. У profi.ru — всегда 0/NULL (поля hh, к заказам не относятся).
- `raw_json`: **урезанный** `vacancyView` (`repo.DETAIL_KEYS`: vacancyId, name, description, keySkills, compensation,
  workFormats, employmentForm, area, status, publicationDate, workExperience, workScheduleByDays, workingHours,
  closedForApplicants, userLabels, civilLawContracts + company{id,name,visibleName,@trusted}, address{city,street,building,displayName}).
- `applied`, `has_chat`: из страницы откликов; `triage_priority`, `triage_note`: от триажа.
- `employer_id` (v8, `_m007`): hh.ru `company.id` строкой — ключ «одна компания — один лид»; пишется из карточки поиска и со
  страницы вакансии, для старых строк заполнен из `raw_json.company.id`; у карточек до v8 и у profi.ru — NULL (тогда сравнение
  по `employer` через SQL-функцию `casefold`, зарегистрированную в `db.connect`). Индексы `idx_vacancies_employer_id`, `idx_vacancies_employer`.

## `evaluations`
`tech_score`, `role_score`, `lead_score` (0–100, от ИИ); `total` (код: 0.55/0.25/0.20); `salary_score`, `format_score` —
устаревшие, всегда 0; `ip_gph_possible` yes/maybe/no; `is_agency`; `employment_hint` staff/project/unknown;
`company_kind` integrator/manufacturer/end_customer/agency/unknown; `verdict`; `pitch_hint`; `red_flags` JSON; `model_note` (NULL).

## `kv` — служебные ключи
| Ключ | Значение | Кто |
|---|---|---|
| `crawl_attempts` | — | устаревший ключ v5 (повторы сбора); удаляется при каждом старте сервиса |
| `paused` | `"1"` или отсутствует | `/pause`, `/resume`; глушит только плановые сборы, дайджест идёт |
| `next_crawl_at` | ISO с зоной | старт следующего подхода; пуст во время планового сбора (после него назначается следующий) |
| `crawl_window_idx` | 0…N−1 | индекс окна `CRAWL_WINDOWS` назначенного/идущего подхода |
| `crawl_window_date` | YYYY-MM-DD | день этого окна |
| `sitting_done` | `YYYY-MM-DD:idx` | последнее окно, в котором подход сегодня уже стартовал или был пропущен по паузе; по нему планировщик не назначает то же окно дважды |
| `daily_cap:<YYYY-MM-DD>` | число | лимит загрузок на день (случайный из `DAILY_PAGE_LOADS_MIN..MAX`); старые ключи удаляются |
| `alert:<key>:<YYYY-MM-DD>` | ISO | тревога уже отправлена сегодня (`health.Alerter`); старые дни чистятся |
| `alerts_today`, `alerts_cleaned`, `watchdog_last` | служебные | счётчик/дата очистки/время последней проверки сторожа |
| `preview_marked` | `"1"` | одноразовый флаг первого старта сервиса (`main.py`) |

## Инварианты
1. Вакансия попадает в дайджест не более одного раза (UNIQUE `hh_id` + статусы `sent`/`rejected`).
1a. Компания hh.ru получает не больше одного лида за `EMPLOYER_REPEAT_DAYS` (90; 0 — навсегда): остальные её вакансии —
   `skipped/duplicate_employer:<hh_id>`. profi.ru не дедуплицируется (`employer` там — имя клиента).
1b. Компания, куда владелец уже откликнулся, за те же `EMPLOYER_REPEAT_DAYS` не получает ни лида, ни письма:
   её вакансии — `skipped/employer_responded:<hh_id>` (v9.3). Проверка стоит раньше 1а — она верна и тогда, когда
   лида у компании не было вовсе (отклик отправлен прямо на hh.ru).
2. `applied=1` никогда не отправляется.
3. Перезапуск после сбоя продолжает с места остановки: статусы фиксируются после каждого шага.
4. «Проверено N» в заголовке = число строк `evaluations`, созданных после последнего дайджеста.
5. Резервное копирование БД **не реализовано** (этап 7).

## `employers` (v9.0, `_m009`)
Досье на работодателя из открытых источников — одно на компанию, переиспользуется всеми её вакансиями.
`employer_id` (PK) — hh.ru `company.id`, тот же ключ, что у «одна компания — один лид»; `name`;
`found` (0/1 — искали и не нашли тоже запоминается, чтобы не платить за пустоту дважды); `brief` — JSON
`CompanyBrief` (`what_they_do`, `industry`, `products`, `sites`, `scale`, `automation_hooks`, `sources`, `note`);
`sources` — JSON списка прочитанных URL; `researched_at`. Свежесть — `COMPANY_RESEARCH_TTL_DAYS` (180 дней).
Заполняет `llm/company_research.py`; в карточку лида попадает строка «🏭 О компании» (`LEAD_SELECT` подмешивает
`brief` как `company_brief`). Поля-факты собраны с прочитанных страниц, `automation_hooks` — явные предположения.

## Что ответила компания (v9.7)
`vacancies.negotiation_state` — состояние переписки на hh.ru как его отдаёт сам сайт (`RESPONSE` — отклик без ответа,
`INTERVIEW` — пригласили, `DISCARD` — отказ), `negotiation_seen_at` — когда мы это увидели. Пишется при синхронизации
откликов (`collector._sync_negotiations` → `repo.mark_applied`) и **только вперёд**: hh не отзывает приглашение.
Успехом метода считается `INTERVIEW`, отказ — неудачей (решение №42). Старое поле `has_chat` («кто-то ответил»)
остаётся как более грубый сигнал. `repo.outcome_stats` сводит это по полосам балла (`repo.SCORE_BANDS`), `/stats`
показывает таблицу, шапка дайджеста — число приглашений за 14 дней. Письма, отправленные мимо hh.ru
(`applied = 0`), попадают в колонку «без канала измерения»: их исход нам не виден, и в конверсию они не идут.

## `evaluated` — это очередь (v9.1)
Статус `evaluated` с `total ≥ SCORE_THRESHOLD` означает не «ждёт ближайшего дайджеста», а «стоит в очереди».
Дайджест забирает сверху `DIGEST_MAX_ITEMS` (10) по приоритету `total + MIN(суток ожидания, QUEUE_WAIT_BONUS_MAX)`,
остальные **остаются `evaluated`** и соревнуются с завтрашними поступлениями (`repo.lead_queue`, `repo.queue_size`).
`reject_below` по-прежнему списывает то, что ниже порога; `repo.expire_queue` — то, что простояло дольше
`QUEUE_TTL_DAYS`. Ожидание считается от `evaluations.created_at` (строка на вакансию одна).

