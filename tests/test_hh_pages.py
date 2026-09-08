import pytest

from hh_scout.browser.hh_pages import (
    PageFormatError,
    build_search_url,
    extract_initial_state,
    normalize_employment,
    normalize_work_format,
    parse_negotiations,
    parse_search,
    parse_vacancy,
    strip_html,
    user_type,
    vacancy_url,
)


def test_extract_initial_state_missing():
    assert extract_initial_state("<html><body>nothing</body></html>") is None
    assert extract_initial_state('<template id="HH-Lux-InitialState">not json</template>') is None


def test_search_page_parses_cards(search_state):
    assert search_state is not None
    sp = parse_search(search_state)
    assert sp.total == 10 and len(sp.cards) == 10
    assert sp.has_next is False and sp.page == 0
    assert sp.user_type == "anonymous"
    first = sp.cards[0]
    assert first.hh_id == "136519902"
    assert first.title == "Инженер-программист АСУ ТП (PLC, HMI, частотные преобразователи)"  # nbsp/‑ normalised
    assert first.employer == "Амперика"
    assert first.area_name == "Москва"
    assert first.work_format == "office" and first.employment == "full"
    assert first.compensation["from"] == 140000 and first.compensation["gross"] is True
    assert first.published_at.startswith("2026-09-07T17:03:08")
    assert first.url == "https://hh.ru/vacancy/136519902"
    assert first.applied is False and first.archived is False
    ids = {c.hh_id for c in sp.cards}
    assert len(ids) == 10
    assert any(c.employment == "part" for c in sp.cards)  # SIDE_JOB -> part


def test_search_page_criteria_echo(search_state):
    sp = parse_search(search_state)
    assert sp.criteria.get("text") == "CODESYS"
    assert sp.criteria.get("area") == [1]


def test_has_next_from_pages_list():
    state = {"vacancySearchResult": {"vacancies": [], "criteria": {"page": 0},
             "paging": {"pages": [{"page": 0, "selected": True}, {"page": 1}], "next": {"page": 1, "disabled": False}}}}
    assert parse_search(state).has_next is True
    state["vacancySearchResult"]["paging"] = {"pages": [{"page": 0, "selected": True}], "next": {"page": 1, "disabled": True}}
    assert parse_search(state).has_next is False
    state["vacancySearchResult"]["paging"] = None
    assert parse_search(state).has_next is False


def test_user_labels_map_marks_applied():
    state = {"vacancySearchResult": {"vacancies": [{"vacancyId": 5, "name": "X", "userLabels": []}], "criteria": {}},
             "userLabelsForVacancies": {"5": ["RESPONDED"]}}
    assert parse_search(state).cards[0].applied is True


def test_parse_search_wrong_page(vacancy_state):
    with pytest.raises(PageFormatError):
        parse_search(vacancy_state)


def test_vacancy_page(vacancy_state):
    d = parse_vacancy(vacancy_state)
    assert d.hh_id == "136519902"
    assert d.employer == "Амперика" and d.area_name == "Москва"
    assert d.work_format == "office" and d.employment == "full"
    assert d.archived is False and d.applied is False and d.closed_for_applicants is False
    assert "CODESYS" in d.key_skills and "SCADA" in d.key_skills
    assert "<" not in d.description_text and len(d.description_text) > 1000
    assert "АМПЕРИКА" in d.description_text
    assert d.published_at.startswith("2026-09-07")
    with pytest.raises(PageFormatError):
        parse_vacancy({"foo": 1})


def test_normalizers():
    assert normalize_work_format([{"workFormatsElement": ["ON_SITE", "REMOTE", "HYBRID"]}]) == "remote"
    assert normalize_work_format(["HYBRID", "ON_SITE"]) == "hybrid"
    assert normalize_work_format(["FIELD_WORK"]) == "field"
    assert normalize_work_format(None) == "unknown"
    assert normalize_employment("PROJECT") == "project"
    assert normalize_employment({"@type": "FULL"}) == "full"
    assert normalize_employment("WEIRD") == "unknown"


def test_clean_text():
    from hh_scout.browser.hh_pages import clean_text
    assert clean_text("Инженер\u2011программист АСУ\xa0ТП  x") == "Инженер-программист АСУ ТП x"
    assert clean_text(None) == ""


def test_strip_html():
    assert strip_html("<p>Привет,&nbsp;мир</p><ul><li>раз</li><li>два</li></ul>") == "Привет, мир\nраз\nдва"
    assert strip_html(None) == ""


def test_build_search_url():
    url = build_search_url('("АСУ ТП" OR PLC)', areas=[1, 2019], work_formats=["REMOTE"],
                           employment_forms=["PROJECT", "PART"], period_days=2, page=1, items_on_page=50)
    assert url.startswith("https://hh.ru/search/vacancy?text=")
    assert "area=1&area=2019" in url and "work_format=REMOTE" in url
    assert "employment_form=PROJECT&employment_form=PART" in url
    assert "search_period=2" in url and "items_on_page=50" in url and "page=1" in url
    assert "no_magic=true" in url and "order_by=publication_time" in url
    assert "&page=" not in build_search_url("x")
    assert vacancy_url(42) == "https://hh.ru/vacancy/42"


def test_parse_negotiations_tolerant():
    state = {"negotiations": {"list": [
        {"negotiationId": 1, "vacancy": {"vacancyId": 100, "name": "A"}, "state": {"id": "RESPONSE"}, "hasNewMessages": False},
        {"topicId": 2, "vacancy": {"id": 200}, "status": "INVITATION", "messagesCount": 3},
    ]}}
    items = {n.hh_id: n for n in parse_negotiations(state)}
    assert set(items) == {"100", "200"}
    assert items["100"].state == "RESPONSE" and items["100"].has_messages is False
    assert items["200"].state == "INVITATION" and items["200"].has_messages is True
    assert parse_negotiations({"nothing": []}) == []
    assert user_type({"userType": "applicant"}) == "applicant"
