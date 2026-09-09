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
    # Daily page-load cap (search + vacancy pages, all processes): drawn once per day at random from this range
    # and stored in kv `daily_cap:<date>` so the number differs from day to day.
    daily_page_loads_min: int = 100
    daily_page_loads_max: int = 140
    # Rhythm inside a sitting: browse for burst_minutes, stay quiet for gap_minutes, repeat ("MIN-MAX").
    burst_minutes: str = "7-13"
    gap_minutes: str = "4-9"
    items_per_page: int = 50
    max_pages_per_query: int = 6
    low_priority_ttl_days: int = 3  # triage priority 3 cards still unopened after this many days are dropped

    # Claude bridge on the host (bridge/hh_scout_bridge.py)
    bridge_url: str = "http://127.0.0.1:8766"
    bridge_token: str = ""
    bridge_model: str = ""  # empty -> the bridge's own default (BRIDGE_MODEL)
    bridge_timeout_s: float = 180.0

    # Telegram
    tg_bot_token: str = ""
    tg_owner_chat_id: int | None = None

    # Schedule and limits
    digest_time: str = "12:00"  # Europe/Moscow, HH:MM — digest is sent every day at this time
    # Sittings: one crawl per window, its START picked at random inside the window; the daily cap is shared between
    # the sittings still ahead. Comma-separated, sorted, non-overlapping.
    crawl_windows: str = "07:00-10:00,12:00-15:00,18:00-22:00"
    digest_max_items: int = 20
    search_period_days: int = 2
    db_path: Path = PROJECT_ROOT / "data" / "hh_scout.db"
    prompts_dir: Path = PROJECT_ROOT / "prompts"
    log_level: str = "INFO"

    # AI triage of search cards before opening vacancy pages
    triage_batch_size: int = 30

    # Lead scoring (the AI returns sub-scores, the code computes the total).
    # Vacancies are leads for the owner's ИП contracting: salary and work format do not score.
    score_threshold: int = 60
    weight_tech: float = 0.55   # CODESYS/ST/MasterSCADA/PLC programming match
    weight_role: float = 0.25   # they need a programmer (not designer / maintenance / sales)
    weight_lead: float = 0.20   # direct employer, contract-friendly signals
    min_salary_net: int = 120_000  # informational only; no longer used by the prefilter

    @field_validator("tg_owner_chat_id", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        return None if v in ("", None) else v

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
)

# Region names resolved through GET /areas at first run and cached in the DB.
# Geography (owner's decision 2026-09-08): European Russia without the Urals and the Caucasus republics,
# roughly 1300 km from Moscow. Names must match hh.ru's /areas exactly (see tests/fixtures/areas_russia.json).
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
