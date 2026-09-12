"""AI triage of search cards: decide which vacancy pages are worth opening.

Input: vacancies in status `triage` (passed the rule prefilter). Batches of `triage_batch_size`
cards go to the bridge with `prompts/card_triage.md`; verdicts move cards to `to_fetch` or
`skipped/triage`. A batch whose answer fails validation twice stays in `triage` (retried next run).

CLI:  python -m hh_scout.llm.triage [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass, field

from pydantic import ValidationError

from hh_scout.config import Settings
from hh_scout.hh.salary import normalize
from hh_scout.llm.bridge_client import BridgeClient, BridgeError, extract_json
from hh_scout.llm.prompts import render
from hh_scout.llm.schemas import TriageBatch, TriageVerdict
from hh_scout.pipeline import repo

log = logging.getLogger(__name__)

_RETRY_NOTE = ("\n\nПредыдущий ответ не прошёл валидацию: {error}. "
               "Верни ТОЛЬКО корректный JSON-массив по схеме, по одной записи на каждую карточку.")


@dataclass
class TriageStats:
    cards: int = 0
    opened: int = 0
    closed: int = 0
    failed_batches: int = 0
    bridge_calls: int = 0
    verdicts: list[tuple[str, TriageVerdict]] = field(default_factory=list)  # (title, verdict) for logs/CLI


def card_payload(row: sqlite3.Row) -> dict:
    return {
        "hh_id": row["hh_id"],
        "title": row["title"],
        "employer": row["employer"],
        "area": row["area_name"],
        "work_format": row["work_format"],
        "employment": row["employment"],
        "accept_temporary": bool(row["accept_temporary"]),
        "civil_law_contracts": json.loads(row["civil_law_contracts"] or "[]"),
        "salary": normalize(json.loads(row["salary_raw"]) if row["salary_raw"] else None).human(),
        "search_pass": row["search_pass"],
        "published_at": (row["published_at"] or "")[:10],
    }


class Triager:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, bridge: BridgeClient | None = None) -> None:
        self.s = settings
        self.conn = conn
        self.bridge = bridge or BridgeClient(settings)
        self.stats = TriageStats()

    def run(self, limit: int | None = None, *, dry_run: bool = False) -> TriageStats:
        rows = repo.list_vacancies(self.conn, "triage", limit)
        system_text = render(self.s.prompts_dir, "card_triage.md")
        for i in range(0, len(rows), self.s.triage_batch_size):
            batch = rows[i:i + self.s.triage_batch_size]
            verdicts = self._triage_batch(system_text, batch)
            if verdicts is None:
                self.stats.failed_batches += 1
                continue
            by_id = {r["hh_id"]: r for r in batch}
            with self.conn:
                for v in verdicts:
                    row = by_id.get(v.hh_id)
                    if row is None:
                        log.warning("Триаж вернул неизвестный hh_id %s — игнорирую", v.hh_id)
                        continue
                    self.stats.cards += 1
                    self.stats.opened += int(v.open)
                    self.stats.closed += int(not v.open)
                    self.stats.verdicts.append((row["title"], v))
                    if not dry_run:
                        repo.save_triage(self.conn, v.hh_id, open_it=v.open, priority=v.priority, note=v.reason)
                missing = set(by_id) - {v.hh_id for v in verdicts}
                if missing:
                    log.warning("Триаж не вернул вердикты для %d карточек — останутся в triage", len(missing))
        self.stats.bridge_calls = self.bridge.calls
        log.info("Триаж: карточек %d, открыть %d, закрыть %d, неудачных пачек %d, вызовов моста %d, cost $%.3f",
                 self.stats.cards, self.stats.opened, self.stats.closed, self.stats.failed_batches,
                 self.stats.bridge_calls, self.bridge.cost_usd)
        return self.stats

    def _triage_batch(self, system_text: str, batch: list[sqlite3.Row]) -> list[TriageVerdict] | None:
        user_text = json.dumps([card_payload(r) for r in batch], ensure_ascii=False, indent=0)
        last_error = ""
        for attempt in range(2):
            prompt = user_text if attempt == 0 else user_text + _RETRY_NOTE.format(error=last_error)
            try:
                answer = self.bridge.complete(system_text, prompt)
            except BridgeError as e:
                log.error("Триаж: мост недоступен: %s", e)
                raise
            try:
                parsed = TriageBatch.model_validate(extract_json(answer))
                return parsed.root
            except (ValueError, ValidationError) as e:
                last_error = str(e)[:300]
                log.warning("Триаж: невалидный ответ (попытка %d): %s", attempt + 1, last_error)
        return None


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    stats = Triager(settings, conn).run(args.limit, dry_run=args.dry_run)
    for title, v in sorted(stats.verdicts, key=lambda t: (not t[1].open, t[1].priority)):
        mark = f"OPEN p{v.priority}" if v.open else "close  "
        print(f"{mark}  {v.hh_id}  {title[:60]:<60}  {v.reason}")
    print(f"\nИтог: открыть {stats.opened}, закрыть {stats.closed}, неудачных пачек {stats.failed_batches}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
