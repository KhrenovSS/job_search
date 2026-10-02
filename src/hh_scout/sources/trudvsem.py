"""«Работа России» (trudvsem.ru) — the state job portal's open vacancy API as a second vacancy source (v9.25).

`GET https://opendata.trudvsem.ru/api/v1/vacancies?text=…&offset=<page>&limit=100[&modifiedFrom=ISO]` answers JSON
with everything a vacancy page on hh.ru would give — title, duties, requirements, skills, salary, region and the
employer's name, INN/OGRN and **contact e-mail** — so there is nothing to open in the browser: no page loads,
no daily limit, no triage. The text search is full-text (a query «CODESYS» finds it in the requirements), and
`modifiedFrom` returns only what changed since the last sync.

A row lands as `site='trudvsem'`, `hh_id='tv:<uuid>'`, `lead_kind='vacancy'`: the rules of `prefilter.decide`
run at insert time (stop words, engineering title, closed regions — here by the region's *name*, which the API
states outright), a passing row goes straight to `prefiltered` for the same evaluation and letter as an hh.ru
vacancy (`letter_key == 'hh'`), a failing one to `skipped/<reason>`. Only `TRUDVSEM_PER_RUN` rows enter evaluation per
sync; the rest wait as `new` and are admitted by the next syncs (`admit_waiting`) — the first sync alone brings
~700 rows for 30 days, hours of bridge time in one sitting. The employer cannot be answered on hh.ru,
so the letter goes by e-mail: the card shows «📧 Писать на:», and a vacancy without any address is
`skipped/no_email` (decision #56 applied to vacancies).

Panel builders (v9.40, decision #75): `TRUDVSEM_COMPANY_QUERIES` («сборщик щитов», …) find companies that assemble
cabinets; their rows are company leads — `lead_kind='company'`, `search_pass='panel'` — judged by the company, not the
title (the title rule is skipped as for an hh.ru company card), with their own gate `TRUDVSEM_COMPANY_PER_RUN`. They skip
triage like every portal row and go to `company_evaluation.md` → research → the partnership offer by e-mail.

Access: the host reaches the API only through the owner's direct (non-VPN) router route (decision #66).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from hh_scout.config import BLOCKED_REGIONS, TRUDVSEM_COMPANY_QUERIES, TRUDVSEM_QUERIES, Settings
from hh_scout.db import kv_get, kv_set, transaction, utcnow
from hh_scout.pipeline import dedup, home_region, prefilter, repo
from hh_scout.pipeline.contacts import normalize_email

log = logging.getLogger(__name__)

SITE = "trudvsem"
ID_PREFIX = "tv:"
SOURCE_PREFIX = "trudvsem:"
SEARCH_PASS = "regional"           # a plain vacancy, like an hh.ru card from the regional pass
COMPANY_PASS = "panel"             # a panel builder found by its assembler vacancy (v9.40) — the hh.ru channel's name
PAGE_SIZE = 100                    # the API's maximum
KV_LAST_SYNC = "trudvsem_last"     # "<utc iso>|<seen>|<new>" of the last successful sync
OVERLAP = timedelta(days=2)        # re-read this much before the last sync: `date_modify` is the portal's clock, not ours
COMPANY_URL = "https://trudvsem.ru/company/{code}"

# The API names regions in words. Decision #65 lists them by hh.ru id; here the same regions are recognised by a
# distinctive part of the name (Севастополь is its own region on the portal, part of Крым on hh.ru).
_REGION_MARKS: dict[str, str] = {
    "крым": BLOCKED_REGIONS[2114], "севастополь": BLOCKED_REGIONS[2114],
    "донецкая народная": BLOCKED_REGIONS[2134], "запорожская": BLOCKED_REGIONS[2155],
    "луганская народная": BLOCKED_REGIONS[2173], "херсонская": BLOCKED_REGIONS[2209],
}
_EMPLOYMENT = {"полная занятость": "full", "частичная занятость": "part", "временная": "project",
               "сезонная": "project", "стажировка": "project"}
_CITY_RE = re.compile(r"(?:^|,)\s*(?:г\.?|город|пгт\.?|п\.?|с\.?|рп\.?|село|посёлок|поселок|деревня|д\.)\s+([^,]+)", re.I)


class TrudvsemUnavailable(RuntimeError):
    """The API did not answer or answered nonsense — the sync is skipped, hh.ru is unaffected."""


@dataclass
class TvVacancy:
    ext_id: str                       # 'tv:<uuid>'
    title: str
    employer: str
    employer_id: str | None           # 'tv:<ogrn|inn|companycode>' — stable across the company's vacancies
    url: str
    company_url: str | None
    region: str
    city: str | None
    inn: str | None
    ogrn: str | None
    hr_agency: bool
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    contact_person: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    employment: str = "unknown"
    work_format: str = "unknown"
    employment_text: str | None = None
    schedule: str | None = None
    duty: str = ""
    requirements: str = ""
    skills: list[str] = field(default_factory=list)
    education: str | None = None
    experience_years: int | None = None
    specialisation: str | None = None
    addresses: list[str] = field(default_factory=list)
    modified_at: str | None = None    # as given by the portal ("2026-09-15T04:06:21+0300")
    created_at: str | None = None     # "2026-05-18"

    @property
    def area_name(self) -> str:
        return f"{self.city}, {self.region}" if self.city and self.city.casefold() not in self.region.casefold() \
            else self.region

    @property
    def description_html(self) -> str:
        """What the evaluator and the letter read as `raw_json.description` (the hh page's field)."""
        parts = []
        if self.duty:
            parts.append(f"<p><b>Обязанности:</b> {self.duty}</p>")
        if self.requirements:
            parts.append(f"<p><b>Требования:</b> {self.requirements}</p>")
        if self.skills:
            parts.append("<p><b>Навыки:</b> " + ", ".join(self.skills) + "</p>")
        facts = [f"занятость: {self.employment_text}" if self.employment_text else None,
                 f"график: {self.schedule}" if self.schedule else None,
                 f"образование: {self.education}" if self.education else None,
                 f"опыт: от {self.experience_years} лет" if self.experience_years else None,
                 f"отрасль: {self.specialisation}" if self.specialisation else None,
                 f"адрес: {self.addresses[0]}" if self.addresses else None]
        facts = [f for f in facts if f]
        if facts:
            parts.append("<p>" + "; ".join(facts) + "</p>")
        parts.append("<p>Источник: портал «Работа России» (trudvsem.ru).</p>")
        return "".join(parts)

    def raw(self) -> dict:
        """Stored as `vacancies.raw_json`; the keys the pipeline reads are the hh page's (`description`,
        `keySkills`, `workExperience`, `emails`), the rest is for the card and the dossier."""
        return {
            "site": SITE, "description": self.description_html, "keySkills": list(self.skills),
            "workExperience": f"от {self.experience_years} лет" if self.experience_years else None,
            "emails": list(self.emails), "phones": list(self.phones), "contact_person": self.contact_person,
            "inn": self.inn, "ogrn": self.ogrn, "region": self.region, "city": self.city,
            "company_url": self.company_url, "hr_agency": self.hr_agency, "specialisation": self.specialisation,
            "employment": self.employment_text, "schedule": self.schedule, "addresses": list(self.addresses),
            "modified_at": self.modified_at, "created_at": self.created_at,
        }

    def salary_raw(self) -> dict | None:
        if self.salary_from is None and self.salary_to is None:
            return None
        # The portal states monthly figures without saying gross or net; `normalize` treats None as "as is".
        return {"from": self.salary_from, "to": self.salary_to, "currencyCode": "RUR", "gross": None, "mode": "MONTH"}


def blocked_region_name(region: str | None) -> str | None:
    """The blocked region (decision #65) this portal region is, or None. Matched on the region's own name —
    the API states it as a field, so unlike a city name on hh.ru this is not a guess."""
    key = (region or "").casefold()
    for mark, name in _REGION_MARKS.items():
        if mark in key:
            return name
    return None


def _city_of(location: str | None) -> str | None:
    if not location:
        return None
    m = _CITY_RE.search(location)
    return m.group(1).strip() if m else None


def _int(v) -> int | None:
    try:
        return int(v) if v not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return None


def _skills(v: dict) -> list[str]:
    out = []
    for s in v.get("skills") or []:
        name = s if isinstance(s, str) else (s.get("name") or s.get("skill") or s.get("title")) if isinstance(s, dict) else None
        if name:
            out.append(str(name).strip())
    return out


def parse_vacancy(item: dict) -> TvVacancy | None:
    """One `results.vacancies[i].vacancy` → TvVacancy; None when the item lacks an id or a title."""
    v = item.get("vacancy") if "vacancy" in item else item
    if not isinstance(v, dict) or not v.get("id") or not (v.get("job-name") or "").strip():
        return None
    c = v.get("company") or {}
    code = c.get("ogrn") or c.get("inn") or c.get("companycode")
    emails: list[str] = []
    phones: list[str] = []
    for ct in v.get("contact_list") or []:
        kind, val = (ct.get("contact_type") or "").casefold(), (ct.get("contact_value") or "").strip()
        if not val:
            continue
        if "почт" in kind or "mail" in kind or "@" in val:
            emails.append(val)
        elif "телефон" in kind or "phone" in kind:
            phones.append(val)
    if c.get("email"):
        emails.append(c["email"])
    emails = list(dict.fromkeys(e for e in (normalize_email(x) for x in emails) if e))
    phones = list(dict.fromkeys(phones))
    addresses = [a.get("location") for a in (v.get("addresses") or {}).get("address") or [] if a.get("location")]
    region = (v.get("region") or {}).get("name") or ""
    employment_text = (v.get("employment") or "").strip() or None
    schedule = (v.get("schedule") or "").strip() or None
    employment = _EMPLOYMENT.get((employment_text or "").casefold(), "unknown")
    if schedule and "вахт" in schedule.casefold():
        employment = "fly_in_fly_out"
    work_format = "remote" if schedule and "удал" in schedule.casefold() else "unknown"
    req = v.get("requirement") or {}
    return TvVacancy(
        ext_id=ID_PREFIX + str(v["id"]), title=v["job-name"].strip(),
        employer=(c.get("name") or "").strip() or "компания не указана",
        employer_id=ID_PREFIX + str(code) if code else None,
        url=v.get("vac_url") or f"https://trudvsem.ru/vacancy/card/{c.get('companycode', '')}/{v['id']}",
        company_url=c.get("url") or (COMPANY_URL.format(code=c["companycode"]) if c.get("companycode") else None),
        region=region, city=_city_of(addresses[0] if addresses else None),
        inn=c.get("inn"), ogrn=c.get("ogrn"), hr_agency=bool(c.get("hr-agency")),
        emails=emails, phones=phones, contact_person=(v.get("contact_person") or "").strip() or None,
        salary_from=_int(v.get("salary_min")), salary_to=_int(v.get("salary_max")),
        employment=employment, work_format=work_format, employment_text=employment_text, schedule=schedule,
        duty=(v.get("duty") or "").strip(), requirements=(v.get("requirements") or "").strip(), skills=_skills(v),
        education=(req.get("education") or "").strip() or None, experience_years=_int(req.get("experience")),
        specialisation=(v.get("category") or {}).get("specialisation") or None, addresses=addresses,
        modified_at=v.get("date_modify"), created_at=v.get("creation-date"),
    )


def parse_vacancies(payload: dict) -> tuple[list[TvVacancy], int]:
    """The API answer → (vacancies, total matches on the portal)."""
    if not isinstance(payload, dict) or "results" not in payload:
        raise TrudvsemUnavailable("ответ API без поля results")
    items = (payload.get("results") or {}).get("vacancies") or []
    total = int((payload.get("meta") or {}).get("total") or len(items))
    out = [p for p in (parse_vacancy(i) for i in items) if p is not None]
    return out, total


def _published_at(v: TvVacancy) -> str:
    for s in (v.modified_at, v.created_at):
        if not s:
            continue
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            continue
    return utcnow()


def store_vacancy(conn: sqlite3.Connection, settings: Settings, v: TvVacancy, query: str, *,
                  admit: bool = True, search_pass: str = SEARCH_PASS, lead_kind: str = "vacancy") -> str | None:
    """Insert one vacancy through the card rules; returns the row's status, or None if it was already known.

    Same stages as an hh.ru card, only at once: rules → `prefiltered` (no page to open, no triage to run) or
    `skipped/<reason>`; a passing row is then checked against the company's existing lead (`dedup`).
    With `admit=False` a passing row waits as `new` for `admit_waiting` (the per-sync gate).
    `lead_kind="company"` (v9.40) stores a panel builder found by its assembler vacancy: the title rule does not
    apply — the company is the lead, the vacancy only how it was found.
    """
    if repo.vacancy_exists(conn, v.ext_id):
        return None
    facts = prefilter.CardFacts(hh_id=v.ext_id, title=v.title, applied=False, archived=False,
                                region=blocked_region_name(v.region), employer=v.employer,
                                company=(lead_kind == "company"),
                                outside=home_region.outside_name(v.area_name, home_region.stems_of(settings)))
    reason = prefilter.decide(facts)
    if reason is None and not v.emails:
        reason = "no_email"   # nothing to answer on hh.ru and nowhere to write — no lead (decision #56)
    status = "skipped" if reason else ("prefiltered" if admit else "new")
    now = utcnow()
    sal = v.salary_raw()
    conn.execute(
        """INSERT INTO vacancies(hh_id, site, title, employer, employer_id, url, area_name, area_path, work_format,
                                 employment, accept_temporary, civil_law_contracts, salary_from, salary_to, salary_raw,
                                 published_at, source, search_pass, lead_kind, raw_json, status, skip_reason, applied,
                                 first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (v.ext_id, SITE, v.title, v.employer, v.employer_id, v.url, v.area_name, None, v.work_format, v.employment,
         0, None, v.salary_from, v.salary_to, json.dumps(sal, ensure_ascii=False) if sal else None, _published_at(v),
         SOURCE_PREFIX + query, search_pass, lead_kind, json.dumps(v.raw(), ensure_ascii=False), status, reason, 0,
         now, now),
    )
    if v.employer_id:
        repo.record_contacts(conn, v.employer_id, emails=v.emails, urls=[])
    if status == "prefiltered":
        row = conn.execute("SELECT * FROM vacancies WHERE hh_id = ?", (v.ext_id,)).fetchone()
        if dedup.skip_if_covered(conn, settings, row) is not None:
            return "skipped"
    return status


def fetch_page(settings: Settings, text: str, page: int, modified_from: datetime | None) -> dict:
    """One page of the answer. `offset` is the page number (0-based), not a record offset: offset=100 is a 500."""
    params: dict[str, str | int] = {"text": text, "offset": page, "limit": PAGE_SIZE}
    if modified_from is not None:
        params["modifiedFrom"] = modified_from.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        resp = httpx.get(settings.trudvsem_api_url, params=params, timeout=settings.trudvsem_timeout_s,
                         headers={"User-Agent": settings.hh_user_agent, "Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise TrudvsemUnavailable(f"{e.__class__.__name__}: {str(e)[:160]}") from e


def admit_waiting(conn: sqlite3.Connection, settings: Settings, limit: int, *, lead_kind: str = "vacancy") -> int:
    """Let up to `limit` waiting rows (`new`, newest first) of one kind into evaluation; a row whose company got
    a lead meanwhile is skipped as a duplicate instead. Returns how many are now `prefiltered`."""
    if limit <= 0:
        return 0
    rows = conn.execute("SELECT * FROM vacancies WHERE site = ? AND status = 'new' AND lead_kind = ? "
                        "ORDER BY published_at DESC, id LIMIT ?", (SITE, lead_kind, limit)).fetchall()
    admitted = 0
    with transaction(conn):
        for row in rows:
            repo.set_status(conn, row["hh_id"], "prefiltered", None)
            fresh = conn.execute("SELECT * FROM vacancies WHERE id = ?", (row["id"],)).fetchone()
            if dedup.skip_if_covered(conn, settings, fresh) is None:
                admitted += 1
    if rows:
        log.info("Работа России: допущено в оценку %d из %d ждавших %s (по %d за подход)", admitted, len(rows),
                 "щитовиков" if lead_kind == "company" else "вакансий", limit)
    return admitted


def waiting(conn: sqlite3.Connection, lead_kind: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM vacancies WHERE site = ? AND status = 'new'"
    args: tuple = (SITE,)
    if lead_kind:
        sql += " AND lead_kind = ?"
        args += (lead_kind,)
    return int(conn.execute(sql, args).fetchone()[0])


@dataclass
class SyncResult:
    seen: int = 0
    new: int = 0
    prefiltered: int = 0
    waiting: int = 0        # passed the rules but wait for a later sync (the per-sync gate)
    company_seen: int = 0   # panel-builder queries (v9.40): company rows, their own gate
    company_new: int = 0
    company_prefiltered: int = 0
    company_waiting: int = 0
    requests: int = 0
    since: datetime | None = None


def last_sync(conn: sqlite3.Connection) -> datetime | None:
    raw = kv_get(conn, KV_LAST_SYNC)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.split("|", 1)[0])
    except ValueError:
        return None


def sync(conn: sqlite3.Connection, settings: Settings, *, days: int | None = None,
         queries: tuple[str, ...] = TRUDVSEM_QUERIES, company_queries: tuple[str, ...] = TRUDVSEM_COMPANY_QUERIES,
         sleep=time.sleep) -> SyncResult:
    """Read every query since the last sync (or `days` back) and store what is new. Raises
    `TrudvsemUnavailable` on the first failed request; what was stored before it stays.

    Vacancy queries go first, then the panel-builder ones (v9.40): a vacancy found by both stays a vacancy
    (`seen_ids` is shared), and each kind has its own per-sync gate.
    """
    res = SyncResult()
    started = datetime.now(timezone.utc)
    if days is not None:
        res.since = started - timedelta(days=days)
    else:
        last = last_sync(conn)
        res.since = (last - OVERLAP) if last else started - timedelta(days=settings.trudvsem_backfill_days)
    seen_ids: set[str] = set()
    # yesterday's tail first
    res.prefiltered += admit_waiting(conn, settings, settings.trudvsem_per_run)
    res.company_prefiltered += admit_waiting(conn, settings, settings.trudvsem_company_per_run, lead_kind="company")
    plan = [(q, SEARCH_PASS, "vacancy") for q in queries] + [(q, COMPANY_PASS, "company") for q in company_queries]
    for q, search_pass, lead_kind in plan:
        company = lead_kind == "company"
        got = 0
        for page_no in range(settings.trudvsem_max_pages_per_query):
            if res.requests:
                sleep(settings.trudvsem_request_gap_s)
            page = fetch_page(settings, q, page_no, res.since)
            res.requests += 1
            vacancies, total = parse_vacancies(page)
            with transaction(conn):
                for v in vacancies:
                    if v.ext_id in seen_ids:
                        continue
                    seen_ids.add(v.ext_id)
                    if company:
                        res.company_seen += 1
                        admit = res.company_prefiltered < settings.trudvsem_company_per_run
                    else:
                        res.seen += 1
                        admit = res.prefiltered < settings.trudvsem_per_run
                    status = store_vacancy(conn, settings, v, q, admit=admit, search_pass=search_pass, lead_kind=lead_kind)
                    if status is None:
                        continue
                    if company:
                        res.company_new += 1
                        res.company_prefiltered += status == "prefiltered"
                        res.company_waiting += status == "new"
                    else:
                        res.new += 1
                        res.prefiltered += status == "prefiltered"
                        res.waiting += status == "new"
            got += len(vacancies)
            if len(vacancies) < PAGE_SIZE or got >= total:
                break
    with conn:
        kv_set(conn, KV_LAST_SYNC, f"{started.isoformat(timespec='seconds')}|{res.seen + res.company_seen}|{res.new + res.company_new}")
    log.info("Работа России: вакансий в выдаче %d, новых %d (в оценку %d, ждут %d); щитовиков в выдаче %d, новых %d "
             "(в оценку %d, ждут %d); запросов %d, с %s",
             res.seen, res.new, res.prefiltered, waiting(conn, "vacancy"), res.company_seen, res.company_new,
             res.company_prefiltered, waiting(conn, "company"), res.requests,
             res.since.astimezone(timezone.utc).strftime("%d.%m %H:%M") if res.since else "начала")
    return res


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser(description="Sync vacancies from the «Работа России» open API into the lead base")
    ap.add_argument("--days", type=int, default=None, help="read this many days back instead of since the last sync")
    ap.add_argument("--file", help="parse a saved API answer instead of downloading (tests, offline)")
    ap.add_argument("--query", action="append", help="only these query texts (default: TRUDVSEM_QUERIES)")
    ap.add_argument("--company-query", action="append",
                    help="panel-builder query texts → company leads (default: TRUDVSEM_COMPANY_QUERIES; 'none' = skip)")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    if args.file:
        vacancies, total = parse_vacancies(json.load(open(args.file, encoding="utf-8")))
        with transaction(conn):
            new = sum(store_vacancy(conn, settings, v, "file") is not None for v in vacancies)
        print(f"В файле {len(vacancies)} (на портале {total}), новых {new}")
        return 0
    try:
        cq = TRUDVSEM_COMPANY_QUERIES
        if args.company_query:
            cq = () if args.company_query == ["none"] else tuple(args.company_query)
        res = sync(conn, settings, days=args.days, queries=tuple(args.query) if args.query else TRUDVSEM_QUERIES,
                   company_queries=cq)
    except TrudvsemUnavailable as e:
        print(f"ОШИБКА: {e}")
        return 1
    print(f"В выдаче {res.seen}, новых {res.new}, в оценку {res.prefiltered}, ждут следующих подходов {waiting(conn, 'vacancy')}; "
          f"щитовиков в выдаче {res.company_seen}, новых {res.company_new}, в оценку {res.company_prefiltered}, "
          f"ждут {waiting(conn, 'company')}; запросов {res.requests}; всего в базе {repo.count_site(conn, SITE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
