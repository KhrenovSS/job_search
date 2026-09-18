"""What the companies answered (decision #42) and how long they have been searching (decision #44)."""

from hh_scout.db import connect, migrate, utcnow
from hh_scout.pipeline import repo


def _db():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _lead(conn, hh_id, total, *, applied=1, has_chat=0, state=None, written=True, employer="ООО Завод"):
    conn.execute(
        "INSERT INTO vacancies(hh_id, title, employer, employer_id, url, source, search_pass, status, site, "
        "applied, has_chat, negotiation_state, first_seen_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,'sent','hh',?,?,?,?,?)",
        (hh_id, "Инженер-программист АСУ ТП", employer, "77", f"u/{hh_id}", "s", "regional",
         applied, has_chat, state, utcnow(), utcnow()))
    vid = conn.execute("SELECT id FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()["id"]
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                 "VALUES (?,?,0,0,?,?,?,'maybe',0,'staff','integrator','v','p','[]',?)",
                 (vid, total, total, total, total, utcnow()))
    conn.execute("INSERT INTO digests(sent_at, items_count, collected_count, note) VALUES (?, 1, 1, 't')", (utcnow(),))
    did = conn.execute("SELECT MAX(id) AS i FROM digests").fetchone()["i"]
    conn.execute("INSERT INTO digest_items(digest_id, vacancy_id, position) VALUES (?,?,1)", (did, vid))
    if written:
        repo.add_action(conn, vid, "responded")
    return vid


def test_outcome_stats_counts_only_leads_the_owner_wrote_to():
    conn = _db()
    _lead(conn, "1", 78, has_chat=1, state="INTERVIEW")
    _lead(conn, "2", 76, has_chat=1, state="DISCARD")
    _lead(conn, "3", 62, has_chat=0)
    _lead(conn, "4", 80, written=False)          # never written to -> says nothing about the band
    bands = {b["band"]: b for b in repo.outcome_stats(conn, "2000-01-01")}
    assert bands["75+"]["written"] == 2
    assert bands["75+"]["invited"] == 1 and bands["75+"]["refused"] == 1
    assert bands["60-64"]["written"] == 1 and bands["60-64"]["answered"] == 0
    assert repo.invited_since(conn, "2000-01-01") == 1


def test_letters_sent_outside_hh_are_reported_as_blind_not_as_failures():
    conn = _db()
    _lead(conn, "5", 77, applied=0)  # owner wrote by e-mail: hh cannot tell us what came back
    band = {b["band"]: b for b in repo.outcome_stats(conn, "2000-01-01")}["75+"]
    assert band["written"] == 1 and band["blind"] == 1 and band["answered"] == 0


def test_searching_days_measures_our_own_history_not_the_posting_date():
    conn = _db()
    old = "2026-09-01T10:00:00+00:00"
    conn.execute("INSERT INTO vacancies(hh_id, title, employer, employer_id, url, source, search_pass, status, site,"
                 " first_seen_at, updated_at) VALUES ('900','Инженер АСУ ТП','ООО Завод','77','u','s','regional',"
                 "'skipped','hh',?,?)", (old, old))
    conn.execute("INSERT INTO vacancies(hh_id, title, employer, employer_id, url, source, search_pass, status, site,"
                 " first_seen_at, updated_at) VALUES ('901','Инженер АСУ ТП','ООО Завод','77','u','s','regional',"
                 "'new','hh',?,?)", (utcnow(), utcnow()))
    row = conn.execute("SELECT * FROM vacancies WHERE hh_id = '901'").fetchone()
    assert repo.employer_searching_days(conn, row) >= 10

    # a different role at the same employer is a different search
    conn.execute("INSERT INTO vacancies(hh_id, title, employer, employer_id, url, source, search_pass, status, site,"
                 " first_seen_at, updated_at) VALUES ('902','Слесарь КИПиА','ООО Завод','77','u','s','regional',"
                 "'new','hh',?,?)", (utcnow(), utcnow()))
    other = conn.execute("SELECT * FROM vacancies WHERE hh_id = '902'").fetchone()
    assert repo.employer_searching_days(conn, other) == 0
