"""The ОВЕН system-integrator catalogue as a source of company leads (v9.13, decision #53).

`https://owen.ru/spisok_sistemnih_integratorov` is rendered by a script that fetches one public JSON
(`integrators.php`): ~230 companies with name, site, city, region, industries, phones, e-mails, partner status and
a link to their project page on owen.ru. These companies deliver ОВЕН/CODESYS projects — the owner's core stack —
and are the most natural subcontract customers for the programming part. Read with plain HTTP from the host, no
browser, no hh.ru, no page loads from the daily cap.

Entries land in `vacancies` as `new` company leads (`site='owen'`, `hh_id='owen:<tag_id>'`) and are let into
evaluation a few per day (`repo.admit_company_leads`), so the catalogue does not crowd out vacancy leads.

CLI:  python -m hh_scout.sources.owen [--admit N] [--file integrators.json]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import httpx

from hh_scout.browser.hh_pages import strip_html
from hh_scout.config import Settings
from hh_scout.db import transaction
from hh_scout.pipeline import repo

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
SKIP_STATUSES = frozenset({"Дилеры"})   # resellers, not integrators: nobody there programs anything


@dataclass(frozen=True)
class IntegratorCard:
    tag_id: str
    name: str
    site: str | None
    city: str | None
    region: str | None
    status: str
    projects_url: str | None
    address: str | None
    description: str
    industries: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()

    @property
    def ext_id(self) -> str:
        return f"owen:{self.tag_id}"


_SLUG_RE = re.compile(r"[^0-9a-zа-яё]+")


def _key(it: dict) -> str | None:
    """The feed's `tag_id`, or a slug of the name when the feed left it empty (it does, for a few dozen entries)."""
    if it.get("tag_id"):
        return str(it["tag_id"])
    slug = _SLUG_RE.sub("-", str(it.get("name") or "").casefold()).strip("-")
    return f"n-{slug[:60]}" if slug else None


class OwenCatalogUnavailable(RuntimeError):
    """The feed did not answer or did not look like the catalogue."""


def _names(items: Any) -> tuple[str, ...]:
    """`[{"name": "a@b.ru", "link": …}]` → ("a@b.ru",); tolerant of plain strings."""
    out: list[str] = []
    for it in items or []:
        value = it.get("name") if isinstance(it, dict) else it
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return tuple(dict.fromkeys(out))


def parse_integrators(data: Any) -> list[IntegratorCard]:
    """The feed's `items` as cards; hidden entries and dealers are left out."""
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise OwenCatalogUnavailable("в ответе каталога нет списка items")
    cards: list[IntegratorCard] = []
    for it in items:
        if not isinstance(it, dict) or it.get("hidden") or not it.get("name"):
            continue
        key = _key(it)
        if key is None:
            continue
        status = str(it.get("status") or "").strip() or "Без партнерства"
        if status in SKIP_STATUSES:
            continue
        description = strip_html(str(it.get("description") or ""))
        industries = tuple(str(x).strip() for x in (it.get("industries") or []) if str(x).strip())
        if not description and industries:
            description = "Отрасли по каталогу ОВЕН: " + ", ".join(industries)
        cards.append(IntegratorCard(
            tag_id=key, name=str(it["name"]).strip(),
            site=(str(it.get("site")).strip() or None) if it.get("site") else None,
            city=(str(it.get("city")).strip() or None) if it.get("city") else None,
            region=(str(it.get("region")).strip() or None) if it.get("region") else None,
            status=status, projects_url=(str(it.get("sp_projects")).strip() or None) if it.get("sp_projects") else None,
            address=(str(it.get("address")).strip() or None) if it.get("address") else None,
            description=description, industries=industries, emails=_names(it.get("emails")), phones=_names(it.get("phones")),
        ))
    return cards


def fetch_integrators(settings: Settings, timeout_s: float = 30.0) -> list[IntegratorCard]:
    """Download the catalogue. One plain request, like the page's own script makes."""
    headers = {"User-Agent": UA, "Referer": settings.owen_integrators_referer, "Accept": "application/json, text/plain, */*"}
    try:
        r = httpx.get(settings.owen_integrators_url, headers=headers, timeout=timeout_s, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise OwenCatalogUnavailable(f"каталог ОВЕН не ответил: {e.__class__.__name__}: {e}") from e
    return parse_integrators(data)


def store(conn: sqlite3.Connection, cards: list[IntegratorCard]) -> int:
    """New catalogue companies as `new` leads; known ones (by `hh_id`) are left untouched. Returns how many were new."""
    new = 0
    with transaction(conn):
        for card in cards:
            if repo.insert_integrator(conn, card):
                new += 1
    return new


def refresh(conn: sqlite3.Connection, settings: Settings) -> tuple[int, int]:
    """Fetch + store. Returns (new, total in the feed). Raises OwenCatalogUnavailable — the caller decides how loud."""
    cards = fetch_integrators(settings)
    new = store(conn, cards)
    log.info("Каталог ОВЕН: в ленте %d интеграторов, новых %d", len(cards), new)
    return new, len(cards)


def main() -> int:
    from hh_scout.config import load_settings
    from hh_scout.db import open_db
    from hh_scout.logging_setup import setup_logging

    ap = argparse.ArgumentParser(description="Read the ОВЕН integrator catalogue into the lead base")
    ap.add_argument("--admit", type=int, default=None,
                    help="let this many catalogue companies into evaluation now (default: COMPANY_LEADS_PER_DAY)")
    ap.add_argument("--file", help="parse a saved JSON instead of downloading (tests, offline)")
    args = ap.parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    conn = open_db(settings.db_path)
    if args.file:
        cards = parse_integrators(json.load(open(args.file, encoding="utf-8")))
        new, total = store(conn, cards), len(cards)
    else:
        try:
            new, total = refresh(conn, settings)
        except OwenCatalogUnavailable as e:
            print(f"ОШИБКА: {e}")
            return 1
    per_day = settings.company_leads_per_day if args.admit is None else args.admit
    with conn:
        admitted = repo.admit_company_leads(conn, per_day)
    t = repo.owen_totals(conn)
    print(f"В ленте {total}, новых {new}; допущено в оценку сейчас {admitted}; "
          f"всего в базе {t['total']}, ждут допуска {t['waiting']}, отправлено {t['sent']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
