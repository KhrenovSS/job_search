"""Daily schedule: digest at DIGEST_TIME, one crawl ("sitting") per window in CRAWL_WINDOWS.

Pure helpers (`next_sitting`, `sittings_left`) are unit-tested; `Scheduler` wires them into APScheduler
and the bot. State survives restarts through the `kv` table (next_crawl_at, crawl_window_idx,
crawl_window_date, paused). Each sitting gets its share of what is left of today's page-load cap.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import date, datetime, time, timedelta
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from hh_scout.config import TZ, Settings
from hh_scout.db import kv_get, kv_set
from hh_scout.pipeline import repo
from hh_scout.pipeline.budget import daily_cap
from hh_scout.pipeline.run import CrawlReport, run_crawl

log = logging.getLogger(__name__)

MIN_LEAD_MINUTES = 20

Window = tuple[time, time]


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t.replace(tzinfo=None), tzinfo=TZ)


def next_sitting(now: datetime, windows: list[Window], done_idx_today: int | None = None,
                 rng: random.Random | None = None) -> tuple[datetime, int]:
    """Random start inside the next usable window; returns (when, window index).

    Usable today: index greater than `done_idx_today` (the sitting already started or skipped today; None = none)
    and the window still has room after now + 20 min. Otherwise the first window tomorrow.
    """
    rng = rng or random.Random()
    earliest = now + timedelta(minutes=MIN_LEAD_MINUTES)
    for idx, (start, end) in enumerate(windows):
        if done_idx_today is not None and idx <= done_idx_today:
            continue
        lo, hi = max(_at(now.date(), start), earliest), _at(now.date(), end)
        if lo < hi:
            return lo + timedelta(seconds=rng.uniform(0, (hi - lo).total_seconds())), idx
    d = now.date() + timedelta(days=1)
    start, end = windows[0]
    lo, hi = _at(d, start), _at(d, end)
    return lo + timedelta(seconds=rng.uniform(0, (hi - lo).total_seconds())), 0


def sittings_left(now: datetime, windows: list[Window], current_idx: int | None = None) -> int:
    """How many sittings (the current one included) are still ahead today — for sharing the daily cap."""
    n = 0
    for idx, (_, end) in enumerate(windows):
        if idx == current_idx or _at(now.date(), end) > now:
            n += 1
    return max(1, n)


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

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        dt = self.s.digest_time_parsed
        self.aps.add_job(self.digest_job, CronTrigger(hour=dt.hour, minute=dt.minute, timezone=TZ), id="digest", replace_existing=True)
        with self.conn:
            kv_set(self.conn, "crawl_attempts", None)  # v5 leftover
        self._restore_crawl()
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
        log.info("Подход запланирован на %s", when.strftime("%d.%m %H:%M"))

    def _done_idx_today(self, now: datetime) -> int | None:
        """Index of the sitting already started/skipped today (kv), or None."""
        if kv_get(self.conn, "crawl_window_date") != now.date().isoformat():
            return None
        return self.next_window_idx()

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

    async def crawl_job(self, trigger: str = "schedule") -> CrawlReport | None:
        now = datetime.now(TZ)
        if trigger == "schedule" and self.paused():
            when = self.plan_next_crawl(now)
            await self.notify(f"⏸ Подход пропущен: бот на паузе (/resume — возобновить). Следующий: {when.strftime('%d.%m %H:%M')}")
            return None
        if self.crawl_lock.locked():
            await self.notify("Сбор уже идёт")
            return None
        async with self.crawl_lock:
            budget: int | None = None
            if trigger == "schedule":
                budget = self._budget_share(now)
                # keep the window index and today's date in kv, clear the time: a restart mid-run must not re-plan this window
                self._set_next_crawl(None, self.next_window_idx())
            await self.notify(f"▶️ Начинаю сбор ({trigger}"
                              + (f", до {budget} страниц" if budget is not None else "")
                              + "). Листаю сериями по ~10 мин с паузами ~5 мин.")
            try:
                report = await asyncio.to_thread(run_crawl, self.s, self.s.db_path, trigger, budget=budget)
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
        await self.notify(text)
        if self.after_crawl is not None:
            try:
                await self.after_crawl()
            except Exception as e:  # noqa: BLE001
                log.exception("after_crawl упал")
                await self.notify(f"⚠️ Автозакрытие лидов не выполнено: {e}")
        return report

    async def trigger_manual_crawl(self) -> None:
        asyncio.create_task(self.crawl_job(trigger="manual"))
