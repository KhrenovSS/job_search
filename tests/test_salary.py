from hh_scout.hh.salary import normalize


def test_gross_month_rur_converted_to_net():
    s = normalize({"from": 100000, "to": 200000, "currencyCode": "RUR", "gross": True, "mode": "MONTH"})
    assert (s.from_net, s.to_net) == (87000, 174000)
    assert s.note is None and s.stated
    assert s.human() == "87–174 тыс. ₽ на руки"


def test_net_kept_and_open_ranges():
    assert normalize({"from": 160000, "currencyCode": "RUR", "gross": False}).human() == "от 160 тыс. ₽ на руки"
    s = normalize({"to": 150000, "currencyCode": "RUR", "gross": False})
    assert s.from_net is None and s.to_net == 150000 and s.human() == "до 150 тыс. ₽ на руки"


def test_not_stated():
    for raw in (None, {}, {"noCompensation": {}}, {"currencyCode": "RUR", "gross": True}):
        s = normalize(raw)
        assert not s.stated and s.human() == "зарплата не указана"


def test_foreign_currency_and_hourly_not_converted():
    s = normalize({"from": 3000, "to": 4000, "currencyCode": "USD", "gross": True, "mode": "MONTH"})
    assert (s.from_net, s.to_net) == (3000, 4000) and "USD" in s.note
    assert s.human() == "3 000–4 000 USD (валюта USD, не пересчитано)"
    assert normalize({"from": 2500, "currencyCode": "USD", "gross": False}).human() == "от 2 500 USD (валюта USD, не пересчитано)"
    h = normalize({"from": 2000, "currencyCode": "RUR", "gross": True, "mode": "HOUR"})
    assert h.from_net == 2000 and "hour" in h.note
