"""zakupki.gov.ru (ЕИС) — winners of public procurements for automation work as company leads (channel `tender`, v9.26).

The owner's decision (28.09, #68): the lead is not the customer (under 44-ФЗ a cold letter to it is pointless) but
the **winner** — the contractor that has just signed a contract for SCADA/АСУ ТП work and needs the programming
part done now. The offer is subcontracting (`offer_focus` `subcontract_programming`, `commissioning_scada`).

Chain, all anonymous and allowed by robots.txt (`/*search*`, `/*rss*`, `/*notice*`, `/*contract*`; `Crawl-delay: 60`):
1. RSS of the extended notice search per phrase (`ZAKUPKI_QUERIES`), 44-ФЗ, stage «Закупка завершена»
   (`pc=on`), newest first → object, customer, price, regNumber, notice type → a `new` row per relevant notice
   (`relevant()`: the object names SCADA / АСУ ТП / диспетчеризация…, not licences, mobile links or road works).
2. For each `new` row: the notice's «Результаты определения поставщика» page → the contract table (reestrNumber,
   supplier). No contract yet → the row waits (`tries`), after `ZAKUPKI_MAX_TRIES` it is `skipped/tender:no_contract`.
3. The contract card → supplier INN, phone, **e-mail**, subject, price, signing date → the row becomes the
   winner's company lead: `employer`, `employer_id='zk:<ИНН>'`, `status='prefiltered'` (same evaluation and
   offer prompts as other company channels), addresses into `employer_contacts`.
223-ФЗ is left out: its contract card does not name the supplier on any public tab (checked 28.09).

One request a minute (`ZAKUPKI_REQUEST_GAP_S`), a daily scheduler job (`Scheduler.zakupki_job`), no browser.
TLS: the site's chain ends in the Russian Trusted Root CA (Минцифры) — `certs/russian_trusted_root_ca.pem`,
fingerprint checked against the live chain on 27.09.2026.
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import logging
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from hh_scout.config import PROJECT_ROOT, ZAKUPKI_QUERIES, Settings
from hh_scout.db import kv_set, transaction, utcnow
from hh_scout.pipeline import dedup, repo
from hh_scout.pipeline.contacts import normalize_email

log = logging.getLogger(__name__)

SITE = "zakupki"
PASS = "tender"
ID_PREFIX = "zk:"
BASE = "https://zakupki.gov.ru"
NOTICE_RSS = BASE + "/epz/order/extendedsearch/rss.html"
CA_PATH = PROJECT_ROOT / "certs" / "russian_trusted_root_ca.pem"
KV_LAST = "zakupki_last"   # "<utc iso>|<notices seen>|<new rows>|<resolved winners>"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

# What the procurement must be about (its object name), and what only looks like it.
_RELEVANT = re.compile(r"scada|асу\s?тп|асутп|автоматиз|диспетчер|плк\b|контроллер|телемехани|мнемосхем|hmi|"
                       r"пусконалад|пуско-налад|шкаф[а-я]* (?:управлени|автоматик)|программ[а-я]* обеспечени", re.I)
# Maintenance and support contracts are the bulk of the feed («техническое обслуживание диспетчеризации», АПС/СОУЭ,
# structured cabling, IT support): their winners are service firms, not integrators building a system now.
_NOISE = re.compile(r"лиценз|неисключительн|продлени|подписк|сотовой|связи\b|обучени|дорог|запчаст|лифт|платформ подъ|"
                    r"охран|уборк|питани|канцеляр|мебел|автомобил|бумаг|антивирус|1с\b|1c\b|"
                    r"техническ\w* обслуживани|техническ\w* и аварийн|аварийн\w* обслуживани|обеспечению эксплуатации|"
                    r"поддержк\w* пользовател|мониторинг\w* \(|апс\b|соуэ|пожарн|инвалид|структурированн|коммуникационн\w* \(|"
                    r"вентиляци|кондиционир|узлов учета|приборов учета|видеонаблюд|глонасс|транспорт", re.I)


class ZakupkiUnavailable(RuntimeError):
    """The site did not answer as expected — the job stops, what was stored stays."""


@dataclass
class Notice:
    reg_number: str
    law: str                       # "44" | "223"
    url: str
    notice_type: str               # ea20 / zk20 / ok20 / … — the path segment of the notice pages
    title: str                     # the object of the procurement
    customer: str
    price: str
    stage: str
    published: str | None          # ISO UTC
    ikz: str | None = None

    @property
    def ext_id(self) -> str:
        return ID_PREFIX + self.reg_number

    def results_url(self) -> str:
        return f"{BASE}/epz/order/notice/{self.notice_type}/view/supplier-results.html?regNumber={self.reg_number}"


@dataclass
class Supplier:
    name: str
    short_name: str | None
    inn: str | None
    kpp: str | None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    address: str | None = None
    status: str | None = None      # «субъект малого предпринимательства» etc.
    subject: str | None = None     # contract subject
    price: str | None = None
    signed: str | None = None      # dd.mm.yyyy
    deadline: str | None = None
    customer: str | None = None
    reestr_number: str | None = None


def _text(fragment: str) -> str:
    t = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return htmlmod.unescape(re.sub(r"\s+", " ", t)).strip()


def _field(text: str, label: str, stop: str = r"(?=\s+[А-ЯЁ][а-яё]+(?:\s[а-яё]+){0,3}:|$)") -> str:
    m = re.search(re.escape(label) + r":?\s*(.*?)" + stop, text)
    return m.group(1).strip() if m else ""


def relevant(title: str) -> bool:
    t = title or ""
    return bool(_RELEVANT.search(t)) and not _NOISE.search(t)


def parse_notice_rss(xml: str) -> list[Notice]:
    """RSS 2.0 of the extended search → notices. Items without a regNumber are dropped."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ZakupkiUnavailable(f"RSS не разбирается: {e}") from e
    out: list[Notice] = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        m = re.search(r"/epz/order/notice/([a-z0-9]+)/view/[^?]*\?regNumber=(\d+)", link)
        if not m:
            continue
        d = _text(htmlmod.unescape(it.findtext("description") or ""))
        law = "223" if "223-ФЗ" in d else "44"
        published = None
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            pass
        out.append(Notice(
            reg_number=m.group(2), law=law, url=link, notice_type=m.group(1),
            title=_field(d, "Наименование объекта закупки", r"(?=\s+Размещение выполняется|$)"),
            customer=_field(d, "Наименование Заказчика", r"(?=\s+Начальная цена|\s+Размещено|$)"),
            price=_field(d, "Начальная цена контракта", r"(?=\s+Валюта|\s+Размещено|$)"),
            stage=_field(d, "Этап размещения", r"(?=\s+ИКЗ|\s+Идентификационный|$)"),
            published=published, ikz=(re.search(r"ИКЗ:?\s*(\d{20,40})", d) or [None, None])[1],
        ))
    return out


def parse_supplier_results(page: str) -> list[tuple[str, str]]:
    """«Результаты определения поставщика» → [(contract reestrNumber, supplier name)] from the contract table;
    empty when no contract is registered yet."""
    i = page.find("с которым заключен контракт")
    if i < 0:
        return []
    out = []
    for row in re.findall(r"<tr class=\"tableBlock__row\">(.*?)</tr>", page[i:], flags=re.S):
        cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)]
        if len(cells) >= 4 and re.fullmatch(r"\d{15,25}", cells[0].split()[0] if cells[0] else ""):
            out.append((cells[0].split()[0], cells[2]))
    return out


def _short_name(full: str) -> str | None:
    m = re.search(r"\(([^()]*\"[^()]*\")\)", full) or re.search(r"\(([^()]+)\)\s*$", full)
    return m.group(1).strip() if m else None


def parse_contract_card(page: str) -> Supplier | None:
    """The 44-ФЗ contract card → the supplier with contacts and the contract's subject/dates; None if the
    supplier block is missing (a card without «Информация о поставщиках»)."""
    i = page.find("Информация о поставщиках")
    if i < 0:
        return None
    block = page[i:]
    rows = re.findall(r"<tr class=\"tableBlock__row\">(.*?)</tr>", block, flags=re.S)
    cells = None
    for r in rows:
        c = re.findall(r"<td[^>]*>(.*?)</td>", r, flags=re.S)
        if len(c) >= 5:
            cells = c
            break
    if not cells:
        return None
    org = _text(cells[0])
    inn = re.search(r"ИНН:\s*(\d{10,12})", org)
    kpp = re.search(r"КПП:\s*(\d{9})", org)
    name = re.sub(r"\s*ИНН:.*$", "", org).strip()
    contacts = _text(cells[4])
    emails = list(dict.fromkeys(e for e in (normalize_email(x) for x in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", contacts)) if e))
    phones = [p.strip() for p in re.findall(r"(?<![\w@.])\+?\d[\d\s()-]{6,}\d", re.sub(r"[\w.+-]+@[\w.-]+", " ", contacts))]
    text = _text(page)
    return Supplier(
        name=re.sub(r"\s*\([^()]*\)\s*$", "", name).strip() or name, short_name=_short_name(name),
        inn=inn.group(1) if inn else None, kpp=kpp.group(1) if kpp else None,
        emails=emails, phones=phones[:2], address=_text(cells[2]) or None, status=_text(cells[5]) if len(cells) > 5 else None,
        subject=_field(text, "Предмет контракта", r"(?=\s+Цена контракта|$)") or None,
        price=_field(text, "Цена контракта", r"(?=\s+Валюта|\s+Заключение|$)").replace("₽", "").strip() or None,
        signed=(re.search(r"Дата заключения контракта\s*(\d{2}\.\d{2}\.\d{4})", text) or [None, None])[1],
        deadline=(re.search(r"Срок исполнения\s*(\d{2}\.\d{2}\.\d{4})", text) or [None, None])[1],
        customer=_field(text, "Полное наименование заказчика", r"(?=\s+ИНН|\s+Сокращенное|$)") or None,
        reestr_number=(re.search(r"reestrNumber=(\d+)", page) or [None, None])[1],
    )


class Fetcher:
    """One request at a time with the crawl delay the site asks for. `get(url) -> text`."""

    def __init__(self, settings: Settings, sleep=time.sleep) -> None:
        self.s = settings
        self.sleep = sleep
        self._last: float | None = None
        self.requests = 0

    def get(self, url: str, params: dict | None = None) -> str:
        if self._last is not None:
            wait = self.s.zakupki_request_gap_s - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
        try:
            resp = httpx.get(url, params=params, timeout=self.s.zakupki_timeout_s, follow_redirects=True,
                             headers={"User-Agent": USER_AGENT, "Accept-Language": "ru"}, verify=str(CA_PATH))
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ZakupkiUnavailable(f"{e.__class__.__name__}: {str(e)[:160]}") from e
        finally:
            self._last = time.monotonic()
            self.requests += 1
        return resp.text


def rss_params(query: str, settings: Settings) -> dict[str, str]:
    p = {"searchString": query, "morphology": "on", "pageNumber": "1", "sortDirection": "false",
         "recordsPerPage": "_50", "showLotsInfoHidden": "false", "sortBy": "UPDATE_DATE", "fz44": "on", "pc": "on"}
    return p


def _description(n: Notice) -> str:
    return (f"<p><b>Закупка (44-ФЗ, {n.stage or 'закупка завершена'}):</b> {n.title}</p>"
            f"<p>Заказчик: {n.customer}. Начальная цена: {n.price} ₽. Извещение № {n.reg_number}.</p>")


def store_notice(conn: sqlite3.Connection, n: Notice, query: str) -> bool:
    """A relevant completed notice becomes a `new` row waiting for its winner. False if already known."""
    if repo.vacancy_exists(conn, n.ext_id):
        return False
    now = utcnow()
    raw = {"site": SITE, "law": n.law, "reg_number": n.reg_number, "notice_type": n.notice_type, "object": n.title,
           "customer": n.customer, "price": n.price, "stage": n.stage, "ikz": n.ikz, "query": query, "tries": 0,
           "description": _description(n)}
    conn.execute(
        """INSERT INTO vacancies(hh_id, site, title, employer, employer_id, url, area_name, work_format, employment,
                                 published_at, source, search_pass, lead_kind, raw_json, status, applied,
                                 first_seen_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (n.ext_id, SITE, f"Закупка: {n.title}"[:300], None, None, n.url, None, "unknown", "project",
         n.published or now, f"zakupki:{query}", PASS, "company", json.dumps(raw, ensure_ascii=False), "new", 0, now, now),
    )
    return True


def _city(address: str | None) -> str | None:
    """«…, Г. ЧЕБОКСАРЫ, …» / «ГОРОД МОСКВА» / «С ЭНИКАЛИ» → the settlement; «г.о.» (городской округ) and
    «м.р-н.» are skipped — they name the district, not the town."""
    if not address:
        return None
    for part in address.split(","):
        part = part.strip()
        m = re.match(r"(?:Г\.|ГОРОД|Г|С\.|С|СЕЛО|ПГТ\.?|П\.|ПОС\.|Д\.|ДЕРЕВНЯ|РП\.?)\s*([А-ЯЁ][А-ЯЁа-яё-]+(?:\s[А-ЯЁ][А-ЯЁа-яё-]+)?)$",
                     part, flags=re.I)
        if m and not re.match(r"(?:г\.о\.|м\.р-н)", part, flags=re.I):
            return " ".join(w.capitalize() for w in m.group(1).split())
    return None


def resolve_row(conn: sqlite3.Connection, settings: Settings, fetcher: Fetcher, row: sqlite3.Row) -> str:
    """Find the winner of one `new` notice. Returns the row's new status ('prefiltered', 'skipped' or 'new' = wait)."""
    raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    n = Notice(reg_number=raw.get("reg_number") or row["hh_id"][len(ID_PREFIX):], law=raw.get("law", "44"),
               url=row["url"], notice_type=raw.get("notice_type", "ea20"), title=raw.get("object") or row["title"],
               customer=raw.get("customer", ""), price=raw.get("price", ""), stage=raw.get("stage", ""), published=None)
    contracts = parse_supplier_results(fetcher.get(n.results_url()))
    raw["tries"] = int(raw.get("tries") or 0) + 1
    if not contracts:
        if raw["tries"] >= settings.zakupki_max_tries:
            repo.set_status(conn, row["hh_id"], "skipped", "tender:no_contract")
            conn.execute("UPDATE vacancies SET raw_json = ? WHERE id = ?", (json.dumps(raw, ensure_ascii=False), row["id"]))
            return "skipped"
        conn.execute("UPDATE vacancies SET raw_json = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(raw, ensure_ascii=False), utcnow(), row["id"]))
        return "new"
    reestr, supplier_name = contracts[0]
    card_url = f"{BASE}/epz/contract/contractCard/common-info.html?reestrNumber={reestr}"
    supplier = parse_contract_card(fetcher.get(card_url))
    if supplier is None:
        supplier = Supplier(name=supplier_name, short_name=_short_name(supplier_name), inn=None, kpp=None, reestr_number=reestr)
    if not supplier.inn and not supplier.name:
        repo.set_status(conn, row["hh_id"], "skipped", "tender:no_supplier")
        return "skipped"
    employer = supplier.short_name or supplier.name
    employer_id = ID_PREFIX + (supplier.inn or re.sub(r"\W+", "", employer.casefold())[:40])
    raw.update({"winner": supplier.name, "winner_short": supplier.short_name, "inn": supplier.inn, "kpp": supplier.kpp,
                "emails": supplier.emails, "phones": supplier.phones, "address": supplier.address,
                "supplier_status": supplier.status, "contract_subject": supplier.subject, "contract_price": supplier.price,
                "contract_signed": supplier.signed, "contract_deadline": supplier.deadline,
                "contract_reestr": reestr, "contract_url": card_url})
    raw["description"] = (_description(n) + f"<p><b>Победитель (исполнитель контракта):</b> {supplier.name}"
                          + (f", ИНН {supplier.inn}" if supplier.inn else "") + (f", {supplier.address}" if supplier.address else "")
                          + ".</p>" + (f"<p>Контракт от {supplier.signed}" if supplier.signed else "<p>Контракт")
                          + (f" на {supplier.price} ₽" if supplier.price else "")
                          + (f", срок исполнения до {supplier.deadline}" if supplier.deadline else "")
                          + (f": {supplier.subject}" if supplier.subject else "") + ".</p>")
    conn.execute("UPDATE vacancies SET employer = ?, employer_id = ?, area_name = ?, raw_json = ?, status = 'prefiltered', "
                 "skip_reason = NULL, updated_at = ? WHERE id = ?",
                 (employer, employer_id, _city(supplier.address), json.dumps(raw, ensure_ascii=False), utcnow(), row["id"]))
    repo.record_contacts(conn, employer_id, emails=supplier.emails, urls=[])
    fresh = conn.execute("SELECT * FROM vacancies WHERE id = ?", (row["id"],)).fetchone()
    if dedup.skip_if_covered(conn, settings, fresh) is not None:
        return "skipped"
    log.info("Победитель закупки: %s (ИНН %s) — «%s» для %s, %s ₽; e-mail %s", employer, supplier.inn or "—",
             (supplier.subject or n.title)[:70], (n.customer or "—")[:40], supplier.price or "?",
             supplier.emails[0] if supplier.emails else "нет")
    return "prefiltered"


@dataclass
class JobResult:
    notices: int = 0
    relevant: int = 0
    new: int = 0
    resolved: int = 0
    waiting: int = 0
    skipped: int = 0
    requests: int = 0


def run_job(conn: sqlite3.Connection, settings: Settings, fetcher: Fetcher | None = None,
            queries: tuple[str, ...] = ZAKUPKI_QUERIES, *, resolve_limit: int | None = None) -> JobResult:
    """The daily job: read the feeds, then resolve up to `ZAKUPKI_PAGES_PER_RUN` waiting notices (2 pages each)."""
    f = fetcher or Fetcher(settings)
    res = JobResult()
    for q in queries:
        notices = parse_notice_rss(f.get(NOTICE_RSS, rss_params(q, settings)))
        res.notices += len(notices)
        with transaction(conn):
            for n in notices:
                if n.law != "44" or not relevant(n.title):
                    continue
                res.relevant += 1
                res.new += store_notice(conn, n, q)
    limit = settings.zakupki_pages_per_run // 2 if resolve_limit is None else resolve_limit
    # first looks first (a contract shows up 1–3 weeks after the notice completes), then the newest notices
    rows = conn.execute("SELECT * FROM vacancies WHERE site = ? AND status = 'new' "
                        "ORDER BY COALESCE(json_extract(raw_json, '$.tries'), 0), published_at DESC, id LIMIT ?",
                        (SITE, limit)).fetchall()
    for row in rows:
        with transaction(conn):
            status = resolve_row(conn, settings, f, row)
        res.resolved += status == "prefiltered"
        res.waiting += status == "new"
        res.skipped += status == "skipped"
    res.requests = f.requests
    with conn:
        kv_set(conn, KV_LAST, f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}|{res.notices}|{res.new}|{res.resolved}")
    log.info("Закупки: извещений %d, по теме %d, новых %d; победителей найдено %d, ждут контракта %d, закрыто %d; запросов %d",
             res.notices, res.relevant, res.new, res.resolved, res.waiting, res.skipped, res.requests)
    return res


def totals(conn: sqlite3.Connection) -> dict[str, int]:
    r = conn.execute("SELECT SUM(status = 'new') w, SUM(status = 'sent') s, COUNT(*) t FROM vacancies WHERE site = ?",
                     (SITE,)).fetchone()
    return {"waiting": int(r["w"] or 0), "sent": int(r["s"] or 0), "total": int(r["t"] or 0)}


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser(description="Winners of automation procurements on zakupki.gov.ru → company leads")
    ap.add_argument("--resolve", type=int, default=None, help="how many waiting notices to resolve now (default: ZAKUPKI_PAGES_PER_RUN/2)")
    ap.add_argument("--query", action="append", help="only these phrases (default: ZAKUPKI_QUERIES)")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    try:
        res = run_job(conn, settings, queries=tuple(args.query) if args.query else ZAKUPKI_QUERIES, resolve_limit=args.resolve)
    except ZakupkiUnavailable as e:
        print(f"ОШИБКА: {e}")
        return 1
    t = totals(conn)
    print(f"Извещений {res.notices}, по теме {res.relevant}, новых {res.new}; победителей {res.resolved}, ждут контракта "
          f"{res.waiting}, закрыто {res.skipped}; запросов {res.requests}. В базе: всего {t['total']}, ждут {t['waiting']}, "
          f"отправлено {t['sent']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
