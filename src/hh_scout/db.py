"""SQLite connection and migrations.

Plain `sqlite3`, no ORM. Schema versions are tracked with `PRAGMA user_version`;
each migration is a function that receives an open connection. Migrations only
ever append to MIGRATIONS — never edit an applied one.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def utcnow() -> str:
    """ISO-8601 UTC timestamp with second precision, e.g. 2026-09-08T09:00:00+00:00."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str) -> sqlite3.Connection:
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(p) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(p) != ":memory:":
        # WAL + NORMAL: durable against crashes of the process, fsync only on checkpoint, not on every commit.
        # On the owner's HDD a FULL-sync commit costs ~90 ms; hundreds of them in a row starve the bot's connection.
        conn.execute("PRAGMA synchronous = NORMAL")
    # SQLite's lower()/NOCASE fold ASCII only; employer names are Cyrillic (used by repo.same_employer_sql)
    conn.create_function("casefold", 1, lambda s: s.casefold() if isinstance(s, str) else s, deterministic=True)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One real write transaction for a batch of statements.

    Connections run in autocommit (`isolation_level=None`), so `with conn:` commits nothing — every statement is
    its own commit with its own disk sync, and a loop of hundreds of them holds the database lock for seconds,
    long enough to starve another connection past its busy_timeout. Wrap batch writes in this instead.
    Nested use joins the outer transaction (the outer commit/rollback wins).
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _m001_initial(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE areas_cache (
            area_id    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            parent_id  INTEGER,
            fetched_at TEXT NOT NULL
        );
        CREATE INDEX idx_areas_name ON areas_cache(name);

        CREATE TABLE vacancies (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            hh_id          TEXT NOT NULL UNIQUE,
            title          TEXT NOT NULL,
            employer       TEXT,
            url            TEXT NOT NULL,
            area_name      TEXT,
            work_format    TEXT,              -- remote / hybrid / office / field / unknown
            employment     TEXT,              -- full / part / project / fly_in_fly_out / unknown (hh employment_form)
            salary_from    INTEGER,           -- RUB net, NULL if not stated
            salary_to      INTEGER,
            salary_raw     TEXT,              -- original salary JSON
            published_at   TEXT,
            source         TEXT NOT NULL,     -- search:<query idx> | similar_to_resume | negotiations
            search_pass    TEXT NOT NULL,     -- regional | remote | project | similar | negotiations
            raw_json       TEXT,              -- trimmed vacancyView from the vacancy page (see repo.trim_vacancy_view)
            status         TEXT NOT NULL DEFAULT 'new',
                -- new -> triage -> to_fetch -> prefiltered -> evaluated -> sent | rejected
                -- or skipped (with skip_reason) | evaluation_failed   (canon: docs/DATABASE.md)
            skip_reason    TEXT,
            applied        INTEGER NOT NULL DEFAULT 0,
            has_chat       INTEGER NOT NULL DEFAULT 0,
            first_seen_at  TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );
        CREATE INDEX idx_vacancies_status ON vacancies(status);

        CREATE TABLE evaluations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_id      INTEGER NOT NULL REFERENCES vacancies(id),
            tech_score      INTEGER NOT NULL,
            salary_score    INTEGER NOT NULL,
            format_score    INTEGER NOT NULL,
            total           INTEGER NOT NULL,   -- computed by code from the weights in config
            ip_gph_possible TEXT NOT NULL,      -- yes / maybe / no
            is_agency       INTEGER NOT NULL DEFAULT 0,
            employment_hint TEXT,               -- staff / project / unknown
            verdict         TEXT NOT NULL,
            red_flags       TEXT,               -- JSON array of strings
            model_note      TEXT,
            created_at      TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_eval_vacancy ON evaluations(vacancy_id);

        CREATE TABLE digests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at         TEXT NOT NULL,
            items_count     INTEGER NOT NULL,
            collected_count INTEGER NOT NULL,
            note            TEXT
        );

        CREATE TABLE digest_items (
            digest_id     INTEGER NOT NULL REFERENCES digests(id),
            vacancy_id    INTEGER NOT NULL REFERENCES vacancies(id),
            position      INTEGER NOT NULL,
            tg_message_id INTEGER,
            PRIMARY KEY (digest_id, vacancy_id)
        );

        CREATE TABLE feedback (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_id  INTEGER NOT NULL REFERENCES vacancies(id),
            value       INTEGER NOT NULL,       -- +1 / -1
            reason      TEXT,                   -- salary / format / stack / agency / NULL
            created_at  TEXT NOT NULL
        );

        CREATE TABLE runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at   TEXT NOT NULL,
            finished_at  TEXT,
            status       TEXT NOT NULL,         -- running / ok / failed
            trigger      TEXT NOT NULL,         -- schedule / manual
            collected    INTEGER, prefiltered INTEGER, evaluated INTEGER, sent INTEGER,
            bridge_calls INTEGER,
            page_loads   INTEGER,               -- pages opened in Firefox during this run
            error        TEXT
        );

        CREATE TABLE kv (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        COMMIT;
        """
    )


def _m002_triage_columns(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE vacancies ADD COLUMN triage_priority INTEGER")
    conn.execute("ALTER TABLE vacancies ADD COLUMN triage_note TEXT")


def _m003_lead_scoring(conn: sqlite3.Connection) -> None:
    """v3: vacancies are leads for contracting — new sub-scores and a cover-letter hint."""
    conn.execute("ALTER TABLE evaluations ADD COLUMN role_score INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE evaluations ADD COLUMN lead_score INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE evaluations ADD COLUMN company_kind TEXT")
    conn.execute("ALTER TABLE evaluations ADD COLUMN pitch_hint TEXT")


def _m004_cover_letters(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE cover_letters (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_id  INTEGER NOT NULL UNIQUE REFERENCES vacancies(id),
            text        TEXT NOT NULL,
            model_note  TEXT,
            created_at  TEXT NOT NULL
        );
        """
    )


def _m005_lead_actions(conn: sqlite3.Connection) -> None:
    """v5: lead lifecycle in the chat — actions on sent leads and the letter message id for cleanup."""
    conn.executescript(
        """
        CREATE TABLE lead_actions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_id  INTEGER NOT NULL REFERENCES vacancies(id),
            action      TEXT NOT NULL,   -- liked / disliked / responded / auto_responded / deferred / closed_stale
            reason      TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX idx_lead_actions_vacancy ON lead_actions(vacancy_id);
        """
    )
    conn.execute("ALTER TABLE digest_items ADD COLUMN letter_message_id INTEGER")


def _m006_site(conn: sqlite3.Connection) -> None:
    """v7: second source. `site` = hh | profi; profi rows use hh_id = 'profi:<order id>' (the UNIQUE stays valid)."""
    conn.execute("ALTER TABLE vacancies ADD COLUMN site TEXT NOT NULL DEFAULT 'hh'")
    conn.execute("CREATE INDEX idx_vacancies_site_status ON vacancies(site, status)")


def _m007_employer_id(conn: sqlite3.Connection) -> None:
    """v8: one lead per company. hh.ru `company.id` as a stable employer key; backfilled from the stored vacancyView."""
    conn.execute("ALTER TABLE vacancies ADD COLUMN employer_id TEXT")
    conn.execute("UPDATE vacancies SET employer_id = CAST(json_extract(raw_json, '$.company.id') AS TEXT) "
                 "WHERE site = 'hh' AND raw_json IS NOT NULL AND json_valid(raw_json) "
                 "AND json_extract(raw_json, '$.company.id') IS NOT NULL")
    conn.execute("CREATE INDEX idx_vacancies_employer_id ON vacancies(employer_id)")
    conn.execute("CREATE INDEX idx_vacancies_employer ON vacancies(employer)")


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _m001_initial,
    _m002_triage_columns,
    _m003_lead_scoring,
    _m004_cover_letters,
    _m005_lead_actions,
    _m006_site,
    _m007_employer_id,
]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, step in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        log.info("Применяю миграцию БД %d (%s)", version, step.__name__)
        # Each step manages its own transaction (executescript issues an implicit COMMIT first).
        step(conn)
        conn.execute(f"PRAGMA user_version = {version}")
    return conn.execute("PRAGMA user_version").fetchone()[0]


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    version = migrate(conn)
    log.info("БД %s открыта, версия схемы %d", path, version)
    return conn


# --- tiny key/value helpers used by several modules -------------------------

def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
