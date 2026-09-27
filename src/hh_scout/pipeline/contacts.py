"""Contact keys of an organisation: e-mail addresses and web domains (v9.22, decision #64).

"One company — one lead" used to know a company by its hh.ru id or name only, so the same inbox could get a
partnership offer from the ОВЕН catalogue and, a week later, a response to that firm's hh.ru vacancy. Every place
that learns a company's address — the catalogue entry, the company dossier, the vacancy page (`companySiteUrl`) —
turns it into `(kind, value)` keys here; `repo.record_contacts` stores them in `employer_contacts`, and
`repo.linked_employer_ids` finds the other employer ids that share one.

Stdlib only on purpose: the migration that backfills the table imports this module from `db.py`, which must not
depend on `pipeline.repo` (repo imports db).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

_EMAIL_RE = re.compile(r"^[^@\s<>()]+@[^@\s<>()]+\.[a-zA-Zа-яА-Я]{2,}$")

# Hosts that identify nobody: free mail, social networks, marketplaces, company registries a dossier may quote as the
# "website" and hh.ru / owen.ru themselves. An address there is still recorded as an e-mail — only the domain is not.
SHARED_DOMAINS: frozenset[str] = frozenset({
    "mail.ru", "bk.ru", "list.ru", "inbox.ru", "internet.ru", "xmail.ru", "yandex.ru", "ya.ru", "yandex.com", "narod.ru",
    "gmail.com", "googlemail.com", "rambler.ru", "lenta.ru", "myrambler.ru", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "me.com", "yahoo.com", "protonmail.com", "proton.me", "mail.com", "tut.by", "ukr.net", "i.ua",
    "vk.com", "vk.ru", "t.me", "telegram.me", "telegram.org", "ok.ru", "instagram.com", "facebook.com", "linkedin.com",
    "youtube.com", "hh.ru", "owen.ru", "avito.ru", "trudvsem.ru", "2gis.ru", "tiu.ru", "prom.ru", "pulscen.ru", "google.com",
    "sbis.ru", "rusprofile.ru", "zachestnyibiznes.ru", "list-org.com", "spark-interfax.ru", "checko.ru",
    "companies.rbc.ru", "kontur.ru", "focus.kontur.ru", "egrul.nalog.ru", "yandex.ru", "maps.yandex.ru",
})


def normalize_email(value: object) -> str | None:
    """`" Sales@Firm.RU. "` → `"sales@firm.ru"`; None for anything that is not an address."""
    text = str(value or "").strip().strip(".,;:").lower()
    return text if _EMAIL_RE.match(text) else None


def domain_of(value: object) -> str | None:
    """The host of a URL or of an e-mail address, lowercased, without `www.`; None for shared hosts and junk.

    `www.` only — `sales@mail.firm.ru` and `firm.ru` do not link (no registrable-domain collapsing); the rare
    miss is cheaper than gluing `ivanov.narod.ru` to every other narod site.
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    if "@" in text and " " not in text:
        email = normalize_email(text)
        if email is None:
            return None
        host = email.rsplit("@", 1)[1]
    else:
        if "://" not in text:
            text = "//" + text
        try:
            host = urlsplit(text).hostname or ""
        except ValueError:
            return None
    host = host.strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or host in SHARED_DOMAINS or re.search(r"[\s/@:]", host):
        return None
    return host


def contact_keys(emails: Iterable[object] = (), urls: Iterable[object] = ()) -> set[tuple[str, str]]:
    """`('email', addr)` for every valid address and `('domain', host)` for every non-shared host of a URL or address."""
    keys: set[tuple[str, str]] = set()
    for value in emails:
        email = normalize_email(value)
        if email is None:
            continue
        keys.add(("email", email))
        domain = domain_of(email)
        if domain:
            keys.add(("domain", domain))
    for value in urls:
        domain = domain_of(value)
        if domain:
            keys.add(("domain", domain))
    return keys
