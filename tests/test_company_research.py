"""Company dossier (llm/company_research.py): cache, refusals and the graceful failure path.

The dossier is the only web-enabled bridge call. It must never be able to break a letter: anything that goes
wrong — research disabled, no employer id, a profi.ru client, an invalid answer, a dead bridge — ends as None
and the letter is written exactly as it was before v9.0.
"""

import json

import httpx
import respx

from hh_scout.config import Settings
from hh_scout.db import connect, migrate, utcnow
from hh_scout.llm.bridge_client import BridgeClient
from hh_scout.llm.company_research import CompanyResearcher, cached, save
from hh_scout.llm.schemas import CompanyBrief

BRIEF = {"found": True, "what_they_do": "Выпускает эластичный пенополиуретан", "industry": "Химическое производство",
         "products": ["ППУ", "матрасы"], "sites": ["Кузнецк"], "scale": "7 заводов",
         "automation_hooks": ["линии вспенивания", "дозирование компонентов"],
         "sources": ["https://hh.ru/employer/108082"], "note": ""}


def _settings(tmp_path, **kw):
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "candidate_profile.md").write_text("ПРОФИЛЬ", encoding="utf-8")
    (prompts / "company_research.md").write_text("h\n---\nСОБЕРИ ДОСЬЕ", encoding="utf-8")
    return Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts, **kw)


def _db(*, employer_id="108082", site="hh"):
    conn = connect(":memory:")
    migrate(conn)
    conn.execute("INSERT INTO vacancies(id,hh_id,site,title,employer,employer_id,url,area_name,source,search_pass,"
                 "status,raw_json,first_seen_at,updated_at) VALUES (1,'1',?,?,?,?,'u','Кузнецк','s','remote',"
                 "'evaluated',?,'t','t')",
                 (site, "Инженер", "ФомЛайн", employer_id, '{"description": "<p>Нужен ПЛК</p>"}'))
    return conn


def _row(conn):
    return conn.execute("SELECT * FROM vacancies WHERE id = 1").fetchone()


def _reply(text):
    return respx.post("http://bridge.test/complete").mock(
        return_value=httpx.Response(200, json={"text": text, "cost_usd": 0.05}))


@respx.mock
def test_dossier_is_fetched_once_and_then_served_from_cache(tmp_path):
    conn = _db()
    route = _reply(json.dumps(BRIEF, ensure_ascii=False))
    s = _settings(tmp_path)
    r = CompanyResearcher(s, conn, BridgeClient(s, retries=0))

    first = r.for_row(_row(conn))
    assert first is not None and first.industry == "Химическое производство"
    assert json.loads(route.calls[0].request.content)["allow_web"] is True   # web only for research
    assert r.for_row(_row(conn)).what_they_do == first.what_they_do
    assert route.call_count == 1                                             # the second call came from the cache
    assert conn.execute("SELECT found FROM employers WHERE employer_id = '108082'").fetchone()[0] == 1


@respx.mock
def test_stale_dossier_is_researched_again(tmp_path):
    conn = _db()
    save(conn, "108082", "ФомЛайн", CompanyBrief(**BRIEF))
    conn.execute("UPDATE employers SET researched_at = '2020-01-01T00:00:00+00:00'")
    assert cached(conn, "108082", ttl_days=180) is None
    assert cached(conn, "108082", ttl_days=0) is not None      # ttl 0 = forever

    _reply(json.dumps(BRIEF, ensure_ascii=False))
    s = _settings(tmp_path)
    assert CompanyResearcher(s, conn, BridgeClient(s, retries=0)).for_row(_row(conn)) is not None


@respx.mock
def test_nothing_found_is_remembered_so_it_is_not_paid_for_twice(tmp_path):
    conn = _db()
    route = _reply(json.dumps({"found": False, "note": "сайт не открылся"}, ensure_ascii=False))
    s = _settings(tmp_path)
    r = CompanyResearcher(s, conn, BridgeClient(s, retries=0))

    brief = r.for_row(_row(conn))
    assert brief is not None and brief.found is False
    r.for_row(_row(conn))
    assert route.call_count == 1
    assert conn.execute("SELECT found FROM employers").fetchone()[0] == 0


@respx.mock
def test_profi_clients_and_cards_without_an_id_are_never_researched(tmp_path):
    s = _settings(tmp_path)
    route = _reply(json.dumps(BRIEF, ensure_ascii=False))
    for conn in (_db(site="profi"), _db(employer_id=None)):
        assert CompanyResearcher(s, conn, BridgeClient(s, retries=0)).for_row(_row(conn)) is None
    assert route.call_count == 0


@respx.mock
def test_disabled_by_setting(tmp_path):
    conn = _db()
    route = _reply(json.dumps(BRIEF, ensure_ascii=False))
    s = _settings(tmp_path, company_research_enabled=False)
    assert CompanyResearcher(s, conn, BridgeClient(s, retries=0)).for_row(_row(conn)) is None
    assert route.call_count == 0


@respx.mock
def test_a_broken_answer_retries_once_then_gives_up_without_raising(tmp_path):
    conn = _db()
    route = _reply("это не JSON")
    s = _settings(tmp_path)
    assert CompanyResearcher(s, conn, BridgeClient(s, retries=0)).for_row(_row(conn)) is None
    assert route.call_count == 2                                  # one retry, as CLAUDE.md requires
    assert conn.execute("SELECT COUNT(*) FROM employers").fetchone()[0] == 0


@respx.mock
def test_a_dead_bridge_does_not_break_the_letter(tmp_path):
    conn = _db()
    respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(500, text="boom"))
    s = _settings(tmp_path)
    assert CompanyResearcher(s, conn, BridgeClient(s, retries=0)).for_row(_row(conn)) is None


@respx.mock
def test_only_a_few_companies_are_researched_per_run(tmp_path):
    """Reading the web costs minutes per company — the digest must not wait for a long tail of new employers."""
    conn = _db()
    for i, emp in ((2, "200"), (3, "300")):
        conn.execute("INSERT INTO vacancies(id,hh_id,site,title,employer,employer_id,url,area_name,source,"
                     "search_pass,status,raw_json,first_seen_at,updated_at) VALUES (?,?,'hh','Инженер','ООО',?,"
                     "'u','Москва','s','remote','evaluated','{\"description\":\"d\"}','t','t')", (i, str(i), emp))
    route = _reply(json.dumps(BRIEF, ensure_ascii=False))
    s = _settings(tmp_path, company_research_max_per_run=2)
    r = CompanyResearcher(s, conn, BridgeClient(s, retries=0))

    got = [r.for_row(conn.execute("SELECT * FROM vacancies WHERE id = ?", (i,)).fetchone()) for i in (1, 2, 3)]
    assert [g is not None for g in got] == [True, True, False]
    assert route.call_count == 2
    # the one left out is researched by a later run — its employer simply has no row yet
    assert conn.execute("SELECT COUNT(*) FROM employers").fetchone()[0] == 2


@respx.mock
def test_research_waits_longer_than_an_ordinary_call(tmp_path):
    """A measured research ran 5 minutes; the default 180 s client timeout would have killed it."""
    conn = _db()
    route = _reply(json.dumps(BRIEF, ensure_ascii=False))
    s = _settings(tmp_path)
    CompanyResearcher(s, conn, BridgeClient(s, retries=0)).for_row(_row(conn))
    assert route.calls[0].request.extensions["timeout"]["read"] == s.company_research_timeout_s
    assert s.company_research_timeout_s > s.bridge_timeout_s
