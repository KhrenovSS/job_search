"""The lead queue (v9.1): rating across all companies, a daily quota, waiting and expiry.

Leads that do not fit today's quota are NOT written off — they keep their place and compete with whatever
arrives tomorrow. Strong always goes first; a day of waiting is worth one point, capped, so the tail cannot
starve forever.
"""

from datetime import datetime, timedelta, timezone

from hh_scout.config import Settings
from hh_scout.db import connect, migrate, utcnow
from hh_scout.pipeline import repo


def _conn():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _lead(conn, hh_id, total, *, days_ago=0, employer=None, letter=False):
    conn.execute("INSERT INTO vacancies(hh_id, title, employer, employer_id, url, source, search_pass, status, "
                 "published_at, first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (hh_id, f"Инженер {hh_id}", employer or f"ООО {hh_id}", hh_id, "u", "s", "remote", "evaluated",
                  "2026-09-01", "t", utcnow()))
    vid = conn.execute("SELECT id FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()[0]
    queued = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,"
                 "total,ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,"
                 "created_at) VALUES (?,80,0,0,80,60,?,'maybe',0,'staff','integrator','v','p','[]',?)",
                 (vid, total, queued))
    if letter:
        repo.save_cover_letter(conn, vid, "письмо")
    return vid


def _order(conn, **kw):
    return [r["hh_id"] for r in repo.lead_queue(conn, 60, **kw)]


def test_strong_goes_first_but_waiting_is_worth_a_point_a_day():
    conn = _conn()
    _lead(conn, "fresh75", 75)
    _lead(conn, "old68", 68, days_ago=1)
    assert _order(conn)[0] == "fresh75"          # a day of waiting does not beat 7 points of quality

    _lead(conn, "old68b", 68, days_ago=7)
    _lead(conn, "fresh72", 72)
    order = _order(conn)
    assert order.index("old68b") < order.index("fresh72")   # a week of waiting does: 68+7 > 72


def test_the_wait_bonus_is_capped():
    """Otherwise an ancient weak vacancy would eventually outrank everything."""
    conn = _conn()
    _lead(conn, "ancient61", 61, days_ago=90)
    _lead(conn, "fresh70", 70)
    assert _order(conn)[0] == "fresh70"           # 61 + 7 < 70


def test_leads_below_the_threshold_never_enter_the_queue():
    conn = _conn()
    _lead(conn, "good", 60)
    _lead(conn, "weak", 59, days_ago=30)
    assert _order(conn) == ["good"]


def test_quota_is_daily_not_per_run():
    """The crawl runs three times a day; without a daily count it would write three times the quota."""
    conn = _conn()
    for i in range(4):
        _lead(conn, f"v{i}", 70 + i, letter=i < 2)
    assert repo.letters_written_today(conn) == 2
    conn.execute("UPDATE cover_letters SET created_at = '2020-01-01T00:00:00+00:00'")
    assert repo.letters_written_today(conn) == 0     # yesterday's letters do not count against today


def test_the_tail_keeps_waiting_and_is_offset_by_the_quota():
    conn = _conn()
    for i in range(8):
        _lead(conn, f"v{i}", 60 + i)
    top = repo.lead_queue(conn, 60, 3)
    tail = repo.lead_queue(conn, 60, 10, offset=3)
    assert [r["hh_id"] for r in top] == ["v7", "v6", "v5"]
    assert [r["hh_id"] for r in tail] == ["v4", "v3", "v2", "v1", "v0"]
    assert repo.queue_size(conn, 60) == 8


def test_expire_queue_drops_only_the_long_forgotten():
    conn = _conn()
    _lead(conn, "old", 70, days_ago=31)
    _lead(conn, "borderline", 70, days_ago=29)
    assert repo.expire_queue(conn, 30) == 1
    assert _order(conn) == ["borderline"]
    row = conn.execute("SELECT status, skip_reason FROM vacancies WHERE hh_id = 'old'").fetchone()
    assert (row["status"], row["skip_reason"]) == ("rejected", "queue_expired")
    assert repo.expire_queue(conn, 0) == 0          # 0 = never expire


def test_the_queue_reports_how_long_each_lead_has_waited():
    """The owner must see that a lead is old — the bot does not re-open vacancy pages to check they are still live."""
    conn = _conn()
    _lead(conn, "old", 70, days_ago=9)
    _lead(conn, "fresh", 71)
    by_id = {r["hh_id"]: r["waiting_days"] for r in repo.lead_queue(conn, 60)}
    assert by_id["old"] == 9 and by_id["fresh"] == 0
