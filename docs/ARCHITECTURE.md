# Архитектура HH-Scout

## Компоненты
```
┌──────────────────────────── Debian-хост, X11-сессия владельца (24/7) ────────────────────────────┐
│ Firefox ESR --marionette (127.0.0.1:2828, логин владельца на hh.ru)                              │
│        ▲ Marionette (одна сессия!)                                                                │
│ geckodriver --connect-existing  ← дочерний процесс на время серии, потом убивается (без quit())    │
│        ▲ WebDriver                                                                                 │
│ hh-scout.service = aiogram bot + APScheduler + pipeline (Selenium в потоке) → SQLite data/hh_scout.db │
│        │                                              └──► hh-scout-bridge.service :8766 → claude -p   │
│        └──► Telegram (только владелец)                                                             │
└────────────────────────────────────────────────────────────────────────────────────────────────────┘
```
Один процесс Python: aiogram polling + `AsyncIOScheduler`. Пайплайн синхронный, выполняется через `asyncio.to_thread`
со своим соединением SQLite (WAL, busy_timeout 5 с). Внутри процесса сборы сериализует `asyncio.Lock`; между процессами —
только запись `runs.status='running'`: второй процесс видит её и **сразу выходит** («уже идёт другой прогон»); зависшие
записи снимаются через 3 ч. Это не защищает от CLI, запущенного, когда `runs` уже закрыт, а сессия Marionette ещё занята.

## Один прогон (`pipeline/run.py: run_crawl`)
Общий дневной бюджет загрузок = `MAX_PAGE_LOADS_PER_RUN − page_loads_today`. Шаги изолированы: ошибка браузера
(`BrowserUnavailable`, `HHBlocked`) пропускает браузерные шаги, ошибка моста (`BridgeError`) — ИИ-шаги; остальное выполняется.

| # | Шаг | Модуль | Вход → выход | Ресурс |
|---|---|---|---|---|
| 1 | Сбор | `pipeline/collector.py` | страницы поиска → `vacancies(new)`; страница откликов → `applied/has_chat` (+`suitableVacancies` как проход `similar`) | браузер |
| 2 | Правила | `pipeline/prefilter.py` | `new → triage` или `skipped` (applied, archived, fly_in_fly_out, stopword, no_engineering_title) | — |
| 3 | Триаж ИИ | `llm/triage.py` | карточки пачками по 30 → `to_fetch` (+priority 1–3) или `skipped/triage` | мост |
| 4 | Описания | `pipeline/details.py` | `to_fetch` по приоритету → страница вакансии → `prefiltered` (архив/отклик → `skipped`; страница без `vacancyView` → `evaluation_failed`) | браузер, остаток бюджета |
| 5 | Оценка | `llm/evaluator.py` | `prefiltered` пачками по 5 → `evaluations` (tech/role/lead, verdict, pitch_hint…) → `evaluated`; total = код | мост |
| 6 | Письма | `llm/cover_letter.py` | `evaluated` с `total ≥ порог` без письма → `cover_letters` (1 вызов на лид) | мост |

Сбор: задачи = `SEARCH_QUERIES` × проходы (regional: 49 регионов одним запросом; remote: `work_format=REMOTE`;
project: `employment_form=PROJECT,PART`), в случайном порядке; пагинация до `max_pages_per_query` (6) с ранней
остановкой, когда страница ≥ 2 не даёт новых. Браузерные шаги идут **сериями** (`browser/bursts.py`): подключение →
3–7 страниц с паузами 4–12 с (каждая ~8-я 20–40 с) и прокруткой → отсоединение → пауза 10–40 мин. Между сериями
Firefox свободен. Окно бота помечено `window.name=hh-scout-bot` и закрывается при следующем подключении, если осталось.

Итог прогона — `CrawlReport.as_text()` одним сообщением владельцу; метрики в `runs`.

## Расписание (`scheduler.py`)
- **digest** — cron `DIGEST_TIME` (12:00): `send_digest()`, затем `plan_next_crawl()` — случайный момент в
  `CRAWL_WINDOW` (13:00–23:00), не раньше «сейчас + 20 мин»; если окно прошло — завтра. → `kv.next_crawl_at`, `crawl_attempts=0`.
- **crawl** — одноразовый DateTrigger: при `kv.paused` пропуск с уведомлением; иначе `run_crawl('schedule')` → отчёт.
  Если браузер был недоступен — повтор через 60–180 мин, но не позже 11:30 следующего дня, максимум 3 попытки.
- Старт сервиса: `next_crawl_at` в будущем → зарегистрировать job на **то же** сохранённое время; в прошлом → запустить
  через 2–5 мин (проверки «уже был сегодня» нет — рестарт после пропущенного времени даёт сбор); ключа нет → до ближайшего
  дайджеста сборов не будет (ручной `/crawl`).
- Любая ошибка job'а → одна строка владельцу, traceback в журнал.

## Дайджест (`pipeline/digest_builder.py`, `bot/digest.py`)
`evaluated` с `total ≥ score_threshold` по убыванию total, не больше `DIGEST_MAX_ITEMS` (20); лишние остаются `evaluated`
до следующего раза. Лидам без письма письмо дописывается перед отправкой. Сообщения: заголовок «Лиды за <дата> — N
(проверено M вакансий)» → на каждый лид карточка (`ranker.format_card`, клавиатура 👍/👎) + письмо (`format_letter`, `<pre>`),
пауза 0.6 с. Затем `sent` + `digests/digest_items`; всё `evaluated` ниже порога → `rejected`. Пусто → «Сегодня лидов не нашлось.
Проверено M новых вакансий.» M = число оценок с прошлого дайджеста. При первом старте сервиса уже показанные превью
помечаются `sent` без отправки (`kv.preview_marked`).

## Обратная связь (`bot/feedback.py`)
👍 → `feedback(+1)`, кнопка → «👍 учтено». 👎 → `feedback(-1)` → клавиатура причин (💰 зарплата · 🏢 формат ·
🔧 не мой стек · 🏷 агентство · пропустить) → `reason` → «👎 <причина>». `evaluator.feedback_block` подмешивает до 20
последних записей в промпт оценки. Дообучения нет.

## Команды бота (только `TG_OWNER_CHAT_ID`; чужие апдейты игнорируются, в лог — INFO)
`/start` `/help` — справка · `/status` — Firefox, мост, автосбор/пауза, идёт ли сбор, следующий сбор, загрузок сегодня,
последний прогон, счётчики статусов, размер БД · `/digest` — прислать лиды сейчас · `/crawl` — прогон сейчас ·
`/next` — время следующего сбора · `/pause` `/resume` · `/skipped [N≤50]` — последние отсеянные · `/letter <hh_id>` —
переписать письмо и прислать карточку с письмом.

## Обработка сбоев — см. `docs/OPERATIONS.md` (плейбук).
