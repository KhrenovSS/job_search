import json

import httpx
import respx

from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.llm.bridge_client import BridgeClient
from hh_scout.llm.cover_letter import CoverLetterWriter, letter_payload, rules_hash, usable_letter
from hh_scout.pipeline import repo


def _settings(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("ПРОФИЛЬ", encoding="utf-8")
    (prompts / "resume.md").write_text("РЕЗЮМЕ: CODESYS, MasterSCADA", encoding="utf-8")
    (prompts / "cover_letter.md").write_text("h\n---\nSYS {resume} | {candidate_profile}", encoding="utf-8")
    return Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts)


def _db():
    conn = connect(":memory:")
    migrate(conn)
    for i, total in ((1, 80), (2, 40), (3, 70)):
        conn.execute("INSERT INTO vacancies(id,hh_id,title,employer,url,source,search_pass,status,raw_json,first_seen_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (i, str(i), f"Инженер {i}", "ООО", "u", "s", "regional", "evaluated",
                      '{"description": "<p>Нужен ПЛК</p>", "keySkills": {"keySkill": ["CODESYS"]}}', "t", "t"))
        conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                     "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (i, 80, 0, 0, 80, 60, total, "maybe", 0, "staff", "integrator", "нужен программист", "предложить", "[]", "t"))
    return conn


@respx.mock
def test_letters_written_only_for_leads_and_not_twice(tmp_path):
    s = _settings(tmp_path)
    conn = _db()
    good = "Здравствуйте.\n" + "Опыт CODESYS и MasterSCADA. " * 30 + "\nИван Иванов, +7 900"
    route = respx.post("http://bridge.test/complete").mock(
        return_value=httpx.Response(200, json={"text": "```text\n" + good + "\n```", "usage": {}, "cost_usd": 0.01}))
    w = CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None))
    stats = w.run()
    assert stats.written == 2 and stats.failed == 0 and route.call_count == 2  # totals 80 and 70, not 40
    assert repo.get_cover_letter(conn, 1).startswith("Здравствуйте.")  # fence stripped
    assert repo.get_cover_letter(conn, 2) is None
    sent = route.calls[0].request.content.decode()
    assert "SYS РЕЗЮМЕ: CODESYS, MasterSCADA | ПРОФИЛЬ" in sent and "Нужен ПЛК" in sent
    # second run: nothing to do
    assert CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None)).run().written == 0


def test_the_rules_stamp_follows_the_prompt_the_profile_and_the_resume(tmp_path):
    """v9.9: what shapes the letter is what the stamp must notice changing (decision #46)."""
    s = _settings(tmp_path)
    first = rules_hash(s)
    assert first and rules_hash(s) == first                       # same files, same stamp
    (s.prompts_dir / "cover_letter.md").write_text("h\n---\nSYS {resume} | {candidate_profile} | без должностей",
                                                   encoding="utf-8")
    assert rules_hash(s) != first
    (s.prompts_dir / "resume.md").write_text("РЕЗЮМЕ: CODESYS, MasterSCADA 4D", encoding="utf-8")
    assert len({first, rules_hash(s)}) == 2
    assert rules_hash(Settings(_env_file=None, prompts_dir=tmp_path / "нет")) == ""   # no prompts, no stamp


def test_usable_letter_is_the_stored_one_only_under_the_current_rules():
    conn = _db()
    repo.save_cover_letter(conn, 1, "письмо", rules_hash="rules-now")
    repo.save_cover_letter(conn, 3, "старое письмо", rules_hash="rules-of-last-week")
    rows = {r["hh_id"]: r for r in repo.lead_queue(conn, 60)}
    assert usable_letter(rows["1"], "rules-now") == "письмо"
    assert usable_letter(rows["3"], "rules-now") is None          # written under older rules
    conn.execute("UPDATE cover_letters SET rules_hash = NULL WHERE vacancy_id = 1")
    rows = {r["hh_id"]: r for r in repo.lead_queue(conn, 60)}
    assert usable_letter(rows["1"], "rules-now") is None          # rules unknown = stale
    assert usable_letter(rows["1"], "") is None                   # and "" never matches anything either


@respx.mock
def test_a_letter_written_under_older_rules_is_rewritten_after_the_leads_that_have_none(tmp_path):
    """The queue outlives a prompt change, so the stored text has to be refreshed before it is sent."""
    s = _settings(tmp_path)
    conn = _db()
    repo.save_cover_letter(conn, 1, "письмо по прежним правилам", rules_hash="rules-of-last-week")
    good = "Здравствуйте.\n" + "Опыт CODESYS и MasterSCADA. " * 30 + "\nИван Иванов, +7 900"
    route = respx.post("http://bridge.test/complete").mock(
        return_value=httpx.Response(200, json={"text": good, "usage": {}, "cost_usd": 0.01}))
    w = CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None))
    assert w.run().written == 2                        # the lead without a letter AND the stale one
    assert repo.get_cover_letter(conn, 1).startswith("Здравствуйте.")
    assert conn.execute("SELECT rules_hash FROM cover_letters WHERE vacancy_id = 1").fetchone()[0] == rules_hash(s)
    # the lead that had no letter at all went first — rewriting never starves today's finds of the quota
    assert "Инженер 3" in route.calls[0].request.content.decode()
    assert CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None)).run().written == 0


@respx.mock
def test_too_short_letter_rejected(tmp_path):
    s = _settings(tmp_path)
    conn = _db()
    respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(200, json={"text": "Коротко.", "usage": {}}))
    stats = CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None)).run()
    assert stats.written == 0 and stats.failed == 2
    assert conn.execute("SELECT COUNT(*) FROM cover_letters").fetchone()[0] == 0


def _payload_row(conn, vacancy_id):
    return conn.execute(
        """SELECT v.*, e.verdict, e.pitch_hint, e.company_kind, e.ip_gph_possible, e.employment_hint
           FROM vacancies v JOIN evaluations e ON e.vacancy_id = v.id WHERE v.id = ?""", (vacancy_id,)).fetchone()


def _set(conn, vacancy_id, **cols):
    for k, v in cols.items():
        conn.execute(f"UPDATE vacancies SET {k} = ? WHERE id = ?", (v, vacancy_id))


def test_salary_stated_is_a_flag_not_a_figure():
    """The letter must never quote a sum; it only needs to know whether "ваш бюджет" is a known thing."""
    conn = _db()
    _set(conn, 1, salary_raw=json.dumps({"from": 80000, "currencyCode": "RUR", "gross": True}))
    assert letter_payload(_payload_row(conn, 1))["salary_stated"] is True
    assert not any("80000" in str(v) for v in letter_payload(_payload_row(conn, 1)).values())

    _set(conn, 1, salary_raw=json.dumps({"noCompensation": {}}))
    assert letter_payload(_payload_row(conn, 1))["salary_stated"] is False

    _set(conn, 1, salary_raw=None)
    assert letter_payload(_payload_row(conn, 1))["salary_stated"] is False

    _set(conn, 1, salary_raw=json.dumps({"to": 150000, "currencyCode": "RUR"}))  # upper bound only
    assert letter_payload(_payload_row(conn, 1))["salary_stated"] is True


def test_salary_note_catches_the_common_phrasings():
    conn = _db()
    asks = '{"description": "<p>Нужен ПЛК. В отклике укажите ваши финансовые ожидания.</p>"}'
    _set(conn, 1, raw_json=asks)
    assert letter_payload(_payload_row(conn, 1))["salary_note"] == "вакансия просит указать зарплатные ожидания"

    _set(conn, 1, raw_json='{"description": "<p>Нужен ПЛК. Напишите желаемый доход.</p>"}')
    assert letter_payload(_payload_row(conn, 1))["salary_note"] == "вакансия просит указать зарплатные ожидания"

    _set(conn, 1, raw_json='{"description": "<p>Нужен программист ПЛК на CODESYS.</p>"}')
    assert letter_payload(_payload_row(conn, 1))["salary_note"] == "зарплату не упоминать"


def test_profi_bid_payload_has_its_own_budget_not_the_hh_flag():
    conn = _db()
    conn.execute("INSERT INTO vacancies(id,hh_id,site,title,employer,url,source,search_pass,status,raw_json,first_seen_at,updated_at) "
                 "VALUES (9,'profi:1','profi','Наладить ПЛК','Сергей','u','profi','profi','evaluated',?,'t','t')",
                 ('{"description": "нужен ПЛК", "budget": "до 5000 ₽"}',))
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                 "VALUES (9,80,0,0,80,60,80,'yes',0,'project','end_customer','нужен ПЛК','предложить','[]','t')")
    payload = letter_payload(_payload_row(conn, 9))
    assert payload["budget"] == "до 5000 ₽" and "salary_stated" not in payload


def test_letter_payload_carries_the_company_dossier_and_the_place(tmp_path):
    """Before v9.0 the model knew only the employer's name — not even the city the work is in."""
    conn = _db()
    conn.execute("UPDATE vacancies SET area_name = 'Кузнецк', work_format = 'remote', "
                 "raw_json = ? WHERE id = 1",
                 (json.dumps({"description": "<p>Нужен ПЛК</p>",
                              "address": {"displayName": "Кузнецк, улица Белинского, 8А"}}, ensure_ascii=False),))
    row = repo.lead_by_hh_id(conn, "1")

    brief = {"found": True, "what_they_do": "Выпускает эластичный пенополиуретан", "industry": "Химия"}
    payload = letter_payload(row, brief)
    assert payload["company"] == brief
    assert payload["city"] == "Кузнецк"
    assert payload["address"] == "Кузнецк, улица Белинского, 8А"
    assert payload["work_format"] == "remote"

    assert letter_payload(row)["company"] is None      # nothing known: the prompt then writes as before


def _row_with_company(conn):
    conn.execute("UPDATE vacancies SET employer = 'ФомЛайн' WHERE id = 1")
    return repo.lead_by_hh_id(conn, "1")


def test_code_checks_catch_money_and_cliches():
    """The letter must never quote a sum (decisions #22-24) and must not read as a mailshot."""
    from hh_scout.llm.cover_letter import check_letter
    conn = _db()
    row = _row_with_company(conn)
    company = {"industry": "химическое производство", "products": ["пенополиуретан"]}

    ok = "Вижу, что вы производите пенополиуретан. Работаю по ИП."
    assert check_letter(ok, row, company) == ""
    assert "сумма" in check_letter(ok + " Ставка 150 000 ₽ в месяц.", row, company)
    assert "оборот" in check_letter(ok + " Помогу закрыть позицию.", row, company)
    assert "не называет" in check_letter("Здравствуйте. Работаю по ИП, готов обсудить.", row, company)
    assert check_letter("Здравствуйте. Работаю по ИП.", row, None) == ""   # no dossier — no such demand


@respx.mock
def test_daily_quota_limits_letters_across_runs(tmp_path):
    """Three sittings a day must not turn a quota of two into six."""
    s = _settings(tmp_path)
    s = s.model_copy(update={"digest_max_items": 2})
    conn = _db()
    respx.post("http://bridge.test/complete").mock(
        return_value=httpx.Response(200, json={"text": "x" * 600, "cost_usd": 0.01}))

    first = CoverLetterWriter(s, conn, BridgeClient(s, retries=0)).run()
    assert first.written == 2
    second = CoverLetterWriter(s, conn, BridgeClient(s, retries=0)).run()
    assert second.written == 0            # the quota is spent for today
    assert repo.letters_written_today(conn) == 2


@respx.mock
def test_research_gets_its_own_client_without_retries(tmp_path):
    """White box on purpose: sharing the letter's client (retries=2) with research cost 21 minutes on one company.

    A dead site makes the bridge answer 504 only after BRIDGE_WEB_TIMEOUT (420 s), so every extra retry is
    another seven minutes of waiting for a dossier that will not arrive.
    """
    s = _settings(tmp_path)
    conn = _db()
    letter_bridge = BridgeClient(s, sleep=lambda x: None)
    w = CoverLetterWriter(s, conn, letter_bridge)
    assert w.researcher.bridge is not letter_bridge
    assert w.researcher.bridge._retries == 0 and letter_bridge._retries == 2


@respx.mock
def test_reported_cost_covers_research_too(tmp_path, caplog):
    s = _settings(tmp_path)
    conn = _db()
    good = "Здравствуйте.\n" + "Опыт CODESYS и MasterSCADA. " * 30 + "\nИван Иванов, +7 900"
    respx.post("http://bridge.test/complete").mock(
        return_value=httpx.Response(200, json={"text": good, "usage": {}, "cost_usd": 0.01}))
    w = CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None))
    w._research_bridge.cost_usd = 0.25   # as if one dossier had been read from the web
    w._research_bridge.calls = 1
    with caplog.at_level("INFO", logger="hh_scout.llm.cover_letter"):
        stats = w.run()
    assert stats.bridge_calls == w.bridge.calls + 1
    line = next(m for m in caplog.messages if m.startswith("Письма: написано"))
    assert "$0.2" in line or "$0.3" in line   # research money is in the total, not silently dropped


@respx.mock
def test_no_letter_for_a_company_the_owner_already_answered(tmp_path):
    """The owner's rule: write only to companies he has not answered yet — a second letter hits the same HR desk."""
    s = _settings(tmp_path)
    conn = _db()          # все три вакансии одного работодателя «ООО»
    conn.execute("UPDATE vacancies SET applied = 1 WHERE hh_id = '2'")   # отклик на hh.ru по одной из них
    route = respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(200, json={"text": "x" * 600}))
    stats = CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None)).run()
    assert stats.written == 0 and route.call_count == 0   # ни разведки, ни черновика — отсечено до трат
    assert repo.get_cover_letter(conn, 1) is None


@respx.mock
def test_the_card_button_closes_the_company_for_letters_too(tmp_path):
    s = _settings(tmp_path)
    conn = _db()
    conn.execute("INSERT INTO lead_actions(vacancy_id, action, created_at) VALUES (3, 'responded', ?)",
                 ("2026-09-14T09:23:15+00:00",))
    route = respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(200, json={"text": "x" * 600}))
    w = CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None))
    assert w.run().written == 0 and route.call_count == 0
    answered = w.answered_employer(conn.execute("SELECT * FROM vacancies WHERE id = 1").fetchone())
    assert answered["hh_id"] == "3"    # именно та вакансия, по которой владелец отметил «✅ Написал»


def test_role_address_label_is_stripped_but_the_thought_stays():
    """«Для отдела кадров:» выдаёт рассылку — надпись срезается, смысловая часть остаётся (решение №38)."""
    from hh_scout.llm.cover_letter import _clean

    tail = "подряд не требует ставки в штатном расписании."
    for label in ("Для менеджера по подбору:", "Для отдела кадров:", "Для службы персонала:",
                  "Для кадровой части:", "Для HR:", "Для тех, кто ведёт подбор:", "Отдельно для подбора:"):
        cleaned = _clean(f"Здравствуйте.\n\n{label} {tail}\n\nСергей")
        assert cleaned == f"Здравствуйте.\n\nПодряд не требует ставки в штатном расписании.\n\nСергей", label


def test_role_address_stripped_in_the_middle_of_a_paragraph():
    from hh_scout.llm.cover_letter import _clean

    text = "Стоимость считается от объёма. Отдельно для подбора: задача может поехать сразу."
    assert _clean(text) == "Стоимость считается от объёма. Задача может поехать сразу."


def test_normal_letter_text_is_not_touched():
    """Никаких ложных срабатываний: «для» в обычной фразе и двоеточие в перечислении — не обращение."""
    from hh_scout.llm.cover_letter import _clean

    for text in ("Для этого достаточно одного узла — посмотрим, как пойдёт.",
                 "Готов работать в вашем процессе: стандарты оформления кода, отчётность, созвоны.",
                 "По вашим задачам:\n\n— Программирование ПЛК: CODESYS 3.5, Structured Text.",
                 "Беру программную часть для вашей линии розлива: ПЛК, экраны панелей, обмен."):
        assert _clean(text) == text
