"""Cover letters for leads: one bridge call per vacancy, text stored in `cover_letters`.

The letter is written from the owner's resume (prompts/resume.md) in the style of their own best
letter; it explicitly offers contract work through the owner's ИП. The owner copies it from Telegram.

CLI:  python -m hh_scout.llm.cover_letter [--limit N] [--hh-id X] [--force] [--preview]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass

from hh_scout.browser.hh_pages import strip_html
from hh_scout.config import Settings
from hh_scout.llm.bridge_client import BridgeClient, BridgeError
from hh_scout.llm.prompts import read_private, render
from hh_scout.pipeline import repo

log = logging.getLogger(__name__)

MIN_CHARS = 400
MAX_CHARS = 2500
MAX_DESCRIPTION_CHARS = 6000


@dataclass
class LetterStats:
    written: int = 0
    failed: int = 0
    bridge_calls: int = 0


def letter_payload(row: sqlite3.Row) -> dict:
    raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    skills = raw.get("keySkills")
    if isinstance(skills, dict):
        skills = skills.get("keySkill")
    desc = strip_html(raw.get("description"))
    asks_salary = any(w in desc.lower() for w in ("ожидаемый уровень", "зарплатные ожидания", "укажите желаемую", "ожидания по зарплате"))
    return {
        "title": row["title"],
        "employer": row["employer"],
        "company_kind": row["company_kind"],
        "employment": row["employment"],
        "ip_gph_possible": row["ip_gph_possible"],
        "description": desc[:MAX_DESCRIPTION_CHARS],
        "key_skills": skills or [],
        "verdict": row["verdict"],
        "pitch_hint": row["pitch_hint"],
        "salary_note": "вакансия просит указать зарплатные ожидания" if asks_salary else "зарплату не упоминать",
    }


def _clean(text: str) -> str:
    text = text.strip()
    # strip accidental code fences or a leading label
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("text"):
            text = text[4:].strip()
    for prefix in ("Отклик:", "Письмо:", "Текст письма:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text


class CoverLetterWriter:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, bridge: BridgeClient | None = None) -> None:
        self.s = settings
        self.conn = conn
        self.bridge = bridge or BridgeClient(settings)
        self.stats = LetterStats()

    def write_for(self, row: sqlite3.Row, system_text: str | None = None) -> str | None:
        system_text = system_text or self._system()
        user_text = json.dumps(letter_payload(row), ensure_ascii=False)
        try:
            answer = self.bridge.complete(system_text, user_text)
        except BridgeError as e:
            log.error("Письмо для %s: мост недоступен: %s", row["hh_id"], e)
            raise
        text = _clean(answer)
        if not (MIN_CHARS <= len(text) <= MAX_CHARS):
            log.warning("Письмо для %s отклонено по длине (%d символов)", row["hh_id"], len(text))
            self.stats.failed += 1
            return None
        with self.conn:
            repo.save_cover_letter(self.conn, row["id"], text, model_note=self.s.bridge_model or "bridge-default")
        self.stats.written += 1
        log.info("Письмо для %s «%s» (%s): %d символов", row["hh_id"], row["title"][:40], row["employer"], len(text))
        return text

    def run(self, limit: int | None = None) -> LetterStats:
        rows = repo.leads_without_letter(self.conn, self.s.score_threshold, limit)
        if not rows:
            log.info("Все лиды уже с письмами")
            return self.stats
        system_text = self._system()
        for row in rows:
            self.write_for(row, system_text)
        self.stats.bridge_calls = self.bridge.calls
        log.info("Письма: написано %d, отклонено %d, вызовов моста %d, cost $%.3f",
                 self.stats.written, self.stats.failed, self.stats.bridge_calls, self.bridge.cost_usd)
        return self.stats

    def _system(self) -> str:
        return render(self.s.prompts_dir, "cover_letter.md", resume=read_private(self.s.prompts_dir, "resume.md"))


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--hh-id", help="write (or rewrite with --force) a letter for one vacancy")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--preview", action="store_true", help="print the letters")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    writer = CoverLetterWriter(settings, conn)
    if args.hh_id:
        row = conn.execute(
            """SELECT v.*, e.total, e.verdict, e.pitch_hint, e.company_kind, e.ip_gph_possible, e.employment_hint
               FROM vacancies v JOIN evaluations e ON e.vacancy_id = v.id WHERE v.hh_id = ?""", (args.hh_id,)).fetchone()
        if row is None:
            print("Нет оценённой вакансии с таким hh_id")
            return 1
        if repo.get_cover_letter(conn, row["id"]) and not args.force:
            print("Письмо уже есть, используйте --force для перезаписи")
        else:
            writer.write_for(row)
        rows = [row]
    else:
        writer.run(args.limit)
        rows = conn.execute(
            """SELECT v.*, c.text FROM vacancies v JOIN cover_letters c ON c.vacancy_id = v.id
               JOIN evaluations e ON e.vacancy_id = v.id ORDER BY e.total DESC""").fetchall()
    if args.preview:
        for r in rows:
            text = repo.get_cover_letter(conn, r["id"])
            print(f"\n===== {r['hh_id']} — {r['title']} — {r['employer']} ({len(text or '')} симв.) =====\n{text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
