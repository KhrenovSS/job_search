"""Thin DAO over SQLite for the pipeline. No business rules here — just SQL."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timezone
from typing import Any

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
        """INSERT INTO vacancies(hh_id, title, employer, url, area_name, work_format, employment,
                                 salary_from, salary_to, salary_raw, published_at, source, search_pass,
                                 status, skip_reason, applied, first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (card.hh_id, card.title, card.employer, card.url, card.area_name, card.work_format, card.employment,
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


def mark_applied(conn: sqlite3.Connection, hh_id: str, *, has_chat: bool, title: str | None = None,
                 employer: str | None = None, url: str | None = None) -> None:
    """Flag a vacancy the owner already responded to; creates a stub row if unknown."""
    now = utcnow()
    if vacancy_exists(conn, hh_id):
        conn.execute(
            """UPDATE vacancies SET applied = 1, has_chat = MAX(has_chat, ?), updated_at = ?,
                      status = CASE WHEN status IN ('new', 'prefiltered', 'evaluated') THEN 'skipped' ELSE status END,
                      skip_reason = CASE WHEN status IN ('new', 'prefiltered', 'evaluated') THEN 'applied' ELSE skip_reason END
               WHERE hh_id = ?""",
            (int(has_chat), now, hh_id),
        )
    else:
        conn.execute(
            """INSERT INTO vacancies(hh_id, title, employer, url, source, search_pass, status, skip_reason,
                                     applied, has_chat, first_seen_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (hh_id, title or "(отклик)", employer, url or f"https://hh.ru/vacancy/{hh_id}", "negotiations",
             "negotiations", "skipped", "applied", 1, int(has_chat), now, now),
        )


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
               "closedForApplicants", "userLabels")


def trim_vacancy_view(raw: dict[str, Any]) -> dict[str, Any]:
    out = {k: raw.get(k) for k in DETAIL_KEYS if k in raw}
    company = raw.get("company") if isinstance(raw.get("company"), dict) else {}
    out["company"] = {k: company.get(k) for k in ("id", "name", "visibleName", "@trusted") if k in company}
    addr = raw.get("address") if isinstance(raw.get("address"), dict) else {}
    out["address"] = {k: addr.get(k) for k in ("city", "street", "building", "displayName") if k in addr}
    return out


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
                  area_name = COALESCE(?, area_name), work_format = ?, employment = ?,
                  salary_from = ?, salary_to = ?, salary_raw = COALESCE(?, salary_raw),
                  applied = MAX(applied, ?), status = ?, skip_reason = ?, updated_at = ?
           WHERE hh_id = ?""",
        (json.dumps(trim_vacancy_view(detail.raw), ensure_ascii=False), detail.title, detail.employer, detail.area_name,
         detail.work_format, detail.employment, sal.from_net, sal.to_net,
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


def save_cover_letter(conn: sqlite3.Connection, vacancy_id: int, text: str, model_note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO cover_letters(vacancy_id, text, model_note, created_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(vacancy_id) DO UPDATE SET text = excluded.text, model_note = excluded.model_note,
                                                created_at = excluded.created_at""",
        (vacancy_id, text, model_note, utcnow()),
    )


def get_cover_letter(conn: sqlite3.Connection, vacancy_id: int) -> str | None:
    row = conn.execute("SELECT text FROM cover_letters WHERE vacancy_id = ?", (vacancy_id,)).fetchone()
    return row["text"] if row else None


# --- digests & feedback -------------------------------------------------------------

LEAD_SELECT = """SELECT v.*, e.tech_score, e.role_score, e.lead_score, e.total, e.ip_gph_possible, e.is_agency,
                        e.employment_hint, e.company_kind, e.verdict, e.pitch_hint, e.red_flags,
                        (SELECT text FROM cover_letters c WHERE c.vacancy_id = v.id) AS letter
                 FROM vacancies v JOIN evaluations e ON e.vacancy_id = v.id"""


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


def evaluations_since_last_digest(conn: sqlite3.Connection) -> int:
    last = last_digest(conn)
    since = last["sent_at"] if last else "1970-01-01T00:00:00+00:00"
    return int(conn.execute("SELECT COUNT(*) FROM evaluations WHERE created_at > ?", (since,)).fetchone()[0])


def create_digest(conn: sqlite3.Connection, items_count: int, collected_count: int, note: str | None = None) -> int:
    cur = conn.execute("INSERT INTO digests(sent_at, items_count, collected_count, note) VALUES (?, ?, ?, ?)",
                       (utcnow(), items_count, collected_count, note))
    return int(cur.lastrowid)


def add_digest_item(conn: sqlite3.Connection, digest_id: int, vacancy_id: int, position: int, tg_message_id: int | None,
                    letter_message_id: int | None = None) -> None:
    conn.execute("INSERT OR REPLACE INTO digest_items(digest_id, vacancy_id, position, tg_message_id, letter_message_id) "
                 "VALUES (?, ?, ?, ?, ?)", (digest_id, vacancy_id, position, tg_message_id, letter_message_id))
    conn.execute("UPDATE vacancies SET status = 'sent', updated_at = ? WHERE id = ?", (utcnow(), vacancy_id))


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


def running_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY id DESC LIMIT 1").fetchone()

def fail_stale_runs(conn: sqlite3.Connection, max_age_hours: float = 3.0) -> int:
    """Mark 'running' runs older than max_age_hours as failed (process was killed externally)."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).replace(microsecond=0).isoformat()
    cur = conn.execute(
        "UPDATE runs SET status = 'failed', finished_at = ?, error = 'прерван внешне (процесс убит)' "
        "WHERE status = 'running' AND started_at < ?",
        (utcnow(), cutoff),
    )
    return cur.rowcount


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
