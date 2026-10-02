"""Cheap rule-based prefilter — no browser, no AI.

Applies to vacancies in status `new`. Every rejection records a `skip_reason`; survivors move to
`triage` where the AI decides (from the card alone) whether the page is worth opening.
Rules are deliberately conservative: when in doubt, let the AI decide. Salary is not a rule —
vacancies are leads for the owner's contracting work, a low salary says nothing about the lead.

CLI:  python -m hh_scout.pipeline.prefilter [--dry-run] [--show-skipped] [--requeue-reason REASON --days N] [--defense-sweep]
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass

from hh_scout.config import BLOCKED_REGIONS, TITLE_KEEP_WORDS, TITLE_REQUIRED_ANY, TITLE_STOP_WORDS, Settings
from hh_scout.db import transaction
from hh_scout.hh.areas import blocked_region
from hh_scout.pipeline import defense, home_region, repo
from hh_scout.pipeline.rows import lead_kind

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CardFacts:
    hh_id: str
    title: str
    applied: bool
    archived: bool
    company: bool = False   # a company-channel card (v9.13): the title names the company's trade, not a programmer
    region: str | None = None  # the blocked region the card lies in (`hh.areas.blocked_region`), decision #65
    employer: str | None = None  # the company name — the defence-industry rule reads it (`pipeline.defense`), decision #72
    outside: str | None = None   # the place outside the home regions the card lies in (`pipeline.home_region`), decision #77


# Stop words match only at the start of a word: "водитель" must not hit "руководитель".
_STOP_RES = [(w, re.compile(r"(?<!\w)" + re.escape(w.strip()), re.I)) for w in TITLE_STOP_WORDS]


def decide(card: CardFacts) -> str | None:
    """Return a skip reason, or None if the card passes."""
    if card.applied:
        return "applied"
    if card.archived:
        return "archived"
    if card.region:
        return repo.REGION_PREFIX + card.region   # the owner does not work there — whatever the title says
    if card.outside:
        return home_region.PREFIX + card.outside  # home regions only while the owner is away (decision #77)
    hit = defense.match(card.employer)
    if hit:
        return defense.name_reason(hit)   # a defence enterprise gets no letter, whatever the title says (decision #72)
    title = card.title.casefold()
    keep = any(k in title for k in TITLE_KEEP_WORDS)
    if not keep:
        for w, rx in _STOP_RES:
            if rx.search(title):
                return f"stopword:{w.strip()}"
    if not card.company and not any(k in title for k in TITLE_REQUIRED_ANY):
        return "no_engineering_title"   # a company card («Сборщик шкафов») is judged by the company, not the title
    # Neither salary nor work format is a rule: vacancies are leads for contracting, not jobs to take.
    # Rotation work (fly_in_fly_out) used to be dropped here — 467 vacancies, 12% of everything skipped —
    # although it only says where the object is. Whether the programming part can be done from a desk is
    # a judgement call, so it belongs to the triage model now (decision #43).
    return None


def _facts(row: sqlite3.Row, home: home_region.Home | None = None) -> CardFacts:
    return CardFacts(hh_id=row["hh_id"], title=row["title"] or "", applied=bool(row["applied"]),
                     archived=(row["skip_reason"] == "archived"), company=(lead_kind(row) == "company"),
                     region=blocked_region(row["area_path"]), employer=row["employer"],
                     outside=home.hh_place(row["area_path"], row["area_name"]) if home else None)


def run(conn: sqlite3.Connection, settings: Settings, *, dry_run: bool = False) -> Counter:
    """Move `new` → `triage` or `skipped`. Returns a Counter of outcomes ('passed' or reason)."""
    outcomes: Counter = Counter()
    # hh.ru only: catalogue companies (site='owen') wait in `new` for their daily admission and have no vacancy
    # page to open — letting them through here sent DetailsFetcher to hh.ru/vacancy/owen:<id> (v9.14).
    rows = repo.list_vacancies(conn, "new", site="hh")
    home = home_region.Home.load(conn, settings)
    with transaction(conn):  # one commit for the whole batch, not one disk sync per card
        for row in rows:
            reason = decide(_facts(row, home))
            key = reason or "passed"
            outcomes[key.split(":")[0]] += 1
            if dry_run:
                continue
            if reason:
                repo.set_status(conn, row["hh_id"], "skipped", reason)
            else:
                repo.set_status(conn, row["hh_id"], "triage")
        if not dry_run:
            # Rows past `new` that turned out to lie in a blocked region (an old card whose page was opened before
            # the rule existed, a region added to the list, the plant pool) are withdrawn here as well.
            withdrawn = repo.skip_blocked_regions(conn, BLOCKED_REGIONS)
            if withdrawn:
                log.info("Закрытые регионы: снято с очереди %s", ", ".join(f"{k} — {v}" for k, v in withdrawn.items()))
            # Same for the defence industry (decision #72): rows of every source and stage whose employer the rule now
            # names, and the other rows of a company one of whose rows the triage, the evaluator or the dossier marked.
            swept = repo.skip_defense_employers(conn)
            if swept:
                log.info("Оборонка: снято с очереди %s", ", ".join(f"{k} — {v}" for k, v in swept.items()))
            # Home regions (decision #77): every source and stage, by hh.ru region id or by the words of the region.
            if home.active:
                gone = repo.skip_outside_home(conn, home.ids, home.stems)
                if gone:
                    log.info("Вне домашних регионов: снято с очереди %s", ", ".join(f"{k} — {v}" for k, v in gone.items()))
    log.info("Префильтр: %d карточек → %s", len(rows), dict(outcomes))
    return outcomes


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="only report, do not change statuses")
    ap.add_argument("--show-skipped", action="store_true", help="list skipped titles with reasons")
    ap.add_argument("--requeue-reason", metavar="REASON",
                    help="one-off after a rules change: cards skipped with this reason go back to triage")
    ap.add_argument("--days", type=int, default=14, help="--requeue-reason: only cards first seen within N days")
    ap.add_argument("--defense-sweep", action="store_true",
                    help="only the defence-industry sweep over rows already past the rules (decision #72); no other change")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    if args.requeue_reason:
        with conn:
            n = repo.requeue_skipped(conn, args.requeue_reason, args.days)
        print(f"Возвращено в triage: {n} (skipped/{args.requeue_reason}, не старше {args.days} дн.)")
        return 0
    if args.defense_sweep:
        with conn:
            swept = repo.skip_defense_employers(conn)
        print("Оборонка: снято", sum(swept.values()), dict(swept))
        return 0

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
