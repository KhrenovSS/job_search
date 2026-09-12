"""Collect fresh vacancies from hh.ru through the owner's Firefox, gently.

A collection is a list of page-load *tasks* (one per search query × pass, paginated lazily)
executed in short *bursts*: attach to Firefox, load a few pages with human pauses, detach,
sleep tens of minutes, repeat. Everything is idempotent — vacancies are keyed by hh_id and a
re-run only adds what is new.

CLI:  python -m hh_scout.pipeline.collector [--budget N] [--gap-scale 0.3] [--no-gaps]
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from hh_scout.browser import pacing
from hh_scout.browser.bursts import run_in_bursts
from hh_scout.browser.hh_pages import (
    NEGOTIATIONS_URL,
    SearchPage,
    VacancyCard,
    build_search_url,
    parse_negotiations,
    parse_search,
    parse_suitable,
    user_type,
)
from hh_scout.browser.session import BrowserSession, BrowserUnavailable, HHBlocked, PageBudgetExceeded
from hh_scout.config import SEARCH_QUERIES, Settings
from hh_scout.db import transaction
from hh_scout.hh.areas import resolve_region_ids
from hh_scout.pipeline import repo

log = logging.getLogger(__name__)

PASS_REGIONAL = "regional"
PASS_REMOTE = "remote"
PASS_PROJECT = "project"
PASS_GPH = "gph"          # hh filter "Оформление по ГПХ или по совместительству"
PASS_SIMILAR = "similar"


@dataclass
class SearchTask:
    search_pass: str
    query_idx: int
    query: str
    areas: tuple[int, ...] = ()
    work_formats: tuple[str, ...] = ()
    employment_forms: tuple[str, ...] = ()
    accept_temporary: bool = False
    next_page: int = 0
    done: bool = False

    @property
    def source(self) -> str:
        return f"search:{self.query_idx}"


@dataclass
class CollectStats:
    page_loads: int = 0
    cards_seen: int = 0
    new_vacancies: int = 0
    applied_synced: int = 0
    bursts: int = 0
    stopped_reason: str | None = None
    not_logged_in: bool = False

    def as_text(self) -> str:
        return (f"страниц {self.page_loads}, карточек {self.cards_seen}, новых {self.new_vacancies}, "
                f"откликов синхронизировано {self.applied_synced}, серий {self.bursts}"
                + (f", остановка: {self.stopped_reason}" if self.stopped_reason else ""))


def plan_tasks(region_ids: list[int], queries: tuple[str, ...] | None = None, rng: random.Random | None = None) -> list[SearchTask]:
    rng = rng or random.Random()
    queries = SEARCH_QUERIES if queries is None else queries
    tasks: list[SearchTask] = []
    for i, q in enumerate(queries):
        tasks.append(SearchTask(PASS_REGIONAL, i, q, areas=tuple(region_ids)))
        tasks.append(SearchTask(PASS_REMOTE, i, q, work_formats=("REMOTE",)))
        tasks.append(SearchTask(PASS_PROJECT, i, q, employment_forms=("PROJECT", "PART")))
        tasks.append(SearchTask(PASS_GPH, i, q, accept_temporary=True))
    rng.shuffle(tasks)
    return tasks


class Collector:
    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        *,
        session_factory: Callable[[int], BrowserSession] | None = None,
        rng: random.Random | None = None,
        gap_scale: float = 1.0,
        page_budget: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.s = settings
        self.conn = conn
        self.rng = rng or random.Random()
        self.gap_scale = gap_scale
        self.page_budget = page_budget if page_budget is not None else settings.daily_page_loads_max
        self.policy = pacing.policy_from_settings(settings)
        self._session_factory = session_factory or (lambda budget: BrowserSession(settings, page_budget=budget, rng=self.rng))
        self._should_stop = should_stop or (lambda: False)
        self.stats = CollectStats()

    # -- public ------------------------------------------------------------------

    def run(self, run_id: int | None = None) -> CollectStats:
        region_ids = resolve_region_ids(self.conn, self.s)
        tasks = plan_tasks(region_ids, rng=self.rng)
        log.info("План сбора: %d задач, бюджет %d загрузок, регионы %s", len(tasks), self.page_budget, region_ids)
        state = {"negotiations_done": False}

        def step(session: BrowserSession) -> bool:
            if not state["negotiations_done"]:
                self._sync_negotiations(session)
                state["negotiations_done"] = True
            return self._one_page(session, tasks)

        def after_burst(bs) -> None:
            self.stats.page_loads = bs.page_loads
            self.stats.bursts = bs.bursts
            if run_id is not None:
                repo.update_run(self.conn, run_id, collected=self.stats.new_vacancies, page_loads=self.stats.page_loads)
            log.info("Сделано %d/%d задач", len(tasks) - len(self._pending(tasks)), len(tasks))

        try:
            bs = run_in_bursts(step, session_factory=self._session_factory, budget=self.page_budget, policy=self.policy,
                               rng=self.rng, gap_scale=self.gap_scale, should_stop=self._should_stop,
                               after_burst=after_burst, label="сбор")
            self.stats.page_loads, self.stats.bursts, self.stats.stopped_reason = bs.page_loads, bs.bursts, bs.stopped_reason
        except BrowserUnavailable as e:
            self.stats.stopped_reason = f"браузер недоступен: {e}"
            log.error("Сбор прерван: %s", e)
            raise
        except HHBlocked as e:
            self.stats.stopped_reason = f"hh.ru не отдал страницу: {e}"
            log.error("Сбор остановлен мягко: %s", e)
        log.info("Сбор завершён: %s", self.stats.as_text())
        return self.stats

    # -- internals ---------------------------------------------------------------

    @staticmethod
    def _pending(tasks: list[SearchTask]) -> list[SearchTask]:
        return [t for t in tasks if not t.done]

    def _one_page(self, session: BrowserSession, tasks: list[SearchTask]) -> bool:
        """Load one search page for the first pending task. Returns True while tasks remain."""
        pending = self._pending(tasks)
        if not pending:
            return False
        task = pending[0]
        url = build_search_url(
            task.query, areas=task.areas, work_formats=task.work_formats, employment_forms=task.employment_forms,
            accept_temporary=task.accept_temporary,
            period_days=self.s.search_period_days, page=task.next_page, items_on_page=self.s.items_per_page,
        )
        state = session.open(url)  # may raise PageBudgetExceeded -> handled by run_in_bursts
        page = parse_search(state)
        self._note_user_type(page.user_type)
        new_here = self._store_cards(page.cards, task.source, task.search_pass)
        log.info("%s q%d стр.%d: карточек %d, новых %d, всего %d, есть след.: %s",
                 task.search_pass, task.query_idx, task.next_page, len(page.cards), new_here, page.total, page.has_next)
        self._advance(task, page, new_here)
        return bool(self._pending(tasks))

    def _advance(self, task: SearchTask, page: SearchPage, new_here: int) -> None:
        task.next_page += 1
        if not page.has_next or not page.cards:
            task.done = True
        elif task.next_page >= self.s.max_pages_per_query:
            task.done = True
        elif new_here == 0 and task.next_page > 1:
            # sorted by publication time desc: a page with nothing new means the rest is known too
            task.done = True

    def _store_cards(self, cards: list[VacancyCard], source: str, search_pass: str) -> int:
        new = 0
        with transaction(self.conn):
            for card in cards:
                self.stats.cards_seen += 1
                if repo.insert_card(self.conn, card, source, search_pass):
                    new += 1
        self.stats.new_vacancies += new
        return new

    def _sync_negotiations(self, session: BrowserSession) -> None:
        state = session.open(NEGOTIATIONS_URL)
        self._note_user_type(user_type(state))
        items = parse_negotiations(state)
        with transaction(self.conn):
            for n in items:
                repo.mark_applied(self.conn, n.hh_id, has_chat=n.has_messages, title=n.title, employer=n.employer)
        self.stats.applied_synced = len(items)
        similar = parse_suitable(state)
        new_similar = self._store_cards(similar, "similar_to_resume", PASS_SIMILAR)
        log.info("Отклики: %d синхронизировано; подходящих по резюме на странице: %d, новых %d", len(items), len(similar), new_similar)

    def _note_user_type(self, ut: str) -> None:
        if ut != "applicant" and not self.stats.not_logged_in:
            self.stats.not_logged_in = True
            log.warning("hh.ru видит нас как %r — вход в аккаунт в Firefox не выполнен, отклики не видны", ut)


# --- CLI ----------------------------------------------------------------------------

def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging
    from hh_scout.pipeline.budget import daily_cap

    ap = argparse.ArgumentParser(description="Collect vacancies through the owner's Firefox")
    ap.add_argument("--budget", type=int, help="page-load budget for this run (default from .env)")
    ap.add_argument("--gap-scale", type=float, default=1.0, help="multiply pauses between bursts (debug only)")
    ap.add_argument("--no-gaps", action="store_true", help="no pauses between bursts (debug only, NOT for daily use)")
    ap.add_argument("--stale-hours", type=float, default=3.0, help="treat a 'running' run older than this as killed")
    args = ap.parse_args()

    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    with conn:
        stale = repo.fail_stale_runs(conn, args.stale_hours)
    if stale:
        log.warning("Помечено как прерванных прогонов: %d", stale)
    if repo.running_run(conn):
        log.error("Уже есть незавершённый прогон (runs.status=running) — выходим")
        return 2
    used_today = repo.page_loads_today(conn)
    cap = daily_cap(conn, settings)
    budget = args.budget if args.budget is not None else max(0, cap - used_today)
    log.info("Загрузок сегодня уже %d из %d, бюджет на этот прогон %d", used_today, cap, budget)
    run_id = repo.start_run(conn, "manual")
    started = time.monotonic()
    collector = Collector(settings, conn, gap_scale=0.0 if args.no_gaps else args.gap_scale, page_budget=budget)
    try:
        stats = collector.run(run_id)
    except Exception as e:  # noqa: BLE001 — report and mark the run failed
        repo.finish_run(conn, run_id, "failed", error=str(e)[:500], collected=collector.stats.new_vacancies,
                        page_loads=collector.stats.page_loads)
        log.exception("Сбор упал")
        return 1
    repo.finish_run(conn, run_id, "ok", collected=stats.new_vacancies, page_loads=stats.page_loads)
    log.info("Готово за %.0f мин. Статусы в БД: %s", (time.monotonic() - started) / 60, repo.count_by_status(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
