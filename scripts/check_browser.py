"""Check that hh-scout can attach to the owner's Firefox via Marionette.

Usage: .venv/bin/python scripts/check_browser.py
Exit code 0 = OK, 1 = Firefox/geckodriver problem.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hh_scout.browser.session import BrowserSession, BrowserUnavailable  # noqa: E402
from hh_scout.config import load_settings  # noqa: E402
from hh_scout.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    settings = load_settings()
    setup_logging("WARNING")
    try:
        with BrowserSession(settings) as b:
            i = b.info()
            print(f"Firefox {i.browser_version}, Marionette OK ({settings.marionette_host}:{settings.marionette_port})")
            print(f"Профиль: {i.profile}")
            print(f"Окон/вкладок: {i.windows}, активная: {i.title[:60]!r} {i.current_url}")
        print("Отключились, Firefox продолжает работать.")
        return 0
    except BrowserUnavailable as e:
        print(f"ОШИБКА: {e}")
        print("Подсказка: bash scripts/setup_firefox.sh, затем перезапустить Firefox.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
