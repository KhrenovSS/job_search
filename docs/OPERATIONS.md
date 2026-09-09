# Эксплуатация (runbook)

## Процессы на хосте
| Что | Как запущено | Логи |
|---|---|---|
| Firefox ESR владельца с `--marionette` | вручную/из меню (`scripts/setup_firefox.sh` правит копию `.desktop`) | — |
| Мост Claude `hh-scout-bridge` | systemd, `:8766`, слушает 0.0.0.0 (защита — токен) | `journalctl -u hh-scout-bridge` |
| Бот + планировщик `hh-scout` | systemd (`scripts/install_service.sh` рендерит `hh-scout.service.template`, sudo) | `journalctl -u hh-scout -f` |
| Ручные CLI-шаги | из venv | stdout; при `setsid nohup … > data/logs/x.log` — файл |

Логи пишутся в stdout (`logging_setup.py`); под systemd их собирает journald. Каталог `data/` (БД, логи) в .gitignore.

## CLI: ручной запуск шагов (все — из корня репо, `.venv/bin/python -m …`)
| Команда | Что делает | Браузер | Мост |
|---|---|---|---|
| `hh_scout.pipeline.run [--trigger manual] [--gap-scale X]` | **весь прогон**: collect → prefilter → triage → details → evaluate → letters | да | да |
| `hh_scout.pipeline.collector [--budget N] [--gap-scale X] [--no-gaps] [--stale-hours H]` | сбор карточек (3 прохода × запросы) + синк откликов | да | — |
| `hh_scout.pipeline.prefilter [--dry-run] [--show-skipped]` | правила: `new → triage/skipped` | — | — |
| `hh_scout.llm.triage [--limit N] [--dry-run]` | ИИ по карточкам: `triage → to_fetch/skipped` | — | да |
| `hh_scout.pipeline.details [--budget N] [--gap-scale X] [--no-gaps] [--stale-hours H]` | страницы вакансий: `to_fetch → prefiltered` | да | — |
| `hh_scout.llm.evaluator [--limit N] [--preview] [--send] [--tail]` | оценка: `prefiltered → evaluated`; `--send` шлёт превью в Telegram **напрямую, без кнопок и без записи в digests** — статусы не меняет (лиды остаются `evaluated` и попадут в следующий дайджест) | — | да |
| `hh_scout.llm.cover_letter [--limit N] [--hh-id X] [--force] [--preview]` | письма для лидов ≥ порога | — | да |
| `hh_scout.browser.hh_pages --search … [--area N]… [--remote] [--project] [--page N] [--vacancy ID]` | открыть ОДНУ страницу и распечатать разбор | да | — |
| `scripts/check_browser.py` | проверить подключение к Firefox | да (кратко) | — |
| `scripts/tg_whoami.py [--timeout 90]` | одноразово определить chat_id владельца | — | — |

Команды бота для работы с лентой: `/inbox` (открытые лиды), `/done <hh_id>`, `/cleanup [дней]` — см. ARCHITECTURE «Жизненный цикл лида».
Правильный порядок вручную = порядок в `pipeline.run`. Каждый CLI открывает своё соединение с БД и создаёт свою запись
в `runs` (`trigger=manual`); не смешивать с идущим прогоном сервиса.

## Что нельзя запускать одновременно
Marionette принимает **одну** сессию. Взаимно исключают друг друга: сервис `hh-scout` в момент сбора, `pipeline.run`,
`collector`, `details`, `hh_pages`, `check_browser.py`. Внутри сервиса защита — `asyncio.Lock`; между процессами —
только слабая проверка `runs.status='running'` (см. ниже). Перед ручным браузерным шагом: `/status` в боте
(«сбор идёт: нет») или `pgrep -f 'hh_scout.(pipeline|browser)'`.
Дайджест 12:00 и ручной `/digest` запускают оценку и письма (только мост, без браузера) на отдельном соединении — это
допустимо параллельно идущему сбору: одна и та же вакансия дважды не оценивается (статус меняется на `evaluated`).

## Дневной бюджет загрузок
`MAX_PAGE_LOADS_PER_RUN` (по умолчанию 80; имя историческое, читать как «за день») — **за календарный день
(Europe/Moscow), суммарно** по всем `runs`
(`repo.page_loads_today`), включая неудачные и ручные. Внутри прогона пул общий: сначала сбор, остаток — описания.
Что не влезло, остаётся `to_fetch` на завтра. Один сбор ≈ 15–25 загрузок поиска + до 50–60 описаний.
Стоимость ИИ (мост, opus): триаж ~$0.08 за пачку 30, оценка ~$0.10 за пачку 5, письмо ~$0.06. Дооценка перед
дайджестом — те же вызовы, что сделал бы сбор, лишних расходов не даёт.

## Плейбук сбоев
| Симптом | Причина | Что делать |
|---|---|---|
| «Уже есть незавершённый прогон» / сбор не стартует | запись `runs.status='running'` от убитого процесса | само пройдёт через 3 ч (`fail_stale_runs`), либо `sqlite3 data/hh_scout.db "update runs set status='failed', error='прерван вручную' where status='running'"` |
| `/next` → «не назначен», сбор не идёт | нет `kv.next_crawl_at`: окно 07:00–08:30 уже прошло, или сегодня прогон уже был, или пауза | штатно: после дайджеста 12:00 назначится на завтра. Нужен сбор сегодня — `/crawl`. Рестарт сервиса до окна с пустым ключом сам назначает сбор на сегодня |
| «Marionette не отвечает» | Firefox закрыт или запущен без `--marionette` | запустить Firefox из меню (или `firefox-esr --marionette &`); `check_browser.py`. Планировщик сам повторит до 3 раз через 60–180 мин, не позже 21:00 |
| «hh.ru вернул страницу без данных» (HHBlocked) | капча / просит войти | открыть hh.ru в этом Firefox руками, пройти проверку/войти; следующий прогон продолжит |
| Мост: 401 / недоступен | токены `.env` и `bridge/.env.bridge` не совпадают / сервис упал | `systemctl status hh-scout-bridge`, `curl :8766/health`; `sudo systemctl restart hh-scout-bridge` |
| Мост 502 «claude CLI exit» | CLI не авторизован под systemd | `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN=` в `bridge/.env.bridge`, рестарт |
| Пачка триажа «невалидный ответ» дважды | ИИ вернул не JSON | остаётся в `triage`, подхватится следующим прогоном |
| Лид без письма | письмо отклонено по длине (жёсткая рамка 400–2500 знаков; целевая 1000–1500 задана только промптом) | `/letter <hh_id>` или `cover_letter --hh-id X --force` |
| Окно бота осталось в Firefox | процесс убит посреди серии | закроется при следующем подключении (метка `window.name=hh-scout-bot`) |

`evaluation_failed` — терминальный статус, автоматически не повторяется (вернуть в `prefiltered` вручную при необходимости).

## Сброс и ремонт данных (SQL)
```sql
-- переоценить всё уже отправленное/оценённое новым промптом (письма останутся, будут перезаписаны при --force)
UPDATE vacancies SET status='prefiltered' WHERE status IN ('evaluated','rejected');
-- вернуть закрытые триажем на повторный триаж
UPDATE vacancies SET status='triage', skip_reason=NULL WHERE status='skipped' AND skip_reason='triage';
-- сжать WAL после больших правок
PRAGMA wal_checkpoint(TRUNCATE);
```
`kv.preview_marked=1` — одноразовый флаг первого старта сервиса: без него `main.py` пометит все текущие лиды `sent`
без отправки. Не удалять. Переоценка (`DELETE+INSERT` в `evaluations`) сбрасывает `created_at` и счётчик
«проверено с прошлого дайджеста».

## Что и где настраивать
| Хочу изменить | Где |
|---|---|
| Что считается лидом, веса важности, стиль вердикта | `prompts/vacancy_evaluation.md`, `prompts/candidate_profile.md` (владелец) |
| Что открывать по карточке | `prompts/card_triage.md` |
| Стиль/длину писем, предложение по ИП | `prompts/cover_letter.md`; факты — `prompts/resume.md` (владелец; файл в .gitignore, образец `prompts/resume.example.md`) |
| Поисковые запросы, регионы, стоп-слова, обязательные слова | `config.py`: `SEARCH_QUERIES`, `REGION_NAMES`, `TITLE_STOP_WORDS`, `TITLE_KEEP_WORDS`, `TITLE_REQUIRED_ANY` |
| Порог, веса total, размер дайджеста, лимит загрузок, паузы, окно сбора, время дайджеста | `.env` (см. `.env.example`) — все поля `Settings` переопределяемы |
| Модель Claude | `bridge/.env.bridge` `BRIDGE_MODEL` (по умолчанию для моста) или `.env` `BRIDGE_MODEL` (переопределяет на каждый запрос) |
Правило промптов: файлы с `---` — модели уходит только текст после разделителя; `candidate_profile.md` и `resume.md`
читаются целиком, включая их заголовки.

## Установка с нуля (владелец)
0. Личные файлы: `cp prompts/candidate_profile.example.md prompts/candidate_profile.md` и заполнить; `prompts/resume.md`
   из PDF-экспорта резюме hh.ru (`pdftotext -layout <файл>.pdf -`, см. `prompts/resume.example.md`). Оба в .gitignore;
   при их отсутствии код выдаёт ошибку `PrivatePromptMissing` с подсказкой.
1. `virtualenv .venv && .venv/bin/pip install -r requirements-dev.txt -e .` (python3-venv на хосте нет).
2. `bash scripts/setup_firefox.sh` → перезапустить Firefox → `.venv/bin/python scripts/check_browser.py`.
3. `bash bridge/install.sh` (sudo) → токен → `.env` `BRIDGE_TOKEN`.
4. BotFather → `TG_BOT_TOKEN` в `.env` → `.venv/bin/python scripts/tg_whoami.py` (написать боту `/start`).
5. `bash scripts/install_service.sh` (sudo) → `journalctl -u hh-scout -f`.
