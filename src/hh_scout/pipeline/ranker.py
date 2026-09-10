"""Lead score and digest formatting. The AI gives sub-scores; the code owns the weights and the wording.

v3: a vacancy is a lead for the owner's contracting work. Salary and work format are shown as facts only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from hh_scout.config import TZ, Settings
from hh_scout.hh.salary import human_from_raw

WORK_FORMAT_RU = {"remote": "🏠 удалёнка", "hybrid": "гибрид", "office": "🏢 офис", "field": "🚗 разъездная", "unknown": None}
EMPLOYMENT_RU = {"full": "штат", "part": "частичная занятость", "project": "📄 проектная работа", "fly_in_fly_out": "вахта", "unknown": None}
IP_RU = {"yes": "да", "maybe": "возможно", "no": "нет"}
COMPANY_RU = {"integrator": "интегратор", "manufacturer": "производитель оборудования", "end_customer": "конечный заказчик",
              "agency": "агентство", "unknown": None}


def total_score(settings: Settings, tech: int, role: int, lead: int) -> int:
    return int(round(settings.weight_tech * tech + settings.weight_role * role + settings.weight_lead * lead))


def format_card(position: int, v: sqlite3.Row, e: sqlite3.Row) -> str:
    """One digest message (Telegram HTML)."""
    salary_raw = json.loads(v["salary_raw"]) if v["salary_raw"] else None
    profi = row_site(v) == "profi"
    kind = COMPANY_RU.get(e["company_kind"] or "unknown")
    who = v["employer"] or ("заказчик не указан" if profi else "компания не указана")
    head = f"<b>{position}. {_esc(v['title'])}</b> — {_esc(who)}" + (f" ({kind})" if kind and not profi else "")
    facts = ["🛠 заказ на profi.ru" if profi else None, f"💰 {human_from_raw(salary_raw)}", WORK_FORMAT_RU.get(v["work_format"]),
             f"📍 {v['area_name']}" if v["area_name"] else None, EMPLOYMENT_RU.get(v["employment"] or "unknown")]
    lead_bits = [f"🤝 ИП/ГПХ: {IP_RU.get(e['ip_gph_possible'], e['ip_gph_possible'])}"]
    if e["is_agency"]:
        lead_bits.append("🏷 агентство")
    flags = json.loads(e["red_flags"]) if e["red_flags"] else []
    lines = [
        head,
        "   " + " · ".join(x for x in facts if x),
        "   " + " · ".join(lead_bits),
        f"   ⭐ Лид: <b>{e['total']}/100</b> (техника {e['tech_score']} · роль {e['role_score']} · лид {e['lead_score']})",
        f"   Что им нужно: {_esc(e['verdict'])}",
    ]
    if e["pitch_hint"]:
        lines.append(f"   ✉️ Зацепка: {_esc(e['pitch_hint'])}")
    if flags:
        lines.append(f"   ⚠️ {_esc('; '.join(flags))}")
    lines.append(f"   {v['url']}")
    return "\n".join(lines)


def format_letter(employer: str | None, text: str, site: str = "hh") -> str:
    """Cover letter (or a profi.ru bid) as a separate Telegram message; <pre> gives one-tap copy in Telegram clients."""
    if site == "profi":
        return f"✉️ Предложение для «{_esc(employer or 'заказчика')}» (profi.ru):\n<pre>{_esc(text)}</pre>"
    return f"✉️ Отклик для «{_esc(employer or 'компании')}»:\n<pre>{_esc(text)}</pre>"


def row_site(row: sqlite3.Row) -> str:
    """`vacancies.site` with a fallback for rows built without the column (old fixtures, ad-hoc SELECTs)."""
    try:
        return row["site"] or "hh"
    except (IndexError, KeyError):
        return "hh"


COLLAPSED_LABELS = {
    "responded": "✅ Написал",
    "auto_responded": "✅ Откликнулся на hh.ru",
    "disliked": "👎 Мимо",
    "closed_stale": "⌛ Устарело",
}
_REASON_RU = {"salary": "зарплата", "format": "формат", "stack": "не мой стек", "agency": "агентство"}


def format_collapsed(kind: str, row: sqlite3.Row, when: datetime | None = None, reason: str | None = None) -> str:
    """One-line replacement for a processed lead card (no keyboard)."""
    when = when or datetime.now(TZ)
    label = COLLAPSED_LABELS.get(kind, kind)
    tail = f" · {_REASON_RU.get(reason, reason)}" if reason else ""
    return (f"{label} {when.strftime('%d.%m')}{tail} · {_esc(row['employer'] or 'компания не указана')} · "
            f"<a href=\"{row['url']}\">{_esc(row['title'])}</a>")


def format_inbox(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "Все лиды обработаны — открытых нет."
    lines = ["<b>Открытые лиды</b> (сначала старые; ⏸ — отложенные внизу):"]
    for r in rows:
        try:
            d = datetime.fromisoformat(r["sent_at"]).astimezone(TZ).strftime("%d.%m")
        except (TypeError, ValueError):
            d = "—"
        mark = "⏸ " if r["deferred"] else ""
        lines.append(f"{mark}{d} · {r['total']} · {_esc(r['employer'] or '—')} · <a href=\"{r['url']}\">{_esc(r['title'][:60])}</a>")
    lines.append(f"\nИтого: {len(rows)}. Закрыть: кнопки под карточкой, /done <hh_id>, /cleanup [дней].")
    return "\n".join(lines)


def digest_header(count: int, checked: int, when: datetime | None = None, open_before: int = 0) -> str:
    when = when or datetime.now(TZ)
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    date = f"{when.day} {months[when.month - 1]}"
    tail = f"\nНеобработанных с прошлых дней: {open_before} (/inbox)" if open_before else ""
    if count == 0:
        return f"Сегодня лидов не нашлось. Проверено {checked} новых вакансий.{tail}"
    noun = "лид" if count % 10 == 1 and count % 100 != 11 else "лида" if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14 else "лидов"
    return f"<b>Лиды за {date} — {count} {noun}</b> (проверено {checked} вакансий){tail}"


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
