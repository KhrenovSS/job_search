"""One full crawl: collect → prefilter → triage → details → evaluate → cover letters.

Blocking (Selenium); the bot calls it via asyncio.to_thread with its own DB connection.
Every step is isolated: a browser failure skips the browser steps but the AI steps still run
on whatever is already in the DB, and vice versa.

A sitting (the scheduler's trigger 'schedule') gets `budget` = its share of what is left of today's cap;
a manual run takes everything that is left.

CLI:  python -m hh_scout.pipeline.run [--trigger manual] [--budget N] [--gap-scale X]
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from hh_scout.browser.session import BrowserUnavailable, HHBlocked
from hh_scout.config import TZ, Settings
from hh_scout.db import open_db
from hh_scout.llm.bridge_client import BridgeError
from hh_scout.llm.cover_letter import CoverLetterWriter
from hh_scout.llm.evaluator import Evaluator
from hh_scout.llm.triage import Triager
from hh_scout.pipeline import prefilter, repo
from hh_scout.pipeline.budget import daily_cap
from hh_scout.pipeline.collector import Collector
from hh_scout.pipeline.details import DetailsFetcher

log = logging.getLogger(__name__)


@dataclass
class CrawlReport:
    trigger: str
    page_loads: int = 0
    used_today: int = 0     # after this run, all processes
    daily_cap: int = 0
    expired_low_priority: int = 0
    new_vacancies: int = 0
    search_pages: int = 0       # for health.analyze_report
    cards_seen: int = 0
    not_logged_in: bool = False
    format_errors: int = 0
    prefiltered_pass: int = 0
    triage_open: int = 0
    details: int = 0
    evaluated: int = 0
    leads: int = 0
    letters: int = 0
    bridge_calls: int = 0
    browser_error: str | None = None
    bridge_error: str | None = None
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    deadline: datetime | None = None   # browsing must stop by this time (sitting: window end + grace)
    deadline_hit: bool = False         # the deadline actually cut the browsing short

    @property
    def ok(self) -> bool:
        return not self.errors and not self.browser_error and not self.bridge_error

    def as_text(self) -> str:
        head = "✅ Сбор завершён" if self.ok else "⚠️ Сбор завершён с замечаниями"
        lines = [f"{head} ({self.trigger}, {self.duration_s / 60:.0f} мин)"
                 + (f" · остановлен по концу окна в {self.deadline.astimezone(TZ):%H:%M}" if self.deadline_hit and self.deadline else ""),
                 f"Страниц: {self.page_loads} (за день {self.used_today}/{self.daily_cap}) · новых вакансий: {self.new_vacancies} · описаний: {self.details}"
                 + (f" · списано слабых: {self.expired_low_priority}" if self.expired_low_priority else ""),
                 f"Оценено: {self.evaluated} · новых лидов: {self.leads} · писем: {self.letters} · вызовов ИИ: {self.bridge_calls}"]
        if self.browser_error:
            lines.append(f"Браузер: {self.browser_error}")
        if self.bridge_error:
            lines.append(f"Мост Claude: {self.bridge_error}")
        lines += [f"Ошибка: {e}" for e in self.errors]
        return "\n".join(lines)


def run_crawl(settings: Settings, db_path: Path | str, trigger: str = "manual", *, budget: int | None = None,
              gap_scale: float = 1.0, should_stop: Callable[[], bool] | None = None, stale_hours: float = 3.0,
              deadline: datetime | None = None) -> CrawlReport:
    """One crawl. `deadline` (aware datetime) ends browsing — bursts stop, evaluation/letters still run."""
    conn = open_db(db_path)
    report = CrawlReport(trigger=trigger, deadline=deadline)
    started = time.monotonic()

    def stop() -> bool:
        if should_stop and should_stop():
            return True
        if deadline is not None and datetime.now(TZ) >= deadline:
            if not report.deadline_hit:
                log.info("Дедлайн подхода %s наступил — браузер больше не открываем", deadline.astimezone(TZ).strftime("%H:%M"))
            report.deadline_hit = True
            return True
        return False

    with conn:
        repo.fail_stale_runs(conn, stale_hours)
    if repo.running_run(conn):
        report.errors.append("уже идёт другой прогон")
        return report
    run_id = repo.start_run(conn, trigger)
    cap = daily_cap(conn, settings)
    used = repo.page_loads_today(conn)
    remaining = max(0, cap - used)
    budget = remaining if budget is None else max(0, min(budget, remaining))
    report.daily_cap = cap
    log.info("Прогон #%d (%s): загрузок сегодня %d из %d, бюджет прогона %d", run_id, trigger, used, cap, budget)
    bridge_calls = 0

    # 1. collect
    if budget > 0:
        try:
            c = Collector(settings, conn, gap_scale=gap_scale, page_budget=budget, should_stop=stop)
            st = c.run(run_id)
            report.page_loads += st.page_loads
            report.new_vacancies = st.new_vacancies
            report.search_pages, report.cards_seen, report.not_logged_in = st.page_loads, st.cards_seen, st.not_logged_in
        except BrowserUnavailable as e:
            report.browser_error = str(e)
        except HHBlocked as e:
            report.browser_error = str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("Сбор упал")
            report.errors.append(f"сбор: {e}")
    else:
        report.browser_error = "дневной лимит загрузок исчерпан до начала прогона"

    # 2. prefilter (no I/O)
    try:
        outcomes = prefilter.run(conn, settings)
        report.prefiltered_pass = outcomes.get("passed", 0)
    except Exception as e:  # noqa: BLE001
        log.exception("Префильтр упал")
        report.errors.append(f"префильтр: {e}")

    # 3. triage (bridge)
    try:
        t = Triager(settings, conn).run()
        report.triage_open = t.opened
        bridge_calls += t.bridge_calls
    except BridgeError as e:
        report.bridge_error = str(e)
    except Exception as e:  # noqa: BLE001
        log.exception("Триаж упал")
        report.errors.append(f"триаж: {e}")

    # 4. details (browser): drop stale low-priority cards first, then spend what is left of this run's budget
    try:
        with conn:
            report.expired_low_priority = repo.expire_low_priority(conn, settings.low_priority_ttl_days)
        if report.expired_low_priority:
            log.info("Списано слабых карточек (приоритет 3 старше %d дн.): %d", settings.low_priority_ttl_days, report.expired_low_priority)
    except Exception as e:  # noqa: BLE001
        log.exception("Списание слабых карточек упало")
        report.errors.append(f"списание: {e}")
    remaining = max(0, budget - report.page_loads)
    if report.browser_error is None and remaining > 0:
        try:
            d = DetailsFetcher(settings, conn, gap_scale=gap_scale, page_budget=remaining, should_stop=stop)
            ds = d.run(run_id)
            report.page_loads += ds.page_loads
            report.details = ds.outcomes.get("prefiltered", 0)
            report.format_errors = ds.outcomes.get("format_error", 0)
        except BrowserUnavailable as e:
            report.browser_error = str(e)
        except HHBlocked as e:
            report.browser_error = str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("Описания упали")
            report.errors.append(f"описания: {e}")

    # 5. evaluate + 6. letters (bridge)
    if report.bridge_error is None:
        try:
            ev = Evaluator(settings, conn).run()
            report.evaluated = ev.evaluated
            bridge_calls += ev.bridge_calls
            report.leads = len(repo.evaluated_leads(conn, settings.score_threshold))
            lw = CoverLetterWriter(settings, conn).run()
            report.letters = lw.written
            bridge_calls += lw.bridge_calls
        except BridgeError as e:
            report.bridge_error = str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("Оценка/письма упали")
            report.errors.append(f"оценка: {e}")

    report.bridge_calls = bridge_calls
    report.used_today = used + report.page_loads
    report.duration_s = time.monotonic() - started
    status = "ok" if report.ok else "failed"
    err = "; ".join(filter(None, [report.browser_error, report.bridge_error, *report.errors]))[:500] or None
    with conn:
        repo.finish_run(conn, run_id, status, error=err, collected=report.new_vacancies, prefiltered=report.details,
                        evaluated=report.evaluated, bridge_calls=bridge_calls, page_loads=report.page_loads)
    log.info("Прогон #%d завершён: %s", run_id, report.as_text().replace("\n", " | "))
    conn.close()
    return report


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", default="manual")
    ap.add_argument("--budget", type=int, help="page-load budget for this run (default: today's remaining)")
    ap.add_argument("--gap-scale", type=float, default=1.0)
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    report = run_crawl(settings, settings.db_path, args.trigger, budget=args.budget, gap_scale=args.gap_scale)
    print(report.as_text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
