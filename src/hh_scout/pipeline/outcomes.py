"""What the companies did about the letters — the measurement the whole method is calibrated on.

Pure Python over the rows `repo.outcome_rows` returns; no SQL here and no SQL-free analytics in `repo.py`.
One vocabulary for every table and for the evaluator's calibration block (decisions #42, #48, #49):

* `outcome_of(row)` — invited / refused / answered / silent / blind;
* «ответ» means the company wrote and did not refuse; an invitation is counted on its own;
* nothing younger than `mature_days` enters a rate — a letter sent this morning is not "silence";
* a cell smaller than `MIN_CELL` shows a count, never a percentage.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

# Score bands the method is calibrated on. Kept here, not in the query, so the digest, /stats and
# any later threshold decision all slice the data the same way.
# 40–59 joined on 2026-09-20 (decision #52: threshold 50 and the daily floor down to 40); the older bands keep their edges.
SCORE_BANDS: tuple[tuple[str, int, int], ...] = (("40-49", 40, 49), ("50-54", 50, 54), ("55-59", 55, 59),
                                                 ("60-64", 60, 64), ("65-69", 65, 69), ("70-74", 70, 74), ("75+", 75, 1000))
MIN_CELL = 8   # below this a percentage is noise dressed as a finding, so the report prints the count instead
SEARCHING_LONG_DAYS = 14   # an employer advertising the same role this long "cannot fill the seat" (decision #44)

COMPANY_RU = {"integrator": "интегратор", "manufacturer": "производитель оборудования", "end_customer": "конечный заказчик",
              "agency": "агентство", "panel_builder": "сборщик шкафов", "design_bureau": "проектное бюро",
              "unknown": "не определён"}
LEAD_KIND_RU = {"vacancy": "вакансия", "company": "компания"}
CHANNEL_RU = {"panel": "щитовики (hh)", "design": "проектные бюро (hh)", "owen_si": "каталог ОВЕН", "profi": "profi.ru"}
WORK_FORMAT_RU = {"remote": "удалёнка", "hybrid": "гибрид", "office": "офис", "field": "разъездная"}


def age_days(row: sqlite3.Row) -> float:
    """How long the letter has had to produce an answer, from the moment the owner received the lead.

    Age is the confounder that breaks naive tables: measured 19.09, letters 0-1 days old answered 0 % and
    9-10 days old 78-82 %. Counted from the first delivery (`sent_at`), not from `cover_letters.created_at`,
    which a `/letter` rewrite resets (v9.11).
    """
    started = row["sent_at"] or row["letter_at"]
    if not started:
        return 0.0
    try:
        when = datetime.fromisoformat(str(started))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400.0


def outcome_of(row: sqlite3.Row | dict) -> str:
    """invited / refused / answered / silent / blind — what the company did about this letter."""
    if not row["applied"]:
        return "blind"       # sent outside hh.ru: the answer is invisible to us, never a failure
    state = (row["state"] or "").upper()
    if state == "INTERVIEW":
        return "invited"
    if state == "DISCARD":
        return "refused"
    return "answered" if row["has_chat"] else "silent"


OUTCOME_RU = {"invited": "компания пригласила к разговору", "refused": "компания отказала",
              "answered": "компания ответила", "silent": "компания молчит"}


def outcome_note(row: sqlite3.Row | dict, mature_days: int) -> str:
    """One clause for the evaluator's calibration block, or "" when there is nothing honest to say.

    Silence is reported only once the letter is old enough to have been answered — the same maturity rule
    `/stats` applies, so the model is not taught that this morning's letter "failed".
    """
    kind = outcome_of(row)
    if kind == "blind":
        return ""
    if kind == "silent" and age_days(row) < mature_days:
        return ""
    return f". ИТОГ: {OUTCOME_RU[kind]}"


def mature(rows: Sequence[sqlite3.Row], mature_days: int) -> list[sqlite3.Row]:
    return [r for r in rows if age_days(r) >= mature_days]


def maturing(rows: Sequence[sqlite3.Row], mature_days: int) -> int:
    """Letters too young to count yet — reported so their silence is never read as a failure."""
    return sum(1 for r in rows if age_days(r) < mature_days)


def _tally(cell: Sequence[sqlite3.Row]) -> dict[str, Any]:
    tracked = [r for r in cell if r["applied"]]
    kinds = [outcome_of(r) for r in tracked]
    answered, invited = kinds.count("answered"), kinds.count("invited")
    return {
        "n": len(cell),
        "tracked": len(tracked),
        "blind": len(cell) - len(tracked),
        "answered": answered,      # wrote back and did not refuse; invitations are their own column
        "invited": invited,
        "refused": kinds.count("refused"),
        "silent": kinds.count("silent"),
        # the share worth acting on: any positive reaction among the letters hh can see
        "rate": round(100 * (answered + invited) / len(tracked)) if len(tracked) >= MIN_CELL else None,
    }


def outcome_by(rows: Sequence[sqlite3.Row], key: Callable[[sqlite3.Row], str | None],
               mature_days: int) -> list[dict[str, Any]]:
    """Cross-tab of outcomes by any feature, counting only letters old enough to have an answer.

    `rate` is None when the cell is too small to mean anything (`MIN_CELL`): printing "2 of 2 = 100 %"
    would invent a finding out of two observations.
    """
    buckets: dict[str, list[sqlite3.Row]] = {}
    for r in mature(rows, mature_days):
        name = key(r)
        if name is not None:
            buckets.setdefault(name, []).append(r)
    return [{"name": name, **_tally(cell)} for name, cell in sorted(buckets.items(), key=lambda kv: -len(kv[1]))]


def outcome_stats(rows: Sequence[sqlite3.Row], mature_days: int) -> list[dict[str, Any]]:
    """Per score band: how many leads the owner wrote to, and what the companies did about it.

    Only leads the owner actually wrote to are in `rows` — a lead he waved away says nothing about the score.
    `blind` are the ones sent outside hh.ru (no `applied`), where the answer is invisible to us: they are
    reported separately instead of quietly diluting the conversion. The columns are disjoint:
    blind + silent + answered + invited + refused = written.
    """
    ready = mature(rows, mature_days)
    out = []
    for name, lo, hi in SCORE_BANDS:
        band = [r for r in ready if lo <= int(r["total"] or 0) <= hi]
        t = _tally(band)
        out.append({"band": name, "written": len(band), "blind": t["blind"], "answered": t["answered"],
                    "invited": t["invited"], "refused": t["refused"], "silent": t["silent"]})
    return out


def invited_count(rows: Sequence[sqlite3.Row]) -> int:
    """How many companies invited the owner to talk — an invitation needs no maturity to count."""
    return sum(1 for r in rows if outcome_of(r) == "invited")


def reply_delay_curve(rows: Sequence[sqlite3.Row]) -> list[tuple[str, int, int]]:
    """(bucket, letters, reacted) by letter age — the evidence the maturity threshold rests on.

    Here a refusal counts: the question is how long a company takes to react at all, and that is what
    says when a letter's silence stops being "too early" and starts being an answer in itself.
    """
    buckets = (("0-1 дн.", 0, 2), ("2-4 дн.", 2, 5), ("5-7 дн.", 5, 8), ("8+ дн.", 8, 10_000))
    out = []
    for name, lo, hi in buckets:
        cell = [r for r in rows if r["applied"] and lo <= age_days(r) < hi]
        out.append((name, len(cell), sum(1 for r in cell if outcome_of(r) != "silent")))
    return out


# --- the breakdowns /stats prints: title and how a row is bucketed, in one place -----------------------

def _by_company_kind(r: sqlite3.Row) -> str:
    return COMPANY_RU.get(r["company_kind"] or "unknown") or COMPANY_RU["unknown"]


def _by_work_format(r: sqlite3.Row) -> str:
    return WORK_FORMAT_RU.get(r["work_format"] or "", "не указан")


def _by_dossier(r: sqlite3.Row) -> str:
    return "собрано" if r["dossier"] else "пусто"


def _by_searching(r: sqlite3.Row) -> str | None:
    if r["searching_days"] is None:
        return None
    return f"ищут {SEARCHING_LONG_DAYS}+ дн." if int(r["searching_days"]) >= SEARCHING_LONG_DAYS else "свежая вакансия"


def _by_floor(r: sqlite3.Row) -> str:
    return "по дневному минимуму" if r["floor"] else "по порогу"


def _by_lead_kind(r: sqlite3.Row) -> str:
    return LEAD_KIND_RU.get(r["lead_kind"] or "vacancy", r["lead_kind"] or "вакансия")


def _by_channel(r: sqlite3.Row) -> str:
    ch = r["channel"] or ""
    return CHANNEL_RU.get(ch, "поиск вакансий")


def _by_letter_len(r: sqlite3.Row) -> str | None:
    n = r["letter_len"]
    if not n:
        return None
    return "до 2500" if n < 2500 else "2500-3000" if n < 3000 else "3000+"


DIMENSIONS: tuple[tuple[str, Callable[[sqlite3.Row], str | None]], ...] = (
    ("Тип компании", _by_company_kind),
    ("Формат работы", _by_work_format),
    ("Досье на компанию", _by_dossier),
    ("Ищут давно", _by_searching),
    ("Длина письма", _by_letter_len),
    ("Порог / минимум", _by_floor),
    ("Тип лида", _by_lead_kind),
    ("Канал", _by_channel),
)
