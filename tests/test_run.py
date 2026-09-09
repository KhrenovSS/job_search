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
