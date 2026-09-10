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
    assert "🤝 ИП/ГПХ: возможно · 🏷 агентство" in card
    assert "⭐ Лид: <b>78/100</b> (техника 90 · роль 80 · лид 50)" in card
    assert "✉️ Зацепка: Предложить доработку программ" in card
    assert "⚠️ агентство" in card and card.endswith("https://hh.ru/vacancy/1")


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
