"""AI evaluation of vacancies with full descriptions (status `prefiltered`).

Batches of 5 go to the bridge with `prompts/vacancy_evaluation.md` + candidate profile + feedback block.
Sub-scores come back, the total is computed here (weights in config). Results → `evaluations`,
vacancy status → `evaluated` (or `evaluation_failed` after a failed retry).

CLI:  python -m hh_scout.llm.evaluator [--limit N] [--preview] [--send]
      --preview prints the digest as the bot would send it; --send also sends it to the owner's Telegram.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass

from pydantic import ValidationError

from hh_scout.browser.hh_pages import strip_html
from hh_scout.config import Settings
from hh_scout.db import utcnow
from hh_scout.hh.salary import human_from_raw
from hh_scout.llm.bridge_client import BridgeClient, BridgeError, extract_json
from hh_scout.llm.prompts import render
from hh_scout.llm.schemas import EvaluationBatch, VacancyEvaluation
from hh_scout.pipeline import repo
from hh_scout.pipeline.ranker import total_score

log = logging.getLogger(__name__)

BATCH = 5
MAX_DESCRIPTION_CHARS = 6000
_RETRY_NOTE = ("\n\nПредыдущий ответ не прошёл валидацию: {error}. "
               "Верни ТОЛЬКО корректный JSON-массив по схеме, по одной записи на каждую вакансию.")


@dataclass
class EvalStats:
    evaluated: int = 0
    failed: int = 0
    bridge_calls: int = 0


def feedback_block(conn: sqlite3.Connection, limit: int = 20) -> str:
    rows = conn.execute(
        """SELECT f.value, f.reason, v.title, v.employer, e.verdict,
                  EXISTS(SELECT 1 FROM lead_actions a WHERE a.vacancy_id = v.id AND a.action IN ('responded','auto_responded')) AS responded
           FROM feedback f
           JOIN vacancies v ON v.id = f.vacancy_id LEFT JOIN evaluations e ON e.vacancy_id = v.id
           ORDER BY f.id DESC LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return "(пока нет)"
    reasons = {"salary": "зарплата", "format": "формат работы", "stack": "не мой стек", "agency": "агентство"}
    out = []
    for r in rows:
        mark = "👍" if r["value"] > 0 else "👎"
        if r["value"] > 0 and r["responded"]:
            mark = "👍 (кандидат написал этой компании)"
        why = f" — причина: {reasons.get(r['reason'], r['reason'])}" if r["reason"] else ""
        out.append(f"{mark} «{r['title']}» ({r['employer'] or '—'}){why}. Вердикт ИИ был: {r['verdict'] or '—'}")
    return "\n".join(out)


def vacancy_payload(row: sqlite3.Row) -> dict:
    raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    skills = raw.get("keySkills")
    if isinstance(skills, dict):
        skills = skills.get("keySkill")
    salary_raw = json.loads(row["salary_raw"]) if row["salary_raw"] else None
    site = row_site(row)
    payload = {
        "hh_id": row["hh_id"],
        "site": site,
        "kind": "order" if site == "profi" else "vacancy",
        "title": row["title"],
        "employer": row["employer"],
        "area": row["area_name"],
        "work_format": row["work_format"],
        "employment": row["employment"],
        "salary_raw": salary_raw,
        "salary_net_human": human_from_raw(salary_raw),
        "experience": raw.get("workExperience"),
        "key_skills": skills or [],
        "description": strip_html(raw.get("description"))[:MAX_DESCRIPTION_CHARS],
    }
    if site == "profi":
        payload.update({"client": raw.get("client"), "budget": raw.get("budget"), "when": raw.get("when"),
                        "posted": raw.get("posted")})
    return payload


def row_site(row: sqlite3.Row) -> str:
    return row["site"] if "site" in row.keys() and row["site"] else "hh"


EVAL_PROMPTS = {"hh": "vacancy_evaluation.md", "profi": "profi_order_evaluation.md"}


class Evaluator:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, bridge: BridgeClient | None = None) -> None:
        self.s = settings
        self.conn = conn
        self.bridge = bridge or BridgeClient(settings)
        self.stats = EvalStats()

    def run(self, limit: int | None = None) -> EvalStats:
        rows = repo.list_vacancies(self.conn, "prefiltered", limit)
        if not rows:
            log.info("Оценивать нечего (нет prefiltered)")
            return self.stats
        fb = feedback_block(self.conn)
        by_site: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_site.setdefault(row_site(r), []).append(r)
        batches: list[tuple[str, list[sqlite3.Row]]] = []
        for site, site_rows in by_site.items():  # never mix vacancies and orders in one prompt
            system_text = render(self.s.prompts_dir, EVAL_PROMPTS.get(site, EVAL_PROMPTS["hh"]), feedback_block=fb)
            batches += [(system_text, site_rows[i:i + BATCH]) for i in range(0, len(site_rows), BATCH)]
        for system_text, batch in batches:
            results = self._evaluate_batch(system_text, batch)
            by_id = {r["hh_id"]: r for r in batch}
            with self.conn:
                if results is None:
                    for r in batch:
                        repo.set_status(self.conn, r["hh_id"], "evaluation_failed", "invalid_ai_answer")
                    self.stats.failed += len(batch)
                    continue
                seen = set()
                for ev in results:
                    row = by_id.get(ev.hh_id)
                    if row is None:
                        log.warning("Оценка для неизвестного hh_id %s — игнорирую", ev.hh_id)
                        continue
                    seen.add(ev.hh_id)
                    self._store(row, ev)
                    self.stats.evaluated += 1
                for hh_id in set(by_id) - seen:
                    repo.set_status(self.conn, hh_id, "evaluation_failed", "missing_in_ai_answer")
                    self.stats.failed += 1
        self.stats.bridge_calls = self.bridge.calls
        log.info("Оценка: оценено %d, неудачно %d, вызовов моста %d, cost $%.3f",
                 self.stats.evaluated, self.stats.failed, self.stats.bridge_calls, self.bridge.cost_usd)
        return self.stats

    def _store(self, row: sqlite3.Row, ev: VacancyEvaluation) -> None:
        total = total_score(self.s, ev.tech_score, ev.role_score, ev.lead_score)
        self.conn.execute("DELETE FROM evaluations WHERE vacancy_id = ?", (row["id"],))
        self.conn.execute(
            """INSERT INTO evaluations(vacancy_id, tech_score, salary_score, format_score, role_score, lead_score, total,
                                       ip_gph_possible, is_agency, employment_hint, company_kind, verdict, pitch_hint,
                                       red_flags, model_note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], ev.tech_score, 0, 0, ev.role_score, ev.lead_score, total, ev.ip_gph_possible, int(ev.is_agency),
             ev.employment_hint, ev.company_kind, ev.verdict, ev.pitch_hint,
             json.dumps(ev.red_flags, ensure_ascii=False), None, utcnow()),
        )
        repo.set_status(self.conn, row["hh_id"], "evaluated")

    def _evaluate_batch(self, system_text: str, batch: list[sqlite3.Row]) -> list[VacancyEvaluation] | None:
        user_text = json.dumps([vacancy_payload(r) for r in batch], ensure_ascii=False)
        last_error = ""
        for attempt in range(2):
            prompt = user_text if attempt == 0 else user_text + _RETRY_NOTE.format(error=last_error)
            try:
                answer = self.bridge.complete(system_text, prompt)
            except BridgeError as e:
                log.error("Оценка: мост недоступен: %s", e)
                raise
            try:
                return EvaluationBatch.model_validate(extract_json(answer)).root
            except (ValueError, ValidationError) as e:
                last_error = str(e)[:300]
                log.warning("Оценка: невалидный ответ (попытка %d): %s", attempt + 1, last_error)
        return None


def build_digest_preview(conn: sqlite3.Connection, settings: Settings, checked: int, *, with_tail: bool = False) -> list[str]:
    """Messages the bot would send right now from evaluated vacancies (not marked sent)."""
    from hh_scout.pipeline.ranker import digest_header, format_card, format_letter

    rows = conn.execute(
        """SELECT v.*, e.tech_score, e.role_score, e.lead_score, e.total, e.ip_gph_possible, e.is_agency,
                  e.employment_hint, e.company_kind, e.verdict, e.pitch_hint, e.red_flags
           FROM vacancies v JOIN evaluations e ON e.vacancy_id = v.id
           WHERE v.status = 'evaluated' ORDER BY e.total DESC, v.published_at DESC""").fetchall()
    passing = [r for r in rows if r["total"] >= settings.score_threshold][: settings.digest_max_items]
    messages = [digest_header(len(passing), checked)]
    for i, r in enumerate(passing, 1):
        messages.append(format_card(i, r, r))
        letter = repo.get_cover_letter(conn, r["id"])
        if letter:
            messages.append(format_letter(r["employer"], letter, row_site(r)))
    if with_tail:
        below = [r for r in rows if r["total"] < settings.score_threshold]
        if below:
            messages.append("Ниже порога (в дайджест не попадут):\n" + "\n".join(
                f"• {r['total']}/100 — {r['title']} ({r['employer'] or '—'}): {r['verdict']}" for r in below))
    return messages


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--preview", action="store_true", help="print the digest the bot would send")
    ap.add_argument("--send", action="store_true", help="also send the preview to the owner's Telegram (no buttons)")
    ap.add_argument("--tail", action="store_true", help="preview only: also list vacancies below the threshold")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    Evaluator(settings, conn).run(args.limit)
    if args.preview or args.send:
        checked = conn.execute("SELECT COUNT(*) FROM vacancies WHERE status != 'skipped' OR skip_reason != 'applied'").fetchone()[0]
        messages = build_digest_preview(conn, settings, checked, with_tail=args.tail)
        import re
        for m in messages:
            print(re.sub(r"</?(b|pre)>", "", m).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
            print()
        if args.send and settings.tg_bot_token and settings.tg_owner_chat_id:
            import time

            import httpx
            api = f"https://api.telegram.org/bot{settings.tg_bot_token}/sendMessage"
            for m in messages:
                r = httpx.post(api, json={"chat_id": settings.tg_owner_chat_id, "text": m, "parse_mode": "HTML",
                                          "link_preview_options": {"is_disabled": True}}, timeout=20).json()
                if not r.get("ok"):
                    print("Telegram отказал:", r)
                time.sleep(0.6)
            print(f"Отправлено сообщений в Telegram: {len(messages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
