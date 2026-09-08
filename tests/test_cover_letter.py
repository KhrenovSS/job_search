import httpx
import respx

from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.llm.bridge_client import BridgeClient
from hh_scout.llm.cover_letter import CoverLetterWriter
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
