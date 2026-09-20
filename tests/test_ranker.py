import json
import sqlite3

from hh_scout.config import Settings
from hh_scout.pipeline.ranker import digest_header, format_card, total_score


def test_total_uses_lead_weights():
    s = Settings(_env_file=None)
    assert total_score(s, 100, 100, 100) == 100
    assert total_score(s, 90, 80, 50) == round(0.55 * 90 + 0.25 * 80 + 0.20 * 50)
    assert total_score(s, 0, 0, 0) == 0


def _row(**kw):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(kw)
    return conn.execute(f"SELECT {', '.join('? AS ' + k for k in kw)}", list(kw.values())).fetchone()


def test_card_shows_hh_gph_flag_when_set():
    """hh said "Оформление по ГПХ или по совместительству" — show it as a fact, above the model's guess."""
    v = _row(title="Программист ПЛК", employer="ООО", work_format="remote", employment="project",
             area_name="Москва", url="https://hh.ru/vacancy/2", salary_raw=None, accept_temporary=1,
             civil_law_contracts=json.dumps(["INDIVIDUAL_ENTREPRENEUR", "SELF_EMPLOYED"]))
    e = _row(total=80, tech_score=90, role_score=80, lead_score=60, ip_gph_possible="yes", is_agency=0,
             company_kind="integrator", verdict="Нужен программист", pitch_hint=None, red_flags=None)
    card = format_card(1, v, e)
    assert "✅ hh: оформление — ИП, самозанятый · 🤝 ИП/ГПХ: да" in card

    # flag without the explicit list falls back to the generic wording
    bare = _row(title="Программист ПЛК", employer="ООО", work_format="remote", employment="project",
                area_name="Москва", url="https://hh.ru/vacancy/3", salary_raw=None, accept_temporary=1)
    assert "✅ hh: оформление по ГПХ/совместительству" in format_card(1, bare, e)


def test_card_shows_lead_fields_and_hides_salary_weight():
    v = _row(title="Инженер-программист АСУ ТП", employer="Компания <X>", work_format="office", employment="full",
             area_name="Королёв", url="https://hh.ru/vacancy/1",
             salary_raw=json.dumps({"from": 120000, "to": 160000, "currencyCode": "RUR", "gross": False}))
    e = _row(total=78, tech_score=90, role_score=80, lead_score=50, ip_gph_possible="maybe", is_agency=1,
             company_kind="integrator", verdict="Нужен программист ОВЕН/CODESYS", pitch_hint="Предложить доработку программ",
             red_flags=json.dumps(["агентство"], ensure_ascii=False))
    card = format_card(1, v, e)
    assert "<b>1. Инженер-программист АСУ ТП</b> — Компания &lt;X&gt; (интегратор)" in card
    assert "120–160 тыс. ₽ на руки" in card and "🏢 офис" in card and "штат" in card
    assert "🤝 ИП/ГПХ: не указано · 🏷 агентство" in card  # "maybe" = the vacancy says nothing
    assert "оформление по ГПХ" not in card  # no hh flag -> no line
    assert "⭐ Лид: <b>78/100</b> (техника 90 · роль 80 · лид 50)" in card
    assert "✉️ Зацепка: Предложить доработку программ" in card
    assert "⚠️ агентство" in card and card.endswith("https://hh.ru/vacancy/1")


def test_format_letter_cuts_an_address_by_job_title_on_the_way_out():
    """v9.9: the label reached the owner in a letter written two days before the rule existed (decision #46).

    The writer cleans what the bridge returns, but a letter waits in the queue and is sent from the database
    verbatim — so the same cut has to happen here, in the last door before Telegram.
    """
    from hh_scout.pipeline.ranker import format_letter

    stored = ("Про деньги коротко: стоимость считается от объёма.\n\n"
              "Отдельно для тех, кто ведёт подбор: такой формат не требует ставки в штатном расписании.\n\n"
              "Сергей Хренов")
    out = format_letter("ГЕНЕРИУМ", stored)
    assert "ведёт подбор" not in out
    assert "Такой формат не требует ставки в штатном расписании." in out
    assert "стоимость считается от объёма" in out and "Сергей Хренов" in out
    # a letter with nothing to cut passes through untouched
    plain = "Здравствуйте.\n\nДля этого достаточно одного узла — посмотрим, как пойдёт."
    assert plain in format_letter("ООО Ромашка", plain)


def test_profi_order_card_and_bid_wording():
    from hh_scout.pipeline.ranker import format_letter

    assert format_letter("Клиент А", "текст", "profi").startswith("✉️ Предложение для «Клиент А» (profi.ru):")
    assert format_letter("ООО Ромашка", "текст").startswith("✉️ Отклик для «ООО Ромашка»:")
    v = _row(hh_id="profi:1", site="profi", title="Программирование овен", employer="Клиент А", area_name="Москва",
             work_format="remote", employment="project", url="https://profi.ru/backoffice/n.php?o=1",
             salary_raw='{"profi_budget": "до 5000 ₽", "to": 5000, "currencyCode": "RUR", "gross": false, "mode": "PROJECT"}')
    e = _row(total=80, tech_score=90, role_score=85, lead_score=60, company_kind="end_customer", ip_gph_possible="yes",
             is_agency=0, verdict="Дописать обмен с ИПП120", pitch_hint="", red_flags=None)
    card = format_card(1, v, e)
    assert "🛠 заказ на profi.ru" in card and "💰 бюджет до 5000 ₽" in card and "🏠 удалёнка" in card
    assert "(конечный заказчик)" not in card and card.endswith("https://profi.ru/backoffice/n.php?o=1")


def test_header_plural_forms():
    assert digest_header(0, 50).startswith("Сегодня лидов не нашлось")
    assert "1 лид</b>" in digest_header(1, 50)
    assert "3 лида</b>" in digest_header(3, 50)
    assert "11 лидов</b>" in digest_header(11, 50)
    work = {"sittings": 3, "page_loads": 118}
    assert digest_header(0, 50, work=work).endswith("Работа за сутки: подходов 3 · страниц 118")
    assert "Работа за сутки: подходов 3 · страниц 118\nНеобработанных" in digest_header(2, 50, open_before=1, work=work)
    assert "Работа за сутки" not in digest_header(2, 50)


def test_company_card_names_the_company_the_channel_the_offer_and_the_contacts():
    """v9.13: a company lead has no salary, no role score and no vacancy title in the head — the company is the lead."""
    v = _row(title="Сборщик шкафов автоматики", employer="Ктм Групп", employer_id="77", lead_kind="company",
             search_pass="panel", site="hh", area_name="Краснодар", url="https://hh.ru/vacancy/5", raw_json=None,
             salary_raw=None, company_brief=None)
    e = _row(total=72, tech_score=80, role_score=0, lead_score=60, ip_gph_possible="maybe", is_agency=0,
             company_kind="panel_builder", verdict="Собирают шкафы управления вентиляцией", pitch_hint="Программа под каждый шкаф",
             red_flags=None, offer_focus=json.dumps(["plc_hmi_per_panel", "templates"]), floor=0)
    card = format_card(1, v, e)
    assert card.startswith("<b>1. Ктм Групп</b> (сборщик шкафов)")
    assert "🔧 щитовики (hh)" in card and "👀 Найдена по: Сборщик шкафов автоматики" in card
    assert "⭐ Лид: <b>72/100</b> (соответствие 80 · лид 60)" in card and "роль" not in card and "💰" not in card
    assert "🤝 Предложить: программа ПЛК и панель под каждый шкаф; типовые программы для серийных шкафов" in card

    owen = _row(title="Системный интегратор ОВЕН (Золотой)", employer="КАЭЛ", employer_id="owen:1426", lead_kind="company",
                search_pass="owen_si", site="owen", area_name="Белгород", url="https://kael.pro/", salary_raw=None, company_brief=None,
                raw_json=json.dumps({"status": "Золотой", "site": "https://kael.pro/", "emails": ["vk@kael.pro"], "phones": ["+7 (909) 208-32-55"]}))
    card = format_card(2, owen, e)
    assert "🟡 каталог ОВЕН" in card and "партнёр ОВЕН: Золотой" in card and "Найдена по" not in card
    assert "📞 https://kael.pro/ · vk@kael.pro · +7 (909) 208-32-55" in card
