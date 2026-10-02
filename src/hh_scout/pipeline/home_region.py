"""Home regions (v9.43, decision #77): while the owner is away, leads come only from a short list of regions.

`HOME_REGIONS` in .env names hh.ru regions («Москва, Московская область, Тверская область, …»); empty — the whole
country, as before. With a list: the hh.ru search asks for exactly those areas (`collector`), the card rules put every
row from elsewhere into `skipped/outside_home:<место>` before any AI or page load (`prefilter.decide`, `details`), the
other sources judge their own region strings at insert (`trudvsem`, `zakupki`, `web_discovery`), and
`repo.skip_outside_home` withdraws what was already collected — hh.ru rows by the region id in `area.path`, the rest by
the words of the region in `area_name` / `raw_json` (ОВЕН writes «Белгородская обл», a procurement card «Г.МОСКВА», so
regions are matched by the stem of their name: «московск», «тверск», «москва»). A city that does not name its region
(«Люберцы») is outside unless listed in `HOME_EXTRA_PLACES`; no geography at all is let through, never guessed.
Undo after the trip: empty the list, restart, `prefilter --requeue-reason 'outside_home%' --days 30`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache

from hh_scout.config import Settings
from hh_scout.hh.areas import AreaResolutionError, path_ids, resolve_region_ids

log = logging.getLogger(__name__)

PREFIX = "outside_home:"
_GENERIC = {"область", "обл", "край", "республика", "респ", "автономный", "автономная", "округ", "город", "г", "ао"}
_ADJ_ENDINGS = ("ая", "ий", "ой", "ое", "ый")


@lru_cache(maxsize=16)
def stems(names: tuple[str, ...]) -> tuple[str, ...]:
    """Region names → the word stems a free-text place must contain: («Московская область», «Москва») →
    («московск», «москва»). Adjectives lose their ending so «Московская» also hits «Московской обл» and «МОСКОВСКАЯ»."""
    out: list[str] = []
    for name in names:
        for word in name.replace("-", " ").replace(",", " ").split():
            w = word.casefold().strip(".")
            if w in _GENERIC or len(w) < 4:
                continue
            if len(w) > 6 and w.endswith(_ADJ_ENDINGS):
                w = w[:-2]
            if w not in out:
                out.append(w)
    return tuple(out)


def stems_of(settings: Settings) -> tuple[str, ...]:
    return stems(settings.home_region_names + settings.home_extra_place_words) if settings.home_region_names else ()


def outside_name(place: str | None, home_stems: tuple[str, ...]) -> str | None:
    """The place as the skip reason's tail when it names somewhere outside home; None when it is home, empty or the
    list is off. Matching is by substring of the casefolded text, so «105203, Г.МОСКВА, УЛ…» is home."""
    if not home_stems or not place or not place.strip():
        return None
    text = place.casefold()
    if any(s in text for s in home_stems):
        return None
    return " ".join(place.split())[:60]


def outside_path(area_path: str | None, home_ids: list[int] | tuple[int, ...]) -> bool:
    """True when hh.ru's `area.path` has region ids and none of them is a home region. No path → False."""
    ids = path_ids(area_path)
    return bool(home_ids) and bool(ids) and not any(i in home_ids for i in ids)


@dataclass
class Home:
    ids: list[int] = field(default_factory=list)
    stems: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.stems)

    @classmethod
    def load(cls, conn: sqlite3.Connection, settings: Settings) -> "Home":
        """Ids through the cached hh.ru dictionary; a dictionary failure keeps the name rules and logs — a stale cache
        is refreshed by the collector on the next sitting anyway."""
        names = settings.home_region_names
        if not names:
            return cls()
        try:
            ids = resolve_region_ids(conn, settings, names)
        except (AreaResolutionError, Exception) as e:  # noqa: BLE001 — httpx errors too; the rule must not kill a sitting
            log.warning("Домашние регионы: справочник hh.ru недоступен (%s) — правило по id пропущено в этом подходе", e)
            ids = []
        return cls(ids=ids, stems=stems_of(settings))

    def hh_place(self, area_path: str | None, area_name: str | None) -> str | None:
        """The reason tail for an hh.ru row outside home, or None."""
        if outside_path(area_path, self.ids):
            return (area_name or area_path or "?")[:60]
        return None
