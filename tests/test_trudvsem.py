"""v9.25: «Работа России» (trudvsem.ru) open API as a second vacancy source (decision #67)."""

import json
from pathlib import Path

import httpx
import pytest

from hh_scout.config import Settings
from hh_scout.db import connect, kv_get, migrate, utcnow
from hh_scout.pipeline import repo
from hh_scout.pipeline.ranker import format_card
from hh_scout.pipeline.rows import contact_email, letter_key, needs_email
from hh_scout.sources import trudvsem

FIXTURE = Path(__file__).parent / "fixtures" / "trudvsem_vacancies.json"


def _payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _conn():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _settings(**kw):
    return Settings(_env_file=None, trudvsem_enabled=True, trudvsem_request_gap_s=0, **kw)


def _status(conn, hh_id):
    r = conn.execute("SELECT status, skip_reason FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()
    return r["status"], r["skip_reason"]


def test_parse_reads_company_contacts_region_city_and_text():
    vacancies, total = trudvsem.parse_vacancies(_payload())
    assert total == 5 and len(vacancies) == 5
    v = vacancies[0]
    assert v.ext_id.startswith("tv:") and v.title == "Инженер АСУ ТП" and v.employer == 'ООО "ХОЛОД"'
    assert v.employer_id == "tv:1022200704900"                                   # ОГРН: stable across the company's ads
    assert v.emails[0] == "personal7@oooholod.ru" and "simonov@oooholod.ru" in v.emails   # the HR contact first
    assert v.phones and v.contact_person and v.region == "Алтайский край" and v.city == "Заринск"
    assert v.area_name == "Заринск, Алтайский край" and v.url.startswith("https://trudvsem.ru/vacancy/card/")
    assert v.salary_from == 101900 and v.salary_to == 179800 and v.employment == "full"
    assert "Обязанности" in v.description_html and "TIA Portal" in v.description_html
    raw = v.raw()
    assert raw["site"] == "trudvsem" and raw["emails"] == v.emails and raw["inn"] == "2205006790"


def test_blocked_regions_are_recognised_by_name():
    assert trudvsem.blocked_region_name("Республика Крым") == "Республика Крым"
    assert trudvsem.blocked_region_name("Город Севастополь") == "Республика Крым"
    assert trudvsem.blocked_region_name("Херсонская область") == "Херсонская область"
    assert trudvsem.blocked_region_name("Ростовская область") is None
    assert trudvsem.blocked_region_name(None) is None


def test_store_applies_card_rules_and_lands_passing_rows_as_prefiltered():
    conn = _conn()
    s = _settings()
    vacancies, _ = trudvsem.parse_vacancies(_payload())
    statuses = [trudvsem.store_vacancy(conn, s, v, "АСУ ТП") for v in vacancies]
    assert statuses == ["prefiltered", "prefiltered", "skipped", "skipped", "skipped"]
    assert [trudvsem.store_vacancy(conn, s, v, "АСУ ТП") for v in vacancies] == [None] * 5   # idempotent
    assert _status(conn, "tv:11111111-2222-3333-4444-555555555555") == ("skipped", "region:Республика Крым")
    assert _status(conn, "tv:aaaaaaaa-0000-0000-0000-000000000001") == ("skipped", "no_email")
    assert _status(conn, "tv:bbbbbbbb-0000-0000-0000-000000000003") == ("skipped", "stopword:водитель")
    row = conn.execute("SELECT * FROM vacancies WHERE hh_id = ?", (vacancies[0].ext_id,)).fetchone()
    assert row["site"] == "trudvsem" and row["lead_kind"] == "vacancy" and row["search_pass"] == "regional"
    assert row["source"] == "trudvsem:АСУ ТП" and row["status"] == "prefiltered" and row["area_path"] is None
    assert row["salary_from"] == 101900 and json.loads(row["salary_raw"])["currencyCode"] == "RUR"
    # the same prompts as an hh.ru vacancy, but the letter goes by e-mail
    assert letter_key(row) == "hh" and needs_email(row) and contact_email(row) == "personal7@oooholod.ru"
    # the company's addresses are known for «one contact — one lead» (decision #64)
    keys = {(r["kind"], r["value"]) for r in conn.execute(
        "SELECT kind, value FROM employer_contacts WHERE employer_id = ?", (row["employer_id"],))}
    assert ("email", "personal7@oooholod.ru") in keys and ("domain", "oooholod.ru") in keys


def test_a_company_with_an_hh_lead_is_a_duplicate_through_the_shared_domain():
    conn = _conn()
    s = _settings()
    conn.execute("INSERT INTO vacancies(hh_id, site, title, employer, employer_id, url, area_name, source, search_pass, "
                 "status, first_seen_at, updated_at) VALUES ('777','hh','Инженер АСУ ТП','Холод','555','u','Барнаул',"
                 "'s','regional','sent',?,?)", (utcnow(), utcnow()))
    repo.record_contacts(conn, "555", emails=[], urls=["https://oooholod.ru/"])
    vacancies, _ = trudvsem.parse_vacancies(_payload())
    assert trudvsem.store_vacancy(conn, s, vacancies[0], "ПЛК") == "skipped"
    assert _status(conn, vacancies[0].ext_id) == ("skipped", "duplicate_employer:777")


def test_card_shows_the_portal_badge_and_where_to_write():
    conn = _conn()
    s = _settings()
    vacancies, _ = trudvsem.parse_vacancies(_payload())
    trudvsem.store_vacancy(conn, s, vacancies[0], "ПЛК")
    vid = conn.execute("SELECT id FROM vacancies WHERE hh_id = ?", (vacancies[0].ext_id,)).fetchone()[0]
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                 "VALUES (?,80,0,0,80,60,75,'maybe',0,'staff','plant','нужен ПЛК','p','[]',?)", (vid, utcnow()))
    conn.execute("UPDATE vacancies SET status = 'evaluated' WHERE id = ?", (vid,))
    v, e = repo.lead_queue(conn, 50)[0], None
    e = conn.execute("SELECT * FROM evaluations WHERE vacancy_id = ?", (vid,)).fetchone()
    text = format_card(1, v, e)
    assert "🇷🇺 Работа России" in text and "📧 Писать на: <b>personal7@oooholod.ru</b>" in text
    assert "📞 Злобина Марина Сергеевна, +7(913) 082-64-06" in text and "trudvsem.ru/vacancy/card/" in text


def test_sync_pages_through_every_query_and_remembers_the_sync(monkeypatch):
    conn = _conn()
    s = _settings(trudvsem_backfill_days=7)
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(dict(params))
        payload = _payload()
        if params["offset"]:
            payload["results"]["vacancies"] = []
        payload["meta"]["total"] = 5
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(trudvsem.httpx, "get", fake_get)
    res = trudvsem.sync(conn, s, queries=("АСУ ТП", "ПЛК"), company_queries=())
    assert res.seen == 5 and res.new == 5 and res.prefiltered == 2 and res.requests == 2   # 5 < 100: one page per query
    assert [c["text"] for c in calls] == ["АСУ ТП", "ПЛК"] and all("modifiedFrom" in c for c in calls)
    assert repo.count_site(conn, "trudvsem") == 5
    last = kv_get(conn, trudvsem.KV_LAST_SYNC)
    assert last and last.endswith("|5|5")
    # the next sync starts from the last one (minus the overlap), not from the backfill window
    res2 = trudvsem.sync(conn, s, queries=("АСУ ТП",), company_queries=())
    assert res2.new == 0 and res2.seen == 5
    assert trudvsem.last_sync(conn) is not None and res2.since > res.since


def test_sync_failure_is_reported_not_raised_as_a_crash(monkeypatch):
    conn = _conn()

    def boom(*a, **kw):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(trudvsem.httpx, "get", boom)
    with pytest.raises(trudvsem.TrudvsemUnavailable):
        trudvsem.sync(conn, _settings())
    assert kv_get(conn, trudvsem.KV_LAST_SYNC) is None


def test_run_report_lines_and_soft_alert():
    from hh_scout import health
    from hh_scout.pipeline.run import CrawlReport

    r = CrawlReport(trigger="manual", trudvsem_seen=12, trudvsem_new=3)
    assert "Работа России: в выдаче 12 · новых 3" in r.as_text()
    r.trudvsem_error = "ConnectTimeout: timed out"
    assert r.ok
    keys = [a.key for a in health.analyze_report(r)]
    assert any(k.startswith("trudvsem_down:") for k in keys)


def test_per_run_gate_holds_the_tail_as_new_and_admits_it_next_time(monkeypatch):
    conn = _conn()
    s = _settings(trudvsem_per_run=1)

    def fake_get(url, params=None, **kw):
        payload = _payload()
        if params["offset"]:
            payload["results"]["vacancies"] = []
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(trudvsem.httpx, "get", fake_get)
    res = trudvsem.sync(conn, s, queries=("АСУ ТП",), company_queries=())
    assert res.prefiltered == 1 and res.waiting == 1 and trudvsem.waiting(conn) == 1
    res2 = trudvsem.sync(conn, s, queries=("АСУ ТП",), company_queries=())   # nothing new: the waiting row is admitted
    assert res2.prefiltered == 1 and res2.new == 0 and trudvsem.waiting(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE site='trudvsem' AND status='prefiltered'").fetchone()[0] == 2



def _assembler(i, *, email="default"):
    """A panel builder's vacancy as the portal returns it: an assembler, not an engineer (v9.40)."""
    v = _payload()["results"]["vacancies"][0]["vacancy"]
    v = json.loads(json.dumps(v))
    if email == "default":
        email = f"zakaz@shchit-{i}.ru"
    v["id"] = f"cccccccc-0000-0000-0000-00000000000{i}"
    v["job-name"] = "Сборщик щитов"
    v["duty"] = "Сборка шкафов управления и НКУ по схемам, монтаж аппаратуры ОВЕН"
    v["company"] = {"name": f'ООО "ЩИТ-{i}"', "inn": f"526000000{i}", "ogrn": f"102520000000{i}", "companycode": f"c{i}",
                    "email": email, "hr-agency": False}
    v["contact_list"] = [{"contact_type": "Эл. почта", "contact_value": email}] if email else []
    v["region"] = {"name": "Нижегородская область"}
    return v


def test_an_assembler_vacancy_becomes_a_panel_builder_company_lead_not_a_rejected_vacancy():
    """v9.40 (decision #75): «Сборщик щитов» is no engineering title — as a vacancy it is dropped, as a company
    lead of the `panel` channel it goes to the company evaluation and the partnership offer by e-mail."""
    from hh_scout.llm.evaluator import company_payload
    conn = _conn()
    s = _settings()
    v = trudvsem.parse_vacancy(_assembler(1))
    assert trudvsem.store_vacancy(conn, s, v, "сборщик щитов", search_pass="panel", lead_kind="company") == "prefiltered"
    row = conn.execute("SELECT * FROM vacancies WHERE hh_id = ?", (v.ext_id,)).fetchone()
    assert row["lead_kind"] == "company" and row["search_pass"] == "panel" and row["source"] == "trudvsem:сборщик щитов"
    assert letter_key(row) == "company" and needs_email(row) and contact_email(row) == "zakaz@shchit-1.ru"
    assert company_payload(row)["channel"] == "panel" and company_payload(row)["source"] == "портал «Работа России»"
    # the same vacancy stored as a plain vacancy is judged by its title
    v2 = trudvsem.parse_vacancy(_assembler(2))
    assert trudvsem.store_vacancy(conn, s, v2, "сборщик щитов") == "skipped"
    assert _status(conn, v2.ext_id) == ("skipped", "no_engineering_title")
    # without an address the company is no lead either (decision #56)
    v3 = trudvsem.parse_vacancy(_assembler(3, email=None))
    assert trudvsem.store_vacancy(conn, s, v3, "сборщик щитов", search_pass="panel", lead_kind="company") == "skipped"
    assert _status(conn, v3.ext_id) == ("skipped", "no_email")


def test_company_card_shows_the_portal_badge():
    from hh_scout.pipeline.ranker import format_company_card
    conn = _conn()
    v = trudvsem.parse_vacancy(_assembler(1))
    trudvsem.store_vacancy(conn, _settings(), v, "сборщик щитов", search_pass="panel", lead_kind="company")
    vid = conn.execute("SELECT id FROM vacancies WHERE hh_id = ?", (v.ext_id,)).fetchone()[0]
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,offer_focus,created_at) "
                 "VALUES (?,80,0,0,80,80,80,'maybe',0,'staff','panel_builder','собирают шкафы','p','[]','[]',?)", (vid, utcnow()))
    conn.execute("UPDATE vacancies SET status = 'evaluated' WHERE id = ?", (vid,))
    row = repo.lead_queue(conn, 50)[0]
    e = conn.execute("SELECT * FROM evaluations WHERE vacancy_id = ?", (vid,)).fetchone()
    text = format_company_card(1, row, e)
    assert "🔧" in text and "🇷🇺 Работа России" in text and "👀 Найдена по: Сборщик щитов" in text
    assert "📧 Писать на: <b>zakaz@shchit-1.ru</b>" in text


def test_sync_reads_company_queries_after_vacancy_ones_with_their_own_gate(monkeypatch):
    conn = _conn()
    s = _settings(trudvsem_per_run=60, trudvsem_company_per_run=1)
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(params["text"])
        payload = _payload()
        if params["text"] == "сборщик щитов":
            payload["results"]["vacancies"] = [{"vacancy": _assembler(1)}, {"vacancy": _assembler(2)},
                                               {"vacancy": _payload()["results"]["vacancies"][0]["vacancy"]}]
            payload["meta"]["total"] = 3
        if params["offset"]:
            payload["results"]["vacancies"] = []
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(trudvsem.httpx, "get", fake_get)
    res = trudvsem.sync(conn, s, queries=("АСУ ТП",), company_queries=("сборщик щитов",))
    assert calls == ["АСУ ТП", "сборщик щитов"]
    assert res.seen == 5 and res.new == 5 and res.prefiltered == 2            # vacancies as before
    assert res.company_seen == 2 and res.company_new == 2                    # the engineer seen by both stays a vacancy
    assert res.company_prefiltered == 1 and res.company_waiting == 1         # the company gate is its own
    assert trudvsem.waiting(conn, "company") == 1 and trudvsem.waiting(conn, "vacancy") == 0
    assert kv_get(conn, trudvsem.KV_LAST_SYNC).endswith("|7|7")
    res2 = trudvsem.sync(conn, s, queries=("АСУ ТП",), company_queries=("сборщик щитов",))
    assert res2.company_prefiltered == 1 and res2.company_new == 0 and trudvsem.waiting(conn, "company") == 0
    rows = conn.execute("SELECT lead_kind, search_pass, status FROM vacancies WHERE site='trudvsem' AND lead_kind='company'").fetchall()
    assert len(rows) == 2 and all(r["search_pass"] == "panel" and r["status"] == "prefiltered" for r in rows)


def test_run_report_names_new_panel_builders():
    from hh_scout.pipeline.run import CrawlReport
    r = CrawlReport(trigger="manual", trudvsem_seen=12, trudvsem_new=3, trudvsem_company_new=2)
    assert "Работа России: в выдаче 12 · новых 3 · щитовиков новых 2" in r.as_text()
