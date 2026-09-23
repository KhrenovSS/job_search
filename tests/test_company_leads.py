"""v9.13: a lead can be a company — panel builders, design bureaus, ОВЕН integrators (decision #53)."""

import json

import httpx
import respx

from hh_scout.config import Settings
from hh_scout.db import MIGRATIONS, connect, migrate
from hh_scout.llm.bridge_client import BridgeClient
from hh_scout.pipeline import repo
from hh_scout.pipeline.rows import contact_email, lead_kind, letter_key, needs_email


def _db():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _company_row(conn, vid, hh_id, *, title="Сборщик шкафов автоматики", employer="Ктм Групп", channel="panel",
                 status="prefiltered", description="Собираем шкафы управления вентиляцией и насосными на ОВЕН"):
    conn.execute("INSERT INTO vacancies(id,hh_id,title,employer,employer_id,url,area_name,source,search_pass,lead_kind,status,"
                 "raw_json,first_seen_at,updated_at) VALUES (?,?,?,?,?,'u','Краснодар','s',?,'company',?,?,'t','t')",
                 (vid, hh_id, title, employer, str(vid), channel, status, json.dumps({"description": description})))


def test_m015_adds_lead_kind_and_offer_focus():
    conn = _db()
    assert migrate(conn) == len(MIGRATIONS)
    assert {r[1] for r in conn.execute("PRAGMA table_info(vacancies)")} >= {"lead_kind"}
    assert {r[1] for r in conn.execute("PRAGMA table_info(evaluations)")} >= {"offer_focus"}
    conn.execute("INSERT INTO vacancies(hh_id,title,url,source,search_pass,status,first_seen_at,updated_at) "
                 "VALUES ('1','t','u','s','regional','new','x','x')")
    row = conn.execute("SELECT * FROM vacancies").fetchone()
    assert lead_kind(row) == "vacancy" and letter_key(row) == "hh"


def test_letter_key_separates_the_three_prompt_families():
    assert letter_key({"site": "profi", "lead_kind": "vacancy"}) == "profi"
    assert letter_key({"site": "hh", "lead_kind": "company"}) == "company"
    assert letter_key({"site": "owen", "lead_kind": "company"}) == "company"
    assert letter_key({"site": "hh"}) == "hh"


@respx.mock
def test_triage_uses_the_company_prompt_for_company_cards(tmp_path):
    from hh_scout.llm.triage import Triager

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("ПРОФИЛЬ", encoding="utf-8")
    (prompts / "card_triage.md").write_text("h\n---\nVACANCY {candidate_profile}", encoding="utf-8")
    (prompts / "company_triage.md").write_text("h\n---\nCOMPANY {candidate_profile}", encoding="utf-8")
    s = Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts)
    conn = _db()
    conn.execute("INSERT INTO vacancies(hh_id,title,url,source,search_pass,status,first_seen_at,updated_at) "
                 "VALUES ('1','Инженер АСУ ТП','u','s','regional','triage','t','t')")
    _company_row(conn, 2, "2", status="triage")
    seen = []

    def answer(request):
        body = json.loads(request.content)
        seen.append(body["system_text"])
        ids = [c["hh_id"] for c in json.loads(body["messages"][0]["content"])]
        return httpx.Response(200, json={"text": json.dumps([{"hh_id": i, "open": True, "priority": 1, "reason": "ok"} for i in ids])})

    respx.post("http://bridge.test/complete").mock(side_effect=answer)
    Triager(s, conn, BridgeClient(s, sleep=lambda x: None)).run()
    assert sorted(seen) == ["COMPANY ПРОФИЛЬ", "VACANCY ПРОФИЛЬ"]
    assert conn.execute("SELECT status FROM vacancies WHERE hh_id='2'").fetchone()[0] == "to_fetch"


@respx.mock
def test_company_evaluation_is_stored_with_fit_and_lead_and_no_role(tmp_path):
    from hh_scout.llm.evaluator import Evaluator, vacancy_payload

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("ПРОФИЛЬ", encoding="utf-8")
    (prompts / "company_evaluation.md").write_text("h\n---\nCOMPANY-EVAL {feedback_block}", encoding="utf-8")
    s = Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts)
    conn = _db()
    _company_row(conn, 5, "5")
    payload = vacancy_payload(conn.execute("SELECT * FROM vacancies WHERE id=5").fetchone())
    assert payload["kind"] == "company" and payload["channel"] == "panel" and "salary" not in json.dumps(payload)
    respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(200, json={"text": json.dumps([
        {"hh_id": "5", "fit_score": 80, "lead_score": 60, "company_kind": "panel_builder", "verdict": "Собирают шкафы",
         "pitch_hint": "Программа под каждый шкаф", "offer_focus": ["plc_hmi_per_panel", "templates", "nonsense"],
         "red_flags": []}])}))
    stats = Evaluator(s, conn, BridgeClient(s, sleep=lambda x: None)).run()
    assert stats.evaluated == 1
    row = repo.lead_by_hh_id(conn, "5")
    assert row["total"] == round(0.6 * 80 + 0.4 * 60) and row["tech_score"] == 80 and row["role_score"] == 0
    assert row["company_kind"] == "panel_builder" and row["ip_gph_possible"] == "maybe"
    assert json.loads(row["offer_focus"]) == ["plc_hmi_per_panel", "templates"]     # unknown codes dropped
    assert row["status"] == "evaluated" and lead_kind(row) == "company"


@respx.mock
def test_company_letter_uses_its_own_prompt_stamp_and_payload(tmp_path):
    from hh_scout.llm.cover_letter import CoverLetterWriter, letter_payload, rules_hash

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("ПРОФИЛЬ", encoding="utf-8")
    (prompts / "resume.md").write_text("РЕЗЮМЕ", encoding="utf-8")
    (prompts / "cover_letter.md").write_text("h\n---\nRESPONSE {resume}", encoding="utf-8")
    (prompts / "company_offer.md").write_text("h\n---\nOFFER {resume} {candidate_profile}", encoding="utf-8")
    s = Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts,
                 company_research_enabled=False)
    conn = _db()
    _company_row(conn, 7, "7", status="evaluated")
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at,offer_focus) "
                 "VALUES (7,80,0,0,0,60,72,'maybe',0,'unknown','panel_builder','Собирают шкафы','под каждый шкаф','[]','t',?)",
                 (json.dumps(["plc_hmi_per_panel"]),))
    row = repo.lead_by_hh_id(conn, "7")
    p = letter_payload(row)
    assert p["kind"] == "company" and p["offer_focus"] == ["plc_hmi_per_panel"] and p["seen_through"] == "Сборщик шкафов автоматики"
    assert "salary_stated" not in p and "salary_note" not in p
    assert rules_hash(s, "company") not in ("", rules_hash(s, "hh"))
    good = "Здравствуйте.\n" + "Собираете шкафы управления, беру программу под каждый. " * 25 + \
           "\n\nИван Иванов\nинженер-программист ПЛК и SCADA, работаю по договору (ИП)\n+7 900 123-45-67"
    route = respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(200, json={"text": good, "cost_usd": 0.01}))
    w = CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None))
    assert w.run().written == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["system_text"] == "OFFER РЕЗЮМЕ ПРОФИЛЬ"
    assert conn.execute("SELECT rules_hash FROM cover_letters WHERE vacancy_id = 7").fetchone()[0] == rules_hash(s, "company")


def test_outcomes_slice_by_lead_kind_and_channel():
    from hh_scout.pipeline import outcomes

    conn = _db()
    _company_row(conn, 9, "9", status="sent")
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                 "VALUES (9,80,0,0,0,60,72,'maybe',0,'unknown','panel_builder','v','p','[]','t')")
    did = conn.execute("INSERT INTO digests(sent_at,items_count,collected_count) VALUES ('2026-09-01T10:00:00+00:00',1,1)").lastrowid
    conn.execute("INSERT INTO digest_items(digest_id,vacancy_id,position) VALUES (?,9,1)", (did,))
    conn.execute("INSERT INTO lead_actions(vacancy_id,action,created_at) VALUES (9,'responded','t')")
    rows = repo.outcome_rows(conn, "2000-01-01")
    dims = dict(outcomes.DIMENSIONS)
    assert outcomes.outcome_by(rows, dims["Тип лида"], 0)[0]["name"] == "компания"
    assert outcomes.outcome_by(rows, dims["Канал"], 0)[0]["name"] == "щитовики (hh)"


def test_catalogue_rows_never_enter_the_vacancy_stages(monkeypatch):
    """v9.14 regression: on 20.09 the ОВЕН rows went new → triage → to_fetch and DetailsFetcher spent 76 page
    loads on `hh.ru/vacancy/owen:<id>`, which also raised a false «markup changed» alarm."""
    from hh_scout.pipeline import prefilter
    from hh_scout.sources import owen

    conn = _db()
    owen.store(conn, owen.parse_integrators(json.loads(
        (__import__("pathlib").Path(__file__).parent / "fixtures" / "owen_integrators.json").read_text(encoding="utf-8"))))
    conn.execute("INSERT INTO vacancies(hh_id,title,url,source,search_pass,status,site,first_seen_at,updated_at) "
                 "VALUES ('1','Инженер АСУ ТП','u','s','regional','new','hh','t','t')")
    prefilter.run(conn, Settings(_env_file=None))
    kinds = {r["site"]: r["status"] for r in conn.execute("SELECT site, status FROM vacancies GROUP BY site")}
    assert kinds["hh"] == "triage" and kinds["owen"] == "new"          # the catalogue waits for its admission
    assert repo.list_vacancies(conn, "to_fetch", site="hh") == []

    # even if a row somehow reaches to_fetch, the details stage refuses to open it
    conn.execute("UPDATE vacancies SET status = 'to_fetch' WHERE site = 'owen'")
    assert [r["site"] for r in repo.list_vacancies(conn, "to_fetch", site="hh")] == []
    assert repo.reset_catalogue_rows(conn) == 3
    assert {r["status"] for r in conn.execute("SELECT status FROM vacancies WHERE site='owen'")} == {"new"}


def _owen_row(conn, vid, hh_id, emails, *, status="evaluated", total=70):
    raw = {"description": "Интегратор ОВЕН", "industries": ["вода"], "status": "Золотой", "site": "https://x.ru/",
           "emails": emails, "phones": ["+7 900 000-00-00"]}
    conn.execute("INSERT INTO vacancies(id,hh_id,site,title,employer,employer_id,url,area_name,source,search_pass,lead_kind,status,"
                 "raw_json,first_seen_at,updated_at) VALUES (?,?,'owen','Системный интегратор ОВЕН (Золотой)',?,?,'https://x.ru/',"
                 "'Тула','owen_catalog','owen_si','company',?,?,'t','t')", (vid, hh_id, f"Компания {vid}", hh_id, status, json.dumps(raw)))
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at,offer_focus) "
                 "VALUES (?,80,0,0,0,60,?,'maybe',0,'unknown','integrator','Интегратор','подряд','[]','t','[]')", (vid, total))


def test_contact_email_prefers_the_catalogue_then_the_dossier_and_validates_both():
    """decision #56: a catalogue company is reachable only by e-mail — the card must name it or the lead is dropped."""
    row = {"site": "owen", "lead_kind": "company", "raw_json": json.dumps({"emails": ["нет", " Sales@Firm.RU. "]}), "company_brief": None}
    assert needs_email(row) and contact_email(row) == "sales@firm.ru"
    row = {"site": "owen", "lead_kind": "company", "raw_json": json.dumps({"emails": []}),
           "company_brief": json.dumps({"found": True, "contact_email": "info@firm.ru"})}
    assert contact_email(row) == "info@firm.ru"
    row = {"site": "owen", "lead_kind": "company", "raw_json": None, "company_brief": None}
    assert contact_email(row) is None
    assert contact_email(row, {"found": True, "contact_email": "office@firm.ru"}) == "office@firm.ru"
    assert contact_email(row, {"found": False, "contact_email": "www.firm.ru"}) is None
    # hh vacancies and companies found through hh vacancies are answered on hh.ru — no e-mail needed
    assert not needs_email({"site": "hh", "lead_kind": "company"}) and not needs_email({"site": "hh", "lead_kind": "vacancy"})


@respx.mock
def test_letter_stage_skips_a_catalogue_company_without_an_email_and_writes_for_one_with(tmp_path):
    from hh_scout.llm.cover_letter import CoverLetterWriter

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("ПРОФИЛЬ", encoding="utf-8")
    (prompts / "resume.md").write_text("РЕЗЮМЕ", encoding="utf-8")
    (prompts / "cover_letter.md").write_text("h\n---\nRESPONSE {resume}", encoding="utf-8")
    (prompts / "company_offer.md").write_text("h\n---\nOFFER {resume} {candidate_profile}", encoding="utf-8")
    s = Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts,
                 company_research_enabled=False)
    conn = _db()
    _owen_row(conn, 11, "owen:11", ["vk@firm.ru"])
    _owen_row(conn, 12, "owen:12", [])
    good = "Здравствуйте.\n" + "Ведёте проекты на ОВЕН, беру программную часть под ваши шкафы. " * 25 + \
           "\n\nИван Иванов\nинженер-программист ПЛК и SCADA, работаю по договору (ИП)\n+7 900 123-45-67"
    route = respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(200, json={"text": good, "cost_usd": 0.01}))
    w = CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None))
    stats = w.run()
    assert stats.written == 1 and stats.no_email == 1 and stats.failed == 0
    assert route.call_count == 1   # no bridge time spent on the company nobody can write to
    assert conn.execute("SELECT status, skip_reason FROM vacancies WHERE hh_id = 'owen:12'").fetchone()[:] == ("skipped", "no_email")
    assert conn.execute("SELECT status FROM vacancies WHERE hh_id = 'owen:11'").fetchone()[0] == "evaluated"
    assert conn.execute("SELECT COUNT(*) FROM cover_letters").fetchone()[0] == 1


def test_letter_stage_takes_the_email_the_dossier_just_found(tmp_path):
    """Research runs before the check: an address read on the company's site keeps the lead alive."""
    from hh_scout.llm.cover_letter import CoverLetterWriter
    from hh_scout.llm.schemas import CompanyBrief

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name, text in (("candidate_profile.md", "П"), ("resume.md", "Р"), ("cover_letter.md", "h\n---\nR {resume}"),
                       ("company_offer.md", "h\n---\nO {resume}"), ("letter_review.md", "h\n---\nREVIEW")):
        (prompts / name).write_text(text, encoding="utf-8")
    s = Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts,
                 company_research_enabled=False)
    conn = _db()
    _owen_row(conn, 13, "owen:13", [])
    row = repo.lead_by_hh_id(conn, "owen:13")
    w = CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None))
    brief = CompanyBrief(found=True, what_they_do="щиты", contact_email="info@firm.ru")
    assert w.unreachable(row, brief.model_dump()) is False
    assert w.unreachable(row, CompanyBrief(found=False).model_dump()) is True
    assert conn.execute("SELECT skip_reason FROM vacancies WHERE hh_id = 'owen:13'").fetchone()[0] == "no_email"


def test_send_side_guard_drops_a_queued_catalogue_company_without_an_email():
    """A letter written before decision #56 must not carry an unreachable company into the chat."""
    from hh_scout.pipeline.digest_builder import plan_digest, skip_unreachable

    s = Settings(_env_file=None, bridge_url="http://b", bridge_token="t", score_threshold=50)
    conn = _db()
    _owen_row(conn, 21, "owen:21", ["a@firm.ru"])
    _owen_row(conn, 22, "owen:22", [])
    _company_row(conn, 23, "23", status="evaluated")   # a panel builder from an hh vacancy: reachable via hh.ru
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at,offer_focus) "
                 "VALUES (23,80,0,0,0,60,72,'maybe',0,'unknown','panel_builder','щиты','x','[]','t','[]')")
    for vid in (21, 22, 23):
        conn.execute("INSERT INTO cover_letters(vacancy_id,text,created_at) VALUES (?,'письмо','t')", (vid,))
    assert skip_unreachable(conn, s) == 1
    assert skip_unreachable(conn, s) == 0
    assert {r["hh_id"] for r in plan_digest(conn, s).leads} == {"owen:21", "23"}
    assert conn.execute("SELECT status, skip_reason FROM vacancies WHERE hh_id = 'owen:22'").fetchone()[:] == ("skipped", "no_email")
