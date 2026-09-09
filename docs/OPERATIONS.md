# Эксплуатация (runbook)

## Процессы на хосте
| Что | Как запущено | Логи |
|---|---|---|
| Firefox ESR владельца с `--marionette` | вручную/из меню (`scripts/setup_firefox.sh` правит копию `.desktop`) | — |
| Мост Claude `hh-scout-bridge` | systemd, `:8766`, слушает 0.0.0.0 (защита — токен) | `journalctl -u hh-scout-bridge` |
| Бот + планировщик `hh-scout` | systemd (`scripts/install_service.sh` рендерит `hh-scout.service.template`, sudo) | `journalctl -u hh-scout -f` |
| Управление сервисом | `bash scripts/svc.sh status\|logs [N]\|start\|stop\|restart\|reinstall\|bridge-restart` — без пароля после `grant_agent_control.sh` (ниже) | — |
| Ручные CLI-шаги | из venv | stdout; при `setsid nohup … > data/logs/x.log` — файл |

Логи пишутся в stdout (`logging_setup.py`); под systemd их собирает journald. Каталог `data/` (БД, логи) в .gitignore.

## CLI: ручной запуск шагов (все — из корня репо, `.venv/bin/python -m …`)
| Команда | Что делает | Браузер | Мост |
|---|---|---|---|
| `hh_scout.pipeline.run [--trigger manual] [--budget N] [--gap-scale X]` | **весь прогон**: collect → prefilter → triage → details → evaluate → letters | да | да |
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
Лимит дня — случайное число из `DAILY_PAGE_LOADS_MIN..MAX` (100–140), тянется один раз в день (`pipeline/budget.daily_cap`,
kv `daily_cap:<дата>`) — **за календарный день (Europe/Moscow), суммарно** по всем `runs` (`repo.page_loads_today`),
включая неудачные и ручные. Плановый подход получает долю: остаток ÷ подходы, оставшиеся сегодня (утро ≈ ⅓, день ≈ ½
остатка, вечер — всё). Внутри прогона пул общий: сначала сбор (в следующих подходах — 9–10 страниц: новые карточки
только сверху), остаток — описания. Что не влезло, остаётся `to_fetch` на следующий подход; карточки приоритета 3 старше
`LOW_PRIORITY_TTL_DAYS` (3) списываются (`skipped/low_priority_expired`). Ручной `/crawl [N]` и CLI берут весь остаток дня
(`/crawl 40`, `--budget N` — ограничить явно). Ритм: `BURST_MINUTES` (7–13) листаем / `GAP_MINUTES` (4–9) тишина; 120 загрузок ≈ 3 часа
листания за день с учётом пауз, разбитые на три подхода по ~1 часу.
Стоимость ИИ (мост, opus): триаж ~$0.08 за пачку 30, оценка ~$0.10 за пачку 5, письмо ~$0.06. Дооценка перед
дайджестом — те же вызовы, что сделал бы сбор, лишних расходов не даёт.

## Тревоги в Telegram (что значит и что делать)
| Сообщение | Причина | Действие |
|---|---|---|
| ⚠️ Окно … прошло без подхода | планировщик не стартовал сбор в окне (сервис лежал, план был на другой день, ошибка) | `/status`, `journalctl -u hh-scout -n 50`; если план пуст — сторож уже перепланировал |
| ⚠️ Подход не был назначен — назначаю | `next_crawl_at` пуст вне сбора | ничего; повторяется — ошибка планировщика, смотреть журнал |
| 🚨 За весь день ни одной загрузки | все окна прошли, `page_loads_today=0` | Firefox? пауза? журнал |
| ⚠️ Прогон висит в статусе running | процесс сбора убит | само снимется через 3 ч (`fail_stale_runs`) |
| 🦊 Подход в HH:MM: Firefox не отвечает | предпроверка за 30 мин | запустить Firefox с `--marionette` |
| 🦊 Браузер недоступен | подход начался, а Marionette не ответил (`BrowserUnavailable` в отчёте прогона) | запустить Firefox с `--marionette`, `check_browser.py`; следующий подход придёт сам |
| 🤖 Подход в HH:MM: мост недоступен / Мост Claude не отвечал | `hh-scout-bridge` лежит | `bash scripts/svc.sh bridge-restart` |
| 🚫 hh.ru не отдал данные | капча / просит войти | открыть hh.ru в этом Firefox руками; повторяется — снизить лимит, удлинить паузы |
| 👤 hh.ru видит нас не как соискателя | вылетел логин | войти на hh.ru в Firefox |
| 🧩 Поиск без карточек / страницы без данных | hh.ru изменил структуру `HH-Lux-InitialState` | обновить парсеры `browser/hh_pages.py` (фикстуры в tests/) |
| 📊/⚠️ Итог дня | сводка после 22:30 | ⚠️ = подходов меньше, чем окон |
| 🚨 hh-scout: сервис упал и не смог перезапуститься | 5 падений за 10 мин (systemd OnFailure) | `journalctl -u hh-scout -n 50`, починить, `bash scripts/svc.sh restart` |

Каждая тревога приходит не чаще раза в день. Юнит тревоги устанавливает `scripts/install_service.sh` (повторный запуск
скрипта безопасен); проверить: `systemctl cat hh-scout | grep OnFailure`, вручную — `scripts/tg_alert.sh "тест"`.

## Управление сервисом без пароля (агент)
Владелец один раз запускает `bash scripts/grant_agent_control.sh` (спросит пароль sudo). Скрипт рендерит
`scripts/sudoers-hh-scout.template` (пользователь, путь репо), проверяет его `visudo -cf`, ставит в `/etc/sudoers.d/hh-scout`
(0440), прогоняет `visudo -c` и при ошибке откатывает, затем проверяет `sudo -n`. После этого пользователю (и ИИ-агенту под
ним) без пароля разрешены **только**: `systemctl start|stop|restart|reset-failed|enable hh-scout`, `restart|start|stop
hh-scout-bridge`, `daemon-reload` и `install` двух отрендеренных юнитов из `data/` в `/etc/systemd/system/`. По сути это
root без пароля для этого пользователя (юнит запускается от root) — приемлемо на личной машине, где он и так в группе
`sudo`. Отозвать: `sudo rm /etc/sudoers.d/hh-scout`; посмотреть: `sudo -l | grep hh-scout`.

Работать через обёртку `bash scripts/svc.sh …`: `status` (юниты, `/health`, «сбор идёт»), `logs [N]`, `start`, `stop`,
`restart`, `reinstall` (= `install_service.sh` + рестарт: новые юниты, зависимости), `bridge-restart`. `stop|restart|reinstall`
**отказывают (код 3), пока идёт сбор** — есть `runs.status='running'` или процесс `hh_scout.(pipeline|browser)`;
обход `--force` только если прогон точно мёртв. Без правила sudoers — код 4 с подсказкой.

## Плейбук сбоев
| Симптом | Причина | Что делать |
|---|---|---|
| «Уже есть незавершённый прогон» / сбор не стартует | запись `runs.status='running'` от убитого процесса | само пройдёт через 3 ч (`fail_stale_runs`), либо `sqlite3 data/hh_scout.db "update runs set status='failed', error='прерван вручную' where status='running'"` |
| `/next` → «Сбор идёт сейчас; следующий подход назначится после него», а сбор не идёт | `next_crawl_at` пуст, а прогон упал до `plan_next_crawl` (редко) | сторож перепланирует сам в течение 30 мин (тревога «Подход не был назначен»); быстрее — `bash scripts/svc.sh restart` или `/crawl` |
| «Marionette не отвечает» | Firefox закрыт или запущен без `--marionette` | запустить Firefox из меню (или `firefox-esr --marionette &`); `check_browser.py`. Ретраев нет — следующий подход придёт сам через несколько часов (время в отчёте и `/next`) |
| «hh.ru вернул страницу без данных» (HHBlocked) | капча / просит войти | открыть hh.ru в этом Firefox руками, пройти проверку/войти; следующий подход продолжит. Повторяется — снизить `DAILY_PAGE_LOADS_*` и/или удлинить `GAP_MINUTES` |
| Мост: 401 / недоступен | токены `.env` и `bridge/.env.bridge` не совпадают / сервис упал | `systemctl status hh-scout-bridge`, `curl :8766/health`; `bash scripts/svc.sh bridge-restart` |
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
0. Личные файлы: `cp .env.example .env` (заполнять по шагам 3–4); `cp prompts/candidate_profile.example.md prompts/candidate_profile.md` и заполнить; `prompts/resume.md`
   из PDF-экспорта резюме hh.ru (`pdftotext -layout <файл>.pdf -`, см. `prompts/resume.example.md`). Оба в .gitignore;
   при их отсутствии код выдаёт ошибку `PrivatePromptMissing` с подсказкой.
1. `virtualenv .venv && .venv/bin/pip install -r requirements-dev.txt -e .` (python3-venv на хосте нет).
2. `bash scripts/setup_firefox.sh` → перезапустить Firefox → `.venv/bin/python scripts/check_browser.py`.
3. `bash bridge/install.sh` (sudo) → токен → `.env` `BRIDGE_TOKEN`.
4. BotFather → `TG_BOT_TOKEN` в `.env` → `.venv/bin/python scripts/tg_whoami.py` (написать боту `/start`).
5. `bash scripts/install_service.sh` (sudo) → `journalctl -u hh-scout -f`.
6. По желанию: `bash scripts/grant_agent_control.sh` (sudo один раз) — дальше рестарты и переустановка юнитов через
   `bash scripts/svc.sh …` без пароля, в том числе ИИ-агентом.
