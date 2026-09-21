"""Channel `plant` (v9.15, decision #55): companies that run automation but do not hire a programmer.

The card triage closes a vacancy for a КИПиА fitter, an electrician on a production site, an operations engineer —
and until v9.15 that was the end of the company. Yet such a company has PLCs, panels and lines of its own and no
programmer of its own: for contract work that is a customer, not a miss. So a closed card with `plant: true` in the
verdict does not die: it becomes the company's lead-in-waiting (`skipped/plant_pool`, `lead_kind='company'`,
`search_pass='plant'`), and every run lets `PLANT_LEADS_PER_DAY` of them out of the pool (`to_fetch`, priority 3).
From there the row walks the company-lead path of v9.13: one vacancy page, `company_evaluation.md`,
`company_offer.md`.

One company — one plant row, and a company the owner already wrote to (or that already has a lead) is left alone.
The pool lives in `skipped` on purpose: those rows are inert for every stage, so no new status was needed.

CLI:  python -m hh_scout.pipeline.plant --backfill [--dry-run] [--days N]    # pool the cards closed before v9.15
      python -m hh_scout.pipeline.plant --admit [N]                          # let N out of the pool now
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3

from hh_scout.config import Settings
from hh_scout.db import transaction
from hh_scout.pipeline import dedup, repo

log = logging.getLogger(__name__)

# Backfill only: the cards closed before the triage learned the `plant` flag carry a free-text reason.
# Included — the company runs equipment itself; excluded — a bureau, a sales desk, a twin, an agency.
POOL_NOTE_RE = re.compile(r"КИП|эксплуат|обслуж|электромонт|электромехан|энергетик|метролог|наладчик|механик|технолог",
                          re.IGNORECASE)
NOT_POOL_NOTE_RE = re.compile(r"продаж|проект|документац|ПТО|смет|ВОЛС|связ|дубль|агентств|\bIT\b|аналитик|руковод|охран",
                              re.IGNORECASE)


def note_says_plant(note: str | None) -> bool:
    """Whether a pre-v9.15 triage reason describes a company that runs automation (backfill heuristic)."""
    text = note or ""
    return bool(POOL_NOTE_RE.search(text)) and not NOT_POOL_NOTE_RE.search(text)


def pool(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> bool:
    """Move a just-closed card into the plant pool if the company is free and has no plant row yet. Commits nothing.

    Returns True when the row was pooled. The row is the vacancy's card as it was closed by triage (`skipped/triage`).
    """
    if repo.PLANT_PASS not in settings.company_channels_set:
        return False
    if repo.plant_row_exists(conn, row["employer_id"], row["employer"]):
        return False
    if dedup.answered_employer(conn, settings, row) is not None:
        return False                    # the owner already wrote to this company
    if repo.employer_lead(conn, row["employer_id"], row["employer"], exclude_id=row["id"],
                          threshold=settings.score_threshold, repeat_days=settings.employer_repeat_days,
                          candidate_kind="company") is not None:
        return False                    # the company has a lead on its way — a vacancy or another company row
    repo.move_to_plant_pool(conn, row["id"])
    return True


def admit(conn: sqlite3.Connection, settings: Settings) -> int:
    """Daily gate from the pool to the page queue; rows whose company got covered meanwhile are skipped instead.
    Returns how many are now `to_fetch`."""
    if repo.PLANT_PASS not in settings.company_channels_set:
        return 0
    with transaction(conn):
        rows = repo.admit_plant_leads(conn, settings.plant_leads_per_day)
        admitted = 0
        for r in rows:
            fresh = repo.vacancy_by_id(conn, r["id"])
            if dedup.skip_if_covered(conn, settings, fresh) is None:
                admitted += 1
    if admitted:
        log.info("Эксплуатанты: допущено в очередь описаний %d компаний (по %d в день)", admitted, settings.plant_leads_per_day)
    return admitted


def backfill(conn: sqlite3.Connection, settings: Settings, *, days: int | None = None, dry_run: bool = False) -> list[sqlite3.Row]:
    """Pool the cards the triage closed before v9.15 whose reason says "operations": one row per employer, the
    newest. Returns the rows pooled (or that would be, with `dry_run`)."""
    sql = "SELECT * FROM vacancies WHERE site = 'hh' AND status = 'skipped' AND skip_reason = 'triage'"
    params: list = []
    if days is not None:
        sql += " AND first_seen_at >= ?"
        params.append(repo._ago(days))
    sql += " ORDER BY first_seen_at DESC, id DESC"
    seen: set[str] = set()
    chosen: list[sqlite3.Row] = []
    with transaction(conn):
        for row in conn.execute(sql, params).fetchall():
            if not note_says_plant(row["triage_note"]):
                continue
            key = (row["employer_id"] or (row["employer"] or "").casefold()) or f"#{row['id']}"
            if key in seen:
                continue
            seen.add(key)
            if dry_run:
                if not repo.plant_row_exists(conn, row["employer_id"], row["employer"]) \
                        and dedup.answered_employer(conn, settings, row) is None:
                    chosen.append(row)
                continue
            if pool(conn, settings, row):
                chosen.append(row)
    log.info("Эксплуатанты, задел: %s %d компаний", "нашлось бы" if dry_run else "в пул отправлено", len(chosen))
    return chosen


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true", help="pool the cards closed before v9.15 (by triage reason)")
    ap.add_argument("--days", type=int, default=None, help="backfill: only cards first seen within N days")
    ap.add_argument("--dry-run", action="store_true", help="backfill: show what would be pooled, change nothing")
    ap.add_argument("--admit", type=int, nargs="?", const=-1, default=None,
                    help="let N companies out of the pool now (default: PLANT_LEADS_PER_DAY)")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    if args.backfill:
        rows = backfill(conn, settings, days=args.days, dry_run=args.dry_run)
        for r in rows[:40]:
            print(f"{r['hh_id']:>10}  {(r['employer'] or '—')[:36]:36}  {(r['triage_note'] or '')[:60]}")
        if len(rows) > 40:
            print(f"… и ещё {len(rows) - 40}")
        print(f"{'нашлось бы' if args.dry_run else 'в пуле'}: {len(rows)}")
    if args.admit is not None:
        if args.admit >= 0:
            settings = settings.model_copy(update={"plant_leads_per_day": args.admit})
        print(f"допущено: {admit(conn, settings)}")
    t = repo.plant_totals(conn)
    print(f"эксплуатанты: всего {t['total']} · в пуле {t['pool']} · ждут страницы {t['to_fetch']} · отправлено {t['sent']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
