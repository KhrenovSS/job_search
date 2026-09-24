# Схема БД (SQLite)

Файл `DB_PATH` (по умолчанию `data/hh_scout.db`, WAL). Миграции — список функций в `db.py`, версия через
`PRAGMA user_version`; применённые не редактировать, только добавлять: `_m001_initial` (таблицы),
`_m002_triage_columns` (`vacancies.triage_priority`, `triage_note`), `_m003_lead_scoring` (`evaluations.role_score`,
`lead_score`, `company_kind`, `pitch_hint`), `_m004_cover_letters`, `_m005_lead_actions` (`lead_actions`,
`digest_items.letter_message_id`), `_m006_site`, `_m007_employer_id`, `_m008_accept_temporary`
(`vacancies.accept_temporary`, `civil_law_contracts`), `_m009_employers`, `_m010_negotiation_state`
(`vacancies.negotiation_state`, `negotiation_seen_at`), `_m011_letter_rules` (`cover_letters.rules_hash`),
`_m012_negotiation_events` (`negotiation_events` + засев из снимка), `_m013_letter_context` (`cover_letters.owner_hint`,
`with_dossier`), `_m014_floor` (`evaluations.floor` — взята по дневному минимуму ниже порога, решение №52),
`_m015_lead_kind` (`vacancies.lead_kind` vacancy/company, `evaluations.offer_focus` — решение №53).
Время — TEXT ISO-8601 UTC; границы для сравнения строятся через `repo.iso_utc` (строка с `+03:00` рядом с `+00:00`
сравнивается как текст и сдвигает окно на три часа).
Весь SQL — в `src/hh_scout/pipeline/repo.py`; аналитика над строками (исходы, полосы, зрелость) — в `pipeline/outcomes.py`.

| Таблица | Назначение |
|---|---|
| `areas_cache` | регионы России из открытого `api.hh.ru/areas` (обновляется раз в 30 дней) |
| `vacancies` | все увиденные вакансии; `hh_id` UNIQUE = «уже видели» |
| `evaluations` | одна оценка на вакансию (UNIQUE `vacancy_id`); при переоценке строка пересоздаётся |
| `cover_letters` | текст отклика (UNIQUE `vacancy_id`), `model_note`, `created_at` (время **последней** записи — по нему считается суточная норма писем), `rules_hash` — отпечаток правил, по которым письмо написано (промпт письма + профиль + резюме + чеклист редактора + правила кода `llm/letter_checks.CODE_RULES` + рамка длины, `llm.cover_letter.rules_hash`). NULL или чужой отпечаток = письмо устарело и переписывается перед отправкой (решение №50). `owner_hint` — пожелание из `/letter <id> …`, переживает переписку; `with_dossier` 0/1 — было ли досье компании при написании: письмо без досье устаревает, когда досье появилось (v9.11) |
| `negotiation_events` | история переписки: `vacancy_id`, `state`, `has_messages`, `seen_at`. Строка добавляется **при смене состояния или флага `has_messages`** (`repo.record_negotiation`); `state` может быть NULL — засев из снимка в `_m012` (`seen_at` = `COALESCE(negotiation_seen_at, updated_at)`), поэтому три синхронизации в сутки ничего не раздувают. Отсюда берутся время до ответа и переходы `RESPONSE → INTERVIEW/DISCARD` (решение №48) |
| `digests`, `digest_items` | отправленные подборки; `tg_message_id` карточки, `letter_message_id` письма (для сворачивания) |
| `lead_actions` | действия владельца по отправленному лиду: `action` liked / disliked / responded / auto_responded / deferred / closed_stale, `reason`, `created_at`. Лид открыт, пока нет responded/auto_responded/disliked/closed_stale |
| `feedback` | 👍/👎: `value` ±1, `reason` — код salary/format/stack/agency, NULL или **текст владельца** («✍️ своими словами», до 300 знаков, v9.11); текст попадает в калибровочный блок оценщика дословно |
| `runs` | прогоны: `trigger` schedule/manual, `status` running/ok/failed, метрики collected/prefiltered/evaluated/bridge_calls/page_loads, `error`; колонка `sent` не используется |
| `employers` | досье на компанию из открытых источников (веб-разведка, v9.0) — свой раздел ниже |
| `kv` | флаги, см. ниже |

## `vacancies.status` — 9 значений
| Статус | Кто ставит | Смысл |
|---|---|---|
| `new` | collector | карточка собрана, правила не применялись |
| `triage` | prefilter | прошла правила, ждёт триаж ИИ |
| `to_fetch` | triage · `plant.admit` (из пула, приоритет 3) · `dedup.revive_orphans` (дубль, чей «победитель» лидом не стал) | ИИ велел открыть страницу; `triage_priority` 1–3 |
| `prefiltered` | details · `repo.admit_company_leads` (каталог ОВЕН: `new` → сразу сюда, страницы нет) · profi (заказ вставляется сразу в этом статусе) | страница загружена (или описание есть из другого источника), `raw_json` заполнен, ждёт оценку |
| `evaluated` | evaluator | оценена, строка в `evaluations`, кандидат в дайджест |
| `sent` | digest | отправлена в дайджесте (или помечена при первом старте сервиса — `kv.preview_marked`); открыт/закрыт лид — по `lead_actions` |
| `rejected` | digest | была `evaluated`, `total < score_threshold` на момент дайджеста (строки с `evaluations.floor = 1` не списываются); либо простояла в очереди дольше `QUEUE_TTL_DAYS` — тогда с `skip_reason = queue_expired`. Обратимо (v9.12): дневной минимум (`digest_builder.promote_floor`) и `evaluator --readmit` возвращают `rejected` без `skip_reason` в `evaluated` |
| `skipped` | prefilter/collector/triage/details/dedup/digest · синхронизация откликов (`mark_applied` → `applied`, в т.ч. заглушки для незнакомых вакансий) · `plant.pool` (`plant_pool`) · письма (`no_email`) | отсеяна; всегда с `skip_reason` |
| `evaluation_failed` | evaluator (ИИ дважды вернул невалидный ответ / пропустил hh_id) или details (страница без `vacancyView`) | терминальная ошибка, автоматически не повторяется (вернуть вручную; `repo.reset_catalogue_rows` для строк ОВЕН вызывается только из тестов) |

Лиды выше порога, к которым ещё не написано письмо (бюджет `LETTERS_BUDGET_MIN` кончился, мост молчал), остаются `evaluated` до следующей отправки.

## `vacancies.skip_reason`
`applied` (владелец уже откликался) · `archived` · `stopword:<слово>` · `no_engineering_title`
(нет инженерного слова в названии — самый частый) · `triage` (ИИ: не открывать) · `invalid_ai_answer` /
`missing_in_ai_answer` (оценка) · `no_vacancy_view` (страница без данных) · `low_priority_expired` (приоритет 3 триажа не открыт за `LOW_PRIORITY_TTL_DAYS`) · `queue_expired` (v9.1: лид простоял в очереди дольше `QUEUE_TTL_DAYS` — до него так и не дошла суточная норма) ·
`duplicate_employer:<hh_id>` (v8: у компании уже есть лид `<hh_id>` — отправленный за последние `EMPLOYER_REPEAT_DAYS` или ждущий
дайджест; ставится на любом статусе от `triage` до `evaluated`, см. `pipeline/dedup.py`). **Обратимая причина** (v8.7; штатно возвращается ещё `plant_pool`, а любую причину вернёт `prefilter --requeue-reason`): если `<hh_id>` так и не стал лидом (отклонён, сорвалась оценка или оценён ниже порога) и у компании лида
не осталось, `dedup.revive_orphans` в начале каждого прогона возвращает дубль в `to_fetch` (триаж ИИ уже пройден) или `triage`. ·
`plant_pool` (v9.15: карточка слесаря КИПиА / электромонтёра / энергетика, закрытая триажем с `plant: true` — компания
ждёт в пуле канала `plant` (`lead_kind='company'`, `search_pass='plant'`), `plant.admit` выпускает по `PLANT_LEADS_PER_DAY`
в день в `to_fetch`; вторая «причина» после `duplicate_employer`, из которой строка штатно возвращается в конвейер) ·
`employer_responded:<hh_id>` (v9.3: в компанию уже откликались — сам владелец на hh.ru (`applied=1`) или кнопкой «✅ Написал»
(`lead_actions.responded`/`auto_responded`) — не позже `EMPLOYER_REPEAT_DAYS` назад; письмо уходит кадровику всей организации,
второе на тот же стол не нужно. Причина **необратимая**, в отличие от `duplicate_employer:`: `revive_orphans` такие строки
не воскрешает) · `no_email` (v9.16, решение №56: компания из каталога ОВЕН без e-mail ни в `raw_json.emails`, ни в `employers.brief.contact_email` — писать некуда; ставится стадией писем после разведки и `digest_builder.skip_unreachable` перед отправкой). `set_status` в не-skip переходах обнуляет `skip_reason`.

## Прочие поля `vacancies`
- `work_format`: remote / hybrid / office / field / unknown (приоритет remote > hybrid > office > field).
- `employment`: full / part (в т.ч. SIDE_JOB) / project / fly_in_fly_out / unknown.
- `salary_raw`: исходный объект `compensation` hh (JSON); `salary_from`, `salary_to`: рубли net (gross×0.87) **только для
  месячных RUR**; валюта/почасовые хранятся как есть (пометка `Salary.note` в БД не хранится, вычисляется из `salary_raw`).
- `lead_kind` (v9.13, `_m015`): `vacancy` (по умолчанию) / `company` — лид-компания: щитовик или проектное бюро,
  найденные по вакансии не для программиста (`search_pass` panel / design), или интегратор из каталога ОВЕН
  (`site='owen'`, `hh_id='owen:<tag_id>'` или `owen:n-<slug>` без tag_id, `employer_id` тот же ключ — под досье и дедуп,
  `url` = сайт компании, `raw_json = {description, industries, status, projects_url, site, emails, phones, address, region}`).
  Оценка у таких строк — `tech_score` = соответствие, `role_score` = 0, `lead_score`; `total = 0.6·fit + 0.4·lead`;
  `ip_gph_possible` = maybe; `offer_focus` — JSON-список кодов предложения (`schemas.OFFER_FOCUS`). Строки каталога входят
  `new` и допускаются в `prefiltered` по `COMPANY_LEADS_PER_DAY` в день (`repo.admit_company_leads`, порядок по статусу
  партнёра). Дедуп «одна компания — один лид» для `site='owen'` не действует (ключ уникален сам по себе).
- `site` (v7, `_m006`): `hh` (по умолчанию) / `profi` / `owen` (v9.13). Заказы profi.ru: `hh_id = 'profi:<номер заказа>'` — глобальный `UNIQUE`
  остаётся, коллизий с hh нет; `employer` = имя клиента, `employment = project`, `raw_json = {description, budget, when, client,
  posted, work_format, city, site}`, `salary_raw = {"profi_budget": "до 5000 ₽", from, to, …}`; статус сразу `prefiltered`.
- `source`: `search:<idx>` / `similar_to_resume` / `negotiations` / `profi` / `owen_catalog` (каталог ОВЕН); `search_pass`: regional / remote / project / **gph**
  (v8.2, поиск с фильтром hh `accept_temporary=true`; с v9.15 по умолчанию идёт только regional — `SEARCH_PASSES`) / similar /
  negotiations / profi / каналы компаний panel / design / owen_si / **plant** (v9.15: строка вакансии, перекрашенная в
  лид-компанию `plant.pool`; исходный проход при этом теряется — канал важнее).
- «Одна компания — один лид» по видам (v9.15, `repo._kind_sql`): кандидат `lead_kind='vacancy'` считается покрытым только
  строками `lead_kind='vacancy'` (и ответами по ним); кандидат-компания — любыми. Холодное предложение заводу не закрывает
  его будущую вакансию программиста.
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
`id`, `vacancy_id` (UNIQUE), `created_at` (по нему — «проверено N» в шапке и приоритет очереди); `tech_score`, `role_score`,
`lead_score` (0–100, от ИИ); `total` (код: вакансии 0.55/0.25/0.20, компании 0.6·fit + 0.4·lead — `tech_score` хранит fit,
`role_score` = 0); `salary_score`, `format_score` — устаревшие, всегда 0; `ip_gph_possible` yes/maybe/no; `is_agency`
(у компаний = `company_kind == 'agency'`); `employment_hint` — с v9.13 всегда `unknown` (staff/project только в старых
строках); `company_kind` integrator/manufacturer/end_customer/agency/unknown, у лидов-компаний ещё panel_builder/design_bureau;
`verdict`; `pitch_hint`; `red_flags` JSON; `model_note` (NULL); `floor` (0/1 — добран дневным минимумом, v9.12);
`offer_focus` JSON — что предлагать компании (v9.13).

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
| `alerts_cleaned`, `watchdog_last` | служебные | дата очистки тревог / время последней проверки сторожа |
| `last_backup` | `ISO|имя файла` | последняя резервная копия (`Scheduler.backup_job`), видна в `/status` |
| `bot_window_handle` | хендл окна | окно бота в Firefox, пока идёт сессия браузера (`browser/session.WindowRegistry`); прогон закрывает оставшееся от прерванного окно по этому хендлу и только его — страница про метку не знает (v9.11) |
| `awaiting_reason` | `<vacancy_id>:<message_id>` | бот ждёт причину 👎 своими словами ответом на своё сообщение; сторож забывает на следующий день |
| `preview_marked` | `"1"` | одноразовый флаг первого старта сервиса (`main.py`) |
| `sending_since` | ISO | идёт отправка лидов (мгновенная или дайджест); `svc.sh restart` и `/status` смотрят сюда; снимается в `finally` и при старте сервиса (v9.15) |
| `owen_last` | `ISO\|всего\|новых` | последнее чтение каталога интеграторов ОВЕН (`Scheduler.owen_job`, воскресенье 04:00) |
| `alerts_today` | — | остаток старых версий, кодом не читается и не пишется; можно удалить |

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
5. Резервная копия БД — каждую ночь в 03:40 (`db.backup`, 7 копий в `data/backups/`, kv `last_backup`).
6. Отклик владельца датируется точно: кнопка «✅ Написал» — по `lead_actions.created_at`, отклик на hh.ru — по
   первому наблюдению в `negotiation_events`; `updated_at` — только запасной вариант, и `mark_applied` его не трогает,
   если ничего не изменилось (иначе компания оставалась закрытой навсегда, v9.11).
7. Лид, полученный по `/letter <id>` из очереди, становится `sent` (`digests.note='manual:/letter'`) — с этого момента он
   закрывается кнопкой, считается в `/stats` и не приходит повторно.

## `employers` (v9.0, `_m009`)
Досье на работодателя из открытых источников — одно на компанию, переиспользуется всеми её вакансиями.
`employer_id` (PK) — hh.ru `company.id`, тот же ключ, что у «одна компания — один лид»; `name`;
`found` (0/1 — искали и не нашли тоже запоминается, чтобы не платить за пустоту дважды); `brief` — JSON
`CompanyBrief` (`what_they_do`, `industry`, `products`, `sites`, `scale`, `automation_hooks`, `sources`, `note`; с v9.15 —
`website`, `contact_email`, `contact_phone`: общие контакты компании с её сайта/страницы hh, для карточек компаний без своих
контактов);
`sources` — JSON списка прочитанных URL; `researched_at`. Свежесть — `COMPANY_RESEARCH_TTL_DAYS` (180 дней).
Заполняет `llm/company_research.py`; в карточку лида попадает строка «🏭 О компании» (`LEAD_SELECT` подмешивает
`brief` как `company_brief`). Поля-факты собраны с прочитанных страниц, `automation_hooks` — явные предположения.

## Норма суток и мгновенные отправки (v9.8)
kv `sending_since` (21.09) — момент начала мгновенной отправки после подхода; снимается в конце и при старте сервиса.
`scripts/svc.sh` и `/status` читают его, чтобы не перезапускать сервис между карточкой и письмом; `repo.repair_open_digests`
на старте досчитывает `items_count` дайджестам, чей `close_digest` не успел выполниться.
`digests.note = 'instant:<site>'` — лиды, ушедшие сразу после подхода, а не в дайджесте 12:00. Суточная норма
`DIGEST_MAX_ITEMS` (0 = нормы нет, v9.14) общая на всех: `repo.leads_sent_today` считает строки `digest_items` с полуночи по местному
времени, и мгновенная отправка и `plan_digest` берут только остаток. Другие значения `note`: `manual:/letter` (лид по `/letter` из очереди), `manual /digest` (ручной дайджест — с пробелом, считается
дневным), `preview before first service start` (первый старт сервиса). `repo.last_daily_digest` /
`evaluations_since_last_digest(daily_only=True)` пропускают `instant:*` и `manual:*`, чтобы «проверено N» в дневной шапке
означало сутки, а не время с последнего подхода.

## Что ответила компания (v9.7)
`vacancies.negotiation_state` — состояние переписки на hh.ru как его отдаёт сам сайт (`RESPONSE` — отклик без ответа,
`INTERVIEW` — пригласили, `DISCARD` — отказ), `negotiation_seen_at` — когда мы это увидели. Пишется при синхронизации
откликов (`pipeline/negotiations.sync_page` → `repo.mark_applied`): любое ненулевое состояние перезаписывает прежнее, NULL поверх
значения не пишется (`COALESCE`); порядок код не проверяет — hh сам не отзывает приглашение.
Успехом метода считается `INTERVIEW`, отказ — неудачей (решение №42). Старое поле `has_chat` («кто-то ответил»)
остаётся как более грубый сигнал. `outcomes.outcome_stats` сводит это по полосам балла (`outcomes.SCORE_BANDS`), `/stats`
показывает таблицу, шапка дайджеста — число приглашений за 14 дней. Письма, отправленные мимо hh.ru
(`applied = 0`), попадают в колонку «без канала измерения»: их исход нам не виден, и в конверсию они не идут.

## `evaluated` — это очередь (v9.1)
Статус `evaluated` с `total ≥ SCORE_THRESHOLD` означает не «ждёт ближайшего дайджеста», а «стоит в очереди».
Отправка забирает всю очередь по приоритету `total + MIN(суток ожидания, QUEUE_WAIT_BONUS_MAX)` (при `DIGEST_MAX_ITEMS` > 0 — её остаток за вычетом `repo.leads_sent_today`),
остальные **остаются `evaluated`** и соревнуются с завтрашними поступлениями (`repo.lead_queue`, `repo.queue_size`).
`reject_below` по-прежнему списывает то, что ниже порога; `repo.expire_queue` — то, что простояло дольше
`QUEUE_TTL_DAYS`. Ожидание считается от `evaluations.created_at` (строка на вакансию одна).

