"""Cover letters for leads: one bridge call per vacancy, text stored in `cover_letters`.

The letter is written from the owner's resume (prompts/resume.md) in the style of their own best
letter; it explicitly offers contract work through the owner's ИП. The owner copies it from Telegram.

CLI:  python -m hh_scout.llm.cover_letter [--limit N] [--hh-id X] [--force] [--preview]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from hh_scout.browser.hh_pages import strip_html
from hh_scout.config import Settings
from hh_scout.llm.bridge_client import BridgeClient, BridgeError
from hh_scout.llm.company_research import CompanyResearcher
from hh_scout.llm.prompts import read_private, render
from hh_scout.pipeline import repo

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
# profi.ru orders get a short bid, not a cover letter
LETTER_PROMPTS = {"hh": "cover_letter.md", "profi": "profi_bid.md"}
REVIEW_PROMPT = "letter_review.md"  # v9.1: a second pass over every hh letter — quality over quantity
# The profi ceiling was 1200 and threw away a perfectly good bid at 1492 (profi:93756285), leaving the order
# with no text at all. Same reasoning as MAX_CHARS above: a bid a bit over the target beats no bid.
LENGTH_LIMITS = {"hh": (MIN_CHARS, MAX_CHARS), "profi": (150, 1500)}


def row_site(row: sqlite3.Row) -> str:
    return row["site"] if "site" in row.keys() and row["site"] else "hh"


@dataclass
class LetterStats:
    written: int = 0
    failed: int = 0
    reviewed: int = 0   # letters the editor actually changed
    bridge_calls: int = 0


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


# The letter must never quote money — neither the owner's figure nor the vacancy's (decisions #22-24).
_MONEY_RE = re.compile(r"\d[\d\s  ]{2,}\s*(?:₽|руб|р\.|тыс|на руки)|(?:₽|руб|тыс)\s*\d", re.IGNORECASE)
# Phrases that make the letter read as a mailshot or as flattery of the recruiter.
_BANNED = ("помогу закрыть", "закрыть позицию", "закрыть вакансию", "уникальн", "инновацион", "уважаемые",
           "динамично развивающ", "выполните kpi", "сэкономите на зарплате")
# The letter must never sort its reader by job title: "Для отдела кадров: подряд не требует…" reads as a mailshot,
# and the owner deleted that label by hand from every letter that had it (v9.6, decision #38). Cutting the label is
# cheaper and surer than regenerating the whole letter, so this lives in _clean(), not in check_letter().
_ROLE_ADDRESS_RE = re.compile(
    r"(?:\A|\n|(?<=\.)[ \t])[ \t]*(?:отдельно\s+)?для\s+[^:\n]{0,60}?(?:кадр|подбор|персонал|hr|рекрут)[^:\n]{0,20}:[ \t]*",
    re.IGNORECASE)


def check_letter(text: str, row: sqlite3.Row, company: dict | None) -> str:
    """What is wrong with the letter, or "" if it passes. Cheap rules only — the editor pass does the rest."""
    low = text.lower()
    if _MONEY_RE.search(text):
        return "в тексте есть денежная сумма — ни одной цифры про деньги быть не должно"
    for phrase in _BANNED:
        if phrase in low:
            return f"запрещённый оборот «{phrase}» — письмо должно быть деловым, без штампов и лести"
    if row_site(row) == "hh" and company:
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
    text = text.strip()
    # strip accidental code fences or a leading label
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("text"):
            text = text[4:].strip()
    for prefix in ("Отклик:", "Письмо:", "Текст письма:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return _strip_role_address(text)


def _strip_role_address(text: str) -> str:
    """Drop a "Для отдела кадров:" label, keeping the sentence it introduced (decision #38).

    The owner did exactly this by hand before sending: the thought is right, naming the reader's job is not.
    """
    out: list[str] = []
    cuts: list[int] = []   # where in the result the sentence that lost its label now begins
    last = pos = 0
    for m in _ROLE_ADDRESS_RE.finditer(text):
        log.info("Убрал из письма обращение по должности: «%s»", m.group(0).strip())
        lead = m.group(0)[0]
        chunk = text[last:m.start()] + (lead if lead.isspace() else "")   # keep the break, drop the label
        out.append(chunk)
        pos += len(chunk)
        cuts.append(pos)
        last = m.end()
    if not cuts:
        return text
    out.append(text[last:])
    chars = list("".join(out))
    for i in cuts:                       # the label carried the capital letter — give it back
        if i < len(chars):
            chars[i] = chars[i].upper()
    return "".join(chars)


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
        site = row_site(row)
        if self.answered_employer(row) is not None:
            return None
        system_text = system_text or self._system(site)
        company = None
        if site == "hh":
            brief = self.researcher.for_row(row)
            company = brief.model_dump() if brief is not None and brief.found else None
        payload = letter_payload(row, company, hint)
        lo, hi = LENGTH_LIMITS.get(site, LENGTH_LIMITS["hh"])
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
            if not (lo <= len(text) <= hi):
                problem = f"длина {len(text)} символов, нужно {lo}–{hi}"
            else:
                problem = check_letter(text, row, company)
            if not problem:
                break
            log.info("Письмо для %s — правим и переписываем: %s", row["hh_id"], problem)
        if problem:
            log.warning("Письмо для %s отклонено: %s", row["hh_id"], problem)
            self.stats.failed += 1
            return None
        if site == "hh":
            text = self._review(text, payload) or text
        with self.conn:
            repo.save_cover_letter(self.conn, row["id"], text, model_note=self.s.bridge_model or "bridge-default")
        self.stats.written += 1
        log.info("Письмо для %s «%s» (%s): %d символов", row["hh_id"], row["title"][:40], row["employer"], len(text))
        return text

    def run(self, limit: int | None = None) -> LetterStats:
        """Letters for the top of the queue, within the day's quota (v9.1).

        The quota is daily, not per run: the crawl runs three times a day and would otherwise write three
        times as many. What is left over keeps its place in the queue and gets its letter on a later day.
        """
        left = max(0, self.s.digest_max_items - repo.letters_written_today(self.conn))
        if limit is not None:
            left = min(left, limit)
        if left == 0:
            log.info("Суточная норма писем исчерпана (%d) — остальные ждут очереди", self.s.digest_max_items)
            return self.stats
        rows = [r for r in repo.lead_queue(self.conn, self.s.score_threshold, None,
                                           wait_bonus_max=self.s.queue_wait_bonus_max) if not r["letter"]][:left]
        if not rows:
            log.info("Все лиды в норме уже с письмами")
            return self.stats
        systems: dict[str, str] = {}
        for row in rows:
            site = row_site(row)
            if site not in systems:
                systems[site] = self._system(site)
            self.write_for(row, systems[site])
        self.stats.bridge_calls = self.bridge.calls + self._research_bridge.calls  # research has its own client
        log.info("Письма: написано %d (правил редактор %d), отклонено %d, вызовов моста %d, cost $%.3f",
                 self.stats.written, self.stats.reviewed, self.stats.failed, self.stats.bridge_calls,
                 self.bridge.cost_usd + self._research_bridge.cost_usd)
        return self.stats

    def _review(self, text: str, payload: dict) -> str | None:
        """Second pass: an editor checks the letter against the checklist and returns `OK` or a fixed text.

        Cheap insurance now that the day's quota is five letters: one extra call (~$0.06) per letter that the
        owner will actually send. Any trouble — keep the draft, a letter in hand beats no letter.
        """
        try:
            system_text = render(self.s.prompts_dir, REVIEW_PROMPT, resume=read_private(self.s.prompts_dir, "resume.md"))
            answer = self.bridge.complete(system_text, json.dumps({"letter": text, "vacancy": payload},
                                                                  ensure_ascii=False))
        except (BridgeError, OSError) as e:
            log.warning("Редактор письма недоступен (%s) — оставляю черновик", e)
            return None
        fixed = _clean(answer)
        if not fixed or fixed.strip().upper().startswith("OK"):
            return None
        lo, hi = LENGTH_LIMITS["hh"]
        if not (lo <= len(fixed) <= hi):
            log.warning("Редактор вернул текст длиной %d — оставляю черновик", len(fixed))
            return None
        log.info("Редактор поправил письмо: было %d символов, стало %d", len(text), len(fixed))
        self.stats.reviewed += 1
        return fixed

    def _system(self, site: str = "hh") -> str:
        template = LETTER_PROMPTS.get(site, LETTER_PROMPTS["hh"])
        return render(self.s.prompts_dir, template, resume=read_private(self.s.prompts_dir, "resume.md"))


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
