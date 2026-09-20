"""Lead score and digest formatting. The AI gives sub-scores; the code owns the weights and the wording.

v3: a vacancy is a lead for the owner's contracting work. Salary and work format are shown as facts only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

from hh_scout.config import TZ, Settings
from hh_scout.hh.salary import human_from_raw
from hh_scout.llm.letter_checks import strip_role_address
from hh_scout.pipeline.outcomes import COMPANY_RU as _COMPANY_KIND_RU, SEARCHING_LONG_DAYS
from hh_scout.pipeline.rows import row_get as _row_get, row_site

log = logging.getLogger(__name__)

WORK_FORMAT_RU = {"remote": "🏠 удалёнка", "hybrid": "гибрид", "office": "🏢 офис", "field": "🚗 разъездная", "unknown": None}
EMPLOYMENT_RU = {"full": "штат", "part": "частичная занятость", "project": "📄 проектная работа", "fly_in_fly_out": "🚁 вахта", "unknown": None}
IP_RU = {"yes": "да", "maybe": "не указано", "no": "нет"}  # "maybe" = the vacancy says nothing, not "probably yes"
CONTRACT_RU = {"INDIVIDUAL_ENTREPRENEUR": "ИП", "SELF_EMPLOYED": "самозанятый", "INDIVIDUAL_PERSON": "физлицо"}
# The card shows the company kind only when the model named one; the stats tables print «не определён» instead.
COMPANY_RU = {**_COMPANY_KIND_RU, "unknown": None}


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


def format_outcome_dimensions(blocks: list[tuple[str, list[dict]]], curve: list[tuple[str, int, int]],
                              mature_days: int) -> str:
    """Outcomes sliced by feature. A cell too small for a percentage shows its count instead — the whole
    point of the report is to stop a two-observation cell from looking like a finding."""
    lines = ["<b>Что различает вакансии, на которые отвечают</b>",
             f"Считаются письма старше {mature_days} дн. — младшие ещё не дозрели. "
             "«Ответ» — компания написала и не отказала; приглашения и отказы показаны отдельно, "
             "доля — ответы и приглашения вместе."]
    for title, rows in blocks:
        if not rows:
            continue
        lines.append(f"\n<b>{_esc(title)}</b>")
        lines.append("<pre>                       писем  отв. пригл. отказ  доля</pre>")
        for r in rows:
            share = f"{r['rate']}%" if r["rate"] is not None else "мало данных"
            lines.append(f"<pre>{_esc(str(r['name']))[:22]:<22} {r['tracked']:>5} {r['answered']:>5} {r['invited']:>6} "
                         f"{r['refused']:>5}  {share}</pre>")
    lines.append("\n<b>Когда компания вообще реагирует</b> (ответ, приглашение или отказ)")
    lines.append("<pre>возраст    писем  реакц.  доля</pre>")
    for name, n, answered in curve:
        if n:
            lines.append(f"<pre>{name:<9} {n:>6} {answered:>6}  {round(100 * answered / n)}%</pre>")
    return "\n".join(lines)


def format_letter(employer: str | None, text: str, site: str = "hh") -> str:
    """Cover letter (or a profi.ru bid) as a separate Telegram message; <pre> gives one-tap copy in Telegram clients.

    The last door before Telegram: a letter written days ago is sent from the database verbatim, so the
    role-address label is cut here too (decision #46).
    """
    text = strip_role_address(text)
    if site == "profi":
        return f"✉️ Предложение для «{_esc(employer or 'заказчика')}» (profi.ru):\n<pre>{_esc(text)}</pre>"
    return f"✉️ Отклик для «{_esc(employer or 'компании')}»:\n<pre>{_esc(text)}</pre>"


def hh_contract_note(row: sqlite3.Row) -> str | None:
    """What hh itself says about the contract form — a fact from the site, not the model's guess."""
    raw = _row_get(row, "civil_law_contracts")
    forms = [CONTRACT_RU[c] for c in (json.loads(raw) if raw else []) if c in CONTRACT_RU]
    if forms:
        return "✅ hh: оформление — " + ", ".join(forms)
    if _row_get(row, "accept_temporary"):
        return "✅ hh: оформление по ГПХ/совместительству"
    return None


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
