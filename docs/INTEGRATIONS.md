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
**Вторая разметка «magritte» (с 25.09.2026, эксперимент `web_applicant_vacancy_magritte`, выкатывается поэтапно —
в подходе 18:19 13 из 30 страниц, в 20:33 — все 30):** тот же объект вакансии лежит в
`vacancyView.vacancyFull.vacancy` (`vacancyId`, `name`, `description`, `keySkills[]`, `status`, `compensation`,
`workFormats[]`, `employmentForm`, `civilLawContracts[]`, `area`, `company`, `closedForApplicants`), дата —
`publicationTimeIso` вместо `publicationDate`, `userLabels` — в `vacancyView.vacancyFull.extraVacancyFields`.
`hh_pages._vacancy_block` распознаёт обе разметки (фикстуры `tests/fixtures/vacancy_page.html` и
`vacancy_page_magritte.json`); «уже откликался» берётся из `applicantVacancyResponseStatuses.<id>.alreadyApplied`
(есть в обеих), `userLabels` — запасной путь. Третья разметка даст `PageFormatError` с перечнем ключей `vacancyView`
в warning — по ним видно, куда переехали данные.
**Внимание:** `acceptTemporary` в объекте вакансии **нет** — он лежит в
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
- На каждую серию (`bursts.run_in_bursts`, 7–13 мин): `geckodriver --connect-existing --marionette-host 127.0.0.1 --marionette-port 2828 --host 127.0.0.1 --port <свободный> --log warn` (дочерний процесс),
  `selenium.webdriver.Remote(...)`. Открываем **своё окно** (`switch_to.new_window("window")`), работаем в нём,
  закрываем окно, завершаем geckodriver. **`driver.quit()` не вызывать** — в режиме connect-existing это
  попросит Firefox завершиться. Проверено на headless-экземпляре: после отсоединения Firefox живёт.
- Только навигация по URL, прокрутка и сдвиг курсора (`ActionChains.move_by_offset`), чтение `page_source`. Никаких кликов по элементам сайта.
- Человекоподобие (v6, `browser/pacing.py`, `browser/bursts.py`): «чтение» страницы 6–20 с (примерно каждая 6-я — 25–60 с),
  прокрутка на случайную глубину, случайный порядок запросов и проходов; по подходу на каждое окно `CRAWL_WINDOWS` со случайным
  стартом (по умолчанию три: 07–10 / 12–15 / 18–22; у владельца шесть круглые сутки, решение №55), внутри подхода серии `BURST_MINUTES` 7–13 мин с паузами `GAP_MINUTES` 4–9 мин;
  дневной лимит — случайный `DAILY_PAGE_LOADS_MIN..MAX` (150–200) **суммарно по всем процессам**, делится между
  оставшимися подходами.
- Ошибки: Marionette не отвечает / geckodriver нет → `BrowserUnavailable` → тревога владельцу; ретраев нет, следующий
  подход придёт по расписанию (за 30 мин до него `precheck_job` проверит Firefox и мост).
  Страница с шаблоном `HH-Lux-InitialState`, но оборванным JSON (недогруженный документ, v9.39) → ждём `readyState complete`
  и перечитываем, затем одна перезагрузка, затем `PageIncomplete` — одна страница пропускается, стадия идёт дальше.
  Страница без `HH-Lux-InitialState` (капча, редирект на логин) → warning, прогон останавливается мягко,
  владельцу: «🚫 hh.ru не отдал данные — похоже на капчу или требование войти. Откройте hh.ru в этом Firefox, пройдите проверку/войдите…» (`health.py`).

## 1b. profi.ru — кабинет специалиста в том же Firefox (v7, выключено по умолчанию)
- Страница: `https://profi.ru/backoffice/n.php` — лента заказов по специализациям анкеты владельца (поиск и фильтры
  живут в кабинете; бот ничего не настраивает и не кликает). Карточка заказа: `n.php?o=<id>` — не открывается, в ленте
  уже есть весь текст. Нет ни API, ни встроенного JSON-состояния; парсим DOM после рендера (`profi/pages.py`).
- Признаки кабинета: `data-testid="…_order-snippet"`, «Вы посмотрели все новые заказы», пункт меню «Анкета»; без них —
  `ProfiBlocked` (вышли из аккаунта, капча, сменилась разметка) → тревога 🚫 profi.ru, hh.ru не страдает.
- **Правила profi.ru (`/documents/terms-of-use/`, «Использование материалов Компании») запрещают «извлечение любых
  данных с сайта (парсинг)»**, компания вправе удалить аккаунт; отклики платные. Решение владельца 2026-09-10 — источник
  включить, след держать минимальным: одна загрузка ленты за подход (по числу окон: 3 в день по умолчанию, 6 у владельца), только чтение, стоп при капче; см.
  `docs/DECISIONS.md` №18. План Б без парсинга — уведомления profi.ru о заказах на почту (Mail.ru) + IMAP.
- Данные заказа: id, заголовок, «Пожелания и особенности» (полный текст), бюджет («до 5000 ₽», «30 000 ₽»), формат
  (Дистанционно / У клиента / У специалиста) и город, удобное время, имя клиента, «Вчера в 12:09». Фикстура —
  `tests/fixtures/profi_orders.html` (обезличенный снимок 2026-09-10, имена клиентов заменены).

## 1c. «Работа России» (trudvsem.ru) — открытое API, без браузера (v9.25)
- `GET https://opendata.trudvsem.ru/api/v1/vacancies?text=<запрос>&offset=<номер страницы, с 0>&limit=100[&modifiedFrom=YYYY-MM-DDTHH:MM:SSZ]`
  (`offset` — номер страницы, а не смещение записи: `offset=100` отвечает 500)
  → `{status, meta: {total, limit}, results: {vacancies: [{vacancy: {...}}]}}`. Поиск полнотекстовый (по требованиям
  тоже), без OR — по запросу на вызов (`config.TRUDVSEM_QUERIES`). Ответ ~9 с, `httpx` с `TRUDVSEM_TIMEOUT_S`=90.
- Поля записи: `id` (uuid), `job-name`, `duty`, `requirements`, `skills`, `salary_min/max`, `employment`, `schedule`,
  `region.name`, `addresses.address[].location`, `company {name, inn, ogrn, companycode, email, hr-agency, url}`,
  `contact_list [{contact_type: «Эл. почта»|«Телефон», contact_value}]`, `contact_person`, `vac_url`, `date_modify`,
  `requirement {education, experience}`, `category.specialisation`. Парсер — `sources/trudvsem.parse_vacancy`; фикстура —
  `tests/fixtures/trudvsem_vacancies.json` (две реальные записи и три синтетические: Крым, без e-mail, стоп-слово).
  Запросы идут с `User-Agent` = `HH_USER_AGENT`, с паузой `TRUDVSEM_REQUEST_GAP_S` (3 с), не больше
  `TRUDVSEM_MAX_PAGES_PER_QUERY` (10) страниц на запрос; синхронизация — стадия 1b каждого подхода (`trudvsem.sync`),
  отдельной задачи планировщика нет; момент последней удачной — kv `trudvsem_last`, следующая читает с него минус 2 дня; первая (ключа нет) — за
  `TRUDVSEM_BACKFILL_DAYS` (14) назад.
- Доступ с хоста — только через прямой маршрут на роутере владельца (решение №66); без него `opendata.` уходит
  в таймаут, `trudvsem.ru` отвечает 460. Сбой — `TrudvsemUnavailable` → `report.trudvsem_error` → мягкая тревога.

## 1d. zakupki.gov.ru (ЕИС) — RSS и страницы без браузера, по запросу в минуту (v9.26)
- `robots.txt`: разрешены `/epz/main/public*`, `/*order*`, `/*search*`, `/*rss*`, `/*notice*`, `/*contract*`, `/*printForm*`;
  запрещены `/*auth*`, `/*admin*`, `/*private*`; **`Crawl-delay: 60`** — `Fetcher` держит паузу `ZAKUPKI_REQUEST_GAP_S` (61 с)
  между любыми двумя запросами, `User-Agent` — строка Firefox 128 (`zakupki.USER_AGENT`). Бот-защиты нет (27–28.09: ни капчи,
  ни 403/429; только `session-cookie`). Вся цепочка — `zakupki.run_job`: ежедневно из `Scheduler.zakupki_job`
  (`ZAKUPKI_HOUR:ZAKUPKI_MINUTE`, по умолчанию 05:20, `misfire_grace_time` 12 ч, сбой — тревога `zakupki_failed`; v9.29 — до 28.09 задачи
  в `scheduler.py` не было) или вручную `python -m hh_scout.sources.zakupki`; kv `zakupki_last` пишет сам `run_job`.
- TLS: сертификат `*.zakupki.gov.ru` выдан Russian Trusted Sub CA → Russian Trusted Root CA (Минцифры); системные CA его не
  знают. Корневой PEM — `certs/russian_trusted_root_ca.pem` (скачан с gu-st.ru, SHA-256 `D2:6D:2D:02:…:CF:31` сверен с
  живой цепочкой), `httpx.get(verify=CA_PATH)`. Истекает 27.02.2032.
- Открытых данных больше нет: `ftp.zakupki.gov.ru` — NXDOMAIN у авторитетного DNS, `/opendata/` — 403, SOAP-шлюз
  `int44.zakupki.gov.ru` — по токену организации и закрыт файрволом.
- **RSS расширенного поиска извещений**: `/epz/order/extendedsearch/rss.html?searchString=<фраза>&morphology=on&fz44=on&pc=on
  &sortBy=UPDATE_DATE&recordsPerPage=_50…` (`pc=on` — этап «Закупка завершена»; `af`/`ca` — подача заявок / работа комиссии).
  Элемент: `<link>` `…/epz/order/notice/<тип>/view/common-info.html?regNumber=<19 цифр>` (тип `ea20`/`zk20`/`ok20`…),
  `<description>` — «Наименование объекта закупки», «Размещение выполняется по» (44-ФЗ/223-ФЗ), «Наименование Заказчика»,
  «Начальная цена контракта», «Этап размещения», ИКЗ. Поиск полнотекстовый по вложениям — фразы см. `ZAKUPKI_QUERIES`;
  извещения 223-ФЗ и не прошедшие `zakupki.relevant()` (по предмету) в базу не попадают.
- **Результаты определения поставщика**: `/epz/order/notice/<тип>/view/supplier-results.html?regNumber=…` — таблица
  «Сведения о контракте из реестра контрактов» (реестровый номер, заказчик, исполнитель, цена); пока контракт не
  заключён, таблицы нет (`parse_supplier_results` → `[]`) — извещение ждёт следующего запуска (`raw_json.tries`), после
  `ZAKUPKI_MAX_TRIES` (6) — `skipped/tender:no_contract`.
- **Карточка контракта 44-ФЗ**: `/epz/contract/contractCard/common-info.html?reestrNumber=…` — «Общие данные» (дата
  заключения, предмет, цена, срок исполнения, заказчик) и «Информация о поставщиках» (организация с ИНН/КПП, адрес,
  **телефон и e-mail**, статус СМП); карточка без блока поставщиков (`parse_contract_card` → `None`) лид не отменяет — победитель
  берётся по имени из таблицы результатов, без ИНН и e-mail (дальше обычно `skipped/no_email`); `skipped/tender:no_supplier`
  ставится только строке без имени и ИНН. Фикстуры —
  `tests/fixtures/zakupki_rss.xml`, `zakupki_supplier_results.html`, `zakupki_contract_card.html` (снимки 27–28.09).
- 223-ФЗ: карточка договора (`/epz/contractfz223/card/contract-info.html`) исполнителя не показывает; RSS реестра договоров
  223 без описания. Не используется.

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
  "model": "",                                         # пусто → BRIDGE_MODEL моста (клиент шлёт .env BRIDGE_MODEL)
  "allow_web": false,                                  # true ТОЛЬКО для разведки по компании (v9.0)
  "max_turns": 1                                       # действует лишь при allow_web; иначе всегда 1
}                                                      # max_tokens сервер принимает, клиент не отправляет
→ 200 {"text": "<ответ модели>", "usage": {input_tokens, output_tokens, cache_read_input_tokens,
        cache_creation_input_tokens}, "cost_usd": 0.0017}
→ 401 неверный токен · 502 CLI упал / вернул не-JSON / is_error · 504 CLI не уложился в BRIDGE_TIMEOUT
```

### Веб-инструменты (`allow_web`, v9.0)
По умолчанию CLI запускается `--max-turns 1 --tools ""` — триаж, оценка и письма обязаны быть воспроизводимыми
и дешёвыми. `allow_web: true` даёт **только** `WebFetch,WebSearch` плюс `--permission-mode bypassPermissions`:
файловых и командных инструментов в списке нет, cwd и так пустая песочница (`BRIDGE_WORKDIR`, без CLAUDE.md).
Единственный клиент — `llm/company_research.py` (досье для строк `site` hh / owen / trudvsem / zakupki с непустым `employer_id`;
победитель закупки — по названию и ИНН, v9.30; заказы profi не разведываются). Сборка командной строки вынесена в `bridge/cmdline.py`,
чтобы её можно было проверить тестом из основного venv (в нём нет FastAPI).
Чтение страниц идёт минутами, а не секундами (замер: 5 ходов = 300 с), поэтому у веб-запросов свой таймаут
`BRIDGE_WEB_TIMEOUT` (900 с против 150 с обычного; 420 с не хватало — шесть обращений на opus в них не влезают,
см. v9.4.1), клиент ждёт `COMPANY_RESEARCH_TIMEOUT_S` (930 с) и **без
ретраев**: мост, только что не уложившийся в тяжёлую страницу, не уложится и со второй попытки. «Без ретраев» —
это отдельный `BridgeClient(settings, retries=0)`, который `CoverLetterWriter` заводит разведке сам
(`self._research_bridge`): пока разведке отдавали клиент письма с `retries=2`, умолчание `CompanyResearcher`
молча затиралось и мёртвый сайт стоил 3 × 420 с = 21 минуту на одну компанию (v9.2). Стоимость в логе шага
писем складывает оба клиента. Объём работы ограничен с двух сторон — промпт разрешает 6 обращений к сети
(`COMPANY_RESEARCH_MAX_TURNS`=8 — запас под них, а не цель), `COMPANY_RESEARCH_MAX_PER_RUN` (3 — снижен вместе
с ростом таймаута, чтобы худший случай шага писем остался ~45 мин) не даёт одному
прогону писем уйти в чтение веба надолго; недоисследованные компании достаются следующему прогону.
**Источники (v9.4)**: страница работодателя на hh.ru, сайт компании (одна попытка `https`, затем одна `http`),
открытые источники по 2–3 поисковым запросам (каталоги, отраслевые порталы, выставки, тендеры, СМИ)
и **другие вакансии этой же компании на hh.ru**. Последнее — не прихоть: сайты российских промышленных
компаний из этого контура почти всегда молчат (9 досье из 10 за 14.09 собраны без сайта), а их собственные
вакансии читаются всегда и называют стек прямее сайта — бренды ПЛК, SCADA, протоколы, типы объектов.
Модель разведки — `COMPANY_RESEARCH_MODEL`, по умолчанию **пусто = модель моста** (opus): именно разведка
решает, насколько письмо понимает производство компании, поэтому экономить тут нечего (sonnet дешевле в 4–5 раз,
но хуже вытаскивает зацепки с сайта).

### Настройка (`bridge/.env.bridge`, создаётся `install.sh`)
`HH_BRIDGE_TOKEN` (общий секрет = `BRIDGE_TOKEN` проекта), `BRIDGE_MODEL=opus`, `BRIDGE_TIMEOUT=150`,
`BRIDGE_PORT=8766` (только для unit-файла), `BRIDGE_WEB_TIMEOUT=900` (таймаут веб-запросов разведки),
`BRIDGE_WORKDIR` (cwd для CLI, без CLAUDE.md; по умолчанию temp),
`CLAUDE_BIN` (путь к claude), при необходимости `CLAUDE_CODE_OAUTH_TOKEN` (из `claude setup-token`).

### Клиент `src/hh_scout/llm/bridge_client.py`
- httpx, таймаут **180 с** (больше серверных 150 с); ретраи таймаут/сеть/502/503/504 до 3 попыток (2 с, 4 с);
  401 и прочие 4xx не ретраить.
- `extract_json(text)` — первый корректный JSON-блок (модель может обернуть в ```json …```).
- Невалидный JSON → один повторный запрос с дописанным «Предыдущий ответ не прошёл валидацию: <ошибка>. Верни ТОЛЬКО
  корректный JSON-массив по схеме…» (тексты в `llm/triage.py`, `llm/evaluator.py`).

### Бюджет вызовов за сутки
Триаж: 1 вызов на 30 карточек (~$0.08); оценка: 1 на 5 вакансий (~$0.10); письма: 2–3 на лид — черновик, при отказе
проверок один повтор, затем редактор — для hh и компаний, заявки profi без него (~$0.06 за вызов); разведка по компании: 1 на **новую** компанию, opus,
до `COMPANY_RESEARCH_MAX_TURNS` (8) ходов и 6 обращений к сети (~$0.2–0.3 и 1–3 мин;
на sonnet было ~$0.05–0.09 и 20–45 с — разница сознательно оплачена качеством письма, v9.2).
Типичный день (v9.18, шесть подходов): триаж и оценка в каждом подходе, писем столько, сколько влезло в `LETTERS_BUDGET_MIN`
(60 мин на подход, нормы нет с v9.14) — по счётчику моста ≈ $0.5–0.6 за оценку подхода плюс письма (подписка).
Разведка идёт **мимо браузера**: страницы читает CLI, дневной лимит загрузок hh.ru она не расходует.

## 3. Telegram-бот

- aiogram v3, long polling. Бот отдельный, создаётся через @BotFather.
- `TG_OWNER_CHAT_ID`: если пуст, бот на любое сообщение отвечает «Ваш chat_id: N — впишите его в .env»
  и больше ничего не делает. Все апдейты не от владельца игнорируются без ответа (строка INFO в лог).
- Клавиатура под карточкой — два ряда: `[👍] [👎]` (`fb:<vacancy_id>:up|down`) и `[✅ Написал] [⏸ Позже]`
  (`act:<vacancy_id>:responded|later`); служебный `noop`. 👍 → кнопка «👍 отмечено»; 👎 → клавиатура причины
  `fbr:<vacancy_id>:salary|format|stack|agency|text|skip` (`text` — причина своими словами ответом на вопрос бота, kv `awaiting_reason`) → карточка сворачивается в строку, письмо удаляется; ✅ → то же
  с пометкой «✅ Написал»; ⏸ → «⏸ отложено». Подробности — ARCHITECTURE «Жизненный цикл лида».
- HTML-разметка, превью ссылок отключено; карточка + письмо (`<pre>`) на лид, пауза `TELEGRAM_PAUSE_S` (1.2 с) между сообщениями; в `QUIET_HOURS` — без звука.
