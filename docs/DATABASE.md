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
`_m015_lead_kind` (`vacancies.lead_kind` vacancy/company, `evaluations.offer_focus` — решение №53), `_m016_employer_contacts`
(`employer_contacts` — решение №64), `_m017_area_path` (`vacancies.area_path` — `area.path` hh.ru «.113.225.2114.131.», бэкфилл из
`raw_json.area`; закрытые регионы, решение №65).
Время — TEXT ISO-8601 UTC; границы для сравнения строятся через `repo.iso_utc` (строка с `+03:00` рядом с `+00:00`
сравнивается как текст и сдвигает окно на три часа).
Почти весь SQL — в `src/hh_scout/pipeline/repo.py` (свои запросы держат ещё миграции `db.py`, `llm/evaluator.py`, `llm/company_research.py`, `pipeline/budget.py`, `pipeline/plant.py`, `pipeline/dedup.py` (`revive_orphans`), `sources/trudvsem.py`, `sources/zakupki.py`, `hh/areas.py`, `health.py`, `bot/feedback.py`, `bot/handlers.py`); аналитика над строками (исходы, полосы, зрелость) — в `pipeline/outcomes.py`.

| Таблица | Назначение |
|---|---|
| `areas_cache` | регионы России из открытого `api.hh.ru/areas` (обновляется, когда пуст или старше 30 дней — `hh/areas.resolve_region_ids`; нужен только при `SEARCH_ALL_RUSSIA=false`, закрытые регионы решения №65 читаются из `area_path` без него) |
| `vacancies` | все увиденные вакансии; `hh_id` UNIQUE = «уже видели» |
| `evaluations` | одна оценка на вакансию (UNIQUE `vacancy_id`); при переоценке строка пересоздаётся |
| `cover_letters` | текст отклика (UNIQUE `vacancy_id`), `model_note`, `created_at` (время **последней** записи; `repo.letters_written_today` по нему — только для отчёта, объём писем держит `LETTERS_BUDGET_MIN`, v9.14), `rules_hash` — отпечаток правил, по которым письмо написано (промпт письма + профиль + резюме + для hh и компаний (`REVIEWED_KEYS`) ещё чеклист редактора + правила кода `llm/letter_checks.CODE_RULES` + рамка длины; для profi — только промпт; `llm.cover_letter.rules_hash`). NULL или чужой отпечаток = письмо устарело и переписывается перед отправкой (решение №50). `owner_hint` — пожелание из `/letter <id> …`, переживает переписку; `with_dossier` 0/1 — было ли досье компании при написании: письмо без досье устаревает, когда досье появилось (v9.11) |
| `negotiation_events` | история переписки: `vacancy_id`, `state`, `has_messages`, `seen_at`. Строка добавляется **при смене состояния или флага `has_messages`** (`repo.record_negotiation`); `state` NULL — карточка из списка откликов без состояния (`mark_applied` без `state`, из сбора); засев из снимка в `_m012` берёт только строки с состоянием (`seen_at` = `COALESCE(negotiation_seen_at, updated_at)`). Синхронизация в каждом подходе ничего не раздувает. Отсюда берутся время до ответа и переходы `RESPONSE → INTERVIEW/DISCARD` (решение №48) |
| `digests`, `digest_items` | отправленные подборки: `sent_at`, `items_count` (лидов ушло), `collected_count` («проверено N»), `note` (NULL — плановый дайджест 12:00; остальные значения — ниже); в `digest_items` — `position`, `tg_message_id` карточки, `letter_message_id` письма (для сворачивания) |
| `lead_actions` | действия владельца по отправленному лиду: `action` liked / disliked / responded / auto_responded / deferred / closed_stale, `reason`, `created_at`. Лид открыт, пока нет responded/auto_responded/disliked/closed_stale |
| `feedback` | 👍/👎: `value` ±1, `reason` — код salary/format/stack/agency, NULL или **текст владельца** («✍️ своими словами», до 300 знаков, v9.11); текст попадает в калибровочный блок оценщика дословно. 👍 пишет и сюда, и `lead_actions.liked`; «✅ Написал» тоже пишет +1 |
| `runs` | прогоны: `trigger` schedule/manual, `status` running/ok/failed, метрики collected/prefiltered/evaluated/bridge_calls/page_loads, `error`; колонка `sent` не используется |
| `employers` | досье на компанию из открытых источников (веб-разведка, v9.0) — свой раздел ниже |
| `employer_contacts` | e-mail и домены организации — ключ «одна компания — один лид» помимо `employer_id` и имени (v9.22, `_m016`) — свой раздел ниже |
| `kv` | флаги, см. ниже |

## `vacancies.status` — 9 значений
| Статус | Кто ставит | Смысл |
|---|---|---|
| `new` | collector · каталог ОВЕН (`repo.insert_integrator`) · «Работа России» сверх `TRUDVSEM_PER_RUN` (`trudvsem.store_vacancy(admit=False)`: правила уже пройдены, ждёт `admit_waiting`) · закупки (`zakupki.store_notice`: извещение ждёт победителя) | карточка собрана, правила не применялись (у hh); у портала и закупок — ждёт допуска / контракта |
| `triage` | prefilter · `dedup.revive_orphans` (дубль без пройденного триажа) · `prefilter --requeue-reason` | прошла правила, ждёт триаж ИИ |
| `to_fetch` | triage · `plant.admit` (из пула, приоритет 3) · `dedup.revive_orphans` (дубль, чей «победитель» лидом не стал) | ИИ велел открыть страницу; `triage_priority` 1–3 |
| `prefiltered` | details · `repo.admit_company_leads` (каталог ОВЕН: `new` → сразу сюда, страницы нет) · profi (заказ вставляется сразу в этом статусе) · «Работа России» (`trudvsem.store_vacancy` при вставке или `admit_waiting` из `new`) · закупки (`zakupki.resolve_row`, когда найден победитель) · `repo.admit_plant_leads` для строки пула с уже прочитанной страницей (v9.19) · `dedup.revive_orphans` для дублей без страницы hh (owen / trudvsem / zakupki) · `evaluator --requeue-rejected` / `--requeue-id` (оценка удаляется, строка возвращается сюда) | страница загружена (или описание есть из другого источника), `raw_json` заполнен, ждёт оценку |
| `evaluated` | evaluator | оценена, строка в `evaluations`, кандидат в дайджест |
| `sent` | `repo.add_digest_item`: дайджест 12:00, мгновенная отправка после подхода, `/letter` из очереди (или помечена при первом старте сервиса — `kv.preview_marked`); открыт/закрыт лид — по `lead_actions` |
| `rejected` | digest | была `evaluated`, `total < score_threshold` на момент дайджеста (строки с `evaluations.floor = 1` не списываются); либо простояла в очереди дольше `QUEUE_TTL_DAYS` — тогда с `skip_reason = queue_expired`. Обратимо (v9.12): дневной минимум (`digest_builder.promote_floor`) и `evaluator --readmit` возвращают `rejected` без `skip_reason` в `evaluated` |
| `skipped` | prefilter/collector/triage/details/dedup/digest · оценка (`foreign_platform_only`, v9.20; `defense:evaluation`, v9.33) · оборонка на любом шаге (`defense:*`, v9.33, решение №72) · синхронизация откликов (`mark_applied` → `applied`, в т.ч. заглушки для незнакомых вакансий) · `plant.pool` / `plant.pool_evaluated` (`plant_pool`) · письма (`no_email`) · «Работа России» при вставке (правила карточки, `no_email`, дубль компании) · закупки (`tender:no_contract`, `tender:no_supplier`, дубль компании) | отсеяна; всегда с `skip_reason` |
| `evaluation_failed` | evaluator (ИИ дважды вернул невалидный ответ / пропустил hh_id) или details (страница без `vacancyView`) | терминальная ошибка, автоматически не повторяется (вернуть вручную; `repo.reset_catalogue_rows` для строк ОВЕН вызывается только из тестов) |

Лиды выше порога, к которым ещё не написано письмо (бюджет `LETTERS_BUDGET_MIN` кончился, мост молчал), остаются `evaluated` до следующей отправки.

## `vacancies.skip_reason`
`applied` (владелец уже откликался) · `archived` · `region:<регион>` (v9.23, решение №65: Крым с Севастополем, ДНР, ЛНР,
Запорожская и Херсонская области — `config.BLOCKED_REGIONS` по id региона в `vacancies.area_path`, а не по названию города:
«Донецк (Ростовская область)» проходит; ставится правилами на `new`, стадией описаний, если путь появился только на странице,
и `repo.skip_blocked_regions` на каждом прогоне правил — снимает такие строки `site='hh'` с любой стадии до письма и из пула `plant_pool`,
а списанным по баллу `rejected` дописывает причину, чтобы дневной минимум их не поднял; `sent` не трогает. У «Работы России»
`area_path` нет — регион узнаётся по названию из API при вставке, `trudvsem.blocked_region_name`) · `stopword:<слово>` · `no_engineering_title`
(нет инженерного слова в названии — самый частый; правила карточки те же и для строк портала при вставке, `prefilter.decide`) · `triage` (ИИ: не открывать) · `invalid_ai_answer` /
`missing_in_ai_answer` (оценка) · `no_vacancy_view` (страница без данных: нет `vacancyView` или, с v9.34, нет `HH-Lux-InitialState` при заголовке окна вакансии) · `redirect:<хост>` (v9.35, статус `skipped`: hh.ru перенаправил страницу вакансии на другой сайт — так hh показывает вакансии «Работы России», `…?utm_redirect_vacancy_id=<hh_id>` на trudvsem.ru; своей страницы у вакансии нет, то же объявление приходит строкой `trudvsem` своим каналом) · `low_priority_expired` (приоритет 3 триажа не открыт за `LOW_PRIORITY_TTL_DAYS`) · `queue_expired` (v9.1: лид простоял в очереди дольше `QUEUE_TTL_DAYS` — до него так и не дошла суточная норма) ·
`duplicate_employer:<hh_id>` (v8: у компании уже есть лид `<hh_id>` — отправленный за последние `EMPLOYER_REPEAT_DAYS` или ждущий
дайджест; ставится на `triage` (шаг 2b), `to_fetch` (перед загрузкой страницы) и `evaluated` (после оценки), см. `pipeline/dedup.py`;
с v9.22 «компания» — это ещё и общий e-mail или домен сайта (`employer_contacts`), поэтому `<hh_id>` бывает `owen:<tag>`, а
причина ставится и строкам каталога ОВЕН — при допуске (`run.py` 4b), после оценки и стадией писем после разведки; строкам «Работы России» — при вставке и при `admit_waiting`, закупкам — при найденном победителе (`skip_if_covered`). **Обратимая причина** (v8.7; штатно возвращается ещё `plant_pool`, а любую причину вернёт `prefilter --requeue-reason`): если `<hh_id>` так и не стал лидом (отклонён, сорвалась оценка или оценён ниже порога) и у компании лида
не осталось, `dedup.revive_orphans` на шаге 2b каждого прогона (после сбора и правил) возвращает дубль в `to_fetch` (триаж ИИ уже пройден) или `triage`; строку каталога ОВЕН, портала или закупки — сразу в `prefiltered` (страницы hh у неё нет). ·
`plant_pool` (v9.15: карточка слесаря КИПиА / электромонтёра / энергетика, закрытая триажем с `plant: true`, а с v9.19 —
и вакансия, оценённая ниже порога с `plant: true` в оценке (`plant.pool_evaluated`: её `evaluations` удаляется, `raw_json`
остаётся, и `admit` выпускает её сразу в `prefiltered`) — компания
ждёт в пуле канала `plant` (`lead_kind='company'`, `search_pass='plant'`), `plant.admit` выпускает по `PLANT_LEADS_PER_DAY`
в день в `to_fetch`; вторая «причина» после `duplicate_employer`, из которой строка штатно возвращается в конвейер) ·
`employer_responded:<hh_id>` (v9.3: в компанию уже откликались — сам владелец на hh.ru (`applied=1`) или кнопкой «✅ Написал»
(`lead_actions.responded`/`auto_responded`) — не позже `EMPLOYER_REPEAT_DAYS` назад; письмо уходит кадровику всей организации,
второе на тот же стол не нужно. Причина **необратимая**, в отличие от `duplicate_employer:`: `revive_orphans` такие строки
не воскрешает) · `no_email` (v9.16, решение №56: строка, которой пишут по e-mail (`rows.needs_email` — компания из каталога ОВЕН, вакансия «Работы России» (v9.25), победитель закупки (v9.26)), без адреса ни в `raw_json.emails`, ни в `employers.brief.contact_email` — писать некуда; ставится стадией писем после разведки (`CoverLetterWriter.unreachable`), `digest_builder.skip_unreachable` перед отправкой, а строкам портала — сразу при вставке) · `tender:no_contract` / `tender:no_supplier` (v9.26, `sources/zakupki.resolve_row`: извещение закупки, у которого контракт не появился за `ZAKUPKI_MAX_TRIES` запусков; строка, у которой после карточки контракта нет ни ИНН, ни имени поставщика — карточка без блока «Информация о поставщиках» сама по себе причиной не становится: победитель берётся по имени из таблицы результатов и обычно уходит в `no_email`). `set_status` пишет ту причину, которую ему передали: у `evaluation_failed` это `invalid_ai_answer` / `missing_in_ai_answer` / `no_vacancy_view`, у `rejected` по сроку очереди — `queue_expired`. · `foreign_platform_only` (v9.20, решение №61: единственная обязательная среда вакансии — чужая (Siemens/TIA, Allen-Bradley, Omron, Mitsubishi, B&R …), ставится на шаге оценки `evaluator._lock_out` по флагу `foreign_platform_only` в ответе ИИ независимо от баллов — только вакансиям (`letter_key == 'hh'`: hh и «Работа России»), компаниям и заказам profi нет; строка `evaluations` остаётся, в очередь, дневной минимум и `--requeue-rejected` не возвращается) · **`defense:<источник>`** (v9.33, решение №72: оборонное предприятие или структура холдинга с оборонным крылом — не лид, все источники кроме profi; после двоеточия — кто поставил: `name:<слово>` — правило кодом по названию работодателя (`pipeline/defense.match`, словарь `config.DEFENSE_*`; `prefilter.decide`, `dedup.skip_if_defense`, вставка строк «Работы России» и каталога ОВЕН, победитель закупки), `triage` — флаг `TriageVerdict.defense`, `evaluation` — флаг `defense_enterprise` оценки вакансии или компании (`evaluator._lock_out`, строка `evaluations` остаётся), `dossier` — поле `CompanyBrief.defense` (`CoverLetterWriter.defense`, `digest_builder.skip_defense`), `customer` — заказчик закупки из словаря (`zakupki.store_notice` / `resolve_row`, карточка ЕИС не грузится), `employer:<hh_id>` — другая строка той же компании уже помечена (`repo.defense_employer`, развёртка `repo.skip_defense_employers` на стадии правил: живые статусы и `plant_pool` → `skipped`, `rejected` без причины — только причина, `sent` не трогается). Откат — `prefilter --requeue-reason 'defense%' --days N` (`%` включает LIKE))

## Прочие поля `vacancies`
- `work_format`: remote / hybrid / office / field / unknown (приоритет remote > hybrid > office > field).
- `employment`: full / part (в т.ч. SIDE_JOB) / project / fly_in_fly_out / unknown.
- `salary_raw`: исходный объект `compensation` hh (JSON); `salary_from`, `salary_to`: рубли net (gross×0.87) **только для
  месячных RUR**; валюта/почасовые хранятся как есть (пометка `Salary.note` в БД не хранится, вычисляется из `salary_raw`).
- `lead_kind` (v9.13, `_m015`): `vacancy` (по умолчанию) / `company` — лид-компания: щитовик или проектное бюро,
  найденные по вакансии не для программиста (`search_pass` panel / design), эксплуатант автоматики из пула (`plant`,
  `repo.move_to_plant_pool`), или интегратор из каталога ОВЕН
  (`site='owen'`, `hh_id='owen:<tag_id>'` или `owen:n-<slug>` без tag_id, `employer_id` тот же ключ — под досье и дедуп,
  `url` = сайт компании, `raw_json = {description, industries, status, projects_url, site, emails, phones, address, region}`).
  Оценка у таких строк — `tech_score` = соответствие, `role_score` = 0, `lead_score`; `total = 0.6·fit + 0.4·lead`;
  `ip_gph_possible` = maybe; `offer_focus` — JSON-список кодов предложения (`schemas.OFFER_FOCUS`). Строки каталога входят
  `new` и допускаются в `prefiltered` по `COMPANY_LEADS_PER_DAY` в день (`repo.admit_company_leads`, порядок по статусу
  партнёра). По `employer_id` и имени строка каталога с другими сайтами не сравнивается (`repo.NAMED_SITES` — hh, trudvsem,
  zakupki), но с v9.22 та же фирма на hh.ru находится через общий e-mail / домен (`employer_contacts`, `linked_ids`), а среди
  оценённых `dedupe_evaluated` берёт и каталог.
- `site` (v7, `_m006`): `hh` (по умолчанию) / `profi` / `owen` (v9.13) / `trudvsem` (v9.25: `hh_id='tv:<uuid>'`, `employer_id='tv:<ОГРН|ИНН|companycode>'` (NULL, если у компании нет ни одного кода), `search_pass='regional'`, `area_path` NULL — регион словами в `area_name`, `raw_json = {site, description (HTML из обязанностей/требований), keySkills, workExperience, emails, phones, contact_person, inn, ogrn, region, city, company_url, hr_agency, specialisation, employment, schedule, addresses, modified_at, created_at}`, `salary_raw = {from, to, RUR, gross: null, MONTH}`, `accept_temporary`/`civil_law_contracts` всегда 0/NULL, статус сразу `prefiltered` или `skipped/<правило|no_email|duplicate_employer|employer_responded>`, сверх `TRUDVSEM_PER_RUN` за подход — `new` до допуска `trudvsem.admit_waiting`; с v9.40 (решение №75) строки по `TRUDVSEM_COMPANY_QUERIES` («сборщик щитов»…) — лиды-компании: `lead_kind='company'`, `search_pass='panel'`, правило заголовка не применяется, свой гейт `TRUDVSEM_COMPANY_PER_RUN`, `admit_waiting(lead_kind=…)` держит два бюджета) / `zakupki` (v9.26: лид-компания, `hh_id='zk:<номер извещения 44-ФЗ>'`, `title='Закупка: <предмет>'`, `search_pass='tender'`, `employment='project'`, `status='new'` пока победитель не найден (`raw_json.tries` — сколько запусков смотрели), затем `employer` (короткое имя поставщика), `employer_id='zk:<ИНН>'` (без ИНН — `zk:<имя в нижнем регистре без пробелов и знаков, до 40 символов>`), `area_name` (город из адреса) из карточки контракта и `prefiltered`; `raw_json = {site, law, reg_number, notice_type, object, customer, price, stage, ikz, query, tries, description, winner, winner_short, inn, kpp, emails, phones, address, supplier_status, contract_subject, contract_price, contract_signed, contract_deadline, contract_reestr, contract_url}`; без контракта — `skipped/tender:no_contract`, без имени и ИНН поставщика — `skipped/tender:no_supplier`; карточка без блока поставщиков даёт лид по имени из таблицы результатов, без e-mail). Заказы profi.ru: `hh_id = 'profi:<номер заказа>'` — глобальный `UNIQUE`
  остаётся, коллизий с hh нет; `employer` = имя клиента, `employment = project`, `raw_json = {description, budget, when, client,
  posted, work_format, city, site}`, `salary_raw = {"profi_budget": "до 5000 ₽", from, to, …}`; статус сразу `prefiltered`.
- `source`: `search:<idx>` / `similar_to_resume` / `negotiations` / `profi` / `owen_catalog` (каталог ОВЕН) / `trudvsem:<запрос>` (v9.25; с v9.40 и запросы щитовиков) / `zakupki:<фраза>` (v9.26); `search_pass`: regional / remote / project / **gph** (у строк `trudvsem` — `regional`, у щитовиков с портала — `panel`, v9.40)
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
  closedForApplicants, userLabels, civilLawContracts + company{id,name,visibleName,@trusted,companySiteUrl} (сайт — с v9.22, для `employer_contacts`), address{city,street,building,displayName}).
- `applied`, `has_chat`: из страницы откликов; `triage_priority`, `triage_note`: от триажа.
- `employer_id` (v8, `_m007`): hh.ru `company.id` строкой — ключ «одна компания — один лид»; пишется из карточки поиска и со
  страницы вакансии, для старых строк заполнен из `raw_json.company.id`; у карточек до v8 и у profi.ru — NULL (тогда сравнение
  по `employer` через SQL-функцию `casefold`, зарегистрированную в `db.connect`). Индексы: `idx_vacancies_employer_id`, `idx_vacancies_employer`, `idx_vacancies_status`, `idx_vacancies_site_status` (`_m006`), `idx_areas_name`, UNIQUE `idx_eval_vacancy`, `idx_lead_actions_vacancy`, `ix_negotiation_events(vacancy_id, seen_at)` (`_m012`), `idx_employer_contacts_value(kind, value)` (`_m016`).

## `evaluations`
`id`, `vacancy_id` (UNIQUE), `created_at` (по нему — «проверено N» в шапке и приоритет очереди); `tech_score`, `role_score`,
`lead_score` (0–100, от ИИ); `total` (код: вакансии 0.55/0.25/0.20, компании 0.6·fit + 0.4·lead — `tech_score` хранит fit,
`role_score` = 0); `salary_score`, `format_score` — устаревшие, всегда 0; `ip_gph_possible` yes/maybe/no; `is_agency`
(у компаний = `company_kind == 'agency'`); `employment_hint` — с v9.13 всегда `unknown` (staff/project только в старых
строках); `company_kind` panel_builder/design_bureau/integrator/manufacturer/end_customer/agency/unknown (с v9.38 щитовик и бюро — и у вакансий; `config.KIND_PRIORITY_BONUS` добавляет им очки в порядке очереди `repo.PRIORITY_SQL`, решение №74);
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
| `sitting_done` | `YYYY-MM-DD:idx` | последнее окно, в котором подход сегодня уже стартовал или был пропущен (пауза, идущий ручной сбор, окно уже закрылось); по нему планировщик не назначает то же окно дважды |
| `daily_cap:<YYYY-MM-DD>` | число | лимит загрузок на день (случайный из `DAILY_PAGE_LOADS_MIN..MAX`); старые ключи удаляются |
| `alert:<key>:<YYYY-MM-DD>` | ISO | тревога уже отправлена сегодня (`health.Alerter`); старые дни чистятся |
| `alerts_cleaned`, `watchdog_last` | служебные | дата очистки тревог / время последней проверки сторожа |
| `last_backup` | `ISO|имя файла` | последняя резервная копия (`Scheduler.backup_job`), видна в `/status` |
| `bot_window_handle` | хендл окна | окно бота в Firefox, пока идёт сессия браузера (`browser/session.WindowRegistry`); прогон закрывает оставшееся от прерванного окно по этому хендлу и только его — страница про метку не знает (v9.11) |
| `awaiting_reason` | `<vacancy_id>:<message_id>` | бот ждёт причину 👎 своими словами ответом на своё сообщение; сторож забывает на следующий день |
| `preview_marked` | `"1"` | одноразовый флаг первого старта сервиса (`main.py`) |
| `sending_since` | ISO | идёт отправка лидов после подхода (`after_crawl`: автозакрытие и мгновенная отправка; дайджест 12:00 и `/digest` его не ставят); `svc.sh restart` и `/status` смотрят сюда; снимается в `finally` и при старте сервиса (v9.15) |
| `owen_last` | `ISO\|всего\|новых` | последнее чтение каталога интеграторов ОВЕН (`Scheduler.owen_job`, воскресенье 04:00) |
| `trudvsem_last` | `ISO\|в выдаче\|новых` | последняя удачная синхронизация «Работы России» (v9.25); следующая читает с этого момента минус 2 дня |
| `zakupki_last` | `ISO\|извещений\|новых\|победителей` | последний удачный запуск канала `tender` (`sources/zakupki.run_job`, v9.26) — из ежедневного `Scheduler.zakupki_job` (v9.29) или CLI `python -m hh_scout.sources.zakupki` |
| `alerts_today` | — | остаток старых версий, кодом не читается и не пишется; можно удалить |

## Инварианты
1. Вакансия попадает в дайджест не более одного раза (UNIQUE `hh_id` + статусы `sent`/`rejected`).
1a. Компания получает не больше одного лида за `EMPLOYER_REPEAT_DAYS` (90; 0 — навсегда): остальные её вакансии —
   `skipped/duplicate_employer:<hh_id>`. Компания — это hh `employer_id`, имя (карточки без id) **или общий e-mail / домен
   сайта** (`employer_contacts`, v9.22, решение №64): так встречаются строка каталога ОВЕН и та же фирма на hh.ru, в обе стороны.
   Вакансия и лид-компания одного работодателя — разные каналы и могут сосуществовать
   (`repo._kind_sql`, `dedup.same_company`, v9.15) — и через общий адрес тоже. profi.ru не дедуплицируется (`employer` там — имя клиента).
1b. Компания, куда владелец уже откликнулся, за те же `EMPLOYER_REPEAT_DAYS` не получает ни лида, ни письма:
   её вакансии — `skipped/employer_responded:<hh_id>` (v9.3). Проверка стоит раньше 1а (в `dedupe_evaluated` — после поиска близнеца среди оценённых) — она верна и тогда, когда
   лида у компании не было вовсе (отклик отправлен прямо на hh.ru).
2. `applied=1` никогда не отправляется.
3. Перезапуск после сбоя продолжает с места остановки: статусы фиксируются после каждого шага.
4. «Проверено N» в заголовке = число строк `evaluations`, созданных после последнего полуденного дайджеста (`instant:*` и `manual:*` не в счёт).
5. Резервная копия БД — каждую ночь в 03:40 (`db.backup`, 7 копий в `data/backups/`, kv `last_backup`).
6. Отклик владельца датируется точно: кнопка «✅ Написал» — по `lead_actions.created_at`, отклик на hh.ru — по
   первому наблюдению в `negotiation_events`; `updated_at` — только запасной вариант, и `mark_applied` его не трогает,
   если ничего не изменилось (иначе компания оставалась закрытой навсегда, v9.11).
7. Лид, полученный по `/letter <id>` из очереди, становится `sent` (`digests.note='manual:/letter'`) — с этого момента он
   закрывается кнопкой, считается в `/stats` и не приходит повторно.

## `employers` (v9.0, `_m009`)
Досье на работодателя из открытых источников — одно на компанию, переиспользуется всеми её вакансиями.
`employer_id` (PK) — hh.ru `company.id`, тот же ключ, что у «одна компания — один лид» (у строк каталога ОВЕН — `owen:<…>`, у «Работы России» — `tv:<…>`, у победителей закупок — `zk:<ИНН>` (v9.30); profi не разведывается — `company_research.for_row` берёт `site` hh / owen / trudvsem / zakupki с непустым `employer_id`); `name`;
`found` (0/1 — искали и не нашли тоже запоминается, чтобы не платить за пустоту дважды); `brief` — JSON
`CompanyBrief` (`found`, `what_they_do`, `industry`, `products`, `sites`, `scale`, `automation_hooks`, `sources`, `note`; с v9.15 —
`website`, `contact_email`, `contact_phone`: общие контакты компании с её сайта/страницы hh, для карточек компаний без своих
контактов);
`sources` — JSON списка прочитанных URL; `researched_at`. Свежесть — `COMPANY_RESEARCH_TTL_DAYS` (180 дней).
Заполняет `llm/company_research.py`; в карточку лида попадает строка «🏭 О компании» (`LEAD_SELECT` подмешивает
`brief` как `company_brief` только при `found = 1`). Поля-факты собраны с прочитанных страниц, `automation_hooks` — явные предположения.

## `employer_contacts` (v9.22, `_m016`)
Контакты организации — то, по чему «одна компания — один лид» узнаёт фирму помимо hh `employer_id` и имени (решение №64).
`employer_id` (hh `company.id`, `owen:<tag>`, `tv:<ОГРН|ИНН|companycode>` или `zk:<ИНН>`), `kind` (`email` | `domain`), `value` (нормализованный адрес или хост без `www.`),
`updated_at`; PK `(employer_id, kind, value)`, индекс `idx_employer_contacts_value (kind, value)`. Пишет `repo.record_contacts` из
трёх источников v9.22: запись каталога ОВЕН (`raw_json.emails`, `raw_json.site` — при `insert_integrator`), досье (`brief.contact_email`,
`brief.website` — при `company_research.save`, только `found = 1`), страница вакансии hh (`vacancyView.company.companySiteUrl` — при
`save_details`; `trim_vacancy_view` теперь оставляет это поле); с v9.25/v9.26 ещё e-mail вакансии «Работы России» (`trudvsem.store_vacancy`,
ключ `tv:<…>`) и e-mail поставщика из карточки контракта (`zakupki.resolve_row`, ключ `zk:<ИНН>`). Хосты бесплатной почты, соцсетей, реестров, hh.ru и owen.ru (`contacts.SHARED_DOMAINS`)
доменом не записываются — такой адрес идентифицирует только сам себя (как `email` он всё равно хранится). `repo.linked_employer_ids`
даёт другие `employer_id` с тем же ключом — один переход (A–B по e-mail, B–C по домену ≠ A–C); `employer_lead` / `employer_responded`
принимают их как `linked_ids` и добавляют `OR v.employer_id IN (…)` без ограничения `site = 'hh'`. Строки без `employer_id`
(карточки до v8) в связи не участвуют. Бэкфилл миграции — каталог и досье; hh-строки получают домен при следующей загрузке страницы.

## Норма суток и мгновенные отправки (v9.8)
kv `sending_since` (21.09) — момент начала мгновенной отправки после подхода; снимается в конце и при старте сервиса.
`scripts/svc.sh` и `/status` читают его, чтобы не перезапускать сервис между карточкой и письмом; `repo.repair_open_digests`
на старте досчитывает `items_count` дайджестам, чей `close_digest` не успел выполниться.
`digests.note = 'instant:<site>'` — лиды, ушедшие сразу после подхода, а не в дайджесте 12:00. Суточная норма
`DIGEST_MAX_ITEMS` (0 = нормы нет, v9.14) общая на всех: `repo.leads_sent_today` считает строки `digest_items` с полуночи по местному
времени, и мгновенная отправка и `plan_digest` берут только остаток. Другие значения `note`: `manual:/letter` (лид по `/letter` из очереди), `manual /digest` (ручной дайджест — с пробелом, считается
дневным), `preview before first service start` (первый старт сервиса); в базе есть ещё два старых значения от 14.09 («ручной дайджест по просьбе владельца», «очередь по просьбе владельца»), код их больше не пишет. `repo._last_daily_digest` /
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
Статус `evaluated` с `total ≥ SCORE_THRESHOLD` или `evaluations.floor = 1` (`repo.IN_QUEUE_SQL`) означает не «ждёт ближайшего дайджеста», а «стоит в очереди».
Отправка забирает всю очередь по приоритету `total + MIN(суток ожидания, QUEUE_WAIT_BONUS_MAX)` (при `DIGEST_MAX_ITEMS` > 0 — её остаток за вычетом `repo.leads_sent_today`),
остальные **остаются `evaluated`** и соревнуются с завтрашними поступлениями (`repo.lead_queue`, `repo.queue_size`).
`reject_below` по-прежнему списывает то, что ниже порога; `repo.expire_queue` — то, что простояло дольше
`QUEUE_TTL_DAYS`. Ожидание считается от `evaluations.created_at` (строка на вакансию одна).

