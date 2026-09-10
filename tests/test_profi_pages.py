from datetime import datetime
from pathlib import Path

import pytest

from hh_scout.config import TZ
from hh_scout.profi.pages import ProfiBlocked, parse_budget, parse_orders, parse_posted

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 10, 7, 12, tzinfo=TZ)


@pytest.fixture(scope="session")
def feed_html() -> str:
    return (FIXTURES / "profi_orders.html").read_text(encoding="utf-8")


def test_parse_orders_reads_both_cards_with_all_fields(feed_html):
    orders = parse_orders(feed_html, NOW)
    assert [o.order_id for o in orders] == ["93716145", "93638725"]
    a, b = orders
    assert a.ext_id == "profi:93716145" and a.url == "https://profi.ru/backoffice/n.php?o=93716145"
    assert a.title == "Программирование овен"
    assert a.description.startswith("Имеется готовая программа") and "ИПП120 будет slave" in a.description
    assert "Пожелания и особенности" not in a.description
    assert a.budget_text == "до 5000 ₽" and (a.budget_from, a.budget_to) == (None, 5000)
    assert a.work_format == "remote" and a.city == "Москва"
    assert a.when == "9 сен. (Ср)"
    assert a.client == "Клиент А" and a.posted_text == "Вчера в 12:09"
    assert a.published_at == datetime(2026, 9, 9, 12, 9, tzinfo=TZ).isoformat()
    assert b.title.startswith("Программирование программируемых логических контроллеров")
    assert b.budget_text == "до 30 000 ₽" and b.budget_to == 30000
    assert b.city == "Калуга" and b.when == "7 сен. (Пн) - 13 сен. (Вс)"
    assert b.client == "Клиент Б" and b.published_at == datetime(2026, 9, 7, 12, 0, tzinfo=TZ).isoformat()


def test_empty_feed_is_fine_but_a_login_page_is_blocked():
    empty = '<html><body><nav><a>Анкета</a></nav><div>Вы посмотрели все новые заказы в вашем районе</div></body></html>'
    assert parse_orders(empty, NOW) == []
    login = '<html><body><h1>Вход</h1><form>Введите номер телефона<input></form></body></html>'
    with pytest.raises(ProfiBlocked):
        parse_orders(login, NOW)
    with pytest.raises(ProfiBlocked):
        parse_orders("<html><body>что-то совсем другое</body></html>", NOW)


def test_parse_budget_variants():
    assert parse_budget("до 5000 ₽") == (None, 5000)
    assert parse_budget("от 10 000 ₽") == (10000, None)
    assert parse_budget("30 000 ₽") == (30000, 30000)
    assert parse_budget("5 000–8 000 ₽") == (5000, 8000)
    assert parse_budget("договорная") == (None, None)
    assert parse_budget(None) == (None, None)


def test_parse_posted_relative_and_absolute():
    assert parse_posted("Вчера в 12:09", NOW) == datetime(2026, 9, 9, 12, 9, tzinfo=TZ).isoformat()
    assert parse_posted("Сегодня в 9:05", NOW) == datetime(2026, 9, 10, 9, 5, tzinfo=TZ).isoformat()
    assert parse_posted("7 сентября", NOW) == datetime(2026, 9, 7, 12, 0, tzinfo=TZ).isoformat()
    assert parse_posted("28 декабря в 10:00", NOW) == datetime(2025, 12, 28, 10, 0, tzinfo=TZ).isoformat()
    assert parse_posted("12:09", NOW) == datetime(2026, 9, 10, 12, 9, tzinfo=TZ).isoformat()
    assert parse_posted("", NOW) is None and parse_posted("когда-то", NOW) is None
