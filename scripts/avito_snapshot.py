#!/usr/bin/env python
"""Load the Avito search pages once and save their rendered HTML — stage 0 of the Avito source (v9.14).

Avito is a JS application: from the host `curl` gets an empty shell (`captcha_enabled`, zero cards), so the pages
can only be read in the owner's logged-in Firefox. This script opens its own window, loads one search page per
channel, saves `page_source` to `data/avito_snapshot_<channel>_<stamp>.html` (+ a small .json with url/title)
and prints what it found. The parser (`avito/pages.py`) is then written from that real DOM, not from guesswork.

Read-only, like everything else on other people's sites: no clicks, no messages, no ad pages. The loads are
counted in the daily cap (a `runs` row), and the script refuses to start while a crawl is running — Marionette
holds a single session (CLAUDE.md rule 4).

    .venv/bin/python scripts/avito_snapshot.py [--channel jobs|services|equipment] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hh_scout.browser.session import BrowserSession, BrowserUnavailable, WindowRegistry  # noqa: E402
from hh_scout.config import AVITO_QUERIES, load_settings                                  # noqa: E402
from hh_scout.db import open_db                                                            # noqa: E402
from hh_scout.logging_setup import setup_logging                                           # noqa: E402
from hh_scout.pipeline import repo                                                         # noqa: E402
from hh_scout.pipeline.budget import daily_cap                                             # noqa: E402

log = logging.getLogger("avito_snapshot")

# What tells us the search page actually rendered, or that Avito wants a captcha instead. Either way the page
# is done loading and worth saving — the whole point of the snapshot is to see which one we get.
WAIT_MARKERS = ('data-marker="item"', "data-marker=\\'item\\'", "Ничего не найдено",
                "Подтвердите, что вы не робот", "Доступ ограничен", "captcha")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", choices=sorted(AVITO_QUERIES), action="append",
                    help="only this channel (repeatable); default — all three")
    ap.add_argument("--out", help="where to write the snapshots (default: next to the database, data/)")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    if repo.running_run(conn):
        print("ОТКАЗ: сейчас идёт сбор (runs.status='running') — Marionette держит одну сессию. "
              "Дождитесь конца подхода: bash scripts/svc.sh status → «сбор идёт: нет».")
        return 2

    channels = args.channel or list(AVITO_QUERIES)
    used, cap = repo.page_loads_today(conn), daily_cap(conn, settings)
    budget = max(0, min(len(channels), cap - used))
    print(f"Загрузок сегодня {used} из {cap}; снимаю {budget} стр.")
    if budget == 0:
        print("Дневной лимит загрузок исчерпан — снимок подождёт завтра.")
        return 1

    out_dir = Path(args.out) if args.out else Path(settings.db_path).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = repo.start_run(conn, "manual")
    rng = random.Random()
    written = 0
    try:
        with BrowserSession(settings, page_budget=budget, rng=rng, registry=WindowRegistry(conn, reap=True)) as b:
            for channel in channels[:budget]:
                url = AVITO_QUERIES[channel]
                html = b.open_raw(url, wait_for=WAIT_MARKERS)
                written += 1
                d = b.driver
                path = out_dir / f"avito_snapshot_{channel}_{stamp}.html"
                path.write_text(html, encoding="utf-8")
                meta = {"channel": channel, "requested_url": url, "final_url": d.current_url, "title": d.title,
                        "saved_at": stamp, "bytes": path.stat().st_size,
                        "cards": html.count('data-marker="item"'),
                        "looks_blocked": any(m in html for m in ("Подтвердите, что вы не робот", "Доступ ограничен"))}
                path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[{channel}] карточек {meta['cards']}, блок: {meta['looks_blocked']}, {meta['bytes']} байт\n"
                      f"     {meta['title'][:70]!r}\n     {meta['final_url'][:110]}\n     -> {path}")
    except BrowserUnavailable as e:
        repo.finish_run(conn, run_id, "failed", error=str(e)[:500], page_loads=written)
        print(f"ОШИБКА: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 — a partial snapshot is still worth keeping
        repo.finish_run(conn, run_id, "failed", error=str(e)[:500], page_loads=written)
        log.exception("Снимок Avito прерван")
        return 1
    repo.finish_run(conn, run_id, "ok", page_loads=written)
    print(f"Готово: {written} стр., прогон #{run_id}. Файлы в {out_dir} (в .gitignore).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
