#!/usr/bin/env python3
"""Measure what the `no_engineering_title` rule throws away before the AI ever sees it.

The rule drops any card whose title carries none of TITLE_REQUIRED_ANY — about a quarter of everything
skipped. It has never been checked. This takes a random sample of those titles, asks the triage prompt
what it would do with them, and reports how many it would have opened. Nothing is written to the
database and no page is loaded: only the bridge is used.

    .venv/bin/python scripts/audit_title_filter.py --sample 200
"""
from __future__ import annotations

import argparse
import json
import random
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from hh_scout.config import Settings  # noqa: E402
from hh_scout.db import connect  # noqa: E402
from hh_scout.llm.bridge_client import BridgeClient, extract_json  # noqa: E402
from hh_scout.llm.prompts import render  # noqa: E402

BATCH = 30


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--db", help="путь к базе (по умолчанию data/hh_scout.db рядом с репозиторием)")
    args = ap.parse_args()

    s = Settings()
    conn = connect(args.db or s.db_path)
    rows = conn.execute(
        "SELECT hh_id, title, employer, area_name, work_format, employment, search_pass, published_at "
        "FROM vacancies WHERE skip_reason = 'no_engineering_title'").fetchall()
    if not rows:
        print("Нечего проверять: таких отсеянных нет.")
        return 0
    sample = random.Random(args.seed).sample(list(rows), min(args.sample, len(rows)))
    print(f"Всего отсеяно по названию: {len(rows)}; проверяем выборку {len(sample)}")

    system_text = render(s.prompts_dir, "card_triage.md")
    bridge = BridgeClient(s)
    opened: list[tuple[str, str, int]] = []
    checked = 0
    for i in range(0, len(sample), BATCH):
        batch = sample[i:i + BATCH]
        cards = [{"hh_id": r["hh_id"], "title": r["title"], "employer": r["employer"], "area": r["area_name"],
                  "work_format": r["work_format"], "employment": r["employment"], "accept_temporary": False,
                  "civil_law_contracts": [], "salary": None, "search_pass": r["search_pass"],
                  "published_at": (r["published_at"] or "")[:10], "employer_searching_days": 0} for r in batch]
        try:
            answer = bridge.complete(system_text, json.dumps(cards, ensure_ascii=False, indent=0))
            verdicts = extract_json(answer)  # already parsed
        except Exception as e:  # noqa: BLE001
            print(f"  пачка {i // BATCH + 1}: не получилось ({e})")
            continue
        checked += len(batch)
        by_id = {r["hh_id"]: r for r in batch}
        for v in verdicts:
            if isinstance(v, dict) and v.get("open"):
                r = by_id.get(str(v.get("hh_id")))
                if r:
                    opened.append((r["title"], r["employer"] or "—", int(v.get("priority") or 3)))
        print(f"  пачка {i // BATCH + 1}: проверено {len(batch)}, к открытию {len(opened)}")

    if not checked:
        print("Ни одна пачка не прошла — мост недоступен?")
        return 1
    share = 100.0 * len(opened) / checked
    print(f"\nИТОГ: из {checked} отсеянных названий ИИ открыл бы {len(opened)} ({share:.1f} %)")
    for title, employer, prio in sorted(opened, key=lambda x: x[2])[:40]:
        print(f"  p{prio} «{title}» — {employer}")
    print("\nЕсли доля выше ~5 %, стоит дополнить TITLE_REQUIRED_ANY корнями из списка выше.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
