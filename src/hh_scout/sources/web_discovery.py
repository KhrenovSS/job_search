"""Panel builders found by searching the web through the Claude bridge (v9.41, decision #75).

The owner's best-answering channel is panel builders, and hh.ru only shows those hiring an assembler right now.
Vendor partner catalogues are unreadable from this host (geo-blocks, SPAs, 403), so the model searches instead:
one bridge call with the web allowed (`prompts/company_discovery.md`) per *task* — a region × a query, or a vendor
whose partner programme lists licensed panel builders — answers 10–25 companies with a site and a page that proves
they assemble cabinets. The search itself runs on Anthropic's side, so the host's route does not matter; reading
the found sites later is the dossier's job (`company_research.md`, it finds the e-mail).

A company becomes a `new` row of `site='web'`, `hh_id = employer_id = 'web:<domain>'`, `lead_kind='company'`,
`search_pass='discovery'` — unless its domain is already known through any source (`employer_contacts`: the same
firm seen on hh.ru, in the ОВЕН catalogue, on the portal) or the name is on the defence list. Admission into
evaluation is `DISCOVERY_LEADS_PER_DAY` a day (`repo.admit_company_leads(search_pass='discovery')`, run.py step 4b′);
from there the row goes the catalogue way: company evaluation → dossier → the partnership offer by e-mail, or
`skipped/no_email`. Tasks go round-robin over days (kv `discovery_cursor`); the daily job is `Scheduler.discovery_job`.

CLI:  python -m hh_scout.sources.web_discovery [--tasks N] [--region "…" | --vendor "…"] [--query "…"] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError

from hh_scout.config import (DISCOVERY_EXTRA_REGIONS, DISCOVERY_REGION_QUERIES, DISCOVERY_VENDOR_HINT, DISCOVERY_VENDORS,
                             REGION_NAMES, Settings)
from hh_scout.db import kv_get, kv_set, transaction, utcnow
from hh_scout.llm.bridge_client import BridgeClient, BridgeError, BridgeUnavailable, extract_json
from hh_scout.llm.prompts import load_prompt_body
from hh_scout.llm.schemas import DiscoveredCompany, DiscoveryAnswer
from hh_scout.pipeline import defense, repo
from hh_scout.pipeline.contacts import domain_of, normalize_email

log = logging.getLogger(__name__)

SITE = "web"
ID_PREFIX = "web:"
PASS = "discovery"
SOURCE_PREFIX = "discovery:"
KV_CURSOR = "discovery_cursor"   # index of the next task in `tasks()`
KV_LAST = "discovery_last"       # "<utc iso>|<tasks>|<found>|<new>|<cost usd>" of the last job
PROMPT_FILE = "company_discovery.md"
_RETRY_NOTE = ("\n\nПредыдущий ответ не прошёл валидацию: {error}. "
               "Ответь строго JSON по схеме: {{\"companies\": [{{\"name\", \"website\", \"city\", \"region\", \"inn\", "
               "\"emails\", \"what_they_do\", \"evidence_url\"}}], \"note\": \"\"}}.")


class DiscoveryFailed(RuntimeError):
    """The bridge did not answer, or answered twice with something that is not the list."""


@dataclass(frozen=True)
class Task:
    kind: str          # "region" | "vendor"
    subject: str       # the region's or the vendor's name
    query: str = ""    # region tasks: the search phrase

    @property
    def label(self) -> str:
        return f"{self.subject} · {self.query}" if self.kind == "region" else f"партнёры {self.subject}"

    def payload(self) -> dict:
        if self.kind == "vendor":
            return {"task": "vendor", "vendor": self.subject, "hint": DISCOVERY_VENDOR_HINT}
        return {"task": "region", "region": self.subject, "query": self.query}


@dataclass(frozen=True)
class DiscoveredCard:
    """A company as stored: the model's answer plus the domain that keys it."""
    name: str
    website: str
    domain: str
    city: str | None
    region: str | None
    inn: str | None
    emails: tuple[str, ...]
    what_they_do: str
    evidence_url: str
    query: str

    @property
    def ext_id(self) -> str:
        return ID_PREFIX + self.domain


@dataclass
class JobResult:
    tasks: int = 0
    failed: int = 0
    found: int = 0
    new: int = 0
    known: int = 0
    defense: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    labels: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        return (f"задач {self.tasks} (не удалось {self.failed}), найдено {self.found}, новых {self.new}, уже известны {self.known}, "
                f"оборонка {self.defense}, вызовов моста {self.calls}, cost ${self.cost_usd:.2f}")


def regions() -> tuple[str, ...]:
    return tuple(dict.fromkeys(REGION_NAMES + DISCOVERY_EXTRA_REGIONS))


def tasks() -> list[Task]:
    """Vendors first, then every region with the first query, then the next query, and so on."""
    out = [Task("vendor", v) for v in DISCOVERY_VENDORS]
    for q in DISCOVERY_REGION_QUERIES:
        out += [Task("region", r, q) for r in regions()]
    return out


def to_card(c: DiscoveredCompany, task: Task) -> DiscoveredCard | None:
    domain = domain_of(c.website)
    if domain is None:
        return None
    emails = tuple(dict.fromkeys(e for e in (normalize_email(x) for x in c.emails) if e))
    return DiscoveredCard(name=c.name.strip(), website=c.website.strip(), domain=domain, city=c.city.strip() or None,
                          region=c.region.strip() or None, inn=c.inn.strip() or None, emails=emails,
                          what_they_do=c.what_they_do.strip(), evidence_url=c.evidence_url.strip(), query=task.label)


def parse_answer(data: object) -> DiscoveryAnswer:
    """The model's JSON as `DiscoveryAnswer`; one bad entry (no domain, no name) is dropped, not the whole list.
    Raises ValueError when the shape is not {"companies": [...]} at all."""
    if not isinstance(data, dict) or not isinstance(data.get("companies"), list):
        raise ValueError("нет списка companies")
    companies: list[DiscoveredCompany] = []
    for item in data["companies"]:
        try:
            companies.append(DiscoveredCompany.model_validate(item))
        except ValidationError as e:
            log.debug("Запись поиска отброшена (%s): %r", str(e)[:120], item)
    return DiscoveryAnswer(companies=companies, note=str(data.get("note") or "")[:500])


def discover(task: Task, settings: Settings, bridge: BridgeClient) -> DiscoveryAnswer:
    """One task → the model's list. Invalid JSON gets one retry with the error quoted; then DiscoveryFailed."""
    system = load_prompt_body(settings.prompts_dir / PROMPT_FILE)
    user = json.dumps(task.payload(), ensure_ascii=False)
    last = ""
    for attempt in range(2):
        try:
            text = bridge.complete(system, user, model=settings.discovery_model or None, allow_web=True,
                                   max_turns=settings.discovery_max_turns, timeout_s=settings.discovery_timeout_s)
        except (BridgeError, BridgeUnavailable) as e:
            raise DiscoveryFailed(f"мост: {e}") from e
        try:
            return parse_answer(extract_json(text))
        except (ValueError, ValidationError) as e:
            last = str(e)[:200]
            log.warning("Поиск щитовиков (%s): ответ не разобрался (%s)%s", task.label, last,
                        " — повтор" if attempt == 0 else "")
            user = json.dumps(task.payload(), ensure_ascii=False) + _RETRY_NOTE.format(error=last)
    raise DiscoveryFailed(f"ответ не разобрался дважды: {last}")


def store(conn: sqlite3.Connection, answer: DiscoveryAnswer, task: Task, res: JobResult) -> None:
    """Insert what is new; a domain already known through any source is the same company, not a lead."""
    with transaction(conn):
        for c in answer.companies:
            card = to_card(c, task)
            if card is None:
                continue
            res.found += 1
            if repo.vacancy_exists(conn, card.ext_id) or repo.domain_known(conn, card.domain):
                res.known += 1
                continue
            if repo.insert_discovered(conn, card):
                res.new += 1
                if defense.match(card.name):
                    res.defense += 1


def run_job(conn: sqlite3.Connection, settings: Settings, *, tasks_n: int | None = None, only: list[Task] | None = None,
            bridge: BridgeClient | None = None) -> JobResult:
    """`tasks_n` tasks from the cursor (default DISCOVERY_TASKS_PER_DAY), or exactly `only`. A failed task is logged,
    counted and skipped — the cursor moves on either way, so a dead vendor page does not stall the rotation."""
    res = JobResult()
    bridge = bridge or BridgeClient(settings, retries=0)
    if only is not None:
        todo = list(only)
    else:
        allt = tasks()
        cursor = int(kv_get(conn, KV_CURSOR) or 0) % len(allt)
        n = settings.discovery_tasks_per_day if tasks_n is None else tasks_n
        todo = [allt[(cursor + i) % len(allt)] for i in range(max(0, n))]
    for task in todo:
        res.tasks += 1
        res.labels.append(task.label)
        try:
            answer = discover(task, settings, bridge)
        except DiscoveryFailed as e:
            res.failed += 1
            log.warning("Поиск щитовиков (%s): %s", task.label, e)
        else:
            before = (res.found, res.new)
            store(conn, answer, task, res)
            log.info("Поиск щитовиков (%s): найдено %d, новых %d%s", task.label, res.found - before[0], res.new - before[1],
                     f"; заметка: {answer.note[:160]}" if answer.note else "")
        if only is None:
            with conn:
                kv_set(conn, KV_CURSOR, str((cursor + res.tasks) % len(allt)))
    res.calls, res.cost_usd = bridge.calls, bridge.cost_usd
    with conn:
        kv_set(conn, KV_LAST, f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}|{res.tasks}|{res.found}|{res.new}|{res.cost_usd:.2f}")
    log.info("Поиск щитовиков: %s", res.as_text())
    return res


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser(description="Find panel builders on the web through the bridge and store them as company leads")
    ap.add_argument("--tasks", type=int, default=None, help="how many tasks from the cursor (default: DISCOVERY_TASKS_PER_DAY)")
    ap.add_argument("--region", help="one region task instead of the cursor")
    ap.add_argument("--vendor", help="one vendor task instead of the cursor")
    ap.add_argument("--query", default=DISCOVERY_REGION_QUERIES[0], help="the phrase for --region")
    ap.add_argument("--dry-run", action="store_true", help="print the model's list, store nothing, keep the cursor")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    only = None
    if args.region:
        only = [Task("region", args.region, args.query)]
    elif args.vendor:
        only = [Task("vendor", args.vendor)]
    if args.dry_run:
        bridge = BridgeClient(settings, retries=0)
        for task in (only or tasks()[int(kv_get(conn, KV_CURSOR) or 0):][: (args.tasks or 1)]):
            try:
                answer = discover(task, settings, bridge)
            except DiscoveryFailed as e:
                print(f"{task.label}: ОШИБКА {e}")
                continue
            print(f"== {task.label}: {len(answer.companies)} компаний" + (f" · {answer.note}" if answer.note else ""))
            for c in answer.companies:
                card = to_card(c, task)
                known = card and (repo.vacancy_exists(conn, card.ext_id) or repo.domain_known(conn, card.domain))
                print(f"  {'=' if known else '+'} {c.name} — {c.website} ({c.city}) {', '.join(c.emails)}\n      {c.what_they_do}\n      {c.evidence_url}")
        print(f"вызовов {bridge.calls}, cost ${bridge.cost_usd:.2f}")
        return 0
    res = run_job(conn, settings, tasks_n=args.tasks, only=only)
    print(res.as_text())
    t = repo.site_totals(conn, SITE)
    print(f"всего в базе {t['total']}, ждут допуска {t['waiting']}, отправлено {t['sent']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
