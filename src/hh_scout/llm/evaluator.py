"""AI evaluation of vacancies with full descriptions (status `prefiltered`).

Batches of 5 go to the bridge with `prompts/vacancy_evaluation.md` + candidate profile + feedback block.
Sub-scores come back, the total is computed here (weights in config). Results → `evaluations`,
vacancy status → `evaluated` (or `evaluation_failed` after a failed retry).

CLI:  python -m hh_scout.llm.evaluator [--limit N] [--preview] [--send]
                                         [--requeue-rejected [--min-total N] | --requeue-id HH_ID ...]
      --preview prints the digest as the bot would send it; --send also sends it to the owner's Telegram.
      --requeue-rejected re-evaluates what an older prompt left out (rejected, and evaluated below the threshold —
      a vacancy sits in the first only if a digest has run since), --requeue-id named ones: their descriptions are
      already in `raw_json`, so this needs the bridge only — no browser, no page loads. Manual use, never part of a run.
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
from hh_scout.llm.bridge_client import BridgeClient, BridgeError, extract_json
from hh_scout.llm.prompts import render
from hh_scout.llm.schemas import CompanyEvaluation, CompanyEvaluationBatch, EvaluationBatch, VacancyEvaluation
from hh_scout.pipeline import outcomes, plant, repo
from hh_scout.pipeline.ranker import company_total_score, total_score
from hh_scout.pipeline.rows import letter_key, row_site

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
    pooled: int = 0   # vacancies below the threshold that became plant pool rows instead of write-offs (v9.19)


REASON_RU = {"salary": "зарплата", "format": "формат работы", "stack": "не мой стек", "agency": "агентство"}


def feedback_block(conn: sqlite3.Connection, mature_days: int = 5, limit: int = 20) -> str:
    """The owner's 👍/👎 with reasons and what the company did — the evaluator's calibration.

    A reason is a code from the keyboard or the owner's own words (v9.11); the outcome comes from the same
    vocabulary and maturity rule as /stats, so a letter sent this morning is not shown as "silence".
    """
    rows = conn.execute(
        """SELECT f.value, f.reason, v.title, v.employer, e.verdict, v.applied, v.has_chat,
                  v.negotiation_state AS state,
                  EXISTS(SELECT 1 FROM lead_actions a WHERE a.vacancy_id = v.id AND a.action IN ('responded','auto_responded')) AS responded,
                  (SELECT MIN(d.sent_at) FROM digest_items di JOIN digests d ON d.id = di.digest_id
                    WHERE di.vacancy_id = v.id) AS sent_at,
                  NULL AS letter_at
           FROM feedback f
           JOIN vacancies v ON v.id = f.vacancy_id LEFT JOIN evaluations e ON e.vacancy_id = v.id
           ORDER BY f.id DESC LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return "(пока нет)"
    out = []
    for r in rows:
        mark = "👍" if r["value"] > 0 else "👎"
        if r["value"] > 0 and r["responded"]:
            mark = "👍 (кандидат написал этой компании)"
        why = f" — причина: {REASON_RU.get(r['reason'], r['reason'])}" if r["reason"] else ""
        note = outcomes.outcome_note(r, mature_days) if r["responded"] else ""
        out.append(f"{mark} «{r['title']}» ({r['employer'] or '—'}){why}{note}. Вердикт ИИ был: {r['verdict'] or '—'}")
    return "\n".join(out)


def company_payload(row: sqlite3.Row) -> dict:
    """What the company evaluation sees: the vacancy text (an hh channel) or the catalogue entry (ОВЕН)."""
    raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    payload = {
        "hh_id": row["hh_id"],
        "kind": "company",
        "channel": row["search_pass"],
        "company": row["employer"],
        "area": row["area_name"],
        "vacancy_title": row["title"],
        "description": strip_html(raw.get("description"))[:MAX_DESCRIPTION_CHARS],
    }
    if row_site(row) == "owen":
        payload.update({"catalog": {k: raw.get(k) for k in ("industries", "status", "region", "site", "projects_url")}})
    return payload


def vacancy_payload(row: sqlite3.Row, searching_days: int = 0) -> dict:
    if letter_key(row) == "company":
        return company_payload(row)
    raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    skills = raw.get("keySkills")
    if isinstance(skills, dict):
        skills = skills.get("keySkill")
    site = row_site(row)
    # No salary here on purpose (rule 5 of the prompt: it does not score, and the card prints it from the DB);
    # no `search_pass` either — which pass saw the card first is our crawl's shuffle, not the employer's word (v9.11).
    payload = {
        "hh_id": row["hh_id"],
        "site": site,
        "kind": "order" if site == "profi" else "vacancy",
        "title": row["title"],
        "employer": row["employer"],
        "area": row["area_name"],
        "work_format": row["work_format"],
        "employment": row["employment"],
        "experience": raw.get("workExperience"),
        "key_skills": skills or [],
        "description": strip_html(raw.get("description"))[:MAX_DESCRIPTION_CHARS],
    }
    if site == "profi":
        payload.update({"client": raw.get("client"), "budget": raw.get("budget"), "when": raw.get("when"),
                        "posted": raw.get("posted")})
    else:
        # hh's own "Оформление по ГПХ или по совместительству" flag and contract forms
        payload.update({"accept_temporary": bool(row["accept_temporary"]),
                        "civil_law_contracts": json.loads(row["civil_law_contracts"] or "[]"),
                        # our own observation: for how long this employer has been advertising this role
                        "employer_searching_days": searching_days})
    return payload


EVAL_PROMPTS = {"hh": "vacancy_evaluation.md", "profi": "profi_order_evaluation.md", "company": "company_evaluation.md"}


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
        fb = feedback_block(self.conn, self.s.outcome_mature_days)
        by_key: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_key.setdefault(letter_key(r), []).append(r)
        batches: list[tuple[str, str, list[sqlite3.Row]]] = []
        for key, key_rows in by_key.items():  # never mix vacancies, orders and companies in one prompt
            system_text = render(self.s.prompts_dir, EVAL_PROMPTS.get(key, EVAL_PROMPTS["hh"]), feedback_block=fb)
            batches += [(key, system_text, key_rows[i:i + BATCH]) for i in range(0, len(key_rows), BATCH)]
        for key, system_text, batch in batches:
            results = self._evaluate_batch(system_text, batch, company=(key == "company"))
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
                    if self._pool_if_plant(row, ev):
                        self.stats.pooled += 1
                for hh_id in set(by_id) - seen:
                    repo.set_status(self.conn, hh_id, "evaluation_failed", "missing_in_ai_answer")
                    self.stats.failed += 1
        self.stats.bridge_calls = self.bridge.calls
        log.info("Оценка: оценено %d, неудачно %d, в пул эксплуатантов %d, вызовов моста %d, cost $%.3f",
                 self.stats.evaluated, self.stats.failed, self.stats.pooled, self.stats.bridge_calls, self.bridge.cost_usd)
        return self.stats

    def _pool_if_plant(self, row: sqlite3.Row, ev: VacancyEvaluation | CompanyEvaluation) -> bool:
        """A vacancy below the threshold that the evaluator flagged `plant` joins the plant pool (v9.19).

        Only vacancy rows of hh.ru: company rows have their own path, profi orders have no company channel.
        The page already read travels with the row, so `plant.admit` will not load it again."""
        if isinstance(ev, CompanyEvaluation) or not ev.plant or letter_key(row) != "hh":
            return False
        total = total_score(self.s, ev.tech_score, ev.role_score, ev.lead_score)
        if total >= self.s.score_threshold:
            return False
        fresh = repo.vacancy_by_id(self.conn, row["id"])
        if plant.pool_evaluated(self.conn, self.s, fresh):
            log.info("В пул эксплуатантов после оценки: %s «%s» — %s (%d баллов)", row["hh_id"],
                     (row["title"] or "")[:50], row["employer"] or "—", total)
            return True
        return False

    def _store(self, row: sqlite3.Row, ev: VacancyEvaluation | CompanyEvaluation) -> None:
        if isinstance(ev, CompanyEvaluation):
            # a company lead: fit stands where tech does, there is no role, the contract form is unknown by nature
            total = company_total_score(self.s, ev.fit_score, ev.lead_score)
            values = (row["id"], ev.fit_score, 0, 0, 0, ev.lead_score, total, "maybe", int(ev.company_kind == "agency"),
                      "unknown", ev.company_kind, ev.verdict, ev.pitch_hint, json.dumps(ev.red_flags, ensure_ascii=False),
                      None, utcnow(), json.dumps(ev.offer_focus, ensure_ascii=False))
        else:
            total = total_score(self.s, ev.tech_score, ev.role_score, ev.lead_score)
            values = (row["id"], ev.tech_score, 0, 0, ev.role_score, ev.lead_score, total, ev.ip_gph_possible,
                      int(ev.is_agency), "unknown", ev.company_kind, ev.verdict, ev.pitch_hint,
                      json.dumps(ev.red_flags, ensure_ascii=False), None, utcnow(), None)
        self.conn.execute("DELETE FROM evaluations WHERE vacancy_id = ?", (row["id"],))
        self.conn.execute(
            """INSERT INTO evaluations(vacancy_id, tech_score, salary_score, format_score, role_score, lead_score, total,
                                       ip_gph_possible, is_agency, employment_hint, company_kind, verdict, pitch_hint,
                                       red_flags, model_note, created_at, offer_focus)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
        repo.set_status(self.conn, row["hh_id"], "evaluated")

    def _evaluate_batch(self, system_text: str, batch: list[sqlite3.Row], *, company: bool = False,
                        ) -> list[VacancyEvaluation] | list[CompanyEvaluation] | None:
        user_text = json.dumps([vacancy_payload(r, repo.employer_searching_days(self.conn, r)) for r in batch],
                               ensure_ascii=False)
        last_error = ""
        for attempt in range(2):
            prompt = user_text if attempt == 0 else user_text + _RETRY_NOTE.format(error=last_error)
            try:
                answer = self.bridge.complete(system_text, prompt)
            except BridgeError as e:
                log.error("Оценка: мост недоступен: %s", e)
                raise
            try:
                schema = CompanyEvaluationBatch if company else EvaluationBatch
                return schema.model_validate(extract_json(answer)).root
            except (ValueError, ValidationError) as e:
                last_error = str(e)[:300]
                log.warning("Оценка: невалидный ответ (попытка %d): %s", attempt + 1, last_error)
        return None


PREVIEW_MAX_ITEMS = 20   # a CLI preview shows the head of the queue, not the whole of it — sends have no quota


def build_digest_preview(conn: sqlite3.Connection, settings: Settings, checked: int, *, with_tail: bool = False) -> list[str]:
    """Messages the bot would send right now from evaluated vacancies (not marked sent)."""
    from hh_scout.pipeline.ranker import digest_header, format_card, format_letter

    rows = repo.evaluated_all(conn)
    passing = [r for r in rows if r["total"] >= settings.score_threshold][:PREVIEW_MAX_ITEMS]
    messages = [digest_header(len(passing), checked)]
    for i, r in enumerate(passing, 1):
        messages.append(format_card(i, r, r))
        letter = repo.get_cover_letter(conn, r["id"])
        if letter:
            messages.append(format_letter(r["employer"], letter, letter_key(r)))
    if with_tail:
        below = [r for r in rows if r["total"] < settings.score_threshold]
        if below:
            messages.append("Ниже порога (в дайджест не попадут):\n" + "\n".join(
                f"• {r['total']}/100 — {r['title']} ({r['employer'] or '—'}): {r['verdict']}" for r in below))
    return messages


def requeue(conn: sqlite3.Connection, *, min_total: int | None = None, threshold: int = 0,
            hh_ids: list[str] | None = None, limit: int | None = None) -> int:
    """Already evaluated vacancies go back to `prefiltered` for a fresh evaluation. Returns how many.

    `min_total` takes everything that was evaluated and did NOT become a lead but scored at least that much —
    a prompt change only moves the borderline. "Did not become a lead" is both `rejected` and `evaluated` below
    `threshold`: which of the two a vacancy sits in only says whether a digest has run since (`repo.reject_below`
    in `finalize_digest` writes them off), so filtering by `rejected` alone would silently skip a whole day's work.
    `hh_ids` takes exactly those vacancies whatever their status — for trying a prompt on a known case.

    Leads are never touched (`sent`, or `evaluated` at/above the threshold). The old `evaluations` row is dropped
    (the table has one row per vacancy). The page is never re-opened: the description is already in `raw_json`."""
    params: list = []
    sql = ("SELECT v.id, v.hh_id FROM vacancies v JOIN evaluations e ON e.vacancy_id = v.id "
           "WHERE v.raw_json IS NOT NULL AND v.raw_json != ''")
    if hh_ids:
        sql += f" AND v.hh_id IN ({','.join('?' * len(hh_ids))})"
        params += hh_ids
    else:
        sql += " AND (v.status = 'rejected' OR (v.status = 'evaluated' AND e.total < ?)) AND e.total >= ?"
        params += [threshold, min_total if min_total is not None else 0]
    sql += " ORDER BY e.total DESC, v.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    with conn:
        for row in rows:
            conn.execute("DELETE FROM evaluations WHERE vacancy_id = ?", (row["id"],))
            repo.set_status(conn, row["hh_id"], "prefiltered", None)
    log.info("Возвращено на переоценку: %d (%s)", len(rows),
             "по списку id" if hh_ids else f"не ставшие лидом с баллом ≥ {min_total}")
    return len(rows)


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--preview", action="store_true", help="print the digest the bot would send")
    ap.add_argument("--send", action="store_true", help="also send the preview to the owner's Telegram (no buttons)")
    ap.add_argument("--tail", action="store_true", help="preview only: also list vacancies below the threshold")
    ap.add_argument("--requeue-rejected", action="store_true",
                    help="re-evaluate what an older prompt left out: rejected and evaluated below the threshold")
    ap.add_argument("--min-total", type=int, default=45, help="--requeue-rejected: lowest old score to take back")
    ap.add_argument("--requeue-id", action="append", metavar="HH_ID", default=[],
                    help="re-evaluate exactly this vacancy whatever its status (repeatable); for trying a prompt")
    ap.add_argument("--readmit", action="store_true",
                    help="after a threshold change: rejected hh vacancies with total >= --min-total from the last --days "
                         "days go back into the queue without re-evaluation")
    ap.add_argument("--days", type=int, default=3, help="--readmit: how far back to look")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    if args.readmit:
        with conn:
            n = repo.readmit_rejected(conn, min_total=args.min_total, days=args.days)
        print(f"Возвращено в очередь: {n} (rejected с баллом ≥ {args.min_total} за {args.days} дн.)")
        return 0
    if args.requeue_id:
        requeue(conn, hh_ids=args.requeue_id)
    elif args.requeue_rejected:
        requeue(conn, min_total=args.min_total, threshold=settings.score_threshold, limit=args.limit)
    Evaluator(settings, conn).run(args.limit)
    if args.preview or args.send:
        checked = conn.execute("SELECT COUNT(*) FROM vacancies WHERE status != 'skipped' "
                               "OR COALESCE(skip_reason, '') != 'applied'").fetchone()[0]
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
