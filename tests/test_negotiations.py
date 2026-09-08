from hh_scout.browser.hh_pages import parse_negotiations, parse_suitable

STATE = {
    "userType": "applicant",
    "applicantNegotiations": {
        "topicList": [
            {"id": 1, "vacancyId": 136531698, "lastState": "INTERVIEW", "initialState": "RESPONSE",
             "conversationMessagesCount": 4, "hasNewMessages": False, "archived": False},
            {"id": 2, "vacancyId": 136067468, "lastState": "RESPONSE", "conversationMessagesCount": 1, "hasNewMessages": False},
            {"id": 3, "vacancyId": 136935570, "lastState": "DISCARD", "conversationMessagesCount": 2},
            {"id": 4, "lastState": "RESPONSE"},  # no vacancyId -> ignored
        ],
        "total": 3, "paging": None,
    },
    "vacanciesShort": {"vacanciesList": [
        {"vacancyId": 136531698, "name": "Инженер АСУ ТП", "company": {"visibleName": "ООО Ромашка"}},
    ], "total": 1},
    "suitableVacancies": {"resultsFound": 1581, "vacancies": [
        {"vacancyId": 137053536, "name": "Инженер-программист АСУТП", "company": {"name": "ТЕКОН"},
         "area": {"name": "Москва"}, "workFormats": [{"workFormatsElement": ["ON_SITE"]}], "employmentForm": "FULL",
         "compensation": {"noCompensation": {}}, "publicationTime": {"$": "2026-09-08T10:00:00+03:00"}},
    ]},
}


def test_parse_negotiations_real_shape():
    items = {n.hh_id: n for n in parse_negotiations(STATE)}
    assert set(items) == {"136531698", "136067468", "136935570"}
    assert items["136531698"].state == "INTERVIEW" and items["136531698"].has_messages is True
    assert items["136531698"].title == "Инженер АСУ ТП" and items["136531698"].employer == "ООО Ромашка"
    assert items["136067468"].has_messages is False  # only the owner's own message
    assert items["136935570"].state == "DISCARD" and items["136935570"].has_messages is True


def test_parse_suitable_cards():
    cards = parse_suitable(STATE)
    assert len(cards) == 1
    c = cards[0]
    assert c.hh_id == "137053536" and c.employer == "ТЕКОН" and c.work_format == "office"
    assert parse_suitable({"foo": 1}) == []
