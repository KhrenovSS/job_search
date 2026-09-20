"""Tiny helpers for `sqlite3.Row` values that may or may not carry a column.

Rows come from many SELECTs (the lead select, ad-hoc CLI queries, old test fixtures), and a handful of modules
each grew their own `"x" in row.keys()` dance. One place instead (v9.11).
"""

from __future__ import annotations

import sqlite3
from typing import Any


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
