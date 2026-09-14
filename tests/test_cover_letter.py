import json

import httpx
import respx

from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.llm.bridge_client import BridgeClient
from hh_scout.llm.cover_letter import CoverLetterWriter, letter_payload
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
