"""What the model is told about the contract form.

Before v8.2 the ГПХ/ИП verdict came only from the description text, which almost never mentions it.
These tests pin the facts hh itself provides all the way to each prompt payload.
"""

import json

from hh_scout.db import connect, migrate
from hh_scout.llm.cover_letter import letter_payload
from hh_scout.llm.evaluator import vacancy_payload
from hh_scout.llm.triage import card_payload

CONTRACTS = ["INDIVIDUAL_ENTREPRENEUR", "SELF_EMPLOYED"]


def _conn():
    conn = connect(":memory:")
    migrate(conn)
    conn.execute(
        "INSERT INTO vacancies(id,hh_id,title,employer,url,area_name,work_format,employment,accept_temporary,"
        "civil_law_contracts,source,search_pass,status,raw_json,first_seen_at,updated_at) "
        "VALUES (1,'1','Программист ПЛК','ООО','u','Москва','remote','project',1,?,'s','gph','evaluated',?,'t','t')",
        (json.dumps(CONTRACTS), '{"description": "<p>Нужен ПЛК</p>", "keySkills": {"keySkill": ["CODESYS"]}}'))
    conn.execute(
        "INSERT INTO vacancies(id,hh_id,site,title,employer,url,source,search_pass,status,raw_json,first_seen_at,updated_at) "
        "VALUES (2,'profi:9','profi','Наладить ПЛК','Сергей','u','profi','profi','evaluated',?,'t','t')",
        ('{"description": "нужен ПЛК", "budget": "10 000 ₽"}',))
    conn.execute(
        "INSERT INTO evaluations(vacancy_id,tech_score,salary_score,format_score,role_score,lead_score,total,"
        "ip_gph_possible,is_agency,employment_hint,company_kind,verdict,pitch_hint,red_flags,created_at) "
        "VALUES (1,80,0,0,80,70,80,'yes',0,'project','integrator','нужен программист','предложить','[]','t')")
    return conn


def _row(conn, vacancy_id):
    return conn.execute("SELECT v.*, e.verdict, e.pitch_hint, e.company_kind, e.ip_gph_possible, e.employment_hint "
                        "FROM vacancies v LEFT JOIN evaluations e ON e.vacancy_id = v.id WHERE v.id = ?",
                        (vacancy_id,)).fetchone()


def test_triage_card_carries_hh_contract_facts():
    row = _row(_conn(), 1)
    payload = card_payload(row)
    assert payload["accept_temporary"] is True
    assert payload["civil_law_contracts"] == CONTRACTS
    assert payload["search_pass"] == "gph"


def test_evaluation_payload_carries_hh_contract_facts():
    conn = _conn()
    payload = vacancy_payload(_row(conn, 1))
    assert payload["accept_temporary"] is True
    assert payload["civil_law_contracts"] == CONTRACTS
    assert payload["search_pass"] == "gph"  # the pass that found it was never passed to the evaluator before v8.2

    # a profi.ru order has no hh contract fields: "accept_temporary: false" there would read as a denial
    order = vacancy_payload(_row(conn, 2))
    assert "accept_temporary" not in order and "civil_law_contracts" not in order
    assert order["kind"] == "order"


def test_letter_payload_carries_hh_contract_facts():
    payload = letter_payload(_row(_conn(), 1))
    assert payload["accept_temporary"] is True
    assert payload["civil_law_contracts"] == CONTRACTS
