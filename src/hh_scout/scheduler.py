"""Daily schedule: digest at DIGEST_TIME, one crawl ("sitting") per window in CRAWL_WINDOWS.

Pure helpers (`next_sitting`, `sittings_left`) are unit-tested; `Scheduler` wires them into APScheduler
and the bot. State survives restarts through the `kv` table (next_crawl_at, crawl_window_idx,
crawl_window_date, sitting_done, paused). Each sitting gets its share of what is left of today's page-load cap.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from hh_scout import health
from hh_scout.config import TZ, Settings
from hh_scout.db import kv_get, kv_set
from hh_scout.pipeline import repo
from hh_scout.pipeline.budget import daily_cap
from hh_scout.pipeline.run import CrawlReport, run_crawl

log = logging.getLogger(__name__)

MIN_LEAD_MINUTES = 20
MIN_SITTING_MINUTES = 60   # a sitting starts no later than this before its window closes
SITTING_GRACE_MIN = health.WINDOW_GRACE_MIN  # ...and stops this long after the window closes, budget or not

Window = tuple[time, time]


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t.replace(tzinfo=None), tzinfo=TZ)


def next_sitting(now: datetime, windows: list[Window], done_idx_today: int | None = None,
                 rng: random.Random | None = None) -> tuple[datetime, int]:
    """Random start inside the next usable window; returns (when, window index).

    Usable today: index greater than `done_idx_today` (the sitting already started or skipped today; None = none)
    and at least MIN_SITTING_MINUTES of the window remain after now + MIN_LEAD_MINUTES. Otherwise the first window
    tomorrow. The start is drawn from [window start, window end − MIN_SITTING_MINUTES] so a sitting always has an hour.
    """
    rng = rng or random.Random()
    earliest = now + timedelta(minutes=MIN_LEAD_MINUTES)
    tail = timedelta(minutes=MIN_SITTING_MINUTES)
    for idx, (start, end) in enumerate(windows):
        if done_idx_today is not None and idx <= done_idx_today:
            continue
        lo, hi = max(_at(now.date(), start), earliest), _at(now.date(), end) - tail
        if lo <= hi:
            return lo + timedelta(seconds=rng.uniform(0, (hi - lo).total_seconds())), idx
    d = now.date() + timedelta(days=1)
    start, end = windows[0]
    lo, hi = _at(d, start), max(_at(d, start), _at(d, end) - tail)
    return lo + timedelta(seconds=rng.uniform(0, (hi - lo).total_seconds())), 0


def sitting_deadline(day: date, windows: list[Window], idx: int) -> datetime:
    """When a sitting planned for window `idx` on `day` must stop browsing: window end + SITTING_GRACE_MIN."""
    return _at(day, windows[idx][1]) + timedelta(minutes=SITTING_GRACE_MIN)


def sittings_left(now: datetime, windows: list[Window], current_idx: int | None = None) -> int:
    """How many sittings (the current one included) are still ahead today — for sharing the daily cap."""
    n = 0
    for idx, (_, end) in enumerate(windows):
        if idx == current_idx or _at(now.date(), end) > now:
            n += 1
    return max(1, n)


def _port_open(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _bridge_ok(url: str) -> bool:
    import httpx

    try:
        return httpx.get(f"{url}/health", timeout=5).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _fmt_windows(windows: list[Window]) -> str:
    return ", ".join(f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')}" for a, b in windows)


class Scheduler:
    def __init__(self, settings: Settings, conn, notify: Callable[[str], Awaitable[None]],
                 send_digest: Callable[[], Awaitable[int]], after_crawl: Callable[[], Awaitable[None]] | None = None) -> None:
        self.s = settings
        self.conn = conn
        self.notify = notify
        self.send_digest = send_digest
        self.after_crawl = after_crawl
        self.rng = random.Random()
        self.aps = AsyncIOScheduler(timezone=TZ)
        self.crawl_lock = asyncio.Lock()
        self.last_report: CrawlReport | None = None
        self.windows = settings.crawl_windows_parsed
        self.alerter = health.Alerter(conn, notify)
        self.probe_marionette: Callable[[], bool] = lambda: _port_open(settings.marionette_host, settings.marionette_port)
        self.probe_bridge: Callable[[], bool] = lambda: _bridge_ok(settings.bridge_url)

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        dt = self.s.digest_time_parsed
        self.aps.add_job(self.digest_job, CronTrigger(hour=dt.hour, minute=dt.minute, timezone=TZ), id="digest", replace_existing=True)
        with self.conn:
            kv_set(self.conn, "crawl_attempts", None)  # v5 leftover
        self._restore_crawl()
        self.aps.add_job(self.watchdog_job, IntervalTrigger(minutes=health.WATCHDOG_INTERVAL_MIN, timezone=TZ), id="watchdog",
                         replace_existing=True)
        self.aps.start()
        nxt = self.next_crawl_at()
        log.info("Планировщик запущен: дайджест ежедневно в %s, окна сбора %s, следующий подход %s",
                 self.s.digest_time, _fmt_windows(self.windows), nxt.strftime("%d.%m %H:%M") if nxt else "не назначен")

    def shutdown(self) -> None:
        self.aps.shutdown(wait=False)

    # -- state ---------------------------------------------------------------------

    def paused(self) -> bool:
        return kv_get(self.conn, "paused") == "1"

    def set_paused(self, value: bool) -> None:
        with self.conn:
            kv_set(self.conn, "paused", "1" if value else None)

    def next_crawl_at(self) -> datetime | None:
        raw = kv_get(self.conn, "next_crawl_at")
        return datetime.fromisoformat(raw).astimezone(TZ) if raw else None

    def next_window_idx(self) -> int | None:
        raw = kv_get(self.conn, "crawl_window_idx")
        return int(raw) if raw not in (None, "") else None

    def daily_cap(self) -> int:
        return daily_cap(self.conn, self.s, self.rng)

    def _set_next_crawl(self, when: datetime | None, idx: int | None) -> None:
        with self.conn:
            kv_set(self.conn, "next_crawl_at", when.isoformat() if when else None)
            kv_set(self.conn, "crawl_window_idx", str(idx) if idx is not None else None)
            if when is not None:
                kv_set(self.conn, "crawl_window_date", when.date().isoformat())

    def _schedule_crawl(self, when: datetime) -> None:
        self.aps.add_job(self.crawl_job, DateTrigger(run_date=when), id="crawl", replace_existing=True, kwargs={"trigger": "schedule"})
        pre = when - timedelta(minutes=health.PRECHECK_LEAD_MIN)
        if pre > datetime.now(TZ):
            self.aps.add_job(self.precheck_job, DateTrigger(run_date=pre), id="precheck", replace_existing=True, kwargs={"when": when})
        log.info("Подход запланирован на %s", when.strftime("%d.%m %H:%M"))

    def _done_idx_today(self, now: datetime) -> int | None:
        """Index of the last sitting started/skipped today (kv `sitting_done` = 'YYYY-MM-DD:idx'), or None."""
        raw = kv_get(self.conn, "sitting_done") or ""
        day, _, idx = raw.partition(":")
        return int(idx) if day == now.date().isoformat() and idx.isdigit() else None

    def _mark_sitting_done(self, now: datetime) -> None:
        """Remember that today's planned window was used — unless this run is a carried-over sitting from another day."""
        idx = self.next_window_idx()
        if idx is not None and kv_get(self.conn, "crawl_window_date") == now.date().isoformat():
            with self.conn:
                kv_set(self.conn, "sitting_done", f"{now.date().isoformat()}:{idx}")

    def _window_idx_for(self, when: datetime) -> int:
        for idx, (start, end) in enumerate(self.windows):
            if _at(when.date(), start) <= when < _at(when.date(), end):
                return idx
        return 0

    def _restore_crawl(self, now: datetime | None = None) -> None:
        now = now or datetime.now(TZ)
        when = self.next_crawl_at()
        if when is None:
            self.plan_next_crawl(now)
            return
        if self.next_window_idx() is None or kv_get(self.conn, "crawl_window_date") is None:  # plan saved by v5
            self._set_next_crawl(when, self._window_idx_for(when))
        if when > now:
            if when.date() > now.date():
                # plan for a later day while today still has an unused window (old single-sitting plan, or windows
                # were added in .env): today's sitting must not be lost
                today, idx = next_sitting(now, self.windows, self._done_idx_today(now), self.rng)
                if today.date() == now.date():
                    log.info("План %s отложен: сегодня ещё есть окно — подход в %s", when.strftime("%d.%m %H:%M"), today.strftime("%H:%M"))
                    self._set_next_crawl(today, idx)
                    self._schedule_crawl(today)
                    return
            self._schedule_crawl(when)
        else:
            # missed while the service was down: run soon (see docs/ARCHITECTURE.md)
            soon = now + timedelta(minutes=self.rng.uniform(2, 5))
            log.info("Пропущенный подход (%s) — запускаю в %s", when.strftime("%d.%m %H:%M"), soon.strftime("%H:%M"))
            self._set_next_crawl(soon, self.next_window_idx())
            with self.conn:  # keep the planned day: a sitting missed yesterday must not "use up" today's window
                kv_set(self.conn, "crawl_window_date", when.date().isoformat())
            self._schedule_crawl(soon)

    def plan_next_crawl(self, now: datetime | None = None) -> datetime:
        now = now or datetime.now(TZ)
        when, idx = next_sitting(now, self.windows, self._done_idx_today(now), self.rng)
        self._set_next_crawl(when, idx)
        self._schedule_crawl(when)
        return when

    def _budget_share(self, now: datetime) -> int:
        used = repo.page_loads_today(self.conn)
        remaining = max(0, self.daily_cap() - used)
        return math.ceil(remaining / sittings_left(now, self.windows, self.next_window_idx()))

    # -- jobs ----------------------------------------------------------------------

    async def digest_job(self) -> None:
        try:
            await self.send_digest()
        except Exception as e:  # noqa: BLE001
            log.exception("Дайджест упал")
            await self.notify(f"⚠️ Дайджест не отправлен: {e}")

    async def watchdog_job(self, now: datetime | None = None) -> list[health.Alert]:
        """Every 30 min: is the day going to plan? Alerts once per condition per day; re-plans if nothing is planned."""
        now = now or datetime.now(TZ)
        try:
            stale = repo.running_run(self.conn)
            stale_since = datetime.fromisoformat(stale["started_at"]) if stale and not self.crawl_lock.locked() else None
            alerts = health.schedule_checks(
                now, self.windows, repo.run_starts_today(self.conn), next_crawl_at=self.next_crawl_at(),
                crawl_running=self.crawl_lock.locked(), paused=self.paused(), page_loads_today=repo.page_loads_today(self.conn),
                stale_running_since=stale_since,
            )
            if any(a.key == "noplan" for a in alerts):
                self.plan_next_crawl(now)
            last_end = _at(now.date(), self.windows[-1][1]) + timedelta(minutes=health.WINDOW_GRACE_MIN)
            if (now >= last_end and not self.paused() and not self.crawl_lock.locked()
                    and not self.alerter.already_sent("day_summary", now.date())):
                t = repo.day_totals(self.conn, self.s.score_threshold)
                summary = health.day_summary_alert(
                    now, self.windows, repo.run_starts_today(self.conn), page_loads_today=t["page_loads"], daily_cap=self.daily_cap(),
                    new_vacancies=t["new_vacancies"], leads=t["leads"])
                if summary is not None:
                    alerts.append(summary)
                else:  # a full day is routine: remember it was tallied, tell nobody (the noon digest reports the work)
                    self.alerter.mark_sent("day_summary", now)
            await self.alerter.send(alerts, now)
        except Exception as e:  # noqa: BLE001
            log.exception("Сторож упал")
            await self.notify(f"⚠️ Сторож расписания упал: {e}")
            return []
        try:  # bookkeeping: a crawl thread writing a big batch may hold the lock past busy_timeout — journal only
            kv_set(self.conn, "watchdog_last", now.isoformat())
            if now.hour == 0 or kv_get(self.conn, "alerts_cleaned") != now.date().isoformat():
                self.alerter.forget_old(now.date())
                kv_set(self.conn, "alerts_cleaned", now.date().isoformat())
        except sqlite3.OperationalError as e:
            log.warning("Сторож: отметка в kv не записана (%s), повторим через 30 мин", e)
        return alerts

    async def precheck_job(self, when: datetime) -> list[health.Alert]:
        """30 min before a sitting: Firefox/Marionette and the bridge must be up."""
        if self.paused():
            return []
        m_ok, b_ok = await asyncio.gather(asyncio.to_thread(self.probe_marionette), asyncio.to_thread(self.probe_bridge))
        alerts = health.precheck(when, marionette_ok=m_ok, bridge_ok=b_ok)
        await self.alerter.send(alerts)
        return alerts

    async def crawl_job(self, trigger: str = "schedule", manual_budget: int | None = None) -> CrawlReport | None:
        now = datetime.now(TZ)
        if trigger == "schedule" and self.paused():
            self._mark_sitting_done(now)
            when = self.plan_next_crawl(now)
            log.info("Подход пропущен: бот на паузе. Следующий: %s", when.strftime("%d.%m %H:%M"))
            return None
        if self.crawl_lock.locked():
            log.warning("Сбор уже идёт — новый не стартую")
            return None
        async with self.crawl_lock:
            budget = None if trigger == "schedule" else manual_budget
            deadline: datetime | None = None
            if trigger == "schedule":
                idx = self.next_window_idx()
                deadline = sitting_deadline(now.date(), self.windows, idx if idx is not None else self._window_idx_for(now))
                self._mark_sitting_done(now)  # a restart mid-run must not plan this window again
                self._set_next_crawl(None, idx)
                if deadline <= now:  # e.g. restored long after the window closed: the night is for sleeping
                    when = self.plan_next_crawl(now)
                    log.info("Подход пропущен: окно уже закрылось (%s). Следующий: %s", f"{deadline:%H:%M}", when.strftime("%d.%m %H:%M"))
                    return None
                budget = self._budget_share(now)
            start_note = (f"Начинаю сбор ({trigger}" + (f", до {budget} страниц" if budget is not None else "")
                          + (f", не позже {deadline:%H:%M}" if deadline else "") + ")")
            if trigger == "schedule":
                log.info("%s", start_note)  # quiet mode: a scheduled sitting is routine, the owner hears only about trouble
            else:
                await self.notify("▶️ " + start_note + ". Листаю сериями по ~10 мин с паузами ~5 мин.")
            try:
                report = await asyncio.to_thread(run_crawl, self.s, self.s.db_path, trigger, budget=budget, deadline=deadline)
            except Exception as e:  # noqa: BLE001
                log.exception("Прогон упал")
                await self.notify(f"❌ Сбор упал: {e}")
                report = None
        when = self.plan_next_crawl(datetime.now(TZ)) if trigger == "schedule" else self.next_crawl_at()
        if report is None:
            return None
        self.last_report = report
        text = report.as_text()
        if when is not None:
            text += f"\nСледующий подход: {when.strftime('%d.%m %H:%M')}"
        if report.browser_error:
            text += "\nПроверьте, что Firefox запущен с --marionette."
        alerts_sent = 0
        try:
            alerts_sent = await self.alerter.send(health.analyze_report(report))
        except Exception as e:  # noqa: BLE001
            log.exception("Разбор отчёта упал: %s", e)
        # Quiet mode: /crawl always gets its report; a scheduled sitting reports only when something went wrong
        # and no alert with concrete advice has already covered it (one event — one message).
        if trigger != "schedule" or (not report.ok and alerts_sent == 0):
            await self.notify(text)
        if self.after_crawl is not None:
            try:
                await self.after_crawl()
            except Exception as e:  # noqa: BLE001
                log.exception("after_crawl упал")
                await self.notify(f"⚠️ Автозакрытие лидов не выполнено: {e}")
        return report

    async def trigger_manual_crawl(self, budget: int | None = None) -> None:
        """/crawl [N]: run now; N caps this run's page loads (default: everything left of today's cap)."""
        asyncio.create_task(self.crawl_job(trigger="manual", manual_budget=budget))
