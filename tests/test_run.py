import pytest

from hh_scout.browser import pacing
from hh_scout.browser.session import BrowserUnavailable
from hh_scout.config import Settings
from hh_scout.pipeline import repo, run as run_mod


@pytest.fixture(autouse=True)
def _no_real_pauses(monkeypatch):
    """The pause between browser stages (v9.11) is real minutes; tests only record that it happened."""
    pauses = []
    monkeypatch.setattr(pacing, "sleep", lambda s: pauses.append(s))
    return pauses


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

    s = Settings(_env_file=None, daily_page_loads_min=120, daily_page_loads_max=120, details_budget_share=0.4)
    report = run_mod.run_crawl(s, tmp_path / "t.db", "schedule", budget=40)
    # 40 minus the 16-page reserve goes to collection; details then get the reserve plus the 12 it did not spend
    assert budgets == {"collect": 24, "details": 28}
    assert report.page_loads == 40 and report.daily_cap == 120 and report.used_today == 40
    assert "за день 40/120" in report.as_text()
    # a manual run takes everything that is left of the day, minus the same reserve
    report2 = run_mod.run_crawl(s, tmp_path / "t.db", "manual")
    assert budgets["collect"] == 48  # (120 - 40) - round(80 * 0.4)


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
    s = Settings(_env_file=None, prompts_dir=tmp_path, daily_page_loads_min=50, daily_page_loads_max=50,
                 profi_enabled=True, details_budget_share=0.0)
    report = run_mod.run_crawl(s, tmp_path / "t.db", "schedule", budget=10)
    assert budgets == {"profi": 1, "hh": 9}  # profi takes its page first, hh gets the rest
    assert report.page_loads == 4 and report.profi_orders == 2 and report.profi_new == 1
    assert "profi.ru: заказов в ленте 2 · новых 1" in report.as_text() and report.ok

    # the cabinet is logged out: profi is reported, hh still crawls; profi disabled -> never constructed
    class BlockedProfi(FakeProfi):
        def run(self, run_id):
            self.stats = _Stats(page_loads=1)   # the feed page was loaded before the missing cabinet was noticed
            raise ProfiBlocked("нет кабинета")

    monkeypatch.setattr(run_mod, "ProfiCollector", BlockedProfi)
    report2 = run_mod.run_crawl(s, tmp_path / "t2.db", "schedule", budget=10)
    assert report2.profi_error == "нет кабинета" and budgets["hh"] == 9 and report2.browser_error is None
    budgets.clear()
    s_off = Settings(_env_file=None, prompts_dir=tmp_path, daily_page_loads_min=50, daily_page_loads_max=50,
                     details_budget_share=0.0)
    report3 = run_mod.run_crawl(s_off, tmp_path / "t3.db", "manual", budget=10)
    assert "profi" not in budgets and budgets["hh"] == 10 and report3.profi_orders is None


def test_details_reserve_survives_a_greedy_collector(monkeypatch, tmp_path):
    """A wide search must not eat the whole sitting: vacancy pages keep a guaranteed floor.

    Regression for 13.09: collection spent 15 of 15 pages, DetailsFetcher got 0, nothing was opened
    and the digest had no leads even though cards had been collected.
    """
    budgets = {}

    class GreedyCollector:
        def __init__(self, *a, page_budget=None, **kw):
            budgets["collect"] = page_budget

        def run(self, run_id):
            # spends every page it is given
            return _Stats(page_loads=budgets["collect"], new_vacancies=99, search_pages=budgets["collect"],
                          cards_seen=99, not_logged_in=False)

    class FakeDetails:
        def __init__(self, *a, page_budget=None, **kw):
            budgets["details"] = page_budget

        def run(self, run_id):
            return _Stats(page_loads=0, outcomes={"prefiltered": 0})

    _noop_bridge_steps(monkeypatch)
    monkeypatch.setattr(run_mod, "Collector", GreedyCollector)
    monkeypatch.setattr(run_mod, "DetailsFetcher", FakeDetails)

    s = Settings(_env_file=None, prompts_dir=tmp_path, daily_page_loads_min=100, daily_page_loads_max=100,
                 details_budget_share=0.4)
    report = run_mod.run_crawl(s, tmp_path / "t.db", "schedule", budget=30)
    assert budgets["collect"] == 18 and budgets["details"] == 12  # 30 - round(30 * 0.4), then the reserve
    assert report.browser_error is None and report.ok


def test_zero_details_reserve_is_not_reported_as_an_exhausted_limit(monkeypatch, tmp_path):
    """A budget too small to split is not an error — every page simply goes to vacancy pages."""
    budgets = {}

    class FakeDetails:
        def __init__(self, *a, page_budget=None, **kw):
            budgets["details"] = page_budget

        def run(self, run_id):
            return _Stats(page_loads=0, outcomes={})

    _noop_bridge_steps(monkeypatch)
    monkeypatch.setattr(run_mod, "DetailsFetcher", FakeDetails)
    s = Settings(_env_file=None, prompts_dir=tmp_path, daily_page_loads_min=100, daily_page_loads_max=100,
                 details_budget_share=1.0)
    report = run_mod.run_crawl(s, tmp_path / "t.db", "schedule", budget=8)
    assert budgets["details"] == 8 and report.browser_error is None


def test_profi_network_failure_does_not_fail_the_hh_run(monkeypatch, tmp_path):
    """profi.ru unreachable (13.09: its DNS stopped resolving) must not mark the run failed."""

    class DeadProfi:
        def __init__(self, *a, **kw):
            pass

        def run(self, run_id):
            raise RuntimeError("Reached error page: about:neterror?e=dnsNotFound\nStacktrace:\nRemoteError@chrome://...")

    class FakeCollector:
        def __init__(self, *a, page_budget=None, **kw):
            pass

        def run(self, run_id):
            return _Stats(page_loads=3, new_vacancies=5, search_pages=1, cards_seen=5, not_logged_in=False)

    _noop_bridge_steps(monkeypatch)
    monkeypatch.setattr(run_mod, "ProfiCollector", DeadProfi)
    monkeypatch.setattr(run_mod, "Collector", FakeCollector)
    s = Settings(_env_file=None, prompts_dir=tmp_path, daily_page_loads_min=50, daily_page_loads_max=50,
                 profi_enabled=True)
    report = run_mod.run_crawl(s, tmp_path / "t.db", "schedule", budget=10)
    assert report.ok and report.errors == []          # hh.ru worked: the run is a success
    assert report.new_vacancies == 5                  # collection still happened
    assert report.profi_error == "лента недоступна (RuntimeError)"
    assert "Stacktrace" not in report.as_text()       # no Marionette dump in runs.error / the alert


def test_step_2b_revives_orphan_twins_before_skipping_covered_ones(monkeypatch, tmp_path):
    """A twin skipped against a vacancy that ended up rejected goes back into the queue, and is reported."""
    from hh_scout.db import open_db, utcnow

    order = []
    real_revive, real_skip = run_mod.dedup.revive_orphans, run_mod.dedup.skip_covered
    monkeypatch.setattr(run_mod.dedup, "revive_orphans", lambda c, s: (order.append("revive"), real_revive(c, s))[1])
    monkeypatch.setattr(run_mod.dedup, "skip_covered", lambda c, s, st: (order.append("skip"), real_skip(c, s, st))[1])
    for name in ("Collector", "Triager", "DetailsFetcher", "Evaluator", "CoverLetterWriter"):
        monkeypatch.setattr(run_mod, name, _fake_stage())
    monkeypatch.setattr(run_mod.prefilter, "run", lambda conn, s: {"passed": 0})

    db = tmp_path / "t.db"
    conn = open_db(db)
    for hh_id, status, reason, prio in (("R", "rejected", None, None), ("twin", "skipped", "duplicate_employer:R", 1)):
        conn.execute("INSERT INTO vacancies(hh_id, title, employer, employer_id, url, source, search_pass, status, "
                     "skip_reason, triage_priority, first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (hh_id, "Инженер", "Альфа", "1", "u", "s", "remote", status, reason, prio, "t", utcnow()))
    conn.commit()
    conn.close()

    report = run_mod.run_crawl(Settings(_env_file=None), db, "manual")
    assert order == ["revive", "skip"]                    # revive first: a freed twin must not be skipped again
    assert report.revived_duplicates == 1
    assert "дублей вернулось: 1" in report.as_text()
    conn = open_db(db)
    assert conn.execute("SELECT status, skip_reason FROM vacancies WHERE hh_id = 'twin'").fetchone()[:] == ("to_fetch", None)


def _fake_stage():
    class Fake:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return _Stats(page_loads=0, outcomes={}, opened=0, bridge_calls=0, evaluated=0, written=0,
                          new_vacancies=0, search_pages=0, cards_seen=0, not_logged_in=False, format_errors=0)
    return Fake


# --- v9.11: the rhythm holds across stages, a block stops the run and is reported ------------------

def test_a_pause_separates_collection_from_vacancy_pages(monkeypatch, tmp_path, _no_real_pauses):
    """collect → details used to be gapless: ~20 minutes of loading per sitting instead of 7–13-minute bursts."""
    order = []

    class FakeCollector:
        def __init__(self, *a, **kw):
            pass

        def run(self, run_id):
            order.append("collect")
            return _Stats(page_loads=12, new_vacancies=3, search_pages=12, cards_seen=50, not_logged_in=False)

    class FakeDetails:
        def __init__(self, *a, **kw):
            pass

        def run(self, run_id):
            order.append(("details", len(_no_real_pauses)))
            return _Stats(page_loads=5, outcomes={"prefiltered": 5})

    _noop_bridge_steps(monkeypatch)
    monkeypatch.setattr(run_mod, "Collector", FakeCollector)
    monkeypatch.setattr(run_mod, "DetailsFetcher", FakeDetails)
    s = Settings(_env_file=None, prompts_dir=tmp_path, daily_page_loads_min=50, daily_page_loads_max=50)
    run_mod.run_crawl(s, tmp_path / "t.db", "schedule", budget=40)
    assert order == ["collect", ("details", 1)]         # exactly one gap slept before the vacancy pages
    assert 4 * 60 <= _no_real_pauses[0] <= 9 * 60

    # nothing was loaded by the collector -> no pause either
    class IdleCollector(FakeCollector):
        def run(self, run_id):
            return _Stats(page_loads=0, new_vacancies=0, search_pages=0, cards_seen=0, not_logged_in=False)

    monkeypatch.setattr(run_mod, "Collector", IdleCollector)
    _no_real_pauses.clear()
    run_mod.run_crawl(s, tmp_path / "t2.db", "schedule", budget=40)
    assert _no_real_pauses == []


def test_a_blocked_search_stops_the_run_flags_it_and_keeps_the_pages_it_loaded(monkeypatch, tmp_path):
    from hh_scout.browser.session import HHBlocked

    calls = []

    class BlockedCollector:
        def __init__(self, *a, **kw):
            self.stats = _Stats(page_loads=4, new_vacancies=7)

        def run(self, run_id):
            calls.append("collect")
            raise HHBlocked("hh.ru вернул страницу без данных (заголовок: 'Проверка')")

    class FakeDetails:
        def __init__(self, *a, **kw):
            pass

        def run(self, run_id):
            calls.append("details")
            return _Stats(page_loads=1, outcomes={})

    _noop_bridge_steps(monkeypatch)
    monkeypatch.setattr(run_mod, "Collector", BlockedCollector)
    monkeypatch.setattr(run_mod, "DetailsFetcher", FakeDetails)
    s = Settings(_env_file=None, prompts_dir=tmp_path, daily_page_loads_min=50, daily_page_loads_max=50)
    report = run_mod.run_crawl(s, tmp_path / "t.db", "schedule", budget=20)
    assert calls == ["collect"]                       # no fresh session into the block
    assert report.blocked and not report.ok and report.page_loads == 4 and report.new_vacancies == 7
    assert "🚫 hh.ru" in report.as_text()
    from hh_scout import health
    assert [a.key for a in health.analyze_report(report)] == ["hh_blocked"]
    from hh_scout.db import open_db
    last = repo.last_run(open_db(tmp_path / "t.db"))
    assert last["status"] == "failed" and last["page_loads"] == 4
