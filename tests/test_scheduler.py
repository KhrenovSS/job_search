import random
from datetime import datetime, time, timedelta

import pytest

from hh_scout.config import TZ, Settings, parse_windows
from hh_scout.db import connect, kv_get, kv_set, migrate
from hh_scout.pipeline import repo
from hh_scout.scheduler import Scheduler, next_sitting, sittings_left

WINDOWS = parse_windows("07:00-10:00,12:00-15:00,18:00-22:00")


def _dt(h, m=0, day=9):
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def _in(t, start, end):
    return time(*start) <= t.time() <= time(*end)


def test_parse_windows_sorts_and_validates():
    assert parse_windows("12:00-15:00, 07:00-10:00") == [(time(7, 0, tzinfo=TZ), time(10, 0, tzinfo=TZ)),
                                                        (time(12, 0, tzinfo=TZ), time(15, 0, tzinfo=TZ))]
    with pytest.raises(ValueError):
        parse_windows("07:00-10:00,09:00-12:00")
    with pytest.raises(ValueError):
        parse_windows("10:00-07:00")
    s = Settings(_env_file=None, burst_minutes="7-13", gap_minutes="4-9")
    assert s.burst_seconds == (420.0, 780.0) and s.gap_seconds == (240.0, 540.0)


def test_next_sitting_before_first_window_lands_in_it_today():
    for seed in range(30):
        when, idx = next_sitting(_dt(6, 30), WINDOWS, None, random.Random(seed))
        assert idx == 0 and when.day == 9 and _in(when, (7, 0), (10, 0))


def test_next_sitting_inside_window_respects_lead_time():
    for seed in range(30):
        when, idx = next_sitting(_dt(7, 40), WINDOWS, None, random.Random(seed))
        assert idx == 0 and when.day == 9 and _in(when, (8, 0), (10, 0))
    # too close to the end of the window -> the next one
    when, idx = next_sitting(_dt(9, 45), WINDOWS, None, random.Random(1))
    assert idx == 1 and _in(when, (12, 0), (15, 0))


def test_next_sitting_skips_windows_already_used_today():
    when, idx = next_sitting(_dt(8, 30), WINDOWS, 0, random.Random(2))
    assert idx == 1 and when.day == 9 and _in(when, (12, 0), (15, 0))
    when, idx = next_sitting(_dt(13, 0), WINDOWS, 2, random.Random(2))  # evening already used (odd, but be safe)
    assert idx == 0 and when.day == 10


def test_next_sitting_after_last_window_goes_to_tomorrow():
    for seed in range(20):
        when, idx = next_sitting(_dt(22, 30), WINDOWS, 2, random.Random(seed))
        assert idx == 0 and when.day == 10 and _in(when, (7, 0), (10, 0))


def test_sittings_left_counts_current_and_future_windows():
    assert sittings_left(_dt(7, 30), WINDOWS, 0) == 3
    assert sittings_left(_dt(13, 0), WINDOWS, 1) == 2
    assert sittings_left(_dt(20, 0), WINDOWS, 2) == 1
    assert sittings_left(_dt(23, 0), WINDOWS, None) == 1  # never divide by zero
    assert sittings_left(_dt(6, 0), WINDOWS, None) == 3


async def _noop(*_a):
    return None


def _scheduler():
    settings = Settings(_env_file=None, daily_page_loads_min=120, daily_page_loads_max=120)
    conn = connect(":memory:")
    migrate(conn)
    return Scheduler(settings, conn, _noop, _noop), conn


def test_restore_with_empty_kv_plans_today():
    sch, conn = _scheduler()
    sch._restore_crawl(now=_dt(6, 58))
    when = sch.next_crawl_at()
    assert when is not None and when.day == 9 and _in(when, (7, 0), (10, 0))
    assert kv_get(conn, "crawl_window_idx") == "0" and kv_get(conn, "crawl_window_date") == "2026-09-09"
    assert sch.aps.get_job("crawl") is not None


def test_restore_keeps_v5_plan_and_fills_window_index():
    sch, conn = _scheduler()
    kv_set(conn, "next_crawl_at", _dt(7, 9, day=10).isoformat())  # saved by v5: no idx / date
    kv_set(conn, "crawl_attempts", "0")
    sch._restore_crawl(now=_dt(22, 30))  # today's windows are over
    assert sch.next_crawl_at() == _dt(7, 9, day=10)
    assert kv_get(conn, "crawl_window_idx") == "0" and kv_get(conn, "crawl_window_date") == "2026-09-10"


def test_restore_after_a_missed_sitting_runs_soon_and_keeps_planned_day():
    sch, conn = _scheduler()
    kv_set(conn, "next_crawl_at", _dt(18, 30, day=8).isoformat())
    kv_set(conn, "crawl_window_idx", "2")
    kv_set(conn, "crawl_window_date", "2026-09-08")
    sch._restore_crawl(now=_dt(6, 0))
    when = sch.next_crawl_at()
    assert _dt(6, 2) <= when <= _dt(6, 5)
    assert kv_get(conn, "crawl_window_date") == "2026-09-08"  # yesterday's sitting: today's windows stay available
    # after that run the planner sees no sitting done today and picks window 0 today
    nxt = sch.plan_next_crawl(now=_dt(6, 40))
    assert nxt.day == 9 and _in(nxt, (7, 0), (10, 0)) and kv_get(conn, "crawl_window_idx") == "0"


def test_plan_next_after_todays_sitting_moves_to_next_window():
    sch, conn = _scheduler()
    kv_set(conn, "sitting_done", "2026-09-09:0")
    nxt = sch.plan_next_crawl(now=_dt(8, 40))
    assert _in(nxt, (12, 0), (15, 0)) and kv_get(conn, "crawl_window_idx") == "1"


def test_budget_share_splits_remaining_cap_between_sittings_left():
    sch, conn = _scheduler()
    kv_set(conn, "crawl_window_idx", "0")
    kv_set(conn, "crawl_window_date", "2026-09-09")
    assert sch._budget_share(_dt(7, 30)) == 40  # 120 / 3
    run_id = repo.start_run(conn, "schedule")
    repo.finish_run(conn, run_id, "ok", page_loads=30)
    kv_set(conn, "crawl_window_idx", "1")
    assert sch._budget_share(_dt(13, 0)) == 45  # (120 - 30) / 2


def test_restore_replans_today_when_saved_plan_is_for_a_later_day_and_a_window_is_free():
    sch, conn = _scheduler()
    kv_set(conn, "next_crawl_at", _dt(7, 9, day=10).isoformat())  # v5 plan for tomorrow morning, evening window unused
    sch._restore_crawl(now=_dt(19, 46))
    when = sch.next_crawl_at()
    assert when.day == 9 and _in(when, (20, 6), (22, 0))
    assert kv_get(conn, "crawl_window_idx") == "2" and kv_get(conn, "crawl_window_date") == "2026-09-09"


def test_restore_keeps_tomorrow_plan_after_todays_last_sitting():
    sch, conn = _scheduler()
    kv_set(conn, "sitting_done", "2026-09-09:2")  # evening sitting ran, planner saved tomorrow
    kv_set(conn, "next_crawl_at", _dt(7, 9, day=10).isoformat())
    kv_set(conn, "crawl_window_idx", "0")
    kv_set(conn, "crawl_window_date", "2026-09-10")
    sch._restore_crawl(now=_dt(21, 0))
    assert sch.next_crawl_at() == _dt(7, 9, day=10)
    # and when less than 20 min of the last window remain, nothing fits today either
    sch2, conn2 = _scheduler()
    kv_set(conn2, "next_crawl_at", _dt(7, 9, day=10).isoformat())
    sch2._restore_crawl(now=_dt(21, 50))
    assert sch2.next_crawl_at() == _dt(7, 9, day=10)


def test_mark_sitting_done_ignores_carried_over_sitting():
    sch, conn = _scheduler()
    kv_set(conn, "crawl_window_idx", "2")
    kv_set(conn, "crawl_window_date", "2026-09-08")  # yesterday's missed evening, run this morning
    sch._mark_sitting_done(_dt(6, 3))
    assert kv_get(conn, "sitting_done") is None
    kv_set(conn, "crawl_window_date", "2026-09-09")
    sch._mark_sitting_done(_dt(7, 30))
    assert kv_get(conn, "sitting_done") == "2026-09-09:2"


def test_next_sitting_leaves_an_hour_before_the_window_closes():
    from hh_scout.scheduler import MIN_SITTING_MINUTES
    for seed in range(30):
        when, idx = next_sitting(_dt(6, 30), WINDOWS, None, random.Random(seed))
        assert idx == 0 and when <= _dt(10, 0) - timedelta(minutes=MIN_SITTING_MINUTES)
    # 9:10 + 20 min lead leaves less than an hour of the morning window -> the midday one
    when, idx = next_sitting(_dt(9, 10), WINDOWS, None, random.Random(3))
    assert idx == 1 and _in(when, (12, 0), (14, 0))
    # tomorrow's start also keeps the hour
    when, idx = next_sitting(_dt(23, 0), WINDOWS, None, random.Random(4))
    assert idx == 0 and when.day == 10 and _in(when, (7, 0), (9, 0))


def test_sitting_deadline_is_window_end_plus_grace():
    from hh_scout.scheduler import SITTING_GRACE_MIN, sitting_deadline
    assert sitting_deadline(_dt(20, 0).date(), WINDOWS, 2) == _dt(22, 0) + timedelta(minutes=SITTING_GRACE_MIN)
    assert sitting_deadline(_dt(8, 0).date(), WINDOWS, 0) == _dt(10, 30)


class _FrozenDatetime(datetime):
    _now = None

    @classmethod
    def at(cls, when):
        cls._now = when
        return cls

    @classmethod
    def now(cls, tz=None):
        return cls._now.astimezone(tz) if tz else cls._now


@pytest.mark.asyncio
async def test_crawl_job_passes_the_window_deadline_and_skips_a_closed_window(monkeypatch):
    import hh_scout.scheduler as sched_mod

    sch, conn = _scheduler()
    notes, calls = [], []

    async def notify(text):
        notes.append(text)

    sch.notify = notify
    monkeypatch.setattr(sched_mod, "run_crawl", lambda *a, **kw: calls.append(kw) or None)
    monkeypatch.setattr(sched_mod, "datetime", _FrozenDatetime.at(_dt(21, 51)))
    kv_set(conn, "crawl_window_idx", "2")
    kv_set(conn, "crawl_window_date", "2026-09-09")
    await sch.crawl_job("schedule")
    assert calls and calls[0]["deadline"] == _dt(22, 30)
    assert notes == []  # quiet mode: a scheduled sitting neither announces its start nor its (absent) report

    # the same window restored at 23:40 (service was down): skip, plan tomorrow, do not browse at night
    calls.clear()
    monkeypatch.setattr(sched_mod, "datetime", _FrozenDatetime.at(_dt(23, 40)))
    kv_set(conn, "crawl_window_idx", "2")
    kv_set(conn, "crawl_window_date", "2026-09-09")
    kv_set(conn, "sitting_done", None)
    await sch.crawl_job("schedule")
    assert not calls and notes == []  # skipped quietly (journal only)
    assert sch.next_crawl_at().day == 10


@pytest.mark.asyncio
async def test_crawl_job_is_quiet_unless_something_went_wrong(monkeypatch):
    """Scheduled sittings: silence when ok; the report when not ok; an alert instead of the report when health knows why.
    Manual /crawl: always the start note and the report."""
    import hh_scout.scheduler as sched_mod
    from hh_scout.pipeline.run import CrawlReport

    sch, conn = _scheduler()
    notes = []

    async def notify(text):
        notes.append(text)

    sch.notify = notify
    sch.alerter.notify = notify
    reports = []
    monkeypatch.setattr(sched_mod, "run_crawl", lambda *a, **kw: reports.pop(0))
    monkeypatch.setattr(sched_mod, "datetime", _FrozenDatetime.at(_dt(13, 30)))

    def arm(**kw):
        kv_set(conn, "crawl_window_idx", "1")
        kv_set(conn, "crawl_window_date", "2026-09-09")
        kv_set(conn, "sitting_done", None)
        reports.append(CrawlReport(trigger="schedule", **kw))

    arm(page_loads=40, leads=1)
    await sch.crawl_job("schedule")
    assert notes == []

    arm(errors=["уже идёт другой прогон"])
    await sch.crawl_job("schedule")
    assert len(notes) == 1 and notes[0].startswith("⚠️ Сбор завершён с замечаниями") and "Следующий подход" in notes[0]

    notes.clear()
    arm(browser_error="Marionette не отвечает")
    await sch.crawl_job("schedule")
    assert len(notes) == 1 and notes[0].startswith("🦊 Браузер недоступен")  # the alert, not a second copy as a report

    notes.clear()
    reports.append(CrawlReport(trigger="manual", page_loads=5))
    await sch.crawl_job("manual", manual_budget=5)
    assert len(notes) == 2 and notes[0].startswith("▶️ Начинаю сбор (manual, до 5 страниц)") and notes[1].startswith("✅ Сбор завершён")


@pytest.mark.asyncio
async def test_watchdog_bookkeeping_lock_is_journal_only(monkeypatch):
    """`watchdog_last` colliding with a crawl thread's batch write (database is locked) is not an incident."""
    import sqlite3

    import hh_scout.scheduler as sched_mod

    sch, conn = _scheduler()
    notes = []

    async def notify(text):
        notes.append(text)

    sch.notify = notify
    sch.alerter.notify = notify
    sch._restore_crawl(now=_dt(6, 58))  # a sitting is planned in the first window: nothing to alert about

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sched_mod, "kv_set", locked)
    alerts = await sch.watchdog_job(now=_dt(7, 30))
    assert alerts == [] and notes == []  # checks ran, nothing to report, the failed kv write stayed in the journal
    assert kv_get(conn, "watchdog_last") is None
