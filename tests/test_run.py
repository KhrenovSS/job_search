from hh_scout.browser.session import BrowserUnavailable
from hh_scout.config import Settings
from hh_scout.pipeline import repo, run as run_mod


class _Stats:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_run_crawl_survives_browser_failure(monkeypatch, tmp_path):
    calls = []

    class FakeCollector:
        def __init__(self, *a, **kw):
            pass

        def run(self, run_id):
            calls.append("collect")
            raise BrowserUnavailable("Marionette не отвечает")

    class FakeTriager:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            calls.append("triage")
            return _Stats(opened=2, bridge_calls=1)

    class FakeDetails:
        def __init__(self, *a, **kw):
            pass

        def run(self, run_id):
            calls.append("details")
            return _Stats(page_loads=0, outcomes={})

    class FakeEvaluator:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            calls.append("evaluate")
            return _Stats(evaluated=3, bridge_calls=1)

    class FakeLetters:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            calls.append("letters")
            return _Stats(written=1, bridge_calls=1)

    monkeypatch.setattr(run_mod, "Collector", FakeCollector)
    monkeypatch.setattr(run_mod, "Triager", FakeTriager)
    monkeypatch.setattr(run_mod, "DetailsFetcher", FakeDetails)
    monkeypatch.setattr(run_mod, "Evaluator", FakeEvaluator)
    monkeypatch.setattr(run_mod, "CoverLetterWriter", FakeLetters)
    monkeypatch.setattr(run_mod.prefilter, "run", lambda conn, s: {"passed": 5})

    s = Settings(_env_file=None)
    db = tmp_path / "t.db"
    report = run_mod.run_crawl(s, db, "manual")
    assert calls == ["collect", "triage", "evaluate", "letters"]  # details skipped after browser failure
    assert report.browser_error and "Marionette" in report.browser_error
    assert report.evaluated == 3 and report.letters == 1 and report.bridge_calls == 3 and not report.ok
    from hh_scout.db import open_db
    conn = open_db(db)
    last = repo.last_run(conn)
    assert last["status"] == "failed" and "Marionette" in last["error"] and last["evaluated"] == 3
    assert "Сбор завершён с замечаниями" in report.as_text()


def test_daily_cap_is_random_once_per_day_and_stable():
    from hh_scout.db import connect, kv_get, migrate
    from hh_scout.pipeline.budget import daily_cap

    conn = connect(":memory:")
    migrate(conn)
    s = Settings(_env_file=None, daily_page_loads_min=100, daily_page_loads_max=140)
    cap = daily_cap(conn, s, today="2026-09-09")
    assert 100 <= cap <= 140
    assert daily_cap(conn, s, today="2026-09-09") == cap  # stable within the day
    nxt = daily_cap(conn, s, today="2026-09-10")
    assert 100 <= nxt <= 140 and kv_get(conn, "daily_cap:2026-09-09") is None  # old key dropped


def test_run_crawl_budget_share_limits_collector_and_details(monkeypatch, tmp_path):
    budgets = {}

    class FakeCollector:
        def __init__(self, *a, page_budget=None, **kw):
            budgets["collect"] = page_budget

        def run(self, run_id):
            return _Stats(page_loads=12, new_vacancies=3)

    class FakeDetails:
        def __init__(self, *a, page_budget=None, **kw):
            budgets["details"] = page_budget

        def run(self, run_id):
            return _Stats(page_loads=28, outcomes={"prefiltered": 20})

    class FakeBridgeStep:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            return _Stats(opened=0, bridge_calls=0, evaluated=0, written=0)

    monkeypatch.setattr(run_mod, "Collector", FakeCollector)
    monkeypatch.setattr(run_mod, "DetailsFetcher", FakeDetails)
    monkeypatch.setattr(run_mod, "Triager", FakeBridgeStep)
    monkeypatch.setattr(run_mod, "Evaluator", FakeBridgeStep)
    monkeypatch.setattr(run_mod, "CoverLetterWriter", FakeBridgeStep)
    monkeypatch.setattr(run_mod.prefilter, "run", lambda conn, s: {"passed": 0})

    s = Settings(_env_file=None, daily_page_loads_min=120, daily_page_loads_max=120)
    report = run_mod.run_crawl(s, tmp_path / "t.db", "schedule", budget=40)
    assert budgets == {"collect": 40, "details": 28}  # details get what the collector left of this run's share
    assert report.page_loads == 40 and report.daily_cap == 120 and report.used_today == 40
    assert "за день 40/120" in report.as_text()
    # a manual run takes everything that is left of the day
    report2 = run_mod.run_crawl(s, tmp_path / "t.db", "manual")
    assert budgets["collect"] == 80


def test_run_crawl_deadline_stops_browsing_and_is_reported(monkeypatch, tmp_path):
    from datetime import datetime, timedelta

    from hh_scout.config import TZ

    seen = {}

    class FakeCollector:
        def __init__(self, *a, **kw):
            seen["stop"] = kw["should_stop"]

        def run(self, run_id):
            assert seen["stop"]() is True  # the deadline is already in the past -> bursts stop at once
            return _Stats(page_loads=0, new_vacancies=0, search_pages=0, cards_seen=0, not_logged_in=False)

    class FakeNoop:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a):
            return _Stats(opened=0, bridge_calls=0, page_loads=0, outcomes={}, evaluated=0, written=0)

    monkeypatch.setattr(run_mod, "Collector", FakeCollector)
    monkeypatch.setattr(run_mod, "Triager", FakeNoop)
    monkeypatch.setattr(run_mod, "DetailsFetcher", FakeNoop)
    monkeypatch.setattr(run_mod, "Evaluator", FakeNoop)
    monkeypatch.setattr(run_mod, "CoverLetterWriter", FakeNoop)
    monkeypatch.setattr(run_mod.prefilter, "run", lambda conn, s: {"passed": 0})
    settings = Settings(_env_file=None, prompts_dir=tmp_path, daily_page_loads_min=50, daily_page_loads_max=50)
    deadline = datetime.now(TZ) - timedelta(minutes=1)
    report = run_mod.run_crawl(settings, tmp_path / "t.db", "schedule", budget=10, deadline=deadline)
    assert report.deadline_hit and "остановлен по концу окна" in report.as_text()


def _noop_bridge_steps(monkeypatch):
    class FakeNoop:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a):
            return _Stats(opened=0, bridge_calls=0, page_loads=0, outcomes={}, evaluated=0, written=0)

    for name in ("Triager", "DetailsFetcher", "Evaluator", "CoverLetterWriter"):
        monkeypatch.setattr(run_mod, name, FakeNoop)
    monkeypatch.setattr(run_mod.prefilter, "run", lambda conn, s: {"passed": 0})


def test_profi_stage_runs_first_shares_the_budget_and_does_not_block_hh(monkeypatch, tmp_path):
    from hh_scout.profi.pages import ProfiBlocked

    budgets = {}

    class FakeProfi:
        def __init__(self, *a, page_budget=None, **kw):
            budgets["profi"] = page_budget

        def run(self, run_id):
            return _Stats(page_loads=1, orders_seen=2, new_orders=1)

    class FakeCollector:
        def __init__(self, *a, page_budget=None, **kw):
            budgets["hh"] = page_budget

        def run(self, run_id):
            return _Stats(page_loads=3, new_vacancies=5, search_pages=1, cards_seen=5, not_logged_in=False)

    _noop_bridge_steps(monkeypatch)
    monkeypatch.setattr(run_mod, "ProfiCollector", FakeProfi)
    monkeypatch.setattr(run_mod, "Collector", FakeCollector)
    s = Settings(_env_file=None, prompts_dir=tmp_path, daily_page_loads_min=50, daily_page_loads_max=50, profi_enabled=True)
    report = run_mod.run_crawl(s, tmp_path / "t.db", "schedule", budget=10)
    assert budgets == {"profi": 1, "hh": 9}  # profi takes its page first, hh gets the rest
    assert report.page_loads == 4 and report.profi_orders == 2 and report.profi_new == 1
    assert "profi.ru: заказов в ленте 2 · новых 1" in report.as_text() and report.ok

    # the cabinet is logged out: profi is reported, hh still crawls; profi disabled -> never constructed
    class BlockedProfi(FakeProfi):
        def run(self, run_id):
            raise ProfiBlocked("нет кабинета")

    monkeypatch.setattr(run_mod, "ProfiCollector", BlockedProfi)
    report2 = run_mod.run_crawl(s, tmp_path / "t2.db", "schedule", budget=10)
    assert report2.profi_error == "нет кабинета" and budgets["hh"] == 9 and report2.browser_error is None
    budgets.clear()
    s_off = Settings(_env_file=None, prompts_dir=tmp_path, daily_page_loads_min=50, daily_page_loads_max=50)
    report3 = run_mod.run_crawl(s_off, tmp_path / "t3.db", "manual", budget=10)
    assert "profi" not in budgets and budgets["hh"] == 10 and report3.profi_orders is None
