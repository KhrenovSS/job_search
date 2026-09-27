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
    res = trudvsem.sync(conn, s, queries=("АСУ ТП", "ПЛК"))
    assert res.seen == 5 and res.new == 5 and res.prefiltered == 2 and res.requests == 2   # 5 < 100: one page per query
    assert [c["text"] for c in calls] == ["АСУ ТП", "ПЛК"] and all("modifiedFrom" in c for c in calls)
    assert repo.count_site(conn, "trudvsem") == 5
    last = kv_get(conn, trudvsem.KV_LAST_SYNC)
    assert last and last.endswith("|5|5")
    # the next sync starts from the last one (minus the overlap), not from the backfill window
    res2 = trudvsem.sync(conn, s, queries=("АСУ ТП",))
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
    res = trudvsem.sync(conn, s, queries=("АСУ ТП",))
    assert res.prefiltered == 1 and res.waiting == 1 and trudvsem.waiting(conn) == 1
    res2 = trudvsem.sync(conn, s, queries=("АСУ ТП",))          # nothing new: the waiting row is admitted
    assert res2.prefiltered == 1 and res2.new == 0 and trudvsem.waiting(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE site='trudvsem' AND status='prefiltered'").fetchone()[0] == 2

