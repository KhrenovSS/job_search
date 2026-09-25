"""v9.15 (decision #55): channel `plant` — companies that run automation without a programmer; quiet hours;
kind-aware "one company — one lead"; contacts from the dossier."""

import json
from datetime import datetime, time

from hh_scout.config import TZ, Settings, in_span, parse_span
from hh_scout.db import connect, migrate, utcnow
from hh_scout.llm.schemas import CompanyBrief, TriageVerdict
from hh_scout.pipeline import dedup, plant, repo


def _conn():
    conn = connect(":memory:")
    migrate(conn)
    return conn


def _card(conn, hh_id, *, employer="Завод Ромашка", employer_id="100", status="skipped", skip_reason="triage",
          note="слесарь КИПиА, обслуживание", lead_kind="vacancy", search_pass="regional", first_seen="2026-09-10T00:00:00+00:00",
          total=None, applied=0):
    conn.execute("INSERT INTO vacancies(hh_id, site, title, employer, employer_id, url, area_name, source, search_pass, lead_kind, "
                 "status, skip_reason, triage_note, applied, first_seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (hh_id, "hh", f"Вакансия {hh_id}", employer, employer_id, "u", "Тула", "s", search_pass, lead_kind,
                  status, skip_reason, note, applied, first_seen, utcnow()))
    vid = conn.execute("SELECT id FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()[0]
    if total is not None:
        conn.execute("INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
                     "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
                     "VALUES (?,80,0,0,80,60,?,'maybe',0,'staff','end_customer','v','p','[]',?)", (vid, total, utcnow()))
    return repo.vacancy_by_id(conn, vid)


def _row(conn, hh_id):
    return conn.execute("SELECT * FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()


S = Settings(_env_file=None)


# --- the pool -------------------------------------------------------------------------------

def test_a_closed_card_of_a_plant_goes_into_the_pool_once_per_company():
    conn = _conn()
    first = _card(conn, "1")
    assert plant.pool(conn, S, first)
    r = _row(conn, "1")
    assert (r["status"], r["skip_reason"], r["lead_kind"], r["search_pass"]) == ("skipped", "plant_pool", "company", "plant")
    assert r["triage_note"] == "слесарь КИПиА, обслуживание"        # the card's reason survives — the card shows it
    twin = _card(conn, "2", note="электромеханик, эксплуатация")
    assert not plant.pool(conn, S, twin)                              # one plant row per company
    assert _row(conn, "2")["skip_reason"] == "triage"
    assert repo.plant_totals(conn) == {"pool": 1, "to_fetch": 0, "sent": 0, "total": 1}


def test_a_company_the_owner_wrote_to_or_that_has_a_lead_is_left_alone():
    conn = _conn()
    _card(conn, "L", status="sent", skip_reason=None, note=None)      # the company already got a lead
    assert not plant.pool(conn, S, _card(conn, "1"))
    conn2 = _conn()
    _card(conn2, "A", status="skipped", skip_reason="applied", note=None, applied=1)   # the owner answered on hh.ru
    assert not plant.pool(conn2, S, _card(conn2, "1"))
    conn3 = _conn()
    assert not plant.pool(conn3, Settings(_env_file=None, company_channels="panel,design,owen_si"), _card(conn3, "1"))


def test_admission_is_a_daily_gate_newest_first_and_drops_covered_companies():
    conn = _conn()
    for i, day in ((1, "2026-09-10"), (2, "2026-09-12"), (3, "2026-09-11")):
        plant.pool(conn, S, _card(conn, str(i), employer=f"Завод {i}", employer_id=str(100 + i), first_seen=f"{day}T00:00:00+00:00"))
    s = Settings(_env_file=None, plant_leads_per_day=2)
    assert plant.admit(conn, s) == 2
    assert _row(conn, "2")["status"] == "to_fetch" and _row(conn, "3")["status"] == "to_fetch"   # the two newest
    assert _row(conn, "2")["triage_priority"] == 3 and _row(conn, "2")["skip_reason"] is None
    assert _row(conn, "1")["skip_reason"] == "plant_pool"
    assert plant.admit(conn, s) == 0                                  # today's quota is spent
    # a company that got a real lead while it waited in the pool is skipped, not opened
    conn2 = _conn()
    plant.pool(conn2, S, _card(conn2, "1"))
    _card(conn2, "V", status="prefiltered", skip_reason=None, note=None)
    assert plant.admit(conn2, S) == 0
    assert _row(conn2, "1")["skip_reason"].startswith("duplicate_employer:")


def test_backfill_reads_the_old_triage_reasons():
    yes = ["слесарь КИПиА, обслуживание руками", "электромонтаж, не программирование", "электромеханик, обслуживание оборудования",
           "мастер электромонтажа, не программирование", "энергетик предприятия", "наладчик станков"]
    no = ["проектировщик АСУ ТП, документация", "дубль той же вакансии другого региона", "продажи", "ПТО, документация и сметы",
          "монтаж ВОЛС, связь без автоматики", "руководитель отдела", "кадровое агентство", "IT-аналитик", "охрана труда", None]
    assert all(plant.note_says_plant(n) for n in yes)
    assert not any(plant.note_says_plant(n) for n in no)

    conn = _conn()
    _card(conn, "1", first_seen="2026-09-10T00:00:00+00:00")
    _card(conn, "2", first_seen="2026-09-12T00:00:00+00:00")          # same company, newer — the one to keep
    _card(conn, "3", employer="Бюро", employer_id="7", note="проектировщик, документация")
    _card(conn, "4", employer="Комбинат", employer_id="8", note="электромонтаж, не программирование")
    dry = plant.backfill(conn, S, dry_run=True)
    assert {r["hh_id"] for r in dry} == {"2", "4"}
    assert _row(conn, "2")["skip_reason"] == "triage"                # dry run changed nothing
    done = plant.backfill(conn, S)
    assert {r["hh_id"] for r in done} == {"2", "4"}
    assert _row(conn, "2")["skip_reason"] == "plant_pool" and _row(conn, "1")["skip_reason"] == "triage"
    assert _row(conn, "3")["skip_reason"] == "triage"


def test_the_triage_verdict_carries_the_flag_but_does_not_require_it():
    assert TriageVerdict(hh_id=1, open=False).plant is False
    assert TriageVerdict(hh_id="1", open=False, plant=True).plant


# --- one company, one lead — by kind (v9.15) ------------------------------------------------

def test_a_plant_offer_does_not_cover_the_companys_real_programmer_vacancy():
    conn = _conn()
    _card(conn, "P", status="sent", skip_reason=None, note=None, lead_kind="company", search_pass="plant")
    vacancy = _card(conn, "V", status="triage", skip_reason=None, note=None)
    assert dedup.covering_lead(conn, S, vacancy) is None             # a vacancy is covered by vacancies only
    another_company_row = _card(conn, "C", status="triage", skip_reason=None, note=None, lead_kind="company", search_pass="panel")
    assert dedup.covering_lead(conn, S, another_company_row)["hh_id"] == "P"   # a company candidate — by anything
    # ...and the "✅ Написал" pressed on the plant card does not close the vacancy channel either
    repo.add_action(conn, _row(conn, "P")["id"], "responded")
    assert dedup.answered_employer(conn, S, vacancy) is None
    assert dedup.answered_employer(conn, S, another_company_row) is not None


def test_a_vacancy_lead_still_covers_the_companys_plant_row():
    conn = _conn()
    _card(conn, "V", status="sent", skip_reason=None, note=None)
    pooled = _card(conn, "P", lead_kind="company", search_pass="plant", skip_reason="plant_pool")
    assert dedup.covering_lead(conn, S, pooled)["hh_id"] == "V"


def test_dedupe_evaluated_keeps_a_vacancy_lead_and_a_company_offer_of_one_employer_apart():
    conn = _conn()
    _card(conn, "V", status="evaluated", skip_reason=None, note=None, total=70)
    _card(conn, "P", status="evaluated", skip_reason=None, note=None, lead_kind="company", search_pass="plant", total=65)
    dedup.dedupe_evaluated(conn, S)
    assert _row(conn, "V")["status"] == "evaluated"
    assert _row(conn, "P")["status"] == "evaluated"                  # two channels, not twins
    # the other way round: the company row scores higher and is seen first — the vacancy must survive too
    conn2 = _conn()
    _card(conn2, "V", status="evaluated", skip_reason=None, note=None, total=70)
    _card(conn2, "P", status="evaluated", skip_reason=None, note=None, lead_kind="company", search_pass="plant", total=80)
    dedup.dedupe_evaluated(conn2, S)
    assert _row(conn2, "V")["status"] == "evaluated" and _row(conn2, "P")["status"] == "evaluated"
    assert dedup.same_company(_row(conn2, "P"), _row(conn2, "V")) is False


# --- the evaluator feeds the pool too (v9.19) ---------------------------------------------------

def _with_page(conn, hh_id):
    conn.execute("UPDATE vacancies SET raw_json = ? WHERE hh_id = ?",
                 (json.dumps({"description": "Завод: линии розлива на ПЛК ОВЕН, обслуживание КИПиА"}), hh_id))
    return _row(conn, hh_id)


def test_an_evaluated_operations_vacancy_of_a_plant_goes_into_the_pool_with_its_page():
    conn = _conn()
    row = _with_page(conn, _card(conn, "E", status="evaluated", skip_reason=None, note=None, total=30)["hh_id"])
    assert plant.pool_evaluated(conn, S, row)
    r = _row(conn, "E")
    assert (r["status"], r["skip_reason"], r["lead_kind"], r["search_pass"]) == ("skipped", "plant_pool", "company", "plant")
    assert "розлива" in json.loads(r["raw_json"])["description"]                    # the page travels with the row
    assert conn.execute("SELECT COUNT(*) FROM evaluations WHERE vacancy_id = ?", (r["id"],)).fetchone()[0] == 0
    # leaving the pool: a row with its page read goes straight to `prefiltered`, a bare card still needs the page
    bare = _card(conn, "B", employer="Завод Лютик", employer_id="200")
    assert plant.pool(conn, S, bare)
    assert plant.admit(conn, S) == 2
    assert _row(conn, "E")["status"] == "prefiltered" and _row(conn, "B")["status"] == "to_fetch"


def test_pool_evaluated_leaves_a_company_with_a_vacancy_lead_alone():
    conn = _conn()
    _card(conn, "V", status="sent", skip_reason=None, note=None, total=80)
    row = _with_page(conn, _card(conn, "E", status="evaluated", skip_reason=None, note=None, total=30)["hh_id"])
    assert not plant.pool_evaluated(conn, S, row)
    assert _row(conn, "E")["status"] == "evaluated"


def test_the_evaluator_pools_a_flagged_plant_below_the_threshold_only(tmp_path):
    import httpx
    import respx
    from hh_scout.llm.bridge_client import BridgeClient
    from hh_scout.llm.evaluator import Evaluator

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("ПРОФИЛЬ", encoding="utf-8")
    (prompts / "vacancy_evaluation.md").write_text("h\n---\nEVAL {feedback_block}", encoding="utf-8")
    s = Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts)
    conn = _conn()
    _with_page(conn, _card(conn, "1", status="prefiltered", skip_reason=None, note=None)["hh_id"])
    _with_page(conn, _card(conn, "2", status="prefiltered", skip_reason=None, note=None, employer="Завод Лютик", employer_id="200")["hh_id"])

    def scored(hh_id, tech, role, lead):
        return {"hh_id": hh_id, "tech_score": tech, "role_score": role, "lead_score": lead, "ip_gph_possible": "maybe",
                "is_agency": False, "company_kind": "end_customer", "verdict": "v", "pitch_hint": "p", "red_flags": [],
                "plant": True}

    with respx.mock:
        respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(200, json={"text": json.dumps(
            [scored("1", 30, 20, 40), scored("2", 80, 70, 60)])}))
        stats = Evaluator(s, conn, BridgeClient(s, sleep=lambda x: None)).run()
    assert stats.evaluated == 2 and stats.pooled == 1
    assert (_row(conn, "1")["status"], _row(conn, "1")["skip_reason"], _row(conn, "1")["search_pass"]) == ("skipped", "plant_pool", "plant")
    assert _row(conn, "2")["status"] == "evaluated"                                    # a real lead stays a lead


def test_the_evaluator_locks_out_a_single_foreign_platform_whatever_the_scores(tmp_path):
    """v9.20, decision #61: «only Siemens (Allen-Bradley, Omron …)» is never a lead — the owner does not sell that."""
    import httpx
    import respx
    from hh_scout.llm.bridge_client import BridgeClient
    from hh_scout.llm.evaluator import Evaluator
    from hh_scout.llm.schemas import VacancyEvaluation

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "candidate_profile.md").write_text("ПРОФИЛЬ", encoding="utf-8")
    (prompts / "vacancy_evaluation.md").write_text("h\n---\nEVAL {feedback_block}", encoding="utf-8")
    s = Settings(_env_file=None, bridge_url="http://bridge.test", bridge_token="t", prompts_dir=prompts)
    conn = _conn()
    for hh_id, emp in (("1", "100"), ("2", "200"), ("3", "300")):
        _with_page(conn, _card(conn, hh_id, status="prefiltered", skip_reason=None, note=None, employer=f"Завод {emp}",
                               employer_id=emp)["hh_id"])

    def scored(hh_id, tech, role, lead, **extra):
        return {"hh_id": hh_id, "tech_score": tech, "role_score": role, "lead_score": lead, "ip_gph_possible": "maybe",
                "is_agency": False, "company_kind": "manufacturer", "verdict": "v", "pitch_hint": "p",
                "red_flags": ["весь стек на Siemens TIA Portal"], **extra}

    with respx.mock:
        respx.post("http://bridge.test/complete").mock(return_value=httpx.Response(200, json={"text": json.dumps([
            scored("1", 65, 85, 65, foreign_platform_only=True),                 # the ЭКОМАШГРУПП case: 70 points, sent before
            scored("2", 80, 70, 60),                                             # a real lead
            scored("3", 20, 20, 40, foreign_platform_only=True, plant=True),     # a Siemens plant: no pool row either
        ])}))
        stats = Evaluator(s, conn, BridgeClient(s, sleep=lambda x: None)).run()
    assert (stats.evaluated, stats.locked_out, stats.pooled) == (3, 2, 0)
    for hh_id in ("1", "3"):
        assert (_row(conn, hh_id)["status"], _row(conn, hh_id)["skip_reason"]) == ("skipped", "foreign_platform_only")
    ev = conn.execute("SELECT e.tech_score, e.red_flags FROM evaluations e JOIN vacancies v ON v.id = e.vacancy_id "
                      "WHERE v.hh_id = '1'").fetchone()
    assert ev["tech_score"] == 65 and "Siemens" in ev["red_flags"]                 # the scores stay for the record
    assert _row(conn, "2")["status"] == "evaluated"
    assert [r["hh_id"] for r in repo.lead_queue(conn, 50)] == ["2"]
    assert VacancyEvaluation(hh_id="x", tech_score=1, role_score=1, lead_score=1, ip_gph_possible="maybe",
                             verdict="v").foreign_platform_only is False        # older answers without the field stay valid


# --- quiet hours ------------------------------------------------------------------------------

def test_quiet_hours_cross_midnight():
    span = parse_span("23:00-07:00")
    assert in_span(time(23, 30), span) and in_span(time(3, 0), span) and in_span(time(6, 59), span)
    assert not in_span(time(7, 0), span) and not in_span(time(12, 0), span) and not in_span(time(22, 59), span)
    assert in_span(time(13, 0, tzinfo=TZ), parse_span("12:00-15:00"))
    assert parse_span("") is None and not in_span(time(3, 0), None)


def test_messages_are_silent_at_night():
    from hh_scout.bot.digest import silent_now

    s = Settings(_env_file=None, quiet_hours="23:00-07:00")
    assert silent_now(s, datetime(2026, 9, 22, 3, 0, tzinfo=TZ))
    assert not silent_now(s, datetime(2026, 9, 22, 12, 0, tzinfo=TZ))
    assert not silent_now(Settings(_env_file=None, quiet_hours=""), datetime(2026, 9, 22, 3, 0, tzinfo=TZ))


# --- contacts from the dossier ----------------------------------------------------------------

def test_company_card_takes_contacts_from_the_dossier_when_the_card_has_none():
    from hh_scout.pipeline.ranker import _dossier_contacts

    brief = CompanyBrief(found=True, what_they_do="льют чугун", website="https://zavod.ru", contact_email="info@zavod.ru")
    assert brief.contact_phone == ""                                  # the new fields are optional
    assert CompanyBrief().website == ""
    row = {"company_brief": brief.model_dump_json()}
    assert _dossier_contacts(row) == ["https://zavod.ru", "info@zavod.ru"]
    assert _dossier_contacts({"company_brief": None}) == []
    assert _dossier_contacts({"company_brief": json.dumps({"what_they_do": "x"})}) == []
