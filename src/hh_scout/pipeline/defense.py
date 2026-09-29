"""Defence enterprises are not leads (decision #72, 2026-09-29).

The owner: «не хочу откликаться на вакансии, связанные с военными предприятиями, изготовлением средств нападения и
обороны» — and, asked how wide, chose to include holdings with a defence wing (Ростех, Алмаз-Антей, ОАК, ОСК, КТРВ, УВЗ …)
even for civil vacancies, and procurements whose customer is Минобороны, Росгвардия, ФСБ, ФСО or a defence plant.

This module is the name rule: a pure function over a company (or customer) name, used by every source before triage
(`prefilter.decide`, `sources/trudvsem.py`, `sources/owen.py`, `sources/zakupki.py`), by the per-row dedup hook
(`dedup.skip_if_covered`, so a company once marked closes its other rows) and by the queue sweep
(`repo.skip_defense_employers`). The lists live in `config.py` and are the owner's to edit. What a name does not
show — «входит в холдинг», гособоронзаказ in the description — the triage/evaluation flags and the dossier field
`defense` catch (`llm/schemas.py`, `llm/evaluator.py`, `llm/triage.py`, `llm/cover_letter.py`).

`skip_reason` values all start with `defense`: `defense:name:<entry>` (this rule), `defense:triage`,
`defense:evaluation`, `defense:dossier`, `defense:customer` (a procurement's customer), `defense:employer:<hh_id>`
(another row of a company already marked). Undo: `prefilter --requeue-reason 'defense%' --days N`.
"""

from __future__ import annotations

import re

from hh_scout.config import DEFENSE_ABBREVIATIONS, DEFENSE_EMPLOYER_WORDS, DEFENSE_EMPLOYERS, DEFENSE_WORD_STEMS

PREFIX = "defense"
NAME_PREFIX = "defense:name:"
CUSTOMER_REASON = "defense:customer"


def _fold(text: str) -> str:
    return text.casefold().replace("ё", "е")


_EMPLOYERS = tuple((e, _fold(e)) for e in DEFENSE_EMPLOYERS)
_WORD_RES = tuple((w, re.compile(r"(?<!\w)" + re.escape(_fold(w)) + r"(?!\w)")) for w in DEFENSE_EMPLOYER_WORDS)
_STEM_RES = tuple((w, re.compile(r"(?<!\w)" + re.escape(_fold(w)))) for w in DEFENSE_WORD_STEMS)
_ABBR_RES = tuple((a, re.compile(r"(?<![\w-])" + re.escape(a) + r"(?![\w-])")) for a in DEFENSE_ABBREVIATIONS)


def match(name: str | None) -> str | None:
    """The dictionary entry that makes `name` a defence enterprise, or None.

    Order: company names first (the log then names the holding), whole-word names, then word stems, then capital abbreviations
    as whole words of the *original* text — «ОСК» in «Росконтроль» or «оск» in a lower-case word never counts.
    """
    if not name:
        return None
    folded = _fold(name)
    for entry, low in _EMPLOYERS:
        if low in folded:
            return entry
    for word, rx in _WORD_RES:
        if rx.search(folded):
            return word
    for stem, rx in _STEM_RES:
        if rx.search(folded):
            return stem
    for abbr, rx in _ABBR_RES:
        if rx.search(name):
            return abbr
    return None


def name_reason(entry: str) -> str:
    return NAME_PREFIX + entry


def is_defense_reason(skip_reason: str | None) -> bool:
    return bool(skip_reason) and (skip_reason == PREFIX or skip_reason.startswith(PREFIX + ":"))
