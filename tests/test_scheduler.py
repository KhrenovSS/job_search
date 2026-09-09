import random
from datetime import datetime, time, timedelta

from hh_scout.config import TZ
from hh_scout.scheduler import pick_crawl_time, plan_on_start, retry_time

WINDOW = (time(13, 0), time(23, 0))


def test_pick_inside_window_after_digest():
    now = datetime(2026, 9, 9, 12, 0, tzinfo=TZ)
    for seed in range(50):
        t = pick_crawl_time(now, WINDOW, random.Random(seed))
        assert t.date() == now.date() and time(13, 0) <= t.time() <= time(23, 0)


def test_pick_respects_lead_time_when_window_started():
    now = datetime(2026, 9, 9, 20, 0, tzinfo=TZ)
    for seed in range(50):
        t = pick_crawl_time(now, WINDOW, random.Random(seed))
        assert time(20, 20) <= t.time() <= time(23, 0)


def test_pick_moves_to_tomorrow_when_window_over():
    now = datetime(2026, 9, 9, 22, 50, tzinfo=TZ)
    t = pick_crawl_time(now, WINDOW, random.Random(1))
    assert t.date().day == 10 and time(13, 0) <= t.time() <= time(23, 0)


def test_retry_bounded_by_same_day_deadline():
    morning_fail = datetime(2026, 9, 9, 8, 0, tzinfo=TZ)
    for seed in range(30):
        t = retry_time(morning_fail, random.Random(seed))
        assert t is not None and morning_fail + timedelta(minutes=60) <= t <= morning_fail + timedelta(minutes=180)
    late = datetime(2026, 9, 9, 19, 30, tzinfo=TZ)
    for seed in range(30):
        t = retry_time(late, random.Random(seed))
        assert t is None or t <= datetime(2026, 9, 9, 21, 0, tzinfo=TZ)
    assert retry_time(datetime(2026, 9, 9, 20, 30, tzinfo=TZ), random.Random(0)) is None


def test_morning_window_after_noon_digest_goes_to_tomorrow():
    morning = (time(7, 0), time(8, 30))
    now = datetime(2026, 9, 9, 12, 0, 30, tzinfo=TZ)  # right after the digest
    for seed in range(30):
        t = pick_crawl_time(now, morning, random.Random(seed))
        assert t.date().day == 10 and time(7, 0) <= t.time() <= time(8, 30)
    # service restarted at 06:30 with no plan: a manual pick today lands inside today's window
    t = pick_crawl_time(datetime(2026, 9, 9, 6, 30, tzinfo=TZ), morning, random.Random(1))
    assert t.date().day == 9 and time(7, 0) <= t.time() <= time(8, 30)


MORNING = (time(7, 0), time(8, 30))


def test_plan_on_start_uses_today_window_when_ahead():
    # service (re)started at 06:30 with no saved plan and no run today → crawl inside today's window
    now = datetime(2026, 9, 9, 6, 30, tzinfo=TZ)
    for seed in range(30):
        t = plan_on_start(now, MORNING, runs_today=0, rng=random.Random(seed))
        assert t is not None and t.date().day == 9 and time(7, 0) <= t.time() <= time(8, 30)
    # inside the window: still today, respecting the 20-min lead
    t = plan_on_start(datetime(2026, 9, 9, 7, 40, tzinfo=TZ), MORNING, runs_today=0, rng=random.Random(1))
    assert t is not None and t.date().day == 9 and time(8, 0) <= t.time() <= time(8, 30)


def test_plan_on_start_skips_when_window_over_or_already_ran():
    # window over: the digest will plan tomorrow's crawl, nothing to do at start
    assert plan_on_start(datetime(2026, 9, 9, 12, 30, tzinfo=TZ), MORNING, runs_today=0, rng=random.Random(0)) is None
    # something already ran today (manual /crawl or a scheduled run cleared the kv): one crawl per day
    assert plan_on_start(datetime(2026, 9, 9, 6, 30, tzinfo=TZ), MORNING, runs_today=1, rng=random.Random(0)) is None


def test_scheduler_restore_plans_today_when_kv_empty():
    from hh_scout.config import Settings
    from hh_scout.db import connect, kv_get, migrate
    from hh_scout.pipeline import repo
    from hh_scout.scheduler import Scheduler

    async def _noop(*_a):  # pragma: no cover - never awaited here
        return None

    settings = Settings(_env_file=None, crawl_window="07:00-08:30")
    conn = connect(":memory:")
    migrate(conn)
    sch = Scheduler(settings, conn, _noop, _noop)
    sch._restore_crawl(now=datetime(2026, 9, 9, 6, 58, tzinfo=TZ))
    saved = kv_get(conn, "next_crawl_at")
    assert saved is not None and datetime.fromisoformat(saved).astimezone(TZ).date().day == 9
    assert kv_get(conn, "crawl_attempts") == "0"
    assert sch.aps.get_job("crawl") is not None

    # a run already started today → stays unplanned
    conn2 = connect(":memory:")
    migrate(conn2)
    repo.start_run(conn2, "manual")
    sch2 = Scheduler(settings, conn2, _noop, _noop)
    sch2._restore_crawl(now=datetime(2026, 9, 9, 6, 58, tzinfo=TZ))
    assert kv_get(conn2, "next_crawl_at") is None
