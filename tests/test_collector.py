import random

from hh_scout.browser.hh_pages import VacancyCard
from hh_scout.browser.session import PageBudgetExceeded
from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.pipeline import repo
from hh_scout.pipeline.collector import Collector, plan_tasks


def _card(i, applied=False):
    return VacancyCard(hh_id=str(i), title=f"Инженер {i}", employer="ООО", url=f"https://hh.ru/vacancy/{i}",
                       area_name="Москва", work_format="office", employment="full",
                       compensation={"from": 100000, "currencyCode": "RUR", "gross": False},
                       published_at="2026-09-08T10:00:00+03:00", applied=applied)


def _search_state(ids, has_next, page=0, user="applicant", accept_temporary=False):
    return {"userType": user, "vacancySearchResult": {
        "criteria": {"page": page}, "totalResults": 999,
        "paging": {"next": {"page": page + 1, "disabled": not has_next}} if has_next else None,
        "vacancies": [{"vacancyId": i, "name": f"Инженер {i}", "company": {"name": "ООО"}, "area": {"name": "Москва"},
                       "workFormats": [{"workFormatsElement": ["ON_SITE"]}], "employmentForm": "FULL",
                       "acceptTemporary": accept_temporary,
                       "civilLawContracts": [{"civilLawContractsElement": ["INDIVIDUAL_ENTREPRENEUR"]}] if accept_temporary else [{}],
                       "compensation": {"noCompensation": {}}, "publicationTime": {"$": "2026-09-08T10:00:00+03:00"}} for i in ids]}}


NEGOTIATIONS_STATE = {"userType": "applicant", "applicantNegotiations": {"topicList": [
    {"vacancyId": 77, "lastState": "RESPONSE", "conversationMessagesCount": 1},
    {"vacancyId": 1001, "lastState": "INTERVIEW", "conversationMessagesCount": 3},
]}, "suitableVacancies": {"vacancies": [{"vacancyId": 5000, "name": "Похожая", "company": {"name": "X"}}]}}


class FakeSession:
    """Serves canned states by URL substring; counts loads against a budget like the real one."""

    def __init__(self, budget, log):
        self.page_budget = budget
        self.page_loads = 0
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def open(self, url):
        if self.page_loads >= self.page_budget:
            raise PageBudgetExceeded("budget")
        self.page_loads += 1
        self.log.append(url)
        if "negotiations" in url:
            return NEGOTIATIONS_STATE
        page = int(url.split("&page=")[1].split("&")[0]) if "&page=" in url else 0
        if "accept_temporary=true" in url:  # gph pass: hh's own "ГПХ или совместительство" filter
            return _search_state([4001], has_next=False, accept_temporary=True)
        if "employment_form" in url:  # project pass: single page, includes an already-known id
            return _search_state([77, 3001, 3002], has_next=False)
        if "work_format=REMOTE" in url:
            return _search_state([2001], has_next=False)
        # regional: 3 pages, page 2 brings nothing new
        if page == 0:
            return _search_state([1001, 1002, 1003], has_next=True, page=0)
        if page == 1:
            return _search_state([1004], has_next=True, page=1)
        return _search_state([1001, 1002], has_next=True, page=2)


def _make(monkeypatch, budget=60):
    from hh_scout.browser import pacing
    from hh_scout.pipeline import collector as mod
    monkeypatch.setattr(pacing, "sleep", lambda s: None)
    monkeypatch.setattr(mod, "resolve_region_ids", lambda conn, s: [1, 2019])
    monkeypatch.setattr(mod, "SEARCH_QUERIES", ("Q",))
    conn = connect(":memory:")
    migrate(conn)
    loads = []
    settings = Settings(_env_file=None, daily_page_loads_min=budget, daily_page_loads_max=budget, max_pages_per_query=4)
    c = Collector(settings, conn, session_factory=lambda b: FakeSession(b, loads), rng=random.Random(0), page_budget=budget)
    return c, conn, loads


def test_plan_tasks_covers_four_passes_per_query():
    tasks = plan_tasks([1, 2019], queries=("a", "b"), rng=random.Random(1))
    assert len(tasks) == 8
    assert {t.search_pass for t in tasks} == {"regional", "remote", "project", "gph"}
    assert all(t.areas == (1, 2019) for t in tasks if t.search_pass == "regional")
    gph = [t for t in tasks if t.search_pass == "gph"]
    assert len(gph) == 2 and all(t.accept_temporary for t in gph)


def test_collect_dedups_stops_early_and_syncs_negotiations(monkeypatch):
    c, conn, loads = _make(monkeypatch)
    stats = c.run()
    # negotiations + regional pages 0,1,2 + remote + project + gph = 7 loads; page 2 had nothing new -> stop
    assert stats.page_loads == 7 and stats.bursts >= 1
    assert sum("negotiations" in u for u in loads) == 1
    assert stats.applied_synced == 2
    rows = {r["hh_id"]: r for r in conn.execute("SELECT * FROM vacancies")}
    # 1001 was applied per negotiations -> skipped/applied even though the search saw it
    assert rows["1001"]["applied"] == 1 and rows["1001"]["status"] == "skipped"
    assert rows["1001"]["has_chat"] == 1
    assert rows["77"]["status"] == "skipped" and rows["77"]["search_pass"] == "negotiations"
    assert rows["5000"]["search_pass"] == "similar" and rows["5000"]["status"] == "new"
    assert rows["1002"]["status"] == "new" and rows["2001"]["search_pass"] == "remote"
    # the gph pass stores hh's own ГПХ flag; vacancies from other passes keep 0
    assert rows["4001"]["search_pass"] == "gph" and rows["4001"]["accept_temporary"] == 1
    assert rows["4001"]["civil_law_contracts"] == '["INDIVIDUAL_ENTREPRENEUR"]'
    assert rows["2001"]["accept_temporary"] == 0 and rows["2001"]["civil_law_contracts"] is None
    assert rows["3001"]["search_pass"] == "project"
    assert stats.new_vacancies == 8  # 1002,1003,1004 + 2001 + 3001,3002 + 4001 + 5000 (1001 and 77 came in as applied stubs)
    # second run adds nothing
    c2, _, _ = _make(monkeypatch)
    c2.conn = conn
    assert c2.run().new_vacancies == 0


def test_budget_is_respected(monkeypatch):
    c, conn, loads = _make(monkeypatch, budget=3)
    stats = c.run()
    assert stats.page_loads == 3 and len(loads) == 3
    assert stats.stopped_reason and "лимит" in stats.stopped_reason


def test_repo_insert_and_mark_applied():
    conn = connect(":memory:")
    migrate(conn)
    assert repo.insert_card(conn, _card(1), "search:0", "regional") is True
    assert repo.insert_card(conn, _card(1), "search:0", "regional") is False
    repo.mark_applied(conn, "1", has_chat=True)
    row = conn.execute("SELECT * FROM vacancies WHERE hh_id='1'").fetchone()
    assert row["applied"] == 1 and row["has_chat"] == 1 and row["status"] == "skipped" and row["skip_reason"] == "applied"
    assert repo.insert_card(conn, _card(2, applied=True), "search:0", "regional") is True
    assert conn.execute("SELECT status FROM vacancies WHERE hh_id='2'").fetchone()["status"] == "skipped"


def test_fail_stale_runs():
    conn = connect(":memory:")
    migrate(conn)
    conn.execute("INSERT INTO runs(started_at, status, trigger) VALUES ('2020-01-01T00:00:00+00:00', 'running', 'manual')")
    fresh = repo.start_run(conn, "manual")
    assert repo.fail_stale_runs(conn, 3.0) == 1
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (fresh,)).fetchone()["status"] == "running"
    assert conn.execute("SELECT status FROM runs WHERE id = 1").fetchone()["status"] == "failed"
