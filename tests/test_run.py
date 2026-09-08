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
