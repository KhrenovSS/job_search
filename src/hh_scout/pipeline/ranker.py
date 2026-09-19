"""Lead score and digest formatting. The AI gives sub-scores; the code owns the weights and the wording.

v3: a vacancy is a lead for the owner's contracting work. Salary and work format are shown as facts only.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime

from hh_scout.config import TZ, Settings
from hh_scout.hh.salary import human_from_raw

log = logging.getLogger(__name__)

WORK_FORMAT_RU = {"remote": "🏠 удалёнка", "hybrid": "гибрид", "office": "🏢 офис", "field": "🚗 разъездная", "unknown": None}
EMPLOYMENT_RU = {"full": "штат", "part": "частичная занятость", "project": "📄 проектная работа", "fly_in_fly_out": "🚁 вахта", "unknown": None}
# How long an employer must have been advertising the same role before it counts as "cannot fill it".
SEARCHING_LONG_DAYS = 14
IP_RU = {"yes": "да", "maybe": "не указано", "no": "нет"}  # "maybe" = the vacancy says nothing, not "probably yes"
CONTRACT_RU = {"INDIVIDUAL_ENTREPRENEUR": "ИП", "SELF_EMPLOYED": "самозанятый", "INDIVIDUAL_PERSON": "физлицо"}
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
    lead_bits = []
    hh_note = hh_contract_note(v)
    if hh_note:
        lead_bits.append(hh_note)
    lead_bits.append(f"🤝 ИП/ГПХ: {IP_RU.get(e['ip_gph_possible'], e['ip_gph_possible'])}")
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
    waited = _row_get(v, "waiting_days") or 0
    if waited >= 2:
        lines[1] += f" · ⏳ в очереди {waited} дн."
    searching = _row_get(v, "searching_days") or 0
    if searching >= SEARCHING_LONG_DAYS:
        lines[1] += f" · 🔁 ищут {searching} дн."
    about = _company_line(v)
    if about:
        lines.append(f"   🏭 О компании: {_esc(about)}")
    if e["pitch_hint"]:
        lines.append(f"   ✉️ Зацепка: {_esc(e['pitch_hint'])}")
    if flags:
        lines.append(f"   ⚠️ {_esc('; '.join(flags))}")
    lines.append(f"   {v['url']}")
    return "\n".join(lines)


def _company_line(v: sqlite3.Row) -> str:
    """One line of the employer dossier, so the owner sees what the letter was built on and can spot an invention."""
    raw = _row_get(v, "company_brief")
    if not raw:
        return ""
    try:
        brief = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    return str(brief.get("what_they_do") or "")[:300]


# The letter must never sort its reader by job title: "Для отдела кадров: подряд не требует…" reads as a mailshot,
# and the owner deleted that label by hand from every letter that had it (v9.6, decision #38). Cutting the label is
# cheaper and surer than regenerating the whole letter, so it is cut, not rejected — on the way in (the writer
# cleans the bridge answer) and again here, on the way out: a letter written days ago under older rules is sent
# from the database verbatim, and this is the last door before Telegram (v9.9, decision #46).
_ROLE_ADDRESS_RE = re.compile(
    r"(?:\A|\n|(?<=\.)[ \t])[ \t]*(?:отдельно\s+)?для\s+[^:\n]{0,60}?(?:кадр|подбор|персонал|hr|рекрут)[^:\n]{0,20}:[ \t]*",
    re.IGNORECASE)


def strip_role_address(text: str) -> str:
    """Drop a "Для отдела кадров:" label, keeping the sentence it introduced (decision #38).

    The owner did exactly this by hand before sending: the thought is right, naming the reader's job is not.
    """
    out: list[str] = []
    cuts: list[int] = []   # where in the result the sentence that lost its label now begins
    last = pos = 0
    for m in _ROLE_ADDRESS_RE.finditer(text):
        log.info("Убрал из письма обращение по должности: «%s»", m.group(0).strip())
        lead = m.group(0)[0]
        chunk = text[last:m.start()] + (lead if lead.isspace() else "")   # keep the break, drop the label
        out.append(chunk)
        pos += len(chunk)
        cuts.append(pos)
        last = m.end()
    if not cuts:
        return text
    out.append(text[last:])
    chars = list("".join(out))
    for i in cuts:                       # the label carried the capital letter — give it back
        if i < len(chars):
            chars[i] = chars[i].upper()
    return "".join(chars)


def format_letter(employer: str | None, text: str, site: str = "hh") -> str:
    """Cover letter (or a profi.ru bid) as a separate Telegram message; <pre> gives one-tap copy in Telegram clients."""
    text = strip_role_address(text)
    if site == "profi":
        return f"✉️ Предложение для «{_esc(employer or 'заказчика')}» (profi.ru):\n<pre>{_esc(text)}</pre>"
    return f"✉️ Отклик для «{_esc(employer or 'компании')}»:\n<pre>{_esc(text)}</pre>"


def _row_get(row: sqlite3.Row, column: str):
    """Column value, or None for rows built without it (old fixtures, ad-hoc SELECTs)."""
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


def hh_contract_note(row: sqlite3.Row) -> str | None:
    """What hh itself says about the contract form — a fact from the site, not the model's guess."""
    raw = _row_get(row, "civil_law_contracts")
    forms = [CONTRACT_RU[c] for c in (json.loads(raw) if raw else []) if c in CONTRACT_RU]
    if forms:
        return "✅ hh: оформление — " + ", ".join(forms)
    if _row_get(row, "accept_temporary"):
        return "✅ hh: оформление по ГПХ/совместительству"
    return None


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


def format_queue_tail(waiting: list[sqlite3.Row], total: int) -> str:
    """The leads that did not make today's quota — one line each, letter on request.

    They are not rejected: the queue outlives the day, and tomorrow they compete with whatever arrives.
    """
    if not waiting:
        return ""
    head = (f"<b>Ждут очереди: {total}</b>" if total <= len(waiting)
            else f"<b>Ждут очереди: {total}</b> (ближайшие {len(waiting)})")
    lines = [head + " — не попали в сегодняшнюю норму, но не отброшены."]
    for r in waiting:
        waited = r["waiting_days"] if "waiting_days" in r.keys() else 0
        age = f" · ждёт {waited} дн." if (waited or 0) >= 2 else ""
        lines.append(f"{r['total']}/100{age} · {_esc(r['employer'] or '—')} · "
                     f"<a href=\"{r['url']}\">{_esc((r['title'] or '')[:55])}</a> · /letter {r['hh_id']}")
    lines.append("Письмо по любой из них — командой /letter &lt;id&gt;, можно с пожеланием: "
                 "<code>/letter 12345678 больше про SCADA</code>")
    return "\n".join(lines)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def digest_header(count: int, checked: int, when: datetime | None = None, open_before: int = 0,
                  work: dict[str, int] | None = None, invited: int | None = None, invited_days: int = 14,
                  sent_today: int = 0) -> str:
    """`work` = repo.work_totals(): the only daily word about how the service itself is doing (quiet mode).

    `invited` = repo.invited_since(): what the letters actually bought over the last `invited_days`.
    """
    when = when or datetime.now(TZ)
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    date = f"{when.day} {months[when.month - 1]}"
    tail = f"\nРабота за сутки: подходов {work['sittings']} · страниц {work['page_loads']}" if work else ""
    tail += f"\nНеобработанных с прошлых дней: {open_before} (/inbox)" if open_before else ""
    if invited is not None:
        tail += f"\nПриглашений за {invited_days} дн.: {invited} (/stats)"
    if count == 0:
        if sent_today:
            noun = _plural(sent_today, "лид", "лида", "лидов")
            return (f"<b>Итог за {date}</b>: {sent_today} {noun} уже ушло сразу после подходов, "
                    f"нового к этому часу нет. Проверено {checked} вакансий.{tail}")
        return f"Сегодня лидов не нашлось. Проверено {checked} новых вакансий.{tail}"
    noun = _plural(count, "лид", "лида", "лидов")
    head = f"<b>Лиды за {date} — {count} {noun}</b> (проверено {checked} вакансий)"
    if sent_today:
        head += f"\nЕщё {sent_today} ушло сразу после подходов"
    return head + tail


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
