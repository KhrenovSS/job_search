# HH-Scout — точка входа для ИИ-агента

## Что это
Личный сервис владельца (инженер-программист ПЛК на CODESYS/ST и SCADA на MasterSCADA 4D, работает по ИП).
Каждый день в 12:00 присылает в Telegram **лиды**: вакансии hh.ru, за которыми стоит компания, которой прямо сейчас
нужен программист ПЛК/SCADA. Вакансия — не «работа, куда идти», а повод написать компании письмо с предложением
сотрудничества по ИП. К каждому лиду бот прикладывает готовый текст отклика. Зарплата и формат работы на отбор
**не влияют** (только факты в карточке); проектирование, документация, шкафы, эксплуатация — не лиды.

hh.ru закрыл API для соискателей, поэтому бот ходит по сайту **в уже запущенном Firefox владельца** через Marionette
под его логином, «как человек»: своё окно, три подхода в день со случайным стартом в окнах 07–10 / 12–15 / 18–22,
внутри подхода серии ~7–13 мин листания с паузами ~4–9 мин, «чтение» страницы 6–20 с, дневной лимит — случайные
100–140 загрузок, поделённые между оставшимися подходами; дайджест в 12:00 дооценивает загруженное и отправляет.
**Только чтение**: никаких откликов и кликов по кнопкам сайта.

## Где мы сейчас
Актуальное состояние, история этапов и открытые дела — `docs/ROADMAP.md` (раздел «Текущее состояние»).
Почему всё устроено именно так — `docs/DECISIONS.md`. Эксплуатация и починка — `docs/OPERATIONS.md`.

## Быстрый старт для агента
**Сначала** убедитесь, что браузером никто не пользуется: Marionette держит одну сессию, поэтому `check_browser.py`,
`pipeline.run`, `collector`, `details`, `hh_pages` нельзя запускать, пока сервис `hh-scout` в сборе или идёт другой такой
процесс (`pgrep -f 'hh_scout.(pipeline|browser)'` должен быть пуст; в боте `/status` → «сбор идёт: нет»).
```bash
cd "$(git rev-parse --show-toplevel)"                 # корень репо
.venv/bin/python -m pytest -q                        # тесты (без сети, без браузера)
.venv/bin/python scripts/check_browser.py            # Firefox доступен по Marionette? (одна короткая сессия)
bash scripts/svc.sh status                           # юниты hh-scout и моста, /health, «сбор идёт: да/нет»
bash scripts/svc.sh restart                          # перезапуск без пароля (после grant_agent_control.sh); reinstall — юниты + рестарт
sqlite3 -column data/hh_scout.db "select status, count(*) from vacancies group by 1;"
journalctl -u hh-scout -n 50 --no-pager              # логи сервиса, если установлен; иначе data/logs/*.log
```
Все CLI, плейбук сбоев и настройка — `docs/OPERATIONS.md`.

## Карта репозитория
```
CLAUDE.md                      ← вы здесь
docs/  ARCHITECTURE.md (компоненты и 6 шагов пайплайна) · OPERATIONS.md (runbook) · DATABASE.md (схема, статусы, kv)
       INTEGRATIONS.md (страницы hh.ru, мост, Telegram) · DECISIONS.md (почему) · ROADMAP.md (история, состояние)
       archive/README.md      описание архива; сама папка archive/2026-09-08-initial-vision/ (старые доки, PDF резюме,
                              переписка с примером письма) — только локально, в .gitignore
prompts/  candidate_profile.md  профиль кандидата — правит ВЛАДЕЛЕЦ, читается целиком при каждом прогоне; в .gitignore
          resume.md             резюме (из PDF) — правит владелец; источник фактов для писем; в .gitignore
          *.example.md          публичные образцы этих двух файлов (скопировать и заполнить на новой машине)
          card_triage.md · vacancy_evaluation.md · cover_letter.md  системные промпты (часть после `---`)
bridge/   hh_scout_bridge.py  мост Claude: FastAPI → `claude -p` под подпиской; hh-scout-bridge.service.template, install.sh, .env.bridge(.example)
scripts/  install_service.sh (sudo) · install_geckodriver.sh · setup_firefox.sh · check_browser.py · tg_whoami.py
          · tg_alert.sh (Telegram через curl для systemd OnFailure)
          · svc.sh (status/logs/start/stop/restart/reinstall/bridge-restart; не рестартует во время сбора) · grant_agent_control.sh
          (владелец, один раз: sudoers-правило из sudoers-hh-scout.template → рестарты без пароля, в т.ч. агентом)
README.md · .gitignore · hh-scout.service.template + hh-scout-alert.service.template (юниты, рендерит install_service.sh) · pyproject.toml · requirements(-dev).txt
· pytest.ini · .env.example · LICENSE (MIT)
src/hh_scout/
  config.py        Settings из .env (порог, веса, лимиты, окна, ритм, токены) + константы: SEARCH_QUERIES, REGION_NAMES (49),
                   TITLE_STOP/KEEP/REQUIRED_ANY;  logging_setup.py — логи в stdout/journald
  db.py            SQLite, миграции _m001…_m005 (PRAGMA user_version), kv_get/kv_set
  main.py          сервис: aiogram polling + планировщик; первый старт помечает превью как sent
  scheduler.py     дайджест по cron, три подхода в день (окна, случайный старт, доля лимита), восстановление из kv,
                   сторож каждые 30 мин и предпроверка перед подходом;  health.py — тревоги (чистые проверки + Alerter)
  browser/         session.py (geckodriver --connect-existing, своё окно, бюджет) · hh_pages.py (URL, парсеры
                   HH-Lux-InitialState) · pacing.py (паузы, длительность серий, прокрутка) · bursts.py (серии по времени)
  hh/              areas.py (регионы из открытого api.hh.ru/areas, кэш) · salary.py (gross→net, только RUR/месяц)
  llm/             bridge_client.py · prompts.py (сборка промптов) · schemas.py · triage.py (карточки → открывать?)
                   evaluator.py (лид: техника/роль/лид) · cover_letter.py (письмо на лид)
  pipeline/        repo.py (весь SQL) · budget.py (дневной лимит) · collector.py · prefilter.py · details.py · ranker.py (total, карточка)
                   digest_builder.py · run.py (оркестратор одного прогона)
  bot/             app.py (только владелец) · handlers.py (команды: /start=/help /status /digest /crawl [N] /next /pause /resume
                   /skipped /letter /inbox /done /cleanup) · digest.py · feedback.py (кнопки 👍/👎/✅/⏸) · lead_actions.py
                   (сворачивание карточек, автозакрытие по откликам) · keyboards.py
tests/             unit-тесты (`pytest -q`, без сети и браузера); фикстуры — реальные страницы hh.ru и справочник регионов
data/              hh_scout.db (WAL), logs/ — в .gitignore
```

## Статусная машина вакансии (канон — `docs/DATABASE.md`)
`new → triage → to_fetch → prefiltered → evaluated → sent | rejected`, ветки `skipped` (с `skip_reason`) и `evaluation_failed`.
Шаги: сбор (new) → правила (triage/skipped) → триаж ИИ по карточкам (to_fetch/skipped) → страница вакансии (prefiltered)
→ оценка ИИ (evaluated) → письмо → дайджест (sent; ниже порога — rejected).

## Правила работы
1. Этапы и критерии — `docs/ROADMAP.md`. После изменений: сводка владельцу, как проверить руками, обновить доки.
2. **Браузер — только чтение.** Никаких кликов по элементам сайта, откликов, сообщений.
3. **Человекоподобие обязательно**: паузы, серии по времени, случайный порядок, только дневные окна, дневной лимит
   `DAILY_PAGE_LOADS_MIN..MAX` (100–140, случайный на день, kv `daily_cap:<дата>`) **суммарно по всем процессам**.
   `--no-gaps`/`--gap-scale` — только для отладки, не для ежедневной работы.
4. Одна Marionette-сессия: не запускать CLI с браузером параллельно сервису.
5. Данные — из JSON `HH-Lux-InitialState`; изменилась структура → warning и пропуск, прогон не падает.
6. Ответы ИИ валидируются pydantic; невалидно → один ретрай → `evaluation_failed`/пачка остаётся в `triage`.
7. Итоговый балл считает код: `0.55·tech + 0.25·role + 0.20·lead`, порог 60. ИИ даёт подоценки. Веса и порог —
   поля `Settings` (переопределяются в `.env`: `SCORE_THRESHOLD`, `WEIGHT_*`), значения по умолчанию в `config.py`.
8. Промпты: в файлах с разделителем `---` модели уходит только часть после него; `candidate_profile.md` и `resume.md`
   читаются целиком — не добавлять в них `---`.
9. Секреты только в `.env` / `bridge/.env.bridge` (в .gitignore). Комментарии в .env — отдельными строками.
10. Код — английский; сообщения бота и логи INFO — русский. Тесты не ходят в сеть и браузер.

## Где лежит истина
Формат карточки и письма — `pipeline/ranker.py`. Расписание — `scheduler.py`. Запросы/регионы/стоп-слова — `config.py`;
порог, веса, лимиты, окна подходов, ритм серий, время дайджеста — `.env` (`.env.example` перечисляет всё). Что считается лидом —
`prompts/vacancy_evaluation.md` + `prompts/candidate_profile.md`. Схема БД — `db.py`, перечисления — `docs/DATABASE.md`.
