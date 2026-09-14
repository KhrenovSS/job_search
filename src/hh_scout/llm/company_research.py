"""What the open web says about an employer — one dossier per company, reused by every letter.

The only place the bridge is allowed to use web tools (`allow_web`): the CLI opens the employer's page on
hh.ru and, if it finds one, the company's own site. This runs **outside the owner's browser**, so it costs
no page loads from the daily cap and leaves no trace on his hh session — only bridge money (~$0.05 a company).

Cached in `employers` by hh.ru `company.id`, the same key as "one lead per company", so a company is researched
once and every vacancy of it gets the same dossier. A failure is never fatal: the letter is simply written
without the dossier, exactly as before v9.0.

CLI:  python -m hh_scout.llm.company_research (--employer-id ID | --hh-id ID) [--force] [--preview]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from hh_scout.browser.hh_pages import strip_html
from hh_scout.config import Settings
from hh_scout.db import utcnow
from hh_scout.llm.bridge_client import BridgeClient, BridgeError, extract_json
from hh_scout.llm.prompts import load_prompt_body
from hh_scout.llm.schemas import CompanyBrief

log = logging.getLogger(__name__)

PROMPT = "company_research.md"
MAX_SUMMARY_CHARS = 1500
_RETRY_NOTE = ("\n\nПредыдущий ответ не прошёл валидацию: {error}. "
               "Верни ТОЛЬКО корректный JSON-объект по схеме, без пояснений.")


def employer_url(employer_id: str) -> str:
    return f"https://hh.ru/employer/{employer_id}"


def cached(conn: sqlite3.Connection, employer_id: str, ttl_days: int) -> CompanyBrief | None:
    """The stored dossier if it is still fresh. A stored `found: false` counts too — do not pay twice for nothing."""
    row = conn.execute("SELECT brief, researched_at FROM employers WHERE employer_id = ?", (employer_id,)).fetchone()
    if row is None:
        return None
    if ttl_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).replace(microsecond=0).isoformat()
        if (row["researched_at"] or "") < cutoff:
            return None
    try:
        return CompanyBrief.model_validate_json(row["brief"] or "{}")
    except ValidationError:
        return None


def save(conn: sqlite3.Connection, employer_id: str, name: str | None, brief: CompanyBrief) -> None:
    conn.execute(
        """INSERT INTO employers(employer_id, name, found, brief, sources, researched_at) VALUES (?,?,?,?,?,?)
           ON CONFLICT(employer_id) DO UPDATE SET name = excluded.name, found = excluded.found,
               brief = excluded.brief, sources = excluded.sources, researched_at = excluded.researched_at""",
        (employer_id, name, int(brief.found), brief.model_dump_json(),
         json.dumps(brief.sources, ensure_ascii=False), utcnow()),
    )


def payload(row: sqlite3.Row) -> dict:
    raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    return {
        "employer": row["employer"],
        "employer_id": row["employer_id"],
        "employer_url": employer_url(row["employer_id"]),
        "city": row["area_name"],
        "vacancy_title": row["title"],
        "vacancy_summary": strip_html(raw.get("description"))[:MAX_SUMMARY_CHARS],
    }


class CompanyResearcher:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, bridge: BridgeClient | None = None) -> None:
        self.s = settings
        self.conn = conn
        self.bridge = bridge or BridgeClient(settings)
        self.calls = 0

    def for_row(self, row: sqlite3.Row, *, force: bool = False) -> CompanyBrief | None:
        """The dossier for this vacancy's employer, from cache or the web. None if it cannot be had."""
        if not self.s.company_research_enabled:
            return None
        site = row["site"] if "site" in row.keys() and row["site"] else "hh"
        if site != "hh" or not row["employer_id"]:
            return None  # profi.ru clients are private people; cards without an id have no stable key
        if not force:
            hit = cached(self.conn, row["employer_id"], self.s.company_research_ttl_days)
            if hit is not None:
                return hit
        brief = self._ask(payload(row))
        if brief is None:
            return None
        with self.conn:
            save(self.conn, row["employer_id"], row["employer"], brief)
        log.info("Досье на компанию %s (%s): %s", row["employer"] or "—", row["employer_id"],
                 (brief.what_they_do or brief.note or "ничего не нашлось")[:120])
        return brief

    def _ask(self, data: dict) -> CompanyBrief | None:
        system_text = load_prompt_body(self.s.prompts_dir / PROMPT)
        user_text = json.dumps(data, ensure_ascii=False)
        for attempt in (0, 1):
            try:
                answer = self.bridge.complete(system_text, user_text, model=self.s.company_research_model,
                                              allow_web=True, max_turns=self.s.company_research_max_turns)
            except BridgeError as e:
                log.warning("Разведка по компании не удалась (мост): %s", e)
                return None
            self.calls += 1
            try:
                return CompanyBrief.model_validate(extract_json(answer))
            except (ValueError, ValidationError) as e:
                if attempt:
                    log.warning("Разведка по компании: ответ не прошёл валидацию дважды — пропускаю (%s)", e)
                    return None
                user_text += _RETRY_NOTE.format(error=str(e)[:200])
        return None


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser()
    ap.add_argument("--employer-id", help="hh.ru company.id")
    ap.add_argument("--hh-id", help="any vacancy of the company")
    ap.add_argument("--force", action="store_true", help="ignore the cache and research again")
    ap.add_argument("--preview", action="store_true", help="print the dossier as JSON")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    if args.hh_id:
        row = conn.execute("SELECT * FROM vacancies WHERE hh_id = ?", (args.hh_id,)).fetchone()
    elif args.employer_id:
        row = conn.execute("SELECT * FROM vacancies WHERE employer_id = ? ORDER BY id DESC LIMIT 1",
                           (args.employer_id,)).fetchone()
    else:
        ap.error("нужен --employer-id или --hh-id")
    if row is None:
        print("Такой компании или вакансии нет в базе")
        return 1
    brief = CompanyResearcher(settings, conn).for_row(row, force=args.force)
    if brief is None:
        print("Досье собрать не удалось (выключено, нет employer_id или мост не ответил)")
        return 1
    if args.preview:
        print(brief.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
