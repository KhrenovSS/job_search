"""v9.10: the measurement channel behind the feedback loop (decision #48).

The loop the owner asked for — "read which letters get answers and tune by it" — is only as honest as what
feeds it. These tests pin the three things that made it dishonest before: a truncated sample, a snapshot
instead of a history, and percentages computed off cells too small to mean anything.
"""

from datetime import datetime, timedelta, timezone

import pytest

from hh_scout.browser.hh_pages import negotiations_has_next, negotiations_url
from hh_scout.config import Settings
from hh_scout.db import MIGRATIONS, connect, migrate, utcnow
from hh_scout.pipeline import outcomes, repo


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


def _db():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _lead(conn, hh_id, total, *, letter_age=10.0, has_chat=0, state=None, applied=1,
          company_kind="integrator", work_format="office", letter_len=2000):
    conn.execute("INSERT INTO vacancies(hh_id,title,employer,employer_id,url,source,search_pass,status,site,"
                 "work_format,applied,has_chat,negotiation_state,first_seen_at,updated_at) "
                 "VALUES (?,?,?,?,'u','s','regional','sent','hh',?,?,?,?,?,?)",
                 (hh_id, f"Инженер {hh_id}", f"ООО {hh_id}", hh_id, work_format, applied, has_chat, state,
                  utcnow(), utcnow()))
    vid = conn.execute("SELECT id FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()[0]
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,"
                 "total,ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,"
                 "created_at) VALUES (?,80,0,0,80,60,?,'maybe',0,'staff',?,'v','p','[]',?)",
                 (vid, total, company_kind, utcnow()))
    conn.execute("INSERT INTO cover_letters(vacancy_id,text,created_at) VALUES (?,?,?)",
                 (vid, "п" * letter_len, _iso(letter_age)))
    did = conn.execute("INSERT INTO digests(sent_at,items_count,collected_count) VALUES (?,1,1)",
                       (_iso(letter_age),)).lastrowid
    conn.execute("INSERT INTO digest_items(digest_id,vacancy_id,position) VALUES (?,?,1)", (did, vid))
    conn.execute("INSERT INTO lead_actions(vacancy_id,action,created_at) VALUES (?,'responded',?)", (vid, utcnow()))
    return vid


# --- 1. the sample is no longer truncated ------------------------------------

def test_negotiations_url_is_paged_and_the_first_page_keeps_its_old_address():
    assert negotiations_url() == negotiations_url(0) == "https://hh.ru/applicant/negotiations?filter=all"
    assert negotiations_url(2) == "https://hh.ru/applicant/negotiations?filter=all&page=2"


def test_has_next_reads_the_same_paging_shape_as_search():
    state = {"applicantNegotiations": {"paging": {"next": {"disabled": False}}}}
    assert negotiations_has_next(state, 0) is True
    assert negotiations_has_next({"applicantNegotiations": {"paging": None}}, 0) is False
    assert negotiations_has_next({}, 0) is False          # no block at all -> do not page blindly


# --- 2. history, not a snapshot ----------------------------------------------

def test_an_event_is_appended_only_when_the_conversation_actually_changes():
    conn = _db()
    vid = _lead(conn, "1", 70)
    assert repo.record_negotiation(conn, vid, "RESPONSE", False) is True
    assert repo.record_negotiation(conn, vid, "RESPONSE", False) is False   # same sync, three times a day
    assert repo.record_negotiation(conn, vid, "RESPONSE", True) is True     # the company wrote
    assert repo.record_negotiation(conn, vid, "INTERVIEW", True) is True    # and then invited
    states = [r[0] for r in conn.execute("SELECT state FROM negotiation_events WHERE vacancy_id=? ORDER BY id", (vid,))]
    assert states == ["RESPONSE", "RESPONSE", "INTERVIEW"]


def test_mark_applied_records_the_history_on_the_way_through():
    conn = _db()
    _lead(conn, "2", 70, state=None)
    repo.mark_applied(conn, "2", has_chat=True, state="DISCARD")
    row = conn.execute("SELECT state, has_messages FROM negotiation_events").fetchone()
    assert row["state"] == "DISCARD" and row["has_messages"] == 1


def test_m012_seeds_the_history_from_the_snapshot_it_replaces():
    """The column `negotiation_seen_at` was written for days and read by nobody — the migration uses it."""
    conn = connect(":memory:")
    for step in MIGRATIONS[:11]:
        step(conn)
    conn.execute("PRAGMA user_version = 11")
    conn.execute("INSERT INTO vacancies(hh_id,title,url,source,search_pass,status,applied,has_chat,"
                 "negotiation_state,negotiation_seen_at,first_seen_at,updated_at) "
                 "VALUES ('9','t','u','s','regional','skipped',1,1,'DISCARD','2026-09-19T05:00:00+00:00','x','x')")
    migrate(conn)
    row = conn.execute("SELECT state, has_messages, seen_at FROM negotiation_events").fetchone()
    assert row["state"] == "DISCARD" and row["has_messages"] == 1
    assert row["seen_at"] == "2026-09-19T05:00:00+00:00"


# --- 3. the report refuses to invent findings --------------------------------

def test_a_letter_too_young_to_have_an_answer_is_not_counted_as_silence():
    conn = _db()
    _lead(conn, "3", 70, letter_age=0.5)          # sent this morning
    _lead(conn, "4", 70, letter_age=9, has_chat=1)
    rows = repo.outcome_rows(conn, "2000-01-01")
    assert outcomes.maturing(rows, 5) == 1
    by = outcomes.outcome_by(rows, lambda r: "все", mature_days=5)
    assert by[0]["n"] == 1 and by[0]["answered"] == 1      # the young one is out of the rate entirely
    # the band table applies the same maturity rule as the breakdowns (v9.11)
    bands = {b["band"]: b for b in outcomes.outcome_stats(rows, 5)}
    assert bands["70-74"]["written"] == 1 and bands["70-74"]["answered"] == 1


def test_a_cell_smaller_than_the_floor_shows_a_count_instead_of_a_percentage():
    conn = _db()
    for i in range(outcomes.MIN_CELL):
        _lead(conn, f"big{i}", 70, has_chat=i % 2, company_kind="integrator")
    _lead(conn, "small", 70, has_chat=1, company_kind="manufacturer")
    rows = repo.outcome_rows(conn, "2000-01-01")
    by = {b["name"]: b for b in outcomes.outcome_by(rows, lambda r: r["company_kind"], mature_days=5)}
    assert by["integrator"]["rate"] == 50                  # 8 observations: a number worth printing
    assert by["manufacturer"]["rate"] is None              # 1 observation: "мало данных", not "100 %"


def test_outcome_of_separates_silence_from_a_letter_sent_outside_hh():
    conn = _db()
    _lead(conn, "5", 70, applied=0)                        # by e-mail: hh cannot tell us anything
    _lead(conn, "6", 70, applied=1, has_chat=0)            # sent on hh, nobody answered
    _lead(conn, "7", 70, applied=1, state="INTERVIEW")
    got = {r["vacancy_id"]: outcomes.outcome_of(r) for r in repo.outcome_rows(conn, "2000-01-01")}
    assert sorted(got.values()) == ["blind", "invited", "silent"]


def test_the_reply_curve_is_what_the_maturity_threshold_rests_on():
    conn = _db()
    _lead(conn, "8", 70, letter_age=0.5, has_chat=0)
    _lead(conn, "9", 70, letter_age=9, has_chat=1)
    curve = dict((name, (n, a)) for name, n, a in outcomes.reply_delay_curve(repo.outcome_rows(conn, "2000-01-01")))
    assert curve["0-1 дн."] == (1, 0) and curve["8+ дн."] == (1, 1)


def test_a_rejection_is_not_counted_as_an_answer():
    """hh marks a refusal with has_chat=1 too. While we knew of 8 refusals that overlap was harmless;
    the full sync found 22 of them among 36 replies, and «ответов 36» would have been a lie (v9.10)."""
    conn = _db()
    _lead(conn, "20", 78, has_chat=1, state="DISCARD")
    _lead(conn, "21", 78, has_chat=1, state=None)
    _lead(conn, "22", 78, has_chat=0, state=None)
    _lead(conn, "23", 78, applied=0)
    band = {b["band"]: b for b in outcomes.outcome_stats(repo.outcome_rows(conn, "2000-01-01"), 5)}["75+"]
    assert band["refused"] == 1 and band["answered"] == 1 and band["silent"] == 1 and band["blind"] == 1
    # the columns are disjoint, so they add up to what was written
    assert band["blind"] + band["silent"] + band["answered"] + band["invited"] + band["refused"] == band["written"]


def test_the_reaction_curve_counts_a_refusal_because_it_measures_timing_not_quality():
    conn = _db()
    _lead(conn, "24", 70, letter_age=9, has_chat=1, state="DISCARD")
    curve = dict((name, (n, a)) for name, n, a in outcomes.reply_delay_curve(repo.outcome_rows(conn, "2000-01-01")))
    assert curve["8+ дн."] == (1, 1)


def test_score_bands_still_work_the_way_the_digest_header_reads_them():
    """`/stats` and the digest header share one slice; the rewrite must not change it."""
    conn = _db()
    _lead(conn, "10", 78, has_chat=1, state="INTERVIEW")
    _lead(conn, "11", 62, has_chat=0)
    bands = {b["band"]: b for b in outcomes.outcome_stats(repo.outcome_rows(conn, "2000-01-01"), 5)}
    assert bands["75+"]["invited"] == 1 and bands["60-64"]["written"] == 1
    assert outcomes.invited_count(repo.outcome_rows(conn, "2000-01-01")) == 1


def test_settings_carry_the_two_new_knobs():
    s = Settings(_env_file=None)
    assert s.negotiations_pages == 3 and s.outcome_mature_days == 5


# --- 4. the sitting walks more than one page, but never blindly ---------------

class _PagedSession:
    """Serves N pages of responses; `repeat` makes hh ignore `page=` and re-serve page 1."""

    def __init__(self, pages, repeat=False, budget=10):
        self.pages, self.repeat, self.page_budget, self.page_loads, self.urls = pages, repeat, budget, 0, []

    def open(self, url):
        from hh_scout.browser.session import PageBudgetExceeded
        if self.page_loads >= self.page_budget:
            raise PageBudgetExceeded("budget")
        self.page_loads += 1
        self.urls.append(url)
        page = int(url.split("&page=")[1]) if "&page=" in url else 0
        served = 0 if self.repeat else page
        if served >= self.pages:
            return {"userType": "applicant", "applicantNegotiations": {"topicList": [], "paging": None}}
        return {"userType": "applicant", "applicantNegotiations": {
            "topicList": [{"vacancyId": 1000 + served * 10 + i, "lastState": "RESPONSE",
                           "conversationMessagesCount": 1} for i in range(2)],
            "paging": {"next": {"disabled": served + 1 >= self.pages}}}}


def _sync(pages=3):
    from hh_scout.pipeline.negotiations import NegotiationsSync
    return NegotiationsSync(pages=pages)


def _walk(conn, session, pages=3):
    """The loop a sitting runs page by page (`Collector.run` → `negotiations.sync_page`)."""
    from hh_scout.pipeline.negotiations import sync_page
    sync = _sync(pages)
    while not sync.done:
        sync_page(conn, session, sync)
    return sync


def test_the_sitting_reads_three_pages_instead_of_the_newest_twenty():
    conn = _db()
    session = _PagedSession(pages=5)
    _walk(conn, session, pages=3)
    assert session.urls == [negotiations_url(0), negotiations_url(1), negotiations_url(2)]
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE applied=1").fetchone()[0] == 6


def test_paging_stops_at_the_end_of_the_list_without_wasting_a_load():
    conn = _db()
    session = _PagedSession(pages=2)
    _walk(conn, session, pages=5)
    assert len(session.urls) == 2          # `has_next` said so — no empty third load


def test_a_repeated_page_stops_the_walk_after_one_wasted_load():
    """If hh ignores `page=`, the cost of finding out must be one load, not two every sitting forever."""
    conn = _db()
    session = _PagedSession(pages=5, repeat=True)
    _walk(conn, session, pages=3)
    assert len(session.urls) == 2
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE applied=1").fetchone()[0] == 2


def test_the_budget_running_out_on_page_two_keeps_page_one(caplog):
    from hh_scout.browser.session import PageBudgetExceeded
    conn = _db()
    from hh_scout.pipeline.negotiations import sync_page
    session = _PagedSession(pages=5, budget=1)
    sync = _sync(3)
    sync_page(conn, session, sync)
    with pytest.raises(PageBudgetExceeded):
        sync_page(conn, session, sync)
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE applied=1").fetchone()[0] == 2
    assert sync.page == 2                  # the cursor moved past the failed page, so it is not re-read blind
