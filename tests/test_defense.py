"""v9.33, decision #72: defence enterprises — and holdings with a defence wing — are never leads."""

import json

import httpx
import respx

from hh_scout.config import Settings
from hh_scout.db import connect, migrate
from hh_scout.llm.bridge_client import BridgeClient
from hh_scout.pipeline import defense, dedup, repo
from hh_scout.pipeline.prefilter import CardFacts, decide, run


def _conn():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _row(conn, hh_id, *, title="Инженер-программист ПЛК", employer="Завод", employer_id=None, status="new", reason=None,
         site="hh", lead_kind="vacancy", search_pass="regional"):
    conn.execute("INSERT INTO vacancies(hh_id,site,lead_kind,title,employer,employer_id,url,source,search_pass,status,skip_reason,"
                 "first_seen_at,updated_at) VALUES (?,?,?,?,?,?,'u','s',?,?,?,datetime('now'),'t')",
                 (hh_id, site, lead_kind, title, employer, employer_id, search_pass, status, reason))
    return conn.execute("SELECT * FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()


def _state(conn):
    return {r["hh_id"]: (r["status"], r["skip_reason"]) for r in conn.execute("SELECT * FROM vacancies")}


# --- the name rule -----------------------------------------------------------------------------

def test_match_knows_holdings_stems_and_capital_abbreviations_but_not_lookalikes():
    assert defense.match("АО Концерн ВКО «Алмаз-Антей»") == "алмаз-антей"
    assert defense.match("Госкорпорация Ростех") == "ростех"
    assert defense.match("Филиал ПАО ОАК Нижегородский Авиастроительный Завод Сокол") == "ОАК"
    assert defense.match("Войсковая часть 12345") == "войсковая часть"
    assert defense.match("в/ч 12345") == "в/ч "
    assert defense.match("Тульский патронный завод") == "патронн"
    assert defense.match("ФГБУ «48 ЦНИИ» Минобороны России") == "минобороны"
    assert defense.match("Отделение вневедомственной охраны — филиал ФГКУ УВО войск национальной гвардии") == "войск национальной гвардии"
    assert defense.match("АО «ЗЕЛЕНОДОЛЬСКИЙ ЗАВОД ИМЕНИ А.М. ГОРЬКОГО»") == "зеленодольский завод"   # ё = е, case
    # lookalikes stay leads
    for name in ("ООО РТР", "Росконтроль", "ООО Оск-Сервис", "Ростехнадзор", "РосТехЭнерго-Воронеж", "Иркутская нефтяная компания",
                 "Газпром добыча Иркутск", "СпецСтройМашина", "Самооборона-Сервис", "Курганский машиностроительный завод",
                 "ОмегаПром-Энерго", None, ""):
        assert defense.match(name) is None, name
    assert defense.is_defense_reason("defense:name:ростех") and defense.is_defense_reason("defense")
    assert not defense.is_defense_reason("foreign_platform_only") and not defense.is_defense_reason(None)


def test_decide_closes_a_defence_employer_before_the_title_rules():
    card = CardFacts(hh_id="1", title="Инженер-программист ПЛК", applied=False, archived=False, employer="ПАО «Иркут»")
    assert decide(card) == "defense:name:иркут"
    # the region still wins (both are hard rules; the region one came first) and a civil employer passes
    assert decide(CardFacts(hh_id="1", title="Инженер-программист ПЛК", applied=False, archived=False, employer="ПАО «Иркут»",
                            region="Республика Крым")) == "region:Республика Крым"
    assert decide(CardFacts(hh_id="1", title="Инженер-программист ПЛК", applied=False, archived=False, employer="Водоканал")) is None


def test_the_rules_stage_sweeps_every_source_and_stage_and_propagates_to_the_company():
    conn = _conn()
    _row(conn, "1", employer="Концерн ВКО Алмаз - Антей")                                   # a new card → rule
    _row(conn, "2", employer="Водоканал")                                                     # passes
    _row(conn, "3", employer="Уралвагонзавод", status="evaluated")                            # queued → withdrawn
    _row(conn, "4", employer="Оборонэнерго", status="rejected")                               # written off → reason only
    _row(conn, "5", employer="Севмаш", status="skipped", reason="plant_pool")                 # plant pool → withdrawn
    _row(conn, "6", employer="Севмаш", status="sent")                                         # history stays
    _row(conn, "7", employer="Севмаш", status="skipped", reason="triage")                     # already skipped otherwise
    _row(conn, "tv:1", site="trudvsem", employer="ООО «Швабе-Москва»", status="prefiltered")  # another source
    _row(conn, "owen:1", site="owen", lead_kind="company", employer="НПО Сплав", search_pass="owen_si")
    _row(conn, "8", employer="Завод Икс", employer_id="500", status="to_fetch")              # marked by the evaluator …
    _row(conn, "9", employer="Завод Икс", employer_id="500", status="skipped", reason="defense:evaluation")
    _row(conn, "10", employer="Завод Икс (филиал)", employer_id="500", status="evaluated")   # … so its twins follow
    _row(conn, "profi:1", site="profi", employer="Иван", status="evaluated")                  # profi: a person, never
    outcomes = run(conn, Settings(_env_file=None))
    assert outcomes["defense"] == 1 and outcomes["passed"] == 1
    st = _state(conn)
    assert st["1"] == ("skipped", "defense:name:концерн вко")
    assert st["2"] == ("triage", None)
    assert st["3"] == ("skipped", "defense:name:уралвагонзавод")
    assert st["4"] == ("rejected", "defense:name:оборонэнерго")
    assert st["5"] == ("skipped", "defense:name:севмаш")
    assert st["6"] == ("sent", None)
    assert st["7"] == ("skipped", "triage")
    assert st["tv:1"] == ("skipped", "defense:name:швабе")
    assert st["owen:1"] == ("skipped", "defense:name:нпо сплав")
    assert st["8"] == ("skipped", "defense:employer:9")
    assert st["10"] == ("skipped", "defense:employer:9")
    assert st["profi:1"] == ("evaluated", None)
    assert repo.floor_candidates(conn, threshold=50, min_total=40, min_role=40, lookback_days=3) == []
    assert repo.skip_defense_employers(conn) == {}                                          # idempotent
    # undo by family
    with conn:
        n = repo.requeue_skipped(conn, "defense%", 30)
    assert n == 8 and _state(conn)["3"] == ("triage", None)


def test_the_per_row_hook_skips_a_defence_name_and_a_twin_of_a_marked_company():
    conn = _conn()
    s = Settings(_env_file=None)
    by_name = _row(conn, "1", employer="АО «Туполев»", status="to_fetch")
    assert dedup.skip_if_covered(conn, s, by_name) is not None
    _row(conn, "2", employer="Завод Игрек", employer_id="700", status="skipped", reason="defense:dossier")
    twin = _row(conn, "3", employer="Завод Игрек", employer_id="700", status="to_fetch")
    marked = dedup.skip_if_covered(conn, s, twin)
    assert marked is not None and marked["hh_id"] == "2"
    free = _row(conn, "4", employer="Водоканал", employer_id="800", status="to_fetch")
    assert dedup.skip_if_covered(conn, s, free) is None
    st = _state(conn)
    assert st["1"] == ("skipped", "defense:name:туполев") and st["3"] == ("skipped", "defense:employer:2")
    assert st["4"] == ("to_fetch", None)


# --- the model flags ---------------------------------------------------------------------------

def test_triage_closes_a_flagged_card_whatever_open_says_for_vacancies_and_companies(tmp_path):
    from hh_scout.llm.triage import Triager

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("П", encoding="utf-8")
    for name in ("card_triage.md", "company_triage.md"):
        (prompts / name).write_text("h\n---\nSYSTEM {candidate_profile}", encoding="utf-8")
    s = Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts, triage_batch_size=5)
    conn = _conn()
    _row(conn, "1", employer="Завод А", status="triage")
    _row(conn, "2", employer="КБ Б", status="triage")
    _row(conn, "3", employer="Щиты В", status="triage", lead_kind="company", search_pass="panel")
    answers = {
        "vacancy": [{"hh_id": "1", "open": True, "priority": 1, "reason": "ядро"},
                    {"hh_id": "2", "open": True, "priority": 1, "reason": "КБ вооружений", "defense": True, "plant": True}],
        "company": [{"hh_id": "3", "open": True, "priority": 1, "reason": "щиты для верфи ОСК", "defense": True}],
    }

    def reply(req):
        body = json.loads(req.content)
        kind = "company" if '"kind": "company"' in body["messages"][0]["content"] or "panel" in body["messages"][0]["content"] else "vacancy"
        return httpx.Response(200, json={"text": json.dumps(answers[kind]), "usage": {}, "cost_usd": 0.0})

    with respx.mock:
        respx.post("http://bridge.test/complete").mock(side_effect=reply)
        stats = Triager(s, conn, BridgeClient(s, sleep=lambda x: None)).run()
    assert stats.defense == 2 and stats.pooled == 0
    st = _state(conn)
    assert st["1"] == ("to_fetch", None)
    assert st["2"] == ("skipped", "defense:triage")
    assert st["3"] == ("skipped", "defense:triage")


def test_the_evaluator_locks_out_a_defence_enterprise_whatever_the_scores_for_vacancies_and_companies(tmp_path):
    from hh_scout.llm.evaluator import Evaluator
    from hh_scout.llm.schemas import CompanyEvaluation, VacancyEvaluation

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("П", encoding="utf-8")
    (prompts / "vacancy_evaluation.md").write_text("h\n---\nEVAL {feedback_block}", encoding="utf-8")
    (prompts / "company_evaluation.md").write_text("h\n---\nCOMPANY {feedback_block}", encoding="utf-8")
    s = Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts)
    conn = _conn()
    for hh_id, emp in (("1", "100"), ("2", "200")):
        _row(conn, hh_id, employer=f"Завод {emp}", employer_id=emp, status="prefiltered")
    _row(conn, "3", employer="Щиты 300", employer_id="300", status="prefiltered", lead_kind="company", search_pass="panel")
    conn.execute("UPDATE vacancies SET raw_json = ?", (json.dumps({"description": "описание"}),))

    def scored(hh_id, tech, role, lead, **extra):
        return {"hh_id": hh_id, "tech_score": tech, "role_score": role, "lead_score": lead, "ip_gph_possible": "maybe",
                "is_agency": False, "company_kind": "manufacturer", "verdict": "v", "pitch_hint": "p",
                "red_flags": ["предприятие ОПК"], **extra}

    def reply(req):
        body = json.loads(req.content)
        if body["system_text"].startswith("COMPANY"):
            text = json.dumps([{"hh_id": "3", "fit_score": 90, "lead_score": 80, "company_kind": "panel_builder", "verdict": "v",
                                "pitch_hint": "p", "offer_focus": ["templates"], "red_flags": [], "defense_enterprise": True}])
        else:
            text = json.dumps([scored("1", 90, 90, 90, defense_enterprise=True), scored("2", 80, 70, 60)])
        return httpx.Response(200, json={"text": text, "usage": {}, "cost_usd": 0.0})

    with respx.mock:
        respx.post("http://bridge.test/complete").mock(side_effect=reply)
        stats = Evaluator(s, conn, BridgeClient(s, sleep=lambda x: None)).run()
    assert (stats.evaluated, stats.defense, stats.locked_out, stats.pooled) == (3, 2, 0, 0)
    st = _state(conn)
    assert st["1"] == ("skipped", "defense:evaluation") and st["3"] == ("skipped", "defense:evaluation")
    assert st["2"] == ("evaluated", None)
    assert conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 3          # the scores stay for the record
    assert [r["hh_id"] for r in repo.lead_queue(conn, 50)] == ["2"]
    assert VacancyEvaluation(hh_id="x", tech_score=1, role_score=1, lead_score=1, ip_gph_possible="maybe",
                             verdict="v").defense_enterprise is False                  # older answers stay valid
    assert CompanyEvaluation(hh_id="x", fit_score=1, lead_score=1, verdict="v").defense_enterprise is False


# --- the dossier and the send-side guard -------------------------------------------------------

def _queued(conn, hh_id, employer, *, brief=None):
    row = _row(conn, hh_id, employer=employer, employer_id="e" + hh_id, status="evaluated")
    conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                 "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                 "VALUES (?,80,0,0,80,60,76,'maybe',0,'unknown','integrator','v','p','[]','t')", (row["id"],))
    conn.execute("INSERT INTO cover_letters(vacancy_id,text,created_at) VALUES (?,'письмо','t')", (row["id"],))
    if brief is not None:
        conn.execute("INSERT INTO employers(employer_id,name,found,brief,sources,researched_at) VALUES (?,?,1,?,'[]','t')",
                     ("e" + hh_id, employer, json.dumps(brief)))
    return row


def test_the_letter_stage_skips_a_company_the_dossier_calls_a_defence_enterprise(tmp_path):
    from hh_scout.llm.cover_letter import CoverLetterWriter
    from hh_scout.llm.schemas import CompanyBrief

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name, text in (("candidate_profile.md", "П"), ("resume.md", "Р"), ("cover_letter.md", "h\n---\nR {resume}"),
                       ("company_offer.md", "h\n---\nO {resume}"), ("letter_review.md", "h\n---\nREVIEW")):
        (prompts / name).write_text(text, encoding="utf-8")
    s = Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts,
                 company_research_enabled=False)
    conn = _conn()
    row = repo.lead_by_hh_id(conn, _queued(conn, "1", "Завод Зет")["hh_id"])
    w = CoverLetterWriter(s, conn, BridgeClient(s, sleep=lambda x: None))
    assert w.defense(row, CompanyBrief(found=True, what_they_do="насосы").model_dump()) is False
    assert w.defense(row, CompanyBrief(found=True, what_they_do="входит в Ростех", defense=True).model_dump()) is True
    assert w.stats.defense == 1
    assert _state(conn)["1"] == ("skipped", "defense:dossier")
    sent = repo.lead_by_hh_id(conn, _queued(conn, "2", "Завод Ю")["hh_id"])
    conn.execute("UPDATE vacancies SET status = 'sent' WHERE hh_id = '2'")
    sent = repo.lead_by_hh_id(conn, "2")
    assert w.defense(sent, CompanyBrief(found=True, defense=True).model_dump()) is False   # a /letter rewrite never skips
    assert CompanyBrief().defense is False


def test_the_send_side_guard_drops_defence_leads_from_the_queue():
    from hh_scout.pipeline.digest_builder import plan_digest, skip_defense

    s = Settings(_env_file=None, bridge_url="http://b", bridge_token="t", score_threshold=50)
    conn = _conn()
    _queued(conn, "1", "Водоканал")
    _queued(conn, "2", "АО «Севмаш»")                                                  # by name
    _queued(conn, "3", "Завод Дельта", brief={"found": True, "defense": True})           # by dossier
    _queued(conn, "4", "Завод Эпсилон", brief={"found": True, "defense": False})
    assert skip_defense(conn, s) == 2
    assert skip_defense(conn, s) == 0
    assert {r["hh_id"] for r in plan_digest(conn, s).leads} == {"1", "4"}
    st = _state(conn)
    assert st["2"] == ("skipped", "defense:name:севмаш") and st["3"] == ("skipped", "defense:dossier")


# --- the sources without hh.ru ------------------------------------------------------------------

def test_a_procurement_for_a_defence_customer_is_remembered_but_never_resolved_and_a_defence_winner_is_skipped():
    from hh_scout.sources import zakupki

    conn = _conn()
    s = Settings(_env_file=None, zakupki_enabled=True, zakupki_request_gap_s=0)
    mil = zakupki.Notice(reg_number="1", law="44", url="u1", notice_type="ea20", title="Работы по внедрению АСУ ТП котельной",
                         customer="ФКУ «Войсковая часть 55555» Министерства обороны РФ", price="1", stage="", published=None)
    civ = zakupki.Notice(reg_number="2", law="44", url="u2", notice_type="ea20", title="Работы по внедрению SCADA",
                         customer="МУП «Водоканал»", price="1", stage="", published=None)
    assert zakupki.store_notice(conn, mil, "SCADA") and zakupki.store_notice(conn, civ, "SCADA")
    st = _state(conn)
    assert st["zk:1"] == ("skipped", "defense:customer") and st["zk:2"] == ("new", None)

    class Fetcher:
        def __init__(self):
            self.urls = []

        def get(self, url, params=None):
            self.urls.append(url)
            raise AssertionError("no request expected")

    # a notice stored before the rule: the customer check comes before any request
    conn.execute("UPDATE vacancies SET status = 'new', skip_reason = NULL WHERE hh_id = 'zk:1'")
    row = conn.execute("SELECT * FROM vacancies WHERE hh_id = 'zk:1'").fetchone()
    f = Fetcher()
    assert zakupki.resolve_row(conn, s, f, row) == "skipped" and f.urls == []
    assert _state(conn)["zk:1"] == ("skipped", "defense:customer")
    # the winner itself is a defence plant: the name is kept, the lead is not
    results = zakupki.parse_supplier_results
    real_card = zakupki.parse_contract_card
    zakupki.parse_supplier_results = lambda html: [("77", 'АО "НПО СПЛАВ" ИМ. А.Н. ГАНИЧЕВА')]
    zakupki.parse_contract_card = lambda page: None
    try:
        class Ok:
            def get(self, url, params=None):
                return ""
        row = conn.execute("SELECT * FROM vacancies WHERE hh_id = 'zk:2'").fetchone()
        assert zakupki.resolve_row(conn, s, Ok(), row) == "skipped"
    finally:
        zakupki.parse_supplier_results = results
        zakupki.parse_contract_card = real_card
    r = conn.execute("SELECT * FROM vacancies WHERE hh_id = 'zk:2'").fetchone()
    assert (r["status"], r["skip_reason"]) == ("skipped", "defense:name:нпо сплав") and "СПЛАВ" in r["employer"]


def test_catalogue_and_portal_rows_of_defence_companies_land_as_skipped_on_insert():
    from hh_scout.sources import owen, trudvsem

    conn = _conn()
    s = Settings(_env_file=None)
    cards = [owen.IntegratorCard(tag_id="1", name="АО «Концерн ВКО «Алмаз-Антей»", site=None, city="Москва", region="Москва",
                                 status="Партнёр", projects_url=None, address=None, description="", emails=("info@x.ru",)),
             owen.IntegratorCard(tag_id="2", name="ООО «Автоматика-Сервис»", site=None, city="Тула", region="Тульская",
                                 status="Партнёр", projects_url=None, address=None, description="", emails=("a@y.ru",))]
    assert owen.store(conn, cards) == 2
    st = _state(conn)
    assert st["owen:1"] == ("skipped", "defense:name:алмаз-антей") and st["owen:2"] == ("new", None)
    v = trudvsem.TvVacancy(ext_id="tv:1", title="Инженер-программист ПЛК", employer='АО "УРАЛВАГОНЗАВОД"', employer_id="tv:1",
                           url="u", company_url=None, region="Свердловская область", city="Нижний Тагил", inn="1", ogrn="1",
                           hr_agency=False, emails=["hr@uvz.ru"])
    assert trudvsem.store_vacancy(conn, s, v, "плк") == "skipped"
    assert _state(conn)["tv:1"] == ("skipped", "defense:name:уралвагонзавод")
