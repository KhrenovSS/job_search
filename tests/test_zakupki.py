"""v9.26: winners of 44-ФЗ automation procurements on zakupki.gov.ru as company leads (channel `tender`, decision #68)."""

import json
from pathlib import Path

import pytest

from hh_scout.config import Settings
from hh_scout.db import connect, kv_get, migrate, utcnow
from hh_scout.llm.evaluator import company_payload
from hh_scout.pipeline import repo
from hh_scout.pipeline.ranker import format_card
from hh_scout.pipeline.rows import contact_email, letter_key, needs_email
from hh_scout.sources import zakupki

FIX = Path(__file__).parent / "fixtures"
RSS = (FIX / "zakupki_rss.xml").read_text(encoding="utf-8")
RESULTS = (FIX / "zakupki_supplier_results.html").read_text(encoding="utf-8")
CARD = (FIX / "zakupki_contract_card.html").read_text(encoding="utf-8")


def _conn():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _settings(**kw):
    return Settings(_env_file=None, zakupki_enabled=True, zakupki_request_gap_s=0, **kw)


class FakeFetcher:
    """Answers by URL substring; counts requests like the real one."""

    def __init__(self, pages: dict[str, str], conn=None):
        self.pages = pages
        self.requests = 0
        self.urls: list[str] = []
        self.conn = conn            # when given: every request asserts the job holds no transaction while it waits
        self.in_tx: list[bool] = []

    def get(self, url, params=None):
        self.requests += 1
        self.urls.append(url)
        if self.conn is not None:
            self.in_tx.append(self.conn.in_transaction)
        for key, page in self.pages.items():
            if key in url:
                return page
        raise zakupki.ZakupkiUnavailable(f"нет страницы для {url}")


def test_parse_notice_rss_reads_object_customer_price_and_stage():
    notices = zakupki.parse_notice_rss(RSS)
    assert len(notices) == 6
    n = notices[0]
    assert n.reg_number == "0373200566326000068" and n.law == "44" and n.notice_type == "zk20"
    assert n.title.startswith("Работы по внедрению информационных систем и ресурсов (Master SCADA")
    assert n.customer.startswith("ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ УЧРЕЖДЕНИЕ") and n.price == "2432781.19"
    assert n.stage == "Определение поставщика завершено" and n.published and n.published.endswith("+00:00")
    assert n.results_url().endswith("/epz/order/notice/zk20/view/supplier-results.html?regNumber=0373200566326000068")


def test_relevance_filter_keeps_automation_and_drops_licences_links_and_roads():
    assert zakupki.relevant("Работы по внедрению информационных систем и ресурсов (Master SCADA)")
    assert zakupki.relevant("Приобретение и монтаж систем автоматизации работы насосных агрегатов и диспетчеризации")
    assert zakupki.relevant("Поставка шкафа ПЛК ТМ для К-1 с выполнением ШМР и ПНР")
    assert not zakupki.relevant("Поставка обновления лицензии на программное обеспечение SCADA-система ЭНТЕК")
    assert not zakupki.relevant("Услуги сотовой связи для сбора данных с приборов учёта (диспетчеризация)")
    assert not zakupki.relevant("Содержание автомобильных дорог регионального значения")
    assert not zakupki.relevant("Оказание услуг по техническому обслуживанию системы диспетчеризации лифтов")
    assert not zakupki.relevant("Оказание услуг по техническому обслуживанию диспетчеризации инженерных систем")
    assert not zakupki.relevant("Модернизация диспетчеризации АПС, СОУЭ и АПТ зданий")
    assert zakupki.relevant("Оказание услуг по модернизации системы автоматизации и диспетчеризации котельной")
    assert zakupki.relevant("Модернизация программного обеспечения (Master SCADA)")
    # v9.40: cabinets and switchgear as the object — the winner is a panel builder (decision #75)
    assert zakupki.relevant("Поставка НКУ для реконструкции КТП")
    assert zakupki.relevant("Поставка щита управления насосами")
    assert zakupki.relevant("Поставка низковольтного комплектного устройства (ВРУ)")
    assert not zakupki.relevant("Поставка щитов питания для вентиляции")


def test_parse_supplier_results_and_contract_card():
    contracts = zakupki.parse_supplier_results(RESULTS)
    assert contracts == [("2771822544426000087", 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "НАУЧНО-АНАЛИТИЧЕСКИЙ ЦЕНТР АГИАСМА"')]
    assert zakupki.parse_supplier_results("<html>ничего</html>") == []
    s = zakupki.parse_contract_card(CARD)
    assert s.short_name == 'ООО "НАУЧНО-АНАЛИТИЧЕСКИЙ ЦЕНТР АГИАСМА"' and s.inn == "9719084711" and s.kpp == "771901001"
    assert s.emails == ["agiasma-info@mail.ru"] and s.phones and "9660047475" in s.phones[0]
    assert s.address.startswith("105203, Г.МОСКВА") and s.status == "субъект малого предпринимательства"
    assert s.subject == "Работы по внедрению информационных систем и ресурсов (MasterSCADA)"
    assert s.price.startswith("1 448 420") and s.signed == "22.09.2026" and s.deadline == "28.12.2026"
    assert s.customer.startswith("ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ УЧРЕЖДЕНИЕ ЗДРАВООХРАНЕНИЯ") and s.reestr_number == "2771822544426000087"


def test_job_stores_relevant_notices_and_resolves_the_winner_into_a_company_lead():
    conn = _conn()
    s = _settings()
    f = FakeFetcher({"rss.html": RSS, "supplier-results.html": RESULTS, "contractCard": CARD})
    res = zakupki.run_job(conn, s, f, queries=("SCADA",), resolve_limit=1)
    assert res.notices == 6 and res.relevant >= 3 and res.new == res.relevant
    assert res.resolved == 1 and res.requests == 3                       # RSS + results + card
    row = conn.execute("SELECT * FROM vacancies WHERE site = 'zakupki' AND status = 'prefiltered'").fetchone()
    assert row["hh_id"] == "zk:0373200566326000068" and row["lead_kind"] == "company" and row["search_pass"] == "tender"
    assert row["employer"] == 'ООО "НАУЧНО-АНАЛИТИЧЕСКИЙ ЦЕНТР АГИАСМА"' and row["employer_id"] == "zk:9719084711"
    assert row["area_name"] == "Москва" and row["title"].startswith("Закупка: Работы по внедрению")
    raw = json.loads(row["raw_json"])
    assert raw["emails"] == ["agiasma-info@mail.ru"] and raw["contract_signed"] == "22.09.2026" and raw["tries"] == 1
    assert "Победитель" in raw["description"] and "MasterSCADA" in raw["description"]
    assert letter_key(row) == "company" and needs_email(row) and contact_email(row) == "agiasma-info@mail.ru"
    assert ("email", "agiasma-info@mail.ru") in {(r["kind"], r["value"]) for r in conn.execute(
        "SELECT kind, value FROM employer_contacts WHERE employer_id = 'zk:9719084711'")}
    payload = company_payload(row)
    assert payload["channel"] == "tender" and payload["tender"]["customer"].startswith("ГОСУДАРСТВЕННОЕ")
    assert payload["tender"]["contract_subject"].endswith("(MasterSCADA)")
    last = kv_get(conn, zakupki.KV_LAST)
    assert last and last.endswith("|6|%d|1" % res.new)
    # the same feed tomorrow: nothing new; the next notice resolves to the same winner — one company, one lead
    res2 = zakupki.run_job(conn, s, FakeFetcher({"rss.html": RSS, "supplier-results.html": RESULTS, "contractCard": CARD}),
                           queries=("SCADA",), resolve_limit=1)
    assert res2.new == 0 and res2.resolved == 0 and res2.skipped == 1
    twin = conn.execute("SELECT skip_reason FROM vacancies WHERE site='zakupki' AND status='skipped'").fetchone()
    assert twin["skip_reason"] == "duplicate_employer:zk:0373200566326000068"


def test_notice_without_a_contract_waits_then_gives_up():
    conn = _conn()
    s = _settings(zakupki_max_tries=2)
    empty = "<html><body>Информация о процедуре заключения контракта</body></html>"
    f = FakeFetcher({"rss.html": RSS, "supplier-results.html": empty})
    res = zakupki.run_job(conn, s, f, queries=("SCADA",), resolve_limit=10)
    assert res.resolved == 0 and res.waiting == res.new == 3
    row = conn.execute("SELECT * FROM vacancies WHERE site='zakupki' ORDER BY published_at DESC, id LIMIT 1").fetchone()
    assert row["status"] == "new" and json.loads(row["raw_json"])["tries"] == 1
    res = zakupki.run_job(conn, s, f, queries=("SCADA",), resolve_limit=10)   # second daily look: still no contract
    assert res.skipped == 3
    row = conn.execute("SELECT status, skip_reason FROM vacancies WHERE hh_id = ?", (row["hh_id"],)).fetchone()
    assert tuple(row) == ("skipped", "tender:no_contract")


def test_winner_already_answered_on_hh_is_closed_through_the_shared_address():
    conn = _conn()
    s = _settings()
    conn.execute("INSERT INTO vacancies(hh_id, site, title, employer, employer_id, url, area_name, source, search_pass, "
                 "status, applied, first_seen_at, updated_at) VALUES ('555','hh','Инженер','Агиасма','9','u','Москва',"
                 "'s','regional','sent',1,?,?)", (utcnow(), utcnow()))
    repo.record_contacts(conn, "9", emails=["agiasma-info@mail.ru"], urls=[])
    f = FakeFetcher({"rss.html": RSS, "supplier-results.html": RESULTS, "contractCard": CARD})
    res = zakupki.run_job(conn, s, f, queries=("SCADA",), resolve_limit=1)
    assert res.resolved == 0 and res.skipped == 1
    row = conn.execute("SELECT status, skip_reason FROM vacancies WHERE hh_id = 'zk:0373200566326000068'").fetchone()
    assert row["status"] == "skipped" and row["skip_reason"].startswith("employer_responded:")


def test_company_card_shows_the_contract_line_and_where_to_write():
    conn = _conn()
    s = _settings()
    f = FakeFetcher({"rss.html": RSS, "supplier-results.html": RESULTS, "contractCard": CARD})
    zakupki.run_job(conn, s, f, queries=("SCADA",), resolve_limit=1)
    vid = conn.execute("SELECT id FROM vacancies WHERE status = 'prefiltered'").fetchone()[0]
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,offer_focus,created_at) "
                 "VALUES (?,80,0,0,0,70,76,'maybe',0,'staff','integrator','внедряют MasterSCADA','p','[]',"
                 "'[\"subcontract_programming\"]',?)", (vid, utcnow()))
    conn.execute("UPDATE vacancies SET status = 'evaluated' WHERE id = ?", (vid,))
    v = repo.lead_queue(conn, 50)[0]
    e = conn.execute("SELECT * FROM evaluations WHERE vacancy_id = ?", (vid,)).fetchone()
    text = format_card(1, v, e)
    assert "🏛 победители закупок (ЕИС)" in text and "🏛 Контракт: Работы по внедрению" in text
    assert "1 448 420,00 ₽" in text and "от 22.09.2026" in text and "до 28.12.2026" in text
    assert "📧 Писать на: <b>agiasma-info@mail.ru</b>" in text and "zakupki" not in text.split("📞")[1].split("\n")[0]


def test_fetch_failure_is_reported_as_unavailable(monkeypatch):
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(zakupki.httpx, "get", boom)
    with pytest.raises(zakupki.ZakupkiUnavailable):
        zakupki.Fetcher(_settings()).get("https://zakupki.gov.ru/x")
    assert zakupki.CA_PATH.exists()


def test_city_from_supplier_address_skips_districts():
    assert zakupki._city("ЧУВАШСКАЯ РЕСПУБЛИКА - ЧУВАШИЯ, г.о. ГОРОД ЧЕБОКСАРЫ, Г. ЧЕБОКСАРЫ, УЛ. ТЕКСТИЛЬЩИКОВ, ЗД. 8Л") == "Чебоксары"
    assert zakupki._city("366320, ЧЕЧЕНСКАЯ РЕСПУБЛИКА, м.р-н. КУРЧАЛОЕВСКИЙ, ЭНИКАЛИНСКОЕ, С ЭНИКАЛИ, УЛ А.А.КАДЫРОВА, Д. 17") == "Эникали"
    assert zakupki._city("105203, Г.МОСКВА, ВН.ТЕР.Г. МУНИЦИПАЛЬНЫЙ ОКРУГ ВОСТОЧНОЕ ИЗМАЙЛОВО, УЛ. 14-Я ПАРКОВАЯ") == "Москва"
    assert zakupki._city(None) is None


def test_job_never_holds_a_transaction_while_fetching():
    """v9.44 (03.10): a `BEGIN IMMEDIATE` held across the minute-long gap between two ЕИС requests locked every other
    connection out of writing, and on the scheduler's shared connection the sitting's `with conn:` committed it from
    under the job — «cannot commit - no transaction is active». The network is read in autocommit; writes are short."""
    conn = _conn()
    f = FakeFetcher({"rss.html": RSS, "supplier-results.html": RESULTS, "contractCard": CARD}, conn=conn)
    res = zakupki.run_job(conn, _settings(), f, queries=("SCADA",), resolve_limit=1)
    assert res.resolved == 1 and f.requests == 3
    assert f.in_tx == [False, False, False]
    assert not conn.in_transaction
