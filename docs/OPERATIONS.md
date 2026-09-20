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
| `scripts/profi_snapshot.py` | сохранить HTML уже открытых вкладок profi.ru из Firefox в `data/profi_snapshot_*.html` (ничего не загружает; для разбора разметки) | да (только чтение вкладок) | — |
| `hh_scout.llm.triage [--limit N] [--dry-run]` | ИИ по карточкам: `triage → to_fetch/skipped` | — | да |
| `hh_scout.pipeline.details [--budget N] [--gap-scale X] [--no-gaps] [--stale-hours H]` | страницы вакансий: `to_fetch → prefiltered` | да | — |
| `hh_scout.llm.company_research (--employer-id ID \| --hh-id ID) [--force] [--preview]` | досье на компанию: CLI моста с веб-инструментами читает страницу работодателя на hh.ru и сайт компании → таблица `employers` (v9.0). Кэш `COMPANY_RESEARCH_TTL_DAYS` (180 дн.), `--force` игнорирует его. **Браузер не нужен и дневной лимит загрузок не тратится** — страницы читает CLI | — | да |
| `hh_scout.llm.evaluator [--limit N] [--preview] [--send] [--tail] [--requeue-rejected [--min-total N]] [--requeue-id HH_ID …]` | оценка: `prefiltered → evaluated`; `--send` шлёт превью в Telegram **напрямую, без кнопок и без записи в digests** — статусы не меняет (лиды остаются `evaluated` и попадут в следующий дайджест). `--requeue-rejected` (v8.7) возвращает на переоценку всё, что оценено и лидом не стало, с баллом ≥ `--min-total` (45) — и `rejected`, и `evaluated` ниже порога (в первом статусе вакансия оказывается, только если с тех пор был дайджест: списывает их `reject_below`); `--requeue-id` (v8.8, можно повторять) — названные вакансии в любом статусе. Лиды (`sent`, `evaluated ≥ порога`) не трогаются никогда: описания уже в `raw_json`, поэтому нужен только мост — браузер и лимит загрузок не трогаются. Ручная операция после правки промпта оценки; `--requeue-id` удобен, чтобы проверить правку на известном примере | — | да |
| `hh_scout.llm.cover_letter [--limit N] [--hh-id X] [--force] [--preview]` | письма для лидов ≥ порога | — | да |
| `hh_scout.browser.hh_pages --search … [--area N]… [--remote] [--project] [--page N] [--vacancy ID]` | открыть ОДНУ страницу и распечатать разбор | да | — |
| `scripts/check_browser.py` | проверить подключение к Firefox | да (кратко) | — |
| `scripts/tg_whoami.py [--timeout 90]` | одноразово определить chat_id владельца | — | — |
| `hh_scout.pipeline.collector --pass panel\|design --period 30` | стартовый проход одного канала компаний за 30 дней (v9.13; без синхронизации откликов, 5–20 страниц из лимита) | да | — |
| `hh_scout.sources.owen [--admit N] [--file integrators.json]` | прочитать каталог интеграторов ОВЕН в базу и допустить N компаний в оценку (по умолчанию `COMPANY_LEADS_PER_DAY`) | — | — |
| `bash go_hh.sh [ЧЧ:ММ \| YYYY-MM-DD]` | разбор последнего подхода одной командой: состояние сервиса, прогоны за день, отправки за сутки, лиды 50–59 с письмами, итог суток, стадии и предупреждения из журнала. Только чтение — можно и во время сбора | — | — |

Команды бота для работы с лентой: `/inbox` (открытые лиды), `/done <hh_id>`, `/cleanup [дней]` — см. ARCHITECTURE «Жизненный цикл лида».
Правильный порядок вручную = порядок в `pipeline.run`. Каждый CLI открывает своё соединение с БД и создаёт свою запись
в `runs` (`trigger=manual`); не смешивать с идущим прогоном сервиса.

## Что нельзя запускать одновременно
Marionette принимает **одну** сессию. Взаимно исключают друг друга: сервис `hh-scout` в момент сбора, `pipeline.run`,
`collector`, `details`, `hh_pages`, `check_browser.py`, `profi_snapshot.py`, `sync_negotiations.py`.
Внутри сервиса защита — `asyncio.Lock`; между процессами — проверка `svc.sh crawl_running()`: она смотрит и
`runs.status='running'` (плановый подход идёт **внутри** процесса сервиса, снаружи его не видно), и запущенные
браузерные CLI по `$BROWSER_CMD_RE`. Перед ручным браузерным шагом: `/status` в боте или
`bash scripts/svc.sh status` («сбор идёт: нет»).
**Не проверяйте это самодельным `pgrep -f 'hh_scout...'`**: такой шаблон ловит вашу же командную строку, если
в ней просто упомянут модуль (так 19.09 `svc.sh` отказал в рестарте, когда сбора не было), и при этом не видит
скрипты вроде `sync_negotiations.py`, где этой подстроки нет. В `svc.sh` шаблон сужен до реальных запусков
питона, а собственный процесс и его предки исключаются по PID.
Дайджест 12:00 и ручной `/digest` запускают оценку и письма (только мост, без браузера) на отдельном соединении, но
**не во время подхода**: пока `crawl_lock` занят, дооценка пропускается, подход пришлёт лиды сам; сами отправки
(дайджест и мгновенные) идут под одним замком (v9.11).
Загрузки **любого** процесса считаются в дневном лимите: `sync_negotiations.py` и `hh_pages` тоже оставляют строку в
`runs` и отказываются стартовать при идущем сборе.

## Дневной бюджет загрузок
Лимит дня — случайное число из `DAILY_PAGE_LOADS_MIN..MAX` (150–200), тянется один раз в день (`pipeline/budget.daily_cap`,
kv `daily_cap:<дата>`) — **за календарный день (Europe/Moscow), суммарно** по всем `runs` (`repo.page_loads_today`),
включая неудачные и ручные. Плановый подход получает долю: остаток ÷ подходы, оставшиеся сегодня (утро ≈ ⅓, день ≈ ½
остатка, вечер — всё). Внутри прогона пул делится: доля `DETAILS_BUDGET_SHARE` (0.55) **резервируется под страницы вакансий** —
сбор получает `бюджет − резерв` (в следующих подходах ему хватает: новые карточки только сверху, 9–10 страниц),
описания получают резерв плюс всё, что сбор не потратил. Резерв — пол, а не потолок; если открывать нечего,
он просто не тратится и переходит в следующий подход. Без него широкий поиск съедал весь подход и описания
не качались вовсе (13.09, прогон #14: сбор 15 из 15, «Описаний к загрузке: 0»). Что не влезло, остаётся `to_fetch` на следующий подход; карточки приоритета 3 старше
`LOW_PRIORITY_TTL_DAYS` (3) списываются (`skipped/low_priority_expired`). **Очередь лидов** (v9.1): оценённые выше порога складываются в очередь, мгновенные отправки и дайджест берут сверху остаток суточной нормы `DIGEST_MAX_ITEMS` (20) по приоритету «балл + сутки ожидания, не больше `QUEUE_WAIT_BONUS_MAX`=7»; остальные ждут и видны в хвосте дайджеста (`DIGEST_TAIL_ITEMS`=8, письмо по `/letter <id>` — после него лид считается отправленным), а через `QUEUE_TTL_DAYS`=30 выбывают (`rejected/queue_expired`). Письма пишутся только для суточной нормы — не на каждый прогон, а на день. **Одна компания — один лид** (v8): вакансии работодателя,
у которого уже есть лид (отправлен за `EMPLOYER_REPEAT_DAYS`=90 дней или ждёт дайджест), пропускаются как `duplicate_employer:<hh_id>`
до триажа и до загрузки страницы; среди оценённых остаётся лучшая по баллу. `/skipped` показывает их с причиной.
Если «победитель» в итоге не стал лидом (отклонён или сорвалась оценка), дубли возвращаются в очередь в начале следующего прогона (v8.7) — компания не выпадает молча; в отчёте `/crawl` это строка «дублей вернулось: N». Ручной `/crawl [N]` и CLI берут весь остаток дня
(`/crawl 40`, `--budget N` — ограничить явно). Ритм: `BURST_MINUTES` (7–13) листаем / `GAP_MINUTES` (4–9) тишина; 120 загрузок ≈ 3 часа
листания за день с учётом пауз, разбитые на три подхода по ~1 часу.
Стоимость ИИ (мост, opus): триаж ~$0.08 за пачку 30, оценка ~$0.10 за пачку 5, письмо ~$0.06. Дооценка перед
дайджестом — те же вызовы, что сделал бы сбор, лишних расходов не даёт.

## Тревоги в Telegram (что значит и что делать)
**Штатная работа — молча** (с 10.09): старт и конец планового подхода, пропуск подхода на паузе или из-за закрывшегося окна,
рестарт сервиса — только в журнале. Отчёт подхода приходит владельцу лишь когда он не `ok` и ни одна тревога ниже его не
объяснила (одно событие — одно сообщение). Единственный ежедневный отчёт — дайджест 12:00: в его заголовке строка «Работа за
сутки: подходов N · страниц P» (за 24 ч, `repo.work_totals`). Ручной `/crawl` отвечает как раньше: «▶️ Начинаю сбор…» и отчёт.
Нет дайджеста в 12:00 — это уже тревога сама по себе.

| Сообщение | Причина | Действие |
|---|---|---|
| ⚠️ Окно … прошло без подхода | планировщик не стартовал сбор в окне (сервис лежал, план был на другой день, ошибка) | `/status`, `journalctl -u hh-scout -n 50`; если план пуст — сторож уже перепланировал |
| ⚠️ Подход не был назначен — назначаю | `next_crawl_at` пуст вне сбора | ничего; повторяется — ошибка планировщика, смотреть журнал |
| 🚨 За весь день ни одной загрузки | все окна прошли, `page_loads_today=0` | Firefox? пауза? журнал |
| ⚠️ Прогон висит в статусе running | процесс сбора убит | само снимется через 3 ч (`fail_stale_runs`); плановый — сразу при старте сервиса |
| 🦊 Подход в HH:MM: Firefox не отвечает | предпроверка за 30 мин | запустить Firefox с `--marionette` |
| 🦊 Браузер недоступен | подход начался, а Marionette не ответил (`BrowserUnavailable` в отчёте прогона) | запустить Firefox с `--marionette`, `check_browser.py`; следующий подход придёт сам |
| 🚫 profi.ru: … | лента заказов без кабинета: вышли из аккаунта, капча или сменилась разметка (`ProfiBlocked`) | открыть profi.ru в этом Firefox и войти; повторяется при входе — снять снимок `scripts/profi_snapshot.py`, сравнить с `tests/fixtures/profi_orders.html`, обновить `profi/pages.py` |
| 🤖 Подход в HH:MM: мост недоступен / Мост Claude не отвечал | `hh-scout-bridge` лежит | `bash scripts/svc.sh bridge-restart` |
| 🚫 hh.ru не отдал данные | капча / просит войти (`CrawlReport.blocked`; подход остановлен на первой же такой странице, описания не открывались) | открыть hh.ru в этом Firefox руками, пройти проверку; следующий подход упрётся в неё снова после одной страницы, пока не пройдена; повторяется — снизить лимит, удлинить паузы |
| 👤 hh.ru видит нас не как соискателя | вылетел логин | войти на hh.ru в Firefox |
| 🧩 Поиск без карточек / страницы без данных | hh.ru изменил структуру `HH-Lux-InitialState` | обновить парсеры `browser/hh_pages.py` (фикстуры в tests/) |
| ⚠️ Итог дня: подходов k из n | после 22:30 подходов было меньше, чем окон (полный день не сообщается) | `/status`, журнал; причины пропусков — тревоги выше |
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
| «Уже есть незавершённый прогон» / сбор не стартует | запись `runs.status='running'` от убитого процесса | плановый прогон снимается при старте сервиса, ручной — через 3 ч (`fail_stale_runs`), либо `sqlite3 data/hh_scout.db "update runs set status='failed', error='прерван вручную' where status='running'"` |
| Сервис перезапущен ночью, а утром подход «потерян» | пропущенный вечерний подход не догоняется вне окна (v9.11) | ничего: следующий подход в ближайшем окне; проверить `/next` |
| `/next` → «Сбор идёт сейчас; следующий подход назначится после него», а сбор не идёт | `next_crawl_at` пуст, а прогон упал до `plan_next_crawl` (редко) | сторож перепланирует сам в течение 30 мин (тревога «Подход не был назначен»); быстрее — `bash scripts/svc.sh restart` или `/crawl` |
| «Marionette не отвечает» | Firefox закрыт или запущен без `--marionette` | запустить Firefox из меню (или `firefox-esr --marionette &`); `check_browser.py`. Ретраев нет — следующий подход придёт сам через несколько часов (время в отчёте и `/next`) |
| «hh.ru вернул страницу без данных» (HHBlocked) | капча / просит войти | открыть hh.ru в этом Firefox руками, пройти проверку/войти; следующий подход продолжит. Повторяется — снизить `DAILY_PAGE_LOADS_*` и/или удлинить `GAP_MINUTES` |
| Мост: 401 / недоступен | токены `.env` и `bridge/.env.bridge` не совпадают / сервис упал | `systemctl status hh-scout-bridge`, `curl :8766/health`; `bash scripts/svc.sh bridge-restart` |
| Мост 502 «claude CLI exit» | CLI не авторизован под systemd | `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN=` в `bridge/.env.bridge`, рестарт |
| Пачка триажа «невалидный ответ» дважды | ИИ вернул не JSON | остаётся в `triage`, подхватится следующим прогоном |
| Лид без письма | письмо дважды не прошло проверки кодом: длина 400–3600, суммы (в т.ч. без валюты рядом с «ставка/вилка/бюджет», `300k`, проценты), штампы, трёхстрочная подпись, названо ли производство (`llm/letter_checks.py`); ответ редактора проверяется так же | `/letter <hh_id>` или `cover_letter --hh-id X --force`; причина — в журнале «Письмо для … отклонено» |
| После правки промпта/резюме/правил кода письма очереди переписываются и съедают норму | `rules_hash` включает и правила кода (`letter_checks.CODE_RULES`), устаревшие письма идут после новых | штатно; норма `DIGEST_MAX_ITEMS` общая на письма и лиды |
| Нужно вернуть карточки, отсеянные отменённым правилом | правило отменено, карточки остались `skipped` | `.venv/bin/python -m hh_scout.pipeline.prefilter --requeue-reason fly_in_fly_out --days 14` (только свежие; браузер не нужен) |
| «⚠️ Сторож расписания упал: database is locked» / `database is locked` в журнале | два соединения одного процесса (бот и поток прогона) столкнулись на записи дольше `busy_timeout` 5 с; до v8.1 префильтр коммитил каждую из сотен карточек отдельно (~90 мс fsync на HDD) и морил голодом соединение бота | с v8.1 пакетные записи идут одной транзакцией (`db.transaction`), а отметка сторожа при коллизии — только warning в журнале. Если повторяется — `bash scripts/svc.sh incidents`, искать долгую запись рядом по времени |
| Окно бота осталось в Firefox | процесс убит посреди серии | закроется следующим прогоном по хендлу из kv `bot_window_handle` (страница про окно не знает; `check_browser.py` чужие окна не трогает) |
| `profi.ru: лента недоступна (WebDriverException)` в отчёте, hh.ru работает | сайт не резолвится или недоступен. Проверить: `getent hosts profi.ru` (пусто = DNS) и `dig +short @1.1.1.1 profi.ru` (отвечает = виноват DNS провайдера/роутера) | прогон из-за этого **не падает** (v8.6) — hh.ru идёт как обычно. Починить DNS на хосте (нужен root): `sudo nmcli con mod "Wired connection 1" ipv4.ignore-auto-dns yes ipv4.dns "1.1.1.1 8.8.8.8"`, затем `sudo nmcli con up "Wired connection 1"`; проверка — `getent hosts profi.ru`. Не нужен profi.ru — `PROFI_ENABLED=false` в `.env` |
| Дайджест пуст несколько дней подряд, при этом «за день» заметно меньше лимита | периметр поиска вычерпан: сбор обрывает задачи по «страница без новых → дальше всё известное». Проверить: `select date(first_seen_at), count(*) from vacancies group by 1 order by 1 desc limit 7;` — приток упал; в журнале «карточек 393, новых 26» | расширять периметр, а не смягчать порог: запросы в `SEARCH_QUERIES`, `SEARCH_ALL_RUSSIA`, проходы. Сначала убедиться, что отсев верен: `.venv/bin/python -m hh_scout.pipeline.prefilter --dry-run --show-skipped` и `select skip_reason, count(*) …` |

`evaluation_failed` — терминальный статус, автоматически не повторяется (вернуть в `prefiltered` вручную при необходимости).

### Разбор инцидента постфактум (вечером, после дня без присмотра)
Всё пишется в journald (персистентный, `/var/log/journal`, переживает перезагрузку) на уровне INFO и выше; исключения —
с трассировкой (`log.exception` в прогоне, стороже, дайджесте, мгновенной отправке); тревоги дублируются в Telegram и
помечаются в kv `alert:<ключ>:<дата>` (по одной в день на причину); падение процесса ловит systemd
(`OnFailure=hh-scout-alert.service` → 🚨 в Telegram). Одной командой:
```bash
bash scripts/svc.sh incidents              # сегодня
bash scripts/svc.sh incidents 2026-09-10   # конкретный день
```
Печатает: предупреждения/ошибки `hh-scout` и моста за день, трассировки, старты/остановки сервиса, прогоны дня из `runs`
(статус `ok` / `failed` + текст ошибки, страницы, собрано, оценено), отправленные тревоги (из журнала: `hh_scout.health: Тревога …`;
kv `alert:*` хранит только текущий день), загрузки/лимит, упавшие юниты.
Читать вручную: `journalctl -u hh-scout --since today -p warning`, `journalctl -u hh-scout --since '2026-09-10 08:50' --until '2026-09-10 10:40'`
(один подход), `sqlite3 -column data/hh_scout.db "select * from runs order by id desc limit 5;"`. Статусы `runs`: `ok` — прогон
дошёл до конца (замечания в `error`), `failed` — браузер/мост/исключение, `running` старше 3 ч — процесс был убит (снимет сторож).

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
| Каналы лидов-компаний, порция каталога в день, веса балла компании | `.env`: `COMPANY_CHANNELS` (panel,design,owen_si; пусто — выключить все), `COMPANY_LEADS_PER_DAY` (5), `WEIGHT_COMPANY_FIT`/`WEIGHT_COMPANY_LEAD` (0.6/0.4); что предлагать — `prompts/company_offer.md`, кого считать компанией-партнёром — `prompts/company_triage.md`, `prompts/company_evaluation.md` |
| Дневной минимум писем и его границы | `.env`: `DAILY_LETTERS_FLOOR` (5, 0 — выключить), `FLOOR_MIN_TOTAL` (40), `FLOOR_MIN_ROLE` (40), `FLOOR_LOOKBACK_DAYS` (3). После снижения порога вернуть недавние списанные: `python -m hh_scout.llm.evaluator --readmit --min-total 50 --days 3` (без переоценки и без браузера) |
| Порог (50 с 20.09), веса total, размер дайджеста, лимит загрузок, паузы (в т.ч. `LONG_READ_*`), окно сбора, время дайджеста | `.env` (см. `.env.example`) — все поля `Settings` переопределяемы; паузы меньше 3 с и доля резерва вне 0…1 отвергаются при старте. Таймауты загрузки страницы (`PAGE_LOAD_TIMEOUT_S` 15, `MARKUP_WAIT_S` 20) — константы `browser/session.py` |
| Модель Claude | `bridge/.env.bridge` `BRIDGE_MODEL` (по умолчанию для моста) или `.env` `BRIDGE_MODEL` (переопределяет на каждый запрос) |
| Модель разведки по компании | `.env` `COMPANY_RESEARCH_MODEL` — пусто = модель моста (opus). Глубина: `COMPANY_RESEARCH_MAX_TURNS` (8 ходов CLI) и потолок 6 обращений к сети в `prompts/company_research.md`; ретраев у разведки нет намеренно. Разведка читает hh.ru с того же IP без cookies — риск принят владельцем 20.09 |
| Заказы с profi.ru: включить/выключить, адрес ленты, страниц за подход | `.env`: `PROFI_ENABLED`, `PROFI_ORDERS_URL`, `PROFI_PAGES_PER_RUN`; критерии — `prompts/profi_order_evaluation.md`, текст предложения — `prompts/profi_bid.md` |
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
