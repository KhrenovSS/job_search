import sqlite3

from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.pipeline import repo
from hh_scout.pipeline.digest_builder import finalize_digest, plan_digest
from hh_scout.pipeline.ranker import digest_header, format_collapsed, format_inbox


def _db():
    conn = connect(":memory:")
    migrate(conn)
    for i, total in ((1, 90), (2, 80), (3, 70)):
        conn.execute("INSERT INTO vacancies(id,hh_id,title,employer,url,source,search_pass,status,first_seen_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (i, str(i), f"Инженер {i}", f"ООО {i}", f"https://hh.ru/vacancy/{i}", "s", "regional", "evaluated", "t", "t"))
        conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                     "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (i, 80, 0, 0, 80, 60, total, "maybe", 0, "staff", "integrator", "v", "p", "[]", "2026-09-08T10:00:00+00:00"))
    s = Settings(_env_file=None)
    plan = plan_digest(conn, s)
    finalize_digest(conn, s, [(r, 100 + r["id"], 200 + r["id"]) for r in plan.leads], plan.checked)
    return conn


def test_open_leads_and_actions():
    conn = _db()
    assert [r["hh_id"] for r in repo.open_leads(conn)] == ["1", "2", "3"]
    assert repo.lead_messages(conn, 1) == (101, 201)
    repo.add_action(conn, 1, "responded")
    repo.add_action(conn, 3, "deferred")
    repo.add_action(conn, 2, "liked")
    rows = repo.open_leads(conn)
    assert [r["hh_id"] for r in rows] == ["2", "3"]  # responded closed; deferred goes last
    assert rows[1]["deferred"] == 1 and rows[0]["deferred"] == 0
    assert repo.is_lead_open(conn, 1) is False and repo.is_lead_open(conn, 2) is True
    repo.add_action(conn, 2, "disliked", "stack")
    assert [r["hh_id"] for r in repo.open_leads(conn)] == ["3"]


def test_pending_auto_closes_and_stale():
    conn = _db()
    conn.execute("UPDATE vacancies SET applied = 1 WHERE id = 2")
    assert [r["hh_id"] for r in repo.pending_auto_closes(conn)] == ["2"]
    repo.add_action(conn, 2, "auto_responded")
    assert repo.pending_auto_closes(conn) == []
    assert [r["hh_id"] for r in repo.open_leads_older_than(conn, "2999-01-01T00:00:00+00:00")] == ["1", "3"]
    assert repo.open_leads_older_than(conn, "2000-01-01T00:00:00+00:00") == []


def test_formatting():
    conn = _db()
    row = repo.vacancy_by_id(conn, 1)
    line = format_collapsed("responded", row, reason=None)
    assert line.startswith("✅ Написал ") and "ООО 1" in line and 'href="https://hh.ru/vacancy/1"' in line
    assert format_collapsed("disliked", row, reason="stack").startswith("👎 Мимо ") and "не мой стек" in format_collapsed("disliked", row, reason="stack")
    inbox = format_inbox(repo.open_leads(conn))
    assert "Открытые лиды" in inbox and "Итого: 3" in inbox
    assert format_inbox([]).startswith("Все лиды обработаны")
    assert "Необработанных с прошлых дней: 3 (/inbox)" in digest_header(2, 10, open_before=3)
    assert "Необработанных" not in digest_header(2, 10, open_before=0)
