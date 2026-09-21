"""Cover letters for leads: one bridge call per vacancy, text stored in `cover_letters`.

The letter is written from the owner's resume (prompts/resume.md) in the style of their own best
letter; it explicitly offers contract work through the owner's ИП. The owner copies it from Telegram.

CLI:  python -m hh_scout.llm.cover_letter [--limit N] [--hh-id X] [--force] [--preview]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass

from hh_scout.browser.hh_pages import strip_html
from hh_scout.config import Settings
from hh_scout.llm import letter_checks
from hh_scout.llm.bridge_client import BridgeClient, BridgeError
from hh_scout.llm.company_research import CompanyResearcher
from hh_scout.llm.letter_checks import strip_role_address
from hh_scout.llm.prompts import PrivatePromptMissing, load_prompt_body, read_private, render
from hh_scout.pipeline import repo
from hh_scout.pipeline.rows import letter_key, row_get, row_site

log = logging.getLogger(__name__)

MIN_CHARS = 400
MAX_CHARS = 3600  # v9.0: +the company line and the "easy to arrange" paragraph. Deliberately looser than the
# 3400 the prompts aim at: a letter 100 chars over the guidance beats no letter at all, so this only stops rambling.
MAX_DESCRIPTION_CHARS = 6000
# the vacancy asks the applicant to name a figure; the letter answers "по объёму задач", never a number
ASKS_SALARY_PHRASES = (
    "ожидаемый уровень", "зарплатные ожидания", "зарплатных ожиданий", "зарплатные пожелания",
    "ожидания по зарплате", "финансовые ожидания", "желаемый доход", "уровень дохода",
    "желаемый уровень", "укажите желаемую", "укажите вилку", "желаемую заработную плату",
    "salary expectation",
)
# Three prompt families (`rows.letter_key`): a response to a vacancy, a short bid for a profi.ru order, and a
# partnership offer to a company (v9.13) — the last is read by a director, not HR, so it is shorter.
LETTER_PROMPTS = {"hh": "cover_letter.md", "profi": "profi_bid.md", "company": "company_offer.md"}
REVIEW_PROMPT = "letter_review.md"  # v9.1: a second pass over every hh letter; company offers go through it too
REVIEWED_KEYS = frozenset({"hh", "company"})
# The profi ceiling was 1200 and threw away a perfectly good bid at 1492 (profi:93756285), leaving the order
# with no text at all. Same reasoning as MAX_CHARS above: a bid a bit over the target beats no bid.
LENGTH_LIMITS = {"hh": (MIN_CHARS, MAX_CHARS), "profi": (150, 1500), "company": (MIN_CHARS, 3000)}
# What the letter payload takes from the dossier: the facts and the guesses, not the researcher's own notes
# (`sources`, `note` are meta-commentary the letter model was never told to ignore).
DOSSIER_FIELDS = ("found", "what_they_do", "industry", "products", "sites", "scale", "automation_hooks")


@dataclass
class LetterStats:
    written: int = 0
    failed: int = 0
    reviewed: int = 0   # letters the editor actually changed
    bridge_calls: int = 0
    left_for_later: int = 0   # leads the time budget did not reach; they keep their place in the queue


def salary_stated(row: sqlite3.Row) -> bool:
    """Whether the vacancy names any money at all.

    Deliberately a flag, not the figure: the letter must never quote a sum, and all the model needs is
    whether it can refer to "ваш бюджет" as a known thing. hh sends `{"noCompensation": …}` when it is silent.
    """
    raw = json.loads(row["salary_raw"]) if row["salary_raw"] else None
    return bool(raw) and (raw.get("from") is not None or raw.get("to") is not None)


def letter_payload(row: sqlite3.Row, company: dict | None = None, owner_hint: str | None = None) -> dict:
    raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    skills = raw.get("keySkills")
    if isinstance(skills, dict):
        skills = skills.get("keySkill")
    desc = strip_html(raw.get("description"))
    asks_salary = any(w in desc.lower() for w in ASKS_SALARY_PHRASES)
    if letter_key(row) == "company":
        # a partnership offer: what we know about the company, what to offer it — and no salary anything
        catalog = {k: raw.get(k) for k in ("industries", "status", "region", "site", "projects_url") if raw.get(k)}
        return {
            "kind": "company",
            "channel": row["search_pass"],
            "company_name": row["employer"],
            "company_kind": row["company_kind"],
            "city": row["area_name"],
            "company": company,      # dossier from the open web; None = only what the vacancy / catalogue said
            "catalog": catalog or None,
            "seen_through": row["title"],   # the vacancy (or catalogue line) the company was found by
            "description": desc[:MAX_DESCRIPTION_CHARS],
            "verdict": row["verdict"],
            "pitch_hint": row["pitch_hint"],
            "offer_focus": json.loads(row_get(row, "offer_focus") or "[]"),
            "owner_hint": owner_hint or None,
        }
    if row_site(row) == "profi":
        return {
            "kind": "order",
            "title": row["title"],
            "client": row["employer"],
            "description": desc[:MAX_DESCRIPTION_CHARS],
            "budget": raw.get("budget"),
            "when": raw.get("when"),
            "work_format": row["work_format"],
            "city": row["area_name"],
            "verdict": row["verdict"],
            "pitch_hint": row["pitch_hint"],
        }
    address = raw.get("address") or {}
    return {
        "title": row["title"],
        "employer": row["employer"],
        "company": company,  # dossier from the open web (prompts/company_research.md); None = nothing known
        "owner_hint": owner_hint or None,  # what the owner asked for when rewriting by hand (/letter <id> …)
        "company_kind": row["company_kind"],
        "city": row["area_name"],
        "address": address.get("displayName") or None,
        "work_format": row["work_format"],
        "employment": row["employment"],
        "accept_temporary": bool(row["accept_temporary"]),
        "civil_law_contracts": json.loads(row["civil_law_contracts"] or "[]"),
        "ip_gph_possible": row["ip_gph_possible"],
        "salary_stated": salary_stated(row),
        "description": desc[:MAX_DESCRIPTION_CHARS],
        "key_skills": skills or [],
        "verdict": row["verdict"],
        "pitch_hint": row["pitch_hint"],
        "salary_note": "вакансия просит указать зарплатные ожидания" if asks_salary else "зарплату не упоминать",
    }


def render_letter_prompt(settings: Settings, key: str = "hh") -> str:
    """The family's letter prompt with the resume and the profile substituted — what the model is told."""
    template = LETTER_PROMPTS.get(key, LETTER_PROMPTS["hh"])
    return render(settings.prompts_dir, template, resume=read_private(settings.prompts_dir, "resume.md"))


def render_review_prompt(settings: Settings) -> str:
    return render(settings.prompts_dir, REVIEW_PROMPT, resume=read_private(settings.prompts_dir, "resume.md"))


def rules_hash(settings: Settings, key: str = "hh") -> str:
    """Which version of the letter rules a text was written under (decision #50).

    The letter is written the day the lead is found and may leave the queue days later — by then the prompt,
    the owner's profile, the resume or the code's own checks may have changed, and the stored text quietly
    breaks a rule that now exists. The stamp is the fingerprint of everything that shapes the letter: the site's
    prompt with the profile and the resume already substituted, the editor's checklist for hh, and the code
    rules in `letter_checks` (the incident behind #50 was a *code* rule the stored letters violated).

    An unreadable prompt gives "" — «rules unknown», so every stored letter counts as stale and the writer
    (which needs the same files and fails loudly) decides what happens next. The digest then sends cards
    without letters instead of crashing on a machine where the private prompts are missing.
    """
    try:
        parts = [render_letter_prompt(settings, key)]
    except (PrivatePromptMissing, OSError) as e:
        log.warning("Не читаются промпты письма (%s) — считаю все сохранённые письма устаревшими", e)
        return ""
    if key in REVIEWED_KEYS:
        try:
            parts.append(load_prompt_body(settings.prompts_dir / REVIEW_PROMPT))
        except OSError:
            pass      # no checklist, no editor pass — `_review` degrades exactly the same way
        parts.append(letter_checks.CODE_RULES)
        parts.append(f"length:{LENGTH_LIMITS.get(key, LENGTH_LIMITS['hh'])}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def usable_letter(row: sqlite3.Row, rules: str) -> str | None:
    """The stored letter, but only if it was written under the current rules — otherwise None, i.e. «письма нет».

    One rule instead of three: a stale letter behaves exactly like a missing one, so the digest rewrites it and
    the instant send holds the lead back, with no new machinery on either side. A letter written without a
    company dossier is stale too once the dossier exists: the card would name the company's production and the
    letter would not (v9.11).
    """
    text = row_get(row, "letter")
    if not text:
        return None
    if not rules or row_get(row, "letter_rules") != rules:
        return None
    if row_get(row, "company_brief") and not row_get(row, "letter_dossier"):
        return None
    return text


def check_letter(text: str, row: sqlite3.Row, company: dict | None) -> str:
    """What is wrong with the letter, or "" if it passes. Cheap rules only — the editor pass does the rest."""
    problem = letter_checks.money_problem(text) or letter_checks.cliche_problem(text)
    if problem:
        return problem
    if letter_key(row) in REVIEWED_KEYS:
        problem = letter_checks.signature_problem(text)
        if problem:
            return problem
        if company:
            low = text.lower()
            hooks = [w for w in _company_words(row, company) if len(w) > 4]
            if hooks and not any(w.lower() in low for w in hooks):
                return ("письмо не называет, чем занимается компания — первая строка должна показывать, "
                        f"что автор понимает их производство (например: {', '.join(hooks[:3])})")
    return ""


def _company_words(row: sqlite3.Row, company: dict) -> list[str]:
    """Words that prove the letter is about THIS company: its name and what the dossier says it makes."""
    words = [row["employer"] or ""]
    for value in (company.get("industry") or "", *(company.get("products") or [])):
        words += [w.strip(" ,.;:()«»\"") for w in str(value).split() if len(w) > 4]
    return [w for w in dict.fromkeys(words) if w]


def _clean(text: str) -> str:
    """The bridge answer as a letter: no fence, no label, no address by job title."""
    text = text.strip()
    # strip accidental code fences or a leading label
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("text"):
            text = text[4:].strip()
    for prefix in ("Отклик:", "Письмо:", "Текст письма:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return strip_role_address(text)


class CoverLetterWriter:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, bridge: BridgeClient | None = None) -> None:
        self.s = settings
        self.conn = conn
        self.bridge = bridge or BridgeClient(settings)
        # Research gets its own client, deliberately without retries: it waits on the web for minutes, and a
        # bridge that just timed out on a heavy page will time out again. Handing it self.bridge (retries=2)
        # silently overrode CompanyResearcher's own retries=0 and cost 21 minutes on a single dead company site.
        self._research_bridge = BridgeClient(settings, retries=0)
        self.researcher = CompanyResearcher(settings, conn, self._research_bridge)
        self.stats = LetterStats()
        self._rules_cache: dict[str, str] = {}

    def rules(self, key: str) -> str:
        """The current rules stamp for a prompt family, read once per writer (the prompt files are re-read by design)."""
        if key not in self._rules_cache:
            self._rules_cache[key] = rules_hash(self.s, key)
        return self._rules_cache[key]

    def answered_employer(self, row: sqlite3.Row) -> sqlite3.Row | None:
        """The vacancy of this company the owner has already answered — then no letter is written at all.

        The single choke point for the owner's rule «пишем только туда, куда ещё не откликались»: it holds for the
        digest, for `/letter` and for any ad-hoc script, and it fires before research, so a skip costs nothing.
        """
        if row_site(row) != "hh":
            return None
        answered = repo.employer_responded(self.conn, row["employer_id"], row["employer"],
                                           exclude_id=None, within_days=self.s.employer_repeat_days)
        if answered is not None:
            log.info("Письмо для %s не пишу: в «%s» уже откликались (%s, %s)", row["hh_id"], row["employer"] or "—",
                     answered["hh_id"], (answered["answered_at"] or "")[:10])
        return answered

    def write_for(self, row: sqlite3.Row, system_text: str | None = None, hint: str | None = None) -> str | None:
        """Write (or rewrite) the letter for one lead; None when it could not be written.

        A rewrite keeps the owner's earlier wish (`cover_letters.owner_hint`) unless a new one is given: the
        instruction typed into `/letter <id> …` must survive a rules change (v9.11).
        """
        key = letter_key(row)
        if self.answered_employer(row) is not None:
            return None
        hint = hint or row_get(row, "letter_hint") or None
        system_text = system_text or render_letter_prompt(self.s, key)
        company = None
        if key in REVIEWED_KEYS:
            brief = self.researcher.for_row(row)
            company = brief.model_dump(include=set(DOSSIER_FIELDS)) if brief is not None and brief.found else None
        payload = letter_payload(row, company, hint)
        lo, hi = LENGTH_LIMITS.get(key, LENGTH_LIMITS["hh"])
        text, problem = "", ""
        for attempt in (0, 1):
            user_text = json.dumps(payload, ensure_ascii=False)
            if problem:
                user_text += f"\n\nПредыдущий вариант не годится: {problem}. Перепиши письмо целиком без этой ошибки."
            try:
                answer = self.bridge.complete(system_text, user_text)
            except BridgeError as e:
                log.error("Письмо для %s: мост недоступен: %s", row["hh_id"], e)
                raise
            text = _clean(answer)
            problem = self._problem(text, row, company, lo, hi)
            if not problem:
                break
            log.info("Письмо для %s — правим и переписываем: %s", row["hh_id"], problem)
        if problem:
            log.warning("Письмо для %s отклонено: %s", row["hh_id"], problem)
            self.stats.failed += 1
            return None
        if key in REVIEWED_KEYS:
            text = self._review(text, payload, row, company) or text
        with self.conn:
            repo.save_cover_letter(self.conn, row["id"], text, model_note=self.s.bridge_model or "bridge-default",
                                   rules_hash=self.rules(key), owner_hint=hint, with_dossier=company is not None)
        self.stats.written += 1
        log.info("Письмо для %s «%s» (%s): %d символов", row["hh_id"], row["title"][:40], row["employer"], len(text))
        return text

    @staticmethod
    def _problem(text: str, row: sqlite3.Row, company: dict | None, lo: int, hi: int) -> str:
        if not (lo <= len(text) <= hi):
            return f"длина {len(text)} символов, нужно {lo}–{hi}"
        return check_letter(text, row, company)

    def run(self, limit: int | None = None) -> LetterStats:
        """Letters for the whole queue, for as long as the time budget allows (v9.14, decision #54).

        Until v9.14 this pass was capped by the daily lead quota. With the quota gone the cap is time:
        a letter costs ~80 s of bridge, and this stage runs inside a sitting that must not spill into the
        next window. Leads with no letter go first, rewrites of stale ones after (decision #50); whatever
        the budget does not reach keeps its place in the queue — a day of waiting is worth a point there,
        so it comes back at the head next sitting.
        """
        queue = repo.lead_queue(self.conn, self.s.score_threshold, None,
                                wait_bonus_max=self.s.queue_wait_bonus_max)
        fresh = [r for r in queue if not r["letter"]]
        stale = [r for r in queue if r["letter"] and not usable_letter(r, self.rules(letter_key(r)))]
        if stale:
            log.info("Писем, написанных по прежним правилам: %d — переписываю после новых лидов", len(stale))
        rows = fresh + stale
        if limit is not None:
            rows = rows[:limit]
        if not rows:
            log.info("Все лиды в очереди уже с письмами")
            return self.stats
        deadline = time.monotonic() + self.s.letters_budget_min * 60 if self.s.letters_budget_min > 0 else None
        systems: dict[str, str] = {}
        for i, row in enumerate(rows):
            # Checked between letters, never inside one: an unfinished letter is worse than a late one
            if deadline is not None and i and time.monotonic() >= deadline:
                self.stats.left_for_later = len(rows) - i
                log.info("Бюджет времени на письма исчерпан (%d мин): написано %d, ждут очереди %d",
                         self.s.letters_budget_min, self.stats.written, self.stats.left_for_later)
                break
            key = letter_key(row)
            if key not in systems:
                systems[key] = render_letter_prompt(self.s, key)
            if self.answered_employer(row) is not None:
                continue      # the company is closed; `dedup.dedupe_evaluated` will skip the row on its next pass
            if self.write_for(row, systems[key]) is None and row["letter"]:
                log.warning("Письмо для %s осталось по прежним правилам — лид подождёт следующего подхода",
                            row["hh_id"])
        self.stats.bridge_calls = self.bridge.calls + self._research_bridge.calls  # research has its own client
        log.info("Письма: написано %d (правил редактор %d), отклонено %d, вызовов моста %d, cost $%.3f",
                 self.stats.written, self.stats.reviewed, self.stats.failed, self.stats.bridge_calls,
                 self.bridge.cost_usd + self._research_bridge.cost_usd)
        return self.stats

    def _review(self, text: str, payload: dict, row: sqlite3.Row, company: dict | None) -> str | None:
        """Second pass: an editor checks the letter against the checklist and returns `OK` or a fixed text.

        The editor is the last model to touch the text, so its answer goes through the same code checks as the
        draft (v9.11) — a sum or a cliché it puts back is not let through. Any trouble — keep the draft,
        a letter in hand beats no letter.
        """
        try:
            answer = self.bridge.complete(render_review_prompt(self.s), json.dumps({"letter": text, "vacancy": payload},
                                                                                   ensure_ascii=False))
        except (BridgeError, OSError) as e:
            log.warning("Редактор письма недоступен (%s) — оставляю черновик", e)
            return None
        fixed = _clean(answer)
        if not fixed or fixed.strip().upper().startswith(("OK", "ОК")):
            return None
        lo, hi = LENGTH_LIMITS.get(letter_key(row), LENGTH_LIMITS["hh"])
        problem = self._problem(fixed, row, company, lo, hi)
        if problem:
            log.warning("Редактор вернул текст с ошибкой (%s) — оставляю черновик", problem)
            return None
        log.info("Редактор поправил письмо: было %d символов, стало %d", len(text), len(fixed))
        self.stats.reviewed += 1
        return fixed


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
        row = repo.lead_by_hh_id(conn, args.hh_id)
        if row is None:
            print("Нет оценённой вакансии с таким hh_id")
            return 1
        if row["letter"] and not args.force:
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
