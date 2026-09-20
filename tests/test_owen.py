"""v9.13: the ОВЕН integrator catalogue as a source of company leads (decision #53)."""

import json
from pathlib import Path

from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.pipeline import repo
from hh_scout.sources import owen

FIXTURE = Path(__file__).parent / "fixtures" / "owen_integrators.json"


def _cards():
    return owen.parse_integrators(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_parse_keeps_integrators_and_drops_dealers_and_hidden_entries():
    cards = _cards()
    assert [c.status for c in cards] == ["Золотой", "Серебряный", "Без партнерства"]   # dealer and hidden are out
    gold = cards[0]
    assert gold.ext_id == f"owen:{gold.tag_id}" and gold.site and gold.emails and gold.description
    assert "<" not in gold.description                                                     # HTML stripped
    plain = cards[2]
    assert plain.description.startswith("Отрасли по каталогу ОВЕН:")                     # no description -> industries


def test_store_is_idempotent_and_rows_are_company_leads_with_a_stable_dossier_key():
    conn = connect(":memory:")
    migrate(conn)
    cards = _cards()
    assert owen.store(conn, cards) == 3
    assert owen.store(conn, cards) == 0
    row = conn.execute("SELECT * FROM vacancies WHERE hh_id = ?", (cards[0].ext_id,)).fetchone()
    assert row["site"] == "owen" and row["lead_kind"] == "company" and row["search_pass"] == "owen_si"
    assert row["employer_id"] == cards[0].ext_id and row["status"] == "new" and row["employer"] == cards[0].name
    raw = json.loads(row["raw_json"])
    assert raw["status"] == "Золотой" and raw["projects_url"].startswith("https://owen.ru/") and raw["emails"]


def test_admission_lets_a_few_companies_in_per_day_best_partners_first():
    conn = connect(":memory:")
    migrate(conn)
    owen.store(conn, _cards())
    assert repo.admit_company_leads(conn, 2) == 2
    admitted = [r["employer"] for r in conn.execute(
        "SELECT employer FROM vacancies WHERE status = 'prefiltered' ORDER BY json_extract(raw_json, '$.status')")]
    statuses = {r["employer"]: json.loads(r["raw_json"])["status"] for r in conn.execute("SELECT employer, raw_json FROM vacancies")}
    assert sorted(statuses[n] for n in admitted) == ["Золотой", "Серебряный"]
    assert repo.admit_company_leads(conn, 2) == 0                       # today's allowance is spent
    t = repo.owen_totals(conn)
    assert t == {"total": 3, "waiting": 1, "sent": 0}
    assert repo.admit_company_leads(conn, 0) == 0


def test_research_payload_for_a_catalogue_company_points_at_owen_not_hh():
    from hh_scout.llm.company_research import payload

    conn = connect(":memory:")
    migrate(conn)
    owen.store(conn, _cards())
    row = conn.execute("SELECT * FROM vacancies ORDER BY id LIMIT 1").fetchone()
    p = payload(row)
    assert p["employer_url"].startswith("https://owen.ru/") and "hh.ru" not in p["employer_url"]
    assert p["source"] == "каталог системных интеграторов ОВЕН" and p["company_site"]


def test_fetch_failure_is_one_exception_not_a_crash(monkeypatch):
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(owen.httpx, "get", boom)
    import pytest
    with pytest.raises(owen.OwenCatalogUnavailable):
        owen.fetch_integrators(Settings(_env_file=None))
