"""Daily page-load cap shared by every process that touches hh.ru (service sittings and manual CLIs).

The cap is drawn once per day at random from `DAILY_PAGE_LOADS_MIN..MAX` and remembered in kv
(`daily_cap:<YYYY-MM-DD>`), so the number of pages differs from day to day. What is already spent today
comes from `repo.page_loads_today` (sum over all runs started today).
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime

from hh_scout.config import TZ, Settings
from hh_scout.db import kv_get, kv_set

KV_PREFIX = "daily_cap:"


def daily_cap(conn: sqlite3.Connection, settings: Settings, rng: random.Random | None = None, today: str | None = None) -> int:
    """Today's cap; drawn and stored on first use, then stable for the day (older keys are dropped)."""
    today = today or datetime.now(TZ).date().isoformat()
    key = KV_PREFIX + today
    raw = kv_get(conn, key)
    if raw:
        return int(raw)
    lo, hi = settings.daily_page_loads_min, settings.daily_page_loads_max
    cap = (rng or random.Random()).randint(min(lo, hi), max(lo, hi))
    with conn:
        conn.execute("DELETE FROM kv WHERE key LIKE ? AND key != ?", (KV_PREFIX + "%", key))
        kv_set(conn, key, str(cap))
    return cap
