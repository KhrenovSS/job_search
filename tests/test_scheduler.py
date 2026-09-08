import random
from datetime import datetime, time

from hh_scout.config import TZ
from hh_scout.scheduler import pick_crawl_time, retry_time

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


def test_retry_bounded_by_deadline():
    now = datetime(2026, 9, 9, 22, 0, tzinfo=TZ)
    t = retry_time(now, random.Random(3))
    assert t is not None and t <= datetime(2026, 9, 10, 11, 30, tzinfo=TZ) and t > now
    late = datetime(2026, 9, 10, 10, 30, tzinfo=TZ)
    for seed in range(30):
        t = retry_time(late, random.Random(seed))
        assert t is None or t <= datetime(2026, 9, 10, 11, 30, tzinfo=TZ)
    assert retry_time(datetime(2026, 9, 10, 11, 0, tzinfo=TZ), random.Random(0)) is None
