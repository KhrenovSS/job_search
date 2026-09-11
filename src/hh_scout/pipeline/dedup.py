"""One lead per company.

Companies post near-identical vacancies in several regions; the owner writes to the company once — a partnership is with
the whole organisation, not with a regional branch. So an employer that already has a lead (sent within
`EMPLOYER_REPEAT_DAYS`, or waiting for the digest) gets no second one: further vacancies are `skipped/duplicate_employer:<hh_id>`.

Applied three times, each as early as the data allows (cheapest first):
* `skip_covered(conn, settings, status)` — before AI triage (`triage`) and, per row, before opening a vacancy page (`to_fetch`);
* `dedupe_evaluated(conn, settings)` — after evaluation, before letters, and again when the digest is planned: among the
  evaluated hh.ru leads at/above the threshold only the best-scored vacancy of each employer stays.
Identity: hh.ru `company.id` (`vacancies.employer_id`) or, for cards without an id, the employer name; profi.ru is exempt.
"""

from __future__ import annotations

import logging
import sqlite3

from hh_scout.config import Settings
from hh_scout.db import transaction
from hh_scout.pipeline import repo

log = logging.getLogger(__name__)


def _is_hh(row: sqlite3.Row) -> bool:
    return (row["site"] if "site" in row.keys() else "hh") == "hh"


def covering_lead(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> sqlite3.Row | None:
    """The existing lead of this row's employer, or None if the company is still free (or the row is not hh.ru)."""
    if not _is_hh(row):
        return None
    return repo.employer_lead(conn, row["employer_id"], row["employer"], exclude_id=row["id"],
                              threshold=settings.score_threshold, repeat_days=settings.employer_repeat_days)


def skip_if_covered(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> sqlite3.Row | None:
    """Mark `row` a duplicate if its employer already has a lead; returns that lead or None. Commits nothing."""
    lead = covering_lead(conn, settings, row)
    if lead is not None:
        repo.skip_as_duplicate(conn, row["id"], lead["hh_id"])
        log.info("Дубль компании: %s «%s» (%s) — у %s уже есть лид %s (%s)", row["hh_id"], (row["title"] or "")[:50],
                 row["area_name"] or "—", row["employer"] or "—", lead["hh_id"], lead["status"])
    return lead


def skip_covered(conn: sqlite3.Connection, settings: Settings, status: str) -> int:
    """All rows in `status` whose employer already has a lead → skipped/duplicate_employer. Returns how many."""
    n = 0
    with transaction(conn):
        for row in repo.list_vacancies(conn, status):
            if skip_if_covered(conn, settings, row) is not None:
                n += 1
    if n:
        log.info("Дубли компаний среди %s: %d пропущено без загрузки/оценки", status, n)
    return n


def _same_company(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    if a["employer_id"] and b["employer_id"]:
        return a["employer_id"] == b["employer_id"]
    return bool(a["employer"]) and (a["employer"] or "").casefold() == (b["employer"] or "").casefold()


def dedupe_evaluated(conn: sqlite3.Connection, settings: Settings) -> int:
    """Among evaluated hh.ru leads at/above the threshold keep one per company (highest total, then newest);
    a company that already has a `sent` lead within the repeat window keeps none. Returns how many were skipped."""
    rows = repo.evaluated_leads(conn, settings.score_threshold, site="hh")  # ordered by total DESC, published DESC
    groups: list[list[sqlite3.Row]] = []  # groups[i][0] is the kept lead; a row joins a group if it matches ANY member
    n = 0
    with transaction(conn):
        for row in rows:
            group = next((g for g in groups if any(_same_company(m, row) for m in g)), None)
            if group is not None:
                twin = group[0]
                group.append(row)
                repo.skip_as_duplicate(conn, row["id"], twin["hh_id"])
                log.info("Дубль компании в дайджесте: %s «%s» (%s) уступает %s (%s, %d баллов)", row["hh_id"],
                         (row["title"] or "")[:50], row["area_name"] or "—", twin["hh_id"], twin["area_name"] or "—", twin["total"])
                n += 1
                continue
            sent = repo.employer_lead(conn, row["employer_id"], row["employer"], exclude_id=row["id"],
                                      threshold=settings.score_threshold, repeat_days=settings.employer_repeat_days)
            if sent is not None and sent["status"] == "sent":
                repo.skip_as_duplicate(conn, row["id"], sent["hh_id"])
                log.info("Дубль компании: %s «%s» — %s уже получал лид %s", row["hh_id"], (row["title"] or "")[:50],
                         row["employer"] or "—", sent["hh_id"])
                n += 1
                continue
            groups.append([row])
    if n:
        log.info("Одна компания — один лид: %d оценённых вакансий пропущено как дубли", n)
    return n
