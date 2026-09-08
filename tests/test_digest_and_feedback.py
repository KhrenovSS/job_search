from hh_scout.bot.keyboards import parse_callback, reason_kb, vote_kb
from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.pipeline import repo
from hh_scout.pipeline.digest_builder import finalize_digest, mark_previewed_as_sent, plan_digest


def _db():
    conn = connect(":memory:")
    migrate(conn)
    for i, total in ((1, 90), (2, 40), (3, 70), (4, 65)):
        conn.execute("INSERT INTO vacancies(id,hh_id,title,employer,url,source,search_pass,status,first_seen_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (i, str(i), f"Инженер {i}", "ООО", "u", "s", "regional", "evaluated", "t", "t"))
        conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                     "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (i, 80, 0, 0, 80, 60, total, "maybe", 0, "staff", "integrator", "v", "p", "[]", "2026-09-08T10:00:00+00:00"))
    repo.save_cover_letter(conn, 1, "письмо 1")
    return conn


def test_plan_and_finalize_digest():
    s = Settings(_env_file=None, digest_max_items=2)
    conn = _db()
    plan = plan_digest(conn, s)
    assert [r["hh_id"] for r in plan.leads] == ["1", "3"]  # top-2 by total, above 60
    assert plan.leads[0]["letter"] == "письмо 1" and plan.leads[1]["letter"] is None
    assert plan.checked == 4
    finalize_digest(conn, s, [(plan.leads[0], 111), (plan.leads[1], 222)], plan.checked, note="test")
    st = repo.count_by_status(conn)
    assert st == {"sent": 2, "rejected": 1, "evaluated": 1}  # 65 stays evaluated for the next digest
    d = repo.last_digest(conn)
    assert d["items_count"] == 2 and d["collected_count"] == 4 and d["note"] == "test"
    items = conn.execute("SELECT vacancy_id, position, tg_message_id FROM digest_items ORDER BY position").fetchall()
    assert [tuple(i) for i in items] == [(1, 1, 111), (3, 2, 222)]
    assert repo.evaluations_since_last_digest(conn) == 0


def test_mark_previewed_as_sent():
    s = Settings(_env_file=None)
    conn = _db()
    assert mark_previewed_as_sent(conn, s, "preview") == 3
    assert repo.count_by_status(conn) == {"sent": 3, "rejected": 1}


def test_feedback_roundtrip_and_callbacks():
    conn = _db()
    repo.add_feedback(conn, 1, -1)
    repo.update_feedback_reason(conn, 1, "stack")
    repo.add_feedback(conn, 3, +1)
    rows = conn.execute("SELECT vacancy_id, value, reason FROM feedback ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, -1, "stack"), (3, 1, None)]
    assert parse_callback("fb:12:up") == ("fb", 12, "up")
    assert parse_callback("fbr:7:agency") == ("fbr", 7, "agency")
    assert parse_callback("garbage") is None and parse_callback("fb:x:up") is None
    kb = vote_kb(5)
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["fb:5:up", "fb:5:down"]
    assert reason_kb(5).inline_keyboard[2][0].callback_data == "fbr:5:skip"
