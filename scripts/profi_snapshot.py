"""Save the HTML of profi.ru tabs that are already open in the owner's Firefox — without loading anything.

Purpose: stage 0 of the profi.ru source (docs/ROADMAP.md). The orders feed is only visible after login and is rendered
by JavaScript, so a real snapshot is needed before writing a parser. This script only reads what the browser already
shows: it walks the open tabs, and for every tab whose URL is on profi.ru it writes `page_source` to
data/profi_snapshot_<N>_<timestamp>.html plus a small .json with url/title. No navigation, no clicks, zero page loads.

Usage:
  1. In Firefox (the one started with --marionette) log in to profi.ru, open the orders feed in one tab
     and one order card in another tab. Make sure no crawl is running: bash scripts/svc.sh status -> «сбор идёт: нет».
  2. .venv/bin/python scripts/profi_snapshot.py
Exit code 0 = at least one snapshot written, 1 = browser problem or no profi.ru tab found.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hh_scout.browser.session import BrowserSession, BrowserUnavailable  # noqa: E402
from hh_scout.config import load_settings  # noqa: E402
from hh_scout.db import open_db  # noqa: E402
from hh_scout.logging_setup import setup_logging  # noqa: E402
from hh_scout.pipeline import repo  # noqa: E402

HOST_MARK = "profi.ru"


def main() -> int:
    settings = load_settings()
    setup_logging("WARNING")
    conn = open_db(settings.db_path)
    if repo.running_run(conn):
        print("ОТКАЗ: сейчас идёт сбор (runs.status='running') — Marionette держит одну сессию. Дождитесь конца подхода.")
        return 1
    conn.close()

    out_dir = Path(settings.db_path).resolve().parent
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    written = 0
    try:
        with BrowserSession(settings) as b:
            d = b.driver
            original = d.current_window_handle
            handles = list(d.window_handles)
            print(f"Открытых вкладок/окон: {len(handles)}")
            for h in handles:
                d.switch_to.window(h)
                url, title = d.current_url, d.title
                if HOST_MARK not in url:
                    continue
                written += 1
                html_path = out_dir / f"profi_snapshot_{written}_{stamp}.html"
                html_path.write_text(d.page_source, encoding="utf-8")
                meta = {"url": url, "title": title, "saved_at": stamp, "bytes": html_path.stat().st_size}
                html_path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[{written}] {title[:70]!r}\n     {url}\n     -> {html_path} ({meta['bytes']} байт)")
            try:
                d.switch_to.window(original)
            except Exception:  # noqa: BLE001 — the owner may have closed it meanwhile
                pass
    except BrowserUnavailable as e:
        print(f"ОШИБКА: {e}")
        return 1
    if not written:
        print("Вкладок с profi.ru не найдено. Откройте ленту заказов (и карточку заказа) в этом Firefox и повторите.")
        return 1
    print("Готово: страницы не загружались, только прочитаны из открытых вкладок. Файлы лежат в data/ (в .gitignore).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
