"""Thin DAO over SQLite for the pipeline. No business rules here — just SQL."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Sequence

from hh_scout.browser.hh_pages import VacancyCard, VacancyDetail
from hh_scout.config import TZ
from hh_scout.db import utcnow
from hh_scout.hh.salary import normalize


# --- vacancies ------------------------------------------------------------------

def vacancy_exists(conn: sqlite3.Connection, hh_id: str) -> bool:
    return conn.execute("SELECT 1 FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone() is not None


def insert_card(conn: sqlite3.Connection, card: VacancyCard, source: str, search_pass: str) -> bool:
    """Insert a freshly seen vacancy. Returns False if it was already known (nothing changed)."""
    if vacancy_exists(conn, card.hh_id):
        if card.applied:
            mark_applied(conn, card.hh_id, has_chat=False)
        return False
    sal = normalize(card.compensation)
    now = utcnow()
    status, reason = ("new", None)
    if card.applied:
        status, reason = ("skipped", "applied")
    elif card.archived:
        status, reason = ("skipped", "archived")
    conn.execute(
        """INSERT INTO vacancies(hh_id, title, employer, employer_id, url, area_name, work_format, employment,
                                 accept_temporary, civil_law_contracts,
                                 salary_from, salary_to, salary_raw, published_at, source, search_pass,
                                 status, skip_reason, applied, first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (card.hh_id, card.title, card.employer, card.employer_id, card.url, card.area_name, card.work_format, card.employment,
         int(card.accept_temporary), _contracts_json(card.civil_law_contracts),
         sal.from_net, sal.to_net, json.dumps(card.compensation, ensure_ascii=False) if card.compensation else None,
         card.published_at, source, search_pass, status, reason, int(card.applied), now, now),
    )
    return True


def insert_order(conn: sqlite3.Connection, order: "OrderCard") -> bool:
    """Insert a profi.ru order as a `prefiltered` row (full text is already in the feed card). False if known."""
    if vacancy_exists(conn, order.ext_id):
        return False
    now = utcnow()
    raw = {"description": order.description, "budget": order.budget_text, "when": order.when, "client": order.client,
           "posted": order.posted_text, "work_format": order.work_format, "city": order.city, "site": "profi"}
    salary_raw = {"profi_budget": order.budget_text, "from": order.budget_from, "to": order.budget_to,
                  "currencyCode": "RUR", "gross": False, "mode": "PROJECT"} if order.budget_text else None
    conn.execute(
        """INSERT INTO vacancies(hh_id, site, title, employer, url, area_name, work_format, employment,
                                 salary_from, salary_to, salary_raw, published_at, source, search_pass, raw_json,
                                 status, skip_reason, applied, first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (order.ext_id, "profi", order.title, order.client or "частный заказчик", order.url, order.city, order.work_format,
         "project", order.budget_from, order.budget_to, json.dumps(salary_raw, ensure_ascii=False) if salary_raw else None,
         order.published_at or now, "profi", "profi", json.dumps(raw, ensure_ascii=False), "prefiltered", None, 0, now, now),
    )
    return True


def count_site(conn: sqlite3.Connection, site: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM vacancies WHERE site = ?", (site,)).fetchone()[0])


def record_negotiation(conn: sqlite3.Connection, vacancy_id: int, state: str | None, has_messages: bool) -> bool:
    """Append a conversation event, but only when something actually changed (decision #48).

    The sync re-reads the same list three times a day; without this check the history would be 180 identical
    rows a day. Returns True when a row was appended.
    """
    last = conn.execute(
        "SELECT state, has_messages FROM negotiation_events WHERE vacancy_id = ? ORDER BY id DESC LIMIT 1",
        (vacancy_id,)).fetchone()
    if last is not None and (last["state"] or "") == (state or "") and bool(last["has_messages"]) == bool(has_messages):
        return False
    conn.execute("INSERT INTO negotiation_events(vacancy_id, state, has_messages, seen_at) VALUES (?,?,?,?)",
                 (vacancy_id, state, int(has_messages), utcnow()))
    return True


def mark_applied(conn: sqlite3.Connection, hh_id: str, *, has_chat: bool, state: str | None = None,
                 title: str | None = None, employer: str | None = None, url: str | None = None) -> None:
    """Flag a vacancy the owner already responded to; creates a stub row if unknown.

    `state` is hh's own view of the conversation (RESPONSE / INTERVIEW / DISCARD) — the only place the
    method learns whether an offer led anywhere. The latest state wins, but a missing one never erases
    a state we already knew: a page that failed to parse must not look like "nothing happened".
    """
    now = utcnow()
    if vacancy_exists(conn, hh_id):
        conn.execute(
            """UPDATE vacancies SET applied = 1, has_chat = MAX(has_chat, ?),
                      negotiation_state = COALESCE(?, negotiation_state),
                      negotiation_seen_at = CASE WHEN ? IS NULL THEN negotiation_seen_at ELSE ? END,
                      updated_at = ?,
                      status = CASE WHEN status IN ('new', 'prefiltered', 'evaluated') THEN 'skipped' ELSE status END,
                      skip_reason = CASE WHEN status IN ('new', 'prefiltered', 'evaluated') THEN 'applied' ELSE skip_reason END
               WHERE hh_id = ?""",
            (int(has_chat), state, state, now, now, hh_id),
        )
    else:
        conn.execute(
            """INSERT INTO vacancies(hh_id, title, employer, url, source, search_pass, status, skip_reason,
                                     applied, has_chat, negotiation_state, negotiation_seen_at, first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (hh_id, title or "(отклик)", employer, url or f"https://hh.ru/vacancy/{hh_id}", "negotiations",
             "negotiations", "skipped", "applied", 1, int(has_chat), state, now if state else None, now, now),
        )
    # The snapshot above keeps only the latest state; the history is what "time to answer" is computed from.
    row = conn.execute("SELECT id FROM vacancies WHERE hh_id = ?", (hh_id,)).fetchone()
    if row is not None:
        record_negotiation(conn, int(row["id"]), state, has_chat)


def set_status(conn: sqlite3.Connection, hh_id: str, status: str, reason: str | None = None) -> None:
    conn.execute(
        "UPDATE vacancies SET status = ?, skip_reason = ?, updated_at = ? WHERE hh_id = ?",
        (status, reason, utcnow(), hh_id),
    )


def save_triage(conn: sqlite3.Connection, hh_id: str, *, open_it: bool, priority: int, note: str) -> None:
    status, reason = ("to_fetch", None) if open_it else ("skipped", "triage")
    conn.execute(
        "UPDATE vacancies SET status = ?, skip_reason = ?, triage_priority = ?, triage_note = ?, updated_at = ? WHERE hh_id = ?",
        (status, reason, priority, note[:200], utcnow(), hh_id),
    )


DETAIL_KEYS = ("vacancyId", "name", "description", "keySkills", "compensation", "workFormats", "employmentForm",
               "area", "status", "publicationDate", "workExperience", "workScheduleByDays", "workingHours",
               "closedForApplicants", "userLabels", "civilLawContracts")


def trim_vacancy_view(raw: dict[str, Any]) -> dict[str, Any]:
    out = {k: raw.get(k) for k in DETAIL_KEYS if k in raw}
    company = raw.get("company") if isinstance(raw.get("company"), dict) else {}
    out["company"] = {k: company.get(k) for k in ("id", "name", "visibleName", "@trusted") if k in company}
    addr = raw.get("address") if isinstance(raw.get("address"), dict) else {}
    out["address"] = {k: addr.get(k) for k in ("city", "street", "building", "displayName") if k in addr}
    return out


def _contracts_json(values: tuple[str, ...]) -> str | None:
    """hh's `civilLawContracts` as stored text; None keeps whatever an earlier page already saw."""
    return json.dumps(list(values), ensure_ascii=False) if values else None


def save_details(conn: sqlite3.Connection, detail: VacancyDetail) -> str:
    """Store a fetched vacancy page; returns the resulting status."""
    if detail.applied:
        status, reason = "skipped", "applied"
    elif detail.archived:
        status, reason = "skipped", "archived"
    else:
        status, reason = "prefiltered", None
    sal = normalize(detail.compensation)
    conn.execute(
        """UPDATE vacancies SET raw_json = ?, title = COALESCE(NULLIF(?, ''), title), employer = COALESCE(?, employer),
                  employer_id = COALESCE(?, employer_id),
                  area_name = COALESCE(?, area_name), work_format = ?, employment = ?,
                  accept_temporary = MAX(accept_temporary, ?),
                  civil_law_contracts = COALESCE(?, civil_law_contracts),
                  salary_from = ?, salary_to = ?, salary_raw = COALESCE(?, salary_raw),
                  applied = MAX(applied, ?), status = ?, skip_reason = ?, updated_at = ?
           WHERE hh_id = ?""",
        (json.dumps(trim_vacancy_view(detail.raw), ensure_ascii=False), detail.title, detail.employer, detail.employer_id, detail.area_name,
         detail.work_format, detail.employment,
         int(detail.accept_temporary), _contracts_json(detail.civil_law_contracts), sal.from_net, sal.to_net,
         json.dumps(detail.compensation, ensure_ascii=False) if detail.compensation else None,
         int(detail.applied), status, reason, utcnow(), detail.hh_id),
    )
    return status


def page_loads_today(conn: sqlite3.Connection) -> int:
    """Sum of page loads over all runs started today (Europe/Moscow), any status."""
    start_local = datetime.combine(datetime.now(TZ).date(), time(0, 0), tzinfo=TZ)
    start_utc = start_local.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    row = conn.execute("SELECT COALESCE(SUM(page_loads), 0) AS n FROM runs WHERE started_at >= ?", (start_utc,)).fetchone()
    return int(row["n"])


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM vacancies GROUP BY status")}


def list_vacancies(conn: sqlite3.Connection, status: str, limit: int | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT * FROM vacancies WHERE status = ? "
           "ORDER BY COALESCE(triage_priority, 9), published_at DESC, id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (status,)).fetchall()


def expire_low_priority(conn: sqlite3.Connection, ttl_days: int, min_priority: int = 3) -> int:
    """Drop `to_fetch` cards of low triage priority that waited longer than ttl_days (skip_reason low_priority_expired)."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).replace(microsecond=0).isoformat()
    cur = conn.execute(
        "UPDATE vacancies SET status = 'skipped', skip_reason = 'low_priority_expired', updated_at = ? "
        "WHERE status = 'to_fetch' AND COALESCE(triage_priority, 3) >= ? AND updated_at < ?",
        (utcnow(), min_priority, cutoff),
    )
    return cur.rowcount


# --- one lead per company -----------------------------------------------------------

def same_employer_sql(alias: str = "v") -> str:
    """WHERE fragment: `alias` is an hh.ru row of the employer given by params (employer_id, employer_id, employer, employer).

    Match by hh.ru company id, or by name (case-insensitive via the `casefold` function registered in db.connect) —
    cards collected before v8 carry no id.
    profi.ru rows never match (their `employer` is a client's first name).
    """
    return (f"{alias}.site = 'hh' AND ((? IS NOT NULL AND {alias}.employer_id = ?) "
            f"OR (? IS NOT NULL AND casefold({alias}.employer) = casefold(?)))")


def same_employer_params(employer_id: str | None, employer: str | None) -> list:
    return [employer_id, employer_id, employer or None, employer or None]


def employer_lead(conn: sqlite3.Connection, employer_id: str | None, employer: str | None, *, exclude_id: int | None,
                  threshold: int, repeat_days: int) -> sqlite3.Row | None:
    """The vacancy of this employer that already is a lead (sent within `repeat_days`; 0 = ever) or is about to become one
    (`prefiltered`, or `evaluated` at/above the threshold and waiting for the digest). None if the company is still free."""
    if employer_id is None and not employer:
        return None
    from datetime import timedelta

    cutoff = ((datetime.now(timezone.utc) - timedelta(days=repeat_days)).replace(microsecond=0).isoformat()
              if repeat_days > 0 else "1970-01-01T00:00:00+00:00")
    sql = (f"SELECT v.id, v.hh_id, v.status FROM vacancies v WHERE {same_employer_sql('v')} AND v.id IS NOT ? "
           "AND ((v.status = 'sent' AND v.updated_at >= ?) OR v.status = 'prefiltered' "
           "     OR (v.status = 'evaluated' AND EXISTS (SELECT 1 FROM evaluations e WHERE e.vacancy_id = v.id AND e.total >= ?))) "
           "ORDER BY CASE v.status WHEN 'sent' THEN 0 WHEN 'evaluated' THEN 1 ELSE 2 END, v.id LIMIT 1")
    return conn.execute(sql, same_employer_params(employer_id, employer) + [exclude_id, cutoff, threshold]).fetchone()


def employer_responded(conn: sqlite3.Connection, employer_id: str | None, employer: str | None, *,
                       exclude_id: int | None, within_days: int) -> sqlite3.Row | None:
    """The vacancy of this employer the owner has already answered, within `within_days` (0 = ever), newest first.

    Two ways an answer is recorded: the owner applied on hh.ru himself and the collector saw it in his negotiations
    (`vacancies.applied = 1`), or he pressed «✅ Написал» on a lead card (`lead_actions.responded` / `auto_responded`).
    A letter goes to the company's HR, not to a branch, so one answer closes the whole company — writing again would
    land on the same desk.
    """
    if employer_id is None and not employer:
        return None
    from datetime import timedelta

    cutoff = ((datetime.now(timezone.utc) - timedelta(days=within_days)).replace(microsecond=0).isoformat()
              if within_days > 0 else "1970-01-01T00:00:00+00:00")
    sql = (f"""SELECT * FROM (
                 SELECT v.id, v.hh_id, v.title, v.status,
                        COALESCE((SELECT MAX(a.created_at) FROM lead_actions a
                                    WHERE a.vacancy_id = v.id
                                      AND a.action IN ('responded', 'auto_responded')),
                                 v.updated_at) AS answered_at   -- «✅ Написал» точен; у откликов с hh.ru есть
                                                                -- только момент, когда бот их увидел
                   FROM vacancies v
                  WHERE {same_employer_sql('v')} AND v.id IS NOT ?
                    AND (v.applied = 1 OR EXISTS (SELECT 1 FROM lead_actions a WHERE a.vacancy_id = v.id
                                                   AND a.action IN ('responded', 'auto_responded')))
               ) WHERE answered_at >= ? ORDER BY answered_at DESC, id DESC LIMIT 1""")
    return conn.execute(sql, same_employer_params(employer_id, employer) + [exclude_id, cutoff]).fetchone()


def skip_as_duplicate(conn: sqlite3.Connection, vacancy_id: int, of_hh_id: str) -> None:
    conn.execute("UPDATE vacancies SET status = 'skipped', skip_reason = ?, updated_at = ? WHERE id = ?",
                 (f"duplicate_employer:{of_hh_id}", utcnow(), vacancy_id))


def skip_as_responded(conn: sqlite3.Connection, vacancy_id: int, of_hh_id: str) -> None:
    """Its own reason, not `duplicate_employer:`: `dedup.revive_orphans` resurrects duplicates, and a company
    the owner has already written to must stay closed."""
    conn.execute("UPDATE vacancies SET status = 'skipped', skip_reason = ?, updated_at = ? WHERE id = ?",
                 (f"employer_responded:{of_hh_id}", utcnow(), vacancy_id))


# --- cover letters ----------------------------------------------------------------

def leads_without_letter(conn: sqlite3.Connection, threshold: int, limit: int | None = None) -> list[sqlite3.Row]:
    sql = """SELECT v.*, e.total, e.verdict, e.pitch_hint, e.company_kind, e.ip_gph_possible, e.employment_hint
             FROM vacancies v JOIN evaluations e ON e.vacancy_id = v.id
             LEFT JOIN cover_letters c ON c.vacancy_id = v.id
             WHERE v.status IN ('evaluated', 'sent') AND e.total >= ? AND c.id IS NULL
             ORDER BY e.total DESC, v.published_at DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (threshold,)).fetchall()


def save_cover_letter(conn: sqlite3.Connection, vacancy_id: int, text: str, model_note: str | None = None,
                      rules_hash: str | None = None) -> None:
    """Store the letter. `rules_hash` says which version of the rules wrote it (decision #46)."""
    conn.execute(
        """INSERT INTO cover_letters(vacancy_id, text, model_note, created_at, rules_hash) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(vacancy_id) DO UPDATE SET text = excluded.text, model_note = excluded.model_note,
                                                created_at = excluded.created_at,
                                                rules_hash = excluded.rules_hash""",
        (vacancy_id, text, model_note, utcnow(), rules_hash),
    )


def get_cover_letter(conn: sqlite3.Connection, vacancy_id: int) -> str | None:
    row = conn.execute("SELECT text FROM cover_letters WHERE vacancy_id = ?", (vacancy_id,)).fetchone()
    return row["text"] if row else None


# --- digests & feedback -------------------------------------------------------------

LEAD_SELECT = """SELECT v.*, e.tech_score, e.role_score, e.lead_score, e.total, e.ip_gph_possible, e.is_agency,
                        e.employment_hint, e.company_kind, e.verdict, e.pitch_hint, e.red_flags,
                        (SELECT text FROM cover_letters c WHERE c.vacancy_id = v.id) AS letter,
                        (SELECT rules_hash FROM cover_letters c WHERE c.vacancy_id = v.id) AS letter_rules,
                        (SELECT brief FROM employers emp WHERE emp.employer_id = v.employer_id AND emp.found = 1)
                            AS company_brief,
                        (SELECT CAST(julianday('now') - julianday(MIN(o.first_seen_at)) AS INTEGER)
                           FROM vacancies o
                          WHERE o.site = 'hh' AND casefold(o.title) = casefold(v.title)
                            AND ((v.employer_id IS NOT NULL AND o.employer_id = v.employer_id)
                                 OR (v.employer_id IS NULL AND casefold(o.employer) = casefold(v.employer))))
                            AS searching_days
                 FROM vacancies v JOIN evaluations e ON e.vacancy_id = v.id"""


# --- the lead queue -----------------------------------------------------------------
# Leads that did not fit into a digest are NOT written off (`reject_below` only clears what is below the
# threshold), so `evaluated` is a queue that outlives the day. Order in it is score plus a small bonus for
# waiting: a fresh strong vacancy always goes before a stale weak one, but a week of waiting is worth 7 points,
# so the tail cannot starve forever. `evaluations` has one row per vacancy, so `created_at` is when it queued.
PRIORITY_SQL = ("(e.total + MIN(CAST(julianday('now') - julianday(e.created_at) AS INTEGER), {bonus}))")


def lead_queue(conn: sqlite3.Connection, threshold: int, limit: int | None = None, *, wait_bonus_max: int = 7,
               offset: int = 0) -> list[sqlite3.Row]:
    """The pending leads, best first by priority. `offset` skips the ones already taken (the digest tail)."""
    prio = PRIORITY_SQL.format(bonus=int(wait_bonus_max))
    sql = (LEAD_SELECT.replace(" FROM vacancies v",
                               ", CAST(julianday('now') - julianday(e.created_at) AS INTEGER) AS waiting_days"
                               " FROM vacancies v")
           + " WHERE v.status = 'evaluated' AND e.total >= ? "
           f"ORDER BY {prio} DESC, e.total DESC, v.published_at DESC")
    if limit is not None:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    elif offset:
        sql += f" LIMIT -1 OFFSET {int(offset)}"
    return conn.execute(sql, (threshold,)).fetchall()


def queue_size(conn: sqlite3.Connection, threshold: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM vacancies v JOIN evaluations e ON e.vacancy_id = v.id "
                            "WHERE v.status = 'evaluated' AND e.total >= ?", (threshold,)).fetchone()[0])


def letters_written_today(conn: sqlite3.Connection) -> int:
    """Letters written since local midnight — the daily quota must hold across all three sittings, not per run."""
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return int(conn.execute("SELECT COUNT(*) FROM cover_letters WHERE created_at >= ?",
                            (start.replace(microsecond=0).isoformat(),)).fetchone()[0])


def expire_queue(conn: sqlite3.Connection, ttl_days: int) -> int:
    """Leads nobody got to within `ttl_days` leave the queue: by then the vacancy is usually gone."""
    if ttl_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).replace(microsecond=0).isoformat()
    cur = conn.execute(
        "UPDATE vacancies SET status = 'rejected', skip_reason = 'queue_expired', updated_at = ? "
        "WHERE status = 'evaluated' AND id IN (SELECT vacancy_id FROM evaluations WHERE created_at < ?)",
        (utcnow(), cutoff))
    return cur.rowcount


def evaluated_leads(conn: sqlite3.Connection, threshold: int, limit: int | None = None,
                    site: str | None = None) -> list[sqlite3.Row]:
    sql = LEAD_SELECT + " WHERE v.status = 'evaluated' AND e.total >= ?"
    params: list = [threshold]
    if site:
        sql += " AND v.site = ?"
        params.append(site)
    sql += " ORDER BY e.total DESC, v.published_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def lead_by_hh_id(conn: sqlite3.Connection, hh_id: str) -> sqlite3.Row | None:
    return conn.execute(LEAD_SELECT + " WHERE v.hh_id = ?", (hh_id,)).fetchone()


def reject_below(conn: sqlite3.Connection, threshold: int) -> int:
    cur = conn.execute(
        """UPDATE vacancies SET status = 'rejected', updated_at = ? WHERE status = 'evaluated'
           AND id IN (SELECT vacancy_id FROM evaluations WHERE total < ?)""", (utcnow(), threshold))
    return cur.rowcount


def last_digest(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()


def last_daily_digest(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The last *noon* digest, ignoring instant sends between them (v9.8)."""
    return conn.execute("SELECT * FROM digests WHERE note IS NULL OR note NOT LIKE 'instant:%' "
                        "ORDER BY id DESC LIMIT 1").fetchone()


def evaluations_since_last_digest(conn: sqlite3.Connection, *, daily_only: bool = False) -> int:
    """How many vacancies were scored since the last digest — "проверено N" in its header.

    `daily_only` measures from the last noon digest: since v9.8 leads also go out right after every sitting,
    and counting from those would turn the daily summary into "since the last sitting".
    """
    last = last_daily_digest(conn) if daily_only else last_digest(conn)
    since = last["sent_at"] if last else "1970-01-01T00:00:00+00:00"
    return int(conn.execute("SELECT COUNT(*) FROM evaluations WHERE created_at > ?", (since,)).fetchone()[0])


def leads_sent_today(conn: sqlite3.Connection) -> int:
    """Leads sent since local midnight, instant sends and the noon digest together.

    The daily quota is a property of the day, not of one message: without this the noon digest would happily
    send another full quota on top of what the sittings already delivered.
    """
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return int(conn.execute(
        "SELECT COUNT(*) FROM digest_items di JOIN digests d ON d.id = di.digest_id WHERE d.sent_at >= ?",
        (start.replace(microsecond=0).isoformat(),)).fetchone()[0])


def create_digest(conn: sqlite3.Connection, items_count: int, collected_count: int, note: str | None = None) -> int:
    cur = conn.execute("INSERT INTO digests(sent_at, items_count, collected_count, note) VALUES (?, ?, ?, ?)",
                       (utcnow(), items_count, collected_count, note))
    return int(cur.lastrowid)


def add_digest_item(conn: sqlite3.Connection, digest_id: int, vacancy_id: int, position: int, tg_message_id: int | None,
                    letter_message_id: int | None = None) -> None:
    conn.execute("INSERT OR REPLACE INTO digest_items(digest_id, vacancy_id, position, tg_message_id, letter_message_id) "
                 "VALUES (?, ?, ?, ?, ?)", (digest_id, vacancy_id, position, tg_message_id, letter_message_id))
    conn.execute("UPDATE vacancies SET status = 'sent', updated_at = ? WHERE id = ?", (utcnow(), vacancy_id))


# --- "they have been searching for a while" (v9.7) ---------------------------------------

# A vacancy hh shows as published today may have been re-posted for weeks: hh bumps the date on every
# refresh, so the age of the posting says nothing (in 3745 of 4059 rows we first saw it on its own
# publication date, and not one was older than 21 days). What does say something is our own history:
# how long this employer has been advertising this same role to us. 14+ days means they cannot fill
# the seat, which is exactly when a contract starts to look like the obvious answer (decision #44).
_SEARCHING_DAYS_SQL = """
    SELECT CAST(julianday('now') - julianday(MIN(v.first_seen_at)) AS INTEGER) AS days
    FROM vacancies v
    WHERE v.site = 'hh' AND casefold(v.title) = casefold(?)
      AND (""" + "(? IS NOT NULL AND v.employer_id = ?) OR (? IS NULL AND casefold(v.employer) = casefold(?))" + """)
"""


def employer_searching_days(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """For how many days we have been seeing this employer advertise this very role.

    Title match is exact (case-folded) on purpose: a company hiring both a programmer and a fitter is
    not "searching long" for either. 0 means we are seeing it for the first time, which is not a signal.
    """
    emp_id = row["employer_id"] if "employer_id" in row.keys() else None
    name = row["employer"] if "employer" in row.keys() else None
    title = row["title"] or ""
    got = conn.execute(_SEARCHING_DAYS_SQL, (title, emp_id, emp_id, emp_id, name)).fetchone()
    return int(got["days"] or 0) if got else 0


# --- outcomes: what came back from the companies (v9.7) ----------------------------------

# Score bands the method is calibrated on. Kept here, not in the query, so the digest, /stats and
# any later threshold decision all slice the data the same way.
SCORE_BANDS: tuple[tuple[str, int, int], ...] = (("60-64", 60, 64), ("65-69", 65, 69), ("70-74", 70, 74), ("75+", 75, 1000))

_OUTCOMES_SQL = """
    SELECT v.id AS vacancy_id,
           e.total AS total,
           v.applied AS applied,
           v.has_chat AS has_chat,
           v.negotiation_state AS state,
           e.company_kind AS company_kind,
           e.is_agency AS is_agency,
           v.work_format AS work_format,
           v.area_name AS area_name,
           c.created_at AS letter_at,
           LENGTH(c.text) AS letter_len,
           MIN(d.sent_at) AS sent_at,
           (SELECT 1 FROM employers emp
             WHERE emp.employer_id = v.employer_id AND emp.found = 1) AS dossier,
           (SELECT MIN(ne.seen_at) FROM negotiation_events ne
             WHERE ne.vacancy_id = v.id AND ne.has_messages = 1) AS first_reply_at,
           (SELECT CAST(julianday('now') - julianday(MIN(o.first_seen_at)) AS INTEGER)
              FROM vacancies o
             WHERE o.site = 'hh' AND casefold(o.title) = casefold(v.title)
               AND ((v.employer_id IS NOT NULL AND o.employer_id = v.employer_id)
                    OR (v.employer_id IS NULL AND casefold(o.employer) = casefold(v.employer)))) AS searching_days
    FROM vacancies v
    JOIN evaluations e ON e.vacancy_id = v.id
    JOIN digest_items di ON di.vacancy_id = v.id
    JOIN digests d ON d.id = di.digest_id
    LEFT JOIN cover_letters c ON c.vacancy_id = v.id
    WHERE v.site = 'hh' AND d.sent_at >= ?
      AND EXISTS (SELECT 1 FROM lead_actions a WHERE a.vacancy_id = v.id
                  AND a.action IN ('responded', 'auto_responded'))
    GROUP BY v.id
"""


def outcome_rows(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    """One row per lead the owner actually wrote to, with everything the report slices by.

    Only leads he wrote to count — a lead he waved away says nothing about the score or about the letter.
    """
    return conn.execute(_OUTCOMES_SQL, (since_iso,)).fetchall()


def _age_days(row: sqlite3.Row) -> float:
    """How long the letter has had to produce an answer. Age is the confounder that breaks naive tables:
    measured 19.09, letters 0-1 days old answered 0 % and 9-10 days old 78-82 %."""
    started = row["letter_at"] or row["sent_at"]
    if not started:
        return 0.0
    try:
        when = datetime.fromisoformat(str(started))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400.0


def outcome_of(row: sqlite3.Row) -> str:
    """invited / refused / answered / silent / blind — what the company did about this letter."""
    if not row["applied"]:
        return "blind"       # sent outside hh.ru: the answer is invisible to us, never a failure
    state = (row["state"] or "").upper()
    if state == "INTERVIEW":
        return "invited"
    if state == "DISCARD":
        return "refused"
    return "answered" if row["has_chat"] else "silent"


MIN_CELL = 8   # below this a percentage is noise dressed as a finding, so the report prints the count instead


def outcome_by(rows: Sequence[sqlite3.Row], key: Callable[[sqlite3.Row], str | None],
               mature_days: int) -> list[dict[str, Any]]:
    """Cross-tab of outcomes by any feature, counting only letters old enough to have an answer.

    `rate` is None when the cell is too small to mean anything (`MIN_CELL`): printing "2 of 2 = 100 %"
    would invent a finding out of two observations.
    """
    buckets: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if _age_days(r) < mature_days:
            continue
        name = key(r)
        if name is not None:
            buckets.setdefault(name, []).append(r)
    out = []
    for name, cell in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        tracked = [r for r in cell if r["applied"]]
        answered = sum(1 for r in tracked if outcome_of(r) in ("answered", "invited"))
        out.append({
            "name": name,
            "n": len(cell),
            "tracked": len(tracked),
            "answered": answered,      # a refusal is an answer, but not the kind we are optimising for
            "invited": sum(1 for r in tracked if outcome_of(r) == "invited"),
            "refused": sum(1 for r in tracked if outcome_of(r) == "refused"),
            "rate": round(100 * answered / len(tracked)) if len(tracked) >= MIN_CELL else None,
        })
    return out


def maturing(rows: Sequence[sqlite3.Row], mature_days: int) -> int:
    """Letters too young to count yet — reported so their silence is never read as a failure."""
    return sum(1 for r in rows if _age_days(r) < mature_days)


def reply_delay_curve(rows: Sequence[sqlite3.Row]) -> list[tuple[str, int, int]]:
    """(bucket, letters, reacted) by letter age — the evidence the maturity threshold rests on.

    Here a refusal counts: the question is how long a company takes to react at all, and that is what
    says when a letter's silence stops being "too early" and starts being an answer in itself.
    """
    buckets = (("0-1 дн.", 0, 2), ("2-4 дн.", 2, 5), ("5-7 дн.", 5, 8), ("8+ дн.", 8, 10_000))
    out = []
    for name, lo, hi in buckets:
        cell = [r for r in rows if r["applied"] and lo <= _age_days(r) < hi]
        out.append((name, len(cell), sum(1 for r in cell if outcome_of(r) != "silent")))
    return out


def outcome_stats(conn: sqlite3.Connection, since_iso: str) -> list[dict[str, int | str]]:
    """Per score band: how many leads the owner wrote to, and what the companies did about it.

    Only leads the owner actually wrote to count — a lead he waved away says nothing about the score.
    `blind` are the ones sent outside hh.ru (no `applied`), where the answer is invisible to us: they
    are reported separately instead of quietly diluting the conversion.

    `answered` excludes refusals. It used to be plain `has_chat`, which counted a rejection as an answer —
    harmless while we knew of 8 refusals, misleading once the full sync found 22 of them among 36 replies
    (v9.10). The columns are now disjoint: blind + silent + answered + invited + refused = written.
    """
    rows = outcome_rows(conn, since_iso)
    out = []
    for name, lo, hi in SCORE_BANDS:
        band = [r for r in rows if lo <= int(r["total"] or 0) <= hi]
        tracked = [r for r in band if r["applied"]]
        out.append({
            "band": name,
            "written": len(band),
            "blind": len(band) - len(tracked),
            "answered": sum(1 for r in tracked if outcome_of(r) == "answered"),
            "invited": sum(1 for r in tracked if outcome_of(r) == "invited"),
            "refused": sum(1 for r in tracked if outcome_of(r) == "refused"),
            "silent": sum(1 for r in tracked if outcome_of(r) == "silent"),
        })
    return out


def invited_since(conn: sqlite3.Connection, since_iso: str) -> int:
    """How many companies invited the owner to talk since `since_iso` — the digest header line."""
    return sum(int(b["invited"]) for b in outcome_stats(conn, since_iso))


# --- lead lifecycle (v5) --------------------------------------------------------------

CLOSING_ACTIONS = ("responded", "auto_responded", "disliked", "closed_stale")


def add_action(conn: sqlite3.Connection, vacancy_id: int, action: str, reason: str | None = None) -> None:
    conn.execute("INSERT INTO lead_actions(vacancy_id, action, reason, created_at) VALUES (?, ?, ?, ?)",
                 (vacancy_id, action, reason, utcnow()))


def lead_messages(conn: sqlite3.Connection, vacancy_id: int) -> tuple[int | None, int | None]:
    """(card message id, letter message id) of the latest digest item for this vacancy."""
    row = conn.execute("SELECT tg_message_id, letter_message_id FROM digest_items WHERE vacancy_id = ? "
                       "ORDER BY digest_id DESC LIMIT 1", (vacancy_id,)).fetchone()
    return (row["tg_message_id"], row["letter_message_id"]) if row else (None, None)


_OPEN_LEADS_SQL = """
    SELECT v.id, v.hh_id, v.title, v.employer, v.url, v.applied, e.total, d.sent_at, di.tg_message_id, di.letter_message_id,
           EXISTS(SELECT 1 FROM lead_actions a WHERE a.vacancy_id = v.id AND a.action = 'deferred') AS deferred
    FROM vacancies v
    JOIN evaluations e ON e.vacancy_id = v.id
    JOIN digest_items di ON di.vacancy_id = v.id
    JOIN digests d ON d.id = di.digest_id
    WHERE v.status = 'sent'
      AND NOT EXISTS (SELECT 1 FROM lead_actions a WHERE a.vacancy_id = v.id
                      AND a.action IN ('responded', 'auto_responded', 'disliked', 'closed_stale'))
    GROUP BY v.id
"""


def open_leads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Sent leads the owner has not acted on yet; deferred ones last, then oldest first."""
    return conn.execute(_OPEN_LEADS_SQL + " ORDER BY deferred, d.sent_at, e.total DESC").fetchall()


def open_leads_older_than(conn: sqlite3.Connection, cutoff_iso: str) -> list[sqlite3.Row]:
    return conn.execute(_OPEN_LEADS_SQL + " HAVING d.sent_at < ? ORDER BY d.sent_at", (cutoff_iso,)).fetchall()


def pending_auto_closes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Open leads the owner has since responded to on hh.ru (applied=1) — to be collapsed in the chat."""
    return conn.execute(_OPEN_LEADS_SQL + " HAVING v.applied = 1").fetchall()


def is_lead_open(conn: sqlite3.Connection, vacancy_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM vacancies v WHERE v.id = ? AND v.status = 'sent' AND NOT EXISTS ("
        "SELECT 1 FROM lead_actions a WHERE a.vacancy_id = v.id AND a.action IN ('responded','auto_responded','disliked','closed_stale'))",
        (vacancy_id,)).fetchone()
    return row is not None


def add_feedback(conn: sqlite3.Connection, vacancy_id: int, value: int, reason: str | None = None) -> None:
    conn.execute("INSERT INTO feedback(vacancy_id, value, reason, created_at) VALUES (?, ?, ?, ?)",
                 (vacancy_id, value, reason, utcnow()))


def update_feedback_reason(conn: sqlite3.Connection, vacancy_id: int, reason: str | None) -> None:
    conn.execute("""UPDATE feedback SET reason = ? WHERE id = (SELECT id FROM feedback WHERE vacancy_id = ? AND value < 0
                    ORDER BY id DESC LIMIT 1)""", (reason, vacancy_id))


def recent_skipped(conn: sqlite3.Connection, limit: int = 15) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT hh_id, title, employer, status, skip_reason, triage_note FROM vacancies
           WHERE status IN ('skipped', 'rejected') AND COALESCE(skip_reason, '') != 'applied'
           ORDER BY updated_at DESC LIMIT ?""", (limit,)).fetchall()


def vacancy_by_id(conn: sqlite3.Connection, vacancy_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,)).fetchone()


# --- runs -----------------------------------------------------------------------

def _today_start_utc() -> str:
    start_local = datetime.combine(datetime.now(TZ).date(), time(0, 0), tzinfo=TZ)
    return start_local.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def run_starts_today(conn: sqlite3.Connection) -> list[datetime]:
    """Start times (aware, Europe/Moscow) of every run started today, any trigger or status."""
    rows = conn.execute("SELECT started_at FROM runs WHERE started_at >= ? ORDER BY id", (_today_start_utc(),)).fetchall()
    return [datetime.fromisoformat(r["started_at"]).astimezone(TZ) for r in rows]


def day_totals(conn: sqlite3.Connection, threshold: int) -> dict[str, int]:
    """Today's totals for the end-of-day summary: page loads, new vacancies, leads scored at or above the threshold."""
    start = _today_start_utc()
    r = conn.execute("SELECT COALESCE(SUM(page_loads), 0) AS p, COALESCE(SUM(collected), 0) AS c FROM runs WHERE started_at >= ?",
                     (start,)).fetchone()
    leads = conn.execute("SELECT COUNT(*) AS n FROM evaluations WHERE created_at >= ? AND total >= ?", (start, threshold)).fetchone()
    return {"page_loads": int(r["p"]), "new_vacancies": int(r["c"]), "leads": int(leads["n"])}


def work_totals(conn: sqlite3.Connection, since: datetime) -> dict[str, int]:
    """Work done since `since` (aware) for the digest's one-line report: scheduled sittings and page loads (all runs)."""
    since_utc = since.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    r = conn.execute("SELECT COUNT(*) FILTER (WHERE trigger = 'schedule') AS s, COALESCE(SUM(page_loads), 0) AS p "
                     "FROM runs WHERE started_at >= ?", (since_utc,)).fetchone()
    return {"sittings": int(r["s"]), "page_loads": int(r["p"])}


def running_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY id DESC LIMIT 1").fetchone()

def fail_stale_runs(conn: sqlite3.Connection, max_age_hours: float = 3.0, trigger: str | None = None) -> int:
    """Mark 'running' runs older than max_age_hours as failed (process was killed externally).

    `trigger` narrows it to one kind of run: the service calls it with `'schedule'` and 0 hours at start-up,
    because a scheduled run cannot outlive the service that ran it.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).replace(microsecond=0).isoformat()
    sql = ("UPDATE runs SET status = 'failed', finished_at = ?, error = 'прерван внешне (процесс убит)' "
           "WHERE status = 'running' AND started_at < ?")
    params: list = [utcnow(), cutoff]
    if trigger:
        sql += " AND trigger = ?"
        params.append(trigger)
    return conn.execute(sql, params).rowcount


def start_run(conn: sqlite3.Connection, trigger: str) -> int:
    cur = conn.execute("INSERT INTO runs(started_at, status, trigger) VALUES (?, 'running', ?)", (utcnow(), trigger))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, error: str | None = None, **metrics: Any) -> None:
    cols = {k: v for k, v in metrics.items() if k in ("collected", "prefiltered", "evaluated", "sent", "bridge_calls", "page_loads")}
    sets = ", ".join(f"{k} = ?" for k in cols)
    sql = f"UPDATE runs SET finished_at = ?, status = ?, error = ?{', ' + sets if sets else ''} WHERE id = ?"
    conn.execute(sql, (utcnow(), status, error, *cols.values(), run_id))


def update_run(conn: sqlite3.Connection, run_id: int, **metrics: Any) -> None:
    cols = {k: v for k, v in metrics.items() if k in ("collected", "prefiltered", "evaluated", "sent", "bridge_calls", "page_loads")}
    if not cols:
        return
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*cols.values(), run_id))


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
