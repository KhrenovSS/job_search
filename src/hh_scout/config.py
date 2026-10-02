"""All runtime settings in one place.

Secrets and deployment knobs come from `.env` (see `.env.example`).
Search tuning (queries, regions, stop words, score weights) lives here as plain
constants: it changes rarely and is easier to review in code than in env vars.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TZ = ZoneInfo("Europe/Moscow")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # hh.ru is browsed through the owner's running Firefox (Marionette). Only the open
    # dictionaries (api.hh.ru/areas) are fetched directly, with this User-Agent.
    hh_user_agent: str = "hh-scout/1.0 (set HH_USER_AGENT in .env)"

    # Firefox / Marionette / geckodriver
    marionette_host: str = "127.0.0.1"
    marionette_port: int = 2828
    geckodriver_path: str = ""  # empty -> look up "geckodriver" in PATH (incl. ~/.local/bin)
    page_delay_min_s: float = 6.0  # human-like "reading" pause after each page load, seconds
    page_delay_max_s: float = 20.0
    # Every ~N-th page gets a long "reading" pause instead (a person stops on something interesting).
    long_read_every: int = 6
    long_read_min_s: float = 25.0
    long_read_max_s: float = 60.0
    # Daily page-load cap (search + vacancy pages, all processes): drawn once per day at random from this range
    # and stored in kv `daily_cap:<date>` so the number differs from day to day.
    # 100-140 (v9.5) → 150-200. The owner's instance runs 250-300 since v9.15 (decision #55, set in .env): the cap was
    # the one throttle left (375 vacancies and 260 companies waited for a page). The default stays the safer range —
    # how much to load is a risk decision for whoever runs the account, not for the code.
    daily_page_loads_min: int = 150
    daily_page_loads_max: int = 200
    # Rhythm inside a sitting: browse for burst_minutes, stay quiet for gap_minutes, repeat ("MIN-MAX").
    burst_minutes: str = "7-13"
    gap_minutes: str = "4-9"
    items_per_page: int = 50
    max_pages_per_query: int = 6
    # Pages of the owner's own responses read per sitting (20 per page). One page saw only the newest 20 of 77
    # and the loop learned from a truncated sample; 3 covers the current rate (v9.10, decision #48).
    negotiations_pages: int = 3
    # Share of a run's page budget held back for vacancy pages, so collection cannot eat it all
    # and leave DetailsFetcher with nothing (cards pile up in `to_fetch`, no evaluations, no leads).
    # A floor, not a ceiling: details also get whatever collection did not spend.
    details_budget_share: float = 0.55  # v9.5: 243 cards collected per sitting against 14 pages opened — cards
                                        # were never the scarce side, and a lead is born only from an opened page
    # How the vacancy pages of a sitting are shared between channels (v9.17, decision #57): "channel:share,...", keys
    # are search passes, `vacancy` covers every pass not listed (regional, remote, similar...). Without shares the
    # newest card always won and a company admitted a day earlier waited until the TTL wrote it off (23.09: 120 plant
    # companies admitted, 0 opened). A share nobody uses flows to the others; triage priority 1-2 cards go first
    # regardless. Empty = the old order (priority, then newest).
    # v9.38 (decision #74): panel builders first — 0.45/0.20/0.10/0.25 → 0.35/0.30/0.15/0.20.
    details_channel_shares: str = "vacancy:0.35,panel:0.30,design:0.15,plant:0.20"
    # A letter younger than this has not had time to be answered and is left out of every rate
    # (measured 19.09: 0-1 days old -> 0 % answered, 9-10 days -> 78-82 %). Reported separately instead.
    outcome_mature_days: int = 5
    low_priority_ttl_days: int = 3  # triage priority 3 cards still unopened after this many days are dropped
    # One lead per company: further vacancies of an employer that already got a lead are skipped (duplicate_employer)
    # for this many days after the lead was sent; 0 = forever. Same-employer twins inside one digest always collapse to one.
    employer_repeat_days: int = 90

    # Claude bridge on the host (bridge/hh_scout_bridge.py)
    bridge_url: str = "http://127.0.0.1:8766"
    bridge_token: str = ""
    bridge_model: str = ""  # empty -> the bridge's own default (BRIDGE_MODEL)
    bridge_timeout_s: float = 180.0

    # Company research (v9.0): one web-enabled bridge call per employer, cached in `employers`.
    # Runs outside the browser, so it costs no page loads from the daily cap — only bridge money.
    company_research_enabled: bool = True
    company_research_model: str = ""        # empty -> the bridge's own model (BRIDGE_MODEL, opus): reading a
                                            # company's site well is what makes the letter specific
    company_research_ttl_days: int = 180
    company_research_max_turns: int = 8     # CLI turns (tool calls + answer); the prompt itself caps network calls
                                            # at 6, so 8 leaves room for the final answer. Not a target: most runs
                                            # take 2-4. The 19-minute run was retries on a dead site, not depth (v9.2)
    company_research_timeout_s: float = 930.0   # must exceed the bridge's own BRIDGE_WEB_TIMEOUT (900): six
                                            # network calls on opus do not fit into 420 s when the site is silent
    company_research_max_per_run: int = 3   # ceiling on how long one letters step may spend reading the web —
                                            # lowered with the timeout raised, so the worst case stays ~45 min

    # Which programmer search passes run (comma-separated of regional, remote, project, gph). With the whole
    # country in one `regional` pass the other three are strict subsets of it: over 10 days they brought 6 % of the
    # cards and 3 letters while costing a page per task per sitting — a quarter of the daily cap (v9.15).
    search_passes: str = "regional"

    # Telegram
    tg_bot_token: str = ""
    tg_owner_chat_id: int | None = None
    # Pause before every message of a batch. Telegram allows about one message per second into a chat, and
    # since v9.14 a send can be dozens of leads (two messages each), so the old 1.0 s sat exactly on the limit.
    telegram_pause_s: float = 1.2

    # Schedule and limits
    digest_time: str = "12:00"  # Europe/Moscow, HH:MM — digest is sent every day at this time
    # Sittings: one crawl per window, its START picked at random inside the window; the daily cap is shared between
    # the sittings still ahead. Comma-separated, sorted, non-overlapping, none across midnight.
    # The owner's instance runs six three-hour windows round the clock since v9.15 (decision #55, see .env.example):
    # hour-long gaps between them, the 03:00-04:00 gap is where the nightly backup lands, Firefox stays open all
    # night. The default keeps the daytime schedule — the same risk decision as the page cap above.
    crawl_windows: str = "07:00-10:00,12:00-15:00,18:00-22:00"
    # Telegram messages sent in this interval (may cross midnight) arrive silent (`disable_notification`): a lead
    # found at 03:00 is in the chat by morning without waking anyone. Empty = never silent.
    quiet_hours: str = "23:00-07:00"
    # The daily quota of leads sent, across instant sends and the noon digest together. v9.1 set it to 5;
    # v9.7 doubled it; v9.8 raised it to 20. 0 = no quota at all, which is the default since v9.14
    # (decision #54): the owner wants every lead found to go out, because a letter not written is a certain
    # no. What holds the day's volume now is letters_budget_min below — the bridge's pace, not a number.
    # A positive value turns the fuse back on.
    digest_max_items: int = 0
    # How long one letter-writing pass may take (minutes). The real cost of a lead is not its slot in a quota
    # but ~80 s of bridge time, and the letter stage knows nothing about the sitting's window — without this
    # a hundred leads would push the crawl into the next window and hold crawl_lock for hours. Whatever is left
    # unwritten keeps its place in the queue (it gains a waiting bonus) and gets its letter next sitting.
    letters_budget_min: int = 60
    queue_ttl_days: int = 30        # a lead nobody got to in a month leaves the queue (the vacancy is gone)
    queue_wait_bonus_max: int = 7   # a day of waiting is worth a point, capped: the tail must not starve
    digest_tail_items: int = 8      # how many of the waiting ones the digest lists by name
    search_period_days: int = 2
    # Geography: True searches the whole country (one area id 113) — the owner contracts remotely, so where
    # the client sits does not matter. False falls back to the explicit REGION_NAMES list below.
    search_all_russia: bool = True
    db_path: Path = PROJECT_ROOT / "data" / "hh_scout.db"
    # Nightly backup of the database: the owner's feedback and the employers' answers are not
    # reproducible from hh.ru, so they get their own copies (decision #40).
    backup_dir: Path = PROJECT_ROOT / "data" / "backups"
    backup_keep: int = 7
    prompts_dir: Path = PROJECT_ROOT / "prompts"
    log_level: str = "INFO"

    # profi.ru — second source: client orders from the owner's specialist cabinet (needs login in the same Firefox).
    # Off by default. One feed load per sitting, read-only; the site's terms forbid parsing — keep the footprint tiny.
    profi_enabled: bool = False
    profi_orders_url: str = "https://profi.ru/backoffice/n.php"
    profi_pages_per_run: int = 1

    # «Работа России» (trudvsem.ru, v9.25) — the state portal's open vacancy API: JSON, no browser, no page limit.
    # Off by default; the host reaches it only through the owner's direct (non-VPN) route (decision #66).
    trudvsem_enabled: bool = False
    trudvsem_api_url: str = "https://opendata.trudvsem.ru/api/v1/vacancies"
    trudvsem_backfill_days: int = 14       # first sync: how far back; later syncs read since the last one
    trudvsem_per_run: int = 60             # rows let into evaluation per sync; the rest wait as `new` (a 30-day backfill is ~700)
    trudvsem_company_per_run: int = 20     # same gate for panel-builder rows (TRUDVSEM_COMPANY_QUERIES, v9.40): their own budget
    trudvsem_max_pages_per_query: int = 10  # × 100 vacancies
    trudvsem_request_gap_s: float = 3.0     # a public API, but no need to hammer it: ~9 s per answer anyway
    trudvsem_timeout_s: float = 90.0

    # zakupki.gov.ru (v9.26, decision #68) — winners of 44-ФЗ procurements for automation work as company leads
    # (channel `tender`). Off by default. One request a minute (robots.txt Crawl-delay: 60), a daily job, no browser;
    # the host reaches the site only through the owner's direct route and the Минцифры root CA (certs/).
    zakupki_enabled: bool = False
    zakupki_hour: int = 5                  # local time of the daily job (~25 min: feeds + pages, one a minute)
    zakupki_minute: int = 20
    zakupki_request_gap_s: float = 61.0
    zakupki_timeout_s: float = 60.0
    zakupki_pages_per_run: int = 16        # notice pages per job: results page + contract card = 2 per winner
    zakupki_max_tries: int = 6             # daily looks for a contract before a completed notice is given up

    # AI triage of search cards before opening vacancy pages
    triage_batch_size: int = 30

    # Company leads (v9.13, decision #53): companies that could hand the programming part to a contractor, found
    # through vacancies that are not for a programmer (panel builders, design bureaus) or through the ОВЕН integrator
    # catalogue. Channels: panel, design (hh.ru search passes, COMPANY_QUERIES), owen_si (the catalogue) and, since
    # v9.15 (decision #55), plant — companies whose vacancy the card triage closed as "operations / КИПиА /
    # electrical on a production site": they run automation without a programmer of their own.
    company_channels: str = "panel,design,owen_si,plant"
    # How many plant companies leave the pool (`skipped/plant_pool`) for a vacancy page per day. Must stay below the
    # day's capacity for vacancy pages: they go in at triage priority 3 and are written off unopened after
    # low_priority_ttl_days.
    plant_leads_per_day: int = 40
    # How many catalogue companies may enter evaluation per day — 230 integrators at once would crowd the queue.
    # 5 → 25 in v9.14 (decision #54): the catalogue costs no page loads, only bridge time, and that is now
    # capped by letters_budget_min; at 5 a day the 167 waiting integrators would have taken until November.
    company_leads_per_day: int = 25
    # Web discovery of panel builders through the bridge (v9.41, decision #75): one web-enabled bridge call per task
    # (a region × a query, or a vendor's partner list), the answer's companies become `new` rows of `site='web'`,
    # channel `discovery`. Off by default; `discovery` must also be in COMPANY_CHANNELS for the daily admission.
    discovery_enabled: bool = False
    discovery_tasks_per_day: int = 3          # bridge calls per daily job (~3–6 min and ~$0.3–0.8 each)
    discovery_leads_per_day: int = 10         # rows let into evaluation per day (`repo.admit_company_leads`)
    discovery_hour: int = 5                   # after the procurement job (05:20, ~25 min), before the 08–11 window
    discovery_minute: int = 50
    discovery_max_turns: int = 12             # CLI turns: searches + page reads + the answer; the bridge caps it at 12 (422 above)
    discovery_timeout_s: float = 930.0        # must exceed the bridge's BRIDGE_WEB_TIMEOUT (900)
    discovery_model: str = ""                 # empty -> the bridge's own model
    owen_integrators_url: str = "https://owen.ru/upl_files/modules/system_integrators/client/integrators.php"
    owen_integrators_referer: str = "https://owen.ru/spisok_sistemnih_integratorov"
    # Company lead score = fit (is there programming work here that can be contracted out) + lead (direct company,
    # scale, stack); no "role" dimension — the vacancy that revealed the company is not for a programmer.
    weight_company_fit: float = 0.6
    weight_company_lead: float = 0.4

    # Lead scoring (the AI returns sub-scores, the code computes the total).
    # Vacancies are leads for the owner's ИП contracting: salary and work format do not score.
    # 60 → 50 on 2026-09-20 (decision #52): the owner would rather write to a "near lead" than to nobody —
    # the 45–59 band carried real pitches and was written off every noon. Bands 50–59 are measured separately in /stats.
    score_threshold: int = 50
    # The daily floor (decision #52): if fewer than this many leads went out in the 24 h before the noon digest,
    # the digest tops up from the best evaluated vacancies below the threshold (not an agency, role ≥ floor_min_role,
    # total ≥ floor_min_total), including ones already written off within floor_lookback_days. 0 disables it.
    daily_letters_floor: int = 5
    floor_min_total: int = 40
    floor_min_role: int = 40
    floor_lookback_days: int = 3
    weight_tech: float = 0.55   # CODESYS/ST/MasterSCADA/PLC programming match
    weight_role: float = 0.25   # they need a programmer (not designer / maintenance / sales)
    weight_lead: float = 0.20   # direct employer, contract-friendly signals

    # Home regions (v9.43, decision #77): while the owner is away he takes fewer leads of a higher grade — only from
    # Moscow, the Moscow region and its neighbours. hh.ru region names, comma-separated (resolved through the cached
    # /areas dictionary like REGION_NAMES); empty — the whole country. The search asks hh.ru for these areas only,
    # the rules skip every row from elsewhere (`skipped/outside_home:<место>`, `pipeline/home_region.py`), and what was
    # collected before is withdrawn. HOME_EXTRA_PLACES — words of towns that do not name their region («Люберцы»)
    # but count as home when a source gives only a city (ОВЕН catalogue, procurement addresses, «Работа России»).
    home_regions: str = ""
    home_extra_places: str = ""

    # The owner is away (v9.42, decision #76): from away_lead_days before away_from through away_until every letter
    # ends with a postscript — where he is, the time difference with Moscow, the phone is probably unreachable, write
    # to e-mail. The code appends it at the Telegram door (`ranker.away_note` / `format_letter`), not the model: a
    # letter written before the trip and sent from the queue gets it too, and the four-line signature check still
    # sees its lines. Empty dates — no postscript. Starts before the departure because an answer to a letter sent
    # the day before lands during the trip.
    away_from: date | None = None
    away_until: date | None = None
    away_where: str = "в командировке"     # prepositional phrase: «в командировке в Китае»
    away_tz_shift_h: int = 0                # hours ahead of Moscow (negative — behind); 0 — the clause is omitted
    away_lead_days: int = 3

    @property
    def home_region_names(self) -> tuple[str, ...]:
        return tuple(x.strip() for x in self.home_regions.split(",") if x.strip())

    @property
    def home_extra_place_words(self) -> tuple[str, ...]:
        return tuple(x.strip() for x in self.home_extra_places.split(",") if x.strip())

    @field_validator("tg_owner_chat_id", "away_from", "away_until", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        return None if v in ("", None) else v

    # The pacing knobs are the account's safety margin: a typo in .env must not silently turn the bot into
    # a metronome with zero pauses (v9.11). Bursts and gaps are already checked by `_parse_minutes_range`.
    @field_validator("page_delay_min_s")
    @classmethod
    def _delay_min(cls, v: float) -> float:
        if v < 3:
            raise ValueError("PAGE_DELAY_MIN_S: пауза чтения меньше 3 с — это не человек")
        return v

    @field_validator("page_delay_max_s", "long_read_max_s")
    @classmethod
    def _delay_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("пауза должна быть больше нуля")
        return v

    @field_validator("long_read_every")
    @classmethod
    def _long_read_every(cls, v: int) -> int:
        if v < 1:
            raise ValueError("LONG_READ_EVERY должен быть не меньше 1")
        return v

    @field_validator("details_budget_share")
    @classmethod
    def _share(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("DETAILS_BUDGET_SHARE должен быть долей от 0 до 1")
        return v

    @field_validator("details_channel_shares")
    @classmethod
    def _channel_shares(cls, v: str) -> str:
        _parse_shares(v)
        return v

    def model_post_init(self, __context: object) -> None:
        if self.page_delay_max_s < self.page_delay_min_s:
            raise ValueError("PAGE_DELAY_MAX_S меньше PAGE_DELAY_MIN_S")
        if self.long_read_max_s < self.long_read_min_s:
            raise ValueError("LONG_READ_MAX_S меньше LONG_READ_MIN_S")

    @property
    def details_channel_shares_map(self) -> dict[str, float]:
        return _parse_shares(self.details_channel_shares)

    @property
    def company_channels_set(self) -> frozenset[str]:
        return frozenset(c.strip() for c in self.company_channels.split(",") if c.strip())

    @property
    def digest_time_parsed(self) -> time:
        return _parse_hhmm(self.digest_time)

    @property
    def crawl_windows_parsed(self) -> list[tuple[time, time]]:
        return parse_windows(self.crawl_windows)

    @property
    def search_passes_set(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.search_passes.split(",") if p.strip())

    @property
    def quiet_hours_parsed(self) -> tuple[time, time] | None:
        return parse_span(self.quiet_hours)

    @property
    def burst_seconds(self) -> tuple[float, float]:
        return _parse_minutes_range(self.burst_minutes)

    @property
    def gap_seconds(self) -> tuple[float, float]:
        return _parse_minutes_range(self.gap_minutes)


def _parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm), tzinfo=TZ)


def _parse_shares(value: str) -> dict[str, float]:
    """'vacancy:0.45,plant:0.25' -> {'vacancy': 0.45, 'plant': 0.25}; shares 0..1 summing to at most 1."""
    shares: dict[str, float] = {}
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, num = chunk.partition(":")
        try:
            share = float(num)
        except ValueError:
            raise ValueError(f"DETAILS_CHANNEL_SHARES: {chunk!r} — нужно «канал:доля»") from None
        if not name.strip() or not 0.0 <= share <= 1.0:
            raise ValueError(f"DETAILS_CHANNEL_SHARES: {chunk!r} — нужно «канал:доля», доля от 0 до 1")
        shares[name.strip()] = share
    if sum(shares.values()) > 1.0 + 1e-9:
        raise ValueError("DETAILS_CHANNEL_SHARES: сумма долей больше 1")
    return shares


def parse_windows(value: str) -> list[tuple[time, time]]:
    """'07:00-10:00,12:00-15:00' -> sorted, non-overlapping (start, end) pairs; raises ValueError otherwise."""
    windows: list[tuple[time, time]] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        start, end = chunk.split("-")
        a, b = _parse_hhmm(start), _parse_hhmm(end)
        if a >= b:
            raise ValueError(f"окно сбора {chunk!r}: начало не раньше конца")
        windows.append((a, b))
    if not windows:
        raise ValueError("CRAWL_WINDOWS пуст")
    windows.sort()
    for (_, prev_end), (nxt_start, _) in zip(windows, windows[1:]):
        if nxt_start < prev_end:
            raise ValueError("окна сбора пересекаются")
    return windows


def parse_span(value: str) -> tuple[time, time] | None:
    """'23:00-07:00' -> (start, end); may cross midnight, unlike a crawl window. Empty -> None."""
    value = (value or "").strip()
    if not value:
        return None
    start, end = value.split("-")
    a, b = _parse_hhmm(start), _parse_hhmm(end)
    if a == b:
        raise ValueError(f"интервал {value!r}: начало совпадает с концом")
    return a, b


def in_span(now: time, span: tuple[time, time] | None) -> bool:
    """Whether `now` falls inside `span`; a span whose end is before its start wraps past midnight."""
    if span is None:
        return False
    a, b = (t.replace(tzinfo=None) for t in span)
    now = now.replace(tzinfo=None)
    return a <= now < b if a < b else (now >= a or now < b)


def _parse_minutes_range(value: str) -> tuple[float, float]:
    """'7-13' -> (420.0, 780.0) seconds."""
    lo, hi = (float(x) for x in value.split("-"))
    if lo <= 0 or hi < lo:
        raise ValueError(f"диапазон минут {value!r} некорректен")
    return lo * 60, hi * 60


def load_settings() -> Settings:
    return Settings()


# --- Search tuning -----------------------------------------------------------

# Composite hh.ru queries (hh query language: OR, quotes, parentheses).
# Fewer, broader queries beat dozens of narrow ones: same recall, fewer requests.
SEARCH_QUERIES: tuple[str, ...] = (
    '("АСУ ТП" OR ПЛК OR PLC OR CODESYS OR SCADA OR MasterSCADA OR "Master SCADA" '
    'OR "промышленной автоматизации" OR "промышленной автоматики")',
    '("инженер по автоматизации" OR "инженер-программист" OR "Automation Engineer" OR "Controls Engineer" '
    'OR "Control Systems Engineer") AND (ПЛК OR PLC OR контроллер OR "технологического оборудования" OR SCADA OR HMI)',
    '(КИПиА OR "шкафов управления" OR "систем управления") AND (программирование OR ПЛК OR PLC OR разработка)',
    # The same work named through the vendor: many ads never say "ПЛК" or "АСУ ТП", only the brand. The list
    # follows the evaluation prompt: the core platforms and the "single foreign environment" band (Allen-Bradley,
    # TwinCAT, EcoStruxure/SoMachine, ТЕКОН, REGUL, Berghof) — a vacancy the prompt can score is a vacancy the
    # search must be able to find (v9.11).
    '(Siemens OR "TIA Portal" OR Step7 OR "Step 7" OR WinCC OR ОВЕН OR Beckhoff OR TwinCAT OR Wago '
    'OR "Schneider Electric" OR EcoStruxure OR SoMachine OR Omron OR Mitsubishi OR Segnetics OR Delta '
    'OR "Allen-Bradley" OR Rockwell OR "Studio 5000" OR RSLogix OR ТЕКОН OR REGUL OR Berghof) '
    'AND (программирование OR программист OR контроллер OR ПЛК OR АСУ OR наладка)',

    # Protocols and the upper level (диспетчеризация / АСКУЭ / телемеханика).
    '(Modbus OR "OPC UA" OR Profinet OR Profibus OR "верхний уровень" OR диспетчеризация '
    'OR АСКУЭ OR телемеханика) AND (АСУ OR ПЛК OR SCADA OR программирование OR инженер)',
)

# Company channels on hh.ru (v9.13, decision #53): the vacancy is only the way to see the company. A panel builder
# hiring an assembler has customers who want a working program in the cabinet; a design bureau hiring a designer has
# projects that someone must program and commission. One task per channel per sitting, all of Russia.
# «Работа России» text queries (v9.25): the API searches the full text, one word or phrase per call, no OR.
# Broad trade names («КИПиА», «инженер-электроник») are left out on purpose — they bring thousands of
# maintenance vacancies; those companies reach the `plant` pool through the evaluator anyway.
TRUDVSEM_QUERIES: tuple[str, ...] = (
    "АСУ ТП", "АСУТП", "ПЛК", "SCADA", "CODESYS", "MasterSCADA", "ОВЕН", "программист контроллеров",
    "промышленной автоматизации", "автоматизации технологических процессов",
)
# «Работа России» queries that find panel builders (v9.40, decision #75): a company hiring a cabinet assembler is
# a company lead of the `panel` channel, like the hh.ru pass of COMPANY_QUERIES — the portal hands its e-mail over
# at once. One phrase per call; the rows go in as `lead_kind='company'`, `search_pass='panel'`, skipping the title rule.
TRUDVSEM_COMPANY_QUERIES: tuple[str, ...] = (
    "сборщик щитов", "сборщик шкафов", "электромонтажник щитов", "сборщик НКУ", "электромонтажник-сборщик",
)

# zakupki.gov.ru search phrases (v9.26): the notice search is full-text over attachments too, so short trade words
# («АСУ ТП», «ПЛК») drown in road works and homonyms; `zakupki.relevant()` then filters by the object's name.
ZAKUPKI_QUERIES: tuple[str, ...] = (
    "SCADA", "MasterSCADA", "диспетчеризации", "автоматизированной системы управления технологическим",
    "шкаф управления", "телемеханики",
    # v9.40 (decision #75): a supplier of cabinets / switchgear is a panel builder with a signed order
    "НКУ", "щит управления", "низковольтное комплектное устройство",
)

# Web discovery of panel builders (v9.41, decision #75): a task is a region × a query, or a vendor whose partner
# programme lists licensed panel builders. Vendors go first (a dozen tasks), then every region with the first query,
# then the next query, round-robin over days (kv `discovery_cursor`).
DISCOVERY_REGION_QUERIES: tuple[str, ...] = (
    "производство шкафов автоматики", "сборка НКУ", "изготовление щитов управления", "сборка шкафов управления",
)
DISCOVERY_VENDORS: tuple[str, ...] = (
    "Schneider Electric", "ABB", "Rittal", "Siemens", "IEK", "EKF", "DKC", "КЭАЗ", "Chint", "ТДМ Электрик", "ОВЕН",
)
DISCOVERY_VENDOR_HINT = "лицензированные / сертифицированные сборщики НКУ, партнёры-сборщики, авторизованные щитовые производства"
DISCOVERY_EXTRA_REGIONS: tuple[str, ...] = (
    "Свердловская область", "Челябинская область", "Тюменская область", "Ханты-Мансийский автономный округ",
    "Ямало-Ненецкий автономный округ", "Курганская область", "Новосибирская область", "Красноярский край", "Омская область",
    "Кемеровская область", "Иркутская область", "Томская область", "Алтайский край", "Хабаровский край", "Приморский край",
    "Мурманская область", "Республика Дагестан", "Кабардино-Балкарская Республика", "Республика Северная Осетия",
)

# Queue priority bonus by company kind (v9.38, decision #74): the owner prefers panel builders and small firms
# without a programmer of their own over holdings and large plants with an АСУ ТП department. Added to
# `evaluations.total` in `repo.PRIORITY_SQL` — it orders the queue (letters, instant sends, /next, the digest
# tail) but does not change the score or the threshold. Kinds not listed get 0.
KIND_PRIORITY_BONUS: dict[str, int] = {"panel_builder": 15, "design_bureau": 10, "integrator": 5}

COMPANY_QUERIES: dict[str, str] = {
    "panel": '("сборщик шкафов" OR "сборщик щитов" OR "сборщик электрощитового" OR "электромонтажник шкафов" '
             'OR "электромонтажник щитов" OR "электромонтажник-сборщик" OR "шкафов автоматики" OR "шкафов управления" '
             'OR "щитовое оборудование" OR НКУ OR "щитов автоматики")',
    "design": '("инженер-проектировщик" OR проектировщик) AND ("АСУ ТП" OR "систем автоматизации" '
              'OR "систем автоматики" OR КИПиА OR автоматизации)',
}

# Fallback geography for SEARCH_ALL_RUSSIA=false. Region names resolved through GET /areas and cached in the DB.
# Was the default until 2026-09-13 (owner's decision 2026-09-08: European Russia, roughly 1300 km from Moscow);
# dropped because the owner contracts remotely and this perimeter saturated in four days — see docs/DECISIONS.md.
# Names must match hh.ru's /areas exactly (see tests/fixtures/areas_russia.json).
REGION_NAMES: tuple[str, ...] = (
    # Центральный ФО
    "Москва", "Московская область", "Белгородская область", "Брянская область", "Владимирская область",
    "Воронежская область", "Ивановская область", "Калужская область", "Костромская область", "Курская область",
    "Липецкая область", "Орловская область", "Рязанская область", "Смоленская область", "Тамбовская область",
    "Тверская область", "Тульская область", "Ярославская область",
    # Северо-Западный ФО (без Мурманской области и Ненецкого АО)
    "Санкт-Петербург", "Ленинградская область", "Архангельская область", "Вологодская область",
    "Калининградская область", "Новгородская область", "Псковская область", "Республика Карелия", "Республика Коми",
    # Приволжский ФО
    "Нижегородская область", "Кировская область", "Республика Марий Эл", "Республика Мордовия",
    "Чувашская Республика", "Республика Татарстан", "Удмуртская Республика", "Пермский край",
    "Республика Башкортостан", "Оренбургская область", "Самарская область", "Саратовская область",
    "Ульяновская область", "Пензенская область",
    # Юг (без кавказских республик и новых территорий)
    "Ростовская область", "Краснодарский край", "Республика Адыгея", "Волгоградская область",
    "Астраханская область", "Республика Калмыкия", "Ставропольский край",
)
# The home perimeter the owner chose for his October 2026 trip (decision #77): Moscow, the Moscow region and the
# regions bordering it. The value of HOME_REGIONS in his .env; kept here so the names are checked against the hh.ru
# dictionary by the tests. Towns of the Moscow region that a source may name without the region go to HOME_EXTRA_PLACES.
HOME_REGIONS_CENTRAL: tuple[str, ...] = (
    "Москва", "Московская область", "Тверская область", "Ярославская область", "Владимирская область",
    "Рязанская область", "Тульская область", "Калужская область", "Смоленская область",
)
HOME_EXTRA_PLACES_MOSCOW: tuple[str, ...] = (
    "Подольск", "Химки", "Мытищи", "Балашиха", "Королёв", "Королев", "Люберцы", "Одинцово", "Красногорск", "Домодедово",
    "Щёлково", "Щелково", "Электросталь", "Серпухов", "Коломна", "Пушкино", "Жуковский", "Раменское", "Долгопрудный",
    "Реутов", "Сергиев Посад", "Орехово-Зуево", "Ногинск", "Фрязино", "Дубна", "Чехов", "Ивантеевка", "Клин",
    "Дмитров", "Ступино", "Павловский Посад", "Наро-Фоминск", "Видное", "Лобня", "Егорьевск", "Солнечногорск",
    "Истра", "Зеленоград", "Троицк", "Щербинка", "Лыткарино", "Воскресенск", "Кашира", "Можайск", "Волоколамск",
)

# Control values for cache validation (documented ids on hh.ru).
KNOWN_AREA_IDS: dict[str, int] = {"Москва": 1, "Московская область": 2019}

# Regions the owner does not work with at all (decision #65, 2026-09-27): Crimea (Sevastopol is inside it on hh.ru)
# and the four regions annexed in 2022. A vacancy there is skipped by the rules stage before any AI or page load,
# whatever its score, and a lead already in the queue is withdrawn. Keyed by the hh.ru area id of the region: every
# card and vacancy page carries `area.path` (".113.225.2114.131." for Симферополь, ".113.2173.123." for Луганск —
# the federal district is not always in it), so a city is matched through its region id, never by name:
# «Донецк (Ростовская область)» stays. Ids from api.hh.ru/areas (tests/fixtures/areas_russia.json).
BLOCKED_REGIONS: dict[int, str] = {
    2114: "Республика Крым",
    2134: "Донецкая Народная Республика",
    2155: "Запорожская область",
    2173: "Луганская Народная Республика",
    2209: "Херсонская область",
}

# Defence industry is off limits (decision #72, 2026-09-29): the owner does not write to military enterprises, makers
# of weapons or defence systems, and — his choice, deliberately wide — to any structure of a holding with a defence
# wing, even when the vacancy itself is about civil products. Matched case-insensitively (ё = е) against the employer
# name of every source (hh.ru card, «Работа России», ОВЕН catalogue, procurement winner) and against a procurement's
# customer; a hit is `skipped/defense:name:<entry>` before any AI or page load. Two lists, both edited by the owner:
# `DEFENSE_EMPLOYERS` are substrings of company names (holdings, plants, agencies), `DEFENSE_EMPLOYER_WORDS` the
# same but whole words only, `DEFENSE_WORD_STEMS` are word stems matched at the start of a word («оборон» hits
# «Оборонэнерго» and «оборонный», not «самооборона»).
# Abbreviations in `DEFENSE_ABBREVIATIONS` count only as a whole word in capitals: «Росконтроль» must not hit «ОСК».
# What the name does not show — «входит в Ростех» on the company site, гособоронзаказ in the description — the
# triage/evaluation flags and the dossier catch (`llm/schemas.py`); a wrong hit is undone with
# `prefilter --requeue-reason 'defense%'`.
DEFENSE_EMPLOYERS: tuple[str, ...] = (
    "алмаз-антей", "алмаз – антей", "алмаз — антей", "алмаз антей", "концерн вко",
    "объединенная авиастроительная", "компания сухой", "ркк миг", "рск миг", "туполев", "ильюшин", "корпорация иркут",
    "объединенная двигателестроительная", "вертолеты россии", "радиоэлектронные технологии", "швабе", "росэлектроника",
    "концерн техмаш", "технодинамика", "высокоточные комплексы", "тактическое ракетное вооружение", "уралвагонзавод",
    "курганмашзавод", "концерн калашников", "тульский оружейный", "ижмаш", "мотовилихинские заводы", "нпо сплав",
    "нпо машиностроения", "объединенная судостроительная", "севмаш", "адмиралтейские верфи", "балтийский завод",
    "северная верфь", "цс звездочка", "амурский судостроительный", "зеленодольский завод", "средне-невский",
    "грц макеева", "институт теплотехники", "оборонэнерго", "оборонлогистика", "военторг",
    "военно-строительн", "росгвардия", "росгвардии", "федеральная служба безопасности", "федеральная служба охраны",
    "войск национальной гвардии", "минобороны", "министерство обороны", "министерства обороны", "войсковая часть",
    "в/ч ", "ракетно-артиллер", "ракетных войск", "ракетного вооружения",
)
# Names that are also the start of harmless words: matched as a whole word only («Ростех», not «Ростехнадзор» or
# «РосТехЭнерго»; «Иркут», not «Иркутск»; «Спецстрой», not «СпецСтройМашина»).
DEFENSE_EMPLOYER_WORDS: tuple[str, ...] = ("ростех", "иркут", "спецстрой", "техмаш", "калашников")
DEFENSE_WORD_STEMS: tuple[str, ...] = (
    "оборон", "вооруж", "боеприпас", "военн", "патронн", "спецсвяз", "гособоронзаказ", "оружейн", "бронетанк", "минобор",
)
DEFENSE_ABBREVIATIONS: tuple[str, ...] = ("ОАК", "ОСК", "УВЗ", "КТРВ", "ОДК", "КРЭТ", "ВПК", "ФСБ", "ФСО", "ГВСУ", "ВКО")

# Conservative stop words for the local prefilter. When in doubt, let the AI decide.
# Matched case-insensitively against the vacancy title only.
TITLE_STOP_WORDS: tuple[str, ...] = (
    "1с", "1c", "водитель", "менеджер по продажам", "оператор call", "оператор колл",
    "продавец", "кладовщик", "грузчик", "бухгалтер", "юрист", "курьер", "охранник",
    "уборщ", "повар", "hr ", "рекрутер", "маркетолог", "smm", "копирайтер",
)
# Titles containing any of these are never stopped even if a stop word matched.
TITLE_KEEP_WORDS: tuple[str, ...] = ("программ", "плк", "plc", "асу", "scada", "codesys", "автоматиз", "автоматик")
# A title must contain at least one of these to be worth the AI's attention (engineering vocabulary).
TITLE_REQUIRED_ANY: tuple[str, ...] = (
    "инженер", "программ", "автоматиз", "автоматик", "асу", "плк", "plc", "scada", "codesys", "кип", "наладчик",
    "разработчик", "техник", "электроник", "embedded", "engineer", "automation", "controls",
)

GROSS_TO_NET = 0.87  # approximation; progressive НДФЛ since 2025 makes it slightly optimistic above 200k/month
