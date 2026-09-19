#!/usr/bin/env python
"""One-off: read the owner's responses on hh.ru deeper than a sitting does, and record their outcomes.

A sitting reads `NEGOTIATIONS_PAGES` pages (20 responses each) — enough to keep up, not enough to backfill
the history that accumulated before the loop existed. This walks the whole list once.

Browser rules apply: ONE Marionette session, so the service must not be collecting
(`bash scripts/svc.sh status` → «сбор идёт: нет»). Read-only, like everything else we do on hh.ru.

    .venv/bin/python scripts/sync_negotiations.py [--pages N] [--budget N]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hh_scout.config import load_settings                      # noqa: E402
from hh_scout.db import open_db                                 # noqa: E402
from hh_scout.logging_setup import setup_logging                # noqa: E402
from hh_scout.pipeline.collector import Collector               # noqa: E402

log = logging.getLogger("sync_negotiations")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pages", default="all", help="сколько страниц списка прочитать (по умолчанию все)")
    ap.add_argument("--budget", type=int, default=12, help="потолок загрузок страниц (по умолчанию 12)")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    pages = 1000 if args.pages == "all" else int(args.pages)
    settings = settings.model_copy(update={"negotiations_pages": pages})
    conn = open_db(settings.db_path)
    before = conn.execute("SELECT COUNT(*) FROM vacancies WHERE applied = 1 AND negotiation_state IS NULL").fetchone()[0]

    c = Collector(settings, conn, page_budget=args.budget)
    neg: dict = {"page": 0, "done": False, "seen": set()}
    session = c._session_factory(args.budget)
    try:
        while not neg["done"]:
            c._sync_negotiations_page(session, neg)
    except Exception as e:  # noqa: BLE001 — a partial walk is still worth keeping
        log.warning("Остановились на странице %d: %s", neg["page"], e)
    finally:
        session.close()

    after = conn.execute("SELECT COUNT(*) FROM vacancies WHERE applied = 1 AND negotiation_state IS NULL").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM negotiation_events").fetchone()[0]
    log.info("Готово: страниц %d, откликов синхронизировано %d; без состояния было %d, стало %d; событий в истории %d",
             neg["page"], c.stats.applied_synced, before, after, events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
