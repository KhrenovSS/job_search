"""Tiny helpers for `sqlite3.Row` values that may or may not carry a column.

Rows come from many SELECTs (the lead select, ad-hoc CLI queries, old test fixtures), and a handful of modules
each grew their own `"x" in row.keys()` dance. One place instead (v9.11).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

_EMAIL_RE = re.compile(r"^[^@\s<>()]+@[^@\s<>()]+\.[a-zA-Zа-яА-Я]{2,}$")


def row_get(row: sqlite3.Row | dict, column: str, default: Any = None) -> Any:
    """Column value, or `default` for rows built without it."""
    try:
        return row[column]
    except (IndexError, KeyError):
        return default


def row_site(row: sqlite3.Row | dict) -> str:
    """`vacancies.site` with the hh.ru fallback for rows built without the column."""
    return row_get(row, "site") or "hh"


def is_hh(row: sqlite3.Row | dict) -> bool:
    return row_site(row) == "hh"


def lead_kind(row: sqlite3.Row | dict) -> str:
    """`vacancies.lead_kind`: 'vacancy' (default) or 'company' (v9.13, decision #53)."""
    return row_get(row, "lead_kind") or "vacancy"


def letter_key(row: sqlite3.Row | dict) -> str:
    """Which prompt family a row belongs to: 'profi' (an order), 'company' (a partnership offer) or 'hh' (a response).

    Everything that used to branch on the site — the evaluation prompt, the letter prompt, the rules stamp, the
    length limits — branches on this instead.
    """
    if row_site(row) == "profi":
        return "profi"
    if lead_kind(row) == "company":
        return "company"
    return "hh"


def needs_email(row: sqlite3.Row | dict) -> bool:
    """A catalogue company (ОВЕН, `site='owen'`) has no hh.ru vacancy to answer, so the only way to reach it is
    its e-mail: without one the lead is useless and does not go out (decision #56)."""
    return row_site(row) == "owen" and lead_kind(row) == "company"


def _as_email(value: Any) -> str | None:
    text = str(value or "").strip().strip(".,;").lower()
    return text if _EMAIL_RE.match(text) else None


def contact_email(row: sqlite3.Row | dict, brief: dict | None = None) -> str | None:
    """Where to e-mail a company lead: the catalogue's first valid address (`raw_json.emails`), else the general
    address the dossier read on the company's site (`employers.brief.contact_email`, v9.15). `brief` is a dossier
    fresher than the row's own `company_brief` column — the letter writer has just researched it. None if nothing."""
    raw = row_get(row, "raw_json")
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        data = {}
    for candidate in data.get("emails") or []:
        found = _as_email(candidate)
        if found:
            return found
    for source in (brief, row_get(row, "company_brief")):
        if not source:
            continue
        if isinstance(source, str):
            try:
                source = json.loads(source)
            except (TypeError, ValueError):
                continue
        found = _as_email(source.get("contact_email")) if isinstance(source, dict) else None
        if found:
            return found
    return None
