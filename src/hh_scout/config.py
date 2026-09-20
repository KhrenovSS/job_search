"""All runtime settings in one place.

Secrets and deployment knobs come from `.env` (see `.env.example`).
Search tuning (queries, regions, stop words, score weights) lives here as plain
constants: it changes rarely and is easier to review in code than in env vars.
"""

from __future__ import annotations

from datetime import time
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
    daily_page_loads_min: int = 150   # raised from 100-140 (v9.5): sittings spend their share to the last page
    daily_page_loads_max: int = 200   # (35 / 34 of a cap of 103), so the cap is what limits how many leads appear
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

    # Telegram
    tg_bot_token: str = ""
    tg_owner_chat_id: int | None = None

    # Schedule and limits
    digest_time: str = "12:00"  # Europe/Moscow, HH:MM — digest is sent every day at this time
    # Sittings: one crawl per window, its START picked at random inside the window; the daily cap is shared between
    # the sittings still ahead. Comma-separated, sorted, non-overlapping.
    crawl_windows: str = "07:00-10:00,12:00-15:00,18:00-22:00"
    # The daily quota: how many leads may be sent and letters written in a day, across instant sends and the
    # noon digest together. v9.1 set it to 5; v9.7 doubled it; v9.8 raised it to 20, which at the current supply
    # of 8-12 leads a day holds nothing back — it stays as a fuse against a sudden flood (decision #46).
    digest_max_items: int = 20
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

    # AI triage of search cards before opening vacancy pages
    triage_batch_size: int = 30

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

    @field_validator("tg_owner_chat_id", mode="before")
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

    def model_post_init(self, __context: object) -> None:
        if self.page_delay_max_s < self.page_delay_min_s:
            raise ValueError("PAGE_DELAY_MAX_S меньше PAGE_DELAY_MIN_S")
        if self.long_read_max_s < self.long_read_min_s:
            raise ValueError("LONG_READ_MAX_S меньше LONG_READ_MIN_S")

    @property
    def digest_time_parsed(self) -> time:
        return _parse_hhmm(self.digest_time)

    @property
    def crawl_windows_parsed(self) -> list[tuple[time, time]]:
        return parse_windows(self.crawl_windows)

    @property
    def burst_seconds(self) -> tuple[float, float]:
        return _parse_minutes_range(self.burst_minutes)

    @property
    def gap_seconds(self) -> tuple[float, float]:
        return _parse_minutes_range(self.gap_minutes)


def _parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm), tzinfo=TZ)


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
    "Астраханская область", "Республика Калмыкия", "Республика Крым", "Ставропольский край",
)
# Control values for cache validation (documented ids on hh.ru).
KNOWN_AREA_IDS: dict[str, int] = {"Москва": 1, "Московская область": 2019}

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
