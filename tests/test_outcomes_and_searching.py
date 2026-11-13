"""What the companies answered (decision #42) and how long they have been searching (decision #44)."""

from hh_scout.db import connect, migrate, utcnow
from hh_scout.pipeline import outcomes, repo


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
    bands = {b["band"]: b for b in outcomes.outcome_stats(repo.outcome_rows(conn, "2000-01-01"), 0)}
    assert bands["75+"]["written"] == 2
    assert bands["75+"]["invited"] == 1 and bands["75+"]["refused"] == 1
    assert bands["60-64"]["written"] == 1 and bands["60-64"]["answered"] == 0
    assert outcomes.invited_count(repo.outcome_rows(conn, "2000-01-01")) == 1


def test_letters_sent_outside_hh_are_reported_as_blind_not_as_failures():
    conn = _db()
    _lead(conn, "5", 77, applied=0)  # owner wrote by e-mail: hh cannot tell us what came back
    band = {b["band"]: b for b in outcomes.outcome_stats(repo.outcome_rows(conn, "2000-01-01"), 0)}["75+"]
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


def _mail_lead(conn, hh_id, total, *, sent_at, site="trudvsem"):
    vid = _lead(conn, hh_id, total, applied=0)
    conn.execute("UPDATE vacancies SET site = ? WHERE id = ?", (site, vid))
    conn.execute("UPDATE digests SET sent_at = ? WHERE id = (SELECT digest_id FROM digest_items WHERE vacancy_id = ?)",
                 (sent_at, vid))
    return vid


def test_an_email_letter_is_measured_once_the_owner_can_report_its_answer():
    """v9.47: a letter sent by e-mail after the outcome buttons appeared is silence until the owner says otherwise;
    an older one stays blind — unless he reported its answer afterwards."""
    conn = _db()
    old = _mail_lead(conn, "t1", 77, sent_at="2026-10-20T10:00:00+00:00")
    old_answered = _mail_lead(conn, "t2", 78, sent_at="2026-10-21T10:00:00+00:00", site="owen")
    new = _mail_lead(conn, "t3", 79, sent_at=utcnow(), site="web")
    repo.record_mail_outcome(conn, old_answered, "invited")
    got = {r["vacancy_id"]: outcomes.outcome_of(r) for r in repo.outcome_rows(conn, "2000-01-01")}
    assert got == {old: "blind", old_answered: "invited", new: "silent"}
    repo.record_mail_outcome(conn, new, "answered")
    repo.record_mail_outcome(conn, new, "answered")          # the same button twice is one event
    got = {r["vacancy_id"]: outcomes.outcome_of(r) for r in repo.outcome_rows(conn, "2000-01-01")}
    assert got[new] == "answered"
    assert conn.execute("SELECT COUNT(*) FROM negotiation_events WHERE vacancy_id = ?", (new,)).fetchone()[0] == 1
    band = {b["band"]: b for b in outcomes.outcome_stats(repo.outcome_rows(conn, "2000-01-01"), 0)}["75+"]
    assert band["written"] == 3 and band["blind"] == 1 and band["invited"] == 1 and band["answered"] == 1


def test_replied_finds_an_email_lead_by_a_part_of_the_company_name():
    conn = _db()
    vid = _mail_lead(conn, "t4", 70, sent_at="2026-10-20T10:00:00+00:00")
    conn.execute("UPDATE vacancies SET employer = 'ООО «Энергопром»' WHERE id = ?", (vid,))
    _lead(conn, "9", 70, employer="Энергопром-Сервис")       # an hh.ru response: its answer comes from the sync
    assert [r["id"] for r in repo.mail_leads_like(conn, "энергопром")] == [vid]
    assert repo.mail_leads_like(conn, "нет такой") == []


def test_outcome_buttons_and_the_collapsed_line():
    from hh_scout.bot.keyboards import outcome_kb, parse_callback
    from hh_scout.pipeline.ranker import format_collapsed
    kb = outcome_kb(7, "invited").inline_keyboard[0]
    assert [b.callback_data for b in kb] == ["out:7:answered", "out:7:invited", "out:7:refused"]
    assert [b.text.endswith("✓") for b in kb] == [False, True, False]
    assert parse_callback("out:7:invited") == ("out", 7, "invited")
    row = {"employer": "ООО Щит", "url": "u", "title": "Сборщик"}
    assert "🤝 разговор" in format_collapsed("responded", row, outcome="invited")
