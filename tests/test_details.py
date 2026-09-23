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


def _db_channels(rows):
    conn = connect(":memory:")
    migrate(conn)
    for hh_id, search_pass, prio, published in rows:
        conn.execute("INSERT INTO vacancies(hh_id,title,url,source,search_pass,status,triage_priority,published_at,first_seen_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (hh_id, "t", "u", "s", search_pass, "to_fetch", prio, published, "t", "t"))
    return conn


def _states_for(ids, vacancy_state):
    states = {}
    for i in ids:
        s2 = json.loads(json.dumps(vacancy_state))
        s2["vacancyView"]["vacancyId"] = int(i)
        s2["vacancyView"]["company"]["id"] = int(i)   # distinct employers: no duplicate_employer collapse
        states[i] = s2
    return states


def test_details_share_pages_between_channels(monkeypatch, vacancy_state):
    """23.09: fresh vacancy cards always won and admitted plant companies were never opened. With shares an older
    plant row gets its turn; a hot (priority 1-2) card still goes first."""
    from hh_scout.browser import pacing
    monkeypatch.setattr(pacing, "sleep", lambda s: None)
    rows = [(str(100 + i), "regional", 3, f"2026-09-23T10:{i:02d}") for i in range(6)]
    rows += [(str(200 + i), "plant", 3, f"2026-09-20T10:{i:02d}") for i in range(6)]
    rows += [("300", "regional", 1, "2026-09-01")]
    conn = _db_channels(rows)
    states = _states_for([r[0] for r in rows], vacancy_state)
    loads = []
    settings = Settings(_env_file=None, details_channel_shares="vacancy:0.5,plant:0.5")
    f = DetailsFetcher(settings, conn, session_factory=lambda b: FakeSession(b, states, loads), rng=random.Random(0), page_budget=5)
    stats = f.run()
    opened = [u.rsplit("/", 1)[1] for u in loads]
    assert opened[0] == "300"                                   # priority 1 first
    assert sum(1 for i in opened if i.startswith("2")) == 2     # plant gets its half of the rest
    assert stats.channels == {"vacancy": 3, "plant": 2}


def test_details_unused_share_flows_to_others(monkeypatch, vacancy_state):
    from hh_scout.browser import pacing
    monkeypatch.setattr(pacing, "sleep", lambda s: None)
    rows = [(str(100 + i), "regional", 3, f"2026-09-23T10:{i:02d}") for i in range(4)]
    conn = _db_channels(rows)
    states = _states_for([r[0] for r in rows], vacancy_state)
    loads = []
    settings = Settings(_env_file=None, details_channel_shares="vacancy:0.2,plant:0.8")
    f = DetailsFetcher(settings, conn, session_factory=lambda b: FakeSession(b, states, loads), rng=random.Random(0), page_budget=4)
    assert f.run().page_loads == 4                              # no plant rows: vacancies take the whole budget


def test_channel_shares_validation():
    import pytest
    assert Settings(_env_file=None, details_channel_shares="").details_channel_shares_map == {}
    assert Settings(_env_file=None).details_channel_shares_map["plant"] == 0.25
    for bad in ("plant:1.5", "plant", "vacancy:0.7,plant:0.5"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, details_channel_shares=bad)
