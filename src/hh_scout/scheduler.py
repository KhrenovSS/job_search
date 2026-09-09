"""Daily schedule: digest at DIGEST_TIME, crawl at a random moment inside CRAWL_WINDOW.

Pure helpers (`pick_crawl_time`, `plan_on_start`, `retry_time`) are unit-tested; `Scheduler` wires them into APScheduler
and the bot. State survives restarts through the `kv` table (next_crawl_at, crawl_attempts, paused).
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime, time, timedelta
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from hh_scout.config import TZ, Settings
from hh_scout.db import kv_get, kv_set
from hh_scout.pipeline import repo
from hh_scout.pipeline.run import CrawlReport, run_crawl

log = logging.getLogger(__name__)

MAX_CRAWL_ATTEMPTS = 3
RETRY_DEADLINE = time(21, 0)  # retries after a browser failure stay within the same day, not later than this
MIN_LEAD_MINUTES = 20


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t.replace(tzinfo=None), tzinfo=TZ)


def pick_crawl_time(now: datetime, window: tuple[time, time], rng: random.Random | None = None) -> datetime:
    """Random moment inside today's window (or tomorrow's if today's is over), at least 20 min from now."""
    rng = rng or random.Random()
    start, end = window
    today_start, today_end = _at(now.date(), start), _at(now.date(), end)
    earliest = now + timedelta(minutes=MIN_LEAD_MINUTES)
    lo = max(today_start, earliest)
    if lo >= today_end:
        d = now.date() + timedelta(days=1)
        lo, today_end = _at(d, start), _at(d, end)
    span = (today_end - lo).total_seconds()
    return lo + timedelta(seconds=rng.uniform(0, span))


def plan_on_start(now: datetime, window: tuple[time, time], runs_today: int, rng: random.Random | None = None) -> datetime | None:
    """Crawl time for a service start with no saved plan: today's window is still ahead and nothing ran today.

    Fills the gap between installation (or a restart that found an empty `kv.next_crawl_at`) and the next digest,
    which is otherwise the only place a crawl gets planned. Returns None once today's window is over or a run has
    already started today (one crawl per day; the digest will plan tomorrow's).
    """
    if runs_today > 0:
        return None
    when = pick_crawl_time(now, window, rng)
    return when if when.date() == now.date() else None


def retry_time(now: datetime, rng: random.Random | None = None) -> datetime | None:
    """60–180 min from now, but not after RETRY_DEADLINE today; None if that is impossible.

    With the morning crawl window a failed crawl is retried during the same day; a late result simply
    lands in the next day's digest.
    """
    rng = rng or random.Random()
    candidate = now + timedelta(minutes=rng.uniform(60, 180))
    deadline = _at(now.date(), RETRY_DEADLINE)
    return candidate if candidate <= deadline else None


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

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        dt = self.s.digest_time_parsed
        self.aps.add_job(self.digest_job, CronTrigger(hour=dt.hour, minute=dt.minute, timezone=TZ), id="digest", replace_existing=True)
        self._restore_crawl()
        self.aps.start()
        log.info("Планировщик запущен: дайджест ежедневно в %s, следующий сбор %s", self.s.digest_time, self.next_crawl_at() or "не назначен")

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

    def _set_next_crawl(self, when: datetime | None, attempts: int | None = None) -> None:
        with self.conn:
            kv_set(self.conn, "next_crawl_at", when.isoformat() if when else None)
            if attempts is not None:
                kv_set(self.conn, "crawl_attempts", str(attempts))

    def _schedule_crawl(self, when: datetime) -> None:
        self.aps.add_job(self.crawl_job, DateTrigger(run_date=when), id="crawl", replace_existing=True, kwargs={"trigger": "schedule"})
        log.info("Сбор запланирован на %s", when.strftime("%d.%m %H:%M"))

    def _restore_crawl(self, now: datetime | None = None) -> None:
        now = now or datetime.now(TZ)
        when = self.next_crawl_at()
        if when is None:
            # no saved plan (fresh install, retries exhausted, restart with an empty kv): use today's window if it
            # is still ahead and nothing ran today, instead of waiting for the next digest to plan tomorrow
            when = plan_on_start(now, self.s.crawl_window_parsed, repo.runs_today(self.conn), self.rng)
            if when is not None:
                log.info("Сбор на сегодня не был назначен — назначаю на %s", when.strftime("%H:%M"))
                self._set_next_crawl(when, attempts=0)
                self._schedule_crawl(when)
            return
        if when > now:
            self._schedule_crawl(when)
        else:
            # missed while the service was down: run soon (no "already ran today" check — see docs/ARCHITECTURE.md)
            soon = now + timedelta(minutes=self.rng.uniform(2, 5))
            log.info("Пропущенный сбор (%s) — запускаю в %s", when.strftime("%d.%m %H:%M"), soon.strftime("%H:%M"))
            self._set_next_crawl(soon)
            self._schedule_crawl(soon)

    def plan_next_crawl(self) -> datetime:
        when = pick_crawl_time(datetime.now(TZ), self.s.crawl_window_parsed, self.rng)
        self._set_next_crawl(when, attempts=0)
        self._schedule_crawl(when)
        return when

    # -- jobs ----------------------------------------------------------------------

    async def digest_job(self) -> None:
        try:
            await self.send_digest()
        except Exception as e:  # noqa: BLE001
            log.exception("Дайджест упал")
            await self.notify(f"⚠️ Дайджест не отправлен: {e}")
        try:
            when = self.plan_next_crawl()
            log.info("Следующий сбор: %s", when)
        except Exception as e:  # noqa: BLE001
            log.exception("Не удалось запланировать сбор")
            await self.notify(f"⚠️ Не удалось запланировать сбор: {e}")

    async def crawl_job(self, trigger: str = "schedule") -> CrawlReport | None:
        if trigger == "schedule" and self.paused():
            await self.notify("⏸ Сбор пропущен: бот на паузе (/resume — возобновить)")
            self._set_next_crawl(None)
            return None
        if self.crawl_lock.locked():
            await self.notify("Сбор уже идёт")
            return None
        async with self.crawl_lock:
            if trigger == "schedule":
                self._set_next_crawl(None)
            await self.notify(f"▶️ Начинаю сбор ({trigger}). Это займёт от 30 минут до нескольких часов — работаю сериями с паузами.")
            try:
                report = await asyncio.to_thread(run_crawl, self.s, self.s.db_path, trigger)
            except Exception as e:  # noqa: BLE001
                log.exception("Прогон упал")
                await self.notify(f"❌ Сбор упал: {e}")
                return None
        self.last_report = report
        await self.notify(report.as_text())
        if self.after_crawl is not None:
            try:
                await self.after_crawl()
            except Exception as e:  # noqa: BLE001
                log.exception("after_crawl упал")
                await self.notify(f"⚠️ Автозакрытие лидов не выполнено: {e}")
        if report.browser_error and trigger == "schedule":
            await self._maybe_retry(report)
        return report

    async def _maybe_retry(self, report: CrawlReport) -> None:
        attempts = int(kv_get(self.conn, "crawl_attempts", "0") or 0) + 1
        when = retry_time(datetime.now(TZ), self.rng) if attempts <= MAX_CRAWL_ATTEMPTS else None
        if when is None:
            self._set_next_crawl(None, attempts=attempts)
            await self.notify("Повторов сбора сегодня больше не будет. Проверьте Firefox (--marionette) — завтра попробую снова.")
            return
        self._set_next_crawl(when, attempts=attempts)
        self._schedule_crawl(when)
        await self.notify(f"🔁 Повтор сбора в {when.strftime('%H:%M')} (попытка {attempts}/{MAX_CRAWL_ATTEMPTS}). "
                          "Убедитесь, что Firefox запущен с --marionette.")

    async def trigger_manual_crawl(self) -> None:
        asyncio.create_task(self.crawl_job(trigger="manual"))
