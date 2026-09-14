"""Sending already evaluated vacancies back for a fresh evaluation (llm/evaluator.requeue).

A prompt change is worth nothing if it only applies to vacancies collected after it, so the manual CLI puts old
rows back to `prefiltered`. The page is never re-opened — the description is already in `raw_json`.
"""

from hh_scout.config import Settings
from hh_scout.db import connect, migrate, utcnow
from hh_scout.llm.evaluator import requeue

THRESHOLD = Settings(_env_file=None).score_threshold


def _conn():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _vac(conn, hh_id, status, total, *, raw='{"description": "d"}'):
    conn.execute("INSERT INTO vacancies(hh_id, title, employer, url, source, search_pass, status, raw_json, "
                 "first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (hh_id, "Инженер", "ООО", "u", "s", "remote", status, raw, "t", utcnow()))
    vid = conn.execute("SELECT id FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()[0]
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,"
                 "total,ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,"
                 "created_at) VALUES (?,50,0,0,50,50,?,'maybe',0,'staff','integrator','v','p','[]',?)",
                 (vid, total, utcnow()))
    return vid


def _state(conn, hh_id):
    r = conn.execute("SELECT v.status, e.id FROM vacancies v LEFT JOIN evaluations e ON e.vacancy_id = v.id "
                     "WHERE v.hh_id = ?", (hh_id,)).fetchone()
    return r["status"], r["id"] is not None


def test_requeue_takes_borderline_non_leads_only():
    conn = _conn()
    _vac(conn, "near", "rejected", 57)
    _vac(conn, "far", "rejected", 20)                              # hopeless: a prompt tweak will not save it
    _vac(conn, "sent", "sent", 80)                                 # already a lead, never touched
    _vac(conn, "noraw", "rejected", 58, raw="")                    # no description cached: would need the browser

    assert requeue(conn, min_total=45, threshold=THRESHOLD) == 1
    assert _state(conn, "near") == ("prefiltered", False)           # old evaluation dropped
    assert _state(conn, "far") == ("rejected", True)
    assert _state(conn, "sent") == ("sent", True)
    assert _state(conn, "noraw") == ("rejected", True)


def test_requeue_takes_evaluated_below_the_threshold_too():
    """Whether a non-lead sits in `rejected` or `evaluated` only says if a digest has run since — not its fate."""
    conn = _conn()
    _vac(conn, "waiting", "evaluated", THRESHOLD - 1)              # written off at the next digest, a non-lead already
    _vac(conn, "lead", "evaluated", THRESHOLD)                     # exactly at the threshold: a lead, hands off
    _vac(conn, "written_off", "rejected", THRESHOLD - 1)           # the same vacancy one digest later

    assert requeue(conn, min_total=45, threshold=THRESHOLD) == 2
    assert _state(conn, "waiting") == ("prefiltered", False)
    assert _state(conn, "written_off") == ("prefiltered", False)
    assert _state(conn, "lead") == ("evaluated", True)


def test_requeue_by_id_ignores_status_and_score():
    conn = _conn()
    _vac(conn, "a", "rejected", 10)
    _vac(conn, "b", "evaluated", 62)
    _vac(conn, "c", "rejected", 58)

    assert requeue(conn, hh_ids=["a", "b"]) == 2
    assert _state(conn, "a") == ("prefiltered", False)
    assert _state(conn, "b") == ("prefiltered", False)
    assert _state(conn, "c") == ("rejected", True)                 # not asked for


def test_requeue_limit_takes_the_closest_to_the_threshold():
    conn = _conn()
    for hh_id, total in (("a", 46), ("b", 59), ("c", 52)):
        _vac(conn, hh_id, "rejected", total)
    assert requeue(conn, min_total=45, threshold=THRESHOLD, limit=2) == 2
    assert _state(conn, "b")[0] == "prefiltered" and _state(conn, "c")[0] == "prefiltered"
    assert _state(conn, "a")[0] == "rejected"
