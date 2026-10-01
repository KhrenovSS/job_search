"""v9.41: panel builders found by searching the web through the bridge (decision #75)."""

import json

import httpx
import pytest
import respx

from hh_scout.config import Settings
from hh_scout.db import connect, kv_get, migrate, utcnow
from hh_scout.llm.bridge_client import BridgeClient
from hh_scout.llm.company_research import payload as research_payload
from hh_scout.llm.evaluator import company_payload
from hh_scout.llm.schemas import DiscoveredCompany, DiscoveryAnswer
from hh_scout.pipeline import repo
from hh_scout.pipeline.ranker import format_company_card
from hh_scout.pipeline.rows import contact_email, letter_key, needs_email
from hh_scout.sources import web_discovery as wd

ANSWER = {"companies": [
    {"name": "ООО «Щит-Сервис»", "website": "https://shchit-service.ru/", "city": "Пермь", "region": "Пермский край",
     "inn": "5900000001", "emails": ["info@shchit-service.ru"], "what_they_do": "собирает шкафы управления насосными и АВР",
     "evidence_url": "https://shchit-service.ru/production"},
    {"name": "ООО «Электрощит-Урал»", "website": "https://eshit-ural.ru", "city": "Екатеринбург", "region": "Свердловская область",
     "emails": [], "what_they_do": "НКУ, ВРУ, шкафы автоматики", "evidence_url": "https://eshit-ural.ru/about"},
    {"name": "АО «Уралвагонзавод»", "website": "https://uvz.ru", "city": "Нижний Тагил", "what_they_do": "вагоны", "evidence_url": "https://uvz.ru"},
    {"name": "ООО «Без сайта»", "website": "сайта нет", "city": "Тверь", "what_they_do": "щиты", "evidence_url": ""},
], "note": "сайт DKC рисует список скриптом"}


def _settings(tmp_path, **kw):
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "company_discovery.md").write_text("h\n---\nНАЙДИ ЩИТОВИКОВ", encoding="utf-8")
    return Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts,
                    company_channels="panel,discovery", **kw)


def _db():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _bridge_ok(answers):
    """Each call answers the next item (a dict → JSON text, a str → as is)."""
    it = iter(answers)

    def responder(request):
        a = next(it)
        text = json.dumps(a, ensure_ascii=False) if isinstance(a, dict) else a
        return httpx.Response(200, json={"text": text, "cost_usd": 0.4})
    return respx.post("http://bridge.test/complete").mock(side_effect=responder)


def test_schema_drops_a_website_without_a_domain():
    with pytest.raises(ValueError):
        DiscoveredCompany(name="X", website="сайта нет")
    a = DiscoveryAnswer.model_validate(ANSWER | {"companies": ANSWER["companies"][:2]})
    assert len(a.companies) == 2


def test_tasks_go_vendors_first_then_every_region_per_query():
    t = wd.tasks()
    assert t[0].kind == "vendor" and t[0].payload()["task"] == "vendor" and "сборщики" in t[0].payload()["hint"]
    first_region = t[len(wd.tasks()) - len(wd.regions()) * len(wd.DISCOVERY_REGION_QUERIES)]
    assert first_region.kind == "region" and first_region.query == wd.DISCOVERY_REGION_QUERIES[0]
    assert len(t) == len(wd.DISCOVERY_VENDORS) + len(wd.regions()) * len(wd.DISCOVERY_REGION_QUERIES)
    assert len(set(wd.regions())) == len(wd.regions()) and "Пермский край" in wd.regions()


@respx.mock
def test_job_stores_new_companies_skips_known_domains_and_marks_defence(tmp_path):
    s = _settings(tmp_path, discovery_tasks_per_day=1)
    conn = _db()
    # the Ural firm is already an hh.ru employer with that domain — the same company, not a lead
    conn.execute("INSERT INTO vacancies(hh_id, site, title, employer, employer_id, url, area_name, source, search_pass, status, "
                 "first_seen_at, updated_at) VALUES ('777','hh','Сборщик щитов','Электрощит-Урал','555','u','Екатеринбург','s',"
                 "'panel','sent',?,?)", (utcnow(), utcnow()))
    repo.record_contacts(conn, "555", emails=[], urls=["https://www.eshit-ural.ru/contacts"])
    route = _bridge_ok([ANSWER])
    res = wd.run_job(conn, s, bridge=BridgeClient(s, retries=0))
    assert route.called and res.tasks == 1 and res.failed == 0
    assert res.found == 3 and res.new == 2 and res.known == 1 and res.defense == 1   # the site-less entry never counts
    assert res.calls == 1 and res.cost_usd == pytest.approx(0.4)
    row = conn.execute("SELECT * FROM vacancies WHERE hh_id = 'web:shchit-service.ru'").fetchone()
    assert row["site"] == "web" and row["lead_kind"] == "company" and row["search_pass"] == "discovery" and row["status"] == "new"
    assert row["employer_id"] == "web:shchit-service.ru" and row["employer"] == "ООО «Щит-Сервис»" and row["area_name"] == "Пермь"
    assert row["source"].startswith("discovery:партнёры ") and json.loads(row["raw_json"])["evidence_url"].endswith("/production")
    assert letter_key(row) == "company" and needs_email(row) and contact_email(row) == "info@shchit-service.ru"
    keys = {(r["kind"], r["value"]) for r in conn.execute("SELECT kind, value FROM employer_contacts WHERE employer_id = ?", (row["employer_id"],))}
    assert ("domain", "shchit-service.ru") in keys and ("email", "info@shchit-service.ru") in keys
    uvz = conn.execute("SELECT status, skip_reason FROM vacancies WHERE hh_id = 'web:uvz.ru'").fetchone()
    assert uvz["status"] == "skipped" and uvz["skip_reason"].startswith("defense:name:")
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE hh_id = 'web:eshit-ural.ru'").fetchone()[0] == 0
    assert kv_get(conn, wd.KV_CURSOR) == "1" and kv_get(conn, wd.KV_LAST).endswith("|1|3|2|0.40")
    # the next day moves on to the next task and re-storing is idempotent
    _bridge_ok([ANSWER])
    res2 = wd.run_job(conn, s, bridge=BridgeClient(s, retries=0))
    assert res2.new == 0 and res2.known == 3 and kv_get(conn, wd.KV_CURSOR) == "2"


@respx.mock
def test_job_retries_once_on_a_broken_answer_and_skips_the_task_after_that(tmp_path):
    s = _settings(tmp_path)
    conn = _db()
    route = _bridge_ok(["это не json", "{\"items\": []}", ANSWER])
    res = wd.run_job(conn, s, tasks_n=2, bridge=BridgeClient(s, retries=0))
    assert route.call_count == 3                                   # task 1: two broken answers; task 2: one good one
    assert res.tasks == 2 and res.failed == 1 and res.new == 3 and kv_get(conn, wd.KV_CURSOR) == "2"


def test_parse_answer_drops_one_bad_entry_but_rejects_the_wrong_shape():
    a = wd.parse_answer(ANSWER)
    assert [c.name for c in a.companies][:2] == ["ООО «Щит-Сервис»", "ООО «Электрощит-Урал»"] and len(a.companies) == 3
    assert a.note.startswith("сайт DKC")
    with pytest.raises(ValueError):
        wd.parse_answer({"items": []})
    with pytest.raises(ValueError):
        wd.parse_answer([1, 2])


@respx.mock
def test_a_dead_bridge_fails_the_task_not_the_job(tmp_path):
    s = _settings(tmp_path)
    conn = _db()
    respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(502, text="claude CLI exit 1"))
    res = wd.run_job(conn, s, tasks_n=1, bridge=BridgeClient(s, retries=0))
    assert res.tasks == 1 and res.failed == 1 and res.new == 0 and kv_get(conn, wd.KV_CURSOR) == "1"


@respx.mock
def test_admission_gate_payloads_and_card(tmp_path):
    s = _settings(tmp_path, discovery_leads_per_day=1)
    conn = _db()
    _bridge_ok([ANSWER | {"companies": ANSWER["companies"][:2]}])
    wd.run_job(conn, s, tasks_n=1, bridge=BridgeClient(s, retries=0))
    rows = repo.admit_company_leads(conn, s.discovery_leads_per_day, search_pass=repo.DISCOVERY_PASS)
    assert len(rows) == 1 and repo.admit_company_leads(conn, 1, search_pass=repo.DISCOVERY_PASS) == []   # one a day
    assert repo.site_totals(conn, "web") == {"total": 2, "waiting": 1, "sent": 0}
    row = conn.execute("SELECT * FROM vacancies WHERE id = ?", (rows[0]["id"],)).fetchone()
    assert row["hh_id"] == "web:shchit-service.ru" and row["status"] == "prefiltered"   # the one with an address goes first
    ev = company_payload(row)
    assert ev["channel"] == "discovery" and ev["vacancy_title"] is None and ev["discovery"]["evidence_url"].endswith("/production")
    assert "собирает шкафы" in ev["description"]
    rp = research_payload(row)
    assert rp["company_site"] == "https://shchit-service.ru/" and rp["source"].startswith("поиск в интернете") and rp["inn"] == "5900000001"
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,offer_focus,created_at) "
                 "VALUES (?,80,0,0,80,80,80,'maybe',0,'staff','panel_builder','собирают шкафы','p','[]','[]',?)", (row["id"], utcnow()))
    conn.execute("UPDATE vacancies SET status = 'evaluated' WHERE id = ?", (row["id"],))
    v = repo.lead_queue(conn, 50)[0]
    e = conn.execute("SELECT * FROM evaluations WHERE vacancy_id = ?", (row["id"],)).fetchone()
    text = format_company_card(1, v, e)
    assert "🌐 щитовики (поиск в интернете)" in text and "🔍 Найдена поиском: партнёры" in text
    assert "shchit-service.ru/production" in text and "📧 Писать на: <b>info@shchit-service.ru</b>" in text
    assert "Найдена по:" not in text and "партнёр ОВЕН" not in text
