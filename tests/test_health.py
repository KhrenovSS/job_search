from datetime import datetime, timedelta

import pytest

from hh_scout import health
from hh_scout.config import TZ, Settings, parse_windows
from hh_scout.db import connect, kv_get, migrate
from hh_scout.scheduler import Scheduler

WINDOWS = parse_windows("07:00-10:00,12:00-15:00,18:00-22:00")


def _dt(h, m=0, day=9):
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def _checks(now, starts=(), **kw):
    args = dict(next_crawl_at=_dt(23, 59), crawl_running=False, paused=False, page_loads_today=10, stale_running_since=None)
    args.update(kw)
    return health.schedule_checks(now, WINDOWS, list(starts), **args)


def test_missed_window_is_reported_once_per_window():
    keys = [a.key for a in _checks(_dt(11, 0))]
    assert keys == ["missed:0"]
    # a sitting started inside (or shortly after) the window counts
    assert [a.key for a in _checks(_dt(11, 0), starts=[_dt(7, 9)])] == []
    assert [a.key for a in _checks(_dt(11, 0), starts=[_dt(10, 20)])] == []
    # window still open (grace period): nothing yet
    assert [a.key for a in _checks(_dt(10, 15))] == []
    # afternoon: both morning windows missed
    assert [a.key for a in _checks(_dt(16, 0))] == ["missed:0", "missed:1"]


def test_no_plan_zero_day_and_stale_run():
    assert [a.key for a in _checks(_dt(8, 0), starts=[_dt(7, 9)], next_crawl_at=None)] == ["noplan"]
    assert [a.key for a in _checks(_dt(8, 0), next_crawl_at=None, crawl_running=True)] == []  # running: fine
    keys = [a.key for a in _checks(_dt(22, 45), next_crawl_at=None, page_loads_today=0)]
    assert keys == ["missed:0", "missed:1", "missed:2", "noplan", "zero_day"]
    assert [a.key for a in _checks(_dt(9, 0), starts=[_dt(7, 9)], stale_running_since=_dt(5, 0))] == ["stale_run"]
    assert [a.key for a in _checks(_dt(9, 0), starts=[_dt(7, 9)], stale_running_since=_dt(8, 0))] == []


def test_paused_silences_schedule_checks():
    assert _checks(_dt(22, 45), next_crawl_at=None, page_loads_today=0, paused=True) == []


def test_day_summary_marks_shortfall():
    ok = health.day_summary(_dt(22, 40), WINDOWS, [_dt(7, 9), _dt(13, 0), _dt(19, 30)], page_loads_today=98, daily_cap=111,
                            new_vacancies=40, leads=2)
    assert ok.startswith("📊") and "3 из 3" in ok and "98/111" in ok
    bad = health.day_summary(_dt(22, 40), WINDOWS, [_dt(7, 9)], page_loads_today=30, daily_cap=111, new_vacancies=5, leads=0)
    assert bad.startswith("⚠️") and "1 из 3" in bad


class _Report:
    def __init__(self, **kw):
        self.browser_error = None
        self.bridge_error = None
        self.details = 0
        self.__dict__.update(kw)


def test_analyze_report_detects_block_and_markup_changes():
    keys = lambda r: [a.key for a in health.analyze_report(r)]  # noqa: E731
    assert keys(_Report(browser_error="hh.ru вернул страницу без данных (заголовок: 'Проверка')")) == ["hh_blocked"]
    assert keys(_Report(browser_error="Marionette не отвечает на 127.0.0.1:2828")) == ["browser_down"]
    assert keys(_Report(not_logged_in=True)) == ["not_applicant"]
    assert keys(_Report(search_pages=9, cards_seen=0)) == ["no_cards"]
    assert keys(_Report(search_pages=9, cards_seen=300)) == []
    assert keys(_Report(format_errors=5, details=3)) == ["no_vacancy_view"]
    assert keys(_Report(format_errors=2, details=30)) == []
    assert keys(_Report(bridge_error="502 claude CLI exit")) == ["bridge_down"]


def test_precheck():
    assert [a.key for a in health.precheck(_dt(21, 51), marionette_ok=False, bridge_ok=True)] == ["pre_browser"]
    assert [a.key for a in health.precheck(_dt(21, 51), marionette_ok=True, bridge_ok=False)] == ["pre_bridge"]
    assert health.precheck(_dt(21, 51), marionette_ok=True, bridge_ok=True) == []


@pytest.mark.asyncio
async def test_alerter_sends_each_key_once_per_day():
    conn = connect(":memory:")
    migrate(conn)
    sent = []

    async def notify(text):
        sent.append(text)

    al = health.Alerter(conn, notify)
    alerts = [health.Alert("missed:0", "a"), health.Alert("noplan", "b")]
    assert await al.send(alerts, _dt(11, 0)) == 2
    assert await al.send(alerts, _dt(11, 30)) == 0  # same day: silent
    assert al.count_today(_dt(11, 30).date()) == 2
    assert await al.send([health.Alert("missed:0", "a")], _dt(11, 0, day=10)) == 1  # next day: again
    al.forget_old(_dt(0, 5, day=10).date())
    assert kv_get(conn, "alert:missed:0:2026-09-09") is None and kv_get(conn, "alert:missed:0:2026-09-10") is not None
    assert sent == ["a", "b", "a"]


@pytest.mark.asyncio
async def test_watchdog_replans_when_nothing_is_planned_and_reports_missed_window():
    settings = Settings(_env_file=None, daily_page_loads_min=120, daily_page_loads_max=120)
    conn = connect(":memory:")
    migrate(conn)
    sent = []

    async def notify(text):
        sent.append(text)

    sch = Scheduler(settings, conn, notify, notify)
    # kv empty (like the v5.1 starts on 09.09), morning window over, nothing ran
    alerts = await sch.watchdog_job(now=_dt(11, 0))
    assert sorted(a.key for a in alerts) == ["missed:0", "noplan"]
    assert sch.next_crawl_at() is not None and sch.next_crawl_at().hour >= 12  # re-planned into the next window
    assert len(sent) == 2 and kv_get(conn, "watchdog_last") is not None
    # second tick within the day: nothing new
    assert await sch.watchdog_job(now=_dt(11, 30)) == [] or len(sent) == 2


@pytest.mark.asyncio
async def test_watchdog_end_of_day_summary_once():
    settings = Settings(_env_file=None, daily_page_loads_min=120, daily_page_loads_max=120)
    conn = connect(":memory:")
    migrate(conn)
    sent = []

    async def notify(text):
        sent.append(text)

    sch = Scheduler(settings, conn, notify, notify)
    # repo helpers use the real "today", so build the day around the real date with explicit start times
    today = datetime.now(TZ).date()
    at = lambda h, m=0: datetime.combine(today, datetime.min.time(), tzinfo=TZ).replace(hour=h, minute=m)  # noqa: E731
    from datetime import timezone
    for h in (7, 13, 19):
        conn.execute("INSERT INTO runs(started_at, status, trigger, page_loads, collected) VALUES (?, 'ok', 'schedule', 30, 10)",
                     (at(h).astimezone(timezone.utc).isoformat(),))
    sch.plan_next_crawl(now=at(22))
    # quiet mode: a full day (3 of 3) is tallied but not reported — the noon digest carries the work line
    await sch.watchdog_job(now=at(22, 40))
    assert not [t for t in sent if "Итог дня" in t]
    assert sch.alerter.already_sent("day_summary", today)
    await sch.watchdog_job(now=at(23, 10))
    assert sent == []

    # a short day (1 of 3) is a problem: one ⚠️ line, not repeated on the next tick
    conn.execute("DELETE FROM runs WHERE started_at > ?", (at(8).astimezone(timezone.utc).isoformat(),))
    conn.execute("DELETE FROM kv WHERE key LIKE 'alert:%'")
    await sch.watchdog_job(now=at(22, 40))
    summary = [t for t in sent if "Итог дня" in t]
    assert len(summary) == 1 and summary[0].startswith("⚠️") and "1 из 3" in summary[0] and "30/120" in summary[0]
    await sch.watchdog_job(now=at(23, 10))
    assert len([t for t in sent if "Итог дня" in t]) == 1


def test_day_summary_alert_only_on_shortfall():
    full = health.day_summary_alert(_dt(22, 40), WINDOWS, [_dt(7, 9), _dt(13, 0), _dt(19, 30)], page_loads_today=98, daily_cap=111,
                                    new_vacancies=12, leads=2)
    short = health.day_summary_alert(_dt(22, 40), WINDOWS, [_dt(7, 9)], page_loads_today=30, daily_cap=111, new_vacancies=5, leads=0)
    assert full is None
    assert short is not None and short.key == "day_summary" and short.text.startswith("⚠️")
