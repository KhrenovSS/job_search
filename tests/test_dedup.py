"""One lead per company (pipeline/dedup.py): early skips before triage / page load, collapse among evaluated leads."""

from datetime import datetime, timedelta, timezone

from hh_scout.config import Settings
from hh_scout.db import connect, migrate, utcnow
from hh_scout.pipeline import dedup, repo


def _conn():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _vac(conn, hh_id, status, *, employer="ООО Ромашка", employer_id="100", site="hh", total=None, updated_at=None, area="Москва"):
    conn.execute("INSERT INTO vacancies(hh_id, site, title, employer, employer_id, url, area_name, source, search_pass, status, "
                 "first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (hh_id, site, f"Инженер {hh_id}", employer, employer_id, "u", area, "s", "regional", status, "t", updated_at or utcnow()))
    vid = conn.execute("SELECT id FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()[0]
    if total is not None:
        conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                     "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                     "VALUES (?,80,0,0,80,60,?,'maybe',0,'staff','integrator','v','p','[]',?)", (vid, total, utcnow()))
    return vid


def _status(conn, hh_id):
    r = conn.execute("SELECT status, skip_reason FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()
    return r["status"], r["skip_reason"]


def test_skip_covered_by_id_by_name_and_window():
    s = Settings(_env_file=None, employer_repeat_days=90)
    conn = _conn()
    _vac(conn, "L", "sent")                                        # the lead the company already got
    _vac(conn, "a", "triage")                                      # same id → duplicate
    _vac(conn, "b", "triage", employer="ооо ромашка", employer_id=None)  # old card, name only, other case → duplicate
    _vac(conn, "c", "triage", employer="ООО Ромашка", employer_id="200")  # same name, different hh id → still one company
    _vac(conn, "d", "triage", employer="Другая", employer_id="300")  # free company
    _vac(conn, "p", "triage", employer="ООО Ромашка", employer_id=None, site="profi")  # profi.ru is exempt
    assert dedup.skip_covered(conn, s, "triage") == 3
    assert _status(conn, "a") == ("skipped", "duplicate_employer:L")
    assert _status(conn, "b") == ("skipped", "duplicate_employer:L")
    assert _status(conn, "c") == ("skipped", "duplicate_employer:L")
    assert _status(conn, "d") == ("triage", None) and _status(conn, "p") == ("triage", None)

    # the lead is older than the repeat window → the company may get a new one
    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    conn.execute("UPDATE vacancies SET updated_at = ? WHERE hh_id = 'L'", (old,))
    _vac(conn, "e", "triage")
    assert dedup.skip_covered(conn, s, "triage") == 0 and _status(conn, "e") == ("triage", None)
    # ...unless EMPLOYER_REPEAT_DAYS=0 (forever)
    assert dedup.skip_covered(conn, Settings(_env_file=None, employer_repeat_days=0), "triage") == 1
    assert _status(conn, "e") == ("skipped", "duplicate_employer:L")


def test_pending_lead_covers_too_but_low_score_does_not():
    s = Settings(_env_file=None)
    conn = _conn()
    _vac(conn, "P", "prefiltered", employer="Альфа", employer_id="1")           # fetched, awaiting evaluation
    _vac(conn, "E", "evaluated", employer="Бета", employer_id="2", total=75)   # above threshold, awaiting digest
    _vac(conn, "W", "evaluated", employer="Гамма", employer_id="3", total=40)  # below threshold: no lead
    _vac(conn, "R", "rejected", employer="Дельта", employer_id="4")
    for hh_id, emp, eid in (("a", "Альфа", "1"), ("b", "Бета", "2"), ("c", "Гамма", "3"), ("d", "Дельта", "4")):
        _vac(conn, hh_id, "to_fetch", employer=emp, employer_id=eid)
    assert dedup.skip_covered(conn, s, "to_fetch") == 2
    assert _status(conn, "a")[1] == "duplicate_employer:P" and _status(conn, "b")[1] == "duplicate_employer:E"
    assert _status(conn, "c") == ("to_fetch", None) and _status(conn, "d") == ("to_fetch", None)


def test_dedupe_evaluated_keeps_best_and_respects_sent():
    s = Settings(_env_file=None)
    conn = _conn()
    _vac(conn, "1", "evaluated", employer="Альфа", employer_id="1", total=70, area="Калуга")
    _vac(conn, "2", "evaluated", employer="Альфа", employer_id="1", total=88, area="Брянск")   # best of Альфа
    _vac(conn, "3", "evaluated", employer="альфа", employer_id=None, total=65, area="Тула")    # name-only twin
    _vac(conn, "4", "evaluated", employer="Бета", employer_id="2", total=80)
    _vac(conn, "S", "sent", employer="Бета", employer_id="2")                                 # Бета already has a lead
    _vac(conn, "5", "evaluated", employer="Гамма", employer_id="3", total=61)
    _vac(conn, "6", "evaluated", employer="Гамма", employer_id="3", total=50)                # below threshold: not a lead, untouched
    _vac(conn, "p1", "evaluated", employer="Сергей", employer_id=None, site="profi", total=90)
    _vac(conn, "p2", "evaluated", employer="Сергей", employer_id=None, site="profi", total=85)  # profi: two clients, both stay
    assert dedup.dedupe_evaluated(conn, s) == 3
    left = [r["hh_id"] for r in repo.evaluated_leads(conn, s.score_threshold)]
    assert left == ["p1", "2", "p2", "5"]
    assert _status(conn, "1") == ("skipped", "duplicate_employer:2") and _status(conn, "3") == ("skipped", "duplicate_employer:2")
    assert _status(conn, "4") == ("skipped", "duplicate_employer:S")
    assert _status(conn, "6") == ("evaluated", None)
    assert dedup.dedupe_evaluated(conn, s) == 0  # idempotent
