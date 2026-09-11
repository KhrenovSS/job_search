"""Cheap rule-based prefilter — no browser, no AI.

Applies to vacancies in status `new`. Every rejection records a `skip_reason`; survivors move to
`triage` where the AI decides (from the card alone) whether the page is worth opening.
Rules are deliberately conservative: when in doubt, let the AI decide. Salary is not a rule —
vacancies are leads for the owner's contracting work, a low salary says nothing about the lead.

CLI:  python -m hh_scout.pipeline.prefilter [--dry-run] [--show-skipped]
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass

from hh_scout.config import TITLE_KEEP_WORDS, TITLE_REQUIRED_ANY, TITLE_STOP_WORDS, Settings
from hh_scout.db import transaction
from hh_scout.pipeline import repo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CardFacts:
    hh_id: str
    title: str
    applied: bool
    archived: bool
    employment: str | None
    salary_to_net: int | None


# Stop words match only at the start of a word: "водитель" must not hit "руководитель".
_STOP_RES = [(w, re.compile(r"(?<!\w)" + re.escape(w.strip()), re.I)) for w in TITLE_STOP_WORDS]


def decide(card: CardFacts, min_salary_net: int) -> str | None:
    """Return a skip reason, or None if the card passes."""
    if card.applied:
        return "applied"
    if card.archived:
        return "archived"
    if card.employment == "fly_in_fly_out":
        return "fly_in_fly_out"
    title = card.title.casefold()
    keep = any(k in title for k in TITLE_KEEP_WORDS)
    if not keep:
        for w, rx in _STOP_RES:
            if rx.search(title):
                return f"stopword:{w.strip()}"
    if not any(k in title for k in TITLE_REQUIRED_ANY):
        return "no_engineering_title"
    # Salary is deliberately NOT a rule: vacancies are leads for contracting, not jobs to take.
    return None


def _facts(row: sqlite3.Row) -> CardFacts:
    return CardFacts(
        hh_id=row["hh_id"],
        title=row["title"] or "",
        applied=bool(row["applied"]),
        archived=(row["skip_reason"] == "archived"),
        employment=row["employment"],
        salary_to_net=row["salary_to"],
    )


def run(conn: sqlite3.Connection, settings: Settings, *, dry_run: bool = False) -> Counter:
    """Move `new` → `triage` or `skipped`. Returns a Counter of outcomes ('passed' or reason)."""
    outcomes: Counter = Counter()
    rows = repo.list_vacancies(conn, "new")
    with transaction(conn):  # one commit for the whole batch, not one disk sync per card
        for row in rows:
            reason = decide(_facts(row), settings.min_salary_net)
            key = reason or "passed"
            outcomes[key.split(":")[0]] += 1
            if dry_run:
                continue
            if reason:
                repo.set_status(conn, row["hh_id"], "skipped", reason)
            else:
                repo.set_status(conn, row["hh_id"], "triage")
    log.info("Префильтр: %d карточек → %s", len(rows), dict(outcomes))
    return outcomes


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="only report, do not change statuses")
    ap.add_argument("--show-skipped", action="store_true", help="list skipped titles with reasons")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)

    outcomes = run(conn, settings, dry_run=args.dry_run)
    print("Итог:", dict(outcomes))
    if args.show_skipped:
        rows = conn.execute(
            "SELECT hh_id, title, employer, skip_reason FROM vacancies WHERE status = 'skipped' "
            "AND skip_reason NOT IN ('applied', 'triage') ORDER BY skip_reason, title"
        ).fetchall()
        cur = None
        for r in rows:
            if r["skip_reason"] != cur:
                cur = r["skip_reason"]
                print(f"\n== {cur} ==")
            print(f"  {r['hh_id']}  {r['title'][:70]}  — {r['employer'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
