# Схема БД (SQLite)

Файл `DB_PATH` (по умолчанию `data/hh_scout.db`, WAL). Миграции — список функций в `db.py`, версия через
`PRAGMA user_version`; применённые не редактировать, только добавлять: `_m001_initial` (таблицы),
`_m002_triage_columns` (`vacancies.triage_priority`, `triage_note`), `_m003_lead_scoring` (`evaluations.role_score`,
`lead_score`, `company_kind`, `pitch_hint`), `_m004_cover_letters`, `_m005_lead_actions` (`lead_actions`,
`digest_items.letter_message_id`). Время — TEXT ISO-8601 UTC.
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
| `skipped` | prefilter/collector/triage/details | отсеяна; всегда с `skip_reason` |
| `evaluation_failed` | evaluator (ИИ дважды вернул невалидный ответ / пропустил hh_id) или details (страница без `vacancyView`) | терминальная ошибка, не повторяется |

Лиды выше порога, не влезшие в `DIGEST_MAX_ITEMS` (20), остаются `evaluated` до следующего дайджеста.

## `vacancies.skip_reason`
`applied` (владелец уже откликался) · `archived` · `fly_in_fly_out` (вахта) · `stopword:<слово>` · `no_engineering_title`
(нет инженерного слова в названии — самый частый) · `triage` (ИИ: не открывать) · `invalid_ai_answer` /
`missing_in_ai_answer` (оценка) · `no_vacancy_view` (страница без данных). `set_status` в не-skip переходах обнуляет `skip_reason`.

## Прочие поля `vacancies`
- `work_format`: remote / hybrid / office / field / unknown (приоритет remote > hybrid > office > field).
- `employment`: full / part (в т.ч. SIDE_JOB) / project / fly_in_fly_out / unknown.
- `salary_raw`: исходный объект `compensation` hh (JSON); `salary_from`, `salary_to`: рубли net (gross×0.87) **только для
  месячных RUR**; валюта/почасовые хранятся как есть (пометка `Salary.note` в БД не хранится, вычисляется из `salary_raw`).
- `source`: `search:<idx>` / `similar_to_resume` / `negotiations`; `search_pass`: regional / remote / project / similar / negotiations.
- `raw_json`: **урезанный** `vacancyView` (`repo.DETAIL_KEYS`: description, keySkills, compensation, workFormats,
  employmentForm, area, status, publicationDate, workExperience, company{id,name,visibleName,@trusted}, address{city,…}).
- `applied`, `has_chat`: из страницы откликов; `triage_priority`, `triage_note`: от триажа.

## `evaluations`
`tech_score`, `role_score`, `lead_score` (0–100, от ИИ); `total` (код: 0.55/0.25/0.20); `salary_score`, `format_score` —
устаревшие, всегда 0; `ip_gph_possible` yes/maybe/no; `is_agency`; `employment_hint` staff/project/unknown;
`company_kind` integrator/manufacturer/end_customer/agency/unknown; `verdict`; `pitch_hint`; `red_flags` JSON; `model_note` (NULL).

## `kv` — 4 ключа
| Ключ | Значение | Кто |
|---|---|---|
| `paused` | `"1"` или отсутствует | `/pause`, `/resume`; глушит только плановые сборы, дайджест идёт |
| `next_crawl_at` | ISO с зоной | планировщик; нет ключа = сбор не назначен до следующего дайджеста |
| `crawl_attempts` | число | счётчик повторов при недоступном браузере, максимум 3 |
| `preview_marked` | `"1"` | одноразовый флаг первого старта сервиса (`main.py`) |

## Инварианты
1. Вакансия попадает в дайджест не более одного раза (UNIQUE `hh_id` + статусы `sent`/`rejected`).
2. `applied=1` никогда не отправляется.
3. Перезапуск после сбоя продолжает с места остановки: статусы фиксируются после каждого шага.
4. «Проверено N» в заголовке = число строк `evaluations`, созданных после последнего дайджеста.
5. Резервное копирование БД **не реализовано** (этап 7).
