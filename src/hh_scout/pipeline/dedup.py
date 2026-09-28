"""One lead per company.

Companies post near-identical vacancies in several regions; the owner writes to the company once — a partnership is with
the whole organisation, not with a regional branch. So an employer that already has a lead (sent within
`EMPLOYER_REPEAT_DAYS`, or waiting for the digest) gets no second one: further vacancies are `skipped/duplicate_employer:<hh_id>`.
A company the owner has already answered — himself on hh.ru (`applied = 1`) or with «✅ Написал» on a card — is closed
the same way for `EMPLOYER_REPEAT_DAYS`, as `skipped/employer_responded:<hh_id>`: a second letter would reach the same
HR desk. That check runs first, since it holds even when the company has no lead at all.

Applied three times, each as early as the data allows (cheapest first):
* `skip_covered(conn, settings, status)` — before AI triage (`triage`) and, per row, before opening a vacancy page (`to_fetch`);
* `dedupe_evaluated(conn, settings)` — after evaluation, before letters, and again when the digest is planned: among the
  evaluated hh.ru leads at/above the threshold only the best-scored vacancy of each employer stays.
Identity: hh.ru `company.id` (`vacancies.employer_id`) or, for cards without an id, the employer name — and, since v9.22
(decision #64), a shared e-mail address or web domain (`employer_contacts`, `repo.linked_employer_ids`): that is how a
firm from the ОВЕН catalogue (`owen:<tag>`) and the same firm on hh.ru meet, in both directions. profi.ru is exempt.
Kinds (v9.15): a *vacancy* candidate is covered only by vacancy rows — a cold `plant` offer must not block the same
company's real programmer vacancy later; a *company* candidate is covered by any row (`repo._kind_sql`).

A twin is often skipped against a vacancy that has not been evaluated yet (`prefiltered`). If that one then turns out
not to be a lead, the whole company would silently vanish — so `revive_orphans(conn, settings)` puts such twins back
into the queue at the start of every run.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping

from hh_scout.config import Settings
from hh_scout.db import transaction
from hh_scout.pipeline import repo
from hh_scout.pipeline.rows import lead_kind as _lead_kind, row_site

log = logging.getLogger(__name__)


def _exempt(row: sqlite3.Row) -> bool:
    """profi.ru orders are private people, not companies — no identity to dedup on."""
    return row_site(row) == "profi"


def _links(conn: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    return repo.linked_employer_ids(conn, row["employer_id"])


def _via_contact(row: sqlite3.Row, other: sqlite3.Row) -> str:
    """Suffix for the log line when the match came through a shared address rather than the same employer id."""
    other_id = other["employer_id"] if "employer_id" in other.keys() else None
    return " — по общему адресу/домену" if other_id and other_id != row["employer_id"] else ""


def covering_lead(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> sqlite3.Row | None:
    """The existing lead of this row's employer, or None if the company is still free (or the row is profi.ru)."""
    if _exempt(row):
        return None
    return repo.employer_lead(conn, row["employer_id"], row["employer"], exclude_id=row["id"],
                              threshold=settings.score_threshold, repeat_days=settings.employer_repeat_days,
                              candidate_kind=_lead_kind(row), linked_ids=_links(conn, row))


def answered_employer(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row,
                      *, exclude_self: bool = True) -> sqlite3.Row | None:
    """The vacancy of this row's employer the owner has already answered, or None (also None for profi.ru)."""
    if _exempt(row):
        return None
    return repo.employer_responded(conn, row["employer_id"], row["employer"],
                                   exclude_id=row["id"] if exclude_self else None,
                                   within_days=settings.employer_repeat_days, candidate_kind=_lead_kind(row),
                                   linked_ids=_links(conn, row))


def skip_if_covered(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> sqlite3.Row | None:
    """Mark `row` a duplicate if its employer already has a lead — or was already answered; returns it or None.

    Commits nothing."""
    answered = answered_employer(conn, settings, row)
    if answered is not None:
        repo.skip_as_responded(conn, row["id"], answered["hh_id"])
        log.info("В компанию уже откликались: %s «%s» (%s) — %s, отклик по %s%s", row["hh_id"], (row["title"] or "")[:50],
                 row["area_name"] or "—", row["employer"] or "—", answered["hh_id"], _via_contact(row, answered))
        return answered
    lead = covering_lead(conn, settings, row)
    if lead is not None:
        repo.skip_as_duplicate(conn, row["id"], lead["hh_id"])
        log.info("Дубль компании: %s «%s» (%s) — у %s уже есть лид %s (%s)%s", row["hh_id"], (row["title"] or "")[:50],
                 row["area_name"] or "—", row["employer"] or "—", lead["hh_id"], lead["status"], _via_contact(row, lead))
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


DUPLICATE_PREFIX = "duplicate_employer:"
_IN_FLIGHT = ("new", "triage", "to_fetch")  # the covering vacancy still has a chance; `prefiltered` covers via covering_lead


def revive_orphans(conn: sqlite3.Connection, settings: Settings) -> int:
    """Twins whose covering vacancy never became a lead go back into the queue. Returns how many.

    "Never became a lead" is decided by `covering_lead`, not by the covering vacancy's status alone: `evaluated` below
    the threshold is as lost as `rejected`, it just has not been written off yet. Only a vacancy still on its way
    (`new`/`triage`/`to_fetch`) keeps its twins down.

    Back to `to_fetch` if the card already passed AI triage (`triage_priority` is set), otherwise back to `triage`;
    a catalogue row (`site='owen'`, v9.22) has no page to open and goes straight back to `prefiltered`.
    No loop: a revived twin ends up `rejected` (not `skipped`), so it is never picked up a second time."""
    rows = conn.execute(
        "SELECT * FROM vacancies WHERE site IN ('hh', 'owen', 'trudvsem', 'zakupki') AND status = 'skipped' AND skip_reason LIKE ? ORDER BY id",
        (DUPLICATE_PREFIX + "%",)).fetchall()
    n = 0
    with transaction(conn):
        for row in rows:
            cover = conn.execute("SELECT status FROM vacancies WHERE hh_id = ?",
                                 (row["skip_reason"][len(DUPLICATE_PREFIX):],)).fetchone()
            if cover is not None and cover["status"] in _IN_FLIGHT:
                continue                                   # that vacancy may still become the company's lead
            if covering_lead(conn, settings, row) is not None:
                continue                                   # the company has a lead (or one on its way) — stay a duplicate
            if row_site(row) in ("owen", "trudvsem", "zakupki"):
                status = "prefiltered"   # nothing to open: the catalogue card / the API item is the whole page
            else:
                status = "to_fetch" if row["triage_priority"] is not None else "triage"
            repo.set_status(conn, row["hh_id"], status, None)
            log.info("Дубль вернулся в очередь (%s): %s «%s» (%s) — у %s лида не осталось", status, row["hh_id"],
                     (row["title"] or "")[:50], row["area_name"] or "—", row["employer"] or "—")
            n += 1
    if n:
        log.info("Осиротевших дублей возвращено в очередь: %d", n)
    return n


def same_company(a: sqlite3.Row, b: sqlite3.Row, links: Mapping[str, set[str]] | None = None) -> bool:
    """Whether two rows are one organisation: the same hh.ru id or name, or (with `links` from `repo.contact_links`)
    two employers that share an e-mail address or a web domain (v9.22)."""
    if (_lead_kind(a) == "vacancy") != (_lead_kind(b) == "vacancy"):
        # a vacancy lead and a company offer to the same employer are two channels, not twins (v9.15) — in both
        # directions: until 24.09 only `a`=vacancy was checked, so a company row with the higher total closed the
        # programmer vacancy as its duplicate in `dedupe_evaluated`, against decision #55 (`repo._kind_sql`)
        return False
    if a["employer_id"] and b["employer_id"]:
        if a["employer_id"] == b["employer_id"]:
            return True
        return bool(links) and b["employer_id"] in links.get(a["employer_id"], ())
    return bool(a["employer"]) and (a["employer"] or "").casefold() == (b["employer"] or "").casefold()


def dedupe_evaluated(conn: sqlite3.Connection, settings: Settings) -> int:
    """Among evaluated hh.ru and catalogue leads at/above the threshold keep one per company (highest total, then
    newest); a company that already has a `sent` lead within the repeat window keeps none. Returns how many were skipped."""
    rows = [r for r in repo.evaluated_leads(conn, settings.score_threshold) if not _exempt(r)]  # total DESC, published DESC
    links = repo.contact_links(conn)
    groups: list[list[sqlite3.Row]] = []  # groups[i][0] is the kept lead; a row joins a group if it matches ANY member
    n = 0
    with transaction(conn):
        for row in rows:
            group = next((g for g in groups if any(same_company(m, row, links) for m in g)), None)
            if group is not None:
                twin = group[0]
                group.append(row)
                repo.skip_as_duplicate(conn, row["id"], twin["hh_id"])
                log.info("Дубль компании в дайджесте: %s «%s» (%s) уступает %s (%s, %d баллов)", row["hh_id"],
                         (row["title"] or "")[:50], row["area_name"] or "—", twin["hh_id"], twin["area_name"] or "—", twin["total"])
                n += 1
                continue
            answered = answered_employer(conn, settings, row)
            if answered is not None:
                repo.skip_as_responded(conn, row["id"], answered["hh_id"])
                log.info("В компанию уже откликались: %s «%s» — %s, отклик по %s", row["hh_id"],
                         (row["title"] or "")[:50], row["employer"] or "—", answered["hh_id"])
                n += 1
                continue
            sent = repo.employer_lead(conn, row["employer_id"], row["employer"], exclude_id=row["id"],
                                      threshold=settings.score_threshold, repeat_days=settings.employer_repeat_days,
                                      candidate_kind=_lead_kind(row), linked_ids=sorted(links.get(row["employer_id"], ())))
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
