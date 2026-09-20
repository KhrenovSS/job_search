#!/usr/bin/env python
"""One-off: read the owner's responses on hh.ru deeper than a sitting does, and record their outcomes.

A sitting reads `NEGOTIATIONS_PAGES` pages (20 responses each) — enough to keep up, not enough to backfill
the history that accumulated before the loop existed. This walks the whole list once, in the same bursts
and pauses as a sitting, and its pages count against the daily cap like everyone else's (a `runs` row).

Browser rules apply: ONE Marionette session, so the service must not be collecting
(`bash scripts/svc.sh status` → «сбор идёт: нет»); the script refuses to start otherwise. Read-only.

    .venv/bin/python scripts/sync_negotiations.py [--pages N|all] [--budget N]
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hh_scout.browser.session import BrowserSession, WindowRegistry   # noqa: E402
from hh_scout.config import load_settings                               # noqa: E402
from hh_scout.db import open_db                                          # noqa: E402
from hh_scout.logging_setup import setup_logging                         # noqa: E402
from hh_scout.pipeline import negotiations, repo                         # noqa: E402
from hh_scout.pipeline.budget import daily_cap                           # noqa: E402

log = logging.getLogger("sync_negotiations")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pages", default="all", help="сколько страниц списка прочитать (по умолчанию все)")
    ap.add_argument("--budget", type=int, default=12, help="потолок загрузок страниц (по умолчанию 12, не больше остатка дня)")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    pages = 1000 if args.pages == "all" else int(args.pages)
    conn = open_db(settings.db_path)
    if repo.running_run(conn):
        log.error("Сейчас идёт сбор (runs.status='running') — Marionette держит одну сессию. Дождитесь конца подхода.")
        return 2
    used = repo.page_loads_today(conn)
    cap = daily_cap(conn, settings)
    budget = max(0, min(args.budget, cap - used))
    log.info("Загрузок сегодня уже %d из %d, бюджет на эту синхронизацию %d", used, cap, budget)
    if budget == 0:
        log.error("Дневной лимит загрузок исчерпан — синхронизация подождёт завтра")
        return 1
    before = conn.execute("SELECT COUNT(*) FROM vacancies WHERE applied = 1 AND negotiation_state IS NULL").fetchone()[0]
    run_id = repo.start_run(conn, "manual")
    rng = random.Random()
    registry = WindowRegistry(conn, reap=True)
    try:
        sync, stats = negotiations.walk(
            conn, settings, pages=pages, budget=budget, rng=rng,
            session_factory=lambda b: BrowserSession(settings, page_budget=b, rng=rng, registry=registry),
            after_burst=lambda bs: repo.update_run(conn, run_id, page_loads=bs.page_loads))
    except Exception as e:  # noqa: BLE001 — a partial walk is still worth keeping, but the run is failed
        repo.finish_run(conn, run_id, "failed", error=str(e)[:500])
        log.exception("Синхронизация откликов прервана")
        return 1
    repo.finish_run(conn, run_id, "ok", page_loads=stats.page_loads)
    after = conn.execute("SELECT COUNT(*) FROM vacancies WHERE applied = 1 AND negotiation_state IS NULL").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM negotiation_events").fetchone()[0]
    log.info("Готово: страниц %d, откликов синхронизировано %d; без состояния было %d, стало %d; событий в истории %d%s",
             stats.page_loads, sync.synced, before, after, events,
             f"; остановка: {stats.stopped_reason}" if stats.stopped_reason else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
