"""Region ids from the open hh.ru dictionary `GET https://api.hh.ru/areas` (no token needed).

Only Russia's first-level regions are cached (`areas_cache`), enough to resolve the names in
`config.REGION_NAMES`. The cache is refreshed when it is empty or older than 30 days.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from hh_scout.config import KNOWN_AREA_IDS, REGION_NAMES, Settings
from hh_scout.db import utcnow

log = logging.getLogger(__name__)

AREAS_URL = "https://api.hh.ru/areas"
RUSSIA_ID = 113
CACHE_TTL = timedelta(days=30)


class AreaResolutionError(RuntimeError):
    pass


def _fetch_russia_regions(user_agent: str) -> list[dict]:
    resp = httpx.get(AREAS_URL, headers={"HH-User-Agent": user_agent, "User-Agent": user_agent}, timeout=30)
    resp.raise_for_status()
    for country in resp.json():
        if int(country.get("id", 0)) == RUSSIA_ID:
            return country.get("areas", [])
    raise AreaResolutionError("в ответе /areas нет России (id 113)")


def refresh_cache(conn: sqlite3.Connection, user_agent: str) -> int:
    regions = _fetch_russia_regions(user_agent)
    now = utcnow()
    with conn:
        conn.execute("DELETE FROM areas_cache")
        conn.executemany(
            "INSERT INTO areas_cache(area_id, name, parent_id, fetched_at) VALUES (?, ?, ?, ?)",
            [(int(r["id"]), r["name"], RUSSIA_ID, now) for r in regions],
        )
    log.info("Кэш регионов обновлён: %d регионов России", len(regions))
    return len(regions)


def _cache_is_fresh(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT MIN(fetched_at) AS f, COUNT(*) AS n FROM areas_cache").fetchone()
    if not row or not row["n"]:
        return False
    fetched = datetime.fromisoformat(row["f"])
    return datetime.now(timezone.utc) - fetched < CACHE_TTL


def resolve_region_ids(conn: sqlite3.Connection, settings: Settings, names: tuple[str, ...] = REGION_NAMES) -> list[int]:
    """Names → ids, refreshing the cache if needed. Validates against KNOWN_AREA_IDS."""
    if not _cache_is_fresh(conn):
        refresh_cache(conn, settings.hh_user_agent)
    rows = conn.execute("SELECT area_id, name FROM areas_cache").fetchall()
    by_name = {r["name"].casefold(): int(r["area_id"]) for r in rows}
    ids: list[int] = []
    missing: list[str] = []
    for name in names:
        area_id = by_name.get(name.casefold())
        if area_id is None:
            missing.append(name)
        else:
            ids.append(area_id)
    for name, expected in KNOWN_AREA_IDS.items():
        got = by_name.get(name.casefold())
        if got is not None and got != expected:
            raise AreaResolutionError(f"контрольный регион {name!r}: ожидали id {expected}, получили {got}")
    if missing:
        raise AreaResolutionError(f"регионы не найдены в справочнике hh.ru: {missing}")
    return ids
