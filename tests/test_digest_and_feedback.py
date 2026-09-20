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
    assert [b.callback_data for b in reason_kb(5).inline_keyboard[2]] == ["fbr:5:text", "fbr:5:skip"]


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


def test_letter_cmd_on_a_queue_lead_records_it_as_sent(tmp_path, monkeypatch):
    """v9.11: the owner now holds the card and the letter — ✅ must close the company, /stats must see it,
    and the queue must not deliver the same lead again."""
    import asyncio

    from aiogram.filters import CommandObject

    from hh_scout.bot import handlers
    from hh_scout.llm.cover_letter import CoverLetterWriter

    conn = _db()
    said = []

    class _Msg:
        chat = type("C", (), {"id": 1})()
        bot = None
        _n = 100

        async def answer(self, text, **kw):
            said.append(text)
            _Msg._n += 1
            return type("M", (), {"message_id": _Msg._n})()

    monkeypatch.setattr(CoverLetterWriter, "answered_employer", lambda self, row: None)
    monkeypatch.setattr(CoverLetterWriter, "write_for", lambda self, row, system_text=None, hint=None: "готовое письмо")
    s = Settings(_env_file=None, prompts_dir=tmp_path, bridge_url="http://bridge.test", bridge_token="t")
    asyncio.run(handlers.letter_cmd(_Msg(), CommandObject(args="3"), s, conn))
    row = repo.lead_by_hh_id(conn, "3")
    assert row["status"] == "sent" and repo.is_lead_open(conn, 3)
    assert repo.lead_messages(conn, 3) == (102, 103)                     # card, then letter
    assert repo.last_digest(conn)["note"] == "manual:/letter"
    assert [r["hh_id"] for r in repo.lead_queue(conn, 60)] == ["1", "4"]  # gone from the queue
    assert repo.evaluations_since_last_digest(conn, daily_only=True) == 4  # a manual letter is not a noon digest


def test_iso_utc_compares_like_the_stored_timestamps():
    from datetime import datetime, timedelta

    from hh_scout.config import TZ

    when = datetime(2026, 9, 20, 12, 0, tzinfo=TZ)
    assert repo.iso_utc(when) == "2026-09-20T09:00:00+00:00"
    conn = _db()
    conn.execute("INSERT INTO digests(sent_at, items_count, collected_count) VALUES ('2026-09-20T10:30:00+00:00', 1, 1)")
    conn.execute("INSERT INTO digest_items(digest_id, vacancy_id, position) VALUES (1, 1, 1)")
    conn.execute("INSERT INTO lead_actions(vacancy_id, action, created_at) VALUES (1, 'responded', 'x')")
    conn.execute("UPDATE vacancies SET status = 'sent' WHERE id = 1")
    # 13:00 Moscow is 10:00 UTC — the row sent at 10:30 UTC is inside the window; the naive local string would miss it
    assert len(repo.outcome_rows(conn, repo.iso_utc(when + timedelta(hours=1)))) == 1
    assert len(repo.outcome_rows(conn, (when + timedelta(hours=1)).isoformat())) == 0


def test_a_dislike_in_the_owners_own_words_reaches_the_calibration_block():
    """v9.11: «✍️ своими словами» asks for a reply; the reply lands in feedback.reason and in the evaluator's block."""
    import asyncio

    from hh_scout.bot import feedback as fb_mod, handlers
    from hh_scout.llm.evaluator import feedback_block

    conn = _db()
    finalize_digest(conn, Settings(_env_file=None), [(repo.lead_by_hh_id(conn, "1"), 500, 501)], checked=0)
    said = []

    class _Bot:
        async def edit_message_text(self, text, chat_id, message_id):
            said.append(("edit", message_id, text))

        async def delete_message(self, chat_id, message_id):
            said.append(("delete", message_id))

    class _Prompt:
        message_id = 777

    class _Card:
        chat = type("C", (), {"id": 1})()

        async def answer(self, text, **kw):
            said.append(("ask", text))
            return _Prompt()

        async def edit_reply_markup(self, **kw):
            pass

    class _Cb:
        data = "fbr:1:text"
        bot = _Bot()
        message = _Card()

        async def answer(self, text=None):
            said.append(("cb", text))

    repo.add_feedback(conn, 1, -1)
    asyncio.run(fb_mod.reason(_Cb(), conn))
    assert fb_mod.awaiting_reason(conn) == (1, 777)
    assert any(kind == "ask" for kind, *_ in said)

    class _Reply:
        chat = type("C", (), {"id": 1})()
        bot = _Bot()
        text = "  они хотят инженера в штат на объект в Норильске, подряд им не нужен  "
        reply_to_message = type("R", (), {"message_id": 777})()

        async def answer(self, text, **kw):
            said.append(("done", text))

    asyncio.run(handlers.reason_reply(_Reply(), conn))
    reason = conn.execute("SELECT reason FROM feedback WHERE vacancy_id = 1").fetchone()[0]
    assert reason.startswith("они хотят инженера в штат")
    assert fb_mod.awaiting_reason(conn) is None
    assert any(kind == "edit" and "Норильске" in text for kind, _, text in [s for s in said if s[0] == "edit"])
    block = feedback_block(conn, mature_days=5)
    assert "причина: они хотят инженера в штат" in block

    # a stray reply to some other message is ignored
    class _Other(_Reply):
        reply_to_message = type("R", (), {"message_id": 1})()

    asyncio.run(handlers.reason_reply(_Other(), conn))
    assert conn.execute("SELECT reason FROM feedback WHERE vacancy_id = 1").fetchone()[0] == reason


def test_calibration_block_does_not_call_a_fresh_letter_silence():
    from hh_scout.llm.evaluator import feedback_block

    conn = _db()
    s = Settings(_env_file=None)
    finalize_digest(conn, s, [(repo.lead_by_hh_id(conn, "1"), 1, 2), (repo.lead_by_hh_id(conn, "3"), 3, 4)], checked=0)
    for vid in (1, 3):
        repo.add_action(conn, vid, "responded")
        repo.add_feedback(conn, vid, +1)
        conn.execute("UPDATE vacancies SET applied = 1 WHERE id = ?", (vid,))
    conn.execute("UPDATE digests SET sent_at = '2026-09-01T10:00:00+00:00'")      # both delivered long ago
    conn.execute("UPDATE vacancies SET negotiation_state = 'INTERVIEW', has_chat = 1 WHERE id = 3")
    block = feedback_block(conn, mature_days=5)
    assert "«Инженер 3»" in block and "пригласила" in block
    assert "«Инженер 1»" in block and "молчит" in block            # old enough: silence is a fact
    conn.execute("UPDATE digests SET sent_at = ?", (repo.utcnow(),))
    block = feedback_block(conn, mature_days=5)
    assert "молчит" not in block                                    # sent today: silence means nothing yet
