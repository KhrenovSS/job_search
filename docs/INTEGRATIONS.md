# Интеграции

## 1. hh.ru — через сайт в Firefox владельца

### Почему не API
Соискательский API hh.ru закрыт с 15.12.2025; с 2026 `GET api.hh.ru/vacancies` без ключа работодателя → 403
(проверено 2026-09-08). Живыми остались открытые справочники: `GET https://api.hh.ru/areas`,
`GET https://api.hh.ru/dictionaries` (без токена, с заголовком `HH-User-Agent`).

### Как читаем страницы
Каждая страница hh.ru содержит `<template style="display:none" id="HH-Lux-InitialState">…</template>`
с **HTML-escaped JSON** (`&quot;` → `"`, см. `html.unescape`). Берём `driver.page_source` → regex по
`<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>` → `json.loads(html.unescape(...))`.

**Поиск** `https://hh.ru/search/vacancy?…` → `vacancySearchResult`:
- `criteria` — эхо принятых параметров (для самопроверки), `totalResults`, `paging` (`null`, если одна
  страница; иначе `pages[] {page, selected}`, `next`), `vacancies[]`:
  `vacancyId`, `name`, `compensation {from, to, currencyCode, gross, mode}`, `area {@id, name}`,
  `company {id, name, visibleName, @trusted}`, `workFormats[] {workFormatsElement[]}` (ON_SITE/REMOTE/HYBRID/FIELD_WORK),
  `employmentForm` (FULL/PART/PROJECT/…), `acceptTemporary` (bool), `civilLawContracts[] {civilLawContractsElement[]}`
  (SELF_EMPLOYED/INDIVIDUAL_ENTREPRENEUR/INDIVIDUAL_PERSON), `publicationTime {$ iso, @timestamp}`, `links.desktop`,
  `userLabels[]` (метки пользователя; признак отклика ищет `hh_pages._labels_mean_applied`), `responsesCount`.

**Вакансия** `https://hh.ru/vacancy/{id}` → `vacancyView`: `description` (HTML), `keySkills.keySkill[]`,
`status {active, archived, disabled}`, `compensation`, `workFormats[]` (плоский список), `employmentForm`,
`civilLawContracts[]` (плоский список), `acceptLaborContract`, `area {@id, name}`, `publicationDate`, `company`,
`userLabels`, `address`, `workExperience`.
**Внимание:** `acceptTemporary` в `vacancyView` **нет** — он лежит в
`applicantVacancyResponseStatuses.<id>.shortVacancy.acceptTemporary` (там же полная короткая карточка).
`hh_pages._accept_temporary` читает оттуда, а при отсутствии блока выводит флаг из непустого `civilLawContracts`.

**Отклики** `https://hh.ru/applicant/negotiations?filter=all` (проверено в залогиненном браузере 2026-09-08):
`userType == "applicant"`; `applicantNegotiations.topicList[] {vacancyId, lastState (RESPONSE/INTERVIEW/DISCARD/…),
initialState, conversationMessagesCount, hasNewMessages, archived, resumeId, chatId}`, `total`, `paging` (null на одной
странице); `vacanciesShort.vacanciesList[]` — короткие карточки вакансий откликов. **Бонус:** на той же странице
`suitableVacancies {resultsFound, vacancies[]}` — 6 рекомендованных по резюме вакансий в формате карточек поиска.
Их забираем как проход `similar` без дополнительных загрузок.
На странице поиска под логином `userLabelsForVacancies {vacancyId: [labels]}` — метки пользователя по карточкам.

### Параметры URL поиска (совпадают со старым API, подтверждено через `criteria`)
- `text` — язык запросов hh: `OR`, `AND`, кавычки, скобки; `no_magic=true` — не переписывать запрос.
- `area` — id региона, **повторяемый** (`area=1&area=2019`).
- `work_format=REMOTE|HYBRID|ON_SITE|FIELD_WORK`; `employment_form=FULL|PART|PROJECT|FLY_IN_FLY_OUT|SIDE_JOB` (повторяемые).
- `accept_temporary=true` — фильтр hh «Оформление по ГПХ или по совместительству» (проход `gph`, v8.2).
  Название и параметр подтверждены эхом фильтров на странице поиска: `accept_temporary.groups.true.title`.
- `search_period=2` (дней), `order_by=publication_time`, `items_on_page=50`, `page=0..`.
- `search_field=name|company_name|description` (по умолчанию все).

### Управление браузером
- Firefox ESR 140 владельца запущен с `--marionette` → Marionette на `127.0.0.1:2828`
  (`scripts/setup_firefox.sh` правит пользовательский `.desktop`).
- На каждую серию (`bursts.run_in_bursts`, 7–13 мин): `geckodriver --connect-existing --marionette-port 2828 --port <свободный>` (дочерний процесс),
  `selenium.webdriver.Remote(...)`. Открываем **своё окно** (`switch_to.new_window("window")`), работаем в нём,
  закрываем окно, завершаем geckodriver. **`driver.quit()` не вызывать** — в режиме connect-existing это
  попросит Firefox завершиться. Проверено на headless-экземпляре: после отсоединения Firefox живёт.
- Только навигация по URL, прокрутка, чтение `page_source`. Никаких кликов по элементам сайта.
- Человекоподобие (v6, `browser/pacing.py`, `browser/bursts.py`): «чтение» страницы 6–20 с (примерно каждая 6-я — 25–60 с),
  прокрутка на случайную глубину, случайный порядок запросов и проходов; три подхода в день со случайным стартом в окнах
  `CRAWL_WINDOWS` (07–10 / 12–15 / 18–22), внутри подхода серии `BURST_MINUTES` 7–13 мин с паузами `GAP_MINUTES` 4–9 мин;
  дневной лимит — случайный `DAILY_PAGE_LOADS_MIN..MAX` (100–140) **суммарно по всем процессам**, делится между
  оставшимися подходами.
- Ошибки: Marionette не отвечает / geckodriver нет → `BrowserUnavailable` → тревога владельцу; ретраев нет, следующий
  подход придёт по расписанию (за 30 мин до него `precheck_job` проверит Firefox и мост).
  Страница без `HH-Lux-InitialState` (капча, редирект на логин) → warning, прогон останавливается мягко,
  владельцу: «hh.ru показал капчу/просит войти — откройте hh.ru в Firefox и пройдите проверку».

## 1b. profi.ru — кабинет специалиста в том же Firefox (v7, выключено по умолчанию)
- Страница: `https://profi.ru/backoffice/n.php` — лента заказов по специализациям анкеты владельца (поиск и фильтры
  живут в кабинете; бот ничего не настраивает и не кликает). Карточка заказа: `n.php?o=<id>` — не открывается, в ленте
  уже есть весь текст. Нет ни API, ни встроенного JSON-состояния; парсим DOM после рендера (`profi/pages.py`).
- Признаки кабинета: `data-testid="…_order-snippet"`, «Вы посмотрели все новые заказы», пункт меню «Анкета»; без них —
  `ProfiBlocked` (вышли из аккаунта, капча, сменилась разметка) → тревога 🚫 profi.ru, hh.ru не страдает.
- **Правила profi.ru (`/documents/terms-of-use/`, «Использование материалов Компании») запрещают «извлечение любых
  данных с сайта (парсинг)»**, компания вправе удалить аккаунт; отклики платные. Решение владельца 2026-09-10 — источник
  включить, след держать минимальным: одна загрузка ленты за подход (≤3 в день), только чтение, стоп при капче; см.
  `docs/DECISIONS.md` №18. План Б без парсинга — уведомления profi.ru о заказах на почту (Mail.ru) + IMAP.
- Данные заказа: id, заголовок, «Пожелания и особенности» (полный текст), бюджет («до 5000 ₽», «30 000 ₽»), формат
  (Дистанционно / У клиента / У специалиста) и город, удобное время, имя клиента, «Вчера в 12:09». Фикстура —
  `tests/fixtures/profi_orders.html` (обезличенный снимок 2026-09-10, имена клиентов заменены).

## 2. Мост Claude (`bridge/`)

Собственный сервис проекта: FastAPI + systemd на хосте, порт **8766** (слушает 0.0.0.0, защита — токен), на каждый запрос запускает
`claude -p --output-format json --max-turns 1 --tools "" --model <m> [--system-prompt <s>]` (prompt в stdin;
`--system-prompt` только при непустом `system_text`)
под подпиской владельца. Рабочая директория CLI — каталог без CLAUDE.md (по умолчанию системный temp).

### Контракт
```
GET  /health → {"ok": true, "model": "<BRIDGE_MODEL>"}

POST /complete
Headers: X-Bridge-Token: <token>, Content-Type: application/json
Body: {
  "system_text": "<системный промпт>",
  "messages": [{"role": "user", "content": "..."}],   # 1..20 сообщений; несколько склеиваются в один prompt
  "model": ""                                          # пусто → BRIDGE_MODEL моста (клиент шлёт .env BRIDGE_MODEL)
}                                                      # max_tokens сервер принимает, клиент не отправляет
→ 200 {"text": "<ответ модели>", "usage": {input_tokens, output_tokens, cache_read_input_tokens,
        cache_creation_input_tokens}, "cost_usd": 0.0017}
→ 401 неверный токен · 502 CLI упал / вернул не-JSON / is_error · 504 CLI не уложился в BRIDGE_TIMEOUT
```

### Настройка (`bridge/.env.bridge`, создаётся `install.sh`)
`HH_BRIDGE_TOKEN` (общий секрет = `BRIDGE_TOKEN` проекта), `BRIDGE_MODEL=opus`, `BRIDGE_TIMEOUT=150`,
`BRIDGE_PORT=8766` (только для unit-файла), `BRIDGE_WORKDIR` (cwd для CLI, без CLAUDE.md; по умолчанию temp),
`CLAUDE_BIN` (путь к claude), при необходимости `CLAUDE_CODE_OAUTH_TOKEN` (из `claude setup-token`).

### Клиент `src/hh_scout/llm/bridge_client.py`
- httpx, таймаут **180 с** (больше серверных 150 с); ретраи таймаут/сеть/502/503/504 до 3 попыток (2 с, 4 с);
  401 и прочие 4xx не ретраить.
- `extract_json(text)` — первый корректный JSON-блок (модель может обернуть в ```json …```).
- Невалидный JSON → один повторный запрос с дописанным «Предыдущий ответ не прошёл валидацию: <ошибка>. Верни ТОЛЬКО
  корректный JSON-массив по схеме…» (тексты в `llm/triage.py`, `llm/evaluator.py`).

### Бюджет вызовов за сутки
Триаж: 1 вызов на 30 карточек (~$0.08); оценка: 1 на 5 вакансий (~$0.10); письма: 1 на лид (~$0.06).
Типичный день: 3–5 триаж + 5–12 оценка + 5–20 писем ≈ $1.5–3 по счётчику моста (оплачивается подпиской).

## 3. Telegram-бот

- aiogram v3, long polling. Бот отдельный, создаётся через @BotFather.
- `TG_OWNER_CHAT_ID`: если пуст, бот на любое сообщение отвечает «Ваш chat_id: N — впишите его в .env»
  и больше ничего не делает. Все апдейты не от владельца игнорируются без ответа.
- Клавиатура под карточкой — два ряда: `[👍] [👎]` (`fb:<vacancy_id>:up|down`) и `[✅ Написал] [⏸ Позже]`
  (`act:<vacancy_id>:responded|later`); служебный `noop`. 👍 → кнопка «👍 отмечено»; 👎 → клавиатура причины
  `fbr:<vacancy_id>:salary|format|stack|agency|skip` → карточка сворачивается в строку, письмо удаляется; ✅ → то же
  с пометкой «✅ Написал»; ⏸ → «⏸ отложено». Подробности — ARCHITECTURE «Жизненный цикл лида».
- HTML-разметка, превью ссылок отключено; карточка + письмо (`<pre>`) на лид, пауза 0.6 с между сообщениями.
- Чужие апдейты игнорируются без ответа (строка INFO в лог). При пустом `TG_OWNER_CHAT_ID` бот отвечает chat_id.
