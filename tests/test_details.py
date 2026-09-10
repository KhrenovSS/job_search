import json
import random

from hh_scout.browser.session import PageBudgetExceeded
from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.pipeline import repo
from hh_scout.pipeline.details import DetailsFetcher


class FakeSession:
    def __init__(self, budget, states, log):
        self.page_budget, self.page_loads, self.states, self.log = budget, 0, states, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def open(self, url):
        if self.page_loads >= self.page_budget:
            raise PageBudgetExceeded("b")
        self.page_loads += 1
        self.log.append(url)
        return self.states[url.rsplit("/", 1)[1]]


def _db_with(ids_prio):
    conn = connect(":memory:")
    migrate(conn)
    for hh_id, prio in ids_prio:
        conn.execute("INSERT INTO vacancies(hh_id,title,url,source,search_pass,status,triage_priority,published_at,first_seen_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (hh_id, "t", "u", "s", "regional", "to_fetch", prio, "2026-09-08", "t", "t"))
    return conn


def test_details_saves_trimmed_json_in_priority_order(monkeypatch, vacancy_state):
    from hh_scout.browser import pacing
    monkeypatch.setattr(pacing, "sleep", lambda s: None)
    conn = _db_with([("136519902", 2), ("999", 1), ("777", 3)])
    archived = json.loads(json.dumps(vacancy_state))
    archived["vacancyView"]["vacancyId"] = 999
    archived["vacancyView"]["status"]["archived"] = True
    weird = {"vacancyView": {"vacancyId": 777, "name": "x", "status": {}}}
    states = {"136519902": vacancy_state, "999": archived, "777": weird}
    loads = []
    settings = Settings(_env_file=None)
    f = DetailsFetcher(settings, conn, session_factory=lambda b: FakeSession(b, states, loads), rng=random.Random(0), page_budget=10)
    stats = f.run()
    assert [u.rsplit("/", 1)[1] for u in loads] == ["999", "136519902", "777"]  # priority 1 first
    rows = {r["hh_id"]: r for r in conn.execute("SELECT * FROM vacancies")}
    main = rows["136519902"]
    assert main["status"] == "prefiltered"
    raw = json.loads(main["raw_json"])
    assert "description" in raw and "CODESYS" in json.dumps(raw["keySkills"], ensure_ascii=False)
    assert "logos" not in json.dumps(raw)  # trimmed
    assert main["salary_from"] == 121800 and main["salary_to"] == 156600  # 140k/180k gross -> net
    assert rows["999"]["status"] == "skipped" and rows["999"]["skip_reason"] == "archived"
    assert rows["777"]["status"] == "prefiltered"  # minimal but valid vacancyView
    assert stats.page_loads == 3 and stats.outcomes["prefiltered"] == 2


def test_details_respects_budget(monkeypatch, vacancy_state):
    from hh_scout.browser import pacing
    monkeypatch.setattr(pacing, "sleep", lambda s: None)
    conn = _db_with([("1", 1), ("2", 1), ("3", 1)])
    st = json.loads(json.dumps(vacancy_state))
    states = {}
    for i in ("1", "2", "3"):
        s2 = json.loads(json.dumps(st))
        s2["vacancyView"]["vacancyId"] = int(i)
        states[i] = s2
    f = DetailsFetcher(Settings(_env_file=None), conn, session_factory=lambda b: FakeSession(b, states, []), rng=random.Random(1), page_budget=2)
    stats = f.run()
    assert stats.page_loads == 2 and "лимит" in stats.stopped_reason
    assert repo.count_by_status(conn) == {"prefiltered": 2, "to_fetch": 1}


def test_page_loads_today_and_run_metrics():
    conn = connect(":memory:")
    migrate(conn)
    rid = repo.start_run(conn, "manual")
    repo.finish_run(conn, rid, "ok", page_loads=7, prefiltered=3)
    conn.execute("INSERT INTO runs(started_at,status,trigger,page_loads) VALUES ('2020-01-01T00:00:00+00:00','ok','manual',99)")
    assert repo.page_loads_today(conn) == 7
    from datetime import datetime, timedelta
    from hh_scout.config import TZ
    w = repo.work_totals(conn, datetime.now(TZ) - timedelta(hours=24))
    assert w == {"sittings": 0, "page_loads": 7}  # manual runs count pages but are not sittings
    from hh_scout.db import utcnow
    conn.execute("INSERT INTO runs(started_at,status,trigger,page_loads) VALUES (?, 'ok', 'schedule', 30)", (utcnow(),))
    assert repo.work_totals(conn, datetime.now(TZ) - timedelta(hours=24)) == {"sittings": 1, "page_loads": 37}


def test_expire_low_priority_drops_only_old_priority_3():
    from datetime import datetime, timedelta, timezone

    conn = connect(":memory:")
    migrate(conn)
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rows = [("a", 3, old), ("b", 3, fresh), ("c", 1, old), ("d", None, old), ("e", 2, old)]
    for hh_id, prio, upd in rows:
        conn.execute("INSERT INTO vacancies(hh_id,title,url,source,search_pass,status,triage_priority,published_at,first_seen_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (hh_id, "t", "u", "s", "regional", "to_fetch", prio, "2026-09-08", "t", upd))
    conn.execute("UPDATE vacancies SET status='sent' WHERE hh_id='e'")
    assert repo.expire_low_priority(conn, 3) == 2  # a (old, prio 3) and d (old, no priority = 3)
    got = {r["hh_id"]: (r["status"], r["skip_reason"]) for r in conn.execute("SELECT hh_id, status, skip_reason FROM vacancies")}
    assert got["a"] == ("skipped", "low_priority_expired") and got["d"] == ("skipped", "low_priority_expired")
    assert got["b"][0] == "to_fetch" and got["c"][0] == "to_fetch" and got["e"][0] == "sent"
