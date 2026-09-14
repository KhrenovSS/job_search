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
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (i, str(i), f"Инженер {i}", f"ООО {i}", "u", "s", "regional", "evaluated", "t", "t"))
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


def test_the_tail_survives_the_digest_and_is_listed():
    """v9.1: what did not fit the quota is a queue, not a reject — it competes again tomorrow."""
    from hh_scout.pipeline.ranker import format_queue_tail

    s = Settings(_env_file=None, digest_max_items=1, digest_tail_items=5)
    conn = _db()
    plan = plan_digest(conn, s)
    assert len(plan.leads) == 1 and plan.waiting_total == 2      # quota 1, two more keep waiting
    assert [r["hh_id"] for r in plan.waiting] == ["3", "4"]

    finalize_digest(conn, s, [(plan.leads[0], 111)], plan.checked)
    assert repo.count_by_status(conn) == {"sent": 1, "rejected": 1, "evaluated": 2}  # the tail stays evaluated

    tail = format_queue_tail(plan.waiting, plan.waiting_total)
    assert "Ждут очереди: 2" in tail and "/letter 3" in tail


def test_plan_digest_keeps_one_lead_per_company():
    """Same employer in three regions → only the best-scored vacancy goes out, the twins are skipped as duplicates."""
    s = Settings(_env_file=None)
    conn = _db()
    conn.execute("UPDATE vacancies SET employer = 'РУССКИЙ ПРОДУКТ', employer_id = '777', area_name = 'Калуга' WHERE id = 3")
    conn.execute("UPDATE vacancies SET employer = 'Русский продукт', employer_id = NULL, area_name = 'Воронеж' WHERE id = 4")  # old card: name only
    conn.execute("UPDATE vacancies SET employer = 'Другая', employer_id = '777', area_name = 'Липецк' WHERE id = 1")  # renamed brand, same id
    plan = plan_digest(conn, s)
    assert [r["hh_id"] for r in plan.leads] == ["1"]  # 90 beats 70 and 65 of the same company
    skipped = {r["hh_id"]: r["skip_reason"] for r in conn.execute("SELECT hh_id, skip_reason FROM vacancies WHERE status = 'skipped'")}
    assert skipped == {"3": "duplicate_employer:1", "4": "duplicate_employer:1"}
    assert plan_digest(conn, s).leads[0]["hh_id"] == "1"  # idempotent
    finalize_digest(conn, s, [(plan.leads[0], 1)], plan.checked)
    assert repo.count_by_status(conn) == {"sent": 1, "skipped": 2, "rejected": 1}


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


def test_letter_cmd_refuses_for_a_company_the_owner_already_answered(tmp_path):
    """Иначе владелец увидел бы «ИИ вернул текст неподходящей длины» — неправда и потраченные деньги."""
    import asyncio

    from aiogram.filters import CommandObject

    from hh_scout.bot.handlers import letter_cmd

    conn = _db()
    conn.execute("UPDATE vacancies SET employer = 'ООО 1', updated_at = '2026-09-14T09:00:00+00:00' WHERE id IN (1, 2)")
    conn.execute("INSERT INTO lead_actions(vacancy_id, action, created_at) VALUES (1, 'responded', ?)",
                 ("2026-09-14T09:23:15+00:00",))
    said: list[str] = []

    class _Msg:
        async def answer(self, text, **kw):
            said.append(text)

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("П", encoding="utf-8")
    (prompts / "resume.md").write_text("Р", encoding="utf-8")
    (prompts / "cover_letter.md").write_text("h\n---\nSYS", encoding="utf-8")
    s = Settings(_env_file=None, prompts_dir=prompts, bridge_url="http://bridge.test", bridge_token="t")
    asyncio.run(letter_cmd(_Msg(), CommandObject(args="2"), s, conn))
    assert len(said) == 1 and said[0].startswith("Вы уже откликались")
    assert "2026-09-14" in said[0] and "Инженер 1" in said[0]
