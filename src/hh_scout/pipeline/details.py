"""Open vacancy pages for `to_fetch` vacancies (approved by triage) and store full descriptions.

Order: triage priority 1→3, then newest first. Runs in bursts like the collector; whatever does
not fit into today's page budget stays `to_fetch` for the next sitting. Priority-3 cards that wait longer than
`LOW_PRIORITY_TTL_DAYS` are dropped (`repo.expire_low_priority`, called by the orchestrator and the CLI).

CLI:  python -m hh_scout.pipeline.details [--budget N] [--gap-scale X] [--no-gaps]
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from hh_scout.browser import pacing
from hh_scout.browser.bursts import run_in_bursts
from hh_scout.browser.hh_pages import PageFormatError, parse_vacancy, vacancy_url
from hh_scout.browser.session import BrowserSession, BrowserUnavailable, HHBlocked
from hh_scout.config import Settings
from hh_scout.pipeline import dedup, repo

log = logging.getLogger(__name__)


@dataclass
class DetailsStats:
    page_loads: int = 0
    bursts: int = 0
    outcomes: Counter = field(default_factory=Counter)  # prefiltered / skipped / format_error
    stopped_reason: str | None = None

    def as_text(self) -> str:
        return f"страниц {self.page_loads}, серий {self.bursts}, итоги {dict(self.outcomes)}" + (
            f", остановка: {self.stopped_reason}" if self.stopped_reason else "")


class DetailsFetcher:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, *, session_factory: Callable[[int], BrowserSession] | None = None,
                 rng: random.Random | None = None, gap_scale: float = 1.0, page_budget: int | None = None,
                 should_stop: Callable[[], bool] | None = None) -> None:
        self.s = settings
        self.conn = conn
        self.rng = rng or random.Random()
        self.gap_scale = gap_scale
        self.page_budget = page_budget if page_budget is not None else settings.daily_page_loads_max
        self.policy = pacing.policy_from_settings(settings)
        self._session_factory = session_factory or (lambda budget: BrowserSession(settings, page_budget=budget, rng=self.rng))
        self._should_stop = should_stop or (lambda: False)
        self.stats = DetailsStats()
        self._done: set[str] = set()

    def _next(self) -> sqlite3.Row | None:
        for row in repo.list_vacancies(self.conn, "to_fetch"):
            if row["hh_id"] in self._done:
                continue
            with self.conn:  # one lead per company: a twin of an existing lead is not worth a page load
                if dedup.skip_if_covered(self.conn, self.s, row) is not None:
                    self.stats.outcomes["duplicate_employer"] += 1
                    continue
            return row
        return None

    def _step(self, session: BrowserSession) -> bool:
        row = self._next()
        if row is None:
            return False
        hh_id = row["hh_id"]
        state = session.open(vacancy_url(hh_id))
        self._done.add(hh_id)
        try:
            detail = parse_vacancy(state)
        except PageFormatError as e:
            log.warning("Вакансия %s: %s — помечаю evaluation_failed", hh_id, e)
            with self.conn:
                repo.set_status(self.conn, hh_id, "evaluation_failed", "no_vacancy_view")
            self.stats.outcomes["format_error"] += 1
            return self._next() is not None
        with self.conn:
            status = repo.save_details(self.conn, detail)
        self.stats.outcomes[status] += 1
        log.info("Вакансия %s «%s» → %s (описание %d симв., навыков %d)", hh_id, detail.title[:50], status,
                 len(detail.description_text), len(detail.key_skills))
        return self._next() is not None

    def run(self, run_id: int | None = None) -> DetailsStats:
        pending = len(repo.list_vacancies(self.conn, "to_fetch"))
        log.info("Описаний к загрузке: %d, бюджет %d", pending, self.page_budget)
        if pending == 0:
            return self.stats

        def after_burst(bs) -> None:
            self.stats.page_loads, self.stats.bursts = bs.page_loads, bs.bursts
            if run_id is not None:
                repo.update_run(self.conn, run_id, prefiltered=self.stats.outcomes["prefiltered"], page_loads=self.stats.page_loads)

        try:
            bs = run_in_bursts(self._step, session_factory=self._session_factory, budget=self.page_budget, policy=self.policy,
                               rng=self.rng, gap_scale=self.gap_scale, should_stop=self._should_stop,
                               after_burst=after_burst, label="описания")
            self.stats.page_loads, self.stats.bursts, self.stats.stopped_reason = bs.page_loads, bs.bursts, bs.stopped_reason
        except BrowserUnavailable as e:
            self.stats.stopped_reason = f"браузер недоступен: {e}"
            log.error("Загрузка описаний прервана: %s", e)
            raise
        except HHBlocked as e:
            self.stats.stopped_reason = f"hh.ru не отдал страницу: {e}"
            log.error("Загрузка описаний остановлена мягко: %s", e)
        log.info("Описания: %s", self.stats.as_text())
        return self.stats


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging
    from hh_scout.pipeline.budget import daily_cap

    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, help="page-load budget for this run (default: today's remaining)")
    ap.add_argument("--gap-scale", type=float, default=1.0)
    ap.add_argument("--no-gaps", action="store_true")
    ap.add_argument("--stale-hours", type=float, default=3.0)
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    with conn:
        repo.fail_stale_runs(conn, args.stale_hours)
    if repo.running_run(conn):
        log.error("Уже есть незавершённый прогон — выходим")
        return 2
    used_today = repo.page_loads_today(conn)
    cap = daily_cap(conn, settings)
    budget = args.budget if args.budget is not None else max(0, cap - used_today)
    log.info("Загрузок сегодня уже %d из %d, бюджет на этот прогон %d", used_today, cap, budget)
    with conn:
        expired = repo.expire_low_priority(conn, settings.low_priority_ttl_days)
    if expired:
        log.info("Списано слабых карточек (приоритет 3 старше %d дн.): %d", settings.low_priority_ttl_days, expired)
    run_id = repo.start_run(conn, "manual")
    started = time.monotonic()
    fetcher = DetailsFetcher(settings, conn, gap_scale=0.0 if args.no_gaps else args.gap_scale, page_budget=budget)
    try:
        stats = fetcher.run(run_id)
    except Exception as e:  # noqa: BLE001
        repo.finish_run(conn, run_id, "failed", error=str(e)[:500], prefiltered=fetcher.stats.outcomes["prefiltered"],
                        page_loads=fetcher.stats.page_loads)
        log.exception("Загрузка описаний упала")
        return 1
    repo.finish_run(conn, run_id, "ok", prefiltered=stats.outcomes["prefiltered"], page_loads=stats.page_loads)
    log.info("Готово за %.0f мин. Статусы: %s", (time.monotonic() - started) / 60, repo.count_by_status(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
